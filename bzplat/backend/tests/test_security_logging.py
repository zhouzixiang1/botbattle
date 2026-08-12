"""安全日志与审计测试：access.log + audit.log + 真实 IP 透传 + 验证码脱敏。

覆盖公网暴露加固（PR feat/security-logging）：
- logging_config 三 handler（app/access/audit 独立文件 + propagate 隔离）。
- AccessLogMiddleware 只接受受信 socket peer 提供的代理身份头。
- audit_log 辅助函数格式（ip/action/result/user/target/detail）+ result=fail 升 WARNING。
- admin_logs file 参数（app/access/audit 三文件白名单、响应不泄漏绝对路径）。
- admin_logs 按结构化记录过滤，多行 ERROR 保留 traceback 与对局上下文。
- 验证码脱敏（SMTP 未配置时不打明文 code 到日志）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest
from fastapi import FastAPI, File, UploadFile
from fastapi.testclient import TestClient

from bzplat.backend import security as security_module
from bzplat.backend.crypto import hash_password
from bzplat.backend.logging_config import (
    ACCESS_LOGGER,
    AUDIT_LOGGER,
    UvicornRequestTargetFilter,
    setup_logging,
)
from bzplat.backend.security import (
    AVATAR_BODY_MAX_BYTES,
    BOT_UPLOAD_BODY_MAX_BYTES,
    BUG_ATTACHMENT_BODY_MAX_BYTES,
    BotUploadBodyLimitMiddleware,
    RateLimitMiddleware,
    audit_log,
    client_ip,
    normalize_public_origin,
    trusted_proxy_networks,
    validate_server_bind,
    websocket_origin_allowed,
)


@pytest.fixture
def log_dir(tmp_path):
    d = tmp_path / "logs"
    d.mkdir()
    setup_logging(log_dir=d, level="INFO")
    return d


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.is_file() else ""


def test_public_origin_normalization_and_exact_websocket_match():
    assert normalize_public_origin("HTTPS://Example.COM:443/") == "https://example.com"
    assert normalize_public_origin("http://127.0.0.1:50381") == "http://127.0.0.1:50381"
    assert websocket_origin_allowed(
        "HTTPS://Example.COM:443/", public_origin="https://example.com"
    )
    assert not websocket_origin_allowed(
        "https://other.example", public_origin="https://example.com"
    )


def test_lan_http_origin_cannot_replace_public_https_websocket_origin():
    """LAN HTTP may serve pages/REST, but cookie-only human WS stays HTTPS-only."""
    public_origin = "https://bot.tydfxt.top"

    assert websocket_origin_allowed(public_origin, public_origin=public_origin)
    assert not websocket_origin_allowed(
        "http://192.168.1.13:50380",
        public_origin=public_origin,
    )


@pytest.mark.parametrize(
    "origin",
    [
        "",
        "null",
        "ws://example.com",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?query=1",
        "https://example.com#fragment",
        "https://example.com\\@evil.test",
    ],
)
def test_public_origin_rejects_missing_or_non_origin_values(origin):
    assert not websocket_origin_allowed(origin, public_origin="https://example.com")


def test_websocket_origin_fails_closed_without_public_origin(monkeypatch):
    monkeypatch.delenv("BZ_PUBLIC_ORIGIN", raising=False)
    assert not websocket_origin_allowed("https://example.com")
    assert not websocket_origin_allowed(
        "https://example.com", public_origin="https://example.com/path"
    )


def test_uvicorn_filter_projects_http_and_websocket_targets_to_path_only():
    secret = "session-secret-must-not-survive"
    request_filter = UvicornRequestTargetFilter()
    http_record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (("127.0.0.1", 1234), "GET", f"/api/items?token={secret}", "1.1", 200),
        None,
    )
    ws_record = logging.LogRecord(
        "uvicorn.error",
        logging.INFO,
        __file__,
        1,
        '%s - "WebSocket %s" [accepted]',
        (("127.0.0.1", 1234), f"/api/matches/m1/play?token={secret}"),
        None,
    )

    assert request_filter.filter(http_record)
    assert request_filter.filter(ws_record)
    assert http_record.args[1:] == ("GET", "/api/items", "1.1", 200)
    assert ws_record.args[1] == "/api/matches/m1/play"
    assert secret not in http_record.getMessage()
    assert secret not in ws_record.getMessage()


def test_uvicorn_http_and_websocket_queries_never_reach_serialized_logs(
    log_dir, capsys
):
    http_secret = "http-query-secret"
    ws_secret = "websocket-query-secret"
    logging.getLogger("uvicorn.access").info(
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", 1234),
        "GET",
        f"/api/history?token={http_secret}&page=2",
        "1.1",
        200,
    )
    logging.getLogger("uvicorn.error").info(
        '%s - "WebSocket %s" 403',
        ("127.0.0.1", 1234),
        f"/api/matches/m1/play?token={ws_secret}",
    )
    for logger_name in ("uvicorn.access", "uvicorn.error"):
        for handler in logging.getLogger(logger_name).handlers:
            handler.flush()

    content = _read(log_dir / "app.log")
    captured = capsys.readouterr()
    console = captured.out + captured.err
    assert http_secret not in content
    assert ws_secret not in content
    assert http_secret not in console
    assert ws_secret not in console
    assert 'GET /api/history HTTP/1.1" 200' in content
    assert 'WebSocket /api/matches/m1/play" 403' in content


# ── logging_config：三 handler 独立文件 + propagate 隔离 ────────────────────


def test_three_log_files_created(log_dir):
    """setup_logging 后应创建 app.log / access.log / audit.log 三个文件。"""
    # 触发一条各 logger
    logging.getLogger("bzplat.backend").info("app msg")
    logging.getLogger(ACCESS_LOGGER).info("access msg")
    logging.getLogger(AUDIT_LOGGER).info("audit msg")
    for name in ("app.log", "access.log", "audit.log"):
        # 文件可能尚未落盘（buffer），flush 一下
        for h in logging.getLogger().handlers + logging.getLogger(ACCESS_LOGGER).handlers + logging.getLogger(AUDIT_LOGGER).handlers:
            try:
                h.flush()
            except Exception:
                pass
    # access/audit 各自的 handler 目标文件
    assert (log_dir / "app.log").is_file()
    assert (log_dir / "access.log").is_file()
    assert (log_dir / "audit.log").is_file()


def test_access_audit_do_not_leak_to_app_log(log_dir):
    """access/audit logger 的消息不应进 app.log（propagate=False）。"""
    logging.getLogger(ACCESS_LOGGER).info("ACCESS_ONLY_MARKER")
    logging.getLogger(AUDIT_LOGGER).info("AUDIT_ONLY_MARKER")
    for h in logging.getLogger().handlers + logging.getLogger(ACCESS_LOGGER).handlers + logging.getLogger(AUDIT_LOGGER).handlers:
        try:
            h.flush()
        except Exception:
            pass
    app_content = _read(log_dir / "app.log")
    assert "ACCESS_ONLY_MARKER" not in app_content, "access 日志不应泄漏到 app.log"
    assert "AUDIT_ONLY_MARKER" not in app_content, "audit 日志不应泄漏到 app.log"
    # 但各自文件里要有
    assert "ACCESS_ONLY_MARKER" in _read(log_dir / "access.log")
    assert "AUDIT_ONLY_MARKER" in _read(log_dir / "audit.log")


# ── client_ip：trust_proxy 解析 X-Forwarded-For ─────────────────────────────


class _FakeReq:
    """最小化 Request 替身，用于 client_ip 单测。"""

    def __init__(self, headers: dict[str, str], host: str = "127.0.0.1"):
        self.headers = headers
        self.client = type("C", (), {"host": host})()


def test_client_ip_trust_proxy_reads_xff_rightmost():
    """XFF 取倒数第 hops 跳（受信代理前一跳），非最左可伪造段（审计 P1）。"""
    # 单层 nginx（覆盖式 XFF 只 1 段）：最左==最右，行为不变
    req = _FakeReq({"x-forwarded-for": "203.0.113.5"})
    assert client_ip(req, trust_proxy=True, hops=1) == "203.0.113.5"
    # 追加式 XFF（客户端塞伪造最左 + nginx 追加真实段）：取最右（nginx 加的）
    req = _FakeReq({"x-forwarded-for": "999.999.999.999, 203.0.113.5"})
    assert client_ip(req, trust_proxy=True, hops=1) == "203.0.113.5"


def test_client_ip_trust_proxy_prefers_real_ip():
    """优先 X-Real-IP（nginx 覆盖式设置，客户端无法伪造）。"""
    req = _FakeReq({"x-real-ip": "198.51.100.7", "x-forwarded-for": "1.2.3.4"})
    assert client_ip(req, trust_proxy=True) == "198.51.100.7"


def test_client_ip_no_trust_proxy_uses_socket_peer():
    req = _FakeReq({"x-forwarded-for": "203.0.113.5"}, host="127.0.0.1")
    assert client_ip(req, trust_proxy=False) == "127.0.0.1"


def test_client_ip_direct_lan_peer_cannot_spoof_proxy_headers():
    """A direct LAN caller is not a proxy, even when global proxy mode is on."""
    req = _FakeReq(
        {
            "x-real-ip": "198.51.100.7",
            "x-forwarded-for": "203.0.113.5",
        },
        host="192.168.1.42",
    )

    assert client_ip(req, trust_proxy=True) == "192.168.1.42"


def test_rate_limit_direct_lan_cannot_rotate_spoofed_proxy_headers(
    monkeypatch,
):
    monkeypatch.setenv("BZ_TRUST_PROXY", "1")
    monkeypatch.delenv("BZ_TRUSTED_PROXY_CIDRS", raising=False)
    monkeypatch.setattr(security_module, "_API_DEFAULT", (1, 60))
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, enabled=True)

    @app.get("/api/direct-lan-probe")
    def direct_lan_probe():
        return {"ok": True}

    with TestClient(app, client=("192.168.1.42", 50000)) as client:
        first = client.get(
            "/api/direct-lan-probe",
            headers={"X-Real-IP": "198.51.100.1"},
        )
        second = client.get(
            "/api/direct-lan-probe",
            headers={"X-Real-IP": "198.51.100.2"},
        )

    assert first.status_code == 200
    assert second.status_code == 429


def test_client_ip_only_trusts_explicit_proxy_peer_cidrs():
    req = _FakeReq({"x-real-ip": "198.51.100.7"}, host="10.20.30.40")

    assert client_ip(
        req,
        trust_proxy=True,
        trusted_proxy_cidrs=trusted_proxy_networks("10.20.30.40/32"),
    ) == "198.51.100.7"
    assert client_ip(
        req,
        trust_proxy=True,
        trusted_proxy_cidrs=trusted_proxy_networks("10.20.30.41/32"),
    ) == "10.20.30.40"


def test_trusted_proxy_cidrs_default_loopback_and_invalid_config_fails(monkeypatch):
    monkeypatch.delenv("BZ_TRUSTED_PROXY_CIDRS", raising=False)
    assert {str(network) for network in trusted_proxy_networks()} == {
        "127.0.0.1/32",
        "::1/128",
    }
    with pytest.raises(ValueError, match="无效 CIDR"):
        trusted_proxy_networks("127.0.0.1/32,not-a-network")
    with pytest.raises(ValueError, match="无效 CIDR"):
        trusted_proxy_networks("192.168.1.5/24")


def test_client_ip_malformed_or_short_proxy_chain_falls_back_to_peer():
    malformed = _FakeReq(
        {"x-real-ip": "not-an-ip", "x-forwarded-for": "also-bad"}
    )
    malformed_authoritative = _FakeReq(
        {"x-real-ip": "not-an-ip", "x-forwarded-for": "198.51.100.7"}
    )
    short_chain = _FakeReq({"x-forwarded-for": "203.0.113.5"})

    assert client_ip(malformed, trust_proxy=True) == "127.0.0.1"
    assert client_ip(malformed_authoritative, trust_proxy=True) == "127.0.0.1"
    assert client_ip(short_chain, trust_proxy=True, hops=2) == "127.0.0.1"


def test_server_bind_requires_explicit_gate_for_ipv4_wildcard():
    assert validate_server_bind("127.0.0.1", allow_lan_bind=False) == "127.0.0.1"
    assert validate_server_bind("0.0.0.0", allow_lan_bind=True) == "0.0.0.0"
    with pytest.raises(ValueError, match="BZ_ALLOW_LAN_BIND=1"):
        validate_server_bind("0.0.0.0", allow_lan_bind=False)
    with pytest.raises(ValueError, match="不支持"):
        validate_server_bind("192.168.1.10", allow_lan_bind=True)


def test_client_ip_xff_spoofed_leftmost_ignored():
    """攻击者在 XFF 最左塞伪造 IP 不应击穿（取最右可信跳）。"""
    req = _FakeReq({"x-forwarded-for": "1.1.1.1, 2.2.2.2, 3.3.3.3"})
    # hops=1 → 取最后 1 段（3.3.3.3 = 受信代理加的真实客户端段）
    assert client_ip(req, trust_proxy=True, hops=1) == "3.3.3.3"
    # hops=2 → 取倒数第 2 段（2.2.2.2，双层代理场景）
    assert client_ip(req, trust_proxy=True, hops=2) == "2.2.2.2"
    # 关键：最左的 1.1.1.1（可伪造）绝不被取
    assert client_ip(req, trust_proxy=True, hops=1) != "1.1.1.1"


def test_rate_limit_separates_reads_from_upload_writes():
    """刷新版本历史不能提前耗尽同一路径 POST 的 6 次上传额度。"""
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, enabled=True)

    @app.get("/api/bots/7/versions")
    def read_versions():
        return {"versions": []}

    @app.post("/api/bots/7/versions")
    def upload_version():
        return {"ok": True}

    with TestClient(app) as client:
        for _ in range(10):
            assert client.get("/api/bots/7/versions").status_code == 200
        for _ in range(6):
            assert client.post("/api/bots/7/versions").status_code == 200
        limited = client.post("/api/bots/7/versions")

    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limit_exceeded"


def _asgi_scope(
    path: str = "/api/bots",
    *,
    method: str = "POST",
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": headers or [],
        "client": ("127.0.0.1", 12345),
        "server": ("test", 80),
    }


def test_upload_body_limit_rejects_declared_size_without_receive_or_downstream():
    called = False
    receive_calls = 0
    sent: list[dict] = []

    async def downstream(_scope, _receive, _send):
        nonlocal called
        called = True

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return {"type": "http.request", "body": b"never", "more_body": False}

    async def send(message):
        sent.append(message)

    async def exercise():
        limiter = BotUploadBodyLimitMiddleware(downstream, max_body_bytes=8)
        await limiter(
            _asgi_scope(
                headers=[
                    (b"content-length", b"9"),
                    (b"x-forwarded-for", b"203.0.113.77"),
                ]
            ),
            receive,
            send,
        )

    asyncio.run(exercise())
    assert called is False
    assert receive_calls == 0
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    assert sent[0]["status"] == 413
    body = json.loads(sent[1]["body"])
    assert body["detail"]["code"] == "upload_body_too_large"


@pytest.mark.parametrize(
    ("path", "limit", "code"),
    [
        ("/api/bots", BOT_UPLOAD_BODY_MAX_BYTES, "upload_body_too_large"),
        (
            "/api/feedback/bugs/bug_test/attachments",
            BUG_ATTACHMENT_BODY_MAX_BYTES,
            "attachment_body_too_large",
        ),
        ("/api/auth/avatar", AVATAR_BODY_MAX_BYTES, "avatar_body_too_large"),
    ],
)
def test_upload_body_limit_uses_exact_route_envelopes(path, limit, code):
    downstream_calls = 0

    async def downstream(_scope, _receive, send):
        nonlocal downstream_calls
        downstream_calls += 1
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        raise AssertionError("declared-size decision must not read request body")

    async def invoke(declared):
        sent = []

        async def send(message):
            sent.append(message)

        limiter = BotUploadBodyLimitMiddleware(downstream)
        await limiter(
            _asgi_scope(
                path,
                headers=[(b"content-length", str(declared).encode())],
            ),
            receive,
            send,
        )
        return sent

    accepted = asyncio.run(invoke(limit))
    rejected = asyncio.run(invoke(limit + 1))
    assert downstream_calls == 1
    assert accepted[0]["status"] == 204
    assert rejected[0]["status"] == 413
    assert json.loads(rejected[1]["body"])["detail"]["code"] == code


@pytest.mark.parametrize(
    ("path", "expected_code"),
    [
        ("/api/bots", "upload_body_too_large"),
        (
            "/api/feedback/bugs/bug_test/attachments",
            "attachment_body_too_large",
        ),
        ("/api/auth/avatar", "avatar_body_too_large"),
    ],
)
@pytest.mark.parametrize(
    "headers",
    [
        [(b"transfer-encoding", b"chunked")],
        [(b"content-length", b"1")],
    ],
    ids=["chunked-no-length", "forged-small-length"],
)
def test_upload_body_limit_counts_chunks_and_disconnects_caught_downstream(
    path, expected_code, headers,
):
    messages = iter(
        [
            {"type": "http.request", "body": b"1234", "more_body": True},
            {"type": "http.request", "body": b"5678", "more_body": True},
            {"type": "http.request", "body": b"9", "more_body": True},
            {"type": "http.request", "body": b"not-read", "more_body": False},
        ]
    )
    receive_calls = 0
    delivered: list[dict] = []
    after_reject: list[dict] = []
    sent: list[dict] = []

    async def receive():
        nonlocal receive_calls
        receive_calls += 1
        return next(messages)

    async def downstream(_scope, limited_receive, _send):
        try:
            while True:
                message = await limited_receive()
                delivered.append(message)
                if not message.get("more_body"):
                    return
        except Exception:
            # Defensive downstream code cannot resume reading client bytes after
            # the crossing chunk; it sees a synthetic disconnect instead.
            after_reject.append(await limited_receive())

    async def send(message):
        sent.append(message)

    async def exercise():
        limiter = BotUploadBodyLimitMiddleware(downstream, max_body_bytes=8)
        await limiter(
            _asgi_scope(path, headers=headers),
            receive,
            send,
        )

    asyncio.run(exercise())
    assert [message["body"] for message in delivered] == [b"1234", b"5678"]
    assert sum(len(message["body"]) for message in delivered) == 8
    assert after_reject == [{"type": "http.disconnect"}]
    assert receive_calls == 3
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["detail"]["code"] == expected_code


def test_upload_body_limit_preserves_real_disconnect_without_413():
    messages = iter(
        [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.disconnect"},
        ]
    )
    delivered: list[dict] = []
    sent: list[dict] = []

    async def receive():
        return next(messages)

    async def downstream(_scope, limited_receive, _send):
        delivered.append(await limited_receive())
        delivered.append(await limited_receive())

    async def send(message):
        sent.append(message)

    async def exercise():
        limiter = BotUploadBodyLimitMiddleware(downstream, max_body_bytes=8)
        await limiter(_asgi_scope(), receive, send)

    asyncio.run(exercise())
    assert [message["type"] for message in delivered] == [
        "http.request",
        "http.disconnect",
    ]
    assert sent == []


@pytest.mark.parametrize(
    ("method", "path", "limited"),
    [
        ("POST", "/api/bots/7/versions", True),
        ("POST", "/api/bots/not-an-int/versions", True),
        ("POST", "/api/feedback/bugs/bug_test/attachments", True),
        ("POST", "/api/auth/avatar", True),
        ("GET", "/api/bots", False),
        ("GET", "/api/auth/avatar", False),
        ("POST", "/api/feedback/bugs/bug_test/attachments/", False),
        ("POST", "/api/bots/7/versions/", False),
        ("POST", "/api/bots/7/versions/extra", False),
        ("POST", "/api/bots-extra", False),
    ],
)
def test_upload_body_limit_matches_only_exact_upload_routes(method, path, limited):
    called = False
    sent: list[dict] = []

    async def downstream(_scope, _receive, send):
        nonlocal called
        called = True
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    async def exercise():
        limiter = BotUploadBodyLimitMiddleware(downstream, max_body_bytes=8)
        await limiter(
            _asgi_scope(
                path,
                method=method,
                headers=[(b"content-length", b"9")],
            ),
            receive,
            send,
        )

    asyncio.run(exercise())
    assert called is (not limited)
    assert sent[0]["status"] == (413 if limited else 204)


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/api/bots", "upload_body_too_large"),
        (
            "/api/feedback/bugs/bug_test/attachments",
            "attachment_body_too_large",
        ),
        ("/api/auth/avatar", "avatar_body_too_large"),
    ],
)
def test_upload_body_limit_http_response_is_structured_413(path, code):
    app = FastAPI()
    app.add_middleware(BotUploadBodyLimitMiddleware, max_body_bytes=256)
    endpoint_called = False

    async def upload(file: UploadFile = File(...)):
        nonlocal endpoint_called
        endpoint_called = True
        return {"size": len(await file.read())}

    app.add_api_route(path, upload, methods=["POST"])

    response = TestClient(app).post(
        path,
        files={"file": ("bot.bin", b"x" * 512, "application/octet-stream")},
    )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == code
    assert endpoint_called is False


@pytest.mark.parametrize(
    ("path", "code"),
    [
        ("/api/bots", "upload_body_too_large"),
        (
            "/api/feedback/bugs/bug_test/attachments",
            "attachment_body_too_large",
        ),
        ("/api/auth/avatar", "avatar_body_too_large"),
    ],
)
def test_chunked_multipart_limit_closes_rolled_spool(
    monkeypatch, path, code
):
    """A receive-time 413 must enter Starlette's open-file cleanup branch."""
    import tempfile
    import starlette.formparsers as formparsers

    created = []
    real_spooled_file = tempfile.SpooledTemporaryFile

    def tracking_spooled_file(*args, **kwargs):
        kwargs["max_size"] = 8
        spool = real_spooled_file(*args, **kwargs)
        created.append(spool)
        return spool

    monkeypatch.setattr(formparsers, "SpooledTemporaryFile", tracking_spooled_file)

    app = FastAPI()
    app.add_middleware(BotUploadBodyLimitMiddleware, max_body_bytes=256)
    endpoint_called = False

    async def upload(file: UploadFile = File(...)):
        nonlocal endpoint_called
        endpoint_called = True
        return {"size": len(await file.read())}

    app.add_api_route(path, upload, methods=["POST"])

    boundary = b"botbattle-boundary"
    body = (
        b"--" + boundary
        + b'\r\nContent-Disposition: form-data; name="file"; filename="bot.bin"'
        + b"\r\nContent-Type: application/octet-stream\r\n\r\n"
        + (b"x" * 512)
        + b"\r\n--" + boundary + b"--\r\n"
    )
    chunks = [body[offset : offset + 64] for offset in range(0, len(body), 64)]
    sent: list[dict] = []
    index = 0

    async def receive():
        nonlocal index
        chunk = chunks[index]
        index += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": index < len(chunks),
        }

    async def send(message):
        sent.append(message)

    async def exercise():
        await app(
            _asgi_scope(
                path,
                headers=[
                    (
                        b"content-type",
                        b"multipart/form-data; boundary=" + boundary,
                    ),
                    (b"transfer-encoding", b"chunked"),
                ]
            ),
            receive,
            send,
        )

    asyncio.run(exercise())
    assert endpoint_called is False
    assert [message["type"] for message in sent] == [
        "http.response.start",
        "http.response.body",
    ]
    assert sent[0]["status"] == 413
    assert json.loads(sent[1]["body"])["detail"]["code"] == code
    assert len(created) == 1
    assert created[0]._rolled is True
    assert created[0].closed is True


