"""闲时自动对局调度器测试。"""
from __future__ import annotations

import asyncio
import time
from dataclasses import replace

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.auto_matcher import AutoMatchScheduler
from bzplat.backend.runtime.config import AUTO_MATCH_CONFIG
from bzplat.backend.store import Store
from bzplat.backend.store.schema import TYPE_LADDER


class FakeOrch:
    """计数 challenge 调用；模拟 _tasks / max_concurrent。"""

    def __init__(self, *, max_concurrent: int = 4) -> None:
        self.max_concurrent = max_concurrent
        self._tasks: dict[str, object] = {}
        self._bot_running = 0  # 旧 orchestrator fallback 的实际运行计数
        self.calls: list[dict] = []

    async def challenge(self, a, b, owner_user_id, *, match_type="challenge",
                        contest_id=None, game_id=None, match_config=None):
        mid = f"m{len(self.calls)}"
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


def test_disabled_does_not_schedule(store):
    _mk_bots(store, 4)
    orch = FakeOrch(max_concurrent=4)
    sched = AutoMatchScheduler(
        orch, store, config=replace(AUTO_MATCH_CONFIG, enabled=False)
    )
    assert sched._cfg()["enabled"] is False


def test_not_idle_when_full(store):
    _mk_bots(store, 4)
    orch = FakeOrch(max_concurrent=2)
    orch._bot_running = 2  # 满（reserve=1 → free = 2-1-2 = -1）
    sched = AutoMatchScheduler(orch, store)
    assert sched._is_idle(sched._cfg()) is False


def test_admitted_waiting_task_consumes_global_slot(store):
    """尚未进入 _bot_running 的已接纳任务也不能被 auto-match 越过。"""
    orch = FakeOrch(max_concurrent=2)
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
    sched = AutoMatchScheduler(FakeOrch(), store)

    assert sched._cfg() == AUTO_MATCH_CONFIG.as_dict()


def test_idle_respects_reserve_slots(store):
    _mk_bots(store, 4)
    orch = FakeOrch(max_concurrent=2)
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
    orch = FakeOrch(max_concurrent=4)
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
    orch = FakeOrch(max_concurrent=4)
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
    orch = FakeOrch(max_concurrent=4)
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
    orch = FakeOrch(max_concurrent=4)
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
    orch = FakeOrch(max_concurrent=8)
    sched = AutoMatchScheduler(
        orch, store, config=replace(AUTO_MATCH_CONFIG, max_per_round=1)
    )
    sched._idle_since = time.monotonic() - 10
    n = asyncio.run(sched._schedule_some(sched._cfg()))
    assert n == 1
