"""赛事 publish/start 的锁内复核与失败补偿回归。"""
from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
from pathlib import Path

import pytest

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.validation import contest_current_stage_index
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.store import Store
from bzplat.backend.store.schema import EXECUTION_SOURCE_CONTEST, TYPE_CONTEST
from bzplat.backend.tests.execution_helpers import (
    claim_request,
    enable_execution_queue,
    queued_execution_jobs,
)


class _SuccessOrch:
    def __init__(self) -> None:
        self.calls = 0

    async def challenge(self, *args, **kwargs):
        self.calls += 1
        return f"match-{self.calls}"


class _FailingOrch:
    async def challenge(self, *args, **kwargs):
        raise RuntimeError("dispatch exploded")


def _fixture_file(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"test fixture")
    return str(path)


def _setup(tmp_path):
    store = Store(str(tmp_path / "contest-atomicity.db"))
    u1 = store.create_user("atomic1", "atomic1@example.com", "hash")
    u2 = store.create_user("atomic2", "atomic2@example.com", "hash")
    b1 = store.create_bot(
        u1["id"], "atomic-bot-1",
        binary_path=_fixture_file(tmp_path, "atomic-1"), format="elf",
        game_id="holdem",
    )
    b2 = store.create_bot(
        u2["id"], "atomic-bot-2",
        binary_path=_fixture_file(tmp_path, "atomic-2"), format="elf",
        game_id="holdem",
    )
    contest = store.create_contest(
        "Atomic",
        organizer_id=u1["id"],
        status="open",
        game_id="holdem",
        template_id="holdem_rr",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    store.add_contest_entry(contest["id"], u1["id"], b1["id"])
    store.add_contest_entry(contest["id"], u2["id"], b2["id"])
    store.update_entry(contest["id"], u1["id"], seed=7, eliminated=1)
    store.update_entry(contest["id"], u2["id"], seed=8, eliminated=0)
    return store, contest["id"]


def _entry_state(store: Store, contest_id: int) -> dict[int, tuple[int, int]]:
    return {
        entry["user_id"]: (int(entry["seed"]), int(entry["eliminated"]))
        for entry in store.list_contest_entries(contest_id)
    }


def _seal_low_level_lifecycle_fixture(
    store: Store,
    contest_id: int,
    *,
    stage_idx: int,
    status: str,
    official_results_ready: int | None = None,
    rest_ends_at: str | None = None,
) -> None:
    """Seal an intentionally low-level imported lifecycle shape for a test.

    Product setup should use Manager publication/transition APIs. A handful of
    corruption/recovery tests must first persist an impossible historical shape;
    this helper gives that shape an exact manifest/revision so the semantic gate
    under test is reached instead of failing earlier as an unsealed active row.
    """
    pairings = store.list_contest_pairings(contest_id, stage_idx=stage_idx)
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status='published',current_stage_idx=?,"
            "published_stage_pairing_count=NULL,"
            "sealed_pairing_topology_revision=NULL WHERE id=?",
            (stage_idx, contest_id),
        )
    store.seal_published_stage_pairing_count(
        contest_id,
        stage_idx,
        expected_count=len(pairings),
        expected_existing_ids=[int(row["id"]) for row in pairings],
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        sets = ["status=?", "current_stage_idx=?"]
        values: list[object] = [status, stage_idx]
        if official_results_ready is not None:
            sets.append("official_results_ready=?")
            values.append(official_results_ready)
        if rest_ends_at is not None:
            sets.append("rest_ends_at=?")
            values.append(rest_ends_at)
        values.append(contest_id)
        connection.execute(
            f"UPDATE contests SET {','.join(sets)} WHERE id=?",
            values,
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest_id,),
        )


@pytest.mark.parametrize("raw", [0.5, -1, "0", True, None])
def test_current_stage_cursor_parser_rejects_coercible_or_missing_values(raw):
    assert (
        contest_current_stage_index(
            {"current_stage_idx": raw}, stage_count=1
        )
        is None
    )
    assert contest_current_stage_index({}, stage_count=1) == 0


@pytest.mark.parametrize("corruption", ["contest_cursor", "pairing_cursor"])
def test_malformed_stage_cursor_blocks_scoring_completion_and_force_finish(
    tmp_path, corruption
):
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    for entry in entries:
        store.update_entry(
            contest_id, entry["user_id"], eliminated=0
        )
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "duplicate": False,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
    }
    store.update_contest(
        contest_id,
        status="published",
        current_stage_idx=0,
        stages_json=json.dumps([stage]),
    )
    pairing = store.add_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        stage_idx=0,
        stage_key="rr",
    )
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="published",
    )
    match_id = f"malformed-stage-{corruption}"
    store.create_match(
        match_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        owner_id=entries[0]["user_id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
        match_config={"duplicate": False},
    )
    store.bind_contest_pairing_match(
        contest_id,
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        reason="completed",
        result={"rounds_played": 70, "deltas": [100, -100]},
    )
    store.complete_contest_pairing_for_match(contest_id, match_id)
    store.update_contest(contest_id, status="running")
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if corruption == "contest_cursor":
            connection.execute(
                "UPDATE contests SET current_stage_idx=0.5 WHERE id=?",
                (contest_id,),
            )
        else:
            connection.execute(
                "UPDATE contest_pairings SET stage_idx=0.5 WHERE id=?",
                (pairing["id"],),
            )

    manager = ContestManager(store, _SuccessOrch())
    if corruption == "contest_cursor":
        assert manager.standings(contest_id) == []
    else:
        assert all(row["points"] == 0 for row in manager.standings(contest_id))
    assert manager._stage_done(contest_id, 0) is False
    assert manager._has_unfinished_pairings(contest_id) is True
    with pytest.raises(ValueError):
        asyncio.run(manager.finish(contest_id))
    asyncio.run(manager.handle_match_done(match_id, contest_id))
    state = store.get_contest(contest_id)
    assert state["status"] == "running"
    assert state["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []
    store.close()


def test_publish_pairing_batch_failure_rolls_back_state_and_rows(
    tmp_path, monkeypatch
):
    """完整批次提交失败不得留下 published 空壳、pairing 或被重写的 seed。"""
    async def exercise():
        store, contest_id = _setup(tmp_path)
        manager = ContestManager(store, _SuccessOrch())
        before = store.get_contest(contest_id)
        before_entries = _entry_state(store, contest_id)

        def fail_batch(*args, **kwargs):
            raise RuntimeError("pairing batch exploded")

        monkeypatch.setattr(store, "create_contest_stage_pairings", fail_batch)
        with pytest.raises(RuntimeError, match="pairing batch exploded"):
            await manager.publish(contest_id)

        after = store.get_contest(contest_id)
        assert after["status"] == before["status"] == "open"
        assert after["registration_closes_at"] == before["registration_closes_at"]
        assert after["starts_at"] == before["starts_at"]
        assert after["stages_json"] == before["stages_json"]
        assert store.list_contest_pairings(contest_id) == []
        assert _entry_state(store, contest_id) == before_entries

    asyncio.run(exercise())


def test_start_first_dispatch_failure_restores_prestart_state_and_pairings(tmp_path):
    """首场 challenge 失败时不得留下 running/published 空壳或未启动对阵。"""
    async def exercise():
        store, contest_id = _setup(tmp_path)
        manager = ContestManager(store, _FailingOrch())
        before = store.get_contest(contest_id)
        before_entries = _entry_state(store, contest_id)

        with pytest.raises(RuntimeError, match="dispatch exploded"):
            await manager.start(contest_id)

        after = store.get_contest(contest_id)
        assert after["status"] == before["status"] == "open"
        assert after["registration_closes_at"] == before["registration_closes_at"]
        assert after["starts_at"] == before["starts_at"]
        assert store.list_contest_pairings(contest_id) == []
        assert _entry_state(store, contest_id) == before_entries

    asyncio.run(exercise())


def test_start_published_dispatch_failure_restores_original_schedule(tmp_path):
    """既有 published 排期首场失败后仍可按原计划重试，不被 start 改成 now。"""
    async def exercise():
        store, contest_id = _setup(tmp_path)
        store.update_contest(contest_id, starts_at="2099-12-31T23:59:59")
        manager = ContestManager(store, _SuccessOrch())
        await manager.publish(contest_id)
        before = store.get_contest(contest_id)
        before_schedules = {
            pairing["id"]: pairing["scheduled_at"]
            for pairing in store.list_contest_pairings(contest_id)
        }
        manager.orch = _FailingOrch()

        with pytest.raises(RuntimeError, match="dispatch exploded"):
            await manager.start(contest_id)

        after = store.get_contest(contest_id)
        assert after["status"] == "published"
        assert after["starts_at"] == before["starts_at"]
        pairings = store.list_contest_pairings(contest_id)
        assert {p["id"]: p["scheduled_at"] for p in pairings} == before_schedules
        assert all(p["status"] == "pending" and p["match_id"] is None for p in pairings)

    asyncio.run(exercise())


def test_start_published_schedule_batch_failure_is_atomic_and_not_dispatchable(
    tmp_path
):
    """The Nth schedule write cannot leave an early-dispatchable prefix."""

    async def exercise():
        store, contest_id = _setup(tmp_path)
        for index in range(2):
            user = store.create_user(
                f"atomic-start-{index}",
                f"atomic-start-{index}@example.com",
                "hash",
            )
            bot = store.create_bot(
                user["id"],
                f"atomic-start-bot-{index}",
                binary_path=_fixture_file(
                    tmp_path, f"atomic-start-bot-{index}"
                ),
                format="elf",
                game_id="holdem",
            )
            store.add_contest_entry(contest_id, user["id"], bot["id"])
        store.update_contest(
            contest_id, starts_at="2099-12-31T23:59:59"
        )
        orch = _SuccessOrch()
        manager = ContestManager(store, orch)
        await manager.publish(contest_id)
        pairings = store.list_contest_pairings(contest_id, stage_idx=0)
        assert len(pairings) == 6
        before_contest = store.get_contest(contest_id)
        before_schedules = {
            int(pairing["id"]): pairing["scheduled_at"]
            for pairing in pairings
        }
        fail_id = int(pairings[1]["id"])
        with store._tx() as connection:
            connection.execute(
                "CREATE TEMP TRIGGER fail_nth_manual_start_schedule "
                "BEFORE UPDATE OF scheduled_at ON contest_pairings "
                f"WHEN OLD.id={fail_id} AND "
                "NEW.scheduled_at IS NOT OLD.scheduled_at "
                "BEGIN SELECT RAISE(ABORT, "
                "'injected manual start schedule failure'); END"
            )

        with pytest.raises(
            sqlite3.DatabaseError,
            match="injected manual start schedule failure",
        ):
            await manager.start(contest_id)

        after = store.get_contest(contest_id)
        assert after["status"] == before_contest["status"] == "published"
        for key in (
            "registration_opens_at",
            "registration_closes_at",
            "starts_at",
            "rest_ends_at",
        ):
            assert after[key] == before_contest[key]
        assert {
            int(pairing["id"]): pairing["scheduled_at"]
            for pairing in store.list_contest_pairings(
                contest_id, stage_idx=0
            )
        } == before_schedules
        assert orch.calls == 0

        await manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
        assert orch.calls == 0
        assert store.list_matches(contest_id=contest_id) == []
        store.close()

    asyncio.run(exercise())


def test_concurrent_publish_rechecks_status_under_single_contest_lock(tmp_path):
    """两个 publish 快照同时到达也只能生成一份对阵。"""
    async def exercise():
        store, contest_id = _setup(tmp_path)
        manager = ContestManager(store, _SuccessOrch())
        results = await asyncio.gather(
            manager.publish(contest_id),
            manager.publish(contest_id),
            return_exceptions=True,
        )

        assert sum(isinstance(result, dict) for result in results) == 1
        errors = [result for result in results if isinstance(result, Exception)]
        assert len(errors) == 1
        assert isinstance(errors[0], ValueError)
        assert store.get_contest(contest_id)["status"] == "published"
        assert len(store.list_contest_pairings(contest_id)) == 1

    asyncio.run(exercise())


def test_pairing_bind_failure_discards_prepared_match_without_orphan(
    tmp_path, monkeypatch
):
    """Atomic claim 的 pairing 写入失败时不留下 match/replay/index 孤儿。"""
    async def exercise():
        store, contest_id = _setup(tmp_path)
        orch = MatchOrchestrator(store)
        manager = ContestManager(store, orch)
        enable_execution_queue(store)
        assert (await manager.start(contest_id))["status"] == "published"
        queued = queued_execution_jobs(orch)
        assert len(queued) == 1
        with store._tx() as conn:
            conn.execute(
                "CREATE TRIGGER fail_pairing_claim BEFORE UPDATE OF match_id "
                "ON contest_pairings WHEN NEW.match_id IS NOT NULL BEGIN "
                "SELECT RAISE(ABORT, 'pairing commit exploded'); END"
            )

        with pytest.raises(sqlite3.IntegrityError, match="pairing commit exploded"):
            claim_request(orch, queued[0]["public_id"], start=False)

        pairing = store.list_contest_pairings(contest_id)
        assert len(pairing) == 1
        assert pairing[0]["status"] == "pending"
        assert pairing[0]["match_id"] is None
        assert store.list_matches(contest_id=contest_id) == []
        assert store.executions.get(queued[0]["public_id"])["status"] == "queued"
        assert orch._tasks == {}

    asyncio.run(exercise())


def test_nth_dispatch_failure_keeps_started_progress_and_failed_pairing_retryable(
    tmp_path, monkeypatch
):
    """第 N 次 claim 失败保留既有进度，其余请求仍 queued 且无孤儿 match。"""
    async def exercise():
        store, contest_id = _setup(tmp_path)
        u3 = store.create_user("atomic3", "atomic3@example.com", "hash")
        b3 = store.create_bot(
            u3["id"], "atomic-bot-3",
            binary_path=_fixture_file(tmp_path, "atomic-3"),
            format="elf", game_id="holdem",
        )
        u4 = store.create_user("atomic4", "atomic4@example.com", "hash")
        b4 = store.create_bot(
            u4["id"], "atomic-bot-4",
            binary_path=_fixture_file(tmp_path, "atomic-4"),
            format="elf", game_id="holdem",
        )
        store.add_contest_entry(contest_id, u3["id"], b3["id"])
        store.add_contest_entry(contest_id, u4["id"], b4["id"])
        orch = MatchOrchestrator(store)
        manager = ContestManager(store, orch)
        enable_execution_queue(store)
        result = await manager.start(contest_id)
        assert result["status"] == "published"

        queued = queued_execution_jobs(orch)
        assert len(queued) == 6
        first = claim_request(orch, queued[0]["public_id"], start=False)
        assert store.get_contest(contest_id)["status"] == "running"
        active_bot_ids = {int(first["bot_a_id"]), int(first["bot_b_id"])}
        failed_request = next(
            request
            for request in queued[1:]
            if active_bot_ids.isdisjoint(
                {int(request["bot_a_id"]), int(request["bot_b_id"])}
            )
        )
        failed_pairing_id = int(failed_request["contest_pairing_id"])
        with store._tx() as conn:
            conn.execute(
                "CREATE TRIGGER fail_second_pairing_claim "
                "BEFORE UPDATE OF match_id ON contest_pairings "
                f"WHEN NEW.id={failed_pairing_id} AND NEW.match_id IS NOT NULL BEGIN "
                "SELECT RAISE(ABORT, 'second pairing commit exploded'); END"
            )
        with pytest.raises(
            sqlite3.IntegrityError, match="second pairing commit exploded"
        ):
            claim_request(orch, failed_request["public_id"], start=False)

        pairings = store.list_contest_pairings(contest_id)
        assert len(pairings) == 6
        bound = [pairing for pairing in pairings if pairing.get("match_id")]
        retryable = [pairing for pairing in pairings if not pairing.get("match_id")]
        assert len(bound) == 1
        assert bound[0]["match_id"] == first["current_match_id"]
        assert len(retryable) == 5
        assert all(pairing["status"] == "pending" for pairing in retryable)
        matches = store.list_matches(contest_id=contest_id)
        assert {match["id"] for match in matches} == {first["current_match_id"]}
        assert all(
            store.executions.get(request["public_id"])["status"] == "queued"
            for request in queued[1:]
        )
        await orch.shutdown()

    asyncio.run(exercise())


def test_cross_stage_begin_requires_immutable_source_decision(tmp_path):
    """跨阶段生成不能绕过来源 decision，也不能覆盖 legacy future 行。"""
    db_path = tmp_path / "next-stage-atomic.db"
    store = Store(str(db_path))
    users = [
        store.create_user(f"stage-user-{i}", f"stage-{i}@example.com", "hash")
        for i in range(3)
    ]
    bots = [
        store.create_bot(
            user["id"],
            f"stage-bot-{i}",
            binary_path=_fixture_file(tmp_path, f"stage-{i}"),
            format="elf",
            game_id="holdem",
        )
        for i, user in enumerate(users)
    ]
    for bot in bots:
        store.add_bot_version(
            bot["id"],
            binary_path=_fixture_file(tmp_path, f"stage-v-{bot['id']}"),
            version=1,
        )
    contest_id = store.create_contest(
        "Atomic next stage",
        organizer_id=users[0]["id"],
        status="rest",
        game_id="holdem",
        current_stage_idx=0,
        stages_json=json.dumps(
            [
                {"key": "qualifier", "type": "round_robin"},
                {"key": "final", "type": "single_elimination"},
            ]
        ),
    )["id"]
    for seed, (user, bot) in enumerate(zip(users, bots), start=1):
        store.add_contest_entry(contest_id, user["id"], bot["id"])
        store.update_entry(contest_id, user["id"], seed=seed, eliminated=0)

    manager = ContestManager(store, _SuccessOrch())
    legacy_partial = store.add_contest_pairing(
        contest_id,
        bots[0]["id"],
        None,
        status="completed",
        stage_idx=1,
        stage_key="final",
        entry_a_id=store.list_contest_entries(contest_id)[0]["id"],
        published_at="2026-01-01T00:00:00",
    )
    before = store.get_contest(contest_id)
    with pytest.raises(ValueError, match="绑定不可变来源决策"):
        asyncio.run(manager._begin_stage(contest_id, 1, dispatch_pending=False))
    assert store.get_contest(contest_id) == before
    assert [
        row["id"]
        for row in store.list_contest_pairings(contest_id, stage_idx=1)
    ] == [legacy_partial["id"]]
    store.close()


@pytest.mark.parametrize("surviving_count", [2, 3])
def test_rest_partial_pairing_and_snapshot_cannot_self_certify_advancement(
    tmp_path, surviving_count
):
    """REST 必须以完整 active roster 校验快照，不能由同一残图缩域自证。"""
    store, contest_id = _setup(tmp_path)
    for index in range(2):
        user = store.create_user(
            f"rest-partial-{index}",
            f"rest-partial-{index}@example.com",
            "hash",
        )
        bot = store.create_bot(
            user["id"],
            f"rest-partial-bot-{index}",
            binary_path=_fixture_file(tmp_path, f"rest-partial-{index}"),
            format="elf",
            game_id="holdem",
        )
        store.add_contest_entry(contest_id, user["id"], bot["id"])
    entries = store.list_contest_entries(contest_id)
    for seed, entry in enumerate(entries, start=1):
        store.update_entry(
            contest_id,
            entry["user_id"],
            seed=seed,
            eliminated=0,
        )
    store.update_contest(
        contest_id,
        status="running",
        current_stage_idx=0,
        stages_json=json.dumps(
            [
                {
                    "key": "qualifier",
                    "type": "round_robin",
                    "advance_count": 2,
                },
                {"key": "final", "type": "single_elimination"},
            ]
        ),
    )
    for left in range(len(entries)):
        for right in range(left + 1, len(entries)):
            match_id = f"rest-partial-{left}-{right}"
            store.create_match(
                match_id,
                entries[left]["bot_id"],
                entries[right]["bot_id"],
                owner_id=entries[left]["user_id"],
                contest_id=contest_id,
                match_type="contest",
                game_id="holdem",
            )
            store.update_match(
                match_id,
                status="completed",
                winner=0,
                result={"deltas": [10, -10]},
            )
            store.add_contest_pairing(
                contest_id,
                entries[left]["bot_id"],
                entries[right]["bot_id"],
                match_id=match_id,
                status="completed",
                stage_idx=0,
                stage_key="qualifier",
                entry_a_id=entries[left]["id"],
                entry_b_id=entries[right]["id"],
            )
    manager = ContestManager(store, _SuccessOrch())
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="running",
    )
    assert manager._stage_done(contest_id, 0) is True
    manager._snapshot_stage_results(contest_id, 0)
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="rest",
        rest_ends_at="2099-01-01T00:00:00",
    )

    surviving_ids = {entry["id"] for entry in entries[:surviving_count]}
    placeholders = ",".join("?" for _ in surviving_ids)
    with store._tx() as connection:
        connection.execute(
            "DELETE FROM contest_pairings WHERE contest_id=? AND "
            f"(entry_a_id NOT IN ({placeholders}) OR "
            f"entry_b_id NOT IN ({placeholders}))",
            (contest_id, *surviving_ids, *surviving_ids),
        )
        connection.execute(
            "DELETE FROM contest_stage_results WHERE contest_id=? "
            f"AND entry_id NOT IN ({placeholders})",
            (contest_id, *surviving_ids),
        )
    def lifecycle_revision() -> tuple[int, int]:
        with store._tx() as connection:
            row = connection.execute(
                "SELECT pairing_topology_revision,"
                "sealed_pairing_topology_revision FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
        assert row is not None
        return (
            int(row["pairing_topology_revision"]),
            int(row["sealed_pairing_topology_revision"]),
        )

    before_revision = lifecycle_revision()
    before_contest = store.get_contest(contest_id)
    before_entries = store.list_contest_entries(contest_id)
    before_pairings = store.list_contest_pairings(contest_id, stage_idx=0)
    before_snapshot = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )

    assert manager._stage_ranking_from_recovery_snapshot(contest_id, 0) is None
    with pytest.raises(ValueError, match="无法验证|不完整"):
        asyncio.run(manager.resume(contest_id))
    assert store.get_contest(contest_id) == before_contest
    assert store.list_contest_entries(contest_id) == before_entries
    assert store.list_contest_pairings(contest_id, stage_idx=0) == before_pairings
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == before_snapshot
    assert store.list_contest_pairings(contest_id, stage_idx=1) == []
    assert lifecycle_revision() == before_revision
    store.close()


