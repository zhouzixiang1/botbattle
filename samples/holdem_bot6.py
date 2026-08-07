"""
Bot 6
"""
import bisect
import itertools
import json
import random

N_PLAYERS = 2
INITIAL_CHIPS = 20000
SMALL_BLIND = 50
BIG_BLIND = 100
TOTAL_HANDS = 50
LOCK_WIN_MARGIN = 1500

HAND_CLASS_SCORE = [0.08, 0.22, 0.40, 0.58, 0.69, 0.76, 0.84, 0.93, 0.98]
SIMULATIONS_BY_PUBLIC_COUNT = {
    0: 500,
    3: 700,
    4: 900,
    5: 0,
}
EXTRA_SIMULATIONS_BY_PUBLIC_COUNT = {
    0: 200,
    3: 220,
    4: 180,
}


def clamp(value, low, high):
    return max(low, min(high, value))


def card_suit(card):
    return card % 4


def card_number(card):
    return card // 4 + 2


def next_player(player, offset):
    return (player + offset) % N_PLAYERS


def evaluate_5(cards):
    ranks = sorted((card_number(c) for c in cards), reverse=True)
    suits = [card_suit(c) for c in cards]
    rank_counts = {}
    for rank in ranks:
        rank_counts[rank] = rank_counts.get(rank, 0) + 1
    groups = sorted(((count, rank) for rank, count in rank_counts.items()), reverse=True)

    is_flush = len(set(suits)) == 1
    unique_ranks = sorted(set(ranks), reverse=True)

    is_straight = False
    straight_high = 0
    if len(unique_ranks) == 5:
        if unique_ranks[0] - unique_ranks[4] == 4:
            is_straight = True
            straight_high = unique_ranks[0]

    if is_flush and is_straight:
        return (8, straight_high)
    if groups[0][0] == 4:
        quad = groups[0][1]
        kicker = max(rank for rank in ranks if rank != quad)
        return (7, quad, kicker)
    if groups[0][0] == 3 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if is_flush:
        return (5, *ranks)
    if is_straight:
        return (4, straight_high)
    if groups[0][0] == 3:
        trips = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != trips), reverse=True)
        return (3, trips, *kickers)
    if groups[0][0] == 2 and groups[1][0] == 2:
        high_pair = max(groups[0][1], groups[1][1])
        low_pair = min(groups[0][1], groups[1][1])
        kicker = max(rank for rank in ranks if rank not in (high_pair, low_pair))
        return (2, high_pair, low_pair, kicker)
    if groups[0][0] == 2:
        pair = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != pair), reverse=True)
        return (1, pair, *kickers)
    return (0, *ranks)


def evaluate_best(cards):
    if len(cards) == 5:
        return evaluate_5(cards)
    best = None
    for combo in itertools.combinations(cards, 5):
        score = evaluate_5(combo)
        if best is None or score > best:
            best = score
    return best


def evaluate_7(cards):
    return evaluate_best(cards)


def estimate_preflop_strength(my_cards):
    r1 = card_number(my_cards[0])
    r2 = card_number(my_cards[1])
    s1 = card_suit(my_cards[0])
    s2 = card_suit(my_cards[1])
    high = max(r1, r2)
    low = min(r1, r2)
    gap = high - low
    suited = s1 == s2
    pair = r1 == r2

    score = 0.0
    score += (high - 2) / 16.0
    score += (low - 2) / 28.0

    if pair:
        score += 0.25 + (high - 2) / 30.0
    else:
        if suited:
            score += 0.06
        if gap == 1:
            score += 0.06
        elif gap == 2:
            score += 0.03
        elif gap >= 4:
            score -= 0.04

    if high == 14:
        score += 0.04
        if low >= 10:
            score += 0.04

    return clamp(score, 0.0, 1.0)


def preflop_hand_profile(my_cards):
    ranks = sorted((card_number(card) for card in my_cards), reverse=True)
    suited = card_suit(my_cards[0]) == card_suit(my_cards[1])
    pair = ranks[0] == ranks[1]
    return {
        "high": ranks[0],
        "low": ranks[1],
        "suited": suited,
        "pair": pair,
    }


def is_preflop_3bet_candidate(my_cards):
    profile = preflop_hand_profile(my_cards)
    if profile["pair"]:
        return True
    return profile["high"] == 14 and profile["low"] >= 12


def is_preflop_trash_hand(my_cards, preflop_strength=None):
    profile = preflop_hand_profile(my_cards)
    if profile["pair"]:
        return False

    high = profile["high"]
    low = profile["low"]
    gap = high - low
    suited = profile["suited"]
    strength = estimate_preflop_strength(my_cards) if preflop_strength is None else preflop_strength

    if high == 14:
        return False
    if suited and gap <= 2 and high >= 6:
        return False
    if high >= 11 and low >= 8 and gap <= 4:
        return False

    if strength <= 0.30:
        return True
    if not suited and high <= 10 and low <= 5 and gap >= 3:
        return True
    if not suited and high <= 12 and low <= 4 and gap >= 5:
        return True
    if suited and high <= 9 and low <= 4 and gap >= 4:
        return True
    return False


def get_hand_index(req):
    for key in ("hand", "hand_id", "hand_index", "round_id", "round_index", "game_id", "game_index"):
        if key in req:
            try:
                return int(req[key])
            except (TypeError, ValueError):
                pass
    return None


def get_remaining_hands(req):
    if "hand" in req and "max_hand" in req:
        try:
            return max(0, int(req["max_hand"]) - int(req["hand"]))
        except (TypeError, ValueError):
            pass

    direct_keys = (
        "remaining_hands",
        "remain_hands",
        "hands_left",
        "left_hands",
        "remaining_rounds",
        "remain_rounds",
        "rounds_left",
        "left_rounds",
    )
    for key in direct_keys:
        if key in req:
            try:
                value = int(req[key])
                if value >= 0:
                    return value
            except (TypeError, ValueError):
                pass

    hand_idx = get_hand_index(req)
    if hand_idx is not None:
        candidates = [TOTAL_HANDS - hand_idx, TOTAL_HANDS - hand_idx + 1]
        candidates = [value for value in candidates if value >= 0]
        if candidates:
            return max(candidates)
    return None


def infer_remaining_hands_from_requests(requests):
    if not requests:
        return TOTAL_HANDS

    direct = get_remaining_hands(requests[-1])
    if direct is not None:
        return direct

    hand_indices = [get_hand_index(req) for req in requests]
    hand_indices = [value for value in hand_indices if value is not None]
    if hand_indices:
        return max(0, TOTAL_HANDS - max(hand_indices))

    started_hands = 0
    for req in requests:
        if len(req.get("public_cards", [])) == 0 and len(req.get("history", [])) == 0:
            started_hands += 1
    if started_hands <= 0:
        return TOTAL_HANDS
    return max(0, TOTAL_HANDS - started_hands + 1)


def reconstruct_state(req):
    my_id = req["my_id"]
    dealer_id = req["dealer_id"]

    stacks = [INITIAL_CHIPS] * N_PLAYERS
    committed = [0] * N_PLAYERS
    sb = next_player(dealer_id, 1)
    bb = next_player(dealer_id, 2)

    stacks[sb] -= SMALL_BLIND
    stacks[bb] -= BIG_BLIND
    committed[sb] += SMALL_BLIND
    committed[bb] += BIG_BLIND

    current_round = 0
    round_bet = BIG_BLIND
    judge_round_raise = BIG_BLIND
    round_contrib = [0] * N_PLAYERS
    round_contrib[sb] = SMALL_BLIND
    round_contrib[bb] = BIG_BLIND
    alive = [True] * N_PLAYERS
    allin = [False] * N_PLAYERS

    for record in req["history"]:
        record_round = record["round"]
        pid = record["player_id"]
        action = record["action"]
        action_type = record["action_type"]

        if record_round != current_round:
            current_round = record_round
            round_bet = 0
            judge_round_raise = SMALL_BLIND
            round_contrib = [0] * N_PLAYERS

        if action_type == "fold":
            alive[pid] = False
            continue

        if not alive[pid] or allin[pid]:
            continue

        if action_type == "allin":
            add = stacks[pid]
            stacks[pid] = 0
            committed[pid] += add
            round_contrib[pid] += add
            allin[pid] = True
            round_bet = max(round_bet, round_contrib[pid])
            continue

        if action_type in ("call", "check"):
            need = max(0, round_bet - round_contrib[pid])
            need = min(need, stacks[pid])
            stacks[pid] -= need
            committed[pid] += need
            round_contrib[pid] += need
        elif action_type == "raise":
            add = max(0, min(action, stacks[pid]))
            stacks[pid] -= add
            committed[pid] += add
            round_contrib[pid] += add
            round_bet = max(round_bet, round_contrib[pid])
            judge_round_raise = max(judge_round_raise, add)

    public_cards = len(req["public_cards"])
    round_idx = 0 if public_cards == 0 else 1 if public_cards == 3 else 2 if public_cards == 4 else 3

    if current_round != round_idx:
        round_bet = 0
        judge_round_raise = SMALL_BLIND
        round_contrib = [0] * N_PLAYERS

    player_bets = [0] * N_PLAYERS
    for pid in range(N_PLAYERS):
        if not alive[pid]:
            player_bets[pid] = -1
        elif allin[pid]:
            player_bets[pid] = -2
        else:
            player_bets[pid] = round_contrib[pid]

    opponent_id = next_player(my_id, 1)
    opponent_allin = allin[opponent_id] and alive[opponent_id]
    my_round_bet = 0 if player_bets[my_id] < 0 else player_bets[my_id]
    to_call = max(0, round_bet - my_round_bet)
    min_raise_action = max(0, 2 * judge_round_raise - my_round_bet)
    allin_call_amount = max(
        0,
        min(committed[opponent_id], committed[my_id] + stacks[my_id]) - committed[my_id],
    )

    return {
        "round": round_idx,
        "round_bet": round_bet,
        "round_raise": judge_round_raise,
        "judge_round_raise": judge_round_raise,
        "min_raise_action": min_raise_action,
        "round_contrib": round_contrib,
        "player_bets": player_bets,
        "stacks": stacks,
        "committed": committed,
        "pot": committed[0] + committed[1],
        "to_call": to_call,
        "opponent_allin": opponent_allin,
        "allin_call_amount": allin_call_amount,
        "my_round_bet": my_round_bet,
    }


def forced_fold_loss_bound(req, state, my_id, remaining_hands):
    if remaining_hands is None or remaining_hands <= 0:
        return None

    loss = state["committed"][my_id]
    current_dealer = req["dealer_id"]
    for offset in range(1, remaining_hands):
        future_dealer = next_player(current_dealer, offset)
        future_sb = next_player(future_dealer, 1)
        future_bb = next_player(future_dealer, 2)
        if my_id == future_sb:
            loss += SMALL_BLIND
        elif my_id == future_bb:
            loss += BIG_BLIND
    return loss


def should_lock_win(req, state, my_id):
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= my_id:
        return False

    try:
        lead = int(total_win_chips[my_id])
    except (TypeError, ValueError):
        return False

    remaining_hands = get_remaining_hands(req)
    if remaining_hands is None:
        return lead >= LOCK_WIN_MARGIN

    max_forced_loss = forced_fold_loss_bound(req, state, my_id, remaining_hands)
    if max_forced_loss is None:
        return lead >= LOCK_WIN_MARGIN
    return lead > max_forced_loss


def opponent_can_lock_win(req, my_id):
    opponent_id = next_player(my_id, 1)
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= opponent_id:
        return False

    try:
        lead = int(total_win_chips[opponent_id])
    except (TypeError, ValueError):
        return False

    remaining_hands = get_remaining_hands(req)
    if remaining_hands is None:
        return lead >= LOCK_WIN_MARGIN

    state = reconstruct_state(req)
    max_forced_loss = forced_fold_loss_bound(req, state, opponent_id, remaining_hands)
    if max_forced_loss is None:
        return lead >= LOCK_WIN_MARGIN
    return lead > max_forced_loss + BIG_BLIND


def fold_gives_opponent_lock(req, state, my_id):
    opponent_id = next_player(my_id, 1)
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= opponent_id:
        return False

    remaining_hands = get_remaining_hands(req)
    if remaining_hands is None or remaining_hands <= 1:
        return False

    try:
        opponent_lead = int(total_win_chips[opponent_id])
    except (TypeError, ValueError):
        return False

    opponent_lead_after_fold = opponent_lead + state["committed"][my_id]
    max_forced_loss = forced_fold_loss_bound(req, state, opponent_id, remaining_hands)
    if max_forced_loss is None:
        return opponent_lead_after_fold >= LOCK_WIN_MARGIN

    future_forced_loss = max(0, max_forced_loss - state["committed"][opponent_id])
    return opponent_lead_after_fold > future_forced_loss


