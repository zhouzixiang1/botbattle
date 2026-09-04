"""Linearization contracts for Bot/account disable and Local-AI teardown."""

from __future__ import annotations

import asyncio
import functools
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Callable

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.runtime.local_ai import (
    LocalAIConnectionError,
    LocalAITechnicalError,
)
from bzplat.backend.store.db import Store


PASSWORD = "pw123456"


def _user(app, username: str, *, role: str = "user") -> dict[str, Any]:
    user = app.state.store.create_user(
        username,
        f"{username}@example.invalid",
        hash_password(PASSWORD),
        role=role,
    )
    return app.state.store.update_user(
        int(user["id"]), email_verified=1, is_active=1
    )


def _bot(app, owner: dict[str, Any], name: str) -> dict[str, Any]:
    binary = Path(app.state.store.path).parent / f"{name}.elf"
    binary.write_bytes(b"local-ai-revocation-test")
    bot = app.state.store.create_bot(
        int(owner["id"]),
        name,
        binary_path=str(binary),
        format="elf",
        game_id="gomoku",
    )
    app.state.store.add_bot_version(int(bot["id"]), binary_path=str(binary))
    return bot


def _auth(app, username: str) -> dict[str, str]:
    _, token = app.state.auth.authenticate(username, PASSWORD)
    return {"Authorization": f"Bearer {token}"}


async def _create_connected_agent(
    app,
    *,
    owner_id: int,
    bot_id: int,
    label: str,
) -> dict[str, Any]:
    projected, _token = await app.state.local_ai_service.create(
        owner_id=int(owner_id), bot_id=int(bot_id), label=label
    )
    agent = app.state.store.get_local_ai_agent(int(projected["id"]))
    assert agent is not None
    connection, generation = await app.state.local_ai_service.connect(agent)
    assert (await app.state.local_ai_service.hub.status(agent["public_id"])).online
    return {
        **agent,
        "_connection_id": connection.connection_id,
        "_connection_generation": generation,
    }


