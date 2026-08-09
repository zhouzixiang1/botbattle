"""点格棋行协议（全面解耦 PR-D：序列化逻辑共享自 games/_board_protocol.py）。

pencil 的行协议与 gomoku 同构（一行一条 JSON，{x,y} 语义），故序列化逻辑共享。
本文件仅 re-export 共享工具 + pencil 的请求 builder；游戏规则在 engine.py。
"""
from bzplat.backend.games._board_protocol import (  # noqa: F401
    build_gomoku_request,
    build_pencil_request,
    build_xy_response,
    dumps_request,
    loads_response,
    parse_xy,
    validate_response_payload,
)
