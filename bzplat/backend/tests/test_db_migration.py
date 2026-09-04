"""全面解耦 PR3：DB 迁移测试——matches 拆每游戏表 + matches_index + ratings/rating_history 加 game_id。

验证：
1. 新库 schema 正确（三张 per-game 表 + matches_index + ratings 复合 PK + rating_history.game_id）
2. matches 路由：create/get/update/list/count_stats/like/incr_view 经 matches_index 正确
3. ratings per-game：ensure/get/update/history 按 (bot_id, game_id)
4. 跨游戏 UNION 查询（list_matches 无 game_id、count_stats）正确
5. 旧库迁移：旧单表 matches 被丢弃（对局数据不保留），用户/bot/赛事数据保留；
   ratings 加 game_id 维度回填；contest_pairings.match_id 清空
"""
from __future__ import annotations

import gc
import json
import os
import sqlite3

import pytest

from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    CONTEST_ENTRY_PAGE_INDEX_SQL,
    CONTEST_PAIRING_SCHEDULE_INDEX_SQL,
    CONTEST_PAIRING_SYNC_INDEX_SQL,
    EXECUTION_CLAIM_CONTEST_ORDER_INDEX_SQL,
    EXECUTION_CLAIM_SOURCE_ORDER_INDEX_SQL,
    EXECUTION_CONTEST_DISPATCH_GAP_INDEX_SQL,
    CONTEST_SOURCE_NAVIGATION_ALL_INDEX_SQL,
    CONTEST_SOURCE_NAVIGATION_OWNER_INDEX_SQL,
    CONTEST_SOURCE_NAVIGATION_PUBLIC_INDEX_SQL,
    CONTEST_SOURCE_PROTECTED_INDEX_SQL,
    CONTEST_SOURCE_DEFAULT_NAVIGATION_ALL_INDEX_SQL,
    CONTEST_SOURCE_DEFAULT_NAVIGATION_OWNER_INDEX_SQL,
    CONTEST_SOURCE_DEFAULT_NAVIGATION_PUBLIC_INDEX_SQL,
    CONTEST_SOURCE_DEFAULT_PROTECTED_INDEX_SQL,
    CONTEST_SOURCE_SEARCH_GRAMS_TABLE_SQL,
)


# ── 新库 schema 正确性 ────────────────────────────────────────
def test_new_db_has_per_game_match_tables(tmp_path):
    """新库建出三张 per-game 表 + matches_index，无旧单表 matches。"""
    s = Store(str(tmp_path / "new.db"))
    with s._tx() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        contest_columns = {
            row[1] for row in c.execute("PRAGMA table_info(contests)")
        }
    s.close()
    assert "matches_holdem" in tables
    assert "matches_gomoku" in tables
    assert "matches_pencil" in tables
    assert "matches_index" in tables
    assert "matches" not in tables  # 旧单表不存在
    assert "match_replays" in tables  # replay 表保留（全局）
    with sqlite3.connect(tmp_path / "new.db") as connection:
        email_code_columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(email_codes)")
        }
    assert email_code_columns["failed_attempts"][4] == "0"
    assert "published_stage_pairing_count" in contest_columns
    assert "pairing_topology_revision" in contest_columns
    assert "sealed_pairing_topology_revision" in contest_columns


def test_legacy_email_codes_gain_persistent_failure_budget_on_reopen(tmp_path):
    path = tmp_path / "legacy-email-code-attempts.db"
    store = Store(str(path))
    user = store.create_user("legacy-code", "legacy-code@example.com", "hash")
    store.add_email_code(
        user["id"], "reset", "123456", "2099-01-01T00:00:00"
    )
    store.close()

    with sqlite3.connect(path) as legacy:
        legacy.execute("ALTER TABLE email_codes DROP COLUMN failed_attempts")

    migrated = Store(str(path))
    try:
        columns = {
            row[1]: row
            for row in migrated._conn.execute("PRAGMA table_info(email_codes)")
        }
        assert columns["failed_attempts"][4] == "0"
        row = migrated._conn.execute(
            "SELECT failed_attempts FROM email_codes WHERE user_id=?",
            (user["id"],),
        ).fetchone()
        assert row["failed_attempts"] == 0
    finally:
        migrated.close()


_PAIRING_TOPOLOGY_TRIGGERS = (
    "trg_contest_pairing_topology_insert",
    "trg_contest_pairing_topology_delete",
    "trg_contest_pairing_topology_update",
    "trg_contest_pairing_topology_stage_cursor",
    "trg_contest_pairing_topology_manifest",
)
_CONTEST_JOB_REF_TRIGGERS = (
    "trg_execution_contest_pairing_ref_insert",
    "trg_execution_contest_pairing_ref_update",
)
_CONTEST_LIFECYCLE_REVISION_TRIGGERS = (
    "trg_contest_lifecycle_revision_update",
    "trg_contest_entries_lifecycle_revision_insert",
    "trg_contest_entries_lifecycle_revision_delete",
    "trg_contest_entries_lifecycle_revision_update",
    "trg_contest_stage_results_lifecycle_revision_insert",
    "trg_contest_stage_results_lifecycle_revision_delete",
    "trg_contest_stage_results_lifecycle_revision_update",
)


def test_pairing_topology_revision_fresh_legacy_and_reopen_fail_closed(tmp_path):
    path = tmp_path / "pairing-topology-legacy.db"
    store = Store(str(path))
    owner = store.create_user(
        "topology-owner", "topology-owner@example.test", "hash"
    )
    safe = store.create_contest(
        "safe topology", owner["id"], status="published", game_id="holdem"
    )
    damaged = store.create_contest(
        "damaged topology", owner["id"], status="published", game_id="holdem"
    )
    with store._tx() as connection:
        safe_pairing = connection.execute(
            "INSERT INTO contest_pairings(contest_id,status,stage_idx) "
            "VALUES(?,'pending',0)",
            (safe["id"],),
        ).lastrowid
        connection.execute(
            "INSERT INTO contest_pairings(contest_id,status,stage_idx) "
            "VALUES(?,'pending',0)",
            (damaged["id"],),
        )
    store.seal_published_stage_pairing_count(
        safe["id"],
        0,
        expected_count=1,
        expected_existing_ids=[safe_pairing],
    )
    with store._tx() as connection:
        # Simulate a latent pre-revision cardinality defect.  The new epoch may
        # certify neither this row nor the superficially intact one above.
        connection.execute(
            "UPDATE contests SET published_stage_pairing_count=2 WHERE id=?",
            (damaged["id"],),
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (damaged["id"],),
        )
    assert {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }.issuperset({*_PAIRING_TOPOLOGY_TRIGGERS, *_CONTEST_JOB_REF_TRIGGERS})
    assert "pairing_topology_revision" not in store.get_contest(safe["id"])
    assert "sealed_pairing_topology_revision" not in store.get_contest(safe["id"])
    store.close()

    legacy = sqlite3.connect(path)
    for name in _CONTEST_LIFECYCLE_REVISION_TRIGGERS:
        legacy.execute(f"DROP TRIGGER {name}")
    for name in _PAIRING_TOPOLOGY_TRIGGERS:
        legacy.execute(f"DROP TRIGGER {name}")
    legacy.execute(
        "ALTER TABLE contests DROP COLUMN sealed_pairing_topology_revision"
    )
    legacy.execute("ALTER TABLE contests DROP COLUMN pairing_topology_revision")
    legacy.commit()
    legacy.close()

    migrated = Store(str(path))
    safe_header = migrated._conn.execute(
        "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
        "FROM contests WHERE id=?",
        (safe["id"],),
    ).fetchone()
    damaged_header = migrated._conn.execute(
        "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
        "FROM contests WHERE id=?",
        (damaged["id"],),
    ).fetchone()
    # A pre-epoch topology seal cannot prove the roster, stage-decision rows,
    # or frozen format that the lifecycle read model consumes.  Migration must
    # never bless it from a cardinality scan.
    assert tuple(safe_header) == (0, None)
    assert tuple(damaged_header) == (0, None)
    assert migrated._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert migrated._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    migrated.close()

    reopened = Store(str(path))
    assert tuple(
        reopened._conn.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (safe["id"],),
        ).fetchone()
    ) == (0, None)
    assert tuple(
        reopened._conn.execute(
            "SELECT pairing_topology_revision,"
            "sealed_pairing_topology_revision FROM contests WHERE id=?",
            (damaged["id"],),
        ).fetchone()
    ) == (0, None)
    reopened.close()


def test_contest_lifecycle_revision_epoch_is_fresh_and_reopen_stable(tmp_path):
    path = tmp_path / "contest-lifecycle-fresh.db"
    store = Store(str(path))
    owner = store.create_user(
        "lifecycle-fresh-owner", "lifecycle-fresh-owner@example.test", "hash"
    )
    contest = store.create_contest(
        "lifecycle fresh", owner["id"], status="published", game_id="holdem"
    )
    trigger_names = {
        row[0]
        for row in store._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        )
    }
    assert set(_CONTEST_LIFECYCLE_REVISION_TRIGGERS) <= trigger_names
    assert tuple(
        store._conn.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest["id"],),
        ).fetchone()
    ) == (0, None)
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest["id"],),
        )
    store.close()

    reopened = Store(str(path))
    assert tuple(
        reopened._conn.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest["id"],),
        ).fetchone()
    ) == (0, 0)
    reopened.close()


def test_contest_lifecycle_revision_topology_only_epoch_never_preserves_seal(
    tmp_path,
):
    path = tmp_path / "contest-lifecycle-topology-only.db"
    store = Store(str(path))
    owner = store.create_user(
        "lifecycle-topology-owner",
        "lifecycle-topology-owner@example.test",
        "hash",
    )
    contest = store.create_contest(
        "lifecycle topology only",
        owner["id"],
        status="published",
        game_id="holdem",
    )
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest["id"],),
        )
    revision = int(
        store._conn.execute(
            "SELECT pairing_topology_revision FROM contests WHERE id=?",
            (contest["id"],),
        ).fetchone()[0]
    )
    store.close()

    with sqlite3.connect(path) as connection:
        for name in _CONTEST_LIFECYCLE_REVISION_TRIGGERS:
            connection.execute(f"DROP TRIGGER {name}")
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }.issuperset(_PAIRING_TOPOLOGY_TRIGGERS)

    migrated = Store(str(path))
    assert tuple(
        migrated._conn.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest["id"],),
        ).fetchone()
    ) == (revision, None)
    migrated.close()


@pytest.mark.parametrize(
    "missing_name",
    (
        _CONTEST_LIFECYCLE_REVISION_TRIGGERS[-1],
        _PAIRING_TOPOLOGY_TRIGGERS[0],
    ),
)
def test_contest_lifecycle_revision_missing_trigger_invalidates_every_seal_once(
    tmp_path, missing_name,
):
    path = tmp_path / "contest-lifecycle-missing-trigger.db"
    store = Store(str(path))
    owner = store.create_user(
        "lifecycle-missing-owner",
        "lifecycle-missing-owner@example.test",
        "hash",
    )
    contests = [
        store.create_contest(
            f"lifecycle missing {index}",
            owner["id"],
            status="published",
            game_id="holdem",
        )
        for index in range(2)
    ]
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision"
        )
    store.close()

    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP TRIGGER {missing_name}")

    migrated = Store(str(path))
    assert [
        tuple(row)
        for row in migrated._conn.execute(
            "SELECT sealed_pairing_topology_revision FROM contests ORDER BY id"
        ).fetchall()
    ] == [(None,), (None,)]
    assert migrated._conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
        (missing_name,),
    ).fetchone() is not None
    with migrated._tx() as connection:
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision"
        )
    expected = [
        tuple(row)
        for row in migrated._conn.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests ORDER BY id"
        ).fetchall()
    ]
    migrated.close()

    reopened = Store(str(path))
    assert [
        tuple(row)
        for row in reopened._conn.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests ORDER BY id"
        ).fetchall()
    ] == expected
    reopened.close()


