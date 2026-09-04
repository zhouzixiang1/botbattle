"""Resource and authorization boundaries for public match event streams."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Request

import bzplat.backend.api_routes as api_routes
from bzplat.backend.api_routes import match_events
from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.orchestrator import (
    HumanWebSocketLimitError,
    MatchOrchestrator,
    PublicSSELimitError,
)
from bzplat.backend.runtime.config import (
    HUMAN_ACTION_RATE_BURST,
    HUMAN_ACTION_RATE_BUCKET_LIMIT,
    HUMAN_ACTION_RATE_REFILL_PER_SECOND,
    HUMAN_WS_GLOBAL_LIMIT,
    HUMAN_WS_PER_MATCH_LIMIT,
    HUMAN_WS_PER_USER_LIMIT,
    PUBLIC_SSE_GLOBAL_LIMIT,
    PUBLIC_SSE_PER_IP_LIMIT,
    PUBLIC_SSE_PER_MATCH_LIMIT,
)
from bzplat.backend.store import Store


@pytest.fixture
def store(tmp_path) -> Store:
    return Store(str(tmp_path / "stream-limits.db"))


@pytest.fixture
def orch(store: Store) -> MatchOrchestrator:
    return MatchOrchestrator(store)


def _match_factory(store: Store):
    user = store.create_user(
        "stream-owner", "stream-owner@example.test", hash_password("password1")
    )
    bot = store.create_bot(
        user["id"],
        "stream-bot",
        binary_path="/tmp/stream-bot",
        format="elf",
        game_id="gomoku",
    )
    sequence = 0

    def create() -> str:
        nonlocal sequence
        sequence += 1
        match_id = f"stream-limit-{sequence}"
        store.create_match(
            match_id,
            bot["id"],
            bot["id"],
            match_type="challenge",
            game_id="gomoku",
        )
        return match_id

    return create


def test_public_sse_enforces_per_ip_before_allocating_queue(
    store: Store, orch: MatchOrchestrator
):
    make_match = _match_factory(store)
    queues = [
        orch.subscribe(make_match(), public_client_ip="198.51.100.7")
        for _ in range(PUBLIC_SSE_PER_IP_LIMIT)
    ]
    next_match = make_match()

    with pytest.raises(PublicSSELimitError):
        orch.subscribe(next_match, public_client_ip="198.51.100.7")

    assert next_match not in orch._sse
    assert len(orch._public_sse_subscriptions) == PUBLIC_SSE_PER_IP_LIMIT
    for queue, (match_id, _client_ip) in list(
        orch._public_sse_subscriptions.items()
    ):
        orch.unsubscribe(match_id, queue)
    assert queues


def test_public_sse_enforces_per_match_and_bounds_broadcast_fanout(
    store: Store, orch: MatchOrchestrator
):
    match_id = _match_factory(store)()
    queues = [
        orch.subscribe(
            match_id,
            public_client_ip=f"198.51.100.{index + 1}",
        )
        for index in range(PUBLIC_SSE_PER_MATCH_LIMIT)
    ]

    with pytest.raises(PublicSSELimitError):
        orch.subscribe(match_id, public_client_ip="203.0.113.250")

    assert len(orch._sse[match_id]) == PUBLIC_SSE_PER_MATCH_LIMIT
    for queue in queues:
        queue.get_nowait()  # snapshot
    orch._broadcast(match_id, {"type": "move", "move_index": 1})
    assert all(queue.qsize() == 1 for queue in queues)


def test_public_sse_global_limit_spans_matches_and_ips(
    store: Store, orch: MatchOrchestrator
):
    make_match = _match_factory(store)
    queues: list[tuple[str, asyncio.Queue]] = []
    for index in range(PUBLIC_SSE_GLOBAL_LIMIT):
        match_id = make_match()
        queue = orch.subscribe(
            match_id,
            public_client_ip=f"2001:db8::{index + 1}",
        )
        queues.append((match_id, queue))

    blocked_match = make_match()
    with pytest.raises(PublicSSELimitError):
        orch.subscribe(blocked_match, public_client_ip="203.0.113.1")
    assert blocked_match not in orch._sse

    for match_id, queue in queues:
        orch.unsubscribe(match_id, queue)


def test_public_sse_release_is_idempotent_and_internal_subscription_is_exempt(
    store: Store, orch: MatchOrchestrator
):
    match_id = _match_factory(store)()
    public_queues = [
        orch.subscribe(
            match_id,
            public_client_ip=f"192.0.2.{index + 1}",
        )
        for index in range(PUBLIC_SSE_PER_MATCH_LIMIT)
    ]

    # The authenticated human WebSocket and internal consumers use the existing
    # subscription path and must not consume or be rejected by public quotas.
    internal_queue = orch.subscribe(match_id, human_viewer_seat=1)
    assert len(orch._sse[match_id]) == PUBLIC_SSE_PER_MATCH_LIMIT + 1

    released = public_queues.pop()
    orch.unsubscribe(match_id, released)
    orch.unsubscribe(match_id, released)
    replacement = orch.subscribe(match_id, public_client_ip="203.0.113.9")
    assert len(orch._public_sse_subscriptions) == PUBLIC_SSE_PER_MATCH_LIMIT

    for queue in [*public_queues, replacement, internal_queue]:
        orch.unsubscribe(match_id, queue)
    assert orch._public_sse_subscriptions == {}
    assert orch._public_sse_total == 0
    assert orch._public_sse_by_match == {}
    assert orch._public_sse_by_ip == {}


def test_human_websocket_enforces_per_match_before_allocating_queue(
    store: Store, orch: MatchOrchestrator
):
    match_id = _match_factory(store)()
    queues = [
        orch.subscribe(
            match_id,
            human_viewer_seat=1,
            human_user_id=index + 1,
        )
        for index in range(HUMAN_WS_PER_MATCH_LIMIT)
    ]

    with pytest.raises(HumanWebSocketLimitError) as rejected:
        orch.subscribe(
            match_id,
            human_viewer_seat=1,
            human_user_id=HUMAN_WS_PER_MATCH_LIMIT + 1,
        )

    assert rejected.value.scope == "match"
    assert len(orch._sse[match_id]) == HUMAN_WS_PER_MATCH_LIMIT
    assert len(orch._human_ws_subscriptions) == HUMAN_WS_PER_MATCH_LIMIT
    for queue in queues:
        orch.unsubscribe(match_id, queue)


def test_human_websocket_enforces_per_user_across_matches(
    store: Store, orch: MatchOrchestrator
):
    make_match = _match_factory(store)
    user_id = 91
    queues = [
        (match_id, orch.subscribe(
            match_id,
            human_viewer_seat=1,
            human_user_id=user_id,
        ))
        for match_id in [make_match() for _ in range(HUMAN_WS_PER_USER_LIMIT)]
    ]
    rejected_match = make_match()

    with pytest.raises(HumanWebSocketLimitError) as rejected:
        orch.subscribe(
            rejected_match,
            human_viewer_seat=1,
            human_user_id=user_id,
        )

    assert rejected.value.scope == "user"
    assert rejected_match not in orch._sse
    assert orch._human_ws_by_user[user_id] == HUMAN_WS_PER_USER_LIMIT
    for match_id, queue in queues:
        orch.unsubscribe(match_id, queue)


def test_human_websocket_enforces_global_limit_and_preserves_public_meter(
    store: Store, orch: MatchOrchestrator
):
    make_match = _match_factory(store)
    queues: list[tuple[str, asyncio.Queue]] = []
    for index in range(HUMAN_WS_GLOBAL_LIMIT):
        match_id = make_match()
        queue = orch.subscribe(
            match_id,
            human_viewer_seat=1,
            human_user_id=index + 1,
        )
        queues.append((match_id, queue))

    blocked_match = make_match()
    with pytest.raises(HumanWebSocketLimitError) as rejected:
        orch.subscribe(
            blocked_match,
            human_viewer_seat=1,
            human_user_id=HUMAN_WS_GLOBAL_LIMIT + 1,
        )

    assert rejected.value.scope == "global"
    assert orch._human_ws_total == HUMAN_WS_GLOBAL_LIMIT
    assert orch._public_sse_total == 0
    for match_id, queue in queues:
        orch.unsubscribe(match_id, queue)


def test_human_websocket_release_is_idempotent_and_snapshot_failure_rolls_back(
    store: Store,
    orch: MatchOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
):
    match_id = _match_factory(store)()
    queue = orch.subscribe(
        match_id,
        human_viewer_seat=1,
        human_user_id=7,
    )
    orch.unsubscribe(match_id, queue)
    orch.unsubscribe(match_id, queue)
    assert orch._human_ws_total == 0
    assert orch._human_ws_subscriptions == {}

    def fail_replay(*_args, **_kwargs):
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(store, "get_public_replay", fail_replay)
    with pytest.raises(RuntimeError, match="snapshot failed"):
        orch.subscribe(
            match_id,
            human_viewer_seat=1,
            human_user_id=7,
        )
    assert orch._human_ws_total == 0
    assert orch._human_ws_subscriptions == {}
    assert orch._human_ws_by_match == {}
    assert orch._human_ws_by_user == {}


def test_human_websocket_shutdown_releases_all_reservations(
    store: Store, orch: MatchOrchestrator
):
    match_id = _match_factory(store)()
    orch.subscribe(
        match_id,
        human_viewer_seat=1,
        human_user_id=7,
    )

    asyncio.run(orch.shutdown())

    assert orch._human_ws_total == 0
    assert orch._human_ws_subscriptions == {}
    assert orch._human_ws_by_match == {}
    assert orch._human_ws_by_user == {}


def test_human_action_rate_is_shared_by_user_and_ip_across_connections(
    store: Store, orch: MatchOrchestrator
):
    user_id = 73
    ip = "198.51.100.73"

    for _ in range(HUMAN_ACTION_RATE_BURST):
        assert orch.consume_human_action_token(user_id, ip, now=100.0)

    # A reconnect or a second socket receives no new per-user allowance.
    assert not orch.consume_human_action_token(
        user_id, "203.0.113.73", now=100.0
    )
    # A different user on the same peer also shares the peer-IP boundary.
    assert not orch.consume_human_action_token(74, ip, now=100.0)
    assert orch.consume_human_action_token(74, "203.0.113.74", now=100.0)

    refill_at = 100.0 + (1.0 / HUMAN_ACTION_RATE_REFILL_PER_SECOND)
    assert orch.consume_human_action_token(user_id, ip, now=refill_at)


def test_human_action_rate_bucket_registry_is_bounded_and_fail_closed(
    store: Store, orch: MatchOrchestrator
):
    # Fresh identities fill the combined user/IP registry exactly to its cap.
    pairs = HUMAN_ACTION_RATE_BUCKET_LIMIT // 2
    for index in range(pairs):
        assert orch.consume_human_action_token(
            index + 1,
            f"2001:db8::{index + 1}",
            now=100.0,
        )
    assert len(orch._human_action_rate_by_user) == pairs
    assert len(orch._human_action_rate_by_ip) == pairs
    assert not orch.consume_human_action_token(
        pairs + 1,
        "2001:db8::ffff",
        now=100.0,
    )

    # Fully refilled stale buckets are reclaimed instead of growing forever.
    assert orch.consume_human_action_token(
        pairs + 1,
        "2001:db8::ffff",
        now=200.0,
    )
    assert (
        len(orch._human_action_rate_by_user)
        + len(orch._human_action_rate_by_ip)
        <= HUMAN_ACTION_RATE_BUCKET_LIMIT
    )


@pytest.mark.parametrize("invalid_user_id", [True, 1.0, "1", 0, -1])
def test_human_websocket_rejects_noncanonical_user_identity_before_reservation(
    store: Store,
    orch: MatchOrchestrator,
    invalid_user_id,
):
    match_id = _match_factory(store)()

    with pytest.raises(ValueError, match="positive integer"):
        orch.subscribe(
            match_id,
            human_viewer_seat=1,
            human_user_id=invalid_user_id,
        )

    assert match_id not in orch._sse
    assert orch._human_ws_total == 0
    assert orch._human_ws_subscriptions == {}


def test_public_sse_reservation_rolls_back_when_snapshot_fails(
    store: Store, orch: MatchOrchestrator, monkeypatch: pytest.MonkeyPatch
):
    match_id = _match_factory(store)()

    def fail_replay(*_args, **_kwargs):
        raise RuntimeError("snapshot failed")

    monkeypatch.setattr(store, "get_public_replay", fail_replay)
    with pytest.raises(RuntimeError, match="snapshot failed"):
        orch.subscribe(match_id, public_client_ip="192.0.2.8")

    assert orch._public_sse_total == 0
    assert orch._public_sse_subscriptions == {}


def test_public_sse_concurrent_admission_never_overshoots_match_limit(
    store: Store, orch: MatchOrchestrator
):
    match_id = _match_factory(store)()

    async def exercise() -> tuple[list[asyncio.Queue], int]:
        gate = asyncio.Event()

        async def connect(index: int):
            await gate.wait()
            try:
                return orch.subscribe(
                    match_id,
                    public_client_ip=f"2001:db8:1::{index + 1}",
                )
            except PublicSSELimitError:
                return None

        tasks = [asyncio.create_task(connect(index)) for index in range(48)]
        gate.set()
        results = await asyncio.gather(*tasks)
        accepted = [queue for queue in results if queue is not None]
        return accepted, len(results) - len(accepted)

    accepted, rejected = asyncio.run(exercise())
    assert len(accepted) == PUBLIC_SSE_PER_MATCH_LIMIT
    assert rejected == 48 - PUBLIC_SSE_PER_MATCH_LIMIT
    assert orch._public_sse_total == PUBLIC_SSE_PER_MATCH_LIMIT
    assert len(orch._sse[match_id]) == PUBLIC_SSE_PER_MATCH_LIMIT


def test_match_events_uses_socket_peer_and_returns_bounded_429(store: Store):
    match_id = _match_factory(store)()
    captured: dict[str, str] = {}

    class RejectingOrchestrator:
        def subscribe(self, requested_match_id, *, public_client_ip=None):
            captured.update(
                match_id=requested_match_id,
                client_ip=public_client_ip,
            )
            raise PublicSSELimitError("ip")

    app = SimpleNamespace(
        state=SimpleNamespace(
            store=store,
            orch=RejectingOrchestrator(),
            trusted_proxy_cidrs=(),
        )
    )
    request = Request({
        "type": "http",
        "method": "GET",
        "path": f"/api/matches/{match_id}/events",
        "headers": [],
        "client": ("203.0.113.44", 43210),
        "app": app,
    })

    with pytest.raises(HTTPException) as rejected:
        asyncio.run(match_events(match_id, request))

    assert rejected.value.status_code == 429
    assert rejected.value.headers == {"Retry-After": "5"}
    assert captured == {
        "match_id": match_id,
        "client_ip": "203.0.113.44",
    }


def test_match_events_disconnect_releases_public_reservation(
    store: Store, orch: MatchOrchestrator
):
    match_id = _match_factory(store)()
    app = SimpleNamespace(
        state=SimpleNamespace(
            store=store,
            orch=orch,
            trusted_proxy_cidrs=(),
        )
    )
    request = Request({
        "type": "http",
        "method": "GET",
        "path": f"/api/matches/{match_id}/events",
        "headers": [],
        "client": ("192.0.2.77", 43210),
        "app": app,
    })

    async def connect_then_disconnect() -> str:
        response = await match_events(match_id, request)
        first = await anext(response.body_iterator)
        assert orch._public_sse_total == 1
        await response.body_iterator.aclose()
        return first

    first = asyncio.run(connect_then_disconnect())
    assert '"type": "snapshot"' in first
    assert orch._public_sse_total == 0
    assert orch._public_sse_subscriptions == {}
    assert match_id not in orch._sse


def test_match_events_cancel_releases_public_reservation(
    store: Store, orch: MatchOrchestrator
):
    match_id = _match_factory(store)()
    request = Request({
        "type": "http",
        "method": "GET",
        "path": f"/api/matches/{match_id}/events",
        "headers": [],
        "client": ("192.0.2.78", 43210),
        "app": SimpleNamespace(
            state=SimpleNamespace(
                store=store,
                orch=orch,
                trusted_proxy_cidrs=(),
            )
        ),
    })

    async def cancel_waiting_stream() -> None:
        response = await match_events(match_id, request)
        await anext(response.body_iterator)  # snapshot
        waiting = asyncio.create_task(anext(response.body_iterator))
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting

    asyncio.run(cancel_waiting_stream())
    assert orch._public_sse_total == 0
    assert orch._public_sse_subscriptions == {}
    assert match_id not in orch._sse


def test_match_events_response_construction_failure_releases_reservation(
    store: Store,
    orch: MatchOrchestrator,
    monkeypatch: pytest.MonkeyPatch,
):
    match_id = _match_factory(store)()
    request = Request({
        "type": "http",
        "method": "GET",
        "path": f"/api/matches/{match_id}/events",
        "headers": [],
        "client": ("192.0.2.79", 43210),
        "app": SimpleNamespace(
            state=SimpleNamespace(
                store=store,
                orch=orch,
                trusted_proxy_cidrs=(),
            )
        ),
    })

    def fail_response(*_args, **_kwargs):
        raise RuntimeError("response construction failed")

    monkeypatch.setattr(
        api_routes,
        "_ResponseScopeCleanupStreamingResponse",
        fail_response,
    )
    with pytest.raises(RuntimeError, match="response construction failed"):
        asyncio.run(match_events(match_id, request))
    assert orch._public_sse_total == 0
    assert orch._public_sse_subscriptions == {}
    assert match_id not in orch._sse


def test_match_events_asgi23_disconnect_before_first_body_releases_reservation(
    store: Store, orch: MatchOrchestrator
):
    match_id = _match_factory(store)()
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/api/matches/{match_id}/events",
        "raw_path": f"/api/matches/{match_id}/events".encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("192.0.2.81", 43210),
        "server": ("127.0.0.1", 50381),
        "app": SimpleNamespace(
            state=SimpleNamespace(
                store=store,
                orch=orch,
                trusted_proxy_cidrs=(),
            )
        ),
    }
    request = Request(scope)

    async def disconnect_after_response_start() -> list[str]:
        response = await match_events(match_id, request)
        assert orch._public_sse_total == 1
        assert len(orch._sse[match_id]) == 1

        original_body_iterator = response.body_iterator
        first_body_requested = asyncio.Event()
        hold_first_body = asyncio.Event()
        sent_types: list[str] = []

        async def delayed_first_body():
            first_body_requested.set()
            await hold_first_body.wait()
            async for chunk in original_body_iterator:
                yield chunk

        response.body_iterator = delayed_first_body()

        async def receive():
            await first_body_requested.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            sent_types.append(message["type"])

        await response(scope, receive, send)
        return sent_types

    sent_types = asyncio.run(disconnect_after_response_start())
    assert sent_types == ["http.response.start"]
    assert orch._public_sse_total == 0
    assert orch._public_sse_subscriptions == {}
    assert orch._public_sse_by_match == {}
    assert orch._public_sse_by_ip == {}
    assert match_id not in orch._sse

    replacement = orch.subscribe(match_id, public_client_ip="192.0.2.81")
    assert orch._public_sse_total == 1
    assert orch._sse[match_id] == [replacement]
    orch.unsubscribe(match_id, replacement)


def test_match_task_cleanup_does_not_release_a_still_open_http_stream(
    store: Store, orch: MatchOrchestrator
):
    match_id = _match_factory(store)()
    queue = orch.subscribe(match_id, public_client_ip="192.0.2.80")

    asyncio.run(orch._finish_match_task(match_id, None))

    assert match_id not in orch._sse
    assert orch._public_sse_total == 1
    orch.unsubscribe(match_id, queue)
    assert orch._public_sse_total == 0