# ── audit_log：格式 + result=fail 升级为 WARNING ─────────────────────────────


def test_audit_log_writes_structured_fields(log_dir):
    """audit_log 应记 ip/action/result/user/target/detail 到 audit.log。"""
    req = _FakeReq({"x-forwarded-for": "203.0.113.9"})
    audit_log(
        req, "login", result="ok", user="alice", target="alice", detail="pwd ok",
        trust_proxy=True,
    )
    for h in logging.getLogger(AUDIT_LOGGER).handlers:
        try:
            h.flush()
        except Exception:
            pass
    content = _read(log_dir / "audit.log")
    assert "action=login" in content
    assert "result=ok" in content
    assert "user=alice" in content
    assert "ip=203.0.113.9" in content


def test_audit_log_fail_is_warning(log_dir):
    """result=fail 应记为 WARNING 级别（安全事件优先关注）。"""
    req = _FakeReq({})
    audit_log(req, "login", result="fail", target="bob", detail="invalid_credentials")
    for h in logging.getLogger(AUDIT_LOGGER).handlers:
        try:
            h.flush()
        except Exception:
            pass
    content = _read(log_dir / "audit.log")
    assert " WARNING " in content
    assert "result=fail" in content


# ── admin_logs file 参数：三文件白名单 + 防路径穿越 ──────────────────────────


