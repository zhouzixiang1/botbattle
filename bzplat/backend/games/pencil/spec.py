"""点格棋 GameSpec——引擎/协议/配置/段位/模板统一声明。

PR4：引擎/协议/结果/段位已物理迁入本包（games/pencil/），不再共享基类。
与 gomoku 各自独立 protocol.py 副本（不共享 board_protocol）。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, JudgeParamSpec, ProtocolSpec
from bzplat.backend.games.pencil.engine import DEFAULT_N, PencilSession
from bzplat.backend.games.pencil import protocol as proto
from bzplat.backend.games.pencil import tiers as _tiers_mod
from bzplat.backend.games.pencil import templates as _templates_mod

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


def _eta_for_match(match_config: dict[str, Any]) -> int:
    # pencil ETA ∝ 格数（基准 N=6 → 25 格 标定 60s，按 (n_dots-1)² 缩放）
    n_dots = int(match_config.get("n_dots", DEFAULT_N) or DEFAULT_N)
    boxes = (n_dots - 1) ** 2
    return max(30, int(boxes / 25 * 60))


SPEC = GameSpec(
    game_id=GAME_ID,
    label="点格棋",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={"n_dots": DEFAULT_N},
    validate_match_params=_validate_match_params,
    rounds_per_match=_rounds_per_match,
    normalize_earnings=_normalize_earnings,
    eta_per_match_sec=60.0,  # N=6/25 格基准（随 n_dots 缩放）
    eta_for_match=_eta_for_match,
    judge_params=[],  # n_dots 走 match 列，非全局 setting
    tiers=_tiers_mod.TIERS,
    tier_for=_tiers_mod.tier_for,
    templates=_templates_mod.TEMPLATES,
    default_scoring="ccgc_2_1_0",
    code_path="bzplat/backend/games/pencil/engine.py",
    summary="N=6 点阵（对齐 Botzone grid_size=11 交错→25 格）；红先；占相邻边围格得分并连走；先到多数格（13）或终局格多者胜。",
    frontend_module="@/games/pencil",
)
