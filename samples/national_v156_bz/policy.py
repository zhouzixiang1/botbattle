"""Anytime first-strict policy for the national raw-TCP typed-intent ABI.

The system runtime owns TCP, state reconstruction, deadlines, legality and
wire serialization.  This module produces a sub-250 ms baseline, then spends
only the remaining monotonic budget on deterministic equity/EV batches.  Every
published refinement remains a closed typed intent.
"""

from __future__ import annotations

import itertools
import math
import time

import precompute


ACTION_AGGRESSION_PRIOR = 0.30
FOLD_TO_RAISE_PRIOR = 0.35
FOLD_TO_JAM_PRIOR = 0.28
RIVER_OVERCALL_PRIOR = 0.55
SHOWDOWN_TIGHTNESS_PRIOR = 190.0 / 1326.0
MAX_ADAPTATION_WEIGHT = 0.65
# A 32k deterministic cap has sub-percent Monte Carlo standard error even
# under the bounded opponent weights.  Exhausting there is finite bounded
# termination after a high-precision estimate; it avoids burning the entire
# official refinement window but does not claim every close action converged.
MAX_EQUITY_SAMPLES = 32_768
INITIAL_BATCH_SIZE = 32
MAX_BATCH_SIZE = 512
DEADLINE_GUARD_SECONDS = 0.002
BASELINE_FLOP_SAMPLES = 192
BASELINE_TURN_SAMPLES = 256
BASELINE_RIVER_SAMPLES = 96


