"""Strict REST boundary for conversations, broadcasts and bug feedback."""
from __future__ import annotations

from typing import Annotated, Literal, Union

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel, Field, StrictBool
from starlette.datastructures import UploadFile

from bzplat.backend.auth.dependencies import optional_user, require_admin, require_user
from bzplat.backend.security import _env_bool, audit_log
from bzplat.backend.store.schema import ROLE_ADMIN

from .diagnostics import build_diagnostic_bundle
from .feedback import FeedbackService, MAX_ATTACHMENT_BYTES
from .repository import (
    CommunicationConflict,
    CommunicationForbidden,
    CommunicationNotFound,
)
from .service import CommunicationService

router = APIRouter(tags=["communications"])


def _service(request: Request) -> CommunicationService:
    return request.app.state.communications


def _feedback(request: Request) -> FeedbackService:
    return request.app.state.feedback


def _domain_error(exc: Exception) -> HTTPException:
    if isinstance(exc, CommunicationNotFound):
        return HTTPException(404, str(exc))
    if isinstance(exc, CommunicationForbidden):
        return HTTPException(403, str(exc))
    if isinstance(exc, CommunicationConflict):
        return HTTPException(409, str(exc))
    if isinstance(exc, ValueError):
        return HTTPException(400, str(exc))
    return HTTPException(500, "通信服务异常")


class StrictModel(BaseModel):
    model_config = {"extra": "forbid"}


class ReplyBody(StrictModel):
    body: str = Field(..., min_length=1, max_length=10_000)
    reply_to: str | None = Field(None, max_length=80)
    email: StrictBool = False


@router.get("/api/communications/inbox")
def user_inbox(
    request: Request,
    page: int = 1,
    per_page: int = 30,
    user=Depends(require_user),
):
    return _service(request).repository.list_threads(
        user_id=user["id"], box="inbox", page=page, per_page=per_page
    )


@router.get("/api/communications/sent")
def user_sent(
    request: Request,
    page: int = 1,
    per_page: int = 30,
    user=Depends(require_user),
):
    return _service(request).repository.list_threads(
        user_id=user["id"], box="sent", page=page, per_page=per_page
    )


@router.get("/api/communications/threads/{conversation_public_id}")
def user_thread(
    conversation_public_id: str,
    request: Request,
    response: Response,
    user=Depends(require_user),
):
    _no_store(response)
    try:
        return _service(request).repository.get_thread(
            conversation_public_id, user_id=user["id"]
        )
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/api/communications/threads/{conversation_public_id}/read")
def user_read_thread(
    conversation_public_id: str,
    request: Request,
    user=Depends(require_user),
):
    try:
        marker = _service(request).repository.mark_read(
            conversation_public_id, user["id"]
        )
        return {"ok": True, "read_through_message_public_id": marker}
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/api/communications/threads/{conversation_public_id}/reply")
def user_reply_thread(
    conversation_public_id: str,
    body: ReplyBody,
    request: Request,
    user=Depends(require_user),
):
    if body.email:
        raise HTTPException(400, "用户回复不能指定邮件投递")
    try:
        message = _service(request).reply_user(
            conversation_public_id,
            user_id=user["id"],
            body_text=body.body,
            reply_to=body.reply_to,
        )
        return {"message": message}
    except Exception as exc:
        raise _domain_error(exc) from exc


# ── admin conversation queues ───────────────────────────────────────

@router.get("/api/admin/communications/inbox")
def admin_inbox(
    request: Request,
    page: int = 1,
    per_page: int = 30,
    _admin=Depends(require_admin),
):
    return _service(request).repository.list_threads(
        admin=True, box="inbox", page=page, per_page=per_page
    )


@router.get("/api/admin/communications/sent")
def admin_sent(
    request: Request,
    page: int = 1,
    per_page: int = 30,
    _admin=Depends(require_admin),
):
    return _service(request).repository.list_threads(
        admin=True, box="sent", page=page, per_page=per_page
    )


