"""System-owned poker facts and evaluators loaded once per policy worker.

The module is deliberately stdlib-only and performs no file I/O.  Candidate
``policy.py`` may consume these immutable tables and pure helpers, but cannot
replace them.  Card ids use the national protocol rank/suit space:
``card_id = rank_index * 4 + suit`` where rank 0 is deuce and 12 is ace.
"""

from __future__ import annotations

import hashlib
import itertools
import json


PRECOMPUTE_SCHEMA_VERSION = 4
CARD_ENCODING = "national_tcp_card_id_v1:card_id=rank_index*4+suit"
GENERATOR_VERSION = "national-precompute-v3"
PREFLOP_EQUITY_METHOD = "fixed_seed_uniform_opponent_board_mc_v1"
PREFLOP_EQUITY_SAMPLES_PER_CLASS = 65_536
PREFLOP_EQUITY_BASE_SEED = 0x4E4154494F4E414C
PREFLOP_EQUITY_CLASS_SEED_DERIVATION = (
    "base_seed_xor_uint64(class_index*0x9e3779b97f4a7c15)"
)
PREFLOP_EQUITY_DRAW_CONTRACT = (
    "python_random_sample_without_replacement_7:opponent2_then_board5"
)
PREFLOP_EQUITY_BUILD_RUNTIME = "CPython-3.14.4"
PREFLOP_EQUITY_RANDOM_SOURCE_SHA256 = (
    "62dca8cdae7482513b99bb093ff038afd5131954e7eb78166d673a772cee871c"
)
PREFLOP_EQUITY_EVALUATOR_SOURCE = "sever/engine/evaluator.py"
PREFLOP_EQUITY_EVALUATOR_SHA256 = (
    "9992ee2608db9aef0320a586117f9ced8bdf33ad79581b9356686210cabd425f"
)
PREFLOP_EQUITY_CARD_SOURCE = "sever/engine/deck.py"
PREFLOP_EQUITY_CARD_SOURCE_SHA256 = (
    "8afb902bc936bca5659997e9b36a923d69304946f5659b35c054cd8c702851d5"
)
PREFLOP_EQUITY_GENERATOR_SHA256 = (
    "5aa6808974f9af67ac7bb5189c431791d9aed9e791869f9428b1ab8e04cf62d3"
)
FULL_DECK = tuple(range(52))
FIVE_OF_SEVEN_INDICES = tuple(itertools.combinations(range(7), 5))
RANK_SYMBOLS = "23456789TJQKA"


def card_id(suit: int, rank_index: int) -> int:
    suit, rank_index = int(suit), int(rank_index)
    if not 0 <= suit < 4 or not 0 <= rank_index < 13:
        raise ValueError("national card outside suit=0..3/rank=0..12")
    return rank_index * 4 + suit


def card_parts(card: int) -> tuple[int, int]:
    card = int(card)
    if not 0 <= card < 52:
        raise ValueError("card id outside 0..51")
    return card % 4, card // 4


def _hole_fact(card_a: int, card_b: int) -> tuple[int, int, bool, bool, int]:
    rank_a, rank_b = card_a // 4 + 2, card_b // 4 + 2
    high, low = max(rank_a, rank_b), min(rank_a, rank_b)
    return high, low, card_a % 4 == card_b % 4, high == low, high - low


def _hole_class_index(card_a: int, card_b: int) -> int:
    rank_a, rank_b = card_a // 4, card_b // 4
    high, low = max(rank_a, rank_b), min(rank_a, rank_b)
    if high == low:
        row, column = high, high
    elif card_a % 4 == card_b % 4:
        row, column = high, low
    else:
        row, column = low, high
    return row * 13 + column


def _preflop_bucket(card_a: int, card_b: int) -> str:
    rank_a, rank_b = card_a // 4 + 2, card_b // 4 + 2
    high, low = max(rank_a, rank_b), min(rank_a, rank_b)
    suited = card_a % 4 == card_b % 4
    if high == low:
        return "premium_pair" if high >= 10 else "small_pair"
    if high == 14 and low >= 10:
        return "ace_broadway"
    if low >= 10:
        return "broadway"
    if suited and high - low <= 2:
        return "suited_connector"
    if suited and high == 14:
        return "suited_ace"
    if not suited and high == 14:
        return "offsuit_ace"
    if suited:
        return "suited_other"
    return "offsuit_other"


