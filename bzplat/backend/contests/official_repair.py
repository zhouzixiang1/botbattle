"""Explicit offline repair for one narrow incomplete official-result shape.

This module is deliberately outside the runtime contest state machine.  It
never replays Match rows into a ranking, never migrates a database and never
reseals lifecycle metadata.  The only supported write is the missing final
``contest_official_results`` row of an otherwise exact historical
``pencil_swiss_ko`` result.
"""
from __future__ import annotations

import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
import threading
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from bzplat.backend.contests.manager import advancing_entry_ids
from bzplat.backend.contests.presentation import build_stage_summaries
from bzplat.backend.contests.ranking import (
    build_official_result_rows,
    final_stage_replaces_previous_ranking,
    merge_replace_top,
)
from bzplat.backend.contests.stages import effective_swiss_rounds
from bzplat.backend.contests.templates import get_template
from bzplat.backend.contests.validation import active_contest_entries
from bzplat.backend.store.db import (
    _contest_pairing_explicit_series_marker_sql,
    _normalize_official_result_input,
    _official_result_validation_context_tx,
    _validate_complete_official_results,
)
from bzplat.backend.store.schema import STATUS_COMPLETED, TYPE_CONTEST, VALID_GAME_IDS
from bzplat.backend.store.validation import (
    exact_sqlite_bool,
    is_authoritative_no_opponent_pairing,
    validate_canonical_naive_timestamp,
)


