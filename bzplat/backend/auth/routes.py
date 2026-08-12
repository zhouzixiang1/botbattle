"""认证 API：注册 / 登录 / 验证码 / 邮箱验证 / 重置密码。"""
from __future__ import annotations

import os
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    Response,
)
from pydantic import BaseModel, Field, field_validator
from starlette.datastructures import UploadFile

from .auth_manager import COOKIE_NAME, AuthError, AuthManager, validate_phone
from .captcha import CAPTCHA_TTL_SEC, CaptchaStore, png_to_data_url
from .dependencies import require_user
from bzplat.backend.security import audit_log, client_ip
from bzplat.backend.security import _env_bool

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_MAX_AGE = 7 * 24 * 3600


class RegisterReq(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    email: str
    password: str = Field(..., min_length=8)
    display_name: str = Field("", max_length=64)
    real_name: str | None = Field(None, max_length=32)
    phone: str | None = Field(None, max_length=20)
    school: str | None = Field(None, max_length=64)
    student_id: str | None = Field(None, max_length=32)
    captcha_id: str
    captcha_answer: str


class LoginReq(BaseModel):
    username: str
    password: str
    captcha_id: str
    captcha_answer: str


class ChangePasswordReq(BaseModel):
    old_password: str
    new_password: str = Field(..., min_length=8)


class ProfileUpdateReq(BaseModel):
    display_name: str | None = Field(None, max_length=64)
    bio: str | None = Field(None, max_length=500)
    real_name: str | None = Field(None, max_length=32)
    phone: str | None = Field(None, max_length=20)
    school: str | None = Field(None, max_length=64)
    student_id: str | None = Field(None, max_length=32)

    @field_validator("display_name")
    @classmethod
    def display_name_no_angle_brackets(cls, v: str | None) -> str | None:
        """禁止 <> 等尖括号，避免侧栏/主页脏显示名（转义安全但体验差）。"""
        if v is None:
            return v
        if "<" in v or ">" in v:
            raise ValueError("显示名不能包含 < 或 > 字符")
        return v


class RequestResetReq(BaseModel):
    email_or_username: str
    captcha_id: str
    captcha_answer: str


class ResetPasswordReq(BaseModel):
    email_or_username: str
    code: str
    new_password: str = Field(..., min_length=8)


class VerifyEmailReq(BaseModel):
    email_or_username: str
    code: str


class ResendVerifyReq(BaseModel):
    email_or_username: str
    captcha_id: str
    captcha_answer: str


def _secure_cookie() -> bool:
    return os.environ.get("BZ_SECURE_COOKIE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _set_session_cookie(resp: Response, token: str) -> None:
    resp.set_cookie(
        COOKIE_NAME,
        token,
        httponly=True,
        max_age=COOKIE_MAX_AGE,
        samesite="lax",
        path="/",
        secure=_secure_cookie(),
    )


def _err(exc: AuthError) -> HTTPException:
    code_to_status = {
        "username_taken": 409,
        "email_taken": 409,
        "invalid_credentials": 401,
        "inactive": 403,
        "email_unverified": 403,
        "wrong_old_password": 401,
        "invalid_code": 400,
        "expired_code": 400,
        "no_user": 404,
        "invalid_captcha": 400,
        "invalid_phone": 400,
    }
    return HTTPException(
        status_code=code_to_status.get(exc.code, 400), detail=exc.message
    )


def _require_captcha(request: Request, captcha_id: str, answer: str) -> None:
    # 测试便利开关：BZ_SKIP_CAPTCHA=1 时跳过验证码校验，便于端到端自动化
    # （仅测试/开发环境开启；生产环境不设此变量，行为与原来完全一致）
    if _env_bool("BZ_SKIP_CAPTCHA", False):
        return
    store: CaptchaStore = request.app.state.captcha
    if not store.verify(captcha_id, answer):
        raise HTTPException(status_code=400, detail="图形验证码错误或已过期")


@router.get("/captcha")
async def get_captcha(request: Request) -> dict:
    store: CaptchaStore = request.app.state.captcha
    cid, answer, png = store.create()
    # 测试模式：暴露答案便于自动化测试走完整验证码流程（仅 BZ_TEST_CAPTCHA 开启时）
    test_mode = os.environ.get("BZ_TEST_CAPTCHA", "").lower() in {"1", "true", "yes"}
    return {
        "captcha_id": cid,
        "image_base64": png_to_data_url(png),
        "ttl": CAPTCHA_TTL_SEC,
        **({"answer": answer} if test_mode else {}),
    }


@router.post("/register")
async def register(req: RegisterReq, request: Request) -> dict:
    _require_captcha(request, req.captcha_id, req.captcha_answer)
    auth: AuthManager = request.app.state.auth
    try:
        user = auth.register(
            req.username,
            req.email,
            req.password,
            display_name=req.display_name,
            real_name=(req.real_name or "").strip(),
            phone=(req.phone or "").strip(),
            school=(req.school or "").strip(),
            student_id=(req.student_id or "").strip(),
        )
        auth.send_verify_code(user)
    except AuthError as exc:
        audit_log(request, "register", result="fail", target=req.username, detail=exc.code)
        raise _err(exc) from exc
    audit_log(request, "register", result="ok", user=user.get("username"))
    return {
        "user": user,
        "message": "注册成功，验证码邮件已进入发送队列，请完成验证后再登录",
        "delivery_status": "queued",
        "need_verify": True,
    }


@router.post("/verify-email")
async def verify_email(req: VerifyEmailReq, request: Request) -> dict:
    auth: AuthManager = request.app.state.auth
    try:
        user = auth.verify_email(req.email_or_username, req.code)
    except AuthError as exc:
        audit_log(request, "verify_email", result="fail", target=req.email_or_username, detail=exc.code)
        raise _err(exc) from exc
    audit_log(request, "verify_email", result="ok", user=user.get("username"))
    return {"ok": True, "user": user, "message": "邮箱已验证,请登录"}


@router.post("/resend-verify")
async def resend_verify(req: ResendVerifyReq, request: Request) -> dict:
    _require_captcha(request, req.captcha_id, req.captcha_answer)
    auth: AuthManager = request.app.state.auth
    user = auth.store.get_user_by_email(
        req.email_or_username
    ) or auth.store.get_user_by_username(req.email_or_username)
    if user and not user.get("email_verified"):
        try:
            auth.send_verify_code(user)
        except AuthError as exc:
            raise _err(exc) from exc
    return {
        "ok": True,
        "message": "若账号存在且未验证，验证码邮件已进入发送队列",
        "delivery_status": "queued",
    }


@router.post("/login")
async def login(req: LoginReq, request: Request, response: Response) -> dict:
    try:
        _require_captcha(request, req.captcha_id, req.captcha_answer)
    except HTTPException:
        # 验证码失败也要审计（暴力试探的早期信号）
        audit_log(request, "login", result="fail", target=req.username, detail="captcha_failed")
        raise
    auth: AuthManager = request.app.state.auth
    # 只有命中 trusted-proxy CIDR 的原始 socket peer 才可提交代理身份头。
    from bzplat.backend.security import _env_int
    ip = client_ip(
        request,
        trust_proxy=_env_bool("BZ_TRUST_PROXY", False),
        hops=_env_int("BZ_TRUSTED_PROXY_HOPS", 1),
    )
    try:
        user, token = auth.authenticate(
            req.username,
            req.password,
            ip_addr=ip,
            user_agent=request.headers.get("user-agent", ""),
        )
    except AuthError as exc:
        audit_log(request, "login", result="fail", target=req.username, detail=exc.code)
        raise _err(exc) from exc
    audit_log(request, "login", result="ok", user=user.get("username") or user.get("id"))
    _set_session_cookie(response, token)
    return {"user": user, "token": token}


@router.post("/logout")
async def logout(request: Request, response: Response) -> dict:
    auth: AuthManager = request.app.state.auth
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        token = auth_header[7:].strip()
    else:
        token = request.cookies.get(COOKIE_NAME)
    auth.logout(token)
    response.delete_cookie(COOKIE_NAME, path="/")
    audit_log(request, "logout")
    return {"ok": True}


@router.get("/me")
async def me(user: dict = Depends(require_user)) -> dict:
    return {"user": user}


@router.post("/change-password")
async def change_password(
    req: ChangePasswordReq,
    request: Request,
    user: dict = Depends(require_user),
) -> dict:
    auth: AuthManager = request.app.state.auth
    try:
        auth.change_password(user["id"], req.old_password, req.new_password)
    except AuthError as exc:
        audit_log(request, "change_password", result="fail", user=user.get("username"), detail=exc.code)
        raise _err(exc) from exc
    audit_log(request, "change_password", result="ok", user=user.get("username"))
    return {"ok": True, "message": "密码已修改,请重新登录"}


@router.put("/profile")
async def update_profile(
    req: ProfileUpdateReq,
    request: Request,
    user: dict = Depends(require_user),
) -> dict:
    """更新当前用户的显示名/简介/实名信息。"""
    store = request.app.state.store
    fields: dict = {}
    if req.display_name is not None:
        fields["display_name"] = req.display_name.strip()[:64]
    if req.bio is not None:
        fields["bio"] = req.bio.strip()[:500]
    if req.real_name is not None:
        fields["real_name"] = req.real_name.strip()[:32]
    if req.phone is not None:
        phone = req.phone.strip()
        try:
            validate_phone(phone)
        except AuthError as exc:
            raise _err(exc) from exc
        fields["phone"] = phone[:20]
    if req.school is not None:
        fields["school"] = req.school.strip()[:64]
    if req.student_id is not None:
        fields["student_id"] = req.student_id.strip()[:32]
    if fields:
        store.update_user(user["id"], **fields)
    u = store.get_user(user["id"])
    return {"user": _safe_user_out(u)}


_AVATAR_MAX = 2 * 1024 * 1024  # 2MB
_AVATAR_ALLOWED = {"image/png", "image/jpeg", "image/webp", "image/gif"}
_AVATAR_UPLOAD_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {
                        "file": {"type": "string", "format": "binary"}
                    },
                }
            }
        },
    },
    "responses": {
        "400": {"description": "头像文件大小或媒体类型无效"},
        "401": {"description": "未登录或会话过期"},
        "413": {"description": "multipart 请求体超过 3 MiB"},
        "422": {"description": "缺少 multipart 文件字段"},
    },
}


