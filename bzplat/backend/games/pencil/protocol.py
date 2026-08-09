"""点格棋唯一行协议入口；底层棋类 JSON 工具共享且随裁判源码公开。"""
from bzplat.backend.games._board_protocol import (
    build_pencil_request,
    build_xy_response,
    dumps_request,
    loads_response,
    parse_xy,
    validate_response_payload,
)

__all__ = [
    "build_pencil_request",
    "build_xy_response",
    "dumps_request",
    "loads_response",
    "parse_xy",
    "validate_response_payload",
]
