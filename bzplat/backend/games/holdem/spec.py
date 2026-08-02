"""德州扑克 GameSpec——把引擎/协议/配置/段位/模板统一声明成一个对象。

PR4：引擎/协议/结果/段位已物理迁入本包（games/holdem/），不再共享基类。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, JudgeParamSpec, ProtocolSpec
from bzplat.backend.games.holdem.engine import (
    BIG_BLIND,
    DEFAULT_HANDS,
    MatchSession,
    SMALL_BLIND,
    STARTING_STACK,
)
from bzplat.backend.games.holdem import protocol as proto
from bzplat.backend.games.holdem import tiers as _tiers_mod
from bzplat.backend.games.holdem import templates as _templates_mod
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


def _eta_for_match(match_config: dict[str, Any]) -> int:
    # holdem ETA ∝ 手数（每手约 2s）
    hands = int(match_config.get("hands", DEFAULT_HANDS) or DEFAULT_HANDS)
    return hands * 2


async def _preflight_check(binary_path: str, binary_runner: Any, *, timeout: float = 8.0) -> tuple[bool, str]:
    """Bot 预检：发一个德州 act 请求，验证响应含合法 action。"""
    from bzplat.backend.games.holdem.protocol import build_act_request, parse_response, dumps_request
    from bzplat.backend.runtime.binary_runner import BotCrashedError
    import asyncio

    # 构造最小 act 请求（preflop，seat 0/SB，需 call 50）
    from bzplat.backend.games.holdem.cards import Card
    req = build_act_request(
        hand=0, total_hands=1, my_id=0, dealer_or_sb=0,
        my_cards=[Card(0, 0), Card(1, 0)], board=[], hist=[],
        my_chips=19950, opp_chips=19900, sb=50, bb=100, to_call=50,
    )
    line = dumps_request(req)
    try:
        sid = await binary_runner.start_session(binary_path)
    except Exception as e:
        return False, f"启动失败: {e}"
    try:
        resp_line = await binary_runner.send(sid, line, timeout=timeout)
        parse_response(resp_line)  # 抛 ValueError = 不合法
        return True, "响应合法"
    except BotCrashedError:
        return False, "Bot 启动后立即退出（stdout EOF）——不符合长驻协议"
    except asyncio.TimeoutError:
        return False, f"Bot {timeout}s 内未响应"
    except (ValueError, Exception) as e:
        return False, f"响应不合法: {e}"
    finally:
        await binary_runner.stop_session(sid)


SPEC = GameSpec(
    game_id=GAME_ID,
    label="德州扑克",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={"hands": DEFAULT_HANDS},
    validate_match_params=_validate_match_params,
    rounds_per_match=_rounds_per_match,
    normalize_earnings=_normalize_earnings,
    eta_for_match=_eta_for_match,
    judge_params=[
        JudgeParamSpec(SETTING_JUDGE_HOLDEM_STACK, "起始筹码", "starting_stack",
                       STARTING_STACK, (1000, 1_000_000)),
        JudgeParamSpec(SETTING_JUDGE_HOLDEM_SB, "小盲注", "sb",
                       SMALL_BLIND, (1, 10_000)),
        JudgeParamSpec(SETTING_JUDGE_HOLDEM_BB, "大盲注", "bb",
                       BIG_BLIND, (2, 20_000)),
        # field="num_hands" 对齐 session_factory 的 kwarg 名（原 default_hands 接不上 → 静默失效）。
        # orchestrator 优先用此 admin 全局设置，未设时回退 match.total_hands（对局级配置）。
        JudgeParamSpec(SETTING_JUDGE_HOLDEM_HANDS, "挑战默认手数", "num_hands",
                       DEFAULT_HANDS, (1, 1000)),
    ],
    tiers=_tiers_mod.TIERS,
    templates=_templates_mod.TEMPLATES,
    default_scoring="poker_3_1_0",
    code_path="bzplat/backend/games/holdem/engine.py",
    summary="HU NLHE；单局多手；按筹码差判胜。",
    preflight_check=_preflight_check,
)