@router.get("/api/admin/communications/drafts")
def admin_drafts(
    request: Request,
    page: int = 1,
    per_page: int = 30,
    _admin=Depends(require_admin),
):
    return _service(request).repository.list_broadcast_drafts(
        page=page, per_page=per_page
    )


@router.get("/api/admin/communications/failed")
def admin_failed(
    request: Request,
    page: int = 1,
    per_page: int = 30,
    _admin=Depends(require_admin),
):
    return _service(request).repository.list_failed_deliveries(
        page=page, per_page=per_page
    )


@router.get("/api/admin/communications/threads/{conversation_public_id}")
def admin_thread(
    conversation_public_id: str,
    request: Request,
    response: Response,
    _admin=Depends(require_admin),
):
    _no_store(response)
    try:
        return _service(request).repository.get_thread(
            conversation_public_id, admin=True
        )
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/api/admin/communications/threads/{conversation_public_id}/reply")
def admin_reply_thread(
    conversation_public_id: str,
    body: ReplyBody,
    request: Request,
    admin=Depends(require_admin),
):
    try:
        message = _service(request).reply_admin(
            conversation_public_id,
            admin_user_id=admin["id"],
            body_text=body.body,
            reply_to=body.reply_to,
            queue_email=body.email,
        )
        audit_log(
            request,
            "admin_communication_reply",
            result="ok",
            user=admin.get("username"),
            target=conversation_public_id,
        )
        return {"message": message}
    except Exception as exc:
        audit_log(
            request,
            "admin_communication_reply",
            result="fail",
            user=admin.get("username"),
            target=conversation_public_id,
            detail=type(exc).__name__,
        )
        raise _domain_error(exc) from exc


# ── fixed-snapshot, two-step broadcasts ─────────────────────────────

class ActiveAudience(StrictModel):
    kind: Literal["active_users"]


class RoleAudience(StrictModel):
    kind: Literal["role"]
    role: Literal["user", "organizer", "admin"]


class GameOwnersAudience(StrictModel):
    kind: Literal["game_bot_owners"]
    game_id: Literal["holdem", "gomoku", "pencil"]


class ContestAudience(StrictModel):
    kind: Literal["contest_entrants"]
    contest_id: int = Field(..., gt=0)


PublicUsername = Annotated[
    str,
    Field(min_length=3, max_length=32, pattern=r"^[A-Za-z][A-Za-z0-9_]*$"),
]


class SelectedUsersAudience(StrictModel):
    kind: Literal["selected_users"]
    usernames: list[PublicUsername] = Field(..., min_length=1, max_length=500)


Audience = Annotated[
    Union[
        ActiveAudience,
        RoleAudience,
        GameOwnersAudience,
        ContestAudience,
        SelectedUsersAudience,
    ],
    Field(discriminator="kind"),
]


class BroadcastPreviewBody(StrictModel):
    audience: Audience
    subject: str = Field(..., min_length=1, max_length=160)
    body: str = Field(..., min_length=1, max_length=20_000)
    channels: list[Literal["in_app", "email"]] = Field(
        default_factory=lambda: ["in_app"], min_length=1, max_length=2
    )


class BroadcastCreateBody(StrictModel):
    preview_public_id: str = Field(..., min_length=8, max_length=80)
    approval_token: str = Field(..., min_length=20, max_length=200)
    confirm: StrictBool
    scheduled_at: str | None = Field(None, max_length=40)


class BroadcastRetryBody(StrictModel):
    recipient_public_ids: list[str] = Field(default_factory=list, max_length=100)
    delivery_public_ids: list[str] = Field(default_factory=list, max_length=100)


