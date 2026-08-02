"""向后兼容 shim（全面解耦 PR4）——转发到 _compat.protocol_board。

棋类协议已拆为 gomoku/pencil 各自独立 protocol.py 副本（不共享）。
"""
from bzplat.backend._compat.protocol_board import *  # noqa: F401,F403
