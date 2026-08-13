"""MatchRunner contracts for platform and user-hosted execution seats."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from bzplat.backend.matches import runner as runner_module
from bzplat.backend.matches.orchestrator import (
    MatchOrchestrator,
    _frozen_execution_profile_version,
)
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.limits import (
    EXECUTION_RESOURCE_PROFILE_REGISTRY,
    PLATFORM_HIGH_PROFILE,
    PLATFORM_LOW_PROFILE,
    execution_resource_snapshot,
    resolve_execution_resource_profile,
)
from bzplat.backend.runtime.local_ai import LocalAIHub, LocalAITechnicalError


class _ProfileTransport:
    """Minimal Traditional transport that records the selected Docker profile."""

    def __init__(self) -> None:
        self._sessions: dict[str, SimpleNamespace] = {}
        self.prepared: list[tuple[str, object]] = []
        self.started: list[tuple[str, object]] = []
        self.stopped: list[str] = []

    @staticmethod
    def _state(
        path: str,
        runtime_mode: str,
        profile,
        execution_scope=None,
    ) -> SimpleNamespace:
        return SimpleNamespace(
            binary_path=path,
            runtime_mode=runtime_mode,
            profile=profile,
            execution_scope=execution_scope,
            requests=[],
            responses=[],
            turn=0,
            long_running=False,
        )

    async def prepare_session(
        self,
        path,
        *,
        runtime_mode,
        profile,
        execution_scope=None,
    ) -> str:
        sid = f"logical-{len(self.prepared)}"
        self.prepared.append((str(path), profile))
        self._sessions[sid] = self._state(
            str(path), runtime_mode, profile, execution_scope
        )
        return sid

    async def start_session(
        self,
        path,
        *,
        runtime_mode,
        profile,
        execution_scope=None,
    ) -> str:
        sid = f"process-{len(self.started)}"
        self.started.append((str(path), profile))
        self._sessions[sid] = self._state(
            str(path), runtime_mode, profile, execution_scope
        )
        return sid

    async def send(self, _session_id, _line, *, timeout):
        assert timeout > 0
        # Canonical transport response, but an illegal game move.  The judge
        # ends immediately, keeping this a focused environment-routing test.
        return '{"response":{"x":999,"y":999}}'

    async def read_extra_line(self, _session_id, *, timeout):
        assert timeout > 0
        return ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"

    async def stop_session(self, session_id):
        self.stopped.append(session_id)
        self._sessions.pop(session_id, None)


class _NeverStartMatchRunner:
    action_timeout = 1.0

    async def run_binaries(self, *_args, **_kwargs):
        raise AssertionError("invalid profile reached the runner")

    async def run_bot_vs_human(self, *_args, **_kwargs):
        raise AssertionError("invalid human profile reached the runner")


class _InvalidProfileMatchStore:
    def __init__(self, match: dict) -> None:
        self.match = dict(match)
        self.updates: list[dict] = []
        self.replays: list[str] = []

    def get_match(self, match_id):
        return dict(self.match) if match_id == self.match["id"] else None

    def get_bot(self, bot_id):
        return {"id": bot_id, "name": f"bot-{bot_id}", "is_active": 1}

    def update_match(self, _match_id, **fields):
        self.updates.append(dict(fields))
        self.match.update(fields)
        return dict(self.match)

    def upsert_replay(self, _match_id, events_json):
        self.replays.append(str(events_json))


def test_high_profile_is_applied_to_both_logical_and_one_shot_sessions():
    transport = _ProfileTransport()
    result = asyncio.run(
        MatchRunner(transport).run_binaries(
            "/bots/high-a",
            "/bots/high-b",
            game_id="gomoku",
            execution_environments=("platform_high", "platform_high"),
        )
    )

    assert result.reason == "illegal"
    assert transport.prepared == [
        ("/bots/high-a", PLATFORM_HIGH_PROFILE),
        ("/bots/high-b", PLATFORM_HIGH_PROFILE),
    ]
    assert transport.started == [("/bots/high-a", PLATFORM_HIGH_PROFILE)]
    assert PLATFORM_HIGH_PROFILE.cpus == 2
    assert PLATFORM_HIGH_PROFILE.memory_mb == 2048
    assert len(transport.stopped) == 3


def test_traditional_every_decision_reuses_the_frozen_profile(monkeypatch):
    async def fake_run_session(_game_id, decide, **_kwargs):
        await decide(0, {"turn": 1})
        await decide(1, {"turn": 2})
        await decide(0, {"turn": 3})
        return SimpleNamespace(reason="complete")

    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    transport = _ProfileTransport()
    result = asyncio.run(
        MatchRunner(transport).run_binaries(
            "/bots/high-a",
            "/bots/high-b",
            game_id="gomoku",
            execution_environments=("platform_high", "platform_high"),
            execution_profile_version=1,
        )
    )

    assert result.reason == "complete"
    assert transport.started == [
        ("/bots/high-a", PLATFORM_HIGH_PROFILE),
        ("/bots/high-b", PLATFORM_HIGH_PROFILE),
        ("/bots/high-a", PLATFORM_HIGH_PROFILE),
    ]


def test_high_profile_is_applied_to_longrunning_sessions():
    transport = _ProfileTransport()
    result = asyncio.run(
        MatchRunner(transport).run_binaries(
            "/bots/high-a",
            "/bots/high-b",
            game_id="gomoku",
            runtime_modes=("longrunning", "longrunning"),
            execution_environments=("platform_high", "platform_high"),
            execution_profile_version=1,
        )
    )

    assert result.reason == "illegal"
    assert transport.prepared == []
    assert transport.started == [
        ("/bots/high-a", PLATFORM_HIGH_PROFILE),
        ("/bots/high-b", PLATFORM_HIGH_PROFILE),
    ]
    assert len(transport.stopped) == 2


def test_duplicate_uses_the_frozen_profile_for_both_seats(monkeypatch):
    async def fake_run_session(*_args, **_kwargs):
        return SimpleNamespace(
            rounds=[],
            rounds_played=0,
            events=[],
            net=[0, 0],
            final_chips=[0, 0],
        )

    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    transport = _ProfileTransport()
    result = asyncio.run(
        MatchRunner(transport).run_duplicate(
            "/bots/high-a",
            "/bots/high-b",
            game_id="holdem",
            seed=7,
            execution_environments=("platform_high", "platform_high"),
            execution_profile_version=1,
            duplicate=True,
        )
    )

    assert result.legs == [
        {"winner": None, "deltas": [0, 0]},
        {"winner": None, "deltas": [0, 0]},
    ]
    assert transport.prepared == [
        ("/bots/high-a", PLATFORM_HIGH_PROFILE),
        ("/bots/high-b", PLATFORM_HIGH_PROFILE),
    ]


def test_human_runner_uses_the_frozen_legacy_low_profile():
    transport = _ProfileTransport()
    result = asyncio.run(
        MatchRunner(transport).run_bot_vs_human(
            "/bots/legacy-low",
            bot_seat=0,
            human_decide=lambda *_args: {"x": 0, "y": 0},
            game_id="gomoku",
            execution_environment="platform_low",
            execution_profile_version=0,
        )
    )

    assert result.reason == "illegal"
    assert transport.prepared == [
        (
            "/bots/legacy-low",
            EXECUTION_RESOURCE_PROFILE_REGISTRY[0]["platform_low"],
        )
    ]


def test_unknown_or_incompatible_profile_version_fails_before_launch():
    transport = _ProfileTransport()
    with pytest.raises(ValueError, match="未知执行资源档位版本"):
        asyncio.run(
            MatchRunner(transport).run_binaries(
                "/bots/a",
                "/bots/b",
                game_id="gomoku",
                execution_profile_version=999,
            )
        )
    with pytest.raises(ValueError, match="不支持执行环境"):
        asyncio.run(
            MatchRunner(transport).run_binaries(
                "/bots/a",
                "/bots/b",
                game_id="gomoku",
                execution_environments=("platform_high", "platform_high"),
                execution_profile_version=0,
            )
        )

    assert transport.prepared == []
    assert transport.started == []


def test_profile_registry_and_orchestrator_legacy_contract_are_versioned():
    assert execution_resource_snapshot(
        ("remote_local", "platform_low"), 1
    ) == (1, 1000, 512)
    assert execution_resource_snapshot(
        ("platform_high", "platform_high"), 1
    ) == (2, 4000, 4096)
    assert resolve_execution_resource_profile(
        "platform_low", 0
    ) is EXECUTION_RESOURCE_PROFILE_REGISTRY[0]["platform_low"]
    assert _frozen_execution_profile_version(
        {}, ("platform_low", "platform_low")
    ) == 0
    with pytest.raises(ValueError, match="未知执行资源档位版本"):
        _frozen_execution_profile_version(
            {"_execution_profile_version": 404},
            ("platform_low", "platform_low"),
        )


def test_orchestrator_rejects_unknown_profile_before_marking_match_running():
    store = _InvalidProfileMatchStore(
        {
            "id": "invalid-profile",
            "game_id": "gomoku",
            "bot_a_id": 1,
            "bot_b_id": 2,
            "match_type": "challenge",
            "match_config": {
                "_bot_a_environment": "platform_low",
                "_bot_b_environment": "platform_low",
                "_execution_profile_version": 404,
            },
        }
    )
    orchestrator = MatchOrchestrator(
        store, runner=_NeverStartMatchRunner()
    )

    asyncio.run(
        orchestrator._MatchOrchestrator__run_match_inner("invalid-profile")
    )

    assert store.match["status"] == "aborted"
    assert store.match["reason"] == "invalid_match_config"
    assert not any(update.get("status") == "running" for update in store.updates)
    assert len(store.replays) == 1


def test_human_orchestrator_rejects_unknown_profile_before_runner():
    store = _InvalidProfileMatchStore(
        {
            "id": "invalid-human-profile",
            "game_id": "gomoku",
            "bot_a_id": 1,
            "bot_b_id": 1,
            "match_type": "human",
            "human_seat": 1,
            "match_config": {
                "_bot_a_environment": "platform_low",
                "_bot_b_environment": "human",
                "_execution_profile_version": 404,
            },
        }
    )
    orchestrator = MatchOrchestrator(
        store, runner=_NeverStartMatchRunner()
    )

    asyncio.run(orchestrator._run_human_match("invalid-human-profile"))

    assert store.match["status"] == "aborted"
    assert store.match["reason"] == "invalid_match_config"
    assert not any(update.get("status") == "running" for update in store.updates)
    assert len(store.replays) == 1


def test_mixed_local_and_low_profile_routes_only_docker_seat_to_transport():
    async def scenario():
        hub = LocalAIHub()
        connection = await hub.register(
            "agent-local-a", connection_id="connector-a"
        )
        transport = _ProfileTransport()

        async def connector():
            turn = await hub.next_turn("agent-local-a", connection.connection_id)
            assert turn is not None
            envelope = json.loads(turn.input)
            assert set(envelope) == {"requests", "responses"}
            assert envelope["responses"] == []
            assert (turn.match_id, turn.seat, turn.turn) == (
                "match-mixed",
                0,
                1,
            )
            await hub.submit_response(
                "agent-local-a",
                connection.connection_id,
                request_id=turn.request_id,
                match_id=turn.match_id,
                turn=turn.turn,
                output='{"response":{"x":999,"y":999}}',
            )

        connector_task = asyncio.create_task(connector())
        result = await MatchRunner(
            transport, local_ai_hub=hub
        ).run_binaries(
            None,
            "/bots/low-b",
            game_id="gomoku",
            execution_environments=("remote_local", "platform_low"),
            local_agent_ids=("agent-local-a", None),
            match_id="match-mixed",
        )
        await connector_task
        assert result.reason == "illegal"
        assert transport.prepared == [("/bots/low-b", PLATFORM_LOW_PROFILE)]
        assert transport.started == []
        assert len(transport.stopped) == 1

    asyncio.run(scenario())


def test_two_local_bots_use_referee_without_starting_any_docker_session():
    async def scenario():
        hub = LocalAIHub()
        first = await hub.register("agent-a", connection_id="connector-a")
        await hub.register("agent-b", connection_id="connector-b")
        transport = _ProfileTransport()

        async def first_connector():
            turn = await hub.next_turn("agent-a", first.connection_id)
            assert turn is not None
            await hub.submit_response(
                "agent-a",
                first.connection_id,
                request_id=turn.request_id,
                match_id=turn.match_id,
                turn=turn.turn,
                output='{"response":{"x":999,"y":999}}',
            )

        connector_task = asyncio.create_task(first_connector())
        result = await MatchRunner(
            transport, local_ai_hub=hub
        ).run_binaries(
            None,
            None,
            game_id="gomoku",
            execution_environments=("remote_local", "remote_local"),
            local_agent_ids=("agent-a", "agent-b"),
            match_id="match-local-local",
        )
        await connector_task
        assert result.reason == "illegal"
        assert transport.prepared == []
        assert transport.started == []
        assert transport.stopped == []
        assert (await hub.status("agent-a")).state == "online"
        assert (await hub.status("agent-b")).state == "online"

    asyncio.run(scenario())


def test_pencil_client_failure_ends_900_second_local_turn_immediately():
    async def scenario():
        hub = LocalAIHub()
        first = await hub.register("agent-a", connection_id="connector-a")
        await hub.register("agent-b", connection_id="connector-b")
        transport = _ProfileTransport()
        events: list[dict] = []

        async def first_connector():
            turn = await hub.next_turn("agent-a", first.connection_id)
            assert turn is not None
            assert (turn.match_id, turn.seat, turn.turn) == (
                "match-pencil-client-failure",
                0,
                1,
            )
            await hub.submit_failure(
                "agent-a",
                first.connection_id,
                request_id=turn.request_id,
                match_id=turn.match_id,
                turn=turn.turn,
                reason="bot_start_failed",
            )

        connector_task = asyncio.create_task(first_connector())
        with pytest.raises(LocalAITechnicalError) as failed:
            await asyncio.wait_for(
                MatchRunner(transport, local_ai_hub=hub).run_binaries(
                    None,
                    None,
                    game_id="pencil",
                    execution_environments=("remote_local", "remote_local"),
                    local_agent_ids=("agent-a", "agent-b"),
                    match_id="match-pencil-client-failure",
                    time_budget_per_side=900.0,
                    on_event=lambda _kind, event: events.append(event),
                ),
                timeout=1.0,
            )
        await connector_task

        assert failed.value.error_code == "local_ai_unavailable"
        assert failed.value.failed_seat == 0
        assert failed.value.turn == 1
        assert any(
            event.get("type") == "technical_incident"
            and event.get("code") == "local_ai_unavailable"
            for event in events
        )
        assert (await hub.status("agent-a")).state == "online"
        assert (await hub.status("agent-b")).state == "online"
        assert transport.prepared == []
        assert transport.started == []

    asyncio.run(scenario())
