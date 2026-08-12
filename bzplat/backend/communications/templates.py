"""代码拥有、显式版本化的事务邮件模板。

旧 ``email_templates`` 表只保留为历史/只读兼容数据。运行路径从不读取其正文，
因此启动不会覆盖管理员以前保存的自定义内容，也不会让数据库正文改变安全邮件。
"""
from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import Any

from bzplat.backend.store.schema import (
    TPL_RESET_PASSWORD,
    TPL_VERIFY_EMAIL,
    TPL_WELCOME,
)

_PLACEHOLDER_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    key: str
    version: int
    subject: str
    body_html: str
    body_text: str
    secret: bool = False

    def render(self, context: dict[str, Any]) -> tuple[str, str, str]:
        def text_repl(match: re.Match[str]) -> str:
            return str(context.get(match.group(1), match.group(0)))

        def html_repl(match: re.Match[str]) -> str:
            value = context.get(match.group(1), match.group(0))
            return html.escape(str(value), quote=True)

        subject = _PLACEHOLDER_RE.sub(text_repl, self.subject)
        body_text = _PLACEHOLDER_RE.sub(text_repl, self.body_text)
        body_html = _PLACEHOLDER_RE.sub(html_repl, self.body_html)
        return subject, body_text, body_html


_TEMPLATES = {
    TPL_VERIFY_EMAIL: TemplateSpec(
        key=TPL_VERIFY_EMAIL,
        version=1,
        subject="【Botbattle】邮箱验证码",
        body_html=(
            "<p>{{username}}，你好：</p>"
            "<p>你正在验证 Botbattle 账号邮箱，验证码为 "
            "<strong>{{code}}</strong>。验证码在 {{expires_minutes}} 分钟内有效。</p>"
            "<p>如非本人操作，请忽略本邮件，且不要向任何人透露验证码。</p>"
            "<p>Botbattle 多游戏 Bot 竞赛平台</p>"
        ),
        body_text=(
            "{{username}}，你好：\n"
            "你正在验证 Botbattle 账号邮箱，验证码为 {{code}}。"
            "验证码在 {{expires_minutes}} 分钟内有效。\n"
            "如非本人操作，请忽略本邮件，且不要向任何人透露验证码。\n"
            "Botbattle 多游戏 Bot 竞赛平台"
        ),
        secret=True,
    ),
    TPL_RESET_PASSWORD: TemplateSpec(
        key=TPL_RESET_PASSWORD,
        version=1,
        subject="【Botbattle】密码重置验证码",
        body_html=(
            "<p>{{username}}，你好：</p>"
            "<p>你正在申请重置 Botbattle 账号密码，验证码为 "
            "<strong>{{code}}</strong>。验证码在 {{expires_minutes}} 分钟内有效。</p>"
            "<p>如非本人操作，请忽略本邮件并及时检查账号安全。</p>"
            "<p>Botbattle 多游戏 Bot 竞赛平台</p>"
        ),
        body_text=(
            "{{username}}，你好：\n"
            "你正在申请重置 Botbattle 账号密码，验证码为 {{code}}。"
            "验证码在 {{expires_minutes}} 分钟内有效。\n"
            "如非本人操作，请忽略本邮件并及时检查账号安全。\n"
            "Botbattle 多游戏 Bot 竞赛平台"
        ),
        secret=True,
    ),
    TPL_WELCOME: TemplateSpec(
        key=TPL_WELCOME,
        version=1,
        subject="【Botbattle】欢迎加入多游戏 Bot 竞赛平台",
        body_html=(
            "<p>{{username}}，你好：</p>"
            "<p>欢迎加入 Botbattle 多游戏 Bot 竞赛平台。你的邮箱已验证完成。</p>"
            "<p>平台支持德州扑克、五子棋和点格棋；你可以上传 Bot、发起挑战、"
            "参加锦标赛并查看对局回放。</p>"
            "<p>开始前请在 Wiki 查看对应游戏规则与唯一现行通信协议。</p>"
        ),
        body_text=(
            "{{username}}，你好：\n"
            "欢迎加入 Botbattle 多游戏 Bot 竞赛平台。你的邮箱已验证完成。\n"
            "平台支持德州扑克、五子棋和点格棋；你可以上传 Bot、发起挑战、"
            "参加锦标赛并查看对局回放。\n"
            "开始前请在 Wiki 查看对应游戏规则与唯一现行通信协议。"
        ),
    ),
}


def get_template(key: str, version: int | None = None) -> TemplateSpec:
    template = _TEMPLATES.get(key)
    if template is None or (version is not None and template.version != version):
        raise KeyError(f"未知邮件模板版本: {key}@{version}")
    return template


def list_templates() -> list[TemplateSpec]:
    return sorted(_TEMPLATES.values(), key=lambda item: item.key)


def legacy_seed_rows() -> list[tuple[str, str, str, str]]:
    """仅供旧表首次 INSERT OR IGNORE；永不覆盖历史自定义。"""
    return [
        (item.key, item.subject, item.body_html, item.body_text)
        for item in list_templates()
    ]