@router.post("/api/admin/communications/broadcasts/preview")
def broadcast_preview(
    body: BroadcastPreviewBody,
    request: Request,
    admin=Depends(require_admin),
):
    audience = body.audience.model_dump()
    kind = audience.pop("kind")
    try:
        result = _service(request).preview_broadcast(
            admin_user_id=admin["id"],
            audience_kind=kind,
            audience_filter=audience,
            subject=body.subject,
            body_text=body.body,
            channels=list(body.channels),
        )
    except Exception as exc:
        audit_log(
            request,
            "broadcast_preview",
            result="fail",
            user=admin.get("username"),
            detail=f"audience={kind} error={type(exc).__name__}",
        )
        raise _domain_error(exc) from exc
    audit_log(
        request,
        "broadcast_preview",
        result="ok",
        user=admin.get("username"),
        target=result["public_id"],
        detail=f"audience={kind} count={result['audience_count']}",
    )
    return {"dry_run": True, "broadcast": result}


@router.post("/api/admin/communications/broadcasts/create")
def broadcast_create(
    body: BroadcastCreateBody,
    request: Request,
    admin=Depends(require_admin),
):
    if body.confirm is not True:
        audit_log(
            request,
            "broadcast_approve",
            result="fail",
            user=admin.get("username"),
            target=body.preview_public_id,
            detail="confirm_required",
        )
        raise HTTPException(400, "必须明确 confirm=true 完成二次批准")
    try:
        result = _service(request).repository.approve_broadcast(
            body.preview_public_id,
            actor_user_id=admin["id"],
            approval_token=body.approval_token,
            scheduled_at=body.scheduled_at,
        )
    except Exception as exc:
        audit_log(
            request,
            "broadcast_approve",
            result="fail",
            user=admin.get("username"),
            target=body.preview_public_id,
            detail=type(exc).__name__,
        )
        raise _domain_error(exc) from exc
    audit_log(
        request,
        "broadcast_approve",
        result="ok",
        user=admin.get("username"),
        target=result["public_id"],
        detail=f"count={result['audience_count']}",
    )
    return {"broadcast": result}


@router.post("/api/admin/communications/broadcasts/{broadcast_public_id}/cancel")
def broadcast_cancel(
    broadcast_public_id: str,
    request: Request,
    admin=Depends(require_admin),
):
    try:
        result = _service(request).repository.cancel_broadcast(broadcast_public_id)
    except Exception as exc:
        audit_log(
            request,
            "broadcast_cancel",
            result="fail",
            user=admin.get("username"),
            target=broadcast_public_id,
            detail=type(exc).__name__,
        )
        raise _domain_error(exc) from exc
    audit_log(
        request,
        "broadcast_cancel",
        result="ok",
        user=admin.get("username"),
        target=broadcast_public_id,
    )
    return {"broadcast": result}


@router.get("/api/admin/communications/broadcasts")
def broadcast_list(
    request: Request,
    state: str | None = None,
    page: int = 1,
    per_page: int = 30,
    _admin=Depends(require_admin),
):
    try:
        return _service(request).repository.list_broadcasts(
            state=state, page=page, per_page=per_page
        )
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.get("/api/admin/communications/broadcasts/{broadcast_public_id}")
def broadcast_detail(
    broadcast_public_id: str,
    request: Request,
    _admin=Depends(require_admin),
):
    try:
        return {
            "broadcast": _service(request).repository.get_broadcast_detail(
                broadcast_public_id
            )
        }
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/api/admin/communications/broadcasts/{broadcast_public_id}/retry-failed")
def broadcast_retry_failed(
    broadcast_public_id: str,
    body: BroadcastRetryBody,
    request: Request,
    admin=Depends(require_admin),
):
    selected_count = len(set(body.recipient_public_ids)) + len(
        set(body.delivery_public_ids)
    )
    if selected_count < 1 or selected_count > 100:
        raise HTTPException(400, "每次必须选择 1-100 个失败项")
    try:
        result = _service(request).repository.retry_failed_broadcast_work(
            broadcast_public_id,
            recipient_public_ids=body.recipient_public_ids,
            delivery_public_ids=body.delivery_public_ids,
        )
    except Exception as exc:
        audit_log(
            request,
            "broadcast_retry_failed",
            result="fail",
            user=admin.get("username"),
            target=broadcast_public_id,
            detail=type(exc).__name__,
        )
        raise _domain_error(exc) from exc
    audit_log(
        request,
        "broadcast_retry_failed",
        result="ok",
        user=admin.get("username"),
        target=broadcast_public_id,
        detail=(
            f"selected={selected_count} "
            f"retried={len(result['retried_recipients']) + len(result['retried_deliveries'])} "
            f"ignored={len(result['ignored'])} exhausted={len(result['exhausted'])}"
        ),
    )
    return {"retry": result}