def test_cross_stage_recovery_rejects_future_deleted_identity_without_decision(
    tmp_path,
):
    """A valid decision still cannot overwrite a damaged progressed future row."""
    stages = [
        {"key": "rr", "type": "round_robin"},
        {"key": "ko", "type": "single_elimination"},
    ]
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="future-real-opponent",
        stages=stages,
        player_count=2,
    )
    entries = store.list_contest_entries(contest_id)
    ranked_rows, decision_revision, decision_entries, source_groups = (
        manager._ensure_stage_decision(contest_id, 0)
    )
    next_entries, entry_updates = manager._plan_participant_advancement(
        contest_id,
        0,
        ranked_rows=ranked_rows,
    )
    contest = store.get_contest(contest_id)
    stage, specs, bot_to_entry = manager._stage_pairing_plan(
        contest,
        1,
        entry_rows=next_entries,
    )
    replacement_rows = manager._pairing_rows_for_plan(
        contest_id,
        1,
        stage,
        specs,
        bot_to_entry,
        base="2026-01-01T00:00:00",
    )
    store.enter_contest_rest_from_decision(
        contest_id,
        0,
        expected_revision=decision_revision,
        expected_status="running",
        expected_entries=decision_entries,
        expected_stage_groups=source_groups,
        rest_ends_at="2099-01-01T00:00:00",
    )
    damaged = store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        None,
        status="completed",
        stage_idx=1,
        stage_key="ko",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        published_at="2026-01-01T00:00:00",
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest_id,),
        )
        source_revision = int(
            connection.execute(
                "SELECT pairing_topology_revision FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()["pairing_topology_revision"]
        )
    with pytest.raises(ValueError, match="已有运行进度|不能覆盖"):
        store.create_contest_stage_pairings(
            contest_id,
            1,
            replacement_rows,
            expected_current_stage_idx=0,
            expected_status="rest",
            activate_running=True,
            entry_updates=entry_updates,
            source_decision_revision=source_revision,
            source_stage_groups=source_groups,
        )
    persisted = store.list_contest_pairings(contest_id, stage_idx=1)
    assert [row["id"] for row in persisted] == [damaged["id"]]
    assert persisted[0]["entry_b_id"] == entries[1]["id"]
    assert persisted[0]["bot_b_id"] is None
    assert store.get_contest(contest_id)["status"] == "rest"
    store.close()


def test_published_rebuild_does_not_overwrite_deleted_real_opponent(tmp_path):
    """Published recovery uses the same strict no-opponent predicate."""
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    store.update_contest(
        contest_id,
        status="published",
        current_stage_idx=0,
        stages_json=json.dumps([{"key": "swiss", "type": "swiss"}]),
    )
    damaged = store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        status="completed",
        stage_idx=0,
        stage_key="swiss",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    assert store.delete_bot(entries[1]["bot_id"])
    with pytest.raises(ValueError, match="已有运行进度"):
        store.replace_unstarted_contest_stage_pairings(
            contest_id,
            0,
            [
                {
                    "bot_a_id": entries[0]["bot_id"],
                    "bot_b_id": None,
                    "entry_a_id": entries[0]["id"],
                    "entry_b_id": None,
                    "round_num": 1,
                    "status": "completed",
                    "stage_key": "swiss",
                    "published_at": "2026-01-01T00:00:00",
                }
            ],
            expected_existing_ids=[damaged["id"]],
        )
    persisted = store.list_contest_pairings(contest_id, stage_idx=0)
    assert [row["id"] for row in persisted] == [damaged["id"]]
    assert persisted[0]["entry_b_id"] == entries[1]["id"]
    assert persisted[0]["bot_b_id"] is None
    store.close()


def test_published_rebuild_rejects_queued_execution_before_delete(tmp_path):
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "duplicate": False,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
    }
    store.update_contest(
        contest_id,
        status="published",
        current_stage_idx=0,
        stages_json=json.dumps([stage]),
    )
    versions = []
    for entry in entries:
        bot = store.get_bot(entry["bot_id"])
        versions.append(
            store.add_bot_version(
                entry["bot_id"], binary_path=bot["binary_path"]
            )
        )
    pairing = store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        bot_a_version_id=versions[0]["id"],
        bot_b_version_id=versions[1]["id"],
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        stage_idx=0,
        stage_key="rr",
        pairing_seed=12_345,
        published_at="2026-01-01T00:00:00",
    )
    store.seal_published_stage_pairing_count(
        contest_id,
        0,
        expected_count=1,
        expected_existing_ids=[pairing["id"]],
    )
    enable_execution_queue(store)
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=entries[0]["user_id"],
        game_id="holdem",
        match_type=TYPE_CONTEST,
        bot_a_id=entries[0]["bot_id"],
        bot_b_id=entries[1]["bot_id"],
        bot_a_version_id=versions[0]["id"],
        bot_b_version_id=versions[1]["id"],
        contest_id=contest_id,
        contest_pairing_id=pairing["id"],
        match_config={"duplicate": False},
    )

    with pytest.raises(ValueError, match="active 执行请求"):
        store.replace_unstarted_contest_stage_pairings(
            contest_id,
            0,
            [dict(pairing)],
            expected_existing_ids=[pairing["id"]],
        )

    assert [row["id"] for row in store.list_contest_pairings(contest_id)] == [
        pairing["id"]
    ]
    assert store.executions.get(job["public_id"])["status"] == "queued"
    store.close()


@pytest.mark.parametrize(
    ("stage", "player_count", "expected_next_rows"),
    [
        ({"key": "swiss", "type": "swiss", "rounds": 2}, 4, 2),
        ({"key": "ko", "type": "single_elimination"}, 8, 2),
    ],
)
def test_lazy_next_round_batch_failure_rolls_back_and_retry_is_unique(
    tmp_path, stage, player_count, expected_next_rows
):
    """Swiss/KO 后续轮第二行故障须零 partial，重试只落一个完整批次。"""
    store = Store(str(tmp_path / f"lazy-{stage['type']}.db"))
    users = [
        store.create_user(f"lazy-{i}", f"lazy-{i}@example.com", "hash")
        for i in range(player_count)
    ]
    bots = [
        store.create_bot(
            user["id"], f"lazy-bot-{i}",
            binary_path=_fixture_file(tmp_path, f"lazy-{i}"),
            format="elf", game_id="holdem",
        )
        for i, user in enumerate(users)
    ]
    contest_id = store.create_contest(
        f"lazy-{stage['type']}",
        organizer_id=users[0]["id"],
        status="published",
        game_id="holdem",
        current_stage_idx=0,
        stages_json=json.dumps([stage]),
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest_id, user["id"], bot["id"])
    manager = ContestManager(store, _SuccessOrch())

    async def create_and_complete_first_round():
        await manager._begin_stage(
            contest_id,
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
        for pairing in store.list_contest_pairings(contest_id, stage_idx=0):
            if pairing["bot_b_id"] is None:
                continue
            match_id = f"lazy-r1-{pairing['id']}"
            store.create_match(
                match_id,
                pairing["bot_a_id"],
                pairing["bot_b_id"],
                owner_id=users[0]["id"],
                contest_id=contest_id,
                match_type="contest",
                game_id="holdem",
            )
            store.update_match(
                match_id,
                status="completed",
                winner=0,
                result={"deltas": [1, -1]},
            )
            store.update_contest_pairing(
                pairing["id"], match_id=match_id, status="completed"
            )

    asyncio.run(create_and_complete_first_round())
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_second_lazy_round_pairing "
            "BEFORE INSERT ON contest_pairings "
            "WHEN NEW.round_num=2 AND "
            "(SELECT COUNT(*) FROM contest_pairings "
            " WHERE contest_id=NEW.contest_id AND stage_idx=NEW.stage_idx "
            " AND round_num=2)=1 "
            "BEGIN SELECT RAISE(ABORT, 'injected lazy round failure'); END"
        )

    async def advance_once():
        if stage["type"] == "swiss":
            return await manager._maybe_next_swiss_round(contest_id, 0, stage)
        return await manager._maybe_next_elim_round(contest_id, 0, stage)

    with pytest.raises(sqlite3.DatabaseError, match="injected lazy round failure"):
        asyncio.run(advance_once())
    assert [
        pairing for pairing in store.list_contest_pairings(contest_id, stage_idx=0)
        if pairing["round_num"] == 2
    ] == []

    with store._tx() as connection:
        connection.execute("DROP TRIGGER fail_second_lazy_round_pairing")
    advanced = asyncio.run(advance_once())
    if stage["type"] == "swiss":
        assert advanced is True
    else:
        assert advanced == "created"
    next_round = [
        pairing for pairing in store.list_contest_pairings(contest_id, stage_idx=0)
        if pairing["round_num"] == 2
    ]
    assert len(next_round) == expected_next_rows

    # A stale retry carrying the old expected max round is rejected under the
    # same BEGIN IMMEDIATE lock instead of duplicating round 2.
    with pytest.raises(ValueError, match="上一轮已变化"):
        store.append_contest_round_pairings(
            contest_id,
            0,
            next_round,
            expected_current_stage_idx=0,
            expected_previous_max_round=1,
        )
    assert len([
        pairing for pairing in store.list_contest_pairings(contest_id, stage_idx=0)
        if pairing["round_num"] == 2
    ]) == expected_next_rows
    store.close()


