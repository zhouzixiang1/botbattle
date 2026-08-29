"""scheduler/maybe_finish 并发 dispatch 防双发测试（审计 P1）。

根因：scheduler tick（不持锁）调 _dispatch_pending，与 maybe_finish（持锁）链路
并发时，challenge() 的 await 让出期间另一路径读到同一 pending pairing 二次派发，
导致一个 pairing 挂两条 match（一条变孤儿）。

修复：_dispatch_pending 获取 per-contest 锁（_dispatch_pending_locked 是已持锁版）。
本测试 mock slow challenge 制造交错窗口，断言并发下每 pairing 只创建 1 个 match。
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path

import pytest


def _app(tmp_path):
    from bzplat.backend.main import create_app
    os.environ["BZ_BOT_LOCAL"] = "1"
    os.environ["BZ_SKIP_CAPTCHA"] = "1"
    return create_app(db_path=str(tmp_path / "dc.db"))


def _setup_contest(app, tmp_path: Path, *, status="running"):
    """建一个 running 赛事 + 1 个 pending pairing（2 bot）。"""
    from bzplat.backend.crypto import hash_password
    store = app.state.store
    org = store.create_user("org", "org@e.com", hash_password("pw123456"))
    store.update_user(org["id"], role="organizer", email_verified=1)
    player = store.create_user("player", "player@e.com", hash_password("pw123456"))
    store.update_user(player["id"], email_verified=1)
    path_a = tmp_path / "contest-bot-a.elf"
    path_b = tmp_path / "contest-bot-b.elf"
    path_a.write_bytes(b"contest bot A fixture")
    path_b.write_bytes(b"contest bot B fixture")
    b1 = store.create_bot(
        org["id"], "botA", binary_path=str(path_a), format="elf", game_id="holdem"
    )
    b2 = store.create_bot(
        player["id"], "botB", binary_path=str(path_b), format="elf", game_id="holdem"
    )
    cid = store.create_contest(
        "DupTest", organizer_id=org["id"], game_id="holdem", status=status,
        starts_at="2000-01-01T00:00:00" if status == "published" else None,
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )["id"]
    entry_a = store.add_contest_entry(cid, org["id"], b1["id"])
    entry_b = store.add_contest_entry(cid, player["id"], b2["id"])
    store.add_contest_pairing(
        cid, b1["id"], b2["id"], stage_idx=0, stage_key="rr",
        entry_a_id=entry_a["id"], entry_b_id=entry_b["id"], status="pending",
    )
    return store, org, cid, b1, b2


def _persist_prepared_contest_match(store, match_id: str, args, kwargs) -> str:
    """Persist the identity-complete prepared Match expected by the bind CAS."""
    bot_a_id, bot_b_id = (int(args[0]), int(args[1]))
    store.create_match(
        match_id,
        bot_a_id,
        bot_b_id,
        owner_id=int(kwargs["owner_user_id"]),
        contest_id=int(kwargs["contest_id"]),
        match_type=str(kwargs["match_type"]),
        game_id=str(kwargs["game_id"]),
        match_config={
            "duplicate": False,
            "_bot_a_version_id": kwargs.get("bot_a_version_id"),
            "_bot_b_version_id": kwargs.get("bot_b_version_id"),
        },
    )
    return match_id


def test_dispatch_no_double_under_concurrent_overlap(tmp_path):
    """并发 _dispatch_pending 调用不应双发——锁串行化，每 pairing 只 1 个 match。"""
    async def exercise():
        app = _app(tmp_path)
        store, org, cid, b1, b2 = _setup_contest(app, tmp_path)
        mgr = app.state.contest_manager
        # mock slow challenge 制造让出窗口
        dispatched: list[str] = []

        class _SlowOrch:
            async def challenge(self, *a, **kw):
                await asyncio.sleep(0.05)  # 让出控制权，制造交错窗口
                mid = f"m{len(dispatched)}"
                _persist_prepared_contest_match(store, mid, a, kw)
                dispatched.append(mid)
                return mid

        mgr.orch = _SlowOrch()
        # 并发两次 _dispatch_pending（模拟 scheduler tick + on_match_done 交错）
        await asyncio.gather(
            mgr._dispatch_pending(cid, 0),
            mgr._dispatch_pending(cid, 0),
        )
        # 应只派发 1 次（锁串行化，第二个读到 match_id 已设跳过）
        assert len(dispatched) == 1, f"双发！期望 1 个 match，实际 {dispatched}"
        # pairing 只挂 1 个 match_id
        pairings = store.list_contest_pairings(cid, stage_idx=0)
        assert len(pairings) == 1
        assert pairings[0]["match_id"] == dispatched[0]
        assert pairings[0]["status"] == "running"

    asyncio.run(exercise())


def test_cancel_waits_for_dispatch_and_rechecks_running_state(tmp_path):
    """dispatch 已开始时取消必须等同一把锁；首场成功后取消应被锁内复核拒绝。"""
    async def exercise():
        app = _app(tmp_path)
        store, _org, cid, _b1, _b2 = _setup_contest(
            app, tmp_path, status="published"
        )
        mgr = app.state.contest_manager
        entered = asyncio.Event()
        release = asyncio.Event()
        dispatched: list[str] = []

        class _BlockingOrch:
            async def challenge(self, *args, **kwargs):
                entered.set()
                await release.wait()
                _persist_prepared_contest_match(
                    store, "locked-match", args, kwargs
                )
                dispatched.append("locked-match")
                return "locked-match"

        mgr.orch = _BlockingOrch()
        dispatch_task = asyncio.create_task(mgr._dispatch_pending(cid, 0))
        await asyncio.wait_for(entered.wait(), timeout=1)
        cancel_task = asyncio.create_task(mgr.cancel(cid))
        await asyncio.sleep(0)
        assert not cancel_task.done(), "取消不得在 dispatch 持锁期间穿透并改写状态"

        release.set()
        await dispatch_task
        with pytest.raises(ValueError, match="不能取消"):
            await cancel_task

        assert dispatched == ["locked-match"]
        assert store.get_contest(cid)["status"] == "running"
        pairing = store.list_contest_pairings(cid, stage_idx=0)[0]
        assert pairing["status"] == "running"
        assert pairing["match_id"] == "locked-match"

    asyncio.run(exercise())


def test_cancel_queued_before_dispatch_prevents_dispatch(tmp_path):
    """取消先取得锁时，后续 dispatch 必须在锁内看到 cancelled 并零派发。"""
    async def exercise():
        app = _app(tmp_path)
        store, _org, cid, _b1, _b2 = _setup_contest(
            app, tmp_path, status="published"
        )
        mgr = app.state.contest_manager
        dispatched: list[str] = []

        class _RecordingOrch:
            async def challenge(self, *args, **kwargs):
                dispatched.append("unexpected")
                return "unexpected"

        mgr.orch = _RecordingOrch()
        lock = mgr._lock(cid)
        await lock.acquire()
        try:
            cancel_task = asyncio.create_task(mgr.cancel(cid))
            await asyncio.sleep(0)
            dispatch_task = asyncio.create_task(mgr._dispatch_pending(cid, 0))
            await asyncio.sleep(0)
        finally:
            lock.release()

        await asyncio.gather(cancel_task, dispatch_task)
        assert store.get_contest(cid)["status"] == "cancelled"
        assert dispatched == []
        pairing = store.list_contest_pairings(cid, stage_idx=0)[0]
        assert pairing["status"] == "pending"
        assert pairing["match_id"] is None

    asyncio.run(exercise())