@pytest.fixture
def admin_client(tmp_path, monkeypatch):
    """起一个带 admin 的测试 app，BZ_LOG_DIR 指向临时目录，BZ_TEST_CAPTCHA 绕验证码。"""
    from bzplat.backend.crypto import hash_password
    from bzplat.backend.store import Store
    from bzplat.backend.main import create_app

    db_path = str(tmp_path / "t.db")
    monkeypatch.setenv("BZ_DB_PATH", db_path)
    monkeypatch.setenv("BZ_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BZ_TRUST_PROXY", "1")
    monkeypatch.setenv("BZ_TEST_CAPTCHA", "1")
    logd = tmp_path / "logs"
    logd.mkdir(exist_ok=True)
    # 预写三文件
    (logd / "app.log").write_text("APP_LINE_TEST\n", encoding="utf-8")
    (logd / "access.log").write_text("ACCESS_LINE_TEST ip=1.2.3.4\n", encoding="utf-8")
    (logd / "audit.log").write_text("AUDIT_LINE_TEST action=login\n", encoding="utf-8")

    # 先建 admin（create_app 会打开同一个 DB）
    store = Store(db_path)
    u = store.create_user("adminu", "a@ex.com", hash_password("pw123456"), role="admin")
    store.update_user(u["id"], email_verified=1)  # 登录要求邮箱已验证
    store.close()

    app = create_app()
    client = TestClient(app)
    # 取验证码（test 模式返回 answer）
    cap = client.get("/api/auth/captcha").json()
    r = client.post("/api/auth/login", json={
        "username": "adminu", "password": "pw123456",
        "captcha_id": cap["captcha_id"], "captcha_answer": cap["answer"],
    })
    token = r.json().get("token", "")
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_admin_logs_default_app(admin_client):
    r = admin_client.get("/api/admin/logs")
    assert r.status_code == 200
    lines = r.json()["lines"]
    assert any("APP_LINE_TEST" in ln for ln in lines)


