"""五子棋 GameSpec——引擎/协议/配置/段位/模板统一声明。

PR1 阶段：引擎/协议文件仍留原位（engine/gomoku.py、protocol/board_protocol.py），
本 spec 用 import 引用；PR4 会物理迁移到 games/gomoku/ 包内（与 pencil 各自独立
protocol.py 副本，不共享）。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, JudgeParamSpec, ProtocolSpec, TierDef
from bzplat.backend.engine.gomoku import BOARD_SIZE, GomokuSession
from bzplat.backend.protocol import board_protocol as proto
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


# 五子棋段位曲线（per-game，独立于德州；初始阈值与历史全局一致，可独立调）
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
    label="五子棋",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={},
    validate_match_params=_validate_match_params,
    rounds_per_match=_rounds_per_match,
    normalize_earnings=_normalize_earnings,
    eta_per_match_sec=60.0,
    judge_params=[
        JudgeParamSpec(SETTING_JUDGE_GOMOKU_SIZE, "棋盘边长", "board_size",
                       BOARD_SIZE, (9, 19)),
    ],
    tiers=_TIERS,
    tier_for=_tier_for,
    templates=[],  # PR2 迁入
    default_scoring="ccgc_2_1_0",
    code_path="bzplat/backend/engine/gomoku.py",
    summary="15×15；黑先；横竖斜连续≥5 即胜；无禁手。",
    frontend_module="@/games/gomoku",
)
