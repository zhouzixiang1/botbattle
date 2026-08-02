"""五子棋行协议（全面解耦 PR-D：序列化逻辑共享自 games/_board_protocol.py）。

gomoku 的行协议与 pencil 同构（一行一条 JSON，{x,y} 语义），故序列化逻辑共享。
本文件仅 re-export 共享工具 + gomoku 的请求 builder；游戏规则在 engine.py。
"""
from bzplat.backend.games._board_protocol import (  # noqa: F401
    PROTOCOL_VERSION,
    build_gomoku_request,
    build_pencil_request,
    build_xy_response,
    dumps_request,
    loads_response,
    parse_xy,
)
