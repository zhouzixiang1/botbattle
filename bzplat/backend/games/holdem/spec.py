"""德州扑克 GameSpec——把引擎/协议/配置/段位/模板统一声明成一个对象。

PR1 阶段：引擎/协议文件仍留原位（engine/game.py、protocol/json_protocol.py），
本 spec 用 import 引用它们；PR4 会物理迁移到 games/holdem/ 包内。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, JudgeParamSpec, ProtocolSpec, TierDef
from bzplat.backend.engine.game import (
    BIG_BLIND,
    DEFAULT_HANDS,
    MatchSession,
    SMALL_BLIND,
    STARTING_STACK,
)
from bzplat.backend.protocol import json_protocol as proto
from bzplat.backend.store.schema import (
    SETTING_JUDGE_HOLDEM_BB,
    SETTING_JUDGE_HOLDEM_HANDS,
    SETTING_JUDGE_HOLDEM_SB,
    SETTING_JUDGE_HOLDEM_STACK,
)

GAME_ID = "holdem"


async def _session_factory(decide, *, on_event=None, **params: Any):
    """构造 MatchSession 并 run_async。params 含 num_hands/starting_stack/sb/bb/rng。"""
    session = MatchSession(
        num_hands=params.get("num_hands", DEFAULT_HANDS),
        starting_stack=params.get("starting_stack") or STARTING_STACK,
        sb=params.get("sb") or SMALL_BLIND,
        bb=params.get("bb") or BIG_BLIND,
        rng=params.get("rng"),
        on_event=on_event,
    )
    return await session.run_async(decide)


_PROTOCOL = ProtocolSpec(
    dumps_request=proto.dumps_request,
    loads_response=proto.loads_response,
    fail_response=lambda: {"a": "f"},  # 扑克超时兜底：fold
)


def _validate_match_params(cfg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        raise ValueError("match_config 必须是对象")
    hands = cfg.get("hands", DEFAULT_HANDS)
    if not isinstance(hands, int) or not (1 <= hands <= 500):
        raise ValueError(f"holdem match_config.hands 须为 1–500 的整数（得到 {hands}）")
    return {"hands": hands}


def _rounds_per_match(match_config: dict[str, Any]) -> int:
    return int(match_config.get("hands", DEFAULT_HANDS))


def _normalize_earnings(ea: int) -> float:
    # 德州筹码以"大盲注"为单位展示（bb/100）：除 100
    return ea / 100.0


# 德州段位曲线（per-game；与全局 tiers.py 历史阈值一致，后续可独立调整）
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


# 德州赛事模板（PR2 会从 contests/templates.py 迁入；PR1 先空，避免循环依赖）
# PR1 阶段 templates 暂为空列表，PR2/PR5 落位。

SPEC = GameSpec(
    game_id=GAME_ID,
    label="德州扑克",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={"hands": DEFAULT_HANDS},
    validate_match_params=_validate_match_params,
    rounds_per_match=_rounds_per_match,
    normalize_earnings=_normalize_earnings,
    eta_per_match_sec=140.0,  # ~2s/手 × 70 手
    judge_params=[
        JudgeParamSpec(SETTING_JUDGE_HOLDEM_STACK, "起始筹码", "starting_stack",
                       STARTING_STACK, (1000, 1_000_000)),
        JudgeParamSpec(SETTING_JUDGE_HOLDEM_SB, "小盲注", "sb",
                       SMALL_BLIND, (1, 10_000)),
        JudgeParamSpec(SETTING_JUDGE_HOLDEM_BB, "大盲注", "bb",
                       BIG_BLIND, (2, 20_000)),
        JudgeParamSpec(SETTING_JUDGE_HOLDEM_HANDS, "挑战默认手数", "default_hands",
                       DEFAULT_HANDS, (1, 1000)),
    ],
    tiers=_TIERS,
    tier_for=_tier_for,
    templates=[],  # PR2 迁入
    default_scoring="poker_3_1_0",
    code_path="bzplat/backend/engine/game.py",
    summary="HU NLHE；单局多手；按筹码差判胜。",
    frontend_module="@/games/holdem",
)
