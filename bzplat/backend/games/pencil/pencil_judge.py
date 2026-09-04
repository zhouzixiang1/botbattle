"""点格棋纯裁判程序（游戏规则，0 平台依赖）。

本站唯一现行规则：
- 6×6 点阵 → (N-1)²=25 格，交错坐标维度 size=2N-1=11
- 占相邻边；围成格得分并连走
- 先到多数格（⌈25/2⌉=13）立即胜
- 全部占完则格多者胜（奇数格 25 无平局）
- 非法着/超时/崩溃 → 对手胜（比分归一化 2-0）

本纯裁判模块不解析或生成通信协议数据。

只管游戏规则：交错网格、占边、成格连走、多数胜、归属追踪。
不 import protocol/result/engine/orchestrator/runner —— 可独立审计/复用/单测。

适配层（engine.py PencilSession）调用本模块做规则判定，自己做协议/事件/decide。
"""
from __future__ import annotations

DEFAULT_N = 6  # 点数边长；交错坐标维度 11，共 25 格

GRID_DOT = 3
GRID_EDGE = 4
GRID_EDGE_USED = 5
GRID_BOX = 2
_DIRS = ((-1, 0), (1, 0), (0, -1), (0, 1))


def max_timed_decisions(n_dots: int = DEFAULT_N) -> int:
    """Return the strict request-count upper bound for one normal game.

    Every edge consumes one move request.  After a scoring move the protocol
    also asks the opponent to acknowledge one forced ``pass``; the final box
    ends the game before that acknowledgement, so at most ``boxes - 1`` pass
    requests are timed.  For N=6 this is 60 edge moves + 24 passes = 84.
    """
    if isinstance(n_dots, bool) or not isinstance(n_dots, int) or n_dots < 2:
        raise ValueError("n_dots 必须是至少 2 的整数")
    edges = 2 * n_dots * (n_dots - 1)
    boxes = (n_dots - 1) ** 2
    return edges + boxes - 1


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


__all__ = [
    "DEFAULT_N",
    "GRID_DOT",
    "GRID_EDGE",
    "GRID_EDGE_USED",
    "GRID_BOX",
    "PencilBoard",
    "max_timed_decisions",
]