def test_contest_lifecycle_revision_trigger_drift_fails_startup(tmp_path):
    path = tmp_path / "contest-lifecycle-trigger-drift.db"
    store = Store(str(path))
    store.close()
    name = _CONTEST_LIFECYCLE_REVISION_TRIGGERS[0]
    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP TRIGGER {name}")
        connection.execute(
            f"CREATE TRIGGER {name} AFTER UPDATE OF status ON contests "
            "BEGIN SELECT 1; END"
        )
    with pytest.raises(RuntimeError, match="canonical trigger definition mismatch"):
        Store(str(path))
    gc.collect()


def test_contest_lifecycle_revision_epoch_upgrade_is_one_transaction(tmp_path):
    path = tmp_path / "contest-lifecycle-epoch-rollback.db"
    store = Store(str(path))
    owner = store.create_user(
        "lifecycle-rollback-owner",
        "lifecycle-rollback-owner@example.test",
        "hash",
    )
    contest = store.create_contest(
        "lifecycle rollback", owner["id"], status="published", game_id="holdem"
    )
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest["id"],),
        )
    expected_header = tuple(
        store._conn.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest["id"],),
        ).fetchone()
    )
    store.close()

    missing_name = _CONTEST_LIFECYCLE_REVISION_TRIGGERS[0]
    drift_name = _CONTEST_LIFECYCLE_REVISION_TRIGGERS[-1]
    drift_sql = (
        f"CREATE TRIGGER {drift_name} AFTER UPDATE OF points "
        "ON contest_stage_results BEGIN SELECT 1; END"
    )
    with sqlite3.connect(path) as connection:
        connection.execute(f"DROP TRIGGER {missing_name}")
        connection.execute(f"DROP TRIGGER {drift_name}")
        connection.execute(drift_sql)

    with pytest.raises(RuntimeError, match="canonical trigger definition mismatch"):
        Store(str(path))
    gc.collect()

    with sqlite3.connect(path) as connection:
        assert tuple(
            connection.execute(
                "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
                "FROM contests WHERE id=?",
                (contest["id"],),
            ).fetchone()
        ) == expected_header
        triggers = {
            row[0]: " ".join(str(row[1]).rstrip(";").split())
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger'"
            )
        }
        assert missing_name not in triggers
        assert triggers[drift_name] == " ".join(drift_sql.split())


def test_contest_lifecycle_revision_tracks_exact_business_dependencies(tmp_path):
    path = tmp_path / "contest-lifecycle-fields.db"
    store = Store(str(path))
    owner = store.create_user(
        "lifecycle-fields-owner", "lifecycle-fields-owner@example.test", "hash"
    )
    other_user = store.create_user(
        "lifecycle-fields-user", "lifecycle-fields-user@example.test", "hash"
    )
    bot = store.create_bot(owner["id"], "lifecycle-fields-bot")
    first = store.create_contest(
        "lifecycle fields first", owner["id"], status="running", game_id="holdem"
    )
    second = store.create_contest(
        "lifecycle fields second", owner["id"], status="running", game_id="holdem"
    )

    def revision(contest_id: int) -> int:
        return int(
            store._conn.execute(
                "SELECT pairing_topology_revision FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()[0]
        )

    with store._tx() as connection:
        entry_id = connection.execute(
            "INSERT INTO contest_entries(contest_id,user_id,bot_id,registered_at) "
            "VALUES(?,?,NULL,'2026-09-03T00:00:00')",
            (first["id"], owner["id"]),
        ).lastrowid
    assert revision(first["id"]) == 1

    contest_dependencies = (
        ("game_id", "gomoku"),
        ("template_id", "lifecycle-template-v2"),
        ("stages_json", '[{"type":"round_robin"}]'),
        ("format_snapshot_json", '{"version":1}'),
        ("source_contest_id", second["id"]),
    )
    expected_first = revision(first["id"])
    for field, value in contest_dependencies:
        with store._tx() as connection:
            connection.execute(
                f"UPDATE contests SET {field}=? WHERE id=?",
                (value, first["id"]),
            )
        expected_first += 1
        assert revision(first["id"]) == expected_first
        # No-op updates do not manufacture a second revision.
        with store._tx() as connection:
            connection.execute(
                f"UPDATE contests SET {field}=? WHERE id=?",
                (value, first["id"]),
            )
        assert revision(first["id"]) == expected_first

    # Ordinary pre-active state progression is not itself a stage decision;
    # entering or leaving rest/finished is.
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET status='rest' WHERE id=?", (first["id"],)
        )
    expected_first += 1
    assert revision(first["id"]) == expected_first
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET status='running' WHERE id=?", (first["id"],)
        )
    expected_first += 1
    assert revision(first["id"]) == expected_first
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET status='finished' WHERE id=?", (first["id"],)
        )
    expected_first += 1
    assert revision(first["id"]) == expected_first

    excluded_contest_fields = (
        ("title", "lifecycle fields renamed"),
        ("description", "progress metadata"),
        ("starts_at", "2026-09-05T00:00:00"),
        ("ends_at", "2026-09-06T00:00:00"),
        ("rest_ends_at", "2026-09-07T00:00:00"),
        ("official_results_ready", 1),
        ("phase", "preliminary"),
        ("time_control_id", "holdem-standard-v1"),
        ("require_real_name", 1),
    )
    for field, value in excluded_contest_fields:
        with store._tx() as connection:
            connection.execute(
                f"UPDATE contests SET {field}=? WHERE id=?",
                (value, first["id"]),
            )
        assert revision(first["id"]) == expected_first

    entry_dependencies = (
        ("id", entry_id + 1000),
        ("user_id", other_user["id"]),
        ("bot_id", bot["id"]),
        ("group_id", "A"),
        ("seed", 7),
        ("eliminated", 1),
    )
    current_entry_id = entry_id
    for field, value in entry_dependencies:
        with store._tx() as connection:
            connection.execute(
                f"UPDATE contest_entries SET {field}=? WHERE id=?",
                (value, current_entry_id),
            )
        if field == "id":
            current_entry_id = value
        expected_first += 1
        assert revision(first["id"]) == expected_first

    for field, value in (
        ("registered_at", "2026-09-03T01:00:00"),
        ("dispatched_at", "2026-09-03T02:00:00"),
        ("real_name_snapshot", "snapshot"),
    ):
        with store._tx() as connection:
            connection.execute(
                f"UPDATE contest_entries SET {field}=? WHERE id=?",
                (value, current_entry_id),
            )
        assert revision(first["id"]) == expected_first

    second_before = revision(second["id"])
    with store._tx() as connection:
        connection.execute(
            "UPDATE contest_entries SET contest_id=? WHERE id=?",
            (second["id"], current_entry_id),
        )
    expected_first += 1
    assert revision(first["id"]) == expected_first
    assert revision(second["id"]) == second_before + 1

    with store._tx() as connection:
        result_id = connection.execute(
            "INSERT INTO contest_stage_results("
            "contest_id,stage_idx,stage_key,entry_id,bot_id,points,wins,draws,"
            "losses,delta_total,group_id,rank_in_group,payload_json) "
            "VALUES(?,0,'stage-0',?, ?,3,1,0,0,10,'A',1,'{}')",
            (second["id"], current_entry_id, bot["id"]),
        ).lastrowid
    expected_second = second_before + 2
    assert revision(second["id"]) == expected_second

    stage_dependencies = (
        ("id", result_id + 1000),
        ("stage_idx", 1),
        ("stage_key", "stage-1"),
        ("entry_id", current_entry_id + 1),
        ("bot_id", None),
        ("points", 4.5),
        ("wins", 2),
        ("draws", 1),
        ("losses", 1),
        ("delta_total", -5),
        ("group_id", "B"),
        ("rank_in_group", 2),
        ("payload_json", '{"overall_rank":2}'),
    )
    current_result_id = result_id
    for field, value in stage_dependencies:
        with store._tx() as connection:
            connection.execute(
                f"UPDATE contest_stage_results SET {field}=? WHERE id=?",
                (value, current_result_id),
            )
        if field == "id":
            current_result_id = value
        expected_second += 1
        assert revision(second["id"]) == expected_second

    first_before_move = revision(first["id"])
    with store._tx() as connection:
        connection.execute(
            "UPDATE contest_stage_results SET contest_id=? WHERE id=?",
            (first["id"], current_result_id),
        )
    assert revision(second["id"]) == expected_second + 1
    assert revision(first["id"]) == first_before_move + 1

    with store._tx() as connection:
        connection.execute(
            "DELETE FROM contest_stage_results WHERE id=?", (current_result_id,)
        )
        connection.execute(
            "DELETE FROM contest_entries WHERE id=?", (current_entry_id,)
        )
    assert revision(first["id"]) == first_before_move + 2
    assert revision(second["id"]) == expected_second + 2
    store.close()


def test_pairing_topology_update_tracks_only_exact_identity_changes(tmp_path):
    path = tmp_path / "pairing-topology-update.db"
    store = Store(str(path))
    owner = store.create_user(
        "topology-update-owner", "topology-update-owner@example.test", "hash"
    )
    contest = store.create_contest(
        "topology update", owner["id"], status="published", game_id="holdem"
    )
    with store._tx() as connection:
        pairing_id = connection.execute(
            "INSERT INTO contest_pairings(contest_id,status,stage_idx) "
            "VALUES(?,'pending',0)",
            (contest["id"],),
        ).lastrowid
    store.seal_published_stage_pairing_count(
        contest["id"],
        0,
        expected_count=1,
        expected_existing_ids=[pairing_id],
    )

    store.update_pairing(
        pairing_id,
        status="running",
        match_id="progress-only",
        scheduled_at="2099-01-01T00:00:00",
    )
    def header() -> tuple[int, int]:
        return tuple(
            store._conn.execute(
                "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
                "FROM contests WHERE id=?",
                (contest["id"],),
            ).fetchone()
        )
    assert header() == (2, 2)

    identity_changes = (
        {"round_num": 2},
        {"stage_key": "rewritten"},
        {"group_id": "B"},
        {"bracket_slot": 3},
        {"color_first": 1},
        {"series_index": 2, "series_size": 3},
        {"tiebreak_group": 1, "tiebreak_game": 1},
        {"tiebreak_game": 2},
        {"pairing_seed": 424242},
        {"published_at": "2026-09-02T01:02:03"},
    )
    expected_revision = 2
    for identity in identity_changes:
        expected_revision += 1
        # Series/tiebreak coordinates are immutable through the public Store
        # method, but the canonical trigger must still guard direct maintenance
        # SQL and older writers that can reach the same database.
        if set(identity).intersection(
            {"series_index", "series_size", "tiebreak_group", "tiebreak_game", "pairing_seed", "published_at"}
        ):
            with store._tx() as connection:
                connection.execute(
                    "UPDATE contest_pairings SET "
                    + ",".join(f"{field}=?" for field in identity)
                    + " WHERE id=?",
                    (*identity.values(), pairing_id),
                )
        else:
            store.update_pairing(pairing_id, **identity)
        assert header() == (expected_revision, 2)
        if set(identity).intersection(
            {"series_index", "series_size", "tiebreak_group", "tiebreak_game", "pairing_seed", "published_at"}
        ):
            with store._tx() as connection:
                connection.execute(
                    "UPDATE contest_pairings SET "
                    + ",".join(f"{field}=?" for field in identity)
                    + " WHERE id=?",
                    (*identity.values(), pairing_id),
                )
        else:
            store.update_pairing(pairing_id, **identity)
        assert header() == (expected_revision, 2)
    store.close()

    reopened = Store(str(path))
    assert tuple(
        reopened._conn.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest["id"],),
        ).fetchone()
    ) == (12, 2)
    reopened.close()