def _straight_high(rank_mask: int) -> int:
    mask = int(rank_mask) & 0x1FFF
    for high_index in range(12, 3, -1):
        window = 0b11111 << (high_index - 4)
        if mask & window == window:
            return high_index + 2
    wheel = (1 << 12) | 0b1111
    return 5 if mask & wheel == wheel else 0


HOLE_COMBO_FACTS = {
    (card_a, card_b): _hole_fact(card_a, card_b)
    for card_a in range(52)
    for card_b in range(card_a + 1, 52)
}
HOLE_CLASS_INDEX_BY_COMBO = {
    key: _hole_class_index(*key) for key in HOLE_COMBO_FACTS
}
HOLE_BUCKET_BY_COMBO = {
    key: _preflop_bucket(*key) for key in HOLE_COMBO_FACTS
}
STRAIGHT_HIGH_BY_MASK = {
    rank_mask: _straight_high(rank_mask)
    for rank_mask in range(1 << 13)
}
# Compact system-owned facts generated offline with the evaluator below.  For
# each canonical 169 class, a fixed per-class seed samples 65,536 uniformly
# random opponent-hole/board completions.  Runtime performs no simulation or
# file I/O; the literal is content-bound by PRECOMPUTE_MANIFEST.  Rows/columns
# retain `_hole_class_index`: diagonal=pair, lower triangle=suited, upper
# triangle=offsuit.
PREFLOP_CLASS_EQUITY = (
    0.505562, 0.322670, 0.331421, 0.345146, 0.342972, 0.341774, 0.368095, 0.387222, 0.416359, 0.442924, 0.474236, 0.501106, 0.551247,
    0.357185, 0.538025, 0.349358, 0.362289, 0.359276, 0.367126, 0.373817, 0.400490, 0.424042, 0.453644, 0.481262, 0.517700, 0.557213,
    0.372108, 0.386574, 0.569916, 0.382294, 0.378036, 0.381859, 0.395424, 0.407585, 0.436333, 0.460594, 0.491196, 0.519691, 0.565475,
    0.381386, 0.397804, 0.415733, 0.604012, 0.400528, 0.405571, 0.414703, 0.423820, 0.441399, 0.472992, 0.503845, 0.533699, 0.579208,
    0.379669, 0.395454, 0.412865, 0.428802, 0.632370, 0.423744, 0.432610, 0.443848, 0.462898, 0.480812, 0.508186, 0.545547, 0.576149,
    0.385216, 0.398239, 0.418434, 0.435295, 0.454697, 0.663002, 0.451591, 0.460457, 0.479599, 0.494904, 0.518990, 0.549400, 0.584755,
    0.406075, 0.405975, 0.426208, 0.443024, 0.466362, 0.483116, 0.690964, 0.483467, 0.498352, 0.517311, 0.535866, 0.561302, 0.599449,
    0.420876, 0.432449, 0.436714, 0.457077, 0.476410, 0.493073, 0.507797, 0.721916, 0.515305, 0.533386, 0.554527, 0.579147, 0.604622,
    0.449333, 0.457863, 0.466248, 0.473595, 0.489662, 0.509956, 0.522110, 0.540840, 0.749001, 0.552902, 0.572510, 0.598198, 0.627930,
    0.476631, 0.482178, 0.490730, 0.500206, 0.505775, 0.519569, 0.537994, 0.554581, 0.574120, 0.775276, 0.581406, 0.603699, 0.635590,
    0.502884, 0.512283, 0.521706, 0.530334, 0.535919, 0.542717, 0.563805, 0.575523, 0.594666, 0.602203, 0.797539, 0.612953, 0.642410,
    0.532730, 0.541290, 0.545059, 0.558815, 0.565666, 0.573570, 0.579514, 0.599289, 0.617752, 0.626503, 0.634270, 0.821808, 0.652756,
    0.572975, 0.580811, 0.588860, 0.601120, 0.597969, 0.609055, 0.619118, 0.624474, 0.645691, 0.655029, 0.662453, 0.668808, 0.853325,
)


