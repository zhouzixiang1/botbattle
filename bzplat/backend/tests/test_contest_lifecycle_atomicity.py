"""赛事 publish/start 的锁内复核与失败补偿回归。"""
from __future__ import annotations

import asyncio
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


def test_next_stage_batch_is_atomic_and_restart_retry_replaces_legacy_partial(
    tmp_path,
):
    """第二条 INSERT 故障时整批/阶段游标回滚；重启可一次性重建完整下一阶段。"""
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

    # The first row is inserted, then SQLite aborts the second INSERT.  The Store
    # transaction must roll the first row and the state transition back together.
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_second_next_stage_pairing "
            "BEFORE INSERT ON contest_pairings "
            "WHEN NEW.stage_idx=1 AND "
            "(SELECT COUNT(*) FROM contest_pairings "
            " WHERE contest_id=NEW.contest_id AND stage_idx=1)=1 "
            "BEGIN SELECT RAISE(ABORT, 'injected stage batch failure'); END"
        )
    manager = ContestManager(store, _SuccessOrch())

    async def fail_batch():
        with pytest.raises(sqlite3.DatabaseError, match="injected stage batch failure"):
            await manager._begin_stage(
                contest_id, 1, dispatch_pending=False
            )

    asyncio.run(fail_batch())
    failed_state = store.get_contest(contest_id)
    assert failed_state["status"] == "rest"
    assert failed_state["current_stage_idx"] == 0
    assert store.list_contest_pairings(contest_id, stage_idx=1) == []
    store.close()

    # Simulate an upgrade from the old per-row implementation: a hard crash may
    # already have left one unbound next-stage row.  Restart retry replaces only
    # that safe shape and atomically advances the contest.
    recovered = Store(str(db_path))
    legacy_partial = recovered.add_contest_pairing(
        contest_id,
        bots[0]["id"],
        None,
        status="completed",
        stage_idx=1,
        stage_key="final",
        entry_a_id=recovered.list_contest_entries(contest_id)[0]["id"],
        published_at="2026-01-01T00:00:00",
    )
    retry_manager = ContestManager(recovered, _SuccessOrch())

    async def retry_after_restart():
        await retry_manager._begin_stage(
            contest_id, 1, dispatch_pending=False
        )

    asyncio.run(retry_after_restart())
    final_state = recovered.get_contest(contest_id)
    assert final_state["status"] == "running"
    assert final_state["current_stage_idx"] == 1
    pairings = recovered.list_contest_pairings(contest_id, stage_idx=1)
    assert len(pairings) == 2  # 3-player elimination: one match + one bye.
    assert legacy_partial["id"] not in {pairing["id"] for pairing in pairings}
    real = [pairing for pairing in pairings if pairing["bot_b_id"] is not None]
    byes = [pairing for pairing in pairings if pairing["bot_b_id"] is None]
    assert len(real) == 1 and len(byes) == 1
    assert real[0]["bot_a_version_id"] is not None
    assert real[0]["bot_b_version_id"] is not None
    assert real[0]["scheduled_at"] is not None
    assert byes[0]["status"] == "completed" and byes[0]["scheduled_at"] is None
    recovered.close()


