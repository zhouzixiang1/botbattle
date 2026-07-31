"""Bots / Matches / Contests / Admin / Leaderboard API 路由。"""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bzplat.backend.auth.dependencies import (
    require_admin,
    require_organizer,
    require_user,
)
from bzplat.backend.bots import BotError, BotManager
from bzplat.backend.contests import ContestManager
from bzplat.backend.contests.stages import estimate_match_count
from bzplat.backend.contests.validation import validate_stage, validate_template
from bzplat.backend.matches import MatchOrchestrator
from bzplat.backend.runtime.limits import (
    ACTION_TIMEOUT_MAX,
    ACTION_TIMEOUT_MIN,
    BOT_CPUS,
    BOT_MEMORY_MB,
    clamp_concurrent,
    concurrent_ceiling,
    cpu_count,
)
from bzplat.backend.store.schema import (
    SETTING_ACTION_TIMEOUT,
    SETTING_AUTO_MATCH_BOT_COOLDOWN,
    SETTING_AUTO_MATCH_ENABLED,
    SETTING_AUTO_MATCH_INTERVAL_SEC,
    SETTING_AUTO_MATCH_MIN_IDLE_SEC,
    SETTING_AUTO_MATCH_RESERVE_SLOTS,
    SETTING_AUTO_MATCH_STALE_SEC,
    SETTING_CONTEST_REST,
    SETTING_JUDGE_GOMOKU_SIZE,
    SETTING_JUDGE_HOLDEM_BB,
    SETTING_JUDGE_HOLDEM_HANDS,
    SETTING_JUDGE_HOLDEM_SB,
    SETTING_JUDGE_HOLDEM_STACK,
    SETTING_MAX_CONCURRENT,
)
router = APIRouter()


def _orch(request: Request) -> MatchOrchestrator:
    return request.app.state.orch


def _bots(request: Request) -> BotManager:
    return request.app.state.bot_manager


def _contests(request: Request) -> ContestManager:
    return request.app.state.contest_manager


def _store(request: Request):
    return request.app.state.store


# ── bots ──────────────────────────────────────────────────────

@router.get("/api/bots/mine")
def my_bots(
    request: Request,
    user=Depends(require_user),
    game_id: str | None = None,
):
    return {"bots": _bots(request).list_mine(user["id"], game_id=game_id)}


@router.get("/api/bots/public")
def public_bots(
    request: Request, game_id: str | None = None, owner_id: int | None = None
):
    return {"bots": _bots(request).list_public(game_id=game_id, owner_id=owner_id)}


@router.get("/api/users")
def search_users(request: Request, q: str | None = None, limit: int = 20):
    """按用户名前缀搜索（公开，仅返回 id/username/display_name）。"""
    return {"users": _store(request).search_users(q or "", limit=max(1, min(limit, 50)))}


@router.get("/api/bots/{bot_id}")
def get_bot(bot_id: int, request: Request):
    bot = _bots(request).get(bot_id)
    if not bot:
        raise HTTPException(404, "bot 不存在")
    return {"bot": bot}


@router.post("/api/bots")
async def upload_bot(
    request: Request,
    name: str = Form(...),
    display_name: str = Form(""),
    description: str = Form(""),
    upload_note: str = Form(""),
    is_public: bool = Form(True),
    game_id: str = Form("holdem"),
    file: UploadFile = File(...),
    user=Depends(require_user),
):
    raw = await file.read()
    try:
        bot = _bots(request).create_from_upload(
            user["id"], name, raw,
            display_name=display_name, description=description,
            upload_note=upload_note, is_public=is_public,
            game_id=game_id,
        )
    except BotError as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message})
    return {"bot": bot}


@router.post("/api/bots/{bot_id}/versions")
async def upload_bot_version(
    bot_id: int,
    request: Request,
    upload_note: str = Form(""),
    file: UploadFile = File(...),
    user=Depends(require_user),
):
    raw = await file.read()
    try:
        bot = _bots(request).upload_version(
            bot_id, user["id"], raw, upload_note=upload_note
        )
    except BotError as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message})
    return {"bot": bot}


@router.post("/api/bots/{bot_id}/active")
def set_bot_active(
    bot_id: int, request: Request, active: bool = True, user=Depends(require_user)
):
    try:
        bot = _bots(request).set_active(bot_id, user["id"], active)
    except BotError as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message})
    return {"bot": bot}


# ── matches ───────────────────────────────────────────────────

class ChallengeBody(BaseModel):
    my_bot_id: int
    opponent_bot_id: int
    hands: int = Field(70, ge=1, le=70)
    game_id: str | None = None


@router.post("/api/matches/challenge")
async def challenge(body: ChallengeBody, request: Request, user=Depends(require_user)):
    try:
        mid = await _orch(request).challenge(
            body.my_bot_id,
            body.opponent_bot_id,
            user["id"],
            hands=body.hands,
            game_id=body.game_id,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"match_id": mid, "status": "pending"}


class HumanChallengeBody(BaseModel):
    bot_id: int
    human_seat: int = 1  # 0 或 1，人类坐哪位
    game_id: str | None = None
    hands: int = Field(70, ge=1, le=300)
    n_dots: int | None = None


