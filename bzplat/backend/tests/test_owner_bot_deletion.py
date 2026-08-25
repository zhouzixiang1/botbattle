"""Owner Bot tombstone, queue convergence, and race-boundary regressions."""
from __future__ import annotations

import asyncio
import hashlib
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.bots.manager import BotError, BotManager
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import (
    BotDeletedError,
    BotOwnerDeleteBusyError,
    Store,
)
from bzplat.backend.store.schema import (
    CONTEST_CANCELLED,
    CONTEST_DRAFT,
    CONTEST_FINISHED,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    CONTEST_REST,
    CONTEST_RUNNING,
    EXECUTION_SOURCE_AUTO,
    EXECUTION_SOURCE_CONTEST,
    EXECUTION_SOURCE_HUMAN,
    EXECUTION_SOURCE_MANUAL,
    SCHEMA,
    TYPE_CHALLENGE,
    TYPE_CONTEST,
    TYPE_HUMAN,
    TYPE_LADDER,
)
from bzplat.backend.tests.execution_helpers import (
    enable_execution_queue,
    verify_rating_projection,
)


_PASSWORD = "password12"


def _user(store: Store, key: str, *, role: str = "user") -> dict:
    username = f"delete_{key}"
    user = store.create_user(
        username,
        f"{username}@example.test",
        hash_password(_PASSWORD),
        role=role,
    )
    store.update_user(int(user["id"]), email_verified=1)
    return store.get_user(int(user["id"]))


def _bot(
    store: Store,
    owner: dict,
    key: str,
    *,
    versions: int = 1,
) -> dict:
    root = Path(store.path).parent
    binary = root / f"{key}.elf"
    binary.write_bytes(f"fixture-{key}".encode())
    bot = store.create_bot(
        int(owner["id"]),
        f"bot_{key}",
        binary_path=str(binary),
        format="elf",
        game_id="holdem",
    )
    version_rows = [
        store.add_bot_version(
            int(bot["id"]),
            binary_path=str(binary),
            upload_note=f"v{number}",
        )
        for number in range(1, versions + 1)
    ]
    return {
        "owner_id": int(owner["id"]),
        "bot_id": int(bot["id"]),
        "version_id": int(version_rows[-1]["id"]),
        "versions": version_rows,
        "name": str(bot["name"]),
        "binary_path": str(binary),
    }


def _decision(store: Store, a: dict, b: dict, key: str) -> int:
    with store._tx() as conn:
        cur = conn.execute(
            "INSERT INTO auto_match_decisions("
            "policy_version,state_revision,cursor_game_idx,requested_lane,"
            "actual_lane,fallback_reason,game_id,bot_a_id,bot_b_id,owner_a_id,"
            "owner_b_id,bot_a_version_id,bot_b_version_id,owner_a_service_before,"
            "owner_b_service_before,bot_a_service_before,bot_b_service_before,"
            "bot_pair_count_before,owner_pair_count_before,rating_gap,"
            "bot_a_seat_debt_before,bot_b_seat_debt_before,selection_reason,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "test-policy",
                0,
                0,
                "bootstrap",
                "bootstrap",
                "",
                "holdem",
                a["bot_id"],
                b["bot_id"],
                a["owner_id"],
                b["owner_id"],
                a["version_id"],
                b["version_id"],
                0,
                0,
                0,
                0,
                0,
                0,
                0.0,
                0,
                0,
                key,
                "2026-08-22T00:00:00",
            ),
        )
        return int(cur.lastrowid)


def _enqueue(store: Store, source: str, a: dict, b: dict) -> dict:
    if source == EXECUTION_SOURCE_HUMAN:
        return store.executions.enqueue(
            source=source,
            owner_user_id=a["owner_id"],
            game_id="holdem",
            match_type=TYPE_HUMAN,
            bot_a_id=a["bot_id"],
            bot_b_id=a["bot_id"],
            bot_a_version_id=a["version_id"],
            bot_b_version_id=None,
            human_user_id=a["owner_id"],
            human_seat=1,
        )
    if source == EXECUTION_SOURCE_CONTEST:
        contest = store.create_contest(
            f"finished-{a['bot_id']}",
            a["owner_id"],
            status=CONTEST_FINISHED,
            game_id="holdem",
        )
        pairing = store.add_pairing(
            int(contest["id"]),
            a["bot_id"],
            b["bot_id"],
            bot_a_version_id=a["version_id"],
            bot_b_version_id=b["version_id"],
        )
        return store.executions.enqueue(
            source=source,
            owner_user_id=a["owner_id"],
            game_id="holdem",
            match_type=TYPE_CONTEST,
            bot_a_id=a["bot_id"],
            bot_b_id=b["bot_id"],
            bot_a_version_id=a["version_id"],
            bot_b_version_id=b["version_id"],
            contest_id=int(contest["id"]),
            contest_pairing_id=int(pairing["id"]),
        )
    return store.executions.enqueue(
        source=source,
        owner_user_id=(
            None if source == EXECUTION_SOURCE_AUTO else a["owner_id"]
        ),
        game_id="holdem",
        match_type=(
            TYPE_LADDER if source == EXECUTION_SOURCE_AUTO else TYPE_CHALLENGE
        ),
        bot_a_id=a["bot_id"],
        bot_b_id=b["bot_id"],
        bot_a_version_id=a["version_id"],
        bot_b_version_id=b["version_id"],
    )


def _force_active_job_state(store: Store, public_id: str, state: str) -> None:
    assert state in {"starting", "running", "settling"}
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status=?,current_match_id=?,claimed_at=?,"
            "started_at=?,settling_at=?,cleanup_state=? WHERE public_id=?",
            (
                state,
                f"active-{public_id}",
                "2026-08-22T00:00:01",
                (
                    "2026-08-22T00:00:02"
                    if state == "running"
                    else None
                ),
                (
                    "2026-08-22T00:00:03"
                    if state == "settling"
                    else None
                ),
                "pending" if state == "settling" else "none",
                public_id,
            ),
        )


def _table_snapshot(store: Store, *tables: str) -> dict[str, tuple[tuple, ...]]:
    with store._tx() as conn:
        return {
            table: tuple(
                tuple(row)
                for row in conn.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
            )
            for table in tables
        }


def _contest_with_entry(
    store: Store,
    owner: dict,
    bot: dict,
    *,
    status: str,
    key: str,
) -> dict:
    contest = store.create_contest(
        f"contest-{key}",
        int(owner["id"]),
        status=status,
        game_id="holdem",
    )
    store.add_contest_entry(
        int(contest["id"]), int(owner["id"]), int(bot["bot_id"])
    )
    return contest


