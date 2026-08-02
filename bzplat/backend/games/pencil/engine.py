"""点格棋引擎（对齐 Botzone Pencil 官方 C++ 裁判）。

规则（对齐权威裁判 grid_size=11 交错维度）：
- N×N 点（默认 N=6 → 交错 size=2N-1=11 → (N-1)²=25 格，奇数无平局）
- 红先（seat 0）；占相邻边；围成格得分并连走；**先到多数格（⌈boxes/2⌉）立即胜**；
  全部占完则格多者胜。
- 非法着 / 超时 / 崩溃 → **对手 2-0 胜**（scores 归一化，对齐裁判）。

长驻行协议（语义对齐 Botzone `{x,y,pass}`）：
  请求: {"v":1,"t":"mv","x":int,"y":int,"pass":0|1,"me":0|1,"scores":[r,b]}
    - 红方首手 x=y=-1, pass=0
    - pass=1 时必须响应 {"x":-1,"y":-1}（对方得分连走）
  响应: {"x":int,"y":int}

归属追踪（前端着色用）：edge_owner 记每条已占边的玩家；box_owner 记每个已闭合格
的归属。move 事件带 closed_boxes（本手新闭合格 + owner）；match_end 带 box_owners 网格。
"""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass, field
from typing import Any, Callable

from bzplat.backend.games.pencil.result import MatchResult, RoundResult
from bzplat.backend.games.pencil import protocol as proto
from bzplat.backend.runtime.binary_runner import BotCrashedError

DEFAULT_N = 6  # 点数边长（对齐 Botzone grid_size=11 交错 → 6 点 → 25 格）
DecideFn = Callable[[int, dict[str, Any]], Any]
EventFn = Callable[[str, dict[str, Any]], Any]

GRID_DOT = 3
GRID_EDGE = 4
GRID_EDGE_USED = 5
GRID_BOX = 2
_DIRS = ((-1, 0), (1, 0), (0, -1), (0, 1))


class PencilBoard:
    """交错网格：偶偶=点，奇偶/偶奇=边，奇奇=格心。追踪 edge/box 归属。"""

    def __init__(self, n_dots: int = DEFAULT_N) -> None:
        self.n_dots = n_dots
        self.size = 2 * n_dots - 1
        self.board = [[0] * self.size for _ in range(self.size)]
        self.scores = [0, 0]
        self.curr_player = 0
        # 归属追踪（前端着色用）：edge_owner[(x,y)]=player；box_owner[(bx,by)]=player
        self.edge_owner: dict[tuple[int, int], int] = {}
        self.box_owner: dict[tuple[int, int], int] = {}
        for x in range(self.size):
            for y in range(self.size):
                if x % 2 == 0 and y % 2 == 0:
                    self.board[x][y] = GRID_DOT
                elif (x + y) % 2 == 1:
                    self.board[x][y] = GRID_EDGE
                else:
                    self.board[x][y] = GRID_BOX

    def in_board(self, x: int, y: int) -> bool:
        return 0 <= x < self.size and 0 <= y < self.size

    def is_legal_edge(self, x: int, y: int) -> bool:
        return self.in_board(x, y) and self.board[x][y] == GRID_EDGE

    def legal_actions(self) -> list[tuple[int, int]]:
        out: list[tuple[int, int]] = []
        for x in range(self.size):
            for y in range(self.size):
                if self.board[x][y] == GRID_EDGE:
                    out.append((x, y))
        return out

    def remaining_edges(self) -> int:
        return sum(1 for row in self.board for c in row if c == GRID_EDGE)

    def _update_boxes(self, x: int, y: int) -> list[tuple[int, int]]:
        """检查 (x,y) 边四邻格心是否成格；返回本手新闭合的格心坐标列表。"""
        closed: list[tuple[int, int]] = []
        for dx, dy in _DIRS:
            bx, by = x + dx, y + dy
            if not (self.in_board(bx, by) and self.board[bx][by] == GRID_BOX):
                continue
            if (bx, by) in self.box_owner:
                continue  # 已闭合
            n = 0
            for ddx, ddy in _DIRS:
                ex, ey = bx + ddx, by + ddy
                if self.in_board(ex, ey) and self.board[ex][ey] == GRID_EDGE_USED:
                    n += 1
            if n == 4:
                self.scores[self.curr_player] += 1
                self.box_owner[(bx, by)] = self.curr_player
                closed.append((bx, by))
        return closed

    def do_action(self, x: int, y: int) -> list[tuple[int, int]]:
        """占边；返回本手新闭合的格心坐标列表（空=未得分）。调用前须校验合法。"""
        self.board[x][y] = GRID_EDGE_USED
        self.edge_owner[(x, y)] = self.curr_player
        return self._update_boxes(x, y)

    def box_owners_grid(self) -> list[list[int]]:
        """格心归属网格：-1 未占，0 红，1 蓝（前端着色用）。非格心位置为 -2（忽略）。"""
        grid = [[-2] * self.size for _ in range(self.size)]
        for x in range(self.size):
            for y in range(self.size):
                if self.board[x][y] == GRID_BOX:
                    grid[x][y] = self.box_owner.get((x, y), -1)
        return grid

    def min_win(self) -> int:
        """多数胜阈值：⌈boxes/2⌉（对齐裁判 hasPlayerWon）。6 点→25 格→13。"""
        boxes = (self.n_dots - 1) ** 2
        return boxes // 2 + 1