def _materialized_stage_fixture(
    tmp_path: Path,
    *,
    label: str,
    stages: list[dict],
    player_count: int = 4,
) -> tuple[Store, int, ContestManager]:
    """Create one isolated sealed running stage and settle every row."""
    store = Store(str(tmp_path / f"terminal-topology-{label}.db"))
    users = [
        store.create_user(
            f"terminal-{label}-{index}",
            f"terminal-{label}-{index}@example.com",
            "hash",
        )
        for index in range(player_count)
    ]
    bots = [
        store.create_bot(
            user["id"],
            f"terminal-{label}-bot-{index}",
            binary_path=_fixture_file(tmp_path, f"terminal-{label}-{index}"),
            format="elf",
            game_id="holdem",
        )
        for index, user in enumerate(users)
    ]
    contest_id = store.create_contest(
        f"terminal topology {label}",
        organizer_id=users[0]["id"],
        status="published",
        game_id="holdem",
        current_stage_idx=0,
        stages_json=json.dumps(stages),
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest_id, user["id"], bot["id"])
    manager = ContestManager(store, _SuccessOrch())
    asyncio.run(
        manager._begin_stage(
            contest_id,
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    for pairing in store.list_contest_pairings(contest_id, stage_idx=0):
        if pairing["bot_b_id"] is None:
            continue
        match_id = f"terminal-{label}-{pairing['id']}"
        store.create_match(
            match_id,
            pairing["bot_a_id"],
            pairing["bot_b_id"],
            owner_id=users[0]["id"],
            contest_id=contest_id,
            match_type="contest",
            game_id="holdem",
        )
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            result={"deltas": [1, -1]},
        )
        store.update_contest_pairing(
            pairing["id"], match_id=match_id, status="completed"
        )
    return store, contest_id, manager


def _partial_historical_stage_fixture(
    tmp_path: Path, *, label: str
) -> tuple[Store, int, ContestManager]:
    """Build a full current stage behind a self-consistent partial history."""
    store = Store(str(tmp_path / f"partial-history-{label}.db"))
    users = [
        store.create_user(
            f"partial-history-{label}-{index}",
            f"partial-history-{label}-{index}@example.com",
            "hash",
        )
        for index in range(4)
    ]
    bots = [
        store.create_bot(
            user["id"],
            f"partial-history-{label}-bot-{index}",
            binary_path=_fixture_file(
                tmp_path, f"partial-history-{label}-{index}"
            ),
            format="elf",
            game_id="holdem",
        )
        for index, user in enumerate(users)
    ]
    stages = [
        {"key": "qualifier", "type": "round_robin"},
        {"key": "final", "type": "round_robin"},
    ]
    contest_id = store.create_contest(
        f"partial historical topology {label}",
        organizer_id=users[0]["id"],
        status="running",
        game_id="holdem",
        current_stage_idx=1,
        stages_json=json.dumps(stages),
    )["id"]
    entries = [
        store.add_contest_entry(contest_id, user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]

    def add_completed_pairing(
        stage_idx: int, stage_key: str, left: int, right: int
    ) -> None:
        match_id = f"partial-history-{label}-{stage_idx}-{left}-{right}"
        store.create_match(
            match_id,
            bots[left]["id"],
            bots[right]["id"],
            owner_id=users[0]["id"],
            contest_id=contest_id,
            match_type="contest",
            game_id="holdem",
        )
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            result={"deltas": [1, -1]},
        )
        store.add_contest_pairing(
            contest_id,
            bots[left]["id"],
            bots[right]["id"],
            match_id=match_id,
            status="completed",
            stage_idx=stage_idx,
            stage_key=stage_key,
            entry_a_id=entries[left]["id"],
            entry_b_id=entries[right]["id"],
        )

    # Damaged legacy history: both the surviving RR edge and its snapshot name
    # only entries 0/1.  Neither artifact may shrink the expected four-player
    # cohort or certify the other as complete.
    add_completed_pairing(0, "qualifier", 0, 1)
    for rank, index in enumerate((0, 1), start=1):
        store.upsert_stage_result(
            contest_id,
            0,
            entries[index]["id"],
            bot_id=bots[index]["id"],
            stage_key="qualifier",
            rank_in_group=rank,
            payload_json=json.dumps(
                {
                    "overall_rank": rank,
                    "tiebreaks": {
                        "points": 0,
                        "buchholz": 0,
                        "buchholz_cut1": 0,
                        "sonneborn_berger": 0,
                        "head_to_head": 0,
                        "normalized_delta": 0,
                        "technical_losses": 0,
                        "seed": rank,
                    },
                }
            ),
        )

    # The current stage is genuinely complete for all four entrants.  Without
    # an external historical cohort authority, the partial qualifier would be
    # ignored and this final alone could publish a plausible full official list.
    for left in range(4):
        for right in range(left + 1, 4):
            add_completed_pairing(1, "final", left, right)
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=1,
        status="running",
    )
    return store, contest_id, ContestManager(store, _SuccessOrch())


@pytest.mark.parametrize("path", ["force", "finished-recovery"])
def test_terminal_paths_reject_partial_historical_pairing_and_snapshot_cohort(
    tmp_path, path
):
    """A partial historical graph cannot derive its cohort from itself."""
    store, contest_id, manager = _partial_historical_stage_fixture(
        tmp_path, label=path
    )
    before_pairings = store.list_contest_pairings(contest_id)
    before_snapshot = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )

    assert manager._stage_ranking_from_recovery_snapshot(contest_id, 0) is None
    assert manager._has_unfinished_pairings(contest_id) is True
    if path == "force":
        with pytest.raises(ValueError, match="未完成对阵|批次完整性"):
            asyncio.run(manager.finish(contest_id))
        assert store.get_contest(contest_id)["status"] == "running"
    else:
        _seal_low_level_lifecycle_fixture(
            store,
            contest_id,
            stage_idx=1,
            status="finished",
            official_results_ready=0,
        )
        asyncio.run(
            manager.reconcile_running_contests(
                interruption_reason="orphan_after_service_restart"
            )
        )
        recovered = store.get_contest(contest_id)
        assert recovered["status"] == "finished"
        assert recovered["official_results_ready"] == 0

    assert store.list_contest_pairings(contest_id) == before_pairings
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == before_snapshot
    assert store.list_stage_results(contest_id, stage_idx=1) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def test_running_dispatch_rejects_partial_no_shrink_predecessor(
    tmp_path, monkeypatch
):
    """A forged later-stage batch cannot run before its predecessor is proven."""
    store, contest_id, manager = _partial_historical_stage_fixture(
        tmp_path, label="dispatch"
    )
    for pairing in store.list_contest_pairings(contest_id, stage_idx=1):
        store.update_contest_pairing(
            pairing["id"], match_id=None, status="pending"
        )
    before_contest = store.get_contest(contest_id)
    before_entries = store.list_contest_entries(contest_id)
    before_pairings = store.list_contest_pairings(contest_id)
    before_matches = store.list_matches(contest_id=contest_id)
    prepare_calls: list[int] = []

    async def unexpected_prepare(_contest, pairing, **_kwargs):
        prepare_calls.append(int(pairing["id"]))
        raise AssertionError("unproved current stage reached match preparation")

    monkeypatch.setattr(
        manager, "_prepare_bind_start_pairing", unexpected_prepare
    )

    asyncio.run(manager._dispatch_pending_locked(contest_id, 1))

    assert prepare_calls == []
    assert manager.standings(contest_id, stage_idx=1) == []
    assert store.get_contest(contest_id) == before_contest
    assert store.list_contest_entries(contest_id) == before_entries
    assert store.list_contest_pairings(contest_id) == before_pairings
    assert store.list_matches(contest_id=contest_id) == before_matches
    assert store.list_stage_results(contest_id, stage_idx=1) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def _sealed_pending_current_fixture(
    tmp_path: Path, *, label: str
) -> tuple[Store, int, ContestManager, list[dict], list[dict]]:
    store = Store(str(tmp_path / f"sealed-pending-{label}.db"))
    users = [
        store.create_user(
            f"sealed-pending-{label}-{index}",
            f"sealed-pending-{label}-{index}@example.com",
            "hash",
        )
        for index in range(2)
    ]
    bots = [
        store.create_bot(
            user["id"],
            f"sealed-pending-{label}-bot-{index}",
            binary_path=_fixture_file(tmp_path, f"sealed-pending-{label}-{index}"),
            format="elf",
            game_id="holdem",
        )
        for index, user in enumerate(users)
    ]
    stages = [
        {"key": "current", "type": "round_robin", "scoring": "poker_3_1_0"},
        {"key": "future", "type": "round_robin", "scoring": "poker_3_1_0"},
    ]
    contest_id = store.create_contest(
        f"sealed pending {label}",
        organizer_id=users[0]["id"],
        status="published",
        game_id="holdem",
        current_stage_idx=0,
        stages_json=json.dumps(stages),
    )["id"]
    entries = [
        store.add_contest_entry(contest_id, user["id"], bot["id"])
        for user, bot in zip(users, bots, strict=True)
    ]
    manager = ContestManager(store, _SuccessOrch())
    asyncio.run(manager._begin_stage(contest_id, 0, dispatch_pending=False))
    current = store.get_contest(contest_id)
    assert current["published_stage_pairing_count"] == 1
    return store, contest_id, manager, entries, bots


def _insert_stage_result_fixture_row(
    store: Store, contest_id: int, stage_idx: int, row: dict
) -> None:
    with store._tx() as connection:
        connection.execute(
            "INSERT INTO contest_stage_results("
            "contest_id,stage_idx,stage_key,entry_id,bot_id,points,wins,draws,"
            "losses,delta_total,group_id,rank_in_group,payload_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                contest_id,
                stage_idx,
                row.get("stage_key") or f"stage{stage_idx}",
                row["entry_id"],
                row.get("bot_id"),
                row.get("points", 0),
                row.get("wins", 0),
                row.get("draws", 0),
                row.get("losses", 0),
                row.get("delta_total", 0),
                row.get("group_id", ""),
                row.get("rank_in_group"),
                row.get("payload_json", "{}"),
            ),
        )


@pytest.mark.parametrize(
    "corruption",
    ["future-pairing", "future-result", "current-partial", "current-exact"],
)
def test_normal_and_safe_dispatch_reject_future_or_current_decision_drift(
    tmp_path, monkeypatch, corruption
):
    """Every dispatch entrypoint consumes the same sealed current authority."""
    store, contest_id, manager, entries, bots = _sealed_pending_current_fixture(
        tmp_path, label=corruption
    )
    snapshot_rows, expected_entries, expected_groups, _ranked = (
        manager._build_stage_result_rows(contest_id, 0)
    )
    if corruption == "future-pairing":
        store.add_pairing(
            contest_id,
            bots[0]["id"],
            bots[1]["id"],
            entry_a_id=entries[0]["id"],
            entry_b_id=entries[1]["id"],
            stage_idx=1,
            stage_key="future",
        )
    elif corruption == "future-result":
        _insert_stage_result_fixture_row(store, contest_id, 1, snapshot_rows[0])
    elif corruption == "current-partial":
        _insert_stage_result_fixture_row(store, contest_id, 0, snapshot_rows[0])
    else:
        for row in snapshot_rows:
            _insert_stage_result_fixture_row(store, contest_id, 0, row)
        with store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=?",
                (contest_id,),
            )

    before = {
        "contest": store.get_contest(contest_id),
        "entries": store.list_contest_entries(contest_id),
        "pairings": store.list_contest_pairings(contest_id),
        "matches": store.list_matches(contest_id=contest_id),
        "results": store.list_stage_result_recovery_snapshots(
            contest_id, stage_idx=0
        ),
    }
    prepare_calls: list[int] = []

    async def unexpected_prepare(_contest, pairing, **_kwargs):
        prepare_calls.append(int(pairing["id"]))
        raise AssertionError("invalid current authority reached match preparation")

    monkeypatch.setattr(
        manager, "_prepare_bind_start_pairing", unexpected_prepare
    )
    asyncio.run(manager._dispatch_pending_locked(contest_id, 0))
    asyncio.run(manager._dispatch_pending_safe_locked(contest_id, 0))

    assert prepare_calls == []
    assert store.get_contest(contest_id) == before["contest"]
    assert store.list_contest_entries(contest_id) == before["entries"]
    assert store.list_contest_pairings(contest_id) == before["pairings"]
    assert store.list_matches(contest_id=contest_id) == before["matches"]
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == before["results"]
    with store._tx() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM execution_jobs WHERE contest_id=?",
            (contest_id,),
        ).fetchone()[0] == 0
    store.close()


