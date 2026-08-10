"""邮件默认品牌与种子模板回归。"""
from __future__ import annotations

from bzplat.backend.mail import (
    DEFAULT_SENDER_NAME,
    MailConfig,
    TPL_RESET_PASSWORD,
    TPL_VERIFY_EMAIL,
    TPL_WELCOME,
    default_email_templates,
    render_template,
)
from bzplat.backend.store import Store


def _templates_by_key() -> dict[str, tuple[str, str, str]]:
    return {
        key: (subject, body_html, body_text)
        for key, subject, body_html, body_text in default_email_templates()
    }


def test_mail_config_uses_official_sender_name_by_default(monkeypatch):
    monkeypatch.delenv("SMTP_FROM_NAME", raising=False)
    monkeypatch.delenv("SMTP_PORT", raising=False)
    monkeypatch.delenv("EMAIL_CODE_TTL_MINUTES", raising=False)
    assert DEFAULT_SENDER_NAME == "Botbattle"
    assert MailConfig().from_name == "Botbattle"

    monkeypatch.setenv("SMTP_FROM_NAME", "赛事组委会")
    assert MailConfig().from_name == "赛事组委会"


def test_default_templates_use_one_multigame_botbattle_identity():
    templates = _templates_by_key()
    assert set(templates) == {
        TPL_VERIFY_EMAIL,
        TPL_RESET_PASSWORD,
        TPL_WELCOME,
    }

    corpus = "\n".join(part for template in templates.values() for part in template)
    for obsolete in ("bot" + "zone-platform", "load" + "test"):
        assert obsolete.casefold() not in corpus.casefold()

    for subject, body_html, body_text in templates.values():
        assert subject.startswith("【Botbattle】")
        assert "Botbattle" in body_html
        assert "Botbattle" in body_text
        assert "{{username}}" in body_html
        assert "{{username}}" in body_text

    welcome = "\n".join(templates[TPL_WELCOME])
    assert "多游戏 Bot 竞赛平台" in welcome
    assert "邮箱已验证完成" in welcome
    for game_name in ("德州扑克", "五子棋", "点格棋"):
        assert game_name in welcome


def test_seeded_templates_render_formal_user_facing_copy():
    ctx = {"username": "参赛者", "code": "123456", "expires_minutes": 30}
    templates = _templates_by_key()

    verify_subject, verify_html, verify_text = templates[TPL_VERIFY_EMAIL]
    assert verify_subject == "【Botbattle】邮箱验证码"
    for rendered in (
        render_template(verify_html, ctx),
        render_template(verify_text, ctx),
    ):
        assert "参赛者" in rendered
        assert "123456" in rendered
        assert "30 分钟内有效" in rendered
        assert "{{" not in rendered

    welcome_subject, welcome_html, welcome_text = templates[TPL_WELCOME]
    assert welcome_subject == "【Botbattle】欢迎加入多游戏 Bot 竞赛平台"
    assert "参赛者" in render_template(welcome_html, ctx)
    assert "参赛者" in render_template(welcome_text, ctx)


def test_reseeding_preserves_admin_custom_template(tmp_path):
    db_path = tmp_path / "mail-seed.db"
    first = Store(str(db_path))
    first.update_template(
        TPL_WELCOME,
        subject="赛事组委会通知",
        body_html="<p>自定义正文</p>",
        body_text="自定义正文",
    )
    first.close()

    reopened = Store(str(db_path))
    try:
        template = reopened.get_template(TPL_WELCOME)
        assert template["subject"] == "赛事组委会通知"
        assert template["body_html"] == "<p>自定义正文</p>"
        assert template["body_text"] == "自定义正文"
    finally:
        reopened.close()