def test_owner_delete_atomically_converges_durable_state_and_is_idempotent(
    tmp_path,
):
    store = Store(str(tmp_path / "converge.db"))
    owner = _user(store, "converge_owner")
    opponent_owner = _user(store, "converge_opponent")
    target = _bot(store, owner, "converge", versions=2)
    opponent = _bot(store, opponent_owner, "converge_other")
    store.ensure_rating(target["bot_id"], game_id="holdem")
    history_match_id = "owner-delete-history"
    store.create_match(
        history_match_id,
        target["bot_id"],
        opponent["bot_id"],
        owner_id=target["owner_id"],
        match_type=TYPE_CHALLENGE,
        game_id="holdem",
    )
    store.update_match(
        history_match_id,
        status="completed",
        winner=0,
        result={
            "rounds_played": 1,
            "deltas": [1, -1],
            "normalized_delta": 1,
        },
    )
    assert store.mark_match_rating_settled(history_match_id) is True
    store.upsert_replay(
        history_match_id,
        '[{"type":"match_end","winner":0,"reason":"completed"}]',
    )
    assert store.replace_match_debug(
        history_match_id,
        [
            {
                "seat": 0,
                "turn": 1,
                "leg": -1,
                "debug_json": '{"note":"retained"}',
            }
        ],
    )
    verify_rating_projection(store)
    history_before = store.get_match(history_match_id)
    replay_before = store.get_replay(history_match_id)
    debug_before = store.get_match_debug_for_user(
        history_match_id,
        user_id=target["owner_id"],
        is_admin=False,
    )
    enable_execution_queue(store)

    retry = _enqueue(store, EXECUTION_SOURCE_MANUAL, target, opponent)
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status='interrupted',terminal_at=?,"
            "terminal_reason='transient',last_error='transient',retryable=1,"
            "next_attempt_at=? WHERE public_id=?",
            (
                "2026-08-22T00:00:04",
                "2099-01-01T00:00:00",
                retry["public_id"],
            ),
        )
    # Keep the auto row as the deletion fixture by inserting it after the
    # foreground request has already become terminal. Foreground enqueue now
    # intentionally cancels any older queued auto in the same transaction.
    decision_id = _decision(store, target, opponent, "delete convergence")
    queued = store.executions.enqueue(
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
        game_id="holdem",
        match_type=TYPE_LADDER,
        bot_a_id=target["bot_id"],
        bot_b_id=opponent["bot_id"],
        bot_a_version_id=target["version_id"],
        bot_b_version_id=opponent["version_id"],
        auto_decision_id=decision_id,
    )

    agent = store.create_local_ai_agent(
        owner_id=target["owner_id"],
        bot_id=target["bot_id"],
        label="desktop-agent",
        public_id="agent-delete-converge",
        token_hash=hashlib.sha256(b"delete-converge").hexdigest(),
        token_hint="converge",
    )
    with store._tx() as conn:
        conn.execute(
            "INSERT INTO local_ai_leases(agent_id,job_public_id,attempt_no,seat,"
            "status,acquired_at) VALUES(?,?,?,?,?,?)",
            (
                int(agent["id"]),
                "old-local-job",
                1,
                0,
                "active",
                "2026-08-22T00:00:00",
            ),
        )

    result = store.owner_delete_bot(target["owner_id"], target["bot_id"])

    assert result["changed"] is True
    assert result["cancelled_queued_jobs"] == 1
    assert result["invalidated_retryable_jobs"] == 1
    assert result["revoked_local_ai_public_ids"] == [agent["public_id"]]
    deleted = store.get_bot(target["bot_id"])
    assert deleted["owner_deleted_at"]
    assert deleted["is_active"] == deleted["is_ranked"] == 0
    cancelled = store.executions.get(queued["public_id"])
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] == 1
    assert cancelled["retryable"] == 0
    assert cancelled["next_attempt_at"] is None
    assert cancelled["terminal_reason"] == "bot_owner_deleted"
    retried = store.executions.get(retry["public_id"])
    assert retried["status"] == "interrupted"
    assert retried["retryable"] == 0
    assert retried["next_attempt_at"] is None
    with store._tx() as conn:
        decision = conn.execute(
            "SELECT lifecycle,terminal_reason FROM auto_match_decisions WHERE id=?",
            (decision_id,),
        ).fetchone()
        lease = conn.execute(
            "SELECT status,terminal_reason FROM local_ai_leases WHERE agent_id=?",
            (int(agent["id"]),),
        ).fetchone()
    assert tuple(decision) == ("cancelled", "bot_owner_deleted")
    assert tuple(lease) == ("released", "bot_owner_deleted")
    assert store.get_local_ai_agent(int(agent["id"]))["status"] == "revoked"

    # Binary, historical Match/Replay/private debug, versions, rating, and the
    # unique owner/name identity remain intact even though owner inventory hides it.
    assert Path(target["binary_path"]).read_bytes() == b"fixture-converge"
    assert store.get_match(history_match_id) == history_before
    assert store.get_replay(history_match_id) == replay_before
    assert store.get_match_debug_for_user(
        history_match_id,
        user_id=target["owner_id"],
        is_admin=False,
    ) == debug_before
    assert len(store.list_bot_versions(target["bot_id"])) == 2
    assert store.get_rating(target["bot_id"], game_id="holdem") is not None
    assert target["bot_id"] in {
        int(row["id"]) for row in store.list_bots(active_only=False)
    }
    assert target["bot_id"] not in {
        int(row["id"])
        for row in store.list_bots(
            owner_id=target["owner_id"],
            active_only=False,
            include_owner_deleted=False,
        )
    }
    profile = store.user_profile(owner["username"])
    assert profile["bot_count"] == 0
    assert profile["stats"]["rated_bots"] == 1
    assert store.bot_profile(target["bot_id"])["owner_deleted_at"]
    with pytest.raises(sqlite3.IntegrityError):
        store.create_bot(
            target["owner_id"],
            target["name"],
            binary_path=target["binary_path"],
            format="elf",
            game_id="holdem",
        )

    repeated = store.owner_delete_bot(target["owner_id"], target["bot_id"])
    assert repeated["changed"] is False
    assert repeated["revoked_local_ai_public_ids"] == [agent["public_id"]]
    repeated_bot = store.get_bot(target["bot_id"])
    assert repeated_bot["owner_deleted_at"] == deleted["owner_deleted_at"]
    assert repeated_bot["updated_at"] == deleted["updated_at"]

    for mutation in (
        lambda: store.add_bot_version(
            target["bot_id"], binary_path=target["binary_path"]
        ),
        lambda: store.set_current_version(target["bot_id"], 1),
        lambda: store.delete_bot_version(target["bot_id"], 1),
        lambda: store.update_owned_bot(
            target["owner_id"], target["bot_id"], display_name="revive"
        ),
        lambda: store.select_ranked_bot(
            target["owner_id"], target["bot_id"], if_empty=False
        ),
        lambda: store.clear_ranked_bot(target["owner_id"], target["bot_id"]),
        lambda: store.create_local_ai_agent(
            owner_id=target["owner_id"],
            bot_id=target["bot_id"],
            label="second-agent",
            public_id="agent-after-delete",
            token_hash=hashlib.sha256(b"after-delete").hexdigest(),
            token_hint="deleted1",
        ),
    ):
        with pytest.raises(BotDeletedError):
            mutation()

    renamed = store.update_admin_bot(
        target["bot_id"], display_name="historical identity"
    )
    assert renamed["display_name"] == "historical identity"
    with pytest.raises(BotDeletedError):
        store.update_admin_bot(target["bot_id"], is_active=1)


