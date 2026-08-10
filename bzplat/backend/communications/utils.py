"""communications 内部的公共、小而纯的安全工具。"""
from __future__ import annotations

import hashlib
import html
import json
import re
import secrets
from datetime import datetime
from typing import Any

_SAFE_ROUTE_RE = re.compile(r"^/[A-Za-z0-9_./:@-]{0,199}$")


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def public_id(prefix: str) -> str:
    """生成不可枚举、可辨实体类型的公开 ID。"""
    return f"{prefix}_{secrets.token_urlsafe(18)}"


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def plain_to_safe_html(body: str) -> str:
    """只从纯文本生成 HTML；不接受/清洗调用方 HTML。"""
    escaped = html.escape(body, quote=True)
    paragraphs = [part.replace("\n", "<br>") for part in escaped.split("\n\n")]
    return "".join(f"<p>{part}</p>" for part in paragraphs if part) or "<p></p>"


def clean_text(value: str, *, max_length: int, field: str) -> str:
    text = (value or "").replace("\x00", "").strip()
    if not text:
        raise ValueError(f"{field}不能为空")
    if len(text) > max_length:
        raise ValueError(f"{field}不能超过 {max_length} 个字符")
    return text


def clean_single_line(value: str, *, max_length: int, field: str) -> str:
    text = clean_text(value, max_length=max_length, field=field)
    if "\r" in text or "\n" in text:
        raise ValueError(f"{field}必须是单行文本")
    return text


def safe_route(value: str) -> str:
    """保留无 query/fragment 的站内路径，避免诊断收集 URL 中的 token。"""
    route = (value or "").split("?", 1)[0].split("#", 1)[0].strip()
    if not route:
        return ""
    if not _SAFE_ROUTE_RE.fullmatch(route):
        raise ValueError("current_route 不是安全的站内路径")
    return route


def deterministic_message_id(idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"<{digest}@mail.botbattle.local>"
