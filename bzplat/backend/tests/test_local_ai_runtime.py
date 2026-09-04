"""Transport-independent contract tests for outbound local Bot connections."""
from __future__ import annotations

import asyncio
import gc

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


def test_relative_decision_timeout_starts_after_lock_and_delivery_prep():
    """Hub contention and request copying are outside the Bot decision clock."""

    async def scenario() -> None:
        clock = _Clock(10)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")

        class SlowPayload:
            def __deepcopy__(self, _memo):
                clock.now += 2
                return SlowPayload()

        await hub._lock.acquire()
        elapsed: list[float] = []
        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-relative-deadline",
                match_id="match-relative-deadline",
                seat=0,
                turn=1,
                decision_timeout=3,
                input=SlowPayload(),
                on_decision_elapsed=elapsed.append,
            )
        )
        await asyncio.sleep(0)
        clock.now = 15
        hub._lock.release()

        message = await hub.next_turn("bot-a", "connection-a")
        assert message is not None
        assert message.phase == "prepare"
        assert message.input is None
        assert message.deadline_at == pytest.approx(25)
        clock.now = 17
        await hub.mark_prepared(
            "bot-a",
            "connection-a",
            request_id=message.request_id,
            match_id=message.match_id,
            turn=message.turn,
        )
        active = await hub.next_turn("bot-a", "connection-a")
        assert active is not None
        assert active.phase == "decision"
        assert isinstance(active.input, SlowPayload)
        assert active.deadline_at == pytest.approx(22)
        clock.now = 20
        await hub.submit_response(
            "bot-a",
            "connection-a",
            request_id="request-relative-deadline",
            match_id="match-relative-deadline",
            turn=1,
            output={"response": 0},
        )
        assert await decision == {"response": 0}
        assert elapsed == pytest.approx([1])

    asyncio.run(scenario())


def test_relative_timeout_stays_frozen_after_delivered_reconnect():
    """Once delivered, reconnect cannot restart a relative decision clock."""

    async def scenario() -> None:
        clock = _Clock(100)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")
        elapsed: list[float] = []

        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-relative-reconnect",
                match_id="match-relative-reconnect",
                seat=0,
                turn=1,
                decision_timeout=3,
                input={"request": "move"},
                on_decision_elapsed=elapsed.append,
            )
        )
        preparation = await hub.next_turn("bot-a", "connection-a")
        assert preparation is not None and preparation.phase == "prepare"
        assert preparation.input is None
        await hub.mark_prepared(
            "bot-a",
            "connection-a",
            request_id=preparation.request_id,
            match_id=preparation.match_id,
            turn=preparation.turn,
        )
        first = await hub.next_turn("bot-a", "connection-a")
        assert first is not None and first.phase == "decision"
        assert first.deadline_at == pytest.approx(103)

        # Keep the elapsed interval small while making the reconnect timestamps
        # visibly different from the original deadline's clock domain.
        clock.now = 101
        await hub.close("bot-a", "connection-a")
        clock.now = 102
        await hub.register("bot-a", connection_id="connection-b")
        repeated = await hub.next_turn("bot-a", "connection-b")
        assert repeated is not None
        assert repeated.request_id == first.request_id
        assert repeated.deadline_at == first.deadline_at
        assert await hub.next_turn(
            "bot-a", "connection-b", timeout=0.001
        ) is None

        await hub.submit_response(
            "bot-a",
            "connection-b",
            request_id="request-relative-reconnect",
            match_id="match-relative-reconnect",
            turn=1,
            output={"response": "move"},
        )
        assert await decision == {"response": "move"}
        assert elapsed == pytest.approx([2])

    asyncio.run(scenario())


