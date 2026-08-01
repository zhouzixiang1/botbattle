"""点格棋 GameSpec——引擎/协议/配置/段位/模板统一声明。

PR1 阶段：引擎/协议文件仍留原位（engine/pencil.py、protocol/board_protocol.py），
本 spec 用 import 引用；PR4 会物理迁移到 games/pencil/ 包内（与 gomoku 各自独立
protocol.py 副本，不共享）。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, JudgeParamSpec, ProtocolSpec, TierDef
from bzplat.backend.engine.pencil import DEFAULT_N, PencilSession
from bzplat.backend.protocol import board_protocol as proto

GAME_ID = "pencil"


async def _session_factory(decide, *, on_event=None, **params: Any):
    """构造 PencilSession 并 run_async。params 含 n_dots。"""
    session = PencilSession(
        n_dots=params.get("n_dots") or DEFAULT_N,
        on_event=on_event,
    )
    return await session.run_async(decide)


_PROTOCOL = ProtocolSpec(
    dumps_request=proto.dumps_request,
    loads_response=proto.loads_response,
    fail_response=lambda: {"x": -99, "y": -99},  # 棋类超时兜底：非法坐标
)


def _validate_match_params(cfg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        raise ValueError("match_config 必须是对象")
    n_dots = cfg.get("n_dots", DEFAULT_N)
    if not isinstance(n_dots, int) or not (3 <= n_dots <= 15):
        raise ValueError(f"pencil match_config.n_dots 须为 3–15 的整数（得到 {n_dots}）")
    return {"n_dots": n_dots}


def _rounds_per_match(match_config: dict[str, Any]) -> int:
    return 1


def _normalize_earnings(ea: int) -> float:
    return float(ea)


# 点格棋段位曲线（per-game）
_TIERS = [
    TierDef(5, "master", "大师", "text-violet-700", "bg-violet-50", 2200),
    TierDef(4, "expert", "专家", "text-indigo-700", "bg-indigo-50", 2050),
    TierDef(3, "gold", "高手", "text-amber-700", "bg-amber-50", 1900),
    TierDef(2, "silver", "熟练", "text-slate-700", "bg-slate-100", 1750),
    TierDef(1, "bronze", "进阶", "text-emerald-700", "bg-emerald-50", 1600),
    TierDef(0, "novice", "新手", "text-sky-700", "bg-sky-50", 0),
]


def _tier_for(rating: float | int | None) -> TierDef:
    if rating is None:
        return _TIERS[-1]
    r = float(rating)
    for t in _TIERS:
        if r >= t.min_rating:
            return t
    return _TIERS[-1]


SPEC = GameSpec(
    game_id=GAME_ID,
    label="点格棋",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={"n_dots": DEFAULT_N},
    validate_match_params=_validate_match_params,
    rounds_per_match=_rounds_per_match,
    normalize_earnings=_normalize_earnings,
    eta_per_match_sec=90.0,  # 随 n_dots 缩放，此处取中等估算
    judge_params=[],  # n_dots 走 match 列，非全局 setting
    tiers=_TIERS,
    tier_for=_tier_for,
    templates=[],  # PR2 迁入
    default_scoring="ccgc_2_1_0",
    code_path="bzplat/backend/engine/pencil.py",
    summary="N=11 点阵；红先；占相邻边围格得分并连走；格多者胜。",
    frontend_module="@/games/pencil",
)
