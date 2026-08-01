"""对局执行：BinaryRunner ×2 + 按 game_id 路由引擎。"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from bzplat.backend.engine.game import DEFAULT_HANDS
from bzplat.backend.engine.registry import (
    GAME_HOLDEM,
    normalize_game_id,
    run_session,
)
# 全面解耦：runner 不再按 game_id 切协议模块，统一委托 games 注册表。
from bzplat.backend.games import dumps as _reg_dumps, fail_response as _reg_fail, loads as _reg_loads
from bzplat.backend.engine.result import MatchResult  # 共享基类（PR4 拆 per-game）
from bzplat.backend.runtime.binary_runner import BinaryRunner, BotCrashedError, DEFAULT_ACTION_TIMEOUT

logger = logging.getLogger(__name__)

EventSink = Callable[[str, dict[str, Any]], None]


def _dumps(game_id: str, request: dict[str, Any]) -> str:
    return _reg_dumps(game_id, request)


def _loads(game_id: str, line: str) -> dict[str, Any]:
    return _reg_loads(game_id, line)


def _fail_response(game_id: str) -> dict[str, Any]:
    """超时/异常时的兜底响应（按游戏：扑克 fold；棋类非法坐标）。"""
    return _reg_fail(game_id)


class MatchRunner:
    def __init__(
        self,
        runner: BinaryRunner | None = None,
        *,
        action_timeout: float = DEFAULT_ACTION_TIMEOUT,
    ) -> None:
        self.runner = runner or BinaryRunner()
        self.action_timeout = action_timeout

    async def run_binaries(
        self,
        path_a: str,
        path_b: str,
        *,
        game_id: str = GAME_HOLDEM,
        num_hands: int = DEFAULT_HANDS,
        n_dots: int | None = None,
        board_size: int | None = None,
        starting_stack: int | None = None,
        sb: int | None = None,
        bb: int | None = None,
        on_event: EventSink | None = None,
        seed: int | None = None,
    ) -> MatchResult:
        import random

        gid = normalize_game_id(game_id)
        sid_a = await self.runner.start_session(path_a)
        try:
            sid_b = await self.runner.start_session(path_b)
        except BaseException:
            # 第二个 session 启动失败（如 BotCrashedError）时，必须释放已启动的第一个，
            # 否则其容器/进程会泄漏（finally 只保护下方 try 块，不覆盖这两个 start_session）。
            await self.runner.stop_session(sid_a)
            raise
        try:
            rng = random.Random(seed) if seed is not None else random.Random()

            async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                sid = sid_a if player_idx == 0 else sid_b
                line = _dumps(gid, request)
                try:
                    resp_line = await self.runner.send(
                        sid, line, timeout=self.action_timeout
                    )
                    return _loads(gid, resp_line)
                except BotCrashedError:
                    # Bot 进程已死，不可恢复——向上传播触发对局 abort（而非吞成默认动作死磕）
                    raise
                except Exception as exc:
                    logger.warning("bot %s decide failed: %s", player_idx, exc)
                    return _fail_response(gid)

            return await run_session(
                gid, decide,
                num_hands=num_hands, n_dots=n_dots,
                board_size=board_size, starting_stack=starting_stack, sb=sb, bb=bb,
                on_event=on_event, rng=rng,
            )
        finally:
            await self.runner.stop_session(sid_a)
            await self.runner.stop_session(sid_b)

    async def run_bot_vs_human(
        self,
        bot_path: str,
        *,
        bot_seat: int,
        human_decide,
        game_id: str = GAME_HOLDEM,
        num_hands: int = DEFAULT_HANDS,
        n_dots: int | None = None,
        board_size: int | None = None,
        starting_stack: int | None = None,
        sb: int | None = None,
        bb: int | None = None,
        on_event: EventSink | None = None,
        seed: int | None = None,
    ) -> MatchResult:
        """Bot vs 人类：bot 侧走 BinaryRunner，人类侧走 human_decide 协程。

        bot_seat 为 bot 坐位（0/1）；人类坐另一侧。human_decide(player_idx, request)
        由调用方实现（通常经 asyncio.Future 等待 WS 回传），超时由其内部处理。
        """
        import random

        gid = normalize_game_id(game_id)
        sid_bot = await self.runner.start_session(bot_path)
        try:
            rng = random.Random(seed) if seed is not None else random.Random()

            async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                if player_idx == bot_seat:
                    line = _dumps(gid, request)
                    try:
                        resp_line = await self.runner.send(
                            sid_bot, line, timeout=self.action_timeout
                        )
                        return _loads(gid, resp_line)
                    except BotCrashedError:
                        # Bot 进程已死——向上传播触发对局 abort
                        raise
                    except Exception as exc:
                        logger.warning("bot %s decide failed: %s", player_idx, exc)
                        return _fail_response(gid)
                # 人类侧
                out = human_decide(player_idx, request)
                if inspect.isawaitable(out):
                    out = await out
                return out if isinstance(out, dict) else _fail_response(gid)

            return await run_session(
                gid, decide,
                num_hands=num_hands, n_dots=n_dots,
                board_size=board_size, starting_stack=starting_stack, sb=sb, bb=bb,
                on_event=on_event, rng=rng,
            )
        finally:
            await self.runner.stop_session(sid_bot)

    async def run_callables(
        self,
        decide_a,
        decide_b,
        *,
        game_id: str = GAME_HOLDEM,
        num_hands: int = DEFAULT_HANDS,
        n_dots: int | None = None,
        board_size: int | None = None,
        starting_stack: int | None = None,
        sb: int | None = None,
        bb: int | None = None,
        on_event: EventSink | None = None,
        seed: int | None = None,
    ) -> MatchResult:
        import random

        gid = normalize_game_id(game_id)
        rng = random.Random(seed) if seed is not None else random.Random()

        async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
            fn = decide_a if player_idx == 0 else decide_b
            out = fn(request)
            if hasattr(out, "__await__"):
                out = await out
            return out

        return await run_session(
            gid, decide,
            num_hands=num_hands, n_dots=n_dots,
            board_size=board_size, starting_stack=starting_stack, sb=sb, bb=bb,
            on_event=on_event, rng=rng,
        )
