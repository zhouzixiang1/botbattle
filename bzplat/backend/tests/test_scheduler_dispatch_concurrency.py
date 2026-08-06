"""scheduler/maybe_finish 并发 dispatch 防双发测试（审计 P1）。

根因：scheduler tick（不持锁）调 _dispatch_pending，与 maybe_finish（持锁）链路
并发时，challenge() 的 await 让出期间另一路径读到同一 pending pairing 二次派发，
导致一个 pairing 挂两条 match（一条变孤儿）。

修复：_dispatch_pending 获取 per-contest 锁（_dispatch_pending_locked 是已持锁版）。
本测试 mock slow challenge 制造交错窗口，断言并发下每 pairing 只创建 1 个 match。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest


def _app(tmp_path):
    from bzplat.backend.main import create_app
    os.environ["BZ_BOT_LOCAL"] = "1"
    os.environ["BZ_SKIP_CAPTCHA"] = "1"
    return create_app(db_path=str(tmp_path / "dc.db"))


def _setup_contest(app):
    """建一个 running 赛事 + 1 个 pending pairing（2 bot）。"""
    from bzplat.backend.crypto import hash_password
    store = app.state.store
    org = store.create_user("org", "org@e.com", hash_password("pw123456"))
    store.update_user(org["id"], role="organizer", email_verified=1)
    b1 = store.create_bot(org["id"], "botA", binary_path="/tmp/a", format="elf", game_id="holdem")
    b2 = store.create_bot(org["id"], "botB", binary_path="/tmp/b", format="elf", game_id="holdem")
    cid = store.create_contest("DupTest", organizer_id=org["id"], game_id="holdem", status="running")["id"]
    with store._tx() as c:
        c.execute(
            "INSERT INTO contest_pairings(contest_id, stage_idx, round_num, bot_a_id, bot_b_id, status) "
            "VALUES(?,0,1,?,?,'pending')",
            (cid, b1["id"], b2["id"]),
        )
    return store, org, cid, b1, b2


@pytest.mark.asyncio
async def test_dispatch_no_double_under_concurrent_overlap(tmp_path):
    """并发 _dispatch_pending 调用不应双发——锁串行化，每 pairing 只 1 个 match。"""
    app = _app(tmp_path)
    store, org, cid, b1, b2 = _setup_contest(app)
    mgr = app.state.contest_manager
    # mock slow challenge 制造让出窗口
    dispatched: list[str] = []

    class _SlowOrch:
        async def challenge(self, *a, **kw):
            await asyncio.sleep(0.05)  # 让出控制权，制造交错窗口
            mid = f"m{len(dispatched)}"
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
