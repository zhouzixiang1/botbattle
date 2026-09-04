"""Transport-agnostic coordination for user-hosted (local) Bot decisions.

The hub deliberately knows nothing about bearer tokens, HTTP, WebSocket or the
database.  An authenticated API adapter registers an ``agent_id``, relays
``next_turn`` messages over an outbound client connection and submits responses
back here.  Match execution awaits ``request_decision`` exactly as it would
await a sandboxed Bot.

Connection loss does not extend a decision deadline.  A reconnect receives the
same unresolved request, with the same request id and absolute deadline.  Local
AI faults are attributable Bot faults and never platform/Docker health faults.
"""

from __future__ import annotations

import asyncio
import copy
import math
import secrets
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from bzplat.backend.runtime.binary_runner import BotTechnicalError
from bzplat.backend.runtime.config import LOCAL_AI_PREPARE_TIMEOUT_SEC


_LOCAL_AI_CLIENT_FAILURES: dict[str, tuple[str, str]] = {
    "bot_start_failed": ("local_ai_unavailable", "本地 Bot 无法启动"),
    "bot_no_response": ("missing_response", "本地 Bot 未输出响应"),
    "bot_output_too_large": (
        "response_line_too_large",
        "本地 Bot 响应行超过 64 KiB 上限",
    ),
    "bot_output_invalid": ("invalid_response", "本地 Bot 输出格式无效"),
    "bot_io_failed": ("local_ai_unavailable", "本地 Bot 输入输出失败"),
    "bot_decision_timeout": ("decision_timeout", "本地 Bot 决策超时"),
}
LOCAL_AI_CLIENT_FAILURE_REASONS = frozenset(_LOCAL_AI_CLIENT_FAILURES)
LOCAL_AI_WEBSOCKET_SUBPROTOCOL = "botbattle.local-ai.v2"


class LocalAIHubError(RuntimeError):
    """Base class for connector/API contract errors, not match outcomes."""


class LocalAIConnectionError(LocalAIHubError):
    """The connector is unknown, stale, already connected or revoked."""


class LocalAIBusyError(LocalAIHubError):
    """An agent already owns one unresolved decision request."""


