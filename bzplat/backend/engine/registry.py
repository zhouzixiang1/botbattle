"""按 game_id 路由对战引擎。"""
from __future__ import annotations

from typing import Any, Callable

from bzplat.backend.engine.game import (
    BIG_BLIND,
    DEFAULT_HANDS,
    MatchResult,
    MatchSession,
    SMALL_BLIND,
    STARTING_STACK,
)
from bzplat.backend.engine.gomoku import BOARD_SIZE, GomokuSession
from bzplat.backend.engine.pencil import DEFAULT_N, PencilSession
from bzplat.backend.store.schema import REGISTERED_ENGINES

EventFn = Callable[[str, dict[str, Any]], Any]

GAME_HOLDEM = "holdem"
GAME_GOMOKU = "gomoku"
GAME_PENCIL = "pencil"

GAME_LABELS = {
    GAME_HOLDEM: "德州扑克",
    GAME_GOMOKU: "五子棋",
    GAME_PENCIL: "点格棋",
}


def is_registered(game_id: str) -> bool:
    return game_id in REGISTERED_ENGINES


def normalize_game_id(game_id: str | None) -> str:
    gid = (game_id or GAME_HOLDEM).strip().lower()
    return gid if gid else GAME_HOLDEM


async def run_session(
    game_id: str,
    decide,
    *,
    num_hands: int = DEFAULT_HANDS,
    on_event: EventFn | None = None,
    rng=None,
    n_dots: int = DEFAULT_N,
    board_size: int | None = None,
    starting_stack: int | None = None,
    sb: int | None = None,
    bb: int | None = None,
) -> MatchResult:
    """统一入口：按 game_id 构造 Session 并 run_async(decide)。

    规则参数（board_size / starting_stack / sb / bb）传 None 时由各引擎
    常量兜底（BOARD_SIZE / STARTING_STACK / SMALL_BLIND / BIG_BLIND）。
    """
    gid = normalize_game_id(game_id)
    if gid == GAME_GOMOKU:
        return await GomokuSession(
            size=board_size or BOARD_SIZE, on_event=on_event
        ).run_async(decide)
    if gid == GAME_PENCIL:
        return await PencilSession(n_dots=n_dots or DEFAULT_N, on_event=on_event).run_async(decide)
    # holdem（默认）
    session = MatchSession(
        num_hands=num_hands,
        starting_stack=starting_stack or STARTING_STACK,
        sb=sb or SMALL_BLIND,
        bb=bb or BIG_BLIND,
        rng=rng,
        on_event=on_event,
    )
    return await session.run_async(decide)