@router.post("/api/matches/human")
async def challenge_human(body: HumanChallengeBody, request: Request, user=Depends(require_user)):
    """人类 vs bot：当前登录用户作为人类玩家对战指定 bot。"""
    if body.human_seat not in (0, 1):
        raise HTTPException(400, "human_seat 须为 0 或 1")
    try:
        mid = await _orch(request).challenge_human(
            body.bot_id,
            user["id"],
            human_seat=body.human_seat,
            game_id=body.game_id,
            hands=body.hands,
            n_dots=body.n_dots,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"match_id": mid, "status": "pending"}


@router.websocket("/api/matches/{match_id}/play")
async def play_websocket(websocket: WebSocket, match_id: str):
    """人类对战双向通道：推送事件（含 your_turn）+ 接收人类落子。

    鉴权：query 参数 token（Bearer）或 cookie bz_session。
    仅 match.human_user_id 本人可连；解析 pending 人类回合 Future。
    """
    store = websocket.app.state.store
    auth = websocket.app.state.auth
    orch = websocket.app.state.orch
    # 鉴权
    token = websocket.query_params.get("token") or websocket.cookies.get("bz_session")
    user = auth.verify_session(token)
    m = store.get_match(match_id)
    if not user or not m or m.get("human_user_id") != user["id"]:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "无权访问该对局"})
        await websocket.close()
        return
    await websocket.accept()
    # 订阅事件流
    q = orch.subscribe(match_id)
    # 先发历史快照
    replay = store.get_replay(match_id) or {}
    try:
        await websocket.send_json({
            "type": "snapshot",
            "match": m,
            "events": json.loads(replay.get("events_json") or "[]"),
        })
    except Exception:
        pass
    human_seat = int(m.get("human_seat")) if m.get("human_seat") is not None else 1

    async def pump_events():
        while True:
            ev = await q.get()
            await websocket.send_json(ev)
            if isinstance(ev, dict) and ev.get("type") in ("match_end", "error"):
                return

    task = asyncio.create_task(pump_events())
    try:
        while True:
            msg = await websocket.receive_json()
            # 解析人类落子
            move = msg if isinstance(msg, dict) else {}
            if not orch.resolve_human_turn(match_id, human_seat, move):
                await websocket.send_json({"type": "reject", "message": "当前非你的回合或动作非法"})
    except WebSocketDisconnect:
        pass
    finally:
        task.cancel()
        orch.unsubscribe(match_id, q)


@router.get("/api/matches")
def list_matches(
    request: Request,
    status: str | None = None,
    game_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    rows = _store(request).list_matches(
        status=status, game_id=game_id, limit=limit, offset=offset
    )
    return {"matches": rows}


@router.get("/api/matches/{match_id}")
def match_detail(match_id: str, request: Request):
    m = _store(request).get_match(match_id)
    if not m:
        raise HTTPException(404, "对局不存在")
    replay = _store(request).get_replay(match_id) or {}
    return {"match": m, "replay": replay}


@router.get("/api/matches/{match_id}/events")
async def match_events(match_id: str, request: Request):
    orch = _orch(request)
    if not _store(request).get_match(match_id):
        raise HTTPException(404, "对局不存在")
    q = orch.subscribe(match_id)

    async def gen():
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                payload = json.dumps(ev, ensure_ascii=False)
                yield f"data: {payload}\n\n"
                if ev.get("type") in ("match_end", "error"):
                    break
        finally:
            orch.unsubscribe(match_id, q)

    return StreamingResponse(gen(), media_type="text/event-stream")


# ── leaderboard ───────────────────────────────────────────────

@router.get("/api/leaderboard")
def leaderboard(request: Request, limit: int = 50, game_id: str | None = None):
    return {"leaderboard": _store(request).list_leaderboard(limit=limit, game_id=game_id)}


# ── contests ──────────────────────────────────────────────────

class ContestCreate(BaseModel):
    title: str
    description: str = ""
    hands_per_match: int = 70
    template_id: str | None = None
    game_id: str | None = None
    stages: list[dict[str, Any]] | None = None
    match_config: dict[str, Any] | None = None


class ContestRegister(BaseModel):
    bot_id: int


class ContestDispatch(BaseModel):
    bot_id: int


@router.get("/api/contests/templates")
def contest_templates(request: Request, game: str | None = None):
    # 从 contest_templates 表读（与 admin 同源，含覆盖；修复原读代码默认的不一致）
    return {"templates": _store(request).list_contest_templates(game_id=game)}


@router.get("/api/contests")
def list_contests(request: Request, status: str | None = None):
    return {"contests": _store(request).list_contests(status=status)}


@router.post("/api/contests")
def create_contest(body: ContestCreate, request: Request, user=Depends(require_organizer)):
    try:
        c = _contests(request).create(
            user["id"],
            body.title,
            description=body.description,
            hands_per_match=body.hands_per_match,
            template_id=body.template_id,
            game_id=body.game_id,
            stages=body.stages,
            match_config=body.match_config,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"contest": c}


@router.get("/api/contests/{contest_id}")
def contest_detail(contest_id: int, request: Request):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404, "比赛不存在")
    entries = _store(request).list_contest_entries(contest_id)
    pairings = _store(request).list_contest_pairings(contest_id)
    standings = _contests(request).standings(contest_id)
    stage_results = _store(request).list_stage_results(contest_id)
    try:
        estimate = _contests(request).estimate(contest_id)
    except ValueError:
        estimate = None
    return {
        "contest": c,
        "entries": entries,
        "pairings": pairings,
        "standings": standings,
        "stage_results": stage_results,
        "estimate": estimate,
    }


