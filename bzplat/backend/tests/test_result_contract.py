"""全面解耦 PR-D：各游戏 result 鸭子契约测试（防 drift）。

三游戏的 result.py 独立定义（不共享基类）。本测试断言它们都满足通用层
（orchestrator/contests）读取的鸭子契约字段——若任一游戏的 result 漏字段或改名，
此测试会捕获（防 "改一个忘另两个" 的 drift）。
"""
from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.games import registry
from bzplat.backend.games.holdem.result import MatchResult as HMR, RoundResult as HRR
from bzplat.backend.games.gomoku.result import MatchResult as GMR, RoundResult as GRR
from bzplat.backend.tests._gomoku_v2 import seat_zero_winning_decider
from bzplat.backend.games.pencil.result import MatchResult as PMR, RoundResult as PRR
from bzplat.backend.matches.result_contract import (
    RESULT_COMMON_FIELDS,
    build_result_payload,
    build_technical_result_payload,
)
from bzplat.backend.store.public_contract import sanitize_public_result


# ── RoundResult 鸭子契约 ──────────────────────────────────────
def test_all_round_results_have_winners_and_deltas():
    """三游戏的 RoundResult 都有 winners(list[int]) + deltas(list[int])。"""
    for RR, label in ((HRR, "holdem"), (GRR, "gomoku"), (PRR, "pencil")):
        r = RR(winners=[0], deltas=[1, -1])
        assert r.winners == [0], f"{label} RoundResult.winners"
        assert r.deltas == [1, -1], f"{label} RoundResult.deltas"


# ── MatchResult 鸭子契约（通用层读取的字段）──────────────────
def test_all_match_results_have_contract_fields():
    """三游戏 MatchResult 都有 rounds_played/rounds/events/winner（通用层读这些）。"""
    for MR, RR, label in ((HMR, HRR, "holdem"), (GMR, GRR, "gomoku"), (PMR, PRR, "pencil")):
        m = MR(rounds_played=1, rounds=[RR([0], [1, -1])], events=[{"type": "match_start"}])
        # 通用层 orchestrator.py 读取的字段
        assert m.rounds_played == 1, f"{label} rounds_played"
        assert len(m.rounds) == 1, f"{label} rounds"
        assert m.rounds[0].winners == [0], f"{label} rounds[0].winners"
        assert m.rounds[0].deltas == [1, -1], f"{label} rounds[0].deltas"
        assert isinstance(m.events, list), f"{label} events"
        # winner 属性/字段（holdem 是 property 返回 int|None；棋类是字段）
        _ = m.winner  # 不抛 AttributeError 即可


def test_persisted_result_builder_is_the_only_three_field_common_contract():
    expected_normalized = {"holdem": 5.0, "gomoku": 500.0, "pencil": 500.0}
    for game_id in registry.all_ids():
        payload = build_result_payload(
            registry.get(game_id), rounds_played=7, deltas=[500, -500]
        )
        assert set(payload) == RESULT_COMMON_FIELDS
        assert payload["rounds_played"] == 7
        assert payload["deltas"] == [500, -500]
        assert payload["normalized_delta"] == expected_normalized[game_id]


def test_persisted_result_builder_rejects_invalid_common_values_and_overrides():
    spec = registry.get("gomoku")
    with pytest.raises(ValueError, match="零和"):
        build_result_payload(spec, rounds_played=1, deltas=[1, 1])
    with pytest.raises(TypeError, match="非负整数"):
        build_result_payload(spec, rounds_played=True, deltas=[1, -1])
    with pytest.raises(ValueError, match="不得覆盖"):
        build_result_payload(
            spec,
            rounds_played=1,
            deltas=[1, -1],
            extra={"normalized_delta": 99},
        )


def test_technical_result_progress_is_game_owned_without_common_game_branch():
    cases = {
        "holdem": [{"type": "settle"}, {"type": "action"}, {"type": "settle"}],
        "gomoku": [{"type": "move"}, {"type": "turn"}, {"type": "move"}],
        "pencil": [{"type": "move"}, {"type": "time_used"}, {"type": "move"}],
    }
    for game_id, events in cases.items():
        payload = build_technical_result_payload(
            registry.get(game_id), events, deltas=[-1, 1]
        )
        assert payload["rounds_played"] == 2
        assert set(payload) == RESULT_COMMON_FIELDS