def test_relative_request_waits_bounded_reconnect_without_clock_charge():
    async def scenario() -> None:
        clock = _Clock(100)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")
        await hub.close("bot-a", "connection-a")
        elapsed: list[float] = []

        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-offline-before-delivery",
                match_id="match-offline-before-delivery",
                seat=0,
                turn=1,
                decision_timeout=3,
                input={"request": "move"},
                on_decision_elapsed=elapsed.append,
            )
        )
        await asyncio.sleep(0)
        status = await hub.status("bot-a")
        assert status.online is False and status.busy is True
        assert status.pending_request_id == "request-offline-before-delivery"
        assert status.pending_deadline_at == 108
        assert elapsed == []
        assert "request-offline-before-delivery" in hub._seen_request_ids
        assert "request-offline-before-delivery" not in hub._terminal

        clock.now = 104
        await hub.register("bot-a", connection_id="connection-b")
        preparation = await hub.next_turn("bot-a", "connection-b")
        assert preparation is not None and preparation.phase == "prepare"
        assert preparation.input is None and preparation.deadline_at == 108
        await hub.mark_prepared(
            "bot-a",
            "connection-b",
            request_id=preparation.request_id,
            match_id=preparation.match_id,
            turn=preparation.turn,
        )
        message = await hub.next_turn("bot-a", "connection-b")
        assert message is not None and message.deadline_at == 107
        clock.now = 105
        await hub.submit_response(
            "bot-a",
            "connection-b",
            request_id=message.request_id,
            match_id=message.match_id,
            turn=message.turn,
            output={"response": "move"},
        )
        assert await decision == {"response": "move"}
        assert elapsed == pytest.approx([1])

    asyncio.run(scenario())


def test_relative_preparation_timeout_is_bounded_without_clock_charge_or_input_leak():
    async def scenario() -> None:
        clock = _Clock(10)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")
        elapsed: list[float] = []
        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-preparation-timeout",
                match_id="match-preparation-timeout",
                seat=0,
                turn=1,
                decision_timeout=1,
                input={"secret_position": "must-not-leak"},
                on_decision_elapsed=elapsed.append,
            )
        )
        preparation = await hub.next_turn("bot-a", "connection-a")
        assert preparation is not None
        assert preparation.phase == "prepare" and preparation.input is None
        clock.now = preparation.deadline_at
        with pytest.raises(LocalAIResponseRejected) as rejected:
            await hub.mark_prepared(
                "bot-a",
                "connection-a",
                request_id=preparation.request_id,
                match_id=preparation.match_id,
                turn=preparation.turn,
            )
        assert rejected.value.reason == "preparation_deadline_exceeded"
        with pytest.raises(LocalAITechnicalError) as failed:
            await decision
        assert failed.value.error_code == "local_ai_unavailable"
        assert elapsed == []
        assert (await hub.status("bot-a")).busy is False

    asyncio.run(scenario())


def test_response_elapsed_is_frozen_at_hub_acceptance_not_task_resume():
    async def scenario() -> None:
        clock = _Clock(100)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")
        elapsed: list[float] = []
        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-frozen-elapsed",
                match_id="match-frozen-elapsed",
                seat=0,
                turn=1,
                decision_timeout=10,
                input={"request": "move"},
                on_decision_elapsed=elapsed.append,
            )
        )
        preparation = await hub.next_turn("bot-a", "connection-a")
        assert preparation is not None
        await hub.mark_prepared(
            "bot-a",
            "connection-a",
            request_id=preparation.request_id,
            match_id=preparation.match_id,
            turn=preparation.turn,
        )
        active = await hub.next_turn("bot-a", "connection-a")
        assert active is not None and active.deadline_at == 110
        clock.now = 101
        await hub.submit_response(
            "bot-a",
            "connection-a",
            request_id=active.request_id,
            match_id=active.match_id,
            turn=active.turn,
            output={"response": "move"},
        )
        clock.now = 109
        assert await decision == {"response": "move"}
        assert elapsed == pytest.approx([1])

    asyncio.run(scenario())


def test_late_response_caps_elapsed_at_the_frozen_decision_deadline():
    async def scenario() -> None:
        clock = _Clock(50)
        hub = LocalAIHub(clock=clock)
        await hub.register("bot-a", connection_id="connection-a")
        elapsed: list[float] = []
        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-capped-elapsed",
                match_id="match-capped-elapsed",
                seat=0,
                turn=1,
                decision_timeout=3,
                input={},
                on_decision_elapsed=elapsed.append,
            )
        )
        preparation = await hub.next_turn("bot-a", "connection-a")
        assert preparation is not None
        await hub.mark_prepared(
            "bot-a",
            "connection-a",
            request_id=preparation.request_id,
            match_id=preparation.match_id,
            turn=preparation.turn,
        )
        active = await hub.next_turn("bot-a", "connection-a")
        assert active is not None and active.deadline_at == 53
        clock.now = 60
        with pytest.raises(LocalAIResponseRejected, match="deadline_exceeded"):
            await hub.submit_response(
                "bot-a",
                "connection-a",
                request_id=active.request_id,
                match_id=active.match_id,
                turn=active.turn,
                output={"response": "late"},
            )
        with pytest.raises(LocalAITechnicalError) as failed:
            await decision
        assert failed.value.error_code == "local_ai_timeout"
        assert elapsed == pytest.approx([3])

    asyncio.run(scenario())


