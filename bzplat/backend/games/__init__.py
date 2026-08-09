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


# normalize_game_id：空值兜底 holdem（保旧语义；通用层 import 自本包）
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

    平台规则由各游戏 spec 固定；``params`` 只供引擎内部复现控制（如 rng、
    duplicate deal_sequence），不构成公开可配置规则。
    未知 game_id 抛 KeyError（行为修正：不再静默跑 holdem）。
    """
    return await registry.get(game_id).run_session(decide, on_event=on_event, **params)


def dumps(game_id: str, request: dict[str, Any]) -> str:
    """按游戏序列化 Bot 请求。"""
    return registry.get(game_id).protocol.dumps_request(request)


def loads(game_id: str, line: str) -> dict[str, Any]:
    """按游戏反序列化 Bot 响应。"""
    return registry.get(game_id).protocol.loads_response(line)


def fail_response(game_id: str) -> Any:
    """按游戏返回人类超时等游戏内兜底（Bot 技术故障禁止使用）。"""
    return registry.get(game_id).protocol.fail_response()


def is_registered(game_id: str) -> bool:
    return registry.is_registered(game_id)


def all_ids() -> frozenset[str]:
    return registry.all_ids()


def validate_match_config(game_id: str, cfg: Any) -> dict[str, Any]:
    """校验赛事内部 match_config；现行固定规则只允许空对象。

    ``match_config`` 仍作为数据库内部快照容器存在，但公开规则已经固定，调用者
    不能再通过此入口覆盖手数、棋盘或时限。未知游戏仍由注册表显式拒绝。
    """
    registry.get(game_id)
    if not isinstance(cfg, dict):
        raise ValueError("match_config 必须是 JSON 对象")
    if cfg:
        fields = ", ".join(sorted(str(key) for key in cfg))
        raise ValueError(f"游戏规则已固定，不接受 match_config 字段：{fields}")
    return {}


def default_match_config(game_id: str) -> dict[str, Any]:
    """按游戏返回默认 match_config。"""
    import copy
    return copy.deepcopy(registry.get(game_id).default_match_params)


def game_label(game_id: str) -> str:
    return registry.get(game_id).label


# ── 段位便捷函数（模块级，默认 holdem；取代旧 engine.tiers 全局 API）──
def tier_for(rating: float | int | None, game_id: str = "holdem"):
    """按 rating 查段位（默认 holdem 曲线）。旧 engine.tiers.tier_for 语义。"""
    return registry.tier_for(game_id, rating)


def tier_dict(rating: float | int | None, game_id: str = "holdem") -> dict:
    """段位字典（默认 holdem）。旧 engine.tiers.tier_dict 语义。"""
    return registry.tier_dict(game_id, rating)


def all_tiers(game_id: str = "holdem") -> list[dict]:
    """该游戏的全部段位曲线（默认 holdem）。旧 engine.tiers.all_tiers 语义。"""
    return registry.all_tiers(game_id)


def _build_game_labels() -> dict[str, str]:
    return {gid: registry.get(gid).label for gid in registry.all_ids()}


# GAME_LABELS：首次访问时从注册表派生（取代旧 engine.registry 的延迟 __getattr__）。
# 用模块级 __getattr__ 延迟构建（避免 import 时注册表未完全加载）。
def __getattr__(name: str):
    if name == "GAME_LABELS":
        return _build_game_labels()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | {"GAME_LABELS"})


async def preflight_bot(
    game_id: str,
    binary_path: str,
    binary_runner: Any,
    *,
    runtime_mode: str,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """Bot 预检：按用户选择的运行模式执行正式协议首回合。

    经该游戏的 spec.preflight_check 执行；若 spec 未定义 preflight_check 则跳过（返回 ok）。
    返回 (ok, detail)。ok=False 时上传/API 应拒绝该 bot。
    """
    spec = registry.get(game_id)
    if spec.preflight_check is None:
        return True, "该游戏未定义预检"
    return await spec.preflight_check(
        binary_path,
        binary_runner,
        runtime_mode=runtime_mode,
        timeout=timeout,
    )


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
    "tier_for",
    "tier_dict",
    "all_tiers",
    "GAME_LABELS",
]
