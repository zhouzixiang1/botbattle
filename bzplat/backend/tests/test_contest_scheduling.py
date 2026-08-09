"""赛事时间编排 + 两阶段排期 + 调度器测试（PR-1）。

覆盖：
1. 报名截止时间校验（register 在 closes_at 过后被拒）；
2. 两阶段排期（publish → published 态 → scheduled_at 到点 dispatch → running）；
3. ContestScheduler 到点自动推进（draft→open→published→running）；
4. create 透传时间字段 + 时间窗口校验。
"""
from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from bzplat.backend.contests.manager import ContestManager, _now, _validate_contest_times
from bzplat.backend.contests.scheduler import ContestScheduler
from bzplat.backend.matches.orchestrator import BotCapacityError, MatchOrchestrator
from bzplat.backend.store import Store

SAMPLES = Path(__file__).resolve().parents[3] / "samples"


class _CreatingFakeOrch:
    """落真实 match 行但不启动 subprocess，避免 asyncio.run 关环时遗留任务。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls = 0

    async def challenge(self, bot_a_id, bot_b_id, owner_user_id, **kwargs):
        self.calls += 1
        match_id = f"schedule-fake-{self.calls}"
        self.store.create_match(
            match_id,
            bot_a_id,
            bot_b_id,
            owner_id=owner_user_id,
            contest_id=kwargs.get("contest_id"),
            match_type=kwargs.get("match_type", "contest"),
            game_id=kwargs.get("game_id") or "holdem",
            match_config=kwargs.get("match_config") or {},
        )
        return match_id

    async def challenge_duplicate(self, *args, **kwargs):
        return await self.challenge(*args, **kwargs)

    def start_prepared_match(self, match_id: str) -> None:
        self.store.update_match(match_id, status="running")

    def discard_prepared_match(self, match_id: str) -> bool:
        return self.store.delete_match(match_id)


class _CapacityFakeOrch(_CreatingFakeOrch):
    """Use active DB rows as a deterministic admission counter."""

    def __init__(self, store: Store, max_concurrent: int) -> None:
        super().__init__(store)
        self.max_concurrent = max_concurrent

    def available_bot_slots(self) -> int:
        active = sum(
            match["status"] in ("pending", "running")
            and match.get("match_type") != "human"
            for match in self.store.list_matches(limit=1000)
        )
        return max(0, self.max_concurrent - active)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """建临时 Store + 2 个 holdem bot + manager/scheduler。"""
    monkeypatch.chdir(tmp_path)
    os.environ["BZ_BOT_LOCAL"] = "1"
    store = Store(str(tmp_path / "t.db"))
    store.create_user("user01", "u1@e.com", "hx")
    store.create_user("user02", "u2@e.com", "hx")
    u1 = store._conn.execute("SELECT id FROM users WHERE username='user01'").fetchone()["id"]
    u2 = store._conn.execute("SELECT id FROM users WHERE username='user02'").fetchone()["id"]
    for uid, n in [(u1, "b1"), (u2, "b2")]:
        d = tmp_path / f"bot_uploads/{uid}/v1"
        d.mkdir(parents=True)
        shutil.copyfile(str(SAMPLES / "callbot_linux_amd64"), str(d / "bot.bin"))
        store.create_bot(uid, n, binary_path=str(d / "bot.bin"), format="elf", game_id="holdem")
    b1, b2 = [r[0] for r in store._conn.execute("SELECT id FROM bots ORDER BY id").fetchall()]
    orch = _CreatingFakeOrch(store)
    mgr = ContestManager(store, orch)
    sched = ContestScheduler(mgr, store)
    return store, mgr, sched, {"u1": u1, "u2": u2, "b1": b1, "b2": b2}


# ── 时间校验 ────────────────────────────────────────────────────────────

def test_validate_contest_times_order():
    _validate_contest_times(None, None, None)  # 全空 OK
    _validate_contest_times("2026-01-01T00:00:00", "2026-01-02T00:00:00", "2026-01-03T00:00:00")
    # 相同时间是明确支持的语义：手动立即推进可在同一秒开放、截止、开赛。
    _validate_contest_times("2026-01-01T00:00:00", "2026-01-01T00:00:00", "2026-01-01T00:00:00")
    with pytest.raises(ValueError):
        _validate_contest_times("2026-01-03T00:00:00", "2026-01-02T00:00:00", None)  # opens>closes
    with pytest.raises(ValueError):
        _validate_contest_times(None, "2026-01-03T00:00:00", "2026-01-02T00:00:00")  # closes>starts
    with pytest.raises(ValueError):
        _validate_contest_times("2026-01-03T00:00:00", None, "2026-01-02T00:00:00")


def test_store_time_writes_validate_complete_candidate_atomically(setup):
    store, _mgr, _, users = setup
    contest = store.create_contest(
        "Store timeline",
        users["u1"],
        registration_opens_at="2099-01-01T00:00:00",
        registration_closes_at="2099-01-02T00:00:00",
        starts_at="2099-01-03T00:00:00",
    )

    with pytest.raises(ValueError, match="允许相同"):
        store.update_contest(
            contest["id"],
            title="不得部分写入",
            registration_closes_at="2099-01-04T00:00:00",
        )

    saved = store.get_contest(contest["id"])
    assert saved["title"] == "Store timeline"
    assert saved["registration_closes_at"] == "2099-01-02T00:00:00"


def test_store_create_rejects_inverted_timeline_before_insert(setup):
    store, _mgr, _, users = setup
    before = len(store.list_contests())

    with pytest.raises(ValueError, match="报名截止时间不能早于报名开放时间"):
        store.create_contest(
            "Bad timeline",
            users["u1"],
            registration_opens_at="2099-01-02T00:00:00",
            registration_closes_at="2099-01-01T00:00:00",
        )

    assert len(store.list_contests()) == before


def test_create_with_times(setup):
    store, mgr, _, _ = setup
    c = mgr.create(
        1, "Timed", template_id="holdem_rr", game_id="holdem",
        registration_opens_at="2099-01-01T00:00:00",
        registration_closes_at="2099-01-02T00:00:00",
        starts_at="2099-01-03T00:00:00",
    )
    c = store.get_contest(c["id"])
    assert c["registration_opens_at"] == "2099-01-01T00:00:00"
    assert c["registration_closes_at"] == "2099-01-02T00:00:00"
    assert c["starts_at"] == "2099-01-03T00:00:00"


# ── 报名截止时间校验 ────────────────────────────────────────────────────

def test_register_rejected_after_deadline(setup):
    store, mgr, _, users = setup
    c = mgr.create(
        users["u1"], "DL", template_id="holdem_rr", game_id="holdem",
        registration_closes_at="2020-01-01T00:00:00",  # 已过
    )
    asyncio.run(mgr.open_registration(c["id"]))
    with pytest.raises(ValueError, match="报名已截止"):
        asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))


def test_register_allowed_before_deadline(setup):
    store, mgr, _, users = setup
    c = mgr.create(
        users["u1"], "DL", template_id="holdem_rr", game_id="holdem",
        registration_closes_at="2099-12-31T00:00:00",  # 未来
    )
    asyncio.run(mgr.open_registration(c["id"]))
    e = asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))  # 不抛
    assert e["bot_id"] == users["b1"]


def test_register_no_deadline_still_works(setup):
    """registration_closes_at 未设时不校验时间（兼容旧行为）。"""
    store, mgr, _, users = setup
    c = mgr.create(users["u1"], "NoDL", template_id="holdem_rr", game_id="holdem")
    asyncio.run(mgr.open_registration(c["id"]))
    asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))  # 不抛


# ── 两阶段排期 ──────────────────────────────────────────────────────────

def test_publish_creates_pairings_with_future_schedule(setup):
    """publish → published 态，pairing 有 scheduled_at（未来），dispatch 不开打。"""
    store, mgr, _, users = setup
    c = mgr.create(
        users["u1"], "2Phase", template_id="holdem_rr", game_id="holdem",
        starts_at="2099-12-31T23:59:59",
    )
    asyncio.run(mgr.open_registration(c["id"]))
    asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))
    asyncio.run(mgr.register(c["id"], users["u2"], users["b2"]))
    asyncio.run(mgr.publish(c["id"]))
    assert store.get_contest(c["id"])["status"] == "published"
    ps = store.list_contest_pairings(c["id"])
    assert len(ps) >= 1
    assert all(p.get("scheduled_at") for p in ps)  # 都有排期
    # dispatch 不开打（scheduled_at 在未来）
    asyncio.run(mgr._dispatch_pending(c["id"], 0))
    assert all(p["status"] == "pending" for p in store.list_contest_pairings(c["id"]))
    assert store.get_contest(c["id"])["status"] == "published"


def test_publish_without_start_time_waits_for_manual_start(setup):
    """starts_at 为空表示只出排期，scheduler 不得把截止报名当开赛。"""
    store, mgr, sched, users = setup
    c = mgr.create(
        users["u1"], "ManualStart", template_id="holdem_rr", game_id="holdem"
    )
    asyncio.run(mgr.open_registration(c["id"]))
    asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))
    asyncio.run(mgr.register(c["id"], users["u2"], users["b2"]))
    asyncio.run(mgr.publish(c["id"]))

    published = store.get_contest(c["id"])
    assert published["status"] == "published"
    assert published["starts_at"] is None
    assert all(
        pairing["scheduled_at"] is None
        for pairing in store.list_contest_pairings(c["id"])
    )

    asyncio.run(sched._tick())
    assert store.get_contest(c["id"])["status"] == "published"
    assert store.list_matches(contest_id=c["id"]) == []

    # Manager 是权威闸门；启动对账或内部直接调用也不能绕过 scheduler。
    asyncio.run(mgr._dispatch_pending(c["id"], 0))
    assert store.get_contest(c["id"])["status"] == "published"
    assert store.list_matches(contest_id=c["id"]) == []

    asyncio.run(mgr.start(c["id"]))
    started = store.get_contest(c["id"])
    assert started["status"] == "running"
    assert started["starts_at"] is not None


def test_publish_then_schedule_arrives_dispatches(setup):
    """排期到点后 dispatch 开打，status→running。"""
    store, mgr, _, users = setup
    c = mgr.create(users["u1"], "2Phase", template_id="holdem_rr", game_id="holdem",
                   starts_at="2099-12-31T23:59:59")
    asyncio.run(mgr.open_registration(c["id"]))
    asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))
    asyncio.run(mgr.register(c["id"], users["u2"], users["b2"]))
    asyncio.run(mgr.publish(c["id"]))
    # 仅把 pairing 排期改成过去仍不得越过赛事级 starts_at 闸门。
    for p in store.list_contest_pairings(c["id"]):
        store.update_contest_pairing(p["id"], scheduled_at="2020-01-01T00:00:00")
    asyncio.run(mgr._dispatch_pending(c["id"], 0))
    assert store.list_matches(contest_id=c["id"]) == []
    assert store.get_contest(c["id"])["status"] == "published"

    # 模拟赛事开赛时间与逐场排期均已到点。
    published = store.get_contest(c["id"])
    store.update_contest(
        c["id"], starts_at=published["registration_closes_at"]
    )
    asyncio.run(mgr._dispatch_pending(c["id"], 0))
    ps = store.list_contest_pairings(c["id"])
    assert any(p["status"] == "running" for p in ps)
    assert store.get_contest(c["id"])["status"] == "running"


def test_start_immediate_skips_schedule(setup):
    """start() 立即开赛：scheduled_at 全设 now，立即 dispatch。"""
    store, mgr, _, users = setup
    c = mgr.create(users["u1"], "Imm", template_id="holdem_rr", game_id="holdem")
    asyncio.run(mgr.open_registration(c["id"]))
    asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))
    asyncio.run(mgr.register(c["id"], users["u2"], users["b2"]))
    asyncio.run(mgr.start(c["id"]))
    # start 立即 running + pairing 已 dispatch
    assert store.get_contest(c["id"])["status"] == "running"
    assert any(p["status"] == "running" for p in store.list_contest_pairings(c["id"]))


def test_manual_early_lifecycle_uses_actual_times_without_inversion(setup):
    """手动提前开放/发布/开赛必须改写未来计划，不能留下截止晚于开赛。"""
    store, mgr, _, users = setup
    contest = mgr.create(
        users["u1"],
        "Early",
        template_id="holdem_rr",
        game_id="holdem",
        registration_opens_at="2099-01-01T00:00:00",
        registration_closes_at="2099-01-02T00:00:00",
        starts_at="2099-01-03T00:00:00",
    )
    asyncio.run(mgr.open_registration(contest["id"]))
    asyncio.run(mgr.register(contest["id"], users["u1"], users["b1"]))
    asyncio.run(mgr.register(contest["id"], users["u2"], users["b2"]))
    asyncio.run(mgr.publish(contest["id"]))
    published = store.get_contest(contest["id"])
    assert published["registration_opens_at"] <= published["registration_closes_at"]
    assert published["registration_closes_at"] <= published["starts_at"]

    asyncio.run(mgr.start(contest["id"]))
    started = store.get_contest(contest["id"])
    assert started["registration_opens_at"] <= started["registration_closes_at"]
    assert started["registration_closes_at"] == started["starts_at"]


# ── ContestScheduler 自动推进 ───────────────────────────────────────────

def test_scheduler_auto_opens_registration(setup):
    """draft 且 registration_opens_at<=now → scheduler 自动 open。"""
    store, mgr, sched, users = setup
    c = mgr.create(users["u1"], "Auto", template_id="holdem_rr", game_id="holdem",
                   registration_opens_at="2020-01-01T00:00:00")  # 已过
    assert store.get_contest(c["id"])["status"] == "draft"
    asyncio.run(sched._tick())
    assert store.get_contest(c["id"])["status"] == "open"


def test_scheduler_auto_publishes_on_deadline(setup):
    """open 且 registration_closes_at<=now → scheduler 自动 publish。"""
    store, mgr, sched, users = setup
    # 先用未来截止时间报名，再改成过去模拟时间流逝
    c = mgr.create(users["u1"], "Auto", template_id="holdem_rr", game_id="holdem",
                   starts_at="2099-12-31T23:59:59",
                   registration_closes_at="2099-12-30T00:00:00")  # 未来（先报名）
    asyncio.run(mgr.open_registration(c["id"]))
    asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))
    asyncio.run(mgr.register(c["id"], users["u2"], users["b2"]))
    # 模拟截止时间已到
    # 时间流逝模拟须保持新 Store 不变量，成组移动开放/截止时间。
    store.update_contest(
        c["id"],
        registration_opens_at="2019-12-31T00:00:00",
        registration_closes_at="2020-01-01T00:00:00",
    )
    asyncio.run(sched._tick())
    assert store.get_contest(c["id"])["status"] == "published"


def test_scheduler_dispatches_published_on_schedule(setup):
    """published 且 scheduled_at<=now → scheduler dispatch → running。"""
    store, mgr, sched, users = setup
    c = mgr.create(users["u1"], "Auto", template_id="holdem_rr", game_id="holdem",
                   starts_at="2099-12-31T23:59:59")  # 未来（先 publish 不立即开打）
    asyncio.run(mgr.open_registration(c["id"]))
    asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))
    asyncio.run(mgr.register(c["id"], users["u2"], users["b2"]))
    asyncio.run(mgr.publish(c["id"]))
    assert store.get_contest(c["id"])["status"] == "published"
    # 模拟赛事级开赛时间与逐场排期均已到点。
    published = store.get_contest(c["id"])
    store.update_contest(c["id"], starts_at=published["registration_closes_at"])
    for p in store.list_contest_pairings(c["id"]):
        store.update_contest_pairing(p["id"], scheduled_at="2020-01-01T00:00:00")
    asyncio.run(sched._tick())  # 到点 dispatch
    assert store.get_contest(c["id"])["status"] == "running"


def test_scheduler_does_not_advance_future_contest(setup):
    """时间未到的赛事 scheduler 不推进。"""
    store, mgr, sched, users = setup
    c = mgr.create(users["u1"], "Future", template_id="holdem_rr", game_id="holdem",
                   registration_opens_at="2099-12-31T23:59:59")  # 未来
    asyncio.run(sched._tick())
    assert store.get_contest(c["id"])["status"] == "draft"  # 不变


# ── 审计修复回归（start+published 不重复 / maybe_finish 锁 / 时间格式校验） ──

def test_start_from_published_does_not_duplicate_pairings(setup):
    """start() 从 published 态开赛：不重新生成 pairing，只改 scheduled_at=now + dispatch。"""
    store, mgr, _, users = setup
    c = mgr.create(users["u1"], "Dup", template_id="holdem_rr", game_id="holdem",
                   starts_at="2099-12-31T23:59:59")
    asyncio.run(mgr.open_registration(c["id"]))
    asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))
    asyncio.run(mgr.register(c["id"], users["u2"], users["b2"]))
    asyncio.run(mgr.publish(c["id"]))
    before = len(store.list_contest_pairings(c["id"]))
    assert before >= 1
    # start 从 published 开赛
    asyncio.run(mgr.start(c["id"]))
    after = len(store.list_contest_pairings(c["id"]))
    assert after == before, f"start(published) 不应重复生成 pairing: {before} → {after}"
    assert store.get_contest(c["id"])["status"] == "running"


def test_maybe_finish_has_per_contest_lock(setup):
    """maybe_finish 用 per-contest asyncio.Lock（防并发重复轮次）。"""
    store, mgr, _, _ = setup
    # 验证 _locks 字典存在 + _lock 返回同一个 Lock 实例
    lk1 = mgr._lock(999)
    lk2 = mgr._lock(999)
    assert lk1 is lk2, "同一 contest 应返回同一个 Lock 实例"
    assert isinstance(lk1, asyncio.Lock)


def test_time_format_validation_rejects_bad_iso(setup):
    """时间格式校验：拒绝带毫秒/时区/非法格式的 ISO 字符串。"""
    store, mgr, _, users = setup
    # 带时区
    with pytest.raises(ValueError, match="不应带时区"):
        mgr.create(users["u1"], "TZ", template_id="holdem_rr", game_id="holdem",
                   registration_opens_at="2026-01-01T00:00:00+08:00")
    # 非法格式
    with pytest.raises(ValueError, match="格式非法"):
        mgr.create(users["u1"], "Bad", template_id="holdem_rr", game_id="holdem",
                   registration_opens_at="not-a-date")


def test_scheduler_tick_does_not_double_process(setup):
    """scheduler tick 快照分派：published→running 后同 tick 不再 running 分支处理。"""
    store, mgr, sched, users = setup
    c = mgr.create(users["u1"], "NoDouble", template_id="holdem_rr", game_id="holdem",
                   starts_at="2020-01-01T00:00:00")  # 过去，排期立即到
    asyncio.run(mgr.open_registration(c["id"]))
    asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))
    asyncio.run(mgr.register(c["id"], users["u2"], users["b2"]))
    asyncio.run(mgr.publish(c["id"]))
    # publish 后 scheduled_at 已是过去 → _dispatch_pending（在 publish 内）应已转 running
    # 或至少 tick 后转 running。验证 tick 不抛、不重复
    pairings_before = len(store.list_contest_pairings(c["id"]))
    asyncio.run(sched._tick())
    pairings_after = len(store.list_contest_pairings(c["id"]))
    assert pairings_after == pairings_before, "tick 不应增加 pairing 数量"


def test_contest_dispatch_admits_only_free_slots_and_refills_on_completion(
    setup, tmp_path
):
    """整轮不可先建成 pending task；完成一场只补进一个空闲槽。"""
    store, _mgr, _, users = setup
    participants = [(users["u1"], users["b1"]), (users["u2"], users["b2"])]
    for index in range(3, 7):
        user = store.create_user(
            f"capacity{index}", f"capacity{index}@example.com", "hash"
        )
        binary = tmp_path / f"capacity-{index}.bin"
        shutil.copyfile(SAMPLES / "callbot_linux_amd64", binary)
        bot = store.create_bot(
            user["id"], f"capacity-bot-{index}", binary_path=str(binary),
            format="elf", game_id="holdem",
        )
        participants.append((user["id"], bot["id"]))

    orch = _CapacityFakeOrch(store, max_concurrent=2)
    mgr = ContestManager(store, orch)
    contest = mgr.create(
        users["u1"], "Capacity", template_id="holdem_rr", game_id="holdem"
    )
    asyncio.run(mgr.open_registration(contest["id"]))
    for user_id, bot_id in participants:
        asyncio.run(mgr.register(contest["id"], user_id, bot_id))

    asyncio.run(mgr.start(contest["id"]))
    pairings = store.list_contest_pairings(contest["id"])
    assert sum(pairing["status"] == "running" for pairing in pairings) == 2
    assert sum(pairing["status"] == "pending" for pairing in pairings) == 13
    assert len(store.list_matches(contest_id=contest["id"], limit=1000)) == 2

    first = next(pairing for pairing in pairings if pairing.get("match_id"))
    store.update_match(
        first["match_id"], status="completed", winner=0,
        result={"deltas": [1, -1]}, ended_at=_now(),
    )
    asyncio.run(mgr.handle_match_done(first["match_id"], contest["id"]))

    refreshed = store.list_contest_pairings(contest["id"])
    completed = next(pairing for pairing in refreshed if pairing["id"] == first["id"])
    assert completed["status"] == "completed"
    active = [
        match for match in store.list_matches(contest_id=contest["id"], limit=1000)
        if match["status"] in ("pending", "running")
    ]
    assert len(active) == 2
    assert len(store.list_matches(contest_id=contest["id"], limit=1000)) == 3
    assert sum(pairing["status"] == "running" for pairing in refreshed) == 2


def test_orchestrator_capacity_counts_waiting_bot_tasks_but_not_human(setup):
    """admission 看已建 task（含等 semaphore），人类局走独立槽。"""
    store, _mgr, _, users = setup
    store.create_match(
        "capacity-bot-task", users["b1"], users["b2"],
        match_type="challenge", game_id="holdem",
    )
    store.create_match(
        "capacity-human-task", users["b1"], None,
        match_type="human", game_id="holdem", human_user_id=users["u2"],
        human_seat=1,
    )
    orch = MatchOrchestrator(store, max_concurrent=2)

    async def exercise():
        gate = asyncio.Event()

        async def wait_forever():
            await gate.wait()

        bot_task = asyncio.create_task(wait_forever())
        human_task = asyncio.create_task(wait_forever())
        orch._tasks["capacity-bot-task"] = bot_task
        orch._tasks["capacity-human-task"] = human_task
        orch._reserve_bot_slot("capacity-bot-task")
        try:
            assert orch.available_bot_slots() == 1
        finally:
            bot_task.cancel()
            human_task.cancel()
            await asyncio.gather(bot_task, human_task, return_exceptions=True)
            orch._tasks.clear()
            orch._bot_admitted.clear()

    asyncio.run(exercise())


def test_orchestrator_global_admission_rejects_before_creating_extra_match(setup):
    """挑战入口也必须使用统一令牌，不能在 semaphore 后堆 pending 行。"""
    store, _mgr, _, users = setup
    orch = MatchOrchestrator(store, max_concurrent=1)

    async def exercise():
        first = await orch.challenge(
            users["b1"], users["b2"], users["u1"],
            game_id="holdem", defer_start=True,
        )
        assert orch.available_bot_slots() == 0
        with pytest.raises(BotCapacityError):
            await orch.challenge(
                users["b1"], users["b2"], users["u1"],
                game_id="holdem", defer_start=True,
            )
        assert [match["id"] for match in store.list_matches(limit=100)] == [first]

        assert orch.discard_prepared_match(first) is True
        assert orch.available_bot_slots() == 1
        replacement = await orch.challenge(
            users["b1"], users["b2"], users["u1"],
            game_id="holdem", defer_start=True,
        )
        assert replacement != first
        assert orch.discard_prepared_match(replacement) is True

    asyncio.run(exercise())


def test_reconcile_repairs_legacy_start_time_and_completed_pairing(setup):
    """启动对账幂等修复旧赛事 NULL starts_at 与 running 假状态。"""
    store, mgr, _, users = setup
    contest = store.create_contest(
        "LegacyTimeline", users["u1"], game_id="holdem",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )
    match_id = "legacy-contest-timeline"
    store.create_match(
        match_id, users["b1"], users["b2"], contest_id=contest["id"],
        match_type="contest", game_id="holdem",
    )
    store.update_match(
        match_id, status="completed", winner=0, result={"deltas": [1, -1]},
        started_at="2026-08-09T20:56:17", ended_at="2026-08-09T20:57:17",
    )
    store.add_contest_pairing(
        contest["id"], users["b1"], users["b2"], match_id=match_id,
        status="running", stage_idx=0, stage_key="rr",
    )
    store.update_contest(contest["id"], status="finished")

    asyncio.run(mgr.reconcile_running_contests())
    repaired = store.get_contest(contest["id"])
    pairing = store.list_contest_pairings(contest["id"])[0]
    assert repaired["starts_at"] == "2026-08-09T20:56:17"
    assert pairing["status"] == "completed"


def test_actual_start_backfill_never_arms_published_or_cross_bound_contest(setup):
    """脏 pairing 不能借别场 started_at 绕过手动开赛闸门。"""
    store, _mgr, _, users = setup
    owner = store.create_contest(
        "Owner", users["u1"], status="running", game_id="holdem",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )
    waiting = store.create_contest(
        "Waiting", users["u1"], status="published", game_id="holdem",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )
    match_id = "cross-bound-start"
    store.create_match(
        match_id, users["b1"], users["b2"], contest_id=owner["id"],
        match_type="contest", game_id="holdem",
    )
    store.update_match(
        match_id, status="running", started_at="2026-08-09T20:56:17",
    )
    store.add_contest_pairing(
        waiting["id"], users["b1"], users["b2"], match_id=match_id,
        status="running", stage_idx=0, stage_key="rr",
    )

    assert store.backfill_contest_actual_start(waiting["id"]) is None
    assert store.get_contest(waiting["id"])["starts_at"] is None
    store.update_contest(waiting["id"], status="running")
    assert store.backfill_contest_actual_start(waiting["id"]) is None
    assert store.get_contest(waiting["id"])["starts_at"] is None


def test_match_can_bind_to_only_one_pairing(setup):
    """逻辑外键 match_id 必须保持一对一，避免状态与积分双计。"""
    store, _mgr, _, users = setup
    contest = store.create_contest(
        "UniqueBind", users["u1"], status="running", game_id="holdem",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )
    first = store.add_contest_pairing(
        contest["id"], users["b1"], users["b2"], status="pending",
        stage_idx=0, stage_key="rr",
    )
    second = store.add_contest_pairing(
        contest["id"], users["b2"], users["b1"], status="pending",
        stage_idx=0, stage_key="rr",
    )
    match_id = "unique-pairing-match"
    store.create_match(
        match_id, users["b1"], users["b2"], contest_id=contest["id"],
        match_type="contest", game_id="holdem",
    )
    store.bind_contest_pairing_match(contest["id"], first["id"], match_id)
    with pytest.raises(ValueError, match="多个赛事对阵"):
        store.bind_contest_pairing_match(contest["id"], second["id"], match_id)
