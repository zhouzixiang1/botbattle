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
    ok, _ = auth.request_reset("e@ex.com")
    assert ok
    assert any("重置" in m["subject"] or "reset" in m["subject"].lower() for m in mailer.sent)
    code = auth.store.get_latest_email_code(user["id"], CODE_RESET)["code"]
    auth.reset_password("erin", code, "brandnew1")
    _, token = auth.authenticate("erin", "brandnew1")
    assert token

    # 不存在用户不抛
    ok2, empty = auth.request_reset("nobody@ex.com")
    assert ok2 is False
    assert empty == {}


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
