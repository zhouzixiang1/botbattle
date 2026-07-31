"""对局结果的公共基类。

三款游戏（holdem/gomoku/pencil）各有自己语义正确的结果类型，但通用编排层
（orchestrator/rating/replay/contests）对结果对象只依赖一个最小契约：
- 单局/单手结果的 `winners`（座位号；空=平局）与 `deltas`（长2，各方分差/筹码差）
- 整场结果的 `rounds_played`（holdem=手数；棋类=步数；仅写 DB 显示用）与
  `rounds`（单局/单手结果列表，通用层 sum(deltas) + 取 [0].winners）

本模块定义这个公共父类，避免棋类再借 holdem 的 `HandResult` 塞
`pot/board/holes/folded` 占位值。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoundResult:
    """单轮结果（holdem 一手 = 一轮；棋类一局 = 一轮）。"""

    winners: list[int]  # 座位号；空表示平局
    deltas: list[int]  # 长 2，各座位的分差/筹码差（零和）


@dataclass
class MatchResult:
    """整场对局的通用基类。"""

    rounds_played: int  # holdem=手数；棋类=步数（仅写 DB 显示列用）
    rounds: list[RoundResult] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def winner(self) -> int | None:
        """单局棋类（rounds 恰一条）取该轮胜者；多手（扑克）返回 None。"""
        if len(self.rounds) == 1 and self.rounds[0].winners:
            return self.rounds[0].winners[0]
        return None


__all__ = ["RoundResult", "MatchResult"]
