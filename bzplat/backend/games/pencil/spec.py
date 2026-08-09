"""点格棋 GameSpec——引擎/协议/配置/段位/模板统一声明。

PR4：引擎/协议/结果/段位已物理迁入本包（games/pencil/），不再共享基类。
与 gomoku 各自独立 protocol.py 副本（不共享 board_protocol）。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, JudgeParamSpec, ProtocolSpec
from bzplat.backend.games.pencil.engine import DEFAULT_N, PencilSession
from bzplat.backend.games.pencil import protocol as proto
from bzplat.backend.games.pencil import tiers as _tiers_mod
from bzplat.backend.games.pencil import templates as _templates_mod

GAME_ID = "pencil"


async def _session_factory(decide, *, on_event=None, **params: Any):
    """构造 PencilSession 并 run_async。

    点阵边长固定 DEFAULT_N（6）——游戏规则钉死，不接受 match_config 配置。
    """
    session = PencilSession(
        n_dots=DEFAULT_N,
        on_event=on_event,
    )
    return await session.run_async(decide)


_PROTOCOL = ProtocolSpec(
    dumps_request=proto.dumps_request,
    loads_response=proto.loads_response,
    fail_response=lambda: {"x": -99, "y": -99},  # 棋类超时兜底：非法坐标
)


def _validate_match_params(cfg: dict[str, Any]) -> dict[str, Any]:
    # 点阵边长已钉死 DEFAULT_N（6），不再接受配置；忽略任何传入字段。
    if not isinstance(cfg, dict):
        raise ValueError("match_config 必须是对象")
    return {}


def _rounds_per_match(match_config: dict[str, Any]) -> int:
    return 1


def _normalize_earnings(ea: int) -> float:
    return float(ea)


def _eta_for_match(match_config: dict[str, Any]) -> int:
    # pencil ETA 固定（N=6 钉死 → 25 格 标定 60s）
    return 60


async def _preflight_check(binary_path: str, binary_runner: Any, *, timeout: float = 8.0) -> tuple[bool, str]:
    """Bot 预检：发点格棋首手请求（红方 x=y=-1 pass=0），验证响应含合法坐标。"""
    from bzplat.backend.games._board_protocol import build_pencil_request, dumps_request, loads_response, parse_xy
    from bzplat.backend.runtime.binary_runner import BotCrashedError, PlatformRunnerError
    import asyncio

    req = build_pencil_request(x=-1, y=-1, pass_=0, me=0, scores=[0, 0])
    line = dumps_request(req)
    try:
        sid = await binary_runner.start_session(binary_path)
    except PlatformRunnerError:
        raise
    except Exception as e:
        return False, f"启动失败: {e}"
    try:
        resp_line = await binary_runner.send(sid, line, timeout=timeout)
        resp = loads_response(resp_line) if isinstance(resp_line, str) else resp_line
        x, y = parse_xy(resp)
        if x is None or y is None:
            return False, f"响应缺 x/y 坐标: {resp_line}"
        return True, f"响应合法: ({x},{y})"
    except PlatformRunnerError:
        raise
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
    label="点格棋",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={},  # 点阵边长钉死 DEFAULT_N（6），无对局级可配参数
    validate_match_params=_validate_match_params,
    rounds_per_match=_rounds_per_match,
    normalize_earnings=_normalize_earnings,
    eta_for_match=_eta_for_match,
    judge_params=[],  # n_dots 走 match 列，非全局 setting
    tiers=_tiers_mod.TIERS,
    templates=_templates_mod.TEMPLATES,
    default_scoring="ccgc_2_1_0",
    code_path="bzplat/backend/games/pencil/engine.py",
    summary="N=6 点阵（对齐 Botzone grid_size=11 交错→25 格）；红先；占相邻边围格得分并连走；先到多数格（13）或终局格多者胜。",
    preflight_check=_preflight_check,
    time_budget_per_side=900.0,  # 象棋钟：每方累计 15 分钟
)
