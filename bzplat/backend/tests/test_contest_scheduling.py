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
from bzplat.backend.matches.orchestrator import MatchOrchestrator
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
        match_config = dict(kwargs.get("match_config") or {})
        if kwargs.get("time_control_id") is not None:
            match_config["time_control_id"] = kwargs["time_control_id"]
        self.store.create_match(
            match_id,
            bot_a_id,
            bot_b_id,
            owner_id=owner_user_id,
            contest_id=kwargs.get("contest_id"),
            match_type=kwargs.get("match_type", "contest"),
            game_id=kwargs.get("game_id") or "holdem",
            match_config=match_config,
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


def test_scheduler_does_not_revalidate_or_reenqueue_active_queued_pairing(
    setup, monkeypatch
):
    store, _legacy_manager, _legacy_scheduler, users = setup
    store.executions.resume()
    orchestrator = MatchOrchestrator(store, max_concurrent=1)
    manager = ContestManager(store, orchestrator)
    scheduler = ContestScheduler(manager, store)
    contest = manager.create(
        users["u1"],
        "queued scheduler idempotency",
        template_id="holdem_rr",
        game_id="holdem",
        starts_at="2099-12-31T23:59:59",
    )
    asyncio.run(manager.open_registration(contest["id"]))
    asyncio.run(manager.register(contest["id"], users["u1"], users["b1"]))
    asyncio.run(manager.register(contest["id"], users["u2"], users["b2"]))
    asyncio.run(manager.publish(contest["id"]))
    pairings = store.list_contest_pairings(contest["id"], stage_idx=0)
    due_at = store.get_contest(contest["id"])["registration_closes_at"]
    store.update_published_contest_schedule(
        contest["id"],
        {"starts_at": due_at},
        stage_idx=0,
        pending_pairing_schedules=[
            {
                "id": pairing["id"],
                "round_num": pairing["round_num"],
                "scheduled_at": due_at,
            }
            for pairing in pairings
        ],
    )

    availability_checks = 0
    enqueue_calls = 0
    original_unavailable = manager._bot_unavailable_reason
    original_challenge = orchestrator.challenge

    def counted_unavailable(*args, **kwargs):
        nonlocal availability_checks
        availability_checks += 1
        return original_unavailable(*args, **kwargs)

    async def counted_challenge(*args, **kwargs):
        nonlocal enqueue_calls
        enqueue_calls += 1
        return await original_challenge(*args, **kwargs)

    monkeypatch.setattr(manager, "_bot_unavailable_reason", counted_unavailable)
    monkeypatch.setattr(orchestrator, "challenge", counted_challenge)

    asyncio.run(scheduler._tick())
    first_checks = availability_checks
    first_enqueues = enqueue_calls
    queued = store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE contest_id=? AND status='queued'",
        (contest["id"],),
    ).fetchone()[0]
    assert queued == len(pairings)
    assert (
        store.get_contest(contest["id"])["published_stage_pairing_count"]
        == len(pairings)
    )
    assert first_checks == 2
    assert first_enqueues == len(pairings)

    original_stage_plan = manager._stage_pairing_plan
    original_pairing_list = store.list_contest_pairings
    stage_plan_calls = 0
    pairing_list_calls = 0

    def counted_stage_plan(*args, **kwargs):
        nonlocal stage_plan_calls
        stage_plan_calls += 1
        return original_stage_plan(*args, **kwargs)

    def counted_pairing_list(*args, **kwargs):
        nonlocal pairing_list_calls
        pairing_list_calls += 1
        return original_pairing_list(*args, **kwargs)

    monkeypatch.setattr(manager, "_stage_pairing_plan", counted_stage_plan)
    monkeypatch.setattr(store, "list_contest_pairings", counted_pairing_list)
    asyncio.run(scheduler._tick())
    assert stage_plan_calls == 0
    assert pairing_list_calls == 0
    assert availability_checks == first_checks
    assert enqueue_calls == first_enqueues
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE contest_id=? AND status='queued'",
        (contest["id"],),
    ).fetchone()[0] == queued
    monkeypatch.setattr(manager, "_stage_pairing_plan", original_stage_plan)
    monkeypatch.setattr(store, "list_contest_pairings", original_pairing_list)

    # A terminal job is no longer an active exclusion.  Once its bounded
    # backoff is due, the still-pending pairing must be eligible for a fresh
    # durable request rather than wedging forever.
    first_job = store._conn.execute(
        "SELECT public_id,contest_pairing_id FROM execution_jobs "
        "WHERE contest_id=? ORDER BY id LIMIT 1",
        (contest["id"],),
    ).fetchone()
    store.executions.request_cancel(first_job["public_id"], owner_user_id=None)
    store.update_contest_pairing(
        first_job["contest_pairing_id"], scheduled_at=due_at
    )
    asyncio.run(scheduler._tick())
    assert availability_checks == first_checks + 2
    assert enqueue_calls == first_enqueues + 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE contest_id=?",
        (contest["id"],),
    ).fetchone()[0] == queued + 1


