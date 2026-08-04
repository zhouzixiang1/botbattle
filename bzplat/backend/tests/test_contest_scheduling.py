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
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store

SAMPLES = Path(__file__).resolve().parents[3] / "samples"


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
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    orch = MatchOrchestrator(store, runner=runner, max_concurrent=2)
    mgr = ContestManager(store, orch)
    sched = ContestScheduler(mgr, store)
    return store, mgr, sched, {"u1": u1, "u2": u2, "b1": b1, "b2": b2}


# ── 时间校验 ────────────────────────────────────────────────────────────

def test_validate_contest_times_order():
    _validate_contest_times(None, None, None)  # 全空 OK
    _validate_contest_times("2026-01-01T00:00:00", "2026-01-02T00:00:00", "2026-01-03T00:00:00")
    with pytest.raises(ValueError):
        _validate_contest_times("2026-01-03T00:00:00", "2026-01-02T00:00:00", None)  # opens>closes
    with pytest.raises(ValueError):
        _validate_contest_times(None, "2026-01-03T00:00:00", "2026-01-02T00:00:00")  # closes>starts


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
    mgr.open_registration(c["id"])
    with pytest.raises(ValueError, match="报名已截止"):
        mgr.register(c["id"], users["u1"], users["b1"])


def test_register_allowed_before_deadline(setup):
    store, mgr, _, users = setup
    c = mgr.create(
        users["u1"], "DL", template_id="holdem_rr", game_id="holdem",
        registration_closes_at="2099-12-31T00:00:00",  # 未来
    )
    mgr.open_registration(c["id"])
    e = mgr.register(c["id"], users["u1"], users["b1"])  # 不抛
    assert e["bot_id"] == users["b1"]


def test_register_no_deadline_still_works(setup):
    """registration_closes_at 未设时不校验时间（兼容旧行为）。"""
    store, mgr, _, users = setup
    c = mgr.create(users["u1"], "NoDL", template_id="holdem_rr", game_id="holdem")
    mgr.open_registration(c["id"])
    mgr.register(c["id"], users["u1"], users["b1"])  # 不抛


# ── 两阶段排期 ──────────────────────────────────────────────────────────

def test_publish_creates_pairings_with_future_schedule(setup):
    """publish → published 态，pairing 有 scheduled_at（未来），dispatch 不开打。"""
    store, mgr, _, users = setup
    c = mgr.create(
        users["u1"], "2Phase", template_id="holdem_rr", game_id="holdem",
        starts_at="2099-12-31T23:59:59",
    )
    mgr.open_registration(c["id"])
    mgr.register(c["id"], users["u1"], users["b1"])
    mgr.register(c["id"], users["u2"], users["b2"])
    asyncio.run(mgr.publish(c["id"]))
    assert store.get_contest(c["id"])["status"] == "published"
    ps = store.list_contest_pairings(c["id"])
    assert len(ps) >= 1
    assert all(p.get("scheduled_at") for p in ps)  # 都有排期
    # dispatch 不开打（scheduled_at 在未来）
    asyncio.run(mgr._dispatch_pending(c["id"], 0))
    assert all(p["status"] == "pending" for p in store.list_contest_pairings(c["id"]))
    assert store.get_contest(c["id"])["status"] == "published"


def test_publish_then_schedule_arrives_dispatches(setup):
    """排期到点后 dispatch 开打，status→running。"""
    store, mgr, _, users = setup
    c = mgr.create(users["u1"], "2Phase", template_id="holdem_rr", game_id="holdem",
                   starts_at="2099-12-31T23:59:59")
    mgr.open_registration(c["id"])
    mgr.register(c["id"], users["u1"], users["b1"])
    mgr.register(c["id"], users["u2"], users["b2"])
    asyncio.run(mgr.publish(c["id"]))
    # 模拟排期到点：把 scheduled_at 改成过去
    for p in store.list_contest_pairings(c["id"]):
        store.update_contest_pairing(p["id"], scheduled_at="2020-01-01T00:00:00")
    asyncio.run(mgr._dispatch_pending(c["id"], 0))
    ps = store.list_contest_pairings(c["id"])
    assert any(p["status"] == "running" for p in ps)
    assert store.get_contest(c["id"])["status"] == "running"


def test_start_immediate_skips_schedule(setup):
    """start() 立即开赛：scheduled_at 全设 now，立即 dispatch。"""
    store, mgr, _, users = setup
    c = mgr.create(users["u1"], "Imm", template_id="holdem_rr", game_id="holdem")
    mgr.open_registration(c["id"])
    mgr.register(c["id"], users["u1"], users["b1"])
    mgr.register(c["id"], users["u2"], users["b2"])
    asyncio.run(mgr.start(c["id"]))
    # start 立即 running + pairing 已 dispatch
    assert store.get_contest(c["id"])["status"] == "running"
    assert any(p["status"] == "running" for p in store.list_contest_pairings(c["id"]))


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
    mgr.open_registration(c["id"])
    mgr.register(c["id"], users["u1"], users["b1"])
    mgr.register(c["id"], users["u2"], users["b2"])
    # 模拟截止时间已到
    store.update_contest(c["id"], registration_closes_at="2020-01-01T00:00:00")
    asyncio.run(sched._tick())
    assert store.get_contest(c["id"])["status"] == "published"


def test_scheduler_dispatches_published_on_schedule(setup):
    """published 且 scheduled_at<=now → scheduler dispatch → running。"""
    store, mgr, sched, users = setup
    c = mgr.create(users["u1"], "Auto", template_id="holdem_rr", game_id="holdem",
                   starts_at="2099-12-31T23:59:59")  # 未来（先 publish 不立即开打）
    mgr.open_registration(c["id"])
    mgr.register(c["id"], users["u1"], users["b1"])
    mgr.register(c["id"], users["u2"], users["b2"])
    asyncio.run(mgr.publish(c["id"]))
    assert store.get_contest(c["id"])["status"] == "published"
    # 模拟排期到点：把 scheduled_at 改成过去
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
    mgr.open_registration(c["id"])
    mgr.register(c["id"], users["u1"], users["b1"])
    mgr.register(c["id"], users["u2"], users["b2"])
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
    mgr.open_registration(c["id"])
    mgr.register(c["id"], users["u1"], users["b1"])
    mgr.register(c["id"], users["u2"], users["b2"])
    asyncio.run(mgr.publish(c["id"]))
    # publish 后 scheduled_at 已是过去 → _dispatch_pending（在 publish 内）应已转 running
    # 或至少 tick 后转 running。验证 tick 不抛、不重复
    pairings_before = len(store.list_contest_pairings(c["id"]))
    asyncio.run(sched._tick())
    pairings_after = len(store.list_contest_pairings(c["id"]))
    assert pairings_after == pairings_before, "tick 不应增加 pairing 数量"