@router.get("/api/admin/communications/broadcasts/{broadcast_public_id}/deliveries")
def broadcast_deliveries(
    broadcast_public_id: str,
    request: Request,
    _admin=Depends(require_admin),
):
    try:
        return {"stats": _service(request).repository.broadcast_stats(broadcast_public_id)}
    except Exception as exc:
        raise _domain_error(exc) from exc


# ── beginner bug feedback ───────────────────────────────────────────

class DiagnosticInput(StrictModel):
    browser_family: Literal["chrome", "firefox", "safari", "edge", "other", "unknown"] = "unknown"
    os_family: Literal["windows", "macos", "linux", "android", "ios", "other", "unknown"] = "unknown"
    viewport_width: int | None = Field(None, ge=240, le=16_384)
    viewport_height: int | None = Field(None, ge=240, le=16_384)
    locale: str = Field("", max_length=20)
    timezone: str = Field("", max_length=64)
    theme: Literal["light", "dark", "system", "unknown"] = "unknown"
    failed_api_template: Literal[
        "/api/auth/*",
        "/api/bots/*",
        "/api/matches/*",
        "/api/contests/*",
        "/api/communications/*",
        "/api/feedback/bugs",
        "/api/notifications",
    ] | None = None
    failed_api_status: int | None = Field(None, ge=100, le=599)
    trace_id: str = Field("", max_length=64)
    public_match_id: str | None = Field(None, max_length=80)
    contest_id: int | None = Field(None, gt=0)


class BugCreateBody(StrictModel):
    category: Literal["match", "bot", "contest", "account", "page", "other"]
    impact: Literal["blocked", "major", "minor", "cosmetic"]
    title: str = Field(..., min_length=1, max_length=160)
    body: str = Field(..., min_length=1, max_length=20_000)
    current_route: str = Field("", max_length=200)
    diagnostics: DiagnosticInput = Field(default_factory=DiagnosticInput)
    captcha_id: str | None = Field(None, max_length=200)
    captcha_answer: str | None = Field(None, max_length=100)


class BugStatusBody(StrictModel):
    status: Literal[
        "acknowledged", "needs_info", "in_progress", "resolved", "duplicate", "wont_fix"
    ]
    note: str = Field("", max_length=2_000)
    duplicate_of: str | None = Field(None, max_length=80)


