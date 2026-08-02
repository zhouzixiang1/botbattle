"""德州扑克段位曲线（per-game，独立于其他游戏——全面解耦 PR4）。

rating → 段位映射。查表算法共享自 base.tier_for_in（PR-D DRY）；本文件只声明
德州专属的 TIERS 数据列表（曲线阈值可独立于其他游戏调整）。
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
    """返回 rating 对应的段位（经共享查表算法）。"""
    return tier_for_in(rating, TIERS)
