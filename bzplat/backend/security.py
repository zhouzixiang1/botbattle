"""公网暴露加固：安全响应头 + 内存 IP 限流 + 访问日志 + 安全审计日志。

单进程 uvicorn 用内存限流；多 worker 再换 Redis。
信任 X-Forwarded-For 仅在 BZ_TRUST_PROXY=1 时开启（公网经 nginx/frp 代理必需）。
"""
from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from typing import Any

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from bzplat.backend.bots.manager import MAX_BYTES as MAX_BOT_BINARY_BYTES
from bzplat.backend.logging_config import ACCESS_LOGGER, AUDIT_LOGGER

logger = logging.getLogger(__name__)
_access_logger = logging.getLogger(ACCESS_LOGGER)
_audit_logger = logging.getLogger(AUDIT_LOGGER)

_AUTH_STRICT = (20, 60)
_CAPTCHA_LIMIT = (60, 60)
_UPLOAD_STRICT = (6, 60)
_CHALLENGE_STRICT = (8, 60)
_FEEDBACK_STRICT = (5, 60)
_API_DEFAULT = (120, 60)
BOT_UPLOAD_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
BOT_UPLOAD_BODY_MAX_BYTES = (
    MAX_BOT_BINARY_BYTES + BOT_UPLOAD_MULTIPART_OVERHEAD_BYTES
)
_BOT_VERSION_UPLOAD_PATH = re.compile(r"/api/bots/[^/]+/versions")
_BOT_UPLOAD_TOO_LARGE = {
    "code": "upload_body_too_large",
    "message": "Bot 二进制最大 50 MiB，上传请求体超过允许的 multipart 上限",
}
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


class _BotUploadBodyTooLarge(OSError):
    """Enter Starlette's multipart error cleanup path for open spool files."""


class BotUploadBodyLimitMiddleware:
    """Bound Bot multipart bodies before Starlette creates spooled files.

    ``Content-Length`` is only an early-reject hint.  Every delivered ASGI body
    chunk is still counted, so a missing or forged-small header cannot bypass the
    limit.  Proxy identity headers are deliberately irrelevant to this boundary.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int = BOT_UPLOAD_BODY_MAX_BYTES,
    ) -> None:
        if max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        self.max_body_bytes = int(max_body_bytes)

    @staticmethod
    def _is_bot_upload(scope: Scope) -> bool:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return False
        path = str(scope.get("path") or "")
        return path == "/api/bots" or bool(
            _BOT_VERSION_UPLOAD_PATH.fullmatch(path)
        )

    def _declared_too_large(self, scope: Scope) -> bool:
        for name, raw_value in scope.get("headers") or []:
            if name.lower() != b"content-length":
                continue
            # Treat each duplicate/comma-separated numeric value as an early
            # rejection signal.  Malformed or forged-small values never grant
            # admission; the receive counter below remains authoritative.
            for value in raw_value.split(b","):
                try:
                    declared = int(value.strip())
                except ValueError:
                    continue
                if declared > self.max_body_bytes:
                    return True
        return False

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": dict(_BOT_UPLOAD_TOO_LARGE)},
        )
        await response(scope, receive, send)

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        if not self._is_bot_upload(scope):
            await self.app(scope, receive, send)
            return
        if self._declared_too_large(scope):
            # Do not call receive or downstream: an honest oversized request is
            # rejected before multipart parsing can create a spool file.
            await self._reject(scope, receive, send)
            return

        received = 0
        rejected = False

        async def limited_receive() -> Message:
            nonlocal received, rejected
            if rejected:
                # If a defensive downstream catches the size exception and asks
                # again, never expose further client bytes or block on receive.
                return {"type": "http.disconnect"}
            message = await receive()
            if message.get("type") != "http.request":
                # In particular, propagate a real disconnect without turning it
                # into a misleading 413 response.
                return message
            received += len(message.get("body", b""))
            if received > self.max_body_bytes:
                rejected = True
                # The crossing chunk is never returned to Starlette, so its
                # multipart spool cannot grow past the configured body limit.
                raise _BotUploadBodyTooLarge
            return message

        async def limited_send(message: Message) -> None:
            # A downstream which catches our private receive exception must not
            # race its own response against the authoritative 413 below.
            if not rejected:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _BotUploadBodyTooLarge:
            pass
        if rejected:
            await self._reject(scope, limited_receive, send)


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def client_ip(request: Request, *, trust_proxy: bool, hops: int = 1) -> str:
    """解析客户端真实 IP。

    trust_proxy 开启时（部署在 nginx/frp 等反代后）：
    - **优先信 X-Real-IP**（nginx 用 ``X-Real-IP: $remote_addr`` 覆盖式设置，
      客户端无法伪造——比 XFF 最左段可靠）。
    - X-Forwarded-For 取**倒数第 ``hops`` 跳**（受信代理前一跳），而非最左可伪造段。
      ``hops`` = 受信代理层数（env ``BZ_TRUSTED_PROXY_HOPS``，默认 1）。
      攻击者在 XFF 最左塞伪造 IP 不再击穿限流（审计 P1）。
    - 单层 nginx + 覆盖式配置（XFF 只 1 段=$remote_addr）时，最左==最右，行为不变。

    注意：彻底防御需运维正确配 nginx（``set_real_ip_from`` + 覆盖式 XFF，
    见 doc/SECURITY.md）。代码侧此处减少误配的伤害面。
    """
    if trust_proxy:
        # 优先 X-Real-IP（nginx 覆盖，不可被客户端伪造）
        real = request.headers.get("x-real-ip")
        if real:
            return real.strip() or "unknown"
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            if parts:
                # 取倒数第 hops 跳（受信代理前一跳），最左的可伪造
                idx = max(0, len(parts) - max(1, hops))
                return parts[idx] or "unknown"
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
        self.proxy_hops = max(1, _env_int("BZ_TRUSTED_PROXY_HOPS", 1))
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
        # Bot 上传：POST /api/bots（新建）与 POST /api/bots/{id}/versions（新版本）
        if method == "POST" and (
            path == "/api/bots"
            or (path.startswith("/api/bots/") and path.endswith("/versions"))
        ):
            return _UPLOAD_STRICT
        if path == "/api/matches/challenge":
            return _CHALLENGE_STRICT
        if method == "POST" and (
            path == "/api/feedback/bugs"
            or (path.startswith("/api/feedback/bugs/") and path.endswith("/attachments"))
        ):
            return _FEEDBACK_STRICT
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
        ip = client_ip(request, trust_proxy=self.trust_proxy, hops=self.proxy_hops)
        # The same resource commonly has a cheap GET and a stricter mutating
        # POST budget (notably Bot version history vs version upload). Sharing a
        # path-only bucket lets harmless reads consume the write allowance.
        key = f"{ip}:{request.method}:{request.url.path}"
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
        self.proxy_hops = max(1, _env_int("BZ_TRUSTED_PROXY_HOPS", 1))

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
            ip = client_ip(request, trust_proxy=self.trust_proxy, hops=self.proxy_hops)
            _access_logger.info(
                "ip=%s method=%s path=%s status=%s dt=%dms",
                ip, request.method, path, status, dt_ms,
            )
            raise
        dt_ms = int((time.monotonic() - start) * 1000)
        ip = client_ip(request, trust_proxy=self.trust_proxy, hops=self.proxy_hops)
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
    hops = max(1, _env_int("BZ_TRUSTED_PROXY_HOPS", 1))
    ip = client_ip(request, trust_proxy=tp, hops=hops)
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
