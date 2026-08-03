"""启动对账（reconcile_running_contests）测试。

复现并守护修复「赛事卡 running」的三类场景：
1. match 全完成但 maybe_finish 回调丢失/被吞（生产 contest 25）→ 对账直接 finish。
2. match 被 orphan_after_restart 清成 aborted，pairing 仍指它（生产 contest 24）→
   reset_dead_contest_pairings 复位后重派完成。
3. pairing 建了 match 行但 _run_match 从未跑完（pending match）→ 识别为死 pairing 重派。
4. bot 已删/不可用 → 重派抛 ValueError → 标 aborted，_stage_done 仍通过推进。
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store
from bzplat.backend.store.schema import STATUS_ABORTED, STATUS_COMPLETED


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "rc.db"))


def _mk_bots(store: Store, n: int = 4):
    users = []
    bots = []
    for i in range(n):
        u = store.create_user(f"user{i}", f"u{i}@ex.com", hash_password("password1"))
        users.append(u)
        b = store.create_bot(
            u["id"],
            f"bot{i}",
            binary_path=f"/tmp/fake{i}",
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
            total_hands=k.get("hands", 1), match_type="contest",
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
            earnings_a=100 if w == 0 else (-100 if w == 1 else 0),
            earnings_b=-100 if w == 0 else (100 if w == 1 else 0),
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


# ── 3. bot 不可用 → pairing 标 aborted，阶段仍推进 ──────────────────────


def test_reconcile_bot_unavailable_aborts_pairing(store: Store):
    """死 pairing 重派时 bot 被 reject（模拟已删）→ challenge 抛 ValueError →
    pairing 挂 aborted match → _stage_done 接受 aborted → 阶段推进 → finished。"""
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

    # 所有 bot 都 reject（极端：全部不可用）→ 所有 pairing 都标 aborted
    rejected = {b["id"] for b in bots}
    # 初始 reject 集为空：R1 正常派发；之后再打开 reject 模拟「重启后 bot 被删」
    orch = _FakeOrch(store, reject_bot_ids=set())
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0)
        # 初始 R1 已派发成功（此时 reject 集还是空）。现在打开 reject，模拟 bot
        # 在「重启后被删」——对账重派时全部失败。
        orch.reject_bot_ids = rejected
        # 把已派的 match 全标 aborted（模拟孤儿）→ pairing 复位
        for p in store.list_contest_pairings(cid, stage_idx=0):
            store.update_match(p["match_id"], status=STATUS_ABORTED, reason="orphan_after_restart")
        # 对账：重派全部失败（bot 不可用）→ 全标 aborted → _stage_done 通过 → finished
        n = await mgr.reconcile_running_contests()
        assert n == 1
        c = store.get_contest(cid)
        assert c["status"] == "finished", (
            f"所有 bot 不可用 → pairing 全 aborted → 阶段应推进 finished，实际 {c['status']}"
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
