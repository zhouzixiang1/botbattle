"""Texas Hold'em cards, deck, and 5–7 card hand evaluation."""

from __future__ import annotations

import random
from dataclasses import dataclass
from itertools import combinations
from typing import Iterable, Sequence

RANK_CHARS = "23456789TJQKA"
SUIT_CHARS = "shdc"  # internal: 0=♠, 1=♥, 2=♦, 3=♣

# category: higher is better
CAT_HIGH = 0
CAT_PAIR = 1
CAT_TWO_PAIR = 2
CAT_TRIPS = 3
CAT_STRAIGHT = 4
CAT_FLUSH = 5
CAT_FULL_HOUSE = 6
CAT_QUADS = 7
CAT_STRAIGHT_FLUSH = 8


@dataclass(frozen=True, slots=True, order=True)
class Card:
    """A playing card. rank 0=2 … 12=A; suit 0=♠ 1=♥ 2=♦ 3=♣."""

    rank: int
    suit: int

    def __post_init__(self) -> None:
        if not (0 <= self.rank <= 12):
            raise ValueError(f"invalid rank: {self.rank}")
        if not (0 <= self.suit <= 3):
            raise ValueError(f"invalid suit: {self.suit}")

    def __str__(self) -> str:
        return f"{RANK_CHARS[self.rank]}{SUIT_CHARS[self.suit]}"

    def __repr__(self) -> str:
        return f"Card({self!s})"


class Deck:
    """52-card deck with shuffle / deal."""

    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()
        self._cards: list[Card] = [
            Card(rank, suit) for rank in range(13) for suit in range(4)
        ]

    def __len__(self) -> int:
        return len(self._cards)

    def shuffle(self) -> None:
        self._rng.shuffle(self._cards)

    def deal(self, n: int = 1) -> list[Card]:
        if n < 0 or n > len(self._cards):
            raise ValueError(f"cannot deal {n} from {len(self._cards)}")
        out = self._cards[:n]
        self._cards = self._cards[n:]
        return out

    def deal_one(self) -> Card:
        return self.deal(1)[0]

    def reset(self) -> None:
        self._cards = [Card(rank, suit) for rank in range(13) for suit in range(4)]


def _straight_high(ranks: Sequence[int]) -> int | None:
    """Return high card of a 5-card straight, or None. Wheel → 3 (five-high)."""
    uniq = sorted(set(ranks))
    if len(uniq) != 5:
        return None
    # A-2-3-4-5
    if uniq == [0, 1, 2, 3, 12]:
        return 3
    if uniq[-1] - uniq[0] == 4 and len(uniq) == 5:
        return uniq[-1]
    return None


def evaluate_5(cards: Sequence[Card]) -> tuple[int, ...]:
    """Score a 5-card hand. Higher tuple wins."""
    if len(cards) != 5:
        raise ValueError("evaluate_5 requires exactly 5 cards")
    ranks = sorted((c.rank for c in cards), reverse=True)
    suits = [c.suit for c in cards]
    is_flush = len(set(suits)) == 1
    straight_hi = _straight_high(ranks)

    # rank counts
    counts: dict[int, int] = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    by_count = sorted(counts.items(), key=lambda kv: (kv[1], kv[0]), reverse=True)
    pattern = sorted(counts.values(), reverse=True)

    if is_flush and straight_hi is not None:
        return (CAT_STRAIGHT_FLUSH, straight_hi)
    if pattern == [4, 1]:
        quad = by_count[0][0]
        kicker = by_count[1][0]
        return (CAT_QUADS, quad, kicker)
    if pattern == [3, 2]:
        trips = by_count[0][0]
        pair = by_count[1][0]
        return (CAT_FULL_HOUSE, trips, pair)
    if is_flush:
        return (CAT_FLUSH, *ranks)
    if straight_hi is not None:
        return (CAT_STRAIGHT, straight_hi)
    if pattern == [3, 1, 1]:
        trips = by_count[0][0]
        kickers = sorted((r for r, c in counts.items() if c == 1), reverse=True)
        return (CAT_TRIPS, trips, *kickers)
    if pattern == [2, 2, 1]:
        pairs = sorted((r for r, c in counts.items() if c == 2), reverse=True)
        kicker = next(r for r, c in counts.items() if c == 1)
        return (CAT_TWO_PAIR, pairs[0], pairs[1], kicker)
    if pattern == [2, 1, 1, 1]:
        pair = by_count[0][0]
        kickers = sorted((r for r, c in counts.items() if c == 1), reverse=True)
        return (CAT_PAIR, pair, *kickers)
    return (CAT_HIGH, *ranks)


def evaluate(cards: Sequence[Card]) -> tuple[int, ...]:
    """Best 5-card score from 5–7 cards. Higher tuple wins."""
    n = len(cards)
    if n < 5 or n > 7:
        raise ValueError(f"evaluate requires 5–7 cards, got {n}")
    if n == 5:
        return evaluate_5(cards)
    best: tuple[int, ...] | None = None
    for combo in combinations(cards, 5):
        score = evaluate_5(combo)
        if best is None or score > best:
            best = score
    assert best is not None
    return best


def compare_hands(a: Sequence[Card], b: Sequence[Card]) -> int:
    """Compare two 5–7 card holdings. +1 if a wins, -1 if b wins, 0 tie."""
    sa, sb = evaluate(a), evaluate(b)
    if sa > sb:
        return 1
    if sa < sb:
        return -1
    return 0


def cards_to_str(cards: Iterable[Card]) -> str:
    return " ".join(str(c) for c in cards)