@pytest.mark.parametrize(
    ("field", "initial", "value"),
    [
        ("series_index", {"series_size": 3}, 2),
        ("series_size", {}, 3),
        ("tiebreak_group", {"tiebreak_group": 1, "tiebreak_game": 1}, 2),
        ("tiebreak_game", {"tiebreak_group": 1, "tiebreak_game": 1}, 2),
        ("pairing_seed", {}, 424242),
        ("published_at", {}, "2026-09-02T01:02:03"),
    ],
)
def test_pairing_topology_new_coordinates_each_bump_revision(
    tmp_path, field, initial, value
):
    store = Store(str(tmp_path / f"topology-{field}.db"))
    owner = store.create_user(
        f"topology-{field}", f"topology-{field}@example.test", "hash"
    )
    contest = store.create_contest(
        f"topology {field}", owner["id"], status="published", game_id="holdem"
    )
    columns = {"contest_id": contest["id"], **initial}
    with store._tx() as connection:
        pairing_id = connection.execute(
            "INSERT INTO contest_pairings("
            + ",".join(columns)
            + ") VALUES("
            + ",".join("?" for _ in columns)
            + ")",
            tuple(columns.values()),
        ).lastrowid
    store.seal_published_stage_pairing_count(
        contest["id"], 0, expected_count=1, expected_existing_ids=[pairing_id]
    )
    before = tuple(
        store._conn.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest["id"],),
        ).fetchone()
    )
    assert before[0] == before[1]
    with store._tx() as connection:
        connection.execute(
            f"UPDATE contest_pairings SET {field}=? WHERE id=?",
            (value, pairing_id),
        )
    after = tuple(
        store._conn.execute(
            "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest["id"],),
        ).fetchone()
    )
    assert after == (before[0] + 1, before[1])
    store.close()


def test_pairing_topology_trigger_drift_and_partial_schema_fail_closed(tmp_path):
    drift_path = tmp_path / "pairing-topology-trigger-drift.db"
    store = Store(str(drift_path))
    store.close()
    connection = sqlite3.connect(drift_path)
    connection.executescript(
        "DROP TRIGGER trg_contest_pairing_topology_update;"
        "CREATE TRIGGER trg_contest_pairing_topology_update "
        "AFTER UPDATE OF id,contest_id,stage_idx ON contest_pairings "
        "BEGIN SELECT 1; END;"
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="canonical trigger definition mismatch"):
        Store(str(drift_path))
    gc.collect()

    partial_path = tmp_path / "pairing-topology-partial.db"
    store = Store(str(partial_path))
    store.close()
    connection = sqlite3.connect(partial_path)
    for name in _PAIRING_TOPOLOGY_TRIGGERS:
        connection.execute(f"DROP TRIGGER {name}")
    connection.execute(
        "ALTER TABLE contests DROP COLUMN sealed_pairing_topology_revision"
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="topology revision schema is partial"):
        Store(str(partial_path))
    gc.collect()


def test_new_db_ratings_composite_pk(tmp_path):
    """ratings 表 PK = (bot_id, game_id)，含 game_id 列。"""
    s = Store(str(tmp_path / "new.db"))
    with s._tx() as c:
        cols = {r[1]: r for r in c.execute("PRAGMA table_info(ratings)")}
    s.close()
    assert "game_id" in cols
    # PK 标志：pk 字段在 PRAGMA table_info 里，bot_id 和 game_id 都是 pk=1
    assert cols["bot_id"]["pk"] >= 1
    assert cols["game_id"]["pk"] >= 1


def test_new_db_rating_history_has_game_id(tmp_path):
    s = Store(str(tmp_path / "new.db"))
    with s._tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(rating_history)")}
    s.close()
    assert "game_id" in cols


def test_new_db_uses_only_neutral_persistence_columns(tmp_path):
    s = Store(str(tmp_path / "neutral.db"))
    with s._tx() as c:
        ratings = {row[1] for row in c.execute("PRAGMA table_info(ratings)")}
        stage = {
            row[1] for row in c.execute("PRAGMA table_info(contest_stage_results)")
        }
        pair = {row[1] for row in c.execute("PRAGMA table_info(pair_stats)")}
        replay = {row[1] for row in c.execute("PRAGMA table_info(match_replays)")}
    s.close()

    assert "delta_total" in ratings and "net_chips" not in ratings
    assert "delta_total" in stage and "net_chips" not in stage
    assert {"bb_per_100_mean", "ci_low", "ci_high"}.isdisjoint(pair)
    assert "hands_json" not in replay


def test_contest_pairings_match_id_no_db_fk(tmp_path):
    """contest_pairings.match_id 无 DB 级 FK（逻辑外键，避免引用已删除的 matches 表）。"""
    s = Store(str(tmp_path / "new.db"))
    with s._tx() as c:
        fk_rows = c.execute("PRAGMA foreign_key_list(contest_pairings)").fetchall()
    s.close()
    # 不应有引用 matches_holdem/gomoku/pencil 或 matches 的 FK
    ref_tables = {r[2] for r in fk_rows}  # r[2] = referenced table
    assert not any("matches" in t for t in ref_tables)


def test_contest_pairing_schedule_index_is_canonical_planned_and_reopen_safe(
    tmp_path,
):
    path = tmp_path / "contest-schedule-index.db"
    store = Store(str(path))
    with store._tx() as connection:
        schedule_definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_contest_pairings_schedule",),
        ).fetchone()[0]
        sync_definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_contest_pairings_completion_sync",),
        ).fetchone()[0]
        schedule_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT id FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? AND status=? "
                "AND (scheduled_at IS NULL OR scheduled_at<=?)",
                (1, 0, "pending", "2099-01-01T00:00:00"),
            )
        )
        sync_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT id,match_id FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? AND status<>? "
                "AND match_id IS NOT NULL ORDER BY id",
                (1, 0, "completed"),
            )
        )
        sync_exists_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? AND status<>? "
                "AND match_id IS NOT NULL LIMIT 1",
                (1, 0, "completed"),
            )
        )
    assert "".join(schedule_definition.split()).lower() == "".join(
        CONTEST_PAIRING_SCHEDULE_INDEX_SQL.split()
    ).lower()
    assert "".join(sync_definition.split()).lower() == "".join(
        CONTEST_PAIRING_SYNC_INDEX_SQL.split()
    ).lower()
    assert "idx_contest_pairings_schedule" in schedule_plan
    assert "idx_contest_pairings_completion_sync" in sync_plan
    assert "idx_contest_pairings_completion_sync" in sync_exists_plan

    owner = store.create_user(
        "sync-index-owner", "sync-index-owner@example.com", "hash"
    )
    contest = store.create_contest("sync index scale", owner["id"])
    with store._tx() as connection:
        connection.executemany(
            "INSERT INTO contest_pairings(contest_id,match_id,status,stage_idx) "
            "VALUES(?,?,'completed',0)",
            ((contest["id"], f"completed-{index}") for index in range(10_000)),
        )
        progress_steps = 0

        def count_progress() -> int:
            nonlocal progress_steps
            progress_steps += 1
            return 0

        connection.set_progress_handler(count_progress, 1)
        try:
            incomplete = connection.execute(
                "SELECT 1 FROM contest_pairings WHERE contest_id=? "
                "AND stage_idx=? AND status<>? AND match_id IS NOT NULL LIMIT 1",
                (contest["id"], 0, "completed"),
            ).fetchone()
        finally:
            connection.set_progress_handler(None, 0)
    assert incomplete is None
    # The partial index excludes all 10k mirrored rows.  A regression to an
    # index that merely stores every bound Match takes about 40k VM steps.
    assert progress_steps < 500
    store.close()

    # Simulate a legacy schema that predates the index; first reopen migrates it,
    # second reopen proves the canonical check and creation are idempotent.
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX idx_contest_pairings_schedule")
    connection.execute("DROP INDEX idx_contest_pairings_completion_sync")
    connection.commit()
    connection.close()
    migrated = Store(str(path))
    migrated.close()
    reopened = Store(str(path))
    assert reopened._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert reopened._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()

    malformed_path = tmp_path / "contest-schedule-index-malformed.db"
    malformed = Store(str(malformed_path))
    malformed.close()
    connection = sqlite3.connect(malformed_path)
    connection.execute("DROP INDEX idx_contest_pairings_schedule")
    connection.execute(
        "CREATE INDEX idx_contest_pairings_schedule "
        "ON contest_pairings(contest_id,status)"
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="schedule index definition mismatch"):
        Store(str(malformed_path))

    malformed_sync_path = tmp_path / "contest-sync-index-malformed.db"
    malformed_sync = Store(str(malformed_sync_path))
    malformed_sync.close()
    connection = sqlite3.connect(malformed_sync_path)
    connection.execute("DROP INDEX idx_contest_pairings_completion_sync")
    connection.execute(
        "CREATE INDEX idx_contest_pairings_completion_sync "
        "ON contest_pairings(contest_id,status)"
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="completion sync index definition mismatch"):
        Store(str(malformed_sync_path))


def test_contest_entry_page_index_is_canonical_planned_and_reopen_safe(tmp_path):
    path = tmp_path / "contest-entry-page-index.db"
    store = Store(str(path))
    with store._tx() as connection:
        definition = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_contest_entries_page_order",),
        ).fetchone()[0]
        plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN "
                "SELECT e.id,e.contest_id,e.user_id,e.bot_id,e.registered_at,"
                "e.group_id,e.seed,e.eliminated,e.dispatched_at,"
                "b.name,u.username FROM contest_entries e "
                "LEFT JOIN bots b ON e.bot_id=b.id "
                "LEFT JOIN users u ON e.user_id=u.id "
                "WHERE e.contest_id=? "
                "ORDER BY e.seed,e.registered_at,e.id LIMIT ? OFFSET ?",
                (1, 20, 0),
            )
        )
    assert "".join(definition.split()).lower() == "".join(
        CONTEST_ENTRY_PAGE_INDEX_SQL.split()
    ).lower()
    assert "idx_contest_entries_page_order" in plan
    assert "USE TEMP B-TREE FOR ORDER BY" not in plan
    store.close()

    # A legacy database gains the index on its first open; the second open must
    # certify the same definition without further schema churn.
    with sqlite3.connect(path) as connection:
        connection.execute("DROP INDEX idx_contest_entries_page_order")
    migrated = Store(str(path))
    migrated.close()
    with sqlite3.connect(path) as connection:
        migrated_schema_version = int(
            connection.execute("PRAGMA schema_version").fetchone()[0]
        )
    reopened = Store(str(path))
    assert int(reopened._conn.execute("PRAGMA schema_version").fetchone()[0]) == (
        migrated_schema_version
    )
    assert reopened._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert reopened._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()

    malformed_path = tmp_path / "contest-entry-page-index-malformed.db"
    malformed = Store(str(malformed_path))
    malformed.close()
    with sqlite3.connect(malformed_path) as connection:
        connection.execute("DROP INDEX idx_contest_entries_page_order")
        connection.execute(
            "CREATE INDEX idx_contest_entries_page_order "
            "ON contest_entries(contest_id,id)"
        )
    with pytest.raises(RuntimeError, match="entry page index definition mismatch"):
        Store(str(malformed_path))