POLICY_VERSION = "pencil-swiss-ko-official-tail-v1"
_SQLITE_MAX_ROWID = 2**63 - 1
_MAX_REPAIR_PREIMAGE_SEQUENCE = _SQLITE_MAX_ROWID - 2
_REPAIR_GAME_ID = "pencil"
_REPAIR_TEMPLATE_ID = "pencil_swiss_ko"
_REPAIR_RULESET_VERSION = "pencil_ccgc_v1"
_REPAIR_PROTOCOL_VERSION = "pencil_xy_v1"
_REPAIR_RATING_POOL_ID = "pencil_rating_v1"
_MAX_STAGES_JSON_CHARS = 4096
_MAX_STAGE_PAYLOAD_JSON_CHARS = 2048
_MAX_MATCH_CONFIG_JSON_CHARS = 2048
_MAX_MATCH_RESULT_JSON_CHARS = 2048
_MAX_OFFICIAL_TIEBREAKS_JSON_CHARS = 2048
_STABLE_HEADER_PRAGMAS = (
    "user_version",
    "application_id",
    "schema_version",
    "page_size",
    "encoding",
    "auto_vacuum",
    "default_cache_size",
)
_REPAIR_MATCH_CONFIG_KEYS = frozenset(
    {
        "_bot_a_environment",
        "_bot_a_local_agent_id",
        "_bot_a_version_id",
        "_bot_b_environment",
        "_bot_b_local_agent_id",
        "_bot_b_version_id",
        "_execution_profile_version",
        "_execution_request_id",
        "_rating_eligible",
        "_rating_reason",
        "duplicate",
    }
)
_REPAIR_EXECUTION_REQUEST_ID_RE = re.compile(r"req_[A-Za-z0-9_-]{24}")
_REPAIR_TIEBREAK_KEYS = (
    "points",
    "buchholz",
    "buchholz_cut1",
    "sonneborn_berger",
    "head_to_head",
    "normalized_delta",
    "technical_losses",
    "seed",
)
_REPAIR_STAGE_RESULT_ALLOWLIST = {
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
_REPAIR_PAIRING_OUTCOMES = {
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
_REPAIR_BYE_COORDINATES = {
    (0, 1, 4, None),
    (0, 2, 7, None),
    (0, 3, 6, None),
    (0, 4, 5, None),
}
_REPAIR_KO_BRACKET_SLOTS = {
    (1, 1, 2, 5): 0,
    (1, 1, 9, 8): 1,
    (1, 1, 1, 7): 2,
    (1, 1, 3, 6): 3,
    (1, 2, 2, 9): 0,
    (1, 2, 1, 3): 1,
    (1, 3, 2, 1): 0,
}
_REPAIR_SCHEDULED_COORDINATES = {
    coordinate
    for coordinate in _REPAIR_PAIRING_OUTCOMES
    if coordinate[1] == 1
}
_OFFICIAL_COLUMNS = (
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
_OFFICIAL_RAW_COLUMNS = ("id", *_OFFICIAL_COLUMNS)
_TERMINAL_EXECUTION_STATUSES = {"completed", "cancelled", "interrupted"}
_REQUIRED_LIFECYCLE_TRIGGER_DIGESTS = {
    "trg_contest_entries_lifecycle_revision_delete": (
        "a68f921bf71bbeea9a2d6ee6ef6f866e540b5f923355cbb9bc63705e7a83f580"
    ),
    "trg_contest_entries_lifecycle_revision_insert": (
        "63b8c5fcfaf879b008e42f8a044fc93bd79b67719bf6ee8b0cb3612c9c3ea278"
    ),
    "trg_contest_entries_lifecycle_revision_update": (
        "6e62b9ad5b2787ed3cb8d6128e7e5292cccde6ee63cb4d5d9966c6c622feebee"
    ),
    "trg_contest_lifecycle_revision_update": (
        "683c47aa0bbad5bef6d3366f30d32b69e559ad6aeb9b548d21c71055b8028891"
    ),
    "trg_contest_pairing_topology_delete": (
        "2e594cda965edd6c0fe77ebb7a299e3581d8dc9a8596445ac127ea92e07ae149"
    ),
    "trg_contest_pairing_topology_insert": (
        "cbd573abfb7d4d26dfb4fc0a3240a2678e6c9c5edc720b5f6e64bbdcb825a8d7"
    ),
    "trg_contest_pairing_topology_manifest": (
        "98da1b7e1fa8f86f314ac0dbf5affa36d7aff964842b37cfc597610e36a9eacd"
    ),
    "trg_contest_pairing_topology_stage_cursor": (
        "759a9340b96da8e3b80ef72eaf78e6373287b46f81502382a80474f420f72dab"
    ),
    "trg_contest_pairing_topology_update": (
        "f7dac97116f78ff072c24ce2248ff29a2ed17162325804688b7b5313b20d8105"
    ),
    "trg_contest_stage_results_lifecycle_revision_delete": (
        "4762c3acd963f9161a379891f38beb7066e4cb4ae4055ebb440cb94a499b397d"
    ),
    "trg_contest_stage_results_lifecycle_revision_insert": (
        "150a2b8b7da601325daa3163a6443d64c636f3afa6186eca9a3f9314bcd42442"
    ),
    "trg_contest_stage_results_lifecycle_revision_update": (
        "5df184f19011f648492f514781f93b8a9d60d46c0f7fd54ba83f29de026b4994"
    ),
}


class OfficialRepairError(ValueError):
    """A stable, non-PII rejection from the offline repair planner."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class OfficialRepairPlan:
    contest_id: int
    eligibility: str
    authority_digest: str
    old_official_digest: str
    repaired_official_digest: str
    plan_digest: str
    source_business_digest: str
    expected_post_business_digest: str
    existing_official_count: int
    repaired_official_count: int
    missing_rank: int | None
    missing_entry_is_eliminated: bool
    _candidate_rows: tuple[dict[str, Any], ...] = field(repr=False)
    _missing_row: dict[str, Any] | None = field(repr=False)

    @property
    def eligible(self) -> bool:
        return self.eligibility == "repairable"

    @property
    def already_applied(self) -> bool:
        return self.eligibility == "already_applied"

    @property
    def candidate_rows(self) -> list[dict[str, Any]]:
        return copy.deepcopy(list(self._candidate_rows))

    def public_report(self) -> dict[str, Any]:
        """Return the deliberately identity-free operator review envelope."""
        return {
            "authority_digest": self.authority_digest,
            "contest_id": self.contest_id,
            "eligibility": self.eligibility,
            "existing_official_count": self.existing_official_count,
            "missing_rank": self.missing_rank,
            "old_official_digest": self.old_official_digest,
            "plan_digest": self.plan_digest,
            "policy_version": POLICY_VERSION,
            "repaired_official_count": self.repaired_official_count,
            "repaired_official_digest": self.repaired_official_digest,
            "source_business_digest": self.source_business_digest,
            "expected_post_business_digest": self.expected_post_business_digest,
        }


@dataclass
class OfficialRepairPathGuard:
    database_path: str
    lock_path: str
    thread_id: int
    lock_fd: int
    lock_dev: int
    lock_ino: int
    target_dev: int
    target_ino: int
    active: bool = True
    finalized: bool = False


def _dict_rows(cursor: sqlite3.Cursor) -> list[dict[str, Any]]:
    columns = [str(column[0]) for column in cursor.description or ()]
    return [dict(zip(columns, tuple(row))) for row in cursor.fetchall()]


def _one_dict(cursor: sqlite3.Cursor) -> dict[str, Any] | None:
    rows = _dict_rows(cursor)
    return rows[0] if rows else None


def _typed(value: Any) -> Any:
    """Encode SQLite values without bool/int/float/NULL aliasing."""
    if value is None:
        return ["null"]
    if isinstance(value, bool):
        return ["bool", 1 if value else 0]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        if not math.isfinite(value):
            return ["float-nonfinite", repr(value)]
        return ["float", value.hex()]
    if isinstance(value, str):
        return ["text", value]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, list):
        return ["list", [_typed(item) for item in value]]
    if isinstance(value, tuple):
        return ["tuple", [_typed(item) for item in value]]
    if isinstance(value, dict):
        return [
            "object",
            [[str(key), _typed(value[key])] for key in sorted(value)],
        ]
    raise TypeError(f"unsupported digest value: {type(value).__name__}")


def _digest(value: Any) -> str:
    encoded = json.dumps(
        _typed(value), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _schema_sql_fingerprint(sql: str) -> str:
    """Hash SQL tokens while ignoring only formatting and comments.

    SQLite preserves comments in ``sqlite_schema.sql``.  Removing them with a
    quote-aware scanner lets the two reviewed physical table layouts share a
    finite allowlist without allowing a comment to impersonate a constraint.
    """
    if not isinstance(sql, str):
        return ""
    text = sql.strip()
    if text.endswith(";"):
        text = text[:-1]
    normalized: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if text.startswith("--", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline < 0 else newline + 1
            continue
        if text.startswith("/*", index):
            closing = text.find("*/", index + 2)
            if closing < 0:
                return ""
            index = closing + 2
            continue
        if char in {"'", '"', "`"}:
            quote = char
            normalized.append(char)
            index += 1
            while index < len(text):
                current = text[index]
                normalized.append(current)
                index += 1
                if current != quote:
                    continue
                if index < len(text) and text[index] == quote:
                    normalized.append(text[index])
                    index += 1
                    continue
                break
            else:
                return ""
            continue
        if char == "[":
            closing = text.find("]", index + 1)
            if closing < 0:
                return ""
            normalized.append(text[index : closing + 1])
            index = closing + 1
            continue
        normalized.append(char.upper())
        index += 1
    return hashlib.sha256("".join(normalized).encode("utf-8")).hexdigest()


_OFFICIAL_TABLE_DDL_PREFIX = (
    "CREATE TABLE contest_official_results("
    "id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,"
    "entry_id INTEGER NOT NULL,stage_idx INTEGER NOT NULL DEFAULT 0,"
    "rank INTEGER NOT NULL,points REAL NOT NULL DEFAULT 0,"
    "bot_id INTEGER REFERENCES bots(id) ON DELETE SET NULL,user_id INTEGER,"
)
_OFFICIAL_TABLE_DDL_SUFFIX = "UNIQUE(contest_id,entry_id))"
_ACCEPTED_OFFICIAL_TABLE_FINGERPRINTS = frozenset(
    _schema_sql_fingerprint(sql)
    for sql in (
        _OFFICIAL_TABLE_DDL_PREFIX
        + "group_id TEXT NOT NULL DEFAULT '',rank_in_group INTEGER,"
        "tiebreaks_json TEXT NOT NULL DEFAULT '{}',"
        "awarded TEXT NOT NULL DEFAULT '',"
        + _OFFICIAL_TABLE_DDL_SUFFIX,
        _OFFICIAL_TABLE_DDL_PREFIX
        + "tiebreaks_json TEXT NOT NULL DEFAULT '{}',"
        "awarded TEXT NOT NULL DEFAULT '',group_id TEXT NOT NULL DEFAULT '',"
        "rank_in_group INTEGER,"
        + _OFFICIAL_TABLE_DDL_SUFFIX,
    )
)


def _stable_header_contract(
    connection: sqlite3.Connection,
) -> tuple[tuple[str, object], ...]:
    values: list[tuple[str, object]] = []
    for name in _STABLE_HEADER_PRAGMAS:
        row = connection.execute(f"PRAGMA {name}").fetchone()
        if row is None or len(row) != 1:
            _reject("header_invalid", "SQLite header pragma 无法读取")
        value = row[0]
        if (name == "encoding" and type(value) is not str) or (
            name != "encoding" and type(value) is not int
        ):
            _reject("header_invalid", "SQLite header pragma 类型损坏")
        values.append((name, value))
    return tuple(values)


def _database_business_digest(
    connection: sqlite3.Connection,
    *,
    official_override: tuple[dict[str, Any], ...] | None = None,
    official_sequence_override: int | None = None,
    contest_id: int | None = None,
) -> str:
    """Hash the complete logical schema and every application row.

    The optional override computes the exact logical postimage without writing
    a planning copy.  It is intentionally limited to one contest's official
    rows and the matching AUTOINCREMENT sequence.
    """
    if (official_override is None) != (official_sequence_override is None):
        raise TypeError("official postimage override is incomplete")
    if official_override is not None and (
        isinstance(contest_id, bool)
        or not isinstance(contest_id, int)
        or contest_id < 1
    ):
        raise TypeError("official postimage contest id is invalid")

    digest = hashlib.sha256()

    def add(value: Any) -> None:
        payload = json.dumps(
            _typed(value), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    add({"sqlite_header_pragmas": _stable_header_contract(connection)})

    schema_rows = connection.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_autoindex_%' ORDER BY type,name"
    ).fetchall()
    add({"schema": [list(row) for row in schema_rows]})
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND (name NOT LIKE 'sqlite_%' OR name='sqlite_sequence') "
            "ORDER BY name"
        )
    ]
    for table in tables:
        columns = _dict_rows(
            connection.execute("SELECT * FROM pragma_table_xinfo(?)", (table,))
        )
        names = [str(column["name"]) for column in columns]
        primary_key = [
            str(column["name"])
            for column in sorted(columns, key=lambda column: int(column["pk"] or 0))
            if int(column["pk"] or 0) > 0
        ]
        order_by = primary_key or names
        select_columns = ",".join(_quoted_identifier(name) for name in names)
        order_columns = ",".join(_quoted_identifier(name) for name in order_by)
        rows = [
            list(row)
            for row in connection.execute(
                f"SELECT {select_columns} FROM {_quoted_identifier(table)}"
                + (f" ORDER BY {order_columns}" if order_columns else "")
            )
        ]
        if official_override is not None and table == "contest_official_results":
            contest_index = names.index("contest_id")
            rows = [row for row in rows if row[contest_index] != contest_id]
            rows.extend(
                [[row.get(name) for name in names] for row in official_override]
            )
            id_index = names.index("id")
            rows.sort(key=lambda row: row[id_index])
        elif official_override is not None and table == "sqlite_sequence":
            name_index = names.index("name")
            seq_index = names.index("seq")
            replaced = False
            for row in rows:
                if row[name_index] == "contest_official_results":
                    row[seq_index] = official_sequence_override
                    replaced = True
            if not replaced:
                rows.append(
                    [
                        "contest_official_results"
                        if name == "name"
                        else official_sequence_override
                        for name in names
                    ]
                )
            rows.sort(key=lambda row: tuple(_typed(value) for value in row))
        add({"table": table, "column_schema": columns})
        for row in rows:
            add(row)
    return digest.hexdigest()


def _reject(code: str, message: str) -> None:
    raise OfficialRepairError(code, message)


def _read_pairings(
    connection: sqlite3.Connection, contest_id: int, game_id: str
) -> list[dict[str, Any]]:
    if game_id not in VALID_GAME_IDS:
        _reject("game_not_supported", "赛事游戏不属于修复工具支持范围")
    match_table = f"matches_{game_id}"
    explicit_series = _contest_pairing_explicit_series_marker_sql()
    return _dict_rows(
        connection.execute(
            "SELECT p.*,p.entry_a_id AS _raw_entry_a_id,"
            "p.entry_b_id AS _raw_entry_b_id,"
            f"{explicit_series} AS _explicit_series_marker,"
            "m.winner AS match_winner,m.status AS match_status,"
            "m.created_at AS _match_created_at,"
            "m.started_at AS _match_started_at,m.ended_at AS _match_ended_at,"
            "m.likes_count AS _match_likes_count,"
            "m.views_count AS _match_views_count,m.id AS _match_id,"
            "m.owner_id AS _match_owner_id,"
            "m.contest_id AS _match_contest_id,"
            "m.game_id AS _match_game_id,m.match_type AS _match_type,"
            "m.ruleset_version AS _match_ruleset_version,"
            "m.protocol_version AS _match_protocol_version,"
            "m.rating_pool_id AS _match_rating_pool_id,"
            "m.bot_a_id AS _match_bot_a_id,m.bot_b_id AS _match_bot_b_id,"
            "m.result AS _match_result_json,"
            "m.match_config AS _match_config_json,"
            "m.reason AS _match_reason,"
            "m.technical_loss AS _match_technical_loss,"
            "m.match_seed AS _match_seed,"
            "m.human_user_id AS _match_human_user_id,"
            "m.human_seat AS _match_human_seat,"
            "ea.user_id AS _entry_a_user_id,"
            "eb.user_id AS _entry_b_user_id,"
            "ba.owner_id AS _pairing_bot_a_owner_id,"
            "bb.owner_id AS _pairing_bot_b_owner_id "
            "FROM contest_pairings p "
            "JOIN contests pairing_contest ON pairing_contest.id=p.contest_id "
            "LEFT JOIN contest_entries ea ON ea.id=p.entry_a_id "
            "AND ea.contest_id=p.contest_id "
            "LEFT JOIN contest_entries eb ON eb.id=p.entry_b_id "
            "AND eb.contest_id=p.contest_id "
            "LEFT JOIN bots ba ON ba.id=p.bot_a_id "
            "LEFT JOIN bots bb ON bb.id=p.bot_b_id "
            f"LEFT JOIN {match_table} m ON m.id=p.match_id "
            "WHERE p.contest_id=? "
            "ORDER BY p.stage_idx,p.round_num,p.id",
            (contest_id,),
        )
    )


def _read_stage_results(
    connection: sqlite3.Connection, contest_id: int
) -> list[dict[str, Any]]:
    return _dict_rows(
        connection.execute(
            "SELECT * FROM contest_stage_results WHERE contest_id=? "
            "ORDER BY stage_idx,id",
            (contest_id,),
        )
    )


def _read_match_authority(
    connection: sqlite3.Connection,
    contest_id: int,
    pairing_match_ids: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tables = [
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name GLOB 'matches_*' AND name<>'matches_index' ORDER BY name"
        )
    ]
    rows: list[dict[str, Any]] = []
    for table in tables:
        if not table.replace("_", "").isalnum():  # pragma: no cover - schema gate
            _reject("schema_invalid", "Match 表名损坏")
        selected = _dict_rows(
            connection.execute(
                f"SELECT * FROM {_quoted_identifier(table)} "
                "WHERE contest_id=? ORDER BY id",
                (contest_id,),
            )
        )
        if pairing_match_ids:
            marks = ",".join("?" for _ in pairing_match_ids)
            selected_by_id = _dict_rows(
                connection.execute(
                    f"SELECT * FROM {_quoted_identifier(table)} "
                    f"WHERE id IN ({marks}) ORDER BY id",
                    tuple(sorted(pairing_match_ids)),
                )
            )
            by_id = {str(row["id"]): row for row in selected}
            by_id.update({str(row["id"]): row for row in selected_by_id})
            selected = [by_id[key] for key in sorted(by_id)]
        rows.extend({"table": table, "row": row} for row in selected)
    ids_by_table: dict[str, str] = {}
    for item in rows:
        match_id = item["row"].get("id")
        if not isinstance(match_id, str) or not match_id:
            _reject("match_identity_invalid", "赛事 Match 身份损坏")
        if match_id in ids_by_table:
            _reject("match_identity_invalid", "赛事 Match 跨表身份冲突")
        ids_by_table[match_id] = str(item["table"])
    if set(ids_by_table) != pairing_match_ids:
        _reject("match_binding_invalid", "赛事对阵与 Match 集合不一致")
    if pairing_match_ids:
        marks = ",".join("?" for _ in pairing_match_ids)
        durable_references = _dict_rows(
            connection.execute(
                "SELECT id,contest_id,match_id FROM contest_pairings "
                f"WHERE match_id IN ({marks}) ORDER BY match_id,id",
                tuple(sorted(pairing_match_ids)),
            )
        )
        references_by_match: dict[str, list[dict[str, Any]]] = {}
        for reference in durable_references:
            references_by_match.setdefault(str(reference.get("match_id")), []).append(
                reference
            )
        if set(references_by_match) != pairing_match_ids or any(
            len(references_by_match[match_id]) != 1
            or references_by_match[match_id][0].get("contest_id") != contest_id
            for match_id in pairing_match_ids
        ):
            _reject("match_affiliation_invalid", "赛事 Match 存在冲突对阵归属")
    if any(
        item["row"].get("contest_id") != contest_id
        or item["row"].get("game_id") != _REPAIR_GAME_ID
        or item["row"].get("match_type") != "contest"
        for item in rows
    ):
        _reject("match_affiliation_invalid", "赛事 Match 归属损坏")
    if pairing_match_ids:
        marks = ",".join("?" for _ in pairing_match_ids)
        index_rows = _dict_rows(
            connection.execute(
                f"SELECT id,game_id FROM matches_index WHERE id IN ({marks}) "
                "ORDER BY id",
                tuple(sorted(pairing_match_ids)),
            )
        )
    else:
        index_rows = []
    indexed = {
        str(row.get("id")): str(row.get("game_id")) for row in index_rows
    }
    if set(indexed) != pairing_match_ids or any(
        indexed[match_id] != ids_by_table[match_id].removeprefix("matches_")
        for match_id in pairing_match_ids
    ):
        _reject("match_index_invalid", "赛事 Match 索引与物理表不一致")
    return rows, index_rows


class _SnapshotOnlyManager:
    def standings(self, *_args: Any, **_kwargs: Any) -> list[dict[str, Any]]:
        raise AssertionError("offline official repair must not replay Match standings")


def _expected_frozen_stages(roster_count: int) -> list[dict[str, Any]]:
    template = get_template(_REPAIR_TEMPLATE_ID)
    if not isinstance(template, dict) or not isinstance(template.get("stages"), list):
        raise RuntimeError("pencil_swiss_ko template is unavailable")
    expected = copy.deepcopy(template["stages"])
    expected[0]["effective_rounds"] = effective_swiss_rounds(
        expected[0], roster_count
    )
    return expected


def _supported_frozen_stages(roster_count: int) -> tuple[list[dict[str, Any]], ...]:
    current = _expected_frozen_stages(roster_count)
    legacy_derived_rounds = copy.deepcopy(current)
    legacy_derived_rounds[0].pop("effective_rounds", None)
    return current, legacy_derived_rounds


def _is_exact_positive_int(value: object) -> bool:
    return type(value) is int and value > 0


def _decode_strict_json(
    raw: object,
    *,
    max_chars: int,
    label: str,
) -> Any:
    """Decode one reviewed cold-DB JSON value without lossy aliases."""
    if type(raw) is not str or len(raw) > max_chars:
        _reject("raw_authority_invalid", f"{label}不是有界文本")

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate JSON key")
            result[key] = value
        return result

    def finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed) or value != str(parsed):
            raise ValueError("non-canonical JSON float")
        return parsed

    def canonical_int(value: str) -> int:
        parsed = int(value)
        if value != str(parsed):
            raise ValueError("non-canonical JSON integer")
        return parsed

    try:
        return json.loads(
            raw,
            object_pairs_hook=unique_object,
            parse_int=canonical_int,
            parse_float=finite_float,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {value}")
            ),
        )
    except (RecursionError, TypeError, ValueError) as exc:
        raise OfficialRepairError(
            "raw_authority_invalid", f"{label} JSON 损坏"
        ) from exc


def _decode_strict_json_object(
    raw: object,
    *,
    max_chars: int,
    label: str,
) -> dict[str, Any]:
    decoded = _decode_strict_json(raw, max_chars=max_chars, label=label)
    if type(decoded) is not dict:
        _reject("raw_authority_invalid", f"{label}不是对象")
    return decoded


def _decode_repair_stage_payload(raw: object) -> dict[str, Any]:
    payload = _decode_strict_json_object(
        raw,
        max_chars=_MAX_STAGE_PAYLOAD_JSON_CHARS,
        label="赛事阶段结果 payload",
    )
    if set(payload) != {"tiebreaks"}:
        _reject("raw_authority_invalid", "赛事阶段结果 payload 字段损坏")
    tiebreaks = payload.get("tiebreaks")
    if (
        type(tiebreaks) is not dict
        or set(tiebreaks) != set(_REPAIR_TIEBREAK_KEYS)
        or any(type(tiebreaks.get(key)) is not float for key in _REPAIR_TIEBREAK_KEYS[:6])
        or any(type(tiebreaks.get(key)) is not int for key in _REPAIR_TIEBREAK_KEYS[6:])
    ):
        _reject("raw_authority_invalid", "赛事阶段结果破同分字段或类型损坏")
    return payload


def _validate_existing_official_json(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        _decode_strict_json_object(
            row.get("tiebreaks_json"),
            max_chars=_MAX_OFFICIAL_TIEBREAKS_JSON_CHARS,
            label="既有正式排名破同分",
        )


def _validate_raw_repair_authority_tx(
    connection: sqlite3.Connection,
    contest: dict[str, Any],
    stages: list[dict[str, Any]],
    entries: list[dict[str, Any]],
    pairings: list[dict[str, Any]],
    stage_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Bind the one reviewed legacy artifact before any presentation helper.

    This intentionally does not define a reusable contest compatibility layer.
    It accepts only the anonymous, type-exact production shape reviewed for the
    single Pencil 9-to-8 repair.  In particular, no ``int()``, ``str()`` or
    truthiness fallback may turn weakly typed SQLite data into that authority.
    """
    contest_id = contest.get("id")
    organizer_id = contest.get("organizer_id")
    if (
        not _is_exact_positive_int(contest_id)
        or not _is_exact_positive_int(organizer_id)
        or type(contest.get("game_id")) is not str
        or contest["game_id"] != _REPAIR_GAME_ID
        or type(contest.get("ruleset_version")) is not str
        or contest["ruleset_version"] != _REPAIR_RULESET_VERSION
        or type(contest.get("protocol_version")) is not str
        or contest["protocol_version"] != _REPAIR_PROTOCOL_VERSION
        or type(contest.get("rating_pool_id")) is not str
        or contest["rating_pool_id"] != _REPAIR_RATING_POOL_ID
        or contest.get("time_control_id") is not None
    ):
        _reject("raw_authority_invalid", "赛事冻结契约不是审核目标")

    expected_stage_keys: dict[int, str] = {}
    for stage_idx, stage in enumerate(stages):
        stage_key = stage.get("key")
        if type(stage_key) is not str or not stage_key:
            _reject("raw_authority_invalid", "赛事冻结阶段标识类型损坏")
        expected_stage_keys[stage_idx] = stage_key

    entry_by_id: dict[int, dict[str, Any]] = {}
    entry_user_ids: set[int] = set()
    current_bot_ids: set[int] = set()
    seeds: set[int] = set()
    for entry in entries:
        entry_id = entry.get("id")
        user_id = entry.get("user_id")
        bot_id = entry.get("bot_id")
        seed = entry.get("seed")
        eliminated = entry.get("eliminated")
        if (
            not _is_exact_positive_int(entry_id)
            or type(entry.get("contest_id")) is not int
            or entry["contest_id"] != contest_id
            or not _is_exact_positive_int(user_id)
            or not _is_exact_positive_int(bot_id)
            or not _is_exact_positive_int(seed)
            or type(eliminated) is not int
            or eliminated not in (0, 1)
            or type(entry.get("group_id")) is not str
            or entry["group_id"] != ""
            or entry_id in entry_by_id
            or user_id in entry_user_ids
            or bot_id in current_bot_ids
            or seed in seeds
        ):
            _reject("raw_authority_invalid", "赛事名册身份或坐标不是审核目标")
        entry_by_id[entry_id] = entry
        entry_user_ids.add(user_id)
        current_bot_ids.add(bot_id)
        seeds.add(seed)
    if seeds != set(range(1, 10)):
        _reject("raw_authority_invalid", "赛事名册 seed 不是精确一至九排列")

    stage_bot_bindings: list[tuple[int, int]] = []
    seen_stage_rows: set[tuple[int, int]] = set()
    observed_stage_results: dict[tuple[int, int], tuple[Any, ...]] = {}
    public_stage_results: list[dict[str, Any]] = []
    for row in stage_results:
        row_id = row.get("id")
        stage_idx = row.get("stage_idx")
        stage_key = row.get("stage_key")
        entry_id = row.get("entry_id")
        bot_id = row.get("bot_id")
        if (
            not _is_exact_positive_int(row_id)
            or type(row.get("contest_id")) is not int
            or row["contest_id"] != contest_id
            or type(stage_idx) is not int
            or stage_idx not in expected_stage_keys
            or type(stage_key) is not str
            or stage_key != expected_stage_keys[stage_idx]
            or type(row.get("group_id")) is not str
            or row["group_id"] != ""
            or not _is_exact_positive_int(entry_id)
            or entry_id not in entry_by_id
            or not _is_exact_positive_int(bot_id)
            or (stage_idx, entry_id) in seen_stage_rows
        ):
            _reject("raw_authority_invalid", "赛事阶段结果身份不是审核目标")
        payload = _decode_repair_stage_payload(row.get("payload_json"))
        tiebreaks = payload["tiebreaks"]
        rank = row.get("rank_in_group")
        points = row.get("points")
        wins = row.get("wins")
        draws = row.get("draws")
        losses = row.get("losses")
        delta_total = row.get("delta_total")
        seed = entry_by_id[entry_id]["seed"]
        if (
            not _is_exact_positive_int(rank)
            or type(points) is not float
            or type(wins) is not int
            or type(draws) is not int
            or type(losses) is not int
            or type(delta_total) is not int
        ):
            _reject("raw_authority_invalid", "赛事阶段结果统计类型损坏")
        observed_stage_results[(stage_idx, seed)] = (
            rank,
            points,
            wins,
            draws,
            losses,
            delta_total,
            tuple(tiebreaks[key] for key in _REPAIR_TIEBREAK_KEYS),
        )
        public_row = dict(row)
        public_row.pop("payload_json", None)
        public_row["tiebreaks"] = copy.deepcopy(tiebreaks)
        public_stage_results.append(public_row)
        seen_stage_rows.add((stage_idx, entry_id))
        stage_bot_bindings.append((entry_id, bot_id))
    expected_stage_rows = {
        (0, entry_id) for entry_id in entry_by_id
    } | {
        (1, entry_id)
        for entry_id, entry in entry_by_id.items()
        if entry["eliminated"] == 0
    }
    if seen_stage_rows != expected_stage_rows:
        _reject("raw_authority_invalid", "赛事阶段结果名册不是精确九进八形状")
    if _typed(observed_stage_results) != _typed(_REPAIR_STAGE_RESULT_ALLOWLIST):
        _reject("raw_authority_invalid", "赛事阶段结果不等于审核匿名快照")

    all_bot_ids = current_bot_ids | {bot_id for _, bot_id in stage_bot_bindings}
    marks = ",".join("?" for _ in all_bot_ids)
    bot_rows = _dict_rows(
        connection.execute(
            "SELECT id,owner_id,game_id,protocol_version FROM bots "
            f"WHERE id IN ({marks}) ORDER BY id",
            tuple(sorted(all_bot_ids)),
        )
    )
    bot_by_id: dict[int, dict[str, Any]] = {}
    for bot in bot_rows:
        bot_id = bot.get("id")
        if (
            not _is_exact_positive_int(bot_id)
            or not _is_exact_positive_int(bot.get("owner_id"))
            or type(bot.get("game_id")) is not str
            or bot["game_id"] != _REPAIR_GAME_ID
            or type(bot.get("protocol_version")) is not str
            or bot["protocol_version"] != _REPAIR_PROTOCOL_VERSION
            or bot_id in bot_by_id
        ):
            _reject("raw_authority_invalid", "赛事 Bot 契约不是审核目标")
        bot_by_id[bot_id] = bot
    if set(bot_by_id) != all_bot_ids:
        _reject("raw_authority_invalid", "赛事 Bot 身份缺失")
    if any(
        bot_by_id[entry["bot_id"]]["owner_id"] != entry["user_id"]
        for entry in entries
    ) or any(
        bot_by_id[bot_id]["owner_id"] != entry_by_id[entry_id]["user_id"]
        for entry_id, bot_id in stage_bot_bindings
    ):
        _reject("raw_authority_invalid", "赛事 Bot 与报名用户不一致")

    seen_pairing_ids: set[int] = set()
    seen_match_ids: set[str] = set()
    observed_real_coordinates: set[tuple[int, int, int, int]] = set()
    observed_bye_coordinates: set[tuple[int, int, int, None]] = set()
    expected_match_outcomes: dict[str, tuple[int, int, float, int, str]] = {}
    version_bindings: list[tuple[int, int]] = []
    real_pairings: list[dict[str, Any]] = []
    swiss_round_counts: Counter[int] = Counter()
    swiss_bye_counts: Counter[int] = Counter()
    pairing_counts: Counter[int] = Counter()
    for pairing in pairings:
        pairing_id = pairing.get("id")
        stage_idx = pairing.get("stage_idx")
        stage_key = pairing.get("stage_key")
        round_num = pairing.get("round_num")
        if (
            not _is_exact_positive_int(pairing_id)
            or type(pairing.get("contest_id")) is not int
            or pairing["contest_id"] != contest_id
            or type(stage_idx) is not int
            or stage_idx not in expected_stage_keys
            or type(stage_key) is not str
            or stage_key != expected_stage_keys[stage_idx]
            or type(pairing.get("group_id")) is not str
            or pairing["group_id"] != ""
            or not _is_exact_positive_int(round_num)
            or type(pairing.get("color_first")) is not int
            or pairing["color_first"] != 0
            or type(pairing.get("series_index")) is not int
            or pairing["series_index"] != 1
            or type(pairing.get("series_size")) is not int
            or pairing["series_size"] != 1
            or pairing.get("pairing_seed") is not None
            or type(pairing.get("tiebreak_group")) is not int
            or pairing["tiebreak_group"] != 0
            or type(pairing.get("tiebreak_game")) is not int
            or pairing["tiebreak_game"] != 0
            or pairing_id in seen_pairing_ids
        ):
            _reject("raw_authority_invalid", "赛事对阵坐标不是审核目标")
        try:
            validate_canonical_naive_timestamp(pairing.get("published_at"), "发布时间")
            validate_canonical_naive_timestamp(
                pairing.get("scheduled_at"), "计划时间", allow_none=True
            )
        except ValueError:
            _reject("raw_authority_invalid", "赛事对阵时间不是规范秒级值")
        if stage_idx == 0:
            if pairing.get("bracket_slot") is not None or round_num not in range(1, 5):
                _reject("raw_authority_invalid", "瑞士对阵坐标不是审核目标")
            swiss_round_counts[round_num] += 1
        elif (
            type(pairing.get("bracket_slot")) is not int
            or pairing["bracket_slot"] < 0
        ):
            _reject("raw_authority_invalid", "淘汰赛对阵坐标类型损坏")

        entry_a_id = pairing.get("entry_a_id")
        bot_a_id = pairing.get("bot_a_id")
        version_a_id = pairing.get("bot_a_version_id")
        if (
            not _is_exact_positive_int(entry_a_id)
            or entry_a_id not in entry_by_id
            or pairing.get("_raw_entry_a_id") != entry_a_id
            or not _is_exact_positive_int(bot_a_id)
            or bot_a_id != entry_by_id[entry_a_id]["bot_id"]
            or pairing.get("_entry_a_user_id") != entry_by_id[entry_a_id]["user_id"]
            or pairing.get("_pairing_bot_a_owner_id")
            != entry_by_id[entry_a_id]["user_id"]
        ):
            _reject("raw_authority_invalid", "赛事对阵座位一绑定损坏")

        entry_b_id = pairing.get("entry_b_id")
        if entry_b_id is None:
            if (
                stage_idx != 0
                or pairing.get("_raw_entry_b_id") is not None
                or version_a_id is not None
                or pairing.get("bot_b_id") is not None
                or pairing.get("bot_b_version_id") is not None
                or pairing.get("_entry_b_user_id") is not None
                or pairing.get("_pairing_bot_b_owner_id") is not None
                or pairing.get("match_id") is not None
            ):
                _reject("raw_authority_invalid", "赛事轮空座位绑定损坏")
            bye_coordinate = (
                stage_idx,
                round_num,
                entry_by_id[entry_a_id]["seed"],
                None,
            )
            observed_bye_coordinates.add(bye_coordinate)
            swiss_bye_counts[round_num] += 1
            if pairing.get("scheduled_at") is not None:
                _reject("raw_authority_invalid", "赛事轮空计划时间不是审核目标")
        else:
            bot_b_id = pairing.get("bot_b_id")
            version_b_id = pairing.get("bot_b_version_id")
            match_id = pairing.get("match_id")
            if (
                not _is_exact_positive_int(entry_b_id)
                or entry_b_id == entry_a_id
                or entry_b_id not in entry_by_id
                or pairing.get("_raw_entry_b_id") != entry_b_id
                or not _is_exact_positive_int(bot_b_id)
                or bot_b_id != entry_by_id[entry_b_id]["bot_id"]
                or pairing.get("_entry_b_user_id")
                != entry_by_id[entry_b_id]["user_id"]
                or pairing.get("_pairing_bot_b_owner_id")
                != entry_by_id[entry_b_id]["user_id"]
                or not _is_exact_positive_int(version_a_id)
                or not _is_exact_positive_int(version_b_id)
                or type(match_id) is not str
                or not match_id
                or match_id in seen_match_ids
            ):
                _reject("raw_authority_invalid", "赛事真实对阵座位绑定损坏")
            coordinate = (
                stage_idx,
                round_num,
                entry_by_id[entry_a_id]["seed"],
                entry_by_id[entry_b_id]["seed"],
            )
            expected_outcome = _REPAIR_PAIRING_OUTCOMES.get(coordinate)
            if expected_outcome is None or coordinate in observed_real_coordinates:
                _reject("raw_authority_invalid", "赛事对阵 seed 图不是审核目标")
            if (
                (pairing.get("scheduled_at") is not None)
                != (coordinate in _REPAIR_SCHEDULED_COORDINATES)
                or (
                    stage_idx == 1
                    and pairing.get("bracket_slot")
                    != _REPAIR_KO_BRACKET_SLOTS.get(coordinate)
                )
            ):
                _reject("raw_authority_invalid", "赛事对阵发布坐标不是审核目标")
            observed_real_coordinates.add(coordinate)
            expected_match_outcomes[match_id] = expected_outcome
            version_bindings.append((version_a_id, bot_a_id))
            version_bindings.append((version_b_id, bot_b_id))
            seen_match_ids.add(match_id)
            real_pairings.append(pairing)
        pairing_counts[stage_idx] += 1
        seen_pairing_ids.add(pairing_id)

    if (
        pairing_counts != Counter({0: 20, 1: 7})
        or swiss_round_counts != Counter({1: 5, 2: 5, 3: 5, 4: 5})
        or swiss_bye_counts != Counter({1: 1, 2: 1, 3: 1, 4: 1})
        or len(real_pairings) != 23
        or observed_real_coordinates != set(_REPAIR_PAIRING_OUTCOMES)
        or observed_bye_coordinates != _REPAIR_BYE_COORDINATES
    ):
        _reject("raw_authority_invalid", "赛事对阵批次不是审核目标")

    version_ids = {version_id for version_id, _bot_id in version_bindings}
    marks = ",".join("?" for _ in version_ids)
    version_rows = _dict_rows(
        connection.execute(
            "SELECT id,bot_id,protocol_version FROM bot_versions "
            f"WHERE id IN ({marks}) ORDER BY id",
            tuple(sorted(version_ids)),
        )
    )
    version_by_id: dict[int, dict[str, Any]] = {}
    for version in version_rows:
        version_id = version.get("id")
        if (
            not _is_exact_positive_int(version_id)
            or not _is_exact_positive_int(version.get("bot_id"))
            or type(version.get("protocol_version")) is not str
            or version["protocol_version"] != _REPAIR_PROTOCOL_VERSION
            or version_id in version_by_id
        ):
            _reject("raw_authority_invalid", "赛事 Bot 版本契约损坏")
        version_by_id[version_id] = version
    if set(version_by_id) != version_ids or any(
        version_by_id[version_id]["bot_id"] != bot_id
        for version_id, bot_id in version_bindings
    ):
        _reject("raw_authority_invalid", "赛事 Bot 版本与对阵座位不一致")

    request_ids: set[str] = set()
    for pairing in real_pairings:
        if (
            pairing.get("_match_id") != pairing["match_id"]
            or not _is_exact_positive_int(pairing.get("_match_owner_id"))
            or pairing["_match_owner_id"] != organizer_id
            or not _is_exact_positive_int(pairing.get("_match_contest_id"))
            or pairing["_match_contest_id"] != contest_id
            or type(pairing.get("_match_game_id")) is not str
            or pairing["_match_game_id"] != _REPAIR_GAME_ID
            or type(pairing.get("_match_type")) is not str
            or pairing["_match_type"] != TYPE_CONTEST
            or type(pairing.get("_match_ruleset_version")) is not str
            or pairing["_match_ruleset_version"] != _REPAIR_RULESET_VERSION
            or type(pairing.get("_match_protocol_version")) is not str
            or pairing["_match_protocol_version"] != _REPAIR_PROTOCOL_VERSION
            or type(pairing.get("_match_rating_pool_id")) is not str
            or pairing["_match_rating_pool_id"] != _REPAIR_RATING_POOL_ID
            or not _is_exact_positive_int(pairing.get("_match_bot_a_id"))
            or pairing["_match_bot_a_id"] != pairing["bot_a_id"]
            or not _is_exact_positive_int(pairing.get("_match_bot_b_id"))
            or pairing["_match_bot_b_id"] != pairing["bot_b_id"]
            or pairing.get("_match_seed") is not None
            or pairing.get("_match_human_user_id") is not None
            or pairing.get("_match_human_seat") is not None
        ):
            _reject("raw_authority_invalid", "赛事 Match 冻结身份不是审核目标")
        try:
            match_created_at = validate_canonical_naive_timestamp(
                pairing.get("_match_created_at"), "赛事 Match 创建时间"
            )
            match_started_at = validate_canonical_naive_timestamp(
                pairing.get("_match_started_at"), "赛事 Match 开始时间"
            )
            match_ended_at = validate_canonical_naive_timestamp(
                pairing.get("_match_ended_at"), "赛事 Match 结束时间"
            )
        except ValueError:
            _reject("raw_authority_invalid", "赛事 Match 时间不是规范秒级值")
        scheduled_at = pairing.get("scheduled_at")
        if (
            not (
                pairing["published_at"]
                <= match_created_at
                <= match_started_at
                <= match_ended_at
            )
            or (scheduled_at is not None and scheduled_at > match_created_at)
            or type(pairing.get("_match_likes_count")) is not int
            or pairing["_match_likes_count"] < 0
            or type(pairing.get("_match_views_count")) is not int
            or pairing["_match_views_count"] < 0
        ):
            _reject("raw_authority_invalid", "赛事 Match 时间或计数不是审核目标")
        config = _decode_strict_json_object(
            pairing.get("_match_config_json"),
            max_chars=_MAX_MATCH_CONFIG_JSON_CHARS,
            label="赛事 Match 冻结配置",
        )
        request_id = config.get("_execution_request_id")
        if (
            set(config) != _REPAIR_MATCH_CONFIG_KEYS
            or config.get("_bot_a_environment") != "platform_high"
            or config.get("_bot_b_environment") != "platform_high"
            or config.get("_bot_a_local_agent_id") is not None
            or config.get("_bot_b_local_agent_id") is not None
            or type(config.get("_bot_a_version_id")) is not int
            or config["_bot_a_version_id"] != pairing["bot_a_version_id"]
            or type(config.get("_bot_b_version_id")) is not int
            or config["_bot_b_version_id"] != pairing["bot_b_version_id"]
            or type(config.get("_execution_profile_version")) is not int
            or config["_execution_profile_version"] != 1
            or type(request_id) is not str
            or _REPAIR_EXECUTION_REQUEST_ID_RE.fullmatch(request_id) is None
            or request_id in request_ids
            or type(config.get("_rating_eligible")) is not bool
            or config["_rating_eligible"] is not False
            or type(config.get("_rating_reason")) is not str
            or config["_rating_reason"] != "contest"
            or type(config.get("duplicate")) is not bool
            or config["duplicate"] is not False
        ):
            _reject("raw_authority_invalid", "赛事 Match 冻结配置不是审核目标")
        request_ids.add(request_id)
        result = _decode_strict_json_object(
            pairing.get("_match_result_json"),
            max_chars=_MAX_MATCH_RESULT_JSON_CHARS,
            label="赛事 Match 结果",
        )
        (
            expected_winner,
            expected_delta,
            expected_normalized_delta,
            expected_rounds,
            expected_reason,
        ) = expected_match_outcomes[pairing["match_id"]]
        deltas = result.get("deltas")
        if (
            set(result) != {"rounds_played", "deltas", "normalized_delta"}
            or type(result.get("rounds_played")) is not int
            or result["rounds_played"] != expected_rounds
            or type(deltas) is not list
            or len(deltas) != 2
            or any(type(delta) is not int for delta in deltas)
            or deltas != [expected_delta, -expected_delta]
            or type(result.get("normalized_delta")) is not float
            or _typed(result["normalized_delta"])
            != _typed(expected_normalized_delta)
            or type(pairing.get("match_winner")) is not int
            or pairing["match_winner"] != expected_winner
            or type(pairing.get("_match_reason")) is not str
            or pairing["_match_reason"] != expected_reason
            or type(pairing.get("_match_technical_loss")) is not int
            or pairing["_match_technical_loss"] != 0
        ):
            _reject("raw_authority_invalid", "赛事 Match 结果不是审核目标")
    return public_stage_results


def _official_business_row(row: dict[str, Any]) -> dict[str, Any]:
    return {column: row.get(column) for column in _OFFICIAL_COLUMNS}


def _official_raw_row(row: dict[str, Any]) -> dict[str, Any]:
    return {column: row.get(column) for column in _OFFICIAL_RAW_COLUMNS}


def _validate_repair_schema_tx(connection: sqlite3.Connection) -> None:
    triggers = {
        str(row[0]): row[1]
        for row in connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger'"
        )
    }
    if any(
        not isinstance(triggers.get(name), str)
        or hashlib.sha256(
            " ".join(str(triggers[name]).strip().rstrip(";").split()).encode(
                "utf-8"
            )
        ).hexdigest()
        != expected_digest
        for name, expected_digest in _REQUIRED_LIFECYCLE_TRIGGER_DIGESTS.items()
    ):
        _reject("schema_invalid", "数据库尚未完成赛事 lifecycle epoch 迁移")
    official_columns = _dict_rows(
        connection.execute("PRAGMA table_xinfo(contest_official_results)")
    )
    expected_columns = {
        "id": ("INTEGER", 0, 1, None, 0),
        "contest_id": ("INTEGER", 1, 0, None, 0),
        "entry_id": ("INTEGER", 1, 0, None, 0),
        "stage_idx": ("INTEGER", 1, 0, "0", 0),
        "rank": ("INTEGER", 1, 0, None, 0),
        "points": ("REAL", 1, 0, "0", 0),
        "bot_id": ("INTEGER", 0, 0, None, 0),
        "user_id": ("INTEGER", 0, 0, None, 0),
        "group_id": ("TEXT", 1, 0, "''", 0),
        "rank_in_group": ("INTEGER", 0, 0, None, 0),
        "tiebreaks_json": ("TEXT", 1, 0, "'{}'", 0),
        "awarded": ("TEXT", 1, 0, "''", 0),
    }
    observed_columns = {
        str(row.get("name")): (
            str(row.get("type") or "").upper(),
            row.get("notnull"),
            row.get("pk"),
            row.get("dflt_value"),
            row.get("hidden"),
        )
        for row in official_columns
    }
    if observed_columns != expected_columns:
        _reject("schema_invalid", "正式排名表 schema 与修复策略不一致")
    table_sql_row = connection.execute(
        "SELECT sql FROM sqlite_schema "
        "WHERE type='table' AND name='contest_official_results'"
    ).fetchone()
    table_sql = None if table_sql_row is None else table_sql_row[0]
    if (
        not isinstance(table_sql, str)
        or _schema_sql_fingerprint(table_sql)
        not in _ACCEPTED_OFFICIAL_TABLE_FINGERPRINTS
    ):
        _reject("schema_invalid", "正式排名表物理定义与修复策略不一致")

    unique_indexes: list[tuple[str, int, tuple[str, ...]]] = []
    for index in _dict_rows(
        connection.execute("PRAGMA index_list(contest_official_results)")
    ):
        if index.get("unique") != 1:
            continue
        index_name = index.get("name")
        if not isinstance(index_name, str) or not index_name:
            _reject("schema_invalid", "正式排名表唯一约束身份损坏")
        columns = tuple(
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (index_name,),
            )
        )
        unique_indexes.append(
            (str(index.get("origin") or ""), int(index.get("partial") or 0), columns)
        )
    if unique_indexes != [("u", 0, ("contest_id", "entry_id"))]:
        _reject("schema_invalid", "正式排名表唯一约束损坏")

    foreign_keys = sorted(
        (
            str(row.get("from") or ""),
            str(row.get("table") or ""),
            str(row.get("to") or ""),
            str(row.get("on_update") or "").upper(),
            str(row.get("on_delete") or "").upper(),
            str(row.get("match") or "").upper(),
            row.get("seq"),
        )
        for row in _dict_rows(
            connection.execute("PRAGMA foreign_key_list(contest_official_results)")
        )
    )
    if foreign_keys != sorted([
        ("contest_id", "contests", "id", "NO ACTION", "CASCADE", "NONE", 0),
        ("bot_id", "bots", "id", "NO ACTION", "SET NULL", "NONE", 0),
    ]):
        _reject("schema_invalid", "正式排名表外键约束损坏")


def _official_sequence_tx(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT seq FROM sqlite_sequence WHERE name='contest_official_results'"
    ).fetchall()
    if (
        len(rows) != 1
        or type(rows[0][0]) is not int
        or rows[0][0] < 1
        or rows[0][0] > _SQLITE_MAX_ROWID
    ):
        _reject("official_sequence_invalid", "正式排名自增序列损坏")
    row = rows[0]
    maximum = connection.execute(
        "SELECT MAX(id) FROM contest_official_results"
    ).fetchone()[0]
    if type(maximum) is not int or maximum < 1 or row[0] < maximum:
        _reject("official_sequence_invalid", "正式排名自增序列落后于持久行")
    return int(row[0])


def _build_plan_tx(
    connection: sqlite3.Connection, contest_id: int
) -> OfficialRepairPlan:
    if isinstance(contest_id, bool) or not isinstance(contest_id, int) or contest_id < 1:
        _reject("contest_id_invalid", "赛事 ID 必须是正整数")
    _validate_repair_schema_tx(connection)
    contest = _one_dict(
        connection.execute("SELECT * FROM contests WHERE id=?", (contest_id,))
    )
    if contest is None:
        _reject("contest_missing", "赛事不存在")
    if (
        contest.get("status") != "finished"
        or exact_sqlite_bool(contest.get("official_results_ready")) is not True
        or contest.get("showcase_key") is not None
    ):
        _reject("contest_state_invalid", "赛事不是可修复的正式终态")
    if (
        contest.get("published_stage_pairing_count") is not None
        or type(contest.get("pairing_topology_revision")) is not int
        or contest.get("pairing_topology_revision") != 0
        or contest.get("sealed_pairing_topology_revision") is not None
    ):
        _reject("legacy_epoch_invalid", "赛事不符合唯一受支持的 legacy epoch")
    if (
        contest.get("game_id") != _REPAIR_GAME_ID
        or contest.get("template_id") != _REPAIR_TEMPLATE_ID
        or type(contest.get("current_stage_idx")) is not int
        or contest.get("current_stage_idx") != 1
    ):
        _reject("format_invalid", "赛事不是受支持的 Pencil Swiss 到淘汰赛")

    stages = _decode_strict_json(
        contest.get("stages_json"),
        max_chars=_MAX_STAGES_JSON_CHARS,
        label="赛事冻结阶段",
    )
    if type(stages) is not list:
        _reject("raw_authority_invalid", "赛事冻结阶段不是列表")
    entries = _dict_rows(
        connection.execute(
            "SELECT * FROM contest_entries WHERE contest_id=? ORDER BY id",
            (contest_id,),
        )
    )
    if not any(
        _typed(stages) == _typed(supported)
        for supported in _supported_frozen_stages(len(entries))
    ):
        _reject("stages_invalid", "赛事冻结阶段不等于受支持的精确模板")
    if len(entries) != 9:
        _reject("roster_invalid", "修复仅支持精确九人冻结名册")
    active_entries = active_contest_entries(entries)
    if active_entries is None or len(active_entries) != 8:
        _reject("roster_invalid", "赛事名册不是精确八人晋级形状")

    pairings = _read_pairings(connection, contest_id, _REPAIR_GAME_ID)
    raw_stage_results = _read_stage_results(connection, contest_id)
    if (
        not pairings
        or {row.get("stage_idx") for row in pairings} != {0, 1}
        or {row.get("stage_idx") for row in raw_stage_results} != {0, 1}
        or sum(row.get("stage_idx") == 0 for row in raw_stage_results) != 9
        or sum(row.get("stage_idx") == 1 for row in raw_stage_results) != 8
    ):
        _reject("future_or_partial_artifact", "赛事阶段工件不完整或包含未来阶段")
    for pairing in pairings:
        stage_idx = pairing.get("stage_idx")
        if type(stage_idx) is not int or stage_idx not in (0, 1):
            _reject("settlement_invalid", "赛事对阵阶段坐标损坏")
        if pairing.get("entry_b_id") is None:
            if not is_authoritative_no_opponent_pairing(
                stages[stage_idx].get("type"), pairing
            ):
                _reject("settlement_invalid", "赛事轮空对阵未权威裁决")
            continue
        if (
            not isinstance(pairing.get("match_id"), str)
            or not pairing["match_id"]
            or pairing.get("_match_id") != pairing.get("match_id")
        ):
            _reject("settlement_invalid", "赛事对阵未绑定同赛事完整赛果")
        if (
            pairing.get("_match_contest_id") != contest_id
            or pairing.get("_match_game_id") != _REPAIR_GAME_ID
            or pairing.get("_match_type") != TYPE_CONTEST
        ):
            _reject("match_affiliation_invalid", "赛事 Match 归属损坏")
    pairing_match_ids = {
        str(row["match_id"])
        for row in pairings
        if isinstance(row.get("match_id"), str) and row.get("match_id")
    }
    if len(pairing_match_ids) != sum(
        isinstance(row.get("match_id"), str) and bool(row.get("match_id"))
        for row in pairings
    ):
        _reject("match_binding_invalid", "多个赛事对阵复用了同一 Match")
    match_authority, match_index = _read_match_authority(
        connection, contest_id, pairing_match_ids
    )
    direct_match_ids = {
        str(item["row"]["id"])
        for item in match_authority
        if item["row"].get("contest_id") == contest_id
    }
    if direct_match_ids - pairing_match_ids:
        _reject("orphan_match", "赛事存在未被对阵引用的 Match")

    jobs = _dict_rows(
        connection.execute(
            "SELECT * FROM execution_jobs WHERE contest_id=? "
            "OR contest_pairing_id IN (SELECT id FROM contest_pairings "
            "WHERE contest_id=?) OR current_match_id IN (SELECT match_id "
            "FROM contest_pairings WHERE contest_id=? AND match_id IS NOT NULL) "
            "ORDER BY id",
            (contest_id, contest_id, contest_id),
        )
    )
    if any(job.get("status") not in _TERMINAL_EXECUTION_STATUSES for job in jobs):
        _reject("active_execution", "赛事仍有未终结 execution job")

    stage_results = _validate_raw_repair_authority_tx(
        connection,
        contest,
        stages,
        entries,
        pairings,
        raw_stage_results,
    )
    for pairing in pairings:
        if pairing.get("entry_b_id") is None:
            continue
        if (
            pairing.get("status") != STATUS_COMPLETED
            or pairing.get("match_status") != STATUS_COMPLETED
            or pairing.get("_match_bot_a_id") != pairing.get("bot_a_id")
            or pairing.get("_match_bot_b_id") != pairing.get("bot_b_id")
        ):
            _reject("settlement_invalid", "赛事对阵未绑定同赛事完整赛果")
    try:
        summaries = build_stage_summaries(
            _SnapshotOnlyManager(),
            contest,
            entries,
            pairings,
            stage_results=stage_results,
            historical_topology_sealed=False,
            current_topology_sealed=False,
        )
    except AssertionError as exc:
        raise OfficialRepairError(
            "snapshot_incomplete", "阶段快照不足，拒绝 Match 重放"
        ) from exc
    if len(summaries) != 2:
        _reject("stage_authority_invalid", "阶段权威数量不完整")
    expected_sizes = (9, 8)
    for stage_idx, (summary, expected_size) in enumerate(
        zip(summaries, expected_sizes)
    ):
        if (
            summary.get("stage_idx") != stage_idx
            or summary.get("status") != "completed"
            or summary.get("source") != "persisted"
            or summary.get("_durable_chain_verified") is not True
            or summary.get("_materialized_topology_valid") is not True
            or summary.get("completed_pairings") != summary.get("total_pairings")
            or len(summary.get("rows") or []) != expected_size
        ):
            _reject("stage_authority_invalid", "阶段快照或完整拓扑无法验证")

    qualifier = [dict(row) for row in summaries[0]["rows"]]
    knockout = [dict(row) for row in summaries[1]["rows"]]
    qualifier_ids = {int(row["entry_id"]) for row in qualifier}
    knockout_ids = {int(row["entry_id"]) for row in knockout}
    roster_ids = {int(entry["id"]) for entry in entries}
    active_ids = {int(entry["id"]) for entry in active_entries}
    advance_ids = advancing_entry_ids(stages[0], qualifier, default_all=False)
    eliminated_ids = roster_ids - active_ids
    if (
        qualifier_ids != roster_ids
        or knockout_ids != active_ids
        or advance_ids != active_ids
        or len(eliminated_ids) != 1
        or not final_stage_replaces_previous_ranking(stages[1], stage_idx=1)
    ):
        _reject("advancement_invalid", "阶段晋级 cohort 无法精确证明")
    expected_groups = {int(entry["id"]): entry["group_id"] for entry in entries}
    merged = merge_replace_top(
        qualifier,
        knockout,
        scope=8,
        expected_entry_groups=expected_groups,
    )
    if len(merged) != 9:
        _reject("ranking_merge_invalid", "跨阶段正式排名无法合成")
    roster_by_id = {int(entry["id"]): entry for entry in entries}
    rebound = []
    for row in merged:
        entry = roster_by_id[int(row["entry_id"])]
        rebound.append(
            {**row, "bot_id": entry.get("bot_id"), "user_id": entry.get("user_id")}
        )
    candidate_input = build_official_result_rows(rebound, stage_idx=1)
    contest_context, roster_rows, stage_entry_ids, legacy_groups = (
        _official_result_validation_context_tx(connection, contest_id)
    )
    candidate = _validate_complete_official_results(
        _normalize_official_result_input(contest_id, candidate_input),
        contest_id=contest_id,
        contest=contest_context,
        roster_rows=roster_rows,
        stage_entry_ids=stage_entry_ids,
        legacy_entry_groups=legacy_groups,
    )
    candidate_business = [_official_business_row(row) for row in candidate]
    existing = _dict_rows(
        connection.execute(
            "SELECT " + ",".join(_OFFICIAL_RAW_COLUMNS) + " "
            "FROM contest_official_results WHERE contest_id=? "
            "ORDER BY rank,id",
            (contest_id,),
        )
    )
    _validate_existing_official_json(existing)
    existing_business = [_official_business_row(row) for row in existing]
    if len(existing_business) == len(candidate_business):
        if existing_business != candidate_business:
            _reject("official_mismatch", "既有正式排名不是审核 candidate")
        eligibility = "already_applied"
        missing_row = None
        missing_rank = None
        missing_is_eliminated = False
    elif len(existing_business) == len(candidate_business) - 1:
        if existing_business != candidate_business[:-1]:
            _reject("official_prefix_mismatch", "既有正式排名不是精确 candidate 前缀")
        missing_row = candidate_business[-1]
        if (
            missing_row.get("rank") != 9
            or missing_row.get("entry_id") not in eliminated_ids
        ):
            _reject("missing_tail_invalid", "唯一缺行不是淘汰者末位")
        eligibility = "repairable"
        missing_rank = 9
        missing_is_eliminated = True
    else:
        _reject("official_count_invalid", "正式排名缺行数量不受支持")

    sequence = _official_sequence_tx(connection)
    existing_raw = [_official_raw_row(row) for row in existing]
    if eligibility == "repairable":
        if sequence > _MAX_REPAIR_PREIMAGE_SEQUENCE:
            _reject(
                "official_sequence_invalid",
                "正式排名自增序列没有安全的单行修复空间",
            )
        missing_raw = {
            "id": sequence + 1,
            **copy.deepcopy(missing_row),
        }
        repaired_raw = [*copy.deepcopy(existing_raw), missing_raw]
        repaired_sequence = sequence + 1
    else:
        missing_raw = None
        repaired_raw = copy.deepcopy(existing_raw)
        repaired_sequence = sequence

    authority_sections = {
        "policy": POLICY_VERSION,
        "contest": contest,
        "entries": entries,
        "pairings": [
            {key: value for key, value in row.items() if not key.startswith("_")}
            for row in pairings
        ],
        "matches": match_authority,
        "matches_index": match_index,
        "stage_results": raw_stage_results,
        "execution_jobs": jobs,
        "schema": _dict_rows(
            connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema "
                "WHERE name NOT LIKE 'sqlite_autoindex_%' ORDER BY type,name"
            )
        ),
    }
    authority_digest = _digest(authority_sections)
    old_official_digest = _digest(
        {"rows": existing_raw, "sqlite_sequence": sequence}
    )
    repaired_official_digest = _digest(
        {"rows": repaired_raw, "sqlite_sequence": repaired_sequence}
    )
    source_business_digest = _database_business_digest(connection)
    expected_post_business_digest = _database_business_digest(
        connection,
        official_override=tuple(repaired_raw),
        official_sequence_override=repaired_sequence,
        contest_id=contest_id,
    )
    plan_digest = _digest(
        {
            "policy": POLICY_VERSION,
            "contest_id": contest_id,
            "authority_digest": authority_digest,
            "old_official_digest": old_official_digest,
            "repaired_official_digest": repaired_official_digest,
            "source_business_digest": source_business_digest,
            "expected_post_business_digest": expected_post_business_digest,
            "eligibility": eligibility,
            "missing_rank": missing_rank,
        }
    )
    return OfficialRepairPlan(
        contest_id=contest_id,
        eligibility=eligibility,
        authority_digest=authority_digest,
        old_official_digest=old_official_digest,
        repaired_official_digest=repaired_official_digest,
        plan_digest=plan_digest,
        source_business_digest=source_business_digest,
        expected_post_business_digest=expected_post_business_digest,
        existing_official_count=len(existing_business),
        repaired_official_count=len(candidate_business),
        missing_rank=missing_rank,
        missing_entry_is_eliminated=missing_is_eliminated,
        _candidate_rows=tuple(copy.deepcopy(candidate_business)),
        _missing_row=copy.deepcopy(missing_raw),
    )


