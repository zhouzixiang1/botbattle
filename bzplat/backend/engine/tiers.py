"""Rating → 段位映射（前后端镜像：前端 lib/tiers.ts 保持一致）。

段位按 Glicko-2 rating 分档，配中文名 + 颜色（tailwind 色板）。等级 gating（PR-9）
可基于 tier.level 推导。修改此映射时同步前端 lib/tiers.ts。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tier:
    level: int          # 0-based 序号（用于 gating 推导）
    key: str            # 英文 key
    name: str           # 中文段位名
    color: str          # tailwind 文字色类
    bg: str             # tailwind 背景色类（浅）
    min_rating: float   # 该段位最低 rating（含）


# rating 降序匹配：第一个 min_rating <= rating 的段位胜出
TIERS: list[Tier] = [
    Tier(5, "master",   "大师",   "text-violet-700",  "bg-violet-50",  2200),
    Tier(4, "expert",   "专家",   "text-indigo-700",  "bg-indigo-50",  2050),
    Tier(3, "gold",     "高手",   "text-amber-700",   "bg-amber-50",   1900),
    Tier(2, "silver",   "熟练",   "text-slate-700",   "bg-slate-100",  1750),
    Tier(1, "bronze",   "进阶",   "text-emerald-700", "bg-emerald-50", 1600),
    Tier(0, "novice",   "新手",   "text-sky-700",     "bg-sky-50",     0),
]


def tier_for(rating: float | int | None) -> Tier:
    """返回 rating 对应的段位。None/空 → 最低段位（新手）。"""
    if rating is None:
        return TIERS[-1]
    r = float(rating)
    for t in TIERS:
        if r >= t.min_rating:
            return t
    return TIERS[-1]


def tier_dict(rating: float | int | None) -> dict:
    """段位信息（JSON 友好，端点返回用）。"""
    t = tier_for(rating)
    return {
        "level": t.level,
        "key": t.key,
        "name": t.name,
        "color": t.color,
        "bg": t.bg,
        "min_rating": t.min_rating,
    }


def all_tiers() -> list[dict]:
    """全部段位定义（前端镜像校验 / 等级 gating 用）。"""
    return [
        {
            "level": t.level, "key": t.key, "name": t.name,
            "color": t.color, "bg": t.bg, "min_rating": t.min_rating,
        }
        for t in TIERS
    ]
