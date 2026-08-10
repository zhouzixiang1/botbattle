"""Strict allow-list diagnostic bundle generation for beginner bug reports."""
from __future__ import annotations

import os
import re
from typing import Any

from bzplat.backend.store import Store
from bzplat.backend.store.schema import CONTEST_CANCELLED, CONTEST_DRAFT

from .utils import safe_route

DIAGNOSTIC_SCHEMA_VERSION = 1
BROWSER_FAMILIES = frozenset({"chrome", "firefox", "safari", "edge", "other", "unknown"})
OS_FAMILIES = frozenset({"windows", "macos", "linux", "android", "ios", "other", "unknown"})
FAILED_API_TEMPLATES = frozenset({
    "/api/auth/*",
    "/api/bots/*",
    "/api/matches/*",
    "/api/contests/*",
    "/api/communications/*",
    "/api/feedback/bugs",
    "/api/notifications",
})
_LOCALE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})?$")
_TIMEZONE_RE = re.compile(r"^[A-Za-z0-9_+.-]+(?:/[A-Za-z0-9_+.-]+){0,2}$")
_TRACE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_BUILD_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")


def _build_id() -> str:
    raw = os.environ.get("BZ_BUILD_SHA", "").strip()
    return raw.lower() if _BUILD_RE.fullmatch(raw) else "unknown"


def build_diagnostic_bundle(
    store: Store,
    *,
    current_route: str,
    role: str,
    browser_family: str,
    os_family: str,
    viewport_width: int | None,
    viewport_height: int | None,
    locale: str,
    timezone: str,
    failed_api_template: str | None,
    failed_api_status: int | None,
    trace_id: str,
    public_match_id: str | None,
    contest_id: int | None,
) -> dict[str, Any]:
    if browser_family not in BROWSER_FAMILIES:
        raise ValueError("browser_family 只能使用粗粒度枚举")
    if os_family not in OS_FAMILIES:
        raise ValueError("os_family 只能使用粗粒度枚举")
    route = safe_route(current_route)
    if viewport_width is not None and not 240 <= viewport_width <= 16_384:
        raise ValueError("viewport_width 超出安全范围")
    if viewport_height is not None and not 240 <= viewport_height <= 16_384:
        raise ValueError("viewport_height 超出安全范围")
    if locale and not _LOCALE_RE.fullmatch(locale):
        raise ValueError("locale 格式无效")
    if timezone and not _TIMEZONE_RE.fullmatch(timezone):
        raise ValueError("timezone 格式无效")
    if failed_api_template is not None and failed_api_template not in FAILED_API_TEMPLATES:
        raise ValueError("failed_api_template 不在公开模板白名单")
    if failed_api_status is not None and not 100 <= failed_api_status <= 599:
        raise ValueError("failed_api_status 无效")
    if trace_id and not _TRACE_RE.fullmatch(trace_id):
        raise ValueError("trace_id 格式无效")
    if failed_api_template is None and (failed_api_status is not None or trace_id):
        raise ValueError("status/trace_id 必须与 failed_api_template 一起提交")
    if (viewport_width is None) != (viewport_height is None):
        raise ValueError("视口宽高必须同时提交")

    bundle: dict[str, Any] = {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "build": _build_id(),
        "route": route,
        "role": role,
        "client": {
            "browser_family": browser_family,
            "os_family": os_family,
            "viewport": (
                {"width": viewport_width, "height": viewport_height}
                if viewport_width is not None and viewport_height is not None
                else None
            ),
            "locale": locale or "unknown",
            "timezone": timezone or "unknown",
        },
        "failed_api": (
            {
                "template": failed_api_template,
                "status": failed_api_status,
                "trace_id": trace_id or None,
            }
            if failed_api_template is not None
            else None
        ),
        "public_context": {},
    }
    if public_match_id:
        match = store.get_match(public_match_id)
        if match:
            bundle["public_context"]["match"] = {
                key: match.get(key)
                for key in (
                    "id", "game_id", "status", "match_type", "reason",
                    "winner", "created_at", "started_at", "ended_at",
                )
            }
    if contest_id is not None:
        contest = store.get_contest(contest_id)
        # Draft/cancelled contests are hidden from ordinary public APIs; diagnostics
        # must not become an oracle for their titles or state.
        if contest and contest.get("status") not in {CONTEST_DRAFT, CONTEST_CANCELLED}:
            bundle["public_context"]["contest"] = {
                key: contest.get(key)
                for key in ("id", "title", "game_id", "status", "current_stage_idx")
            }
    stats = store.count_stats()
    bundle["public_context"]["queue"] = {
        "pending": int(stats.get("matches_pending", 0)),
        "running": int(stats.get("matches_running", 0)),
    }
    # Intentionally absent: headers/UA/cookies/tokens/email/real-name/binary paths,
    # replay, raw stderr/debug and game-private state such as hole cards.
    return bundle
