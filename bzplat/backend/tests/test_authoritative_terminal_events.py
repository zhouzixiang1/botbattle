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

from bzplat.backend.api_routes import match_detail, match_replay
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
from bzplat.backend.store.public_contract import sanitize_public_event
from bzplat.backend.tests.execution_helpers import (
    challenge_and_start,
    ensure_cleanup_surface,
    start_claimed_match,
)


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
        "rounds_played": 1,
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


def test_public_events_are_strictly_projected_for_replay_and_live(tmp_path):
    """Unknown diagnostics and private extra fields never cross public paths."""
    store = Store(str(tmp_path / "public-events.db"))
    private = "/private/adapter.py: secret stderr"
    raw_move = {
        "type": "move",
        "player": 0,
        "x": 3,
        "y": 4,
        "move_index": 1,
        "message": private,
        "path": private,
        "debug": {"stderr": private},
    }
    safe_move = {
        "type": "move",
        "player": 0,
        "x": 3,
        "y": 4,
        "move_index": 1,
    }
    diagnostic = {
        "type": "diagnostic",
        "message": private,
        "path": private,
        "stderr": private,
    }
    assert sanitize_public_event(raw_move) == safe_move
    assert sanitize_public_event(diagnostic) is None
    assert sanitize_public_event(
        {
            "type": "your_turn",
            "player": 1,
            "request": {
                "num_players": 2,
                "my_id": 1,
                "history": [
                    {
                        "round": 0,
                        "player_id": 0,
                        "action": 0,
                        "action_type": "call",
                        "path": private,
                    }
                ],
                "debug": private,
            },
            "message": private,
        }
    ) == {
        "type": "your_turn",
        "player": 1,
        "request": {
            "num_players": 2,
            "my_id": 1,
            "history": [
                {
                    "round": 0,
                    "player_id": 0,
                    "action": 0,
                    "action_type": "call",
                }
            ],
        },
    }

    owner, bot_a = _user_bot(store, "public-event-a", game_id="gomoku")
    _, bot_b = _user_bot(store, "public-event-b", game_id="gomoku")
    match_id = "public-event-projection"
    store.create_match(
        match_id,
        bot_a["id"],
        bot_b["id"],
        owner_id=owner["id"],
        game_id="gomoku",
    )
    store.update_match(
        match_id,
        status="completed",
        reason="completed",
        winner=0,
        result={"deltas": [1, -1]},
    )
    store.upsert_replay(
        match_id,
        json.dumps([diagnostic, raw_move, {"type": "match_end"}]),
    )
    public_events = json.loads(store.get_public_replay(match_id)["events_json"])
    assert public_events == [
        safe_move,
        {"type": "match_end", "winner": 0, "reason": "completed", "deltas": [1, -1]},
    ]
    assert private not in json.dumps(public_events)

    orch = MatchOrchestrator(store, runner=object(), max_concurrent=1)
    queue: asyncio.Queue = asyncio.Queue()
    orch._sse[match_id] = [queue]
    orch._broadcast(match_id, diagnostic)
    assert queue.empty()
    orch._broadcast(match_id, raw_move)
    assert queue.get_nowait() == safe_move
    store.close()