def collect_latest_requests_by_hand(requests):
    latest = {}
    fallback_hand = TOTAL_HANDS
    for req in requests:
        hand = get_hand_index(req)
        if hand is None:
            hand = fallback_hand
            fallback_hand += 1
        prev = latest.get(hand)
        if prev is None or len(req.get("history", [])) >= len(prev.get("history", [])):
            latest[hand] = req
    return [latest[hand] for hand in sorted(latest)]


def smooth_rate(successes, total, prior_mean, prior_weight):
    return (successes + prior_mean * prior_weight) / (total + prior_weight)


def build_opponent_model(requests, my_id):
    opponent_id = next_player(my_id, 1)
    hand_requests = collect_latest_requests_by_hand(requests)

    preflop_opportunities = 0
    voluntary_preflop = 0
    preflop_raise = 0
    total_actions = 0
    aggressive_actions = 0
    allin_actions = 0
    postflop_actions = 0
    postflop_aggressive = 0
    postflop_checks = 0
    fold_to_raise_opportunities = 0
    fold_to_raise = 0
    raise_sizes = []

    for req in hand_requests:
        if opponent_can_lock_win(req, my_id):
            continue

        history = req.get("history", [])
        if not history:
            continue

        saw_opponent_preflop_action = False
        pending_my_pressure = False

        for record in history:
            pid = record["player_id"]
            action_type = record["action_type"]
            action = record["action"]
            round_idx = record["round"]

            if pid == my_id and action_type in ("raise", "allin"):
                pending_my_pressure = True
                continue

            if pid != opponent_id:
                continue

            total_actions += 1
            if action_type in ("raise", "allin"):
                aggressive_actions += 1
            if action_type == "allin":
                allin_actions += 1

            if round_idx == 0 and not saw_opponent_preflop_action:
                saw_opponent_preflop_action = True
                preflop_opportunities += 1
                if action_type in ("call", "raise", "allin"):
                    voluntary_preflop += 1
                if action_type in ("raise", "allin"):
                    preflop_raise += 1

            if round_idx > 0:
                postflop_actions += 1
                if action_type in ("raise", "allin"):
                    postflop_aggressive += 1
                if action_type == "check":
                    postflop_checks += 1

            if action_type == "raise":
                raise_sizes.append(action / BIG_BLIND)

            if pending_my_pressure:
                fold_to_raise_opportunities += 1
                if action_type == "fold":
                    fold_to_raise += 1
                pending_my_pressure = False

    confidence = clamp((total_actions - 5) / 35.0, 0.0, 1.0)
    avg_raise_bb = sum(raise_sizes) / len(raise_sizes) if raise_sizes else 2.6

    return {
        "confidence": confidence,
        "vpip": smooth_rate(voluntary_preflop, preflop_opportunities, 0.58, 4.0),
        "pfr": smooth_rate(preflop_raise, preflop_opportunities, 0.28, 4.0),
        "allin_rate": smooth_rate(allin_actions, total_actions, 0.05, 8.0),
        "postflop_aggr": smooth_rate(postflop_aggressive, postflop_actions, 0.36, 5.0),
        "postflop_check_rate": smooth_rate(postflop_checks, postflop_actions, 0.42, 5.0),
        "fold_to_raise": smooth_rate(fold_to_raise, fold_to_raise_opportunities, 0.44, 4.0),
        "aggression": smooth_rate(aggressive_actions, total_actions, 0.30, 6.0),
        "avg_raise_bb": avg_raise_bb,
    }


def analyze_current_spot(req, state):
    my_id = req["my_id"]
    opponent_id = next_player(my_id, 1)
    dealer_id = req["dealer_id"]
    sb = next_player(dealer_id, 1)
    bb = next_player(dealer_id, 2)
    history = req["history"]

    info = {
        "my_is_sb": my_id == sb,
        "my_is_bb": my_id == bb,
        "has_position": my_id == bb,
        "opp_preflop_raises": 0,
        "opp_round_raises": 0,
        "opp_total_raises": 0,
        "opp_postflop_bet_count": 0,
        "opp_current_round_bet_count": 0,
        "opp_postflop_check_count": 0,
        "opp_current_round_check_count": 0,
        "opp_prior_postflop_check_count": 0,
        "opp_prior_postflop_raise_count": 0,
        "opp_previous_round_raise_count": 0,
        "facing_raise": False,
        "facing_allin": state["opponent_allin"],
        "facing_postflop_aggression": False,
        "last_opp_action_type": None,
        "last_raise_bb": 0.0,
        "last_raise_pot_ratio": 0.0,
        "preflop_spot": "other",
    }

    for record in history:
        if record["player_id"] == opponent_id and record["round"] > 0 and record["action_type"] == "check":
            info["opp_postflop_check_count"] += 1
            if record["round"] == state["round"]:
                info["opp_current_round_check_count"] += 1
            elif record["round"] < state["round"]:
                info["opp_prior_postflop_check_count"] += 1

        if record["player_id"] != opponent_id or record["action_type"] not in ("raise", "allin"):
            continue
        info["opp_total_raises"] += 1
        if record["round"] == 0:
            info["opp_preflop_raises"] += 1
        if record["round"] > 0:
            info["opp_postflop_bet_count"] += 1
            if record["round"] < state["round"]:
                info["opp_prior_postflop_raise_count"] += 1
            if record["round"] == state["round"] - 1:
                info["opp_previous_round_raise_count"] += 1
        if record["round"] == state["round"]:
            info["opp_round_raises"] += 1
            if record["round"] > 0:
                info["opp_current_round_bet_count"] += 1

    if history and history[-1]["player_id"] == opponent_id:
        last = history[-1]
        info["last_opp_action_type"] = last["action_type"]
        if last["action_type"] in ("raise", "allin"):
            info["facing_raise"] = True
            info["facing_postflop_aggression"] = state["round"] > 0
            if last["action_type"] == "raise":
                info["last_raise_bb"] = last["action"] / BIG_BLIND
                info["last_raise_pot_ratio"] = last["action"] / max(1, state["pot"])
            else:
                info["last_raise_bb"] = state["allin_call_amount"] / max(1, BIG_BLIND)
                info["last_raise_pot_ratio"] = state["allin_call_amount"] / max(1, state["pot"])

    if state["round"] == 0:
        if not history and info["my_is_sb"]:
            info["preflop_spot"] = "sb_open"
        elif history and info["my_is_bb"] and history[-1]["player_id"] == opponent_id:
            if history[-1]["action_type"] == "call":
                info["preflop_spot"] = "bb_vs_limp"
            elif history[-1]["action_type"] in ("raise", "allin"):
                info["preflop_spot"] = "bb_vs_raise"
        elif history and info["my_is_sb"] and history[-1]["player_id"] == opponent_id:
            if history[-1]["action_type"] in ("raise", "allin"):
                info["preflop_spot"] = "sb_vs_reraise"

    return info


def made_hand_metric(hole_cards, public_cards):
    if len(public_cards) < 3:
        return 0.0
    score = evaluate_best(hole_cards + public_cards)
    metric = HAND_CLASS_SCORE[score[0]]
    detail = 0.0
    for idx, rank in enumerate(score[1:4]):
        detail += rank / (16.0 * (2 ** idx))
    return clamp(metric + detail * 0.008, 0.0, 0.995)


def pair_board_profile(hole_cards, public_cards):
    info = {
        "made_class": -1,
        "pair_rank": None,
        "pair_type": "none",
        "kicker_rank": 0,
        "board_overcards": 0,
        "uses_hole_card": False,
        "weak_kicker": False,
    }

    if len(public_cards) < 3:
        return info

    score = evaluate_best(hole_cards + public_cards)
    info["made_class"] = score[0]
    if score[0] != 1:
        return info

    pair_rank = score[1]
    hole_ranks = [card_number(card) for card in hole_cards]
    board_ranks = [card_number(card) for card in public_cards]
    board_unique = sorted(set(board_ranks), reverse=True)

    info["pair_rank"] = pair_rank
    info["board_overcards"] = sum(1 for rank in set(board_ranks) if rank > pair_rank)

    uses_hole = pair_rank in hole_ranks
    info["uses_hole_card"] = uses_hole

    hole_kickers = [rank for rank in hole_ranks if rank != pair_rank]
    if hole_kickers:
        info["kicker_rank"] = max(hole_kickers)
    else:
        board_kickers = [rank for rank in board_ranks if rank != pair_rank]
        info["kicker_rank"] = max(board_kickers, default=0)

    info["weak_kicker"] = info["kicker_rank"] <= 9

    if not uses_hole:
        info["pair_type"] = "board_pair"
        return info

    pocket_pair = hole_ranks[0] == hole_ranks[1] and hole_ranks[0] == pair_rank
    if pocket_pair:
        if board_ranks and pair_rank > max(board_ranks):
            info["pair_type"] = "overpair"
        elif info["board_overcards"] >= 1:
            info["pair_type"] = "underpair"
        else:
            info["pair_type"] = "pocket_pair"
        return info

    if board_unique and pair_rank == board_unique[0]:
        info["pair_type"] = "top_pair"
    elif len(board_unique) >= 2 and pair_rank == board_unique[1]:
        info["pair_type"] = "middle_pair"
    else:
        info["pair_type"] = "bottom_pair"

    return info


def pair_domination_margin(pair_profile, spot_info, round_idx):
    if pair_profile is None or pair_profile["made_class"] != 1:
        return 0.0

    pair_type = pair_profile["pair_type"]
    margin = 0.0

    if pair_type == "top_pair":
        margin += 0.012 if pair_profile["weak_kicker"] else 0.004
    elif pair_type == "middle_pair":
        margin += 0.030
        if pair_profile["weak_kicker"]:
            margin += 0.012
    elif pair_type == "bottom_pair":
        margin += 0.050
        if pair_profile["weak_kicker"]:
            margin += 0.012
    elif pair_type == "underpair":
        margin += 0.045 + 0.010 * pair_profile["board_overcards"]
    elif pair_type == "board_pair":
        margin += 0.065

    if spot_info["facing_postflop_aggression"]:
        margin += 0.010
    if spot_info.get("opp_postflop_bet_count", 0) >= 2:
        margin += 0.012
    if round_idx == 3 and pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
        margin += 0.015

    return clamp(margin, 0.0, 0.10)


def marginal_pair_under_pressure(pair_profile, board_texture):
    if pair_profile is None or pair_profile["made_class"] != 1:
        return False

    pair_type = pair_profile["pair_type"]
    if pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
        return True
    if pair_type == "top_pair" and pair_profile["weak_kicker"]:
        return True
    if pair_type == "top_pair" and board_texture is not None:
        return board_texture["high_card"] >= 14 and pair_profile["kicker_rank"] <= 11
    return False


def board_texture_profile(public_cards):
    info = {
        "wetness": 0.0,
        "flush_pressure": 0.0,
        "straight_pressure": 0.0,
        "paired": False,
        "high_card": 0,
        "dynamic": False,
    }

    if len(public_cards) < 3:
        return info

    board_ranks = [card_number(card) for card in public_cards]
    board_suits = [card_suit(card) for card in public_cards]
    info["high_card"] = max(board_ranks)
    info["paired"] = len(set(board_ranks)) < len(board_ranks)

    suit_counts = {}
    for suit in board_suits:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    max_suit = max(suit_counts.values())

    if max_suit >= 4:
        info["flush_pressure"] = 1.0
    elif max_suit == 3:
        info["flush_pressure"] = 0.75
    elif max_suit == 2 and len(public_cards) >= 4:
        info["flush_pressure"] = 0.35

    ranks = set(board_ranks)
    expanded = set(ranks)

    best_straight_pressure = 0.0
    for start in range(1, 11):
        window = set(range(start, start + 5))
        present = len(expanded & window)
        if present >= 4:
            best_straight_pressure = max(best_straight_pressure, 1.0)
        elif present == 3:
            best_straight_pressure = max(best_straight_pressure, 0.65)
        elif present == 2 and max(window & expanded, default=start) - min(window & expanded, default=start) <= 3:
            best_straight_pressure = max(best_straight_pressure, 0.28)

    info["straight_pressure"] = best_straight_pressure

    wetness = 0.18 * info["flush_pressure"]
    wetness += 0.22 * info["straight_pressure"]
    if info["high_card"] >= 12:
        wetness += 0.03
    if len(public_cards) >= 4 and not info["paired"]:
        wetness += 0.04
    if info["paired"]:
        wetness -= 0.06

    info["wetness"] = clamp(wetness, 0.0, 1.0)
    info["dynamic"] = (
        info["flush_pressure"] >= 0.75
        or info["straight_pressure"] >= 0.65
        or info["wetness"] >= 0.45
    )
    return info


