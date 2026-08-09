#!/usr/bin/env python3
"""五子棋随机空点样例 Bot（平台唯一 JSON 信封协议）。

Traditional 使用完整历史；LongRunning 首回合使用完整历史并严格握手，之后使用单 request。
请求负载：{x,y,me}；响应信封：{"response": {"x":.., "y":..}}。
"""
from __future__ import annotations

import json
import random
import sys

SIZE = 15
KEEP_RUNNING = ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"


def make_board() -> list[list[int]]:
    return [[-1] * SIZE for _ in range(SIZE)]


board = make_board()


def place(x: int, y: int, p: int) -> None:
    if 0 <= x < SIZE and 0 <= y < SIZE:
        board[x][y] = p


def _mark_payload(move: object, player: int) -> None:
    if not isinstance(move, dict):
        raise ValueError("落子 payload 不是对象")
    x, y = int(move.get("x", -1)), int(move.get("y", -1))
    if 0 <= x < SIZE and 0 <= y < SIZE:
        place(x, y, player)


def load_turn(envelope: dict) -> dict:
    """按所选运行模式的标准信封更新棋盘并返回当前 request。"""
    global board
    requests = envelope.get("requests")
    if isinstance(requests, list):
        if not requests or not isinstance(requests[-1], dict):
            raise ValueError("完整历史信封缺少当前 request")
        board = make_board()
        me = int(requests[-1].get("me", 0))
        for request in requests:
            _mark_payload(request, 1 - me)
        responses = envelope.get("responses")
        if not isinstance(responses, list):
            raise ValueError("完整历史信封缺少 responses")
        for response in responses:
            _mark_payload(response, me)
        return requests[-1]

    request = envelope.get("request")
    if not isinstance(request, dict):
        raise ValueError("增量信封缺少 request")
    me = int(request.get("me", 0))
    _mark_payload(request, 1 - me)
    return request


def main() -> None:
    first_response = True
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            env = json.loads(line)
            if not isinstance(env, dict):
                raise ValueError("信封不是对象")
            req = load_turn(env)
        except (json.JSONDecodeError, TypeError, ValueError):
            print(json.dumps({"response": {"x": -1, "y": -1}}), flush=True)
        else:
            me = int(req.get("me", 0))
            empties = [
                (x, y)
                for x in range(SIZE)
                for y in range(SIZE)
                if board[x][y] < 0
            ]
            if not empties:
                x, y = -1, -1
            else:
                x, y = random.choice(empties)
                place(x, y, me)
            print(json.dumps({"response": {"x": x, "y": y}}), flush=True)
        if first_response:
            print(KEEP_RUNNING, flush=True)
            first_response = False


if __name__ == "__main__":
    main()
