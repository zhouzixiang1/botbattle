#!/usr/bin/env python3
"""Botzone compact-JSON wrapper around national_v156 baseline policy.

Reads one JSON act-request per stdin line; writes one compact response.
Uses get_baseline_decision only (sub-second, no long MC refinement).
"""
from __future__ import annotations

import json
import sys
import time

import policy

# Platform: card = rank*4 + judge_suit; judge map internal→{0:2,1:0,2:1,3:3}
_JUDGE_TO_SUIT = {2: 0, 0: 1, 1: 2, 3: 3}

CODE_FOLD, CODE_CHECK, CODE_CALL, CODE_RAISE, CODE_ALLIN = 0, 1, 2, 3, 4


def _plat_to_national(card_int: int) -> dict:
    rank, js = divmod(int(card_int), 4)
    suit = _JUDGE_TO_SUIT[js]
    return {"suit": suit, "rank": rank}


def _street_name(n_board: int) -> str:
    return {0: "preflop", 3: "flop", 4: "turn", 5: "river"}.get(n_board, "preflop")


def _replay(req: dict) -> dict:
    """Rebuild pot / street bets / min-max raise from blinds + hist."""
    sb = int(req.get("sb") or 50)
    bb = int(req.get("bb") or 100)
    sb_idx = int(req.get("d") or 0)
    bb_idx = 1 - sb_idx
    my_id = int(req.get("id") or 0)
    hist = req.get("hist") or []
    board = req.get("pc") or []
    target_street = {0: 0, 3: 1, 4: 2, 5: 3}.get(len(board), 0)

    pot = 0
    street_bet = [0, 0]
    street = 0
    current_bet = 0
    min_raise_to = bb
    last_raise_to = 0

    def post_blinds() -> None:
        nonlocal pot, current_bet, min_raise_to, last_raise_to
        street_bet[sb_idx] = sb
        street_bet[bb_idx] = bb
        pot = sb + bb
        current_bet = bb
        last_raise_to = bb
        min_raise_to = 2 * bb

    def start_postflop() -> None:
        nonlocal current_bet, min_raise_to, last_raise_to
        street_bet[0] = street_bet[1] = 0
        current_bet = 0
        last_raise_to = 0
        min_raise_to = bb

    post_blinds()

    # Replay whole hist; advance street when betting round closes.
    i = 0
    while i < len(hist) and street <= target_street:
        # At start of each street after preflop, reset bets (already posted blinds for 0)
        pid, code, amount = hist[i]
        pid = int(pid)
        code = int(code)
        amount = int(amount)

        if code == CODE_FOLD:
            i += 1
            continue
        if code == CODE_CHECK:
            i += 1
        elif code == CODE_CALL:
            pay = amount
            street_bet[pid] += pay
            pot += pay
            i += 1
        elif code in (CODE_RAISE, CODE_ALLIN):
            raise_to = amount
            pay = max(0, raise_to - street_bet[pid])
            street_bet[pid] = raise_to
            pot += pay
            if raise_to > current_bet:
                prev = current_bet
                current_bet = raise_to
                if prev == 0 or raise_to >= (2 * last_raise_to if last_raise_to else bb):
                    last_raise_to = raise_to
                    min_raise_to = 2 * raise_to
            i += 1
        else:
            i += 1

        # Detect end of betting round → next street (only if more hist or we need to reach target)
        matched = street_bet[0] == street_bet[1]
        # Crude: if matched and both acted since last aggression, and next street needed
        # Use board length as authority: after consuming all hist for earlier streets,
        # if street < target and matched, advance.
        if matched and street < target_street:
            # Peek: if remaining hist empty, just advance to target
            # Else: if both players have had chance (hist continues), advance when
            # we see the round is "done" — both street_bets equal and not opening.
            # Advance one street per matched completion when target requires it.
            # Safer: count completions by advancing whenever matched after ≥1 action
            # and street < target — but only once per street after first matching close.
            pass

    # Simpler second pass: only care about CURRENT street state using chips + to
    to_call = int(req.get("to") or 0)
    my_chips = int(req.get("c") or 0)
    opp_chips = int(req.get("o") or 0)

    # Recompute current-street bets from end of hist for current street only.
    pot2, sbets, cur, min_to, last_to = _current_street_state(req)
    hero_street = sbets[my_id]
    opp_street = sbets[1 - my_id]
    if to_call != max(0, opp_street - hero_street):
        # trust to_call; adjust opp
        opp_street = hero_street + to_call
    max_to = hero_street + my_chips
    if cur == 0:
        min_to = max(bb, min_to)
    else:
        min_to = max(min_to, 2 * cur if cur else bb)

    return {
        "pot": max(1, pot2),
        "hero_street": hero_street,
        "opp_street": opp_street,
        "to_call": to_call,
        "current_bet": cur,
        "min_raise_to": min_to,
        "max_raise_to": max_to,
        "my_chips": my_chips,
        "opp_chips": opp_chips,
        "sb": sb,
        "bb": bb,
        "sb_idx": sb_idx,
        "my_id": my_id,
        "street": _street_name(len(board)),
        "street_index": target_street,
    }