@pytest.mark.parametrize(
    "source",
    (
        EXECUTION_SOURCE_MANUAL,
        EXECUTION_SOURCE_HUMAN,
        EXECUTION_SOURCE_CONTEST,
        EXECUTION_SOURCE_AUTO,
    ),
)
def test_owner_delete_cancels_every_safe_queued_source(tmp_path, source: str):
    store = Store(str(tmp_path / f"queued-{source}.db"))
    owner = _user(store, f"queued_{source}_owner")
    opponent_owner = _user(store, f"queued_{source}_other")
    target = _bot(store, owner, f"queued_{source}")
    opponent = _bot(store, opponent_owner, f"queued_{source}_opponent")
    enable_execution_queue(store)
    job = _enqueue(store, source, target, opponent)

    result = store.owner_delete_bot(target["owner_id"], target["bot_id"])

    assert result["changed"] is True
    assert result["cancelled_queued_jobs"] == 1
    cancelled = store.executions.get(str(job["public_id"]))
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancel_requested"] == 1
    assert cancelled["retryable"] == 0
    assert cancelled["next_attempt_at"] is None
    assert cancelled["terminal_reason"] == "bot_owner_deleted"


@pytest.mark.parametrize(
    "source",
    (
        EXECUTION_SOURCE_MANUAL,
        EXECUTION_SOURCE_HUMAN,
        EXECUTION_SOURCE_CONTEST,
        EXECUTION_SOURCE_AUTO,
    ),
)
@pytest.mark.parametrize("state", ("starting", "running", "settling"))
def test_owner_delete_rejects_every_active_job_source_without_writes(
    tmp_path, source: str, state: str
):
    store = Store(str(tmp_path / f"busy-{source}-{state}.db"))
    owner = _user(store, f"busy_{source}_{state}_owner")
    opponent_owner = _user(store, f"busy_{source}_{state}_other")
    target = _bot(store, owner, f"busy_{source}_{state}")
    opponent = _bot(store, opponent_owner, f"other_{source}_{state}")
    enable_execution_queue(store)
    job = _enqueue(store, source, target, opponent)
    _force_active_job_state(store, str(job["public_id"]), state)

    with pytest.raises(BotOwnerDeleteBusyError) as raised:
        store.owner_delete_bot(target["owner_id"], target["bot_id"])

    assert raised.value.code == "bot_busy"
    assert store.get_bot(target["bot_id"])["owner_deleted_at"] is None
    assert store.executions.get(job["public_id"])["status"] == state


def test_owner_delete_busy_keeps_every_convergence_table_unchanged(tmp_path):
    store = Store(str(tmp_path / "busy-zero-write.db"))
    owner = _user(store, "busy_zero_owner")
    opponent_owner = _user(store, "busy_zero_other")
    target = _bot(store, owner, "busy_zero")
    opponent = _bot(store, opponent_owner, "busy_zero_other")
    enable_execution_queue(store)
    verify_rating_projection(store)

    active = _enqueue(store, EXECUTION_SOURCE_HUMAN, target, opponent)
    _force_active_job_state(store, str(active["public_id"]), "starting")
    retry = _enqueue(store, EXECUTION_SOURCE_MANUAL, target, opponent)
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status='interrupted',retryable=1,"
            "terminal_reason='transient',last_error='transient',"
            "terminal_at='2026-08-22T00:00:00',"
            "next_attempt_at='2099-01-01T00:00:00' WHERE public_id=?",
            (str(retry["public_id"]),),
        )
    decision_id = _decision(store, target, opponent, "busy zero write")
    queued = store.executions.enqueue(
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
        game_id="holdem",
        match_type=TYPE_LADDER,
        bot_a_id=target["bot_id"],
        bot_b_id=opponent["bot_id"],
        bot_a_version_id=target["version_id"],
        bot_b_version_id=opponent["version_id"],
        auto_decision_id=decision_id,
    )
    agent = store.create_local_ai_agent(
        owner_id=target["owner_id"],
        bot_id=target["bot_id"],
        label="busy-zero-agent",
        public_id="agent-busy-zero",
        token_hash=hashlib.sha256(b"busy-zero").hexdigest(),
        token_hint="busyzero",
    )
    with store._tx() as conn:
        conn.execute(
            "INSERT INTO local_ai_leases(agent_id,job_public_id,attempt_no,seat,"
            "status,acquired_at) VALUES(?,?,?,?,?,?)",
            (
                int(agent["id"]),
                "busy-zero-lease",
                1,
                0,
                "active",
                "2026-08-22T00:00:00",
            ),
        )

    tables = (
        "bots",
        "execution_jobs",
        "auto_match_decisions",
        "local_ai_agents",
        "local_ai_leases",
        "rating_projection_state",
    )
    before = _table_snapshot(store, *tables)

    with pytest.raises(BotOwnerDeleteBusyError) as raised:
        store.owner_delete_bot(target["owner_id"], target["bot_id"])

    assert raised.value.code == "bot_busy"
    assert _table_snapshot(store, *tables) == before
    assert store.executions.get(str(queued["public_id"]))["status"] == "queued"


@pytest.mark.parametrize("status", ("pending", "running"))
def test_owner_delete_rejects_active_neutral_match_without_writes(
    tmp_path, status: str
):
    store = Store(str(tmp_path / f"match-{status}.db"))
    owner = _user(store, f"match_{status}_owner")
    target = _bot(store, owner, f"match_{status}")
    opponent = _bot(store, owner, f"match_{status}_other")
    match_id = f"neutral-{status}"
    store.create_match(
        match_id,
        target["bot_id"],
        opponent["bot_id"],
        owner_id=target["owner_id"],
        match_type=TYPE_CHALLENGE,
        game_id="holdem",
    )
    if status == "running":
        store.update_match(match_id, status="running")
    verify_rating_projection(store)

    with pytest.raises(BotOwnerDeleteBusyError) as raised:
        store.owner_delete_bot(target["owner_id"], target["bot_id"])

    assert raised.value.code == "bot_busy"
    assert store.get_bot(target["bot_id"])["owner_deleted_at"] is None
    assert store.get_match(match_id)["status"] == status


def test_owner_delete_rejects_completed_rated_unsettled_match(tmp_path):
    store = Store(str(tmp_path / "rated-unsettled.db"))
    owner = _user(store, "rated_owner")
    opponent_owner = _user(store, "rated_other")
    target = _bot(store, owner, "rated")
    opponent = _bot(store, opponent_owner, "rated_other")
    verify_rating_projection(store)
    store.select_ranked_bot(target["owner_id"], target["bot_id"], if_empty=True)
    store.select_ranked_bot(
        opponent["owner_id"], opponent["bot_id"], if_empty=True
    )
    store.create_match(
        "rated-unsettled",
        target["bot_id"],
        opponent["bot_id"],
        owner_id=target["owner_id"],
        match_type=TYPE_CHALLENGE,
        game_id="holdem",
    )
    store.update_match(
        "rated-unsettled",
        status="completed",
        winner=0,
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
    )

    with pytest.raises(BotOwnerDeleteBusyError) as raised:
        store.owner_delete_bot(target["owner_id"], target["bot_id"])

    assert raised.value.code == "ranking_busy"
    assert store.get_bot(target["bot_id"])["owner_deleted_at"] is None