def _validated_cards(cards, expected=None) -> tuple[int, ...]:
    result = tuple(int(card) for card in cards)
    if expected is not None and len(result) != int(expected):
        raise ValueError(f"expected {expected} cards, got {len(result)}")
    if len(result) < 5 or len(result) > 7:
        raise ValueError("hand evaluator requires five through seven cards")
    if any(card < 0 or card >= 52 for card in result):
        raise ValueError("card id outside 0..51")
    if len(set(result)) != len(result):
        raise ValueError("duplicate card in hand")
    return result


def _evaluate_five_unchecked(cards) -> tuple:
    ranks = sorted((card // 4 for card in cards), reverse=True)
    suits = [card % 4 for card in cards]
    counts = {}
    rank_mask = 0
    for rank in ranks:
        counts[rank] = counts.get(rank, 0) + 1
        rank_mask |= 1 << rank
    groups = sorted(counts.items(), key=lambda item: (item[1], item[0]), reverse=True)
    pattern = tuple(item[1] for item in groups)
    kickers = tuple(item[0] for item in groups)
    flush = len(set(suits)) == 1
    straight = STRAIGHT_HIGH_BY_MASK[rank_mask]
    # ``straight_high`` stores natural rank values 5..14; sever's evaluator
    # uses protocol rank indices 3..12, hence the subtraction here.
    straight_high = straight - 2 if straight else 0
    if flush and straight:
        return (9, straight_high)
    if pattern == (4, 1):
        return (8, kickers)
    if pattern == (3, 2):
        return (7, kickers)
    if flush:
        return (6, tuple(ranks))
    if straight:
        return (5, straight_high)
    if pattern == (3, 1, 1):
        return (4, kickers)
    if pattern == (2, 2, 1):
        return (3, kickers)
    if pattern == (2, 1, 1, 1):
        return (2, kickers)
    return (1, tuple(ranks))


def evaluate_five(cards) -> tuple:
    """Return the complete, directly comparable five-card rank tuple."""

    return _evaluate_five_unchecked(_validated_cards(cards, 5))


def best_hand_rank(cards) -> tuple:
    """Return the best five-card rank from a valid five-, six-, or seven-card set."""

    cards = _validated_cards(cards)
    if len(cards) == 5:
        return _evaluate_five_unchecked(cards)
    indices = (
        FIVE_OF_SEVEN_INDICES
        if len(cards) == 7
        else itertools.combinations(range(len(cards)), 5)
    )
    return max(
        _evaluate_five_unchecked(tuple(cards[index] for index in selected))
        for selected in indices
    )


def evaluate_seven(cards) -> tuple:
    return best_hand_rank(_validated_cards(cards, 7))


def compare_hands(left, right) -> int:
    left_rank, right_rank = best_hand_rank(left), best_hand_rank(right)
    return (left_rank > right_rank) - (left_rank < right_rank)


def deck_without(excluded=()) -> tuple[int, ...]:
    excluded = tuple(int(card) for card in excluded)
    if any(card < 0 or card >= 52 for card in excluded):
        raise ValueError("excluded card id outside 0..51")
    if len(set(excluded)) != len(excluded):
        raise ValueError("duplicate excluded card")
    blocked = set(excluded)
    return tuple(card for card in FULL_DECK if card not in blocked)


def deterministic_draw(deck, count: int, state: int) -> tuple[tuple[int, ...], int]:
    """Draw without replacement using a stable xorshift64 stream."""

    pool = list(deck)
    count = int(count)
    if count < 0 or count > len(pool):
        raise ValueError("draw count outside deck")
    state = int(state) & 0xFFFFFFFFFFFFFFFF or 0x9E3779B97F4A7C15
    for offset in range(count):
        state ^= (state << 13) & 0xFFFFFFFFFFFFFFFF
        state ^= state >> 7
        state ^= (state << 17) & 0xFFFFFFFFFFFFFFFF
        state &= 0xFFFFFFFFFFFFFFFF
        selected = offset + state % (len(pool) - offset)
        pool[offset], pool[selected] = pool[selected], pool[offset]
    return tuple(pool[:count]), state


def _content_digest() -> str:
    payload = {
        "five_of_seven": FIVE_OF_SEVEN_INDICES,
        "hole_combo_facts": sorted((list(key), list(value)) for key, value in HOLE_COMBO_FACTS.items()),
        "hole_class_indices": sorted((list(key), value) for key, value in HOLE_CLASS_INDEX_BY_COMBO.items()),
        "hole_buckets": sorted((list(key), value) for key, value in HOLE_BUCKET_BY_COMBO.items()),
        "preflop_class_equity": PREFLOP_CLASS_EQUITY,
        "straight_high": [STRAIGHT_HIGH_BY_MASK[index] for index in range(1 << 13)],
    }
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("ascii")
    ).hexdigest()


PRECOMPUTE_MANIFEST = {
    "schema_version": PRECOMPUTE_SCHEMA_VERSION,
    "generator_version": GENERATOR_VERSION,
    "card_encoding": CARD_ENCODING,
    "hole_combo_entries": len(HOLE_COMBO_FACTS),
    "hole_class_entries": len(PREFLOP_CLASS_EQUITY),
    "hole_bucket_entries": len(HOLE_BUCKET_BY_COMBO),
    "preflop_equity_method": PREFLOP_EQUITY_METHOD,
    "preflop_equity_samples_per_class": PREFLOP_EQUITY_SAMPLES_PER_CLASS,
    "preflop_equity_base_seed": PREFLOP_EQUITY_BASE_SEED,
    "preflop_equity_class_seed_derivation": (
        PREFLOP_EQUITY_CLASS_SEED_DERIVATION
    ),
    "preflop_equity_draw_contract": PREFLOP_EQUITY_DRAW_CONTRACT,
    "preflop_equity_build_runtime": PREFLOP_EQUITY_BUILD_RUNTIME,
    "preflop_equity_random_source_sha256": (
        PREFLOP_EQUITY_RANDOM_SOURCE_SHA256
    ),
    "preflop_equity_evaluator_source": PREFLOP_EQUITY_EVALUATOR_SOURCE,
    "preflop_equity_evaluator_sha256": PREFLOP_EQUITY_EVALUATOR_SHA256,
    "preflop_equity_card_source": PREFLOP_EQUITY_CARD_SOURCE,
    "preflop_equity_card_source_sha256": PREFLOP_EQUITY_CARD_SOURCE_SHA256,
    "preflop_equity_generator_sha256": PREFLOP_EQUITY_GENERATOR_SHA256,
    "straight_mask_entries": len(STRAIGHT_HIGH_BY_MASK),
    "five_of_seven_entries": len(FIVE_OF_SEVEN_INDICES),
    # These are system-provided domain facts.  They may accelerate a live
    # decision, but a plan may not claim them alone as a state-learning
    # innovation; the runtime probe must still prove value-sensitive wire
    # influence for any selected precompute primary.
    "foundation_pure_facts": [
        "HOLE_COMBO_FACTS",
        "HOLE_CLASS_INDEX_BY_COMBO",
        "HOLE_BUCKET_BY_COMBO",
        "PREFLOP_CLASS_EQUITY",
        "STRAIGHT_HIGH_BY_MASK",
        "FIVE_OF_SEVEN_INDICES",
    ],
    "content_digest": _content_digest(),
}


def hole_combo_fact(card_a: int, card_b: int):
    key = (card_a, card_b) if card_a < card_b else (card_b, card_a)
    return HOLE_COMBO_FACTS.get(key)


def hole_class_index(card_a: int, card_b: int) -> int:
    key = (card_a, card_b) if card_a < card_b else (card_b, card_a)
    if key not in HOLE_CLASS_INDEX_BY_COMBO:
        raise ValueError("hole cards must be two distinct card ids")
    return HOLE_CLASS_INDEX_BY_COMBO[key]


def preflop_equity(card_a: int, card_b: int) -> float:
    return PREFLOP_CLASS_EQUITY[hole_class_index(card_a, card_b)]


def preflop_bucket(card_a: int, card_b: int) -> str:
    key = (card_a, card_b) if card_a < card_b else (card_b, card_a)
    if key not in HOLE_BUCKET_BY_COMBO:
        raise ValueError("hole cards must be two distinct card ids")
    return HOLE_BUCKET_BY_COMBO[key]


def straight_high(rank_mask: int) -> int:
    return STRAIGHT_HIGH_BY_MASK.get(int(rank_mask) & 0x1FFF, 0)
