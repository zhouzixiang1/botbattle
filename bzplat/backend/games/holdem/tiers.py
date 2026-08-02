"""德州扑克段位曲线（per-game，独立于其他游戏——全面解耦 PR4）。

rating → 段位映射，配中文名 + tailwind 色板。后续可独立于其他游戏调整阈值。
前端 lib/tiers.ts（PR6 拆 per-game）保持一致。
"""
from __future__ import annotations

from bzplat.backend.games.base import TierDef

TIERS: list[TierDef] = [
    TierDef(5, "master", "大师", "text-violet-700", "bg-violet-50", 2200),
    TierDef(4, "expert", "专家", "text-indigo-700", "bg-indigo-50", 2050),
    TierDef(3, "gold", "高手", "text-amber-700", "bg-amber-50", 1900),
    TierDef(2, "silver", "熟练", "text-slate-700", "bg-slate-100", 1750),
    TierDef(1, "bronze", "进阶", "text-emerald-700", "bg-emerald-50", 1600),
    TierDef(0, "novice", "新手", "text-sky-700", "bg-sky-50", 0),
]


def tier_for(rating: float | int | None) -> TierDef:
    """返回 rating 对应的段位。None/空 → 最低段位（新手）。"""
    if rating is None:
        return TIERS[-1]
    r = float(rating)
    for t in TIERS:
        if r >= t.min_rating:
            return t
    return TIERS[-1]
