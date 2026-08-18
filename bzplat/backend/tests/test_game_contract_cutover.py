"""规则大版 hard-cutover 的持久化、幂等与执行闸门回归。"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import os
import shutil
import sqlite3
from pathlib import Path

import pytest

from bzplat.backend.bots.manager import BotError, BotManager
from bzplat.backend.store import Store, rating_projection_digests
from bzplat.backend.store.schema import (
    EXECUTION_SOURCE_MANUAL,
    GOMOKU_CURRENT_PROTOCOL,
    GOMOKU_CURRENT_RATING_POOL,
    GOMOKU_CURRENT_RULESET,
    GOMOKU_LEGACY_PROTOCOL,
    GOMOKU_LEGACY_RATING_POOL,
    GOMOKU_LEGACY_RULESET,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    TYPE_CHALLENGE,
    game_rule_contract,
)


def _set_legacy_contract(store: Store) -> None:
    legacy = game_rule_contract("gomoku", legacy=True)
    with store._tx() as conn:
        conn.execute(
            "UPDATE rating_pool_state SET active_pool_id=?,ruleset_version=?,"
            "protocol_version=?,activated_at='legacy-test' WHERE game_id='gomoku'",
            (
                legacy["rating_pool_id"],
                legacy["ruleset_version"],
                legacy["protocol_version"],
            ),
        )


def _certify_projection(store: Store) -> None:
    with store._tx() as conn:
        live = rating_projection_digests(conn)
        assert live["issues"] == []
        conn.execute(
            "UPDATE rating_projection_state SET policy_version='owner-ranked-bot-v4',"
            "rebuilt_at='test',source_settlement_count=?,"
            "source_last_settled_order=?,source_digest=?,projection_digest=?,"
            "plan_digest=?,trusted_mutation_revision=mutation_revision "
            "WHERE singleton=1",
            (
                live["source_settlement_count"],
                live["source_last_settled_order"],
                live["source_digest"],
                live["projection_digest"],
                live["plan_digest"],
            ),
        )


def _legacy_bot(store: Store, tmp_path, key: str) -> tuple[dict, dict]:
    owner = store.create_user(key, f"{key}@example.test", "hash")
    binary = tmp_path / f"{key}-legacy.elf"
    binary.write_bytes(f"legacy-{key}".encode())
    payload = binary.read_bytes()
    bot = store.create_bot(
        owner["id"], key, game_id="gomoku", binary_path=str(binary)
    )
    version = store.add_bot_version(
        bot["id"],
        binary_path=str(binary),
        checksum=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
    )
    return owner, version


def _standard_asset() -> tuple[Path, str, int]:
    asset = Path("/bin/true").resolve(strict=True)
    payload = asset.read_bytes()
    return asset, hashlib.sha256(payload).hexdigest(), len(payload)


def _manager(store: Store, tmp_path) -> BotManager:
    return BotManager(store, upload_root=tmp_path / "bot_uploads")


def _plan(manager: BotManager, *, cutover_id: str) -> dict:
    asset, checksum, size = _standard_asset()
    return manager.plan_game_contract_cutover(
        cutover_id=cutover_id,
        game_id="gomoku",
        from_contract=game_rule_contract("gomoku", legacy=True),
        to_contract=game_rule_contract("gomoku"),
        source_binary_path=asset,
        expected_sha256=checksum,
        expected_size_bytes=size,
        upload_note="platform gomoku action-v2 cutover",
    )


def _prepare_cold_cutover(store: Store) -> None:
    control = store.executions.control()
    if control["dispatcher_state"] != "running":
        store.executions.resume()
    store.executions.begin_maintenance("gomoku contract hard cutover")
    store.executions.set_control(dispatcher_state="stopped", accepting=False)


def _begin_launch(store: Store, *, token: str, owner_kind: str) -> dict:
    return store.executions.begin_docker_launch(
        launch_token=token,
        instance_key="qa-cutover",
        owner_kind=owner_kind,
        job_public_id=f"{owner_kind}-proof",
        attempt_no=1,
        slot=0,
        container_name=f"bz-cutover-{owner_kind}",
        host_boot_id="boot-cutover",
    )


class _PassingCutoverPreflight:
    def __init__(self) -> None:
        self.starts = 0
        self.stops = 0
        self.runtime_modes: list[str] = []
        self.binary_digests: list[str] = []

    async def start_session(self, *_args, **kwargs):
        self.starts += 1
        self.runtime_modes.append(str(kwargs.get("runtime_mode") or ""))
        binary_path = kwargs.get("binary_path") or (_args[0] if _args else None)
        if binary_path:
            self.binary_digests.append(
                hashlib.sha256(Path(binary_path).read_bytes()).hexdigest()
            )
        return "cutover-preflight"

    async def send(self, *_args, **_kwargs):
        return (
            '{"response":{"action":"opening","white2":{"x":7,"y":8},'
            '"black3":{"x":8,"y":8},"n":2}}'
        )

    async def read_extra_line(self, *_args, **_kwargs):
        return ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"

    async def stop_session(self, *_args, **_kwargs):
        self.stops += 1


def test_cold_cutover_allows_only_preflight_docker_launch(tmp_path):
    store = Store(str(tmp_path / "cold-preflight.db"))

    with pytest.raises(RuntimeError, match="dispatcher 未运行"):
        _begin_launch(store, token="not-drained", owner_kind="preflight")

    _prepare_cold_cutover(store)
    with pytest.raises(RuntimeError, match="dispatcher 未运行"):
        _begin_launch(store, token="execution-blocked", owner_kind="execution")

    launch = _begin_launch(store, token="preflight-allowed", owner_kind="preflight")
    assert launch["state"] == "creating"
    assert launch["owner_kind"] == "preflight"
    store.executions.mark_docker_launch_created("preflight-allowed")
    store.executions.clear_docker_launch_created("preflight-allowed")
    assert store.executions.docker_launch()["state"] == "idle"
    store.close()


def _apply(manager: BotManager, plan: dict, *, binary_runner=None) -> dict:
    asset, checksum, size = _standard_asset()
    return manager.apply_game_contract_cutover(
        cutover_id=plan["cutover_id"],
        game_id=plan["game_id"],
        from_contract=plan["from_contract"],
        to_contract=plan["to_contract"],
        source_binary_path=asset,
        expected_sha256=checksum,
        expected_size_bytes=size,
        expected_manifest_digest=plan["manifest_digest"],
        upload_note="platform gomoku action-v2 cutover",
        binary_runner=binary_runner or _PassingCutoverPreflight(),
    )


def test_gomoku_hard_cutover_is_atomic_idempotent_and_retires_old_jobs(
    tmp_path,
):
    store = Store(str(tmp_path / "cutover.db"))
    _set_legacy_contract(store)
    owner_a, old_a = _legacy_bot(store, tmp_path, "legacy_a")
    owner_b, old_b = _legacy_bot(store, tmp_path, "legacy_b")
    bot_a = store.get_bot(old_a["bot_id"])
    bot_b = store.get_bot(old_b["bot_id"])

    old_match = store.create_match(
        "legacy-gomoku-match",
        bot_a["id"],
        bot_b["id"],
        game_id="gomoku",
    )
    assert old_match["ruleset_version"] == GOMOKU_LEGACY_RULESET
    store.update_match(
        old_match["id"],
        status=STATUS_COMPLETED,
        winner=0,
        reason="five",
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
        ended_at="2026-08-16T10:00:00",
    )
    assert store.apply_match_ratings_atomic(
        bot_a["id"],
        bot_b["id"],
        game_id="gomoku",
        rating_a=(1510.0, 340.0, 0.06),
        rating_b=(1490.0, 340.0, 0.06),
        winner=0,
        delta_a=1,
        delta_b=-1,
        reason=old_match["id"],
        settlement_id=old_match["id"],
    )
    _certify_projection(store)
    store.executions.resume()
    queued = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner_a["id"],
        game_id="gomoku",
        match_type=TYPE_CHALLENGE,
        bot_a_id=bot_a["id"],
        bot_b_id=bot_b["id"],
        bot_a_version_id=old_a["id"],
        bot_b_version_id=old_b["id"],
    )
    interrupted = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner_b["id"],
        game_id="gomoku",
        match_type=TYPE_CHALLENGE,
        bot_a_id=bot_b["id"],
        bot_b_id=bot_a["id"],
        bot_a_version_id=old_b["id"],
        bot_b_version_id=old_a["id"],
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status='interrupted',retryable=1,"
            "terminal_reason='runtime_failure',terminal_at='test' WHERE id=?",
            (interrupted["id"],),
        )

    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="gomoku-ccgc-2013-v1-test")
    manifest = plan["version_manifest"]
    assert len({entry["binary_path"] for entry in manifest}) == 2
    assert all(
        Path(entry["binary_path"]).parent.name == f"v{entry['version']}"
        and Path(entry["binary_path"]).name == "bot.bin"
        and "/samples/" not in entry["binary_path"]
        for entry in manifest
    )
    old_rows = {
        row["id"]: dict(row)
        for row in (old_a, old_b)
    }
    _prepare_cold_cutover(store)
    result = _apply(manager, plan)
    assert result["already_applied"] is False
    assert result["bot_count"] == 2
    assert result["retired_count"] == 2
    assert result["cancelled_jobs"] == 2
    assert store.get_active_game_contract("gomoku") == {
        "ruleset_version": GOMOKU_CURRENT_RULESET,
        "protocol_version": GOMOKU_CURRENT_PROTOCOL,
        "rating_pool_id": GOMOKU_CURRENT_RATING_POOL,
    }

    for old_id, before in old_rows.items():
        after = store.get_bot_version(old_id)
        for immutable in (
            "id", "bot_id", "version", "binary_path", "checksum", "size_bytes",
            "protocol_version", "uploaded_at",
        ):
            assert after[immutable] == before[immutable]
        assert after["retired_at"] is not None
        assert after["retirement_reason"] == "ruleset_retired"
        assert after["protocol_version"] == GOMOKU_LEGACY_PROTOCOL

    for bot in (bot_a, bot_b):
        current_bot = store.get_bot(bot["id"])
        current = store.get_current_bot_version(bot["id"])
        assert current_bot["current_version"] == current["version"] == 2
        assert current["protocol_version"] == GOMOKU_CURRENT_PROTOCOL
        assert current["retired_at"] is None
    current_paths = [
        Path(store.get_current_bot_version(bot["id"])["binary_path"])
        for bot in (bot_a, bot_b)
    ]
    assert len({str(path) for path in current_paths}) == 2
    assert len({(path.stat().st_dev, path.stat().st_ino) for path in current_paths}) == 2
    assert all(path.parent.parent.parent == tmp_path / "bot_uploads" for path in current_paths)

    assert store.get_match(old_match["id"])["ruleset_version"] == GOMOKU_LEGACY_RULESET
    archived = store._conn.execute(
        "SELECT * FROM rating_pool_archives WHERE game_id='gomoku'"
    ).fetchone()
    assert archived["pool_id"] == GOMOKU_LEGACY_RATING_POOL
    assert archived["ratings_count"] == 2
    assert archived["history_count"] == 2
    assert archived["pair_count"] == 1
    ratings = store._conn.execute(
        "SELECT * FROM ratings WHERE game_id='gomoku' ORDER BY bot_id"
    ).fetchall()
    assert len(ratings) == 2
    assert all(row["rating"] == 1500.0 and row["matches_played"] == 0 for row in ratings)
    assert store._conn.execute(
        "SELECT COUNT(*) FROM rating_history WHERE game_id='gomoku'"
    ).fetchone()[0] == 0
    assert store.rating_projection_status()["ready"] is True
    from bzplat.backend.rating.rebuild import build_rebuild_plan

    rebuild = build_rebuild_plan(store.path)
    assert not [row for row in rebuild.source if row["game_id"] == "gomoku"]
    rebuilt_gomoku = [
        row for row in rebuild.ratings if row["game_id"] == "gomoku"
    ]
    assert len(rebuilt_gomoku) == 2
    assert all(row["rating"] == 1500.0 and row["matches_played"] == 0 for row in rebuilt_gomoku)

    queued_after = store.executions.get(queued["public_id"])
    interrupted_after = store.executions.get(interrupted["public_id"])
    assert (queued_after["status"], queued_after["retryable"]) == ("cancelled", 0)
    assert (interrupted_after["status"], interrupted_after["retryable"]) == (
        "interrupted", 0,
    )
    with pytest.raises(ValueError, match="不可重试"):
        store.executions.retry(
            interrupted["public_id"], owner_user_id=owner_b["id"]
        )
    with pytest.raises(ValueError, match="退役"):
        store.set_current_version(bot_a["id"], 1)
    with pytest.raises(ValueError, match="审计证据"):
        store.delete_bot_version(bot_a["id"], 1)
    repeated = _apply(manager, plan)
    assert repeated["already_applied"] is True
    assert store._conn.execute(
        "SELECT COUNT(*) FROM bot_versions WHERE bot_id IN (?,?)",
        (bot_a["id"], bot_b["id"]),
    ).fetchone()[0] == 4

    drifted = [dict(entry) for entry in manifest]
    drifted[0]["upload_note"] = "different manifest"
    with store.offline_cutover_guard() as guard:
        with pytest.raises(ValueError, match="不同切换"):
            store.cutover_game_contract(
                cutover_id="gomoku-ccgc-2013-v1-test",
                game_id="gomoku",
                from_contract=game_rule_contract("gomoku", legacy=True),
                to_contract=game_rule_contract("gomoku"),
                version_manifest=drifted,
                canonical_binary_root=tmp_path / "bot_uploads",
                offline_guard=guard,
            )

    # Normal service startup resumes the stopped dispatcher under the durable
    # maintenance bit; only then may the operator reopen admission.
    store.executions.resume()
    store.executions.end_maintenance()
    with pytest.raises(ValueError, match="退役|协议"):
        store.executions.enqueue(
            source=EXECUTION_SOURCE_MANUAL,
            owner_user_id=owner_a["id"],
            game_id="gomoku",
            match_type=TYPE_CHALLENGE,
            bot_a_id=bot_a["id"],
            bot_b_id=bot_b["id"],
            bot_a_version_id=old_a["id"],
            bot_b_version_id=old_b["id"],
        )

    current_a = store.get_current_bot_version(bot_a["id"])
    current_b = store.get_current_bot_version(bot_b["id"])
    stale = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner_a["id"],
        game_id="gomoku",
        match_type=TYPE_CHALLENGE,
        bot_a_id=bot_a["id"],
        bot_b_id=bot_b["id"],
        bot_a_version_id=current_a["id"],
        bot_b_version_id=current_b["id"],
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET ruleset_version=?,protocol_version=?,"
            "rating_pool_id=? WHERE id=?",
            (
                GOMOKU_LEGACY_RULESET,
                GOMOKU_LEGACY_PROTOCOL,
                GOMOKU_LEGACY_RATING_POOL,
                stale["id"],
            ),
        )
    assert store.executions.claim_next(
        max_match_slots=1,
        max_sandbox_units=2,
        aging_seconds=60,
        user_active_limit=1,
        contest_share_slots=1,
    ) is None
    assert store.executions.get(stale["public_id"])["terminal_reason"] == "ruleset_retired"
    store.close()


def test_game_contract_backfill_is_repeatable(tmp_path):
    path = tmp_path / "migration-twice.db"
    store = Store(str(path))
    _set_legacy_contract(store)
    owner, version = _legacy_bot(store, tmp_path, "migration_legacy")
    contest = store.create_contest(
        "legacy contract contest",
        owner["id"],
        game_id="gomoku",
        template_id="gomoku_round_robin",
    )
    match = store.create_match(
        "migration-legacy-match",
        version["bot_id"],
        version["bot_id"],
        game_id="gomoku",
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE bots SET protocol_version='' WHERE id=?", (version["bot_id"],)
        )
        conn.execute(
            "UPDATE bot_versions SET protocol_version='' WHERE id=?", (version["id"],)
        )
        conn.execute(
            "UPDATE matches_gomoku SET ruleset_version='',protocol_version='',"
            "rating_pool_id='' WHERE id=?",
            (match["id"],),
        )
        conn.execute(
            "UPDATE contests SET ruleset_version='',protocol_version='',"
            "rating_pool_id='' WHERE id=?",
            (contest["id"],),
        )
    store.close()

    for _ in range(2):
        reopened = Store(str(path))
        assert reopened.get_active_game_contract("gomoku") == {
            "ruleset_version": GOMOKU_LEGACY_RULESET,
            "protocol_version": GOMOKU_LEGACY_PROTOCOL,
            "rating_pool_id": GOMOKU_LEGACY_RATING_POOL,
        }
        assert reopened.get_bot(version["bot_id"])["protocol_version"] == GOMOKU_LEGACY_PROTOCOL
        assert reopened.get_bot_version(version["id"])["protocol_version"] == GOMOKU_LEGACY_PROTOCOL
        restored_match = reopened.get_match(match["id"])
        assert restored_match["ruleset_version"] == GOMOKU_LEGACY_RULESET
        assert restored_match["rating_pool_id"] == GOMOKU_LEGACY_RATING_POOL
        restored_contest = reopened.get_contest(contest["id"])
        assert restored_contest["ruleset_version"] == GOMOKU_LEGACY_RULESET
        assert restored_contest["protocol_version"] == GOMOKU_LEGACY_PROTOCOL
        assert restored_contest["rating_pool_id"] == GOMOKU_LEGACY_RATING_POOL
        reopened.close()


def test_cutover_markers_form_a_multi_generation_chain(tmp_path, monkeypatch):
    import bzplat.backend.store.db as db_module
    import bzplat.backend.store.schema as schema_module

    store = Store(str(tmp_path / "multi-generation.db"))
    _set_legacy_contract(store)
    _, legacy = _legacy_bot(store, tmp_path, "multi_generation")
    manager = _manager(store, tmp_path)
    first = _plan(manager, cutover_id="generation-v1-v2")
    _prepare_cold_cutover(store)
    assert _apply(manager, first)["already_applied"] is False

    v3 = {
        "ruleset_version": "gomoku_future_rules_v3",
        "protocol_version": "gomoku_future_action_v3",
        "rating_pool_id": "gomoku_future_rating_v3",
    }
    real_schema_contract = schema_module.game_rule_contract

    def future_contract(game_id: str, *, legacy: bool = False):
        if game_id == "gomoku" and not legacy:
            return dict(v3)
        return real_schema_contract(game_id, legacy=legacy)

    monkeypatch.setattr(schema_module, "game_rule_contract", future_contract)
    monkeypatch.setattr(db_module, "game_rule_contract", future_contract)
    asset, checksum, size = _standard_asset()
    second = manager.plan_game_contract_cutover(
        cutover_id="generation-v2-v3",
        game_id="gomoku",
        from_contract=game_rule_contract("gomoku"),
        to_contract=v3,
        source_binary_path=asset,
        expected_sha256=checksum,
        expected_size_bytes=size,
    )
    _prepare_cold_cutover(store)
    second_result = manager.apply_game_contract_cutover(
        cutover_id=second["cutover_id"],
        game_id="gomoku",
        from_contract=second["from_contract"],
        to_contract=second["to_contract"],
        source_binary_path=asset,
        expected_sha256=checksum,
        expected_size_bytes=size,
        expected_manifest_digest=second["manifest_digest"],
        binary_runner=_PassingCutoverPreflight(),
    )
    assert second_result["already_applied"] is False
    assert store.get_active_game_contract("gomoku") == v3
    versions = store.list_bot_versions(legacy["bot_id"])
    assert [row["protocol_version"] for row in versions] == [
        v3["protocol_version"],
        GOMOKU_CURRENT_PROTOCOL,
        GOMOKU_LEGACY_PROTOCOL,
    ]
    assert versions[0]["retired_at"] is None
    assert all(row["retired_at"] is not None for row in versions[1:])
    markers = {
        row["cutover_id"]: dict(row)
        for row in store._conn.execute(
            "SELECT * FROM protocol_cutovers WHERE game_id='gomoku'"
        ).fetchall()
    }
    assert markers["generation-v1-v2"]["retired_count"] == 1
    assert markers["generation-v2-v3"]["retired_count"] == 1
    assert {
        row["pool_id"]
        for row in store._conn.execute(
            "SELECT pool_id FROM rating_pool_archives WHERE game_id='gomoku'"
        ).fetchall()
    } == {GOMOKU_LEGACY_RATING_POOL, GOMOKU_CURRENT_RATING_POOL}
    first_audit = store.get_protocol_cutover("generation-v1-v2")
    second_audit = store.get_protocol_cutover("generation-v2-v3")
    first_version = next(
        row
        for row in versions
        if row["version"] == first_audit["version_manifest"][0]["version"]
    )
    second_version = next(
        row
        for row in versions
        if row["version"] == second_audit["version_manifest"][0]["version"]
    )
    assert first_version["retired_at"] is not None
    assert second_version["retired_at"] is None
    assert Path(first_version["binary_path"]).is_file()
    assert Path(second_version["binary_path"]).is_file()
    store.assert_protocol_cutover_postconditions()

    # Both generations remain independently idempotent after the second edge.
    assert _apply(manager, first)["already_applied"] is True
    assert manager.apply_game_contract_cutover(
        cutover_id=second["cutover_id"],
        game_id="gomoku",
        from_contract=second["from_contract"],
        to_contract=second["to_contract"],
        source_binary_path=asset,
        expected_sha256=checksum,
        expected_size_bytes=size,
        expected_manifest_digest=second["manifest_digest"],
        binary_runner=_PassingCutoverPreflight(),
    )["already_applied"] is True

    # A disconnected edge is rejected.
    with store._tx() as conn:
        conn.execute(
            "UPDATE protocol_cutovers SET from_ruleset='broken_source' "
            "WHERE cutover_id='generation-v2-v3'"
        )
    with pytest.raises(RuntimeError, match="断链|多起点"):
        store.assert_protocol_cutover_postconditions()
    with store._tx() as conn:
        conn.execute(
            "UPDATE protocol_cutovers SET from_ruleset=? "
            "WHERE cutover_id='generation-v2-v3'",
            (GOMOKU_CURRENT_RULESET,),
        )

    # A second edge from the same source is a fork and is rejected.
    with store._tx() as conn:
        conn.execute(
            "INSERT INTO protocol_cutovers SELECT 'generation-fork',game_id,"
            "from_ruleset,'fork_rules',from_protocol,'fork_protocol',"
            "from_rating_pool,'fork_pool',manifest_digest,manifest_json,bot_count,"
            "retired_count,cancelled_jobs,archive_digest,completed_at "
            "FROM protocol_cutovers WHERE cutover_id='generation-v2-v3'"
        )
    with pytest.raises(RuntimeError, match="分叉|合并"):
        store.assert_protocol_cutover_postconditions()
    with store._tx() as conn:
        conn.execute(
            "DELETE FROM protocol_cutovers WHERE cutover_id='generation-fork'"
        )

    # Reusing a protocol generation ID is forbidden even when the full triplet
    # differs, because retired_count is reconstructed by protocol generation.
    with store._tx() as conn:
        conn.execute(
            "UPDATE protocol_cutovers SET to_protocol=? "
            "WHERE cutover_id='generation-v2-v3'",
            (GOMOKU_LEGACY_PROTOCOL,),
        )
    with pytest.raises(RuntimeError, match="protocol 代际 ID|断链|环"):
        store.assert_protocol_cutover_postconditions()
    store.close()


def test_cutover_rolls_back_every_write_if_final_projection_check_fails(
    tmp_path, monkeypatch
):
    import bzplat.backend.store.db as db_module

    store = Store(str(tmp_path / "rollback.db"))
    _set_legacy_contract(store)
    _, old = _legacy_bot(store, tmp_path, "rollback_legacy")
    bot = store.get_bot(old["bot_id"])
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="rollback-test")
    real_digests = db_module.rating_projection_digests

    def fail_only_after_contract_cas(conn):
        state = conn.execute(
            "SELECT active_pool_id FROM rating_pool_state WHERE game_id='gomoku'"
        ).fetchone()
        if state["active_pool_id"] == GOMOKU_CURRENT_RATING_POOL:
            raise RuntimeError("injected final verification failure")
        return real_digests(conn)

    monkeypatch.setattr(db_module, "rating_projection_digests", fail_only_after_contract_cas)
    _prepare_cold_cutover(store)
    with pytest.raises(RuntimeError, match="injected final"):
        _apply(manager, plan)

    assert store.get_active_game_contract("gomoku") == game_rule_contract(
        "gomoku", legacy=True
    )
    assert store.get_bot(bot["id"])["current_version"] == 1
    old_after = store.get_bot_version(old["id"])
    assert old_after["retired_at"] is None
    assert len(store.list_bot_versions(bot["id"])) == 1
    assert store._conn.execute(
        "SELECT COUNT(*) FROM rating_pool_archives"
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM protocol_cutovers"
    ).fetchone()[0] == 0
    assert not Path(plan["version_manifest"][0]["binary_path"]).exists()
    store.close()


def test_cutover_requires_stopped_maintenance_and_offline_flock(tmp_path):
    store = Store(str(tmp_path / "cold-only.db"))
    _set_legacy_contract(store)
    _legacy_bot(store, tmp_path, "cold_only")
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="cold-only-test")
    target = Path(plan["version_manifest"][0]["binary_path"])

    with pytest.raises(ValueError, match="部署维护|dispatcher"):
        _apply(manager, plan)
    assert not target.exists()
    assert store._conn.execute("SELECT COUNT(*) FROM protocol_cutovers").fetchone()[0] == 0

    _prepare_cold_cutover(store)
    with store.offline_cutover_guard():
        with pytest.raises(RuntimeError, match="停服冷切|dispatcher"):
            _apply(manager, plan)
    assert not target.exists()

    assert _apply(manager, plan)["already_applied"] is False
    store.close()


def test_cutover_cli_locks_raw_db_before_store_migration(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import bzplat.backend.cli as cli_module

    database = (tmp_path / "raw-lock.db").resolve()
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel(value) VALUES('unchanged')")
    backup = (tmp_path / "raw-lock.backup.db").resolve()
    shutil.copyfile(database, backup)
    before_bytes = database.read_bytes()
    before_mtime_ns = database.stat().st_mtime_ns
    source, checksum, size = _standard_asset()
    constructed: list[str] = []

    def forbidden_store(path: str):
        constructed.append(path)
        raise AssertionError("Store must not open while dispatcher lock is held")

    monkeypatch.setattr(cli_module, "Store", forbidden_store)
    lock_fd = os.open(
        str(database) + ".execution-dispatcher.lock",
        os.O_CREAT | os.O_RDWR,
        0o600,
    )
    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = CliRunner().invoke(
            cli_module.app,
            [
                "game-contract-cutover",
                "--db", str(database),
                "--cutover-id", "raw-lock-proof",
                "--game-id", "gomoku",
                "--from-ruleset", GOMOKU_LEGACY_RULESET,
                "--from-protocol", GOMOKU_LEGACY_PROTOCOL,
                "--from-rating-pool", GOMOKU_LEGACY_RATING_POOL,
                "--source-binary", str(source),
                "--source-sha256", checksum,
                "--source-size-bytes", str(size),
                "--backup", str(backup),
                "--confirm-service-stopped",
                "--confirm-cold-backup",
            ],
        )
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)

    assert result.exit_code != 0
    assert "dispatcher 仍在线" in result.output
    assert constructed == []
    assert database.stat().st_mtime_ns == before_mtime_ns
    assert database.read_bytes() == before_bytes
    with sqlite3.connect(database) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {"sentinel"}


def test_cutover_cli_requires_cold_backup_before_store_open(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import bzplat.backend.cli as cli_module

    database = (tmp_path / "missing-gates.db").resolve()
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    source, checksum, size = _standard_asset()
    constructed: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "Store",
        lambda path: constructed.append(path),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "game-contract-cutover",
            "--db", str(database),
            "--cutover-id", "missing-gates",
            "--game-id", "gomoku",
            "--from-ruleset", GOMOKU_LEGACY_RULESET,
            "--from-protocol", GOMOKU_LEGACY_PROTOCOL,
            "--from-rating-pool", GOMOKU_LEGACY_RATING_POOL,
            "--source-binary", str(source),
            "--source-sha256", checksum,
            "--source-size-bytes", str(size),
        ],
    )

    assert result.exit_code != 0
    assert "confirm-service-stopped" in result.output
    assert constructed == []
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


def test_cutover_cli_rejects_stale_backup_before_store_open(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    import bzplat.backend.cli as cli_module

    database = (tmp_path / "stale-target.db").resolve()
    backup = (tmp_path / "stale-backup.db").resolve()
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel(value) VALUES('target')")
    with sqlite3.connect(backup) as conn:
        conn.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        conn.execute("INSERT INTO sentinel(value) VALUES('stale')")
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    source, checksum, size = _standard_asset()
    constructed: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "Store",
        lambda path: constructed.append(path),
    )

    result = CliRunner().invoke(
        cli_module.app,
        [
            "game-contract-cutover",
            "--db", str(database),
            "--cutover-id", "stale-backup",
            "--game-id", "gomoku",
            "--from-ruleset", GOMOKU_LEGACY_RULESET,
            "--from-protocol", GOMOKU_LEGACY_PROTOCOL,
            "--from-rating-pool", GOMOKU_LEGACY_RATING_POOL,
            "--source-binary", str(source),
            "--source-sha256", checksum,
            "--source-size-bytes", str(size),
            "--backup", str(backup),
            "--confirm-service-stopped",
            "--confirm-cold-backup",
        ],
    )

    assert result.exit_code != 0
    assert "逐字节同一冷备" in result.output
    assert constructed == []
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


def test_cutover_cli_dry_run_migrates_only_temporary_copy(tmp_path):
    from typer.testing import CliRunner

    from bzplat.backend.cli import app as cli_app

    database = (tmp_path / "dry-run-target.db").resolve()
    store = Store(str(database))
    _set_legacy_contract(store)
    store.close()
    backup = (tmp_path / "dry-run-target.backup.db").resolve()
    shutil.copyfile(database, backup)
    before = (
        database.read_bytes(),
        database.stat().st_mtime_ns,
        sqlite3.connect(database).execute("PRAGMA schema_version").fetchone()[0],
    )
    source, checksum, size = _standard_asset()

    result = CliRunner().invoke(
        cli_app,
        [
            "game-contract-cutover",
            "--db", str(database),
            "--cutover-id", "temporary-plan",
            "--game-id", "gomoku",
            "--from-ruleset", GOMOKU_LEGACY_RULESET,
            "--from-protocol", GOMOKU_LEGACY_PROTOCOL,
            "--from-rating-pool", GOMOKU_LEGACY_RATING_POOL,
            "--source-binary", str(source),
            "--source-sha256", checksum,
            "--source-size-bytes", str(size),
            "--backup", str(backup),
            "--confirm-service-stopped",
            "--confirm-cold-backup",
        ],
    )

    assert result.exit_code == 0, result.output
    report = __import__("json").loads(result.output)
    assert report["mode"] == "dry-run"
    assert report["backup_path"] == str(backup)
    after = (
        database.read_bytes(),
        database.stat().st_mtime_ns,
        sqlite3.connect(database).execute("PRAGMA schema_version").fetchone()[0],
    )
    assert after == before
    assert not list(tmp_path.glob(".dry-run-target.db.cutover-plan-*.db*"))
    assert not (tmp_path / "bot_uploads").exists()


def test_cutover_cli_binds_apply_to_reviewed_preimage_before_store_open(
    tmp_path, monkeypatch
):
    from typer.testing import CliRunner

    import bzplat.backend.cli as cli_module

    database = (tmp_path / "preimage-target.db").resolve()
    store = Store(str(database))
    _set_legacy_contract(store)
    store.close()
    original_backup = (tmp_path / "preimage-original.db").resolve()
    shutil.copyfile(database, original_backup)
    reviewed_digest = hashlib.sha256(database.read_bytes()).hexdigest()

    with sqlite3.connect(database) as conn:
        conn.execute(
            "INSERT INTO platform_settings(key,value,updated_at) "
            "VALUES('post_dry_run_change','1','test')"
        )
    current_backup = (tmp_path / "preimage-current.db").resolve()
    shutil.copyfile(database, current_backup)
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    source, checksum, size = _standard_asset()
    constructed: list[str] = []
    monkeypatch.setattr(
        cli_module,
        "Store",
        lambda path: constructed.append(path),
    )
    result = CliRunner().invoke(
        cli_module.app,
        [
            "game-contract-cutover",
            "--db", str(database),
            "--cutover-id", "preimage-binding-test",
            "--game-id", "gomoku",
            "--from-ruleset", GOMOKU_LEGACY_RULESET,
            "--from-protocol", GOMOKU_LEGACY_PROTOCOL,
            "--from-rating-pool", GOMOKU_LEGACY_RATING_POOL,
            "--source-binary", str(source),
            "--source-sha256", checksum,
            "--source-size-bytes", str(size),
            "--backup", str(current_backup),
            "--confirm-service-stopped",
            "--confirm-cold-backup",
            "--apply",
            "--confirm-db", str(database),
            "--expect-manifest-digest", "0" * 64,
            "--expect-target-preimage-sha256", reviewed_digest,
        ],
        env={"BZ_BOT_LOCAL": ""},
    )
    assert result.exit_code != 0
    assert "preimage" in result.output
    assert constructed == []
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


def test_cutover_cli_post_commit_retry_uses_original_backup_and_marker(
    tmp_path, monkeypatch
):
    from typer.testing import CliRunner

    import bzplat.backend.runtime.binary_runner as runner_module
    import bzplat.backend.runtime.docker_supervisor as supervisor_module
    from bzplat.backend.cli import app as cli_app

    database = (tmp_path / "cli-idempotent.db").resolve()
    store = Store(str(database))
    _set_legacy_contract(store)
    _legacy_bot(store, tmp_path, "cli_idempotent")
    _prepare_cold_cutover(store)
    store.close()
    backup = (tmp_path / "cli-idempotent.preimage.db").resolve()
    shutil.copyfile(database, backup)
    source, checksum, size = _standard_asset()
    common = [
        "game-contract-cutover",
        "--db", str(database),
        "--cutover-id", "cli-idempotent-test",
        "--game-id", "gomoku",
        "--from-ruleset", GOMOKU_LEGACY_RULESET,
        "--from-protocol", GOMOKU_LEGACY_PROTOCOL,
        "--from-rating-pool", GOMOKU_LEGACY_RATING_POOL,
        "--source-binary", str(source),
        "--source-sha256", checksum,
        "--source-size-bytes", str(size),
        "--backup", str(backup),
        "--confirm-service-stopped",
        "--confirm-cold-backup",
    ]
    runner = CliRunner()
    dry = runner.invoke(cli_app, common, env={"BZ_BOT_LOCAL": ""})
    assert dry.exit_code == 0, dry.output
    dry_report = __import__("json").loads(dry.output)

    preflight = _PassingCutoverPreflight()
    monkeypatch.setattr(
        supervisor_module, "DockerSupervisor", lambda **_kwargs: object()
    )
    monkeypatch.setattr(
        runner_module, "BinaryRunner", lambda **_kwargs: preflight
    )
    apply_args = common + [
        "--apply",
        "--confirm-db", str(database),
        "--expect-manifest-digest", dry_report["manifest_digest"],
        "--expect-target-preimage-sha256",
        dry_report["target_preimage_sha256"],
    ]
    first = runner.invoke(cli_app, apply_args, env={"BZ_BOT_LOCAL": ""})
    assert first.exit_code == 0, first.output
    assert __import__("json").loads(first.output)["already_applied"] is False

    second = runner.invoke(cli_app, apply_args, env={"BZ_BOT_LOCAL": ""})
    assert second.exit_code == 0, second.output
    second_report = __import__("json").loads(second.output)
    assert second_report["already_applied"] is True
    assert second_report["manifest_digest"] == dry_report["manifest_digest"]
    reopened = Store(str(database))
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM protocol_cutovers WHERE cutover_id='cli-idempotent-test'"
    ).fetchone()[0] == 1
    assert reopened._conn.execute(
        "SELECT COUNT(*) FROM bot_versions WHERE protocol_version=?",
        (GOMOKU_CURRENT_PROTOCOL,),
    ).fetchone()[0] == 1
    reopened.close()


def test_cutover_preflight_failure_writes_no_files_or_metadata(tmp_path):
    class RejectOpeningPreflight(_PassingCutoverPreflight):
        async def send(self, *_args, **_kwargs):
            return '{"response":{"action":"move","x":7,"y":7}}'

    store = Store(str(tmp_path / "preflight-fail.db"))
    _set_legacy_contract(store)
    _, version = _legacy_bot(store, tmp_path, "preflight_fail")
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="preflight-fail-test")
    target = Path(plan["version_manifest"][0]["binary_path"])
    _prepare_cold_cutover(store)
    runner = RejectOpeningPreflight()

    with pytest.raises(BotError, match="现行规则首回合预检") as exc:
        _apply(manager, plan, binary_runner=runner)
    assert exc.value.code == "cutover_preflight_failed"
    assert (runner.starts, runner.stops) == (1, 1)
    assert not target.exists()
    assert store.get_active_game_contract("gomoku") == game_rule_contract(
        "gomoku", legacy=True
    )
    assert store.get_current_bot_version(version["bot_id"])["id"] == version["id"]
    assert store._conn.execute("SELECT COUNT(*) FROM protocol_cutovers").fetchone()[0] == 0
    store.close()


def test_cutover_preflights_the_same_reviewed_bytes_that_are_deployed(
    tmp_path,
):
    source = tmp_path / "reviewed-standard.elf"
    source.write_bytes(Path("/bin/true").read_bytes())
    reviewed = source.read_bytes()
    checksum = hashlib.sha256(reviewed).hexdigest()

    class ReplaceSourceDuringPreflight(_PassingCutoverPreflight):
        async def start_session(self, *args, **kwargs):
            result = await super().start_session(*args, **kwargs)
            source.write_bytes(Path("/bin/false").read_bytes())
            return result

    store = Store(str(tmp_path / "preflight-snapshot.db"))
    _set_legacy_contract(store)
    _, old = _legacy_bot(store, tmp_path, "preflight_snapshot")
    manager = _manager(store, tmp_path)
    plan = manager.plan_game_contract_cutover(
        cutover_id="preflight-snapshot-test",
        game_id="gomoku",
        from_contract=game_rule_contract("gomoku", legacy=True),
        to_contract=game_rule_contract("gomoku"),
        source_binary_path=source,
        expected_sha256=checksum,
        expected_size_bytes=len(reviewed),
    )
    _prepare_cold_cutover(store)
    runner = ReplaceSourceDuringPreflight()
    result = manager.apply_game_contract_cutover(
        cutover_id=plan["cutover_id"],
        game_id=plan["game_id"],
        from_contract=plan["from_contract"],
        to_contract=plan["to_contract"],
        source_binary_path=source,
        expected_sha256=checksum,
        expected_size_bytes=len(reviewed),
        expected_manifest_digest=plan["manifest_digest"],
        binary_runner=runner,
    )
    deployed = Path(store.get_current_bot_version(old["bot_id"])["binary_path"])
    assert runner.binary_digests == [checksum]
    assert hashlib.sha256(deployed.read_bytes()).hexdigest() == checksum
    assert hashlib.sha256(source.read_bytes()).hexdigest() != checksum
    assert result["preflight"]["source_checksum"] == checksum
    assert not list((tmp_path / "bot_uploads").glob(".cutover-preflight-*"))
    store.close()


def test_cutover_commit_then_wrapper_error_preserves_committed_assets(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "commit-wrapper.db"))
    _set_legacy_contract(store)
    _, old = _legacy_bot(store, tmp_path, "commit_wrapper")
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="commit-wrapper-test")
    _prepare_cold_cutover(store)
    real_cutover = store.cutover_game_contract

    def commit_then_raise(**kwargs):
        real_cutover(**kwargs)
        raise RuntimeError("simulated lost CLI output")

    monkeypatch.setattr(store, "cutover_game_contract", commit_then_raise)
    with pytest.raises(RuntimeError, match="lost CLI output"):
        _apply(manager, plan)

    marker = store.get_protocol_cutover(plan["cutover_id"])
    assert marker is not None
    store.assert_protocol_cutover_postconditions(
        plan["cutover_id"],
        expected_manifest_digest=plan["manifest_digest"],
    )
    current = store.get_current_bot_version(old["bot_id"])
    assert current["protocol_version"] == GOMOKU_CURRENT_PROTOCOL
    assert Path(current["binary_path"]).is_file()
    store.close()


def test_cutover_fsync_failure_leaves_no_marker_or_staging(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "fsync-failure.db"))
    _set_legacy_contract(store)
    _, old = _legacy_bot(store, tmp_path, "fsync_failure")
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="fsync-failure-test")
    _prepare_cold_cutover(store)
    real_fsync = manager._fsync_path

    def fail_staged_directory(path: Path, *, directory: bool = False):
        if directory and path.name.startswith(".cutover-v"):
            raise OSError("simulated directory fsync failure")
        return real_fsync(path, directory=directory)

    monkeypatch.setattr(manager, "_fsync_path", fail_staged_directory)
    with pytest.raises(OSError, match="fsync failure"):
        _apply(manager, plan)
    assert store.get_current_bot_version(old["bot_id"])["id"] == old["id"]
    assert store.get_protocol_cutover(plan["cutover_id"]) is None
    assert not (tmp_path / "bot_uploads").exists()
    store.close()


def test_cutover_rejects_symlink_and_non_executable_existing_targets(
    tmp_path,
):
    store = Store(str(tmp_path / "unsafe-target.db"))
    _set_legacy_contract(store)
    _legacy_bot(store, tmp_path, "unsafe_target")
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="unsafe-target-test")
    entry = plan["version_manifest"][0]
    dest_dir = Path(entry["binary_path"]).parent
    dest_dir.parent.mkdir(parents=True)
    external = tmp_path / "external-sentinel"
    external.mkdir()
    sentinel = external / "keep.txt"
    sentinel.write_bytes(b"do-not-touch")
    sentinel.chmod(0o444)
    dest_dir.symlink_to(external, target_is_directory=True)
    _prepare_cold_cutover(store)
    with pytest.raises(BotError) as symlink_error:
        _apply(manager, plan)
    assert symlink_error.value.code == "unsafe_cutover_target"
    assert sentinel.read_bytes() == b"do-not-touch"
    assert sentinel.stat().st_mode & 0o777 == 0o444
    assert dest_dir.is_symlink()
    assert store.get_protocol_cutover(plan["cutover_id"]) is None

    dest_dir.unlink()
    dest_dir.mkdir()
    dest = dest_dir / "bot.bin"
    dest.write_bytes(_standard_asset()[0].read_bytes())
    dest.chmod(0o644)
    dest_dir.chmod(0o555)
    with pytest.raises(BotError) as mode_error:
        _apply(manager, plan)
    assert mode_error.value.code == "unsafe_cutover_target"
    assert dest.stat().st_mode & 0o777 == 0o644
    assert store.get_protocol_cutover(plan["cutover_id"]) is None
    store.close()


def test_cutover_retired_versions_block_bot_user_hard_delete_and_file_purge(
    tmp_path,
):
    store = Store(str(tmp_path / "retired-delete.db"))
    _set_legacy_contract(store)
    owner_bot, old_bot = _legacy_bot(store, tmp_path, "retired_delete_bot")
    owner_user, old_user = _legacy_bot(store, tmp_path, "retired_delete_user")
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="retired-delete-test")
    _prepare_cold_cutover(store)
    assert _apply(manager, plan)["already_applied"] is False

    bot_id = int(old_bot["bot_id"])
    user_bot_id = int(old_user["bot_id"])
    bot_current = store.get_bot(bot_id)
    user_current = store.get_bot(user_bot_id)
    protected_paths = [
        Path(old_bot["binary_path"]),
        Path(old_user["binary_path"]),
        Path(bot_current["binary_path"]),
        Path(user_current["binary_path"]),
    ]
    assert all(path.is_file() for path in protected_paths)

    bot_delete = store.delete_bot_if_safe(bot_id)
    assert bot_delete == {
        "found": True,
        "deleted": False,
        "references": {"matches": 0, "pairings": 0, "audit_versions": 2},
    }
    user_delete = store.delete_user_if_safe(owner_user["id"])
    assert user_delete["deleted"] is False
    assert user_delete["blockers"]["audit_versions"] == 2

    with pytest.raises(ValueError, match="marker 引用"):
        store.delete_bot_version(bot_id, int(bot_current["current_version"]))

    with pytest.raises(ValueError, match="不可删除审计证据"):
        store.delete_bot(bot_id)
    with pytest.raises(ValueError, match="不可删除审计证据"):
        store.delete_user(owner_user["id"])
    with pytest.raises(BotError) as exc:
        manager.purge_bot_files(bot_id)
    assert exc.value.code == "audit_version_retained"

    assert store.get_user(owner_bot["id"]) is not None
    assert store.get_user(owner_user["id"]) is not None
    assert store.get_bot(bot_id) is not None
    assert store.get_bot(user_bot_id) is not None
    assert store.get_bot_version(old_bot["id"])["retired_at"] is not None
    assert store.get_bot_version(old_user["id"])["retired_at"] is not None
    assert all(path.is_file() for path in protected_paths)
    assert store._conn.execute(
        "SELECT COUNT(*) FROM protocol_cutovers WHERE cutover_id='retired-delete-test'"
    ).fetchone()[0] == 1
    store.close()


def test_cutover_marker_version_without_legacy_row_blocks_all_hard_delete_paths(
    tmp_path,
):
    store = Store(str(tmp_path / "pre-version-audit.db"))
    _set_legacy_contract(store)
    owner = store.create_user("pre_version", "pre-version@example.test", "hash")
    legacy_file = tmp_path / "pre-version-legacy.elf"
    legacy_file.write_bytes(b"legacy mirror only")
    bot = store.create_bot(
        owner["id"],
        "pre_version_bot",
        game_id="gomoku",
        binary_path=str(legacy_file),
    )
    assert store.get_latest_bot_version(bot["id"]) is None
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="pre-version-audit-test")
    _prepare_cold_cutover(store)
    _apply(manager, plan)
    current = store.get_current_bot_version(bot["id"])
    assert current["version"] == 1
    assert current["retired_at"] is None
    assert store.delete_bot_if_safe(bot["id"])["references"]["audit_versions"] == 1
    assert store.delete_user_if_safe(owner["id"])["blockers"]["audit_versions"] == 1
    with pytest.raises(ValueError, match="审计证据"):
        store.delete_bot(bot["id"])
    with pytest.raises(ValueError, match="审计证据"):
        store.delete_user(owner["id"])
    with pytest.raises(ValueError, match="marker 引用"):
        store.delete_bot_version(bot["id"], current["version"])
    with pytest.raises(BotError) as purge_error:
        manager.purge_bot_files(bot["id"])
    assert purge_error.value.code == "audit_version_retained"
    assert Path(current["binary_path"]).is_file()
    store.close()


def test_store_cutover_boundary_rejects_symlinked_canonical_root(tmp_path):
    store = Store(str(tmp_path / "store-root-symlink.db"))
    _set_legacy_contract(store)
    external = tmp_path / "external-root"
    external.mkdir()
    root = tmp_path / "bot_uploads"
    root.symlink_to(external, target_is_directory=True)
    _prepare_cold_cutover(store)
    with store.offline_cutover_guard() as guard:
        with pytest.raises(ValueError, match="符号链接"):
            store.cutover_game_contract(
                cutover_id="root-symlink-test",
                game_id="gomoku",
                from_contract=game_rule_contract("gomoku", legacy=True),
                to_contract=game_rule_contract("gomoku"),
                version_manifest=[],
                canonical_binary_root=root,
                offline_guard=guard,
            )
    assert store.get_protocol_cutover("root-symlink-test") is None
    assert root.is_symlink()
    store.close()


def test_cutover_plan_preserves_runtime_modes(tmp_path):
    store = Store(str(tmp_path / "runtime-plan.db"))
    _set_legacy_contract(store)
    _legacy_bot(store, tmp_path, "runtime_traditional")
    _, long_version = _legacy_bot(store, tmp_path, "runtime_longrunning")
    with store._tx() as conn:
        conn.execute(
            "UPDATE bot_versions SET runtime_mode='longrunning' WHERE id=?",
            (long_version["id"],),
        )
        conn.execute(
            "UPDATE bots SET runtime_mode='longrunning' WHERE id=?",
            (long_version["bot_id"],),
        )
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="runtime-plan-test")
    assert plan["existing_runtime_modes"] == {
        "longrunning": 1,
        "traditional": 1,
    }
    assert plan["replacement_runtime_modes"] == {
        "longrunning": 1,
        "traditional": 1,
    }
    assert plan["runtime_mode_change_count"] == 0
    assert {
        entry["runtime_mode"] for entry in plan["version_manifest"]
    } == {"traditional", "longrunning"}
    runner = _PassingCutoverPreflight()
    _prepare_cold_cutover(store)
    result = _apply(manager, plan, binary_runner=runner)
    assert result["runtime_mode_change_count"] == 0
    assert sorted(runner.runtime_modes) == ["longrunning", "traditional"]
    assert {
        store.get_current_bot_version(entry["bot_id"])["runtime_mode"]
        for entry in plan["version_manifest"]
    } == {"traditional", "longrunning"}
    store.close()


def test_cutover_rejects_shared_inode_and_noncanonical_manifest_paths(tmp_path):
    store = Store(str(tmp_path / "manifest-paths.db"))
    _set_legacy_contract(store)
    _legacy_bot(store, tmp_path, "path_a")
    _legacy_bot(store, tmp_path, "path_b")
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="manifest-paths-test")
    raw = _standard_asset()[0].read_bytes()
    first = Path(plan["version_manifest"][0]["binary_path"])
    second = Path(plan["version_manifest"][1]["binary_path"])
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_bytes(raw)
    os.link(first, second)
    _prepare_cold_cutover(store)

    with store.offline_cutover_guard() as guard:
        with pytest.raises(ValueError, match="硬链接共享"):
            store.cutover_game_contract(
                cutover_id=plan["cutover_id"],
                game_id=plan["game_id"],
                from_contract=plan["from_contract"],
                to_contract=plan["to_contract"],
                version_manifest=plan["version_manifest"],
                canonical_binary_root=tmp_path / "bot_uploads",
                offline_guard=guard,
            )

    noncanonical = [dict(entry) for entry in plan["version_manifest"]]
    noncanonical[0]["binary_path"] = str(_standard_asset()[0])
    with store.offline_cutover_guard() as guard:
        with pytest.raises(ValueError, match="canonical"):
            store.cutover_game_contract(
                cutover_id=plan["cutover_id"],
                game_id=plan["game_id"],
                from_contract=plan["from_contract"],
                to_contract=plan["to_contract"],
                version_manifest=noncanonical,
                canonical_binary_root=tmp_path / "bot_uploads",
                offline_guard=guard,
            )
    assert store._conn.execute("SELECT COUNT(*) FROM protocol_cutovers").fetchone()[0] == 0
    store.close()


def test_cutover_requires_jobs_matches_launches_and_local_leases_to_be_idle(
    tmp_path,
):
    store = Store(str(tmp_path / "runtime-idle.db"))
    _set_legacy_contract(store)
    owner, version = _legacy_bot(store, tmp_path, "runtime_idle")
    bot = store.get_bot(version["bot_id"])
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="runtime-idle-test")
    store.executions.resume()
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner["id"],
        game_id="gomoku",
        match_type=TYPE_CHALLENGE,
        bot_a_id=bot["id"],
        bot_b_id=bot["id"],
        bot_a_version_id=version["id"],
        bot_b_version_id=version["id"],
    )
    active_match = store.create_match(
        "cutover-active-match", bot["id"], bot["id"], game_id="gomoku"
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status='starting',current_match_id=?,"
            "attempt_count=1,claimed_at='test' WHERE id=?",
            (active_match["id"], job["id"]),
        )
        conn.execute(
            "INSERT INTO execution_job_attempts(job_id,attempt_no,match_id,status,"
            "created_at) VALUES(?,1,?,'starting','test')",
            (job["id"], active_match["id"]),
        )
    _prepare_cold_cutover(store)
    with pytest.raises(ValueError, match="jobs=1.*attempts=1.*matches=1"):
        _apply(manager, plan)
    assert not Path(plan["version_manifest"][0]["binary_path"]).exists()

    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_job_attempts SET status='interrupted',terminal_at='test',"
            "terminal_reason='test' WHERE job_id=?",
            (job["id"],),
        )
        conn.execute(
            "UPDATE execution_jobs SET status='interrupted',retryable=1,"
            "terminal_at='test',terminal_reason='test' WHERE id=?",
            (job["id"],),
        )
    store.update_match(
        active_match["id"],
        status=STATUS_ABORTED,
        reason="platform_error",
        ended_at="test",
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE docker_launch_journal SET state='creating',launch_token='token',"
            "instance_key='test',owner_kind='preflight',job_public_id='preflight',"
            "attempt_no=1,slot=0,container_name='test',host_boot_id='test',"
            "updated_at='test' WHERE singleton=1"
        )
    with pytest.raises(ValueError, match="launch journal"):
        _apply(manager, plan)
    with store._tx() as conn:
        conn.execute(
            "UPDATE docker_launch_journal SET state='idle',launch_token=NULL,"
            "instance_key=NULL,owner_kind=NULL,job_public_id=NULL,attempt_no=NULL,"
            "slot=NULL,container_name=NULL,host_boot_id=NULL,updated_at='test' "
            "WHERE singleton=1"
        )

    agent = store.create_local_ai_agent(
        owner_id=owner["id"],
        bot_id=bot["id"],
        label="runtime idle agent",
        public_id="lia_runtime_idle",
        token_hash="hash",
        token_hint="hint",
    )
    with store._tx() as conn:
        conn.execute(
            "INSERT INTO local_ai_leases(agent_id,job_public_id,attempt_no,seat,"
            "acquired_at) VALUES(?,?,1,0,'test')",
            (agent["id"], job["public_id"]),
        )
    with pytest.raises(ValueError, match="local_leases=1"):
        _apply(manager, plan)
    with store._tx() as conn:
        conn.execute(
            "UPDATE local_ai_leases SET status='released',released_at='test',"
            "terminal_reason='test' WHERE status='active'"
        )
    assert _apply(manager, plan)["already_applied"] is False
    store.close()


def test_asgi_lifespan_rejects_legacy_contract_before_dispatcher_start(
    tmp_path, monkeypatch
):
    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "legacy-startup.db"))
    _set_legacy_contract(app.state.store)
    called = {"dispatcher_start": 0, "dispatcher_flock": 0}

    def forbidden_flock():
        called["dispatcher_flock"] += 1
        raise AssertionError("legacy dispatcher must fail before its flock")

    monkeypatch.setattr(
        app.state.execution_dispatcher, "_acquire_singleton", forbidden_flock
    )
    with pytest.raises(RuntimeError, match="拒绝启动在线 runtime.*离线"):
        asyncio.run(app.state.execution_dispatcher.start())
    assert called["dispatcher_flock"] == 0

    async def forbidden_start():
        called["dispatcher_start"] += 1
        raise AssertionError("dispatcher must not start for a legacy contract")

    monkeypatch.setattr(app.state.execution_dispatcher, "start", forbidden_start)

    async def enter_lifespan():
        async with app.router.lifespan_context(app):
            raise AssertionError("legacy runtime must not yield startup")

    with pytest.raises(RuntimeError, match="拒绝启动在线 runtime.*离线"):
        asyncio.run(enter_lifespan())
    assert called["dispatcher_start"] == 0
    assert called["dispatcher_flock"] == 0
    app.state.store.close()


def test_cutover_postconditions_reject_marker_bot_rating_contract_and_job_drift(
    tmp_path,
):
    store = Store(str(tmp_path / "postcondition-drift.db"))
    _set_legacy_contract(store)
    owner, old = _legacy_bot(store, tmp_path, "postcondition_drift")
    bot = store.get_bot(old["bot_id"])
    store.executions.resume()
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=owner["id"],
        game_id="gomoku",
        match_type=TYPE_CHALLENGE,
        bot_a_id=bot["id"],
        bot_b_id=bot["id"],
        bot_a_version_id=old["id"],
        bot_b_version_id=old["id"],
    )
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="postcondition-drift-test")
    _prepare_cold_cutover(store)
    _apply(manager, plan)
    current = store.get_current_bot_version(bot["id"])

    def rejected() -> None:
        with pytest.raises(RuntimeError, match="cutover postcondition drift"):
            store.assert_protocol_cutover_postconditions()

    with store._tx() as conn:
        conn.execute(
            "UPDATE protocol_cutovers SET retired_count=retired_count+1 "
            "WHERE cutover_id=?",
            (plan["cutover_id"],),
        )
    rejected()
    with store._tx() as conn:
        conn.execute(
            "UPDATE protocol_cutovers SET retired_count=retired_count-1 "
            "WHERE cutover_id=?",
            (plan["cutover_id"],),
        )

    with store._tx() as conn:
        conn.execute(
            "UPDATE bots SET current_version=1 WHERE id=?", (bot["id"],)
        )
    rejected()
    with store._tx() as conn:
        conn.execute(
            "UPDATE bots SET current_version=? WHERE id=?",
            (current["version"], bot["id"]),
        )

    with store._tx() as conn:
        conn.execute(
            "UPDATE bot_versions SET retired_at='drift' WHERE id=?",
            (current["id"],),
        )
    rejected()
    with store._tx() as conn:
        conn.execute(
            "UPDATE bot_versions SET retired_at=NULL WHERE id=?", (current["id"],)
        )

    with store._tx() as conn:
        conn.execute(
            "UPDATE ratings SET rating=1601 WHERE bot_id=? AND game_id='gomoku'",
            (bot["id"],),
        )
    rejected()
    with store._tx() as conn:
        conn.execute(
            "UPDATE ratings SET rating=1500 WHERE bot_id=? AND game_id='gomoku'",
            (bot["id"],),
        )
    _certify_projection(store)

    with store._tx() as conn:
        conn.execute(
            "UPDATE rating_pool_state SET active_pool_id=?,ruleset_version=?,"
            "protocol_version=? WHERE game_id='gomoku'",
            (
                GOMOKU_LEGACY_RATING_POOL,
                GOMOKU_LEGACY_RULESET,
                GOMOKU_LEGACY_PROTOCOL,
            ),
        )
    rejected()
    with store._tx() as conn:
        conn.execute(
            "UPDATE rating_pool_state SET active_pool_id=?,ruleset_version=?,"
            "protocol_version=? WHERE game_id='gomoku'",
            (
                GOMOKU_CURRENT_RATING_POOL,
                GOMOKU_CURRENT_RULESET,
                GOMOKU_CURRENT_PROTOCOL,
            ),
        )

    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status='queued',retryable=1,"
            "current_match_id=NULL,claimed_at=NULL,started_at=NULL,settling_at=NULL,"
            "terminal_at=NULL,cleanup_state='none' WHERE id=?",
            (job["id"],),
        )
    rejected()
    with store._tx() as conn:
        conn.execute(
            "UPDATE execution_jobs SET status='cancelled',retryable=0,terminal_at='test' "
            "WHERE id=?",
            (job["id"],),
        )
    store.assert_runtime_contracts_current()
    store.close()


def test_orchestrator_rejects_stale_match_contract_before_runner_start(tmp_path):
    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    class NeverStartRunner:
        def __init__(self):
            self.calls = 0
            self.action_timeout = 1.0

        async def run_binaries(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("stale persisted Match must not start a Bot")

    store = Store(str(tmp_path / "stale-match.db"))
    _set_legacy_contract(store)
    _, version_a = _legacy_bot(store, tmp_path, "stale_match_a")
    _, version_b = _legacy_bot(store, tmp_path, "stale_match_b")
    stale = store.create_match(
        "stale-contract-match",
        version_a["bot_id"],
        version_b["bot_id"],
        game_id="gomoku",
    )
    runner = NeverStartRunner()
    orchestrator = MatchOrchestrator(store, runner=runner, max_concurrent=1)
    asyncio.run(
        orchestrator._MatchOrchestrator__run_match_inner(stale["id"])
    )
    after = store.get_match(stale["id"])
    assert after["status"] == STATUS_ABORTED
    assert after["reason"] == "invalid_match_config"
    assert runner.calls == 0
    store.close()


def test_orchestrator_rejects_current_match_that_freezes_retired_versions(
    tmp_path,
):
    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    class NeverStartRunner:
        def __init__(self):
            self.calls = 0
            self.action_timeout = 1.0

        async def run_binaries(self, *_args, **_kwargs):
            self.calls += 1
            raise AssertionError("retired binary must never start")

    store = Store(str(tmp_path / "retired-match-version.db"))
    _set_legacy_contract(store)
    _, old_a = _legacy_bot(store, tmp_path, "retired_match_a")
    _, old_b = _legacy_bot(store, tmp_path, "retired_match_b")
    manager = _manager(store, tmp_path)
    plan = _plan(manager, cutover_id="retired-match-version-test")
    _prepare_cold_cutover(store)
    _apply(manager, plan)

    match = store.create_match(
        "current-contract-retired-versions",
        old_a["bot_id"],
        old_b["bot_id"],
        game_id="gomoku",
        match_config={
            "_bot_a_version_id": old_a["id"],
            "_bot_b_version_id": old_b["id"],
        },
    )
    runner = NeverStartRunner()
    orchestrator = MatchOrchestrator(store, runner=runner, max_concurrent=1)
    asyncio.run(
        orchestrator._MatchOrchestrator__run_match_inner(match["id"])
    )
    after = store.get_match(match["id"])
    assert after["status"] == STATUS_ABORTED
    assert after["reason"] == "invalid_match_config"
    assert runner.calls == 0
    store.close()


def test_persisted_current_contracts_match_registered_game_specs():
    from bzplat.backend.games import registry

    for game_id in registry.all_ids():
        spec = registry.get(game_id)
        assert game_rule_contract(game_id) == {
            "ruleset_version": spec.ruleset_id,
            "protocol_version": spec.protocol_version,
            "rating_pool_id": spec.rating_pool_id,
        }


def test_local_ai_identity_requires_agent_bot_and_active_protocol_alignment(
    tmp_path,
):
    store = Store(str(tmp_path / "local-ai-contract.db"))
    owner = store.create_user("local_contract", "local-contract@example.test", "hash")
    bot = store.create_bot(owner["id"], "local_contract_bot", game_id="gomoku")
    with store._tx() as conn:
        conn.execute(
            "UPDATE bots SET protocol_version=? WHERE id=?",
            (GOMOKU_LEGACY_PROTOCOL, bot["id"]),
        )
    with pytest.raises(ValueError, match="当前游戏契约"):
        store.create_local_ai_agent(
            owner_id=owner["id"],
            bot_id=bot["id"],
            label="legacy create",
            public_id="lia_legacy_create",
            token_hash="legacy-create-hash",
            token_hint="hint",
        )

    with store._tx() as conn:
        conn.execute(
            "UPDATE bots SET protocol_version=? WHERE id=?",
            (GOMOKU_CURRENT_PROTOCOL, bot["id"]),
        )
    agent = store.create_local_ai_agent(
        owner_id=owner["id"],
        bot_id=bot["id"],
        label="current agent",
        public_id="lia_current_agent",
        token_hash="current-agent-hash",
        token_hint="hint",
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE local_ai_agents SET protocol_version=? WHERE id=?",
            (GOMOKU_LEGACY_PROTOCOL, agent["id"]),
        )
    assert store.connect_local_ai_agent(
        agent["id"], expected_public_id=agent["public_id"]
    ) is None

    with store._tx() as conn:
        conn.execute(
            "UPDATE local_ai_agents SET protocol_version=? WHERE id=?",
            (GOMOKU_CURRENT_PROTOCOL, agent["id"]),
        )
    connected = store.connect_local_ai_agent(
        agent["id"], expected_public_id=agent["public_id"]
    )
    assert connected is not None
    generation = int(connected["connection_generation"])
    with store._tx() as conn:
        conn.execute(
            "UPDATE bots SET protocol_version=? WHERE id=?",
            (GOMOKU_LEGACY_PROTOCOL, bot["id"]),
        )
    assert store.touch_local_ai_agent(agent["id"], generation) is False
    assert store.local_ai_connection_still_authorized(
        agent["id"], generation
    ) is False
    store.close()


def test_new_contest_freezes_the_full_current_contract(tmp_path):
    store = Store(str(tmp_path / "contest-contract.db"))
    owner = store.create_user("contest_owner", "contest@example.test", "hash")
    contest = store.create_contest(
        "current gomoku contest",
        owner["id"],
        game_id="gomoku",
        template_id="gomoku_round_robin",
    )
    assert {
        "ruleset_version": contest["ruleset_version"],
        "protocol_version": contest["protocol_version"],
        "rating_pool_id": contest["rating_pool_id"],
    } == game_rule_contract("gomoku")
    store.close()