def test_active_published_batch_missing_row_blocks_scheduler_dispatch(setup):
    store, _legacy_manager, _legacy_scheduler, users = setup
    store.executions.resume()
    orchestrator = MatchOrchestrator(store, max_concurrent=1)
    manager = ContestManager(store, orchestrator)
    scheduler = ContestScheduler(manager, store)
    contest = manager.create(
        users["u1"],
        "damaged active published batch",
        template_id="holdem_rr",
        game_id="holdem",
        games_per_pair=4,
        starts_at="2099-12-31T23:59:59",
    )
    asyncio.run(manager.open_registration(contest["id"]))
    asyncio.run(manager.register(contest["id"], users["u1"], users["b1"]))
    asyncio.run(manager.register(contest["id"], users["u2"], users["b2"]))
    asyncio.run(manager.publish(contest["id"]))
    pairings = store.list_contest_pairings(contest["id"], stage_idx=0)
    assert len(pairings) == 4
    due_at = store.get_contest(contest["id"])["registration_closes_at"]
    store.update_published_contest_schedule(
        contest["id"],
        {"starts_at": due_at},
        stage_idx=0,
        pending_pairing_schedules=[
            {
                "id": pairing["id"],
                "round_num": pairing["round_num"],
                "scheduled_at": due_at,
            }
            for pairing in pairings
        ],
    )
    asyncio.run(scheduler._tick())
    jobs = store._conn.execute(
        "SELECT public_id,contest_pairing_id FROM execution_jobs "
        "WHERE contest_id=? ORDER BY id",
        (contest["id"],),
    ).fetchall()
    assert len(jobs) == 4
    for job in jobs[1:]:
        store.executions.request_cancel(job["public_id"], owner_user_id=None)
    with store._tx() as connection:
        connection.execute(
            "DELETE FROM contest_pairings WHERE id=?",
            (jobs[1]["contest_pairing_id"],),
        )

    with pytest.raises(ValueError, match="active 对阵批次完整性"):
        asyncio.run(manager._dispatch_pending(contest["id"], 0))
    asyncio.run(scheduler._tick())

    assert store.get_contest(contest["id"])["status"] == "published"
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE contest_id=? AND status='queued'",
        (contest["id"],),
    ).fetchone()[0] == 0
    failed_closed = store._conn.execute(
        "SELECT retryable,terminal_reason,last_error,next_attempt_at,terminal_at "
        "FROM execution_jobs WHERE contest_id=? AND status='cancelled' "
        "AND terminal_reason='contest_pairing_batch_changed'",
        (contest["id"],),
    ).fetchall()
    assert len(failed_closed) == 1
    assert all(
        tuple(row[:4])
        == (
            0,
            "contest_pairing_batch_changed",
            "contest_pairing_batch_changed",
            None,
        )
        and row[4] is not None
        for row in failed_closed
    )
    assert store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE contest_id=?",
        (contest["id"],),
    ).fetchone()[0] == 4
    assert store.list_matches(contest_id=contest["id"]) == []


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


def test_scheduler_rejects_real_stage_cursor_before_dispatch(setup):
    store, mgr, sched, users = setup
    c = mgr.create(
        users["u1"],
        "Malformed cursor",
        template_id="holdem_rr",
        game_id="holdem",
        starts_at="2099-12-31T23:59:59",
    )
    asyncio.run(mgr.open_registration(c["id"]))
    asyncio.run(mgr.register(c["id"], users["u1"], users["b1"]))
    asyncio.run(mgr.register(c["id"], users["u2"], users["b2"]))
    asyncio.run(mgr.publish(c["id"]))
    published = store.get_contest(c["id"])
    store.update_contest(
        c["id"], starts_at=published["registration_closes_at"]
    )
    for pairing in store.list_contest_pairings(c["id"]):
        store.update_contest_pairing(
            pairing["id"], scheduled_at="2020-01-01T00:00:00"
        )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET current_stage_idx=0.5 WHERE id=?",
            (c["id"],),
        )

    before_calls = mgr.orch.calls
    asyncio.run(sched._tick())
    assert mgr.orch.calls == before_calls
    assert store.get_contest(c["id"])["status"] == "published"
    assert all(
        pairing["match_id"] is None
        for pairing in store.list_contest_pairings(c["id"])
    )


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
    started = store.get_contest(c["id"])
    assert started["status"] == "running"
    assert started["registration_closes_at"] == started["starts_at"]
    assert {
        pairing["scheduled_at"]
        for pairing in store.list_contest_pairings(c["id"], stage_idx=0)
    } == {started["starts_at"]}


