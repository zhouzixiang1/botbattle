"""德州扑克 GameSpec——把引擎/协议/配置/模板统一声明成一个对象。

引擎/协议/结果已物理迁入本包（games/holdem/），不再共享基类。
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
from bzplat.backend.games.holdem import templates as _templates_mod

GAME_ID = "holdem"


async def _session_factory(decide, *, on_event=None, **params: Any):
    """构造 MatchSession 并 run_async。

    手数固定 DEFAULT_HANDS（70）——游戏规则钉死，不接受 match_config 配置
    （曾因 hands/num_hands key 名不一致导致配置静默失效，现彻底移除配置能力）。
    规则常量全部固定；params 仅接受平台内部的 rng/deal_sequence。任何其他键
    都是错误调用，必须显式失败，不能静默按固定规则继续执行。
    """
    unexpected = set(params) - {"rng", "deal_sequence"}
    if unexpected:
        fields = ", ".join(sorted(unexpected))
        raise TypeError(f"德州扑克 Session 不接受参数: {fields}")
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


def _normalize_delta(delta: int) -> float:
    """将筹码分差换算为大盲注。

    这是整场累计分差的单位换算，不是每 100 手统计量。
    """
    return delta / BIG_BLIND


def _progress_from_events(events: list[dict[str, Any]]) -> int:
    return sum(1 for event in events if event.get("type") == "settle")


def _eta_for_match(match_config: dict[str, Any]) -> int:
    _validate_match_params(match_config)
    # holdem ETA ∝ 手数（每手约 2s）；手数钉死 70
    return DEFAULT_HANDS * 2


def _build_match_plan(seed: int, params: dict[str, Any]) -> list[dict[str, Any]]:
    """返回两场同牌换座的 70 手独立计分计划；普通模式返回一场。"""
    unexpected = set(params) - {"duplicate"}
    if unexpected:
        fields = ", ".join(sorted(unexpected))
        raise ValueError(f"复式赛计划不接受参数: {fields}")
    if not params.get("duplicate"):
        return [{"seat_swap": False, "params": {}}]
    # 手数钉死 DEFAULT_HANDS（不再读 params["num_hands"]）
    from bzplat.backend.games.holdem.engine import generate_deal_sequence
    ds = generate_deal_sequence(DEFAULT_HANDS, seed) if seed is not None else None
    shared = {"deal_sequence": ds}
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
        hand=0, total_hands=DEFAULT_HANDS, my_id=0, dealer_id=0,
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
    ruleset_id="holdem_hu_nlhe_allin_v2",
    protocol_version="holdem_action_v1",
    rating_pool_id="holdem_allin_rating_v2",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={},  # 手数钉死 DEFAULT_HANDS，无对局级可配参数
    validate_match_params=_validate_match_params,
    normalize_delta=_normalize_delta,
    progress_from_events=_progress_from_events,
    eta_for_match=_eta_for_match,
    templates=_templates_mod.TEMPLATES,
    default_scoring="poker_3_1_0",
    fixed_rounds_per_match=DEFAULT_HANDS,
    code_path="bzplat/backend/games/holdem/engine.py",
    summary="HU NLHE；每个计分场固定 70 手；按本场筹码差判胜；复式两场独立计分。",
    preflight_check=_preflight_check,
    build_match_plan=_build_match_plan,
    contest_games_per_pair_max=10,
)
