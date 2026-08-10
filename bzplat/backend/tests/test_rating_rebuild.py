"""Offline rating projection rebuild and settlement-order invariants."""
from __future__ import annotations

import json
import shutil
import sqlite3

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from bzplat.backend.cli import app as cli_app
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
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


def test_rank_and_tier_diff_matches_public_leaderboard_scope_and_order():
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
    # formal rank changed as a consequence of Bot 2's rebuild.
    assert changes[1]["projection_changed"] is False
    assert (changes[1]["rank_before"], changes[1]["rank_after"]) == (2, 1)
    assert (changes[2]["rank_before"], changes[2]["rank_after"]) == (1, 2)
    # A high-rated placement Bot has no formal rank, but its online tier diff is
    # still visible. Inactive and wrong-architecture Bots never affect ranks.
    assert changes[3]["is_placement_before"] is True
    assert changes[3]["rank_before"] is None
    assert changes[3]["tier_before"]["key"] == "expert"
    assert changes[3]["tier_after"]["key"] == "master"
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
    assert "_rating_settled_order" not in pending

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
