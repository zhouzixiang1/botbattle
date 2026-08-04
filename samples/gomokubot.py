#!/usr/bin/env python3
"""五子棋随机空点样例 bot（Botzone 标准协议，信封）。

Botzone 信封：Traditional 完整历史 / LongRunning 单 request（首回合完整）。
请求负载：{x,y,me}；响应信封：{"response": {"x":.., "y":..}}。
"""
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


def _extract_request(envelope: dict) -> dict:
    """从信封取当前回合请求负载。"""
    if "request" in envelope:
        return envelope["request"]
    reqs = envelope.get("requests") or []
    return reqs[-1] if reqs else {}


def main() -> None:
    global my_color
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            env = json.loads(line)
        except json.JSONDecodeError:
            print(json.dumps({"response": {"x": -1, "y": -1}}), flush=True)
            continue
        req = _extract_request(env) if isinstance(env, dict) else {}
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
            print(json.dumps({"response": {"x": -1, "y": -1}}), flush=True)
            continue
        x, y = random.choice(empties)
        place(x, y, me)
        print(json.dumps({"response": {"x": x, "y": y}}), flush=True)


if __name__ == "__main__":
    main()