def plan_official_results_repair(
    connection: sqlite3.Connection, contest_id: int
) -> OfficialRepairPlan:
    """Build one repair plan from the caller's SQLite snapshot."""
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("plan requires an explicit sqlite3.Connection")
    owns_transaction = not connection.in_transaction
    if owns_transaction:
        connection.execute("BEGIN")
    try:
        try:
            return _build_plan_tx(connection, contest_id)
        except OfficialRepairError:
            raise
        except (
            AssertionError,
            IndexError,
            KeyError,
            RecursionError,
            TypeError,
            ValueError,
        ) as exc:
            raise OfficialRepairError(
                "authority_invalid", "赛事修复权威数据损坏"
            ) from exc
    finally:
        if owns_transaction and connection.in_transaction:
            connection.rollback()


def _secure_regular_file(path: Path, *, label: str, mode: int | None = None) -> os.stat_result:
    try:
        metadata = path.lstat()
    except FileNotFoundError as exc:
        raise RuntimeError(f"{label}不存在") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_nlink != 1
        or metadata.st_mode & 0o077
        or (mode is not None and stat.S_IMODE(metadata.st_mode) != mode)
    ):
        raise RuntimeError(f"{label}文件权限或 inode 不安全")
    return metadata


def _stable_file_stat(
    metadata: os.stat_result,
) -> tuple[int, int, int, int, int, int, int, int]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_mode,
        metadata.st_uid,
        metadata.st_nlink,
    )


