"""五子棋 GameSpec——引擎/协议/配置/段位/模板统一声明。

PR4：引擎/协议/结果/段位已物理迁入本包（games/gomoku/），不再共享基类。
与 pencil 各自独立 protocol.py 副本（不共享 board_protocol）。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, JudgeParamSpec, ProtocolSpec
from bzplat.backend.games.gomoku.engine import BOARD_SIZE, GomokuSession
from bzplat.backend.games.gomoku import protocol as proto
from bzplat.backend.games.gomoku import tiers as _tiers_mod
from bzplat.backend.games.gomoku import templates as _templates_mod
from bzplat.backend.store.schema import SETTING_JUDGE_GOMOKU_SIZE

GAME_ID = "gomoku"


async def _session_factory(decide, *, on_event=None, **params: Any):
    """构造 GomokuSession 并 run_async。params 含 board_size。"""
    session = GomokuSession(
        size=params.get("board_size") or BOARD_SIZE,
        on_event=on_event,
    )
    return await session.run_async(decide)


_PROTOCOL = ProtocolSpec(
    dumps_request=proto.dumps_request,
    loads_response=proto.loads_response,
    fail_response=lambda: {"x": -99, "y": -99},  # 棋类超时兜底：非法坐标
)


def _validate_match_params(cfg: dict[str, Any]) -> dict[str, Any]:
    # 五子棋单局，无可调参数；忽略任何字段
    if not isinstance(cfg, dict):
        raise ValueError("match_config 必须是对象")
    return {}


def _rounds_per_match(match_config: dict[str, Any]) -> int:
    return 1


def _normalize_earnings(ea: int) -> float:
    # 棋类 deltas 是胜负（±1/±2），直接透传，不做 bb/100 换算
    return float(ea)


def _eta_for_match(match_config: dict[str, Any]) -> int:
    # gomoku 单局固定 ETA（无可调参数）
    return 60


SPEC = GameSpec(
    game_id=GAME_ID,
    label="五子棋",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={},
    validate_match_params=_validate_match_params,
    rounds_per_match=_rounds_per_match,
    normalize_earnings=_normalize_earnings,
    eta_per_match_sec=60.0,
    eta_for_match=_eta_for_match,
    judge_params=[
        JudgeParamSpec(SETTING_JUDGE_GOMOKU_SIZE, "棋盘边长", "board_size",
                       BOARD_SIZE, (9, 19)),
    ],
    tiers=_tiers_mod.TIERS,
    tier_for=_tiers_mod.tier_for,
    templates=_templates_mod.TEMPLATES,
    default_scoring="ccgc_2_1_0",
    code_path="bzplat/backend/games/gomoku/engine.py",
    summary="15×15；黑先；横竖斜连续≥5 即胜；无禁手。",
    frontend_module="@/games/gomoku",
)