def test_active_current_authority_is_fixed_four_select_snapshot_without_match_reads(
    tmp_path, monkeypatch
):
    store, contest_id, manager, _entries, _bots = _sealed_pending_current_fixture(
        tmp_path, label="query-count"
    )
    contest = store.get_contest(contest_id)
    stages = json.loads(contest["stages_json"])

    def unexpected_get_match(*_args, **_kwargs):
        raise AssertionError("authority proof performed an N+1 Match read")

    monkeypatch.setattr(store, "get_match", unexpected_get_match)
    traced: list[str] = []
    store._conn.set_trace_callback(traced.append)
    try:
        authority = manager._active_current_stage_authority(contest, stages)
    finally:
        store._conn.set_trace_callback(None)

    assert authority is not None
    selects = [sql for sql in traced if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 4, selects
    assert any("FROM contests" in sql for sql in selects)
    assert any("FROM contest_pairings" in sql for sql in selects)
    assert any("FROM contest_entries" in sql for sql in selects)
    assert any("FROM contest_stage_results" in sql for sql in selects)
    store.close()


def _wrong_advanced_current_stage_fixture(
    tmp_path: Path, *, label: str
) -> tuple[Store, int, ContestManager]:
    """Persist a valid qualifier, then forge the opposite finalist cohort."""
    qualifier = {
        "key": "qualifier",
        "type": "round_robin",
        "advance_count": 2,
    }
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label=f"wrong-advance-{label}",
        stages=[qualifier],
        player_count=4,
    )
    snapshot_rows, expected_entries, expected_groups, _ranked = (
        manager._build_stage_result_rows(contest_id, 0)
    )
    store.replace_stage_results(
        contest_id,
        0,
        snapshot_rows,
        expected_entries=expected_entries,
        expected_stage_groups=expected_groups,
    )
    qualifier_snapshot = sorted(
        store.list_stage_result_recovery_snapshots(
            contest_id, stage_idx=0
        ),
        key=lambda row: row["rank_in_group"],
    )
    correct_finalists = {
        int(row["entry_id"]) for row in qualifier_snapshot[:2]
    }
    wrong_finalists = {
        int(row["entry_id"]) for row in qualifier_snapshot[2:]
    }
    assert correct_finalists.isdisjoint(wrong_finalists)

    entries = store.list_contest_entries(contest_id)
    by_id = {int(entry["id"]): entry for entry in entries}
    for entry in entries:
        entry_id = int(entry["id"])
        store.update_entry(
            contest_id,
            entry["user_id"],
            eliminated=0 if entry_id in wrong_finalists else 1,
        )
    final = {
        "key": "final",
        "type": "round_robin",
        "ranking_mode": "replace_top",
        "ranking_scope": 2,
    }
    store.update_contest(
        contest_id,
        current_stage_idx=1,
        stages_json=json.dumps([qualifier, final]),
        published_stage_pairing_count=None,
    )
    left_id, right_id = sorted(wrong_finalists)
    left, right = by_id[left_id], by_id[right_id]
    match_id = f"wrong-advance-final-{label}"
    store.create_match(
        match_id,
        left["bot_id"],
        right["bot_id"],
        owner_id=left["user_id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        result={"deltas": [1, -1]},
    )
    store.add_contest_pairing(
        contest_id,
        left["bot_id"],
        right["bot_id"],
        match_id=match_id,
        status="completed",
        stage_idx=1,
        stage_key="final",
        entry_a_id=left_id,
        entry_b_id=right_id,
    )
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=1,
        status="running",
    )
    return store, contest_id, manager


@pytest.mark.parametrize("path", ["force", "finished-recovery"])
def test_terminal_paths_reject_current_cohort_that_disagrees_with_advancement(
    tmp_path, path
):
    """Active flags must equal the previous snapshot's exact Top-N decision."""
    store, contest_id, manager = _wrong_advanced_current_stage_fixture(
        tmp_path, label=path
    )
    before_pairings = store.list_contest_pairings(contest_id)
    before_snapshots = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )

    assert manager._stage_ranking_from_recovery_snapshot(contest_id, 1) is None
    assert manager._has_unfinished_pairings(contest_id) is True
    if path == "force":
        with pytest.raises(ValueError, match="未完成对阵|批次完整性"):
            asyncio.run(manager.finish(contest_id))
        assert store.get_contest(contest_id)["status"] == "running"
    else:
        _seal_low_level_lifecycle_fixture(
            store,
            contest_id,
            stage_idx=1,
            status="finished",
            official_results_ready=0,
        )
        asyncio.run(
            manager.reconcile_running_contests(
                interruption_reason="orphan_after_service_restart"
            )
        )
        recovered = store.get_contest(contest_id)
        assert recovered["status"] == "finished"
        assert recovered["official_results_ready"] == 0

    assert store.list_contest_pairings(contest_id) == before_pairings
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == before_snapshots
    assert store.list_stage_results(contest_id, stage_idx=1) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def test_single_stage_terminal_rejects_active_subset_without_shrink_contract(
    tmp_path,
):
    """A forged eliminated subset cannot redefine a single-stage RR cohort."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="single-stage-active-subset",
        stages=[{"key": "rr", "type": "round_robin"}],
        player_count=4,
    )
    pairings = store.list_contest_pairings(contest_id, stage_idx=0)
    survivor = pairings[0]
    survivor_ids = {
        int(survivor["entry_a_id"]), int(survivor["entry_b_id"])
    }
    for entry in store.list_contest_entries(contest_id):
        store.update_entry(
            contest_id,
            entry["user_id"],
            eliminated=0 if int(entry["id"]) in survivor_ids else 1,
        )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM contest_pairings WHERE contest_id=? AND id<>?",
            (contest_id, survivor["id"]),
        )
        connection.execute(
            "UPDATE contests SET published_stage_pairing_count=NULL WHERE id=?",
            (contest_id,),
        )

    assert manager._stage_ranking_from_recovery_snapshot(contest_id, 0) is None
    assert manager._has_unfinished_pairings(contest_id) is True
    with pytest.raises(ValueError, match="未完成对阵|批次完整性"):
        asyncio.run(manager.finish(contest_id))
    assert store.get_contest(contest_id)["status"] == "running"
    assert store.list_stage_results(contest_id) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def _rest_bot_swap_final_fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    label: str,
    swap_eliminated: bool = True,
) -> tuple[Store, int, ContestManager, int, int]:
    """Reach a two-player final after one atomic rest-window Bot swap."""
    store = Store(str(tmp_path / f"rest-bot-swap-{label}.db"))
    users = [
        store.create_user(
            f"rest-bot-swap-{label}-{index}",
            f"rest-bot-swap-{label}-{index}@example.com",
            "hash",
        )
        for index in range(3)
    ]
    bots = [
        store.create_bot(
            user["id"],
            f"rest-bot-swap-{label}-bot-{index}",
            binary_path=_fixture_file(
                tmp_path, f"rest-bot-swap-{label}-{index}"
            ),
            format="elf",
            game_id="holdem",
        )
        for index, user in enumerate(users)
    ]
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "advance_count": 2,
            "rest_after_minutes": 60,
        },
        {
            "key": "final",
            "type": "round_robin",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
        },
    ]
    contest_id = store.create_contest(
        f"rest bot swap {label}",
        organizer_id=users[0]["id"],
        status="published",
        game_id="holdem",
        current_stage_idx=0,
        stages_json=json.dumps(stages),
    )["id"]
    for seed, (user, bot) in enumerate(zip(users, bots), start=1):
        store.add_contest_entry(contest_id, user["id"], bot["id"])
        store.update_entry(
            contest_id, user["id"], seed=seed, eliminated=0
        )
    manager = ContestManager(store, _SuccessOrch())
    asyncio.run(
        manager._begin_stage(
            contest_id,
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )

    def settle_stage(stage_idx: int) -> None:
        for pairing in store.list_contest_pairings(
            contest_id, stage_idx=stage_idx
        ):
            if pairing["bot_b_id"] is None:
                continue
            match_id = f"rest-bot-swap-{label}-{stage_idx}-{pairing['id']}"
            store.create_match(
                match_id,
                pairing["bot_a_id"],
                pairing["bot_b_id"],
                owner_id=users[0]["id"],
                contest_id=contest_id,
                match_type="contest",
                game_id="holdem",
            )
            store.update_match(
                match_id,
                status="completed",
                winner=None,
                result={"deltas": [0, 0]},
            )
            store.update_contest_pairing(
                pairing["id"], match_id=match_id, status="completed"
            )

    settle_stage(0)
    assert manager._stage_done(contest_id, 0) is True
    rested = asyncio.run(manager.maybe_finish(contest_id))
    assert rested["status"] == "rest"
    qualifier_snapshot = sorted(
        store.list_stage_result_recovery_snapshots(
            contest_id, stage_idx=0
        ),
        key=lambda row: row["rank_in_group"],
    )
    assert len(qualifier_snapshot) == 3
    swapped_entry_id = int(
        qualifier_snapshot[-1 if swap_eliminated else 0]["entry_id"]
    )
    swapped_entry = next(
        entry
        for entry in store.list_contest_entries(contest_id)
        if int(entry["id"]) == swapped_entry_id
    )
    replacement = store.create_bot(
        swapped_entry["user_id"],
        f"rest-bot-swap-{label}-replacement",
        binary_path=_fixture_file(
            tmp_path, f"rest-bot-swap-{label}-replacement"
        ),
        format="elf",
        game_id="holdem",
    )
    store.add_bot_version(
        replacement["id"],
        binary_path=_fixture_file(
            tmp_path, f"rest-bot-swap-{label}-replacement-v1"
        ),
        version=1,
    )
    asyncio.run(
        manager.dispatch(
            contest_id,
            swapped_entry["user_id"],
            replacement["id"],
        )
    )

    async def no_dispatch(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr(manager, "_dispatch_pending_locked", no_dispatch)
    asyncio.run(manager.resume(contest_id))
    assert store.get_contest(contest_id)["current_stage_idx"] == 1
    current_entries = store.list_contest_entries(contest_id)
    assert {
        int(entry["id"])
        for entry in current_entries
        if int(entry["eliminated"]) == 0
    } == {
        int(row["entry_id"]) for row in qualifier_snapshot[:2]
    }
    settle_stage(1)
    return (
        store,
        contest_id,
        manager,
        swapped_entry_id,
        int(replacement["id"]),
    )


def test_rest_bot_swap_resume_freezes_replacement_current_version(
    tmp_path, monkeypatch
):
    """A survivor swapped at rest enters the next stage with its current version."""
    store, contest_id, _manager, swapped_entry_id, replacement_bot_id = (
        _rest_bot_swap_final_fixture(
            tmp_path,
            monkeypatch,
            label="survivor-version",
            swap_eliminated=False,
        )
    )
    replacement_version = store.get_current_bot_version(replacement_bot_id)
    assert replacement_version is not None
    affected = [
        pairing
        for pairing in store.list_contest_pairings(contest_id, stage_idx=1)
        if swapped_entry_id in (pairing["entry_a_id"], pairing["entry_b_id"])
    ]
    assert affected
    for pairing in affected:
        if pairing["entry_a_id"] == swapped_entry_id:
            assert pairing["bot_a_id"] == replacement_bot_id
            assert pairing["bot_a_version_id"] == replacement_version["id"]
        else:
            assert pairing["bot_b_id"] == replacement_bot_id
            assert pairing["bot_b_version_id"] == replacement_version["id"]
    store.close()


@pytest.mark.parametrize("path", ["automatic", "force", "finished-recovery"])
def test_terminal_paths_rebind_rest_swapped_eliminated_bot_in_official_table(
    tmp_path, monkeypatch, path
):
    """Historical scores survive a rest swap; official identity follows roster."""
    store, contest_id, manager, eliminated_entry_id, replacement_bot_id = (
        _rest_bot_swap_final_fixture(tmp_path, monkeypatch, label=path)
    )
    if path == "automatic":
        result = asyncio.run(manager.maybe_finish(contest_id))
        assert result is not None and result["status"] == "finished"
    elif path == "force":
        result = asyncio.run(manager.finish(contest_id))
        assert result["status"] == "finished"
    else:
        manager._ensure_stage_decision(contest_id, 1)
        # Model the only supported legacy recovery input: a complete immutable
        # stage decision behind an exact finished lifecycle seal, but no
        # official table yet.  Generic active-status writers intentionally do
        # not manufacture this state.
        with store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE contests SET status='finished',"
                "official_results_ready=0 WHERE id=?",
                (contest_id,),
            )
            connection.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=?",
                (contest_id,),
            )
        asyncio.run(
            manager.reconcile_running_contests(
                interruption_reason="orphan_after_service_restart"
            )
        )

    finished = store.get_contest(contest_id)
    assert finished["status"] == "finished"
    assert finished["official_results_ready"] == 1
    official = store.list_official_results(contest_id)
    assert len(official) == 3
    eliminated_official = next(
        row for row in official if int(row["entry_id"]) == eliminated_entry_id
    )
    assert int(eliminated_official["bot_id"]) == replacement_bot_id
    assert eliminated_official["rank"] == 3
    qualifier_snapshot = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )
    historical = next(
        row
        for row in qualifier_snapshot
        if int(row["entry_id"]) == eliminated_entry_id
    )
    assert int(historical["bot_id"]) != replacement_bot_id
    store.close()


@pytest.mark.parametrize("historical_drift", ["match-status", "binding"])
def test_finished_recovery_rechecks_historical_settlement_in_official_tx(
    tmp_path, monkeypatch, historical_drift
):
    """A prior-stage drift after the outer gate cannot publish ready=1."""
    store, contest_id, manager, _entry_id, _bot_id = (
        _rest_bot_swap_final_fixture(
            tmp_path,
            monkeypatch,
            label=f"historical-{historical_drift}",
        )
    )
    manager._ensure_stage_decision(contest_id, 1)
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status='finished',official_results_ready=0 "
            "WHERE id=?",
            (contest_id,),
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest_id,),
        )
    stage_zero_pairing = next(
        row
        for row in store.list_contest_pairings(contest_id, stage_idx=0)
        if row.get("match_id") is not None
    )
    stage_zero_match_id = str(stage_zero_pairing["match_id"])
    snapshot_before = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=1
    )
    original_recover = store.recover_finished_contest_official_results

    def drift_after_outer_gate(*args, **kwargs):
        if historical_drift == "match-status":
            store.update_match(stage_zero_match_id, status="pending")
        else:
            store.update_contest_pairing(
                stage_zero_pairing["id"], match_id=None, status="completed"
            )
        return original_recover(*args, **kwargs)

    monkeypatch.setattr(
        store,
        "recover_finished_contest_official_results",
        drift_after_outer_gate,
    )
    asyncio.run(manager._reconcile_one(contest_id))

    state = store.get_contest(contest_id)
    assert state["status"] == "finished"
    assert state["official_results_ready"] == 0
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=1
    ) == snapshot_before
    assert store.list_official_results(contest_id) == []
    store.close()


def test_rest_bot_swap_reseal_failure_rolls_back_entry_pairings_and_revision(
    tmp_path
):
    """Bot/roster mutation and lifecycle reseal are one atomic write."""
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "advance_count": 2,
            "rest_after_minutes": 60,
        },
        {
            "key": "final",
            "type": "round_robin",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
        },
    ]
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="rest-swap-reseal-rollback",
        stages=stages,
        player_count=3,
    )
    rested = asyncio.run(manager.maybe_finish(contest_id))
    assert rested is not None and rested["status"] == "rest"
    entry = store.list_contest_entries(contest_id)[0]
    replacement = store.create_bot(
        entry["user_id"],
        "rest-swap-reseal-rollback-bot",
        binary_path=_fixture_file(tmp_path, "rest-swap-reseal-rollback-bot"),
        format="elf",
        game_id="holdem",
    )
    before_contest = store.get_contest(contest_id)
    before_entries = store.list_contest_entries(contest_id)
    before_pairings = store.list_contest_pairings(contest_id)

    def lifecycle_revision() -> tuple[int, int]:
        with store._tx() as connection:
            row = connection.execute(
                "SELECT pairing_topology_revision,"
                "sealed_pairing_topology_revision FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
        assert row is not None
        return (
            int(row["pairing_topology_revision"]),
            int(row["sealed_pairing_topology_revision"]),
        )

    before_revision = lifecycle_revision()
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_rest_bot_swap_reseal "
            "BEFORE UPDATE OF sealed_pairing_topology_revision ON contests "
            "WHEN NEW.id=OLD.id AND "
            "NEW.sealed_pairing_topology_revision "
            "IS NOT OLD.sealed_pairing_topology_revision "
            "BEGIN SELECT RAISE(ABORT, 'injected bot swap reseal failure'); END"
        )

    with pytest.raises(sqlite3.DatabaseError, match="injected bot swap reseal failure"):
        asyncio.run(
            manager.dispatch(
                contest_id,
                entry["user_id"],
                replacement["id"],
            )
        )

    assert lifecycle_revision() == before_revision
    assert store.list_contest_entries(contest_id) == before_entries
    assert store.list_contest_pairings(contest_id) == before_pairings
    store.close()


@pytest.mark.parametrize(
    ("label", "stage"),
    [
        ("swiss-prefix", {"key": "swiss", "type": "swiss", "rounds": 3}),
        ("ko-prefix", {"key": "ko", "type": "single_elimination"}),
    ],
)
def test_force_and_recovery_reject_settled_incomplete_terminal_topology(
    tmp_path, label, stage
):
    """Settled Swiss R1 / KO semifinals are not a complete terminal stage."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path, label=label, stages=[stage]
    )
    before_pairings = store.list_contest_pairings(contest_id, stage_idx=0)

    with pytest.raises(ValueError, match="未完成对阵|批次完整性"):
        asyncio.run(manager.finish(contest_id))
    assert store.get_contest(contest_id)["status"] == "running"
    assert store.list_stage_results(contest_id, stage_idx=0) == []
    assert store.list_official_results(contest_id) == []
    assert store.list_contest_pairings(contest_id, stage_idx=0) == before_pairings

    # A premature terminal row from an older process must not bypass the same
    # full-topology proof during finished-unready recovery.
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="finished",
        official_results_ready=0,
    )
    asyncio.run(
        manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    )
    assert store.get_contest(contest_id)["official_results_ready"] == 0
    assert store.list_stage_results(contest_id, stage_idx=0) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def test_force_finish_rejects_legacy_round_robin_missing_one_edge(tmp_path):
    """Five settled edges cannot self-certify a four-player RR as complete."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="rr-missing-edge",
        stages=[{"key": "rr", "type": "round_robin"}],
    )
    pairings = store.list_contest_pairings(contest_id, stage_idx=0)
    missing = pairings[-1]
    with store._tx() as connection:
        connection.execute("DELETE FROM contest_pairings WHERE id=?", (missing["id"],))
        # Reproduce an unsealed legacy row: a fresh seal would reject the
        # mutation earlier, while terminal topology must also protect history.
        connection.execute(
            "UPDATE contests SET published_stage_pairing_count=NULL WHERE id=?",
            (contest_id,),
        )

    with pytest.raises(ValueError, match="未完成对阵|批次完整性"):
        asyncio.run(manager.finish(contest_id))
    assert store.get_contest(contest_id)["status"] == "running"
    assert store.list_stage_results(contest_id, stage_idx=0) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def test_two_player_knockout_can_force_finish_after_unique_champion(tmp_path):
    """The terminal topology gate retains the valid one-match KO boundary."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="ko-champion",
        stages=[{"key": "ko", "type": "single_elimination"}],
        player_count=2,
    )

    result = asyncio.run(manager.finish(contest_id))

    assert result["status"] == "finished"
    assert result["official_results_ready"] == 1
    assert len(store.list_stage_results(contest_id, stage_idx=0)) == 2
    assert len(store.list_official_results(contest_id)) == 2
    store.close()