@router.post("/api/contests/{contest_id}/open")
def open_contest(contest_id: int, request: Request, user=Depends(require_organizer)):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404)
    if c["organizer_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(403)
    return {"contest": _contests(request).open_registration(contest_id)}


@router.post("/api/contests/{contest_id}/register")
def register_contest(
    contest_id: int, body: ContestRegister, request: Request, user=Depends(require_user)
):
    try:
        entry = _contests(request).register(
            contest_id, user["id"], body.bot_id, role=user.get("role", "")
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"entry": entry}


@router.post("/api/contests/{contest_id}/dispatch")
def dispatch_contest(
    contest_id: int, body: ContestDispatch, request: Request, user=Depends(require_user)
):
    try:
        entry = _contests(request).dispatch(
            contest_id, user["id"], body.bot_id, role=user.get("role", "")
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"entry": entry}


@router.post("/api/contests/{contest_id}/start")
async def start_contest(
    contest_id: int, request: Request, user=Depends(require_organizer)
):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404)
    if c["organizer_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(403)
    try:
        contest = await _contests(request).start(contest_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"contest": contest}


@router.post("/api/contests/{contest_id}/resume")
async def resume_contest(
    contest_id: int, request: Request, user=Depends(require_organizer)
):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404)
    if c["organizer_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(403)
    try:
        contest = await _contests(request).resume(contest_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"contest": contest}


@router.post("/api/contests/{contest_id}/advance")
async def advance_contest(
    contest_id: int, request: Request, user=Depends(require_organizer)
):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404)
    if c["organizer_id"] != user["id"] and user["role"] != "admin":
        raise HTTPException(403)
    try:
        contest = await _contests(request).advance(contest_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"contest": contest}


# ── admin ─────────────────────────────────────────────────────

@router.get("/api/admin/users")
def admin_users(request: Request, _admin=Depends(require_admin)):
    return {"users": _store(request).list_users()}


@router.post("/api/admin/users/{user_id}/role")
def admin_set_role(
    user_id: int, role: str, request: Request, _admin=Depends(require_admin)
):
    if role not in ("user", "organizer", "admin"):
        raise HTTPException(400, "非法角色")
    u = _store(request).update_user(user_id, role=role)
    return {"user": u}


class AdminUserPatch(BaseModel):
    is_active: bool | None = None
    email_verified: bool | None = None
    role: str | None = None


@router.patch("/api/admin/users/{user_id}")
def admin_patch_user(
    user_id: int, body: AdminUserPatch, request: Request, _admin=Depends(require_admin)
):
    fields: dict[str, Any] = {}
    if body.is_active is not None:
        fields["is_active"] = 1 if body.is_active else 0
    if body.email_verified is not None:
        fields["email_verified"] = 1 if body.email_verified else 0
    if body.role is not None:
        if body.role not in ("user", "organizer", "admin"):
            raise HTTPException(400, "非法角色")
        fields["role"] = body.role
    if not fields:
        raise HTTPException(400, "无更新字段")
    u = _store(request).update_user(user_id, **fields)
    if not u:
        raise HTTPException(404, "用户不存在")
    return {"user": u}


@router.delete("/api/admin/users/{user_id}")
def admin_delete_user(user_id: int, request: Request, admin=Depends(require_admin)):
    if admin["id"] == user_id:
        raise HTTPException(400, "不能删除自己")
    if not _store(request).delete_user(user_id):
        raise HTTPException(404, "用户不存在")
    return {"ok": True}


@router.get("/api/admin/users/{user_id}/sessions")
def admin_user_sessions(
    user_id: int, request: Request, _admin=Depends(require_admin)
):
    return {"sessions": _store(request).list_sessions(user_id)}


@router.delete("/api/admin/users/{user_id}/sessions")
def admin_revoke_sessions(
    user_id: int, request: Request, _admin=Depends(require_admin)
):
    n = _store(request).delete_sessions_for_user(user_id)
    return {"ok": True, "revoked": n}


# ── admin: matches ─────────────────────────────────────────────

class AdminMatchPatch(BaseModel):
    status: str | None = None
    reason: str | None = None


@router.patch("/api/admin/matches/{match_id}")
def admin_patch_match(
    match_id: str, body: AdminMatchPatch, request: Request, _admin=Depends(require_admin)
):
    """管理员强制修正对局状态（如中止卡住的对局）。"""
    fields: dict[str, Any] = {}
    if body.status is not None:
        if body.status not in ("pending", "running", "completed", "aborted"):
            raise HTTPException(400, "非法对局状态")
        fields["status"] = body.status
    if body.reason is not None:
        fields["reason"] = body.reason
    if not fields:
        raise HTTPException(400, "无更新字段")
    m = _store(request).update_match(match_id, **fields)
    if not m:
        raise HTTPException(404, "对局不存在")
    return {"match": m}


