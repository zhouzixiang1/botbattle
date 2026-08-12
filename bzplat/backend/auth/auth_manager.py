"""认证管理器：注册 / 登录 / 邮箱验证 / 重置密码。"""
from __future__ import annotations

import re
import secrets
from datetime import datetime, timedelta

from bzplat.backend.crypto import (
    hash_password,
    new_session_token,
    session_expires,
    verify_password,
)
from bzplat.backend.communications.service import CommunicationService
from bzplat.backend.mail import Mailer
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    CODE_RESET,
    CODE_VERIFY,
    ROLE_ADMIN,
    ROLE_ORGANIZER,
    ROLE_USER,
)

SESSION_TTL_SEC = 7 * 24 * 3600
COOKIE_NAME = "bz_session"
_DEFAULT_CODE_TTL_MIN = 30

_USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,31}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE_RE = re.compile(r"^1[3-9][0-9]{9}$")
_MIN_PASSWORD_LEN = 8


class AuthError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_username(username: str) -> None:
    if not _USERNAME_RE.match(username or ""):
        raise AuthError(
            "invalid_username",
            "用户名须 3-32 字符,字母开头,只含字母数字下划线",
        )


def _validate_email(email: str) -> None:
    if not _EMAIL_RE.match(email or ""):
        raise AuthError("invalid_email", "邮箱格式不正确")


def _validate_password(password: str) -> None:
    if not password or len(password) < _MIN_PASSWORD_LEN:
        raise AuthError("weak_password", f"密码至少 {_MIN_PASSWORD_LEN} 个字符")


def validate_phone(phone: str) -> None:
    """校验选填的中国大陆手机号；空值表示未填写。"""
    if phone and not _PHONE_RE.fullmatch(phone):
        raise AuthError("invalid_phone", "手机号格式不正确")