@pytest.mark.parametrize(
    "status", (CONTEST_OPEN, CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST)
)
def test_live_contest_entry_blocks_owner_delete(tmp_path, status: str):
    store = Store(str(tmp_path / f"contest-{status}.db"))
    owner = _user(store, f"contest_{status}")
    target = _bot(store, owner, f"contest_{status}")
    contest = _contest_with_entry(
        store, owner, target, status=status, key=status
    )
    verify_rating_projection(store)

    with pytest.raises(BotOwnerDeleteBusyError) as raised:
        store.owner_delete_bot(target["owner_id"], target["bot_id"])

    assert raised.value.code == "bot_busy"
    assert "联系赛事组织者" in raised.value.message
    assert store.get_bot(target["bot_id"])["owner_deleted_at"] is None
    assert store.get_contest(int(contest["id"]))["status"] == status


@pytest.mark.parametrize(
    "status", (CONTEST_OPEN, CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST)
)
def test_live_contest_pairing_alone_blocks_owner_delete(tmp_path, status: str):
    store = Store(str(tmp_path / f"pairing-only-{status}.db"))
    owner = _user(store, f"pairing_only_{status}_owner")
    opponent_owner = _user(store, f"pairing_only_{status}_other")
    target = _bot(store, owner, f"pairing_only_{status}")
    opponent = _bot(store, opponent_owner, f"pairing_other_{status}")
    contest = store.create_contest(
        f"pairing-only-{status}",
        int(owner["id"]),
        status=status,
        game_id="holdem",
    )
    pairing = store.add_pairing(
        int(contest["id"]), target["bot_id"], opponent["bot_id"]
    )
    verify_rating_projection(store)

    with pytest.raises(BotOwnerDeleteBusyError) as raised:
        store.owner_delete_bot(target["owner_id"], target["bot_id"])

    assert raised.value.code == "bot_busy"
    assert store.get_bot(target["bot_id"])["owner_deleted_at"] is None
    persisted = {
        int(row["id"]): row for row in store.list_pairings(int(contest["id"]))
    }
    assert persisted[int(pairing["id"])]["bot_a_id"] == target["bot_id"]


@pytest.mark.parametrize("status", (CONTEST_FINISHED, CONTEST_CANCELLED))
def test_terminal_contest_history_does_not_block_owner_delete(tmp_path, status: str):
    store = Store(str(tmp_path / f"terminal-{status}.db"))
    owner = _user(store, f"terminal_{status}")
    target = _bot(store, owner, f"terminal_{status}")
    contest = _contest_with_entry(
        store, owner, target, status=status, key=f"terminal-{status}"
    )
    verify_rating_projection(store)

    result = store.owner_delete_bot(target["owner_id"], target["bot_id"])

    assert result["changed"] is True
    assert store.get_contest(int(contest["id"]))["status"] == status
    assert store.list_contest_entries(int(contest["id"]))[0]["bot_id"] == target[
        "bot_id"
    ]


def test_draft_reference_allows_delete_but_cannot_transition_live(tmp_path):
    store = Store(str(tmp_path / "draft-delete.db"))
    owner = _user(store, "draft_owner")
    opponent_owner = _user(store, "draft_other_owner")
    target = _bot(store, owner, "draft")
    opponent = _bot(store, opponent_owner, "draft_other")
    contest = _contest_with_entry(
        store, owner, target, status=CONTEST_DRAFT, key="draft"
    )
    verify_rating_projection(store)

    assert store.owner_delete_bot(target["owner_id"], target["bot_id"])[
        "changed"
    ] is True
    with pytest.raises(ValueError, match="已删除 Bot"):
        store.update_contest(int(contest["id"]), status=CONTEST_OPEN)
    assert store.get_contest(int(contest["id"]))["status"] == CONTEST_DRAFT

    with pytest.raises(
        sqlite3.IntegrityError,
        match="live contest cannot reference owner-deleted Bot",
    ):
        with store._tx() as conn:
            conn.execute(
                "UPDATE contests SET status='open' WHERE id=?",
                (int(contest["id"]),),
            )

    another = store.create_contest(
        "draft-after-delete",
        int(owner["id"]),
        status=CONTEST_DRAFT,
        game_id="holdem",
    )
    with pytest.raises(ValueError, match="停用或删除"):
        store.add_contest_roster_entries(
            int(another["id"]), [(int(owner["id"]), target["bot_id"])]
        )
    with pytest.raises(sqlite3.IntegrityError, match="must be active"):
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO contest_entries(contest_id,user_id,bot_id,registered_at) "
                "VALUES(?,?,?,?)",
                (
                    int(another["id"]),
                    int(owner["id"]),
                    target["bot_id"],
                    "2026-08-22T00:00:00",
                ),
            )

    # Draft history may retain the tombstone, but no direct or Store pairing
    # writer can inject that identity into an already-live contest.
    store.add_pairing(
        int(contest["id"]), target["bot_id"], opponent["bot_id"]
    )
    live = store.create_contest(
        "live-after-delete",
        int(owner["id"]),
        status=CONTEST_OPEN,
        game_id="holdem",
    )
    with pytest.raises(ValueError, match="对阵不能引用已删除 Bot"):
        store.add_pairing(
            int(live["id"]), target["bot_id"], opponent["bot_id"]
        )
    safe_pairing = store.add_pairing(
        int(live["id"]), opponent["bot_id"], opponent["bot_id"]
    )
    with pytest.raises(ValueError, match="对阵不能引用已删除 Bot"):
        store.update_contest_pairing(
            int(safe_pairing["id"]), bot_a_id=target["bot_id"]
        )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="live contest pairing cannot reference owner-deleted Bot",
    ):
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO contest_pairings(contest_id,bot_a_id,bot_b_id) "
                "VALUES(?,?,?)",
                (int(live["id"]), target["bot_id"], opponent["bot_id"]),
            )
    with pytest.raises(
        sqlite3.IntegrityError,
        match="live contest pairing cannot reference owner-deleted Bot",
    ):
        with store._tx() as conn:
            conn.execute(
                "UPDATE contest_pairings SET bot_a_id=? WHERE id=?",
                (target["bot_id"], int(safe_pairing["id"])),
            )


def test_draft_transition_first_makes_owner_delete_busy(tmp_path):
    store = Store(str(tmp_path / "draft-transition-first.db"))
    owner = _user(store, "draft_first_owner")
    target = _bot(store, owner, "draft_first")
    contest = _contest_with_entry(
        store, owner, target, status=CONTEST_DRAFT, key="draft-first"
    )
    verify_rating_projection(store)

    moved = store.update_contest(int(contest["id"]), status=CONTEST_OPEN)
    assert moved["status"] == CONTEST_OPEN
    with pytest.raises(BotOwnerDeleteBusyError) as raised:
        store.owner_delete_bot(target["owner_id"], target["bot_id"])
    assert raised.value.code == "bot_busy"
    assert store.get_bot(target["bot_id"])["owner_deleted_at"] is None


def test_live_transition_keeps_legacy_inactive_bot_compatibility(tmp_path):
    store = Store(str(tmp_path / "inactive-contest.db"))
    owner = _user(store, "inactive_owner")
    target = _bot(store, owner, "inactive")
    contest = _contest_with_entry(
        store, owner, target, status=CONTEST_DRAFT, key="inactive"
    )
    verify_rating_projection(store)
    store.update_bot(target["bot_id"], is_active=0)

    moved = store.update_contest(int(contest["id"]), status=CONTEST_OPEN)

    assert moved["status"] == CONTEST_OPEN
    assert store.get_bot(target["bot_id"])["owner_deleted_at"] is None


