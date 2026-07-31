"""紧凑 JSON 协议编解码。"""

from bzplat.backend.protocol.json_protocol import (
    ACTION_TO_A,
    ACTION_TO_CODE,
    A_TO_ACTION,
    CODE_TO_ACTION,
    PROTOCOL_VERSION,
    build_act_request,
    build_response,
    decode_card,
    decode_cards,
    dumps_request,
    encode_card,
    encode_cards,
    loads_response,
    parse_response,
)

__all__ = [
    "PROTOCOL_VERSION",
    "ACTION_TO_CODE",
    "CODE_TO_ACTION",
    "A_TO_ACTION",
    "ACTION_TO_A",
    "encode_card",
    "decode_card",
    "encode_cards",
    "decode_cards",
    "build_act_request",
    "parse_response",
    "build_response",
    "dumps_request",
    "loads_response",
]
