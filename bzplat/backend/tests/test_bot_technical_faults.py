"""Bot protocol/timeout faults are terminal, attributable and diagnosable."""
from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.orchestrator import (
    MatchOrchestrator,
    _technical_incident_summary,
)
from bzplat.backend.matches.runner import MatchRunner, _botzone_decide
from bzplat.backend.runtime.binary_runner import (
    BotDecisionTimeoutError,
    BotProtocolError,
    BotTechnicalError,
)
from bzplat.backend.store import Store
from bzplat.backend.store.schema import STATUS_COMPLETED


class _TransportSession:
    def __init__(self, path: str, runtime_mode: str) -> None:
        self.binary_path = path
        self.runtime_mode = runtime_mode
        self.requests: list = []
        self.responses: list = []
        self.turn = 0
        self.long_running = False


class _ScriptedTransport:
    """Minimal BinaryRunner transport; seat 0 receives one scripted outcome."""

    def __init__(self, outcome: str | BaseException) -> None:
        self.outcome = outcome
        self._sessions: dict[str, _TransportSession] = {}
        self._started = 0

    async def start_session(self, path, *, runtime_mode="longrunning", **_kwargs):
        sid = f"s{self._started}"
        self._started += 1
        self._sessions[sid] = _TransportSession(str(path), runtime_mode)
        return sid

    async def prepare_session(self, path, *, runtime_mode):
        sid = f"s{self._started}"
        self._started += 1
        self._sessions[sid] = _TransportSession(str(path), runtime_mode)
        return sid

    async def send(self, sid, _line, *, timeout=None):
        session = self._sessions[sid]
        if sid == "bot" or not session.binary_path.endswith("b.bin"):
            if isinstance(self.outcome, BaseException):
                raise self.outcome
            return self.outcome
        return '{"response":0}'

    async def read_extra_line(self, _sid, *, timeout=1.0):
        return None

    async def stop_session(self, sid):
        self._sessions.pop(sid, None)


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ('{"a":"c"}', "missing_response"),
        ("{}", "missing_response"),
        ("not-json", "invalid_json"),
        ("[]", "invalid_envelope"),
        ('{"response":0,"debug":"retired"}', "unexpected_fields"),
        ('{"response":"0"}', "invalid_response"),
    ],
)
def test_botzone_response_faults_are_not_committed_or_defaulted(line, code):
    transport = _ScriptedTransport(line)
    session = _TransportSession("/private/bot.bin", "longrunning")
    transport._sessions["bot"] = session

    with pytest.raises(BotProtocolError) as raised:
        asyncio.run(
            _botzone_decide(
                transport,
                "bot",
                {"hand": 0},
                game_id="holdem",
                action_timeout=1,
                failed_seat=1,
            )
        )

    assert raised.value.error_code == code
    assert raised.value.failed_seat == 1
    assert raised.value.turn == 1
    assert session.requests == []
    assert session.responses == []
    assert session.turn == 0
    assert "/private" not in str(raised.value)


@pytest.mark.parametrize("runtime_mode", ["longrunning", "traditional"])
def test_botzone_timeout_is_a_typed_terminal_bot_fault(runtime_mode):
    transport = _ScriptedTransport(asyncio.TimeoutError())
    session = _TransportSession("/private/bot.bin", runtime_mode)
    transport._sessions["bot"] = session

    with pytest.raises(BotDecisionTimeoutError) as raised:
        asyncio.run(
            _botzone_decide(
                transport,
                "bot",
                {},
                game_id="gomoku",
                action_timeout=0.01,
                failed_seat=0,
            )
        )

    assert raised.value.reason == "timeout"
    assert raised.value.error_code == "decision_timeout"
    assert raised.value.failed_seat == 0
    assert session.turn == 0


@pytest.mark.parametrize(
    ("game_id", "line"),
    [
        ("holdem", '{"response":{"x":1,"y":2}}'),
        ("gomoku", '{"response":{"x":"1","y":2}}'),
        ("pencil", '{"response":{"x":1}}'),
    ],
)
def test_game_specific_response_shape_faults_are_terminal(game_id, line):
    transport = _ScriptedTransport(line)
    session = _TransportSession("/private/bot.bin", "longrunning")
    transport._sessions["bot"] = session

    with pytest.raises(BotProtocolError) as raised:
        asyncio.run(
            _botzone_decide(
                transport,
                "bot",
                {},
                game_id=game_id,
                action_timeout=1,
                failed_seat=0,
            )
        )
    assert raised.value.error_code == "invalid_response"
    assert session.turn == 0


