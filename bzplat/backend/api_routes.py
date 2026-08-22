"""Bots / Matches / Contests / Admin / Leaderboard API 路由。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import time
from collections.abc import AsyncIterator, Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Literal, TypeVar

import anyio
from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field, StrictBool
from starlette.datastructures import FormData, UploadFile

from bzplat.backend.auth.auth_manager import COOKIE_NAME
from bzplat.backend.auth.dependencies import (
    _extract_token,
    optional_user,
    require_admin,
    require_organizer,
    require_user,
)
from bzplat.backend.security import (
    BOT_UPLOAD_BODY_MAX_BYTES,
    audit_log,
    client_ip,
    websocket_origin_allowed,
)
from bzplat.backend.bots import BotError, BotManager
from bzplat.backend.bots.manager import MAX_BYTES
from bzplat.backend.runtime.limits import (
    MAX_BOT_RESPONSE_LINE_BYTES,
    MAX_LOCAL_AI_WEBSOCKET_MESSAGE_BYTES,
)
from bzplat.backend.runtime.local_ai import (
    LocalAIConnectionError,
    LocalAIResponseRejected,
)
from bzplat.backend.runtime.local_ai_service import (
    LocalAIAgentBusyError,
    LocalAIRateLimitError,
)

logger = logging.getLogger(__name__)
_LOCAL_AI_DB_TOUCH_INTERVAL_SECONDS = 15.0
_LOCAL_AI_INBOUND_BURST = 20.0
_LOCAL_AI_INBOUND_REFILL_PER_SECOND = 5.0


async def _deny_local_ai_websocket(websocket: WebSocket) -> None:
    """Reject a connector during HTTP upgrade without exposing credentials."""

    # Keep the rejection on the baseline ASGI WebSocket contract.  Uvicorn's
    # SansIO backend can emit duplicate HTTP entity headers and leave its
    # handshake state incomplete after ``websocket.http.response`` denial
    # responses.  A close before accept is mapped to a valid HTTP 403 by every
    # supported Uvicorn backend and never upgrades an unauthenticated peer.
    await websocket.close(code=1008, reason="invalid_credentials")
_DEBUG_NO_STORE_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "Vary": "Authorization, Cookie",
}
_ADMIN_PRIVATE_HEADERS = {
    "Cache-Control": "private, no-store, max-age=0",
    "Pragma": "no-cache",
    "Vary": "Authorization, Cookie",
    "Referrer-Policy": "no-referrer",
}
_CONTEST_IDENTITY_PRIVATE_HEADERS = {
    **_ADMIN_PRIVATE_HEADERS,
    "X-Content-Type-Options": "nosniff",
}
from bzplat.backend.contests import ContestManager
from bzplat.backend.contests.ranking import with_official_result_provenance
from bzplat.backend.contests.presentation import build_stage_summaries
from bzplat.backend.contests.showcase import (
    ShowcaseReadOnlyError,
    public_description as contest_public_description,
    require_mutable as require_mutable_contest,
    template_name as contest_template_name,
)
from bzplat.backend.contests.templates import list_templates
from bzplat.backend.games import registry as game_registry
from bzplat.backend.games.base import MatchRecordExportError
from bzplat.backend.matches import MatchOrchestrator
from bzplat.backend.runtime.config import (
    ACTION_TIMEOUT_SEC,
    BOT_UPLOAD_ADMISSION_WAIT_SEC,
    CONFIGURATION_SOURCE,
    CONTEST_SCHEDULER_CONFIG,
    FULL_RR_MAX_N,
    RANKING_MIN_RATED_MATCHES,
)
from bzplat.backend.runtime.limits import (
    BOT_CPUS,
    BOT_MEMORY_MB,
    concurrent_ceiling,
    cpu_count,
)
from bzplat.backend.runtime.binary_runner import PlatformRunnerError
from bzplat.backend.store import (
    ContestRealNameRosterForbidden,
    ContestRosterWriteValidationError,
)
from bzplat.backend.store.execution import (
    ExecutionIdempotencyConflict,
    ExecutionMaintenanceConflict,
    ExecutionQueueClosed,
)
from bzplat.backend.store.schema import (
    COMMENT_TARGET_TYPES,
    CONTEST_CANCELLED,
    CONTEST_DRAFT,
    CONTEST_FINISHED,
    CONTEST_IDENTITY_SOURCE_LEGACY,
    CONTEST_IDENTITY_SOURCE_REGISTRATION,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    CONTEST_REST,
    CONTEST_RUNNING,
    DEFAULT_RUNTIME_MODE,
    EXECUTION_SOURCE_HUMAN,
    EXECUTION_SOURCE_MANUAL,
    EXECUTION_ENV_PLATFORM_LOW,
    EXECUTION_ENV_REMOTE_LOCAL,
    LIKE_TARGET_TYPES,
    ROLE_ADMIN,
    ROLE_ORGANIZER,
    SUPPORTED_BINARY_ERROR,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    is_supported_binary_metadata,
)
from bzplat.backend.store.public_contract import sanitize_public_match
from bzplat.backend.store.validation import is_authoritative_no_opponent_pairing
router = APIRouter()
_T = TypeVar("_T")
_BINARY_FILE_SCHEMA = {"type": "string", "format": "binary"}
_BOT_UPLOAD_BODY_MAX_MIB = BOT_UPLOAD_BODY_MAX_BYTES // (1024 * 1024)
_BOT_CREATE_UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["name", "file"],
                    "properties": {
                        "name": {"type": "string"},
                        "display_name": {"type": "string", "default": ""},
                        "description": {"type": "string", "default": ""},
                        "upload_note": {"type": "string", "default": ""},
                        "game_id": {"type": "string", "default": "holdem"},
                        "runtime_mode": {
                            "type": "string",
                            "default": DEFAULT_RUNTIME_MODE,
                        },
                        "file": _BINARY_FILE_SCHEMA,
                    },
                }
            }
        },
    },
    "responses": {
        "400": {"description": "Bot 文件或参数无效，或预检失败"},
        "401": {"description": "未登录或会话过期"},
        "413": {
            "description": f"multipart 请求体超过 {_BOT_UPLOAD_BODY_MAX_MIB} MiB"
        },
        "503": {"description": "上传槽繁忙或沙箱暂不可用"},
    },
}
_BOT_VERSION_UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "upload_note": {"type": "string", "default": ""},
                        "runtime_mode": {"type": "string", "default": ""},
                        "file": _BINARY_FILE_SCHEMA,
                    },
                }
            }
        },
    },
    "responses": {
        "400": {"description": "Bot 文件或参数无效，或预检失败"},
        "401": {"description": "未登录或会话过期"},
        "413": {
            "description": f"multipart 请求体超过 {_BOT_UPLOAD_BODY_MAX_MIB} MiB"
        },
        "503": {"description": "上传槽繁忙或沙箱暂不可用"},
    },
}


class _BotUploadBusy(Exception):
    """The one global upload/preflight lane did not open in time."""


class _DeploymentMaintenance(Exception):
    """Deployment drain closed upload admission before multipart parsing."""


async def _finish_upload_step_before_cancel(awaitable: Awaitable[_T]) -> _T:
    """Keep admission held until a started file/thread operation really ends.

    Cancelling ``asyncio.to_thread`` only cancels its asyncio wrapper; the worker
    keeps running.  Releasing the global upload permit at that point would allow
    another request to retain/write a second payload while the first preflight is
    still active.  Shield and drain the task, then propagate cancellation.
    """

    task = asyncio.ensure_future(awaitable)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        if task.cancelled():
            raise
        # Starlette/AnyIO cancellation scopes are level-triggered: merely
        # catching CancelledError and awaiting again can spin until the worker
        # ends, starving the event loop and defeating bounded admission waits.
        # Shield this short drain so other requests keep making progress while
        # the already-started file/thread operation converges.
        with anyio.CancelScope(shield=True):
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    if task.cancelled():
                        raise
                except BaseException:
                    break
            try:
                task.result()
            except BaseException:
                pass
        raise
    except BaseException:
        return task.result()


@asynccontextmanager
async def _bot_upload_admission(request: Request) -> AsyncIterator[None]:
    """Acquire the process-wide upload lane with bounded, cancel-safe waiting."""

    gate = getattr(request.app.state, "bot_upload_gate", None)
    activity_lock = getattr(
        request.app.state, "bot_upload_activity_lock", None
    )
    activity = getattr(request.app.state, "bot_upload_activity", None)
    if gate is None or activity_lock is None or activity is None:
        # Partial assembly cannot prove that a deployment drain sees every
        # body/preflight already in progress.  Fail closed instead of exposing
        # a green maintenance-ready state with an uncounted upload.
        raise _BotUploadBusy
    acquired = False
    counted = False
    try:
        async with activity_lock:
            if _store(request).executions.is_maintenance_control(
                _store(request).executions.control()
            ):
                raise _DeploymentMaintenance
        try:
            await asyncio.wait_for(
                gate.acquire(), timeout=BOT_UPLOAD_ADMISSION_WAIT_SEC
            )
        except TimeoutError:
            raise _BotUploadBusy
        acquired = True
        async with activity_lock:
            # Recheck after waiting for the lane: begin-maintenance shares
            # this mutex, so no upload can cross its durable boundary.
            if _store(request).executions.is_maintenance_control(
                _store(request).executions.control()
            ):
                raise _DeploymentMaintenance
            activity["active"] = int(activity.get("active") or 0) + 1
            counted = True
        yield
    finally:
        try:
            if counted:
                # AnyIO cancellation is level-triggered.  Once the upload body
                # has propagated cancellation, a second cancellation while
                # waiting for this mutex must not leak the active counter or
                # leave the global upload permit held forever.
                with anyio.CancelScope(shield=True):
                    async with activity_lock:
                        activity["active"] = max(
                            0, int(activity.get("active") or 0) - 1
                        )
        finally:
            if acquired:
                gate.release()


def _multipart_text(
    form: FormData,
    key: str,
    *,
    default: str = "",
    required: bool = False,
) -> str:
    value = form.get(key)
    if isinstance(value, str):
        return value
    if not required and value is None:
        return default
    raise HTTPException(422, detail=f"multipart 字段 {key} 缺失或类型错误")


def _multipart_file(form: FormData) -> UploadFile:
    value = form.get("file")
    if isinstance(value, UploadFile):
        return value
    raise HTTPException(422, detail="multipart 文件字段 file 缺失或类型错误")


async def _read_bot_upload(file: UploadFile) -> bytes:
    """Read at most the supported limit plus one sentinel byte."""

    raw = await _finish_upload_step_before_cancel(file.read(MAX_BYTES + 1))
    if not raw or len(raw) > MAX_BYTES:
        raise BotError("invalid_size", f"二进制大小须 1..{MAX_BYTES} 字节")
    return raw


def _upload_busy_error() -> HTTPException:
    return HTTPException(
        503,
        detail={
            "code": "upload_busy",
            "message": "Bot 上传预检繁忙，请稍后重试",
        },
        headers={
            "Retry-After": str(max(1, math.ceil(BOT_UPLOAD_ADMISSION_WAIT_SEC)))
        },
    )


def _deployment_maintenance_error() -> HTTPException:
    return HTTPException(
        503,
        detail={
            "code": "deployment_maintenance",
            "message": "平台正在部署维护，暂不接收 Bot 上传",
        },
        headers={"Retry-After": "30"},
    )


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
    if public.get("retired_at") is not None:
        runnable = False
    public["runnable"] = runnable
    public["unsupported_reason"] = (
        None
        if runnable
        else "该版本已退役"
        if public.get("retired_at") is not None
        else SUPPORTED_BINARY_ERROR
    )
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


def _execution_dispatcher(request: Request):
    dispatcher = getattr(request.app.state, "execution_dispatcher", None)
    if dispatcher is None:
        raise HTTPException(503, "执行队列未就绪")
    return dispatcher


def _local_ai(request: Request):
    service = getattr(request.app.state, "local_ai_service", None)
    if service is None:
        raise HTTPException(503, "本地 Bot 连接服务未就绪")
    return service


def _require_social_target(
    store,
    target_type: str,
    target_id: str,
    *,
    allowed_types: frozenset[str],
) -> None:
    """Fail closed for polymorphic comments/likes targets."""
    if target_type not in allowed_types:
        raise HTTPException(400, f"不支持的目标类型: {target_type!r}")
    try:
        exists = store.social_target_exists(target_type, target_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    if not exists:
        raise HTTPException(404, "互动目标不存在")


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

    public_match = sanitize_public_match(m) or m
    human_user = None
    if store is not None and public_match and public_match.get("match_type") == "human" and public_match.get("human_user_id") is not None:
        try:
            human_user = store.get_user(int(public_match["human_user_id"]))
        except Exception:
            human_user = None
    result = with_seat_info(public_match, human_user=human_user) or public_match
    if store is not None and result.get("id"):
        # Eligibility is frozen at creation, but it does not prove the derived
        # rating transaction committed.  Only the exactly-once marker does.
        result["rating_settled"] = bool(
            store.is_match_rating_settled(str(result["id"]))
        )
    return result


_PUBLIC_MATCH_LIST_FIELDS = frozenset(
    {
        "id",
        "game_id",
        "status",
        "winner",
        "reason",
        "match_type",
        "contest_id",
        "created_at",
        "bot_a_id",
        "bot_b_id",
        "bot_a_environment",
        "bot_b_environment",
        "technical_loss",
        "result",
        "bot_a",
        "bot_b",
    }
)
_PUBLIC_MATCH_ENGAGEMENT_FIELDS = frozenset({"likes_count", "views_count"})
_PUBLIC_MATCH_RECORD_FIELDS = frozenset(
    {
        "id",
        "game_id",
        "status",
        "winner",
        "reason",
        "match_type",
        "contest_id",
        "human_seat",
        "created_at",
        "started_at",
        "ended_at",
        "technical_loss",
        "result",
        "bot_a",
        "bot_b",
    }
)
_MATCH_RECORD_CONTRACT_FIELDS = ("ruleset_version", "protocol_version")
_MATCH_RECORD_HEADERS = {
    "Cache-Control": "no-store",
    "X-Content-Type-Options": "nosniff",
}
_MATCH_LOG_FORMAT = "botbattle.match.log"
_MATCH_LOG_FORMAT_VERSION = 1
_SAFE_RECORD_FILENAME_COMPONENT = re.compile(r"[^A-Za-z0-9_-]+")
_MATCH_RECORD_CONTRACT_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,127}\Z")


def _public_match_record_source(match: dict) -> dict[str, Any]:
    """Build the game exporter input from an explicit public allow-list."""
    # ``Store.get_match_record_source`` already joined Bot owners and the
    # optional human identity in the replay snapshot.  Do not perform a second
    # user query here or mix participant labels from another database state.
    public = _with_seat_info(match)
    source = {
        key: value
        for key, value in public.items()
        if key in _PUBLIC_MATCH_RECORD_FIELDS
    }
    # Frozen rule/protocol ids are needed to interpret historical records, but
    # rating pools, Bot version ids/paths and match_config are intentionally not.
    for key in _MATCH_RECORD_CONTRACT_FIELDS:
        value = match.get(key)
        if not isinstance(value, str) or not _MATCH_RECORD_CONTRACT_ID.fullmatch(value):
            raise ValueError(f"invalid frozen match contract: {key}")
        source[key] = value
    return source


def _match_record_filename(game_id: str, match_id: str) -> str:
    """Return an ASCII-only attachment filename with no header metacharacters."""
    safe_game = _SAFE_RECORD_FILENAME_COMPONENT.sub("-", game_id).strip("-_")
    safe_match = _SAFE_RECORD_FILENAME_COMPONENT.sub("-", match_id).strip("-_")
    safe_game = (safe_game or "game")[:32]
    safe_match = (safe_match or "match")[:80]
    return f"botbattle-{safe_game}-{safe_match}.json"


def _match_log_filename(game_id: str, match_id: str) -> str:
    """Return the safe attachment name for one public match log."""
    record_name = _match_record_filename(game_id, match_id)
    return f"{record_name[:-5]}-log.json"


def _public_match_list_rows(
    rows: list[dict], *, keep_engagement: bool = False
) -> list[dict]:
    """Project public list rows through the same participant identity contract.

    ``Store.list_matches`` joins the two Bot owners and the optional human
    player in one bounded query.  This avoids per-row user lookups while making
    History unambiguous: every seat is either one public user-owned Bot or one
    public human identity.
    """
    from bzplat.backend.matches.seat_info import with_seat_info

    projected: list[dict] = []
    for row in rows:
        public = with_seat_info(sanitize_public_match(row) or row) or row
        # 用正向白名单锁定公开列表契约：Store 为技术故障归一携带的
        # _replay_events_json，以及未来新增的执行/关联列，都不能因忘记更新黑名单
        # 而静默泄漏。Bot id 仍是公开详情路由键，但 UI 不得用它当名称兜底。
        allowed = _PUBLIC_MATCH_LIST_FIELDS
        if keep_engagement:
            allowed = allowed | _PUBLIC_MATCH_ENGAGEMENT_FIELDS
        projected.append(
            {key: value for key, value in public.items() if key in allowed}
        )
    return projected


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


class LocalAIAgentCreate(BaseModel):
    model_config = {"extra": "forbid"}

    bot_id: int = Field(gt=0)
    label: str = Field(min_length=2, max_length=32)


def _private_no_store(response: Response) -> None:
    for key, value in _ADMIN_PRIVATE_HEADERS.items():
        response.headers[key] = value


def _owned_local_agent(request: Request, public_id: str, user: dict) -> dict:
    agent = _store(request).get_local_ai_agent_by_public_id(public_id)
    if agent is None or int(agent["owner_id"]) != int(user["id"]):
        raise HTTPException(404, "本地 Bot 连接不存在")
    return agent


@router.get("/api/local-ai/agents")
async def list_local_ai_agents(
    request: Request,
    response: Response,
    user=Depends(require_user),
):
    _private_no_store(response)
    return {"items": await _local_ai(request).list_for_owner(int(user["id"]))}


@router.get("/api/local-ai/client")
async def download_local_ai_client(
    request: Request,
    _user=Depends(require_user),
):
    """Download the credential-free reference connector.

    The script is a reviewed repository asset.  Tokens are deliberately never
    embedded in the response or URL; the client reads ``BZ_LOCAL_AI_TOKEN`` on
    the user's computer.
    """

    path = Path(__file__).resolve().parents[2] / "scripts" / "local_ai_client.py"
    if not path.is_file():  # pragma: no cover - broken deployment artifact
        raise HTTPException(503, "本地 Bot 连接器暂不可下载")
    headers = dict(_ADMIN_PRIVATE_HEADERS)
    headers["X-Content-Type-Options"] = "nosniff"
    return FileResponse(
        path,
        media_type="text/x-python; charset=utf-8",
        filename="local_ai_client.py",
        headers=headers,
    )


@router.post("/api/local-ai/agents", status_code=201)
async def create_local_ai_agent(
    body: LocalAIAgentCreate,
    request: Request,
    response: Response,
    user=Depends(require_user),
):
    _private_no_store(response)
    try:
        agent, token = await _local_ai(request).create(
            owner_id=int(user["id"]),
            bot_id=int(body.bot_id),
            label=body.label,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit_log(
        request,
        "local_ai_create",
        result="ok",
        user=user.get("username"),
        target=agent["public_id"],
        detail=f"bot_id={agent['bot_id']}",
    )
    return {
        "agent": agent,
        "token": token,
        "connection_url": "/api/local-ai/connect",
    }


@router.post("/api/local-ai/agents/{public_id}/rotate")
async def rotate_local_ai_agent(
    public_id: str,
    request: Request,
    response: Response,
    user=Depends(require_user),
):
    _private_no_store(response)
    current = _owned_local_agent(request, public_id, user)
    try:
        result = await _local_ai(request).rotate(
            agent_id=int(current["id"]), owner_id=int(user["id"])
        )
    except LocalAIRateLimitError as exc:
        raise HTTPException(
            429, str(exc), headers={"Retry-After": str(exc.retry_after)}
        ) from exc
    except LocalAIAgentBusyError as exc:
        raise HTTPException(409, str(exc)) from exc
    if result is None:  # pragma: no cover - ownership raced with revoke
        raise HTTPException(404, "本地 Bot 连接不存在")
    agent, token = result
    audit_log(
        request,
        "local_ai_rotate",
        result="ok",
        user=user.get("username"),
        target=agent["public_id"],
        detail=f"bot_id={agent['bot_id']}",
    )
    return {
        "agent": agent,
        "token": token,
        "connection_url": "/api/local-ai/connect",
    }


@router.delete("/api/local-ai/agents/{public_id}")
async def revoke_local_ai_agent(
    public_id: str,
    request: Request,
    response: Response,
    user=Depends(require_user),
):
    _private_no_store(response)
    current = _owned_local_agent(request, public_id, user)
    changed = await _local_ai(request).revoke(
        agent_id=int(current["id"]), owner_id=int(user["id"])
    )
    if not changed:
        raise HTTPException(409, "本地 Bot 连接已撤销")
    audit_log(
        request,
        "local_ai_revoke",
        result="ok",
        user=user.get("username"),
        target=public_id,
        detail=f"bot_id={current['bot_id']}",
    )
    return {"ok": True}


@router.get("/api/admin/local-ai/agents")
async def admin_list_local_ai_agents(
    request: Request,
    response: Response,
    page: int = 1,
    per_page: int = 20,
    _admin=Depends(require_admin),
):
    _private_no_store(response)
    return await _local_ai(request).list_for_admin(
        page=max(1, int(page)), per_page=max(1, min(100, int(per_page)))
    )


@router.delete("/api/admin/local-ai/agents/{public_id}")
async def admin_revoke_local_ai_agent(
    public_id: str,
    request: Request,
    response: Response,
    admin=Depends(require_admin),
):
    _private_no_store(response)
    current = _store(request).get_local_ai_agent_by_public_id(public_id)
    if current is None:
        raise HTTPException(404, "本地 Bot 连接不存在")
    if not await _local_ai(request).revoke_as_admin(agent_id=int(current["id"])):
        raise HTTPException(409, "本地 Bot 连接已撤销")
    audit_log(
        request,
        "admin_local_ai_revoke",
        result="ok",
        user=admin.get("username"),
        target=public_id,
        detail=f"owner_id={current['owner_id']}",
    )
    return {"ok": True}


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
    bots = [
        _public_bot_identity(_sanitize_bot(_with_bot_runnable(b), user))
        for b in bots
    ]
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
    store = _store(request)
    if not store.get_user(user_id):
        raise HTTPException(404, "用户不存在")
    return {"followers": store.list_followers(user_id, limit=limit)}


@router.get("/api/users/{user_id}/following")
def user_following(user_id: int, request: Request, limit: int = 50):
    store = _store(request)
    if not store.get_user(user_id):
        raise HTTPException(404, "用户不存在")
    return {"following": store.list_following(user_id, limit=limit)}


@router.post("/api/users/{user_id}/follow")
def follow_user(user_id: int, request: Request, user=Depends(require_user)):
    store = _store(request)
    target = store.get_user(user_id)
    if not target:
        raise HTTPException(404, "用户不存在")
    if user["id"] == user_id:
        raise HTTPException(400, "不能关注自己")
    try:
        # Store 在同一写事务内再次确认两端仍存在；这里处理预检查与写入之间
        # 被管理员删除的竞态，不能把底层 FK/LookupError 泄漏成 500。
        created = store.follow(user["id"], user_id)
    except LookupError:
        raise HTTPException(404, "用户不存在") from None
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
    store = _store(request)
    if not store.get_user(user_id):
        raise HTTPException(404, "用户不存在")
    try:
        store.unfollow(user["id"], user_id)
    except LookupError:
        raise HTTPException(404, "用户不存在") from None
    return {"ok": True, "following": False}


@router.get("/api/users/{user_id}/follow-status")
def follow_status(user_id: int, request: Request, user=Depends(require_user)):
    store = _store(request)
    if not store.get_user(user_id):
        raise HTTPException(404, "用户不存在")
    return {
        "following": store.is_following(user["id"], user_id),
        "follower_count": store.follower_count(user_id),
        "following_count": store.following_count(user_id),
    }


@router.post("/api/bots/{bot_id}/favorite")
def favorite_bot(bot_id: int, request: Request, user=Depends(require_user)):
    store = _store(request)
    if not store.get_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    try:
        created = store.favorite(user["id"], bot_id)
    except LookupError:
        raise HTTPException(404, "bot 不存在") from None
    return {"ok": True, "favorited": True, "created": created}


@router.delete("/api/bots/{bot_id}/favorite")
def unfavorite_bot(bot_id: int, request: Request, user=Depends(require_user)):
    store = _store(request)
    if not store.get_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    try:
        store.unfavorite(user["id"], bot_id)
    except LookupError:
        raise HTTPException(404, "bot 不存在") from None
    return {"ok": True, "favorited": False}


@router.get("/api/bots/{bot_id}/favorite-status")
def favorite_status(bot_id: int, request: Request, user=Depends(require_user)):
    store = _store(request)
    if not store.get_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
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
        items = [
            _public_bot_identity(_sanitize_bot(_with_bot_runnable(b), user))
            for b in result["items"]
        ]
        return {"bots": items, "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {
        "bots": [
            _public_bot_identity(_sanitize_bot(_with_bot_runnable(b), user))
            for b in result
        ]
    }


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
                _public_bot_identity(_with_bot_runnable(bot))
                for bot in store.search_bots(ql, limit=lim, game_id=game_id)
            ]
        }
    if t == "matches":
        return {
            "matches": _public_match_list_rows(
                store.search_matches(ql, limit=lim, game_id=game_id)
            )
        }
    # 默认 users
    return {"users": store.search_users(ql, limit=lim)}


# 公开 bot 详情需脱敏的敏感字段（非 owner/admin 不可见）。
# 与 /api/bots/{id}/versions 的脱敏口径一致：binary_path 暴露磁盘布局，
# runtime_mode 是内部运行配置，均不应泄漏给访客（审计 P1-B）。
_BOT_SENSITIVE_FIELDS = ("binary_path", "runtime_mode")
_PUBLIC_CANONICAL_PLATFORM_FIELDS = ("format", "os", "arch")


def _sanitize_bot(bot: dict, user: dict | None) -> dict:
    """非 owner/admin 访问时脱敏 bot 字段（返回副本，不改原 dict）。"""
    if user is not None and (bot.get("owner_id") == user.get("id") or user.get("role") == "admin"):
        return bot
    return {k: v for k, v in bot.items() if k not in _BOT_SENSITIVE_FIELDS}


def _public_bot_identity(bot: dict) -> dict:
    """公开浏览面不重复发送恒定的 Linux x86_64 ELF 三元组。

    可运行性必须先由 ``_with_bot_runnable`` 计算；owner/admin 的 MyBots、版本和
    管理接口仍保留原始字段，供不可运行历史记录诊断。
    """
    public = dict(bot)
    for field in _PUBLIC_CANONICAL_PLATFORM_FIELDS:
        public.pop(field, None)
    return public


@router.get("/api/bots/{bot_id}")
def get_bot(bot_id: int, request: Request, user=Depends(optional_user)):
    bot = _bots(request).get(bot_id)
    if not bot:
        raise HTTPException(404, "bot 不存在")
    return {
        "bot": _public_bot_identity(
            _sanitize_bot(_with_bot_runnable(bot), user)
        )
    }


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
    p = _public_bot_identity(p)
    # 裁响应死字段；公开数值评分字段由 Store 的单一投影返回。
    for k in ("vol", "rated_at", "is_builtin", "updated_at"):
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
        rows = _public_match_list_rows(
            store.list_matches(limit=pp, offset=off, bot_id=bot_id)
        )
        total = store.count_bot_matches(bot_id)
        return {"matches": rows, "page": max(1, page), "per_page": pp, "total": total}
    rows = _public_match_list_rows(
        store.list_matches(
            bot_id=bot_id, limit=max(1, min(limit, 100)), offset=max(0, offset)
        )
    )
    return {"matches": rows}


@router.get("/api/bots/{bot_id}/opponents")
def bot_opponents(
    bot_id: int,
    request: Request,
    limit: int = 20,
    page: int | None = None,
    per_page: int = 20,
):
    """某 Bot 对各对手的战绩（公开，从 pair_stats 读）。

    旧 ``limit`` 列表响应保持兼容；提供 ``page`` 时使用全站统一的
    ``{opponents, page, per_page, total}`` 服务端分页契约。
    """
    store = _store(request)
    if not store.get_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    if page is not None:
        result = store.bot_opponents_stats(
            bot_id,
            page=page,
            per_page=per_page,
        )
        return {
            "opponents": result["items"],
            "page": result["page"],
            "per_page": result["per_page"],
            "total": result["total"],
        }
    return {
        "opponents": store.bot_opponents_stats(
            bot_id,
            limit=max(1, min(limit, 200)),
        )
    }


@router.get("/api/bots/{bot_id}/rating-history")
def bot_rating_history(
    bot_id: int, request: Request, limit: int = 100
):
    """某 Bot 的评分变化时序（公开，画曲线/趋势用）。"""
    if not _store(request).get_bot(bot_id):
        raise HTTPException(404, "bot 不存在")
    return {"history": _store(request).list_rating_history(bot_id, limit=max(1, min(limit, 500)))}


@router.post("/api/bots", openapi_extra=_BOT_CREATE_UPLOAD_OPENAPI)
async def upload_bot(
    request: Request,
    user=Depends(require_user),
):
    name = ""
    game_id = "holdem"
    runtime_mode = DEFAULT_RUNTIME_MODE
    try:
        async with _bot_upload_admission(request):
            async with request.form(max_files=1, max_fields=10) as form:
                name = _multipart_text(form, "name", required=True)
                display_name = _multipart_text(form, "display_name")
                description = _multipart_text(form, "description")
                upload_note = _multipart_text(form, "upload_note")
                game_id = _multipart_text(
                    form, "game_id", default="holdem"
                )
                runtime_mode = _multipart_text(
                    form, "runtime_mode", default=DEFAULT_RUNTIME_MODE
                )
                raw = await _read_bot_upload(_multipart_file(form))
            bot = await _finish_upload_step_before_cancel(
                asyncio.to_thread(
                    _bots(request).create_from_upload,
                    user["id"],
                    name,
                    raw,
                    display_name=display_name,
                    description=description,
                    upload_note=upload_note,
                    game_id=game_id,
                    runtime_mode=runtime_mode,
                    binary_runner=_new_preflight_runner(request),
                )
            )
    except _DeploymentMaintenance:
        audit_log(
            request,
            "bot_upload",
            result="busy",
            user=user.get("username"),
            target=name,
            detail="deployment_maintenance",
        )
        raise _deployment_maintenance_error()
    except _BotUploadBusy:
        audit_log(
            request,
            "bot_upload",
            result="fail",
            user=user.get("username"),
            target=name,
            detail="upload_busy",
        )
        raise _upload_busy_error()
    except BotError as e:
        audit_log(request, "bot_upload", result="fail", user=user.get("username"), target=name, detail=e.code)
        raise HTTPException(400, detail={"code": e.code, "message": e.message})
    except PlatformRunnerError:
        audit_log(request, "bot_upload", result="fail", user=user.get("username"), target=name, detail="sandbox_unavailable")
        raise HTTPException(503, "Bot 沙箱暂不可用，请稍后重试")
    audit_log(request, "bot_upload", result="ok", user=user.get("username"), target=name, detail=f"game={game_id} mode={runtime_mode} size={len(raw)}")
    return {"bot": bot}


@router.post(
    "/api/bots/{bot_id}/versions",
    openapi_extra=_BOT_VERSION_UPLOAD_OPENAPI,
)
async def upload_bot_version(
    bot_id: int,
    request: Request,
    user=Depends(require_user),
):
    try:
        async with _bot_upload_admission(request):
            async with request.form(max_files=1, max_fields=4) as form:
                upload_note = _multipart_text(form, "upload_note")
                runtime_mode = _multipart_text(form, "runtime_mode")
                raw = await _read_bot_upload(_multipart_file(form))
            bot = await _finish_upload_step_before_cancel(
                asyncio.to_thread(
                    _bots(request).upload_version,
                    bot_id,
                    user["id"],
                    raw,
                    upload_note=upload_note,
                    runtime_mode=runtime_mode or None,
                    binary_runner=_new_preflight_runner(request),
                )
            )
    except _DeploymentMaintenance:
        audit_log(
            request,
            "bot_version_upload",
            result="busy",
            user=user.get("username"),
            target=bot_id,
            detail="deployment_maintenance",
        )
        raise _deployment_maintenance_error()
    except _BotUploadBusy:
        audit_log(
            request,
            "bot_version_upload",
            result="fail",
            user=user.get("username"),
            target=bot_id,
            detail="upload_busy",
        )
        raise _upload_busy_error()
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
    active_protocol = store.get_active_game_contract(bot["game_id"])[
        "protocol_version"
    ]
    versions = []
    for raw_version in store.list_bot_versions(bot_id):
        version = _with_bot_runnable(raw_version)
        if raw_version.get("retired_at") is not None:
            version["runnable"] = False
            version["unsupported_reason"] = "该版本已退役"
        elif raw_version.get("protocol_version") != active_protocol:
            version["runnable"] = False
            version["unsupported_reason"] = "协议版本与当前游戏规则不兼容"
        versions.append(version)
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
            "version_retired": 409,
            "protocol_incompatible": 409,
        }.get(e.code, 400)
        raise HTTPException(status, detail={"code": e.code, "message": e.message})
    audit_log(request, "bot_version_rollback", result="ok", user=user.get("username"), target=bot_id, detail=f"v{version}")
    return {"bot": result}


@router.post("/api/bots/{bot_id}/active")
async def set_bot_active(
    bot_id: int, request: Request, active: bool = True, user=Depends(require_user)
):
    public_ids = (
        _store(request).list_active_local_ai_public_ids_for_bot(bot_id)
        if not active
        else []
    )
    try:
        bot = _bots(request).set_active(bot_id, user["id"], active)
    except BotError as e:
        status = (
            404 if e.code == "not_found"
            else 403 if e.code == "forbidden"
            else 409 if e.code in {
                "unsupported_binary", "version_unavailable",
                "version_retired", "protocol_incompatible",
            }
            else 400
        )
        raise HTTPException(status, detail={"code": e.code, "message": e.message})
    if public_ids:
        await _local_ai(request).revoke_public_ids(public_ids)
    return {"bot": bot}


@router.put("/api/bots/{bot_id}/ranking")
def select_ranked_bot(
    bot_id: int, request: Request, user=Depends(require_user)
):
    """Atomically make this Bot the owner's sole ranked entry for its game."""
    try:
        result = _bots(request).select_ranked(bot_id, int(user["id"]))
    except BotError as exc:
        status = {
            "not_found": 404,
            "forbidden": 403,
            "ranking_busy": 409,
            "ranking_unavailable": 409,
            "unsupported_binary": 409,
            "version_unavailable": 409,
            "version_retired": 409,
            "protocol_incompatible": 409,
        }.get(exc.code, 400)
        raise HTTPException(
            status, detail={"code": exc.code, "message": exc.message}
        ) from exc
    audit_log(
        request,
        "bot_ranking_select",
        result="ok",
        user=user.get("username"),
        target=bot_id,
        detail=(
            f"previous={result.get('previous_bot_id')} "
            f"cancelled={result.get('cancelled_queued_jobs', 0)}"
        ),
    )
    return {**result, "bot": _with_bot_runnable(result["bot"])}


