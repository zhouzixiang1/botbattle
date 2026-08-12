"""五子棋 GameSpec——引擎/协议/配置/模板统一声明。

引擎/协议/结果已物理迁入本包（games/gomoku/）。protocol.py 只公开
五子棋 API，棋类同构 JSON 原语由公开的 _board_protocol.py 唯一实现。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, ProtocolSpec
from bzplat.backend.games import _botzone_protocol as botzone
from bzplat.backend.games.gomoku.engine import BOARD_SIZE, GomokuSession
from bzplat.backend.games.gomoku import protocol as proto
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
    fail_response=lambda: {"x": -99, "y": -99},  # 人类超时等游戏内兜底
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
    # gomoku 单局固定 ETA（无可调参数）
    return 60


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

    req = proto.build_gomoku_request(x=-1, y=-1, me=0)
    try:
        payload = await botzone.preflight_exchange(
            binary_path,
            binary_runner,
            req,
            proto.validate_response_payload,
            runtime_mode=runtime_mode,
            timeout=timeout,
        )
        x, y = payload["x"], payload["y"]
        if not (0 <= x < 15 and 0 <= y < 15):
            return False, f"坐标越界: ({x},{y})"
        return True, f"响应合法: ({x},{y})"
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
    summary="15×15；黑先；横竖斜连续≥5 即胜；无禁手。",
    preflight_check=_preflight_check,
    shared_source_files=("_board_protocol.py",),
)