def test_contest_source_search_schema_is_fresh_legacy_reopen_and_drift_safe(tmp_path):
    path = tmp_path / "contest-source-search.db"
    store = Store(str(path))
    owner = store.create_user(
        "source-search-owner", "source-search-owner@example.test", "hash"
    )
    contest = store.create_contest(
        "旧标题 决赛 ABC", owner["id"], game_id="gomoku", status="finished"
    )
    store.update_contest(contest["id"], official_results_ready=1)
    with store._tx() as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ("contest_source_search_grams",),
        ).fetchone()[0]
        index_sql = {
            row[0]: row[1]
            for row in connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='index' "
                "AND name LIKE 'idx_contests_source_%'"
            )
        }
        assert connection.execute(
            "SELECT 1 FROM contest_source_search_grams "
            "WHERE gram_len=2 AND gram='决赛' AND contest_id=?",
            (contest["id"],),
        ).fetchone()
    assert " ".join(table_sql.split()).lower() == " ".join(
        CONTEST_SOURCE_SEARCH_GRAMS_TABLE_SQL.split()
    ).lower()
    expected_indexes = {
        "idx_contests_source_protected": CONTEST_SOURCE_PROTECTED_INDEX_SQL,
        "idx_contests_source_navigation_all": CONTEST_SOURCE_NAVIGATION_ALL_INDEX_SQL,
        "idx_contests_source_navigation_public": CONTEST_SOURCE_NAVIGATION_PUBLIC_INDEX_SQL,
        "idx_contests_source_navigation_owner": CONTEST_SOURCE_NAVIGATION_OWNER_INDEX_SQL,
        "idx_contests_source_default_protected": CONTEST_SOURCE_DEFAULT_PROTECTED_INDEX_SQL,
        "idx_contests_source_default_navigation_all": CONTEST_SOURCE_DEFAULT_NAVIGATION_ALL_INDEX_SQL,
        "idx_contests_source_default_navigation_public": CONTEST_SOURCE_DEFAULT_NAVIGATION_PUBLIC_INDEX_SQL,
        "idx_contests_source_default_navigation_owner": CONTEST_SOURCE_DEFAULT_NAVIGATION_OWNER_INDEX_SQL,
    }
    for name, expected in expected_indexes.items():
        assert " ".join(index_sql[name].split()).lower() == " ".join(
            expected.split()
        ).lower()

    # Direct SQL is covered by the same canonical insert/update/delete triggers.
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET title='新标题 半决赛 xyz' WHERE id=?",
            (contest["id"],),
        )
        assert not connection.execute(
            "SELECT 1 FROM contest_source_search_grams "
            "WHERE gram_len=2 AND gram='旧标' AND contest_id=?",
            (contest["id"],),
        ).fetchone()
        assert connection.execute(
            "SELECT 1 FROM contest_source_search_grams "
            "WHERE gram_len=3 AND gram='半决赛' AND contest_id=?",
            (contest["id"],),
        ).fetchone()
        connection.execute(
            "UPDATE contests SET status='draft',official_results_ready='bad',"
            "showcase_key='hidden',created_at='2026-09-02T01:02:03' WHERE id=?",
            (contest["id"],),
        )
        hints = connection.execute(
            "SELECT DISTINCT created_at,is_nonshowcase,is_protected,"
            "is_nav_public,is_nav_hidden FROM contest_source_search_grams "
            "WHERE contest_id=?",
            (contest["id"],),
        ).fetchone()
        assert tuple(hints) == ("2026-09-02T01:02:03", 0, 0, 0, 0)
        connection.execute(
            "UPDATE contests SET status='finished',official_results_ready=1,"
            "showcase_key=NULL WHERE id=?",
            (contest["id"],),
        )
        direct_id = connection.execute(
            "INSERT INTO contests(title,description,organizer_id,status,created_at,"
            "game_id) VALUES('直接写入 决赛','',?,'draft','2026-09-02',"
            "'gomoku')",
            (owner["id"],),
        ).lastrowid
        assert connection.execute(
            "SELECT 1 FROM contest_source_search_grams "
            "WHERE gram_len=2 AND gram='决赛' AND contest_id=?",
            (direct_id,),
        ).fetchone()
        connection.execute("DELETE FROM contests WHERE id=?", (direct_id,))
        assert not connection.execute(
            "SELECT 1 FROM contest_source_search_grams WHERE contest_id=?",
            (direct_id,),
        ).fetchone()
    store.close()
    with sqlite3.connect(path) as connection:
        stable_version = int(connection.execute("PRAGMA schema_version").fetchone()[0])
    reopened = Store(str(path))
    assert int(reopened._conn.execute("PRAGMA schema_version").fetchone()[0]) == (
        stable_version
    )
    reopened.close()

    # Simulate a legacy DB that predates the projection and its triggers.
    with sqlite3.connect(path) as connection:
        for name in (
            "trg_contest_source_search_insert",
            "trg_contest_source_search_update",
            "trg_contest_source_search_delete",
        ):
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute("DROP TABLE contest_source_search_grams")
    migrated = Store(str(path))
    assert migrated.list_contest_source_candidates(
        game_id="gomoku", query="半决赛"
    )["items"] == [{"id": contest["id"], "title": "新标题 半决赛 xyz"}]
    migrated.close()
    Store(str(path)).close()

    drift_path = tmp_path / "contest-source-search-drift.db"
    drift = Store(str(drift_path))
    drift.close()
    with sqlite3.connect(drift_path) as connection:
        connection.execute("DROP INDEX idx_contests_source_protected")
        connection.execute(
            "CREATE INDEX idx_contests_source_protected ON contests(game_id,id)"
        )
    with pytest.raises(RuntimeError, match="canonical index definition mismatch"):
        Store(str(drift_path))

    trigger_drift_path = tmp_path / "contest-source-trigger-drift.db"
    trigger_drift = Store(str(trigger_drift_path))
    trigger_drift.close()
    with sqlite3.connect(trigger_drift_path) as connection:
        connection.execute("DROP TRIGGER trg_contest_source_search_update")
        connection.execute(
            "CREATE TRIGGER trg_contest_source_search_update "
            "AFTER UPDATE OF title ON contests BEGIN SELECT 1; END"
        )
    with pytest.raises(RuntimeError, match="canonical trigger definition mismatch"):
        Store(str(trigger_drift_path))

    table_drift_path = tmp_path / "contest-source-table-drift.db"
    table_drift = Store(str(table_drift_path))
    table_drift.close()
    with sqlite3.connect(table_drift_path) as connection:
        for name in (
            "trg_contest_source_search_insert",
            "trg_contest_source_search_update",
            "trg_contest_source_search_delete",
        ):
            connection.execute(f"DROP TRIGGER {name}")
        connection.execute("DROP TABLE contest_source_search_grams")
        connection.execute(
            "CREATE TABLE contest_source_search_grams(contest_id INTEGER)"
        )
    with pytest.raises(RuntimeError, match="source search table definition mismatch"):
        Store(str(table_drift_path))


def test_unreleased_contest_gram_projection_fails_closed_without_drop(tmp_path):
    path = tmp_path / "unreleased-contest-grams.db"
    store = Store(str(path))
    store.close()
    unreleased_table_sql = (
        "CREATE TABLE contest_source_search_grams ("
        "gram_len INTEGER NOT NULL CHECK(gram_len IN (1,2,3)),"
        "gram TEXT NOT NULL COLLATE NOCASE,"
        "contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,"
        "PRIMARY KEY(gram_len,gram,contest_id)) WITHOUT ROWID"
    )
    unreleased_index_sql = (
        "CREATE INDEX idx_contests_source_protected "
        "ON contests(game_id,created_at DESC,id DESC) WHERE showcase_key IS NULL "
        "AND status='finished' AND typeof(official_results_ready)='integer' "
        "AND official_results_ready=1"
    )
    unreleased_trigger_sql = (
        "CREATE TRIGGER trg_contest_source_search_update AFTER UPDATE OF title "
        "ON contests BEGIN SELECT 1; END"
    )
    with sqlite3.connect(path) as connection:
        for name in (
            "trg_contest_source_search_insert",
            "trg_contest_source_search_update",
            "trg_contest_source_search_delete",
        ):
            connection.execute(f"DROP TRIGGER {name}")
        for (name,) in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name LIKE 'idx_contests_source_%'"
        ).fetchall():
            connection.execute(f"DROP INDEX {name}")
        connection.execute("DROP TABLE contest_source_search_grams")
        connection.execute(unreleased_table_sql)
        connection.execute(unreleased_index_sql)
        connection.execute(unreleased_trigger_sql)

    with pytest.raises(RuntimeError, match="source search table definition mismatch"):
        Store(str(path))

    with sqlite3.connect(path) as connection:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
            ("contest_source_search_grams",),
        ).fetchone()[0]
        trigger_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            ("trg_contest_source_search_update",),
        ).fetchone()[0]
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_contests_source_protected",),
        ).fetchone()[0]
    assert " ".join(table_sql.split()).lower() == " ".join(
        unreleased_table_sql.split()
    ).lower()
    assert " ".join(trigger_sql.split()).lower() == " ".join(
        unreleased_trigger_sql.split()
    ).lower()
    assert " ".join(index_sql.split()).lower() == " ".join(
        unreleased_index_sql.split()
    ).lower()


def test_execution_claim_indexes_are_canonical_planned_and_reopen_safe(tmp_path):
    path = tmp_path / "execution-claim-indexes.db"
    store = Store(str(path))
    expected = {
        "idx_execution_jobs_claim_source_order": EXECUTION_CLAIM_SOURCE_ORDER_INDEX_SQL,
        "idx_execution_jobs_claim_contest_order": EXECUTION_CLAIM_CONTEST_ORDER_INDEX_SQL,
        "idx_execution_jobs_contest_dispatch_gap": EXECUTION_CONTEST_DISPATCH_GAP_INDEX_SQL,
    }
    with store._tx() as connection:
        definitions = {
            name: connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                (name,),
            ).fetchone()[0]
            for name in expected
        }
        source_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM execution_jobs INDEXED BY "
                "idx_execution_jobs_claim_source_order "
                "WHERE source=? AND status='queued' AND cancel_requested=0 "
                "ORDER BY created_at,id LIMIT 64",
                ("contest",),
            )
        )
        gap_plan = " ".join(
            str(row[3])
            for row in connection.execute(
                "EXPLAIN QUERY PLAN SELECT contest_pairing_id FROM execution_jobs "
                "INDEXED BY idx_execution_jobs_contest_dispatch_gap "
                "WHERE source=? AND contest_id=? AND status IN (?,?) LIMIT 1",
                ("contest", 1, "cancelled", "interrupted"),
            )
        )
    for name, sql in expected.items():
        assert "".join(definitions[name].split()).lower() == "".join(
            sql.split()
        ).lower()
    assert "idx_execution_jobs_claim_source_order" in source_plan
    assert "idx_execution_jobs_contest_dispatch_gap" in gap_plan
    store.close()

    # A database created before these indexes gets them on first open; a
    # second reopen proves the canonical certification is idempotent.
    connection = sqlite3.connect(path)
    for name in expected:
        connection.execute(f'DROP INDEX "{name}"')
    connection.commit()
    connection.close()
    migrated = Store(str(path))
    migrated.close()
    reopened = Store(str(path))
    assert reopened._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert reopened._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()

    malformed_path = tmp_path / "execution-claim-index-malformed.db"
    malformed = Store(str(malformed_path))
    malformed.close()
    connection = sqlite3.connect(malformed_path)
    connection.execute("DROP INDEX idx_execution_jobs_claim_source_order")
    connection.execute(
        "CREATE INDEX idx_execution_jobs_claim_source_order "
        "ON execution_jobs(source,status)"
    )
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="execution queue index definition mismatch"):
        Store(str(malformed_path))


