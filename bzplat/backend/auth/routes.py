"""认证 API：注册 / 登录 / 验证码 / 邮箱验证 / 重置密码。"""
from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from .auth_manager import COOKIE_NAME, AuthError, AuthManager
from .captcha import CAPTCHA_TTL_SEC, CaptchaStore, png_to_data_url
from .dependencies import require_admin, require_user

router = APIRouter(prefix="/api/auth", tags=["auth"])

COOKIE_MAX_AGE = 7 * 24 * 3600


class RegisterReq(BaseModel):
    username: str = Field(..., min_length=3, max_length=32)
    email: str
    password: str = Field(..., min_length=8)
    display_name: str = Field("", max_length=64)
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


class AdminResetReq(BaseModel):
    username_or_email: str


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
        "invalid_reset_token": 400,
        "expired_reset_token": 400,
        "invalid_code": 400,
        "expired_code": 400,
        "mail_failed": 502,
        "mail_not_configured": 503,
        "no_user": 404,
        "invalid_captcha": 400,
    }
    return HTTPException(
        status_code=code_to_status.get(exc.code, 400), detail=exc.message
    )


def _require_captcha(request: Request, captcha_id: str, answer: str) -> None:
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
        )
        auth.send_verify_code(user)
    except AuthError as exc:
        raise _err(exc) from exc
    return {
        "user": user,
        "message": "注册成功,验证码已发送到邮箱,请完成验证后再登录",
        "need_verify": True,
    }


@router.post("/verify-email")
async def verify_email(req: VerifyEmailReq, request: Request) -> dict:
    auth: AuthManager = request.app.state.auth
    try:
        user = auth.verify_email(req.email_or_username, req.code)
    except AuthError as exc:
        raise _err(exc) from exc
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
    return {"ok": True, "message": "若账号存在且未验证,验证码已重新发送"}


@router.post("/login")
async def login(req: LoginReq, request: Request, response: Response) -> dict:
    _require_captcha(request, req.captcha_id, req.captcha_answer)
    auth: AuthManager = request.app.state.auth
    try:
        user, token = auth.authenticate(
            req.username,
            req.password,
            ip_addr=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""),
        )
    except AuthError as exc:
        raise _err(exc) from exc
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
        raise _err(exc) from exc
    return {"ok": True, "message": "密码已修改,请重新登录"}


@router.post("/request-reset")
async def request_reset(req: RequestResetReq, request: Request) -> dict:
    _require_captcha(request, req.captcha_id, req.captcha_answer)
    auth: AuthManager = request.app.state.auth
    try:
        auth.request_reset(req.email_or_username)
    except AuthError as exc:
        if exc.code == "mail_failed":
            raise _err(exc) from exc
    return {
        "ok": True,
        "message": "若账号存在,重置验证码已发送到邮箱",
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
        raise _err(exc) from exc
    return {
        "ok": True,
        "message": "密码已重置,请用新密码登录",
        "username": user.get("username"),
    }


@router.post("/admin/create-reset-token")
async def admin_create_reset_token(
    req: AdminResetReq,
    request: Request,
    _: dict = Depends(require_admin),
) -> dict:
    auth: AuthManager = request.app.state.auth
    try:
        token, user = auth.admin_create_reset_token(req.username_or_email)
    except AuthError as exc:
        raise _err(exc) from exc
    return {"ok": True, "token": token, "user": user}
