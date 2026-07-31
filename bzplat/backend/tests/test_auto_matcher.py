"""闲时自动对局调度器测试。"""
from __future__ import annotations

import asyncio
import time

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.auto_matcher import AutoMatchScheduler
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    SETTING_AUTO_MATCH_BOT_COOLDOWN,
    SETTING_AUTO_MATCH_ENABLED,
    SETTING_AUTO_MATCH_MIN_IDLE_SEC,
    SETTING_AUTO_MATCH_RESERVE_SLOTS,
    TYPE_LADDER,
)


class FakeOrch:
    """计数 challenge 调用；模拟 _tasks / max_concurrent。"""

    def __init__(self, *, max_concurrent: int = 4) -> None:
        self.max_concurrent = max_concurrent
        self._tasks: dict[str, object] = {}
        self.calls: list[dict] = []

    async def challenge(self, a, b, owner_user_id, *, hands=70, match_type="challenge",
                        contest_id=None, game_id=None):
        mid = f"m{len(self.calls)}"
        self._tasks[mid] = object()
        self.calls.append(
            {"a": a, "b": b, "owner": owner_user_id, "type": match_type, "game": game_id}
        )
        return mid


@pytest.fixture
def store(tmp_path):
    s = Store(str(tmp_path / "am.db"))
    # 种入默认 auto_match 配置
    s.seed_setting_if_absent(SETTING_AUTO_MATCH_ENABLED, "1")
    s.seed_setting_if_absent(SETTING_AUTO_MATCH_MIN_IDLE_SEC, "0")  # 测试立即触发
    s.seed_setting_if_absent(SETTING_AUTO_MATCH_BOT_COOLDOWN, "600")
    s.seed_setting_if_absent(SETTING_AUTO_MATCH_RESERVE_SLOTS, "1")
    return s


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
            is_public=1,
            game_id=game_id,
        )
        store.ensure_rating(b["id"])  # last_played_at = NULL（最陈旧）
        bots.append(b)
    return bots


def test_disabled_does_not_schedule(store):
    _mk_bots(store, 4)
    orch = FakeOrch(max_concurrent=4)
    sched = AutoMatchScheduler(orch, store)
    store.set_setting(SETTING_AUTO_MATCH_ENABLED, "0")
    # enabled=False 时 _is_idle 总返回 False
    assert sched._is_idle(sched._cfg()) is False


def test_not_idle_when_full(store):
    _mk_bots(store, 4)
    orch = FakeOrch(max_concurrent=2)
    orch._tasks["x"] = object()
    orch._tasks["y"] = object()  # 满（reserve=1 → free = 2-1-2 = -1）
    sched = AutoMatchScheduler(orch, store)
    assert sched._is_idle(sched._cfg()) is False


def test_idle_respects_reserve_slots(store):
    _mk_bots(store, 4)
    orch = FakeOrch(max_concurrent=2)
    orch._tasks["x"] = object()  # 1 running；reserve=1 → free=0 → 非闲
    sched = AutoMatchScheduler(orch, store)
    assert sched._is_idle(sched._cfg()) is False
    # reserve=0 → free=1 → 有空闲槽
    store.set_setting(SETTING_AUTO_MATCH_RESERVE_SLOTS, "0")
    orch._idle_since = None
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
