"""Persistence-level validation shared by safe Store write paths."""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .schema import STATUS_COMPLETED


_NO_OPPONENT_STAGE_TYPES = frozenset({"swiss", "single_elimination"})


def is_authoritative_no_opponent_pairing(
    stage_type: object,
    pairing: Mapping[str, Any],
) -> bool:
    """Return whether ``pairing`` is a durable no-opponent placeholder.

    Only Swiss and single-elimination stages can create such rows.  Requiring
    both durable entry/Bot identities on side B to be absent prevents a real
    opponent whose Bot was later deleted (FK ``SET NULL``) from being mistaken
    for a bye.  Pending, match-bound and otherwise drifted rows fail closed.
    """
    return bool(
        isinstance(stage_type, str)
        and stage_type in _NO_OPPONENT_STAGE_TYPES
        and pairing.get("entry_b_id") is None
        and pairing.get("bot_b_id") is None
        and pairing.get("match_id") is None
        and pairing.get("status") == STATUS_COMPLETED
    )


def _parse_local_time(value: str, label: str) -> datetime:
    """Parse one naive local ISO timestamp and return its comparable value."""
    if not isinstance(value, str):
        raise ValueError(f"{label}必须是 ISO 时间字符串")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(
            f"{label}格式非法（需 YYYY-MM-DDTHH:MM:SS）: {value}"
        ) from exc
    if parsed.tzinfo is not None:
        raise ValueError(f"{label}不应带时区（平台用 naive 本地时间）: {value}")
    return parsed


def validate_contest_times(
    registration_opens_at: str | None,
    registration_closes_at: str | None,
    starts_at: str | None,
) -> None:
    """Validate the known parts of ``opens <= closes <= starts``.

    All three fields are optional.  Every pair that is present is compared in
    lifecycle order, so omitting the middle value cannot hide an inverted
    opening/start time.  Equal timestamps are deliberately valid: a manually
    advanced contest may open, close registration and start in the same second.
    """
    ordered = (
        ("报名开放时间", registration_opens_at),
        ("报名截止时间", registration_closes_at),
        ("比赛开始时间", starts_at),
    )
    parsed = [
        (label, _parse_local_time(value, label))
        for label, value in ordered
        if value is not None
    ]
    for index, (earlier_label, earlier) in enumerate(parsed):
        for later_label, later in parsed[index + 1 :]:
            if earlier > later:
                raise ValueError(
                    f"{later_label}不能早于{earlier_label}（允许相同）"
                )


__all__ = [
    "is_authoritative_no_opponent_pairing",
    "validate_contest_times",
]