def test_register_delete_race_is_serialized_without_integrity_error(tmp_path):
    path = tmp_path / "register-delete-race.db"
    seed = Store(str(path))
    owner = _user(seed, "register_owner")
    target = _bot(seed, owner, "register")
    contest = seed.create_contest(
        "register-race",
        int(owner["id"]),
        status=CONTEST_OPEN,
        game_id="holdem",
    )
    verify_rating_projection(seed)
    seed.close()
    register_store = Store(str(path))
    delete_store = Store(str(path))
    barrier = threading.Barrier(2)

    def register():
        barrier.wait(timeout=5)
        try:
            register_store.add_contest_entry_once(
                int(contest["id"]), int(owner["id"]), target["bot_id"]
            )
            return "registered"
        except ValueError:
            return "unavailable"

    def delete():
        barrier.wait(timeout=5)
        try:
            delete_store.owner_delete_bot(target["owner_id"], target["bot_id"])
            return "deleted"
        except BotOwnerDeleteBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(register), pool.submit(delete)]
        outcomes = {future.result(timeout=10) for future in futures}

    # The two valid serial orders are register→busy and delete→unavailable.
    assert outcomes in ({"registered", "busy"}, {"deleted", "unavailable"})
    bot = delete_store.get_bot(target["bot_id"])
    entry = delete_store.get_contest_entry(
        int(contest["id"]), int(owner["id"])
    )
    assert not (bot["owner_deleted_at"] is not None and entry is not None)
    register_store.close()
    delete_store.close()


def test_claim_delete_race_has_only_complete_serial_outcomes(tmp_path):
    path = tmp_path / "claim-delete-race.db"
    seed = Store(str(path))
    owner = _user(seed, "claim_owner")
    opponent_owner = _user(seed, "claim_other_owner")
    target = _bot(seed, owner, "claim")
    opponent = _bot(seed, opponent_owner, "claim_other")
    enable_execution_queue(seed)
    job = _enqueue(seed, EXECUTION_SOURCE_MANUAL, target, opponent)
    verify_rating_projection(seed)
    seed.close()
    claim_store = Store(str(path))
    delete_store = Store(str(path))
    barrier = threading.Barrier(2)

    def claim():
        barrier.wait(timeout=5)
        claimed = claim_store.executions.claim_next(
            max_match_slots=2,
            max_sandbox_units=4,
            aging_seconds=60,
            user_active_limit=1,
            contest_share_slots=1,
        )
        return "claimed" if claimed is not None else "empty"

    def delete():
        barrier.wait(timeout=5)
        try:
            delete_store.owner_delete_bot(target["owner_id"], target["bot_id"])
            return "deleted"
        except BotOwnerDeleteBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(claim), pool.submit(delete)]
        outcomes = {future.result(timeout=10) for future in futures}

    assert outcomes in ({"claimed", "busy"}, {"deleted", "empty"})
    bot = delete_store.get_bot(target["bot_id"])
    persisted_job = delete_store.executions.get(str(job["public_id"]))
    if "deleted" in outcomes:
        assert bot["owner_deleted_at"] is not None
        assert persisted_job["status"] == "cancelled"
        assert persisted_job["current_match_id"] is None
    else:
        assert bot["owner_deleted_at"] is None
        assert persisted_job["status"] == "starting"
        assert persisted_job["current_match_id"] is not None
    claim_store.close()
    delete_store.close()


def test_entry_swap_delete_race_never_writes_tombstone_after_guard(tmp_path):
    path = tmp_path / "entry-swap-delete-race.db"
    seed = Store(str(path))
    owner = _user(seed, "swap_owner")
    target = _bot(seed, owner, "swap_target")
    replacement = _bot(seed, owner, "swap_replacement")
    contest = _contest_with_entry(
        seed,
        owner,
        replacement,
        status=CONTEST_DRAFT,
        key="swap-race",
    )
    verify_rating_projection(seed)
    seed.close()
    entry_store = Store(str(path))
    delete_store = Store(str(path))
    barrier = threading.Barrier(2)

    def swap():
        barrier.wait(timeout=5)
        try:
            entry_store.update_entry(
                int(contest["id"]), int(owner["id"]), bot_id=target["bot_id"]
            )
            return "updated"
        except ValueError:
            return "unavailable"

    def delete():
        barrier.wait(timeout=5)
        delete_store.owner_delete_bot(target["owner_id"], target["bot_id"])
        return "deleted"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(swap), pool.submit(delete)]
        outcomes = {future.result(timeout=10) for future in futures}

    assert outcomes in ({"updated", "deleted"}, {"unavailable", "deleted"})
    assert delete_store.get_bot(target["bot_id"])["owner_deleted_at"] is not None
    entry = delete_store.get_contest_entry(int(contest["id"]), int(owner["id"]))
    if "updated" in outcomes:
        assert entry["bot_id"] == target["bot_id"]
    else:
        assert entry["bot_id"] == replacement["bot_id"]
    entry_store.close()
    delete_store.close()


@pytest.mark.parametrize("reference_kind", ("entry", "pairing"))
def test_draft_live_transition_delete_race_is_serialized(
    tmp_path, reference_kind: str
):
    path = tmp_path / f"transition-delete-race-{reference_kind}.db"
    seed = Store(str(path))
    owner = _user(seed, f"transition_{reference_kind}_owner")
    opponent_owner = _user(seed, f"transition_{reference_kind}_other_owner")
    target = _bot(seed, owner, f"transition_{reference_kind}")
    opponent = _bot(seed, opponent_owner, f"transition_{reference_kind}_other")
    contest = seed.create_contest(
        f"transition-{reference_kind}",
        int(owner["id"]),
        status=CONTEST_DRAFT,
        game_id="holdem",
    )
    if reference_kind == "entry":
        seed.add_contest_entry(
            int(contest["id"]), int(owner["id"]), target["bot_id"]
        )
    else:
        seed.add_pairing(
            int(contest["id"]), target["bot_id"], opponent["bot_id"]
        )
    verify_rating_projection(seed)
    seed.close()
    transition_store = Store(str(path))
    delete_store = Store(str(path))
    barrier = threading.Barrier(2)

    def transition():
        barrier.wait(timeout=5)
        try:
            transition_store.update_contest(
                int(contest["id"]), status=CONTEST_OPEN
            )
            return "opened"
        except ValueError:
            return "blocked"

    def delete():
        barrier.wait(timeout=5)
        try:
            delete_store.owner_delete_bot(target["owner_id"], target["bot_id"])
            return "deleted"
        except BotOwnerDeleteBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(transition), pool.submit(delete)]
        outcomes = {future.result(timeout=10) for future in futures}

    assert outcomes in ({"opened", "busy"}, {"blocked", "deleted"})
    bot = delete_store.get_bot(target["bot_id"])
    persisted_contest = delete_store.get_contest(int(contest["id"]))
    if "deleted" in outcomes:
        assert bot["owner_deleted_at"] is not None
        assert persisted_contest["status"] == CONTEST_DRAFT
    else:
        assert bot["owner_deleted_at"] is None
        assert persisted_contest["status"] == CONTEST_OPEN
    transition_store.close()
    delete_store.close()


