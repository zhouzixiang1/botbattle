"""邮件发送：SMTP + 模板渲染。"""
from __future__ import annotations

import logging
import os
import re
import smtplib
import ssl
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from typing import Any

# 模板 key 字面量（避免 mail → store.schema → store.__init__ → db → mail 循环导入）
TPL_VERIFY_EMAIL = "verify_email"
TPL_RESET_PASSWORD = "reset_password"
TPL_WELCOME = "welcome"

logger = logging.getLogger(__name__)


_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_template(text: str, ctx: dict[str, Any]) -> str:
    """将 ``{{key}}`` 占位符替换为 ctx 中的值。"""

    def repl(m: re.Match) -> str:
        key = m.group(1)
        return str(ctx.get(key, m.group(0)))

    return _PLACEHOLDER_RE.sub(repl, text or "")


class MailConfig:
    def __init__(self) -> None:
        self.host = os.environ.get("SMTP_HOST", "")
        self.port = int(os.environ.get("SMTP_PORT", "465"))
        self.user = os.environ.get("SMTP_USER", "")
        self.password = os.environ.get("SMTP_PASSWORD", "")
        self.from_addr = os.environ.get("SMTP_FROM", self.user)
        self.from_name = os.environ.get("SMTP_FROM_NAME", "botzone-platform")
        self.code_ttl_minutes = int(os.environ.get("EMAIL_CODE_TTL_MINUTES", "30"))

    @property
    def configured(self) -> bool:
        return bool(self.host and self.user and self.password and self.from_addr)


class Mailer:
    """同步 SMTP_SSL 发信。"""

    def __init__(self, config: MailConfig | None = None) -> None:
        self.config = config or MailConfig()

    def send(
        self,
        to_addr: str,
        subject: str,
        *,
        body_text: str = "",
        body_html: str = "",
    ) -> None:
        if not self.config.configured:
            raise RuntimeError("SMTP 未配置（检查 SMTP_USER/SMTP_PASSWORD）")
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = formataddr((self.config.from_name, self.config.from_addr))
        msg["To"] = to_addr
        if body_text:
            msg.attach(MIMEText(body_text, "plain", "utf-8"))
        if body_html:
            msg.attach(MIMEText(body_html, "html", "utf-8"))
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(
            self.config.host, self.config.port, context=context, timeout=30
        ) as server:
            server.login(self.config.user, self.config.password)
            server.sendmail(self.config.from_addr, [to_addr], msg.as_string())
        logger.info("mail sent to=%s subject=%s", to_addr, subject)


def default_email_templates() -> list[tuple[str, str, str, str]]:
    """返回默认邮件模板 (key, subject, body_html, body_text)。"""
    return [
        (
            TPL_VERIFY_EMAIL,
            "【botzone-platform】邮箱验证码",
            "<p>你好 {{username}},</p>"
            "<p>你的邮箱验证码是 <strong>{{code}}</strong>,"
            "{{expires_minutes}} 分钟内有效。</p>"
            "<p>如非本人操作请忽略本邮件。</p>",
            "你好 {{username}},\n你的邮箱验证码是 {{code}},"
            "{{expires_minutes}} 分钟内有效。\n如非本人操作请忽略。",
        ),
        (
            TPL_RESET_PASSWORD,
            "【botzone-platform】密码重置验证码",
            "<p>你好 {{username}},</p>"
            "<p>你正在重置密码,验证码 <strong>{{code}}</strong>,"
            "{{expires_minutes}} 分钟内有效。</p>"
            "<p>如非本人操作请立即忽略并检查账号安全。</p>",
            "你好 {{username}},\n你正在重置密码,验证码 {{code}},"
            "{{expires_minutes}} 分钟内有效。",
        ),
        (
            TPL_WELCOME,
            "【botzone-platform】欢迎加入",
            "<p>你好 {{username}},欢迎加入 botzone-platform 德州扑克竞赛平台!</p>"
            "<p>请先完成邮箱验证,然后上传 bot 并发起对战。</p>",
            "你好 {{username}},欢迎加入 botzone-platform!\n请先完成邮箱验证。",
        ),
    ]


def seed_email_templates(conn, now: str) -> None:
    """向连接写入默认邮件模板（INSERT OR IGNORE）。"""
    for key, subject, html, text in default_email_templates():
        conn.execute(
            "INSERT OR IGNORE INTO email_templates"
            "(key, subject, body_html, body_text, updated_at) "
            "VALUES(?,?,?,?,?)",
            (key, subject, html, text, now),
        )


__all__ = [
    "MailConfig",
    "Mailer",
    "render_template",
    "default_email_templates",
    "seed_email_templates",
]
