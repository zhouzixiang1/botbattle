"""转发：bzplat.backend.engine.game → bzplat.backend.games.holdem.engine。

德州引擎已迁入 games/holdem/engine.py。
"""
from bzplat.backend.games.holdem.engine import *  # noqa: F401,F403
from bzplat.backend.games.holdem.engine import (  # noqa: F401
    BIG_BLIND,
    DEFAULT_HANDS,
    SMALL_BLIND,
    STARTING_STACK,
    Action,
    GameEngine,
    MatchSession,
    Street,
)
# result 类经 holdem.result 提供（向后兼容旧 from engine.game import MatchResult/HandResult）
from bzplat.backend.games.holdem.result import HandResult, MatchResult  # noqa: F401
