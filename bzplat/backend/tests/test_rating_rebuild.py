"""Offline rating projection rebuild and settlement-order invariants."""
from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from bzplat.backend.cli import app as cli_app
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.bots.manager import BotError, BotManager
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.rating import rebuild as rebuild_module
from bzplat.backend.rating.rebuild import (
    _rating_diff,
    apply_rebuild_plan,
    build_rebuild_plan,
)
from bzplat.backend.store import (
    Store,
    rating_projection_digest,
    rating_projection_digests,
)


def _apply_reviewed(db, plan, backup):
    return apply_rebuild_plan(
        db,
        plan.report["source_digest"],
        plan.report["plan_digest"],
        plan.report["rebuilt_projection_digest"],
        confirmed_database=db,
        backup_path=backup,
        service_stopped=True,
        cold_backup_confirmed=True,
    )


def _mark_projection_verified(store: Store) -> None:
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        live = rating_projection_digests(conn)
        assert live["issues"] == []
        conn.execute(
            "UPDATE rating_projection_state SET policy_version='owner-neutral-v3',"
            "source_settlement_count=?,source_last_settled_order=?,source_digest=?,"
            "projection_digest=?,plan_digest=?,"
            "trusted_mutation_revision=mutation_revision WHERE singleton=1",
            (
                live["source_settlement_count"],
                live["source_last_settled_order"],
                live["source_digest"],
                live["projection_digest"],
                live["plan_digest"],
            ),
        )


def test_fresh_store_certifies_empty_projection_and_keeps_it_current(tmp_path):
    store = Store(str(tmp_path / "fresh-projection.db"))
    initial = store.rating_projection_status()
    assert initial["ready"] is True
    assert initial["state"]["policy_version"] == "owner-neutral-v3"

    _bot(store, "fresh-projection")
    after_bot = store.rating_projection_status()
    assert after_bot["ready"] is True
    assert after_bot["state"]["mutation_revision"] == (
        after_bot["state"]["trusted_mutation_revision"]
    )
    store.close()


def test_existing_empty_store_is_never_silently_recertified(tmp_path):
    db = tmp_path / "existing-empty-projection.db"
    store = Store(str(db))
    assert store.rating_projection_status()["ready"] is True
    with store._tx() as conn:
        conn.execute(
            "UPDATE rating_projection_state SET policy_version='legacy-unverified',"
            "source_digest='',projection_digest='',plan_digest='' WHERE singleton=1"
        )
    store.close()

    reopened = Store(str(db))
    status = reopened.rating_projection_status()
    assert status["ready"] is False
    assert status["state"]["policy_version"] == "legacy-unverified"
    reopened.close()


def _bot(
    store: Store,
    username: str,
    *,
    owner_id: int | None = None,
    binary_dir: Path | None = None,
) -> dict:
    if owner_id is None:
        owner = store.create_user(
            username, f"{username}@example.com", hash_password("password1")
        )
        owner_id = int(owner["id"])
    path = (
        binary_dir / f"{username}.elf"
        if binary_dir is not None
        else Path(f"/tmp/{username}.elf")
    )
    if binary_dir is not None:
        path.write_bytes(f"fixture-{username}".encode())
    binary_path = str(path)
    bot = store.create_bot(
        owner_id,
        f"bot-{username}",
        binary_path=binary_path,
        format="elf",
        game_id="gomoku",
    )
    store.add_bot_version(bot["id"], binary_path=binary_path)
    store.ensure_rating(bot["id"], game_id="gomoku")
    return bot


def _complete(
    store: Store,
    orch: MatchOrchestrator,
    match_id: str,
    bot_a: int,
    bot_b: int,
    *,
    winner: int,
    ended_at: str,
) -> None:
    store.create_match(match_id, bot_a, bot_b, game_id="gomoku")
    deltas = [1, -1] if winner == 0 else [-1, 1]
    store.update_match(
        match_id,
        status="completed",
        winner=winner,
        result={"rounds_played": 1, "deltas": deltas, "normalized_delta": deltas[0]},
        ended_at=ended_at,
    )
    # This low-level call intentionally models the legacy bug: it rates a pair
    # even when the frozen v2 policy says same_owner is neutral.
    assert orch._apply_ratings(
        bot_a,
        bot_b,
        winner,
        deltas[0],
        deltas[1],
        reason=match_id,
        settlement_id=match_id,
        game_id="gomoku",
    )


def test_hard_delete_rejects_completed_unsettled_rated_lifecycle(tmp_path):
    """Admin hard delete cannot erase a Bot between completion and settlement."""
    store = Store(str(tmp_path / "delete-unsettled.db"))
    bot_a = _bot(store, "delete-unsettled-a")
    bot_b = _bot(store, "delete-unsettled-b")
    store.create_match(
        "delete-unsettled-match",
        bot_a["id"],
        bot_b["id"],
        game_id="gomoku",
    )
    store.update_match(
        "delete-unsettled-match",
        status="completed",
        winner=0,
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
        ended_at="2026-08-10T09:59:00",
    )

    result = store.delete_bot_if_safe(bot_a["id"])

    assert result == {
        "found": True,
        "deleted": False,
        "references": {"matches": 1, "pairings": 0, "audit_versions": 0},
    }
    assert store.get_bot(bot_a["id"]) is not None
    assert store.is_match_rating_settled("delete-unsettled-match") is False
    store.close()