def test_start_from_published_accepts_complete_batch_with_persisted_bye(
    setup, tmp_path
):
    """Manifest covers pending games plus completed no-match byes atomically."""
    store, mgr, _, users = setup
    third = store.create_user("user03", "u3@e.com", "hx")
    bot_dir = tmp_path / f"bot_uploads/{third['id']}/v1"
    bot_dir.mkdir(parents=True)
    shutil.copyfile(str(SAMPLES / "callbot_linux_amd64"), str(bot_dir / "bot.bin"))
    third_bot = store.create_bot(
        third["id"],
        "b3",
        binary_path=str(bot_dir / "bot.bin"),
        format="elf",
        game_id="holdem",
    )
    contest = mgr.create(
        users["u1"],
        "Published Swiss bye",
        game_id="holdem",
        starts_at="2099-12-31T23:59:59",
        stages=[{"key": "swiss", "type": "swiss", "rounds": 1}],
    )
    for user_id, bot_id in (
        (users["u1"], users["b1"]),
        (users["u2"], users["b2"]),
        (third["id"], third_bot["id"]),
    ):
        store.add_contest_entry(contest["id"], user_id, bot_id)

    asyncio.run(mgr.publish(contest["id"]))
    before = store.list_contest_pairings(contest["id"], stage_idx=0)
    assert len(before) == 2
    bye = next(pairing for pairing in before if pairing["entry_b_id"] is None)
    assert bye["status"] == "completed" and bye["match_id"] is None

    asyncio.run(mgr.start(contest["id"]))

    started = store.get_contest(contest["id"])
    after = store.list_contest_pairings(contest["id"], stage_idx=0)
    saved_bye = next(pairing for pairing in after if pairing["id"] == bye["id"])
    assert started["status"] == "running"
    assert saved_bye == bye
    assert sum(pairing["match_id"] is not None for pairing in after) == 1


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


@pytest.mark.parametrize(
    "value",
    [
        "2026-01-01 00:00:00",
        "20260101T000000",
        "2026-01-01T00:00:00.000000",
    ],
)
def test_contest_time_writes_require_canonical_naive_iso_seconds(
    setup, value
):
    """All persisted contest times use one lexicographically sortable format."""
    store, mgr, _, users = setup
    before = len(store.list_contests())

    with pytest.raises(ValueError, match="规范.*ISO.*秒"):
        mgr.create(
            users["u1"],
            "Noncanonical time",
            template_id="holdem_rr",
            game_id="holdem",
            starts_at=value,
        )

    assert len(store.list_contests()) == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ends_at", "2026-01-01 00:00:00"),
        ("rest_ends_at", "20260101T000000"),
    ],
)
def test_store_rejects_noncanonical_terminal_times_without_partial_write(
    setup, field, value
):
    store, _mgr, _, users = setup
    contest = store.create_contest("Canonical boundary", users["u1"])

    with pytest.raises(ValueError, match="规范.*ISO.*秒"):
        store.update_contest(contest["id"], title="must-not-write", **{field: value})

    saved = store.get_contest(contest["id"])
    assert saved["title"] == "Canonical boundary"
    assert saved[field] is None


def test_formal_pairing_batch_rejects_noncanonical_publication_time(setup):
    store, _mgr, _, users = setup
    contest = store.create_contest(
        "Canonical pairing batch",
        users["u1"],
        status="published",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )
    entry_a = store.add_contest_entry(
        contest["id"], users["u1"], users["b1"]
    )
    entry_b = store.add_contest_entry(
        contest["id"], users["u2"], users["b2"]
    )

    with pytest.raises(ValueError, match="发布时间.*规范"):
        store.create_contest_stage_pairings(
            contest["id"],
            0,
            [
                {
                    "entry_a_id": entry_a["id"],
                    "entry_b_id": entry_b["id"],
                    "bot_a_id": users["b1"],
                    "bot_b_id": users["b2"],
                    "round_num": 1,
                    "stage_key": "rr",
                    "published_at": "2026-01-01 00:00:00",
                }
            ],
            expected_current_stage_idx=0,
            expected_status="published",
        )

    assert store.list_contest_pairings(contest["id"]) == []