class AuthManager:
    """注册 / 登录 / 邮箱验证 / 重置密码。

    ``mailer`` 仅保留验证码 TTL 配置兼容；本类绝不调用 SMTP。
    """

    def __init__(
        self,
        store: Store,
        mailer: Mailer | None = None,
        *,
        communications: CommunicationService | None = None,
    ) -> None:
        self.store = store
        # ``mailer`` only supplies the TTL compatibility setting.  It is never called;
        # all SMTP is owned by the lifespan DeliveryWorker.
        self.mailer = mailer
        self.communications = communications or CommunicationService(store)

    def _code_ttl_minutes(self) -> int:
        if self.mailer is not None and getattr(self.mailer, "config", None):
            return int(self.mailer.config.code_ttl_minutes)
        return _DEFAULT_CODE_TTL_MIN

    def register(
        self,
        username: str,
        email: str,
        password: str,
        *,
        display_name: str = "",
        real_name: str = "",
        phone: str = "",
        school: str = "",
        student_id: str = "",
    ) -> dict:
        _validate_username(username)
        _validate_email(email)
        _validate_password(password)
        phone = phone.strip()
        validate_phone(phone)
        if self.store.get_user_by_username(username):
            raise AuthError("username_taken", "用户名已被占用")
        if self.store.get_user_by_email(email):
            raise AuthError("email_taken", "邮箱已注册")
        pw_hash = hash_password(password)
        user = self.store.create_user(
            username, email, pw_hash, display_name=display_name, role=ROLE_USER,
            real_name=real_name, phone=phone, school=school, student_id=student_id,
        )
        self.store.update_user(user["id"], email_verified=0)
        user = self.store.get_user(user["id"])
        return _safe_user(user)

    def authenticate(
        self,
        username: str,
        password: str,
        *,
        ip_addr: str = "",
        user_agent: str = "",
    ) -> tuple[dict, str]:
        user = self.store.get_user_by_username(username or "")
        ok = (
            verify_password(password or "", user["password_hash"]) if user else False
        )
        if not user or not ok:
            raise AuthError("invalid_credentials", "用户名或密码错误")
        if not user["is_active"]:
            raise AuthError("inactive", "账号已被停用,请联系管理员")
        if not user.get("email_verified"):
            raise AuthError(
                "email_unverified", "邮箱未验证,请先完成邮箱验证后再登录"
            )
        token = new_session_token()
        self.store.add_session(
            token,
            user["id"],
            session_expires(SESSION_TTL_SEC),
            ip_addr=ip_addr,
            user_agent=user_agent,
        )
        self.store.update_user(
            user["id"],
            last_login_at=datetime.now().isoformat(timespec="seconds"),
        )
        return _safe_user(user), token

    def logout(self, token: str | None) -> None:
        if token:
            self.store.delete_session(token)

    def verify_session(self, token: str | None) -> dict | None:
        if not token:
            return None
        s = self.store.get_session(token)
        if not s:
            return None
        try:
            if datetime.fromisoformat(s["expires_at"]) < datetime.now():
                self.store.delete_session(token)
                return None
        except ValueError:
            return None
        user = self.store.get_user(s["user_id"])
        if not user or not user["is_active"]:
            self.store.delete_session(token)
            return None
        return _safe_user(user)

    def change_password(
        self, user_id: int, old_password: str, new_password: str
    ) -> None:
        _validate_password(new_password)
        user = self.store.get_user(user_id)
        if not user:
            raise AuthError("no_user", "用户不存在")
        if not verify_password(old_password or "", user["password_hash"]):
            raise AuthError("wrong_old_password", "旧密码错误")
        self.store.update_user(user_id, password_hash=hash_password(new_password))
        self.store.delete_sessions_for_user(user_id)

    def _gen_code(self) -> str:
        return f"{secrets.randbelow(1_000_000):06d}"

    def send_verify_code(self, user: dict) -> None:
        self.request_email_code(user, CODE_VERIFY)

    def request_email_code(self, user: dict, purpose: str) -> None:
        """生成验证码并排入高优先级事务邮件；请求线程绝不连接 SMTP。"""
        if purpose not in (CODE_VERIFY, CODE_RESET):
            raise AuthError("invalid_purpose", "无效的验证码用途")
        code = self._gen_code()
        ttl_min = self._code_ttl_minutes()
        expires = (datetime.now() + timedelta(minutes=ttl_min)).isoformat(
            timespec="seconds"
        )
        self.communications.queue_email_code(
            user,
            purpose=purpose,
            code=code,
            expires_at=expires,
        )

    send_email_code = request_email_code

    def verify_email(self, email_or_username: str, code: str) -> dict:
        """校验注册验证码并标记 email_verified=1。"""
        user = self.store.get_user_by_email(
            email_or_username or ""
        ) or self.store.get_user_by_username(email_or_username or "")
        if not user:
            raise AuthError("no_user", "用户不存在")
        row = self.store.get_latest_email_code(user["id"], CODE_VERIFY)
        if not row or row["code"] != (code or "").strip():
            raise AuthError("invalid_code", "验证码无效")
        try:
            if datetime.fromisoformat(row["expires_at"]) < datetime.now():
                raise AuthError("expired_code", "验证码已过期,请重新获取")
        except ValueError as exc:
            raise AuthError("invalid_code", "验证码无效") from exc
        self.store.mark_email_code_used(row["id"])
        self.store.update_user(user["id"], email_verified=1)
        # Welcome is also queued; verification success is never rolled back by SMTP.
        self.communications.queue_welcome(user)
        return _safe_user(self.store.get_user(user["id"]))

    verify_email_code = verify_email

    def request_reset(self, email_or_username: str) -> tuple[bool, dict]:
        """申请重置：发邮件验证码。防枚举：不存在也返回成功语义。"""
        user = self.store.get_user_by_email(
            email_or_username or ""
        ) or self.store.get_user_by_username(email_or_username or "")
        if not user:
            return False, {}
        self.request_email_code(user, CODE_RESET)
        return True, _safe_user(user)

    request_password_reset = request_reset

    def reset_password(
        self, email_or_username: str, code: str, new_password: str
    ) -> dict:
        _validate_password(new_password)
        user = self.store.get_user_by_email(
            email_or_username or ""
        ) or self.store.get_user_by_username(email_or_username or "")
        if not user:
            raise AuthError("no_user", "用户不存在")
        row = self.store.get_latest_email_code(user["id"], CODE_RESET)
        if not row or row["code"] != (code or "").strip():
            raise AuthError("invalid_code", "验证码无效")
        try:
            if datetime.fromisoformat(row["expires_at"]) < datetime.now():
                raise AuthError("expired_code", "验证码已过期,请重新获取")
        except (TypeError, ValueError) as exc:
            raise AuthError("invalid_code", "验证码无效") from exc
        result = self.store.reset_password_with_credential(
            user["id"],
            hash_password(new_password),
            email_code_id=row["id"],
            email_code=row["code"],
        )
        if result == "expired":
            raise AuthError("expired_code", "验证码已过期,请重新获取")
        if result != "ok":
            raise AuthError("invalid_code", "验证码无效或已使用")
        return _safe_user(self.store.get_user(user["id"]))

    reset_password_with_code = reset_password

    def admin_set_user_role(self, user_id: int, role: str) -> dict:
        if role not in (ROLE_USER, ROLE_ORGANIZER, ROLE_ADMIN):
            raise AuthError("invalid_role", "角色必须是 user/organizer/admin")
        user = self.store.update_user(user_id, role=role)
        if not user:
            raise AuthError("no_user", "用户不存在")
        return _safe_user(user)


def _safe_user(user: dict | None) -> dict:
    if user is None:
        return {}
    return {k: v for k, v in user.items() if k != "password_hash"}