@router.post("/api/feedback/bugs")
def create_bug_report(
    body: BugCreateBody,
    request: Request,
    response: Response,
    user=Depends(optional_user),
):
    if user is None:
        # Anonymous creation returns a bearer-like tracking token.  Prevent browser,
        # proxy and referrer persistence of that response.
        _no_store(response)
        if not _env_bool("BZ_SKIP_CAPTCHA", False):
            captcha = request.app.state.captcha
            if not captcha.verify(body.captcha_id or "", body.captcha_answer or ""):
                audit_log(request, "bug_report_create", result="fail", detail="captcha")
                raise HTTPException(400, "图形验证码错误或已过期")
    diag = body.diagnostics
    try:
        bundle = build_diagnostic_bundle(
            request.app.state.store,
            current_route=body.current_route,
            role=(user.get("role") if user else "guest"),
            browser_family=diag.browser_family,
            os_family=diag.os_family,
            viewport_width=diag.viewport_width,
            viewport_height=diag.viewport_height,
            locale=diag.locale,
            timezone=diag.timezone,
            theme=diag.theme,
            failed_api_template=diag.failed_api_template,
            failed_api_status=diag.failed_api_status,
            trace_id=diag.trace_id,
            public_match_id=diag.public_match_id,
            contest_id=diag.contest_id,
        )
        result = _feedback(request).create_report(
            reporter_user_id=(int(user["id"]) if user else None),
            category=body.category,
            impact=body.impact,
            title=body.title,
            body=body.body,
            current_route=bundle["route"],
            diagnostics=bundle,
        )
    except Exception as exc:
        audit_log(
            request,
            "bug_report_create",
            result="fail",
            user=(user.get("username") if user else None),
            detail=type(exc).__name__,
        )
        raise _domain_error(exc) from exc
    audit_log(
        request,
        "bug_report_create",
        result="ok",
        user=(user.get("username") if user else None),
        target=result["public_id"],
        detail=f"category={body.category} impact={body.impact}",
    )
    return {"bug_report": result}


@router.get("/api/feedback/bugs")
def list_my_bug_reports(
    request: Request,
    response: Response,
    page: int = 1,
    per_page: int = 30,
    user=Depends(require_user),
):
    _no_store(response)
    return _feedback(request).list_owned(user["id"], page=page, per_page=per_page)


@router.get("/api/feedback/bugs/{bug_public_id}")
def get_my_bug_report(
    bug_public_id: str,
    request: Request,
    response: Response,
    user=Depends(require_user),
):
    _no_store(response)
    try:
        return {"bug_report": _feedback(request).get_detail(
            bug_public_id, user_id=user["id"], admin=False
        )}
    except Exception as exc:
        raise _domain_error(exc) from exc


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Referrer-Policy"] = "no-referrer"


@router.get("/api/feedback/bugs/{bug_public_id}/track")
def track_guest_bug_report(
    bug_public_id: str,
    request: Request,
    response: Response,
    tracking_token: str = Header("", alias="X-Feedback-Token", max_length=200),
):
    _no_store(response)
    try:
        bug = _feedback(request).get_detail(
            bug_public_id,
            user_id=None,
            admin=False,
            tracking_token=tracking_token,
        )
        thread = _service(request).repository.get_thread(
            bug["conversation_public_id"], admin=True
        )
        return {"bug_report": bug, "thread": thread}
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.post("/api/feedback/bugs/{bug_public_id}/track/reply")
def reply_guest_bug_report(
    bug_public_id: str,
    body: ReplyBody,
    request: Request,
    response: Response,
    tracking_token: str = Header("", alias="X-Feedback-Token", max_length=200),
):
    _no_store(response)
    if body.email:
        raise HTTPException(400, "访客回复不能指定邮件投递")
    try:
        bug = _feedback(request).authorize_report(
            bug_public_id,
            user_id=None,
            admin=False,
            tracking_token=tracking_token,
        )
        message = _service(request).reply_guest_report(
            bug["conversation_public_id"],
            body_text=body.body,
            reply_to=body.reply_to,
        )
    except Exception as exc:
        audit_log(
            request,
            "bug_report_guest_reply",
            result="fail",
            target=bug_public_id,
            detail=type(exc).__name__,
        )
        raise _domain_error(exc) from exc
    audit_log(
        request,
        "bug_report_guest_reply",
        result="ok",
        target=bug_public_id,
    )
    return {"message": message}