def _current_street_state(req: dict) -> tuple[int, list[int], int, int, int]:
    """Return pot, street_bet[2], current_bet, min_raise_to, last_raise_to for current street."""
    sb = int(req.get("sb") or 50)
    bb = int(req.get("bb") or 100)
    sb_idx = int(req.get("d") or 0)
    bb_idx = 1 - sb_idx
    hist = [list(map(int, x)) for x in (req.get("hist") or [])]
    n_board = len(req.get("pc") or [])
    target = {0: 0, 3: 1, 4: 2, 5: 3}.get(n_board, 0)

    pot = 0
    street = 0
    street_bet = [0, 0]
    current_bet = 0
    last_raise_to = 0
    min_raise_to = bb

    def begin_preflop() -> None:
        nonlocal pot, current_bet, last_raise_to, min_raise_to
        street_bet[0] = street_bet[1] = 0
        street_bet[sb_idx] = sb
        street_bet[bb_idx] = bb
        pot = sb + bb
        current_bet = bb
        last_raise_to = bb
        min_raise_to = 2 * bb

    def begin_post() -> None:
        nonlocal current_bet, last_raise_to, min_raise_to
        street_bet[0] = street_bet[1] = 0
        current_bet = 0
        last_raise_to = 0
        min_raise_to = bb

    begin_preflop()
    pending = {0, 1}
    aggressor = None

    def close_round() -> bool:
        return street_bet[0] == street_bet[1] and not pending

    for pid, code, amount in hist:
        if code == CODE_FOLD:
            pending.clear()
            continue
        if code == CODE_CHECK:
            pending.discard(pid)
        elif code == CODE_CALL:
            pay = amount
            street_bet[pid] += pay
            pot += pay
            pending.discard(pid)
        elif code in (CODE_RAISE, CODE_ALLIN):
            raise_to = amount
            pay = max(0, raise_to - street_bet[pid])
            street_bet[pid] += pay
            # normalize to raise_to
            street_bet[pid] = max(street_bet[pid], raise_to)
            pot += pay
            if raise_to > current_bet:
                prev = current_bet
                current_bet = raise_to
                if prev == 0:
                    last_raise_to = raise_to
                    min_raise_to = max(2 * raise_to, 2 * bb)
                elif raise_to >= min_raise_to:
                    last_raise_to = raise_to
                    min_raise_to = 2 * raise_to
                aggressor = pid
                pending = {1 - pid}
            else:
                pending.discard(pid)
        if close_round() and street < target:
            street += 1
            begin_post()
            pending = {0, 1}
            aggressor = None

    # If we still need to be on target street (hist ended mid-round), stay.
    while street < target:
        street += 1
        begin_post()

    return pot, list(street_bet), current_bet, min_raise_to, last_raise_to


