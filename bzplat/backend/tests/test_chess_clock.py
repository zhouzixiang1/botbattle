"""象棋钟计时测试（_ChessClock 纯逻辑 + runner 集成）。"""
from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

import bzplat.backend.matches.runner as runner_module
import bzplat.backend.runtime.binary_runner as binary_runner_module
from bzplat.backend.bots.classify import BinaryInfo
from bzplat.backend.games import registry as game_registry
from bzplat.backend.games.base import TimeControlSpec
from bzplat.backend.games import _botzone_protocol as botzone_protocol
from bzplat.backend.matches.runner import (
    _ChessClock,
    _botzone_decide,
    MatchRunner,
)
from bzplat.backend.runtime.binary_runner import (
    BinaryRunner,
    BotDecisionTimeoutError,
    BotProtocolError,
    BotSession,
    ExecutionScope,
)
from bzplat.backend.runtime.limits import PLATFORM_LOW_PROFILE
from bzplat.backend.runtime.local_ai import (
    LocalAIHub,
    LocalAIResponseRejected,
    LocalAITechnicalError,
)

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
def _local_bot(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")


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


def test_omitted_time_control_uses_game_default():
    """不传 ID 时使用游戏默认时限，不存在无时限隐式分支。"""
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
        seed=1,
    ))
    time_used = [e for e in events if e.get("type") == "time_used"]
    assert time_used
    starts = [e for e in events if e.get("type") == "match_start"]
    assert starts[0]["time_control"] == {
        "id": "pencil_per_side_total_900s_v1",
        "mode": "per_side_total",
        "seconds": 900,
        "applies_to": "both_bots",
    }


def test_pencil_run_duplicate_is_rejected_instead_of_falling_back():
    """无 duplicate 计划的游戏不能把请求静默改成单 leg。"""
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    with pytest.raises(ValueError, match="不支持 duplicate"):
        asyncio.run(runner.run_duplicate(
            "/not/used/a", "/not/used/b",
            game_id="pencil",
            time_budget_per_side=900.0,
            seed=1,
        ))


class _FakeBinaryRunner:
    def __init__(self) -> None:
        self.stopped: list[str] = []
        self._sessions: dict[str, object] = {}
        self.runtime_ready_calls = 0

    async def start_session(
        self,
        _path: str,
        *,
        runtime_mode: str,
        profile=PLATFORM_LOW_PROFILE,
    ) -> str:
        sid = f"session-{runtime_mode}"
        self._sessions[sid] = SimpleNamespace(
            binary_path=_path,
            runtime_mode=runtime_mode,
            profile=profile,
            requests=[],
            responses=[],
            turn=0,
            long_running=False,
        )
        return sid

    async def prepare_session(
        self,
        _path: str,
        *,
        runtime_mode: str,
        profile=PLATFORM_LOW_PROFILE,
    ) -> str:
        sid = f"session-{runtime_mode}"
        self._sessions[sid] = SimpleNamespace(
            binary_path=_path,
            runtime_mode=runtime_mode,
            profile=profile,
            requests=[],
            responses=[],
            turn=0,
            long_running=False,
        )
        return sid

    async def ensure_runtime_ready(self) -> None:
        self.runtime_ready_calls += 1

    async def stop_session(self, session_id: str) -> None:
        self.stopped.append(session_id)


def test_pencil_clock_uses_cumulative_remaining_not_fixed_action_timeout(
    monkeypatch,
):
    """Pencil 每步等待上限来自该座位剩余 900s，而非固定单步 timeout。"""

    observed_timeouts: list[float] = []

    async def fake_bot_decide(
        *_args, action_timeout, on_decision_elapsed, **_kwargs
    ):
        observed_timeouts.append(action_timeout)
        on_decision_elapsed(0.2)
        return {"response": {"x": 0, "y": 0}}

    async def fake_run_session(_game_id, decide, **_kwargs):
        await decide(0, {})
        await decide(0, {})
        return object()

    monkeypatch.setattr(runner_module, "_botzone_decide", fake_bot_decide)
    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    binary_runner = _FakeBinaryRunner()
    runner = MatchRunner(binary_runner, action_timeout=0.001)

    asyncio.run(
        runner.run_binaries(
            "/fake/a",
            "/fake/b",
            game_id="pencil",
            time_budget_per_side=900.0,
        )
    )

    assert observed_timeouts == pytest.approx([900.0, 899.8])
    assert binary_runner.runtime_ready_calls == 2
    assert binary_runner.stopped == ["session-traditional", "session-traditional"]