def test_rebuild_dry_run_apply_verify_is_source_preserving_and_idempotent(tmp_path):
    db = (tmp_path / "ratings.db").resolve()
    store = Store(str(db))
    owner = store.create_user(
        "same-owner", "same-owner@example.com", hash_password("password1")
    )
    bot_a = _bot(store, "same-a", owner_id=owner["id"])
    bot_b = _bot(store, "same-b", owner_id=owner["id"])
    bot_c = _bot(store, "other")
    # This fixture models an upgraded v2 database whose old settlement path
    # rated same-owner matches.  A genuinely fresh database is certified now,
    # so explicitly restore the legacy trust boundary before injecting that
    # historical corruption.
    with store._tx() as conn:
        conn.execute(
            "UPDATE rating_projection_state SET policy_version='legacy-unverified',"
            "source_digest='',projection_digest='',plan_digest='' WHERE singleton=1"
        )
    orch = MatchOrchestrator(store)
    _complete(
        store, orch, "eligible-1", bot_a["id"], bot_c["id"],
        winner=0, ended_at="2026-08-10T10:00:00",
    )
    _complete(
        store, orch, "legacy-same-owner", bot_a["id"], bot_b["id"],
        winner=0, ended_at="2026-08-10T10:01:00",
    )
    _complete(
        store, orch, "eligible-2", bot_b["id"], bot_c["id"],
        winner=1, ended_at="2026-08-10T10:02:00",
    )
    with pytest.raises(sqlite3.IntegrityError, match="policy source immutable"):
        with store._tx() as conn:
            conn.execute(
                "UPDATE match_rating_policies SET source='tampered' "
                "WHERE match_id='legacy-same-owner'"
            )
    with store._tx() as conn:
        settlements_before = [
            tuple(row)
            for row in conn.execute(
                "SELECT match_id,settled_at,settled_order "
                "FROM match_rating_settlements WHERE settled_order>0 "
                "ORDER BY settled_order"
            )
        ]
    store.close()

    plan = build_rebuild_plan(db)
    assert plan.report["source_settlement_count"] == 3
    assert plan.report["rated_source_count"] == 2
    assert plan.report["neutral_source_count"] == 1
    assert plan.report["projection_matches"] is False
    assert plan.report["changed_bot_count"] >= 2
    assert plan.report["projection_state_current"] is False
    with sqlite3.connect(db) as digest_conn:
        digest_conn.row_factory = sqlite3.Row
        live = rating_projection_digests(digest_conn)
    assert plan.report["source_digest"] == live["source_digest"]
    assert plan.report["plan_digest"] == live["plan_digest"]
    assert plan.report["current_projection_digest"] == live["projection_digest"]
    assert plan.report["rebuilt_projection_digest"] == rating_projection_digest(
        plan.ratings, plan.history, plan.pairs
    )

    with sqlite3.connect(db) as fault_conn:
        fault_conn.execute(
            "CREATE TRIGGER fail_rating_rebuild_history "
            "BEFORE INSERT ON rating_history BEGIN "
            "SELECT RAISE(ABORT,'rebuild fault injection'); END"
        )
    backup = (tmp_path / "ratings.fault.cold-backup.db").resolve()
    shutil.copy2(db, backup)
    with pytest.raises(sqlite3.IntegrityError, match="rebuild fault injection"):
        _apply_reviewed(db, plan, backup)
    after_fault = build_rebuild_plan(db)
    assert after_fault.report["current_projection_hash"] == plan.report["current_projection_hash"]
    assert after_fault.report["projection_state_current"] is False
    with sqlite3.connect(db) as fault_conn:
        fault_conn.execute("DROP TRIGGER fail_rating_rebuild_history")

    backup = (tmp_path / "ratings.cold-backup.db").resolve()
    shutil.copy2(db, backup)
    applied = _apply_reviewed(db, plan, backup)
    assert applied["verified_after_apply"] is True
    assert applied["applied"] is True
    assert applied["no_op"] is False
    assert applied["changed_bot_count"] == 0
    verified = build_rebuild_plan(db)
    assert verified.report["projection_matches"] is True
    assert verified.report["projection_state_current"] is True

    reopened = Store(str(db))
    with reopened._tx() as conn:
        settlements_after = [
            tuple(row)
            for row in conn.execute(
                "SELECT match_id,settled_at,settled_order "
                "FROM match_rating_settlements WHERE settled_order>0 "
                "ORDER BY settled_order"
            )
        ]
        assert conn.execute("SELECT COUNT(*) FROM rating_history").fetchone()[0] == 4
        assert conn.execute("SELECT SUM(samples) FROM pair_stats").fetchone()[0] == 2
    assert settlements_after == settlements_before
    reopened.close()

    no_op_backup = (tmp_path / "ratings.no-op.cold-backup.db").resolve()
    shutil.copy2(db, no_op_backup)
    before_no_op_bytes = db.read_bytes()
    before_no_op_mtime = db.stat().st_mtime_ns
    with sqlite3.connect(db) as state_conn:
        rebuilt_at_before = state_conn.execute(
            "SELECT rebuilt_at FROM rating_projection_state WHERE singleton=1"
        ).fetchone()[0]
    repeated = _apply_reviewed(db, verified, no_op_backup)
    assert repeated["verified_after_apply"] is True
    assert repeated["no_op"] is True
    assert repeated["applied"] is False
    assert repeated["rows_written"] == 0
    assert db.read_bytes() == before_no_op_bytes
    assert db.stat().st_mtime_ns == before_no_op_mtime
    with sqlite3.connect(db) as state_conn:
        assert state_conn.execute(
            "SELECT rebuilt_at FROM rating_projection_state WHERE singleton=1"
        ).fetchone()[0] == rebuilt_at_before
    assert (
        repeated["rebuilt_projection_digest"]
        == verified.report["rebuilt_projection_digest"]
    )


