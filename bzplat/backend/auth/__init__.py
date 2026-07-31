"""认证：注册 / 登录 / 重置密码 / 角色依赖。"""
from .auth_manager import COOKIE_NAME, AuthError, AuthManager
from .captcha import CAPTCHA_TTL_SEC, CaptchaStore, png_to_data_url
from .dependencies import (
    get_auth,
    get_auth_manager,
    get_current_user,
    get_store,
    require_admin,
    require_organizer,
    require_user,
)
from .routes import router

__all__ = [
    "AuthManager",
    "AuthError",
    "COOKIE_NAME",
    "CaptchaStore",
    "CAPTCHA_TTL_SEC",
    "png_to_data_url",
    "get_store",
    "get_auth",
    "get_auth_manager",
    "get_current_user",
    "require_user",
    "require_admin",
    "require_organizer",
    "router",
]
