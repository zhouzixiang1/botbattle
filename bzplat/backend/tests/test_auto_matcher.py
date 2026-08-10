"""闲时自动对局调度器测试。"""
from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timezone
from threading import Barrier

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.auto_matcher import AutoMatchScheduler
from bzplat.backend.runtime.config import AUTO_MATCH_CONFIG, platform_local_day
from bzplat.backend.store import AutoMatchDailyCapReached, Store
from bzplat.backend.store.schema import (
    AUTO_MATCH_CLAIMS_MIGRATION_SENTINEL,
    TYPE_LADDER,
)


class FakeOrch:
    """计数 challenge 调用；模拟 _tasks / max_concurrent。"""

    def __init__(self, store: Store | None = None, *, max_concurrent: int = 4) -> None:
        self.store = store
        self.max_concurrent = max_concurrent
        self._tasks: dict[str, object] = {}
        self._bot_running = 0  # 旧 orchestrator fallback 的实际运行计数
        self.calls: list[dict] = []

    async def challenge(self, a, b, owner_user_id, *, match_type="challenge",
                        contest_id=None, game_id=None, match_config=None,
                        auto_match_daily_cap=None):
        mid = f"m{len(self.calls)}"
        if auto_match_daily_cap is not None:
            assert self.store is not None
            self.store.create_match(
                mid,
                a,
                b,
                owner_id=owner_user_id,
                contest_id=contest_id,
                match_type=match_type,
                game_id=game_id,
                match_config=match_config,
                auto_match_daily_cap=auto_match_daily_cap,
            )
        self._tasks[mid] = object()
        self.calls.append(
            {"a": a, "b": b, "owner": owner_user_id, "type": match_type, "game": game_id}
        )
        return mid


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "am.db"))


def _mk_bots(store: Store, n: int, game_id: str = "holdem", *, base: int = 0):
    """创建 n 个可对战的 public bot（带 binary_path + rating 行）。"""
    bots = []
    for i in range(n):
        k = base + i
        u = store.create_user(f"usr{k}", f"u{k}@ex.com", hash_password("password1"))
        b = store.create_bot(
            u["id"],
            f"bot{k}",
            binary_path=f"/tmp/b{k}",
            format="elf",
            is_active=1,
            game_id=game_id,
        )
        store.ensure_rating(b["id"])  # last_played_at = NULL（最陈旧）
        bots.append(b)
    return bots


def test_disabled_does_not_schedule(store, monkeypatch):
    _mk_bots(store, 4)
    orch = FakeOrch(store, max_concurrent=4)
    sched = AutoMatchScheduler(
        orch, store, config=replace(AUTO_MATCH_CONFIG, enabled=False)
    )
    assert sched._cfg()["enabled"] is False

    original_sleep = asyncio.sleep
    sleeps = 0

    async def stop_after_disabled_iteration(_delay):
        nonlocal sleeps
        sleeps += 1
        if sleeps > 1:
            raise asyncio.CancelledError
        await original_sleep(0)

    monkeypatch.setattr(
        "bzplat.backend.matches.auto_matcher.asyncio.sleep",
        stop_after_disabled_iteration,
    )

    async def run_loop() -> None:
        with pytest.raises(asyncio.CancelledError):
            await sched.loop()

    asyncio.run(run_loop())
    assert orch.calls == []


def test_not_idle_when_full(store):
    _mk_bots(store, 4)
    orch = FakeOrch(store, max_concurrent=2)
    orch._bot_running = 2  # 满（reserve=1 → free = 2-1-2 = -1）
    sched = AutoMatchScheduler(orch, store)
    assert sched._is_idle(sched._cfg()) is False


def test_admitted_waiting_task_consumes_global_slot(store):
    """尚未进入 _bot_running 的已接纳任务也不能被 auto-match 越过。"""
    orch = FakeOrch(store, max_concurrent=2)
    orch._tasks["contest-waiting"] = object()
    sched = AutoMatchScheduler(orch, store)

    assert orch._bot_running == 0
    assert sched._free_slots(reserve=1) == 0
    assert sched._is_idle(sched._cfg()) is False


def test_legacy_auto_match_rows_cannot_override_code_config(store):
    store.set_settings(
        {
            "auto_match_enabled": "0",
            "auto_match_interval_sec": "1",
            "auto_match_reserve_slots": "99",
        }
    )
    sched = AutoMatchScheduler(FakeOrch(store), store)

    assert sched._cfg() == AUTO_MATCH_CONFIG.as_dict()