def test_completed_matches_freeze_recovery_order_before_settlement(tmp_path):
    store = Store(str(tmp_path / "order.db"))
    bots = [_bot(store, f"order-{index}") for index in range(4)]
    for match_id, ended_at, bot_a, bot_b in (
        ("lexically-later", "2026-08-10T11:00:00", bots[0], bots[1]),
        ("lexically-earlier", "2026-08-10T10:00:00", bots[2], bots[3]),
    ):
        store.create_match(match_id, bot_a["id"], bot_b["id"], game_id="gomoku")
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            result={"rounds_played": 1, "deltas": [1, -1]},
            ended_at=ended_at,
        )
    pending = store.list_unsettled_completed_rating_matches()
    assert [row["id"] for row in pending] == [
        "lexically-later", "lexically-earlier"
    ]
    assert [row["_rating_settled_order"] for row in pending] == [1, 2]
    store.close()


def _complete_unsettled(
    store: Store, match_id: str, bot_a: int, bot_b: int, *, winner: int = 0
) -> None:
    deltas = [1, -1] if winner == 0 else [-1, 1]
    store.create_match(match_id, bot_a, bot_b, game_id="gomoku")
    store.update_match(
        match_id,
        status="completed",
        winner=winner,
        result={
            "rounds_played": 1,
            "deltas": deltas,
            "normalized_delta": deltas[0],
        },
        ended_at="2026-08-10T11:30:00",
    )


def test_projection_mutation_guard_accepts_verified_reserved_settlement(tmp_path):
    db = str(tmp_path / "trusted-settlement.db")
    store = Store(db)
    bot_a = _bot(store, "trusted-settlement-a")
    bot_b = _bot(store, "trusted-settlement-b")
    _mark_projection_verified(store)
    _complete_unsettled(
        store, "trusted-settlement", bot_a["id"], bot_b["id"]
    )

    assert store.rating_projection_status()["ready"] is False
    store.close()
    store = Store(db)
    orch = MatchOrchestrator(store)
    assert orch._apply_ratings(
        bot_a["id"], bot_b["id"], 0, 1, -1,
        reason="trusted-settlement", settlement_id="trusted-settlement",
        game_id="gomoku",
    )
    assert store.rating_projection_status()["ready"] is True
    store.close()


@pytest.mark.parametrize(
    "mutation", ["ensure", "rated", "selfplay", "neutral"]
)
def test_projection_mutation_guard_never_revalidates_stale_state(
    tmp_path, mutation
):
    store = Store(str(tmp_path / f"stale-{mutation}.db"))
    bot_a = _bot(store, f"stale-{mutation}-a")
    bot_b = _bot(store, f"stale-{mutation}-b")
    if mutation == "neutral":
        bot_b = _bot(
            store,
            "stale-neutral-same-owner",
            owner_id=store.get_bot(bot_a["id"])["owner_id"],
        )
    _mark_projection_verified(store)

    if mutation == "ensure":
        with store._tx() as conn:
            conn.execute(
                "DELETE FROM ratings WHERE bot_id=? AND game_id='gomoku'",
                (bot_a["id"],),
            )
        assert store.rating_projection_status()["ready"] is False
        store.ensure_rating(bot_a["id"], game_id="gomoku")
    else:
        match_id = f"stale-{mutation}"
        if mutation == "selfplay":
            match_id = "stale-selfplay"
            _complete_unsettled(store, match_id, bot_a["id"], bot_a["id"])
        else:
            _complete_unsettled(store, match_id, bot_a["id"], bot_b["id"])
        with store._tx() as conn:
            conn.execute(
                "UPDATE rating_projection_state SET projection_digest='stale' "
                "WHERE singleton=1"
            )
        if mutation == "neutral":
            assert store.mark_match_rating_settled(match_id)
        else:
            orch = MatchOrchestrator(store)
            assert orch._apply_ratings(
                bot_a["id"],
                bot_a["id"] if mutation == "selfplay" else bot_b["id"],
                0, 1, -1,
                reason=match_id, settlement_id=match_id, game_id="gomoku",
            )

    assert store.rating_projection_status()["ready"] is False
    store.close()


