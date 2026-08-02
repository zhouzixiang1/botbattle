"""五子棋段位曲线（per-game，独立于其他游戏——全面解耦 PR4）。

查表算法共享自 base.tier_for_in（PR-D DRY）；本文件只声明五子棋专属 TIERS。
"""
from __future__ import annotations

from bzplat.backend.games.base import TierDef, tier_for_in

TIERS: list[TierDef] = [
    TierDef(5, "master", "大师", "text-violet-700", "bg-violet-50", 2200),
    TierDef(4, "expert", "专家", "text-indigo-700", "bg-indigo-50", 2050),
    TierDef(3, "gold", "高手", "text-amber-700", "bg-amber-50", 1900),
    TierDef(2, "silver", "熟练", "text-slate-700", "bg-slate-100", 1750),
    TierDef(1, "bronze", "进阶", "text-emerald-700", "bg-emerald-50", 1600),
    TierDef(0, "novice", "新手", "text-sky-700", "bg-sky-50", 0),
]


def tier_for(rating: float | int | None) -> TierDef:
    return tier_for_in(rating, TIERS)
