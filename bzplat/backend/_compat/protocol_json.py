"""转发：bzplat.backend.protocol.json_protocol → bzplat.backend.games.holdem.protocol。

德州紧凑 JSON 协议已迁入 games/holdem/protocol.py。
"""
from bzplat.backend.games.holdem.protocol import *  # noqa: F401,F403
from bzplat.backend.games.holdem.protocol import (  # noqa: F401
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
