"""按 game_id 路由对战引擎。

全面解耦（PR1）：本模块的 if-chain 已删除，统一委托给 ``bzplat.backend.games``
注册表。保留本模块仅为向后兼容现存 import（``from bzplat.backend.engine.registry
import run_session/GAME_*/normalize_game_id/is_registered``）。PR4 会把转发逻辑
迁到独立的 ``_compat/`` 层。

注意：``run_session`` 仍保留旧的具名参数（num_hands/n_dots/board_size/
starting_stack/sb/bb）以兼容 runner.py 的调用——这些参数按游戏透传给 spec.session_factory。
"""
from __future__ import annotations

from typing import Any, Callable

from bzplat.backend.engine.result import MatchResult  # noqa: F401  (向后兼容 re-export)
from bzplat.backend.games import (
    GAME_GOMOKU,
    GAME_HOLDEM,
    GAME_PENCIL,
    normalize_game_id,
    registry,
)

EventFn = Callable[[str, dict[str, Any]], Any]


def is_registered(game_id: str) -> bool:
    """引擎是否已注册（委托注册表）。"""
    return registry.is_registered(game_id)


# GAME_LABELS 从注册表派生（取代旧手写字典）——单一真相。
GAME_LABELS: dict[str, str] = {gid: registry.get(gid).label for gid in registry.all_ids()}


async def run_session(
    game_id: str,
    decide,
    *,
    num_hands: int | None = None,
    on_event=None,
    rng=None,
    n_dots: int | None = None,
    board_size: int | None = None,
    starting_stack: int | None = None,
    sb: int | None = None,
    bb: int | None = None,
) -> MatchResult:
    """统一入口：按 game_id 取 spec 并 run_session。

    未知 game_id：旧行为是静默兜底跑 holdem；新行为是经 registry.get 抛 KeyError。
    为保持向后兼容（runner/orchestrator 传入已 normalize 的 gid），这里仍用
    normalize_game_id（空值兜底 holdem）；但 normalize 后若不在注册表则报错（修正）。
    """
    gid = normalize_game_id(game_id)
    params: dict[str, object] = {}
    if num_hands is not None:
        params["num_hands"] = num_hands
    if n_dots is not None:
        params["n_dots"] = n_dots
    if board_size is not None:
        params["board_size"] = board_size
    if starting_stack is not None:
        params["starting_stack"] = starting_stack
    if sb is not None:
        params["sb"] = sb
    if bb is not None:
        params["bb"] = bb
    if rng is not None:
        params["rng"] = rng
    return await registry.get(gid).run_session(decide, on_event=on_event, **params)
