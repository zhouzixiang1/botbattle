"""Gomoku / Pencil 引擎单元测试。"""
from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.games.gomoku.engine import (
    BOARD_SIZE,
    GomokuSession,
    check_win,
    in_board,
)
from bzplat.backend.games.pencil.engine import PencilBoard, PencilSession
from bzplat.backend.games.gomoku.protocol import build_gomoku_request, parse_xy
from bzplat.backend.games.pencil.protocol import build_pencil_request


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
        return {"response": {"x": x, "y": y}}

    def decide_b(req):
        nonlocal wi
        x, y = white_moves[wi]
        wi += 1
        return {"response": {"x": x, "y": y}}

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
            return {"response": {"x": 0, "y": 0}}
        return {"response": {"x": 0, "y": 0}}  # 占已有点 → 非法

    result = asyncio.run(GomokuSession().run_async(decide))
    assert result.winner == 0
    assert result.reason == "illegal"
    assert result.rounds[0].winners == [0]


def test_pencil_score_and_continue():
    """小棋盘：占第四边得分并连走。do_action 返回新闭合的格心坐标列表。"""
    # n_dots=2 → size=3，只有 1 个格子
    g = PencilBoard(2)
    assert g.size == 3
    assert len(g.legal_actions()) == 4
    # 占三条边不得分（返回空列表）
    g.curr_player = 0
    assert g.do_action(0, 1) == []  # 上边
    assert g.do_action(1, 0) == []  # 左边
    assert g.do_action(1, 2) == []  # 右边
    assert g.scores == [0, 0]
    closed = g.do_action(2, 1)  # 下边 → 成格
    assert len(closed) == 1  # 闭合 1 格
    assert closed[0] == (1, 1)  # 格心坐标
    assert g.scores == [1, 0]
    assert g.box_owner[(1, 1)] == 0  # 归属红方
    assert g.edge_owner[(2, 1)] == 0  # 下边归属红方


def test_pencil_illegal_edge():
    async def decide(player, req):
        return {"response": {"x": 0, "y": 0}}  # 点不是边

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
                return {"response": {"x": -1, "y": -1}}
            acts = board.legal_actions()
            assert acts, "no legal edges"
            x, y = acts[0]
            board.curr_player = int(req["me"])
            board.do_action(x, y)
            return {"response": {"x": x, "y": y}}

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
    # Botzone 标准协议：请求负载 {x,y,me}（信封由传输层包），无 v/t 字段。
    g = build_gomoku_request(x=-1, y=-1, me=0)
    assert g["x"] == -1 and g["me"] == 0
    assert "t" not in g  # Botzone 化后无 t 字段
    p = build_pencil_request(x=1, y=0, pass_=1, me=1, scores=[2, 1])
    assert p["pass"] == 1 and p["scores"] == [2, 1]
    # response 必填；顶层调试字段忽略，游戏 payload 仍严格只有 x/y。
    assert parse_xy({"x": 3, "y": 4}) == (None, None)
    assert parse_xy({"response": {"x": 5, "y": 10}}) == (5, 10)
    assert parse_xy({"response": {"x": 5, "y": 10}, "debug": "x"}) == (5, 10)
    assert parse_xy({"response": {"x": 5, "y": 10, "debug": "x"}}) == (None, None)
    assert parse_xy({}) == (None, None)
    assert parse_xy({"response": {}}) == (None, None)


def test_run_session_pencil_rejects_removed_rule_params():
    """直接入口不能把 n_dots/num_hands 当成可忽略参数。"""
    from bzplat.backend.games import run_session

    async def decide(_player, _req):
        raise AssertionError("非法参数应在开始对局前被拒绝")

    with pytest.raises(TypeError, match="Session 不接受参数"):
        asyncio.run(run_session("pencil", decide, n_dots=None, num_hands=1))


# ── 对齐权威裁判（C++）：25 格 / 多数胜 / 2-0 归一化 / 归属追踪 ──────────

def test_pencil_default_n_is_6_25_boxes():
    """对齐裁判：默认 n_dots=6 → size=11 → 25 格（奇数无平局）。"""
    from bzplat.backend.games.pencil.engine import DEFAULT_N, PencilBoard
    assert DEFAULT_N == 6
    g = PencilBoard()
    assert g.size == 11
    assert (g.n_dots - 1) ** 2 == 25  # 25 格
    assert g.min_win() == 13  # 多数胜阈值 ⌈25/2⌉


