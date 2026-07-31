"""Unit tests for compact JSON protocol."""

from __future__ import annotations

from bzplat.backend.engine.cards import Card
from bzplat.backend.protocol.json_protocol import (
    A_TO_ACTION,
    ACTION_TO_A,
    PROTOCOL_VERSION,
    build_act_request,
    build_response,
    decode_card,
    decode_cards,
    encode_card,
    encode_cards,
    parse_response,
)


def test_encode_decode_card_roundtrip():
    for rank in range(13):
        for suit in range(4):
            c = Card(rank, suit)
            n = encode_card(c)
            assert 0 <= n <= 51
            assert decode_card(n) == c


def test_suit_judge_mapping():
    # suit map internal→judge: {0:2,1:0,2:1,3:3}
    # card_int = rank*4 + judge
    assert encode_card(Card(0, 0)) == 0 * 4 + 2  # 2♠ → judge suit 2
    assert encode_card(Card(0, 1)) == 0 * 4 + 0  # 2♥ → 0
    assert encode_card(Card(0, 2)) == 0 * 4 + 1  # 2♦ → 1
    assert encode_card(Card(0, 3)) == 0 * 4 + 3  # 2♣ → 3
    assert encode_card(Card(12, 1)) == 12 * 4 + 0  # A♥


def test_encode_cards_list():
    cards = [Card(12, 0), Card(0, 3)]
    ints = encode_cards(cards)
    assert decode_cards(ints) == cards


def test_build_and_parse_act():
    req = build_act_request(
        hand=12,
        total_hands=70,
        my_id=0,
        dealer_or_sb=0,
        my_cards=[Card(12, 0), Card(12, 3)],
        board=[Card(3, 0), Card(6, 1), Card(9, 2)],
        hist=[[0, 2, 50], [1, 3, 200]],
        my_chips=19900,
        opp_chips=19800,
        sb=50,
        bb=100,
        to_call=100,
    )
    assert req["v"] == PROTOCOL_VERSION
    assert req["t"] == "act"
    assert req["h"] == 12
    assert req["H"] == 70
    assert req["id"] == 0
    assert req["d"] == 0
    assert len(req["mc"]) == 2
    assert len(req["pc"]) == 3
    assert req["to"] == 100
    assert req["c"] == 19900
    assert req["sb"] == 50


def test_parse_response_codes():
    assert parse_response({"a": "f"}) == ("fold", None)
    assert parse_response({"a": "c"}) == ("call", None)
    assert parse_response({"a": "k"}) == ("check", None)
    assert parse_response({"a": "r", "x": 400}) == ("raise", 400)
    assert parse_response({"a": "all"}) == ("allin", None)
    assert parse_response({"a": "R", "x": 200}) == ("raise", 200)


def test_build_response_roundtrip():
    for name, code in ACTION_TO_A.items():
        if name == "raise":
            raw = build_response(name, 400)
            assert raw == {"a": "r", "x": 400}
            assert parse_response(raw) == ("raise", 400)
        else:
            raw = build_response(name)
            assert raw["a"] == code
            assert parse_response(raw)[0] == name
            assert A_TO_ACTION[code] == name