def _validate_target_preimage(
    database: Path,
    guard: OfficialRepairPathGuard,
    *,
    expected_stat: tuple[int, int, int, int, int, int, int, int],
    expected_sha256: str,
) -> None:
    if (
        type(expected_stat) is not tuple
        or len(expected_stat) != 8
        or any(type(value) is not int for value in expected_stat)
        or type(expected_sha256) is not str
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        _reject("target_preimage_changed", "审核 target preimage 参数损坏")
    before = _secure_regular_file(database, label="目标数据库", mode=0o600)
    if (
        _stable_file_stat(before) != expected_stat
        or (before.st_dev, before.st_ino) != (guard.target_dev, guard.target_ino)
    ):
        _reject("target_preimage_changed", "目标数据库 preimage 已变化")

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(database, flags)
    try:
        opened_before = os.fstat(fd)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or _stable_file_stat(opened_before) != expected_stat
        ):
            _reject("target_preimage_changed", "目标数据库 preimage 已变化")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        opened_after = os.fstat(fd)
    finally:
        os.close(fd)
    after = _secure_regular_file(database, label="目标数据库", mode=0o600)
    if (
        _stable_file_stat(opened_after) != expected_stat
        or _stable_file_stat(after) != expected_stat
        or digest.hexdigest() != expected_sha256
    ):
        _reject("target_preimage_changed", "目标数据库 preimage 已变化")


