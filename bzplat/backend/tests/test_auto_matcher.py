"""持久、公平、全局串行自动排位队列的定向不变量测试。"""
from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.auto_matcher import AutoMatchScheduler
from bzplat.backend.store import Store


@pytest.fixture
def store(tmp_path):
    instance = Store(str(tmp_path / "auto.db"))
    _mark_projection_verified(instance)
    yield instance
    instance.close()


def _mark_projection_verified(store: Store) -> None:
    with store._tx() as conn:
        source = conn.execute(
            "SELECT COUNT(*) AS n,COALESCE(MAX(settled_order),0) AS last_order "
            "FROM match_rating_settlements WHERE settled_order>0"
        ).fetchone()
        conn.execute(
            "UPDATE rating_projection_state SET policy_version='owner-neutral-v2',"
            "rebuilt_at='test',source_settlement_count=?,"
            "source_last_settled_order=? WHERE singleton=1",
            (int(source["n"]), int(source["last_order"])),
        )


def _mk_bot(
    store: Store,
    key: str,
    *,
    game_id: str = "holdem",
    owner_id: int | None = None,
    matches_played: int = 0,
) -> dict:
    if owner_id is None:
        user = store.create_user(
            f"user-{key}", f"{key}@example.com", hash_password("password1")
        )
        owner_id = int(user["id"])
    path = f"/tmp/{key}.elf"
    bot = store.create_bot(
        owner_id,
        f"bot-{key}",
        binary_path=path,
        format="elf",
        game_id=game_id,
    )
    store.add_bot_version(bot["id"], binary_path=path)
    store.ensure_rating(bot["id"], game_id=game_id)
    if matches_played:
        store.update_rating_row(
            bot["id"], game_id=game_id, matches_played=matches_played
        )
    return store.get_bot(bot["id"])


def _leader(store: Store, token: str = "test-leader") -> str:
    _mark_projection_verified(store)
    assert store.acquire_auto_match_dispatcher(token, lease_seconds=30)["owned"]
    return token


class _FakeOrchestrator:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.max_concurrent = 2
        self.admitted: set[str] = set()
        self.started: list[str] = []
        self.fail_start = fail_start

    def available_bot_slots(self) -> int:
        return self.max_concurrent - len(self.admitted)

    def reserve_prepared_match_slot(
        self, match_id: str, *, keep_free: int = 0
    ) -> None:
        if self.available_bot_slots() <= keep_free:
            raise RuntimeError("full")
        self.admitted.add(match_id)

    def release_prepared_match_slot(self, match_id: str) -> None:
        self.admitted.discard(match_id)

    def start_prepared_match(self, match_id: str) -> None:
        if self.fail_start:
            raise RuntimeError("create_task failed")
        self.started.append(match_id)

    async def recover_unsettled_match_ratings(self) -> int:
        return 0


def test_scheduler_does_not_lose_wake_arriving_during_dispatch_turn():
    class WakeDuringRunScheduler(AutoMatchScheduler):
        def __init__(self):
            super().__init__(_FakeOrchestrator(), object())
            self.calls = 0

        async def run_once(self) -> dict:
            self.calls += 1
            if self.calls == 1:
                self.wake()
                return {"outcome": "first"}
            raise asyncio.CancelledError

    async def exercise():
        scheduler = WakeDuringRunScheduler()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(scheduler.loop(), timeout=0.2)
        assert scheduler.calls == 2

    asyncio.run(exercise())


def test_migration_removes_daily_truth_and_legacy_switch_cannot_override(tmp_path):
    path = str(tmp_path / "migration.db")
    first = Store(path)
    first.set_setting("auto_match_enabled", "0")
    first.set_setting("auto_match_daily_cap", "2")
    with first._tx() as conn:
        conn.execute(
            "CREATE TABLE auto_match_daily_claims(match_id TEXT PRIMARY KEY)"
        )
    first.close()

    migrated = Store(path)
    assert migrated.get_auto_match_enabled() is True
    assert migrated.get_setting("auto_match_enabled") is None
    assert migrated.get_setting("auto_match_daily_cap") is None
    assert migrated._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='auto_match_daily_claims'"
    ).fetchone() is None
    migrated.set_auto_match_enabled(False)
    migrated.close()

    reopened = Store(path)
    assert reopened.get_auto_match_enabled() is False
    assert reopened._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()