def test_pairing_insert_delete_race_is_serialized(tmp_path):
    path = tmp_path / "pairing-insert-delete-race.db"
    seed = Store(str(path))
    owner = _user(seed, "pair_race_owner")
    opponent_owner = _user(seed, "pair_race_other_owner")
    target = _bot(seed, owner, "pair_race")
    opponent = _bot(seed, opponent_owner, "pair_race_other")
    contest = seed.create_contest(
        "pairing-race",
        int(owner["id"]),
        status=CONTEST_OPEN,
        game_id="holdem",
    )
    verify_rating_projection(seed)
    seed.close()
    pairing_store = Store(str(path))
    delete_store = Store(str(path))
    barrier = threading.Barrier(2)

    def add_pairing():
        barrier.wait(timeout=5)
        try:
            pairing_store.add_pairing(
                int(contest["id"]), target["bot_id"], opponent["bot_id"]
            )
            return "paired"
        except ValueError:
            return "unavailable"

    def delete():
        barrier.wait(timeout=5)
        try:
            delete_store.owner_delete_bot(target["owner_id"], target["bot_id"])
            return "deleted"
        except BotOwnerDeleteBusyError:
            return "busy"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(add_pairing), pool.submit(delete)]
        outcomes = {future.result(timeout=10) for future in futures}

    assert outcomes in ({"paired", "busy"}, {"deleted", "unavailable"})
    bot = delete_store.get_bot(target["bot_id"])
    pairings = delete_store.list_pairings(int(contest["id"]))
    assert not (bot["owner_deleted_at"] is not None and pairings)
    pairing_store.close()
    delete_store.close()


def _assert_owner_delete_sql_guards(store: Store) -> None:
    owner = _user(store, f"sql_{Path(store.path).stem}")
    target = _bot(store, owner, f"sql_{Path(store.path).stem}")
    with pytest.raises(sqlite3.IntegrityError):
        with store._tx() as conn:
            conn.execute(
                "UPDATE bots SET owner_deleted_at='2026-08-22T00:00:00' WHERE id=?",
                (target["bot_id"],),
            )
    with store._tx() as conn:
        conn.execute(
            "UPDATE bots SET owner_deleted_at='2026-08-22T00:00:00',"
            "is_active=0,is_ranked=0 WHERE id=?",
            (target["bot_id"],),
        )
    for sql in (
        "UPDATE bots SET owner_deleted_at=NULL WHERE id=?",
        "UPDATE bots SET owner_deleted_at='2026-08-22T00:00:01' WHERE id=?",
        "UPDATE bots SET is_active=1 WHERE id=?",
        "UPDATE bots SET is_ranked=1 WHERE id=?",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            with store._tx() as conn:
                conn.execute(sql, (target["bot_id"],))


def test_fresh_and_legacy_schema_install_canonical_owner_delete_guards(tmp_path):
    expected = {
        "trg_bots_owner_deleted_guard_insert",
        "trg_bots_owner_deleted_guard_update",
        "trg_contest_entries_live_bot_insert",
        "trg_contest_entries_live_bot_update",
        "trg_contests_live_state_deleted_bot_guard",
        "trg_contest_pairings_live_bot_insert",
        "trg_contest_pairings_live_bot_update",
    }

    def definitions(store: Store) -> dict[str, str]:
        rows = store._conn.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        return {
            str(row[0]): " ".join(str(row[1]).split())
            for row in rows
            if str(row[0]) in expected
        }

    fresh = Store(str(tmp_path / "fresh.db"))
    fresh_definitions = definitions(fresh)
    assert set(fresh_definitions) == expected
    _assert_owner_delete_sql_guards(fresh)
    fresh.close()

    legacy_path = tmp_path / "legacy.db"
    legacy_schema = SCHEMA.replace("    owner_deleted_at TEXT,\n", "").replace(
        "    CONSTRAINT chk_bot_owner_deleted CHECK (\n"
        "        owner_deleted_at IS NULL OR (is_active=0 AND is_ranked=0)),\n",
        "",
    )
    conn = sqlite3.connect(legacy_path)
    conn.executescript(legacy_schema)
    conn.execute(
        "CREATE UNIQUE INDEX idx_bots_one_ranked_per_owner_game "
        "ON bots(owner_id,game_id) WHERE is_ranked=1"
    )
    assert "owner_deleted_at" not in {
        row[1] for row in conn.execute("PRAGMA table_info(bots)")
    }
    owner_id = int(
        conn.execute(
            "INSERT INTO users(username,email,password_hash,created_at) "
            "VALUES(?,?,?,?)",
            (
                "legacy_inactive",
                "legacy-inactive@example.test",
                "hash",
                "2026-08-22T00:00:00",
            ),
        ).lastrowid
    )
    legacy_inactive_id = int(
        conn.execute(
            "INSERT INTO bots(owner_id,name,is_active,game_id,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            (
                owner_id,
                "legacy_inactive_bot",
                0,
                "holdem",
                "2026-08-22T00:00:00",
                "2026-08-22T00:00:00",
            ),
        ).lastrowid
    )
    conn.commit()
    conn.close()

    migrated = Store(str(legacy_path))
    assert "owner_deleted_at" in {
        row[1] for row in migrated._conn.execute("PRAGMA table_info(bots)")
    }
    assert definitions(migrated) == fresh_definitions
    legacy_inactive = migrated.get_bot(legacy_inactive_id)
    assert legacy_inactive["is_active"] == 0
    assert legacy_inactive["owner_deleted_at"] is None
    _assert_owner_delete_sql_guards(migrated)
    migrated_definitions = definitions(migrated)
    migrated.close()
    # Canonical trigger installation is idempotent on a second migration run.
    reopened = Store(str(legacy_path))
    assert definitions(reopened) == migrated_definitions
    assert reopened._conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    reopened.close()


def test_cross_store_version_mutations_linearize_with_owner_delete(tmp_path):
    for operation in ("add", "activate"):
        path = tmp_path / f"version-race-{operation}.db"
        seed = Store(str(path))
        owner = _user(seed, f"version_{operation}")
        target = _bot(seed, owner, f"version_{operation}", versions=2)
        seed.set_current_version(target["bot_id"], 1)
        verify_rating_projection(seed)
        seed.close()
        mutation_store = Store(str(path))
        delete_store = Store(str(path))
        barrier = threading.Barrier(2)

        def mutate():
            barrier.wait(timeout=5)
            try:
                if operation == "add":
                    mutation_store.add_bot_version(
                        target["bot_id"], binary_path=target["binary_path"]
                    )
                else:
                    mutation_store.set_current_version(target["bot_id"], 2)
                return "mutated"
            except BotDeletedError:
                return "deleted"

        def delete():
            barrier.wait(timeout=5)
            delete_store.owner_delete_bot(target["owner_id"], target["bot_id"])
            return "tombstoned"

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(mutate), pool.submit(delete)]
            outcomes = {future.result(timeout=10) for future in futures}
        assert "tombstoned" in outcomes
        assert outcomes <= {"tombstoned", "mutated", "deleted"}
        bot = delete_store.get_bot(target["bot_id"])
        assert bot["owner_deleted_at"] is not None
        versions = mutation_store.list_bot_versions(target["bot_id"])
        assert int(bot["current_version"]) in {
            int(version["version"]) for version in versions
        }
        mutation_store.close()
        delete_store.close()


