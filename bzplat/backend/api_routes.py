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
    _extract_token,
    optional_user,
    require_admin,
    require_organizer,
    require_user,
)
from bzplat.backend.security import audit_log
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
    DEFAULT_RUNTIME_MODE,
    SETTING_ACTION_TIMEOUT,
    SETTING_AUTO_MATCH_BOT_COOLDOWN,
    SETTING_AUTO_MATCH_DAILY_CAP,
    SETTING_AUTO_MATCH_ENABLED,
    SETTING_AUTO_MATCH_INTERVAL_SEC,
    SETTING_AUTO_MATCH_MAX_PER_ROUND,
    SETTING_AUTO_MATCH_MIN_IDLE_SEC,
    SETTING_AUTO_MATCH_PLACEMENT_GAMES,
    SETTING_AUTO_MATCH_RESERVE_SLOTS,
    SETTING_AUTO_MATCH_STALE_SEC,
    SETTING_CONTEST_REST,
    SETTING_JUDGE_HOLDEM_BB,
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


def _with_seat_info(m: dict, store=None) -> dict:
    """观赛座位身份：委托 matches.seat_info（人类座改写真人用户名）。"""
    from bzplat.backend.matches.seat_info import with_seat_info

    human_user = None
    if store is not None and m and m.get("match_type") == "human" and m.get("human_user_id") is not None:
        try:
            human_user = store.get_user(int(m["human_user_id"]))
        except Exception:
            human_user = None
    return with_seat_info(m, human_user=human_user) or m


# ── bots ──────────────────────────────────────────────────────

