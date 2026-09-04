"""Frozen Bot versions fail closed across every match execution path."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os

import pytest

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.matches.orchestrator import (
    BotVersionUnavailableError,
    MatchOrchestrator,
)
from bzplat.backend.store import Store
from bzplat.backend.tests.execution_helpers import (
    challenge_and_start,
    human_and_start,
    start_claimed_match,
)


class NeverRunner:
    """Runner spy: version-integrity failures must stop before this boundary."""

    def __init__(self) -> None:
        self.calls = 0

    async def run_binaries(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("失效的冻结版本不得进入 Bot runner")

    async def run_duplicate(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("失效的冻结版本不得进入 duplicate runner")

    async def run_bot_vs_human(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("失效的冻结版本不得进入 human runner")


def _create_versioned_bots(
    store: Store, tmp_path, prefix: str, *, game_id: str = "holdem"
):
    users = [
        store.create_user(
            f"{prefix}u{i}", f"{prefix}u{i}@example.com", "hash"
        )
        for i in range(2)
    ]
    paths = [tmp_path / f"{prefix}-frozen-{i}" for i in range(2)]
    for path in paths:
        path.write_bytes(b"pre-integrity historical executable placeholder")
    bots = [
        store.create_bot(
            users[i]["id"],
            f"{prefix}b{i}",
            binary_path=str(paths[i]),
            game_id=game_id,
        )
        for i in range(2)
    ]
    versions = [
        store.add_bot_version(
            bots[i]["id"],
            binary_path=str(paths[i]),
            version=1,
        )
        for i in range(2)
    ]
    for bot in bots:
        store.ensure_rating(bot["id"], game_id=game_id)
    return users, bots, versions


def _ratings(store: Store, bots: list[dict], game_id: str) -> list[dict]:
    return [dict(store.get_rating(bot["id"], game_id=game_id)) for bot in bots]


def _set_match_version(
    store: Store, match_id: str, *, game_id: str, key: str, version_id: int | None
) -> None:
    match = store.get_match(match_id)
    config = dict(match.get("match_config") or {})
    if version_id is None:
        config.pop(key, None)
    else:
        config[key] = version_id
    with store._tx() as conn:
        conn.execute(
            f"UPDATE matches_{game_id} SET match_config=? WHERE id=?",
            (json.dumps(config), match_id),
        )


def _drain(queue: asyncio.Queue) -> list[dict]:
    events: list[dict] = []
    while not queue.empty():
        events.append(queue.get_nowait())
    return events


@pytest.mark.parametrize(
    "corruption",
    ["missing_row", "cross_bot", "empty_path", "missing_snapshot"],
)
def test_bot_match_never_falls_back_from_invalid_frozen_version(
    tmp_path,
    caplog: pytest.LogCaptureFixture,
    corruption: str,
):
    """普通对局中所有冻结引用损坏都 aborted，且不执行镜像或评分。"""

    async def exercise() -> None:
        store = Store(str(tmp_path / f"ordinary-{corruption}.db"))
        users, bots, versions = _create_versioned_bots(
            store, tmp_path, "ordinary"
        )
        runner = NeverRunner()
        orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
        before = _ratings(store, bots, "holdem")
        match_id = await challenge_and_start(
            orch,
            bots[0]["id"],
            bots[1]["id"],
            users[0]["id"],
            game_id="holdem",
            defer_start=True,
        )

        if corruption == "missing_row":
            with store._tx() as conn:
                conn.execute("DELETE FROM bot_versions WHERE id=?", (versions[0]["id"],))
        elif corruption == "cross_bot":
            _set_match_version(
                store,
                match_id,
                game_id="holdem",
                key="_bot_a_version_id",
                version_id=versions[1]["id"],
            )
        elif corruption == "empty_path":
            with store._tx() as conn:
                conn.execute(
                    "UPDATE bot_versions SET binary_path='' WHERE id=?",
                    (versions[0]["id"],),
                )
        else:
            # A Bot with version history is not a pre-version legacy Bot.  A
            # missing snapshot must not silently select its mutable mirror.
            _set_match_version(
                store,
                match_id,
                game_id="holdem",
                key="_bot_a_version_id",
                version_id=None,
            )

        queue = orch.subscribe(match_id)
        start_claimed_match(orch, match_id)
        task = orch._tasks[match_id]
        await task

        match = store.get_match(match_id)
        assert match["status"] == "aborted"
        assert match["reason"] == "version_unavailable"
        assert match["winner"] is None
        assert int(match["technical_loss"] or 0) == 0
        assert runner.calls == 0
        assert _ratings(store, bots, "holdem") == before
        assert not store.is_match_rating_settled(match_id)
        assert match_id not in orch._tasks
        assert match_id not in orch._sse

        errors = [event for event in _drain(queue) if event.get("type") == "error"]
        assert errors == [
            {
                "type": "error",
                "reason": "version_unavailable",
            }
        ]
        assert str(tmp_path) not in json.dumps(errors, ensure_ascii=False)
        store.close()

    with caplog.at_level(logging.ERROR):
        asyncio.run(exercise())
    assert str(tmp_path) not in caplog.text


def test_human_match_cross_bot_version_aborts_and_releases_user(tmp_path):
    """人机局的跨 Bot 冻结引用不启动 runner，并释放全部内存占用。"""

    async def exercise() -> None:
        store = Store(str(tmp_path / "human-cross-bot.db"))
        users, bots, versions = _create_versioned_bots(
            store, tmp_path, "human", game_id="gomoku"
        )
        runner = NeverRunner()
        orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
        before = _ratings(store, bots, "gomoku")
        match_id = await human_and_start(
            orch,
            bots[0]["id"],
            users[0]["id"],
            human_seat=1,
            game_id="gomoku",
            defer_start=True,
        )
        _set_match_version(
            store,
            match_id,
            game_id="gomoku",
            key="_bot_a_version_id",
            version_id=versions[1]["id"],
        )
        queue = orch.subscribe(match_id)
        start_claimed_match(orch, match_id)
        task = orch._tasks[match_id]
        await task

        match = store.get_match(match_id)
        assert match["status"] == "aborted"
        assert match["reason"] == "version_unavailable"
        assert match["winner"] is None
        assert runner.calls == 0
        assert _ratings(store, bots, "gomoku") == before
        assert not store.is_match_rating_settled(match_id)
        assert match_id not in orch._tasks
        assert users[0]["id"] not in orch._human_active_users
        assert not any(key[0] == match_id for key in orch._human_turns)
        assert match_id not in orch._sse
        errors = [event for event in _drain(queue) if event.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "version_unavailable"
        assert str(tmp_path) not in json.dumps(errors, ensure_ascii=False)
        store.close()

    asyncio.run(exercise())


def test_pre_integrity_missing_file_is_rejected_before_match_creation(tmp_path):
    """旧版本即使没有 checksum/size，也不能把缺文件延迟成 Bot 技术负。"""

    async def exercise() -> None:
        store = Store(str(tmp_path / "legacy-missing-file.db"))
        users, bots, versions = _create_versioned_bots(
            store, tmp_path, "legacymissing"
        )
        missing = versions[0]["binary_path"]
        os.unlink(missing)
        runner = NeverRunner()
        orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
        before = _ratings(store, bots, "holdem")

        with pytest.raises(BotVersionUnavailableError, match="version_unavailable"):
            await orch.challenge(
                bots[0]["id"], bots[1]["id"], users[0]["id"], game_id="holdem"
            )

        assert store.list_matches() == []
        assert orch._tasks == {}
        assert runner.calls == 0
        assert _ratings(store, bots, "holdem") == before

        manager = ContestManager(store, orch)
        with pytest.raises(ValueError, match="version_unavailable"):
            manager._version_snapshot(bots[0]["id"], bots[1]["id"])
        assert store.list_matches() == []
        store.close()

    asyncio.run(exercise())


def test_checksum_tamper_is_rejected_during_snapshot_before_match_creation(tmp_path):
    """已知损坏在挑战/赛事快照阶段拒绝，不先创建 match 或 task。"""

    async def exercise() -> None:
        store = Store(str(tmp_path / "snapshot-integrity.db"))
        user = store.create_user("snapshotu", "snapshotu@example.com", "hash")
        original = b"snapshot-immutable-linux-binary"
        paths = [tmp_path / "snapshot-a", tmp_path / "snapshot-b"]
        for path in paths:
            path.write_bytes(original)
        bots = [
            store.create_bot(
                user["id"], f"snapshotb{i}", binary_path=str(paths[i]),
                game_id="holdem",
            )
            for i in range(2)
        ]
        for i, bot in enumerate(bots):
            store.add_bot_version(
                bot["id"],
                binary_path=str(paths[i]),
                version=1,
                checksum=hashlib.sha256(original).hexdigest(),
                size_bytes=len(original),
            )
            store.ensure_rating(bot["id"], game_id="holdem")

        changed = bytearray(original)
        changed[-1] ^= 0x01
        paths[0].write_bytes(changed)
        runner = NeverRunner()
        orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
        before = _ratings(store, bots, "holdem")

        with pytest.raises(BotVersionUnavailableError, match="version_unavailable"):
            await orch.challenge(
                bots[0]["id"], bots[1]["id"], user["id"], game_id="holdem"
            )
        assert runner.calls == 0
        assert orch._tasks == {}
        assert store.list_matches() == []
        assert _ratings(store, bots, "holdem") == before

        # Contest publication uses the same integrity primitive before it writes
        # a pairing snapshot.
        manager = ContestManager(store, orch)
        with pytest.raises(ValueError, match="version_unavailable"):
            manager._version_snapshot(bots[0]["id"], bots[1]["id"])
        assert store.list_matches() == []
        store.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("tamper", ["same_size", "size_changed"])
def test_checksum_or_size_tamper_invalidates_cache_before_runner(tmp_path, tamper: str):
    """冻结文件被原位改写后，即使已缓存验证结果也必须 fail-closed。"""

    async def exercise() -> None:
        store = Store(str(tmp_path / f"integrity-{tamper}.db"))
        user = store.create_user("integrityu", "integrityu@example.com", "hash")
        original_a = b"immutable-linux-binary-a"
        original_b = b"immutable-linux-binary-b"
        binary_a = tmp_path / f"{tamper}-a"
        binary_b = tmp_path / f"{tamper}-b"
        binary_a.write_bytes(original_a)
        binary_b.write_bytes(original_b)
        bots = [
            store.create_bot(
                user["id"], "integritya", binary_path=str(binary_a), game_id="holdem"
            ),
            store.create_bot(
                user["id"], "integrityb", binary_path=str(binary_b), game_id="holdem"
            ),
        ]
        versions = [
            store.add_bot_version(
                bots[0]["id"],
                binary_path=str(binary_a),
                version=1,
                checksum=hashlib.sha256(original_a).hexdigest(),
                size_bytes=len(original_a),
            ),
            store.add_bot_version(
                bots[1]["id"],
                binary_path=str(binary_b),
                version=1,
                checksum=hashlib.sha256(original_b).hexdigest(),
                size_bytes=len(original_b),
            ),
        ]
        for bot in bots:
            store.ensure_rating(bot["id"], game_id="holdem")

        runner = NeverRunner()
        orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
        # Prime the integrity cache with the valid file.  The subsequent stat
        # signature change must invalidate this exact entry.
        assert orch._runtime_for_bot_version(
            store.get_bot(bots[0]["id"]), versions[0]["id"], seat=0
        )[0] == str(binary_a)
        match_id = await challenge_and_start(
            orch,
            bots[0]["id"],
            bots[1]["id"],
            user["id"],
            game_id="holdem",
            defer_start=True,
        )
        before = _ratings(store, bots, "holdem")

        previous = binary_a.stat()
        if tamper == "same_size":
            changed = bytearray(original_a)
            changed[0] ^= 0x01
            binary_a.write_bytes(changed)
        else:
            binary_a.write_bytes(original_a + b"-extra")
        # Restore the original mtime deliberately.  The cache must still miss
        # because ctime changed (and its key also includes dev/inode/size).
        os.utime(binary_a, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        changed_stat = binary_a.stat()
        assert changed_stat.st_mtime_ns == previous.st_mtime_ns
        assert changed_stat.st_ctime_ns != previous.st_ctime_ns

        queue = orch.subscribe(match_id)
        start_claimed_match(orch, match_id)
        task = orch._tasks[match_id]
        await task

        match = store.get_match(match_id)
        assert match["status"] == "aborted"
        assert match["reason"] == "version_unavailable"
        assert match["winner"] is None
        assert runner.calls == 0
        assert _ratings(store, bots, "holdem") == before
        assert not store.is_match_rating_settled(match_id)
        assert match_id not in orch._tasks
        errors = [event for event in _drain(queue) if event.get("type") == "error"]
        assert len(errors) == 1
        assert errors[0]["reason"] == "version_unavailable"
        assert str(binary_a) not in json.dumps(errors, ensure_ascii=False)
        store.close()

    asyncio.run(exercise())


def test_contest_empty_frozen_path_aborts_and_resets_pairing_safely(tmp_path):
    """赛事局无效版本无裁决、触发统一回调，并安全复位 pairing 等待修复。"""

    async def exercise() -> None:
        store = Store(str(tmp_path / "contest-empty-path.db"))
        users, bots, versions = _create_versioned_bots(
            store, tmp_path, "contest"
        )
        organizer = store.create_user(
            "contestorg", "contestorg@example.com", "hash", role="organizer"
        )
        contest = store.create_contest(
            "冻结版本失效赛事",
            organizer["id"],
            status="published",
            game_id="holdem",
            stages_json='[{"key":"ko","type":"single_elimination"}]',
        )
        entries = [
            store.add_contest_entry(contest["id"], users[i]["id"], bots[i]["id"])
            for i in range(2)
        ]
        pairings = store.create_contest_stage_pairings(
            contest["id"],
            0,
            [
                {
                    "bot_a_id": bots[0]["id"],
                    "bot_b_id": bots[1]["id"],
                    "entry_a_id": entries[0]["id"],
                    "entry_b_id": entries[1]["id"],
                    "bot_a_version_id": versions[0]["id"],
                    "bot_b_version_id": versions[1]["id"],
                    "round_num": 1,
                    "stage_key": "ko",
                    "bracket_slot": 0,
                    "published_at": "2026-01-01T00:00:00",
                }
            ],
            expected_current_stage_idx=0,
            expected_status="published",
            activate_running=True,
        )
        assert len(pairings) == 1
        assert store.contest_stage_manifest_is_valid(contest["id"], 0)
        pairing = pairings[0]
        runner = NeverRunner()
        orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
        manager = ContestManager(store, orch)
        callbacks: list[tuple[str, int | None]] = []

        async def on_match_done(match_id: str, contest_id: int | None) -> None:
            callbacks.append((match_id, contest_id))
            assert contest_id is not None
            await manager.handle_match_done(match_id, contest_id)

        orch.on_match_done = on_match_done
        before = _ratings(store, bots, "holdem")
        match_id = await challenge_and_start(
            orch,
            bots[0]["id"],
            bots[1]["id"],
            organizer["id"],
            match_type="contest",
            contest_id=contest["id"],
            contest_pairing_id=pairing["id"],
            game_id="holdem",
            bot_a_version_id=versions[0]["id"],
            bot_b_version_id=versions[1]["id"],
            defer_start=True,
        )
        with store._tx() as conn:
            conn.execute(
                "UPDATE bot_versions SET binary_path='' WHERE id=?",
                (versions[0]["id"],),
            )

        start_claimed_match(orch, match_id)
        task = orch._tasks[match_id]
        await task

        match = store.get_match(match_id)
        assert match["status"] == "aborted"
        assert match["reason"] == "version_unavailable"
        assert match["winner"] is None
        assert runner.calls == 0
        assert _ratings(store, bots, "holdem") == before
        assert not store.is_match_rating_settled(match_id)
        assert callbacks == [(match_id, contest["id"])]
        assert match_id not in orch._tasks

        refreshed = store.list_contest_pairings(contest["id"])
        assert len(refreshed) == 1
        assert refreshed[0]["id"] == pairing["id"]
        assert refreshed[0]["status"] == "pending"
        assert refreshed[0]["match_id"] is None
        assert refreshed[0]["scheduled_at"], "永久故障也不得在回调栈内热循环重派"
        assert store.get_contest(contest["id"])["status"] == "running"
        assert store.list_official_results(contest["id"]) == []
        store.close()

    asyncio.run(exercise())