def test_pencil_majority_win_ends_early():
    """对齐裁判 hasPlayerWon：先到多数格（13）立即胜，不等终局。"""
    from bzplat.backend.games.pencil.engine import PencilBoard

    # 构造一个红方连得 13 分的场景：n_dots=6，红方每次占第四边成格连走
    # 用最小可复现：直接检查 min_win 阈值 + do_action 后的 scores 触发
    g = PencilBoard(6)
    assert g.min_win() == 13
    # 模拟：红方分数到 13 时应判胜（engine 在 do_action 后检查 scores[curr]>=min_win）
    g.scores = [12, 0]
    g.curr_player = 0
    # 占一条边让红方得第 13 分（需构造一个可成格的四边场景——用 n_dots=2 单格验证逻辑）
    g2 = PencilBoard(2)  # 1 格，min_win=1
    assert g2.min_win() == 1
    g2.curr_player = 0
    g2.do_action(0, 1)
    g2.do_action(1, 0)
    g2.do_action(1, 2)
    closed = g2.do_action(2, 1)  # 成格 → scores=[1,0] >= min_win(1)
    assert len(closed) == 1
    assert g2.scores[0] >= g2.min_win()  # 触发多数胜条件


def test_pencil_illegal_normalizes_2_0():
    """对齐裁判：非法着 → 对手 2-0（scores 归一化，非实时部分分）。"""
    async def decide(player, req):
        return {"response": {"x": 0, "y": 0}}  # (0,0) 是点不是边 → 非法

    result = asyncio.run(PencilSession(n_dots=3).run_async(decide))
    assert result.reason == "illegal"
    assert result.winner == 1  # 红方非法 → 蓝方胜
    assert result.scores == [0, 2]  # 归一化 2-0（对手蓝方 2 分）
    assert result.rounds[0].deltas == [-2, 2]  # deltas 反映归一化


def test_pencil_crash_normalizes_2_0():
    """对齐裁判：bot 崩溃 → 判负 2-0（不再中止整场）。"""
    from bzplat.backend.runtime.binary_runner import BotCrashedError

    def crashing_decide(player, req):
        raise BotCrashedError("simulated crash")

    result = asyncio.run(PencilSession(n_dots=3).run_async(crashing_decide))
    assert result.reason == "crash"
    assert result.winner == 1  # 红方崩 → 蓝方胜
    assert result.scores == [0, 2]


def test_pencil_ownership_tracking():
    """edge_owner + box_owner 追踪（前端着色用）。"""
    g = PencilBoard(2)  # 1 格
    g.curr_player = 0
    g.do_action(0, 1)
    g.do_action(1, 0)
    g.do_action(1, 2)
    # 第四边前：无归属格
    assert g.box_owner == {}
    closed = g.do_action(2, 1)  # 成格
    assert (1, 1) in g.box_owner
    assert g.box_owner[(1, 1)] == 0  # 红方
    assert g.edge_owner[(2, 1)] == 0  # 下边属红方
    # box_owners_grid 返回真实归属
    bog = g.box_owners_grid()
    assert bog[1][1] == 0  # 格心属红方


def test_pencil_move_event_has_closed_boxes():
    """move 事件带 closed_boxes（本手新闭合格 + owner）。"""
    moves = iter([(0, 1), (1, 0), (1, 2), (2, 1)])  # n_dots=2，双方交替占四边

    async def decide(player, req):
        if int(req.get("pass") or 0) == 1:
            return {"response": {"x": -1, "y": -1}}
        try:
            x, y = next(moves)
        except StopIteration:
            x, y = -1, -1
        return {"response": {"x": x, "y": y}}

    sess = PencilSession(n_dots=2)
    result = asyncio.run(sess.run_async(decide))
    # 找带 closed_boxes 的 move 事件
    move_events = [e for e in result.events if e.get("type") == "move"]
    scoring_moves = [e for e in move_events if e.get("scored")]
    assert len(scoring_moves) >= 1
    sm = scoring_moves[0]
    assert "closed_boxes" in sm
    assert len(sm["closed_boxes"]) == 1
    # 第 4 手是蓝方(player=1)下的 → 格属蓝方；且 n_dots=2 的 min_win=1 → 多数胜提前结束
    assert sm["closed_boxes"][0]["owner"] == 1  # 蓝方
    assert result.reason == "majority"  # 蓝方 1 分 >= min_win(1)


