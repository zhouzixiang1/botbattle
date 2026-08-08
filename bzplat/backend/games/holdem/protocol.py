"""德州扑克行协议（完全照 Botzone TexasHoldem2p 标准）。

参考：https://wiki.botzone.org.cn/index.php?title=TexasHoldem2p

关键点（对照 Botzone wiki）：
- **Request 负载字段全名**：``num_players`` / ``dealer_id`` / ``my_id`` / ``my_chips``
  / ``my_cards``(0-51) / ``public_cards``(0-51) / ``history``(对象数组) / ``hand``
  / ``max_hand`` / ``total_win_chips`` / ``total_win_games``。
- **Response 是裸整数**：``-1``=fold, ``-2``=allin, ``0``=call/check, ``>0``=raise
  **额外加注量**（=「需要额外下注的筹码」= raise_to_total - 当前已下注额）。
- **牌编码 0-51**：``card % 4`` = 花色（0♥ 1♦ 2♠ 3♣），``card // 4 + 2`` = 点数（2..14）。
  裁判 Card（holdem_judge.Card）的花色编码恰与 Botzone 线协议一致（Suit: 0♥ 1♦ 2♠ 3♣），
  故 encode/decode 无需映射表——``encode_card(card) == card.to_int()``。
- **history 对象**：``{"round": 0/1/2/3, "player_id": 0/1, "action": <裸整数>,
  "action_type": "fold"/"call"/"check"/"raise"/"allin"}``。

引擎内部用 raise-to-total 语义（min_raise_to = 2×current_bet），转换发生在
本协议边界：``build_act_request`` 写 history.action 时把引擎的 raise-to 换算成
delta；``parse_response`` 读 Bot 的 raise delta 后引擎再转成 raise-to-total 校验。
"""

from __future__ import annotations

import json
from typing import Any, Sequence

from bzplat.backend.games.holdem.holdem_judge import Card, Suit

PROTOCOL_VERSION = 2  # Botzone 标准（v1 是旧的紧凑协议，已废弃）

# ── Botzone 裸整数 response 码 ─────────────────────────────────────────────
RESP_FOLD = -1
RESP_ALLIN = -2
RESP_CALL_CHECK = 0

# action_type 字符串（Botzone history.action_type）
ATYPE_FOLD = "fold"
ATYPE_ALLIN = "allin"
ATYPE_CALL = "call"
ATYPE_CHECK = "check"
ATYPE_RAISE = "raise"

ACTION_TO_ATYPE = {
    "fold": ATYPE_FOLD,
    "check": ATYPE_CHECK,
    "call": ATYPE_CALL,
    "raise": ATYPE_RAISE,
    "allin": ATYPE_ALLIN,
}
ATYPE_TO_ACTION = {v: k for k, v in ACTION_TO_ATYPE.items()}

# 裸整数 → 动作名（>0 是 raise delta，具体量由调用方取）。
_INT_TO_ACTION = {
    RESP_FOLD: "fold",
    RESP_ALLIN: "allin",
    RESP_CALL_CHECK: "call",  # 0 可能是 call 也可能是 check，引擎按合法集判定
}

# 街道 → Botzone history.round（0=preflop 1=flop 2=turn 3=river）
STREET_TO_ROUND = {"preflop": 0, "flop": 1, "turn": 2, "river": 3}

# 裁判 Card 花色编码（Suit: 0♥ 1♦ 2♠ 3♣）== Botzone 线协议花色编码，无需映射。


def encode_card(card: Card) -> int:
    """Card → Botzone 整数 ``(number-2)*4 + suit``（0-51）。

    裁判 Card 的 ``to_int()`` 即此公式（花色编码与 Botzone 一致，无映射表）。
    """
    return card.to_int()


def decode_card(n: int) -> Card:
    """Botzone 整数 → Card。``n%4``=suit(0♥1♦2♠3♣)，``n//4+2``=number(2..14)。"""
    if not isinstance(n, int) or isinstance(n, bool) or n < 0 or n > 51:
        raise ValueError(f"invalid card_int: {n}")
    return Card.from_int(n)


def encode_cards(cards: Sequence[Card]) -> list[int]:
    return [encode_card(c) for c in cards]


def decode_cards(ints: Sequence[int]) -> list[Card]:
    return [decode_card(i) for i in ints]


