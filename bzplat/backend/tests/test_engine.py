"""Unit tests for HU NLHE engine."""

from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.games.holdem.cards import (
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
from bzplat.backend.games.holdem.engine import (
    BIG_BLIND,
    SMALL_BLIND,
    STARTING_STACK,
    Action,
    MatchSession,
)
from bzplat.backend.games.holdem.protocol import RESP_ALLIN, RESP_CALL_CHECK, RESP_FOLD


def resp(action: str, *, raise_to: int | None = None, street_bet: int = 0) -> dict:
    """构造 Botzone 协议响应信封 ``{"response": <裸整数>}``。

    - fold → -1；allin → -2；call/check → 0。
    - raise：``raise_to`` 是意图的「加注到此总额」，转换为 Botzone delta =
      raise_to - street_bet（玩家加注前已在本街投入的筹码）。
    """
    if action == "fold":
        return {"response": RESP_FOLD}
    if action == "allin":
        return {"response": RESP_ALLIN}
    if action in ("call", "check"):
        return {"response": RESP_CALL_CHECK}
    if action == "raise":
        assert raise_to is not None and raise_to > street_bet, (raise_to, street_bet)
        return {"response": raise_to - street_bet}
    raise ValueError(f"unknown action: {action}")


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
    to_call = int(req.get("to_call", req.get("to", 0)))
    if to_call <= 0:
        return resp("check")
    return resp("call")


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
    """Facing BB, raise-to 200 legal; after that min re-raise-to is 400.

    Botzone 协议：raise response 是「额外下注筹码」= raise_to_total - 加注方 street_bet。
    - SB(0) street_bet=50（盲注）raise 到 200 → delta=150。
    - BB(1) street_bet=100（盲注）raise 到 400 → delta=300。
    """
    session = MatchSession(num_hands=1, rng=__import__("random").Random(1))

    # 脚本存「意图加注到的总额」（便于读），resp() 按当前 street_bet 转 delta。
    # street_bet 从 req 推断：本手开始 = 0，盲注后 = SB 的 sb / BB 的 bb。
    script = {
        0: [("raise", 200), "f"],   # SB raise to 200, then fold
        1: [("raise", 400)],         # BB raise to 400
    }
    cursors = {0: 0, 1: 0}

    def _street_bet(pid: int, req: dict) -> int:
        # 从 history 推断本方本街已投入：盲注基础（preflop SB=50/BB=100，history 不含盲注）
        # + 本方在当前 street 的 raise delta 累加。
        my = req.get("my_id", pid)
        hist = req.get("history", [])
        # 翻前盲注基础（round 0）：SB 投了 50、BB 投了 100
        round0 = all(ev.get("round", 0) == 0 for ev in hist) if hist else True
        bet = (50 if my == 0 else 100) if round0 else 0
        for ev in hist:
            if ev.get("player_id") == my:
                a = ev.get("action")
                at = ev.get("action_type")
                if at == "raise" and isinstance(a, int) and a > 0:
                    bet += a  # delta 累加为本玩家已投入
        return bet

    def scripted(pid: int, req: dict) -> dict:
        seq = script[pid]
        i = cursors[pid]
        if i >= len(seq):
            return resp("fold")
        token = seq[i]
        cursors[pid] = i + 1
        if token == "f":
            return resp("fold")
        if token == "k":
            return resp("check")
        if token == "c":
            return resp("call")
        if isinstance(token, tuple) and token[0] == "raise":
            return resp("raise", raise_to=token[1], street_bet=_street_bet(pid, req))
        return resp("fold")

    result = asyncio.run(session.run_async(scripted))
    assert result.hands_played == 1
    assert result.hand_results[0].reason == "fold"
    # SB raised 200 (put 200 total: already 50 blind + 150), BB raised to 400,
    # SB folded → BB wins 200 (SB's contrib) after uncalled return.
    assert result.hand_results[0].deltas == [-200, 200]

    # Illegal short re-raise (< 2x) → fold
    session2 = MatchSession(num_hands=1, rng=__import__("random").Random(2))
    script2 = {0: [("raise", 200)], 1: [("raise", 300)]}  # 300 < 400 → illegal → fold
    cursors2 = {0: 0, 1: 0}

    def scripted2(pid: int, req: dict) -> dict:
        seq = script2[pid]
        i = cursors2[pid]
        if i >= len(seq):
            to_call = int(req.get("to_call", 0))
            return resp("check") if to_call == 0 else resp("call")
        token = seq[i]
        cursors2[pid] = i + 1
        if isinstance(token, tuple) and token[0] == "raise":
            return resp("raise", raise_to=token[1], street_bet=_street_bet(pid, req))
        return resp("fold")

    result2 = asyncio.run(session2.run_async(scripted2))
    assert result2.hands_played == 1
    # BB's illegal raise treated as fold → SB wins
    assert result2.hand_results[0].winners == [0]
    assert result2.hand_results[0].reason == "fold"


def test_fold_ends_hand():
    session = MatchSession(num_hands=1, rng=__import__("random").Random(7))

    def sb_folds(pid: int, req: dict) -> dict:
        if pid == 0:
            return resp("fold")
        return resp("check")

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
    """Botzone 计分：每手筹码复位为 STARTING_STACK（不跨手累积），但 hand_start 事件
    的 chips 字段反映盲注扣除后的筹码（对齐 Botzone 显示：fold 时筹码立即变化）。
    即 hand_start.chips = [STARTING_STACK - SB, STARTING_STACK - BB]。"""
    session = MatchSession(num_hands=3, rng=__import__("random").Random(42))
    result = asyncio.run(session.run_async(_passive_bot))
    starts = [e for e in result.events if e["type"] == "hand_start"]
    assert len(starts) == 3
    for hs in starts:
        sb_idx, bb_idx = hs["sb"], hs["bb"]
        expected = [0, 0]
        expected[sb_idx] = STARTING_STACK - SMALL_BLIND
        expected[bb_idx] = STARTING_STACK - BIG_BLIND
        assert hs["chips"] == expected, (
            f"hand_start chips 应为盲注后 {[STARTING_STACK-SMALL_BLIND, STARTING_STACK-BIG_BLIND]}（按 sb/bb 位），实际 {hs['chips']}"
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
        return resp("allin")

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
