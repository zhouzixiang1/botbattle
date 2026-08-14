"""Transport-independent contract tests for outbound local Bot connections."""
from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.runtime.local_ai import (
    LOCAL_AI_CLIENT_FAILURE_REASONS,
    LocalAIBusyError,
    LocalAIConnectionError,
    LocalAIHub,
    LocalAIResponseRejected,
    LocalAITechnicalError,
)


class _Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_connection_lifecycle_and_status_are_agent_scoped():
    async def scenario() -> None:
        clock = _Clock(10)
        hub = LocalAIHub(clock=clock)

        first = await hub.register("bot-a", connection_id="connection-a")
        assert first.connected_at == 10
        assert (await hub.status("bot-a")).state == "online"

        with pytest.raises(LocalAIConnectionError, match="already_connected"):
            await hub.register("bot-a", connection_id="connection-b")

        clock.now = 12
        await hub.heartbeat("bot-a", "connection-a")
        assert (await hub.status("bot-a")).last_seen_at == 12
        assert await hub.close("bot-a", "connection-a") is True
        assert await hub.close("bot-a", "connection-a") is False
        assert (await hub.status("bot-a")).state == "offline"

        second = await hub.register("bot-a", connection_id="connection-b")
        assert second.connection_id == "connection-b"
        with pytest.raises(LocalAIConnectionError, match="stale_connection"):
            await hub.close("bot-a", "connection-a")

    asyncio.run(scenario())


def test_turn_delivery_response_binding_and_duplicate_rejection():
    async def scenario() -> None:
        clock = _Clock(20)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")

        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-1",
                match_id="match-1",
                seat=1,
                turn=3,
                deadline_at=30,
                input={"requests": [{"x": 1}], "responses": []},
            )
        )
        message = await hub.next_turn("bot-a", "connection-a")
        assert message is not None
        assert (message.request_id, message.match_id, message.turn) == (
            "request-1",
            "match-1",
            3,
        )
        assert (await hub.status("bot-a")).state == "busy"

        with pytest.raises(LocalAIResponseRejected, match="binding_mismatch"):
            await hub.submit_response(
                "bot-a",
                "connection-a",
                request_id="request-1",
                match_id="another-match",
                turn=3,
                output={"response": 1},
            )

        accepted = await hub.submit_response(
            "bot-a",
            "connection-a",
            request_id="request-1",
            match_id="match-1",
            turn=3,
            output={"response": {"x": 2, "y": 4}},
        )
        assert accepted.request_id == "request-1"
        assert await decision == {"response": {"x": 2, "y": 4}}
        assert (await hub.status("bot-a")).state == "online"

        with pytest.raises(LocalAIResponseRejected) as raised:
            await hub.submit_response(
                "bot-a",
                "connection-a",
                request_id="request-1",
                match_id="match-1",
                turn=3,
                output={"response": 0},
            )
        assert raised.value.reason == "duplicate_response"

    asyncio.run(scenario())


def test_client_failure_is_strongly_bound_and_releases_pending_turn():
    assert LOCAL_AI_CLIENT_FAILURE_REASONS == {
        "bot_start_failed",
        "bot_no_response",
        "bot_output_too_large",
        "bot_output_invalid",
        "bot_io_failed",
        "bot_decision_timeout",
    }

    async def scenario() -> None:
        clock = _Clock(20)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")
        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-failure-1",
                match_id="match-failure-1",
                seat=1,
                turn=6,
                deadline_at=30,
                input={"request": "move"},
            )
        )
        await hub.next_turn("bot-a", "connection-a")

        with pytest.raises(LocalAIResponseRejected) as wrong_match:
            await hub.submit_failure(
                "bot-a",
                "connection-a",
                request_id="request-failure-1",
                match_id="another-match",
                turn=6,
                reason="bot_start_failed",
            )
        assert wrong_match.value.reason == "request_binding_mismatch"
        assert (await hub.status("bot-a")).busy is True

        with pytest.raises(LocalAIResponseRejected) as invalid_binding:
            await hub.submit_failure(
                "bot-a",
                "connection-a",
                request_id="request-failure-1",
                match_id="match-failure-1",
                turn=True,
                reason="bot_start_failed",
            )
        assert invalid_binding.value.reason == "invalid_binding"

        with pytest.raises(LocalAIResponseRejected) as invalid_reason:
            await hub.submit_failure(
                "bot-a",
                "connection-a",
                request_id="request-failure-1",
                match_id="match-failure-1",
                turn=6,
                reason="private:/home/student/bot",
            )
        assert invalid_reason.value.reason == "invalid_failure_reason"

        accepted = await hub.submit_failure(
            "bot-a",
            "connection-a",
            request_id="request-failure-1",
            match_id="match-failure-1",
            turn=6,
            reason="bot_start_failed",
        )
        assert accepted.request_id == "request-failure-1"
        with pytest.raises(LocalAITechnicalError) as failed:
            await decision
        assert failed.value.error_code == "local_ai_unavailable"
        assert failed.value.failed_seat == 1
        assert failed.value.turn == 6
        assert str(failed.value) == "本地 Bot 无法启动"
        assert (await hub.status("bot-a")).busy is False

        with pytest.raises(LocalAIResponseRejected) as late:
            await hub.submit_failure(
                "bot-a",
                "connection-a",
                request_id="request-failure-1",
                match_id="match-failure-1",
                turn=6,
                reason="bot_start_failed",
            )
        assert late.value.reason == "request_closed"

    asyncio.run(scenario())


