"""转发：bzplat.backend.protocol.board_protocol（棋类协议，向后兼容）。

全面解耦 PR4：gomoku/pencil 各有独立 protocol.py 副本（不共享）。旧 board_protocol
提供 dumps_request/loads_response/parse_xy/build_gomoku_request/build_pencil_request
——本兼容层从两游戏的 protocol 合并导出（dumps/loads 两游戏相同；build_* 各取）。
"""
from bzplat.backend.games.gomoku.protocol import (  # noqa: F401
    dumps_request,
    loads_response,
    parse_xy,
    build_gomoku_request,
)
from bzplat.backend.games.pencil.protocol import build_pencil_request, build_xy_response  # noqa: F401
from bzplat.backend.games.gomoku.protocol import PROTOCOL_VERSION  # noqa: F401