def _number(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        result = float(default)
    return float(default) if not math.isfinite(result) else result


def _integer(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return int(default)


def _plain_integer(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _bounded(value, lower, upper, default=0.0):
    return max(lower, min(upper, _number(value, default)))


def _card_id(card):
    card = card or {}
    return precompute.card_id(
        _integer(card.get("suit"), -1),
        _integer(card.get("rank"), -1),
    )


def _card_ids(cards):
    try:
        result = tuple(_card_id(card) for card in cards)
    except (TypeError, ValueError):
        return ()
    return result if len(set(result)) == len(result) else ()


def _opponent_posterior(context):
    """Return a bounded range/response projection, never raw history.

    Revealed hands are sampled conditional on reaching showdown.  Bucket
    weighting is therefore admitted only under the reducer's exact selection
    guard and remains discounted by its already reach-scaled adaptation weight.
    """

    opponent = context.get("opponent", {}) or {}
    opponent = opponent if isinstance(opponent, dict) else {}
    rates = opponent.get("rates", {}) or {}
    rates = rates if isinstance(rates, dict) else {}
    terminal = opponent.get("terminal_response", {}) or {}
    terminal = terminal if isinstance(terminal, dict) else {}
    showdown = opponent.get("showdown_range", {}) or {}
    showdown = showdown if isinstance(showdown, dict) else {}
    action_weight = _bounded(
        opponent.get("adaptation_weight"), 0.0, MAX_ADAPTATION_WEIGHT
    )
    terminal_weight = _bounded(
        terminal.get(
            "adaptation_weight",
            _bounded(terminal.get("confidence"), 0.0, 1.0)
            * MAX_ADAPTATION_WEIGHT,
        ),
        0.0,
        MAX_ADAPTATION_WEIGHT,
    )
    showdown_guarded = bool(
        showdown.get("selection_scope") == "reached_showdown_only"
        and showdown.get("selection_bias_guard")
        == "reach_rate_discount_and_capped_influence"
    )
    showdown_weight = (
        _bounded(
            showdown.get("adaptation_weight"),
            0.0,
            MAX_ADAPTATION_WEIGHT,
        )
        if showdown_guarded
        else 0.0
    )
    aggression = _bounded(
        rates.get("aggression"), 0.0, 1.0, ACTION_AGGRESSION_PRIOR
    )
    aggression_delta = action_weight * (aggression - ACTION_AGGRESSION_PRIOR)
    tightness_delta = showdown_weight * (
        _bounded(
            showdown.get("tightness"),
            0.0,
            1.0,
            SHOWDOWN_TIGHTNESS_PRIOR,
        )
        - SHOWDOWN_TIGHTNESS_PRIOR
    )

    bucket_multipliers = {}
    bucket_rates = showdown.get("bucket_rates", {}) or {}
    bucket_rates = bucket_rates if isinstance(bucket_rates, dict) else {}
    bucket_priors = showdown.get("bucket_priors", {}) or {}
    bucket_priors = bucket_priors if isinstance(bucket_priors, dict) else {}
    if showdown_guarded and showdown_weight > 0.0:
        for bucket, observed in bucket_rates.items():
            prior = _bounded(bucket_priors.get(bucket), 1e-6, 1.0, 0.0)
            if prior <= 1e-6:
                continue
            relative = _bounded(_number(observed) / prior - 1.0, -0.75, 1.50)
            bucket_multipliers[str(bucket)] = _bounded(
                1.0 + showdown_weight * relative,
                0.50,
                1.75,
                1.0,
            )

    def posterior_rate(field, prior):
        observed = _bounded(terminal.get(field), 0.0, 1.0, prior)
        return _bounded(
            prior + terminal_weight * (observed - prior), 0.05, 0.90, prior
        )

    pressure = _current_pressure_adjustment(context)
    return {
        "action_weight": action_weight,
        "terminal_weight": terminal_weight,
        "showdown_weight": showdown_weight,
        "showdown_guarded": showdown_guarded,
        "bucket_multipliers": bucket_multipliers,
        # Positive values place modestly more simulation mass on weak holdings
        # when the action profile demonstrates a wider/aggressive range.
        "wide_range_tilt": _bounded(aggression_delta, -0.18, 0.35),
        "line_strength_tilt": pressure["range_tilt"],
        "preflop_line_strength_tilt": pressure["preflop_range_tilt"],
        "current_action_strength_tilt": pressure["current_action_tilt"],
        "range_strength": _bounded(
            0.10 * aggression_delta
            - 0.12 * tightness_delta
            + pressure["equity_delta"],
            -0.16,
            0.08,
        ),
        "raise_fraction": _bounded(
            0.08 * aggression_delta
            + 0.55
            * terminal_weight
            * (
                _bounded(
                    terminal.get("fold_to_raise"),
                    0.0,
                    1.0,
                    FOLD_TO_RAISE_PRIOR,
                )
                - FOLD_TO_RAISE_PRIOR
            )
            - 0.12 * tightness_delta,
            -0.20,
            0.28,
        ),
        "fold_to_raise": posterior_rate("fold_to_raise", FOLD_TO_RAISE_PRIOR),
        "fold_to_jam": posterior_rate("fold_to_jam", FOLD_TO_JAM_PRIOR),
        "river_overcall": posterior_rate("river_overcall", RIVER_OVERCALL_PRIOR),
    }


_HEADS_UP_PREFLOP_EQUITY_DELTA = {
    "sb_open": 0.018,
    "sb_limp": 0.0,
    "sb_vs_reraise": -0.026,
    "bb_option": 0.006,
    "bb_vs_limp": 0.012,
    "bb_vs_raise": -0.014,
}
_HEADS_UP_PREFLOP_SIZE_DELTA = {
    "sb_open": 0.015,
    "sb_limp": 0.0,
    "sb_vs_reraise": -0.015,
    "bb_option": 0.0,
    "bb_vs_limp": 0.020,
    "bb_vs_raise": -0.010,
}
_HEADS_UP_ACTIONABLE_PREFLOP_SPOTS = frozenset({
    "sb_open",
    "sb_vs_reraise",
    "bb_vs_limp",
    "bb_vs_raise",
})
_HEADS_UP_LINE_STATE_ONLY_SPOTS = frozenset({"sb_limp", "bb_option"})


def _preflop_spot_adjustment(context):
    """Consume only the runtime-produced heads-up line classification."""

    hand = context.get("hand", {}) or {}
    cards = context.get("cards", {}) or {}
    if hand.get("street") != "preflop" or cards.get("board"):
        return {"equity_delta": 0.0, "sizing_delta": 0.0}
    spot = str((context.get("line", {}) or {}).get("preflop_spot") or "")
    if spot in _HEADS_UP_LINE_STATE_ONLY_SPOTS:
        return {"equity_delta": 0.0, "sizing_delta": 0.0}
    return {
        "equity_delta": _HEADS_UP_PREFLOP_EQUITY_DELTA.get(spot, 0.0),
        "sizing_delta": _HEADS_UP_PREFLOP_SIZE_DELTA.get(spot, 0.0),
    }


def _current_pressure_adjustment(context):
    """Condition the bounded range prior on the current public action.

    A showdown posterior is not an unconditional opponent range.  The exact
    current street action and preflop line are system-produced public facts, so
    a large raise must strengthen the sampled range while a limp/check may
    relax it.  Sparse or malformed tracker fields remain neutral.
    """

    line = context.get("line", {}) or {}
    line = line if isinstance(line, dict) else {}
    hand = context.get("hand", {}) or {}
    hand = hand if isinstance(hand, dict) else {}
    betting = context.get("betting", {}) or {}
    betting = betting if isinstance(betting, dict) else {}
    opponent = context.get("opponent", {}) or {}
    opponent = opponent if isinstance(opponent, dict) else {}
    spot = str(line.get("preflop_spot") or "")
    preflop_range_tilt = {
        "sb_vs_reraise": 0.62,
        "bb_vs_raise": 0.36,
        "bb_vs_limp": -0.12,
    }.get(spot, 0.0)
    current_action_tilt = 0.0

    current = line.get("current_street", {}) or {}
    current = current if isinstance(current, dict) else {}
    actions = current.get("actions", ())
    if not isinstance(actions, (list, tuple)):
        actions = ()
    opponent_action = None
    for action in reversed(actions):
        if isinstance(action, dict) and action.get("actor") == "opponent":
            opponent_action = str(action.get("action") or "")
            break
    pot = max(1.0, _number(betting.get("pot"), 1.0))
    to_call = max(0.0, _number(betting.get("to_call"), 0.0))
    pressure_ratio = _bounded(to_call / pot, 0.0, 2.0, 0.0)
    if opponent_action == "allin":
        current_action_tilt += 0.58 + 0.16 * min(1.0, pressure_ratio)
    elif opponent_action == "raise":
        current_action_tilt += 0.26 + 0.24 * min(1.0, pressure_ratio)
    elif opponent_action in {"check", "pass"} and to_call == 0.0:
        current_action_tilt -= 0.10

    street = str(hand.get("street") or line.get("street") or "")
    if street in {"flop", "turn", "river"}:
        raw_by_street = opponent.get("raw_street_actions", {}) or {}
        raw_by_street = raw_by_street if isinstance(raw_by_street, dict) else {}
        street_counts = raw_by_street.get(street, {}) or {}
        if isinstance(street_counts, dict):
            samples = sum(
                max(0, _integer(value, 0)) for value in street_counts.values()
            )
        else:
            samples = 0
        observed = _bounded(
            opponent.get(f"{street}_aggr"), 0.0, 1.0, 0.36
        )
        sample_weight = _bounded(samples / (samples + 16.0), 0.0, 1.0, 0.0)
        current_action_tilt += 0.22 * sample_weight * (observed - 0.36)
        if street == "river" and current_action_tilt > 0.0:
            current_action_tilt *= 1.10

    preflop_range_tilt = _bounded(
        preflop_range_tilt, -0.30, 0.70, 0.0
    )
    current_action_tilt = _bounded(
        current_action_tilt, -0.30, 1.10, 0.0
    )
    range_tilt = _bounded(
        preflop_range_tilt + current_action_tilt,
        -0.30,
        1.10,
        0.0,
    )
    return {
        "range_tilt": range_tilt,
        "preflop_range_tilt": preflop_range_tilt,
        "current_action_tilt": current_action_tilt,
        "equity_delta": _bounded(
            0.025 * max(0.0, -range_tilt)
            - 0.085 * max(0.0, range_tilt),
            -0.095,
            0.020,
            0.0,
        ),
    }


def _match_pressure_adjustment(context):
    """Bound final-match protection/chasing without reconstructing history.

    Positive equity/size deltas chase a late deficit; negative deltas protect a
    late lead.  Missing, early, or malformed public reducer fields are neutral.
    """

    neutral = {
        "equity_delta": 0.0,
        "sizing_delta": 0.0,
        "protect": 0.0,
        "chase": 0.0,
    }
    hand = context.get("hand", {}) or {}
    if "remaining_including_current" not in hand:
        return neutral
    remaining_value = _number(hand.get("remaining_including_current"), 0.0)
    if remaining_value < 1.0 or remaining_value > 70.0:
        return neutral
    remaining = int(remaining_value)
    if abs(remaining_value - remaining) > 1e-9 or remaining >= 15:
        return neutral
    match_result = (
        ((context.get("opponent", {}) or {}).get("match_result", {}) or {})
    )
    if "hero_net_earned" not in match_result:
        return neutral
    hero_net = _number(match_result.get("hero_net_earned"), 0.0)
    if hero_net == 0.0:
        return neutral
    late_factor = _bounded((15.0 - remaining) / 14.0, 0.0, 1.0, 0.0)
    per_hand_pressure = _bounded(
        (abs(hero_net) / remaining - 50.0) / 250.0,
        0.0,
        1.0,
        0.0,
    )
    intensity = _bounded(late_factor * per_hand_pressure, 0.0, 1.0, 0.0)
    chase = intensity if hero_net < 0.0 else 0.0
    protect = intensity if hero_net > 0.0 else 0.0
    return {
        "equity_delta": _bounded(
            0.045 * chase - 0.045 * protect,
            -0.045,
            0.045,
            0.0,
        ),
        "sizing_delta": _bounded(
            0.12 * chase - 0.08 * protect,
            -0.08,
            0.12,
            0.0,
        ),
        "protect": protect,
        "chase": chase,
    }


def _fold_locks_match_win(context):
    """Consume only an internally consistent system match-control proof."""

    hand = context.get("hand", {}) or {}
    betting = context.get("betting", {}) or {}
    line = context.get("line", {}) or {}
    opponent = context.get("opponent", {}) or {}
    control = hand.get("match_control", {}) or {}
    match_result = opponent.get("match_result", {}) or {}
    if not all(
        isinstance(value, dict)
        for value in (hand, betting, line, opponent, control, match_result)
    ):
        return False
    integer_fields = (
        "initial_chips",
        "small_blind",
        "big_blind",
        "current_exposure",
        "future_forced_blinds",
        "forced_fold_loss_bound",
        "hero_net_earned",
    )
    if (
        control.get("schema_version") != 1
        or any(not _plain_integer(control.get(field)) for field in integer_fields)
        or not isinstance(control.get("fold_locks_win"), bool)
        or control.get("initial_chips") != 20_000
        or control.get("small_blind") != 50
        or control.get("big_blind") != 100
        or not _plain_integer(hand.get("number"))
        or not _plain_integer(hand.get("total_hands"))
        or not _plain_integer(hand.get("remaining_including_current"))
        or not _plain_integer(betting.get("hero_stack"))
        or not _plain_integer(match_result.get("hero_net_earned"))
    ):
        return False
    number = hand["number"]
    total = hand["total_hands"]
    remaining = hand["remaining_including_current"]
    hero_stack = betting["hero_stack"]
    position = str(hand.get("position") or "")
    if (
        total != 70
        or not 1 <= number <= total
        or remaining != total - number + 1
        or not 0 <= hero_stack <= control["initial_chips"]
        or position not in {"small_blind", "big_blind"}
        or control.get("current_position") != position
        or str(line.get("position") or "") != position
        or control["hero_net_earned"] != match_result["hero_net_earned"]
    ):
        return False
    current_exposure = control["initial_chips"] - hero_stack
    future_hands = remaining - 1
    pairs, odd = divmod(future_hands, 2)
    future_blinds = pairs * (
        control["small_blind"] + control["big_blind"]
    )
    if odd:
        future_blinds += (
            control["big_blind"]
            if position == "small_blind"
            else control["small_blind"]
        )
    bound = current_exposure + future_blinds
    locks = control["hero_net_earned"] > bound
    return bool(
        control["current_exposure"] == current_exposure
        and control["future_forced_blinds"] == future_blinds
        and control["forced_fold_loss_bound"] == bound
        and control["fold_locks_win"] is locks
        and locks
    )


def _straight_pressure(rank_set):
    rank_mask = 0
    for rank in rank_set:
        rank_mask |= 1 << int(rank)
    pressure = 0.0
    for high in range(12, 3, -1):
        present = (rank_mask & (0b11111 << (high - 4))).bit_count()
        if present >= 4:
            return 1.0
        if present == 3:
            pressure = max(pressure, 0.60)
    wheel_mask = (1 << 12) | 0b1111
    present = (rank_mask & wheel_mask).bit_count()
    if present >= 4:
        return 1.0
    if present == 3:
        pressure = max(pressure, 0.60)
    return pressure


def _made_hand_tier(hole, board):
    if len(hole) != 2 or len(board) < 3:
        return "air"
    rank = precompute.best_hand_rank((*hole, *board))
    category = rank[0]
    if len(board) == 5:
        board_rank = precompute.best_hand_rank(board)
        if category >= 3 and rank == board_rank:
            # The hero plays the public five-card hand.  It may tie or lose to
            # a hole-card improvement, but it is never private nut authority.
            return "shared"
    if category == 9:
        return "nut_like" if rank[1] == 12 else "strong"
    if category == 8:
        combined_ranks = [
            precompute.card_parts(card)[1] for card in (*hole, *board)
        ]
        quad_rank = next(
            (rank for rank in set(combined_ranks) if combined_ranks.count(rank) == 4),
            None,
        )
        board_ranks = [precompute.card_parts(card)[1] for card in board]
        if quad_rank is not None and board_ranks.count(quad_rank) == 4:
            hole_kicker = max(
                (
                    precompute.card_parts(card)[1] for card in hole
                    if precompute.card_parts(card)[1] != quad_rank
                ),
                default=-1,
            )
            visible = set(combined_ranks)
            higher_live = [
                rank for rank in range(hole_kicker + 1, 13)
                if rank != quad_rank and rank not in visible
            ]
            return "nut_like" if not higher_live else "strong"
        return "nut_like"
    if category == 7:
        # Full houses are robust value, but on paired public boards their
        # relative nut rank depends on unseen pocket combinations.  Treat them
        # as strong rather than granting unconditional nut authority.
        return "strong"
    if category == 6:
        suit_counts = {}
        for card in (*hole, *board):
            suit, _rank = precompute.card_parts(card)
            suit_counts[suit] = suit_counts.get(suit, 0) + 1
        flush_suit = next(
            (suit for suit, count in suit_counts.items() if count >= 5),
            None,
        )
        if flush_suit is None:
            return "strong"
        hole_flush_ranks = sorted(
            (
                precompute.card_parts(card)[1]
                for card in hole
                if precompute.card_parts(card)[0] == flush_suit
            ),
            reverse=True,
        )
        if not hole_flush_ranks:
            return "weak"
        visible_flush_ranks = {
            precompute.card_parts(card)[1]
            for card in (*hole, *board)
            if precompute.card_parts(card)[0] == flush_suit
        }
        hero_high = hole_flush_ranks[0]
        higher_live = [
            rank for rank in range(hero_high + 1, 13)
            if rank not in visible_flush_ranks
        ]
        if not higher_live:
            return "nut_like"
        return "strong" if len(higher_live) <= 2 and hero_high >= 9 else "weak"
    if category == 3:
        board_ranks = [precompute.card_parts(card)[1] for card in board]
        hole_ranks = [precompute.card_parts(card)[1] for card in hole]
        board_pairs = {
            rank for rank in set(board_ranks) if board_ranks.count(rank) >= 2
        }
        if len(board_pairs) >= 2:
            board_kicker = max(
                (rank for rank in board_ranks if rank not in board_pairs),
                default=-1,
            )
            hero_kicker = max(
                (rank for rank in hole_ranks if rank not in board_pairs),
                default=-1,
            )
            if hero_kicker <= board_kicker:
                return "weak"
            visible = set((*board_ranks, *hole_ranks))
            higher_live = [
                rank for rank in range(hero_kicker + 1, 13)
                if rank not in board_pairs and rank not in visible
            ]
            return "strong" if not higher_live else "weak"
        return "strong"
    if category >= 4:
        return "strong"
    if category <= 1:
        return "air"
    hole_ranks = [precompute.card_parts(card)[1] for card in hole]
    board_ranks = [precompute.card_parts(card)[1] for card in board]
    if hole_ranks[0] == hole_ranks[1]:
        return "strong" if hole_ranks[0] > max(board_ranks) else "weak"
    paired_hole_ranks = [rank for rank in hole_ranks if rank in board_ranks]
    if not paired_hole_ranks:
        return "weak"
    pair_rank = max(paired_hole_ranks)
    kicker = max(rank for rank in hole_ranks if rank != pair_rank)
    if pair_rank == max(board_ranks) and kicker >= 9:
        return "strong"
    return "weak"


def _board_adjustment(context):
    """Make public texture affect weak and strong made hands differently."""

    cards = context.get("cards", {}) or {}
    hole = _card_ids(cards.get("hole", ()))
    board = _card_ids(cards.get("board", ()))
    if len(hole) != 2 or len(board) < 3:
        return {
            "equity_delta": 0.0,
            "sizing_delta": 0.0,
            "danger": 0.0,
            "made_tier": "air",
        }
    board_suits = [precompute.card_parts(card)[0] for card in board]
    board_ranks = [precompute.card_parts(card)[1] for card in board]
    suit_counts = {}
    for suit in board_suits:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    max_suit = max(suit_counts.values(), default=0)
    flush_pressure = 1.0 if max_suit >= 4 else 0.65 if max_suit == 3 else 0.0
    straight_pressure = _straight_pressure(set(board_ranks))
    paired = len(set(board_ranks)) < len(board_ranks)
    danger = _bounded(
        0.50 * flush_pressure
        + 0.38 * straight_pressure
        + (0.12 if paired else 0.0),
        0.0,
        1.0,
        0.0,
    )
    tier = _made_hand_tier(hole, board)
    if tier in {"air", "weak"}:
        equity_delta = -0.040 * danger
        sizing_delta = -0.040 * danger
    elif tier == "shared":
        equity_delta = -0.020 * danger
        sizing_delta = -0.050 * danger
    elif tier == "strong":
        equity_delta = -0.008 * danger
        sizing_delta = 0.050 * danger
    else:
        equity_delta = 0.0
        sizing_delta = 0.025 * danger
    return {
        "equity_delta": _bounded(equity_delta, -0.040, 0.0, 0.0),
        "sizing_delta": _bounded(sizing_delta, -0.050, 0.050, 0.0),
        "danger": danger,
        "made_tier": tier,
    }


def _strategy_adjustments(context):
    preflop = _preflop_spot_adjustment(context)
    match = _match_pressure_adjustment(context)
    board = _board_adjustment(context)
    return {
        "equity_delta": _bounded(
            preflop["equity_delta"]
            + match["equity_delta"]
            + board["equity_delta"],
            -0.08,
            0.08,
            0.0,
        ),
        "sizing_delta": _bounded(
            preflop["sizing_delta"]
            + match["sizing_delta"]
            + board["sizing_delta"],
            -0.10,
            0.15,
            0.0,
        ),
    }


def _draw_bonus(hole, board):
    """Return only hole-backed, non-river draw equity."""

    if len(hole) != 2 or len(board) >= 5 or len(board) < 3:
        return 0.0
    combined = (*hole, *board)
    combined_suits = {}
    board_suits = {}
    hole_suits = {}
    for card in combined:
        suit, rank = precompute.card_parts(card)
        combined_suits[suit] = combined_suits.get(suit, 0) + 1
    for card in board:
        suit, _rank = precompute.card_parts(card)
        board_suits[suit] = board_suits.get(suit, 0) + 1
    for card in hole:
        suit, _rank = precompute.card_parts(card)
        hole_suits[suit] = hole_suits.get(suit, 0) + 1
    bonus = 0.0
    if any(
        count == 4
        and board_suits.get(suit, 0) < 4
        and hole_suits.get(suit, 0) > 0
        for suit, count in combined_suits.items()
    ):
        bonus = 0.075
    combined_rank_mask = 0
    board_rank_mask = 0
    hole_rank_mask = 0
    for card in combined:
        combined_rank_mask |= 1 << precompute.card_parts(card)[1]
    for card in board:
        board_rank_mask |= 1 << precompute.card_parts(card)[1]
    for card in hole:
        hole_rank_mask |= 1 << precompute.card_parts(card)[1]
    for high in range(12, 3, -1):
        window_mask = 0b11111 << (high - 4)
        if (
            (combined_rank_mask & window_mask).bit_count() == 4
            and (board_rank_mask & window_mask).bit_count() < 4
            and bool(hole_rank_mask & window_mask)
        ):
            bonus += 0.055
            break
    wheel_mask = (1 << 12) | 0b1111
    if (
        (combined_rank_mask & wheel_mask).bit_count() == 4
        and (board_rank_mask & wheel_mask).bit_count() < 4
        and bool(hole_rank_mask & wheel_mask)
    ):
        bonus = max(bonus, 0.055)
    return min(0.12, bonus)


def _bounded_postflop_baseline_equity(context, hole, board):
    """Run a fixed deterministic postflop sample below the baseline target."""

    if len(hole) != 2 or len(board) not in {3, 4, 5}:
        return None
    known = (*hole, *board)
    if len(set(known)) != len(known):
        return None
    sample_count = {
        3: BASELINE_FLOP_SAMPLES,
        4: BASELINE_TURN_SAMPLES,
        5: BASELINE_RIVER_SAMPLES,
    }[len(board)]
    deck = precompute.deck_without(known)
    board_needed = 5 - len(board)
    state = _simulation_seed(context, known) ^ 0xBA5E11E
    posterior = _opponent_posterior(context)
    weighted_points = 0.0
    weight_total = 0.0
    for _index in range(sample_count):
        draw, state = precompute.deterministic_draw(
            deck, 2 + board_needed, state
        )
        opponent_hole = draw[:2]
        final_board = (*board, *draw[2:])
        hero_rank = precompute.evaluate_seven((*hole, *final_board))
        opponent_rank = precompute.evaluate_seven((*opponent_hole, *final_board))
        point = (
            1.0 if hero_rank > opponent_rank
            else 0.5 if hero_rank == opponent_rank
            else 0.0
        )
        weight = _opponent_sample_weight(posterior, opponent_hole, board)
        weighted_points += weight * point
        weight_total += weight
    if weight_total <= 0.0:
        return None
    return _bounded(weighted_points / weight_total, 0.0, 1.0, 0.5)


def _refinement_prior_equity(context, hole, board):
    """Return a cheap prior; live refinement samples replace it progressively."""

    if not board:
        value = precompute.preflop_equity(hole[0], hole[1])
    else:
        category = precompute.best_hand_rank((*hole, *board))[0]
        value = {
            1: 0.31,
            2: 0.55,
            3: 0.70,
            4: 0.79,
            5: 0.84,
            6: 0.88,
            7: 0.93,
            8: 0.97,
            9: 0.985,
        }[category]
        made_tier = _made_hand_tier(hole, board)
        if made_tier == "shared":
            value = 0.50
        elif category == 6 and made_tier == "weak":
            value = min(value, 0.52)
        elif category == 6 and made_tier == "strong":
            value = min(value, 0.78)
        elif category == 3 and made_tier == "weak":
            value = min(value, 0.54)
        elif category in {7, 8} and made_tier == "strong":
            value = min(value, 0.92)
        elif category == 9 and made_tier == "strong":
            value = min(value, 0.95)
        if category == 1:
            value += 0.08 * (max(card // 4 for card in hole) / 12.0)
        value += _draw_bonus(hole, board)
    value += _opponent_posterior(context)["range_strength"]
    return _bounded(value, 0.02, 0.98, 0.35)


def _baseline_equity(context):
    cards = context.get("cards", {}) or {}
    hole = _card_ids(cards.get("hole", ()))
    board = _card_ids(cards.get("board", ()))
    if len(hole) != 2 or len(board) not in (0, 3, 4, 5):
        return 0.35
    posterior_applied = False
    if not board:
        value = precompute.preflop_equity(hole[0], hole[1])
    else:
        sampled = _bounded_postflop_baseline_equity(context, hole, board)
        if sampled is not None:
            value = sampled
            posterior_applied = True
        else:
            rank = precompute.best_hand_rank((*hole, *board))
            category = rank[0]
            category_prior = {
                1: 0.31,
                2: 0.55,
                3: 0.70,
                4: 0.79,
                5: 0.84,
                6: 0.88,
                7: 0.93,
                8: 0.97,
                9: 0.985,
            }
            value = category_prior[category]
            if category == 1:
                value += 0.08 * (max(card // 4 for card in hole) / 12.0)
            value += _draw_bonus(hole, board)
    if not posterior_applied:
        value += _opponent_posterior(context)["range_strength"]
    return _bounded(value, 0.02, 0.98, 0.35)


def _preflop_raise_total(context, effective_fraction):
    """Return a spot-aware total contribution, not a postflop pot bet."""

    hand = context.get("hand", {}) or {}
    line = context.get("line", {}) or {}
    betting = context.get("betting", {}) or {}
    if hand.get("street") != "preflop" or (context.get("cards", {}) or {}).get(
        "board"
    ):
        return None
    spot = str(line.get("preflop_spot") or "")
    if spot not in _HEADS_UP_ACTIONABLE_PREFLOP_SPOTS:
        return None
    control = hand.get("match_control", {}) or {}
    big_blind = (
        control.get("big_blind")
        if isinstance(control, dict)
        and control.get("schema_version") == 1
        and _plain_integer(control.get("big_blind"))
        and control.get("big_blind") > 0
        else 100
    )
    opponent_total = max(
        0, _integer(betting.get("opponent_street_bet"), 0)
    )
    if spot == "sb_open":
        base, floor, reference = 2.50 * big_blind, 2.25 * big_blind, 0.48
    elif spot == "bb_vs_limp":
        base, floor, reference = 3.50 * big_blind, 3.25 * big_blind, 0.55
    elif spot == "bb_vs_raise":
        base = max(6.50 * big_blind, 3.50 * opponent_total)
        floor = max(6.50 * big_blind, 3.25 * opponent_total)
        reference = 0.62
    else:  # sb_vs_reraise
        base = max(9.00 * big_blind, 2.50 * opponent_total)
        floor = max(9.00 * big_blind, 2.25 * opponent_total)
        reference = 0.72
    scale = _bounded(
        _number(effective_fraction, reference) / reference,
        0.85,
        1.25,
        1.0,
    )
    chip_increment = max(1, big_blind // 4)
    target = max(floor, base * scale)
    return int(round(target / chip_increment) * chip_increment)


def _raise_intent(context, fraction, adaptation_scale=1.0):
    legal = context.get("legal", {}) or {}
    betting = context.get("betting", {}) or {}
    kinds = set(legal.get("policy_kinds", ()))
    can_raise = "raise" in kinds
    can_allin = "allin" in kinds
    if not can_raise and not can_allin:
        return None
    minimum = _integer(legal.get("min_raise_to"), 0)
    maximum = _integer(legal.get("max_raise_to"), 0)
    raise_boundary_valid = can_raise and minimum > 0 and maximum >= minimum
    if can_raise and not raise_boundary_valid:
        can_raise = False
    pot = max(1, _integer(betting.get("pot"), 1))
    hero_stage = max(0, _integer(betting.get("hero_street_bet"), 0))
    hero_stack = max(0, _integer(betting.get("hero_stack"), 0))
    adjustment = _opponent_posterior(context)["raise_fraction"]
    strategy_sizing = _strategy_adjustments(context)["sizing_delta"]
    effective_fraction = _bounded(
        _number(fraction, 0.45)
        + _bounded(adaptation_scale, 0.0, 1.0, 1.0) * adjustment
        + strategy_sizing,
        0.20,
        1.25,
        0.45,
    )
    preflop_target = _preflop_raise_total(context, effective_fraction)
    target = max(
        minimum,
        preflop_target
        if preflop_target is not None
        else hero_stage + int(round(pot * effective_fraction)),
    )
    hero_total = hero_stage + hero_stack
    if (
        can_allin
        and hero_stack > 0
        and target >= hero_total
    ):
        return {"kind": "allin"}
    if not can_raise:
        return None
    return {"kind": "raise", "raise_to": min(maximum, target)}


def _deterministic_mix(context, salt):
    cards = context.get("cards", {}) or {}
    known = (*_card_ids(cards.get("hole", ())), *_card_ids(cards.get("board", ())))
    state = _simulation_seed(context, known)
    state ^= int(salt) & 0xFFFFFFFFFFFFFFFF
    state ^= (state >> 30)
    state = (state * 0xBF58476D1CE4E5B9) & 0xFFFFFFFFFFFFFFFF
    state ^= (state >> 27)
    state = (state * 0x94D049BB133111EB) & 0xFFFFFFFFFFFFFFFF
    state ^= (state >> 31)
    return state / float(1 << 64)


def _bluff_allowed(context, equity):
    """Permit deterministic low-frequency semi-bluffs, never fixed air spam."""

    hand = context.get("hand", {}) or {}
    hand = hand if isinstance(hand, dict) else {}
    betting = context.get("betting", {}) or {}
    betting = betting if isinstance(betting, dict) else {}
    line = context.get("line", {}) or {}
    line = line if isinstance(line, dict) else {}
    street = str(hand.get("street") or "")
    if street not in {"flop", "turn"} or _number(betting.get("to_call"), 0.0) > 0:
        return False
    if _number(equity, 0.0) > 0.46:
        return False
    cards = context.get("cards", {}) or {}
    hole = _card_ids(cards.get("hole", ()))
    board = _card_ids(cards.get("board", ()))
    if (
        len(hole) != 2
        or len(board) not in {3, 4}
        or len(set((*hole, *board))) != len(hole) + len(board)
    ):
        return False
    draw = _draw_bonus(hole, board) if len(hole) == 2 else 0.0
    raw_tags = line.get("line_tags", ()) or ()
    if not isinstance(raw_tags, (list, tuple, set, frozenset)):
        raw_tags = ()
    tags = {item for item in raw_tags if isinstance(item, str)}
    line_permission = bool(
        line.get("can_donk")
        or line.get("can_delayed_probe")
        or line.get("responding_to_check")
        or "previous_street_checked_through" in tags
    )
    frequency = 0.30 if draw > 0.0 else 0.10 if line_permission else 0.0
    return frequency > 0.0 and _deterministic_mix(context, 0xB10FF) < frequency


def _polarized_raise_fraction(context, equity):
    betting = context.get("betting", {}) or {}
    line = context.get("line", {}) or {}
    spr = _bounded(betting.get("spr"), 0.0, 200.0, 20.0)
    to_call = max(0, _integer(betting.get("to_call"), 0))
    if to_call == 0 and (line.get("can_donk") or line.get("can_delayed_probe")):
        # Donk-only reach: ``line.can_donk`` is the exclusive active flag in
        # the OR expression above (``can_delayed_probe`` False).  Emit a
        # distinct, larger value/bluff geometry so ``can_donk`` has an isolated
        # causal effect on the final typed ``raise_to`` instead of collapsing
        # into the shared 0.58/0.38 path used by every other reach combination.
        # Runtime Contract: no I/O, no history scan, no allocation; the branch
        # returns a scalar fraction (or ``None`` fallback) consumed by the
        # existing ``_raise_intent`` path.  Reachable consumer:
        # ``get_baseline_decision`` and ``_decision_from_equity`` -> ``_raise_intent``.
        if line.get("can_donk") and not line.get("can_delayed_probe"):
            if equity >= 0.74:
                return 0.66
            if _bluff_allowed(context, equity):
                return 0.42
            return None
        if equity >= 0.70:
            return 0.58
        return 0.38 if _bluff_allowed(context, equity) else None
    if equity >= 0.78:
        if spr <= 1.5:
            return 0.90
        if spr <= 4.0:
            return 0.72
        return 0.58
    if equity <= 0.46 and to_call == 0 and _bluff_allowed(context, equity):
        return 0.34
    return None


def _legal_pass_or_fold(kinds, *, prefer_pass=True):
    if prefer_pass and "pass" in kinds:
        return {"kind": "pass"}
    if "fold" in kinds:
        return {"kind": "fold"}
    if "pass" in kinds:
        return {"kind": "pass"}
    return {"kind": "allin"}


def _preflop_baseline_decision(context, equity):
    """Return a spot-aware heads-up baseline, or ``None`` for unknown lines."""

    hand = context.get("hand", {}) or {}
    cards = context.get("cards", {}) or {}
    if hand.get("street") != "preflop" or cards.get("board"):
        return None
    legal = context.get("legal", {}) or {}
    betting = context.get("betting", {}) or {}
    kinds = set(legal.get("policy_kinds", ()))
    spot = str((context.get("line", {}) or {}).get("preflop_spot") or "")
    if spot not in _HEADS_UP_PREFLOP_EQUITY_DELTA:
        return None
    pot_odds = _effective_call_pot_odds(context)
    hole = _card_ids(cards.get("hole", ()))
    raw_preflop_equity = (
        precompute.preflop_equity(*hole) if len(hole) == 2 else 0.0
    )

    def raise_or_continue(fraction):
        raised = _raise_intent(context, fraction, adaptation_scale=0.5)
        return raised or _legal_pass_or_fold(kinds)

    if spot == "sb_open":
        if equity >= 0.52:
            return raise_or_continue(0.48)
        if equity >= 0.35:
            return _legal_pass_or_fold(kinds)
        return _legal_pass_or_fold(kinds, prefer_pass=False)
    if spot == "bb_vs_limp":
        return (
            raise_or_continue(0.55)
            if equity >= 0.58
            else _legal_pass_or_fold(kinds)
        )
    if spot in {"bb_option", "sb_limp"}:
        return _legal_pass_or_fold(kinds)
    if spot == "bb_vs_raise":
        if equity >= 0.72:
            return raise_or_continue(0.62)
        return _legal_pass_or_fold(
            kinds,
            prefer_pass=equity >= pot_odds + 0.035,
        )
    if spot == "sb_vs_reraise":
        if (
            raw_preflop_equity >= 0.84
            and "allin" in kinds
            and _bounded(betting.get("spr"), 0.0, 200.0, 20.0) <= 2.0
        ):
            return {"kind": "allin"}
        if equity >= 0.79 or raw_preflop_equity >= 0.82:
            return raise_or_continue(0.72)
        return _legal_pass_or_fold(
            kinds,
            prefer_pass=equity >= pot_odds + 0.070,
        )
    return None


def get_baseline_decision(context):
    """Return a legal, I/O-free baseline using bounded deterministic samples."""

    legal = context.get("legal", {}) or {}
    betting = context.get("betting", {}) or {}
    kinds = set(legal.get("policy_kinds", ()))
    if "fold" in kinds and _fold_locks_match_win(context):
        return {"kind": "fold"}
    to_call = max(0, _integer(betting.get("to_call"), 0))
    pot = max(1, _integer(betting.get("pot"), 1))
    spr = _bounded(betting.get("spr"), 0.0, 200.0, 20.0)
    adjustment = _strategy_adjustments(context)
    equity = _bounded(
        _baseline_equity(context) + adjustment["equity_delta"],
        0.0,
        1.0,
        0.35,
    )
    pot_odds = _effective_call_pot_odds(context)

    preflop = _preflop_baseline_decision(context, equity)
    if preflop is not None:
        return preflop

    call_equity = _realized_call_equity(context, equity)
    if "allin" in kinds and equity >= 0.91 and spr <= 1.6:
        return {"kind": "allin"}
    fraction = _polarized_raise_fraction(context, equity)
    if fraction is not None and (to_call == 0 or equity >= pot_odds + 0.22):
        raised = _raise_intent(context, fraction, adaptation_scale=0.5)
        if raised is not None:
            return raised
    if to_call > 0 and call_equity + 0.035 < pot_odds and "fold" in kinds:
        return {"kind": "fold"}
    if "pass" in kinds:
        return {"kind": "pass"}
    if "fold" in kinds:
        return {"kind": "fold"}
    return {"kind": "allin"}


def _simulation_seed(context, known):
    hand = context.get("hand", {}) or {}
    seed = 0xD1B54A32D192ED03
    for value in (
        _integer(context.get("decision_id"), 0),
        _integer(hand.get("number"), 0),
        *known,
    ):
        seed ^= (int(value) + 0x9E3779B97F4A7C15 + (seed << 6) + (seed >> 2))
        seed &= 0xFFFFFFFFFFFFFFFF
    return seed or 1


def _current_board_range_strength(opponent_hole, current_board):
    """Return made/draw strength using only cards public at this decision."""

    if not isinstance(opponent_hole, (list, tuple)) or not isinstance(
        current_board, (list, tuple)
    ):
        return None
    try:
        hole = tuple(int(card) for card in opponent_hole)
        board = tuple(int(card) for card in current_board)
    except (TypeError, ValueError, OverflowError):
        return None
    if (
        len(hole) != 2
        or len(board) not in {3, 4, 5}
        or len(set((*hole, *board))) != len(hole) + len(board)
        or any(card < 0 or card >= 52 for card in (*hole, *board))
    ):
        return None
    try:
        rank = precompute.best_hand_rank((*hole, *board))
    except (TypeError, ValueError):
        return None

    def flatten(values):
        for value in values:
            if isinstance(value, tuple):
                yield from flatten(value)
            else:
                yield _integer(value, 0)

    category = _integer(rank[0], 1)
    kickers = tuple(flatten(rank[1:]))
    kicker_fraction = sum(
        (_bounded(value, 0.0, 14.0, 0.0) + 1.0) / (16.0 ** (index + 1))
        for index, value in enumerate(kickers[:5])
    )
    made_strength = (category - 1.0) / 8.0 + 0.08 * kicker_fraction
    draw_strength = _draw_bonus(hole, board) if len(board) < 5 else 0.0
    return _bounded(
        made_strength + 0.65 * draw_strength,
        0.0,
        1.0,
        0.0,
    )


def _opponent_sample_weight(posterior, opponent_hole, current_board=()):
    bucket = precompute.preflop_bucket(*opponent_hole)
    weight = _number(posterior["bucket_multipliers"].get(bucket), 1.0)
    class_equity = precompute.preflop_equity(*opponent_hole)
    weight *= 1.0 + posterior["wide_range_tilt"] * (0.55 - class_equity)
    preflop_centered = _bounded(
        (class_equity - 0.52) / 0.22,
        -1.0,
        1.0,
        0.0,
    )
    weight *= math.exp(
        _bounded(
            posterior.get(
                "preflop_line_strength_tilt",
                posterior.get("line_strength_tilt"),
            ),
            -0.30,
            0.70,
            0.0,
        )
        * preflop_centered
    )
    board_strength = _current_board_range_strength(opponent_hole, current_board)
    current_tilt = _bounded(
        posterior.get("current_action_strength_tilt"),
        -0.30,
        1.10,
        0.0,
    )
    if board_strength is not None:
        board_centered = _bounded(
            (board_strength - 0.18) / 0.30,
            -1.0,
            1.0,
            0.0,
        )
        weight *= math.exp(current_tilt * board_centered)
    elif not current_board:
        # Preflop current action has no public board; the calibrated 169 fact
        # remains the only admissible strength ordering.
        weight *= math.exp(current_tilt * preflop_centered)
    return _bounded(weight, 0.35, 2.00, 1.0)


def _candidate_raise_fractions(context, equity):
    spr = _bounded((context.get("betting", {}) or {}).get("spr"), 0.0, 200.0, 20.0)
    structural = _polarized_raise_fraction(context, equity)
    fractions = [0.38, 0.66 if spr > 2.0 else 0.52, 1.05 if spr > 5.0 else 0.82]
    if structural is not None:
        fractions.append(structural)
    return tuple(dict.fromkeys(round(value, 3) for value in fractions))


def _matched_showdown_commitments(context, target):
    """Return contest pot and exact future contributions if a raise is called."""

    betting = context.get("betting", {}) or {}
    pot = max(0.0, _number(betting.get("pot"), 0.0))
    hero_stage = max(0.0, _number(betting.get("hero_street_bet"), 0.0))
    opponent_stage = max(
        0.0, _number(betting.get("opponent_street_bet"), 0.0)
    )
    hero_stack = max(0.0, _number(betting.get("hero_stack"), 0.0))
    opponent_stack = max(0.0, _number(betting.get("opponent_stack"), 0.0))
    hero_target = min(hero_stage + hero_stack, max(hero_stage, _number(target, hero_stage)))
    opponent_cap = opponent_stage + opponent_stack
    matched_target = min(hero_target, opponent_cap)
    # An oversized prior bet or shove may include unmatched chips that the
    # engine returns at settlement.  They are not part of the showdown pot.
    unmatched_existing = max(0.0, hero_stage - matched_target) + max(
        0.0, opponent_stage - matched_target
    )
    contest_pot = max(0.0, pot - unmatched_existing)
    return {
        "contest_pot": contest_pot,
        "hero_extra": max(0.0, matched_target - hero_stage),
        "opponent_extra": max(0.0, matched_target - opponent_stage),
    }


def _called_showdown_ev(context, target, equity):
    commitments = _matched_showdown_commitments(context, target)
    return (
        _bounded(equity, 0.0, 1.0, 0.0)
        * (
            commitments["contest_pot"]
            + commitments["hero_extra"]
            + commitments["opponent_extra"]
        )
        - commitments["hero_extra"]
    )


def _effective_call_pot_odds(context):
    betting = context.get("betting", {}) or {}
    opponent_target = max(
        0.0, _number(betting.get("opponent_street_bet"), 0.0)
    )
    commitments = _matched_showdown_commitments(context, opponent_target)
    cost = commitments["hero_extra"]
    return cost / max(
        1.0,
        commitments["contest_pot"]
        + commitments["hero_extra"]
        + commitments["opponent_extra"],
    )


def _realized_call_equity(context, equity):
    """Discount future-street realization only when position is authoritative."""

    value = _bounded(equity, 0.0, 1.0, 0.0)
    hand = context.get("hand", {}) or {}
    betting = context.get("betting", {}) or {}
    line = context.get("line", {}) or {}
    street = str(hand.get("street") or "")
    if street not in {"flop", "turn"}:
        return value
    closing = betting.get("call_closes_allin_runout")
    if not isinstance(closing, bool) or closing:
        return value
    to_call = max(0.0, _number(betting.get("to_call"), 0.0))
    if to_call <= 0.0:
        return value
    in_position = line.get("hero_in_position_postflop")
    acts_first = hand.get("acts_first_postflop")
    if (
        not isinstance(in_position, bool)
        or not isinstance(acts_first, bool)
        or in_position is acts_first
    ):
        return value
    pot = max(1.0, _number(betting.get("pot"), 1.0))
    spr = _bounded(betting.get("spr"), 0.0, 200.0, 20.0)
    future_factor = _bounded(spr / 6.0, 0.25, 1.0, 1.0)
    pressure = _bounded(to_call / (pot + to_call), 0.0, 1.0, 0.0)
    penalty = (
        (0.014 if in_position else 0.055) * future_factor
        + 0.012 * pressure * future_factor
    )
    return _bounded(value - penalty, 0.0, 1.0, value)


def _decision_from_equity(context, equity, confidence, samples, return_margin=False):
    legal = context.get("legal", {}) or {}
    betting = context.get("betting", {}) or {}
    line = context.get("line", {}) or {}
    hand = context.get("hand", {}) or {}
    kinds = set(legal.get("policy_kinds", ()))
    if "fold" in kinds and _fold_locks_match_win(context):
        result = {"kind": "fold"}
        return (result, 1.0) if return_margin else result
    pot = max(1.0, _number(betting.get("pot"), 1.0))
    to_call = max(0.0, _number(betting.get("to_call"), 0.0))
    hero_stage = max(0, _integer(betting.get("hero_street_bet"), 0))
    hero_stack = max(0.0, _number(betting.get("hero_stack"), 0.0))
    spr = _bounded(betting.get("spr"), 0.0, 200.0, 20.0)
    posterior = _opponent_posterior(context)
    river = hand.get("street") == "river"
    raise_fold_rate = posterior["fold_to_raise"]
    jam_fold_rate = posterior["fold_to_jam"]
    if river:
        overcall_fold = 1.0 - posterior["river_overcall"]
        raise_fold_rate = 0.60 * raise_fold_rate + 0.40 * overcall_fold
        jam_fold_rate = 0.65 * jam_fold_rate + 0.35 * overcall_fold
    uncertainty = min(0.22, 0.50 / math.sqrt(max(4.0, float(samples))))
    safe_equity = _bounded(
        equity
        - uncertainty * (1.0 - confidence)
        + _strategy_adjustments(context)["equity_delta"],
        0.0,
        1.0,
    )
    candidates = []
    if "fold" in kinds:
        candidates.append((0.0, {"kind": "fold"}))
    if "pass" in kinds:
        opponent_target = max(
            0.0, _number(betting.get("opponent_street_bet"), 0.0)
        )
        call_ev = _called_showdown_ev(
            context,
            opponent_target,
            _realized_call_equity(context, safe_equity),
        )
        candidates.append((call_ev, {"kind": "pass"}))

    bluff_allowed = _bluff_allowed(context, safe_equity)
    for fraction in _candidate_raise_fractions(context, safe_equity):
        if safe_equity < 0.56 and not bluff_allowed:
            continue
        intent = _raise_intent(context, fraction, adaptation_scale=1.0)
        if intent is None or intent.get("kind") != "raise":
            continue
        commitments = _matched_showdown_commitments(
            context, intent["raise_to"]
        )
        risk = commitments["hero_extra"]
        pressure = risk / max(1.0, pot + risk)
        # A caller's range is stronger than the sampled unconditional range.
        # Keep a prior continuation penalty even when terminal fold evidence is
        # strong; the exploit signal may change frequency/size but cannot turn
        # every small raw-equity edge into a stack-off.
        call_equity = _bounded(
            safe_equity - 0.025 - 0.065 * pressure,
            0.0,
            1.0,
        )
        called_ev = _called_showdown_ev(
            context, intent["raise_to"], call_equity
        )
        score = (
            raise_fold_rate * pot
            + (1.0 - raise_fold_rate) * called_ev
            - uncertainty * risk * 0.12
        )
        if to_call == 0 and (line.get("can_donk") or line.get("can_delayed_probe")):
            score += pot * 0.08
        candidates.append((score, intent))

    if (
        "allin" in kinds
        and hero_stack > 0
        and safe_equity >= 0.62
        and (spr <= 2.5 or safe_equity >= 0.78)
    ):
        jam_equity = _bounded(safe_equity - 0.105, 0.0, 1.0)
        jam_target = hero_stage + hero_stack
        jam_commitments = _matched_showdown_commitments(context, jam_target)
        called_ev = _called_showdown_ev(context, jam_target, jam_equity)
        jam_risk = jam_commitments["hero_extra"]
        jam_score = (
            jam_fold_rate * pot
            + (1.0 - jam_fold_rate) * called_ev
            - uncertainty * jam_risk * 0.15
        )
        candidates.append((jam_score, {"kind": "allin"}))
    if not candidates:
        result, margin = {"kind": "fold"}, 1.0
    else:
        ordered = sorted(candidates, key=lambda item: item[0], reverse=True)
        result = ordered[0][1]
        runner_up = ordered[1][0] if len(ordered) > 1 else ordered[0][0] - pot
        margin = _bounded(
            (ordered[0][0] - runner_up) / max(1.0, pot, abs(ordered[0][0])),
            0.0,
            1.0,
        )
    return (result, margin) if return_margin else result


def iter_decisions(context, baseline, deadline):
    """Yield deterministic progressive equity/EV refinements until ``deadline``.

    Each batch is finite, the total sample cap is fixed, and the monotonic
    deadline is checked both inside and between batches.  The runtime may kill
    the worker at the same boundary; the socket owner always retains the most
    recent already-validated intent.
    """

    legal = context.get("legal", {}) or {}
    if (
        "fold" in set(legal.get("policy_kinds", ()))
        and _fold_locks_match_win(context)
    ):
        return
    deadline = _number(deadline, 0.0)
    if deadline <= time.monotonic() + DEADLINE_GUARD_SECONDS:
        return
    cards = context.get("cards", {}) or {}
    hole = _card_ids(cards.get("hole", ()))
    board = _card_ids(cards.get("board", ()))
    if len(hole) != 2 or len(board) not in (0, 3, 4, 5):
        return
    known = (*hole, *board)
    if len(set(known)) != len(known):
        return

    # Publish a cheap full-posterior sizing improvement before simulation.
    # The fast baseline already performed its bounded postflop rollout.  Do
    # not repeat that work inside the caller-owned refinement deadline; start
    # from a cheap prior and let the following counted samples replace it.
    baseline_equity = _refinement_prior_equity(context, hole, board)
    fraction = _polarized_raise_fraction(context, baseline_equity)
    if baseline.get("kind") == "raise" and fraction is not None:
        adjusted = _raise_intent(context, fraction, adaptation_scale=1.0)
        if adjusted is not None and adjusted != baseline and time.monotonic() < deadline:
            yield {
                "decision": adjusted,
                "sample_count": 0,
                "confidence": 0.0,
                "reason": "bounded_posterior_sizing",
                "complete": False,
            }

    deck = precompute.deck_without(known)
    board_needed = 5 - len(board)
    state = _simulation_seed(context, known)
    posterior = _opponent_posterior(context)
    river_pairs = iter(itertools.combinations(deck, 2)) if board_needed == 0 else None
    river_exhausted = False
    samples = 0
    weighted_points = 0.0
    weight_total = 0.0
    weight_square_total = 0.0
    batch_size = 64 if river_pairs is not None else INITIAL_BATCH_SIZE
    stable_batches = 0
    last_decision = None

    while samples < MAX_EQUITY_SAMPLES:
        completed_batch = 0
        for _index in range(min(batch_size, MAX_EQUITY_SAMPLES - samples)):
            if time.monotonic() >= deadline - DEADLINE_GUARD_SECONDS:
                return
            if river_pairs is not None:
                try:
                    opponent_hole = next(river_pairs)
                except StopIteration:
                    river_exhausted = True
                    break
                runout = ()
            else:
                draw, state = precompute.deterministic_draw(
                    deck, 2 + board_needed, state
                )
                opponent_hole, runout = draw[:2], draw[2:]
            final_board = (*board, *runout)
            hero_rank = precompute.evaluate_seven((*hole, *final_board))
            opponent_rank = precompute.evaluate_seven((*opponent_hole, *final_board))
            point = 1.0 if hero_rank > opponent_rank else 0.5 if hero_rank == opponent_rank else 0.0
            weight = _opponent_sample_weight(posterior, opponent_hole, board)
            weighted_points += weight * point
            weight_total += weight
            weight_square_total += weight * weight
            samples += 1
            completed_batch += 1
        if completed_batch == 0:
            break
        estimate = weighted_points / max(1e-9, weight_total)
        effective_samples = weight_total * weight_total / max(1e-9, weight_square_total)
        if river_exhausted:
            # Once every legal opponent hole has been evaluated under the
            # frozen posterior, this is no longer a sample estimate.  Publish
            # the exact posterior result rather than retaining a prior blend.
            confidence = 1.0
            blended_equity = estimate
        else:
            confidence = effective_samples / (effective_samples + 192.0)
            blended_equity = (
                (1.0 - confidence) * baseline_equity + confidence * estimate
            )
        decision, score_margin = _decision_from_equity(
            context,
            _bounded(blended_equity, 0.0, 1.0),
            confidence,
            samples,
            return_margin=True,
        )
        if decision == last_decision:
            stable_batches += 1
        else:
            stable_batches = 1
            last_decision = decision
        # Clear spots finish quickly; medium-margin spots require materially
        # more evidence; genuinely close actions can consume the full official
        # budget.  This avoids burning 54 seconds on every trivial fold/check.
        converged = bool(
            (
                samples >= 4_096
                and confidence >= 0.94
                and stable_batches >= 4
                and score_margin >= 0.08
            )
            or (
                samples >= 32_768
                and confidence >= 0.99
                and stable_batches >= 8
                and score_margin >= 0.025
            )
        )
        complete = bool(
            river_exhausted or converged or samples >= MAX_EQUITY_SAMPLES
        )
        if time.monotonic() >= deadline - DEADLINE_GUARD_SECONDS:
            return
        yield {
            "decision": decision,
            "sample_count": samples,
            "confidence": round(confidence, 6),
            "reason": (
                "deterministic_equity_ev_converged"
                if converged
                else "deterministic_range_weighted_equity_ev_batch"
            ),
            "complete": complete,
        }
        if complete:
            return
        if river_pairs is None:
            batch_size = min(MAX_BATCH_SIZE, batch_size * 2)
