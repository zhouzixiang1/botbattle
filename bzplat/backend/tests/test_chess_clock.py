"""象棋钟计时测试（_ChessClock 纯逻辑 + runner 集成）。"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from bzplat.backend.matches.runner import _ChessClock, MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
_PENCIL_BOT = SAMPLES / "pencilbot_linux_amd64"


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


@pytest.fixture(autouse=True)
def _local_bot():
    os.environ["BZ_BOT_LOCAL"] = "1"


def test_pencil_match_emits_time_used_events():
    """pencil 对局（time_budget_per_side=900）emit time_used 事件。

    用样例 pencilbot（local 模式，长驻）跑一场完整 pencil 对局，启用象棋钟，
    验证每方决策后都 emit "time_used" 事件（含 seat/used/remaining/budget 字段）。
    """
    if not _PENCIL_BOT.is_file():
        pytest.skip("pencilbot sample missing")
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    events: list[dict] = []

    def on_event(kind: str, ev: dict) -> None:
        events.append(ev)

    asyncio.run(runner.run_binaries(
        str(_PENCIL_BOT), str(_PENCIL_BOT),
        game_id="pencil",
        on_event=on_event,
        time_budget_per_side=900.0,
        seed=1,
    ))
    time_used = [e for e in events if e.get("type") == "time_used"]
    assert time_used, "pencil 对局启用象棋钟应 emit time_used 事件"
    # 两方都应被记录（seat 0 与 seat 1）
    seats = {e["seat"] for e in time_used}
    assert seats == {0, 1}, f"time_used 应覆盖双方 seat，实际 {seats}"
    # 字段契约
    first = time_used[0]
    assert first["budget"] == 900.0
    assert "used" in first and "remaining" in first
    # used 不应超过 budget
    assert all(e["used"] <= 900.0 for e in time_used)


def test_no_time_budget_emits_no_time_used_events():
    """未启用象棋钟（time_budget_per_side=None）时不 emit time_used 事件。

    同一 pencil 对局，不传 time_budget_per_side（默认 None）→ clock.active=False，
    decide 不 emit time_used。证明时钟是 opt-in，对非 pencil 游戏无副作用。
    """
    if not _PENCIL_BOT.is_file():
        pytest.skip("pencilbot sample missing")
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    events: list[dict] = []

    def on_event(kind: str, ev: dict) -> None:
        events.append(ev)

    asyncio.run(runner.run_binaries(
        str(_PENCIL_BOT), str(_PENCIL_BOT),
        game_id="pencil",
        on_event=on_event,
        # 不传 time_budget_per_side → None → 不计时
        seed=1,
    ))
    time_used = [e for e in events if e.get("type") == "time_used"]
    assert not time_used, "未启用象棋钟不应 emit time_used 事件"


def test_pencil_time_budget_passthrough_to_run_duplicate():
    """run_duplicate 接受 time_budget_per_side 并透传给退化单 leg 的 run_binaries。

    pencil 的 spec.build_match_plan is None → run_duplicate 退化为单 leg
    run_binaries；验证该路径下 time_budget_per_side 仍生效（emit time_used），
    且参数透传不崩。
    """
    if not _PENCIL_BOT.is_file():
        pytest.skip("pencilbot sample missing")
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    events: list[dict] = []

    def on_event(kind: str, ev: dict) -> None:
        events.append(ev)

    asyncio.run(runner.run_duplicate(
        str(_PENCIL_BOT), str(_PENCIL_BOT),
        game_id="pencil",
        on_event=on_event,
        time_budget_per_side=900.0,
        seed=1,
    ))
    time_used = [e for e in events if e.get("type") == "time_used"]
    assert time_used, "run_duplicate(pencil) 透传 time_budget_per_side 后应 emit time_used"