# ── admin: bots ───────────────────────────────────────────────

@router.get("/api/admin/bots")
def admin_bots(
    request: Request,
    active: bool | None = None,
    q: str | None = None,
    _admin=Depends(require_admin),
):
    rows = _store(request).list_bots(active_only=bool(active) if active is not None else False)
    if q:
        ql = q.lower()
        rows = [b for b in rows if ql in (b.get("name") or "").lower()
                or ql in (b.get("display_name") or "").lower()
                or ql in str(b.get("owner_id"))]
    return {"bots": rows}


@router.patch("/api/admin/bots/{bot_id}")
def admin_patch_bot(
    bot_id: int, body: dict, request: Request, _admin=Depends(require_admin)
):
    allowed = {"is_active", "is_public", "is_builtin", "display_name", "description"}
    fields = {k: (1 if v is True else 0 if v is False else v)
              for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "无可更新字段")
    b = _store(request).update_bot(bot_id, **fields)
    if not b:
        raise HTTPException(404, "bot 不存在")
    return {"bot": b}


@router.delete("/api/admin/bots/{bot_id}")
def admin_delete_bot(bot_id: int, request: Request, _admin=Depends(require_admin)):
    if not _store(request).delete_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    return {"ok": True}


@router.get("/api/admin/bots/{bot_id}/versions")
def admin_bot_versions(
    bot_id: int, request: Request, _admin=Depends(require_admin)
):
    return {"versions": _store(request).list_bot_versions(bot_id)}


# ── admin: stats / dashboard ──────────────────────────────────

@router.get("/api/admin/stats")
def admin_stats(request: Request, _admin=Depends(require_admin)):
    return _store(request).count_stats()


# ── admin: contests ───────────────────────────────────────────

class AdminContestPatch(BaseModel):
    status: str | None = None
    title: str | None = None
    hands_per_match: int | None = None


@router.get("/api/admin/contests")
def admin_contests(
    request: Request, status: str | None = None, _admin=Depends(require_admin)
):
    return {"contests": _store(request).list_contests(status=status)}


@router.patch("/api/admin/contests/{contest_id}")
async def admin_patch_contest(
    contest_id: int, body: AdminContestPatch, request: Request, _admin=Depends(require_admin)
):
    fields: dict[str, Any] = {}
    if body.status is not None:
        # open → running 必须走真正 start，禁止静默改状态
        if body.status == "running":
            c0 = _store(request).get_contest(contest_id)
            if not c0:
                raise HTTPException(404, "比赛不存在")
            if c0["status"] in ("open", "draft"):
                try:
                    contest = await _contests(request).start(contest_id)
                except ValueError as e:
                    raise HTTPException(400, str(e))
                return {"contest": contest}
        if body.status not in (
            "draft", "open", "running", "rest", "finished", "cancelled"
        ):
            raise HTTPException(400, "非法比赛状态")
        fields["status"] = body.status
    if body.title is not None:
        fields["title"] = body.title
    if body.hands_per_match is not None:
        fields["hands_per_match"] = body.hands_per_match
    if not fields:
        raise HTTPException(400, "无更新字段")
    c = _store(request).update_contest(contest_id, **fields)
    if not c:
        raise HTTPException(404, "比赛不存在")
    return {"contest": c}


@router.delete("/api/admin/contests/{contest_id}")
def admin_delete_contest(contest_id: int, request: Request, _admin=Depends(require_admin)):
    if not _store(request).delete_contest(contest_id):
        raise HTTPException(404, "比赛不存在")
    return {"ok": True}


@router.get("/api/admin/contests/{contest_id}/entries")
def admin_contest_entries(
    contest_id: int, request: Request, _admin=Depends(require_admin)
):
    return {"entries": _store(request).list_entries(contest_id)}


@router.delete("/api/admin/contests/{contest_id}/entries/{user_id}")
def admin_delete_entry(
    contest_id: int, user_id: int, request: Request, _admin=Depends(require_admin)
):
    if not _store(request).delete_entry(contest_id, user_id):
        raise HTTPException(404, "报名记录不存在")
    return {"ok": True}


# ── admin: email templates & outbox ───────────────────────────

class TemplateUpdate(BaseModel):
    subject: str
    body_html: str = ""
    body_text: str = ""


@router.get("/api/admin/email/templates")
def admin_templates(request: Request, _admin=Depends(require_admin)):
    return {"templates": _store(request).list_templates()}


@router.get("/api/admin/email/templates/{key}")
def admin_template(key: str, request: Request, _admin=Depends(require_admin)):
    t = _store(request).get_template(key)
    if not t:
        raise HTTPException(404, "模板不存在")
    return {"template": t}


@router.put("/api/admin/email/templates/{key}")
def admin_update_template(
    key: str, body: TemplateUpdate, request: Request, _admin=Depends(require_admin)
):
    t = _store(request).update_template(
        key, subject=body.subject, body_html=body.body_html, body_text=body.body_text
    )
    if not t:
        raise HTTPException(404, "模板不存在")
    return {"template": t}