def build_act_request(
    *,
    hand: int,
    total_hands: int,
    my_id: int,
    dealer_id: int,
    my_cards: Sequence[Card],
    board: Sequence[Card],
    history: list[dict[str, Any]],
    my_chips: int,
    total_win_chips: list[int] | None = None,
    total_win_games: list[int] | None = None,
) -> dict[str, Any]:
    """构造 Botzone 标准 act 请求负载（信封由传输层包）。

    字段名严格对齐 Botzone TexasHoldem2p 的 11 个官方字段（不多发任何平台扩展字段，
    标准 Botzone Bot 可直接跑）：
    ``num_players`` / ``dealer_id`` / ``my_id`` / ``my_chips`` / ``my_cards``(0-51)
    / ``public_cards``(0-51) / ``history``(对象数组) / ``hand`` / ``max_hand``
    / ``total_win_chips`` / ``total_win_games``。

    ``hand`` 为 0-based 当前手牌序号；``max_hand`` = 总手数（用户要求 70，Botzone
    文档默认 50）。需要更多决策信息（to_call/sb/bb/对手筹码等）的 Bot 可从
    ``history`` + ``my_chips`` 自行重放推导——这正是 Botzone 标准模型。
    """
    return {
        "num_players": 2,
        "dealer_id": int(dealer_id),
        "my_id": int(my_id),
        "my_chips": int(my_chips),
        "my_cards": encode_cards(my_cards),
        "public_cards": encode_cards(board),
        "history": history,
        "hand": int(hand),
        "max_hand": int(total_hands),
        "total_win_chips": list(total_win_chips) if total_win_chips is not None else [0, 0],
        "total_win_games": list(total_win_games) if total_win_games is not None else [0, 0],
    }


def action_to_history_int(action: str, raise_extra: int | None) -> int:
    """动作 → Botzone history.action 裸整数。

    - fold → -1；allin → -2；call/check → 0；raise → raise_extra（>0）。
    - raise 时 raise_extra 必须为正整数（=「额外下注筹码」=raise_to - 旧 current_bet）。
    """
    if action == "fold":
        return RESP_FOLD
    if action == "allin":
        return RESP_ALLIN
    if action in ("call", "check"):
        return RESP_CALL_CHECK
    if action == "raise":
        if raise_extra is None or raise_extra <= 0:
            raise ValueError(f"raise 需要 raise_extra>0，得到 {raise_extra}")
        return int(raise_extra)
    raise ValueError(f"未知动作: {action}")


def parse_response(raw: Any) -> tuple[str, int | None]:
    """解析 Bot 输出 → ``(action_name, raise_delta | None)``。

    全面对齐 Botzone 标准协议，只接受两种输入：
    1. 裸整数（Botzone 标准）：``-1`` / ``-2`` / ``0`` / ``>0``。
    2. ``{"response": <裸整数>}`` 信封（传输层 extract_response_payload 取出的负载包成信封）。

    返回：raise 时第二个元素是 **raise delta**（额外量，非 raise-to-total）。
    """
    # 裸整数
    if isinstance(raw, int) and not isinstance(raw, bool):
        return _int_to_action(raw), (raw if raw > 0 else None)

    # 字符串 → 先尝试整数再 JSON
    if isinstance(raw, str):
        s = raw.strip()
        try:
            n = int(s)
            return _int_to_action(n), (n if n > 0 else None)
        except ValueError:
            raw = json.loads(s)

    # dict：Botzone 信封 {"response": int}
    if isinstance(raw, dict):
        if "response" in raw:
            payload = raw["response"]
            if isinstance(payload, bool) or not isinstance(payload, int):
                raise ValueError(f"response 必须是整数，得到 {payload!r}")
            return _int_to_action(payload), (payload if payload > 0 else None)
        raise ValueError("响应 dict 缺 response 字段（Botzone 标准协议要求 {\"response\": int}）")

    raise ValueError(f"无法解析的响应: {raw!r}")


def _int_to_action(n: int) -> str:
    """裸整数 → 动作名（0 优先当 call；引擎按合法集把非法 call 折成 check/fold）。"""
    if n in _INT_TO_ACTION:
        return _INT_TO_ACTION[n]
    if n > 0:
        return "raise"
    raise ValueError(f"未知 response 整数: {n}")


def dumps_request(req: dict[str, Any]) -> str:
    """序列化请求负载为单行 JSON（信封化由 runner 传输层做）。"""
    return json.dumps(req, separators=(",", ":"), ensure_ascii=False)


def loads_response(line: str) -> dict[str, Any]:
    """解析 Bot 输出一行（返回信封 dict，payload 由调用方取）。"""
    return json.loads(line)


def fail_response() -> int:
    """超时/异常兜底：fold（Botzone 裸整数 -1）。"""
    return RESP_FOLD
