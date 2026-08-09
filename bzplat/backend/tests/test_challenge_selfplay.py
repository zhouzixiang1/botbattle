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
    mid = asyncio.run(orch.challenge(b["id"], b["id"], u, game_id="holdem"))
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
        game_id="holdem",
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
            game_id="holdem",
            bot_b_version_id=va_other["id"],  # 属于 ba 不属于 bb
        ))
    s.close()


def test_default_versions_are_frozen_before_deferred_runner_start(tmp_path):
    """未显式选版本也应在建局时冻结 current，排队后回滚不改变 runner 路径。"""
    from types import SimpleNamespace

    class CapturingRunner:
        def __init__(self):
            self.calls: list[tuple[str, str, tuple[str, str] | None]] = []

        async def run_binaries(self, path_a, path_b, *, runtime_modes=None, **_kwargs):
            self.calls.append((path_a, path_b, runtime_modes))
            return SimpleNamespace(
                rounds_played=1,
                rounds=[SimpleNamespace(deltas=[0, 0])],
                winner=None,
                events=[],
            )

    async def exercise():
        s = _store(tmp_path)
        uid = s.create_user("default-pin", "default-pin@e.com", "x")["id"]
        ba = s.create_bot(
            uid, "default-a", binary_path="/tmp/a-base", format="elf",
            game_id="holdem",
        )
        bb = s.create_bot(
            uid, "default-b", binary_path="/tmp/b-base", format="elf",
            game_id="holdem",
        )
        s.ensure_rating(ba["id"])
        s.ensure_rating(bb["id"])
        a1 = s.add_bot_version(
            ba["id"], binary_path="/tmp/a-v1", version=1,
            runtime_mode="traditional",
        )
        s.add_bot_version(
            ba["id"], binary_path="/tmp/a-v2", version=2,
            runtime_mode="longrunning",
        )
        b1 = s.add_bot_version(
            bb["id"], binary_path="/tmp/b-v1", version=1,
            runtime_mode="traditional",
        )
        s.add_bot_version(
            bb["id"], binary_path="/tmp/b-v2", version=2,
            runtime_mode="longrunning",
        )
        s.set_current_version(ba["id"], 1)
        s.set_current_version(bb["id"], 1)

        runner = CapturingRunner()
        orch = MatchOrchestrator(s, runner=runner, max_concurrent=1)
        mid = await orch.challenge(
            ba["id"], bb["id"], uid, game_id="holdem", defer_start=True,
        )
        match = s.get_match(mid)
        assert match["match_config"]["_bot_a_version_id"] == a1["id"]
        assert match["match_config"]["_bot_b_version_id"] == b1["id"]

        # 模拟 match 已排队但尚未获得 runner：此时 owner 上传/回滚到 v2。
        s.set_current_version(ba["id"], 2)
        s.set_current_version(bb["id"], 2)
        orch.start_prepared_match(mid)
        task = orch._tasks[mid]
        await task

        assert s.get_match(mid)["status"] == "completed"
        assert runner.calls == [
            ("/tmp/a-v1", "/tmp/b-v1", ("traditional", "traditional"))
        ]
        s.close()

    asyncio.run(exercise())


def test_legacy_bots_without_version_rows_fall_back_to_binary_path(tmp_path):
    """旧 bot 没有 bot_versions 行时仍可执行，不因默认版本快照为空而崩溃。"""
    from types import SimpleNamespace

    class CapturingRunner:
        def __init__(self):
            self.paths: list[tuple[str, str]] = []

        async def run_binaries(self, path_a, path_b, **_kwargs):
            self.paths.append((path_a, path_b))
            return SimpleNamespace(
                rounds_played=1,
                rounds=[SimpleNamespace(deltas=[0, 0])],
                winner=None,
                events=[],
            )

    async def exercise():
        s = _store(tmp_path)
        uid = s.create_user("legacy-pin", "legacy-pin@e.com", "x")["id"]
        ba = s.create_bot(
            uid, "legacy-a", binary_path="/tmp/legacy-a", format="elf",
            game_id="holdem",
        )
        bb = s.create_bot(
            uid, "legacy-b", binary_path="/tmp/legacy-b", format="elf",
            game_id="holdem",
        )
        s.ensure_rating(ba["id"])
        s.ensure_rating(bb["id"])
        runner = CapturingRunner()
        orch = MatchOrchestrator(s, runner=runner, max_concurrent=1)

        mid = await orch.challenge(
            ba["id"], bb["id"], uid, game_id="holdem", defer_start=True,
        )
        assert s.get_match(mid)["match_config"] == {}
        orch.start_prepared_match(mid)
        task = orch._tasks[mid]
        await task

        assert s.get_match(mid)["status"] == "completed"
        assert runner.paths == [("/tmp/legacy-a", "/tmp/legacy-b")]
        s.close()

    asyncio.run(exercise())


def test_selfplay_skips_rating_update(tmp_path):
    """自博弈完成不更新 Glicko 评分（防 _apply_ratings 同行双写损坏）。"""
    import os, tempfile
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    with tempfile.TemporaryDirectory() as td:
        s = Store(str(td + "/sr.db"))
        u = s.create_user("sruser", "sr@e.com", "x")["id"]
        b = s.create_bot(u, "srbot", binary_path="/dev/null", format="elf", game_id="holdem")
        s.ensure_rating(b["id"])
        r_before = s.get_rating(b["id"])
        # 模拟 _apply_ratings 对自博弈（同 bot）
        orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
        orch._apply_ratings(b["id"], b["id"], winner=0, ea=100, eb=-100)
        r_after = s.get_rating(b["id"])
        # 评分/胜负/对局数 应不变（自博弈跳过）
        assert r_after["rating"] == r_before["rating"], "自博弈不应改 rating"
        assert r_after["wins"] == r_before["wins"], "自博弈不应改 wins"
        assert r_after["matches_played"] == r_before["matches_played"], "自博弈不应改 matches_played"
        s.close()
