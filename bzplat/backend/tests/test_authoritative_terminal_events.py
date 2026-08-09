"""Live terminal events are emitted once, after the authoritative DB result."""
from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from starlette.websockets import WebSocketDisconnect

from bzplat.backend.api_routes import match_detail
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import (
    BotCrashedError,
    BotProtocolError,
    PlatformRunnerError,
)
from bzplat.backend.store import Store


REPO_ROOT = Path(__file__).resolve().parents[3]


def _user_bot(store: Store, name: str, *, game_id: str = "holdem"):
    fixture_dir = Path(store.path).resolve().parent / "bot-fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    base_path = fixture_dir / f"{name}.bin"
    version_path = fixture_dir / f"{name}-v1.bin"
    base_path.write_bytes(b"test fixture")
    version_path.write_bytes(b"test fixture")
    user = store.create_user(
        name,
        f"{name}@example.test",
        hash_password("password1"),
    )
    bot = store.create_bot(
        user["id"],
        f"{name}-bot",
        binary_path=str(base_path),
        format="elf",
        game_id=game_id,
        runtime_mode="traditional",
    )
    store.add_bot_version(
        bot["id"],
        binary_path=str(version_path),
        version=1,
        runtime_mode="traditional",
    )
    store.set_current_version(bot["id"], 1)
    store.ensure_rating(bot["id"], game_id=game_id)
    return user, bot


def _normal_result(*, deltas: tuple[int, int] = (37, -37), winner: int = 0):
    engine_end = {
        "type": "match_end",
        "hands_played": 1,
        "final_chips": list(deltas),
        # Hold'em's game replay event is deliberately not the platform winner.
        "winner": None,
        "reason": None,
    }
    result = SimpleNamespace(
        rounds_played=1,
        rounds=[SimpleNamespace(deltas=list(deltas))],
        events=[engine_end],
        winner=winner,
        reason=None,
    )
    return result, engine_end


class _ActualHoldemRunner:
    async def run_binaries(self, *_args, **kwargs):
        # Exercise the real 70-hand Hold'em engine. Both players always
        # check/call; the fixed seed produces a deterministic non-draw result.
        return await MatchRunner().run_callables(
            lambda _request: 0,
            lambda _request: 0,
            game_id="holdem",
            on_event=kwargs["on_event"],
            seed=20260809,
        )


class _DuplicateEngineTerminalRunner:
    async def run_duplicate(self, *_args, **kwargs):
        on_event = kwargs["on_event"]
        legs = [
            {"winner": 0, "deltas": [9, -9]},
            {"winner": 1, "deltas": [-4, 4]},
        ]
        events: list[dict] = []
        for leg, leg_result in enumerate(legs):
            start = {"type": "match_start", "leg": leg}
            end = {
                "type": "match_end",
                "leg": leg,
                "winner": leg_result["winner"],
                "reason": "completed",
            }
            events.extend((start, end))
            on_event("match_start", start)
            on_event("match_end", end)
        return SimpleNamespace(
            rounds_played=2,
            rounds=[SimpleNamespace(deltas=leg["deltas"]) for leg in legs],
            events=events,
            winner=None,
            legs=legs,
        )


class _FailureRunner:
    def __init__(self, error: BaseException) -> None:
        self.error = error

    async def run_binaries(self, *_args, **kwargs):
        on_event = kwargs.get("on_event")
        if isinstance(self.error, BotProtocolError) and on_event is not None:
            on_event(
                "technical_incident",
                {"type": "technical_incident", **self.error.incident()},
            )
        raise self.error


def _api_request(store: Store, match_id: str) -> Request:
    app = SimpleNamespace(state=SimpleNamespace(store=store))
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": f"/api/matches/{match_id}",
            "headers": [],
            "app": app,
        }
    )