def test_migration_freezes_legacy_same_owner_truth_without_replaying_ratings(tmp_path):
    path = str(tmp_path / "legacy-rating.db")
    legacy = Store(path)
    owner = legacy.create_user(
        "legacy-owner", "legacy-owner@example.com", hash_password("password1")
    )
    bot_a = _mk_bot(legacy, "legacy-a", owner_id=owner["id"])
    bot_b = _mk_bot(legacy, "legacy-b", owner_id=owner["id"])
    legacy.create_match("legacy-same-owner", bot_a["id"], bot_b["id"])
    legacy.update_match(
        "legacy-same-owner",
        status="completed",
        winner=0,
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
        ended_at="2026-08-01T12:00:00",
    )
    legacy.mark_match_rating_settled("legacy-same-owner")
    legacy.update_rating_row(bot_a["id"], rating=1777.0, matches_played=9)
    # Simulate a v1 database, which had no frozen policy row.
    with legacy._tx() as conn:
        conn.execute("DROP TRIGGER trg_match_rating_policy_settled_delete")
        conn.execute("DROP TRIGGER trg_match_rating_policy_source_immutable")
        conn.execute(
            "DELETE FROM match_rating_policies WHERE match_id='legacy-same-owner'"
        )
    legacy.close()

    migrated = Store(path)
    assert migrated.get_rating(bot_a["id"])["rating"] == 1777.0
    assert migrated.get_rating(bot_a["id"])["matches_played"] == 9
    policy = migrated._conn.execute(
        "SELECT rated,rating_reason,source FROM match_rating_policies "
        "WHERE match_id='legacy-same-owner'"
    ).fetchone()
    assert tuple(policy) == (0, "same_owner", "legacy_migration")
    audit = migrated.rating_integrity_diagnostics()
    assert audit["rebuild_required"] is True
    assert audit["legacy_same_owner_settled_count"] == 1
    assert audit["affected_bot_ids"] == sorted([bot_a["id"], bot_b["id"]])
    assert [row["id"] for row in audit["matches"]] == ["legacy-same-owner"]
    migrated.close()


def test_unverified_rating_projection_pauses_refill_and_claim(tmp_path):
    store = Store(str(tmp_path / "unverified.db"))
    _mk_bot(store, "unverified-a")
    _mk_bot(store, "unverified-b")
    token = "unverified-leader"
    assert store.acquire_auto_match_dispatcher(token, lease_seconds=30)["owned"]
    refill = store.refill_auto_match_queue(
        target_queued=1, placement_required=10, dispatcher_token=token
    )
    assert refill["outcome"] == "rating_unverified"
    assert store.claim_next_auto_match(
        "must-not-exist", dispatcher_token=token
    )["outcome"] == "rating_unverified"
    assert store.get_match("must-not-exist") is None
    snapshot = AutoMatchScheduler(_FakeOrchestrator(), store).public_snapshot()
    assert snapshot["paused"] is True
    assert snapshot["rating_projection_ready"] is False
    assert "排行榜投影" in snapshot["pause_reason"]
    store.close()


def test_game_cursor_and_lane_are_persistent_and_fixed(store: Store):
    for game in ("gomoku", "holdem", "pencil"):
        _mk_bot(store, f"{game}-p1", game_id=game)
        _mk_bot(store, f"{game}-p2", game_id=game)
    token = _leader(store)

    result = store.refill_auto_match_queue(
        target_queued=3, placement_required=10, dispatcher_token=token
    )
    assert result["inserted"] == 3
    rows = store.list_auto_match_queue()
    assert {row["game_id"] for row in rows} == {"gomoku", "holdem", "pencil"}
    assert [row["requested_lane"] for row in rows] == [
        "placement", "formal", "placement"
    ]
    # With no formal pool, the formal quota is filled but explicitly audited.
    assert rows[1]["actual_lane"] == "placement"
    assert "requested_lane_empty" in rows[1]["fallback_reason"]

    state = store.get_auto_match_fair_state()
    reopened = Store(store.path)
    assert reopened.get_auto_match_fair_state()["revision"] == state["revision"]
    reopened.close()


