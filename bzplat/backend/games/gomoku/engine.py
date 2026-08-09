"""五子棋引擎（适配层）——对齐 Botzone Gomoku。

本文件是**平台协议适配层**：调 decide → 经 protocol 构造请求/解析响应 → 驱动纯裁判
（gomoku_judge.py）做规则判定 → emit 平台事件 → 返回 MatchResult。

纯游戏规则（棋盘/合法着/连五/计分）在 gomoku_judge.py，0 平台依赖，可独立审计。
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from bzplat.backend.games.gomoku.result import MatchResult, RoundResult
from bzplat.backend.games.gomoku import protocol as proto
from bzplat.backend.games.gomoku.gomoku_judge import (
    BOARD_SIZE,
    in_board,
    check_win,
    board_full,
    is_legal_move,
    new_board,
    compute_scores,
    compute_deltas,
)
from bzplat.backend.runtime.binary_runner import (
    BotCrashedError,
    BotTechnicalError,
    PlatformRunnerError,
)

DecideFn = Callable[[int, dict[str, Any]], Any]
EventFn = Callable[[str, dict[str, Any]], Any]


@dataclass
class GomokuSession:
    """单局五子棋：decide(player_idx, request) → {"x","y"}。"""

    size: int = BOARD_SIZE
    on_event: EventFn | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def _emit(self, kind: str, **payload: Any) -> None:
        ev = {"type": kind, **payload}
        self.events.append(ev)
        if self.on_event is not None:
            out = self.on_event(kind, ev)
            if inspect.isawaitable(out):
                # sync path ignores; async path awaits in run_async
                pass

    async def _emit_async(self, kind: str, **payload: Any) -> None:
        ev = {"type": kind, **payload}
        self.events.append(ev)
        if self.on_event is not None:
            out = self.on_event(kind, ev)
            if inspect.isawaitable(out):
                await out

    async def _decide(
        self, decide: DecideFn, player: int, request: dict[str, Any]
    ) -> dict[str, Any]:
        out = decide(player, request)
        if inspect.isawaitable(out):
            out = await out
        return out if isinstance(out, dict) else {}

    async def run_async(self, decide: DecideFn) -> MatchResult:
        size = self.size
        board = new_board(size)
        moves: list[dict[str, int]] = []
        await self._emit_async(
            "match_start",
            game_id="gomoku",
            size=size,
            first=0,
        )

        to_move = 0  # 黑
        last_x, last_y = -1, -1
        winner: int | None = None
        reason = "draw"
        moves_n = 0

        while True:
            req = proto.build_gomoku_request(x=last_x, y=last_y, me=to_move)
            await self._emit_async(
                "turn",
                player=to_move,
                last={"x": last_x, "y": last_y},
            )
            try:
                raw = await self._decide(decide, to_move, req)
            except PlatformRunnerError:
                raise
            except BotTechnicalError:
                raise
            except BotCrashedError:
                # 对齐权威裁判：bot 崩溃不可恢复 → 判负（对手赢），不中止整场。
                winner = 1 - to_move
                reason = "crash"
                await self._emit_async(
                    "illegal", player=to_move, move={"x": None, "y": None}, why="crash"
                )
                break
            except Exception:
                winner = 1 - to_move
                reason = "error"
                break

            mx, my = proto.parse_xy(raw)
            if not is_legal_move(board, mx, my, size):
                winner = 1 - to_move
                reason = "illegal"
                await self._emit_async(
                    "illegal",
                    player=to_move,
                    move={"x": mx, "y": my},
                )
                break

            board[mx][my] = to_move
            moves.append({"x": mx, "y": my, "p": to_move})
            moves_n += 1
            await self._emit_async(
                "move",
                player=to_move,
                x=mx,
                y=my,
                move_index=moves_n,
            )

            if check_win(board, mx, my, to_move, size):
                winner = to_move
                reason = "five"
                break
            if board_full(board):
                winner = None
                reason = "draw"
                break

            last_x, last_y = mx, my
            to_move = 1 - to_move

        scores = compute_scores(winner)
        deltas = compute_deltas(winner)

        round_result = RoundResult(
            winners=[winner] if winner is not None else [],
            deltas=deltas,
        )
        await self._emit_async(
            "match_end",
            game_id="gomoku",
            winner=winner,
            reason=reason,
            scores=scores,
            moves=moves_n,
            board=[[board[x][y] for y in range(size)] for x in range(size)],
        )
        return MatchResult(
            rounds_played=moves_n,
            rounds=[round_result],
            events=self.events,
            winner=winner,
            reason=reason,
            scores=scores,
            moves=moves_n,
            board_grid=[[board[x][y] for y in range(size)] for x in range(size)],
        )

    def run(self, decide: DecideFn) -> MatchResult:
        return asyncio.run(self.run_async(decide))
