"""公网暴露加固：安全响应头 + 内存 IP 限流 + 访问日志 + 安全审计日志。

单进程 uvicorn 用内存限流；多 worker 再换 Redis。
代理身份头需要 BZ_TRUST_PROXY=1 且原始 socket peer 命中
BZ_TRUSTED_PROXY_CIDRS；直连 LAN 永远使用真实 peer。
"""
from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Callable
from ipaddress import IPv4Network, IPv6Network, ip_address, ip_network
from typing import Any
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.formparsers import MultiPartException
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
MULTIPART_OVERHEAD_BYTES = 1024 * 1024
BOT_UPLOAD_MULTIPART_OVERHEAD_BYTES = MULTIPART_OVERHEAD_BYTES
BOT_UPLOAD_BODY_MAX_BYTES = (
    MAX_BOT_BINARY_BYTES + BOT_UPLOAD_MULTIPART_OVERHEAD_BYTES
)
BUG_ATTACHMENT_BODY_MAX_BYTES = 5 * 1024 * 1024 + MULTIPART_OVERHEAD_BYTES
AVATAR_BODY_MAX_BYTES = 2 * 1024 * 1024 + MULTIPART_OVERHEAD_BYTES
_BOT_VERSION_UPLOAD_PATH = re.compile(r"/api/bots/[^/]+/versions")
_BUG_ATTACHMENT_UPLOAD_PATH = re.compile(
    r"/api/feedback/bugs/[^/]+/attachments"
)
_BOT_UPLOAD_TOO_LARGE = {
    "code": "upload_body_too_large",
    "message": "Bot 二进制最大 50 MiB，上传请求体超过允许的 multipart 上限",
}
_BUG_ATTACHMENT_TOO_LARGE = {
    "code": "attachment_body_too_large",
    "message": "Bug 附件最大 5 MiB，上传请求体超过允许的 multipart 上限",
}
_AVATAR_TOO_LARGE = {
    "code": "avatar_body_too_large",
    "message": "头像最大 2 MiB，上传请求体超过允许的 multipart 上限",
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
_DEFAULT_TRUSTED_PROXY_CIDRS = ("127.0.0.1/32", "::1/128")
ProxyNetwork = IPv4Network | IPv6Network


def normalize_public_origin(value: str) -> str:
    """Return one canonical HTTP(S) origin without path/query/credentials."""
    raw = (value or "").strip()
    if not raw or "\\" in raw or any(ord(char) < 0x20 or char.isspace() for char in raw):
        raise ValueError("origin 格式无效")
    try:
        parsed = urlsplit(raw)
        port = parsed.port
    except ValueError as exc:
        raise ValueError("origin 格式无效") from exc
    scheme = parsed.scheme.lower()
    if (
        scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("origin 必须是不含路径、查询或凭据的 HTTP(S) origin")
    host = parsed.hostname
    try:
        if ":" in host:
            host = f"[{host.lower()}]"
        else:
            host = host.encode("idna").decode("ascii").lower()
    except UnicodeError as exc:
        raise ValueError("origin 主机名无效") from exc
    if port == (443 if scheme == "https" else 80):
        port = None
    return f"{scheme}://{host}{f':{port}' if port is not None else ''}"


def websocket_origin_allowed(
    origin: str | None,
    *,
    public_origin: str | None = None,
) -> bool:
    """Fail closed unless a browser Origin matches ``BZ_PUBLIC_ORIGIN`` exactly."""
    expected = (
        os.environ.get("BZ_PUBLIC_ORIGIN", "")
        if public_origin is None
        else public_origin
    )
    if not origin or not expected:
        return False
    try:
        return normalize_public_origin(origin) == normalize_public_origin(expected)
    except ValueError:
        return False


class _UploadBodyTooLarge(MultiPartException):
    """Enter Starlette's multipart error cleanup path for open spool files."""

    def __init__(self) -> None:
        super().__init__("Upload request body exceeded its route limit.")


class BotUploadBodyLimitMiddleware:
    """Bound protected multipart bodies before Starlette creates spooled files.

    ``Content-Length`` is only an early-reject hint.  Every delivered ASGI body
    chunk is still counted, so a missing or forged-small header cannot bypass the
    limit.  Proxy identity headers are deliberately irrelevant to this boundary.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body_bytes: int | None = None,
    ) -> None:
        if max_body_bytes is not None and max_body_bytes < 1:
            raise ValueError("max_body_bytes must be positive")
        self.app = app
        # Test-only override retained for the existing direct ASGI harness. In
        # production each exact route uses its own fixed request envelope.
        self.max_body_bytes = (
            int(max_body_bytes) if max_body_bytes is not None else None
        )

    @staticmethod
    def _is_bot_upload(scope: Scope) -> bool:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return False
        path = str(scope.get("path") or "")
        return path == "/api/bots" or bool(
            _BOT_VERSION_UPLOAD_PATH.fullmatch(path)
        )

    def _policy_for_scope(
        self, scope: Scope
    ) -> tuple[int, dict[str, str]] | None:
        if scope.get("type") != "http" or scope.get("method") != "POST":
            return None
        path = str(scope.get("path") or "")
        if self._is_bot_upload(scope):
            limit = BOT_UPLOAD_BODY_MAX_BYTES
            detail = _BOT_UPLOAD_TOO_LARGE
        elif _BUG_ATTACHMENT_UPLOAD_PATH.fullmatch(path):
            limit = BUG_ATTACHMENT_BODY_MAX_BYTES
            detail = _BUG_ATTACHMENT_TOO_LARGE
        elif path == "/api/auth/avatar":
            limit = AVATAR_BODY_MAX_BYTES
            detail = _AVATAR_TOO_LARGE
        else:
            return None
        return self.max_body_bytes or limit, detail

    @staticmethod
    def _declared_too_large(scope: Scope, max_body_bytes: int) -> bool:
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
                if declared > max_body_bytes:
                    return True
        return False

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        detail: dict[str, str],
    ) -> None:
        response = JSONResponse(
            status_code=413,
            content={"detail": dict(detail)},
        )
        await response(scope, receive, send)

    async def __call__(
        self, scope: Scope, receive: Receive, send: Send
    ) -> None:
        policy = self._policy_for_scope(scope)
        if policy is None:
            await self.app(scope, receive, send)
            return
        max_body_bytes, detail = policy
        if self._declared_too_large(scope, max_body_bytes):
            # Do not call receive or downstream: an honest oversized request is
            # rejected before multipart parsing can create a spool file.
            await self._reject(scope, receive, send, detail)
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
            if received > max_body_bytes:
                rejected = True
                # The crossing chunk is never returned to Starlette, so its
                # multipart spool cannot grow past the configured body limit.
                raise _UploadBodyTooLarge
            return message

        async def limited_send(message: Message) -> None:
            # A downstream which catches our private receive exception must not
            # race its own response against the authoritative 413 below.
            if not rejected:
                await send(message)

        try:
            await self.app(scope, limited_receive, limited_send)
        except _UploadBodyTooLarge:
            pass
        if rejected:
            await self._reject(scope, limited_receive, send, detail)


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


def trusted_proxy_networks(raw: str | None = None) -> tuple[ProxyNetwork, ...]:
    """Parse the only socket peers allowed to supply proxy identity headers.

    A missing/blank setting deliberately defaults to exact IPv4/IPv6 loopback,
    which keeps the local nginx/frp path working without trusting LAN peers.
    Malformed explicit configuration fails startup instead of silently widening
    or disabling the identity boundary.
    """
    configured = os.environ.get("BZ_TRUSTED_PROXY_CIDRS") if raw is None else raw
    if configured is None or not configured.strip():
        entries = list(_DEFAULT_TRUSTED_PROXY_CIDRS)
    else:
        entries = [entry.strip() for entry in configured.split(",")]
        if not entries or any(not entry for entry in entries):
            raise ValueError("BZ_TRUSTED_PROXY_CIDRS 包含空 CIDR")

    networks: list[ProxyNetwork] = []
    for entry in entries:
        try:
            network = ip_network(entry, strict=True)
        except ValueError as exc:
            raise ValueError(
                f"BZ_TRUSTED_PROXY_CIDRS 包含无效 CIDR: {entry}"
            ) from exc
        if network not in networks:
            networks.append(network)
    return tuple(networks)


def validate_server_bind(
    host: str,
    *,
    allow_lan_bind: bool | None = None,
) -> str:
    """Allow loopback by default and wildcard IPv4 only behind an explicit gate."""
    normalized = (host or "").strip().lower()
    if normalized in {"127.0.0.1", "localhost", "::1"}:
        return normalized
    allow_lan = (
        _env_bool("BZ_ALLOW_LAN_BIND", False)
        if allow_lan_bind is None
        else allow_lan_bind
    )
    if normalized == "0.0.0.0" and allow_lan:
        return normalized
    if normalized == "0.0.0.0":
        raise ValueError(
            "BZ_HOST=0.0.0.0 需要显式设置 BZ_ALLOW_LAN_BIND=1，"
            "并先把主机防火墙限制为受信 LAN"
        )
    raise ValueError(
        f"不支持 BZ_HOST={host!r}；只允许 loopback，或经 "
        "BZ_ALLOW_LAN_BIND=1 授权的 0.0.0.0"
    )


def client_ip(
    request: Request,
    *,
    trust_proxy: bool,
    hops: int = 1,
    trusted_proxy_cidrs: tuple[ProxyNetwork, ...] | None = None,
) -> str:
    """解析客户端真实 IP。

    只有同时满足 trust_proxy 开启且 ASGI socket peer 命中
    ``BZ_TRUSTED_PROXY_CIDRS`` 时才读取代理头。缺省 CIDR 仅包含精确
    IPv4/IPv6 loopback；直连 LAN/公网客户端伪造头时始终使用 socket peer。

    对受信代理：
    - **优先信 X-Real-IP**（nginx 用 ``X-Real-IP: $remote_addr`` 覆盖式设置，
      客户端无法伪造——比 XFF 最左段可靠）。
    - X-Forwarded-For 取**倒数第 ``hops`` 跳**（受信代理前一跳），而非最左可伪造段。
      ``hops`` = 受信代理层数（env ``BZ_TRUSTED_PROXY_HOPS``，默认 1）。
      攻击者在 XFF 最左塞伪造 IP 不再击穿限流（审计 P1）。
    - 单层 nginx + 覆盖式配置（XFF 只 1 段=$remote_addr）时，最左==最右，行为不变。

    Uvicorn 自带的 proxy-header 重写必须关闭，确保这里看到的
    ``request.client`` 仍是不可伪造的 socket peer。
    """
    peer = (
        request.client.host
        if request.client and request.client.host
        else "unknown"
    )
    if trust_proxy and peer != "unknown":
        try:
            peer_address = ip_address(peer)
        except ValueError:
            peer_address = None
        networks = (
            trusted_proxy_networks()
            if trusted_proxy_cidrs is None
            else trusted_proxy_cidrs
        )
        peer_is_trusted = peer_address is not None and any(
            peer_address.version == network.version and peer_address in network
            for network in networks
        )
    else:
        peer_is_trusted = False

    if peer_is_trusted:
        # X-Real-IP is accepted only from an allowlisted proxy socket peer.
        real = request.headers.get("x-real-ip")
        if real is not None:
            try:
                return ip_address(real.strip()).compressed
            except ValueError:
                # An allowlisted proxy that emits X-Real-IP owns this identity
                # boundary.  If that authoritative header is malformed, do not
                # silently fall through to a potentially client-supplied XFF.
                return peer
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            parts = [p.strip() for p in forwarded.split(",") if p.strip()]
            trusted_hops = max(1, hops)
            if len(parts) >= trusted_hops:
                # 取倒数第 hops 跳（受信代理前一跳），最左的可伪造
                candidate = parts[-trusted_hops]
                try:
                    return ip_address(candidate).compressed
                except ValueError:
                    pass
    return peer


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

    def __init__(
        self,
        app: ASGIApp,
        *,
        enabled: bool | None = None,
        trusted_proxy_cidrs: tuple[ProxyNetwork, ...] | None = None,
    ) -> None:
        super().__init__(app)
        self.enabled = (
            _env_bool("BZ_RATE_LIMIT", True) if enabled is None else enabled
        )
        self.trust_proxy = _env_bool("BZ_TRUST_PROXY", False)
        self.proxy_hops = max(1, _env_int("BZ_TRUSTED_PROXY_HOPS", 1))
        self.trusted_proxy_cidrs = (
            trusted_proxy_networks()
            if trusted_proxy_cidrs is None
            else trusted_proxy_cidrs
        )
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
        ip = client_ip(
            request,
            trust_proxy=self.trust_proxy,
            hops=self.proxy_hops,
            trusted_proxy_cidrs=self.trusted_proxy_cidrs,
        )
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
        "trusted_proxy_cidrs": [
            str(network) for network in trusted_proxy_networks()
        ],
        "hsts": _env_bool("BZ_HSTS", False),
        "secure_cookie": _env_bool("BZ_SECURE_COOKIE", False),
    }


class AccessLogMiddleware(BaseHTTPMiddleware):
    """每个 HTTP 请求记一行访问日志（含真实客户端 IP）到 logs/access.log。

    格式：``ip=<IP> method=<METHOD> path=<path> status=<状态码> dt=<耗时ms>``
    IP 经 ``client_ip()`` 解析；只有受信 socket peer 才能提供代理身份头。
    跳过静态资源与 /api/health，避免噪音。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        trust_proxy: bool | None = None,
        trusted_proxy_cidrs: tuple[ProxyNetwork, ...] | None = None,
    ) -> None:
        super().__init__(app)
        self.trust_proxy = (
            _env_bool("BZ_TRUST_PROXY", False) if trust_proxy is None else trust_proxy
        )
        self.proxy_hops = max(1, _env_int("BZ_TRUSTED_PROXY_HOPS", 1))
        self.trusted_proxy_cidrs = (
            trusted_proxy_networks()
            if trusted_proxy_cidrs is None
            else trusted_proxy_cidrs
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
            ip = client_ip(
                request,
                trust_proxy=self.trust_proxy,
                hops=self.proxy_hops,
                trusted_proxy_cidrs=self.trusted_proxy_cidrs,
            )
            _access_logger.info(
                "ip=%s method=%s path=%s status=%s dt=%dms",
                ip, request.method, path, status, dt_ms,
            )
            raise
        dt_ms = int((time.monotonic() - start) * 1000)
        ip = client_ip(
            request,
            trust_proxy=self.trust_proxy,
            hops=self.proxy_hops,
            trusted_proxy_cidrs=self.trusted_proxy_cidrs,
        )
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
    configured_networks = getattr(
        getattr(request, "app", None),
        "state",
        None,
    )
    networks = getattr(configured_networks, "trusted_proxy_cidrs", None)
    ip = client_ip(
        request,
        trust_proxy=tp,
        hops=hops,
        trusted_proxy_cidrs=networks,
    )
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
