"""点格棋 GameSpec——引擎/协议/配置/模板统一声明。

引擎/协议/结果已物理迁入本包（games/pencil/）。protocol.py 只公开
点格棋 API，棋类同构 JSON 原语由公开的 _board_protocol.py 唯一实现。
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import GameSpec, ProtocolSpec
from bzplat.backend.games import _botzone_protocol as botzone
from bzplat.backend.games.pencil.engine import DEFAULT_N, PencilSession
from bzplat.backend.games.pencil import protocol as proto
from bzplat.backend.games.pencil import templates as _templates_mod

GAME_ID = "pencil"


async def _session_factory(decide, *, on_event=None, **params: Any):
    """构造 PencilSession 并 run_async。

    点阵边长固定 DEFAULT_N（6）——游戏规则钉死，不接受 match_config 配置。
    runner 的通用内部 rng 可以传入但本游戏不消费；其他键一律报错。
    """
    unexpected = set(params) - {"rng"}
    if unexpected:
        fields = ", ".join(sorted(unexpected))
        raise TypeError(f"点格棋 Session 不接受参数: {fields}")
    session = PencilSession(
        n_dots=DEFAULT_N,
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
        raise ValueError("点格棋规则固定，match_config 不允许包含字段")
    return {}


def _normalize_delta(delta: int) -> float:
    return float(delta)


def _progress_from_events(events: list[dict[str, Any]]) -> int:
    return sum(1 for event in events if event.get("type") == "move")


def _eta_for_match(match_config: dict[str, Any]) -> int:
    _validate_match_params(match_config)
    # pencil ETA 固定（N=6 钉死 → 25 格 标定 60s）
    return 60


async def _preflight_check(
    binary_path: str,
    binary_runner: Any,
    *,
    runtime_mode: str,
    timeout: float = 8.0,
) -> tuple[bool, str]:
    """按所选模式发送 canonical 首回合并验证点格棋坐标。"""
    from bzplat.backend.runtime.binary_runner import BotCrashedError, PlatformRunnerError
    import asyncio

    req = proto.build_pencil_request(x=-1, y=-1, pass_=0, me=0, scores=[0, 0])
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
        return True, f"响应合法: ({x},{y})"
    except PlatformRunnerError:
        raise
    except BotCrashedError as exc:
        return False, f"Bot 进程异常退出: {exc}"
    except asyncio.TimeoutError:
        return False, (
            f"ELF 已在沙箱中启动，但 {timeout}s 内没有按 Botzone JSON 首回合协议响应。"
            "请让程序读取 requests/responses JSON 信封，输出 "
            '{"response":{"x":x,"y":y}}，末尾换行并立即 flush。'
            "旧 SAU 的 name?/new/move/take 文本协议与本平台不兼容。"
            "修复步骤：Bot 开发指南 → 上传预检（/wiki?slug=bot-dev）。"
        )
    except Exception as e:
        return False, f"响应不合法: {e}"


SPEC = GameSpec(
    game_id=GAME_ID,
    label="点格棋",
    session_factory=_session_factory,
    protocol=_PROTOCOL,
    default_match_params={},  # 点阵边长钉死 DEFAULT_N（6），无对局级可配参数
    validate_match_params=_validate_match_params,
    normalize_delta=_normalize_delta,
    progress_from_events=_progress_from_events,
    eta_for_match=_eta_for_match,
    templates=_templates_mod.TEMPLATES,
    default_scoring="ccgc_2_1_0",
    code_path="bzplat/backend/games/pencil/engine.py",
    summary="N=6 点阵（对齐 Botzone grid_size=11 交错→25 格）；红先；占相邻边围格得分并连走；先到多数格（13）或终局格多者胜。",
    preflight_check=_preflight_check,
    time_budget_per_side=900.0,  # 象棋钟：每方累计 15 分钟
    shared_source_files=("_board_protocol.py",),
)
