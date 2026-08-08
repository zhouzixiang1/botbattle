"""五子棋纯裁判程序（游戏规则，0 平台依赖）。

只管游戏规则：棋盘状态、合法着判定、连五判胜、棋盘满判平局、计分。
不 import protocol/result/engine/orchestrator/runner —— 可独立审计/复用/单测。

适配层（engine.py GomokuSession）调用本模块做规则判定，自己做协议/事件/decide。
"""
from __future__ import annotations

BOARD_SIZE = 15

# 方向：横、竖、两斜
_DIRS = ((1, 0), (0, 1), (1, 1), (1, -1))


def in_board(x: int, y: int, size: int = BOARD_SIZE) -> bool:
    """坐标是否在棋盘内。"""
    return 0 <= x < size and 0 <= y < size


def check_win(board: list[list[int]], x: int, y: int, player: int, size: int = BOARD_SIZE) -> bool:
    """以 (x,y) 为中心，任一方向连续 ≥5 同色即胜（含长连，无禁手）。"""
    for dx, dy in _DIRS:
        count = 1
        for sign in (1, -1):
            cx, cy = x + sign * dx, y + sign * dy
            while in_board(cx, cy, size) and board[cx][cy] == player:
                count += 1
                cx += sign * dx
                cy += sign * dy
        if count >= 5:
            return True
    return False


def board_full(board: list[list[int]]) -> bool:
    """棋盘是否已下满（无空位 -1）。"""
    return all(cell != -1 for row in board for cell in row)


def is_legal_move(board: list[list[int]], x: int | None, y: int | None, size: int = BOARD_SIZE) -> bool:
    """落子是否合法：坐标有效且该位为空。"""
    if x is None or y is None:
        return False
    if not in_board(x, y, size):
        return False
    return board[x][y] == -1


def new_board(size: int = BOARD_SIZE) -> list[list[int]]:
    """创建空棋盘（-1=空，0=黑，1=白）。"""
    return [[-1 for _ in range(size)] for _ in range(size)]


def compute_scores(winner: int | None) -> list[int]:
    """根据胜者计算比分 [黑分, 白分]：胜=1，负=0，平=0/0。"""
    scores = [0, 0]
    if winner is not None:
        scores[winner] = 1
    return scores


def compute_deltas(winner: int | None) -> list[int]:
    """根据胜者计算 deltas（零和）：黑胜 [+1,-1]，白胜 [-1,+1]，平 [0,0]。"""
    if winner == 0:
        return [1, -1]
    elif winner == 1:
        return [-1, 1]
    return [0, 0]


__all__ = [
    "BOARD_SIZE",
    "in_board",
    "check_win",
    "board_full",
    "is_legal_move",
    "new_board",
    "compute_scores",
    "compute_deltas",
]