class _PreimageBarrier:
    """Pause once after the legacy list snapshot or before the fixed mutation."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self._lock = threading.Lock()
        self._used = False

    def wait_once(self) -> None:
        with self._lock:
            if self._used:
                return
            self._used = True
        self.entered.set()
        if not self.release.wait(timeout=10):
            raise AssertionError("disable request did not receive barrier release")


def _install_legacy_snapshot_barrier(
    monkeypatch: pytest.MonkeyPatch,
    *,
    list_owner: Any,
    list_name: str,
    mutation_owner: Any,
    mutation_name: str,
) -> _PreimageBarrier:
    """Expose the preimage race without requiring production test hooks.

    On the vulnerable route the list wrapper pauses after returning the stale
    snapshot.  Once the route stops doing that read, the mutation wrapper keeps
    the same deterministic ordering by pausing immediately before its Store
    transaction, so the regression continues to test the public contract.
    """

    gate = _PreimageBarrier()
    original_list = getattr(list_owner, list_name)
    original_mutation = getattr(mutation_owner, mutation_name)

    def gated_list(*args, **kwargs):
        result = original_list(*args, **kwargs)
        gate.wait_once()
        return result

    def gated_mutation(*args, **kwargs):
        gate.wait_once()
        return original_mutation(*args, **kwargs)

    monkeypatch.setattr(list_owner, list_name, gated_list)
    monkeypatch.setattr(mutation_owner, mutation_name, gated_mutation)
    return gate


def _request_in_thread(call: Callable[[], Any]) -> tuple[threading.Thread, list[Any]]:
    outcome: list[Any] = []

    def invoke() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:  # surfaced on the asserting thread
            outcome.append(exc)

    thread = threading.Thread(target=invoke, name="local-ai-disable-request")
    thread.start()
    return thread, outcome


@pytest.mark.parametrize(
    "entrypoint",
    ["owner_active", "owner_patch", "admin_user", "admin_bot"],
)
def test_disable_closes_agent_created_after_legacy_preimage_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
):
    """The disable transaction must return every identity it actually revoked."""

    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setenv("BZ_RATE_LIMIT", "0")
    app = create_app(db_path=str(tmp_path / f"{entrypoint}.db"))
    owner = _user(app, f"owner_{entrypoint}")
    admin = _user(app, f"admin_{entrypoint}", role="admin")
    target_bot = _bot(app, owner, f"target_{entrypoint}")
    unrelated_owner = (
        _user(app, f"other_{entrypoint}") if entrypoint == "admin_user" else owner
    )
    unrelated_bot = _bot(app, unrelated_owner, f"unrelated_{entrypoint}")

    with TestClient(app) as client:
        owner_headers = _auth(app, owner["username"])
        admin_headers = _auth(app, admin["username"])
        unrelated = client.portal.call(
            lambda: _create_connected_agent(
                app,
                owner_id=int(unrelated_owner["id"]),
                bot_id=int(unrelated_bot["id"]),
                label="unrelated",
            )
        )

        if entrypoint in {"owner_active", "owner_patch", "admin_bot"}:
            list_name = "list_active_local_ai_public_ids_for_bot"
            if entrypoint == "owner_active":
                mutation_owner = app.state.bot_manager
                mutation_name = "set_active"
                call = lambda: client.post(
                    f"/api/bots/{target_bot['id']}/active?active=false",
                    headers=owner_headers,
                )
            elif entrypoint == "owner_patch":
                mutation_owner = app.state.bot_manager
                mutation_name = "patch_owner"
                call = lambda: client.patch(
                    f"/api/bots/{target_bot['id']}",
                    headers=owner_headers,
                    json={"is_active": False},
                )
            else:
                mutation_owner = app.state.bot_manager
                mutation_name = "patch_admin"
                call = lambda: client.patch(
                    f"/api/admin/bots/{target_bot['id']}",
                    headers=admin_headers,
                    json={"is_active": False},
                )
            gate = _install_legacy_snapshot_barrier(
                monkeypatch,
                list_owner=app.state.store,
                list_name=list_name,
                mutation_owner=mutation_owner,
                mutation_name=mutation_name,
            )
        else:
            call = lambda: client.patch(
                f"/api/admin/users/{owner['id']}",
                headers=admin_headers,
                json={"is_active": False},
            )
            gate = _install_legacy_snapshot_barrier(
                monkeypatch,
                list_owner=app.state.store,
                list_name="list_active_local_ai_public_ids_for_owner",
                mutation_owner=app.state.store,
                mutation_name="update_user",
            )

        request_thread, outcome = _request_in_thread(call)
        assert gate.entered.wait(timeout=10), "disable request never reached barrier"
        target = asyncio.run(
            _create_connected_agent(
                app,
                owner_id=int(owner["id"]),
                bot_id=int(target_bot["id"]),
                label="created-after-snapshot",
            )
        )
        gate.release.set()
        request_thread.join(timeout=10)
        assert not request_thread.is_alive(), "disable request did not finish"
        assert len(outcome) == 1
        if isinstance(outcome[0], BaseException):
            raise outcome[0]
        response = outcome[0]
        assert response.status_code == 200, response.text
        assert "_revoked_local_ai_targets" not in response.text
        assert "_local_ai_revocation_scope" not in response.text

        persisted = app.state.store.get_local_ai_agent(int(target["id"]))
        assert persisted is not None and persisted["status"] == "revoked"
        target_status = asyncio.run(
            app.state.local_ai_service.hub.status(str(target["public_id"]))
        )
        assert target_status.online is False
        assert target_status.revoked is True
        unrelated_status = asyncio.run(
            app.state.local_ai_service.hub.status(str(unrelated["public_id"]))
        )
        assert unrelated_status.online is True


def test_repeat_disable_retries_transport_convergence_after_first_revoke_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A committed disable remains retryable when its first hub drain fails."""

    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setenv("BZ_RATE_LIMIT", "0")
    app = create_app(db_path=str(tmp_path / "retry-after-revoke-failure.db"))
    owner = _user(app, "retry_owner")
    target_bot = _bot(app, owner, "retry_target")
    unrelated_bot = _bot(app, owner, "retry_unrelated")

    with TestClient(app) as client:
        headers = _auth(app, owner["username"])
        target = client.portal.call(
            lambda: _create_connected_agent(
                app,
                owner_id=int(owner["id"]),
                bot_id=int(target_bot["id"]),
                label="retry-target",
            )
        )
        unrelated = client.portal.call(
            lambda: _create_connected_agent(
                app,
                owner_id=int(owner["id"]),
                bot_id=int(unrelated_bot["id"]),
                label="retry-unrelated",
            )
        )
        original_revoke = app.state.local_ai_service.hub.revoke
        calls: list[str] = []

        async def fail_first(public_id: str) -> None:
            calls.append(str(public_id))
            if len(calls) == 1:
                raise RuntimeError("injected transport revoke failure")
            await original_revoke(public_id)

        monkeypatch.setattr(
            app.state.local_ai_service.hub, "revoke", fail_first
        )
        with pytest.raises(RuntimeError, match="injected transport revoke failure"):
            client.post(
                f"/api/bots/{target_bot['id']}/active?active=false",
                headers=headers,
            )

        persisted = app.state.store.get_local_ai_agent(int(target["id"]))
        assert persisted is not None and persisted["status"] == "revoked"
        assert client.portal.call(
            app.state.local_ai_service.hub.status, str(target["public_id"])
        ).online

        retried = client.post(
            f"/api/bots/{target_bot['id']}/active?active=false",
            headers=headers,
        )
        assert retried.status_code == 200, retried.text
        assert "_revoked_local_ai_targets" not in retried.text
        assert "_local_ai_revocation_scope" not in retried.text
        assert calls == [str(target["public_id"]), str(target["public_id"])]
        target_status = client.portal.call(
            app.state.local_ai_service.hub.status, str(target["public_id"])
        )
        assert target_status.online is False and target_status.revoked is True
        assert client.portal.call(
            app.state.local_ai_service.hub.status, str(unrelated["public_id"])
        ).online


