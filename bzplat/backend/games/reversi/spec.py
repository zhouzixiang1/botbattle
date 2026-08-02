"""黑白棋 GameSpec——引擎/协议/配置/段位/模板统一声明。

第 4 款游戏（reversi）：验证「新增游戏 = 通用层零改动」。规则与 holdem/gomoku/pencil
完全不同（夹击翻转），但复用 _board_protocol 行协议 + base tier_for_in 段位算法。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, ProtocolSpec
from bzplat.backend.games.reversi.engine import BOARD_SIZE, ReversiSession
from bzplat.backend.games.reversi import protocol as proto
from bzplat.backend.games.reversi import tiers as _tiers_mod
from bzplat.backend.games.reversi import templates as _templates_mod

GAME_ID = "reversi"


async def _session_factory(decide, *, on_event=None, **params: Any):
    """构造 ReversiSession 并 run_async。params 含 board_size（可选，默认 8）。"""
    session = ReversiSession(
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
    # 黑白棋单局，无可调 match 参数；忽略任何字段
    if not isinstance(cfg, dict):
        raise ValueError("match_config 必须是对象")
    return {}


def _rounds_per_match(match_config: dict[str, Any]) -> int:
    return 1


def _normalize_earnings(ea: int) -> float:
    return float(ea)


def _eta_for_match(match_config: dict[str, Any]) -> int:
    # reversi 单局固定 ETA（无可调参数；8×8 约 60 步，标定 90s）
    return 90


async def _preflight_check(binary_path: str, binary_runner: Any, *, timeout: float = 8.0) -> tuple[bool, str]:
    """Bot 预检：发黑白棋首手请求（黑方 x=y=-1），验证响应含合法坐标。"""
    from bzplat.backend.games._board_protocol import dumps_request, loads_response, parse_xy
    from bzplat.backend.runtime.binary_runner import BotCrashedError
    import asyncio

    req = {"v": 1, "t": "mv", "x": -1, "y": -1, "me": 0}
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
        if not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
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
    label="黑白棋",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={},
    validate_match_params=_validate_match_params,
    rounds_per_match=_rounds_per_match,
    normalize_earnings=_normalize_earnings,
    eta_for_match=_eta_for_match,
    judge_params=[],
    tiers=_tiers_mod.TIERS,
    templates=_templates_mod.TEMPLATES,
    default_scoring="ccgc_2_1_0",
    code_path="bzplat/backend/games/reversi/engine.py",
    summary="8×8；黑先；中心 4 子开局；落子须夹住对方连线并翻转；无合法手 pass；双方均无合法手或棋盘满终局，子多者胜。",
    preflight_check=_preflight_check,
)
