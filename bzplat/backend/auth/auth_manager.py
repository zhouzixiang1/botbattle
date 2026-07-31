"""认证管理器：注册 / 登录 / 邮箱验证 / 重置密码。"""
from __future__ import annotations

import logging
import re
import secrets
from datetime import datetime, timedelta

from bzplat.backend.crypto import (
    hash_password,
    new_session_token,
    session_expires,
    verify_password,
)
from bzplat.backend.mail import Mailer, render_template
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    CODE_RESET,
    CODE_VERIFY,
    ROLE_ADMIN,
    ROLE_ORGANIZER,
    ROLE_USER,
    TPL_RESET_PASSWORD,
    TPL_VERIFY_EMAIL,
    TPL_WELCOME,
)

logger = logging.getLogger(__name__)

SESSION_TTL_SEC = 7 * 24 * 3600
PASSWORD_RESET_TTL_SEC = 24 * 3600
COOKIE_NAME = "bz_session"
_DEFAULT_CODE_TTL_MIN = 30

_USERNAME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]{2,31}$")
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
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


class AuthManager:
    """注册 / 登录 / 邮箱验证 / 重置密码。

    ``mailer`` 可为 ``None``（测试跳过 SMTP）或自定义 mock。
    """

    def __init__(self, store: Store, mailer: Mailer | None = None) -> None:
        self.store = store
        self.mailer = mailer

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
    ) -> dict:
        _validate_username(username)
        _validate_email(email)
        _validate_password(password)
        if self.store.get_user_by_username(username):
            raise AuthError("username_taken", "用户名已被占用")
        if self.store.get_user_by_email(email):
            raise AuthError("email_taken", "邮箱已注册")
        pw_hash = hash_password(password)
        user = self.store.create_user(
            username, email, pw_hash, display_name=display_name, role=ROLE_USER
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
        """生成并邮件发送验证码。purpose: verify|reset。"""
        if purpose not in (CODE_VERIFY, CODE_RESET):
            raise AuthError("invalid_purpose", "无效的验证码用途")
        code = self._gen_code()
        ttl_min = self._code_ttl_minutes()
        expires = (datetime.now() + timedelta(minutes=ttl_min)).isoformat(
            timespec="seconds"
        )
        self.store.add_email_code(user["id"], purpose, code, expires)
        tpl_key = (
            TPL_VERIFY_EMAIL if purpose == CODE_VERIFY else TPL_RESET_PASSWORD
        )
        tpl = self.store.get_template(tpl_key)
        if not tpl:
            raise AuthError("no_template", f"缺少邮件模板 {tpl_key}")
        ctx = {
            "username": user.get("display_name") or user.get("username") or "",
            "code": code,
            "expires_minutes": ttl_min,
        }
        subject = render_template(tpl["subject"], ctx)
        html = render_template(tpl["body_html"], ctx)
        text = render_template(tpl["body_text"], ctx)
        if self.mailer is None:
            logger.warning(
                "SMTP 未配置，验证码未发信 purpose=%s user=%s email=%s code=%s",
                purpose,
                user.get("username"),
                user.get("email"),
                code,
            )
            self.store.add_outbox(
                user["email"], subject, template_key=tpl_key, status="skipped",
                error="SMTP 未配置",
            )
            # 未配置邮件时明确失败，避免前端提示「已发送」却收不到
            raise AuthError(
                "mail_not_configured",
                "邮件服务未配置，无法发送验证码。请管理员配置 SMTP_* 环境变量后重试。",
            )
        try:
            self.mailer.send(
                user["email"], subject, body_text=text, body_html=html
            )
            self.store.add_outbox(
                user["email"], subject, template_key=tpl_key, status="sent"
            )
        except Exception as exc:
            self.store.add_outbox(
                user["email"],
                subject,
                template_key=tpl_key,
                status="failed",
                error=str(exc)[:500],
            )
            raise AuthError("mail_failed", f"邮件发送失败: {exc}") from exc

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
        if self.mailer is not None:
            try:
                tpl = self.store.get_template(TPL_WELCOME)
                if tpl:
                    ctx = {
                        "username": user.get("display_name") or user["username"],
                        "code": "",
                        "expires_minutes": 0,
                    }
                    subject = render_template(tpl["subject"], ctx)
                    self.mailer.send(
                        user["email"],
                        subject,
                        body_text=render_template(tpl["body_text"], ctx),
                        body_html=render_template(tpl["body_html"], ctx),
                    )
                    self.store.add_outbox(
                        user["email"], subject, template_key=TPL_WELCOME
                    )
            except Exception:
                logger.debug("welcome mail skipped", exc_info=True)
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
        except ValueError as exc:
            raise AuthError("invalid_code", "验证码无效") from exc
        self.store.mark_email_code_used(row["id"])
        self.store.update_user(user["id"], password_hash=hash_password(new_password))
        self.store.delete_sessions_for_user(user["id"])
        return _safe_user(user)

    reset_password_with_code = reset_password

    def reset_password_by_token(self, token: str, new_password: str) -> dict:
        _validate_password(new_password)
        r = self.store.get_password_reset(token)
        if not r:
            raise AuthError("invalid_reset_token", "重置链接无效或已使用")
        try:
            if datetime.fromisoformat(r["expires_at"]) < datetime.now():
                self.store.mark_password_reset_used(token)
                raise AuthError("expired_reset_token", "重置链接已过期,请重新申请")
        except ValueError as exc:
            raise AuthError("invalid_reset_token", "重置链接无效") from exc
        user = self.store.get_user(r["user_id"])
        if not user:
            raise AuthError("no_user", "用户不存在")
        self.store.update_user(user["id"], password_hash=hash_password(new_password))
        self.store.mark_password_reset_used(token)
        self.store.delete_sessions_for_user(user["id"])
        return _safe_user(user)

    def admin_create_reset_token(
        self, username_or_email: str
    ) -> tuple[str, dict]:
        user = self.store.get_user_by_email(
            username_or_email or ""
        ) or self.store.get_user_by_username(username_or_email or "")
        if not user:
            raise AuthError("no_user", "用户不存在")
        token = secrets.token_urlsafe(32)
        expires = (
            datetime.now() + timedelta(seconds=PASSWORD_RESET_TTL_SEC)
        ).isoformat(timespec="seconds")
        self.store.add_password_reset(token, user["id"], expires)
        return token, _safe_user(user)

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