def _validate_cold_backup_preimage(
    backup: Path,
    guard: OfficialRepairPathGuard,
    *,
    expected_stat: tuple[int, int, int, int, int, int, int, int],
    expected_sha256: str,
) -> None:
    """Rebind the reviewed cold backup inside the target write transaction."""
    if (
        type(expected_stat) is not tuple
        or len(expected_stat) != 8
        or any(type(value) is not int for value in expected_stat)
        or type(expected_sha256) is not str
        or len(expected_sha256) != 64
        or any(character not in "0123456789abcdef" for character in expected_sha256)
    ):
        _reject("backup_preimage_changed", "审核 cold backup preimage 参数损坏")
    before = _secure_regular_file(backup, label="冷备", mode=0o400)
    if (
        _stable_file_stat(before) != expected_stat
        or (before.st_dev, before.st_ino) == (guard.target_dev, guard.target_ino)
    ):
        _reject("backup_preimage_changed", "冷备 preimage 已变化")
    try:
        assert_no_sqlite_sidecars(backup, label="冷备")
    except RuntimeError as exc:
        raise OfficialRepairError(
            "backup_preimage_changed", "冷备 preimage 已出现 SQLite sidecar"
        ) from exc

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(backup, flags)
    try:
        opened_before = os.fstat(fd)
        if (
            not stat.S_ISREG(opened_before.st_mode)
            or _stable_file_stat(opened_before) != expected_stat
        ):
            _reject("backup_preimage_changed", "冷备 preimage 已变化")
        digest = hashlib.sha256()
        while chunk := os.read(fd, 1024 * 1024):
            digest.update(chunk)
        opened_after = os.fstat(fd)
    finally:
        os.close(fd)
    after = _secure_regular_file(backup, label="冷备", mode=0o400)
    try:
        assert_no_sqlite_sidecars(backup, label="冷备")
    except RuntimeError as exc:
        raise OfficialRepairError(
            "backup_preimage_changed", "冷备 preimage 已出现 SQLite sidecar"
        ) from exc
    if (
        _stable_file_stat(opened_after) != expected_stat
        or _stable_file_stat(after) != expected_stat
        or digest.hexdigest() != expected_sha256
    ):
        _reject("backup_preimage_changed", "冷备 preimage 已变化")


