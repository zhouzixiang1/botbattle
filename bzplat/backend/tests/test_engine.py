"""Unit tests for HU NLHE engine."""

from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.games.holdem.holdem_judge import (
    Card,
    HandType,
    Suit,
    compare_full_cards,
    hand_type_of_cards,
)
from bzplat.backend.games.holdem.engine import (
    BIG_BLIND,
    SMALL_BLIND,
    STARTING_STACK,
    Action,
    MatchSession,
    generate_deal_sequence,
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


def test_hand_evaluation_pairwise():
    """裁判 holdem_judge 牌型评估（hand_type_of_cards + compare_full_cards）。"""
    # Royal / straight flush beats quads
    sf = [Card(Suit.SPADE, 14), Card(Suit.SPADE, 13), Card(Suit.SPADE, 12), Card(Suit.SPADE, 11), Card(Suit.SPADE, 10)]
    quads = [Card(Suit.SPADE, 14), Card(Suit.HEART, 14), Card(Suit.DIAMOND, 14), Card(Suit.CLUB, 14), Card(Suit.SPADE, 4)]
    assert hand_type_of_cards(sf) == HandType.STRAIGHT_FLUSH
    assert hand_type_of_cards(quads) == HandType.FOUR_OF_A_KIND
    assert compare_full_cards(sf, quads) > 0

    # Full house beats flush
    fh = [Card(Suit.SPADE, 10), Card(Suit.HEART, 10), Card(Suit.DIAMOND, 10), Card(Suit.SPADE, 5), Card(Suit.HEART, 5)]
    fl = [Card(Suit.HEART, 14), Card(Suit.HEART, 12), Card(Suit.HEART, 9), Card(Suit.HEART, 6), Card(Suit.HEART, 4)]
    assert hand_type_of_cards(fh) == HandType.FULL_HOUSE
    assert hand_type_of_cards(fl) == HandType.FLUSH
    assert compare_full_cards(fh, fl) > 0

    # Straight beats trips
    st = [Card(Suit.SPADE, 6), Card(Suit.HEART, 7), Card(Suit.DIAMOND, 8), Card(Suit.SPADE, 9), Card(Suit.HEART, 10)]
    tr = [Card(Suit.SPADE, 11), Card(Suit.HEART, 11), Card(Suit.DIAMOND, 11), Card(Suit.SPADE, 4), Card(Suit.HEART, 5)]
    assert hand_type_of_cards(st) == HandType.STRAIGHT
    assert hand_type_of_cards(tr) == HandType.THREE_OF_A_KIND
    assert compare_full_cards(st, tr) > 0

    # Two pair beats pair
    tp = [Card(Suit.SPADE, 12), Card(Suit.HEART, 12), Card(Suit.SPADE, 7), Card(Suit.HEART, 7), Card(Suit.SPADE, 4)]
    op = [Card(Suit.SPADE, 14), Card(Suit.HEART, 14), Card(Suit.SPADE, 10), Card(Suit.HEART, 9), Card(Suit.SPADE, 5)]
    assert hand_type_of_cards(tp) == HandType.TWO_PAIR
    assert hand_type_of_cards(op) == HandType.PAIR
    assert compare_full_cards(tp, op) > 0

    # Wheel straight（修复点 1：A-2-3-4-5 = 5-high straight）
    wheel = [Card(Suit.SPADE, 14), Card(Suit.HEART, 2), Card(Suit.DIAMOND, 3), Card(Suit.SPADE, 4), Card(Suit.HEART, 5)]
    assert hand_type_of_cards(wheel) == HandType.STRAIGHT

    # 7-card: best five（取最佳五牌）
    seven = [
        Card(Suit.SPADE, 14), Card(Suit.HEART, 14), Card(Suit.DIAMOND, 14),
        Card(Suit.SPADE, 7), Card(Suit.HEART, 7), Card(Suit.CLUB, 4), Card(Suit.DIAMOND, 5),
    ]
    ht, _ = __import__("bzplat.backend.games.holdem.holdem_judge", fromlist=["find_max_hand_type"]).find_max_hand_type(seven)
    assert ht == HandType.FULL_HOUSE


def _passive_bot(player_idx: int, req: dict) -> dict:
    """Always check if possible else call; never raise."""
    to_call = int(req.get("to_call", req.get("to", 0)))
    if to_call <= 0:
        return resp("check")
    return resp("call")


def test_short_match_check_call_bots():
    session = MatchSession(num_hands=2, rng=__import__("random").Random(42))
    result = asyncio.run(session.run_async(_passive_bot))
    assert result.rounds_played == 2
    assert len(result.rounds) == 2
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


def test_duplicate_deal_sequence_uses_only_canonical_card_encoding():
    """duplicate 与普通发牌共用 ``0♥1♦2♠3♣`` 整数编码。"""
    first = generate_deal_sequence(3, seed=20260809)
    second = generate_deal_sequence(3, seed=20260809)
    assert first == second
    assert all(sorted(hand) == list(range(52)) for hand in first)

    # 用顺序牌直接驱动一手：座0 依次收 0=2♥、1=2♦，座1 收
    # 2=2♠、3=2♣。旧 0♠1♥2♦3♣ 转换路径会使该断言失败。
    session = MatchSession(num_hands=1, deal_sequence=[list(range(52))])
    result = asyncio.run(session.run_async(lambda _seat, _req: {"response": RESP_FOLD}))
    holes = next(event["holes"] for event in result.events if event["type"] == "deal_hole")
    assert holes == [["2h", "2d"], ["2s", "2c"]]
    assert result.winner == 1
    assert result.final_chips == [-SMALL_BLIND, SMALL_BLIND]


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
    assert result.rounds_played == 1
    assert result.rounds[0].reason == "fold"
    # SB raised 200 (put 200 total: already 50 blind + 150), BB raised to 400,
    # SB folded → BB wins 200 (SB's contrib) after uncalled return.
    assert result.rounds[0].deltas == [-200, 200]

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
    assert result2.rounds_played == 1
    # BB's illegal raise treated as fold → SB wins
    assert result2.rounds[0].winners == [0]
    assert result2.rounds[0].reason == "fold"


def test_fold_ends_hand():
    session = MatchSession(num_hands=1, rng=__import__("random").Random(7))

    def sb_folds(pid: int, req: dict) -> dict:
        if pid == 0:
            return resp("fold")
        return resp("check")

    result = asyncio.run(session.run_async(sb_folds))
    assert result.rounds_played == 1
    hr = result.rounds[0]
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
        sum(r.deltas[0] for r in result.rounds),
        sum(r.deltas[1] for r in result.rounds),
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
    assert result.rounds_played == 5, (
        f"每手复位不应因归零提前结束，应跑完 5 手，实际 {result.rounds_played}"
    )


def test_min_raise_rule_in_judge():
    """min re-raise-to = 2 × round_raise（裁判机制，原适配层 _compute_min_raise_to 已移除）。

    裁判 player_action raise 分支要求 bet + street_bet >= round_raise * 2。
    盲注后 round_raise = BB（100），SB street_bet=50 → min delta = 200-50 = 150
    （即 raise-to 200 = 2×100）。"""
    from bzplat.backend.games.holdem.holdem_judge import Holdem

    # 非法 raise（delta 不达 min）→ 裁判抛 ValueError
    judge = Holdem(player_chips=[20000, 20000], dealer_idx=0)
    judge.set_deck_from_str(["As"] * 8)
    judge.deal_cards_and_blind()
    # SB raise delta=100 → raise-to=150 < 2*round_raise(100)=200 → INVALID
    with pytest.raises(ValueError):
        judge.player_action(100)  # delta=100, street_bet=50 → 150 < 200
    # 合法 raise delta=150 → raise-to=200 = 2*100 → OK
    judge.player_action(150)


# ─── 对抗审计 P0 回归测试 ───────────────────────────────────────────────

def test_allin_runout_emits_deal_board_for_all_streets():
    """P0：all-in runout 必须补 emit deal_board（flop/turn/river 三事件）。

    两人全下后裁判内部递归 _next_round 连发板子，适配层 diff public_cards 长度
    补 emit。若 _emit_runout_deal_boards 的 idx 阶梯有 off-by-one，会漏发/重发。
    """
    import random as _r

    def allin_both(pid, req):
        return resp("allin")

    session = MatchSession(num_hands=1, rng=_r.Random(7))
    result = asyncio.run(session.run_async(allin_both))
    boards = [e for e in result.events if e["type"] == "deal_board"]
    # all-in runout 应发 flop(3) + turn(1) + river(1) = 3 个 deal_board 事件
    assert len(boards) == 3, f"all-in runout 应发 3 个 deal_board，实际 {len(boards)}"
    # 街道顺序 + 增量正确
    assert boards[0]["street"] == "flop"
    assert len(boards[0]["dealt"]) == 3
    assert boards[1]["street"] == "turn"
    assert len(boards[1]["dealt"]) == 1
    assert boards[2]["street"] == "river"
    assert len(boards[2]["dealt"]) == 1
    # 最终 board 5 张
    settle = next(e for e in result.events if e["type"] == "settle")
    assert len(settle["board"]) == 5


def test_holdem_event_contract_keys():
    """P0：holdem 6 类事件 dict 键契约守护（前端 reducer 读这些键，drift 会静默破坏 UI）。

    任何键增删都会导致前端 reducer 读到 undefined。本测试锚定各事件类型必须含的键集。
    """
    import random as _r

    def call_bot(pid, req):
        return resp("call")

    session = MatchSession(num_hands=1, rng=_r.Random(1))
    result = asyncio.run(session.run_async(call_bot))
    by_type = {}
    for e in result.events:
        by_type.setdefault(e["type"], []).append(e)

    # 事件类型齐全
    expected_types = {"hand_start", "deal_hole", "action", "deal_board", "settle", "match_end"}
    assert expected_types <= set(by_type.keys()), f"缺事件类型: {expected_types - set(by_type.keys())}"

    # 各事件必须含的键（前端 reducer 读的子集）
    key_contract = {
        "hand_start": {"hand", "sb", "bb", "chips"},
        "deal_hole": {"hand", "holes"},
        "deal_board": {"hand", "street", "board", "dealt"},
        "action": {"hand", "player", "action", "amount"},
        "settle": {"hand", "winners", "deltas", "chips", "net", "pot", "board", "reason"},
        "match_end": {"hands_played", "final_chips", "winner", "reason"},
    }
    for etype, required in key_contract.items():
        for ev in by_type[etype]:
            missing = required - set(ev.keys())
            assert not missing, f"{etype} 事件缺键: {missing}（前端 reducer 会读 undefined）"


def test_holdem_crash_loser_identified():
    """P1：bot 崩溃 → 判负（崩溃方 net 扣全筹码，对手得全筹码）。

    _call_decide 抛 BotCrashedError 时，_current_actor 必须是崩溃方。
    """
    import random as _r
    from bzplat.backend.runtime.binary_runner import BotCrashedError

    call_count = {0: 0, 1: 0}

    def crashing_bot(pid, req):
        call_count[pid] += 1
        # 座 1 第 2 次调用时崩溃
        if pid == 1 and call_count[1] >= 2:
            raise BotCrashedError("test crash")
        return resp("call")

    session = MatchSession(num_hands=3, rng=_r.Random(1))
    result = asyncio.run(session.run_async(crashing_bot))
    # 崩溃方(座1) net 扣 STARTING_STACK，对手(座0) 得 STARTING_STACK
    assert result.final_chips == [STARTING_STACK, -STARTING_STACK], f"crash net: {result.final_chips}"
    me = next(e for e in result.events if e["type"] == "match_end")
    assert me["reason"] == "crash"
    assert me["winner"] == 0  # 座 1 崩溃 → 座 0 胜
