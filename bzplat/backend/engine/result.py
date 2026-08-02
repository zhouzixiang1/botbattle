"""向后兼容 shim（全面解耦 PR4）——转发到 _compat.engine_result。

result 已拆为各游戏独立副本（games/<game>/result.py）。本文件仅为兼容旧
``from bzplat.backend.engine.result import MatchResult/RoundResult``。
"""
from bzplat.backend._compat.engine_result import *  # noqa: F401,F403
from bzplat.backend._compat.engine_result import MatchResult, RoundResult  # noqa: F401