def test_disable_transport_work_does_not_scale_with_revoked_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """Historical tombstones never enter one live disable convergence batch."""

    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setenv("BZ_RATE_LIMIT", "0")
    app = create_app(db_path=str(tmp_path / "bounded-revoked-history.db"))
    owner = _user(app, "bounded_owner")
    bot = _bot(app, owner, "bounded_target")
    for index in range(96):
        historical = app.state.store.create_local_ai_agent(
            owner_id=int(owner["id"]),
            bot_id=int(bot["id"]),
            label=f"history-{index:03d}",
            public_id=f"lai_history_{index:03d}",
            token_hash=f"history-token-hash-{index:03d}",
            token_hint=f"h{index:05d}",
        )
        assert app.state.store.revoke_local_ai_agent(
            int(historical["id"]), int(owner["id"])
        )

    with TestClient(app) as client:
        headers = _auth(app, owner["username"])
        target = client.portal.call(
            lambda: _create_connected_agent(
                app,
                owner_id=int(owner["id"]),
                bot_id=int(bot["id"]),
                label="current-live-target",
            )
        )
        original_revoke = app.state.local_ai_service.hub.revoke
        calls: list[str] = []

        async def counted_revoke(public_id: str) -> None:
            calls.append(str(public_id))
            await original_revoke(public_id)

        monkeypatch.setattr(
            app.state.local_ai_service.hub, "revoke", counted_revoke
        )
        response = client.post(
            f"/api/bots/{bot['id']}/active?active=false", headers=headers
        )
        assert response.status_code == 200, response.text
        assert calls == [str(target["public_id"])]
        assert app.state.local_ai_service._pending_scope_revocations == {}


