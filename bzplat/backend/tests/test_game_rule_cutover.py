"""Same-wire game-rule cutover keeps Bot assets while isolating ratings."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import stat
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bzplat.backend.cli import app as cli_app
from bzplat.backend.store import Store, rating_projection_digests
from bzplat.backend.store.schema import (
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    EXECUTION_SOURCE_MANUAL,
    GOMOKU_CURRENT_PROTOCOL,
    GOMOKU_CURRENT_RATING_POOL,
    GOMOKU_CURRENT_RULESET,
    GOMOKU_PREVIOUS_RATING_POOL,
    GOMOKU_PREVIOUS_RULESET,
    HOLDEM_CURRENT_RATING_POOL,
    HOLDEM_CURRENT_RULESET,
    HOLDEM_PREVIOUS_RATING_POOL,
    HOLDEM_PREVIOUS_RULESET,
    HOLDEM_PROTOCOL,
    STATUS_COMPLETED,
    TYPE_CHALLENGE,
    game_rule_contract,
)


PREVIOUS_CONTRACT = {
    "ruleset_version": GOMOKU_PREVIOUS_RULESET,
    "protocol_version": GOMOKU_CURRENT_PROTOCOL,
    "rating_pool_id": GOMOKU_PREVIOUS_RATING_POOL,
}


def test_holdem_allin_v2_uses_same_wire_and_requires_new_rating_pool(tmp_path):
    """下注修复改变裁判与结算，必须同协议换 ruleset/rating pool，旧库拒绝在线启动。"""
    source = game_rule_contract("holdem", legacy=True)
    target = game_rule_contract("holdem")
    assert source == {
        "ruleset_version": HOLDEM_PREVIOUS_RULESET,
        "protocol_version": HOLDEM_PROTOCOL,
        "rating_pool_id": HOLDEM_PREVIOUS_RATING_POOL,
    }
    assert target == {
        "ruleset_version": HOLDEM_CURRENT_RULESET,
        "protocol_version": HOLDEM_PROTOCOL,
        "rating_pool_id": HOLDEM_CURRENT_RATING_POOL,
    }

    store = Store(str(tmp_path / "holdem-v1-needs-cutover.db"))
    with store._tx() as conn:
        conn.execute(
            "UPDATE rating_pool_state SET active_pool_id=?,ruleset_version=?,"
            "protocol_version=?,activated_at='holdem-v1-test' WHERE game_id='holdem'",
            (
                HOLDEM_PREVIOUS_RATING_POOL,
                HOLDEM_PREVIOUS_RULESET,
                HOLDEM_PROTOCOL,
            ),
        )
    with pytest.raises(RuntimeError, match="game-rule-cutover"):
        store.assert_runtime_contracts_current()

    _certify_projection(store)
    plan = store.plan_game_rule_cutover(
        cutover_id="holdem-allin-v2-test",
        game_id="holdem",
        from_contract=source,
        to_contract=target,
    )
    assert plan["version_manifest"] == []
    assert plan["manifest_digest"] == hashlib.sha256(b"[]").hexdigest()
    _prepare_cold_cutover(store)
    with store.offline_cutover_guard() as guard:
        applied = store.apply_game_rule_cutover(
            cutover_id=plan["cutover_id"],
            game_id="holdem",
            from_contract=source,
            to_contract=target,
            expected_plan_digest=plan["plan_digest"],
            offline_guard=guard,
        )
    assert applied["already_applied"] is False
    assert store.get_active_game_contract("holdem") == target
    store.assert_runtime_contracts_current()
    store.close()


def _set_previous_contract(store: Store) -> None:
    with store._tx() as conn:
        conn.execute(
            "UPDATE rating_pool_state SET active_pool_id=?,ruleset_version=?,"
            "protocol_version=?,activated_at='previous-test' WHERE game_id='gomoku'",
            (
                GOMOKU_PREVIOUS_RATING_POOL,
                GOMOKU_PREVIOUS_RULESET,
                GOMOKU_CURRENT_PROTOCOL,
            ),
        )


def _certify_projection(store: Store) -> None:
    with store._tx() as conn:
        live = rating_projection_digests(conn)
        assert live["issues"] == []
        conn.execute(
            "UPDATE rating_projection_state SET policy_version='owner-ranked-bot-v4',"
            "rebuilt_at='test',source_settlement_count=?,source_last_settled_order=?,"
            "source_digest=?,projection_digest=?,plan_digest=?,"
            "trusted_mutation_revision=mutation_revision WHERE singleton=1",
            (
                live["source_settlement_count"],
                live["source_last_settled_order"],
                live["source_digest"],
                live["projection_digest"],
                live["plan_digest"],
            ),
        )


def _canonical_bot(
    store: Store,
    tmp_path: Path,
    key: str,
    *,
    uploader_permissions: bool = False,
    bot_dir_mode: int | None = None,
) -> tuple[dict, dict]:
    owner = store.create_user(key, f"{key}@example.test", "hash")
    bot = store.create_bot(owner["id"], key, game_id="gomoku")
    root = tmp_path / "bot_uploads"
    root.mkdir(mode=0o755, exist_ok=True)
    bot_dir = root / str(bot["id"])
    bot_dir.mkdir(
        mode=(
            bot_dir_mode
            if bot_dir_mode is not None
            else (0o700 if uploader_permissions else 0o755)
        )
    )
    if bot_dir_mode is not None:
        bot_dir.chmod(bot_dir_mode)
    version_dir = bot_dir / "v1"
    version_dir.mkdir(mode=0o700 if uploader_permissions else 0o755)
    binary = version_dir / "bot.bin"
    payload = Path("/bin/true").read_bytes()
    binary.write_bytes(payload)
    binary.chmod(0o755 if uploader_permissions else 0o555)
    if not uploader_permissions:
        version_dir.chmod(0o555)
    version = store.add_bot_version(
        bot["id"],
        binary_path=str(binary),
        checksum=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        upload_note=f"{key} current",
    )
    return owner, version


@pytest.mark.parametrize("bot_dir_mode", [0o700, 0o755])
def test_rule_cutover_accepts_live_uploader_asset_permissions(
    tmp_path, bot_dir_mode
):
    store = Store(str(tmp_path / f"uploader-permissions-{bot_dir_mode:o}.db"))
    _set_previous_contract(store)
    _, version = _canonical_bot(
        store,
        tmp_path,
        f"uploader_permissions_{bot_dir_mode:o}",
        uploader_permissions=True,
        bot_dir_mode=bot_dir_mode,
    )
    binary = Path(version["binary_path"])
    assert stat.S_IMODE(binary.parent.parent.stat().st_mode) == bot_dir_mode
    assert stat.S_IMODE(binary.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(binary.stat().st_mode) == 0o755
    _certify_projection(store)

    plan = _plan(store, "uploader-permissions-cutover")
    _prepare_cold_cutover(store)
    result = _apply(store, plan)

    assert result["already_applied"] is False
    assert store.get_active_game_contract("gomoku") == game_rule_contract("gomoku")
    store.assert_runtime_contracts_current()
    store.close()


@pytest.mark.parametrize(
    ("target", "mode"),
    [
        ("bot_dir", 0o720),
        ("version_dir", 0o720),
        ("binary", 0o775),
        ("version_dir", 0o755),
        ("version_dir", 0o555),
        ("binary", 0o555),
    ],
)
def test_rule_cutover_rejects_unsafe_or_noncanonical_uploader_permissions(
    tmp_path, target, mode
):
    store = Store(str(tmp_path / f"unsafe-permissions-{target}-{mode:o}.db"))
    _set_previous_contract(store)
    _, version = _canonical_bot(
        store,
        tmp_path,
        f"unsafe_{target}_{mode:o}",
        uploader_permissions=True,
    )
    binary = Path(version["binary_path"])
    targets = {
        "bot_dir": binary.parent.parent,
        "version_dir": binary.parent,
        "binary": binary,
    }
    targets[target].chmod(mode)
    _certify_projection(store)

    with pytest.raises(ValueError, match="current asset 缺失或不安全"):
        _plan(store, f"unsafe-{target}-{mode:o}-cutover")
    assert store.get_active_game_contract("gomoku") == PREVIOUS_CONTRACT
    assert store._conn.execute(
        "SELECT COUNT(*) FROM protocol_cutovers"
    ).fetchone()[0] == 0
    store.close()


def _prepare_cold_cutover(store: Store) -> None:
    control = store.executions.control()
    if control["dispatcher_state"] != "running":
        store.executions.resume()
    store.executions.begin_maintenance("same-wire game rule cutover")
    store.executions.set_control(dispatcher_state="stopped", accepting=False)


def _plan(
    store: Store,
    cutover_id: str,
    *,
    migrate_unstarted_contest_ids: tuple[int, ...] = (),
) -> dict:
    return store.plan_game_rule_cutover(
        cutover_id=cutover_id,
        game_id="gomoku",
        from_contract=PREVIOUS_CONTRACT,
        to_contract=game_rule_contract("gomoku"),
        migrate_unstarted_contest_ids=migrate_unstarted_contest_ids,
    )


def _apply(
    store: Store,
    plan: dict,
    *,
    migrate_unstarted_contest_ids: tuple[int, ...] = (),
) -> dict:
    with store.offline_cutover_guard() as guard:
        return store.apply_game_rule_cutover(
            cutover_id=plan["cutover_id"],
            game_id="gomoku",
            from_contract=PREVIOUS_CONTRACT,
            to_contract=game_rule_contract("gomoku"),
            expected_plan_digest=plan["plan_digest"],
            offline_guard=guard,
            migrate_unstarted_contest_ids=migrate_unstarted_contest_ids,
        )


def _open_unstarted_contest(
    store: Store,
    *,
    organizer_id: int,
    title: str,
) -> dict:
    return store.create_contest(
        title,
        organizer_id,
        status=CONTEST_OPEN,
        game_id="gomoku",
        template_id="gomoku_test",
        stages_json=json.dumps(
            [{"key": "swiss", "type": "swiss", "rounds": 1}],
            ensure_ascii=False,
        ),
    )


def test_rule_cutover_is_atomic_idempotent_and_keeps_current_bot_assets(tmp_path):
    database = tmp_path / "rule-cutover.db"
    store = Store(str(database))
    _set_previous_contract(store)
    owner_a, version_a = _canonical_bot(store, tmp_path, "rule_a")
    owner_b, version_b = _canonical_bot(store, tmp_path, "rule_b")
    delete_bot_owner, delete_bot_version = _canonical_bot(
        store, tmp_path, "rule_delete_bot"
    )
    delete_user, delete_user_version = _canonical_bot(
        store, tmp_path, "rule_delete_user"
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE bots SET is_ranked=1 WHERE id IN (?,?)",
            (version_a["bot_id"], version_b["bot_id"]),
        )

    historical = store.create_match(
        "previous-n3-match",
        version_a["bot_id"],
        version_b["bot_id"],
        game_id="gomoku",
    )
    store.update_match(
        historical["id"],
        status=STATUS_COMPLETED,
        winner=0,
        reason="five",
        result={"rounds_played": 5, "deltas": [1, -1], "normalized_delta": 1},
    )
    store.upsert_replay(
        historical["id"],
        json.dumps(
            [
                {"type": "opening", "n": 3},
                {"type": "match_end", "winner": 0, "reason": "five"},
            ]
        ),
    )
    assert store.apply_match_ratings_atomic(
        version_a["bot_id"],
        version_b["bot_id"],
        game_id="gomoku",
        rating_a=(1510.0, 340.0, 0.06),
        rating_b=(1490.0, 340.0, 0.06),
        winner=0,
        delta_a=1,
        delta_b=-1,
        reason=historical["id"],
        settlement_id=historical["id"],
    )
    _certify_projection(store)

    store.executions.resume()
    queued = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner_a["id"],
        game_id="gomoku",
        match_type=TYPE_CHALLENGE,
        bot_a_id=version_a["bot_id"],
        bot_b_id=version_b["bot_id"],
        bot_a_version_id=version_a["id"],
        bot_b_version_id=version_b["id"],
    )
    interrupted = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner_b["id"],
        game_id="gomoku",
        match_type=TYPE_CHALLENGE,
        bot_a_id=version_b["bot_id"],
        bot_b_id=version_a["bot_id"],
        bot_a_version_id=version_b["id"],
        bot_b_version_id=version_a["id"],
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status='interrupted',retryable=1,"
            "terminal_reason='runtime_failure',terminal_at='test' WHERE id=?",
            (interrupted["id"],),
        )
        owner_lo, owner_hi = sorted((owner_a["id"], owner_b["id"]))
        bot_lo, bot_hi = sorted((version_a["bot_id"], version_b["bot_id"]))
        conn.execute(
            "INSERT INTO auto_match_owner_service(owner_id,game_id,served_count) "
            "VALUES(?, 'gomoku', 7)",
            (owner_a["id"],),
        )
        conn.execute(
            "INSERT INTO auto_match_owner_service(owner_id,game_id,served_count) "
            "VALUES(?, 'pencil', 11)",
            (owner_a["id"],),
        )
        conn.execute(
            "INSERT INTO auto_match_bot_service(bot_id,game_id,served_count) "
            "VALUES(?, 'gomoku', 8)",
            (version_a["bot_id"],),
        )
        conn.execute(
            "INSERT INTO auto_match_bot_pair_service(game_id,bot_lo_id,bot_hi_id,"
            "served_count) VALUES('gomoku',?,?,9)",
            (bot_lo, bot_hi),
        )
        conn.execute(
            "INSERT INTO auto_match_owner_pair_service(game_id,owner_lo_id,owner_hi_id,"
            "served_count) VALUES('gomoku',?,?,10)",
            (owner_lo, owner_hi),
        )
        conn.execute(
            "UPDATE auto_match_fair_state SET next_game_idx=2,next_lane=1,revision=37,"
            "bootstrap_version=5,updated_at='fair-before' WHERE singleton=1"
        )
        conn.execute(
            "INSERT INTO auto_match_decisions("
            "policy_version,state_revision,cursor_game_idx,requested_lane,actual_lane,"
            "game_id,bot_a_id,bot_b_id,owner_a_id,owner_b_id,bot_a_version_id,"
            "bot_b_version_id,owner_a_service_before,owner_b_service_before,"
            "bot_a_service_before,bot_b_service_before,bot_pair_count_before,"
            "owner_pair_count_before,rating_gap,bot_a_seat_debt_before,"
            "bot_b_seat_debt_before,selection_reason,created_at) "
            "VALUES('test-policy',37,2,'bootstrap','bootstrap','gomoku',?,?,?,?,?,?,"
            "0,0,0,0,0,0,0.0,0,0,'test','test')",
            (
                version_a["bot_id"],
                version_b["bot_id"],
                owner_a["id"],
                owner_b["id"],
                version_a["id"],
                version_b["id"],
            ),
        )

    agent = store.create_local_ai_agent(
        owner_id=owner_a["id"],
        bot_id=version_a["bot_id"],
        label="rule local",
        public_id="lia_rule_cutover",
        token_hash="rule-token-hash",
        token_hint="hint",
    )
    versions_before = {
        version["id"]: dict(version)
        for version in (
            version_a,
            version_b,
            delete_bot_version,
            delete_user_version,
        )
    }
    bots_before = {
        bot_id: store.get_bot(bot_id)
        for bot_id in (version_a["bot_id"], version_b["bot_id"])
    }
    plan = _plan(store, "gomoku-five-move-two-rule-v2-test")
    assert plan["version_manifest"] == []
    assert plan["bot_count"] == 4
    assert plan["current_version_count"] == 4
    _prepare_cold_cutover(store)
    applied = _apply(store, plan)

    assert applied["already_applied"] is False
    assert applied["bot_count"] == applied["retired_count"] == 0
    assert applied["cancelled_jobs"] == 2
    assert store.get_active_game_contract("gomoku") == game_rule_contract("gomoku")
    assert store.get_match(historical["id"])["ruleset_version"] == (
        GOMOKU_PREVIOUS_RULESET
    )
    assert json.loads(store.get_replay(historical["id"])["events_json"])[0]["n"] == 3

    for version_id, before in versions_before.items():
        after = store.get_bot_version(version_id)
        assert after == before
        assert Path(after["binary_path"]).is_file()
    for bot_id, before in bots_before.items():
        after = store.get_bot(bot_id)
        assert after["current_version"] == before["current_version"]
        assert after["binary_path"] == before["binary_path"]
        assert after["is_active"] == before["is_active"]
        assert after["is_ranked"] == before["is_ranked"] == 1
    agent_after = store._conn.execute(
        "SELECT * FROM local_ai_agents WHERE id=?", (agent["id"],)
    ).fetchone()
    assert agent_after["status"] == agent["status"] == "active"
    assert agent_after["connection_generation"] == agent["connection_generation"]

    for table in (
        "auto_match_owner_service",
        "auto_match_bot_service",
        "auto_match_bot_pair_service",
        "auto_match_owner_pair_service",
    ):
        assert store._conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE game_id='gomoku'"
        ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT served_count FROM auto_match_owner_service "
        "WHERE owner_id=? AND game_id='pencil'",
        (owner_a["id"],),
    ).fetchone()[0] == 11
    auto_decision = store._conn.execute(
        "SELECT lifecycle,terminal_reason FROM auto_match_decisions "
        "WHERE game_id='gomoku'"
    ).fetchone()
    assert tuple(auto_decision) == ("cancelled", "ruleset_retired")
    fair_state = dict(
        store._conn.execute(
            "SELECT * FROM auto_match_fair_state WHERE singleton=1"
        ).fetchone()
    )
    assert (
        fair_state["next_game_idx"],
        fair_state["next_lane"],
        fair_state["revision"],
        fair_state["bootstrap_version"],
    ) == (2, 1, 37, 5)

    archive = store._conn.execute(
        "SELECT * FROM rating_pool_archives WHERE game_id='gomoku' AND pool_id=?",
        (GOMOKU_PREVIOUS_RATING_POOL,),
    ).fetchone()
    assert archive is not None
    assert (
        archive["ratings_count"],
        archive["history_count"],
        archive["pair_count"],
    ) == (4, 2, 1)
    archived_ratings = store._conn.execute(
        "SELECT bot_id,rating,matches_played FROM ratings_archive "
        "WHERE game_id='gomoku' AND pool_id=? ORDER BY bot_id",
        (GOMOKU_PREVIOUS_RATING_POOL,),
    ).fetchall()
    assert len(archived_ratings) == 4
    by_bot = {row["bot_id"]: row for row in archived_ratings}
    assert (by_bot[version_a["bot_id"]]["rating"], by_bot[version_a["bot_id"]]["matches_played"]) == (1510.0, 1)
    assert (by_bot[version_b["bot_id"]]["rating"], by_bot[version_b["bot_id"]]["matches_played"]) == (1490.0, 1)
    ratings = store._conn.execute(
        "SELECT * FROM ratings WHERE game_id='gomoku' ORDER BY bot_id"
    ).fetchall()
    assert len(ratings) == 4
    assert all(row["rating"] == 1500.0 and row["matches_played"] == 0 for row in ratings)
    projection = store.rating_projection_status()
    assert projection["ready"] is True
    assert projection["source_settlement_count"] == 0
    assert store.executions.get(queued["public_id"])["status"] == "cancelled"
    assert store.executions.get(interrupted["public_id"])["retryable"] == 0
    marker = store.get_protocol_cutover(plan["cutover_id"])
    assert marker["version_manifest"] == []
    assert marker["manifest_digest"] == hashlib.sha256(b"[]").hexdigest()

    repeated = _apply(store, plan)
    assert repeated["already_applied"] is True
    assert store._conn.execute(
        "SELECT COUNT(*) FROM bot_versions WHERE bot_id IN (?,?,?,?)",
        tuple(before["bot_id"] for before in versions_before.values()),
    ).fetchone()[0] == 4
    store.assert_runtime_contracts_current()
    store.close()

    reopened = Store(str(database))
    reopened.assert_runtime_contracts_current()
    purged = reopened.delete_user_if_safe(delete_user["id"])
    assert purged["deleted"] is True
    assert purged["blockers"]["audit_versions"] == 0
    assert reopened.delete_bot(delete_bot_version["bot_id"]) is True
    _certify_projection(reopened)
    reopened.assert_protocol_cutover_postconditions(plan["cutover_id"])
    reopened.close()


def test_rule_cutover_atomically_migrates_only_explicit_open_unstarted_contests(
    tmp_path,
):
    store = Store(str(tmp_path / "rule-cutover-open-contests.db"))
    _set_previous_contract(store)
    owner, version = _canonical_bot(store, tmp_path, "contest_migration")
    first = _open_unstarted_contest(
        store, organizer_id=owner["id"], title="migrate first"
    )
    second = _open_unstarted_contest(
        store, organizer_id=owner["id"], title="migrate second"
    )
    entry = store.add_contest_entry_once(
        second["id"], owner["id"], version["bot_id"]
    )
    historical = store.create_contest(
        "historical finished",
        owner["id"],
        status="finished",
        game_id="gomoku",
        stages_json='[{"key":"done","type":"swiss","rounds":1}]',
    )
    before_contests = {
        contest_id: store.get_contest(contest_id)
        for contest_id in (first["id"], second["id"], historical["id"])
    }
    before_entry = store.get_contest_entry(second["id"], owner["id"])
    _certify_projection(store)

    ids = (first["id"], second["id"])
    plan = _plan(
        store,
        "open-contests-rule-cutover",
        migrate_unstarted_contest_ids=ids,
    )
    assert [
        item["contest_id"] for item in plan["contest_contract_migrations"]
    ] == list(ids)
    assert [
        item["entry_count"] for item in plan["contest_contract_migrations"]
    ] == [0, 1]
    _prepare_cold_cutover(store)
    applied = _apply(
        store,
        plan,
        migrate_unstarted_contest_ids=ids,
    )
    assert applied["already_applied"] is False

    target = game_rule_contract("gomoku")
    contract_keys = ("ruleset_version", "protocol_version", "rating_pool_id")
    for contest_id in ids:
        after = store.get_contest(contest_id)
        assert {key: after[key] for key in contract_keys} == target
        before = before_contests[contest_id]
        assert {
            key: value for key, value in after.items() if key not in contract_keys
        } == {
            key: value for key, value in before.items() if key not in contract_keys
        }
    assert store.get_contest_entry(second["id"], owner["id"]) == before_entry == entry
    assert store.get_contest(historical["id"]) == before_contests[historical["id"]]
    store.assert_protocol_cutover_postconditions(plan["cutover_id"])
    repeated = _apply(
        store,
        plan,
        migrate_unstarted_contest_ids=ids,
    )
    assert repeated["already_applied"] is True
    with store._tx() as conn:
        conn.execute(
            "UPDATE contests SET ruleset_version=? WHERE id=?",
            (GOMOKU_PREVIOUS_RULESET, first["id"]),
        )
    with pytest.raises(RuntimeError, match="未终结赛事 contract"):
        store.assert_protocol_cutover_postconditions(plan["cutover_id"])
    store.close()


def test_migrated_contest_publish_and_start_freeze_target_contract(tmp_path):
    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    store = Store(str(tmp_path / "migrated-contest-publish-start.db"))
    _set_previous_contract(store)
    owner_a, version_a = _canonical_bot(store, tmp_path, "migrated_publish_a")
    owner_b, version_b = _canonical_bot(store, tmp_path, "migrated_publish_b")
    contest = _open_unstarted_contest(
        store, organizer_id=owner_a["id"], title="publish after migration"
    )
    store.add_contest_entry_once(
        contest["id"], owner_a["id"], version_a["bot_id"]
    )
    store.add_contest_entry_once(
        contest["id"], owner_b["id"], version_b["bot_id"]
    )
    _certify_projection(store)
    ids = (contest["id"],)
    plan = _plan(
        store,
        "migrated-contest-publish-start-cutover",
        migrate_unstarted_contest_ids=ids,
    )
    _prepare_cold_cutover(store)
    _apply(store, plan, migrate_unstarted_contest_ids=ids)
    store.executions.resume()
    store.executions.end_maintenance()

    manager = ContestManager(store, MatchOrchestrator(store))
    asyncio.run(manager.publish(contest["id"]))
    pairing = store.list_contest_pairings(contest["id"])[0]
    frozen_by_bot = {
        int(pairing["bot_a_id"]): int(pairing["bot_a_version_id"]),
        int(pairing["bot_b_id"]): int(pairing["bot_b_version_id"]),
    }
    assert frozen_by_bot == {
        int(version_a["bot_id"]): int(version_a["id"]),
        int(version_b["bot_id"]): int(version_b["id"]),
    }

    asyncio.run(manager.start(contest["id"]))
    jobs = store._conn.execute(
        "SELECT ruleset_version,protocol_version,rating_pool_id FROM execution_jobs "
        "WHERE contest_id=? ORDER BY id",
        (contest["id"],),
    ).fetchall()
    assert len(jobs) == 1
    assert dict(jobs[0]) == game_rule_contract("gomoku")
    store.close()


@pytest.mark.parametrize(
    ("blocker", "message"),
    [
        ("missing_authorization", "显式授权迁移 ID 不一致"),
        ("published", "不是 open"),
        ("starts_at", "已进入赛程"),
        ("current_stage_real", "已进入赛程"),
        ("official_ready_real", "已进入赛程"),
        ("dispatched_entry", "已派发报名"),
        ("pairing", "已生成赛程/对局"),
        ("match", "已生成赛程/对局"),
        ("stage_result", "已生成赛程/对局"),
        ("official_result", "已生成赛程/对局"),
        ("execution_job", "已生成赛程/对局"),
    ],
)
def test_rule_cutover_rejects_any_started_or_unreviewed_contest_state(
    tmp_path, blocker, message
):
    store = Store(str(tmp_path / f"contest-blocker-{blocker}.db"))
    _set_previous_contract(store)
    owner, version = _canonical_bot(store, tmp_path, f"contest_{blocker}")
    contest = _open_unstarted_contest(
        store, organizer_id=owner["id"], title=f"blocked {blocker}"
    )
    entry = store.add_contest_entry_once(
        contest["id"], owner["id"], version["bot_id"]
    )
    if blocker == "published":
        with store._tx() as conn:
            conn.execute(
                "UPDATE contests SET status=? WHERE id=?",
                (CONTEST_PUBLISHED, contest["id"]),
            )
    elif blocker == "starts_at":
        with store._tx() as conn:
            conn.execute(
                "UPDATE contests SET starts_at='2026-01-01T00:00:00' WHERE id=?",
                (contest["id"],),
            )
    elif blocker in {"current_stage_real", "official_ready_real"}:
        field = (
            "current_stage_idx"
            if blocker == "current_stage_real"
            else "official_results_ready"
        )
        with store._tx() as conn:
            conn.execute(
                f"UPDATE contests SET {field}=0.5 WHERE id=?",
                (contest["id"],),
            )
    elif blocker == "dispatched_entry":
        with store._tx() as conn:
            conn.execute(
                "UPDATE contest_entries SET dispatched_at='test' WHERE id=?",
                (entry["id"],),
            )
    elif blocker == "pairing":
        store.add_pairing(
            contest["id"],
            version["bot_id"],
            version["bot_id"],
        )
    elif blocker == "match":
        store.create_match(
            f"contest-blocker-{blocker}",
            version["bot_id"],
            version["bot_id"],
            game_id="gomoku",
            contest_id=contest["id"],
        )
    elif blocker == "stage_result":
        store.upsert_stage_result(
            contest["id"],
            0,
            entry["id"],
            bot_id=version["bot_id"],
        )
    elif blocker == "official_result":
        store.upsert_official_result(
            contest["id"],
            entry["id"],
            1,
            bot_id=version["bot_id"],
            user_id=owner["id"],
        )
    elif blocker == "execution_job":
        store.executions.resume()
        store.executions.enqueue(
            source=EXECUTION_SOURCE_MANUAL,
            owner_user_id=owner["id"],
            game_id="gomoku",
            match_type=TYPE_CHALLENGE,
            bot_a_id=version["bot_id"],
            bot_b_id=version["bot_id"],
            bot_a_version_id=version["id"],
            bot_b_version_id=version["id"],
            contest_id=contest["id"],
        )
    _certify_projection(store)

    authorized = () if blocker == "missing_authorization" else (contest["id"],)
    with pytest.raises(ValueError, match=message):
        _plan(
            store,
            f"contest-blocker-{blocker}-cutover",
            migrate_unstarted_contest_ids=authorized,
        )
    assert store.get_active_game_contract("gomoku") == PREVIOUS_CONTRACT
    assert store.get_protocol_cutover(f"contest-blocker-{blocker}-cutover") is None
    store.close()


def test_rule_cutover_rejects_roster_drift_after_review_without_partial_writes(
    tmp_path,
):
    store = Store(str(tmp_path / "contest-roster-plan-drift.db"))
    _set_previous_contract(store)
    owner, version = _canonical_bot(store, tmp_path, "contest_roster_drift")
    contest = _open_unstarted_contest(
        store, organizer_id=owner["id"], title="roster drift"
    )
    entry = store.add_contest_entry_once(
        contest["id"], owner["id"], version["bot_id"]
    )
    _certify_projection(store)
    ids = (contest["id"],)
    plan = _plan(
        store,
        "contest-roster-drift-cutover",
        migrate_unstarted_contest_ids=ids,
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_entries SET seed=seed+1 WHERE id=?",
            (entry["id"],),
        )
    _prepare_cold_cutover(store)

    with pytest.raises(ValueError, match="expected_plan_digest"):
        _apply(
            store,
            plan,
            migrate_unstarted_contest_ids=ids,
        )
    assert store.get_contest(contest["id"])["ruleset_version"] == (
        GOMOKU_PREVIOUS_RULESET
    )
    assert store.get_protocol_cutover(plan["cutover_id"]) is None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM rating_pool_archives WHERE game_id='gomoku'"
    ).fetchone()[0] == 0
    store.close()


def test_rule_cutover_rechecks_zero_result_graph_inside_apply_transaction(tmp_path):
    store = Store(str(tmp_path / "contest-result-plan-drift.db"))
    _set_previous_contract(store)
    owner, version = _canonical_bot(store, tmp_path, "contest_result_drift")
    contest = _open_unstarted_contest(
        store, organizer_id=owner["id"], title="result graph drift"
    )
    entry = store.add_contest_entry_once(
        contest["id"], owner["id"], version["bot_id"]
    )
    _certify_projection(store)
    ids = (contest["id"],)
    plan = _plan(
        store,
        "contest-result-drift-cutover",
        migrate_unstarted_contest_ids=ids,
    )
    store.upsert_stage_result(
        contest["id"], 0, entry["id"], bot_id=version["bot_id"]
    )
    _prepare_cold_cutover(store)

    with pytest.raises(ValueError, match="已生成赛程/对局"):
        _apply(store, plan, migrate_unstarted_contest_ids=ids)
    assert store.get_contest(contest["id"])["ruleset_version"] == (
        GOMOKU_PREVIOUS_RULESET
    )
    assert store.get_protocol_cutover(plan["cutover_id"]) is None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM rating_pool_archives WHERE game_id='gomoku'"
    ).fetchone()[0] == 0
    store.close()


def test_rule_cutover_rolls_back_contest_migration_with_later_pool_failure(
    tmp_path,
):
    store = Store(str(tmp_path / "contest-migration-atomic-rollback.db"))
    _set_previous_contract(store)
    owner, version = _canonical_bot(store, tmp_path, "contest_atomic_rollback")
    contest = _open_unstarted_contest(
        store, organizer_id=owner["id"], title="atomic rollback"
    )
    entry = store.add_contest_entry_once(
        contest["id"], owner["id"], version["bot_id"]
    )
    before_contest = store.get_contest(contest["id"])
    before_entry = store.get_contest_entry(contest["id"], owner["id"])
    _certify_projection(store)
    ids = (contest["id"],)
    plan = _plan(
        store,
        "contest-migration-atomic-rollback-cutover",
        migrate_unstarted_contest_ids=ids,
    )
    _prepare_cold_cutover(store)
    with store._tx() as conn:
        conn.execute(
            "CREATE TRIGGER fail_pool_switch BEFORE UPDATE ON rating_pool_state "
            "WHEN OLD.game_id='gomoku' BEGIN "
            "SELECT RAISE(ABORT, 'forced pool switch failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced pool switch failure"):
        _apply(
            store,
            plan,
            migrate_unstarted_contest_ids=ids,
        )

    assert store.get_contest(contest["id"]) == before_contest
    assert store.get_contest_entry(contest["id"], owner["id"]) == before_entry == entry
    assert store.get_active_game_contract("gomoku") == PREVIOUS_CONTRACT
    assert store.get_protocol_cutover(plan["cutover_id"]) is None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM rating_pool_archives WHERE game_id='gomoku'"
    ).fetchone()[0] == 0
    store.close()


@pytest.mark.parametrize("authorized_ids", [(-1,), (1, 1)])
def test_rule_cutover_rejects_invalid_explicit_contest_ids(tmp_path, authorized_ids):
    store = Store(str(tmp_path / f"invalid-contest-ids-{authorized_ids!r}.db"))
    _set_previous_contract(store)
    _canonical_bot(store, tmp_path, f"invalid_contest_ids_{len(authorized_ids)}")
    _certify_projection(store)
    with pytest.raises(ValueError, match="赛事 ID"):
        _plan(
            store,
            "invalid-explicit-contest-ids-cutover",
            migrate_unstarted_contest_ids=authorized_ids,
        )
    assert store.get_active_game_contract("gomoku") == PREVIOUS_CONTRACT
    assert store.get_protocol_cutover("invalid-explicit-contest-ids-cutover") is None
    store.close()


@pytest.mark.parametrize(
    ("source", "target", "message"),
    [
        (
            PREVIOUS_CONTRACT,
            {**game_rule_contract("gomoku"), "protocol_version": "different-v3"},
            "protocol 完全相同",
        ),
        (
            PREVIOUS_CONTRACT,
            {**game_rule_contract("gomoku"), "ruleset_version": GOMOKU_PREVIOUS_RULESET},
            "更换 ruleset",
        ),
        (
            PREVIOUS_CONTRACT,
            {**game_rule_contract("gomoku"), "rating_pool_id": GOMOKU_PREVIOUS_RATING_POOL},
            "更换 rating pool",
        ),
    ],
)
def test_rule_cutover_contract_shape_is_fail_closed(tmp_path, source, target, message):
    store = Store(str(tmp_path / "invalid-rule-cutover.db"))
    _set_previous_contract(store)
    _canonical_bot(store, tmp_path, "invalid_rule")
    _certify_projection(store)
    with pytest.raises(ValueError, match=message):
        store.plan_game_rule_cutover(
            cutover_id="invalid-rule-edge",
            game_id="gomoku",
            from_contract=source,
            to_contract=target,
        )
    store.close()


def test_rule_cutover_rejects_identical_bytes_inode_replacement_after_review(
    tmp_path,
):
    store = Store(str(tmp_path / "inode-plan-drift.db"))
    _set_previous_contract(store)
    _, version = _canonical_bot(store, tmp_path, "inode_plan")
    _certify_projection(store)
    plan = _plan(store, "inode-plan-cutover")
    binary = Path(version["binary_path"])
    before_inode = binary.stat().st_ino
    replacement = binary.with_name("bot.replacement")
    binary.parent.chmod(0o755)
    replacement.write_bytes(binary.read_bytes())
    replacement.chmod(0o555)
    os.replace(replacement, binary)
    binary.parent.chmod(0o555)
    assert binary.stat().st_ino != before_inode
    _prepare_cold_cutover(store)

    with pytest.raises(ValueError, match="expected_plan_digest"):
        _apply(store, plan)
    assert store.get_active_game_contract("gomoku") == PREVIOUS_CONTRACT
    assert store.get_protocol_cutover(plan["cutover_id"]) is None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM rating_pool_archives WHERE game_id='gomoku'"
    ).fetchone()[0] == 0
    store.close()


def test_fresh_database_uses_fixed_two_and_previous_database_fails_startup_closed(
    tmp_path,
):
    fresh = Store(str(tmp_path / "fresh-fixed-two.db"))
    assert fresh.get_active_game_contract("gomoku") == game_rule_contract("gomoku")
    fresh.assert_runtime_contracts_current()
    fresh.close()

    previous = Store(str(tmp_path / "previous-needs-rule-cutover.db"))
    _set_previous_contract(previous)
    with pytest.raises(RuntimeError) as exc:
        previous.assert_runtime_contracts_current()
    message = str(exc.value)
    assert "拒绝启动在线 runtime" in message
    assert "game-rule-cutover" in message
    assert "game-contract-cutover" in message
    previous.close()


@pytest.mark.parametrize(
    ("blocker", "message"),
    [
        ("control", "部署维护|dispatcher"),
        ("job_attempt", "排空失败.*jobs=1.*attempts=1"),
        ("match", "排空失败.*matches=1"),
        ("lease", "排空失败.*local_leases=1"),
        ("journal", "journal 未静默"),
    ],
)
def test_rule_cutover_offline_blockers_roll_back_without_marker(
    tmp_path, blocker, message
):
    store = Store(str(tmp_path / f"blocker-{blocker}.db"))
    _set_previous_contract(store)
    owner_a, version_a = _canonical_bot(store, tmp_path, f"{blocker}_a")
    _, version_b = _canonical_bot(store, tmp_path, f"{blocker}_b")
    _certify_projection(store)

    if blocker == "job_attempt":
        store.executions.resume()
        store.executions.enqueue(
            source=EXECUTION_SOURCE_MANUAL,
            owner_user_id=owner_a["id"],
            game_id="gomoku",
            match_type=TYPE_CHALLENGE,
            bot_a_id=version_a["bot_id"],
            bot_b_id=version_b["bot_id"],
            bot_a_version_id=version_a["id"],
            bot_b_version_id=version_b["id"],
        )
    agent = None
    if blocker == "lease":
        agent = store.create_local_ai_agent(
            owner_id=owner_a["id"],
            bot_id=version_a["bot_id"],
            label="lease blocker",
            public_id=f"lia_{blocker}",
            token_hash="hash",
            token_hint="hint",
        )
    plan = _plan(store, f"blocker-{blocker}-cutover")

    if blocker != "control":
        if blocker == "job_attempt":
            claimed = store.executions.claim_next(
                max_match_slots=1,
                max_sandbox_units=2,
                aging_seconds=60,
                user_active_limit=1,
                contest_share_slots=1,
            )
            assert claimed is not None
        _prepare_cold_cutover(store)
    if blocker == "match":
        store.create_match(
            "pending-rule-blocker",
            version_a["bot_id"],
            version_b["bot_id"],
            game_id="gomoku",
        )
    elif blocker == "lease":
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO local_ai_leases(agent_id,job_public_id,attempt_no,seat,"
                "acquired_at) VALUES(?, 'lease-blocker-job', 1, 0, 'test')",
                (agent["id"],),
            )
    elif blocker == "journal":
        store.executions.begin_docker_launch(
            launch_token="rule-blocker-token",
            instance_key="qa-rule-blocker",
            owner_kind="preflight",
            job_public_id="rule-blocker-preflight",
            attempt_no=1,
            slot=0,
            container_name="bz-rule-blocker",
            host_boot_id="boot-rule-blocker",
        )

    with pytest.raises(ValueError, match=message):
        _apply(store, plan)
    assert store.get_active_game_contract("gomoku") == PREVIOUS_CONTRACT
    assert store.get_protocol_cutover(plan["cutover_id"]) is None
    assert store._conn.execute(
        "SELECT COUNT(*) FROM rating_pool_archives WHERE game_id='gomoku'"
    ).fetchone()[0] == 0
    store.close()


def _cli_source_database(tmp_path: Path, name: str) -> tuple[Path, dict]:
    database = (tmp_path / f"{name}.db").resolve()
    store = Store(str(database))
    _set_previous_contract(store)
    _canonical_bot(store, tmp_path, f"{name}_bot")
    _certify_projection(store)
    plan = _plan(store, f"{name}-cutover")
    _prepare_cold_cutover(store)
    store.close()
    return database, plan


def test_game_rule_cutover_cli_dry_run_apply_and_lost_output_retry(tmp_path):
    database, _ = _cli_source_database(tmp_path, "rule-cli")
    backup = (tmp_path / "rule-cli.preimage.db").resolve()
    shutil.copyfile(database, backup)
    common = [
        "game-rule-cutover",
        "--db", str(database),
        "--cutover-id", "rule-cli-cutover",
        "--game-id", "gomoku",
        "--from-ruleset", GOMOKU_PREVIOUS_RULESET,
        "--from-protocol", GOMOKU_CURRENT_PROTOCOL,
        "--from-rating-pool", GOMOKU_PREVIOUS_RATING_POOL,
        "--backup", str(backup),
        "--confirm-service-stopped",
        "--confirm-cold-backup",
    ]
    runner = CliRunner()
    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    report = json.loads(dry.output)
    assert report["mode"] == "dry-run"
    assert report["version_manifest"] == []
    apply_args = common + [
        "--apply",
        "--confirm-db", str(database),
        "--expect-plan-digest", report["plan_digest"],
        "--expect-manifest-digest", report["manifest_digest"],
        "--expect-target-preimage-sha256", report["target_preimage_sha256"],
    ]
    first = runner.invoke(cli_app, apply_args)
    assert first.exit_code == 0, first.output
    assert json.loads(first.output)["already_applied"] is False
    second = runner.invoke(cli_app, apply_args)
    assert second.exit_code == 0, second.output
    assert json.loads(second.output)["already_applied"] is True
    wrong_plan_args = list(apply_args)
    digest_index = wrong_plan_args.index("--expect-plan-digest") + 1
    wrong_plan_args[digest_index] = "f" * 64
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    rejected = runner.invoke(cli_app, wrong_plan_args)
    assert rejected.exit_code != 0
    assert "原审核计划" in rejected.output
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


def test_game_rule_cutover_cli_requires_and_applies_explicit_contest_ids(tmp_path):
    database, _ = _cli_source_database(tmp_path, "rule-cli-contest")
    store = Store(str(database))
    organizer = store.create_user(
        "rule_cli_organizer", "rule-cli-organizer@example.test", "hash"
    )
    first = _open_unstarted_contest(
        store, organizer_id=organizer["id"], title="CLI first"
    )
    second = _open_unstarted_contest(
        store, organizer_id=organizer["id"], title="CLI second"
    )
    bot = store._conn.execute(
        "SELECT id,owner_id FROM bots WHERE game_id='gomoku' ORDER BY id LIMIT 1"
    ).fetchone()
    entry = store.add_contest_entry_once(
        second["id"], int(bot["owner_id"]), int(bot["id"])
    )
    pii_sentinels = {
        "real_name_snapshot": "CUTOVER-PRIVATE-NAME",
        "phone_snapshot": "CUTOVER-PRIVATE-PHONE",
        "school_snapshot": "CUTOVER-PRIVATE-SCHOOL",
        "student_id_snapshot": "CUTOVER-PRIVATE-STUDENT",
        "identity_captured_at": "2026-08-25T00:00:00",
        "identity_source": "verified_profile",
    }
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_entries SET real_name_snapshot=?,phone_snapshot=?,"
            "school_snapshot=?,student_id_snapshot=?,identity_captured_at=?,"
            "identity_source=? WHERE id=?",
            (*pii_sentinels.values(), entry["id"]),
        )
    before_entry = dict(
        store._conn.execute(
            "SELECT * FROM contest_entries WHERE id=?", (entry["id"],)
        ).fetchone()
    )
    store.close()
    backup = (tmp_path / "rule-cli-contest.preimage.db").resolve()
    shutil.copyfile(database, backup)
    common = [
        "game-rule-cutover",
        "--db", str(database),
        "--cutover-id", "rule-cli-contest-cutover",
        "--game-id", "gomoku",
        "--from-ruleset", GOMOKU_PREVIOUS_RULESET,
        "--from-protocol", GOMOKU_CURRENT_PROTOCOL,
        "--from-rating-pool", GOMOKU_PREVIOUS_RATING_POOL,
        "--migrate-unstarted-contest-id", str(first["id"]),
        "--migrate-unstarted-contest-id", str(second["id"]),
        "--backup", str(backup),
        "--confirm-service-stopped",
        "--confirm-cold-backup",
    ]
    runner = CliRunner()
    without_ids = runner.invoke(
        cli_app,
        common[:13] + common[17:],
    )
    assert without_ids.exit_code != 0
    assert "显式授权迁移 ID 不一致" in without_ids.output

    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    assert all(value not in dry.output for value in pii_sentinels.values())
    report = json.loads(dry.output)
    assert [
        item["contest_id"] for item in report["contest_contract_migrations"]
    ] == [first["id"], second["id"]]
    applied = runner.invoke(
        cli_app,
        common
        + [
            "--apply",
            "--confirm-db", str(database),
            "--expect-plan-digest", report["plan_digest"],
            "--expect-manifest-digest", report["manifest_digest"],
            "--expect-target-preimage-sha256", report["target_preimage_sha256"],
        ],
    )
    assert applied.exit_code == 0, applied.output
    assert all(value not in applied.output for value in pii_sentinels.values())
    repeated = runner.invoke(
        cli_app,
        common
        + [
            "--apply",
            "--confirm-db", str(database),
            "--expect-plan-digest", report["plan_digest"],
            "--expect-manifest-digest", report["manifest_digest"],
            "--expect-target-preimage-sha256", report["target_preimage_sha256"],
        ],
    )
    assert repeated.exit_code == 0, repeated.output
    assert json.loads(repeated.output)["already_applied"] is True
    assert all(value not in repeated.output for value in pii_sentinels.values())
    with sqlite3.connect(database) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id,status,ruleset_version,protocol_version,rating_pool_id "
            "FROM contests WHERE id IN (?,?) ORDER BY id",
            (first["id"], second["id"]),
        ).fetchall()
        after_entry = dict(
            conn.execute(
                "SELECT * FROM contest_entries WHERE id=?", (entry["id"],)
            ).fetchone()
        )
    assert [tuple(row) for row in rows] == [
        (
            first["id"],
            CONTEST_OPEN,
            GOMOKU_CURRENT_RULESET,
            GOMOKU_CURRENT_PROTOCOL,
            GOMOKU_CURRENT_RATING_POOL,
        ),
        (
            second["id"],
            CONTEST_OPEN,
            GOMOKU_CURRENT_RULESET,
            GOMOKU_CURRENT_PROTOCOL,
            GOMOKU_CURRENT_RATING_POOL,
        ),
    ]
    assert after_entry == before_entry


def test_game_rule_cutover_cli_rejects_raw_marker_impersonation(tmp_path):
    database, plan = _cli_source_database(tmp_path, "rule-spoof")
    backup = (tmp_path / "rule-spoof.preimage.db").resolve()
    shutil.copyfile(database, backup)
    preimage = hashlib.sha256(backup.read_bytes()).hexdigest()
    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO protocol_cutovers(cutover_id,game_id,from_ruleset,to_ruleset,"
            "from_protocol,to_protocol,from_rating_pool,to_rating_pool,manifest_digest,"
            "manifest_json,bot_count,retired_count,cancelled_jobs,archive_digest,"
            "completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "rule-spoof-cutover",
                "gomoku",
                "attacker-ruleset",
                GOMOKU_CURRENT_RULESET,
                GOMOKU_CURRENT_PROTOCOL,
                GOMOKU_CURRENT_PROTOCOL,
                GOMOKU_PREVIOUS_RATING_POOL,
                GOMOKU_CURRENT_RATING_POOL,
                hashlib.sha256(b"[]").hexdigest(),
                "[]",
                0,
                0,
                0,
                "attacker",
                "test",
            ),
        )
    result = CliRunner().invoke(
        cli_app,
        [
            "game-rule-cutover",
            "--db", str(database),
            "--cutover-id", "rule-spoof-cutover",
            "--game-id", "gomoku",
            "--from-ruleset", GOMOKU_PREVIOUS_RULESET,
            "--from-protocol", GOMOKU_CURRENT_PROTOCOL,
            "--from-rating-pool", GOMOKU_PREVIOUS_RATING_POOL,
            "--backup", str(backup),
            "--confirm-service-stopped",
            "--confirm-cold-backup",
            "--apply",
            "--confirm-db", str(database),
            "--expect-plan-digest", plan["plan_digest"],
            "--expect-manifest-digest", hashlib.sha256(b"[]").hexdigest(),
            "--expect-target-preimage-sha256", preimage,
        ],
    )
    assert result.exit_code != 0
    assert "完整匹配" in result.output


def _insert_marker(
    conn: sqlite3.Connection,
    cutover_id: str,
    source: tuple[str, str, str],
    target: tuple[str, str, str],
) -> None:
    conn.execute(
        "INSERT INTO protocol_cutovers(cutover_id,game_id,from_ruleset,to_ruleset,"
        "from_protocol,to_protocol,from_rating_pool,to_rating_pool,manifest_digest,"
        "manifest_json,bot_count,retired_count,cancelled_jobs,archive_digest,completed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            cutover_id,
            "gomoku",
            source[0],
            target[0],
            source[1],
            target[1],
            source[2],
            target[2],
            hashlib.sha256(b"[]").hexdigest(),
            "[]",
            0,
            0,
            0,
            "test",
            "test",
        ),
    )


def test_cutover_chain_allows_same_protocol_edge_but_rejects_generation_reuse(
    tmp_path,
):
    store = Store(str(tmp_path / "chain-integrity.db"))
    first = ("rules-v1", "protocol-v1", "pool-v1")
    second = ("rules-v2", "protocol-v2", "pool-v2")
    third = ("rules-v3", "protocol-v2", "pool-v3")
    with store._tx() as conn:
        _insert_marker(conn, "chain-1", first, second)
        _insert_marker(conn, "chain-2", second, third)
        assert [row["cutover_id"] for row in store._protocol_cutover_chain_tx(conn, "gomoku")] == [
            "chain-1",
            "chain-2",
        ]

    for suffix, target, message in (
        ("protocol", ("rules-v4", "protocol-v1", "pool-v4"), "protocol"),
        ("ruleset", ("rules-v1", "protocol-v3", "pool-v4"), "ruleset"),
        ("pool", ("rules-v4", "protocol-v3", "pool-v1"), "rating pool"),
    ):
        with store._tx() as conn:
            _insert_marker(conn, f"reuse-{suffix}", third, target)
            with pytest.raises(RuntimeError, match=message):
                store._protocol_cutover_chain_tx(conn, "gomoku")
            conn.execute(
                "DELETE FROM protocol_cutovers WHERE cutover_id=?",
                (f"reuse-{suffix}",),
            )
    store.close()


def test_real_hard_cutover_then_same_protocol_rule_cutover_forms_valid_chain(
    tmp_path, monkeypatch
):
    import bzplat.backend.store.db as db_module
    import bzplat.backend.store.schema as schema_module
    from bzplat.backend.bots.manager import BotManager
    from bzplat.backend.tests.test_game_contract_cutover import (
        _PassingCutoverPreflight,
        _legacy_bot,
        _prepare_cold_cutover as prepare_hard,
        _set_legacy_contract,
        _standard_asset,
    )

    store = Store(str(tmp_path / "real-hard-rule-chain.db"))
    _set_legacy_contract(store)
    _, legacy_version = _legacy_bot(store, tmp_path, "real_chain")
    manager = BotManager(store, upload_root=tmp_path / "bot_uploads")
    previous = dict(PREVIOUS_CONTRACT)
    real_schema_contract = schema_module.game_rule_contract
    real_db_contract = db_module.game_rule_contract

    def previous_current(game_id: str, *, legacy: bool = False):
        if game_id == "gomoku" and not legacy:
            return dict(previous)
        return real_schema_contract(game_id, legacy=legacy)

    monkeypatch.setattr(schema_module, "game_rule_contract", previous_current)
    monkeypatch.setattr(db_module, "game_rule_contract", previous_current)
    asset, checksum, size = _standard_asset()
    hard_plan = manager.plan_game_contract_cutover(
        cutover_id="real-chain-hard-v1-v2",
        game_id="gomoku",
        from_contract=real_schema_contract("gomoku", legacy=True),
        to_contract=previous,
        source_binary_path=asset,
        expected_sha256=checksum,
        expected_size_bytes=size,
    )
    prepare_hard(store)
    hard_result = manager.apply_game_contract_cutover(
        cutover_id=hard_plan["cutover_id"],
        game_id="gomoku",
        from_contract=hard_plan["from_contract"],
        to_contract=hard_plan["to_contract"],
        source_binary_path=asset,
        expected_sha256=checksum,
        expected_size_bytes=size,
        expected_manifest_digest=hard_plan["manifest_digest"],
        binary_runner=_PassingCutoverPreflight(),
    )
    assert hard_result["already_applied"] is False
    assert store.get_active_game_contract("gomoku") == previous

    monkeypatch.setattr(schema_module, "game_rule_contract", real_schema_contract)
    monkeypatch.setattr(db_module, "game_rule_contract", real_db_contract)
    rule_plan = _plan(store, "real-chain-rule-v2-v2")
    rule_result = _apply(store, rule_plan)
    assert rule_result["already_applied"] is False
    assert store.get_active_game_contract("gomoku") == game_rule_contract("gomoku")
    versions = store.list_bot_versions(legacy_version["bot_id"])
    assert len(versions) == 2
    assert versions[0]["protocol_version"] == GOMOKU_CURRENT_PROTOCOL
    assert versions[0]["retired_at"] is None
    assert versions[1]["retired_at"] is not None
    with store._tx() as conn:
        chain = store._protocol_cutover_chain_tx(conn, "gomoku")
        assert [row["cutover_id"] for row in chain] == [
            "real-chain-hard-v1-v2",
            "real-chain-rule-v2-v2",
        ]
        assert [row["to_protocol"] for row in chain] == [
            GOMOKU_CURRENT_PROTOCOL,
            GOMOKU_CURRENT_PROTOCOL,
        ]
    archived_pools = {
        row["pool_id"]
        for row in store._conn.execute(
            "SELECT pool_id FROM rating_pool_archives WHERE game_id='gomoku'"
        ).fetchall()
    }
    assert archived_pools == {
        real_schema_contract("gomoku", legacy=True)["rating_pool_id"],
        GOMOKU_PREVIOUS_RATING_POOL,
    }
    store.assert_runtime_contracts_current()

    # A new plan must revalidate every older marker/archive before writing.
    with store._tx() as conn:
        conn.execute(
            "UPDATE rating_pool_archives SET projection_digest='tampered' "
            "WHERE game_id='gomoku' AND pool_id=?",
            (real_schema_contract("gomoku", legacy=True)["rating_pool_id"],),
        )
    future = {
        "ruleset_version": "gomoku-future-rules-v3",
        "protocol_version": GOMOKU_CURRENT_PROTOCOL,
        "rating_pool_id": "gomoku-future-pool-v3",
    }

    def future_current(game_id: str, *, legacy: bool = False):
        if game_id == "gomoku" and not legacy:
            return dict(future)
        return real_schema_contract(game_id, legacy=legacy)

    monkeypatch.setattr(schema_module, "game_rule_contract", future_current)
    monkeypatch.setattr(db_module, "game_rule_contract", future_current)
    marker_count = store._conn.execute(
        "SELECT COUNT(*) FROM protocol_cutovers"
    ).fetchone()[0]
    active_before = store.get_active_game_contract("gomoku")
    with pytest.raises(RuntimeError, match="archive digest"):
        store.plan_game_rule_cutover(
            cutover_id="must-not-write-after-old-tamper",
            game_id="gomoku",
            from_contract=active_before,
            to_contract=future,
        )
    assert store.get_active_game_contract("gomoku") == active_before
    assert store._conn.execute(
        "SELECT COUNT(*) FROM protocol_cutovers"
    ).fetchone()[0] == marker_count
    store.close()