def test_idle_respects_reserve_slots(store):
    _mk_bots(store, 4)
    orch = FakeOrch(store, max_concurrent=2)
    orch._bot_running = 1  # 1 running；reserve=1 → free=0 → 非闲
    sched = AutoMatchScheduler(orch, store)
    assert sched._is_idle(sched._cfg()) is False
    # reserve=0 → free=1 → 有空闲槽
    sched = AutoMatchScheduler(
        orch,
        store,
        config=replace(AUTO_MATCH_CONFIG, reserve=0, min_idle=0),
    )
    # 第一次设 idle_since
    assert sched._is_idle(sched._cfg()) is False
    # 第二次（min_idle=0）应触发
    assert sched._is_idle(sched._cfg()) is True


def test_schedules_stale_pair_same_game(store):
    bs = _mk_bots(store, 4, "holdem")
    orch = FakeOrch(store, max_concurrent=4)
    sched = AutoMatchScheduler(orch, store)
    sched._idle_since = time.monotonic() - 10  # 已连续空闲
    n = asyncio.run(sched._schedule_some(sched._cfg()))
    assert n >= 1
    assert len(orch.calls) >= 1
    c = orch.calls[0]
    assert c["type"] == TYPE_LADDER
    assert c["owner"] is None
    assert c["game"] == "holdem"
    assert c["a"] != c["b"]


def test_cooldown_skips_recently_scheduled_bot(store):
    _mk_bots(store, 2, "holdem")  # 只有 2 个 bot
    orch = FakeOrch(store, max_concurrent=4)
    sched = AutoMatchScheduler(orch, store)
    sched._idle_since = time.monotonic() - 10
    # 第一轮应安排
    n1 = asyncio.run(sched._schedule_some(sched._cfg()))
    assert n1 == 1
    # 两个 bot 都刚被安排（cooldown 内），第二轮无可配对
    sched._idle_since = time.monotonic() - 10
    n2 = asyncio.run(sched._schedule_some(sched._cfg()))
    assert n2 == 0


def test_no_cross_game_pairing(store):
    holdem = _mk_bots(store, 2, "holdem", base=0)
    gomoku = _mk_bots(store, 2, "gomoku", base=10)
    orch = FakeOrch(store, max_concurrent=4)
    sched = AutoMatchScheduler(orch, store)
    sched._idle_since = time.monotonic() - 10
    asyncio.run(sched._schedule_some(sched._cfg()))
    for c in orch.calls:
        # 同一对局双方必须是同一 game_id（由 orch 调用参数 game 反映）
        assert c["game"] in ("holdem", "gomoku")


def test_least_recently_played_orders_by_staleness(store):
    bs = _mk_bots(store, 3, "holdem")
    # 让 bot0 最近赛过，bot1 较早，bot2 从未赛（NULL，最陈旧）
    store.update_rating_row(bs[0]["id"], last_played_at="2026-07-31T10:00:00")
    store.update_rating_row(bs[1]["id"], last_played_at="2026-07-01T10:00:00")
    rows = store.least_recently_played("holdem", limit=10)
    ids = [r["bot_id"] for r in rows]
    # NULL（bot2）应在最前
    assert ids[0] == bs[2]["id"]
    assert ids[1] == bs[1]["id"]
    assert ids[2] == bs[0]["id"]


def test_count_matches_by_status(store):
    bs = _mk_bots(store, 2)
    store.create_match("m1", bs[0]["id"], bs[1]["id"], match_type="challenge")
    store.create_match("m2", bs[0]["id"], bs[1]["id"], match_type="challenge")
    store.update_match("m2", status="running")
    assert store.count_matches("pending") == 1
    assert store.count_matches("running") == 1
    assert store.count_matches() == 2


def test_ladder_match_type_writable(store):
    """迁移后 match_type='ladder' 写入合法。"""
    bs = _mk_bots(store, 2)
    store.create_match("L1", bs[0]["id"], bs[1]["id"], match_type="ladder")
    m = store.get_match("L1")
    assert m["match_type"] == "ladder"


# ── 增强：stale 过滤 / 定级优先 / 每日上限 / 每轮上限 ─────────────────────
def test_least_recently_played_stale_filter(store: Store):
    """stale_since 过滤：只回陈旧或从未赛的 bot。"""
    bs = _mk_bots(store, 3, "holdem")
    # bot0 刚赛过（新鲜），bot1 较旧，bot2 从未赛
    from datetime import datetime, timedelta
    fresh = (datetime.now() - timedelta(seconds=10)).isoformat(timespec="seconds")
    old = (datetime.now() - timedelta(seconds=7200)).isoformat(timespec="seconds")
    store.update_rating_row(bs[0]["id"], last_played_at=fresh)
    store.update_rating_row(bs[1]["id"], last_played_at=old)
    # stale_since=3600：应排除 fresh bot0，保留 old bot1 + NULL bot2
    rows = store.least_recently_played("holdem", stale_since=3600)
    ids = {r["bot_id"] for r in rows}
    assert bs[0]["id"] not in ids
    assert bs[1]["id"] in ids
    assert bs[2]["id"] in ids  # NULL（从未赛）始终算陈旧


