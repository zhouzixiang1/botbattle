"""德州扑克对局结果（独立定义，不共享基类——全面解耦 PR4）。

满足平台鸭子契约（通用编排/赛制层只读这些字段）：
- RoundResult：winners（座位号列表，空=平局）+ deltas（长 2 零和）
- MatchResult：rounds_played + rounds + events + winner（property）
本游戏在此基础上追加德州专属字段（pot/board/holes/folded/final_chips）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoundResult:
    """单手结果（通用契约：winners + deltas）。"""

    winners: list[int]  # 座位号；空表示平局
    deltas: list[int]  # 长 2，各座位的筹码差（零和）


@dataclass
class HandResult(RoundResult):
    """德州单手结果（追加德州专属字段）。"""

    hand_index: int  # 0-based
    pot: int
    board: list[Any]  # list[Card]
    holes: list[list[Any]]  # list[list[Card]]
    folded: list[bool]
    reason: str  # "fold" | "showdown"


@dataclass
class MatchResult:
    """德州整场结果：rounds 即手结果列表。final_chips 为德州专属。"""

    rounds_played: int
    rounds: list[RoundResult] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)
    final_chips: list[int] = field(default_factory=list)

    @property
    def winner(self) -> int | None:
        """对局级胜者（权威）：单手取该手胜者；多手按累计净筹码（final_chips）比较。

        PR4 修复：原多手恒返回 None，依赖编排层三层兜底（result.winner→ea/eb→match_end
        事件）+ holdem 特例注释——这是隐性 if-game_id。现多手也在引擎内权威化 winner，
        编排层只需读 result.winner（+ ea/eb 平局兜底）。
        """
        if len(self.rounds) == 1:
            w = self.rounds[0].winners
            if len(w) == 1:  # 唯一胜者
                return w[0]
            # winners 长度 0（无胜者）或 >1（split pot 平局）→ 视为平局返 None
            return None
        # 多手：按累计净筹码（final_chips = net）比较，平局返 None
        if len(self.final_chips) >= 2:
            fa, fb = self.final_chips[0], self.final_chips[1]
            if fa > fb:
                return 0
            if fb > fa:
                return 1
        return None

__all__ = ["RoundResult", "HandResult", "MatchResult"]
