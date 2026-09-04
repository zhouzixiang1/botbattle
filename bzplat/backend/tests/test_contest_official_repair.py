from __future__ import annotations

import asyncio
import csv
import fcntl
import hashlib
import io
import json
import shutil
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner
from fastapi.testclient import TestClient

import bzplat.backend.cli as cli_module
import bzplat.backend.contests.official_repair as official_repair_module
from bzplat.backend.cli import app as cli_app
from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.official_repair import (
    OfficialRepairError,
    apply_official_results_repair,
    offline_official_repair_guard,
    plan_official_results_repair,
    scan_official_results_repairs,
)
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


class _PersistingOrchestrator:
    """Create contest Match rows without starting Bot processes."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.sequence = 0

    async def challenge(
        self,
        bot_a_id,
        bot_b_id,
        owner_user_id,
        *,
        match_type="contest",
        contest_id=None,
        game_id=None,
        **kwargs,
    ):
        self.sequence += 1
        match_id = f"official-repair-{contest_id}-{self.sequence}"
        bot_a_version_id = kwargs["bot_a_version_id"]
        bot_b_version_id = kwargs["bot_b_version_id"]
        assert type(bot_a_version_id) is int
        assert type(bot_b_version_id) is int
        self.store.create_match(
            match_id,
            bot_a_id,
            bot_b_id,
            owner_id=owner_user_id,
            contest_id=contest_id,
            match_type=match_type,
            game_id=game_id,
            match_config={
                "_bot_a_environment": "platform_high",
                "_bot_a_local_agent_id": None,
                "_bot_a_version_id": bot_a_version_id,
                "_bot_b_environment": "platform_high",
                "_bot_b_local_agent_id": None,
                "_bot_b_version_id": bot_b_version_id,
                "_execution_profile_version": 1,
                "_execution_request_id": f"req_{self.sequence:024d}",
                "duplicate": False,
                "time_control_id": kwargs["time_control_id"],
            },
        )
        return match_id


_FORMAL_MATCH_OUTCOMES = {
    (0, 1, 2, 1): (0, 4, 4.0, 56, "majority"),
    (0, 1, 6, 5): (1, -5, -5.0, 56, "majority"),
    (0, 1, 8, 3): (0, 9, 9.0, 53, "majority"),
    (0, 1, 7, 9): (1, -1, -1.0, 60, "majority"),
    (0, 2, 5, 2): (1, -7, -7.0, 52, "majority"),
    (0, 2, 9, 8): (0, 2, 2.0, 17, "illegal"),
    (0, 2, 4, 1): (1, -9, -9.0, 51, "majority"),
    (0, 2, 3, 6): (0, 5, 5.0, 56, "majority"),
    (0, 3, 2, 9): (0, 3, 3.0, 58, "majority"),
    (0, 3, 1, 5): (0, 13, 13.0, 45, "majority"),
    (0, 3, 8, 7): (0, 13, 13.0, 47, "majority"),
    (0, 3, 3, 4): (0, 2, 2.0, 59, "majority"),
    (0, 4, 2, 8): (0, 2, 2.0, 11, "illegal"),
    (0, 4, 1, 3): (0, 2, 2.0, 13, "crash"),
    (0, 4, 9, 6): (0, 6, 6.0, 55, "majority"),
    (0, 4, 7, 4): (0, 13, 13.0, 43, "majority"),
    (1, 1, 2, 5): (0, 9, 9.0, 52, "majority"),
    (1, 1, 9, 8): (0, 2, 2.0, 17, "illegal"),
    (1, 1, 1, 7): (0, 5, 5.0, 56, "majority"),
    (1, 1, 3, 6): (0, 6, 6.0, 55, "majority"),
    (1, 2, 2, 9): (0, 4, 4.0, 57, "majority"),
    (1, 2, 1, 3): (0, 2, 2.0, 13, "crash"),
    (1, 3, 2, 1): (0, 1, 1.0, 60, "majority"),
}

_EXPECTED_KO_BRACKET_SLOTS = {
    (1, 1, 2, 5): 0,
    (1, 1, 9, 8): 1,
    (1, 1, 1, 7): 2,
    (1, 1, 3, 6): 3,
    (1, 2, 2, 9): 0,
    (1, 2, 1, 3): 1,
    (1, 3, 2, 1): 0,
}

_EXPECTED_SCHEDULED_COORDINATES = {
    coordinate
    for coordinate in _FORMAL_MATCH_OUTCOMES
    if coordinate[1] == 1
}

_EXPECTED_PAIRING_SEATS = {
    (0, 1, 2, 1),
    (0, 1, 6, 5),
    (0, 1, 8, 3),
    (0, 1, 7, 9),
    (0, 1, 4, None),
    (0, 2, 5, 2),
    (0, 2, 9, 8),
    (0, 2, 4, 1),
    (0, 2, 3, 6),
    (0, 2, 7, None),
    (0, 3, 2, 9),
    (0, 3, 1, 5),
    (0, 3, 8, 7),
    (0, 3, 3, 4),
    (0, 3, 6, None),
    (0, 4, 2, 8),
    (0, 4, 1, 3),
    (0, 4, 9, 6),
    (0, 4, 7, 4),
    (0, 4, 5, None),
    (1, 1, 2, 5),
    (1, 1, 9, 8),
    (1, 1, 1, 7),
    (1, 1, 3, 6),
    (1, 2, 2, 9),
    (1, 2, 1, 3),
    (1, 3, 2, 1),
}

_EXPECTED_STAGE_RESULT_MAP = {
    (0, 2): (1, 8.0, 4, 0, 0, 16, (8.0, 20.0, 14.0, 20.0, 0.0, 16.0, 0, 2)),
    (0, 1): (2, 6.0, 3, 0, 1, 20, (6.0, 18.0, 10.0, 10.0, 0.0, 20.0, 0, 1)),
    (0, 9): (3, 6.0, 3, 0, 1, 6, (6.0, 18.0, 10.0, 10.0, 0.0, 6.0, 0, 9)),
    (0, 8): (4, 4.0, 2, 0, 2, 18, (4.0, 22.0, 14.0, 8.0, 1.0, 18.0, 0, 8)),
    (0, 3): (5, 4.0, 2, 0, 2, -4, (4.0, 14.0, 8.0, 4.0, 0.0, -4.0, 0, 3)),
    (0, 5): (6, 4.0, 1, 0, 2, -15, (4.0, 16.0, 8.0, 2.0, 0.0, -15.0, 0, 5)),
    (0, 7): (7, 4.0, 1, 0, 2, -1, (4.0, 12.0, 6.0, 2.0, 0.0, -1.0, 0, 7)),
    (0, 6): (8, 2.0, 0, 0, 3, -16, (2.0, 14.0, 8.0, 0.0, 0.0, -16.0, 0, 6)),
    (0, 4): (9, 2.0, 0, 0, 3, -24, (2.0, 14.0, 8.0, 0.0, 0.0, -24.0, 0, 4)),
    (1, 2): (1, 6.0, 3, 0, 0, 14, (6.0, 6.0, 2.0, 6.0, 0.0, 14.0, 0, 2)),
    (1, 1): (2, 4.0, 2, 0, 1, 6, (4.0, 8.0, 2.0, 2.0, 0.0, 6.0, 0, 1)),
    (1, 3): (3, 2.0, 1, 0, 1, 4, (2.0, 4.0, 0.0, 0.0, 0.0, 4.0, 0, 3)),
    (1, 9): (4, 2.0, 1, 0, 1, -2, (2.0, 6.0, 0.0, 0.0, 0.0, -2.0, 0, 9)),
    (1, 8): (5, 0.0, 0, 0, 1, -2, (0.0, 2.0, 0.0, 0.0, 0.0, -2.0, 0, 8)),
    (1, 7): (6, 0.0, 0, 0, 1, -5, (0.0, 4.0, 0.0, 0.0, 0.0, -5.0, 0, 7)),
    (1, 6): (7, 0.0, 0, 0, 1, -6, (0.0, 2.0, 0.0, 0.0, 0.0, -6.0, 0, 6)),
    (1, 5): (8, 0.0, 0, 0, 1, -9, (0.0, 6.0, 0.0, 0.0, 0.0, -9.0, 0, 5)),
}

_TIEBREAK_KEYS = (
    "points",
    "buchholz",
    "buchholz_cut1",
    "sonneborn_berger",
    "head_to_head",
    "normalized_delta",
    "technical_losses",
    "seed",
)


def _create_players(store: Store, root: Path, count: int = 9):
    players = []
    for index in range(count):
        user = store.create_user(
            f"official-repair-user-{index}",
            f"official-repair-{index}@example.invalid",
            hash_password("fixture-password"),
            role="organizer" if index == 0 else "user",
        )
        store.update_user(user["id"], email_verified=1)
        user = store.get_user(user["id"])
        assert user is not None
        binary = root / f"official-repair-bot-{index}"
        binary.write_bytes(b"offline repair fixture")
        bot = store.create_bot(
            user["id"],
            f"official-repair-bot-{index}",
            binary_path=str(binary),
            format="elf",
            game_id="pencil",
        )
        version = store.add_bot_version(
            bot["id"],
            binary_path=str(binary),
            protocol_version="pencil_xy_v1",
        )
        assert version["bot_id"] == bot["id"]
        players.append((user, bot))
    return players


def _complete_current_matches(store: Store, contest_id: int, stage_idx: int) -> int:
    entries = {
        entry["id"]: entry for entry in store.list_contest_entries(contest_id)
    }
    completed = 0
    for pairing in store.list_contest_pairings(contest_id, stage_idx=stage_idx):
        match_id = pairing.get("match_id")
        if not match_id:
            continue
        match = store.get_match(match_id)
        if match and match["status"] == "completed":
            continue
        seed_a = entries[pairing["entry_a_id"]]["seed"]
        seed_b = entries[pairing["entry_b_id"]]["seed"]
        outcome_key = (stage_idx, pairing["round_num"], seed_a, seed_b)
        winner, signed_delta, normalized_delta, rounds_played, reason = (
            _FORMAL_MATCH_OUTCOMES[outcome_key]
        )
        assert type(normalized_delta) is float
        assert match is not None
        timestamp = match["created_at"]
        store.update_match(
            match_id,
            status="completed",
            winner=winner,
            reason=reason,
            technical_loss=0,
            started_at=timestamp,
            ended_at=timestamp,
            result={
                "rounds_played": rounds_played,
                "deltas": [signed_delta, -signed_delta],
                "normalized_delta": normalized_delta,
            },
        )
        assert store.complete_contest_pairing_for_match(contest_id, match_id)
        completed += 1
    return completed


@pytest.fixture
def exact_legacy_nine_eight(tmp_path):
    """Build a real 9-player Pencil Swiss->KO result, then import its old 9/8 gap."""
    db_path = tmp_path / "official-repair.db"
    store = Store(str(db_path))
    players = _create_players(store, tmp_path)
    organizer = players[0][0]
    players = [players[index] for index in (1, 0, 5, 8, 3, 2, 6, 4, 7)]
    manager = ContestManager(store, _PersistingOrchestrator(store))
    contest = manager.create(
        organizer["id"],
        "offline official repair fixture",
        game_id="pencil",
        template_id="pencil_swiss_ko",
    )
    for user, bot in players:
        store.add_contest_entry(contest["id"], user["id"], bot["id"])

    async def finish_contest() -> None:
        await manager.start(contest["id"])
        for _ in range(16):
            current = store.get_contest(contest["id"])
            assert current is not None
            if current["status"] == "finished":
                return
            if current["status"] == "rest":
                await manager.resume(contest["id"])
                continue
            assert current["status"] == "running"
            stage_idx = int(current["current_stage_idx"])
            assert _complete_current_matches(store, contest["id"], stage_idx) > 0
            await manager.maybe_finish(contest["id"])
        pytest.fail("fixture contest did not reach finished state")

    asyncio.run(finish_contest())
    current = store.get_contest(contest["id"])
    assert current is not None
    assert current["status"] == "finished"
    assert current["official_results_ready"] == 1
    assert len(store.list_stage_results(contest["id"], stage_idx=0)) == 9
    assert len(store.list_stage_results(contest["id"], stage_idx=1)) == 8
    full_official = store.list_official_results(contest["id"])
    assert len(full_official) == 9
    entries = store.list_contest_entries(contest["id"])
    eliminated = {entry["id"] for entry in entries if entry["eliminated"] == 1}
    assert eliminated == {full_official[-1]["entry_id"]}

    # Explicit imported legacy shape observed in the deployment preflight:
    # ready=1 with the otherwise exact top-eight prefix, no lifecycle manifest,
    # revision epoch 0 and no seal.  Product lifecycle code never creates it.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for index, row in enumerate(
            connection.execute(
                "SELECT id,stage_idx,round_num,match_id FROM contest_pairings "
                "WHERE contest_id=? ORDER BY stage_idx,round_num,id",
                (contest["id"],),
            ).fetchall()
        ):
            timestamp = f"2026-08-01T00:{index:02d}:00"
            scheduled_at = (
                timestamp
                if row["match_id"] is not None and row["round_num"] == 1
                else None
            )
            connection.execute(
                "UPDATE contest_pairings SET published_at=?,scheduled_at=? "
                "WHERE id=?",
                (timestamp, scheduled_at, row["id"]),
            )
            if row["match_id"] is not None:
                connection.execute(
                    "UPDATE matches_pencil SET created_at=?,started_at=?,ended_at=?,"
                    "likes_count=0,views_count=? WHERE id=?",
                    (timestamp, timestamp, timestamp, index % 8, row["match_id"]),
                )
        for row in connection.execute(
            "SELECT id,match_config FROM matches_pencil WHERE contest_id=?",
            (contest["id"],),
        ).fetchall():
            match_config = json.loads(row["match_config"])
            assert match_config.pop("time_control_id")
            connection.execute(
                "UPDATE matches_pencil SET match_config=? WHERE id=?",
                (
                    json.dumps(
                        match_config,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                    row["id"],
                ),
            )
        connection.execute(
            "DELETE FROM contest_official_results WHERE contest_id=? AND rank=9",
            (contest["id"],),
        )
        connection.execute(
            "UPDATE contests SET published_stage_pairing_count=NULL,"
            "time_control_id=NULL WHERE id=?",
            (contest["id"],),
        )
        connection.execute(
            "UPDATE contests SET pairing_topology_revision=0,"
            "sealed_pairing_topology_revision=NULL WHERE id=?",
            (contest["id"],),
        )

    with store._tx() as connection:
        lifecycle = tuple(
            connection.execute(
                "SELECT published_stage_pairing_count,pairing_topology_revision,"
                "sealed_pairing_topology_revision FROM contests WHERE id=?",
                (contest["id"],),
            ).fetchone()
        )
        official_count = connection.execute(
            "SELECT COUNT(*) FROM contest_official_results WHERE contest_id=?",
            (contest["id"],),
        ).fetchone()[0]
        contract = tuple(
            connection.execute(
                "SELECT typeof(ruleset_version),ruleset_version,"
                "typeof(protocol_version),protocol_version,"
                "typeof(rating_pool_id),rating_pool_id,typeof(time_control_id) "
                "FROM contests WHERE id=?",
                (contest["id"],),
            ).fetchone()
        )
        entry_seeds = connection.execute(
            "SELECT typeof(seed),seed FROM contest_entries WHERE contest_id=? "
            "ORDER BY seed",
            (contest["id"],),
        ).fetchall()
        pairing_shapes = connection.execute(
            "SELECT stage_idx,stage_key,COUNT(*),SUM(entry_b_id IS NULL),"
            "SUM(typeof(bot_a_version_id)='integer'),"
            "SUM(typeof(bot_a_version_id)='null'),"
            "SUM(typeof(bot_b_version_id)='integer'),"
            "SUM(typeof(bot_b_version_id)='null'),"
            "SUM(typeof(scheduled_at)='text'),SUM(typeof(scheduled_at)='null') "
            "FROM contest_pairings WHERE contest_id=? GROUP BY stage_idx,stage_key "
            "ORDER BY stage_idx",
            (contest["id"],),
        ).fetchall()
        stage_shapes = connection.execute(
            "SELECT stage_idx,stage_key,COUNT(*),"
            "SUM(typeof(group_id)='text' AND group_id=''),"
            "SUM(typeof(bot_id)='integer') FROM contest_stage_results "
            "WHERE contest_id=? GROUP BY stage_idx,stage_key ORDER BY stage_idx",
            (contest["id"],),
        ).fetchall()
    assert lifecycle == (None, 0, None)
    assert official_count == 8
    assert contract == (
        "text",
        "pencil_ccgc_v1",
        "text",
        "pencil_xy_v1",
        "text",
        "pencil_rating_v1",
        "null",
    )
    assert [tuple(row) for row in entry_seeds] == [
        ("integer", seed) for seed in range(1, 10)
    ]
    assert [tuple(row) for row in pairing_shapes] == [
        (0, "swiss", 20, 4, 16, 4, 16, 4, 4, 16),
        (1, "ko", 7, 0, 7, 0, 7, 0, 4, 3),
    ]
    assert [tuple(row) for row in stage_shapes] == [
        (0, "swiss", 9, 9, 9),
        (1, "ko", 8, 8, 8),
    ]
    yield store, contest["id"], full_official
    store.close()


def test_snapshot_only_official_repair_plans_exact_legacy_prefix(
    exact_legacy_nine_eight,
):
    store, contest_id, full_official = exact_legacy_nine_eight

    with store._tx() as connection:
        connection.execute("BEGIN")
        plan = plan_official_results_repair(connection, contest_id)
    assert plan.eligible is True
    assert plan.contest_id == contest_id
    assert plan.existing_official_count == 8
    assert plan.repaired_official_count == 9
    assert plan.missing_rank == 9
    assert plan.missing_entry_is_eliminated is True
    assert plan.candidate_rows[-1]["entry_id"] == full_official[-1]["entry_id"]
    report = plan.public_report()
    assert set(report) == {
        "authority_digest",
        "contest_id",
        "eligibility",
        "existing_official_count",
        "missing_rank",
        "old_official_digest",
        "plan_digest",
        "policy_version",
        "repaired_official_count",
        "repaired_official_digest",
        "source_business_digest",
        "expected_post_business_digest",
    }
    serialized = json.dumps(report, ensure_ascii=False)
    assert "official-repair-user" not in serialized
    assert "example.invalid" not in serialized
    assert "entry_id" not in serialized
    assert "bot_id" not in serialized
    assert "user_id" not in serialized


def test_formal_fixture_matches_reviewed_anonymous_production_authority(
    exact_legacy_nine_eight,
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("BEGIN")
        rows = connection.execute(
            "SELECT r.stage_idx,e.seed,r.rank_in_group,r.points,r.wins,r.draws,"
            "r.losses,r.delta_total,r.payload_json FROM contest_stage_results r "
            "JOIN contest_entries e ON e.id=r.entry_id AND e.contest_id=r.contest_id "
            "WHERE r.contest_id=? ORDER BY r.stage_idx,e.seed",
            (contest_id,),
        ).fetchall()
        pairings = connection.execute(
            "SELECT p.stage_idx,p.round_num,ea.seed,eb.seed,p.bracket_slot,"
            "p.published_at,p.scheduled_at,m.winner,m.reason,m.technical_loss,"
            "m.created_at,m.started_at,m.ended_at,m.likes_count,m.views_count,m.result "
            "FROM contest_pairings p JOIN contest_entries ea ON ea.id=p.entry_a_id "
            "LEFT JOIN contest_entries eb ON eb.id=p.entry_b_id "
            "LEFT JOIN matches_pencil m ON m.id=p.match_id "
            "WHERE p.contest_id=? ORDER BY p.stage_idx,p.round_num,p.id",
            (contest_id,),
        ).fetchall()

    observed = {}
    for row in rows:
        payload = json.loads(row[8])
        assert set(payload) == {"tiebreaks"}
        tiebreaks = payload["tiebreaks"]
        assert tuple(tiebreaks) == _TIEBREAK_KEYS
        assert all(type(tiebreaks[key]) is float for key in _TIEBREAK_KEYS[:6])
        assert all(type(tiebreaks[key]) is int for key in _TIEBREAK_KEYS[6:])
        assert type(row[2]) is int
        assert type(row[3]) is float
        assert all(type(row[index]) is int for index in range(4, 8))
        observed[(row[0], row[1])] = (
            row[2],
            row[3],
            row[4],
            row[5],
            row[6],
            row[7],
            tuple(tiebreaks[key] for key in _TIEBREAK_KEYS),
        )
    assert observed == _EXPECTED_STAGE_RESULT_MAP
    assert {(row[0], row[1], row[2], row[3]) for row in pairings} == (
        _EXPECTED_PAIRING_SEATS
    )
    observed_views = set()
    for (
        stage_idx,
        round_num,
        seed_a,
        seed_b,
        bracket_slot,
        published_at,
        scheduled_at,
        winner,
        reason,
        technical_loss,
        created_at,
        started_at,
        ended_at,
        likes_count,
        views_count,
        result_json,
    ) in pairings:
        coordinate = (stage_idx, round_num, seed_a, seed_b)
        assert official_repair_module.validate_canonical_naive_timestamp(
            published_at, "published"
        ) == published_at
        assert (scheduled_at is not None) == (
            coordinate in _EXPECTED_SCHEDULED_COORDINATES
        )
        if seed_b is None:
            assert bracket_slot is None
            assert scheduled_at is None
            assert winner is None
            assert result_json is None
            continue
        if stage_idx == 0:
            assert bracket_slot is None
        else:
            assert bracket_slot == _EXPECTED_KO_BRACKET_SLOTS[coordinate]
        for label, timestamp in (
            ("created", created_at),
            ("started", started_at),
            ("ended", ended_at),
        ):
            assert official_repair_module.validate_canonical_naive_timestamp(
                timestamp, label
            ) == timestamp
        if scheduled_at is not None:
            assert official_repair_module.validate_canonical_naive_timestamp(
                scheduled_at, "scheduled"
            ) == scheduled_at
            assert scheduled_at <= created_at
        assert published_at <= created_at <= started_at <= ended_at
        expected_winner, signed_delta, normalized_delta, rounds_played, expected_reason = (
            _FORMAL_MATCH_OUTCOMES[coordinate]
        )
        assert winner == expected_winner
        assert reason == expected_reason
        assert type(technical_loss) is int and technical_loss == 0
        assert type(likes_count) is int and likes_count == 0
        assert type(views_count) is int and 0 <= views_count <= 7
        observed_views.add(views_count)
        assert json.loads(result_json) == {
            "rounds_played": rounds_played,
            "deltas": [signed_delta, -signed_delta],
            "normalized_delta": normalized_delta,
        }
        assert type(json.loads(result_json)["normalized_delta"]) is float
    assert observed_views == set(range(8))


def test_planner_accepts_legacy_migration_column_order(
    exact_legacy_nine_eight,
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        sequence = connection.execute(
            "SELECT seq FROM sqlite_sequence "
            "WHERE name='contest_official_results'"
        ).fetchone()[0]
        connection.execute(
            "ALTER TABLE contest_official_results "
            "RENAME TO contest_official_results_fresh_order"
        )
        connection.execute(
            "CREATE TABLE contest_official_results("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,"
            "contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,"
            "entry_id INTEGER NOT NULL,stage_idx INTEGER NOT NULL DEFAULT 0,"
            "rank INTEGER NOT NULL,points REAL NOT NULL DEFAULT 0,"
            "bot_id INTEGER REFERENCES bots(id) ON DELETE SET NULL,user_id INTEGER,"
            "tiebreaks_json TEXT NOT NULL DEFAULT '{}',"
            "awarded TEXT NOT NULL DEFAULT '',group_id TEXT NOT NULL DEFAULT '',"
            "rank_in_group INTEGER,UNIQUE(contest_id,entry_id))"
        )
        connection.execute(
            "INSERT INTO contest_official_results("
            "id,contest_id,entry_id,stage_idx,rank,points,bot_id,user_id,"
            "tiebreaks_json,awarded,group_id,rank_in_group) "
            "SELECT id,contest_id,entry_id,stage_idx,rank,points,bot_id,user_id,"
            "tiebreaks_json,awarded,group_id,rank_in_group "
            "FROM contest_official_results_fresh_order"
        )
        connection.execute("DROP TABLE contest_official_results_fresh_order")
        connection.execute(
            "UPDATE sqlite_sequence SET seq=? "
            "WHERE name='contest_official_results'",
            (sequence,),
        )

    plan = _plan(store, contest_id)
    assert plan.eligible is True
    assert plan.existing_official_count == 8
    assert plan.repaired_official_count == 9


@pytest.mark.parametrize(
    "missing_constraint",
    [
        "autoincrement",
        "autoincrement_comment",
        "defaults",
        "foreign_keys",
        "unique",
        "generated_column",
        "extra_check",
    ],
)
def test_planner_rejects_matching_official_columns_without_exact_constraints(
    exact_legacy_nine_eight, missing_constraint
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    autoincrement = (
        ""
        if missing_constraint in {"autoincrement", "autoincrement_comment"}
        else " AUTOINCREMENT"
    )
    autoincrement_comment = (
        " /* id INTEGER PRIMARY KEY AUTOINCREMENT */"
        if missing_constraint == "autoincrement_comment"
        else ""
    )
    stage_default = "" if missing_constraint == "defaults" else " DEFAULT 0"
    points_default = "" if missing_constraint == "defaults" else " DEFAULT 0"
    text_default = "" if missing_constraint == "defaults" else " DEFAULT '{}'"
    awarded_default = "" if missing_constraint == "defaults" else " DEFAULT ''"
    group_default = "" if missing_constraint == "defaults" else " DEFAULT ''"
    contest_reference = (
        "" if missing_constraint == "foreign_keys"
        else " REFERENCES contests(id) ON DELETE CASCADE"
    )
    bot_reference = (
        "" if missing_constraint == "foreign_keys"
        else " REFERENCES bots(id) ON DELETE SET NULL"
    )
    unique = "" if missing_constraint == "unique" else ",UNIQUE(contest_id,entry_id)"
    generated_column = (
        ",shadow TEXT GENERATED ALWAYS AS ('x') VIRTUAL"
        if missing_constraint == "generated_column"
        else ""
    )
    extra_check = ",CHECK(rank<9)" if missing_constraint == "extra_check" else ""
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "ALTER TABLE contest_official_results "
            "RENAME TO contest_official_results_canonical"
        )
        connection.execute(
            "CREATE TABLE contest_official_results("
            f"id INTEGER PRIMARY KEY{autoincrement}{autoincrement_comment},"
            f"contest_id INTEGER NOT NULL{contest_reference},"
            "entry_id INTEGER NOT NULL,"
            f"stage_idx INTEGER NOT NULL{stage_default},"
            "rank INTEGER NOT NULL,"
            f"points REAL NOT NULL{points_default},"
            f"bot_id INTEGER{bot_reference},user_id INTEGER,"
            f"tiebreaks_json TEXT NOT NULL{text_default},"
            f"awarded TEXT NOT NULL{awarded_default},"
            f"group_id TEXT NOT NULL{group_default},rank_in_group INTEGER"
            f"{generated_column}{extra_check}{unique})"
        )
        connection.execute(
            "INSERT INTO contest_official_results("
            "id,contest_id,entry_id,stage_idx,rank,points,bot_id,user_id,"
            "tiebreaks_json,awarded,group_id,rank_in_group) "
            "SELECT id,contest_id,entry_id,stage_idx,rank,points,bot_id,user_id,"
            "tiebreaks_json,awarded,group_id,rank_in_group "
            "FROM contest_official_results_canonical"
        )
        connection.execute("DROP TABLE contest_official_results_canonical")
        if missing_constraint == "autoincrement_comment":
            maximum = connection.execute(
                "SELECT MAX(id) FROM contest_official_results"
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)",
                ("contest_official_results", maximum),
            )

    with store._tx() as connection:
        connection.execute("BEGIN")
        with pytest.raises(OfficialRepairError) as raised:
            plan_official_results_repair(connection, contest_id)
    assert raised.value.code == "schema_invalid"


def test_planner_accepts_observed_legacy_derived_swiss_round_count(
    exact_legacy_nine_eight,
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        stages = json.loads(
            connection.execute(
                "SELECT stages_json FROM contests WHERE id=?", (contest_id,)
            ).fetchone()[0]
        )
        assert stages[0].pop("effective_rounds") == 4
        connection.execute(
            "UPDATE contests SET stages_json=? WHERE id=?",
            (json.dumps(stages, ensure_ascii=False), contest_id),
        )
        _restore_exact_legacy_epoch(connection, contest_id)

    plan = _plan(store, contest_id)
    assert plan.eligible is True
    assert plan.existing_official_count == 8
    assert plan.repaired_official_count == 9


def _plan(store: Store, contest_id: int):
    with store._tx() as connection:
        connection.execute("BEGIN")
        return plan_official_results_repair(connection, contest_id)


def _raw_plan(path: Path, contest_id: int):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        return plan_official_results_repair(connection, contest_id)


def _restore_exact_legacy_epoch(connection: sqlite3.Connection, contest_id: int) -> None:
    connection.execute(
        "UPDATE contests SET published_stage_pairing_count=NULL,"
        "pairing_topology_revision=0,sealed_pairing_topology_revision=NULL "
        "WHERE id=?",
        (contest_id,),
    )


def _stage_result_row(
    connection: sqlite3.Connection,
    contest_id: int,
    *,
    stage_idx: int,
    seed: int,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT r.*,e.seed FROM contest_stage_results r "
        "JOIN contest_entries e ON e.id=r.entry_id AND e.contest_id=r.contest_id "
        "WHERE r.contest_id=? AND r.stage_idx=? AND e.seed=?",
        (contest_id, stage_idx, seed),
    ).fetchone()
    assert row is not None
    return row


_STAGE_PAYLOAD_SHAPE_MUTATIONS = (
    "blob",
    "non_object",
    "root_extra",
    "overall_rank",
    "inner_extra",
    "duplicate_root",
    "duplicate_inner",
    "nan",
    "infinity",
    "overflow_float",
    "oversize",
    "deep_recursion",
    *(f"int_alias_{key}" for key in _TIEBREAK_KEYS[:6]),
)


@pytest.mark.parametrize("mutation", _STAGE_PAYLOAD_SHAPE_MUTATIONS)
def test_planner_rejects_noncanonical_stage_payload_without_writes(
    exact_legacy_nine_eight, mutation
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = _stage_result_row(connection, contest_id, stage_idx=0, seed=4)
        payload = json.loads(row["payload_json"])
        tiebreaks = payload["tiebreaks"]
        if mutation == "blob":
            raw = sqlite3.Binary(row["payload_json"].encode("utf-8"))
        elif mutation == "non_object":
            raw = "[]"
        elif mutation == "root_extra":
            payload["extra"] = None
            raw = json.dumps(payload, separators=(",", ":"))
        elif mutation == "overall_rank":
            payload["overall_rank"] = 9
            raw = json.dumps(payload, separators=(",", ":"))
        elif mutation == "inner_extra":
            tiebreaks["extra"] = None
            raw = json.dumps(payload, separators=(",", ":"))
        elif mutation == "duplicate_root":
            encoded = json.dumps(tiebreaks, separators=(",", ":"))
            raw = f'{{"tiebreaks":{encoded},"tiebreaks":{encoded}}}'
        elif mutation == "duplicate_inner":
            encoded = json.dumps(tiebreaks, separators=(",", ":"))
            raw = (
                '{"tiebreaks":{"points":2.0,'
                + encoded.removeprefix("{")
                + "}"
            )
        elif mutation == "nan":
            encoded = json.dumps(tiebreaks, separators=(",", ":"))
            raw = f'{{"tiebreaks":{encoded},"ignored":NaN}}'
        elif mutation == "infinity":
            encoded = json.dumps(tiebreaks, separators=(",", ":"))
            raw = f'{{"tiebreaks":{encoded},"ignored":Infinity}}'
        elif mutation == "overflow_float":
            encoded = json.dumps(tiebreaks, separators=(",", ":"))
            raw = encoded.replace('"points":2.0', '"points":1e999', 1)
            raw = f'{{"tiebreaks":{raw}}}'
        elif mutation == "oversize":
            raw = row["payload_json"] + (" " * 5000)
        elif mutation == "deep_recursion":
            encoded = json.dumps(tiebreaks, separators=(",", ":"))
            deep = ("[" * 1500) + "0" + ("]" * 1500)
            raw = f'{{"tiebreaks":{encoded},"ignored":{deep}}}'
        else:
            key = mutation.removeprefix("int_alias_")
            assert key in _TIEBREAK_KEYS[:6]
            tiebreaks[key] = int(tiebreaks[key])
            raw = json.dumps(payload, separators=(",", ":"))
        connection.execute(
            "UPDATE contest_stage_results SET payload_json=? WHERE id=?",
            (raw, row["id"]),
        )
        _restore_exact_legacy_epoch(connection, contest_id)

    with store._tx() as connection:
        connection.execute("BEGIN")
        before_changes = connection.total_changes
        with pytest.raises(OfficialRepairError) as raised:
            plan_official_results_repair(connection, contest_id)
        assert connection.total_changes == before_changes
    assert raised.value.code == "raw_authority_invalid"


@pytest.mark.parametrize(
    "target",
    ["stages", "stage_payload", "match_config", "match_result", "official_tiebreaks"],
)
@pytest.mark.parametrize(
    "variant",
    [
        "oversize",
        "deep_recursion",
        "duplicate_key",
        "nonfinite",
        "negative_zero_int",
        "rounded_float",
        "exponent_float",
        "underflow_float",
    ],
)
def test_all_cold_json_boundaries_fail_closed_without_writes(
    exact_legacy_nine_eight, target, variant
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    deep = ("[" * 1500) + "0" + ("]" * 1500)
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if target == "stages":
            row = connection.execute(
                "SELECT stages_json FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            raw = row[0]
            if variant == "oversize":
                raw += " " * 5000
            elif variant == "deep_recursion":
                canonical = json.dumps(json.loads(raw), separators=(",", ":"))
                raw = canonical.replace(
                    '"key":"swiss"', f'"key":{deep},"key":"swiss"', 1
                )
            elif variant == "duplicate_key":
                canonical = json.dumps(json.loads(raw), separators=(",", ":"))
                raw = canonical.replace(
                    '"key":"swiss"', '"key":"swiss","key":"swiss"', 1
                )
            elif variant == "nonfinite":
                decoded = json.loads(raw)
                decoded[0]["effective_rounds"] = float("nan")
                raw = json.dumps(decoded, separators=(",", ":"))
            else:
                raw = json.dumps(json.loads(raw), separators=(",", ":"))
                replacement = {
                    "negative_zero_int": ('"rounds":0', '"rounds":-0'),
                    "rounded_float": (
                        '"advance_count":8',
                        '"advance_count":8.0000000000000001',
                    ),
                    "exponent_float": ('"advance_count":8', '"advance_count":8e0'),
                    "underflow_float": ('"rounds":0', '"rounds":1e-999'),
                }[variant]
                changed = raw.replace(*replacement, 1)
                assert changed != raw
                raw = changed
            connection.execute(
                "UPDATE contests SET stages_json=? WHERE id=?", (raw, contest_id)
            )
        elif target == "stage_payload":
            stage_idx, seed = (
                (0, 2)
                if variant in {"rounded_float", "exponent_float"}
                else ((1, 8) if variant == "underflow_float" else (0, 4))
            )
            row = _stage_result_row(
                connection, contest_id, stage_idx=stage_idx, seed=seed
            )
            raw = row["payload_json"]
            if variant == "oversize":
                raw += " " * 5000
            elif variant == "deep_recursion":
                raw = raw[:-1] + f',"ignored":{deep}}}'
            elif variant == "duplicate_key":
                tiebreaks = json.loads(raw)["tiebreaks"]
                encoded = json.dumps(tiebreaks, separators=(",", ":"))
                raw = f'{{"tiebreaks":{encoded},"tiebreaks":{encoded}}}'
            elif variant == "nonfinite":
                raw = raw[:-1] + ',"ignored":NaN}'
            else:
                raw = json.dumps(json.loads(raw), separators=(",", ":"))
                replacement = {
                    "negative_zero_int": (
                        '"technical_losses":0',
                        '"technical_losses":-0',
                    ),
                    "rounded_float": (
                        '"points":8.0',
                        '"points":8.0000000000000001',
                    ),
                    "exponent_float": ('"points":8.0', '"points":8e0'),
                    "underflow_float": ('"points":0.0', '"points":1e-999'),
                }[variant]
                changed = raw.replace(*replacement, 1)
                assert changed != raw
                raw = changed
            connection.execute(
                "UPDATE contest_stage_results SET payload_json=? WHERE id=?",
                (raw, row["id"]),
            )
        elif target in {"match_config", "match_result"}:
            column = "match_config" if target == "match_config" else "result"
            row = connection.execute(
                f"SELECT id,{column} FROM matches_pencil WHERE contest_id=? "
                "ORDER BY id LIMIT 1",
                (contest_id,),
            ).fetchone()
            raw = row[column]
            if variant == "oversize":
                raw += " " * 5000
            elif variant == "deep_recursion" and target == "match_config":
                raw = raw.replace(
                    '"_bot_a_environment":"platform_high"',
                    f'"_bot_a_environment":{deep},'
                    '"_bot_a_environment":"platform_high"',
                    1,
                )
            elif variant == "deep_recursion":
                raw = raw[:-1] + f',"ignored":{deep}}}'
            elif variant == "duplicate_key" and target == "match_config":
                raw = raw.replace(
                    '"_bot_a_environment":"platform_high"',
                    '"_bot_a_environment":"platform_high",'
                    '"_bot_a_environment":"platform_high"',
                    1,
                )
            elif variant == "duplicate_key":
                canonical = json.dumps(json.loads(raw), separators=(",", ":"))
                rounds_played = json.loads(raw)["rounds_played"]
                raw = canonical.replace(
                    f'"rounds_played":{rounds_played}',
                    f'"rounds_played":{rounds_played},'
                    f'"rounds_played":{rounds_played}',
                    1,
                )
            elif variant == "nonfinite":
                decoded = json.loads(raw)
                key = (
                    "_execution_profile_version"
                    if target == "match_config"
                    else "normalized_delta"
                )
                decoded[key] = float("nan")
                raw = json.dumps(decoded, separators=(",", ":"))
            else:
                decoded = json.loads(raw)
                raw = json.dumps(decoded, separators=(",", ":"))
                if target == "match_config":
                    original = '"_execution_profile_version":1'
                    token = {
                        "negative_zero_int": "-0",
                        "rounded_float": "1.0000000000000001",
                        "exponent_float": "1e0",
                        "underflow_float": "1e-999",
                    }[variant]
                else:
                    if variant == "negative_zero_int":
                        original = f'"rounds_played":{decoded["rounds_played"]}'
                        token = "-0"
                    else:
                        normalized = decoded["normalized_delta"]
                        original = (
                            '"normalized_delta":'
                            + json.dumps(normalized, separators=(",", ":"))
                        )
                        token = {
                            "rounded_float": f"{int(normalized)}.0000000000000001",
                            "exponent_float": f"{int(normalized)}e0",
                            "underflow_float": "1e-999",
                        }[variant]
                changed = raw.replace(original, original.split(":", 1)[0] + ":" + token, 1)
                assert changed != raw
                raw = changed
            connection.execute(
                f"UPDATE matches_pencil SET {column}=? WHERE id=?", (raw, row["id"])
            )
        else:
            row = connection.execute(
                "SELECT id,tiebreaks_json FROM contest_official_results "
                "WHERE contest_id=? ORDER BY rank LIMIT 1",
                (contest_id,),
            ).fetchone()
            raw = row["tiebreaks_json"]
            if variant == "oversize":
                raw += " " * 5000
            elif variant == "deep_recursion":
                raw = raw[:-1] + f',"ignored":{deep}}}'
            elif variant == "duplicate_key":
                canonical = json.dumps(json.loads(raw), separators=(",", ":"))
                points = json.loads(raw)["points"]
                raw = canonical.replace(
                    f'"points":{json.dumps(points)}',
                    f'"points":{json.dumps(points)},"points":{json.dumps(points)}',
                    1,
                )
            elif variant == "nonfinite":
                decoded = json.loads(raw)
                decoded["points"] = float("nan")
                raw = json.dumps(decoded, separators=(",", ":"))
            else:
                decoded = json.loads(raw)
                raw = json.dumps(decoded, separators=(",", ":"))
                if variant == "negative_zero_int":
                    original = '"technical_losses":0'
                    token = '"technical_losses":-0'
                elif variant in {"rounded_float", "exponent_float"}:
                    key = next(
                        key
                        for key, value in decoded.items()
                        if type(value) is float and value != 0.0
                    )
                    original = f'"{key}":{json.dumps(decoded[key])}'
                    encoded = int(decoded[key])
                    replacement_value = (
                        f"{encoded}.0000000000000001"
                        if variant == "rounded_float"
                        else f"{encoded}e0"
                    )
                    token = f'"{key}":{replacement_value}'
                else:
                    key = next(
                        key
                        for key, value in decoded.items()
                        if type(value) is float and value == 0.0
                    )
                    original = f'"{key}":{json.dumps(decoded[key])}'
                    token = f'"{key}":1e-999'
                changed = raw.replace(original, token, 1)
                assert changed != raw
                raw = changed
            connection.execute(
                "UPDATE contest_official_results SET tiebreaks_json=? WHERE id=?",
                (raw, row["id"]),
            )
        _restore_exact_legacy_epoch(connection, contest_id)

    with store._tx() as connection:
        connection.execute("BEGIN")
        before_changes = connection.total_changes
        with pytest.raises(OfficialRepairError) as raised:
            plan_official_results_repair(connection, contest_id)
        assert raised.value.code == "raw_authority_invalid"
        reports = scan_official_results_repairs(connection)
        assert reports == [
            {
                "contest_id": contest_id,
                "eligibility": "blocked",
                "reason_code": "raw_authority_invalid",
            }
        ]
        assert connection.total_changes == before_changes


@pytest.mark.parametrize(
    "raw",
    ["-0", "8.0000000000000001", "8e0", "1e-999"],
)
def test_strict_json_loader_rejects_noncanonical_numeric_tokens(raw):
    with pytest.raises(OfficialRepairError) as raised:
        official_repair_module._decode_strict_json(
            raw, max_chars=64, label="numeric lexical test"
        )
    assert raised.value.code == "raw_authority_invalid"


_STAGE_CONTENT_MUTATIONS = (
    "points",
    "wins",
    "draws",
    "losses",
    "delta_total",
    "rank_swap",
    "tiebreak_negative_zero",
    *(f"tiebreak_{key}" for key in _TIEBREAK_KEYS),
)


def _sync_stage_result_to_official(
    connection: sqlite3.Connection,
    contest_id: int,
    *,
    stage_idx: int,
    seed: int,
) -> None:
    if stage_idx != 1:
        return
    row = _stage_result_row(
        connection, contest_id, stage_idx=stage_idx, seed=seed
    )
    tiebreaks = json.loads(row["payload_json"])["tiebreaks"]
    connection.execute(
        "UPDATE contest_official_results SET rank=?,points=?,tiebreaks_json=? "
        "WHERE contest_id=? AND entry_id=?",
        (
            row["rank_in_group"],
            row["points"],
            json.dumps(tiebreaks, ensure_ascii=False),
            contest_id,
            row["entry_id"],
        ),
    )


def _mutate_stage_result_content(
    connection: sqlite3.Connection,
    contest_id: int,
    *,
    stage_idx: int,
    mutation: str,
) -> None:
    seed = 6 if stage_idx == 0 and mutation == "rank_swap" else (
        4 if stage_idx == 0 else 1
    )
    row = _stage_result_row(
        connection, contest_id, stage_idx=stage_idx, seed=seed
    )
    if mutation == "rank_swap":
        other_seed = 7 if stage_idx == 0 else 3
        other = _stage_result_row(
            connection, contest_id, stage_idx=stage_idx, seed=other_seed
        )
        connection.execute(
            "UPDATE contest_stage_results SET rank_in_group=? WHERE id=?",
            (other["rank_in_group"], row["id"]),
        )
        connection.execute(
            "UPDATE contest_stage_results SET rank_in_group=? WHERE id=?",
            (row["rank_in_group"], other["id"]),
        )
        _sync_stage_result_to_official(
            connection, contest_id, stage_idx=stage_idx, seed=seed
        )
        _sync_stage_result_to_official(
            connection, contest_id, stage_idx=stage_idx, seed=other_seed
        )
        return

    payload = json.loads(row["payload_json"])
    tiebreaks = payload["tiebreaks"]
    if mutation == "points":
        connection.execute(
            "UPDATE contest_stage_results SET points=points+0.25 WHERE id=?",
            (row["id"],),
        )
        tiebreaks["points"] += 0.25
    elif mutation in {"wins", "draws", "losses"}:
        connection.execute(
            f"UPDATE contest_stage_results SET {mutation}={mutation}+1 WHERE id=?",
            (row["id"],),
        )
    elif mutation == "delta_total":
        connection.execute(
            "UPDATE contest_stage_results SET delta_total=delta_total+1 WHERE id=?",
            (row["id"],),
        )
        tiebreaks["normalized_delta"] += 1.0
    elif mutation == "tiebreak_negative_zero":
        tiebreaks["head_to_head"] = -0.0
    else:
        key = mutation.removeprefix("tiebreak_")
        assert key in _TIEBREAK_KEYS
        if key in _TIEBREAK_KEYS[:6]:
            tiebreaks[key] += 0.25
        else:
            tiebreaks[key] += 1
    connection.execute(
        "UPDATE contest_stage_results SET payload_json=? WHERE id=?",
        (json.dumps(payload, separators=(",", ":")), row["id"]),
    )
    _sync_stage_result_to_official(
        connection, contest_id, stage_idx=stage_idx, seed=seed
    )


def test_planner_binds_every_reviewed_stage_result_value_without_writes(
    exact_legacy_nine_eight,
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for stage_idx in (0, 1):
            for mutation in _STAGE_CONTENT_MUTATIONS:
                connection.execute("SAVEPOINT stage_content_mutation")
                _mutate_stage_result_content(
                    connection,
                    contest_id,
                    stage_idx=stage_idx,
                    mutation=mutation,
                )
                _restore_exact_legacy_epoch(connection, contest_id)
                before_changes = connection.total_changes
                with pytest.raises(OfficialRepairError) as raised:
                    plan_official_results_repair(connection, contest_id)
                assert raised.value.code == "raw_authority_invalid", (
                    stage_idx,
                    mutation,
                    raised.value.code,
                )
                assert connection.total_changes == before_changes
                connection.execute("ROLLBACK TO stage_content_mutation")
                connection.execute("RELEASE stage_content_mutation")


def test_planner_binds_reviewed_match_outcome_without_replaying_ranking(
    exact_legacy_nine_eight,
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        match = connection.execute(
            "SELECT id,winner,result FROM matches_pencil WHERE contest_id=? "
            "ORDER BY id LIMIT 1",
            (contest_id,),
        ).fetchone()
        result = json.loads(match["result"])
        result["deltas"] = [-result["deltas"][0], -result["deltas"][1]]
        result["normalized_delta"] = -result["normalized_delta"]
        connection.execute(
            "UPDATE matches_pencil SET winner=?,result=? WHERE id=?",
            (
                1 - match["winner"],
                json.dumps(result, separators=(",", ":")),
                match["id"],
            ),
        )
        _restore_exact_legacy_epoch(connection, contest_id)

    with store._tx() as connection:
        connection.execute("BEGIN")
        before_changes = connection.total_changes
        with pytest.raises(OfficialRepairError) as raised:
            plan_official_results_repair(connection, contest_id)
        assert connection.total_changes == before_changes
    assert raised.value.code == "raw_authority_invalid"


@pytest.mark.parametrize(
    "table",
    [
        "contest_entries",
        "contest_pairings",
        "contest_stage_results",
    ],
)
@pytest.mark.parametrize("raw_group_id", [b"", b"not-text", "unexpected-group"])
def test_planner_rejects_noncanonical_group_authority_without_truthiness_coercion(
    exact_legacy_nine_eight, table, raw_group_id
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"UPDATE {table} SET group_id=? WHERE id=(SELECT id FROM {table} "
            "WHERE contest_id=? ORDER BY id LIMIT 1)",
            (
                sqlite3.Binary(raw_group_id)
                if isinstance(raw_group_id, bytes)
                else raw_group_id,
                contest_id,
            ),
        )
        observed = connection.execute(
            f"SELECT typeof(group_id),group_id FROM {table} "
            "WHERE contest_id=? ORDER BY id LIMIT 1",
            (contest_id,),
        ).fetchone()
        assert observed is not None
        assert observed[0] == (
            "blob" if isinstance(raw_group_id, bytes) else "text"
        )
        assert observed[1] == raw_group_id
        _restore_exact_legacy_epoch(connection, contest_id)

    with store._tx() as connection:
        connection.execute("BEGIN")
        with pytest.raises(OfficialRepairError) as raised:
            plan_official_results_repair(connection, contest_id)
    assert raised.value.code == "raw_authority_invalid"


@pytest.mark.parametrize("table", ["contest_pairings", "contest_stage_results"])
@pytest.mark.parametrize("raw_stage_key", [b"swiss", "unexpected-stage"])
def test_planner_rejects_noncanonical_materialized_stage_keys(
    exact_legacy_nine_eight, table, raw_stage_key
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"UPDATE {table} SET stage_key=? WHERE id=(SELECT id FROM {table} "
            "WHERE contest_id=? AND stage_idx=0 ORDER BY id LIMIT 1)",
            (
                sqlite3.Binary(raw_stage_key)
                if isinstance(raw_stage_key, bytes)
                else raw_stage_key,
                contest_id,
            ),
        )
        _restore_exact_legacy_epoch(connection, contest_id)

    with store._tx() as connection:
        connection.execute("BEGIN")
        with pytest.raises(OfficialRepairError) as raised:
            plan_official_results_repair(connection, contest_id)
    assert raised.value.code == "raw_authority_invalid"


def test_planner_rejects_blob_frozen_stage_json_without_bytes_coercion(
    exact_legacy_nine_eight,
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        stages_json = connection.execute(
            "SELECT stages_json FROM contests WHERE id=?", (contest_id,)
        ).fetchone()[0]
        assert isinstance(stages_json, str)
        connection.execute(
            "UPDATE contests SET stages_json=? WHERE id=?",
            (sqlite3.Binary(stages_json.encode("utf-8")), contest_id),
        )
        _restore_exact_legacy_epoch(connection, contest_id)

    with store._tx() as connection:
        connection.execute("BEGIN")
        with pytest.raises(OfficialRepairError) as raised:
            plan_official_results_repair(connection, contest_id)
    assert raised.value.code == "raw_authority_invalid"


_RAW_AUTHORITY_MUTATIONS = (
    "contest_contract_blob",
    "contest_protocol_wrong",
    "contest_rating_pool_wrong",
    "contest_time_control_present",
    "entry_seed_blob",
    "entry_seed_negative",
    "entry_seed_duplicate",
    "entry_seed_swap",
    "entry_bot_null",
    "entry_bot_missing",
    "entry_bot_duplicate",
    "entry_bot_other_owner",
    "entry_bot_cross_game",
    "entry_bot_protocol_wrong",
    "stage_bot_null",
    "stage_bot_missing",
    "stage_bot_other_owner",
    "stage_entry_wrong",
    "pairing_entry_blob",
    "pairing_bot_blob",
    "pairing_color_blob",
    "pairing_color_negative",
    "pairing_series_size_wrong",
    "pairing_series_index_wrong",
    "pairing_seed_blob",
    "pairing_seed_present",
    "pairing_tiebreak_group_blob",
    "pairing_tiebreak_game_negative",
    "swiss_bracket_present",
    "ko_bracket_mirror",
    "swiss_round_blob",
    "swiss_round_out_of_range",
    "published_at_blob",
    "published_at_noncanonical",
    "published_after_match_created",
    "scheduled_at_blob",
    "scheduled_at_noncanonical",
    "scheduled_presence_missing",
    "scheduled_presence_extra",
    "scheduled_after_match_created",
    "real_version_null",
    "real_version_blob",
    "real_version_missing",
    "real_version_wrong_bot",
    "real_version_protocol_wrong",
    "bye_version_present",
    "match_owner_null",
    "match_owner_other",
    "match_owner_blob",
    "match_ruleset_wrong",
    "match_protocol_blob",
    "match_rating_pool_wrong",
    "match_contest_wrong",
    "match_game_wrong",
    "match_type_wrong",
    "match_bot_wrong",
    "match_human_user",
    "match_human_seat",
    "match_seed_zero",
    "match_seed_blob",
    "match_created_blob",
    "match_created_noncanonical",
    "match_started_null",
    "match_started_blob",
    "match_started_noncanonical",
    "match_ended_null",
    "match_ended_blob",
    "match_ended_noncanonical",
    "match_time_order",
    "match_likes_blob",
    "match_likes_negative",
    "match_likes_invalid",
    "match_views_blob",
    "match_views_negative",
    "match_views_invalid",
    "match_reason_wrong",
    "match_reason_blob",
    "match_technical_loss_wrong",
    "match_technical_loss_blob",
    "match_result_round_wrong",
    "match_result_round_bool",
    "match_result_normalized_int",
    "match_result_normalized_wrong",
    "match_result_normalized_bool",
    "config_time_control_key",
    "config_version_missing",
    "config_version_wrong",
    "config_version_blob",
    "config_duplicate_true",
    "config_duplicate_nonbool",
    "config_duplicate_json_key",
    "config_seed_key",
    "config_environment_wrong",
    "config_local_agent_present",
    "config_profile_wrong",
    "config_request_short",
    "config_request_wrong_prefix",
    "config_request_control",
    "config_request_duplicate",
    "config_rating_eligible_true",
    "config_rating_reason_wrong",
    "config_extra_key",
)


def _rewrite_match_config(
    connection: sqlite3.Connection,
    contest_id: int,
    mutation,
    *,
    offset: int = 0,
) -> None:
    row = connection.execute(
        "SELECT id,match_config FROM matches_pencil WHERE contest_id=? "
        "ORDER BY id LIMIT 1 OFFSET ?",
        (contest_id, offset),
    ).fetchone()
    assert row is not None
    config = json.loads(row["match_config"])
    assert isinstance(config, dict)
    mutation(config)
    connection.execute(
        "UPDATE matches_pencil SET match_config=? WHERE id=?",
        (
            json.dumps(config, ensure_ascii=False, separators=(",", ":")),
            row["id"],
        ),
    )


def _rewrite_match_result(
    connection: sqlite3.Connection,
    contest_id: int,
    mutation,
) -> None:
    row = connection.execute(
        "SELECT id,result FROM matches_pencil WHERE contest_id=? ORDER BY id LIMIT 1",
        (contest_id,),
    ).fetchone()
    assert row is not None
    result = json.loads(row["result"])
    assert isinstance(result, dict)
    mutation(result)
    connection.execute(
        "UPDATE matches_pencil SET result=? WHERE id=?",
        (
            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
            row["id"],
        ),
    )


def _mutate_raw_authority(
    connection: sqlite3.Connection, contest_id: int, mutation: str
) -> None:
    first_entry = (
        "(SELECT id FROM contest_entries WHERE contest_id=? ORDER BY id LIMIT 1)"
    )
    second_entry = (
        "(SELECT id FROM contest_entries WHERE contest_id=? ORDER BY id LIMIT 1 "
        "OFFSET 1)"
    )
    real_pairing = (
        "(SELECT id FROM contest_pairings WHERE contest_id=? "
        "AND match_id IS NOT NULL ORDER BY id LIMIT 1)"
    )
    bye_pairing = (
        "(SELECT id FROM contest_pairings WHERE contest_id=? "
        "AND match_id IS NULL ORDER BY id LIMIT 1)"
    )
    first_match = (
        "(SELECT match_id FROM contest_pairings WHERE contest_id=? "
        "AND match_id IS NOT NULL ORDER BY id LIMIT 1)"
    )

    if mutation == "contest_contract_blob":
        connection.execute(
            "UPDATE contests SET ruleset_version=? WHERE id=?",
            (sqlite3.Binary(b"pencil_ccgc_v1"), contest_id),
        )
    elif mutation == "contest_protocol_wrong":
        connection.execute(
            "UPDATE contests SET protocol_version='wrong' WHERE id=?", (contest_id,)
        )
    elif mutation == "contest_rating_pool_wrong":
        connection.execute(
            "UPDATE contests SET rating_pool_id='wrong' WHERE id=?", (contest_id,)
        )
    elif mutation == "contest_time_control_present":
        connection.execute(
            "UPDATE contests SET time_control_id='pencil_per_side_total_900s_v1' "
            "WHERE id=?",
            (contest_id,),
        )
    elif mutation == "entry_seed_blob":
        connection.execute(
            f"UPDATE contest_entries SET seed=? WHERE id={first_entry}",
            (sqlite3.Binary(b"1"), contest_id),
        )
    elif mutation == "entry_seed_negative":
        connection.execute(
            f"UPDATE contest_entries SET seed=-1 WHERE id={first_entry}",
            (contest_id,),
        )
    elif mutation == "entry_seed_duplicate":
        connection.execute(
            f"UPDATE contest_entries SET seed=1 WHERE id={second_entry}",
            (contest_id,),
        )
    elif mutation == "entry_seed_swap":
        rows = connection.execute(
            "SELECT id,seed FROM contest_entries WHERE contest_id=? "
            "ORDER BY id LIMIT 2",
            (contest_id,),
        ).fetchall()
        assert len(rows) == 2
        connection.execute(
            "UPDATE contest_entries SET seed=99 WHERE id=?", (rows[0]["id"],)
        )
        connection.execute(
            "UPDATE contest_entries SET seed=? WHERE id=?",
            (rows[0]["seed"], rows[1]["id"]),
        )
        connection.execute(
            "UPDATE contest_entries SET seed=? WHERE id=?",
            (rows[1]["seed"], rows[0]["id"]),
        )
    elif mutation == "entry_bot_null":
        connection.execute(
            f"UPDATE contest_entries SET bot_id=NULL WHERE id={first_entry}",
            (contest_id,),
        )
    elif mutation == "entry_bot_missing":
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='trigger' "
            "AND name='trg_contest_entries_live_bot_update'"
        ).fetchone()[0]
        assert isinstance(trigger_sql, str)
        connection.execute("DROP TRIGGER trg_contest_entries_live_bot_update")
        connection.execute(
            f"UPDATE contest_entries SET bot_id=(SELECT MAX(id)+1000 FROM bots) "
            f"WHERE id={first_entry}",
            (contest_id,),
        )
        connection.execute(trigger_sql)
    elif mutation == "entry_bot_duplicate":
        connection.execute(
            f"UPDATE contest_entries SET bot_id=(SELECT bot_id FROM "
            f"contest_entries WHERE id={first_entry}) WHERE id={second_entry}",
            (contest_id, contest_id),
        )
    elif mutation == "entry_bot_other_owner":
        connection.execute(
            "UPDATE bots SET owner_id=(SELECT user_id FROM contest_entries "
            "WHERE contest_id=? ORDER BY id LIMIT 1 OFFSET 1) WHERE id=(SELECT "
            "bot_id FROM contest_entries WHERE contest_id=? ORDER BY id LIMIT 1)",
            (contest_id, contest_id),
        )
    elif mutation == "entry_bot_cross_game":
        connection.execute(
            "UPDATE bots SET game_id='gomoku' WHERE id=(SELECT bot_id FROM "
            "contest_entries WHERE contest_id=? ORDER BY id LIMIT 1)",
            (contest_id,),
        )
    elif mutation == "entry_bot_protocol_wrong":
        connection.execute(
            "UPDATE bots SET protocol_version='wrong' WHERE id=(SELECT bot_id "
            "FROM contest_entries WHERE contest_id=? ORDER BY id LIMIT 1)",
            (contest_id,),
        )
    elif mutation == "stage_bot_null":
        connection.execute(
            "UPDATE contest_stage_results SET bot_id=NULL WHERE id=(SELECT id "
            "FROM contest_stage_results WHERE contest_id=? ORDER BY id LIMIT 1)",
            (contest_id,),
        )
    elif mutation == "stage_bot_missing":
        connection.execute(
            "UPDATE contest_stage_results SET bot_id=(SELECT MAX(id)+1000 FROM bots) "
            "WHERE id=(SELECT id FROM contest_stage_results WHERE contest_id=? "
            "ORDER BY id LIMIT 1)",
            (contest_id,),
        )
    elif mutation == "stage_bot_other_owner":
        connection.execute(
            "UPDATE contest_stage_results AS target SET bot_id=(SELECT other.bot_id "
            "FROM contest_entries other JOIN contest_entries owner "
            "ON owner.id=target.entry_id WHERE other.contest_id=? "
            "AND other.user_id<>owner.user_id ORDER BY other.id LIMIT 1) "
            "WHERE target.id=(SELECT id FROM contest_stage_results "
            "WHERE contest_id=? ORDER BY id LIMIT 1)",
            (contest_id, contest_id),
        )
    elif mutation == "stage_entry_wrong":
        connection.execute(
            "UPDATE contest_stage_results SET entry_id=(SELECT id FROM "
            "contest_entries WHERE contest_id=? AND eliminated=1) WHERE id=(SELECT "
            "id FROM contest_stage_results WHERE contest_id=? AND stage_idx=1 "
            "ORDER BY id LIMIT 1)",
            (contest_id, contest_id),
        )
    elif mutation == "pairing_entry_blob":
        connection.execute(
            f"UPDATE contest_pairings SET entry_a_id=? WHERE id={real_pairing}",
            (sqlite3.Binary(b"1"), contest_id),
        )
    elif mutation == "pairing_bot_blob":
        connection.execute(
            f"UPDATE contest_pairings SET bot_a_id=? WHERE id={real_pairing}",
            (sqlite3.Binary(b"1"), contest_id),
        )
    elif mutation == "pairing_color_blob":
        connection.execute(
            f"UPDATE contest_pairings SET color_first=? WHERE id={real_pairing}",
            (sqlite3.Binary(b"0"), contest_id),
        )
    elif mutation == "pairing_color_negative":
        connection.execute(
            f"UPDATE contest_pairings SET color_first=-1 WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "pairing_series_size_wrong":
        connection.execute(
            f"UPDATE contest_pairings SET series_size=2 WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "pairing_series_index_wrong":
        connection.execute(
            f"UPDATE contest_pairings SET series_index=2 WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "pairing_seed_blob":
        connection.execute(
            f"UPDATE contest_pairings SET pairing_seed=? WHERE id={real_pairing}",
            (sqlite3.Binary(b""), contest_id),
        )
    elif mutation == "pairing_seed_present":
        connection.execute(
            f"UPDATE contest_pairings SET pairing_seed=1 WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "pairing_tiebreak_group_blob":
        connection.execute(
            f"UPDATE contest_pairings SET tiebreak_group=? WHERE id={real_pairing}",
            (sqlite3.Binary(b"0"), contest_id),
        )
    elif mutation == "pairing_tiebreak_game_negative":
        connection.execute(
            "PRAGMA ignore_check_constraints=ON"
        )
        connection.execute(
            f"UPDATE contest_pairings SET tiebreak_game=-1 WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "swiss_bracket_present":
        connection.execute(
            "UPDATE contest_pairings SET bracket_slot=0 WHERE id=(SELECT id FROM "
            "contest_pairings WHERE contest_id=? AND stage_idx=0 ORDER BY id LIMIT 1)",
            (contest_id,),
        )
    elif mutation == "ko_bracket_mirror":
        mirrored = connection.execute(
            "SELECT id,bracket_slot FROM contest_pairings WHERE contest_id=? "
            "AND stage_idx=1 AND round_num=1 AND bracket_slot IN (0,1) "
            "ORDER BY bracket_slot",
            (contest_id,),
        ).fetchall()
        assert len(mirrored) == 2
        connection.execute(
            "UPDATE contest_pairings SET bracket_slot=99 WHERE id=?",
            (mirrored[0]["id"],),
        )
        connection.execute(
            "UPDATE contest_pairings SET bracket_slot=0 WHERE id=?",
            (mirrored[1]["id"],),
        )
        connection.execute(
            "UPDATE contest_pairings SET bracket_slot=1 WHERE id=?",
            (mirrored[0]["id"],),
        )
    elif mutation == "swiss_round_blob":
        connection.execute(
            "UPDATE contest_pairings SET round_num=? WHERE id=(SELECT id FROM "
            "contest_pairings WHERE contest_id=? AND stage_idx=0 ORDER BY id LIMIT 1)",
            (sqlite3.Binary(b"1"), contest_id),
        )
    elif mutation == "swiss_round_out_of_range":
        connection.execute(
            "UPDATE contest_pairings SET round_num=5 WHERE id=(SELECT id FROM "
            "contest_pairings WHERE contest_id=? AND stage_idx=0 ORDER BY id LIMIT 1)",
            (contest_id,),
        )
    elif mutation == "published_at_blob":
        connection.execute(
            f"UPDATE contest_pairings SET published_at=? WHERE id={real_pairing}",
            (sqlite3.Binary(b"2026-09-03T00:00:00"), contest_id),
        )
    elif mutation == "published_at_noncanonical":
        connection.execute(
            f"UPDATE contest_pairings SET published_at='2026-09-03 00:00:00' "
            f"WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "published_after_match_created":
        connection.execute(
            f"UPDATE contest_pairings SET published_at='9999-12-31T23:59:59' "
            f"WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "scheduled_at_blob":
        connection.execute(
            f"UPDATE contest_pairings SET scheduled_at=? WHERE id={real_pairing}",
            (sqlite3.Binary(b""), contest_id),
        )
    elif mutation == "scheduled_at_noncanonical":
        connection.execute(
            f"UPDATE contest_pairings SET scheduled_at='2026-09-03T00:00:00Z' "
            f"WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "scheduled_presence_missing":
        connection.execute(
            "UPDATE contest_pairings SET scheduled_at=NULL WHERE id=(SELECT id "
            "FROM contest_pairings WHERE contest_id=? AND match_id IS NOT NULL "
            "AND scheduled_at IS NOT NULL ORDER BY id LIMIT 1)",
            (contest_id,),
        )
    elif mutation == "scheduled_presence_extra":
        connection.execute(
            "UPDATE contest_pairings SET scheduled_at=published_at WHERE id=(SELECT id "
            "FROM contest_pairings WHERE contest_id=? AND match_id IS NOT NULL "
            "AND scheduled_at IS NULL ORDER BY id LIMIT 1)",
            (contest_id,),
        )
    elif mutation == "scheduled_after_match_created":
        connection.execute(
            "UPDATE contest_pairings SET scheduled_at='9999-12-31T23:59:59' "
            "WHERE id=(SELECT id FROM contest_pairings WHERE contest_id=? "
            "AND match_id IS NOT NULL AND scheduled_at IS NOT NULL "
            "ORDER BY id LIMIT 1)",
            (contest_id,),
        )
    elif mutation == "real_version_null":
        connection.execute(
            f"UPDATE contest_pairings SET bot_a_version_id=NULL "
            f"WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "real_version_blob":
        connection.execute(
            f"UPDATE contest_pairings SET bot_a_version_id=? "
            f"WHERE id={real_pairing}",
            (sqlite3.Binary(b"1"), contest_id),
        )
    elif mutation == "real_version_missing":
        connection.execute(
            f"UPDATE contest_pairings SET bot_a_version_id=(SELECT MAX(id)+1000 "
            f"FROM bot_versions) WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "real_version_wrong_bot":
        connection.execute(
            f"UPDATE contest_pairings SET bot_a_version_id=(SELECT id FROM "
            f"bot_versions WHERE bot_id<>contest_pairings.bot_a_id ORDER BY id "
            f"LIMIT 1) WHERE id={real_pairing}",
            (contest_id,),
        )
    elif mutation == "real_version_protocol_wrong":
        connection.execute(
            f"UPDATE bot_versions SET protocol_version='wrong' WHERE id=(SELECT "
            f"bot_a_version_id FROM contest_pairings WHERE id={real_pairing})",
            (contest_id,),
        )
    elif mutation == "bye_version_present":
        connection.execute(
            f"UPDATE contest_pairings SET bot_a_version_id=(SELECT id FROM "
            f"bot_versions WHERE bot_id=contest_pairings.bot_a_id "
            f"ORDER BY id DESC LIMIT 1) "
            f"WHERE id={bye_pairing}",
            (contest_id,),
        )
    elif mutation.startswith("match_result_"):
        def change_result(result: dict) -> None:
            if mutation == "match_result_round_wrong":
                result["rounds_played"] += 1
            elif mutation == "match_result_round_bool":
                result["rounds_played"] = True
            elif mutation == "match_result_normalized_int":
                result["normalized_delta"] = int(result["normalized_delta"])
            elif mutation == "match_result_normalized_wrong":
                result["normalized_delta"] += 0.5
            elif mutation == "match_result_normalized_bool":
                result["normalized_delta"] = True
            else:  # pragma: no cover - parameter list and mutator stay paired
                raise AssertionError(mutation)

        _rewrite_match_result(connection, contest_id, change_result)
    elif mutation.startswith("match_"):
        assignment = {
            "match_owner_null": "owner_id=NULL",
            "match_owner_other": (
                "owner_id=(SELECT entry.user_id FROM contest_entries entry "
                "JOIN contests contest ON contest.id=entry.contest_id WHERE "
                f"entry.contest_id={contest_id} AND entry.user_id<>"
                "contest.organizer_id ORDER BY entry.id LIMIT 1)"
            ),
            "match_owner_blob": "owner_id=X'31'",
            "match_ruleset_wrong": "ruleset_version='wrong'",
            "match_protocol_blob": "protocol_version=X'70656e63696c5f78795f7631'",
            "match_rating_pool_wrong": "rating_pool_id='wrong'",
            "match_contest_wrong": "contest_id=NULL",
            "match_game_wrong": "game_id='gomoku'",
            "match_type_wrong": "match_type='manual'",
            "match_bot_wrong": "bot_a_id=bot_b_id",
            "match_human_user": (
                "human_user_id=(SELECT user_id FROM contest_entries WHERE contest_id="
                f"{contest_id} ORDER BY id LIMIT 1)"
            ),
            "match_human_seat": "human_seat=0",
            "match_seed_zero": "match_seed=0",
            "match_seed_blob": "match_seed=X''",
            "match_created_blob": "created_at=X'323032362d30382d30315430303a30303a3030'",
            "match_created_noncanonical": "created_at='2026-08-01 00:00:00'",
            "match_started_null": "started_at=NULL",
            "match_started_blob": "started_at=X'323032362d30382d30315430303a30303a3030'",
            "match_started_noncanonical": "started_at='2026-08-01T00:00:00Z'",
            "match_ended_null": "ended_at=NULL",
            "match_ended_blob": "ended_at=X'323032362d30382d30315430303a30303a3030'",
            "match_ended_noncanonical": "ended_at='2026-08-01T00:00'",
            "match_time_order": "started_at='9999-12-31T23:59:59'",
            "match_likes_blob": "likes_count=X'30'",
            "match_likes_negative": "likes_count=-1",
            "match_likes_invalid": "likes_count='true'",
            "match_views_blob": "views_count=X'30'",
            "match_views_negative": "views_count=-1",
            "match_views_invalid": "views_count='false'",
            "match_reason_wrong": "reason='timeout'",
            "match_reason_blob": "reason=X'6d616a6f72697479'",
            "match_technical_loss_wrong": "technical_loss=1",
            "match_technical_loss_blob": "technical_loss=X'30'",
        }[mutation]
        connection.execute(
            f"UPDATE matches_pencil SET {assignment} WHERE id={first_match}",
            (contest_id,),
        )
    elif mutation == "config_duplicate_json_key":
        row = connection.execute(
            "SELECT id,match_config FROM matches_pencil WHERE contest_id=? "
            "ORDER BY id LIMIT 1",
            (contest_id,),
        ).fetchone()
        assert row is not None and row["match_config"].endswith("}")
        connection.execute(
            "UPDATE matches_pencil SET match_config=? WHERE id=?",
            (row["match_config"][:-1] + ',"duplicate":false}', row["id"]),
        )
    elif mutation == "config_request_duplicate":
        first = json.loads(
            connection.execute(
                "SELECT match_config FROM matches_pencil WHERE contest_id=? "
                "ORDER BY id LIMIT 1",
                (contest_id,),
            ).fetchone()[0]
        )["_execution_request_id"]
        _rewrite_match_config(
            connection,
            contest_id,
            lambda config: config.__setitem__("_execution_request_id", first),
            offset=1,
        )
    else:
        def change_config(config: dict) -> None:
            if mutation == "config_time_control_key":
                config["time_control_id"] = None
            elif mutation == "config_version_missing":
                config.pop("_bot_a_version_id")
            elif mutation == "config_version_wrong":
                config["_bot_a_version_id"] = config["_bot_b_version_id"]
            elif mutation == "config_version_blob":
                config["_bot_a_version_id"] = "1"
            elif mutation == "config_duplicate_true":
                config["duplicate"] = True
            elif mutation == "config_duplicate_nonbool":
                config["duplicate"] = 0
            elif mutation == "config_seed_key":
                config["match_seed"] = 1
            elif mutation == "config_environment_wrong":
                config["_bot_a_environment"] = "platform_low"
            elif mutation == "config_local_agent_present":
                config["_bot_a_local_agent_id"] = 1
            elif mutation == "config_profile_wrong":
                config["_execution_profile_version"] = 2
            elif mutation == "config_request_short":
                config["_execution_request_id"] = "short"
            elif mutation == "config_request_wrong_prefix":
                config["_execution_request_id"] = "bad_" + ("x" * 24)
            elif mutation == "config_request_control":
                config["_execution_request_id"] = "req_" + ("x" * 23) + "\n"
            elif mutation == "config_rating_eligible_true":
                config["_rating_eligible"] = True
            elif mutation == "config_rating_reason_wrong":
                config["_rating_reason"] = "eligible"
            elif mutation == "config_extra_key":
                config["unexpected"] = None
            else:  # pragma: no cover - parameter list and mutator stay paired
                raise AssertionError(mutation)

        _rewrite_match_config(connection, contest_id, change_config)


@pytest.mark.parametrize("mutation", _RAW_AUTHORITY_MUTATIONS)
def test_planner_rejects_each_raw_authority_mutation_without_writes(
    exact_legacy_nine_eight, mutation
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("BEGIN IMMEDIATE")
        _mutate_raw_authority(connection, contest_id, mutation)
        _restore_exact_legacy_epoch(connection, contest_id)

    with store._tx() as connection:
        connection.execute("BEGIN")
        before_changes = connection.total_changes
        with pytest.raises(OfficialRepairError) as raised:
            plan_official_results_repair(connection, contest_id)
        assert connection.total_changes == before_changes
    assert raised.value.code == (
        "match_affiliation_invalid"
        if mutation in {"match_contest_wrong", "match_game_wrong", "match_type_wrong"}
        else "raw_authority_invalid"
    )


def test_planner_accepts_historical_stage_bot_owned_by_the_same_entry_user(
    exact_legacy_nine_eight, tmp_path
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    entry = store.list_contest_entries(contest_id)[0]
    historical_binary = tmp_path / "historical-stage-bot"
    historical_binary.write_bytes(b"historical stage identity")
    historical = store.create_bot(
        entry["user_id"],
        "historical-stage-bot",
        binary_path=str(historical_binary),
        format="elf",
        game_id="pencil",
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_stage_results SET bot_id=? WHERE contest_id=? "
            "AND entry_id=? AND stage_idx=0",
            (historical["id"], contest_id, entry["id"]),
        )
        _restore_exact_legacy_epoch(connection, contest_id)

    assert _plan(store, contest_id).eligible is True


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ("future_pairing", "future_or_partial_artifact"),
        ("missing_pairing", "match_binding_invalid"),
        ("match_index", "match_index_invalid"),
        ("missing_snapshot", "future_or_partial_artifact"),
        ("official_prefix", "official_prefix_mismatch"),
        ("eliminated", "roster_invalid"),
        ("foreign_match", "match_affiliation_invalid"),
        ("cross_contest_pairing", "match_affiliation_invalid"),
        ("pairing_status", "settlement_invalid"),
        ("orphan_match", "match_binding_invalid"),
        ("schema_trigger", "schema_invalid"),
        ("schema_trigger_definition", "schema_invalid"),
        ("duplicate_sequence", "official_sequence_invalid"),
        ("near_max_sequence", "official_sequence_invalid"),
        ("max_sequence", "official_sequence_invalid"),
        ("queued_job", "active_execution"),
    ],
)
def test_planner_rejects_every_noncanonical_authority_mutation(
    exact_legacy_nine_eight, mutation, reason_code
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    if mutation == "orphan_match":
        entries = store.list_contest_entries(contest_id)
        store.create_match(
            "official-repair-orphan",
            entries[0]["bot_id"],
            entries[1]["bot_id"],
            owner_id=entries[0]["user_id"],
            contest_id=contest_id,
            match_type="contest",
            game_id="pencil",
            match_config={"time_control_id": "pencil_default"},
        )
    elif mutation == "cross_contest_pairing":
        source = store.get_contest(contest_id)
        assert source is not None
        other = store.create_contest(
            "foreign match reference",
            source["organizer_id"],
            game_id="pencil",
            template_id="pencil_round_robin",
            stages_json="[]",
        )
        with store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            # A damaged legacy schema may have lost the uniqueness index.  The
            # repair planner must still prove durable affiliation from rows,
            # rather than treating the index as its authority.
            connection.execute("DROP INDEX idx_contest_pairings_match_unique")
            pairing = connection.execute(
                "SELECT * FROM contest_pairings WHERE contest_id=? "
                "AND match_id IS NOT NULL ORDER BY id LIMIT 1",
                (contest_id,),
            ).fetchone()
            assert pairing is not None
            connection.execute(
                "INSERT INTO contest_pairings("
                "contest_id,round_num,entry_a_id,entry_b_id,bot_a_id,bot_b_id,"
                "bot_a_version_id,bot_b_version_id,pairing_seed,published_at,"
                "scheduled_at,match_id,status,stage_idx,stage_key,group_id,"
                "bracket_slot,color_first,series_index,series_size,"
                "tiebreak_group,tiebreak_game) "
                "SELECT ?,round_num,entry_a_id,entry_b_id,bot_a_id,bot_b_id,"
                "bot_a_version_id,bot_b_version_id,pairing_seed,published_at,"
                "scheduled_at,match_id,status,stage_idx,stage_key,group_id,"
                "bracket_slot,color_first,series_index,series_size,"
                "tiebreak_group,tiebreak_game FROM contest_pairings WHERE id=?",
                (other["id"], pairing["id"]),
            )
            _restore_exact_legacy_epoch(connection, contest_id)
    else:
        with store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if mutation == "future_pairing":
                connection.execute(
                    "UPDATE contest_pairings SET stage_idx=2 WHERE id=("
                    "SELECT id FROM contest_pairings WHERE contest_id=? "
                    "AND stage_idx=0 ORDER BY id LIMIT 1)",
                    (contest_id,),
                )
            elif mutation == "missing_pairing":
                connection.execute(
                    "DELETE FROM contest_pairings WHERE id=(SELECT id FROM "
                    "contest_pairings WHERE contest_id=? AND match_id IS NOT NULL "
                    "ORDER BY id LIMIT 1)",
                    (contest_id,),
                )
            elif mutation == "match_index":
                connection.execute(
                    "UPDATE matches_index SET game_id='gomoku' WHERE id=(SELECT "
                    "match_id FROM contest_pairings WHERE contest_id=? "
                    "AND match_id IS NOT NULL ORDER BY id LIMIT 1)",
                    (contest_id,),
                )
            elif mutation == "missing_snapshot":
                connection.execute(
                    "DELETE FROM contest_stage_results WHERE id=(SELECT id FROM "
                    "contest_stage_results WHERE contest_id=? AND stage_idx=0 "
                    "ORDER BY id LIMIT 1)",
                    (contest_id,),
                )
            elif mutation == "official_prefix":
                connection.execute(
                    "UPDATE contest_official_results SET points=points+0.5 "
                    "WHERE contest_id=? AND rank=1",
                    (contest_id,),
                )
            elif mutation == "eliminated":
                connection.execute(
                    "UPDATE contest_entries SET eliminated=0 WHERE contest_id=? "
                    "AND eliminated=1",
                    (contest_id,),
                )
            elif mutation == "foreign_match":
                connection.execute(
                    "UPDATE matches_pencil SET contest_id=NULL WHERE id=(SELECT "
                    "match_id FROM contest_pairings WHERE contest_id=? "
                    "AND match_id IS NOT NULL ORDER BY id LIMIT 1)",
                    (contest_id,),
                )
            elif mutation == "pairing_status":
                connection.execute(
                    "UPDATE contest_pairings SET status='running' WHERE id=("
                    "SELECT id FROM contest_pairings WHERE contest_id=? "
                    "AND match_id IS NOT NULL ORDER BY id LIMIT 1)",
                    (contest_id,),
                )
            elif mutation == "schema_trigger":
                connection.execute(
                    "DROP TRIGGER trg_contest_stage_results_lifecycle_revision_update"
                )
            elif mutation == "schema_trigger_definition":
                connection.execute(
                    "DROP TRIGGER trg_contest_pairing_topology_insert"
                )
                connection.execute(
                    "CREATE TRIGGER trg_contest_pairing_topology_insert "
                    "AFTER INSERT ON contest_pairings BEGIN SELECT 1; END"
                )
            elif mutation == "duplicate_sequence":
                sequence = connection.execute(
                    "SELECT seq FROM sqlite_sequence "
                    "WHERE name='contest_official_results'"
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO sqlite_sequence(name,seq) VALUES(?,?)",
                    ("contest_official_results", sequence),
                )
            elif mutation in {"near_max_sequence", "max_sequence"}:
                connection.execute(
                    "UPDATE sqlite_sequence SET seq=? "
                    "WHERE name='contest_official_results'",
                    (
                        2**63 - 2
                        if mutation == "near_max_sequence"
                        else 2**63 - 1,
                    ),
                )
            elif mutation == "queued_job":
                pairing = connection.execute(
                    "SELECT * FROM contest_pairings WHERE contest_id=? "
                    "AND stage_idx=1 AND bot_a_id IS NOT NULL "
                    "AND bot_b_id IS NOT NULL ORDER BY id LIMIT 1",
                    (contest_id,),
                ).fetchone()
                contest = connection.execute(
                    "SELECT * FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
                assert pairing is not None and contest is not None
                connection.execute(
                    "INSERT INTO execution_jobs("
                    "public_id,source,status,priority,owner_user_id,game_id,"
                    "ruleset_version,protocol_version,rating_pool_id,match_type,"
                    "bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id,"
                    "bot_a_environment,bot_b_environment,contest_id,"
                    "contest_pairing_id,match_config,rated,rating_reason,"
                    "sandbox_units,host_cpu_millis,host_memory_mb,created_at) "
                    "VALUES(?, 'contest','queued',50,?,?,?,?,?,'contest',"
                    "?,?,?,?, 'platform_high','platform_high',?,?, '{}',0,'',"
                    "2,4000,4096,'2026-09-03T00:00:00')",
                    (
                        "official-repair-queued",
                        contest["organizer_id"],
                        contest["game_id"],
                        contest["ruleset_version"],
                        contest["protocol_version"],
                        contest["rating_pool_id"],
                        pairing["bot_a_id"],
                        pairing["bot_b_id"],
                        pairing["bot_a_version_id"],
                        pairing["bot_b_version_id"],
                        contest_id,
                        pairing["id"],
                    ),
                )
            _restore_exact_legacy_epoch(connection, contest_id)
    with store._tx() as connection:
        connection.execute("BEGIN")
        with pytest.raises(OfficialRepairError) as raised:
            plan_official_results_repair(connection, contest_id)
    assert raised.value.code == reason_code


def test_highest_allowed_preimage_sequence_applies_to_a_valid_postimage(
    exact_legacy_nine_eight,
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    highest_allowed_preimage = 2**63 - 3
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE sqlite_sequence SET seq=? "
            "WHERE name='contest_official_results'",
            (highest_allowed_preimage,),
        )
    path = _prepare_offline_apply(store)
    plan = _raw_plan(path, contest_id)
    before_header = _stable_header_snapshot(path)
    assert plan.eligible is True
    assert plan._missing_row is not None
    assert plan._missing_row["id"] == highest_allowed_preimage + 1
    with offline_official_repair_guard(path) as guard:
        repaired = apply_official_results_repair(
            path,
            contest_id,
            **_reviewed_apply_kwargs(path, plan),
            guard=guard,
        )
    assert repaired.already_applied is True
    assert _stable_header_snapshot(path) == before_header
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT seq FROM sqlite_sequence "
            "WHERE name='contest_official_results'"
        ).fetchone() == (highest_allowed_preimage + 1,)


@pytest.mark.parametrize(
    "assignment",
    [
        "published_stage_pairing_count=1,pairing_topology_revision=0,"
        "sealed_pairing_topology_revision=NULL",
        "published_stage_pairing_count=NULL,pairing_topology_revision=1,"
        "sealed_pairing_topology_revision=NULL",
        "published_stage_pairing_count=NULL,pairing_topology_revision=0,"
        "sealed_pairing_topology_revision=0",
    ],
)
def test_planner_accepts_only_exact_observed_legacy_epoch(
    exact_legacy_nine_eight, assignment
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"UPDATE contests SET {assignment} WHERE id=?",
            (contest_id,),
        )
    with store._tx() as connection:
        connection.execute("BEGIN")
        with pytest.raises(OfficialRepairError) as raised:
            plan_official_results_repair(connection, contest_id)
    assert raised.value.code == "legacy_epoch_invalid"


def _table_digest(connection: sqlite3.Connection, table: str) -> str:
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info({table})")]
    rows = [tuple(row) for row in connection.execute(f"SELECT * FROM {table}")]
    return hashlib.sha256(
        json.dumps(
            {"columns": columns, "rows": rows},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


_STABLE_HEADER_PRAGMAS = (
    "user_version",
    "application_id",
    "schema_version",
    "page_size",
    "encoding",
    "auto_vacuum",
    "default_cache_size",
)


def _stable_header_snapshot(path: Path) -> tuple[tuple[str, object], ...]:
    with sqlite3.connect(path) as connection:
        return tuple(
            (name, connection.execute(f"PRAGMA {name}").fetchone()[0])
            for name in _STABLE_HEADER_PRAGMAS
        )


def _prepare_offline_apply(store: Store) -> Path:
    path = Path(store.path).resolve()
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE execution_control SET dispatcher_state='stopped',accepting=0,"
            "auto_enabled=0,deployment_drain_requested=1 WHERE singleton=1"
        )
    store.close()
    path.chmod(0o600)
    lock_path = Path(str(path) + ".execution-dispatcher.lock")
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    return path


def _reviewed_apply_kwargs(path: Path, plan) -> dict:
    metadata = path.stat()
    backup = path.with_name(f".{path.name}.{plan.plan_digest[:12]}.cold.db")
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    backup_metadata = backup.stat()
    return {
        "expected_authority_digest": plan.authority_digest,
        "expected_old_official_digest": plan.old_official_digest,
        "expected_plan_digest": plan.plan_digest,
        "expected_repaired_official_digest": plan.repaired_official_digest,
        "expected_source_business_digest": plan.source_business_digest,
        "expected_post_business_digest": plan.expected_post_business_digest,
        "expected_target_stat": (
            metadata.st_dev,
            metadata.st_ino,
            metadata.st_size,
            metadata.st_mtime_ns,
            metadata.st_ctime_ns,
            metadata.st_mode,
            metadata.st_uid,
            metadata.st_nlink,
        ),
        "expected_target_preimage_sha256": hashlib.sha256(
            path.read_bytes()
        ).hexdigest(),
        "cold_backup_path": backup,
        "expected_backup_stat": (
            backup_metadata.st_dev,
            backup_metadata.st_ino,
            backup_metadata.st_size,
            backup_metadata.st_mtime_ns,
            backup_metadata.st_ctime_ns,
            backup_metadata.st_mode,
            backup_metadata.st_uid,
            backup_metadata.st_nlink,
        ),
        "expected_backup_sha256": hashlib.sha256(
            backup.read_bytes()
        ).hexdigest(),
    }


def _offline_cli_common(path: Path, backup: Path, contest_id: int) -> list[str]:
    return [
        "contest-official-repair",
        "--db",
        str(path),
        "--backup",
        str(backup),
        "--contest-id",
        str(contest_id),
        "--confirm-db",
        str(path),
        "--confirm-contest-id",
        str(contest_id),
        "--confirm-service-stopped",
        "--confirm-maintenance-ready",
        "--confirm-cold-backup",
    ]


def _reviewed_cli_args(report: dict) -> list[str]:
    return [
        "--expect-authority-digest",
        report["authority_digest"],
        "--expect-old-official-digest",
        report["old_official_digest"],
        "--expect-repaired-official-digest",
        report["repaired_official_digest"],
        "--expect-plan-digest",
        report["plan_digest"],
        "--expect-source-business-digest",
        report["source_business_digest"],
        "--expect-post-business-digest",
        report["expected_post_business_digest"],
        "--expect-target-preimage-sha256",
        report["target_preimage_sha256"],
    ]


def _add_valid_imported_finished_contest(
    store: Store, source_contest_id: int
) -> int:
    source_entries = store.list_contest_entries(source_contest_id)[:2]
    assert len(source_entries) == 2
    contest = store.create_contest(
        "valid inventory control",
        source_entries[0]["user_id"],
        game_id="pencil",
        template_id="pencil_round_robin",
        stages_json="[]",
    )
    imported_entries = []
    for source in source_entries:
        imported_entries.append(
            store.add_contest_entry(
                contest["id"], source["user_id"], source["bot_id"]
            )
        )
    for rank, entry in enumerate(imported_entries, start=1):
        store.upsert_official_result(
            contest["id"],
            entry["id"],
            rank,
            stage_idx=0,
            points=float(3 - rank),
            bot_id=entry["bot_id"],
            user_id=entry["user_id"],
        )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status='finished',official_results_ready=1 "
            "WHERE id=?",
            (contest["id"],),
        )
    return int(contest["id"])


def test_inventory_separates_general_validity_from_special_repair_eligibility(
    exact_legacy_nine_eight,
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    valid_id = _add_valid_imported_finished_contest(store, contest_id)
    with store._tx() as connection:
        connection.execute("BEGIN")
        before = scan_official_results_repairs(connection)
    by_id = {row["contest_id"]: row for row in before}
    assert by_id[contest_id]["eligibility"] == "repairable"
    assert by_id[valid_id]["eligibility"] == "valid"

    path = _prepare_offline_apply(store)
    plan = _raw_plan(path, contest_id)
    with offline_official_repair_guard(path) as guard:
        apply_official_results_repair(
            path,
            contest_id,
            **_reviewed_apply_kwargs(path, plan),
            guard=guard,
        )
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("BEGIN")
        after = scan_official_results_repairs(connection)
    assert {row["contest_id"]: row["eligibility"] for row in after} == {
        contest_id: "valid",
        valid_id: "valid",
    }


def test_inventory_reports_malformed_non_target_ready_table_as_blocked(
    exact_legacy_nine_eight, monkeypatch
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    invalid_id = _add_valid_imported_finished_contest(store, contest_id)
    original = official_repair_module._validate_complete_official_results

    def reject_malformed_non_target(rows, **kwargs):
        if kwargs.get("contest_id") == invalid_id:
            raise TypeError("simulated malformed legacy projection")
        return original(rows, **kwargs)

    monkeypatch.setattr(
        official_repair_module,
        "_validate_complete_official_results",
        reject_malformed_non_target,
    )
    with store._tx() as connection:
        connection.execute("BEGIN")
        reports = scan_official_results_repairs(connection)
    by_id = {row["contest_id"]: row for row in reports}
    assert by_id[contest_id]["eligibility"] == "repairable"
    assert by_id[invalid_id] == {
        "contest_id": invalid_id,
        "eligibility": "blocked",
        "reason_code": "legacy_epoch_invalid",
    }


def test_cli_inventory_is_valid_for_other_templates_before_and_after_repair(
    exact_legacy_nine_eight, tmp_path
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    valid_id = _add_valid_imported_finished_contest(store, contest_id)
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-inventory.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    scan_args = [
        "contest-official-repair",
        "--db",
        str(path),
        "--backup",
        str(backup),
        "--scan-all",
        "--confirm-db",
        str(path),
        "--confirm-service-stopped",
        "--confirm-maintenance-ready",
        "--confirm-cold-backup",
    ]
    runner = CliRunner()
    before = runner.invoke(cli_app, scan_args)
    assert before.exit_code == 0, before.output
    before_report = json.loads(before.output)
    assert before_report["repairable"] == 1
    assert before_report["blocked"] == 0
    assert before_report["valid"] == 1
    assert {row["contest_id"] for row in before_report["contests"]} == {
        contest_id,
        valid_id,
    }

    dry = runner.invoke(
        cli_app, _offline_cli_common(path, backup, contest_id)
    )
    assert dry.exit_code == 0, dry.output
    dry_report = json.loads(dry.output)
    applied = runner.invoke(
        cli_app,
        [
            *_offline_cli_common(path, backup, contest_id),
            "--apply",
            *_reviewed_cli_args(dry_report),
        ],
    )
    assert applied.exit_code == 0, applied.output
    after = runner.invoke(cli_app, scan_args)
    assert after.exit_code == 0, after.output
    after_report = json.loads(after.output)
    assert after_report["repairable"] == 0
    assert after_report["blocked"] == 0
    assert after_report["valid"] == 2


def test_cli_inventory_blocks_repair_when_any_other_ready_table_is_invalid(
    exact_legacy_nine_eight, tmp_path
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    invalid_id = _add_valid_imported_finished_contest(store, contest_id)
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM contest_official_results WHERE contest_id=? AND rank=2",
            (invalid_id,),
        )
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-blocked-inventory.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    scan_args = [
        "contest-official-repair",
        "--db",
        str(path),
        "--backup",
        str(backup),
        "--scan-all",
        "--confirm-db",
        str(path),
        "--confirm-service-stopped",
        "--confirm-maintenance-ready",
        "--confirm-cold-backup",
    ]
    runner = CliRunner()
    scan = runner.invoke(cli_app, scan_args)
    assert scan.exit_code == 1
    report = json.loads(scan.output)
    assert report["repairable"] == 1
    assert report["blocked"] == 1
    assert {
        row["contest_id"]
        for row in report["contests"]
        if row["eligibility"] == "blocked"
    } == {invalid_id}

    before = path.read_bytes()
    dry = runner.invoke(
        cli_app, _offline_cli_common(path, backup, contest_id)
    )
    assert dry.exit_code != 0
    assert "ready" in dry.output and "正式排名" in dry.output
    assert path.read_bytes() == before

    target_plan = _raw_plan(path, contest_id)
    with offline_official_repair_guard(path) as guard:
        with pytest.raises(OfficialRepairError) as raised:
            apply_official_results_repair(
                path,
                contest_id,
                **_reviewed_apply_kwargs(path, target_plan),
                guard=guard,
            )
    assert raised.value.code == "inventory_blocked"
    assert path.read_bytes() == before


def test_apply_inserts_only_missing_tail_and_preserves_every_other_business_row(
    exact_legacy_nine_eight,
):
    store, contest_id, full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    plan = _raw_plan(path, contest_id)
    before_header = _stable_header_snapshot(path)
    with sqlite3.connect(path) as connection:
        before_official = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM contest_official_results WHERE contest_id=? ORDER BY id",
                (contest_id,),
            )
        ]
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            if row[0] != "contest_official_results"
        ]
        before_other = {table: _table_digest(connection, table) for table in tables}
        before_lifecycle = connection.execute(
            "SELECT published_stage_pairing_count,pairing_topology_revision,"
            "sealed_pairing_topology_revision,status,official_results_ready "
            "FROM contests WHERE id=?",
            (contest_id,),
        ).fetchone()

    with offline_official_repair_guard(path) as guard:
        repaired = apply_official_results_repair(
            path,
            contest_id,
            **_reviewed_apply_kwargs(path, plan),
            guard=guard,
        )
    assert repaired.already_applied is True
    assert _stable_header_snapshot(path) == before_header

    with sqlite3.connect(path) as connection:
        after_official = [
            tuple(row)
            for row in connection.execute(
                "SELECT * FROM contest_official_results WHERE contest_id=? ORDER BY id",
                (contest_id,),
            )
        ]
        assert after_official[:8] == before_official
        assert len(after_official) == 9
        assert after_official[-1][2] == full_official[-1]["entry_id"]
        assert {table: _table_digest(connection, table) for table in tables} == before_other
        assert connection.execute(
            "SELECT published_stage_pairing_count,pairing_topology_revision,"
            "sealed_pairing_topology_revision,status,official_results_ready "
            "FROM contests WHERE id=?",
            (contest_id,),
        ).fetchone() == before_lifecycle
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_repair_restores_one_consistent_store_api_csv_and_private_export(
    exact_legacy_nine_eight,
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with pytest.raises(ValueError, match="冻结名册成员不一致"):
        store.list_official_results(contest_id)
    path = _prepare_offline_apply(store)
    plan = _raw_plan(path, contest_id)
    with offline_official_repair_guard(path) as guard:
        apply_official_results_repair(
            path,
            contest_id,
            **_reviewed_apply_kwargs(path, plan),
            guard=guard,
        )

    reopened = Store(str(path))
    try:
        official = reopened.list_official_results(contest_id)
        assert [row["rank"] for row in official] == list(range(1, 10))
        assert {row["entry_id"] for row in official} == {
            row["id"] for row in reopened.list_contest_entries(contest_id)
        }
    finally:
        reopened.close()

    app = create_app(db_path=str(path))
    try:
        client = TestClient(app)
        public_json = client.get(f"/api/contests/{contest_id}/official-results")
        public_csv = client.get(
            f"/api/contests/{contest_id}/official-results?format=csv"
        )
        assert public_json.status_code == public_csv.status_code == 200
        assert [row["rank"] for row in public_json.json()["results"]] == list(
            range(1, 10)
        )
        public_rows = list(
            csv.DictReader(io.StringIO(public_csv.content.decode("utf-8-sig")))
        )
        assert [int(row["rank"]) for row in public_rows] == list(range(1, 10))

        _user, token = app.state.auth.authenticate(
            "official-repair-user-0", "fixture-password"
        )
        private_export = client.get(
            f"/api/contests/{contest_id}/export?format=csv&schema=2",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert private_export.status_code == 200
        private_rows = list(
            csv.DictReader(
                io.StringIO(private_export.content.decode("utf-8-sig"))
            )
        )
        assert len(private_rows) == 9
        assert {
            row["成绩状态(result_status)"] for row in private_rows
        } == {"正式成绩"}
    finally:
        app.state.store.close()


@pytest.mark.parametrize(
    "digest_field",
    [
        "authority_digest",
        "old_official_digest",
        "plan_digest",
        "repaired_official_digest",
        "source_business_digest",
        "post_business_digest",
    ],
)
def test_apply_digest_cas_failure_is_zero_write(
    exact_legacy_nine_eight, digest_field
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    plan = _raw_plan(path, contest_id)
    kwargs = _reviewed_apply_kwargs(path, plan)
    expected_key = (
        "expected_post_business_digest"
        if digest_field == "post_business_digest"
        else f"expected_{digest_field}"
    )
    kwargs[expected_key] = "0" * 64
    before = path.read_bytes()
    with offline_official_repair_guard(path) as guard:
        with pytest.raises(OfficialRepairError, match="digest"):
            apply_official_results_repair(
                path, contest_id, guard=guard, **kwargs
            )
    assert path.read_bytes() == before


def test_apply_insert_trigger_failure_rolls_back_whole_transaction(
    exact_legacy_nine_eight,
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    with store._tx() as connection:
        connection.execute(
            "CREATE TRIGGER fail_official_tail BEFORE INSERT ON "
            "contest_official_results WHEN NEW.contest_id=%d AND NEW.rank=9 "
            "BEGIN SELECT RAISE(ABORT,'injected official tail failure'); END"
            % contest_id
        )
    path = _prepare_offline_apply(store)
    plan = _raw_plan(path, contest_id)
    before = path.read_bytes()
    with offline_official_repair_guard(path) as guard:
        with pytest.raises(sqlite3.DatabaseError, match="injected official tail failure"):
            apply_official_results_repair(
                path,
                contest_id,
                **_reviewed_apply_kwargs(path, plan),
                guard=guard,
            )
    assert path.read_bytes() == before


def test_cli_apply_rechecks_exact_raw_preimage_inside_write_transaction(
    exact_legacy_nine_eight, tmp_path, monkeypatch
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-preimage-cas.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    common = _offline_cli_common(path, backup, contest_id)
    runner = CliRunner()
    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    reviewed = _reviewed_cli_args(json.loads(dry.output))
    original_apply = official_repair_module.apply_official_results_repair
    original_inode = path.stat().st_ino
    with sqlite3.connect(path) as connection:
        before_user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        before_official_count = connection.execute(
            "SELECT COUNT(*) FROM contest_official_results WHERE contest_id=?",
            (contest_id,),
        ).fetchone()[0]
    assert before_official_count == 8
    injected = False

    def drift_header_then_apply(*args, **kwargs):
        nonlocal injected
        assert not injected
        injected = True
        with sqlite3.connect(path) as connection:
            connection.execute(f"PRAGMA user_version={before_user_version + 1}")
        assert path.stat().st_ino == original_inode
        assert not any(
            Path(str(path) + suffix).exists()
            for suffix in ("-wal", "-shm", "-journal")
        )
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(
        official_repair_module,
        "apply_official_results_repair",
        drift_header_then_apply,
    )
    applied = runner.invoke(cli_app, [*common, "--apply", *reviewed])
    assert injected is True
    assert applied.exit_code != 0
    assert "preimage" in applied.output or "变化" in applied.output
    assert path.stat().st_ino == original_inode
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            before_user_version + 1,
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM contest_official_results WHERE contest_id=?",
            (contest_id,),
        ).fetchone() == (8,)


@pytest.mark.parametrize("drift", ["sidecar", "replace_inode", "header"])
def test_cli_apply_rechecks_exact_cold_backup_inside_write_transaction(
    exact_legacy_nine_eight, tmp_path, monkeypatch, drift
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-backup-preimage-cas.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    common = _offline_cli_common(path, backup, contest_id)
    runner = CliRunner()
    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    reviewed = _reviewed_cli_args(json.loads(dry.output))
    original_apply = official_repair_module.apply_official_results_repair
    before_target = path.read_bytes()
    before_target_mtime = path.stat().st_mtime_ns
    injected = False

    def drift_backup_then_apply(*args, **kwargs):
        nonlocal injected
        assert not injected
        injected = True
        if drift == "sidecar":
            Path(str(backup) + "-wal").write_bytes(b"late cold-backup WAL")
        elif drift == "replace_inode":
            replacement = backup.with_name(backup.name + ".replacement")
            shutil.copyfile(backup, replacement)
            replacement.chmod(0o400)
            replacement.replace(backup)
        else:
            backup.chmod(0o600)
            with sqlite3.connect(backup) as connection:
                user_version = connection.execute(
                    "PRAGMA user_version"
                ).fetchone()[0]
                connection.execute(f"PRAGMA user_version={user_version + 1}")
            backup.chmod(0o400)
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(
        official_repair_module,
        "apply_official_results_repair",
        drift_backup_then_apply,
    )
    applied = runner.invoke(cli_app, [*common, "--apply", *reviewed])
    assert injected is True
    assert applied.exit_code != 0
    assert path.read_bytes() == before_target
    assert path.stat().st_mtime_ns == before_target_mtime
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM contest_official_results WHERE contest_id=?",
            (contest_id,),
        ).fetchone() == (8,)


def test_cli_verify_and_retry_reject_postimage_header_drift(
    exact_legacy_nine_eight, tmp_path
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-header-contract.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    common = _offline_cli_common(path, backup, contest_id)
    runner = CliRunner()
    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    reviewed = _reviewed_cli_args(json.loads(dry.output))
    before_header = _stable_header_snapshot(path)
    applied = runner.invoke(cli_app, [*common, "--apply", *reviewed])
    assert applied.exit_code == 0, applied.output
    assert _stable_header_snapshot(path) == before_header

    with sqlite3.connect(path) as connection:
        connection.execute(
            f"PRAGMA user_version={dict(before_header)['user_version'] + 1}"
        )
    assert _stable_header_snapshot(path) != before_header
    for mode in ("--verify", "--apply"):
        rejected = runner.invoke(cli_app, [*common, mode, *reviewed])
        assert rejected.exit_code != 0, rejected.output
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT COUNT(*) FROM contest_official_results WHERE contest_id=?",
                (contest_id,),
            ).fetchone() == (9,)


def test_cli_success_output_is_emitted_while_dispatcher_flock_is_still_held(
    exact_legacy_nine_eight, tmp_path, monkeypatch
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-output-lock.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    common = _offline_cli_common(path, backup, contest_id)
    runner = CliRunner()
    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    reviewed = _reviewed_cli_args(json.loads(dry.output))
    original_echo = cli_module.typer.echo
    observed_locked = False

    def echo_while_locked(*args, **kwargs):
        nonlocal observed_locked
        lock_path = Path(str(path) + ".execution-dispatcher.lock")
        with lock_path.open("r+") as contender:
            try:
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed_locked = True
            else:
                fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
                pytest.fail("success output was emitted after releasing dispatcher flock")
        return original_echo(*args, **kwargs)

    monkeypatch.setattr(cli_module.typer, "echo", echo_while_locked)
    applied = runner.invoke(cli_app, [*common, "--apply", *reviewed])
    assert applied.exit_code == 0, applied.output
    assert observed_locked is True


def test_cli_broken_pipe_after_commit_is_recoverable_as_lost_output(
    exact_legacy_nine_eight, tmp_path, monkeypatch
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-broken-pipe.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    common = _offline_cli_common(path, backup, contest_id)
    runner = CliRunner()
    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    reviewed = _reviewed_cli_args(json.loads(dry.output))
    original_echo = cli_module.typer.echo
    observed_locked = False

    def broken_echo(*_args, **_kwargs):
        nonlocal observed_locked
        lock_path = Path(str(path) + ".execution-dispatcher.lock")
        with lock_path.open("r+") as contender:
            try:
                fcntl.flock(contender.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                observed_locked = True
            else:
                fcntl.flock(contender.fileno(), fcntl.LOCK_UN)
        raise BrokenPipeError("injected lost stdout")

    monkeypatch.setattr(cli_module.typer, "echo", broken_echo)
    lost = runner.invoke(cli_app, [*common, "--apply", *reviewed])
    assert lost.exit_code != 0
    assert observed_locked is True
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM contest_official_results WHERE contest_id=?",
            (contest_id,),
        ).fetchone() == (9,)

    monkeypatch.setattr(cli_module.typer, "echo", original_echo)
    verified = runner.invoke(cli_app, [*common, "--verify", *reviewed])
    assert verified.exit_code == 0, verified.output
    assert json.loads(verified.output)["mode"] == "verify"


def test_offline_cli_dry_run_apply_and_lost_output_retry_are_explicit_and_idempotent(
    exact_legacy_nine_eight, tmp_path, monkeypatch
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    preimage = hashlib.sha256(path.read_bytes()).hexdigest()
    common = _offline_cli_common(path, backup, contest_id)
    monkeypatch.setattr(
        Store,
        "__init__",
        lambda *_args, **_kwargs: pytest.fail(
            "offline repair CLI must not construct Store on any database"
        ),
    )
    runner = CliRunner()
    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    report = json.loads(dry.output)
    assert report["mode"] == "dry-run"
    assert report["eligibility"] == "repairable"
    assert report["target_preimage_sha256"] == preimage
    reviewed = _reviewed_cli_args(report)
    apply = runner.invoke(
        cli_app,
        [
            *common,
            "--apply",
            *reviewed,
        ],
    )
    assert apply.exit_code == 0, apply.output
    applied = json.loads(apply.output)
    assert applied["mode"] == "applied"
    assert applied["eligibility"] == "already_applied"
    applied_bytes = path.read_bytes()
    applied_mtime = path.stat().st_mtime_ns

    verify = runner.invoke(cli_app, [*common, "--verify", *reviewed])
    assert verify.exit_code == 0, verify.output
    verified = json.loads(verify.output)
    assert verified["mode"] == "verify"
    assert verified["eligibility"] == "already_applied"
    assert verified["zero_write"] is True
    assert path.read_bytes() == applied_bytes
    assert path.stat().st_mtime_ns == applied_mtime

    retry = runner.invoke(cli_app, [*common, "--apply", *reviewed])
    assert retry.exit_code == 0, retry.output
    retried = json.loads(retry.output)
    assert retried["mode"] == "already-applied"
    assert retried["zero_write"] is True
    assert path.read_bytes() == applied_bytes
    assert path.stat().st_mtime_ns == applied_mtime


def test_cli_apply_refuses_to_report_success_after_postcommit_target_drift(
    exact_legacy_nine_eight, tmp_path, monkeypatch
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-postcommit-drift.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    common = _offline_cli_common(path, backup, contest_id)
    runner = CliRunner()
    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    reviewed = _reviewed_cli_args(json.loads(dry.output))

    original_remove = cli_module._remove_cutover_plan_copy
    injected = False

    def remove_then_drift(copy_path: Path) -> None:
        nonlocal injected
        original_remove(copy_path)
        if not injected:
            injected = True
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE execution_control SET updated_at='postcommit drift' "
                    "WHERE singleton=1"
                )
                connection.commit()

    monkeypatch.setattr(cli_module, "_remove_cutover_plan_copy", remove_then_drift)
    applied = runner.invoke(cli_app, [*common, "--apply", *reviewed])
    assert applied.exit_code != 0
    assert "目标数据库" in applied.output and "变化" in applied.output


def test_cli_apply_rejects_drift_between_postcheck_and_file_baseline(
    exact_legacy_nine_eight, tmp_path, monkeypatch
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-postcheck-drift.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    common = _offline_cli_common(path, backup, contest_id)
    runner = CliRunner()
    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    reviewed = _reviewed_cli_args(json.loads(dry.output))

    original_validate = official_repair_module.validate_official_repair_inventory
    repaired_validations = 0

    def validate_then_drift(reports, selected_contest_id, *, repaired):
        nonlocal repaired_validations
        original_validate(reports, selected_contest_id, repaired=repaired)
        if not repaired:
            return
        repaired_validations += 1
        if repaired_validations != 2:
            return
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE execution_control SET updated_at='postcheck drift' "
                "WHERE singleton=1"
            )
            connection.commit()

    monkeypatch.setattr(
        official_repair_module,
        "validate_official_repair_inventory",
        validate_then_drift,
    )
    applied = runner.invoke(cli_app, [*common, "--apply", *reviewed])
    assert repaired_validations == 2
    assert applied.exit_code != 0
    assert "目标数据库" in applied.output and "变化" in applied.output


@pytest.mark.parametrize("drift", ["unrelated_business", "official_row_identity"])
def test_lost_output_retry_rejects_any_noncanonical_postimage(
    exact_legacy_nine_eight, tmp_path, drift
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-drift.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    common = _offline_cli_common(path, backup, contest_id)
    runner = CliRunner()
    dry = runner.invoke(cli_app, common)
    assert dry.exit_code == 0, dry.output
    report = json.loads(dry.output)
    reviewed = _reviewed_cli_args(report)
    applied = runner.invoke(cli_app, [*common, "--apply", *reviewed])
    assert applied.exit_code == 0, applied.output

    with sqlite3.connect(path) as connection:
        if drift == "unrelated_business":
            connection.execute(
                "UPDATE users SET xp=xp+1 WHERE id=(SELECT user_id FROM "
                "contest_entries WHERE contest_id=? ORDER BY id LIMIT 1)",
                (contest_id,),
            )
        else:
            row = connection.execute(
                "SELECT " + ",".join(
                    column for column in (
                        "contest_id",
                        "entry_id",
                        "stage_idx",
                        "rank",
                        "points",
                        "bot_id",
                        "user_id",
                        "group_id",
                        "rank_in_group",
                        "tiebreaks_json",
                        "awarded",
                    )
                )
                + " FROM contest_official_results WHERE contest_id=? AND rank=1",
                (contest_id,),
            ).fetchone()
            assert row is not None
            connection.execute(
                "DELETE FROM contest_official_results WHERE contest_id=? AND rank=1",
                (contest_id,),
            )
            connection.execute(
                "INSERT INTO contest_official_results("
                "contest_id,entry_id,stage_idx,rank,points,bot_id,user_id,"
                "group_id,rank_in_group,tiebreaks_json,awarded) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                tuple(row),
            )
        connection.commit()
    drifted_bytes = path.read_bytes()
    drifted_mtime = path.stat().st_mtime_ns
    retry = runner.invoke(cli_app, [*common, "--apply", *reviewed])
    assert retry.exit_code != 0
    assert "postimage" in retry.output
    assert path.read_bytes() == drifted_bytes
    assert path.stat().st_mtime_ns == drifted_mtime


@pytest.mark.parametrize("sidecar_owner", ["target", "backup"])
def test_cli_rejects_even_empty_sqlite_sidecars_without_writes(
    exact_legacy_nine_eight, tmp_path, sidecar_owner
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-sidecar.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    subject = path if sidecar_owner == "target" else backup
    sidecar = Path(str(subject) + "-shm")
    sidecar.touch()
    before = path.read_bytes()
    result = CliRunner().invoke(
        cli_app, _offline_cli_common(path, backup, contest_id)
    )
    assert result.exit_code != 0
    assert "sidecar" in result.output
    assert path.read_bytes() == before


@pytest.mark.parametrize("sidecar_owner", ["target", "backup"])
@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_cli_rejects_dangling_sqlite_sidecar_symlinks_without_writes(
    exact_legacy_nine_eight, tmp_path, sidecar_owner, suffix
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-dangling-sidecar.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    owner = path if sidecar_owner == "target" else backup
    Path(str(owner) + suffix).symlink_to(tmp_path / "missing-sidecar-target")
    before = path.read_bytes()
    result = CliRunner().invoke(
        cli_app, _offline_cli_common(path, backup, contest_id)
    )
    assert result.exit_code != 0
    assert "sidecar" in result.output
    assert path.read_bytes() == before


def test_cli_requires_read_only_cold_backup_and_existing_unlocked_inode(
    exact_legacy_nine_eight, tmp_path
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-mode.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o600)
    common = _offline_cli_common(path, backup, contest_id)
    runner = CliRunner()
    wrong_mode = runner.invoke(cli_app, common)
    assert wrong_mode.exit_code != 0
    assert "权限" in wrong_mode.output

    backup.chmod(0o400)
    lock_path = Path(str(path) + ".execution-dispatcher.lock")
    lock_path.unlink()
    missing = runner.invoke(cli_app, common)
    assert missing.exit_code != 0
    assert "lock" in missing.output
    lock_path.touch(mode=0o600)
    lock_path.chmod(0o600)
    fd = lock_path.open("r+")
    try:
        fcntl.flock(fd.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        held = runner.invoke(cli_app, common)
        assert held.exit_code != 0
        assert "仍持有" in held.output
    finally:
        fcntl.flock(fd.fileno(), fcntl.LOCK_UN)
        fd.close()


@pytest.mark.parametrize("alias_owner", ["target", "backup"])
def test_cli_rejects_symlinked_database_paths_without_writes(
    exact_legacy_nine_eight, tmp_path, alias_owner
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-canonical.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    target_arg = path
    backup_arg = backup
    if alias_owner == "target":
        target_arg = tmp_path / "target-link.db"
        target_arg.symlink_to(path)
    else:
        backup_arg = tmp_path / "backup-link.db"
        backup_arg.symlink_to(backup)
    before = path.read_bytes()
    result = CliRunner().invoke(
        cli_app,
        _offline_cli_common(target_arg, backup_arg, contest_id),
    )
    assert result.exit_code != 0
    assert "canonical" in result.output or "symlink" in result.output
    assert path.read_bytes() == before


def test_cli_dry_run_requires_byte_exact_cold_backup(
    exact_legacy_nine_eight, tmp_path
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    backup = tmp_path / "official-repair-stale.cold.db"
    shutil.copyfile(path, backup)
    with sqlite3.connect(backup) as connection:
        connection.execute(
            "UPDATE execution_control SET updated_at='stale cold backup' "
            "WHERE singleton=1"
        )
        connection.commit()
    backup.chmod(0o400)
    before = path.read_bytes()
    result = CliRunner().invoke(
        cli_app, _offline_cli_common(path, backup, contest_id)
    )
    assert result.exit_code != 0
    assert "逐字节" in result.output
    assert path.read_bytes() == before


def test_cli_dry_run_rejects_clean_wal_header_before_plan_review(
    exact_legacy_nine_eight, tmp_path
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    assert path.read_bytes()[18:20] == b"\x02\x02"
    assert all(
        not Path(str(path) + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    )
    backup = tmp_path / "official-repair-wal-header.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    before = path.read_bytes()
    result = CliRunner().invoke(
        cli_app, _offline_cli_common(path, backup, contest_id)
    )
    assert result.exit_code != 0
    assert "journal_mode=delete" in result.output
    assert path.read_bytes() == before


def test_guard_detects_lock_inode_replacement_before_success(
    exact_legacy_nine_eight, tmp_path
):
    store, _contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    lock_path = Path(str(path) + ".execution-dispatcher.lock")
    original = tmp_path / "original-lock"
    with pytest.raises(RuntimeError, match="inode"):
        with offline_official_repair_guard(path):
            lock_path.rename(original)
            lock_path.touch(mode=0o600)
            lock_path.chmod(0o600)


def test_guard_detects_target_inode_replacement_before_success(
    exact_legacy_nine_eight, tmp_path
):
    store, _contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    original = tmp_path / "original-target.db"
    with pytest.raises(RuntimeError, match="inode"):
        with offline_official_repair_guard(path):
            path.rename(original)
            shutil.copyfile(original, path)
            path.chmod(0o600)


@pytest.mark.parametrize(
    "mutation_sql",
    [
        "UPDATE execution_control SET accepting=1 WHERE singleton=1",
        "UPDATE execution_control SET deployment_drain_requested=0 WHERE singleton=1",
        "UPDATE execution_control SET auto_enabled=1 WHERE singleton=1",
        "UPDATE execution_control SET dispatcher_state='paused' WHERE singleton=1",
        "UPDATE execution_control SET dispatcher_state='running' WHERE singleton=1",
        "UPDATE matches_pencil SET status='running' WHERE id=(SELECT match_id "
        "FROM contest_pairings WHERE match_id IS NOT NULL ORDER BY id LIMIT 1)",
    ],
)
def test_cli_confirmations_cannot_bypass_durable_maintenance_state(
    exact_legacy_nine_eight, tmp_path, mutation_sql
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    path = _prepare_offline_apply(store)
    with sqlite3.connect(path) as connection:
        connection.execute(mutation_sql)
        connection.commit()
    backup = tmp_path / "official-repair-maintenance.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    before = path.read_bytes()
    result = CliRunner().invoke(
        cli_app, _offline_cli_common(path, backup, contest_id)
    )
    assert result.exit_code != 0
    assert "维护" in result.output or "running Match" in result.output
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "blocker", ["active_job", "active_attempt", "active_lease", "launch"]
)
def test_cli_rejects_durable_runtime_blockers_even_after_stop_confirmations(
    exact_legacy_nine_eight, tmp_path, blocker
):
    store, contest_id, _full_official = exact_legacy_nine_eight
    lease_agent_id = None
    if blocker == "active_lease":
        entry = store.list_contest_entries(contest_id)[0]
        agent = store.create_local_ai_agent(
            owner_id=entry["user_id"],
            bot_id=entry["bot_id"],
            label="official repair blocker",
            public_id="official_repair_blocker_agent",
            token_hash="hash",
            token_hint="hint",
        )
        lease_agent_id = agent["id"]
    path = _prepare_offline_apply(store)
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        if blocker in {"active_job", "active_attempt"}:
            pairing = connection.execute(
                "SELECT p.*,c.organizer_id,c.ruleset_version,c.protocol_version,"
                "c.rating_pool_id FROM contest_pairings p JOIN contests c "
                "ON c.id=p.contest_id WHERE p.contest_id=? "
                "AND p.match_id IS NOT NULL ORDER BY p.id LIMIT 1",
                (contest_id,),
            ).fetchone()
            assert pairing is not None
            cursor = connection.execute(
                "INSERT INTO execution_jobs("
                "public_id,source,status,priority,owner_user_id,game_id,"
                "ruleset_version,protocol_version,rating_pool_id,match_type,"
                "bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id,"
                "bot_a_environment,bot_b_environment,match_config,rated,"
                "rating_reason,sandbox_units,host_cpu_millis,host_memory_mb,"
                "current_match_id,attempt_count,cleanup_state,created_at,claimed_at,"
                "terminal_at) "
                "VALUES('official-repair-active-job','manual',?,50,?,"
                "'pencil',?,?,?,'challenge',?,?,?,?,"
                "'platform_low','platform_low','{}',0,'',2,2000,1024,?,1,"
                "'none','2026-09-03T00:00:00',?,?)",
                (
                    "starting" if blocker == "active_job" else "cancelled",
                    pairing["organizer_id"],
                    pairing["ruleset_version"],
                    pairing["protocol_version"],
                    pairing["rating_pool_id"],
                    pairing["bot_a_id"],
                    pairing["bot_b_id"],
                    pairing["bot_a_version_id"],
                    pairing["bot_b_version_id"],
                    pairing["match_id"] if blocker == "active_job" else None,
                    (
                        "2026-09-03T00:00:00"
                        if blocker == "active_job"
                        else None
                    ),
                    (
                        None
                        if blocker == "active_job"
                        else "2026-09-03T00:00:00"
                    ),
                ),
            )
            if blocker == "active_attempt":
                connection.execute(
                    "INSERT INTO execution_job_attempts("
                    "job_id,attempt_no,match_id,status,events_observed,created_at) "
                    "VALUES(?,1,'official-repair-stale-attempt','starting',0,"
                    "'2026-09-03T00:00:00')",
                    (cursor.lastrowid,),
                )
        elif blocker == "active_lease":
            assert lease_agent_id is not None
            connection.execute(
                "INSERT INTO local_ai_leases("
                "agent_id,job_public_id,attempt_no,seat,status,acquired_at) "
                "VALUES(?,'official-repair-active-lease',1,0,'active',"
                "'2026-09-03T00:00:00')",
                (lease_agent_id,),
            )
        else:
            connection.execute(
                "UPDATE docker_launch_journal SET state='creating',"
                "launch_token='repair-blocker',instance_key='repair-blocker',"
                "owner_kind='preflight',job_public_id='repair-blocker',"
                "attempt_no=1,slot=0,container_name='repair-blocker',"
                "host_boot_id='repair-blocker',updated_at='2026-09-03T00:00:00' "
                "WHERE singleton=1"
            )
        connection.commit()
    backup = tmp_path / f"official-repair-{blocker}.cold.db"
    shutil.copyfile(path, backup)
    backup.chmod(0o400)
    before = path.read_bytes()
    result = CliRunner().invoke(
        cli_app, _offline_cli_common(path, backup, contest_id)
    )
    assert result.exit_code != 0
    assert "active" in result.output
    assert path.read_bytes() == before
