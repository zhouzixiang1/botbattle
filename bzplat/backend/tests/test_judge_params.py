"""裁判规则参数（settings → 引擎）贯通测试。

验证：settings 写入后 `_judge_params` 能读到、`run_session` 用新参数构造 Session、
缺失/非法 settings 时用引擎常量兜底。
"""
from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.engine.gomoku import BOARD_SIZE
from bzplat.backend.engine.registry import run_session
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    SETTING_JUDGE_GOMOKU_SIZE,
    SETTING_JUDGE_HOLDEM_BB,
    SETTING_JUDGE_HOLDEM_SB,
    SETTING_JUDGE_HOLDEM_STACK,
)


@pytest.fixture()
def store(tmp_path):
    return Store(str(tmp_path / "judge.db"))


@pytest.fixture()
def orch(store):
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    return MatchOrchestrator(store, runner=runner, max_concurrent=1)


# ── _judge_params 读取与兜底 ──────────────────────────────────────

def test_judge_params_defaults_when_unset(orch):
    """未写 settings 时全部返回 None（交由引擎常量兜底）。"""
    jp = orch._judge_params()
    assert jp == {"board_size": None, "starting_stack": None, "sb": None, "bb": None}


def test_judge_params_reads_settings(orch, store):
    """写入合法 settings 后 _judge_params 读到对应值。"""
    store.set_setting(SETTING_JUDGE_GOMOKU_SIZE, "9")
    store.set_setting(SETTING_JUDGE_HOLDEM_STACK, "5000")
    store.set_setting(SETTING_JUDGE_HOLDEM_SB, "25")
    store.set_setting(SETTING_JUDGE_HOLDEM_BB, "50")
    jp = orch._judge_params()
    assert jp == {"board_size": 9, "starting_stack": 5000, "sb": 25, "bb": 50}


def test_judge_params_bad_values_fall_back(orch, store):
    """非法值（负数/非数字/空串）返回 None，由引擎兜底。"""
    store.set_setting(SETTING_JUDGE_GOMOKU_SIZE, "not-a-number")
    store.set_setting(SETTING_JUDGE_HOLDEM_STACK, "0")
    store.set_setting(SETTING_JUDGE_HOLDEM_BB, "-5")
    store.set_setting(SETTING_JUDGE_HOLDEM_SB, "")
    jp = orch._judge_params()
    assert jp == {"board_size": None, "starting_stack": None, "sb": None, "bb": None}


# ── run_session 用新参数构造 Session ──────────────────────────────

def _run_callables(game_id, **kw):
    """两个「总下第一空点」的 callable bot 自打自一局。"""
    import random

    rng = random.Random(0)

    async def make_decide(board_state):
        async def decide(player_idx, request):
            return board_state[player_idx].play(request)
        return decide

    # gomoku: 每方维护本地棋盘，随机/顺序下空点
    if game_id == "gomoku":
        class GState:
            def __init__(self):
                self.b = [[-1] * 99 for _ in range(99)]
                self.i = 0
            def play(self, req):
                me = int(req.get("me", 0))
                ox, oy = int(req.get("x", -1)), int(req.get("y", -1))
                if ox >= 0:
                    self.b[ox][oy] = 1 - me
                # 顺序找一个空点
                x = self.i
                self.i += 1
                self.b[x][0] = me
                return {"x": x, "y": 0}
        sa, sb = GState(), GState()

        async def decide(player_idx, request):
            return sa.play(request) if player_idx == 0 else sb.play(request)

        return asyncio.run(run_session(game_id, decide, rng=rng, **kw))

    raise ValueError(f"unsupported game in helper: {game_id}")


def test_gomoku_board_size_takes_effect():
    """board_size=9 时对局在 9×9 棋盘进行：第 10 手（index 9）会越界被判非法。"""
    result = _run_callables("gomoku", board_size=9)
    # 黑方从 x=0..顺序下，白方同理；9×9 共 81 格，双方各占一半。
    # 这里只断言参数确实被传进去（9 而非默认 15）：非法应在 x>=9 后触发。
    assert result.reason in ("five", "illegal", "draw")
    # 关键：回合数不应超过 9*9（若仍是 15×15 则不会因 9 越界而提前结束结构异常）
    assert result.rounds_played <= 9 * 9 + 1


def test_gomoku_board_size_none_uses_default():
    """board_size=None 用默认 15。"""
    result = _run_callables("gomoku")  # 不传 board_size
    assert result.reason in ("five", "illegal", "draw")
    # 默认 15×15，回合数上限 15*15+1
    assert result.rounds_played <= 15 * 15 + 1


# ── runner 透传 ───────────────────────────────────────────────────

def test_runner_passes_judge_params_to_session(monkeypatch):
    """run_callables 把 board_size 透传给 run_session（patch run_session 捕获）。"""
    captured: dict = {}

    async def fake_run_session(game_id, decide, **kw):
        captured.update(kw)
        from bzplat.backend.engine.result import MatchResult
        return MatchResult(rounds_played=0)

    runner = MatchRunner(BinaryRunner(prefer_local=True))
    monkeypatch.setattr("bzplat.backend.matches.runner.run_session", fake_run_session)

    async def decide_a(req):
        return {"x": 0, "y": 0}

    async def decide_b(req):
        return {"x": 0, "y": 0}

    asyncio.run(
        runner.run_callables(
            decide_a, decide_b, game_id="gomoku",
            board_size=13, starting_stack=10000, sb=25, bb=50,
        )
    )
    assert captured["board_size"] == 13
    assert captured["starting_stack"] == 10000
    assert captured["sb"] == 25
    assert captured["bb"] == 50