@router.get("/api/bots/mine")
def my_bots(
    request: Request,
    user=Depends(require_user),
    game_id: str | None = None,
    page: int | None = None,
    per_page: int = 50,
):
    result = _bots(request).list_mine(
        user["id"], game_id=game_id, page=page, per_page=per_page
    )
    if isinstance(result, dict):
        return {"bots": result["items"], "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"bots": result}


@router.get("/api/bots/public")
def public_bots(
    request: Request, game_id: str | None = None, owner_id: int | None = None,
    page: int | None = None, per_page: int = 50,
    user=Depends(optional_user),
):
    result = _bots(request).list_public(
        game_id=game_id, owner_id=owner_id, page=page, per_page=per_page
    )
    bots = result["items"] if isinstance(result, dict) else result
    # 脱敏敏感字段（binary_path/runtime_mode）——非 owner/admin 不可见（审计 P1-B）
    bots = [_sanitize_bot(b, user) for b in bots]
    # 附带 owner_name/owner_display（供对手选择弹窗展示）
    store = _store(request)
    owner_ids = {b["owner_id"] for b in bots if b.get("owner_id") is not None}
    owner_map: dict[int, dict] = {}
    if owner_ids:
        for oid in owner_ids:
            ou = store.get_user(oid)
            if ou:
                owner_map[oid] = ou
    for b in bots:
        ou = owner_map.get(b.get("owner_id"))
        if ou:
            b["owner_name"] = ou.get("username")
            b["owner_display"] = ou.get("display_name")
    if isinstance(result, dict):
        return {"bots": bots, "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"bots": bots}


@router.get("/api/users")
def search_users(request: Request, q: str | None = None, limit: int = 20):
    """按用户名前缀搜索（公开，仅返回 id/username/display_name）。"""
    return {"users": _store(request).search_users(q or "", limit=max(1, min(limit, 50)))}


@router.get("/api/users/{username}/profile")
def user_profile(username: str, request: Request):
    """用户主页聚合：公开信息 + 总战绩（公开）。"""
    p = _store(request).user_profile(username)
    if not p:
        raise HTTPException(404, "用户不存在")
    return {"profile": p}


@router.get("/api/users/{user_id}/followers")
def user_followers(user_id: int, request: Request, limit: int = 50):
    return {"followers": _store(request).list_followers(user_id, limit=limit)}


@router.get("/api/users/{user_id}/following")
def user_following(user_id: int, request: Request, limit: int = 50):
    return {"following": _store(request).list_following(user_id, limit=limit)}


@router.post("/api/users/{user_id}/follow")
def follow_user(user_id: int, request: Request, user=Depends(require_user)):
    store = _store(request)
    target = store.get_user(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if user["id"] == user_id:
        raise HTTPException(400, "不能关注自己")
    created = store.follow(user["id"], user_id)
    # 关注时通知被关注者 + 被关注者经验
    notifier = getattr(request.app.state, "notifier", None)
    if created:
        from bzplat.backend.store.schema import XP_FOLLOWED
        store.award_xp(user_id, XP_FOLLOWED)
        if notifier is not None:
            try:
                me = store.get_user(user["id"])
                notifier.notify(
                    user_id, type="followed",
                    title=f"{me.get('display_name') or me.get('username')} 关注了你",
                    body="",
                    link=f"/user/{me.get('username')}",
                )
            except Exception:
                pass
    return {"ok": True, "following": True, "created": created}


@router.delete("/api/users/{user_id}/follow")
def unfollow_user(user_id: int, request: Request, user=Depends(require_user)):
    _store(request).unfollow(user["id"], user_id)
    return {"ok": True, "following": False}


@router.get("/api/users/{user_id}/follow-status")
def follow_status(user_id: int, request: Request, user=Depends(require_user)):
    store = _store(request)
    return {
        "following": store.is_following(user["id"], user_id),
        "follower_count": store.follower_count(user_id),
        "following_count": store.following_count(user_id),
    }


@router.post("/api/bots/{bot_id}/favorite")
def favorite_bot(bot_id: int, request: Request, user=Depends(require_user)):
    if not _store(request).get_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    created = _store(request).favorite(user["id"], bot_id)
    return {"ok": True, "favorited": True, "created": created}


@router.delete("/api/bots/{bot_id}/favorite")
def unfavorite_bot(bot_id: int, request: Request, user=Depends(require_user)):
    _store(request).unfavorite(user["id"], bot_id)
    return {"ok": True, "favorited": False}


@router.get("/api/bots/{bot_id}/favorite-status")
def favorite_status(bot_id: int, request: Request, user=Depends(require_user)):
    store = _store(request)
    return {
        "favorited": store.is_favorite(user["id"], bot_id),
        "favorite_count": store.favorite_count(bot_id),
    }


@router.get("/api/auth/me/favorites")
def my_favorites(request: Request, limit: int = 50, user=Depends(require_user)):
    return {"favorites": _store(request).list_favorites(user["id"], limit=limit)}


@router.get("/api/users/{username}/bots")
def user_bots(
    username: str, request: Request, page: int | None = None, per_page: int = 50,
    user=Depends(optional_user),
):
    """某用户的公开 Bot 列表（公开）。"""
    store = _store(request)
    u = store.get_user_by_username(username)
    if not u:
        raise HTTPException(404, "用户不存在")
    result = store.list_bots(owner_id=u["id"], page=page, per_page=per_page)
    if isinstance(result, dict):
        # 脱敏敏感字段（审计 P1-B）
        items = [_sanitize_bot(b, user) for b in result["items"]]
        return {"bots": items, "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"bots": [_sanitize_bot(b, user) for b in result]}


@router.get("/api/search")
def global_search(
    request: Request,
    q: str | None = None,
    type: str | None = None,
    limit: int = 20,
    game_id: str | None = None,
):
    """全局搜索：type=users|bots|matches（默认 users）。

    bots 按 name/display_name 模糊；matches 按 bot 名模糊；users 沿用前缀搜索。
    game_id 可选过滤（仅对 bots/matches 有效）。
    """
    store = _store(request)
    ql = (q or "").strip()
    lim = max(1, min(limit, 50))
    t = (type or "users").lower()
    if t == "bots":
        return {"bots": store.search_bots(ql, limit=lim, game_id=game_id)}
    if t == "matches":
        return {"matches": store.search_matches(ql, limit=lim, game_id=game_id)}
    # 默认 users
    return {"users": store.search_users(ql, limit=lim)}


# 公开 bot 详情需脱敏的敏感字段（非 owner/admin 不可见）。
# 与 /api/bots/{id}/versions 的脱敏口径一致：binary_path 暴露磁盘布局，
# runtime_mode 是内部运行配置，均不应泄漏给访客（审计 P1-B）。
_BOT_SENSITIVE_FIELDS = ("binary_path", "runtime_mode")


def _sanitize_bot(bot: dict, user: dict | None) -> dict:
    """非 owner/admin 访问时脱敏 bot 字段（返回副本，不改原 dict）。"""
    if user is not None and (bot.get("owner_id") == user.get("id") or user.get("role") == "admin"):
        return bot
    return {k: v for k, v in bot.items() if k not in _BOT_SENSITIVE_FIELDS}


@router.get("/api/bots/{bot_id}")
def get_bot(bot_id: int, request: Request, user=Depends(optional_user)):
    bot = _bots(request).get(bot_id)
    if not bot:
        raise HTTPException(404, "bot 不存在")
    return {"bot": _sanitize_bot(bot, user)}


@router.get("/api/bots/{bot_id}/profile")
def bot_profile(bot_id: int, request: Request, user=Depends(optional_user)):
    """Bot 详情聚合：bot 信息 + owner + rating + 胜率（公开）。"""
    store = _store(request)
    p = store.bot_profile(bot_id)
    if not p:
        raise HTTPException(404, "bot 不存在")
    # 脱敏敏感字段（审计 P1-B）——profile 是聚合 dict，按 owner/admin 判断后 pop
    is_privileged = user is not None and (
        p.get("owner_id") == user.get("id") or user.get("role") == "admin"
    )
    if not is_privileged:
        p = {k: v for k, v in p.items() if k not in _BOT_SENSITIVE_FIELDS}
    return {"profile": p}


@router.get("/api/bots/{bot_id}/matches")
def bot_matches(
    bot_id: int, request: Request, limit: int = 30, offset: int = 0,
    page: int | None = None, per_page: int = 50,
):
    """某 Bot 的对局历史（公开，复用 list_matches(bot_id=)）。

    旘认 ``limit``/``offset`` 仍生效（向后兼容）；提供 ``page`` 时改返回
    ``{matches, page, per_page, total}`` 分页契约（limit/offset 在该模式下忽略）。
    """
    store = _store(request)
    if not store.get_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    if page is not None:
        pp = max(1, min(200, per_page))
        off = (max(1, page) - 1) * pp
        rows = store.list_matches(limit=pp, offset=off, bot_id=bot_id)
        total = store.count_bot_matches(bot_id)
        return {"matches": rows, "page": max(1, page), "per_page": pp, "total": total}
    rows = store.list_matches(
        bot_id=bot_id, limit=max(1, min(limit, 100)), offset=max(0, offset)
    )
    return {"matches": rows}


@router.get("/api/bots/{bot_id}/opponents")
def bot_opponents(
    bot_id: int, request: Request, limit: int = 20
):
    """某 Bot 对各对手的战绩（公开，从 pair_stats 读）。"""
    if not _store(request).get_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    return {"opponents": _store(request).bot_opponents_stats(bot_id, limit=max(1, min(limit, 200)))}


@router.get("/api/bots/{bot_id}/rating-history")
def bot_rating_history(
    bot_id: int, request: Request, limit: int = 100
):
    """某 Bot 的评分变化时序（公开，画曲线/趋势用）。"""
    if not _store(request).get_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    return {"history": _store(request).list_rating_history(bot_id, limit=max(1, min(limit, 500)))}


@router.post("/api/bots")
async def upload_bot(
    request: Request,
    name: str = Form(...),
    display_name: str = Form(""),
    description: str = Form(""),
    upload_note: str = Form(""),
    game_id: str = Form("holdem"),
    runtime_mode: str = Form(DEFAULT_RUNTIME_MODE),
    file: UploadFile = File(...),
    user=Depends(require_user),
):
    raw = await file.read()
    try:
        bot = _bots(request).create_from_upload(
            user["id"], name, raw,
            display_name=display_name, description=description,
            upload_note=upload_note,
            game_id=game_id,
            runtime_mode=runtime_mode,
            binary_runner=getattr(request.app.state, "binary_runner", None),
        )
    except BotError as e:
        audit_log(request, "bot_upload", result="fail", user=user.get("username"), target=name, detail=e.code)
        raise HTTPException(400, detail={"code": e.code, "message": e.message})
    audit_log(request, "bot_upload", result="ok", user=user.get("username"), target=name, detail=f"game={game_id} mode={runtime_mode} size={len(raw)}")
    return {"bot": bot}


@router.post("/api/bots/{bot_id}/versions")
async def upload_bot_version(
    bot_id: int,
    request: Request,
    upload_note: str = Form(""),
    runtime_mode: str = Form(""),
    file: UploadFile = File(...),
    user=Depends(require_user),
):
    raw = await file.read()
    try:
        bot = _bots(request).upload_version(
            bot_id, user["id"], raw, upload_note=upload_note,
            runtime_mode=runtime_mode or None,
            binary_runner=getattr(request.app.state, "binary_runner", None),
        )
    except BotError as e:
        audit_log(request, "bot_version_upload", result="fail", user=user.get("username"), target=bot_id, detail=e.code)
        raise HTTPException(400, detail={"code": e.code, "message": e.message})
    audit_log(request, "bot_version_upload", result="ok", user=user.get("username"), target=bot_id, detail=f"size={len(raw)}")
    return {"bot": bot}


@router.get("/api/bots/{bot_id}/versions")
def list_my_bot_versions(bot_id: int, request: Request, user=Depends(require_user)):
    """Bot 版本列表。

    - owner/admin：完整版本信息（含 runtime_mode，回滚时恢复）。
    - 非 owner（公开访客）：仅版本号 + 上传时间 + 备注（不含 binary_path/runtime_mode），
      供挑战页版本选择（选某版本对战）。必须登录（挑战需登录）。
    """
    store = _store(request)
    bot = store.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot 不存在")
    is_owner = bot["owner_id"] == user["id"] or user.get("role") == "admin"
    versions = store.list_bot_versions(bot_id)
    if not is_owner:
        # 公开视图：脱敏（不含 binary_path/runtime_mode 等敏感字段）。
        # 保留 id：挑战页选某版本对战时，my_bot_version_id/opponent_bot_version_id
        # 解析的是 bot_versions.id（主键），非 version 号——故必须回传 id 才能选版本。
        versions = [
            {
                "id": v.get("id"),
                "version": v.get("version"),
                "upload_note": v.get("upload_note") or "",
                "created_at": v.get("created_at") or v.get("uploaded_at") or "",
                "size_bytes": v.get("size_bytes") or 0,
            }
            for v in versions
        ]
    return {"versions": versions, "current_version": bot["current_version"]}


@router.post("/api/bots/{bot_id}/versions/{version}/activate")
def rollback_bot_version(
    bot_id: int, version: int, request: Request, user=Depends(require_user)
):
    """Bot 拥有者回滚到指定版本（MyBots「回滚到此版本」）。

    不删除其他版本，仅切换 current_version + 镜像（binary_path/runtime_mode/...）。
    """
    store = _store(request)
    bot = store.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot 不存在")
    if bot["owner_id"] != user["id"]:
        raise HTTPException(403, "无权修改他人的 Bot")
    result = store.set_current_version(bot_id, version)
    if result is None:
        raise HTTPException(404, f"版本 {version} 不存在")
    audit_log(request, "bot_version_rollback", result="ok", user=user.get("username"), target=bot_id, detail=f"v{version}")
    return {"bot": result}


@router.post("/api/bots/{bot_id}/active")
def set_bot_active(
    bot_id: int, request: Request, active: bool = True, user=Depends(require_user)
):
    try:
        bot = _bots(request).set_active(bot_id, user["id"], active)
    except BotError as e:
        raise HTTPException(400, detail={"code": e.code, "message": e.message})
    return {"bot": bot}


@router.patch("/api/bots/{bot_id}")
def update_my_bot(
    bot_id: int, body: dict, request: Request, user=Depends(require_user)
):
    """Bot 拥有者改 display_name/description/is_active（受限白名单）。"""
    store = _store(request)
    bot = store.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot 不存在")
    if bot["owner_id"] != user["id"]:
        raise HTTPException(403, "无权修改他人的 Bot")
    allowed = {"display_name", "description", "is_active"}
    fields = {
        k: (1 if v is True else 0 if v is False else str(v)[:200] if k in ("display_name",) else str(v)[:2000])
        for k, v in body.items() if k in allowed
    }
    if not fields:
        raise HTTPException(400, "无可更新字段")
    b = store.update_bot(bot_id, **fields)
    return {"bot": b}


@router.delete("/api/bots/{bot_id}")
def delete_my_bot(bot_id: int, request: Request, user=Depends(require_user)):
    """Bot 拥有者删除自己的 Bot（软删：is_active=0）。"""
    store = _store(request)
    bot = store.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot 不存在")
    if bot["owner_id"] != user["id"]:
        raise HTTPException(403, "无权删除他人的 Bot")
    store.update_bot(bot_id, is_active=0)
    return {"ok": True}


# ── matches ───────────────────────────────────────────────────

class ChallengeBody(BaseModel):
    my_bot_id: int
    opponent_bot_id: int
    # 版本快照（可选）：指定各座位跑哪个版本。缺省/None=当前激活版本。
    # 自博弈（my_bot_id == opponent_bot_id）允许——用于同 bot 新旧版本对比。
    my_bot_version_id: int | None = None
    opponent_bot_version_id: int | None = None
    # 对局级配置（如 {"hands":70}/{"n_dots":6}）；缺省/空用 spec.default_match_params。
    # 取代散落的 hands/n_dots 具名字段——第 4 游戏带新参数无需改本 Body。范围校验交
    # spec.validate_match_params（holdem 1-500；棋类忽略 hands）。
    match_config: dict = Field(default_factory=dict)
    game_id: str | None = None


@router.post("/api/matches/challenge")
async def challenge(body: ChallengeBody, request: Request, user=Depends(require_user)):
    try:
        mid = await _orch(request).challenge(
            body.my_bot_id,
            body.opponent_bot_id,
            user["id"],
            match_config=body.match_config,
            game_id=body.game_id,
            bot_a_version_id=body.my_bot_version_id,
            bot_b_version_id=body.opponent_bot_version_id,
        )
    except ValueError as e:
        audit_log(request, "match_challenge", result="fail", user=user.get("username"), detail=str(e))
        raise HTTPException(400, str(e))
    audit_log(request, "match_challenge", result="ok", user=user.get("username"), target=mid, detail=f"bots={body.my_bot_id}vs{body.opponent_bot_id}")
    return {"match_id": mid, "status": "pending"}


class HumanChallengeBody(BaseModel):
    bot_id: int
    human_seat: int = 1  # 0 或 1，人类坐哪位
    game_id: str | None = None
    # 对局级配置（同 ChallengeBody.match_config）；缺省用 spec 默认。
    match_config: dict = Field(default_factory=dict)


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
            match_config=body.match_config,
        )
    except ValueError as e:
        audit_log(request, "match_human", result="fail", user=user.get("username"), detail=str(e))
        raise HTTPException(400, str(e))
    audit_log(request, "match_human", result="ok", user=user.get("username"), target=mid, detail=f"bot={body.bot_id} seat={body.human_seat}")
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
    # 订阅事件流（subscribe 会再推一条带 seats 的 snapshot，此处先发一份便于立即渲染）
    q = orch.subscribe(match_id)
    from bzplat.backend.matches.seat_info import match_for_viewer

    replay = store.get_replay(match_id) or {}
    try:
        await websocket.send_json({
            "type": "snapshot",
            "match": match_for_viewer(store, match_id) or m,
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
    store = _store(request)
    lim = max(1, min(limit, 100))
    off = max(0, offset)
    rows = store.list_matches(
        status=status, game_id=game_id, limit=lim, offset=off
    )
    total = store.count_matches(status=status, game_id=game_id)
    return {"matches": rows, "total": total, "limit": lim, "offset": off}


@router.get("/api/matches/liked-top")
def liked_top_matches(request: Request, limit: int = 10):
    """对局点赞排行榜（对标 Botzone，首页用）。必须在 {match_id} 路由前注册。"""
    store = _store(request)
    return {"matches": store.list_liked_top_matches(limit)}


@router.get("/api/matches/{match_id}")
def match_detail(match_id: str, request: Request):
    store = _store(request)
    m = store.get_match_detailed(match_id)
    if not m:
        raise HTTPException(404, "对局不存在")
    replay = _store(request).get_replay(match_id) or {}
    return {"match": _with_seat_info(m, store=store), "replay": replay}


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
def leaderboard(
    request: Request, limit: int = 50, game_id: str | None = None,
    page: int | None = None, per_page: int = 50,
):
    result = _store(request).list_leaderboard(
        limit=max(1, min(limit, 200)), game_id=game_id, page=page, per_page=per_page,
    )
    if isinstance(result, dict):
        return {"leaderboard": result["items"], "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"leaderboard": result}


@router.get("/api/tiers")
def tiers(game_id: str | None = None):
    """段位定义（公开，前端镜像校验用）。

    段位与游戏挂钩（PR2）：传 game_id 返回该游戏的段位曲线；不传则返回 holdem
    的曲线作为默认（向后兼容旧前端无参调用）。经 games 注册表取 per-game 曲线。
    """
    from bzplat.backend.games import registry as _game_registry
    gid = (game_id or "holdem").strip().lower()
    try:
        return {"tiers": _game_registry.all_tiers(gid), "game_id": gid}
    except KeyError:
        # 未知 game_id 回退 holdem（保公开端点容错）
        return {"tiers": _game_registry.all_tiers("holdem"), "game_id": "holdem"}  # allow-game-fallback


@router.get("/api/levels/info")
def levels_info():
    """经验/等级体系定义（公开，前端展示进度条用）。"""
    from bzplat.backend.store.schema import (
        XP_COMMENT,
        XP_CONTEST_PARTICIPATE,
        XP_FOLLOWED,
        XP_MATCH_PARTICIPATE,
        XP_MATCH_WIN,
        xp_for_level,
    )
    return {
        "xp_match_participate": XP_MATCH_PARTICIPATE,
        "xp_match_win": XP_MATCH_WIN,
        "xp_contest_participate": XP_CONTEST_PARTICIPATE,
        "xp_comment": XP_COMMENT,
        "xp_followed": XP_FOLLOWED,
        "thresholds": [{"level": lv, "xp": xp_for_level(lv)} for lv in range(0, 11)],
    }


@router.get("/api/site/info")
def site_info(request: Request):
    """站点公开信息（站名/公告/about，无需登录）。"""
    from bzplat.backend.store.schema import (
        SETTING_SITE_NAME, SETTING_SITE_LOGO, SETTING_SITE_ANNOUNCEMENT, SETTING_SITE_ABOUT,
    )
    s = _store(request).get_settings([
        SETTING_SITE_NAME, SETTING_SITE_LOGO, SETTING_SITE_ANNOUNCEMENT, SETTING_SITE_ABOUT,
    ])
    return {
        "name": s.get(SETTING_SITE_NAME) or "Botbattle",
        "logo": s.get(SETTING_SITE_LOGO) or "",
        "announcement": s.get(SETTING_SITE_ANNOUNCEMENT) or "",
        "about": s.get(SETTING_SITE_ABOUT) or "",
    }


# ── comments / likes ──────────────────────────────────────────

class CommentCreate(BaseModel):
    target_type: str  # 'match' | 'bot'
    target_id: str
    body: str = Field(..., min_length=1, max_length=2000)


class LikeReq(BaseModel):
    target_type: str  # 'match' | 'bot' | 'comment'
    target_id: str


@router.get("/api/comments")
def list_comments(
    request: Request,
    target_type: str,
    target_id: str,
    limit: int = 100,
    page: int | None = None,
    per_page: int = 50,
):
    store = _store(request)
    lim = max(1, min(limit, 500))  # clamp（评论可能很多，但 500 已够看）
    result = store.list_comments(
        target_type, target_id, limit=lim, page=page, per_page=per_page,
    )
    if isinstance(result, dict):
        # 分页模式：total 已由 _paginate 算出，count 复用它避免冗余 COUNT 查询
        return {
            "comments": result["items"], "page": result["page"],
            "per_page": result["per_page"], "total": result["total"],
            "count": result["total"],
        }
    count = store.comment_count(target_type, target_id)
    return {"comments": result, "count": count}


@router.post("/api/comments")
def create_comment(
    req: CommentCreate, request: Request, user=Depends(require_user)
):
    store = _store(request)
    body = req.body.strip()
    if not body:
        raise HTTPException(400, "评论内容不能为空")
    c = store.add_comment(user["id"], req.target_type, req.target_id, body)
    # 评论经验（活跃度）
    from bzplat.backend.store.schema import XP_COMMENT
    store.award_xp(user["id"], XP_COMMENT)
    # 通知 target owner（match → 双方 bot owner；bot → bot owner）
    notifier = getattr(request.app.state, "notifier", None)
    if notifier is not None:
        try:
            if req.target_type == "match":
                m = store.get_match(req.target_id)
                if m:
                    notifier.notify_both_owners(
                        m["bot_a_id"], m["bot_b_id"], type="comment",
                        title="你的对局有新评论",
                        body=body[:80], link=f"/match/{req.target_id}",
                    )
            elif req.target_type == "bot":
                b = store.get_bot(int(req.target_id))
                if b and b.get("owner_id"):
                    notifier.notify(
                        b["owner_id"], type="comment",
                        title="你的 Bot 有新评论",
                        body=body[:80], link=f"/bot/{req.target_id}",
                    )
        except Exception:
            pass
    return {"comment": c}


@router.delete("/api/comments/{comment_id}")
def delete_comment(comment_id: int, request: Request, user=Depends(require_user)):
    store = _store(request)
    ok = store.delete_comment(comment_id, user["id"])
    if ok:
        return {"ok": True}
    # 删除失败：要么评论不存在，要么非作者。统一规则：评论不存在→404（先于权限判），
    # 存在但非作者→admin 可强删 / 非 admin 403。用只读 exists 区分（不破坏性删除）。
    exists = store.comment_exists(comment_id)
    if not exists:
        raise HTTPException(404, "评论不存在")
    if user.get("role") != "admin":
        raise HTTPException(403, "无权删除该评论")
    # admin 强删（无视作者）
    store.delete_comment_admin(comment_id)
    return {"ok": True}


@router.post("/api/likes")
def like_target(req: LikeReq, request: Request, user=Depends(require_user)):
    created = _store(request).like(user["id"], req.target_type, req.target_id)
    return {"ok": True, "liked": True, "created": created}


@router.delete("/api/likes")
def unlike_target(req: LikeReq, request: Request, user=Depends(require_user)):
    _store(request).unlike(user["id"], req.target_type, req.target_id)
    return {"ok": True, "liked": False}


@router.get("/api/likes/status")
def like_status(
    request: Request,
    target_type: str,
    target_id: str,
    user=Depends(require_user),
):
    store = _store(request)
    return {
        "liked": store.is_liked(user["id"], target_type, target_id),
        "count": store.like_count(target_type, target_id),
    }


@router.post("/api/matches/{match_id}/view")
def record_view(match_id: str, request: Request):
    """记录对局浏览（+1 views_count），公开。"""
    store = _store(request)
    if not store.get_match(match_id):
        raise HTTPException(404, "对局不存在")
    store.incr_match_view(match_id)
    return {"ok": True}


# ── notifications ─────────────────────────────────────────────

class NotifReadReq(BaseModel):
    id: int


@router.get("/api/notifications")
def list_notifications(
    request: Request,
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    page: int | None = None,
    per_page: int = 50,
    user=Depends(require_user),
):
    store = _store(request)
    lim = max(1, min(limit, 200))
    off = max(0, offset)
    result = store.list_notifications(
        user["id"], unread_only=unread_only, limit=lim, offset=off,
        page=page, per_page=per_page,
    )
    unread = store.unread_notification_count(user["id"])
    if isinstance(result, dict):
        return {
            "notifications": result["items"], "page": result["page"],
            "per_page": result["per_page"], "total": result["total"],
            "unread_count": unread,
        }
    return {"notifications": result, "unread_count": unread}


@router.get("/api/notifications/unread-count")
def unread_count(request: Request, user=Depends(require_user)):
    return {"count": _store(request).unread_notification_count(user["id"])}


@router.post("/api/notifications/read")
def read_notification(
    req: NotifReadReq, request: Request, user=Depends(require_user)
):
    ok = _store(request).mark_notification_read(req.id, user["id"])
    if not ok:
        raise HTTPException(404, "通知不存在或无权操作")
    return {"ok": True}


@router.post("/api/notifications/read-all")
def read_all_notifications(request: Request, user=Depends(require_user)):
    n = _store(request).mark_all_notifications_read(user["id"])
    return {"ok": True, "updated": n}


@router.get("/api/notification-prefs")
def get_notif_prefs(request: Request, user=Depends(require_user)):
    return {"prefs": _store(request).get_notification_prefs(user["id"])}


@router.put("/api/notification-prefs")
def update_notif_prefs(
    prefs: dict, request: Request, user=Depends(require_user)
):
    allowed = {
        "email_match_done", "email_followed", "email_contest", "email_comment",
    }
    clean = {k: v for k, v in prefs.items() if k in allowed}
    if not clean:
        raise HTTPException(400, "无可更新字段")
    return {"prefs": _store(request).update_notification_prefs(user["id"], **clean)}


# ── contests ──────────────────────────────────────────────────

class ContestCreate(BaseModel):
    title: str
    description: str = ""
    hands_per_match: int = 70
    template_id: str | None = None
    game_id: str | None = None
    stages: list[dict[str, Any]] | None = None
    match_config: dict[str, Any] | None = None
    phase: str = "standalone"  # P5: preliminary/final/standalone
    source_contest_id: int | None = None  # P5: 软链（预赛→决赛导航）
    require_real_name: bool = False  # 报名是否要求实名
    # 时间编排（ISO 字符串，可选；留空=手动触发对应阶段）
    registration_opens_at: str | None = None
    registration_closes_at: str | None = None
    starts_at: str | None = None


class ContestRegister(BaseModel):
    bot_id: int


class ContestDispatch(BaseModel):
    bot_id: int


@router.get("/api/contests/templates")
def contest_templates(request: Request, game: str | None = None):
    # 从 contest_templates 表读（与 admin 同源，含覆盖；修复原读代码默认的不一致）
    return {"templates": _store(request).list_contest_templates(game_id=game)}


# 访客/普通用户不可见的赛事状态（草稿/取消）——组织者/admin 可见全部（审计 P1-E）。
_CONTEST_HIDDEN_STATUSES = ["draft", "cancelled"]


@router.get("/api/contests")
def list_contests(request: Request, status: str | None = None, game_id: str | None = None,
                  page: int | None = None, per_page: int = 20,
                  user=Depends(optional_user)):
    # 非 organizer/admin 且未显式指定 status 时，默认排除 draft/cancelled
    # （组织者未发布的赛事结构不应提前暴露给访客）。显式传 status 则尊重调用方。
    is_privileged = user is not None and user.get("role") in ("organizer", "admin")
    exclude = None if (is_privileged or status) else _CONTEST_HIDDEN_STATUSES
    result = _store(request).list_contests(status=status, game_id=game_id,
                                           page=page, per_page=per_page,
                                           exclude_statuses=exclude)
    if isinstance(result, dict):
        return {"contests": result["items"], "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"contests": result}


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
            phase=body.phase,
            source_contest_id=body.source_contest_id,
            require_real_name=int(body.require_real_name),
            registration_opens_at=body.registration_opens_at,
            registration_closes_at=body.registration_closes_at,
            starts_at=body.starts_at,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_log(request, "contest_create", result="ok", user=user.get("username"), target=c["id"], detail=body.title)
    return {"contest": c}


@router.get("/api/contests/{contest_id}")
def contest_detail(
    contest_id: int, request: Request,
    entries_page: int | None = None, entries_per_page: int = 50,
    user=Depends(optional_user),
):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404, "比赛不存在")
    # draft/cancelled 赛事仅 organizer/admin 可见（与 list_contests 口径一致；审计 P1-E）
    is_privileged = user is not None and (
        c.get("organizer_id") == user.get("id") or user.get("role") in ("organizer", "admin")
    )
    if c.get("status") in _CONTEST_HIDDEN_STATUSES and not is_privileged:
        raise HTTPException(404, "比赛不存在")
    store = _store(request)
    # entries 可单列分页（115 报名场景）：提供 entries_page 时返回分页元信息，
    # 否则保持旧的全量列表契约（pairings/standings 不分页——stage 级，量小）。
    entries_result = store.contest_entries_named(
        contest_id, page=entries_page, per_page=entries_per_page,
    )
    if isinstance(entries_result, dict):
        entries = entries_result["items"]
        entries_meta = {
            "entries_page": entries_result["page"],
            "entries_per_page": entries_result["per_page"],
            "entries_total": entries_result["total"],
        }
    else:
        entries = entries_result
        entries_meta = {}
    pairings = store.contest_bracket(contest_id)
    standings = _contests(request).standings(contest_id)
    # 给 standings 补 bot 名（standings 只有 bot_id）
    for s in standings:
        b = store.get_bot(s.get("bot_id"))
        if b:
            s["bot_name"] = b.get("display_name") or b.get("name")
    try:
        estimate = _contests(request).estimate(contest_id)
    except ValueError:
        estimate = None
    # my_entry + is_organizer：当前登录用户的报名条目 + 是否赛事组织者（组织者可见实名）。
    # 未登录或未报名时 my_entry 为 null。实名脱敏：仅组织者/admin 可见 real_name/phone/
    # school/student_id（contest_entries_named 已 JOIN 这几列，这里对非组织者剔除）。
    my_entry = None
    is_organizer = False
    try:
        token = _extract_token(request)
        u = request.app.state.auth.verify_session(token) if token else None
        if u:
            my_entry = store.get_entry(contest_id, u["id"])
            is_organizer = c.get("organizer_id") == u.get("id") or u.get("role") == "admin"
    except Exception:
        pass
    # 非组织者脱敏实名字段（隐私保护——实名仅组织者用于线下核对/上报）
    if not is_organizer:
        for e in entries:
            for k in ("real_name", "phone", "school", "student_id"):
                e.pop(k, None)
    resp = {
        "contest": c,
        "entries": entries,
        "pairings": pairings,
        "standings": standings,
        "estimate": estimate,
        "my_entry": my_entry,
        "is_organizer": is_organizer,
    }
    resp.update(entries_meta)
    return resp


@router.get("/api/contests/{contest_id}/bracket")
def contest_bracket(contest_id: int, request: Request):
    """对阵图数据（带 bot 名/owner 名/winner，公开）。"""
    if not _store(request).get_contest(contest_id):
        raise HTTPException(404, "比赛不存在")
    return {"pairings": _store(request).contest_bracket(contest_id)}


def _require_contest_organizer(c: dict, user: dict) -> None:
    """校验当前用户是该场赛事组织者或 admin（与 open/start 同权限模型）。"""
    if c.get("organizer_id") != user.get("id") and user.get("role") != "admin":
        raise HTTPException(403, "仅该场赛事组织者或管理员可操作")


@router.post("/api/contests/{contest_id}/entries")
def organizer_add_entry(
    contest_id: int, body: dict, request: Request, user=Depends(require_organizer)
):
    """P5 组织者名单：单条加人（draft/open 允许）。

    有意设计：此处**绕开** register 流程的 owner 校验——组织者可替他人把任意其名下
    Bot 加进赛事（如现场代报名/补录）。权限收口在 _require_contest_organizer（仅组织者或
    admin 可调用），bot 归属/游戏/激活状态等业务校验仍保留。
    """
    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    if c["status"] not in ("draft", "open"):
        raise HTTPException(400, "开赛后不可改名册")
    from bzplat.backend.games import normalize_game_id
    raw_uid = body.get("user_id")
    raw_bid = body.get("bot_id")
    if raw_uid is None or raw_bid is None:
        raise HTTPException(400, "user_id 与 bot_id 均不可为空")
    try:
        uid, bid = int(raw_uid), int(raw_bid)
    except (TypeError, ValueError):
        raise HTTPException(400, "user_id / bot_id 必须是整数")
    if not store.get_user(uid):
        raise HTTPException(400, f"user {uid} 不存在")
    b = store.get_bot(bid)
    if not b or not b.get("is_active") or not b.get("binary_path"):
        raise HTTPException(400, "bot 不可用")
    cgid = normalize_game_id(c.get("game_id"))
    if normalize_game_id(b.get("game_id")) != cgid:
        raise HTTPException(400, f"bot 游戏 {b.get('game_id')} ≠ 赛事 {cgid}")
    if store.get_entry(contest_id, uid):
        raise HTTPException(400, "该用户已报名")
    store.add_contest_entry(contest_id, uid, bid)
    return {"ok": True}


@router.post("/api/contests/{contest_id}/entries/bulk")
def organizer_assign_entries(
    contest_id: int, body: AdminAssignEntries, request: Request, user=Depends(require_organizer)
):
    """P5 组织者名单：批量加人（迁移自 admin bulk，assign_all + 显式列表两模式）。"""
    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    if c["status"] not in ("draft", "open"):
        raise HTTPException(400, "开赛后不可改名册")
    from bzplat.backend.games import normalize_game_id
    cgid = normalize_game_id(c.get("game_id"))
    if body.assign_all:
        gid = normalize_game_id(body.game_id or cgid)
        if gid != cgid:
            raise HTTPException(400, f"assign_all 的 game_id {gid} 与赛事 {cgid} 不一致")
        bots = store.list_bots(active_only=True, game_id=gid)
        if body.name_prefix:
            np = body.name_prefix.lower()
            bots = [b for b in bots if np in (b.get("name") or "").lower()]
        seen_users: set[int] = set()
        target: list[tuple[int, int]] = []
        for b in bots:
            uid = b.get("owner_id")
            if uid is None or uid in seen_users:
                continue
            seen_users.add(uid)
            target.append((uid, b["id"]))
    else:
        target = [(int(e.get("user_id")), int(e.get("bot_id"))) for e in body.entries or []]
    added = 0
    skipped: list[str] = []
    existing = {e["user_id"] for e in store.list_entries(contest_id)}
    for uid, bid in target:
        if uid in existing:
            skipped.append(f"user {uid} 已报名，跳过")
            continue
        b = store.get_bot(bid)
        if not b or not b.get("is_active") or not b.get("binary_path"):
            skipped.append(f"bot {bid} 不可用，跳过")
            continue
        if normalize_game_id(b.get("game_id")) != cgid:
            skipped.append(f"bot {bid} 游戏 {b.get('game_id')} ≠ 赛事 {cgid}，跳过")
            continue
        store.add_contest_entry(contest_id, uid, bid)
        existing.add(uid)
        added += 1
    return {"added": added, "skipped": skipped, "total_entries": len(existing)}


@router.delete("/api/contests/{contest_id}/entries/{user_id}")
def organizer_delete_entry(
    contest_id: int, user_id: int, request: Request, user=Depends(require_organizer)
):
    """P5 组织者名单：删人（draft/open 允许）。"""
    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    if c["status"] not in ("draft", "open"):
        raise HTTPException(400, "开赛后不可改名册")
    if not store.delete_entry(contest_id, user_id):
        raise HTTPException(404, "报名记录不存在")
    return {"ok": True}


@router.get("/api/contests/{contest_id}/official-results")
def contest_official_results(contest_id: int, request: Request, format: str = "json"):
    """全员唯一正式名次（P2）。?format=csv|json 导出。

    赛事 finished 且 official_results_ready=1 时返回全员排名（1..N 唯一连续，
    含破同分明细 tiebreaks）；否则 404/409。
    """
    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        raise HTTPException(404, "比赛不存在")
    if not int(c.get("official_results_ready") or 0):
        raise HTTPException(409, "正式名次尚未生成（赛事未结束或排名未落库）")
    rows = store.list_official_results(contest_id)
    if format.lower() == "csv":
        import csv as _csv
        import io

        def gen():
            buf = io.StringIO()
            w = _csv.writer(buf)
            w.writerow(["rank", "entry_id", "bot_name", "owner_name", "points",
                        "buchholz_cut1", "sonneborn_berger", "awarded"])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)
            for r in rows:
                tb = r.get("tiebreaks_json") or "{}"
                try:
                    import json as _json
                    tb = _json.loads(tb)
                except Exception:
                    tb = {}
                w.writerow([
                    r["rank"], r["entry_id"], r.get("bot_name") or "",
                    r.get("owner_name") or "", r.get("points") or 0,
                    tb.get("buchholz_cut1", 0), tb.get("sonneborn_berger", 0),
                    r.get("awarded") or "",
                ])
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)

        return StreamingResponse(
            gen(), media_type="text/csv",
            headers={"Content-Disposition": f'attachment; filename="contest-{contest_id}-results.csv"'},
        )
    # json（默认）：返回结构化排名
    return {
        "contest_id": contest_id,
        "phase": c.get("phase") or "standalone",
        "ready": True,
        "results": rows,
    }


@router.get("/api/contests/{contest_id}/export")
def contest_export(contest_id: int, request: Request, format: str = "csv"):
    """组织者导出：报名名单（含实名）+ 结果排名合并 CSV。

    仅赛事组织者/admin 可访问（实名隐私）。任何赛事状态可导出：
    - 报名中（draft/open）：导出已报名名单（rank 列空）。
    - 已结束（finished）：含正式排名 + 战绩。
    列：rank, seed, group_id, bot_name, owner_name, real_name, phone, school,
    student_id, points, wins, draws, losses, eliminated, awarded, registered_at。
    """
    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        raise HTTPException(404, "比赛不存在")
    if format.lower() not in ("csv",):
        raise HTTPException(400, "仅支持 format=csv")
    # 组织者鉴权（实名隐私——仅组织者/admin 可导出）。用 _extract_token + verify_session
    # 取当前用户（endpoint 无 Depends(require_user)，直接从 request 解析）。
    token = _extract_token(request)
    user = request.app.state.auth.verify_session(token) if token else None
    if not user:
        raise HTTPException(401, "未登录或会话过期")
    _require_contest_organizer(c, user)
    rows = store.list_contest_export(contest_id)
    import csv as _csv
    import io

    def _safe(v: object) -> object:
        """防 CSV 公式注入：以 =/+/-/@ 开头的字符串前缀单引号（Excel 不解释为公式）。"""
        if isinstance(v, str) and v and v[0] in ("=", "+", "-", "@"):
            return "'" + v
        return v

    def gen():
        buf = io.StringIO()
        w = _csv.writer(buf)
        # BOM 让 Excel 正确识别 UTF-8
        yield "\ufeff"
        w.writerow(["rank", "seed", "group_id", "bot_name", "owner_name",
                    "real_name", "phone", "school", "student_id",
                    "points", "wins", "draws", "losses", "eliminated",
                    "awarded", "registered_at"])
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        for r in rows:
            w.writerow([
                r.get("rank") if r.get("rank") is not None else "",
                r.get("seed") or 0, _safe(r.get("group_id") or ""),
                _safe(r.get("bot_name") or ""), _safe(r.get("owner_name") or ""),
                _safe(r.get("real_name") or ""), _safe(r.get("phone") or ""),
                _safe(r.get("school") or ""), _safe(r.get("student_id") or ""),
                r.get("points") if r.get("points") is not None else "",
                r.get("wins") if r.get("wins") is not None else "",
                r.get("draws") if r.get("draws") is not None else "",
                r.get("losses") if r.get("losses") is not None else "",
                int(bool(r.get("eliminated"))),
                _safe(r.get("awarded") or ""), r.get("registered_at") or "",
            ])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    return StreamingResponse(
        gen(), media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="contest-{contest_id}-export.csv"'},
    )


@router.post("/api/contests/{contest_id}/open")
def open_contest(contest_id: int, request: Request, user=Depends(require_organizer)):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
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
    # 赛事报名经验
    from bzplat.backend.store.schema import XP_CONTEST_PARTICIPATE
    _store(request).award_xp(user["id"], XP_CONTEST_PARTICIPATE)
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
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    try:
        contest = await _contests(request).start(contest_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"contest": contest}


@router.post("/api/contests/{contest_id}/publish")
async def publish_contest(
    contest_id: int, request: Request, user=Depends(require_organizer)
):
    """手动截止报名 + 出排期（open→published）。

    生成对阵 + 逐场排期 scheduled_at + 冻结版本，但不立即开打——等开赛时间到
    调度器 dispatch（或组织者手动 start 立即开打）。
    """
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    try:
        contest = await _contests(request).publish(contest_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    audit_log(request, "contest_publish", result="ok", user=user.get("username"), target=contest_id)
    return {"contest": contest}


@router.post("/api/contests/{contest_id}/resume")
async def resume_contest(
    contest_id: int, request: Request, user=Depends(require_organizer)
):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
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
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    try:
        contest = await _contests(request).advance(contest_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"contest": contest}


# ── admin ─────────────────────────────────────────────────────

@router.get("/api/admin/users")
def admin_users(
    request: Request, page: int | None = None, per_page: int = 50,
    _admin=Depends(require_admin),
):
    result = _store(request).list_users(page=page, per_page=per_page)
    if isinstance(result, dict):
        return {"users": result["items"], "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"users": result}


@router.post("/api/admin/users/{user_id}/role")
def admin_set_role(
    user_id: int, role: str, request: Request, admin=Depends(require_admin)
):
    if role not in ("user", "organizer", "admin"):
        raise HTTPException(400, "非法角色")
    u = _store(request).update_user(user_id, role=role)
    audit_log(request, "admin_set_role", result="ok", user=admin.get("username"), target=user_id, detail=f"role={role}")
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
    audit_log(request, "admin_delete_user", result="ok", user=admin.get("username"), target=user_id)
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
    game_id: str | None = None,
    page: int | None = None,
    per_page: int = 50,
    _admin=Depends(require_admin),
):
    rows = _store(request).list_bots(
        active_only=bool(active) if active is not None else False,
        game_id=game_id,
    )
    if q:
        ql = q.lower()
        rows = [b for b in rows if ql in (b.get("name") or "").lower()
                or ql in (b.get("display_name") or "").lower()
                or ql in str(b.get("owner_id"))]
    total = len(rows)
    if page is not None:
        pp = max(1, min(200, per_page))
        offset = (max(1, page) - 1) * pp
        rows = rows[offset:offset + pp]
        return {"bots": rows, "page": page, "per_page": pp, "total": total}
    return {"bots": rows}


@router.patch("/api/admin/bots/{bot_id}")
def admin_patch_bot(
    bot_id: int, body: dict, request: Request, _admin=Depends(require_admin)
):
    allowed = {"is_active", "is_builtin", "display_name", "description"}
    fields = {k: (1 if v is True else 0 if v is False else v)
              for k, v in body.items() if k in allowed}
    if not fields:
        raise HTTPException(400, "无可更新字段")
    b = _store(request).update_bot(bot_id, **fields)
    if not b:
        raise HTTPException(404, "bot 不存在")
    return {"bot": b}


@router.delete("/api/admin/bots/{bot_id}")
def admin_delete_bot(bot_id: int, request: Request, admin=Depends(require_admin)):
    store = _store(request)
    # 业务规则：硬删前检查活跃引用。bots 表 FK 是 ON DELETE SET NULL（matches 与
    # contest_pairings/entries 均为 SET NULL，保历史）。硬删正在打(pending/running)对局或
    # 进行中赛事(published/running/rest)报名的 bot 会：①让运行中对局 bot_id 变 NULL→
    # _apply_ratings(None) 崩；②进行中赛事对阵/报名的 bot_id 变 NULL→对阵表残缺。
    # 此时应改用停用（is_active=0，用户路径）。
    refs = store.bot_active_references(bot_id)
    if any(v > 0 for v in refs.values()):
        raise HTTPException(
            409,
            f"bot 存在活跃引用，不能硬删：{refs}（进行中对局/赛事；请改用停用 is_active=0）",
        )
    if not store.delete_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    # 硬删 bot 后清理磁盘文件（bot_uploads/<id>/），避免孤儿
    _bots(request).purge_bot_files(bot_id)
    audit_log(request, "admin_delete_bot", result="ok", user=admin.get("username"), target=bot_id)
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
    # 时间编排（admin 可改时间窗口）
    registration_opens_at: str | None = None
    registration_closes_at: str | None = None
    starts_at: str | None = None


@router.get("/api/admin/contests")
def admin_contests(
    request: Request, status: str | None = None, game_id: str | None = None,
    page: int | None = None, per_page: int = 50,
    _admin=Depends(require_admin),
):
    result = _store(request).list_contests(status=status, game_id=game_id,
                                           page=page, per_page=per_page)
    if isinstance(result, dict):
        return {"contests": result["items"], "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"contests": result}


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
            if c0["status"] in ("open", "draft", "published"):
                try:
                    contest = await _contests(request).start(contest_id)
                except ValueError as e:
                    raise HTTPException(400, str(e))
                return {"contest": contest}
        if body.status not in (
            "draft", "open", "published", "running", "rest", "finished", "cancelled"
        ):
            raise HTTPException(400, "非法比赛状态")
        fields["status"] = body.status
    if body.title is not None:
        fields["title"] = body.title
    if body.hands_per_match is not None:
        fields["hands_per_match"] = body.hands_per_match
    # 时间编排字段（admin 可改）
    for tk in ("registration_opens_at", "registration_closes_at", "starts_at"):
        tv = getattr(body, tk)
        if tv is not None:
            fields[tk] = tv
    if not fields:
        raise HTTPException(400, "无更新字段")
    c = _store(request).update_contest(contest_id, **fields)
    if not c:
        raise HTTPException(404, "比赛不存在")
    return {"contest": c}


@router.delete("/api/admin/contests/{contest_id}")
def admin_delete_contest(contest_id: int, request: Request, admin=Depends(require_admin)):
    if not _store(request).delete_contest(contest_id):
        raise HTTPException(404, "比赛不存在")
    audit_log(request, "admin_delete_contest", result="ok", user=admin.get("username"), target=contest_id)
    return {"ok": True}


@router.get("/api/admin/contests/{contest_id}/entries")
def admin_contest_entries(
    contest_id: int, request: Request, _admin=Depends(require_admin)
):
    return {"entries": _store(request).list_entries(contest_id)}


class AdminAssignEntries(BaseModel):
    """管理员批量指派参赛者+Bot（测试期 admin 派遣，正式版用户自己报名）。

    两种模式：
    - 显式列表：entries=[{user_id, bot_id}, ...]
    - 便捷全选：assign_all=true + game_id（自动找该游戏所有 active+public 的 Bot，
      每用户取其一个 Bot 指派），可选 name_prefix 过滤 Bot/用户名前缀（如 "load_"）。
    """
    entries: list[dict] | None = None  # [{user_id, bot_id}, ...]
    assign_all: bool = False
    game_id: str | None = None
    name_prefix: str | None = None


@router.post("/api/admin/contests/{contest_id}/entries/bulk")
def admin_assign_entries(
    contest_id: int, body: AdminAssignEntries, request: Request, _admin=Depends(require_admin)
):
    """管理员批量指派参赛者+Bot。绕开 register() 的 CONTEST_OPEN + owner 校验（admin 专享）。
    校验：bot 存在+active、bot.game_id==contest.game_id、用户未重复报名。"""
    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    from bzplat.backend.games import normalize_game_id
    cgid = normalize_game_id(c.get("game_id"))

    # 解析目标 entries 列表
    if body.assign_all:
        gid = normalize_game_id(body.game_id or cgid)
        if gid != cgid:
            raise HTTPException(400, f"assign_all 的 game_id {gid} 与赛事 {cgid} 不一致")
        bots = store.list_bots(active_only=True, game_id=gid)
        if body.name_prefix:
            np = body.name_prefix.lower()
            bots = [b for b in bots if np in (b.get("name") or "").lower()]
        # 每用户取其一个 Bot（UNIQUE(contest,user) 限制每用户一个 Bot）
        seen_users: set[int] = set()
        target: list[tuple[int, int]] = []
        for b in bots:
            uid = b.get("owner_id")
            if uid is None or uid in seen_users:
                continue
            seen_users.add(uid)
            target.append((uid, b["id"]))
    else:
        target = []
        for e in body.entries or []:
            uid = int(e.get("user_id"))
            bid = int(e.get("bot_id"))
            target.append((uid, bid))

    added = 0
    skipped: list[str] = []
    existing = {e["user_id"] for e in store.list_entries(contest_id)}
    for uid, bid in target:
        if uid in existing:
            skipped.append(f"user {uid} 已报名，跳过")
            continue
        b = store.get_bot(bid)
        if not b or not b.get("is_active") or not b.get("binary_path"):
            skipped.append(f"bot {bid} 不可用，跳过")
            continue
        if normalize_game_id(b.get("game_id")) != cgid:
            skipped.append(f"bot {bid} 游戏 {b.get('game_id')} ≠ 赛事 {cgid}，跳过")
            continue
        store.add_contest_entry(contest_id, uid, bid)
        existing.add(uid)
        added += 1
    audit_log(request, "admin_assign_entries", result="ok", target=contest_id,
              detail=f"added={added} skipped={len(skipped)}")
    return {"added": added, "skipped": skipped, "total_entries": len(existing)}


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
    auto_match_placement_games: int | None = None
    auto_match_max_per_round: int | None = None
    auto_match_daily_cap: int | None = None


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
        "placement_games": int(_sett(SETTING_AUTO_MATCH_PLACEMENT_GAMES, "10")),
        "max_per_round": int(_sett(SETTING_AUTO_MATCH_MAX_PER_ROUND, "2")),
        "daily_cap": int(_sett(SETTING_AUTO_MATCH_DAILY_CAP, "200")),
        "daily_count": getattr(getattr(request.app.state, "auto_matcher", None), "daily_count", 0),
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


class SiteSettingsPatch(BaseModel):
    name: str | None = Field(None, max_length=64)
    logo: str | None = Field(None, max_length=500)
    announcement: str | None = Field(None, max_length=2000)
    about: str | None = Field(None, max_length=5000)


@router.patch("/api/admin/settings/site")
def admin_patch_site(
    body: SiteSettingsPatch, request: Request, _admin=Depends(require_admin)
):
    from bzplat.backend.store.schema import (
        SETTING_SITE_NAME, SETTING_SITE_LOGO, SETTING_SITE_ANNOUNCEMENT, SETTING_SITE_ABOUT,
    )
    store = _store(request)
    if body.name is not None:
        store.set_setting(SETTING_SITE_NAME, body.name)
    if body.logo is not None:
        store.set_setting(SETTING_SITE_LOGO, body.logo)
    if body.announcement is not None:
        store.set_setting(SETTING_SITE_ANNOUNCEMENT, body.announcement)
    if body.about is not None:
        store.set_setting(SETTING_SITE_ABOUT, body.about)
    s = store.get_settings([
        SETTING_SITE_NAME, SETTING_SITE_LOGO, SETTING_SITE_ANNOUNCEMENT, SETTING_SITE_ABOUT,
    ])
    return {"site": {
        "name": s.get(SETTING_SITE_NAME) or "Botbattle",
        "logo": s.get(SETTING_SITE_LOGO) or "",
        "announcement": s.get(SETTING_SITE_ANNOUNCEMENT) or "",
        "about": s.get(SETTING_SITE_ABOUT) or "",
    }}


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
        if v < 0 or v > 604800:  # 0=不限
            raise HTTPException(400, "auto_match_stale_sec 须在 0–604800（0=不限）")
        store.set_setting(SETTING_AUTO_MATCH_STALE_SEC, str(v))
        updated["auto_match_stale_sec"] = v
    if body.auto_match_reserve_slots is not None:
        v = int(body.auto_match_reserve_slots)
        if v < 0 or v > ceiling:
            raise HTTPException(400, "auto_match_reserve_slots 须在 0–ceiling")
        store.set_setting(SETTING_AUTO_MATCH_RESERVE_SLOTS, str(v))
        updated["auto_match_reserve_slots"] = v
    if body.auto_match_placement_games is not None:
        v = int(body.auto_match_placement_games)
        if v < 0 or v > 100:
            raise HTTPException(400, "auto_match_placement_games 须在 0–100（0=禁用）")
        store.set_setting(SETTING_AUTO_MATCH_PLACEMENT_GAMES, str(v))
        updated["auto_match_placement_games"] = v
    if body.auto_match_max_per_round is not None:
        v = int(body.auto_match_max_per_round)
        if v < 1 or v > 50:
            raise HTTPException(400, "auto_match_max_per_round 须在 1–50")
        store.set_setting(SETTING_AUTO_MATCH_MAX_PER_ROUND, str(v))
        updated["auto_match_max_per_round"] = v
    if body.auto_match_daily_cap is not None:
        v = int(body.auto_match_daily_cap)
        if v < 0 or v > 100000:
            raise HTTPException(400, "auto_match_daily_cap 须在 0–100000（0=不限）")
        store.set_setting(SETTING_AUTO_MATCH_DAILY_CAP, str(v))
        updated["auto_match_daily_cap"] = v

    if not updated:
        raise HTTPException(400, "无更新字段")
    return {"updated": updated, "runtime": admin_get_runtime(request, _admin)}


# ── admin: 裁判引擎（规则参数热调 + 代码只读） ────────────────────
# 裁判规则参数存 platform_settings，orchestrator 每局热读，下局即生效。
# PR2：三张表（JUDGE_PARAM_BOUNDS/JUDGE_PARAM_DEFAULTS/JUDGE_GAMES）全部从
# games 注册表派生——消除第 4 个并行游戏元数据来源，单一真相。
from bzplat.backend.games import registry as _game_registry

JUDGE_PARAM_DEFAULTS, JUDGE_PARAM_BOUNDS = _game_registry.judge_param_table()
JUDGE_GAMES: list[dict[str, Any]] = _game_registry.judge_games()


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


# ── 公开：裁判规则与源码（对全体玩家透明，可审计） ──────────────────
# 裁判是公开的规则定义（非私有 Bot 策略）——源码必须对全体玩家公开明文展示。
# 这与 Bot 端「私有黑盒二进制」形成对照：Bot 保护玩家智力成果，裁判保证规则公正可查。
@router.get("/api/judges")
def public_get_judges(request: Request):
    """公开裁判列表：每游戏的元信息（label/code_path/summary/source_files 列表）。

    无需登录——任意访客可查。不含可调参数当前值（那是 admin 能力）。
    """
    return {
        "games": [
            {
                "game_id": g["game_id"],
                "label": g["label"],
                "code_path": g["code_path"],
                "summary": g["summary"],
                "source_files": g["source_files"],
            }
            for g in JUDGE_GAMES
        ]
    }


@router.get("/api/judges/{game_id}/source")
def public_get_judge_source(game_id: str, request: Request):
    """公开裁判源码全文：返回该游戏 spec.source_files 声明的源码文件明文。

    无需登录——任意访客可查。裁判源码透明是平台公正性的基础。
    """
    try:
        spec = _game_registry.get(game_id)
    except KeyError:
        raise HTTPException(404, f"未注册的游戏: {game_id!r}")
    backend_dir = Path(__file__).resolve().parent
    # games 包目录：code_path 形如 bzplat/backend/games/<game>/engine.py
    pkg_dir = backend_dir / "games" / spec.game_id
    files: list[dict[str, str]] = []
    for rel in spec.source_files:
        path = pkg_dir / rel
        if not path.is_file():
            continue  # 游戏未提供该文件则跳过（如某些游戏无独立 result.py）
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        files.append({"name": rel, "path": str(path.relative_to(backend_dir.parent)), "source": text})
    return {
        "game_id": spec.game_id,
        "label": spec.label,
        "summary": spec.summary,
        "files": files,
    }


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
    # 裁判代码说明（JUDGE_CODE.md 已移至 doc/——面向开发者；玩家经 /api/judges/{game}/source 看源码）
    judge_code_path = _wiki_dir().parent / "doc" / "JUDGE_CODE.md"
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
    game_id: str = "holdem"


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
        norm_stages = [validate_stage(s, i, body.game_id) for i, s in enumerate(body.stages)]
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
    file: str = "app",
    _admin=Depends(require_admin),
):
    """读 logs/{app,access,audit}.log 末尾 N 行，按级别/关键字过滤。

    file: app（业务/系统）、access（HTTP 访问，含真实 IP）、audit（安全审计）。
    """
    # 白名单：只允许读这三个日志文件，防路径穿越
    allowed = {"app": "app.log", "access": "access.log", "audit": "audit.log"}
    fname = allowed.get(file, "app.log")
    log_path = Path(os.environ.get("BZ_LOG_DIR", "logs")) / fname
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
# 精简为 7 页（核心 = 3 游戏；功能说明统一进 GUIDE）。
WIKI_PAGES: list[dict[str, str]] = [
    {"slug": "index", "file": "INDEX.md", "title": "Wiki 首页", "summary": "站内文档导航与 Botzone 差异总览"},
    {"slug": "protocol", "file": "PROTOCOL.md", "title": "协议规范", "summary": "Botzone 标准对局协议、信封、两模式、卡牌编码"},
    {"slug": "bot-dev", "file": "BOT_DEV.md", "title": "Bot 开发指南", "summary": "从零编写一个 Bot：样例、编译、上传、调试"},
    {"slug": "texas", "file": "TEXAS.md", "title": "德州扑克 (TexasHoldem2p)", "summary": "Botzone 规则摘要与本平台协议对照"},
    {"slug": "gomoku", "file": "GOMOKU.md", "title": "五子棋 (Gomoku)", "summary": "15×15 规则、协议、样例 + 一手交换变体"},
    {"slug": "pencil", "file": "PENCIL.md", "title": "点格棋 (Pencil)", "summary": "交错网格、pass 连走与协议"},
    {"slug": "guide", "file": "GUIDE.md", "title": "平台功能指南", "summary": "对局/裁判/段位/等级/锦标赛/Bot详情/用户主页/社交/通知/设置——一页看全"},
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