def test_placement_and_formal_lanes_alternate_without_starvation(store: Store):
    placement = [_mk_bot(store, f"p{i}") for i in range(2)]
    formal = [_mk_bot(store, f"f{i}", matches_played=20) for i in range(2)]
    token = _leader(store)

    store.refill_auto_match_queue(
        target_queued=2, placement_required=10, dispatcher_token=token
    )
    rows = store.list_auto_match_queue()
    assert [row["actual_lane"] for row in rows] == ["placement", "formal"]
    assert {rows[0]["bot_a_id"], rows[0]["bot_b_id"]} == {
        placement[0]["id"], placement[1]["id"]
    }
    assert {rows[1]["bot_a_id"], rows[1]["bot_b_id"]} == {
        formal[0]["id"], formal[1]["id"]
    }


def test_single_placement_owner_widens_only_to_formal_other_owner(store: Store):
    placement = _mk_bot(store, "only-placement")
    formal = _mk_bot(store, "formal-partner", matches_played=20)
    token = _leader(store)
    store.refill_auto_match_queue(
        target_queued=1, placement_required=10, dispatcher_token=token
    )
    row = store.list_auto_match_queue()[0]
    assert {row["bot_a_id"], row["bot_b_id"]} == {
        placement["id"], formal["id"]
    }
    assert row["fallback_reason"] == "single_placement_owner"


def test_owner_with_many_bots_has_only_one_global_queue_share(store: Store):
    attacker = store.create_user(
        "attacker", "attacker@example.com", hash_password("password1")
    )
    for index in range(20):
        _mk_bot(store, f"attack-{index}", owner_id=attacker["id"])
    for index in range(6):
        _mk_bot(store, f"honest-{index}")
    token = _leader(store)
    store.refill_auto_match_queue(
        target_queued=4, placement_required=10, dispatcher_token=token
    )

    rows = store.list_auto_match_queue()
    attacker_ids = {
        bot["id"] for bot in store.list_bots(owner_id=attacker["id"], active_only=True)
    }
    appearances = sum(
        int(row["bot_a_id"] in attacker_ids) + int(row["bot_b_id"] in attacker_ids)
        for row in rows
    )
    assert appearances == 1
    owners = []
    for row in rows:
        owners.extend((row["bot_a_owner"], row["bot_b_owner"]))
    assert len(owners) == len(set(owners))


def test_multi_store_claim_is_exactly_one_and_atomic(tmp_path):
    path = str(tmp_path / "claim.db")
    setup = Store(path)
    for index in range(8):
        _mk_bot(setup, f"claim-{index}")
    token = _leader(setup, "shared-process-token")
    setup.refill_auto_match_queue(
        target_queued=4, placement_required=10, dispatcher_token=token
    )
    setup.close()

    stores = [Store(path) for _ in range(8)]
    barrier = Barrier(len(stores))

    def claim(index: int) -> str:
        barrier.wait()
        return stores[index].claim_next_auto_match(
            f"concurrent-{index}", dispatcher_token=token
        )["outcome"]

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            outcomes = list(pool.map(claim, range(len(stores))))
        assert outcomes.count("claimed") == 1
        assert outcomes.count("busy") == 7
        row = stores[0]._conn.execute(
            "SELECT match_id FROM auto_match_queue WHERE status='dispatched'"
        ).fetchone()
        assert row is not None
        match_id = row["match_id"]
        assert stores[0].get_match(match_id)["status"] == "pending"
        assert stores[0]._conn.execute(
            "SELECT 1 FROM matches_index WHERE id=?", (match_id,)
        ).fetchone()
        assert stores[0]._conn.execute(
            "SELECT 1 FROM match_replays WHERE match_id=?", (match_id,)
        ).fetchone()
    finally:
        for instance in stores:
            instance.close()