def test_force_and_recovery_reject_unreached_configured_final(tmp_path):
    """A complete qualifier cannot stand in for an unmaterialized final."""
    stages = [
        {"key": "qualifier", "type": "round_robin", "advance_count": 2},
        {"key": "final", "type": "single_elimination"},
    ]
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path, label="unreached-final", stages=stages
    )
    before_pairings = store.list_contest_pairings(contest_id)

    with pytest.raises(ValueError, match="尚未到达配置中的最终阶段"):
        asyncio.run(manager.finish(contest_id))
    assert store.get_contest(contest_id)["status"] == "running"
    assert store.list_stage_results(contest_id) == []
    assert store.list_official_results(contest_id) == []

    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="finished",
        official_results_ready=0,
    )
    asyncio.run(
        manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    )
    assert store.get_contest(contest_id)["official_results_ready"] == 0
    assert store.list_contest_pairings(contest_id) == before_pairings
    assert store.list_stage_results(contest_id) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def test_empty_next_shortcut_cannot_skip_more_than_the_terminal_stage(tmp_path):
    """Even the internal 0/1 shortcut may cross only to the adjacent final."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="empty-next-skip",
        stages=[{"key": "stage0", "type": "round_robin"}],
        player_count=2,
    )
    store.update_contest(
        contest_id,
        stages_json=json.dumps(
            [
                {"key": "stage0", "type": "round_robin"},
                {
                    "key": "stage1",
                    "type": "single_elimination",
                    "advance_count": 1,
                },
                {"key": "stage2", "type": "single_elimination"},
            ]
        ),
    )
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="running",
    )

    result = manager._finish_adjudicated_contest_locked(
        contest_id,
        0,
        context="empty-next-stage",
        entry_updates=[],
        current_ranking=[],
        allow_unreached_empty_stage=True,
    )

    assert result is None
    assert store.get_contest(contest_id)["status"] == "running"
    assert store.list_stage_results(contest_id) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def test_force_and_recovery_reject_legacy_replace_scope_below_finalists(
    tmp_path,
):
    """A full but unrepresentable legacy graph cannot publish wrong places 3/4."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="bad-replace-scope",
        stages=[{"key": "qualifier", "type": "round_robin"}],
    )
    entries = store.list_contest_entries(contest_id)
    bad_stages = [
        {"key": "qualifier", "type": "round_robin", "advance_count": 4},
        {
            "key": "final",
            "type": "round_robin",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
        },
    ]
    store.update_contest(
        contest_id,
        current_stage_idx=1,
        stages_json=json.dumps(bad_stages),
        published_stage_pairing_count=None,
    )
    for left in range(len(entries)):
        for right in range(left + 1, len(entries)):
            entry_a, entry_b = entries[left], entries[right]
            match_id = f"bad-replace-final-{left}-{right}"
            store.create_match(
                match_id,
                entry_a["bot_id"],
                entry_b["bot_id"],
                owner_id=entry_a["user_id"],
                contest_id=contest_id,
                match_type="contest",
                game_id="holdem",
            )
            store.update_match(
                match_id,
                status="completed",
                winner=1,
                result={"deltas": [-1, 1]},
            )
            store.add_contest_pairing(
                contest_id,
                entry_a["bot_id"],
                entry_b["bot_id"],
                match_id=match_id,
                status="completed",
                stage_idx=1,
                stage_key="final",
                entry_a_id=entry_a["id"],
                entry_b_id=entry_b["id"],
            )
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=1,
        status="running",
    )

    with pytest.raises(ValueError, match="正式排名拓扑无效"):
        asyncio.run(manager.finish(contest_id))
    assert store.get_contest(contest_id)["status"] == "running"
    assert store.list_stage_results(contest_id) == []
    assert store.list_official_results(contest_id) == []

    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=1,
        status="finished",
        official_results_ready=0,
    )
    asyncio.run(
        manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    )
    assert store.get_contest(contest_id)["official_results_ready"] == 0
    assert store.list_stage_results(contest_id) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def test_stage_snapshot_strict_batch_rejects_partial_or_malformed_replacement(
    tmp_path,
):
    """Every rejected strict batch preserves the previous complete snapshot."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="strict-stage-batch",
        stages=[{"key": "rr", "type": "round_robin"}],
        player_count=2,
    )
    rows, expected_entries, expected_groups, _ranked = (
        manager._build_stage_result_rows(contest_id, 0)
    )
    store.replace_stage_results(
        contest_id,
        0,
        rows,
        expected_entries=expected_entries,
        expected_stage_groups=expected_groups,
    )
    before = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )

    foreign = copy.deepcopy(rows)
    foreign[-1]["entry_id"] = 999_999
    rank_gap = copy.deepcopy(rows)
    rank_gap[-1]["rank_in_group"] = len(rows) + 1
    bad_tiebreak = copy.deepcopy(rows)
    payload = json.loads(bad_tiebreak[-1]["payload_json"])
    payload["tiebreaks"]["points"] = "bad"
    bad_tiebreak[-1]["payload_json"] = json.dumps(payload)
    candidates = [
        [],
        rows[:1],
        [copy.deepcopy(rows[0]), copy.deepcopy(rows[0])],
        foreign,
        rank_gap,
        bad_tiebreak,
    ]
    for candidate in candidates:
        with pytest.raises(ValueError):
            store.replace_stage_results(
                contest_id,
                0,
                candidate,
                expected_entries=expected_entries,
                expected_stage_groups=expected_groups,
            )
        assert store.list_stage_result_recovery_snapshots(
            contest_id, stage_idx=0
        ) == before
    store.close()


def test_empty_stage_snapshot_batch_requires_exactly_empty_active_cohort(tmp_path):
    store = Store(str(tmp_path / "empty-stage-batch.db"))
    organizer = store.create_user(
        "empty-batch-org", "empty-batch-org@example.com", "hash"
    )
    bot = store.create_bot(
        organizer["id"],
        "empty-batch-bot",
        binary_path=_fixture_file(tmp_path, "empty-batch-bot"),
        format="elf",
        game_id="holdem",
    )
    stages_json = json.dumps([{"key": "rr", "type": "round_robin"}])
    empty_contest = store.create_contest(
        "empty batch",
        organizer_id=organizer["id"],
        status="running",
        game_id="holdem",
        current_stage_idx=0,
        stages_json=stages_json,
    )
    _seal_low_level_lifecycle_fixture(
        store,
        empty_contest["id"],
        stage_idx=0,
        status="running",
    )
    store.replace_stage_results(
        empty_contest["id"], 0, [], expected_entries=[]
    )
    assert store.list_stage_results(empty_contest["id"], stage_idx=0) == []

    one_contest = store.create_contest(
        "one player batch",
        organizer_id=organizer["id"],
        status="running",
        game_id="holdem",
        current_stage_idx=0,
        stages_json=stages_json,
    )
    store.add_contest_entry(one_contest["id"], organizer["id"], bot["id"])
    _seal_low_level_lifecycle_fixture(
        store,
        one_contest["id"],
        stage_idx=0,
        status="running",
    )
    expected = store.list_contest_entries(one_contest["id"])
    with pytest.raises(ValueError, match="未精确覆盖"):
        store.replace_stage_results(
            one_contest["id"], 0, [], expected_entries=expected
        )
    assert store.list_stage_results(one_contest["id"], stage_idx=0) == []
    store.close()


def test_sealed_dynamic_round_append_moves_manifest_and_rejects_damage(tmp_path):
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    stage = {
        "key": "swiss",
        "type": "swiss",
        "rounds": 3,
        "scoring": "poker_3_1_0",
    }
    store.update_contest(
        contest_id,
        status="published",
        stages_json=json.dumps([stage]),
    )
    first_round = store.create_contest_stage_pairings(
        contest_id,
        0,
        [
            {
                "entry_a_id": entries[0]["id"],
                "entry_b_id": entries[1]["id"],
                "bot_a_id": entries[0]["bot_id"],
                "bot_b_id": entries[1]["bot_id"],
                "round_num": 1,
                "stage_key": "swiss",
                "published_at": "2026-01-01T00:00:00",
            }
        ],
        expected_current_stage_idx=0,
        expected_status="published",
    )
    assert store.get_contest(contest_id)["published_stage_pairing_count"] == 1
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET status='running' WHERE id=?", (contest_id,)
        )

    second_round = store.append_contest_round_pairings(
        contest_id,
        0,
        [
            {
                "entry_a_id": entries[1]["id"],
                "entry_b_id": entries[0]["id"],
                "bot_a_id": entries[1]["bot_id"],
                "bot_b_id": entries[0]["bot_id"],
                "round_num": 2,
                "stage_key": "swiss",
                "published_at": "2026-01-01T00:00:00",
            }
        ],
        expected_current_stage_idx=0,
        expected_previous_max_round=1,
    )
    assert len(second_round) == 1
    assert store.get_contest(contest_id)["published_stage_pairing_count"] == 2
    assert store.contest_stage_manifest_is_valid(
        contest_id, 0, include_terminal_orphans=True
    )

    with store._tx() as connection:
        connection.execute(
            "DELETE FROM contest_pairings WHERE id=?", (first_round[0]["id"],)
        )
    with pytest.raises(ValueError, match="批次完整性"):
        store.append_contest_round_pairings(
            contest_id,
            0,
            [
                {
                    "entry_a_id": entries[0]["id"],
                    "entry_b_id": entries[1]["id"],
                    "bot_a_id": entries[0]["bot_id"],
                    "bot_b_id": entries[1]["bot_id"],
                    "round_num": 3,
                    "stage_key": "swiss",
                    "published_at": "2026-01-01T00:00:00",
                }
            ],
            expected_current_stage_idx=0,
            expected_previous_max_round=2,
        )
    assert store.get_contest(contest_id)["published_stage_pairing_count"] == 2
    assert not any(
        pairing["round_num"] == 3
        for pairing in store.list_contest_pairings(contest_id, stage_idx=0)
    )
    store.close()


def test_terminal_result_failure_rolls_back_then_running_restart_retries(
    tmp_path,
):
    """正式榜第二行故障回滚整批；running 在启动对账后完整重试。"""
    store, contest_id = _setup(tmp_path)
    db_path = tmp_path / "contest-atomicity.db"
    entries = store.list_contest_entries(contest_id)
    for entry in entries:
        store.update_entry(contest_id, entry["user_id"], eliminated=0)
    store.update_contest(contest_id, status="running", current_stage_idx=0)
    match_id = "official-result-final"
    store.create_match(
        match_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        owner_id=entries[0]["user_id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        result={"deltas": [100, -100]},
    )
    store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        match_id=match_id,
        status="completed",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    manager = ContestManager(store, _SuccessOrch())
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="running",
    )
    manager._snapshot_stage_results(contest_id, 0)
    prior_stage_rows = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )
    prior_stage_ids = [row["id"] for row in prior_stage_rows]
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_second_official_result "
            "BEFORE INSERT ON contest_official_results "
            "WHEN (SELECT COUNT(*) FROM contest_official_results "
            "      WHERE contest_id=NEW.contest_id)=1 "
            "BEGIN SELECT RAISE(ABORT, 'injected official result failure'); END"
        )
    asyncio.run(manager.maybe_finish(contest_id))

    failed_state = store.get_contest(contest_id)
    assert failed_state["status"] == "running"
    assert failed_state["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []
    failed_stage_rows = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )
    assert failed_stage_rows == prior_stage_rows
    assert [row["id"] for row in failed_stage_rows] == prior_stage_ids
    store.close()  # TEMP trigger disappears: model the recovering process.

    recovered = Store(str(db_path))
    assert recovered.get_match(match_id)["result"]["rounds_played"] == 70
    recovery_manager = ContestManager(recovered, _SuccessOrch())
    assert asyncio.run(
        recovery_manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    ) == 1
    ready_state = recovered.get_contest(contest_id)
    assert ready_state["status"] == "finished"
    assert ready_state["official_results_ready"] == 1
    results = recovered.list_official_results(contest_id)
    assert [row["rank"] for row in results] == [1, 2]
    assert {row["entry_id"] for row in results} == {
        entry["id"] for entry in entries
    }
    # Once ready, another startup scan is a no-op rather than rewriting IDs/data.
    result_ids = [row["id"] for row in results]
    assert asyncio.run(
        recovery_manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    ) == 0
    assert [row["id"] for row in recovered.list_official_results(contest_id)] == result_ids
    recovered.close()

    from fastapi.testclient import TestClient

    from bzplat.backend.main import create_app

    app = create_app(db_path=str(db_path))
    response = TestClient(app).get(
        f"/api/contests/{contest_id}/official-results"
    )
    assert response.status_code == 200
    assert len(response.json()["results"]) == 2
    app.state.store.close()


@pytest.mark.parametrize("bad_seed", ["bad", 0.5, -1])
def test_bad_seed_cannot_clear_snapshot_or_publish_terminal_state(
    tmp_path, bad_seed
):
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    for seed, entry in enumerate(entries, start=1):
        store.update_entry(
            contest_id,
            entry["user_id"],
            seed=seed,
            eliminated=0,
        )
    store.update_contest(contest_id, status="running", current_stage_idx=0)
    match_id = f"bad-seed-terminal-{bad_seed!s}"
    store.create_match(
        match_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        owner_id=entries[0]["user_id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        result={"deltas": [100, -100]},
    )
    store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        match_id=match_id,
        status="completed",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    manager = ContestManager(store, _SuccessOrch())
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="running",
    )
    manager._snapshot_stage_results(contest_id, 0)
    prior_snapshot = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )
    with store._tx() as connection:
        connection.execute(
            "UPDATE contest_entries SET seed=? WHERE id=?",
            (bad_seed, entries[0]["id"]),
        )

    # The stricter terminal cohort/schema gate may reject before the finalizer
    # and therefore return the documented no-transition sentinel.  The durable
    # state, not a convenience return projection, is the atomicity contract.
    assert asyncio.run(manager.maybe_finish(contest_id)) is None
    with pytest.raises(ValueError, match="无法结束赛事|无法强制结束"):
        asyncio.run(manager.finish(contest_id))
    blocked = store.get_contest(contest_id)
    assert blocked["status"] == "running"
    assert blocked["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == prior_snapshot

    with store._tx() as connection:
        connection.execute(
            "UPDATE contest_entries SET seed=? WHERE id=?",
            (1, entries[0]["id"]),
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest_id,),
        )
    recovered = asyncio.run(manager.maybe_finish(contest_id))
    assert recovered["status"] == "finished"
    assert recovered["official_results_ready"] == 1
    assert len(store.list_official_results(contest_id)) == 2
    store.close()


@pytest.mark.parametrize("status", ["running", "rest"])
@pytest.mark.parametrize(
    "stages",
    [
        [
            {"key": "q", "type": "round_robin", "advance_count": 1},
            {"key": "f", "type": "swiss", "rounds": 1},
        ],
        [
            {"key": "q0", "type": "round_robin", "advance_count": 1},
            {"key": "q1", "type": "swiss", "rounds": 1, "advance_count": 1},
            {"key": "f", "type": "single_elimination"},
        ],
    ],
)
def test_imported_active_unrepresentable_ranking_graph_fails_before_writes(
    tmp_path, status, stages
):
    """升级前非法 active 图只诊断阻断，不反复写半成品。"""
    store, contest_id = _setup(tmp_path)
    for entry in store.list_contest_entries(contest_id):
        store.update_entry(contest_id, entry["user_id"], eliminated=0)
    store.update_contest(
        contest_id,
        current_stage_idx=0,
        stages_json=json.dumps(stages),
    )
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status=status,
        rest_ends_at=("2099-01-01T00:00:00" if status == "rest" else None),
    )
    before_contest = store.get_contest(contest_id)
    before_entries = store.list_contest_entries(contest_id)
    manager = ContestManager(store, _SuccessOrch())

    if status == "running":
        assert asyncio.run(manager.maybe_finish(contest_id)) is None
    else:
        with pytest.raises(ValueError, match="cohort|正式排名"):
            asyncio.run(manager.resume(contest_id))

    assert store.get_contest(contest_id) == before_contest
    assert store.list_contest_entries(contest_id) == before_entries
    assert store.list_contest_pairings(contest_id) == []
    assert store.list_stage_results(contest_id) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def test_imported_nonterminal_knockout_without_advancement_cannot_advance(
    tmp_path,
):
    """An old ambiguous KO snapshot must not advance everyone by default."""
    valid_stages = [
        {
            "key": "qualifier",
            "type": "single_elimination",
            "advance_count": 1,
        },
        {
            "key": "final",
            "type": "double_round_robin",
            "ranking_mode": "replace_top",
            "ranking_scope": 1,
        },
    ]
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="legacy-ko-no-advance",
        stages=valid_stages,
        player_count=2,
    )
    imported_stages = [
        {
            key: value
            for key, value in valid_stages[0].items()
            if key != "advance_count"
        },
        valid_stages[1],
    ]
    store.update_contest(contest_id, stages_json=json.dumps(imported_stages))
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="running",
    )
    before_contest = store.get_contest(contest_id)
    before_entries = store.list_contest_entries(contest_id)
    before_pairings = store.list_contest_pairings(contest_id)

    with pytest.raises(
        ValueError,
        match="非终局 single_elimination|advance_count|正式排名拓扑",
    ):
        manager._validated_active_lifecycle_stages(
            store.get_contest(contest_id), imported_stages
        )
    with pytest.raises(
        ValueError,
        match="对阵尚未全部完成|正式排名拓扑",
    ):
        asyncio.run(manager.advance(contest_id))

    assert store.get_contest(contest_id) == before_contest
    assert store.list_contest_entries(contest_id) == before_entries
    assert store.list_contest_pairings(contest_id) == before_pairings
    assert store.list_stage_results(contest_id) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def test_stage_snapshot_batch_failure_leaves_no_partial_rows(tmp_path):
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="stage-snapshot-failure",
        stages=[{"key": "rr", "type": "round_robin"}],
        player_count=2,
    )
    entries = store.list_contest_entries(contest_id)
    failure_entry_id = int(entries[1]["id"])
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_second_stage_result "
            "BEFORE INSERT ON contest_stage_results "
            f"WHEN NEW.entry_id={failure_entry_id} "
            "BEGIN SELECT RAISE(ABORT, 'injected stage snapshot failure'); END"
        )

    with pytest.raises(sqlite3.DatabaseError, match="injected stage snapshot failure"):
        manager._snapshot_stage_results(contest_id, 0)

    assert store.list_stage_results(contest_id, stage_idx=0) == []
    store.close()


@pytest.mark.parametrize("drift", ["missing", "aborted", "deleted_opponent"])
def test_finished_unready_recovery_refuses_unadjudicated_pairings(
    tmp_path, drift
):
    """Terminal status cannot freeze missing/aborted/deleted drift into a ranking."""
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    match_id = None
    if drift == "deleted_opponent":
        store.update_contest(
            contest_id,
            stages_json=json.dumps([{"key": "swiss", "type": "swiss"}]),
        )
    else:
        match_id = f"unready-{drift}"
        if drift == "aborted":
            store.create_match(
                match_id,
                entries[0]["bot_id"],
                entries[1]["bot_id"],
                owner_id=entries[0]["user_id"],
                contest_id=contest_id,
                match_type="contest",
                game_id="holdem",
            )
            store.update_match(match_id, status="aborted", reason="test_abort")
        # ``missing`` intentionally leaves a non-null logical match id with no
        # row in matches_index/the per-game match table.
        if drift == "missing":
            match_id = "unready-missing-match"

    pairing = store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        match_id=match_id,
        status="completed",
        stage_idx=0,
        stage_key="swiss" if drift == "deleted_opponent" else "rr",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="finished",
        official_results_ready=0,
    )
    if drift == "deleted_opponent":
        assert store.delete_bot(entries[1]["bot_id"])
        damaged = next(
            row for row in store.list_contest_pairings(contest_id)
            if row["id"] == pairing["id"]
        )
        assert damaged["entry_b_id"] == entries[1]["id"]
        assert damaged["bot_b_id"] is None

    manager = ContestManager(store, _SuccessOrch())
    assert asyncio.run(
        manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    ) == 1
    blocked = store.get_contest(contest_id)
    assert blocked["status"] == "finished"
    assert blocked["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []
    # A repeated startup remains fail-closed and cannot turn the same drift
    # into a different result.
    assert asyncio.run(
        manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    ) == 1
    assert store.get_contest(contest_id)["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []
    store.close()


@pytest.mark.parametrize(
    ("participant_count", "expected_ready"),
    [(0, True), (1, False), (2, False)],
)
def test_finished_unready_zero_pairing_recovery_uses_active_entry_gate(
    tmp_path, participant_count, expected_ready
):
    """Only the uniquely empty cohort can recover without a persisted decision."""
    store = Store(str(tmp_path / f"zero-pairing-{participant_count}.db"))
    organizer = store.create_user(
        f"zero-org-{participant_count}",
        f"zero-org-{participant_count}@example.com",
        "hash",
        role="organizer",
    )
    contest_id = store.create_contest(
        f"zero pairing {participant_count}",
        organizer_id=organizer["id"],
        status="open",
        game_id="holdem",
        current_stage_idx=0,
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )["id"]
    entry_ids = []
    for index in range(participant_count):
        player = store.create_user(
            f"zero-player-{index}", f"zero-player-{index}@example.com", "hash"
        )
        bot = store.create_bot(
            player["id"], f"zero-bot-{index}", binary_path="/tmp", format="elf",
            game_id="holdem",
        )
        entry_ids.append(store.add_contest_entry(
            contest_id, player["id"], bot["id"]
        )["id"])
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="finished",
        official_results_ready=0,
    )

    manager = ContestManager(store, _SuccessOrch())

    async def recover_concurrently():
        return await asyncio.gather(
            manager.reconcile_running_contests(
                interruption_reason="orphan_after_service_restart"
            ),
            manager.reconcile_running_contests(
                interruption_reason="orphan_after_service_restart"
            ),
        )

    attempts = asyncio.run(recover_concurrently())
    assert max(attempts) == 1
    recovered = store.get_contest(contest_id)
    assert recovered["official_results_ready"] == int(expected_ready)
    official = store.list_official_results(contest_id)
    if participant_count == 1 and expected_ready:
        assert len(official) == 1
        assert official[0]["entry_id"] == entry_ids[0]
        assert official[0]["rank"] == 1
        assert official[0]["points"] == 0
    else:
        assert official == []
    assert asyncio.run(
        manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    ) == (
        0 if expected_ready else 1
    )
    store.close()


@pytest.mark.parametrize("drift", ["missing", "aborted", "completed"])
def test_terminal_replace_top_rechecks_every_stage_before_snapshot(
    tmp_path, drift
):
    """A completed final cannot hide a missing/aborted qualifier Match."""
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    for entry in entries:
        store.update_entry(contest_id, entry["user_id"], eliminated=0)
    store.update_contest(
        contest_id,
        status="running",
        current_stage_idx=1,
        stages_json=json.dumps(
            [
                {"key": "qualifier", "type": "round_robin"},
                {
                    "key": "final",
                    "type": "round_robin",
                    "ranking_mode": "replace_top",
                    "ranking_scope": 2,
                },
            ]
        ),
    )

    qualifier_match_id = f"replace-top-qualifier-{drift}"
    if drift != "missing":
        store.create_match(
            qualifier_match_id,
            entries[0]["bot_id"],
            entries[1]["bot_id"],
            owner_id=entries[0]["user_id"],
            contest_id=contest_id,
            match_type="contest",
            game_id="holdem",
        )
        update = {
            "status": drift,
            "winner": 0 if drift == "completed" else None,
            "result": {"deltas": [100, -100]} if drift == "completed" else {},
        }
        if drift == "aborted":
            update["reason"] = "test_abort"
        store.update_match(qualifier_match_id, **update)
    store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        match_id=qualifier_match_id,
        status="completed",
        stage_idx=0,
        stage_key="qualifier",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    manager = ContestManager(store, _SuccessOrch())
    if drift == "completed":
        _seal_low_level_lifecycle_fixture(
            store,
            contest_id,
            stage_idx=0,
            status="running",
        )
        manager._snapshot_stage_results(contest_id, 0)

    final_match_id = f"replace-top-final-{drift}"
    store.create_match(
        final_match_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        owner_id=entries[0]["user_id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(
        final_match_id,
        status="completed",
        winner=1,
        result={"deltas": [-50, 50]},
    )
    store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        match_id=final_match_id,
        status="completed",
        stage_idx=1,
        stage_key="final",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=1,
        status="running",
    )

    result = asyncio.run(manager.maybe_finish(contest_id))
    state = store.get_contest(contest_id)
    if drift == "completed":
        assert result["status"] == "finished"
        assert state["status"] == "finished"
        assert state["official_results_ready"] == 1
        assert len(store.list_stage_results(contest_id, stage_idx=1)) == 2
        assert len(store.list_official_results(contest_id)) == 2
    else:
        assert result is None
        assert state["status"] == "running"
        assert state["current_stage_idx"] == 1
        assert state["official_results_ready"] == 0
        assert store.list_stage_results(contest_id, stage_idx=1) == []
        assert store.list_official_results(contest_id) == []
    store.close()


@pytest.mark.parametrize("status", ["running", "rest"])
def test_force_finish_rejects_two_active_entries_without_current_pairings(
    tmp_path, status
):
    """running/rest cannot freeze a two-player empty graph into a zero table."""
    store, contest_id = _setup(tmp_path)
    for entry in store.list_contest_entries(contest_id):
        store.update_entry(contest_id, entry["user_id"], eliminated=0)
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status=status,
    )

    manager = ContestManager(store, _SuccessOrch())
    with pytest.raises(ValueError, match="未完成对阵|批次完整性"):
        asyncio.run(manager.finish(contest_id))
    state = store.get_contest(contest_id)
    assert state["status"] == status
    assert state["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []
    store.close()


def test_persisted_later_empty_stage_cannot_recover_from_prior_standings(
    tmp_path,
):
    """Recovery must not rank an empty final and reverse its qualifier winner."""
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    store.update_entry(contest_id, entries[0]["user_id"], seed=1, eliminated=1)
    store.update_entry(contest_id, entries[1]["user_id"], seed=2, eliminated=0)
    store.update_contest(
        contest_id,
        status="running",
        current_stage_idx=1,
        stages_json=json.dumps(
            [
                {"key": "qualifier", "type": "round_robin"},
                {"key": "final", "type": "single_elimination"},
            ]
        ),
    )
    match_id = "one-active-qualifier"
    store.create_match(
        match_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        owner_id=entries[0]["user_id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(
        match_id,
        status="completed",
        winner=1,
        result={"deltas": [-100, 100]},
    )
    store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        match_id=match_id,
        status="completed",
        stage_idx=0,
        stage_key="qualifier",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=1,
        status="finished",
        official_results_ready=0,
    )

    manager = ContestManager(store, _SuccessOrch())
    assert asyncio.run(
        manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    ) == 1
    state = store.get_contest(contest_id)
    assert state["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []
    store.close()


def test_generated_empty_next_stage_finalizes_from_completed_prior_stage(
    tmp_path,
):
    """A real qualifier champion needs no one-player final and keeps rank #1."""
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    store.update_entry(contest_id, entries[0]["user_id"], seed=1, eliminated=0)
    store.update_entry(contest_id, entries[1]["user_id"], seed=2, eliminated=0)
    store.update_contest(
        contest_id,
        status="running",
        current_stage_idx=0,
        stages_json=json.dumps(
            [
                {
                    "key": "qualifier",
                    "type": "round_robin",
                    "advance_count": 1,
                },
                {"key": "final", "type": "single_elimination"},
            ]
        ),
    )
    match_id = "generated-one-player-final"
    store.create_match(
        match_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        owner_id=entries[0]["user_id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(
        match_id,
        status="completed",
        winner=1,
        result={"deltas": [-100, 100]},
    )
    store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        match_id=match_id,
        status="completed",
        stage_idx=0,
        stage_key="qualifier",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )

    manager = ContestManager(store, _SuccessOrch())
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="running",
    )
    result = asyncio.run(manager.maybe_finish(contest_id))
    assert result["status"] == "finished"
    assert result["current_stage_idx"] == 0
    assert result["official_results_ready"] == 1
    assert store.list_contest_pairings(contest_id, stage_idx=1) == []
    official = store.list_official_results(contest_id)
    assert [row["entry_id"] for row in official] == [
        entries[1]["id"],
        entries[0]["id"],
    ]
    assert [row["rank"] for row in official] == [1, 2]
    store.close()


