"""平台持久化对局结果的唯一公共契约。

游戏引擎仍可以在自己的 ``MatchResult`` 上保留专属字段；平台
``matches_<game>.result`` 的共通部分只能由本模块构造：

``rounds_played``
    已完成的手数、合法落子数或合法占边数。
``deltas``
    两个座位的原始零和分差。
``normalized_delta``
    座位 0 分差经 ``GameSpec.normalize_delta`` 换算后的数值。

技术判负没有可返回的引擎结果，因此通过同一 GameSpec 的
``progress_from_events`` 统计进度，避免通用编排层出现游戏名分支。
"""
from __future__ import annotations

import math
from numbers import Integral, Real
from typing import Any, Mapping, Sequence

from bzplat.backend.games.base import GameSpec

RESULT_COMMON_FIELDS = frozenset(
    {"rounds_played", "deltas", "normalized_delta"}
)


def _canonical_rounds_played(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        raise TypeError("rounds_played 必须是非负整数")
    rounds_played = int(value)
    if rounds_played < 0:
        raise ValueError("rounds_played 不能为负数")
    return rounds_played


def canonical_deltas(value: Any) -> list[int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("deltas 必须是长度为 2 的整数序列")
    if len(value) != 2:
        raise ValueError("deltas 长度必须为 2")
    if any(isinstance(item, bool) or not isinstance(item, Integral) for item in value):
        raise TypeError("deltas 只能包含整数")
    deltas = [int(value[0]), int(value[1])]
    if sum(deltas) != 0:
        raise ValueError("deltas 必须零和")
    return deltas


def build_result_payload(
    spec: GameSpec,
    *,
    rounds_played: int,
    deltas: Sequence[int],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """构造一个完整、已校验的持久化结果。

    ``extra`` 只用于 duplicate ``legs`` 和有界技术摘要等扩展，
    不允许覆盖三个公共字段。
    """
    canonical_rounds = _canonical_rounds_played(rounds_played)
    canonical_delta_values = canonical_deltas(deltas)
    normalized = spec.normalize_delta(canonical_delta_values[0])
    if (
        isinstance(normalized, bool)
        or not isinstance(normalized, Real)
        or not math.isfinite(float(normalized))
    ):
        raise ValueError("normalize_delta 必须返回有限数值")

    payload: dict[str, Any] = {
        "rounds_played": canonical_rounds,
        "deltas": canonical_delta_values,
        "normalized_delta": float(normalized),
    }
    if extra:
        overlap = RESULT_COMMON_FIELDS.intersection(extra)
        if overlap:
            raise ValueError(f"扩展结果不得覆盖公共字段: {sorted(overlap)}")
        payload.update(extra)
    return payload


def build_engine_result_payload(
    spec: GameSpec,
    result: Any,
    *,
    deltas: Sequence[int],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """将游戏引擎结果收敛到持久化契约。"""
    return build_result_payload(
        spec,
        rounds_played=getattr(result, "rounds_played"),
        deltas=deltas,
        extra=extra,
    )


def build_technical_result_payload(
    spec: GameSpec,
    events: list[dict[str, Any]],
    *,
    deltas: Sequence[int],
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """用已公开的游戏事件前缀构造技术终局结果。"""
    return build_result_payload(
        spec,
        rounds_played=spec.progress_from_events(events),
        deltas=deltas,
        extra=extra,
    )


__all__ = [
    "RESULT_COMMON_FIELDS",
    "build_engine_result_payload",
    "build_result_payload",
    "build_technical_result_payload",
    "canonical_deltas",
]