@router.get("/api/admin/email/outbox")
def admin_outbox(
    request: Request,
    status: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _admin=Depends(require_admin),
):
    rows = _store(request).list_outbox(status=status, limit=limit, offset=offset)
    return {"outbox": rows, "total": len(rows)}


# ── admin: runtime settings ───────────────────────────────────

class RuntimeSettingsPatch(BaseModel):
    model_config = {"extra": "forbid"}

    action_timeout_sec: float | None = None
    max_concurrent_matches: int | None = None
    contest_default_rest_minutes: int | None = None
    bot_cpus: float | None = None
    bot_memory_mb: int | None = None
    # 闲时自动对局
    auto_match_enabled: bool | None = None
    auto_match_interval_sec: int | None = None
    auto_match_min_idle_sec: int | None = None
    auto_match_bot_cooldown: int | None = None
    auto_match_stale_sec: int | None = None
    auto_match_reserve_slots: int | None = None


@router.get("/api/admin/settings/runtime")
def admin_get_runtime(request: Request, _admin=Depends(require_admin)):
    store = _store(request)
    orch = _orch(request)
    ceiling = concurrent_ceiling()
    raw_conc = store.get_setting(SETTING_MAX_CONCURRENT) or str(orch.max_concurrent)
    try:
        admin_conc = int(raw_conc)
    except ValueError:
        admin_conc = orch.max_concurrent
    timeout = store.get_setting(SETTING_ACTION_TIMEOUT) or "60"
    rest = store.get_setting(SETTING_CONTEST_REST) or "10"
    stats = store.count_stats()

    def _sett(key: str, default: str) -> str:
        return store.get_setting(key) or default

    am = {
        "enabled": _sett(SETTING_AUTO_MATCH_ENABLED, "1") in ("1", "true", "yes"),
        "interval_sec": int(_sett(SETTING_AUTO_MATCH_INTERVAL_SEC, "30")),
        "min_idle_sec": int(_sett(SETTING_AUTO_MATCH_MIN_IDLE_SEC, "5")),
        "bot_cooldown": int(_sett(SETTING_AUTO_MATCH_BOT_COOLDOWN, "600")),
        "stale_sec": int(_sett(SETTING_AUTO_MATCH_STALE_SEC, "3600")),
        "reserve_slots": int(_sett(SETTING_AUTO_MATCH_RESERVE_SLOTS, "1")),
    }
    return {
        "cpu_count": cpu_count(),
        "ceiling": ceiling,
        "action_timeout_sec": float(timeout),
        "max_concurrent_matches": clamp_concurrent(admin_conc),
        "admin_requested": admin_conc,
        "effective_concurrent": orch.max_concurrent,
        "bot_cpus": BOT_CPUS,
        "bot_memory_mb": BOT_MEMORY_MB,
        "contest_default_rest_minutes": int(rest),
        "queue": {
            "pending": stats.get("matches_pending", 0),
            "running": stats.get("matches_running", 0),
        },
        "auto_match": am,
        "readonly": ["bot_cpus", "bot_memory_mb"],
    }