@router.post("/avatar", openapi_extra=_AVATAR_UPLOAD_OPENAPI)
async def upload_avatar(
    request: Request,
    user: dict = Depends(require_user),
) -> dict:
    """上传/更新当前用户头像。存本地 avatars/<uid>.<ext>，StaticFiles 托管。"""
    async with request.form(max_files=1, max_fields=1) as form:
        file = form.get("file")
        if not isinstance(file, UploadFile):
            raise HTTPException(422, "multipart 文件字段 file 缺失或类型错误")
        raw = await file.read(_AVATAR_MAX + 1)
        ctype = (file.content_type or "").lower()
    if not raw or len(raw) > _AVATAR_MAX:
        raise HTTPException(400, "头像文件须 1..2MB")
    ext_map = {
        "image/png": "png", "image/jpeg": "jpg",
        "image/webp": "webp", "image/gif": "gif",
    }
    ext = ext_map.get(ctype)
    if not ext:
        raise HTTPException(400, "仅支持 png/jpeg/webp/gif")
    avatars_dir = Path(request.app.state.avatar_dir)
    avatars_dir.mkdir(parents=True, exist_ok=True)
    # 覆盖旧头像（删其他扩展名的同名文件）
    uid = user["id"]
    for old in ("png", "jpg", "webp", "gif"):
        old_path = avatars_dir / f"{uid}.{old}"
        old_path.unlink(missing_ok=True)
    path = avatars_dir / f"{uid}.{ext}"
    with path.open("wb") as f:
        f.write(raw)
    store = request.app.state.store
    rel = f"{uid}.{ext}"
    store.update_user(uid, avatar=rel)
    u = store.get_user(uid)
    return {"user": _safe_user_out(u), "avatar": rel}


def _safe_user_out(u: dict | None) -> dict:
    """剔除敏感字段，保留 bio/avatar。"""
    if not u:
        return {}
    return {k: v for k, v in u.items() if k != "password_hash"}


@router.post("/request-reset")
async def request_reset(req: RequestResetReq, request: Request) -> dict:
    _require_captcha(request, req.captcha_id, req.captcha_answer)
    auth: AuthManager = request.app.state.auth
    auth.request_reset(req.email_or_username)
    return {
        "ok": True,
        "message": "若账号存在，重置验证码邮件已进入发送队列",
        "delivery_status": "queued",
        "token": None,
    }


@router.post("/reset-password")
async def reset_password(req: ResetPasswordReq, request: Request) -> dict:
    auth: AuthManager = request.app.state.auth
    try:
        user = auth.reset_password(
            req.email_or_username, req.code, req.new_password
        )
    except AuthError as exc:
        audit_log(request, "reset_password", result="fail", target=req.email_or_username, detail=exc.code)
        raise _err(exc) from exc
    audit_log(request, "reset_password", result="ok", user=user.get("username"))
    return {
        "ok": True,
        "message": "密码已重置,请用新密码登录",
        "username": user.get("username"),
    }
