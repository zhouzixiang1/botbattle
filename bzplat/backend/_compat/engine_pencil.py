"""转发：bzplat.backend.engine.pencil → bzplat.backend.games.pencil.engine。

点格棋引擎已迁入 games/pencil/engine.py。
"""
from bzplat.backend.games.pencil.engine import *  # noqa: F401,F403
from bzplat.backend.games.pencil.engine import (  # noqa: F401
    DEFAULT_N,
    GRID_BOX,
    GRID_DOT,
    GRID_EDGE,
    GRID_EDGE_USED,
    PencilBoard,
    PencilSession,
)
# 旧代码用 PencilResult；现 pencil 的 MatchResult 即原 PencilResult（字段一致）。
from bzplat.backend.games.pencil.result import MatchResult as PencilResult  # noqa: F401