@router.patch("/api/admin/settings/runtime")
def admin_patch_runtime(
    body: RuntimeSettingsPatch, request: Request, _admin=Depends(require_admin)
):
    store = _store(request)
    orch = _orch(request)
    ceiling = concurrent_ceiling()
    updated: dict[str, Any] = {}

    if body.bot_cpus is not None or body.bot_memory_mb is not None:
        raise HTTPException(400, "bot_cpus / bot_memory_mb 为只读硬限制，不可修改")

    if body.max_concurrent_matches is not None:
        req = int(body.max_concurrent_matches)
        if req > ceiling:
            raise HTTPException(
                400,
                f"max_concurrent_matches={req} 超过半负载硬顶 ceiling={ceiling}"
                f"（机器 {cpu_count()} 核）",
            )
        if req < 1:
            raise HTTPException(400, "max_concurrent_matches 至少为 1")
        store.set_setting(SETTING_MAX_CONCURRENT, str(req))
        orch.rebuild_concurrency(req)
        updated["max_concurrent_matches"] = req

    if body.action_timeout_sec is not None:
        t = float(body.action_timeout_sec)
        if t < ACTION_TIMEOUT_MIN or t > ACTION_TIMEOUT_MAX:
            raise HTTPException(
                400,
                f"action_timeout_sec 须在 {ACTION_TIMEOUT_MIN}–{ACTION_TIMEOUT_MAX}",
            )
        store.set_setting(SETTING_ACTION_TIMEOUT, str(t))
        orch.set_action_timeout(t)
        updated["action_timeout_sec"] = t

    if body.contest_default_rest_minutes is not None:
        m = int(body.contest_default_rest_minutes)
        if m < 0 or m > 120:
            raise HTTPException(400, "contest_default_rest_minutes 须在 0–120")
        store.set_setting(SETTING_CONTEST_REST, str(m))
        updated["contest_default_rest_minutes"] = m

    # 闲时自动对局（写 settings 即热更新：调度器每轮重读）
    if body.auto_match_enabled is not None:
        store.set_setting(SETTING_AUTO_MATCH_ENABLED, "1" if body.auto_match_enabled else "0")
        updated["auto_match_enabled"] = body.auto_match_enabled
    if body.auto_match_interval_sec is not None:
        v = int(body.auto_match_interval_sec)
        if v < 1 or v > 3600:
            raise HTTPException(400, "auto_match_interval_sec 须在 1–3600")
        store.set_setting(SETTING_AUTO_MATCH_INTERVAL_SEC, str(v))
        updated["auto_match_interval_sec"] = v
    if body.auto_match_min_idle_sec is not None:
        v = int(body.auto_match_min_idle_sec)
        if v < 0 or v > 600:
            raise HTTPException(400, "auto_match_min_idle_sec 须在 0–600")
        store.set_setting(SETTING_AUTO_MATCH_MIN_IDLE_SEC, str(v))
        updated["auto_match_min_idle_sec"] = v
    if body.auto_match_bot_cooldown is not None:
        v = int(body.auto_match_bot_cooldown)
        if v < 0 or v > 86400:
            raise HTTPException(400, "auto_match_bot_cooldown 须在 0–86400")
        store.set_setting(SETTING_AUTO_MATCH_BOT_COOLDOWN, str(v))
        updated["auto_match_bot_cooldown"] = v
    if body.auto_match_stale_sec is not None:
        v = int(body.auto_match_stale_sec)
        if v < 60 or v > 604800:
            raise HTTPException(400, "auto_match_stale_sec 须在 60–604800")
        store.set_setting(SETTING_AUTO_MATCH_STALE_SEC, str(v))
        updated["auto_match_stale_sec"] = v
    if body.auto_match_reserve_slots is not None:
        v = int(body.auto_match_reserve_slots)
        if v < 0 or v > ceiling:
            raise HTTPException(400, "auto_match_reserve_slots 须在 0–ceiling")
        store.set_setting(SETTING_AUTO_MATCH_RESERVE_SLOTS, str(v))
        updated["auto_match_reserve_slots"] = v

    if not updated:
        raise HTTPException(400, "无更新字段")
    return {"updated": updated, "runtime": admin_get_runtime(request, _admin)}


# ── admin: 裁判引擎（规则参数热调 + 代码只读） ────────────────────
# 裁判规则参数存 platform_settings，orchestrator 每局热读，下局即生效。
JUDGE_PARAM_BOUNDS: dict[str, tuple[int, int]] = {
    SETTING_JUDGE_GOMOKU_SIZE: (9, 19),
    SETTING_JUDGE_HOLDEM_STACK: (1000, 1_000_000),
    SETTING_JUDGE_HOLDEM_SB: (1, 10_000),
    SETTING_JUDGE_HOLDEM_BB: (2, 20_000),
    SETTING_JUDGE_HOLDEM_HANDS: (1, 1000),
}
# 裁判参数默认值（与各引擎常量对齐）
JUDGE_PARAM_DEFAULTS: dict[str, int] = {
    SETTING_JUDGE_GOMOKU_SIZE: 15,
    SETTING_JUDGE_HOLDEM_STACK: 20000,
    SETTING_JUDGE_HOLDEM_SB: 50,
    SETTING_JUDGE_HOLDEM_BB: 100,
    SETTING_JUDGE_HOLDEM_HANDS: 70,
}
# 三款游戏裁判元信息（代码位置 / 规则摘要 / 可调参数 key）
JUDGE_GAMES: list[dict[str, Any]] = [
    {
        "game_id": "holdem",
        "label": "德州扑克",
        "code_path": "bzplat/backend/engine/game.py",
        "summary": "HU NLHE；单局多手；按筹码差判胜。",
        "params": [
            {"key": SETTING_JUDGE_HOLDEM_STACK, "label": "起始筹码", "field": "starting_stack"},
            {"key": SETTING_JUDGE_HOLDEM_SB, "label": "小盲注", "field": "sb"},
            {"key": SETTING_JUDGE_HOLDEM_BB, "label": "大盲注", "field": "bb"},
            {"key": SETTING_JUDGE_HOLDEM_HANDS, "label": "挑战默认手数", "field": "default_hands"},
        ],
    },
    {
        "game_id": "gomoku",
        "label": "五子棋",
        "code_path": "bzplat/backend/engine/gomoku.py",
        "summary": "15×15；黑先；横竖斜连续≥5 即胜；无禁手。",
        "params": [
            {"key": SETTING_JUDGE_GOMOKU_SIZE, "label": "棋盘边长", "field": "board_size"},
        ],
    },
    {
        "game_id": "pencil",
        "label": "点格棋",
        "code_path": "bzplat/backend/engine/pencil.py",
        "summary": "N=11 点阵；红先；占相邻边围格得分并连走；格多者胜。",
        "params": [],  # n_dots 走 match 列，非全局 setting
    },
]


