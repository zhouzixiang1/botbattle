"""对局执行：BinaryRunner ×2 + MatchSession + 事件回调。"""
from __future__ import annotations

import json
import logging
from typing import Any, Callable

from bzplat.backend.engine.game import MatchSession, MatchResult, DEFAULT_HANDS
from bzplat.backend.protocol import json_protocol as proto
from bzplat.backend.runtime.binary_runner import BinaryRunner, DEFAULT_ACTION_TIMEOUT

logger = logging.getLogger(__name__)

EventSink = Callable[[str, dict[str, Any]], None]


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
        num_hands: int = DEFAULT_HANDS,
        on_event: EventSink | None = None,
        seed: int | None = None,
    ) -> MatchResult:
        import random

        sid_a = await self.runner.start_session(path_a)
        sid_b = await self.runner.start_session(path_b)
        try:
            rng = random.Random(seed) if seed is not None else random.Random()

            async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                sid = sid_a if player_idx == 0 else sid_b
                line = proto.dumps_request(request)
                try:
                    resp_line = await self.runner.send(
                        sid, line, timeout=self.action_timeout
                    )
                    return proto.loads_response(resp_line)
                except Exception as exc:
                    logger.warning("bot %s decide failed: %s", player_idx, exc)
                    return {"a": "f"}

            session = MatchSession(
                num_hands=num_hands, rng=rng, on_event=on_event
            )
            return await session.run_async(decide)
        finally:
            await self.runner.stop_session(sid_a)
            await self.runner.stop_session(sid_b)

    async def run_callables(
        self,
        decide_a,
        decide_b,
        *,
        num_hands: int = DEFAULT_HANDS,
        on_event: EventSink | None = None,
        seed: int | None = None,
    ) -> MatchResult:
        import random

        rng = random.Random(seed) if seed is not None else random.Random()

        async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
            fn = decide_a if player_idx == 0 else decide_b
            out = fn(request)
            if hasattr(out, "__await__"):
                out = await out
            return out

        session = MatchSession(num_hands=num_hands, rng=rng, on_event=on_event)
        return await session.run_async(decide)
