"""Pre-auth admission limits for the browser human-play WebSocket."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bzplat.backend.auth.auth_manager import COOKIE_NAME
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.runtime import config as runtime_config
from bzplat.backend.runtime.websocket_gate import WebSocketHandshakeGate


ORIGIN = "http://testserver"


def test_human_handshake_limits_are_exported_code_configuration() -> None:
    expected = {
        "HUMAN_WS_HANDSHAKE_MAX_ATTEMPTS": 30,
        "HUMAN_WS_HANDSHAKE_WINDOW_SECONDS": 60.0,
        "HUMAN_WS_HANDSHAKE_MAX_INFLIGHT": 16,
        "HUMAN_WS_HANDSHAKE_MAX_BUCKETS": 2048,
    }
    for name, value in expected.items():
        assert name in runtime_config.__all__
        assert getattr(runtime_config, name) == value


def _new_user(store, username: str) -> dict:
    user = store.create_user(
        username,
        f"{username}@example.test",
        hash_password("password1"),
    )
    store.update_user(int(user["id"]), email_verified=1)
    return user


def _new_human_match(store, owner: dict, match_id: str) -> None:
    bot = store.create_bot(
        int(owner["id"]),
        f"{match_id}-bot",
        binary_path=f"/tmp/{match_id}-bot",
        format="elf",
        game_id="gomoku",
    )
    store.create_match(
        match_id,
        int(bot["id"]),
        int(bot["id"]),
        owner_id=int(owner["id"]),
        match_type="human",
        game_id="gomoku",
        human_user_id=int(owner["id"]),
        human_seat=1,
    )


def _assert_forbidden(
    client: TestClient,
    match_id: str,
    *,
    cookie: str | None = None,
    real_ip: str = "198.51.100.24",
    origin: str = ORIGIN,
) -> dict:
    headers = {"origin": origin, "x-real-ip": real_ip}
    if cookie is not None:
        headers["cookie"] = f"{COOKIE_NAME}={cookie}"
    with client.websocket_connect(
        f"/api/matches/{match_id}/play",
        headers=headers,
    ) as websocket:
        rejection = websocket.receive_json()
        with pytest.raises(WebSocketDisconnect) as closed:
            websocket.receive_json()
    assert rejection == {
        "type": "reject",
        "reason": "forbidden",
        "message": "无权访问该对局",
    }
    assert closed.value.code == 1008
    assert closed.value.reason == "forbidden"
    return rejection


def test_websocket_handshake_gate_hard_caps_keys_and_reclaims_expired() -> None:
    async def scenario() -> None:
        gate = WebSocketHandshakeGate(
            max_attempts=2,
            window_seconds=60.0,
            max_inflight=2,
            max_buckets=2,
        )
        assert await gate.begin("192.0.2.1", now=0.0) is True
        await gate.end()
        assert await gate.begin("192.0.2.2", now=0.0) is True
        await gate.end()

        # Every retained key is still active. A new address must fail closed
        # without first inserting a third bucket.
        assert await gate.begin("192.0.2.3", now=1.0) is False
        assert set(gate._hits) == {"192.0.2.1", "192.0.2.2"}
        assert gate._inflight == 0

        # Once the original window expires, admission may reclaim those keys.
        assert await gate.begin("192.0.2.3", now=61.0) is True
        assert set(gate._hits) == {"192.0.2.3"}
        await gate.end()
        assert gate._inflight == 0

    asyncio.run(scenario())


def test_websocket_handshake_gate_caps_global_inflight_without_new_key() -> None:
    async def scenario() -> None:
        gate = WebSocketHandshakeGate(
            max_attempts=20,
            window_seconds=60.0,
            max_inflight=2,
            max_buckets=8,
        )
        assert await gate.begin("192.0.2.1", now=0.0) is True
        assert await gate.begin("192.0.2.2", now=0.0) is True
        assert await gate.begin("192.0.2.3", now=0.0) is False
        assert "192.0.2.3" not in gate._hits
        assert gate._inflight == 2
        await gate.end()
        await gate.end()
        assert gate._inflight == 0

    asyncio.run(scenario())


def test_missing_and_random_cookie_are_gated_before_authority_reads(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("BZ_TRUST_PROXY", "1")
    app = create_app(db_path=str(tmp_path / "human-handshake-invalid.db"))
    gate = WebSocketHandshakeGate(
        max_attempts=2,
        window_seconds=60.0,
        max_inflight=2,
        max_buckets=8,
    )
    app.state.human_play_handshake_gate = gate

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        reads = {"session": 0, "match": 0}
        original_verify = app.state.auth.verify_session
        original_get_match = app.state.store.get_match

        def counted_verify(token):
            reads["session"] += 1
            return original_verify(token)

        def counted_get_match(match_id):
            reads["match"] += 1
            return original_get_match(match_id)

        monkeypatch.setattr(app.state.auth, "verify_session", counted_verify)
        monkeypatch.setattr(app.state.store, "get_match", counted_get_match)

        _assert_forbidden(client, "rotated-missing")
        assert reads == {"session": 0, "match": 0}
        _assert_forbidden(client, "rotated-random", cookie="random-session")
        assert reads == {"session": 1, "match": 0}

        # A third path/cookie variation from the same trusted client identity
        # is rejected by the gate without multiplying SQLite reads.
        _assert_forbidden(client, "another-random", cookie="another-session")
        assert reads == {"session": 1, "match": 0}

        # Origin rejection remains even earlier and does not consume a gate hit.
        _assert_forbidden(
            client,
            "bad-origin",
            cookie="another-session",
            real_ip="203.0.113.7",
            origin="https://evil.example",
        )
        assert "203.0.113.7" not in gate._hits
        assert reads == {"session": 1, "match": 0}

    assert set(gate._hits) == {"198.51.100.24"}
    assert gate._inflight == 0


def test_route_global_inflight_rejection_precedes_authority_reads(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", ORIGIN)
    app = create_app(db_path=str(tmp_path / "human-handshake-inflight.db"))
    gate = WebSocketHandshakeGate(
        max_attempts=10,
        window_seconds=60.0,
        max_inflight=1,
        max_buckets=8,
    )
    app.state.human_play_handshake_gate = gate

    with TestClient(app, client=("192.0.2.44", 50000)) as client:
        assert client.portal.call(gate.begin, "held-peer") is True
        reads = {"session": 0, "match": 0}

        def counted_verify(_value):
            reads["session"] += 1
            return None

        def counted_get_match(_value):
            reads["match"] += 1
            return None

        monkeypatch.setattr(app.state.auth, "verify_session", counted_verify)
        monkeypatch.setattr(app.state.store, "get_match", counted_get_match)
        _assert_forbidden(client, "inflight-blocked", cookie="random")
        assert reads == {"session": 0, "match": 0}
        # A rejected begin owns no reservation and therefore cannot decrement
        # the unrelated slot held above.
        assert gate._inflight == 1
        client.portal.call(gate.end)
        assert gate._inflight == 0


def test_non_owner_rotating_match_ids_share_trusted_peer_budget(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("BZ_TRUST_PROXY", "1")
    app = create_app(db_path=str(tmp_path / "human-handshake-owner.db"))
    gate = WebSocketHandshakeGate(
        max_attempts=2,
        window_seconds=60.0,
        max_inflight=2,
        max_buckets=8,
    )
    app.state.human_play_handshake_gate = gate

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        store = app.state.store
        owner = _new_user(store, "handshake-owner")
        intruder = _new_user(store, "handshake-intruder")
        for match_id in ("human-owner-a", "human-owner-b", "human-owner-c"):
            _new_human_match(store, owner, match_id)
        _, token = app.state.auth.authenticate("handshake-intruder", "password1")

        reads = {"session": 0, "match": 0}
        original_verify = app.state.auth.verify_session
        original_get_match = store.get_match

        def counted_verify(value):
            reads["session"] += 1
            return original_verify(value)

        def counted_get_match(value):
            reads["match"] += 1
            return original_get_match(value)

        monkeypatch.setattr(app.state.auth, "verify_session", counted_verify)
        monkeypatch.setattr(store, "get_match", counted_get_match)
        for match_id in ("human-owner-a", "human-owner-b", "human-owner-c"):
            _assert_forbidden(client, match_id, cookie=token)

        assert reads == {"session": 2, "match": 2}
        # A different trusted peer reaches the same generic response for an
        # absent id; neither ownership, existence, nor the gate's cause leaks.
        _assert_forbidden(
            client,
            "human-does-not-exist",
            cookie=token,
            real_ip="203.0.113.88",
        )
        assert reads == {"session": 3, "match": 3}
        assert gate._inflight == 0


def test_owner_handshake_still_gets_snapshot_and_releases_gate(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", ORIGIN)
    monkeypatch.setenv("BZ_TRUST_PROXY", "1")
    app = create_app(db_path=str(tmp_path / "human-handshake-success.db"))
    gate = WebSocketHandshakeGate(
        max_attempts=2,
        window_seconds=60.0,
        max_inflight=2,
        max_buckets=8,
    )
    app.state.human_play_handshake_gate = gate

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
        store = app.state.store
        owner = _new_user(store, "handshake-success")
        _new_human_match(store, owner, "human-handshake-success")
        _, token = app.state.auth.authenticate("handshake-success", "password1")
        client.cookies.set(COOKIE_NAME, token)
        with client.websocket_connect(
            "/api/matches/human-handshake-success/play",
            headers={"origin": ORIGIN, "x-real-ip": "203.0.113.11"},
        ) as websocket:
            snapshot = websocket.receive_json()
            assert snapshot["type"] == "snapshot"
            assert gate._inflight == 0
            assert set(gate._hits) == {"203.0.113.11"}

    assert gate._inflight == 0


def test_handshake_exception_releases_inflight_reservation(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", ORIGIN)
    app = create_app(db_path=str(tmp_path / "human-handshake-exception.db"))
    gate = WebSocketHandshakeGate(
        max_attempts=2,
        window_seconds=60.0,
        max_inflight=1,
        max_buckets=8,
    )
    app.state.human_play_handshake_gate = gate

    def explode(_token):
        raise RuntimeError("synthetic handshake failure")

    monkeypatch.setattr(app.state.auth, "verify_session", explode)
    with TestClient(app, raise_server_exceptions=True) as client:
        with pytest.raises(RuntimeError, match="synthetic handshake failure"):
            with client.websocket_connect(
                "/api/matches/exception/play",
                headers={
                    "origin": ORIGIN,
                    "cookie": f"{COOKIE_NAME}=random",
                },
            ):
                pass
    assert gate._inflight == 0
