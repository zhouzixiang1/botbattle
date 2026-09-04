"""HTTP 边界安全回归：静态文件、请求体、Cookie CSRF 与动态限流桶。"""
from __future__ import annotations

import asyncio
import json
import os
import uuid
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from fastapi.responses import JSONResponse, Response
from starlette.requests import Request

from bzplat.backend import security as security_module
from bzplat.backend.auth.auth_manager import COOKIE_NAME
from bzplat.backend.auth.routes import (
    ChangePasswordReq,
    LoginReq,
    ProfileUpdateReq,
    RegisterReq,
    RequestResetReq,
    ResendVerifyReq,
    ResetPasswordReq,
    VerifyEmailReq,
)
from bzplat.backend.main import create_app
from bzplat.backend.security import RateLimitMiddleware


_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _hardened_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setenv("BZ_RATE_LIMIT", "0")
    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "https://bot.example")
    return create_app(db_path=str(tmp_path / "http-boundaries.db"))


@pytest.mark.skipif(not _DIST.is_dir(), reason="frontend/dist 尚未构建")
@pytest.mark.parametrize(
    "attack_path",
    [
        "/%2e%2e/%2e%2e/%2e%2e/pyproject.toml",
        "/%2e%2e%2f%2e%2e%2f%2e%2e%2fpyproject.toml",
        "/..%2f..%2f..%2fpyproject.toml",
    ],
)
def test_spa_catch_all_rejects_encoded_parent_traversal(
    tmp_path, monkeypatch, attack_path
):
    expected_secret = (_PROJECT_ROOT / "pyproject.toml").read_bytes()
    app = _hardened_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.get(attack_path)

    assert response.status_code == 404
    assert response.content != expected_secret


@pytest.mark.skipif(not _DIST.is_dir(), reason="frontend/dist 尚未构建")
def test_spa_catch_all_rejects_symlink_to_file_outside_dist(
    tmp_path, monkeypatch
):
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("OUTSIDE-SECRET-DO-NOT-SERVE", encoding="utf-8")
    link = _DIST / f"security-probe-{uuid.uuid4().hex}"
    link.symlink_to(outside)
    try:
        app = _hardened_app(tmp_path, monkeypatch)
        with TestClient(app) as client:
            response = client.get(f"/{link.name}")
    finally:
        link.unlink(missing_ok=True)

    assert response.status_code == 404
    assert outside.read_bytes() not in response.content


@pytest.mark.skipif(not _DIST.is_dir(), reason="frontend/dist 尚未构建")
def test_spa_keeps_index_deep_links_and_public_root_files(tmp_path, monkeypatch):
    app = _hardened_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        index = client.get("/")
        deep_link = client.get("/arena")
        favicon = client.get("/favicon.svg")

    assert index.status_code == 200
    assert deep_link.status_code == 200
    assert deep_link.content == index.content
    assert favicon.status_code == 200
    assert favicon.content == (_DIST / "favicon.svg").read_bytes()


async def _raw_asgi_request(
    app,
    *,
    method: str = "POST",
    path: str,
    body: bytes,
    declared_length: bytes | None,
) -> tuple[int, bytes]:
    headers = [(b"content-type", b"application/json")]
    if declared_length is None:
        headers.append((b"transfer-encoding", b"chunked"))
    else:
        headers.append((b"content-length", declared_length))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("198.51.100.20", 50000),
        "server": ("bot.example", 443),
    }
    chunks = [body[offset : offset + 4096] for offset in range(0, len(body), 4096)]
    if not chunks:
        chunks = [b""]
    cursor = 0
    sent: list[dict] = []

    async def receive():
        nonlocal cursor
        if cursor >= len(chunks):
            return {"type": "http.disconnect"}
        chunk = chunks[cursor]
        cursor += 1
        return {
            "type": "http.request",
            "body": chunk,
            "more_body": cursor < len(chunks),
        }

    async def send(message):
        sent.append(message)

    await app(scope, receive, send)
    start = next(message for message in sent if message["type"] == "http.response.start")
    payload = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start["status"], payload