def test_per_decision_control_resets_for_every_request(monkeypatch):
    observed_timeouts: list[float] = []

    async def fake_bot_decide(
        *_args, action_timeout, on_decision_elapsed, **_kwargs
    ):
        observed_timeouts.append(action_timeout)
        on_decision_elapsed(0.8)
        return {"response": {"x": 0, "y": 0}}

    async def fake_run_session(_game_id, decide, **_kwargs):
        await decide(0, {})
        await decide(0, {})
        return object()

    monkeypatch.setattr(runner_module, "_botzone_decide", fake_bot_decide)
    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    events: list[dict] = []
    asyncio.run(
        MatchRunner(_FakeBinaryRunner()).run_binaries(
            "/fake/a",
            "/fake/b",
            game_id="pencil",
            time_control_id="pencil_per_decision_1s_v1",
            on_event=lambda _kind, event: events.append(event),
        )
    )

    assert observed_timeouts == [1.0, 1.0]
    used = [event for event in events if event["type"] == "time_used"]
    assert [event["used"] for event in used] == [0.8, 0.8]


def test_pencil_forced_pass_is_a_separately_timed_decision(monkeypatch):
    """The protocol's pass acknowledgement consumes its own 1s decision."""

    observed: list[tuple[int, float]] = []

    async def fake_bot_decide(
        _runner,
        _session_id,
        request,
        *,
        action_timeout,
        on_decision_elapsed,
        **_kwargs,
    ):
        observed.append((int(request.get("pass") or 0), action_timeout))
        on_decision_elapsed(0.4)
        return {"response": {"x": -1, "y": -1}}

    async def fake_run_session(_game_id, decide, **_kwargs):
        await decide(0, {"pass": 0})
        await decide(1, {"pass": 1})
        return object()

    monkeypatch.setattr(runner_module, "_botzone_decide", fake_bot_decide)
    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    asyncio.run(
        MatchRunner(_FakeBinaryRunner()).run_binaries(
            "/fake/a",
            "/fake/b",
            game_id="pencil",
            time_control_id="pencil_per_decision_1s_v1",
        )
    )

    assert observed == [(0, 1.0), (1, 1.0)]


def test_duplicate_resets_per_decision_clock_for_each_request_and_leg(monkeypatch):
    observed_timeouts: list[float] = []

    async def fake_bot_decide(
        *_args, action_timeout, on_decision_elapsed, **_kwargs
    ):
        observed_timeouts.append(action_timeout)
        on_decision_elapsed(59.0)
        return {"response": 0}

    async def fake_run_session(_game_id, decide, **_kwargs):
        await decide(0, {})
        await decide(0, {})
        return SimpleNamespace(
            rounds=[],
            rounds_played=0,
            events=[],
            net=[0, 0],
            final_chips=[0, 0],
        )

    monkeypatch.setattr(runner_module, "_botzone_decide", fake_bot_decide)
    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    events: list[dict] = []
    result = asyncio.run(
        MatchRunner(_FakeBinaryRunner()).run_duplicate(
            "/fake/a",
            "/fake/b",
            game_id="holdem",
            time_control_id="holdem_per_decision_60s_v1",
            on_event=lambda _kind, event: events.append(event),
            seed=7,
            duplicate=True,
        )
    )

    assert len(result.legs) == 2
    assert observed_timeouts == [60.0, 60.0, 60.0, 60.0]
    starts = [event for event in events if event["type"] == "match_start"]
    assert [event["leg"] for event in starts] == [0, 1]
    assert all(
        event["time_control"]["id"] == "holdem_per_decision_60s_v1"
        for event in starts
    )
    used = [event for event in events if event["type"] == "time_used"]
    assert [event["used"] for event in used] == [59.0, 59.0, 59.0, 59.0]