def test_active_human_holdem_hides_opponent_cards_from_public_streams(tmp_path):
    """Spectators see no live hole cards; the authenticated human sees only theirs."""
    store = Store(str(tmp_path / "human-card-visibility.db"))
    human, bot = _user_bot(store, "human-card-visibility")
    match_id = "active-human-card-visibility"
    store.create_match(
        match_id,
        bot["id"],
        bot["id"],
        owner_id=human["id"],
        match_type="human",
        game_id="holdem",
        human_user_id=human["id"],
        human_seat=1,
    )
    store.update_match(match_id, status="running")
    deal = {
        "type": "deal_hole",
        "hand": 0,
        "holes": [["As", "Ah"], ["Ks", "Kh"]],
    }
    turn = {
        "type": "your_turn",
        "player": 1,
        "request": {
            "num_players": 2,
            "my_id": 1,
            "my_cards": [44, 45],
            "public_cards": [],
            "history": [],
            "hand": 0,
            "max_hand": 70,
        },
    }
    store.upsert_replay(match_id, json.dumps([deal, turn]))

    spectator_events = json.loads(store.get_public_replay(match_id)["events_json"])
    assert spectator_events == [
        {"type": "deal_hole", "hand": 0, "holes": [[], []]},
    ]
    player_events = json.loads(
        store.get_public_replay(match_id, human_viewer_seat=1)["events_json"]
    )
    assert player_events == [
        {"type": "deal_hole", "hand": 0, "holes": [[], ["Ks", "Kh"]]},
        turn,
    ]

    orch = MatchOrchestrator(store, runner=object(), max_concurrent=1)
    unpersisted_action = {
        "type": "action",
        "hand": 0,
        "player": 0,
        "action": "call",
        "amount": 50,
        "debug": "/private/live-prefix",
    }
    # Running subscriptions use the complete in-memory prefix, not the lagging
    # persisted replay, while retaining the same viewer-specific redaction.
    orch._active_replay_events[match_id] = [deal, turn, unpersisted_action]
    spectator = orch.subscribe(match_id)
    player = orch.subscribe(match_id, human_viewer_seat=1)
    assert spectator.get_nowait()["events"] == [
        {"type": "deal_hole", "hand": 0, "holes": [[], []]},
        {
            "type": "action",
            "hand": 0,
            "player": 0,
            "action": "call",
            "amount": 50,
        },
    ]
    assert player.get_nowait()["events"] == [
        {"type": "deal_hole", "hand": 0, "holes": [[], ["Ks", "Kh"]]},
        turn,
        {
            "type": "action",
            "hand": 0,
            "player": 0,
            "action": "call",
            "amount": 50,
        },
    ]

    orch._broadcast(match_id, deal)
    orch._broadcast(match_id, turn)
    assert spectator.get_nowait() == {
        "type": "deal_hole",
        "hand": 0,
        "holes": [[], []],
    }
    assert spectator.empty()
    assert player.get_nowait() == {
        "type": "deal_hole",
        "hand": 0,
        "holes": [[], ["Ks", "Kh"]],
    }
    assert player.get_nowait() == turn
    orch.unsubscribe(match_id, spectator)
    orch.unsubscribe(match_id, player)
    orch._active_replay_events.pop(match_id, None)
    store.close()


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