@pytest.mark.parametrize(
    "declared_length",
    [None, b"1"],
    ids=["chunked-without-content-length", "forged-small-content-length"],
)
def test_auth_json_body_limit_counts_delivered_bytes(
    tmp_path, monkeypatch, declared_length
):
    app = _hardened_app(tmp_path, monkeypatch)
    body = json.dumps(
        {
            "username": "x" * (70 * 1024),
            "password": "password12",
            "captcha_id": "skip",
            "captcha_answer": "skip",
        }
    ).encode("utf-8")

    status, payload = asyncio.run(
        _raw_asgi_request(
            app,
            path="/api/auth/login",
            body=body,
            declared_length=declared_length,
        )
    )

    assert status == 413
    assert json.loads(payload)["detail"]["code"] == "auth_body_too_large"


def test_auth_json_body_limit_also_precedes_put_profile_parsing(
    tmp_path, monkeypatch
):
    app = _hardened_app(tmp_path, monkeypatch)
    body = json.dumps({"bio": "x" * (70 * 1024)}).encode("utf-8")

    status, payload = asyncio.run(
        _raw_asgi_request(
            app,
            method="PUT",
            path="/api/auth/profile",
            body=body,
            declared_length=b"1",
        )
    )

    assert status == 413
    assert json.loads(payload)["detail"]["code"] == "auth_body_too_large"


def test_auth_json_body_limit_early_rejects_honest_declared_size(
    tmp_path, monkeypatch
):
    app = _hardened_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.post(
            "/api/auth/login",
            content=b"{" + (b"x" * (70 * 1024)) + b"}",
            headers={"Content-Type": "application/json"},
        )

    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "auth_body_too_large"


@pytest.mark.parametrize(
    "declared_length",
    [None, b"1"],
    ids=["missing-content-length", "inaccurate-content-length"],
)
def test_anonymous_feedback_json_has_preparser_body_ceiling(
    tmp_path, monkeypatch, declared_length
):
    app = _hardened_app(tmp_path, monkeypatch)
    body = json.dumps(
        {
            "category": "other",
            "impact": "minor",
            "title": "bounded",
            "body": "x" * (1024 * 1024 + 1),
        }
    ).encode("utf-8")

    status, payload = asyncio.run(
        _raw_asgi_request(
            app,
            path="/api/feedback/bugs",
            body=body,
            declared_length=declared_length,
        )
    )

    assert status == 413
    assert json.loads(payload)["detail"]["code"] == "api_body_too_large"


def _string_schema_nodes(node: dict):
    if node.get("type") == "string":
        yield node
    for option in node.get("anyOf", []):
        yield from _string_schema_nodes(option)


def test_every_auth_request_string_field_has_a_character_ceiling():
    models = (
        RegisterReq,
        LoginReq,
        ChangePasswordReq,
        ProfileUpdateReq,
        RequestResetReq,
        ResetPasswordReq,
        VerifyEmailReq,
        ResendVerifyReq,
    )
    missing: list[str] = []
    for model in models:
        for field_name, schema in model.model_json_schema()["properties"].items():
            for string_schema in _string_schema_nodes(schema):
                if "maxLength" not in string_schema:
                    missing.append(f"{model.__name__}.{field_name}")
    assert missing == []


def _admin_role_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _hardened_app(tmp_path, monkeypatch)
    admin = app.state.auth.register(
        "csrfadmin", "csrfadmin@example.com", "password12"
    )
    victim = app.state.auth.register(
        "csrfvictim", "csrfvictim@example.com", "password12"
    )
    app.state.store.update_user(admin["id"], role="admin", email_verified=1)
    app.state.store.update_user(victim["id"], email_verified=1)
    _, token = app.state.auth.authenticate("csrfadmin", "password12")
    return app, victim["id"], token


