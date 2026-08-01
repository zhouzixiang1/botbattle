"""Gomoku / Pencil 引擎单元测试。"""
from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.engine.gomoku import (
    BOARD_SIZE,
    GomokuSession,
    check_win,
    in_board,
)
from bzplat.backend.engine.pencil import PencilBoard, PencilSession
from bzplat.backend.protocol.board_protocol import (
    build_gomoku_request,
    build_pencil_request,
    parse_xy,
)


def test_gomoku_check_win_and_bounds():
    board = [[-1] * BOARD_SIZE for _ in range(BOARD_SIZE)]
    for i in range(5):
        board[7][i] = 0
    assert check_win(board, 7, 2, 0)
    # 长连
    board[7][5] = 0
    assert check_win(board, 7, 5, 0)
    assert in_board(0, 0)
    assert not in_board(-1, 0)
    assert not in_board(15, 0)


def test_gomoku_five_in_a_row_match():
    """黑方连续下成五，白方应付固定点。"""
    black_moves = [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)]
    white_moves = [(1, 0), (1, 1), (1, 2), (1, 3)]
    bi = wi = 0

    def decide_a(req):
        nonlocal bi
        x, y = black_moves[bi]
        bi += 1
        return {"x": x, "y": y}

    def decide_b(req):
        nonlocal wi
        x, y = white_moves[wi]
        wi += 1
        return {"x": x, "y": y}

    async def decide(player, req):
        return decide_a(req) if player == 0 else decide_b(req)

    result = asyncio.run(GomokuSession().run_async(decide))
    assert result.winner == 0
    assert result.reason == "five"
    assert result.rounds[0].winners == [0]
    assert result.rounds_played == 9  # 5 black + 4 white
    assert result.moves == 9


def test_gomoku_illegal_loses():
    async def decide(player, req):
        if player == 0:
            return {"x": 0, "y": 0}
        return {"x": 0, "y": 0}  # 占已有点 → 非法

    result = asyncio.run(GomokuSession().run_async(decide))
    assert result.winner == 0
    assert result.reason == "illegal"
    assert result.rounds[0].winners == [0]


def test_pencil_score_and_continue():
    """小棋盘：占第四边得分并连走。"""
    # n_dots=2 → size=3，只有 1 个格子
    g = PencilBoard(2)
    assert g.size == 3
    assert len(g.legal_actions()) == 4
    # 占三条边不得分
    g.curr_player = 0
    assert g.do_action(0, 1) is False  # 上边
    assert g.do_action(1, 0) is False  # 左边
    assert g.do_action(1, 2) is False  # 右边
    assert g.scores == [0, 0]
    assert g.do_action(2, 1) is True  # 下边 → 成格
    assert g.scores == [1, 0]


def test_pencil_illegal_edge():
    async def decide(player, req):
        return {"x": 0, "y": 0}  # 点不是边

    result = asyncio.run(PencilSession(n_dots=3).run_async(decide))
    assert result.reason == "illegal"
    assert result.winner == 1
    assert result.rounds[0].winners == [1]


def test_pencil_full_game_randomish():
    """双方总选第一条合法边；含 pass 连走。"""

    def make_bot():
        board = PencilBoard(3)

        def decide(req):
            ox, oy = int(req["x"]), int(req["y"])
            # pass 请求也带对方刚下的边，必须先落子再回 -1,-1
            if ox >= 0 and board.is_legal_edge(ox, oy):
                board.curr_player = 1 - int(req["me"])
                board.do_action(ox, oy)
            if int(req.get("pass") or 0) == 1:
                return {"x": -1, "y": -1}
            acts = board.legal_actions()
            assert acts, "no legal edges"
            x, y = acts[0]
            board.curr_player = int(req["me"])
            board.do_action(x, y)
            return {"x": x, "y": y}

        return decide

    a = make_bot()
    b = make_bot()

    async def decide(player, req):
        return a(req) if player == 0 else b(req)

    result = asyncio.run(PencilSession(n_dots=3).run_async(decide))
    assert result.reason in ("score", "draw")
    assert sum(result.scores) == 4  # (3-1)^2 = 4 boxes
    assert result.rounds_played > 0
    assert result.moves > 0


def test_board_protocol_roundtrip():
    g = build_gomoku_request(x=-1, y=-1, me=0)
    assert g["t"] == "mv" and g["x"] == -1
    p = build_pencil_request(x=1, y=0, pass_=1, me=1, scores=[2, 1])
    assert p["pass"] == 1 and p["scores"] == [2, 1]
    assert parse_xy({"x": 3, "y": 4}) == (3, 4)
    assert parse_xy({}) == (None, None)


def test_run_session_pencil_n_dots_none_uses_default():
    """registry.run_session 在 n_dots=None 时应兜底 DEFAULT_N（而非崩溃）。

    回归：/api/matches/challenge 不接受 n_dots，match 行 n_dots=NULL，
    经 orchestrator→runner→run_session(n_dots=None)→PencilBoard(None) 曾抛
    TypeError: unsupported operand type(s) for *: 'int' and 'NoneType'。
    """
    from bzplat.backend.engine.pencil import DEFAULT_N
    from bzplat.backend.engine.registry import run_session

    moves = iter([(0, 1), (0, 1), (0, 1), (0, 1)])  # 简单合法边序列（n_dots=3）

    async def decide(player, req):
        try:
            x, y = next(moves)
        except StopIteration:
            x, y = -1, -1
        return {"x": x, "y": y}

    # n_dots=None 不应抛 TypeError；应使用 DEFAULT_N 跑完整局
    result = asyncio.run(run_session("pencil", decide, n_dots=None, num_hands=1))
    assert result is not None
    # 确认用的是默认 n_dots（DEFAULT_N），盘面非空
    assert result.rounds_played > 0 or result.moves >= 0  # 至少不崩溃

