"""Unit tests for HU NLHE engine."""

from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.engine.cards import (
    CAT_FLUSH,
    CAT_FULL_HOUSE,
    CAT_PAIR,
    CAT_QUADS,
    CAT_STRAIGHT,
    CAT_STRAIGHT_FLUSH,
    CAT_TRIPS,
    CAT_TWO_PAIR,
    Card,
    Deck,
    compare_hands,
    evaluate,
)
from bzplat.backend.engine.game import (
    BIG_BLIND,
    SMALL_BLIND,
    STARTING_STACK,
    Action,
    MatchSession,
)
from bzplat.backend.protocol.json_protocol import build_response


def test_deck_size_and_unique():
    d = Deck()
    assert len(d) == 52
    cards = d.deal(52)
    assert len(cards) == 52
    assert len(set(cards)) == 52
    assert len(d) == 0


def test_hand_evaluation_pairwise():
    # Royal / straight flush beats quads
    sf = [Card(12, 0), Card(11, 0), Card(10, 0), Card(9, 0), Card(8, 0)]
    quads = [Card(12, 0), Card(12, 1), Card(12, 2), Card(12, 3), Card(2, 0)]
    assert evaluate(sf)[0] == CAT_STRAIGHT_FLUSH
    assert evaluate(quads)[0] == CAT_QUADS
    assert compare_hands(sf, quads) == 1

    # Full house beats flush
    fh = [Card(8, 0), Card(8, 1), Card(8, 2), Card(3, 0), Card(3, 1)]
    fl = [Card(12, 1), Card(10, 1), Card(7, 1), Card(4, 1), Card(2, 1)]
    assert evaluate(fh)[0] == CAT_FULL_HOUSE
    assert evaluate(fl)[0] == CAT_FLUSH
    assert compare_hands(fh, fl) == 1

    # Straight beats trips
    st = [Card(4, 0), Card(5, 1), Card(6, 2), Card(7, 0), Card(8, 1)]
    tr = [Card(9, 0), Card(9, 1), Card(9, 2), Card(2, 0), Card(3, 1)]
    assert evaluate(st)[0] == CAT_STRAIGHT
    assert evaluate(tr)[0] == CAT_TRIPS
    assert compare_hands(st, tr) == 1

    # Two pair beats pair
    tp = [Card(10, 0), Card(10, 1), Card(5, 0), Card(5, 1), Card(2, 0)]
    op = [Card(12, 0), Card(12, 1), Card(8, 0), Card(7, 1), Card(3, 0)]
    assert evaluate(tp)[0] == CAT_TWO_PAIR
    assert evaluate(op)[0] == CAT_PAIR
    assert compare_hands(tp, op) == 1

    # Wheel straight
    wheel = [Card(12, 0), Card(0, 1), Card(1, 2), Card(2, 0), Card(3, 1)]
    assert evaluate(wheel)[0] == CAT_STRAIGHT

    # 7-card: best five
    seven = [
        Card(12, 0),
        Card(12, 1),
        Card(12, 2),
        Card(5, 0),
        Card(5, 1),
        Card(2, 3),
        Card(3, 3),
    ]
    assert evaluate(seven)[0] == CAT_FULL_HOUSE


def _passive_bot(player_idx: int, req: dict) -> dict:
    """Always check if possible else call; never raise."""
    to_call = int(req.get("to", 0))
    if to_call <= 0:
        return build_response("check")
    return build_response("call")


def test_short_match_check_call_bots():
    session = MatchSession(num_hands=2, rng=__import__("random").Random(42))
    result = asyncio.run(session.run_async(_passive_bot))
    assert result.hands_played == 2
    assert len(result.hand_results) == 2
    # Botzone 计分：final_chips = 累计净输赢 net（零和），不再守恒于 2*STARTING_STACK
    assert sum(result.final_chips) == 0
    types = [e["type"] for e in result.events]
    assert "hand_start" in types
    assert "deal_hole" in types
    assert "settle" in types
    assert "match_end" in types
    # blinds alternate
    starts = [e for e in result.events if e["type"] == "hand_start"]
    assert starts[0]["sb"] == 0
    assert starts[1]["sb"] == 1


