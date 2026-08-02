"""向后兼容 shim（全面解耦 PR4）——转发到 _compat.engine_cards。

cards 仅 holdem 使用，已迁入 games/holdem/cards.py。
"""
from bzplat.backend._compat.engine_cards import *  # noqa: F401,F403
