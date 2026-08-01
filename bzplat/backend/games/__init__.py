"""游戏注册表入口——单一真相来源。

注册三款游戏并暴露便捷函数。通用层（orchestrator/runner/contests/api_routes/
schema）经本包的 ``registry`` 单例按 game_id 取 spec，绝不 import 具体游戏模块。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import (
    DecideFn,
    EventFn,
    GameRegistry,
    GameSpec,
    JudgeParamSpec,
    ProtocolSpec,
    SessionFactory,
    TierDef,
)
from bzplat.backend.games.gomoku.spec import SPEC as _GOMOKU_SPEC
from bzplat.backend.games.holdem.spec import SPEC as _HOLDEM_SPEC
from bzplat.backend.games.pencil.spec import SPEC as _PENCIL_SPEC

# 全局单例
registry = GameRegistry()
registry.register(_HOLDEM_SPEC)
registry.register(_GOMOKU_SPEC)
registry.register(_PENCIL_SPEC)

# 一致性断言：schema.py 的 REGISTERED_ENGINES / VALID_GAME_IDS 必须与注册表一致。
# schema.py 是纯常量模块（无 import），不能在 import 时从注册表派生（会循环依赖），
# 故保留字面量 frozenset 作为运行时值，并在此断言二者不漂移——注册表是逻辑真相。
from bzplat.backend.store import schema as _schema  # noqa: E402

_reg_ids = registry.all_ids()
assert _reg_ids == _schema.REGISTERED_ENGINES == _schema.VALID_GAME_IDS, (
    f"注册表与 schema 不一致：registry={sorted(_reg_ids)} "
    f"REGISTERED_ENGINES={sorted(_schema.REGISTERED_ENGINES)} "
    f"VALID_GAME_IDS={sorted(_schema.VALID_GAME_IDS)}。"
    "新增游戏须同时改 games/<game>/spec + 注册 + schema 两个 frozenset。"
)

# ── 向后兼容常量（替代 engine/registry.py 的 GAME_* 字面量）──
GAME_HOLDEM = "holdem"
GAME_GOMOKU = "gomoku"
GAME_PENCIL = "pencil"


def _legacy_normalize(game_id: str | None) -> str:
    """旧 normalize_game_id 语义：空值兜底 holdem（保向后兼容）。

    注意：registry.get() 本身不兜底（未知抛 KeyError）；此函数仅供仍需"空=holdem"
    兜底语义的旧调用点用。新代码应直接 registry.get(gid) 让未知显式报错。
    """
    gid = GameRegistry.normalize(game_id)
    return gid if gid else GAME_HOLDEM


# 别名（保 from bzplat.backend.engine.registry import normalize_game_id 可用）
normalize_game_id = _legacy_normalize


# ── 便捷函数（通用层经这些函数调用，而非 import 具体游戏）──
async def run_session(
    game_id: str,
    decide: DecideFn,
    *,
    on_event: EventFn | None = None,
    **params: Any,
) -> Any:
    """统一入口：按 game_id 取 spec 并 run_session。

    规则参数（num_hands/n_dots/board_size/starting_stack/sb/bb/rng）按游戏透传。
    未知 game_id 抛 KeyError（行为修正：不再静默跑 holdem）。
    """
    return await registry.get(game_id).run_session(decide, on_event=on_event, **params)


def dumps(game_id: str, request: dict[str, Any]) -> str:
    """按游戏序列化 Bot 请求。"""
    return registry.get(game_id).protocol.dumps_request(request)


def loads(game_id: str, line: str) -> dict[str, Any]:
    """按游戏反序列化 Bot 响应。"""
    return registry.get(game_id).protocol.loads_response(line)


def fail_response(game_id: str) -> dict[str, Any]:
    """按游戏返回超时/异常兜底响应。"""
    return registry.get(game_id).protocol.fail_response()


def is_registered(game_id: str) -> bool:
    return registry.is_registered(game_id)


def all_ids() -> frozenset[str]:
    return registry.all_ids()


def validate_match_config(game_id: str, cfg: Any) -> dict[str, Any]:
    """按游戏校验并规整 match_config（替代 contests/validation.py 的 if-chain）。"""
    return registry.get(game_id).validate_match_params(cfg)


def default_match_config(game_id: str) -> dict[str, Any]:
    """按游戏返回默认 match_config。"""
    import copy
    return copy.deepcopy(registry.get(game_id).default_match_params)


def game_label(game_id: str) -> str:
    return registry.get(game_id).label


__all__ = [
    "registry",
    "GameRegistry",
    "GameSpec",
    "TierDef",
    "JudgeParamSpec",
    "ProtocolSpec",
    "SessionFactory",
    "GAME_HOLDEM",
    "GAME_GOMOKU",
    "GAME_PENCIL",
    "normalize_game_id",
    "run_session",
    "dumps",
    "loads",
    "fail_response",
    "is_registered",
    "all_ids",
    "validate_match_config",
    "default_match_config",
    "game_label",
]