def test_admin_logs_file_access(admin_client):
    r = admin_client.get("/api/admin/logs?file=access")
    assert r.status_code == 200
    lines = r.json()["lines"]
    assert any("ACCESS_LINE_TEST" in ln for ln in lines)
    assert r.json()["source"] == "access.log"
    assert "path" not in r.json()


def test_admin_logs_file_audit(admin_client):
    r = admin_client.get("/api/admin/logs?file=audit")
    assert r.status_code == 200
    lines = r.json()["lines"]
    assert any("AUDIT_LINE_TEST" in ln for ln in lines)
    assert r.json()["source"] == "audit.log"


def test_admin_logs_error_filter_keeps_complete_traceback(admin_client, tmp_path):
    """ERROR/q 命中整条记录时，续行 traceback 与首行 match 上下文均保留。"""
    (tmp_path / "logs" / "app.log").write_text(
        "2026-08-09 10:00:00 ERROR [bzplat.backend.matches] "
        "match_id=77 bot_id=9 crashed\n"
        "Traceback (most recent call last):\n"
        "  File \"runner.py\", line 42, in run\n"
        "RuntimeError: bot process exited\n"
        "2026-08-09 10:00:01 INFO [bzplat.backend.matches] match_id=78 completed\n",
        encoding="utf-8",
    )

    r = admin_client.get(
        "/api/admin/logs?file=app&level=ERROR&q=RuntimeError&limit=1"
    )

    assert r.status_code == 200
    lines = r.json()["lines"]
    assert len(lines) == 4  # limit 不切断单条多行记录
    assert "match_id=77" in lines[0]
    assert "Traceback (most recent call last)" in lines[1]
    assert "runner.py" in lines[2]
    assert "RuntimeError: bot process exited" in lines[3]
    assert not any("match_id=78" in line for line in lines)


