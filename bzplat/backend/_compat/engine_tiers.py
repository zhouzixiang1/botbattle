"""转发：bzplat.backend.engine.tiers（全局段位，向后兼容）。

全面解耦 PR4：段位已 per-game（games/<game>/tiers.py）。旧 engine.tiers 提供全局
tier_for/tier_dict/all_tiers——本兼容层委托 games 注册表（默认 holdem 曲线），
保旧 import 与 /api/tiers 无参调用（默认 holdem）可用。

PR-D 清理：不再直接 import games.holdem.tiers（消除兼容层对具体游戏的耦合）；
TIERS 从注册表默认 holdem spec 派生。
"""
from __future__ import annotations

from bzplat.backend.games import registry as _reg


def _holdem_spec():
    return _reg.get("holdem")


# 向后兼容 TIERS 导出（旧 from engine.tiers import TIERS）——从注册表 holdem spec 派生
class _TiersProxy:
    """延迟代理 TIERS：访问时从注册表取 holdem 段位曲线（list-like）。"""

    def __iter__(self):
        return iter(_holdem_spec().tiers)

    def __len__(self):
        return len(_holdem_spec().tiers)

    def __getitem__(self, i):
        return _holdem_spec().tiers[i]

    def __list__(self):
        return list(_holdem_spec().tiers)


TIERS = _TiersProxy()  # type: ignore[assignment]


def tier_for(rating, game_id: str = "holdem"):
    """旧全局 tier_for（默认 holdem 曲线）。新代码用 registry.tier_for(game_id, rating)。"""
    return _reg.tier_for(game_id, rating)


def tier_dict(rating, game_id: str = "holdem") -> dict:
    return _reg.tier_dict(game_id, rating)


def all_tiers(game_id: str = "holdem") -> list[dict]:
    return _reg.all_tiers(game_id)