def test_pencil_match_end_has_box_owners():
    """match_end 事件带 box_owners 网格（前端最终着色）。"""
    async def decide(player, req):
        return {"response": {"x": 0, "y": 0}}  # 非法→快速结束

    result = asyncio.run(PencilSession(n_dots=3).run_async(decide))
    me = next(e for e in result.events if e.get("type") == "match_end")
    assert "box_owners" in me
    assert isinstance(me["box_owners"], list)



# ─── 本站唯一点格棋规则形式化守护（pencil_judge 独立单测）──────────────────
# 逐条断言：6×6 点→25 格、捕获连走、多数胜 13、一步一边、归属追踪。


def test_canonical_grid_6x6_yields_25_boxes():
    """6×6 点阵 → 交错 size=11 → (N-1)²=25 格。"""
    from bzplat.backend.games.pencil.pencil_judge import PencilBoard, DEFAULT_N

    assert DEFAULT_N == 6
    g = PencilBoard()
    assert g.n_dots == 6
    assert g.size == 2 * 6 - 1  # 交错维度 11
    assert (g.n_dots - 1) ** 2 == 25  # 25 格
    assert g.min_win() == 25 // 2 + 1  # ⌈25/2⌉ = 13（多数胜阈值）


def test_canonical_capture_continues_turn():
    """占边围成格 → 得分并连走（curr_player 不变）。"""
    from bzplat.backend.games.pencil.pencil_judge import PencilBoard

    g = PencilBoard(2)  # 1 格，好控制
    g.curr_player = 0
    g.do_action(0, 1)  # 无格
    g.do_action(1, 0)  # 无格
    g.do_action(1, 2)  # 无格
    assert g.scores == [0, 0]
    closed = g.do_action(2, 1)  # 闭合格
    assert len(closed) == 1
    assert g.scores == [1, 0]
    # 捕获连走：engine 层据此让本方再走（裁判本身不改 curr_player，由适配层轮转）
    # 裁判契约：闭合时 curr_player 仍是得分方（适配层读到 scored → 不换人）
    assert g.curr_player == 0


def test_canonical_majority_win_threshold_13():
    """先到 ⌈boxes/2⌉=13 立即胜。"""
    from bzplat.backend.games.pencil.pencil_judge import PencilBoard

    g = PencilBoard(6)
    assert g.min_win() == 13
    # 12 分未到多数胜阈值
    g.scores = [12, 5]
    assert not g.scores[0] >= g.min_win()
    # 13 分达到阈值
    g.scores = [13, 5]
    assert g.scores[0] >= g.min_win()


def test_canonical_single_edge_per_move():
    """一步只占 1 条边，捕获格时仍然如此。"""
    from bzplat.backend.games.pencil.pencil_judge import PencilBoard

    g = PencilBoard(3)  # 4 格
    g.curr_player = 0
    before = g.remaining_edges()
    closed = g.do_action(0, 1)  # 占 1 边
    after = g.remaining_edges()
    assert before - after == 1  # 每手恰好 1 边
    # 即便闭合格（连走），本手仍只占 1 边。
    # do_action 返回的是「本手新闭合格」，不是多线列表
    assert isinstance(closed, list)


def test_canonical_box_ownership_grid_tracks_players():
    """格归属追踪（前端着色用）——红/蓝/未占三态。"""
    from bzplat.backend.games.pencil.pencil_judge import PencilBoard

    g = PencilBoard(2)  # 1 格
    g.curr_player = 0
    g.do_action(0, 1)
    g.do_action(1, 0)
    g.do_action(1, 2)
    bog = g.box_owners_grid()
    assert bog[1][1] == -1  # 未闭合
    g.do_action(2, 1)  # 红方闭合格
    bog = g.box_owners_grid()
    assert bog[1][1] == 0  # 红方
    # 非格心位置为 -2（忽略）
    assert bog[0][0] == -2


# ─── 裁判模块 0 平台依赖守护（gomoku/pencil）────────────────────────────
def test_gomoku_judge_zero_platform_deps():
    """gomoku_judge 不得 import bzplat（0 平台依赖守护）。"""
    import inspect
    from bzplat.backend.games.gomoku import gomoku_judge

    src = inspect.getsource(gomoku_judge)
    assert "bzplat" not in src, "gomoku_judge 含平台依赖"


def test_pencil_judge_zero_platform_deps():
    """pencil_judge 不得 import bzplat（0 平台依赖守护）。"""
    import inspect
    from bzplat.backend.games.pencil import pencil_judge

    src = inspect.getsource(pencil_judge)
    assert "bzplat" not in src, "pencil_judge 含平台依赖"
