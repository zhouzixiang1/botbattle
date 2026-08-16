"""三款游戏结果类型解耦后的通用层契约测试。

通用编排层（orchestrator/rating/replay）只依赖**鸭子契约**（全面解耦 PR4 起
result 不再共享基类，各游戏独立定义 games/<game>/result.py）：
- result.rounds[*].winners / .deltas
- result.rounds_played
- 单局棋类 result.winner 便捷属性
本测试用各游戏最小 decide 跑一局，断言通用层提取的 winner/deltas 一致，
且棋类结果不再含 holdem 专属字段（pot/board/holes/folded）。
"""
from __future__ import annotations

import asyncio

from bzplat.backend.games.gomoku.engine import GomokuSession
from bzplat.backend.games.gomoku.result import MatchResult as GomokuResult
from bzplat.backend.games.pencil.engine import PencilSession
from bzplat.backend.games.pencil.result import MatchResult as PencilResult
from bzplat.backend.games.base import MatchResult, RoundResult
from bzplat.backend.tests._gomoku_v2 import seat_zero_winning_decider


def test_round_result_is_minimal_contract():
    r = RoundResult(winners=[0], deltas=[1, -1])
    assert r.winners == [0]
    assert r.deltas == [1, -1]


def test_match_result_winner_property_single_round():
    base = MatchResult(rounds_played=1, rounds=[RoundResult([0], [1, -1])])
    assert base.winner == 0
    base_draw = MatchResult(rounds_played=1, rounds=[RoundResult([], [0, 0])])
    assert base_draw.winner is None


def test_match_result_winner_property_multi_round_returns_none():
    # 多手（扑克语义）：无 final_chips 时 winner 属性返回 None（编排层按 ea/eb 兜底）
    base = MatchResult(
        rounds_played=3,
        rounds=[RoundResult([0], [1, -1]), RoundResult([1], [-1, 1]), RoundResult([0], [1, -1])],
    )
    assert base.winner is None  # 无 final_chips → 多轮不取单轮胜者，返 None


def test_match_result_winner_property_multi_round_with_final_chips():
    """PR4：多手 holdem 带 final_chips（累计净筹码）时，winner 在引擎内权威化。

    取代编排层三层兜底（result.winner→ea/eb→match_end 事件）+ holdem 特例注释。
    final_chips[0] > final_chips[1] → winner=0；反之 winner=1；相等 None（平局）。
    """
    # 用 holdem 专属 MatchResult（含 final_chips 字段；非上面的 compat 基类）
    from bzplat.backend.games.holdem.result import MatchResult as HMR, RoundResult as HRR

    # 累计净筹码 [300, -300] → 座位 0 胜
    w0 = HMR(
        rounds_played=3,
        rounds=[HRR([0], [100, -100]), HRR([0], [200, -200])],
        final_chips=[300, -300],
    )
    assert w0.winner == 0
    # 累计净筹码 [-300, 300] → 座位 1 胜
    w1 = HMR(rounds_played=3, rounds=[HRR([1], [-100, 100])], final_chips=[-300, 300])
    assert w1.winner == 1
    # 平局
    draw = HMR(rounds_played=3, rounds=[HRR([], [0, 0])], final_chips=[0, 0])
    assert draw.winner is None


def test_gomoku_result_has_no_holdem_fields():
    # 直接构造一个 GomokuResult，断言它没有 holdem 专属属性
    g = GomokuResult(
        rounds_played=9,
        rounds=[RoundResult([0], [1, -1])],
        winner=0,
        reason="five",
        scores=[1, 0],
        moves=9,
    )
    for holdem_field in ("pot", "holes", "folded", "hand_index", "final_chips"):
        assert not hasattr(g, holdem_field) or getattr(g, holdem_field, "X") in (None, [], "")
    # 通用层提取一致
    assert g.winner == 0
    ea = sum(r.deltas[0] for r in g.rounds)
    eb = sum(r.deltas[1] for r in g.rounds)
    assert ea == 1 and eb == -1
    assert g.rounds_played == 9


def test_pencil_result_has_no_holdem_fields():
    p = PencilResult(
        rounds_played=12,
        rounds=[RoundResult([0], [3, -3])],
        winner=0,
        reason="score",
        scores=[7, 4],
        moves=12,
    )
    for holdem_field in ("pot", "holes", "folded", "hand_index", "final_chips"):
        assert not hasattr(p, holdem_field) or getattr(p, holdem_field, "X") in (None, [], "")
    assert p.winner == 0
    assert sum(p.scores) == 11


def test_gomoku_session_returns_gomoku_result():
    result = asyncio.run(
        GomokuSession().run_async(seat_zero_winning_decider())
    )
    # PR4 起 result 不再共享基类——断言鸭子契约字段（通用层只读这些）
    assert hasattr(result, "rounds_played")
    assert hasattr(result, "rounds") and hasattr(result, "events")
    assert result.winner == 0
    assert result.rounds[0].winners == [0]
    # 各游戏 result 类独立，但 GomokuResult 别名（=games.gomoku.result.MatchResult）一致
    assert isinstance(result, GomokuResult)
