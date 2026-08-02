"""按 game_id 路由对战引擎（转发层，委托 games 注册表）。

全面解耦（PR1）：本模块的 if-chain 已删除，统一委托给 ``bzplat.backend.games``
注册表。保留本模块仅为向后兼容现存 import（``from bzplat.backend.engine.registry
import run_session/GAME_*/normalize_game_id/is_registered``）。PR4 会把转发逻辑
迁到独立的 ``_compat/`` 层。

**循环依赖处理**：games/<game>/spec.py 会 import engine.<game>（引擎实现），
故 engine 包的 __init__ 在加载 engine.game 时会触发本模块；而本模块若在顶部
import games 就会形成 games→spec→engine→__init__→registry→games 的循环。
因此本模块对 games 的 import 全部延迟到函数体内（运行时才取，此时 games 包已
完整加载）。
"""
from __future__ import annotations

from typing import Any, Callable

from bzplat.backend.engine.result import MatchResult  # noqa: F401  (向后兼容 re-export)

EventFn = Callable[[str, dict[str, Any]], Any]

# GAME_* 常量本应从 games 派生，但为打破循环依赖这里保留字面量（与 games 一致，
# 由 games 包启动断言 + test_game_registry.test_schema_frozensets_match_registry 保证不漂移）。
GAME_HOLDEM = "holdem"
GAME_GOMOKU = "gomoku"
GAME_PENCIL = "pencil"


def _registry():
    """延迟取 games 注册表单例（避免模块顶部循环 import）。"""
    from bzplat.backend.games import registry as _reg

    return _reg


def normalize_game_id(game_id: str | None) -> str:
    """旧 normalize_game_id 语义：空值兜底 holdem。"""
    from bzplat.backend.games import normalize_game_id as _norm

    return _norm(game_id)


def is_registered(game_id: str) -> bool:
    """引擎是否已注册（委托注册表）。"""
    return _registry().is_registered(game_id)


def _build_game_labels() -> dict[str, str]:
    reg = _registry()
    return {gid: reg.get(gid).label for gid in reg.all_ids()}


# GAME_LABELS 延迟构建：首次访问时从注册表派生。用 __getattr__ 模块级钩子。
def __getattr__(name: str):
    if name == "GAME_LABELS":
        return _build_game_labels()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


async def run_session(
    game_id: str,
    decide,
    *,
    on_event=None,
    rng=None,
    **params: Any,
) -> MatchResult:
    """统一入口：按 game_id 取 spec 并 run_session。

    游戏规则参数（num_hands/n_dots/board_size/starting_stack/sb/bb/...）经 **params
    透传给 spec.run_session——新增第 4 游戏带新参数（如 komi）无需改本签名。
    各参数的意义/默认值/校验全在 GameSpec（default_match_params / judge_params）里声明。

    未知 game_id：旧行为是静默兜底跑 holdem；新行为是经 registry.get 抛 KeyError。
    为保持向后兼容（runner/orchestrator 传入已 normalize 的 gid），这里仍用
    normalize_game_id（空值兜底 holdem）；但 normalize 后若不在注册表则报错（修正）。
    """
    gid = normalize_game_id(game_id)
    if rng is not None:
        params["rng"] = rng
    return await _registry().get(gid).run_session(decide, on_event=on_event, **params)