@pytest.mark.parametrize("game_id", ["holdem", "gomoku", "pencil"])
def test_first_missing_response_stops_every_game_before_fake_completion(game_id):
    events: list[dict] = []
    runner = MatchRunner(_ScriptedTransport("{}"), action_timeout=0.1)

    with pytest.raises(BotProtocolError) as raised:
        asyncio.run(
            runner.run_binaries(
                "/private/a.bin",
                "/private/b.bin",
                game_id=game_id,
                on_event=lambda _kind, event: events.append(event),
            )
        )

    assert raised.value.failed_seat == 0
    incidents = [event for event in events if event["type"] == "technical_incident"]
    assert len(incidents) == 1
    assert incidents[0]["code"] == "missing_response"
    assert not [
        event
        for event in events
        if event.get("type") in {"bot_decide_error", "bot_technical_error"}
    ]
    # The old Hold'em bug emitted 70 settle events after silently folding each hand.
    assert not [event for event in events if event["type"] == "settle"]


def test_protocol_valid_but_game_illegal_move_stays_with_the_judge():
    events: list[dict] = []
    runner = MatchRunner(
        _ScriptedTransport('{"response":{"x":999,"y":999}}'),
        action_timeout=0.1,
    )

    result = asyncio.run(
        runner.run_binaries(
            "/private/a.bin",
            "/private/b.bin",
            game_id="gomoku",
            on_event=lambda _kind, event: events.append(event),
        )
    )

    assert result.reason == "illegal"
    assert result.winner == 1
    assert not [event for event in events if event["type"] == "technical_incident"]


