"""向后兼容 shim（全面解耦 PR4）——转发到 _compat.engine_game。

德州引擎已迁入 games/holdem/engine.py。
"""
from bzplat.backend._compat.engine_game import *  # noqa: F401,F403