@router.post("/api/feedback/bugs/{bug_public_id}/attachments")
async def upload_bug_attachment(
    bug_public_id: str,
    request: Request,
    user=Depends(optional_user),
):
    is_admin = bool(user and user.get("role") == ROLE_ADMIN)
    try:
        async with request.form(max_files=1, max_fields=2) as form:
            token_value = form.get("tracking_token")
            tracking_token = (
                token_value if isinstance(token_value, str) else ""
            )
            file = form.get("file")
            if not isinstance(file, UploadFile):
                raise HTTPException(
                    422, "multipart 文件字段 file 缺失或类型错误"
                )
            bug = _feedback(request).authorize_attachment(
                bug_public_id,
                user_id=(int(user["id"]) if user else None),
                admin=is_admin,
                tracking_token=tracking_token,
            )
            raw = await file.read(MAX_ATTACHMENT_BYTES + 1)
            original_name = file.filename or "image"
            claimed_media_type = file.content_type or ""
        uploader_user_id = (
            int(user["id"])
            if user and (is_admin or bug["reporter_user_id"] == int(user["id"]))
            else None
        )
        attachment = _feedback(request).save_attachment(
            bug,
            uploaded_by_user_id=uploader_user_id,
            original_name=original_name,
            claimed_media_type=claimed_media_type,
            raw=raw,
        )
    except HTTPException:
        raise
    except Exception as exc:
        audit_log(
            request,
            "bug_attachment_add",
            result="fail",
            user=(user.get("username") if user else None),
            target=bug_public_id,
            detail=type(exc).__name__,
        )
        raise _domain_error(exc) from exc
    audit_log(
        request,
        "bug_attachment_add",
        result="ok",
        user=(user.get("username") if user else None),
        target=bug_public_id,
        detail=f"attachment={attachment['public_id']} size={attachment['size_bytes']}",
    )
    return {"attachment": attachment}


@router.get("/api/feedback/bugs/{bug_public_id}/attachments/{attachment_public_id}")
def download_bug_attachment(
    bug_public_id: str,
    attachment_public_id: str,
    request: Request,
    user=Depends(optional_user),
    tracking_token: str = Header("", alias="X-Feedback-Token", max_length=200),
):
    is_admin = bool(user and user.get("role") == ROLE_ADMIN)
    try:
        attachment = _feedback(request).read_attachment(
            bug_public_id,
            attachment_public_id,
            user_id=(int(user["id"]) if user else None),
            admin=is_admin,
            tracking_token=tracking_token,
        )
    except Exception as exc:
        raise _domain_error(exc) from exc
    headers = {
        "Cache-Control": "private, no-store, max-age=0",
        "Pragma": "no-cache",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{attachment["original_name"]}"',
    }
    return Response(
        content=attachment["content"],
        media_type=attachment["media_type"],
        headers=headers,
    )


@router.get("/api/admin/bug-reports")
def admin_bug_reports(
    request: Request,
    status: str | None = None,
    page: int = 1,
    per_page: int = 30,
    _admin=Depends(require_admin),
):
    try:
        return _feedback(request).list_admin(
            status=status, page=page, per_page=per_page
        )
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.get("/api/admin/bug-reports/{bug_public_id}")
def admin_bug_report(
    bug_public_id: str,
    request: Request,
    _admin=Depends(require_admin),
):
    try:
        return {"bug_report": _feedback(request).get_detail(
            bug_public_id, user_id=None, admin=True
        )}
    except Exception as exc:
        raise _domain_error(exc) from exc


@router.patch("/api/admin/bug-reports/{bug_public_id}/status")
def admin_update_bug_status(
    bug_public_id: str,
    body: BugStatusBody,
    request: Request,
    admin=Depends(require_admin),
):
    try:
        result = _feedback(request).update_status(
            bug_public_id,
            admin_user_id=admin["id"],
            new_status=body.status,
            note=body.note,
            duplicate_of_public_id=body.duplicate_of,
        )
    except Exception as exc:
        audit_log(
            request,
            "bug_report_status",
            result="fail",
            user=admin.get("username"),
            target=bug_public_id,
            detail=type(exc).__name__,
        )
        raise _domain_error(exc) from exc
    audit_log(
        request,
        "bug_report_status",
        result="ok",
        user=admin.get("username"),
        target=bug_public_id,
        detail=f"status={body.status}",
    )
    return {"bug_report": result}
