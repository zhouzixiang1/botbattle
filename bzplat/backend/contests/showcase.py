"""Long-lived, read-only contest showcase snapshots.

``showcase_key`` is deliberately orthogonal to the lifecycle status: the status
controls what a customer sees, while the non-null key freezes that graph against
all normal scheduler, reconciliation and HTTP writes.  Only the explicit seed
tool may create or roll back these synthetic records.
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.contests.templates import get_template

SHOWCASE_KEYS = (
    "contest_lifecycle_draft",
    "contest_lifecycle_open",
    "contest_lifecycle_published",
    "contest_lifecycle_running",
    "contest_lifecycle_rest",
    "contest_lifecycle_finished",
)

SHOWCASE_READ_ONLY_MESSAGE = "演示快照为合成只读数据，不能执行生命周期、报名或赛程写操作"


class ShowcaseReadOnlyError(ValueError):
    """A normal write was attempted against an immutable showcase graph."""


def is_showcase(contest: dict[str, Any] | None) -> bool:
    return bool(contest and contest.get("showcase_key"))


def require_mutable(contest: dict[str, Any] | None) -> None:
    if is_showcase(contest):
        raise ShowcaseReadOnlyError(SHOWCASE_READ_ONLY_MESSAGE)


def template_name(contest: dict[str, Any]) -> str:
    """Resolve the code-owned Chinese label without consulting legacy DB rows."""
    template_id = str(contest.get("template_id") or "")
    template = get_template(template_id) if template_id else None
    if template and template.get("name"):
        return str(template["name"])
    return "自定义赛制" if template_id == "custom" else template_id or "未指定赛制"


def public_description(contest: dict[str, Any]) -> str:
    """Hide the resumability marker while preserving customer-facing copy."""
    description = str(contest.get("description") or "")
    if not is_showcase(contest):
        return description
    first, separator, remainder = description.partition("\n")
    if (
        separator
        and first.startswith("[contest-showcase-")
        and first.endswith("]")
    ):
        return remainder
    return description


__all__ = [
    "SHOWCASE_KEYS",
    "SHOWCASE_READ_ONLY_MESSAGE",
    "ShowcaseReadOnlyError",
    "is_showcase",
    "public_description",
    "require_mutable",
    "template_name",
]