@pytest.mark.parametrize(
    "origin_headers",
    [
        {},
        {"Origin": "null"},
        {"Origin": "https://evil.bot.example"},
        {"Origin": "https://attacker.example"},
    ],
    ids=["missing", "null", "hostile-sibling", "cross-site"],
)
def test_cookie_authenticated_mutation_rejects_untrusted_origin(
    tmp_path, monkeypatch, origin_headers
):
    app, victim_id, token = _admin_role_context(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, token)
        response = client.post(
            f"/api/admin/users/{victim_id}/role?role=admin",
            headers=origin_headers,
        )

    assert response.status_code == 403
    assert response.json()["code"] == "csrf_origin_invalid"
    assert app.state.store.get_user(victim_id)["role"] == "user"


def test_cookie_authenticated_mutation_accepts_exact_public_origin(
    tmp_path, monkeypatch
):
    app, victim_id, token = _admin_role_context(tmp_path, monkeypatch)
    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, token)
        response = client.post(
            f"/api/admin/users/{victim_id}/role?role=organizer",
            headers={"Origin": "https://bot.example"},
        )

    assert response.status_code == 200
    assert app.state.store.get_user(victim_id)["role"] == "organizer"


def test_bearer_authenticated_mutation_does_not_require_browser_origin(
    tmp_path, monkeypatch
):
    app, victim_id, token = _admin_role_context(tmp_path, monkeypatch)
    with TestClient(app) as client:
        # Even if a cookie happens to be present, dependencies give Bearer
        # precedence, so the non-browser API contract remains origin-agnostic.
        client.cookies.set(COOKIE_NAME, token)
        response = client.post(
            f"/api/admin/users/{victim_id}/role?role=organizer",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert app.state.store.get_user(victim_id)["role"] == "organizer"


def test_bot_version_upload_ids_share_one_strict_rate_limit_bucket(monkeypatch):
    monkeypatch.setattr(security_module, "_UPLOAD_STRICT", (1, 60))
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, enabled=True)

    @app.post("/api/bots/{bot_id}/versions")
    def upload_version(bot_id: str):
        return {"bot_id": bot_id}

    with TestClient(app) as client:
        first = client.post("/api/bots/bot_owned/versions")
        rotated = client.post("/api/bots/bot_not_owned/versions")

    assert first.status_code == 200
    assert rotated.status_code == 429


def test_feedback_attachment_ids_share_one_strict_rate_limit_bucket(monkeypatch):
    monkeypatch.setattr(security_module, "_FEEDBACK_STRICT", (1, 60))
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, enabled=True)

    @app.post("/api/feedback/bugs/{bug_id}/attachments")
    def upload_attachment(bug_id: str):
        return {"bug_id": bug_id}

    with TestClient(app) as client:
        first = client.post("/api/feedback/bugs/bug_owned/attachments")
        rotated = client.post("/api/feedback/bugs/bug_other/attachments")

    assert first.status_code == 200
    assert rotated.status_code == 429


def test_api_global_rate_budget_is_shared_across_paths_and_isolates_ips(
    monkeypatch,
):
    monkeypatch.setenv("BZ_TRUST_PROXY", "0")
    monkeypatch.setattr(security_module, "_API_GLOBAL_PER_IP", (2, 60))
    monkeypatch.setattr(security_module, "_API_DEFAULT", (100, 60))
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, enabled=True)

    @app.get("/api/probe/{value}")
    def probe(value: str):
        return {"value": value}

    with TestClient(app, client=("198.51.100.10", 50000)) as first_ip:
        assert first_ip.get("/api/probe/one").status_code == 200
        assert first_ip.get("/api/probe/two").status_code == 200
        limited = first_ip.get("/api/probe/three")
    with TestClient(app, client=("198.51.100.11", 50000)) as second_ip:
        isolated = second_ip.get("/api/probe/four")

    assert limited.status_code == 429
    assert limited.headers["X-RateLimit-Limit"] == "2"
    assert isolated.status_code == 200


