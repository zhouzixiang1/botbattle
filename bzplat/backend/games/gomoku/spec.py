"""五子棋 GameSpec——引擎/协议/配置/段位/模板统一声明。

PR4：引擎/协议/结果/段位已物理迁入本包（games/gomoku/），不再共享基类。
与 pencil 各自独立 protocol.py 副本（不共享 board_protocol）。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, ProtocolSpec
from bzplat.backend.games.gomoku.engine import BOARD_SIZE, GomokuSession
from bzplat.backend.games.gomoku import protocol as proto
from bzplat.backend.games.gomoku import tiers as _tiers_mod
from bzplat.backend.games.gomoku import templates as _templates_mod

GAME_ID = "gomoku"


async def _session_factory(decide, *, on_event=None, **params: Any):
    """构造 GomokuSession 并 run_async。

    棋盘边长固定 BOARD_SIZE（15）——游戏规则钉死，不接受 match_config/board_size 配置。
    """
    session = GomokuSession(
        size=BOARD_SIZE,
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


async def _preflight_check(binary_path: str, binary_runner: Any, *, timeout: float = 8.0) -> tuple[bool, str]:
    """Bot 预检：发五子棋首手请求（黑方 x=y=-1），验证响应含合法坐标。"""
    from bzplat.backend.games._board_protocol import build_gomoku_request, dumps_request, loads_response, parse_xy
    from bzplat.backend.runtime.binary_runner import BotCrashedError
    import asyncio

    req = build_gomoku_request(x=-1, y=-1, me=0)
    line = dumps_request(req)
    try:
        sid = await binary_runner.start_session(binary_path)
    except Exception as e:
        return False, f"启动失败: {e}"
    try:
        resp_line = await binary_runner.send(sid, line, timeout=timeout)
        resp = loads_response(resp_line) if isinstance(resp_line, str) else resp_line
        x, y = parse_xy(resp)
        if x is None or y is None:
            return False, f"响应缺 x/y 坐标: {resp_line}"
        if not (0 <= x < 15 and 0 <= y < 15):
            return False, f"坐标越界: ({x},{y})"
        return True, f"响应合法: ({x},{y})"
    except BotCrashedError:
        return False, "Bot 启动后立即退出（stdout EOF）——不符合长驻协议"
    except asyncio.TimeoutError:
        return False, f"Bot {timeout}s 内未响应"
    except Exception as e:
        return False, f"响应不合法: {e}"
    finally:
        await binary_runner.stop_session(sid)


SPEC = GameSpec(
    game_id=GAME_ID,
    label="五子棋",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={},
    validate_match_params=_validate_match_params,
    rounds_per_match=_rounds_per_match,
    normalize_earnings=_normalize_earnings,
    eta_for_match=_eta_for_match,
    judge_params=[],  # 棋盘边长钉死 BOARD_SIZE（15），无 admin 可调项
    tiers=_tiers_mod.TIERS,
    templates=_templates_mod.TEMPLATES,
    default_scoring="ccgc_2_1_0",
    code_path="bzplat/backend/games/gomoku/engine.py",
    summary="15×15；黑先；横竖斜连续≥5 即胜；无禁手。",
    preflight_check=_preflight_check,
)
