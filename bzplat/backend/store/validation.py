"""Persistence-level validation shared by safe Store write paths."""
from __future__ import annotations

from datetime import datetime


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


__all__ = ["validate_contest_times"]
