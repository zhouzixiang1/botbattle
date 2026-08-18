"""Store schema migration idempotency and trigger-definition guards."""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from bzplat.backend.store import Store
from bzplat.backend.store.db import _ensure_trigger
from bzplat.backend.store.schema import VALID_GAME_IDS


_PROJECTION_BUMP = (
    "UPDATE rating_projection_state SET "
    "mutation_revision=mutation_revision+1 WHERE singleton=1"
)


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

    reopened = Store(str(db_path))
    reopened.close()
    version_after, triggers_after = _schema_state(db_path)

    assert version_after == version_before
    assert triggers_after == triggers_before
    _assert_rating_trigger_contract(triggers_after)


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
