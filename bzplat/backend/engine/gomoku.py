"""五子棋引擎（对齐 Botzone Gomoku）。

规则：15×15；黑先（seat 0）；横/竖/斜连续 ≥5 含长连即胜；无禁手。
非法着 / 超时 → 判负。棋盘下满且无人成五 → 平局。

长驻行协议（语义对齐 Botzone `{x,y}`）：
  请求: {"v":1,"t":"mv","x":int,"y":int,"me":0|1}
    - 黑方首手 x=y=-1
    - 之后 x,y 为对方上一手
  响应: {"x":int,"y":int}
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from bzplat.backend.engine.result import MatchResult, RoundResult
from bzplat.backend.protocol import board_protocol as proto

BOARD_SIZE = 15
DecideFn = Callable[[int, dict[str, Any]], Any]
EventFn = Callable[[str, dict[str, Any]], Any]


@dataclass
class GomokuResult(MatchResult):
    """五子棋单局结果（不再借用 holdem 的 HandResult）。"""

    winner: int | None = None
    reason: str = "draw"  # five | draw | illegal | error
    scores: list[int] = field(default_factory=lambda: [0, 0])
    moves: int = 0
    board_grid: list[list[int]] = field(default_factory=list)

# 方向：横、竖、两斜
_DIRS = ((1, 0), (0, 1), (1, 1), (1, -1))


def in_board(x: int, y: int, size: int = BOARD_SIZE) -> bool:
    return 0 <= x < size and 0 <= y < size


def check_win(board: list[list[int]], x: int, y: int, player: int) -> bool:
    """以 (x,y) 为中心，任一方向连续 ≥5 同色即胜。"""
    for dx, dy in _DIRS:
        count = 1
        for sign in (1, -1):
            cx, cy = x + sign * dx, y + sign * dy
            while in_board(cx, cy) and board[cx][cy] == player:
                count += 1
                cx += sign * dx
                cy += sign * dy
        if count >= 5:
            return True
    return False


def board_full(board: list[list[int]]) -> bool:
    return all(cell != -1 for row in board for cell in row)


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
        board = [[-1 for _ in range(size)] for _ in range(size)]
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
            except Exception:
                winner = 1 - to_move
                reason = "error"
                break

            mx, my = proto.parse_xy(raw)
            if mx is None or my is None or not in_board(mx, my, size) or board[mx][my] != -1:
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

            if check_win(board, mx, my, to_move):
                winner = to_move
                reason = "five"
                break
            if board_full(board):
                winner = None
                reason = "draw"
                break

            last_x, last_y = mx, my
            to_move = 1 - to_move

        scores = [0, 0]
        if winner is not None:
            scores[winner] = 1
        deltas = [0, 0]
        if winner == 0:
            deltas = [1, -1]
        elif winner == 1:
            deltas = [-1, 1]

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
        return GomokuResult(
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