def test_raise_validation_exact_2x():
    """Facing BB, raise-to 200 legal; after that min re-raise-to is 400."""
    session = MatchSession(num_hands=1, rng=__import__("random").Random(1))
    # Manually drive first hand setup pieces via private API after start
    # Use scripted decide instead.

    script = {
        # hand 0 preflop: SB (0) raise 200; BB (1) raise 400; SB fold
        0: ["r200", "f"],
        1: ["r400"],
    }
    cursors = {0: 0, 1: 0}

    def scripted(pid: int, req: dict) -> dict:
        seq = script[pid]
        i = cursors[pid]
        if i >= len(seq):
            return build_response("fold")
        token = seq[i]
        cursors[pid] = i + 1
        if token == "f":
            return build_response("fold")
        if token == "k":
            return build_response("check")
        if token == "c":
            return build_response("call")
        if token.startswith("r"):
            return build_response("raise", int(token[1:]))
        return build_response("fold")

    result = asyncio.run(session.run_async(scripted))
    assert result.hands_played == 1
    assert result.hand_results[0].reason == "fold"
    # SB raised 200 (put 200 total: already 50 blind + 150), BB raised to 400,
    # SB folded → BB wins 200 (SB's contrib) after uncalled return.
    # SB contrib at fold: 200; BB had put 400 but uncalled 200 returned.
    # SB delta -200, BB +200
    assert result.hand_results[0].deltas == [-200, 200]

    # Illegal short re-raise (< 2x) → fold
    session2 = MatchSession(num_hands=1, rng=__import__("random").Random(2))
    script2 = {0: ["r200"], 1: ["r300"]}  # 300 < 400 → illegal → fold
    cursors2 = {0: 0, 1: 0}

    def scripted2(pid: int, req: dict) -> dict:
        seq = script2[pid]
        i = cursors2[pid]
        if i >= len(seq):
            return build_response("check") if req.get("to", 0) == 0 else build_response("call")
        token = seq[i]
        cursors2[pid] = i + 1
        if token.startswith("r"):
            return build_response("raise", int(token[1:]))
        return build_response("fold")

    result2 = asyncio.run(session2.run_async(scripted2))
    assert result2.hands_played == 1
    # BB's illegal raise treated as fold → SB wins
    assert result2.hand_results[0].winners == [0]
    assert result2.hand_results[0].reason == "fold"


def test_fold_ends_hand():
    session = MatchSession(num_hands=1, rng=__import__("random").Random(7))

    def sb_folds(pid: int, req: dict) -> dict:
        if pid == 0:
            return build_response("fold")
        return build_response("check")

    result = asyncio.run(session.run_async(sb_folds))
    assert result.hands_played == 1
    hr = result.hand_results[0]
    assert hr.reason == "fold"
    assert hr.winners == [1]
    assert hr.deltas == [-SMALL_BLIND, SMALL_BLIND]
    # Botzone 计分：final_chips 语义改为「累计净输赢 net」。
    # 单手 SB fold → 净输赢 = [-SB, +SB]
    assert result.final_chips == [-SMALL_BLIND, SMALL_BLIND]


def test_each_hand_resets_to_starting_stack():
    """Botzone 计分：每手开始筹码复位为 STARTING_STACK，不跨手累积。
    多手对局中每个 hand_start 事件的 chips 都是 [20000, 20000]。"""
    session = MatchSession(num_hands=3, rng=__import__("random").Random(42))
    result = asyncio.run(session.run_async(_passive_bot))
    starts = [e for e in result.events if e["type"] == "hand_start"]
    assert len(starts) == 3
    for hs in starts:
        assert hs["chips"] == [STARTING_STACK, STARTING_STACK], (
            f"每手应复位到 {STARTING_STACK}，实际 {hs['chips']}"
        )


def test_final_chips_is_cumulative_net():
    """Botzone 计分：final_chips = 各手 deltas 之和（累计净输赢），不是最终累积筹码。
    每手复位 20000 → 多手 passive bot 对局后，net = sum(deltas)。"""
    session = MatchSession(num_hands=4, rng=__import__("random").Random(42))
    result = asyncio.run(session.run_async(_passive_bot))
    expected_net = [
        sum(r.deltas[0] for r in result.hand_results),
        sum(r.deltas[1] for r in result.hand_results),
    ]
    assert result.final_chips == expected_net, (
        f"final_chips 应为累计净输赢 {expected_net}，实际 {result.final_chips}"
    )
    # match_end 事件也带 net（累计净输赢）
    me = [e for e in result.events if e["type"] == "match_end"][0]
    assert me["final_chips"] == expected_net


def test_no_early_exit_on_bust():
    """Botzone 计分：每手独立复位，一方某手筹码归零不提前结束整场。
    用 allin bot：每手可能全押归零，但下一手仍复位 20000 继续。
    num_hands=5 应全部跑完（rounds_played==5）。"""
    def allin_bot(pid: int, req: dict) -> dict:
        return build_response("allin")

    session = MatchSession(num_hands=5, rng=__import__("random").Random(7))
    result = asyncio.run(session.run_async(allin_bot))
    assert result.hands_played == 5, (
        f"每手复位不应因归零提前结束，应跑完 5 手，实际 {result.hands_played}"
    )


def test_min_raise_to_helper():
    session = MatchSession(num_hands=1, rng=__import__("random").Random(0))
    # After blinds conceptually current_bet=bb
    session._current_bet = BIG_BLIND
    assert session._compute_min_raise_to(0) == 2 * BIG_BLIND
    session._current_bet = 200
    assert session._compute_min_raise_to(0) == 400
    session._current_bet = 0
    assert session._compute_min_raise_to(0) == BIG_BLIND
