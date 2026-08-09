"""德州扑克 GameSpec——把引擎/协议/配置/段位/模板统一声明成一个对象。

PR4：引擎/协议/结果/段位已物理迁入本包（games/holdem/），不再共享基类。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, ProtocolSpec
from bzplat.backend.games import _botzone_protocol as botzone
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

GAME_ID = "holdem"


async def _session_factory(decide, *, on_event=None, **params: Any):
    """构造 MatchSession 并 run_async。

    手数固定 DEFAULT_HANDS（70）——游戏规则钉死，不接受 match_config 配置
    （曾因 hands/num_hands key 名不一致导致配置静默失效，现彻底移除配置能力）。
    规则常量全部固定；params 仅消费平台内部的 rng/deal_sequence。
    """
    session = MatchSession(
        num_hands=DEFAULT_HANDS,
        starting_stack=STARTING_STACK,
        sb=SMALL_BLIND,
        bb=BIG_BLIND,
        rng=params.get("rng"),
        on_event=on_event,
        deal_sequence=params.get("deal_sequence"),
    )
    return await session.run_async(decide)


_PROTOCOL = ProtocolSpec(
    dumps_request=proto.dumps_request,
    loads_response=proto.loads_response,
    validate_response_payload=proto.validate_response_payload,
    fail_response=proto.fail_response,  # 人类超时等游戏内兜底：fold
)


def _validate_match_params(cfg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        raise ValueError("match_config 必须是对象")
    if cfg:
        raise ValueError("德州扑克规则固定，match_config 不允许包含字段")
    return {}


def _rounds_per_match(match_config: dict[str, Any]) -> int:
    # 手数钉死 70（不再读 match_config）
    return DEFAULT_HANDS


def _normalize_earnings(ea: int) -> float:
    # 德州筹码以"大盲注"为单位展示（bb/100）：除 100
    return ea / 100.0


def _eta_for_match(match_config: dict[str, Any]) -> int:
    # holdem ETA ∝ 手数（每手约 2s）；手数钉死 70
    return DEFAULT_HANDS * 2


def _build_match_plan(seed: int, params: dict[str, Any]) -> list[dict[str, Any]]:
    """P4 duplicate：返回 2 leg（A-seat0/B-seat1 + B-seat0/A-seat1），同 deal_sequence。

    消除运气：同 seed 生成 deal_sequence，两 leg 用它发牌，净筹码相加判胜负。
    seed=None 或 params['duplicate']=False 时返回单 leg（普通赛）。
    """
    if not params.get("duplicate"):
        return [{"seat_swap": False, "params": {**params, "match_seed": seed}}]
    # 手数钉死 DEFAULT_HANDS（不再读 params["num_hands"]）
    from bzplat.backend.games.holdem.engine import generate_deal_sequence
    ds = generate_deal_sequence(DEFAULT_HANDS, seed) if seed is not None else None
    shared = {**params, "deal_sequence": ds, "match_seed": seed}
    return [
        {"seat_swap": False, "params": shared},  # leg1: A=seat0, B=seat1
        {"seat_swap": True, "params": shared},   # leg2: B=seat0, A=seat1（座位对调）
    ]


async def _preflight_check(
    binary_path: str,
    binary_runner: Any,
    *,
    runtime_mode: str,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """按所选模式发送 canonical 首回合并验证德州响应。"""
    from bzplat.backend.games.holdem.protocol import build_act_request
    from bzplat.backend.runtime.binary_runner import BotCrashedError, PlatformRunnerError
    import asyncio

    # 构造最小 act 请求（preflop，seat 0/SB）
    from bzplat.backend.games.holdem.holdem_judge import Card, Suit
    req = build_act_request(
        hand=0, total_hands=1, my_id=0, dealer_id=0,
        my_cards=[Card(Suit.SPADE, 2), Card(Suit.SPADE, 3)], board=[], history=[],
        my_chips=19950,
    )
    try:
        await botzone.preflight_exchange(
            binary_path,
            binary_runner,
            req,
            proto.validate_response_payload,
            runtime_mode=runtime_mode,
            timeout=timeout,
        )
        return True, "响应合法"
    except PlatformRunnerError:
        raise
    except BotCrashedError as exc:
        return False, f"Bot 进程异常退出: {exc}"
    except asyncio.TimeoutError:
        return False, f"Bot {timeout}s 内未响应"
    except Exception as e:
        return False, f"响应不合法: {e}"


SPEC = GameSpec(
    game_id=GAME_ID,
    label="德州扑克",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={},  # 手数钉死 DEFAULT_HANDS，无对局级可配参数
    validate_match_params=_validate_match_params,
    rounds_per_match=_rounds_per_match,
    normalize_earnings=_normalize_earnings,
    eta_for_match=_eta_for_match,
    judge_params=[],
    tiers=_tiers_mod.TIERS,
    templates=_templates_mod.TEMPLATES,
    default_scoring="poker_3_1_0",
    code_path="bzplat/backend/games/holdem/engine.py",
    summary="HU NLHE；单局多手；按筹码差判胜。",
    preflight_check=_preflight_check,
    build_match_plan=_build_match_plan,
)
