"""全来源持久执行队列、崩溃恢复与 Docker supervisor 定向测试。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.matches.execution_queue import ExecutionDispatcher
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.runtime.docker_supervisor import (
    ATTEMPT_LABEL,
    CANONICAL_DOCKER_HOST,
    INSTANCE_LABEL,
    JOB_LABEL,
    LAUNCH_LABEL,
    DockerExecutionIdentity,
    DockerControlUncertain,
    DockerCreateAmbiguous,
    DockerSupervisor,
    DockerSupervisorError,
    docker_cli_environment,
    instance_namespace,
    validate_local_docker_configuration,
)
from bzplat.backend.runtime.binary_runner import (
    BinaryRunner,
    BotSession,
    SandboxControlUncertain,
)
from bzplat.backend.store import Store, rating_projection_digests
from bzplat.backend.store.schema import (
    EXECUTION_SOURCE_AUTO,
    EXECUTION_SOURCE_CONTEST,
    EXECUTION_SOURCE_HUMAN,
    EXECUTION_SOURCE_MANUAL,
    TYPE_CHALLENGE,
    TYPE_CONTEST,
    TYPE_HUMAN,
    TYPE_LADDER,
)


def _verify_projection(store: Store) -> None:
    with store._tx() as conn:
        live = rating_projection_digests(conn)
        assert live["issues"] == []
        conn.execute(
            "UPDATE rating_projection_state SET policy_version='owner-neutral-v3',"
            "rebuilt_at='test',source_settlement_count=?,"
            "source_last_settled_order=?,source_digest=?,projection_digest=?,"
            "plan_digest=?,trusted_mutation_revision=mutation_revision "
            "WHERE singleton=1",
            (
                live["source_settlement_count"],
                live["source_last_settled_order"],
                live["source_digest"],
                live["projection_digest"],
                live["plan_digest"],
            ),
        )


def _bot(store: Store, key: str, *, game_id: str = "holdem") -> dict:
    user = store.create_user(
        f"user-{key}", f"{key}@example.test", "test-password-hash"
    )
    binary = Path(store.path).parent / f"execution-{key}.elf"
    binary.write_bytes(f"fixture-{key}".encode())
    binary_path = str(binary)
    bot = store.create_bot(
        int(user["id"]),
        f"bot-{key}",
        binary_path=binary_path,
        format="elf",
        game_id=game_id,
    )
    version = store.add_bot_version(bot["id"], binary_path=binary_path)
    store.ensure_rating(bot["id"], game_id=game_id)
    return {
        "user_id": int(user["id"]),
        "bot_id": int(bot["id"]),
        "version_id": int(version["id"]),
    }


_API_PASSWORD = "password12"


def _api_user(store: Store, key: str, *, role: str = "user") -> dict:
    username = f"api_{key}"
    user = store.create_user(
        username,
        f"{username}@example.test",
        hash_password(_API_PASSWORD),
        role=role,
    )
    store.update_user(int(user["id"]), email_verified=1)
    return store.get_user(int(user["id"]))


def _owned_bot(
    store: Store,
    owner: dict,
    key: str,
    *,
    game_id: str = "holdem",
) -> dict:
    binary = Path(store.path).parent / f"execution-api-{key}.elf"
    binary.write_bytes(b"test fixture")
    binary_path = str(binary)
    bot = store.create_bot(
        int(owner["id"]),
        f"api_bot_{key}",
        binary_path=binary_path,
        format="elf",
        game_id=game_id,
    )
    version = store.add_bot_version(bot["id"], binary_path=binary_path)
    return {
        "user_id": int(owner["id"]),
        "bot_id": int(bot["id"]),
        "version_id": int(version["id"]),
    }


def _auth_headers(app, user: dict) -> dict[str, str]:
    _, token = app.state.auth.authenticate(user["username"], _API_PASSWORD)
    return {"Authorization": f"Bearer {token}"}


def _enqueue_pair(
    store: Store,
    pair: tuple[dict, dict],
    *,
    source: str = EXECUTION_SOURCE_MANUAL,
    owner_user_id: int | None = None,
) -> dict:
    a, b = pair
    resolved_owner = (
        None
        if source == EXECUTION_SOURCE_AUTO
        else a["user_id"] if owner_user_id is None else owner_user_id
    )
    return store.executions.enqueue(
        source=source,
        owner_user_id=resolved_owner,
        game_id="holdem",
        match_type=TYPE_LADDER if source == EXECUTION_SOURCE_AUTO else TYPE_CHALLENGE,
        bot_a_id=a["bot_id"],
        bot_b_id=b["bot_id"],
        bot_a_version_id=a["version_id"],
        bot_b_version_id=b["version_id"],
    )


def _enqueue_human(store: Store, key: str) -> tuple[dict, dict, dict]:
    bot = _bot(store, f"{key}-bot")
    human = store.create_user(
        f"human-{key}",
        f"human-{key}@example.test",
        "hash",
    )
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_HUMAN,
        owner_user_id=int(human["id"]),
        game_id="holdem",
        match_type=TYPE_HUMAN,
        bot_a_id=bot["bot_id"],
        bot_b_id=bot["bot_id"],
        bot_a_version_id=bot["version_id"],
        bot_b_version_id=None,
        human_user_id=int(human["id"]),
        human_seat=1,
    )
    return human, bot, job


def _claim(store: Store, *, slots: int = 2, units: int = 4) -> dict | None:
    return store.executions.claim_next(
        max_match_slots=slots,
        max_sandbox_units=units,
        aging_seconds=60,
        user_active_limit=1,
        contest_share_slots=1,
    )


def _create_legacy_auto_match_queue(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE auto_match_queue("
        "id INTEGER PRIMARY KEY,decision_id INTEGER,game_id TEXT,"
        "bot_a_id INTEGER,bot_b_id INTEGER,bot_a_version_id INTEGER,"
        "bot_b_version_id INTEGER,status TEXT,match_id TEXT,"
        "created_at TEXT,dispatched_at TEXT)"
    )


def _insert_auto_decision(
    conn: sqlite3.Connection,
    a: dict,
    b: dict,
    *,
    created_at: str,
) -> int:
    return int(
        conn.execute(
            "INSERT INTO auto_match_decisions("
            "policy_version,state_revision,cursor_game_idx,requested_lane,actual_lane,"
            "game_id,bot_a_id,bot_b_id,owner_a_id,owner_b_id,"
            "bot_a_version_id,bot_b_version_id,owner_a_service_before,"
            "owner_b_service_before,bot_a_service_before,bot_b_service_before,"
            "bot_pair_count_before,owner_pair_count_before,rating_gap,"
            "bot_a_seat_debt_before,bot_b_seat_debt_before,selection_reason,created_at) "
            "VALUES('legacy-v3',1,0,'bootstrap','established','holdem',?,?,?,?,?,?,"
            "0,0,0,0,0,0,12.0,0,0,'legacy migration fixture',?)",
            (
                a["bot_id"],
                b["bot_id"],
                a["user_id"],
                b["user_id"],
                a["version_id"],
                b["version_id"],
                created_at,
            ),
        ).lastrowid
    )


@pytest.fixture
def queue_store(tmp_path):
    store = Store(str(tmp_path / "queue.db"))
    store.executions.resume()
    yield store
    store.close()


@pytest.fixture
def execution_api(tmp_path, monkeypatch):
    """API fixture whose database and runtime roots are confined to tmp_path."""
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(
        db_path=str(tmp_path / "execution-api.db"),
        upload_root=tmp_path / "bot_uploads",
    )
    app.state.store.executions.resume()
    client = TestClient(app)
    yield SimpleNamespace(app=app, client=client, store=app.state.store)
    client.close()
    app.state.store.close()


def test_execution_request_api_enforces_owner_202_cancel_and_retry(execution_api):
    app, client, store = (
        execution_api.app,
        execution_api.client,
        execution_api.store,
    )
    owner = _api_user(store, "request_owner")
    intruder = _api_user(store, "request_intruder")
    admin = _api_user(store, "request_admin", role="admin")
    opponent_owner = _api_user(store, "request_opponent")
    own_bot = _owned_bot(store, owner, "request_owner")
    opponent_bot = _owned_bot(store, opponent_owner, "request_opponent")
    _verify_projection(store)
    owner_headers = _auth_headers(app, owner)
    intruder_headers = _auth_headers(app, intruder)
    admin_headers = _auth_headers(app, admin)

    def create_request():
        return client.post(
            "/api/matches/challenge",
            headers=owner_headers,
            json={
                "my_bot_id": own_bot["bot_id"],
                "opponent_bot_id": opponent_bot["bot_id"],
                "game_id": "holdem",
            },
        )

    created = create_request()
    assert created.status_code == 202
    snapshot = created.json()
    request_id = snapshot["public_id"]
    assert request_id.startswith("req_")
    assert set(snapshot) == {
        "public_id",
        "request",
        "ahead_jobs",
        "ahead_sandbox_units",
        "capacity",
        "eta",
    }
    assert set(snapshot["request"]) == {
        "public_id",
        "request_id",
        "source",
        "status",
        "game_id",
        "match_type",
        "match_id",
        "sandbox_units",
        "rated",
        "rating_reason",
        "retryable",
        "cancel_requested",
        "reason",
        "created_at",
        "started_at",
        "terminal_at",
    }
    assert set(snapshot["capacity"]) == {
        "match_slots",
        "sandbox_units",
        "running_matches",
    }
    assert set(snapshot["capacity"]["match_slots"]) == {"used", "capacity"}
    assert set(snapshot["capacity"]["sandbox_units"]) == {"used", "capacity"}
    assert set(snapshot["eta"]) == {
        "min_seconds",
        "max_seconds",
        "dynamic",
        "note",
    }
    assert snapshot["request"] == {
        **snapshot["request"],
        "public_id": request_id,
        "request_id": request_id,
        "source": EXECUTION_SOURCE_MANUAL,
        "status": "queued",
        "game_id": "holdem",
        "match_type": TYPE_CHALLENGE,
        "match_id": None,
        "sandbox_units": 2,
        "rated": True,
        "rating_reason": "eligible",
        "retryable": False,
        "cancel_requested": False,
        "reason": "",
    }
    assert store.list_matches(limit=20) == []
    serialized = json.dumps(snapshot, ensure_ascii=False)
    for forbidden in (
        "owner_user_id",
        "bot_a_version_id",
        "bot_b_version_id",
        "match_config",
        "auto_decision_id",
    ):
        assert forbidden not in serialized

    detail_path = f"/api/execution-requests/{request_id}"
    assert client.get(detail_path).status_code == 401
    assert client.get(detail_path, headers=intruder_headers).status_code == 403
    assert client.get(detail_path, headers=owner_headers).status_code == 200
    assert client.get(detail_path, headers=admin_headers).status_code == 200
    assert client.delete(detail_path, headers=intruder_headers).status_code == 403

    cancelled = client.delete(detail_path, headers=owner_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["request"]["status"] == "cancelled"
    cannot_retry = client.post(f"{detail_path}/retry", headers=owner_headers)
    assert cannot_retry.status_code == 400

    retryable_created = create_request()
    assert retryable_created.status_code == 202
    retryable_id = retryable_created.json()["public_id"]
    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == retryable_id
    match_id = claimed["current_match_id"]
    store.update_match(match_id, status="running")
    store.upsert_replay(match_id, json.dumps([{"type": "match_start"}]))
    assert store.executions.recover_after_namespace_cleanup() == {
        "requeued": 0,
        "interrupted": 1,
        "settling": 0,
    }
    retry_path = f"/api/execution-requests/{retryable_id}/retry"
    assert client.post(retry_path, headers=intruder_headers).status_code == 403
    retried = client.post(retry_path, headers=owner_headers)
    assert retried.status_code == 202
    assert retried.json()["request"]["status"] == "queued"
    assert retried.json()["request"]["retryable"] is False
    assert retried.json()["request"]["match_id"] is None

    before = store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs"
    ).fetchone()[0]
    store.executions.set_control(
        dispatcher_state="starting",
        accepting=False,
    )
    unavailable = create_request()
    assert unavailable.status_code == 503
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs"
    ).fetchone()[0] == before


def test_client_request_id_recovers_lost_202_without_duplicate_jobs(execution_api):
    """A retried POST must observe the already committed durable request."""
    app, client, store = (
        execution_api.app,
        execution_api.client,
        execution_api.store,
    )
    owner = _api_user(store, "idempotent_owner")
    opponent_owner = _api_user(store, "idempotent_opponent")
    own_bot = _owned_bot(store, owner, "idempotent_owner")
    opponent = _owned_bot(store, opponent_owner, "idempotent_opponent")
    other = _owned_bot(store, opponent_owner, "idempotent_other")
    _verify_projection(store)
    headers = _auth_headers(app, owner)
    request_id = "req_" + "A" * 24
    body = {
        "request_id": request_id,
        "my_bot_id": own_bot["bot_id"],
        "opponent_bot_id": opponent["bot_id"],
        "game_id": "holdem",
    }

    first = client.post("/api/matches/challenge", headers=headers, json=body)
    assert first.status_code == 202
    assert first.json()["public_id"] == request_id
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE public_id=?", (request_id,)
    ).fetchone()[0] == 1

    # Simulate a response being lost, followed by process admission closing:
    # the exact retry still resolves the original row before mutable checks.
    store.executions.set_control(dispatcher_state="starting", accepting=False)
    repeated = client.post("/api/matches/challenge", headers=headers, json=body)
    assert repeated.status_code == 202
    assert repeated.json()["public_id"] == request_id
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE public_id=?", (request_id,)
    ).fetchone()[0] == 1

    conflict = client.post(
        "/api/matches/challenge",
        headers=headers,
        json={**body, "opponent_bot_id": other["bot_id"]},
    )
    assert conflict.status_code == 409
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE public_id=?", (request_id,)
    ).fetchone()[0] == 1

    store.executions.resume()
    human_id = "req_" + "B" * 24
    human_body = {
        "request_id": human_id,
        "bot_id": opponent["bot_id"],
        "human_seat": 1,
        "game_id": "holdem",
    }
    human_first = client.post(
        "/api/matches/human", headers=headers, json=human_body
    )
    human_retry = client.post(
        "/api/matches/human", headers=headers, json=human_body
    )
    assert human_first.status_code == human_retry.status_code == 202
    assert human_first.json()["public_id"] == human_retry.json()["public_id"] == human_id
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE public_id=?", (human_id,)
    ).fetchone()[0] == 1


def test_human_request_api_is_neutral_one_unit_and_owner_scoped(execution_api):
    app, client, store = (
        execution_api.app,
        execution_api.client,
        execution_api.store,
    )
    human = _api_user(store, "human_owner")
    intruder = _api_user(store, "human_intruder")
    bot_owner = _api_user(store, "human_bot_owner")
    bot = _owned_bot(store, bot_owner, "human_target")
    human_headers = _auth_headers(app, human)
    intruder_headers = _auth_headers(app, intruder)

    created = client.post(
        "/api/matches/human",
        headers=human_headers,
        json={"bot_id": bot["bot_id"], "human_seat": 1, "game_id": "holdem"},
    )
    assert created.status_code == 202
    snapshot = created.json()
    request_id = snapshot["public_id"]
    request = snapshot["request"]
    assert request["source"] == EXECUTION_SOURCE_HUMAN
    assert request["match_type"] == TYPE_HUMAN
    assert request["status"] == "queued"
    assert request["sandbox_units"] == 1
    assert request["rated"] is False
    assert request["rating_reason"] == "human"
    assert request["match_id"] is None
    assert store.list_matches(limit=20) == []

    detail_path = f"/api/execution-requests/{request_id}"
    assert client.get(detail_path, headers=intruder_headers).status_code == 403
    assert client.get(detail_path, headers=human_headers).status_code == 200
    duplicate = client.post(
        "/api/matches/human",
        headers=human_headers,
        json={"bot_id": bot["bot_id"], "human_seat": 1, "game_id": "holdem"},
    )
    assert duplicate.status_code == 400
    cancelled = client.delete(detail_path, headers=human_headers)
    assert cancelled.status_code == 200
    assert cancelled.json()["request"]["status"] == "cancelled"


def test_claim_version_loss_has_truthful_retry_and_auto_lifecycle(queue_store):
    """A retryable flag must correspond to an actually accepted retry path."""
    store = queue_store
    manual_pair = (_bot(store, "version-manual-a"), _bot(store, "version-manual-b"))
    manual = _enqueue_pair(store, manual_pair)
    with store._tx() as conn:
        conn.execute(
            "DELETE FROM bot_versions WHERE id=?",
            (manual_pair[0]["version_id"],),
        )

    assert _claim(store, slots=1, units=2) is None
    interrupted = store.executions.get(manual["public_id"])
    assert interrupted["status"] == "interrupted"
    assert interrupted["retryable"] == 1
    assert interrupted["terminal_reason"] == "version_unavailable"
    retried = store.executions.retry(
        manual["public_id"], owner_user_id=manual_pair[0]["user_id"]
    )
    assert retried["status"] == "queued"
    assert retried["retryable"] == 0

    # Remove the unrecoverable fixture before exercising the automatic lane.
    store.executions.request_cancel(
        manual["public_id"], owner_user_id=manual_pair[0]["user_id"]
    )
    human_user, human_bot, human_job = _enqueue_human(store, "version-human")
    with store._tx() as conn:
        conn.execute(
            "DELETE FROM bot_versions WHERE id=?", (human_bot["version_id"],)
        )
    assert _claim(store, slots=1, units=2) is None
    human_interrupted = store.executions.get(human_job["public_id"])
    assert human_interrupted["status"] == "interrupted"
    assert human_interrupted["retryable"] == 1
    store.executions.retry(
        human_job["public_id"], owner_user_id=int(human_user["id"])
    )
    store.executions.request_cancel(
        human_job["public_id"], owner_user_id=int(human_user["id"])
    )

    auto_pair = (_bot(store, "version-auto-a"), _bot(store, "version-auto-b"))
    with store._tx() as conn:
        decision_id = _insert_auto_decision(
            conn,
            auto_pair[0],
            auto_pair[1],
            created_at="2026-08-11T12:00:00",
        )
    automatic = store.executions.enqueue(
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
        game_id="holdem",
        match_type=TYPE_LADDER,
        bot_a_id=auto_pair[0]["bot_id"],
        bot_b_id=auto_pair[1]["bot_id"],
        bot_a_version_id=auto_pair[0]["version_id"],
        bot_b_version_id=auto_pair[1]["version_id"],
        auto_decision_id=decision_id,
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE auto_match_decisions SET job_public_id=? WHERE id=?",
            (automatic["public_id"], decision_id),
        )
        conn.execute(
            "DELETE FROM bot_versions WHERE id=?",
            (auto_pair[0]["version_id"],),
        )

    assert _claim(store, slots=1, units=2) is None
    cancelled = store.executions.get(automatic["public_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["retryable"] == 0
    assert cancelled["terminal_reason"] == "version_unavailable"
    decision = store._conn.execute(
        "SELECT lifecycle,terminal_reason,terminal_at "
        "FROM auto_match_decisions WHERE id=?",
        (decision_id,),
    ).fetchone()
    assert tuple(decision[:2]) == ("cancelled", "version_unavailable")
    assert decision["terminal_at"]


def test_claim_rechecks_frozen_artifact_before_creating_match(queue_store):
    store = queue_store
    missing_pair = (_bot(store, "artifact-missing-a"), _bot(store, "artifact-missing-b"))
    missing_job = _enqueue_pair(store, missing_pair)
    missing_version = store.get_bot_version(missing_pair[0]["version_id"])
    Path(missing_version["binary_path"]).unlink()

    assert _claim(store, slots=1, units=2) is None
    interrupted = store.executions.get(missing_job["public_id"])
    assert interrupted["status"] == "interrupted"
    assert interrupted["terminal_reason"] == "version_unavailable"
    assert store.list_matches(limit=20) == []

    store.executions.request_cancel(
        missing_job["public_id"], owner_user_id=missing_pair[0]["user_id"]
    )
    tampered_pair = (_bot(store, "artifact-tamper-a"), _bot(store, "artifact-tamper-b"))
    frozen = store.get_bot_version(tampered_pair[0]["version_id"])
    artifact = Path(frozen["binary_path"])
    original = artifact.read_bytes()
    with store._tx() as conn:
        conn.execute(
            "UPDATE bot_versions SET checksum=?,size_bytes=? WHERE id=?",
            (
                hashlib.sha256(original).hexdigest(),
                len(original),
                tampered_pair[0]["version_id"],
            ),
        )
    tampered_job = _enqueue_pair(store, tampered_pair)
    artifact.write_bytes(b"X" * len(original))

    assert _claim(store, slots=1, units=2) is None
    interrupted = store.executions.get(tampered_job["public_id"])
    assert interrupted["status"] == "interrupted"
    assert interrupted["terminal_reason"] == "version_unavailable"
    assert store.list_matches(limit=20) == []


def test_execution_integrity_cache_avoids_rehashing_stable_versions(
    queue_store, monkeypatch
):
    import bzplat.backend.runtime.binary_integrity as integrity

    store = queue_store
    pair = (_bot(store, "integrity-cache-a"), _bot(store, "integrity-cache-b"))
    real_sha256 = integrity.hashlib.sha256
    integrity_rows = []
    for bot in pair:
        version = store.get_bot_version(bot["version_id"])
        payload = Path(version["binary_path"]).read_bytes()
        integrity_rows.append(
            (real_sha256(payload).hexdigest(), len(payload), bot["version_id"])
        )
    with store._tx() as conn:
        for checksum, size_bytes, version_id in integrity_rows:
            conn.execute(
                "UPDATE bot_versions SET checksum=?,size_bytes=? WHERE id=?",
                (checksum, size_bytes, version_id),
            )
    digest_calls = 0

    def counting_sha256(*args, **kwargs):
        nonlocal digest_calls
        digest_calls += 1
        return real_sha256(*args, **kwargs)

    monkeypatch.setattr(integrity.hashlib, "sha256", counting_sha256)
    for _ in range(2):
        with store._tx() as conn:
            assert store.executions._version_identity_tx(
                conn,
                bot_id=pair[0]["bot_id"],
                version_id=pair[0]["version_id"],
            )
            assert store.executions._version_identity_tx(
                conn,
                bot_id=pair[1]["bot_id"],
                version_id=pair[1]["version_id"],
            )
    assert digest_calls == 2


def test_auto_refill_quarantines_corrupt_current_artifact(queue_store):
    store = queue_store
    pair = (_bot(store, "artifact-auto-a"), _bot(store, "artifact-auto-b"))
    _verify_projection(store)
    version = store.get_bot_version(pair[0]["version_id"])
    Path(version["binary_path"]).unlink()

    result = store.executions.refill_auto(
        target_queued=1,
        bootstrap_target_matches=5,
    )

    assert result["inserted"] == 0
    assert store.get_bot(pair[0]["bot_id"])["is_active"] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE source='auto'"
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM auto_match_decisions"
    ).fetchone()[0] == 0


def test_auto_refill_bootstrap_lane_prioritizes_low_sample_bots(queue_store):
    """冷启动资格由 execution queue 的已计分场次通道实现。"""
    store = queue_store
    bootstrap = (_bot(store, "bootstrap-a"), _bot(store, "bootstrap-b"))
    established = (_bot(store, "established-a"), _bot(store, "established-b"))
    for bot in bootstrap:
        store.update_rating_row(
            bot["bot_id"], game_id="holdem", matches_played=1
        )
    for bot in established:
        store.update_rating_row(
            bot["bot_id"], game_id="holdem", matches_played=20
        )
    _verify_projection(store)
    with store._tx() as conn:
        conn.execute(
            "UPDATE auto_match_fair_state SET next_game_idx=0,next_lane=0 "
            "WHERE singleton=1"
        )

    result = store.executions.refill_auto(
        target_queued=1,
        bootstrap_target_matches=10,
    )

    assert result["inserted"] == 1
    decision = store._conn.execute(
        "SELECT actual_lane,bot_a_id,bot_b_id FROM auto_match_decisions"
    ).fetchone()
    assert decision["actual_lane"] == "bootstrap"
    assert {decision["bot_a_id"], decision["bot_b_id"]} == {
        bootstrap[0]["bot_id"],
        bootstrap[1]["bot_id"],
    }


def test_finalize_never_releases_capacity_for_non_terminal_match(queue_store):
    pair = (_bot(queue_store, "finalize-a"), _bot(queue_store, "finalize-b"))
    queued = _enqueue_pair(queue_store, pair)
    _verify_projection(queue_store)
    claimed = _claim(queue_store, slots=1, units=2)
    assert claimed is not None and claimed["public_id"] == queued["public_id"]
    with queue_store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status='settling',settling_at=?,"
            "cleanup_state='confirmed' WHERE public_id=?",
            ("2026-08-11T12:00:00", queued["public_id"]),
        )
    with pytest.raises(
        RuntimeError, match="settling execution has non-terminal match"
    ):
        queue_store.executions.finalize_ready()
    assert queue_store.executions.get(queued["public_id"])["status"] == "settling"


def test_terminal_runtime_retryability_is_owned_by_request_source(queue_store):
    store = queue_store
    manual_pair = (_bot(store, "runtime-manual-a"), _bot(store, "runtime-manual-b"))
    _verify_projection(store)
    manual = _enqueue_pair(store, manual_pair)
    manual_claim = _claim(store, slots=1, units=2)
    assert manual_claim is not None
    store.update_match(
        manual_claim["current_match_id"],
        status="aborted",
        reason="platform_error",
    )
    store.executions.mark_cleanup_confirmed(manual["public_id"], 1)
    assert store.executions.finalize_ready() == 1
    manual_terminal = store.executions.get(manual["public_id"])
    assert manual_terminal["status"] == "interrupted"
    assert manual_terminal["retryable"] == 1
    store.executions.retry(
        manual["public_id"], owner_user_id=manual_pair[0]["user_id"]
    )
    store.executions.request_cancel(
        manual["public_id"], owner_user_id=manual_pair[0]["user_id"]
    )

    auto_pair = (_bot(store, "runtime-auto-a"), _bot(store, "runtime-auto-b"))
    _verify_projection(store)
    automatic = _enqueue_pair(
        store,
        auto_pair,
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    auto_claim = _claim(store, slots=1, units=2)
    assert auto_claim is not None
    store.update_match(
        auto_claim["current_match_id"],
        status="aborted",
        reason="platform_error",
    )
    store.executions.mark_cleanup_confirmed(automatic["public_id"], 1)
    assert store.executions.finalize_ready() == 1
    auto_terminal = store.executions.get(automatic["public_id"])
    assert auto_terminal["status"] == "interrupted"
    assert auto_terminal["retryable"] == 0
    with pytest.raises(ValueError, match="不可重试"):
        store.executions.retry(automatic["public_id"], owner_user_id=None)


def test_claim_version_loss_backs_off_contest_pairing(queue_store):
    store = queue_store
    pair = (_bot(store, "version-contest-a"), _bot(store, "version-contest-b"))
    contest = store.create_contest(
        "claim version backoff",
        organizer_id=pair[0]["user_id"],
        status="running",
        game_id="holdem",
    )
    pairing = store.add_contest_pairing(
        contest["id"],
        pair[0]["bot_id"],
        pair[1]["bot_id"],
        bot_a_version_id=pair[0]["version_id"],
        bot_b_version_id=pair[1]["version_id"],
    )
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=pair[0]["user_id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=pair[0]["bot_id"],
        bot_b_id=pair[1]["bot_id"],
        bot_a_version_id=pair[0]["version_id"],
        bot_b_version_id=pair[1]["version_id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
    )
    with store._tx() as conn:
        conn.execute(
            "DELETE FROM bot_versions WHERE id=?", (pair[0]["version_id"],)
        )
    before = datetime.now()
    assert _claim(store, slots=1, units=2) is None
    terminal = store.executions.get(job["public_id"])
    assert terminal["status"] == "cancelled"
    assert terminal["retryable"] == 0
    refreshed = store.list_contest_pairings(contest["id"])[0]
    assert refreshed["status"] == "pending" and refreshed["match_id"] is None
    assert datetime.fromisoformat(refreshed["scheduled_at"]) >= before + timedelta(
        seconds=29
    )


def test_contest_and_auto_requests_are_admin_mutations_only(execution_api):
    app, client, store = (
        execution_api.app,
        execution_api.client,
        execution_api.store,
    )
    organizer = _api_user(store, "source_organizer", role="organizer")
    admin = _api_user(store, "source_admin", role="admin")
    bots = [_owned_bot(store, organizer, f"source_{index}") for index in range(4)]
    organizer_headers = _auth_headers(app, organizer)
    admin_headers = _auth_headers(app, admin)
    contest = store.create_contest(
        "Execution API source ownership",
        organizer["id"],
        status="running",
        game_id="holdem",
    )
    pairing = store.add_pairing(
        contest["id"],
        bots[0]["bot_id"],
        bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
    )
    contest_job = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=organizer["id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=bots[0]["bot_id"],
        bot_b_id=bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
    )
    automatic = _enqueue_pair(
        store,
        (bots[2], bots[3]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )

    contest_path = f"/api/execution-requests/{contest_job['public_id']}"
    assert client.get(contest_path, headers=organizer_headers).status_code == 200
    assert client.delete(contest_path, headers=organizer_headers).status_code == 403
    contest_cancel_before = datetime.now()
    contest_cancelled = client.delete(contest_path, headers=admin_headers)
    assert contest_cancelled.status_code == 200
    assert contest_cancelled.json()["request"]["status"] == "cancelled"
    contest_pairing = store.list_contest_pairings(contest["id"])[0]
    assert datetime.fromisoformat(contest_pairing["scheduled_at"]) >= (
        contest_cancel_before + timedelta(seconds=29)
    )

    auto_path = f"/api/execution-requests/{automatic['public_id']}"
    assert client.get(auto_path, headers=organizer_headers).status_code == 403
    assert client.get(auto_path, headers=admin_headers).status_code == 200
    auto_cancelled = client.delete(auto_path, headers=admin_headers)
    assert auto_cancelled.status_code == 200
    assert auto_cancelled.json()["request"]["status"] == "cancelled"


def test_claim_is_atomic_and_cleanup_plus_rating_are_distinct_barriers(queue_store):
    store = queue_store
    pair = (_bot(store, "atomic-a"), _bot(store, "atomic-b"))
    _verify_projection(store)
    first = _enqueue_pair(store, pair)
    assert store.list_matches(limit=10) == []

    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == first["public_id"]
    match_id = claimed["current_match_id"]
    match = store.get_match(match_id)
    assert match and match["status"] == "pending"
    assert json.loads(store.get_replay(match_id)["events_json"]) == []
    policy = store._conn.execute(
        "SELECT rated,rating_reason,source FROM match_rating_policies WHERE match_id=?",
        (match_id,),
    ).fetchone()
    assert tuple(policy) == (1, "eligible", "execution_claim_v3")

    store.update_match(match_id, status="running", started_at="2026-08-10T10:00:00")
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        reason="completed",
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
        ended_at="2026-08-10T10:00:01",
    )
    assert store.executions.finalize_ready() == 0
    capacity = store.executions.snapshot(
        max_match_slots=1, max_sandbox_units=2, aging_seconds=60
    )["capacity"]
    assert capacity["used_match_slots"] == 1
    assert capacity["used_sandbox_units"] == 2

    store.executions.mark_cleanup_confirmed(first["public_id"], 1)
    assert store.executions.finalize_ready() == 1
    assert store.executions.get(first["public_id"])["status"] == "completed"
    with store._tx() as conn:
        assert store._bot_has_active_rated_match_tx(
            conn, pair[0]["bot_id"], game_id="holdem"
        )

    second = _enqueue_pair(store, pair)
    assert _claim(store, slots=1, units=2) is None
    ratings = [store.get_rating(item["bot_id"], game_id="holdem") for item in pair]
    assert store.apply_match_ratings_atomic(
        pair[0]["bot_id"],
        pair[1]["bot_id"],
        game_id="holdem",
        rating_a=tuple(ratings[0][key] for key in ("rating", "rd", "vol")),
        rating_b=tuple(ratings[1][key] for key in ("rating", "rd", "vol")),
        winner=0,
        delta_a=1,
        delta_b=-1,
        reason="test",
        settlement_id=match_id,
    )
    claimed_second = _claim(store, slots=1, units=2)
    assert claimed_second and claimed_second["public_id"] == second["public_id"]


def test_global_match_and_sandbox_limits_include_human_units(queue_store):
    store = queue_store
    bots = [_bot(store, f"capacity-{index}") for index in range(3)]
    human = store.create_user("human-user", "human@example.test", "hash")
    _verify_projection(store)
    bot_job = _enqueue_pair(store, (bots[0], bots[1]))
    human_job = store.executions.enqueue(
        source=EXECUTION_SOURCE_HUMAN,
        owner_user_id=int(human["id"]),
        game_id="holdem",
        match_type=TYPE_HUMAN,
        bot_a_id=bots[2]["bot_id"],
        bot_b_id=bots[2]["bot_id"],
        bot_a_version_id=bots[2]["version_id"],
        bot_b_version_id=None,
        human_user_id=int(human["id"]),
        human_seat=1,
    )
    first = _claim(store, slots=2, units=2)
    assert first and first["public_id"] == bot_job["public_id"]
    assert _claim(store, slots=2, units=2) is None
    second = _claim(store, slots=2, units=3)
    assert second and second["public_id"] == human_job["public_id"]
    store.update_match(first["current_match_id"], status="running")
    store.update_match(second["current_match_id"], status="running")
    extra = _enqueue_pair(store, (bots[0], bots[1]), owner_user_id=bots[2]["user_id"])
    assert extra["status"] == "queued"
    assert _claim(store, slots=2, units=20) is None


def test_dispatcher_claims_manual_human_contest_and_auto_in_one_capacity_model(
    queue_store,
):
    store = queue_store
    bots = [_bot(store, f"all-source-{index}") for index in range(7)]
    human = store.create_user(
        "all-source-human", "all-source-human@example.test", "hash"
    )
    contest = store.create_contest(
        "All source dispatcher",
        bots[3]["user_id"],
        status="running",
        game_id="holdem",
    )
    pairing = store.add_pairing(
        contest["id"],
        bots[3]["bot_id"],
        bots[4]["bot_id"],
        bot_a_version_id=bots[3]["version_id"],
        bot_b_version_id=bots[4]["version_id"],
    )
    _verify_projection(store)
    manual = _enqueue_pair(store, (bots[0], bots[1]))
    human_job = store.executions.enqueue(
        source=EXECUTION_SOURCE_HUMAN,
        owner_user_id=human["id"],
        game_id="holdem",
        match_type=TYPE_HUMAN,
        bot_a_id=bots[2]["bot_id"],
        bot_b_id=bots[2]["bot_id"],
        bot_a_version_id=bots[2]["version_id"],
        bot_b_version_id=None,
        human_user_id=human["id"],
        human_seat=1,
    )
    contest_job = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=bots[3]["user_id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=bots[3]["bot_id"],
        bot_b_id=bots[4]["bot_id"],
        bot_a_version_id=bots[3]["version_id"],
        bot_b_version_id=bots[4]["version_id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
    )
    automatic = _enqueue_pair(
        store,
        (bots[5], bots[6]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )

    class RecordingOrchestrator:
        def __init__(self) -> None:
            self.started: list[dict] = []

        def start_execution_job(self, job: dict) -> None:
            self.started.append(job)

        async def abort_match(self, _match_id: str) -> None:
            raise AssertionError("no cancellation was requested")

    orch = RecordingOrchestrator()
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=4,
        max_sandbox_units=7,
        auto_capability_enabled=False,
    )
    result = asyncio.run(dispatcher.run_once())
    assert result["claimed"] == 4
    assert [job["public_id"] for job in orch.started] == [
        manual["public_id"],
        human_job["public_id"],
        contest_job["public_id"],
        automatic["public_id"],
    ]
    assert [job["source"] for job in orch.started] == [
        EXECUTION_SOURCE_MANUAL,
        EXECUTION_SOURCE_HUMAN,
        EXECUTION_SOURCE_CONTEST,
        EXECUTION_SOURCE_AUTO,
    ]
    capacity = store.executions.snapshot(
        max_match_slots=4,
        max_sandbox_units=7,
        aging_seconds=60,
    )["capacity"]
    assert capacity["used_match_slots"] == 4
    assert capacity["used_sandbox_units"] == 7
    assert store.list_pairings(contest["id"])[0]["match_id"] == orch.started[2][
        "current_match_id"
    ]


def test_priority_aging_auto_switch_and_contest_share(queue_store):
    store = queue_store
    bots = [_bot(store, f"fair-{index}") for index in range(10)]
    organizer = bots[0]["user_id"]
    contest = store.create_contest(
        "Queue share",
        organizer,
        status="running",
        game_id="holdem",
    )
    pairing_one = store.add_pairing(
        contest["id"],
        bots[0]["bot_id"],
        bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
    )
    pairing_two = store.add_pairing(
        contest["id"],
        bots[2]["bot_id"],
        bots[3]["bot_id"],
        bot_a_version_id=bots[2]["version_id"],
        bot_b_version_id=bots[3]["version_id"],
    )
    _verify_projection(store)

    def enqueue_contest(pairing: dict, a: dict, b: dict) -> dict:
        return store.executions.enqueue(
            source=EXECUTION_SOURCE_CONTEST,
            owner_user_id=organizer,
            game_id="holdem",
            match_type=TYPE_CONTEST,
            bot_a_id=a["bot_id"],
            bot_b_id=b["bot_id"],
            bot_a_version_id=a["version_id"],
            bot_b_version_id=b["version_id"],
            contest_id=contest["id"],
            contest_pairing_id=pairing["id"],
        )

    first_contest = enqueue_contest(pairing_one, bots[0], bots[1])
    assert _claim(store, slots=3, units=6)["public_id"] == first_contest["public_id"]
    second_contest = enqueue_contest(pairing_two, bots[2], bots[3])
    manual = _enqueue_pair(store, (bots[4], bots[5]))
    automatic = _enqueue_pair(
        store,
        (bots[6], bots[7]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    assert _claim(store, slots=3, units=6)["public_id"] == manual["public_id"]
    # One contest share is already active and a non-contest request waits, so
    # the automatic request is chosen instead of a second contest request.
    assert _claim(store, slots=3, units=6)["public_id"] == automatic["public_id"]
    assert store.executions.get(second_contest["public_id"])["status"] == "queued"

    # The switch only holds automatic dispatch; it does not cancel or hide it.
    auto_held = _enqueue_pair(
        store,
        (bots[8], bots[9]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    store.executions.set_auto_enabled(False)
    snapshot = store.executions.snapshot(
        max_match_slots=10, max_sandbox_units=20, aging_seconds=60
    )
    assert auto_held["public_id"] in {row["public_id"] for row in snapshot["queued"]}
    store.executions.set_auto_enabled(True)

    old = (datetime.now() - timedelta(minutes=31)).isoformat(timespec="seconds")
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET created_at=? WHERE public_id=?",
            (old, auto_held["public_id"]),
        )
        auto_row = dict(
            conn.execute(
                "SELECT * FROM execution_jobs WHERE public_id=?",
                (auto_held["public_id"],),
            ).fetchone()
        )
    assert store.executions._effective_priority(
        auto_row, now=datetime.now(), aging_seconds=60
    ) > 40


def test_contest_share_never_leaves_capacity_idle(queue_store):
    store = queue_store
    bots = [_bot(store, f"share-{index}") for index in range(8)]
    organizer = bots[0]["user_id"]
    contest = store.create_contest(
        "Runnable share fallback",
        organizer,
        status="running",
        game_id="holdem",
    )
    pairings = [
        store.add_pairing(
            contest["id"],
            bots[offset]["bot_id"],
            bots[offset + 1]["bot_id"],
            bot_a_version_id=bots[offset]["version_id"],
            bot_b_version_id=bots[offset + 1]["version_id"],
        )
        for offset in (0, 2)
    ]
    _verify_projection(store)

    def contest_job(index: int, offset: int) -> dict:
        return store.executions.enqueue(
            source=EXECUTION_SOURCE_CONTEST,
            owner_user_id=organizer,
            game_id="holdem",
            match_type=TYPE_CONTEST,
            bot_a_id=bots[offset]["bot_id"],
            bot_b_id=bots[offset + 1]["bot_id"],
            bot_a_version_id=bots[offset]["version_id"],
            bot_b_version_id=bots[offset + 1]["version_id"],
            contest_id=contest["id"],
            contest_pairing_id=pairings[index]["id"],
        )

    first_contest = contest_job(0, 0)
    assert _claim(store, slots=4, units=8)["public_id"] == first_contest["public_id"]
    owner = bots[4]["user_id"]
    first_manual = _enqueue_pair(
        store, (bots[4], bots[5]), owner_user_id=owner
    )
    assert _claim(store, slots=4, units=8)["public_id"] == first_manual["public_id"]
    blocked_manual = _enqueue_pair(
        store, (bots[6], bots[7]), owner_user_id=owner
    )
    second_contest = contest_job(1, 2)

    # The foreground request is queued but cannot pass its owner's active gate.
    # Contest share therefore relaxes instead of wasting the remaining slot.
    claimed = _claim(store, slots=4, units=8)
    assert claimed and claimed["public_id"] == second_contest["public_id"]
    assert store.executions.get(blocked_manual["public_id"])["status"] == "queued"


def test_contest_jobs_do_not_consume_organizer_personal_queue_limits(queue_store):
    store = queue_store
    organizer = _api_user(store, "personal_limit_organizer", role="organizer")
    opponent = _api_user(store, "personal_limit_opponent")
    contest_pair = (
        _owned_bot(store, organizer, "personal_limit_contest_a"),
        _owned_bot(store, opponent, "personal_limit_contest_b"),
    )
    manual_pair = (
        _owned_bot(store, organizer, "personal_limit_manual_a"),
        _owned_bot(store, opponent, "personal_limit_manual_b"),
    )
    for bot in (*contest_pair, *manual_pair):
        store.ensure_rating(bot["bot_id"], game_id="holdem")
    contest = store.create_contest(
        "Organizer personal queue isolation",
        organizer["id"],
        status="running",
        game_id="holdem",
    )
    pairing = store.add_pairing(
        contest["id"],
        contest_pair[0]["bot_id"],
        contest_pair[1]["bot_id"],
        bot_a_version_id=contest_pair[0]["version_id"],
        bot_b_version_id=contest_pair[1]["version_id"],
    )
    _verify_projection(store)
    contest_job = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=organizer["id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=contest_pair[0]["bot_id"],
        bot_b_id=contest_pair[1]["bot_id"],
        bot_a_version_id=contest_pair[0]["version_id"],
        bot_b_version_id=contest_pair[1]["version_id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
    )
    assert _claim(store, slots=2, units=4)["public_id"] == contest_job["public_id"]

    # The organizer's contest job is system-owned scheduling work.  It must not
    # consume either the personal queued limit or the personal active gate.
    manual_job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=organizer["id"],
        game_id="holdem",
        match_type=TYPE_CHALLENGE,
        bot_a_id=manual_pair[0]["bot_id"],
        bot_b_id=manual_pair[1]["bot_id"],
        bot_a_version_id=manual_pair[0]["version_id"],
        bot_b_version_id=manual_pair[1]["version_id"],
        user_queued_limit=1,
    )
    claimed = _claim(store, slots=2, units=4)
    assert claimed and claimed["public_id"] == manual_job["public_id"]


def test_contest_claim_obeys_start_and_pairing_schedule_gates(queue_store):
    store = queue_store
    bots = [_bot(store, f"time-gate-{index}") for index in range(2)]
    contest = store.create_contest(
        "Time gated queue",
        bots[0]["user_id"],
        status="published",
        game_id="holdem",
    )
    pairing = store.add_pairing(
        contest["id"],
        bots[0]["bot_id"],
        bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
        scheduled_at=(datetime.now() + timedelta(hours=1)).isoformat(
            timespec="seconds"
        ),
    )
    _verify_projection(store)
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=bots[0]["user_id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=bots[0]["bot_id"],
        bot_b_id=bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
    )

    assert _claim(store, slots=1, units=2) is None
    assert store.executions.get(job["public_id"])["status"] == "queued"
    with store._tx() as conn:
        conn.execute(
            "UPDATE contests SET starts_at=? WHERE id=?",
            (
                (datetime.now() - timedelta(minutes=1)).isoformat(
                    timespec="seconds"
                ),
                contest["id"],
            ),
        )
    assert _claim(store, slots=1, units=2) is None
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET scheduled_at=NULL WHERE id=?",
            (pairing["id"],),
        )
    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == job["public_id"]


def test_public_pause_reason_is_sanitized_but_admin_keeps_diagnostic(queue_store):
    store = queue_store
    raw_reason = "Docker inspect internal operator diagnostic"
    store.executions.pause(raw_reason, bounded_retry=False)
    dispatcher = ExecutionDispatcher(
        SimpleNamespace(), store, max_match_slots=2, max_sandbox_units=4
    )
    public = dispatcher.public_snapshot()
    admin = dispatcher.public_snapshot(include_internal=True)
    assert raw_reason not in json.dumps(public, ensure_ascii=False)
    assert admin["dispatcher"]["pause_reason"] == raw_reason


def test_crash_recovery_never_revives_eventful_match(queue_store):
    store = queue_store
    pair = (_bot(store, "recover-a"), _bot(store, "recover-b"))
    _verify_projection(store)
    job = _enqueue_pair(store, pair)
    first = _claim(store, slots=1, units=2)
    first_match = first["current_match_id"]
    recovered = store.executions.recover_after_namespace_cleanup()
    assert recovered == {"requeued": 1, "interrupted": 0, "settling": 0}
    assert store.get_match(first_match) is None
    assert store.executions.get(job["public_id"])["status"] == "queued"

    second = _claim(store, slots=1, units=2)
    second_match = second["current_match_id"]
    assert second_match != first_match
    store.update_match(second_match, status="running")
    store.upsert_replay(second_match, json.dumps([{"type": "match_start"}]))
    recovered = store.executions.recover_after_namespace_cleanup()
    assert recovered == {"requeued": 0, "interrupted": 1, "settling": 0}
    assert store.get_match(second_match)["status"] == "aborted"
    interrupted = store.executions.get(job["public_id"])
    assert interrupted["status"] == "interrupted"
    assert interrupted["retryable"] == 1
    assert store.executions.retry(
        job["public_id"], owner_user_id=pair[0]["user_id"]
    )["status"] == "queued"
    third = _claim(store, slots=1, units=2)
    assert third["current_match_id"] not in {first_match, second_match}


def test_repeated_runtime_recovery_uses_persistent_exponential_backoff(
    queue_store,
):
    store = queue_store
    pair = (_bot(store, "backoff-a"), _bot(store, "backoff-b"))
    _verify_projection(store)
    job = _enqueue_pair(
        store,
        pair,
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )

    for failure_count, expected_delay in ((1, 1), (2, 2), (3, 4)):
        claimed = _claim(store, slots=1, units=2)
        assert claimed and claimed["public_id"] == job["public_id"]
        store.executions.mark_cleanup_pending(
            job["public_id"], f"runtime failure {failure_count}"
        )
        # The real runner confirms physical cleanup before dispatcher recovery;
        # that transition must not erase the runtime-failure classification.
        store.executions.mark_cleanup_confirmed(
            job["public_id"], int(claimed["attempt_count"])
        )
        before = datetime.now()
        recovered = store.executions.recover_after_namespace_cleanup()
        assert recovered == {"requeued": 1, "interrupted": 0, "settling": 0}
        row = store.executions.get(job["public_id"])
        assert row["status"] == "queued"
        assert row["failure_count"] == failure_count
        retry_at = datetime.fromisoformat(row["next_attempt_at"])
        observed_delay = (retry_at - before).total_seconds()
        assert expected_delay - 1 <= observed_delay <= expected_delay + 1

        store.executions.resume()
        assert _claim(store, slots=1, units=2) is None
        if failure_count < 3:
            with store._tx() as conn:
                conn.execute(
                    "UPDATE execution_jobs SET next_attempt_at=? WHERE public_id=?",
                    (
                        (datetime.now() - timedelta(seconds=1)).isoformat(
                            timespec="seconds"
                        ),
                        job["public_id"],
                    ),
                )


def test_confirmed_cleanup_preserves_manual_and_contest_failure_semantics(
    queue_store,
):
    store = queue_store
    manual_pair = (_bot(store, "confirmed-manual-a"), _bot(store, "confirmed-manual-b"))
    _verify_projection(store)
    manual = _enqueue_pair(store, manual_pair)
    claimed_manual = _claim(store, slots=1, units=2)
    store.executions.mark_cleanup_pending(manual["public_id"], "manual runtime failure")
    store.executions.mark_cleanup_confirmed(
        manual["public_id"], int(claimed_manual["attempt_count"])
    )
    assert store.executions.recover_after_namespace_cleanup() == {
        "requeued": 0,
        "interrupted": 1,
        "settling": 0,
    }
    recovered_manual = store.executions.get(manual["public_id"])
    assert recovered_manual["status"] == "interrupted"
    assert recovered_manual["retryable"] == 1
    assert recovered_manual["failure_count"] == 1
    assert recovered_manual["last_error"] == "manual runtime failure"

    store.executions.resume()
    contest_bots = [
        _bot(store, "confirmed-contest-a"),
        _bot(store, "confirmed-contest-b"),
    ]
    contest = store.create_contest(
        "Confirmed cleanup contest",
        contest_bots[0]["user_id"],
        status="running",
        game_id="holdem",
    )
    pairing = store.add_pairing(
        contest["id"],
        contest_bots[0]["bot_id"],
        contest_bots[1]["bot_id"],
        bot_a_version_id=contest_bots[0]["version_id"],
        bot_b_version_id=contest_bots[1]["version_id"],
    )
    contest_job = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=contest_bots[0]["user_id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=contest_bots[0]["bot_id"],
        bot_b_id=contest_bots[1]["bot_id"],
        bot_a_version_id=contest_bots[0]["version_id"],
        bot_b_version_id=contest_bots[1]["version_id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
    )
    claimed_contest = _claim(store, slots=1, units=2)
    store.executions.mark_cleanup_pending(
        contest_job["public_id"], "contest runtime failure"
    )
    store.executions.mark_cleanup_confirmed(
        contest_job["public_id"], int(claimed_contest["attempt_count"])
    )
    before = datetime.now()
    assert store.executions.recover_after_namespace_cleanup() == {
        "requeued": 1,
        "interrupted": 0,
        "settling": 0,
    }
    recovered_contest = store.executions.get(contest_job["public_id"])
    retry_at = datetime.fromisoformat(recovered_contest["next_attempt_at"])
    assert recovered_contest["status"] == "queued"
    assert recovered_contest["failure_count"] == 1
    assert 0 <= (retry_at - before).total_seconds() <= 2
    recovered_pairing = store.list_pairings(contest["id"])[0]
    assert recovered_pairing["status"] == "pending"
    assert recovered_pairing["match_id"] is None
    assert recovered_pairing["scheduled_at"] == recovered_contest["next_attempt_at"]


def test_human_and_contest_eventful_restart_use_source_specific_recovery(queue_store):
    store = queue_store
    bots = [_bot(store, f"source-recovery-{index}") for index in range(3)]
    human = store.create_user(
        "source-recovery-human",
        "source-recovery-human@example.test",
        "hash",
    )
    human_job = store.executions.enqueue(
        source=EXECUTION_SOURCE_HUMAN,
        owner_user_id=human["id"],
        game_id="holdem",
        match_type=TYPE_HUMAN,
        bot_a_id=bots[0]["bot_id"],
        bot_b_id=bots[0]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=None,
        human_user_id=human["id"],
        human_seat=1,
    )
    claimed_human = _claim(store, slots=1, units=1)
    assert claimed_human and claimed_human["public_id"] == human_job["public_id"]
    human_match = claimed_human["current_match_id"]
    store.update_match(human_match, status="running")
    store.upsert_replay(human_match, json.dumps([{"type": "match_start"}]))
    assert store.executions.recover_after_namespace_cleanup() == {
        "requeued": 0,
        "interrupted": 1,
        "settling": 0,
    }
    recovered_human = store.executions.get(human_job["public_id"])
    assert recovered_human["status"] == "interrupted"
    assert recovered_human["retryable"] == 1
    assert store.get_match(human_match)["status"] == "aborted"

    contest = store.create_contest(
        "Source recovery contest",
        bots[1]["user_id"],
        status="running",
        game_id="holdem",
    )
    pairing = store.add_pairing(
        contest["id"],
        bots[1]["bot_id"],
        bots[2]["bot_id"],
        bot_a_version_id=bots[1]["version_id"],
        bot_b_version_id=bots[2]["version_id"],
    )
    contest_job = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=bots[1]["user_id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=bots[1]["bot_id"],
        bot_b_id=bots[2]["bot_id"],
        bot_a_version_id=bots[1]["version_id"],
        bot_b_version_id=bots[2]["version_id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
    )
    claimed_contest = _claim(store, slots=1, units=2)
    assert claimed_contest and claimed_contest["public_id"] == contest_job["public_id"]
    contest_match = claimed_contest["current_match_id"]
    store.update_match(contest_match, status="running")
    store.upsert_replay(contest_match, json.dumps([{"type": "match_start"}]))
    assert store.executions.recover_after_namespace_cleanup() == {
        "requeued": 1,
        "interrupted": 0,
        "settling": 0,
    }
    recovered_contest = store.executions.get(contest_job["public_id"])
    assert recovered_contest["status"] == "queued"
    assert recovered_contest["current_match_id"] is None
    assert store.get_match(contest_match)["status"] == "aborted"
    recovered_pairing = store.list_pairings(contest["id"])[0]
    assert recovered_pairing["status"] == "pending"
    assert recovered_pairing["match_id"] is None


@pytest.mark.parametrize("raises", [False, True], ids=["completed", "exception"])
def test_human_execution_always_uses_common_finish_and_wakes_dispatcher(
    queue_store,
    raises,
):
    store = queue_store
    human, _bot_row, job = _enqueue_human(store, f"finish-{raises}")
    claimed = _claim(store, slots=1, units=1)
    assert claimed and claimed["public_id"] == job["public_id"]
    match_id = claimed["current_match_id"]

    class CleanupProbe:
        supervisor = None

        def __init__(self) -> None:
            self.calls = 0

        async def cleanup_execution(self, scope) -> None:
            self.calls += 1
            scope.mark_cleanup_confirmed()

    cleanup = CleanupProbe()
    orch = MatchOrchestrator(
        store,
        runner=SimpleNamespace(runner=cleanup),
        max_concurrent=1,
    )
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=1,
        max_sandbox_units=1,
        auto_capability_enabled=False,
    )
    finished: list[tuple[str, int | None]] = []

    async def on_match_done(done_match_id: str, contest_id: int | None) -> None:
        finished.append((done_match_id, contest_id))
        dispatcher.wake()

    orch.on_match_done = on_match_done
    finish_calls: list[tuple[str, int | None]] = []
    original_finish = orch._finish_match_task

    async def finish_spy(done_match_id: str, contest_id: int | None) -> None:
        finish_calls.append((done_match_id, contest_id))
        await original_finish(done_match_id, contest_id)

    orch._finish_match_task = finish_spy
    active_event = {"type": "match_start", "game_id": "holdem"}
    orch._active_replay_events[match_id] = [active_event]
    subscriber = orch.subscribe(match_id, human_viewer_seat=1)
    subscriber.get_nowait()
    assert subscriber in orch._sse_human_views
    assert not dispatcher._wake.is_set()

    async def fake_human_match(
        done_match_id: str,
        *,
        execution_scope=None,
    ) -> None:
        assert done_match_id == match_id
        assert execution_scope is not None
        if raises:
            store.update_match(
                match_id,
                status="aborted",
                reason="forced_human_exception",
                ended_at="2026-08-11T10:00:00",
            )
            raise RuntimeError("forced human execution failure")
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            reason="completed",
            result={
                "rounds_played": 1,
                "deltas": [1, -1],
                "normalized_delta": 1,
            },
            ended_at="2026-08-11T10:00:00",
        )

    orch._run_human_match = fake_human_match

    async def exercise() -> None:
        orch.start_execution_job(claimed)
        task = orch._tasks[match_id]
        if raises:
            with pytest.raises(RuntimeError, match="forced human execution failure"):
                await task
        else:
            await task

    asyncio.run(exercise())
    assert cleanup.calls == 1
    assert finish_calls == [(match_id, None)]
    assert finished == [(match_id, None)]
    assert dispatcher._wake.is_set()
    assert match_id not in orch._tasks
    assert match_id not in orch._active_replay_events
    assert match_id not in orch._sse
    assert subscriber not in orch._sse_human_views
    assert int(human["id"]) not in orch._human_active_users


def test_human_cancel_while_waiting_for_semaphore_releases_all_state(queue_store):
    store = queue_store
    human, _bot_row, job = _enqueue_human(store, "semaphore-cancel")
    claimed = _claim(store, slots=1, units=1)
    assert claimed and claimed["public_id"] == job["public_id"]
    match_id = claimed["current_match_id"]

    class CleanupProbe:
        supervisor = None

        def __init__(self) -> None:
            self.calls = 0

        async def cleanup_execution(self, scope) -> None:
            self.calls += 1
            scope.mark_cleanup_confirmed()

    cleanup = CleanupProbe()
    orch = MatchOrchestrator(
        store,
        runner=SimpleNamespace(runner=cleanup),
        max_concurrent=1,
    )
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=1,
        max_sandbox_units=1,
        auto_capability_enabled=False,
    )
    finished: list[tuple[str, int | None]] = []

    async def on_match_done(done_match_id: str, contest_id: int | None) -> None:
        finished.append((done_match_id, contest_id))
        dispatcher.wake()

    orch.on_match_done = on_match_done
    orch._active_replay_events[match_id] = [
        {"type": "match_start", "game_id": "holdem"}
    ]
    subscriber = orch.subscribe(match_id, human_viewer_seat=1)
    subscriber.get_nowait()

    async def exercise() -> None:
        class ObservedSemaphore(asyncio.Semaphore):
            def __init__(self) -> None:
                super().__init__(0)
                self.acquire_entered = asyncio.Event()

            async def acquire(self) -> bool:
                self.acquire_entered.set()
                return await super().acquire()

        gate = ObservedSemaphore()
        orch._sem = gate
        orch.start_execution_job(claimed)
        task = orch._tasks[match_id]
        await asyncio.wait_for(gate.acquire_entered.wait(), timeout=1)
        assert int(human["id"]) in orch._human_active_users
        aborted = await orch.abort_match(match_id)
        assert aborted["status"] == "aborted"
        assert task.cancelled()

    asyncio.run(exercise())
    assert cleanup.calls == 1
    assert finished == [(match_id, None)]
    assert dispatcher._wake.is_set()
    assert match_id not in orch._tasks
    assert match_id not in orch._active_replay_events
    assert match_id not in orch._sse
    assert subscriber not in orch._sse_human_views
    assert int(human["id"]) not in orch._human_active_users
    settling = store.executions.get(job["public_id"])
    assert settling["status"] == "settling"
    assert settling["cleanup_state"] == "confirmed"
    assert store.executions.finalize_ready() == 1
    assert store.executions.get(job["public_id"])["status"] == "cancelled"


def test_pre_body_cancel_confirms_cleanup_and_releases_global_capacity(queue_store):
    store = queue_store
    pair = (_bot(store, "pre-body-a"), _bot(store, "pre-body-b"))
    _verify_projection(store)
    job = _enqueue_pair(store, pair)
    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == job["public_id"]
    match_id = claimed["current_match_id"]

    class CleanupProbe:
        supervisor = None

        def __init__(self) -> None:
            self.calls = 0

        async def cleanup_execution(self, scope) -> None:
            self.calls += 1
            scope.mark_cleanup_confirmed()

    probe = CleanupProbe()
    orch = MatchOrchestrator(
        store,
        runner=SimpleNamespace(runner=probe),
        max_concurrent=1,
    )

    async def exercise() -> None:
        orch.start_execution_job(claimed)
        task = orch._tasks[match_id]
        # No event-loop yield occurs between create_task and abort_match: the
        # attempt is cancelled before _run_match enters its body/semaphore.
        aborted = await orch.abort_match(match_id)
        assert aborted["status"] == "aborted"
        assert task.cancelled()

    asyncio.run(exercise())
    # The coroutine cleanup finally never ran; the identity-guarded done
    # callback is therefore the only valid zero-work cleanup proof.
    assert probe.calls == 0
    settling = store.executions.get(job["public_id"])
    assert settling["status"] == "settling"
    assert settling["cleanup_state"] == "confirmed"
    assert store.executions.finalize_ready() == 1
    assert store.executions.get(job["public_id"])["status"] == "cancelled"
    capacity = store.executions.snapshot(
        max_match_slots=1,
        max_sandbox_units=2,
        aging_seconds=60,
    )["capacity"]
    assert capacity["used_match_slots"] == 0
    assert capacity["used_sandbox_units"] == 0


def test_uncertain_cleanup_is_not_overwritten_by_done_callback(queue_store):
    store = queue_store
    pair = (_bot(store, "uncertain-cleanup-a"), _bot(store, "uncertain-cleanup-b"))
    _verify_projection(store)
    job = _enqueue_pair(store, pair)
    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == job["public_id"]
    match_id = claimed["current_match_id"]
    uncertainty = "forced exact-label cleanup uncertainty"

    class UncertainCleanup:
        supervisor = None

        def __init__(self) -> None:
            self.calls = 0

        async def cleanup_execution(self, scope) -> None:
            self.calls += 1
            scope.mark_recovery_pending(uncertainty)
            raise SandboxControlUncertain(uncertainty)

    probe = UncertainCleanup()
    orch = MatchOrchestrator(
        store,
        runner=SimpleNamespace(runner=probe),
        max_concurrent=1,
    )

    async def exercise() -> None:
        body_entered = asyncio.Event()
        body_blocked = asyncio.Event()

        async def blocked_inner(_match_id, *, execution_scope=None) -> None:
            assert execution_scope is not None
            body_entered.set()
            await body_blocked.wait()

        orch._MatchOrchestrator__run_match_inner = blocked_inner
        orch.start_execution_job(claimed)
        task = orch._tasks[match_id]
        await asyncio.wait_for(body_entered.wait(), timeout=1)
        aborted = await orch.abort_match(match_id)
        assert aborted["status"] == "aborted"
        assert task.cancelled()

    asyncio.run(exercise())
    assert probe.calls == 1
    settling = store.executions.get(job["public_id"])
    assert settling["status"] == "settling"
    assert settling["cleanup_state"] == "pending"
    assert settling["last_error"] == uncertainty
    control = store.executions.control()
    assert control["dispatcher_state"] == "paused"
    assert control["accepting"] == 1
    assert control["pause_reason"] == uncertainty
    assert store.executions.finalize_ready() == 0
    capacity = store.executions.snapshot(
        max_match_slots=1,
        max_sandbox_units=2,
        aging_seconds=60,
    )["capacity"]
    assert capacity["used_match_slots"] == 1
    assert capacity["used_sandbox_units"] == 2


def test_quiesce_waits_for_blocking_attempt_cleanup_before_deregistering(queue_store):
    """Pause recovery cannot miss a task whose match body ended before cleanup."""
    store = queue_store
    owner_a = _api_user(store, "cleanup_barrier_a")
    owner_b = _api_user(store, "cleanup_barrier_b")
    pair = (
        _owned_bot(store, owner_a, "cleanup_barrier_a"),
        _owned_bot(store, owner_b, "cleanup_barrier_b"),
    )
    _verify_projection(store)
    job = _enqueue_pair(store, pair)
    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == job["public_id"]
    match_id = claimed["current_match_id"]

    class BlockingCleanup:
        supervisor = None

        def __init__(self) -> None:
            self.entered: asyncio.Event | None = None
            self.release: asyncio.Event | None = None
            self.completed = False

        async def cleanup_execution(self, scope) -> None:
            assert self.entered is not None and self.release is not None
            self.entered.set()
            await self.release.wait()
            scope.mark_cleanup_confirmed()
            self.completed = True

    cleanup = BlockingCleanup()
    orch = MatchOrchestrator(
        store,
        runner=SimpleNamespace(runner=cleanup),
        max_concurrent=1,
    )
    finish_saw_cleanup: list[bool] = []

    async def on_match_done(_match_id: str, _contest_id: int | None) -> None:
        finish_saw_cleanup.append(cleanup.completed)

    orch.on_match_done = on_match_done

    async def exercise() -> None:
        cleanup.entered = asyncio.Event()
        cleanup.release = asyncio.Event()

        async def body_returns(_match_id: str, *, execution_scope=None) -> None:
            assert execution_scope is not None

        orch._MatchOrchestrator__run_match_inner = body_returns
        orch.start_execution_job(claimed)
        owned_task = orch._tasks[match_id]
        await asyncio.wait_for(cleanup.entered.wait(), timeout=1)

        # The match body is over, but exact cleanup is deliberately blocked.  A
        # recovery quiesce must still see and drain the registered attempt.
        quiesce = asyncio.create_task(orch.quiesce_execution_tasks())
        await asyncio.sleep(0)
        assert not quiesce.done()
        assert orch._tasks.get(match_id) is owned_task
        assert not cleanup.completed

        cleanup.release.set()
        await asyncio.wait_for(quiesce, timeout=1)
        assert owned_task.cancelled()

    asyncio.run(exercise())
    assert cleanup.completed
    assert finish_saw_cleanup == [True]
    assert orch._tasks == {}


def test_cancel_while_waiting_for_semaphore_still_confirms_cleanup(queue_store):
    store = queue_store
    pair = (_bot(store, "semaphore-cancel-a"), _bot(store, "semaphore-cancel-b"))
    _verify_projection(store)
    job = _enqueue_pair(store, pair)
    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == job["public_id"]
    match_id = claimed["current_match_id"]

    class CleanupProbe:
        supervisor = None

        def __init__(self) -> None:
            self.calls = 0

        async def cleanup_execution(self, scope) -> None:
            self.calls += 1
            scope.mark_cleanup_confirmed()

    probe = CleanupProbe()
    orch = MatchOrchestrator(
        store,
        runner=SimpleNamespace(runner=probe),
        max_concurrent=1,
    )

    async def exercise() -> None:
        class ObservedSemaphore(asyncio.Semaphore):
            def __init__(self) -> None:
                super().__init__(0)
                self.acquire_entered = asyncio.Event()

            async def acquire(self) -> bool:
                self.acquire_entered.set()
                return await super().acquire()

        gate = ObservedSemaphore()
        orch._sem = gate
        orch.start_execution_job(claimed)
        task = orch._tasks[match_id]
        await asyncio.wait_for(gate.acquire_entered.wait(), timeout=1)
        assert not task.done()
        aborted = await orch.abort_match(match_id)
        assert aborted["status"] == "aborted"
        assert task.cancelled()

    asyncio.run(exercise())
    assert probe.calls == 1
    settling = store.executions.get(job["public_id"])
    assert settling["status"] == "settling"
    assert settling["cleanup_state"] == "confirmed"
    assert settling["last_error"] == ""
    assert store.executions.finalize_ready() == 1
    assert store.executions.get(job["public_id"])["status"] == "cancelled"
    capacity = store.executions.snapshot(
        max_match_slots=1,
        max_sandbox_units=2,
        aging_seconds=60,
    )["capacity"]
    assert capacity["used_match_slots"] == 0
    assert capacity["used_sandbox_units"] == 0


def test_auto_eventful_crash_requeues_and_public_projection_is_whitelisted(queue_store):
    store = queue_store
    first_pair = (_bot(store, "public-a"), _bot(store, "public-b"))
    second_pair = (_bot(store, "public-c"), _bot(store, "public-d"))
    _verify_projection(store)
    ahead = _enqueue_pair(store, first_pair)
    target = _enqueue_pair(store, second_pair)
    dispatcher = ExecutionDispatcher(
        SimpleNamespace(), store, max_match_slots=2, max_sandbox_units=4
    )
    payload = dispatcher.public_request(target["public_id"])
    assert payload["ahead_jobs"] == 1
    assert payload["ahead_sandbox_units"] == 2
    assert payload["capacity"]["match_slots"] == {"used": 0, "capacity": 2}
    assert payload["eta"]["dynamic"] is True
    serialized = json.dumps(payload, ensure_ascii=False)
    for forbidden in (
        "version_id",
        "binary_path",
        "checksum",
        "match_config",
        "auto_decision_id",
        '"id":',
    ):
        assert forbidden not in serialized

    store.executions.request_cancel(
        ahead["public_id"], owner_user_id=first_pair[0]["user_id"]
    )
    store.executions.request_cancel(
        target["public_id"], owner_user_id=second_pair[0]["user_id"]
    )
    automatic = _enqueue_pair(
        store,
        first_pair,
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    claimed = _claim(store, slots=1, units=2)
    assert claimed["public_id"] == automatic["public_id"]
    match_id = claimed["current_match_id"]
    store.update_match(match_id, status="running")
    store.upsert_replay(match_id, json.dumps([{"type": "match_start"}]))
    recovered = store.executions.recover_after_namespace_cleanup()
    assert recovered["requeued"] == 1
    assert store.get_match(match_id)["status"] == "aborted"
    auto_job = store.executions.get(automatic["public_id"])
    assert auto_job["status"] == "queued"
    assert auto_job["current_match_id"] is None


def test_docker_commands_are_local_deterministic_and_hardened(monkeypatch, tmp_path):
    monkeypatch.delenv("BZ_DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_HOST", raising=False)
    monkeypatch.delenv("DOCKER_CONTEXT", raising=False)
    validate_local_docker_configuration()
    monkeypatch.setenv("DOCKER_HOST", "tcp://docker.example:2376")
    monkeypatch.setenv("DOCKER_CONTEXT", "remote-development")
    monkeypatch.setenv("DOCKER_TLS_VERIFY", "1")
    validate_local_docker_configuration()
    child_env = docker_cli_environment()
    assert child_env["DOCKER_HOST"] == CANONICAL_DOCKER_HOST
    assert "DOCKER_CONTEXT" not in child_env
    assert "DOCKER_TLS_VERIFY" not in child_env
    monkeypatch.setenv("BZ_DOCKER_HOST", "tcp://docker.example:2376")
    with pytest.raises(DockerSupervisorError, match="BZ_DOCKER_HOST"):
        validate_local_docker_configuration()
    monkeypatch.delenv("BZ_DOCKER_HOST")

    first = instance_namespace(tmp_path / "one.db")
    second = instance_namespace(tmp_path / "two.db")
    assert first != second
    assert instance_namespace(tmp_path / "one.db", "production-a") == "production-a"
    identity = DockerExecutionIdentity(first, "req_public-safe", 3)
    assert identity.container_name(0) == identity.container_name(0)
    assert dict(identity.labels(0))[INSTANCE_LABEL] == first
    assert dict(identity.labels(0))[JOB_LABEL] == "req_public-safe"
    assert dict(identity.labels(0))[ATTEMPT_LABEL] == "3"

    options = DockerSupervisor.sandbox_options(
        identity=identity,
        slot=0,
        name=identity.container_name(0),
        binary_path=Path("/tmp/bot.elf"),
        image="debian:bookworm-slim",
        memory="512m",
        cpus="1",
    )
    for required in (
        "--network=none",
        "--read-only",
        "--tmpfs",
        "--cap-drop=ALL",
        "no-new-privileges",
        "65534:65534",
        "--cpus=1",
        "--memory=512m",
        "--memory-swap=512m",
        "--pids-limit=64",
        "--log-driver=none",
        "--pull=never",
        "linux/amd64",
        "debian:bookworm-slim",
    ):
        assert required in options
    supervisor = object.__new__(DockerSupervisor)
    supervisor.docker_bin = "docker"
    supervisor.instance = first
    assert supervisor.command("ps") == [
        "docker", "--host", CANONICAL_DOCKER_HOST, "ps"
    ]


def test_docker_start_requires_daemon_started_at(monkeypatch):
    supervisor = object.__new__(DockerSupervisor)
    supervisor.docker_bin = "docker"
    supervisor.instance = "test-instance"
    proc = SimpleNamespace(returncode=None)

    async def fake_subprocess(*args, **kwargs):
        assert args[:4] == (
            "docker", "--host", CANONICAL_DOCKER_HOST, "start"
        )
        assert kwargs["env"]["DOCKER_HOST"] == CANONICAL_DOCKER_HOST
        return proc

    async def exercise() -> None:
        polls = iter((False, True))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
        monkeypatch.setattr(supervisor, "_started_at", lambda _name: next(polls))
        attached = await supervisor.start_attached(
            "exact-name", stream_limit=1024, confirm_timeout=0.5
        )
        assert attached is proc

        class FailedControlProcess:
            def __init__(self) -> None:
                self.returncode = None
                self.terminated = False
                self.waited = 0

            def terminate(self) -> None:
                self.terminated = True
                self.returncode = -15

            async def wait(self) -> int:
                self.waited += 1
                return int(self.returncode or 0)

        failed = FailedControlProcess()

        async def failed_subprocess(*_args, **_kwargs):
            return failed

        monkeypatch.setattr(asyncio, "create_subprocess_exec", failed_subprocess)
        monkeypatch.setattr(supervisor, "_started_at", lambda _name: False)
        with pytest.raises(DockerControlUncertain, match="StartedAt"):
            await supervisor.start_attached(
                "never-started", stream_limit=1024, confirm_timeout=0.05
            )
        assert failed.terminated is True
        assert failed.waited >= 1

        cancelled = FailedControlProcess()

        async def cancelled_subprocess(*_args, **_kwargs):
            return cancelled

        monkeypatch.setattr(
            asyncio, "create_subprocess_exec", cancelled_subprocess
        )
        task = asyncio.create_task(
            supervisor.start_attached(
                "cancelled-start", stream_limit=1024, confirm_timeout=1
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.terminated is True
        assert cancelled.waited >= 1

    asyncio.run(exercise())


def test_cancelled_docker_create_is_drained_before_cleanup(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class FakeSupervisor:
        instance = "test-instance"

        @asynccontextmanager
        async def launch_guard(self):
            yield

        def create(self, **_kwargs):
            entered.set()
            assert release.wait(timeout=2)
            return "late-created-container"

        async def start_attached(self, *_args, **_kwargs):
            raise AssertionError("cancelled create must not continue to start")

    binary = tmp_path / "bot.elf"
    binary.write_bytes(b"test")
    runner = object.__new__(BinaryRunner)
    runner.supervisor = FakeSupervisor()
    runner._linux_image = "test-image"
    session = BotSession(
        session_id="cancel-create",
        info=SimpleNamespace(),
        binary_path=binary,
    )

    async def exercise() -> None:
        task = asyncio.create_task(runner._start_docker(session))
        assert await asyncio.to_thread(entered.wait, 1)
        task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert session.container_name == "late-created-container"

    asyncio.run(exercise())


def _journal_supervisor(store: Store, tmp_path: Path) -> DockerSupervisor:
    supervisor = object.__new__(DockerSupervisor)
    supervisor.docker_bin = "docker"
    supervisor.instance = "journal-test-instance"
    supervisor.launch_journal = store.executions
    supervisor._launch_lock_path = tmp_path / "docker-launch.lock"
    return supervisor


def _begin_test_launch(
    store: Store,
    *,
    token: str,
    boot_id: str,
    job_public_id: str = "preflight-proof",
) -> DockerExecutionIdentity:
    identity = DockerExecutionIdentity(
        "journal-test-instance", job_public_id, 1
    )
    store.executions.begin_docker_launch(
        launch_token=token,
        instance_key=identity.instance,
        owner_kind="preflight",
        job_public_id=identity.job_public_id,
        attempt_no=identity.attempt_no,
        slot=0,
        container_name=identity.container_name(0),
        host_boot_id=boot_id,
    )
    return identity


def test_docker_create_persists_intent_before_rpc_and_marks_created(
    queue_store, tmp_path, monkeypatch
):
    import bzplat.backend.runtime.docker_supervisor as supervisor_mod

    supervisor = _journal_supervisor(queue_store, tmp_path)
    identity = DockerExecutionIdentity(
        "journal-test-instance", "create-success", 1
    )
    token = "create-success-token"
    monkeypatch.setattr(supervisor_mod, "host_boot_id", lambda: "boot-create")
    rpc_states: list[str] = []

    def fake_run(args, **_kwargs):
        assert args[0] == "create"
        launch = queue_store.executions.docker_launch()
        rpc_states.append(str(launch["state"]))
        assert launch["launch_token"] == token
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(supervisor, "_run", fake_run)
    monkeypatch.setattr(
        supervisor,
        "inspect_labels",
        lambda _name: dict(identity.labels(0, launch_token=token)),
    )

    name = supervisor.create(
        identity=identity,
        slot=0,
        launch_token=token,
        owner_kind="preflight",
        binary_path=tmp_path / "bot.elf",
        image="test-image",
        memory="512m",
        cpus="1",
    )

    assert name == identity.container_name(0)
    assert rpc_states == ["creating"]
    launch = queue_store.executions.docker_launch()
    assert launch["state"] == "created"
    assert launch["launch_token"] == token


@pytest.mark.parametrize("outcome", ["nonzero", "exception"])
def test_docker_create_failure_keeps_ambiguous_intent(
    queue_store, tmp_path, monkeypatch, outcome
):
    import bzplat.backend.runtime.docker_supervisor as supervisor_mod

    supervisor = _journal_supervisor(queue_store, tmp_path)
    identity = DockerExecutionIdentity(
        "journal-test-instance", f"create-{outcome}", 1
    )
    token = f"create-{outcome}-token"
    monkeypatch.setattr(supervisor_mod, "host_boot_id", lambda: "boot-create")

    def fake_run(args, **_kwargs):
        assert args[0] == "create"
        launch = queue_store.executions.docker_launch()
        assert launch["state"] == "creating"
        assert launch["launch_token"] == token
        if outcome == "exception":
            raise DockerControlUncertain("mock Docker RPC uncertainty")
        return SimpleNamespace(returncode=19)

    monkeypatch.setattr(supervisor, "_run", fake_run)

    with pytest.raises(DockerCreateAmbiguous):
        supervisor.create(
            identity=identity,
            slot=0,
            launch_token=token,
            owner_kind="preflight",
            binary_path=tmp_path / "bot.elf",
            image="test-image",
            memory="512m",
            cpus="1",
        )

    launch = queue_store.executions.docker_launch()
    assert launch["state"] == "creating"
    assert launch["launch_token"] == token


class _JournalControlProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.terminated = False
        self.waited = 0

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    async def wait(self) -> int:
        self.waited += 1
        return int(self.returncode or 0)


def test_docker_start_ack_clears_created_journal(
    queue_store, tmp_path, monkeypatch
):
    supervisor = _journal_supervisor(queue_store, tmp_path)
    token = "start-success-token"
    _begin_test_launch(queue_store, token=token, boot_id="boot-start")
    queue_store.executions.mark_docker_launch_created(token)
    proc = _JournalControlProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(supervisor, "_started_at", lambda _name: True)

    attached = asyncio.run(
        supervisor.start_attached(
            "start-success",
            stream_limit=1024,
            confirm_timeout=0.05,
            launch_token=token,
        )
    )

    assert attached is proc
    assert proc.terminated is False
    assert queue_store.executions.docker_launch()["state"] == "idle"


def test_docker_start_failure_keeps_created_journal_and_drains_process(
    queue_store, tmp_path, monkeypatch
):
    supervisor = _journal_supervisor(queue_store, tmp_path)
    token = "start-failure-token"
    _begin_test_launch(queue_store, token=token, boot_id="boot-start")
    queue_store.executions.mark_docker_launch_created(token)
    proc = _JournalControlProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(supervisor, "_started_at", lambda _name: False)

    with pytest.raises(DockerControlUncertain, match="StartedAt"):
        asyncio.run(
            supervisor.start_attached(
                "start-failure",
                stream_limit=1024,
                confirm_timeout=0.05,
                launch_token=token,
            )
        )

    assert proc.terminated is True
    assert proc.waited >= 1
    launch = queue_store.executions.docker_launch()
    assert launch["state"] == "created"
    assert launch["launch_token"] == token


def test_docker_start_cancellation_keeps_created_journal_and_drains_process(
    queue_store, tmp_path, monkeypatch
):
    supervisor = _journal_supervisor(queue_store, tmp_path)
    token = "start-cancel-token"
    _begin_test_launch(queue_store, token=token, boot_id="boot-start")
    queue_store.executions.mark_docker_launch_created(token)
    proc = _JournalControlProcess()

    async def fake_subprocess(*_args, **_kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_subprocess)
    monkeypatch.setattr(supervisor, "_started_at", lambda _name: False)

    async def exercise() -> None:
        task = asyncio.create_task(
            supervisor.start_attached(
                "start-cancel",
                stream_limit=1024,
                confirm_timeout=1,
                launch_token=token,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(exercise())

    assert proc.terminated is True
    assert proc.waited >= 1
    launch = queue_store.executions.docker_launch()
    assert launch["state"] == "created"
    assert launch["launch_token"] == token


def test_unscoped_preflight_gate_serializes_and_cancellation_does_not_leak(
    tmp_path
):
    gate = threading.BoundedSemaphore(1)
    first_runner = object.__new__(BinaryRunner)
    first_runner._preflight_gate = gate
    second_runner = object.__new__(BinaryRunner)
    second_runner._preflight_gate = gate

    def session(name: str, *, scoped: bool = False) -> BotSession:
        return BotSession(
            session_id=name,
            info=SimpleNamespace(),
            binary_path=tmp_path / f"{name}.elf",
            mode="docker",
            execution_scope=SimpleNamespace() if scoped else None,
        )

    async def exercise() -> None:
        first = session("first")
        second = session("second")
        await first_runner._acquire_preflight_permit(first)
        waiter = asyncio.create_task(
            second_runner._acquire_preflight_permit(second)
        )
        await asyncio.sleep(0.02)
        assert waiter.done() is False
        first_runner._release_preflight_permit(first)
        await waiter
        assert second._preflight_permit_held is True
        second_runner._release_preflight_permit(second)

        holder = session("holder")
        cancelled = session("cancelled")
        await first_runner._acquire_preflight_permit(holder)
        cancelled_waiter = asyncio.create_task(
            second_runner._acquire_preflight_permit(cancelled)
        )
        await asyncio.sleep(0.02)
        assert cancelled_waiter.done() is False
        cancelled_waiter.cancel()
        await asyncio.sleep(0.01)
        # The worker-thread acquire is non-abandonable.  Cancellation drains it,
        # then returns the token before propagating CancelledError.
        assert cancelled_waiter.done() is False
        first_runner._release_preflight_permit(holder)
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter
        assert cancelled._preflight_permit_held is False

        scoped = session("scoped", scoped=True)
        await first_runner._acquire_preflight_permit(scoped)
        assert scoped._preflight_permit_held is False

    asyncio.run(exercise())

    assert gate.acquire(blocking=False) is True
    gate.release()


def test_same_boot_double_zero_cannot_release_ambiguous_create(
    queue_store, tmp_path, monkeypatch
):
    import bzplat.backend.runtime.docker_supervisor as supervisor_mod

    supervisor = _journal_supervisor(queue_store, tmp_path)
    _begin_test_launch(queue_store, token="same-boot-token", boot_id="boot-a")
    zero_queries: list[dict] = []
    monkeypatch.setattr(supervisor_mod, "host_boot_id", lambda: "boot-a")
    monkeypatch.setattr(supervisor, "inspect_labels", lambda _name: None)

    def no_ids(**filters):
        zero_queries.append(filters)
        return []

    monkeypatch.setattr(supervisor, "list_ids", no_ids)
    monkeypatch.setattr(supervisor, "list_name_ids", lambda _name: [])
    monkeypatch.setattr(
        supervisor,
        "remove_names",
        lambda _names: pytest.fail("zero proof must not call rm"),
    )

    with pytest.raises(DockerCreateAmbiguous, match="双零不能排除迟到容器"):
        asyncio.run(supervisor.cleanup_instance())

    assert len(zero_queries) >= 4  # instance + exact launch across both polls
    launch = queue_store.executions.docker_launch()
    assert launch["state"] == "creating"
    assert launch["launch_token"] == "same-boot-token"

    # A daemon-side create that appears after the two zero samples is still
    # owned by the persisted token.  The next cleanup observes its exact
    # identity, removes it, and only then may clear the journal.
    present = True
    expected_labels = {
        INSTANCE_LABEL: "journal-test-instance",
        JOB_LABEL: "preflight-proof",
        ATTEMPT_LABEL: "1",
        "io.botbattle.slot": "0",
        LAUNCH_LABEL: "same-boot-token",
    }
    monkeypatch.setattr(
        supervisor,
        "inspect_labels",
        lambda _name: dict(expected_labels) if present else None,
    )
    monkeypatch.setattr(
        supervisor,
        "list_ids",
        lambda **_filters: ["late-container"] if present else [],
    )
    monkeypatch.setattr(
        supervisor,
        "list_name_ids",
        lambda _name: ["late-container"] if present else [],
    )

    def remove_late(names):
        nonlocal present
        assert "late-container" in names
        present = False

    monkeypatch.setattr(supervisor, "remove_names", remove_late)
    asyncio.run(supervisor.cleanup_instance())
    assert queue_store.executions.docker_launch()["state"] == "idle"


def test_host_boot_change_plus_exact_zero_converges_ambiguous_create(
    queue_store, tmp_path, monkeypatch
):
    import bzplat.backend.runtime.docker_supervisor as supervisor_mod

    supervisor = _journal_supervisor(queue_store, tmp_path)
    _begin_test_launch(queue_store, token="old-boot-token", boot_id="boot-old")
    monkeypatch.setattr(supervisor_mod, "host_boot_id", lambda: "boot-new")
    monkeypatch.setattr(supervisor, "inspect_labels", lambda _name: None)
    monkeypatch.setattr(supervisor, "list_ids", lambda **_filters: [])
    monkeypatch.setattr(supervisor, "list_name_ids", lambda _name: [])

    asyncio.run(supervisor.cleanup_instance())

    assert queue_store.executions.docker_launch()["state"] == "idle"


def test_preflight_launch_and_instance_cleanup_share_the_same_flock(
    queue_store, tmp_path, monkeypatch
):
    supervisor = _journal_supervisor(queue_store, tmp_path)
    cleanup_entered = asyncio.Event()

    async def fake_cleanup_locked(*_args, **_kwargs):
        cleanup_entered.set()

    monkeypatch.setattr(supervisor, "_cleanup_locked", fake_cleanup_locked)

    async def exercise() -> None:
        # This outer owner models preflight's create/start critical section.
        async with supervisor.launch_guard():
            cleanup = asyncio.create_task(supervisor.cleanup_instance())
            await asyncio.sleep(0.02)
            assert cleanup_entered.is_set() is False
        await asyncio.wait_for(cleanup, timeout=1)
        assert cleanup_entered.is_set() is True

    asyncio.run(exercise())


def test_app_main_and_preflight_runners_share_one_supervisor(
    tmp_path, monkeypatch
):
    import bzplat.backend.runtime.docker_supervisor as supervisor_mod

    monkeypatch.delenv("BZ_BOT_LOCAL", raising=False)
    monkeypatch.delenv("BZ_DOCKER_HOST", raising=False)
    monkeypatch.setattr(supervisor_mod.shutil, "which", lambda _name: "/fake/docker")
    app = create_app(
        db_path=str(tmp_path / "shared-supervisor.db"),
        upload_root=tmp_path / "uploads",
    )
    preflight = app.state.preflight_runner_factory()
    second_preflight = app.state.preflight_runner_factory()
    assert preflight is not app.state.binary_runner
    assert second_preflight is not preflight
    assert preflight.supervisor is app.state.binary_runner.supervisor
    assert second_preflight.supervisor is preflight.supervisor
    assert preflight.supervisor.launch_journal is app.state.store.executions
    assert preflight._preflight_gate is app.state.preflight_gate
    assert second_preflight._preflight_gate is app.state.preflight_gate
    assert app.state.binary_runner._preflight_gate is None
    wakes: list[bool] = []
    monkeypatch.setattr(
        app.state.execution_dispatcher, "wake", lambda: wakes.append(True)
    )
    app.state.binary_runner._docker_uncertain_callback("mock uncertainty")
    assert wakes == [True]
    assert app.state.store.executions.control()["dispatcher_state"] == "paused"
    app.state.store.close()


def test_unsettled_launch_journal_blocks_recovery_resume_and_claim(
    queue_store, tmp_path, monkeypatch
):
    import bzplat.backend.runtime.docker_supervisor as supervisor_mod

    supervisor = _journal_supervisor(queue_store, tmp_path)
    _begin_test_launch(queue_store, token="recovery-gate", boot_id="boot-a")
    monkeypatch.setattr(supervisor_mod, "host_boot_id", lambda: "boot-a")

    class NoopRuntime:
        def __init__(self) -> None:
            self.supervisor = supervisor

        async def cleanup_instance(self) -> None:
            return None

        async def ensure_runtime_ready(self) -> None:
            return None

    orch = SimpleNamespace(runner=SimpleNamespace(runner=NoopRuntime()))
    dispatcher = ExecutionDispatcher(
        orch,
        queue_store,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=False,
    )
    forbidden_calls: list[str] = []

    def forbidden(name: str):
        def fail(*_args, **_kwargs):
            forbidden_calls.append(name)
            raise AssertionError(f"{name} must remain gated")

        return fail

    monkeypatch.setattr(
        queue_store.executions,
        "recover_after_namespace_cleanup",
        forbidden("recover"),
    )
    monkeypatch.setattr(queue_store.executions, "resume", forbidden("resume"))
    monkeypatch.setattr(
        queue_store.executions, "claim_next", forbidden("claim")
    )

    async def exercise() -> tuple[dict, dict]:
        started = await dispatcher.start()
        once = await dispatcher.run_once()
        await dispatcher.close()
        return started, once

    started, once = asyncio.run(exercise())
    assert started["outcome"] == "paused"
    assert once["outcome"] == "paused"
    assert forbidden_calls == []
    control = queue_store.executions.control()
    assert control["dispatcher_state"] == "paused"
    assert str(control["pause_reason"]).startswith("manual:")


def test_concurrent_admin_resume_has_one_atomic_recovery_gate(queue_store):
    store = queue_store
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    reconcile_entered = asyncio.Event()
    release_reconcile = asyncio.Event()

    class Runtime:
        supervisor = None

        def __init__(self) -> None:
            self.cleanup_calls = 0

        async def cleanup_instance(self) -> None:
            self.cleanup_calls += 1
            cleanup_entered.set()
            await release_cleanup.wait()

        async def ensure_runtime_ready(self) -> None:
            return None

    runtime = Runtime()

    class Orch:
        runner = SimpleNamespace(runner=runtime)

        def __init__(self) -> None:
            self.quiesce_calls = 0

        async def quiesce_execution_tasks(self) -> None:
            self.quiesce_calls += 1

        async def recover_unsettled_match_ratings(self) -> int:
            return 0

    orch = Orch()

    async def reconcile() -> int:
        reconcile_entered.set()
        await release_reconcile.wait()
        return 0

    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=False,
        contest_reconciler=reconcile,
    )
    store.executions.pause("manual:test concurrent resume", bounded_retry=False)

    async def exercise() -> tuple[bool, bool]:
        first = asyncio.create_task(dispatcher.admin_resume())
        await cleanup_entered.wait()
        second = asyncio.create_task(dispatcher.admin_resume())
        await asyncio.sleep(0)
        assert runtime.cleanup_calls == 1
        release_cleanup.set()
        await reconcile_entered.wait()
        # No claim path may observe running while the first recovery still
        # awaits application reconciliation.
        assert store.executions.control()["dispatcher_state"] == "paused"
        assert second.done() is False
        release_reconcile.set()
        return await first, await second

    assert asyncio.run(exercise()) == (True, True)
    assert runtime.cleanup_calls == 1
    assert orch.quiesce_calls == 1
    assert store.executions.control()["dispatcher_state"] == "running"


def test_legacy_queue_migration_rejects_orphan_decision_before_drop(tmp_path):
    path = str(tmp_path / "legacy-orphan-decision.db")
    legacy = Store(path)
    with legacy._tx() as conn:
        _create_legacy_auto_match_queue(conn)
        conn.execute(
            "INSERT INTO auto_match_queue VALUES(1,999999,'holdem',1,2,1,2,"
            "'queued',NULL,'2026-08-09T12:00:00',NULL)"
        )
    legacy.close()

    opened = None
    try:
        with pytest.raises(RuntimeError, match="缺失 decision 的孤儿行"):
            opened = Store(path)
    finally:
        if opened is not None:
            opened.close()

    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT COUNT(*) FROM auto_match_queue"
        ).fetchone()[0] == 1
        assert check.execute(
            "SELECT decision_id,status,match_id FROM auto_match_queue"
        ).fetchone() == (999999, "queued", None)
        assert check.execute(
            "SELECT COUNT(*) FROM execution_jobs"
        ).fetchone()[0] == 0


def test_legacy_dispatched_missing_match_fails_before_drop(tmp_path):
    path = str(tmp_path / "legacy-missing-match.db")
    legacy = Store(path)
    a = _bot(legacy, "legacy-missing-match-a")
    b = _bot(legacy, "legacy-missing-match-b")
    missing_match_id = "legacy-missing-match"
    with legacy._tx() as conn:
        decision = _insert_auto_decision(
            conn,
            a,
            b,
            created_at="2026-08-09T12:00:00",
        )
        conn.execute(
            "UPDATE auto_match_decisions SET lifecycle='dispatched',match_id=?,"
            "attempt_count=1,dispatched_at=NULL WHERE id=?",
            (missing_match_id, decision),
        )
        _create_legacy_auto_match_queue(conn)
        conn.execute(
            "INSERT INTO auto_match_queue VALUES(1,?,'holdem',?,?,?,?,"
            "'dispatched',?,'2026-08-09T12:00:00',NULL)",
            (
                decision,
                a["bot_id"],
                b["bot_id"],
                a["version_id"],
                b["version_id"],
                missing_match_id,
            ),
        )
    legacy.close()

    opened = None
    try:
        with pytest.raises(RuntimeError, match="match 索引或实体缺失"):
            opened = Store(path)
    finally:
        if opened is not None:
            opened.close()

    with sqlite3.connect(path) as check:
        assert check.execute(
            "SELECT COUNT(*) FROM auto_match_queue"
        ).fetchone()[0] == 1
        assert check.execute(
            "SELECT decision_id,status,match_id,dispatched_at "
            "FROM auto_match_queue"
        ).fetchone() == (decision, "dispatched", missing_match_id, None)
        assert check.execute(
            "SELECT COUNT(*) FROM execution_jobs"
        ).fetchone()[0] == 0
        assert check.execute(
            "SELECT job_public_id FROM auto_match_decisions WHERE id=?",
            (decision,),
        ).fetchone()[0] is None


def test_legacy_queue_migration_is_idempotent_and_preserves_business_rows(tmp_path):
    path = str(tmp_path / "legacy.db")
    legacy = Store(path)
    a, b = _bot(legacy, "legacy-a"), _bot(legacy, "legacy-b")
    legacy.create_match(
        "business-match",
        a["bot_id"],
        b["bot_id"],
        owner_id=a["user_id"],
        match_type=TYPE_CHALLENGE,
        game_id="holdem",
    )
    legacy.update_match(
        "business-match",
        status="completed",
        winner=0,
        reason="completed",
        result={"rounds_played": 1, "deltas": [2, -2], "normalized_delta": 2},
        ended_at="2026-08-09T12:00:00",
    )
    ratings = [legacy.get_rating(item["bot_id"], game_id="holdem") for item in (a, b)]
    legacy.apply_match_ratings_atomic(
        a["bot_id"],
        b["bot_id"],
        game_id="holdem",
        rating_a=tuple(ratings[0][key] for key in ("rating", "rd", "vol")),
        rating_b=tuple(ratings[1][key] for key in ("rating", "rd", "vol")),
        winner=0,
        delta_a=2,
        delta_b=-2,
        reason="business",
        settlement_id="business-match",
    )
    legacy.create_match(
        "legacy-active-match",
        a["bot_id"],
        b["bot_id"],
        match_type=TYPE_LADDER,
        game_id="holdem",
    )
    legacy.update_match(
        "legacy-active-match",
        status="running",
        started_at="2026-08-09T12:02:00",
    )
    legacy.upsert_replay("legacy-active-match", "[]")
    with legacy._tx() as conn:
        business_before = {
            "match": tuple(conn.execute(
                "SELECT * FROM matches_holdem WHERE id='business-match'"
            ).fetchone()),
            "ratings": [tuple(row) for row in conn.execute(
                "SELECT * FROM ratings ORDER BY bot_id,game_id"
            ).fetchall()],
            "history": [tuple(row) for row in conn.execute(
                "SELECT * FROM rating_history ORDER BY id"
            ).fetchall()],
            "settlements": [tuple(row) for row in conn.execute(
                "SELECT * FROM match_rating_settlements ORDER BY match_id"
            ).fetchall()],
        }
        conn.execute(
            "ALTER TABLE auto_match_decisions ADD COLUMN claim_dispatcher_token TEXT"
        )
        conn.execute("PRAGMA ignore_check_constraints=ON")
        decision_sql = (
            "INSERT INTO auto_match_decisions("
            "policy_version,state_revision,cursor_game_idx,requested_lane,actual_lane,"
            "game_id,bot_a_id,bot_b_id,owner_a_id,owner_b_id,"
            "bot_a_version_id,bot_b_version_id,owner_a_service_before,"
            "owner_b_service_before,bot_a_service_before,bot_b_service_before,"
            "bot_pair_count_before,owner_pair_count_before,rating_gap,"
            "bot_a_seat_debt_before,bot_b_seat_debt_before,selection_reason,created_at) "
            "VALUES('legacy-v2',1,0,'placement','formal','holdem',?,?,?,?,?,?,"
            "0,0,0,0,0,0,12.0,0,0,'legacy',?)"
        )
        decision_args = (
            a["bot_id"], b["bot_id"], a["user_id"], b["user_id"],
            a["version_id"], b["version_id"],
        )
        decision = conn.execute(
            decision_sql,
            (*decision_args, "2026-08-09T12:01:00"),
        ).lastrowid
        active_decision = conn.execute(
            decision_sql,
            (*decision_args, "2026-08-09T12:02:00"),
        ).lastrowid
        conn.execute(
            "UPDATE auto_match_decisions SET lifecycle='dispatched',"
            "match_id='legacy-active-match',attempt_count=1,dispatched_at=NULL "
            "WHERE id=?",
            (active_decision,),
        )
        conn.execute("PRAGMA ignore_check_constraints=OFF")
        conn.execute(
            "CREATE TABLE auto_match_queue("
            "id INTEGER PRIMARY KEY,decision_id INTEGER,game_id TEXT,"
            "bot_a_id INTEGER,bot_b_id INTEGER,bot_a_version_id INTEGER,"
            "bot_b_version_id INTEGER,status TEXT,match_id TEXT,"
            "created_at TEXT,dispatched_at TEXT)"
        )
        conn.execute(
            "INSERT INTO auto_match_queue VALUES(1,?,'holdem',?,?,?,?,"
            "'queued',NULL,'2026-08-09T12:01:00',NULL)",
            (
                decision, a["bot_id"], b["bot_id"],
                a["version_id"], b["version_id"],
            ),
        )
        conn.execute(
            "INSERT INTO auto_match_queue VALUES(2,?,'holdem',?,?,?,?,"
            "'dispatched','legacy-active-match','2026-08-09T12:02:00',NULL)",
            (
                active_decision, a["bot_id"], b["bot_id"],
                a["version_id"], b["version_id"],
            ),
        )
        conn.execute(
            "CREATE TABLE auto_match_control(singleton INTEGER PRIMARY KEY,enabled INTEGER)"
        )
        conn.execute("INSERT INTO auto_match_control VALUES(1,0)")
        conn.execute("CREATE TABLE auto_match_dispatcher(singleton INTEGER)")
        conn.execute("CREATE TABLE auto_match_daily_claims(match_id TEXT PRIMARY KEY)")
    legacy.set_setting("auto_match_daily_cap", "5")
    legacy.set_setting("auto_match_enabled", "1")
    legacy.close()

    migrated = Store(path)
    with migrated._tx() as conn:
        assert tuple(conn.execute(
            "SELECT requested_lane,actual_lane FROM auto_match_decisions"
        ).fetchone()) == ("bootstrap", "established")
        jobs = conn.execute(
            "SELECT source,status,current_match_id,claimed_at,cleanup_state,last_error "
            "FROM execution_jobs ORDER BY id"
        ).fetchall()
        assert tuple(jobs[0]) == ("auto", "queued", None, None, "none", "")
        assert tuple(jobs[1]) == (
            "auto",
            "settling",
            "legacy-active-match",
            "2026-08-09T12:02:00",
            "pending",
            "legacy_execution_unscoped",
        )
        attempt = conn.execute(
            "SELECT a.status,a.match_id,a.created_at,a.terminal_reason "
            "FROM execution_job_attempts a JOIN execution_jobs j ON j.id=a.job_id "
            "WHERE j.current_match_id='legacy-active-match'"
        ).fetchone()
        assert tuple(attempt) == (
            "settling",
            "legacy-active-match",
            "2026-08-09T12:02:00",
            "legacy_execution_unscoped",
        )
        control = conn.execute(
            "SELECT dispatcher_state,accepting,pause_reason FROM execution_control "
            "WHERE singleton=1"
        ).fetchone()
        assert tuple(control[:2]) == ("paused", 1)
        assert str(control[2]).startswith("manual:")
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert not {
            "auto_match_queue",
            "auto_match_control",
            "auto_match_dispatcher",
            "auto_match_daily_claims",
        } & tables
        assert "claim_dispatcher_token" not in {
            row[1] for row in conn.execute("PRAGMA table_info(auto_match_decisions)")
        }
        business_after = {
            "match": tuple(conn.execute(
                "SELECT * FROM matches_holdem WHERE id='business-match'"
            ).fetchone()),
            "ratings": [tuple(row) for row in conn.execute(
                "SELECT * FROM ratings ORDER BY bot_id,game_id"
            ).fetchall()],
            "history": [tuple(row) for row in conn.execute(
                "SELECT * FROM rating_history ORDER BY id"
            ).fetchall()],
            "settlements": [tuple(row) for row in conn.execute(
                "SELECT * FROM match_rating_settlements ORDER BY match_id"
            ).fetchall()],
        }
        assert business_after == business_before
    assert migrated.get_setting("auto_match_daily_cap") is None
    assert migrated.get_setting("auto_match_enabled") is None
    assert migrated.get_auto_match_enabled() is False
    migrated.close()

    reopened = Store(path)
    assert reopened._conn.execute("SELECT COUNT(*) FROM execution_jobs").fetchone()[0] == 2
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM execution_job_attempts"
    ).fetchone()[0] == 1
    active_after_reopen = reopened._conn.execute(
        "SELECT status,current_match_id,claimed_at,cleanup_state,last_error "
        "FROM execution_jobs WHERE current_match_id='legacy-active-match'"
    ).fetchone()
    assert tuple(active_after_reopen) == (
        "settling",
        "legacy-active-match",
        "2026-08-09T12:02:00",
        "pending",
        "legacy_execution_unscoped",
    )
    control_after_reopen = reopened.executions.control()
    assert control_after_reopen["dispatcher_state"] == "paused"
    assert control_after_reopen["accepting"] == 1
    assert str(control_after_reopen["pause_reason"]).startswith("manual:")
    assert reopened._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()
