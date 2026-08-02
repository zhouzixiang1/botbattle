"""向后兼容 shim（全面解耦 PR4）——转发到 _compat。

引擎/协议/结果/段位已物理迁入 games/<game>/ 包。本包（engine/）保留仅为兼容
现存 ``from bzplat.backend.engine import X`` / ``from bzplat.backend.engine.<m> import X``。
转发逻辑集中在 _compat/，本 __init__ 一行 re-export 自 _compat。
"""
# 引擎实现类与常量（经 _compat 转发到各 games/<game>/engine.py）
from bzplat.backend._compat.engine_cards import Card, Deck, compare_hands, evaluate, evaluate_5
from bzplat.backend._compat.engine_game import (
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
from bzplat.backend._compat.engine_gomoku import BOARD_SIZE, GomokuResult, GomokuSession, check_win
from bzplat.backend._compat.engine_pencil import DEFAULT_N, PencilBoard, PencilResult, PencilSession
# registry 转发符号延迟导入（打破 games ↔ engine 循环依赖）：访问 engine.GAME_HOLDEM /
# GAME_LABELS / run_session 等时才触发。
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
    "Card", "Deck", "evaluate", "evaluate_5", "compare_hands",
    "Action", "Street", "GameEngine", "MatchSession", "HandResult", "MatchResult",
    "STARTING_STACK", "SMALL_BLIND", "BIG_BLIND", "DEFAULT_HANDS",
    "GomokuSession", "GomokuResult", "BOARD_SIZE", "check_win",
    "PencilSession", "PencilResult", "PencilBoard", "DEFAULT_N",
    "GAME_HOLDEM", "GAME_GOMOKU", "GAME_PENCIL", "GAME_LABELS",
    "is_registered", "normalize_game_id", "run_session",
]