@pytest.fixture
def store(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    db = Store(str(runtime / "test.db"))
    yield db
    db.close()


def _user_bot(store: Store, name: str, game_id: str):
    user = store.create_user(
        name, f"{name}@example.test", hash_password("password1")
    )
    bot = store.create_bot(
        user["id"],
        f"{name}-bot",
        binary_path=f"/private/{name}.bin",
        format="elf",
        game_id=game_id,
        runtime_mode="traditional",
    )
    version = store.add_bot_version(
        bot["id"],
        binary_path=f"/private/{name}-v1.bin",
        version=1,
        runtime_mode="traditional",
    )
    store.set_current_version(bot["id"], 1)
    store.ensure_rating(bot["id"], game_id=game_id)
    return user, bot, version


class _TechnicalRunner:
    def __init__(self, exc: BotTechnicalError, *, repeats: int = 1) -> None:
        self.exc = exc
        self.repeats = repeats

    async def _fail(self, **kwargs):
        on_event = kwargs.get("on_event")
        if on_event is not None:
            for _ in range(self.repeats):
                on_event(
                    "technical_incident",
                    {"type": "technical_incident", **self.exc.incident()},
                )
        raise self.exc

    async def run_binaries(self, *_args, **kwargs):
        return await self._fail(**kwargs)

    async def run_duplicate(self, *_args, **kwargs):
        return await self._fail(**kwargs)

    async def run_bot_vs_human(self, *_args, **kwargs):
        return await self._fail(**kwargs)


def _run_challenge(orch: MatchOrchestrator, bot_a: int, bot_b: int, owner: int, **kwargs):
    async def run():
        match_id = await orch.challenge(bot_a, bot_b, owner, **kwargs)
        task = orch._tasks.get(match_id)
        if task is not None:
            await task
        return match_id

    return asyncio.run(run())


@pytest.mark.parametrize("game_id", ["holdem", "gomoku", "pencil"])
def test_protocol_fault_is_scored_technical_loss_for_every_bot_game(
    store, game_id, caplog
):
    owner, bot_a, _ = _user_bot(store, f"{game_id}a", game_id)
    _, bot_b, version_b = _user_bot(store, f"{game_id}b", game_id)
    exc = BotProtocolError(
        "Bot 响应缺少必填 response 字段",
        error_code="missing_response",
        failed_seat=1,
        turn=7,
    )
    orch = MatchOrchestrator(
        store, runner=_TechnicalRunner(exc), max_concurrent=1
    )

    with caplog.at_level(logging.WARNING):
        match_id = _run_challenge(
            orch,
            bot_a["id"],
            bot_b["id"],
            owner["id"],
            game_id=game_id,
        )

    match = store.get_match(match_id)
    assert match["status"] == STATUS_COMPLETED
    assert match["reason"] == "protocol_error"
    assert match["technical_loss"] == 1
    assert match["winner"] == 0
    assert match["result"]["deltas"] == [1, -1]
    assert match["result"]["technical_incident_count"] == 1
    assert match["result"]["technical_incident_samples"] == [
        {
            "reason": "protocol_error",
            "code": "missing_response",
            "seat": 1,
            "turn": 7,
            "error": "Bot 响应缺少必填 response 字段",
        }
    ]
    replay = json.loads(store.get_replay(match_id)["events_json"])
    assert len([e for e in replay if e.get("type") == "technical_incident"]) == 1
    assert not [
        e
        for e in replay
        if e.get("type") in {"bot_decide_error", "bot_technical_error"}
    ]
    # Attributable technical losses are intentionally scored for non-contest bots.
    assert store.get_rating(bot_a["id"], game_id=game_id)["matches_played"] == 1
    assert store.get_rating(bot_b["id"], game_id=game_id)["matches_played"] == 1
    log_text = caplog.text
    for fragment in (
        f"match_id={match_id}",
        f"bot_id={bot_b['id']}",
        f"version_id={version_b['id']}",
        "runtime=traditional",
        "seat=1",
        "turn=7",
        "code=missing_response",
    ):
        assert fragment in log_text
    assert "/private/" not in log_text


def test_timeout_and_duplicate_faults_are_not_normal_completed_results(store):
    owner, bot_a, _ = _user_bot(store, "dupa", "holdem")
    _, bot_b, _ = _user_bot(store, "dupb", "holdem")
    exc = BotDecisionTimeoutError(
        "Bot 未在决策时限内输出完整响应行",
        error_code="decision_timeout",
        failed_seat=1,
        turn=3,
        leg=1,
    )
    orch = MatchOrchestrator(store, runner=_TechnicalRunner(exc), max_concurrent=1)

    match_id = _run_challenge(
        orch,
        bot_a["id"],
        bot_b["id"],
        owner["id"],
        game_id="holdem",
        duplicate=True,
        duplicate_seed=42,
    )

    match = store.get_match(match_id)
    assert match["status"] == STATUS_COMPLETED
    assert match["reason"] == "timeout"
    assert match["technical_loss"] == 1
    assert match["winner"] == 0
    assert "legs" not in match["result"]
    assert match["result"]["technical_incident_samples"][0]["leg"] == 1


def test_bot_protocol_fault_in_human_match_blames_only_the_bot(store):
    user, bot, _ = _user_bot(store, "humanbot", "gomoku")
    exc = BotProtocolError(
        "Bot 输出不是合法 JSON",
        error_code="invalid_json",
        failed_seat=0,
        turn=1,
    )
    orch = MatchOrchestrator(store, runner=_TechnicalRunner(exc), max_concurrent=1)

    async def run():
        match_id = await orch.challenge_human(
            bot["id"], user["id"], human_seat=1, game_id="gomoku"
        )
        task = orch._tasks[match_id]
        await task
        return match_id

    match_id = asyncio.run(run())
    match = store.get_match(match_id)
    assert match["status"] == STATUS_COMPLETED
    assert match["reason"] == "protocol_error"
    assert match["winner"] == 1  # human seat wins
    assert match["technical_loss"] == 1
    # Human matches never affect the Bot's ladder rating.
    assert store.get_rating(bot["id"], game_id="gomoku")["matches_played"] == 0


def test_technical_incident_result_samples_are_bounded():
    events = [
        {
            "type": "technical_incident",
            "reason": "protocol_error",
            "code": "invalid_json",
            "seat": i % 2,
            "turn": i + 1,
            "error": "safe",
        }
        for i in range(8)
    ]
    summary = _technical_incident_summary(events)
    assert summary["technical_incident_count"] == 8
    assert summary["technical_incidents_by_seat"] == {0: 4, 1: 4}
    assert len(summary["technical_incident_samples"]) == 3
    assert "bot_decide_errors" not in summary


def test_technical_incident_replay_samples_are_bounded(store):
    owner, bot_a, _ = _user_bot(store, "bounda", "gomoku")
    _, bot_b, _ = _user_bot(store, "boundb", "gomoku")
    exc = BotProtocolError(
        "Bot 输出不是合法 JSON",
        error_code="invalid_json",
        failed_seat=0,
        turn=1,
    )
    orch = MatchOrchestrator(
        store, runner=_TechnicalRunner(exc, repeats=8), max_concurrent=1
    )

    match_id = _run_challenge(
        orch,
        bot_a["id"],
        bot_b["id"],
        owner["id"],
        game_id="gomoku",
    )
    match = store.get_match(match_id)
    assert match["result"]["technical_incident_count"] == 8
    assert match["result"]["technical_incidents_by_seat"] == {0: 8, 1: 0}
    # Identical repeated incidents may be deduplicated, but the public sample set
    # must stay non-empty and bounded independently from the authoritative count.
    assert 1 <= len(match["result"]["technical_incident_samples"]) <= 3
    assert "bot_decide_errors" not in match["result"]
    replay = json.loads(store.get_replay(match_id)["events_json"])
    assert len([e for e in replay if e.get("type") == "technical_incident"]) == 3
    assert not [
        e
        for e in replay
        if e.get("type") in {"bot_decide_error", "bot_technical_error"}
    ]
