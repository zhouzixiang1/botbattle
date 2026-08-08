"""force-finish 端点 + Bot 换人语义收紧 + rating 并发串行化测试。

B2: ContestManager.finish —— running/rest→finished
B3: dispatch 移除 running 态（仅开赛前 draft/open/published + rest 受 flag）
B1: _rating_locks 按 (bot,game) 维度建锁（防同 bot 并发评分 lost-update）
"""
from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    s = Store(str(tmp_path / "ff.db"))
    # 建 organizer_id=1 引用的用户（FK 约束）
    from bzplat.backend.crypto import hash_password
    s.create_user("fforg", "fforg@x.com", hash_password("pw123456"), display_name="fforg")
    return s


def _mgr(tmp_path):
    s = _store(tmp_path)
    return s, ContestManager(s, MatchOrchestrator(s))


def _make_contest(store, *, status="draft", stages='[{"key":"s1","type":"swiss","rounds":1,"allow_bot_swap_in_rest":true}]'):
    c = store.create_contest(
        "ff赛", 1, game_id="holdem", template_id="holdem_swiss_ko",
        stages_json=stages, match_config_json="{}", phase="standalone", status=status,
    )
    return c["id"]


def test_force_finish_running_to_finished(tmp_path):
    store, mgr = _mgr(tmp_path)
    cid = _make_contest(store, status="running")
    c = asyncio.run(mgr.finish(cid))
    assert c["status"] == "finished"
    assert c.get("ends_at")


def test_force_finish_rejects_non_running(tmp_path):
    store, mgr = _mgr(tmp_path)
    cid = _make_contest(store, status="draft")
    with pytest.raises(ValueError):
        asyncio.run(mgr.finish(cid))


def test_dispatch_rejects_running(tmp_path):
    """Bot 换人在 running 态被拒（仅开赛前+休息可换）。dispatch 现为 async（加锁）。"""
    store, mgr = _mgr(tmp_path)
    cid = _make_contest(store, status="running")
    with pytest.raises(ValueError, match="不可更换"):
        asyncio.run(mgr.dispatch(contest_id=cid, user_id=1, bot_id=1, role="user"))


def test_rating_lock_dict_keyed_by_bot_game(tmp_path):
    """_rating_locks 字典按 (bot_id, game_id) 维度建锁。"""
    store = _store(tmp_path)
    orch = MatchOrchestrator(store)
    l1 = orch._rating_lock_for(5, "holdem")
    l2 = orch._rating_lock_for(5, "holdem")
    l3 = orch._rating_lock_for(5, "gomoku")
    assert l1 is l2  # 同 bot 同 game 复用
    assert l1 is not l3  # 不同 game 不同锁