def test_generated_empty_next_stage_terminal_failure_rolls_back_advancement(
    tmp_path,
):
    """终局最后一步故障时，0/1 人下一阶段不得留下半晋级名册。"""
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    for seed, entry in enumerate(entries, start=1):
        store.update_entry(
            contest_id,
            entry["user_id"],
            seed=seed,
            eliminated=0,
        )
    store.update_contest(
        contest_id,
        status="running",
        current_stage_idx=0,
        stages_json=json.dumps(
            [
                {
                    "key": "qualifier",
                    "type": "round_robin",
                    "advance_count": 1,
                },
                {"key": "final", "type": "single_elimination"},
            ]
        ),
    )
    match_id = "generated-one-player-final-rollback"
    store.create_match(
        match_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        owner_id=entries[0]["user_id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(
        match_id,
        status="completed",
        winner=1,
        result={"deltas": [-100, 100]},
    )
    store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        match_id=match_id,
        status="completed",
        stage_idx=0,
        stage_key="qualifier",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    before_entries = _entry_state(store, contest_id)
    _seal_low_level_lifecycle_fixture(
        store,
        contest_id,
        stage_idx=0,
        status="running",
    )
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_empty_next_terminal_status "
            "BEFORE UPDATE OF status ON contests "
            "WHEN NEW.id=OLD.id AND NEW.status='finished' "
            "BEGIN SELECT RAISE(ABORT, 'injected terminal status failure'); END"
        )

    manager = ContestManager(store, _SuccessOrch())
    with pytest.raises(ValueError, match="终态发布失败"):
        asyncio.run(manager.maybe_finish(contest_id))

    failed = store.get_contest(contest_id)
    assert failed["status"] == "running"
    assert failed["current_stage_idx"] == 0
    assert failed["official_results_ready"] == 0
    assert _entry_state(store, contest_id) == before_entries
    assert store.list_official_results(contest_id) == []
    stage_rows = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )
    assert len(stage_rows) == 2

    with store._tx() as connection:
        connection.execute("DROP TRIGGER fail_empty_next_terminal_status")
    result = asyncio.run(manager.maybe_finish(contest_id))
    assert result["status"] == "finished"
    assert result["official_results_ready"] == 1
    after_entries = _entry_state(store, contest_id)
    assert after_entries[entries[0]["user_id"]][1] == 1
    assert after_entries[entries[1]["user_id"]][1] == 0
    store.close()


