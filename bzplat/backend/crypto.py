"""密码哈希与会话 token（stdlib pbkdf2_hmac-sha256）。"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta

SESSION_TTL_SEC = 7 * 24 * 3600  # 7 天


def hash_password(
    password: str, salt: str | None = None, iterations: int = 200_000
) -> str:
    """返回 ``pbkdf2_sha256$iterations$salt_hex$hash_hex``。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations
    )
    return f"pbkdf2_sha256${iterations}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间比较。"""
    try:
        algo, iters, salt, hash_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iters)
        )
        return secrets.compare_digest(dk.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def session_expires(seconds: int = SESSION_TTL_SEC) -> str:
    return (datetime.now() + timedelta(seconds=seconds)).isoformat(timespec="seconds")