def _engine_docstring(rel_path: str) -> str:
    """读引擎源码首段 docstring（只读展示）。rel_path 形如 bzplat/backend/engine/gomoku.py。"""
    try:
        # api_routes.py 在 bzplat/backend/ 下；剥离前缀后相对该目录定位
        backend_dir = Path(__file__).resolve().parent
        rel = rel_path.replace("bzplat/backend/", "", 1)
        text = (backend_dir / rel).read_text(encoding="utf-8")
    except OSError:
        return ""
    if not text.startswith('"""'):
        return ""
    end = text.find('"""', 3)
    return text[3:end].strip() if end > 0 else ""


@router.get("/api/admin/judges")
def admin_get_judges(request: Request, _admin=Depends(require_admin)):
    store = _store(request)
    games = []
    for g in JUDGE_GAMES:
        params = []
        for prm in g["params"]:
            key = prm["key"]
            raw = store.get_setting(key)
            try:
                value = int(raw) if raw not in (None, "") else JUDGE_PARAM_DEFAULTS[key]
            except (TypeError, ValueError):
                value = JUDGE_PARAM_DEFAULTS[key]
            lo, hi = JUDGE_PARAM_BOUNDS[key]
            params.append({**prm, "value": value, "min": lo, "max": hi})
        games.append({
            "game_id": g["game_id"],
            "label": g["label"],
            "code_path": g["code_path"],
            "summary": g["summary"],
            "params": params,
            "docstring": _engine_docstring(g["code_path"]),
        })
    # admin-only 裁判代码说明（不入公开 WIKI_PAGES）
    judge_code_path = _wiki_dir() / "JUDGE_CODE.md"
    markdown = (
        judge_code_path.read_text(encoding="utf-8") if judge_code_path.is_file() else ""
    )
    return {"games": games, "markdown": markdown}


class JudgeParamsPatch(BaseModel):
    model_config = {"extra": "forbid"}

    params: dict[str, int]


@router.patch("/api/admin/judges/params")
def admin_patch_judge_params(
    body: JudgeParamsPatch, request: Request, _admin=Depends(require_admin)
):
    store = _store(request)
    if not body.params:
        raise HTTPException(400, "无更新字段")
    updated: dict[str, Any] = {}
    for key, value in body.params.items():
        if key not in JUDGE_PARAM_BOUNDS:
            raise HTTPException(400, f"未知裁判参数: {key}")
        lo, hi = JUDGE_PARAM_BOUNDS[key]
        v = int(value)
        if v < lo or v > hi:
            raise HTTPException(400, f"{key} 须在 {lo}–{hi}")
        updated[key] = v
    # bb > sb 一致性校验（若两者有任一被改，需综合校验）
    def _cur(k: str) -> int:
        return int(updated.get(k, store.get_setting(k)) or JUDGE_PARAM_DEFAULTS[k])
    sb = _cur(SETTING_JUDGE_HOLDEM_SB)
    bb = _cur(SETTING_JUDGE_HOLDEM_BB)
    if bb <= sb:
        raise HTTPException(400, f"大盲({bb})必须大于小盲({sb})")
    for key, v in updated.items():
        store.set_setting(key, str(v))
    # 返回最新裁判总览
    return {"updated": updated, "judges": admin_get_judges(request, _admin)}


# ── admin: 赛制模板 CRUD ──────────────────────────────────────
class TemplateBody(BaseModel):
    id: str
    name: str
    game_id: str
    match_config: dict[str, Any] = {}
    stages: list[dict[str, Any]]


class TemplatePreviewBody(BaseModel):
    stages: list[dict[str, Any]]
    n: int = 8


@router.get("/api/admin/templates")
def admin_list_templates(
    request: Request, game: str | None = None, _admin=Depends(require_admin)
):
    return {"templates": _store(request).list_contest_templates(game_id=game)}


@router.post("/api/admin/templates")
def admin_create_template(
    body: TemplateBody, request: Request, _admin=Depends(require_admin)
):
    store = _store(request)
    if store.get_contest_template(body.id) is not None:
        raise HTTPException(409, f"模板 id 已存在：{body.id}")
    try:
        norm = validate_template(body.id, body.name, body.game_id, body.match_config, body.stages)
    except ValueError as e:
        raise HTTPException(400, str(e))
    t = store.upsert_contest_template(
        norm["id"], name=norm["name"], game_id=norm["game_id"],
        match_config=norm["match_config"], stages=norm["stages"], is_builtin=False,
    )
    return {"template": t}


@router.put("/api/admin/templates/{tid}")
def admin_update_template(
    tid: str, body: TemplateBody, request: Request, _admin=Depends(require_admin)
):
    if tid != body.id:
        raise HTTPException(400, "路径 id 与 body.id 不一致")
    store = _store(request)
    existing = store.get_contest_template(tid)
    if existing is None:
        raise HTTPException(404, f"模板不存在：{tid}")
    try:
        norm = validate_template(body.id, body.name, body.game_id, body.match_config, body.stages)
    except ValueError as e:
        raise HTTPException(400, str(e))
    t = store.upsert_contest_template(
        norm["id"], name=norm["name"], game_id=norm["game_id"],
        match_config=norm["match_config"], stages=norm["stages"],
        is_builtin=bool(existing.get("is_builtin")),
    )
    return {"template": t}


