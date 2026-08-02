"""预赛/决赛 P1：轮次冻结 + dispatch 闸门测试。

验证：
1. pairing 生成时 published_at 非空 + bot_a/b_version_id 快照（版本冻结）
2. dispatch 换 Bot 不改写已发布轮（published_at 非空的 pairing bot_id 不变）
3. _run_match 读冻结的 version 路径（赛事对局用发布时版本，非最新）
"""
from __future__ import annotations

import pytest

from bzplat.backend.store import Store


def _store(tmp_path):
    return Store(str(tmp_path / "p1.db"))


def test_pairings_have_freeze_columns(tmp_path):
    """contest_pairings 有 bot_a_version_id/bot_b_version_id/pairing_seed/published_at 列。"""
    s = _store(tmp_path)
    with s._tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(contest_pairings)")}
    s.close()
    for col in ("bot_a_version_id", "bot_b_version_id", "pairing_seed", "published_at"):
        assert col in cols, f"contest_pairings 应有 {col}（P1 冻结列）"


def test_published_pairing_snapshot_version(tmp_path):
    """_begin_stage 生成的 pairing published_at 非空 + version_id 快照。"""
    import asyncio

    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.matches.runner import MatchRunner
    from bzplat.backend.runtime.binary_runner import BinaryRunner

    s = _store(tmp_path)
    u = s.create_user("org1", "o@e.com", "x", role="organizer")["id"]
    users = [s.create_user(f"p1u{i}", f"u{i}@e.com", "x")["id"] for i in range(4)]
    bots = [
        s.create_bot(uid, f"p1bot{i}", binary_path="/tmp/b", format="elf", game_id="holdem")["id"]
        for i, uid in enumerate(users)
    ]
    c = s.create_contest(
        "P1冻结", organizer_id=u, game_id="holdem", hands_per_match=1,
        stages_json='[{"key":"s1","type":"round_robin","scoring":"poker_3_1_0"}]',
    )["id"]
    for uid, bid in zip(users, bots):
        s.add_contest_entry(c, uid, bid)
    s.update_contest(c, status="open")
    # 给每个 bot 写一个 version（_version_snapshot 取 latest）
    for bid in bots:
        s.add_bot_version(bid, binary_path=f"/tmp/v1_{bid}", version=1)
    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
    cm = ContestManager(s, orch)
    s.update_contest(c, status="running", current_stage_idx=0)

    async def _begin():
        await cm._begin_stage(c, 0)

    asyncio.run(_begin())
    ps = s.list_contest_pairings(c, stage_idx=0)
    assert len(ps) > 0
    for p in ps:
        assert p.get("published_at"), "生成的 pairing 应有 published_at（已发布）"
        assert p.get("bot_a_version_id") is not None, "应快照 bot_a_version_id"
        assert p.get("bot_b_version_id") is not None, "应快照 bot_b_version_id"
    s.close()


def test_dispatch_does_not_change_published_pairing(tmp_path):
    """dispatch 换 Bot 后，已发布轮（published_at 非空）的 pairing bot_id 不变。"""
    import asyncio

    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.matches.runner import MatchRunner
    from bzplat.backend.runtime.binary_runner import BinaryRunner

    s = _store(tmp_path)
    u = s.create_user("org2", "o2@e.com", "x", role="organizer")["id"]
    ua = s.create_user("dp1", "d1@e.com", "x")["id"]
    ub = s.create_user("dp2", "d2@e.com", "x")["id"]
    ba = s.create_bot(ua, "dpbotA", binary_path="/tmp/a", format="elf", game_id="holdem")["id"]
    bb = s.create_bot(ub, "dpbotB", binary_path="/tmp/b", format="elf", game_id="holdem")["id"]
    s.add_bot_version(ba, binary_path="/tmp/v_a", version=1)
    s.add_bot_version(bb, binary_path="/tmp/v_b", version=1)
    c = s.create_contest(
        "P1dispatch", organizer_id=u, game_id="holdem", hands_per_match=1,
        stages_json='[{"key":"s1","type":"double_round_robin","scoring":"poker_3_1_0","allow_bot_swap_in_rest":true}]',
    )["id"]
    s.add_contest_entry(c, ua, ba)
    s.add_contest_entry(c, ub, bb)
    s.update_contest(c, status="open")
    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
    cm = ContestManager(s, orch)
    s.update_contest(c, status="running", current_stage_idx=0)

    async def _begin():
        await cm._begin_stage(c, 0)

    asyncio.run(_begin())
    ps_before = s.list_contest_pairings(c, stage_idx=0)
    # 记录 ua 的 entry 在 pairing 里用的 bot（ba）
    e1 = s.get_entry(c, ua)
    # 换 Bot：ua 派 bb（或新 bot）
    ba2 = s.create_bot(ua, "dpbotA2", binary_path="/tmp/a2", format="elf", game_id="holdem")["id"]
    cm.dispatch(c, ua, ba2, role="organizer")
    ps_after = s.list_contest_pairings(c, stage_idx=0)
    # 已发布轮的 pairing bot_a/b_id 不应变（冻结）
    for before, after in zip(ps_before, ps_after):
        assert before["bot_a_id"] == after["bot_a_id"], (
            "dispatch 不应改已发布轮的 pairing bot_a_id"
        )
        assert before["bot_b_id"] == after["bot_b_id"], (
            "dispatch 不应改已发布轮的 pairing bot_b_id"
        )
    # 但 entry.bot_id 已更新为新 bot
    e1b = s.get_entry(c, ua)
    assert e1b["bot_id"] == ba2, "dispatch 应更新 entry.bot_id"
    s.close()


def test_get_bot_version_reads_frozen_path(tmp_path):
    """store.get_bot_version(version_id) 取冻结的 binary_path。"""
    s = _store(tmp_path)
    u = s.create_user("vrt", "v@e.com", "x")["id"]
    b = s.create_bot(u, "vbot", binary_path="/tmp/cur", format="elf", game_id="holdem")["id"]
    v = s.add_bot_version(b, binary_path="/tmp/frozen_v1", version=1)
    got = s.get_bot_version(v["id"])
    assert got is not None
    assert got["binary_path"] == "/tmp/frozen_v1"
    s.close()
