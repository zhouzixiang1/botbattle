"""Privacy and cache boundaries for authentication validation failures."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.auth.auth_manager import COOKIE_NAME
from bzplat.backend.main import create_app


_LOGIN_SECRET = "LOGIN_PASSWORD_SECRET_DO_NOT_ECHO"
_REGISTER_SECRET = "REGISTER_PASSWORD_SECRET_DO_NOT_ECHO"
_RESET_SECRET = "RESET_PASSWORD_SECRET_DO_NOT_ECHO"


def _assert_private_no_store(response) -> None:
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert {
        field.strip().lower()
        for field in response.headers["vary"].split(",")
        if field.strip()
    } == {"authorization", "cookie"}


@pytest.fixture
def auth_context(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setenv("BZ_RATE_LIMIT", "0")
    app = create_app(db_path=str(tmp_path / "auth-validation-privacy.db"))
    with TestClient(app) as client:
        yield app, client


@pytest.mark.parametrize(
    ("path", "payload", "secret"),
    [
        (
            "/api/auth/login",
            {
                "username": "privacy-user",
                "password": _LOGIN_SECRET + "x" * 4096,
                "captcha_id": "captcha",
                "captcha_answer": "answer",
            },
            _LOGIN_SECRET,
        ),
        (
            "/api/auth/register",
            {
                "username": "privacy-register",
                "email": "privacy-register@example.test",
                "password": {"nested_secret": _REGISTER_SECRET},
                "captcha_id": "captcha",
                "captcha_answer": "answer",
            },
            _REGISTER_SECRET,
        ),
        (
            "/api/auth/reset-password",
            {
                "email_or_username": "privacy-user",
                "code": "123456",
                "new_password": _RESET_SECRET + "x" * 4096,
            },
            _RESET_SECRET,
        ),
    ],
    ids=["login-overlong", "register-malformed", "reset-overlong"],
)
def test_auth_validation_errors_never_echo_submitted_values(
    auth_context, path: str, payload: dict, secret: str
):
    _app, client = auth_context

    response = client.post(path, json=payload)

    assert response.status_code == 422
    assert secret not in response.text
    assert len(response.content) < 2048
    _assert_private_no_store(response)
    body = response.json()
    assert set(body) == {"detail"}
    assert isinstance(body["detail"], list)
    assert body["detail"]
    for error in body["detail"]:
        assert set(error) == {"loc", "msg", "type"}
        assert isinstance(error["loc"], list)
        assert isinstance(error["msg"], str)
        assert isinstance(error["type"], str)


def test_auth_namespace_is_private_for_success_and_early_4xx(auth_context):
    _app, client = auth_context

    responses = [
        client.post("/api/auth/logout"),
        client.post(
            "/api/auth/login",
            json={
                "username": "unknown-user",
                "password": "password12",
                "captcha_id": "missing",
                "captcha_answer": "wrong",
            },
        ),
        client.post("/api/auth/login", json={}),
        client.get("/api/auth/does-not-exist"),
        client.post(
            "/api/auth/login",
            content=b"{" + b"x" * (70 * 1024) + b"}",
            headers={"Content-Type": "application/json"},
        ),
    ]

    assert [response.status_code for response in responses] == [
        200,
        400,
        422,
        404,
        413,
    ]
    for response in responses:
        _assert_private_no_store(response)

    public_health = client.get("/api/health")
    assert public_health.status_code == 200
    assert "cache-control" not in public_health.headers


def test_auth_cache_boundary_preserves_bearer_and_cookie_precedence(auth_context):
    app, client = auth_context
    user = app.state.auth.register(
        "privacy_session", "privacy-session@example.test", "password12"
    )
    app.state.store.update_user(int(user["id"]), email_verified=1)
    _authenticated, token = app.state.auth.authenticate(
        "privacy_session", "password12"
    )

    bearer = client.get(
        "/api/auth/me", headers={"Authorization": f"Bearer {token}"}
    )
    assert bearer.status_code == 200
    assert bearer.json()["user"]["id"] == user["id"]
    _assert_private_no_store(bearer)

    client.cookies.set(COOKIE_NAME, token)
    cookie = client.get("/api/auth/me")
    assert cookie.status_code == 200
    assert cookie.json()["user"]["id"] == user["id"]
    _assert_private_no_store(cookie)

    invalid_bearer_with_valid_cookie = client.get(
        "/api/auth/me",
        headers={"Authorization": "Bearer invalid-token"},
    )
    assert invalid_bearer_with_valid_cookie.status_code == 401
    _assert_private_no_store(invalid_bearer_with_valid_cookie)
