#!/usr/bin/env python3
"""点格棋（Dots and Boxes）参考裁判（独立、无平台依赖）。

Bot 作者可本地运行自测合法着 / 成格计分，逻辑与本平台服务端
`bzplat/backend/games/pencil/engine.py` 的 PencilSession 裁判一致。

规则：固定 N=6：6 点 → 交错 size=2N-1=11 → (N-1)²=25 格，奇数无平局；
红先（seat 0）；
占相邻边；围成格得分并连走；格多者胜；平分则平局。
非法着（非边 / 已占边）→ 判负。

交错网格编码：偶偶=点(GRID_DOT=3)，奇偶/偶奇=边(GRID_EDGE=4)，
已占边(GRID_EDGE_USED=5)，奇奇=格心(GRID_BOX=2)。
"""
from __future__ import annotations

import sys

DEFAULT_N = 6  # 6 点→25 格，奇数格数保证终局有胜负
GRID_DOT = 3
GRID_EDGE = 4
GRID_EDGE_USED = 5
GRID_BOX = 2
_DIRS = ((-1, 0), (1, 0), (0, -1), (0, 1))


class PencilBoard:
    def __init__(self) -> None:
        self.n_dots = DEFAULT_N
        self.size = 2 * DEFAULT_N - 1
        self.board = [[0] * self.size for _ in range(self.size)]
        self.scores = [0, 0]
        self.curr_player = 0
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

    def remaining_edges(self) -> int:
        return sum(1 for row in self.board for c in row if c == GRID_EDGE)

    def _box_completed(self, bx: int, by: int) -> bool:
        """格心 (bx,by) 四周边是否全部已占。"""
        for dx, dy in _DIRS:
            ex, ey = bx + dx, by + dy
            if not (self.in_board(ex, ey) and self.board[ex][ey] == GRID_EDGE_USED):
                return False
        return True

    def do_action(self, x: int, y: int) -> bool:
        """占边 (x,y)；返回是否得分（调用前须校验合法）。"""
        self.board[x][y] = GRID_EDGE_USED
        scored = False
        for dx, dy in _DIRS:
            bx, by = x + dx, y + dy
            if self.in_board(bx, by) and self.board[bx][by] == GRID_BOX:
                if self._box_completed(bx, by):
                    scored = True
                    self.scores[self.curr_player] += 1
        return scored

    def winner(self) -> int | None:
        sa, sb = self.scores
        if sa > sb:
            return 0
        if sb > sa:
            return 1
        return None


def _demo() -> None:
    """固定 N=6，在左上角占第 4 条边形成一格。"""
    g = PencilBoard()
    assert g.size == 11
    assert len([1 for r in g.board for c in r if c == GRID_EDGE]) == 60
    g.curr_player = 0
    assert g.do_action(0, 1) is False  # 上边
    assert g.do_action(1, 0) is False  # 左边
    assert g.do_action(1, 2) is False  # 右边
    assert g.scores == [0, 0]
    assert g.do_action(2, 1) is True   # 下边 → 成格
    assert g.scores == [1, 0]
    print(f"固定 N=6 演示：左上格第 4 边闭合 → scores={g.scores}")


def _interactive() -> None:
    g = PencilBoard()
    print(f"Pencil {g.n_dots}×{g.n_dots}（交错网格 {g.size}×{g.size}），红(0)先")
    to_move = 0
    while g.remaining_edges() > 0:
        who = "红(0)" if to_move == 0 else "蓝(1)"
        try:
            line = input(f"{who} 占边，输入 'x y'：").strip()
        except EOFError:
            break
        parts = line.split()
        if len(parts) != 2:
            print("格式错误"); continue
        try:
            x, y = int(parts[0]), int(parts[1])
        except ValueError:
            print("需为整数"); continue
        if not g.is_legal_edge(x, y):
            print(f"非法着 → {who} 判负"); return
        g.curr_player = to_move
        scored = g.do_action(x, y)
        print(f"  占边 ({x},{y}) → {'得分连走' if scored else '换人'}  scores={g.scores}")
        if not scored:
            to_move = 1 - to_move
    print(f"结束 scores={g.scores} winner={g.winner()}")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _interactive()
    else:
        _demo()