def _make_legacy_neutral_contract_db(tmp_path, name: str) -> tuple[str, dict[str, int]]:
    """Build one valid current DB, then downgrade only the contract surfaces."""
    db = str(tmp_path / name)
    store = Store(db)
    owner = store.create_user("owner", f"owner-{name}@example.com", "hash")
    entrant = store.create_user("entrant", f"entrant-{name}@example.com", "hash")
    holdem_a = store.create_bot(owner["id"], "holdem-a", game_id="holdem")
    holdem_b = store.create_bot(entrant["id"], "holdem-b", game_id="holdem")
    gomoku = store.create_bot(owner["id"], "gomoku-a", game_id="gomoku")
    pencil = store.create_bot(owner["id"], "pencil-a", game_id="pencil")

    matches = {
        "holdem": ("legacy-holdem", holdem_a["id"]),
        "gomoku": ("legacy-gomoku", gomoku["id"]),
        "pencil": ("legacy-pencil", pencil["id"]),
    }
    for game_id, (match_id, bot_id) in matches.items():
        store.create_match(match_id, bot_id, bot_id, game_id=game_id)
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            result={
                "rounds_played": 1,
                "deltas": [1, -1],
                "normalized_delta": 1.0,
            },
        )
        store.upsert_replay(match_id, '[{"type":"match_start"}]')
    store.create_match(
        "legacy-aborted", holdem_a["id"], holdem_b["id"], game_id="holdem"
    )
    store.update_match("legacy-aborted", status="aborted")

    store.ensure_rating(holdem_a["id"])
    store.update_rating_row(holdem_a["id"], delta_total=37)
    store.upsert_pair_stats(
        holdem_a["id"], holdem_b["id"], a_wins_delta=1
    )
    contest = store.create_contest(
        "legacy contract", owner["id"], game_id="holdem"
    )
    entry_a = store.add_contest_entry(contest["id"], owner["id"], holdem_a["id"])
    entry_b = store.add_contest_entry(
        contest["id"], entrant["id"], holdem_b["id"]
    )
    store.upsert_stage_result(
        contest["id"],
        0,
        entry_a["id"],
        bot_id=holdem_a["id"],
        points=3,
        wins=1,
        delta_total=321,
    )
    store.replace_official_results(
        contest["id"],
        [
            {
                "entry_id": entry_a["id"],
                "rank": 1,
                "points": 3,
                "bot_id": holdem_a["id"],
                "user_id": owner["id"],
                "tiebreaks_json": json.dumps({"normalized_delta": 5.0}),
            },
            {
                "entry_id": entry_b["id"],
                "rank": 2,
                "points": 0,
                "bot_id": holdem_b["id"],
                "user_id": entrant["id"],
                "tiebreaks_json": json.dumps({"normalized_delta": -5.0}),
            },
        ],
    )
    store.close()

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    # 三种历史 result：旧键、与新键冲突、新键缺 normalized。
    conn.execute(
        "UPDATE matches_holdem SET result=? WHERE id='legacy-holdem'",
        (json.dumps({"hands_played": 70, "deltas": [500, -500], "net_bb": 5.0}),),
    )
    conn.execute(
        "UPDATE matches_gomoku SET result=? WHERE id='legacy-gomoku'",
        (
            json.dumps(
                {
                    "rounds_played": 9,
                    "hands_played": 3,
                    "deltas": [1, -1],
                    "normalized_delta": 999.0,
                    "net_bb": 123.0,
                }
            ),
        ),
    )
    conn.execute(
        "UPDATE matches_pencil SET result=? WHERE id='legacy-pencil'",
        (json.dumps({"hands_played": 12, "deltas": [-2, 2]}),),
    )
    conn.execute(
        "UPDATE matches_holdem SET result=? WHERE id='legacy-aborted'",
        (
            json.dumps(
                {
                    "hands_played": 8,
                    "deltas": [100, -100],
                    "net_bb": 1.0,
                    "internal_marker": "keep",
                }
            ),
        ),
    )
    # 正式榜冲突时保留新值；只有旧键时做纯改名，rank 不重算。
    conn.execute(
        "UPDATE contest_official_results SET tiebreaks_json=? WHERE rank=1",
        (json.dumps({"net_bb_per_100": 5.0, "buchholz": 2.0}),),
    )
    conn.execute(
        "UPDATE contest_official_results SET tiebreaks_json=? WHERE rank=2",
        (
            json.dumps(
                {"normalized_delta": -7.0, "net_bb_per_100": -99.0}
            ),
        ),
    )
    conn.execute("ALTER TABLE ratings RENAME COLUMN delta_total TO net_chips")
    conn.execute(
        "ALTER TABLE contest_stage_results RENAME COLUMN delta_total TO net_chips"
    )
    conn.execute(
        "ALTER TABLE pair_stats ADD COLUMN bb_per_100_mean REAL NOT NULL DEFAULT 0"
    )
    conn.execute("ALTER TABLE pair_stats ADD COLUMN ci_low REAL NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE pair_stats ADD COLUMN ci_high REAL NOT NULL DEFAULT 0")
    conn.execute(
        "UPDATE pair_stats SET bb_per_100_mean=12.5,ci_low=1.0,ci_high=20.0"
    )
    conn.execute(
        "ALTER TABLE match_replays ADD COLUMN hands_json TEXT NOT NULL DEFAULT '[]'"
    )
    conn.execute("UPDATE match_replays SET hands_json='[1,2,3]'")
    conn.commit()
    conn.close()
    return db, {
        "contest_id": contest["id"],
        "entry_a": entry_a["id"],
        "entry_b": entry_b["id"],
        "holdem_a": holdem_a["id"],
    }


def test_neutral_contract_migration_preserves_data_and_is_idempotent(tmp_path):
    db, ids = _make_legacy_neutral_contract_db(tmp_path, "legacy-neutral.db")

    before = sqlite3.connect(db)
    before_counts = {
        table: before.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "matches_holdem",
            "matches_gomoku",
            "matches_pencil",
            "ratings",
            "pair_stats",
            "match_replays",
            "contest_stage_results",
            "contest_official_results",
        )
    }
    before_rank_order = before.execute(
        "SELECT entry_id,rank FROM contest_official_results ORDER BY rank"
    ).fetchall()
    before.close()

    migrated = Store(db)
    migrated.close()
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    after_counts = {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in before_counts
    }
    assert after_counts == before_counts
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT entry_id,rank FROM contest_official_results ORDER BY rank"
        ).fetchall()
    ] == before_rank_order

    column_sets = {
        table: {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for table in (
            "ratings",
            "pair_stats",
            "match_replays",
            "contest_stage_results",
        )
    }
    assert "delta_total" in column_sets["ratings"]
    assert "net_chips" not in column_sets["ratings"]
    assert "delta_total" in column_sets["contest_stage_results"]
    assert "net_chips" not in column_sets["contest_stage_results"]
    assert {"bb_per_100_mean", "ci_low", "ci_high"}.isdisjoint(
        column_sets["pair_stats"]
    )
    assert "hands_json" not in column_sets["match_replays"]
    assert conn.execute(
        "SELECT delta_total FROM ratings WHERE bot_id=?",
        (ids["holdem_a"],),
    ).fetchone()[0] == 37
    assert conn.execute(
        "SELECT delta_total FROM contest_stage_results WHERE contest_id=?",
        (ids["contest_id"],),
    ).fetchone()[0] == 321

    results = {
        game_id: json.loads(
            conn.execute(
                f"SELECT result FROM matches_{game_id} WHERE id=?",
                (f"legacy-{game_id}",),
            ).fetchone()[0]
        )
        for game_id in ("holdem", "gomoku", "pencil")
    }
    assert results["holdem"] == {
        "rounds_played": 70,
        "deltas": [500, -500],
        "normalized_delta": 5.0,
    }
    assert results["gomoku"] == {
        "rounds_played": 9,
        "deltas": [1, -1],
        "normalized_delta": 1.0,
    }
    assert results["pencil"] == {
        "rounds_played": 12,
        "deltas": [-2, 2],
        "normalized_delta": -2.0,
    }
    aborted_result = json.loads(
        conn.execute(
            "SELECT result FROM matches_holdem WHERE id='legacy-aborted'"
        ).fetchone()[0]
    )
    assert aborted_result == {"internal_marker": "keep"}
    tiebreaks = [
        json.loads(row[0])
        for row in conn.execute(
            "SELECT tiebreaks_json FROM contest_official_results ORDER BY rank"
        )
    ]
    assert tiebreaks == [
        {"buchholz": 2.0, "normalized_delta": 5.0},
        {"normalized_delta": -7.0},
    ]

    first_snapshot = {
        "results": results,
        "tiebreaks": tiebreaks,
        "counts": after_counts,
    }
    conn.close()
    Store(db).close()
    reopened = sqlite3.connect(db)
    second_snapshot = {
        "results": {
            game_id: json.loads(
                reopened.execute(
                    f"SELECT result FROM matches_{game_id} WHERE id=?",
                    (f"legacy-{game_id}",),
                ).fetchone()[0]
            )
            for game_id in ("holdem", "gomoku", "pencil")
        },
        "tiebreaks": [
            json.loads(row[0])
            for row in reopened.execute(
                "SELECT tiebreaks_json FROM contest_official_results ORDER BY rank"
            )
        ],
        "counts": {
            table: reopened.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in before_counts
        },
    }
    reopened.close()
    assert second_snapshot == first_snapshot


def test_neutral_column_migration_prefers_existing_new_values(tmp_path):
    db = str(tmp_path / "new-and-legacy-columns.db")
    store = Store(db)
    owner = store.create_user("owner", "owner@example.com", "hash")
    entrant = store.create_user("entrant", "entrant@example.com", "hash")
    bot_a = store.create_bot(owner["id"], "a", game_id="holdem")
    bot_b = store.create_bot(entrant["id"], "b", game_id="holdem")
    store.ensure_rating(bot_a["id"])
    store.update_rating_row(bot_a["id"], delta_total=88)
    contest = store.create_contest("contract", owner["id"], game_id="holdem")
    entry = store.add_contest_entry(contest["id"], owner["id"], bot_a["id"])
    store.add_contest_entry(contest["id"], entrant["id"], bot_b["id"])
    store.upsert_stage_result(
        contest["id"],
        0,
        entry["id"],
        bot_id=bot_a["id"],
        delta_total=654,
    )
    store.close()

    conn = sqlite3.connect(db)
    conn.execute(
        "ALTER TABLE ratings ADD COLUMN net_chips INTEGER NOT NULL DEFAULT 0"
    )
    conn.execute("UPDATE ratings SET net_chips=37")
    conn.execute(
        "ALTER TABLE contest_stage_results "
        "ADD COLUMN net_chips INTEGER NOT NULL DEFAULT 0"
    )
    conn.execute("UPDATE contest_stage_results SET net_chips=321")
    conn.commit()
    conn.close()

    Store(db).close()
    migrated = sqlite3.connect(db)
    rating_cols = {
        row[1] for row in migrated.execute("PRAGMA table_info(ratings)")
    }
    stage_cols = {
        row[1]
        for row in migrated.execute("PRAGMA table_info(contest_stage_results)")
    }
    assert "net_chips" not in rating_cols
    assert "net_chips" not in stage_cols
    assert migrated.execute(
        "SELECT delta_total FROM ratings WHERE bot_id=?", (bot_a["id"],)
    ).fetchone()[0] == 88
    assert migrated.execute(
        "SELECT delta_total FROM contest_stage_results WHERE contest_id=?",
        (contest["id"],),
    ).fetchone()[0] == 654
    assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    migrated.close()


def test_physical_result_columns_only_backfill_missing_new_json_keys(tmp_path):
    """新 JSON 键优先于旧物理列；非法 JSON 可先规范化再安全回填。"""
    db = str(tmp_path / "physical-and-json-results.db")
    store = Store(db)
    owner = store.create_user("owner", "owner@example.com", "hash")
    bot = store.create_bot(owner["id"], "gomoku", game_id="gomoku")
    for match_id in ("result-conflict", "result-invalid"):
        store.create_match(match_id, bot["id"], bot["id"], game_id="gomoku")
        store.update_match(match_id, status="completed", winner=0)
    store.close()

    conn = sqlite3.connect(db)
    conn.execute("ALTER TABLE matches_gomoku ADD COLUMN hands_played INTEGER")
    conn.execute("ALTER TABLE matches_gomoku ADD COLUMN earnings_a INTEGER")
    conn.execute("ALTER TABLE matches_gomoku ADD COLUMN earnings_b INTEGER")
    conn.execute(
        "UPDATE matches_gomoku SET hands_played=99,earnings_a=77,earnings_b=-77,"
        "result=? WHERE id='result-conflict'",
        (
            json.dumps(
                {
                    "rounds_played": 9,
                    "deltas": [1, -1],
                    "normalized_delta": 999.0,
                }
            ),
        ),
    )
    conn.execute(
        "UPDATE matches_gomoku SET hands_played=7,earnings_a=4,earnings_b=-4,"
        "result='not-json' WHERE id='result-invalid'"
    )
    conn.commit()
    conn.close()

    Store(db).close()
    migrated = sqlite3.connect(db)
    conflict = json.loads(
        migrated.execute(
            "SELECT result FROM matches_gomoku WHERE id='result-conflict'"
        ).fetchone()[0]
    )
    invalid = json.loads(
        migrated.execute(
            "SELECT result FROM matches_gomoku WHERE id='result-invalid'"
        ).fetchone()[0]
    )
    columns = {
        row[1] for row in migrated.execute("PRAGMA table_info(matches_gomoku)")
    }
    assert conflict == {
        "rounds_played": 9,
        "deltas": [1, -1],
        "normalized_delta": 1.0,
    }
    assert invalid == {
        "rounds_played": 7,
        "deltas": [4, -4],
        "normalized_delta": 4.0,
    }
    assert {"hands_played", "earnings_a", "earnings_b"}.isdisjoint(columns)
    assert migrated.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert migrated.execute("PRAGMA foreign_key_check").fetchall() == []
    migrated.close()


def test_neutral_contract_schema_and_json_migrate_in_one_transaction(
    tmp_path, monkeypatch
):
    db, _ids = _make_legacy_neutral_contract_db(tmp_path, "rollback-neutral.db")
    from bzplat.backend.matches import result_contract

    with monkeypatch.context() as patch:
        patch.setattr(
            result_contract,
            "build_result_payload",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                RuntimeError("forced result migration failure")
            ),
        )
        with pytest.raises(RuntimeError, match="forced result migration failure"):
            Store(db)
    gc.collect()

    conn = sqlite3.connect(db)
    assert "net_chips" in {
        row[1] for row in conn.execute("PRAGMA table_info(ratings)")
    }
    assert "net_chips" in {
        row[1] for row in conn.execute("PRAGMA table_info(contest_stage_results)")
    }
    assert "bb_per_100_mean" in {
        row[1] for row in conn.execute("PRAGMA table_info(pair_stats)")
    }
    assert "hands_json" in {
        row[1] for row in conn.execute("PRAGMA table_info(match_replays)")
    }
    raw_result = json.loads(
        conn.execute(
            "SELECT result FROM matches_holdem WHERE id='legacy-holdem'"
        ).fetchone()[0]
    )
    assert "hands_played" in raw_result and "rounds_played" not in raw_result
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()

    # 故障解除后同一副本可完整升级，证明没有 _new 残表或半迁移状态。
    Store(db).close()