def test_switch_off_commit_blocks_refill_and_claim_but_keeps_upcoming(tmp_path):
    path = str(tmp_path / "switch.db")
    setup = Store(path)
    for index in range(4):
        _mk_bot(setup, f"switch-{index}")
    token = _leader(setup)
    setup.refill_auto_match_queue(
        target_queued=2, placement_required=10, dispatcher_token=token
    )
    setup.close()

    toggler = Store(path)
    claimer = Store(path)
    committed = Event()

    def turn_off() -> None:
        toggler.set_auto_match_enabled(False)
        committed.set()

    def claim_after_commit() -> dict:
        committed.wait(timeout=5)
        return claimer.claim_next_auto_match(
            "after-off", dispatcher_token=token
        )

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            off_future = pool.submit(turn_off)
            claim_future = pool.submit(claim_after_commit)
            off_future.result()
            result = claim_future.result()
        assert result["outcome"] == "disabled"
        assert claimer.get_match("after-off") is None
        assert len(claimer.list_auto_match_queue()) == 2
        assert claimer.refill_auto_match_queue(
            target_queued=3, placement_required=10, dispatcher_token=token
        )["outcome"] == "disabled"
        toggler.set_auto_match_enabled(True)
        assert claimer.claim_next_auto_match(
            "after-on", dispatcher_token=token
        )["outcome"] == "claimed"
    finally:
        toggler.close()
        claimer.close()


def test_scheduler_start_failure_restores_queue_without_garbage_match(store: Store):
    _mk_bot(store, "rollback-a")
    _mk_bot(store, "rollback-b")
    orch = _FakeOrchestrator(fail_start=True)
    scheduler = AutoMatchScheduler(orch, store)

    result = asyncio.run(scheduler.run_once())
    assert result["outcome"] == "start_failure"
    assert orch.admitted == set()
    rows = store.list_auto_match_queue()
    assert rows[0]["status"] == "queued"
    assert store.get_match(result["match_id"]) is None
    assert store._conn.execute(
        "SELECT 1 FROM matches_index WHERE id=?", (result["match_id"],)
    ).fetchone() is None
    assert store._conn.execute(
        "SELECT 1 FROM match_replays WHERE match_id=?", (result["match_id"],)
    ).fetchone() is None
    assert store._conn.execute(
        "SELECT 1 FROM match_rating_policies WHERE match_id=?", (result["match_id"],)
    ).fetchone() is None
    decision = store._conn.execute(
        "SELECT lifecycle,attempt_count,last_attempt_error FROM auto_match_decisions"
    ).fetchone()
    assert tuple(decision) == ("queued", 1, "start_failure")
    assert store.get_auto_match_fair_state()["platform_failures"] == 1


def test_live_dispatcher_lease_prevents_other_process_recovery(store: Store):
    _mk_bot(store, "lease-a")
    _mk_bot(store, "lease-b")
    token_a = _leader(store, "leader-a")
    store.refill_auto_match_queue(
        target_queued=1, placement_required=10, dispatcher_token=token_a
    )
    claimed = store.claim_next_auto_match("leased", dispatcher_token=token_a)
    assert claimed["outcome"] == "claimed"

    contender = Store(store.path)
    try:
        lease = contender.acquire_auto_match_dispatcher("leader-b", lease_seconds=30)
        assert lease["owned"] is False
        assert contender.recover_auto_match_dispatcher_takeover(
            dispatcher_token="leader-b"
        )["outcome"] == "not_leader"
        assert contender.get_match("leased")["status"] == "pending"
    finally:
        contender.close()