def test_initial_upload_delete_after_version_commit_preserves_tombstone_assets(
    tmp_path, monkeypatch
):
    """A staged v1 must never be purged after owner DELETE wins publish race."""

    path = tmp_path / "initial-upload-delete-race.db"
    manager_store = Store(str(path))
    owner = _user(manager_store, "initial_upload_race")
    delete_store = Store(str(path))
    upload_root = tmp_path / "uploads"
    manager = BotManager(manager_store, upload_root=upload_root)
    raw = (
        Path(__file__).resolve().parents[3]
        / "samples"
        / "callbot_linux_amd64"
    ).read_bytes()
    version_written = threading.Event()
    allow_publish = threading.Event()
    original_write = manager._write_version

    def pause_after_version_commit(*args, **kwargs):
        result = original_write(*args, **kwargs)
        version_written.set()
        assert allow_publish.wait(timeout=5)
        return result

    monkeypatch.setattr(manager, "_write_version", pause_after_version_commit)
    with ThreadPoolExecutor(max_workers=1) as pool:
        upload = pool.submit(
            manager.create_from_upload,
            int(owner["id"]),
            "initial_race_bot",
            raw,
        )
        assert version_written.wait(timeout=5)
        try:
            staged = delete_store.get_bot_by_owner_name(
                int(owner["id"]), "initial_race_bot"
            )
            assert staged is not None
            bot_id = int(staged["id"])
            assert staged["is_active"] == 0
            assert staged["current_version"] == 1
            versions_before = delete_store.list_bot_versions(bot_id)
            assert len(versions_before) == 1
            binary = Path(versions_before[0]["binary_path"])
            assert binary.read_bytes() == raw
            deleted = delete_store.owner_delete_bot(int(owner["id"]), bot_id)
            assert deleted["changed"] is True
        finally:
            allow_publish.set()

        with pytest.raises(BotError) as failed:
            upload.result(timeout=10)
    assert failed.value.code == "bot_deleted"

    tombstone = delete_store.get_bot(bot_id)
    assert tombstone is not None
    assert tombstone["owner_deleted_at"] is not None
    assert tombstone["is_active"] == 0
    assert tombstone["is_ranked"] == 0
    versions_after = delete_store.list_bot_versions(bot_id)
    assert versions_after == versions_before
    assert binary.read_bytes() == raw
    repeated = delete_store.owner_delete_bot(int(owner["id"]), bot_id)
    assert repeated["changed"] is False
    assert manager_store.delete_unpublished_bot(bot_id) is False
    with pytest.raises(BotDeletedError):
        manager_store.delete_bot(bot_id)
    assert manager_store.get_bot(bot_id) is not None
    assert manager_store.get_bot_by_owner_name(
        int(owner["id"]), "initial_race_bot"
    )["id"] == bot_id
    assert manager_store.rating_projection_status()["ready"] is True
    assert delete_store.rating_projection_status()["ready"] is True
    manager_store.close()
    delete_store.close()


def test_cross_store_admin_activation_race_never_revives_or_raises_sqlite(
    tmp_path,
):
    path = tmp_path / "admin-delete-race.db"
    seed = Store(str(path))
    owner = _user(seed, "admin_race_owner")
    target = _bot(seed, owner, "admin_race")
    verify_rating_projection(seed)
    seed.close()
    admin_store = Store(str(path))
    delete_store = Store(str(path))
    barrier = threading.Barrier(2)

    def activate():
        barrier.wait(timeout=5)
        try:
            admin_store.update_admin_bot(target["bot_id"], is_active=1)
            return "activated"
        except BotDeletedError:
            return "deleted"

    def delete():
        barrier.wait(timeout=5)
        delete_store.owner_delete_bot(target["owner_id"], target["bot_id"])
        return "tombstoned"

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(activate), pool.submit(delete)]
        outcomes = {future.result(timeout=10) for future in futures}
    assert "tombstoned" in outcomes
    assert outcomes <= {"tombstoned", "activated", "deleted"}
    bot = delete_store.get_bot(target["bot_id"])
    assert bot["owner_deleted_at"] is not None
    assert bot["is_active"] == 0
    admin_store.close()
    delete_store.close()


def _api_fixture(tmp_path):
    app = create_app(db_path=str(tmp_path / "api-owner-delete.db"))
    store = app.state.store
    owner = _user(store, "api_owner")
    other = _user(store, "api_other")
    admin = _user(store, "api_admin", role="admin")
    target = _bot(store, owner, "api", versions=1)
    store.ensure_rating(target["bot_id"], game_id="holdem")
    verify_rating_projection(store)
    tokens = {
        user["username"]: app.state.auth.authenticate(
            user["username"], _PASSWORD
        )[1]
        for user in (owner, other, admin)
    }
    return app, owner, other, admin, target, tokens


def _headers(tokens: dict[str, str], user: dict) -> dict[str, str]:
    return {"Authorization": f"Bearer {tokens[user['username']]}"}