def paired_board_outcome_profile(hole_cards, public_cards):
    info = {
        "board_paired": False,
        "board_pair_rank": 0,
        "board_pair_count": 0,
        "hand_class": -1,
        "uses_board_pair": False,
        "board_two_pair": False,
        "trips_vulnerable": False,
        "strengthened": False,
        "weakened": False,
        "fragile_two_pair": False,
        "prefer_check": False,
        "fold_to_raise": False,
        "label": "none",
    }

    if len(public_cards) < 3:
        return info

    board_counts = {}
    for card in public_cards:
        rank = card_number(card)
        board_counts[rank] = board_counts.get(rank, 0) + 1
    paired_ranks = sorted(
        ((rank, count) for rank, count in board_counts.items() if count >= 2),
        reverse=True,
    )
    if not paired_ranks:
        return info

    board_pair_rank, board_pair_count = paired_ranks[0]
    score = evaluate_best(hole_cards + public_cards)
    hand_class = score[0]
    hole_ranks = [card_number(card) for card in hole_cards]
    top_unpaired_board_rank = max(
        (rank for rank in board_counts if rank != board_pair_rank),
        default=0,
    )

    info["board_paired"] = True
    info["board_pair_rank"] = board_pair_rank
    info["board_pair_count"] = board_pair_count
    info["hand_class"] = hand_class

    if hand_class >= 6:
        info["strengthened"] = True
        info["label"] = "trips_plus_on_paired_board"
        return info

    if hand_class == 3:
        trips_rank = score[1]
        if trips_rank == board_pair_rank or hole_ranks.count(trips_rank) == 2:
            info["strengthened"] = True
            info["label"] = "strong_trips_on_paired_board"
        else:
            info["weakened"] = True
            info["prefer_check"] = True
            info["label"] = "fragile_trips_on_paired_board"
        return info

    if hand_class != 2:
        return info

    high_pair = score[1]
    low_pair = score[2]
    uses_board_pair = board_pair_rank in (high_pair, low_pair)
    pocket_pair = hole_ranks[0] == hole_ranks[1]
    info["uses_board_pair"] = uses_board_pair
    info["trips_vulnerable"] = uses_board_pair

    if uses_board_pair and pocket_pair and high_pair == hole_ranks[0] and low_pair == board_pair_rank:
        info["board_two_pair"] = True
        info["fold_to_raise"] = True
        info["label"] = "overpair_two_pair_on_paired_board"
        return info

    if uses_board_pair:
        other_pair = low_pair if high_pair == board_pair_rank else high_pair
        if high_pair == board_pair_rank and other_pair <= 6:
            info["weakened"] = True
            info["fragile_two_pair"] = True
            info["label"] = "low_two_pair_on_paired_board"
        elif high_pair == board_pair_rank and top_unpaired_board_rank > other_pair:
            info["weakened"] = True
            info["fragile_two_pair"] = True
            info["label"] = "dominated_two_pair_on_paired_board"
        elif low_pair == board_pair_rank and high_pair < top_unpaired_board_rank:
            info["weakened"] = True
            info["label"] = "under_top_two_pair_on_paired_board"
        elif low_pair == board_pair_rank and high_pair < 11:
            info["weakened"] = True
            info["fragile_two_pair"] = True
            info["label"] = "thin_two_pair_on_paired_board"
        else:
            info["label"] = "top_two_pair_with_board_pair"
    else:
        if high_pair == top_unpaired_board_rank and high_pair >= 11:
            info["strengthened"] = True
            info["label"] = "top_two_pair_above_board_pair"
        else:
            info["weakened"] = True
            info["label"] = "disconnected_two_pair_on_paired_board"
            if high_pair < 12:
                info["fragile_two_pair"] = True

    if info["weakened"] and not info["strengthened"]:
        info["prefer_check"] = True
    if info["fragile_two_pair"]:
        info["prefer_check"] = True
        info["fold_to_raise"] = True
    elif info["label"] in ("under_top_two_pair_on_paired_board", "disconnected_two_pair_on_paired_board"):
        info["fold_to_raise"] = True

    return info


def bet_size_bucket(last_raise_pot_ratio):
    if last_raise_pot_ratio <= 0.30:
        return "small"
    if last_raise_pot_ratio <= 0.75:
        return "medium"
    return "large"


def value_hand_tier(hole_cards, public_cards, pair_profile=None, board_texture=None, paired_board_profile=None):
    info = {
        "tier": "none",
        "is_value": False,
        "size_bonus": 0.0,
    }

    if len(public_cards) < 3:
        return info

    if pair_profile is None:
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
        board_texture = board_texture_profile(public_cards)
    if paired_board_profile is None:
        paired_board_profile = paired_board_outcome_profile(hole_cards, public_cards)

    score = evaluate_best(hole_cards + public_cards)
    hand_class = score[0]
    wetness = board_texture["wetness"]
    hole_ranks = [card_number(card) for card in hole_cards]
    size_bonus = 0.0
    tier = "none"

    if hand_class >= 6:
        tier = "nut"
        size_bonus = 0.22 + 0.08 * wetness
    elif hand_class == 5:
        flush_profile = made_flush_profile(hole_cards, public_cards, board_texture)
        if flush_profile["nut_like"]:
            tier = "nut"
            size_bonus = 0.18 + 0.06 * wetness
        elif flush_profile["high_hole_rank"] >= 12 and flush_profile["better_unseen_ranks"] <= 1:
            tier = "strong"
            size_bonus = 0.15 + 0.06 * wetness
        elif flush_profile["high_hole_rank"] >= 10:
            tier = "strong"
            size_bonus = 0.12 + 0.04 * wetness
        else:
            tier = "thin"
            size_bonus = 0.05 + 0.02 * wetness
    elif hand_class == 4:
        tier = "strong"
        size_bonus = 0.12 + 0.05 * wetness
    elif hand_class == 3:
        set_made = hole_ranks.count(score[1]) == 2
        tier = "nut" if set_made and board_texture["dynamic"] else "strong"
        size_bonus = 0.20 if tier == "nut" else 0.13 + 0.05 * wetness
    elif hand_class == 2:
        if paired_board_profile["board_paired"]:
            if paired_board_profile["board_two_pair"]:
                tier = "strong"
                size_bonus = 0.02 - 0.02 * wetness
            elif paired_board_profile["fragile_two_pair"]:
                tier = "thin"
                size_bonus = -0.01 - 0.03 * wetness
            elif paired_board_profile["weakened"]:
                tier = "thin"
                size_bonus = 0.02 - 0.03 * wetness
            elif paired_board_profile["label"] == "top_two_pair_above_board_pair":
                tier = "strong"
                size_bonus = 0.07 + 0.03 * wetness
            else:
                tier = "strong"
                size_bonus = 0.07 + 0.04 * wetness
        else:
            tier = "strong"
            size_bonus = 0.10 + 0.06 * wetness
    elif hand_class == 1 and pair_profile["made_class"] == 1:
        pair_type = pair_profile["pair_type"]
        if pair_type == "overpair":
            tier = "strong"
            size_bonus = 0.13 + 0.07 * wetness
        elif pair_type == "top_pair":
            if pair_profile["weak_kicker"]:
                tier = "thin"
                size_bonus = 0.01 - 0.03 * wetness
            else:
                tier = "strong" if pair_profile["pair_rank"] >= 11 else "thin"
                size_bonus = 0.09 + 0.03 * wetness if tier == "strong" else 0.03 - 0.02 * wetness
        elif pair_type == "pocket_pair":
            tier = "thin"
            size_bonus = 0.00 - 0.03 * wetness

    info["tier"] = tier
    info["is_value"] = tier != "none"
    info["size_bonus"] = clamp(size_bonus, -0.04, 0.24)
    return info


def value_bet_plan(value_profile, board_texture, paired_board_profile, pair_profile, nutted_risk, round_idx, pot):
    plan = {
        "size_delta": 0.0,
        "induce": False,
        "protect": False,
        "thin_control": False,
        "label": "normal",
    }

    if value_profile is None or board_texture is None or round_idx <= 0:
        return plan

    tier = value_profile.get("tier", "none")
    if tier == "none":
        return plan

    wetness = board_texture["wetness"]
    dynamic_board = board_texture["dynamic"]
    draw_heavy = board_texture["flush_pressure"] >= 0.75 or board_texture["straight_pressure"] >= 0.65
    paired_warning = (
        paired_board_profile is not None
        and paired_board_profile["board_paired"]
        and paired_board_profile["prefer_check"]
    )
    risk = nutted_risk.get("risk", 0.0) if nutted_risk is not None else 0.0

    if tier == "nut":
        if risk > 0.03:
            plan["size_delta"] -= min(0.08, 0.80 * risk)
            plan["label"] = "nutted_risk_control"
            return plan
        if dynamic_board:
            plan["protect"] = True
            plan["size_delta"] += 0.03 + 0.04 * wetness
            plan["label"] = "nut_value_dynamic"
        else:
            plan["induce"] = pot < 2600
            plan["size_delta"] -= 0.12 if round_idx < 3 else 0.16
            plan["label"] = "nut_value_induce"
        return plan

    if tier == "strong":
        vulnerable_pair = (
            pair_profile is not None
            and pair_profile["made_class"] == 1
            and pair_profile["pair_type"] in ("overpair", "top_pair")
        )
        if dynamic_board or draw_heavy:
            plan["protect"] = True
            plan["size_delta"] += 0.07 + 0.05 * wetness
            if round_idx == 2:
                plan["size_delta"] += 0.02
            plan["label"] = "strong_value_protect"
        elif vulnerable_pair:
            plan["size_delta"] -= 0.03
            plan["label"] = "strong_pair_static_control"

    if tier == "thin":
        plan["thin_control"] = True
        plan["size_delta"] -= 0.04 + 0.04 * wetness
        plan["label"] = "thin_value_control"

    if paired_warning and tier != "nut":
        plan["thin_control"] = True
        plan["size_delta"] -= 0.06
        plan["label"] = "paired_board_control"

    if risk > 0.0 and tier != "nut":
        plan["size_delta"] -= min(0.08, 0.45 * risk)

    plan["size_delta"] = clamp(plan["size_delta"], -0.18, 0.16)
    return plan


def straight_draw_value(cards):
    ranks = {card_number(card) for card in cards}
    expanded = set(ranks)

    best = 0.0
    for start in range(1, 11):
        straight = set(range(start, start + 5))
        present = len(expanded & straight)
        if present != 4:
            continue
        missing = next(iter(straight - expanded))
        if missing in (start, start + 4):
            best = max(best, 0.17)
        else:
            best = max(best, 0.09)
    return best


def empty_draw_profile():
    return {
        "quality": 0.0,
        "type": "none",
        "flush_draw": False,
        "nut_flush_draw": False,
        "near_nut_flush_draw": False,
        "high_flush_draw": False,
        "flush_draw_rank": 0,
        "better_flush_draw_ranks": 0,
        "straight_draw": "none",
        "combo_draw": False,
        "overcards": 0,
        "semi_bluff": False,
        "fold_threshold_delta": 0.0,
        "size_bonus": 0.0,
    }