class _PrivateCompletedReasonRunner:
    async def run_binaries(self, *_args, **kwargs):
        on_event = kwargs["on_event"]
        start = {"type": "match_start"}
        engine_end = {
            "type": "match_end",
            "winner": 0,
            "reason": "privateadapterfailure",
            "message": "内部异常路径",
        }
        on_event("match_start", start)
        on_event("match_end", engine_end)
        return SimpleNamespace(
            rounds_played=1,
            rounds=[SimpleNamespace(deltas=[3, -3])],
            events=[start, engine_end],
            winner=0,
            reason="privateadapterfailure",
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
        elif isinstance(self.error, RuntimeError) and on_event is not None:
            # A future/third-party game adapter must not be able to terminate the
            # public stream early or expose its diagnostic payload.
            on_event(
                "diagnostic",
                {
                    "type": "error",
                    "reason": "private_adapter_failure",
                    "message": str(self.error),
                    "path": "/private/adapter.py",
                },
            )
            # Guard the reverse mismatch too: terminal semantics can arrive in
            # ``kind`` even when an adapter mislabels the event dictionary.
            on_event(
                "error",
                {
                    "type": "diagnostic",
                    "message": str(self.error),
                    "path": "/private/reverse-adapter.py",
                },
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
    match_id = await challenge_and_start(
        orch,
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
    start_claimed_match(orch, match_id)
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


def test_unknown_completed_reason_is_normalized_before_live_and_storage(tmp_path):
    store = Store(str(tmp_path / "private-completed-reason.db"))
    owner, bot_a = _user_bot(store, "private-completed-a")
    _, bot_b = _user_bot(store, "private-completed-b")
    orch = MatchOrchestrator(
        store,
        runner=_PrivateCompletedReasonRunner(),
        max_concurrent=1,
    )

    match_id, live_events, _ = asyncio.run(
        _run_prepared_match(
            store,
            orch,
            bot_a["id"],
            bot_b["id"],
            owner["id"],
        )
    )
    terminal = [event for event in live_events if event.get("type") == "match_end"]
    assert terminal == [
        {"type": "match_end", "winner": 0, "reason": "completed", "deltas": [3, -3]}
    ]
    detail = match_detail(match_id, _api_request(store, match_id))
    assert detail["match"]["reason"] == "completed"
    replay = match_replay(match_id, _api_request(store, match_id))
    assert replay["events"][-1] == terminal[0]
    assert "replay" not in detail
    assert "privateadapterfailure" not in json.dumps(detail, ensure_ascii=False)
    assert "内部异常路径" not in json.dumps(detail, ensure_ascii=False)
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
        (
            RuntimeError("generic failure at /private/bot_uploads/secret"),
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
    if expected_deltas is not None:
        assert len(terminal) == 1
        assert terminal[0]["type"] == expected_type
        assert len(observations) == 1
        assert observations[0]["store"]["status"] == expected_status
        assert observations[0]["api"]["status"] == expected_status
        assert observations[0]["store"]["reason"] == expected_reason
        match_id = observations[0]["store"]["id"]
        replay = json.loads(store.get_public_replay(match_id)["events_json"])
        replay_terminals = [
            event
            for event in replay
            if event.get("type") in {"match_end", "error"}
        ]
        assert replay_terminals == terminal
        assert terminal[0] == {
            "type": "match_end",
            "winner": observations[0]["store"]["winner"],
            "reason": expected_reason,
            "deltas": expected_deltas,
        }
        assert observations[0]["api"]["result"]["deltas"] == expected_deltas
    else:
        # Durable attempts do not manufacture a public platform_error. They keep
        # the match/job recoverable until exact namespace cleanup, then expose a
        # truthful interrupted manual request without retaining a garbage match.
        assert terminal == []
        assert observations == []
        active = store.executions.snapshot(
            max_match_slots=1,
            max_sandbox_units=2,
            aging_seconds=60,
        )["active"]
        assert len(active) == 1
        request_public_id = active[0]["public_id"]
        match_id = active[0]["current_match_id"]
        assert store.get_match(match_id)["status"] == "running"
        assert store.executions.control()["dispatcher_state"] == "paused"
        public_detail = match_detail(match_id, _api_request(store, match_id))
        assert "/private" not in json.dumps(public_detail, ensure_ascii=False)
        assert store.executions.recover_after_namespace_cleanup() == {
            "requeued": 0,
            "interrupted": 1,
            "settling": 0,
        }
        assert store.get_match(match_id) is None
        interrupted = store.executions.get(request_public_id)
        assert interrupted["status"] == "interrupted"
        assert interrupted["retryable"] == 1
        assert store.executions.snapshot(
            max_match_slots=1,
            max_sandbox_units=2,
            aging_seconds=60,
        )["queued"] == []
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
            on_event(
                "error",
                {
                    "type": "diagnostic",
                    "message": "human private adapter failure",
                    "path": "/private/human-adapter.py",
                },
            )
            on_event("match_end", engine_end)
            self.engine_end_emitted.set()
            while not self.release_result.is_set():
                await asyncio.sleep(0.005)
            return result

    app = create_app(db_path=str(tmp_path / "human-terminal.db"))
    runner = ControlledHumanRunner()
    app.state.orch.runner = runner
    ensure_cleanup_surface(app.state.orch)
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
        assert created.status_code == 202, created.text
        request_id = created.json()["public_id"]
        assert runner.engine_end_emitted.wait(timeout=2)
        request = store.executions.get(request_id)
        assert request and request["current_match_id"]
        match_id = request["current_match_id"]

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
            assert "/private" not in json.dumps(snapshot, ensure_ascii=False)

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
            assert "/private" not in json.dumps(body, ensure_ascii=False)
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1000

        final_detail = client.get(f"/api/matches/{match_id}").json()
        assert "replay" not in final_detail
        replay = client.get(f"/api/matches/{match_id}/replay").json()["events"]
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


def test_game_event_describers_do_not_consume_private_error_messages():
    """Platform errors are reason-only; game packages cannot revive message."""
    game_sources = (
        REPO_ROOT / "bzplat/frontend/src/games/holdem/view.tsx",
        REPO_ROOT / "bzplat/frontend/src/games/gomoku/index.ts",
        REPO_ROOT / "bzplat/frontend/src/games/pencil/index.ts",
    )
    for path in game_sources:
        source = path.read_text(encoding="utf-8")
        assert "event.message" not in source