def test_normal_bot_publish_and_visibility_updates_keep_projection_ready(tmp_path):
    store = Store(str(tmp_path / "bot-publish.db"))
    owner = store.create_user(
        "projection-publisher",
        "projection-publisher@example.com",
        hash_password("password1"),
    )
    _mark_projection_verified(store)
    sample = (
        Path(__file__).resolve().parents[3]
        / "samples"
        / "gomokubot_linux_amd64"
    ).read_bytes()
    manager = BotManager(store, upload_root=tmp_path / "uploads")

    bot = manager.create_from_upload(
        owner["id"], "projection_bot", sample, game_id="gomoku"
    )
    assert store.get_rating(bot["id"], game_id="gomoku") is not None
    assert store.rating_projection_status()["ready"] is True
    manager.set_active(bot["id"], owner["id"], False)
    assert store.rating_projection_status()["ready"] is True
    manager.set_active(bot["id"], owner["id"], True)
    assert store.rating_projection_status()["ready"] is True
    store.close()


def test_failed_bot_preflight_rolls_back_staging_projection(
    monkeypatch, tmp_path
):
    store = Store(str(tmp_path / "bot-preflight-rollback.db"))
    owner = store.create_user(
        "projection-rejected",
        "projection-rejected@example.com",
        hash_password("password1"),
    )
    _mark_projection_verified(store)
    sample = (
        Path(__file__).resolve().parents[3]
        / "samples"
        / "gomokubot_linux_amd64"
    ).read_bytes()
    manager = BotManager(store, upload_root=tmp_path / "uploads")
    monkeypatch.setattr(
        manager, "_run_preflight", lambda *args, **kwargs: (False, "rejected")
    )

    with pytest.raises(BotError, match="预检失败"):
        manager.create_from_upload(
            owner["id"], "projection_rejected_bot", sample,
            game_id="gomoku", binary_runner=object(),
        )

    assert store.get_bot_by_owner_name(
        owner["id"], "projection_rejected_bot"
    ) is None
    assert store.rating_projection_status()["ready"] is True
    store.close()


@pytest.mark.parametrize("mutation", ["game_id", "hard_delete", "legacy_rating"])
def test_unreviewed_projection_mutations_remain_fail_closed(tmp_path, mutation):
    store = Store(str(tmp_path / f"fail-closed-{mutation}.db"))
    bot_a = _bot(store, f"fail-closed-{mutation}-a")
    bot_b = _bot(store, f"fail-closed-{mutation}-b")
    _mark_projection_verified(store)

    if mutation == "game_id":
        store.update_bot(bot_a["id"], game_id="holdem")
    elif mutation == "hard_delete":
        assert store.delete_bot(bot_a["id"])
    else:
        assert store.apply_match_ratings_atomic(
            bot_a["id"], bot_b["id"], game_id="gomoku",
            rating_a=(1510.0, 340.0, 0.06),
            rating_b=(1490.0, 340.0, 0.06),
            winner=0, delta_a=1, delta_b=-1,
            reason="legacy-no-marker", settlement_id=None,
        )

    assert store.rating_projection_status()["ready"] is False
    store.close()


def test_v2_projection_state_upgrade_requires_v3_offline_rebuild(tmp_path):
    db = (tmp_path / "projection-v2-upgrade.db").resolve()
    store = Store(str(db))
    _bot(store, "projection-v2-upgrade")
    _mark_projection_verified(store)
    assert store.rating_projection_status()["ready"] is True
    store.close()

    # Recreate the exact pre-lineage state table: matching v2 summaries but no
    # mutation revision columns.  This is indistinguishable from a state that
    # the retired blind refresh incorrectly marked current.
    with sqlite3.connect(db) as legacy:
        trigger_names = [
            str(row[0])
            for row in legacy.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' "
                "AND instr(COALESCE(sql,''),'mutation_revision')>0"
            )
        ]
        for name in trigger_names:
            assert name.replace("_", "").isalnum()
            legacy.execute(f"DROP TRIGGER {name}")
        legacy.execute(
            "ALTER TABLE rating_projection_state "
            "DROP COLUMN trusted_mutation_revision"
        )
        legacy.execute(
            "ALTER TABLE rating_projection_state DROP COLUMN mutation_revision"
        )
        legacy.execute(
            "UPDATE rating_projection_state SET policy_version='owner-neutral-v2' "
            "WHERE singleton=1"
        )
        columns = {
            str(row[1])
            for row in legacy.execute("PRAGMA table_info(rating_projection_state)")
        }
        assert "mutation_revision" not in columns

    upgraded = Store(str(db))
    status = upgraded.rating_projection_status()
    assert status["ready"] is False
    assert status["required_policy_version"] == "owner-neutral-v3"
    assert status["state"]["policy_version"] == "owner-neutral-v2"
    assert status["state"]["mutation_revision"] == 0
    assert status["state"]["trusted_mutation_revision"] == 0
    upgraded.close()

    plan = build_rebuild_plan(db)
    backup = (tmp_path / "projection-v2-upgrade.cold.db").resolve()
    shutil.copy2(db, backup)
    applied = _apply_reviewed(db, plan, backup)
    assert applied["verified_after_apply"] is True
    assert applied["applied"] is True

    rebuilt = Store(str(db))
    rebuilt_status = rebuilt.rating_projection_status()
    assert rebuilt_status["ready"] is True
    assert rebuilt_status["state"]["policy_version"] == "owner-neutral-v3"
    assert rebuilt_status["state"]["mutation_revision"] == (
        rebuilt_status["state"]["trusted_mutation_revision"]
    )
    rebuilt.close()