@router.delete("/api/bots/{bot_id}/ranking")
def clear_ranked_bot(
    bot_id: int, request: Request, user=Depends(require_user)
):
    """Withdraw this ranked entry while retaining all Bot and rating history."""
    try:
        result = _bots(request).clear_ranked(bot_id, int(user["id"]))
    except BotError as exc:
        status = {
            "not_found": 404,
            "forbidden": 403,
            "ranking_busy": 409,
        }.get(exc.code, 400)
        raise HTTPException(
            status, detail={"code": exc.code, "message": exc.message}
        ) from exc
    audit_log(
        request,
        "bot_ranking_clear",
        result="ok",
        user=user.get("username"),
        target=bot_id,
        detail=f"cancelled={result.get('cancelled_queued_jobs', 0)}",
    )
    return {**result, "bot": _with_bot_runnable(result["bot"])}


@router.patch("/api/bots/{bot_id}")
async def update_my_bot(
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
    public_ids = (
        _store(request).list_active_local_ai_public_ids_for_bot(bot_id)
        if fields.get("is_active") == 0
        else []
    )
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
    if public_ids:
        await _local_ai(request).revoke_public_ids(public_ids)
    return {"bot": _with_bot_runnable(bot)}


@router.delete("/api/bots/{bot_id}")
async def delete_my_bot(bot_id: int, request: Request, user=Depends(require_user)):
    """Bot 拥有者删除自己的 Bot（软删：is_active=0）。"""
    store = _store(request)
    bot = store.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot 不存在")
    if bot["owner_id"] != user["id"]:
        raise HTTPException(403, "无权删除他人的 Bot")
    public_ids = store.list_active_local_ai_public_ids_for_bot(bot_id)
    store.update_bot(bot_id, is_active=0)
    if public_ids:
        await _local_ai(request).revoke_public_ids(public_ids)
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
    my_environment: Literal["platform_low", "remote_local"] = "platform_low"
    opponent_environment: Literal["platform_low", "remote_local"] = "platform_low"
    my_local_agent_id: str | None = Field(default=None, max_length=64)
    opponent_local_agent_id: str | None = Field(default=None, max_length=64)
    game_id: str | None = None
    request_id: str | None = Field(
        default=None, pattern=r"^req_[A-Za-z0-9_-]{24}$"
    )


def _execution_idempotency_fingerprint(kind: str, body: BaseModel) -> str:
    payload = {
        "kind": kind,
        "body": body.model_dump(mode="json", exclude={"request_id"}),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _existing_idempotent_request(
    request: Request,
    *,
    request_id: str | None,
    owner_user_id: int,
    source: str,
    fingerprint: str,
) -> dict | None:
    if request_id is None:
        return None
    existing = _store(request).executions.get_idempotent(
        request_id,
        owner_user_id=owner_user_id,
        source=source,
        fingerprint=fingerprint,
    )
    if existing is None:
        return None
    return _execution_dispatcher(request).public_request(request_id)


@router.post("/api/matches/challenge", status_code=202)
async def challenge(body: ChallengeBody, request: Request, user=Depends(require_user)):
    # 普通用户只能用自己的 Bot 占座位 1，防止冒用他人身份污染评分/战绩；
    # 管理员的显式全站调度能力沿用同一版本归属、可运行性和游戏一致性校验。
    # opponent_bot_id 仍允许任意可用 Bot（挑战他人 Bot 是正常功能）。
    my_bot = _store(request).get_bot(body.my_bot_id)
    if not my_bot:
        raise HTTPException(404, "Bot 不存在")
    if my_bot["owner_id"] != user["id"] and user.get("role") != ROLE_ADMIN:
        audit_log(request, "match_challenge", result="deny", user=user.get("username"),
                  detail=f"my_bot_id={body.my_bot_id} 非本人 bot")
        raise HTTPException(403, "只能用自己的 Bot 发起挑战")
    # Resolve the process owner before accepting a durable request.  A broken
    # app fixture/startup must fail without leaving a job that nobody can wake.
    dispatcher = _execution_dispatcher(request)
    fingerprint = _execution_idempotency_fingerprint("challenge", body)
    try:
        existing = _existing_idempotent_request(
            request,
            request_id=body.request_id,
            owner_user_id=int(user["id"]),
            source=EXECUTION_SOURCE_MANUAL,
            fingerprint=fingerprint,
        )
        if existing is not None:
            return existing

        async def resolve_local_agent(
            *,
            environment: str,
            public_id: str | None,
            bot_id: int,
            side: str,
        ) -> int | None:
            if environment == EXECUTION_ENV_PLATFORM_LOW:
                if public_id is not None:
                    raise ValueError(f"{side}使用节能沙箱时不能选择本地连接")
                return None
            if environment != EXECUTION_ENV_REMOTE_LOCAL or not public_id:
                raise ValueError(f"{side}使用本地 Bot 时必须选择在线连接")
            agent = _store(request).get_local_ai_agent_by_public_id(public_id)
            if (
                agent is None
                or int(agent["owner_id"]) != int(user["id"])
                or int(agent["bot_id"]) != int(bot_id)
                or str(agent.get("status") or "") != "active"
            ):
                raise ValueError(f"{side}本地连接与当前用户或 Bot 不匹配")
            if not await _local_ai(request).is_available(int(agent["id"])):
                raise ValueError(f"{side}本地 Bot 已离线或正在处理另一场对局")
            return int(agent["id"])

        local_a = await resolve_local_agent(
            environment=body.my_environment,
            public_id=body.my_local_agent_id,
            bot_id=body.my_bot_id,
            side="先手",
        )
        local_b = await resolve_local_agent(
            environment=body.opponent_environment,
            public_id=body.opponent_local_agent_id,
            bot_id=body.opponent_bot_id,
            side="后手",
        )
        request_id = await _orch(request).challenge(
            body.my_bot_id,
            body.opponent_bot_id,
            user["id"],
            game_id=body.game_id,
            bot_a_version_id=body.my_bot_version_id,
            bot_b_version_id=body.opponent_bot_version_id,
            bot_a_environment=body.my_environment,
            bot_b_environment=body.opponent_environment,
            bot_a_local_agent_id=local_a,
            bot_b_local_agent_id=local_b,
            request_id=body.request_id,
            idempotency_fingerprint=fingerprint,
        )
    except ExecutionIdempotencyConflict as e:
        raise HTTPException(409, str(e))
    except ExecutionQueueClosed as e:
        audit_log(request, "match_challenge", result="busy", user=user.get("username"), detail=str(e))
        raise HTTPException(
            503, detail={"code": e.code, "message": e.message}
        )
    except ValueError as e:
        audit_log(request, "match_challenge", result="fail", user=user.get("username"), detail=str(e))
        raise HTTPException(400, str(e))
    audit_log(request, "match_challenge", result="ok", user=user.get("username"), target=request_id, detail=f"bots={body.my_bot_id}vs{body.opponent_bot_id}")
    dispatcher.wake()
    return dispatcher.public_request(request_id)


class HumanChallengeBody(BaseModel):
    model_config = {"extra": "forbid"}

    bot_id: int
    human_seat: Literal[1] = 1  # 产品契约：人类固定座位 2（内部索引 1）
    game_id: str | None = None
    request_id: str | None = Field(
        default=None, pattern=r"^req_[A-Za-z0-9_-]{24}$"
    )


@router.post("/api/matches/human", status_code=202)
async def challenge_human(body: HumanChallengeBody, request: Request, user=Depends(require_user)):
    """人类 vs bot：当前登录用户作为人类玩家对战指定 bot。"""
    dispatcher = _execution_dispatcher(request)
    fingerprint = _execution_idempotency_fingerprint("human", body)
    try:
        existing = _existing_idempotent_request(
            request,
            request_id=body.request_id,
            owner_user_id=int(user["id"]),
            source=EXECUTION_SOURCE_HUMAN,
            fingerprint=fingerprint,
        )
        if existing is not None:
            return existing
        request_id = await _orch(request).challenge_human(
            body.bot_id,
            user["id"],
            human_seat=body.human_seat,
            game_id=body.game_id,
            request_id=body.request_id,
            idempotency_fingerprint=fingerprint,
        )
    except ExecutionIdempotencyConflict as e:
        raise HTTPException(409, str(e))
    except ExecutionQueueClosed as e:
        audit_log(request, "match_human", result="busy", user=user.get("username"), detail=str(e))
        raise HTTPException(
            503, detail={"code": e.code, "message": e.message}
        )
    except ValueError as e:
        audit_log(request, "match_human", result="fail", user=user.get("username"), detail=str(e))
        raise HTTPException(400, str(e))
    audit_log(request, "match_human", result="ok", user=user.get("username"), target=request_id, detail=f"bot={body.bot_id} seat={body.human_seat}")
    dispatcher.wake()
    return dispatcher.public_request(request_id)


def _owned_execution_request(request: Request, request_id: str, user: dict) -> dict:
    job = _store(request).executions.get(request_id)
    if job is None:
        raise HTTPException(404, "执行请求不存在")
    if job.get("owner_user_id") != user.get("id") and user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "无权访问该执行请求")
    return job


@router.get("/api/execution-requests/{request_id}")
def execution_request_detail(
    request_id: str,
    request: Request,
    user=Depends(require_user),
):
    _owned_execution_request(request, request_id, user)
    payload = _execution_dispatcher(request).public_request(request_id)
    if payload is None:
        raise HTTPException(404, "执行请求不存在")
    return payload


@router.delete("/api/execution-requests/{request_id}")
def cancel_execution_request(
    request_id: str,
    request: Request,
    user=Depends(require_user),
):
    job = _owned_execution_request(request, request_id, user)
    if user.get("role") != ROLE_ADMIN and job.get("source") not in {
        EXECUTION_SOURCE_MANUAL,
        EXECUTION_SOURCE_HUMAN,
    }:
        raise HTTPException(403, "仅可取消自己发起的挑战或人机请求")
    owner = None if user.get("role") == ROLE_ADMIN else int(user["id"])
    try:
        _store(request).executions.request_cancel(
            request_id, owner_user_id=owner
        )
    except (PermissionError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    dispatcher = _execution_dispatcher(request)
    dispatcher.wake()
    return dispatcher.public_request(request_id)


@router.post("/api/execution-requests/{request_id}/retry", status_code=202)
def retry_execution_request(
    request_id: str,
    request: Request,
    user=Depends(require_user),
):
    job = _owned_execution_request(request, request_id, user)
    if user.get("role") != ROLE_ADMIN and job.get("source") not in {
        EXECUTION_SOURCE_MANUAL,
        EXECUTION_SOURCE_HUMAN,
    }:
        raise HTTPException(403, "仅可重试自己发起的挑战或人机请求")
    owner = None if user.get("role") == ROLE_ADMIN else int(user["id"])
    try:
        _store(request).executions.retry(request_id, owner_user_id=owner)
    except ExecutionQueueClosed as exc:
        raise HTTPException(
            503, detail={"code": exc.code, "message": exc.message}
        ) from exc
    except (PermissionError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    dispatcher = _execution_dispatcher(request)
    dispatcher.wake()
    return dispatcher.public_request(request_id)


@router.websocket("/api/matches/{match_id}/play")
async def play_websocket(websocket: WebSocket, match_id: str):
    """人类对战双向通道：推送事件（含 your_turn）+ 接收人类落子。

    鉴权：Origin 必须与 ``BZ_PUBLIC_ORIGIN`` 严格匹配，随后仅从
    同源握手自动携带的 HttpOnly cookie ``bz_session`` 取会话。
    不接受 URL query token，避免长期会话进入 Uvicorn/反代访问日志。
    仅 match.human_user_id 本人可连；解析 pending 人类回合 Future。
    """
    store = websocket.app.state.store
    auth = websocket.app.state.auth
    orch = websocket.app.state.orch
    if (
        "token" in websocket.query_params
        or not websocket_origin_allowed(websocket.headers.get("origin"))
    ):
        await websocket.accept()
        await websocket.send_json({
            "type": "reject",
            "reason": "forbidden",
            "message": "无权访问该对局",
        })
        await websocket.close()
        return
    # 鉴权
    token = websocket.cookies.get(COOKIE_NAME)
    user = auth.verify_session(token)
    m = store.get_match(match_id)
    if not user or not m or m.get("human_user_id") != user["id"]:
        await websocket.accept()
        await websocket.send_json({
            "type": "reject",
            "reason": "forbidden",
            "message": "无权访问该对局",
        })
        await websocket.close()
        return
    await websocket.accept()
    # subscribe 入队一条带 seats 与完整回放的权威 snapshot；pump 随即发送。
    # 不在路由重复发送，否则每次连接都会收到两份相同快照，造成前端重复归约。
    human_seat = int(m.get("human_seat")) if m.get("human_seat") is not None else 1
    q = orch.subscribe(match_id, human_viewer_seat=human_seat)
    try:
        protocol = game_registry.get(m.get("game_id")).protocol
    except KeyError:
        await websocket.send_json({
            "type": "reject",
            "reason": "invalid_game_id",
            "message": "对局游戏协议不存在",
        })
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


@router.websocket("/api/local-ai/connect")
async def local_ai_websocket(websocket: WebSocket):
    """Outbound-only user-hosted Bot transport.

    The long-lived credential is accepted only in the Authorization header.
    Browsers and URL query credentials are rejected, so tokens never enter
    navigation history, referrers or request-target logs.
    """

    service = getattr(websocket.app.state, "local_ai_service", None)
    if service is None:
        await _deny_local_ai_websocket(websocket)
        return

    trust_proxy = str(os.environ.get("BZ_TRUST_PROXY", "")).strip().lower() in {
        "1", "true", "yes", "on",
    }
    try:
        proxy_hops = max(1, int(os.environ.get("BZ_TRUSTED_PROXY_HOPS", "1")))
    except ValueError:
        proxy_hops = 1
    peer_ip = client_ip(
        websocket,
        trust_proxy=trust_proxy,
        hops=proxy_hops,
        trusted_proxy_cidrs=getattr(
            websocket.app.state, "trusted_proxy_cidrs", None
        ),
    )
    if not await service.handshake_gate.begin(peer_ip):
        await _deny_local_ai_websocket(websocket)
        return

    agent = None
    try:
        authorization = str(websocket.headers.get("authorization") or "")
        parts = authorization.split(" ", 1)
        token = (
            parts[1].strip()
            if len(parts) == 2 and parts[0].lower() == "bearer"
            else ""
        )
        if (
            "token" in websocket.query_params
            or websocket.headers.get("origin") is not None
        ):
            await _deny_local_ai_websocket(websocket)
            return
        agent = service.authenticate(token)
        if agent is None:
            # Closing before accept produces an HTTP 403 handshake denial; bad
            # credentials never consume a long-lived WebSocket.
            await _deny_local_ai_websocket(websocket)
            return
    finally:
        await service.handshake_gate.end()

    # The pre-auth concurrency gate has completed its only database lookup.
    # Establish the durable/live connection afterwards so no await between a
    # successful connect and the protected accept/ready block can strand it.
    try:
        connection, generation = await service.connect(agent)
    except (LocalAIConnectionError, RuntimeError, ValueError):
        await _deny_local_ai_websocket(websocket)
        return

    public_id = str(agent["public_id"])
    connection_id = connection.connection_id
    sender: asyncio.Task | None = None
    receiver: asyncio.Task | None = None
    last_db_touch = time.monotonic()
    touch_lock = asyncio.Lock()
    inbound_tokens = _LOCAL_AI_INBOUND_BURST
    inbound_refilled_at = time.monotonic()

    async def persist_liveness_if_due() -> None:
        nonlocal last_db_touch
        now = time.monotonic()
        if now - last_db_touch < _LOCAL_AI_DB_TOUCH_INTERVAL_SECONDS:
            return
        async with touch_lock:
            now = time.monotonic()
            if now - last_db_touch < _LOCAL_AI_DB_TOUCH_INTERVAL_SECONDS:
                return
            await service.touch_connection(agent, connection_id, generation)
            last_db_touch = now

    def consume_inbound_token() -> bool:
        nonlocal inbound_tokens, inbound_refilled_at
        now = time.monotonic()
        inbound_tokens = min(
            _LOCAL_AI_INBOUND_BURST,
            inbound_tokens
            + (now - inbound_refilled_at) * _LOCAL_AI_INBOUND_REFILL_PER_SECOND,
        )
        inbound_refilled_at = now
        if inbound_tokens < 1.0:
            return False
        inbound_tokens -= 1.0
        return True

    def refund_bound_turn_token() -> None:
        """Do not classify one referee-requested reply as unsolicited traffic."""

        nonlocal inbound_tokens
        inbound_tokens = min(_LOCAL_AI_INBOUND_BURST, inbound_tokens + 1.0)

    async def send_turns() -> None:
        while True:
            turn = await service.hub.next_turn(
                public_id, connection_id, timeout=20.0
            )
            await persist_liveness_if_due()
            if turn is None:
                await websocket.send_json({"type": "ping"})
                continue
            remaining_ms = max(
                0, int((float(turn.deadline_at) - time.monotonic()) * 1000)
            )
            await websocket.send_json(
                {
                    "type": "turn",
                    "request_id": turn.request_id,
                    "match_id": turn.match_id,
                    "turn": turn.turn,
                    "seat": turn.seat + 1,
                    "input_line": turn.input,
                    "timeout_ms": remaining_ms,
                }
            )

    async def receive_responses() -> None:
        while True:
            raw = await websocket.receive_text()
            if not consume_inbound_token():
                await websocket.send_json(
                    {"type": "reject", "reason": "rate_limit_exceeded"}
                )
                await websocket.close(code=1008)
                return
            if len(raw.encode("utf-8")) > MAX_LOCAL_AI_WEBSOCKET_MESSAGE_BYTES:
                await websocket.send_json(
                    {"type": "reject", "reason": "message_too_large"}
                )
                await websocket.close(code=1009)
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                await websocket.send_json(
                    {"type": "reject", "reason": "invalid_json"}
                )
                continue
            if not isinstance(message, dict):
                await websocket.send_json(
                    {"type": "reject", "reason": "invalid_message"}
                )
                continue
            if message.get("type") in {"ping", "pong"} and set(message) == {
                "type"
            }:
                await service.hub.heartbeat(public_id, connection_id)
                await persist_liveness_if_due()
                await websocket.send_json({"type": "pong"})
                continue
            message_type = message.get("type")
            response_fields = {
                "type", "request_id", "match_id", "turn", "output",
            }
            failure_fields = {
                "type", "request_id", "match_id", "turn", "reason",
            }
            if not (
                (message_type == "response" and set(message) == response_fields)
                or (message_type == "failure" and set(message) == failure_fields)
            ):
                await websocket.send_json(
                    {"type": "reject", "reason": "invalid_message"}
                )
                continue
            try:
                if message_type == "response":
                    output = message.get("output")
                    if (
                        not isinstance(output, str)
                        or len(output.encode("utf-8"))
                        > MAX_BOT_RESPONSE_LINE_BYTES
                    ):
                        await websocket.send_json(
                            {"type": "reject", "reason": "invalid_output"}
                        )
                        continue
                    accepted = await service.hub.submit_response(
                        public_id,
                        connection_id,
                        request_id=message["request_id"],
                        match_id=message["match_id"],
                        turn=message["turn"],
                        output=output,
                    )
                else:
                    accepted = await service.hub.submit_failure(
                        public_id,
                        connection_id,
                        request_id=message["request_id"],
                        match_id=message["match_id"],
                        turn=message["turn"],
                        reason=message["reason"],
                    )
            except (LocalAIResponseRejected, TypeError, ValueError) as exc:
                reason = getattr(exc, "reason", "invalid_response")
                await websocket.send_json({"type": "reject", "reason": reason})
                continue
            # A response/failure that passed the exact request, match and turn
            # binding is one-for-one traffic requested by the referee.  Return
            # its admission token so fast deterministic Bots are not mistaken
            # for an unsolicited frame flood.  Heartbeats, malformed frames and
            # rejected/duplicate responses remain charged to the abuse bucket.
            refund_bound_turn_token()
            await persist_liveness_if_due()
            await websocket.send_json(
                {
                    "type": "accepted",
                    "request_id": accepted.request_id,
                    "match_id": accepted.match_id,
                    "turn": accepted.turn,
                }
            )

    try:
        # Cleanup ownership starts immediately after durable connect.  Failures
        # in accept/ready/task construction cannot strand the hub registration
        # or its SQLite online generation.
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "ready",
                "agent_id": public_id,
                "label": str(agent["label"]),
                "game_id": str(agent["game_id"]),
            }
        )
        sender = asyncio.create_task(send_turns())
        receiver = asyncio.create_task(receive_responses())
        await asyncio.wait(
            {sender, receiver}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        tasks = [task for task in (sender, receiver) if task is not None]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await service.disconnect(agent, connection_id, generation)
        try:
            await websocket.close(code=1000)
        except (RuntimeError, WebSocketDisconnect):
            pass


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
    rows = _public_match_list_rows(
        store.list_matches(
            status=status,
            game_id=game_id,
            has_technical_incidents=has_technical_incidents,
            limit=lim,
            offset=off,
        )
    )
    # 参与者公开身份与列表裁剪均由 _public_match_list_rows 单点负责。
    # winner/reason/match_type/contest_id 仍是 BotDetail/Home/admin 的现行消费者字段。
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
    return {
        "matches": _public_match_list_rows(
            store.list_liked_top_matches(limit), keep_engagement=True
        )
    }


@router.get("/api/matches/{match_id}")
def match_detail(
    match_id: str,
    request: Request,
    response: Response = None,
    user: dict | None = Depends(optional_user),
):
    """Return lightweight match metadata; replay events have a separate route."""
    # 当前身份决定 can_view_debug；共享缓存必须按认证上下文分离。
    if response is not None:
        response.headers["Vary"] = "Authorization, Cookie"
    store = _store(request)
    m = store.get_match_detailed(match_id)
    if not m:
        raise HTTPException(404, "对局不存在")
    public_match = _with_seat_info(m, store=store)
    # 只暴露“当前身份是否具备读取权限”，不暴露调试记录是否存在、数量或内容。
    # MatchViewer 据此避免让无关登录用户产生预期内的 403 请求噪声。
    public_match["can_view_debug"] = False
    # 权威终局回归会直接调用本函数观察广播时的 API 快照；
    # 该调用无 FastAPI 依赖注入，因而 Depends 默认值不得被当成已登录用户。
    if isinstance(user, dict):
        access = store.can_read_match_debug(
            match_id,
            user_id=int(user["id"]),
            is_admin=user.get("role") == ROLE_ADMIN,
        )
        public_match["can_view_debug"] = bool(access.get("allowed"))
    return {"match": public_match}


@router.get("/api/matches/{match_id}/debug")
def match_debug(
    match_id: str,
    request: Request,
    response: Response,
    user: dict | None = Depends(optional_user),
):
    """终态 Bot debug 私有读取；拒绝响应不泄漏记录是否存在。"""
    response.headers.update(_DEBUG_NO_STORE_HEADERS)
    if not isinstance(user, dict):
        audit_log(
            request,
            "match_debug_read",
            result="fail",
            target=match_id,
            detail="unauthenticated",
        )
        raise HTTPException(
            401,
            "未登录或会话过期",
            headers=_DEBUG_NO_STORE_HEADERS,
        )
    result = _store(request).get_match_debug_for_user(
        match_id,
        user_id=int(user["id"]),
        is_admin=user.get("role") == ROLE_ADMIN,
    )
    if not result.get("found"):
        audit_log(
            request,
            "match_debug_read",
            result="fail",
            user=user.get("username") or user.get("id"),
            target=match_id,
            detail="not_found",
        )
        raise HTTPException(
            404,
            "对局不存在",
            headers=_DEBUG_NO_STORE_HEADERS,
        )
    if not result.get("allowed"):
        audit_log(
            request,
            "match_debug_read",
            result="fail",
            user=user.get("username") or user.get("id"),
            target=match_id,
            detail="denied",
        )
        raise HTTPException(
            403,
            "无权查看该对局的调试信息",
            headers=_DEBUG_NO_STORE_HEADERS,
        )
    entries = result.get("entries") or []
    audit_log(
        request,
        "match_debug_read",
        user=user.get("username") or user.get("id"),
        target=match_id,
        detail=f"entries={len(entries)}",
    )
    return {
        "match_id": match_id,
        "entries": entries,
        "entry_count": int(result.get("entry_count") or 0),
        "total_bytes": int(result.get("total_bytes") or 0),
        "dropped_count": int(result.get("dropped_count") or 0),
        "updated_at": result.get("updated_at"),
    }


@router.get("/api/matches/{match_id}/replay")
def match_replay(match_id: str, request: Request):
    """Return the structured, public replay only when a viewer needs it."""
    payload = _store(request).get_public_replay_payload(match_id)
    if payload is None:
        raise HTTPException(404, "对局不存在")
    return payload


@router.get("/api/matches/{match_id}/log")
def match_log(match_id: str, request: Request):
    """Download one finalized canonical public replay for any known game."""
    try:
        source = _store(request).get_match_record_source(match_id)
    except ValueError:
        raise HTTPException(
            409,
            "对局游戏定位损坏，无法导出日志",
            headers=_MATCH_RECORD_HEADERS,
        ) from None
    if source is None:
        raise HTTPException(404, "对局不存在", headers=_MATCH_RECORD_HEADERS)
    match = source["match"]
    if match.get("status") not in (STATUS_COMPLETED, STATUS_ABORTED):
        raise HTTPException(
            409,
            "对局尚未结束，暂不能导出日志",
            headers=_MATCH_RECORD_HEADERS,
        )

    try:
        spec = game_registry.get(match["game_id"])
    except (AttributeError, KeyError, TypeError):
        raise HTTPException(
            409,
            "该对局的游戏不支持日志导出",
            headers=_MATCH_RECORD_HEADERS,
        ) from None

    if not source["replay_finalized"]:
        raise HTTPException(
            409,
            "对局日志尚未完成持久化，暂不能导出",
            headers=_MATCH_RECORD_HEADERS,
        )

    try:
        public_match = _public_match_record_source(match)
    except ValueError:
        raise HTTPException(
            409,
            "对局规则契约损坏，无法导出日志",
            headers=_MATCH_RECORD_HEADERS,
        ) from None

    payload = {
        "format": _MATCH_LOG_FORMAT,
        "format_version": _MATCH_LOG_FORMAT_VERSION,
        "match": public_match,
        "replay": source["replay"],
    }
    try:
        content = (
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise HTTPException(
            409,
            "对局公开日志数据损坏，无法导出",
            headers=_MATCH_RECORD_HEADERS,
        ) from None
    filename = _match_log_filename(str(spec.game_id), match_id)
    return Response(
        content=content,
        media_type="application/json",
        headers={
            **_MATCH_RECORD_HEADERS,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


@router.get("/api/matches/{match_id}/record")
def match_record(match_id: str, request: Request):
    """Download one terminal match through its game's public record capability."""
    store = _store(request)
    try:
        source = store.get_match_record_source(match_id)
    except ValueError:
        raise HTTPException(
            409,
            "对局游戏定位损坏，无法导出记录",
            headers=_MATCH_RECORD_HEADERS,
        ) from None
    if source is None:
        raise HTTPException(404, "对局不存在", headers=_MATCH_RECORD_HEADERS)
    match = source["match"]
    if match.get("status") not in (STATUS_COMPLETED, STATUS_ABORTED):
        raise HTTPException(
            409,
            "对局尚未结束，暂不能导出记录",
            headers=_MATCH_RECORD_HEADERS,
        )

    try:
        spec = game_registry.get(match["game_id"])
    except (AttributeError, KeyError, TypeError):
        raise HTTPException(
            409,
            "该对局的游戏不支持记录导出",
            headers=_MATCH_RECORD_HEADERS,
        ) from None
    if spec.record_exporter is None:
        raise HTTPException(
            409,
            "该游戏暂不支持记录导出",
            headers=_MATCH_RECORD_HEADERS,
        )

    if not source["replay_finalized"]:
        raise HTTPException(
            409,
            "对局记录尚未完成持久化，暂不能导出",
            headers=_MATCH_RECORD_HEADERS,
        )

    replay = source["replay"]
    try:
        record_source = _public_match_record_source(match)
    except ValueError:
        raise HTTPException(
            409,
            "对局规则契约损坏，无法导出记录",
            headers=_MATCH_RECORD_HEADERS,
        ) from None
    try:
        record = spec.record_exporter(
            match=record_source,
            events=list(replay["events"]),
            replay_updated_at=replay.get("updated_at"),
        )
    except MatchRecordExportError:
        raise HTTPException(
            409,
            "对局规则或回放契约冲突，无法导出记录",
            headers=_MATCH_RECORD_HEADERS,
        ) from None
    content = (
        json.dumps(
            record,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")
    filename = _match_record_filename(str(spec.game_id), match_id)
    return Response(
        content=content,
        media_type="application/json",
        headers={
            **_MATCH_RECORD_HEADERS,
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


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
    request: Request, game_id: str, limit: int = 50,
    page: int | None = None, per_page: int = 50,
):
    normalized_game_id = game_id.strip().lower()
    try:
        game_registry.get(normalized_game_id)
    except (KeyError, AttributeError) as exc:
        raise HTTPException(400, f"未知游戏: {game_id!r}") from exc
    result = _store(request).list_leaderboard(
        game_id=normalized_game_id, limit=max(1, min(limit, 200)), page=page,
        per_page=per_page,
    )
    # 响应白名单投影：平台三元组、game_id 重复列、内部累计分差和波动率都不属于
    # 排行阅读信息；游戏维度只在响应顶层返回一次。
    items = result["items"]
    keep = {
        "rank", "rank_total", "percentile", "bot_id", "rating", "rd",
        "confidence_low", "confidence_high", "wins", "losses", "draws",
        "rated_matches", "unique_opponents", "bot_name", "bot_display", "owner_name",
        "rating_delta", "recent_delta_30d", "ranking_min_matches",
        "ranking_progress", "ranking_eligible",
        "last_match_id", "last_match_at",
    }
    proj = [{k: row[k] for k in keep if k in row} for row in items]
    response = {
        "leaderboard": proj,
        "game_id": result["game_id"],
        "ranking_min_matches": result["ranking_min_matches"],
        "summary": result["summary"],
        "total": result["total"],
    }
    if page is not None:
        response.update({"page": result["page"], "per_page": result["per_page"]})
    return response


@router.get("/api/execution-queue")
def execution_queue(request: Request):
    """Public global capacity/queue projection with no internal identifiers."""
    return _execution_dispatcher(request).public_snapshot()


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
    model_config = {"extra": "forbid"}

    target_type: Literal["match", "bot"]
    target_id: str = Field(..., min_length=1, max_length=128)
    body: str = Field(..., min_length=1, max_length=2000)


class LikeReq(BaseModel):
    model_config = {"extra": "forbid"}

    target_type: Literal["match", "bot", "comment"]
    target_id: str = Field(..., min_length=1, max_length=128)


@router.get("/api/comments")
def list_comments(
    request: Request,
    target_type: Literal["match", "bot"],
    target_id: str,
    limit: int = 100,
    page: int | None = None,
    per_page: int = 50,
):
    store = _store(request)
    _require_social_target(
        store, target_type, target_id, allowed_types=COMMENT_TARGET_TYPES
    )
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
    _require_social_target(
        store, req.target_type, req.target_id, allowed_types=COMMENT_TARGET_TYPES
    )
    try:
        c = store.add_comment(user["id"], req.target_type, req.target_id, body)
    except LookupError as exc:
        # 目标可能在只读校验与写事务之间被管理员删除；仍须 fail closed。
        raise HTTPException(404, "互动目标不存在") from exc
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
                        exclude_user_ids={int(user["id"])},
                    )
            elif req.target_type == "bot":
                b = store.get_bot(int(req.target_id))
                if (
                    b
                    and b.get("owner_id")
                    and int(b["owner_id"]) != int(user["id"])
                ):
                    notifier.notify(
                        b["owner_id"], type="comment",
                        title="你的 Bot 有新评论",
                        body=body[:80], link=f"/bot/{req.target_id}",
                    )
        except Exception:
            logger.warning(
                "comment notify failed target_type=%s target_id=%s user=%s",
                req.target_type,
                req.target_id,
                user.get("id"),
                exc_info=True,
            )
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
    store = _store(request)
    _require_social_target(
        store, req.target_type, req.target_id, allowed_types=LIKE_TARGET_TYPES
    )
    try:
        created = store.like(user["id"], req.target_type, req.target_id)
    except LookupError as exc:
        raise HTTPException(404, "互动目标不存在") from exc
    return {"ok": True, "liked": True, "created": created}


@router.delete("/api/likes")
def unlike_target(req: LikeReq, request: Request, user=Depends(require_user)):
    store = _store(request)
    _require_social_target(
        store, req.target_type, req.target_id, allowed_types=LIKE_TARGET_TYPES
    )
    try:
        store.unlike(user["id"], req.target_type, req.target_id)
    except LookupError as exc:
        raise HTTPException(404, "互动目标不存在") from exc
    return {"ok": True, "liked": False}


@router.get("/api/likes/status")
def like_status(
    request: Request,
    target_type: Literal["match", "bot", "comment"],
    target_id: str,
    user=Depends(require_user),
):
    store = _store(request)
    _require_social_target(
        store, target_type, target_id, allowed_types=LIKE_TARGET_TYPES
    )
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
    model_config = {"extra": "forbid"}

    id: int


class NotificationPrefsUpdate(BaseModel):
    model_config = {"extra": "forbid"}

    email_match_done: StrictBool | None = None
    email_followed: StrictBool | None = None
    email_contest: StrictBool | None = None
    email_comment: StrictBool | None = None


_NOTIFICATION_PREF_FIELDS = tuple(NotificationPrefsUpdate.model_fields)


def _public_notification_prefs(stored: dict[str, Any]) -> dict[str, bool]:
    """Project SQLite's 0/1 storage values onto the public boolean contract."""
    return {field: bool(stored.get(field, 0)) for field in _NOTIFICATION_PREF_FIELDS}


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
    stored = _store(request).get_notification_prefs(user["id"])
    return {"prefs": _public_notification_prefs(stored)}


@router.put("/api/notification-prefs")
def update_notif_prefs(
    prefs: NotificationPrefsUpdate,
    request: Request,
    user=Depends(require_user),
):
    clean = prefs.model_dump(exclude_none=True)
    if not clean:
        raise HTTPException(400, "无可更新字段")
    stored = _store(request).update_notification_prefs(user["id"], **clean)
    return {"prefs": _public_notification_prefs(stored)}


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


_CONTEST_ENTRY_PUBLIC_FIELDS = (
    "id",
    "contest_id",
    "user_id",
    "bot_id",
    "registered_at",
    "group_id",
    "seed",
    "eliminated",
    "dispatched_at",
)
_CONTEST_OFFICIAL_RESULT_PUBLIC_FIELDS = (
    "id",
    "contest_id",
    "entry_id",
    "stage_idx",
    "rank",
    "points",
    "bot_id",
    "user_id",
    "tiebreaks_json",
    "awarded",
    "bot_name",
    "bot_display",
    "owner_name",
    "owner_display",
    "source_stage",
    "ranking_cohort",
)


def _public_my_contest_entry(entry: dict[str, Any] | None) -> dict[str, Any] | None:
    """Positive allow-list for my_entry; snapshot storage never crosses REST."""
    if entry is None:
        return None
    return {key: entry.get(key) for key in _CONTEST_ENTRY_PUBLIC_FIELDS}


def _strip_contest_identity_fields(row: dict[str, Any]) -> dict[str, Any]:
    """Positive allow-list for public results; future Store columns stay private."""
    return {
        field: row.get(field)
        for field in _CONTEST_OFFICIAL_RESULT_PUBLIC_FIELDS
        if field in row
    }


def _csv_safe_cell(value: object, *, force_text: bool = False) -> object:
    """Return an Excel-safe CSV cell without changing numeric values.

    User-controlled strings that could be interpreted as formulas are prefixed
    with an apostrophe.  ``force_text`` is used for phone and student numbers so
    leading zeroes survive spreadsheet import and long values avoid scientific
    notation.
    """
    if not isinstance(value, str) or not value:
        return value
    trimmed = value.lstrip(" \t\r\n")
    dangerous = bool(trimmed and trimmed[0] in ("=", "+", "-", "@"))
    if force_text or dangerous or value[0] in ("\t", "\r", "\n"):
        return "'" + value
    return value


def _template_for_api(template: dict[str, Any]) -> dict[str, Any]:
    """Return the read-only public shape of one code-owned template."""
    public = dict(template)
    public.pop("match_config", None)
    public["is_builtin"] = True
    public["source"] = CONFIGURATION_SOURCE
    return public


def _contest_for_api(contest: dict[str, Any]) -> dict[str, Any]:
    """仅输出现行赛事契约；数据库迁移列不进入 REST 响应。"""
    public = dict(contest)
    public.pop("hands_per_match", None)
    public.pop("match_config_json", None)
    public["template_name"] = contest_template_name(public)
    if public.get("showcase_key"):
        public["description"] = contest_public_description(public)
    return public


def _contest_write_http_error(exc: ValueError) -> HTTPException:
    """Map immutable showcase writes to conflict, preserving normal 400s."""
    if isinstance(exc, ExecutionQueueClosed):
        return HTTPException(
            503,
            detail={"code": exc.code, "message": exc.message},
            headers={"Retry-After": "30"},
        )
    if isinstance(exc, ContestRealNameRosterForbidden):
        return HTTPException(403, str(exc))
    return HTTPException(409 if isinstance(exc, ShowcaseReadOnlyError) else 400, str(exc))


@router.get("/api/contests/templates")
def contest_templates(request: Request, game: str | None = None):
    del request
    try:
        templates = list_templates(game_id=game)
    except (KeyError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    return {
        "templates": [_template_for_api(t) for t in templates],
        "source": CONFIGURATION_SOURCE,
        "mutable": False,
    }


# 未发布/已取消赛事仅该赛事组织者与管理员可见。使用 schema 常量，避免
# API 层另造一套状态字面量；显式 ``?status=draft`` 也不得绕过可见性。
_CONTEST_HIDDEN_STATUSES = (CONTEST_DRAFT, CONTEST_CANCELLED)

_PUBLIC_PAIRING_INTERNAL_FIELDS = (
    "contest_id",
    "entry_a_id",
    "entry_b_id",
    "bot_a_version_id",
    "bot_b_version_id",
    "pairing_seed",
    "published_at",
    "color_first",
    "match_status",
)

_PUBLIC_PAIRING_FIELDS = frozenset(
    {
        "id",
        "round_num",
        "bot_a_id",
        "bot_b_id",
        "scheduled_at",
        "match_id",
        "status",
        "stage_idx",
        "stage_key",
        "group_id",
        "bracket_slot",
        "bot_a_name",
        "bot_a_display",
        "bot_b_name",
        "bot_b_display",
        "owner_a_name",
        "owner_a_display",
        "owner_b_name",
        "owner_b_display",
        "match_winner",
        "is_bye",
    }
)


def _contest_stage_types(contest: dict[str, Any]) -> dict[int, object]:
    """Return persisted stage types keyed by index; malformed history is unknown."""
    raw = contest.get("stages_json") or "[]"
    if isinstance(raw, list):
        stages = raw
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            parsed = []
        stages = parsed if isinstance(parsed, list) else []
    return {
        index: stage.get("type")
        for index, stage in enumerate(stages)
        if isinstance(stage, dict)
    }


def _public_contest_pairings(
    rows: list[dict], *, stage_types: dict[int, object] | None = None
) -> list[dict]:
    """Return schedule rows with public Bot/user identity and no execution keys."""
    known_stage_types = stage_types or {}
    projected: list[dict] = []
    for row in rows:
        public = dict(row)
        try:
            stage_idx = int(row.get("stage_idx") or 0)
        except (TypeError, ValueError):
            stage_idx = -1
        # bot_b_id 可能因历史硬删除被 SET NULL，legacy pairing 也可能没有 entry id。
        # 赛制类型与四项权威条件必须同时成立；歧义一律 fail closed。
        public["is_bye"] = is_authoritative_no_opponent_pairing(
            known_stage_types.get(stage_idx), row
        )
        for field in _PUBLIC_PAIRING_INTERNAL_FIELDS:
            public.pop(field, None)
        projected.append(
            {key: value for key, value in public.items() if key in _PUBLIC_PAIRING_FIELDS}
        )
    return projected


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
                                           hidden_owner_id=hidden_owner_id,
                                           exclude_showcases=True)
    # 裁列表响应死字段（对抗审计：match_config_json/hands_per_match/phase/source_contest_id
    # 列表视图不消费；不动 organizer_id/stages_json/rest_ends_at/current_stage_idx/
    # official_results_ready——共享 list_contests 喂 /api/contests/{id} + 后端内部读取）。
    items = [
        _contest_for_api(contest)
        for contest in (result["items"] if isinstance(result, dict) else result)
    ]
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
    contest_id: int, request: Request, response: Response,
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
    is_organizer = bool(
        user
        and (
            c.get("organizer_id") == user.get("id")
            or user.get("role") == ROLE_ADMIN
        )
    )
    include_identity = bool(
        is_organizer and int(c.get("require_real_name") or 0)
    )
    # entries 可单列分页（115 报名场景）：提供 entries_page 时返回分页元信息，
    # 否则保持旧的全量列表契约（pairings/standings 不分页——stage 级，量小）。
    entries_result = store.contest_entries_named(
        contest_id,
        page=entries_page,
        per_page=entries_per_page,
        include_identity=include_identity,
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
    # 阶段投影依赖 entry_a_id / entry_b_id 求实际参赛者；响应才裁掉这些
    # 内部关联键。不能拿 public pairings 反哺内部 presentation，否则阶段榜会变空。
    raw_pairings = store.contest_bracket(contest_id)
    pairings = _public_contest_pairings(
        raw_pairings, stage_types=_contest_stage_types(c)
    )
    stage_entries = (
        entries
        if not isinstance(entries_result, dict)
        else store.contest_entries_named(contest_id, include_identity=False)
    )
    stage_summaries = build_stage_summaries(
        _contests(request), c, stage_entries, raw_pairings
    )
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
    # my_entry 必须使用正向白名单；contest_entries 新增任何内部快照列都不会
    # 因 SELECT * 自动进入公开响应。实名详情只在实名赛 + 组织者/admin 下投影。
    my_entry = _public_my_contest_entry(
        store.get_entry(contest_id, user["id"]) if user else None
    )
    if include_identity:
        response.headers.update(_CONTEST_IDENTITY_PRIVATE_HEADERS)
    # 裁 standings/pairings 响应死字段（对抗审计：前端 ContestDetail/BracketTree/
    # ScheduleTable 不消费；表列与内部计算保留——仅从响应 dict 去掉）。
    _STANDINGS_DEAD = ("entry_id", "user_id", "seed", "eliminated")
    for s in standings:
        for k in _STANDINGS_DEAD:
            s.pop(k, None)
    # 旧库列仅作历史存储；现行 API 不再暴露可覆盖的规则配置。
    c = _contest_for_api(c)
    resp = {
        "contest": c,
        "entries": entries,
        "pairings": pairings,
        "standings": standings,
        "stage_standings": stage_summaries,
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
    return {
        "pairings": _public_contest_pairings(
            _store(request).contest_bracket(contest_id),
            stage_types=_contest_stage_types(contest),
        )
    }


def _require_contest_organizer(c: dict, user: dict) -> None:
    """校验当前用户是该场赛事组织者或 admin（与 open/start 同权限模型）。"""
    if c.get("organizer_id") != user.get("id") and user.get("role") != ROLE_ADMIN:
        raise HTTPException(403, "仅该场赛事组织者或管理员可操作")


@router.post("/api/contests/{contest_id}/entries")
async def organizer_add_entry(
    contest_id: int, body: dict, request: Request, user=Depends(require_organizer)
):
    """P5 组织者名单：单条加人（draft/open 允许）。

    非实名赛允许组织者现场补录；实名赛采集 PII 必须由参赛者本人 register，只有
    admin 的显式高权限 override 可以代报名，且成功/失败都写无 PII 安全审计。
    """
    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        raise HTTPException(404, "赛事不存在")
    _require_contest_organizer(c, user)
    requires_identity = bool(int(c.get("require_real_name") or 0))
    admin_override = requires_identity and user.get("role") == ROLE_ADMIN
    if requires_identity and not admin_override:
        audit_log(
            request,
            "contest_real_name_roster_override",
            result="fail",
            user=user.get("username"),
            target=contest_id,
            detail="mode=single; reason=self_registration_required",
        )
        raise HTTPException(403, ContestRealNameRosterForbidden.MESSAGE)
    raw_uid = body.get("user_id")
    raw_bid = body.get("bot_id")
    if raw_uid is None or raw_bid is None:
        if admin_override:
            audit_log(
                request,
                "contest_real_name_roster_override",
                result="fail",
                user=user.get("username"),
                target=contest_id,
                detail="mode=single; reason=missing_ids",
            )
        raise HTTPException(400, "user_id 与 bot_id 均不可为空")
    try:
        uid, bid = int(raw_uid), int(raw_bid)
    except (TypeError, ValueError):
        if admin_override:
            audit_log(
                request,
                "contest_real_name_roster_override",
                result="fail",
                user=user.get("username"),
                target=contest_id,
                detail="mode=single; reason=invalid_ids",
            )
        raise HTTPException(400, "user_id / bot_id 必须是整数")
    try:
        entry = await _contests(request).add_roster_entry(
            contest_id,
            uid,
            bid,
            allow_real_name_override=admin_override,
        )
    except ContestRealNameRosterForbidden as exc:
        # Covers a zero-entry 0→1 flag change between the API read and the
        # Manager/Store linearization point.
        audit_log(
            request,
            "contest_real_name_roster_override",
            result="fail",
            user=user.get("username"),
            target=contest_id,
            detail="mode=single; reason=self_registration_required",
        )
        raise _contest_write_http_error(exc) from exc
    except ValueError as exc:
        if admin_override:
            audit_log(
                request,
                "contest_real_name_roster_override",
                result="fail",
                user=user.get("username"),
                target=contest_id,
                detail=(
                    f"mode=single; user_id={uid}; bot_id={bid}; "
                    "reason=validation_failed"
                ),
            )
        raise _contest_write_http_error(exc) from exc
    actual_identity_override = (
        entry.get("identity_source") == CONTEST_IDENTITY_SOURCE_REGISTRATION
    )
    if actual_identity_override:
        audit_log(
            request,
            "contest_real_name_roster_override",
            result="ok",
            user=user.get("username"),
            target=contest_id,
            detail=(
                f"mode=single; entry_id={entry['id']}; "
                f"user_id={uid}; bot_id={bid}"
            ),
        )
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
    requires_identity = bool(int(c.get("require_real_name") or 0))
    admin_override = requires_identity and user.get("role") == ROLE_ADMIN
    if requires_identity and not admin_override:
        audit_log(
            request,
            "contest_real_name_roster_override",
            result="fail",
            user=user.get("username"),
            target=contest_id,
            detail=(
                f"mode=bulk; assign_all={int(body.assign_all)}; "
                f"submitted={len(body.entries or [])}; "
                "reason=self_registration_required"
            ),
        )
        raise HTTPException(403, ContestRealNameRosterForbidden.MESSAGE)
    from bzplat.backend.games import normalize_game_id
    try:
        cgid = normalize_game_id(c.get("game_id"))
    except ValueError as exc:
        if admin_override:
            audit_log(
                request,
                "contest_real_name_roster_override",
                result="fail",
                user=user.get("username"),
                target=contest_id,
                detail="mode=bulk; reason=contest_game_invalid",
            )
        raise HTTPException(400, str(exc)) from exc
    if body.assign_all:
        try:
            gid = normalize_game_id(
                cgid if body.game_id is None else body.game_id
            )
        except ValueError as exc:
            if admin_override:
                audit_log(
                    request,
                    "contest_real_name_roster_override",
                    result="fail",
                    user=user.get("username"),
                    target=contest_id,
                    detail="mode=bulk; assign_all=1; reason=invalid_game",
                )
            raise HTTPException(400, str(exc)) from exc
        if gid != cgid:
            if admin_override:
                audit_log(
                    request,
                    "contest_real_name_roster_override",
                    result="fail",
                    user=user.get("username"),
                    target=contest_id,
                    detail="mode=bulk; assign_all=1; reason=game_mismatch",
                )
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
            if admin_override:
                audit_log(
                    request,
                    "contest_real_name_roster_override",
                    result="fail",
                    user=user.get("username"),
                    target=contest_id,
                    detail="mode=bulk; assign_all=0; reason=invalid_ids",
                )
            raise HTTPException(400, "user_id / bot_id 必须是整数")
    try:
        result = await _contests(request).assign_roster_entries(
            contest_id,
            target,
            allow_real_name_override=admin_override,
        )
    except ContestRealNameRosterForbidden as exc:
        audit_log(
            request,
            "contest_real_name_roster_override",
            result="fail",
            user=user.get("username"),
            target=contest_id,
            detail=(
                f"mode=bulk; assign_all={int(body.assign_all)}; "
                "reason=self_registration_required"
            ),
        )
        raise _contest_write_http_error(exc) from exc
    except ValueError as exc:
        if admin_override:
            audit_log(
                request,
                "contest_real_name_roster_override",
                result="fail",
                user=user.get("username"),
                target=contest_id,
                detail=(
                    f"mode=bulk; assign_all={int(body.assign_all)}; "
                    f"requested={len(target)}; reason=validation_failed"
                ),
            )
        raise _contest_write_http_error(exc) from exc
    actual_identity_override = bool(
        result.pop("_identity_required_at_commit", False)
    )
    if actual_identity_override:
        audit_log(
            request,
            "contest_real_name_roster_override",
            result="ok",
            user=user.get("username"),
            target=contest_id,
            detail=(
                f"mode=bulk; assign_all={int(body.assign_all)}; "
                f"requested={len(target)}; added={result['added']}; "
                f"skipped={len(result['skipped'])}"
            ),
        )
    return result


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
        raise _contest_write_http_error(exc) from exc
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
    # replace_top 的正式榜同时包含决赛选手和预赛未晋级者；积分只在各自
    # 来源阶段内可比较。先统一补齐读模型，再由 JSON/CSV 两种表示复用。
    stage_entry_ids: dict[int, set[int]] = {}
    for result in store.list_stage_results(contest_id):
        try:
            stage = int(result.get("stage_idx") or 0)
            entry = int(result["entry_id"])
        except (KeyError, TypeError, ValueError):
            continue
        stage_entry_ids.setdefault(stage, set()).add(entry)
    rows = with_official_result_provenance(
        c,
        rows,
        stage_entry_ids=stage_entry_ids,
    )
    # 正式成绩是公开能力。即使未来 Store 行扩展，也不允许实名/快照字段
    # 被 JSON 或 CSV 的通用 dict 流程顺带带出。
    rows = [_strip_contest_identity_fields(row) for row in rows]
    if format.lower() == "csv":
        import csv as _csv
        import io

        def gen():
            buf = io.StringIO()
            w = _csv.writer(buf)
            w.writerow(["rank", "entry_id", "bot_name", "owner_name", "points",
                        "buchholz_cut1", "sonneborn_berger", "awarded",
                        "source_stage", "ranking_cohort"])
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
                    _csv_safe_cell(r["rank"]),
                    _csv_safe_cell(r["entry_id"]),
                    _csv_safe_cell(r.get("bot_name") or ""),
                    _csv_safe_cell(r.get("owner_name") or ""),
                    _csv_safe_cell(r.get("points") or 0),
                    _csv_safe_cell(tb.get("buchholz_cut1", 0)),
                    _csv_safe_cell(tb.get("sonneborn_berger", 0)),
                    _csv_safe_cell(r.get("awarded") or ""),
                    _csv_safe_cell(r["source_stage"]),
                    _csv_safe_cell(r["ranking_cohort"]),
                ])
                yield buf.getvalue()
                buf.seek(0); buf.truncate(0)

        return StreamingResponse(
            gen(), media_type="text/csv",
            headers={
                "Content-Disposition": f'attachment; filename="contest-{contest_id}-results.csv"',
                "X-Content-Type-Options": "nosniff",
            },
        )
    # JSON 默认返回可直接展示的结构化破同分字段；不让前端猜测或解析数据库
    # 存储格式。CSV 分支仍从同一份持久值展开，二者共享事实来源。
    public_rows = []
    for row in rows:
        public = dict(row)
        raw_tiebreaks = public.pop("tiebreaks_json", None) or "{}"
        try:
            import json as _json
            parsed_tiebreaks = _json.loads(raw_tiebreaks)
        except (TypeError, ValueError):
            parsed_tiebreaks = {}
        public["tiebreaks"] = (
            parsed_tiebreaks if isinstance(parsed_tiebreaks, dict) else {}
        )
        public_rows.append(public)

    # json（默认）：返回结构化排名
    return {
        "contest_id": contest_id,
        "phase": c.get("phase") or "standalone",
        "ready": True,
        "results": public_rows,
    }


@router.get("/api/contests/{contest_id}/export")
def contest_export(
    contest_id: int,
    request: Request,
    format: str = "csv",
    schema: str | None = None,
):
    """Organizer/admin roster export.

    Omitting ``schema`` preserves the original 16-column CSV v1 contract.
    ``schema=2`` adds stable ids, account/Bot display names, identity provenance,
    stage/result context and human-readable Chinese statuses.  PII is projected
    only when the contest itself requires real-name registration.
    """
    def private_error(status_code: int, detail: str) -> HTTPException:
        return HTTPException(
            status_code,
            detail,
            headers=dict(_CONTEST_IDENTITY_PRIVATE_HEADERS),
        )

    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        raise private_error(404, "比赛不存在")
    if format.lower() not in ("csv",):
        raise private_error(400, "仅支持 format=csv")
    try:
        schema_version = 1 if schema is None else int(schema)
    except (TypeError, ValueError):
        raise private_error(400, "仅支持 schema=1 或 schema=2") from None
    if schema_version not in (1, 2):
        raise private_error(400, "仅支持 schema=1 或 schema=2")
    # 组织者鉴权（实名隐私——仅组织者/admin 可导出）。用 _extract_token + verify_session
    # 取当前用户（endpoint 无 Depends(require_user)，直接从 request 解析）。
    token = _extract_token(request)
    user = request.app.state.auth.verify_session(token) if token else None
    if not user:
        raise private_error(401, "未登录或会话过期")
    if c.get("organizer_id") != user.get("id") and user.get("role") != ROLE_ADMIN:
        raise private_error(403, "仅该场赛事组织者或管理员可操作")
    rows = store.list_contest_export(contest_id)
    # The contest object above is an authorization/status view from an earlier
    # autocommit SELECT.  Identity authorization must come from the same SQL
    # snapshot that projected each export row, otherwise delete-last/toggle/
    # reinsert can mix a stale gate with newer profile data.
    requires_identity = bool(
        rows
        and all(int(row.get("identity_required") or 0) == 1 for row in rows)
    )
    import csv as _csv
    import io

    contest_status_labels = {
        CONTEST_DRAFT: "草稿",
        CONTEST_OPEN: "报名中",
        CONTEST_PUBLISHED: "赛程已发布",
        CONTEST_RUNNING: "进行中",
        CONTEST_REST: "阶段间休息",
        CONTEST_FINISHED: "已结束",
        CONTEST_CANCELLED: "已取消",
    }

    def _stage_key(row: dict[str, Any]) -> str:
        value = row.get("stage_key")
        if value:
            return str(value)
        stage_idx = row.get("stage_idx")
        if stage_idx is None:
            return ""
        # The key is descriptive only; the stable zero-based stage_idx remains
        # an independent exported column and is never inferred from this label.
        raw_stages = c.get("stages_json") or "[]"
        try:
            normalized_stage_idx = int(stage_idx)
            stages = (
                json.loads(raw_stages)
                if isinstance(raw_stages, str)
                else raw_stages
            )
            stage = (
                stages[normalized_stage_idx]
                if isinstance(stages, list)
                else None
            )
        except (IndexError, TypeError, ValueError):
            return ""
        return (
            str(stage.get("key") or f"stage{normalized_stage_idx}")
            if isinstance(stage, dict)
            else f"stage{normalized_stage_idx}"
        )

    def _identity_source_label(value: object) -> str:
        return {
            CONTEST_IDENTITY_SOURCE_REGISTRATION: "报名时资料快照",
            CONTEST_IDENTITY_SOURCE_LEGACY: "历史报名：当前资料回退（非快照）",
        }.get(str(value or ""), "")

    def _entry_status(row: dict[str, Any]) -> str:
        if c.get("status") == CONTEST_CANCELLED:
            return "赛事已取消"
        if int(row.get("eliminated") or 0):
            return "已淘汰"
        if c.get("status") == CONTEST_FINISHED:
            return "已完赛"
        return "参赛中"

    def _result_status(row: dict[str, Any]) -> str:
        source = row.get("result_source")
        if source == "official":
            return "正式成绩"
        if source == "stage":
            return "阶段成绩"
        if c.get("status") == CONTEST_CANCELLED:
            return "赛事已取消"
        if c.get("status") == CONTEST_FINISHED:
            return "正式成绩待生成"
        return "暂无成绩"

    def gen_v1():
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
                _csv_safe_cell(
                    r.get("rank") if r.get("rank") is not None else ""
                ),
                _csv_safe_cell(r.get("seed") or 0),
                _csv_safe_cell(r.get("group_id") or ""),
                _csv_safe_cell(r.get("bot_name") or ""),
                _csv_safe_cell(r.get("owner_name") or ""),
                _csv_safe_cell(r.get("real_name") or ""),
                _csv_safe_cell(
                    r.get("phone") or "", force_text=bool(r.get("phone"))
                ),
                _csv_safe_cell(r.get("school") or ""),
                _csv_safe_cell(
                    r.get("student_id") or "",
                    force_text=bool(r.get("student_id")),
                ),
                _csv_safe_cell(
                    r.get("points") if r.get("points") is not None else ""
                ),
                _csv_safe_cell(
                    r.get("wins") if r.get("wins") is not None else ""
                ),
                _csv_safe_cell(
                    r.get("draws") if r.get("draws") is not None else ""
                ),
                _csv_safe_cell(
                    r.get("losses") if r.get("losses") is not None else ""
                ),
                _csv_safe_cell(int(bool(r.get("eliminated")))),
                _csv_safe_cell(r.get("awarded") or ""),
                _csv_safe_cell(r.get("registered_at") or ""),
            ])
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    v2_headers = [
        "报名ID(entry_id)",
        "用户ID(user_id)",
        "用户账号(username)",
        "用户显示名(user_display)",
        "Bot ID(bot_id)",
        "Bot内部名(bot_name)",
        "Bot显示名(bot_display)",
        "实名姓名(real_name)",
        "手机号(phone)",
        "学校(school)",
        "学号(student_id)",
        "实名来源(identity_source)",
        "实名采集时间(identity_captured_at)",
        "实名完整性(identity_completeness)",
        "正式名次(rank)",
        "种子(seed)",
        "分组(group_id)",
        "赛事状态(contest_status)",
        "参赛状态(entry_status)",
        "成绩状态(result_status)",
        "阶段索引(stage_idx)",
        "阶段标识(stage_key)",
        "积分(points)",
        "胜(wins)",
        "平(draws)",
        "负(losses)",
        "净分(delta_total)",
        "奖项(awarded)",
        "报名时间(registered_at)",
    ]

    def gen_v2():
        buf = io.StringIO()
        writer = _csv.writer(buf)
        yield "\ufeff"
        writer.writerow(v2_headers)
        yield buf.getvalue()
        buf.seek(0); buf.truncate(0)
        for row in rows:
            row_requires_identity = bool(
                int(row.get("identity_required") or 0)
            )
            writer.writerow(
                [
                    _csv_safe_cell(
                        row.get("entry_id")
                        if row.get("entry_id") is not None
                        else ""
                    ),
                    _csv_safe_cell(
                        row.get("user_id")
                        if row.get("user_id") is not None
                        else ""
                    ),
                    _csv_safe_cell(row.get("username") or ""),
                    _csv_safe_cell(row.get("user_display") or ""),
                    _csv_safe_cell(
                        row.get("bot_id")
                        if row.get("bot_id") is not None
                        else ""
                    ),
                    _csv_safe_cell(row.get("bot_name") or ""),
                    _csv_safe_cell(row.get("bot_display") or ""),
                    _csv_safe_cell(row.get("real_name") or ""),
                    _csv_safe_cell(
                        row.get("phone") or "",
                        force_text=bool(row.get("phone")),
                    ),
                    _csv_safe_cell(row.get("school") or ""),
                    _csv_safe_cell(
                        row.get("student_id") or "",
                        force_text=bool(row.get("student_id")),
                    ),
                    _csv_safe_cell(
                        _identity_source_label(row.get("identity_source"))
                    ),
                    _csv_safe_cell(row.get("identity_captured_at") or ""),
                    _csv_safe_cell((
                        "完整" if int(row.get("identity_complete") or 0) else "不完整"
                    ) if row_requires_identity else ""),
                    _csv_safe_cell(
                        row.get("rank") if row.get("rank") is not None else ""
                    ),
                    _csv_safe_cell(
                        row.get("seed")
                        if int(row.get("seed") or 0) > 0
                        else ""
                    ),
                    _csv_safe_cell(row.get("group_id") or ""),
                    _csv_safe_cell(
                        contest_status_labels.get(
                            str(c.get("status") or ""), "未知"
                        )
                    ),
                    _csv_safe_cell(_entry_status(row)),
                    _csv_safe_cell(_result_status(row)),
                    _csv_safe_cell(
                        row.get("stage_idx")
                        if row.get("stage_idx") is not None
                        else ""
                    ),
                    _csv_safe_cell(_stage_key(row)),
                    _csv_safe_cell(
                        row.get("points")
                        if row.get("points") is not None
                        else ""
                    ),
                    _csv_safe_cell(
                        row.get("wins") if row.get("wins") is not None else ""
                    ),
                    _csv_safe_cell(
                        row.get("draws")
                        if row.get("draws") is not None
                        else ""
                    ),
                    _csv_safe_cell(
                        row.get("losses")
                        if row.get("losses") is not None
                        else ""
                    ),
                    _csv_safe_cell(
                        row.get("delta_total")
                        if row.get("delta_total") is not None
                        else ""
                    ),
                    _csv_safe_cell(row.get("awarded") or ""),
                    _csv_safe_cell(row.get("registered_at") or ""),
                ]
            )
            yield buf.getvalue()
            buf.seek(0); buf.truncate(0)

    legacy_count = sum(
        1
        for row in rows
        if row.get("identity_source") == CONTEST_IDENTITY_SOURCE_LEGACY
    )
    audit_log(
        request,
        "contest_export",
        result="ok",
        user=user.get("username"),
        target=contest_id,
        detail=(
            f"schema={schema_version}; rows={len(rows)}; "
            f"identity={'required' if requires_identity else 'excluded'}; "
            f"legacy_fallback_rows={legacy_count}"
        ),
    )

    filename = (
        f"contest-{contest_id}-export.csv"
        if schema_version == 1
        else f"contest-{contest_id}-participants-v2.csv"
    )
    headers = {
        **_CONTEST_IDENTITY_PRIVATE_HEADERS,
        "Content-Disposition": f'attachment; filename="{filename}"',
    }

    return StreamingResponse(
        gen_v1() if schema_version == 1 else gen_v2(),
        media_type="text/csv; charset=utf-8",
        headers=headers,
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
        raise _contest_write_http_error(exc) from exc
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
        raise _contest_write_http_error(e) from e
    # 赛事报名经验
    from bzplat.backend.store.schema import XP_CONTEST_PARTICIPATE
    _store(request).award_xp(user["id"], XP_CONTEST_PARTICIPATE)
    return {"entry": _public_my_contest_entry(entry)}


@router.post("/api/contests/{contest_id}/dispatch")
async def dispatch_contest(
    contest_id: int, body: ContestDispatch, request: Request, user=Depends(require_user)
):
    try:
        entry = await _contests(request).dispatch(
            contest_id, user["id"], body.bot_id, role=user.get("role", "")
        )
    except ValueError as e:
        raise _contest_write_http_error(e) from e
    return {"entry": _public_my_contest_entry(entry)}


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
        raise _contest_write_http_error(e) from e
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
        raise _contest_write_http_error(e) from e
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
        raise _contest_write_http_error(e) from e
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
        raise _contest_write_http_error(e) from e
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
        raise _contest_write_http_error(e) from e
    return {"contest": _contest_for_api(contest)}


# ── admin ─────────────────────────────────────────────────────

_ADMIN_USER_RESPONSE_FIELDS = (
    "id", "username", "email", "role", "display_name", "is_active",
    "email_verified", "created_at", "last_login_at", "real_name", "phone",
    "school", "student_id",
)


def _admin_user_for_api(user: dict[str, Any]) -> dict[str, Any]:
    """Project one user through the admin response allowlist.

    Admin may need PII to verify tournament registrations, but authentication
    credentials and future Store-only columns must never become API fields.
    """
    return {field: user.get(field) for field in _ADMIN_USER_RESPONSE_FIELDS}


def _set_admin_private_headers(response: Response) -> None:
    response.headers.update(_ADMIN_PRIVATE_HEADERS)

@router.get("/api/admin/users")
def admin_users(
    request: Request, response: Response,
    q: str | None = None, real_name: bool | None = None,
    page: int | None = None, per_page: int = 50,
    _admin=Depends(require_admin),
):
    _set_admin_private_headers(response)
    result = _store(request).list_users(
        q=q, real_name=real_name, page=page, per_page=per_page,
    )
    if isinstance(result, dict):
        return {"users": [_admin_user_for_api(u) for u in result["items"]],
                "page": result["page"],
                "per_page": result["per_page"], "total": result["total"]}
    return {"users": [_admin_user_for_api(u) for u in result]}


@router.post("/api/admin/users/{user_id}/role")
def admin_set_role(
    user_id: int, role: str, request: Request, response: Response,
    admin=Depends(require_admin),
):
    _set_admin_private_headers(response)
    if role not in ("user", "organizer", "admin"):
        raise HTTPException(400, "非法角色", headers=_ADMIN_PRIVATE_HEADERS)
    u = _store(request).update_user(user_id, role=role)
    if not u:
        raise HTTPException(404, "用户不存在", headers=_ADMIN_PRIVATE_HEADERS)
    audit_log(request, "admin_set_role", result="ok", user=admin.get("username"), target=user_id, detail=f"role={role}")
    return {"user": _admin_user_for_api(u)}


class AdminUserPatch(BaseModel):
    is_active: bool | None = None
    email_verified: bool | None = None
    role: str | None = None


@router.patch("/api/admin/users/{user_id}")
async def admin_patch_user(
    user_id: int, body: AdminUserPatch, request: Request, response: Response,
    _admin=Depends(require_admin),
):
    _set_admin_private_headers(response)
    fields: dict[str, Any] = {}
    if body.is_active is not None:
        fields["is_active"] = 1 if body.is_active else 0
    if body.email_verified is not None:
        fields["email_verified"] = 1 if body.email_verified else 0
    if body.role is not None:
        if body.role not in ("user", "organizer", "admin"):
            raise HTTPException(400, "非法角色", headers=_ADMIN_PRIVATE_HEADERS)
        fields["role"] = body.role
    if not fields:
        raise HTTPException(400, "无更新字段", headers=_ADMIN_PRIVATE_HEADERS)
    public_ids = (
        _store(request).list_active_local_ai_public_ids_for_owner(user_id)
        if fields.get("is_active") == 0
        else []
    )
    u = _store(request).update_user(user_id, **fields)
    if not u:
        raise HTTPException(404, "用户不存在", headers=_ADMIN_PRIVATE_HEADERS)
    if public_ids:
        await _local_ai(request).revoke_public_ids(public_ids)
    return {"user": _admin_user_for_api(u)}


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
            "用户存在历史或活跃对局/赛事引用、退役规则版本审计证据，"
            "或仍是赛事组织者，不能硬删："
            f"{result['blockers']}（请改为停用账号；历史参赛身份必须保留）",
        )
    for bot_id in result["bot_ids"]:
        _bots(request).purge_bot_files(bot_id)
    audit_log(request, "admin_delete_user", result="ok", user=admin.get("username"), target=user_id)
    return {"ok": True}


@router.get("/api/admin/users/{user_id}/sessions")
def admin_user_sessions(
    user_id: int, request: Request, response: Response,
    _admin=Depends(require_admin),
):
    _set_admin_private_headers(response)
    return {"sessions": _store(request).list_sessions(user_id)}


@router.delete("/api/admin/users/{user_id}/sessions")
def admin_revoke_sessions(
    user_id: int, request: Request, response: Response,
    _admin=Depends(require_admin),
):
    _set_admin_private_headers(response)
    n = _store(request).delete_sessions_for_user(user_id)
    return {"ok": True, "revoked": n}


# ── admin: matches ─────────────────────────────────────────────

class AdminMatchPatch(BaseModel):
    model_config = {"extra": "forbid"}
    status: str | None = None


@router.patch("/api/admin/matches/{match_id}")
async def admin_patch_match(
    match_id: str, body: AdminMatchPatch, request: Request, admin=Depends(require_admin)
):
    """管理员只能经编排器中止活跃对局；原因固定为 admin_aborted。"""
    if body.status is None:
        raise HTTPException(400, "无更新字段")
    # 活跃对局的生命周期由 orchestrator/runner 独占。后台不能伪造
    # pending/running/completed，也不能让客户端注入第二套自由 reason。
    if body.status != "aborted":
        audit_log(
            request,
            "admin_abort_match",
            result="fail",
            user=admin.get("username"),
            target=match_id,
            detail=f"unsupported_status={body.status}",
        )
        raise HTTPException(409, "管理员仅可中止对局，不能手工伪造运行或完成状态")
    try:
        match = await _orch(request).abort_match(match_id)
    except ValueError as exc:
        audit_log(
            request,
            "admin_abort_match",
            result="fail",
            user=admin.get("username"),
            target=match_id,
            detail=str(exc),
        )
        code = 404 if "不存在" in str(exc) else 409
        raise HTTPException(code, str(exc)) from exc
    except Exception as exc:
        logger.exception("admin abort failed match=%s", match_id)
        audit_log(
            request,
            "admin_abort_match",
            result="fail",
            user=admin.get("username"),
            target=match_id,
            detail="internal_error",
        )
        raise HTTPException(500, "中止对局失败") from exc
    audit_log(
        request,
        "admin_abort_match",
        result="ok",
        user=admin.get("username"),
        target=match_id,
        detail=f"reason={match.get('reason') or 'platform_error'}",
    )
    return {"match": match}


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
async def admin_patch_bot(
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
    public_ids = (
        _store(request).list_active_local_ai_public_ids_for_bot(bot_id)
        if fields.get("is_active") == 0
        else []
    )
    try:
        bot = _bots(request).patch_admin(bot_id, **fields)
    except BotError as exc:
        status = 404 if exc.code == "not_found" else 409 if exc.code in {
            "unsupported_binary", "version_unavailable",
        } else 400
        raise HTTPException(
            status, detail={"code": exc.code, "message": exc.message}
        ) from exc
    if public_ids:
        await _local_ai(request).revoke_public_ids(public_ids)
    return {"bot": _with_bot_runnable(bot)}


@router.delete("/api/admin/bots/{bot_id}")
def admin_delete_bot(bot_id: int, request: Request, admin=Depends(require_admin)):
    store = _store(request)
    # 业务规则：仅从未参赛的 Bot 可硬删。SET NULL 虽能保住比赛行，却会永久丢失
    # “哪个用户的哪个 Bot”这一公开历史身份；已有任何对局或赛事记录时必须改用停用。
    result = store.delete_bot_if_safe(bot_id)
    if not result["found"]:
        raise HTTPException(404, "bot 不存在")
    refs = result["references"]
    if not result["deleted"]:
        raise HTTPException(
            409,
            f"bot 存在历史或活跃引用、退役规则版本审计证据，不能硬删：{refs}"
            "（请改用停用 is_active=0，保留公开参赛身份与版本审计）",
        )
    # 硬删 bot 后清理磁盘文件（bot_uploads/<id>/），避免孤儿
    _bots(request).purge_bot_files(bot_id)
    audit_log(request, "admin_delete_bot", result="ok", user=admin.get("username"), target=bot_id)
    return {"ok": True}


@router.get("/api/admin/bots/{bot_id}/versions")
def admin_bot_versions(
    bot_id: int, request: Request, _admin=Depends(require_admin)
):
    store = _store(request)
    bot = store.get_bot(bot_id)
    if not bot:
        raise HTTPException(404, "bot 不存在")
    active_protocol = store.get_active_game_contract(bot["game_id"])[
        "protocol_version"
    ]
    versions = []
    for raw_version in store.list_bot_versions(bot_id):
        version = _with_bot_runnable(raw_version)
        if raw_version.get("retired_at") is not None:
            version["runnable"] = False
            version["unsupported_reason"] = "该版本已退役"
        elif raw_version.get("protocol_version") != active_protocol:
            version["runnable"] = False
            version["unsupported_reason"] = "协议版本与当前游戏规则不兼容"
        versions.append(version)
    return {
        "versions": versions
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
            require_mutable_contest(before)
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
            raise _contest_write_http_error(exc) from exc

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
        # 时间修改须进入 ContestManager 的 per-contest 锁。published 调整
        # starts_at 时，manager 会按原发布公式重算 pending pairing，并由
        # Store 在同一事务写入赛事与逐场排期。
        c = await _contests(request).revise_schedule(contest_id, fields)
    except ValueError as e:
        audit_log(
            request, "admin_patch_contest_fields", result="fail",
            user=admin.get("username"), target=contest_id, detail=str(e),
        )
        raise _contest_write_http_error(e) from e
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
    contest_id: int,
    request: Request,
    response: Response,
    _admin=Depends(require_admin),
):
    store = _store(request)
    contest = store.get_contest(contest_id)
    if not contest:
        raise HTTPException(404, "赛事不存在")
    include_identity = bool(int(contest.get("require_real_name") or 0))
    if include_identity:
        response.headers.update(_CONTEST_IDENTITY_PRIVATE_HEADERS)
    return {
        "entries": store.contest_entries_named(
            contest_id, include_identity=include_identity
        )
    }


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
    contest_id: int,
    body: AdminAssignEntries,
    request: Request,
    admin=Depends(require_admin),
):
    """管理员批量指派参赛者+Bot。绕开 register() 的 CONTEST_OPEN + owner 校验（admin 专享）。
    校验：bot 存在+active、bot.game_id==contest.game_id、用户未重复报名。"""
    store = _store(request)
    c = store.get_contest(contest_id)
    if not c:
        audit_log(
            request,
            "admin_assign_entries",
            result="fail",
            user=admin.get("username"),
            target=contest_id,
            detail="real_name_override=0; reason=contest_missing",
        )
        raise HTTPException(404, "赛事不存在")
    requires_identity = bool(int(c.get("require_real_name") or 0))
    from bzplat.backend.games import normalize_game_id
    try:
        cgid = normalize_game_id(c.get("game_id"))
    except ValueError as exc:
        audit_log(
            request,
            "admin_assign_entries",
            result="fail",
            user=admin.get("username"),
            target=contest_id,
            detail=(
                f"real_name_override={int(requires_identity)}; "
                "reason=contest_game_invalid"
            ),
        )
        raise HTTPException(400, str(exc)) from exc

    # 解析目标 entries 列表
    if body.assign_all:
        try:
            gid = normalize_game_id(
                cgid if body.game_id is None else body.game_id
            )
        except ValueError as exc:
            audit_log(
                request,
                "admin_assign_entries",
                result="fail",
                user=admin.get("username"),
                target=contest_id,
                detail=(
                    f"real_name_override={int(requires_identity)}; "
                    "reason=invalid_game"
                ),
            )
            raise HTTPException(400, str(exc)) from exc
        if gid != cgid:
            audit_log(
                request,
                "admin_assign_entries",
                result="fail",
                user=admin.get("username"),
                target=contest_id,
                detail=(
                    f"real_name_override={int(requires_identity)}; "
                    "reason=game_mismatch"
                ),
            )
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
            audit_log(
                request,
                "admin_assign_entries",
                result="fail",
                user=admin.get("username"),
                target=contest_id,
                detail=(
                    f"real_name_override={int(requires_identity)}; "
                    "reason=invalid_ids"
                ),
            )
            raise HTTPException(400, "user_id / bot_id 必须是整数")

    try:
        result = await _contests(request).assign_roster_entries(
            contest_id,
            target,
            allow_real_name_override=True,
        )
    except ValueError as exc:
        failure_requires_identity = (
            exc.identity_required_at_commit
            if isinstance(exc, ContestRosterWriteValidationError)
            else requires_identity
        )
        audit_log(
            request,
            "admin_assign_entries",
            result="fail",
            user=admin.get("username"),
            target=contest_id,
            detail=(
                f"real_name_override={int(failure_requires_identity)}; "
                f"requested={len(target)}; reason=validation_failed"
            ),
        )
        raise _contest_write_http_error(exc) from exc
    actual_identity_override = bool(
        result.pop("_identity_required_at_commit", False)
    )
    audit_log(
        request,
        "admin_assign_entries",
        result="ok",
        user=admin.get("username"),
        target=contest_id,
        detail=(
            f"real_name_override={int(actual_identity_override)}; requested={len(target)}; "
            f"added={result['added']}; skipped={len(result['skipped'])}"
        ),
    )
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
        raise _contest_write_http_error(exc) from exc
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
    model_config = {"extra": "forbid"}

    subject: str
    body_html: str = ""
    body_text: str = ""


@router.get("/api/admin/email/templates")
def admin_templates(request: Request, _admin=Depends(require_admin)):
    from bzplat.backend.communications.templates import list_templates

    legacy = {row["key"]: row for row in _store(request).list_templates()}
    templates = []
    for item in list_templates():
        old = legacy.get(item.key)
        customized = bool(old and (
            old["subject"] != item.subject
            or old["body_html"] != item.body_html
            or old["body_text"] != item.body_text
        ))
        templates.append({
            "key": item.key,
            "version": item.version,
            "subject": item.subject,
            "body_html": item.body_html,
            "body_text": item.body_text,
            "secret": item.secret,
            "source": "code",
            "mutable": False,
            "legacy_customization_preserved": customized,
        })
    return {
        "source": "code",
        "mutable": False,
        "legacy_rows_preserved": True,
        "templates": templates,
    }


@router.get("/api/admin/email/templates/{key}")
def admin_template(key: str, request: Request, _admin=Depends(require_admin)):
    from bzplat.backend.communications.templates import get_template

    try:
        item = get_template(key)
    except KeyError:
        raise HTTPException(404, "模板不存在")
    return {"template": {
        "key": item.key,
        "version": item.version,
        "subject": item.subject,
        "body_html": item.body_html,
        "body_text": item.body_text,
        "secret": item.secret,
        "source": "code",
        "mutable": False,
    }}


@router.put("/api/admin/email/templates/{key}")
def admin_update_template(
    key: str, body: TemplateUpdate, request: Request, admin=Depends(require_admin)
):
    audit_log(
        request,
        "admin_email_template_update",
        result="fail",
        user=admin.get("username"),
        target=key,
        detail="code_owned",
    )
    raise HTTPException(
        409,
        "事务邮件模板由代码版本管理；旧 email_templates 自定义记录已保留但不再执行",
    )


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


# ── admin: runtime diagnostics + 唯一自动排位总开关 ─────────
@router.get("/api/admin/settings/runtime")
def admin_get_runtime(request: Request, _admin=Depends(require_admin)):
    store = _store(request)
    orch = _orch(request)
    stats = store.count_stats()
    dispatcher = getattr(request.app.state, "execution_dispatcher", None)
    queue_snapshot = dispatcher.public_snapshot(include_internal=True) if dispatcher is not None else {
        "dispatcher": {
            "state": "stopped", "accepting": False, "auto_enabled": False,
            "maintenance": False, "pause_reason": "调度器未就绪", "retry_at": None,
        },
        "capacity": {}, "active": [], "queued": [], "queued_count": 0,
        "maintenance": {
            "requested": False, "ready": False, "reason": "",
            "active_count": 0, "uploads_in_flight": 0,
            "active_local_ai_leases": 0, "docker_launch_state": "unknown",
            "owned_execution_tasks": 0,
            "readiness_unavailable": ["dispatcher"],
        },
    }
    return {
        "source": CONFIGURATION_SOURCE,
        "mutable": False,
        "cpu_count": cpu_count(),
        "ceiling": concurrent_ceiling(),
        "action_timeout_sec": ACTION_TIMEOUT_SEC,
        "max_concurrent_matches": orch.max_concurrent,
        "effective_concurrent": orch.max_concurrent,
        "bot_cpus": BOT_CPUS,
        "bot_memory_mb": BOT_MEMORY_MB,
        "full_rr_max_n": FULL_RR_MAX_N,
        "ranking_min_rated_matches": RANKING_MIN_RATED_MATCHES,
        "contest_scheduler": CONTEST_SCHEDULER_CONFIG.as_dict(),
        "queue": queue_snapshot,
        "auto_match": {
            "enabled": queue_snapshot["dispatcher"]["auto_enabled"],
            "mutable": True,
        },
        "rating_integrity": store.rating_integrity_diagnostics(),
        "readonly": [
            "action_timeout_sec",
            "max_concurrent_matches",
            "bot_cpus",
            "bot_memory_mb",
            "full_rr_max_n",
            "contest_scheduler",
        ],
    }


class AutoMatchToggle(BaseModel):
    enabled: StrictBool

    model_config = {"extra": "forbid"}


@router.put("/api/admin/auto-match")
async def admin_toggle_auto_match(
    body: AutoMatchToggle,
    request: Request,
    admin=Depends(require_admin),
):
    scheduler = getattr(request.app.state, "execution_dispatcher", None)
    if scheduler is None:
        audit_log(
            request,
            "admin_auto_match_toggle",
            result="fail",
            user=admin.get("username"),
            detail="scheduler_unavailable",
        )
        raise HTTPException(503, "自动排位调度器未就绪")
    if body.enabled and not scheduler.auto_capability_enabled:
        audit_log(
            request,
            "admin_auto_match_toggle",
            result="deny",
            user=admin.get("username"),
            detail="qa_capability_guard",
        )
        raise HTTPException(409, "隔离 QA 实例禁止开启自动排位")
    previous = _store(request).get_auto_match_enabled()
    try:
        _store(request).set_auto_match_enabled(bool(body.enabled))
    except ExecutionMaintenanceConflict as exc:
        audit_log(
            request,
            "admin_auto_match_toggle",
            result="deny",
            user=admin.get("username"),
            detail=exc.code,
        )
        raise HTTPException(
            409, detail={"code": exc.code, "message": exc.message}
        ) from exc
    scheduler.wake()
    audit_log(
        request,
        "admin_auto_match_toggle",
        result="ok",
        user=admin.get("username"),
        detail=f"enabled={int(bool(body.enabled))} previous={int(previous)}",
    )
    return scheduler.public_snapshot(include_internal=True)


@router.post("/api/admin/execution-queue/resume")
async def admin_resume_execution_queue(
    request: Request,
    admin=Depends(require_admin),
):
    dispatcher = _execution_dispatcher(request)
    resumed = await dispatcher.admin_resume()
    audit_log(
        request,
        "admin_execution_queue_resume",
        result="ok" if resumed else "paused",
        user=admin.get("username"),
    )
    return dispatcher.public_snapshot(include_internal=True)


class DeploymentMaintenanceBody(BaseModel):
    reason: str = Field(default="管理员准备部署", max_length=200)

    model_config = {"extra": "forbid"}


def _maintenance_conflict(exc: ExecutionMaintenanceConflict) -> HTTPException:
    return HTTPException(
        409, detail={"code": exc.code, "message": exc.message}
    )


@router.get("/api/admin/execution-queue/maintenance")
def admin_get_execution_maintenance(
    request: Request,
    _admin=Depends(require_admin),
):
    return _execution_dispatcher(request).public_snapshot(
        include_internal=True
    )


@router.post("/api/admin/execution-queue/maintenance")
async def admin_begin_execution_maintenance(
    body: DeploymentMaintenanceBody,
    request: Request,
    admin=Depends(require_admin),
):
    dispatcher = _execution_dispatcher(request)
    activity_lock = getattr(
        request.app.state, "bot_upload_activity_lock", None
    )
    activity = getattr(request.app.state, "bot_upload_activity", None)
    contest_manager = getattr(request.app.state, "contest_manager", None)
    contest_activity_lock = getattr(
        contest_manager, "deployment_activity_lock", None
    )
    if (
        activity_lock is None
        or activity is None
        or contest_activity_lock is None
    ):
        raise HTTPException(503, "上传活动计数器未就绪，不能安全排空")
    try:
        async with activity_lock:
            async with contest_activity_lock:
                before = dispatcher.public_snapshot(include_internal=True)
                dispatcher.begin_maintenance(body.reason)
                after = dispatcher.public_snapshot(include_internal=True)
    except ExecutionMaintenanceConflict as exc:
        audit_log(
            request,
            "admin_execution_maintenance_begin",
            result="conflict",
            user=admin.get("username"),
            detail=exc.code,
        )
        raise _maintenance_conflict(exc) from exc
    audit_log(
        request,
        "admin_execution_maintenance_begin",
        result="ok",
        user=admin.get("username"),
        detail=(
            "requested="
            f"{int(bool(before['maintenance']['requested']))}->"
            f"{int(bool(after['maintenance']['requested']))} "
            "accepting="
            f"{int(bool(before['dispatcher']['accepting']))}->"
            f"{int(bool(after['dispatcher']['accepting']))} "
            "auto_enabled="
            f"{int(bool(before['dispatcher']['auto_enabled']))}->"
            f"{int(bool(after['dispatcher']['auto_enabled']))} "
            f"active={after['maintenance']['active_count']} "
            f"queued={after['queued_count']}"
        ),
    )
    return after


@router.delete("/api/admin/execution-queue/maintenance")
async def admin_end_execution_maintenance(
    request: Request,
    admin=Depends(require_admin),
):
    dispatcher = _execution_dispatcher(request)
    activity_lock = getattr(
        request.app.state, "bot_upload_activity_lock", None
    )
    activity = getattr(request.app.state, "bot_upload_activity", None)
    if activity_lock is None or activity is None:
        raise HTTPException(503, "上传活动计数器未就绪，不能安全恢复")
    try:
        async with activity_lock:
            before = dispatcher.public_snapshot(include_internal=True)
            await dispatcher.end_maintenance()
            after = dispatcher.public_snapshot(include_internal=True)
    except ExecutionMaintenanceConflict as exc:
        audit_log(
            request,
            "admin_execution_maintenance_end",
            result="conflict",
            user=admin.get("username"),
            detail=exc.code,
        )
        raise _maintenance_conflict(exc) from exc
    audit_log(
        request,
        "admin_execution_maintenance_end",
        result="ok",
        user=admin.get("username"),
        detail=(
            "requested="
            f"{int(bool(before['maintenance']['requested']))}->"
            f"{int(bool(after['maintenance']['requested']))} "
            "accepting="
            f"{int(bool(before['dispatcher']['accepting']))}->"
            f"{int(bool(after['dispatcher']['accepting']))} "
            "auto_enabled="
            f"{int(bool(before['dispatcher']['auto_enabled']))}->"
            f"{int(bool(after['dispatcher']['auto_enabled']))}"
        ),
    )
    return after


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
    {"slug": "local-ai", "file": "LOCAL_AI.md", "title": "本地 Bot 接入", "summary": "在自己的电脑运行 Bot，由平台负责裁判、回放与技术判定"},
    {"slug": "texas", "file": "TEXAS.md", "title": "德州扑克 (TexasHoldem2p)", "summary": "固定 70 手规则、请求字段与完整示例"},
    {"slug": "gomoku", "file": "GOMOKU.md", "title": "五子棋 (Gomoku)", "summary": "指定开局、交换、五手二打、禁手与 v2 示例"},
    {"slug": "pencil", "file": "PENCIL.md", "title": "点格棋 (Pencil)", "summary": "N=6 规则、900 秒棋钟、协议与示例"},
    {"slug": "guide", "file": "GUIDE.md", "title": "平台功能指南", "summary": "对局/裁判/数值评分/等级/锦标赛/Bot详情/用户主页/社交/通知/设置——一页看全"},
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