def test_owner_delete_api_projection_versions_admin_and_audit(
    tmp_path, monkeypatch
):
    app, owner, other, admin, target, tokens = _api_fixture(tmp_path)
    audits: list[tuple[str, str, str]] = []

    def record_audit(_request, action, *, result="ok", detail="", **_kwargs):
        audits.append((action, result, detail))

    monkeypatch.setattr("bzplat.backend.api_routes.audit_log", record_audit)
    with TestClient(app) as client:
        response = client.delete(
            f"/api/bots/{target['bot_id']}", headers=_headers(tokens, owner)
        )
        assert response.status_code == 200, response.text
        assert response.json() == {"ok": True, "changed": True}
        repeated = client.delete(
            f"/api/bots/{target['bot_id']}", headers=_headers(tokens, owner)
        )
        assert repeated.status_code == 200, repeated.text
        assert repeated.json() == {"ok": True, "changed": False}
        forbidden = client.delete(
            f"/api/bots/{target['bot_id']}", headers=_headers(tokens, other)
        )
        assert forbidden.status_code == 403, forbidden.text
        assert forbidden.json()["detail"]["code"] == "forbidden"

        for path, key in (
            (f"/api/bots/{target['bot_id']}", "bot"),
            (f"/api/bots/{target['bot_id']}/profile", "profile"),
        ):
            payload = client.get(path).json()[key]
            assert payload["is_deleted"] is True
            assert payload["runnable"] is False
            assert payload["unsupported_reason"] == "Bot 已删除"
            assert "owner_deleted_at" not in payload
            assert "updated_at" not in payload

        mine = client.get(
            "/api/bots/mine?page=1&per_page=20",
            headers=_headers(tokens, owner),
        ).json()
        assert mine["bots"] == [] and mine["total"] == 0
        public = client.get("/api/bots/public?game_id=holdem").json()
        assert target["bot_id"] not in {row["id"] for row in public["bots"]}
        searched = client.get("/api/search?q=bot_api&type=bots").json()
        assert searched["bots"] == []
        user_bots = client.get(f"/api/users/{owner['username']}/bots").json()
        assert user_bots["bots"] == []
        user_profile = client.get(
            f"/api/users/{owner['username']}/profile"
        ).json()["profile"]
        assert user_profile["bot_count"] == 0
        assert user_profile["stats"]["rated_bots"] == 1

        owner_versions = client.get(
            f"/api/bots/{target['bot_id']}/versions",
            headers=_headers(tokens, owner),
        )
        assert owner_versions.status_code == 200
        assert len(owner_versions.json()["versions"]) == 1
        assert owner_versions.json()["versions"][0]["runnable"] is False
        assert (
            owner_versions.json()["versions"][0]["unsupported_reason"]
            == "Bot 已删除"
        )
        other_versions = client.get(
            f"/api/bots/{target['bot_id']}/versions",
            headers=_headers(tokens, other),
        )
        assert other_versions.status_code == 200
        assert other_versions.json()["versions"] == []

        owner_mutations = [
            client.patch(
                f"/api/bots/{target['bot_id']}",
                json={"display_name": "cannot-change"},
                headers=_headers(tokens, owner),
            ),
            client.post(
                f"/api/bots/{target['bot_id']}/active?active=true",
                headers=_headers(tokens, owner),
            ),
            client.put(
                f"/api/bots/{target['bot_id']}/ranking",
                headers=_headers(tokens, owner),
            ),
            client.delete(
                f"/api/bots/{target['bot_id']}/ranking",
                headers=_headers(tokens, owner),
            ),
            client.post(
                f"/api/bots/{target['bot_id']}/versions/1/activate",
                headers=_headers(tokens, owner),
            ),
            client.post(
                f"/api/bots/{target['bot_id']}/versions",
                files={
                    "file": (
                        "deleted-bot.bin",
                        b"rejected-before-classify-or-preflight",
                        "application/octet-stream",
                    )
                },
                headers=_headers(tokens, owner),
            ),
        ]
        for mutation in owner_mutations:
            assert mutation.status_code == 409, mutation.text
            assert mutation.json()["detail"]["code"] == "bot_deleted"

        admin_rows = client.get(
            "/api/admin/bots", headers=_headers(tokens, admin)
        ).json()["bots"]
        admin_row = next(row for row in admin_rows if row["id"] == target["bot_id"])
        assert admin_row["is_deleted"] is True
        assert admin_row["owner_deleted_at"]
        metadata = client.patch(
            f"/api/admin/bots/{target['bot_id']}",
            json={"display_name": "historical"},
            headers=_headers(tokens, admin),
        )
        assert metadata.status_code == 200, metadata.text
        assert metadata.json()["bot"]["owner_deleted_at"]
        activation = client.patch(
            f"/api/admin/bots/{target['bot_id']}",
            json={"is_active": True},
            headers=_headers(tokens, admin),
        )
        assert activation.status_code == 409
        assert activation.json()["detail"]["code"] == "bot_deleted"
        local_agent = client.post(
            "/api/local-ai/agents",
            json={"bot_id": target["bot_id"], "label": "deleted-agent"},
            headers=_headers(tokens, owner),
        )
        assert local_agent.status_code == 409
        assert local_agent.json()["detail"]["code"] == "bot_deleted"

    owner_delete_audits = [row for row in audits if row[0] == "bot_owner_delete"]
    assert any(
        result == "ok" and "changed=1" in detail
        for _action, result, detail in owner_delete_audits
    )
    assert any(
        result == "ok" and "changed=0" in detail
        for _action, result, detail in owner_delete_audits
    )
    assert ("bot_owner_delete", "fail", "forbidden") in owner_delete_audits


def test_owner_delete_route_does_not_block_event_loop(tmp_path, monkeypatch):
    from httpx import ASGITransport, AsyncClient

    app, owner, _other, _admin, target, tokens = _api_fixture(tmp_path)
    entered = threading.Event()
    release = threading.Event()

    def blocking_delete(_bot_id: int, _owner_id: int) -> dict:
        entered.set()
        assert release.wait(timeout=3)
        return {
            "changed": False,
            "cancelled_queued_jobs": 0,
            "invalidated_retryable_jobs": 0,
            "revoked_local_ai_public_ids": [],
        }

    monkeypatch.setattr(app.state.bot_manager, "delete_owner", blocking_delete)

    async def exercise():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            deletion = asyncio.create_task(
                client.delete(
                    f"/api/bots/{target['bot_id']}",
                    headers=_headers(tokens, owner),
                )
            )
            try:
                assert await asyncio.wait_for(
                    asyncio.to_thread(entered.wait, 2), timeout=2.5
                )
                health = await asyncio.wait_for(
                    client.get("/api/health"), timeout=0.5
                )
                assert health.status_code == 200
                assert deletion.done() is False
            finally:
                release.set()
            response = await asyncio.wait_for(deletion, timeout=2)
            assert response.status_code == 200
            assert response.json() == {"ok": True, "changed": False}

    asyncio.run(exercise())


def test_cancelled_owner_delete_drains_transport_revoke_and_audit(
    tmp_path, monkeypatch
):
    from httpx import ASGITransport, AsyncClient

    app, owner, _other, _admin, target, tokens = _api_fixture(tmp_path)
    agent = app.state.store.create_local_ai_agent(
        owner_id=target["owner_id"],
        bot_id=target["bot_id"],
        label="cancel-delete-agent",
        public_id="agent-cancel-owner-delete",
        token_hash=hashlib.sha256(b"cancel-owner-delete").hexdigest(),
        token_hint="cancel",
    )
    entered = threading.Event()
    release = threading.Event()
    original_delete = app.state.bot_manager.delete_owner
    revoked: list[str] = []
    audits: list[tuple[str, str, str]] = []

    def blocking_delete(bot_id: int, owner_id: int) -> dict:
        entered.set()
        assert release.wait(timeout=5)
        return original_delete(bot_id, owner_id)

    async def record_revoke(public_ids: list[str]) -> None:
        revoked.extend(public_ids)

    def record_audit(_request, action, *, result="ok", detail="", **_kwargs):
        audits.append((action, result, detail))

    monkeypatch.setattr(app.state.bot_manager, "delete_owner", blocking_delete)
    monkeypatch.setattr(
        app.state.local_ai_service, "revoke_public_ids", record_revoke
    )
    monkeypatch.setattr("bzplat.backend.api_routes.audit_log", record_audit)

    async def exercise():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            deletion = asyncio.create_task(
                client.delete(
                    f"/api/bots/{target['bot_id']}",
                    headers=_headers(tokens, owner),
                )
            )
            assert await asyncio.wait_for(
                asyncio.to_thread(entered.wait, 2), timeout=2.5
            )
            deletion.cancel()
            try:
                await asyncio.sleep(0)
                assert deletion.done() is False
            finally:
                release.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(deletion, timeout=3)

    asyncio.run(exercise())

    tombstone = app.state.store.get_bot(target["bot_id"])
    assert tombstone["owner_deleted_at"] is not None
    assert app.state.store.get_local_ai_agent(int(agent["id"]))["status"] == "revoked"
    assert revoked == ["agent-cancel-owner-delete"]
    owner_delete_audits = [row for row in audits if row[0] == "bot_owner_delete"]
    assert len(owner_delete_audits) == 1
    assert owner_delete_audits[0][1] == "ok"
    assert "changed=1" in owner_delete_audits[0][2]
