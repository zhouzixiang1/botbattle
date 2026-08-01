"""HU NLHE / Gomoku / Pencil 引擎（裁判实现层）。

全面解耦后，本包主要是裁判引擎实现（game.py/gomoku.py/pencil.py + result.py +
cards.py）。registry.py 是转发层（委托 games 注册表）。为打破循环依赖
（games/<game>/spec.py import 本包的引擎 → 触发本 __init__ → 若 import registry
→ registry import games → 循环），本 __init__ 对 registry 转发符号
（GAME_*/GAME_LABELS/is_registered/normalize_game_id/run_session）采用延迟导入：
通过 __getattr__ 在首次访问时才取，此时 games 包已完整加载。
"""
from bzplat.backend.engine.cards import Card, Deck, compare_hands, evaluate, evaluate_5
from bzplat.backend.engine.game import (
    BIG_BLIND,
    DEFAULT_HANDS,
    SMALL_BLIND,
    STARTING_STACK,
    Action,
    GameEngine,
    HandResult,
    MatchResult,
    MatchSession,
    Street,
)
from bzplat.backend.engine.gomoku import BOARD_SIZE, GomokuResult, GomokuSession, check_win
from bzplat.backend.engine.pencil import DEFAULT_N, PencilBoard, PencilResult, PencilSession

# registry 转发符号延迟导入（打破 games ↔ engine 循环依赖）：
# 访问 engine.GAME_HOLDEM / GAME_LABELS / run_session 等时才触发。
_LAZY = {
    "GAME_HOLDEM", "GAME_GOMOKU", "GAME_PENCIL", "GAME_LABELS",
    "is_registered", "normalize_game_id", "run_session",
}


def __getattr__(name: str):
    if name in _LAZY:
        from bzplat.backend.engine import registry as _reg

        return getattr(_reg, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _LAZY)


__all__ = [
    "Card",
    "Deck",
    "evaluate",
    "evaluate_5",
    "compare_hands",
    "Action",
    "Street",
    "GameEngine",
    "MatchSession",
    "HandResult",
    "MatchResult",
    "STARTING_STACK",
    "SMALL_BLIND",
    "BIG_BLIND",
    "DEFAULT_HANDS",
    "GomokuSession",
    "GomokuResult",
    "BOARD_SIZE",
    "check_win",
    "PencilSession",
    "PencilResult",
    "PencilBoard",
    "DEFAULT_N",
    "GAME_HOLDEM",
    "GAME_GOMOKU",
    "GAME_PENCIL",
    "GAME_LABELS",
    "is_registered",
    "normalize_game_id",
    "run_session",
]
