"""Auth / Captcha 单测。"""
from __future__ import annotations

import pytest

from bzplat.backend.auth.auth_manager import AuthError, AuthManager
from bzplat.backend.auth.captcha import CaptchaStore, png_to_data_url
from bzplat.backend.store import Store
from bzplat.backend.store.schema import CODE_RESET, CODE_VERIFY


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
    assert len(mailer.sent) == 1
    code_row = auth.store.get_latest_email_code(user["id"], CODE_VERIFY)
    verified = auth.verify_email("alice", code_row["code"])
    assert verified["email_verified"] == 1

    safe, token = auth.authenticate("alice", "password12")
    assert safe["username"] == "alice"
    assert token
    assert auth.verify_session(token)["id"] == user["id"]
    auth.logout(token)
    assert auth.verify_session(token) is None


def test_mailer_none_rejects_send(tmp_path):
    auth, _ = _auth(tmp_path, mailer=False)
    user = auth.register("bob", "bob@ex.com", "password12")
    with pytest.raises(AuthError) as ei:
        auth.send_verify_code(user)
    assert ei.value.code == "mail_not_configured"
    # 验证码仍入库，管理员可手工查库或配置 SMTP 后重发
    code_row = auth.store.get_latest_email_code(user["id"], CODE_VERIFY)
    assert code_row is not None
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


def test_register_route_rolls_back_user_when_verify_mail_fails(tmp_path, monkeypatch):
    """首封验证邮件失败时，HTTP 注册不能留下占用用户名的半成品账号。"""
    from fastapi.testclient import TestClient
    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = create_app(db_path=str(tmp_path / "register-atomic.db"))
    app.state.auth.mailer = None
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
    assert first.status_code == 503, first.text
    assert app.state.store.get_user_by_username("atomicuser") is None

    # 重试仍进入发信路径（503），而非用户名/邮箱已占用（409）。
    second = client.post("/api/auth/register", json=payload)
    assert second.status_code == 503, second.text
    assert app.state.store.get_user_by_username("atomicuser") is None


def test_change_password(tmp_path):
    auth, _ = _auth(tmp_path, mailer=False)
    user = auth.register("dave", "d@ex.com", "password12")
    auth.store.update_user(user["id"], email_verified=1)
    _, token = auth.authenticate("dave", "password12")
    auth.change_password(user["id"], "password12", "newpass123")
    assert auth.verify_session(token) is None
    _, token2 = auth.authenticate("dave", "newpass123")
    assert token2


def test_request_reset_and_reset_password(tmp_path):
    auth, mailer = _auth(tmp_path)
    user = auth.register("erin", "e@ex.com", "password12")
    auth.store.update_user(user["id"], email_verified=1)
    _, old_session = auth.authenticate("erin", "password12")
    ok, _ = auth.request_reset("e@ex.com")
    assert ok
    assert any("重置" in m["subject"] or "reset" in m["subject"].lower() for m in mailer.sent)
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


def test_admin_reset_token_updates_password_consumes_token_and_revokes_sessions(
    tmp_path,
):
    auth, _ = _auth(tmp_path, mailer=False)
    user = auth.register("tokenuser", "token@example.com", "password12")
    auth.store.update_user(user["id"], email_verified=1)
    _, old_session = auth.authenticate("tokenuser", "password12")
    reset_token, _ = auth.admin_create_reset_token("token@example.com")

    reset_user = auth.reset_password_by_token(reset_token, "tokenpass1")

    assert reset_user["id"] == user["id"]
    assert auth.store.get_password_reset(reset_token) is None
    assert auth.verify_session(old_session) is None
    _, new_session = auth.authenticate("tokenuser", "tokenpass1")
    assert new_session
    with pytest.raises(AuthError) as reused:
        auth.reset_password_by_token(reset_token, "anotherpass1")
    assert reused.value.code == "invalid_reset_token"


def test_expired_reset_credentials_report_expired_without_consuming(tmp_path):
    auth, _ = _auth(tmp_path, mailer=False)
    user = auth.register("expiredauth", "expiredauth@example.com", "password12")
    original_hash = auth.store.get_user(user["id"])["password_hash"]
    auth.store.add_email_code(
        user["id"], CODE_RESET, "222222", "2000-01-01T00:00:00"
    )
    auth.store.add_password_reset(
        "expired-auth-token", user["id"], "2000-01-01T00:00:00"
    )

    with pytest.raises(AuthError) as code_error:
        auth.reset_password("expiredauth", "222222", "newpass123")
    assert code_error.value.code == "expired_code"
    assert auth.store.get_latest_email_code(user["id"], CODE_RESET) is not None

    with pytest.raises(AuthError) as token_error:
        auth.reset_password_by_token("expired-auth-token", "newpass123")
    assert token_error.value.code == "expired_reset_token"
    assert auth.store.get_password_reset("expired-auth-token") is not None
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
