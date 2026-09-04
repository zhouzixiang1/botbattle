"""Auth / Captcha 单测。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from bzplat.backend.auth.auth_manager import AuthError, AuthManager
from bzplat.backend.auth.captcha import CaptchaStore, png_to_data_url
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    CODE_RESET,
    CODE_VERIFY,
    EMAIL_CODE_MAX_FAILED_ATTEMPTS,
)


class RecordingMailer:
    """不连 SMTP，记录 send 调用。"""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.config = type("Cfg", (), {"code_ttl_minutes": 30})()

    def send(
        self,
        to_addr: str,
        subject: str,
        *,
        body_text: str = "",
        body_html: str = "",
    ) -> None:
        self.sent.append(
            {
                "to": to_addr,
                "subject": subject,
                "body_text": body_text,
                "body_html": body_html,
            }
        )


def _auth(tmp_path, mailer=None) -> tuple[AuthManager, RecordingMailer | None]:
    store = Store(str(tmp_path / "auth.db"))
    if mailer is False:
        return AuthManager(store, mailer=None), None
    m = mailer or RecordingMailer()
    return AuthManager(store, mailer=m), m


def test_register_authenticate_logout(tmp_path):
    auth, mailer = _auth(tmp_path)
    user = auth.register("alice", "alice@ex.com", "password12", display_name="A")
    assert user["username"] == "alice"
    assert "password_hash" not in user
    assert user["email_verified"] == 0

    with pytest.raises(AuthError) as ei:
        auth.authenticate("alice", "password12")
    assert ei.value.code == "email_unverified"

    auth.send_verify_code(user)
    queued = auth.store._conn.execute(
        "SELECT template_key,status,payload_json FROM deliveries ORDER BY id"
    ).fetchall()
    assert [(row["template_key"], row["status"]) for row in queued] == [
        ("verify_email", "queued")
    ]
    code_row = auth.store.get_latest_email_code(user["id"], CODE_VERIFY)
    assert code_row["code"] not in queued[0]["payload_json"]
    verified = auth.verify_email("alice", code_row["code"])
    assert verified["email_verified"] == 1
    assert [
        row["template_key"]
        for row in auth.store._conn.execute(
            "SELECT template_key FROM deliveries ORDER BY id"
        )
    ] == ["verify_email", "welcome"]

    safe, token = auth.authenticate("alice", "password12")
    assert safe["username"] == "alice"
    assert token
    assert auth.verify_session(token)["id"] == user["id"]
    auth.logout(token)
    assert auth.verify_session(token) is None


def test_mailer_none_still_queues_without_blocking_request(tmp_path):
    auth, _ = _auth(tmp_path, mailer=False)
    user = auth.register("bob", "bob@ex.com", "password12")
    auth.send_verify_code(user)
    # 验证码与高优先级 delivery 均入库；SMTP 配置/重试属于 worker。
    code_row = auth.store.get_latest_email_code(user["id"], CODE_VERIFY)
    assert code_row is not None
    delivery = auth.store._conn.execute(
        "SELECT status,priority,payload_json FROM deliveries"
    ).fetchone()
    assert delivery["status"] == "queued"
    assert delivery["priority"] == 100
    assert code_row["code"] not in delivery["payload_json"]
    auth.verify_email("bob@ex.com", code_row["code"])
    safe, _token = auth.authenticate("bob", "password12")
    assert safe["email_verified"] == 1


def test_wrong_password_and_duplicate(tmp_path):
    auth, _ = _auth(tmp_path)
    auth.register("carol", "c@ex.com", "password12")
    with pytest.raises(AuthError) as ei:
        auth.register("carol", "other@ex.com", "password12")
    assert ei.value.code == "username_taken"
    with pytest.raises(AuthError) as ei:
        auth.register("carol2", "c@ex.com", "password12")
    assert ei.value.code == "email_taken"

    # verify then wrong pw
    u = auth.store.get_user_by_username("carol")
    auth.store.update_user(u["id"], email_verified=1)
    with pytest.raises(AuthError) as ei:
        auth.authenticate("carol", "wrongpass1")
    assert ei.value.code == "invalid_credentials"


def test_register_rejects_invalid_phone(tmp_path):
    auth, _ = _auth(tmp_path)
    for index, invalid in enumerate(("abc", "13١٢٣٤٥٦٧٨٩", "13１２３４５６７８９")):
        username = f"phoneuser{index}"
        with pytest.raises(AuthError) as ei:
            auth.register(
                username, f"phone{index}@ex.com", "password12", phone=invalid
            )
        assert ei.value.code == "invalid_phone"
        assert auth.store.get_user_by_username(username) is None

    user = auth.register(
        "phoneok", "phoneok@ex.com", "password12", phone=" 13800138000 "
    )
    assert auth.store.get_user(user["id"])["phone"] == "13800138000"


def test_register_route_queues_mail_even_when_smtp_is_unconfigured(tmp_path, monkeypatch):
    """SMTP 不属于注册事务；用户和验证码不能因 provider 不可用而回滚。"""
    from fastapi.testclient import TestClient
    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = create_app(db_path=str(tmp_path / "register-atomic.db"))
    client = TestClient(app)
    payload = {
        "username": "atomicuser",
        "email": "atomic@example.com",
        "password": "password12",
        "phone": "13800138000",
        "captcha_id": "skip",
        "captcha_answer": "skip",
    }
    first = client.post("/api/auth/register", json=payload)
    assert first.status_code == 200, first.text
    assert first.json()["delivery_status"] == "queued"
    assert app.state.store.get_user_by_username("atomicuser") is not None

    # 重试按正常用户名唯一约束拒绝，而不是制造第二个用户。
    second = client.post("/api/auth/register", json=payload)
    assert second.status_code == 409, second.text


def test_change_password(tmp_path):
    auth, _ = _auth(tmp_path, mailer=False)
    user = auth.register("dave", "d@ex.com", "password12")
    auth.store.update_user(user["id"], email_verified=1)
    _, token = auth.authenticate("dave", "password12")
    auth.change_password(user["id"], "password12", "newpass123")
    assert auth.verify_session(token) is None
    _, token2 = auth.authenticate("dave", "newpass123")
    assert token2


@pytest.mark.parametrize("mutation", ["change", "reset"])
def test_stale_password_authentication_cannot_issue_session_after_password_rotation(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
):
    """Password rotation linearizes against an already-verified old login.

    A login may finish the expensive password check before a concurrent change
    or reset commits.  Its later session insert must still compare the exact
    credential generation it verified, otherwise an old password can create a
    fresh bearer after the revocation transaction has deleted existing tokens.
    """
    db_path = tmp_path / f"password-session-race-{mutation}.db"
    login_auth = AuthManager(Store(str(db_path)), mailer=None)
    user = login_auth.register(
        f"race_{mutation}",
        f"race-{mutation}@example.test",
        "password12",
    )
    login_auth.store.update_user(user["id"], email_verified=1)
    rotating_auth = AuthManager(Store(str(db_path)), mailer=None)
    password_verified = threading.Event()
    rotation_committed = threading.Event()
    original_issue = login_auth.store.add_session_if_user_active

    def delayed_issue(*args, **kwargs):
        password_verified.set()
        assert rotation_committed.wait(timeout=10)
        return original_issue(*args, **kwargs)

    monkeypatch.setattr(
        login_auth.store,
        "add_session_if_user_active",
        delayed_issue,
    )
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            stale_login = pool.submit(
                login_auth.authenticate,
                user["username"],
                "password12",
            )
            assert password_verified.wait(timeout=10)
            if mutation == "change":
                rotating_auth.change_password(
                    user["id"],
                    "password12",
                    "replacement12",
                )
            else:
                assert rotating_auth.request_reset(user["username"])[0]
                code = rotating_auth.store.get_latest_email_code(
                    user["id"], CODE_RESET
                )["code"]
                rotating_auth.reset_password(
                    user["username"],
                    code,
                    "replacement12",
                )
            rotation_committed.set()
            with pytest.raises(AuthError) as rejected:
                stale_login.result(timeout=10)
            assert rejected.value.code == "invalid_credentials"

        with pytest.raises(AuthError) as old_password:
            rotating_auth.authenticate(user["username"], "password12")
        assert old_password.value.code == "invalid_credentials"
        _safe, fresh_token = rotating_auth.authenticate(
            user["username"], "replacement12"
        )
        assert rotating_auth.verify_session(fresh_token)["id"] == user["id"]
    finally:
        rotation_committed.set()
        rotating_auth.store.close()
        login_auth.store.close()


def test_disabled_session_does_not_revive_after_reenable(tmp_path):
    auth, _ = _auth(tmp_path, mailer=False)
    user = auth.register("dormant", "dormant@ex.com", "password12")
    auth.store.update_user(user["id"], email_verified=1)
    _, token = auth.authenticate("dormant", "password12")

    auth.store.update_user(user["id"], is_active=0)
    auth.store.update_user(user["id"], is_active=1)

    assert auth.verify_session(token) is None


def test_request_reset_and_reset_password(tmp_path):
    auth, mailer = _auth(tmp_path)
    user = auth.register("erin", "e@ex.com", "password12")
    auth.store.update_user(user["id"], email_verified=1)
    _, old_session = auth.authenticate("erin", "password12")
    ok, _ = auth.request_reset("e@ex.com")
    assert ok
    assert auth.store._conn.execute(
        "SELECT COUNT(*) FROM deliveries WHERE template_key='reset_password'"
    ).fetchone()[0] == 1
    code = auth.store.get_latest_email_code(user["id"], CODE_RESET)["code"]
    auth.reset_password("erin", code, "brandnew1")
    assert auth.verify_session(old_session) is None
    _, token = auth.authenticate("erin", "brandnew1")
    assert token
    with pytest.raises(AuthError) as reused:
        auth.reset_password("erin", code, "anotherpass1")
    assert reused.value.code == "invalid_code"

    # 不存在用户不抛
    ok2, empty = auth.request_reset("nobody@ex.com")
    assert ok2 is False
    assert empty == {}


def test_auth_reset_wrong_code_exhausts_durable_credential(tmp_path):
    auth, _ = _auth(tmp_path, mailer=False)
    user = auth.register("resetlimit", "resetlimit@ex.com", "password12")
    auth.request_reset(user["username"])
    row = auth.store.get_latest_email_code(user["id"], CODE_RESET)
    wrong = "000000" if row["code"] != "000000" else "000001"

    for _ in range(EMAIL_CODE_MAX_FAILED_ATTEMPTS):
        with pytest.raises(AuthError) as rejected:
            auth.reset_password(user["username"], wrong, "attackerpass1")
        assert rejected.value.code == "invalid_code"

    exhausted = auth.store._conn.execute(
        "SELECT failed_attempts,used_at FROM email_codes WHERE id=?", (row["id"],)
    ).fetchone()
    assert exhausted["failed_attempts"] == EMAIL_CODE_MAX_FAILED_ATTEMPTS
    assert exhausted["used_at"] is not None
    with pytest.raises(AuthError) as correct_after_exhaustion:
        auth.reset_password(user["username"], row["code"], "attackerpass1")
    assert correct_after_exhaustion.value.code == "invalid_code"


def test_admin_reset_credential_endpoint_is_removed(tmp_path, monkeypatch):
    """管理员不能从浏览器取得可改密 credential，旧入口须稳定为 404。"""
    from fastapi.testclient import TestClient
    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = create_app(db_path=str(tmp_path / "removed-admin-reset.db"))
    admin = app.state.auth.register(
        "resetadmin", "resetadmin@example.com", "password12"
    )
    app.state.store.update_user(admin["id"], role="admin", email_verified=1)
    _, admin_session = app.state.auth.authenticate("resetadmin", "password12")
    deliveries_before = app.state.store._conn.execute(
        "SELECT COUNT(*) FROM deliveries"
    ).fetchone()[0]

    with TestClient(app) as client:
        response = client.post(
            "/api/auth/admin/create-reset-token",
            json={"username_or_email": "resetadmin"},
            headers={"Authorization": f"Bearer {admin_session}"},
        )

    assert response.status_code == 404
    assert all(
        getattr(route, "path", None) != "/api/auth/admin/create-reset-token"
        for route in app.routes
    )
    assert not hasattr(app.state.auth, "admin_create_reset_token")
    assert not hasattr(app.state.auth, "reset_password_by_token")
    assert app.state.store._conn.execute(
        "SELECT COUNT(*) FROM password_resets"
    ).fetchone()[0] == 0
    assert app.state.store._conn.execute(
        "SELECT COUNT(*) FROM deliveries"
    ).fetchone()[0] == deliveries_before


def test_expired_reset_email_code_reports_expired_without_consuming(tmp_path):
    auth, _ = _auth(tmp_path, mailer=False)
    user = auth.register("expiredauth", "expiredauth@example.com", "password12")
    original_hash = auth.store.get_user(user["id"])["password_hash"]
    auth.store.add_email_code(
        user["id"], CODE_RESET, "222222", "2000-01-01T00:00:00"
    )

    with pytest.raises(AuthError) as code_error:
        auth.reset_password("expiredauth", "222222", "newpass123")
    assert code_error.value.code == "expired_code"
    assert auth.store.get_latest_email_code(user["id"], CODE_RESET) is not None
    assert auth.store.get_user(user["id"])["password_hash"] == original_hash


def test_captcha_create_verify_once(tmp_path):
    cs = CaptchaStore(ttl_sec=300)
    cid, answer, png = cs.create()
    assert cid and answer and png.startswith(b"\x89PNG")
    assert png_to_data_url(png).startswith("data:image/png;base64,")
    assert cs.verify(cid, answer) is True
    # one-time
    assert cs.verify(cid, answer) is False

    cid2, answer2, _ = cs.create()
    assert cs.verify(cid2, answer2.upper() if answer2.isalpha() else answer2) is True


def test_captcha_wrong_answer(tmp_path):
    cs = CaptchaStore()
    cid, _answer, _ = cs.create()
    assert cs.verify(cid, "definitely-wrong-xxx") is False


# ---------- BZ_SKIP_CAPTCHA 开关：HTTP 级端到端守护 ----------


def _make_test_app(tmp_path, monkeypatch, *, skip_captcha: bool):
    """起一个临时 app + 已验证用户，返回 (client, username, password)。"""
    from fastapi.testclient import TestClient

    from bzplat.backend.crypto import hash_password
    from bzplat.backend.main import create_app

    db_path = str(tmp_path / "skipcap.db")
    monkeypatch.setenv("BZ_DB_PATH", db_path)
    if skip_captcha:
        monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    else:
        monkeypatch.delenv("BZ_SKIP_CAPTCHA", raising=False)

    store = Store(db_path)
    u = store.create_user("skipuser", "skip@ex.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    store.close()

    app = create_app()
    return TestClient(app), "skipuser", "pw123456"


def test_skip_captcha_allows_login_with_any_answer(tmp_path, monkeypatch):
    """BZ_SKIP_CAPTCHA=1 时，登录可跳过验证码校验（任意/空 captcha 均可）。"""
    client, username, password = _make_test_app(tmp_path, monkeypatch, skip_captcha=True)
    # 完全不用取验证码，captcha_id/answer 传占位值
    r = client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": password,
            "captcha_id": "skipped",
            "captcha_answer": "anything",
        },
    )
    assert r.status_code == 200, r.text
    assert r.json().get("token")
    session_cookie = r.headers.get("set-cookie", "")
    assert session_cookie.startswith("bz_session=")
    assert "HttpOnly" in session_cookie
    assert "SameSite=lax" in session_cookie


def test_lan_http_client_can_use_bearer_rest_while_cookie_stays_secure(
    tmp_path,
    monkeypatch,
):
    """LAN HTTP REST uses the explicit bearer; it must not weaken the session cookie."""
    from fastapi.testclient import TestClient

    from bzplat.backend.crypto import hash_password
    from bzplat.backend.main import create_app

    db_path = str(tmp_path / "lan-bearer.db")
    monkeypatch.setenv("BZ_DB_PATH", db_path)
    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    monkeypatch.setenv("BZ_SECURE_COOKIE", "1")
    monkeypatch.setenv("BZ_TRUST_PROXY", "1")

    store = Store(db_path)
    user = store.create_user(
        "lanuser",
        "lan@example.com",
        hash_password("pw123456"),
    )
    store.update_user(user["id"], email_verified=1)
    store.close()

    app = create_app()
    with TestClient(
        app,
        base_url="http://192.168.1.13:50380",
        client=("192.168.1.42", 50000),
    ) as client:
        login = client.post(
            "/api/auth/login",
            json={
                "username": "lanuser",
                "password": "pw123456",
                "captcha_id": "skipped",
                "captcha_answer": "skipped",
            },
        )
        assert login.status_code == 200, login.text
        assert "Secure" in login.headers["set-cookie"]
        token = login.json()["token"]

        client.cookies.clear()
        anonymous = client.get("/api/auth/me")
        bearer = client.get(
            "/api/auth/me",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert anonymous.status_code == 401
    assert bearer.status_code == 200
    assert bearer.json()["user"]["username"] == "lanuser"


def test_skip_captcha_off_still_validates(tmp_path, monkeypatch):
    """默认（BZ_SKIP_CAPTCHA 未开）时，错误验证码仍被拒——守护开关不误开。"""
    client, username, password = _make_test_app(tmp_path, monkeypatch, skip_captcha=False)
    r = client.post(
        "/api/auth/login",
        json={
            "username": username,
            "password": password,
            "captcha_id": "whatever",
            "captcha_answer": "definitely-wrong-xxx",
        },
    )
    assert r.status_code == 400
    assert "验证码" in r.json().get("detail", "")