def _flip_completed_stage_results(store: Store, contest_id: int, stage_idx: int) -> None:
    """Make a replay choose the opposite entrants without touching the decision."""
    for pairing in store.list_contest_pairings(contest_id, stage_idx=stage_idx):
        match_id = pairing.get("match_id")
        if not match_id:
            continue
        match = store.get_match(match_id)
        assert match is not None and match["status"] == "completed"
        winner = int(match["winner"])
        flipped = 1 - winner
        store.update_match(
            match_id,
            status="completed",
            winner=flipped,
            result={"deltas": [1, -1] if flipped == 0 else [-1, 1]},
        )


def test_rest_transition_failure_retries_from_installed_decision_without_replay(
    tmp_path, monkeypatch
):
    """A failed running->rest write may install once, but never rewrite ranking."""
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "advance_count": 2,
            "rest_after_minutes": 60,
        },
        {
            "key": "final",
            "type": "round_robin",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
        },
    ]
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="immutable-rest-decision",
        stages=stages,
        player_count=3,
    )
    before_entries = _entry_state(store, contest_id)
    before_pairings = store.list_contest_pairings(contest_id)
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_rest_transition "
            "BEFORE UPDATE OF status ON contests "
            "WHEN NEW.id=OLD.id AND NEW.status='rest' "
            "BEGIN SELECT RAISE(ABORT, 'injected rest transition failure'); END"
        )

    with pytest.raises(sqlite3.DatabaseError, match="injected rest transition failure"):
        asyncio.run(manager.maybe_finish(contest_id))

    failed = store.get_contest(contest_id)
    assert failed["status"] == "running"
    assert failed["current_stage_idx"] == 0
    assert _entry_state(store, contest_id) == before_entries
    assert store.list_contest_pairings(contest_id) == before_pairings
    installed = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )
    assert len(installed) == 3
    frozen_ranking = manager._stage_ranking_from_recovery_snapshot(contest_id, 0)
    assert frozen_ranking is not None
    frozen_advance = {
        int(row["entry_id"])
        for row in sorted(frozen_ranking, key=lambda row: row["rank"])[:2]
    }

    _flip_completed_stage_results(store, contest_id, 0)
    with store._tx() as connection:
        connection.execute("DROP TRIGGER fail_rest_transition")
    retried = asyncio.run(manager.maybe_finish(contest_id))
    assert retried is not None and retried["status"] == "rest"
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == installed

    async def no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(manager, "_dispatch_pending_locked", no_dispatch)
    asyncio.run(manager.resume(contest_id))
    assert {
        int(entry["id"])
        for entry in store.list_contest_entries(contest_id)
        if int(entry["eliminated"]) == 0
    } == frozen_advance
    store.close()


def test_rest_without_persisted_decision_never_replays_match_ranking(
    tmp_path, monkeypatch
):
    """A legacy REST row without its immutable decision stays fail-closed."""
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "advance_count": 2,
            "rest_after_minutes": 60,
        },
        {
            "key": "final",
            "type": "round_robin",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
        },
    ]
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="rest-missing-decision",
        stages=stages,
        player_count=3,
    )
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == []
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status='rest',"
            "rest_ends_at='2099-01-01T00:00:00' WHERE id=?",
            (contest_id,),
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest_id,),
        )

    before_contest = store.get_contest(contest_id)
    before_entries = store.list_contest_entries(contest_id)
    before_pairings = store.list_contest_pairings(contest_id)
    replay_calls = 0

    def forbidden_replay(*_args, **_kwargs):
        nonlocal replay_calls
        replay_calls += 1
        raise AssertionError("REST must not replay Match ranking")

    monkeypatch.setattr(manager, "_build_stage_result_rows", forbidden_replay)
    with pytest.raises(ValueError, match="缺少不可变阶段决策"):
        manager._ensure_stage_decision(contest_id, 0)
    monkeypatch.setattr(manager, "_rank_stage_rows", forbidden_replay)
    with pytest.raises(ValueError, match="缺少不可变阶段决策"):
        manager._plan_participant_advancement(contest_id, 0)

    assert replay_calls == 0
    assert store.get_contest(contest_id) == before_contest
    assert store.list_contest_entries(contest_id) == before_entries
    assert store.list_contest_pairings(contest_id) == before_pairings
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == []
    store.close()


def test_next_stage_failure_retries_from_installed_decision_without_replay(
    tmp_path, monkeypatch
):
    """A failed pairing batch keeps one decision and retries its exact Top-N."""
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "advance_count": 3,
        },
        {
            "key": "final",
            "type": "round_robin",
            "ranking_mode": "replace_top",
            "ranking_scope": 3,
        },
    ]
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="immutable-advance-decision",
        stages=stages,
        player_count=4,
    )
    before_entries = _entry_state(store, contest_id)
    before_pairings = store.list_contest_pairings(contest_id)
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_second_next_stage_pairing "
            "BEFORE INSERT ON contest_pairings "
            "WHEN NEW.stage_idx=1 AND "
            "(SELECT COUNT(*) FROM contest_pairings "
            " WHERE contest_id=NEW.contest_id AND stage_idx=1)=1 "
            "BEGIN SELECT RAISE(ABORT, 'injected next stage failure'); END"
        )

    with pytest.raises(sqlite3.DatabaseError, match="injected next stage failure"):
        asyncio.run(manager.maybe_finish(contest_id))

    failed = store.get_contest(contest_id)
    assert failed["status"] == "running"
    assert failed["current_stage_idx"] == 0
    assert _entry_state(store, contest_id) == before_entries
    assert store.list_contest_pairings(contest_id) == before_pairings
    installed = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )
    assert len(installed) == 4
    frozen_ranking = manager._stage_ranking_from_recovery_snapshot(contest_id, 0)
    assert frozen_ranking is not None
    frozen_advance = {
        int(row["entry_id"])
        for row in sorted(frozen_ranking, key=lambda row: row["rank"])[:3]
    }

    _flip_completed_stage_results(store, contest_id, 0)
    with store._tx() as connection:
        connection.execute("DROP TRIGGER fail_second_next_stage_pairing")

    async def no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(manager, "_dispatch_pending_locked", no_dispatch)
    asyncio.run(manager.maybe_finish(contest_id))
    assert store.get_contest(contest_id)["current_stage_idx"] == 1
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == installed
    assert {
        int(entry["id"])
        for entry in store.list_contest_entries(contest_id)
        if int(entry["eliminated"]) == 0
    } == frozen_advance
    assert len(store.list_contest_pairings(contest_id, stage_idx=1)) == 3
    store.close()


@pytest.mark.parametrize("snapshot_drift", ["missing", "partial", "malformed"])
def test_finished_unready_recovery_never_replays_matches_without_exact_snapshot(
    tmp_path, monkeypatch, snapshot_drift
):
    """Recovery is snapshot-only; damaged history stays unready without replay."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label=f"snapshot-only-{snapshot_drift}",
        stages=[{"key": "rr", "type": "round_robin"}],
        player_count=2,
    )
    rows, expected_entries, expected_groups, replayed_ranking = (
        manager._build_stage_result_rows(contest_id, 0)
    )
    if snapshot_drift == "partial":
        _insert_stage_result_fixture_row(store, contest_id, 0, rows[0])
    elif snapshot_drift == "malformed":
        store.replace_stage_results(
            contest_id,
            0,
            rows,
            expected_entries=expected_entries,
            expected_stage_groups=expected_groups,
        )
        with store._tx() as connection:
            connection.execute(
                "UPDATE contest_stage_results SET payload_json='{}' "
                "WHERE contest_id=? AND stage_idx=0 AND entry_id=?",
                (contest_id, rows[0]["entry_id"]),
            )
    # Model an older process that reached ``finished`` before publishing the
    # official batch.  Re-seal the exact completed topology so this test reaches
    # the snapshot-only recovery boundary rather than failing at an unrelated
    # lifecycle-revision guard.
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET status='finished',official_results_ready=0 "
            "WHERE id=?",
            (contest_id,),
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest_id,),
        )
    before_snapshot = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )
    replay_calls = 0

    def replay_if_called(_contest_id, _stage_idx):
        nonlocal replay_calls
        replay_calls += 1
        return replayed_ranking

    monkeypatch.setattr(manager, "_rank_stage_rows", replay_if_called)
    asyncio.run(manager._reconcile_one(contest_id))

    state = store.get_contest(contest_id)
    assert replay_calls == 0
    assert state["status"] == "finished"
    assert state["official_results_ready"] == 0
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == before_snapshot
    assert store.list_official_results(contest_id) == []
    store.close()


@pytest.mark.parametrize("seal_drift", ["missing", "stale"])
def test_finished_unready_recovery_requires_exact_terminal_seal(
    tmp_path, seal_drift
):
    """A persisted decision is not writable authority without its exact seal."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label=f"finished-seal-{seal_drift}",
        stages=[{"key": "rr", "type": "round_robin"}],
        player_count=2,
    )
    manager._snapshot_stage_results(contest_id, 0)
    snapshot_before = store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status='finished',official_results_ready=0 "
            "WHERE id=?",
            (contest_id,),
        )
        if seal_drift == "missing":
            connection.execute(
                "UPDATE contests SET sealed_pairing_topology_revision=NULL "
                "WHERE id=?",
                (contest_id,),
            )

    asyncio.run(manager._reconcile_one(contest_id))

    state = store.get_contest(contest_id)
    assert state["status"] == "finished"
    assert state["official_results_ready"] == 0
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == snapshot_before
    assert store.list_official_results(contest_id) == []
    store.close()


