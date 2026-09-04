"""启动对账（reconcile_running_contests）测试。

复现并守护修复「赛事卡 running」的三类场景：
1. match 全完成但 maybe_finish 回调丢失/被吞（生产 contest 25）→ 对账直接 finish。
2. match 被 orphan_after_restart 清成 aborted，pairing 仍指它（生产 contest 24）→
   reset_dead_contest_pairings 复位后重派完成。
3. pairing 建了 match 行但 _run_match 从未跑完（pending match）→ 识别为死 pairing 重派。
4. 双方 bot 都不可用 → 明确保持 pending 阻塞，不伪造无裁决结果。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.execution_queue import ExecutionDispatcher
from bzplat.backend.runtime.binary_runner import SandboxControlUncertain
from bzplat.backend.store import Store
from bzplat.backend.store.schema import STATUS_ABORTED, STATUS_COMPLETED
from bzplat.backend.tests.execution_helpers import (
    claim_request,
    enable_execution_queue,
)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "rc.db"))


def _mk_bots(store: Store, n: int = 4):
    fixture_dir = Path(store.path).resolve().parent / "bot-fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    users = []
    bots = []
    for i in range(n):
        u = store.create_user(f"user{i}", f"u{i}@ex.com", hash_password("password1"))
        users.append(u)
        binary_path = fixture_dir / f"fake{i}"
        binary_path.write_bytes(b"test fixture")
        b = store.create_bot(
            u["id"],
            f"bot{i}",
            binary_path=str(binary_path),
            format="elf",
            is_active=1,
            game_id="holdem",
        )
        bots.append(b)
    return users, bots


class _FakeOrch:
    """伪 orchestrator：challenge 直接建 match 行（不真跑 bot）。

    可注入 reject_bot_ids（这些 bot id 调用时抛 ValueError，模拟 bot 已删/不可用）。
    """

    def __init__(self, store: Store, *, reject_bot_ids: set[int] | None = None) -> None:
        self.store = store
        self.n = 0
        self.reject_bot_ids = reject_bot_ids or set()

    async def challenge(self, a, b, owner_user_id, *, contest_id=None, **k):
        if a in self.reject_bot_ids or b in self.reject_bot_ids:
            raise ValueError("bot 不可用（模拟已删）")
        self.n += 1
        mid = f"fake-match-{contest_id}-{self.n}"
        self.store.create_match(
            mid, a, b, owner_id=owner_user_id, contest_id=contest_id,
            match_type="contest", match_config=k.get("match_config") or {},
        )
        return mid


def _complete_all_pairs(
    store: Store, cid: int, stage_idx: int = 0, *, winner_fn=None
) -> int:
    """把某 stage 所有未完成 pairing 的 match 标完成（winner_fn(a,b)->0|1|None）。"""
    n = 0
    for p in store.list_contest_pairings(cid, stage_idx=stage_idx):
        mid = p.get("match_id")
        if not mid:
            continue
        m = store.get_match(mid)
        if not m or m["status"] in (STATUS_COMPLETED, STATUS_ABORTED):
            continue
        w = winner_fn(p["bot_a_id"], p["bot_b_id"]) if winner_fn else 0
        store.update_match(
            mid, status=STATUS_COMPLETED, winner=w,
            result={"deltas": [100 if w == 0 else (-100 if w == 1 else 0), -100 if w == 0 else (100 if w == 1 else 0)]},
        )
        store.update_contest_pairing(p["id"], status="completed")
        n += 1
    return n


# ── 1. 对账 finish：match 全完成但回调丢失（复现 contest 25）──────────────


def test_reconcile_finishes_contest_when_all_matches_done(store: Store):
    """16 人 swiss rounds=0：逐轮完成全 match（含平局），但故意不调 maybe_finish
    （模拟 on_match_done 回调丢失）。reconcile_running_contests 应直接收敛到 finished。"""
    users, bots = _mk_bots(store, 16)
    cid = store.create_contest(
        "swiss16", users[0]["id"], game_id="holdem",
        stages_json=json.dumps([{
            "key": "s", "type": "swiss", "rounds": 0,
            "scoring": "poker_3_1_0", "rest_after_minutes": 0,
        }]),
    )["id"]
    for u, b in zip(users, bots):
        store.add_contest_entry(cid, u["id"], b["id"])
    store.update_contest(cid, status="published", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0, schedule_immediately=True)
        # 逐轮：手动完成（模拟对局跑完），但全程不调 maybe_finish（模拟回调丢失）
        # 每轮完成后手动生成下一轮（绕过 maybe_finish）——复现「match 都完成了但 contest 没推进」
        import math
        total_rounds = max(1, math.ceil(math.log2(16)))  # = 4
        for rnd in range(1, total_rounds + 1):
            _complete_all_pairs(
                store, cid, 0,
                # 一半对局平局（winner=None），复现 contest 25 的 4 场平局
                winner_fn=lambda a, b: None if (a % 2 == 0) else 0,
            )
            if rnd < total_rounds:
                # 手动生成下一轮（不走 maybe_finish）
                await mgr._maybe_next_swiss_round(
                    cid, 0, {"key": "s", "type": "swiss", "rounds": total_rounds}
                )
        # 此时 4 轮全完成，但 status 仍是 running（maybe_finish 从未调用）
        assert store.get_contest(cid)["status"] == "running", "前置：对账前应卡 running"

        # 对账 → 应收敛到 finished
        n = await mgr.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
        assert n == 1, f"应处理 1 个 contest，实际 {n}"
        c = store.get_contest(cid)
        assert c["status"] == "finished", f"对账后应 finished，实际 {c['status']}"
        # official_results 应已落库
        assert c["official_results_ready"] == 1, "对账 finish 后应计算正式名次"

    asyncio.run(run())


# ── 2. 对账重派：orphan 后死 pairing 复位重派（复现 contest 24）──────────


def test_reconcile_redispatches_after_orphan_restart(store: Store):
    """match 被标 orphan_after_restart（aborted）+ pairing 仍 status=running 指它。
    对账应：reset_dead_contest_pairings 复位 → _dispatch_pending 重派 → 完成 → finish。"""
    users, bots = _mk_bots(store, 4)
    cid = store.create_contest(
        "swiss4", users[0]["id"], game_id="holdem",
        stages_json=json.dumps([{
            "key": "s", "type": "swiss", "rounds": 1,
            "scoring": "poker_3_1_0", "rest_after_minutes": 0,
        }]),
    )["id"]
    for u, b in zip(users, bots):
        store.add_contest_entry(cid, u["id"], b["id"])
    store.update_contest(cid, status="published", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0, schedule_immediately=True)
        r1 = store.list_contest_pairings(cid, stage_idx=0)
        assert len(r1) == 2
        # 模拟重启：把 R1 的 match 全标 aborted（orphan_after_restart），pairing 仍 running
        for p in r1:
            mid = p["match_id"]
            store.update_match(
                mid, status=STATUS_ABORTED, reason="orphan_after_restart",
            )
            # pairing.status 此时是 running（dispatch 时设的），模拟「状态没同步」
        assert store.get_contest(cid)["status"] == "running"

        # 对账：reset_dead 复位 pairing → 重派 → （fake orch 建新 match 行）
        n = await mgr.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
        assert n == 1
        # 对账后 pairing 应已重派（新 match_id，status=running）但 match 未完成 → 仍 running
        c = store.get_contest(cid)
        assert c["status"] == "running", "重派后 match 未完成，应仍 running"

        # 现在完成重派后的 match → 再对账一次 → 应 finished
        _complete_all_pairs(store, cid, 0, winner_fn=lambda a, b: 0)
        n2 = await mgr.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
        assert store.get_contest(cid)["status"] == "finished"

    asyncio.run(run())


@pytest.mark.parametrize(
    "interruption_reason",
    (
        "orphan_after_service_restart",
        "orphan_after_runtime_recovery",
    ),
)
def test_reconcile_marks_running_contest_match_with_explicit_recovery_source(
    store: Store,
    interruption_reason: str,
):
    """No-job contest recovery must preserve the dispatcher recovery source."""
    users, bots = _mk_bots(store, 2)
    contest = store.create_contest(
        f"source-aware-{interruption_reason}",
        users[0]["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    pairing = store.list_contest_pairings(contest["id"], stage_idx=0)[0]
    match_id = f"dead-{interruption_reason}"
    store.create_match(
        match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=users[0]["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(match_id, status="running")
    store.bind_contest_pairing_match(
        contest["id"],
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    assert asyncio.run(
        manager.reconcile_running_contests(
            interruption_reason=interruption_reason
        )
    ) == 1
    recovered = store.get_match(match_id)
    assert recovered is not None
    assert recovered["status"] == STATUS_ABORTED
    assert recovered["reason"] == interruption_reason


def test_contest_recovery_rejects_unknown_reason_before_any_write(store: Store):
    """Legacy/free-form recovery values cannot mutate contest state."""
    users, bots = _mk_bots(store, 2)
    contest = store.create_contest(
        "reject-unknown-recovery",
        users[0]["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    pairing = store.list_contest_pairings(contest["id"], stage_idx=0)[0]
    match_id = "reject-unknown-recovery-match"
    store.create_match(
        match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=users[0]["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(match_id, status="running")
    store.bind_contest_pairing_match(
        contest["id"],
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    before = (
        store.get_contest(contest["id"]),
        store.list_contest_pairings(contest["id"]),
        store.get_match(match_id),
    )

    with pytest.raises(ValueError, match="recovery reason"):
        asyncio.run(
            manager.reconcile_running_contests(
                interruption_reason="orphan_after_restart"
            )
        )
    assert (
        store.get_contest(contest["id"]),
        store.list_contest_pairings(contest["id"]),
        store.get_match(match_id),
    ) == before

    with pytest.raises(ValueError, match="recovery reason"):
        store.reset_dead_contest_pairings(
            interruption_reason="free_form_recovery_reason"
        )
    assert (
        store.get_contest(contest["id"]),
        store.list_contest_pairings(contest["id"]),
        store.get_match(match_id),
    ) == before


def test_delayed_dispatcher_resume_runs_full_contest_recovery(store: Store):
    """A later successful Docker resume repairs both dead and unready contests."""
    users, bots = _mk_bots(store, 4)

    dead_contest = store.create_contest(
        "delayed-resume-dead",
        users[0]["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    [
        store.add_contest_entry(dead_contest["id"], users[i]["id"], bots[i]["id"])
        for i in range(2)
    ]
    setup_manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    asyncio.run(
        setup_manager._begin_stage(
            dead_contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    dead_pairing = store.list_contest_pairings(
        dead_contest["id"], stage_idx=0
    )[0]
    dead_match_id = "delayed-resume-dead-match"
    store.create_match(
        dead_match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=users[0]["id"],
        contest_id=dead_contest["id"],
        match_type="contest",
        game_id="holdem",
    )
    store.bind_contest_pairing_match(
        dead_contest["id"], dead_pairing["id"], dead_match_id,
        require_execution_admission=False,
    )

    finished_contest = store.create_contest(
        "delayed-resume-unready",
        users[2]["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    [
        store.add_contest_entry(
            finished_contest["id"], users[i]["id"], bots[i]["id"]
        )
        for i in range(2, 4)
    ]
    asyncio.run(
        setup_manager._begin_stage(
            finished_contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    finished_pairing = store.list_contest_pairings(
        finished_contest["id"], stage_idx=0
    )[0]
    finished_match_id = "delayed-resume-finished-match"
    store.create_match(
        finished_match_id,
        bots[2]["id"],
        bots[3]["id"],
        owner_id=users[2]["id"],
        contest_id=finished_contest["id"],
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(
        finished_match_id,
        status=STATUS_COMPLETED,
        winner=0,
        reason="completed",
        result={
            "rounds_played": 70,
            "deltas": [1, -1],
            "normalized_delta": 0.01,
        },
    )
    store.bind_contest_pairing_match(
        finished_contest["id"], finished_pairing["id"], finished_match_id,
        require_execution_admission=False,
    )
    store.update_contest_pairing(finished_pairing["id"], status="completed")
    setup_manager._ensure_stage_decision(finished_contest["id"], 0)
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status='finished',official_results_ready=0 "
            "WHERE id=?",
            (finished_contest["id"],),
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (finished_contest["id"],),
        )
    assert store.get_contest(finished_contest["id"])["official_results_ready"] == 0

    class RecoveryRuntime:
        supervisor = None

        def __init__(self) -> None:
            self.cleanup_calls = 0

        async def cleanup_instance(self) -> None:
            self.cleanup_calls += 1
            if self.cleanup_calls == 1:
                raise SandboxControlUncertain("first cleanup remains uncertain")

        async def ensure_runtime_ready(self) -> None:
            return None

    class RecoveryOrchestrator(_FakeOrch):
        def __init__(self, target_store: Store, runtime: RecoveryRuntime) -> None:
            super().__init__(target_store)
            self.runner = SimpleNamespace(runner=runtime)

        async def quiesce_execution_tasks(self) -> None:
            return None

        async def recover_unsettled_match_ratings(self) -> int:
            return 0

    runtime = RecoveryRuntime()
    orch = RecoveryOrchestrator(store, runtime)
    manager = ContestManager(store, orch)  # type: ignore[arg-type]
    dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=2,
        max_sandbox_units=4,
        auto_capability_enabled=False,
        contest_reconciler=manager.reconcile_running_contests,
    )
    store.executions.pause("forced recovery test", bounded_retry=False)

    async def run() -> None:
        assert not await dispatcher.admin_resume()
        assert (
            store.list_contest_pairings(dead_contest["id"])[0]["match_id"]
            == dead_match_id
        )
        assert (
            store.get_contest(finished_contest["id"])["official_results_ready"]
            == 0
        )

        assert await dispatcher.admin_resume()
        repaired_pairing = store.list_contest_pairings(dead_contest["id"])[0]
        assert repaired_pairing["status"] == "running"
        assert repaired_pairing["match_id"] not in {None, dead_match_id}
        repaired_finished = store.get_contest(finished_contest["id"])
        assert repaired_finished["official_results_ready"] == 1
        assert len(store.list_official_results(finished_contest["id"])) == 2

    asyncio.run(run())
    assert runtime.cleanup_calls == 2


def test_reconcile_deletes_bound_prepared_ghost_before_redispatch(store: Store):
    """claim 已提交但 task 未启动：namespace recovery 原子删旧 attempt 并重派。"""
    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    users, bots = _mk_bots(store, 2)
    cid = store.create_contest(
        "prepared-crash", users[0]["id"], game_id="holdem",
        stages_json=json.dumps([{
            "key": "rr", "type": "round_robin", "rest_after_minutes": 0,
        }]),
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(cid, user["id"], bot["id"])
    store.update_contest(cid, status="published", current_stage_idx=0)

    async def run():
        original_orch = MatchOrchestrator(store)
        setup_manager = ContestManager(store, original_orch)
        await setup_manager._begin_stage(
            cid,
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
        pairing = store.list_contest_pairings(cid, stage_idx=0)[0]
        enable_execution_queue(store)
        request_id = await original_orch.challenge(
            bots[0]["id"], bots[1]["id"], users[0]["id"],
            contest_id=cid, match_type="contest", game_id="holdem",
            contest_pairing_id=pairing["id"],
        )
        claimed = claim_request(original_orch, request_id, start=False)
        ghost_id = claimed["current_match_id"]
        assert store.get_match(ghost_id)["status"] == "pending"
        assert store.get_replay(ghost_id) is not None
        assert store.list_contest_pairings(cid)[0]["match_id"] == ghost_id

        recovered = store.executions.recover_after_namespace_cleanup(
            interruption_reason="orphan_after_service_restart"
        )
        assert recovered["requeued"] == 1

        assert store.get_match(ghost_id) is None
        assert store.get_replay(ghost_id) is None
        index = store._conn.execute(
            "SELECT 1 FROM matches_index WHERE id=?", (ghost_id,)
        ).fetchone()
        assert index is None
        reset = store.list_contest_pairings(cid, stage_idx=0)[0]
        assert reset["status"] == "pending"
        assert reset["match_id"] is None

        replacement = claim_request(original_orch, request_id, start=False)
        refreshed = store.list_contest_pairings(cid, stage_idx=0)[0]
        assert refreshed["match_id"] == replacement["current_match_id"]
        assert refreshed["match_id"] != ghost_id
        live_matches = store.list_matches(contest_id=cid)
        assert {match["id"] for match in live_matches} == {refreshed["match_id"]}

    asyncio.run(run())


def test_reconcile_deletes_unbound_prepared_ghost_before_redispatch(store: Store):
    """enqueue 与 claim 之间不创建 match，消除旧 prepare-before-bind ghost 窗口。"""
    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    users, bots = _mk_bots(store, 2)
    cid = store.create_contest(
        "prepare-before-bind-crash",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps(
            [{"key": "rr", "type": "round_robin", "rest_after_minutes": 0}]
        ),
    )["id"]
    entries = []
    for user, bot in zip(users, bots):
        entries.append(store.add_contest_entry(cid, user["id"], bot["id"]))
    store.update_contest(cid, status="published", current_stage_idx=0)
    async def run():
        old_orchestrator = MatchOrchestrator(store)
        setup_manager = ContestManager(store, old_orchestrator)
        await setup_manager._begin_stage(
            cid,
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
        pairing = store.list_contest_pairings(cid, stage_idx=0)[0]
        enable_execution_queue(store)
        request_id = await old_orchestrator.challenge(
            bots[0]["id"],
            bots[1]["id"],
            users[0]["id"],
            contest_id=cid,
            match_type="contest",
            game_id="holdem",
            contest_pairing_id=pairing["id"],
        )
        request = store.executions.get(request_id)
        assert request and request["status"] == "queued"
        assert request["current_match_id"] is None
        assert store.list_matches(contest_id=cid) == []
        assert store.list_contest_pairings(cid)[0]["match_id"] is None

        claimed = claim_request(old_orchestrator, request_id, start=False)
        match_id = claimed["current_match_id"]
        assert store.get_match(match_id)["status"] == "pending"
        assert store.get_replay(match_id) is not None
        refreshed = store.list_contest_pairings(cid)[0]
        assert refreshed["id"] == pairing["id"]
        assert refreshed["status"] == "running"
        assert refreshed["match_id"] == match_id
        assert {row["id"] for row in store.list_matches(contest_id=cid)} == {
            match_id
        }

    asyncio.run(run())


def test_reconcile_preserves_partial_unbound_published_first_stage(store: Store):
    """published 残缺批次缺乏完整权威，启动对账必须原样阻断。"""
    users, bots = _mk_bots(store, 4)
    cid = store.create_contest(
        "partial-published",
        users[0]["id"],
        status="published",
        starts_at="2099-01-01T00:00:00",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )["id"]
    entries = [
        store.add_contest_entry(cid, user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    partial = store.add_contest_pairing(
        cid,
        bots[0]["id"],
        bots[3]["id"],
        stage_idx=0,
        stage_key="rr",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[3]["id"],
        published_at="2026-01-01T00:00:00",
        scheduled_at="2099-01-01T00:00:00",
    )
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]

    async def run():
        with pytest.raises(ValueError, match="不完整|证据"):
            await manager.ensure_published_pairings(cid, 0)
        assert await manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        ) == 1
        preserved = store.list_contest_pairings(cid, stage_idx=0)
        assert [row["id"] for row in preserved] == [partial["id"]]
        assert preserved[0]["match_id"] is None
        assert preserved[0]["scheduled_at"] == "2099-01-01T00:00:00"
        assert store.get_contest(cid)["status"] == "published"
        assert store.get_contest(cid)["published_stage_pairing_count"] is None

    asyncio.run(run())


@pytest.mark.parametrize(
    "corruption",
    [
        "missing-version",
        "missing-published-at",
        "garbage-published-at",
        "empty-scheduled-at",
        "garbage-scheduled-at",
        "inactive-bot",
        "stale-version",
    ],
)
def test_published_legacy_batch_seal_rejects_noncanonical_rows(
    store: Store, corruption: str
):
    """A complete topology alone cannot bless stale execution identities."""
    users, bots = _mk_bots(store, 2)
    cid = store.create_contest(
        f"published-corrupt-{corruption}",
        users[0]["id"],
        status="published",
        starts_at="2099-01-01T00:00:00",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(cid, user["id"], bot["id"])
    for index, bot in enumerate(bots):
        version_path = Path(store.path).resolve().parent / f"base-version-{index}"
        version_path.write_bytes(b"base version")
        store.add_bot_version(
            bot["id"], binary_path=str(version_path), version=1
        )
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    asyncio.run(manager._begin_stage(cid, 0, dispatch_pending=False))
    pairing = store.list_contest_pairings(cid, stage_idx=0)[0]

    if corruption == "missing-version":
        with store._tx() as connection:
            connection.execute(
                "UPDATE contest_pairings SET bot_a_version_id=NULL WHERE id=?",
                (pairing["id"],),
            )
    elif corruption in {"missing-published-at", "garbage-published-at"}:
        with store._tx() as connection:
            connection.execute(
                "UPDATE contest_pairings SET published_at=? WHERE id=?",
                (
                    None if corruption == "missing-published-at" else "zzzz",
                    pairing["id"],
                ),
            )
    elif corruption in {"empty-scheduled-at", "garbage-scheduled-at"}:
        with store._tx() as connection:
            connection.execute(
                "UPDATE contest_pairings SET scheduled_at=? WHERE id=?",
                (
                    "" if corruption == "empty-scheduled-at" else "zzzz",
                    pairing["id"],
                ),
            )
    elif corruption == "inactive-bot":
        store.update_bot(bots[0]["id"], is_active=0)
    else:
        version_path = Path(store.path).resolve().parent / "late-version"
        version_path.write_bytes(b"late version")
        store.add_bot_version(
            bots[0]["id"], binary_path=str(version_path), version=2
        )

    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET published_stage_pairing_count=NULL,"
            "sealed_pairing_topology_revision=NULL WHERE id=?",
            (cid,),
        )
    before = store.list_contest_pairings(cid, stage_idx=0)
    with pytest.raises(ValueError, match="版本|可用|停用|发布|完整|规范|时间"):
        asyncio.run(manager.ensure_published_pairings(cid, 0))
    assert store.list_contest_pairings(cid, stage_idx=0) == before
    state = store.get_contest(cid)
    assert state["published_stage_pairing_count"] is None


@pytest.mark.parametrize("scheduled_at", [None, "2099-01-01T00:00:00"])
def test_published_legacy_complete_canonical_batch_installs_only_seal(
    store: Store, scheduled_at: str | None
):
    """A complete canonical legacy batch is preserved byte-for-byte and sealed."""
    users, bots = _mk_bots(store, 2)
    cid = store.create_contest(
        "published-complete-legacy",
        users[0]["id"],
        status="published",
        starts_at="2099-01-01T00:00:00",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(cid, user["id"], bot["id"])
    for index, bot in enumerate(bots):
        version_path = Path(store.path).resolve().parent / f"legacy-version-{index}"
        version_path.write_bytes(b"legacy version")
        store.add_bot_version(
            bot["id"], binary_path=str(version_path), version=1
        )
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    asyncio.run(manager._begin_stage(cid, 0, dispatch_pending=False))
    pairing = store.list_contest_pairings(cid, stage_idx=0)[0]
    store.update_contest_pairing(pairing["id"], scheduled_at=scheduled_at)
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET published_stage_pairing_count=NULL,"
            "sealed_pairing_topology_revision=NULL WHERE id=?",
            (cid,),
        )
    before = store.list_contest_pairings(cid, stage_idx=0)

    asyncio.run(manager.ensure_published_pairings(cid, 0))

    assert store.list_contest_pairings(cid, stage_idx=0) == before
    state = store.contest_projection_snapshot(cid)["contest"]
    assert state["published_stage_pairing_count"] == len(before)
    assert state["pairing_topology_revision"] == state[
        "sealed_pairing_topology_revision"
    ]


@pytest.mark.parametrize("scheduled_at", ["", "zzzz", True, 1])
def test_generic_pairing_schedule_update_rejects_noncanonical_time(
    store: Store, scheduled_at: object
):
    users, bots = _mk_bots(store, 2)
    cid = store.create_contest(
        "pairing-time-guard", users[0]["id"], game_id="holdem"
    )["id"]
    pairing = store.add_contest_pairing(
        cid, bots[0]["id"], bots[1]["id"], scheduled_at=None
    )

    with pytest.raises(ValueError, match="时间|ISO"):
        store.update_contest_pairing(
            pairing["id"], scheduled_at=scheduled_at
        )


@pytest.mark.parametrize("artifact_drift", ["missing", "tampered"])
def test_published_legacy_batch_seal_requires_intact_frozen_artifact(
    store: Store, artifact_drift: str
):
    """A canonical DB shape cannot certify a missing/corrupt Bot artifact."""
    users, bots = _mk_bots(store, 2)
    cid = store.create_contest(
        f"published-artifact-{artifact_drift}",
        users[0]["id"],
        status="published",
        starts_at="2099-01-01T00:00:00",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(cid, user["id"], bot["id"])
    for index, bot in enumerate(bots):
        version_path = Path(store.path).resolve().parent / f"artifact-v{index}"
        payload = b"frozen artifact"
        version_path.write_bytes(payload)
        store.add_bot_version(
            bot["id"],
            binary_path=str(version_path),
            version=1,
            checksum=hashlib.sha256(payload).hexdigest(),
            size_bytes=len(payload),
        )
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    asyncio.run(manager._begin_stage(cid, 0, dispatch_pending=False))
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET published_stage_pairing_count=NULL,"
            "sealed_pairing_topology_revision=NULL WHERE id=?",
            (cid,),
        )
    frozen = store.get_current_bot_version(bots[0]["id"])
    assert frozen is not None
    artifact = Path(str(frozen["binary_path"]))
    if artifact_drift == "missing":
        artifact.unlink()
    else:
        artifact.write_bytes(b"tampered artifact")
    before = store.list_contest_pairings(cid, stage_idx=0)

    with pytest.raises(ValueError, match="version_unavailable|完整性|版本"):
        asyncio.run(manager.ensure_published_pairings(cid, 0))

    assert store.list_contest_pairings(cid, stage_idx=0) == before
    assert store.get_contest(cid)["published_stage_pairing_count"] is None


def test_published_partial_batch_with_bound_match_reports_inconsistency(store: Store):
    """残缺 published 批次若已绑定/active，不能覆盖真实进度静默重建。"""
    users, bots = _mk_bots(store, 4)
    cid = store.create_contest(
        "partial-bound",
        users[0]["id"],
        status="published",
        starts_at="2099-01-01T00:00:00",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )["id"]
    [
        store.add_contest_entry(cid, user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    asyncio.run(manager._begin_stage(cid, 0, dispatch_pending=False))
    complete_batch = store.list_contest_pairings(cid, stage_idx=0)
    pairing = complete_batch[0]
    store.create_match(
        "partial-bound-active",
        pairing["bot_a_id"],
        pairing["bot_b_id"],
        owner_id=users[0]["id"],
        contest_id=cid,
        match_type="contest",
    )
    store.bind_contest_pairing_match(
        cid,
        pairing["id"],
        "partial-bound-active",
        require_execution_admission=False,
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM contest_pairings WHERE contest_id=? AND id<>?",
            (cid, pairing["id"]),
        )
        connection.execute(
            "UPDATE contests SET published_stage_pairing_count=NULL,"
            "sealed_pairing_topology_revision=NULL WHERE id=?",
            (cid,),
        )

    async def run():
        with pytest.raises(ValueError, match="已绑定|不一致|active|证据不完整"):
            await manager.ensure_published_pairings(cid, 0)
        preserved = store.list_contest_pairings(cid, stage_idx=0)
        assert len(preserved) == 1
        assert preserved[0]["match_id"] == "partial-bound-active"

    asyncio.run(run())


# ── 3. bot 不可用 → pairing 标 aborted，阶段仍推进 ──────────────────────


def test_reconcile_both_bots_unavailable_blocks_pairing(store: Store):
    """死 pairing 重派时双方 Bot 都不可用，必须保留 pending 供人工修复。"""
    users, bots = _mk_bots(store, 4)
    cid = store.create_contest(
        "swiss4deadbot", users[0]["id"], game_id="holdem",
        stages_json=json.dumps([{
            "key": "s", "type": "swiss", "rounds": 1,
            "scoring": "poker_3_1_0", "rest_after_minutes": 0,
        }]),
    )["id"]
    for u, b in zip(users, bots):
        store.add_contest_entry(cid, u["id"], b["id"])
    store.update_contest(cid, status="published", current_stage_idx=0)

    # 初始正常派发；之后将全部 Bot 停用模拟重启后名册已不可用。
    orch = _FakeOrch(store, reject_bot_ids=set())
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0, schedule_immediately=True)
        for bot in bots:
            store.update_bot(bot["id"], is_active=0)
        # 把已派的 match 全标 aborted（模拟孤儿）→ pairing 复位
        for p in store.list_contest_pairings(cid, stage_idx=0):
            store.update_match(p["match_id"], status=STATUS_ABORTED, reason="orphan_after_restart")
        # 对账：旧 aborted 历史保留，pairing 复位后因双方不可用而阻塞。
        n = await mgr.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
        assert n == 1
        c = store.get_contest(cid)
        assert c["status"] == "running"
        pairings = store.list_contest_pairings(cid, stage_idx=0)
        assert all(p["status"] == "pending" and p["match_id"] is None for p in pairings)
        assert all(
            m["status"] == STATUS_ABORTED
            for m in store.list_matches(contest_id=cid)
        )

    asyncio.run(run())


# ── 4. reset_dead_contest_pairings 不误伤 completed pairing ─────────────


@pytest.mark.parametrize("terminal_status", ["finished", "cancelled"])
@pytest.mark.parametrize(
    "match_state", ["completed", "aborted", "pending", "missing"]
)
def test_reconcile_never_repairs_finished_ready_pairing_artifacts(
    store: Store, match_state: str, terminal_status: str
):
    """Terminal history is immutable even when an old running binding is bad."""
    users, bots = _mk_bots(store, 2)
    contest = store.create_contest(
        f"{terminal_status}-{match_state}",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    match_id = f"finished-ready-{match_state}"
    store.create_match(
        match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=users[0]["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
    )
    store.upsert_replay(
        match_id,
        json.dumps([{"type": "fixture", "state": match_state}]),
    )
    pairing = store.add_contest_pairing(
        contest["id"],
        bots[0]["id"],
        bots[1]["id"],
        match_id=match_id,
        status="running",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        published_at="2026-09-03T00:00:00",
        scheduled_at="2026-09-03T00:00:00",
    )
    if match_state == "completed":
        store.update_match(
            match_id,
            status=STATUS_COMPLETED,
            winner=0,
            reason="score",
            result={"rounds_played": 1, "deltas": [9, -9]},
            started_at="2026-09-02T23:59:00",
            ended_at="2026-09-03T00:00:00",
        )
    elif match_state == "aborted":
        store.update_match(
            match_id,
            status=STATUS_ABORTED,
            reason="historical_terminal_fixture",
        )
    elif match_state == "missing":
        # Preserve index/replay/policy sidecars around a physically missing
        # legacy Match so reconciliation proves it does not clean terminal data.
        with store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM matches_holdem WHERE id=?", (match_id,)
            )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status=?,official_results_ready=?,"
            "ends_at='2026-09-03T00:00:00' WHERE id=?",
            (
                terminal_status,
                1 if terminal_status == "finished" else 0,
                contest["id"],
            ),
        )

    def snapshot() -> dict:
        with store._tx() as connection:
            def one(sql: str, params: tuple = ()):
                row = connection.execute(sql, params).fetchone()
                return dict(row) if row is not None else None

            return {
                "contest": one(
                    "SELECT * FROM contests WHERE id=?", (contest["id"],)
                ),
                "pairing": one(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing["id"],)
                ),
                "match": one(
                    "SELECT * FROM matches_holdem WHERE id=?", (match_id,)
                ),
                "index": one(
                    "SELECT * FROM matches_index WHERE id=?", (match_id,)
                ),
                "replay": one(
                    "SELECT * FROM match_replays WHERE match_id=?", (match_id,)
                ),
                "policy": one(
                    "SELECT * FROM match_rating_policies WHERE match_id=?",
                    (match_id,),
                ),
            }

    before = snapshot()
    assert before["contest"]["status"] == terminal_status
    assert before["contest"]["official_results_ready"] == (
        1 if terminal_status == "finished" else 0
    )
    assert before["pairing"]["status"] == "running"
    assert before["pairing"]["match_id"] == match_id
    assert before["index"] is not None
    assert before["replay"] is not None
    assert before["policy"] is not None
    if match_state == "missing":
        assert before["match"] is None

    processed = asyncio.run(
        ContestManager(store, _FakeOrch(store)).reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    )

    assert processed == 0
    assert snapshot() == before
    if match_state == "completed":
        assert store.complete_contest_pairing_for_match(
            contest["id"], match_id
        ) is None
        assert store.backfill_contest_actual_start(contest["id"]) is None
        assert snapshot() == before
    elif match_state == "aborted":
        assert store.reset_aborted_contest_pairing(
            contest["id"], match_id
        ) is None
        assert snapshot() == before


@pytest.mark.parametrize("recovery_entrypoint", ["start", "resume"])
@pytest.mark.parametrize("terminal_status", ["finished", "cancelled"])
@pytest.mark.parametrize(
    ("match_state", "match_type", "retain_match_contest_id"),
    [
        ("pending", "contest", True),
        ("running", "contest", True),
        ("pending", "challenge", True),
        ("running", "challenge", True),
        ("running", "challenge", False),
    ],
)
def test_dispatcher_application_recovery_preserves_terminal_match_artifacts(
    store: Store,
    terminal_status: str,
    recovery_entrypoint: str,
    match_state: str,
    match_type: str,
    retain_match_contest_id: bool,
):
    """Namespace recovery must not rewrite terminal contest history first."""
    users, bots = _mk_bots(store, 2)
    contest = store.create_contest(
        f"dispatcher-{recovery_entrypoint}-{terminal_status}",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    match_id = (
        f"dispatcher-{recovery_entrypoint}-{terminal_status}-"
        f"{match_state}-{match_type}"
    )
    store.create_match(
        match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=users[0]["id"],
        contest_id=contest["id"],
        match_type=match_type,
        game_id="holdem",
    )
    if match_state == "running":
        store.update_match(match_id, status="running")
    store.upsert_replay(
        match_id,
        json.dumps([{"type": "historical_pending", "status": terminal_status}]),
    )
    pairing = store.add_contest_pairing(
        contest["id"],
        bots[0]["id"],
        bots[1]["id"],
        match_id=match_id,
        status="running",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        published_at="2026-09-03T00:00:00",
        scheduled_at="2026-09-03T00:00:00",
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not retain_match_contest_id:
            connection.execute(
                "UPDATE matches_holdem SET contest_id=NULL WHERE id=?",
                (match_id,),
            )
        connection.execute(
            "UPDATE contests SET status=?,official_results_ready=?,"
            "ends_at='2026-09-03T00:00:00' WHERE id=?",
            (
                terminal_status,
                1 if terminal_status == "finished" else 0,
                contest["id"],
            ),
        )

    def snapshot() -> dict:
        with store._tx() as connection:
            def one(sql: str, params: tuple = ()):
                row = connection.execute(sql, params).fetchone()
                return dict(row) if row is not None else None

            return {
                "contest": one(
                    "SELECT * FROM contests WHERE id=?", (contest["id"],)
                ),
                "pairing": one(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing["id"],)
                ),
                "match": one(
                    "SELECT * FROM matches_holdem WHERE id=?", (match_id,)
                ),
                "index": one(
                    "SELECT * FROM matches_index WHERE id=?", (match_id,)
                ),
                "replay": one(
                    "SELECT * FROM match_replays WHERE match_id=?", (match_id,)
                ),
                "policy": one(
                    "SELECT * FROM match_rating_policies WHERE match_id=?",
                    (match_id,),
                ),
            }

    before = snapshot()
    assert before["match"]["status"] == match_state
    assert before["pairing"]["status"] == "running"

    class RecoveryRuntime:
        supervisor = None

        async def cleanup_instance(self) -> None:
            return None

        async def ensure_runtime_ready(self) -> None:
            return None

    class RecoveryOrchestrator(_FakeOrch):
        def __init__(self, target_store: Store) -> None:
            super().__init__(target_store)
            self.runner = SimpleNamespace(runner=RecoveryRuntime())

        async def quiesce_execution_tasks(self) -> None:
            return None

        async def recover_unsettled_match_ratings(self) -> int:
            return 0

    orchestrator = RecoveryOrchestrator(store)
    manager = ContestManager(store, orchestrator)  # type: ignore[arg-type]
    dispatcher = ExecutionDispatcher(
        orchestrator,
        store,
        max_match_slots=1,
        max_sandbox_units=2,
        auto_capability_enabled=False,
        contest_reconciler=manager.reconcile_running_contests,
    )

    async def recover() -> dict:
        if recovery_entrypoint == "start":
            started = await dispatcher.start()
            try:
                assert started["outcome"] == "running"
                return started["application_recovered"]
            finally:
                await dispatcher.close()
        store.executions.resume()
        return await dispatcher._recover_application_state_after_resume(
            interruption_reason="orphan_after_runtime_recovery"
        )

    recovered = asyncio.run(recover())
    assert recovered["legacy_orphans"] == 0
    assert snapshot() == before


@pytest.mark.parametrize(
    ("match_state", "match_type", "expected_reason"),
    [
        ("running", "contest", "orphan_after_service_restart"),
        ("running", "challenge", "orphan_after_service_restart"),
        ("pending", "contest", "orphan_pending_no_contest"),
        (
            "pending",
            "challenge",
            "orphan_pending_after_service_restart",
        ),
    ],
)
def test_application_recovery_still_aborts_genuine_noncontest_orphan(
    store: Store,
    match_state: str,
    match_type: str,
    expected_reason: str,
):
    """A Match without either contest reference remains recoverable."""
    users, bots = _mk_bots(store, 2)
    match_id = f"genuine-noncontest-{match_state}-{match_type}-orphan"
    store.create_match(
        match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=users[0]["id"],
        match_type=match_type,
        game_id="holdem",
    )
    if match_state == "running":
        store.update_match(match_id, status="running")
    store.upsert_replay(
        match_id,
        json.dumps([{"type": f"historical_{match_state}"}]),
    )

    assert store.recover_orphan_matches(
        interruption_reason="orphan_after_service_restart"
    ) == 1
    recovered = store.get_match(match_id)
    assert (recovered["status"], recovered["reason"]) == (
        STATUS_ABORTED,
        expected_reason,
    )
    assert json.loads(store.get_replay(match_id)["events_json"])[-1] == {
        "type": "error",
        "reason": expected_reason,
    }


@pytest.mark.parametrize("active_status", ["published", "running", "rest"])
def test_reset_dead_contest_pairings_recovers_drifted_unbound_active_match(
    store: Store, active_status: str
):
    """An active contest_id, not stale match_type, owns an unbound Match."""
    users, bots = _mk_bots(store, 2)
    contest = store.create_contest(
        f"active-unbound-{active_status}",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    match_id = f"active-unbound-{active_status}"
    store.create_match(
        match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=users[0]["id"],
        contest_id=contest["id"],
        match_type="challenge",
        game_id="holdem",
    )
    store.upsert_replay(match_id, json.dumps([{"type": "prepared"}]))
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status=? WHERE id=?",
            (active_status, contest["id"]),
        )

    assert store.recover_orphan_matches(
        interruption_reason="orphan_after_service_restart"
    ) == 0
    assert store.reset_dead_contest_pairings(
        interruption_reason="orphan_after_service_restart"
    ) == 1
    assert store.get_match(match_id) is None
    with store._tx() as connection:
        assert connection.execute(
            "SELECT 1 FROM matches_index WHERE id=?", (match_id,)
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM match_replays WHERE match_id=?", (match_id,)
        ).fetchone() is None
        assert connection.execute(
            "SELECT 1 FROM match_rating_policies WHERE match_id=?", (match_id,)
        ).fetchone() is None


@pytest.mark.parametrize("match_state", ["pending", "running"])
@pytest.mark.parametrize(
    "other_contest_kind",
    ["finished", "cancelled", "showcase", "active_other"],
)
def test_application_recovery_rejects_conflicting_contest_affiliations(
    store: Store,
    match_state: str,
    other_contest_kind: str,
):
    """All direct and pairing contest references must identify one active cup."""
    users, bots = _mk_bots(store, 2)
    pairing_contest = store.create_contest(
        f"pairing-owner-{match_state}-{other_contest_kind}",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    direct_contest = store.create_contest(
        f"match-owner-{match_state}-{other_contest_kind}",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    entries = [
        store.add_contest_entry(pairing_contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    match_id = f"conflicting-{match_state}-{other_contest_kind}"
    store.create_match(
        match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=users[0]["id"],
        contest_id=direct_contest["id"],
        match_type="challenge",
        game_id="holdem",
    )
    if match_state == "running":
        store.update_match(match_id, status="running")
    store.upsert_replay(match_id, json.dumps([{"type": "historical_event"}]))
    pairing = store.add_contest_pairing(
        pairing_contest["id"],
        bots[0]["id"],
        bots[1]["id"],
        match_id=match_id,
        status="running",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        published_at="2026-09-03T00:00:00",
        scheduled_at="2026-09-03T00:00:00",
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status='running' WHERE id=?",
            (pairing_contest["id"],),
        )
        if other_contest_kind == "showcase":
            connection.execute(
                "UPDATE contests SET status='running',showcase_key=? WHERE id=?",
                (f"conflicting-{match_id}", direct_contest["id"]),
            )
        else:
            connection.execute(
                "UPDATE contests SET status=? WHERE id=?",
                (
                    "running"
                    if other_contest_kind == "active_other"
                    else other_contest_kind,
                    direct_contest["id"],
                ),
            )

    def snapshot() -> dict:
        with store._tx() as connection:
            def one(sql: str, params: tuple = ()):
                row = connection.execute(sql, params).fetchone()
                return dict(row) if row is not None else None

            return {
                "contests": [
                    dict(row)
                    for row in connection.execute(
                        "SELECT * FROM contests WHERE id IN (?,?) ORDER BY id",
                        (pairing_contest["id"], direct_contest["id"]),
                    ).fetchall()
                ],
                "pairing": one(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing["id"],)
                ),
                "match": one(
                    "SELECT * FROM matches_holdem WHERE id=?", (match_id,)
                ),
                "index": one("SELECT * FROM matches_index WHERE id=?", (match_id,)),
                "replay": one(
                    "SELECT * FROM match_replays WHERE match_id=?", (match_id,)
                ),
                "policy": one(
                    "SELECT * FROM match_rating_policies WHERE match_id=?",
                    (match_id,),
                ),
            }

    before = snapshot()
    orphaned = store.recover_orphan_matches(
        interruption_reason="orphan_after_service_restart"
    )
    reset = store.reset_dead_contest_pairings(
        interruption_reason="orphan_after_service_restart"
    )
    assert (orphaned, reset) == (0, 0)
    assert snapshot() == before


@pytest.mark.parametrize("job_status", ["starting", "running", "settling"])
@pytest.mark.parametrize("affiliation_shape", ["no_ref", "ghost", "bound"])
def test_application_recovery_preserves_match_owned_by_active_execution_job(
    store: Store,
    job_status: str,
    affiliation_shape: str,
):
    """Orphan scans must never rewrite the current Match of an active job."""
    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    users, bots = _mk_bots(store, 2)
    orchestrator = MatchOrchestrator(store)
    enable_execution_queue(store)

    async def claim() -> dict:
        request_id = await orchestrator.challenge(
            bots[0]["id"],
            bots[1]["id"],
            users[0]["id"],
            match_type="challenge",
            game_id="holdem",
        )
        return claim_request(orchestrator, request_id, start=False)

    claimed = asyncio.run(claim())
    match_id = str(claimed["current_match_id"])
    contest = None
    pairing = None
    if affiliation_shape != "no_ref":
        contest = store.create_contest(
            f"job-owned-{affiliation_shape}-{job_status}",
            users[0]["id"],
            game_id="holdem",
            stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
        )
        entries = [
            store.add_contest_entry(contest["id"], user["id"], bot["id"])
            for user, bot in zip(users, bots)
        ]
        if affiliation_shape == "bound":
            pairing = store.add_contest_pairing(
                contest["id"],
                bots[0]["id"],
                bots[1]["id"],
                match_id=match_id,
                status="running",
                stage_idx=0,
                stage_key="rr",
                entry_a_id=entries[0]["id"],
                entry_b_id=entries[1]["id"],
                published_at="2026-09-03T00:00:00",
                scheduled_at="2026-09-03T00:00:00",
            )
    now = "2026-09-03T00:00:00"
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE matches_holdem SET match_type='contest',contest_id=? WHERE id=?",
            (contest["id"] if contest is not None else None, match_id),
        )
        if contest is not None:
            connection.execute(
                "UPDATE contests SET status='running' WHERE id=?", (contest["id"],)
            )
        if job_status == "running":
            connection.execute(
                "UPDATE execution_jobs SET status='running',started_at=? WHERE id=?",
                (now, claimed["id"]),
            )
        elif job_status == "settling":
            connection.execute(
                "UPDATE execution_jobs SET status='settling',started_at=?,"
                "settling_at=?,cleanup_state='pending' WHERE id=?",
                (now, now, claimed["id"]),
            )
        if job_status != "starting":
            connection.execute(
                "UPDATE execution_job_attempts SET status=?,started_at=? "
                "WHERE job_id=? AND attempt_no=?",
                (
                    job_status,
                    now,
                    claimed["id"],
                    claimed["attempt_count"],
                ),
            )

    def snapshot() -> dict:
        with store._tx() as connection:
            def one(sql: str, params: tuple = ()):
                row = connection.execute(sql, params).fetchone()
                return dict(row) if row is not None else None

            return {
                "contest": (
                    one("SELECT * FROM contests WHERE id=?", (contest["id"],))
                    if contest is not None
                    else None
                ),
                "pairing": (
                    one("SELECT * FROM contest_pairings WHERE id=?", (pairing["id"],))
                    if pairing is not None
                    else None
                ),
                "match": one(
                    "SELECT * FROM matches_holdem WHERE id=?", (match_id,)
                ),
                "index": one("SELECT * FROM matches_index WHERE id=?", (match_id,)),
                "replay": one(
                    "SELECT * FROM match_replays WHERE match_id=?", (match_id,)
                ),
                "policy": one(
                    "SELECT * FROM match_rating_policies WHERE match_id=?",
                    (match_id,),
                ),
                "job": one(
                    "SELECT * FROM execution_jobs WHERE id=?", (claimed["id"],)
                ),
                "attempt": one(
                    "SELECT * FROM execution_job_attempts "
                    "WHERE job_id=? AND attempt_no=?",
                    (claimed["id"], claimed["attempt_count"]),
                ),
            }

    before = snapshot()
    orphaned = store.recover_orphan_matches(
        interruption_reason="orphan_after_service_restart"
    )
    reset = store.reset_dead_contest_pairings(
        interruption_reason="orphan_after_service_restart"
    )
    assert (orphaned, reset) == (0, 0)
    assert snapshot() == before


@pytest.mark.parametrize("active_status", ["published", "running", "rest"])
@pytest.mark.parametrize("match_type", ["contest", "challenge"])
def test_reset_dead_contest_pairings_repairs_each_active_status(
    store: Store, active_status: str, match_type: str
):
    """The terminal-history guard must not disable active contest recovery."""
    users, bots = _mk_bots(store, 2)
    contest = store.create_contest(
        f"active-recovery-{active_status}",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    match_id = f"active-recovery-{active_status}"
    store.create_match(
        match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=users[0]["id"],
        contest_id=contest["id"],
        match_type=match_type,
        game_id="holdem",
    )
    store.update_match(match_id, status="running")
    pairing = store.add_contest_pairing(
        contest["id"],
        bots[0]["id"],
        bots[1]["id"],
        match_id=match_id,
        status="running",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        published_at="2026-09-03T00:00:00",
        scheduled_at="2026-09-03T00:00:00",
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status=? WHERE id=?",
            (active_status, contest["id"]),
        )

    assert store.recover_orphan_matches(
        interruption_reason="orphan_after_service_restart"
    ) == 1
    recovered_match = store.get_match(match_id)
    assert (recovered_match["status"], recovered_match["reason"]) == (
        STATUS_ABORTED,
        "orphan_after_service_restart",
    )
    assert store.reset_dead_contest_pairings(
        interruption_reason="orphan_after_service_restart"
    ) == 1
    repaired = next(
        row
        for row in store.list_contest_pairings(contest["id"])
        if row["id"] == pairing["id"]
    )
    assert (repaired["status"], repaired["match_id"]) == ("pending", None)
    assert store.get_match(match_id) == recovered_match


def test_reset_dead_contest_pairings_preserves_completed(store: Store):
    """completed pairing（match 已完成）不应被 reset 重置。"""
    users, bots = _mk_bots(store, 4)
    cid = store.create_contest(
        "preserve", users[0]["id"], game_id="holdem",
        stages_json=json.dumps([{
            "key": "s", "type": "swiss", "rounds": 1,
            "scoring": "poker_3_1_0", "rest_after_minutes": 0,
        }]),
    )["id"]
    for u, b in zip(users, bots):
        store.add_contest_entry(cid, u["id"], b["id"])
    store.update_contest(cid, status="published", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0, schedule_immediately=True)
        # 完成全部 R1
        _complete_all_pairs(store, cid, 0, winner_fn=lambda a, b: 0)
        # 记录完成态
        before = store.list_contest_pairings(cid, stage_idx=0)
        assert all(p["status"] == "completed" for p in before)

        # reset 不应动 completed pairing
        reset_n = store.reset_dead_contest_pairings(
            interruption_reason="orphan_after_service_restart"
        )
        assert reset_n == 0, "无死 pairing 时应重置 0 行"
        after = store.list_contest_pairings(cid, stage_idx=0)
        for p_a, p_b in zip(before, after):
            assert p_a["status"] == p_b["status"] == "completed"
            assert p_a["match_id"] == p_b["match_id"], "completed 的 match_id 不应被清空"

    asyncio.run(run())


def test_manager_sync_repairs_completed_replay_before_pairing_leaves_running(
    store: Store,
):
    """The real reconcile ordering must not hide a missing terminal replay."""
    users, bots = _mk_bots(store, 2)
    cid = store.create_contest(
        "completed replay sync",
        users[0]["id"],
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
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(cid, user["id"], bot["id"])
    store.update_contest(cid, status="published", current_stage_idx=0)
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]

    async def run() -> None:
        await manager._begin_stage(cid, 0, schedule_immediately=True)
        pairing = store.list_contest_pairings(cid, stage_idx=0)[0]
        match_id = pairing["match_id"]
        assert pairing["status"] == "running" and match_id
        store.update_match(
            match_id,
            status=STATUS_COMPLETED,
            winner=0,
            reason="score",
            result={"deltas": [7, -7]},
            ended_at="2026-09-02T10:00:00",
        )
        assert store.get_replay(match_id) is None

        await manager.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )

        events = json.loads(store.get_replay(match_id)["events_json"])
        assert events == [
            {
                "type": "match_end",
                "winner": 0,
                "reason": "score",
                "deltas": [7, -7],
            }
        ]
        assert store.list_contest_pairings(cid, stage_idx=0)[0]["status"] == (
            "completed"
        )

    asyncio.run(run())


def test_stage_done_pairing_marker_uses_atomic_completion_boundary(store: Store):
    users, bots = _mk_bots(store, 2)
    cid = store.create_contest(
        "stage marker replay",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps(
            [{"key": "rr", "type": "round_robin", "scoring": "poker_3_1_0"}]
        ),
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(cid, user["id"], bot["id"])
    store.update_contest(cid, status="published", current_stage_idx=0)
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]

    async def run() -> None:
        await manager._begin_stage(cid, 0, schedule_immediately=True)
        pairing = store.list_contest_pairings(cid, stage_idx=0)[0]
        match_id = pairing["match_id"]
        store.update_match(
            match_id,
            status=STATUS_COMPLETED,
            winner=1,
            reason="score",
            result={"deltas": [-8, 8]},
        )
        assert store.get_replay(match_id) is None

        manager._mark_stage_pairings_done(cid, 0)

        assert store.list_contest_pairings(cid, stage_idx=0)[0]["status"] == (
            "completed"
        )
        assert json.loads(store.get_replay(match_id)["events_json"])[-1] == {
            "type": "match_end",
            "winner": 1,
            "reason": "score",
            "deltas": [-8, 8],
        }

    asyncio.run(run())


# ── 5. pairing 状态同步：maybe_finish 完成阶段后 pairing.status='completed' ─


def test_pairing_status_synced_to_completed(store: Store):
    """maybe_finish 推进阶段时，已完成 match 的 pairing 应标 status='completed'
    （观测性修复：原只在 dispatch 设 running、从不收尾，对阵图永显 running）。"""
    users, bots = _mk_bots(store, 4)
    cid = store.create_contest(
        "sync", users[0]["id"], game_id="holdem",
        stages_json=json.dumps([{
            "key": "s", "type": "swiss", "rounds": 1,
            "scoring": "poker_3_1_0", "rest_after_minutes": 0,
        }]),
    )["id"]
    for u, b in zip(users, bots):
        store.add_contest_entry(cid, u["id"], b["id"])
    store.update_contest(cid, status="published", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0, schedule_immediately=True)
        _complete_all_pairs(store, cid, 0, winner_fn=lambda a, b: 0)
        # 对账让阶段完成
        await mgr.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
        # 单阶段赛事完成后 pairing 应标 completed
        pairings = store.list_contest_pairings(cid, stage_idx=0)
        assert all(p["status"] == "completed" for p in pairings), (
            f"阶段完成后 pairing 应标 completed，实际 {[p['status'] for p in pairings]}"
        )
        assert store.get_contest(cid)["status"] == "finished"

    asyncio.run(run())
