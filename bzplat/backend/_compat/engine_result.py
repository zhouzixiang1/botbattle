"""转发：bzplat.backend.engine.result（向后兼容类型提示基类）。

全面解耦 PR4：result 已拆为各游戏独立副本（games/<game>/result.py），不再共享基类。
通用编排层（orchestrator/contests）对结果对象只读鸭子契约字段
（winners/deltas/rounds_played/rounds/events/winner），不依赖具体类。

本兼容层提供一个**仅类型提示用**的 MatchResult/RoundResult 基类，供：
- 旧代码 ``from engine.result import MatchResult`` 做类型注解
- 测试构造 fake result（MatchResult(rounds_played=0)）

注意：各游戏真正的 result 类（games/<game>/result.py）不继承此类——它们独立定义、
结构兼容（鸭子类型）。本类仅为 import 兼容，非运行时父类。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class RoundResult:
    """单轮结果（类型提示用基类；各游戏独立定义同名类）。"""

    winners: list[int]
    deltas: list[int]


@dataclass
class MatchResult:
    """整场结果（类型提示用基类；各游戏独立定义同名类）。"""

    rounds_played: int
    rounds: list[RoundResult] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    @property
    def winner(self) -> int | None:
        if len(self.rounds) == 1 and self.rounds[0].winners:
            return self.rounds[0].winners[0]
        return None


__all__ = ["RoundResult", "MatchResult"]