def draw_profile(hole_cards, public_cards, board_texture=None):
    info = empty_draw_profile()
    if len(public_cards) < 3 or len(public_cards) >= 5:
        return info

    if board_texture is None:
        board_texture = board_texture_profile(public_cards)

    cards = hole_cards + public_cards
    hole_ranks = [card_number(card) for card in hole_cards]
    board_high = max(card_number(card) for card in public_cards)
    info["overcards"] = sum(1 for rank in hole_ranks if rank > board_high)

    suit_counts = {}
    for card in cards:
        suit = card_suit(card)
        suit_counts[suit] = suit_counts.get(suit, 0) + 1

    flush_quality = 0.0
    best_flush_rank = 0
    best_better_flush_ranks = 0
    for suit, count in suit_counts.items():
        if count != 4:
            continue
        hole_flush_ranks = sorted(
            (card_number(card) for card in hole_cards if card_suit(card) == suit),
            reverse=True,
        )
        if not hole_flush_ranks:
            continue

        board_flush_ranks = [card_number(card) for card in public_cards if card_suit(card) == suit]
        high_flush_rank = max(hole_flush_ranks)
        seen_flush_ranks = set(hole_flush_ranks + board_flush_ranks)
        better_flush_ranks = len([rank for rank in range(high_flush_rank + 1, 15) if rank not in seen_flush_ranks])
        nut_draw = better_flush_ranks == 0
        info["flush_draw"] = True
        info["nut_flush_draw"] = info["nut_flush_draw"] or nut_draw

        candidate = 0.21 if nut_draw else 0.16
        if not nut_draw and high_flush_rank >= 12 and better_flush_ranks <= 1:
            candidate = max(candidate, 0.185)
        elif not nut_draw and high_flush_rank >= 11:
            candidate = max(candidate, 0.170)
        if high_flush_rank <= 9:
            candidate -= 0.025
        if board_texture["paired"] and not nut_draw:
            candidate -= 0.020
        if candidate > flush_quality:
            best_flush_rank = high_flush_rank
            best_better_flush_ranks = better_flush_ranks
        flush_quality = max(flush_quality, candidate)

    if info["flush_draw"]:
        info["flush_draw_rank"] = best_flush_rank
        info["better_flush_draw_ranks"] = best_better_flush_ranks
        info["near_nut_flush_draw"] = best_flush_rank >= 12 and best_better_flush_ranks <= 1
        info["high_flush_draw"] = best_flush_rank >= 11 and best_better_flush_ranks <= 3

    ranks = {card_number(card) for card in cards}
    expanded = set(ranks)
    hole_expanded = set(hole_ranks)

    straight_quality = 0.0
    gutshot_count = 0
    has_open_ended = False
    has_gutshot = False
    for start in range(1, 11):
        window = set(range(start, start + 5))
        present = expanded & window
        if len(present) != 4 or not (hole_expanded & present):
            continue
        missing = next(iter(window - present))
        if missing in (start, start + 4):
            has_open_ended = True
            straight_quality = max(straight_quality, 0.17)
        else:
            has_gutshot = True
            gutshot_count += 1
            straight_quality = max(straight_quality, 0.10)

    if has_open_ended:
        info["straight_draw"] = "open_ended"
    elif gutshot_count >= 2:
        info["straight_draw"] = "double_gutshot"
        straight_quality = max(straight_quality, 0.13)
    elif has_gutshot:
        info["straight_draw"] = "gutshot"

    info["combo_draw"] = info["flush_draw"] and info["straight_draw"] != "none"
    quality = max(flush_quality, straight_quality)
    if info["flush_draw"] and info["straight_draw"] != "none":
        quality = max(quality, flush_quality + straight_quality + 0.04)
    if len(public_cards) == 3:
        quality += 0.025 * info["overcards"]
    elif info["overcards"] >= 2:
        quality += 0.015

    if info["combo_draw"]:
        info["type"] = "combo_draw"
        info["fold_threshold_delta"] = 0.07
        info["size_bonus"] = 0.06
    elif info["nut_flush_draw"]:
        info["type"] = "nut_flush_draw"
        info["fold_threshold_delta"] = 0.05
        info["size_bonus"] = 0.04
    elif info["flush_draw"]:
        info["type"] = "flush_draw"
        if info["near_nut_flush_draw"]:
            info["fold_threshold_delta"] = 0.04
            info["size_bonus"] = 0.035
        elif info["high_flush_draw"]:
            info["fold_threshold_delta"] = 0.03
            info["size_bonus"] = 0.025
        else:
            info["fold_threshold_delta"] = 0.01 if info["flush_draw_rank"] >= 10 else 0.0
            info["size_bonus"] = 0.015
    elif info["straight_draw"] == "open_ended":
        info["type"] = "open_ended_straight_draw"
        info["fold_threshold_delta"] = 0.03
        info["size_bonus"] = 0.02
    elif info["straight_draw"] == "double_gutshot":
        info["type"] = "double_gutshot"
        info["fold_threshold_delta"] = 0.02
        info["size_bonus"] = 0.01
    elif info["straight_draw"] == "gutshot":
        info["type"] = "gutshot"
        info["fold_threshold_delta"] = -0.03
        info["size_bonus"] = -0.02

    info["quality"] = clamp(quality, 0.0, 0.35)
    info["semi_bluff"] = (
        info["combo_draw"]
        or info["nut_flush_draw"]
        or info["straight_draw"] in ("open_ended", "double_gutshot")
        or (info["flush_draw"] and info["quality"] >= 0.16)
        or (info["straight_draw"] == "gutshot" and info["overcards"] >= 1 and info["quality"] >= 0.13)
    )
    return info


def draw_potential(hole_cards, public_cards):
    return draw_profile(hole_cards, public_cards)["quality"]


def draw_call_margin(draw_info, board_texture, round_idx, spot_info):
    if draw_info is None or draw_info["type"] == "none":
        return 0.0

    margin = 0.0
    draw_type = draw_info["type"]
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])

    if draw_type == "combo_draw":
        margin -= 0.035
    elif draw_type == "nut_flush_draw":
        margin -= 0.025
    elif draw_type == "open_ended_straight_draw":
        margin -= 0.012
    elif draw_type == "double_gutshot":
        margin -= 0.006
    elif draw_type == "gutshot":
        margin += 0.040
    elif draw_type == "flush_draw" and not draw_info["nut_flush_draw"]:
        if draw_info.get("near_nut_flush_draw", False):
            margin -= 0.010
        elif draw_info.get("high_flush_draw", False):
            margin += 0.004
        else:
            margin += 0.020

    if (
        draw_type == "flush_draw"
        and draw_info.get("high_flush_draw", False)
        and size_bucket in ("small", "medium")
        and spot_info.get("has_position", False)
        and (board_texture is None or not board_texture["paired"])
    ):
        margin -= 0.006

    if board_texture is not None:
        if board_texture["paired"] and draw_info["flush_draw"] and not draw_info["nut_flush_draw"]:
            margin += 0.030
        if board_texture["flush_pressure"] >= 0.75 and draw_type == "open_ended_straight_draw":
            margin += 0.008

    if round_idx == 2:
        if draw_type == "gutshot":
            margin += 0.020
        elif draw_type == "flush_draw":
            if draw_info.get("near_nut_flush_draw", False) and size_bucket != "large" and (board_texture is None or not board_texture["paired"]):
                margin += 0.000
            elif draw_info.get("high_flush_draw", False) and size_bucket == "small" and (board_texture is None or not board_texture["paired"]):
                margin += 0.006
            else:
                margin += 0.020
    elif round_idx == 3:
        margin += 0.050

    if size_bucket == "large":
        if draw_type == "gutshot":
            margin += 0.018
        elif draw_type == "flush_draw":
            if draw_info.get("near_nut_flush_draw", False):
                margin += 0.006
            elif draw_info.get("high_flush_draw", False):
                margin += 0.012
            else:
                margin += 0.018

    return clamp(margin, -0.04, 0.08)


def made_flush_profile(hole_cards, public_cards, board_texture=None):
    info = {
        "is_flush": False,
        "flush_suit": None,
        "hole_flush_ranks": [],
        "board_flush_ranks": [],
        "high_hole_rank": 0,
        "better_unseen_ranks": 0,
        "nut_like": False,
        "repressure_continue": False,
    }

    if len(public_cards) < 3:
        return info

    if board_texture is None:
        board_texture = board_texture_profile(public_cards)

    score = evaluate_best(hole_cards + public_cards)
    if score[0] != 5:
        return info

    suit_counts = {}
    for card in hole_cards + public_cards:
        suit = card_suit(card)
        suit_counts[suit] = suit_counts.get(suit, 0) + 1

    flush_suits = [suit for suit, count in suit_counts.items() if count >= 5]
    if not flush_suits:
        return info

    flush_suit = max(
        flush_suits,
        key=lambda suit: sorted(
            (card_number(card) for card in hole_cards + public_cards if card_suit(card) == suit),
            reverse=True,
        )[:5],
    )
    hole_flush_ranks = sorted(
        (card_number(card) for card in hole_cards if card_suit(card) == flush_suit),
        reverse=True,
    )
    board_flush_ranks = sorted(
        (card_number(card) for card in public_cards if card_suit(card) == flush_suit),
        reverse=True,
    )

    if not hole_flush_ranks:
        return info

    high_hole = hole_flush_ranks[0]
    seen_flush_ranks = set(hole_flush_ranks + board_flush_ranks)
    better_unseen = [rank for rank in range(high_hole + 1, 15) if rank not in seen_flush_ranks]

    info["is_flush"] = True
    info["flush_suit"] = flush_suit
    info["hole_flush_ranks"] = hole_flush_ranks
    info["board_flush_ranks"] = board_flush_ranks
    info["high_hole_rank"] = high_hole
    info["better_unseen_ranks"] = len(better_unseen)
    info["nut_like"] = len(better_unseen) == 0

    three_flush_board = len(board_flush_ranks) == 3
    high_private_flush = high_hole >= 11 and len(better_unseen) <= 2
    info["repressure_continue"] = (
        not board_texture["paired"]
        and three_flush_board
        and high_private_flush
    )
    return info


def blocker_bluff_profile(hole_cards, public_cards, pair_profile=None, board_texture=None):
    info = {
        "eligible": False,
        "score": 0.0,
        "type": "none",
    }

    if len(public_cards) < 3:
        return info

    if pair_profile is None:
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
        board_texture = board_texture_profile(public_cards)

    score = evaluate_best(hole_cards + public_cards)
    if score[0] >= 1 and pair_profile["pair_type"] != "board_pair":
        return info

    board_suits = [card_suit(card) for card in public_cards]
    suit_counts = {}
    for suit in board_suits:
        suit_counts[suit] = suit_counts.get(suit, 0) + 1
    target_suit = max(suit_counts, key=suit_counts.get)
    max_board_suit = suit_counts[target_suit]

    blocker_score = 0.0
    bluff_type = "none"
    suited_hole_ranks = sorted(
        (card_number(card) for card in hole_cards if card_suit(card) == target_suit),
        reverse=True,
    )
    if max_board_suit >= 3 and suited_hole_ranks:
        high_blocker = suited_hole_ranks[0]
        if high_blocker == 14:
            blocker_score += 0.24
            bluff_type = "flush_ace_blocker"
        elif high_blocker == 13:
            blocker_score += 0.18
            bluff_type = "flush_king_blocker"
        elif high_blocker == 12:
            blocker_score += 0.11
            bluff_type = "flush_queen_blocker"

    hole_ranks = [card_number(card) for card in hole_cards]
    if board_texture["paired"] and max(hole_ranks) >= 13:
        blocker_score += 0.05
        if bluff_type == "none":
            bluff_type = "paired_board_blocker"
    if board_texture["high_card"] >= 12 and 14 in hole_ranks:
        blocker_score += 0.04
        if bluff_type == "none":
            bluff_type = "ace_high_blocker"
    if board_texture["straight_pressure"] >= 0.65 and max(hole_ranks) >= max(10, board_texture["high_card"] - 1):
        blocker_score += 0.04
        if bluff_type == "none":
            bluff_type = "straight_pressure_blocker"

    info["score"] = blocker_score
    info["type"] = bluff_type
    info["eligible"] = blocker_score >= 0.14
    return info


def allow_low_frequency_blocker_bluff(req, hole_cards, public_cards, blocker_profile, round_idx):
    if not blocker_profile["eligible"]:
        return False

    hand_idx = get_hand_index(req) or 0
    token = (sum(hole_cards) * 7 + sum(public_cards) * 11 + hand_idx * 13 + round_idx * 17) % 100
    threshold = int(clamp(blocker_profile["score"] * 35.0, 5.0, 18.0))
    return token < threshold


