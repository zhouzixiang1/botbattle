"""Bot protocol/timeout faults are terminal, attributable and diagnosable."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.series import series_rows_settled
from bzplat.backend.contests.validation import SERIES_SCORING_INDEPENDENT
from bzplat.backend.games import registry
from bzplat.backend.matches.orchestrator import (
    MatchOrchestrator,
    _technical_incident_summary,
)
from bzplat.backend.matches.public_outcome import build_public_outcome
from bzplat.backend.matches.runner import MatchRunner, _botzone_decide
from bzplat.backend.runtime.binary_runner import (
    BotCrashedError,
    BotDecisionTimeoutError,
    BotProtocolError,
    BotTechnicalError,
)
from bzplat.backend.runtime.limits import PLATFORM_LOW_PROFILE
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    PUBLIC_MATCH_ERROR_REASONS,
    STATUS_COMPLETED,
    TYPE_LADDER,
)
from bzplat.backend.tests.execution_helpers import (
    challenge_and_start,
    human_and_start,
)
from bzplat.backend.tests._gomoku_v2 import ILLEGAL_OPENING_LINE


class _TransportSession:
    def __init__(
        self,
        path: str,
        runtime_mode: str,
        *,
        profile=PLATFORM_LOW_PROFILE,
        execution_scope=None,
    ) -> None:
        self.binary_path = path
        self.runtime_mode = runtime_mode
        self.profile = profile
        self.execution_scope = execution_scope
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
        self._sessions[sid] = _TransportSession(
            str(path),
            runtime_mode,
            profile=_kwargs.get("profile", PLATFORM_LOW_PROFILE),
            execution_scope=_kwargs.get("execution_scope"),
        )
        return sid

    async def prepare_session(self, path, *, runtime_mode, **_kwargs):
        sid = f"s{self._started}"
        self._started += 1
        self._sessions[sid] = _TransportSession(
            str(path),
            runtime_mode,
            profile=_kwargs.get("profile", PLATFORM_LOW_PROFILE),
            execution_scope=_kwargs.get("execution_scope"),
        )
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

    async def cleanup_execution(self, _execution_scope):
        self._sessions.clear()


@pytest.mark.parametrize(
    ("line", "code"),
    [
        ('{"a":"c"}', "missing_response"),
        ("{}", "missing_response"),
        ("not-json", "invalid_json"),
        ("[]", "invalid_envelope"),
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


def test_botzone_decide_ignores_extra_top_level_fields_and_commits_only_response():
    transport = _ScriptedTransport('{"response":0,"debug":"attachment output"}')
    session = _TransportSession("/private/bot.bin", "traditional")
    transport._sessions["bot"] = session

    move = asyncio.run(
        _botzone_decide(
            transport,
            "bot",
            {"hand": 0},
            game_id="holdem",
            action_timeout=1,
            failed_seat=0,
        )
    )

    assert move == {"response": 0}
    assert session.responses == [0]
    assert session.turn == 1


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


def test_longrunning_send_crash_is_attributed_to_requested_seat():
    crash = BotCrashedError("closed stdin")
    transport = _ScriptedTransport(crash)
    transport._sessions["bot"] = _TransportSession(
        "/private/bot.bin", "longrunning"
    )

    with pytest.raises(BotCrashedError) as raised:
        asyncio.run(
            _botzone_decide(
                transport,
                "bot",
                {},
                game_id="holdem",
                action_timeout=1,
                failed_seat=1,
            )
        )

    assert raised.value is crash
    assert raised.value.crashed_seat == 1


def test_longrunning_handshake_crash_is_attributed_to_requested_seat():
    crash = BotCrashedError("stdout EOF before keep-running handshake")

    class HandshakeCrashTransport(_ScriptedTransport):
        async def read_extra_line(self, _sid, *, timeout=1.0):
            raise crash

    transport = HandshakeCrashTransport('{"response":0}')
    transport._sessions["bot"] = _TransportSession(
        "/private/bot.bin", "longrunning"
    )

    with pytest.raises(BotCrashedError) as raised:
        asyncio.run(
            _botzone_decide(
                transport,
                "bot",
                {},
                game_id="holdem",
                action_timeout=1,
                failed_seat=1,
            )
        )

    assert raised.value is crash
    assert raised.value.crashed_seat == 1


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


def test_holdem_runtime_crash_propagates_out_of_single_game_runner():
    events: list[dict] = []
    runner = MatchRunner(
        _ScriptedTransport(BotCrashedError("runtime eof")),
        action_timeout=0.1,
    )

    with pytest.raises(BotCrashedError) as raised:
        asyncio.run(
            runner.run_binaries(
                "/private/a.bin",
                "/private/b.bin",
                game_id="holdem",
                on_event=lambda _kind, event: events.append(event),
            )
        )

    assert raised.value.crashed_seat == 0
    assert not [event for event in events if event["type"] == "match_end"]
    assert not [event for event in events if event["type"] == "settle"]


def test_holdem_runtime_crash_stops_duplicate_before_second_scoring_game():
    events: list[dict] = []
    runner = MatchRunner(
        _ScriptedTransport(BotCrashedError("runtime eof")),
        action_timeout=0.1,
    )

    with pytest.raises(BotCrashedError) as raised:
        asyncio.run(
            runner.run_duplicate(
                "/private/a.bin",
                "/private/b.bin",
                game_id="holdem",
                seed=42,
                on_event=lambda _kind, event: events.append(event),
            )
        )

    assert raised.value.crashed_seat == 0
    assert raised.value.crashed_leg == 0
    starts = [event for event in events if event["type"] == "match_start"]
    assert starts == [
        {
            "type": "match_start",
            "game_id": "holdem",
            "time_control": {
                "id": "holdem_per_decision_60s_v1",
                "mode": "per_decision",
                "seconds": 60,
                "applies_to": "both_bots",
            },
            "leg": 0,
        }
    ]
    assert not [event for event in events if event.get("leg") == 1]
    assert not [event for event in events if event["type"] == "match_end"]


def test_protocol_valid_but_game_illegal_move_stays_with_the_judge():
    events: list[dict] = []
    runner = MatchRunner(
        _ScriptedTransport(ILLEGAL_OPENING_LINE),
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

    assert result.reason == "illegal_opening"
    assert result.winner == 1
    assert not [event for event in events if event["type"] == "technical_incident"]


@pytest.fixture
def store(tmp_path):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    db = Store(str(runtime / "test.db"))
    yield db
    db.close()


def _user_bot(
    store: Store,
    name: str,
    game_id: str,
    *,
    runtime_mode: str = "traditional",
):
    fixture_dir = Path(store.path).resolve().parent / "bot-fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    base_path = fixture_dir / f"{name}.bin"
    version_path = fixture_dir / f"{name}-v1.bin"
    base_path.write_bytes(b"test fixture")
    version_path.write_bytes(b"test fixture")
    user = store.create_user(
        name, f"{name}@example.test", hash_password("password1")
    )
    bot = store.create_bot(
        user["id"],
        f"{name}-bot",
        binary_path=str(base_path),
        format="elf",
        game_id=game_id,
        runtime_mode=runtime_mode,
    )
    version = store.add_bot_version(
        bot["id"],
        binary_path=str(version_path),
        version=1,
        runtime_mode=runtime_mode,
    )
    store.set_current_version(bot["id"], 1)
    store.select_ranked_bot(int(user["id"]), int(bot["id"]), if_empty=True)
    store.ensure_rating(bot["id"], game_id=game_id)
    return user, bot, version


_PAIRING_PUBLISHED_AT = "2026-01-01T00:00:00"


def _activate_single_contest_pairing(
    store: Store,
    contest: dict,
    entry_a: dict,
    entry_b: dict,
    version_a: dict,
    version_b: dict,
) -> dict:
    """Install a complete one-pair batch before low-level execution tests bind."""
    rows = store.create_contest_stage_pairings(
        contest["id"],
        0,
        [
            {
                "entry_a_id": entry_a["id"],
                "entry_b_id": entry_b["id"],
                "bot_a_id": entry_a["bot_id"],
                "bot_b_id": entry_b["bot_id"],
                "bot_a_version_id": version_a["id"],
                "bot_b_version_id": version_b["id"],
                "round_num": 1,
                "stage_key": "rr",
                "series_index": 1,
                "series_size": 1,
                "published_at": _PAIRING_PUBLISHED_AT,
            }
        ],
        expected_current_stage_idx=0,
        expected_status="published",
        activate_running=True,
    )
    assert len(rows) == 1
    assert store.contest_stage_manifest_is_valid(contest["id"], 0)
    return rows[0]


def _replace_imported_active_stage_and_reseal(
    store: Store, contest_id: int, stage: dict
) -> None:
    """Inject one malformed frozen stage without making stale seal the failure."""
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        before = connection.execute(
            "SELECT status,current_stage_idx,published_stage_pairing_count,"
            "pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest_id,),
        ).fetchone()
        assert before is not None
        assert before["status"] == "running"
        assert before["current_stage_idx"] == 0
        assert before["published_stage_pairing_count"] == 1
        assert (
            before["pairing_topology_revision"]
            == before["sealed_pairing_topology_revision"]
        )
        changed = connection.execute(
            "UPDATE contests SET stages_json=? WHERE id=? AND status='running' "
            "AND current_stage_idx=0 AND pairing_topology_revision=? "
            "AND sealed_pairing_topology_revision=?",
            (
                json.dumps([stage]),
                contest_id,
                before["pairing_topology_revision"],
                before["sealed_pairing_topology_revision"],
            ),
        )
        assert changed.rowcount == 1
        after_revision = connection.execute(
            "SELECT pairing_topology_revision FROM contests WHERE id=?",
            (contest_id,),
        ).fetchone()["pairing_topology_revision"]
        resealed = connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision=? "
            "WHERE id=? AND status='running' AND current_stage_idx=0 "
            "AND published_stage_pairing_count=1 "
            "AND pairing_topology_revision=? "
            "AND sealed_pairing_topology_revision=?",
            (
                after_revision,
                contest_id,
                after_revision,
                before["sealed_pairing_topology_revision"],
            ),
        )
        assert resealed.rowcount == 1
    assert store.contest_stage_manifest_is_valid(contest_id, 0)


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
        on_event = kwargs.get("on_event")
        if on_event is not None:
            leg = self.exc.leg if self.exc.leg is not None else 0
            on_event(
                "match_start",
                {"type": "match_start", "game_id": "holdem", "leg": leg},
            )
        return await self._fail(**kwargs)

    async def run_bot_vs_human(self, *_args, **kwargs):
        return await self._fail(**kwargs)


class _SecondGameTechnicalRunner(_TechnicalRunner):
    """Emit one complete game plus a partial second game before failing."""

    async def run_duplicate(self, *_args, **kwargs):
        on_event = kwargs.get("on_event")
        assert on_event is not None
        on_event(
            "match_start",
            {"type": "match_start", "game_id": "holdem", "leg": 0},
        )
        for hand in range(70):
            on_event("settle", {"type": "settle", "hand": hand, "leg": 0})
        on_event(
            "match_start",
            {"type": "match_start", "game_id": "holdem", "leg": 1},
        )
        for hand in range(5):
            on_event("settle", {"type": "settle", "hand": hand, "leg": 1})
        return await self._fail(**kwargs)


class _NeverStartInvalidContractRunner:
    """Guard that records any attempt to start an invalid frozen plan."""

    def __init__(self) -> None:
        self.calls = 0
        self.action_timeout = 1.0

    async def run_binaries(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("invalid contest contract must not start a Bot")

    async def run_duplicate(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("invalid contest contract must not start a Bot")


def _run_challenge(orch: MatchOrchestrator, bot_a: int, bot_b: int, owner: int, **kwargs):
    async def run():
        match_id = await challenge_and_start(
            orch, bot_a, bot_b, owner, **kwargs
        )
        task = orch._tasks.get(match_id)
        if task is not None:
            await task
        return match_id

    return asyncio.run(run())


@pytest.mark.parametrize(
    ("stage_patch", "match_duplicate"),
    [
        ({"duplicate": False}, "false"),
        ({"duplicate": False}, True),
        ({"duplicate": "false"}, False),
        ({"duplicate": False, "series_scoring": "unknown"}, False),
    ],
)
def test_strict_contest_execution_rejects_malformed_or_mismatched_duplicate_plan(
    store, stage_patch, match_duplicate
):
    """Execution must agree with the validated stage before any Bot starts."""
    owner, bot_a, version_a = _user_bot(store, "contracta", "holdem")
    other, bot_b, version_b = _user_bot(store, "contractb", "holdem")
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "games_per_pair": 1,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        **stage_patch,
    }
    bindable_stage = {
        **stage,
        "duplicate": False,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
    }
    contest = store.create_contest(
        "Execution contract",
        owner["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([bindable_stage]),
    )
    entry_a = store.add_contest_entry(
        contest["id"], owner["id"], bot_a["id"]
    )
    entry_b = store.add_contest_entry(
        contest["id"], other["id"], bot_b["id"]
    )
    pairing = _activate_single_contest_pairing(
        store, contest, entry_a, entry_b, version_a, version_b
    )
    match = store.create_match(
        f"invalid-contract-{pairing['id']}",
        bot_a["id"],
        bot_b["id"],
        owner_id=owner["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
        match_config={
            "duplicate": match_duplicate,
            "_bot_a_version_id": version_a["id"],
            "_bot_b_version_id": version_b["id"],
        },
    )
    store.bind_contest_pairing_match(
        contest["id"],
        pairing["id"],
        match["id"],
        require_execution_admission=False,
    )
    if bindable_stage != stage:
        # Simulate a damaged/imported active snapshot after a once-valid bind;
        # reseal the exact injected revision so this test cannot pass merely
        # because the lifecycle seal became stale.
        _replace_imported_active_stage_and_reseal(store, contest["id"], stage)
    runner = _NeverStartInvalidContractRunner()
    orchestrator = MatchOrchestrator(store, runner=runner, max_concurrent=1)

    asyncio.run(
        orchestrator._MatchOrchestrator__run_match_inner(match["id"])
    )

    persisted = store.get_match(match["id"])
    assert persisted["status"] == "aborted"
    assert persisted["reason"] == "invalid_match_config"
    assert runner.calls == 0


@pytest.mark.parametrize(
    "corruption",
    ["match_type", "contest_id", "bot_seats", "version"],
)
def test_linked_contest_identity_drift_aborts_before_any_bot_session(
    store, corruption
):
    owner, bot_a, version_a = _user_bot(store, f"identity-a-{corruption}", "holdem")
    other, bot_b, version_b = _user_bot(store, f"identity-b-{corruption}", "holdem")
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "games_per_pair": 1,
        "duplicate": False,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
    }
    contest = store.create_contest(
        f"Execution identity {corruption}",
        owner["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    entry_a = store.add_contest_entry(contest["id"], owner["id"], bot_a["id"])
    entry_b = store.add_contest_entry(contest["id"], other["id"], bot_b["id"])
    pairing = _activate_single_contest_pairing(
        store, contest, entry_a, entry_b, version_a, version_b
    )
    match_id = f"identity-drift-{corruption}"
    store.create_match(
        match_id,
        bot_a["id"],
        bot_b["id"],
        owner_id=owner["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
        match_config={
            "duplicate": False,
            "_bot_a_version_id": version_a["id"],
            "_bot_b_version_id": version_b["id"],
        },
    )
    store.bind_contest_pairing_match(
        contest["id"],
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    current_config = dict(store.get_match(match_id)["match_config"])
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if corruption == "match_type":
            connection.execute(
                "UPDATE matches_holdem SET match_type='challenge' WHERE id=?",
                (match_id,),
            )
        elif corruption == "contest_id":
            connection.execute(
                "UPDATE matches_holdem SET contest_id=NULL WHERE id=?",
                (match_id,),
            )
        elif corruption == "bot_seats":
            connection.execute(
                "UPDATE matches_holdem SET bot_a_id=?,bot_b_id=? WHERE id=?",
                (bot_b["id"], bot_a["id"], match_id),
            )
        else:
            current_config["_bot_a_version_id"] = version_a["id"] + 10_000
            connection.execute(
                "UPDATE matches_holdem SET match_config=? WHERE id=?",
                (json.dumps(current_config), match_id),
            )

    runner = _NeverStartInvalidContractRunner()
    orchestrator = MatchOrchestrator(store, runner=runner, max_concurrent=1)
    asyncio.run(
        orchestrator._MatchOrchestrator__run_match_inner(match_id)
    )
    persisted = store.get_match(match_id)
    assert persisted["status"] == "aborted"
    assert persisted["reason"] == "invalid_match_config"
    assert runner.calls == 0


@pytest.mark.parametrize(
    ("tag", "series_scoring", "expected_deltas"),
    [
        ("independent", SERIES_SCORING_INDEPENDENT, [0, 0]),
        ("aggregate", "aggregate_match_points_v1", [1, -1]),
    ],
)
def test_contest_technical_margin_is_neutral_only_for_independent_v1(
    store, tag, series_scoring, expected_deltas
):
    owner, bot_a, version_a = _user_bot(
        store, f"contest-tech-a-{tag}", "holdem"
    )
    other, bot_b, version_b = _user_bot(
        store, f"contest-tech-b-{tag}", "holdem"
    )
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "games_per_pair": 1,
        "duplicate": False,
        "series_scoring": series_scoring,
    }
    contest = store.create_contest(
        f"Contest technical {series_scoring}",
        owner["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    entry_a = store.add_contest_entry(contest["id"], owner["id"], bot_a["id"])
    entry_b = store.add_contest_entry(contest["id"], other["id"], bot_b["id"])
    pairing = _activate_single_contest_pairing(
        store, contest, entry_a, entry_b, version_a, version_b
    )
    match_id = f"contest-technical-{pairing['id']}"
    store.create_match(
        match_id,
        bot_a["id"],
        bot_b["id"],
        owner_id=owner["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
        match_config={
            "duplicate": False,
            "_bot_a_version_id": version_a["id"],
            "_bot_b_version_id": version_b["id"],
        },
    )
    store.bind_contest_pairing_match(
        contest["id"],
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    exc = BotProtocolError(
        "Bot response is invalid",
        error_code="invalid_json",
        failed_seat=1,
        turn=1,
    )
    orchestrator = MatchOrchestrator(
        store, runner=_TechnicalRunner(exc), max_concurrent=1
    )

    asyncio.run(
        orchestrator._MatchOrchestrator__run_match_inner(match_id)
    )

    match = store.get_match(match_id)
    assert match["status"] == STATUS_COMPLETED
    assert match["winner"] == 0
    assert match["result"]["deltas"] == expected_deltas
    standings = ContestManager(store, _NeverStartInvalidContractRunner()).standings(
        contest["id"], stage_idx=0
    )
    assert [(row["points"], row["delta_total"]) for row in standings] == [
        (3.0, expected_deltas[0]),
        (0.0, expected_deltas[1]),
    ]


def test_ladder_technical_margin_preserves_rating_delta_total(store):
    owner, bot_a, _ = _user_bot(store, "ladder-tech-a", "holdem")
    _, bot_b, _ = _user_bot(store, "ladder-tech-b", "holdem")
    exc = BotProtocolError(
        "Bot response is invalid",
        error_code="invalid_json",
        failed_seat=1,
        turn=1,
    )
    orchestrator = MatchOrchestrator(
        store, runner=_TechnicalRunner(exc), max_concurrent=1
    )

    match_id = _run_challenge(
        orchestrator,
        bot_a["id"],
        bot_b["id"],
        owner["id"],
        game_id="holdem",
        match_type=TYPE_LADDER,
    )

    assert store.get_match(match_id)["result"]["deltas"] == [1, -1]
    assert store.get_rating(bot_a["id"], game_id="holdem")["delta_total"] == 1
    assert store.get_rating(bot_b["id"], game_id="holdem")["delta_total"] == -1


@pytest.mark.parametrize("duplicate", [False, True])
def test_holdem_runtime_crash_persists_one_authoritative_technical_game(
    store, duplicate
):
    owner, bot_a, _ = _user_bot(store, f"crasha{int(duplicate)}", "holdem")
    _, bot_b, _ = _user_bot(store, f"crashb{int(duplicate)}", "holdem")
    runner = MatchRunner(
        _ScriptedTransport(BotCrashedError("runtime eof")),
        action_timeout=0.1,
    )
    orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)

    match_id = _run_challenge(
        orch,
        bot_a["id"],
        bot_b["id"],
        owner["id"],
        game_id="holdem",
        duplicate=duplicate,
        duplicate_seed=42 if duplicate else None,
    )

    match = store.get_match(match_id)
    assert match["status"] == STATUS_COMPLETED
    assert match["reason"] == "technical_loss"
    assert match["technical_loss"] == 1
    assert match["winner"] == 1
    # A Bot-attributable dead transport must settle only this match.  It must
    # never take the generic internal-error branch that pauses the dispatcher
    # and later turns unrelated attempts into any legacy/current orphan reason.
    control = store.executions.control()
    assert control["dispatcher_state"] == "running"
    assert not str(control.get("pause_reason") or "")
    execution = store.executions.get_by_match(match_id)
    assert execution is not None
    # This legacy scripted transport has no physical cleanup proof callback,
    # but the match body itself must not mark recovery pending.
    assert not str(execution.get("last_error") or "")
    stable_orphan_reasons = {
        reason
        for reason in PUBLIC_MATCH_ERROR_REASONS
        if reason.startswith("orphan_")
    }
    assert not [
        item
        for item in store.list_matches(limit=100)
        if item.get("reason") in stable_orphan_reasons
    ]
    assert match["result"]["rounds_played"] == 0
    assert match["result"]["deltas"] == [-1, 1]
    assert "legs" not in match["result"]
    outcome = build_public_outcome(match, registry.get("holdem"))
    assert outcome is not None
    assert outcome["kind"] == ("duplicate" if duplicate else "single")
    assert outcome["planned_games"] == (2 if duplicate else 1)
    assert outcome["completed_games"] == 1
    assert outcome["score"] == {"wins_a": 0, "draws": 0, "wins_b": 1}
    replay = json.loads(store.get_replay(match_id)["events_json"])
    if duplicate:
        assert replay[0] == {
            "type": "match_start",
            "game_id": "holdem",
            "time_control": {
                "id": "holdem_per_decision_60s_v1",
                "mode": "per_decision",
                "seconds": 60,
                "applies_to": "both_bots",
            },
            "leg": 0,
        }
        assert replay[-1]["leg"] == 0
        assert not [event for event in replay if event.get("leg") == 1]


@pytest.mark.parametrize("duplicate", [False, True])
def test_longrunning_seat_one_crash_persists_correct_winner(store, duplicate):
    owner, bot_a, _ = _user_bot(
        store,
        f"longrunning-good-{int(duplicate)}",
        "holdem",
        runtime_mode="longrunning",
    )
    _, bot_b, _ = _user_bot(
        store,
        f"longrunning-crash-{int(duplicate)}",
        "holdem",
        runtime_mode="longrunning",
    )

    class SeatOneCrashTransport(_ScriptedTransport):
        async def send(self, sid, _line, *, timeout=None):
            session = self._sessions[sid]
            if "longrunning-crash" in Path(session.binary_path).name:
                raise BotCrashedError("closed stdin")
            return '{"response":0}'

        async def read_extra_line(self, _sid, *, timeout=1.0):
            return ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"

    orch = MatchOrchestrator(
        store,
        runner=MatchRunner(SeatOneCrashTransport("unused"), action_timeout=0.1),
        max_concurrent=1,
    )

    match_id = _run_challenge(
        orch,
        bot_a["id"],
        bot_b["id"],
        owner["id"],
        game_id="holdem",
        duplicate=duplicate,
        duplicate_seed=42 if duplicate else None,
    )

    match = store.get_match(match_id)
    assert match["status"] == STATUS_COMPLETED
    assert match["reason"] == "technical_loss"
    assert match["technical_loss"] == 1
    assert match["winner"] == 0
    assert match["result"]["deltas"] == [1, -1]
    assert store.executions.control()["dispatcher_state"] == "running"
    execution = store.executions.get_by_match(match_id)
    assert execution is not None
    assert not str(execution.get("last_error") or "")


def test_unattributed_bot_crash_is_never_scored_as_seat_zero_loss(store):
    owner, bot_a, _ = _user_bot(store, "unattributed-a", "holdem")
    _, bot_b, _ = _user_bot(store, "unattributed-b", "holdem")

    class UnattributedCrashRunner:
        async def run_binaries(self, *_args, **_kwargs):
            raise BotCrashedError("missing seat attribution")

    orch = MatchOrchestrator(
        store, runner=UnattributedCrashRunner(), max_concurrent=1
    )
    match_id = _run_challenge(
        orch,
        bot_a["id"],
        bot_b["id"],
        owner["id"],
        game_id="holdem",
    )

    match = store.get_match(match_id)
    assert match["status"] == "running"
    assert match["winner"] is None
    assert match["technical_loss"] == 0
    control = store.executions.control()
    assert control["dispatcher_state"] == "paused"
    execution = store.executions.get_by_match(match_id)
    assert execution is not None
    assert "归责" in str(execution["last_error"])


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
    assert store.get_rating(bot_a["id"], game_id=game_id)["delta_total"] == 1
    assert store.get_rating(bot_b["id"], game_id=game_id)["delta_total"] == -1
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
    replay = json.loads(store.get_replay(match_id)["events_json"])
    assert replay[0]["type"] == "match_start"
    assert replay[0]["leg"] == 1
    assert replay[-1]["type"] == "match_end"
    assert replay[-1]["leg"] == 1


def test_duplicate_second_game_technical_progress_is_independent_and_scoreable(store):
    owner, bot_a, _ = _user_bot(store, "duppartiala", "holdem")
    other, bot_b, _ = _user_bot(store, "duppartialb", "holdem")
    exc = BotDecisionTimeoutError(
        "Bot 未在决策时限内输出完整响应行",
        error_code="decision_timeout",
        failed_seat=1,
        turn=6,
        leg=1,
    )
    orch = MatchOrchestrator(
        store, runner=_SecondGameTechnicalRunner(exc), max_concurrent=1
    )

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
    assert match["technical_loss"] == 1
    assert match["winner"] == 0
    # The first game's 70 hands remain in replay, but are not borrowed by the
    # authoritative technical scoring record for game 2.
    assert match["result"]["rounds_played"] == 5
    assert match["result"]["deltas"] == [1, -1]
    assert "legs" not in match["result"]
    outcome = build_public_outcome(match, registry.get("holdem"))
    assert outcome is not None
    assert outcome["completed_games"] == 1
    assert outcome["rounds_played"] == 5
    assert outcome["score"] == {"wins_a": 1, "draws": 0, "wins_b": 0}
    assert outcome["games"][0]["index"] == 2
    replay = json.loads(store.get_replay(match_id)["events_json"])
    assert [event["leg"] for event in replay if event["type"] == "match_start"] == [0, 1]
    assert replay[-1]["type"] == "match_end"
    assert replay[-1]["leg"] == 1

    stage = {
        "key": "rr",
        "type": "round_robin",
        "games_per_pair": 1,
        "duplicate": True,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
    }
    strict_result = {
        **match["result"],
        "deltas": [0, 0],
        "normalized_delta": 0,
    }
    strict_match = {
        **match,
        "contest_id": 99,
        "game_id": "holdem",
        "match_type": "contest",
        "result": strict_result,
    }
    pairing = {
        "entry_a_id": 11,
        "entry_b_id": 22,
        "match_id": match_id,
        "round_num": 1,
        "series_index": 1,
        "series_size": 1,
        "match_status": "completed",
        "match_winner": 0,
        # ContestManager's public snapshot path now validates the complete
        # durable pairing↔Match identity before it consumes a result.  This
        # test runs the technical engine through a generic challenge first, so
        # provide the production-shaped frozen contest projection explicitly
        # for the standings half of the assertion.
        "contest_id": 99,
        "bot_a_id": bot_a["id"],
        "bot_b_id": bot_b["id"],
        "bot_a_version_id": match["match_config"]["_bot_a_version_id"],
        "bot_b_version_id": match["match_config"]["_bot_b_version_id"],
        "_raw_entry_a_id": 11,
        "_raw_entry_b_id": 22,
        "_explicit_series_marker": 1,
        "_entry_a_user_id": owner["id"],
        "_entry_b_user_id": other["id"],
        "_pairing_bot_a_owner_id": owner["id"],
        "_pairing_bot_b_owner_id": other["id"],
        "_match_result_json": strict_result,
        "_match_config_json": match["match_config"],
        "_match_reason": match["reason"],
        "_match_technical_loss": 1,
        "_match_contest_id": 99,
        "_match_game_id": "holdem",
        "_match_type": "contest",
        "_match_bot_a_id": bot_a["id"],
        "_match_bot_b_id": bot_b["id"],
    }
    assert series_rows_settled(
        stage,
        [pairing],
        lambda current_id: strict_match if current_id == match_id else None,
        game_spec=registry.get("holdem"),
    ) is True
    standings = ContestManager(None, None).standings(
        99,
        contest={
            "id": 99,
            "game_id": "holdem",
            "current_stage_idx": 0,
            "stages_json": [stage],
        },
        entries=[
            {"id": 11, "bot_id": bot_a["id"], "user_id": owner["id"]},
            {"id": 22, "bot_id": bot_b["id"], "user_id": other["id"]},
        ],
        pairings=[pairing],
    )
    by_entry = {row["entry_id"]: row for row in standings}
    assert (by_entry[11]["points"], by_entry[22]["points"]) == (3, 0)
    assert by_entry[11]["counts"] == {
        "encounter_groups": 1,
        "unique_opponents": 1,
        "match_jobs": 1,
        "scoring_games": 1,
    }


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
        match_id = await human_and_start(
            orch,
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
    assert match["result"]["deltas"] == [-1, 1]
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