async def _run_prepared_match(
    store: Store,
    orch: MatchOrchestrator,
    bot_a: int,
    bot_b: int,
    owner: int,
    *,
    duplicate: bool = False,
) -> tuple[str, list[dict], list[dict]]:
    match_id = await orch.challenge(
        bot_a,
        bot_b,
        owner,
        game_id="holdem",
        duplicate=duplicate,
        duplicate_seed=42 if duplicate else None,
        defer_start=True,
    )
    queue = orch.subscribe(match_id)
    assert queue.get_nowait()["type"] == "snapshot"
    observations: list[dict] = []
    original_broadcast = orch._broadcast

    def record_broadcast(mid: str, event: dict) -> None:
        if event.get("type") in {"match_end", "error"}:
            api = match_detail(mid, _api_request(store, mid))
            observations.append(
                {
                    "event": dict(event),
                    "store": store.get_match(mid),
                    "api": api["match"],
                }
            )
        original_broadcast(mid, event)

    orch._broadcast = record_broadcast  # type: ignore[method-assign]
    orch.start_prepared_match(match_id)
    task = orch._tasks[match_id]
    await task
    live_events: list[dict] = []
    while not queue.empty():
        live_events.append(queue.get_nowait())
    return match_id, live_events, observations


def test_real_holdem_live_terminal_is_single_canonical_event_after_api_commit(tmp_path):
    store = Store(str(tmp_path / "terminal.db"))
    owner, bot_a = _user_bot(store, "terminal-a")
    _, bot_b = _user_bot(store, "terminal-b")
    orch = MatchOrchestrator(store, runner=_ActualHoldemRunner(), max_concurrent=1)

    match_id, live_events, observations = asyncio.run(
        _run_prepared_match(
            store,
            orch,
            bot_a["id"],
            bot_b["id"],
            owner["id"],
        )
    )

    terminals = [event for event in live_events if event.get("type") == "match_end"]
    assert terminals == [
        {
            "type": "match_end",
            "winner": 0,
            "reason": "completed",
            "deltas": [400, -400],
        }
    ]
    assert set(terminals[0]) == {"type", "winner", "reason", "deltas"}
    assert len(observations) == 1
    for source in (observations[0]["store"], observations[0]["api"]):
        assert source["status"] == "completed"
        assert source["winner"] == 0
        assert source["result"]["deltas"] == [400, -400]

    match = store.get_match(match_id)
    assert match["winner"] is not None
    assert match["result"]["deltas"] == [400, -400]
    assert sum(match["result"]["deltas"]) == 0
    replay = json.loads(store.get_replay(match_id)["events_json"])
    replay_ends = [event for event in replay if event.get("type") == "match_end"]
    assert replay_ends == terminals
    assert set(replay_ends[0]) == {"type", "winner", "reason", "deltas"}
    store.close()


def test_duplicate_leg_terminals_stay_internal_and_public_replay_closes_once(tmp_path):
    store = Store(str(tmp_path / "duplicate-terminal.db"))
    owner, bot_a = _user_bot(store, "duplicate-terminal-a")
    _, bot_b = _user_bot(store, "duplicate-terminal-b")
    orch = MatchOrchestrator(
        store,
        runner=_DuplicateEngineTerminalRunner(),
        max_concurrent=1,
    )

    match_id, live_events, observations = asyncio.run(
        _run_prepared_match(
            store,
            orch,
            bot_a["id"],
            bot_b["id"],
            owner["id"],
            duplicate=True,
        )
    )

    terminals = [event for event in live_events if event.get("type") == "match_end"]
    assert terminals == [
        {
            "type": "match_end",
            "winner": None,
            "reason": "completed",
            "deltas": [5, -5],
        }
    ]
    assert observations[0]["store"]["status"] == "completed"
    assert observations[0]["api"]["result"]["deltas"] == [5, -5]
    assert len(observations[0]["api"]["result"]["legs"]) == 2
    replay = json.loads(store.get_replay(match_id)["events_json"])
    assert [event for event in replay if event.get("type") == "match_end"] == terminals
    store.close()


