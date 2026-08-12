"""Admin user/session responses expose only their documented allowlists."""
from __future__ import annotations

import inspect

import pytest
from fastapi.testclient import TestClient

from bzplat.backend import api_routes
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store
from bzplat.backend.store import db as store_db


USER_FIELDS = {
    "id", "username", "email", "role", "display_name", "is_active",
    "email_verified", "created_at", "last_login_at", "real_name", "phone",
    "school", "student_id",
}
SESSION_FIELDS = {
    "user_id", "username", "expires_at", "created_at", "ip_addr", "user_agent",
}
FORBIDDEN_USER_FIELDS = {
    "password_hash", "password", "token", "reset_token", "email_code",
}


def _app_client(tmp_path):
    app = create_app(db_path=str(tmp_path / "admin-private.db"))
    store = app.state.store
    admin = store.create_user(
        "privacy-admin", "privacy-admin@example.com",
        hash_password("pw123456"), role="admin",
    )
    victim = store.create_user(
        "privacy-user", "privacy-user@example.com", hash_password("pw123456")
    )
    store.update_user(
        admin["id"], email_verified=1,
    )
    store.update_user(
        victim["id"], email_verified=1, display_name="Privacy User",
        real_name="测试用户", phone="13800138000", school="测试学校",
        student_id="S-001",
    )
    _, token = app.state.auth.authenticate("privacy-admin", "pw123456")
    return app, TestClient(app), store, victim, {
        "Authorization": f"Bearer {token}",
    }


def _assert_private_headers(response) -> None:
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    vary = {part.strip().lower() for part in response.headers["vary"].split(",")}
    assert vary == {"authorization", "cookie"}


def _assert_safe_user(user: dict) -> None:
    assert set(user) == USER_FIELDS
    assert not FORBIDDEN_USER_FIELDS.intersection(user)
    assert user["email"] == "privacy-user@example.com"
    assert user["real_name"] == "测试用户"
    assert user["phone"] == "13800138000"
    assert user["school"] == "测试学校"
    assert user["student_id"] == "S-001"


def test_admin_user_list_and_mutations_share_safe_projection(tmp_path):
    _, client, _, victim, headers = _app_client(tmp_path)

    listed = client.get(
        "/api/admin/users?page=1&per_page=50", headers=headers,
    )
    assert listed.status_code == 200, listed.text
    _assert_private_headers(listed)
    listed_user = next(
        user for user in listed.json()["users"] if user["id"] == victim["id"]
    )
    _assert_safe_user(listed_user)

    role = client.post(
        f"/api/admin/users/{victim['id']}/role?role=organizer", headers=headers,
    )
    assert role.status_code == 200, role.text
    _assert_private_headers(role)
    _assert_safe_user(role.json()["user"])
    assert role.json()["user"]["role"] == "organizer"

    patched = client.patch(
        f"/api/admin/users/{victim['id']}",
        headers=headers,
        json={"is_active": False, "email_verified": False},
    )
    assert patched.status_code == 200, patched.text
    _assert_private_headers(patched)
    _assert_safe_user(patched.json()["user"])
    assert patched.json()["user"]["is_active"] == 0
    assert patched.json()["user"]["email_verified"] == 0


def test_admin_sessions_return_metadata_without_bearer_tokens(tmp_path):
    _, client, store, victim, headers = _app_client(tmp_path)
    bearer = "raw-bearer-token-that-must-never-leave-store"
    store.add_session(
        bearer, victim["id"], "2099-01-01T00:00:00",
        ip_addr="192.0.2.4", user_agent="Privacy Browser/1.0",
    )

    response = client.get(
        f"/api/admin/users/{victim['id']}/sessions", headers=headers,
    )
    assert response.status_code == 200, response.text
    _assert_private_headers(response)
    sessions = response.json()["sessions"]
    assert len(sessions) == 1
    assert set(sessions[0]) == SESSION_FIELDS
    assert bearer not in response.text
    assert sessions[0]["username"] == "privacy-user"
    assert sessions[0]["ip_addr"] == "192.0.2.4"
    assert sessions[0]["user_agent"] == "Privacy Browser/1.0"

    revoked = client.delete(
        f"/api/admin/users/{victim['id']}/sessions", headers=headers,
    )
    assert revoked.status_code == 200, revoked.text
    _assert_private_headers(revoked)
    assert revoked.json() == {"ok": True, "revoked": 1}


def test_store_admin_reads_are_positive_column_lists(tmp_path):
    store = Store(str(tmp_path / "store-private.db"))
    user = store.create_user(
        "store-private", "store-private@example.com", "sensitive-hash"
    )
    store.add_session(
        "sensitive-token", user["id"], "2099-01-01T00:00:00",
        ip_addr="198.51.100.8", user_agent="UA",
    )

    listed = next(row for row in store.list_users() if row["id"] == user["id"])
    assert set(listed) == USER_FIELDS
    sessions = store.list_sessions(user["id"])
    assert len(sessions) == 1 and set(sessions[0]) == SESSION_FIELDS
    assert "sensitive-hash" not in repr(listed)
    assert "sensitive-token" not in repr(sessions)


def test_admin_privacy_contract_has_static_allowlist_guards():
    assert set(api_routes._ADMIN_USER_RESPONSE_FIELDS) == USER_FIELDS
    assert not FORBIDDEN_USER_FIELDS.intersection(
        api_routes._ADMIN_USER_RESPONSE_FIELDS
    )
    assert "password_hash" not in store_db._ADMIN_USER_COLUMNS
    assert all("token" not in column for column in store_db._ADMIN_SESSION_COLUMNS)

    list_source = inspect.getsource(store_db.Store.list_users)
    session_source = inspect.getsource(store_db.Store.list_sessions)
    assert "SELECT * FROM users" not in list_source
    assert "SELECT s.*" not in session_source
    assert "s.token" not in session_source


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("post", "/api/admin/users/999999/role?role=user", None),
        ("patch", "/api/admin/users/999999", {"is_active": False}),
    ],
)
def test_missing_admin_user_mutation_does_not_return_store_shape(
    tmp_path, method, path, body,
):
    _, client, _, _, headers = _app_client(tmp_path)
    response = getattr(client, method)(path, headers=headers, json=body)
    assert response.status_code == 404
    assert "password_hash" not in response.text
    _assert_private_headers(response)