def test_strict_route_budget_remains_effective_with_global_budget(monkeypatch):
    monkeypatch.setenv("BZ_TRUST_PROXY", "0")
    monkeypatch.setattr(security_module, "_API_GLOBAL_PER_IP", (10, 60))
    monkeypatch.setattr(security_module, "_AUTH_STRICT", (1, 60))
    app = FastAPI()
    app.add_middleware(RateLimitMiddleware, enabled=True)

    @app.post("/api/auth/login")
    def login_probe():
        return {"ok": True}

    @app.post("/api/probe")
    def other_probe():
        return {"ok": True}

    with TestClient(app) as client:
        first = client.post("/api/auth/login")
        strict_limited = client.post("/api/auth/login")
        other_path = client.post("/api/probe")

    assert first.status_code == 200
    assert first.headers["X-RateLimit-Limit"] == "1"
    assert strict_limited.status_code == 429
    assert strict_limited.headers["X-RateLimit-Limit"] == "1"
    assert other_path.status_code == 200


def test_rate_limiter_bucket_table_fails_closed_at_fixed_capacity():
    limiter = security_module.InMemoryRateLimiter(max_buckets=2)

    assert limiter.check("one", 10, 60)[0] is True
    assert limiter.check("two", 10, 60)[0] is True
    assert limiter.check("three", 10, 60)[0] is False
    assert len(limiter._hits) == 2


class _CapturedLogger:
    def __init__(self):
        self.messages: list[str] = []

    def info(self, message, *args):
        self.messages.append(message % args if args else str(message))

    def log(self, _level, message, *args):
        self.messages.append(message % args if args else str(message))


def _request_with_path(path: str, *, query_string: bytes = b"") -> Request:
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": path,
            "raw_path": path.encode("utf-8", errors="surrogatepass"),
            "query_string": query_string,
            "headers": [(b"host", b"bot.example")],
            "client": ("198.51.100.30", 50000),
            "server": ("bot.example", 443),
            "app": FastAPI(),
        }
    )


def test_access_log_fields_remain_single_line_bounded_and_query_free(monkeypatch):
    captured = _CapturedLogger()
    monkeypatch.setattr(security_module, "_access_logger", captured)

    async def downstream(_scope, _receive, _send):
        raise AssertionError("middleware dispatch test does not call ASGI app")

    middleware = security_module.AccessLogMiddleware(
        downstream,
        trust_proxy=False,
    )
    request = type(
        "AccessRequest",
        (),
        {
            "url": type(
                "URL",
                (),
                {
                    "path": "/api/中文\nrecord-" + ("x" * 1500),
                    "query": "token=must-not-be-logged",
                },
            )(),
            "method": "GET",
            "headers": {},
            "client": type("Client", (), {"host": "198.51.100.30"})(),
        },
    )

    async def call_next(_request):
        return JSONResponse({"ok": True})

    response = asyncio.run(middleware.dispatch(request, call_next))

    assert response.status_code == 200
    assert len(captured.messages) == 1
    record = captured.messages[0]
    assert len(record.splitlines()) == 1
    assert "中文" in record
    assert "\\\\n" in record
    assert "must-not-be-logged" not in record
    assert len(record) < 1200


def test_audit_log_fields_remain_single_line_and_bounded(monkeypatch):
    captured = _CapturedLogger()
    monkeypatch.setattr(security_module, "_audit_logger", captured)
    request = _request_with_path("/api/audit")

    security_module.audit_log(
        request,
        "security_event",
        result="fail",
        user="中文用户\r\nrecord",
        target="target-" + ("x" * 2000),
        detail='detail\nwith\tcontrols and "quotes"' + ("y" * 2000),
        trust_proxy=False,
    )

    assert len(captured.messages) == 1
    record = captured.messages[0]
    assert len(record.splitlines()) == 1
    assert "中文用户" in record
    assert "\\\\r\\\\n" in record
    assert "\\\\t" in record
    assert len(record) < 2300


