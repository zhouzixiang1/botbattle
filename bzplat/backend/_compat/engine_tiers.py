"""转发：bzplat.backend.engine.tiers（全局段位，向后兼容）。

全面解耦 PR4：段位已 per-game（games/<game>/tiers.py）。旧 engine.tiers 提供全局
tier_for/tier_dict/all_tiers——本兼容层委托 games 注册表（默认 holdem 曲线），
保旧 import 与 /api/tiers 无参调用（默认 holdem）可用。
"""
from __future__ import annotations

from bzplat.backend.games import registry as _reg
from bzplat.backend.games.holdem.tiers import TIERS  # noqa: F401（向后兼容 TIERS 导出）


def tier_for(rating, game_id: str = "holdem"):
    """旧全局 tier_for（默认 holdem 曲线）。新代码用 registry.tier_for(game_id, rating)。"""
    return _reg.tier_for(game_id, rating)


def tier_dict(rating, game_id: str = "holdem") -> dict:
    return _reg.tier_dict(game_id, rating)


def all_tiers(game_id: str = "holdem") -> list[dict]:
    return _reg.all_tiers(game_id)