def test_duplicate_resets_cumulative_clock_for_each_scoring_game(monkeypatch):
    cumulative = TimeControlSpec(
        id="holdem_per_side_total_1s_test_v1",
        mode="per_side_total",
        seconds=1,
    )
    original_get = game_registry.get
    holdem = original_get("holdem")
    test_spec = replace(
        holdem,
        time_controls=(cumulative,),
        default_time_control_id=cumulative.id,
    )
    monkeypatch.setattr(
        game_registry,
        "get",
        lambda game_id: test_spec if game_id == "holdem" else original_get(game_id),
    )
    observed_timeouts: list[float] = []

    async def fake_bot_decide(
        *_args, action_timeout, on_decision_elapsed, **_kwargs
    ):
        observed_timeouts.append(action_timeout)
        on_decision_elapsed(0.4)
        return {"response": 0}

    async def fake_run_session(_game_id, decide, **_kwargs):
        await decide(0, {})
        await decide(0, {})
        return SimpleNamespace(
            rounds=[],
            rounds_played=0,
            events=[],
            net=[0, 0],
            final_chips=[0, 0],
        )

    monkeypatch.setattr(runner_module, "_botzone_decide", fake_bot_decide)
    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    result = asyncio.run(
        MatchRunner(_FakeBinaryRunner()).run_duplicate(
            "/fake/a",
            "/fake/b",
            game_id="holdem",
            time_control_id=cumulative.id,
            seed=7,
            duplicate=True,
        )
    )

    assert len(result.legs) == 2
    assert observed_timeouts == pytest.approx([1.0, 0.6, 1.0, 0.6])


def test_per_decision_control_rejects_late_transport_response(monkeypatch):
    async def fake_bot_decide(*_args, on_decision_elapsed, **_kwargs):
        on_decision_elapsed(1.01)
        return {"response": {"x": 0, "y": 0}}

    async def fake_run_session(_game_id, decide, **_kwargs):
        return await decide(0, {})

    monkeypatch.setattr(runner_module, "_botzone_decide", fake_bot_decide)
    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    events: list[dict] = []
    with pytest.raises(BotDecisionTimeoutError):
        asyncio.run(
            MatchRunner(_FakeBinaryRunner()).run_binaries(
                "/fake/a",
                "/fake/b",
                game_id="pencil",
                time_control_id="pencil_per_decision_1s_v1",
                on_event=lambda _kind, event: events.append(event),
            )
        )

    assert [event["type"] for event in events] == [
        "time_out",
        "technical_incident",
    ]


def test_legacy_scalar_rejects_unregistered_seconds():
    with pytest.raises(ValueError, match="已注册"):
        asyncio.run(
            MatchRunner(_FakeBinaryRunner()).run_binaries(
                "/fake/a",
                "/fake/b",
                game_id="pencil",
                time_budget_per_side=1.5,
            )
        )


def test_traditional_image_refresh_happens_before_pencil_clock_starts(
    monkeypatch,
):
    """中途 cache 失效需重拉时，平台准备仍不得消耗行动方累计时间。"""
    order: list[str] = []

    class OrderedRunner(_FakeBinaryRunner):
        async def ensure_runtime_ready(self) -> None:
            order.append("image-ready")
            await asyncio.sleep(0)

    async def fake_bot_decide(*_args, on_decision_elapsed, **_kwargs):
        order.append("bot-decide")
        on_decision_elapsed(0.2)
        return {"response": {"x": 0, "y": 0}}

    async def fake_run_session(_game_id, decide, **_kwargs):
        await decide(0, {})
        return object()

    monkeypatch.setattr(runner_module, "_botzone_decide", fake_bot_decide)
    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    asyncio.run(
        MatchRunner(OrderedRunner()).run_binaries(
            "/fake/a",
            "/fake/b",
            game_id="pencil",
            time_budget_per_side=900.0,
        )
    )
    assert order[:2] == ["image-ready", "bot-decide"]


def test_traditional_process_start_is_outside_measured_decision(monkeypatch):
    order: list[str] = []
    ticks = iter((10.0, 10.4))

    class Runner:
        def __init__(self):
            self._sessions = {
                "parent": SimpleNamespace(
                    binary_path="/fake/bot",
                    runtime_mode="traditional",
                    profile=PLATFORM_LOW_PROFILE,
                    requests=[],
                    responses=[],
                    turn=0,
                    long_running=False,
                    execution_scope=None,
                )
            }

        async def start_session(self, *_args, **_kwargs):
            order.append("start")
            self._sessions["child"] = SimpleNamespace()
            return "child"

        async def send(self, *_args, **_kwargs):
            order.append("send")
            return '{"response":0}'

        async def stop_session(self, _session_id):
            order.append("stop")

    def monotonic():
        order.append("clock")
        return next(ticks)

    monkeypatch.setattr(runner_module, "_time", SimpleNamespace(monotonic=monotonic))
    elapsed: list[float] = []
    asyncio.run(
        _botzone_decide(
            Runner(),
            "parent",
            {"hand": 0},
            game_id="holdem",
            action_timeout=60,
            on_decision_elapsed=lambda value: (
                order.append("elapsed"), elapsed.append(value)
            ),
        )
    )

    assert order.index("start") < order.index("clock") < order.index("send")
    assert order.index("elapsed") < order.index("stop")
    assert elapsed == pytest.approx([0.4])