def _assert_credentialed_no_store(response):
    assert response.headers["Cache-Control"] == "private, no-store, max-age=0"
    assert response.headers["Pragma"] == "no-cache"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    vary = {
        token.strip().lower()
        for token in response.headers["Vary"].split(",")
    }
    assert {"authorization", "cookie"}.issubset(vary)


def _private_read_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _hardened_app(tmp_path, monkeypatch)
    user = app.state.auth.register(
        "cacheuser", "cacheuser@example.com", "password12"
    )
    app.state.store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate("cacheuser", "password12")
    return app, token


def test_credentialed_private_gets_are_never_cacheable(tmp_path, monkeypatch):
    app, token = _private_read_context(tmp_path, monkeypatch)
    with TestClient(app) as client:
        bearer = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )
        client.cookies.set(COOKIE_NAME, token)
        cookie = client.get("/api/auth/me")

    assert bearer.status_code == 200
    assert cookie.status_code == 200
    _assert_credentialed_no_store(bearer)
    _assert_credentialed_no_store(cookie)


def test_credentialed_api_error_responses_are_never_cacheable(
    tmp_path, monkeypatch
):
    app, token = _private_read_context(tmp_path, monkeypatch)
    with TestClient(app) as client:
        unauthorized = client.get(
            "/api/auth/me",
            headers={"Authorization": "Bearer invalid-session"},
        )
        forbidden = client.get(
            "/api/admin/stats",
            headers={"Authorization": f"Bearer {token}"},
        )
        missing = client.get(
            "/api/private-route-that-does-not-exist",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert unauthorized.status_code == 401
    assert forbidden.status_code == 403
    assert missing.status_code == 404
    for response in (unauthorized, forbidden, missing):
        _assert_credentialed_no_store(response)


def test_public_api_response_without_credentials_keeps_public_cache_semantics(
    tmp_path, monkeypatch
):
    app = _hardened_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.get("/api/health")

    assert response.status_code == 200
    assert "Cache-Control" not in response.headers
    assert "Pragma" not in response.headers
    assert "Vary" not in response.headers
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def test_credentialed_cache_vary_merges_existing_dimensions():
    response = Response(headers={"Vary": "Origin, authorization"})

    security_module.CredentialedAPINoStoreMiddleware._merge_vary(response)

    assert response.headers["Vary"] == "Origin, authorization, Cookie"


def test_login_credential_responses_are_never_cacheable(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = _hardened_app(tmp_path, monkeypatch)
    user = app.state.auth.register(
        "loginprivate", "loginprivate@example.com", "password12"
    )
    app.state.store.update_user(user["id"], email_verified=1)
    payload = {
        "username": "loginprivate",
        "password": "password12",
        "captcha_id": "skip",
        "captcha_answer": "skip",
    }

    with TestClient(app) as client:
        success = client.post("/api/auth/login", json=payload)
        client.cookies.clear()
        failure = client.post(
            "/api/auth/login",
            json={**payload, "password": "wrongpass1"},
        )

    assert success.status_code == 200
    assert success.json()["token"]
    assert COOKIE_NAME in success.headers["Set-Cookie"]
    assert failure.status_code == 401
    _assert_credentialed_no_store(success)
    _assert_credentialed_no_store(failure)


def test_captcha_challenge_response_is_never_cacheable(tmp_path, monkeypatch):
    app = _hardened_app(tmp_path, monkeypatch)

    with TestClient(app) as client:
        response = client.get("/api/auth/captcha")

    assert response.status_code == 200
    assert response.json()["captcha_id"]
    _assert_credentialed_no_store(response)
