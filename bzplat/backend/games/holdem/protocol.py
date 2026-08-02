"""紧凑 JSON 对局协议（stdin/stdout 一行一条）。"""

from __future__ import annotations

import json
from typing import Any, Sequence

from bzplat.backend.games.holdem.cards import Card

PROTOCOL_VERSION = 1

# hist action codes
CODE_FOLD = 0
CODE_CHECK = 1
CODE_CALL = 2
CODE_RAISE = 3
CODE_ALLIN = 4

ACTION_TO_CODE = {
    "fold": CODE_FOLD,
    "check": CODE_CHECK,
    "call": CODE_CALL,
    "raise": CODE_RAISE,
    "allin": CODE_ALLIN,
}
CODE_TO_ACTION = {v: k for k, v in ACTION_TO_CODE.items()}

# response short codes a ∈ f|c|k|r|all
A_TO_ACTION = {
    "f": "fold",
    "c": "call",
    "k": "check",
    "r": "raise",
    "all": "allin",
}
ACTION_TO_A = {v: k for k, v in A_TO_ACTION.items()}
RESP_TO_ACTION = A_TO_ACTION  # alias

# internal suit 0=♠ 1=♥ 2=♦ 3=♣ → judge suit
_SUIT_TO_JUDGE = {0: 2, 1: 0, 2: 1, 3: 3}
_JUDGE_TO_SUIT = {v: k for k, v in _SUIT_TO_JUDGE.items()}


def encode_card(card: Card) -> int:
    """card_int = rank*4 + judge_suit; judge map {0:2,1:0,2:1,3:3}."""
    return int(card.rank) * 4 + _SUIT_TO_JUDGE[int(card.suit)]


def decode_card(n: int) -> Card:
    if not isinstance(n, int) or isinstance(n, bool) or n < 0 or n > 51:
        raise ValueError(f"invalid card_int: {n}")
    rank, js = divmod(n, 4)
    return Card(rank, _JUDGE_TO_SUIT[js])


def encode_cards(cards: Sequence[Card]) -> list[int]:
    return [encode_card(c) for c in cards]


def decode_cards(ints: Sequence[int]) -> list[Card]:
    return [decode_card(i) for i in ints]


def build_act_request(
    *,
    hand: int,
    total_hands: int,
    my_id: int,
    dealer_or_sb: int,
    my_cards: Sequence[Card],
    board: Sequence[Card],
    hist: list[list[int]],
    my_chips: int,
    opp_chips: int,
    sb: int,
    bb: int,
    to_call: int,
) -> dict[str, Any]:
    """Build compact act request. `hand` is 0-based (emitted as-is in `h`)."""
    return {
        "v": PROTOCOL_VERSION,
        "t": "act",
        "h": int(hand),
        "H": int(total_hands),
        "id": int(my_id),
        "d": int(dealer_or_sb),
        "mc": encode_cards(my_cards),
        "pc": encode_cards(board),
        "hist": hist,
        "c": int(my_chips),
        "o": int(opp_chips),
        "sb": int(sb),
        "bb": int(bb),
        "to": int(to_call),
    }


def parse_response(raw: dict[str, Any] | str | None) -> tuple[str, int | None]:
    """Parse bot response → (action_name, optional raise_to)."""
    if isinstance(raw, str):
        raw = json.loads(raw)
    if not isinstance(raw, dict):
        raise ValueError("response must be a dict")
    a = raw.get("a")
    if not isinstance(a, str):
        raise ValueError("missing or invalid a")
    a = a.strip().lower()
    if a in A_TO_ACTION:
        action = A_TO_ACTION[a]
    elif a in ACTION_TO_CODE:
        action = a
    else:
        raise ValueError(f"unknown action code: {a}")
    x = raw.get("x")
    if x is None:
        return action, None
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise ValueError("invalid x")
    xi = int(x)
    if xi < 0:
        raise ValueError("negative x")
    return action, xi


def build_response(action: str, raise_to: int | None = None) -> dict[str, Any]:
    """Build bot response dict from action name."""
    if action not in ACTION_TO_A:
        raise ValueError(f"unknown action: {action}")
    out: dict[str, Any] = {"a": ACTION_TO_A[action]}
    if raise_to is not None:
        out["x"] = int(raise_to)
    return out


def dumps_request(req: dict[str, Any]) -> str:
    return json.dumps(req, separators=(",", ":"), ensure_ascii=False)


def loads_response(line: str) -> dict[str, Any]:
    return json.loads(line)
