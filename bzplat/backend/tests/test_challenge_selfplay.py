"""挑战对战测试：自博弈（同 bot）+ 版本快照（指定历史版本）。

覆盖：
- 自博弈同 bot 不再被拒（旧逻辑 challenger==opponent raise；现允许）。
- 版本快照：challenge 传 version_id → match_config 带 _bot_a/b_version_id →
  _run_match 解析版本路径（本测试只验 challenge 不抛 + match 行落库，不真跑二进制）。
- 指定不属于该 bot 的 version_id 应被拒。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store
from bzplat.backend.tests.execution_helpers import (
    challenge_and_start,
    start_claimed_match,
)


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "sp.db"))


def _fixture_file(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"test fixture")
    return str(path)


class _NoopRunner:
    async def run_binaries(self, *_args, **_kwargs):
        return SimpleNamespace(
            rounds_played=1,
            rounds=[SimpleNamespace(deltas=[0, 0])],
            winner=None,
            events=[],
        )


def test_selfplay_same_bot_allowed(tmp_path):
    """自博弈（同 bot 对战）不再被拒——challenge 同 bot_id 应成功建 match。"""
    s = _store(tmp_path)
    u = s.create_user("spuser", "sp@e.com", "x")["id"]
    binary_path = _fixture_file(tmp_path, "selfbot.bin")
    b = s.create_bot(
        u, "selfbot", binary_path=binary_path, format="elf", game_id="holdem"
    )
    s.ensure_rating(b["id"])
    orch = MatchOrchestrator(s, runner=_NoopRunner(), max_concurrent=1)
    # 旧逻辑会 raise "不能与自己对战"；现允许
    mid = asyncio.run(
        challenge_and_start(
            orch,
            b["id"], b["id"], u, game_id="holdem", defer_start=True
        )
    )
    assert mid, "自博弈应成功建 match"
    m = s.get_match(mid)
    assert m["bot_a_id"] == b["id"] and m["bot_b_id"] == b["id"], "自博弈双方都是同 bot"
    s.close()


def test_challenge_version_pinning(tmp_path):
    """版本快照：challenge 传 version_id → match_config 带 _bot_a/b_version_id。"""
    s = _store(tmp_path)
    u = s.create_user("vpuser", "vp@e.com", "x")["id"]
    # 建两个 bot，各加一个额外版本
    a_v1 = _fixture_file(tmp_path, "a_v1")
    b_v1 = _fixture_file(tmp_path, "b_v1")
    a_v2 = _fixture_file(tmp_path, "a_v2")
    b_v2 = _fixture_file(tmp_path, "b_v2")
    ba = s.create_bot(u, "vbotA", binary_path=a_v1, format="elf", game_id="holdem")
    bb = s.create_bot(u, "vbotB", binary_path=b_v1, format="elf", game_id="holdem")
    s.ensure_rating(ba["id"]); s.ensure_rating(bb["id"])
    va2 = s.add_bot_version(ba["id"], binary_path=a_v2, format="elf")
    vb2 = s.add_bot_version(bb["id"], binary_path=b_v2, format="elf")
    orch = MatchOrchestrator(s, runner=_NoopRunner(), max_concurrent=1)
    mid = asyncio.run(challenge_and_start(
        orch,
        ba["id"], bb["id"], u,
        game_id="holdem",
        bot_a_version_id=va2["id"], bot_b_version_id=vb2["id"],
        defer_start=True,
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
    a_base = _fixture_file(tmp_path, "wrong-a-base")
    b_base = _fixture_file(tmp_path, "wrong-b-base")
    a_v2 = _fixture_file(tmp_path, "wrong-a-v2")
    ba = s.create_bot(u, "wvbotA", binary_path=a_base, format="elf", game_id="holdem")
    bb = s.create_bot(u, "wvbotB", binary_path=b_base, format="elf", game_id="holdem")
    s.ensure_rating(ba["id"]); s.ensure_rating(bb["id"])
    va_other = s.add_bot_version(ba["id"], binary_path=a_v2, format="elf")
    orch = MatchOrchestrator(s, runner=_NoopRunner(), max_concurrent=1)
    # va_other 属于 ba，但传给 bb 的 bot_b_version_id → 应拒
    with pytest.raises(ValueError, match="座位 2 指定的版本"):
        asyncio.run(orch.challenge(
            ba["id"], bb["id"], u,
            game_id="holdem",
            bot_b_version_id=va_other["id"],  # 属于 ba 不属于 bb
        ))
    s.close()


def test_default_versions_are_frozen_before_deferred_runner_start(tmp_path):
    """未显式选版本也应在建局时冻结 current，排队后回滚不改变 runner 路径。"""
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
        paths = {
            name: _fixture_file(tmp_path, name)
            for name in ("a-base", "b-base", "a-v1", "a-v2", "b-v1", "b-v2")
        }
        ba = s.create_bot(
            uid, "default-a", binary_path=paths["a-base"], format="elf",
            game_id="holdem",
        )
        bb = s.create_bot(
            uid, "default-b", binary_path=paths["b-base"], format="elf",
            game_id="holdem",
        )
        s.ensure_rating(ba["id"])
        s.ensure_rating(bb["id"])
        a1 = s.add_bot_version(
            ba["id"], binary_path=paths["a-v1"], version=1,
            runtime_mode="traditional",
        )
        s.add_bot_version(
            ba["id"], binary_path=paths["a-v2"], version=2,
            runtime_mode="longrunning",
        )
        b1 = s.add_bot_version(
            bb["id"], binary_path=paths["b-v1"], version=1,
            runtime_mode="traditional",
        )
        s.add_bot_version(
            bb["id"], binary_path=paths["b-v2"], version=2,
            runtime_mode="longrunning",
        )
        s.set_current_version(ba["id"], 1)
        s.set_current_version(bb["id"], 1)

        runner = CapturingRunner()
        orch = MatchOrchestrator(s, runner=runner, max_concurrent=1)
        mid = await challenge_and_start(
            orch,
            ba["id"], bb["id"], uid, game_id="holdem", defer_start=True,
        )
        match = s.get_match(mid)
        assert match["match_config"]["_bot_a_version_id"] == a1["id"]
        assert match["match_config"]["_bot_b_version_id"] == b1["id"]

        # 模拟 match 已排队但尚未获得 runner：此时 owner 上传/回滚到 v2。
        s.set_current_version(ba["id"], 2)
        s.set_current_version(bb["id"], 2)
        start_claimed_match(orch, mid)
        task = orch._tasks[mid]
        await task

        assert s.get_match(mid)["status"] == "completed"
        assert runner.calls == [
            (paths["a-v1"], paths["b-v1"], ("traditional", "traditional"))
        ]
        s.close()

    asyncio.run(exercise())


def test_legacy_bots_without_version_rows_fall_back_to_binary_path(tmp_path):
    """旧 bot 没有 bot_versions 行时仍可执行，不因默认版本快照为空而崩溃。"""
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
        path_a = _fixture_file(tmp_path, "legacy-a")
        path_b = _fixture_file(tmp_path, "legacy-b")
        ba = s.create_bot(
            uid, "legacy-a", binary_path=path_a, format="elf",
            game_id="holdem",
        )
        bb = s.create_bot(
            uid, "legacy-b", binary_path=path_b, format="elf",
            game_id="holdem",
        )
        s.ensure_rating(ba["id"])
        s.ensure_rating(bb["id"])
        runner = CapturingRunner()
        orch = MatchOrchestrator(s, runner=runner, max_concurrent=1)

        mid = await challenge_and_start(
            orch,
            ba["id"], bb["id"], uid, game_id="holdem", defer_start=True,
        )
        match_config = s.get_match(mid)["match_config"]
        assert match_config["_rating_eligible"] is False
        assert match_config["_rating_reason"] == "same_owner"
        assert match_config["_execution_request_id"].startswith("req_")
        start_claimed_match(orch, mid)
        task = orch._tasks[mid]
        await task

        assert s.get_match(mid)["status"] == "completed"
        assert runner.paths == [(path_a, path_b)]
        assert s.is_match_rating_settled(mid) is True
        assert s.get_rating(ba["id"])["matches_played"] == 0
        assert s.get_rating(bb["id"])["matches_played"] == 0
        assert s.list_rating_history(ba["id"]) == []
        assert s.list_rating_history(bb["id"]) == []
        assert s.head_to_head(ba["id"], bb["id"]) is None
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


def test_different_owner_challenge_remains_rated(tmp_path):
    """只有不同拥有者的普通挑战进入 Glicko 与 pair/history 投影。"""
    async def exercise():
        s = _store(tmp_path)
        owner_a = s.create_user("rated-a", "rated-a@e.com", "x")["id"]
        owner_b = s.create_user("rated-b", "rated-b@e.com", "x")["id"]
        path_a = _fixture_file(tmp_path, "rated-a")
        path_b = _fixture_file(tmp_path, "rated-b")
        bot_a = s.create_bot(
            owner_a, "rated-a", binary_path=path_a, format="elf", game_id="holdem"
        )
        bot_b = s.create_bot(
            owner_b, "rated-b", binary_path=path_b, format="elf", game_id="holdem"
        )
        s.select_ranked_bot(int(owner_a), int(bot_a["id"]), if_empty=True)
        s.select_ranked_bot(int(owner_b), int(bot_b["id"]), if_empty=True)
        s.ensure_rating(bot_a["id"])
        s.ensure_rating(bot_b["id"])
        orch = MatchOrchestrator(s, runner=_NoopRunner(), max_concurrent=1)
        match_id = await challenge_and_start(
            orch,
            bot_a["id"], bot_b["id"], owner_a,
            game_id="holdem", defer_start=True,
        )
        assert s.get_match(match_id)["match_config"]["_rating_eligible"] is True
        start_claimed_match(orch, match_id)
        await orch._tasks[match_id]

        assert s.is_match_rating_settled(match_id) is True
        assert s.get_rating(bot_a["id"])["matches_played"] == 1
        assert s.get_rating(bot_b["id"])["matches_played"] == 1
        assert len(s.list_rating_history(bot_a["id"])) == 1
        assert s.head_to_head(bot_a["id"], bot_b["id"])["samples"] == 1
        s.close()

    asyncio.run(exercise())