def test_one_agent_has_only_one_unresolved_turn():
    async def scenario() -> None:
        hub = LocalAIHub()
        await hub.register("bot-a", connection_id="connection-a")
        deadline = asyncio.get_running_loop().time() + 10
        first = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-1",
                match_id="match-1",
                seat=0,
                turn=1,
                deadline_at=deadline,
                input={},
            )
        )
        await hub.next_turn("bot-a", "connection-a")

        with pytest.raises(LocalAIBusyError, match="agent_busy"):
            await hub.request_decision(
                "bot-a",
                request_id="request-2",
                match_id="match-2",
                seat=0,
                turn=1,
                deadline_at=deadline,
                input={},
            )

        await hub.submit_response(
            "bot-a",
            "connection-a",
            request_id="request-1",
            match_id="match-1",
            turn=1,
            output={"response": 0},
        )
        await first

    asyncio.run(scenario())


def test_disconnect_reconnect_redelivers_same_request_without_extending_deadline():
    async def scenario() -> None:
        clock = _Clock(100)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")
        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-1",
                match_id="match-1",
                seat=0,
                turn=2,
                deadline_at=110,
                input={"request": "move"},
            )
        )
        first = await hub.next_turn("bot-a", "connection-a")
        assert first is not None and first.deadline_at == 110
        first.input["request"] = "connector-mutated-copy"

        clock.now = 106
        await hub.close("bot-a", "connection-a")
        status = await hub.status("bot-a")
        assert status.state == "offline"
        assert status.online is False and status.busy is True

        await hub.register("bot-a", connection_id="connection-b")
        repeated = await hub.next_turn("bot-a", "connection-b")
        assert repeated is not None
        assert repeated.request_id == first.request_id
        assert repeated.deadline_at == first.deadline_at == 110
        assert repeated.input == {"request": "move"}

        await hub.submit_response(
            "bot-a",
            "connection-b",
            request_id="request-1",
            match_id="match-1",
            turn=2,
            output={"response": 1},
        )
        assert await decision == {"response": 1}

    asyncio.run(scenario())


def test_disconnect_between_turns_waits_for_reconnect_with_original_deadline():
    async def scenario() -> None:
        clock = _Clock(200)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")
        await hub.close("bot-a", "connection-a")

        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-between-turns",
                match_id="match-1",
                seat=1,
                turn=5,
                deadline_at=210,
                input={"request": "next-move"},
            )
        )
        await asyncio.sleep(0)
        status = await hub.status("bot-a")
        assert status.online is False
        assert status.busy is True
        assert status.pending_deadline_at == 210

        clock.now = 204
        await hub.register("bot-a", connection_id="connection-b")
        repeated = await hub.next_turn("bot-a", "connection-b")
        assert repeated is not None
        assert repeated.request_id == "request-between-turns"
        assert repeated.deadline_at == 210

        await hub.submit_response(
            "bot-a",
            "connection-b",
            request_id="request-between-turns",
            match_id="match-1",
            turn=5,
            output={"response": "move"},
        )
        assert await decision == {"response": "move"}

    asyncio.run(scenario())


