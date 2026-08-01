"""公网暴露加固：安全响应头 + 内存 IP 限流 + 访问日志 + 安全审计日志。

单进程 uvicorn 用内存限流；多 worker 再换 Redis。
信任 X-Forwarded-For 仅在 BZ_TRUST_PROXY=1 时开启（公网经 nginx/frp 代理必需）。
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from bzplat.backend.logging_config import ACCESS_LOGGER, AUDIT_LOGGER

logger = logging.getLogger(__name__)
_access_logger = logging.getLogger(ACCESS_LOGGER)
_audit_logger = logging.getLogger(AUDIT_LOGGER)

_AUTH_STRICT = (20, 60)
_CAPTCHA_LIMIT = (60, 60)
_UPLOAD_STRICT = (6, 60)
_CHALLENGE_STRICT = (8, 60)
_API_DEFAULT = (120, 60)
_STATIC_SKIP_EXT = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".ico",
    ".svg",
    ".woff",
    ".woff2",
    ".map",
    ".webp",
)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def client_ip(request: Request, *, trust_proxy: bool) -> str:
    if trust_proxy:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip() or "unknown"
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


class InMemoryRateLimiter:
    """滑动窗口限流（按 key 记时间戳列表）。"""

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def check(
        self, key: str, max_requests: int, window: float
    ) -> tuple[bool, int, int]:
        now = time.monotonic()
        start = now - window
        bucket = [t for t in self._hits.get(key, []) if t > start]
        if len(bucket) >= max_requests:
            oldest = min(bucket) if bucket else now
            retry = int(oldest + window - now) + 1
            self._hits[key] = bucket
            return False, 0, max(1, retry)
        bucket.append(now)
        self._hits[key] = bucket
        return True, max_requests - len(bucket), 0

    def cleanup(self, max_age: float = 3600.0) -> None:
        cutoff = time.monotonic() - max_age
        self._hits = {
            k: v for k, v in self._hits.items() if v and max(v) > cutoff
        }


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """附加常见安全响应头。"""

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault(
            "Referrer-Policy", "strict-origin-when-cross-origin"
        )
        response.headers.setdefault(
            "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
        )
        if _env_bool("BZ_HSTS"):
            response.headers.setdefault(
                "Strict-Transport-Security",
                "max-age=31536000; includeSubDomains",
            )
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """按路径分级的 IP 限流（register/login/captcha/upload/challenge）。"""

    def __init__(self, app: ASGIApp, *, enabled: bool | None = None) -> None:
        super().__init__(app)
        self.enabled = (
            _env_bool("BZ_RATE_LIMIT", True) if enabled is None else enabled
        )
        self.trust_proxy = _env_bool("BZ_TRUST_PROXY", False)
        self._limiter = InMemoryRateLimiter()
        self._last_cleanup = time.monotonic()

    def _limits_for(self, method: str, path: str) -> tuple[int, float] | None:
        if method == "OPTIONS":
            return None
        if path in {"/api/health", "/"}:
            return None
        if any(path.endswith(ext) for ext in _STATIC_SKIP_EXT):
            return None
        if path.startswith("/assets/"):
            return None

        if path in {
            "/api/auth/register",
            "/api/auth/login",
            "/api/auth/request-reset",
            "/api/auth/reset-password",
            "/api/auth/resend-verify",
            "/api/auth/verify-email",
        }:
            return _AUTH_STRICT
        if path == "/api/auth/captcha":
            return _CAPTCHA_LIMIT
        if path == "/api/bots/upload" or path.endswith("/upload"):
            return _UPLOAD_STRICT
        if path == "/api/matches/challenge":
            return _CHALLENGE_STRICT
        if path.startswith("/api/"):
            return _API_DEFAULT
        return None

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        if not self.enabled:
            return await call_next(request)

        limits = self._limits_for(request.method, request.url.path)
        if limits is None:
            return await call_next(request)

        now = time.monotonic()
        if now - self._last_cleanup > 600:
            self._limiter.cleanup()
            self._last_cleanup = now

        max_req, window = limits
        ip = client_ip(request, trust_proxy=self.trust_proxy)
        key = f"{ip}:{request.url.path}"
        ok, remaining, retry = self._limiter.check(key, max_req, window)
        if not ok:
            logger.warning("rate limit: ip=%s path=%s", ip, request.url.path)
            return JSONResponse(
                status_code=429,
                content={
                    "detail": "请求过于频繁,请稍后再试",
                    "code": "rate_limit_exceeded",
                },
                headers={
                    "Retry-After": str(retry),
                    "X-RateLimit-Limit": str(max_req),
                    "X-RateLimit-Remaining": "0",
                },
            )

        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(max_req)
        response.headers["X-RateLimit-Remaining"] = str(remaining)
        return response


def security_settings() -> dict[str, Any]:
    return {
        "rate_limit": _env_bool("BZ_RATE_LIMIT", True),
        "trust_proxy": _env_bool("BZ_TRUST_PROXY", False),
        "hsts": _env_bool("BZ_HSTS", False),
        "secure_cookie": _env_bool("BZ_SECURE_COOKIE", False),
    }


class AccessLogMiddleware(BaseHTTPMiddleware):
    """每个 HTTP 请求记一行访问日志（含真实客户端 IP）到 logs/access.log。

    格式：``ip=<IP> method=<METHOD> path=<path> status=<状态码> dt=<耗时ms>``
    IP 经 ``client_ip()`` 解析（trust_proxy 开启时读 X-Forwarded-For/X-Real-IP）。
    跳过静态资源与 /api/health，避免噪音。
    """

    def __init__(self, app: ASGIApp, *, trust_proxy: bool | None = None) -> None:
        super().__init__(app)
        self.trust_proxy = (
            _env_bool("BZ_TRUST_PROXY", False) if trust_proxy is None else trust_proxy
        )

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        path = request.url.path
        # 跳过静态资源与健康检查（与 RateLimitMiddleware 一致）
        if path in {"/api/health", "/"} or any(
            path.endswith(ext) for ext in _STATIC_SKIP_EXT
        ) or path.startswith("/assets/"):
            return await call_next(request)

        start = time.monotonic()
        try:
            response = await call_next(request)
            status = response.status_code
        except Exception:
            # 异常也要记访问日志（5xx/崩溃），再向上抛
            status = 500
            dt_ms = int((time.monotonic() - start) * 1000)
            ip = client_ip(request, trust_proxy=self.trust_proxy)
            _access_logger.info(
                "ip=%s method=%s path=%s status=%s dt=%dms",
                ip, request.method, path, status, dt_ms,
            )
            raise
        dt_ms = int((time.monotonic() - start) * 1000)
        ip = client_ip(request, trust_proxy=self.trust_proxy)
        _access_logger.info(
            "ip=%s method=%s path=%s status=%d dt=%dms",
            ip, request.method, path, status, dt_ms,
        )
        return response


def audit_log(
    request: Request,
    action: str,
    *,
    result: str = "ok",
    user: str | int | None = None,
    target: str | None = None,
    detail: str | None = None,
    trust_proxy: bool | None = None,
) -> None:
    """记录一条安全审计日志到 logs/audit.log。

    用于敏感操作（登录/注册/上传/对局/admin 写等），含真实 IP、操作者、动作、结果。
    - ``action``：动作名（如 ``login``、``bot_upload``、``admin_delete_user``）。
    - ``result``：``ok`` / ``fail``（失败/拒绝优先关注）。
    - ``user``：操作者 id 或用户名（未登录态可为 None）。
    - ``target``：操作目标（如 bot_id、user_id、contest_id）。
    - ``detail``：附加细节（如失败原因、变更摘要）。
    """
    tp = _env_bool("BZ_TRUST_PROXY", False) if trust_proxy is None else trust_proxy
    ip = client_ip(request, trust_proxy=tp)
    parts = [f"ip={ip}", f"action={action}", f"result={result}"]
    if user is not None:
        parts.append(f"user={user}")
    if target is not None:
        parts.append(f"target={target}")
    if detail is not None:
        # detail 可能含空格，用引号包住便于解析
        parts.append(f'detail="{detail}"')
    level = logging.WARNING if result == "fail" else logging.INFO
    _audit_logger.log(level, " ".join(parts))

