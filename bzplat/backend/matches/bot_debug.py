"""Bot 响应 ``debug`` sidecar 的私有、有界采集契约。

本模块只处理已经由 Botzone 信封层解析出的顶层 ``debug`` 值。动作仍由
``response`` 唯一决定；调试内容的清洗、截断或丢弃绝不能改变裁判结果。
收集器只保存在内存中，编排器在对局进入终态后一次性批量持久化。
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from bzplat.backend.runtime.limits import MAX_BOT_RESPONSE_LINE_BYTES
from bzplat.backend.store.schema import (
    MATCH_DEBUG_MAX_BYTES_PER_MATCH,
    MATCH_DEBUG_MAX_BYTES_PER_SEAT,
    MATCH_DEBUG_MAX_ENTRIES_PER_MATCH,
    MATCH_DEBUG_MAX_ENTRIES_PER_SEAT,
    MATCH_DEBUG_MAX_ENTRY_BYTES,
)

# 兼容本模块既有测试/调用名；权威硬顶位于 runtime.limits，传输与协议共用。
MAX_RESPONSE_LINE_BYTES = MAX_BOT_RESPONSE_LINE_BYTES
MAX_DEBUG_ENTRY_BYTES = MATCH_DEBUG_MAX_ENTRY_BYTES
MAX_DEBUG_DEPTH = 4
MAX_DEBUG_CONTAINER_ITEMS = 64
MAX_DEBUG_NODES = 256
MAX_DEBUG_ENTRIES_PER_SEAT = MATCH_DEBUG_MAX_ENTRIES_PER_SEAT
MAX_DEBUG_BYTES_PER_SEAT = MATCH_DEBUG_MAX_BYTES_PER_SEAT
MAX_DEBUG_ENTRIES_PER_MATCH = MATCH_DEBUG_MAX_ENTRIES_PER_MATCH
MAX_DEBUG_BYTES_PER_MATCH = MATCH_DEBUG_MAX_BYTES_PER_MATCH

_REDACTED = "[REDACTED]"
_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_ANSI_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)? PRIVATE KEY-----.*?"
    r"(?:-----END(?: [A-Z0-9]+)? PRIVATE KEY-----|$)",
    re.IGNORECASE | re.DOTALL,
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_BASIC_AUTH_RE = re.compile(r"(?i)\bBasic\s+[A-Za-z0-9+/=]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_COOKIE_SECRET_RE = re.compile(
    r"(?i)\b(set-cookie|cookie)(\s*(?:=|:)\s*).*$"
)
_QUOTED_SECRET_RE = re.compile(
    r"(?i)\b(token|secret|api[_-]?key|access[_-]?key|password|passwd|"
    r"authorization|credential|cookie|session|private[_-]?key)"
    r"(\s*(?:=|:)\s*)"
    r"(?:\"(?:\\.|[^\"\\])*(?:\"|$)|'(?:\\.|[^'\\])*(?:'|$))"
)
_UNQUOTED_SECRET_RE = re.compile(
    r"(?i)\b(token|secret|api[_-]?key|access[_-]?key|password|passwd|"
    r"authorization|credential|cookie|session|private[_-]?key)"
    r"(\s*(?:=|:)\s*)[^\s&#,;]+"
)
_SENSITIVE_KEY_PARTS = (
    "password",
    "passwd",
    "secret",
    "token",
    "authorization",
    "credential",
    "cookie",
    "session",
    "privatekey",
    "apikey",
    "accesskey",
)


def _truncate_utf8(value: str, max_bytes: int) -> str:
    raw = value.encode("utf-8")
    if len(raw) <= max_bytes:
        return value
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _safe_text(value: str, *, max_bytes: int = 2048) -> str:
    text = unicodedata.normalize("NFC", value)
    text = _ANSI_OSC_RE.sub("", _ANSI_CSI_RE.sub("", text))
    # Cc/Cf 包含换行、终端控制符、零宽与双向控制字符。调试展示不需要
    # 保留这些不可见状态；用空格替代分隔控制，其余直接移除。
    chars: list[str] = []
    for char in text:
        category = unicodedata.category(char)
        if category in {"Cc", "Cf"}:
            if char in "\r\n\t":
                chars.append(" ")
            continue
        chars.append(char)
    text = "".join(chars)
    text = _PRIVATE_KEY_RE.sub(_REDACTED, text)
    text = _BEARER_RE.sub("Bearer " + _REDACTED, text)
    text = _BASIC_AUTH_RE.sub("Basic " + _REDACTED, text)
    text = _JWT_RE.sub(_REDACTED, text)
    redact_assignment = lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED}"
    # Cookie / Set-Cookie 的值本身可含多个以分号分隔的键值对。只遮首段会
    # 把 csrf/session 等后续凭据泄给对手 owner，因此从 header/赋值起整段遮蔽。
    text = _COOKIE_SECRET_RE.sub(redact_assignment, text)
    # Quoted values may contain spaces, so redact them before the conservative
    # unquoted-token form.  Safe over-redaction is intentional at this boundary.
    text = _QUOTED_SECRET_RE.sub(redact_assignment, text)
    text = _UNQUOTED_SECRET_RE.sub(redact_assignment, text)
    return _truncate_utf8(text, max_bytes)


def _sensitive_key(key: str) -> bool:
    canonical = "".join(
        char
        for char in unicodedata.normalize("NFKC", key).casefold()
        if char.isalnum()
    )
    return any(part in canonical for part in _SENSITIVE_KEY_PARTS)


@dataclass
class _Budget:
    nodes: int = 0


def _sanitize(value: Any, *, depth: int, budget: _Budget) -> Any:
    if budget.nodes >= MAX_DEBUG_NODES:
        return "[NODE_LIMIT]"
    budget.nodes += 1
    if depth > MAX_DEBUG_DEPTH:
        return "[DEPTH_LIMIT]"
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else "[NON_FINITE]"
    if isinstance(value, str):
        return _safe_text(value)
    if isinstance(value, list):
        limit = min(len(value), MAX_DEBUG_CONTAINER_ITEMS)
        items = [
            _sanitize(item, depth=depth + 1, budget=budget)
            for item in value[:limit]
        ]
        if len(value) > limit and items:
            items[-1] = "[CONTAINER_LIMIT]"
        return items
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        items = list(value.items())[:MAX_DEBUG_CONTAINER_ITEMS]
        for raw_key, raw_value in items:
            key = _safe_text(str(raw_key), max_bytes=128) or "_"
            if key in result:
                suffix = 2
                base = key
                while f"{base}_{suffix}" in result:
                    suffix += 1
                key = f"{base}_{suffix}"
            result[key] = (
                _REDACTED
                if _sensitive_key(key)
                else _sanitize(raw_value, depth=depth + 1, budget=budget)
            )
        if len(value) > len(items):
            result["__truncated__"] = True
        return result
    return _safe_text(str(value))


def serialize_debug(value: Any) -> str | None:
    """返回安全 JSON；顶层 ``null`` 表示 Bot 未提供调试信息。"""
    if value is None:
        return None
    safe = _sanitize(value, depth=0, budget=_Budget())
    encoded = json.dumps(
        safe,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    if len(encoded.encode("utf-8")) <= MAX_DEBUG_ENTRY_BYTES:
        return encoded

    # 超限时仍返回合法、安全的 JSON。preview 来自已经完成清洗和脱敏的
    # 序列化结果，不可能重新引入原始敏感内容或终端控制序列。
    preview = _truncate_utf8(encoded, MAX_DEBUG_ENTRY_BYTES - 96)
    compact = json.dumps(
        {"truncated": True, "preview": preview},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    while len(compact.encode("utf-8")) > MAX_DEBUG_ENTRY_BYTES and preview:
        preview = _truncate_utf8(preview, max(0, len(preview.encode("utf-8")) - 64))
        compact = json.dumps(
            {"truncated": True, "preview": preview},
            ensure_ascii=False,
            separators=(",", ":"),
        )
    return compact


@dataclass
class BotDebugCollector:
    """一场 Bot-vs-Bot 对局的内存有界 sidecar 收集器。"""

    entries: list[dict[str, Any]] = field(default_factory=list)
    dropped_count: int = 0
    _seat_counts: list[int] = field(default_factory=lambda: [0, 0])
    _seat_bytes: list[int] = field(default_factory=lambda: [0, 0])
    _total_bytes: int = 0

    def capture(
        self,
        *,
        seat: int,
        turn: int,
        debug: Any,
        leg: int | None = None,
    ) -> None:
        """尽力收集一条；任何非法/超限内容只丢弃，不向裁判抛异常。"""
        try:
            if seat not in (0, 1) or int(turn) < 1:
                self.dropped_count += 1
                return
            if debug is None:
                return
            # 容量已经饱和时不得再规范化/遍历/序列化 Bot 控制的 64 KiB
            # JSON。先做无需查看内容的 O(1) 闸门，避免后续回合 CPU 放大。
            if (
                len(self.entries) >= MAX_DEBUG_ENTRIES_PER_MATCH
                or self._total_bytes >= MAX_DEBUG_BYTES_PER_MATCH
                or self._seat_counts[seat] >= MAX_DEBUG_ENTRIES_PER_SEAT
                or self._seat_bytes[seat] >= MAX_DEBUG_BYTES_PER_SEAT
            ):
                self.dropped_count += 1
                return
            serialized = serialize_debug(debug)
            if serialized is None:
                return
            size = len(serialized.encode("utf-8"))
            if (
                len(self.entries) >= MAX_DEBUG_ENTRIES_PER_MATCH
                or self._total_bytes + size > MAX_DEBUG_BYTES_PER_MATCH
                or self._seat_counts[seat] >= MAX_DEBUG_ENTRIES_PER_SEAT
                or self._seat_bytes[seat] + size > MAX_DEBUG_BYTES_PER_SEAT
            ):
                self.dropped_count += 1
                return
            self.entries.append(
                {
                    "seat": seat,
                    "turn": int(turn),
                    "leg": int(leg) if leg is not None else -1,
                    "debug_json": serialized,
                    "size_bytes": size,
                }
            )
            self._seat_counts[seat] += 1
            self._seat_bytes[seat] += size
            self._total_bytes += size
        except Exception:
            self.dropped_count += 1

    @property
    def total_bytes(self) -> int:
        return self._total_bytes


__all__ = [
    "BotDebugCollector",
    "MAX_DEBUG_BYTES_PER_MATCH",
    "MAX_DEBUG_BYTES_PER_SEAT",
    "MAX_DEBUG_CONTAINER_ITEMS",
    "MAX_DEBUG_DEPTH",
    "MAX_DEBUG_ENTRIES_PER_MATCH",
    "MAX_DEBUG_ENTRIES_PER_SEAT",
    "MAX_DEBUG_ENTRY_BYTES",
    "MAX_DEBUG_NODES",
    "MAX_RESPONSE_LINE_BYTES",
    "serialize_debug",
]