def nutted_risk_profile(hole_cards, public_cards, pair_profile=None, board_texture=None, value_profile=None, paired_board_profile=None):
    info = {
        "risk": 0.0,
        "label": "none",
        "vulnerable": False,
    }

    if len(public_cards) < 3:
        return info

    if pair_profile is None:
        pair_profile = pair_board_profile(hole_cards, public_cards)
    if board_texture is None:
        board_texture = board_texture_profile(public_cards)
    if paired_board_profile is None:
        paired_board_profile = paired_board_outcome_profile(hole_cards, public_cards)
    if value_profile is None:
        value_profile = value_hand_tier(hole_cards, public_cards, pair_profile, board_texture, paired_board_profile)

    score = evaluate_best(hole_cards + public_cards)
    hand_class = score[0]
    board_ranks = [card_number(card) for card in public_cards]
    hole_ranks = [card_number(card) for card in hole_cards]

    risk = 0.0
    label = "none"

    if hand_class == 6:
        if len(set(board_ranks)) <= 3 and score[1] < max(board_ranks):
            risk += 0.045
            label = "low_full_house"
    elif hand_class == 5:
        flush_profile = made_flush_profile(hole_cards, public_cards, board_texture)
        if flush_profile["nut_like"]:
            label = "nut_like_flush"
        elif flush_profile["high_hole_rank"] >= 12 and flush_profile["better_unseen_ranks"] <= 1:
            risk += 0.010
            label = "near_nut_flush"
        elif flush_profile["high_hole_rank"] >= 10:
            risk += 0.025
            label = "medium_flush"
        else:
            risk += 0.050
            label = "weak_flush"
        if board_texture["paired"]:
            risk += 0.03
            label += "_paired_board"
    elif hand_class == 4:
        if board_texture["flush_pressure"] >= 0.75:
            risk += 0.04
            label = "straight_on_flush_board"
        if board_texture["paired"]:
            risk += 0.03
            label = "straight_on_paired_board"
    elif hand_class == 3:
        if board_texture["paired"]:
            if paired_board_profile["strengthened"]:
                risk += 0.02
                label = paired_board_profile["label"]
            else:
                risk += 0.04
                label = "trips_on_paired_board"
        if score[1] < max(board_ranks):
            risk += 0.02
    elif hand_class == 2 and board_texture["paired"]:
        if paired_board_profile["board_two_pair"]:
            risk += 0.07
            label = paired_board_profile["label"]
        elif paired_board_profile["fragile_two_pair"]:
            risk += 0.08
            label = paired_board_profile["label"]
        elif paired_board_profile["weakened"]:
            risk += 0.06
            label = paired_board_profile["label"]
        elif paired_board_profile["strengthened"]:
            risk += 0.03
            label = paired_board_profile["label"]
        else:
            risk += 0.05
            label = "two_pair_on_paired_board"
    elif hand_class == 1 and value_profile["tier"] in ("strong", "thin"):
        if board_texture["flush_pressure"] >= 1.0:
            risk += 0.03
            label = "pair_on_four_flush"
        if board_texture["straight_pressure"] >= 1.0:
            risk += 0.02
            label = "pair_on_four_straight"

    info["risk"] = clamp(risk, 0.0, 0.14)
    info["label"] = label
    info["vulnerable"] = info["risk"] >= 0.04
    return info


def combo_range_weight(combo, public_cards, state, opponent_model, spot_info):
    preflop = max(0.05, estimate_preflop_strength(list(combo)))
    confidence = opponent_model["confidence"]

    weight = 0.15 + preflop
    if spot_info["opp_preflop_raises"] > 0:
        pressure = 0.95 + 0.35 * spot_info["opp_preflop_raises"] + 0.20 * spot_info["last_raise_bb"]
        pressure += confidence * max(0.0, 0.42 - opponent_model["pfr"]) * 1.8
        pressure -= confidence * max(0.0, opponent_model["pfr"] - 0.38) * 0.9
        pressure -= confidence * opponent_model["allin_rate"] * 0.8
        weight *= preflop ** clamp(pressure, 0.70, 2.80)
    else:
        flatten = 0.80 - confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.5
        weight *= preflop ** clamp(flatten, 0.55, 1.10)

    if len(public_cards) >= 3:
        made = made_hand_metric(list(combo), public_cards)
        draw = draw_potential(list(combo), public_cards)
        post_metric = max(made, 0.16 + draw)

        if spot_info["facing_postflop_aggression"] or (spot_info["facing_allin"] and state["round"] > 0):
            pressure = 0.95 + 0.25 * spot_info["opp_round_raises"] + 0.35 * spot_info["last_raise_pot_ratio"]
            pressure += confidence * max(0.0, 0.34 - opponent_model["postflop_aggr"]) * 1.6
            pressure -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.46) * 0.8
            weight *= max(0.08, post_metric) ** clamp(pressure, 0.75, 2.80)
        else:
            loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.50) * 0.35
            weight *= 0.40 + post_metric + loose_bonus * 0.10

    if spot_info["facing_allin"] and state["round"] == 0:
        jam_pressure = 0.90 + confidence * max(0.0, 0.06 - opponent_model["allin_rate"]) * 4.0
        weight *= preflop ** clamp(jam_pressure, 0.90, 2.80)

    return max(weight, 1e-6)


def build_opponent_range(my_cards, public_cards, state, opponent_model, spot_info):
    used = set(my_cards + public_cards)
    deck = [card for card in range(52) if card not in used]
    combos = []
    weights = []
    for first, second in itertools.combinations(deck, 2):
        combo = (first, second)
        combos.append(combo)
        weights.append(combo_range_weight(combo, public_cards, state, opponent_model, spot_info))
    return combos, weights


def build_cumulative_weights(weights):
    cumulative = []
    total = 0.0
    for weight in weights:
        total += weight
        cumulative.append(total)
    return cumulative, total


def weighted_choice_index(cumulative, total_weight):
    target = random.random() * total_weight
    return bisect.bisect_left(cumulative, target)


def exact_weighted_river_equity(my_cards, public_cards, combos, weights):
    my_score = evaluate_7(my_cards + public_cards)
    wins = 0.0
    total = 0.0

    for combo, weight in zip(combos, weights):
        opponent_score = evaluate_7(list(combo) + public_cards)
        total += weight
        if my_score > opponent_score:
            wins += weight
        elif my_score == opponent_score:
            wins += 0.5 * weight

    return 0.5 if total <= 0 else wins / total


def monte_carlo_weighted_equity(my_cards, public_cards, combos, cumulative, total_weight, iterations):
    if total_weight <= 0 or not combos:
        return 0.5

    used = set(my_cards + public_cards)
    deck = [card for card in range(52) if card not in used]
    need_public = 5 - len(public_cards)

    wins = 0.0
    for _ in range(iterations):
        combo = combos[weighted_choice_index(cumulative, total_weight)]
        combo_used = set(combo)
        rest_public_pool = [card for card in deck if card not in combo_used]
        rest_public = random.sample(rest_public_pool, need_public)
        board = public_cards + rest_public
        my_score = evaluate_7(my_cards + board)
        opponent_score = evaluate_7(list(combo) + board)
        if my_score > opponent_score:
            wins += 1.0
        elif my_score == opponent_score:
            wins += 0.5

    return wins / max(1, iterations)


def estimate_weighted_win_rate(my_cards, public_cards, combos, weights, iterations):
    if len(public_cards) == 5:
        return exact_weighted_river_equity(my_cards, public_cards, combos, weights)

    cumulative, total_weight = build_cumulative_weights(weights)
    return monte_carlo_weighted_equity(my_cards, public_cards, combos, cumulative, total_weight, iterations)


def match_risk_adjustment(req, my_id, remaining_hands):
    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= my_id:
        return 0.0
    if remaining_hands is None or remaining_hands <= 0:
        return 0.0

    lead = total_win_chips[my_id]
    scale = max(1.0, remaining_hands * BIG_BLIND * 5.0)
    if lead >= 0:
        return min(0.05, lead / scale)
    return -min(0.05, (-lead) / (scale * 0.85))


def match_pressure_profile(req, my_id, remaining_hands):
    profile = {
        "protect": 0.0,
        "chase": 0.0,
        "threshold_delta": 0.0,
        "sizing_delta": 0.0,
        "open_delta": 0.0,
        "bluff_delta": 0.0,
    }

    total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
    if len(total_win_chips) <= my_id or remaining_hands is None or remaining_hands <= 0:
        return profile

    try:
        lead = int(total_win_chips[my_id])
    except (TypeError, ValueError):
        return profile

    hands = max(1, int(remaining_hands))
    late_factor = clamp((12 - hands) / 10.0, 0.0, 1.0)
    if late_factor <= 0.0:
        return profile

    behind_per_hand = max(0.0, -lead) / hands
    ahead_per_hand = max(0.0, lead) / hands

    chase = clamp((behind_per_hand - 0.8 * BIG_BLIND) / (3.5 * BIG_BLIND), 0.0, 1.0)
    protect = clamp((ahead_per_hand - 0.8 * BIG_BLIND) / (3.0 * BIG_BLIND), 0.0, 1.0)
    chase *= late_factor
    protect *= late_factor
    if not (hands <= 8 and lead < -8 * BIG_BLIND):
        chase = min(chase, 0.25)

    profile["protect"] = protect
    profile["chase"] = chase
    profile["threshold_delta"] = 0.055 * protect - 0.055 * chase
    profile["sizing_delta"] = -0.10 * protect + 0.16 * chase
    profile["open_delta"] = 0.020 * protect - 0.030 * chase
    profile["bluff_delta"] = -0.08 * protect + 0.10 * chase
    return profile


def apply_anti_lock_pressure(match_profile):
    match_profile["protect"] = 0.0
    match_profile["chase"] = max(match_profile["chase"], 0.90)
    match_profile["threshold_delta"] = min(match_profile["threshold_delta"], -0.075)
    match_profile["sizing_delta"] = max(match_profile["sizing_delta"], 0.18)
    match_profile["open_delta"] = min(match_profile["open_delta"], -0.045)
    match_profile["bluff_delta"] = max(match_profile["bluff_delta"], 0.13)
    return match_profile


def opponent_pressure_adjustment(opponent_model, spot_info, round_idx):
    confidence = opponent_model["confidence"]
    adjustment = 0.0

    if spot_info["facing_raise"] or spot_info["facing_allin"]:
        adjustment += confidence * max(0.0, 0.44 - opponent_model["pfr"]) * 0.07
        if round_idx > 0:
            adjustment += confidence * max(0.0, 0.36 - opponent_model["postflop_aggr"]) * 0.06
        adjustment -= confidence * max(0.0, opponent_model["allin_rate"] - 0.08) * 0.08
        adjustment -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.48) * 0.05
        adjustment += min(0.04, spot_info["last_raise_pot_ratio"] * 0.04)

    return clamp(adjustment, -0.05, 0.07)


def aggressive_line_strength(spot_info, board_texture):
    strength = 0.0
    if spot_info.get("opp_postflop_bet_count", 0) >= 2:
        strength += 0.04
    if spot_info.get("opp_current_round_bet_count", 0) >= 2:
        strength += 0.08 if board_texture is not None and board_texture["paired"] else 0.05
    if spot_info.get("opp_current_round_bet_count", 0) >= 3:
        strength += 0.03
    return clamp(strength, 0.0, 0.15)


def check_probe_resistance_margin(spot_info, opponent_model, round_idx):
    if round_idx <= 0 or not spot_info["facing_postflop_aggression"]:
        return 0.0

    margin = 0.0
    same_street_check_raise = (
        spot_info.get("opp_current_round_check_count", 0) > 0
        and spot_info.get("opp_current_round_bet_count", 0) > 0
    )
    delayed_resistance = (
        spot_info.get("opp_prior_postflop_check_count", 0) >= 2
        and spot_info.get("opp_current_round_bet_count", 0) > 0
    )

    if same_street_check_raise:
        margin += 0.035
    if delayed_resistance:
        margin += 0.018

    confidence = opponent_model.get("confidence", 0.0)
    if opponent_model.get("postflop_check_rate", 0.42) >= 0.52:
        margin += confidence * 0.018

    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    if size_bucket == "large":
        margin += 0.020
    elif size_bucket == "medium":
        margin += 0.010

    return clamp(margin, 0.0, 0.085)


def must_continue_vs_raise(value_profile, made_strength, pot_odds, nutted_risk, board_texture):
    tier = value_profile.get("tier", "none") if value_profile is not None else "none"
    risk = nutted_risk.get("risk", 0.0) if nutted_risk is not None else 0.0
    extreme_texture = (
        board_texture is not None
        and (board_texture["flush_pressure"] >= 1.0 or board_texture["straight_pressure"] >= 1.0)
    )

    if tier == "nut":
        return True
    if made_strength >= 0.58 and pot_odds <= 0.42 and risk <= 0.07:
        return not (extreme_texture and risk >= 0.04)
    if tier == "strong" and pot_odds <= 0.36 and risk <= 0.05:
        return True
    return False


def anti_lock_continue_floor(pot_odds, round_idx, value_profile, draw_info, made_strength):
    discount = 0.07 + 0.025 * max(0, round_idx)

    if value_profile is not None:
        if value_profile.get("tier") == "nut":
            discount += 0.12
        elif value_profile.get("tier") == "strong":
            discount += 0.08
        elif value_profile.get("tier") == "thin":
            discount += 0.035

    if draw_info is not None:
        if draw_info.get("type") in ("combo_draw", "nut_flush_draw"):
            discount += 0.05
        elif draw_info.get("semi_bluff"):
            discount += 0.03

    if made_strength < 0.18 and (draw_info is None or draw_info.get("quality", 0.0) < 0.08):
        discount -= 0.04

    return max(0.08, pot_odds - discount)