def test_traditional_slow_attempt_check_is_outside_bot_clock(monkeypatch):
    class Clock:
        now = 10.0

        def time(self):
            return self.now

    clock = Clock()
    checks = 0

    def slow_attempt_check():
        nonlocal checks
        checks += 1
        clock.now += 0.3

    scope = ExecutionScope(
        instance="clock-test",
        job_public_id="job-traditional",
        attempt_no=1,
        supervisor=None,
        attempt_check=slow_attempt_check,
    )

    class Stdin:
        def write(self, _data):
            return None

        async def drain(self):
            clock.now += 0.35

    class Stdout:
        async def readline(self):
            clock.now += 0.45
            return b'{"response":0}\n'

    class Proc:
        returncode = None
        stdin = Stdin()
        stdout = Stdout()

    binary = BinaryRunner(prefer_local=False)
    parent = BotSession(
        "parent",
        BinaryInfo("elf", "linux", "amd64", True),
        Path("/fake/bot"),
        runtime_mode="traditional",
        execution_scope=scope,
    )
    binary._sessions[parent.session_id] = parent

    async def start_session(
        binary_path, *, runtime_mode, profile, execution_scope=None
    ):
        binary._sessions["child"] = BotSession(
            "child",
            BinaryInfo("elf", "linux", "amd64", True),
            Path(binary_path),
            proc=Proc(),
            runtime_mode=runtime_mode,
            profile=profile,
            execution_scope=execution_scope,
        )
        return "child"

    async def stop_session(session_id):
        binary._sessions.pop(session_id, None)

    async def exercise():
        async def immediate_wait_for(awaitable, *, timeout):
            return await awaitable

        monkeypatch.setattr(
            binary_runner_module.asyncio, "get_running_loop", lambda: clock
        )
        monkeypatch.setattr(
            binary_runner_module.asyncio, "wait_for", immediate_wait_for
        )
        return await _botzone_decide(
            binary,
            "parent",
            {"hand": 0},
            game_id="holdem",
            action_timeout=1.0,
            on_decision_elapsed=elapsed.append,
        )

    monkeypatch.setattr(binary, "start_session", start_session)
    monkeypatch.setattr(binary, "stop_session", stop_session)
    monkeypatch.setattr(
        runner_module, "_time", SimpleNamespace(monotonic=clock.time)
    )
    elapsed: list[float] = []

    assert asyncio.run(exercise()) == {"response": 0}
    assert checks == 2
    assert elapsed == pytest.approx([0.8])
    assert clock.now == pytest.approx(11.4)