def test_next_stage_recovery_does_not_overwrite_deleted_real_opponent(tmp_path):
    """A completed/no-match row with entry B is progress drift, never a safe bye."""
    store, contest_id = _setup(tmp_path)
    entries = store.list_contest_entries(contest_id)
    store.update_contest(
        contest_id,
        status="rest",
        current_stage_idx=0,
        stages_json=json.dumps(
            [
                {"key": "rr", "type": "round_robin"},
                {"key": "ko", "type": "single_elimination"},
            ]
        ),
    )
    damaged = store.add_contest_pairing(
        contest_id,
        entries[0]["bot_id"],
        entries[1]["bot_id"],
        status="completed",
        stage_idx=1,
        stage_key="ko",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    assert store.delete_bot(entries[1]["bot_id"])
    with pytest.raises(ValueError, match="已有运行进度"):
        store.create_contest_stage_pairings(
            contest_id,
            1,
            [
                {
                    "bot_a_id": entries[0]["bot_id"],
                    "bot_b_id": None,
                    "entry_a_id": entries[0]["id"],
                    "entry_b_id": None,
                    "round_num": 1,
                    "status": "completed",
                    "stage_key": "ko",
                }
            ],
            expected_current_stage_idx=0,
            activate_running=True,
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
        status="running",
        game_id="holdem",
        current_stage_idx=0,
        stages_json=json.dumps([stage]),
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest_id, user["id"], bot["id"])
    manager = ContestManager(store, _SuccessOrch())

    async def create_and_complete_first_round():
        await manager._begin_stage(
            contest_id, 0, dispatch_pending=False
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


def test_finished_unready_official_results_recover_atomically_after_restart(
    tmp_path,
):
    """正式榜第二行故障不留 partial；finished+ready=0 在启动对账后变为完整榜。"""
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
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_second_official_result "
            "BEFORE INSERT ON contest_official_results "
            "WHEN (SELECT COUNT(*) FROM contest_official_results "
            "      WHERE contest_id=NEW.contest_id)=1 "
            "BEGIN SELECT RAISE(ABORT, 'injected official result failure'); END"
        )
    manager = ContestManager(store, _SuccessOrch())
    asyncio.run(manager.maybe_finish(contest_id))

    failed_state = store.get_contest(contest_id)
    assert failed_state["status"] == "finished"
    assert failed_state["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []
    public_stage_rows = store.list_stage_results(contest_id, stage_idx=0)
    assert len(public_stage_rows) == 2
    assert all("official_rank" not in row for row in public_stage_rows)
    store.close()  # TEMP trigger disappears: model the recovering process.

    recovered = Store(str(db_path))
    assert recovered.get_match(match_id)["result"]["rounds_played"] == 70
    recovery_manager = ContestManager(recovered, _SuccessOrch())
    assert asyncio.run(recovery_manager.reconcile_running_contests()) == 1
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
    assert asyncio.run(recovery_manager.reconcile_running_contests()) == 0
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
    store.update_contest(
        contest_id, status="finished", current_stage_idx=0,
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
    assert asyncio.run(manager.reconcile_running_contests()) == 1
    blocked = store.get_contest(contest_id)
    assert blocked["status"] == "finished"
    assert blocked["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []
    # A repeated startup remains fail-closed and cannot turn the same drift
    # into a different result.
    assert asyncio.run(manager.reconcile_running_contests()) == 1
    assert store.get_contest(contest_id)["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []
    store.close()


@pytest.mark.parametrize(
    ("participant_count", "expected_ready"),
    [(0, True), (1, True), (2, False)],
)
def test_finished_unready_zero_pairing_recovery_uses_active_entry_gate(
    tmp_path, participant_count, expected_ready
):
    """An empty current stage is terminal only with at most one active entry."""
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
        status="finished",
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

    manager = ContestManager(store, _SuccessOrch())

    async def recover_concurrently():
        return await asyncio.gather(
            manager.reconcile_running_contests(),
            manager.reconcile_running_contests(),
        )

    attempts = asyncio.run(recover_concurrently())
    assert max(attempts) == 1
    recovered = store.get_contest(contest_id)
    assert recovered["official_results_ready"] == int(expected_ready)
    official = store.list_official_results(contest_id)
    if participant_count == 1:
        assert len(official) == 1
        assert official[0]["entry_id"] == entry_ids[0]
        assert official[0]["rank"] == 1
        assert official[0]["points"] == 0
    else:
        assert official == []
    assert asyncio.run(manager.reconcile_running_contests()) == (
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

    result = asyncio.run(ContestManager(store, _SuccessOrch()).maybe_finish(contest_id))
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
    store.update_contest(contest_id, status=status, current_stage_idx=0)

    manager = ContestManager(store, _SuccessOrch())
    with pytest.raises(ValueError, match="仍有未完成对阵"):
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
        status="finished",
        current_stage_idx=1,
        official_results_ready=0,
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

    manager = ContestManager(store, _SuccessOrch())
    assert asyncio.run(manager.reconcile_running_contests()) == 1
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
