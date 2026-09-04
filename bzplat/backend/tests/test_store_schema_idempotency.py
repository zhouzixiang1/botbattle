"""Store schema migration idempotency and trigger-definition guards."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bzplat.backend.runtime.config import AUTO_MATCH_SCHEDULER_POLICY_VERSION
from bzplat.backend.store import Store
from bzplat.backend.store.db import _ensure_trigger
from bzplat.backend.store.schema import CONTEST_ENTRY_PAGE_INDEX_SQL, VALID_GAME_IDS


_PROJECTION_BUMP = (
    "UPDATE rating_projection_state SET "
    "mutation_revision=mutation_revision+1 WHERE singleton=1"
)
_EXPECTED_TRIGGER_COUNT = 65


def _normalize_sql(sql: str) -> str:
    return " ".join(sql.rstrip(";").split())


def _schema_state(db_path: Path) -> tuple[int, dict[str, str]]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        triggers = {
            str(name): _normalize_sql(str(sql))
            for name, sql in conn.execute(
                "SELECT name,sql FROM sqlite_master "
                "WHERE type='trigger' ORDER BY name"
            ).fetchall()
        }
    return schema_version, triggers


def _assert_fragments(sql: str, *fragments: str) -> None:
    for fragment in fragments:
        assert fragment in sql, (fragment, sql)


def _assert_owner_delete_trigger_contract(triggers: dict[str, str]) -> None:
    assert len(triggers) == _EXPECTED_TRIGGER_COUNT, sorted(triggers)
    contracts = {
        "trg_bots_owner_deleted_guard_insert": (
            "BEFORE INSERT ON bots",
            "deleted Bot must be inactive and unranked",
        ),
        "trg_bots_owner_deleted_guard_update": (
            "BEFORE UPDATE OF owner_deleted_at,is_active,is_ranked ON bots",
            "deleted Bot tombstone invariant",
        ),
        "trg_contest_entries_live_bot_insert": (
            "BEFORE INSERT ON contest_entries",
            "contest entry Bot must be active",
        ),
        "trg_contest_entries_live_bot_update": (
            "BEFORE UPDATE OF bot_id ON contest_entries",
            "contest entry Bot must be active",
        ),
        "trg_contests_live_state_deleted_bot_guard": (
            "BEFORE UPDATE OF status ON contests",
            "live contest cannot reference owner-deleted Bot",
        ),
        "trg_contest_pairings_live_bot_insert": (
            "BEFORE INSERT ON contest_pairings",
            "live contest pairing cannot reference owner-deleted Bot",
        ),
        "trg_contest_pairings_live_bot_update": (
            "BEFORE UPDATE OF contest_id,bot_a_id,bot_b_id ON contest_pairings",
            "live contest pairing cannot reference owner-deleted Bot",
        ),
    }
    for name, fragments in contracts.items():
        _assert_fragments(triggers.get(name, ""), *fragments)


def _assert_contest_pairing_topology_trigger_contract(
    triggers: dict[str, str],
) -> None:
    contracts = {
        "trg_contest_pairing_topology_insert": (
            "AFTER INSERT ON contest_pairings",
            "pairing_topology_revision=pairing_topology_revision+1",
            "WHERE id=NEW.contest_id",
        ),
        "trg_contest_pairing_topology_delete": (
            "AFTER DELETE ON contest_pairings",
            "pairing_topology_revision=pairing_topology_revision+1",
            "WHERE id=OLD.contest_id",
        ),
        "trg_contest_pairing_topology_update": (
            "AFTER UPDATE OF id,contest_id,round_num,entry_a_id,entry_b_id,"
            "bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id,stage_idx,"
            "stage_key,group_id,bracket_slot,color_first,series_index,series_size,"
            "tiebreak_group,tiebreak_game,pairing_seed,published_at ON contest_pairings",
            "OLD.id IS NOT NEW.id",
            "OLD.contest_id IS NOT NEW.contest_id",
            "OLD.round_num IS NOT NEW.round_num",
            "OLD.entry_a_id IS NOT NEW.entry_a_id",
            "OLD.entry_b_id IS NOT NEW.entry_b_id",
            "OLD.bot_a_id IS NOT NEW.bot_a_id",
            "OLD.bot_b_id IS NOT NEW.bot_b_id",
            "OLD.bot_a_version_id IS NOT NEW.bot_a_version_id",
            "OLD.bot_b_version_id IS NOT NEW.bot_b_version_id",
            "OLD.stage_idx IS NOT NEW.stage_idx",
            "OLD.stage_key IS NOT NEW.stage_key",
            "OLD.group_id IS NOT NEW.group_id",
            "OLD.bracket_slot IS NOT NEW.bracket_slot",
            "OLD.color_first IS NOT NEW.color_first",
            "OLD.series_index IS NOT NEW.series_index",
            "OLD.series_size IS NOT NEW.series_size",
            "OLD.tiebreak_group IS NOT NEW.tiebreak_group",
            "OLD.tiebreak_game IS NOT NEW.tiebreak_game",
            "OLD.pairing_seed IS NOT NEW.pairing_seed",
            "OLD.published_at IS NOT NEW.published_at",
            "WHERE id=OLD.contest_id OR id=NEW.contest_id",
        ),
        "trg_contest_pairing_topology_stage_cursor": (
            "AFTER UPDATE OF current_stage_idx ON contests",
            "OLD.current_stage_idx IS NOT NEW.current_stage_idx",
            "pairing_topology_revision=pairing_topology_revision+1",
            "WHERE id=NEW.id",
        ),
        "trg_contest_pairing_topology_manifest": (
            "AFTER UPDATE OF published_stage_pairing_count ON contests",
            "OLD.published_stage_pairing_count IS NOT "
            "NEW.published_stage_pairing_count",
            "pairing_topology_revision=pairing_topology_revision+1",
            "WHERE id=NEW.id",
        ),
        "trg_execution_contest_pairing_ref_insert": (
            "BEFORE INSERT ON execution_jobs",
            "NEW.source='contest'",
            "NEW.contest_id IS NULL",
            "NEW.contest_pairing_id IS NULL",
            "pairing.id=NEW.contest_pairing_id",
            "pairing.contest_id=NEW.contest_id",
            "typeof(contest.current_stage_idx)='integer'",
            "pairing.stage_idx=contest.current_stage_idx",
            "contest execution job must reference its contest pairing",
        ),
        "trg_execution_contest_pairing_ref_update": (
            "BEFORE UPDATE OF source,contest_id,contest_pairing_id "
            "ON execution_jobs",
            "NEW.source='contest'",
            "NEW.contest_id IS NULL",
            "NEW.contest_pairing_id IS NULL",
            "pairing.id=NEW.contest_pairing_id",
            "pairing.contest_id=NEW.contest_id",
            "typeof(contest.current_stage_idx)='integer'",
            "pairing.stage_idx=contest.current_stage_idx",
            "contest execution job must reference its contest pairing",
        ),
    }
    for name, fragments in contracts.items():
        _assert_fragments(triggers.get(name, ""), *fragments)


def _assert_contest_lifecycle_revision_trigger_contract(
    triggers: dict[str, str],
) -> None:
    contracts = {
        "trg_contest_lifecycle_revision_update": (
            "AFTER UPDATE OF game_id,template_id,stages_json,format_snapshot_json,"
            "source_contest_id,status ON contests",
            "OLD.game_id IS NOT NEW.game_id",
            "OLD.template_id IS NOT NEW.template_id",
            "OLD.stages_json IS NOT NEW.stages_json",
            "OLD.format_snapshot_json IS NOT NEW.format_snapshot_json",
            "OLD.source_contest_id IS NOT NEW.source_contest_id",
            "OLD.status IN ('rest','finished')",
            "NEW.status IN ('rest','finished')",
        ),
        "trg_contest_entries_lifecycle_revision_insert": (
            "AFTER INSERT ON contest_entries",
            "WHERE id=NEW.contest_id",
        ),
        "trg_contest_entries_lifecycle_revision_delete": (
            "AFTER DELETE ON contest_entries",
            "WHERE id=OLD.contest_id",
        ),
        "trg_contest_entries_lifecycle_revision_update": (
            "AFTER UPDATE OF id,contest_id,user_id,bot_id,group_id,seed,eliminated "
            "ON contest_entries",
            "OLD.id IS NOT NEW.id",
            "OLD.contest_id IS NOT NEW.contest_id",
            "OLD.user_id IS NOT NEW.user_id",
            "OLD.bot_id IS NOT NEW.bot_id",
            "OLD.group_id IS NOT NEW.group_id",
            "OLD.seed IS NOT NEW.seed",
            "OLD.eliminated IS NOT NEW.eliminated",
            "WHERE id=OLD.contest_id OR id=NEW.contest_id",
        ),
        "trg_contest_stage_results_lifecycle_revision_insert": (
            "AFTER INSERT ON contest_stage_results",
            "WHERE id=NEW.contest_id",
        ),
        "trg_contest_stage_results_lifecycle_revision_delete": (
            "AFTER DELETE ON contest_stage_results",
            "WHERE id=OLD.contest_id",
        ),
        "trg_contest_stage_results_lifecycle_revision_update": (
            "AFTER UPDATE OF id,contest_id,stage_idx,stage_key,entry_id,bot_id,"
            "points,wins,draws,losses,delta_total,group_id,rank_in_group,payload_json "
            "ON contest_stage_results",
            "OLD.id IS NOT NEW.id",
            "OLD.contest_id IS NOT NEW.contest_id",
            "OLD.stage_idx IS NOT NEW.stage_idx",
            "OLD.stage_key IS NOT NEW.stage_key",
            "OLD.entry_id IS NOT NEW.entry_id",
            "OLD.bot_id IS NOT NEW.bot_id",
            "OLD.points IS NOT NEW.points",
            "OLD.wins IS NOT NEW.wins",
            "OLD.draws IS NOT NEW.draws",
            "OLD.losses IS NOT NEW.losses",
            "OLD.delta_total IS NOT NEW.delta_total",
            "OLD.group_id IS NOT NEW.group_id",
            "OLD.rank_in_group IS NOT NEW.rank_in_group",
            "OLD.payload_json IS NOT NEW.payload_json",
            "WHERE id=OLD.contest_id OR id=NEW.contest_id",
        ),
    }
    for name, fragments in contracts.items():
        _assert_fragments(
            triggers.get(name, ""),
            "pairing_topology_revision=pairing_topology_revision+1",
            *fragments,
        )


def _assert_contest_source_search_trigger_contract(
    triggers: dict[str, str],
) -> None:
    contracts = {
        "trg_contest_source_search_insert": (
            "AFTER INSERT ON contests",
            "INSERT OR IGNORE INTO contest_source_search_grams",
            "substr(NEW.title,pos,shape.gram_len)",
        ),
        "trg_contest_source_search_update": (
            "AFTER UPDATE OF title,game_id,created_at,organizer_id,status,"
            "official_results_ready,showcase_key ON contests",
            "DELETE FROM contest_source_search_grams WHERE contest_id=OLD.id",
            "substr(NEW.title,pos,shape.gram_len)",
        ),
        "trg_contest_source_search_delete": (
            "AFTER DELETE ON contests",
            "DELETE FROM contest_source_search_grams WHERE contest_id=OLD.id",
        ),
    }
    for name, fragments in contracts.items():
        _assert_fragments(triggers.get(name, ""), *fragments)

    title_guards = {
        "trg_contest_title_guard_insert": "BEFORE INSERT ON contests",
        "trg_contest_title_guard_update": "BEFORE UPDATE OF title ON contests",
    }
    for name, fragment in title_guards.items():
        _assert_fragments(
            triggers.get(name, ""),
            fragment,
            "contest title invalid",
        )


def _assert_rating_trigger_contract(triggers: dict[str, str]) -> None:
    global_contracts = {
        "trg_match_rating_policy_source_immutable": (
            "BEFORE UPDATE OF match_id,game_id,rating_pool_id,bot_a_id,"
            "bot_b_id,rated,rating_reason,source,classified_at "
            "ON match_rating_policies",
            "rating policy source immutable",
        ),
        "trg_match_rating_policy_settled_delete": (
            "BEFORE DELETE ON match_rating_policies",
            "settled rating policy immutable",
        ),
        "trg_match_rating_policy_identity_insert": (
            "BEFORE INSERT ON match_rating_policies",
            "rating policy identity invalid",
        ),
        "trg_match_rating_policy_identity_update": (
            "BEFORE UPDATE OF game_id,bot_a_id,bot_b_id,rated "
            "ON match_rating_policies",
            "rating policy identity invalid",
        ),
        "trg_match_rating_policy_order_immutable": (
            "BEFORE UPDATE OF settled_order ON match_rating_policies",
            "rating policy settled_order immutable",
        ),
        "trg_match_rating_settlement_order_insert": (
            "BEFORE INSERT ON match_rating_settlements",
            "COALESCE(MAX(settled_order),0)+1",
            "rating settlement order must be next",
        ),
        "trg_match_rating_settlement_order_immutable": (
            "BEFORE UPDATE OF match_id,settled_at,settled_order "
            "ON match_rating_settlements",
            "rating settlement source immutable",
        ),
        "trg_match_rating_settlement_delete_immutable": (
            "BEFORE DELETE ON match_rating_settlements",
            "rating settlement source immutable",
        ),
        "trg_match_rating_projection_dirty_on_delete": (
            "AFTER DELETE ON match_rating_settlements",
            "policy_version='projection-dirty'",
            "rebuilt_at=NULL",
        ),
        "trg_bots_projection_mutation_insert": (
            "AFTER INSERT ON bots",
            _PROJECTION_BUMP,
        ),
        "trg_bots_projection_mutation_delete": (
            "AFTER DELETE ON bots",
            _PROJECTION_BUMP,
        ),
        "trg_bots_projection_mutation_update": (
            "AFTER UPDATE OF owner_id,game_id,is_active,is_ranked,format,os,arch ON bots",
            "OLD.owner_id IS NOT NEW.owner_id",
            "OLD.is_ranked IS NOT NEW.is_ranked",
            _PROJECTION_BUMP,
        ),
        "trg_match_rating_policy_projection_mutation_order": (
            "AFTER UPDATE OF settled_order ON match_rating_policies",
            _PROJECTION_BUMP,
        ),
        "trg_rating_settlement_sequence_projection_mutation": (
            "AFTER UPDATE OF next_order ON rating_settlement_sequence",
            _PROJECTION_BUMP,
        ),
        "trg_match_rating_settlement_projection_mutation_insert": (
            "AFTER INSERT ON match_rating_settlements",
            "NEW.settled_order>0",
            _PROJECTION_BUMP,
        ),
    }

    expected_names = set(global_contracts)
    for table in ("ratings", "rating_history", "pair_stats"):
        for operation in ("insert", "update", "delete"):
            name = f"trg_{table}_projection_mutation_{operation}"
            expected_names.add(name)
            _assert_fragments(
                triggers.get(name, ""),
                f"AFTER {operation.upper()} ON {table}",
                _PROJECTION_BUMP,
            )

    for game_id in sorted(VALID_GAME_IDS):
        table = f"matches_{game_id}"
        game_contracts = {
            f"trg_{table}_rated_overlap_insert": (
                f"BEFORE INSERT ON {table}",
                "NEW.status IN ('pending','running')",
                "json_type(NEW.match_config,'$._rating_eligible') IN ('true','false')",
                "LEFT JOIN match_rating_policies policy ON policy.match_id=m.id",
                "COALESCE(policy.rated,1)=1",
                "rated match lifecycle overlap",
            ),
            f"trg_{table}_rated_overlap_update": (
                "BEFORE UPDATE OF bot_a_id,bot_b_id,match_type,status "
                f"ON {table}",
                "NEW.status IN ('pending','running')",
                "SELECT frozen.rated FROM match_rating_policies frozen",
                "LEFT JOIN match_rating_policies policy ON policy.match_id=m.id",
                "COALESCE(policy.rated,1)=1",
                "rated match lifecycle overlap",
            ),
            f"trg_{table}_rating_source_update": (
                f"BEFORE UPDATE OF id,winner,result,ended_at,status ON {table}",
                "match_rating_settlements",
                "settled match rating source immutable",
            ),
            f"trg_{table}_rating_source_delete": (
                f"BEFORE DELETE ON {table}",
                "match_rating_settlements",
                "settled match rating source immutable",
            ),
            f"trg_{table}_projection_mutation_source": (
                f"AFTER UPDATE OF id,winner,result,ended_at,status ON {table}",
                "policy.settled_order IS NOT NULL",
                _PROJECTION_BUMP,
            ),
        }
        expected_names.update(game_contracts)
        for name, fragments in game_contracts.items():
            _assert_fragments(triggers.get(name, ""), *fragments)
        for operation in ("insert", "update"):
            assert "JOIN bots rating_a" not in triggers[
                f"trg_{table}_rated_overlap_{operation}"
            ]

    missing = expected_names - set(triggers)
    assert not missing, sorted(missing)
    for name, fragments in global_contracts.items():
        _assert_fragments(triggers.get(name, ""), *fragments)


def test_store_reopen_preserves_schema_and_rating_trigger_contract(tmp_path):
    db_path = (tmp_path / "schema-idempotency.db").resolve()
    store = Store(str(db_path))
    store.close()
    version_before, triggers_before = _schema_state(db_path)
    _assert_rating_trigger_contract(triggers_before)
    _assert_owner_delete_trigger_contract(triggers_before)
    _assert_contest_pairing_topology_trigger_contract(triggers_before)
    _assert_contest_lifecycle_revision_trigger_contract(triggers_before)
    _assert_contest_source_search_trigger_contract(triggers_before)
    with sqlite3.connect(db_path) as conn:
        entry_page_index_before = _normalize_sql(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_contest_entries_page_order",),
            ).fetchone()[0]
        )
    assert entry_page_index_before == _normalize_sql(CONTEST_ENTRY_PAGE_INDEX_SQL)

    reopened = Store(str(db_path))
    reopened.close()
    version_after, triggers_after = _schema_state(db_path)

    with sqlite3.connect(db_path) as conn:
        entry_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(contest_entries)")
        }
        auto_columns = {
            row[1]
            for row in conn.execute(
                "PRAGMA table_info(auto_match_fair_state)"
            )
        }
        auto_state = conn.execute(
            "SELECT dispatch_policy_version,next_eligible_at,gate_reason "
            "FROM auto_match_fair_state WHERE singleton=1"
        ).fetchone()
        entry_page_index_after = _normalize_sql(
            conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
                ("idx_contest_entries_page_order",),
            ).fetchone()[0]
        )
        assert {"dispatch_policy_version", "next_eligible_at", "gate_reason"} <= (
            auto_columns
        )
        assert auto_state == ("", None, "idle_grace")
        assert conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert {
        "real_name_snapshot",
        "phone_snapshot",
        "school_snapshot",
        "student_id_snapshot",
        "identity_captured_at",
        "identity_source",
    } <= entry_columns

    assert version_after == version_before
    assert triggers_after == triggers_before
    assert entry_page_index_after == entry_page_index_before
    _assert_rating_trigger_contract(triggers_after)
    _assert_owner_delete_trigger_contract(triggers_after)
    _assert_contest_pairing_topology_trigger_contract(triggers_after)
    _assert_contest_lifecycle_revision_trigger_contract(triggers_after)
    _assert_contest_source_search_trigger_contract(triggers_after)


def test_pairing_seed_lookup_index_is_fresh_migrated_and_planned(tmp_path):
    db_path = (tmp_path / "pairing-seed-index.db").resolve()
    fresh = Store(str(db_path))
    with fresh._tx() as conn:
        indexes = {
            row[1] for row in conn.execute("PRAGMA index_list(contest_pairings)")
        }
        assert "idx_contest_pairings_seed_lookup" in indexes
        plan = " ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? AND pairing_seed=? LIMIT 1",
                (1, 0, 123),
            )
        )
        assert "idx_contest_pairings_seed_lookup" in plan
    fresh.close()

    # Model an upgraded database whose old schema predates the lookup index.
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP INDEX idx_contest_pairings_seed_lookup")

    migrated = Store(str(db_path))
    with migrated._tx() as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND name=?",
            ("idx_contest_pairings_seed_lookup",),
        ).fetchone()[0]
        assert "WHERE pairing_seed IS NOT NULL" in _normalize_sql(sql)
        migrated_plan = " ".join(
            str(row[3])
            for row in conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? AND pairing_seed=? LIMIT 1",
                (1, 0, 456),
            )
        )
        assert "idx_contest_pairings_seed_lookup" in migrated_plan
    migrated.close()

    reopened = Store(str(db_path))
    assert reopened._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert reopened._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()


def test_auto_scheduler_gate_columns_migrate_and_reconcile_idempotently(tmp_path):
    db_path = (tmp_path / "legacy-auto-scheduler-gate.db").resolve()
    legacy = Store(str(db_path))
    legacy.close()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "CREATE TABLE auto_match_fair_state_legacy ("
            "singleton INTEGER PRIMARY KEY CHECK (singleton=1),"
            "next_game_idx INTEGER NOT NULL DEFAULT 0 CHECK (next_game_idx>=0),"
            "next_lane INTEGER NOT NULL DEFAULT 0 CHECK (next_lane IN (0,1)),"
            "revision INTEGER NOT NULL DEFAULT 0 CHECK (revision>=0),"
            "bootstrap_version INTEGER NOT NULL DEFAULT 0 "
            "CHECK (bootstrap_version>=0),"
            "updated_at TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO auto_match_fair_state_legacy("
            "singleton,next_game_idx,next_lane,revision,bootstrap_version,updated_at) "
            "SELECT singleton,next_game_idx,next_lane,revision,bootstrap_version,"
            "updated_at FROM auto_match_fair_state"
        )
        conn.execute("DROP TABLE auto_match_fair_state")
        conn.execute(
            "ALTER TABLE auto_match_fair_state_legacy "
            "RENAME TO auto_match_fair_state"
        )

    migrated = Store(str(db_path))
    with migrated._tx() as conn:
        columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(auto_match_fair_state)")
        }
        assert {"dispatch_policy_version", "next_eligible_at", "gate_reason"} <= (
            columns
        )
        assert tuple(
            conn.execute(
                "SELECT dispatch_policy_version,next_eligible_at,gate_reason "
                "FROM auto_match_fair_state WHERE singleton=1"
            ).fetchone()
        ) == ("", None, "idle_grace")
    reconciled = migrated.executions.reconcile_auto_scheduler_policy()
    assert reconciled["changed"] is True
    installed = migrated._conn.execute(
        "SELECT dispatch_policy_version,next_eligible_at,gate_reason "
        "FROM auto_match_fair_state WHERE singleton=1"
    ).fetchone()
    assert installed["dispatch_policy_version"] == AUTO_MATCH_SCHEDULER_POLICY_VERSION
    assert installed["next_eligible_at"] is not None
    assert installed["gate_reason"] == "idle_grace"
    migrated.close()

    reopened = Store(str(db_path))
    second = reopened.executions.reconcile_auto_scheduler_policy()
    assert second["changed"] is False
    assert second["next_eligible_at"] == installed["next_eligible_at"]
    assert reopened._conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    assert reopened._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()


def test_legacy_contest_entries_gain_nullable_identity_columns_idempotently(tmp_path):
    """迁移只补 nullable 列，不把旧用户当前资料伪装成报名快照。"""
    db_path = (tmp_path / "legacy-contest-identity.db").resolve()
    store = Store(str(db_path))
    organizer = store.create_user("legacy-org", "legacy-org@e.com", "x")
    entrant = store.create_user(
        "legacy-entry", "legacy-entry@e.com", "x", real_name="当前姓名",
        phone="13800138000", school="当前学校", student_id="CURRENT001",
    )
    bot = store.create_bot(
        entrant["id"], "legacy-entry-bot", binary_path="/tmp/legacy-entry",
        format="elf", game_id="holdem",
    )
    contest = store.create_contest(
        "旧实名赛", organizer_id=organizer["id"], game_id="holdem",
        require_real_name=1,
    )
    entry = store.add_contest_entry(contest["id"], entrant["id"], bot["id"])
    store.close()

    identity_columns = (
        "real_name_snapshot", "phone_snapshot", "school_snapshot",
        "student_id_snapshot", "identity_captured_at", "identity_source",
    )
    with sqlite3.connect(db_path) as conn:
        for column in identity_columns:
            conn.execute(f"ALTER TABLE contest_entries DROP COLUMN {column}")

    migrated = Store(str(db_path))
    migrated_entry = migrated.get_entry(contest["id"], entrant["id"])
    assert migrated_entry["id"] == entry["id"]
    assert all(migrated_entry[column] is None for column in identity_columns)
    migrated.close()

    with sqlite3.connect(db_path) as conn:
        first_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        first_columns = tuple(
            row[1] for row in conn.execute("PRAGMA table_info(contest_entries)")
        )
    reopened = Store(str(db_path))
    reopened.close()
    with sqlite3.connect(db_path) as conn:
        assert int(conn.execute("PRAGMA schema_version").fetchone()[0]) == first_version
        assert tuple(
            row[1] for row in conn.execute("PRAGMA table_info(contest_entries)")
        ) == first_columns


def test_docker_launch_journal_schema_and_singleton_are_reopen_idempotent(
    tmp_path,
):
    db_path = (tmp_path / "launch-journal-idempotency.db").resolve()
    first = Store(str(db_path))
    first.close()
    with sqlite3.connect(db_path) as conn:
        before_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
        before_columns = tuple(
            row[1] for row in conn.execute(
                "PRAGMA table_info(docker_launch_journal)"
            ).fetchall()
        )
        assert conn.execute(
            "SELECT COUNT(*) FROM docker_launch_journal WHERE singleton=1"
        ).fetchone()[0] == 1
        assert conn.execute(
            "SELECT state FROM docker_launch_journal WHERE singleton=1"
        ).fetchone()[0] == "idle"
        execution_columns = {
            row[1]
            for row in conn.execute("PRAGMA table_info(execution_jobs)").fetchall()
        }
        assert {"failure_count", "next_attempt_at"} <= execution_columns

    reopened = Store(str(db_path))
    reopened.close()
    with sqlite3.connect(db_path) as conn:
        assert int(conn.execute("PRAGMA schema_version").fetchone()[0]) == before_version
        assert tuple(
            row[1] for row in conn.execute(
                "PRAGMA table_info(docker_launch_journal)"
            ).fetchall()
        ) == before_columns
        assert conn.execute(
            "SELECT COUNT(*) FROM docker_launch_journal WHERE singleton=1"
        ).fetchone()[0] == 1


def test_store_repairs_stale_trigger_definition_once(tmp_path):
    db_path = (tmp_path / "stale-trigger.db").resolve()
    store = Store(str(db_path))
    store.close()

    stale_name = "trg_ratings_projection_mutation_insert"
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"DROP TRIGGER {stale_name}")
        conn.execute(
            f"CREATE TRIGGER {stale_name} AFTER INSERT ON ratings "
            "BEGIN SELECT 1; END"
        )
    stale_version, stale_triggers = _schema_state(db_path)
    assert _PROJECTION_BUMP not in stale_triggers[stale_name]

    repaired = Store(str(db_path))
    repaired.close()
    repaired_version, repaired_triggers = _schema_state(db_path)
    assert repaired_version > stale_version
    _assert_rating_trigger_contract(repaired_triggers)

    stable = Store(str(db_path))
    stable.close()
    stable_version, stable_triggers = _schema_state(db_path)
    assert stable_version == repaired_version
    assert stable_triggers == repaired_triggers


def test_ensure_trigger_rejects_identifier_and_object_type_conflicts():
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE source(id INTEGER)")
        with pytest.raises(ValueError, match="invalid trigger identifier"):
            _ensure_trigger(
                conn,
                "invalid-trigger",
                "CREATE TRIGGER invalid-trigger AFTER INSERT ON source "
                "BEGIN SELECT 1; END",
            )

        conn.execute("CREATE TABLE occupied_name(id INTEGER)")
        with pytest.raises(RuntimeError, match="schema object name collision"):
            _ensure_trigger(
                conn,
                "occupied_name",
                "CREATE TRIGGER occupied_name AFTER INSERT ON source "
                "BEGIN SELECT 1; END",
            )


def test_store_rolls_back_earlier_trigger_repair_on_later_collision(tmp_path):
    db_path = (tmp_path / "trigger-rollback.db").resolve()
    store = Store(str(db_path))
    store.close()

    early_name = "trg_match_rating_policy_identity_insert"
    late_name = "trg_ratings_projection_mutation_insert"
    stale_sql = (
        f"CREATE TRIGGER {early_name} AFTER INSERT ON match_rating_policies "
        "BEGIN SELECT 1; END"
    )
    with sqlite3.connect(db_path) as conn:
        conn.execute(f"DROP TRIGGER {early_name}")
        conn.execute(stale_sql)
        conn.execute(f"DROP TRIGGER {late_name}")
        conn.execute(f"CREATE TABLE {late_name}(id INTEGER)")
    version_before, triggers_before = _schema_state(db_path)
    assert triggers_before[early_name] == _normalize_sql(stale_sql)

    with pytest.raises(RuntimeError, match="schema object name collision"):
        Store(str(db_path))

    version_after, triggers_after = _schema_state(db_path)
    assert version_after == version_before
    assert triggers_after[early_name] == triggers_before[early_name]