def anti_lock_can_continue(anti_lock_pressure, win_rate, pot_odds, round_idx, value_profile, draw_info, made_strength):
    if not anti_lock_pressure:
        return False
    return win_rate >= anti_lock_continue_floor(pot_odds, round_idx, value_profile, draw_info, made_strength)


def choose_anti_lock_pressure_action(
    state,
    my_chips,
    to_call,
    pot,
    round_idx,
    win_rate,
    opponent_model,
    remaining_hands,
    preflop_strength=None,
    value_profile=None,
    draw_info=None,
    blocker_profile=None,
    board_texture=None,
):
    if state["opponent_allin"] or my_chips <= 1:
        return None
    if to_call >= my_chips:
        return -2

    hands_left = remaining_hands if remaining_hands is not None else TOTAL_HANDS
    pot_after_call = pot + to_call
    fold_to_raise = opponent_model.get("fold_to_raise", 0.44)
    confidence = opponent_model.get("confidence", 0.0)

    tier = value_profile.get("tier", "none") if value_profile is not None else "none"
    draw_quality = draw_info.get("quality", 0.0) if draw_info is not None else 0.0
    has_draw = draw_info.get("semi_bluff", False) if draw_info is not None else False
    has_blocker = blocker_profile is not None and blocker_profile.get("eligible", False)

    weak_showdown = tier in ("none", "thin") and draw_quality < 0.14 and win_rate < 0.45
    high_fold_pressure = confidence < 0.20 or fold_to_raise >= 0.42
    emergency_jam = (
        hands_left <= 3
        or (to_call > 0 and to_call / max(1, pot) >= 0.35)
        or (weak_showdown and high_fold_pressure and hands_left <= 6)
        or (win_rate < 0.18 and hands_left <= 5)
    )
    if tier in ("strong", "nut") or has_draw:
        emergency_jam = emergency_jam and hands_left <= 3

    if emergency_jam:
        return -2

    min_raise_action = state.get("min_raise_action", state["round_raise"])

    if round_idx == 0:
        ratio = 2.20 if to_call == 0 else 2.60
        target = int(to_call + pot_after_call * ratio)
        strength = preflop_strength if preflop_strength is not None else win_rate
        target = max(target, int((5.5 + max(0.0, strength - 0.50) * 3.0) * BIG_BLIND) - state["my_round_bet"])
    elif round_idx == 1:
        target = int(to_call + pot_after_call * 1.15)
    elif round_idx == 2:
        target = int(to_call + pot_after_call * 1.35)
    else:
        target = int(to_call + pot_after_call * 1.55)

    if board_texture is not None and board_texture.get("dynamic", False):
        target = int(target * 1.08)
    if has_blocker or has_draw:
        target = int(target * 1.06)
    if weak_showdown:
        target = int(target * 1.12)

    amount = max(min_raise_action, target)
    if amount >= my_chips * 0.72:
        return -2
    amount = min(amount, my_chips - 1)
    if amount <= to_call or amount < min_raise_action:
        return -2 if hands_left <= 4 else None
    return amount


def paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx):
    info = {
        "active": False,
        "severe": False,
        "line_strength": 0.0,
        "size_bucket": "small",
    }

    if round_idx <= 0 or board_texture is None or not board_texture["paired"]:
        return info

    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])
    line_strength = 0.0
    active = False

    if paired_board_profile is not None and paired_board_profile["board_two_pair"]:
        active = True
        line_strength += 0.05
    elif pair_profile is not None and pair_profile["pair_type"] == "overpair":
        active = True
        line_strength += 0.04

    if not active:
        return info

    if spot_info["facing_postflop_aggression"]:
        line_strength += 0.03
    if spot_info.get("opp_current_round_bet_count", 0) >= 2:
        line_strength += 0.08
    elif size_bucket in ("medium", "large"):
        line_strength += 0.04
    if round_idx >= 2:
        line_strength += 0.02

    info["active"] = True
    info["severe"] = (
        spot_info["facing_postflop_aggression"]
        and spot_info.get("opp_current_round_bet_count", 0) >= 2
        and size_bucket in ("medium", "large")
    )
    info["line_strength"] = clamp(line_strength, 0.0, 0.18)
    info["size_bucket"] = size_bucket
    return info


def postflop_call_margin(spot_info, opponent_model, made_strength, draw_strength, round_idx, has_position):
    if round_idx <= 0:
        return 0.0

    margin = 0.0
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    weak_showdown = made_strength < 0.22
    size_bucket = bet_size_bucket(spot_info["last_raise_pot_ratio"])

    if weak_showdown:
        margin += 0.012
    if air_hand:
        margin += 0.018

    if spot_info["facing_postflop_aggression"]:
        margin += 0.008
        if size_bucket == "small":
            margin += 0.020
        elif size_bucket == "medium":
            margin += 0.010
        else:
            margin += 0.024

        if spot_info.get("opp_postflop_bet_count", 0) >= 2:
            margin += 0.024 if size_bucket == "small" else 0.014
        if round_idx >= 2 and air_hand:
            margin += 0.010
        if round_idx == 3 and size_bucket == "large":
            margin += 0.020

    if not has_position:
        margin += 0.008

    confidence = opponent_model["confidence"]
    if air_hand:
        margin -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.50) * 0.015
    else:
        margin -= confidence * max(0.0, opponent_model["postflop_aggr"] - 0.50) * 0.008

    return clamp(margin, 0.0, 0.08)


def realized_postflop_equity(
    win_rate,
    made_strength,
    draw_strength,
    round_idx,
    has_position,
    spot_info,
    pair_profile=None,
):
    air_hand = made_strength < 0.18 and draw_strength < 0.08
    if round_idx <= 0:
        return win_rate

    eqr = 1.0

    if air_hand:
        eqr = 0.72 if has_position else 0.62

        if spot_info.get("opp_postflop_bet_count", 0) >= 2:
            eqr -= 0.10
        if round_idx == 2:
            eqr -= 0.05
        elif round_idx == 3:
            eqr -= 0.12

        eqr = clamp(eqr, 0.45, 0.85)
        return win_rate * eqr

    if pair_profile is not None and pair_profile["made_class"] == 1:
        pair_type = pair_profile["pair_type"]

        if pair_type in ("middle_pair", "bottom_pair", "underpair", "board_pair"):
            eqr = 0.86 if has_position else 0.78

            if pair_profile["weak_kicker"]:
                eqr -= 0.05
            if spot_info.get("opp_postflop_bet_count", 0) >= 2:
                eqr -= 0.06
            if round_idx == 3:
                eqr -= 0.06

            eqr = clamp(eqr, 0.65, 0.92)
            return win_rate * eqr

        if pair_type == "top_pair" and pair_profile["weak_kicker"]:
            eqr = 0.92 if has_position else 0.86
            if spot_info.get("opp_postflop_bet_count", 0) >= 2:
                eqr -= 0.04
            eqr = clamp(eqr, 0.75, 0.95)
            return win_rate * eqr

    return win_rate


def choose_raise(
    min_raise,
    my_chips,
    my_round_bet,
    to_call,
    pot,
    win_rate,
    round_idx,
    spot_name,
    preflop_strength,
    has_position,
    opponent_model,
    semi_bluff=False,
    value_profile=None,
    value_plan=None,
    board_texture=None,
    draw_info=None,
    blocker_bluff=False,
    probe_mode=False,
    pressure_line=False,
    induce_mode=False,
    nutted_risk_score=0.0,
    match_sizing_delta=0.0,
):
    if my_chips <= max(min_raise, to_call) + 1:
        return None

    pot_after_call = pot + to_call
    confidence = opponent_model["confidence"]
    fold_to_raise = opponent_model["fold_to_raise"]
    if value_profile is None:
        value_profile = {"tier": "none", "size_bonus": 0.0}
    if value_plan is None:
        value_plan = {"size_delta": 0.0, "induce": False, "protect": False, "thin_control": False}
    if board_texture is None:
        board_texture = {"wetness": 0.0, "dynamic": False}
    if draw_info is None:
        draw_info = empty_draw_profile()
    wetness = board_texture["wetness"]

    if round_idx == 0:
        ratio = 0.55 if to_call == 0 else 0.75
    elif round_idx == 1:
        ratio = 0.60
    elif round_idx == 2:
        ratio = 0.70
    else:
        ratio = 0.85

    ratio += max(0.0, win_rate - 0.55) * (0.90 + 0.20 * round_idx)
    ratio += -0.05 if has_position else 0.05
    ratio += confidence * max(0.0, fold_to_raise - 0.52) * (0.20 if semi_bluff else 0.10)
    ratio += value_profile.get("size_bonus", 0.0)
    ratio += value_plan.get("size_delta", 0.0)
    ratio += match_sizing_delta
    if round_idx > 0 and value_profile.get("tier") == "strong" and not semi_bluff and not pressure_line:
        if not board_texture["dynamic"]:
            ratio -= 0.05
        if wetness <= 0.20:
            ratio -= 0.02
    if board_texture["dynamic"]:
        if value_profile.get("tier") in ("strong", "nut"):
            ratio += 0.05 * wetness
        elif value_profile.get("tier") == "thin":
            ratio -= 0.04 * wetness
    if semi_bluff:
        ratio -= 0.08
        ratio += 0.02 * wetness
        ratio += draw_info.get("size_bonus", 0.0)
        if draw_info.get("type") == "gutshot":
            ratio -= 0.04
    if pressure_line:
        ratio += 0.05 + 0.04 * wetness
    if nutted_risk_score > 0.0 and value_profile.get("tier") != "nut":
        ratio -= min(0.10, nutted_risk_score * 0.55)
    if blocker_bluff:
        ratio = min(ratio, 0.54 + 0.18 * wetness + 0.08 * max(0, round_idx - 1))
        ratio += confidence * max(0.0, fold_to_raise - 0.58) * 0.22
    inducing_value = (induce_mode or value_plan.get("induce", False)) and to_call == 0 and value_profile.get("tier") == "nut"
    if inducing_value:
        induce_cap = 0.29 + 0.05 * round_idx + 0.05 * wetness
        ratio = min(ratio, induce_cap)
    if probe_mode:
        probe_ratio = 0.25 + 0.08 * wetness
        if value_profile.get("tier") == "thin":
            probe_ratio += 0.08
        if blocker_bluff and round_idx == 3:
            probe_ratio = max(probe_ratio, 0.34 + 0.08 * wetness)
        elif round_idx == 3:
            probe_ratio += 0.05
        ratio = min(ratio, probe_ratio)
    thin_cap = None
    if value_plan.get("thin_control", False) and value_profile.get("tier") != "nut":
        thin_cap = 0.30 if round_idx <= 2 else 0.38
        ratio = min(ratio, thin_cap)
    low_ratio = 0.28 if inducing_value else 0.22 if probe_mode or (blocker_bluff and to_call == 0) else 0.40
    if thin_cap is not None:
        low_ratio = min(low_ratio, thin_cap)
    ratio = clamp(ratio, low_ratio, 1.45)

    amount = int(to_call + pot_after_call * ratio)

    if round_idx == 0 and preflop_strength is not None:
        if spot_name == "sb_open":
            desired_total = int((2.5 + max(0.0, preflop_strength - 0.58) * 1.8) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)
        elif spot_name == "bb_vs_limp":
            desired_total = int((3.2 + max(0.0, preflop_strength - 0.60) * 1.8) * BIG_BLIND)
            amount = max(amount, desired_total - my_round_bet)

    amount = max(min_raise, amount)
    if semi_bluff and fold_to_raise < 0.45:
        amount = min(amount, max(min_raise, int(to_call + pot_after_call * 0.60)))
    if blocker_bluff:
        bluff_cap = max(min_raise, int(to_call + pot_after_call * (0.45 if round_idx == 3 and to_call == 0 else 0.56 + 0.16 * wetness)))
        amount = min(amount, bluff_cap)
    amount = min(amount, my_chips - 1)

    if amount <= to_call or amount < min_raise or amount >= my_chips:
        return None
    return amount