def test_legacy_contest_entries_gain_unique_registration_index(tmp_path):
    """A legacy copied DB without UNIQUE(contest_id,user_id) must accept the
    modern ON CONFLICT registration path instead of returning HTTP 500.

    Three or more duplicate historical rows are collapsed to the earliest entry;
    pairing and result identities are deduplicated before the unique index is
    installed.
    """
    import sqlite3

    db = str(tmp_path / "legacy-contest-entry-unique.db")
    initial = Store(db)
    owner = initial.create_user("owner", "owner@example.com", "hash")
    entrant = initial.create_user("entrant", "entrant@example.com", "hash")
    # The modern registration write path validates the current executable
    # mirror before checking the duplicate key.  Keep this migration fixture a
    # genuinely runnable legacy Bot so the assertion below continues to test
    # the unique-registration migration rather than failing an unrelated Bot
    # admission guard first.
    bot = initial.create_bot(
        entrant["id"],
        "entrant_bot",
        binary_path="/tmp/entrant_bot",
        format="elf",
        game_id="holdem",
    )
    contest = initial.create_contest(
        "legacy registration",
        owner["id"],
        status="open",
    )
    initial.close()

    conn = sqlite3.connect(db)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("ALTER TABLE contest_entries RENAME TO contest_entries_current")
    conn.execute(
        "CREATE TABLE contest_entries ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE, "
        "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
        "bot_id INTEGER REFERENCES bots(id) ON DELETE SET NULL, "
        "registered_at TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '', "
        "seed INTEGER NOT NULL DEFAULT 0, eliminated INTEGER NOT NULL DEFAULT 0, "
        "dispatched_at TEXT)"
    )
    first = conn.execute(
        "INSERT INTO contest_entries(contest_id,user_id,bot_id,registered_at) "
        "VALUES(?,?,?,'2026-01-01T00:00:00Z')",
        (contest["id"], entrant["id"], bot["id"]),
    ).lastrowid
    duplicate = conn.execute(
        "INSERT INTO contest_entries(contest_id,user_id,bot_id,registered_at) "
        "VALUES(?,?,?,'2026-01-02T00:00:00Z')",
        (contest["id"], entrant["id"], bot["id"]),
    ).lastrowid
    second_duplicate = conn.execute(
        "INSERT INTO contest_entries(contest_id,user_id,bot_id,registered_at) "
        "VALUES(?,?,?,'2026-01-03T00:00:00Z')",
        (contest["id"], entrant["id"], bot["id"]),
    ).lastrowid
    conn.execute("DROP TABLE contest_entries_current")
    conn.execute(
        "INSERT INTO contest_pairings(contest_id,entry_a_id,bot_a_id,status) "
        "VALUES(?,?,?,'pending')",
        (contest["id"], second_duplicate, bot["id"]),
    )
    # The keeper has no result row.  Both dropped identities do, so a naive
    # entry_id rewrite would collapse them onto the same UNIQUE key.
    conn.executemany(
        "INSERT INTO contest_stage_results"
        "(contest_id,stage_idx,entry_id,bot_id,points) VALUES(?,?,?,?,?)",
        [
            (contest["id"], 0, duplicate, bot["id"], 3.0),
            (contest["id"], 0, second_duplicate, bot["id"], 1.0),
        ],
    )
    conn.executemany(
        "INSERT INTO contest_official_results"
        "(contest_id,entry_id,stage_idx,rank,points,bot_id,user_id) "
        "VALUES(?,?,?,?,?,?,?)",
        [
            (contest["id"], duplicate, 0, 1, 3.0, bot["id"], entrant["id"]),
            (contest["id"], second_duplicate, 0, 2, 1.0, bot["id"], entrant["id"]),
        ],
    )
    conn.commit()
    conn.close()

    migrated = Store(db)
    with migrated._tx() as c:
        rows = c.execute(
            "SELECT id FROM contest_entries WHERE contest_id=? AND user_id=?",
            (contest["id"], entrant["id"]),
        ).fetchall()
        assert [row[0] for row in rows] == [first]
        pairing_entry = c.execute(
            "SELECT entry_a_id FROM contest_pairings WHERE contest_id=?",
            (contest["id"],),
        ).fetchone()[0]
        assert pairing_entry == first
        stage_results = c.execute(
            "SELECT entry_id, points FROM contest_stage_results "
            "WHERE contest_id=? AND stage_idx=0",
            (contest["id"],),
        ).fetchall()
        assert [(row[0], row[1]) for row in stage_results] == [(first, 3.0)]
        official_results = c.execute(
            "SELECT entry_id, rank FROM contest_official_results WHERE contest_id=?",
            (contest["id"],),
        ).fetchall()
        assert [(row[0], row[1]) for row in official_results] == [(first, 1)]
        unique_indexes = {
            row[1]
            for row in c.execute("PRAGMA index_list(contest_entries)")
            if row[2]
        }
        assert "uq_contest_entries_contest_user" in unique_indexes
    with pytest.raises(ValueError, match="已报名"):
        migrated.add_contest_entry_once(contest["id"], entrant["id"], bot["id"])
    migrated.close()