def test_admin_logs_rejects_unknown_file(admin_client):
    """file 参数不在白名单 → 回退 app.log（防路径穿越读任意文件）。"""
    r = admin_client.get("/api/admin/logs?file=../../etc/passwd")
    assert r.status_code == 200
    assert r.json()["source"] == "app.log"  # 回退 app，绝不读 /etc/passwd
    assert "path" not in r.json()


# ── 验证码脱敏：SMTP 未配置时不打明文 code ──────────────────────────────────


def test_captcha_not_logged_in_plaintext(tmp_path, caplog):
    """排队路径不记录验证码明文（SMTP 未配置也不在请求线程报错）。"""
    from bzplat.backend.auth.auth_manager import AuthManager
    from bzplat.backend.store import Store

    store = Store(str(tmp_path / "c.db"))
    user = store.create_user("masku", "m@ex.com", hash_password("pw123456"))
    store.update_user(user["id"], email_verified=0)
    # mailer=None 模拟 SMTP 未配置
    am = AuthManager(store, mailer=None)
    with caplog.at_level(logging.WARNING):
        am.send_verify_code(store.get_user(user["id"]))
    # 收集所有日志消息，不应含完整 6 位验证码
    full_text = " ".join(r.getMessage() for r in caplog.records)
    # 脱敏后只出现 code=XX*** 形式，不应出现连续 6 位数字的明文 code
    import re
    # 找 "code=" 后跟的内容，不应是 6 位纯数字明文
    m = re.search(r"code=(\S+)", full_text)
    if m:
        val = m.group(1)
        assert not re.fullmatch(r"\d{6}", val), f"验证码明文泄漏到日志: code={val}"
    code = store.get_latest_email_code(user["id"], "verify")["code"]
    assert code not in full_text