def assert_no_sqlite_sidecars(path: Path, *, label: str) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        if os.path.lexists(str(path) + suffix):
            raise RuntimeError(f"{label}存在 SQLite {suffix} sidecar")


def validate_official_repair_file(
    path: str | os.PathLike[str], *, label: str, mode: int
) -> Path:
    """Resolve one canonical operator-supplied cold file without following links."""
    raw = Path(path).expanduser()
    if not raw.is_absolute():
        raise RuntimeError(f"{label}必须使用绝对路径")
    canonical = raw.resolve(strict=True)
    if raw != canonical:
        raise RuntimeError(f"{label}必须是 canonical 路径且不能经过 symlink")
    _secure_regular_file(canonical, label=label, mode=mode)
    assert_no_sqlite_sidecars(canonical, label=label)
    return canonical


def validate_offline_repair_database_state(
    connection: sqlite3.Connection,
) -> None:
    """Public CLI facade for the transaction-local maintenance gate."""
    _validate_offline_database_state(connection)


def scan_official_results_repairs(
    connection: sqlite3.Connection,
) -> list[dict[str, Any]]:
    """Validate every ready finished official table without exposing identities.

    A normal complete table is ``valid`` regardless of template or lifecycle
    epoch.  Only a table which first fails the shared complete-table validator
    is routed through the deliberately narrow legacy-tail eligibility planner.
    """
    contests = _dict_rows(
        connection.execute(
            "SELECT * FROM contests WHERE status='finished' ORDER BY id"
        )
    )
    reports: list[dict[str, Any]] = []
    for contest in contests:
        ready = exact_sqlite_bool(contest.get("official_results_ready"))
        if ready is False:
            continue
        contest_id = contest.get("id")
        if (
            ready is not True
            or isinstance(contest_id, bool)
            or not isinstance(contest_id, int)
            or contest_id < 1
        ):
            reports.append(
                {
                    "contest_id": contest_id if type(contest_id) is int else None,
                    "eligibility": "blocked",
                    "reason_code": "official_readiness_invalid",
                }
            )
            continue
        try:
            context, roster, stage_ids, legacy_groups = (
                _official_result_validation_context_tx(connection, contest_id)
            )
            raw_rows = _dict_rows(
                connection.execute(
                    "SELECT " + ",".join(_OFFICIAL_RAW_COLUMNS) + " "
                    "FROM contest_official_results WHERE contest_id=? "
                    "ORDER BY rank,id",
                    (contest_id,),
                )
            )
            _validate_existing_official_json(raw_rows)
            _validate_complete_official_results(
                raw_rows,
                contest_id=contest_id,
                contest=context,
                roster_rows=roster,
                stage_entry_ids=stage_ids,
                legacy_entry_groups=legacy_groups,
            )
            reports.append(
                {
                    "contest_id": contest_id,
                    "eligibility": "valid",
                    "official_count": len(raw_rows),
                    "official_digest": _digest(
                        {
                            "rows": raw_rows,
                            "sqlite_sequence": (
                                _official_sequence_tx(connection)
                                if connection.execute(
                                    "SELECT 1 FROM contest_official_results LIMIT 1"
                                ).fetchone()
                                else None
                            ),
                        }
                    ),
                }
            )
        except (
            OfficialRepairError,
            AssertionError,
            IndexError,
            KeyError,
            RecursionError,
            TypeError,
            ValueError,
        ):
            try:
                reports.append(
                    plan_official_results_repair(
                        connection, contest_id
                    ).public_report()
                )
            except OfficialRepairError as exc:
                reports.append(
                    {
                        "contest_id": contest_id,
                        "eligibility": "blocked",
                        "reason_code": exc.code,
                    }
                )
    return reports


