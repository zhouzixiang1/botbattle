"""转发：bzplat.backend.engine.gomoku → bzplat.backend.games.gomoku.engine。

五子棋引擎已迁入 games/gomoku/engine.py。
"""
from bzplat.backend.games.gomoku.engine import *  # noqa: F401,F403
from bzplat.backend.games.gomoku.engine import (  # noqa: F401
    BOARD_SIZE,
    GomokuSession,
    check_win,
    in_board,
)
# 旧代码用 GomokuResult；现 gomoku 的 MatchResult 即原 GomokuResult（字段一致），
# 提供别名保 from engine.gomoku import GomokuResult 可用。
from bzplat.backend.games.gomoku.result import MatchResult as GomokuResult  # noqa: F401