class LocalAIResponseRejected(LocalAIHubError):
    """A connector response was not valid for the current pending request."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class LocalAITechnicalError(BotTechnicalError):
    """Terminal fault attributable only to a user-hosted Bot connection.

    Consumers may use ``affects_docker_health`` to make the operational boundary
    explicit: this exception can decide a match, but must never pause Docker or
    mark the platform sandbox unhealthy.
    """

    # Keep the public match terminal inside the existing completed-reason
    # contract.  ``error_code`` and ``runtime_family`` carry the more precise
    # diagnosis without inventing a second public terminal state.
    reason = "technical_loss"
    affects_docker_health = False
    runtime_family = "local_ai"


@dataclass(frozen=True, slots=True)
class LocalAITurn:
    """One immutable request sent to a connector."""

    phase: Literal["prepare", "decision"]
    request_id: str
    match_id: str
    agent_id: str
    seat: int
    turn: int
    deadline_at: float
    input: Any


@dataclass(frozen=True, slots=True)
class LocalAIConnection:
    agent_id: str
    connection_id: str
    connected_at: float


@dataclass(frozen=True, slots=True)
class LocalAIStatus:
    agent_id: str
    state: Literal["offline", "online", "busy", "revoked"]
    online: bool
    busy: bool
    revoked: bool
    connection_id: str | None
    connected_at: float | None
    last_seen_at: float | None
    pending_request_id: str | None
    pending_match_id: str | None
    pending_turn: int | None
    pending_deadline_at: float | None


@dataclass(frozen=True, slots=True)
class LocalAIResponseAcceptance:
    request_id: str
    match_id: str
    turn: int


@dataclass(slots=True)
class _ConnectionState:
    connection_id: str
    connected_at: float
    last_seen_at: float
    queue: asyncio.Queue[object] = field(default_factory=asyncio.Queue)


@dataclass(slots=True)
class _PendingTurn:
    message: LocalAITurn
    response: asyncio.Future[Any]
    decision_started: asyncio.Future[float] | None = None
    decision_timeout: float | None = None
    prepared_input: Any = None
    decision_started_at: float | None = None
    decision_ended_at: float | None = None


@dataclass(slots=True)
class _AgentState:
    agent_id: str
    connection: _ConnectionState | None = None
    pending: _PendingTurn | None = None
    revoked: bool = False


@dataclass(frozen=True, slots=True)
class _TerminalRequest:
    agent_id: str
    match_id: str
    turn: int
    outcome: Literal["accepted", "timed_out", "revoked", "cancelled", "failed"]


_CONNECTION_CLOSED = object()


class LocalAIHub:
    """In-memory rendezvous between match execution and outbound connectors.

    The hub is intended to live on one asyncio event loop.  Authentication and
    persistence remain responsibilities of the API and store layers.
    """

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        terminal_history_size: int = 1024,
        revoked_history_size: int = 1024,
        revoked_ttl_seconds: float = 300.0,
    ) -> None:
        if terminal_history_size < 1:
            raise ValueError("terminal_history_size 必须大于 0")
        if revoked_history_size < 1:
            raise ValueError("revoked_history_size 必须大于 0")
        if revoked_ttl_seconds <= 0:
            raise ValueError("revoked_ttl_seconds 必须大于 0")
        self._clock = clock
        self._terminal_history_size = int(terminal_history_size)
        self._revoked_history_size = int(revoked_history_size)
        self._revoked_ttl_seconds = float(revoked_ttl_seconds)
        self._agents: dict[str, _AgentState] = {}
        self._revoked: OrderedDict[str, float] = OrderedDict()
        self._terminal: OrderedDict[str, _TerminalRequest] = OrderedDict()
        self._seen_request_ids: set[str] = set()
        self._lock = asyncio.Lock()

    async def register(
        self, agent_id: str, *, connection_id: str | None = None
    ) -> LocalAIConnection:
        """Register a newly authenticated outbound connector.

        Only an offline agent may reconnect.  If a decision is pending it is
        re-delivered unchanged to the new connection.
        """

        agent_id = self._required_text(agent_id, "agent_id")
        connection_id = self._required_text(
            connection_id or secrets.token_urlsafe(18), "connection_id"
        )
        now = self._clock()
        async with self._lock:
            self._expire_revoked_locked(now)
            if agent_id in self._revoked:
                raise LocalAIConnectionError("agent_revoked")
            state = self._agents.setdefault(agent_id, _AgentState(agent_id))
            if state.connection is not None:
                raise LocalAIConnectionError("agent_already_connected")
            connection = _ConnectionState(connection_id, now, now)
            state.connection = connection
            if state.pending is not None:
                connection.queue.put_nowait(
                    self._copy_turn(state.pending.message)
                )
            return LocalAIConnection(agent_id, connection_id, now)

    async def heartbeat(self, agent_id: str, connection_id: str) -> None:
        """Mark an authenticated connector as alive."""

        async with self._lock:
            connection = self._current_connection_locked(agent_id, connection_id)
            connection.last_seen_at = self._clock()

    async def close(self, agent_id: str, connection_id: str) -> bool:
        """Close one live transport without cancelling its pending decision.

        Repeating a close for an already-offline agent is harmless.  A stale
        connection id can never close a newer connection.
        """

        async with self._lock:
            state = self._agents.get(agent_id)
            if state is None or state.connection is None:
                return False
            if state.connection.connection_id != connection_id:
                raise LocalAIConnectionError("stale_connection")
            self._close_current_connection_locked(state)
            return True

    async def revoke(self, agent_id: str) -> None:
        """Revoke live state and retain one bounded reconnect tombstone.

        A currently pending match decision fails immediately as a local-AI
        technical fault.  Durable revocation remains the store/API layer's
        responsibility; the TTL/LRU tombstone only closes the in-process race
        window without allowing an unbounded attacker-controlled history.
        """

        agent_id = self._required_text(agent_id, "agent_id")
        async with self._lock:
            now = self._clock()
            self._expire_revoked_locked(now)
            state = self._agents.pop(agent_id, None)
            self._revoked[agent_id] = now
            self._revoked.move_to_end(agent_id)
            while len(self._revoked) > self._revoked_history_size:
                self._revoked.popitem(last=False)
            if state is None:
                return
            if state.connection is not None:
                self._close_current_connection_locked(state)
            if state.pending is not None:
                self._fail_pending_locked(
                    state,
                    error_code="local_ai_revoked",
                    message="本地 Bot 连接已被撤销",
                    outcome="revoked",
                )

    async def status(self, agent_id: str) -> LocalAIStatus:
        """Return a payload-safe online/busy snapshot without request content."""

        async with self._lock:
            self._expire_revoked_locked(self._clock())
            state = self._agents.get(agent_id)
            revoked = agent_id in self._revoked
            if revoked:
                return LocalAIStatus(
                    agent_id=agent_id,
                    state="revoked",
                    online=False,
                    busy=False,
                    revoked=True,
                    connection_id=None,
                    connected_at=None,
                    last_seen_at=None,
                    pending_request_id=None,
                    pending_match_id=None,
                    pending_turn=None,
                    pending_deadline_at=None,
                )
            if state is None:
                return LocalAIStatus(
                    agent_id=agent_id,
                    state="offline",
                    online=False,
                    busy=False,
                    revoked=False,
                    connection_id=None,
                    connected_at=None,
                    last_seen_at=None,
                    pending_request_id=None,
                    pending_match_id=None,
                    pending_turn=None,
                    pending_deadline_at=None,
                )
            connection = state.connection
            pending = state.pending.message if state.pending is not None else None
            online = connection is not None
            busy = pending is not None
            display_state: Literal["offline", "online", "busy", "revoked"]
            if not online:
                display_state = "offline"
            elif busy:
                display_state = "busy"
            else:
                display_state = "online"
            return LocalAIStatus(
                agent_id=agent_id,
                state=display_state,
                online=online,
                busy=busy,
                revoked=False,
                connection_id=connection.connection_id if connection else None,
                connected_at=connection.connected_at if connection else None,
                last_seen_at=connection.last_seen_at if connection else None,
                pending_request_id=pending.request_id if pending else None,
                pending_match_id=pending.match_id if pending else None,
                pending_turn=pending.turn if pending else None,
                pending_deadline_at=pending.deadline_at if pending else None,
            )

    def available_now(self, agent_id: str) -> bool:
        """Return a non-awaiting event-loop-local availability snapshot.

        The execution repository invokes this while selecting one durable job.
        It must not await (and therefore cannot let another request claim the
        same connector between the check and lease insertion).  Callers still
        acquire the durable lease in the same SQLite claim transaction.
        """

        state = self._agents.get(str(agent_id))
        return bool(
            state is not None
            and str(agent_id) not in self._revoked
            and state.connection is not None
            and state.pending is None
        )

    async def shutdown(self) -> None:
        """Wake transports and pending match decisions during app shutdown."""

        async with self._lock:
            agent_ids = list(self._agents)
        for agent_id in agent_ids:
            await self.revoke(agent_id)

    async def next_turn(
        self,
        agent_id: str,
        connection_id: str,
        *,
        timeout: float | None = None,
    ) -> LocalAITurn | None:
        """Wait for the next turn message (WebSocket receive or REST long-poll).

        ``None`` means only that the optional transport wait timed out; it is not
        a Bot decision timeout.  Closing/replacing the connection wakes waiters.
        """

        if timeout is not None and timeout < 0:
            raise ValueError("timeout 不能为负数")
        async with self._lock:
            connection = self._current_connection_locked(agent_id, connection_id)
            queue = connection.queue
        loop = asyncio.get_running_loop()
        wait_deadline = None if timeout is None else loop.time() + timeout
        while True:
            try:
                if wait_deadline is None:
                    item = await queue.get()
                else:
                    remaining = wait_deadline - loop.time()
                    if remaining <= 0:
                        try:
                            item = queue.get_nowait()
                        except asyncio.QueueEmpty:
                            return None
                    else:
                        item = await asyncio.wait_for(queue.get(), timeout=remaining)
            except TimeoutError:
                return None
            if item is _CONNECTION_CLOSED:
                raise LocalAIConnectionError("connection_closed")
            if not isinstance(item, LocalAITurn):  # pragma: no cover - internal guard
                raise RuntimeError("unexpected local AI queue item")
            async with self._lock:
                connection = self._current_connection_locked(
                    agent_id, connection_id
                )
                state = self._agents[agent_id]
                if not self._queued_turn_is_current_locked(state, item):
                    # A terminal/cancel operation can win after queue.get() but
                    # before this lock.  Never deliver that closed request or
                    # its private input to a connector now serving a later job.
                    continue
                connection.last_seen_at = self._clock()
                return item

    async def request_decision(
        self,
        agent_id: str,
        *,
        request_id: str,
        match_id: str,
        seat: int,
        turn: int,
        deadline_at: float | None = None,
        input: Any,
        decision_timeout: float | None = None,
        on_decision_elapsed: Callable[[float], None] | None = None,
    ) -> Any:
        """Deliver one turn and await the exactly-bound response.

        Existing connector callers may supply an absolute ``deadline_at``.
        Match execution supplies ``decision_timeout`` instead and uses a secure
        two-phase hand-off.  The first, independently bounded ``prepare`` frame
        contains no game input.  After the connector confirms that its process
        exists, the hub freezes the game deadline and only then queues the full
        ``decision`` frame.  Startup therefore cannot consume the chess clock,
        while an untrusted connector cannot inspect or compute the position
        before that clock starts.  Reconnect after decision delivery keeps the
        same absolute deadline.
        """

        agent_id = self._required_text(agent_id, "agent_id")
        request_id = self._required_text(request_id, "request_id")
        match_id = self._required_text(match_id, "match_id")
        if seat not in (0, 1):
            raise ValueError("seat 必须是 0/1")
        if int(turn) < 1:
            raise ValueError("turn 必须从 1 开始")
        turn = int(turn)
        if (deadline_at is None) == (decision_timeout is None):
            raise ValueError("deadline_at 与 decision_timeout 必须且只能提供一个")
        relative_timeout: float | None = None
        if decision_timeout is not None:
            if isinstance(decision_timeout, bool):
                raise ValueError("decision_timeout 必须为有限正数")
            relative_timeout = float(decision_timeout)
            if not math.isfinite(relative_timeout) or relative_timeout <= 0:
                raise ValueError("decision_timeout 必须为有限正数")
        else:
            deadline_at = float(deadline_at)
        loop = asyncio.get_running_loop()
        decision_started_at: float | None = None
        pending: _PendingTurn | None = None

        async with self._lock:
            state = self._agents.get(agent_id)
            if state is None or agent_id in self._revoked:
                raise self._technical_error(
                    "本地 Bot 当前未连接",
                    error_code="local_ai_unavailable",
                    seat=seat,
                    turn=turn,
                )
            if state.pending is not None:
                raise LocalAIBusyError("agent_busy")
            if request_id in self._seen_request_ids:
                raise LocalAIHubError("request_id_already_used")
            self._seen_request_ids.add(request_id)
            if relative_timeout is None and deadline_at <= self._clock():
                self._remember_terminal_locked(
                    request_id,
                    _TerminalRequest(agent_id, match_id, turn, "timed_out"),
                )
                raise self._technical_error(
                    "本地 Bot 未在决策截止时间前响应",
                    error_code="local_ai_timeout",
                    seat=seat,
                    turn=turn,
                )
            # Copy the private input before publishing any pending state.  A
            # relative Match request deliberately does not place this value in
            # the prepare frame, so the connector cannot pre-compute off-clock.
            pending_input = copy.deepcopy(input)
            response: asyncio.Future[Any] = loop.create_future()
            if relative_timeout is not None:
                prepare_deadline_at = self._clock() + LOCAL_AI_PREPARE_TIMEOUT_SEC
                decision_started = loop.create_future()
                message = LocalAITurn(
                    phase="prepare",
                    request_id=request_id,
                    match_id=match_id,
                    agent_id=agent_id,
                    seat=seat,
                    turn=turn,
                    deadline_at=prepare_deadline_at,
                    input=None,
                )
                pending = _PendingTurn(
                    message,
                    response,
                    decision_started=decision_started,
                    decision_timeout=relative_timeout,
                    prepared_input=pending_input,
                )
            else:
                assert deadline_at is not None
                message = LocalAITurn(
                    phase="decision",
                    request_id=request_id,
                    match_id=match_id,
                    agent_id=agent_id,
                    seat=seat,
                    turn=turn,
                    deadline_at=deadline_at,
                    input=pending_input,
                )
                pending = _PendingTurn(message, response)
            state.pending = pending
            if state.connection is not None:
                state.connection.queue.put_nowait(self._copy_turn(message))

        try:
            if pending.decision_started is not None:
                prepare_delay = max(
                    0.0, pending.message.deadline_at - self._clock()
                )
                done, _ = await asyncio.wait(
                    (pending.decision_started, response),
                    timeout=prepare_delay,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if response in done:
                    return await response
                if pending.decision_started not in done:
                    async with self._lock:
                        state = self._agents.get(agent_id)
                        if state is not None and state.pending is pending:
                            self._fail_pending_locked(
                                state,
                                error_code="local_ai_unavailable",
                                message="本地 Bot 未在准备时限内就绪",
                                outcome="failed",
                            )
                            self._close_current_connection_locked(state)
                    return await response
                deadline_at = pending.decision_started.result()
                decision_started_at = pending.decision_started_at
            assert deadline_at is not None
            delay = max(0.0, deadline_at - self._clock())
            return await asyncio.wait_for(asyncio.shield(response), timeout=delay)
        except TimeoutError:
            async with self._lock:
                state = self._agents.get(agent_id)
                if state is not None and state.pending is pending:
                    if pending.decision_started_at is not None:
                        pending.decision_ended_at = deadline_at
                    self._discard_queued_request_locked(state, request_id)
                    state.pending = None
                    self._remember_terminal_locked(
                        request_id,
                        _TerminalRequest(agent_id, match_id, turn, "timed_out"),
                    )
                    if not response.done():
                        response.cancel()
                    # A decision frame may already be in the connector.  The
                    # sequential v2 client has no independent cancel channel,
                    # so close this exact transport to wake its wait_closed
                    # watcher and kill the old process before the agent can be
                    # considered available for another Match.
                    self._close_current_connection_locked(state)
            raise self._technical_error(
                "本地 Bot 未在决策截止时间前响应",
                error_code="local_ai_timeout",
                seat=seat,
                turn=turn,
            ) from None
        except asyncio.CancelledError:
            async with self._lock:
                state = self._agents.get(agent_id)
                if state is not None and state.pending is pending:
                    if pending.decision_started_at is not None:
                        pending.decision_ended_at = self._clock()
                    self._discard_queued_request_locked(state, request_id)
                    state.pending = None
                    self._remember_terminal_locked(
                        request_id,
                        _TerminalRequest(agent_id, match_id, turn, "cancelled"),
                    )
                    if not response.done():
                        response.cancel()
                    self._close_current_connection_locked(state)
            raise
        finally:
            if decision_started_at is None and pending is not None:
                decision_started_at = pending.decision_started_at
            decision_ended_at = (
                pending.decision_ended_at if pending is not None else None
            )
            if (
                decision_started_at is not None
                and decision_ended_at is not None
                and on_decision_elapsed is not None
            ):
                on_decision_elapsed(
                    max(0.0, decision_ended_at - decision_started_at)
                )
            # A terminal hub operation can win the lock immediately before the
            # caller is cancelled.  Retrieve its internal Future exception so
            # asyncio never reports an unhandled technical fault; the caller's
            # cancellation still remains authoritative.
            if pending is not None:
                if pending.response.done() and not pending.response.cancelled():
                    pending.response.exception()
                if (
                    pending.decision_started is not None
                    and not pending.decision_started.done()
                ):
                    pending.decision_started.cancel()

    async def mark_prepared(
        self,
        agent_id: str,
        connection_id: str,
        *,
        request_id: Any,
        match_id: Any,
        turn: Any,
    ) -> LocalAIResponseAcceptance:
        """Start one relative decision clock and release its private input.

        The exact binding and current connection are checked under the same hub
        lock that changes the phase, so duplicate/stale preparation frames can
        neither extend a deadline nor receive the hidden request.
        """

        async with self._lock:
            state, pending, request_id, match_id, turn = self._bound_pending_locked(
                agent_id,
                connection_id,
                request_id=request_id,
                match_id=match_id,
                turn=turn,
                require_decision=False,
            )
            if pending.message.phase != "prepare":
                raise LocalAIResponseRejected("decision_already_started")
            if pending.decision_started is None or pending.decision_timeout is None:
                raise LocalAIResponseRejected("invalid_pending_phase")
            prepared_at = self._clock()
            if prepared_at >= pending.message.deadline_at:
                self._fail_pending_locked(
                    state,
                    error_code="local_ai_unavailable",
                    message="本地 Bot 未在准备时限内就绪",
                    outcome="failed",
                )
                self._close_current_connection_locked(state)
                raise LocalAIResponseRejected("preparation_deadline_exceeded")
            # This second defensive copy remains platform preparation.  Finish
            # it before freezing the authoritative game clock.
            delivery_input = copy.deepcopy(pending.prepared_input)
            now = self._clock()
            deadline_at = now + pending.decision_timeout
            decision = LocalAITurn(
                phase="decision",
                request_id=request_id,
                match_id=match_id,
                agent_id=agent_id,
                seat=pending.message.seat,
                turn=turn,
                deadline_at=deadline_at,
                input=delivery_input,
            )
            pending.message = decision
            pending.prepared_input = None
            pending.decision_started_at = now
            self._discard_queued_request_locked(state, request_id)
            state.connection.queue.put_nowait(self._copy_turn(decision))
            if not pending.decision_started.done():
                pending.decision_started.set_result(deadline_at)
            return LocalAIResponseAcceptance(request_id, match_id, turn)

    async def submit_response(
        self,
        agent_id: str,
        connection_id: str,
        *,
        request_id: Any,
        match_id: Any,
        turn: Any,
        output: Any,
    ) -> LocalAIResponseAcceptance:
        """Accept one response only when all binding fields and deadline match."""

        async with self._lock:
            state, pending, request_id, match_id, turn = self._bound_pending_locked(
                agent_id,
                connection_id,
                request_id=request_id,
                match_id=match_id,
                turn=turn,
            )

            pending.decision_ended_at = self._clock()
            self._discard_queued_request_locked(state, request_id)
            state.pending = None
            self._remember_terminal_locked(
                request_id,
                _TerminalRequest(agent_id, match_id, int(turn), "accepted"),
            )
            if not pending.response.done():
                pending.response.set_result(copy.deepcopy(output))
            return LocalAIResponseAcceptance(request_id, match_id, int(turn))

    async def submit_failure(
        self,
        agent_id: str,
        connection_id: str,
        *,
        request_id: Any,
        match_id: Any,
        turn: Any,
        reason: Any,
    ) -> LocalAIResponseAcceptance:
        """Fail exactly one bound turn from a stable, detail-free client reason."""

        if not isinstance(reason, str) or reason not in _LOCAL_AI_CLIENT_FAILURES:
            raise LocalAIResponseRejected("invalid_failure_reason")
        error_code, public_message = _LOCAL_AI_CLIENT_FAILURES[reason]
        async with self._lock:
            state, pending, request_id, match_id, turn = self._bound_pending_locked(
                agent_id,
                connection_id,
                request_id=request_id,
                match_id=match_id,
                turn=turn,
                require_decision=False,
            )
            if pending.message.phase == "prepare" and reason != "bot_start_failed":
                raise LocalAIResponseRejected("invalid_failure_phase")
            self._fail_pending_locked(
                state,
                error_code=error_code,
                message=public_message,
                outcome="failed",
            )
            return LocalAIResponseAcceptance(request_id, match_id, turn)

    def _bound_pending_locked(
        self,
        agent_id: str,
        connection_id: str,
        *,
        request_id: Any,
        match_id: Any,
        turn: Any,
        require_decision: bool = True,
    ) -> tuple[_AgentState, _PendingTurn, str, str, int]:
        """Resolve the one pending request shared by response and failure frames."""

        connection = self._current_connection_locked(agent_id, connection_id)
        connection.last_seen_at = self._clock()
        request_id = self._binding_text(request_id)
        match_id = self._binding_text(match_id)
        turn = self._binding_turn(turn)
        state = self._agents[agent_id]
        pending = state.pending
        if pending is None:
            terminal = self._terminal.get(request_id)
            if terminal is not None:
                if (
                    terminal.agent_id != agent_id
                    or terminal.match_id != match_id
                    or terminal.turn != turn
                ):
                    raise LocalAIResponseRejected("request_binding_mismatch")
                if terminal.outcome == "accepted":
                    raise LocalAIResponseRejected("duplicate_response")
                if terminal.outcome == "timed_out":
                    raise LocalAIResponseRejected("deadline_exceeded")
                raise LocalAIResponseRejected("request_closed")
            raise LocalAIResponseRejected("no_pending_request")

        message = pending.message
        if (
            message.request_id != request_id
            or message.match_id != match_id
            or message.turn != turn
        ):
            raise LocalAIResponseRejected("request_binding_mismatch")
        if require_decision and message.phase != "decision":
            raise LocalAIResponseRejected("decision_not_started")
        if self._clock() >= message.deadline_at:
            preparing = message.phase == "prepare"
            self._fail_pending_locked(
                state,
                error_code=("local_ai_unavailable" if preparing else "local_ai_timeout"),
                message=(
                    "本地 Bot 未在准备时限内就绪"
                    if preparing
                    else "本地 Bot 未在决策截止时间前响应"
                ),
                outcome=("failed" if preparing else "timed_out"),
                ended_at=(None if preparing else message.deadline_at),
            )
            self._close_current_connection_locked(state)
            raise LocalAIResponseRejected(
                "preparation_deadline_exceeded" if preparing else "deadline_exceeded"
            )
        return state, pending, request_id, match_id, turn

    def _current_connection_locked(
        self, agent_id: str, connection_id: str
    ) -> _ConnectionState:
        self._expire_revoked_locked(self._clock())
        state = self._agents.get(agent_id)
        if state is None or agent_id in self._revoked:
            raise LocalAIConnectionError("agent_revoked_or_unknown")
        connection = state.connection
        if connection is None:
            raise LocalAIConnectionError("agent_offline")
        if connection.connection_id != connection_id:
            raise LocalAIConnectionError("stale_connection")
        return connection

    def _fail_pending_locked(
        self,
        state: _AgentState,
        *,
        error_code: str,
        message: str,
        outcome: Literal["timed_out", "revoked", "failed"],
        ended_at: float | None = None,
    ) -> None:
        pending = state.pending
        if pending is None:
            return
        if pending.decision_started_at is not None:
            pending.decision_ended_at = (
                float(ended_at) if ended_at is not None else self._clock()
            )
        self._discard_queued_request_locked(state, pending.message.request_id)
        state.pending = None
        turn = pending.message
        self._remember_terminal_locked(
            turn.request_id,
            _TerminalRequest(
                state.agent_id, turn.match_id, turn.turn, outcome
            ),
        )
        if not pending.response.done():
            pending.response.set_exception(
                self._technical_error(
                    message,
                    error_code=error_code,
                    seat=turn.seat,
                    turn=turn.turn,
                )
            )

    @staticmethod
    def _queued_turn_is_current_locked(
        state: _AgentState, item: LocalAITurn
    ) -> bool:
        pending = state.pending
        if pending is None:
            return False
        current = pending.message
        return (
            item.agent_id == state.agent_id == current.agent_id
            and item.request_id == current.request_id
            and item.match_id == current.match_id
            and item.seat == current.seat
            and item.turn == current.turn
            and item.phase == current.phase
            and item.deadline_at == current.deadline_at
        )

    @staticmethod
    def _discard_queued_request_locked(
        state: _AgentState, request_id: str
    ) -> None:
        connection = state.connection
        if connection is None:
            return
        retained: list[object] = []
        while True:
            try:
                item = connection.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if isinstance(item, LocalAITurn) and item.request_id == request_id:
                continue
            retained.append(item)
        for item in retained:
            connection.queue.put_nowait(item)

    @staticmethod
    def _close_current_connection_locked(state: _AgentState) -> None:
        connection = state.connection
        if connection is None:
            return
        state.connection = None
        LocalAIHub._drain_queue(connection.queue)
        connection.queue.put_nowait(_CONNECTION_CLOSED)

    def _remember_terminal_locked(
        self, request_id: str, item: _TerminalRequest
    ) -> None:
        self._terminal[request_id] = item
        self._terminal.move_to_end(request_id)
        while len(self._terminal) > self._terminal_history_size:
            evicted_request_id, _ = self._terminal.popitem(last=False)
            # The replay-protection set follows the same bounded horizon as the
            # terminal cache. Otherwise every decision handled by a long-lived
            # service would remain resident forever.
            self._seen_request_ids.discard(evicted_request_id)

    def _expire_revoked_locked(self, now: float) -> None:
        cutoff = float(now) - self._revoked_ttl_seconds
        while self._revoked:
            _, revoked_at = next(iter(self._revoked.items()))
            if revoked_at > cutoff:
                break
            self._revoked.popitem(last=False)

    @staticmethod
    def _technical_error(
        message: str, *, error_code: str, seat: int, turn: int
    ) -> LocalAITechnicalError:
        return LocalAITechnicalError(
            message,
            error_code=error_code,
            failed_seat=seat,
            turn=turn,
        )

    @staticmethod
    def _required_text(value: str, name: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{name} 不能为空")
        return value.strip()

    @staticmethod
    def _binding_text(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value
            or value != value.strip()
            or len(value) > 128
        ):
            raise LocalAIResponseRejected("invalid_binding")
        return value

    @staticmethod
    def _binding_turn(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise LocalAIResponseRejected("invalid_binding")
        return value

    @staticmethod
    def _drain_queue(queue: asyncio.Queue[object]) -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    @staticmethod
    def _copy_turn(turn: LocalAITurn) -> LocalAITurn:
        return LocalAITurn(
            phase=turn.phase,
            request_id=turn.request_id,
            match_id=turn.match_id,
            agent_id=turn.agent_id,
            seat=turn.seat,
            turn=turn.turn,
            deadline_at=turn.deadline_at,
            input=copy.deepcopy(turn.input),
        )


__all__ = [
    "LOCAL_AI_CLIENT_FAILURE_REASONS",
    "LOCAL_AI_WEBSOCKET_SUBPROTOCOL",
    "LocalAIBusyError",
    "LocalAIConnection",
    "LocalAIConnectionError",
    "LocalAIHub",
    "LocalAIHubError",
    "LocalAIResponseAcceptance",
    "LocalAIResponseRejected",
    "LocalAIStatus",
    "LocalAITechnicalError",
    "LocalAITurn",
]