def validate_official_repair_inventory(
    reports: list[dict[str, Any]],
    contest_id: int,
    *,
    repaired: bool,
) -> None:
    """Require the exact all-ready-contest preimage or postimage inventory."""
    if not isinstance(reports, list) or any(
        not isinstance(report, dict)
        or type(report.get("contest_id")) is not int
        or report.get("eligibility") not in {
            "valid",
            "repairable",
            "blocked",
        }
        for report in reports
    ):
        _reject("inventory_invalid", "正式排名 inventory 报告损坏")
    if any(report["eligibility"] == "blocked" for report in reports):
        _reject("inventory_blocked", "仍有无法验证的 ready 终态正式排名")
    repairable_ids = {
        int(report["contest_id"])
        for report in reports
        if report["eligibility"] == "repairable"
    }
    expected_repairable = set() if repaired else {contest_id}
    by_id = {int(report["contest_id"]): report for report in reports}
    if (
        len(by_id) != len(reports)
        or repairable_ids != expected_repairable
        or contest_id not in by_id
        or by_id[contest_id]["eligibility"]
        != ("valid" if repaired else "repairable")
    ):
        _reject("inventory_mismatch", "正式排名 inventory 不符合唯一目标状态")


@contextlib.contextmanager
def offline_official_repair_guard(
    database_path: str | os.PathLike[str],
) -> Iterator[OfficialRepairPathGuard]:
    """Acquire an existing, inode-pinned dispatcher lock without creating it."""
    raw = Path(database_path).expanduser()
    if not raw.is_absolute():
        raise RuntimeError("修复数据库必须使用绝对路径")
    database = raw.resolve(strict=True)
    if raw != database:
        raise RuntimeError("修复数据库必须是 canonical 路径且不能经过 symlink")
    target_stat = _secure_regular_file(database, label="目标数据库", mode=0o600)
    lock_path = Path(str(database) + ".execution-dispatcher.lock")
    lock_stat = _secure_regular_file(lock_path, label="dispatcher lock", mode=0o600)
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags)
    try:
        opened = os.fstat(fd)
        if (
            opened.st_dev != lock_stat.st_dev
            or opened.st_ino != lock_stat.st_ino
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise RuntimeError("dispatcher lock inode 在打开期间变化")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("dispatcher 仍持有目标数据库 lock") from exc
        guard = OfficialRepairPathGuard(
            database_path=str(database),
            lock_path=str(lock_path),
            thread_id=threading.get_ident(),
            lock_fd=fd,
            lock_dev=opened.st_dev,
            lock_ino=opened.st_ino,
            target_dev=target_stat.st_dev,
            target_ino=target_stat.st_ino,
        )
        try:
            yield guard
        finally:
            try:
                if not guard.finalized:
                    _validate_guard(guard, database)
                    assert_no_sqlite_sidecars(database, label="目标数据库")
            finally:
                guard.active = False
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _validate_guard(guard: OfficialRepairPathGuard, database: Path) -> None:
    if (
        not isinstance(guard, OfficialRepairPathGuard)
        or not guard.active
        or guard.thread_id != threading.get_ident()
        or guard.database_path != str(database)
    ):
        raise RuntimeError("official repair 缺少当前路径与线程的独占 guard")
    lock_stat = _secure_regular_file(
        Path(guard.lock_path), label="dispatcher lock", mode=0o600
    )
    opened = os.fstat(guard.lock_fd)
    target = _secure_regular_file(database, label="目标数据库", mode=0o600)
    if (
        (lock_stat.st_dev, lock_stat.st_ino)
        != (guard.lock_dev, guard.lock_ino)
        or (opened.st_dev, opened.st_ino) != (guard.lock_dev, guard.lock_ino)
        or (target.st_dev, target.st_ino) != (guard.target_dev, guard.target_ino)
    ):
        raise RuntimeError("official repair 路径或 lock inode 已变化")


def finalize_official_repair_guard(
    guard: OfficialRepairPathGuard,
    database_path: str | os.PathLike[str],
    *,
    validate_final_state: Callable[[], None],
) -> None:
    """Finish all fallible final checks while retaining the dispatcher flock."""
    database = Path(database_path).expanduser().resolve(strict=True)
    if guard.finalized or not callable(validate_final_state):
        raise RuntimeError("official repair guard finalize 参数损坏")
    _validate_guard(guard, database)
    assert_no_sqlite_sidecars(database, label="目标数据库")
    validate_final_state()
    _validate_guard(guard, database)
    assert_no_sqlite_sidecars(database, label="目标数据库")
    guard.finalized = True


def _validate_offline_database_state(connection: sqlite3.Connection) -> None:
    control = _one_dict(
        connection.execute("SELECT * FROM execution_control WHERE singleton=1")
    )
    if (
        control is None
        or control.get("deployment_drain_requested") != 1
        or control.get("accepting") != 0
        or control.get("auto_enabled") != 0
        or control.get("dispatcher_state") != "stopped"
    ):
        _reject("maintenance_invalid", "数据库未处于已排空维护状态")
    active_jobs = connection.execute(
        "SELECT COUNT(*) FROM execution_jobs WHERE status IN ('starting','running','settling')"
    ).fetchone()[0]
    active_attempts = connection.execute(
        "SELECT COUNT(*) FROM execution_job_attempts "
        "WHERE status IN ('starting','running','settling')"
    ).fetchone()[0]
    active_leases = connection.execute(
        "SELECT COUNT(*) FROM local_ai_leases WHERE status='active'"
    ).fetchone()[0]
    launch = connection.execute(
        "SELECT state FROM docker_launch_journal WHERE singleton=1"
    ).fetchone()
    if (
        active_jobs
        or active_attempts
        or active_leases
        or launch is None
        or launch[0] != "idle"
    ):
        _reject("maintenance_not_ready", "数据库仍有 active 执行工件")
    for table_row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name GLOB 'matches_*' AND name<>'matches_index'"
    ):
        table = str(table_row[0])
        if connection.execute(
            f"SELECT 1 FROM {_quoted_identifier(table)} "
            "WHERE status='running' LIMIT 1"
        ).fetchone():
            _reject("maintenance_not_ready", "数据库仍有 running Match")


