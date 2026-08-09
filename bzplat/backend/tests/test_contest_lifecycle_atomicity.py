"""赛事 publish/start 的锁内复核与失败补偿回归。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.store import Store


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
    """challenge prepare 成功但 pairing 事务失败时，runner 不启动且 match 精确删除。"""
    async def exercise():
        store, contest_id = _setup(tmp_path)
        orch = MatchOrchestrator(store)
        manager = ContestManager(store, orch)

        def fail_bind(*args, **kwargs):
            raise RuntimeError("pairing commit exploded")

        monkeypatch.setattr(store, "bind_contest_pairing_match", fail_bind)
        with pytest.raises(RuntimeError, match="pairing commit exploded"):
            await manager.start(contest_id)

        assert store.get_contest(contest_id)["status"] == "open"
        assert store.list_contest_pairings(contest_id) == []
        assert store.list_matches(contest_id=contest_id) == []
        assert orch._tasks == {}

    asyncio.run(exercise())


def test_nth_dispatch_failure_keeps_started_progress_and_failed_pairing_retryable(
    tmp_path, monkeypatch
):
    """第 N 场失败不谎报全失败：已绑定场保留，失败场 pending 且无孤儿 match。"""
    async def exercise():
        store, contest_id = _setup(tmp_path)
        u3 = store.create_user("atomic3", "atomic3@example.com", "hash")
        b3 = store.create_bot(
            u3["id"], "atomic-bot-3",
            binary_path=_fixture_file(tmp_path, "atomic-3"),
            format="elf", game_id="holdem",
        )
        store.add_contest_entry(contest_id, u3["id"], b3["id"])
        orch = MatchOrchestrator(store)
        manager = ContestManager(store, orch)
        original_bind = store.bind_contest_pairing_match
        calls = 0

        def fail_second_bind(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("second pairing commit exploded")
            return original_bind(*args, **kwargs)

        monkeypatch.setattr(store, "bind_contest_pairing_match", fail_second_bind)
        result = await manager.start(contest_id)
        assert result["status"] == "running"

        pairings = store.list_contest_pairings(contest_id)
        assert len(pairings) == 3
        bound = [pairing for pairing in pairings if pairing.get("match_id")]
        retryable = [pairing for pairing in pairings if not pairing.get("match_id")]
        assert len(bound) == 2
        assert len(retryable) == 1
        assert retryable[0]["status"] == "pending"
        matches = store.list_matches(contest_id=contest_id)
        assert {match["id"] for match in matches} == {
            pairing["match_id"] for pairing in bound
        }
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
    assert asyncio.run(advance_once()) is True
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
    store.close()  # TEMP trigger disappears: model the recovering process.

    recovered = Store(str(db_path))
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