# ── matches 路由（经 matches_index）────────────────────────────
@pytest.fixture()
def store_with_matches(tmp_path):
    s = Store(str(tmp_path / "m.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    bh = s.create_bot(u["id"], "botH", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    bg = s.create_bot(u["id"], "botG", binary_path="/tmp", format="elf", game_id="gomoku")["id"]
    bp = s.create_bot(u["id"], "botP", binary_path="/tmp", format="elf", game_id="pencil")["id"]
    yield s, u, bh, bg, bp
    s.close()


def test_create_match_routes_to_correct_table(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mh1", bh, bh, game_id="holdem", match_config={"hands": 70})
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.create_match("mp1", bp, bp, game_id="pencil", match_config={"n_dots": 11})
    # 验证写到了正确的物理表
    with s._tx() as c:
        assert c.execute("SELECT game_id FROM matches_holdem WHERE id=?", ("mh1",)).fetchone()["game_id"] == "holdem"
        assert c.execute("SELECT game_id FROM matches_gomoku WHERE id=?", ("mg1",)).fetchone()["game_id"] == "gomoku"
        assert c.execute("SELECT game_id FROM matches_pencil WHERE id=?", ("mp1",)).fetchone()["game_id"] == "pencil"
        # matches_index 维护正确
        assert c.execute("SELECT game_id FROM matches_index WHERE id=?", ("mh1",)).fetchone()["game_id"] == "holdem"
        assert c.execute("SELECT game_id FROM matches_index WHERE id=?", ("mp1",)).fetchone()["game_id"] == "pencil"


def test_get_match_routes_via_index(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mh1", bh, bh, game_id="holdem")
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.create_match("mp1", bp, bp, game_id="pencil")
    assert s.get_match("mh1")["game_id"] == "holdem"
    assert s.get_match("mg1")["game_id"] == "gomoku"
    assert s.get_match("mp1")["game_id"] == "pencil"
    assert s.get_match("nonexistent") is None


def test_update_match_routes_via_index(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.update_match(
        "mg1",
        status="completed",
        winner=0,
        result={"rounds_played": 9, "deltas": [1, -1], "normalized_delta": 1.0},
    )
    m = s.get_match("mg1")
    assert m["status"] == "completed" and m["winner"] == 0
    assert m["result"] == {
        "rounds_played": 9,
        "deltas": [1, -1],
        "normalized_delta": 1.0,
    }


def test_list_matches_cross_game_union(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mh1", bh, bh, game_id="holdem")
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.create_match("mp1", bp, bp, game_id="pencil")
    # 无 game_id → UNION ALL 三表
    allm = s.list_matches(limit=10)
    assert len(allm) == 3
    gids = {m["game_id"] for m in allm}
    assert gids == {"holdem", "gomoku", "pencil"}
    # 单游戏过滤
    assert len(s.list_matches(game_id="gomoku")) == 1


def test_count_matches_and_stats_cross_game(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mh1", bh, bh, game_id="holdem")
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.update_match("mg1", status="completed")
    assert s.count_matches() == 2
    assert s.count_matches("completed") == 1
    st = s.count_stats()
    assert st["matches"] == 2
    assert st["matches_completed"] == 1


def test_like_and_view_route_via_index(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.incr_match_view("mg1")
    s.like(u["id"], "match", "mg1")
    m = s.get_match("mg1")
    assert m["views_count"] == 1 and m["likes_count"] == 1


# ── ratings per-game ─────────────────────────────────────────
def test_ratings_per_game(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    # ensure_rating 建 (bot, game) 行
    s.ensure_rating(bg)
    r = s.get_rating(bg)
    assert r is not None and r["game_id"] == "gomoku"
    # update_rating_row
    s.update_rating_row(bg, rating=1900, matches_played=3)
    assert s.get_rating(bg)["rating"] == 1900
    # add/list history per-game
    s.add_rating_history(bg, 1900, 80, 0.06, 3)
    hist = s.list_rating_history(bg)
    assert len(hist) == 1 and hist[0]["rating"] == 1900


def test_bot_profile_joins_rating_with_game_id(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.ensure_rating(bg)
    s.update_rating_row(bg, rating=2100)
    p = s.bot_profile(bg)
    assert p["rating"] == 2100
    assert p["confidence_low"] is not None
    assert p["confidence_high"] is not None


# ── 旧库迁移（对局丢弃，用户/bot/赛事保留）──────────────────────
def test_migrate_old_db_drops_matches_keeps_users(tmp_path):
    """旧库（单表 matches）迁移后：matches 表消失，对局数据丢弃，用户/bot 保留。"""
    db = str(tmp_path / "old.db")
    # 用旧 schema 建一个带单表 matches 的库（模拟旧库）
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE, email TEXT UNIQUE, password_hash TEXT,
            role TEXT DEFAULT 'user', display_name TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1, email_verified INTEGER DEFAULT 0,
            created_at TEXT, bio TEXT DEFAULT '', avatar TEXT DEFAULT '',
            xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0, last_active_at TEXT);
        CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER, name TEXT, display_name TEXT DEFAULT '',
            description TEXT DEFAULT '', os TEXT DEFAULT '', arch TEXT DEFAULT '',
            format TEXT DEFAULT 'unknown', binary_path TEXT DEFAULT '',
            current_version INTEGER DEFAULT 0, is_public INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1, is_builtin INTEGER DEFAULT 0,
            game_id TEXT DEFAULT 'holdem', created_at TEXT, updated_at TEXT,
            UNIQUE(owner_id, name));
        CREATE TABLE contests (id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, description TEXT DEFAULT '', organizer_id INTEGER,
            status TEXT DEFAULT 'draft', registration_opens_at TEXT,
            registration_closes_at TEXT, starts_at TEXT, ends_at TEXT,
            hands_per_match INTEGER DEFAULT 70, created_at TEXT,
            game_id TEXT DEFAULT 'holdem', stages_json TEXT DEFAULT '[]',
            current_stage_idx INTEGER DEFAULT 0, template_id TEXT DEFAULT 'holdem_swiss_ko',
            rest_ends_at TEXT, match_config_json TEXT DEFAULT '{}');
        CREATE TABLE matches (id TEXT PRIMARY KEY, bot_a_id INTEGER, bot_b_id INTEGER,
            owner_id INTEGER, contest_id INTEGER, hands_played INTEGER DEFAULT 0,
            total_hands INTEGER DEFAULT 70, earnings_a INTEGER DEFAULT 0,
            earnings_b INTEGER DEFAULT 0, winner INTEGER, reason TEXT DEFAULT 'completed',
            net_bb_a REAL DEFAULT 0, match_type TEXT DEFAULT 'challenge',
            status TEXT DEFAULT 'pending', game_id TEXT DEFAULT 'holdem',
            created_at TEXT);
        CREATE TABLE contest_pairings (id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id INTEGER, round_num INTEGER DEFAULT 1, bot_a_id INTEGER,
            bot_b_id INTEGER, match_id TEXT REFERENCES matches(id) ON DELETE SET NULL,
            status TEXT DEFAULT 'pending', stage_idx INTEGER DEFAULT 0,
            stage_key TEXT DEFAULT '', group_id TEXT DEFAULT '',
            bracket_slot INTEGER, color_first INTEGER DEFAULT 0);
        CREATE TABLE ratings (bot_id INTEGER PRIMARY KEY, rating REAL DEFAULT 1500.0,
            rd REAL DEFAULT 350.0, vol REAL DEFAULT 0.06, wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0, draws INTEGER DEFAULT 0, net_chips INTEGER DEFAULT 0,
            matches_played INTEGER DEFAULT 0, last_played_at TEXT);
    """)
    conn.execute("INSERT INTO users(username,email,password_hash,created_at) VALUES('alice','a@ex.com','h','2026-01-01')")
    conn.execute("INSERT INTO bots(owner_id,name,game_id,created_at,updated_at) VALUES(1,'botH','holdem','2026-01-01','2026-01-01')")
    conn.execute("INSERT INTO contests(id,title,organizer_id,created_at) VALUES(1,'old',1,'2026-01-01')")
    conn.execute("INSERT INTO matches(id,bot_a_id,bot_b_id,game_id,status,created_at) VALUES('m1',1,1,'holdem','completed','2026-01-01')")
    conn.execute("INSERT INTO contest_pairings(contest_id,round_num,bot_a_id,bot_b_id,match_id) VALUES(1,1,1,1,'m1')")
    conn.execute("INSERT INTO ratings(bot_id,rating) VALUES(1,1800)")
    conn.commit()
    conn.close()

    # 打开 → 触发迁移
    s = Store(db)
    with s._tx() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    s.close()

    # 对局表丢弃 + 新三表建出
    assert "matches" not in tables
    assert "matches_holdem" in tables and "matches_gomoku" in tables and "matches_pencil" in tables
    assert "matches_index" in tables
    # 用户/bot 保留
    s2 = Store(db)
    assert s2.get_user_by_email("a@ex.com") is not None
    assert s2.get_bot(1) is not None
    assert s2.get_bot(1)["name"] == "botH"
    migrated_contest = s2.get_contest(1)
    assert migrated_contest["title"] == "old"
    assert migrated_contest["showcase_key"] is None
    assert migrated_contest["published_stage_pairing_count"] is None
    # 对局数据丢弃
    assert s2.get_match("m1") is None
    # ratings game_id 回填
    r = s2.get_rating(1)
    assert r is not None and r["game_id"] == "holdem" and r["rating"] == 1800
    # contest_pairings.match_id 清空（旧引用失效）
    with s2._tx() as c:
        cp = c.execute("SELECT match_id FROM contest_pairings WHERE id=1").fetchone()
    assert cp["match_id"] is None
    s2.close()


# ── P0 修复测试（delete_bot FK + delete_match 一致性）─────────

def test_delete_bot_after_match_succeeds(tmp_path):
    """审计 P0：bot 参与过对局后 delete_bot 不再抛 FOREIGN KEY constraint failed。

    分表后 bot_a_id/bot_b_id 改 ON DELETE SET NULL（可空），删 bot 时对局保留、引用置空。
    """
    s = Store(str(tmp_path / "del.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b1 = s.create_bot(u["id"], "bot1", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    b2 = s.create_bot(u["id"], "bot2", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    s.create_match("m1", b1, b2, game_id="holdem")
    # 删 bot1（参与过 m1）——不应抛异常
    assert s.delete_bot(b1) is True
    # 对局保留，bot_a_id 置空（SET NULL）
    m = s.get_match("m1")
    assert m is not None
    assert m["bot_a_id"] is None  # 被删的 bot 引用置空
    assert m["bot_b_id"] == b2  # 另一方保留
    s.close()


def test_delete_user_cascades_through_matches(tmp_path):
    """delete_user 级联到 bots → matches 的 bot_a/b 置空（不再因 RESTRICT 崩）。"""
    s = Store(str(tmp_path / "delu.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b1 = s.create_bot(u["id"], "bot1", binary_path="/tmp", format="elf", game_id="gomoku")["id"]
    b2 = s.create_bot(u["id"], "bot2", binary_path="/tmp", format="elf", game_id="gomoku")["id"]
    s.create_match("m1", b1, b2, game_id="gomoku")
    # 删用户 → 级联删 bots → matches bot_a/b 置空
    assert s.delete_user(u["id"]) is True
    m = s.get_match("m1")
    assert m is not None and m["bot_a_id"] is None and m["bot_b_id"] is None
    s.close()


def test_delete_match_cleans_index_and_replay(store_with_matches):
    """delete_match 删 per-game 行 + matches_index + replay（保 index 不漂移）。"""
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.upsert_replay("mg1", '[{"type":"move"}]')
    # 删前都在
    assert s.get_match("mg1") is not None
    assert s.get_replay("mg1") is not None
    with s._tx() as c:
        assert c.execute("SELECT 1 FROM matches_index WHERE id=?", ("mg1",)).fetchone() is not None
    # 删除
    assert s.delete_match("mg1") is True
    # 删后全清
    assert s.get_match("mg1") is None
    assert s.get_replay("mg1") is None
    with s._tx() as c:
        assert c.execute("SELECT 1 FROM matches_index WHERE id=?", ("mg1",)).fetchone() is None
        assert c.execute("SELECT 1 FROM matches_gomoku WHERE id=?", ("mg1",)).fetchone() is None
    # 再删已删的返回 False
    assert s.delete_match("mg1") is False
    assert s.delete_match("nonexistent") is False


def test_per_game_tables_fk_on_delete_set_null(tmp_path):
    """所有引用 bots 的 FK 都是 CASCADE 或 SET NULL（防 delete_bot 回归）。

    matches_<game>.bot_a/b = SET NULL（对局保留）；contest_*/pair_stats/ratings 等 = CASCADE。
    """
    s = Store(str(tmp_path / "fk.db"))
    with s._tx() as c:
        for t, in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for r in c.execute(f"PRAGMA foreign_key_list({t})"):
                if r["table"] == "bots":
                    on_del = (r["on_delete"] or "").upper()
                    assert on_del in ("CASCADE", "SET NULL"), (
                        f"{t}.{r['from']} → bots FK 应 CASCADE 或 SET NULL，实际 {on_del}"
                    )
    s.close()


def test_migrate_old_db_orphan_ratings_dropped_not_crash(tmp_path):
    """迁移旧库时孤儿 ratings 行（引用已删 bot）被丢弃而非崩溃启动。"""
    db = str(tmp_path / "orphan.db")
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE, email TEXT UNIQUE, password_hash TEXT,
            role TEXT DEFAULT 'user', display_name TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1, email_verified INTEGER DEFAULT 0,
            created_at TEXT, bio TEXT DEFAULT '', avatar TEXT DEFAULT '',
            xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0, last_active_at TEXT);
        CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER, name TEXT, display_name TEXT DEFAULT '',
            description TEXT DEFAULT '', os TEXT DEFAULT '', arch TEXT DEFAULT '',
            format TEXT DEFAULT 'unknown', binary_path TEXT DEFAULT '',
            current_version INTEGER DEFAULT 0, is_public INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1, is_builtin INTEGER DEFAULT 0,
            game_id TEXT DEFAULT 'holdem', created_at TEXT, updated_at TEXT,
            UNIQUE(owner_id, name));
        CREATE TABLE ratings (bot_id INTEGER PRIMARY KEY, rating REAL DEFAULT 1500.0,
            rd REAL DEFAULT 350.0, vol REAL DEFAULT 0.06, wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0, draws INTEGER DEFAULT 0, net_chips INTEGER DEFAULT 0,
            matches_played INTEGER DEFAULT 0, last_played_at TEXT);
    """)
    conn.execute("INSERT INTO users(username,email,password_hash,created_at) VALUES('a','a@e.com','h','2026')")
    conn.execute("INSERT INTO bots(owner_id,name,game_id,created_at,updated_at) VALUES(1,'bot1','holdem','2026','2026')")
    # bot_id=1 存在；bot_id=999 是孤儿（bots 表无此行）
    conn.execute("INSERT INTO ratings(bot_id,rating) VALUES(1,1800)")
    conn.execute("INSERT INTO ratings(bot_id,rating) VALUES(999,1500)")
    conn.commit()
    conn.close()

    # 迁移不应崩溃
    s = Store(db)
    r1 = s.get_rating(1)
    r_orphan = s.get_rating(999)
    s.close()
    assert r1 is not None and r1["rating"] == 1800  # 有效行保留
    assert r_orphan is None  # 孤儿行被丢弃（FK 校验：bots 表无 999）


# ── 审计 P1：跨游戏聚合遍历注册表（防第 4 游戏静默漏统计）─────────

def test_all_game_ids_derived_from_registry():
    """_all_game_ids 从注册表派生（db.py 跨游戏聚合用它，不再硬编码元组）。"""
    from bzplat.backend.store.db import _all_game_ids
    from bzplat.backend.games import registry
    assert _all_game_ids() == registry.all_ids()
    assert "holdem" in _all_game_ids() and "gomoku" in _all_game_ids() and "pencil" in _all_game_ids()


def test_cross_game_stats_cover_all_registered_games(store_with_matches):
    """count_stats / count_matches / list_matches 跨游戏聚合覆盖注册表全部游戏。

    审计 HIGH：曾硬编码 ("holdem","gomoku","pencil")，新增第 4 游戏会静默漏掉。
    此测试用各注册游戏各建一场对局，断言统计含全部——若有人加第 4 游戏但忘了
    更新 db.py，此处仍应覆盖（因 _all_game_ids 从注册表派生）。
    """
    s, u, bh, bg, bp = store_with_matches
    # 各注册游戏各建一场
    for gid, bot in (("holdem", bh), ("gomoku", bg), ("pencil", bp)):
        s.create_match(f"m_{gid}", bot, bot, game_id=gid)
    # count_matches 跨游戏 = 注册游戏数
    from bzplat.backend.games import registry
    assert s.count_matches() == len(registry.all_ids())
    # count_stats
    st = s.count_stats()
    assert st["matches"] == len(registry.all_ids())
    # list_matches 跨游戏 UNION 含全部
    allm = s.list_matches(limit=50)
    gids = {m["game_id"] for m in allm}
    assert gids == registry.all_ids()
    s.close()


# ── DB 完整性修复（审计 P0）：FK 全局开 + 孤儿清理 + 去重索引 + 删孤儿表 ────────
def test_connection_has_fk_on_at_init(tmp_path):
    """连接级 FK=ON（审计：修前只在 _tx 内 ON，绕过 _tx 的删除不级联→留孤儿）。"""
    s = Store(str(tmp_path / "fk.db"))
    val = s._conn.execute("PRAGMA foreign_keys").fetchone()[0]
    s.close()
    assert val == 1  # 1 = ON at connection level


def test_raw_connection_enforces_fk(tmp_path):
    """绕过 _tx 直接用 _conn 删 user → bots 应级联删（FK 全程 ON）。

    修前（FK 仅 _tx 内 ON）此路径 FK OFF → 不级联 → 留孤儿 ratings/bot_versions。
    """
    s = Store(str(tmp_path / "fk_raw.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    bid = s.create_bot(u["id"], "bot1", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    # 直接用底层连接（绕过 _tx），模拟脚本/restore 路径
    s._conn.execute("DELETE FROM users WHERE id=?", (u["id"],))
    s._conn.commit()
    # users CASCADE → bots 应已级联删除
    assert s._conn.execute("SELECT 1 FROM bots WHERE id=?", (bid,)).fetchone() is None
    s.close()


def test_migrate_cleans_orphan_fk_rows(tmp_path):
    """存量孤儿（FK OFF 期间删 bot/user 残留）在迁移时被清理。"""
    import sqlite3

    db = str(tmp_path / "orphans.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE,
            email TEXT UNIQUE, password_hash TEXT, role TEXT DEFAULT 'user',
            display_name TEXT DEFAULT '', is_active INTEGER DEFAULT 1,
            email_verified INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER,
            name TEXT, display_name TEXT DEFAULT '', description TEXT DEFAULT '',
            os TEXT DEFAULT '', arch TEXT DEFAULT '', format TEXT DEFAULT 'unknown',
            binary_path TEXT DEFAULT '', current_version INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1, is_builtin INTEGER DEFAULT 0,
            game_id TEXT DEFAULT 'holdem', created_at TEXT, updated_at TEXT,
            UNIQUE(owner_id, name));
        CREATE TABLE bot_versions (id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id INTEGER NOT NULL, version INTEGER, binary_path TEXT,
            upload_note TEXT DEFAULT '', checksum TEXT DEFAULT '', size_bytes INTEGER DEFAULT 0,
            os TEXT DEFAULT '', arch TEXT DEFAULT '', format TEXT DEFAULT 'unknown',
            uploaded_at TEXT, UNIQUE(bot_id, version));
        CREATE TABLE password_resets (token TEXT PRIMARY KEY, user_id INTEGER NOT NULL,
            expires_at TEXT, used_at TEXT, created_at TEXT);
        CREATE TABLE contests (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT,
            description TEXT DEFAULT '', organizer_id INTEGER, status TEXT DEFAULT 'draft',
            registration_opens_at TEXT, registration_closes_at TEXT, starts_at TEXT,
            ends_at TEXT, hands_per_match INTEGER DEFAULT 70, created_at TEXT,
            game_id TEXT DEFAULT 'holdem', stages_json TEXT DEFAULT '[]',
            current_stage_idx INTEGER DEFAULT 0, template_id TEXT DEFAULT 'holdem_swiss_ko',
            rest_ends_at TEXT, match_config_json TEXT DEFAULT '{}');
    """
    )
    conn.execute("INSERT INTO users(username,email,password_hash,created_at) VALUES('a','a@e.com','h','2026')")
    conn.execute("INSERT INTO bots(owner_id,name,game_id,created_at,updated_at) VALUES(1,'bot1','holdem','2026','2026')")
    # 孤儿：bot_versions.bot_id=999, password_resets.user_id=999（父行不存在）
    conn.execute("INSERT INTO bot_versions(bot_id,version,binary_path,uploaded_at) VALUES(999,1,'/x','2026')")
    conn.execute("INSERT INTO password_resets(token,user_id,expires_at,created_at) VALUES('tok',999,'2026','2026')")
    conn.commit()
    conn.close()

    s = Store(db)  # 触发迁移 + 孤儿清理
    with s._tx() as c:
        assert c.execute("SELECT COUNT(*) FROM bot_versions WHERE bot_id=999").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM password_resets WHERE user_id=999").fetchone()[0] == 0
    s.close()


def test_legacy_per_game_indexes_dropped(tmp_path):
    """schema.py 旧字面索引（idx_m{game}_bot_a 等）被迁移删除，仅保留 loop 建的。"""
    import sqlite3
    from bzplat.backend.store.schema import SCHEMA

    db = str(tmp_path / "dupidx.db")
    conn = sqlite3.connect(db)
    conn.executescript(SCHEMA)  # 含 idx_mholdem_bot_a 等 18 个旧索引
    conn.commit()
    conn.close()
    s = Store(db)
    with s._tx() as c:
        idx = {r[1] for r in c.execute("PRAGMA index_list('matches_holdem')")}
    s.close()
    # 旧名应消失
    assert "idx_mholdem_bot_a" not in idx
    assert "idx_mholdem_time" not in idx
    # 新名保留
    assert "idx_mholdem_bot_a_id" in idx
    assert "idx_mholdem_created_at" in idx


def test_migrate_drops_unregistered_matches_table(tmp_path):
    """下线游戏的 matches_<game> 残留表被 DROP（如 matches_reversi）。"""
    db = str(tmp_path / "reversi.db")
    s1 = Store(db)
    with s1._tx() as c:
        c.execute("CREATE TABLE matches_reversi (id TEXT PRIMARY KEY, bot_a_id INTEGER)")
        c.execute("INSERT INTO matches_reversi(id) VALUES('r1')")
    s1.close()
    # 重开 → 迁移应 DROP matches_reversi（reversi 不在注册表）
    s2 = Store(db)
    with s2._tx() as c:
        tabs = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    s2.close()
    assert "matches_reversi" not in tabs
    # matches_index（合法、名字相似）必须保留
    assert "matches_index" in tabs


# ── 迁移完整性收尾（对抗审计：contest 侧孤儿清理 + 全库 FK check）────────────
def test_migrate_cleans_contest_side_orphans_and_passes_fk_check(tmp_path):
    """赛事侧孤儿（contest_id / organizer_id）在迁移时被清理，且迁移后
    PRAGMA foreign_key_check 返回 0 行（无任何 FK 违规，catch ALL 遗漏 FK 目标）。

    覆盖 PR #88/#93 遗漏的 contest 侧：
      - contests.organizer_id 指向已删 user（NO ACTION + NOT NULL → 删整条 contest）
      - contest_entries/pairings/stage_results/official_results.contest_id 指向已删 contest
      - contest_entries.user_id 指向已删 user
    """
    import sqlite3

    db = str(tmp_path / "contest_orphans.db")
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE,
            email TEXT UNIQUE, password_hash TEXT, role TEXT DEFAULT 'user',
            display_name TEXT DEFAULT '', is_active INTEGER DEFAULT 1,
            email_verified INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER,
            name TEXT, display_name TEXT DEFAULT '', description TEXT DEFAULT '',
            os TEXT DEFAULT '', arch TEXT DEFAULT '', format TEXT DEFAULT 'unknown',
            binary_path TEXT DEFAULT '', current_version INTEGER DEFAULT 0,
            is_active INTEGER DEFAULT 1, is_builtin INTEGER DEFAULT 0,
            game_id TEXT DEFAULT 'holdem', created_at TEXT, updated_at TEXT,
            UNIQUE(owner_id, name));
        CREATE TABLE contests (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT,
            description TEXT DEFAULT '', organizer_id INTEGER NOT NULL,
            status TEXT DEFAULT 'draft', registration_opens_at TEXT,
            registration_closes_at TEXT, starts_at TEXT, ends_at TEXT,
            hands_per_match INTEGER DEFAULT 70, created_at TEXT,
            game_id TEXT DEFAULT 'holdem', stages_json TEXT DEFAULT '[]',
            current_stage_idx INTEGER DEFAULT 0, template_id TEXT DEFAULT 'holdem_swiss_ko',
            rest_ends_at TEXT, match_config_json TEXT DEFAULT '{}');
        CREATE TABLE contest_entries (id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id INTEGER, user_id INTEGER, bot_id INTEGER,
            registered_at TEXT, group_id TEXT DEFAULT '', seed INTEGER DEFAULT 0,
            eliminated INTEGER DEFAULT 0, dispatched_at TEXT);
        CREATE TABLE contest_pairings (id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id INTEGER, round_num INTEGER DEFAULT 1, bot_a_id INTEGER,
            bot_b_id INTEGER, match_id TEXT, status TEXT DEFAULT 'pending',
            stage_idx INTEGER DEFAULT 0, stage_key TEXT DEFAULT '',
            group_id TEXT DEFAULT '', bracket_slot INTEGER, color_first INTEGER DEFAULT 0);
        CREATE TABLE contest_stage_results (id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id INTEGER, stage_idx INTEGER, stage_key TEXT DEFAULT '',
            bot_id INTEGER, points REAL DEFAULT 0, wins INTEGER DEFAULT 0,
            draws INTEGER DEFAULT 0, losses INTEGER DEFAULT 0, net_chips INTEGER DEFAULT 0,
            group_id TEXT DEFAULT '', rank_in_group INTEGER, payload_json TEXT DEFAULT '{}');
        CREATE TABLE contest_official_results (id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id INTEGER, entry_id INTEGER, stage_idx INTEGER DEFAULT 0,
            rank INTEGER, points REAL DEFAULT 0, bot_id INTEGER, user_id INTEGER,
            tiebreaks_json TEXT DEFAULT '{}', awarded TEXT DEFAULT '');
    """
    )
    # user=1 / bot=1 / contest=1 合法；contest=2 organizer=999 孤儿；
    # contest_* 指向 contest=888（孤儿）/ user=999（孤儿）
    conn.execute("INSERT INTO users(username,email,password_hash,created_at) VALUES('a','a@e.com','h','2026')")
    conn.execute("INSERT INTO bots(owner_id,name,game_id,created_at,updated_at) VALUES(1,'b','holdem','2026','2026')")
    conn.execute("INSERT INTO contests(id,title,organizer_id,created_at) VALUES(1,'ok',1,'2026')")
    conn.execute("INSERT INTO contests(id,title,organizer_id,created_at) VALUES(2,'orphan_org',999,'2026')")
    conn.execute("INSERT INTO contest_entries(contest_id,user_id,registered_at) VALUES(1,1,'2026')")
    conn.execute("INSERT INTO contest_entries(contest_id,user_id,registered_at) VALUES(888,1,'2026')")   # orphan contest
    conn.execute("INSERT INTO contest_entries(contest_id,user_id,registered_at) VALUES(1,999,'2026')")   # orphan user
    conn.execute("INSERT INTO contest_pairings(contest_id,round_num) VALUES(888,1)")
    conn.execute("INSERT INTO contest_stage_results(contest_id,stage_idx) VALUES(888,0)")
    conn.execute("INSERT INTO contest_official_results(contest_id,entry_id,rank) VALUES(888,1,1)")
    conn.commit()
    conn.close()

    s = Store(db)  # 迁移 + 清理，不应崩溃

    with s._tx() as c:
        # (1) 合法行保留
        assert c.execute("SELECT COUNT(*) FROM contests WHERE id=1").fetchone()[0] == 1
        assert c.execute("SELECT COUNT(*) FROM contest_entries WHERE contest_id=1 AND user_id=1").fetchone()[0] == 1
        # (2) 孤儿行已清
        assert c.execute("SELECT COUNT(*) FROM contests WHERE id=2").fetchone()[0] == 0  # organizer 孤儿→删
        assert c.execute("SELECT COUNT(*) FROM contest_entries WHERE contest_id=888 OR user_id=999").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM contest_pairings WHERE contest_id=888").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM contest_stage_results WHERE contest_id=888").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM contest_official_results WHERE contest_id=888").fetchone()[0] == 0
        # (3) 关键：全库 FK 完整性校验通过（0 行 = 无违规，catch ANY 遗漏 FK 目标）
        violations = c.execute("PRAGMA foreign_key_check").fetchall()
        assert violations == [], f"FK 违规残留：{violations}"
    s.close()