def apply_official_results_repair(
    database_path: str | os.PathLike[str],
    contest_id: int,
    *,
    expected_authority_digest: str,
    expected_old_official_digest: str,
    expected_plan_digest: str,
    expected_repaired_official_digest: str,
    expected_source_business_digest: str,
    expected_post_business_digest: str,
    expected_target_stat: tuple[int, int, int, int, int, int, int, int],
    expected_target_preimage_sha256: str,
    cold_backup_path: str | os.PathLike[str],
    expected_backup_stat: tuple[int, int, int, int, int, int, int, int],
    expected_backup_sha256: str,
    guard: OfficialRepairPathGuard,
) -> OfficialRepairPlan:
    """Insert the unique missing tail row in one guarded raw SQLite tx."""
    database = Path(database_path).expanduser().resolve(strict=True)
    raw_backup = Path(cold_backup_path).expanduser()
    if not raw_backup.is_absolute():
        _reject("backup_preimage_changed", "冷备必须使用 canonical 绝对路径")
    try:
        cold_backup = raw_backup.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise OfficialRepairError(
            "backup_preimage_changed", "冷备 canonical 路径已变化"
        ) from exc
    if raw_backup != cold_backup:
        _reject("backup_preimage_changed", "冷备必须使用 canonical 绝对路径")
    _validate_guard(guard, database)
    assert_no_sqlite_sidecars(database, label="目标数据库")
    connection = sqlite3.connect(
        database.as_uri() + "?mode=rw",
        uri=True,
        isolation_level=None,
    )
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA query_only=OFF")
        connection.execute("PRAGMA synchronous=FULL")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise RuntimeError("official repair 无法启用 foreign_keys")
        if connection.execute("PRAGMA trusted_schema").fetchone()[0] != 0:
            raise RuntimeError("official repair 无法关闭 trusted_schema")
        if connection.execute("PRAGMA query_only").fetchone()[0] != 0:
            raise RuntimeError("official repair 连接意外处于 query_only")
        if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
            raise RuntimeError("official repair 无法启用 synchronous=FULL")
        if connection.execute("PRAGMA journal_mode").fetchone()[0].lower() != "delete":
            raise RuntimeError("official repair 只接受 journal_mode=delete 的冷库")
        if connection.execute("PRAGMA locking_mode").fetchone()[0].lower() != "normal":
            raise RuntimeError("official repair 只接受 locking_mode=normal 的冷库")
        connection.execute("BEGIN IMMEDIATE")
        _validate_guard(guard, database)
        _validate_target_preimage(
            database,
            guard,
            expected_stat=expected_target_stat,
            expected_sha256=expected_target_preimage_sha256,
        )
        _validate_cold_backup_preimage(
            cold_backup,
            guard,
            expected_stat=expected_backup_stat,
            expected_sha256=expected_backup_sha256,
        )
        _validate_offline_database_state(connection)
        plan = plan_official_results_repair(connection, contest_id)
        if not plan.eligible or plan._missing_row is None:
            _reject("not_repairable", "当前数据库不是待补唯一尾行状态")
        validate_official_repair_inventory(
            scan_official_results_repairs(connection),
            contest_id,
            repaired=False,
        )
        expected = {
            "authority_digest": expected_authority_digest,
            "old_official_digest": expected_old_official_digest,
            "plan_digest": expected_plan_digest,
            "repaired_official_digest": expected_repaired_official_digest,
            "source_business_digest": expected_source_business_digest,
            "expected_post_business_digest": expected_post_business_digest,
        }
        actual = {
            "authority_digest": plan.authority_digest,
            "old_official_digest": plan.old_official_digest,
            "plan_digest": plan.plan_digest,
            "repaired_official_digest": plan.repaired_official_digest,
            "source_business_digest": plan.source_business_digest,
            "expected_post_business_digest": plan.expected_post_business_digest,
        }
        if any(
            not isinstance(value, str)
            or len(value) != 64
            or value.lower() != actual[key]
            for key, value in expected.items()
        ):
            _reject("digest_cas_failed", "审核 digest 与事务内计划不一致")
        row = plan._missing_row
        connection.execute(
            "INSERT INTO contest_official_results("
            + ",".join(_OFFICIAL_RAW_COLUMNS)
            + ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            tuple(row[column] for column in _OFFICIAL_RAW_COLUMNS),
        )
        repaired = plan_official_results_repair(connection, contest_id)
        validate_official_repair_inventory(
            scan_official_results_repairs(connection),
            contest_id,
            repaired=True,
        )
        if (
            not repaired.already_applied
            or repaired.authority_digest != plan.authority_digest
            or repaired.repaired_official_digest != plan.repaired_official_digest
            or repaired.source_business_digest
            != plan.expected_post_business_digest
            or repaired.expected_post_business_digest
            != plan.expected_post_business_digest
        ):
            _reject("postimage_invalid", "修复事务 postimage 无法验证")
        if connection.execute("PRAGMA foreign_key_check").fetchall():
            _reject("postimage_invalid", "修复事务产生外键损坏")
        integrity = [
            str(row[0]) for row in connection.execute("PRAGMA integrity_check")
        ]
        if integrity != ["ok"]:
            _reject("postimage_invalid", "修复事务 integrity_check 失败")
        _validate_guard(guard, database)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    _validate_guard(guard, database)
    assert_no_sqlite_sidecars(database, label="目标数据库")
    return repaired
