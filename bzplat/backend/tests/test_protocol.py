"""Unit tests for Botzone 标准德州扑克协议（裸整数 response + raise delta + 牌 0-51）。"""

from __future__ import annotations

import pytest

from bzplat.backend.games.holdem.holdem_judge import Card, Suit
from bzplat.backend.games.holdem.protocol import (
    PROTOCOL_VERSION,
    RESP_ALLIN,
    RESP_CALL_CHECK,
    RESP_FOLD,
    action_to_history_int,
    build_act_request,
    decode_card,
    decode_cards,
    encode_card,
    encode_cards,
    parse_response,
)


def test_encode_decode_card_roundtrip():
    for number in range(2, 15):
        for suit in Suit:
            c = Card(suit, number)
            n = encode_card(c)
            assert 0 <= n <= 51
            assert decode_card(n) == c


def test_suit_botzone_mapping():
    # 裁判 Card 花色编码（Suit: 0♥ 1♦ 2♠ 3♣）== Botzone 线协议花色编码。
    # 2♠ → (2-2)*4+2 = 2（%4==2 → ♠ ✓）
    assert encode_card(Card(Suit.SPADE, 2)) == 0 * 4 + 2  # 2♠
    # 2♥ → (2-2)*4+0 = 0（%4==0 → ♥ ✓）
    assert encode_card(Card(Suit.HEART, 2)) == 0 * 4 + 0  # 2♥
    # 2♦ → (2-2)*4+1 = 1（%4==1 → ♦ ✓）
    assert encode_card(Card(Suit.DIAMOND, 2)) == 0 * 4 + 1  # 2♦
    # 2♣ → (2-2)*4+3 = 3（%4==3 → ♣ ✓）
    assert encode_card(Card(Suit.CLUB, 2)) == 0 * 4 + 3  # 2♣
    # A♥ number14 suitHEART → (14-2)*4+0 = 48
    assert encode_card(Card(Suit.HEART, 14)) == (14 - 2) * 4 + 0


def test_card_rank_formula():
    # Botzone 整数 → 裁判 Card。number = card//4 + 2（poker 点数 2..14）。
    assert decode_card(0).number == 2        # 2
    assert decode_card(48).number == 14      # A
    assert decode_card(51).number == 14      # A
    # 验证 Botzone poker 点数公式：card//4 + 2
    assert 0 // 4 + 2 == 2     # '2'
    assert 48 // 4 + 2 == 14   # 'A'


def test_encode_cards_list():
    cards = [Card(Suit.SPADE, 14), Card(Suit.CLUB, 2)]
    ints = encode_cards(cards)
    assert decode_cards(ints) == cards


def test_build_act_request_botzone_fields():
    """build_act_request 输出必须严格对齐 Botzone TexasHoldem2p 官方 11 字段——
    不多发任何平台扩展字段（to_call/sb/bb/opp_chips/... 已移除），保证标准 Botzone
    Bot 直接可跑。本测试是「防扩展字段回归」的守护测试。
    """
    req = build_act_request(
        hand=12,
        total_hands=70,
        my_id=0,
        dealer_id=0,
        my_cards=[Card(Suit.SPADE, 14), Card(Suit.CLUB, 14)],
        board=[Card(Suit.SPADE, 5), Card(Suit.HEART, 8), Card(Suit.DIAMOND, 11)],
        history=[
            {"round": 0, "player_id": 0, "action": 50, "action_type": "raise"},
            {"round": 0, "player_id": 1, "action": -1, "action_type": "fold"},
        ],
        my_chips=19900,
    )
    # Botzone TexasHoldem2p 官方字段（恰好 11 个，一个不多一个不少）
    botzone_fields = {
        "num_players", "dealer_id", "my_id", "my_chips", "my_cards",
        "public_cards", "history", "hand", "max_hand",
        "total_win_chips", "total_win_games",
    }
    assert set(req.keys()) == botzone_fields, (
        f"字段集合偏离 Botzone 官方 11 字段。"
        f"多余: {set(req.keys()) - botzone_fields}；"
        f"缺失: {botzone_fields - set(req.keys())}"
    )
    # Botzone 全名字段值
    assert req["num_players"] == 2
    assert req["dealer_id"] == 0
    assert req["my_id"] == 0
    assert req["hand"] == 12
    assert req["max_hand"] == 70
    assert len(req["my_cards"]) == 2
    assert len(req["public_cards"]) == 3
    assert req["my_chips"] == 19900
    assert req["total_win_chips"] == [0, 0]
    assert req["total_win_games"] == [0, 0]
    assert isinstance(req["history"], list)
    assert req["history"][0]["action_type"] == "raise"


