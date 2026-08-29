"""全来源持久执行队列、崩溃恢复与 Docker supervisor 定向测试。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.matches.execution_queue import (
    DispatcherAlreadyRunning,
    ExecutionDispatcher,
)
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.runtime import limits as runtime_limits
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
    ExecutionScope,
    SandboxControlUncertain,
)
from bzplat.backend.runtime.limits import HostResourceBudget, PLATFORM_LOW_PROFILE
from bzplat.backend.runtime.local_ai import LocalAIHub
from bzplat.backend.runtime.local_ai_service import LocalAIService
from bzplat.backend.runtime.config import (
    AUTO_MATCH_COOLDOWN_SECONDS,
    AUTO_MATCH_CONTEST_GUARD_SECONDS,
    AUTO_MATCH_IDLE_GRACE_SECONDS,
    AUTO_MATCH_SCHEDULER_POLICY_VERSION,
    EXECUTION_AUTO_ACTIVE_LIMIT,
    EXECUTION_AUTO_LOOKAHEAD,
)
from bzplat.backend.store import Store, rating_projection_digests
from bzplat.backend.store.execution import (
    BOT_EXCLUSIVITY_POLICY,
    CONTEST_FAIRNESS_POLICY,
    DockerLaunchInvariantError,
    ExecutionAttemptNotCurrent,
    ExecutionInvariantError,
    ExecutionMaintenanceConflict,
    ExecutionQueueClosed,
)
from bzplat.backend.store.public_contract import sanitize_public_match
from bzplat.backend.store.schema import (
    AUTO_IDLE_POLICY_CUTOVER_REASON,
    AUTO_YIELD_FOREGROUND_REASON,
    EXECUTION_SOURCE_AUTO,
    EXECUTION_SOURCE_CONTEST,
    EXECUTION_SOURCE_HUMAN,
    EXECUTION_SOURCE_MANUAL,
    EXECUTION_SETTLING,
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
            "UPDATE rating_projection_state SET policy_version='owner-ranked-bot-v4',"
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


def _valid_contest_stages_json() -> str:
    """Freeze the minimal real stage contract required by contest claims."""
    return json.dumps(
        [{"key": "rr", "type": "round_robin", "scoring": "poker_3_1_0"}]
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
    store.select_ranked_bot(int(user["id"]), int(bot["id"]), if_empty=True)
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
    store.select_ranked_bot(int(owner["id"]), int(bot["id"]), if_empty=True)
    return {
        "user_id": int(owner["id"]),
        "bot_id": int(bot["id"]),
        "version_id": int(version["id"]),
    }


def _local_agent(store: Store, bot: dict, key: str) -> dict:
    return store.create_local_ai_agent(
        owner_id=int(bot["user_id"]),
        bot_id=int(bot["bot_id"]),
        label=f"local-{key}",
        public_id=f"agent-{key}",
        token_hash=hashlib.sha256(f"token-{key}".encode()).hexdigest(),
        token_hint=f"hint-{key}"[-8:],
    )


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


def _enqueue_contest_pair(
    store: Store,
    contest: dict,
    pair: tuple[dict, dict],
) -> dict:
    a, b = pair
    pairing = store.add_pairing(
        int(contest["id"]),
        a["bot_id"],
        b["bot_id"],
        bot_a_version_id=a["version_id"],
        bot_b_version_id=b["version_id"],
    )
    return store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=a["user_id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=a["bot_id"],
        bot_b_id=b["bot_id"],
        bot_a_version_id=a["version_id"],
        bot_b_version_id=b["version_id"],
        contest_id=int(contest["id"]),
        contest_pairing_id=int(pairing["id"]),
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


def _make_auto_ready(store: Store) -> None:
    old = (datetime.now() - timedelta(minutes=20)).isoformat(timespec="seconds")
    with store._tx() as conn:
        conn.execute(
            "UPDATE auto_match_fair_state SET dispatch_policy_version=?,"
            "next_eligible_at=?,gate_reason='idle_grace' WHERE singleton=1",
            (AUTO_MATCH_SCHEDULER_POLICY_VERSION, old),
        )
        conn.execute(
            "UPDATE execution_jobs SET terminal_at=? WHERE terminal_at IS NOT NULL",
            (old,),
        )


def _claim_auto(store: Store, *, slots: int = 2, units: int = 4) -> dict | None:
    _make_auto_ready(store)
    return store.executions.claim_next(
        max_match_slots=slots,
        max_sandbox_units=units,
        aging_seconds=60,
        user_active_limit=1,
        contest_share_slots=1,
        claim_class="auto",
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
        "bot_a_environment",
        "bot_b_environment",
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
        "bot_a_environment": "platform_low",
        "bot_b_environment": "platform_low",
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
        "bot_a_local_agent_id",
        "bot_b_local_agent_id",
        "host_cpu_millis",
        "host_memory_mb",
        "profile_version",
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


def test_admin_challenge_can_place_foreign_initiator_bot_in_second_seat(
    execution_api,
):
    app, client, store = (
        execution_api.app,
        execution_api.client,
        execution_api.store,
    )
    first_owner = _api_user(store, "admin_seat_one_owner")
    second_owner = _api_user(store, "admin_seat_two_owner")
    administrator = _api_user(store, "admin_seat_one", role="admin")
    first = _owned_bot(store, first_owner, "admin_seat_one_foreign")
    second = _owned_bot(store, second_owner, "admin_seat_two_foreign")
    _verify_projection(store)

    normal_response = client.post(
        "/api/matches/challenge",
        headers=_auth_headers(app, second_owner),
        json={
            "my_bot_id": first["bot_id"],
            "opponent_bot_id": second["bot_id"],
            "my_bot_version_id": first["version_id"],
            "opponent_bot_version_id": second["version_id"],
            "my_seat": 1,
        },
    )
    assert normal_response.status_code == 403

    admin_headers = _auth_headers(app, administrator)
    accepted = client.post(
        "/api/matches/challenge",
        headers=admin_headers,
        json={
            "my_bot_id": first["bot_id"],
            "opponent_bot_id": second["bot_id"],
            "my_bot_version_id": first["version_id"],
            "opponent_bot_version_id": second["version_id"],
            "my_seat": 1,
        },
    )
    assert accepted.status_code == 202
    public_id = accepted.json()["public_id"]
    row = store._conn.execute(
        "SELECT owner_user_id,bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id "
        "FROM execution_jobs WHERE public_id=?",
        (public_id,),
    ).fetchone()
    assert tuple(row) == (
        administrator["id"],
        second["bot_id"],
        first["bot_id"],
        second["version_id"],
        first["version_id"],
    )

    wrong_version = client.post(
        "/api/matches/challenge",
        headers=admin_headers,
        json={
            "my_bot_id": first["bot_id"],
            "opponent_bot_id": second["bot_id"],
            "my_bot_version_id": second["version_id"],
            "opponent_bot_version_id": second["version_id"],
            "my_seat": 1,
        },
    )
    assert wrong_version.status_code == 400
    assert "版本不存在或不属于" in wrong_version.json()["detail"]
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs"
    ).fetchone()[0] == 1


def test_challenge_owner_can_choose_second_seat_with_full_version_mapping(
    execution_api,
    monkeypatch,
):
    app, client, store = (
        execution_api.app,
        execution_api.client,
        execution_api.store,
    )
    owner = _api_user(store, "seat_two_owner")
    opponent_owner = _api_user(store, "seat_two_opponent")
    own = _owned_bot(store, owner, "seat_two_owned")
    opponent = _owned_bot(store, opponent_owner, "seat_two_foreign")
    _verify_projection(store)
    headers = _auth_headers(app, owner)
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )

    accepted = client.post(
        "/api/matches/challenge",
        headers=headers,
        json={
            "my_bot_id": own["bot_id"],
            "opponent_bot_id": opponent["bot_id"],
            "my_bot_version_id": own["version_id"],
            "opponent_bot_version_id": opponent["version_id"],
            "my_seat": 1,
            "game_id": "holdem",
        },
    )
    assert accepted.status_code == 202, accepted.text
    public_id = accepted.json()["public_id"]
    row = store._conn.execute(
        "SELECT owner_user_id,bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id,"
        "bot_a_environment,bot_b_environment,rated,rating_reason "
        "FROM execution_jobs WHERE public_id=?",
        (public_id,),
    ).fetchone()
    assert tuple(row) == (
        owner["id"],
        opponent["bot_id"],
        own["bot_id"],
        opponent["version_id"],
        own["version_id"],
        "platform_low",
        "platform_low",
        1,
        "eligible",
    )
    assert audits == [{
        "action": "match_challenge",
        "result": "ok",
        "user": owner["username"],
        "target": public_id,
        "detail": (
            f"seat0_bot_id={opponent['bot_id']};"
            f"seat1_bot_id={own['bot_id']};my_seat=1"
        ),
    }]

    # my_seat 只改变物理位置，绝不能把“对手字段恰好属于本人”当作 owner 授权。
    denied = client.post(
        "/api/matches/challenge",
        headers=headers,
        json={
            "my_bot_id": opponent["bot_id"],
            "opponent_bot_id": own["bot_id"],
            "my_seat": 1,
        },
    )
    assert denied.status_code == 403
    assert store._conn.execute("SELECT COUNT(*) FROM execution_jobs").fetchone()[0] == 1

    invalid = client.post(
        "/api/matches/challenge",
        headers=headers,
        json={
            "my_bot_id": own["bot_id"],
            "opponent_bot_id": opponent["bot_id"],
            "my_seat": 2,
        },
    )
    assert invalid.status_code == 422
    assert store._conn.execute("SELECT COUNT(*) FROM execution_jobs").fetchone()[0] == 1


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
    repeated = client.post(
        "/api/matches/challenge",
        headers=headers,
        json={**body, "my_seat": 0},
    )
    assert repeated.status_code == 202
    assert repeated.json()["public_id"] == request_id
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE public_id=?", (request_id,)
    ).fetchone()[0] == 1

    changed_seat = client.post(
        "/api/matches/challenge",
        headers=headers,
        json={**body, "my_seat": 1},
    )
    assert changed_seat.status_code == 409
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

    assert _claim_auto(store) is None
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


def test_auto_refill_clamps_internal_target_to_one(queue_store):
    store = queue_store
    for index in range(4):
        _bot(store, f"refill-clamp-{index}")
    _verify_projection(store)
    _make_auto_ready(store)

    result = store.executions.refill_auto(
        target_queued=99,
        bootstrap_target_matches=10,
    )

    assert result["inserted"] == 1
    assert result["queued"] == EXECUTION_AUTO_LOOKAHEAD == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE source='auto' AND status='queued'"
    ).fetchone()[0] == 1


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
    auto_claim = _claim_auto(store)
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


def test_dispatcher_claims_foreground_sources_and_holds_auto_outside_idle_policy(
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
        stages_json=_valid_contest_stages_json(),
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
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=False,
    )
    expected_jobs = [
        manual["public_id"],
        human_job["public_id"],
        contest_job["public_id"],
    ]
    expected_sources = [
        EXECUTION_SOURCE_MANUAL,
        EXECUTION_SOURCE_HUMAN,
        EXECUTION_SOURCE_CONTEST,
    ]

    for index, expected_id in enumerate(expected_jobs):
        result = asyncio.run(dispatcher.run_once())
        assert result["claimed"] == 1
        assert [job["public_id"] for job in orch.started] == expected_jobs[: index + 1]
        assert [job["source"] for job in orch.started] == expected_sources[: index + 1]

        capacity = store.executions.snapshot(
            max_match_slots=1,
            max_sandbox_units=2,
            aging_seconds=60,
        )["capacity"]
        assert capacity["used_match_slots"] == 1
        assert capacity["used_sandbox_units"] in {1, 2}

        claimed = orch.started[-1]
        store.update_match(
            claimed["current_match_id"],
            status="aborted",
            reason="test_sequential_slot_release",
        )
        with store._tx() as conn:
            conn.execute(
                "UPDATE execution_jobs SET status='settling',settling_at=? "
                "WHERE public_id=?",
                ("2026-08-12T12:00:00", expected_id),
            )
        store.executions.mark_cleanup_confirmed(
            expected_id,
            int(claimed["attempt_count"]),
        )
        assert store.executions.finalize_ready() == 1

    assert asyncio.run(dispatcher.run_once())["claimed"] == 0
    held_auto = store.executions.get(automatic["public_id"])
    assert held_auto["status"] == "cancelled"
    assert held_auto["terminal_reason"] == AUTO_IDLE_POLICY_CUTOVER_REASON

    capacity = store.executions.snapshot(
        max_match_slots=1,
        max_sandbox_units=2,
        aging_seconds=60,
    )["capacity"]
    assert capacity["used_match_slots"] == 0
    assert capacity["used_sandbox_units"] == 0
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
        stages_json=_valid_contest_stages_json(),
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
    # Auto is outside the foreground contest-share class. Once the only manual
    # request has claimed, it cannot reserve the remaining slot from a contest.
    assert _claim(store, slots=3, units=6)["public_id"] == second_contest["public_id"]
    assert store.executions.get(automatic["public_id"])["status"] == "queued"

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


@pytest.mark.parametrize(
    "foreground_source",
    [
        EXECUTION_SOURCE_MANUAL,
        EXECUTION_SOURCE_HUMAN,
        EXECUTION_SOURCE_CONTEST,
    ],
)
def test_idle_only_foreground_enqueue_yields_queued_and_active_auto(
    queue_store, foreground_source
):
    store = queue_store
    bots = [_bot(store, f"idle-yield-{index}") for index in range(6)]
    active_request = _enqueue_pair(
        store,
        (bots[0], bots[1]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    waiting_request = _enqueue_pair(
        store,
        (bots[2], bots[3]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    active = _claim_auto(store)
    assert active and active["public_id"] == active_request["public_id"]

    if foreground_source == EXECUTION_SOURCE_MANUAL:
        foreground = _enqueue_pair(store, (bots[4], bots[5]))
    elif foreground_source == EXECUTION_SOURCE_HUMAN:
        human = store.create_user(
            "idle-yield-human",
            "idle-yield-human@example.test",
            "hash",
        )
        foreground = store.executions.enqueue(
            source=EXECUTION_SOURCE_HUMAN,
            owner_user_id=human["id"],
            game_id="holdem",
            match_type=TYPE_HUMAN,
            bot_a_id=bots[4]["bot_id"],
            bot_b_id=bots[4]["bot_id"],
            bot_a_version_id=bots[4]["version_id"],
            bot_b_version_id=None,
            human_user_id=human["id"],
            human_seat=1,
        )
    else:
        contest = store.create_contest(
            "Idle yield contest",
            bots[4]["user_id"],
            status="running",
            game_id="holdem",
        )
        pairing = store.add_pairing(
            contest["id"],
            bots[4]["bot_id"],
            bots[5]["bot_id"],
            bot_a_version_id=bots[4]["version_id"],
            bot_b_version_id=bots[5]["version_id"],
        )
        foreground = store.executions.enqueue(
            source=EXECUTION_SOURCE_CONTEST,
            owner_user_id=bots[4]["user_id"],
            game_id="holdem",
            match_type=TYPE_CONTEST,
            bot_a_id=bots[4]["bot_id"],
            bot_b_id=bots[5]["bot_id"],
            bot_a_version_id=bots[4]["version_id"],
            bot_b_version_id=bots[5]["version_id"],
            contest_id=contest["id"],
            contest_pairing_id=pairing["id"],
        )

    yielding = store.executions.get(active_request["public_id"])
    cancelled = store.executions.get(waiting_request["public_id"])
    assert yielding["status"] == "starting"
    assert yielding["cancel_requested"] == 1
    assert yielding["terminal_reason"] == "auto_yield_foreground"
    assert cancelled["status"] == "cancelled"
    assert cancelled["retryable"] == 0
    assert cancelled["terminal_reason"] == "auto_yield_foreground"
    assert store.executions.get(foreground["public_id"])["status"] == "queued"
    scheduler = store.executions.snapshot(
        max_match_slots=2,
        max_sandbox_units=4,
        aging_seconds=60,
    )["auto_scheduler"]
    assert scheduler["state"] == (
        "contest_guard"
        if foreground_source == EXECUTION_SOURCE_CONTEST
        else "foreground_busy"
    )
    assert scheduler["reason"] == "auto_yield_foreground"
    public_request = ExecutionDispatcher(
        SimpleNamespace(), store, max_match_slots=2, max_sandbox_units=4
    ).public_request(foreground["public_id"])
    assert public_request is not None
    assert public_request["ahead_jobs"] == 0
    assert public_request["ahead_sandbox_units"] == 0
    assert public_request["eta"]["min_seconds"] == 0

    store.executions.set_auto_enabled(False)
    disabled = store.executions.snapshot(
        max_match_slots=2,
        max_sandbox_units=4,
        aging_seconds=60,
    )["auto_scheduler"]
    assert disabled["state"] == "disabled"
    assert disabled["reason"] == AUTO_YIELD_FOREGROUND_REASON


def test_idle_only_showcase_enqueue_does_not_yield_auto(queue_store):
    store = queue_store
    bots = [_bot(store, f"showcase-no-yield-{index}") for index in range(6)]
    active_request = _enqueue_pair(
        store,
        (bots[0], bots[1]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    waiting_request = _enqueue_pair(
        store,
        (bots[2], bots[3]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    assert _claim_auto(store)["public_id"] == active_request["public_id"]
    contest = store.create_contest(
        "Showcase does not preempt",
        bots[4]["user_id"],
        status="running",
        game_id="holdem",
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE contests SET showcase_key='idle-only-showcase' WHERE id=?",
            (contest["id"],),
        )
    pairing = store.add_pairing(
        contest["id"],
        bots[4]["bot_id"],
        bots[5]["bot_id"],
        bot_a_version_id=bots[4]["version_id"],
        bot_b_version_id=bots[5]["version_id"],
    )
    store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=bots[4]["user_id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=bots[4]["bot_id"],
        bot_b_id=bots[5]["bot_id"],
        bot_a_version_id=bots[4]["version_id"],
        bot_b_version_id=bots[5]["version_id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
    )
    active = store.executions.get(active_request["public_id"])
    queued = store.executions.get(waiting_request["public_id"])
    assert (active["status"], active["cancel_requested"]) == ("starting", 0)
    assert queued["status"] == "queued"
    assert store.executions.snapshot(
        max_match_slots=2, max_sandbox_units=4, aging_seconds=60
    )["auto_scheduler"]["state"] == "running"


def test_idle_only_foreground_retry_yields_queued_and_active_auto(queue_store):
    store = queue_store
    bots = [_bot(store, f"retry-yield-{index}") for index in range(6)]
    retrying = _enqueue_pair(store, (bots[4], bots[5]))
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status='interrupted',retryable=1,"
            "terminal_reason='transient',terminal_at=?,next_attempt_at=NULL "
            "WHERE public_id=?",
            (
                (datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),
                retrying["public_id"],
            ),
        )
    active_request = _enqueue_pair(
        store,
        (bots[0], bots[1]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    waiting_request = _enqueue_pair(
        store,
        (bots[2], bots[3]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    assert _claim_auto(store)["public_id"] == active_request["public_id"]

    retried = store.executions.retry(
        retrying["public_id"], owner_user_id=bots[4]["user_id"]
    )
    assert retried["status"] == "queued"
    active = store.executions.get(active_request["public_id"])
    waiting = store.executions.get(waiting_request["public_id"])
    assert (active["status"], active["cancel_requested"], active["terminal_reason"]) == (
        "starting",
        1,
        "auto_yield_foreground",
    )
    assert (waiting["status"], waiting["terminal_reason"]) == (
        "cancelled",
        "auto_yield_foreground",
    )


def test_idle_only_aging_and_contest_share_never_consider_auto(queue_store):
    store = queue_store
    bots = [_bot(store, f"idle-order-{index}") for index in range(8)]
    manual = _enqueue_pair(store, (bots[0], bots[1]))
    automatic = _enqueue_pair(
        store,
        (bots[2], bots[3]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET created_at=? WHERE public_id=?",
            (
                (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds"),
                automatic["public_id"],
            ),
        )
    assert _claim(store, slots=2, units=4)["public_id"] == manual["public_id"]
    assert store.executions.get(automatic["public_id"])["status"] == "queued"

    store.update_match(
        store.executions.get(manual["public_id"])["current_match_id"],
        status="aborted",
        reason="platform_error",
    )
    store.executions.mark_cleanup_confirmed(manual["public_id"], 1)
    assert store.executions.finalize_ready() == 1
    contest = store.create_contest(
        "Auto is not a contest-share peer",
        bots[0]["user_id"],
        status="running",
        game_id="holdem",
        stages_json=_valid_contest_stages_json(),
    )
    pairings = [
        store.add_pairing(
            contest["id"],
            bots[offset]["bot_id"],
            bots[offset + 1]["bot_id"],
            bot_a_version_id=bots[offset]["version_id"],
            bot_b_version_id=bots[offset + 1]["version_id"],
        )
        for offset in (4, 6)
    ]
    contest_jobs = [
        store.executions.enqueue(
            source=EXECUTION_SOURCE_CONTEST,
            owner_user_id=bots[0]["user_id"],
            game_id="holdem",
            match_type=TYPE_CONTEST,
            bot_a_id=bots[offset]["bot_id"],
            bot_b_id=bots[offset + 1]["bot_id"],
            bot_a_version_id=bots[offset]["version_id"],
            bot_b_version_id=bots[offset + 1]["version_id"],
            contest_id=contest["id"],
            contest_pairing_id=pairings[index]["id"],
        )
        for index, offset in enumerate((4, 6))
    ]
    queued_auto = _enqueue_pair(
        store,
        (bots[2], bots[3]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    assert _claim(store, slots=3, units=6)["public_id"] == contest_jobs[0]["public_id"]
    assert _claim(store, slots=3, units=6)["public_id"] == contest_jobs[1]["public_id"]
    assert store.executions.get(queued_auto["public_id"])["status"] == "queued"
    reconciled = store.executions.reconcile_auto_scheduler_policy()
    assert reconciled["queued_cancelled"] == 1
    assert store.executions.get(queued_auto["public_id"])["status"] == "cancelled"


def test_idle_only_policy_reconcile_toggle_guard_and_public_contract(queue_store):
    store = queue_store
    pair = (_bot(store, "idle-policy-a"), _bot(store, "idle-policy-b"))
    legacy = _enqueue_pair(
        store,
        pair,
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    first = store.executions.reconcile_auto_scheduler_policy()
    assert first["changed"] is True
    assert first["queued_cancelled"] == 1
    assert store.executions.get(legacy["public_id"])["terminal_reason"] == (
        AUTO_IDLE_POLICY_CUTOVER_REASON
    )
    second = store.executions.reconcile_auto_scheduler_policy()
    assert second["changed"] is False
    assert second["queued_cancelled"] == 0
    assert second["next_eligible_at"] == first["next_eligible_at"]

    _make_auto_ready(store)
    store.executions.set_auto_enabled(False)
    disabled = store.executions.snapshot(
        max_match_slots=2, max_sandbox_units=4, aging_seconds=60
    )["auto_scheduler"]
    assert disabled["state"] == "disabled"
    store.executions.set_auto_enabled(True)
    enabled = store.executions.snapshot(
        max_match_slots=2, max_sandbox_units=4, aging_seconds=60
    )["auto_scheduler"]
    assert enabled["state"] == "cooldown"
    assert enabled["reason"] == "idle_grace"

    contest = store.create_contest(
        "Real contest guard",
        pair[0]["user_id"],
        status="running",
        game_id="holdem",
    )
    guarded = store.executions.snapshot(
        max_match_slots=2, max_sandbox_units=4, aging_seconds=60
    )["auto_scheduler"]
    assert guarded["state"] == "contest_guard"
    with store._tx() as conn:
        conn.execute(
            "UPDATE contests SET showcase_key='idle-policy-showcase' WHERE id=?",
            (contest["id"],),
        )
    assert store.executions.snapshot(
        max_match_slots=2, max_sandbox_units=4, aging_seconds=60
    )["auto_scheduler"]["state"] == "cooldown"

    public = ExecutionDispatcher(
        SimpleNamespace(), store, max_match_slots=2, max_sandbox_units=4
    ).public_snapshot()["auto_scheduler"]
    assert set(public) == {
        "mode",
        "state",
        "reason",
        "idle_required_seconds",
        "cooldown_seconds",
        "max_active",
        "queued_target",
        "next_eligible_at",
    }
    assert public["mode"] == "idle_only"
    assert public["idle_required_seconds"] == AUTO_MATCH_IDLE_GRACE_SECONDS
    assert public["cooldown_seconds"] == AUTO_MATCH_COOLDOWN_SECONDS
    assert public["max_active"] == EXECUTION_AUTO_ACTIVE_LIMIT
    assert public["queued_target"] == EXECUTION_AUTO_LOOKAHEAD


def test_idle_only_policy_cutover_yields_legacy_active_and_queued_once(queue_store):
    store = queue_store
    bots = [_bot(store, f"idle-cutover-{index}") for index in range(4)]
    active_request = _enqueue_pair(
        store,
        (bots[0], bots[1]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    claimed = _claim_auto(store)
    assert claimed and claimed["public_id"] == active_request["public_id"]
    queued_request = _enqueue_pair(
        store,
        (bots[2], bots[3]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE auto_match_fair_state SET dispatch_policy_version='',"
            "next_eligible_at=NULL,gate_reason='idle_grace' WHERE singleton=1"
        )

    first = store.executions.reconcile_auto_scheduler_policy()
    assert first["changed"] is True
    assert first["queued_cancelled"] == 1
    assert first["active_yielding"] == 1
    active = store.executions.get(active_request["public_id"])
    queued = store.executions.get(queued_request["public_id"])
    assert (active["status"], active["cancel_requested"], active["terminal_reason"]) == (
        "starting",
        1,
        AUTO_IDLE_POLICY_CUTOVER_REASON,
    )
    assert (queued["status"], queued["terminal_reason"]) == (
        "cancelled",
        AUTO_IDLE_POLICY_CUTOVER_REASON,
    )
    assert first["auto_scheduler"]["state"] == "yielding"
    assert first["auto_scheduler"]["reason"] == AUTO_IDLE_POLICY_CUTOVER_REASON

    second = store.executions.reconcile_auto_scheduler_policy()
    assert second["changed"] is False
    assert second["queued_cancelled"] == 0
    assert second["active_yielding"] == 0
    assert second["next_eligible_at"] == first["next_eligible_at"]
    assert second["auto_scheduler"]["state"] == "yielding"
    assert second["auto_scheduler"]["reason"] == AUTO_IDLE_POLICY_CUTOVER_REASON
    assert sanitize_public_match(
        {"status": "aborted", "reason": AUTO_IDLE_POLICY_CUTOVER_REASON}
    )["reason"] == AUTO_IDLE_POLICY_CUTOVER_REASON


@pytest.mark.parametrize(
    "observed_events",
    [[], [{"type": "match_start"}]],
    ids=["no-events", "eventful"],
)
def test_idle_only_policy_cutover_precedes_startup_recovery(
    queue_store,
    observed_events,
):
    store = queue_store
    bots = [_bot(store, f"idle-startup-cutover-{index}") for index in range(2)]
    with store._tx() as conn:
        decision_id = _insert_auto_decision(
            conn,
            bots[0],
            bots[1],
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
    automatic = store.executions.enqueue(
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
        game_id="holdem",
        match_type=TYPE_LADDER,
        bot_a_id=bots[0]["bot_id"],
        bot_b_id=bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
        auto_decision_id=decision_id,
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE auto_match_decisions SET job_public_id=? WHERE id=?",
            (automatic["public_id"], decision_id),
        )
    claimed = _claim_auto(store)
    assert claimed and claimed["public_id"] == automatic["public_id"]
    match_id = claimed["current_match_id"]
    store.update_match(match_id, status="running")
    store.upsert_replay(match_id, json.dumps(observed_events))
    with store._tx() as conn:
        conn.execute(
            "UPDATE auto_match_fair_state SET dispatch_policy_version='',"
            "next_eligible_at=NULL,gate_reason='idle_grace' WHERE singleton=1"
        )
    _verify_projection(store)

    class Runtime:
        supervisor = None

        async def cleanup_instance(self) -> None:
            return None

        async def ensure_runtime_ready(self) -> None:
            return None

    class Orch:
        runner = SimpleNamespace(runner=Runtime())

        async def recover_unsettled_match_ratings(self) -> int:
            return 0

    dispatcher = ExecutionDispatcher(
        Orch(),
        store,
        max_match_slots=2,
        max_sandbox_units=4,
        auto_capability_enabled=True,
    )

    async def exercise() -> dict:
        started = await dispatcher.start()
        await dispatcher.stop()
        await dispatcher.close()
        return started

    started = asyncio.run(exercise())
    assert started["outcome"] == "running"
    assert started["auto_reconciled"]["changed"] is True
    assert started["auto_reconciled"]["active_yielding"] == 1
    assert started["recovered"] == {
        "requeued": 0,
        "interrupted": 0,
        "settling": 1,
    }
    match = store.get_match(match_id)
    assert (match["status"], match["reason"]) == (
        "aborted",
        AUTO_IDLE_POLICY_CUTOVER_REASON,
    )
    replay = json.loads(
        store._conn.execute(
            "SELECT events_json FROM match_replays WHERE match_id=?", (match_id,)
        ).fetchone()["events_json"]
    )
    assert replay == [
        *observed_events,
        {"type": "error", "reason": AUTO_IDLE_POLICY_CUTOVER_REASON},
    ]
    attempt = store._conn.execute(
        "SELECT status,terminal_reason FROM execution_job_attempts "
        "WHERE job_id=? AND match_id=?",
        (claimed["id"], match_id),
    ).fetchone()
    assert tuple(attempt) == ("settling", AUTO_IDLE_POLICY_CUTOVER_REASON)
    assert store.executions.finalize_ready() == 1
    terminal = store.executions.get(automatic["public_id"])
    assert (terminal["status"], terminal["terminal_reason"], terminal["retryable"]) == (
        "cancelled",
        AUTO_IDLE_POLICY_CUTOVER_REASON,
        0,
    )
    decision = store._conn.execute(
        "SELECT lifecycle,terminal_reason FROM auto_match_decisions WHERE id=?",
        (decision_id,),
    ).fetchone()
    assert tuple(decision) == ("aborted", AUTO_IDLE_POLICY_CUTOVER_REASON)
    assert store.executions.finalize_ready() == 0


@pytest.mark.parametrize(
    ("match_status", "match_reason", "expected_job", "expected_decision"),
    [
        ("completed", "completed", "completed", "completed"),
        ("aborted", "platform_error", "interrupted", "aborted"),
    ],
)
def test_idle_only_startup_cutover_preserves_terminal_match_winner(
    queue_store,
    match_status,
    match_reason,
    expected_job,
    expected_decision,
):
    store = queue_store
    bots = [
        _bot(store, f"idle-terminal-{match_status}-{index}")
        for index in range(2)
    ]
    with store._tx() as conn:
        decision_id = _insert_auto_decision(
            conn,
            bots[0],
            bots[1],
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
    automatic = store.executions.enqueue(
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
        game_id="holdem",
        match_type=TYPE_LADDER,
        bot_a_id=bots[0]["bot_id"],
        bot_b_id=bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
        auto_decision_id=decision_id,
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE auto_match_decisions SET job_public_id=? WHERE id=?",
            (automatic["public_id"], decision_id),
        )
    claimed = _claim_auto(store)
    assert claimed and claimed["public_id"] == automatic["public_id"]
    match_id = claimed["current_match_id"]
    update: dict[str, object] = {
        "status": match_status,
        "reason": match_reason,
        "ended_at": datetime.now().isoformat(timespec="seconds"),
    }
    if match_status == "completed":
        update.update(
            winner=0,
            result={
                "rounds_played": 1,
                "deltas": [1, -1],
                "normalized_delta": 1,
            },
        )
    store.update_match(match_id, **update)
    store.upsert_replay(match_id, json.dumps([{"type": "match_end"}]))
    with store._tx() as conn:
        # Model a legacy/crash snapshot whose Match terminal commit won before
        # its still-active execution projection was compensated.
        conn.execute(
            "UPDATE execution_jobs SET status='starting',settling_at=NULL,"
            "cleanup_state='none' WHERE public_id=?",
            (automatic["public_id"],),
        )
        conn.execute(
            "UPDATE execution_job_attempts SET status='starting' "
            "WHERE job_id=? AND match_id=?",
            (claimed["id"], match_id),
        )
        conn.execute(
            "UPDATE auto_match_fair_state SET dispatch_policy_version='',"
            "next_eligible_at=NULL,gate_reason='idle_grace' WHERE singleton=1"
        )

    class Runtime:
        supervisor = None

        async def cleanup_instance(self) -> None:
            return None

        async def ensure_runtime_ready(self) -> None:
            return None

    class Orch:
        runner = SimpleNamespace(runner=Runtime())

        async def recover_unsettled_match_ratings(self) -> int:
            return 0

    dispatcher = ExecutionDispatcher(
        Orch(),
        store,
        max_match_slots=2,
        max_sandbox_units=4,
        auto_capability_enabled=True,
    )

    async def exercise() -> dict:
        started = await dispatcher.start()
        await dispatcher.stop()
        await dispatcher.close()
        return started

    started = asyncio.run(exercise())
    assert started["auto_reconciled"]["active_yielding"] == 1
    assert started["recovered"]["settling"] == 1
    recovered_match = store.get_match(match_id)
    assert (recovered_match["status"], recovered_match["reason"]) == (
        match_status,
        match_reason,
    )
    job_before_finalize = store.executions.get(automatic["public_id"])
    assert (
        job_before_finalize["status"],
        job_before_finalize["cancel_requested"],
        job_before_finalize["terminal_reason"],
    ) == ("settling", 0, "")
    assert store.executions.finalize_ready() == 1
    terminal = store.executions.get(automatic["public_id"])
    assert (terminal["status"], terminal["terminal_reason"], terminal["retryable"]) == (
        expected_job,
        match_reason,
        0,
    )
    decision = store._conn.execute(
        "SELECT lifecycle,terminal_reason FROM auto_match_decisions WHERE id=?",
        (decision_id,),
    ).fetchone()
    assert tuple(decision) == (expected_decision, match_reason)


def test_idle_only_published_contest_guard_matches_dispatch_due_contract(queue_store):
    store = queue_store
    pair = (_bot(store, "published-guard-a"), _bot(store, "published-guard-b"))
    _make_auto_ready(store)
    contest = store.create_contest(
        "Published guard boundary",
        pair[0]["user_id"],
        status="published",
        starts_at=None,
        game_id="holdem",
    )
    pairing = store.add_pairing(
        contest["id"],
        pair[0]["bot_id"],
        pair[1]["bot_id"],
        bot_a_version_id=pair[0]["version_id"],
        bot_b_version_id=pair[1]["version_id"],
        scheduled_at=datetime.now().isoformat(timespec="seconds"),
    )

    def scheduler_state() -> str:
        return store.executions.snapshot(
            max_match_slots=2,
            max_sandbox_units=4,
            aging_seconds=60,
        )["auto_scheduler"]["state"]

    # `starts_at=NULL` is the platform's explicit "wait for manual start"
    # contract. A pairing timestamp alone is not dispatchable and must not
    # reserve the automatic lane indefinitely.
    assert scheduler_state() == "ready"
    with store._tx() as conn:
        conn.execute(
            "UPDATE contests SET starts_at='2099-01-01T00:00:00' WHERE id=?",
            (contest["id"],),
        )
    assert scheduler_state() == "ready"

    due = (datetime.now() + timedelta(seconds=120)).isoformat(timespec="seconds")
    outside_window = (
        datetime.now() + timedelta(seconds=AUTO_MATCH_CONTEST_GUARD_SECONDS + 60)
    ).isoformat(timespec="seconds")
    with store._tx() as conn:
        conn.execute(
            "UPDATE contests SET starts_at=? WHERE id=?", (due, contest["id"])
        )
        conn.execute(
            "UPDATE contest_pairings SET scheduled_at=? WHERE id=?",
            (outside_window, pairing["id"]),
        )
    assert scheduler_state() == "ready"
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET scheduled_at=? WHERE id=?",
            (due, pairing["id"]),
        )
    assert scheduler_state() == "contest_guard"


def test_idle_only_contest_guard_exit_starts_full_persistent_grace(tmp_path):
    path = str(tmp_path / "contest-guard-transition.db")
    first = Store(path)
    first.executions.resume()
    _make_auto_ready(first)
    organizer = first.create_user(
        "guard-transition-owner",
        "guard-transition-owner@example.test",
        "hash",
    )
    contest = first.create_contest(
        "Persistent contest guard",
        organizer["id"],
        status="running",
        game_id="holdem",
    )
    entered = first.executions.reconcile_auto_scheduler_policy()
    assert entered["auto_scheduler"]["state"] == "contest_guard"
    changes_after_entry = first._conn.total_changes
    still_guarded = first.executions.reconcile_auto_scheduler_policy()
    assert still_guarded["auto_scheduler"]["state"] == "contest_guard"
    assert first._conn.total_changes == changes_after_entry
    first.executions.set_auto_enabled(False)
    first.executions.set_auto_enabled(True)
    with first._tx() as conn:
        assert conn.execute(
            "SELECT gate_reason FROM auto_match_fair_state WHERE singleton=1"
        ).fetchone()[0] == "busy"
        # Simulate a guard that has remained active well beyond the original
        # deadline. The busy marker, not this stale timestamp, is authoritative.
        conn.execute(
            "UPDATE auto_match_fair_state SET next_eligible_at=? WHERE singleton=1",
            ((datetime.now() - timedelta(hours=1)).isoformat(timespec="seconds"),),
        )
    first.close()

    reopened = Store(path)
    with reopened._tx() as conn:
        assert conn.execute(
            "SELECT gate_reason FROM auto_match_fair_state WHERE singleton=1"
        ).fetchone()[0] == "busy"
        conn.execute(
            "UPDATE contests SET status='finished' WHERE id=?", (contest["id"],)
        )
    # Fail closed before the dispatcher consumes the persisted busy->idle edge.
    assert reopened.executions.snapshot(
        max_match_slots=2, max_sandbox_units=4, aging_seconds=60
    )["auto_scheduler"]["state"] == "cooldown"
    transition_started = datetime.now()
    exited = reopened.executions.reconcile_auto_scheduler_policy()
    assert exited["changed"] is False
    assert exited["auto_scheduler"]["state"] == "cooldown"
    next_eligible = datetime.fromisoformat(str(exited["next_eligible_at"]))
    assert 298 <= (next_eligible - transition_started).total_seconds() <= 301
    with reopened._tx() as conn:
        assert conn.execute(
            "SELECT gate_reason FROM auto_match_fair_state WHERE singleton=1"
        ).fetchone()[0] == "idle_grace"
        conn.execute(
            "UPDATE auto_match_fair_state SET next_eligible_at=? WHERE singleton=1",
            (
                (
                    datetime.now()
                    + timedelta(seconds=AUTO_MATCH_IDLE_GRACE_SECONDS - 1)
                ).isoformat(timespec="seconds"),
            ),
        )
    assert reopened.executions.snapshot(
        max_match_slots=2, max_sandbox_units=4, aging_seconds=60
    )["auto_scheduler"]["state"] == "cooldown"
    with reopened._tx() as conn:
        conn.execute(
            "UPDATE auto_match_fair_state SET next_eligible_at=? WHERE singleton=1",
            ((datetime.now() - timedelta(seconds=1)).isoformat(timespec="seconds"),),
        )
    assert reopened.executions.snapshot(
        max_match_slots=2, max_sandbox_units=4, aging_seconds=60
    )["auto_scheduler"]["state"] == "ready"
    reopened.close()


def test_idle_only_auto_requires_reserved_capacity_and_has_one_active(
    queue_store, monkeypatch
):
    store = queue_store
    pair = (_bot(store, "idle-capacity-a"), _bot(store, "idle-capacity-b"))
    automatic = _enqueue_pair(
        store,
        pair,
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    _make_auto_ready(store)
    claim_args = {
        "aging_seconds": 60,
        "user_active_limit": 1,
        "contest_share_slots": 1,
        "claim_class": "auto",
    }
    assert store.executions.claim_next(
        max_match_slots=1, max_sandbox_units=4, **claim_args
    ) is None
    assert store.executions.claim_next(
        max_match_slots=2, max_sandbox_units=3, **claim_args
    ) is None
    assert runtime_limits.maximum_execution_match_resource_snapshot()[0] == 2
    with monkeypatch.context() as scoped:
        scoped.setattr(
            runtime_limits,
            "maximum_execution_match_resource_snapshot",
            lambda: (3, 4000, 4096),
        )
        assert store.executions.claim_next(
            max_match_slots=2,
            max_sandbox_units=4,
            max_host_cpu_millis=6000,
            max_host_memory_mb=5120,
            **claim_args,
        ) is None
    assert store.executions.claim_next(
        max_match_slots=2,
        max_sandbox_units=4,
        max_host_cpu_millis=5999,
        max_host_memory_mb=5120,
        **claim_args,
    ) is None
    assert store.executions.claim_next(
        max_match_slots=2,
        max_sandbox_units=4,
        max_host_cpu_millis=6000,
        max_host_memory_mb=5119,
        **claim_args,
    ) is None
    claimed = store.executions.claim_next(
        max_match_slots=2,
        max_sandbox_units=4,
        max_host_cpu_millis=6000,
        max_host_memory_mb=5120,
        **claim_args,
    )
    assert claimed and claimed["public_id"] == automatic["public_id"]

    another = _enqueue_pair(
        store,
        (_bot(store, "idle-capacity-c"), _bot(store, "idle-capacity-d")),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    _make_auto_ready(store)
    assert store.executions.claim_next(
        max_match_slots=2, max_sandbox_units=4, **claim_args
    ) is None
    assert store.executions.get(another["public_id"])["status"] == "queued"


def test_idle_only_dispatcher_preempts_auto_with_dedicated_reason(queue_store):
    store = queue_store
    bots = [_bot(store, f"idle-preempt-{index}") for index in range(4)]
    automatic = _enqueue_pair(
        store,
        (bots[0], bots[1]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    claimed_auto = _claim_auto(store)
    assert claimed_auto and claimed_auto["public_id"] == automatic["public_id"]
    foreground = _enqueue_pair(store, (bots[2], bots[3]))

    class PreemptingOrchestrator:
        def __init__(self) -> None:
            self.started: list[str] = []
            self.abort_reasons: list[str] = []

        async def abort_execution_match(self, match_id: str, *, reason: str) -> None:
            self.abort_reasons.append(reason)
            store.abort_match_if_active(match_id, reason=reason)
            current = store.executions.get_by_match(match_id)
            store.executions.mark_cleanup_confirmed(
                current["public_id"], int(current["attempt_count"])
            )

        async def abort_match(self, _match_id: str) -> None:
            raise AssertionError("foreground yield must not use admin abort")

        def start_execution_job(self, job: dict) -> None:
            self.started.append(str(job["public_id"]))

    orch = PreemptingOrchestrator()
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=2,
        max_sandbox_units=4,
        auto_capability_enabled=True,
    )
    result = asyncio.run(dispatcher.run_once())
    assert result["finalized"] == 1
    assert orch.abort_reasons == ["auto_yield_foreground"]
    assert orch.started == [foreground["public_id"]]
    terminal = store.executions.get(automatic["public_id"])
    assert terminal["status"] == "cancelled"
    assert terminal["retryable"] == 0
    assert terminal["terminal_reason"] == "auto_yield_foreground"
    match = store.get_match(claimed_auto["current_match_id"])
    assert match["status"] == "aborted"
    assert match["reason"] == "auto_yield_foreground"
    assert sanitize_public_match(match)["reason"] == "auto_yield_foreground"


def test_idle_only_unstarted_auto_claim_honors_concurrent_foreground_yield(
    queue_store,
):
    store = queue_store
    bots = [_bot(store, f"idle-rollback-auto-{index}") for index in range(4)]
    with store._tx() as conn:
        decision_id = _insert_auto_decision(
            conn,
            bots[0],
            bots[1],
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
    automatic = store.executions.enqueue(
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
        game_id="holdem",
        match_type=TYPE_LADDER,
        bot_a_id=bots[0]["bot_id"],
        bot_b_id=bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
        auto_decision_id=decision_id,
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE auto_match_decisions SET job_public_id=? WHERE id=?",
            (automatic["public_id"], decision_id),
        )
    claimed = _claim_auto(store)
    assert claimed and claimed["public_id"] == automatic["public_id"]
    match_id = claimed["current_match_id"]

    foreground = _enqueue_pair(store, (bots[2], bots[3]))
    yielding = store.executions.get(automatic["public_id"])
    assert (yielding["status"], yielding["cancel_requested"]) == ("starting", 1)
    assert yielding["terminal_reason"] == "auto_yield_foreground"
    # The foreground request may itself disappear before the runner task-start
    # failure is compensated.  The committed yield intent must still win.
    store.executions.request_cancel(
        foreground["public_id"], owner_user_id=bots[2]["user_id"]
    )

    assert store.executions.rollback_unstarted_claim(
        automatic["public_id"], reason="task_start:RuntimeError"
    )
    terminal = store.executions.get(automatic["public_id"])
    assert (
        terminal["status"],
        terminal["cancel_requested"],
        terminal["cleanup_state"],
        terminal["retryable"],
        terminal["current_match_id"],
        terminal["terminal_reason"],
    ) == (
        "cancelled",
        1,
        "confirmed",
        0,
        None,
        "auto_yield_foreground",
    )
    attempt = store._conn.execute(
        "SELECT status,terminal_reason FROM execution_job_attempts "
        "WHERE job_id=? AND match_id=?",
        (int(terminal["id"]), match_id),
    ).fetchone()
    assert tuple(attempt) == ("cancelled", "auto_yield_foreground")
    decision = store._conn.execute(
        "SELECT lifecycle,match_id,terminal_reason FROM auto_match_decisions "
        "WHERE id=?",
        (decision_id,),
    ).fetchone()
    assert tuple(decision) == ("cancelled", None, "auto_yield_foreground")
    assert store.get_match(match_id) is None
    assert store.executions.rollback_unstarted_claim(
        automatic["public_id"], reason="task_start:RuntimeError"
    ) is False


@pytest.mark.parametrize("source", [EXECUTION_SOURCE_MANUAL, EXECUTION_SOURCE_HUMAN])
def test_unstarted_foreground_claim_honors_concurrent_cancel(queue_store, source):
    store = queue_store
    if source == EXECUTION_SOURCE_HUMAN:
        owner, _bot_row, queued = _enqueue_human(store, "rollback-cancel")
        owner_id = int(owner["id"])
    else:
        pair = (
            _bot(store, "rollback-cancel-manual-a"),
            _bot(store, "rollback-cancel-manual-b"),
        )
        owner_id = int(pair[0]["user_id"])
        queued = _enqueue_pair(store, pair, owner_user_id=owner_id)
    claimed = _claim(store)
    assert claimed and claimed["public_id"] == queued["public_id"]
    match_id = claimed["current_match_id"]
    store.executions.request_cancel(queued["public_id"], owner_user_id=owner_id)

    assert store.executions.rollback_unstarted_claim(
        queued["public_id"], reason="task_start:RuntimeError"
    )
    terminal = store.executions.get(queued["public_id"])
    assert (
        terminal["status"],
        terminal["cancel_requested"],
        terminal["cleanup_state"],
        terminal["retryable"],
        terminal["current_match_id"],
        terminal["terminal_reason"],
    ) == ("cancelled", 1, "confirmed", 0, None, "user_cancelled")
    attempt = store._conn.execute(
        "SELECT status,terminal_reason FROM execution_job_attempts "
        "WHERE job_id=? AND match_id=?",
        (int(terminal["id"]), match_id),
    ).fetchone()
    assert tuple(attempt) == ("cancelled", "user_cancelled")
    assert store.get_match(match_id) is None


def test_idle_only_natural_completion_wins_yield_race_exactly_once(queue_store):
    store = queue_store
    bots = [_bot(store, f"idle-completion-wins-{index}") for index in range(4)]
    _verify_projection(store)
    with store._tx() as conn:
        decision_id = _insert_auto_decision(
            conn,
            bots[0],
            bots[1],
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
    automatic = store.executions.enqueue(
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
        game_id="holdem",
        match_type=TYPE_LADDER,
        bot_a_id=bots[0]["bot_id"],
        bot_b_id=bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
        auto_decision_id=decision_id,
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE auto_match_decisions SET job_public_id=? WHERE id=?",
            (automatic["public_id"], decision_id),
        )
    claimed = _claim_auto(store)
    assert claimed and claimed["public_id"] == automatic["public_id"]
    match_id = claimed["current_match_id"]

    foreground = _enqueue_pair(store, (bots[2], bots[3]))
    assert store.executions.get(automatic["public_id"])["cancel_requested"] == 1
    completed = store.update_match(
        match_id,
        status="completed",
        winner=0,
        reason="completed",
        result={
            "rounds_played": 1,
            "deltas": [1, -1],
            "normalized_delta": 0.01,
        },
        ended_at=datetime.now().isoformat(timespec="seconds"),
    )

    class NotificationProbe:
        def __init__(self) -> None:
            self.calls = 0

        def notify_both_owners(self, *_args, **_kwargs) -> None:
            self.calls += 1

    notifier = NotificationProbe()
    orch = MatchOrchestrator(
        store,
        runner=SimpleNamespace(runner=SimpleNamespace()),
        max_concurrent=1,
    )
    orch.notifier = notifier
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=2,
        max_sandbox_units=4,
        auto_capability_enabled=False,
    )

    # The terminal Match committed first.  A later cancellation pass must not
    # overwrite it, and repeated post-processing must remain exactly once.
    asyncio.run(dispatcher._process_cancellations())
    assert store.get_match(match_id)["status"] == "completed"
    asyncio.run(
        orch._safe_postprocess_completed_match(completed, match_id, 0, 1, -1)
    )
    asyncio.run(
        orch._safe_postprocess_completed_match(completed, match_id, 0, 1, -1)
    )
    assert store._conn.execute(
        "SELECT COUNT(*) FROM match_rating_settlements WHERE match_id=?",
        (match_id,),
    ).fetchone()[0] == 1
    assert notifier.calls == 1

    store.executions.mark_cleanup_confirmed(
        automatic["public_id"], int(claimed["attempt_count"])
    )
    assert store.executions.finalize_ready() == 1
    assert store.executions.finalize_ready() == 0
    assert store.executions.get(automatic["public_id"])["status"] == "completed"
    decision = store._conn.execute(
        "SELECT lifecycle FROM auto_match_decisions WHERE id=?", (decision_id,)
    ).fetchone()
    assert decision["lifecycle"] == "completed"
    assert store._conn.execute(
        "SELECT COALESCE(SUM(served_count),0) FROM auto_match_owner_service"
    ).fetchone()[0] == 2
    assert store._conn.execute(
        "SELECT COALESCE(SUM(served_count),0) FROM auto_match_bot_service"
    ).fetchone()[0] == 2
    assert store.executions.get(foreground["public_id"])["status"] == "queued"


@pytest.mark.parametrize("commit_first", ["foreground", "auto"])
def test_idle_only_enqueue_and_auto_claim_linearize_across_connections(
    tmp_path, commit_first, monkeypatch
):
    path = str(tmp_path / f"idle-linearize-{commit_first}.db")
    first = Store(path)
    first.executions.resume()
    bots = [_bot(first, f"linearize-{commit_first}-{index}") for index in range(4)]
    automatic = _enqueue_pair(
        first,
        (bots[0], bots[1]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    _make_auto_ready(first)
    second = Store(path)
    outcome: dict[str, object] = {}
    lock_entered = threading.Event()
    release_lock = threading.Event()
    contender_called = threading.Event()

    if commit_first == "foreground":
        original_insert = second.executions._insert_job_tx

        def held_insert(*args, **kwargs):
            inserted = original_insert(*args, **kwargs)
            if kwargs.get("source") == EXECUTION_SOURCE_MANUAL:
                lock_entered.set()
                assert release_lock.wait(timeout=2)
            return inserted

        monkeypatch.setattr(second.executions, "_insert_job_tx", held_insert)
    else:
        original_match_insert = first.executions._insert_match_tx

        def held_match_insert(conn, *, job, match_id):
            original_match_insert(conn, job=job, match_id=match_id)
            lock_entered.set()
            assert release_lock.wait(timeout=2)

        monkeypatch.setattr(
            first.executions, "_insert_match_tx", held_match_insert
        )

    def claim() -> None:
        contender_called.set()
        outcome["claim"] = first.executions.claim_next(
            max_match_slots=2,
            max_sandbox_units=4,
            aging_seconds=60,
            user_active_limit=1,
            contest_share_slots=1,
            claim_class="auto",
        )

    def enqueue() -> None:
        contender_called.set()
        outcome["foreground"] = second.executions.enqueue(
            source=EXECUTION_SOURCE_MANUAL,
            owner_user_id=bots[2]["user_id"],
            game_id="holdem",
            match_type=TYPE_CHALLENGE,
            bot_a_id=bots[2]["bot_id"],
            bot_b_id=bots[3]["bot_id"],
            bot_a_version_id=bots[2]["version_id"],
            bot_b_version_id=bots[3]["version_id"],
        )

    preferred = threading.Thread(
        target=enqueue if commit_first == "foreground" else claim
    )
    contender = threading.Thread(
        target=claim if commit_first == "foreground" else enqueue
    )
    preferred.start()
    assert lock_entered.wait(timeout=2)
    contender_called.clear()
    contender.start()
    assert contender_called.wait(timeout=2)
    # The preferred connection still owns BEGIN IMMEDIATE here. Releasing its
    # lock fixes the commit order; the other connection can only observe the
    # fully committed foreground-yield or auto-claim state.
    release_lock.set()
    threads = [preferred, contender]
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    auto_after = first.executions.get(automatic["public_id"])
    if commit_first == "foreground":
        assert outcome["claim"] is None
        assert auto_after["status"] == "cancelled"
    else:
        assert outcome["claim"]["public_id"] == automatic["public_id"]
        assert auto_after["status"] == "starting"
        assert auto_after["cancel_requested"] == 1
    assert auto_after["terminal_reason"] == "auto_yield_foreground"
    assert first.executions.get(outcome["foreground"]["public_id"])["status"] == (
        "queued"
    )
    second.close()
    first.close()


@pytest.mark.parametrize(
    "yield_reason",
    ["auto_yield_foreground", AUTO_IDLE_POLICY_CUTOVER_REASON],
)
def test_idle_only_real_orchestrator_yield_has_exact_cleanup_and_no_side_effects(
    queue_store, yield_reason,
):
    store = queue_store
    bots = [_bot(store, f"real-yield-{index}") for index in range(4)]
    _verify_projection(store)
    with store._tx() as conn:
        decision_id = _insert_auto_decision(
            conn,
            bots[0],
            bots[1],
            created_at=datetime.now().isoformat(timespec="seconds"),
        )
    automatic = store.executions.enqueue(
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
        game_id="holdem",
        match_type=TYPE_LADDER,
        bot_a_id=bots[0]["bot_id"],
        bot_b_id=bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
        auto_decision_id=decision_id,
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE auto_match_decisions SET job_public_id=? WHERE id=?",
            (automatic["public_id"], decision_id),
        )
    claimed = _claim_auto(store)
    assert claimed and claimed["public_id"] == automatic["public_id"]
    match_id = claimed["current_match_id"]

    class CleanupProbe:
        supervisor = None

        def __init__(self) -> None:
            self.calls = 0

        async def cleanup_execution(self, scope) -> None:
            self.calls += 1
            scope.mark_cleanup_confirmed()

    class NotificationProbe:
        def __init__(self) -> None:
            self.calls = 0

        def notify_both_owners(self, *_args, **_kwargs) -> None:
            self.calls += 1

    cleanup = CleanupProbe()
    notifier = NotificationProbe()
    orch = MatchOrchestrator(
        store,
        runner=SimpleNamespace(runner=cleanup),
        max_concurrent=1,
    )
    orch.notifier = notifier
    foreground_holder: dict[str, dict] = {}

    async def exercise() -> None:
        entered = asyncio.Event()
        blocked = asyncio.Event()

        async def blocked_inner(_match_id, *, execution_scope=None) -> None:
            assert execution_scope is not None
            entered.set()
            await blocked.wait()

        orch._MatchOrchestrator__run_match_inner = blocked_inner
        orch.start_execution_job(claimed)
        task = orch._tasks[match_id]
        await asyncio.wait_for(entered.wait(), timeout=1)
        if yield_reason == "auto_yield_foreground":
            foreground_holder["job"] = _enqueue_pair(store, (bots[2], bots[3]))
        else:
            with store._tx() as conn:
                conn.execute(
                    "UPDATE auto_match_fair_state SET dispatch_policy_version='' "
                    "WHERE singleton=1"
                )
            reconciled = store.executions.reconcile_auto_scheduler_policy()
            assert reconciled["active_yielding"] == 1
        with pytest.raises(ValueError, match="reason is not allowed"):
            await orch.abort_execution_match(match_id, reason="admin_aborted")
        aborted = await orch.abort_execution_match(
            match_id, reason=yield_reason
        )
        assert aborted["status"] == "aborted"
        assert task.cancelled()

    asyncio.run(exercise())
    assert cleanup.calls == 1
    settling = store.executions.get(automatic["public_id"])
    assert settling["status"] == "settling"
    assert settling["cleanup_state"] == "confirmed"
    assert store.executions.finalize_ready() == 1
    terminal = store.executions.get(automatic["public_id"])
    assert terminal["status"] == "cancelled"
    assert terminal["terminal_reason"] == yield_reason
    decision = store._conn.execute(
        "SELECT lifecycle,terminal_reason FROM auto_match_decisions WHERE id=?",
        (decision_id,),
    ).fetchone()
    assert tuple(decision) == ("aborted", yield_reason)
    assert store._conn.execute(
        "SELECT COUNT(*) FROM match_rating_settlements WHERE match_id=?",
        (match_id,),
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COALESCE(SUM(served_count),0) FROM auto_match_owner_service"
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COALESCE(SUM(served_count),0) FROM auto_match_bot_service"
    ).fetchone()[0] == 0
    assert store._conn.execute("SELECT COUNT(*) FROM notifications").fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COALESCE(SUM(xp),0) FROM users WHERE id IN (?,?,?,?)",
        tuple(bot["user_id"] for bot in bots),
    ).fetchone()[0] == 0
    replay_events = json.loads(store.get_replay(match_id)["events_json"])
    terminal_event = {"type": "error", "reason": yield_reason}
    assert replay_events[-1] == terminal_event
    assert replay_events.count(terminal_event) == 1
    assert notifier.calls == 0
    if foreground_holder:
        assert store.executions.get(
            foreground_holder["job"]["public_id"]
        )["status"] == "queued"


def test_contest_claims_rotate_durably_and_keep_each_contest_fifo(queue_store):
    store = queue_store
    bots = [_bot(store, f"contest-rotation-{index}") for index in range(8)]
    contests = [
        store.create_contest(
            f"Rotation {label}",
            bots[0]["user_id"],
            status="running",
            game_id="holdem",
            stages_json=_valid_contest_stages_json(),
        )
        for label in ("A", "B")
    ]
    jobs_by_contest: list[list[dict]] = [[], []]
    for contest_index, offsets in enumerate(((0, 2), (4, 6))):
        for offset in offsets:
            pairing = store.add_pairing(
                contests[contest_index]["id"],
                bots[offset]["bot_id"],
                bots[offset + 1]["bot_id"],
                bot_a_version_id=bots[offset]["version_id"],
                bot_b_version_id=bots[offset + 1]["version_id"],
            )
            jobs_by_contest[contest_index].append(
                store.executions.enqueue(
                    source=EXECUTION_SOURCE_CONTEST,
                    owner_user_id=bots[0]["user_id"],
                    game_id="holdem",
                    match_type=TYPE_CONTEST,
                    bot_a_id=bots[offset]["bot_id"],
                    bot_b_id=bots[offset + 1]["bot_id"],
                    bot_a_version_id=bots[offset]["version_id"],
                    bot_b_version_id=bots[offset + 1]["version_id"],
                    contest_id=contests[contest_index]["id"],
                    contest_pairing_id=pairing["id"],
                )
            )

    # Jobs were enqueued A1,A2,B1,B2.  Contest-level service rotates while
    # preserving A1<A2 and B1<B2, even when all claims share one-second
    # claimed_at values.
    claimed = [_claim(store, slots=4, units=8) for _ in range(2)]
    assert [row["public_id"] for row in claimed] == [
        jobs_by_contest[0][0]["public_id"],
        jobs_by_contest[1][0]["public_id"],
    ]

    # A fresh Store/repository has no process-local cursor.  The next turns
    # still derive from claimed_at plus immutable attempt ids in SQLite.
    reopened = Store(store.path)
    try:
        claimed.extend(
            reopened.executions.claim_next(
                max_match_slots=4,
                max_sandbox_units=8,
                aging_seconds=60,
                user_active_limit=1,
                contest_share_slots=1,
            )
            for _ in range(2)
        )
    finally:
        reopened.close()
    assert [row["public_id"] for row in claimed] == [
        jobs_by_contest[0][0]["public_id"],
        jobs_by_contest[1][0]["public_id"],
        jobs_by_contest[0][1]["public_id"],
        jobs_by_contest[1][1]["public_id"],
    ]
    repository_snapshot = store.executions.snapshot(
        max_match_slots=4,
        max_sandbox_units=8,
        aging_seconds=60,
    )
    assert repository_snapshot["fairness"] == {
        "contest": CONTEST_FAIRNESS_POLICY,
        "bot_exclusivity": BOT_EXCLUSIVITY_POLICY,
    }
    public = ExecutionDispatcher(
        SimpleNamespace(), store, max_match_slots=4, max_sandbox_units=8
    ).public_snapshot()
    assert public["fairness"] == {
        "contest": "round_robin_v1",
        "bot_exclusivity": "active_execution_v1",
    }
    assert set(public["fairness"]) == {"contest", "bot_exclusivity"}
    assert "contest_id" not in json.dumps(public["fairness"])


def test_contest_rotation_uses_monotonic_attempt_order_across_clock_correction(
    queue_store,
):
    store = queue_store
    bots = [_bot(store, f"contest-clock-{index}") for index in range(4)]
    contests = [
        store.create_contest(
            f"Clock {label}",
            bots[0]["user_id"],
            status="running",
            game_id="holdem",
            stages_json=_valid_contest_stages_json(),
        )
        for label in ("A", "B")
    ]
    jobs = [
        _enqueue_contest_pair(store, contests[index], (bots[index * 2], bots[index * 2 + 1]))
        for index in range(2)
    ]
    with store._tx() as conn:
        # A was served first while the host clock was incorrectly in 2030; B
        # was served second after correction to 2026.  AUTOINCREMENT attempt
        # ids, not wall time, must make A the next least-recently-served contest.
        conn.execute(
            "INSERT INTO execution_job_attempts("
            "job_id,attempt_no,match_id,status,created_at,terminal_at) "
            "VALUES(?,1,?,'completed',?,?)",
            (int(jobs[0]["id"]), "clock-a", "2030-01-01T00:00:00", "2030-01-01T00:00:01"),
        )
        conn.execute(
            "INSERT INTO execution_job_attempts("
            "job_id,attempt_no,match_id,status,created_at,terminal_at) "
            "VALUES(?,1,?,'completed',?,?)",
            (int(jobs[1]["id"]), "clock-b", "2026-01-01T00:00:00", "2026-01-01T00:00:01"),
        )
        raw = [
            dict(conn.execute("SELECT * FROM execution_jobs WHERE id=?", (job["id"],)).fetchone())
            for job in jobs
        ]
        ordered = store.executions._fair_contest_rows_tx(conn, raw)

    assert [row["public_id"] for row in ordered] == [
        jobs[0]["public_id"],
        jobs[1]["public_id"],
    ]


def test_contest_rotation_cannot_cross_manual_priority_barrier(queue_store):
    store = queue_store
    bots = [_bot(store, f"contest-band-{index}") for index in range(6)]
    contests = [
        store.create_contest(
            f"Contest band {label}",
            bots[0]["user_id"],
            status="running",
            game_id="holdem",
            stages_json=_valid_contest_stages_json(),
        )
        for label in ("A", "B")
    ]
    contest_a = _enqueue_contest_pair(store, contests[0], (bots[0], bots[1]))
    manual = _enqueue_pair(store, (bots[2], bots[3]))
    contest_b = _enqueue_contest_pair(store, contests[1], (bots[4], bots[5]))

    now = datetime.now()
    contest_a_created = (now - timedelta(minutes=20)).isoformat(
        timespec="seconds"
    )
    foreground_created = now.isoformat(timespec="seconds")
    older_b_service = (now - timedelta(hours=2)).isoformat(timespec="seconds")
    newer_a_service = (now - timedelta(hours=1)).isoformat(timespec="seconds")
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET created_at=? WHERE id=?",
            (contest_a_created, int(contest_a["id"])),
        )
        conn.execute(
            "UPDATE execution_jobs SET created_at=? WHERE id IN (?,?)",
            (foreground_created, int(manual["id"]), int(contest_b["id"])),
        )
        # Persist prior service without a process-local cursor.  B was served
        # less recently, so an unconstrained contest rotation would put B first.
        conn.executemany(
            "INSERT INTO execution_job_attempts("
            "job_id,attempt_no,match_id,status,created_at,terminal_at) "
            "VALUES(?,1,?,'completed',?,?)",
                (
                    (
                        int(contest_b["id"]),
                        "contest-band-history-b",
                        older_b_service,
                        older_b_service,
                    ),
                    (
                        int(contest_a["id"]),
                        "contest-band-history-a",
                        newer_a_service,
                        newer_a_service,
                    ),
                ),
            )
        raw_a = dict(
            conn.execute(
                "SELECT * FROM execution_jobs WHERE id=?", (contest_a["id"],)
            ).fetchone()
        )
        raw_b = dict(
            conn.execute(
                "SELECT * FROM execution_jobs WHERE id=?", (contest_b["id"],)
            ).fetchone()
        )
        assert [
            row["public_id"]
            for row in store.executions._fair_contest_rows_tx(
                conn, [raw_a, raw_b]
            )
        ] == [contest_b["public_id"], contest_a["public_id"]]
        ordered = store.executions._ordered_queued_tx(
            conn, aging_seconds=60
        )

    # A's 20-minute aging bonus beats manual; manual's source priority beats B.
    # Fairness applies only inside each contiguous contest band and cannot turn
    # the durable B history into B>A or B>manual.
    assert [row["public_id"] for row in ordered] == [
        contest_a["public_id"],
        manual["public_id"],
        contest_b["public_id"],
    ]


def test_contest_fairness_history_has_targeted_restart_safe_index(queue_store):
    store = queue_store
    with store._tx() as conn:
        columns = {
            str(row["name"]): tuple(
                str(item["name"])
                for item in conn.execute(
                    f"PRAGMA index_info('{row['name']}')"
                ).fetchall()
            )
            for row in conn.execute("PRAGMA index_list('execution_jobs')")
        }
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT j.contest_id,"
            "MAX(COALESCE(j.claimed_at,a.created_at)),COALESCE(MAX(a.id),0) "
            "FROM execution_jobs j LEFT JOIN execution_job_attempts a "
            "ON a.job_id=j.id WHERE j.source=? AND j.contest_id IN (?,?,?) "
            "AND (j.claimed_at IS NOT NULL OR a.id IS NOT NULL) "
            "GROUP BY j.contest_id",
            (EXECUTION_SOURCE_CONTEST, 1, 2, 3),
        ).fetchall()

    assert columns["idx_execution_jobs_contest_claim_history"] == (
        "source",
        "contest_id",
        "claimed_at",
        "id",
    )
    assert "idx_execution_jobs_contest_claim_history" in " ".join(
        str(row["detail"]) for row in plan
    )


def test_all_capacity_entrypoints_defensively_clamp_to_six_slots(queue_store):
    store = queue_store
    dispatcher = ExecutionDispatcher(
        SimpleNamespace(),
        store,
        max_match_slots=999,
        max_sandbox_units=999,
    )
    orchestrator = MatchOrchestrator(
        store,
        runner=SimpleNamespace(),
        max_concurrent=999,
    )
    capacity = store.executions.snapshot(
        max_match_slots=999,
        max_sandbox_units=999,
        aging_seconds=60,
    )["capacity"]

    assert (dispatcher.max_match_slots, dispatcher.max_sandbox_units) == (6, 12)
    assert orchestrator.max_concurrent == 6
    assert orchestrator._sem._value == 6
    orchestrator.rebuild_concurrency(999)
    assert orchestrator.max_concurrent == 6
    assert orchestrator._sem._value == 6
    assert (capacity["max_match_slots"], capacity["max_sandbox_units"]) == (
        6,
        12,
    )


def test_explicit_zero_sandbox_budget_can_only_tighten_dispatcher(queue_store):
    dispatcher = ExecutionDispatcher(
        SimpleNamespace(),
        queue_store,
        max_match_slots=6,
        max_sandbox_units=0,
    )

    assert dispatcher.max_match_slots == 6
    assert dispatcher.max_sandbox_units == 1


def test_nonrated_bot_conflict_skips_to_next_contest(queue_store):
    store = queue_store
    bots = [_bot(store, f"contest-bot-lock-{index}") for index in range(6)]
    contests = [
        store.create_contest(
            f"Bot lock {label}",
            bots[0]["user_id"],
            status="running",
            game_id="holdem",
            stages_json=_valid_contest_stages_json(),
        )
        for label in ("blocked", "runnable")
    ]

    contest_jobs: list[dict] = []
    for contest_index, (left, right) in enumerate(((0, 2), (3, 4))):
        pairing = store.add_pairing(
            contests[contest_index]["id"],
            bots[left]["bot_id"],
            bots[right]["bot_id"],
            bot_a_version_id=bots[left]["version_id"],
            bot_b_version_id=bots[right]["version_id"],
        )
        contest_jobs.append(
            store.executions.enqueue(
                source=EXECUTION_SOURCE_CONTEST,
                owner_user_id=bots[0]["user_id"],
                game_id="holdem",
                match_type=TYPE_CONTEST,
                bot_a_id=bots[left]["bot_id"],
                bot_b_id=bots[right]["bot_id"],
                bot_a_version_id=bots[left]["version_id"],
                bot_b_version_id=bots[right]["version_id"],
                contest_id=contests[contest_index]["id"],
                contest_pairing_id=pairing["id"],
            )
        )

    _verify_projection(store)
    manual = _enqueue_pair(store, (bots[0], bots[1]))
    assert _claim(store, slots=3, units=6)["public_id"] == manual["public_id"]

    # The first contest is next in fair order but shares a Bot with the active
    # manual match.  Contest jobs are neutral, so this specifically exercises
    # the new all-Bot gate rather than the pre-existing rated-only barrier.
    claimed = _claim(store, slots=3, units=6)
    assert claimed and claimed["public_id"] == contest_jobs[1]["public_id"]
    assert store.executions.get(contest_jobs[0]["public_id"])["status"] == "queued"
    assert int(claimed["rated"]) == 0
    blocked = ExecutionDispatcher(
        SimpleNamespace(), store, max_match_slots=3, max_sandbox_units=6
    ).public_request(contest_jobs[0]["public_id"])
    assert blocked and blocked["blocked_code"] == "bot_busy"
    assert "bot_id" not in json.dumps(blocked, ensure_ascii=False)


def test_human_active_bot_is_globally_exclusive(queue_store):
    store = queue_store
    _, shared_bot, human_job = _enqueue_human(store, "bot-lock-human")
    active = _claim(store, slots=3, units=6)
    assert active and active["public_id"] == human_job["public_id"]
    # Both seat snapshots carry the same Bot id, but the human seat is not a
    # real Bot execution identity and the remaining platform seat is retained.
    assert store.executions._non_human_bot_ids(active) == frozenset(
        {shared_bot["bot_id"]}
    )

    others = [_bot(store, f"bot-lock-human-{index}") for index in range(3)]
    contest = store.create_contest(
        "Human Bot exclusivity",
        shared_bot["user_id"],
        status="running",
        game_id="holdem",
        stages_json=_valid_contest_stages_json(),
    )
    blocked = _enqueue_contest_pair(
        store, contest, (shared_bot, others[0])
    )
    runnable = _enqueue_contest_pair(store, contest, (others[1], others[2]))

    claimed = _claim(store, slots=3, units=6)
    assert claimed and claimed["public_id"] == runnable["public_id"]
    assert store.executions.get(blocked["public_id"])["status"] == "queued"
    projected = store.executions.snapshot(
        max_match_slots=3,
        max_sandbox_units=6,
        aging_seconds=60,
        public_id=blocked["public_id"],
    )["target"]
    assert projected["capacity_blocked_code"] == "bot_busy"


def test_remote_local_active_bot_is_globally_exclusive(queue_store):
    store = queue_store
    owner = _api_user(store, "bot_lock_remote_owner")
    opponent_owner = _api_user(store, "bot_lock_remote_opponent")
    remote_bot = _owned_bot(store, owner, "bot_lock_remote")
    docker_bot = _owned_bot(store, opponent_owner, "bot_lock_remote_docker")
    agent = _local_agent(store, remote_bot, "bot-lock-remote")
    store.executions.set_local_agent_available(lambda _agent_id: True)
    remote_job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=int(owner["id"]),
        game_id="holdem",
        match_type=TYPE_CHALLENGE,
        bot_a_id=remote_bot["bot_id"],
        bot_b_id=docker_bot["bot_id"],
        bot_a_version_id=remote_bot["version_id"],
        bot_b_version_id=docker_bot["version_id"],
        bot_a_environment="remote_local",
        bot_a_local_agent_id=int(agent["id"]),
    )
    active = _claim(store, slots=3, units=6)
    assert active and active["public_id"] == remote_job["public_id"]
    assert store.executions._non_human_bot_ids(active) == frozenset(
        {remote_bot["bot_id"], docker_bot["bot_id"]}
    )

    others = [_bot(store, f"bot-lock-remote-{index}") for index in range(3)]
    contest = store.create_contest(
        "Remote-local Bot exclusivity",
        owner["id"],
        status="running",
        game_id="holdem",
        stages_json=_valid_contest_stages_json(),
    )
    blocked = _enqueue_contest_pair(store, contest, (remote_bot, others[0]))
    runnable = _enqueue_contest_pair(store, contest, (others[1], others[2]))

    claimed = _claim(store, slots=3, units=6)
    assert claimed and claimed["public_id"] == runnable["public_id"]
    assert store.executions.get(blocked["public_id"])["status"] == "queued"


def test_settling_selfplay_bot_is_deduped_and_globally_exclusive(queue_store):
    store = queue_store
    shared_bot = _bot(store, "bot-lock-settling-selfplay")
    selfplay = _enqueue_pair(store, (shared_bot, shared_bot))
    active = _claim(store, slots=3, units=6)
    assert active and active["public_id"] == selfplay["public_id"]
    assert active["rating_reason"] == "self_play"
    assert store.executions._non_human_bot_ids(active) == frozenset(
        {shared_bot["bot_id"]}
    )

    settling_at = datetime.now().isoformat(timespec="seconds")
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status='settling',settling_at=?,"
            "cleanup_state='pending' WHERE id=?",
            (settling_at, int(active["id"])),
        )
        conn.execute(
            "UPDATE execution_job_attempts SET status='settling' WHERE job_id=?",
            (int(active["id"]),),
        )
        assert store.executions._active_non_human_bot_ids_tx(conn) == frozenset(
            {shared_bot["bot_id"]}
        )

    others = [_bot(store, f"bot-lock-settling-{index}") for index in range(3)]
    contest = store.create_contest(
        "Settling selfplay exclusivity",
        shared_bot["user_id"],
        status="running",
        game_id="holdem",
        stages_json=_valid_contest_stages_json(),
    )
    blocked = _enqueue_contest_pair(store, contest, (shared_bot, others[0]))
    runnable = _enqueue_contest_pair(store, contest, (others[1], others[2]))

    claimed = _claim(store, slots=3, units=6)
    assert claimed and claimed["public_id"] == runnable["public_id"]
    assert store.executions.get(blocked["public_id"])["status"] == "queued"


def test_six_slot_resource_admission_lets_one_contest_fill_host(queue_store):
    store = queue_store
    bots = [_bot(store, f"six-slot-{index}") for index in range(14)]
    contest = store.create_contest(
        "Six slot capacity",
        bots[0]["user_id"],
        status="running",
        game_id="holdem",
        stages_json=_valid_contest_stages_json(),
    )
    jobs: list[dict] = []
    for offset in range(0, 14, 2):
        pairing = store.add_pairing(
            contest["id"],
            bots[offset]["bot_id"],
            bots[offset + 1]["bot_id"],
            bot_a_version_id=bots[offset]["version_id"],
            bot_b_version_id=bots[offset + 1]["version_id"],
        )
        jobs.append(
            store.executions.enqueue(
                source=EXECUTION_SOURCE_CONTEST,
                owner_user_id=bots[0]["user_id"],
                game_id="holdem",
                match_type=TYPE_CONTEST,
                bot_a_id=bots[offset]["bot_id"],
                bot_b_id=bots[offset + 1]["bot_id"],
                bot_a_version_id=bots[offset]["version_id"],
                bot_b_version_id=bots[offset + 1]["version_id"],
                contest_id=contest["id"],
                contest_pairing_id=pairing["id"],
            )
        )

    claim_kwargs = {
        "max_match_slots": 6,
        "max_sandbox_units": 12,
        "max_host_cpu_millis": 24_000,
        "max_host_memory_mb": 24 * 1024,
        "aging_seconds": 60,
        "user_active_limit": 1,
        "contest_share_slots": 1,
    }
    claimed = [store.executions.claim_next(**claim_kwargs) for _ in range(6)]
    assert [row["public_id"] for row in claimed] == [
        row["public_id"] for row in jobs[:6]
    ]
    assert store.executions.claim_next(**claim_kwargs) is None
    assert store.executions.get(jobs[6]["public_id"])["status"] == "queued"
    capacity = store.executions.snapshot(
        max_match_slots=6,
        max_sandbox_units=12,
        max_host_cpu_millis=24_000,
        max_host_memory_mb=24 * 1024,
        aging_seconds=60,
    )["capacity"]
    assert capacity["used_match_slots"] == 6
    assert capacity["used_sandbox_units"] == 12
    assert capacity["used_host_cpu_millis"] == 24_000
    assert capacity["used_host_memory_mb"] == 24 * 1024


def test_contest_share_never_leaves_capacity_idle(queue_store):
    store = queue_store
    bots = [_bot(store, f"share-{index}") for index in range(8)]
    organizer = bots[0]["user_id"]
    contest = store.create_contest(
        "Runnable share fallback",
        organizer,
        status="running",
        game_id="holdem",
        stages_json=_valid_contest_stages_json(),
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
        stages_json=_valid_contest_stages_json(),
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
        stages_json=json.dumps(
            [
                {
                    "key": "rr",
                    "type": "round_robin",
                    "scoring": "poker_3_1_0",
                }
            ]
        ),
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


@pytest.mark.parametrize("damaged_cursor", [0.5, -1, "bad"])
def test_contest_claim_rejects_malformed_persisted_stage_cursor(
    queue_store, damaged_cursor
):
    store = queue_store
    bots = [_bot(store, f"cursor-claim-{index}") for index in range(2)]
    contest = store.create_contest(
        "Malformed claim cursor",
        bots[0]["user_id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps(
            [
                {
                    "key": "rr",
                    "type": "round_robin",
                    "scoring": "poker_3_1_0",
                    "duplicate": False,
                    "games_per_pair": 1,
                    "series_scoring": "independent_scoring_game_points_v1",
                }
            ]
        ),
    )
    pairing = store.add_pairing(
        contest["id"],
        bots[0]["bot_id"],
        bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
        stage_idx=0,
    )
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
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET current_stage_idx=? WHERE id=?",
            (damaged_cursor, contest["id"]),
        )

    assert _claim(store, slots=1, units=2) is None
    terminal = store.executions.get(job["public_id"])
    assert terminal["status"] == "cancelled"
    assert terminal["terminal_reason"] == "contest_pairing_changed"
    assert terminal["current_match_id"] is None
    persisted_pairing = store.list_contest_pairings(contest["id"])[0]
    assert persisted_pairing["status"] == "pending"
    assert persisted_pairing["match_id"] is None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM matches_index mi "
        "JOIN matches_holdem m ON m.id=mi.id WHERE m.contest_id=?",
        (contest["id"],),
    ).fetchone()[0] == 0


def _seed_bound_contest_job(
    store: Store,
    key: str,
    *,
    stage: dict,
    pairing_seed: int,
    match_config: dict,
) -> tuple[dict, dict, dict]:
    bots = [_bot(store, f"{key}-{index}") for index in range(2)]
    contest = store.create_contest(
        f"seed-bound-{key}",
        bots[0]["user_id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    pairing = store.add_pairing(
        contest["id"],
        bots[0]["bot_id"],
        bots[1]["bot_id"],
        bot_a_version_id=bots[0]["version_id"],
        bot_b_version_id=bots[1]["version_id"],
        stage_idx=0,
        stage_key=str(stage.get("key") or ""),
        pairing_seed=pairing_seed,
    )
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
        match_config=match_config,
    )
    return contest, pairing, job


def test_contest_duplicate_claim_binds_exact_pairing_seed(queue_store):
    store = queue_store
    seed = 4_242_424
    stage = {
        "key": "dup",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "duplicate": True,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
    }
    contest, pairing, job = _seed_bound_contest_job(
        store,
        "duplicate-seed-happy",
        stage=stage,
        pairing_seed=seed,
        match_config={"duplicate": True, "duplicate_seed": seed},
    )

    claimed = _claim(store, slots=1, units=4)
    assert claimed and claimed["public_id"] == job["public_id"]
    match = store.get_match(claimed["current_match_id"])
    assert match["match_seed"] == seed
    assert match["match_config"]["duplicate_seed"] == seed
    assert store.list_contest_pairings(contest["id"])[0]["match_id"] == match["id"]


def test_contest_claim_rejects_pairing_seed_drift_without_creating_match(queue_store):
    store = queue_store
    seed = 5_151_515
    stage = {
        "key": "dup",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "duplicate": True,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
    }
    contest, pairing, job = _seed_bound_contest_job(
        store,
        "duplicate-seed-drift",
        stage=stage,
        pairing_seed=seed,
        match_config={"duplicate": True, "duplicate_seed": seed},
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET pairing_seed=? WHERE id=?",
            (seed + 1, pairing["id"]),
        )

    assert _claim(store, slots=1, units=4) is None
    terminal = store.executions.get(job["public_id"])
    assert terminal["status"] == "cancelled"
    assert terminal["terminal_reason"] == "contest_pairing_changed"
    assert terminal["current_match_id"] is None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM matches_holdem WHERE contest_id=?",
        (contest["id"],),
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    "match_config",
    [
        {},
        {"duplicate": False, "match_seed": 7},
    ],
)
def test_strict_independent_single_claim_requires_false_flag_and_no_seed(
    queue_store, match_config
):
    store = queue_store
    stage = {
        "key": "single",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "duplicate": False,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
    }
    contest, _pairing, job = _seed_bound_contest_job(
        store,
        f"strict-single-{len(match_config)}",
        stage=stage,
        pairing_seed=6_262_626,
        match_config=match_config,
    )

    assert _claim(store, slots=1, units=4) is None
    assert store.executions.get(job["public_id"])["terminal_reason"] == (
        "contest_pairing_changed"
    )
    assert store._conn.execute(
        "SELECT COUNT(*) FROM matches_holdem WHERE contest_id=?",
        (contest["id"],),
    ).fetchone()[0] == 0


def test_ko_tiebreak_claim_binds_ordinary_match_seed(queue_store):
    store = queue_store
    seed = 7_373_737
    stage = {
        "key": "ko",
        "type": "single_elimination",
        "scoring": "poker_3_1_0",
        "duplicate": False,
        "tiebreak": "paired_swap_until_decided",
    }
    contest, pairing, job = _seed_bound_contest_job(
        store,
        "ko-ordinary-seed",
        stage=stage,
        pairing_seed=seed,
        match_config={"duplicate": False, "match_seed": seed},
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET bracket_slot=0,tiebreak_group=1,"
            "tiebreak_game=1 WHERE id=?",
            (pairing["id"],),
        )

    claimed = _claim(store, slots=1, units=4)
    assert claimed and claimed["public_id"] == job["public_id"]
    match = store.get_match(claimed["current_match_id"])
    assert match["match_seed"] == seed
    assert match["match_config"]["match_seed"] == seed
    assert store.list_contest_pairings(contest["id"])[0]["match_id"] == match["id"]


def test_ko_tiebreak_claim_rejects_duplicate_frozen_stage(queue_store):
    store = queue_store
    seed = 8_484_848
    stage = {
        "key": "ko",
        "type": "single_elimination",
        "scoring": "poker_3_1_0",
        "duplicate": True,
        "tiebreak": "paired_swap_until_decided",
    }
    contest, pairing, job = _seed_bound_contest_job(
        store,
        "ko-duplicate-invalid",
        stage=stage,
        pairing_seed=seed,
        match_config={"duplicate": True, "duplicate_seed": seed},
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET bracket_slot=0,tiebreak_group=1,"
            "tiebreak_game=1 WHERE id=?",
            (pairing["id"],),
        )

    assert _claim(store, slots=1, units=4) is None
    assert store.executions.get(job["public_id"])["terminal_reason"] == (
        "contest_pairing_changed"
    )
    assert store._conn.execute(
        "SELECT COUNT(*) FROM matches_holdem WHERE contest_id=?",
        (contest["id"],),
    ).fetchone()[0] == 0


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
    assert "host_cpu_millis" not in public["capacity"]
    assert "host_memory_mb" not in public["capacity"]
    assert admin["capacity"]["host_cpu_millis"] == {
        "used": 0,
        "capacity": dispatcher.max_host_cpu_millis,
    }
    assert admin["capacity"]["host_memory_mb"] == {
        "used": 0,
        "capacity": dispatcher.max_host_memory_mb,
    }


def test_deployment_drain_finishes_active_work_and_preserves_waiting_jobs(
    queue_store,
):
    store = queue_store
    active_pair = (_bot(store, "drain-active-a"), _bot(store, "drain-active-b"))
    waiting_pair = (_bot(store, "drain-wait-a"), _bot(store, "drain-wait-b"))
    _verify_projection(store)
    active = _enqueue_pair(store, active_pair)
    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == active["public_id"]
    waiting = _enqueue_pair(store, waiting_pair)
    waiting_before = store.executions.get(waiting["public_id"])

    class Runtime:
        supervisor = None

    class Orch:
        runner = SimpleNamespace(runner=Runtime())

        def __init__(self) -> None:
            self.started: list[str] = []

        def active_execution_task_count(self) -> int:
            return 0

        def start_execution_job(self, job: dict) -> None:
            self.started.append(str(job["public_id"]))

    orch = Orch()
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=True,
        uploads_in_flight=lambda: 0,
    )

    control = dispatcher.begin_maintenance("发布候选版本")
    assert control["deployment_drain_requested"] == 1
    assert control["accepting"] == 0
    assert control["auto_enabled"] == 0
    draining = dispatcher.public_snapshot(include_internal=True)
    assert draining["dispatcher"]["state"] == "running"
    assert draining["dispatcher"]["maintenance"] is True
    assert draining["maintenance"] == {
        "requested": True,
        "ready": False,
        "reason": "发布候选版本",
        "active_count": 1,
        "uploads_in_flight": 0,
        "active_local_ai_leases": 0,
        "untracked_running_matches": 0,
        "docker_launch_state": "idle",
        "owned_execution_tasks": 0,
        "readiness_unavailable": [],
    }
    assert _claim(store, slots=1, units=2) is None
    assert store.executions.refill_auto(
        target_queued=1, bootstrap_target_matches=10
    ) == {"outcome": "disabled", "inserted": 0}
    with pytest.raises(ExecutionQueueClosed) as closed:
        _enqueue_pair(
            store,
            (_bot(store, "drain-new-a"), _bot(store, "drain-new-b")),
        )
    assert getattr(closed.value, "code", "") == "deployment_maintenance"
    waiting_after = store.executions.get(waiting["public_id"])
    for field in (
        "public_id",
        "status",
        "bot_a_version_id",
        "bot_b_version_id",
        "match_config",
        "sandbox_units",
        "profile_version",
    ):
        assert waiting_after[field] == waiting_before[field]

    store.update_match(
        claimed["current_match_id"], status="aborted", reason="test_complete"
    )
    store.executions.mark_cleanup_confirmed(active["public_id"], 1)
    iteration = asyncio.run(dispatcher.run_once())
    assert iteration["finalized"] == 1
    assert iteration["claimed"] == 0
    assert orch.started == []
    ready = dispatcher.public_snapshot(include_internal=True)
    assert ready["maintenance"]["ready"] is True
    assert ready["queued"][0]["public_id"] == waiting["public_id"]
    assert ready["queued"][0]["blocked_code"] == "deployment_maintenance"
    assert ready["queued"][0]["blocked_reason"] == (
        "部署维护中，恢复调度后继续排队"
    )
    request = dispatcher.public_request(waiting["public_id"])
    assert request is not None
    assert request["blocked_code"] == "deployment_maintenance"
    assert request["blocked_reason"] == "部署维护中，恢复调度后继续排队"
    assert request["request"]["blocked_code"] == "deployment_maintenance"

    assert asyncio.run(dispatcher.end_maintenance()) is True
    resumed = dispatcher.public_snapshot(include_internal=True)
    assert resumed["dispatcher"]["maintenance"] is False
    assert resumed["dispatcher"]["accepting"] is True
    assert resumed["dispatcher"]["auto_enabled"] is False


def test_deployment_ready_waits_for_launch_lease_callbacks_and_probes(
    queue_store,
):
    store = queue_store
    bot = _bot(store, "drain-local-agent")
    agent = _local_agent(store, bot, "drain-ready")
    store.executions.begin_maintenance("readiness blockers")

    class Runtime:
        supervisor = None

    class NoProbeOrch:
        runner = SimpleNamespace(runner=Runtime())

        def start_execution_job(self, _job: dict) -> None:
            raise AssertionError

    unavailable = ExecutionDispatcher(
        NoProbeOrch(),
        store,
        max_match_slots=1,
        max_sandbox_units=2,
    ).public_snapshot(include_internal=True)
    assert unavailable["maintenance"]["ready"] is False
    assert unavailable["maintenance"]["readiness_unavailable"] == [
        "upload_activity",
        "owned_execution_tasks",
    ]

    owned_count = {"value": 0}

    class Orch(NoProbeOrch):
        def active_execution_task_count(self) -> int:
            return owned_count["value"]

    dispatcher = ExecutionDispatcher(
        Orch(),
        store,
        max_match_slots=1,
        max_sandbox_units=2,
        uploads_in_flight=lambda: 0,
    )
    assert dispatcher.public_snapshot(include_internal=True)["maintenance"][
        "ready"
    ] is True

    owned_count["value"] = 1
    assert dispatcher.public_snapshot(include_internal=True)["maintenance"][
        "ready"
    ] is False
    owned_count["value"] = 0

    store.executions.begin_docker_launch(
        launch_token="maintenance-launch",
        instance_key="qa-maintenance",
        owner_kind="preflight",
        job_public_id="upload-before-drain",
        attempt_no=1,
        slot=0,
        container_name="bz-maintenance-launch",
        host_boot_id="boot-a",
    )
    launch_blocked = dispatcher.public_snapshot(include_internal=True)
    assert launch_blocked["maintenance"]["docker_launch_state"] == "creating"
    assert launch_blocked["maintenance"]["ready"] is False
    store.executions.mark_docker_launch_created("maintenance-launch")
    store.executions.clear_docker_launch_created("maintenance-launch")

    with store._tx() as conn:
        conn.execute(
            "INSERT INTO local_ai_leases("
            "agent_id,job_public_id,attempt_no,seat,status,acquired_at) "
            "VALUES(?, 'upload-before-drain', 1, 0, 'active', 'test')",
            (agent["id"],),
        )
    lease_blocked = dispatcher.public_snapshot(include_internal=True)
    assert lease_blocked["maintenance"]["active_local_ai_leases"] == 1
    assert lease_blocked["maintenance"]["ready"] is False
    with store._tx() as conn:
        conn.execute(
            "UPDATE local_ai_leases SET status='released',released_at='test',"
            "terminal_reason='test' WHERE agent_id=?",
            (agent["id"],),
        )

    store.create_match(
        "maintenance-untracked-running",
        bot["bot_id"],
        bot["bot_id"],
        owner_id=bot["user_id"],
        game_id="holdem",
    )
    store.update_match("maintenance-untracked-running", status="running")
    legacy_blocked = dispatcher.public_snapshot(include_internal=True)
    assert legacy_blocked["maintenance"]["untracked_running_matches"] == 1
    assert legacy_blocked["maintenance"]["ready"] is False
    store.update_match(
        "maintenance-untracked-running",
        status="aborted",
        reason="test cleanup",
    )
    assert dispatcher.public_snapshot(include_internal=True)["maintenance"][
        "ready"
    ] is True


def test_deployment_ready_waits_for_match_completion_callback(queue_store):
    store = queue_store
    store.executions.begin_maintenance("callback barrier")
    orch = MatchOrchestrator(store)
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=1,
        max_sandbox_units=2,
        uploads_in_flight=lambda: 0,
    )

    async def exercise() -> None:
        entered = asyncio.Event()
        release = asyncio.Event()

        async def on_match_done(_match_id: str, _contest_id: int | None) -> None:
            entered.set()
            await release.wait()

        orch.on_match_done = on_match_done
        finish = asyncio.create_task(
            orch._finish_match_task("callback-barrier", 42)
        )
        orch._tasks["callback-barrier"] = finish
        await asyncio.wait_for(entered.wait(), timeout=1)
        blocked = dispatcher.public_snapshot(include_internal=True)
        assert blocked["maintenance"]["owned_execution_tasks"] == 1
        assert blocked["maintenance"]["ready"] is False
        release.set()
        await asyncio.wait_for(finish, timeout=1)
        assert orch.active_execution_task_count() == 0
        assert dispatcher.public_snapshot(include_internal=True)["maintenance"][
            "ready"
        ] is True

    asyncio.run(exercise())


def test_recovery_quiesce_waits_for_admin_abort_handoff(queue_store):
    """Admin abort remains owned through replay and contest callback writes."""
    store = queue_store
    pair = (_bot(store, "abort-quiesce-a"), _bot(store, "abort-quiesce-b"))
    match_id = "abort-quiesce-handoff"
    store.create_match(
        match_id,
        pair[0]["bot_id"],
        pair[1]["bot_id"],
        owner_id=pair[0]["user_id"],
        game_id="holdem",
    )
    store.update_match(match_id, status="running")
    store.executions.begin_maintenance("abort handoff barrier")
    orch = MatchOrchestrator(store)
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=1,
        max_sandbox_units=2,
        uploads_in_flight=lambda: 0,
    )

    async def exercise() -> None:
        callback_entered = asyncio.Event()
        release_callback = asyncio.Event()

        async def on_match_done(
            _match_id: str, _contest_id: int | None
        ) -> None:
            callback_entered.set()
            await release_callback.wait()

        orch.on_match_done = on_match_done
        abort = asyncio.create_task(orch.abort_match(match_id))
        await asyncio.wait_for(callback_entered.wait(), timeout=1)
        blocked = dispatcher.public_snapshot(include_internal=True)
        assert blocked["maintenance"]["active_count"] == 0
        assert blocked["maintenance"]["untracked_running_matches"] == 0
        assert blocked["maintenance"]["owned_execution_tasks"] == 1
        assert blocked["maintenance"]["ready"] is False

        quiesce = asyncio.create_task(orch.quiesce_execution_tasks())
        await asyncio.sleep(0)
        assert quiesce.done() is False
        assert orch._admin_abort_handoffs == {match_id}

        release_callback.set()
        aborted = await asyncio.wait_for(abort, timeout=1)
        assert aborted["status"] == "aborted"
        await asyncio.wait_for(quiesce, timeout=1)

    asyncio.run(exercise())
    assert orch.active_execution_task_count() == 0
    assert orch._admin_abort_operations == set()
    assert dispatcher.public_snapshot(include_internal=True)["maintenance"][
        "ready"
    ] is True


def test_deployment_drain_survives_close_start_and_runtime_pause(
    tmp_path,
):
    path = str(tmp_path / "deployment-drain.db")
    store = Store(path)
    store.executions.resume()
    active_pair = (
        _bot(store, "restart-active-a"),
        _bot(store, "restart-active-b"),
    )
    pair = (_bot(store, "restart-drain-a"), _bot(store, "restart-drain-b"))
    _verify_projection(store)
    active = _enqueue_pair(store, active_pair)
    claimed = _claim(store, slots=1, units=2)
    assert claimed["public_id"] == active["public_id"]
    store.update_match(claimed["current_match_id"], status="running")
    store.upsert_replay(
        claimed["current_match_id"],
        json.dumps([{"type": "match_start"}]),
    )
    waiting = _enqueue_pair(store, pair)
    store.executions.begin_maintenance("等待部署")
    before = store.executions.get(waiting["public_id"])
    store.close()

    reopened = Store(path)

    class Runtime:
        supervisor = None

        def __init__(self) -> None:
            self.cleanup_calls = 0

        async def cleanup_instance(self) -> None:
            self.cleanup_calls += 1

        async def ensure_runtime_ready(self) -> None:
            return None

    runtime = Runtime()

    class Orch:
        runner = SimpleNamespace(runner=runtime)

        async def recover_unsettled_match_ratings(self) -> int:
            return 0

        async def quiesce_execution_tasks(self) -> None:
            return None

        def active_execution_task_count(self) -> int:
            return 0

        def start_execution_job(self, _job: dict) -> None:
            raise AssertionError("deployment drain must not start queued work")

    dispatcher = ExecutionDispatcher(
        Orch(),
        reopened,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=True,
        uploads_in_flight=lambda: 0,
    )

    async def exercise() -> tuple[dict, dict, dict, bool, dict]:
        started = await dispatcher.start()
        once = await dispatcher.run_once()
        reopened.executions.pause(
            "runtime uncertainty during drain", bounded_retry=True
        )
        automatic = await dispatcher.run_once()
        resumed = await dispatcher.admin_resume()
        running = reopened.executions.control()
        await dispatcher.stop()
        await dispatcher.close()
        return started, once, automatic, resumed, running

    started, once, automatic, resumed, running = asyncio.run(exercise())
    assert started["outcome"] == "running"
    assert started["recovered"]["interrupted"] == 1
    assert once["claimed"] == 0
    assert automatic == {"outcome": "paused"}
    assert resumed is True
    assert running["dispatcher_state"] == "running"
    assert running["deployment_drain_requested"] == 1
    assert running["accepting"] == 0
    assert running["auto_enabled"] == 0
    assert reopened.executions.get(active["public_id"])["status"] == "interrupted"
    assert reopened.executions.get(waiting["public_id"]) == before
    assert runtime.cleanup_calls == 2
    stopped = reopened.executions.control()
    assert stopped["dispatcher_state"] == "stopped"
    assert stopped["deployment_drain_requested"] == 1
    assert stopped["accepting"] == 0

    # A normal deployment restart performs cleanup/recovery and comes back as
    # a live dispatcher held behind the persistent drain, never as a stale
    # paused pseudo-ready state.  Only the explicit end transition reopens it.
    restarted_dispatcher = ExecutionDispatcher(
        Orch(),
        reopened,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=True,
        uploads_in_flight=lambda: 0,
    )

    async def restart_and_end() -> tuple[dict, dict, dict]:
        restarted = await restarted_dispatcher.start()
        held = restarted_dispatcher.public_snapshot(include_internal=True)
        await restarted_dispatcher.end_maintenance()
        ended = restarted_dispatcher.public_snapshot(include_internal=True)
        await restarted_dispatcher.stop()
        await restarted_dispatcher.close()
        return restarted, held, ended

    restarted, held, ended = asyncio.run(restart_and_end())
    assert restarted["outcome"] == "running"
    assert held["dispatcher"]["state"] == "running"
    assert held["dispatcher"]["accepting"] is False
    assert held["maintenance"]["requested"] is True
    assert held["maintenance"]["ready"] is True
    assert ended["dispatcher"]["accepting"] is True
    assert ended["dispatcher"]["auto_enabled"] is False
    assert ended["maintenance"]["requested"] is False
    reopened.close()


def test_deployment_drain_begin_rejects_paused_dispatcher(queue_store):
    queue_store.executions.pause(
        "manual:runtime uncertainty", bounded_retry=False
    )
    with pytest.raises(ExecutionMaintenanceConflict) as failed:
        queue_store.executions.begin_maintenance("不能旁路运行时恢复")
    assert getattr(failed.value, "code", "") == "maintenance_state_conflict"
    control = queue_store.executions.control()
    assert control["dispatcher_state"] == "paused"
    assert control["deployment_drain_requested"] == 0


def test_deployment_drain_linearizes_against_concurrent_enqueue(queue_store):
    first = queue_store
    pair = (_bot(first, "drain-race-a"), _bot(first, "drain-race-b"))
    _verify_projection(first)
    second = Store(first.path)
    barrier = threading.Barrier(2)
    outcome: dict[str, object] = {}

    def begin() -> None:
        barrier.wait()
        outcome["begin"] = first.executions.begin_maintenance("race")

    def enqueue() -> None:
        barrier.wait()
        try:
            outcome["enqueue"] = second.executions.enqueue(
                source=EXECUTION_SOURCE_MANUAL,
                owner_user_id=pair[0]["user_id"],
                game_id="holdem",
                match_type=TYPE_CHALLENGE,
                bot_a_id=pair[0]["bot_id"],
                bot_b_id=pair[1]["bot_id"],
                bot_a_version_id=pair[0]["version_id"],
                bot_b_version_id=pair[1]["version_id"],
            )
        except ExecutionQueueClosed as exc:
            outcome["enqueue"] = exc

    threads = [threading.Thread(target=begin), threading.Thread(target=enqueue)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    control = first.executions.control()
    assert control["deployment_drain_requested"] == 1
    assert control["accepting"] == 0
    queued_or_closed = outcome["enqueue"]
    if isinstance(queued_or_closed, Exception):
        assert getattr(queued_or_closed, "code", "") == "deployment_maintenance"
        assert first._conn.execute(
            "SELECT COUNT(*) FROM execution_jobs"
        ).fetchone()[0] == 0
    else:
        assert first.executions.get(queued_or_closed["public_id"])["status"] == "queued"
    assert _claim(first, slots=1, units=2) is None
    second.close()


def test_deployment_drain_linearizes_against_concurrent_claim_and_refill(
    tmp_path,
):
    path = str(tmp_path / "deployment-claim-race.db")
    first = Store(path)
    first.executions.resume()
    queued_pair = (_bot(first, "claim-race-a"), _bot(first, "claim-race-b"))
    auto_pair = (_bot(first, "refill-race-a"), _bot(first, "refill-race-b"))
    _verify_projection(first)
    queued = _enqueue_pair(first, queued_pair)
    second = Store(path)
    barrier = threading.Barrier(2)
    outcome: dict[str, object] = {}

    def begin() -> None:
        barrier.wait()
        outcome["begin"] = first.executions.begin_maintenance("claim race")

    def claim() -> None:
        barrier.wait()
        outcome["claim"] = second.executions.claim_next(
            max_match_slots=1,
            max_sandbox_units=2,
            aging_seconds=60,
            user_active_limit=1,
            contest_share_slots=1,
        )

    threads = [threading.Thread(target=begin), threading.Thread(target=claim)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=3)
        assert not thread.is_alive()

    claimed = outcome["claim"]
    if claimed is None:
        assert first.executions.get(queued["public_id"])["status"] == "queued"
    else:
        assert claimed["public_id"] == queued["public_id"]
        assert first.executions.maintenance_status()["active_count"] == 1
    assert _claim(first, slots=1, units=2) is None
    assert first.executions.refill_auto(
        target_queued=1, bootstrap_target_matches=10
    ) == {"outcome": "disabled", "inserted": 0}
    assert first.executions.control()["auto_enabled"] == 0
    # Creating unrelated candidates after the boundary cannot make refill
    # observable, because its control check linearizes on the same DB writer.
    assert auto_pair[0]["bot_id"] != auto_pair[1]["bot_id"]
    second.close()
    first.close()


def test_deployment_drain_schema_is_fresh_and_legacy_upgrade_safe(tmp_path):
    fresh_path = str(tmp_path / "deployment-fresh.db")
    fresh = Store(fresh_path)
    fresh_control = fresh.executions.control()
    assert fresh_control["deployment_drain_requested"] == 0
    assert fresh_control["deployment_drain_reason"] == ""
    fresh.close()

    legacy_path = str(tmp_path / "deployment-legacy.db")
    legacy = Store(legacy_path)
    legacy.close()
    with sqlite3.connect(legacy_path) as conn:
        conn.execute(
            "ALTER TABLE execution_control DROP COLUMN deployment_drain_reason"
        )
        conn.execute(
            "ALTER TABLE execution_control DROP COLUMN deployment_drain_requested"
        )
    upgraded = Store(legacy_path)
    upgraded_control = upgraded.executions.control()
    assert upgraded_control["deployment_drain_requested"] == 0
    assert upgraded_control["deployment_drain_reason"] == ""
    upgraded.executions.resume()
    upgraded.executions.begin_maintenance("升级后保持")
    upgraded.close()
    reopened = Store(legacy_path)
    assert reopened.executions.control()["deployment_drain_requested"] == 1
    assert reopened.executions.control()["accepting"] == 0
    reopened.close()


def test_deployment_maintenance_api_rbac_audit_and_ready_cas(
    execution_api,
    monkeypatch,
):
    app, client, store = (
        execution_api.app,
        execution_api.client,
        execution_api.store,
    )
    owner = _api_user(store, "maintenance_owner")
    opponent = _api_user(store, "maintenance_opponent")
    organizer = _api_user(store, "maintenance_organizer", role="organizer")
    admin = _api_user(store, "maintenance_admin", role="admin")
    own_bots = [
        _owned_bot(store, owner, f"maintenance_owner_{index}")
        for index in range(2)
    ]
    opponent_bots = [
        _owned_bot(store, opponent, f"maintenance_opponent_{index}")
        for index in range(2)
    ]
    _verify_projection(store)
    active = _enqueue_pair(store, (own_bots[0], opponent_bots[0]))
    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == active["public_id"]
    waiting = _enqueue_pair(store, (own_bots[1], opponent_bots[1]))
    owner_headers = _auth_headers(app, owner)
    organizer_headers = _auth_headers(app, organizer)
    admin_headers = _auth_headers(app, admin)
    path = "/api/admin/execution-queue/maintenance"
    audit_calls: list[dict] = []

    def record_audit(_request, action, **fields):
        audit_calls.append({"action": action, **fields})

    monkeypatch.setattr("bzplat.backend.api_routes.audit_log", record_audit)

    assert client.get(path).status_code == 401
    for forbidden_headers in (owner_headers, organizer_headers):
        assert client.get(path, headers=forbidden_headers).status_code == 403
        assert client.post(
            path, headers=forbidden_headers, json={"reason": "越权"}
        ).status_code == 403
        assert client.delete(path, headers=forbidden_headers).status_code == 403

    begun = client.post(
        path, headers=admin_headers, json={"reason": "部署发布"}
    )
    assert begun.status_code == 200, begun.text
    snapshot = begun.json()
    assert snapshot["dispatcher"]["maintenance"] is True
    assert snapshot["dispatcher"]["accepting"] is False
    assert snapshot["dispatcher"]["auto_enabled"] is False
    assert snapshot["maintenance"]["requested"] is True
    assert snapshot["maintenance"]["ready"] is False
    assert snapshot["maintenance"]["active_count"] == 1
    assert snapshot["queued"][0]["public_id"] == waiting["public_id"]
    assert snapshot["queued"][0]["blocked_code"] == "deployment_maintenance"
    waiting_projection = client.get(
        f"/api/execution-requests/{waiting['public_id']}",
        headers=owner_headers,
    )
    assert waiting_projection.status_code == 200
    assert waiting_projection.json()["blocked_code"] == (
        "deployment_maintenance"
    )
    assert waiting_projection.json()["blocked_reason"] == (
        "部署维护中，恢复调度后继续排队"
    )
    public = app.state.execution_dispatcher.public_snapshot(
        include_internal=False
    )
    assert public["maintenance"]["reason"] == "平台正在部署维护"

    repeated = client.post(
        path, headers=admin_headers, json={"reason": "部署发布"}
    )
    assert repeated.status_code == 200
    assert repeated.json()["maintenance"]["requested"] is True

    auto_enable = client.put(
        "/api/admin/auto-match",
        headers=admin_headers,
        json={"enabled": True},
    )
    assert auto_enable.status_code == 409
    assert auto_enable.json()["detail"]["code"] == "maintenance_active"
    auto_disable = client.put(
        "/api/admin/auto-match",
        headers=admin_headers,
        json={"enabled": False},
    )
    assert auto_disable.status_code == 200
    assert auto_disable.json()["dispatcher"]["auto_enabled"] is False

    rejected = client.post(
        "/api/matches/challenge",
        headers=owner_headers,
        json={
            "my_bot_id": own_bots[0]["bot_id"],
            "opponent_bot_id": opponent_bots[0]["bot_id"],
            "game_id": "holdem",
        },
    )
    assert rejected.status_code == 503
    assert rejected.json()["detail"]["code"] == "deployment_maintenance"

    not_ready = client.delete(path, headers=admin_headers)
    assert not_ready.status_code == 409
    assert not_ready.json()["detail"]["code"] == "maintenance_not_ready"
    store.update_match(
        claimed["current_match_id"], status="aborted", reason="test_complete"
    )
    assert store.executions.maintenance_status()["ready"] is False
    store.executions.mark_cleanup_confirmed(active["public_id"], 1)
    assert store.executions.finalize_ready() == 1
    ready = client.get(path, headers=admin_headers)
    assert ready.status_code == 200
    assert ready.json()["maintenance"]["ready"] is True
    assert ready.json()["queued"][0]["public_id"] == waiting["public_id"]

    ended = client.delete(path, headers=admin_headers)
    assert ended.status_code == 200, ended.text
    assert ended.json()["maintenance"]["requested"] is False
    assert ended.json()["dispatcher"]["accepting"] is True
    assert ended.json()["dispatcher"]["auto_enabled"] is False
    idempotent_end = client.delete(path, headers=admin_headers)
    assert idempotent_end.status_code == 200
    assert idempotent_end.json()["dispatcher"]["auto_enabled"] is False

    # A fresh drain cannot be requested while runtime recovery is unresolved.
    # The operator must use the orthogonal resume endpoint first.
    store.executions.pause("manual:test pause", bounded_retry=False)
    paused_begin = client.post(
        path, headers=admin_headers, json={"reason": "错误时机"}
    )
    assert paused_begin.status_code == 409
    assert paused_begin.json()["detail"]["code"] == "maintenance_state_conflict"

    actions = [(call["action"], call.get("result")) for call in audit_calls]
    assert actions.count(("admin_execution_maintenance_begin", "ok")) == 2
    assert ("admin_execution_maintenance_begin", "conflict") in actions
    assert ("admin_execution_maintenance_end", "conflict") in actions
    assert actions.count(("admin_execution_maintenance_end", "ok")) == 2
    assert ("admin_auto_match_toggle", "deny") in actions
    assert any(
        call["action"] == "admin_execution_maintenance_begin"
        and call.get("result") == "ok"
        and "requested=0->1" in str(call.get("detail"))
        and "accepting=1->0" in str(call.get("detail"))
        and "auto_enabled=1->0" in str(call.get("detail"))
        for call in audit_calls
    )
    assert any(
        call["action"] == "admin_execution_maintenance_end"
        and call.get("result") == "ok"
        and "requested=1->0" in str(call.get("detail"))
        and "accepting=0->1" in str(call.get("detail"))
        and "auto_enabled=0->0" in str(call.get("detail"))
        for call in audit_calls
    )


def test_deployment_drain_holds_contest_pairing_before_adjudication_and_bind(
    execution_api,
):
    app, client, store = (
        execution_api.app,
        execution_api.client,
        execution_api.store,
    )
    organizer = _api_user(store, "drain_contest_org", role="organizer")
    opponent = _api_user(store, "drain_contest_opponent")
    admin = _api_user(store, "drain_contest_admin", role="admin")
    first = _owned_bot(store, organizer, "drain_contest_first")
    second = _owned_bot(store, opponent, "drain_contest_second")
    contest = store.create_contest(
        "Deployment held contest",
        organizer["id"],
        status="open",
        game_id="holdem",
        stages_json=json.dumps(
            [
                {
                    "key": "rr",
                    "type": "round_robin",
                    "scoring": "poker_3_1_0",
                    "rest_after_minutes": 0,
                }
            ]
        ),
        template_id="holdem_swiss_ko",
    )
    store.add_contest_entry(
        contest["id"], organizer["id"], first["bot_id"]
    )
    store.add_contest_entry(
        contest["id"], opponent["id"], second["bot_id"]
    )
    published = asyncio.run(
        app.state.contest_manager.publish(contest["id"])
    )
    assert published["status"] == "published"
    pairing_before = store.list_contest_pairings(contest["id"])[0]
    assert pairing_before["status"] == "pending"
    assert pairing_before["match_id"] is None
    contest_before = store.get_contest(contest["id"])

    with store._tx() as conn:
        conn.execute(
            "UPDATE bots SET is_active=0 WHERE id=?", (first["bot_id"],)
        )

    admin_headers = _auth_headers(app, admin)
    organizer_headers = _auth_headers(app, organizer)
    maintenance_path = "/api/admin/execution-queue/maintenance"
    begun = client.post(
        maintenance_path,
        headers=admin_headers,
        json={"reason": "赛事派发边界"},
    )
    assert begun.status_code == 200
    assert begun.json()["maintenance"]["ready"] is True

    # Scheduler/reconcile route: an unavailable Bot would normally produce a
    # technical result, but drain gates before adjudication or lifecycle writes.
    asyncio.run(
        app.state.contest_manager._dispatch_pending(contest["id"], 0)
    )
    assert store.list_contest_pairings(contest["id"])[0] == pairing_before
    assert store.get_contest(contest["id"]) == contest_before
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE contest_id=?",
        (contest["id"],),
    ).fetchone()[0] == 0

    # Organizer route gets a retryable, structured 503 and the published
    # schedule remains byte-for-byte unchanged.
    rejected = client.post(
        f"/api/contests/{contest['id']}/start",
        headers=organizer_headers,
    )
    assert rejected.status_code == 503
    assert rejected.json()["detail"]["code"] == "deployment_maintenance"
    assert rejected.headers["retry-after"] == "30"
    assert store.list_contest_pairings(contest["id"])[0] == pairing_before
    assert store.get_contest(contest["id"]) == contest_before

    with store._tx() as conn:
        conn.execute(
            "UPDATE bots SET is_active=1 WHERE id=?", (first["bot_id"],)
        )
    ended = client.delete(maintenance_path, headers=admin_headers)
    assert ended.status_code == 200
    assert ended.json()["dispatcher"]["auto_enabled"] is False
    resumed = client.post(
        f"/api/contests/{contest['id']}/start",
        headers=organizer_headers,
    )
    assert resumed.status_code == 200, resumed.text
    jobs = store._conn.execute(
        "SELECT * FROM execution_jobs WHERE contest_id=?",
        (contest["id"],),
    ).fetchall()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"
    still_pending = store.list_contest_pairings(contest["id"])[0]
    assert still_pending["status"] == "pending"
    assert still_pending["match_id"] is None
    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["source"] == EXECUTION_SOURCE_CONTEST
    bound = store.list_contest_pairings(contest["id"])[0]
    assert bound["status"] == "running"
    assert bound["match_id"] == claimed["current_match_id"]


def test_deployment_drain_blocks_contest_advance_without_side_effects(
    execution_api,
):
    app, client, store = (
        execution_api.app,
        execution_api.client,
        execution_api.store,
    )
    organizer = _api_user(store, "drain_advance_org", role="organizer")
    opponent = _api_user(store, "drain_advance_opponent")
    admin = _api_user(store, "drain_advance_admin", role="admin")
    first = _owned_bot(store, organizer, "drain_advance_first")
    second = _owned_bot(store, opponent, "drain_advance_second")
    contest = store.create_contest(
        "Deployment held advance",
        organizer["id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps(
            [{"key": "rr", "type": "round_robin", "scoring": "poker_3_1_0"}]
        ),
        current_stage_idx=0,
    )
    store.add_contest_entry(contest["id"], organizer["id"], first["bot_id"])
    store.add_contest_entry(contest["id"], opponent["id"], second["bot_id"])
    match_id = "deployment-advance-complete"
    store.create_match(
        match_id,
        first["bot_id"],
        second["bot_id"],
        owner_id=organizer["id"],
        contest_id=contest["id"],
        match_type=TYPE_CONTEST,
        game_id="holdem",
    )
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 0.01},
    )
    store.add_contest_pairing(
        contest["id"],
        first["bot_id"],
        second["bot_id"],
        status="completed",
        match_id=match_id,
        stage_idx=0,
        round_num=1,
    )
    contest_before = store.get_contest(contest["id"])
    entries_before = store.list_contest_entries(contest["id"])
    pairings_before = store.list_contest_pairings(contest["id"])

    begun = client.post(
        "/api/admin/execution-queue/maintenance",
        headers=_auth_headers(app, admin),
        json={"reason": "推进边界"},
    )
    assert begun.status_code == 200
    rejected = client.post(
        f"/api/contests/{contest['id']}/advance",
        headers=_auth_headers(app, organizer),
    )
    assert rejected.status_code == 503
    assert rejected.json()["detail"]["code"] == "deployment_maintenance"
    assert rejected.headers["retry-after"] == "30"
    assert store.get_contest(contest["id"]) == contest_before
    assert store.list_contest_entries(contest["id"]) == entries_before
    assert store.list_contest_pairings(contest["id"]) == pairings_before


def test_runtime_pause_recovery_reconciles_contest_before_returning(queue_store):
    store = queue_store
    organizer = store.create_user(
        "recovery-contest-org",
        "recovery-contest-org@example.test",
        "hash",
        role="organizer",
    )
    first = _bot(store, "recovery-contest-a")
    second = _bot(store, "recovery-contest-b")
    contest = store.create_contest(
        "Recovery contest",
        organizer["id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps(
            [{"key": "rr", "type": "round_robin", "scoring": "poker_3_1_0"}]
        ),
        current_stage_idx=0,
    )
    store.add_contest_entry(contest["id"], first["user_id"], first["bot_id"])
    store.add_contest_entry(contest["id"], second["user_id"], second["bot_id"])
    pairing = store.add_contest_pairing(
        contest["id"],
        first["bot_id"],
        second["bot_id"],
        bot_a_version_id=first["version_id"],
        bot_b_version_id=second["version_id"],
        status="pending",
        stage_idx=0,
        round_num=1,
        published_at="2026-08-14T00:00:00",
        scheduled_at="2026-08-14T00:00:00",
    )

    class Runtime:
        supervisor = None

        async def cleanup_instance(self) -> None:
            return None

        async def ensure_runtime_ready(self) -> None:
            return None

    class Orch:
        runner = SimpleNamespace(runner=Runtime())

        async def quiesce_execution_tasks(self) -> None:
            return None

        async def recover_unsettled_match_ratings(self) -> int:
            return 0

        def start_execution_job(self, _job: dict) -> None:
            raise AssertionError("recovery reconciliation must not claim")

        async def challenge(
            self,
            bot_a_id: int,
            bot_b_id: int,
            **kwargs,
        ) -> str:
            job = store.executions.enqueue(
                source=EXECUTION_SOURCE_CONTEST,
                owner_user_id=kwargs["owner_user_id"],
                game_id=kwargs["game_id"],
                match_type=TYPE_CONTEST,
                bot_a_id=bot_a_id,
                bot_b_id=bot_b_id,
                bot_a_version_id=kwargs.get("bot_a_version_id"),
                bot_b_version_id=kwargs.get("bot_b_version_id"),
                contest_id=kwargs["contest_id"],
                contest_pairing_id=kwargs["contest_pairing_id"],
            )
            return str(job["public_id"])

        def active_execution_task_count(self) -> int:
            return 0

    orch = Orch()
    manager = ContestManager(
        store, orch, execution_admission_required=lambda: True
    )
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=False,
        contest_reconciler=manager.reconcile_running_contests,
        uploads_in_flight=lambda: 0,
    )
    store.executions.pause("runtime recovery", bounded_retry=True)
    recovered = asyncio.run(dispatcher.admin_resume())
    assert recovered is True
    control = store.executions.control()
    assert control["dispatcher_state"] == "running"
    assert control["accepting"] == 1
    assert control["deployment_drain_requested"] == 0
    jobs = store._conn.execute(
        "SELECT * FROM execution_jobs WHERE contest_pairing_id=?",
        (pairing["id"],),
    ).fetchall()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "queued"
    assert store.list_contest_pairings(contest["id"])[0]["match_id"] is None


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


def test_crash_recovery_preserves_auto_yield_reason_exactly_once(queue_store):
    store = queue_store
    bots = [_bot(store, f"yield-recovery-{index}") for index in range(4)]
    automatic = _enqueue_pair(
        store,
        (bots[0], bots[1]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    claimed = _claim_auto(store)
    assert claimed and claimed["public_id"] == automatic["public_id"]
    match_id = claimed["current_match_id"]
    store.upsert_replay(match_id, json.dumps([{"type": "match_start"}]))

    _enqueue_pair(store, (bots[2], bots[3]))
    yielding = store.executions.get(automatic["public_id"])
    assert yielding["cancel_requested"] == 1
    assert yielding["terminal_reason"] == AUTO_YIELD_FOREGROUND_REASON

    first = store.executions.recover_after_namespace_cleanup()
    second = store.executions.recover_after_namespace_cleanup()
    assert first["settling"] == 1
    assert second["settling"] == 1
    assert store.get_match(match_id)["reason"] == AUTO_YIELD_FOREGROUND_REASON
    terminal_event = {"type": "error", "reason": AUTO_YIELD_FOREGROUND_REASON}
    events = json.loads(store.get_replay(match_id)["events_json"])
    assert events.count(terminal_event) == 1

    assert store.executions.finalize_ready() == 1
    assert store.executions.finalize_ready() == 0
    assert store.executions.get(automatic["public_id"])["status"] == "cancelled"
    assert store._conn.execute(
        "SELECT COUNT(*) FROM match_rating_settlements WHERE match_id=?",
        (match_id,),
    ).fetchone()[0] == 0


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
        claimed = _claim_auto(store)
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
        retry_deadline = str(row["next_attempt_at"])
        fraction = retry_deadline.rpartition(".")[2]
        assert len(fraction) == 6 and fraction.isdigit()
        retry_at = datetime.fromisoformat(retry_deadline)
        observed_delay = (retry_at - before).total_seconds()
        assert expected_delay - 1 <= observed_delay <= expected_delay + 1

        store.executions.resume()
        assert _claim_auto(store) is None
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
        stages_json=_valid_contest_stages_json(),
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
        stages_json=_valid_contest_stages_json(),
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
    claimed = _claim_auto(store)
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


def test_execution_environment_snapshots_are_source_owned(queue_store):
    store = queue_store
    pair = (_bot(store, "environment-a"), _bot(store, "environment-b"))

    manual = _enqueue_pair(store, pair)
    assert (
        manual["bot_a_environment"],
        manual["bot_b_environment"],
        manual["sandbox_units"],
        manual["host_cpu_millis"],
        manual["host_memory_mb"],
        manual["profile_version"],
    ) == ("platform_low", "platform_low", 2, 2000, 1024, 1)

    automatic = _enqueue_pair(store, pair, source=EXECUTION_SOURCE_AUTO)
    assert (
        automatic["bot_a_environment"],
        automatic["bot_b_environment"],
        automatic["host_cpu_millis"],
        automatic["host_memory_mb"],
    ) == ("platform_low", "platform_low", 2000, 1024)

    _, _, human = _enqueue_human(store, "environment-human")
    assert (
        human["bot_a_environment"],
        human["bot_b_environment"],
        human["sandbox_units"],
        human["host_cpu_millis"],
        human["host_memory_mb"],
    ) == ("platform_low", "human", 1, 1000, 512)

    contest = store.create_contest(
        "High profile", pair[0]["user_id"], status="running", game_id="holdem"
    )
    pairing = store.add_pairing(
        contest["id"],
        pair[0]["bot_id"],
        pair[1]["bot_id"],
        bot_a_version_id=pair[0]["version_id"],
        bot_b_version_id=pair[1]["version_id"],
    )
    official = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=pair[0]["user_id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=pair[0]["bot_id"],
        bot_b_id=pair[1]["bot_id"],
        bot_a_version_id=pair[0]["version_id"],
        bot_b_version_id=pair[1]["version_id"],
        # Callers cannot downgrade or replace the official profile.
        bot_a_environment="remote_local",
        bot_b_environment="platform_low",
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
    )
    assert (
        official["bot_a_environment"],
        official["bot_b_environment"],
        official["sandbox_units"],
        official["host_cpu_millis"],
        official["host_memory_mb"],
    ) == ("platform_high", "platform_high", 2, 4000, 4096)


def test_8vcpu_budget_admits_four_frozen_low_profile_matches(queue_store):
    store = queue_store
    pairs = [
        (
            _bot(store, f"low-capacity-{index}-a"),
            _bot(store, f"low-capacity-{index}-b"),
        )
        for index in range(5)
    ]
    _verify_projection(store)
    jobs = [_enqueue_pair(store, pair) for pair in pairs]
    claim_kwargs = {
        "max_match_slots": 6,
        "max_sandbox_units": 12,
        "max_host_cpu_millis": 8000,
        "max_host_memory_mb": 16 * 1024,
        "aging_seconds": 60,
        "user_active_limit": 1,
        "contest_share_slots": 1,
    }

    claimed = [store.executions.claim_next(**claim_kwargs) for _ in range(4)]
    assert [row["public_id"] for row in claimed] == [
        row["public_id"] for row in jobs[:4]
    ]
    assert store.executions.claim_next(**claim_kwargs) is None
    assert store.executions.get(jobs[4]["public_id"])["status"] == "queued"
    capacity = store.executions.snapshot(
        max_match_slots=6,
        max_sandbox_units=12,
        max_host_cpu_millis=8000,
        max_host_memory_mb=16 * 1024,
        aging_seconds=60,
    )["capacity"]
    assert (capacity["max_match_slots"], capacity["max_sandbox_units"]) == (
        6,
        12,
    )
    assert (capacity["used_match_slots"], capacity["used_sandbox_units"]) == (
        4,
        8,
    )
    assert capacity["used_host_cpu_millis"] == 8000
    assert capacity["used_host_memory_mb"] == 4096


def test_8vcpu_budget_admits_six_human_jobs_and_direct_claim_clamps(queue_store):
    store = queue_store
    jobs = [_enqueue_human(store, f"human-capacity-{index}")[2] for index in range(7)]
    claim_kwargs = {
        # Deliberately bypass the runtime/dispatcher entrypoints: the durable
        # repository remains the final defense against a caller asking for 99.
        "max_match_slots": 999,
        "max_sandbox_units": 999,
        "max_host_cpu_millis": 8000,
        "max_host_memory_mb": 16 * 1024,
        "aging_seconds": 60,
        "user_active_limit": 1,
        "contest_share_slots": 1,
    }

    claimed = [store.executions.claim_next(**claim_kwargs) for _ in range(6)]
    assert [row["public_id"] for row in claimed] == [
        row["public_id"] for row in jobs[:6]
    ]
    assert store.executions.claim_next(**claim_kwargs) is None
    assert store.executions.get(jobs[6]["public_id"])["status"] == "queued"
    capacity = store.executions.snapshot(
        max_match_slots=999,
        max_sandbox_units=999,
        max_host_cpu_millis=8000,
        max_host_memory_mb=16 * 1024,
        aging_seconds=60,
    )["capacity"]
    assert (capacity["max_match_slots"], capacity["max_sandbox_units"]) == (
        6,
        12,
    )
    assert (capacity["used_match_slots"], capacity["used_sandbox_units"]) == (
        6,
        6,
    )
    assert capacity["used_host_cpu_millis"] == 6000
    assert capacity["used_host_memory_mb"] == 3072


def test_8vcpu_budget_admits_six_remote_local_jobs(queue_store):
    store = queue_store
    jobs: list[dict] = []
    for index in range(7):
        owner = _api_user(store, f"remote_capacity_{index}_owner")
        opponent_owner = _api_user(store, f"remote_capacity_{index}_opponent")
        remote_bot = _owned_bot(store, owner, f"remote_capacity_{index}")
        docker_bot = _owned_bot(
            store, opponent_owner, f"remote_capacity_{index}_docker"
        )
        agent = _local_agent(store, remote_bot, f"remote-capacity-{index}")
        jobs.append(
            store.executions.enqueue(
                source=EXECUTION_SOURCE_MANUAL,
                owner_user_id=int(owner["id"]),
                game_id="holdem",
                match_type=TYPE_CHALLENGE,
                bot_a_id=remote_bot["bot_id"],
                bot_b_id=docker_bot["bot_id"],
                bot_a_version_id=remote_bot["version_id"],
                bot_b_version_id=docker_bot["version_id"],
                bot_a_environment="remote_local",
                bot_a_local_agent_id=int(agent["id"]),
            )
        )
    store.executions.set_local_agent_available(lambda _agent_id: True)
    claim_kwargs = {
        "max_match_slots": 6,
        "max_sandbox_units": 12,
        "max_host_cpu_millis": 8000,
        "max_host_memory_mb": 16 * 1024,
        "aging_seconds": 60,
        "user_active_limit": 1,
        "contest_share_slots": 1,
    }

    claimed = [store.executions.claim_next(**claim_kwargs) for _ in range(6)]
    assert [row["public_id"] for row in claimed] == [
        row["public_id"] for row in jobs[:6]
    ]
    assert store.executions.claim_next(**claim_kwargs) is None
    assert store.executions.get(jobs[6]["public_id"])["status"] == "queued"
    capacity = store.executions.snapshot(
        max_match_slots=6,
        max_sandbox_units=12,
        max_host_cpu_millis=8000,
        max_host_memory_mb=16 * 1024,
        aging_seconds=60,
    )["capacity"]
    assert (capacity["used_match_slots"], capacity["used_sandbox_units"]) == (
        6,
        6,
    )
    assert capacity["used_host_cpu_millis"] == 6000
    assert capacity["used_host_memory_mb"] == 3072


def test_8vcpu_16gib_budget_admits_two_official_matches_and_holds_third(
    queue_store,
    monkeypatch,
):
    store = queue_store
    bots = [_bot(store, f"dual-capacity-{index}") for index in range(6)]
    contest = store.create_contest(
        "Dual capacity",
        bots[0]["user_id"],
        status="running",
        game_id="holdem",
        stages_json=_valid_contest_stages_json(),
    )
    pairings = [
        store.add_pairing(
            contest["id"],
            bots[offset]["bot_id"],
            bots[offset + 1]["bot_id"],
            bot_a_version_id=bots[offset]["version_id"],
            bot_b_version_id=bots[offset + 1]["version_id"],
        )
        for offset in (0, 2, 4)
    ]
    _verify_projection(store)
    jobs = [
        store.executions.enqueue(
            source=EXECUTION_SOURCE_CONTEST,
            owner_user_id=bots[0]["user_id"],
            game_id="holdem",
            match_type=TYPE_CONTEST,
            bot_a_id=bots[offset]["bot_id"],
            bot_b_id=bots[offset + 1]["bot_id"],
            bot_a_version_id=bots[offset]["version_id"],
            bot_b_version_id=bots[offset + 1]["version_id"],
            contest_id=contest["id"],
            contest_pairing_id=pairings[index]["id"],
        )
        for index, offset in enumerate((0, 2, 4))
    ]

    monkeypatch.setattr(
        "bzplat.backend.matches.execution_queue.effective_host_resource_budget",
        lambda: HostResourceBudget(cpu_millis=8000, memory_mb=16 * 1024),
    )

    class RecordingOrchestrator:
        def __init__(self) -> None:
            self.started: list[dict] = []

        def start_execution_job(self, job: dict) -> None:
            self.started.append(job)

    orch = RecordingOrchestrator()
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=6,
        max_sandbox_units=12,
        max_host_cpu_millis=8000,
        max_host_memory_mb=16 * 1024,
        auto_capability_enabled=False,
    )
    result = asyncio.run(dispatcher.run_once())
    assert result["claimed"] == 2
    assert {job["public_id"] for job in orch.started} == {
        jobs[0]["public_id"],
        jobs[1]["public_id"],
    }

    snapshot = store.executions.snapshot(
        max_match_slots=6,
        max_sandbox_units=12,
        max_host_cpu_millis=8000,
        max_host_memory_mb=16 * 1024,
        aging_seconds=60,
    )
    assert snapshot["capacity"]["max_match_slots"] == 6
    assert snapshot["capacity"]["max_sandbox_units"] == 12
    assert snapshot["capacity"]["used_match_slots"] == 2
    assert snapshot["capacity"]["used_sandbox_units"] == 4
    assert snapshot["capacity"]["used_host_cpu_millis"] == 8000
    assert snapshot["capacity"]["used_host_memory_mb"] == 8192
    assert store.executions.get(jobs[2]["public_id"])["status"] == "queued"


@pytest.mark.parametrize(
    ("host_cpu_millis", "host_memory_mb"),
    [(7999, 16 * 1024), (8000, 8191)],
)
def test_dual_official_matches_require_full_aggregate_host_budget(
    queue_store,
    host_cpu_millis,
    host_memory_mb,
):
    store = queue_store
    bots = [_bot(store, f"dual-host-gate-{index}") for index in range(4)]
    contest = store.create_contest(
        "Dual host gate",
        bots[0]["user_id"],
        status="running",
        game_id="holdem",
        stages_json=_valid_contest_stages_json(),
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
    jobs = [
        store.executions.enqueue(
            source=EXECUTION_SOURCE_CONTEST,
            owner_user_id=bots[0]["user_id"],
            game_id="holdem",
            match_type=TYPE_CONTEST,
            bot_a_id=bots[offset]["bot_id"],
            bot_b_id=bots[offset + 1]["bot_id"],
            bot_a_version_id=bots[offset]["version_id"],
            bot_b_version_id=bots[offset + 1]["version_id"],
            contest_id=contest["id"],
            contest_pairing_id=pairings[index]["id"],
        )
        for index, offset in enumerate((0, 2))
    ]
    claim_kwargs = {
        "max_match_slots": 2,
        "max_sandbox_units": 4,
        "max_host_cpu_millis": host_cpu_millis,
        "max_host_memory_mb": host_memory_mb,
        "aging_seconds": 60,
        "user_active_limit": 1,
        "contest_share_slots": 1,
    }

    first = store.executions.claim_next(**claim_kwargs)
    assert first and first["public_id"] == jobs[0]["public_id"]
    assert store.executions.claim_next(**claim_kwargs) is None
    assert store.executions.get(jobs[1]["public_id"])["status"] == "queued"


def test_untracked_running_match_charges_max_profile_before_second_slot_claim(
    queue_store,
):
    store = queue_store
    bots = [_bot(store, f"untracked-host-gate-{index}") for index in range(4)]
    store.create_match(
        "untracked-high-profile",
        bots[0]["bot_id"],
        bots[1]["bot_id"],
        owner_id=bots[0]["user_id"],
        match_type=TYPE_CONTEST,
        game_id="holdem",
    )
    store.update_match("untracked-high-profile", status="running")

    contest = store.create_contest(
        "Untracked host gate",
        bots[2]["user_id"],
        status="running",
        game_id="holdem",
        stages_json=_valid_contest_stages_json(),
    )
    pairing = store.add_pairing(
        contest["id"],
        bots[2]["bot_id"],
        bots[3]["bot_id"],
        bot_a_version_id=bots[2]["version_id"],
        bot_b_version_id=bots[3]["version_id"],
    )
    _verify_projection(store)
    queued = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=bots[2]["user_id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=bots[2]["bot_id"],
        bot_b_id=bots[3]["bot_id"],
        bot_a_version_id=bots[2]["version_id"],
        bot_b_version_id=bots[3]["version_id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
    )
    claim_kwargs = {
        "max_match_slots": 2,
        "max_sandbox_units": 4,
        "max_host_cpu_millis": 6000,
        "max_host_memory_mb": 5120,
        "aging_seconds": 60,
        "user_active_limit": 1,
        "contest_share_slots": 1,
    }

    # Slot and sandbox limits alone would admit the second contest.  The
    # orphan is conservatively charged as a current high-profile two-Bot
    # match (4000m / 4096 MiB), so the aggregate host budget blocks it.
    assert store.executions.claim_next(**claim_kwargs) is None
    snapshot = store.executions.snapshot(
        max_match_slots=2,
        max_sandbox_units=4,
        max_host_cpu_millis=6000,
        max_host_memory_mb=5120,
        aging_seconds=60,
    )
    capacity = snapshot["capacity"]
    assert capacity["untracked_running_matches"] == 1
    assert capacity["used_match_slots"] == 0
    assert capacity["occupied_match_slots"] == 1
    assert capacity["used_sandbox_units"] == 2
    assert capacity["used_host_cpu_millis"] == 4000
    assert capacity["used_host_memory_mb"] == 4096
    assert store.executions.get(queued["public_id"])["status"] == "queued"

    store.update_match(
        "untracked-high-profile",
        status="aborted",
        reason="test cleanup",
    )
    claimed = store.executions.claim_next(**claim_kwargs)
    assert claimed and claimed["public_id"] == queued["public_id"]


@pytest.mark.parametrize(
    ("host_cpu_millis", "host_memory_mb"),
    [(3999, 4096), (4000, 4095)],
)
def test_official_profile_waits_on_undersized_host_without_downgrade(
    queue_store,
    host_cpu_millis,
    host_memory_mb,
):
    store = queue_store
    pair = (_bot(store, "host-gate-a"), _bot(store, "host-gate-b"))
    _verify_projection(store)
    contest = store.create_contest(
        "Host gate",
        pair[0]["user_id"],
        status="running",
        game_id="holdem",
        stages_json=_valid_contest_stages_json(),
    )
    pairing = store.add_pairing(
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

    blocked = store.executions.claim_next(
        max_match_slots=1,
        max_sandbox_units=2,
        max_host_cpu_millis=host_cpu_millis,
        max_host_memory_mb=host_memory_mb,
        aging_seconds=60,
        user_active_limit=1,
        contest_share_slots=1,
    )
    assert blocked is None
    persisted = store.executions.get(job["public_id"])
    assert persisted["status"] == "queued"
    assert (
        persisted["bot_a_environment"],
        persisted["bot_b_environment"],
        persisted["host_cpu_millis"],
        persisted["host_memory_mb"],
    ) == ("platform_high", "platform_high", 4000, 4096)

    dispatcher = ExecutionDispatcher(
        SimpleNamespace(),
        store,
        max_match_slots=1,
        max_sandbox_units=2,
        max_host_cpu_millis=host_cpu_millis,
        max_host_memory_mb=host_memory_mb,
    )
    request = dispatcher.public_request(job["public_id"])
    assert request["request"]["blocked_code"] == "host_resources_insufficient"
    assert "不会降档" in request["blocked_reason"]
    queued = dispatcher.public_snapshot()["queued"]
    assert queued[0]["blocked_reason"] == request["blocked_reason"]

    claimed = store.executions.claim_next(
        max_match_slots=1,
        max_sandbox_units=2,
        max_host_cpu_millis=4000,
        max_host_memory_mb=4096,
        aging_seconds=60,
        user_active_limit=1,
        contest_share_slots=1,
    )
    assert claimed is not None
    assert claimed["public_id"] == job["public_id"]
    assert (
        claimed["bot_a_environment"],
        claimed["bot_b_environment"],
    ) == ("platform_high", "platform_high")
    match = store.get_match(str(claimed["current_match_id"]))
    assert match["match_config"]["_execution_profile_version"] == 1


def test_claim_rejects_unknown_or_tampered_resource_profile_snapshot(queue_store):
    store = queue_store
    pair = (_bot(store, "profile-guard-a"), _bot(store, "profile-guard-b"))

    unknown = _enqueue_pair(store, pair)
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET profile_version=99 WHERE public_id=?",
            (unknown["public_id"],),
        )
    with pytest.raises(ExecutionInvariantError, match="profile snapshot is unknown"):
        _claim(store, slots=1, units=2)
    assert store.list_matches(limit=20) == []

    with store._tx() as conn:
        # The schema CHECK already rejects ordinary writes. Simulate a damaged
        # legacy database to prove claim still refuses a mismatched snapshot.
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            "UPDATE execution_jobs SET profile_version=1,host_memory_mb=513 "
            "WHERE public_id=?",
            (unknown["public_id"],),
        )
        conn.execute("PRAGMA ignore_check_constraints=OFF")
    with pytest.raises(ExecutionInvariantError, match="profile snapshot mismatch"):
        _claim(store, slots=1, units=2)
    assert store.list_matches(limit=20) == []


def test_remote_environment_freezes_agent_not_upload_version_and_is_unrated(
    queue_store,
):
    store = queue_store
    owner = _api_user(store, "remote-owner")
    opponent = _api_user(store, "remote-opponent")
    local_bot = _owned_bot(store, owner, "remote-owner")
    docker_bot = _owned_bot(store, opponent, "remote-opponent")
    agent = _local_agent(store, local_bot, "remote-owner")

    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner["id"],
        game_id="holdem",
        match_type=TYPE_CHALLENGE,
        bot_a_id=local_bot["bot_id"],
        bot_b_id=docker_bot["bot_id"],
        bot_a_version_id=local_bot["version_id"],
        bot_b_version_id=docker_bot["version_id"],
        bot_a_environment="remote_local",
        bot_b_environment="platform_low",
        bot_a_local_agent_id=agent["id"],
    )

    assert job["bot_a_version_id"] is None
    assert job["bot_b_version_id"] == docker_bot["version_id"]
    assert job["bot_a_local_agent_id"] == agent["id"]
    assert (job["sandbox_units"], job["host_cpu_millis"], job["host_memory_mb"]) == (
        1,
        1000,
        512,
    )
    assert job["rated"] == 0
    assert job["rating_reason"] == "remote_local"


def test_remote_environment_rejects_untrusted_binding(queue_store):
    store = queue_store
    owner = _api_user(store, "remote-binding-owner")
    foreign = _api_user(store, "remote-binding-foreign")
    administrator = _api_user(store, "remote-binding-admin", role="admin")
    own_bot = _owned_bot(store, owner, "remote-binding-owner")
    foreign_bot = _owned_bot(store, foreign, "remote-binding-foreign")
    foreign_agent = _local_agent(store, foreign_bot, "remote-binding-foreign")

    common = dict(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner["id"],
        game_id="holdem",
        match_type=TYPE_CHALLENGE,
        bot_a_id=own_bot["bot_id"],
        bot_b_id=foreign_bot["bot_id"],
        bot_a_version_id=own_bot["version_id"],
        bot_b_version_id=foreign_bot["version_id"],
    )
    with pytest.raises(ValueError, match="\u7528\u6237\u3001Bot \u6216\u6e38\u620f\u4e0d\u5339\u914d"):
        store.executions.enqueue(
            **common,
            bot_b_environment="remote_local",
            bot_b_local_agent_id=foreign_agent["id"],
        )
    with pytest.raises(ValueError, match="\u7528\u6237\u3001Bot \u6216\u6e38\u620f\u4e0d\u5339\u914d"):
        store.executions.enqueue(
            **{**common, "owner_user_id": administrator["id"]},
            bot_b_environment="remote_local",
            bot_b_local_agent_id=foreign_agent["id"],
        )
    with pytest.raises(ValueError, match="\u4f4e\u914d Docker \u6216\u672c\u5730 Bot"):
        store.executions.enqueue(
            **common,
            bot_a_environment="platform_high",
        )
    with pytest.raises(ValueError, match="\u5fc5\u987b\u9009\u62e9\u8fde\u63a5"):
        store.executions.enqueue(
            **common,
            bot_a_environment="remote_local",
        )


@pytest.mark.parametrize("blocked_by", ["offline", "active_lease"])
def test_unavailable_remote_job_does_not_block_later_docker_job(
    queue_store, blocked_by
):
    store = queue_store
    remote_owner = _api_user(store, f"blocked-{blocked_by}-owner")
    opponent = _api_user(store, f"blocked-{blocked_by}-opponent")
    remote_bot = _owned_bot(store, remote_owner, f"blocked-{blocked_by}-remote")
    remote_opponent = _owned_bot(store, opponent, f"blocked-{blocked_by}-opponent")
    agent = _local_agent(store, remote_bot, f"blocked-{blocked_by}")
    remote = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=remote_owner["id"],
        game_id="holdem",
        match_type=TYPE_CHALLENGE,
        bot_a_id=remote_bot["bot_id"],
        bot_b_id=remote_opponent["bot_id"],
        bot_a_version_id=remote_bot["version_id"],
        bot_b_version_id=remote_opponent["version_id"],
        bot_a_environment="remote_local",
        bot_a_local_agent_id=agent["id"],
    )
    fallback_pair = (_bot(store, f"fallback-{blocked_by}-a"), _bot(store, f"fallback-{blocked_by}-b"))
    fallback = _enqueue_pair(store, fallback_pair)
    _verify_projection(store)
    store.executions.set_local_agent_available(
        (lambda _agent_id: False)
        if blocked_by == "offline"
        else (lambda _agent_id: True)
    )
    if blocked_by == "active_lease":
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO local_ai_leases("
                "agent_id,job_public_id,attempt_no,seat,status,acquired_at) "
                "VALUES(?, 'other-job', 1, 0, 'active', ?)",
                (agent["id"], "2026-08-13T00:00:00"),
            )

    dispatcher = ExecutionDispatcher(
        SimpleNamespace(),
        store,
        max_match_slots=1,
        max_sandbox_units=2,
    )
    request = dispatcher.public_request(remote["public_id"])
    assert request["request"]["blocked_code"] == "local_agent_unavailable"
    assert "等待所选本地 Bot 上线并空闲" in request["blocked_reason"]
    queued = dispatcher.public_snapshot()["queued"]
    projected = next(
        row for row in queued if row["public_id"] == remote["public_id"]
    )
    assert projected["blocked_reason"] == request["blocked_reason"]

    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == fallback["public_id"]
    assert store.executions.get(remote["public_id"])["status"] == "queued"


def test_remote_claim_skips_binary_check_and_lease_releases_with_cleanup(queue_store):
    store = queue_store
    owner = _api_user(store, "lease-owner")
    opponent = _api_user(store, "lease-opponent")
    remote_bot = _owned_bot(store, owner, "lease-remote")
    docker_bot = _owned_bot(store, opponent, "lease-docker")
    agent = _local_agent(store, remote_bot, "lease")
    Path(store.get_bot(remote_bot["bot_id"])["binary_path"]).unlink()
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner["id"],
        game_id="holdem",
        match_type=TYPE_CHALLENGE,
        bot_a_id=remote_bot["bot_id"],
        bot_b_id=docker_bot["bot_id"],
        bot_a_version_id=remote_bot["version_id"],
        bot_b_version_id=docker_bot["version_id"],
        bot_a_environment="remote_local",
        bot_a_local_agent_id=agent["id"],
    )
    store.executions.set_local_agent_available(lambda agent_id: agent_id == agent["id"])

    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == job["public_id"]
    match = store.get_match(str(claimed["current_match_id"]))
    assert match["match_config"] == {
        **match["match_config"],
        "_execution_profile_version": 1,
        "_bot_a_environment": "remote_local",
        "_bot_b_environment": "platform_low",
        "_bot_a_local_agent_id": agent["id"],
        "_bot_b_local_agent_id": None,
    }
    with store._tx() as conn:
        lease = conn.execute(
            "SELECT * FROM local_ai_leases WHERE job_public_id=?",
            (job["public_id"],),
        ).fetchone()
        assert lease["status"] == "active"
        assert lease["agent_id"] == agent["id"]

    store.executions.mark_cleanup_confirmed(job["public_id"], 1)
    with store._tx() as conn:
        lease = conn.execute(
            "SELECT * FROM local_ai_leases WHERE job_public_id=?",
            (job["public_id"],),
        ).fetchone()
        assert lease["status"] == "released"
        assert lease["released_at"]


def test_claimed_local_transport_identity_cannot_follow_reused_agent_row(
    queue_store, monkeypatch
):
    """A claimed attempt stays bound to the old public id after label reuse."""

    store = queue_store
    owner = _api_user(store, "frozen-local-owner")
    opponent = _api_user(store, "frozen-local-opponent")
    first_bot = _owned_bot(store, owner, "frozen-local-first")
    replacement_bot = _owned_bot(store, owner, "frozen-local-replacement")
    docker_bot = _owned_bot(store, opponent, "frozen-local-docker")
    agent = _local_agent(store, first_bot, "frozen-local")
    old_public_id = str(agent["public_id"])
    connected_agent = store.connect_local_ai_agent(
        int(agent["id"]), expected_public_id=old_public_id
    )
    assert connected_agent is not None
    old_generation = int(connected_agent["connection_generation"])
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner["id"],
        game_id="holdem",
        match_type=TYPE_CHALLENGE,
        bot_a_id=first_bot["bot_id"],
        bot_b_id=docker_bot["bot_id"],
        bot_a_version_id=first_bot["version_id"],
        bot_b_version_id=docker_bot["version_id"],
        bot_a_environment="remote_local",
        bot_a_local_agent_id=agent["id"],
    )
    store.executions.set_local_agent_available(
        lambda agent_id: int(agent_id) == int(agent["id"])
    )
    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == job["public_id"]
    match_id = str(claimed["current_match_id"])
    claimed_match = store.get_match(match_id)
    frozen_config = claimed_match["match_config"]
    assert frozen_config["_bot_a_local_agent_public_id"] == old_public_id
    assert frozen_config["_bot_a_local_agent_generation"] == old_generation

    # Revocation releases the durable lease; recreating the same owner label
    # deliberately reuses the integer row id but changes Bot/public identity.
    assert store.revoke_local_ai_agent(int(agent["id"]), int(owner["id"]))
    replacement = store.create_local_ai_agent(
        owner_id=int(owner["id"]),
        bot_id=int(replacement_bot["bot_id"]),
        label=str(agent["label"]),
        public_id="agent-frozen-local-replacement",
        token_hash=hashlib.sha256(b"replacement-token").hexdigest(),
        token_hint="replace",
    )
    assert int(replacement["id"]) == int(agent["id"])
    assert replacement["public_id"] != old_public_id
    assert int(replacement["connection_generation"]) > old_generation

    # A mutable row read at start is the exact vulnerability: prove the
    # orchestrator no longer performs one after the claim snapshot exists.
    monkeypatch.setattr(
        store,
        "get_local_ai_agent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("runner must not resolve a reusable agent row")
        ),
    )
    captured: dict[str, object] = {}

    async def exercise() -> None:
        hub = LocalAIHub()
        replacement_connection = await hub.register(str(replacement["public_id"]))
        # This mirrors the service-side revoke that closes/tombstones the old
        # transport before the same owner label is recreated.
        await hub.revoke(old_public_id)

        class _FrozenIdentityRunner:
            async def run_binaries(self, *_args, **kwargs):
                frozen_ids = kwargs.get("local_agent_ids")
                captured["local_agent_ids"] = frozen_ids
                # Exercise the real hub decision boundary. The old identity
                # must fail as a Bot technical fault; the connected replacement
                # must receive no turn at all.
                return await hub.request_decision(
                    frozen_ids[0],
                    request_id="frozen-local-turn",
                    match_id=match_id,
                    seat=0,
                    turn=1,
                    deadline_at=time.monotonic() + 1.0,
                    input="{}",
                )

        await MatchOrchestrator(
            store, runner=_FrozenIdentityRunner(), max_concurrent=1
        )._run_match(match_id)
        assert (
            await hub.next_turn(
                str(replacement["public_id"]),
                replacement_connection.connection_id,
                timeout=0.01,
            )
            is None
        )

    asyncio.run(exercise())

    assert captured["local_agent_ids"] == (old_public_id, None)
    assert replacement["public_id"] not in captured["local_agent_ids"]
    terminal = store.get_match(match_id)
    assert (terminal["status"], terminal["reason"], terminal["winner"]) == (
        "completed",
        "technical_loss",
        1,
    )
    public = sanitize_public_match(terminal)
    assert "match_config" not in public
    assert old_public_id not in json.dumps(public, ensure_ascii=False)
    assert replacement["public_id"] not in json.dumps(public, ensure_ascii=False)


def test_online_local_agent_claim_uses_only_the_live_memory_projection(
    queue_store, monkeypatch
):
    store = queue_store
    owner = _api_user(store, "live-claim-owner")
    opponent = _api_user(store, "live-claim-opponent")
    remote_bot = _owned_bot(store, owner, "live-claim-remote")
    docker_bot = _owned_bot(store, opponent, "live-claim-docker")
    agent = _local_agent(store, remote_bot, "live-claim")
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner["id"],
        game_id="holdem",
        match_type=TYPE_CHALLENGE,
        bot_a_id=remote_bot["bot_id"],
        bot_b_id=docker_bot["bot_id"],
        bot_a_version_id=remote_bot["version_id"],
        bot_b_version_id=docker_bot["version_id"],
        bot_a_environment="remote_local",
        bot_a_local_agent_id=agent["id"],
    )
    service = LocalAIService(store)

    async def exercise() -> None:
        connection, generation = await service.connect(agent)
        store.executions.set_local_agent_available(service.is_available_now)

        def forbidden_store_read(*_args, **_kwargs):
            raise AssertionError("claim availability callback re-entered Store")

        monkeypatch.setattr(store, "get_local_ai_agent", forbidden_store_read)
        started = time.monotonic()
        claimed = _claim(store, slots=1, units=2)
        assert time.monotonic() - started < 0.5
        assert claimed and claimed["public_id"] == job["public_id"]
        await service.disconnect(agent, connection.connection_id, generation)

    asyncio.run(exercise())


def test_revoked_remote_identity_is_interrupted_without_blocking(queue_store):
    store = queue_store
    owner = _api_user(store, "revoked-owner")
    opponent = _api_user(store, "revoked-opponent")
    remote_bot = _owned_bot(store, owner, "revoked-remote")
    docker_bot = _owned_bot(store, opponent, "revoked-opponent")
    agent = _local_agent(store, remote_bot, "revoked")
    remote = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner["id"],
        game_id="holdem",
        match_type=TYPE_CHALLENGE,
        bot_a_id=remote_bot["bot_id"],
        bot_b_id=docker_bot["bot_id"],
        bot_a_version_id=remote_bot["version_id"],
        bot_b_version_id=docker_bot["version_id"],
        bot_a_environment="remote_local",
        bot_a_local_agent_id=agent["id"],
    )
    assert store.revoke_local_ai_agent(agent["id"], owner["id"])
    fallback_pair = (_bot(store, "revoked-fallback-a"), _bot(store, "revoked-fallback-b"))
    fallback = _enqueue_pair(store, fallback_pair)
    _verify_projection(store)
    store.executions.set_local_agent_available(lambda _agent_id: True)

    claimed = _claim(store, slots=1, units=2)
    assert claimed and claimed["public_id"] == fallback["public_id"]
    stopped = store.executions.get(remote["public_id"])
    assert stopped["status"] == "interrupted"
    assert stopped["retryable"] == 1
    assert stopped["terminal_reason"] == "local_agent_unavailable"


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
        profile=PLATFORM_LOW_PROFILE,
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


def test_exact_session_cleanup_removes_only_its_launch_token(
    queue_store, tmp_path, monkeypatch
):
    supervisor = _journal_supervisor(queue_store, tmp_path)
    identity = DockerExecutionIdentity(
        "journal-test-instance", "traditional-turn", 2
    )
    slot = 7
    token = "traditional-turn-token"
    name = identity.container_name(slot)
    target_id = "target-container-id"
    sibling_id = "longrunning-sibling-id"
    present = True
    removed: list[list[str]] = []

    def list_ids(**filters):
        assert filters == {
            "job_public_id": identity.job_public_id,
            "attempt_no": identity.attempt_no,
            "launch_token": token,
        }
        return [target_id] if present else []

    monkeypatch.setattr(supervisor, "list_ids", list_ids)
    monkeypatch.setattr(
        supervisor,
        "list_name_ids",
        lambda exact_name: [target_id] if present and exact_name == name else [],
    )
    monkeypatch.setattr(
        supervisor,
        "inspect_existing_labels",
        lambda container_id: (
            dict(identity.labels(slot, launch_token=token))
            if container_id == target_id
            else pytest.fail("sibling container must not be inspected")
        ),
    )

    def remove_exact(ids):
        nonlocal present
        removed.append(list(ids))
        assert list(ids) == [target_id]
        assert sibling_id not in ids
        present = False

    monkeypatch.setattr(supervisor, "remove_names", remove_exact)

    asyncio.run(
        supervisor.cleanup_session(
            identity,
            slot=slot,
            name=name,
            launch_token=token,
        )
    )

    assert removed == [[target_id]]


def test_exact_session_cleanup_fails_closed_on_label_mismatch(
    queue_store, tmp_path, monkeypatch
):
    supervisor = _journal_supervisor(queue_store, tmp_path)
    identity = DockerExecutionIdentity(
        "journal-test-instance", "traditional-mismatch", 1
    )
    slot = 3
    token = "expected-launch-token"
    name = identity.container_name(slot)
    monkeypatch.setattr(
        supervisor,
        "list_ids",
        lambda **_filters: ["mismatched-container"],
    )
    monkeypatch.setattr(
        supervisor,
        "list_name_ids",
        lambda _name: ["mismatched-container"],
    )
    monkeypatch.setattr(
        supervisor,
        "inspect_existing_labels",
        lambda _container_id: dict(
            identity.labels(slot, launch_token="different-token")
        ),
    )
    monkeypatch.setattr(
        supervisor,
        "remove_names",
        lambda _ids: pytest.fail("mismatched labels must never be removed"),
    )

    with pytest.raises(DockerControlUncertain, match="label"):
        asyncio.run(
            supervisor.cleanup_session(
                identity,
                slot=slot,
                name=name,
                launch_token=token,
            )
        )


def test_scoped_stop_immediately_cleans_traditional_turn_container(tmp_path):
    cleanup_calls: list[tuple] = []
    recovery_reasons: list[str] = []

    class ExactSupervisor:
        async def cleanup_session(
            self, identity, *, slot, name, launch_token
        ) -> None:
            cleanup_calls.append((identity, slot, name, launch_token))

    supervisor = ExactSupervisor()
    scope = SimpleNamespace(
        identity=DockerExecutionIdentity("instance-a", "request-a", 4),
        mark_recovery_pending=recovery_reasons.append,
    )
    session = BotSession(
        session_id="traditional-turn-session",
        info=SimpleNamespace(),
        binary_path=tmp_path / "bot.elf",
        proc=SimpleNamespace(returncode=0),
        mode="docker",
        container_name=scope.identity.container_name(9),
        container_slot=9,
        launch_token="turn-launch-token",
        execution_scope=scope,
    )
    runner = object.__new__(BinaryRunner)
    runner._sessions = {session.session_id: session}
    runner.supervisor = supervisor
    runner._preflight_gate = None

    asyncio.run(runner.stop_session(session.session_id))

    assert cleanup_calls == [
        (
            scope.identity,
            9,
            scope.identity.container_name(9),
            "turn-launch-token",
        )
    ]
    assert recovery_reasons == []
    assert runner._sessions == {}


def test_scoped_stop_marks_recovery_pending_when_exact_cleanup_is_uncertain(
    tmp_path,
):
    recovery_reasons: list[str] = []

    class UncertainSupervisor:
        async def cleanup_session(self, *_args, **_kwargs) -> None:
            raise DockerControlUncertain("mock exact cleanup uncertainty")

    scope = SimpleNamespace(
        identity=DockerExecutionIdentity("instance-a", "request-b", 1),
        mark_recovery_pending=recovery_reasons.append,
    )
    session = BotSession(
        session_id="uncertain-traditional-turn",
        info=SimpleNamespace(),
        binary_path=tmp_path / "bot.elf",
        proc=SimpleNamespace(returncode=0),
        mode="docker",
        container_name=scope.identity.container_name(2),
        container_slot=2,
        launch_token="uncertain-turn-token",
        execution_scope=scope,
    )
    runner = object.__new__(BinaryRunner)
    runner._sessions = {session.session_id: session}
    runner.supervisor = UncertainSupervisor()
    runner._preflight_gate = None

    with pytest.raises(SandboxControlUncertain, match="uncertainty"):
        asyncio.run(runner.stop_session(session.session_id))

    assert recovery_reasons == ["mock exact cleanup uncertainty"]
    assert session.session_id in runner._sessions


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
        profile=PLATFORM_LOW_PROFILE,
    )

    assert name == identity.container_name(0)
    assert rpc_states == ["creating"]
    launch = queue_store.executions.docker_launch()
    assert launch["state"] == "created"
    assert launch["launch_token"] == token


def test_foreground_yield_committed_before_launch_blocks_docker_rpc(
    queue_store, tmp_path, monkeypatch
):
    import bzplat.backend.runtime.docker_supervisor as supervisor_mod

    store = queue_store
    bots = [_bot(store, f"launch-yield-{index}") for index in range(4)]
    automatic = _enqueue_pair(
        store,
        (bots[0], bots[1]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    claimed = _claim_auto(store)
    assert claimed and claimed["public_id"] == automatic["public_id"]
    foreground = _enqueue_pair(store, (bots[2], bots[3]))
    assert store.executions.get(foreground["public_id"])["status"] == "queued"
    with pytest.raises(ExecutionAttemptNotCurrent):
        store.executions.assert_active_attempt(
            automatic["public_id"], int(claimed["attempt_count"])
        )

    supervisor = _journal_supervisor(store, tmp_path)
    identity = DockerExecutionIdentity(
        "journal-test-instance",
        automatic["public_id"],
        int(claimed["attempt_count"]),
    )
    binary = tmp_path / "yield-before-create.elf"
    binary.write_bytes(b"test")
    monkeypatch.setattr(supervisor_mod, "host_boot_id", lambda: "boot-yield")
    docker_rpc_called = False

    def forbidden_rpc(*_args, **_kwargs):
        nonlocal docker_rpc_called
        docker_rpc_called = True
        raise AssertionError("cancelled auto attempt must not reach Docker RPC")

    monkeypatch.setattr(supervisor, "_run", forbidden_rpc)
    with pytest.raises(ExecutionAttemptNotCurrent):
        supervisor.create(
            identity=identity,
            slot=0,
            launch_token="yield-before-create-token",
            owner_kind="execution",
            binary_path=binary,
            image="test-image",
            profile=PLATFORM_LOW_PROFILE,
        )

    assert docker_rpc_called is False
    assert store.executions.docker_launch()["state"] == "idle"


def test_binary_runner_treats_launch_rejection_as_benign_task_cancellation(tmp_path):
    class RejectingSupervisor:
        instance = "qa-launch-rejected"

        @asynccontextmanager
        async def launch_guard(self):
            yield

        def create(self, **_kwargs):
            raise ExecutionAttemptNotCurrent(
                "execution attempt is no longer current"
            )

    binary = tmp_path / "launch-rejected.elf"
    binary.write_bytes(b"test")
    runner = object.__new__(BinaryRunner)
    runner.supervisor = RejectingSupervisor()
    runner._linux_image = "test-image"
    scope = ExecutionScope(
        instance="qa-launch-rejected",
        job_public_id="req-launch-rejected",
        attempt_no=1,
        supervisor=runner.supervisor,
        attempt_check=lambda: None,
    )
    session = BotSession(
        session_id="launch-rejected",
        info=SimpleNamespace(),
        binary_path=binary,
        execution_scope=scope,
    )

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner._start_docker(session))


def test_launch_intent_committed_before_foreground_is_then_yielded(queue_store):
    store = queue_store
    bots = [_bot(store, f"launch-first-{index}") for index in range(4)]
    automatic = _enqueue_pair(
        store,
        (bots[0], bots[1]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    claimed = _claim_auto(store)
    assert claimed and claimed["public_id"] == automatic["public_id"]
    store.executions.begin_docker_launch(
        launch_token="launch-first-token",
        instance_key="qa-launch-first",
        owner_kind="execution",
        job_public_id=automatic["public_id"],
        attempt_no=int(claimed["attempt_count"]),
        slot=0,
        container_name="bz-launch-first",
        host_boot_id="boot-launch-first",
    )

    foreground = _enqueue_pair(store, (bots[2], bots[3]))

    assert store.executions.get(foreground["public_id"])["status"] == "queued"
    yielded = store.executions.get(automatic["public_id"])
    assert yielded["cancel_requested"] == 1
    assert yielded["terminal_reason"] == AUTO_YIELD_FOREGROUND_REASON
    assert store.executions.docker_launch()["state"] == "creating"


def test_docker_launch_rejects_settling_attempt_and_preserves_nonidle_fence(
    queue_store,
):
    store = queue_store
    bots = [_bot(store, f"launch-fence-{index}") for index in range(4)]
    automatic = _enqueue_pair(
        store,
        (bots[0], bots[1]),
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
    )
    claimed = _claim_auto(store)
    attempt_no = int(claimed["attempt_count"])

    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status=?,settling_at=datetime('now'),"
            "cleanup_state='pending' WHERE public_id=?",
            (EXECUTION_SETTLING, automatic["public_id"]),
        )
    with pytest.raises(ExecutionAttemptNotCurrent):
        store.executions.begin_docker_launch(
            launch_token="settling-rejected-token",
            instance_key="qa-launch-fence",
            owner_kind="execution",
            job_public_id=automatic["public_id"],
            attempt_no=attempt_no,
            slot=0,
            container_name="bz-settling-rejected",
            host_boot_id="boot-launch-fence",
        )
    assert store.executions.docker_launch()["state"] == "idle"

    # Establish an unrelated physical create intent, then cancel this job.
    store.executions.begin_docker_launch(
        launch_token="preflight-fence-token",
        instance_key="qa-launch-fence",
        owner_kind="preflight",
        job_public_id="preflight-fence",
        attempt_no=1,
        slot=0,
        container_name="bz-preflight-fence",
        host_boot_id="boot-launch-fence",
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET cancel_requested=1 WHERE public_id=?",
            (automatic["public_id"],),
        )
    with pytest.raises(DockerLaunchInvariantError):
        store.executions.begin_docker_launch(
            launch_token="cancelled-behind-fence-token",
            instance_key="qa-launch-fence",
            owner_kind="execution",
            job_public_id=automatic["public_id"],
            attempt_no=attempt_no,
            slot=0,
            container_name="bz-cancelled-behind-fence",
            host_boot_id="boot-launch-fence",
        )
    launch = store.executions.docker_launch()
    assert launch["state"] == "creating"
    assert launch["launch_token"] == "preflight-fence-token"


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
            profile=PLATFORM_LOW_PROFILE,
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


def test_dispatcher_waits_for_live_launch_before_checking_journal(
    queue_store, tmp_path
):
    supervisor = _journal_supervisor(queue_store, tmp_path)
    runtime = SimpleNamespace(supervisor=supervisor)
    orch = SimpleNamespace(runner=SimpleNamespace(runner=runtime))
    dispatcher = ExecutionDispatcher(
        orch,
        queue_store,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=False,
    )

    async def exercise() -> dict:
        async with supervisor.launch_guard():
            _begin_test_launch(
                queue_store,
                token="live-launch-token",
                boot_id="boot-a",
                job_public_id="live-launch",
            )
            iteration = asyncio.create_task(dispatcher.run_once())
            await asyncio.sleep(0.02)
            assert iteration.done() is False
            assert queue_store.executions.control()["dispatcher_state"] == "running"
            queue_store.executions.mark_docker_launch_created(
                "live-launch-token"
            )
            queue_store.executions.clear_docker_launch_created(
                "live-launch-token"
            )
        return await asyncio.wait_for(iteration, timeout=1)

    result = asyncio.run(exercise())
    assert result["outcome"] == "ok"
    assert queue_store.executions.control()["dispatcher_state"] == "running"
    assert queue_store.executions.docker_launch()["state"] == "idle"


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


def test_runtime_reset_runs_only_after_dispatcher_singleton_is_owned(queue_store):
    class Runtime:
        supervisor = None

        async def cleanup_instance(self) -> None:
            return None

        async def ensure_runtime_ready(self) -> None:
            return None

    class Orch:
        runner = SimpleNamespace(runner=Runtime())

        async def recover_unsettled_match_ratings(self) -> int:
            return 0

    first_resets: list[str] = []
    second_resets: list[str] = []
    first = ExecutionDispatcher(
        Orch(),
        queue_store,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=False,
        singleton_acquired=lambda: first_resets.append("first"),
    )
    second = ExecutionDispatcher(
        Orch(),
        queue_store,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=False,
        singleton_acquired=lambda: second_resets.append("second"),
    )

    async def exercise() -> None:
        assert (await first.start())["outcome"] == "running"
        with pytest.raises(DispatcherAlreadyRunning):
            await second.start()
        # A failed instance may be asked to close by an outer lifespan.  It
        # must neither touch shared queue control nor release the owner's flock.
        await second.close()
        assert first._lock_fd is not None
        await first.close()
        assert (await second.start())["outcome"] == "running"
        await second.close()

    asyncio.run(exercise())
    assert first_resets == ["first"]
    assert second_resets == ["second"]


def test_app_local_ai_shutdown_finishes_before_singleton_release(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(
        db_path=str(tmp_path / "lifespan-order.db"),
        upload_root=tmp_path / "uploads",
    )
    dispatcher = app.state.execution_dispatcher
    events: list[str] = []

    def reset_runtime_state() -> None:
        assert dispatcher._lock_fd is not None
        events.append(
            "startup-reset" if not events else "shutdown-reset"
        )

    dispatcher.singleton_acquired = reset_runtime_state
    monkeypatch.setattr(
        app.state.store,
        "reset_local_ai_runtime_state",
        reset_runtime_state,
    )
    original_orch_shutdown = app.state.orch.shutdown

    async def shutdown_orch() -> None:
        events.append("orchestrator-shutdown")
        await original_orch_shutdown()

    async def shutdown_hub() -> None:
        assert dispatcher._lock_fd is not None
        events.append("local-ai-shutdown")

    monkeypatch.setattr(app.state.orch, "shutdown", shutdown_orch)
    monkeypatch.setattr(app.state.local_ai_service.hub, "shutdown", shutdown_hub)
    original_release = dispatcher._release_singleton

    def release_singleton() -> None:
        events.append("singleton-release")
        original_release()

    monkeypatch.setattr(dispatcher, "_release_singleton", release_singleton)

    async def exercise() -> None:
        assert events == []
        async with app.router.lifespan_context(app):
            assert events == ["startup-reset"]

    asyncio.run(exercise())
    assert events == [
        "startup-reset",
        "orchestrator-shutdown",
        "local-ai-shutdown",
        "shutdown-reset",
        "singleton-release",
    ]
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
        # Recovery exposes running so contest reconciliation can enqueue, but
        # run_once still has an explicit process-local barrier and cannot
        # refill or claim before the callback converges.
        assert store.executions.control()["dispatcher_state"] == "running"
        assert await dispatcher.run_once() == {"outcome": "recovering"}
        with pytest.raises(ExecutionMaintenanceConflict) as blocked:
            dispatcher.begin_maintenance("恢复中不得部署")
        assert blocked.value.code == "maintenance_recovery_in_progress"
        assert store.executions.control()["deployment_drain_requested"] == 0
        assert second.done() is False
        release_reconcile.set()
        return await first, await second

    assert asyncio.run(exercise()) == (True, True)
    assert runtime.cleanup_calls == 1
    assert orch.quiesce_calls == 1
    assert store.executions.control()["dispatcher_state"] == "running"


def test_cancelled_admin_resume_persists_fail_closed_pause(queue_store):
    """Client cancellation cannot expose a half-reconciled running queue."""
    store = queue_store
    rating_entered = asyncio.Event()

    class Runtime:
        supervisor = None

        async def cleanup_instance(self) -> None:
            return None

        async def ensure_runtime_ready(self) -> None:
            return None

    class Orch:
        runner = SimpleNamespace(runner=Runtime())

        async def quiesce_execution_tasks(self) -> None:
            return None

        async def recover_unsettled_match_ratings(self) -> int:
            rating_entered.set()
            await asyncio.Event().wait()
            return 0

    dispatcher = ExecutionDispatcher(
        Orch(),
        store,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=False,
    )
    store.executions.pause("manual:cancel recovery", bounded_retry=False)

    async def exercise() -> None:
        recovery = asyncio.create_task(dispatcher.admin_resume())
        await asyncio.wait_for(rating_entered.wait(), timeout=1)
        assert store.executions.control()["dispatcher_state"] == "running"
        assert dispatcher._recovering_application_state is True
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
        assert dispatcher._recovering_application_state is False
        assert await dispatcher.run_once() == {"outcome": "paused"}

    asyncio.run(exercise())
    control = store.executions.control()
    assert control["dispatcher_state"] == "paused"
    assert control["accepting"] == 0
    assert control["deployment_drain_requested"] == 0
    assert "恢复被中断" in str(control["pause_reason"])


def test_deployment_ready_waits_for_application_recovery(queue_store):
    """Drain readiness stays red while rating/contest repair can still write."""
    store = queue_store
    rating_entered = asyncio.Event()
    release_rating = asyncio.Event()

    class Runtime:
        supervisor = None

        async def cleanup_instance(self) -> None:
            return None

        async def ensure_runtime_ready(self) -> None:
            return None

    class Orch:
        runner = SimpleNamespace(runner=Runtime())

        async def quiesce_execution_tasks(self) -> None:
            return None

        async def recover_unsettled_match_ratings(self) -> int:
            rating_entered.set()
            await release_rating.wait()
            return 0

        def active_execution_task_count(self) -> int:
            return 0

    dispatcher = ExecutionDispatcher(
        Orch(),
        store,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=False,
        uploads_in_flight=lambda: 0,
    )
    store.executions.begin_maintenance("deployment recovery barrier")
    store.executions.pause("manual:runtime recovery", bounded_retry=False)

    async def exercise() -> None:
        recovery = asyncio.create_task(dispatcher.admin_resume())
        await asyncio.wait_for(rating_entered.wait(), timeout=1)
        snapshot = dispatcher.public_snapshot(include_internal=True)
        assert snapshot["maintenance"]["requested"] is True
        assert snapshot["maintenance"]["ready"] is False
        assert "application_recovery" in snapshot["maintenance"][
            "readiness_unavailable"
        ]
        release_rating.set()
        assert await asyncio.wait_for(recovery, timeout=1) is True

    asyncio.run(exercise())
    ready = dispatcher.public_snapshot(include_internal=True)
    assert ready["dispatcher"]["state"] == "running"
    assert ready["dispatcher"]["accepting"] is False
    assert ready["maintenance"]["ready"] is True


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
        conn.execute(
            "CREATE TABLE auto_match_fair_state_legacy ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton=1),"
            "next_game_idx INTEGER NOT NULL DEFAULT 0 CHECK (next_game_idx>=0),"
            "next_lane INTEGER NOT NULL DEFAULT 0 CHECK (next_lane IN (0,1)),"
            "revision INTEGER NOT NULL DEFAULT 0 CHECK (revision>=0),"
            "bootstrap_version INTEGER NOT NULL DEFAULT 0 "
            "CHECK (bootstrap_version>=0),updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO auto_match_fair_state_legacy("
            "singleton,next_game_idx,next_lane,revision,bootstrap_version,updated_at) "
            "SELECT singleton,next_game_idx,next_lane,revision,bootstrap_version,"
            "updated_at FROM auto_match_fair_state"
        )
        conn.execute("DROP TABLE auto_match_fair_state")
        conn.execute(
            "ALTER TABLE auto_match_fair_state_legacy "
            "RENAME TO auto_match_fair_state"
        )
    legacy.set_setting("auto_match_daily_cap", "5")
    legacy.set_setting("auto_match_enabled", "1")
    legacy.close()

    migrated = Store(path)
    with migrated._tx() as conn:
        assert tuple(conn.execute(
            "SELECT requested_lane,actual_lane FROM auto_match_decisions"
        ).fetchone()) == ("bootstrap", "established")
        jobs = conn.execute(
            "SELECT source,status,current_match_id,claimed_at,cleanup_state,last_error,"
            "bot_a_environment,bot_b_environment,sandbox_units,host_cpu_millis,"
            "host_memory_mb,profile_version "
            "FROM execution_jobs ORDER BY id"
        ).fetchall()
        assert tuple(jobs[0]) == (
            "auto",
            "queued",
            None,
            None,
            "none",
            "",
            "platform_low",
            "platform_low",
            2,
            2000,
            1024,
            0,
        )
        assert tuple(jobs[1]) == (
            "auto",
            "settling",
            "legacy-active-match",
            "2026-08-09T12:02:00",
            "pending",
            "legacy_execution_unscoped",
            "platform_low",
            "platform_low",
            2,
            2000,
            1024,
            0,
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
        assert tuple(
            row["name"]
            for row in conn.execute(
                "PRAGMA index_info('idx_execution_jobs_contest_claim_history')"
            )
        ) == ("source", "contest_id", "claimed_at", "id")
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
        assert {
            "dispatch_policy_version",
            "next_eligible_at",
            "gate_reason",
        } <= {
            row[1]
            for row in conn.execute("PRAGMA table_info(auto_match_fair_state)")
        }
        assert tuple(
            conn.execute(
                "SELECT dispatch_policy_version,next_eligible_at,gate_reason "
                "FROM auto_match_fair_state WHERE singleton=1"
            ).fetchone()
        ) == ("", None, "idle_grace")
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
    recovered = migrated.executions.recover_after_namespace_cleanup()
    assert recovered == {"requeued": 1, "interrupted": 0, "settling": 0}
    first_reconcile = migrated.executions.reconcile_auto_scheduler_policy()
    assert first_reconcile["changed"] is True
    assert first_reconcile["queued_cancelled"] == 2
    assert first_reconcile["active_yielding"] == 0
    assert first_reconcile["auto_scheduler"]["state"] == "disabled"
    assert first_reconcile["auto_scheduler"]["reason"] == "auto_disabled"
    with migrated._tx() as conn:
        assert [tuple(row) for row in conn.execute(
            "SELECT status,terminal_reason FROM execution_jobs ORDER BY id"
        ).fetchall()] == [
            ("cancelled", AUTO_IDLE_POLICY_CUTOVER_REASON),
            ("cancelled", AUTO_IDLE_POLICY_CUTOVER_REASON),
        ]
        assert [tuple(row) for row in conn.execute(
            "SELECT lifecycle,terminal_reason FROM auto_match_decisions ORDER BY id"
        ).fetchall()] == [
            ("cancelled", AUTO_IDLE_POLICY_CUTOVER_REASON),
            ("cancelled", AUTO_IDLE_POLICY_CUTOVER_REASON),
        ]
        assert conn.execute(
            "SELECT dispatch_policy_version FROM auto_match_fair_state "
            "WHERE singleton=1"
        ).fetchone()[0] == AUTO_MATCH_SCHEDULER_POLICY_VERSION
        assert conn.execute(
            "SELECT 1 FROM matches_index WHERE id='legacy-active-match'"
        ).fetchone() is None
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    migrated.close()

    reopened = Store(path)
    assert reopened._conn.execute("SELECT COUNT(*) FROM execution_jobs").fetchone()[0] == 2
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM execution_job_attempts"
    ).fetchone()[0] == 1
    second_reconcile = reopened.executions.reconcile_auto_scheduler_policy()
    assert second_reconcile["changed"] is False
    assert second_reconcile["queued_cancelled"] == 0
    assert second_reconcile["active_yielding"] == 0
    assert second_reconcile["next_eligible_at"] == first_reconcile["next_eligible_at"]
    assert [tuple(row) for row in reopened._conn.execute(
        "SELECT status,terminal_reason FROM execution_jobs ORDER BY id"
    ).fetchall()] == [
        ("cancelled", AUTO_IDLE_POLICY_CUTOVER_REASON),
        ("cancelled", AUTO_IDLE_POLICY_CUTOVER_REASON),
    ]
    control_after_reopen = reopened.executions.control()
    assert control_after_reopen["dispatcher_state"] == "paused"
    assert control_after_reopen["accepting"] == 1
    assert str(control_after_reopen["pause_reason"]).startswith("manual:")
    assert reopened._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()
