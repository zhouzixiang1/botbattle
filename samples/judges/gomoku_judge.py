#!/usr/bin/env python3
"""五子棋参考裁判（独立、无平台依赖）。

Bot 作者可本地运行此脚本自测合法着 / 胜负判定，逻辑与本平台服务端
`bzplat/backend/engine/gomoku.py` 的 GomokuSession 裁判一致。

规则：15×15；黑先（seat 0）；横/竖/斜连续 ≥5 即胜（含长连，无禁手）；
非法着（越界 / 占用）→ 判负；棋盘下满无人成五 → 平局。

用法：
    python gomoku_judge.py            # 跑内置演示棋谱（黑五连胜）
    python gomoku_judge.py --check    # 交互：输入 "x y" 逐手判合法/胜负

注意：平台裁判还会在 bot 超时 / 异常时判负，本参考脚本只覆盖落子规则。
"""
from __future__ import annotations

import sys

BOARD_SIZE = 15
EMPTY = -1
# 4 个方向：横、竖、两斜
_DIRS = ((1, 0), (0, 1), (1, 1), (1, -1))


def in_board(x: int, y: int, size: int = BOARD_SIZE) -> bool:
    return 0 <= x < size and 0 <= y < size


def is_legal_move(board: list[list[int]], x: int, y: int) -> bool:
    """落子合法：在棋盘内且该点为空。"""
    return in_board(x, y) and board[x][y] == EMPTY


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
    return all(cell != EMPTY for row in board for cell in row)


def new_board(size: int = BOARD_SIZE) -> list[list[int]]:
    return [[EMPTY for _ in range(size)] for _ in range(size)]


def play_moves(moves: list[tuple[int, int]]) -> str:
    """按 (x,y) 序列对弈，返回 'black' | 'white' | 'draw' | 'illegal:<step>'。"""
    board = new_board()
    player = 0  # 黑先
    for step, (x, y) in enumerate(moves):
        if not is_legal_move(board, x, y):
            return f"illegal:{step+1}:player{player}"
        board[x][y] = player
        if check_win(board, x, y, player):
            return "black" if player == 0 else "white"
        if board_full(board):
            return "draw"
        player = 1 - player
    return "ongoing"


def _demo() -> None:
    # 黑在第一行连五（0,0)..(0,4)，白随机应付第二行
    moves = [(0, 0), (1, 0), (0, 1), (1, 1), (0, 2), (1, 2), (0, 3), (1, 3), (0, 4)]
    result = play_moves(moves)
    print(f"演示棋谱 {len(moves)} 手 → 结果：{result}")
    assert result == "black", "演示应在黑五连胜结束"


def _interactive() -> None:
    board = new_board()
    player = 0
    for step in range(1, BOARD_SIZE * BOARD_SIZE + 1):
        who = "黑(0)" if player == 0 else "白(1)"
        try:
            line = input(f"第 {step} 手 {who}，输入 'x y'（空格分隔）：").strip()
        except EOFError:
            break
        parts = line.split()
        if len(parts) != 2:
            print("格式错误，请重输"); continue
        try:
            x, y = int(parts[0]), int(parts[1])
        except ValueError:
            print("需为整数"); continue
        if not is_legal_move(board, x, y):
            print(f"非法着 → {who} 判负"); return
        board[x][y] = player
        if check_win(board, x, y, player):
            print(f"五连胜！{who} 胜"); return
        if board_full(board):
            print("棋盘满 → 平局"); return
        player = 1 - player
    print("结束")


if __name__ == "__main__":
    if "--check" in sys.argv:
        _interactive()
    else:
        _demo()
