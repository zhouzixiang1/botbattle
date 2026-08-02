"""向后兼容 shim（全面解耦 PR4）——转发到 _compat.engine_tiers。

段位已 per-game（games/<game>/tiers.py），经注册表取曲线。本文件仅为兼容旧
``from bzplat.backend.engine.tiers import tier_for/...``（默认 holdem 曲线）。
"""
from bzplat.backend._compat.engine_tiers import *  # noqa: F401,F403
from bzplat.backend._compat.engine_tiers import TIERS, all_tiers, tier_dict, tier_for  # noqa: F401