def choose_preflop_spot_action(req, state, spot_info, opponent_model, preflop_strength, win_rate, match_profile):
    my_chips = req["my_chips"]
    to_call = state["to_call"]
    match_adjust = match_risk_adjustment(req, req["my_id"], get_remaining_hands(req))
    confidence = opponent_model["confidence"]
    loose_bonus = confidence * max(0.0, opponent_model["vpip"] - 0.55) * 0.03
    trash_hand = is_preflop_trash_hand(req["my_cards"], preflop_strength)

    if spot_info["preflop_spot"] == "sb_open":
        open_threshold = 0.49 + match_adjust + 0.02 + match_profile["open_delta"]
        limp_threshold = 0.36 + match_adjust
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            state["pot"],
            max(win_rate, preflop_strength),
            0,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if not trash_hand and preflop_strength >= open_threshold and raise_amount is not None:
            return raise_amount
        if preflop_strength <= limp_threshold - loose_bonus:
            return -1
        return 0

    if spot_info["preflop_spot"] == "bb_vs_limp":
        iso_threshold = 0.57 + match_adjust - loose_bonus + match_profile["open_delta"]
        iso_threshold -= confidence * max(0.0, opponent_model["vpip"] - 0.58) * 0.08
        iso_threshold -= confidence * max(0.0, opponent_model["fold_to_raise"] - 0.52) * 0.05
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            state["pot"],
            max(win_rate, preflop_strength),
            0,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if not trash_hand and preflop_strength >= iso_threshold and raise_amount is not None:
            return raise_amount
        return 0

    return None