def test_least_recently_played_placement_priority(store: Store):
    """定级期 bot（matches_played < N）排最前。"""
    bs = _mk_bots(store, 3, "holdem")
    # bot0/bot1 已赛多场（非定级），bot2 仅 1 场（定级期）
    store.update_rating_row(bs[0]["id"], matches_played=20, last_played_at="2020-01-01T00:00:00")
    store.update_rating_row(bs[1]["id"], matches_played=20, last_played_at="2019-01-01T00:00:00")
    store.update_rating_row(bs[2]["id"], matches_played=1, last_played_at="2025-01-01T00:00:00")
    rows = store.least_recently_played("holdem", placement_games=10)
    # 定级期 bot2 应排第一（尽管它最近才赛过）
    assert rows[0]["bot_id"] == bs[2]["id"]


def test_daily_cap_stops_scheduling(store: Store):
    """达每日上限后本轮不再调度。"""
    bs = _mk_bots(store, 2, "holdem")
    orch = FakeOrch(store, max_concurrent=4)
    sched = AutoMatchScheduler(
        orch, store, config=replace(AUTO_MATCH_CONFIG, daily_cap=1)
    )
    sched._idle_since = time.monotonic() - 10
    n1 = asyncio.run(sched._schedule_some(sched._cfg()))
    assert n1 == 1  # 第 1 场放行
    sched._idle_since = time.monotonic() - 10
    n2 = asyncio.run(sched._schedule_some(sched._cfg()))
    assert n2 == 0  # 达上限，停止
    assert sched.daily_count == 1


def test_max_per_round_limits(store: Store):
    """每轮上限：max_per_round=1 即使空闲槽更多也只补 1 场。"""
    bs = _mk_bots(store, 4, "holdem")
    orch = FakeOrch(store, max_concurrent=8)
    sched = AutoMatchScheduler(
        orch, store, config=replace(AUTO_MATCH_CONFIG, max_per_round=1)
    )
    sched._idle_since = time.monotonic() - 10
    n = asyncio.run(sched._schedule_some(sched._cfg()))
    assert n == 1


def test_daily_cap_survives_scheduler_restart(store: Store):
    """新 scheduler 实例仍从 DB 看到当天已创建的系统 auto-match。"""
    _mk_bots(store, 2, "holdem")
    config = replace(AUTO_MATCH_CONFIG, daily_cap=1)
    first = AutoMatchScheduler(FakeOrch(store), store, config=config)
    assert asyncio.run(first._schedule_some(first._cfg())) == 1

    restarted = AutoMatchScheduler(FakeOrch(store), store, config=config)
    assert restarted.daily_count == 1
    assert asyncio.run(restarted._schedule_some(restarted._cfg())) == 0


def test_daily_cap_is_atomic_across_store_instances(tmp_path):
    """多个进程等价的独立 SQLite 连接不能同时抢到最后一个配额。"""
    db_path = tmp_path / "shared.db"
    setup = Store(str(db_path))
    bots = _mk_bots(setup, 2, "holdem")
    setup.close()
    stores = [
        Store(str(db_path), auto_match_day_provider=lambda: "2026-08-10")
        for _ in range(6)
    ]
    barrier = Barrier(len(stores))

    def attempt(index: int) -> bool:
        barrier.wait()
        try:
            stores[index].create_match(
                f"atomic-{index}",
                bots[0]["id"],
                bots[1]["id"],
                owner_id=None,
                match_type=TYPE_LADDER,
                game_id="holdem",
                auto_match_daily_cap=2,
            )
        except AutoMatchDailyCapReached:
            return False
        return True

    try:
        with ThreadPoolExecutor(max_workers=len(stores)) as pool:
            accepted = list(pool.map(attempt, range(len(stores))))
        assert accepted.count(True) == 2
        assert stores[0].count_auto_matches_for_day("2026-08-10") == 2
    finally:
        for shared_store in stores:
            shared_store.close()


def test_platform_day_uses_asia_shanghai_boundary():
    just_before = datetime(2026, 8, 10, 15, 59, 59, tzinfo=timezone.utc)
    at_midnight = datetime(2026, 8, 10, 16, 0, 0, tzinfo=timezone.utc)
    assert platform_local_day(just_before) == "2026-08-10"
    assert platform_local_day(at_midnight) == "2026-08-11"