def test_terminal_request_replay_guard_uses_the_same_bounded_history():
    async def scenario() -> None:
        clock = _Clock(300)
        hub = LocalAIHub(clock=clock, terminal_history_size=1)
        await hub.register("bot-a", connection_id="connection-a")

        async def complete(request_id: str, turn: int) -> None:
            decision = asyncio.create_task(
                hub.request_decision(
                    "bot-a",
                    request_id=request_id,
                    match_id="match-1",
                    seat=0,
                    turn=turn,
                    deadline_at=clock.now + 10,
                    input={},
                )
            )
            message = await hub.next_turn("bot-a", "connection-a")
            assert message is not None and message.request_id == request_id
            await hub.submit_response(
                "bot-a",
                "connection-a",
                request_id=request_id,
                match_id="match-1",
                turn=turn,
                output={"response": turn},
            )
            assert await decision == {"response": turn}

        await complete("request-1", 1)
        await complete("request-2", 2)
        assert len(hub._terminal) == 1
        assert len(hub._seen_request_ids) == 1
        assert "request-1" not in hub._seen_request_ids

    asyncio.run(scenario())


def test_late_response_is_rejected_and_becomes_local_ai_technical_fault():
    async def scenario() -> None:
        clock = _Clock(1)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")
        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-1",
                match_id="match-1",
                seat=1,
                turn=4,
                deadline_at=5,
                input={},
            )
        )
        await hub.next_turn("bot-a", "connection-a")
        clock.now = 5

        with pytest.raises(LocalAIResponseRejected) as rejected:
            await hub.submit_response(
                "bot-a",
                "connection-a",
                request_id="request-1",
                match_id="match-1",
                turn=4,
                output={"response": 0},
            )
        assert rejected.value.reason == "deadline_exceeded"

        with pytest.raises(LocalAITechnicalError) as failed:
            await decision
        assert failed.value.error_code == "local_ai_timeout"
        assert failed.value.failed_seat == 1
        assert failed.value.turn == 4
        assert failed.value.affects_docker_health is False
        assert (await hub.status("bot-a")).busy is False

    asyncio.run(scenario())


def test_revoke_closes_connection_and_fails_pending_without_docker_fault():
    async def scenario() -> None:
        hub = LocalAIHub()
        await hub.register("bot-a", connection_id="connection-a")
        deadline = asyncio.get_running_loop().time() + 10
        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-1",
                match_id="match-1",
                seat=0,
                turn=1,
                deadline_at=deadline,
                input={},
            )
        )
        await hub.next_turn("bot-a", "connection-a")
        await hub.revoke("bot-a")

        with pytest.raises(LocalAITechnicalError) as failed:
            await decision
        assert failed.value.error_code == "local_ai_revoked"
        assert failed.value.affects_docker_health is False
        status = await hub.status("bot-a")
        assert status.state == "revoked"
        assert status.online is False and status.busy is False
        with pytest.raises(LocalAIConnectionError, match="agent_revoked"):
            await hub.register("bot-a", connection_id="connection-b")

    asyncio.run(scenario())


def test_revoked_tombstones_expire_and_stay_bounded():
    async def scenario() -> None:
        clock = _Clock(100)
        hub = LocalAIHub(
            clock=clock, revoked_history_size=2, revoked_ttl_seconds=10
        )
        for agent_id in ("bot-a", "bot-b", "bot-c"):
            await hub.revoke(agent_id)
        assert list(hub._revoked) == ["bot-b", "bot-c"]
        assert (await hub.status("bot-b")).state == "revoked"

        clock.now = 111
        assert (await hub.status("bot-b")).state == "offline"
        connection = await hub.register("bot-b", connection_id="connection-b")
        assert connection.connection_id == "connection-b"

    asyncio.run(scenario())


def test_long_poll_timeout_is_not_a_bot_decision_timeout():
    async def scenario() -> None:
        hub = LocalAIHub()
        await hub.register("bot-a", connection_id="connection-a")
        assert await hub.next_turn(
            "bot-a", "connection-a", timeout=0.001
        ) is None
        assert (await hub.status("bot-a")).state == "online"

    asyncio.run(scenario())
