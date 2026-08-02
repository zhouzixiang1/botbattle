"""黑白棋（Reversi / Othello）引擎。

规则：8×8；黑先（seat 0）；初始棋盘中心 4 子（白在 (3,3)/(4,4)，黑在 (3,4)/(4,3)）。
落子须在空格且至少夹住一条对方连线（被夹的连线全部翻转为己方）。无合法手则 pass；
双方均无合法手或棋盘满 → 终局，子多者胜。

非法着 / 超时 / 异常 → 判负。行协议复用 games/_board_protocol.py（与五子棋同 {x,y} 格式）。

这是「零改动新增游戏」承诺的验证游戏（第 4 款）：规则与 holdem/gomoku/pencil 完全不同，
但通用层（runner/orchestrator/contests/store）零改动即可支持。
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from bzplat.backend.games._board_protocol import (
    build_xy_response,
    dumps_request,
    loads_response,
    parse_xy,
)
from bzplat.backend.games.reversi.result import MatchResult, RoundResult
from bzplat.backend.runtime.binary_runner import BotCrashedError

BOARD_SIZE = 8
DecideFn = Callable[[int, dict[str, Any]], Any]
EventFn = Callable[[str, dict[str, Any]], Any]

# 8 方向（夹击检测用）
_DIRS = ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1))


def _in_board(x: int, y: int, size: int = BOARD_SIZE) -> bool:
    return 0 <= x < size and 0 <= y < size


def _new_board(size: int = BOARD_SIZE) -> list[list[int]]:
    """初始棋盘：中心 4 子。黑=0，白=1，空=-1。标准 Othello 开局。"""
    b = [[-1 for _ in range(size)] for _ in range(size)]
    mid = size // 2
    b[mid - 1][mid - 1] = 1  # 白
    b[mid][mid] = 1  # 白
    b[mid - 1][mid] = 0  # 黑
    b[mid][mid - 1] = 0  # 黑
    return b


def _flips_for_move(
    board: list[list[int]], x: int, y: int, player: int, size: int = BOARD_SIZE
) -> list[tuple[int, int]]:
    """若 player 在 (x,y) 落子，返回会被翻转的对方子坐标列表；空=非合法手。"""
    if not _in_board(x, y, size) or board[x][y] != -1:
        return []
    opponent = 1 - player
    flips: list[tuple[int, int]] = []
    for dx, dy in _DIRS:
        line: list[tuple[int, int]] = []
        cx, cy = x + dx, y + dy
        while _in_board(cx, cy, size) and board[cx][cy] == opponent:
            line.append((cx, cy))
            cx += dx
            cy += dy
        # 该方向须以己方子收尾才有效（夹住）
        if line and _in_board(cx, cy, size) and board[cx][cy] == player:
            flips.extend(line)
    return flips


def legal_moves(board: list[list[int]], player: int, size: int = BOARD_SIZE) -> list[tuple[int, int]]:
    """player 的全部合法手坐标。"""
    out: list[tuple[int, int]] = []
    for x in range(size):
        for y in range(size):
            if board[x][y] == -1 and _flips_for_move(board, x, y, player, size):
                out.append((x, y))
    return out


def count_discs(board: list[list[int]], size: int = BOARD_SIZE) -> tuple[int, int]:
    """(黑子数, 白子数)。"""
    black = sum(1 for x in range(size) for y in range(size) if board[x][y] == 0)
    white = sum(1 for x in range(size) for y in range(size) if board[x][y] == 1)
    return black, white


@dataclass
class ReversiSession:
    """单局黑白棋：decide(player_idx, request) → {"x","y"}。

    行协议：请求 {x,y,me}（对方上一手 / 首手 x=y=-1）；响应 {x,y}。
    无合法手时 bot 应回 {"x":-1,"y":-1}（引擎据 board 判定 pass）。
    """

    size: int = BOARD_SIZE
    on_event: EventFn | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    def _emit(self, kind: str, **payload: Any) -> None:
        ev = {"type": kind, **payload}
        self.events.append(ev)
        if self.on_event is not None:
            self.on_event(kind, ev)

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
        board = _new_board(size)
        moves: list[dict[str, int]] = []
        await self._emit_async(
            "match_start",
            game_id="reversi",
            size=size,
            first=0,
        )

        to_move = 0  # 黑先
        last_x, last_y = -1, -1
        winner: int | None = None
        reason = "draw"
        moves_n = 0
        consecutive_passes = 0

        while True:
            # 当前方无合法手 → pass（不计 moves_n）
            if not legal_moves(board, to_move, size):
                consecutive_passes += 1
                await self._emit_async("pass", player=to_move)
                if consecutive_passes >= 2:
                    # 双方连续 pass → 终局
                    break
                to_move = 1 - to_move
                continue
            consecutive_passes = 0

            # 请求格式：{v,t,x,y,me}（x,y=对方上一手，首手 -1,-1）
            req = {"v": 1, "t": "mv", "x": last_x, "y": last_y, "me": to_move}
            await self._emit_async(
                "turn",
                player=to_move,
                last={"x": last_x, "y": last_y},
            )
            try:
                raw = await self._decide(decide, to_move, req)
            except BotCrashedError:
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

            mx, my = parse_xy(raw)
            flips = _flips_for_move(board, mx, my, to_move, size) if mx is not None and my is not None else []
            if not flips:
                # 非法手（越界 / 已占 / 不夹击）→ 判负
                winner = 1 - to_move
                reason = "illegal"
                await self._emit_async(
                    "illegal", player=to_move, move={"x": mx, "y": my}
                )
                break

            # 落子 + 翻转
            board[mx][my] = to_move
            for fx, fy in flips:
                board[fx][fy] = to_move
            moves.append({"x": mx, "y": my, "p": to_move})
            moves_n += 1
            await self._emit_async(
                "move",
                player=to_move,
                x=mx,
                y=my,
                move_index=moves_n,
                closed_boxes=[{"x": fx, "y": fy, "owner": to_move} for fx, fy in flips],
            )

            # 棋盘满 → 终局
            if all(board[x][y] != -1 for x in range(size) for y in range(size)):
                break

            last_x, last_y = mx, my
            to_move = 1 - to_move

        # 终局按子数判胜
        black, white = count_discs(board, size)
        if winner is None:
            if black > white:
                winner = 0
                reason = "score"
            elif white > black:
                winner = 1
                reason = "score"
            else:
                winner = None
                reason = "draw"
        scores = [black, white]
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
            game_id="reversi",
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
            board=[[board[x][y] for y in range(size)] for x in range(size)],
        )

    def run(self, decide: DecideFn) -> MatchResult:
        return asyncio.run(self.run_async(decide))