def test_relative_timer_expiry_reports_the_full_frozen_allowance():
    async def scenario() -> None:
        hub = LocalAIHub()
        await hub.register("bot-a", connection_id="connection-a")
        elapsed: list[float] = []
        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-real-timeout",
                match_id="match-real-timeout",
                seat=0,
                turn=1,
                decision_timeout=0.02,
                input={},
                on_decision_elapsed=elapsed.append,
            )
        )
        preparation = await hub.next_turn("bot-a", "connection-a")
        assert preparation is not None
        await hub.mark_prepared(
            "bot-a",
            "connection-a",
            request_id=preparation.request_id,
            match_id=preparation.match_id,
            turn=preparation.turn,
        )
        await hub.next_turn("bot-a", "connection-a")
        with pytest.raises(LocalAITechnicalError) as failed:
            await decision
        assert failed.value.error_code == "local_ai_timeout"
        assert elapsed == pytest.approx([0.02])

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal_mode", ["cancel", "failure", "timeout"])
def test_closed_queued_decision_cannot_reach_the_next_request(terminal_mode):
    async def scenario() -> None:
        hub = LocalAIHub()
        await hub.register("bot-a", connection_id="connection-a")
        timeout = 0.01 if terminal_mode == "timeout" else 1.0
        old = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id=f"request-old-{terminal_mode}",
                match_id="match-old",
                seat=0,
                turn=1,
                decision_timeout=timeout,
                input={"secret_old_position": "must-not-be-delivered"},
            )
        )
        preparation = await hub.next_turn("bot-a", "connection-a")
        assert preparation is not None and preparation.phase == "prepare"
        await hub.mark_prepared(
            "bot-a",
            "connection-a",
            request_id=preparation.request_id,
            match_id=preparation.match_id,
            turn=preparation.turn,
        )
        # Deliberately leave the full decision frame queued in the transport.
        if terminal_mode == "cancel":
            old.cancel()
            with pytest.raises(asyncio.CancelledError):
                await old
        elif terminal_mode == "failure":
            await hub.submit_failure(
                "bot-a",
                "connection-a",
                request_id=preparation.request_id,
                match_id=preparation.match_id,
                turn=preparation.turn,
                reason="bot_output_invalid",
            )
            with pytest.raises(LocalAITechnicalError):
                await old
        else:
            with pytest.raises(LocalAITechnicalError) as timed_out:
                await old
            assert timed_out.value.error_code == "local_ai_timeout"

        connection_id = "connection-a"
        if terminal_mode in {"cancel", "timeout"}:
            status = await hub.status("bot-a")
            assert status.online is False and hub.available_now("bot-a") is False
            connection_id = "connection-b"
            await hub.register("bot-a", connection_id=connection_id)
        fresh = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id=f"request-fresh-{terminal_mode}",
                match_id="match-fresh",
                seat=0,
                turn=2,
                decision_timeout=1,
                input={"fresh_position": True},
            )
        )
        delivered = await hub.next_turn(
            "bot-a", connection_id, timeout=0.1
        )
        assert delivered is not None
        assert delivered.request_id == f"request-fresh-{terminal_mode}"
        assert delivered.match_id == "match-fresh"
        assert delivered.phase == "prepare" and delivered.input is None
        fresh.cancel()
        with pytest.raises(asyncio.CancelledError):
            await fresh

    asyncio.run(scenario())


