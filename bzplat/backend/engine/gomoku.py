"""向后兼容 shim（全面解耦 PR4）——转发到 _compat.engine_gomoku。

五子棋引擎已迁入 games/gomoku/engine.py。
"""
from bzplat.backend._compat.engine_gomoku import *  # noqa: F401,F403
