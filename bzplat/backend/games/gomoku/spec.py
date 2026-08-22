"""五子棋 GameSpec——引擎/协议/配置/模板统一声明。

引擎/协议/结果已物理迁入本包（games/gomoku/）。``protocol.py`` 只公开
五子棋 v2 分阶段动作 API；旧坐标协议与 Pencil 的共享坐标原语不再适用。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, ProtocolSpec
from bzplat.backend.games import _botzone_protocol as botzone
from bzplat.backend.games.gomoku.engine import BOARD_SIZE, GomokuSession
from bzplat.backend.games.gomoku import protocol as proto
from bzplat.backend.games.gomoku.record import build_record
from bzplat.backend.games.gomoku import templates as _templates_mod

GAME_ID = "gomoku"


async def _session_factory(decide, *, on_event=None, **params: Any):
    """构造 GomokuSession 并 run_async。

    棋盘边长固定 BOARD_SIZE（15）——游戏规则钉死，不接受 match_config/board_size 配置。
    runner 的通用内部 rng 可以传入但本游戏不消费；其他键一律报错。
    """
    unexpected = set(params) - {"rng"}
    if unexpected:
        fields = ", ".join(sorted(unexpected))
        raise TypeError(f"五子棋 Session 不接受参数: {fields}")
    session = GomokuSession(
        size=BOARD_SIZE,
        on_event=on_event,
    )
    return await session.run_async(decide)


_PROTOCOL = ProtocolSpec(
    dumps_request=proto.dumps_request,
    loads_response=proto.loads_response,
    validate_response_payload=proto.validate_response_payload,
    fail_response=proto.fail_response,
)


def _validate_match_params(cfg: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(cfg, dict):
        raise ValueError("match_config 必须是对象")
    if cfg:
        raise ValueError("五子棋规则固定，match_config 不允许包含字段")
    return {}


def _normalize_delta(delta: int) -> float:
    return float(delta)


def _progress_from_events(events: list[dict[str, Any]]) -> int:
    return sum(1 for event in events if event.get("type") == "move")


def _eta_for_match(match_config: dict[str, Any]) -> int:
    _validate_match_params(match_config)
    # 两个座位各有 900s 累计棋钟；ETA 取最坏棋钟上界。
    return 1_800


async def _preflight_check(
    binary_path: str,
    binary_runner: Any,
    *,
    runtime_mode: str,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """按所选模式发送 canonical 首回合并验证五子棋坐标。"""
    from bzplat.backend.runtime.binary_runner import BotCrashedError, PlatformRunnerError
    import asyncio

    from bzplat.backend.games.gomoku.gomoku_judge import new_board

    req = proto.build_request(
        phase=proto.PHASE_OPENING,
        me=0,
        color=0,
        board=new_board(),
        seat_colors=[0, 1],
    )
    try:
        payload = await botzone.preflight_exchange(
            binary_path,
            binary_runner,
            req,
            proto.validate_response_payload,
            runtime_mode=runtime_mode,
            timeout=timeout,
        )
        if payload.get("action") != proto.ACTION_OPENING:
            return False, "首回合必须提交 opening 动作"
        from bzplat.backend.games.gomoku.gomoku_judge import validate_opening

        white2 = payload["white2"]
        black3 = payload["black3"]
        opening = validate_opening(
            (white2["x"], white2["y"]),
            (black3["x"], black3["y"]),
            payload["n"],
        )
        if opening is None:
            return False, "指定开局不属于合法 26 类，或五手候选数不是固定值 2"
        return True, f"v2 指定开局响应合法: {opening}"
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
    label="五子棋",
    ruleset_id=proto.RULESET_ID,
    protocol_version="gomoku_action_v2",
    rating_pool_id="gomoku_ccgc_2013_five_move_two_rating_v2",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={},
    validate_match_params=_validate_match_params,
    normalize_delta=_normalize_delta,
    progress_from_events=_progress_from_events,
    eta_for_match=_eta_for_match,
    templates=_templates_mod.TEMPLATES,
    default_scoring="ccgc_2_1_0",
    code_path="bzplat/backend/games/gomoku/engine.py",
    summary="15×15；26 种指定开局、三手交换、五手二打；黑方三三/四四/长连禁手；每方累计 15 分钟。",
    preflight_check=_preflight_check,
    source_files=("gomoku_judge.py", "forbidden.py", "engine.py", "protocol.py", "result.py"),
    time_budget_per_side=900.0,
    record_exporter=build_record,
)