def test_published_schedule_batch_rejects_noncanonical_time_atomically(setup):
    store, _mgr, _, users = setup
    contest = store.create_contest(
        "Canonical published reschedule",
        users["u1"],
        status="published",
        starts_at="2099-01-01T00:00:00",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )
    pairing = store.add_contest_pairing(
        contest["id"],
        users["b1"],
        users["b2"],
        stage_idx=0,
        stage_key="rr",
        published_at="2026-01-01T00:00:00",
        scheduled_at="2099-01-01T00:00:00",
    )

    with pytest.raises(ValueError, match="计划时间.*规范"):
        store.update_published_contest_schedule(
            contest["id"],
            {"title": "must-not-write"},
            stage_idx=0,
            pending_pairing_schedules=[
                {
                    "id": pairing["id"],
                    "round_num": 1,
                    "scheduled_at": "2099-01-01 00:00:00",
                }
            ],
        )

    saved = store.get_contest(contest["id"])
    assert saved["title"] == "Canonical published reschedule"
    persisted = next(
        row
        for row in store.list_contest_pairings(contest["id"])
        if row["id"] == pairing["id"]
    )
    assert persisted["scheduled_at"] == "2099-01-01T00:00:00"


def test_terminal_result_writer_rejects_noncanonical_end_time_before_write(setup):
    store, _mgr, _, _users = setup

    with pytest.raises(ValueError, match="结束时间.*规范"):
        store.finish_contest_with_results(
            999999,
            0,
            stage_result_rows=None,
            official_result_rows=[],
            expected_decision_revision=0,
            expected_status="running",
            expected_entries=[],
            expected_stage_groups=None,
            ends_at="2026-01-01 00:00:00",
        )


@pytest.mark.parametrize(
    "started_at",
    ["2026-08-09 20:56:17", "20260809T205617", "2026-08-09T20:56:17+08:00"],
)
def test_actual_start_backfill_rejects_noncanonical_match_time(
    setup, started_at
):
    store, _mgr, _, users = setup
    contest = store.create_contest(
        "Malformed legacy actual start",
        users["u1"],
        status="running",
        game_id="holdem",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )
    match_id = f"malformed-start-{started_at}"
    store.create_match(
        match_id,
        users["b1"],
        users["b2"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(match_id, status="running", started_at=started_at)
    store.add_contest_pairing(
        contest["id"],
        users["b1"],
        users["b2"],
        match_id=match_id,
        status="running",
        stage_idx=0,
        stage_key="rr",
    )

    assert store.backfill_contest_actual_start(contest["id"]) is None
    assert store.get_contest(contest["id"])["starts_at"] is None


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


def test_reconcile_repairs_active_legacy_start_time_and_completed_pairing(setup):
    """启动对账幂等修复 active 旧赛事 NULL starts_at 与 running 假状态。"""
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
    # Intentional pre-transaction active legacy shape: this test exercises
    # start-time/pairing repair without granting permission to rewrite a
    # finished/cancelled historical contest.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status='running' WHERE id=?",
            (contest["id"],),
        )

    asyncio.run(
        mgr.reconcile_running_contests(
            interruption_reason="orphan_after_service_restart"
        )
    )
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
    store, mgr, _, users = setup
    contest = store.create_contest(
        "UniqueBind", users["u1"], status="published", game_id="holdem",
        stages_json='[{"key":"rr","type":"double_round_robin"}]',
    )
    store.add_contest_entry(contest["id"], users["u1"], users["b1"])
    store.add_contest_entry(contest["id"], users["u2"], users["b2"])
    asyncio.run(mgr._begin_stage(contest["id"], 0, dispatch_pending=False))
    first, second = store.list_contest_pairings(contest["id"], stage_idx=0)
    match_id = "unique-pairing-match"
    store.create_match(
        match_id, users["b1"], users["b2"], contest_id=contest["id"],
        match_type="contest", game_id="holdem",
    )
    store.bind_contest_pairing_match(
        contest["id"], first["id"], match_id,
        require_execution_admission=False,
    )
    with pytest.raises(ValueError, match="多个赛事对阵"):
        store.bind_contest_pairing_match(
            contest["id"], second["id"], match_id,
            require_execution_admission=False,
        )