def test_longrunning_first_response_clock_includes_keep_running(monkeypatch):
    order: list[str] = []
    ticks = iter((20.0, 20.3, 20.7))
    handshake_timeouts: list[float] = []
    session = SimpleNamespace(
        runtime_mode="longrunning",
        requests=[],
        responses=[],
        turn=0,
        long_running=False,
    )

    class Runner:
        _sessions = {"bot": session}

        async def send(self, *_args, **_kwargs):
            order.append("response")
            return '{"response":0}'

        async def read_extra_line(self, *_args, timeout, **_kwargs):
            order.append("keep_running")
            handshake_timeouts.append(timeout)
            return botzone_protocol.KEEP_RUNNING_SIGNAL

    monkeypatch.setattr(
        runner_module,
        "_time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )
    elapsed: list[float] = []
    asyncio.run(
        _botzone_decide(
            Runner(),
            "bot",
            {"hand": 0},
            game_id="holdem",
            action_timeout=60,
            on_decision_elapsed=lambda value: (
                order.append("elapsed"), elapsed.append(value)
            ),
        )
    )

    assert order == ["response", "keep_running", "elapsed"]
    assert elapsed == pytest.approx([0.7])
    assert handshake_timeouts == pytest.approx([59.7])
    assert session.long_running is True


def test_longrunning_slow_attempt_checks_preserve_deadline_and_bot_clock(
    monkeypatch,
):
    class Clock:
        now = 20.0

        def time(self):
            return self.now

    clock = Clock()
    checks = 0

    def slow_attempt_check():
        nonlocal checks
        checks += 1
        clock.now += 0.25

    scope = ExecutionScope(
        instance="clock-test",
        job_public_id="job-longrunning",
        attempt_no=1,
        supervisor=None,
        attempt_check=slow_attempt_check,
    )

    class Stdin:
        def write(self, _data):
            return None

        async def drain(self):
            clock.now += 0.2

    class Stdout:
        lines = iter(
            (
                (0.35, b'{"response":0}\n'),
                (0.15, (botzone_protocol.KEEP_RUNNING_SIGNAL + "\n").encode()),
            )
        )

        async def readline(self):
            duration, line = next(self.lines)
            clock.now += duration
            return line

    class Proc:
        returncode = None
        stdin = Stdin()
        stdout = Stdout()

    binary = BinaryRunner(prefer_local=False)
    session = BotSession(
        "bot",
        BinaryInfo("elf", "linux", "amd64", True),
        Path("/fake/bot"),
        proc=Proc(),
        runtime_mode="longrunning",
        execution_scope=scope,
    )
    binary._sessions[session.session_id] = session
    observed_budgets: list[float] = []

    async def exercise():
        async def immediate_wait_for(awaitable, *, timeout):
            observed_budgets.append(timeout)
            return await awaitable

        monkeypatch.setattr(
            binary_runner_module.asyncio, "get_running_loop", lambda: clock
        )
        monkeypatch.setattr(
            binary_runner_module.asyncio, "wait_for", immediate_wait_for
        )
        return await _botzone_decide(
            binary,
            "bot",
            {"hand": 0},
            game_id="holdem",
            action_timeout=1.0,
            on_decision_elapsed=elapsed.append,
        )

    monkeypatch.setattr(
        runner_module, "_time", SimpleNamespace(monotonic=clock.time)
    )
    elapsed: list[float] = []

    assert asyncio.run(exercise()) == {"response": 0}
    assert checks == 4
    assert observed_budgets == pytest.approx([1.0, 0.8, 0.45])
    assert elapsed == pytest.approx([0.7])
    assert clock.now == pytest.approx(21.7)
    assert session.long_running is True


def test_per_decision_deadline_covers_longrunning_handshake(monkeypatch):
    ticks = iter((30.0, 30.8, 31.0))
    session = SimpleNamespace(
        runtime_mode="longrunning",
        requests=[],
        responses=[],
        turn=0,
        long_running=False,
    )
    observed_timeout: list[float] = []

    class Runner:
        _sessions = {"bot": session}

        async def send(self, *_args, **_kwargs):
            return '{"response":{"x":0,"y":0}}'

        async def read_extra_line(self, *_args, timeout, **_kwargs):
            observed_timeout.append(timeout)
            raise asyncio.TimeoutError

    monkeypatch.setattr(
        runner_module,
        "_time",
        SimpleNamespace(monotonic=lambda: next(ticks)),
    )
    elapsed: list[float] = []
    with pytest.raises(BotDecisionTimeoutError):
        asyncio.run(
            _botzone_decide(
                Runner(),
                "bot",
                {"x": -1, "y": -1},
                game_id="pencil",
                action_timeout=1.0,
                on_decision_elapsed=elapsed.append,
            )
        )

    assert observed_timeout == pytest.approx([0.2])
    assert elapsed == pytest.approx([1.0])


def test_longrunning_handshake_timeout_from_binary_runner_is_decision_timeout(
    monkeypatch,
):
    """The second line shares the decision deadline and times out technically."""

    class Clock:
        now = 40.0

        def time(self):
            return self.now

    clock = Clock()
    checks = 0

    def slow_attempt_check():
        nonlocal checks
        checks += 1
        clock.now += 0.25

    scope = ExecutionScope(
        instance="clock-test",
        job_public_id="job-longrunning-timeout",
        attempt_no=1,
        supervisor=None,
        attempt_check=slow_attempt_check,
    )

    class Stdin:
        def write(self, _data):
            return None

        async def drain(self):
            clock.now += 0.2

    class Stdout:
        calls = 0

        async def readline(self):
            self.calls += 1
            if self.calls == 1:
                clock.now += 0.3
                return b'{"response":{"x":0,"y":0}}\n'
            raise AssertionError("second-line timeout is owned by wait_for")

    class Proc:
        returncode = None

        def __init__(self):
            self.stdin = Stdin()
            self.stdout = Stdout()

    class Transport(BinaryRunner):
        def __init__(self):
            super().__init__(prefer_local=True)
            self.next_id = 0

        async def start_session(
            self,
            binary_path,
            *,
            runtime_mode,
            profile,
            execution_scope=None,
        ):
            self.next_id += 1
            sid = f"bot-{self.next_id}"
            self._sessions[sid] = BotSession(
                sid,
                BinaryInfo("elf", "linux", "amd64", True),
                Path(binary_path),
                proc=Proc(),
                runtime_mode=runtime_mode,
                profile=profile,
                execution_scope=execution_scope,
            )
            return sid

        async def stop_session(self, session_id):
            self._sessions.pop(session_id, None)

    observed_budgets: list[float] = []
    wait_calls = 0

    async def deadline_wait_for(awaitable, *, timeout):
        nonlocal wait_calls
        wait_calls += 1
        observed_budgets.append(timeout)
        if wait_calls == 3:
            awaitable.close()
            clock.now += timeout
            raise asyncio.TimeoutError
        return await awaitable

    async def one_decision(_game_id, decide, **_kwargs):
        return await decide(0, {"x": -1, "y": -1})

    monkeypatch.setattr(
        binary_runner_module.asyncio, "get_running_loop", lambda: clock
    )
    monkeypatch.setattr(
        binary_runner_module.asyncio, "wait_for", deadline_wait_for
    )
    monkeypatch.setattr(
        runner_module, "_time", SimpleNamespace(monotonic=clock.time)
    )
    monkeypatch.setattr(runner_module, "run_session", one_decision)
    events: list[dict] = []

    with pytest.raises(BotDecisionTimeoutError) as raised:
        asyncio.run(
            MatchRunner(Transport()).run_binaries(
                "/fake/a",
                "/fake/b",
                game_id="pencil",
                runtime_modes=("longrunning", "longrunning"),
                time_control_id="pencil_per_decision_1s_v1",
                execution_scope=scope,
                on_event=lambda _kind, event: events.append(event),
            )
        )

    assert raised.value.error_code == "decision_timeout"
    assert observed_budgets == pytest.approx([1.0, 0.8, 0.5])
    assert checks == 3
    assert [event["type"] for event in events] == [
        "time_out",
        "technical_incident",
    ]
    assert events[0]["used"] == pytest.approx(1.0)


@pytest.mark.parametrize("handshake_bytes", [b"", b"\n"], ids=["eof", "blank"])
def test_longrunning_eof_or_blank_handshake_remains_missing_keep_running(
    handshake_bytes,
):
    class Stdin:
        def write(self, _data):
            return None

        async def drain(self):
            return None

    async def exercise():
        stdout = asyncio.StreamReader()
        stdout.feed_data(b'{"response":{"x":0,"y":0}}\n')
        if handshake_bytes:
            stdout.feed_data(handshake_bytes)
        stdout.feed_eof()
        proc = SimpleNamespace(
            returncode=None,
            stdin=Stdin(),
            stdout=stdout,
            stderr=None,
        )
        transport = BinaryRunner(prefer_local=True)
        transport._sessions["bot"] = BotSession(
            "bot",
            BinaryInfo("elf", "linux", "amd64", True),
            Path("/fake/bot"),
            proc=proc,
            runtime_mode="longrunning",
            profile=PLATFORM_LOW_PROFILE,
        )
        with pytest.raises(BotProtocolError) as raised:
            await _botzone_decide(
                transport,
                "bot",
                {"x": -1, "y": -1},
                game_id="pencil",
                action_timeout=1.0,
            )
        return raised.value

    failure = asyncio.run(exercise())
    assert failure.error_code == "missing_keep_running"


@pytest.mark.parametrize("source_code", ["local_ai_timeout", "decision_timeout"])
def test_local_ai_timeout_sources_map_to_decision_timeout_and_event(
    monkeypatch,
    source_code,
):
    class TimeoutHub:
        async def request_decision(
            self,
            *_args,
            on_decision_elapsed=None,
            decision_timeout=None,
            **_kwargs,
        ):
            if on_decision_elapsed is not None:
                on_decision_elapsed(float(decision_timeout))
            raise LocalAITechnicalError(
                "local timeout",
                error_code=source_code,
                failed_seat=0,
                turn=1,
            )

    async def one_decision(_game_id, decide, **_kwargs):
        return await decide(0, {"x": -1, "y": -1})

    monkeypatch.setattr(runner_module, "run_session", one_decision)
    events: list[dict] = []

    with pytest.raises(BotDecisionTimeoutError) as raised:
        asyncio.run(
            MatchRunner(
                _FakeBinaryRunner(), local_ai_hub=TimeoutHub()
            ).run_binaries(
                None,
                "/fake/b",
                game_id="pencil",
                execution_environments=("remote_local", "platform_low"),
                local_agent_ids=("agent-a", None),
                match_id="match-local-timeout",
                time_control_id="pencil_per_decision_1s_v1",
                on_event=lambda _kind, event: events.append(event),
            )
        )

    assert raised.value.error_code == "decision_timeout"
    assert [event["type"] for event in events] == [
        "time_out",
        "technical_incident",
    ]
    assert events[0]["used"] == pytest.approx(1.0)


def test_local_ai_cumulative_clock_keeps_deadline_after_delivered_reconnect(
    monkeypatch,
):
    """A delivered turn keeps one clock across reconnect and times out."""

    class Clock:
        now = 100.0

        def __call__(self) -> float:
            return self.now

    clock = Clock()
    hub = LocalAIHub(clock=clock)

    async def two_decisions(_game_id, decide, **_kwargs):
        await decide(0, {"x": -1, "y": -1})
        return await decide(0, {"x": -1, "y": -1})

    monkeypatch.setattr(runner_module, "run_session", two_decisions)
    events: list[dict] = []

    async def scenario() -> BotDecisionTimeoutError:
        await hub.register("agent-a", connection_id="connection-a")

        async def connector() -> None:
            first_prepare = await hub.next_turn("agent-a", "connection-a")
            assert first_prepare is not None
            assert first_prepare.phase == "prepare"
            await hub.mark_prepared(
                "agent-a",
                "connection-a",
                request_id=first_prepare.request_id,
                match_id=first_prepare.match_id,
                turn=first_prepare.turn,
            )
            first = await hub.next_turn("agent-a", "connection-a")
            assert first is not None
            assert first.phase == "decision"
            assert first.deadline_at == pytest.approx(1000)
            clock.now += 0.4
            await hub.submit_response(
                "agent-a",
                "connection-a",
                request_id=first.request_id,
                match_id=first.match_id,
                turn=first.turn,
                output='{"response":{"x":0,"y":0}}',
            )
            second_prepare = await hub.next_turn("agent-a", "connection-a")
            assert second_prepare is not None
            assert second_prepare.phase == "prepare"
            await hub.mark_prepared(
                "agent-a",
                "connection-a",
                request_id=second_prepare.request_id,
                match_id=second_prepare.match_id,
                turn=second_prepare.turn,
            )
            second = await hub.next_turn("agent-a", "connection-a")
            assert second is not None
            assert second.phase == "decision"
            assert second.deadline_at == pytest.approx(1000)
            await hub.close("agent-a", "connection-a")
            # Turn two was already delivered, so its reconnect interval remains
            # chargeable and the original absolute deadline cannot move.
            clock.now += 50
            await hub.register("agent-a", connection_id="connection-b")
            repeated = await hub.next_turn("agent-a", "connection-b")
            assert repeated is not None
            assert repeated.request_id == second.request_id
            assert repeated.deadline_at == second.deadline_at == pytest.approx(1000)
            clock.now += 849.7
            with pytest.raises(LocalAIResponseRejected) as late:
                await hub.submit_response(
                    "agent-a",
                    "connection-b",
                    request_id=repeated.request_id,
                    match_id=repeated.match_id,
                    turn=repeated.turn,
                    output='{"response":{"x":1,"y":1}}',
                )
            assert late.value.reason == "deadline_exceeded"

        connector_task = asyncio.create_task(connector())
        try:
            await MatchRunner(
                _FakeBinaryRunner(), local_ai_hub=hub
            ).run_binaries(
                None,
                "/fake/b",
                game_id="pencil",
                execution_environments=("remote_local", "platform_low"),
                local_agent_ids=("agent-a", None),
                match_id="match-local-cumulative-reconnect",
                time_control_id="pencil_per_side_total_900s_v1",
                on_event=lambda _kind, event: events.append(event),
            )
        except BotDecisionTimeoutError as exc:
            return exc
        finally:
            await connector_task
        raise AssertionError("turn two should exhaust the cumulative clock")

    failure = asyncio.run(scenario())
    assert failure.error_code == "decision_timeout"
    assert [event["type"] for event in events] == [
        "time_used",
        "time_out",
        "technical_incident",
    ]
    assert events[0]["used"] == pytest.approx(0.4)
    assert events[1]["used"] == pytest.approx(900.0)


def test_cumulative_control_rejects_next_decision_after_budget(monkeypatch):
    calls = 0

    async def fake_bot_decide(*_args, on_decision_elapsed, **_kwargs):
        nonlocal calls
        calls += 1
        on_decision_elapsed(900.0)
        return {"response": {"x": 0, "y": 0}}

    async def fake_run_session(_game_id, decide, **_kwargs):
        await decide(0, {})
        return await decide(0, {})

    monkeypatch.setattr(runner_module, "_botzone_decide", fake_bot_decide)
    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    events: list[dict] = []
    with pytest.raises(BotDecisionTimeoutError):
        asyncio.run(
            MatchRunner(_FakeBinaryRunner()).run_binaries(
                "/fake/a",
                "/fake/b",
                game_id="pencil",
                time_control_id="pencil_per_side_total_900s_v1",
                on_event=lambda _kind, event: events.append(event),
            )
        )
    assert calls == 1
    assert [event["type"] for event in events].count("time_out") == 1


def test_human_runner_clock_only_times_bot_and_projects_bot_only(
    monkeypatch,
):
    """Human practice applies the selected clock only to the Bot seat."""

    async def fake_bot_decide(*_args, on_decision_elapsed, **_kwargs):
        on_decision_elapsed(0.2)
        return {"response": {"x": 0, "y": 0}}

    async def fake_run_session(_game_id, decide, *, on_event, **_kwargs):
        on_event("match_start", {"type": "match_start", "game_id": "pencil"})
        for seat in (0, 1, 0, 1):
            await decide(seat, {})
        return object()

    async def human_decide(_seat, _request):
        return {"x": 0, "y": 0}

    monkeypatch.setattr(runner_module, "_botzone_decide", fake_bot_decide)
    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    binary_runner = _FakeBinaryRunner()
    runner = MatchRunner(binary_runner)
    events: list[dict] = []

    asyncio.run(runner.run_bot_vs_human(
        "/fake/bot", bot_seat=0, human_decide=human_decide,
        game_id="pencil", on_event=lambda _kind, ev: events.append(ev),
        time_control_id="pencil_per_side_total_900s_v1",
    ))

    time_used = [event for event in events if event["type"] == "time_used"]
    assert [event["seat"] for event in time_used] == [0, 0]
    assert [event["used"] for event in time_used] == [0.2, 0.4]
    assert [event["remaining"] for event in time_used] == [899.8, 899.6]
    starts = [event for event in events if event["type"] == "match_start"]
    assert starts[0]["time_control"]["applies_to"] == "bot_only"
    assert binary_runner.runtime_ready_calls == 2
    assert binary_runner.stopped == ["session-traditional"]


def test_human_runner_time_control_timeout_can_only_target_bot(monkeypatch):
    """Bot timeout is technical; human moves retain their outer inactivity policy."""

    async def fake_bot_decide(
        *_args, on_decision_elapsed, failed_seat, **_kwargs
    ):
        on_decision_elapsed(1.0)
        raise BotDecisionTimeoutError(
            "timeout",
            error_code="decision_timeout",
            failed_seat=failed_seat,
            turn=1,
        )

    async def fake_run_session(_game_id, decide, **_kwargs):
        await decide(1, {})  # human is not wrapped in the selected 1s control
        return await decide(0, {})

    async def human_decide(_seat, _request):
        return {"x": 0, "y": 0}

    monkeypatch.setattr(runner_module, "_botzone_decide", fake_bot_decide)
    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    binary_runner = _FakeBinaryRunner()
    runner = MatchRunner(binary_runner)
    events: list[dict] = []

    with pytest.raises(BotDecisionTimeoutError):
        asyncio.run(runner.run_bot_vs_human(
            "/fake/bot", bot_seat=0, human_decide=human_decide,
            game_id="pencil", on_event=lambda _kind, ev: events.append(ev),
            time_control_id="pencil_per_decision_1s_v1",
        ))

    timeouts = [event for event in events if event["type"] == "time_out"]
    assert len(timeouts) == 1
    assert timeouts[0]["seat"] == 0
    assert timeouts[0]["budget"] == 1
    assert timeouts[0]["time_control"]["applies_to"] == "bot_only"
    assert binary_runner.stopped == ["session-traditional"]