def test_rating_rebuild_cli_defaults_readonly_and_gates_apply(tmp_path):
    db = (tmp_path / "cli.db").resolve()
    store = Store(str(db))
    store.close()
    backup = (tmp_path / "cli.cold.db").resolve()
    shutil.copy2(db, backup)
    runner = CliRunner()
    before_bytes = db.read_bytes()
    before_mtime = db.stat().st_mtime_ns

    dry = runner.invoke(cli_app, ["rating-rebuild", "--db", str(db)])
    assert dry.exit_code == 0
    report = json.loads(dry.stdout)
    assert report["mode"] == "dry-run"
    assert report["projection_state_current"] is True
    assert len(report["source_digest"]) == 64
    assert len(report["plan_digest"]) == 64
    assert len(report["rebuilt_projection_digest"]) == 64
    assert db.read_bytes() == before_bytes
    assert db.stat().st_mtime_ns == before_mtime

    denied = runner.invoke(
        cli_app,
        [
            "rating-rebuild", "--db", str(db), "--apply",
            "--expect-source-digest", report["source_digest"],
        ],
    )
    assert denied.exit_code != 0

    applied = runner.invoke(
        cli_app,
        [
            "rating-rebuild", "--db", str(db), "--apply",
            "--expect-source-digest", report["source_digest"],
            "--expect-plan-digest", report["plan_digest"],
            "--expect-rebuilt-projection-digest",
            report["rebuilt_projection_digest"],
            "--confirm-db", str(db), "--backup", str(backup),
            "--confirm-service-stopped", "--confirm-cold-backup",
        ],
    )
    assert applied.exit_code == 0, applied.stdout
    assert json.loads(applied.stdout)["no_op"] is True
    verified = runner.invoke(
        cli_app, ["rating-rebuild", "--db", str(db), "--verify"]
    )
    assert verified.exit_code == 0


def test_apply_rejects_source_change_after_reviewed_digest(tmp_path):
    db = (tmp_path / "source-change.db").resolve()
    store = Store(str(db))
    bots = [_bot(store, f"source-{index}") for index in range(3)]
    orch = MatchOrchestrator(store)
    _complete(
        store, orch, "source-before", bots[0]["id"], bots[1]["id"],
        winner=0, ended_at="2026-08-10T12:00:00",
    )
    store.close()
    reviewed = build_rebuild_plan(db)
    backup = (tmp_path / "source-change.cold.db").resolve()
    shutil.copy2(db, backup)

    changed = Store(str(db))
    changed_orch = MatchOrchestrator(changed)
    _complete(
        changed, changed_orch, "source-after", bots[1]["id"], bots[2]["id"],
        winner=1, ended_at="2026-08-10T12:01:00",
    )
    changed.close()
    with pytest.raises(RuntimeError, match="评分重建摘要已变化"):
        _apply_reviewed(db, reviewed, backup)


def test_deleted_bot_is_replayed_in_memory_but_not_written_to_fk_tables(tmp_path):
    db = (tmp_path / "deleted-bot.db").resolve()
    store = Store(str(db))
    bot_a, deleted, bot_c = [_bot(store, f"deleted-{index}") for index in range(3)]
    orch = MatchOrchestrator(store)
    _complete(
        store, orch, "deleted-first", bot_a["id"], deleted["id"],
        winner=0, ended_at="2026-08-10T13:00:00",
    )
    _complete(
        store, orch, "deleted-second", deleted["id"], bot_c["id"],
        winner=0, ended_at="2026-08-10T13:01:00",
    )
    assert store.delete_bot(deleted["id"]) is True
    store.close()

    plan = build_rebuild_plan(db)
    rebuilt_ids = {int(row["bot_id"]) for row in plan.ratings}
    assert rebuilt_ids == {bot_a["id"], bot_c["id"]}
    rebuilt_c = next(row for row in plan.ratings if row["bot_id"] == bot_c["id"])
    assert rebuilt_c["matches_played"] == 1
    assert rebuilt_c["rating"] != 1500.0
    assert {row["reason"] for row in plan.history} == {
        "deleted-first", "deleted-second"
    }
    assert plan.pairs == []
    assert plan.report["projection_matches"] is True


