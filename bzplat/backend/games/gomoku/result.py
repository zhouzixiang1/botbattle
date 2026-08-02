"""五子棋对局结果（独立定义，不共享基类——全面解耦 PR4）。

满足平台鸭子契约（winners + deltas + rounds_played + rounds + events + winner）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoundResult:
    """单局结果（通用契约：winners + deltas）。"""

    winners: list[int]  # 座位号；空表示平局
    deltas: list[int]  # 长 2，零和（胜方 +1/-2、负方反之、平局 0）


@dataclass
class MatchResult:
    """五子棋整场结果（单局=一场）。"""

    rounds_played: int
    rounds: list[RoundResult] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    winner: int | None = None
    reason: str = "draw"  # five | draw | illegal | error
    scores: list[int] = field(default_factory=lambda: [0, 0])
    moves: int = 0
    board_grid: list[list[int]] = field(default_factory=list)


__all__ = ["RoundResult", "MatchResult"]
