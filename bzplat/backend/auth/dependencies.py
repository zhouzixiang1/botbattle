"""认证 FastAPI 依赖：get_current_user / require_user / require_admin / require_organizer。

token 来源：cookie ``bz_session`` 或 ``Authorization: Bearer <token>``。
"""
from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from bzplat.backend.store import Store
from bzplat.backend.store.schema import ROLE_ADMIN, ROLE_ORGANIZER

from .auth_manager import COOKIE_NAME, AuthManager


def get_store(request: Request) -> Store:
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(status_code=503, detail="数据库未初始化")
    return store


def get_auth(request: Request) -> AuthManager:
    auth = getattr(request.app.state, "auth", None)
    if auth is None:
        raise HTTPException(status_code=503, detail="认证未启用")
    return auth


get_auth_manager = get_auth


def _extract_token(request: Request) -> str | None:
    auth_header = request.headers.get("authorization", "")
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip() or None
    return request.cookies.get(COOKIE_NAME)


def get_current_user(
    request: Request, auth: AuthManager = Depends(get_auth)
) -> dict:
    """从 cookie 或 Bearer 解析当前用户；未登录抛 401。"""
    token = _extract_token(request)
    user = auth.verify_session(token)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话过期")
    return user


require_user = get_current_user


def optional_user(
    request: Request, auth: AuthManager = Depends(get_auth)
) -> dict | None:
    """可选登录：已登录返回 user，未登录返回 None（不抛 401）。

    用于公开端点需区分 owner/访客做脱敏的场景（如 bot 详情：owner 看完整、
    访客脱敏 binary_path）。区别于 require_user（未登录直接 401）。
    """
    token = _extract_token(request)
    return auth.verify_session(token) or None


def require_admin(user: dict = Depends(require_user)) -> dict:
    if user.get("role") != ROLE_ADMIN:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


def require_organizer(user: dict = Depends(require_user)) -> dict:
    if user.get("role") not in (ROLE_ORGANIZER, ROLE_ADMIN):
        raise HTTPException(status_code=403, detail="需要组织者或管理员权限")
    return user