def test_platform_abort_is_zero_service_and_persistently_backed_off(store: Store):
    bots = [_mk_bot(store, f"platform-{index}") for index in range(2)]
    token = _leader(store)
    store.refill_auto_match_queue(
        target_queued=1, placement_required=10, dispatcher_token=token
    )
    assert store.claim_next_auto_match(
        "platform-abort", dispatcher_token=token
    )["outcome"] == "claimed"
    store.update_match(
        "platform-abort", status="aborted", reason="platform_error", ended_at="2026-08-10T12:00:00"
    )
    assert store.reconcile_auto_match_queue(
        dispatcher_token=token
    )["removed_terminal"] == 1
    assert store.list_auto_match_queue() == []
    assert store._conn.execute(
        "SELECT COALESCE(SUM(served_count),0) FROM auto_match_bot_service "
        "WHERE bot_id IN (?,?)", (bots[0]["id"], bots[1]["id"])
    ).fetchone()[0] == 0
    state = store.get_auto_match_fair_state()
    assert state["platform_failures"] == 1
    assert state["not_before"] is not None
    assert store.refill_auto_match_queue(
        target_queued=1, placement_required=10, dispatcher_token=token
    )["outcome"] == "backoff"


def test_completed_settled_terminal_updates_auto_service_once(store: Store):
    bots = [_mk_bot(store, f"settled-{index}") for index in range(2)]
    token = _leader(store)
    store.refill_auto_match_queue(
        target_queued=1, placement_required=10, dispatcher_token=token
    )
    store.claim_next_auto_match("settled", dispatcher_token=token)
    store.update_match(
        "settled",
        status="completed",
        reason="technical_loss",
        winner=1,
        result={"rounds_played": 0, "deltas": [-1, 1], "normalized_delta": -1},
        technical_loss=1,
        ended_at="2026-08-10T12:00:00",
    )
    assert store.reconcile_auto_match_queue(
        dispatcher_token=token
    )["waiting_settlement"] == 1
    assert len(store.list_auto_match_queue()) == 1
    store.mark_match_rating_settled("settled")
    assert store.reconcile_auto_match_queue(
        dispatcher_token=token
    )["removed_terminal"] == 1
    assert store.reconcile_auto_match_queue(
        dispatcher_token=token
    )["removed_terminal"] == 0
    counts = store._conn.execute(
        "SELECT bot_id,served_count FROM auto_match_bot_service "
        "WHERE bot_id IN (?,?) ORDER BY bot_id", (bots[0]["id"], bots[1]["id"])
    ).fetchall()
    assert [row["served_count"] for row in counts] == [1, 1]
    decision = store._conn.execute(
        "SELECT lifecycle,terminal_reason FROM auto_match_decisions"
    ).fetchone()
    assert tuple(decision) == ("completed", "technical_loss")


def test_frozen_queue_version_is_restrict_deleted(store: Store):
    bots = [_mk_bot(store, f"version-{index}") for index in range(2)]
    token = _leader(store)
    store.refill_auto_match_queue(
        target_queued=1, placement_required=10, dispatcher_token=token
    )
    queued = store.list_auto_match_queue()[0]
    version = store.get_bot_version(queued["bot_a_version_id"])
    with pytest.raises(ValueError, match="自动排位队列"):
        store.delete_bot_version(version["bot_id"], version["version"])
    assert len(store.list_auto_match_queue()) == 1


def test_database_trigger_rejects_rated_overlap_until_settlement(store: Store):
    first = _mk_bot(store, "overlap-a")
    second = _mk_bot(store, "overlap-b")
    third = _mk_bot(store, "overlap-c")
    store.create_match("first", first["id"], second["id"], game_id="holdem")

    config = '{"_rating_eligible":true,"_rating_reason":"eligible"}'
    with pytest.raises(sqlite3.IntegrityError, match="rated match lifecycle overlap"):
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO matches_holdem("
                "id,bot_a_id,bot_b_id,match_type,status,game_id,match_config,created_at) "
                "VALUES('second',?,?,'challenge','pending','holdem',?,?)",
                (first["id"], third["id"], config, "2026-08-10T12:00:00"),
            )

    store.update_match(
        "first", status="completed", winner=0,
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
        ended_at="2026-08-10T12:00:00",
    )
    with pytest.raises(ValueError, match="计分对局"):
        store.create_match("blocked", first["id"], third["id"], game_id="holdem")
    store.mark_match_rating_settled("first")
    assert store.create_match(
        "after-settlement", first["id"], third["id"], game_id="holdem"
    )["id"] == "after-settlement"