@pytest.mark.parametrize("input_drift", ["result", "binding"])
def test_stage_decision_install_rejects_match_input_drift_after_ranking_replay(
    tmp_path, monkeypatch, input_drift
):
    """A candidate may only commit against the exact Match graph it ranked."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label=f"decision-input-{input_drift}",
        stages=[{"key": "rr", "type": "round_robin"}],
        player_count=2,
    )
    pairing = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    original_match_id = str(pairing["match_id"])
    original_builder = manager._build_stage_result_rows

    def build_then_drift(*args, **kwargs):
        candidate = original_builder(*args, **kwargs)
        if input_drift == "result":
            store.update_match(
                original_match_id,
                status="completed",
                winner=1,
                result={"deltas": [-9, 9]},
            )
        else:
            replacement_match_id = f"replacement-{original_match_id}"
            store.create_match(
                replacement_match_id,
                pairing["bot_a_id"],
                pairing["bot_b_id"],
                owner_id=store.list_contest_entries(contest_id)[0]["user_id"],
                contest_id=contest_id,
                match_type="contest",
                game_id="holdem",
            )
            store.update_match(
                replacement_match_id,
                status="completed",
                winner=1,
                result={"deltas": [-11, 11]},
            )
            store.update_contest_pairing(
                pairing["id"],
                match_id=replacement_match_id,
                status="completed",
            )
        return candidate

    monkeypatch.setattr(manager, "_build_stage_result_rows", build_then_drift)
    outcome = asyncio.run(manager.maybe_finish(contest_id))
    assert outcome is not None and outcome["status"] == "running"

    state = store.get_contest(contest_id)
    assert state["status"] == "running"
    assert state["official_results_ready"] == 0
    assert store.list_stage_result_recovery_snapshots(
        contest_id, stage_idx=0
    ) == []
    assert store.list_official_results(contest_id) == []
    store.close()


def test_stage_decision_candidate_is_pure_over_one_store_projection(
    tmp_path, monkeypatch
):
    """Ranking a tokenized candidate performs no second Store read."""
    store, contest_id, manager = _materialized_stage_fixture(
        tmp_path,
        label="decision-input-pure",
        stages=[{"key": "rr", "type": "round_robin"}],
        player_count=3,
    )
    projection = store.contest_stage_decision_input_snapshot(
        contest_id, 0, expected_status="running"
    )
    assert projection is not None

    def unexpected_store_read(*_args, **_kwargs):
        raise AssertionError("candidate replay escaped its Store projection")

    for method_name in (
        "get_contest",
        "list_contest_entries",
        "list_contest_pairings",
        "get_match",
    ):
        monkeypatch.setattr(store, method_name, unexpected_store_read)

    rows, entries, groups, ranking = manager._build_stage_result_rows(
        contest_id,
        0,
        decision_input_snapshot=projection,
    )
    assert len(rows) == len(entries) == len(ranking) == 3
    assert groups is None
    store.close()


def _clear_active_stage_manifest(store: Store, contest_id: int) -> None:
    """Reproduce a pre-seal active row without legitimizing it on reopen."""
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET published_stage_pairing_count=NULL,"
            "sealed_pairing_topology_revision=NULL WHERE id=?",
            (contest_id,),
        )


@pytest.mark.parametrize(
    "operation",
    [
        "dispatchable",
        "existing-seed",
        "markerless-seed",
        "bind",
        "technical-adjudication",
    ],
)
@pytest.mark.parametrize("active_status", ["published", "running"])
def test_active_null_manifest_rejects_every_pre_execution_store_boundary(
    tmp_path, operation, active_status
):
    """A legacy NULL manifest is read-only and can never authorize execution."""
    store, contest_id, _manager, _entries, _bots = _sealed_pending_current_fixture(
        tmp_path, label=f"null-manifest-{operation}"
    )
    pairing = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    contest = store.get_contest(contest_id)
    time_control_id = contest["time_control_id"] or "holdem_per_decision_60s_v1"
    if active_status == "running":
        with store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE contests SET status='running' WHERE id=?",
                (contest_id,),
            )
    if operation == "existing-seed":
        with store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE contest_pairings SET pairing_seed=123456 WHERE id=?",
                (pairing["id"],),
            )
        pairing = store.list_contest_pairings(contest_id, stage_idx=0)[0]
        assert pairing["pairing_seed"] == 123456
    elif operation == "markerless-seed":
        with store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE contest_pairings SET pairing_seed=NULL WHERE id=?",
                (pairing["id"],),
            )
            connection.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=?",
                (contest_id,),
            )
        pairing = store.list_contest_pairings(contest_id, stage_idx=0)[0]
        assert pairing["pairing_seed"] is None
    _clear_active_stage_manifest(store, contest_id)

    match_id = f"null-manifest-{operation}"
    if operation == "bind":
        match_config = {"time_control_id": time_control_id}
        for suffix in ("a", "b"):
            version_id = pairing[f"bot_{suffix}_version_id"]
            if version_id is not None:
                match_config[f"_bot_{suffix}_version_id"] = version_id
        store.create_match(
            match_id,
            pairing["bot_a_id"],
            pairing["bot_b_id"],
            owner_id=contest["organizer_id"],
            contest_id=contest_id,
            match_type="contest",
            game_id="holdem",
            match_config=match_config,
        )

    with pytest.raises(ValueError, match="批次|冻结|manifest|seal"):
        if operation == "dispatchable":
            store.list_dispatchable_contest_pairings(
                contest_id,
                stage_idx=0,
                due_at="2099-01-01T00:00:00",
            )
        elif operation in ("existing-seed", "markerless-seed"):
            store.ensure_contest_pairing_seed_for_enqueue(
                contest_id,
                pairing,
                expected_stages_json=contest["stages_json"],
            )
        elif operation == "bind":
            store.bind_contest_pairing_match(
                contest_id,
                pairing["id"],
                match_id,
                require_execution_admission=False,
            )
        else:
            store.adjudicate_unavailable_contest_pairing(
                contest_id,
                pairing["id"],
                match_id,
                game_id="holdem",
                winner=0,
                result={"rounds_played": 70, "deltas": [1, -1]},
                time_control_id=time_control_id,
                require_execution_admission=False,
            )

    persisted = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    assert persisted["match_id"] is None
    assert persisted["status"] == "pending"
    if operation == "bind":
        assert store.get_match(match_id) is not None
    else:
        assert store.get_match(match_id) is None
    store.close()


@pytest.mark.parametrize("operation", ["bind", "technical-adjudication"])
def test_active_execution_writers_reject_raw_null_entry_identity(
    tmp_path, operation
):
    """Effective legacy lookup cannot replace a frozen active entry id."""
    store, contest_id, _manager, _entries, _bots = _sealed_pending_current_fixture(
        tmp_path, label=f"raw-null-entry-{operation}"
    )
    contest = store.get_contest(contest_id)
    pairing = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    time_control_id = contest["time_control_id"] or "holdem_per_decision_60s_v1"
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_pairings SET entry_a_id=NULL WHERE id=?",
            (pairing["id"],),
        )
        # Model an already-sealed imported row so the identity guard, rather
        # than a stale revision, is the rejecting boundary under test.
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest_id,),
        )
    pairing = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    match_id = f"raw-null-entry-{operation}"
    if operation == "bind":
        match_config = {"time_control_id": time_control_id}
        for suffix in ("a", "b"):
            version_id = pairing[f"bot_{suffix}_version_id"]
            if version_id is not None:
                match_config[f"_bot_{suffix}_version_id"] = version_id
        store.create_match(
            match_id,
            pairing["bot_a_id"],
            pairing["bot_b_id"],
            owner_id=contest["organizer_id"],
            contest_id=contest_id,
            match_type="contest",
            game_id="holdem",
            match_config=match_config,
        )

    with pytest.raises(ValueError, match="entry|\u53c2\u8d5b\u9879|\u8eab\u4efd"):
        if operation == "bind":
            store.bind_contest_pairing_match(
                contest_id,
                pairing["id"],
                match_id,
                require_execution_admission=False,
            )
        else:
            store.adjudicate_unavailable_contest_pairing(
                contest_id,
                pairing["id"],
                match_id,
                game_id="holdem",
                winner=0,
                result={"rounds_played": 70, "deltas": [1, -1]},
                time_control_id=time_control_id,
                require_execution_admission=False,
            )

    persisted = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    assert persisted["match_id"] is None
    assert persisted["status"] == "pending"
    if operation == "bind":
        assert store.get_match(match_id) is not None
    else:
        assert store.get_match(match_id) is None
    store.close()


class _PreparedStartFailureOrch:
    def __init__(self, store: Store, contest_id: int, *, drift: bool) -> None:
        self.store = store
        self.contest_id = contest_id
        self.drift = drift
        self.discard_calls: list[str] = []

    async def challenge(self, bot_a_id, bot_b_id, owner_user_id, **kwargs):
        match_id = "prepared-start-drift"
        config = {"time_control_id": kwargs.get("time_control_id")}
        for suffix in ("a", "b"):
            version_id = kwargs.get(f"bot_{suffix}_version_id")
            if version_id is not None:
                config[f"_bot_{suffix}_version_id"] = version_id
        self.store.create_match(
            match_id,
            bot_a_id,
            bot_b_id,
            owner_id=owner_user_id,
            contest_id=self.contest_id,
            match_type="contest",
            game_id=kwargs.get("game_id") or "holdem",
            match_config=config,
        )
        return match_id

    def start_prepared_match(self, _match_id: str) -> None:
        if self.drift:
            # Drift the lifecycle epoch after bind but before compensation.
            entry = self.store.list_contest_entries(self.contest_id)[0]
            with self.store._tx() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE contest_entries SET seed=seed+1 WHERE id=?",
                    (entry["id"],),
                )
        raise RuntimeError("prepared start exploded")

    def discard_prepared_match(self, match_id: str) -> bool:
        self.discard_calls.append(match_id)
        return self.store.delete_match(match_id)


def test_failed_prepared_start_retains_match_when_unbind_revision_cas_loses(
    tmp_path
):
    """A drifted bound Match must not be deleted as though it were unowned."""
    store, contest_id, _manager, _entries, _bots = _sealed_pending_current_fixture(
        tmp_path, label="unbind-revision-drift"
    )
    contest = store.get_contest(contest_id)
    pairing = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    orch = _PreparedStartFailureOrch(store, contest_id, drift=True)
    manager = ContestManager(store, orch)

    with pytest.raises(RuntimeError, match="prepared start exploded"):
        asyncio.run(
            manager._prepare_bind_start_pairing(
                contest,
                pairing,
                gid="holdem",
                want_duplicate=False,
                activate_running=True,
            )
        )

    persisted = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    assert persisted["match_id"] == "prepared-start-drift"
    assert persisted["status"] == "running"
    assert store.get_match("prepared-start-drift") is not None
    assert orch.discard_calls == []
    store.close()


def test_failed_prepared_start_unbinds_and_discards_under_same_revision(
    tmp_path
):
    """An unchanged bind can still be compensated and retried normally."""
    store, contest_id, _manager, _entries, _bots = _sealed_pending_current_fixture(
        tmp_path, label="unbind-same-revision"
    )
    contest = store.get_contest(contest_id)
    pairing = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    orch = _PreparedStartFailureOrch(store, contest_id, drift=False)
    manager = ContestManager(store, orch)

    with pytest.raises(RuntimeError, match="prepared start exploded"):
        asyncio.run(
            manager._prepare_bind_start_pairing(
                contest,
                pairing,
                gid="holdem",
                want_duplicate=False,
                activate_running=True,
            )
        )

    persisted = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    assert persisted["match_id"] is None
    assert persisted["status"] == "pending"
    assert store.get_contest(contest_id)["status"] == "published"
    assert store.get_match("prepared-start-drift") is None
    assert orch.discard_calls == ["prepared-start-drift"]
    store.close()


def test_running_current_stage_cannot_bootstrap_a_missing_manifest(tmp_path):
    """Only the controlled publication boundary may install the first seal."""
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    for entry in entries:
        store.update_entry(contest_id, entry["user_id"], eliminated=0)
    store.update_contest(contest_id, status="running", current_stage_idx=0)
    before = store.get_contest(contest_id)

    with pytest.raises(ValueError, match="批次|冻结|manifest|seal"):
        store.create_contest_stage_pairings(
            contest_id,
            0,
            [
                {
                    "entry_a_id": entries[0]["id"],
                    "entry_b_id": entries[1]["id"],
                    "bot_a_id": entries[0]["bot_id"],
                    "bot_b_id": entries[1]["bot_id"],
                    "stage_key": "rr",
                    "published_at": "2026-01-01T00:00:00",
                }
            ],
            expected_current_stage_idx=0,
            expected_status="running",
        )

    assert store.get_contest(contest_id) == before
    assert store.list_contest_pairings(contest_id) == []
    store.close()


def test_running_null_manifest_cannot_append_dynamic_round(tmp_path):
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    for entry in entries:
        store.update_entry(contest_id, entry["user_id"], eliminated=0)
    stage = {"key": "swiss", "type": "swiss", "rounds": 3}
    store.update_contest(
        contest_id,
        status="published",
        current_stage_idx=0,
        stages_json=json.dumps([stage]),
    )
    store.create_contest_stage_pairings(
        contest_id,
        0,
        [
            {
                "entry_a_id": entries[0]["id"],
                "entry_b_id": entries[1]["id"],
                "bot_a_id": entries[0]["bot_id"],
                "bot_b_id": entries[1]["bot_id"],
                "round_num": 1,
                "stage_key": "swiss",
                "published_at": "2026-01-01T00:00:00",
            }
        ],
        expected_current_stage_idx=0,
        expected_status="published",
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status='running' WHERE id=?", (contest_id,)
        )
    _clear_active_stage_manifest(store, contest_id)
    before_pairings = store.list_contest_pairings(contest_id)

    with pytest.raises(ValueError, match="批次|冻结|manifest|seal"):
        store.append_contest_round_pairings(
            contest_id,
            0,
            [
                {
                    "entry_a_id": entries[1]["id"],
                    "entry_b_id": entries[0]["id"],
                    "bot_a_id": entries[1]["bot_id"],
                    "bot_b_id": entries[0]["bot_id"],
                    "round_num": 2,
                    "stage_key": "swiss",
                    "published_at": "2026-01-01T00:00:00",
                }
            ],
            expected_current_stage_idx=0,
            expected_previous_max_round=1,
        )

    assert store.list_contest_pairings(contest_id) == before_pairings
    store.close()


@pytest.mark.parametrize("target_status", ["rest", "finished"])
def test_generic_update_contest_cannot_enter_decision_states(
    tmp_path, target_status
):
    store, contest_id = _setup(tmp_path)
    store.update_contest(contest_id, status="running", current_stage_idx=0)
    before = store.get_contest(contest_id)

    with pytest.raises(ValueError, match="专用|事务|状态"):
        store.update_contest(contest_id, status=target_status)

    assert store.get_contest(contest_id) == before
    store.close()


def test_standalone_contest_advancement_writer_is_closed(tmp_path):
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    store.update_contest(contest_id, status="running", current_stage_idx=0)
    before = store.list_contest_entries(contest_id)
    updates = [
        {
            "id": entry["id"],
            "user_id": entry["user_id"],
            "expected_bot_id": entry["bot_id"],
            "expected_seed": entry["seed"],
            "expected_group_id": entry["group_id"],
            "expected_eliminated": entry["eliminated"],
            "seed": index + 1,
            "eliminated": 0,
        }
        for index, entry in enumerate(entries)
    ]

    with pytest.raises(ValueError, match="专用|事务|推进"):
        store.apply_contest_entry_advancement(
            contest_id,
            0,
            updates,
            expected_status="running",
            expected_current_stage_idx=0,
        )

    assert store.list_contest_entries(contest_id) == before
    store.close()
