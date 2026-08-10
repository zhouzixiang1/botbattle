"""Offline rating projection rebuild and settlement-order invariants."""
from __future__ import annotations

import json
import shutil
import sqlite3

import pytest
from typer.testing import CliRunner

from bzplat.backend.cli import app as cli_app
from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.rating.rebuild import apply_rebuild_plan, build_rebuild_plan
from bzplat.backend.store import Store


def _bot(store: Store, username: str, *, owner_id: int | None = None) -> dict:
    if owner_id is None:
        owner = store.create_user(
            username, f"{username}@example.com", hash_password("password1")
        )
        owner_id = int(owner["id"])
    path = f"/tmp/{username}.elf"
    bot = store.create_bot(
        owner_id, f"bot-{username}", binary_path=path, format="elf", game_id="gomoku"
    )
    store.add_bot_version(bot["id"], binary_path=path)
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
        "references": {"matches": 1, "pairings": 0},
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

    backup = (tmp_path / "ratings.cold-backup.db").resolve()
    shutil.copy2(db, backup)
    with sqlite3.connect(db) as fault_conn:
        fault_conn.execute(
            "CREATE TRIGGER fail_rating_rebuild_history "
            "BEFORE INSERT ON rating_history BEGIN "
            "SELECT RAISE(ABORT,'rebuild fault injection'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="rebuild fault injection"):
        apply_rebuild_plan(
            db,
            plan.report["source_hash"],
            confirmed_database=db,
            backup_path=backup,
            service_stopped=True,
            cold_backup_confirmed=True,
        )
    after_fault = build_rebuild_plan(db)
    assert after_fault.report["current_projection_hash"] == plan.report["current_projection_hash"]
    assert after_fault.report["projection_state_current"] is False
    with sqlite3.connect(db) as fault_conn:
        fault_conn.execute("DROP TRIGGER fail_rating_rebuild_history")

    applied = apply_rebuild_plan(
        db,
        plan.report["source_hash"],
        confirmed_database=db,
        backup_path=backup,
        service_stopped=True,
        cold_backup_confirmed=True,
    )
    assert applied["verified_after_apply"] is True
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

    repeated = apply_rebuild_plan(
        db,
        verified.report["source_hash"],
        confirmed_database=db,
        backup_path=backup,
        service_stopped=True,
        cold_backup_confirmed=True,
    )
    assert repeated["verified_after_apply"] is True
    assert repeated["rebuilt_projection_hash"] == verified.report["rebuilt_projection_hash"]


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
    assert report["projection_state_current"] is False
    assert db.read_bytes() == before_bytes
    assert db.stat().st_mtime_ns == before_mtime

    denied = runner.invoke(
        cli_app,
        [
            "rating-rebuild", "--db", str(db), "--apply",
            "--source-digest", report["source_hash"],
        ],
    )
    assert denied.exit_code != 0

    applied = runner.invoke(
        cli_app,
        [
            "rating-rebuild", "--db", str(db), "--apply",
            "--source-digest", report["source_hash"],
            "--confirm-db", str(db), "--backup", str(backup),
            "--confirm-service-stopped", "--confirm-cold-backup",
        ],
    )
    assert applied.exit_code == 0, applied.stdout
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
    with pytest.raises(RuntimeError, match="评分源已变化"):
        apply_rebuild_plan(
            db,
            reviewed.report["source_hash"],
            confirmed_database=db,
            backup_path=backup,
            service_stopped=True,
            cold_backup_confirmed=True,
        )


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
