"""挑战对战测试：自博弈（同 bot）+ 版本快照（指定历史版本）。

覆盖：
- 自博弈同 bot 不再被拒（旧逻辑 challenger==opponent raise；现允许）。
- 版本快照：challenge 传 version_id → match_config 带 _bot_a/b_version_id →
  _run_match 解析版本路径（本测试只验 challenge 不抛 + match 行落库，不真跑二进制）。
- 指定不属于该 bot 的 version_id 应被拒。
"""
from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "sp.db"))


def test_selfplay_same_bot_allowed(tmp_path):
    """自博弈（同 bot 对战）不再被拒——challenge 同 bot_id 应成功建 match。"""
    s = _store(tmp_path)
    u = s.create_user("spuser", "sp@e.com", "x")["id"]
    b = s.create_bot(u, "selfbot", binary_path="/dev/null", format="elf", game_id="holdem")
    s.ensure_rating(b["id"])
    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
    # 旧逻辑会 raise "不能与自己对战"；现允许
    mid = asyncio.run(orch.challenge(b["id"], b["id"], u, match_config={"hands": 1}, game_id="holdem"))
    assert mid, "自博弈应成功建 match"
    m = s.get_match(mid)
    assert m["bot_a_id"] == b["id"] and m["bot_b_id"] == b["id"], "自博弈双方都是同 bot"
    s.close()


def test_challenge_version_pinning(tmp_path):
    """版本快照：challenge 传 version_id → match_config 带 _bot_a/b_version_id。"""
    s = _store(tmp_path)
    u = s.create_user("vpuser", "vp@e.com", "x")["id"]
    # 建两个 bot，各加一个额外版本
    ba = s.create_bot(u, "vbotA", binary_path="/tmp/a_v1", format="elf", game_id="holdem")
    bb = s.create_bot(u, "vbotB", binary_path="/tmp/b_v1", format="elf", game_id="holdem")
    s.ensure_rating(ba["id"]); s.ensure_rating(bb["id"])
    va2 = s.add_bot_version(ba["id"], binary_path="/tmp/a_v2", format="elf")
    vb2 = s.add_bot_version(bb["id"], binary_path="/tmp/b_v2", format="elf")
    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
    mid = asyncio.run(orch.challenge(
        ba["id"], bb["id"], u,
        match_config={"hands": 1}, game_id="holdem",
        bot_a_version_id=va2["id"], bot_b_version_id=vb2["id"],
    ))
    m = s.get_match(mid)
    mc = m["match_config"]
    if isinstance(mc, str):
        import json
        mc = json.loads(mc)
    assert mc.get("_bot_a_version_id") == va2["id"], "match_config 应快照 bot_a 版本"
    assert mc.get("_bot_b_version_id") == vb2["id"], "match_config 应快照 bot_b 版本"
    s.close()


def test_challenge_wrong_version_rejected(tmp_path):
    """指定不属于该 bot 的 version_id 应被拒。"""
    s = _store(tmp_path)
    u = s.create_user("wvuser", "wv@e.com", "x")["id"]
    ba = s.create_bot(u, "wvbotA", binary_path="/dev/null", format="elf", game_id="holdem")
    bb = s.create_bot(u, "wvbotB", binary_path="/dev/null", format="elf", game_id="holdem")
    s.ensure_rating(ba["id"]); s.ensure_rating(bb["id"])
    va_other = s.add_bot_version(ba["id"], binary_path="/tmp/a_v2", format="elf")
    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
    # va_other 属于 ba，但传给 bb 的 bot_b_version_id → 应拒
    with pytest.raises(ValueError, match="座位1 指定的版本"):
        asyncio.run(orch.challenge(
            ba["id"], bb["id"], u,
            match_config={"hands": 1}, game_id="holdem",
            bot_b_version_id=va_other["id"],  # 属于 ba 不属于 bb
        ))
    s.close()
