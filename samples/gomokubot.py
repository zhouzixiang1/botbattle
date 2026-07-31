#!/usr/bin/env python3
"""五子棋随机空点样例 bot（长驻行协议）。"""
from __future__ import annotations

import json
import random
import sys

SIZE = 15
board = [[-1] * SIZE for _ in range(SIZE)]
my_color = None


def place(x: int, y: int, p: int) -> None:
    if 0 <= x < SIZE and 0 <= y < SIZE:
        board[x][y] = p


def main() -> None:
    global my_color
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"x": -1, "y": -1}), flush=True)
            continue
        me = int(req.get("me", 0))
        if my_color is None:
            my_color = me
        ox, oy = int(req.get("x", -1)), int(req.get("y", -1))
        if ox >= 0:
            place(ox, oy, 1 - me)
        empties = [
            (x, y)
            for x in range(SIZE)
            for y in range(SIZE)
            if board[x][y] < 0
        ]
        if not empties:
            print(json.dumps({"x": -1, "y": -1}), flush=True)
            continue
        x, y = random.choice(empties)
        place(x, y, me)
        print(json.dumps({"x": x, "y": y}), flush=True)


if __name__ == "__main__":
    main()