def test_rebuild_keeps_only_latest_200_history_rows_per_bot(tmp_path):
    db = (tmp_path / "history-cap.db").resolve()
    store = Store(str(db))
    bot_a, bot_b = [_bot(store, f"history-{index}") for index in range(2)]
    orch = MatchOrchestrator(store)
    for index in range(205):
        _complete(
            store,
            orch,
            f"history-{index:03d}",
            bot_a["id"],
            bot_b["id"],
            winner=index % 2,
            ended_at=f"2026-08-10T14:{index // 60:02d}:{index % 60:02d}",
        )
    store.close()

    plan = build_rebuild_plan(db)
    for bot_id in (bot_a["id"], bot_b["id"]):
        rows = [row for row in plan.history if row["bot_id"] == bot_id]
        assert len(rows) == 200
        assert rows[0]["reason"] == "history-005"
        assert rows[-1]["reason"] == "history-204"
    assert plan.report["projection_matches"] is True


def test_dry_run_uses_one_read_snapshot_for_source_plan_and_projection(
    monkeypatch, tmp_path
):
    db = (tmp_path / "single-snapshot.db").resolve()
    store = Store(str(db))
    bot = _bot(store, "snapshot-bot")
    store.close()
    with sqlite3.connect(db) as conn:
        assert conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"

    original_load_source = rebuild_module._load_source
    wrote = False

    def load_then_mutate(conn):
        nonlocal wrote
        result = original_load_source(conn)
        if not wrote:
            wrote = True
            with sqlite3.connect(db) as writer:
                writer.execute(
                    "UPDATE bots SET is_active=0 WHERE id=?", (bot["id"],)
                )
        return result

    monkeypatch.setattr(rebuild_module, "_load_source", load_then_mutate)
    plan = build_rebuild_plan(db)

    assert wrote is True
    planned_bot = next(row for row in plan.bot_universe if row["id"] == bot["id"])
    assert planned_bot["is_active"] == 1
    with sqlite3.connect(db) as conn:
        assert conn.execute(
            "SELECT is_active FROM bots WHERE id=?", (bot["id"],)
        ).fetchone()[0] == 0


def test_numeric_rank_diff_matches_public_leaderboard_scope_and_order():
    def rating(bot_id, value, matches):
        return {
            "bot_id": bot_id,
            "game_id": "gomoku",
            "rating": float(value),
            "rd": 100.0,
            "vol": 0.06,
            "wins": matches,
            "losses": 0,
            "draws": 0,
            "delta_total": matches,
            "matches_played": matches,
            "last_played_at": "2026-08-10T16:00:00",
        }

    bot_universe = [
        {
            "id": bot_id,
            "game_id": "gomoku",
            "is_active": active,
            "format": "elf",
            "os": "linux",
            "arch": arch,
        }
        for bot_id, active, arch in (
            (1, 1, "amd64"),
            (2, 1, "amd64"),
            (3, 1, "amd64"),
            (4, 1, "amd64"),
            (5, 0, "amd64"),
            (6, 1, "arm64"),
        )
    ]
    current = [
        rating(1, 1800, 10),
        rating(2, 1800, 11),
        rating(3, 2100, 9),
        rating(4, 1700, 10),
        rating(5, 9999, 99),
        rating(6, 9998, 99),
    ]
    rebuilt = [
        rating(1, 1800, 10),
        rating(2, 1800, 10),
        rating(3, 2200, 9),
        rating(4, 1700, 10),
        rating(5, 9999, 99),
        rating(6, 9998, 99),
    ]

    changes = {row["bot_id"]: row for row in _rating_diff(
        current, rebuilt, bot_universe
    )}

    # Same rating: production order next compares matches, then Bot ID. Bot 1
    # is included even though its own projection did not change, because its
    # public rank changed as a consequence of Bot 2's rebuild.
    assert changes[1]["projection_changed"] is False
    assert (changes[1]["rank_before"], changes[1]["rank_after"]) == (2, 1)
    assert (changes[2]["rank_before"], changes[2]["rank_after"]) == (1, 2)
    # A high-rated sample below the public threshold has no rank. Its numeric
    # rating change remains auditable; inactive and wrong-architecture Bots
    # never affect public ranks.
    assert changes[3]["ranking_eligible_before"] is False
    assert changes[3]["ranking_eligible_after"] is False
    assert changes[3]["rank_before"] is None
    assert changes[3]["rating_before"] == 2100
    assert changes[3]["rating_after"] == 2200
    assert 5 not in changes
    assert 6 not in changes


