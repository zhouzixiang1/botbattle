#!/usr/bin/env python3
"""点格棋随机占边样例 bot（长驻行协议，对齐 Botzone pass 语义）。"""
from __future__ import annotations

import json
import random
import sys

N = 6  # 对齐 Botzone grid_size=11 交错维度（6 点 → 25 格）
SIZE = 2 * N - 1
GRID_EDGE = 4
GRID_EDGE_USED = 5
GRID_BOX = 2
GRID_DOT = 3
DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1)]


def make_board():
    b = [[0] * SIZE for _ in range(SIZE)]
    for x in range(SIZE):
        for y in range(SIZE):
            if x % 2 == 0 and y % 2 == 0:
                b[x][y] = GRID_DOT
            elif (x + y) % 2 == 1:
                b[x][y] = GRID_EDGE
            else:
                b[x][y] = GRID_BOX
    return b


board = make_board()
curr = 0


def in_board(x, y):
    return 0 <= x < SIZE and 0 <= y < SIZE


def update_boxes(x, y, player):
    scored = False
    for dx, dy in DIRS:
        bx, by = x + dx, y + dy
        if not (in_board(bx, by) and board[bx][by] == GRID_BOX):
            continue
        n = sum(
            1
            for ddx, ddy in DIRS
            if in_board(bx + ddx, by + ddy)
            and board[bx + ddx][by + ddy] == GRID_EDGE_USED
        )
        if n == 4:
            scored = True
    return scored


def do_action(x, y):
    board[x][y] = GRID_EDGE_USED
    return update_boxes(x, y, curr)


def legal():
    return [
        (x, y)
        for x in range(SIZE)
        for y in range(SIZE)
        if board[x][y] == GRID_EDGE
    ]


def main() -> None:
    global curr
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"x": -1, "y": -1}), flush=True)
            continue
        if int(req.get("pass") or 0) == 1:
            ox, oy = int(req.get("x", -1)), int(req.get("y", -1))
            if ox >= 0 and board[ox][oy] == GRID_EDGE:
                do_action(ox, oy)
            print(json.dumps({"x": -1, "y": -1}), flush=True)
            continue
        ox, oy = int(req.get("x", -1)), int(req.get("y", -1))
        me = int(req.get("me", 0))
        if ox >= 0 and board[ox][oy] == GRID_EDGE:
            do_action(ox, oy)
        acts = legal()
        if not acts:
            print(json.dumps({"x": -1, "y": -1}), flush=True)
            continue
        x, y = random.choice(acts)
        do_action(x, y)
        print(json.dumps({"x": x, "y": y}), flush=True)


if __name__ == "__main__":
    main()