def test_parse_response_bare_int():
    """Botzone response 是裸整数。"""
    assert parse_response(RESP_FOLD) == ("fold", None)
    assert parse_response(RESP_ALLIN) == ("allin", None)
    assert parse_response(RESP_CALL_CHECK) == ("call", None)
    assert parse_response(150) == ("raise", 150)  # raise delta
    assert parse_response(1) == ("raise", 1)


def test_parse_response_envelope():
    """Botzone 信封 {"response": int}。"""
    assert parse_response({"response": -1}) == ("fold", None)
    assert parse_response({"response": -2}) == ("allin", None)
    assert parse_response({"response": 0}) == ("call", None)
    assert parse_response({"response": 250}) == ("raise", 250)


def test_parse_response_string():
    assert parse_response("-1") == ("fold", None)
    assert parse_response("150") == ("raise", 150)
    assert parse_response('{"response":-2}') == ("allin", None)


def test_parse_response_legacy_format_rejected():
    """旧 {a, x} 格式已废弃（全面对齐 Botzone 标准协议）——拒绝并报错。"""
    with pytest.raises(ValueError):
        parse_response({"a": "f"})
    with pytest.raises(ValueError):
        parse_response({"a": "r", "x": 400})


def test_parse_response_invalid():
    with pytest.raises(Exception):
        parse_response({"response": "not an int"})
    with pytest.raises(Exception):
        parse_response({"response": True})  # bool rejected
    with pytest.raises(Exception):
        parse_response(99.5)  # float not int


def test_action_to_history_int():
    assert action_to_history_int("fold", None) == RESP_FOLD
    assert action_to_history_int("allin", None) == RESP_ALLIN
    assert action_to_history_int("call", None) == RESP_CALL_CHECK
    assert action_to_history_int("check", None) == RESP_CALL_CHECK
    assert action_to_history_int("raise", 250) == 250
    # raise without delta / non-positive → error
    with pytest.raises(ValueError):
        action_to_history_int("raise", None)
    with pytest.raises(ValueError):
        action_to_history_int("raise", 0)
    with pytest.raises(ValueError):
        action_to_history_int("raise", -5)


def test_protocol_version():
    assert PROTOCOL_VERSION == 2


def test_protocol_request_schema_matches_engine():
    """contracts/protocol_request.schema.json 必须与引擎 build_act_request 产出字段
    严格一致（双向守护）——schema 是 Bot 作者读的协议真相源，若与引擎 drift 会误导。

    PR#115 移除了 7+ 平台扩展字段（to_call/sb/bb/opp_chips/...），但当时漏更新本
    schema（审计 P1-A 发现）。本测试防此类 drift 再次发生。
    """
    import json
    from pathlib import Path

    schema_path = Path(__file__).resolve().parents[3] / "contracts" / "protocol_request.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    schema_fields = set(schema["properties"].keys())

    # 引擎实际产出字段（与 test_build_act_request_botzone_fields 同源）
    req = build_act_request(
        hand=0, total_hands=70, my_id=0, dealer_id=0,
        my_cards=[Card(Suit.SPADE, 2), Card(Suit.SPADE, 3)], board=[], history=[], my_chips=1000,
    )
    engine_fields = set(req.keys())

    assert schema_fields == engine_fields, (
        f"协议 schema 与引擎产出字段 drift。"
        f"schema 多余: {schema_fields - engine_fields}；"
        f"schema 缺失: {engine_fields - schema_fields}"
    )
    # additionalProperties: false 必须开启（防扩展字段回归）
    assert schema.get("additionalProperties") is False, "schema 必须禁额外字段（防扩展回归）"


def test_fail_response_parseable_per_game():
    """每游戏的 fail_response 返回值必须能直接喂给该游戏的解析器不抛（审计 P1）。

    holdem fail_response 返裸 int -1（=fold，Botzone 标准）；棋类返 dict。
    签名统一为 Any（如实反映），运行时各解析器兼容各自返回类型。
    """
    from bzplat.backend.games import registry
    from bzplat.backend.games.holdem.protocol import parse_response
    from bzplat.backend.games._board_protocol import parse_xy

    # holdem: fail_response 返回值能喂 parse_response
    h = registry.get("holdem").protocol.fail_response()
    parse_response(h)  # 不抛

    # gomoku/pencil: fail_response 返回值能喂 parse_xy
    for gid in ("gomoku", "pencil"):
        g = registry.get(gid).protocol.fail_response()
        parse_xy(g)  # 不抛