@pytest.mark.parametrize("terminal_mode", ["cancel", "timeout"])
def test_delivered_terminal_request_disconnects_transport_before_reuse(
    terminal_mode,
):
    async def scenario() -> None:
        hub = LocalAIHub()
        await hub.register("bot-a", connection_id="connection-a")
        old = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id=f"request-delivered-{terminal_mode}",
                match_id="match-delivered",
                seat=0,
                turn=1,
                decision_timeout=(0.01 if terminal_mode == "timeout" else 1),
                input={"secret_old_position": True},
            )
        )
        preparation = await hub.next_turn("bot-a", "connection-a")
        assert preparation is not None
        await hub.mark_prepared(
            "bot-a",
            "connection-a",
            request_id=preparation.request_id,
            match_id=preparation.match_id,
            turn=preparation.turn,
        )
        delivered = await hub.next_turn("bot-a", "connection-a")
        assert delivered is not None and delivered.phase == "decision"
        # This is the API sender's next loop iteration.  A terminal request
        # must wake it so the endpoint closes the socket and the v2 client
        # kills its in-flight process.
        sender_waiter = asyncio.create_task(
            hub.next_turn("bot-a", "connection-a")
        )
        await asyncio.sleep(0)

        if terminal_mode == "cancel":
            old.cancel()
            with pytest.raises(asyncio.CancelledError):
                await old
        else:
            with pytest.raises(LocalAITechnicalError) as timed_out:
                await old
            assert timed_out.value.error_code == "local_ai_timeout"

        status = await hub.status("bot-a")
        assert status.online is False and status.busy is False
        assert hub.available_now("bot-a") is False
        with pytest.raises(LocalAIConnectionError, match="connection_closed"):
            await sender_waiter

        await hub.register("bot-a", connection_id="connection-b")
        assert hub.available_now("bot-a") is True

    asyncio.run(scenario())


def test_prepare_phase_accepts_only_start_failure_category():
    async def scenario() -> None:
        hub = LocalAIHub()
        await hub.register("bot-a", connection_id="connection-a")
        decision = asyncio.create_task(
            hub.request_decision(
                "bot-a",
                request_id="request-prepare-failure",
                match_id="match-prepare-failure",
                seat=0,
                turn=1,
                decision_timeout=1,
                input={},
            )
        )
        preparation = await hub.next_turn("bot-a", "connection-a")
        assert preparation is not None and preparation.phase == "prepare"
        with pytest.raises(LocalAIResponseRejected) as invalid:
            await hub.submit_failure(
                "bot-a",
                "connection-a",
                request_id=preparation.request_id,
                match_id=preparation.match_id,
                turn=preparation.turn,
                reason="bot_decision_timeout",
            )
        assert invalid.value.reason == "invalid_failure_phase"
        assert (await hub.status("bot-a")).busy is True
        await hub.submit_failure(
            "bot-a",
            "connection-a",
            request_id=preparation.request_id,
            match_id=preparation.match_id,
            turn=preparation.turn,
            reason="bot_start_failed",
        )
        with pytest.raises(LocalAITechnicalError) as failed:
            await decision
        assert failed.value.error_code == "local_ai_unavailable"

    asyncio.run(scenario())


def test_failure_then_caller_cancel_race_consumes_internal_future_exception():
    async def scenario() -> None:
        loop = asyncio.get_running_loop()
        unhandled: list[dict] = []
        previous = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            hub = LocalAIHub()
            await hub.register("bot-a", connection_id="connection-a")
            decision = asyncio.create_task(
                hub.request_decision(
                    "bot-a",
                    request_id="request-failure-cancel-race",
                    match_id="match-failure-cancel-race",
                    seat=0,
                    turn=1,
                    decision_timeout=1,
                    input={},
                )
            )
            preparation = await hub.next_turn("bot-a", "connection-a")
            assert preparation is not None
            await hub._lock.acquire()
            failure = asyncio.create_task(
                hub.submit_failure(
                    "bot-a",
                    "connection-a",
                    request_id=preparation.request_id,
                    match_id=preparation.match_id,
                    turn=preparation.turn,
                    reason="bot_start_failed",
                )
            )
            await asyncio.sleep(0)
            decision.cancel()
            hub._lock.release()
            await failure
            with pytest.raises(asyncio.CancelledError):
                await decision
            gc.collect()
            await asyncio.sleep(0)
            assert unhandled == []
        finally:
            loop.set_exception_handler(previous)

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


def test_absolute_request_waits_for_reconnect_without_extending_deadline():
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
        assert "request-between-turns" in hub._seen_request_ids

        clock.now = 205
        await hub.register("bot-a", connection_id="connection-b")
        message = await hub.next_turn("bot-a", "connection-b")
        assert message is not None
        assert message.phase == "decision"
        assert message.deadline_at == 210
        assert message.input == {"request": "next-move"}
        clock.now = 206
        await hub.submit_response(
            "bot-a",
            "connection-b",
            request_id=message.request_id,
            match_id=message.match_id,
            turn=message.turn,
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
