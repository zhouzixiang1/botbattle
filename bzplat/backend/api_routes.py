"""Bots / Matches / Contests / Admin / Leaderboard API 路由。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from bzplat.backend.auth.dependencies import (
    require_admin,
    require_organizer,
    require_user,
)
from bzplat.backend.bots import BotError, BotManager
from bzplat.backend.contests import ContestManager
from bzplat.backend.matches import MatchOrchestrator

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
def my_bots(request: Request, user=Depends(require_user)):
    return {"bots": _bots(request).list_mine(user["id"])}


@router.get("/api/bots/public")
def public_bots(request: Request):
    return {"bots": _bots(request).list_public()}


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
    file: UploadFile = File(...),
    user=Depends(require_user),
):
    raw = await file.read()
    try:
        bot = _bots(request).create_from_upload(
            user["id"], name, raw,
            display_name=display_name, description=description,
            upload_note=upload_note, is_public=is_public,
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


@router.post("/api/matches/challenge")
async def challenge(body: ChallengeBody, request: Request, user=Depends(require_user)):
    try:
        mid = await _orch(request).challenge(
            body.my_bot_id, body.opponent_bot_id, user["id"], hands=body.hands
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"match_id": mid, "status": "pending"}


@router.get("/api/matches")
def list_matches(
    request: Request,
    status: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    rows = _store(request).list_matches(status=status, limit=limit, offset=offset)
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
def leaderboard(request: Request, limit: int = 50):
    return {"leaderboard": _store(request).list_leaderboard(limit=limit)}


# ── contests ──────────────────────────────────────────────────

class ContestCreate(BaseModel):
    title: str
    description: str = ""
    hands_per_match: int = 70


class ContestRegister(BaseModel):
    bot_id: int


@router.get("/api/contests")
def list_contests(request: Request, status: str | None = None):
    return {"contests": _store(request).list_contests(status=status)}


@router.post("/api/contests")
def create_contest(body: ContestCreate, request: Request, user=Depends(require_organizer)):
    c = _contests(request).create(
        user["id"], body.title,
        description=body.description, hands_per_match=body.hands_per_match,
    )
    return {"contest": c}


@router.get("/api/contests/{contest_id}")
def contest_detail(contest_id: int, request: Request):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404, "比赛不存在")
    entries = _store(request).list_contest_entries(contest_id)
    pairings = _store(request).list_contest_pairings(contest_id)
    standings = _contests(request).standings(contest_id)
    return {"contest": c, "entries": entries, "pairings": pairings, "standings": standings}


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
def admin_patch_contest(
    contest_id: int, body: AdminContestPatch, request: Request, _admin=Depends(require_admin)
):
    fields: dict[str, Any] = {}
    if body.status is not None:
        if body.status not in ("draft", "open", "running", "finished", "cancelled"):
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


# ── wiki ──────────────────────────────────────────────────────
# 站内 Wiki：多页索引 + 按 slug 取正文。wiki/ 目录下每个 .md 一页，
# slug 为文件名（去 .md）。索引按固定顺序排列，缺失文件自动跳过。
WIKI_PAGES: list[dict[str, str]] = [
    {"slug": "protocol", "file": "PROTOCOL.md", "title": "协议规范", "summary": "紧凑 JSON 对局协议、字段、卡牌编码、规则"},
    {"slug": "bot-dev", "file": "BOT_DEV.md", "title": "Bot 开发指南", "summary": "从零编写一个 Bot：样例、编译、上传、调试"},
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
