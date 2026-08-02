"""向后兼容 shim（全面解耦 PR4）——转发到 _compat.engine_pencil。

点格棋引擎已迁入 games/pencil/engine.py。
"""
from bzplat.backend._compat.engine_pencil import *  # noqa: F401,F403
