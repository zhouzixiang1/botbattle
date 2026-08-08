"""象棋钟计时测试（_ChessClock 纯逻辑 + runner 集成）。"""
from __future__ import annotations

from bzplat.backend.matches.runner import _ChessClock


def test_chess_clock_remaining_decreases():
    """每次 record 后 remaining 减少。"""
    clk = _ChessClock(budget=900.0)
    assert clk.remaining(0) == 900.0
    clk.record(0, 100.0)  # 座0 用了 100s
    assert clk.remaining(0) == 800.0
    assert clk.remaining(1) == 900.0  # 座1 未动


def test_chess_clock_timeout_when_exhausted():
    """剩余≤0 时 is_exhausted 返回 True。"""
    clk = _ChessClock(budget=900.0)
    clk.record(0, 900.0)
    assert clk.is_exhausted(0)
    assert not clk.is_exhausted(1)


def test_chess_clock_used():
    clk = _ChessClock(budget=900.0)
    clk.record(0, 50.5)
    clk.record(0, 49.5)
    assert clk.used(0) == 100.0