def _build_context(req: dict, st: dict) -> dict:
    hole = [_plat_to_national(c) for c in (req.get("mc") or [])]
    board = [_plat_to_national(c) for c in (req.get("pc") or [])]
    to_call = st["to_call"]
    my_chips = st["my_chips"]
    opp_chips = st["opp_chips"]
    pot = st["pot"]
    eff = min(my_chips, opp_chips)
    hand_num = int(req.get("h") or 0) + 1
    total = int(req.get("H") or 70)
    remaining = max(1, total - hand_num + 1)
    is_sb = st["my_id"] == st["sb_idx"]
    street = st["street"]
    street_index = st["street_index"]

    kinds = {"fold"}
    if to_call == 0:
        kinds.add("pass")
    else:
        kinds.add("pass")  # pass = call in national ABI when facing bet? actually call is "pass" when to_call>0 via pass_wire
    # national: call/check both map to "pass"
    kinds.add("pass")
    if my_chips > to_call and st["max_raise_to"] > st["current_bet"]:
        kinds.add("raise")
    if my_chips > 0:
        kinds.add("allin")
    if to_call > 0:
        kinds.add("fold")

    min_r = st["min_raise_to"]
    max_r = st["max_raise_to"]
    if "raise" in kinds and min_r > max_r:
        kinds.discard("raise")

    return {
        "schema_version": 1,
        "runtime_version": 10,
        "decision_id": hand_num * 10 + street_index,
        "cards": {
            "encoding": "national_tcp_suit_rank_v1",
            "hole": hole,
            "board": board,
        },
        "hand": {
            "number": hand_num,
            "total_hands": total,
            "remaining_including_current": remaining,
            "street": street,
            "street_index": street_index,
            "position": "small_blind" if is_sb else "big_blind",
            "acts_first_postflop": not is_sb,
            "match_control": {
                "schema_version": 1,
                "initial_chips": 20000,
                "small_blind": st["sb"],
                "big_blind": st["bb"],
                "remaining_including_current": remaining,
                "future_forced_blinds": 0,
                "current_exposure": max(0, 20000 - my_chips),
                "forced_fold_loss_bound": max(0, 20000 - my_chips),
                "hero_net_earned": 0,
                "fold_locks_match_win": False,
            },
        },
        "betting": {
            "pot": pot,
            "hero_stack": my_chips,
            "opponent_stack": opp_chips,
            "effective_stack": eff,
            "hero_street_bet": st["hero_street"],
            "opponent_street_bet": st["opp_street"],
            "to_call": to_call,
            "spr": round(eff / max(1.0, float(pot)), 6),
            "pot_odds": round(to_call / max(1.0, float(pot + to_call)), 6),
            "call_closes_allin_runout": bool(
                to_call > 0 and (opp_chips == 0 or my_chips <= to_call)
            ),
        },
        "history": {"schema_version": 1, "streets": []},
        "line": {
            "schema_version": 1,
            "street": street,
            "street_index": street_index,
            "position": "small_blind" if is_sb else "big_blind",
            "hero_in_position_postflop": is_sb,
            "preflop_aggressor": None,
            "preflop_spot": "bb_defend" if not is_sb else "sb_open",
            "street_open": to_call == 0 and st["current_bet"] == 0,
            "responding_to_check": to_call == 0 and st["current_bet"] == 0,
            "can_donk": False,
            "can_delayed_probe": False,
            "line_tags": [],
            "current_street": {"actions": (), "opponent_checked_back": False, "checked_through": False},
            "previous_street": None,
        },
        "legal": {
            "schema_version": 1,
            "policy_kinds": tuple(sorted(kinds)),
            "pass_wire_kind": "check" if to_call == 0 else "call",
            "min_raise_to": min_r if "raise" in kinds else None,
            "max_raise_to": max_r if "raise" in kinds else None,
            "raise_boundary": "inclusive_exact_2x_raise_to",
        },
        "opponent": {
            "rates": {},
            "terminal_response": {},
            "showdown_range": {},
            "adaptation_weight": 0.0,
            "raw_street_actions": {},
        },
        "deadline": {
            "clock": "time.monotonic",
            "hard_monotonic": time.monotonic() + 50.0,
            "refinement_monotonic": time.monotonic() + 0.2,
            "hard_budget_ms": 50000,
            "baseline_target_ms": 250,
            "refinement_budget_ms": 200,
        },
    }


def _to_response(decision: dict, st: dict) -> dict:
    kind = decision.get("kind")
    to_call = st["to_call"]
    if kind == "fold":
        return {"a": "f"}
    if kind == "allin":
        return {"a": "all"}
    if kind == "raise":
        x = decision.get("raise_to")
        if isinstance(x, int) and x > 0:
            if x >= st["max_raise_to"]:
                return {"a": "all"}
            return {"a": "r", "x": int(x)}
        return {"a": "c" if to_call > 0 else "k"}
    # pass
    if to_call > 0:
        return {"a": "c"}
    return {"a": "k"}


def decide_line(line: str) -> str:
    req = json.loads(line)
    if req.get("t") and req.get("t") != "act":
        return json.dumps({"a": "k"}, separators=(",", ":"))
    st = _replay(req)
    # Prefer chip-derived to_call
    st["to_call"] = int(req.get("to") or 0)
    st["my_chips"] = int(req.get("c") or 0)
    st["opp_chips"] = int(req.get("o") or 0)
    ctx = _build_context(req, st)
    try:
        raw = policy.get_baseline_decision(ctx)
    except Exception:
        raw = {"kind": "fold"} if st["to_call"] > 0 else {"kind": "pass"}
    if not isinstance(raw, dict):
        raw = {"kind": "pass"}
    # Legalize raise_to
    if raw.get("kind") == "raise":
        x = raw.get("raise_to")
        kinds = set(ctx["legal"]["policy_kinds"])
        min_r = ctx["legal"]["min_raise_to"]
        max_r = ctx["legal"]["max_raise_to"]
        if "raise" not in kinds or not isinstance(x, int):
            raw = {"kind": "pass"}
        elif max_r is not None and x >= max_r:
            raw = {"kind": "allin"}
        elif min_r is not None and x < min_r:
            raw = {"kind": "pass"}
    resp = _to_response(raw, st)
    return json.dumps(resp, separators=(",", ":"))


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            out = decide_line(line)
        except Exception:
            out = '{"a":"f"}'
        sys.stdout.write(out + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