@dataclass
class PencilSession:
    n_dots: int = DEFAULT_N
    on_event: EventFn | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

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
        g = PencilBoard(self.n_dots)
        await self._emit_async(
            "match_start",
            game_id="pencil",
            n_dots=self.n_dots,
            size=g.size,
            first=0,
            scores=[0, 0],
        )

        to_move = 0
        last_x, last_y = -1, -1
        pass_flag = 0
        winner: int | None = None
        reason = "completed"
        moves_n = 0
        max_boxes = (self.n_dots - 1) ** 2
        min_win = g.min_win()
        # 最终 scores（正常终局用实时分；非法/崩溃归一化 2-0）
        final_scores: list[int] | None = None

        while g.remaining_edges() > 0 or pass_flag == 1:
            # 若边已尽且非 pass 回合，结束
            if pass_flag == 0 and g.remaining_edges() == 0:
                break

            req = proto.build_pencil_request(
                x=last_x,
                y=last_y,
                pass_=pass_flag,
                me=to_move,
                scores=list(g.scores),
            )
            await self._emit_async(
                "turn",
                player=to_move,
                pass_=pass_flag,
                last={"x": last_x, "y": last_y},
                scores=list(g.scores),
            )
            try:
                raw = await self._decide(decide, to_move, req)
            except BotCrashedError:
                # 对齐裁判：bot 崩溃不可恢复 → 判负 2-0（不再中止整场）。
                winner = 1 - to_move
                reason = "crash"
                await self._emit_async(
                    "illegal", player=to_move, move={"x": None, "y": None}, why="crash"
                )
                break
            except Exception:
                winner = 1 - to_move
                reason = "error"
                await self._emit_async(
                    "illegal", player=to_move, move={"x": None, "y": None}, why="error"
                )
                break

            mx, my = proto.parse_xy(raw)

            if pass_flag == 1:
                if mx != -1 or my != -1:
                    winner = 1 - to_move
                    reason = "illegal"
                    await self._emit_async(
                        "illegal", player=to_move, move={"x": mx, "y": my}, why="pass"
                    )
                    break
                # 对方连走：pass 后把回合交回连走方
                await self._emit_async("pass", player=to_move)
                to_move = 1 - to_move
                pass_flag = 0
                last_x, last_y = -1, -1
                continue

            if mx is None or my is None or not g.is_legal_edge(mx, my):
                winner = 1 - to_move
                reason = "illegal"
                await self._emit_async(
                    "illegal",
                    player=to_move,
                    move={"x": mx, "y": my},
                    why="illegal_move",
                )
                break

            g.curr_player = to_move
            closed = g.do_action(mx, my)
            scored = len(closed) > 0
            moves_n += 1
            await self._emit_async(
                "move",
                player=to_move,
                x=mx,
                y=my,
                scored=scored,
                scores=list(g.scores),
                move_index=moves_n,
                closed_boxes=[{"x": bx, "y": by, "owner": g.box_owner[(bx, by)]} for bx, by in closed],
            )

            # 多数胜提前结束（对齐裁判 hasPlayerWon）
            if g.scores[to_move] >= min_win:
                winner = to_move
                reason = "majority"
                break

            if sum(g.scores) >= max_boxes:
                # 全部格子已占完
                break

            if scored:
                # 通知对方 pass，再由本方连走
                last_x, last_y = mx, my
                pass_flag = 1
                to_move = 1 - to_move
            else:
                last_x, last_y = mx, my
                pass_flag = 0
                to_move = 1 - to_move

        # 计算最终 scores（非法/崩溃归一化 2-0，对齐裁判）
        if winner is not None and reason in ("illegal", "error", "crash"):
            final_scores = [2, 0] if winner == 0 else [0, 2]
        else:
            final_scores = list(g.scores)

        if winner is None:
            sa, sb = final_scores
            if sa > sb:
                winner = 0
                reason = "score"
            elif sb > sa:
                winner = 1
                reason = "score"
            else:
                winner = None
                reason = "draw"

        deltas = [final_scores[0] - final_scores[1], final_scores[1] - final_scores[0]]
        round_result = RoundResult(
            winners=[winner] if winner is not None else [],
            deltas=deltas,
        )
        await self._emit_async(
            "match_end",
            game_id="pencil",
            winner=winner,
            reason=reason,
            scores=list(final_scores),
            moves=moves_n,
            box_owners=g.box_owners_grid(),
        )
        return MatchResult(
            rounds_played=moves_n,
            rounds=[round_result],
            events=self.events,
            winner=winner,
            reason=reason,
            scores=list(final_scores),
            moves=moves_n,
        )

    def run(self, decide: DecideFn) -> MatchResult:
        return asyncio.run(self.run_async(decide))