@router.delete("/api/admin/templates/{tid}")
def admin_delete_template(tid: str, request: Request, _admin=Depends(require_admin)):
    store = _store(request)
    existing = store.get_contest_template(tid)
    if existing is None:
        raise HTTPException(404, f"模板不存在：{tid}")
    if existing.get("is_builtin"):
        raise HTTPException(400, "内置模板不可删除")
    if not store.delete_contest_template(tid):
        raise HTTPException(400, "删除失败")
    return {"ok": True}


@router.post("/api/admin/templates/preview")
def admin_preview_template(
    body: TemplatePreviewBody, request: Request, _admin=Depends(require_admin)
):
    """dry-run：给定 stages + 人数 n → 各阶段 / 总场数预估。"""
    try:
        norm_stages = [validate_stage(s, i) for i, s in enumerate(body.stages)]
    except ValueError as e:
        raise HTTPException(400, str(e))
    n = max(0, int(body.n))
    per = [estimate_match_count(st, n) for st in norm_stages]
    return {"per_stage": per, "total": sum(per), "n": n}


# ── admin: 日志查看 ────────────────────────────────────────────
@router.get("/api/admin/logs")
def admin_logs(
    request: Request,
    level: str | None = None,
    q: str | None = None,
    limit: int = 300,
    _admin=Depends(require_admin),
):
    """读 logs/app.log 末尾 N 行，按级别/关键字过滤。"""
    log_path = Path(os.environ.get("BZ_LOG_DIR", "logs")) / "app.log"
    lines: list[str] = []
    if log_path.is_file():
        # 读末尾（最多 ~8000 行，取后 limit 行）
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-8000:]
        level_upper = level.upper() if level else None
        kw = (q or "").lower()
        for ln in tail:
            if level_upper and f" {level_upper} " not in f" {ln} ":
                continue
            if kw and kw not in ln.lower():
                continue
            lines.append(ln.rstrip("\n"))
        lines = lines[-limit:]
    return {"lines": lines, "path": str(log_path)}


# ── wiki ──────────────────────────────────────────────────────
# 站内 Wiki：多页索引 + 按 slug 取正文。wiki/ 目录下每个 .md 一页，
# slug 为文件名（去 .md）。索引按固定顺序排列，缺失文件自动跳过。
WIKI_PAGES: list[dict[str, str]] = [
    {"slug": "index", "file": "INDEX.md", "title": "Wiki 首页", "summary": "站内文档导航与 Botzone 差异总览"},
    {"slug": "protocol", "file": "PROTOCOL.md", "title": "协议规范", "summary": "紧凑 JSON 对局协议、字段、卡牌编码、规则"},
    {"slug": "bot-dev", "file": "BOT_DEV.md", "title": "Bot 开发指南", "summary": "从零编写一个 Bot：样例、编译、上传、调试"},
    {"slug": "runtime", "file": "RUNTIME.md", "title": "运行时与资源限制", "summary": "Docker CPU/内存、超时、半负载并发与 Botzone 差异"},
    {"slug": "gomoku", "file": "GOMOKU.md", "title": "五子棋 (Gomoku)", "summary": "15×15 规则、协议、样例与本平台对照"},
    {"slug": "gomoku-swap1", "file": "GOMOKU_SWAP1.md", "title": "一手交换五子棋", "summary": "Gomoku-Swap1 简介（规则正文待补）"},
    {"slug": "pencil", "file": "PENCIL.md", "title": "点格棋 (Pencil)", "summary": "N=11 规则、交错网格、pass 连走与协议"},
    {"slug": "texas", "file": "TEXAS.md", "title": "德州扑克 (TexasHoldem2p)", "summary": "Botzone 规则摘要与本平台协议对照"},
    {"slug": "judge", "file": "JUDGE.md", "title": "裁判", "summary": "Botzone 裁判概念与本平台引擎对照"},
    {"slug": "match", "file": "MATCH.md", "title": "对局", "summary": "对局生命周期、错误码与观赛"},
    {"slug": "contest-format", "file": "CONTEST_FORMAT.md", "title": "赛制模板", "summary": "赛制模板结构、阶段类型、match_config 与管理员配置"},
]


def _wiki_dir() -> Path:
    # api_routes.py → backend → bzplat → <project_root>
    return Path(__file__).resolve().parents[2] / "wiki"


@router.get("/api/wiki")
def wiki_content(slug: str | None = None):
    wiki = _wiki_dir()
    pages = []
    for p in WIKI_PAGES:
        if (wiki / p["file"]).is_file():
            pages.append({"slug": p["slug"], "title": p["title"], "summary": p["summary"]})

    if slug is None:
        # 无 slug：返回索引（首页直接展示默认页正文，减少一次请求）
        slug = pages[0]["slug"] if pages else ""

    target = next((p for p in WIKI_PAGES if p["slug"] == slug), None)
    path = wiki / target["file"] if target else None
    markdown = path.read_text(encoding="utf-8") if path and path.is_file() else "# Wiki\n\n暂无内容。"
    return {
        "slug": slug,
        "title": target["title"] if target else "Wiki",
        "markdown": markdown,
        "pages": pages,
    }