@pytest.mark.parametrize("scope", ["bot", "owner"])
def test_disable_rolls_back_authority_and_agent_revocation_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    scope: str,
):
    """A failure inside the write unit cannot commit a partial disable."""

    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setenv("BZ_RATE_LIMIT", "0")
    app = create_app(db_path=str(tmp_path / f"rollback-{scope}.db"))
    owner = _user(app, f"rollback_{scope}")
    bot = _bot(app, owner, f"rollback_bot_{scope}")
    _safe_user, session_token = app.state.auth.authenticate(
        owner["username"], PASSWORD
    )
    agent = asyncio.run(
        _create_connected_agent(
            app,
            owner_id=int(owner["id"]),
            bot_id=int(bot["id"]),
            label=f"rollback-{scope}",
        )
    )
    before_generation = int(agent["_connection_generation"])

    with app.state.store._tx() as connection:
        connection.execute(
            f"""
            CREATE TRIGGER fail_revocation_{scope}
            BEFORE UPDATE OF status ON local_ai_agents
            WHEN OLD.id={int(agent['id'])} AND NEW.status='revoked'
            BEGIN
                SELECT RAISE(ABORT, 'injected Local-AI revocation failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected Local-AI"):
        if scope == "bot":
            app.state.store.update_owned_bot(
                int(owner["id"]), int(bot["id"]), is_active=0
            )
        else:
            app.state.store.update_user(int(owner["id"]), is_active=0)

    assert app.state.store.get_user(int(owner["id"]))["is_active"] == 1
    assert app.state.store.get_bot(int(bot["id"]))["is_active"] == 1
    persisted = app.state.store.get_local_ai_agent(int(agent["id"]))
    assert persisted is not None
    assert persisted["status"] == "active"
    assert int(persisted["connection_generation"]) == before_generation
    assert persisted["connected_at"] is not None
    assert app.state.store.get_session(session_token) is not None
    status = asyncio.run(
        app.state.local_ai_service.hub.status(str(agent["public_id"]))
    )
    assert status.online is True and status.revoked is False


def test_disable_return_closes_pending_decision_and_rejects_late_response(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """After the HTTP success boundary, an old response cannot still commit."""

    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setenv("BZ_RATE_LIMIT", "0")
    app = create_app(db_path=str(tmp_path / "pending-response.db"))
    owner = _user(app, "pending_owner")
    bot = _bot(app, owner, "pending_target")

    with TestClient(app) as client:
        headers = _auth(app, owner["username"])
        agent = client.portal.call(
            lambda: _create_connected_agent(
                app,
                owner_id=int(owner["id"]),
                bot_id=int(bot["id"]),
                label="pending-agent",
            )
        )
        public_id = str(agent["public_id"])
        connection_id = str(agent["_connection_id"])
        decision = client.portal.start_task_soon(
            functools.partial(
                app.state.local_ai_service.hub.request_decision,
                public_id,
                request_id="pending-before-disable",
                match_id="pending-match",
                seat=0,
                turn=1,
                deadline_at=time.monotonic() + 30,
                input={"private_position": "must-close"},
            )
        )
        delivered = client.portal.call(
            lambda: app.state.local_ai_service.hub.next_turn(
                public_id, connection_id, timeout=2
            )
        )
        assert delivered is not None
        assert delivered.request_id == "pending-before-disable"

        response = client.post(
            f"/api/bots/{bot['id']}/active?active=false", headers=headers
        )
        assert response.status_code == 200, response.text
        with pytest.raises(LocalAITechnicalError) as failed:
            decision.result(timeout=2)
        assert failed.value.error_code == "local_ai_revoked"
        with pytest.raises(LocalAIConnectionError, match="revoked_or_unknown"):
            client.portal.call(
                lambda: app.state.local_ai_service.hub.submit_response(
                    public_id,
                    connection_id,
                    request_id="pending-before-disable",
                    match_id="pending-match",
                    turn=1,
                    output={"response": "too-late"},
                )
            )


def test_disable_wins_before_concurrent_agent_create_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """A create ordered after the disable write lock must fail closed."""

    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setenv("BZ_RATE_LIMIT", "0")
    app = create_app(db_path=str(tmp_path / "disable-before-create.db"))
    owner = _user(app, "linear_owner")
    bot = _bot(app, owner, "linear_target")
    competing_store = Store(app.state.store.path)
    disable_inside_tx = threading.Event()
    release_disable = threading.Event()
    create_begin_attempted = threading.Event()
    original_revoke_tx = app.state.store._revoke_local_ai_agents_tx

    def gated_revoke(connection, **kwargs):
        disable_inside_tx.set()
        if not release_disable.wait(timeout=10):
            raise AssertionError("disable transaction was not released")
        return original_revoke_tx(connection, **kwargs)

    monkeypatch.setattr(
        app.state.store, "_revoke_local_ai_agents_tx", gated_revoke
    )
    competing_store._conn.set_trace_callback(
        lambda sql: create_begin_attempted.set()
        if sql.strip().upper().startswith("BEGIN IMMEDIATE")
        else None
    )

    disable_thread, disable_outcome = _request_in_thread(
        lambda: app.state.store.update_owned_bot(
            int(owner["id"]), int(bot["id"]), is_active=0
        )
    )
    assert disable_inside_tx.wait(timeout=10)
    create_thread, create_outcome = _request_in_thread(
        lambda: competing_store.create_local_ai_agent(
            owner_id=int(owner["id"]),
            bot_id=int(bot["id"]),
            label="ordered-after-disable",
            public_id="lai_ordered_after_disable",
            token_hash="ordered-after-disable-token-hash",
            token_hint="linear",
        )
    )
    try:
        assert create_begin_attempted.wait(timeout=10)
        assert create_thread.is_alive(), "create did not wait behind disable write tx"
    finally:
        release_disable.set()
    disable_thread.join(timeout=10)
    create_thread.join(timeout=10)
    competing_store.close()

    assert not disable_thread.is_alive() and not create_thread.is_alive()
    assert len(disable_outcome) == 1 and isinstance(disable_outcome[0], dict)
    assert disable_outcome[0]["is_active"] == 0
    assert len(create_outcome) == 1
    assert isinstance(create_outcome[0], ValueError)
    assert "请先启用这个 Bot" in str(create_outcome[0])
    assert app.state.store.list_local_ai_agents(int(owner["id"])) == []
