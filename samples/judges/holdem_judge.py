#!/usr/bin/env python3
"""德州扑克参考裁判（独立、无平台依赖）：手牌评估 + 动作合法性。

Bot 作者可本地自测：
- 七牌最佳五牌组合的牌力比较（与本平台 games/holdem/cards.py 评估口径一致）
- raise 下限（min-raise = 2× 当前下注）等动作合法性

注意：平台裁判（games/holdem/engine.py MatchSession）还处理盲注、side pot、
all-in runout、超时 fold 等；本参考脚本只覆盖手牌评估与下注合法性核心。
"""
from __future__ import annotations

import itertools
import sys

RANK_CHARS = "23456789TJQKA"  # rank 0..12
# 牌力类别（越高越强）：高牌0 < 一对1 < 两对2 < 三条3 < 顺子4 < 同花5
#                    < 葫芦6 < 四条7 < 同花顺8
CAT_HIGH, CAT_PAIR, CAT_TWOPAIR, CAT_TRIPS, CAT_STRAIGHT = 0, 1, 2, 3, 4
CAT_FLUSH, CAT_FULLHOUSE, CAT_QUADS, CAT_STRAIGHT_FLUSH = 5, 6, 7, 8


def parse_card(s: str) -> tuple[int, int]:
    """'Th' → (rank=8, suit=1)；suit 0=♠1=♥2=♦3=♣（仅用于同花判定）。"""
    s = s.strip()
    rank = RANK_CHARS.index(s[0].upper())
    suit = "shdc".index(s[1].lower())
    return rank, suit


def _straight_high(ranks: list[int]) -> int | None:
    """5 张不同点数是否成顺；返回顺子最高点（A-2-3-4-5 返回 3，即五高）。"""
    rs = sorted(set(ranks), reverse=True)
    if len(rs) != 5:
        return None
    if rs[0] - rs[4] == 4:
        return rs[0]
    if rs == [12, 3, 2, 1, 0]:  # A-2-3-4-5
        return 3
    return None


def evaluate_5(cards: list[tuple[int, int]]) -> tuple[int, ...]:
    """5 张牌的牌力元组，元组越大越强。"""
    ranks = sorted((c[0] for c in cards), reverse=True)
    suits = [c[1] for c in cards]
    is_flush = len(set(suits)) == 1
    high = _straight_high(ranks)

    # 按点数统计
    from collections import Counter
    counts = sorted(Counter(ranks).items(), key=lambda kv: (-kv[1], -kv[0]))
    c1, c2 = counts[0][1], counts[1][1] if len(counts) > 1 else 0

    if is_flush and high is not None:
        return (CAT_STRAIGHT_FLUSH, high)
    if c1 == 4:
        return (CAT_QUADS, counts[0][0], counts[1][0])
    if c1 == 3 and c2 == 2:
        return (CAT_FULLHOUSE, counts[0][0], counts[1][0])
    if is_flush:
        return (CAT_FLUSH, *ranks)
    if high is not None:
        return (CAT_STRAIGHT, high)
    if c1 == 3:
        return (CAT_TRIPS, counts[0][0], counts[1][0], counts[2][0])
    if c1 == 2 and c2 == 2:
        return (CAT_TWOPAIR, counts[0][0], counts[1][0], counts[2][0])
    if c1 == 2:
        return (CAT_PAIR, counts[0][0], counts[1][0], counts[2][0], counts[3][0])
    return (CAT_HIGH, *ranks)


def best_hand(cards: list[tuple[int, int]]) -> tuple[int, ...]:
    """5–7 张牌的最佳五牌组合。"""
    return max(evaluate_5(list(combo)) for combo in itertools.combinations(cards, 5))


def compare(a: list[tuple[int, int]], b: list[tuple[int, int]]) -> int:
    """+1 a 胜 / -1 b 胜 / 0 平。"""
    ba, bb = best_hand(a), best_hand(b)
    return (ba > bb) - (ba < bb)


# ── 动作合法性 ──────────────────────────────────────────────────────
def legal_raise_to(current_bet: int, last_raise_to: int, bb: int) -> int:
    """最小合法 raise-to 总额：至少 2× 当前下注（首次 = 2bb）。

    平台规则（见 engine/game.py）：current_bet==0 → bb；否则 2*current_bet。
    """
    if current_bet <= 0:
        return bb
    return max(current_bet * 2, last_raise_to + (current_bet - last_raise_to) * 2)


def is_legal_action(
    action: str,
    *,
    to_call: int,
    current_bet: int,
    last_raise_to: int,
    bb: int,
    my_chips: int,
    raise_to: int | None = None,
) -> bool:
    """判断动作是否合法（简化版，不含 all-in 细节）。

    action: 'fold' | 'check' | 'call' | 'raise'
    """
    if action == "fold":
        return True
    if action == "check":
        return to_call == 0
    if action == "call":
        return to_call > 0 and my_chips >= to_call
    if action == "raise":
        if raise_to is None or raise_to <= current_bet:
            return False
        min_to = legal_raise_to(current_bet, last_raise_to, bb)
        return raise_to >= min_to and raise_to <= my_chips
    return False


def _demo() -> None:
    # 皇家同花顺 vs 四条
    a = [parse_card(c) for c in ("As", "Ks", "Qs", "Js", "Ts", "2h", "3d")]
    b = [parse_card(c) for c in ("Ac", "Ah", "Ad", "As", "Kh", "2d", "3c")]
    print("A 最佳五牌类别:", best_hand(a))
    print("B 最佳五牌类别:", best_hand(b))
    print("比较结果（+1 A胜）:", compare(a, b))
    # raise 合法性
    print("BB=100，当前下注 100 → 最小 raise-to:", legal_raise_to(100, 100, 100))
    print("raise 到 150（<200）合法？", is_legal_action(
        "raise", to_call=100, current_bet=100, last_raise_to=100, bb=100,
        my_chips=5000, raise_to=150))
    print("raise 到 300（≥200）合法？", is_legal_action(
        "raise", to_call=100, current_bet=100, last_raise_to=100, bb=100,
        my_chips=5000, raise_to=300))


if __name__ == "__main__":
    _demo()
