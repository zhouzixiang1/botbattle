"""向后兼容 shim（全面解耦 PR4）——转发到 _compat.protocol_json。

德州紧凑 JSON 协议已迁入 games/holdem/protocol.py。
"""
from bzplat.backend._compat.protocol_json import *  # noqa: F401,F403
