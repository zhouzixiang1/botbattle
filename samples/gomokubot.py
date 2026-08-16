#!/usr/bin/env python3
"""全国机器博弈竞赛五子棋 v2 确定性样例 Bot。

Traditional 读取 ``requests[-1]``，LongRunning 读取 ``request``；两种模式
收到的当前请求都带完整棋盘。Bot 覆盖指定开局、三手交换、白4、N 打、
候选选择和正常行棋，并始终使用平台标准 ``response`` 信封。
"""
from __future__ import annotations

import json
import sys
from typing import Any, Iterable

SIZE = 15
EMPTY = -1
BLACK = 0
WHITE = 1
KEEP_RUNNING = ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"

PHASE_OPENING = "opening_proposal"
PHASE_SWAP = "swap_choice"
PHASE_WHITE4 = "white4"
PHASE_BLACK5_CANDIDATES = "black5_candidates"
PHASE_BLACK5_SELECT = "black5_select"
PHASE_NORMAL = "normal_play"

_DIRECTIONS = ((1, 0), (0, 1), (1, 1), (1, -1))
_WHITE_PLAN = tuple((2, y) for y in range(2, 7))
_CANDIDATE_PLAN = ((0, 0), (14, 14), (0, 14), (14, 0), (1, 13))


def _current_request(envelope: dict[str, Any]) -> dict[str, Any]:
    requests = envelope.get("requests")
    if isinstance(requests, list):
        if not requests or not isinstance(requests[-1], dict):
            raise ValueError("Traditional 信封缺少当前 request")
        return requests[-1]
    request = envelope.get("request")
    if not isinstance(request, dict):
        raise ValueError("LongRunning 信封缺少 request")
    return request


def _board_from(request: dict[str, Any]) -> list[list[int]]:
    raw = request.get("board")
    if not isinstance(raw, list) or len(raw) != SIZE:
        raise ValueError("board 必须是 15 列")
    board: list[list[int]] = []
    for column in raw:
        if not isinstance(column, list) or len(column) != SIZE:
            raise ValueError("board 每列必须有 15 个交叉点")
        normalized: list[int] = []
        for cell in column:
            if isinstance(cell, bool) or not isinstance(cell, int) or cell not in {
                EMPTY,
                BLACK,
                WHITE,
            }:
                raise ValueError("board 只允许 -1/0/1")
            normalized.append(cell)
        board.append(normalized)
    return board


def _all_empty(board: list[list[int]]) -> Iterable[tuple[int, int]]:
    for x in range(SIZE):
        for y in range(SIZE):
            if board[x][y] == EMPTY:
                yield x, y


def _first_empty(
    board: list[list[int]], preferred: Iterable[tuple[int, int]] = ()
) -> tuple[int, int] | None:
    seen: set[tuple[int, int]] = set()
    for x, y in (*tuple(preferred), *_all_empty(board)):
        if (x, y) in seen:
            continue
        seen.add((x, y))
        if 0 <= x < SIZE and 0 <= y < SIZE and board[x][y] == EMPTY:
            return x, y
    return None


def _black_move_is_conservatively_safe(
    board: list[list[int]], x: int, y: int
) -> bool:
    """只选不可能由该手形成三、四、五或长连的黑点。

    所有禁手和五连都必须在包含新子的五格窗口（长连则包含相邻连续子）
    中出现。四条线上距新点四步内完全没有其他黑子，是容易独立审计的
    充分安全条件；条件过严时 Bot 可以按规则 PASS。
    """

    if not (0 <= x < SIZE and 0 <= y < SIZE) or board[x][y] != EMPTY:
        return False
    for dx, dy in _DIRECTIONS:
        for step in range(-4, 5):
            if step == 0:
                continue
            cx, cy = x + step * dx, y + step * dy
            if 0 <= cx < SIZE and 0 <= cy < SIZE and board[cx][cy] == BLACK:
                return False
    return True


def _safe_black_move(board: list[list[int]]) -> tuple[int, int] | None:
    # 97 与 225 互质，因此这一固定序列会且只会检查每个交叉点一次。
    for index in range(SIZE * SIZE):
        position = (SIZE * SIZE - 1 - index * 97) % (SIZE * SIZE)
        x, y = divmod(position, SIZE)
        if _black_move_is_conservatively_safe(board, x, y):
            return x, y
    return None


def _candidate_points(
    board: list[list[int]], count: int
) -> list[dict[str, int]]:
    points: list[dict[str, int]] = []
    reserved: set[tuple[int, int]] = set()
    for point in (*_CANDIDATE_PLAN, *_all_empty(board)):
        x, y = point
        if point in reserved or board[x][y] != EMPTY:
            continue
        reserved.add(point)
        points.append({"x": x, "y": y})
        if len(points) == count:
            return points
    raise ValueError("棋盘没有足够的黑5候选点")


def _respond(request: dict[str, Any]) -> dict[str, Any]:
    phase = request.get("phase")
    board = _board_from(request)

    if phase == PHASE_OPENING:
        # 黑1由裁判固定在 H8；白2相邻，黑3位于中心 5x5，对应合法指定开局。
        return {
            "action": "opening",
            "white2": {"x": 7, "y": 8},
            "black3": {"x": 8, "y": 8},
            "n": 2,
        }
    if phase == PHASE_SWAP:
        return {"action": "swap", "swap": False}
    if phase == PHASE_WHITE4:
        point = _first_empty(board, _WHITE_PLAN)
        if point is None:
            raise ValueError("白4无空点")
        return {"action": "move", "x": point[0], "y": point[1]}
    if phase == PHASE_BLACK5_CANDIDATES:
        count = request.get("n")
        if isinstance(count, bool) or not isinstance(count, int) or not 2 <= count <= 5:
            raise ValueError("n 必须是 2..5")
        return {"action": "black5_candidates", "points": _candidate_points(board, count)}
    if phase == PHASE_BLACK5_SELECT:
        candidates = request.get("candidates")
        if not isinstance(candidates, list) or not candidates:
            raise ValueError("缺少黑5候选点")
        return {"action": "black5_select", "index": 0}
    if phase == PHASE_NORMAL:
        color = request.get("color")
        point = (
            _safe_black_move(board)
            if color == BLACK
            else _first_empty(board, _WHITE_PLAN)
        )
        if point is None:
            return {"action": "pass"}
        return {"action": "move", "x": point[0], "y": point[1]}
    raise ValueError(f"未知 phase: {phase!r}")


def main() -> None:
    first_response = True
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            envelope = json.loads(line)
            if not isinstance(envelope, dict):
                raise ValueError("信封不是对象")
            response = _respond(_current_request(envelope))
        except (json.JSONDecodeError, TypeError, ValueError):
            response = {"action": "move", "x": -99, "y": -99}
        print(json.dumps({"response": response}, separators=(",", ":")), flush=True)
        if first_response:
            print(KEEP_RUNNING, flush=True)
            first_response = False


if __name__ == "__main__":
    main()