def test_new_platform_day_gets_fresh_quota(tmp_path):
    current_day = ["2026-08-10"]
    store = Store(
        str(tmp_path / "day-boundary.db"),
        auto_match_day_provider=lambda: current_day[0],
    )
    bots = _mk_bots(store, 2, "holdem")
    for day, match_id in (("2026-08-10", "day-1"), ("2026-08-11", "day-2")):
        current_day[0] = day
        store.create_match(
            match_id,
            bots[0]["id"],
            bots[1]["id"],
            owner_id=None,
            match_type=TYPE_LADDER,
            game_id="holdem",
            auto_match_daily_cap=1,
        )
    assert store.count_auto_matches_for_day("2026-08-10") == 1
    assert store.count_auto_matches_for_day("2026-08-11") == 1


def test_store_resolves_day_after_write_lock_crossing_midnight(tmp_path):
    """调用前的旧日期无权威性；拿写锁后必须按新日 claim/cap。"""
    current_day = ["2026-08-10"]
    holder: dict[str, Store] = {}

    def day_after_lock() -> str:
        store = holder["store"]
        assert store._conn.in_transaction is True
        return current_day[0]

    store = Store(
        str(tmp_path / "midnight-lock.db"),
        auto_match_day_provider=day_after_lock,
    )
    holder["store"] = store
    bots = _mk_bots(store, 2, "holdem")
    common = {
        "bot_a_id": bots[0]["id"],
        "bot_b_id": bots[1]["id"],
        "owner_id": None,
        "match_type": TYPE_LADDER,
        "game_id": "holdem",
        "auto_match_daily_cap": 1,
    }
    store.create_match("old-day-full", **common)

    # Scheduler/caller observed 23:59:59, then waited; Store gets the write
    # lock after Asia/Shanghai midnight and must ignore that stale observation.
    observed_before_lock = platform_local_day(
        datetime(2026, 8, 10, 15, 59, 59, tzinfo=timezone.utc)
    )
    assert observed_before_lock == "2026-08-10"
    current_day[0] = "2026-08-11"
    store.create_match("new-day-first", **common)

    assert store.count_auto_matches_for_day("2026-08-10") == 1
    assert store.count_auto_matches_for_day("2026-08-11") == 1
    with pytest.raises(AutoMatchDailyCapReached) as exc_info:
        store.create_match("new-day-over-cap", **common)
    assert exc_info.value.local_day == "2026-08-11"
    assert store.get_match("new-day-over-cap") is None


def test_non_auto_ladder_is_not_counted(store: Store):
    """只有显式系统 claim 计数，owner 非空/无 claim 的 ladder 均不误计。"""
    bots = _mk_bots(store, 2, "holdem")
    owner = store.get_bot(bots[0]["id"])["owner_id"]
    store.create_match(
        "user-ladder",
        bots[0]["id"],
        bots[1]["id"],
        owner_id=owner,
        match_type=TYPE_LADDER,
        game_id="holdem",
    )
    store.create_match(
        "internal-unclaimed-ladder",
        bots[0]["id"],
        bots[1]["id"],
        owner_id=None,
        match_type=TYPE_LADDER,
        game_id="holdem",
    )
    assert store.count_auto_matches_for_day(platform_local_day()) == 0


def test_concurrent_first_migration_is_idempotent(tmp_path):
    """两个 Store 同时首次升级旧库，均成功且回填/哨兵不重复。"""
    db_path = tmp_path / "legacy-shared.db"
    legacy = Store(str(db_path))
    bots = _mk_bots(legacy, 2, "holdem")
    legacy.create_match(
        "legacy-auto",
        bots[0]["id"],
        bots[1]["id"],
        owner_id=None,
        match_type=TYPE_LADDER,
        game_id="holdem",
    )
    with legacy._tx() as conn:
        conn.execute("DROP TABLE auto_match_daily_claims")
    legacy.close()

    barrier = Barrier(2)

    def initialize(_index: int) -> Store:
        barrier.wait()
        return Store(str(db_path))

    opened: list[Store] = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            opened = list(pool.map(initialize, range(2)))
        conn = opened[0]._conn
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_match_daily_claims WHERE match_id=?",
            (AUTO_MATCH_CLAIMS_MIGRATION_SENTINEL,),
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM auto_match_daily_claims WHERE match_id='legacy-auto'"
        ).fetchone()[0] == 1
    finally:
        for migrated_store in opened:
            migrated_store.close()