def test_apply_rejects_old_business_backup_even_when_rating_digests_match(tmp_path):
    db = (tmp_path / "business-target.db").resolve()
    store = Store(str(db))
    store.close()
    reviewed = build_rebuild_plan(db)
    backup = (tmp_path / "business-old.cold.db").resolve()
    shutil.copy2(db, backup)

    changed = Store(str(db))
    changed.create_user(
        "new-business-user",
        "new-business-user@example.com",
        hash_password("password1"),
    )
    changed.close()
    current = build_rebuild_plan(db)
    for key in (
        "source_digest",
        "plan_digest",
        "rebuilt_projection_digest",
    ):
        assert current.report[key] == reviewed.report[key]

    with pytest.raises(RuntimeError, match="complete business digest"):
        _apply_reviewed(db, reviewed, backup)


def test_apply_rejects_changed_bot_universe_plan_digest(tmp_path):
    db = (tmp_path / "plan-change.db").resolve()
    store = Store(str(db))
    _bot(store, "plan-change")
    store.close()
    reviewed = build_rebuild_plan(db)
    backup = (tmp_path / "plan-change.cold.db").resolve()
    shutil.copy2(db, backup)

    changed = Store(str(db))
    _bot(changed, "plan-added-after-review")
    changed.close()
    with pytest.raises(RuntimeError, match="评分重建摘要已变化.*plan"):
        _apply_reviewed(db, reviewed, backup)


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("is_active", 0),
        ("format", "pe"),
        ("os", "windows"),
        ("arch", "arm64"),
    ],
)
def test_apply_rejects_leaderboard_visibility_change_in_plan_digest(
    tmp_path, column, value
):
    db = (tmp_path / f"visibility-{column}.db").resolve()
    store = Store(str(db))
    bot = _bot(store, f"visibility-{column}")
    store.close()

    # Production only accepts Linux/amd64 ELF, but legacy/corrupt databases can
    # lack those CHECKs.  Remove only the three metadata CHECK clauses before
    # taking the reviewed baseline so the apply path still runs integrity_check.
    with sqlite3.connect(db) as conn:
        schema = conn.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='bots'"
        ).fetchone()[0]
        relaxed = schema
        for clause in (
            "    CONSTRAINT chk_bot_os CHECK (os = 'linux'),\n",
            "    CONSTRAINT chk_bot_arch CHECK (arch = 'amd64'),\n",
            "    CONSTRAINT chk_format CHECK (format = 'elf'),\n",
        ):
            relaxed = relaxed.replace(clause, "")
        assert relaxed != schema
        version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        conn.execute("PRAGMA writable_schema=ON")
        conn.execute(
            "UPDATE sqlite_schema SET sql=? WHERE type='table' AND name='bots'",
            (relaxed,),
        )
        conn.execute("PRAGMA writable_schema=OFF")
        conn.execute(f"PRAGMA schema_version={version + 1}")

    reviewed = build_rebuild_plan(db)
    backup = (tmp_path / f"visibility-{column}.cold.db").resolve()
    shutil.copy2(db, backup)
    with sqlite3.connect(db) as conn:
        conn.execute(
            f"UPDATE bots SET {column}=? WHERE id=?", (value, bot["id"])
        )

    current = build_rebuild_plan(db)
    assert current.report["source_digest"] == reviewed.report["source_digest"]
    assert (
        current.report["rebuilt_projection_digest"]
        == reviewed.report["rebuilt_projection_digest"]
    )
    assert current.report["bot_universe_digest"] != reviewed.report[
        "bot_universe_digest"
    ]
    assert current.report["plan_digest"] != reviewed.report["plan_digest"]
    with pytest.raises(RuntimeError, match="评分重建摘要已变化.*plan"):
        _apply_reviewed(db, reviewed, backup)


def test_rebuild_is_no_go_while_execution_attempt_is_active(tmp_path):
    db = (tmp_path / "queued-generation.db").resolve()
    store = Store(str(db))
    first = _bot(store, "queued-generation-a", binary_dir=tmp_path)
    second = _bot(store, "queued-generation-b", binary_dir=tmp_path)
    _mark_projection_verified(store)
    store.executions.resume()
    first_version = store.get_current_bot_version(first["id"])
    second_version = store.get_current_bot_version(second["id"])
    assert first_version is not None and second_version is not None
    queued = store.executions.enqueue(
        source="auto",
        owner_user_id=None,
        game_id="gomoku",
        match_type="ladder",
        bot_a_id=first["id"],
        bot_b_id=second["id"],
        bot_a_version_id=first_version["id"],
        bot_b_version_id=second_version["id"],
    )
    claimed = store.executions.claim_next(
        max_match_slots=1,
        max_sandbox_units=2,
        aging_seconds=60,
        user_active_limit=1,
        contest_share_slots=1,
    )
    assert claimed is not None and claimed["public_id"] == queued["public_id"]
    store.close()

    plan = build_rebuild_plan(db)
    assert plan.report["execution_active_count"] == 1
    assert plan.report["ready_to_apply"] is False
    assert any("execution_jobs 有 1 个活跃 attempt" in issue for issue in plan.report["issues"])

    backup = (tmp_path / "queued-generation.cold.db").resolve()
    shutil.copy2(db, backup)
    with pytest.raises(RuntimeError, match="No-Go: execution_jobs 有 1 个活跃 attempt"):
        _apply_reviewed(db, plan, backup)