def test_public_projection_does_not_serve_retired_result_aliases():
    assert sanitize_public_result(
        {"hands_played": 70, "net_bb": 3.5}
    ) == {}
    assert sanitize_public_result(
        {
            "rounds_played": 70,
            "deltas": [350, -350],
            "normalized_delta": 3.5,
            "hands_played": 1,
            "net_bb": 99,
        }
    ) == {
        "rounds_played": 70,
        "deltas": [350, -350],
        "normalized_delta": 3.5,
    }


def test_match_result_winner_semantics():
    """单轮有胜者 → winner 返回胜者；空 winners → None/平局。"""
    # 棋类单轮有胜者
    g = GMR(rounds_played=1, rounds=[GRR([0], [1, -1])], winner=0)
    assert g.winner == 0
    p = PMR(rounds_played=1, rounds=[PRR([1], [-1, 1])], winner=1)
    assert p.winner == 1
    # holdem 单轮有胜者（property 取 rounds[0].winners[0]）
    h = HMR(rounds_played=1, rounds=[HRR([0], [100, -100])])
    assert h.winner == 0
    # holdem 多轮无 final_chips → winner None（编排层按 ea/eb 兜底判）
    h2 = HMR(rounds_played=3, rounds=[HRR([0], [1, -1]), HRR([1], [-1, 1]), HRR([0], [1, -1])])
    assert h2.winner is None
    # holdem 多轮带 final_chips（累计净筹码）→ winner 在引擎内权威化（PR4）
    h3 = HMR(rounds_played=3, rounds=[HRR([0], [100, -100]), HRR([0], [200, -200])],
             final_chips=[300, -300])
    assert h3.winner == 0


def test_rounds_element_supports_delta_sum():
    """通用层 sum(r.deltas[0] for r in result.rounds) 须可用。"""
    for MR, RR, label in ((HMR, HRR, "holdem"), (GMR, GRR, "gomoku"), (PMR, PRR, "pencil")):
        m = MR(rounds_played=2, rounds=[RR([0], [1, -1]), RR([0], [2, -2])])
        ea = sum(r.deltas[0] for r in m.rounds)
        eb = sum(r.deltas[1] for r in m.rounds)
        assert ea == 3 and eb == -3, f"{label} deltas sum"


# ── 真实引擎产出的 result 也满足契约（端到端）─────────────────
def test_real_gomoku_result_satisfies_contract():
    """真实跑一局 gomoku，断言产出的 MatchResult 满足鸭子契约。"""
    from bzplat.backend.games.gomoku.engine import GomokuSession

    result = asyncio.run(
        GomokuSession().run_async(seat_zero_winning_decider())
    )
    # 契约字段
    assert hasattr(result, "rounds_played")
    assert hasattr(result, "rounds") and len(result.rounds) == 1
    assert hasattr(result, "events")
    assert result.rounds[0].winners == [0]
    ea = sum(r.deltas[0] for r in result.rounds)
    assert ea == 1  # 胜方 +1


def test_holdem_single_hand_split_pot_is_draw():
    """单手 split pot（winners=[0,1]）→ winner 应为 None（平局），非误判 seat0。

    审计 P1：原逻辑 `winners[0]` 在双胜者（split）时误判座位0胜。
    修复：winners 长度==1 才取，>1 视为平局返 None。
    生产路径（70手多手）走 final_chips 比较，不受影响。
    """
    # split pot（deltas 相等）
    h = HMR(rounds_played=1, rounds=[HRR([0, 1], [50, 50])])
    assert h.winner is None, "split pot（winners=[0,1]）应为平局 None，非误判 seat0"
    # odd chip 给一方，winners 仍是 [0,1]（双胜者=平局），不靠 deltas 判
    h2 = HMR(rounds_played=1, rounds=[HRR([0, 1], [51, 50])])
    assert h2.winner is None, "odd chip split 仍应平局（winners 长度=2 是权威平局信号）"
    # 唯一胜者不受影响
    h3 = HMR(rounds_played=1, rounds=[HRR([0], [100, -100])])
    assert h3.winner == 0
    h4 = HMR(rounds_played=1, rounds=[HRR([1], [-100, 100])])
    assert h4.winner == 1
    # 无胜者（winners=[]）平局
    h5 = HMR(rounds_played=1, rounds=[HRR([], [0, 0])])
    assert h5.winner is None
