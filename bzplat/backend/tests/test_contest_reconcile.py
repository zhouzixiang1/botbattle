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
    store.update_contest(cid, status="running", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0)
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
        n = await mgr.reconcile_running_contests()
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
    store.update_contest(cid, status="running", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0)
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
        n = await mgr.reconcile_running_contests()
        assert n == 1
        # 对账后 pairing 应已重派（新 match_id，status=running）但 match 未完成 → 仍 running
        c = store.get_contest(cid)
        assert c["status"] == "running", "重派后 match 未完成，应仍 running"

        # 现在完成重派后的 match → 再对账一次 → 应 finished
        _complete_all_pairs(store, cid, 0, winner_fn=lambda a, b: 0)
        n2 = await mgr.reconcile_running_contests()
        assert store.get_contest(cid)["status"] == "finished"

    asyncio.run(run())


def test_delayed_dispatcher_resume_runs_full_contest_recovery(store: Store):
    """A later successful Docker resume repairs both dead and unready contests."""
    users, bots = _mk_bots(store, 4)

    dead_contest = store.create_contest(
        "delayed-resume-dead",
        users[0]["id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    dead_entries = [
        store.add_contest_entry(dead_contest["id"], users[i]["id"], bots[i]["id"])
        for i in range(2)
    ]
    dead_pairing = store.add_contest_pairing(
        dead_contest["id"],
        bots[0]["id"],
        bots[1]["id"],
        status="pending",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=dead_entries[0]["id"],
        entry_b_id=dead_entries[1]["id"],
    )
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
        status="running",
        game_id="holdem",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    finished_entries = [
        store.add_contest_entry(
            finished_contest["id"], users[i]["id"], bots[i]["id"]
        )
        for i in range(2, 4)
    ]
    finished_pairing = store.add_contest_pairing(
        finished_contest["id"],
        bots[2]["id"],
        bots[3]["id"],
        status="pending",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=finished_entries[0]["id"],
        entry_b_id=finished_entries[1]["id"],
    )
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
            "rounds_played": 1,
            "deltas": [1, -1],
            "normalized_delta": 1,
        },
    )
    store.bind_contest_pairing_match(
        finished_contest["id"], finished_pairing["id"], finished_match_id,
        require_execution_admission=False,
    )
    store.update_contest_pairing(finished_pairing["id"], status="completed")
    store.update_contest(
        finished_contest["id"], status="finished", official_results_ready=0
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
    store.update_contest(cid, status="running", current_stage_idx=0)

    async def run():
        original_orch = MatchOrchestrator(store)
        pairing = store.add_contest_pairing(
            cid, bots[0]["id"], bots[1]["id"], status="pending", stage_idx=0,
        )
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

        recovered = store.executions.recover_after_namespace_cleanup()
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
    store.update_contest(cid, status="running", current_stage_idx=0)
    pairing = store.add_contest_pairing(
        cid,
        bots[0]["id"],
        bots[1]["id"],
        status="pending",
        stage_idx=0,
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )

    async def run():
        old_orchestrator = MatchOrchestrator(store)
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


def test_reconcile_rebuilds_partial_unbound_published_first_stage(store: Store):
    """published 首阶段只持久化部分 pairing 就硬崩，启动对账重建完整批次。"""
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
        assert await manager.reconcile_running_contests() == 1
        rebuilt = store.list_contest_pairings(cid, stage_idx=0)
        assert len(rebuilt) == 6
        assert partial["id"] not in {row["id"] for row in rebuilt}
        assert all(
            row["status"] == "pending"
            and row["match_id"] is None
            and row["scheduled_at"] == "2099-01-01T00:00:00"
            for row in rebuilt
        )
        assert store.get_contest(cid)["status"] == "published"

    asyncio.run(run())


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
    entries = [
        store.add_contest_entry(cid, user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    pairing = store.add_contest_pairing(
        cid,
        bots[0]["id"],
        bots[3]["id"],
        status="pending",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[3]["id"],
    )
    store.create_match(
        "partial-bound-active",
        bots[0]["id"],
        bots[3]["id"],
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
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]

    async def run():
        with pytest.raises(ValueError, match="已绑定|不一致|active"):
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
    store.update_contest(cid, status="running", current_stage_idx=0)

    # 初始正常派发；之后将全部 Bot 停用模拟重启后名册已不可用。
    orch = _FakeOrch(store, reject_bot_ids=set())
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0)
        for bot in bots:
            store.update_bot(bot["id"], is_active=0)
        # 把已派的 match 全标 aborted（模拟孤儿）→ pairing 复位
        for p in store.list_contest_pairings(cid, stage_idx=0):
            store.update_match(p["match_id"], status=STATUS_ABORTED, reason="orphan_after_restart")
        # 对账：旧 aborted 历史保留，pairing 复位后因双方不可用而阻塞。
        n = await mgr.reconcile_running_contests()
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
    store.update_contest(cid, status="running", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0)
        # 完成全部 R1
        _complete_all_pairs(store, cid, 0, winner_fn=lambda a, b: 0)
        # 记录完成态
        before = store.list_contest_pairings(cid, stage_idx=0)
        assert all(p["status"] == "completed" for p in before)

        # reset 不应动 completed pairing
        reset_n = store.reset_dead_contest_pairings()
        assert reset_n == 0, "无死 pairing 时应重置 0 行"
        after = store.list_contest_pairings(cid, stage_idx=0)
        for p_a, p_b in zip(before, after):
            assert p_a["status"] == p_b["status"] == "completed"
            assert p_a["match_id"] == p_b["match_id"], "completed 的 match_id 不应被清空"

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
    store.update_contest(cid, status="running", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0)
        _complete_all_pairs(store, cid, 0, winner_fn=lambda a, b: 0)
        # 对账让阶段完成
        await mgr.reconcile_running_contests()
        # 单阶段赛事完成后 pairing 应标 completed
        pairings = store.list_contest_pairings(cid, stage_idx=0)
        assert all(p["status"] == "completed" for p in pairings), (
            f"阶段完成后 pairing 应标 completed，实际 {[p['status'] for p in pairings]}"
        )
        assert store.get_contest(cid)["status"] == "finished"

    asyncio.run(run())