def get_action(req, requests):
    my_id = req["my_id"]
    my_chips = req["my_chips"]
    my_cards = req["my_cards"]
    public_cards = req["public_cards"]

    state = reconstruct_state(req)
    if should_lock_win(req, state, my_id):
        return -1

    opponent_model = build_opponent_model(requests, my_id)
    spot_info = analyze_current_spot(req, state)
    round_idx = state["round"]
    to_call = state["to_call"]
    pot = max(1, state["pot"])
    remaining_hands = get_remaining_hands(req)
    match_profile = match_pressure_profile(req, my_id, remaining_hands)
    anti_lock_pressure = fold_gives_opponent_lock(req, state, my_id)
    if anti_lock_pressure:
        match_profile = apply_anti_lock_pressure(match_profile)

    preflop_strength = estimate_preflop_strength(my_cards) if not public_cards else None
    preflop_3bet_candidate = is_preflop_3bet_candidate(my_cards) if preflop_strength is not None else False
    preflop_trash_hand = is_preflop_trash_hand(my_cards, preflop_strength) if preflop_strength is not None else False
    combos, weights = build_opponent_range(my_cards, public_cards, state, opponent_model, spot_info)

    simulations = SIMULATIONS_BY_PUBLIC_COUNT.get(len(public_cards), 700)

    win_rate = estimate_weighted_win_rate(my_cards, public_cards, combos, weights, simulations)

    critical_spot = to_call > 0 and (
        to_call / pot >= 0.25 or to_call >= BIG_BLIND * 4 or spot_info["facing_allin"]
    )
    extra = EXTRA_SIMULATIONS_BY_PUBLIC_COUNT.get(len(public_cards), 0)
    if critical_spot and extra > 0:
        refined = estimate_weighted_win_rate(my_cards, public_cards, combos, weights, extra)
        win_rate = (win_rate * simulations + refined * extra) / (simulations + extra)

    if round_idx == 0 and preflop_strength is not None:
        spot_action = choose_preflop_spot_action(
            req,
            state,
            spot_info,
            opponent_model,
            preflop_strength,
            win_rate,
            match_profile,
        )
        if spot_action is not None:
            if anti_lock_pressure and spot_action <= 0:
                if not preflop_trash_hand:
                    anti_lock_attack = choose_anti_lock_pressure_action(
                        state,
                        my_chips,
                        to_call,
                        pot,
                        round_idx,
                        win_rate,
                        opponent_model,
                        remaining_hands,
                        preflop_strength=preflop_strength,
                    )
                    if anti_lock_attack is not None:
                        return anti_lock_attack
                if spot_action == -1 and to_call < my_chips:
                    return 0
            return spot_action

    pot_odds = to_call / (pot + to_call) if to_call > 0 else 0.0
    made_strength = made_hand_metric(my_cards, public_cards) if len(public_cards) >= 3 else 0.0
    pair_profile = pair_board_profile(my_cards, public_cards) if len(public_cards) >= 3 else None
    board_texture = board_texture_profile(public_cards) if len(public_cards) >= 3 else None
    draw_info = draw_profile(my_cards, public_cards, board_texture) if len(public_cards) >= 3 else empty_draw_profile()
    draw_strength = draw_info["quality"]
    marginal_pair = marginal_pair_under_pressure(pair_profile, board_texture) if len(public_cards) >= 3 else False
    paired_board_profile = paired_board_outcome_profile(my_cards, public_cards) if len(public_cards) >= 3 else None
    value_profile = value_hand_tier(my_cards, public_cards, pair_profile, board_texture, paired_board_profile) if len(public_cards) >= 3 else None
    flush_profile = made_flush_profile(my_cards, public_cards, board_texture) if len(public_cards) >= 3 else None
    blocker_profile = blocker_bluff_profile(my_cards, public_cards, pair_profile, board_texture) if len(public_cards) >= 3 else None
    nutted_risk = (
        nutted_risk_profile(my_cards, public_cards, pair_profile, board_texture, value_profile, paired_board_profile)
        if len(public_cards) >= 3
        else {"risk": 0.0, "label": "none", "vulnerable": False}
    )
    value_plan = (
        value_bet_plan(value_profile, board_texture, paired_board_profile, pair_profile, nutted_risk, round_idx, pot)
        if len(public_cards) >= 3
        else {"size_delta": 0.0, "induce": False, "protect": False, "thin_control": False}
    )
    line_strength = aggressive_line_strength(spot_info, board_texture) if len(public_cards) >= 3 else 0.0
    check_resistance = check_probe_resistance_margin(spot_info, opponent_model, round_idx) if len(public_cards) >= 3 else 0.0
    paired_board_stackoff = (
        paired_board_stackoff_profile(pair_profile, paired_board_profile, board_texture, spot_info, round_idx)
        if len(public_cards) >= 3
        else {"active": False, "severe": False, "line_strength": 0.0, "size_bucket": "small"}
    )
    repeated_raise_trap = (
        round_idx > 0
        and spot_info["facing_postflop_aggression"]
        and spot_info.get("opp_current_round_bet_count", 0) >= 2
    )
    strong_flush_repressure_continue = (
        flush_profile is not None
        and (
            flush_profile["repressure_continue"]
            or flush_profile["nut_like"]
            or (
                board_texture is not None
                and not board_texture["paired"]
                and flush_profile["high_hole_rank"] >= 12
                and flush_profile["better_unseen_ranks"] <= 1
            )
        )
    )
    hard_repressure_fold = (
        repeated_raise_trap
        and not strong_flush_repressure_continue
        and (value_profile is None or value_profile["tier"] != "nut")
        and (
            (board_texture is not None and board_texture["paired"])
            or bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
        )
    )

    strong = 0.69 if round_idx == 0 else 0.65 if round_idx == 1 else 0.61 if round_idx == 2 else 0.59
    medium = 0.54 if round_idx == 0 else 0.50 if round_idx == 1 else 0.48

    if spot_info["has_position"]:
        strong -= 0.015
        medium -= 0.01
    else:
        strong += 0.02
        medium += 0.015

    if preflop_strength is not None:
        if preflop_strength >= 0.72:
            strong -= 0.03
            medium -= 0.02
        elif preflop_strength <= 0.40:
            strong += 0.04
            medium += 0.03

    match_adjust = match_risk_adjustment(req, my_id, remaining_hands)
    pressure_adjust = opponent_pressure_adjustment(opponent_model, spot_info, round_idx)
    strong += match_adjust + pressure_adjust + match_profile["threshold_delta"]
    medium += match_adjust + pressure_adjust * 0.8 + 0.75 * match_profile["threshold_delta"]
    strong += 0.30 * line_strength + 0.45 * paired_board_stackoff["line_strength"]
    medium += 0.18 * line_strength + 0.22 * paired_board_stackoff["line_strength"]
    strong += 0.30 * check_resistance
    medium += 0.20 * check_resistance
    if value_profile is not None:
        if value_profile["tier"] == "nut":
            strong -= 0.07
            medium -= 0.04
        elif value_profile["tier"] == "strong":
            strong -= 0.04
            medium -= 0.02
        elif value_profile["tier"] == "thin":
            medium -= 0.01
    strong += 0.45 * nutted_risk["risk"]
    medium += 0.30 * nutted_risk["risk"]

    if state["opponent_allin"]:
        jam_cost = max(state["allin_call_amount"], to_call)
        jam_odds = jam_cost / (pot + jam_cost) if jam_cost > 0 else 0.0
        jam_buffer = 0.02 + max(0.0, strong - 0.65) * 0.2
        if value_profile is not None and value_profile["tier"] == "thin":
            jam_buffer += 0.04
        jam_buffer += nutted_risk["risk"]
        jam_buffer += 0.04 * match_profile["protect"]
        jam_buffer += line_strength + paired_board_stackoff["line_strength"]
        jam_buffer += check_resistance
        if remaining_hands == 1:
            total_win_chips = req.get("total_win_chips", [0] * N_PLAYERS)
            if len(total_win_chips) > my_id and total_win_chips[my_id] < 0:
                jam_buffer -= 0.03
        if preflop_strength is not None and preflop_strength < 0.42:
            jam_buffer += 0.02
        if anti_lock_pressure:
            jam_buffer -= 0.10
        anti_lock_jam_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            jam_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_jam_continue:
                return -1
        jam_buffer = clamp(jam_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= jam_odds + jam_buffer or anti_lock_jam_continue else -1

    if to_call >= my_chips:
        shove_odds = my_chips / (pot + my_chips)
        shove_buffer = 0.01 + max(0.0, strong - 0.64) * 0.2
        if value_profile is not None and value_profile["tier"] == "thin":
            shove_buffer += 0.04
        shove_buffer += nutted_risk["risk"]
        shove_buffer += 0.04 * match_profile["protect"]
        shove_buffer += line_strength + paired_board_stackoff["line_strength"]
        shove_buffer += check_resistance
        if anti_lock_pressure:
            shove_buffer -= 0.10
        anti_lock_shove_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            shove_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_shove_continue:
                return -1
        shove_buffer = clamp(shove_buffer, -0.05 if anti_lock_pressure else 0.0, 0.14)
        return -2 if win_rate >= shove_odds + shove_buffer or anti_lock_shove_continue else -1

    if to_call > 0:
        if round_idx == 0:
            call_margin = 0.005 + (0.010 if not spot_info["has_position"] else 0.0)
            if preflop_strength is not None and preflop_strength <= 0.40:
                call_margin += 0.015
            realized_rate = win_rate
        else:
            call_margin = postflop_call_margin(
                spot_info,
                opponent_model,
                made_strength,
                draw_strength,
                round_idx,
                spot_info["has_position"],
            )
            call_margin += pair_domination_margin(
                pair_profile,
                spot_info,
                round_idx,
            )
            call_margin += draw_call_margin(
                draw_info,
                board_texture,
                round_idx,
                spot_info,
            )
            if (
                round_idx == 2
                and spot_info["facing_postflop_aggression"]
                and pair_profile is not None
                and pair_profile["made_class"] == 1
                and pair_profile["pair_type"] in ("middle_pair", "bottom_pair", "underpair")
            ):
                call_margin += 0.035
            call_margin += line_strength + paired_board_stackoff["line_strength"]
            call_margin += check_resistance
            call_margin += 0.50 * nutted_risk["risk"]
            if round_idx == 3 and made_strength < 0.40 and not (blocker_profile and blocker_profile["eligible"]):
                call_margin += 0.04
            if round_idx == 3 and paired_board_profile is not None and paired_board_profile["fold_to_raise"]:
                call_margin += 0.05
            realized_rate = realized_postflop_equity(
                win_rate,
                made_strength,
                draw_strength,
                round_idx,
                spot_info["has_position"],
                spot_info,
                pair_profile,
            )
        if anti_lock_pressure:
            call_margin -= 0.07
        anti_lock_call_continue = anti_lock_can_continue(
            anti_lock_pressure,
            win_rate,
            pot_odds,
            round_idx,
            value_profile,
            draw_info,
            made_strength,
        )
        strong_made_continue = must_continue_vs_raise(
            value_profile,
            made_strength,
            pot_odds,
            nutted_risk,
            board_texture,
        )
        anti_lock_attack = None
        if anti_lock_pressure:
            if round_idx > 0 or preflop_3bet_candidate:
                anti_lock_attack = choose_anti_lock_pressure_action(
                    state,
                    my_chips,
                    to_call,
                    pot,
                    round_idx,
                    win_rate,
                    opponent_model,
                    remaining_hands,
                    preflop_strength=preflop_strength,
                    value_profile=value_profile,
                    draw_info=draw_info,
                    blocker_profile=blocker_profile,
                    board_texture=board_texture,
                )
        fragile_river_raise_fold = (
            round_idx == 3
            and spot_info["facing_postflop_aggression"]
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
            and paired_board_profile is not None
            and paired_board_profile["fold_to_raise"]
            and paired_board_profile["hand_class"] == 2
            and (value_profile is None or value_profile["tier"] != "nut")
        )
        fragile_pair_raise_fold = (
            round_idx > 0
            and spot_info["facing_postflop_aggression"]
            and marginal_pair
            and draw_strength < 0.14
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) in ("medium", "large")
            and (value_profile is None or value_profile["tier"] not in ("strong", "nut"))
        )
        if anti_lock_attack is not None:
            return anti_lock_attack
        if fragile_river_raise_fold:
            if not anti_lock_call_continue:
                return -1
        if fragile_pair_raise_fold:
            if not anti_lock_call_continue:
                return -1
        if hard_repressure_fold or paired_board_stackoff["severe"]:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if realized_rate < pot_odds + call_margin:
            if not anti_lock_call_continue and not strong_made_continue:
                return -1
        if repeated_raise_trap and (value_profile is None or value_profile["tier"] != "nut"):
            return 0

        raise_fold_threshold = 0.56 - 0.30 * match_profile["bluff_delta"]
        blocker_raise_threshold = 0.55 - 0.32 * match_profile["bluff_delta"]
        draw_raise_threshold = clamp(raise_fold_threshold - draw_info["fold_threshold_delta"], 0.46, 0.68)
        draw_equity_slack = 0.05 if draw_info["type"] in ("combo_draw", "nut_flush_draw") else 0.03
        semi_bluff = (
            round_idx > 0
            and draw_info["semi_bluff"]
            and draw_strength >= 0.12
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > draw_raise_threshold
            and win_rate >= pot_odds - draw_equity_slack
        )
        blocker_raise = (
            round_idx == 1
            and spot_info["facing_postflop_aggression"]
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > blocker_raise_threshold
            and blocker_profile is not None
            and blocker_profile["eligible"]
            and made_strength < 0.18
            and draw_strength < 0.12
            and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx)
        )
        trap_nut_slowplay = (
            round_idx in (1, 2)
            and value_profile is not None
            and value_profile["tier"] == "nut"
            and board_texture is not None
            and not board_texture["dynamic"]
            and spot_info["facing_postflop_aggression"]
            and bet_size_bucket(spot_info["last_raise_pot_ratio"]) != "large"
            and pot < 1400
            and nutted_risk["risk"] <= 0.02
            and match_profile["chase"] <= 0.45
            and opponent_model["confidence"] >= 0.20
            and (
                opponent_model["postflop_aggr"] >= 0.38
                or opponent_model["aggression"] >= 0.34
                or opponent_model["fold_to_raise"] < 0.46
            )
        )
        flop_checkraise_exploit = (
            round_idx == 1
            and spot_info["facing_postflop_aggression"]
            and opponent_model["confidence"] >= 0.25
            and opponent_model["fold_to_raise"] > blocker_raise_threshold
            and (
                (value_profile and value_profile["tier"] in ("strong", "nut"))
                or (draw_info["semi_bluff"] and draw_strength >= 0.15)
                or blocker_raise
            )
        )

        if trap_nut_slowplay:
            return 0
        preflop_defensive_only = (
            round_idx == 0
            and to_call > 0
            and not preflop_3bet_candidate
        )
        if not preflop_defensive_only and (win_rate >= max(strong, pot_odds + 0.12) or semi_bluff or flop_checkraise_exploit):
            raise_amount = choose_raise(
                state["min_raise_action"],
                my_chips,
                state["my_round_bet"],
                to_call,
                pot,
                win_rate,
                round_idx,
                spot_info["preflop_spot"],
                preflop_strength,
                spot_info["has_position"],
                opponent_model,
                semi_bluff=semi_bluff or (flop_checkraise_exploit and draw_info["semi_bluff"] and draw_strength >= 0.15),
                value_profile=value_profile,
                value_plan=value_plan,
                board_texture=board_texture,
                draw_info=draw_info,
                blocker_bluff=blocker_raise,
                pressure_line=flop_checkraise_exploit,
                nutted_risk_score=nutted_risk["risk"],
                match_sizing_delta=match_profile["sizing_delta"],
            )
            if raise_amount is not None and raise_amount > to_call:
                return raise_amount
        return 0

    weak_pair_river = (
        round_idx == 3
        and pair_profile is not None
        and pair_profile["made_class"] == 1
        and pair_profile["pair_type"] in ("middle_pair", "bottom_pair", "underpair", "board_pair")
    )
    opp_double_barrel_then_river_check = (
        round_idx == 3
        and to_call == 0
        and spot_info.get("opp_postflop_bet_count", 0) >= 2
        and spot_info["last_opp_action_type"] == "check"
    )
    bad_river_bluff_candidate = (
        round_idx == 3
        and to_call == 0
        and made_strength >= 0.18
        and made_strength < 0.40
        and not (blocker_profile and blocker_profile["eligible"])
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
    )
    weak_bottom_pair_barrel = (
        round_idx >= 2
        and to_call == 0
        and pair_profile is not None
        and pair_profile["made_class"] == 1
        and pair_profile["pair_type"] in ("bottom_pair", "underpair", "board_pair")
        and made_strength < 0.40
        and draw_strength < 0.12
    )
    weak_pair_after_raise_barrel = (
        round_idx >= 2
        and to_call == 0
        and marginal_pair
        and draw_strength < 0.14
        and (value_profile is None or value_profile["tier"] not in ("strong", "nut"))
        and (
            spot_info.get("opp_previous_round_raise_count", 0) > 0
            or spot_info.get("opp_prior_postflop_raise_count", 0) > 0
        )
    )
    bad_river_value_bet = (
        round_idx == 3
        and to_call == 0
        and paired_board_profile is not None
        and paired_board_profile["board_paired"]
        and paired_board_profile["prefer_check"]
        and paired_board_profile["hand_class"] == 2
        and nutted_risk["risk"] >= 0.05
        and (value_profile is None or value_profile["tier"] != "nut")
    )
    bad_stackoff_overpair = (
        round_idx > 0
        and to_call == 0
        and paired_board_stackoff["active"]
        and pot > 3000
        and (value_profile is None or value_profile["tier"] != "nut")
    )
    big_pot_threshold = int(clamp(1500 - 350 * match_profile["protect"] + 250 * match_profile["chase"], 1100, 1800))
    big_pot = pot >= big_pot_threshold
    induce_nut_value = (
        round_idx > 0
        and to_call == 0
        and value_profile is not None
        and value_profile["tier"] == "nut"
        and board_texture is not None
        and not board_texture["dynamic"]
        and not big_pot
        and match_profile["chase"] <= 0.55
        and opponent_model["confidence"] >= 0.20
        and (
            opponent_model["postflop_aggr"] >= 0.38
            or opponent_model["aggression"] >= 0.34
            or opponent_model["fold_to_raise"] < 0.46
        )
    )
    anti_lock_attack = None
    if anti_lock_pressure:
        anti_lock_attack = choose_anti_lock_pressure_action(
            state,
            my_chips,
            to_call,
            pot,
            round_idx,
            win_rate,
            opponent_model,
            remaining_hands,
            preflop_strength=preflop_strength,
            value_profile=value_profile,
            draw_info=draw_info,
            blocker_profile=blocker_profile,
            board_texture=board_texture,
        )
        if anti_lock_attack is not None:
            return anti_lock_attack

    if opp_double_barrel_then_river_check and weak_pair_river:
        return 0
    if bad_river_bluff_candidate:
        return 0
    if weak_bottom_pair_barrel:
        return 0
    if weak_pair_after_raise_barrel:
        return 0
    if bad_river_value_bet:
        return 0
    if bad_stackoff_overpair:
        return 0
    if big_pot and round_idx == 3 and (value_profile is None or value_profile["tier"] not in ("strong", "nut")):
        if blocker_profile is None or not blocker_profile["eligible"]:
            return 0
    thin_static_showdown_control = (
        round_idx >= 2
        and value_profile is not None
        and value_profile["tier"] == "thin"
        and board_texture is not None
        and not board_texture["dynamic"]
        and draw_strength < 0.12
        and not anti_lock_pressure
    )
    if thin_static_showdown_control:
        return 0

    river_bluff_threshold = 0.62 - 0.28 * match_profile["bluff_delta"]
    probe_fold_threshold = 0.56 - 0.32 * match_profile["bluff_delta"]
    semi_bluff_threshold = 0.58 - 0.28 * match_profile["bluff_delta"]
    draw_bet_threshold = clamp(semi_bluff_threshold - draw_info["fold_threshold_delta"], 0.46, 0.70)
    check_probe_signal = (
        spot_info["last_opp_action_type"] == "check"
        and (
            spot_info.get("opp_postflop_check_count", 0) >= 2
            or (
                opponent_model["confidence"] >= 0.20
                and opponent_model.get("postflop_check_rate", 0.42) >= 0.52
            )
        )
    )
    river_blocker_bluff = (
        round_idx == 3
        and made_strength < 0.16
        and draw_strength < 0.08
        and opponent_model["confidence"] >= 0.35
        and opponent_model["fold_to_raise"] > river_bluff_threshold
        and blocker_profile is not None
        and blocker_profile["eligible"]
        and allow_low_frequency_blocker_bluff(req, my_cards, public_cards, blocker_profile, round_idx)
    )
    small_probe = (
        round_idx > 0
        and opponent_model["confidence"] >= 0.25
        and opponent_model["fold_to_raise"] > probe_fold_threshold
        and made_strength < 0.62
        and draw_strength < 0.16
        and board_texture is not None
        and board_texture["wetness"] <= 0.32
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
    )
    check_probe = (
        round_idx > 0
        and check_probe_signal
        and board_texture is not None
        and board_texture["wetness"] <= 0.55
        and made_strength < 0.58
        and draw_strength < 0.20
        and not (value_profile and value_profile["tier"] in ("strong", "nut"))
        and not (round_idx == 3 and made_strength >= 0.18 and not (blocker_profile and blocker_profile["eligible"]))
    )
    blocker_bluff = (
        river_blocker_bluff
    )
    semi_bluff = (
        round_idx > 0
        and draw_info["semi_bluff"]
        and draw_strength >= 0.12
        and opponent_model["confidence"] >= 0.25
        and opponent_model["fold_to_raise"] > draw_bet_threshold
    )
    if win_rate >= medium or semi_bluff or blocker_bluff or small_probe or check_probe or made_strength >= 0.62 or (value_profile and value_profile["tier"] in ("strong", "nut")):
        raise_amount = choose_raise(
            state["min_raise_action"],
            my_chips,
            state["my_round_bet"],
            to_call,
            pot,
            win_rate,
            round_idx,
            spot_info["preflop_spot"],
            preflop_strength,
            spot_info["has_position"],
            opponent_model,
            semi_bluff=semi_bluff and win_rate < medium,
            value_profile=value_profile,
            value_plan=value_plan,
            board_texture=board_texture,
            draw_info=draw_info,
            blocker_bluff=blocker_bluff and win_rate < medium and not semi_bluff,
            probe_mode=check_probe or small_probe or (value_profile and value_profile["tier"] == "thin" and board_texture and not board_texture["dynamic"]),
            induce_mode=induce_nut_value or value_plan.get("induce", False),
            nutted_risk_score=nutted_risk["risk"],
            match_sizing_delta=match_profile["sizing_delta"],
        )
        if raise_amount is not None:
            return raise_amount
    return 0


def sanitize_action(action, state, my_chips):
    if state["opponent_allin"]:
        return action if action in (-1, -2) else -1

    if state["to_call"] >= my_chips:
        return -2 if action == -2 else -1

    if action > 0:
        min_raise = state.get("min_raise_action", state["round_raise"])
        if action >= my_chips:
            return -2
        if action < min_raise or action <= state["to_call"]:
            return 0

    if action == 0 and state["to_call"] > 0:
        return 0

    return action


def main():
    payload = json.loads(input())
    requests = payload["requests"]
    req = dict(requests[-1])
    if "remaining_hands" not in req:
        req["remaining_hands"] = infer_remaining_hands_from_requests(requests)
    action = get_action(req, requests)
    state = reconstruct_state(req)
    action = sanitize_action(action, state, req["my_chips"])
    print(json.dumps({"response": int(action)}))


if __name__ == "__main__":
    main()