@pytest.mark.parametrize(
    ("error", "expected_type", "expected_status", "expected_reason", "expected_deltas"),
    [
        (
            BotProtocolError(
                "Bot response is invalid",
                error_code="invalid_json",
                failed_seat=0,
                turn=1,
            ),
            "match_end",
            "completed",
            "protocol_error",
            [-1, 1],
        ),
        (
            BotCrashedError("Bot exited", crashed_seat=1),
            "match_end",
            "completed",
            "technical_loss",
            [1, -1],
        ),
        (
            PlatformRunnerError("sandbox unavailable"),
            "error",
            "aborted",
            "platform_error",
            None,
        ),
    ],
)
def test_failure_terminal_is_broadcast_only_after_its_persisted_state(
    tmp_path,
    error,
    expected_type,
    expected_status,
    expected_reason,
    expected_deltas,
):
    store = Store(str(tmp_path / f"failure-{expected_reason}.db"))
    owner, bot_a = _user_bot(store, f"failure-{expected_reason}-a")
    _, bot_b = _user_bot(store, f"failure-{expected_reason}-b")
    orch = MatchOrchestrator(store, runner=_FailureRunner(error), max_concurrent=1)

    _, live_events, observations = asyncio.run(
        _run_prepared_match(
            store,
            orch,
            bot_a["id"],
            bot_b["id"],
            owner["id"],
        )
    )

    terminal = [
        event
        for event in live_events
        if event.get("type") in {"match_end", "error"}
    ]
    assert len(terminal) == 1
    assert terminal[0]["type"] == expected_type
    assert len(observations) == 1
    assert observations[0]["store"]["status"] == expected_status
    assert observations[0]["api"]["status"] == expected_status
    assert observations[0]["store"]["reason"] == expected_reason
    if expected_deltas is not None:
        assert terminal[0] == {
            "type": "match_end",
            "winner": observations[0]["store"]["winner"],
            "reason": expected_reason,
            "deltas": expected_deltas,
        }
        assert observations[0]["api"]["result"]["deltas"] == expected_deltas
        match_id = observations[0]["store"]["id"]
        replay = json.loads(store.get_replay(match_id)["events_json"])
        assert [
            event for event in replay if event.get("type") == "match_end"
        ] == terminal
    store.close()


def test_human_websocket_gets_one_canonical_terminal_after_completed_api(tmp_path):
    result, engine_end = _normal_result(deltas=(23, -23), winner=0)

    class ControlledHumanRunner:
        def __init__(self) -> None:
            self.engine_end_emitted = threading.Event()
            self.release_result = threading.Event()

        async def run_bot_vs_human(self, *_args, **kwargs):
            on_event = kwargs["on_event"]
            on_event("match_start", {"type": "match_start", "num_hands": 1})
            on_event("match_end", engine_end)
            self.engine_end_emitted.set()
            while not self.release_result.is_set():
                await asyncio.sleep(0.005)
            return result

    app = create_app(db_path=str(tmp_path / "human-terminal.db"))
    runner = ControlledHumanRunner()
    app.state.orch.runner = runner
    store = app.state.store
    user, bot = _user_bot(store, "human-terminal")
    store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate("human-terminal", "password1")

    with TestClient(app) as client:
        created = client.post(
            "/api/matches/human",
            headers={"Authorization": f"Bearer {token}"},
            json={"bot_id": bot["id"], "human_seat": 1, "game_id": "holdem"},
        )
        assert created.status_code == 200, created.text
        match_id = created.json()["match_id"]
        assert runner.engine_end_emitted.wait(timeout=2)

        with client.websocket_connect(
            f"/api/matches/{match_id}/play?token={token}"
        ) as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["match"]["status"] == "running"
            # The game terminal is held in memory until the authoritative row is
            # committed, so reconnect cannot close on a running snapshot.
            assert not [
                event
                for event in snapshot["events"]
                if event.get("type") == "match_end"
            ]

            runner.release_result.set()
            terminal = websocket.receive_json()
            assert terminal == {
                "type": "match_end",
                "winner": 0,
                "reason": "completed",
                "deltas": [23, -23],
            }

            detail = client.get(f"/api/matches/{match_id}")
            assert detail.status_code == 200, detail.text
            body = detail.json()
            assert body["match"]["status"] == "completed"
            assert body["match"]["winner"] == 0
            assert body["match"]["result"]["deltas"] == [23, -23]
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1000

        final_detail = client.get(f"/api/matches/{match_id}").json()
        replay = json.loads(final_detail["replay"]["events_json"])
        assert [
            event for event in replay if event.get("type") == "match_end"
        ] == [terminal]


def test_frontend_live_terminal_has_no_retired_earnings_contract():
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (REPO_ROOT / "bzplat" / "frontend" / "src").rglob("*")
        if path.suffix in {".ts", ".tsx"}
    )
    assert "earnings_a" not in source
    assert "earnings_b" not in source
