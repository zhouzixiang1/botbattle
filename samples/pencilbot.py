#!/usr/bin/env python3
"""点格棋随机合法边样例（源码；支持 Botzone Traditional/LongRunning）。"""
from __future__ import annotations

import json
import random
import sys
from typing import Any

N = 6
SIZE = 2 * N - 1
KEEP_RUNNING = ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"


def make_board() -> list[list[bool]]:
    """True 表示尚未占用的合法边。"""
    return [[(x + y) % 2 == 1 for y in range(SIZE)] for x in range(SIZE)]


board = make_board()


def mark(move: Any) -> None:
    if not isinstance(move, dict):
        return
    try:
        x, y = int(move.get("x", -1)), int(move.get("y", -1))
    except (TypeError, ValueError):
        return
    if 0 <= x < SIZE and 0 <= y < SIZE and (x + y) % 2 == 1:
        board[x][y] = False


def load_turn(envelope: dict[str, Any]) -> dict[str, Any]:
    """返回当前 request，并按完整历史或单 request 更新棋盘。"""
    global board
    if isinstance(envelope.get("requests"), list):
        board = make_board()
        requests = envelope["requests"]
        responses = envelope.get("responses") or []
        for request in requests:
            mark(request)
        for response in responses:
            # responses[] 是 response payload；兼容误包一层信封的本地输入。
            if isinstance(response, dict) and "response" in response:
                response = response["response"]
            mark(response)
        return requests[-1] if requests and isinstance(requests[-1], dict) else {}

    request = envelope.get("request")
    if not isinstance(request, dict):
        # 上传预检当前直接发送裸 request payload；样例也接受该形式。
        request = envelope
    mark(request)
    return request


def choose_move(request: dict[str, Any]) -> tuple[int, int]:
    if int(request.get("pass") or 0) == 1:
        return -1, -1
    legal = [
        (x, y)
        for x in range(SIZE)
        for y in range(SIZE)
        if board[x][y]
    ]
    if not legal:
        return -1, -1
    x, y = random.choice(legal)
    board[x][y] = False
    return x, y


def main() -> None:
    first_response = True
    for line in sys.stdin:
        try:
            envelope = json.loads(line)
            if not isinstance(envelope, dict):
                raise ValueError("信封不是对象")
            request = load_turn(envelope)
            x, y = choose_move(request)
        except (json.JSONDecodeError, TypeError, ValueError):
            x, y = -1, -1
        print(json.dumps({"response": {"x": x, "y": y}}, separators=(",", ":")), flush=True)
        if first_response:
            print(KEEP_RUNNING, flush=True)
            first_response = False


if __name__ == "__main__":
    main()