@pytest.mark.parametrize("corrupt_target", [False, True])
def test_apply_requires_zero_foreign_key_violations_on_backup_and_target(
    tmp_path, corrupt_target
):
    db = (tmp_path / "fk-target.db").resolve()
    store = Store(str(db))
    store.close()
    reviewed = build_rebuild_plan(db)
    backup = (tmp_path / "fk-target.cold.db").resolve()
    shutil.copy2(db, backup)
    corrupt_path = db if corrupt_target else backup
    with sqlite3.connect(corrupt_path) as conn:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute(
            "INSERT INTO ratings(bot_id,game_id) VALUES(999999,'gomoku')"
        )

    label = "target" if corrupt_target else "backup"
    with pytest.raises(ValueError, match=rf"{label} foreign_key_check failed"):
        _apply_reviewed(db, reviewed, backup)


@pytest.mark.parametrize(
    ("bad_result", "policy_update", "issue_text"),
    [
        ({"deltas": [1, -1, 0]}, None, "exactly two integers"),
        ({"deltas": [True, -1]}, None, "non-boolean integers"),
        ({"deltas": [1.0, -1.0]}, None, "non-boolean integers"),
        ({"deltas": [1, 1]}, None, "zero-sum"),
        ({"deltas": [1, -1]}, (1, "same_owner"), "rated/rating_reason mismatch"),
        ({"deltas": [1, -1]}, (0, "eligible"), "rated/rating_reason mismatch"),
    ],
)
def test_rebuild_rejects_noncanonical_rated_source_contract(
    tmp_path, bad_result, policy_update, issue_text
):
    db = (tmp_path / "invalid-rated-source.db").resolve()
    store = Store(str(db))
    bot_a = _bot(store, "invalid-source-a")
    bot_b = _bot(store, "invalid-source-b")
    orch = MatchOrchestrator(store)
    match_id = "invalid-rated-source"
    _complete(
        store,
        orch,
        match_id,
        bot_a["id"],
        bot_b["id"],
        winner=0,
        ended_at="2026-08-10T18:00:00",
    )
    store.close()

    # Corrupt immutable evidence only inside this isolated fixture to prove the
    # rebuild's independent fail-closed audit, not merely the write trigger.
    with sqlite3.connect(db) as conn:
        if policy_update is None:
            conn.execute(
                "DROP TRIGGER trg_matches_gomoku_rating_source_update"
            )
            conn.execute(
                "UPDATE matches_gomoku SET result=? WHERE id=?",
                (json.dumps(bad_result), match_id),
            )
        else:
            conn.execute("DROP TRIGGER trg_match_rating_policy_source_immutable")
            conn.execute(
                "UPDATE match_rating_policies SET rated=?,rating_reason=? "
                "WHERE match_id=?",
                (*policy_update, match_id),
            )

    plan = build_rebuild_plan(db)
    assert plan.report["ready_to_apply"] is False
    assert any(issue_text in issue for issue in plan.report["issues"])
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        assert any(
            issue_text in issue
            for issue in rating_projection_digests(conn)["issues"]
        )

    backup = (tmp_path / "invalid-rated-source.cold.db").resolve()
    shutil.copy2(db, backup)
    with pytest.raises(ValueError, match="backup rating source is incomplete"):
        _apply_reviewed(db, plan, backup)


def test_match_detail_exposes_marker_truth_across_rating_lifecycle(tmp_path):
    app = create_app(db_path=str(tmp_path / "rating-detail.db"), max_concurrent=1)
    store = app.state.store
    bot_a = _bot(store, "api-state-a")
    bot_b = _bot(store, "api-state-b")
    match_id = "rating-state-lifecycle"
    store.create_match(match_id, bot_a["id"], bot_b["id"], game_id="gomoku")
    client = TestClient(app)

    pending = client.get(f"/api/matches/{match_id}").json()["match"]
    assert pending["rated"] is True
    assert pending["rating_settled"] is False
    assert "rating_settled_order" not in pending
    assert "_rating_settled_order" not in pending
    assert "rating_settlement_status" not in pending

    store.update_match(
        match_id,
        status="completed",
        winner=0,
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
        ended_at="2026-08-10T17:00:00",
    )
    waiting = client.get(f"/api/matches/{match_id}").json()["match"]
    assert waiting["rating_settled"] is False

    assert store.mark_match_rating_settled(match_id) is True
    settled = client.get(f"/api/matches/{match_id}").json()["match"]
    assert settled["rating_settled"] is True

    aborted_id = "rating-state-aborted"
    store.create_match(
        aborted_id, bot_a["id"], bot_b["id"], game_id="gomoku"
    )
    store.update_match(
        aborted_id,
        status="aborted",
        reason="platform_error",
        ended_at="2026-08-10T17:01:00",
    )
    aborted = client.get(f"/api/matches/{aborted_id}").json()["match"]
    assert aborted["rated"] is True
    assert aborted["rating_settled"] is False
