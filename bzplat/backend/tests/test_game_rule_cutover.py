"""Same-wire game-rule cutover keeps Bot assets while isolating ratings."""
from __future__ import annotations

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
    EXECUTION_SOURCE_MANUAL,
    GOMOKU_CURRENT_PROTOCOL,
    GOMOKU_CURRENT_RATING_POOL,
    GOMOKU_CURRENT_RULESET,
    GOMOKU_PREVIOUS_RATING_POOL,
    GOMOKU_PREVIOUS_RULESET,
    STATUS_COMPLETED,
    TYPE_CHALLENGE,
    game_rule_contract,
)


PREVIOUS_CONTRACT = {
    "ruleset_version": GOMOKU_PREVIOUS_RULESET,
    "protocol_version": GOMOKU_CURRENT_PROTOCOL,
    "rating_pool_id": GOMOKU_PREVIOUS_RATING_POOL,
}


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


def _plan(store: Store, cutover_id: str) -> dict:
    return store.plan_game_rule_cutover(
        cutover_id=cutover_id,
        game_id="gomoku",
        from_contract=PREVIOUS_CONTRACT,
        to_contract=game_rule_contract("gomoku"),
    )


def _apply(store: Store, plan: dict) -> dict:
    with store.offline_cutover_guard() as guard:
        return store.apply_game_rule_cutover(
            cutover_id=plan["cutover_id"],
            game_id="gomoku",
            from_contract=PREVIOUS_CONTRACT,
            to_contract=game_rule_contract("gomoku"),
            expected_plan_digest=plan["plan_digest"],
            offline_guard=guard,
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
