"""Bots / Matches / Contests / Admin / Leaderboard API 路由。"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import re
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

logger = logging.getLogger(__name__)
from bzplat.backend.contests import ContestManager
from bzplat.backend.contests.stages import estimate_match_count
from bzplat.backend.contests.validation import validate_stage, validate_template
from bzplat.backend.games import registry as game_registry
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
from bzplat.backend.runtime.binary_runner import PlatformRunnerError
from bzplat.backend.store.schema import (
    CONTEST_CANCELLED,
    CONTEST_DRAFT,
    CONTEST_FINISHED,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    CONTEST_REST,
    CONTEST_RUNNING,
    DEFAULT_RUNTIME_MODE,
    ROLE_ADMIN,
    ROLE_ORGANIZER,
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
    SETTING_MAX_CONCURRENT,
    SUPPORTED_BINARY_ERROR,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    is_supported_binary_metadata,
)
router = APIRouter()


def _orch(request: Request) -> MatchOrchestrator:
    return request.app.state.orch


def _bots(request: Request) -> BotManager:
    return request.app.state.bot_manager


def _with_bot_runnable(bot: dict) -> dict:
    """Expose current executability without rewriting legacy metadata."""
    public = dict(bot)
    runnable = is_supported_binary_metadata(
        str(public.get("format") or ""),
        str(public.get("os") or ""),
        str(public.get("arch") or ""),
    )
    public["runnable"] = runnable
    public["unsupported_reason"] = None if runnable else SUPPORTED_BINARY_ERROR
    return public


def _new_preflight_runner(request: Request):
    """Return one upload-owned runner; never share match subprocess state."""
    factory = getattr(request.app.state, "preflight_runner_factory", None)
    if factory is not None:
        return factory()
    # Compatibility for narrowly constructed test apps; create_app always installs
    # the factory above.
    return getattr(request.app.state, "binary_runner", None)


def _contests(request: Request) -> ContestManager:
    return request.app.state.contest_manager


def _store(request: Request):
    return request.app.state.store


def _is_terminal_stream_event(ev: object) -> bool:
    if not isinstance(ev, dict):
        return False
    if ev.get("type") in ("match_end", "error"):
        return True
    return (
        ev.get("type") == "snapshot"
        and (ev.get("match") or {}).get("status")
        in (STATUS_COMPLETED, STATUS_ABORTED)
    )


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
    # 裁响应死字段（对抗审计：created_at/updated_at 前端 MyBots 不消费；
    # 不动 owner_id/is_builtin——共享 list_bots 喂 /api/bots/public + /api/admin/bots）。
    items = [
        _with_bot_runnable(bot)
        for bot in (result["items"] if isinstance(result, dict) else result)
    ]
    for b in items:
        b.pop("created_at", None)
        b.pop("updated_at", None)
    if isinstance(result, dict):
        return {"bots": items, "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"bots": items}


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
    bots = [_sanitize_bot(_with_bot_runnable(b), user) for b in bots]
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
                logger.warning("follow notify failed target=%s follower=%s", user_id, user.get("id"), exc_info=True)
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
    result = store.list_bots(
        owner_id=u["id"], runnable_only=True, page=page, per_page=per_page
    )
    if isinstance(result, dict):
        # 脱敏敏感字段（审计 P1-B）
        items = [_sanitize_bot(_with_bot_runnable(b), user) for b in result["items"]]
        return {"bots": items, "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"bots": [_sanitize_bot(_with_bot_runnable(b), user) for b in result]}


@router.get("/api/search")
def global_search(
    request: Request,
    q: str | None = None,
    type: str | None = None,
    limit: int = 20,
    game_id: str | None = None,
):
    """全局搜索：type=users|bots|matches（默认 users）。

    bots 按 name/display_name 模糊；matches 按对局 ID/bot 名模糊；users 沿用前缀搜索。
    game_id 可选过滤（仅对 bots/matches 有效）。
    """
    store = _store(request)
    ql = (q or "").strip()
    lim = max(1, min(limit, 50))
    t = (type or "users").lower()
    if t == "bots":
        return {
            "bots": [
                _with_bot_runnable(bot)
                for bot in store.search_bots(ql, limit=lim, game_id=game_id)
            ]
        }
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
    return {"bot": _sanitize_bot(_with_bot_runnable(bot), user)}


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
    p = _with_bot_runnable(p)
    # 裁响应死字段（对抗审计验证：vol/net_chips/rated_at/is_builtin/updated_at 前端不消费；
    # 留 matches_played/tier_level/owner_id——store 测试断言 + 前端补展示）。
    for k in ("vol", "net_chips", "rated_at", "is_builtin", "updated_at"):
        p.pop(k, None)
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
        bot = await asyncio.to_thread(
            _bots(request).create_from_upload,
            user["id"],
            name,
            raw,
            display_name=display_name, description=description,
            upload_note=upload_note,
            game_id=game_id,
            runtime_mode=runtime_mode,
            binary_runner=_new_preflight_runner(request),
        )
    except BotError as e:
        audit_log(request, "bot_upload", result="fail", user=user.get("username"), target=name, detail=e.code)
        raise HTTPException(400, detail={"code": e.code, "message": e.message})
    except PlatformRunnerError:
        audit_log(request, "bot_upload", result="fail", user=user.get("username"), target=name, detail="sandbox_unavailable")
        raise HTTPException(503, "Bot 沙箱暂不可用，请稍后重试")
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
        bot = await asyncio.to_thread(
            _bots(request).upload_version,
            bot_id,
            user["id"],
            raw,
            upload_note=upload_note,
            runtime_mode=runtime_mode or None,
            binary_runner=_new_preflight_runner(request),
        )
    except BotError as e:
        audit_log(request, "bot_version_upload", result="fail", user=user.get("username"), target=bot_id, detail=e.code)
        raise HTTPException(400, detail={"code": e.code, "message": e.message})
    except PlatformRunnerError:
        audit_log(request, "bot_version_upload", result="fail", user=user.get("username"), target=bot_id, detail="sandbox_unavailable")
        raise HTTPException(503, "Bot 沙箱暂不可用，请稍后重试")
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
    versions = [_with_bot_runnable(v) for v in store.list_bot_versions(bot_id)]
    if not is_owner:
        versions = [v for v in versions if v["runnable"]]
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
                "runnable": True,
                "unsupported_reason": None,
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
    try:
        result = _bots(request).activate_version(bot_id, user["id"], version)
    except BotError as e:
        status = {
            "forbidden": 403,
            "not_found": 404,
            "version_not_found": 404,
            "unsupported_binary": 409,
            "version_unavailable": 409,
        }.get(e.code, 400)
        raise HTTPException(status, detail={"code": e.code, "message": e.message})
    audit_log(request, "bot_version_rollback", result="ok", user=user.get("username"), target=bot_id, detail=f"v{version}")
    return {"bot": result}


@router.post("/api/bots/{bot_id}/active")
def set_bot_active(
    bot_id: int, request: Request, active: bool = True, user=Depends(require_user)
):
    try:
        bot = _bots(request).set_active(bot_id, user["id"], active)
    except BotError as e:
        status = (
            404 if e.code == "not_found"
            else 403 if e.code == "forbidden"
            else 409 if e.code in {"unsupported_binary", "version_unavailable"}
            else 400
        )
        raise HTTPException(status, detail={"code": e.code, "message": e.message})
    return {"bot": bot}


@router.patch("/api/bots/{bot_id}")
def update_my_bot(
    bot_id: int, body: dict, request: Request, user=Depends(require_user)
):
    """Bot 拥有者改 display_name/description/is_active（受限白名单）。"""
    allowed = {"display_name", "description", "is_active"}
    unknown = set(body).difference(allowed)
    if unknown:
        raise HTTPException(422, f"不支持的字段：{', '.join(sorted(unknown))}")
    if "is_active" in body and not isinstance(body["is_active"], bool):
        raise HTTPException(422, "is_active 必须是布尔值")
    fields: dict[str, Any] = {}
    if "display_name" in body:
        fields["display_name"] = str(body["display_name"])[:200]
    if "description" in body:
        fields["description"] = str(body["description"])[:2000]
    if "is_active" in body:
        fields["is_active"] = 1 if body["is_active"] else 0
    if not fields:
        raise HTTPException(400, "无可更新字段")
    try:
        bot = _bots(request).patch_owner(bot_id, user["id"], **fields)
    except BotError as exc:
        status = (
            404 if exc.code == "not_found"
            else 403 if exc.code == "forbidden"
            else 409
            if exc.code in {"unsupported_binary", "version_unavailable"}
            else 400
        )
        raise HTTPException(
            status, detail={"code": exc.code, "message": exc.message}
        ) from exc
    return {"bot": _with_bot_runnable(bot)}


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

_FIXED_RULE_OVERRIDE_FIELDS = frozenset({
    "match_config",
    "hands",
    "num_hands",
    "hands_per_match",
    "max_hand",
    "total_hands",
    "maxHands",
    "numHands",
    "n_dots",
    "board_size",
    "boardSize",
    "grid_size",
    "nDots",
    "starting_stack",
    "startingStack",
    "small_blind",
    "smallBlind",
    "big_blind",
    "bigBlind",
    "sb",
    "bb",
    "stack",
    "initial_stack",
    "time_limit",
    "timeLimit",
    "time_limit_per_side",
    "timeLimitPerSide",
    "time_budget_per_side",
    "timeBudgetPerSide",
})


def _find_fixed_rule_overrides(value: Any) -> set[str]:
    """查找嵌在通用阶段字典中的非现行规则字段。"""
    found: set[str] = set()
    if isinstance(value, dict):
        found.update(_FIXED_RULE_OVERRIDE_FIELDS.intersection(value))
        for child in value.values():
            found.update(_find_fixed_rule_overrides(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_find_fixed_rule_overrides(child))
    return found


def _reject_fixed_rule_overrides(value: Any) -> None:
    fields = _find_fixed_rule_overrides(value)
    if fields:
        raise HTTPException(
            400,
            "游戏规则为平台固定契约，不允许覆盖字段: "
            + ", ".join(sorted(fields)),
        )


class ChallengeBody(BaseModel):
    model_config = {"extra": "forbid"}

    my_bot_id: int
    opponent_bot_id: int
    # 版本快照（可选）：指定各座位跑哪个版本。缺省/None=当前激活版本。
    # 自博弈（my_bot_id == opponent_bot_id）允许——用于同 bot 新旧版本对比。
    my_bot_version_id: int | None = None
    opponent_bot_version_id: int | None = None
    game_id: str | None = None


@router.post("/api/matches/challenge")
async def challenge(body: ChallengeBody, request: Request, user=Depends(require_user)):
    # P1-3 安全修复：my_bot_id 必须属于当前用户（防用别人的 bot 开赛，污染其评分/战绩）。
    # opponent_bot_id 允许任意（挑战他人 bot 是正常功能）。
    my_bot = _store(request).get_bot(body.my_bot_id)
    if not my_bot:
        raise HTTPException(404, "Bot 不存在")
    if my_bot["owner_id"] != user["id"]:
        audit_log(request, "match_challenge", result="deny", user=user.get("username"),
                  detail=f"my_bot_id={body.my_bot_id} 非本人 bot")
        raise HTTPException(403, "只能用自己的 Bot 发起挑战")
    try:
        mid = await _orch(request).challenge(
            body.my_bot_id,
            body.opponent_bot_id,
            user["id"],
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
    model_config = {"extra": "forbid"}

    bot_id: int
    human_seat: int = 1  # 0 或 1，人类坐哪位
    game_id: str | None = None


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
    # subscribe 入队一条带 seats 与完整回放的权威 snapshot；pump 随即发送。
    # 不在路由重复发送，否则每次连接都会收到两份相同快照，造成前端重复归约。
    q = orch.subscribe(match_id)
    human_seat = int(m.get("human_seat")) if m.get("human_seat") is not None else 1
    try:
        protocol = game_registry.get(m.get("game_id")).protocol
    except KeyError:
        await websocket.send_json({"type": "error", "message": "对局游戏协议不存在"})
        await websocket.close()
        orch.unsubscribe(match_id, q)
        return

    async def pump_events():
        while True:
            ev = await q.get()
            await websocket.send_json(ev)
            if _is_terminal_stream_event(ev):
                return

    async def receive_actions():
        while True:
            msg = await websocket.receive_json()
            if not isinstance(msg, dict) or set(msg) != {"response"}:
                await websocket.send_json({
                    "type": "reject",
                    "message": "动作协议错误：消息必须恰好包含 response 字段",
                })
                continue
            try:
                payload = protocol.validate_response_payload(msg["response"])
            except (TypeError, ValueError) as exc:
                await websocket.send_json({
                    "type": "reject",
                    "message": f"动作协议错误：{exc}",
                })
                continue
            move = {"response": payload}
            if not orch.resolve_human_turn(match_id, human_seat, move):
                await websocket.send_json({"type": "reject", "message": "当前非你的回合或动作非法"})

    sender = asyncio.create_task(pump_events())
    receiver = asyncio.create_task(receive_actions())
    try:
        done, _ = await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
        # A terminal event must close the server side even if an old or malicious
        # client never sends another frame. Otherwise receive_json + subscription leak.
        if sender in done and not receiver.done():
            try:
                await websocket.close(code=1000)
            except (RuntimeError, WebSocketDisconnect):
                pass
    finally:
        sender.cancel()
        receiver.cancel()
        await asyncio.gather(sender, receiver, return_exceptions=True)
        orch.unsubscribe(match_id, q)


@router.get("/api/matches")
def list_matches(
    request: Request,
    status: str | None = None,
    game_id: str | None = None,
    has_technical_incidents: bool | None = None,
    limit: int = 50,
    offset: int = 0,
):
    retired_filters = tuple(
        name
        for name in ("has_bot_incidents", "has_bot_errors")
        if name in request.query_params
    )
    if retired_filters:
        raise HTTPException(
            400,
            f"{', '.join(retired_filters)} 已移除；"
            "请使用 has_technical_incidents",
        )
    store = _store(request)
    lim = max(1, min(limit, 100))
    off = max(0, offset)
    rows = store.list_matches(
        status=status,
        game_id=game_id,
        has_technical_incidents=has_technical_incidents,
        limit=lim,
        offset=off,
    )
    # 裁列表响应死字段（对抗审计：started_at/ended_at/human_user_id/human_seat/
    # likes_count/views_count/owner_id 列表不消费；
    # 不动 winner/reason/match_type/contest_id——BotDetail/Home/admin 有消费者，删了致回归）。
    _MATCH_LIST_DEAD = ("started_at", "ended_at", "human_user_id", "human_seat",
                        "likes_count", "views_count", "owner_id")
    for m in rows:
        for k in _MATCH_LIST_DEAD:
            m.pop(k, None)
    total = store.count_matches(
        status=status,
        game_id=game_id,
        has_technical_incidents=has_technical_incidents,
    )
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
    replay = store.get_public_replay(match_id) or {}
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
                if _is_terminal_stream_event(ev):
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
    # 响应白名单投影：只返回前端 Leaderboard.tsx 消费的字段（裁死字段 vol/last_played_at/
    # is_builtin/owner_display；留 tier_level——test_tiers 断言）。
    items = result["items"] if isinstance(result, dict) else result
    keep = {
        "bot_id", "rating", "rd", "wins", "losses", "draws", "net_chips",
        "matches_played", "bot_name", "bot_display", "format", "os", "arch",
        "game_id", "owner_name", "rating_delta", "tier_level", "tier_key", "tier_name",
    }
    proj = [{k: row[k] for k in keep if k in row} for row in items]
    if isinstance(result, dict):
        return {"leaderboard": proj, "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"leaderboard": proj}


@router.get("/api/tiers")
def tiers(game_id: str):
    """段位定义（公开，前端镜像校验用）。

    game_id 是必填维度；缺失或未知都明确拒绝，不得伪装成另一款游戏的段位。
    """
    from bzplat.backend.games import registry as _game_registry
    gid = game_id.strip().lower()
    try:
        return {"tiers": _game_registry.all_tiers(gid), "game_id": gid}
    except KeyError as exc:
        raise HTTPException(400, f"未知游戏: {gid!r}") from exc


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
    model_config = {"extra": "forbid"}

    title: str
    description: str = ""
    template_id: str | None = None
    game_id: str | None = None
    stages: list[dict[str, Any]] | None = None
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


def _template_for_api(template: dict[str, Any]) -> dict[str, Any]:
    """Keep template match_config as storage-only metadata, never an editable API field."""
    public = dict(template)
    public.pop("match_config", None)
    return public


def _contest_for_api(contest: dict[str, Any]) -> dict[str, Any]:
    """仅输出现行赛事契约；数据库迁移列不进入 REST 响应。"""
    public = dict(contest)
    public.pop("hands_per_match", None)
    public.pop("match_config_json", None)
    return public


@router.get("/api/contests/templates")
def contest_templates(request: Request, game: str | None = None):
    # 从 contest_templates 表读（与 admin 同源，含覆盖；修复原读代码默认的不一致）
    return {
        "templates": [
            _template_for_api(t)
            for t in _store(request).list_contest_templates(game_id=game)
        ]
    }


# 未发布/已取消赛事仅该赛事组织者与管理员可见。使用 schema 常量，避免
# API 层另造一套状态字面量；显式 ``?status=draft`` 也不得绕过可见性。
_CONTEST_HIDDEN_STATUSES = (CONTEST_DRAFT, CONTEST_CANCELLED)


def _can_view_hidden_contest(contest: dict, user: dict | None) -> bool:
    return bool(
        user
        and (
            user.get("role") == ROLE_ADMIN
            or contest.get("organizer_id") == user.get("id")
        )
    )


@router.get("/api/contests")
def list_contests(request: Request, status: str | None = None, game_id: str | None = None,
                  page: int | None = None, per_page: int = 20,
                  user=Depends(optional_user)):
    # admin 全见；组织者额外看到自己的隐藏赛事；其他调用方始终排除隐藏状态。
    # 过滤在 Store 的分页 SQL 内完成，避免 total/页数泄漏或页内裁剪错位。
    is_admin = user is not None and user.get("role") == ROLE_ADMIN
    exclude = None if is_admin else list(_CONTEST_HIDDEN_STATUSES)
    hidden_owner_id = (
        int(user["id"])
        if user is not None and user.get("role") == ROLE_ORGANIZER
        else None
    )
    result = _store(request).list_contests(status=status, game_id=game_id,
                                           page=page, per_page=per_page,
                                           exclude_statuses=exclude,
                                           hidden_owner_id=hidden_owner_id)
    # 裁列表响应死字段（对抗审计：match_config_json/hands_per_match/phase/source_contest_id
    # 列表视图不消费；不动 organizer_id/stages_json/rest_ends_at/current_stage_idx/
    # official_results_ready——共享 list_contests 喂 /api/contests/{id} + 后端内部读取）。
    items = result["items"] if isinstance(result, dict) else result
    for c in items:
        for k in ("match_config_json", "hands_per_match", "phase", "source_contest_id"):
            c.pop(k, None)
    if isinstance(result, dict):
        return {"contests": items, "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"contests": items}


@router.post("/api/contests")
def create_contest(body: ContestCreate, request: Request, user=Depends(require_organizer)):
    _reject_fixed_rule_overrides(body.stages)
    try:
        c = _contests(request).create(
            user["id"],
            body.title,
            description=body.description,
            template_id=body.template_id,
            game_id=body.game_id,
            stages=body.stages,
            phase=body.phase,
            source_contest_id=body.source_contest_id,
            require_real_name=int(body.require_real_name),
            registration_opens_at=body.registration_opens_at,
            registration_closes_at=body.registration_closes_at,
            starts_at=body.starts_at,
        )
    except ValueError as e:
        audit_log(
            request, "contest_create", result="fail",
            user=user.get("username"), target=body.title, detail=str(e),
        )
        raise HTTPException(400, str(e))
    audit_log(
        request, "contest_create", result="ok",
        user=user.get("username"), target=c["id"],
        detail=(
            f"title={body.title}; opens={c.get('registration_opens_at')}; "
            f"closes={c.get('registration_closes_at')}; starts={c.get('starts_at')}"
        ),
    )
    return {"contest": _contest_for_api(c)}


@router.get("/api/contests/{contest_id}")
def contest_detail(
    contest_id: int, request: Request,
    entries_page: int | None = None, entries_per_page: int = 50,
    user=Depends(optional_user),
):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404, "比赛不存在")
    # hidden 赛事仅本赛事 organizer/admin 可见（不是任意 organizer）。
    if (
        c.get("status") in _CONTEST_HIDDEN_STATUSES
        and not _can_view_hidden_contest(c, user)
    ):
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
            is_organizer = (
                c.get("organizer_id") == u.get("id")
                or u.get("role") == ROLE_ADMIN
            )
    except Exception:
        pass
    # 非组织者脱敏实名字段（隐私保护——实名仅组织者用于线下核对/上报）
    if not is_organizer:
        for e in entries:
            for k in ("real_name", "phone", "school", "student_id"):
                e.pop(k, None)
    # 裁 standings/pairings 响应死字段（对抗审计：前端 ContestDetail/BracketTree/
    # ScheduleTable 不消费；表列与内部计算保留——仅从响应 dict 去掉）。
    _STANDINGS_DEAD = ("entry_id", "user_id", "seed", "eliminated")
    for s in standings:
        for k in _STANDINGS_DEAD:
            s.pop(k, None)
    _PAIRING_DEAD = (
        "contest_id", "entry_a_id", "entry_b_id", "bot_a_version_id",
        "bot_b_version_id", "pairing_seed", "published_at", "color_first",
        "owner_a_name", "owner_b_name",
    )
    for p in pairings:
        for k in _PAIRING_DEAD:
            p.pop(k, None)
    # 旧库列仅作历史存储；现行 API 不再暴露可覆盖的规则配置。
    c = _contest_for_api(c)
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
def contest_bracket(
    contest_id: int, request: Request, user=Depends(optional_user)
):
    """对阵图数据；隐藏赛事沿用 detail 的 owner/admin 可见性。"""
    contest = _store(request).get_contest(contest_id)
    if not contest:
        raise HTTPException(404, "比赛不存在")
    if (
        contest.get("status") in _CONTEST_HIDDEN_STATUSES
        and not _can_view_hidden_contest(contest, user)
    ):
        raise HTTPException(404, "比赛不存在")
    return {"pairings": _store(request).contest_bracket(contest_id)}


def _require_contest_organizer(c: dict, user: dict) -> None:
    """校验当前用户是该场赛事组织者或 admin（与 open/start 同权限模型）。"""
    if c.get("organizer_id") != user.get("id") and user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "仅该场赛事组织者或管理员可操作")


@router.post("/api/contests/{contest_id}/entries")
async def organizer_add_entry(
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
    raw_uid = body.get("user_id")
    raw_bid = body.get("bot_id")
    if raw_uid is None or raw_bid is None:
        raise HTTPException(400, "user_id 与 bot_id 均不可为空")
    try:
        uid, bid = int(raw_uid), int(raw_bid)
    except (TypeError, ValueError):
        raise HTTPException(400, "user_id / bot_id 必须是整数")
    try:
        await _contests(request).add_roster_entry(contest_id, uid, bid)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"ok": True}


@router.post("/api/contests/{contest_id}/entries/bulk")
async def organizer_assign_entries(
    contest_id: int, body: AdminAssignEntries, request: Request, user=Depends(require_organizer)
):
    """P5 组织者名单：批量加人（迁移自 admin bulk，assign_all + 显式列表两模式）。"""
    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    from bzplat.backend.games import normalize_game_id
    cgid = normalize_game_id(c.get("game_id"))
    if body.assign_all:
        gid = normalize_game_id(cgid if body.game_id is None else body.game_id)
        if gid != cgid:
            raise HTTPException(400, f"assign_all 的 game_id {gid} 与赛事 {cgid} 不一致")
        bots = store.list_bots(
            active_only=True, runnable_only=True, game_id=gid
        )
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
        try:
            target = [
                (int(e.get("user_id")), int(e.get("bot_id")))
                for e in body.entries or []
            ]
        except (TypeError, ValueError):
            raise HTTPException(400, "user_id / bot_id 必须是整数")
    try:
        return await _contests(request).assign_roster_entries(contest_id, target)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.delete("/api/contests/{contest_id}/entries/{user_id}")
async def organizer_delete_entry(
    contest_id: int, user_id: int, request: Request, user=Depends(require_organizer)
):
    """P5 组织者名单：删人（draft/open 允许）。"""
    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    try:
        deleted = await _contests(request).delete_roster_entry(contest_id, user_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not deleted:
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
async def open_contest(contest_id: int, request: Request, user=Depends(require_organizer)):
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    try:
        contest = await _contests(request).open_registration(contest_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"contest": _contest_for_api(contest)}


@router.post("/api/contests/{contest_id}/register")
async def register_contest(
    contest_id: int, body: ContestRegister, request: Request, user=Depends(require_user)
):
    try:
        entry = await _contests(request).register(
            contest_id, user["id"], body.bot_id, role=user.get("role", "")
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    # 赛事报名经验
    from bzplat.backend.store.schema import XP_CONTEST_PARTICIPATE
    _store(request).award_xp(user["id"], XP_CONTEST_PARTICIPATE)
    return {"entry": entry}


@router.post("/api/contests/{contest_id}/dispatch")
async def dispatch_contest(
    contest_id: int, body: ContestDispatch, request: Request, user=Depends(require_user)
):
    try:
        entry = await _contests(request).dispatch(
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
    return {"contest": _contest_for_api(contest)}


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
    return {"contest": _contest_for_api(contest)}


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
    return {"contest": _contest_for_api(contest)}


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
    return {"contest": _contest_for_api(contest)}


@router.post("/api/contests/{contest_id}/finish")
async def finish_contest(
    contest_id: int, request: Request, user=Depends(require_organizer)
):
    """组织者/admin 强制结束赛事（running/rest → finished；卡住时的手动出口）。"""
    c = _store(request).get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    try:
        contest = await _contests(request).finish(contest_id)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"contest": _contest_for_api(contest)}


# ── admin ─────────────────────────────────────────────────────

@router.get("/api/admin/users")
def admin_users(
    request: Request, q: str | None = None, real_name: bool | None = None,
    page: int | None = None, per_page: int = 50,
    _admin=Depends(require_admin),
):
    result = _store(request).list_users(
        q=q, real_name=real_name, page=page, per_page=per_page,
    )
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
    result = _store(request).delete_user_if_safe(user_id)
    if not result["found"]:
        raise HTTPException(404, "用户不存在")
    if not result["deleted"]:
        raise HTTPException(
            409,
            "用户存在活跃对局/赛事引用或仍是赛事组织者，不能硬删："
            f"{result['blockers']}（请先中止对局并删除或转移其赛事）",
        )
    for bot_id in result["bot_ids"]:
        _bots(request).purge_bot_files(bot_id)
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
async def admin_patch_match(
    match_id: str, body: AdminMatchPatch, request: Request, _admin=Depends(require_admin)
):
    """管理员强制修正对局状态（如中止卡住的对局）。"""
    fields: dict[str, Any] = {}
    if body.status is not None:
        if body.status not in ("pending", "running", "completed", "aborted"):
            raise HTTPException(400, "非法对局状态")
        # 活跃对局的生命周期由 orchestrator/runner 独占。后台若直接把 running
        # 改成 pending/completed，正在运行的 task 仍会继续并再次覆写结果、评分和
        # 赛事回调；pending/running 也不能作为“手工修复”入口伪造。管理员唯一
        # 支持的状态动作是经 abort_match cancel + drain 后中止。
        if body.status != "aborted":
            raise HTTPException(409, "管理员仅可中止对局，不能手工伪造运行或完成状态")
        fields["status"] = body.status
    if body.reason is not None:
        fields["reason"] = body.reason
    if not fields:
        raise HTTPException(400, "无更新字段")
    if body.status == "aborted":
        try:
            match = await _orch(request).abort_match(
                match_id, reason=body.reason or "admin_aborted"
            )
        except ValueError as exc:
            code = 404 if "不存在" in str(exc) else 409
            raise HTTPException(code, str(exc)) from exc
        return {"match": match}
    before = _store(request).get_match(match_id)
    if not before:
        raise HTTPException(404, "对局不存在")
    m = _store(request).update_match(match_id, **fields)
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
    store = _store(request)
    rows = store.list_bots(
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
    # 管理端所有者链接走 /user/:username，不能把数值 owner_id 填进用户名路由。
    enriched = []
    for row in rows:
        owner = store.get_user(row.get("owner_id")) if row.get("owner_id") else None
        enriched.append({
            **_with_bot_runnable(row),
            "owner_name": owner.get("username") if owner else None,
            "owner_display": owner.get("display_name") if owner else None,
        })
    if page is not None:
        return {"bots": enriched, "page": page, "per_page": pp, "total": total}
    return {"bots": enriched}


@router.patch("/api/admin/bots/{bot_id}")
def admin_patch_bot(
    bot_id: int, body: dict, request: Request, _admin=Depends(require_admin)
):
    allowed = {"is_active", "is_builtin", "display_name", "description"}
    unknown = set(body).difference(allowed)
    if unknown:
        raise HTTPException(422, f"不支持的字段：{', '.join(sorted(unknown))}")
    for key in ("is_active", "is_builtin"):
        if key in body and not isinstance(body[key], bool):
            raise HTTPException(422, f"{key} 必须是布尔值")
    fields: dict[str, Any] = {}
    if "is_active" in body:
        fields["is_active"] = 1 if body["is_active"] else 0
    if "is_builtin" in body:
        fields["is_builtin"] = 1 if body["is_builtin"] else 0
    if "display_name" in body:
        fields["display_name"] = str(body["display_name"])[:200]
    if "description" in body:
        fields["description"] = str(body["description"])[:2000]
    if not fields:
        raise HTTPException(400, "无可更新字段")
    try:
        bot = _bots(request).patch_admin(bot_id, **fields)
    except BotError as exc:
        status = 404 if exc.code == "not_found" else 409 if exc.code in {
            "unsupported_binary", "version_unavailable",
        } else 400
        raise HTTPException(
            status, detail={"code": exc.code, "message": exc.message}
        ) from exc
    return {"bot": _with_bot_runnable(bot)}


@router.delete("/api/admin/bots/{bot_id}")
def admin_delete_bot(bot_id: int, request: Request, admin=Depends(require_admin)):
    store = _store(request)
    # 业务规则：硬删前检查活跃引用。bots 表 FK 是 ON DELETE SET NULL（matches 与
    # contest_pairings/entries 均为 SET NULL，保历史）。硬删正在打(pending/running)对局或
    # 进行中赛事(published/running/rest)报名的 bot 会：①让运行中对局 bot_id 变 NULL→
    # _apply_ratings(None) 崩；②进行中赛事对阵/报名的 bot_id 变 NULL→对阵表残缺。
    # 此时应改用停用（is_active=0，用户路径）。
    result = store.delete_bot_if_safe(bot_id)
    if not result["found"]:
        raise HTTPException(404, "bot 不存在")
    refs = result["references"]
    if not result["deleted"]:
        raise HTTPException(
            409,
            f"bot 存在活跃引用，不能硬删：{refs}（进行中对局/赛事；请改用停用 is_active=0）",
        )
    # 硬删 bot 后清理磁盘文件（bot_uploads/<id>/），避免孤儿
    _bots(request).purge_bot_files(bot_id)
    audit_log(request, "admin_delete_bot", result="ok", user=admin.get("username"), target=bot_id)
    return {"ok": True}


@router.get("/api/admin/bots/{bot_id}/versions")
def admin_bot_versions(
    bot_id: int, request: Request, _admin=Depends(require_admin)
):
    if not _store(request).get_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    return {
        "versions": [
            _with_bot_runnable(version)
            for version in _store(request).list_bot_versions(bot_id)
        ]
    }


# ── admin: stats / dashboard ──────────────────────────────────

@router.get("/api/admin/stats")
def admin_stats(request: Request, _admin=Depends(require_admin)):
    return _store(request).count_stats()


# ── admin: contests ───────────────────────────────────────────

class AdminContestPatch(BaseModel):
    model_config = {"extra": "forbid"}

    status: str | None = None
    title: str | None = None
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
        return {"contests": [_contest_for_api(c) for c in result["items"]], "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"contests": [_contest_for_api(c) for c in result]}


@router.patch("/api/admin/contests/{contest_id}")
async def admin_patch_contest(
    contest_id: int, body: AdminContestPatch, request: Request, admin=Depends(require_admin)
):
    fields: dict[str, Any] = {}
    if body.title is not None:
        fields["title"] = body.title
    # 时间编排字段（admin 可改）
    for tk in ("registration_opens_at", "registration_closes_at", "starts_at"):
        # ``null`` 显式清空可选时间；未提交的字段由 Store 合并旧值后校验。
        if tk in body.model_fields_set:
            fields[tk] = getattr(body, tk)

    if body.status is not None:
        if body.status not in {
            CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED, CONTEST_RUNNING,
            CONTEST_REST, CONTEST_FINISHED, CONTEST_CANCELLED,
        }:
            audit_log(
                request, "admin_patch_contest_status", result="fail",
                user=admin.get("username"), target=contest_id,
                detail=f"非法比赛状态: {body.status}",
            )
            raise HTTPException(400, "非法比赛状态")
        if fields:
            audit_log(
                request, "admin_patch_contest_status", result="fail",
                user=admin.get("username"), target=contest_id,
                detail="状态推进不能与其他字段修改同时提交",
            )
            raise HTTPException(400, "状态推进不能与其他字段修改同时提交")

        store = _store(request)
        before = store.get_contest(contest_id)
        if not before:
            audit_log(
                request, "admin_patch_contest_status", result="fail",
                user=admin.get("username"), target=contest_id,
                detail="比赛不存在",
            )
            raise HTTPException(404, "比赛不存在")
        old_status = before["status"]
        target = body.status
        try:
            if target == old_status:
                contest = before
            elif target == CONTEST_OPEN and old_status == CONTEST_DRAFT:
                contest = await _contests(request).open_registration(contest_id)
            elif target == CONTEST_PUBLISHED and old_status in (CONTEST_DRAFT, CONTEST_OPEN):
                contest = await _contests(request).publish(contest_id)
            elif target == CONTEST_RUNNING and old_status in (
                CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED,
            ):
                contest = await _contests(request).start(contest_id)
            elif target == CONTEST_FINISHED and old_status in (CONTEST_RUNNING, CONTEST_REST):
                contest = await _contests(request).finish(contest_id)
            elif target == CONTEST_CANCELLED and old_status in (
                CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED,
            ):
                contest = await _contests(request).cancel(contest_id)
            else:
                raise ValueError(f"不支持赛事从 {old_status} 推进到 {target}")
        except ValueError as exc:
            audit_log(
                request, "admin_patch_contest_status", result="fail",
                user=admin.get("username"), target=contest_id, detail=str(exc),
            )
            raise HTTPException(400, str(exc)) from exc

        if target != old_status:
            audit_log(
                request, "admin_patch_contest_status", result="ok",
                user=admin.get("username"), target=contest_id,
                detail=f"status={old_status}->{target}",
            )
        return {"contest": _contest_for_api(contest)}

    if not fields:
        raise HTTPException(400, "无更新字段")
    try:
        c = _store(request).update_contest(contest_id, **fields)
    except ValueError as e:
        audit_log(
            request, "admin_patch_contest_fields", result="fail",
            user=admin.get("username"), target=contest_id, detail=str(e),
        )
        raise HTTPException(400, str(e))
    if not c:
        audit_log(
            request, "admin_patch_contest_fields", result="fail",
            user=admin.get("username"), target=contest_id,
            detail="比赛不存在",
        )
        raise HTTPException(404, "比赛不存在")
    audit_log(
        request, "admin_patch_contest_fields", result="ok",
        user=admin.get("username"), target=contest_id,
        detail="; ".join(
            f"{key}={c.get(key)}" if key != "title" else "title=changed"
            for key in sorted(fields)
        ),
    )
    return {"contest": _contest_for_api(c)}


@router.delete("/api/admin/contests/{contest_id}")
async def admin_delete_contest(contest_id: int, request: Request, admin=Depends(require_admin)):
    before = _store(request).get_contest(contest_id)
    try:
        deleted = await _contests(request).delete(contest_id)
    except ValueError as exc:
        audit_log(
            request, "admin_delete_contest", result="fail",
            user=admin.get("username"), target=contest_id, detail=str(exc),
        )
        raise HTTPException(409, str(exc)) from exc
    if not deleted:
        audit_log(
            request, "admin_delete_contest", result="fail",
            user=admin.get("username"), target=contest_id,
            detail="比赛不存在",
        )
        raise HTTPException(404, "比赛不存在")
    previous_status = (before or {}).get("status") or "unknown"
    mode = (
        "cancel_published_schedule_then_delete"
        if previous_status == CONTEST_PUBLISHED else "delete_prestart"
    )
    audit_log(
        request, "admin_delete_contest", result="ok",
        user=admin.get("username"), target=contest_id,
        detail=f"previous_status={previous_status}; mode={mode}",
    )
    return {"ok": True}


@router.get("/api/admin/contests/{contest_id}/entries")
def admin_contest_entries(
    contest_id: int, request: Request, _admin=Depends(require_admin)
):
    return {"entries": _store(request).contest_entries_named(contest_id)}


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
async def admin_assign_entries(
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
        gid = normalize_game_id(cgid if body.game_id is None else body.game_id)
        if gid != cgid:
            raise HTTPException(400, f"assign_all 的 game_id {gid} 与赛事 {cgid} 不一致")
        bots = store.list_bots(
            active_only=True, runnable_only=True, game_id=gid
        )
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
        try:
            for e in body.entries or []:
                uid = int(e.get("user_id"))
                bid = int(e.get("bot_id"))
                target.append((uid, bid))
        except (TypeError, ValueError):
            raise HTTPException(400, "user_id / bot_id 必须是整数")

    try:
        result = await _contests(request).assign_roster_entries(contest_id, target)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit_log(request, "admin_assign_entries", result="ok", target=contest_id,
              detail=f"added={result['added']} skipped={len(result['skipped'])}")
    return result


@router.delete("/api/admin/contests/{contest_id}/entries/{user_id}")
async def admin_delete_entry(
    contest_id: int, user_id: int, request: Request, admin=Depends(require_admin)
):
    if not _store(request).get_contest(contest_id):
        audit_log(
            request, "admin_delete_contest_entry", result="fail",
            user=admin.get("username"), target=contest_id,
            detail=f"user_id={user_id}; reason=比赛不存在",
        )
        raise HTTPException(404, "比赛不存在")
    try:
        deleted = await _contests(request).delete_roster_entry(contest_id, user_id)
    except ValueError as exc:
        audit_log(
            request, "admin_delete_contest_entry", result="fail",
            user=admin.get("username"), target=contest_id,
            detail=f"user_id={user_id}; reason={exc}",
        )
        raise HTTPException(400, str(exc)) from exc
    if not deleted:
        audit_log(
            request, "admin_delete_contest_entry", result="fail",
            user=admin.get("username"), target=contest_id,
            detail=f"user_id={user_id}; reason=报名记录不存在",
        )
        raise HTTPException(404, "报名记录不存在")
    audit_log(
        request, "admin_delete_contest_entry", result="ok",
        user=admin.get("username"), target=contest_id,
        detail=f"user_id={user_id}",
    )
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


def _validated_runtime_patch(
    body: RuntimeSettingsPatch, *, ceiling: int
) -> tuple[dict[str, str], dict[str, Any]]:
    """Validate the complete request without persistence or hot side effects."""
    settings: dict[str, str] = {}
    updated: dict[str, Any] = {}

    def add(key: str, response_key: str, value: Any, stored: str | None = None) -> None:
        settings[key] = str(value) if stored is None else stored
        updated[response_key] = value

    if body.bot_cpus is not None or body.bot_memory_mb is not None:
        raise ValueError("bot_cpus / bot_memory_mb 为只读硬限制，不可修改")

    if body.max_concurrent_matches is not None:
        value = int(body.max_concurrent_matches)
        if value > ceiling:
            raise ValueError(
                f"max_concurrent_matches={value} 超过半负载硬顶 ceiling={ceiling}"
                f"（机器 {cpu_count()} 核）"
            )
        if value < 1:
            raise ValueError("max_concurrent_matches 至少为 1")
        add(SETTING_MAX_CONCURRENT, "max_concurrent_matches", value)

    if body.action_timeout_sec is not None:
        value = float(body.action_timeout_sec)
        if (
            not math.isfinite(value)
            or value < ACTION_TIMEOUT_MIN
            or value > ACTION_TIMEOUT_MAX
        ):
            raise ValueError(
                f"action_timeout_sec 须在 {ACTION_TIMEOUT_MIN}–{ACTION_TIMEOUT_MAX}"
            )
        add(SETTING_ACTION_TIMEOUT, "action_timeout_sec", value)

    if body.contest_default_rest_minutes is not None:
        value = int(body.contest_default_rest_minutes)
        if value < 0 or value > 120:
            raise ValueError("contest_default_rest_minutes 须在 0–120")
        add(SETTING_CONTEST_REST, "contest_default_rest_minutes", value)

    if body.auto_match_enabled is not None:
        add(
            SETTING_AUTO_MATCH_ENABLED,
            "auto_match_enabled",
            body.auto_match_enabled,
            "1" if body.auto_match_enabled else "0",
        )
    integer_fields = (
        ("auto_match_interval_sec", SETTING_AUTO_MATCH_INTERVAL_SEC, 1, 3600,
         "auto_match_interval_sec 须在 1–3600"),
        ("auto_match_min_idle_sec", SETTING_AUTO_MATCH_MIN_IDLE_SEC, 0, 600,
         "auto_match_min_idle_sec 须在 0–600"),
        ("auto_match_bot_cooldown", SETTING_AUTO_MATCH_BOT_COOLDOWN, 0, 86400,
         "auto_match_bot_cooldown 须在 0–86400"),
        ("auto_match_stale_sec", SETTING_AUTO_MATCH_STALE_SEC, 0, 604800,
         "auto_match_stale_sec 须在 0–604800（0=不限）"),
        ("auto_match_reserve_slots", SETTING_AUTO_MATCH_RESERVE_SLOTS, 0, ceiling,
         "auto_match_reserve_slots 须在 0–ceiling"),
        ("auto_match_placement_games", SETTING_AUTO_MATCH_PLACEMENT_GAMES, 0, 100,
         "auto_match_placement_games 须在 0–100（0=禁用）"),
        ("auto_match_max_per_round", SETTING_AUTO_MATCH_MAX_PER_ROUND, 1, 50,
         "auto_match_max_per_round 须在 1–50"),
        ("auto_match_daily_cap", SETTING_AUTO_MATCH_DAILY_CAP, 0, 100000,
         "auto_match_daily_cap 须在 0–100000（0=不限）"),
    )
    for field, setting_key, minimum, maximum, error in integer_fields:
        raw = getattr(body, field)
        if raw is None:
            continue
        value = int(raw)
        if value < minimum or value > maximum:
            raise ValueError(error)
        add(setting_key, field, value)

    if not updated:
        raise ValueError("无更新字段")
    return settings, updated


@router.patch("/api/admin/settings/runtime")
def admin_patch_runtime(
    body: RuntimeSettingsPatch, request: Request, admin=Depends(require_admin)
):
    store = _store(request)
    orch = _orch(request)
    try:
        settings, updated = _validated_runtime_patch(
            body, ceiling=concurrent_ceiling()
        )
    except ValueError as exc:
        audit_log(
            request, "admin_patch_runtime", result="fail",
            user=admin.get("username"), target="runtime", detail=str(exc),
        )
        raise HTTPException(400, str(exc)) from exc

    # All validation completed.  Persist the batch atomically; only after the
    # transaction commits may process-local hot state observe the new values.
    try:
        store.set_settings(settings)
    except Exception as exc:
        audit_log(
            request, "admin_patch_runtime", result="fail",
            user=admin.get("username"), target="runtime",
            detail=f"write_failed={type(exc).__name__}",
        )
        raise HTTPException(500, "运行时配置保存失败") from exc

    try:
        if "max_concurrent_matches" in updated:
            orch.rebuild_concurrency(updated["max_concurrent_matches"])
        if "action_timeout_sec" in updated:
            orch.set_action_timeout(updated["action_timeout_sec"])
    except Exception as exc:
        audit_log(
            request, "admin_patch_runtime", result="fail",
            user=admin.get("username"), target="runtime",
            detail=(
                f"committed={','.join(sorted(updated))}; "
                f"hot_reload_failed={type(exc).__name__}"
            ),
        )
        raise HTTPException(500, "运行时配置已保存，但热更新失败；请重启服务") from exc

    audit_log(
        request, "admin_patch_runtime", result="ok",
        user=admin.get("username"), target="runtime",
        detail="; ".join(f"{key}={value}" for key, value in updated.items()),
    )
    return {"updated": updated, "runtime": admin_get_runtime(request, admin)}


# ── 公开裁判引擎（只读） ──────────────────────────────
# 现行规则只读：公开裁判元数据仅从游戏注册表派生。
JUDGE_GAMES: list[dict[str, Any]] = game_registry.judge_games()


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
                "shared_source_files": g["shared_source_files"],
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
        spec = game_registry.get(game_id)
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
    shared_dir = backend_dir / "games"
    for rel in spec.shared_source_files:
        path = shared_dir / rel
        if not path.is_file():
            continue
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


# ── admin: 赛制模板 CRUD ──────────────────────────────────────
class TemplateBody(BaseModel):
    model_config = {"extra": "forbid"}

    id: str
    name: str
    game_id: str
    stages: list[dict[str, Any]]


class TemplatePreviewBody(BaseModel):
    model_config = {"extra": "forbid"}

    stages: list[dict[str, Any]]
    n: int = 8
    game_id: str = "holdem"


@router.get("/api/admin/templates")
def admin_list_templates(
    request: Request, game: str | None = None, _admin=Depends(require_admin)
):
    return {
        "templates": [
            _template_for_api(t)
            for t in _store(request).list_contest_templates(game_id=game)
        ]
    }


@router.post("/api/admin/templates")
def admin_create_template(
    body: TemplateBody, request: Request, _admin=Depends(require_admin)
):
    store = _store(request)
    if store.get_contest_template(body.id) is not None:
        raise HTTPException(409, f"模板 id 已存在：{body.id}")
    _reject_fixed_rule_overrides(body.stages)
    try:
        norm = validate_template(body.id, body.name, body.game_id, {}, body.stages)
    except ValueError as e:
        raise HTTPException(400, str(e))
    t = store.upsert_contest_template(
        norm["id"], name=norm["name"], game_id=norm["game_id"],
        match_config=norm["match_config"], stages=norm["stages"], is_builtin=False,
    )
    return {"template": _template_for_api(t)}


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
    _reject_fixed_rule_overrides(body.stages)
    try:
        norm = validate_template(body.id, body.name, body.game_id, {}, body.stages)
    except ValueError as e:
        raise HTTPException(400, str(e))
    t = store.upsert_contest_template(
        norm["id"], name=norm["name"], game_id=norm["game_id"],
        match_config=norm["match_config"], stages=norm["stages"],
        is_builtin=bool(existing.get("is_builtin")),
    )
    return {"template": _template_for_api(t)}


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
    _reject_fixed_rule_overrides(body.stages)
    try:
        norm_stages = [validate_stage(s, i, body.game_id) for i, s in enumerate(body.stages)]
    except ValueError as e:
        raise HTTPException(400, str(e))
    n = max(0, int(body.n))
    per = [estimate_match_count(st, n) for st in norm_stages]
    return {"per_stage": per, "total": sum(per), "n": n}


# ── admin: 日志查看 ────────────────────────────────────────────
_LOG_HEADER_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}(?:,\d{3})? "
    r"(?P<level>[A-Z]+) \[[^\]]+\] "
)


def _group_log_records(raw_lines: list[str]) -> list[tuple[str | None, list[str]]]:
    """把 logging 的异常续行归入其结构化首行。

    ``logging`` 只会给一条记录的首行加时间/级别/模块前缀；traceback、Bot
    stderr 等多行消息的后续行没有此前缀。筛选必须作用于整条记录，否则按
    ERROR/关键字过滤时会丢掉最有诊断价值的堆栈和对局上下文。
    """
    records: list[tuple[str | None, list[str]]] = []
    for raw in raw_lines:
        line = raw.rstrip("\r\n")
        header = _LOG_HEADER_RE.match(line)
        if header:
            records.append((header.group("level"), [line]))
        elif records and records[-1][0] is not None:
            records[-1][1].append(line)
        else:
            # 兼容旧日志/截断尾部首行：没有可归属首行时作为独立记录返回。
            records.append((None, [line]))
    return records


@router.get("/api/admin/logs")
def admin_logs(
    request: Request,
    level: str | None = None,
    q: str | None = None,
    limit: int = 300,
    file: str = "app",
    _admin=Depends(require_admin),
):
    """读当前 logs/{app,access,audit}.log 的有界尾部，按记录过滤。

    file: app（业务/系统）、access（HTTP 访问，含真实 IP）、audit（安全审计）。
    多行异常按结构化首行分组，命中筛选时返回完整 traceback；``limit`` 是
    期望的最大物理行数，但单条完整记录不会被截断。
    """
    # 白名单：只允许读这三个日志文件，防路径穿越
    allowed = {"app": "app.log", "access": "access.log", "audit": "audit.log"}
    fname = allowed.get(file, "app.log")
    log_path = Path(os.environ.get("BZ_LOG_DIR", "logs")) / fname
    lines: list[str] = []
    if log_path.is_file():
        # 只读当前轮转文件末尾最多 8000 行；历史轮转文件分页另行处理。
        with log_path.open("r", encoding="utf-8", errors="replace") as fh:
            tail = fh.readlines()[-8000:]
        level_upper = level.upper() if level else None
        kw = (q or "").lower()
        matched: list[list[str]] = []
        for record_level, record_lines in _group_log_records(tail):
            if level_upper and record_level != level_upper:
                continue
            if kw and not any(kw in ln.lower() for ln in record_lines):
                continue
            matched.append(record_lines)

        # 从最新记录向前取，达到行数上限后停止；不切断一条多行异常记录。
        bounded_limit = max(1, min(limit, 2000))
        selected: list[list[str]] = []
        selected_line_count = 0
        for record_lines in reversed(matched):
            selected.append(record_lines)
            selected_line_count += len(record_lines)
            if selected_line_count >= bounded_limit:
                break
        for record_lines in reversed(selected):
            lines.extend(record_lines)
    # 不向浏览器泄漏服务端绝对路径；source 是白名单中的安全文件名。
    return {"lines": lines, "source": fname}


# ── wiki ──────────────────────────────────────────────────────
# 站内 Wiki：多页索引 + 按 slug 取正文。wiki/ 目录下每个 .md 一页，
# slug 为文件名（去 .md）。索引按固定顺序排列，缺失文件自动跳过。
# 精简为 7 页（核心 = 3 游戏；功能说明统一进 GUIDE）。
WIKI_PAGES: list[dict[str, str]] = [
    {"slug": "index", "file": "INDEX.md", "title": "Wiki 首页", "summary": "玩家快速上手、协议与游戏规则导航"},
    {"slug": "protocol", "file": "PROTOCOL.md", "title": "协议规范", "summary": "请求/响应信封、两种运行模式与动作编码"},
    {"slug": "bot-dev", "file": "BOT_DEV.md", "title": "Bot 开发指南", "summary": "从零编写一个 Bot：样例、编译、上传、调试"},
    {"slug": "texas", "file": "TEXAS.md", "title": "德州扑克 (TexasHoldem2p)", "summary": "固定 70 手规则、请求字段与完整示例"},
    {"slug": "gomoku", "file": "GOMOKU.md", "title": "五子棋 (Gomoku)", "summary": "15×15 规则、协议与 C/Python 示例"},
    {"slug": "pencil", "file": "PENCIL.md", "title": "点格棋 (Pencil)", "summary": "N=6 规则、900 秒棋钟、协议与示例"},
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
