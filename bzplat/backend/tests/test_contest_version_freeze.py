"""预赛/决赛 P1：轮次冻结 + dispatch 闸门测试。

验证：
1. pairing 生成时 published_at 非空 + bot_a/b_version_id 快照（版本冻结）
2. published dispatch 与未开始 pairing 的 Bot/版本快照原子换绑并重封
3. _run_match 读冻结的 version 路径（赛事对局用发布时版本，非最新）
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from bzplat.backend.store import Store
from bzplat.backend.tests.execution_helpers import (
    claim_next_queued,
    enable_execution_queue,
)


def _store(tmp_path):
    return Store(str(tmp_path / "p1.db"))


def _fixture_file(tmp_path: Path, name: str) -> str:
    path = tmp_path / name
    path.write_bytes(b"test fixture")
    return str(path)


def _published_swap_fixture(tmp_path: Path, label: str):
    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    store = Store(str(tmp_path / f"published-swap-{label}.db"))
    organizer = store.create_user(
        f"published-swap-org-{label}",
        f"published-swap-org-{label}@example.com",
        "hash",
        role="organizer",
    )
    users = [
        store.create_user(
            f"published-swap-user-{label}-{index}",
            f"published-swap-user-{label}-{index}@example.com",
            "hash",
        )
        for index in range(3)
    ]
    bots = [
        store.create_bot(
            user["id"],
            f"published-swap-bot-{label}-{index}",
            binary_path=_fixture_file(
                tmp_path, f"published-swap-bot-{label}-{index}"
            ),
            format="elf",
            game_id="holdem",
        )
        for index, user in enumerate(users)
    ]
    for index, bot in enumerate(bots):
        store.add_bot_version(
            bot["id"],
            binary_path=_fixture_file(
                tmp_path, f"published-swap-v1-{label}-{index}"
            ),
            version=1,
        )
    contest_id = store.create_contest(
        f"published swap {label}",
        organizer_id=organizer["id"],
        game_id="holdem",
        status="open",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest_id, user["id"], bot["id"])
    manager = ContestManager(store, MatchOrchestrator(store))
    asyncio.run(manager.publish(contest_id))
    replacement = store.create_bot(
        users[0]["id"],
        f"published-swap-replacement-{label}",
        binary_path=_fixture_file(
            tmp_path, f"published-swap-replacement-{label}"
        ),
        format="elf",
        game_id="holdem",
    )
    replacement_version = store.add_bot_version(
        replacement["id"],
        binary_path=_fixture_file(
            tmp_path, f"published-swap-replacement-v1-{label}"
        ),
        version=1,
    )
    return (
        store,
        manager,
        contest_id,
        organizer,
        users,
        bots,
        replacement,
        replacement_version,
    )


def _contest_lifecycle_revision(store: Store, contest_id: int) -> tuple[int, int]:
    with store._tx() as connection:
        row = connection.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest_id,),
        ).fetchone()
    assert row is not None
    return int(row[0]), int(row[1])


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
        s.create_bot(
            uid, f"p1bot{i}",
            binary_path=_fixture_file(tmp_path, f"p1bot-{i}"),
            format="elf", game_id="holdem",
        )["id"]
        for i, uid in enumerate(users)
    ]
    c = s.create_contest(
        "P1冻结", organizer_id=u, game_id="holdem",
        stages_json='[{"key":"s1","type":"round_robin","scoring":"poker_3_1_0"}]',
    )["id"]
    for uid, bid in zip(users, bots):
        s.add_contest_entry(c, uid, bid)
    s.update_contest(c, status="open")
    # 给每个 bot 写一个 version（_version_snapshot 取 latest）
    for bid in bots:
        s.add_bot_version(
            bid, binary_path=_fixture_file(tmp_path, f"v1-{bid}"), version=1
        )
    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
    cm = ContestManager(s, orch)
    s.update_contest(c, status="published", current_stage_idx=0)

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


def test_dispatch_atomically_rebinds_unstarted_published_pairing(tmp_path):
    """Published 换 Bot 同时换绑未开始 pairing 的当前可运行版本。"""
    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    s = _store(tmp_path)
    u = s.create_user("org2", "o2@e.com", "x", role="organizer")["id"]
    ua = s.create_user("dp1", "d1@e.com", "x")["id"]
    ub = s.create_user("dp2", "d2@e.com", "x")["id"]
    ba = s.create_bot(
        ua, "dpbotA", binary_path=_fixture_file(tmp_path, "dp-a"),
        format="elf", game_id="holdem",
    )["id"]
    bb = s.create_bot(
        ub, "dpbotB", binary_path=_fixture_file(tmp_path, "dp-b"),
        format="elf", game_id="holdem",
    )["id"]
    s.add_bot_version(ba, binary_path=_fixture_file(tmp_path, "dp-v-a"), version=1)
    s.add_bot_version(bb, binary_path=_fixture_file(tmp_path, "dp-v-b"), version=1)
    c = s.create_contest(
        "P1dispatch", organizer_id=u, game_id="holdem",
        status="open",
        stages_json='[{"key":"s1","type":"round_robin","scoring":"poker_3_1_0"}]',
    )["id"]
    s.add_contest_entry(c, ua, ba)
    s.add_contest_entry(c, ub, bb)
    cm = ContestManager(s, MatchOrchestrator(s))
    asyncio.run(cm.publish(c))
    ps_before = s.list_contest_pairings(c, stage_idx=0)
    e1 = s.get_entry(c, ua)
    ba2 = s.create_bot(
        ua, "dpbotA2", binary_path=_fixture_file(tmp_path, "dp-a2"),
        format="elf", game_id="holdem",
    )["id"]
    replacement_version = s.add_bot_version(
        ba2,
        binary_path=_fixture_file(tmp_path, "dp-v-a2"),
        version=1,
    )["id"]
    with s._tx() as connection:
        before_revision = connection.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (c,),
        ).fetchone()
    assert before_revision[0] == before_revision[1]

    asyncio.run(cm.dispatch(c, ua, ba2, role="organizer"))
    ps_after = {
        pairing["id"]: pairing
        for pairing in s.list_contest_pairings(c, stage_idx=0)
    }
    for before in ps_before:
        after = ps_after[before["id"]]
        assert after["published_at"] == before["published_at"]
        assert after["pairing_seed"] == before["pairing_seed"]
        if before["entry_a_id"] == e1["id"]:
            assert after["bot_a_id"] == ba2
            assert after["bot_a_version_id"] == replacement_version
            assert after["bot_b_id"] == before["bot_b_id"]
        else:
            assert before["entry_b_id"] == e1["id"]
            assert after["bot_b_id"] == ba2
            assert after["bot_b_version_id"] == replacement_version
            assert after["bot_a_id"] == before["bot_a_id"]
    e1b = s.get_entry(c, ua)
    assert e1b["bot_id"] == ba2, "dispatch 应更新 entry.bot_id"
    with s._tx() as connection:
        after_revision = connection.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (c,),
        ).fetchone()
    assert after_revision[0] == after_revision[1]
    assert after_revision[0] > before_revision[0]
    s.close()


def test_dispatch_rejects_published_pairing_with_existing_progress(tmp_path):
    """A sealed published row with any Match progress cannot be rebound."""
    (
        store,
        manager,
        contest_id,
        organizer,
        users,
        _bots,
        replacement,
        _replacement_version,
    ) = _published_swap_fixture(tmp_path, "progress")
    swapped_entry = store.get_entry(contest_id, users[0]["id"])
    assert swapped_entry is not None
    pairing = next(
        row
        for row in store.list_contest_pairings(contest_id, stage_idx=0)
        if swapped_entry["id"] not in (row["entry_a_id"], row["entry_b_id"])
    )
    match_id = "published-swap-existing-progress"
    store.create_match(
        match_id,
        pairing["bot_a_id"],
        pairing["bot_b_id"],
        owner_id=organizer["id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_pairings SET match_id=?,status='running' WHERE id=?",
            (match_id, pairing["id"]),
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest_id,),
        )
    before_entries = store.list_contest_entries(contest_id)
    before_pairings = store.list_contest_pairings(contest_id, stage_idx=0)
    before_revision = _contest_lifecycle_revision(store, contest_id)

    with pytest.raises(ValueError, match="已开始"):
        asyncio.run(
            manager.dispatch(
                contest_id, users[0]["id"], replacement["id"]
            )
        )

    assert store.list_contest_entries(contest_id) == before_entries
    assert store.list_contest_pairings(contest_id, stage_idx=0) == before_pairings
    assert _contest_lifecycle_revision(store, contest_id) == before_revision
    store.close()


def test_published_swap_reseal_failure_rolls_back_pairings_and_roster(tmp_path):
    """Published pairing and roster mutations roll back with a reseal failure."""
    (
        store,
        manager,
        contest_id,
        _organizer,
        users,
        _bots,
        replacement,
        _replacement_version,
    ) = _published_swap_fixture(tmp_path, "reseal-rollback")
    before_entries = store.list_contest_entries(contest_id)
    before_pairings = store.list_contest_pairings(contest_id, stage_idx=0)
    before_revision = _contest_lifecycle_revision(store, contest_id)
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_published_bot_swap_reseal "
            "BEFORE UPDATE OF sealed_pairing_topology_revision ON contests "
            "WHEN NEW.id=OLD.id AND "
            "NEW.sealed_pairing_topology_revision "
            "IS NOT OLD.sealed_pairing_topology_revision "
            "BEGIN SELECT RAISE(ABORT, 'injected published reseal failure'); END"
        )

    with pytest.raises(
        sqlite3.DatabaseError, match="injected published reseal failure"
    ):
        asyncio.run(
            manager.dispatch(
                contest_id, users[0]["id"], replacement["id"]
            )
        )

    assert store.list_contest_entries(contest_id) == before_entries
    assert store.list_contest_pairings(contest_id, stage_idx=0) == before_pairings
    assert _contest_lifecycle_revision(store, contest_id) == before_revision
    store.close()


@pytest.mark.parametrize("drift", ["owner", "game", "version", "status", "cursor"])
def test_dispatch_rejects_published_swap_input_drift(
    tmp_path, monkeypatch, drift
):
    """Store CAS rejects every Bot/contest coordinate read before the swap."""
    (
        store,
        manager,
        contest_id,
        organizer,
        users,
        _bots,
        replacement,
        _replacement_version,
    ) = _published_swap_fixture(tmp_path, f"drift-{drift}")
    original_swap = store.swap_contest_entry_bot_and_reseal

    def drift_then_swap(*args, **kwargs):
        if drift == "owner":
            with store._tx() as connection:
                connection.execute(
                    "UPDATE bots SET owner_id=? WHERE id=?",
                    (organizer["id"], replacement["id"]),
                )
        elif drift == "game":
            with store._tx() as connection:
                connection.execute(
                    "UPDATE contests SET game_id='gomoku' WHERE id=?",
                    (contest_id,),
                )
        elif drift == "version":
            store.add_bot_version(
                replacement["id"],
                binary_path=_fixture_file(
                    tmp_path, f"published-swap-drift-version-v2"
                ),
                version=2,
            )
        elif drift == "status":
            with store._tx() as connection:
                connection.execute(
                    "UPDATE contests SET status='running' WHERE id=?",
                    (contest_id,),
                )
        else:
            with store._tx() as connection:
                connection.execute(
                    "UPDATE contests SET current_stage_idx=1 WHERE id=?",
                    (contest_id,),
                )
        return original_swap(*args, **kwargs)

    monkeypatch.setattr(
        store, "swap_contest_entry_bot_and_reseal", drift_then_swap
    )
    before_entries = store.list_contest_entries(contest_id)
    before_pairings = store.list_contest_pairings(contest_id, stage_idx=0)
    with pytest.raises(ValueError, match="已变化|无法验证|不属于"):
        asyncio.run(
            manager.dispatch(
                contest_id, users[0]["id"], replacement["id"]
            )
        )
    assert store.list_contest_entries(contest_id) == before_entries
    assert store.list_contest_pairings(contest_id, stage_idx=0) == before_pairings
    store.close()


@pytest.mark.parametrize("status", ["published", "running", "rest"])
def test_generic_update_entry_rejects_active_bot_swap(tmp_path, status):
    """Active contests cannot bypass the dedicated swap/reseal transaction."""
    store = Store(str(tmp_path / f"generic-active-swap-{status}.db"))
    user = store.create_user(
        f"generic-active-swap-{status}",
        f"generic-active-swap-{status}@example.com",
        "hash",
    )
    old_bot = store.create_bot(
        user["id"],
        f"generic-active-old-{status}",
        binary_path=_fixture_file(tmp_path, f"generic-active-old-{status}"),
        format="elf",
        game_id="holdem",
    )
    new_bot = store.create_bot(
        user["id"],
        f"generic-active-new-{status}",
        binary_path=_fixture_file(tmp_path, f"generic-active-new-{status}"),
        format="elf",
        game_id="holdem",
    )
    contest_id = store.create_contest(
        f"generic active swap {status}",
        organizer_id=user["id"],
        game_id="holdem",
        status="draft",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )["id"]
    store.add_contest_entry(contest_id, user["id"], old_bot["id"])
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET status=? WHERE id=?", (status, contest_id)
        )

    with pytest.raises(ValueError, match="原子重封入口"):
        store.update_entry(contest_id, user["id"], bot_id=new_bot["id"])
    assert store.get_entry(contest_id, user["id"])["bot_id"] == old_bot["id"]
    store.close()


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


def test_publish_freezes_current_version_after_rollback(tmp_path):
    """v1→v2→激活 v1 后发布，pairing 必须冻结 v1 而非历史最大 v2。"""
    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    s = _store(tmp_path)
    organizer = s.create_user("rollback-org", "rollback-org@e.com", "x")["id"]
    users = [
        s.create_user(f"rollback-u{i}", f"rollback-u{i}@e.com", "x")["id"]
        for i in range(2)
    ]
    bots = [
        s.create_bot(
            uid, f"rollback-bot-{i}",
            binary_path=_fixture_file(tmp_path, f"rollback-base-{i}"),
            format="elf", game_id="holdem",
        )["id"]
        for i, uid in enumerate(users)
    ]
    current_ids: dict[int, int] = {}
    latest_ids: dict[int, int] = {}
    for i, bot_id in enumerate(bots):
        v1 = s.add_bot_version(
            bot_id, binary_path=_fixture_file(tmp_path, f"rollback-v1-{i}"),
            version=1,
        )
        v2 = s.add_bot_version(
            bot_id, binary_path=_fixture_file(tmp_path, f"rollback-v2-{i}"),
            version=2,
        )
        s.set_current_version(bot_id, 1)
        current_ids[bot_id] = v1["id"]
        latest_ids[bot_id] = v2["id"]
        assert s.get_current_bot_version(bot_id)["id"] == v1["id"]
        assert s.get_latest_bot_version(bot_id)["id"] == v2["id"]

    contest_id = s.create_contest(
        "Rollback freeze", organizer, status="open", game_id="holdem",
        template_id="holdem_rr",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )["id"]
    for user_id, bot_id in zip(users, bots):
        s.add_contest_entry(contest_id, user_id, bot_id)

    manager = ContestManager(s, MatchOrchestrator(s))
    asyncio.run(manager.publish(contest_id))
    pairing = s.list_contest_pairings(contest_id)[0]
    assert pairing["bot_a_version_id"] == current_ids[pairing["bot_a_id"]]
    assert pairing["bot_b_version_id"] == current_ids[pairing["bot_b_id"]]
    assert pairing["bot_a_version_id"] != latest_ids[pairing["bot_a_id"]]
    assert pairing["bot_b_version_id"] != latest_ids[pairing["bot_b_id"]]
    s.close()


def test_dispatch_persists_and_runs_frozen_versions_after_current_changes(tmp_path):
    """发布后切到 v2，match_config 与 runner 实际路径仍必须使用 pairing 冻结的 v1。"""
    async def exercise():
        from types import SimpleNamespace

        from bzplat.backend.contests.manager import ContestManager
        from bzplat.backend.matches.orchestrator import MatchOrchestrator

        class CapturingRunner:
            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            async def run_binaries(self, path_a, path_b, **kwargs):
                self.calls.append((path_a, path_b))
                return SimpleNamespace(
                    rounds_played=1,
                    rounds=[SimpleNamespace(deltas=[0, 0])],
                    winner=None,
                )

        s = _store(tmp_path)
        organizer = s.create_user("runner-org", "runner-org@e.com", "x")["id"]
        users = [
            s.create_user(f"runner-u{i}", f"runner-u{i}@e.com", "x")["id"]
            for i in range(2)
        ]
        bots = [
            s.create_bot(
                uid, f"runner-bot-{i}",
                binary_path=_fixture_file(tmp_path, f"runner-base-{i}"),
                format="elf", game_id="holdem",
            )["id"]
            for i, uid in enumerate(users)
        ]
        frozen: dict[int, dict] = {}
        latest: dict[int, dict] = {}
        for i, bot_id in enumerate(bots):
            frozen[bot_id] = s.add_bot_version(
                bot_id,
                binary_path=_fixture_file(tmp_path, f"runner-v1-{i}"),
                version=1,
            )
            latest[bot_id] = s.add_bot_version(
                bot_id,
                binary_path=_fixture_file(tmp_path, f"runner-v2-{i}"),
                version=2,
            )
            s.set_current_version(bot_id, 1)

        contest_id = s.create_contest(
            "Runner freeze", organizer, status="open", game_id="holdem",
            template_id="holdem_rr",
            stages_json='[{"key":"rr","type":"round_robin"}]',
        )["id"]
        for user_id, bot_id in zip(users, bots):
            s.add_contest_entry(contest_id, user_id, bot_id)

        runner = CapturingRunner()
        orch = MatchOrchestrator(s, runner=runner, max_concurrent=1)
        manager = ContestManager(s, orch)
        enable_execution_queue(s)
        await manager.publish(contest_id)
        pairing = s.list_contest_pairings(contest_id)[0]
        for bot_id in bots:
            s.set_current_version(bot_id, 2)

        await manager.start(contest_id)
        claim_next_queued(orch)
        match_id = s.list_contest_pairings(contest_id)[0]["match_id"]
        for _ in range(100):
            if s.get_match(match_id)["status"] in ("completed", "aborted"):
                break
            await asyncio.sleep(0.01)
        match = s.get_match(match_id)

        assert match["status"] == "completed"
        assert match["match_config"]["_bot_a_version_id"] == pairing["bot_a_version_id"]
        assert match["match_config"]["_bot_b_version_id"] == pairing["bot_b_version_id"]
        assert runner.calls == [
            (
                frozen[match["bot_a_id"]]["binary_path"],
                frozen[match["bot_b_id"]]["binary_path"],
            )
        ]
        assert runner.calls[0] != (
            latest[match["bot_a_id"]]["binary_path"],
            latest[match["bot_b_id"]]["binary_path"],
        )
        s.close()

    asyncio.run(exercise())
