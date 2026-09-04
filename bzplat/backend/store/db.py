"""botzone-platform SQLite 存储层。

持久连接 + threading.Lock；时间戳统一 ISO 秒精度；行返回 dict。
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import math
import os
import secrets
import sqlite3
import stat
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from bzplat.backend.mail import seed_email_templates
from bzplat.backend.runtime.binary_integrity import require_binary_file_integrity
from bzplat.backend.runtime.config import RANKING_MIN_RATED_MATCHES

from .public_contract import (
    PUBLIC_CROSS_GROUP_TIEBREAK_FIELDS,
    READ_TECHNICAL_INCIDENT_EVENTS,
    canonical_public_completed_reason,
    canonical_public_error_reason,
    sanitize_public_event,
    sanitize_public_event_prefix,
    sanitize_public_incident,
    sanitize_public_match,
    sanitize_public_contest_tiebreaks,
    sanitize_public_stage_result_payload,
)

from .schema import (
    CODE_RESET,
    COMMENT_TARGET_TYPES,
    EMAIL_CODE_MAX_FAILED_ATTEMPTS,
    CONTEST_ENTRY_PAGE_INDEX_SQL,
    CONTEST_SOURCE_DEFAULT_NAVIGATION_ALL_INDEX_SQL,
    CONTEST_SOURCE_DEFAULT_NAVIGATION_OWNER_INDEX_SQL,
    CONTEST_SOURCE_DEFAULT_NAVIGATION_PUBLIC_INDEX_SQL,
    CONTEST_SOURCE_DEFAULT_PROTECTED_INDEX_SQL,
    CONTEST_SOURCE_NAVIGATION_ALL_INDEX_SQL,
    CONTEST_SOURCE_NAVIGATION_OWNER_INDEX_SQL,
    CONTEST_SOURCE_NAVIGATION_PUBLIC_INDEX_SQL,
    CONTEST_SOURCE_PROTECTED_INDEX_SQL,
    CONTEST_SOURCE_SEARCH_GRAMS_TABLE_SQL,
    CONTEST_TITLE_EDGE_WHITESPACE_CODEPOINTS,
    CONTEST_TITLE_MAX_LENGTH,
    CONTEST_PAIRING_SCHEDULE_INDEX_SQL,
    CONTEST_PAIRING_SEED_LOOKUP_INDEX_SQL,
    CONTEST_PAIRING_SYNC_INDEX_SQL,
    CONTEST_CANCELLED,
    CONTEST_DRAFT,
    CONTEST_FINISHED,
    CONTEST_IDENTITY_SOURCE_LEGACY,
    CONTEST_IDENTITY_SOURCE_REGISTRATION,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    CONTEST_REST,
    CONTEST_RUNNING,
    DEFAULT_RUNTIME_MODE,
    ELIMINATION_TIEBREAK_PAIRED_SWAP,
    EXECUTION_CLAIM_CONTEST_ORDER_INDEX_SQL,
    EXECUTION_CLAIM_SOURCE_ORDER_INDEX_SQL,
    EXECUTION_CANCELLED,
    EXECUTION_CONTEST_DISPATCH_GAP_INDEX_SQL,
    EXECUTION_INTERRUPTED,
    EXECUTION_QUEUED,
    EXECUTION_RUNNING,
    EXECUTION_SETTLING,
    EXECUTION_SOURCE_CONTEST,
    EXECUTION_STARTING,
    MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,
    LOCAL_AI_MAX_ACTIVE_AGENTS_PER_OWNER,
    LOCAL_AI_MAX_ONLINE_GLOBAL,
    LOCAL_AI_MAX_ONLINE_PER_OWNER,
    PUBLIC_MATCH_COMPLETED_REASONS,
    PUBLIC_MATCH_ERROR_FALLBACK,
    PUBLIC_MATCH_ERROR_REASONS,
    SCHEMA,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    SUPPORTED_BINARY_ARCH,
    SUPPORTED_BINARY_FORMAT,
    SUPPORTED_BINARY_OS,
    TECHNICAL_INCIDENT_EVENT,
    TYPE_CONTEST,
    TYPE_HUMAN,
    TYPE_LADDER,
    validate_contest_title,
    LIKE_TARGET_TYPES,
    MATCH_DEBUG_MAX_BYTES_PER_MATCH,
    MATCH_DEBUG_MAX_BYTES_PER_SEAT,
    MATCH_DEBUG_MAX_ENTRIES_PER_MATCH,
    MATCH_DEBUG_MAX_ENTRIES_PER_SEAT,
    MATCH_DEBUG_MAX_ENTRY_BYTES,
    VALID_RUNTIME_MODES,
    game_rule_contract,
    pending_orphan_recovery_reason,
    require_supported_binary_metadata,
    validate_orphan_recovery_reason,
)
from .validation import (
    exact_nonnegative_int,
    exact_sqlite_bool,
    is_authoritative_no_opponent_pairing,
    validate_canonical_naive_timestamp,
    validate_contest_times,
)

DEFAULT_DB_PATH = "botzone.db"

logger = logging.getLogger(__name__)

_AUTO_MATCH_FAIR_BOOTSTRAP_VERSION = 1
_AUTO_MATCH_POLICY_VERSION = "owner-game-lane-v2"
_RATING_PROJECTION_POLICY_VERSION = "owner-ranked-bot-v4"
_RATING_PROJECTION_LEGACY_VERSION = "legacy-unverified"

# 管理端用户目录允许展示实名/联系信息，但认证凭据仍不属于目录契约。
# 保持显式列清单，避免 users 后续新增敏感列时被 SELECT * 自动带到 API。
_ADMIN_USER_COLUMNS = (
    "id", "username", "email", "role", "display_name", "is_active",
    "email_verified", "created_at", "last_login_at", "real_name", "phone",
    "school", "student_id",
)

# 管理员只能查看会话审计元数据并按用户整体吊销；bearer token 永不离开 Store。
_ADMIN_SESSION_COLUMNS = (
    "s.user_id", "u.username", "s.expires_at", "s.created_at", "s.ip_addr",
    "s.user_agent",
)


@dataclass(frozen=True)
class _RatingProjectionMutationGuard:
    """Proof that the marker-settled projection was trusted before one write.

    The proof is process-local and is only valid inside the ``BEGIN IMMEDIATE``
    transaction that created it.  Requiring this explicit value at the advance
    site prevents a stale projection from being made current merely because its
    policy-version string happens to match.
    """

    trusted_before: bool


class LocalAIAgentBusyError(ValueError):
    """A credential mutation would interrupt an active execution lease."""


class RankedBotSelectionBusyError(ValueError):
    """Changing the ranked representative would cross an active rated lifecycle."""


class BotDeletedError(ValueError):
    """An owner mutation targeted a retained, logically deleted Bot identity."""


class BotOwnerDeleteBusyError(ValueError):
    """Logical deletion would cross an active execution or contest lifecycle."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


class ContestRealNameRosterForbidden(ValueError):
    """A proxy roster write lacks the participant consent required for PII capture."""

    MESSAGE = "实名赛事仅允许参赛者本人报名，组织者不可代报名"

    def __init__(self) -> None:
        super().__init__(self.MESSAGE)


class ContestRosterWriteValidationError(ValueError):
    """Roster validation failed after the Store acquired its writer lock.

    The non-PII gate bit lets an authorized API audit the actual identity mode
    used by the failed transaction instead of an earlier autocommit view.
    """

    def __init__(self, message: str, *, identity_required_at_commit: bool) -> None:
        super().__init__(message)
        self.identity_required_at_commit = bool(identity_required_at_commit)


@dataclass
class _OfflineCutoverGuard:
    """Process-local proof that the DB dispatcher flock is exclusively held."""

    store_identity: int | None
    database_path: str
    thread_id: int
    active: bool = True


@contextlib.contextmanager
def offline_cutover_path_guard(database_path: str | os.PathLike[str]):
    """Lock one DB path before opening a migration-capable :class:`Store`.

    The CLI must acquire the same DB-adjacent flock as the dispatcher before
    ``Store.__init__`` can execute schema migrations.  The returned proof is
    deliberately unbound until the caller constructs and binds that exact
    Store instance.
    """

    database = str(Path(database_path).expanduser().resolve())
    lock_path = database + ".execution-dispatcher.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                "规则 hard cutover 仅允许停服冷切；dispatcher 仍在线"
            ) from exc
        guard = _OfflineCutoverGuard(
            store_identity=None,
            database_path=database,
            thread_id=threading.get_ident(),
        )
        try:
            yield guard
        finally:
            guard.active = False
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _json_values_exactly_equal(left: Any, right: Any) -> bool:
    """Compare parsed JSON without Python's bool/int/float aliases."""

    if type(left) is not type(right):
        return False
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _json_values_exactly_equal(left_item, right_item)
            for left_item, right_item in zip(left, right)
        )
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _json_values_exactly_equal(left[key], right[key]) for key in left
        )
    return bool(left == right)


def _finalize_terminal_replay_tx(
    conn: sqlite3.Connection,
    *,
    match: sqlite3.Row | dict[str, Any],
    updated_at: str,
) -> None:
    """Persist the Match-authoritative terminal without losing a valid prefix.

    Recovery and reconciliation run after the Match terminal transition may
    already have committed, but they still own making the raw replay durable
    before a job/pairing can advance. Missing replay rows are created. A
    parseable JSON array keeps every non-terminal prefix item, while stale
    terminal markers are replaced from the Match row: completed uses the same
    canonical builder as the public projection; aborted normalizes only the
    Match reason through the public allowlist. Corrupt/non-array snapshots
    cannot be trusted as history, so they become terminal-only and emit a
    bounded warning that never includes the raw payload.
    """
    authoritative = dict(match)
    match_id = authoritative.get("id")
    if not isinstance(match_id, str) or not match_id:
        raise ValueError("terminal replay Match id 无效")
    status = authoritative.get("status")
    if status not in {STATUS_COMPLETED, STATUS_ABORTED}:
        raise ValueError("terminal replay Match 尚未终态")
    replay = conn.execute(
        "SELECT events_json FROM match_replays WHERE match_id=?",
        (match_id,),
    ).fetchone()
    events: list[Any] = []
    rebuild_issue: str | None = None
    if replay is not None:
        raw_events = replay["events_json"]
        if not isinstance(raw_events, str):
            rebuild_issue = "invalid_type"
        else:
            try:
                parsed = json.loads(raw_events)
            except (TypeError, ValueError):
                rebuild_issue = "invalid_json"
            else:
                if not isinstance(parsed, list):
                    rebuild_issue = "non_array"
                else:
                    try:
                        json.dumps(parsed, allow_nan=False)
                    except (TypeError, ValueError):
                        rebuild_issue = "non_standard_json"
                    else:
                        events = parsed
    prefix = [
        event
        for event in events
        if not (
            isinstance(event, dict)
            and event.get("type") in {"match_end", "error"}
        )
    ]
    if status == STATUS_COMPLETED:
        # Import lazily: ``matches.__init__`` exposes the orchestrator, whose
        # game registry reaches the Store while the package graph is loading.
        from bzplat.backend.matches.result_contract import canonical_deltas

        raw_result = authoritative.get("result")
        if isinstance(raw_result, str):
            try:
                raw_result = json.loads(raw_result)
            except (TypeError, ValueError) as exc:
                raise ValueError("completed Match result deltas 无效") from exc
        if not isinstance(raw_result, dict):
            raise ValueError("completed Match result deltas 无效")
        try:
            strict_deltas = canonical_deltas(raw_result.get("deltas"))
        except (TypeError, ValueError) as exc:
            raise ValueError("completed Match result deltas 无效") from exc
        strict_match = dict(authoritative)
        strict_match["result"] = {"deltas": strict_deltas}
        # The read-side projection deliberately tolerates legacy damage.  The
        # write-side gate may reuse its public shape only after validating the
        # authoritative Match result, and must never offer stale replay events
        # as a fallback source for deltas.
        terminal = _canonical_public_match_end(strict_match, [])
    else:
        terminal = {
            "type": "error",
            "reason": canonical_public_error_reason(authoritative.get("reason")),
        }
    canonical_events = [*prefix, terminal]
    if (
        rebuild_issue is None
        and replay is not None
        and _json_values_exactly_equal(events, canonical_events)
    ):
        return
    events_json = json.dumps(
        canonical_events,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    conn.execute(
        "INSERT INTO match_replays(match_id,events_json,updated_at) "
        "VALUES(?,?,?) ON CONFLICT(match_id) DO UPDATE SET "
        "events_json=excluded.events_json,updated_at=excluded.updated_at",
        (match_id, events_json, updated_at),
    )
    if rebuild_issue is not None:
        logger.warning(
            "recovery_replay_rebuilt match_id=%s status=%s reason=%s issue=%s",
            match_id,
            status,
            terminal["reason"],
            rebuild_issue,
        )


def _match_recovery_affiliation_tx(
    conn: sqlite3.Connection,
    *,
    match_id: str,
    direct_contest_id: Any,
) -> tuple[str, int | None]:
    """Classify one Match from every durable contest reference in this tx.

    ``match_type`` is intentionally absent because legacy rows can drift there.
    A Match is genuinely unaffiliated only when both its direct ``contest_id``
    and every ``contest_pairings.match_id`` reference are absent. All references
    must collapse to one active, non-showcase contest id; terminal, showcase,
    dangling or conflicting identities are immutable and fail closed.
    """
    contest_ids: set[int] = set()
    if direct_contest_id is not None:
        if type(direct_contest_id) is not int or direct_contest_id <= 0:
            return "blocked", None
        contest_ids.add(direct_contest_id)
    pairing_rows = conn.execute(
        "SELECT DISTINCT contest_id FROM contest_pairings WHERE match_id=?",
        (match_id,),
    ).fetchall()
    for pairing_row in pairing_rows:
        pairing_contest_id = pairing_row["contest_id"]
        if type(pairing_contest_id) is not int or pairing_contest_id <= 0:
            return "blocked", None
        contest_ids.add(pairing_contest_id)
    if not contest_ids:
        return "unaffiliated", None
    if len(contest_ids) != 1:
        return "blocked", None
    contest_id = next(iter(contest_ids))
    contest = conn.execute(
        "SELECT status,showcase_key FROM contests WHERE id=?",
        (contest_id,),
    ).fetchone()
    if (
        contest is None
        or contest["status"]
        not in {CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST}
        or contest["showcase_key"] is not None
    ):
        return "blocked", contest_id
    return "active", contest_id


_CONTEST_IDENTITY_PROFILE_FIELDS = (
    "real_name",
    "phone",
    "school",
    "student_id",
)


def _registration_identity_tx(
    conn: sqlite3.Connection,
    contest_id: int,
    user_id: int,
    *,
    captured_at: str,
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None]:
    """Freeze a complete registration profile inside the entry write transaction.

    Non-real-name contests deliberately persist six NULLs.  Existing real-name
    entries also remain NULL after migration; their explicitly labelled legacy
    fallback is a private read-model concern, not a fabricated snapshot.
    """
    contest = conn.execute(
        "SELECT require_real_name FROM contests WHERE id=?", (contest_id,)
    ).fetchone()
    if not contest:
        raise ValueError("赛事不存在")
    if not int(contest["require_real_name"] or 0):
        return (None, None, None, None, None, None)
    user = conn.execute(
        "SELECT real_name,phone,school,student_id FROM users WHERE id=?",
        (user_id,),
    ).fetchone()
    values = tuple(
        str(user[field] or "").strip() if user is not None else ""
        for field in _CONTEST_IDENTITY_PROFILE_FIELDS
    )
    if not all(values):
        raise ValueError(
            "本赛事要求实名，参赛者须先填写完整实名信息（姓名/手机号/学校/学号）"
        )
    return (
        values[0],
        values[1],
        values[2],
        values[3],
        captured_at,
        CONTEST_IDENTITY_SOURCE_REGISTRATION,
    )


def _version_in_cutover_manifest_tx(
    conn: sqlite3.Connection, *, bot_id: int, version: int
) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM protocol_cutovers cutover,"
            "json_each(cutover.manifest_json) item "
            "WHERE CAST(json_extract(item.value,'$.bot_id') AS INTEGER)=? "
            "AND CAST(json_extract(item.value,'$.version') AS INTEGER)=? LIMIT 1",
            (int(bot_id), int(version)),
        ).fetchone()
    )


def _cutover_audit_version_count_tx(
    conn: sqlite3.Connection,
    *,
    bot_id: int | None = None,
    owner_id: int | None = None,
) -> int:
    filters: list[str] = []
    params: list[int] = []
    if bot_id is not None:
        filters.append("version.bot_id=?")
        params.append(int(bot_id))
    if owner_id is not None:
        filters.append("bot.owner_id=?")
        params.append(int(owner_id))
    if not filters:
        raise ValueError("cutover audit version scope required")
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM bot_versions version "
        "JOIN bots bot ON bot.id=version.bot_id WHERE "
        + " AND ".join(filters)
        + " AND (version.retired_at IS NOT NULL OR EXISTS("
        "SELECT 1 FROM protocol_cutovers cutover,"
        "json_each(cutover.manifest_json) item "
        "WHERE CAST(json_extract(item.value,'$.bot_id') AS INTEGER)=version.bot_id "
        "AND CAST(json_extract(item.value,'$.version') AS INTEGER)=version.version))",
        tuple(params),
    ).fetchone()
    return int(row["n"] if row else 0)


_GLICKO_95_Z = 1.96


def _attach_numeric_ranking(
    row: dict,
    *,
    eligible: bool,
    rank: int | None,
    rank_total: int,
) -> dict:
    """Attach the public numeric ranking contract to one rating row.

    Percentile is an ordinal interpolation over eligible public Bots: a sole
    Bot is at 100; with N>1, ``100 * (N-rank) / (N-1)`` maps first to 100 and
    last to 0. Bots below the public sample threshold have no rank/percentile.
    """
    rated_matches = max(0, int(row.get("rated_matches") or 0))
    minimum = max(1, int(RANKING_MIN_RATED_MATCHES))
    rating = row.get("rating")
    rd = row.get("rd")

    row["rated_matches"] = rated_matches
    row["ranking_min_matches"] = minimum
    row["ranking_progress"] = round(min(1.0, rated_matches / minimum), 4)
    row["ranking_eligible"] = bool(eligible)
    row["rank_total"] = max(0, int(rank_total))
    row["rank"] = int(rank) if eligible and rank is not None else None

    if rating is None or rd is None:
        row["confidence_low"] = None
        row["confidence_high"] = None
    else:
        center = float(rating)
        spread = _GLICKO_95_Z * max(0.0, float(rd))
        row["confidence_low"] = round(center - spread, 2)
        row["confidence_high"] = round(center + spread, 2)

    if not eligible or row["rank"] is None or row["rank_total"] <= 0:
        row["percentile"] = None
    elif row["rank_total"] == 1:
        row["percentile"] = 100.0
    else:
        row["percentile"] = round(
            100.0 * (row["rank_total"] - row["rank"]) / (row["rank_total"] - 1),
            2,
        )
    return row


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


_STAGE_DECISION_CONTEST_TOKEN_FIELDS = (
    "id",
    "status",
    "game_id",
    "template_id",
    "stages_json",
    "current_stage_idx",
    "format_snapshot_json",
    "source_contest_id",
    "published_stage_pairing_count",
    "pairing_topology_revision",
    "sealed_pairing_topology_revision",
)
_STAGE_DECISION_ENTRY_TOKEN_FIELDS = (
    "id",
    "contest_id",
    "user_id",
    "bot_id",
    "registered_at",
    "seed",
    "group_id",
    "eliminated",
)
_STAGE_DECISION_PAIRING_TOKEN_FIELDS = (
    "id",
    "contest_id",
    "round_num",
    "entry_a_id",
    "entry_b_id",
    "_raw_entry_a_id",
    "_raw_entry_b_id",
    "_entry_a_user_id",
    "_entry_b_user_id",
    "bot_a_id",
    "bot_b_id",
    "_pairing_bot_a_owner_id",
    "_pairing_bot_b_owner_id",
    "bot_a_version_id",
    "bot_b_version_id",
    "pairing_seed",
    "published_at",
    "scheduled_at",
    "match_id",
    "status",
    "stage_idx",
    "stage_key",
    "group_id",
    "bracket_slot",
    "color_first",
    "series_index",
    "series_size",
    "tiebreak_group",
    "tiebreak_game",
    "_explicit_series_marker",
    "_match_id",
    "match_status",
    "match_winner",
    "_match_contest_id",
    "_match_game_id",
    "_match_type",
    "_match_bot_a_id",
    "_match_bot_b_id",
    "_match_reason",
    "_match_technical_loss",
    "_match_result_json",
    "_match_config_json",
    "_match_created_at",
    "started_at",
    "ended_at",
)

# Exact durable columns consumed when a manager certifies a pre-manifest
# published stage-zero batch.  The expected rows come from one Store snapshot;
# comparing this complete persisted projection under ``BEGIN IMMEDIATE`` keeps
# a concurrent row replacement from borrowing the manager's topology proof.
_PUBLISHED_PAIRING_SEAL_FIELDS = (
    "id",
    "contest_id",
    "round_num",
    "entry_a_id",
    "entry_b_id",
    "bot_a_id",
    "bot_b_id",
    "bot_a_version_id",
    "bot_b_version_id",
    "pairing_seed",
    "published_at",
    "scheduled_at",
    "match_id",
    "status",
    "stage_idx",
    "stage_key",
    "group_id",
    "bracket_slot",
    "color_first",
    "series_index",
    "series_size",
    "tiebreak_group",
    "tiebreak_game",
)


def _validate_pairing_publication_times(
    pairing_rows: list[dict[str, Any]],
    *,
    require_published_at: bool,
) -> None:
    """Validate every textual pairing time before a publication write.

    ``scheduled_at`` is compared lexicographically by the dispatcher, so all
    writers must preserve the same exact naive ISO-seconds representation.
    Formal publication batches also require a durable ``published_at`` value;
    the generic low-level single-row helper keeps NULL only for historical
    corruption fixtures and validates any supplied value.
    """
    if not isinstance(pairing_rows, list):
        raise ValueError("赛事对阵批次必须是列表")
    for source in pairing_rows:
        if not isinstance(source, dict):
            raise ValueError("赛事对阵批次行类型无效")
        validate_canonical_naive_timestamp(
            source.get("published_at"),
            "赛事对阵发布时间",
            allow_none=not require_published_at,
        )
        validate_canonical_naive_timestamp(
            source.get("scheduled_at"),
            "赛事对阵计划时间",
            allow_none=True,
        )


def _stage_decision_token_value(value: Any) -> list[str]:
    """Encode SQLite values without aliases between NULL/text/numeric types."""
    if value is None:
        return ["null", ""]
    if isinstance(value, bool):
        return ["bool", "1" if value else "0"]
    if isinstance(value, int):
        return ["int", str(value)]
    if isinstance(value, float):
        return ["float", value.hex()]
    if isinstance(value, bytes):
        return ["blob", value.hex()]
    if isinstance(value, str):
        return ["text-utf8", value.encode("utf-8", "surrogatepass").hex()]
    raise ValueError("阶段决策输入包含不可编码的持久值")


def _stage_decision_input_token(
    contest: dict[str, Any],
    entries: list[dict[str, Any]],
    pairings: list[dict[str, Any]],
) -> str:
    """Hash one typed, row-bounded ranking-input projection."""

    def encoded_row(row: dict[str, Any], fields: tuple[str, ...]) -> list[Any]:
        return [
            [field, _stage_decision_token_value(row.get(field))]
            for field in fields
        ]

    payload = [
        ["contest", encoded_row(contest, _STAGE_DECISION_CONTEST_TOKEN_FIELDS)],
        [
            "entries",
            [
                encoded_row(entry, _STAGE_DECISION_ENTRY_TOKEN_FIELDS)
                for entry in entries
            ],
        ],
        [
            "pairings",
            [
                encoded_row(pairing, _STAGE_DECISION_PAIRING_TOKEN_FIELDS)
                for pairing in pairings
            ],
        ],
    ]
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _validate_expected_contest_entries_tx(
    connection: sqlite3.Connection,
    contest_id: int,
    expected_entries: list[dict[str, Any]],
) -> tuple[set[int], dict[int, int | None]]:
    """CAS one exact roster snapshot and return its active entry identities."""
    if not isinstance(expected_entries, list):
        raise ValueError("赛事结果冻结名册快照类型无效")
    expected_by_id: dict[int, tuple[Any, ...]] = {}
    expected_bots: dict[int, int | None] = {}
    seen_users: set[int] = set()
    seen_bots: set[int] = set()
    for entry in expected_entries:
        if not isinstance(entry, dict):
            raise ValueError("赛事结果冻结名册行类型无效")
        entry_id = exact_nonnegative_int(entry.get("id"))
        user_id = exact_nonnegative_int(entry.get("user_id"))
        raw_bot_id = entry.get("bot_id")
        bot_id = (
            exact_nonnegative_int(raw_bot_id)
            if raw_bot_id is not None
            else None
        )
        seed = exact_nonnegative_int(entry.get("seed", 0))
        group_id = entry.get("group_id", "")
        eliminated = exact_sqlite_bool(entry.get("eliminated", 0))
        if (
            entry_id is None
            or entry_id < 1
            or user_id is None
            or user_id < 1
            or (raw_bot_id is not None and (bot_id is None or bot_id < 1))
            or seed is None
            or not isinstance(group_id, str)
            or group_id != group_id.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in group_id)
            or eliminated is None
            or entry_id in expected_by_id
            or user_id in seen_users
            or (bot_id is not None and bot_id in seen_bots)
        ):
            raise ValueError("赛事结果冻结名册身份或状态无效")
        expected_by_id[entry_id] = (
            user_id,
            bot_id,
            seed,
            group_id,
            int(eliminated),
        )
        expected_bots[entry_id] = bot_id
        seen_users.add(user_id)
        if bot_id is not None:
            seen_bots.add(bot_id)

    durable_rows = connection.execute(
        "SELECT id,user_id,bot_id,seed,group_id,eliminated "
        "FROM contest_entries WHERE contest_id=? ORDER BY id",
        (contest_id,),
    ).fetchall()
    durable_by_id = {
        row["id"]: (
            row["user_id"],
            row["bot_id"],
            row["seed"],
            row["group_id"],
            row["eliminated"],
        )
        for row in durable_rows
    }
    if durable_by_id != expected_by_id:
        raise ValueError("赛事结果冻结名册已变化或持久值损坏")
    return (
        {
            entry_id
            for entry_id, values in expected_by_id.items()
            if values[-1] == 0
        },
        expected_bots,
    )


def _normalize_stage_result_batch(
    contest_id: int,
    stage_idx: int,
    result_rows: list[dict[str, Any]],
    *,
    expected_entry_ids: set[int] | None = None,
    expected_entry_bots: dict[int, int | None] | None = None,
    expected_stage_groups: dict[int, str] | None = None,
) -> list[tuple[Any, ...]]:
    """Normalize a stage batch; strict mode proves one complete new snapshot."""
    normalized: list[tuple[Any, ...]] = []
    seen_entries: set[int] = set()
    strict_coordinates: list[tuple[str, int, int | None]] = []
    for raw in result_rows:
        if not isinstance(raw, dict):
            raise ValueError("阶段结果行类型无效")
        entry_id = raw.get("entry_id")
        bot_id = raw.get("bot_id")
        stage_key = raw.get("stage_key", "")
        group_id = raw.get("group_id", "")
        rank_in_group = raw.get("rank_in_group")
        payload_json = raw.get("payload_json", "{}")
        if (
            isinstance(entry_id, bool)
            or not isinstance(entry_id, int)
            or entry_id < 1
            or entry_id in seen_entries
            or (
                bot_id is not None
                and (
                    isinstance(bot_id, bool)
                    or not isinstance(bot_id, int)
                    or bot_id < 1
                )
            )
            or not isinstance(stage_key, str)
            or not isinstance(group_id, str)
            or group_id != group_id.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in group_id)
            or isinstance(rank_in_group, bool)
            or not isinstance(rank_in_group, int)
            or rank_in_group < 1
            or not isinstance(payload_json, str)
        ):
            raise ValueError("阶段结果行坐标无效")
        if expected_entry_ids is not None:
            points = raw.get("points", 0)
            wins = exact_nonnegative_int(raw.get("wins", 0))
            draws = exact_nonnegative_int(raw.get("draws", 0))
            losses = exact_nonnegative_int(raw.get("losses", 0))
            delta_total = raw.get("delta_total", 0)
            try:
                payload = json.loads(
                    payload_json,
                    parse_constant=lambda value: (_ for _ in ()).throw(
                        ValueError(f"非法 JSON 常量: {value}")
                    ),
                )
            except (TypeError, ValueError) as exc:
                raise ValueError("阶段结果破同分载荷损坏") from exc
            tiebreaks = (
                sanitize_public_contest_tiebreaks(payload.get("tiebreaks"))
                if isinstance(payload, dict)
                else None
            )
            overall_rank = (
                exact_nonnegative_int(payload.get("overall_rank"))
                if isinstance(payload, dict) and "overall_rank" in payload
                else None
            )
            if (
                entry_id not in expected_entry_ids
                or expected_entry_bots is None
                or expected_entry_bots.get(entry_id) != bot_id
                or (
                    expected_stage_groups is not None
                    and expected_stage_groups.get(entry_id) != group_id
                )
                or isinstance(points, bool)
                or not isinstance(points, (int, float))
                or not math.isfinite(points)
                or wins is None
                or draws is None
                or losses is None
                or isinstance(delta_total, bool)
                or not isinstance(delta_total, int)
                or tiebreaks is None
                or tiebreaks["points"] != points
                or (group_id != "" and (overall_rank is None or overall_rank < 1))
                or (group_id == "" and overall_rank is not None)
            ):
                raise ValueError("阶段结果批次身份、积分或破同分明细无效")
            strict_coordinates.append((group_id, rank_in_group, overall_rank))
        seen_entries.add(entry_id)
        normalized.append(
            (
                contest_id,
                stage_idx,
                stage_key,
                entry_id,
                bot_id,
                raw.get("points", 0),
                raw.get("wins", 0),
                raw.get("draws", 0),
                raw.get("losses", 0),
                raw.get("delta_total", 0),
                group_id,
                rank_in_group,
                payload_json,
            )
        )

    if expected_entry_ids is not None:
        if seen_entries != expected_entry_ids:
            raise ValueError("阶段结果批次未精确覆盖权威参赛 cohort")
        if expected_stage_groups is not None and (
            set(expected_stage_groups) != expected_entry_ids
            or any(
                not isinstance(group_id, str)
                or not group_id
                or group_id != group_id.strip()
                or any(
                    ord(char) < 32 or ord(char) == 127
                    for char in group_id
                )
                for group_id in expected_stage_groups.values()
            )
        ):
            raise ValueError("阶段结果权威分组映射无效")
        grouped = [bool(group_id) for group_id, _rank, _overall in strict_coordinates]
        if any(grouped) and not all(grouped):
            raise ValueError("阶段结果不能混合分组与非分组坐标")
        if all(grouped) and grouped:
            ranks_by_group: dict[str, list[int]] = {}
            overall_ranks: set[int] = set()
            for group_id, rank_in_group, overall_rank in strict_coordinates:
                assert overall_rank is not None
                ranks_by_group.setdefault(group_id, []).append(rank_in_group)
                if overall_rank in overall_ranks:
                    raise ValueError("阶段结果全局名次重复")
                overall_ranks.add(overall_rank)
            if any(
                sorted(ranks) != list(range(1, len(ranks) + 1))
                for ranks in ranks_by_group.values()
            ) or overall_ranks != set(range(1, len(expected_entry_ids) + 1)):
                raise ValueError("阶段结果分组或全局名次不连续")
        elif {
            rank_in_group
            for _group_id, rank_in_group, _overall in strict_coordinates
        } != set(range(1, len(expected_entry_ids) + 1)):
            raise ValueError("阶段结果名次必须从 1 连续且唯一")
    return normalized


def _replace_stage_result_batch_tx(
    connection: sqlite3.Connection,
    contest_id: int,
    stage_idx: int,
    normalized: list[tuple[Any, ...]],
) -> None:
    connection.execute(
        "DELETE FROM contest_stage_results WHERE contest_id=? AND stage_idx=?",
        (contest_id, stage_idx),
    )
    for values in normalized:
        connection.execute(
            "INSERT INTO contest_stage_results"
            "(contest_id, stage_idx, stage_key, entry_id, bot_id, points, wins, "
            "draws, losses, delta_total, group_id, rank_in_group, payload_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )


def _insert_stage_result_batch_tx(
    connection: sqlite3.Connection,
    normalized: list[tuple[Any, ...]],
) -> None:
    """Install one previously absent immutable stage-decision batch."""
    for values in normalized:
        connection.execute(
            "INSERT INTO contest_stage_results"
            "(contest_id, stage_idx, stage_key, entry_id, bot_id, points, wins, "
            "draws, losses, delta_total, group_id, rank_in_group, payload_json) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            values,
        )


def _stage_result_recovery_rows(
    rows: list[sqlite3.Row] | list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Project private persisted rank coordinates from already-read rows."""
    snapshots: list[dict[str, Any]] = []
    for raw in rows:
        row = dict(raw)
        payload = _loads_json(row.pop("payload_json", None), default={})
        if not isinstance(payload, dict):
            payload = {}
        row["tiebreaks"] = sanitize_public_contest_tiebreaks(
            payload.get("tiebreaks")
        )
        overall_rank = payload.get("overall_rank")
        row["overall_rank"] = (
            overall_rank
            if isinstance(overall_rank, int)
            and not isinstance(overall_rank, bool)
            and overall_rank >= 1
            else None
        )
        snapshots.append(row)
    return snapshots


def _parse_stable_group_id(group_id: object) -> str:
    if (
        not isinstance(group_id, str)
        or not group_id
        or group_id != group_id.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in group_id)
    ):
        raise ValueError("分组标识无效")
    return group_id


def _parse_official_group_coordinates(
    group_id: object, rank_in_group: object
) -> tuple[str, int | None]:
    """Parse the inseparable group coordinates on one official result row.

    Historical/non-group rows have exactly ``('', None)``.  A grouped row must
    carry both a canonical, non-empty label and an exact 1-based integer rank.
    In particular, bool/float/string coercions and half-populated coordinates
    fail closed at every Store write/read boundary.
    """
    if group_id == "":
        if rank_in_group is not None:
            raise ValueError("正式名次分组坐标必须同时为空或同时存在")
        return "", None
    group_id = _parse_stable_group_id(group_id)
    if (
        isinstance(rank_in_group, bool)
        or not isinstance(rank_in_group, int)
        or rank_in_group < 1
    ):
        raise ValueError("正式名次组内名次必须是正整数")
    return group_id, rank_in_group


def _validate_complete_official_group_coordinates(
    rows: list[dict[str, Any] | sqlite3.Row],
    *,
    expected_entry_groups: dict[int, object] | None = None,
) -> None:
    """Validate the complete group-coordinate set of a source official table.

    A non-group or historical final is represented by ``('', None)`` on every
    row, regardless of any earlier group still stored on its roster entry.  If
    any official row is grouped, every row must instead be grouped, ranks must
    be exactly ``1..N`` within each group, and an available frozen roster must
    bind every official entry to the same group.  This source-only full-table
    gate deliberately does not reinterpret non-group finals from roster data.
    """
    parsed: list[tuple[dict[str, Any], str, int | None]] = []
    for raw in rows:
        if isinstance(raw, sqlite3.Row):
            row = dict(raw)
        elif isinstance(raw, dict):
            row = raw
        else:
            raise ValueError("来源正式榜行类型无效")
        group_id, rank_in_group = _parse_official_group_coordinates(
            row.get("group_id"), row.get("rank_in_group")
        )
        parsed.append((row, group_id, rank_in_group))

    if expected_entry_groups is not None:
        expected_entry_ids: set[int] = set()
        for entry_id in expected_entry_groups:
            if (
                isinstance(entry_id, bool)
                or not isinstance(entry_id, int)
                or entry_id < 1
            ):
                raise ValueError("来源正式榜名册 entry_id 无效")
            expected_entry_ids.add(entry_id)
        actual_entry_ids: set[int] = set()
        for row, _group_id, _rank_in_group in parsed:
            entry_id = row.get("entry_id")
            if (
                isinstance(entry_id, bool)
                or not isinstance(entry_id, int)
                or entry_id < 1
                or entry_id in actual_entry_ids
            ):
                raise ValueError("来源正式榜 entry_id 无效或重复")
            actual_entry_ids.add(entry_id)
        if actual_entry_ids != expected_entry_ids:
            raise ValueError("来源正式榜与名册成员不一致")

    grouped_flags = [group_id != "" for _row_data, group_id, _rank in parsed]
    if not any(grouped_flags):
        return
    if not all(grouped_flags):
        raise ValueError("来源正式榜不能混合分组与非分组坐标")

    ranks_by_group: dict[str, list[int]] = {}
    for row, group_id, rank_in_group in parsed:
        if rank_in_group is None:  # defensive: grouped rows are inseparable
            raise ValueError("来源正式榜分组坐标不完整")
        ranks_by_group.setdefault(group_id, []).append(rank_in_group)
        if (
            expected_entry_groups is not None
            and expected_entry_groups[row["entry_id"]] != group_id
        ):
            raise ValueError("来源正式榜分组与冻结名册不一致")
    for ranks in ranks_by_group.values():
        if sorted(ranks) != list(range(1, len(ranks) + 1)):
            raise ValueError("来源正式榜组内名次必须从 1 连续且唯一")


# 旧 contest pairing / stage snapshot 可能先于 entry 身份列存在。只在同一赛事中
# 一个 bot_id 唯一对应一个报名项时，读边界才可恢复 entry_id；0/多条保持未知。
_UNIQUE_CONTEST_ENTRY_SQL = (
    "(SELECT contest_id,bot_id,MIN(id) AS entry_id FROM contest_entries "
    "WHERE bot_id IS NOT NULL GROUP BY contest_id,bot_id HAVING COUNT(*)=1)"
)

_RESERVED_RANDOM_GROUP_TEMPLATES = frozenset(
    {"pencil_group_drr", "gomoku_seeded_group_drr_final"}
)
_RESERVED_RANDOM_GROUP_OFFICIAL_STAGE_CONTRACTS = {
    # (terminal/current stage, allowed authority stages in the merged table)
    "pencil_group_drr": (0, frozenset({0})),
    "gomoku_seeded_group_drr_final": (1, frozenset({0, 1})),
}


def _decode_official_tiebreaks(raw: object) -> dict[str, Any]:
    """Decode one persisted tie-break object without accepting JSON constants."""
    if not isinstance(raw, str):
        raise ValueError("正式名次破同分数据类型无效")

    def reject_constant(value: str) -> None:
        raise ValueError(f"非法 JSON 常量: {value}")

    try:
        decoded = json.loads(raw, parse_constant=reject_constant)
    except (TypeError, ValueError) as exc:
        raise ValueError("正式名次破同分数据损坏") from exc
    if not isinstance(decoded, dict):
        raise ValueError("正式名次破同分数据必须是对象")
    return decoded


def _validate_complete_official_results(
    rows: list[dict[str, Any] | sqlite3.Row],
    *,
    contest_id: int,
    contest: dict[str, Any],
    roster_rows: list[dict[str, Any] | sqlite3.Row],
    stage_entry_ids: dict[int, set[int]] | None = None,
    legacy_entry_groups: dict[int, str] | None = None,
) -> list[dict[str, Any]]:
    """Validate and normalize one complete ready official-result table.

    The official table is one roster-wide authority, not a collection of
    independently plausible rows.  This gate is shared by the atomic writer
    and every ready-table reader so JSON and CSV cannot disagree about damaged
    ranks, identities, group coordinates or tie-break chains.

    Historical ungrouped rows may legitimately carry an empty/partial tie
    object.  They stay readable as ``tiebreaks=None``; grouped rows are a new
    contract and therefore require the complete bounded base projection.
    """
    if (
        isinstance(contest_id, bool)
        or not isinstance(contest_id, int)
        or contest_id < 1
        or contest.get("id") != contest_id
    ):
        raise ValueError("正式名次赛事身份无效")

    roster_by_id: dict[int, dict[str, Any]] = {}
    roster_users: set[int] = set()
    for raw in roster_rows:
        roster = dict(raw) if isinstance(raw, sqlite3.Row) else raw
        if not isinstance(roster, dict):
            raise ValueError("正式名次名册行类型无效")
        entry_id = exact_nonnegative_int(roster.get("id"))
        user_id = exact_nonnegative_int(roster.get("user_id"))
        bot_id = roster.get("bot_id")
        parsed_bot_id = (
            exact_nonnegative_int(bot_id) if bot_id is not None else None
        )
        if (
            entry_id is None
            or entry_id < 1
            or user_id is None
            or user_id < 1
            or (
                bot_id is not None
                and (parsed_bot_id is None or parsed_bot_id < 1)
            )
            or entry_id in roster_by_id
            or user_id in roster_users
        ):
            raise ValueError("正式名次冻结名册身份损坏")
        roster_by_id[entry_id] = dict(roster)
        roster_users.add(user_id)
    if not roster_by_id:
        # A historical zero-participant contest has one legitimate complete
        # table: the exactly empty table.  Any persisted row would invent a
        # member outside the frozen roster.  Non-empty rosters continue through
        # the roster-wide 1..N completeness checks below.
        if rows:
            raise ValueError("空名册正式名次必须精确为空")
        return []

    normalized: list[dict[str, Any]] = []
    seen_entries: set[int] = set()
    seen_ranks: set[int] = set()
    for raw in rows:
        row = dict(raw) if isinstance(raw, sqlite3.Row) else raw
        if not isinstance(row, dict):
            raise ValueError("正式名次行类型无效")
        entry_id = exact_nonnegative_int(row.get("entry_id"))
        rank = exact_nonnegative_int(row.get("rank"))
        stage_idx = exact_nonnegative_int(row.get("stage_idx"))
        row_contest_id = exact_nonnegative_int(row.get("contest_id"))
        user_id = exact_nonnegative_int(row.get("user_id"))
        bot_id = row.get("bot_id")
        parsed_bot_id = (
            exact_nonnegative_int(bot_id) if bot_id is not None else None
        )
        points = row.get("points")
        if (
            entry_id is None
            or entry_id < 1
            or rank is None
            or rank < 1
            or stage_idx is None
            or row_contest_id != contest_id
            or user_id is None
            or user_id < 1
            or (
                bot_id is not None
                and (parsed_bot_id is None or parsed_bot_id < 1)
            )
            or isinstance(points, bool)
            or not isinstance(points, (int, float))
            or not math.isfinite(points)
            or entry_id in seen_entries
            or rank in seen_ranks
        ):
            raise ValueError("正式名次身份、阶段、名次或积分无效")
        roster = roster_by_id.get(entry_id)
        if (
            roster is None
            or roster.get("user_id") != user_id
            or roster.get("bot_id") != bot_id
        ):
            raise ValueError("正式名次与冻结名册身份不一致")
        group_id, rank_in_group = _parse_official_group_coordinates(
            row.get("group_id"), row.get("rank_in_group")
        )
        awarded = row.get("awarded")
        if not isinstance(awarded, str):
            raise ValueError("正式名次奖项字段类型无效")
        decoded_tiebreaks = _decode_official_tiebreaks(
            row.get("tiebreaks_json")
        )
        public_tiebreaks = sanitize_public_contest_tiebreaks(decoded_tiebreaks)
        cross_present = any(
            key in decoded_tiebreaks
            for key in PUBLIC_CROSS_GROUP_TIEBREAK_FIELDS
        )
        if group_id:
            if public_tiebreaks is None:
                raise ValueError("分组正式名次缺少完整破同分数据")
            if public_tiebreaks["points"] != points:
                raise ValueError("正式名次积分与破同分积分矛盾")
            if (
                cross_present
                and public_tiebreaks.get("group_rank") != rank_in_group
            ):
                raise ValueError("跨组破同分组内名次与正式坐标矛盾")
        elif cross_present:
            raise ValueError("非分组正式名次不能携带跨组破同分数据")
        elif (
            public_tiebreaks is not None
            and public_tiebreaks["points"] != points
        ):
            raise ValueError("正式名次积分与破同分积分矛盾")

        normalized_row = dict(row)
        normalized_row["group_id"] = group_id
        normalized_row["rank_in_group"] = rank_in_group
        normalized_row["tiebreaks"] = public_tiebreaks
        normalized.append(normalized_row)
        seen_entries.add(entry_id)
        seen_ranks.add(rank)

    expected_entries = set(roster_by_id)
    if seen_entries != expected_entries:
        raise ValueError("正式名次与冻结名册成员不一致")
    if seen_ranks != set(range(1, len(expected_entries) + 1)):
        raise ValueError("正式名次必须从 1 连续且唯一")

    expected_entry_groups = {
        entry_id: roster.get("group_id")
        for entry_id, roster in roster_by_id.items()
    }
    roster_grouped = [bool(group_id) for group_id in expected_entry_groups.values()]
    result_grouped = [bool(row.get("group_id")) for row in normalized]
    if (
        result_grouped
        and all(result_grouped)
        and not any(roster_grouped)
    ):
        if (
            legacy_entry_groups is None
            or set(legacy_entry_groups) != expected_entries
        ):
            raise ValueError("传统分组正式名次缺少完整外部组权威")
        expected_entry_groups = dict(legacy_entry_groups)
    _validate_complete_official_group_coordinates(
        normalized,
        expected_entry_groups=expected_entry_groups,
    )

    if contest.get("template_id") in _RESERVED_RANDOM_GROUP_TEMPLATES:
        from bzplat.backend.contests.ranking import with_official_result_provenance
        from bzplat.backend.contests.validation import (
            validated_random_group_format_snapshot,
        )

        snapshot = validated_random_group_format_snapshot(contest)
        if snapshot is None:
            raise ValueError("随机分组赛事冻结快照损坏")
        stage_contract = _RESERVED_RANDOM_GROUP_OFFICIAL_STAGE_CONTRACTS.get(
            contest.get("template_id")
        )
        if stage_contract is None:
            raise ValueError("随机分组正式名次阶段契约缺失")
        expected_current_stage, allowed_source_stages = stage_contract
        current_stage_idx = exact_nonnegative_int(contest.get("current_stage_idx"))
        if current_stage_idx != expected_current_stage or any(
            row.get("stage_idx") != expected_current_stage for row in normalized
        ):
            raise ValueError("随机分组正式名次阶段坐标损坏")
        snapshot_groups: dict[int, str] = {}
        for group_id, entry_ids in snapshot["groups"].items():
            for entry_id in entry_ids:
                snapshot_groups[entry_id] = group_id
        if set(snapshot_groups) != expected_entries or any(
            expected_entry_groups[entry_id] != group_id
            for entry_id, group_id in snapshot_groups.items()
        ):
            raise ValueError("随机分组正式名次与冻结抽签不一致")
        draw_position = {
            entry_id: index
            for index, entry_id in enumerate(snapshot["draw_order"], start=1)
        }
        provenance = with_official_result_provenance(
            contest,
            normalized,
            stage_entry_ids=stage_entry_ids or {},
        )
        if len(provenance) != len(normalized):
            raise ValueError("随机分组正式名次来源阶段损坏")
        for row in provenance:
            source_stage = exact_nonnegative_int(row.get("source_stage"))
            if source_stage is None or source_stage not in allowed_source_stages:
                raise ValueError("随机分组正式名次来源阶段损坏")
            if source_stage != 0:
                continue
            tiebreaks = row.get("tiebreaks")
            if not isinstance(tiebreaks, dict) or not all(
                key in tiebreaks
                for key in PUBLIC_CROSS_GROUP_TIEBREAK_FIELDS
            ):
                raise ValueError("随机分组正式名次缺少完整跨组破同分链")
            if (
                tiebreaks["group_rank"] != row.get("rank_in_group")
                or tiebreaks["draw_order"]
                != draw_position.get(row.get("entry_id"))
            ):
                raise ValueError("随机分组跨组破同分链与冻结抽签矛盾")

    normalized.sort(key=lambda row: row["rank"])
    return normalized


def _official_result_validation_context_tx(
    connection: sqlite3.Connection, contest_id: int
) -> tuple[
    dict[str, Any],
    list[sqlite3.Row],
    dict[int, set[int]],
    dict[int, str] | None,
]:
    """Read every authority used by the ready-table validator in one tx."""
    contest = _contest_row(
        connection.execute(
            "SELECT * FROM contests WHERE id=?", (contest_id,)
        ).fetchone()
    )
    if contest is None:
        raise ValueError("赛事不存在")
    roster_rows = connection.execute(
        "SELECT id,user_id,bot_id,group_id,seed FROM contest_entries "
        "WHERE contest_id=? ORDER BY id",
        (contest_id,),
    ).fetchall()
    stage_entry_ids: dict[int, set[int]] = {}
    stage_groups: dict[int, dict[int, str]] = {}
    invalid_group_stage: set[int] = set()
    stage_rows = connection.execute(
        "SELECT result.stage_idx,COALESCE(result.entry_id,legacy.entry_id) "
        "AS entry_id,result.group_id FROM contest_stage_results result "
        f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy "
        "ON result.entry_id IS NULL AND result.bot_id=legacy.bot_id "
        "AND result.contest_id=legacy.contest_id "
        "WHERE result.contest_id=?",
        (contest_id,),
    ).fetchall()
    for row in stage_rows:
        stage_idx = exact_nonnegative_int(row["stage_idx"])
        entry_id = exact_nonnegative_int(row["entry_id"])
        if stage_idx is None or entry_id is None or entry_id < 1:
            raise ValueError("赛事阶段成员快照损坏")
        stage_entry_ids.setdefault(stage_idx, set()).add(entry_id)
        group_id = row["group_id"]
        if group_id:
            if (
                not isinstance(group_id, str)
                or group_id != group_id.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in group_id)
                or entry_id in stage_groups.setdefault(stage_idx, {})
            ):
                invalid_group_stage.add(stage_idx)
            else:
                stage_groups[stage_idx][entry_id] = group_id

    roster_entry_ids = {int(row["id"]) for row in roster_rows}
    candidates = [
        groups
        for snapshot_stage, groups in stage_groups.items()
        if snapshot_stage not in invalid_group_stage
        and set(groups) == roster_entry_ids
    ]
    legacy_entry_groups: dict[int, str] | None = None
    if candidates and all(candidate == candidates[0] for candidate in candidates):
        legacy_entry_groups = dict(candidates[0])
    return contest, roster_rows, stage_entry_ids, legacy_entry_groups


def _normalize_official_result_input(
    contest_id: int, result_rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    if not isinstance(result_rows, list):
        raise ValueError("正式名次批次类型无效")
    normalized_input: list[dict[str, Any]] = []
    for raw in result_rows:
        if not isinstance(raw, dict):
            raise ValueError("正式名次行类型无效")
        if "contest_id" in raw and raw.get("contest_id") != contest_id:
            raise ValueError("正式名次赛事身份矛盾")
        stage_idx = exact_nonnegative_int(raw.get("stage_idx", 0))
        if stage_idx is None:
            raise ValueError("正式名次阶段坐标必须是非负整数")
        group_id, rank_in_group = _parse_official_group_coordinates(
            raw.get("group_id", ""), raw.get("rank_in_group")
        )
        normalized_input.append(
            {
                **raw,
                "contest_id": contest_id,
                "stage_idx": stage_idx,
                "points": raw.get("points", 0),
                "group_id": group_id,
                "rank_in_group": rank_in_group,
                "tiebreaks_json": raw.get("tiebreaks_json", "{}"),
                "awarded": raw.get("awarded", ""),
            }
        )
    return normalized_input


def _replace_official_result_batch_tx(
    connection: sqlite3.Connection,
    contest_id: int,
    result_rows: list[dict[str, Any]],
) -> None:
    contest, roster_rows, stage_entry_ids, legacy_entry_groups = (
        _official_result_validation_context_tx(connection, contest_id)
    )
    normalized = _validate_complete_official_results(
        _normalize_official_result_input(contest_id, result_rows),
        contest_id=contest_id,
        contest=contest,
        roster_rows=roster_rows,
        stage_entry_ids=stage_entry_ids,
        legacy_entry_groups=legacy_entry_groups,
    )
    connection.execute(
        "DELETE FROM contest_official_results WHERE contest_id=?",
        (contest_id,),
    )
    for row in normalized:
        connection.execute(
            "INSERT INTO contest_official_results"
            "(contest_id, entry_id, stage_idx, rank, points, bot_id, user_id, "
            "group_id, rank_in_group, tiebreaks_json, awarded) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                contest_id,
                row["entry_id"],
                row["stage_idx"],
                row["rank"],
                row["points"],
                row.get("bot_id"),
                row.get("user_id"),
                row["group_id"],
                row["rank_in_group"],
                row["tiebreaks_json"],
                row["awarded"],
            ),
        )


def _contest_identity_projection_sql(
    *,
    entry_alias: str = "e",
    user_alias: str = "u",
    gate_sql: str | None = None,
) -> str:
    """Private identity projection, optionally gated inside the same SQL row.

    ``gate_sql`` must be a trusted internal SQL predicate from the same SELECT
    (normally the joined contest's ``require_real_name``).  Wrapping every
    projected value prevents a stale preliminary gate read from authorizing PII
    fetched from a newer entry/profile snapshot.
    """

    def gated(expression: str) -> str:
        if gate_sql is None:
            return expression
        return f"CASE WHEN {gate_sql} THEN ({expression}) ELSE NULL END"

    raw_source = (
        f"CASE WHEN {entry_alias}.identity_source="
        f"'{CONTEST_IDENTITY_SOURCE_REGISTRATION}' "
        f"THEN '{CONTEST_IDENTITY_SOURCE_REGISTRATION}' "
        f"WHEN {entry_alias}.identity_source IS NULL "
        f"THEN '{CONTEST_IDENTITY_SOURCE_LEGACY}' ELSE NULL END"
    )
    projected: list[str] = []
    completeness_terms: list[str] = []
    for field in _CONTEST_IDENTITY_PROFILE_FIELDS:
        snapshot = f"{entry_alias}.{field}_snapshot"
        current = f"{user_alias}.{field}"
        value = (
            f"CASE WHEN {entry_alias}.identity_source="
            f"'{CONTEST_IDENTITY_SOURCE_REGISTRATION}' THEN {snapshot} "
            f"WHEN {entry_alias}.identity_source IS NULL THEN {current} "
            "ELSE NULL END"
        )
        projected.append(f"{gated(value)} AS {field}")
        completeness_terms.append(f"TRIM(COALESCE(({value}),''))<>''")
    completeness = (
        f"CASE WHEN {' AND '.join(completeness_terms)} THEN 1 ELSE 0 END"
    )
    captured_at = (
        f"CASE WHEN {entry_alias}.identity_source="
        f"'{CONTEST_IDENTITY_SOURCE_REGISTRATION}' "
        f"THEN {entry_alias}.identity_captured_at ELSE NULL END"
    )
    projected.extend(
        (
            f"{gated(raw_source)} AS identity_source",
            f"{gated(captured_at)} AS identity_captured_at",
            f"{gated(completeness)} AS identity_complete",
        )
    )
    return ", ".join(projected)


def _apply_effective_entry_ids(row: dict, *fields: tuple[str, str]) -> dict:
    for public_field, projection_field in fields:
        if row.get(public_field) is None:
            row[public_field] = row.get(projection_field)
        row.pop(projection_field, None)
    return row


def _apply_public_stage_result_payload(row: dict) -> dict:
    """Replace the private payload envelope with its bounded public fields."""
    payload = sanitize_public_stage_result_payload(row.pop("payload_json", None))
    row.update(payload)
    return row


def _delete_comment_likes_for(
    conn: sqlite3.Connection,
    where_sql: str,
    params: tuple[Any, ...],
) -> None:
    """Delete likes of comments selected by a trusted internal predicate."""
    conn.execute(
        "DELETE FROM likes WHERE target_type='comment' AND target_id IN ("
        f"SELECT CAST(id AS TEXT) FROM comments WHERE {where_sql})",
        params,
    )


def _delete_social_target(
    conn: sqlite3.Connection,
    target_type: str,
    target_id: str | int,
) -> None:
    """Remove one polymorphic target's comments/likes without leaving orphans."""
    tid = str(target_id)
    _delete_comment_likes_for(
        conn,
        "target_type=? AND target_id=?",
        (target_type, tid),
    )
    conn.execute(
        "DELETE FROM comments WHERE target_type=? AND target_id=?",
        (target_type, tid),
    )
    conn.execute(
        "DELETE FROM likes WHERE target_type=? AND target_id=?",
        (target_type, tid),
    )


def _require_active_contest_user_tx(
    conn: sqlite3.Connection, user_id: int
) -> None:
    user = conn.execute(
        "SELECT is_active FROM users WHERE id=?", (int(user_id),)
    ).fetchone()
    if user is None:
        raise ValueError(f"user {int(user_id)} 不存在")
    if not int(user["is_active"] or 0):
        raise ValueError(f"user {int(user_id)} 已停用")


def _require_live_contest_bot_tx(
    conn: sqlite3.Connection, bot_id: int
) -> None:
    live = conn.execute(
        "SELECT 1 FROM bots WHERE id=? AND owner_deleted_at IS NULL "
        "AND is_active=1",
        (int(bot_id),),
    ).fetchone()
    if live is None:
        raise ValueError("Bot 当前已停用或删除，不能加入赛事")


def _require_current_runnable_contest_bot_tx(
    conn: sqlite3.Connection, bot_id: int
) -> int | None:
    """Validate the current executable Bot mirror at the roster write point."""
    _require_live_contest_bot_tx(conn, bot_id)
    bot = conn.execute(
        "SELECT * FROM bots WHERE id=?", (int(bot_id),)
    ).fetchone()
    if bot is None:  # pragma: no cover - guarded by the same transaction above
        raise RuntimeError("Bot live guard lost its row")
    contract = _active_game_contract_tx(conn, str(bot["game_id"]))
    if (
        str(bot["binary_path"] or "").strip() == ""
        or str(bot["format"] or "") != SUPPORTED_BINARY_FORMAT
        or str(bot["os"] or "") != SUPPORTED_BINARY_OS
        or str(bot["arch"] or "") != SUPPORTED_BINARY_ARCH
        or str(bot["protocol_version"] or "") != contract["protocol_version"]
    ):
        raise ValueError("Bot 当前不可运行，不能加入赛事")
    current_version = int(bot["current_version"] or 0)
    if current_version == 0:
        if conn.execute(
            "SELECT 1 FROM bot_versions WHERE bot_id=? LIMIT 1",
            (int(bot_id),),
        ).fetchone() is not None:
            raise ValueError("Bot 当前不可运行，不能加入赛事")
        return None
    version = conn.execute(
        "SELECT * FROM bot_versions WHERE bot_id=? AND version=?",
        (int(bot_id), current_version),
    ).fetchone()
    if (
        version is None
        or version["retired_at"] is not None
        or str(version["binary_path"] or "") != str(bot["binary_path"] or "")
        or str(version["runtime_mode"] or "") != str(bot["runtime_mode"] or "")
        or str(version["protocol_version"] or "") != contract["protocol_version"]
        or str(version["format"] or "") != SUPPORTED_BINARY_FORMAT
        or str(version["os"] or "") != SUPPORTED_BINARY_OS
        or str(version["arch"] or "") != SUPPORTED_BINARY_ARCH
    ):
        raise ValueError("Bot 当前不可运行，不能加入赛事")
    version_id = exact_nonnegative_int(version["id"])
    if version_id is None or version_id < 1:  # pragma: no cover - PK invariant
        raise ValueError("Bot 当前版本身份损坏，不能加入赛事")
    return version_id


def _require_contest_bot_binding_tx(
    conn: sqlite3.Connection,
    *,
    contest_game_id: str,
    user_id: int,
    bot_id: int,
) -> None:
    """Recheck roster ownership and game identity under the writer lock."""
    bot = conn.execute(
        "SELECT owner_id,game_id FROM bots WHERE id=?", (int(bot_id),)
    ).fetchone()
    if bot is None:  # pragma: no cover - runnable guard owns missing-row wording
        raise RuntimeError("Bot runnable guard lost its row")
    if int(bot["owner_id"]) != int(user_id):
        raise ValueError(f"bot {int(bot_id)} 不属于 user {int(user_id)}")
    if str(bot["game_id"]) != str(contest_game_id):
        raise ValueError(
            f"bot {int(bot_id)} 游戏 {bot['game_id']} ≠ 赛事 {contest_game_id}"
        )


def _require_contest_without_owner_deleted_bot_tx(
    conn: sqlite3.Connection, contest_id: int
) -> None:
    """Reject a live-state transition that would publish a tombstoned Bot.

    Ordinary inactive Bots keep their established contest semantics.  This
    guard is deliberately limited to owner tombstones: a draft roster may
    retain a deleted historical identity, but it cannot cross into a live
    contest state afterwards.
    """

    deleted = conn.execute(
        "SELECT 1 FROM contest_entries entry JOIN bots b ON b.id=entry.bot_id "
        "WHERE entry.contest_id=? AND b.owner_deleted_at IS NOT NULL "
        "UNION ALL "
        "SELECT 1 FROM contest_pairings pairing JOIN bots b "
        "ON b.id=pairing.bot_a_id WHERE pairing.contest_id=? "
        "AND b.owner_deleted_at IS NOT NULL "
        "UNION ALL "
        "SELECT 1 FROM contest_pairings pairing JOIN bots b "
        "ON b.id=pairing.bot_b_id WHERE pairing.contest_id=? "
        "AND b.owner_deleted_at IS NOT NULL LIMIT 1",
        (int(contest_id), int(contest_id), int(contest_id)),
    ).fetchone()
    if deleted is not None:
        raise ValueError("赛事名册或对阵包含已删除 Bot，不能进入开放或运行状态")


def _require_live_contest_pairing_bots_tx(
    conn: sqlite3.Connection,
    contest_id: int,
    bot_a_id: int | None,
    bot_b_id: int | None,
) -> None:
    """Reject tombstoned seats only when writing into a live contest."""

    contest = conn.execute(
        "SELECT status FROM contests WHERE id=?", (int(contest_id),)
    ).fetchone()
    if contest is None:
        raise ValueError("赛事不存在")
    if contest["status"] not in (
        CONTEST_OPEN,
        CONTEST_PUBLISHED,
        CONTEST_RUNNING,
        CONTEST_REST,
    ):
        return
    bot_ids = [
        int(bot_id) for bot_id in (bot_a_id, bot_b_id) if bot_id is not None
    ]
    if not bot_ids:
        return
    marks = ",".join("?" for _ in bot_ids)
    deleted = conn.execute(
        f"SELECT 1 FROM bots WHERE id IN ({marks}) "
        "AND owner_deleted_at IS NOT NULL LIMIT 1",
        bot_ids,
    ).fetchone()
    if deleted is not None:
        raise ValueError("未结束赛事的对阵不能引用已删除 Bot")


def _delete_user_likes(conn: sqlite3.Connection, user_id: int) -> None:
    """Delete one user's likes and keep per-match cached counts exact."""
    match_ids = [
        str(row["target_id"])
        for row in conn.execute(
            "SELECT DISTINCT target_id FROM likes "
            "WHERE user_id=? AND target_type='match'",
            (user_id,),
        ).fetchall()
    ]
    conn.execute("DELETE FROM likes WHERE user_id=?", (user_id,))
    for match_id in match_ids:
        indexed = conn.execute(
            "SELECT game_id FROM matches_index WHERE id=?", (match_id,)
        ).fetchone()
        if not indexed or indexed["game_id"] not in _all_game_ids():
            continue
        table = _matches_table(indexed["game_id"])
        conn.execute(
            f"UPDATE {table} SET likes_count=(SELECT COUNT(*) FROM likes "
            "WHERE target_type='match' AND target_id=?) WHERE id=?",
            (match_id, match_id),
        )


def _contest_row(row: sqlite3.Row | None) -> dict | None:
    """返回现行赛事结构；旧表中的规则列只读忽略且不向上层暴露。"""
    result = _row(row)
    if result is not None:
        result.pop("hands_per_match", None)
        result.pop("match_config_json", None)
        # Internal manifest-integrity watermarks are never part of the manager
        # or REST contest model.  Store/claim read them directly in one SQLite
        # transaction when validating topology.
        result.pop("pairing_topology_revision", None)
        result.pop("sealed_pairing_topology_revision", None)
    return result


def _load_replay_bot_incident_events(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, str) or not raw:
        return []
    try:
        events = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(events, list):
        return []
    return [
        event
        for event in events
        if isinstance(event, dict)
        and event.get("type") in READ_TECHNICAL_INCIDENT_EVENTS
    ]


def _canonical_public_match_end(
    match: dict[str, Any], raw_events: list[Any]
) -> dict[str, Any]:
    """Project one completed row into the only public completion event."""
    result = match.get("result")
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (TypeError, ValueError):
            result = {}
    if not isinstance(result, dict):
        result = {}

    candidates: list[Any] = [result.get("deltas")]
    for event in reversed(raw_events):
        if not isinstance(event, dict) or event.get("type") != "match_end":
            continue
        candidates.extend((event.get("deltas"), event.get("final_chips")))
    deltas = [0, 0]
    for candidate in candidates:
        if not isinstance(candidate, (list, tuple)) or len(candidate) != 2:
            continue
        try:
            deltas = [int(candidate[0]), int(candidate[1])]
        except (TypeError, ValueError):
            continue
        break

    winner = match.get("winner")
    if (
        isinstance(winner, bool)
        or not isinstance(winner, int)
        or winner not in (0, 1)
    ):
        winner = None
    return {
        "type": "match_end",
        "winner": winner,
        "reason": canonical_public_completed_reason(match.get("reason")),
        "deltas": deltas,
    }


def _sanitize_public_replay_events(
    replay: dict | None,
    match: dict | None,
    *,
    human_viewer_seat: int | None = None,
) -> list[dict[str, Any]]:
    """Return one public event list with an authoritative terminal.

    This is the canonical projection used by both the legacy internal
    ``events_json`` wrapper and the public structured replay endpoint.  Keeping
    the event list structured avoids the old REST path's JSON-string-inside-JSON
    double encoding and the matching second parse in the browser.
    """
    raw_events = (replay or {}).get("events_json")
    try:
        events = json.loads(raw_events) if isinstance(raw_events, str) else []
    except (TypeError, ValueError):
        events = []
    if not isinstance(events, list):
        events = []
    redact_active_human = bool(
        match
        and match.get("match_type") == TYPE_HUMAN
        and match.get("status") in {STATUS_PENDING, STATUS_RUNNING}
    )
    authoritative = sanitize_public_match(match)
    expected_time_control = (
        authoritative.get("time_control")
        if isinstance(authoritative, dict)
        and isinstance(authoritative.get("time_control"), dict)
        else None
    )
    sanitized = sanitize_public_event_prefix(
        events,
        redact_active_human=redact_active_human,
        human_viewer_seat=human_viewer_seat,
        expected_time_control=expected_time_control,
        expected_game_id=(
            authoritative.get("game_id")
            if isinstance(authoritative, dict)
            and isinstance(authoritative.get("game_id"), str)
            else None
        ),
    )

    if authoritative is not None:
        status = authoritative.get("status")
        if status == STATUS_COMPLETED:
            sanitized.append(_canonical_public_match_end(authoritative, events))
        elif status == STATUS_ABORTED:
            sanitized.append(
                sanitize_public_event(
                    {"type": "error", "reason": authoritative.get("reason")}
                )
            )
    return sanitized


def _sanitize_public_replay(
    replay: dict | None,
    match: dict | None,
    *,
    human_viewer_seat: int | None = None,
) -> dict | None:
    """Return the internal/compat replay wrapper with authoritative terminal."""
    if replay is None and match is None:
        return None
    public = dict(replay or {})
    if match is not None:
        public.setdefault("match_id", match.get("id"))
    sanitized = _sanitize_public_replay_events(
        replay,
        match,
        human_viewer_seat=human_viewer_seat,
    )
    public["events_json"] = json.dumps(sanitized, ensure_ascii=False)
    return public


def _with_technical_incident_diagnostics(m: dict | None) -> dict | None:
    """Expose one canonical summary while reading historical incident formats.

    Old matches persisted ``bot_decide_error`` / ``bot_technical_error`` replay
    events and sometimes ``result.bot_decide_errors``. Current matches persist
    ``technical_incident`` plus ``technical_incident_*``. Legacy names are
    accepted only while reading stored history and are never emitted by current
    APIs. Persisted result counts are authoritative to avoid double-counting the
    same replay incidents.
    """
    if m is None:
        return None
    replay_events = _load_replay_bot_incident_events(
        m.pop("_replay_incident_events_json", None)
    )
    result = m.get("result")
    if not isinstance(result, dict):
        result = {}
        m["result"] = result

    counts = {0: 0, 1: 0}
    raw_counts = result.get("technical_incidents_by_seat")
    if not isinstance(raw_counts, dict):
        raw_counts = result.get("bot_decide_errors")
    if isinstance(raw_counts, dict):
        for seat in (0, 1):
            try:
                value = raw_counts.get(str(seat), raw_counts.get(seat, 0))
                counts[seat] = max(0, int(value))
            except (TypeError, ValueError):
                counts[seat] = 0
    has_persisted_counts = sum(counts.values()) > 0
    if not has_persisted_counts:
        for event in replay_events:
            sample = sanitize_public_incident(event)
            if sample is not None:
                counts[sample["seat"]] += 1

        # A bounded replay may retain fewer samples than the total result count.
        # Attribute the remainder to the first recorded terminal seat.
        try:
            total = max(0, int(result.get("technical_incident_count", 0)))
        except (TypeError, ValueError):
            total = 0
        if total > sum(counts.values()):
            raw_technical = result.get("technical_incident_samples")
            first = (
                sanitize_public_incident(raw_technical[0])
                if isinstance(raw_technical, list) and raw_technical
                else None
            )
            if first is not None:
                counts[first["seat"]] += total - sum(counts.values())

    sample_sources: list[Any] = []
    for key in ("bot_decide_error_samples", "technical_incident_samples"):
        source = result.get(key)
        if isinstance(source, list):
            sample_sources.extend(source)
    sample_sources.extend(replay_events)
    samples: list[dict[str, Any]] = []
    seen_samples: set[tuple[Any, ...]] = set()
    for raw in sample_sources:
        sample = sanitize_public_incident(raw)
        if sample is None:
            continue
        sample_key = tuple(
            sample.get(key) for key in ("reason", "code", "seat", "turn", "leg", "error")
        )
        if sample_key not in seen_samples:
            seen_samples.add(sample_key)
            samples.append(sample)
        if len(samples) == 3:
            break

    total = sum(counts.values())
    if total > 0:
        try:
            persisted_total = max(
                0, int(result.get("technical_incident_count", 0) or 0)
            )
        except (TypeError, ValueError):
            persisted_total = 0
        result["technical_incident_count"] = max(
            total, persisted_total
        )
        result["technical_incidents_by_seat"] = counts
        result["technical_incident_samples"] = samples
    else:
        result.pop("technical_incidents_by_seat", None)
        result.pop("technical_incident_samples", None)
    # Input-only legacy storage keys must never leak into the current contract.
    result.pop("bot_decide_errors", None)
    result.pop("bot_decide_error_samples", None)
    raw_technical_samples = result.get("technical_incident_samples")
    if isinstance(raw_technical_samples, list):
        safe_technical_samples = []
        for raw in raw_technical_samples[:3]:
            safe = sanitize_public_incident(raw)
            if safe is not None:
                safe_technical_samples.append(safe)
        result["technical_incident_samples"] = safe_technical_samples
    return m


def _parse_match_json_cols(m: dict | None) -> dict | None:
    """把 match 行的 match_config/result JSON 字符串列解析成 dict（消费方直接用）。

    无效/空 JSON → 空 dict。matches 表的 match_config/result 是双 JSON 通路
    （配置 + 结果详情），物理存 TEXT，逻辑是 dict——统一在此解析，避免各消费方重复 json.loads。
    """
    if m is None:
        return None
    for k in ("match_config", "result"):
        raw = m.get(k)
        if isinstance(raw, str):
            try:
                parsed = json.loads(raw)
            except (ValueError, TypeError):
                parsed = {}
                if k == "match_config":
                    m["_match_config_malformed"] = 1
            if k == "match_config" and not isinstance(parsed, dict):
                m["_match_config_malformed"] = 1
                parsed = {}
            m[k] = parsed
        elif raw is None:
            m[k] = {}
        elif k == "match_config" and not isinstance(raw, dict):
            m["_match_config_malformed"] = 1
            m[k] = {}
    return _with_technical_incident_diagnostics(m)


def _technical_incident_filter_sql(alias: str = "m") -> str:
    """SQLite JSON1 predicate covering legacy result/replay and current incidents."""
    safe_result = (
        f"CASE WHEN json_valid({alias}.result) THEN {alias}.result ELSE '{{}}' END"
    )
    safe_events = (
        "CASE WHEN json_valid(mr.events_json) THEN mr.events_json ELSE '[]' END"
    )
    return (
        "("
        f"CAST(COALESCE(json_extract({safe_result}, '$.bot_decide_errors.0'), 0) AS INTEGER) > 0 OR "
        f"CAST(COALESCE(json_extract({safe_result}, '$.bot_decide_errors.1'), 0) AS INTEGER) > 0 OR "
        f"CAST(COALESCE(json_extract({safe_result}, '$.technical_incident_count'), 0) AS INTEGER) > 0 OR "
        "EXISTS (SELECT 1 FROM match_replays mr, "
        f"json_each({safe_events}) je "
        f"WHERE mr.match_id={alias}.id AND "
        "CASE WHEN je.type='object' THEN json_extract(je.value, '$.type') END "
        f"IN ('{TECHNICAL_INCIDENT_EVENT}',"
        "'bot_decide_error','bot_technical_error')))"
    )


def _technical_incident_projection_sql(alias: str = "m") -> str:
    """Project only legacy/current incident objects from a replay JSON array.

    Match list/detail responses still need historical incident diagnostics, but
    selecting the whole replay made their cost proportional to every move in a
    match.  SQLite JSON1 filters the stored array before it crosses the DB/Python
    boundary.  Invalid JSON and non-array JSON fail closed to an empty list.
    """
    safe_events = (
        "CASE WHEN json_valid(mr.events_json) "
        "AND json_type(mr.events_json)='array' "
        "THEN mr.events_json ELSE '[]' END"
    )
    event_types = ",".join(
        f"'{event_type}'" for event_type in sorted(READ_TECHNICAL_INCIDENT_EVENTS)
    )
    return (
        "COALESCE((SELECT json_group_array(json(je.value)) "
        "FROM match_replays mr, "
        f"json_each({safe_events}) je "
        f"WHERE mr.match_id={alias}.id AND je.type='object' "
        "AND json_extract(je.value, '$.type') "
        f"IN ({event_types})), '[]')"
    )


def _contest_pairing_identity_invalid_sql(alias: str = "m") -> str:
    """SQL predicate for pairing/roster/Match identity drift.

    The expression is evaluated inside a correlated ``contest_pairings cp``
    projection.  JSON guards precede every version lookup so malformed imported
    configuration becomes a sentinel, never a SQLite ``malformed JSON`` 500.
    """
    checks: list[str] = []
    safe_stage_idx = (
        "CASE WHEN typeof(cp.stage_idx)='integer' AND cp.stage_idx>=0 "
        "THEN cp.stage_idx ELSE 0 END"
    )
    marker_path = f"'$[' || ({safe_stage_idx}) || '].series_scoring'"
    explicit_series_marker = (
        "CASE WHEN c.id IS NOT NULL AND json_valid(c.stages_json) "
        f"THEN json_extract(c.stages_json,{marker_path}) END "
        "IN ('independent_scoring_game_points_v1','aggregate_match_points_v1')"
    )
    for suffix in ("a", "b"):
        entry = f"cp.entry_{suffix}_id"
        bot = f"cp.bot_{suffix}_id"
        version = f"cp.bot_{suffix}_version_id"
        path = f"'$._bot_{suffix}_version_id'"
        checks.extend(
            (
                # Explicit series snapshots freeze entry coordinates.  Only
                # markerless legacy rows may recover a uniquely owned entry
                # from the frozen Bot id.
                f"({entry} IS NULL AND ({explicit_series_marker} OR {bot} IS NULL "
                "OR (SELECT COUNT(*) FROM contest_entries legacy_ce "
                "JOIN bots legacy_bot ON legacy_bot.id=legacy_ce.bot_id "
                f"WHERE legacy_ce.contest_id=cp.contest_id AND legacy_ce.bot_id={bot} "
                "AND legacy_ce.user_id=legacy_bot.owner_id)!=1))",
                f"({entry} IS NOT NULL AND NOT EXISTS("
                "SELECT 1 FROM contest_entries ce "
                f"WHERE ce.id={entry} AND ce.contest_id=cp.contest_id))",
                f"({entry} IS NOT NULL AND {bot} IS NOT NULL AND NOT EXISTS("
                "SELECT 1 FROM contest_entries ce JOIN bots frozen_bot "
                f"ON frozen_bot.id={bot} WHERE ce.id={entry} "
                "AND ce.contest_id=cp.contest_id "
                "AND ce.user_id=frozen_bot.owner_id))",
                f"({version} IS NOT NULL AND (NOT json_valid({alias}.match_config) "
                f"OR json_type({alias}.match_config,{path})!='integer' "
                f"OR json_extract({alias}.match_config,{path})!={version}))",
                f"({version} IS NULL AND CASE WHEN json_valid({alias}.match_config) "
                f"THEN json_type({alias}.match_config,{path}) END IS NOT NULL)",
            )
        )
    return "(" + " OR ".join(checks) + ")"


def _contest_pairing_explicit_series_marker_sql(
    pairing_alias: str = "p", contest_alias: str = "pairing_contest"
) -> str:
    """Return 1 only when the pairing's frozen stage has a known marker."""
    safe_idx = (
        f"CASE WHEN typeof({pairing_alias}.stage_idx)='integer' "
        f"AND {pairing_alias}.stage_idx>=0 THEN {pairing_alias}.stage_idx ELSE 0 END"
    )
    path = f"'$[' || ({safe_idx}) || '].series_scoring'"
    return (
        f"CASE WHEN json_valid({contest_alias}.stages_json) AND "
        f"json_extract({contest_alias}.stages_json,{path}) IN "
        "('independent_scoring_game_points_v1','aggregate_match_points_v1') "
        "THEN 1 ELSE 0 END"
    )


def _contest_stage_has_explicit_series_marker(
    stages_json: Any, stage_idx: Any
) -> bool | None:
    """Return marker presence, or ``None`` for a malformed frozen stage."""
    try:
        stages = json.loads(stages_json) if isinstance(stages_json, str) else stages_json
    except (TypeError, ValueError):
        return None
    if (
        not isinstance(stages, list)
        or isinstance(stage_idx, bool)
        or not isinstance(stage_idx, int)
        or stage_idx < 0
        or stage_idx >= len(stages)
        or not isinstance(stages[stage_idx], dict)
    ):
        return None
    marker = stages[stage_idx].get("series_scoring")
    if marker is None:
        return False
    if marker in (
        "independent_scoring_game_points_v1",
        "aggregate_match_points_v1",
    ):
        return True
    return None


def _contest_expected_duplicate_projection_sql(alias: str = "m") -> str:
    """Project a linked contest stage's scoring shape without another query.

    ``NULL`` means this Match is not linked to a contest pairing.  ``0``/``1``
    are authoritative single/duplicate expectations.  ``-1`` is deliberately
    malformed so the public outcome builder fails closed when the pairing points
    at a missing/non-object stage or a non-boolean ``duplicate`` value.
    """
    stage_path = "'$[' || cp.stage_idx || ']'"
    duplicate_path = f"({stage_path} || '.duplicate')"
    return (
        "(CASE WHEN EXISTS(SELECT 1 FROM contest_pairings linked "
        f"WHERE linked.match_id={alias}.id) THEN "
        "(SELECT CASE "
        f"WHEN {alias}.contest_id IS NULL "
        f"OR cp.contest_id!={alias}.contest_id OR c.id IS NULL "
        f"OR {alias}.match_type!='{TYPE_CONTEST}' "
        f"OR {alias}.game_id!=c.game_id "
        f"OR {alias}.bot_a_id IS NOT cp.bot_a_id "
        f"OR {alias}.bot_b_id IS NOT cp.bot_b_id "
        f"OR {_contest_pairing_identity_invalid_sql(alias)} THEN -1 "
        "WHEN typeof(cp.stage_idx)!='integer' OR cp.stage_idx<0 THEN -1 "
        "WHEN NOT json_valid(c.stages_json) THEN -1 "
        f"WHEN json_type(c.stages_json, {stage_path}) IS NOT 'object' THEN -1 "
        f"WHEN json_type(c.stages_json, {duplicate_path}) IS NULL THEN 0 "
        f"WHEN json_type(c.stages_json, {duplicate_path}) = 'true' THEN 1 "
        f"WHEN json_type(c.stages_json, {duplicate_path}) = 'false' THEN 0 "
        "ELSE -1 END "
        "FROM contest_pairings cp "
        "LEFT JOIN contests c ON c.id=cp.contest_id "
        f"WHERE cp.match_id={alias}.id "
        "ORDER BY cp.id LIMIT 1) "
        f"WHEN {alias}.contest_id IS NOT NULL THEN -1 ELSE NULL END)"
    )


def _contest_require_frozen_duplicate_projection_sql(alias: str = "m") -> str:
    """Project whether linked contest scoring uses the strict v1 contract."""
    stage_path = "'$[' || cp.stage_idx || ']'"
    scoring_path = f"({stage_path} || '.series_scoring')"
    return (
        "(CASE WHEN EXISTS(SELECT 1 FROM contest_pairings linked "
        f"WHERE linked.match_id={alias}.id) THEN "
        "(SELECT CASE "
        f"WHEN {alias}.contest_id IS NULL "
        f"OR cp.contest_id!={alias}.contest_id OR c.id IS NULL "
        f"OR {alias}.match_type!='{TYPE_CONTEST}' "
        f"OR {alias}.game_id!=c.game_id "
        f"OR {alias}.bot_a_id IS NOT cp.bot_a_id "
        f"OR {alias}.bot_b_id IS NOT cp.bot_b_id "
        f"OR {_contest_pairing_identity_invalid_sql(alias)} THEN -1 "
        "WHEN typeof(cp.stage_idx)!='integer' OR cp.stage_idx<0 THEN -1 "
        "WHEN NOT json_valid(c.stages_json) THEN -1 "
        f"WHEN json_type(c.stages_json, {stage_path}) IS NOT 'object' THEN -1 "
        f"WHEN json_type(c.stages_json, {scoring_path}) IS NULL THEN 0 "
        f"WHEN json_type(c.stages_json, {scoring_path})!='text' THEN -1 "
        f"WHEN json_extract(c.stages_json, {scoring_path})="
        "'independent_scoring_game_points_v1' THEN 1 "
        f"WHEN json_extract(c.stages_json, {scoring_path})="
        "'aggregate_match_points_v1' THEN 0 ELSE -1 END "
        "FROM contest_pairings cp "
        "LEFT JOIN contests c ON c.id=cp.contest_id "
        f"WHERE cp.match_id={alias}.id "
        "ORDER BY cp.id LIMIT 1) "
        f"WHEN {alias}.contest_id IS NOT NULL THEN -1 ELSE NULL END)"
    )


def _contest_stage_config_projection_sql(alias: str = "m") -> str:
    """Project the linked frozen stage object for full read-side validation.

    Shape/binding corruption is already represented by the sibling duplicate
    projections' ``-1`` sentinel.  For a valid coordinate this bounded JSON
    object lets the shared public builder reject damaged K/scoring/advance
    fields consistently across match list/detail and contest surfaces.
    """
    stage_path = "'$[' || cp.stage_idx || ']'"
    return (
        "(CASE WHEN EXISTS(SELECT 1 FROM contest_pairings linked "
        f"WHERE linked.match_id={alias}.id) THEN "
        "(SELECT CASE "
        f"WHEN {alias}.contest_id IS NULL "
        f"OR cp.contest_id!={alias}.contest_id OR c.id IS NULL "
        f"OR {alias}.match_type!='{TYPE_CONTEST}' "
        f"OR {alias}.game_id!=c.game_id "
        f"OR {alias}.bot_a_id IS NOT cp.bot_a_id "
        f"OR {alias}.bot_b_id IS NOT cp.bot_b_id "
        f"OR {_contest_pairing_identity_invalid_sql(alias)} THEN NULL "
        "WHEN typeof(cp.stage_idx)!='integer' OR cp.stage_idx<0 THEN NULL "
        "WHEN NOT json_valid(c.stages_json) THEN NULL "
        f"WHEN json_type(c.stages_json, {stage_path}) IS NOT 'object' THEN NULL "
        f"ELSE json_extract(c.stages_json, {stage_path}) END "
        "FROM contest_pairings cp "
        "LEFT JOIN contests c ON c.id=cp.contest_id "
        f"WHERE cp.match_id={alias}.id "
        "ORDER BY cp.id LIMIT 1) ELSE NULL END)"
    )


def match_deltas(m: dict | None) -> tuple[int, int]:
    """从 match dict 的 result JSON 取双方净筹码/胜负分（deltas）。

    matches 表收敛后结果详情存 result JSON（{"deltas":[ea,eb],...}），取代旧的
    earnings_a/earnings_b 物理列。赛事排名（ranking/manager）经此 helper 统一读取，
    避免各处重复解析 JSON + 兜底缺字段。无 result 或 deltas 缺失 → (0, 0)。
    """
    if not m:
        return (0, 0)
    deltas = (m.get("result") or {}).get("deltas")
    if isinstance(deltas, list) and len(deltas) >= 2:
        try:
            return (int(deltas[0]), int(deltas[1]))
        except (TypeError, ValueError):
            return (0, 0)
    return (0, 0)


def _paginate(
    c: sqlite3.Connection,
    base_query: str,
    params: tuple,
    *,
    page: int = 1,
    per_page: int = 50,
    count_query: str | None = None,
) -> tuple[list[dict], int]:
    """通用分页 helper：返回 (rows, total)。

    ``base_query`` 是不含 LIMIT/OFFSET 的 SELECT。默认从它派生 COUNT；
    多表读模型可传入等价的 ``count_query``，避免 COUNT 重放无关 JOIN。
    自动算 total + 加 LIMIT/OFFSET。page 从 1 开始。
    """
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or page < 1
        or isinstance(per_page, bool)
        or not isinstance(per_page, int)
        or not 1 <= per_page <= 200
    ):
        raise ValueError("分页参数无效")
    if page - 1 > ((2**63 - 1) // per_page):
        raise ValueError("分页偏移超出数据库边界")
    offset = (page - 1) * per_page
    if count_query is None:
        # total：把 SELECT ... 改成 SELECT COUNT(*)。粗略：取 FROM 之前替换。
        count_query = base_query
        # 简单启发：去掉 SELECT ... 到 FROM 之间的列，替换为 COUNT(*)
        from_idx = count_query.upper().find(" FROM ")
        if from_idx > 0:
            count_query = "SELECT COUNT(*)" + count_query[from_idx:]
        else:
            count_query = f"SELECT COUNT(*) FROM ({count_query})"
        # 去掉 ORDER BY（COUNT 不需要，且可能引用别名报错）
        ob_idx = count_query.upper().rfind(" ORDER BY ")
        if ob_idx > 0:
            count_query = count_query[:ob_idx]
    total = int(c.execute(count_query, params).fetchone()[0])
    rows = [
        _row(r) for r in c.execute(
            f"{base_query} LIMIT ? OFFSET ?", params + (per_page, offset)
        ).fetchall()
    ]
    return rows, total


def _loads_json(raw: str | None, *, default: Any) -> Any:
    """容错 JSON 解析：失败/空返回 default。"""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _contest_stage_type(stages_json: str | None, stage_idx: int) -> object:
    """Resolve one persisted stage type without guessing from keys/templates."""
    stages = _loads_json(stages_json, default=[])
    stage_idx = exact_nonnegative_int(stage_idx)
    if (
        not isinstance(stages, list)
        or stage_idx is None
        or stage_idx >= len(stages)
    ):
        return None
    stage = stages[stage_idx]
    return stage.get("type") if isinstance(stage, dict) else None


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_col(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = _table_cols(conn, table)
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def _pairing_series_fields(source: dict[str, Any]) -> tuple[int, int]:
    """Return strict durable series coordinates for one pairing write."""
    index = source.get("series_index", 1)
    size = source.get("series_size", 1)
    if (
        isinstance(index, bool)
        or not isinstance(index, int)
        or isinstance(size, bool)
        or not isinstance(size, int)
        or index < 1
        or size < 1
        or index > size
    ):
        raise ValueError("赛事对阵 series_index/series_size 必须满足 1<=index<=size")
    return index, size


def _pairing_seed_field(source: dict[str, Any], *, required: bool) -> int | None:
    """Return a strict SQLite-safe frozen seed for one pairing write."""
    seed = source.get("pairing_seed")
    if seed is None:
        if required:
            raise ValueError("多场赛事对阵必须冻结 pairing_seed")
        return None
    if (
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or seed < 1
        or seed > 9_223_372_036_854_775_807
    ):
        raise ValueError("赛事对阵 pairing_seed 必须为 SQLite 范围内的正整数")
    return seed


def _pairing_tiebreak_fields(source: dict[str, Any]) -> tuple[int, int]:
    """Return strict durable coordinates for one elimination tiebreak row."""
    group = source.get("tiebreak_group", 0)
    game = source.get("tiebreak_game", 0)
    if (
        isinstance(group, bool)
        or not isinstance(group, int)
        or isinstance(game, bool)
        or not isinstance(game, int)
        or not (
            (group == 0 and game == 0)
            or (group >= 1 and game in (1, 2))
        )
    ):
        raise ValueError(
            "淘汰决胜坐标必须为主赛 0/0 或加赛 group>=1、game=1/2"
        )
    if group and (
        source.get("bracket_slot") is None
        or source.get("bot_b_id") is None
        or source.get("entry_b_id") is None
    ):
        raise ValueError("淘汰决胜场必须绑定完整对阵与 bracket_slot")
    return group, game


def _pairing_series_participants(source: dict[str, Any]) -> tuple[int, int]:
    """Return a seat-independent participant identity for batch validation."""
    first = source.get("entry_a_id")
    second = source.get("entry_b_id")
    if first is None or second is None:
        first = source.get("bot_a_id")
        second = source.get("bot_b_id")
    if (
        isinstance(first, bool)
        or not isinstance(first, int)
        or isinstance(second, bool)
        or not isinstance(second, int)
        or first == second
    ):
        raise ValueError("多场赛事对阵必须包含两个不同的冻结参赛者")
    return tuple(sorted((first, second)))


def _pairing_series_batch(
    rows: list[dict[str, Any]],
    *,
    allow_elimination_tiebreak: bool = False,
) -> list[tuple[int, int]]:
    """Validate complete series coordinates and frozen seeds before a batch write."""
    normalized = [_pairing_series_fields(source) for source in rows]
    seeded_rows: dict[int, list[dict[str, Any]]] = {}
    series: dict[tuple[str, str, int, int], list[tuple[int, int]]] = {}
    for source, (index, size) in zip(rows, normalized):
        seed = _pairing_seed_field(source, required=size > 1)
        if seed is not None:
            seeded_rows.setdefault(seed, []).append(source)
        tiebreak_group, _tiebreak_game = _pairing_tiebreak_fields(source)
        if tiebreak_group and not allow_elimination_tiebreak:
            raise ValueError("淘汰决胜组必须通过专用原子追加接口写入")
        if size > 1:
            first, second = _pairing_series_participants(source)
            key = (
                str(source.get("stage_key") or ""),
                str(source.get("group_id") or ""),
                first,
                second,
            )
            series.setdefault(key, []).append((index, size))

    for seed, same_seed_rows in seeded_rows.items():
        if len(same_seed_rows) == 1:
            continue
        coordinates = {
            (
                row.get("stage_key") or "",
                row.get("round_num"),
                row.get("bracket_slot"),
                row.get("tiebreak_group", 0),
            )
            for row in same_seed_rows
        }
        games = {row.get("tiebreak_game", 0) for row in same_seed_rows}
        if (
            len(same_seed_rows) != 2
            or len(coordinates) != 1
            or next(iter(coordinates))[3] < 1
            or games != {1, 2}
        ):
            raise ValueError(
                "pairing_seed 只能由同一淘汰决胜组的两场换边对局共用"
            )
        first, second = same_seed_rows
        if (
            first.get("bot_a_id") != second.get("bot_b_id")
            or first.get("bot_b_id") != second.get("bot_a_id")
            or first.get("entry_a_id") != second.get("entry_b_id")
            or first.get("entry_b_id") != second.get("entry_a_id")
            or first.get("bot_a_version_id")
            != second.get("bot_b_version_id")
            or first.get("bot_b_version_id")
            != second.get("bot_a_version_id")
        ):
            raise ValueError("同 seed 淘汰决胜组必须精确交换双方座位")
    for coordinates in series.values():
        sizes = {size for _index, size in coordinates}
        if len(sizes) != 1:
            raise ValueError("同一组选手的 series_size 必须一致")
        size = next(iter(sizes))
        if sorted(index for index, _size in coordinates) != list(range(1, size + 1)):
            raise ValueError("同一组选手的 series_index 必须完整覆盖 1..series_size")
    return normalized


def _contest_entry_advancement_batch(
    rows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Strictly normalize one complete, compare-and-swap entry transition.

    Stage advancement is computed outside SQLite so pairing generation remains a
    pure operation.  Every roster identity and every field that influenced that
    computation is therefore carried back as an expected value.  The Store later
    compares this *complete* batch with the durable roster under ``BEGIN
    IMMEDIATE`` before applying any mutation.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("赛事晋级名册批次不能为空")
    required = {
        "id",
        "user_id",
        "expected_bot_id",
        "expected_group_id",
        "expected_seed",
        "expected_eliminated",
        "seed",
        "eliminated",
    }
    normalized: list[dict[str, Any]] = []
    entry_ids: set[int] = set()
    user_ids: set[int] = set()
    for source in rows:
        if not isinstance(source, dict) or set(source) != required:
            raise ValueError("赛事晋级名册批次字段不完整")
        entry_id = exact_nonnegative_int(source["id"])
        user_id = exact_nonnegative_int(source["user_id"])
        bot_id = exact_nonnegative_int(source["expected_bot_id"])
        expected_seed = exact_nonnegative_int(source["expected_seed"])
        seed = exact_nonnegative_int(source["seed"])
        expected_eliminated = exact_sqlite_bool(source["expected_eliminated"])
        eliminated = exact_sqlite_bool(source["eliminated"])
        group_id = source["expected_group_id"]
        if (
            entry_id is None
            or entry_id < 1
            or user_id is None
            or user_id < 1
            or bot_id is None
            or bot_id < 1
            or expected_seed is None
            or seed is None
            or expected_eliminated is None
            or eliminated is None
            or not isinstance(group_id, str)
            or group_id != group_id.strip()
            or any(ord(char) < 32 or ord(char) == 127 for char in group_id)
        ):
            raise ValueError("赛事晋级名册批次包含非法类型或坐标")
        if entry_id in entry_ids or user_id in user_ids:
            raise ValueError("赛事晋级名册批次包含重复身份")
        entry_ids.add(entry_id)
        user_ids.add(user_id)
        normalized.append(
            {
                "id": entry_id,
                "user_id": user_id,
                "expected_bot_id": bot_id,
                "expected_group_id": group_id,
                "expected_seed": expected_seed,
                "expected_eliminated": int(expected_eliminated),
                "seed": seed,
                "eliminated": int(eliminated),
            }
        )
    return normalized


def _apply_contest_entry_advancement_tx(
    connection: sqlite3.Connection,
    contest_id: int,
    rows: list[dict[str, Any]],
) -> None:
    """CAS and apply a complete normalized roster transition in one tx."""
    stored = connection.execute(
        "SELECT id,user_id,bot_id,group_id,seed,eliminated "
        "FROM contest_entries WHERE contest_id=? ORDER BY id",
        (contest_id,),
    ).fetchall()
    if len(stored) != len(rows):
        raise ValueError("赛事晋级期间名册已变化")
    expected_by_id = {row["id"]: row for row in rows}
    if len(expected_by_id) != len(stored):
        raise ValueError("赛事晋级名册批次不完整")
    for durable in stored:
        expected = expected_by_id.get(durable["id"])
        if (
            expected is None
            or durable["user_id"] != expected["user_id"]
            or durable["bot_id"] != expected["expected_bot_id"]
            or durable["group_id"] != expected["expected_group_id"]
            or exact_nonnegative_int(durable["seed"])
            != expected["expected_seed"]
            or exact_sqlite_bool(durable["eliminated"])
            is None
            or int(durable["eliminated"])
            != expected["expected_eliminated"]
        ):
            raise ValueError("赛事晋级期间名册身份或状态已变化")
    for expected in rows:
        changed = connection.execute(
            "UPDATE contest_entries SET seed=?,eliminated=? "
            "WHERE id=? AND contest_id=? AND user_id=? AND bot_id IS ? "
            "AND group_id=? AND seed=? AND eliminated=?",
            (
                expected["seed"],
                expected["eliminated"],
                expected["id"],
                contest_id,
                expected["user_id"],
                expected["expected_bot_id"],
                expected["expected_group_id"],
                expected["expected_seed"],
                expected["expected_eliminated"],
            ),
        )
        if changed.rowcount != 1:
            raise ValueError("赛事晋级名册 CAS 已失效")


# 每游戏对局表的建表模板（全面解耦 PR3：matches 拆三表，结构一致）。
# {suffix} = 注册 game_id；game_id 本身必须由 Store 创建入口显式写入。
_CREATE_MATCHES_TABLE_SQL = """
CREATE TABLE matches_{suffix} (
    id              TEXT    PRIMARY KEY,
    bot_a_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    bot_b_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    owner_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    contest_id      INTEGER REFERENCES contests(id) ON DELETE SET NULL,
    winner          INTEGER,
    reason          TEXT    NOT NULL DEFAULT '',
    match_type      TEXT    NOT NULL DEFAULT 'challenge',
    status          TEXT    NOT NULL DEFAULT 'pending',
    game_id         TEXT    NOT NULL,
    ruleset_version TEXT    NOT NULL DEFAULT '',
    protocol_version TEXT   NOT NULL DEFAULT '',
    rating_pool_id  TEXT    NOT NULL DEFAULT '',
    match_config    TEXT    NOT NULL DEFAULT '{{}}',  -- 内部快照 JSON（Bot 版本/duplicate）；{{}} 经 .format 转义为字面空 JSON
    result          TEXT    NOT NULL DEFAULT '{{}}',  -- 对局结果详情 JSON（rounds_played/deltas/normalized_delta）
    human_user_id   INTEGER,
    human_seat      INTEGER,
    match_seed      INTEGER,  -- P4：对局私密冻结 seed（duplicate 复现/回放用）
    technical_loss  INTEGER NOT NULL DEFAULT 0,  -- P4：技术判负标记（崩溃/超时判负但计分）
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT    NOT NULL,
    likes_count     INTEGER NOT NULL DEFAULT 0,
    views_count     INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT chk_winner_{suffix} CHECK (winner IN (0, 1) OR winner IS NULL),
    CONSTRAINT chk_status_{suffix} CHECK (status IN ('pending','running','completed','aborted')),
    CONSTRAINT chk_type_{suffix} CHECK (match_type IN ('challenge','table','contest','ladder','human'))
)
"""


def _registered_game_id(game_id: Any) -> str:
    """校验并规整必须存在的 game_id；持久化数据不得猜测默认游戏。"""
    if not isinstance(game_id, str) or not game_id.strip():
        raise ValueError("game_id 不可为空")
    gid = game_id.strip().lower()
    if gid not in _all_game_ids():
        raise ValueError(f"未知 game_id: {game_id!r}（合法: {sorted(_all_game_ids())}）")
    return gid


def _validate_contest_source_tx(
    conn: sqlite3.Connection,
    source_contest_id: Any,
    *,
    game_id: str,
    contest_id: int | None = None,
    source_owner_id: int | None = None,
    include_all_hidden: bool = False,
) -> int:
    """Validate one navigation/source edge at its SQLite write boundary."""
    source_id = exact_nonnegative_int(source_contest_id)
    if source_id is None or source_id < 1:
        raise ValueError("关联赛事 ID 必须是正整数")
    if contest_id is not None and source_id == contest_id:
        raise ValueError("赛事不能关联自身")
    if not isinstance(include_all_hidden, bool):
        raise ValueError("关联赛事隐藏态权限无效")
    source = conn.execute(
        "SELECT id,game_id,status,organizer_id FROM contests WHERE id=?", (source_id,)
    ).fetchone()
    if source is None:
        raise ValueError("关联赛事不存在")
    if _registered_game_id(source["game_id"]) != _registered_game_id(game_id):
        raise ValueError("关联赛事必须与当前赛事使用同一游戏")
    if source["status"] in (CONTEST_DRAFT, CONTEST_CANCELLED) and not include_all_hidden:
        owner_id = exact_nonnegative_int(source_owner_id)
        source_organizer_id = exact_nonnegative_int(source["organizer_id"])
        if (
            owner_id is None
            or owner_id < 1
            or source_organizer_id is None
            or source_organizer_id != owner_id
        ):
            raise ValueError("关联赛事不存在或不可见")
    return source_id


def _matches_table(game_id: str) -> str:
    """game_id → 对应的物理表名（matches_holdem/gomoku/pencil）。"""
    gid = _registered_game_id(game_id)
    return f"matches_{gid}"


def _active_game_contract_tx(
    conn: sqlite3.Connection, game_id: str
) -> dict[str, str]:
    """取当前已激活的持久化契约；缺失/空值一律 fail closed。"""
    gid = _registered_game_id(game_id)
    row = conn.execute(
        "SELECT active_pool_id,ruleset_version,protocol_version "
        "FROM rating_pool_state WHERE game_id=?",
        (gid,),
    ).fetchone()
    if row is None or any(not str(row[key] or "").strip() for key in row.keys()):
        raise RuntimeError(f"游戏 {gid} 的 active contract 缺失")
    return {
        "ruleset_version": str(row["ruleset_version"]),
        "protocol_version": str(row["protocol_version"]),
        "rating_pool_id": str(row["active_pool_id"]),
    }


def _all_game_ids() -> frozenset[str]:
    """已注册的全部 game_id（从 games 注册表派生——单一真相，审计 P1 修复）。

    延迟 import 避免循环依赖（games 包加载时 store 已可用）。
    db.py 的跨游戏聚合（UNION ALL / COUNT 遍历）须用此函数，不得硬编码
    ("holdem","gomoku","pencil")——否则新增第 4 游戏会静默漏掉所有跨游戏统计。
    """
    from bzplat.backend.games import registry as _reg

    return _reg.all_ids()


def _resolved_time_control_id(game_id: str, value: object) -> str:
    """Resolve one persisted control without guessing malformed values.

    ``None`` is the only legacy form and maps to the registered game default.
    Delayed import keeps the Store usable while the game registry is loading.
    """
    from bzplat.backend.games import registry as _reg

    if value is not None and not isinstance(value, str):
        raise ValueError("时限快照必须是稳定 ID 或历史缺省值")
    return _reg.get(_registered_game_id(game_id)).resolve_time_control(value).id


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


_RATING_PROJECTION_FIELDS = (
    "rating",
    "rd",
    "vol",
    "wins",
    "losses",
    "draws",
    "delta_total",
    "matches_played",
    "last_played_at",
)


def rating_projection_digest(
    ratings: list[dict[str, Any]],
    history: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
) -> str:
    """Hash the semantic rating projection, excluding surrogate row ids."""
    semantic = {
        "ratings": [
            {key: row.get(key) for key in ("bot_id", "game_id", *_RATING_PROJECTION_FIELDS)}
            for row in sorted(
                ratings,
                key=lambda item: (str(item.get("game_id") or ""), int(item["bot_id"])),
            )
        ],
        "rating_history": [
            {
                key: row.get(key)
                for key in (
                    "bot_id",
                    "game_id",
                    "rating",
                    "rd",
                    "vol",
                    "matches_played",
                    "reason",
                    "created_at",
                )
            }
            for row in sorted(
                history,
                key=lambda item: (
                    str(item.get("game_id") or ""),
                    int(item["bot_id"]),
                    int(item.get("matches_played") or 0),
                    str(item.get("reason") or ""),
                    str(item.get("created_at") or ""),
                    float(item.get("rating") or 0.0),
                ),
            )
        ],
        "pair_stats": [
            {
                key: row.get(key)
                for key in (
                    "bot_a_id",
                    "bot_b_id",
                    "samples",
                    "last_played_at",
                    "a_wins",
                    "a_losses",
                    "draws",
                )
            }
            for row in sorted(
                pairs,
                key=lambda item: (int(item["bot_a_id"]), int(item["bot_b_id"])),
            )
        ],
    }
    return _canonical_digest(semantic)


def rating_plan_digest(source_digest: str, bot_universe_digest: str) -> str:
    """Hash inputs that determine an offline projection rebuild plan."""
    return _canonical_digest(
        {
            "policy_version": _RATING_PROJECTION_POLICY_VERSION,
            "replay_semantics": "glicko2-settled-order-v2",
            "history_limit": 200,
            "source_digest": source_digest,
            "bot_universe_digest": bot_universe_digest,
        }
    )


def rating_source_input_issues(
    *,
    match_id: str,
    rated: Any,
    rating_reason: Any,
    result: Any,
) -> list[str]:
    """Validate the frozen replay policy/result contract for one source row."""
    issues: list[str] = []
    rated_flag = bool(int(rated or 0))
    reason = str(rating_reason or "")
    if rated_flag != (reason == "eligible"):
        issues.append(
            f"rating source rated/rating_reason mismatch: {match_id}"
        )
    if not rated_flag:
        return issues
    deltas = result.get("deltas") if isinstance(result, dict) else None
    if not isinstance(deltas, list) or len(deltas) != 2:
        issues.append(
            f"rated source deltas must contain exactly two integers: {match_id}"
        )
    elif any(type(value) is not int for value in deltas):
        issues.append(
            f"rated source deltas must be non-boolean integers: {match_id}"
        )
    elif deltas[0] + deltas[1] != 0:
        issues.append(f"rated source deltas must be zero-sum: {match_id}")
    return issues


def rating_projection_digests(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return live source/projection/plan digests plus sequence violations.

    This function is shared by the online fail-closed gate and the offline
    rebuild command so the two paths cannot silently diverge on hash semantics.
    """
    issues: list[str] = []
    active_pools = {
        str(row["game_id"]): str(row["active_pool_id"])
        for row in conn.execute(
            "SELECT game_id,active_pool_id FROM rating_pool_state"
        ).fetchall()
    }
    sentinel = conn.execute(
        "SELECT match_id,settled_at,settled_order FROM match_rating_settlements "
        "WHERE match_id=?",
        (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,),
    ).fetchall()
    if len(sentinel) != 1 or sentinel[0]["settled_order"] is None or int(
        sentinel[0]["settled_order"]
    ) != 0:
        issues.append("rating settlement sentinel must exist exactly once at order 0")

    settlements = [
        dict(row)
        for row in conn.execute(
            "SELECT match_id,settled_at,settled_order "
            "FROM match_rating_settlements WHERE match_id<>? "
            "ORDER BY settled_order,match_id",
            (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,),
        ).fetchall()
    ]
    orders = [
        int(row["settled_order"])
        for row in settlements
        if row.get("settled_order") is not None
    ]
    if len(orders) != len(settlements) or orders != list(range(1, len(orders) + 1)):
        issues.append("rating settlements must have strict contiguous order 1..N")

    policies = {
        str(row["match_id"]): dict(row)
        for row in conn.execute(
            "SELECT match_id,game_id,rating_pool_id,bot_a_id,bot_b_id,rated,"
            "rating_reason,source,classified_at,settled_order "
            "FROM match_rating_policies"
        ).fetchall()
    }
    settlement_by_id = {str(row["match_id"]): row for row in settlements}
    source_rows: list[dict[str, Any]] = []
    settled_source_rows: list[dict[str, Any]] = []
    source_ids = sorted(
        set(settlement_by_id)
        | {
            match_id
            for match_id, policy in policies.items()
            if policy.get("settled_order") is not None
        },
        key=lambda match_id: (
            int((policies.get(match_id) or {}).get("settled_order") or 0),
            match_id,
        ),
    )
    for match_id in source_ids:
        policy = policies.get(match_id)
        settlement = settlement_by_id.get(match_id)
        if policy is None:
            issues.append(f"settlement missing rating policy: {match_id}")
            source_row = {"match_id": match_id, "settlement": settlement}
            source_rows.append(source_row)
            if settlement is not None:
                settled_source_rows.append(source_row)
            continue
        policy_order = policy.get("settled_order")
        settlement_order = settlement.get("settled_order") if settlement else None
        if settlement is None:
            issues.append(f"rating policy reserved but unsettled: {match_id}")
        elif int(policy_order or -1) != int(settlement_order or -2):
            issues.append(f"rating policy/settlement order mismatch: {match_id}")
        game_id = str(policy.get("game_id") or "")
        pool_id = str(policy.get("rating_pool_id") or "")
        active_pool = active_pools.get(game_id)
        if not pool_id:
            issues.append(f"rating policy missing pool: {match_id}")
        if active_pool is None:
            issues.append(f"rating pool state missing: {game_id}")
        match_row: dict[str, Any] | None = None
        if game_id in _all_game_ids():
            raw = conn.execute(
                f"SELECT id,status,winner,result,ended_at FROM {_matches_table(game_id)} "
                "WHERE id=?",
                (match_id,),
            ).fetchone()
            match_row = dict(raw) if raw else None
        if match_row is None:
            issues.append(f"rating source match missing: {match_id}")
        else:
            try:
                match_row["result"] = json.loads(match_row.get("result") or "{}")
            except (TypeError, ValueError):
                issues.append(f"rating source result invalid: {match_id}")
        issues.extend(
            rating_source_input_issues(
                match_id=match_id,
                rated=policy.get("rated"),
                rating_reason=policy.get("rating_reason"),
                result=match_row.get("result") if match_row else None,
            )
        )
        source_row = {
            "match_id": match_id,
            "game_id": policy.get("game_id"),
            "bot_a_id": policy.get("bot_a_id"),
            "bot_b_id": policy.get("bot_b_id"),
            "rated": int(policy.get("rated") or 0),
            "rating_reason": policy.get("rating_reason"),
            "source": policy.get("source"),
            "classified_at": policy.get("classified_at"),
            "settled_order": policy_order,
            "settlement": settlement,
            "match": match_row,
        }
        # Historical pools remain immutable audit input, but only the active
        # pool contributes to the live leaderboard projection.  This is the
        # boundary that prevents a later rebuild from silently reinterpreting
        # pre-cutover games under the new ruleset.
        if pool_id == active_pool:
            source_rows.append(source_row)
            if settlement is not None:
                settled_source_rows.append(source_row)

    max_policy_order = max(
        (int(row["settled_order"]) for row in policies.values() if row.get("settled_order") is not None),
        default=0,
    )
    sequence = conn.execute(
        "SELECT next_order FROM rating_settlement_sequence WHERE singleton=1"
    ).fetchone()
    next_order = int(sequence["next_order"] or 0) if sequence else 0
    if next_order != max_policy_order + 1:
        issues.append("rating settlement sequence watermark mismatch")

    ratings = [dict(row) for row in conn.execute("SELECT * FROM ratings").fetchall()]
    history = [
        dict(row) for row in conn.execute("SELECT * FROM rating_history").fetchall()
    ]
    pairs = [dict(row) for row in conn.execute("SELECT * FROM pair_stats").fetchall()]
    # Keep this universe in exact lockstep with list_leaderboard eligibility:
    # identity/game plus every Bot visibility/binary-metadata predicate it reads.
    bots = [
        {
            "id": int(row["id"]),
            "owner_id": int(row["owner_id"]),
            "game_id": str(row["game_id"]),
            "is_active": int(row["is_active"]),
            "is_ranked": int(row["is_ranked"]),
            "format": str(row["format"]),
            "os": str(row["os"]),
            "arch": str(row["arch"]),
        }
        for row in conn.execute(
            "SELECT id,owner_id,game_id,is_active,is_ranked,format,os,arch FROM bots "
            "ORDER BY game_id,id"
        ).fetchall()
    ]
    source_digest = _canonical_digest(source_rows)
    settled_source_digest = _canonical_digest(settled_source_rows)
    bot_universe_digest = _canonical_digest(bots)
    return {
        "source_digest": source_digest,
        # A completed match reserves its immutable order before the settlement
        # marker/rating transaction.  Online mutation guards compare the stored
        # verified state with this marker-only prefix, while the public readiness
        # gate above intentionally continues to see the reserved tail as stale.
        "settled_source_digest": settled_source_digest,
        "projection_digest": rating_projection_digest(ratings, history, pairs),
        "bot_universe_digest": bot_universe_digest,
        "plan_digest": rating_plan_digest(source_digest, bot_universe_digest),
        "settled_plan_digest": rating_plan_digest(
            settled_source_digest, bot_universe_digest
        ),
        "source_settlement_count": sum(
            1 for row in settled_source_rows if row.get("settlement") is not None
        ),
        "source_last_settled_order": max(
            (
                int(row["settlement"]["settled_order"])
                for row in settled_source_rows
                if row.get("settlement") is not None
            ),
            default=0,
        ),
        "sequence_next_order": next_order,
        "issues": sorted(set(issues)),
    }


def _insert_rating_eligible_sql(alias: str) -> str:
    """Read the application-owned rating policy from a new Match payload.

    A Match row must exist before its immutable ``match_rating_policies`` row
    can reference it.  The two canonical creation paths therefore overwrite
    ``match_config._rating_eligible`` immediately before INSERT, and this
    boundary trigger consumes that boolean during the short pre-policy window.
    Missing, invalid, or non-boolean legacy payloads fail closed as rated; they
    can never bypass the overlap fence by omitting the internal field.
    """
    return (
        "(CASE WHEN "
        f"json_valid({alias}.match_config) AND "
        f"json_type({alias}.match_config,'$._rating_eligible') "
        "IN ('true','false') THEN "
        f"json_extract({alias}.match_config,'$._rating_eligible')=1 "
        "ELSE 1 END)"
    )


def _frozen_rating_eligible_sql(alias: str) -> str:
    """Read one existing Match's immutable policy, conservatively if absent."""
    return (
        "COALESCE((SELECT frozen.rated FROM match_rating_policies frozen "
        f"WHERE frozen.match_id={alias}.id),1)=1"
    )


def _normalize_schema_sql(sql: str) -> str:
    """Collapse insignificant formatting for sqlite_master comparisons."""
    return " ".join(sql.strip().rstrip(";").split())


def _ensure_trigger(
    conn: sqlite3.Connection,
    name: str,
    create_sql: str,
) -> None:
    """Install one canonical trigger without rewriting an identical schema.

    A missing trigger is created, while a stale definition is replaced inside
    the caller's migration transaction.  Any collision or post-create mismatch
    raises so that ``Store`` rolls the whole migration back instead of accepting
    a partially guarded database.
    """
    valid_name = bool(name) and name.isascii()
    valid_name = valid_name and (name[0].isalpha() or name[0] == "_")
    valid_name = valid_name and all(ch.isalnum() or ch == "_" for ch in name)
    if not valid_name:
        raise ValueError(f"invalid trigger identifier: {name!r}")

    desired = _normalize_schema_sql(create_sql)
    if not desired.startswith(f"CREATE TRIGGER {name} "):
        raise ValueError(f"trigger SQL/name mismatch: {name!r}")

    objects = conn.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=? ORDER BY type",
        (name,),
    ).fetchall()
    conflicts = sorted(str(row[0]) for row in objects if str(row[0]) != "trigger")
    if conflicts:
        raise RuntimeError(
            f"schema object name collision for trigger {name}: {conflicts}"
        )
    trigger_rows = [row for row in objects if str(row[0]) == "trigger"]
    if len(trigger_rows) > 1:
        raise RuntimeError(f"duplicate trigger definition in sqlite_master: {name}")
    if trigger_rows:
        current = _normalize_schema_sql(str(trigger_rows[0][1] or ""))
        if current == desired:
            return
        conn.execute(f"DROP TRIGGER {name}")

    conn.execute(create_sql)
    installed = conn.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?",
        (name,),
    ).fetchall()
    if (
        len(installed) != 1
        or str(installed[0][0]) != "trigger"
        or _normalize_schema_sql(str(installed[0][1] or "")) != desired
    ):
        raise RuntimeError(f"trigger verification failed after install: {name}")


def _ensure_strict_trigger(
    conn: sqlite3.Connection,
    name: str,
    create_sql: str,
) -> None:
    """Install a missing trigger, but reject any same-name schema drift."""
    desired = _normalize_schema_sql(create_sql)
    objects = conn.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=? ORDER BY type",
        (name,),
    ).fetchall()
    if not objects:
        _ensure_trigger(conn, name, create_sql)
        return
    if (
        len(objects) != 1
        or str(objects[0][0]) != "trigger"
        or _normalize_schema_sql(str(objects[0][1] or "")) != desired
    ):
        raise RuntimeError(f"canonical trigger definition mismatch: {name}")


def _ensure_strict_index(
    conn: sqlite3.Connection,
    name: str,
    create_sql: str,
) -> None:
    """Install one missing canonical index and reject same-name drift."""
    desired = _normalize_schema_sql(create_sql)
    objects = conn.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=? ORDER BY type", (name,)
    ).fetchall()
    if not objects:
        conn.execute(create_sql)
        objects = conn.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=? ORDER BY type", (name,)
        ).fetchall()
    if (
        len(objects) != 1
        or str(objects[0][0]) != "index"
        or _normalize_schema_sql(str(objects[0][1] or "")) != desired
    ):
        raise RuntimeError(f"canonical index definition mismatch: {name}")


def _contest_source_gram_insert_sql(alias: str) -> str:
    title = f"{alias}.title"
    return (
        "INSERT OR IGNORE INTO contest_source_search_grams("
        "contest_id,gram_len,gram,game_id,created_at,organizer_id,"
        "is_nonshowcase,is_protected,is_nav_public,is_nav_hidden) "
        "WITH RECURSIVE positions(pos) AS (SELECT 1 UNION ALL SELECT pos+1 "
        f"FROM positions WHERE pos<length({title})) "
        f"SELECT {alias}.id,shape.gram_len,substr({title},pos,shape.gram_len),"
        f"{alias}.game_id,{alias}.created_at,{alias}.organizer_id,"
        f"CASE WHEN {alias}.showcase_key IS NULL THEN 1 ELSE 0 END,"
        f"CASE WHEN {alias}.showcase_key IS NULL AND {alias}.status='finished' "
        f"AND typeof({alias}.official_results_ready)='integer' "
        f"AND {alias}.official_results_ready=1 THEN 1 ELSE 0 END,"
        f"CASE WHEN {alias}.showcase_key IS NULL AND {alias}.status NOT IN "
        f"('draft','cancelled') THEN 1 ELSE 0 END,"
        f"CASE WHEN {alias}.showcase_key IS NULL AND {alias}.status IN "
        f"('draft','cancelled') THEN 1 ELSE 0 END "
        "FROM positions JOIN (SELECT 1 AS gram_len UNION ALL SELECT 2 "
        "UNION ALL SELECT 3) shape "
        f"WHERE pos+shape.gram_len-1<=length({title})"
    )


_CONTEST_TITLE_EDGE_WHITESPACE_SQL = ",".join(
    f"char({codepoint})" for codepoint in CONTEST_TITLE_EDGE_WHITESPACE_CODEPOINTS
)


def _install_contest_title_schema(conn: sqlite3.Connection) -> None:
    """Reject unbounded legacy titles, then guard every future raw write."""
    edge_whitespace_sql = _CONTEST_TITLE_EDGE_WHITESPACE_SQL
    invalid = conn.execute(
        "SELECT id FROM contests WHERE typeof(title)<>'text' OR length(title)<1 "
        "OR length(title)>? OR substr(title,1,1) IN ("
        + edge_whitespace_sql
        + ") OR substr(title,-1,1) IN ("
        + edge_whitespace_sql
        + ") OR instr(title,char(0))>0 "
        "OR title GLOB '*['||char(1)||'-'||char(31)||char(127)||'-'||"
        "char(159)||']*' LIMIT 1",
        (CONTEST_TITLE_MAX_LENGTH,),
    ).fetchone()
    if invalid is not None:
        raise RuntimeError("legacy contest title violates canonical bounds")
    invalid_sql = (
        "typeof(NEW.title)<>'text' OR length(NEW.title)<1 OR "
        f"length(NEW.title)>{CONTEST_TITLE_MAX_LENGTH} OR "
        f"substr(NEW.title,1,1) IN ({edge_whitespace_sql}) OR "
        f"substr(NEW.title,-1,1) IN ({edge_whitespace_sql}) OR "
        "instr(NEW.title,char(0))>0 OR "
        "NEW.title GLOB '*['||char(1)||'-'||char(31)||char(127)||'-'||"
        "char(159)||']*'"
    )
    for name, event in (
        ("trg_contest_title_guard_insert", "INSERT ON contests"),
        ("trg_contest_title_guard_update", "UPDATE OF title ON contests"),
    ):
        _ensure_strict_trigger(
            conn,
            name,
            f"CREATE TRIGGER {name} BEFORE {event} WHEN {invalid_sql} "
            "BEGIN SELECT RAISE(ABORT,'contest title invalid'); END",
        )


def _install_contest_source_search_schema(conn: sqlite3.Connection) -> None:
    """Install and certify bounded source-candidate search projections."""
    table_rows = conn.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=? ORDER BY type",
        ("contest_source_search_grams",),
    ).fetchall()
    if len(table_rows) != 1 or str(table_rows[0][0]) != "table":
        raise RuntimeError("contest source search table definition mismatch")
    table_definition = _normalize_schema_sql(str(table_rows[0][1] or ""))
    canonical_definition = _normalize_schema_sql(
        CONTEST_SOURCE_SEARCH_GRAMS_TABLE_SQL
    )
    if table_definition != canonical_definition:
        raise RuntimeError("contest source search table definition mismatch")

    indexes = (
        ("idx_contests_source_protected", CONTEST_SOURCE_PROTECTED_INDEX_SQL),
        (
            "idx_contests_source_navigation_all",
            CONTEST_SOURCE_NAVIGATION_ALL_INDEX_SQL,
        ),
        (
            "idx_contests_source_navigation_public",
            CONTEST_SOURCE_NAVIGATION_PUBLIC_INDEX_SQL,
        ),
        (
            "idx_contests_source_navigation_owner",
            CONTEST_SOURCE_NAVIGATION_OWNER_INDEX_SQL,
        ),
        (
            "idx_contests_source_default_protected",
            CONTEST_SOURCE_DEFAULT_PROTECTED_INDEX_SQL,
        ),
        (
            "idx_contests_source_default_navigation_all",
            CONTEST_SOURCE_DEFAULT_NAVIGATION_ALL_INDEX_SQL,
        ),
        (
            "idx_contests_source_default_navigation_public",
            CONTEST_SOURCE_DEFAULT_NAVIGATION_PUBLIC_INDEX_SQL,
        ),
        (
            "idx_contests_source_default_navigation_owner",
            CONTEST_SOURCE_DEFAULT_NAVIGATION_OWNER_INDEX_SQL,
        ),
    )
    for name, sql in indexes:
        _ensure_strict_index(conn, name, sql)

    insert_name = "trg_contest_source_search_insert"
    _ensure_strict_trigger(
        conn,
        insert_name,
        f"CREATE TRIGGER {insert_name} AFTER INSERT ON contests BEGIN "
        + _contest_source_gram_insert_sql("NEW")
        + "; END",
    )
    delete_name = "trg_contest_source_search_delete"
    _ensure_strict_trigger(
        conn,
        delete_name,
        f"CREATE TRIGGER {delete_name} AFTER DELETE ON contests BEGIN "
        "DELETE FROM contest_source_search_grams WHERE contest_id=OLD.id; END",
    )
    update_name = "trg_contest_source_search_update"
    _ensure_strict_trigger(
        conn,
        update_name,
        f"CREATE TRIGGER {update_name} AFTER UPDATE OF title,game_id,created_at,"
        "organizer_id,status,official_results_ready,showcase_key ON contests BEGIN "
        "DELETE FROM contest_source_search_grams WHERE contest_id=OLD.id; "
        + _contest_source_gram_insert_sql("NEW")
        + "; END",
    )

    # A legacy database first sees an empty projection. Backfill per title so
    # work is proportional to actual title bytes/code points rather than
    # MAX(title length) multiplied by all contests.
    missing = conn.execute(
        "SELECT 1 FROM contests c WHERE length(c.title)>0 AND NOT EXISTS("
        "SELECT 1 FROM contest_source_search_grams g WHERE g.contest_id=c.id) "
        "LIMIT 1"
    ).fetchone()
    if missing:
        conn.execute(
            "INSERT OR IGNORE INTO contest_source_search_grams("
            "contest_id,gram_len,gram,game_id,created_at,organizer_id,"
            "is_nonshowcase,is_protected,is_nav_public,is_nav_hidden) "
            "WITH RECURSIVE positions(contest_id,pos) AS ("
            "SELECT id,1 FROM contests UNION ALL SELECT positions.contest_id,pos+1 "
            "FROM positions JOIN contests source ON source.id=positions.contest_id "
            "WHERE pos<length(source.title)) "
            "SELECT c.id,shape.gram_len,substr(c.title,positions.pos,shape.gram_len),"
            "c.game_id,c.created_at,c.organizer_id,"
            "CASE WHEN c.showcase_key IS NULL THEN 1 ELSE 0 END,"
            "CASE WHEN c.showcase_key IS NULL AND c.status='finished' "
            "AND typeof(c.official_results_ready)='integer' "
            "AND c.official_results_ready=1 THEN 1 ELSE 0 END,"
            "CASE WHEN c.showcase_key IS NULL AND c.status NOT IN "
            "('draft','cancelled') THEN 1 ELSE 0 END,"
            "CASE WHEN c.showcase_key IS NULL AND c.status IN "
            "('draft','cancelled') THEN 1 ELSE 0 END "
            "FROM positions JOIN contests c ON c.id=positions.contest_id "
            "JOIN (SELECT 1 AS gram_len UNION ALL SELECT 2 UNION ALL SELECT 3) shape "
            "WHERE positions.pos+shape.gram_len-1<=length(c.title) "
            "AND NOT EXISTS(SELECT 1 FROM contest_source_search_grams existing "
            "WHERE existing.contest_id=c.id)"
        )



def _install_bot_owner_delete_triggers(conn: sqlite3.Connection) -> None:
    """Give upgraded databases the same tombstone invariant as fresh schema.

    SQLite cannot add a table-level CHECK to an existing table without a risky
    rebuild.  Canonical BEFORE triggers therefore guard both creation and every
    later mutation, including direct maintenance SQL and writes from another
    application process.
    """

    insert_name = "trg_bots_owner_deleted_guard_insert"
    _ensure_trigger(
        conn,
        insert_name,
        f"CREATE TRIGGER {insert_name} BEFORE INSERT ON bots "
        "WHEN NEW.owner_deleted_at IS NOT NULL "
        "AND (NEW.is_active<>0 OR NEW.is_ranked<>0) "
        "BEGIN SELECT RAISE(ABORT, 'deleted Bot must be inactive and unranked'); END",
    )
    update_name = "trg_bots_owner_deleted_guard_update"
    _ensure_trigger(
        conn,
        update_name,
        f"CREATE TRIGGER {update_name} "
        "BEFORE UPDATE OF owner_deleted_at,is_active,is_ranked ON bots "
        "WHEN (OLD.owner_deleted_at IS NOT NULL "
        "AND NEW.owner_deleted_at IS NOT OLD.owner_deleted_at) OR "
        "(NEW.owner_deleted_at IS NOT NULL "
        "AND (NEW.is_active<>0 OR NEW.is_ranked<>0)) "
        "BEGIN SELECT RAISE(ABORT, 'deleted Bot tombstone invariant'); END",
    )


def _install_contest_entry_live_bot_triggers(conn: sqlite3.Connection) -> None:
    """Close register/delete races at the final contest roster write boundary."""

    predicate = (
        "NEW.bot_id IS NOT NULL AND NOT EXISTS("
        "SELECT 1 FROM bots live_bot WHERE live_bot.id=NEW.bot_id "
        "AND live_bot.owner_deleted_at IS NULL AND live_bot.is_active=1)"
    )
    insert_name = "trg_contest_entries_live_bot_insert"
    _ensure_trigger(
        conn,
        insert_name,
        f"CREATE TRIGGER {insert_name} BEFORE INSERT ON contest_entries "
        f"WHEN {predicate} "
        "BEGIN SELECT RAISE(ABORT, 'contest entry Bot must be active'); END",
    )
    update_name = "trg_contest_entries_live_bot_update"
    _ensure_trigger(
        conn,
        update_name,
        f"CREATE TRIGGER {update_name} BEFORE UPDATE OF bot_id ON contest_entries "
        f"WHEN {predicate} "
        "BEGIN SELECT RAISE(ABORT, 'contest entry Bot must be active'); END",
    )


def _install_contest_live_state_bot_trigger(conn: sqlite3.Connection) -> None:
    """Prevent a draft tombstone from being published by any SQL writer."""

    name = "trg_contests_live_state_deleted_bot_guard"
    deleted_reference = (
        "EXISTS(SELECT 1 FROM contest_entries entry JOIN bots b "
        "ON b.id=entry.bot_id WHERE entry.contest_id=NEW.id "
        "AND b.owner_deleted_at IS NOT NULL) OR "
        "EXISTS(SELECT 1 FROM contest_pairings pairing JOIN bots b "
        "ON b.id=pairing.bot_a_id WHERE pairing.contest_id=NEW.id "
        "AND b.owner_deleted_at IS NOT NULL) OR "
        "EXISTS(SELECT 1 FROM contest_pairings pairing JOIN bots b "
        "ON b.id=pairing.bot_b_id WHERE pairing.contest_id=NEW.id "
        "AND b.owner_deleted_at IS NOT NULL)"
    )
    _ensure_trigger(
        conn,
        name,
        f"CREATE TRIGGER {name} BEFORE UPDATE OF status ON contests "
        "WHEN NEW.status IN ('open','published','running','rest') AND ("
        f"{deleted_reference}) "
        "BEGIN SELECT RAISE(ABORT, "
        "'live contest cannot reference owner-deleted Bot'); END",
    )

    live_contest = (
        "EXISTS(SELECT 1 FROM contests c WHERE c.id=NEW.contest_id "
        "AND c.status IN ('open','published','running','rest'))"
    )
    deleted_seat = (
        "((NEW.bot_a_id IS NOT NULL AND EXISTS(SELECT 1 FROM bots a "
        "WHERE a.id=NEW.bot_a_id AND a.owner_deleted_at IS NOT NULL)) OR "
        "(NEW.bot_b_id IS NOT NULL AND EXISTS(SELECT 1 FROM bots b "
        "WHERE b.id=NEW.bot_b_id AND b.owner_deleted_at IS NOT NULL)))"
    )
    pairing_insert = "trg_contest_pairings_live_bot_insert"
    _ensure_trigger(
        conn,
        pairing_insert,
        f"CREATE TRIGGER {pairing_insert} BEFORE INSERT ON contest_pairings "
        f"WHEN {live_contest} AND {deleted_seat} "
        "BEGIN SELECT RAISE(ABORT, "
        "'live contest pairing cannot reference owner-deleted Bot'); END",
    )
    pairing_update = "trg_contest_pairings_live_bot_update"
    _ensure_trigger(
        conn,
        pairing_update,
        f"CREATE TRIGGER {pairing_update} "
        "BEFORE UPDATE OF contest_id,bot_a_id,bot_b_id ON contest_pairings "
        f"WHEN {live_contest} AND {deleted_seat} "
        "BEGIN SELECT RAISE(ABORT, "
        "'live contest pairing cannot reference owner-deleted Bot'); END",
    )


_CONTEST_PAIRING_REVISION_TRIGGER_NAMES = (
    "trg_contest_pairing_topology_insert",
    "trg_contest_pairing_topology_delete",
    "trg_contest_pairing_topology_update",
    "trg_contest_pairing_topology_stage_cursor",
    "trg_contest_pairing_topology_manifest",
)
_CONTEST_LIFECYCLE_REVISION_TRIGGER_NAMES = (
    "trg_contest_lifecycle_revision_update",
    "trg_contest_entries_lifecycle_revision_insert",
    "trg_contest_entries_lifecycle_revision_delete",
    "trg_contest_entries_lifecycle_revision_update",
    "trg_contest_stage_results_lifecycle_revision_insert",
    "trg_contest_stage_results_lifecycle_revision_delete",
    "trg_contest_stage_results_lifecycle_revision_update",
)


def _install_contest_pairing_topology_triggers(
    conn: sqlite3.Connection,
) -> None:
    """Keep sealed pairing manifests cheap to validate at claim time.

    Cardinality scans and active-job anti-joins are appropriate at publication,
    append and terminal boundaries, but doing them for every claim makes a full
    round robin quadratic in the already-quadratic number of matches.  These
    triggers turn every topology mutation into one durable revision write and
    reject any newly-created contest job whose pairing reference is already an
    orphan.  Pairing identity and tournament coordinates advance the revision;
    ordinary progress fields (status, match_id and scheduled_at) deliberately
    do not.
    """

    insert_name = _CONTEST_PAIRING_REVISION_TRIGGER_NAMES[0]
    _ensure_strict_trigger(
        conn,
        insert_name,
        f"CREATE TRIGGER {insert_name} AFTER INSERT ON contest_pairings "
        "BEGIN UPDATE contests SET pairing_topology_revision="
        "pairing_topology_revision+1 WHERE id=NEW.contest_id; END",
    )
    delete_name = _CONTEST_PAIRING_REVISION_TRIGGER_NAMES[1]
    _ensure_strict_trigger(
        conn,
        delete_name,
        f"CREATE TRIGGER {delete_name} AFTER DELETE ON contest_pairings "
        "BEGIN UPDATE contests SET pairing_topology_revision="
        "pairing_topology_revision+1 WHERE id=OLD.contest_id; END",
    )
    update_name = _CONTEST_PAIRING_REVISION_TRIGGER_NAMES[2]
    _ensure_strict_trigger(
        conn,
        update_name,
        f"CREATE TRIGGER {update_name} "
        "AFTER UPDATE OF id,contest_id,round_num,entry_a_id,entry_b_id,"
        "bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id,stage_idx,"
        "stage_key,group_id,bracket_slot,color_first,series_index,series_size,"
        "tiebreak_group,tiebreak_game,pairing_seed,published_at "
        "ON contest_pairings "
        "WHEN OLD.id IS NOT NEW.id OR OLD.contest_id IS NOT NEW.contest_id "
        "OR OLD.round_num IS NOT NEW.round_num "
        "OR OLD.entry_a_id IS NOT NEW.entry_a_id "
        "OR OLD.entry_b_id IS NOT NEW.entry_b_id "
        "OR OLD.bot_a_id IS NOT NEW.bot_a_id "
        "OR OLD.bot_b_id IS NOT NEW.bot_b_id "
        "OR OLD.bot_a_version_id IS NOT NEW.bot_a_version_id "
        "OR OLD.bot_b_version_id IS NOT NEW.bot_b_version_id "
        "OR OLD.stage_idx IS NOT NEW.stage_idx "
        "OR OLD.stage_key IS NOT NEW.stage_key "
        "OR OLD.group_id IS NOT NEW.group_id "
        "OR OLD.bracket_slot IS NOT NEW.bracket_slot "
        "OR OLD.color_first IS NOT NEW.color_first "
        "OR OLD.series_index IS NOT NEW.series_index "
        "OR OLD.series_size IS NOT NEW.series_size "
        "OR OLD.tiebreak_group IS NOT NEW.tiebreak_group "
        "OR OLD.tiebreak_game IS NOT NEW.tiebreak_game "
        "OR OLD.pairing_seed IS NOT NEW.pairing_seed "
        "OR OLD.published_at IS NOT NEW.published_at "
        "BEGIN UPDATE contests SET pairing_topology_revision="
        "pairing_topology_revision+1 "
        "WHERE id=OLD.contest_id OR id=NEW.contest_id; END",
    )
    cursor_name = _CONTEST_PAIRING_REVISION_TRIGGER_NAMES[3]
    _ensure_strict_trigger(
        conn,
        cursor_name,
        f"CREATE TRIGGER {cursor_name} "
        "AFTER UPDATE OF current_stage_idx ON contests "
        "WHEN OLD.current_stage_idx IS NOT NEW.current_stage_idx "
        "BEGIN UPDATE contests SET pairing_topology_revision="
        "pairing_topology_revision+1 WHERE id=NEW.id; END",
    )
    manifest_name = _CONTEST_PAIRING_REVISION_TRIGGER_NAMES[4]
    _ensure_strict_trigger(
        conn,
        manifest_name,
        f"CREATE TRIGGER {manifest_name} "
        "AFTER UPDATE OF published_stage_pairing_count ON contests "
        "WHEN OLD.published_stage_pairing_count "
        "IS NOT NEW.published_stage_pairing_count "
        "BEGIN UPDATE contests SET pairing_topology_revision="
        "pairing_topology_revision+1 WHERE id=NEW.id; END",
    )

    invalid_contest_ref = (
        "NEW.source='contest' AND (NEW.contest_id IS NULL "
        "OR NEW.contest_pairing_id IS NULL OR NOT EXISTS("
        "SELECT 1 FROM contest_pairings pairing "
        "JOIN contests contest ON contest.id=pairing.contest_id "
        "WHERE pairing.id=NEW.contest_pairing_id "
        "AND pairing.contest_id=NEW.contest_id "
        "AND typeof(contest.current_stage_idx)='integer' "
        "AND contest.current_stage_idx>=0 "
        "AND pairing.stage_idx=contest.current_stage_idx))"
    )
    job_insert_name = "trg_execution_contest_pairing_ref_insert"
    _ensure_strict_trigger(
        conn,
        job_insert_name,
        f"CREATE TRIGGER {job_insert_name} BEFORE INSERT ON execution_jobs "
        f"WHEN {invalid_contest_ref} "
        "BEGIN SELECT RAISE(ABORT, "
        "'contest execution job must reference its contest pairing'); END",
    )
    job_update_name = "trg_execution_contest_pairing_ref_update"
    _ensure_strict_trigger(
        conn,
        job_update_name,
        f"CREATE TRIGGER {job_update_name} "
        "BEFORE UPDATE OF source,contest_id,contest_pairing_id ON execution_jobs "
        f"WHEN {invalid_contest_ref} "
        "BEGIN SELECT RAISE(ABORT, "
        "'contest execution job must reference its contest pairing'); END",
    )


def _install_contest_lifecycle_revision_triggers(
    conn: sqlite3.Connection,
) -> None:
    """Extend the pairing revision into a complete lifecycle decision epoch.

    The seal is consumed as one proof for the current stage graph, roster,
    frozen format and persisted ranking decision.  Every mutation of those
    inputs therefore advances the same monotonic revision.  Progress-only
    fields (pairing status/match binding, entry dispatch timestamps and PII
    snapshots), rest scheduling and the downstream official-results readiness
    projection remain outside the epoch.
    """

    definitions = (
        (
            "trg_contest_lifecycle_revision_update",
            "CREATE TRIGGER trg_contest_lifecycle_revision_update "
            "AFTER UPDATE OF game_id,template_id,stages_json,format_snapshot_json,"
            "source_contest_id,status "
            "ON contests WHEN OLD.game_id IS NOT NEW.game_id "
            "OR OLD.template_id IS NOT NEW.template_id "
            "OR OLD.stages_json IS NOT NEW.stages_json "
            "OR OLD.format_snapshot_json IS NOT NEW.format_snapshot_json "
            "OR OLD.source_contest_id IS NOT NEW.source_contest_id "
            "OR (OLD.status IS NOT NEW.status AND ("
            "OLD.status IN ('rest','finished') "
            "OR NEW.status IN ('rest','finished'))) "
            "BEGIN UPDATE contests SET pairing_topology_revision="
            "pairing_topology_revision+1 WHERE id=NEW.id; END",
        ),
        (
            "trg_contest_entries_lifecycle_revision_insert",
            "CREATE TRIGGER trg_contest_entries_lifecycle_revision_insert "
            "AFTER INSERT ON contest_entries BEGIN UPDATE contests SET "
            "pairing_topology_revision=pairing_topology_revision+1 "
            "WHERE id=NEW.contest_id; END",
        ),
        (
            "trg_contest_entries_lifecycle_revision_delete",
            "CREATE TRIGGER trg_contest_entries_lifecycle_revision_delete "
            "AFTER DELETE ON contest_entries BEGIN UPDATE contests SET "
            "pairing_topology_revision=pairing_topology_revision+1 "
            "WHERE id=OLD.contest_id; END",
        ),
        (
            "trg_contest_entries_lifecycle_revision_update",
            "CREATE TRIGGER trg_contest_entries_lifecycle_revision_update "
            "AFTER UPDATE OF id,contest_id,user_id,bot_id,group_id,seed,eliminated "
            "ON contest_entries WHEN OLD.id IS NOT NEW.id "
            "OR OLD.contest_id IS NOT NEW.contest_id "
            "OR OLD.user_id IS NOT NEW.user_id "
            "OR OLD.bot_id IS NOT NEW.bot_id "
            "OR OLD.group_id IS NOT NEW.group_id "
            "OR OLD.seed IS NOT NEW.seed "
            "OR OLD.eliminated IS NOT NEW.eliminated "
            "BEGIN UPDATE contests SET pairing_topology_revision="
            "pairing_topology_revision+1 "
            "WHERE id=OLD.contest_id OR id=NEW.contest_id; END",
        ),
        (
            "trg_contest_stage_results_lifecycle_revision_insert",
            "CREATE TRIGGER trg_contest_stage_results_lifecycle_revision_insert "
            "AFTER INSERT ON contest_stage_results BEGIN UPDATE contests SET "
            "pairing_topology_revision=pairing_topology_revision+1 "
            "WHERE id=NEW.contest_id; END",
        ),
        (
            "trg_contest_stage_results_lifecycle_revision_delete",
            "CREATE TRIGGER trg_contest_stage_results_lifecycle_revision_delete "
            "AFTER DELETE ON contest_stage_results BEGIN UPDATE contests SET "
            "pairing_topology_revision=pairing_topology_revision+1 "
            "WHERE id=OLD.contest_id; END",
        ),
        (
            "trg_contest_stage_results_lifecycle_revision_update",
            "CREATE TRIGGER trg_contest_stage_results_lifecycle_revision_update "
            "AFTER UPDATE OF id,contest_id,stage_idx,stage_key,entry_id,bot_id,"
            "points,wins,draws,losses,delta_total,group_id,rank_in_group,payload_json "
            "ON contest_stage_results WHEN OLD.id IS NOT NEW.id "
            "OR OLD.contest_id IS NOT NEW.contest_id "
            "OR OLD.stage_idx IS NOT NEW.stage_idx "
            "OR OLD.stage_key IS NOT NEW.stage_key "
            "OR OLD.entry_id IS NOT NEW.entry_id "
            "OR OLD.bot_id IS NOT NEW.bot_id "
            "OR OLD.points IS NOT NEW.points "
            "OR OLD.wins IS NOT NEW.wins "
            "OR OLD.draws IS NOT NEW.draws "
            "OR OLD.losses IS NOT NEW.losses "
            "OR OLD.delta_total IS NOT NEW.delta_total "
            "OR OLD.group_id IS NOT NEW.group_id "
            "OR OLD.rank_in_group IS NOT NEW.rank_in_group "
            "OR OLD.payload_json IS NOT NEW.payload_json "
            "BEGIN UPDATE contests SET pairing_topology_revision="
            "pairing_topology_revision+1 "
            "WHERE id=OLD.contest_id OR id=NEW.contest_id; END",
        ),
    )
    if (
        tuple(name for name, _sql in definitions)
        != _CONTEST_LIFECYCLE_REVISION_TRIGGER_NAMES
    ):
        raise RuntimeError("contest lifecycle trigger registry drift")
    for name, sql in definitions:
        _ensure_strict_trigger(conn, name, sql)


def _invalidate_contest_seals_for_incomplete_lifecycle_epoch(
    conn: sqlite3.Connection,
) -> None:
    """Invalidate old seals before installing any missing epoch trigger.

    Trigger membership is versioned fail-closed.  A same-name definition
    mismatch is rejected later by ``_ensure_strict_trigger``; because both
    helpers run in the surrounding migration transaction, that failure rolls
    this invalidation and every partial trigger installation back together.
    """
    existing = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
    }
    required = {
        *_CONTEST_PAIRING_REVISION_TRIGGER_NAMES,
        *_CONTEST_LIFECYCLE_REVISION_TRIGGER_NAMES,
    }
    if not required.issubset(existing):
        conn.execute(
            "UPDATE contests SET sealed_pairing_topology_revision=NULL "
            "WHERE sealed_pairing_topology_revision IS NOT NULL"
        )


def _install_rated_overlap_triggers(conn: sqlite3.Connection) -> None:
    """Enforce one rating-bearing lifecycle per Bot at the SQLite boundary."""
    for gid in sorted(_all_game_ids()):
        table = _matches_table(gid)
        inserted_rated = _insert_rating_eligible_sql("NEW")
        updated_rated = _frozen_rating_eligible_sql("OLD")
        overlap = (
            f"SELECT 1 FROM {table} m "
            "LEFT JOIN match_rating_policies policy ON policy.match_id=m.id "
            "WHERE m.id<>NEW.id AND COALESCE(policy.rated,1)=1 "
            "AND (m.bot_a_id IN (NEW.bot_a_id,NEW.bot_b_id) "
            "OR m.bot_b_id IN (NEW.bot_a_id,NEW.bot_b_id)) "
            "AND (m.status IN ('pending','running') OR "
            "(m.status='completed' AND NOT EXISTS ("
            "SELECT 1 FROM match_rating_settlements settled "
            "WHERE settled.match_id=m.id))) LIMIT 1"
        )
        insert_name = f"trg_{table}_rated_overlap_insert"
        _ensure_trigger(
            conn,
            insert_name,
            f"CREATE TRIGGER {insert_name} "
            f"BEFORE INSERT ON {table} "
            "WHEN NEW.status IN ('pending','running') "
            f"AND ({inserted_rated}) AND EXISTS ({overlap}) "
            "BEGIN SELECT RAISE(ABORT, 'rated match lifecycle overlap'); END",
        )
        update_name = f"trg_{table}_rated_overlap_update"
        _ensure_trigger(
            conn,
            update_name,
            f"CREATE TRIGGER {update_name} "
            f"BEFORE UPDATE OF bot_a_id,bot_b_id,match_type,status ON {table} "
            "WHEN NEW.status IN ('pending','running') "
            f"AND ({updated_rated}) AND EXISTS ({overlap}) "
            "BEGIN SELECT RAISE(ABORT, 'rated match lifecycle overlap'); END",
        )


def _install_rating_source_guards(conn: sqlite3.Connection) -> None:
    """Make every settled replay input immutable at the SQLite boundary."""
    _ensure_trigger(
        conn,
        "trg_match_rating_policy_source_immutable",
        "CREATE TRIGGER trg_match_rating_policy_source_immutable "
        "BEFORE UPDATE OF match_id,game_id,rating_pool_id,bot_a_id,bot_b_id,rated,rating_reason,source,"
        "classified_at ON match_rating_policies WHEN "
        "OLD.match_id IS NOT NEW.match_id OR OLD.game_id IS NOT NEW.game_id OR "
        "OLD.rating_pool_id IS NOT NEW.rating_pool_id OR "
        "OLD.bot_a_id IS NOT NEW.bot_a_id OR "
        "OLD.bot_b_id IS NOT NEW.bot_b_id OR OLD.rated IS NOT NEW.rated OR "
        "OLD.rating_reason IS NOT NEW.rating_reason OR OLD.source IS NOT NEW.source OR "
        "OLD.classified_at IS NOT NEW.classified_at BEGIN "
        "SELECT RAISE(ABORT,'rating policy source immutable'); END",
    )
    _ensure_trigger(
        conn,
        "trg_match_rating_policy_settled_delete",
        "CREATE TRIGGER trg_match_rating_policy_settled_delete "
        "BEFORE DELETE ON match_rating_policies WHEN OLD.settled_order IS NOT NULL OR "
        "EXISTS(SELECT 1 FROM match_rating_settlements s WHERE s.match_id=OLD.match_id) "
        "BEGIN SELECT RAISE(ABORT,'settled rating policy immutable'); END",
    )
    for gid in sorted(_all_game_ids()):
        table = _matches_table(gid)
        update_name = f"trg_{table}_rating_source_update"
        _ensure_trigger(
            conn,
            update_name,
            f"CREATE TRIGGER {update_name} "
            f"BEFORE UPDATE OF id,winner,result,ended_at,status ON {table} WHEN "
            "EXISTS(SELECT 1 FROM match_rating_settlements s WHERE s.match_id=OLD.id) "
            "AND (OLD.id IS NOT NEW.id OR OLD.winner IS NOT NEW.winner OR "
            "OLD.result IS NOT NEW.result OR "
            "OLD.ended_at IS NOT NEW.ended_at OR OLD.status IS NOT NEW.status) "
            "BEGIN SELECT RAISE(ABORT,'settled match rating source immutable'); END",
        )
        delete_name = f"trg_{table}_rating_source_delete"
        _ensure_trigger(
            conn,
            delete_name,
            f"CREATE TRIGGER {delete_name} BEFORE DELETE ON {table} "
            "WHEN EXISTS(SELECT 1 FROM match_rating_settlements s "
            "WHERE s.match_id=OLD.id) BEGIN "
            "SELECT RAISE(ABORT,'settled match rating source immutable'); END",
        )


def _install_rating_projection_mutation_triggers(
    conn: sqlite3.Connection,
) -> None:
    """Persist whether every digest-input mutation followed a trusted guard."""
    bump = (
        "UPDATE rating_projection_state SET "
        "mutation_revision=mutation_revision+1 WHERE singleton=1;"
    )
    simple_tables = ("ratings", "rating_history", "pair_stats")
    for table in simple_tables:
        for operation in ("INSERT", "UPDATE", "DELETE"):
            name = f"trg_{table}_projection_mutation_{operation.lower()}"
            _ensure_trigger(
                conn,
                name,
                f"CREATE TRIGGER {name} AFTER {operation} ON {table} "
                f"BEGIN {bump} END",
            )

    _ensure_trigger(
        conn,
        "trg_bots_projection_mutation_insert",
        "CREATE TRIGGER trg_bots_projection_mutation_insert AFTER INSERT ON bots "
        f"BEGIN {bump} END",
    )
    _ensure_trigger(
        conn,
        "trg_bots_projection_mutation_delete",
        "CREATE TRIGGER trg_bots_projection_mutation_delete AFTER DELETE ON bots "
        f"BEGIN {bump} END",
    )
    _ensure_trigger(
        conn,
        "trg_bots_projection_mutation_update",
        "CREATE TRIGGER trg_bots_projection_mutation_update "
        "AFTER UPDATE OF owner_id,game_id,is_active,is_ranked,format,os,arch "
        "ON bots WHEN OLD.owner_id IS NOT NEW.owner_id OR "
        "OLD.game_id IS NOT NEW.game_id OR OLD.is_active IS NOT NEW.is_active OR "
        "OLD.is_ranked IS NOT NEW.is_ranked OR "
        "OLD.format IS NOT NEW.format OR OLD.os IS NOT NEW.os OR "
        f"OLD.arch IS NOT NEW.arch BEGIN {bump} END",
    )

    _ensure_trigger(
        conn,
        "trg_match_rating_policy_projection_mutation_order",
        "CREATE TRIGGER trg_match_rating_policy_projection_mutation_order "
        "AFTER UPDATE OF settled_order ON match_rating_policies WHEN "
        "OLD.settled_order IS NOT NEW.settled_order "
        f"BEGIN {bump} END",
    )
    _ensure_trigger(
        conn,
        "trg_rating_settlement_sequence_projection_mutation",
        "CREATE TRIGGER trg_rating_settlement_sequence_projection_mutation "
        "AFTER UPDATE OF next_order ON rating_settlement_sequence WHEN "
        "OLD.next_order IS NOT NEW.next_order "
        f"BEGIN {bump} END",
    )
    _ensure_trigger(
        conn,
        "trg_match_rating_settlement_projection_mutation_insert",
        "CREATE TRIGGER trg_match_rating_settlement_projection_mutation_insert "
        "AFTER INSERT ON match_rating_settlements WHEN NEW.settled_order>0 "
        f"BEGIN {bump} END",
    )
    for game_id in sorted(_all_game_ids()):
        table = _matches_table(game_id)
        name = f"trg_{table}_projection_mutation_source"
        _ensure_trigger(
            conn,
            name,
            f"CREATE TRIGGER {name} AFTER UPDATE OF id,winner,result,ended_at,status "
            f"ON {table} WHEN EXISTS(SELECT 1 FROM match_rating_policies policy "
            "WHERE policy.match_id=OLD.id AND policy.settled_order IS NOT NULL) "
            "AND (OLD.id IS NOT NEW.id OR OLD.winner IS NOT NEW.winner OR "
            "OLD.result IS NOT NEW.result OR OLD.ended_at IS NOT NEW.ended_at OR "
            f"OLD.status IS NOT NEW.status) BEGIN {bump} END",
        )


def _bootstrap_auto_match_fairness(conn: sqlite3.Connection) -> None:
    """Seed auto-only service counters once from canonical completed ladders.

    The retired daily-claim table carried quota accounting, not a trustworthy
    fairness projection.  Completed system-owned ``ladder`` matches are the
    durable source.  Foreground challenges never enter these counters.
    """
    state = conn.execute(
        "SELECT bootstrap_version,revision FROM auto_match_fair_state "
        "WHERE singleton=1"
    ).fetchone()
    if state is None:
        raise RuntimeError("auto_match_fair_state singleton missing")
    if int(state["bootstrap_version"] or 0) >= _AUTO_MATCH_FAIR_BOOTSTRAP_VERSION:
        return

    history: list[dict[str, Any]] = []
    game_ids = sorted(_all_game_ids())
    existing_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for gid in game_ids:
        table = _matches_table(gid)
        # Keep the post-migration registry/table assertion authoritative.  A
        # partially registered game must fail with that explicit diagnostic,
        # rather than an incidental ``no such table`` while bootstrapping the
        # unrelated automatic-fairness projection.
        if table not in existing_tables:
            continue
        rows = conn.execute(
            f"SELECT m.id,m.game_id,m.bot_a_id,m.bot_b_id,m.created_at,m.ended_at,"
            "a.owner_id AS owner_a_id,b.owner_id AS owner_b_id "
            f"FROM {table} m JOIN bots a ON a.id=m.bot_a_id "
            "JOIN bots b ON b.id=m.bot_b_id "
            "WHERE m.status=? AND m.match_type=? AND m.owner_id IS NULL "
            "AND m.contest_id IS NULL AND m.human_user_id IS NULL "
            "AND m.bot_a_id<>m.bot_b_id AND a.owner_id<>b.owner_id",
            (STATUS_COMPLETED, TYPE_LADDER),
        ).fetchall()
        history.extend(dict(row) for row in rows)
    history.sort(key=lambda row: (str(row.get("ended_at") or ""), str(row["id"])))

    revision = int(state["revision"] or 0)
    for row in history:
        revision += 1
        gid = str(row["game_id"])
        served_at = str(row.get("ended_at") or row.get("created_at") or _now())
        for owner_id in (int(row["owner_a_id"]), int(row["owner_b_id"])):
            conn.execute(
                "INSERT INTO auto_match_owner_service("
                "owner_id,game_id,served_count,last_served_revision,last_served_at) "
                "VALUES(?,?,1,?,?) ON CONFLICT(owner_id,game_id) DO UPDATE SET "
                "served_count=auto_match_owner_service.served_count+1,"
                "last_served_revision=excluded.last_served_revision,"
                "last_served_at=excluded.last_served_at",
                (owner_id, gid, revision, served_at),
            )
        for bot_id, seat_a, seat_b in (
            (int(row["bot_a_id"]), 1, 0),
            (int(row["bot_b_id"]), 0, 1),
        ):
            conn.execute(
                "INSERT INTO auto_match_bot_service("
                "bot_id,game_id,served_count,seat_a_count,seat_b_count,"
                "last_served_revision,last_served_at) VALUES(?,?,1,?,?,?,?) "
                "ON CONFLICT(bot_id,game_id) DO UPDATE SET "
                "served_count=auto_match_bot_service.served_count+1,"
                "seat_a_count=auto_match_bot_service.seat_a_count+excluded.seat_a_count,"
                "seat_b_count=auto_match_bot_service.seat_b_count+excluded.seat_b_count,"
                "last_served_revision=excluded.last_served_revision,"
                "last_served_at=excluded.last_served_at",
                (bot_id, gid, seat_a, seat_b, revision, served_at),
            )
        bot_lo, bot_hi = sorted((int(row["bot_a_id"]), int(row["bot_b_id"])))
        owner_lo, owner_hi = sorted(
            (int(row["owner_a_id"]), int(row["owner_b_id"]))
        )
        conn.execute(
            "INSERT INTO auto_match_bot_pair_service("
            "game_id,bot_lo_id,bot_hi_id,served_count,last_served_at) "
            "VALUES(?,?,?,1,?) ON CONFLICT(game_id,bot_lo_id,bot_hi_id) "
            "DO UPDATE SET served_count=auto_match_bot_pair_service.served_count+1,"
            "last_served_at=excluded.last_served_at",
            (gid, bot_lo, bot_hi, served_at),
        )
        conn.execute(
            "INSERT INTO auto_match_owner_pair_service("
            "game_id,owner_lo_id,owner_hi_id,served_count,last_served_at) "
            "VALUES(?,?,?,1,?) ON CONFLICT(game_id,owner_lo_id,owner_hi_id) "
            "DO UPDATE SET served_count=auto_match_owner_pair_service.served_count+1,"
            "last_served_at=excluded.last_served_at",
            (gid, owner_lo, owner_hi, served_at),
        )

    game_count = max(1, len(game_ids))
    conn.execute(
        "UPDATE auto_match_fair_state SET next_game_idx=?,next_lane=?,revision=?,"
        "bootstrap_version=?,updated_at=? WHERE singleton=1",
        (
            revision % game_count,
            revision % 2,
            revision,
            _AUTO_MATCH_FAIR_BOOTSTRAP_VERSION,
            _now(),
        ),
    )


def _ensure_match_rating_policy_identity(conn: sqlite3.Connection) -> None:
    """Backfill immutable game/Bot identities needed by offline replay."""
    _add_col(conn, "match_rating_policies", "game_id", "TEXT")
    _add_col(conn, "match_rating_policies", "bot_a_id", "INTEGER")
    _add_col(conn, "match_rating_policies", "bot_b_id", "INTEGER")
    _add_col(conn, "match_rating_policies", "settled_order", "INTEGER")
    # Legacy classification below inserts the frozen pool id for Matches that
    # predate policy rows, so this column must exist before that pass.
    _add_col(
        conn,
        "match_rating_policies",
        "rating_pool_id",
        "TEXT NOT NULL DEFAULT ''",
    )
    for gid in sorted(_all_game_ids()):
        table = _matches_table(gid)
        conn.execute(
            "UPDATE match_rating_policies SET "
            "game_id=COALESCE(game_id,?),"
            f"bot_a_id=COALESCE(bot_a_id,(SELECT m.bot_a_id FROM {table} m "
            "WHERE m.id=match_rating_policies.match_id)),"
            f"bot_b_id=COALESCE(bot_b_id,(SELECT m.bot_b_id FROM {table} m "
            "WHERE m.id=match_rating_policies.match_id)) "
            f"WHERE EXISTS(SELECT 1 FROM {table} m "
            "WHERE m.id=match_rating_policies.match_id)",
            (gid,),
        )
    valid_games = ",".join(f"'{gid}'" for gid in sorted(_all_game_ids()))
    for action in ("INSERT", "UPDATE OF game_id,bot_a_id,bot_b_id,rated"):
        suffix = "insert" if action == "INSERT" else "update"
        name = f"trg_match_rating_policy_identity_{suffix}"
        _ensure_trigger(
            conn,
            name,
            f"CREATE TRIGGER {name} "
            f"BEFORE {action} ON match_rating_policies WHEN "
            f"NEW.game_id IS NULL OR NEW.game_id NOT IN ({valid_games}) OR "
            "(NEW.rated=1 AND (NEW.bot_a_id IS NULL OR NEW.bot_b_id IS NULL)) "
            "BEGIN SELECT RAISE(ABORT,'rating policy identity invalid'); END",
        )


def _classify_legacy_match_rating_policies(conn: sqlite3.Connection) -> None:
    """Freeze v1 history policy without changing any rating projection."""
    classified_at = _now()
    for gid in sorted(_all_game_ids()):
        table = _matches_table(gid)
        rows = conn.execute(
            f"SELECT m.id,m.match_type,m.bot_a_id,m.bot_b_id,"
            "a.owner_id AS owner_a_id,b.owner_id AS owner_b_id "
            f"FROM {table} m LEFT JOIN bots a ON a.id=m.bot_a_id "
            "LEFT JOIN bots b ON b.id=m.bot_b_id "
            "LEFT JOIN match_rating_policies policy ON policy.match_id=m.id "
            "WHERE policy.match_id IS NULL"
        ).fetchall()
        for row in rows:
            legacy_contract = game_rule_contract(gid, legacy=True)
            if row["match_type"] == TYPE_CONTEST:
                rated, reason = False, "contest"
            elif row["match_type"] == TYPE_HUMAN:
                rated, reason = False, "human"
            elif row["bot_a_id"] is None or row["bot_b_id"] is None:
                rated, reason = False, "bot_missing"
            elif int(row["bot_a_id"]) == int(row["bot_b_id"]):
                rated, reason = False, "self_play"
            elif row["owner_a_id"] is None or row["owner_b_id"] is None:
                rated, reason = False, "owner_missing"
            elif int(row["owner_a_id"]) == int(row["owner_b_id"]):
                rated, reason = False, "same_owner"
            else:
                rated, reason = True, "eligible"
            conn.execute(
                "INSERT INTO match_rating_policies("
                "match_id,game_id,rating_pool_id,bot_a_id,bot_b_id,rated,rating_reason,source,"
                "classified_at) VALUES(?,?,?,?,?,?,?,'legacy_migration',?)",
                (
                    row["id"], gid, legacy_contract["rating_pool_id"],
                    row["bot_a_id"], row["bot_b_id"],
                    1 if rated else 0, reason, classified_at,
                ),
            )


def _ensure_rating_settlement_sequence(conn: sqlite3.Connection) -> None:
    """Give every real settlement an immutable, globally monotonic order.

    Historical v1 rows did not record a sequence.  Production verification
    established that replaying them by ``(ended_at, id)`` reproduces the live
    Glicko projection exactly, while ``created_at`` does not.  The first v2
    migration therefore assigns that canonical order once.  New rows allocate
    their sequence in the same transaction as the settlement side effects.
    """
    _add_col(conn, "match_rating_settlements", "settled_order", "INTEGER")
    if "auto_match_decisions" in {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }:
        _add_col(conn, "auto_match_decisions", "settlement_order", "INTEGER")

    conn.execute(
        "UPDATE match_rating_settlements SET settled_order=0 "
        "WHERE match_id=? AND settled_order IS NULL",
        (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,),
    )
    existing = conn.execute(
        "SELECT COUNT(*) AS n,COALESCE(MAX(settled_order),0) AS max_order "
        "FROM match_rating_settlements WHERE match_id<>? "
        "AND settled_order IS NOT NULL",
        (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,),
    ).fetchone()
    next_order = int(existing["max_order"] or 0) + 1

    pending: list[tuple[str, str]] = []
    seen: set[str] = set()
    for gid in sorted(_all_game_ids()):
        table = _matches_table(gid)
        rows = conn.execute(
            "SELECT settled.match_id,"
            "COALESCE(m.ended_at,settled.settled_at,'') AS order_at "
            "FROM match_rating_settlements settled "
            f"JOIN {table} m ON m.id=settled.match_id "
            "WHERE settled.match_id<>? AND settled.settled_order IS NULL",
            (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,),
        ).fetchall()
        for row in rows:
            match_id = str(row["match_id"])
            pending.append((str(row["order_at"] or ""), match_id))
            seen.add(match_id)
    # Defensive orphan handling: an old marker without a match must still get
    # an auditable order, after all rows whose authoritative terminal exists.
    for row in conn.execute(
        "SELECT match_id,settled_at FROM match_rating_settlements "
        "WHERE match_id<>? AND settled_order IS NULL ORDER BY settled_at,match_id",
        (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,),
    ).fetchall():
        match_id = str(row["match_id"])
        if match_id not in seen:
            pending.append((str(row["settled_at"] or ""), match_id))
    for _, match_id in sorted(pending):
        conn.execute(
            "UPDATE match_rating_settlements SET settled_order=? "
            "WHERE match_id=? AND settled_order IS NULL",
            (next_order, match_id),
        )
        next_order += 1

    actual_orders = [
        int(row[0])
        for row in conn.execute(
            "SELECT settled_order FROM match_rating_settlements "
            "WHERE match_id<>? ORDER BY settled_order",
            (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,),
        )
    ]
    if actual_orders != list(range(1, len(actual_orders) + 1)):
        raise RuntimeError(
            "match_rating_settlements.settled_order 必须是连续 1..N"
        )
    conn.execute(
        "UPDATE match_rating_policies SET settled_order=("
        "SELECT settled.settled_order FROM match_rating_settlements settled "
        "WHERE settled.match_id=match_rating_policies.match_id) "
        "WHERE EXISTS(SELECT 1 FROM match_rating_settlements settled "
        "WHERE settled.match_id=match_rating_policies.match_id)"
    )
    reserved_orders = [
        int(row[0])
        for row in conn.execute(
            "SELECT policy.settled_order FROM match_rating_policies policy "
            "LEFT JOIN match_rating_settlements settled "
            "ON settled.match_id=policy.match_id "
            "WHERE policy.settled_order IS NOT NULL "
            "AND settled.match_id IS NULL ORDER BY policy.settled_order"
        )
    ]
    expected_reserved = list(
        range(len(actual_orders) + 1, len(actual_orders) + len(reserved_orders) + 1)
    )
    if reserved_orders != expected_reserved:
        raise RuntimeError(
            "未结算 match_rating_policies.settled_order 必须紧接已结算序号"
        )
    next_reserved = len(actual_orders) + len(reserved_orders) + 1
    pending: list[tuple[str, str]] = []
    for gid in sorted(_all_game_ids()):
        table = _matches_table(gid)
        pending.extend(
            (str(row["ended_at"] or ""), str(row["id"]))
            for row in conn.execute(
                f"SELECT m.id,m.ended_at FROM {table} m "
                "JOIN match_rating_policies policy ON policy.match_id=m.id "
                "WHERE m.status=? AND policy.settled_order IS NULL "
                "AND policy.rating_reason NOT IN ('contest','human')",
                (STATUS_COMPLETED,),
            ).fetchall()
        )
    for _, match_id in sorted(pending):
        conn.execute(
            "UPDATE match_rating_policies SET settled_order=? "
            "WHERE match_id=? AND settled_order IS NULL",
            (next_reserved, match_id),
        )
        next_reserved += 1
    conn.execute(
        "UPDATE rating_settlement_sequence SET next_order=? WHERE singleton=1",
        (next_reserved,),
    )
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_match_rating_policy_settled_order "
        "ON match_rating_policies(settled_order) WHERE settled_order IS NOT NULL"
    )
    _ensure_trigger(
        conn,
        "trg_match_rating_policy_order_immutable",
        "CREATE TRIGGER trg_match_rating_policy_order_immutable "
        "BEFORE UPDATE OF settled_order ON match_rating_policies "
        "WHEN OLD.settled_order IS NOT NULL AND OLD.settled_order IS NOT NEW.settled_order "
        "BEGIN SELECT RAISE(ABORT,'rating policy settled_order immutable'); END",
    )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_match_rating_settled_order "
        "ON match_rating_settlements(settled_order) "
        "WHERE settled_order IS NOT NULL AND settled_order>0"
    )
    sentinel = MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL.replace("'", "''")
    _ensure_trigger(
        conn,
        "trg_match_rating_settlement_order_insert",
        "CREATE TRIGGER trg_match_rating_settlement_order_insert "
        "BEFORE INSERT ON match_rating_settlements "
        f"WHEN NEW.match_id<>'{sentinel}' AND ("
        "NEW.settled_order IS NULL OR NEW.settled_order<=0 OR "
        "NEW.settled_order<>(SELECT COALESCE(MAX(settled_order),0)+1 "
        "FROM match_rating_settlements WHERE settled_order>0)) BEGIN "
        "SELECT RAISE(ABORT,'rating settlement order must be next'); END",
    )
    _ensure_trigger(
        conn,
        "trg_match_rating_settlement_order_immutable",
        "CREATE TRIGGER trg_match_rating_settlement_order_immutable "
        "BEFORE UPDATE OF match_id,settled_at,settled_order "
        "ON match_rating_settlements WHEN OLD.match_id IS NOT NEW.match_id OR "
        "OLD.settled_at IS NOT NEW.settled_at OR "
        "OLD.settled_order IS NOT NEW.settled_order BEGIN "
        "SELECT RAISE(ABORT,'rating settlement source immutable'); END",
    )
    _ensure_trigger(
        conn,
        "trg_match_rating_settlement_delete_immutable",
        "CREATE TRIGGER trg_match_rating_settlement_delete_immutable "
        "BEFORE DELETE ON match_rating_settlements "
        "WHEN OLD.settled_order IS NOT NULL BEGIN "
        "SELECT RAISE(ABORT,'rating settlement source immutable'); END",
    )
    # Older releases advanced count/last from policy_version alone.  That
    # partially blessed an already-stale state before the application had
    # verified the projection/source/plan baseline.  The Store now advances all
    # five summary fields together behind an explicit pre-mutation guard.
    conn.execute("DROP TRIGGER IF EXISTS trg_match_rating_projection_advance")
    _ensure_trigger(
        conn,
        "trg_match_rating_projection_dirty_on_delete",
        "CREATE TRIGGER trg_match_rating_projection_dirty_on_delete "
        "AFTER DELETE ON match_rating_settlements WHEN OLD.settled_order>0 BEGIN "
        "UPDATE rating_projection_state SET policy_version='projection-dirty',"
        "rebuilt_at=NULL WHERE singleton=1; END",
    )
    _install_rating_source_guards(conn)


def _schema_create_table_sql(table: str, *, as_name: str | None = None) -> str:
    """Return one canonical CREATE TABLE statement from ``schema.SCHEMA``.

    Execution jobs carry several coupled CHECK constraints.  Reusing the fresh
    schema text keeps an upgraded database byte-for-byte aligned with a new one
    instead of maintaining a second hand-copied definition in migrations.
    """
    marker = f"CREATE TABLE IF NOT EXISTS {table} ("
    start = SCHEMA.find(marker)
    if start < 0:
        raise RuntimeError(f"SCHEMA missing table definition: {table}")
    end = SCHEMA.find("\n);", start)
    if end < 0:
        raise RuntimeError(f"SCHEMA has unterminated table definition: {table}")
    statement = SCHEMA[start : end + 3]
    if as_name is not None:
        statement = statement.replace(marker, f"CREATE TABLE {as_name} (", 1)
    return statement


def _ensure_execution_environment_schema(conn: sqlite3.Connection) -> None:
    """Upgrade the durable queue to frozen per-seat execution environments.

    SQLite cannot relax the old ``sandbox_units IN (1,2)`` / human-only CHECK
    in place.  Rebuild the parent and its sole FK child in one Store transaction,
    preserving every id and attempt.  Existing work is canonical low Docker;
    the human seat remains non-Bot and therefore consumes no sandbox resource.
    """
    tables = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "execution_jobs" not in tables:
        return
    expected = {
        "bot_a_environment",
        "bot_b_environment",
        "bot_a_local_agent_id",
        "bot_b_local_agent_id",
        "host_cpu_millis",
        "host_memory_mb",
        "profile_version",
    }
    if expected.issubset(_table_cols(conn, "execution_jobs")):
        return

    attempts_exist = "execution_job_attempts" in tables
    if attempts_exist:
        conn.execute(
            "CREATE TEMP TABLE execution_job_attempts_env_backup AS "
            "SELECT * FROM execution_job_attempts"
        )
        conn.execute("DROP TABLE execution_job_attempts")

    conn.execute(_schema_create_table_sql("execution_jobs", as_name="execution_jobs_env_new"))
    conn.execute(
        "INSERT INTO execution_jobs_env_new("
        "id,public_id,source,status,priority,owner_user_id,game_id,match_type,"
        "bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id,"
        "bot_a_environment,bot_b_environment,bot_a_local_agent_id,"
        "bot_b_local_agent_id,human_user_id,human_seat,contest_id,"
        "contest_pairing_id,match_config,rated,rating_reason,match_slots,"
        "sandbox_units,host_cpu_millis,host_memory_mb,profile_version,"
        "current_match_id,auto_decision_id,cancel_requested,attempt_count,"
        "cleanup_state,failure_count,next_attempt_at,retryable,terminal_reason,"
        "last_error,created_at,claimed_at,started_at,settling_at,terminal_at) "
        "SELECT id,public_id,source,status,priority,owner_user_id,game_id,match_type,"
        "bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id,"
        "CASE WHEN source='human' AND human_seat=0 THEN 'human' "
        "     ELSE 'platform_low' END,"
        "CASE WHEN source='human' AND human_seat=1 THEN 'human' "
        "     ELSE 'platform_low' END,NULL,NULL,human_user_id,human_seat,contest_id,"
        "contest_pairing_id,match_config,rated,rating_reason,match_slots,"
        "sandbox_units,sandbox_units*1000,sandbox_units*512,0,current_match_id,"
        "auto_decision_id,cancel_requested,attempt_count,cleanup_state,"
        "failure_count,next_attempt_at,retryable,terminal_reason,last_error,"
        "created_at,claimed_at,started_at,settling_at,terminal_at "
        "FROM execution_jobs"
    )
    conn.execute("DROP TABLE execution_jobs")
    conn.execute("ALTER TABLE execution_jobs_env_new RENAME TO execution_jobs")

    if attempts_exist:
        conn.execute(_schema_create_table_sql("execution_job_attempts"))
        conn.execute(
            "INSERT INTO execution_job_attempts("
            "id,job_id,attempt_no,match_id,status,events_observed,created_at,"
            "started_at,terminal_at,terminal_reason) "
            "SELECT id,job_id,attempt_no,match_id,status,events_observed,created_at,"
            "started_at,terminal_at,terminal_reason "
            "FROM execution_job_attempts_env_backup"
        )
        conn.execute("DROP TABLE execution_job_attempts_env_backup")

    conn.execute(
        "CREATE INDEX idx_execution_jobs_dispatch "
        "ON execution_jobs(status,priority,created_at,id)"
    )
    conn.execute(
        "CREATE INDEX idx_execution_jobs_owner "
        "ON execution_jobs(owner_user_id,status,created_at)"
    )
    conn.execute(
        "CREATE INDEX idx_execution_jobs_source "
        "ON execution_jobs(source,status,created_at)"
    )
    conn.execute(
        "CREATE INDEX idx_execution_jobs_contest_claim_history "
        "ON execution_jobs(source,contest_id,claimed_at,id)"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_execution_jobs_current_match "
        "ON execution_jobs(current_match_id) WHERE current_match_id IS NOT NULL"
    )
    conn.execute(
        "CREATE UNIQUE INDEX idx_execution_jobs_active_contest_pairing "
        "ON execution_jobs(contest_pairing_id) WHERE contest_pairing_id IS NOT NULL "
        "AND status IN ('queued','starting','running','settling')"
    )


def _ensure_game_contract_state(
    conn: sqlite3.Connection, *, fresh_schema: bool
) -> None:
    """为旧实体回填规则/协议/评分池契约。

    旧库的 Gomoku 在显式 cutover 前必须保持 legacy；新库则直接
    使用代码声明的 current contract。通用层只遍历声明表，不写
    ``if game_id == ...`` 分支。
    """
    _add_col(
        conn,
        "protocol_cutovers",
        "manifest_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    for table, columns in (
        ("bots", (("protocol_version", "TEXT NOT NULL DEFAULT ''"),)),
        (
            "bot_versions",
            (
                ("protocol_version", "TEXT NOT NULL DEFAULT ''"),
                ("retired_at", "TEXT"),
                ("retirement_reason", "TEXT NOT NULL DEFAULT ''"),
            ),
        ),
        ("local_ai_agents", (("protocol_version", "TEXT NOT NULL DEFAULT ''"),)),
        (
            "contests",
            (
                ("ruleset_version", "TEXT NOT NULL DEFAULT ''"),
                ("protocol_version", "TEXT NOT NULL DEFAULT ''"),
                ("rating_pool_id", "TEXT NOT NULL DEFAULT ''"),
            ),
        ),
        (
            "execution_jobs",
            (
                ("ruleset_version", "TEXT NOT NULL DEFAULT ''"),
                ("protocol_version", "TEXT NOT NULL DEFAULT ''"),
                ("rating_pool_id", "TEXT NOT NULL DEFAULT ''"),
            ),
        ),
        (
            "match_rating_policies",
            (("rating_pool_id", "TEXT NOT NULL DEFAULT ''"),),
        ),
    ):
        for column, declaration in columns:
            _add_col(conn, table, column, declaration)

    for game_id in sorted(_all_game_ids()):
        contract = game_rule_contract(game_id, legacy=not fresh_schema)
        conn.execute(
            "INSERT OR IGNORE INTO rating_pool_state("
            "game_id,active_pool_id,ruleset_version,protocol_version,activated_at) "
            "VALUES(?,?,?,?,?)",
            (
                game_id,
                contract["rating_pool_id"],
                contract["ruleset_version"],
                contract["protocol_version"],
                _now(),
            ),
        )
        historical = game_rule_contract(game_id, legacy=True)
        for table in ("matches_" + game_id,):
            for column, declaration in (
                ("ruleset_version", "TEXT NOT NULL DEFAULT ''"),
                ("protocol_version", "TEXT NOT NULL DEFAULT ''"),
                ("rating_pool_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                _add_col(conn, table, column, declaration)
        conn.execute(
            "UPDATE bots SET protocol_version=? WHERE game_id=? "
            "AND protocol_version=''",
            (historical["protocol_version"], game_id),
        )
        conn.execute(
            "UPDATE bot_versions SET protocol_version=? WHERE protocol_version='' "
            "AND bot_id IN (SELECT id FROM bots WHERE game_id=?)",
            (historical["protocol_version"], game_id),
        )
        conn.execute(
            "UPDATE local_ai_agents SET protocol_version=? WHERE game_id=? "
            "AND protocol_version=''",
            (historical["protocol_version"], game_id),
        )
        conn.execute(
            "UPDATE contests SET ruleset_version=?,protocol_version=?,rating_pool_id=? "
            "WHERE game_id=? AND (ruleset_version='' OR protocol_version='' "
            "OR rating_pool_id='')",
            (
                historical["ruleset_version"],
                historical["protocol_version"],
                historical["rating_pool_id"],
                game_id,
            ),
        )
        conn.execute(
            f"UPDATE matches_{game_id} SET ruleset_version=?,protocol_version=?,"
            "rating_pool_id=? WHERE ruleset_version='' OR protocol_version='' "
            "OR rating_pool_id=''",
            (
                historical["ruleset_version"],
                historical["protocol_version"],
                historical["rating_pool_id"],
            ),
        )
        conn.execute(
            "UPDATE execution_jobs SET ruleset_version=?,protocol_version=?,"
            "rating_pool_id=? WHERE game_id=? AND (ruleset_version='' "
            "OR protocol_version='' OR rating_pool_id='')",
            (
                historical["ruleset_version"],
                historical["protocol_version"],
                historical["rating_pool_id"],
                game_id,
            ),
        )
        conn.execute(
            "UPDATE match_rating_policies SET rating_pool_id=? WHERE game_id=? "
            "AND rating_pool_id=''",
            (historical["rating_pool_id"], game_id),
        )


def _ensure_ranked_bot_selection(
    conn: sqlite3.Connection, *, fresh_schema: bool
) -> None:
    """Install the one-ranked-Bot invariant and seed an existing database once.

    ``SCHEMA`` runs before migrations, so the partial index cannot live there:
    an existing ``bots`` table does not gain the new column from
    ``CREATE TABLE IF NOT EXISTS``.  First-install is therefore identified by
    the old table lacking ``is_ranked`` -- never merely by an object name.  A
    canonical index is verified byte-for-byte after whitespace normalization;
    collisions and drift fail closed rather than silently reassigning an owner
    who intentionally left their ranked seat empty.
    """
    index_name = "idx_bots_one_ranked_per_owner_game"
    create_sql = (
        f"CREATE UNIQUE INDEX {index_name} "
        "ON bots(owner_id,game_id) WHERE is_ranked=1"
    )
    desired_sql = _normalize_schema_sql(create_sql)
    objects = conn.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=? ORDER BY type",
        (index_name,),
    ).fetchall()
    conflicts = sorted(
        str(row["type"]) for row in objects if str(row["type"]) != "index"
    )
    if conflicts:
        raise RuntimeError(
            f"schema object name collision for ranked Bot index {index_name}: "
            f"{conflicts}"
        )
    index_rows = [row for row in objects if str(row["type"]) == "index"]
    if len(index_rows) > 1:
        raise RuntimeError(f"duplicate ranked Bot index definition: {index_name}")
    if index_rows:
        current_sql = _normalize_schema_sql(str(index_rows[0]["sql"] or ""))
        if current_sql != desired_sql:
            raise RuntimeError(f"ranked Bot index definition mismatch: {index_name}")
        if "is_ranked" not in _table_cols(conn, "bots"):
            raise RuntimeError("canonical ranked Bot index exists without is_ranked")
        return

    had_ranked_column = "is_ranked" in _table_cols(conn, "bots")
    bot_count = int(conn.execute("SELECT COUNT(*) FROM bots").fetchone()[0])
    if had_ranked_column and bot_count and not fresh_schema:
        raise RuntimeError(
            f"ranked Bot index missing on non-empty migrated database: {index_name}"
        )
    if fresh_schema and bot_count:
        raise RuntimeError("fresh schema unexpectedly contains Bots before migration")
    _add_col(
        conn,
        "bots",
        "is_ranked",
        "INTEGER NOT NULL DEFAULT 0 CHECK (is_ranked IN (0,1))",
    )
    if not had_ranked_column:
        # Existing public candidates are reduced deterministically to the Bot
        # that would rank highest today.  A fresh schema has no rows here; its
        # first successfully activated upload is selected by BotManager.
        conn.execute("UPDATE bots SET is_ranked=0 WHERE is_ranked<>0")
        conn.execute(
            "WITH candidates AS ("
            " SELECT b.id,ROW_NUMBER() OVER ("
            "  PARTITION BY b.owner_id,b.game_id ORDER BY "
            "  (COALESCE(r.matches_played,0)>=?) DESC,"
            "  COALESCE(r.rating,1500.0) DESC,"
            "  COALESCE(r.matches_played,0) DESC,b.id ASC"
            " ) AS choice_rank "
            " FROM bots b "
            " LEFT JOIN ratings r ON r.bot_id=b.id AND r.game_id=b.game_id "
            " JOIN rating_pool_state pool ON pool.game_id=b.game_id "
            " WHERE b.is_active=1 AND b.binary_path<>'' "
            " AND b.format=? AND b.os=? AND b.arch=? "
            " AND b.protocol_version=pool.protocol_version "
            " AND ((b.current_version=0 AND NOT EXISTS("
            "   SELECT 1 FROM bot_versions any_version WHERE any_version.bot_id=b.id"
            " )) OR EXISTS("
            "   SELECT 1 FROM bot_versions v WHERE v.bot_id=b.id "
            "   AND v.version=b.current_version AND v.retired_at IS NULL "
            "   AND v.binary_path<>'' AND v.binary_path=b.binary_path "
            "   AND v.runtime_mode=b.runtime_mode "
            "   AND v.protocol_version=pool.protocol_version "
            "   AND v.format=? AND v.os=? AND v.arch=?"
            " ))"
            ") UPDATE bots SET is_ranked=1 WHERE id IN ("
            " SELECT id FROM candidates WHERE choice_rank=1"
            ")",
            (
                max(1, int(RANKING_MIN_RATED_MATCHES)),
                SUPPORTED_BINARY_FORMAT,
                SUPPORTED_BINARY_OS,
                SUPPORTED_BINARY_ARCH,
                SUPPORTED_BINARY_FORMAT,
                SUPPORTED_BINARY_OS,
                SUPPORTED_BINARY_ARCH,
            ),
        )
    conn.execute(create_sql)
    installed = conn.execute(
        "SELECT type,sql FROM sqlite_master WHERE name=?", (index_name,)
    ).fetchall()
    if (
        len(installed) != 1
        or str(installed[0]["type"]) != "index"
        or _normalize_schema_sql(str(installed[0]["sql"] or "")) != desired_sql
    ):
        raise RuntimeError(f"ranked Bot index verification failed: {index_name}")


def _migrate(conn: sqlite3.Connection, *, fresh_schema: bool = False) -> None:
    """为已有库补列；必要时重建 contests 以放宽 status CHECK。"""
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "contests" not in tables:
        return

    # 旧 notifications 仅作为 communications 站内消息的兼容读投影。
    # 既有行保持 NULL，不反向伪造成新会话；新写入在 communication 事务里带 public id。
    if "notifications" in tables:
        _add_col(conn, "notifications", "communication_message_public_id", "TEXT")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_notif_communication_message "
            "ON notifications(communication_message_public_id) "
            "WHERE communication_message_public_id IS NOT NULL"
        )
    if "email_codes" in tables:
        _add_col(
            conn,
            "email_codes",
            "failed_attempts",
            "INTEGER NOT NULL DEFAULT 0 "
            "CHECK (typeof(failed_attempts)='integer' AND failed_attempts "
            f"BETWEEN 0 AND {EMAIL_CODE_MAX_FAILED_ATTEMPTS})",
        )
    if "messages" in tables:
        _add_col(conn, "messages", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
    if "execution_control" in tables:
        _add_col(
            conn,
            "execution_control",
            "deployment_drain_requested",
            "INTEGER NOT NULL DEFAULT 0 CHECK (deployment_drain_requested IN (0,1))",
        )
        _add_col(
            conn,
            "execution_control",
            "deployment_drain_reason",
            "TEXT NOT NULL DEFAULT ''",
        )
    if "broadcast_recipients" in tables:
        _add_col(conn, "broadcast_recipients", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "broadcast_recipients", "max_attempts", "INTEGER NOT NULL DEFAULT 5")
        _add_col(conn, "broadcast_recipients", "next_attempt_at", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "broadcast_recipients", "last_error", "TEXT NOT NULL DEFAULT ''")
        work_index = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' "
            "AND name='idx_broadcast_recipient_work'"
        ).fetchone()
        if work_index is None or "next_attempt_at" not in str(work_index[0] or ""):
            conn.execute("DROP INDEX IF EXISTS idx_broadcast_recipient_work")
            conn.execute(
                "CREATE INDEX idx_broadcast_recipient_work ON broadcast_recipients("
                "broadcast_id,state,next_attempt_at,id)"
            )

    # ── 孤儿 FK 行清理（审计 P0：生产 9943 条孤儿源于连接期 FK=OFF，删 bot/user 未级联）──
    # 一次性清理存量孤儿。幂等：DELETE/UPDATE 0 行代价极低，每次迁移都跑。
    # 放在 _migrate 开头（新库早返之后）保证所有后续表重建 INSERT 只看到干净数据
    # （contest_* 重建的 INSERT INTO _new SELECT FROM _ctable 未过滤孤儿，FK ON 时会失败）。
    def _has(table: str) -> bool:
        return table in {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
        }

    # CASCADE 类（删行）：bots→子表 + users→子表
    for _tbl, _col in (
        ("ratings", "bot_id"),
        ("rating_history", "bot_id"),
        ("bot_versions", "bot_id"),
        ("favorites", "bot_id"),
        ("pair_stats", "bot_a_id"),
        ("pair_stats", "bot_b_id"),
        ("password_resets", "user_id"),
        ("sessions", "user_id"),
        ("email_codes", "user_id"),
        ("notifications", "user_id"),
        ("comments", "user_id"),
        ("likes", "user_id"),
        ("follows", "follower_id"),
        ("follows", "followee_id"),
    ):
        if _has(_tbl):
            conn.execute(
                f"DELETE FROM {_tbl} WHERE {_col} IS NOT NULL "
                f"AND {_col} NOT IN (SELECT id FROM {'bots' if _col.endswith('bot_id') or _col in ('bot_a_id', 'bot_b_id') else 'users'})"
            )

    # SET NULL 类（置空保留行）：matches_*.{bot_a/b/owner/contest} + contest_*.{bot_id}
    for _gid in _all_game_ids():
        _tbl = _matches_table(_gid)
        if _has(_tbl):
            for _col in ("bot_a_id", "bot_b_id"):
                conn.execute(
                    f"UPDATE {_tbl} SET {_col}=NULL WHERE {_col} IS NOT NULL "
                    f"AND {_col} NOT IN (SELECT id FROM bots)"
                )
            conn.execute(
                f"UPDATE {_tbl} SET owner_id=NULL WHERE owner_id IS NOT NULL "
                f"AND owner_id NOT IN (SELECT id FROM users)"
            )
            conn.execute(
                f"UPDATE {_tbl} SET contest_id=NULL WHERE contest_id IS NOT NULL "
                f"AND contest_id NOT IN (SELECT id FROM contests)"
            )
    for _tbl, _col in (
        ("contest_entries", "bot_id"),
        ("contest_pairings", "bot_a_id"),
        ("contest_pairings", "bot_b_id"),
        ("contest_stage_results", "bot_id"),
        ("contest_official_results", "bot_id"),
    ):
        if _has(_tbl):
            conn.execute(
                f"UPDATE {_tbl} SET {_col}=NULL WHERE {_col} IS NOT NULL "
                f"AND {_col} NOT IN (SELECT id FROM bots)"
            )

    # ── 赛事侧孤儿 FK 清理（对抗审计：PR #88/#93 仅覆盖 bots/users，漏 contest 侧）──
    # contests.* 的子表 contest_id/user_id 孤儿（CASCADE：删行）+ contests.organizer_id
    # 孤儿（NO ACTION + NOT NULL：只能删整条 contest）。必须在 contests_new / contest_*
    # 重建（下方 INSERT INTO _new SELECT FROM _old，未过滤孤儿）之前完成，否则 FK ON 时
    # 重建 INSERT 抛 IntegrityError 启动崩溃。
    # 顺序：先删 contest 子表孤儿（contest_id/user_id），再删 organizer 孤儿的 contest 本身。
    for _tbl, _col, _parent in (
        ("contest_entries", "contest_id", "contests"),
        ("contest_entries", "user_id", "users"),
        ("contest_pairings", "contest_id", "contests"),
        ("contest_stage_results", "contest_id", "contests"),
        ("contest_official_results", "contest_id", "contests"),
    ):
        if _has(_tbl):
            conn.execute(
                f"DELETE FROM {_tbl} WHERE {_col} IS NOT NULL "
                f"AND {_col} NOT IN (SELECT id FROM {_parent})"
            )
    # contests.organizer_id → users（NO ACTION + NOT NULL）：organizer 不存在的 contest 整条删。
    # 此时其 contest_* 子表孤儿已清（上方），CASCADE 亦带走残留——双保险。
    if _has("contests"):
        conn.execute(
            "DELETE FROM contests WHERE organizer_id IS NOT NULL "
            "AND organizer_id NOT IN (SELECT id FROM users)"
        )
    # 补 PR #88 CASCADE 类遗漏：bots.owner_id / favorites.user_id / notification_prefs.user_id
    for _tbl, _col in (
        ("bots", "owner_id"),
        ("favorites", "user_id"),
        ("notification_prefs", "user_id"),
    ):
        if _has(_tbl):
            conn.execute(
                f"DELETE FROM {_tbl} WHERE {_col} IS NOT NULL "
                f"AND {_col} NOT IN (SELECT id FROM users)"
            )

    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='contests'"
    ).fetchone()
    sql_text = (create_sql[0] or "") if create_sql else ""
    cols = _table_cols(conn, "contests")
    pairing_topology_columns = {
        "pairing_topology_revision",
        "sealed_pairing_topology_revision",
    }
    present_pairing_topology_columns = pairing_topology_columns.intersection(cols)
    if present_pairing_topology_columns and (
        present_pairing_topology_columns != pairing_topology_columns
    ):
        raise RuntimeError("contest pairing topology revision schema is partial")
    for col, decl in (
        ("game_id", "TEXT NOT NULL DEFAULT 'holdem'"),
        ("stages_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("current_stage_idx", "INTEGER NOT NULL DEFAULT 0"),
        ("template_id", "TEXT NOT NULL DEFAULT 'holdem_swiss_ko'"),
        ("rest_ends_at", "TEXT"),
        ("phase", "TEXT NOT NULL DEFAULT 'standalone'"),  # P2 预赛/决赛
        ("source_contest_id", "INTEGER"),
        ("time_control_id", "TEXT"),
        ("format_snapshot_json", "TEXT NOT NULL DEFAULT '{}'"),
        (
            "published_stage_pairing_count",
            "INTEGER CHECK (published_stage_pairing_count IS NULL "
            "OR (typeof(published_stage_pairing_count)='integer' "
            "AND published_stage_pairing_count >= 0))",
        ),
        (
            "pairing_topology_revision",
            "INTEGER NOT NULL DEFAULT 0 CHECK ("
            "typeof(pairing_topology_revision)='integer' "
            "AND pairing_topology_revision >= 0)",
        ),
        (
            "sealed_pairing_topology_revision",
            "INTEGER CHECK (sealed_pairing_topology_revision IS NULL "
            "OR (typeof(sealed_pairing_topology_revision)='integer' "
            "AND sealed_pairing_topology_revision >= 0))",
        ),
        ("official_results_ready", "INTEGER NOT NULL DEFAULT 0"),
        ("require_real_name", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_col(conn, "contests", col, decl)

    cols = _table_cols(conn, "contests")
    if "'rest'" not in sql_text:
        conn.execute(
            """
            CREATE TABLE contests_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                organizer_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'draft',
                registration_opens_at TEXT,
                registration_closes_at TEXT,
                starts_at TEXT,
                ends_at TEXT,
                created_at TEXT NOT NULL,
                game_id TEXT NOT NULL,
                stages_json TEXT NOT NULL DEFAULT '[]',
                current_stage_idx INTEGER NOT NULL DEFAULT 0,
                template_id TEXT NOT NULL DEFAULT 'holdem_swiss_ko',
                rest_ends_at TEXT,
                published_stage_pairing_count INTEGER CHECK (
                    published_stage_pairing_count IS NULL OR (
                        typeof(published_stage_pairing_count)='integer'
                        AND published_stage_pairing_count>=0)),
                pairing_topology_revision INTEGER NOT NULL DEFAULT 0 CHECK (
                    typeof(pairing_topology_revision)='integer'
                    AND pairing_topology_revision>=0),
                sealed_pairing_topology_revision INTEGER CHECK (
                    sealed_pairing_topology_revision IS NULL OR (
                        typeof(sealed_pairing_topology_revision)='integer'
                        AND sealed_pairing_topology_revision>=0)),
                CONSTRAINT chk_contest_status CHECK (
                    status IN ('draft','open','running','rest','finished','cancelled'))
            )
            """
        )
        all_cols = [
            "id", "title", "description", "organizer_id", "status",
            "registration_opens_at", "registration_closes_at", "starts_at",
            "ends_at", "created_at",
            "game_id", "stages_json", "current_stage_idx", "template_id",
            "rest_ends_at", "published_stage_pairing_count",
            "pairing_topology_revision", "sealed_pairing_topology_revision",
        ]
        present = [c for c in all_cols if c in cols]
        conn.execute(
            f"INSERT INTO contests_new ({', '.join(present)}) "
            f"SELECT {', '.join(present)} FROM contests "
            f"WHERE organizer_id IN (SELECT id FROM users)"
        )
        conn.execute("DROP TABLE contests")
        conn.execute("ALTER TABLE contests_new RENAME TO contests")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contests_org ON contests(organizer_id)"
        )

    # ── contests CHECK 加 'published' 状态（时间编排：排期已发布、等待开赛）──
    # 重建表以放宽 CHECK（旧库 CHECK 不含 'published'，新赛事到点出排期会违反约束）。
    if "contests" in tables and "'published'" not in sql_text:
        cols = _table_cols(conn, "contests")
        conn.execute(
            """
            CREATE TABLE contests_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                organizer_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'draft',
                registration_opens_at TEXT,
                registration_closes_at TEXT,
                starts_at TEXT,
                ends_at TEXT,
                created_at TEXT NOT NULL,
                game_id TEXT NOT NULL,
                stages_json TEXT NOT NULL DEFAULT '[]',
                current_stage_idx INTEGER NOT NULL DEFAULT 0,
                template_id TEXT NOT NULL DEFAULT 'holdem_swiss_ko',
                rest_ends_at TEXT,
                phase TEXT NOT NULL DEFAULT 'standalone',
                source_contest_id INTEGER,
                time_control_id TEXT,
                format_snapshot_json TEXT NOT NULL DEFAULT '{}',
                published_stage_pairing_count INTEGER CHECK (
                    published_stage_pairing_count IS NULL OR (
                        typeof(published_stage_pairing_count)='integer'
                        AND published_stage_pairing_count>=0)),
                pairing_topology_revision INTEGER NOT NULL DEFAULT 0 CHECK (
                    typeof(pairing_topology_revision)='integer'
                    AND pairing_topology_revision>=0),
                sealed_pairing_topology_revision INTEGER CHECK (
                    sealed_pairing_topology_revision IS NULL OR (
                        typeof(sealed_pairing_topology_revision)='integer'
                        AND sealed_pairing_topology_revision>=0)),
                official_results_ready INTEGER NOT NULL DEFAULT 0,
                require_real_name INTEGER NOT NULL DEFAULT 0,
                CONSTRAINT chk_contest_status CHECK (
                    status IN ('draft','open','published','running','rest','finished','cancelled'))
            )
            """
        )
        all_cols = [
            "id", "title", "description", "organizer_id", "status",
            "registration_opens_at", "registration_closes_at", "starts_at",
            "ends_at", "created_at", "game_id",
            "stages_json", "current_stage_idx", "template_id", "rest_ends_at",
            "phase", "source_contest_id", "time_control_id", "format_snapshot_json",
            "published_stage_pairing_count", "pairing_topology_revision",
            "sealed_pairing_topology_revision",
            "official_results_ready", "require_real_name",
        ]
        present = [c for c in all_cols if c in cols]
        conn.execute(
            f"INSERT INTO contests_new ({', '.join(present)}) "
            f"SELECT {', '.join(present)} FROM contests "
            f"WHERE organizer_id IN (SELECT id FROM users)"
        )
        conn.execute("DROP TABLE contests")
        conn.execute("ALTER TABLE contests_new RENAME TO contests")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contests_org ON contests(organizer_id)"
        )

    # Reassert post-rebuild columns only after every legacy contests-table
    # replacement above.  Old databases cannot carry a trustworthy publication
    # manifest, so the nullable column is intentionally added without backfill;
    # the manager must strictly validate before sealing it.
    _add_col(
        conn,
        "contests",
        "published_stage_pairing_count",
        "INTEGER CHECK (published_stage_pairing_count IS NULL "
        "OR (typeof(published_stage_pairing_count)='integer' "
        "AND published_stage_pairing_count >= 0))",
    )
    _add_col(
        conn,
        "contests",
        "pairing_topology_revision",
        "INTEGER NOT NULL DEFAULT 0 CHECK ("
        "typeof(pairing_topology_revision)='integer' "
        "AND pairing_topology_revision >= 0)",
    )
    _add_col(
        conn,
        "contests",
        "sealed_pairing_topology_revision",
        "INTEGER CHECK (sealed_pairing_topology_revision IS NULL "
        "OR (typeof(sealed_pairing_topology_revision)='integer' "
        "AND sealed_pairing_topology_revision >= 0))",
    )
    # Long-lived customer-demo contests are explicit immutable snapshots.
    _add_col(conn, "contests", "showcase_key", "TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_contests_showcase_key "
        "ON contests(showcase_key) WHERE showcase_key IS NOT NULL"
    )

    if "contest_official_results" in tables:
        _add_col(
            conn,
            "contest_official_results",
            "group_id",
            "TEXT NOT NULL DEFAULT ''",
        )
        _add_col(conn, "contest_official_results", "rank_in_group", "INTEGER")

    if "contest_entries" in tables:
        for col, decl in (
            ("group_id", "TEXT NOT NULL DEFAULT ''"),
            ("seed", "INTEGER NOT NULL DEFAULT 0"),
            ("eliminated", "INTEGER NOT NULL DEFAULT 0"),
            ("dispatched_at", "TEXT"),
            ("real_name_snapshot", "TEXT"),
            ("phone_snapshot", "TEXT"),
            ("school_snapshot", "TEXT"),
            ("student_id_snapshot", "TEXT"),
            ("identity_captured_at", "TEXT"),
            ("identity_source", "TEXT"),
        ):
            _add_col(conn, "contest_entries", col, decl)

    if "contest_pairings" in tables:
        for col, decl in (
            ("stage_idx", "INTEGER NOT NULL DEFAULT 0"),
            ("stage_key", "TEXT NOT NULL DEFAULT ''"),
            ("group_id", "TEXT NOT NULL DEFAULT ''"),
            ("bracket_slot", "INTEGER"),
            ("color_first", "INTEGER NOT NULL DEFAULT 0"),
            ("scheduled_at", "TEXT"),  # 计划开赛时间（逐场排期；NULL=立即可打）
        ):
            _add_col(conn, "contest_pairings", col, decl)

    if "bots" in tables:
        _add_col(conn, "bots", "game_id", "TEXT NOT NULL DEFAULT 'holdem'")
        _add_col(conn, "bots", "owner_deleted_at", "TEXT")
        # Botzone 运行模式（上传时标明，runner 据此选传输路径）
        _add_col(
            conn,
            "bots",
            "runtime_mode",
            f"TEXT NOT NULL DEFAULT '{DEFAULT_RUNTIME_MODE}'",
        )
        # 下线私有 bot 功能（全局只有「公开」一种状态）：旧库的 is_public 列先转公开
        # 再 DROP COLUMN（保数据不丢）。幂等：列已不存在则跳过。
        if "is_public" in _table_cols(conn, "bots"):
            conn.execute("UPDATE bots SET is_public=1 WHERE is_public=0")
            conn.execute("ALTER TABLE bots DROP COLUMN is_public")

    # bot_versions 加 runtime_mode（每版本独立标明，回滚时恢复该版本的运行模式）
    if "bot_versions" in tables:
        _add_col(
            conn,
            "bot_versions",
            "runtime_mode",
            f"TEXT NOT NULL DEFAULT '{DEFAULT_RUNTIME_MODE}'",
        )

    if "users" in tables:
        _add_col(conn, "users", "bio", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "avatar", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "xp", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "users", "level", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "users", "last_active_at", "TEXT")
        _add_col(conn, "users", "real_name", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "phone", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "school", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "student_id", "TEXT NOT NULL DEFAULT ''")

    if "matches" in tables:
        # 全面解耦 PR3：旧单表 matches 拆成每游戏一张表（matches_holdem/gomoku/pencil）
        # + matches_index 定位表。按用户决策：**对局数据不保留**（可后续跑种子脚本重建），
        # 用户/Bot/赛事/评论/评分等数据保留。故这里直接 DROP 旧表，由 SCHEMA 建新表。
        # 同时清空引用旧 matches 的关联数据（match_replays、contest_pairings.match_id）。
        # 注意顺序：先清 contest_pairings.match_id（此时 matches 还在，FK 校验通过——
        # 置 NULL 不触发 FK 拒绝），再 DROP matches（被引用表删除时 SQLite 不校验 FK）。
        cp_cols = _table_cols(conn, "contest_pairings") if "contest_pairings" in tables else set()
        if "match_id" in cp_cols:
            conn.execute("UPDATE contest_pairings SET match_id=NULL WHERE match_id IS NOT NULL")
        conn.execute("DROP TABLE IF EXISTS matches")
        # 注意：不 DROP match_replays——SCHEMA executescript 已用 IF NOT EXISTS 建空表，
        # 若此处 DROP 会让迁移当次进程内 match_replays 缺失到下次重启。旧 replay 数据
        # 本就随对局丢弃（重建库），无需 DROP 再建。
        # 新三张表 + matches_index 由 SCHEMA executescript 创建（IF NOT EXISTS 幂等）

    # pair_stats 补胜负计数列（head-to-head 战绩用）
    if "pair_stats" in tables:
        for col, decl in (
            ("a_wins", "INTEGER NOT NULL DEFAULT 0"),
            ("a_losses", "INTEGER NOT NULL DEFAULT 0"),
            ("draws", "INTEGER NOT NULL DEFAULT 0"),
        ):
            _add_col(conn, "pair_stats", col, decl)
        # samples 的现行唯一语义是「双方已结算的对局数」。旧写入路径长期把它
        # 固定为 0，真实库因此无法按交手次数排序。胜/负/平已经是逐场、恰好一次
        # 结算的数据，直接以三者之和幂等回填即可。
        conn.execute(
            "UPDATE pair_stats SET samples=a_wins+a_losses+draws "
            "WHERE samples<>a_wins+a_losses+draws"
        )

    # ── ratings / rating_history 加 game_id 维度（全面解耦 PR3）──────────
    # 旧库 ratings PK = bot_id（无 game_id 列）；rating_history 无 game_id 列。
    # 迁移：加 game_id 列，按 bots.game_id 回填，重建表改 PK 为 (bot_id, game_id)。
    # 幂等：若 ratings 已有 game_id 列则跳过（新库 SCHEMA 直接建复合 PK）。
    if "ratings" in tables:
        r_cols = _table_cols(conn, "ratings")
        if "game_id" not in r_cols:
            legacy_delta_source = (
                "r.delta_total" if "delta_total" in r_cols else "r.net_chips"
            )
            # 重建 ratings：加 game_id 列 + 复合 PK，按 bots.game_id 回填
            conn.execute(
                """
                CREATE TABLE ratings_new (
                    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    game_id         TEXT    NOT NULL DEFAULT 'holdem',
                    rating          REAL    NOT NULL DEFAULT 1500.0,
                    rd              REAL    NOT NULL DEFAULT 350.0,
                    vol             REAL    NOT NULL DEFAULT 0.06,
                    wins            INTEGER NOT NULL DEFAULT 0,
                    losses          INTEGER NOT NULL DEFAULT 0,
                    draws           INTEGER NOT NULL DEFAULT 0,
                    delta_total     INTEGER NOT NULL DEFAULT 0,
                    matches_played  INTEGER NOT NULL DEFAULT 0,
                    last_played_at  TEXT,
                    PRIMARY KEY (bot_id, game_id)
                )
                """
            )
            # 回填：每行 game_id 取自 bots.game_id（bot 绑定单一游戏）。
            # 只迁移 bots 表里仍存在的 bot（丢弃孤儿 ratings 行，避免 FK 校验崩溃）。
            conn.execute(
                f"""
                INSERT INTO ratings_new
                    (bot_id, game_id, rating, rd, vol, wins, losses, draws,
                     delta_total, matches_played, last_played_at)
                SELECT r.bot_id, COALESCE(b.game_id, 'holdem'),
                       r.rating, r.rd, r.vol, r.wins, r.losses, r.draws,
                       {legacy_delta_source}, r.matches_played, r.last_played_at
                FROM ratings r
                LEFT JOIN bots b ON b.id = r.bot_id
                WHERE r.bot_id IN (SELECT id FROM bots)
                """
            )
            conn.execute("DROP TABLE ratings")
            conn.execute("ALTER TABLE ratings_new RENAME TO ratings")

    if "rating_history" in tables:
        rh_cols = _table_cols(conn, "rating_history")
        if "game_id" not in rh_cols:
            conn.execute(
                """
                CREATE TABLE rating_history_new (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    game_id         TEXT    NOT NULL DEFAULT 'holdem',
                    rating          REAL    NOT NULL,
                    rd              REAL    NOT NULL,
                    vol             REAL    NOT NULL,
                    matches_played  INTEGER NOT NULL,
                    reason          TEXT    NOT NULL DEFAULT '',
                    created_at      TEXT    NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO rating_history_new
                    (id, bot_id, game_id, rating, rd, vol, matches_played, reason, created_at)
                SELECT rh.id, rh.bot_id, COALESCE(b.game_id, 'holdem'),
                       rh.rating, rh.rd, rh.vol, rh.matches_played, rh.reason, rh.created_at
                FROM rating_history rh
                LEFT JOIN bots b ON b.id = rh.bot_id
                WHERE rh.bot_id IN (SELECT id FROM bots)
                """
            )
            conn.execute("DROP TABLE rating_history")
            conn.execute("ALTER TABLE rating_history_new RENAME TO rating_history")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rating_history_bot "
                "ON rating_history(bot_id, game_id, id DESC)"
            )

    # ── per-game matches 表 FK 加固（ON DELETE SET NULL，全面解耦审计 P0 修复）─────
    # 旧库分表后 bot_a_id/bot_b_id 无 ON DELETE 子句（SQLite 默认 RESTRICT）→
    # delete_bot 在 bot 参与过对局后抛 FOREIGN KEY constraint failed。
    # 检测并重建三表（SQLite 不能 ALTER FK，需 CREATE new→INSERT→DROP→RENAME）。
    # 对局数据可丢弃（用户决策），重建后为空也无妨。
    def _match_fk_has_set_null(conn, table: str) -> bool:
        """该 matches_<game> 表的 bot_a_id FK 是否已带 ON DELETE SET NULL。"""
        for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            # row: (id, seq, table, from, to, on_update, on_delete, match)
            if row["table"] == "bots" and row["from"] in ("bot_a_id", "bot_b_id"):
                if (row["on_delete"] or "").upper() != "SET NULL":
                    return False
        return True

    # 解耦审计 P0：从 games 注册表派生 game_id 列表（不得硬编码 ("holdem","gomoku","pencil")），
    # 否则新增第 4 游戏会静默漏掉 FK 重建 → delete_bot 在该表崩 FOREIGN KEY constraint failed。
    for _gid in _all_game_ids():
        _tbl = _matches_table(_gid)
        if _tbl in tables and not _match_fk_has_set_null(conn, _tbl):
            # FK 非 SET NULL → 重建（对局数据丢弃，与分表迁移一致）
            conn.execute(f"DROP TABLE IF EXISTS {_tbl}")
            conn.execute(_CREATE_MATCHES_TABLE_SQL.format(suffix=_gid))
            # 清理 matches_index 中指向该表的残留定位（表已空）
            conn.execute(
                "DELETE FROM matches_index WHERE game_id=?", (_gid,)
            )

    # Active rows do not have an adjudication result yet.  Older schemas filled
    # `reason='completed'` at INSERT time, and other damaged rows may contain
    # arbitrary diagnostics. Active rows have no adjudication yet, so clear any
    # non-empty value. Terminal rows are normalized separately to explicit
    # completed/error allow-lists below.
    for _gid in _all_game_ids():
        _tbl = _matches_table(_gid)
        if _tbl in tables:
            conn.execute(
                f"UPDATE {_tbl} SET reason='' "
                "WHERE status IN (?,?) AND reason<>''",
                (STATUS_PENDING, STATUS_RUNNING),
            )
            # Retired free-form/admin codes and ``error:<exception>`` strings
            # must not remain a second public terminal contract. Normalize the
            # one former admin spelling first, then map every unknown aborted
            # reason to the stable platform fallback. Completed rows likewise
            # retain only the explicit game/platform adjudication allow-list.
            conn.execute(
                f"UPDATE {_tbl} SET reason='admin_aborted' "
                "WHERE status=? AND reason='admin-abort'",
                (STATUS_ABORTED,),
            )
            allowed = tuple(sorted(PUBLIC_MATCH_ERROR_REASONS))
            placeholders = ",".join("?" for _ in allowed)
            conn.execute(
                f"UPDATE {_tbl} SET reason=? WHERE status=? "
                f"AND reason NOT IN ({placeholders})",
                (PUBLIC_MATCH_ERROR_FALLBACK, STATUS_ABORTED, *allowed),
            )
            completed_allowed = tuple(sorted(PUBLIC_MATCH_COMPLETED_REASONS))
            completed_placeholders = ",".join("?" for _ in completed_allowed)
            conn.execute(
                f"UPDATE {_tbl} SET reason='completed' WHERE status=? "
                f"AND reason NOT IN ({completed_placeholders})",
                (STATUS_COMPLETED, *completed_allowed),
            )

    # ── contest_* 表 bot FK 改 SET NULL + entry 身份列（预赛/决赛 P0：删 bot 不得抹成绩）──
    # 旧：bot FK 为 CASCADE（删 bot → 清报名/对阵/成绩）。新：SET NULL（删 bot → bot_id 置空，
    # entry/pairing/stage_results 保留，历史成绩不丢）。同时加 entry 身份列：
    #   contest_pairings.entry_a_id/entry_b_id（排名键，换 Bot 不丢分）
    #   contest_stage_results.entry_id（唯一键改 entry）
    def _bot_fk_is_set_null(conn, table: str) -> bool:
        """该表所有 bots FK 都已带 ON DELETE SET NULL。"""
        for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            if row["table"] == "bots" and (row["on_delete"] or "").upper() != "SET NULL":
                return False
        return True

    # 各表重建模板：bot FK = SET NULL + 新增 entry 身份列。列与 SCHEMA 一致。
    _CONTEST_TABLE_REBUILDS = {
        "contest_entries": (
            "CREATE TABLE {n}_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "bot_id INTEGER REFERENCES bots(id) ON DELETE SET NULL, "  # SET NULL：删 bot 留 entry
            "registered_at TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '', "
            "seed INTEGER NOT NULL DEFAULT 0, eliminated INTEGER NOT NULL DEFAULT 0, "
            "dispatched_at TEXT, real_name_snapshot TEXT, phone_snapshot TEXT, "
            "school_snapshot TEXT, student_id_snapshot TEXT, identity_captured_at TEXT, "
            "identity_source TEXT)",
            "contest_id, user_id, bot_id, registered_at, group_id, seed, eliminated, "
            "dispatched_at, real_name_snapshot, phone_snapshot, school_snapshot, "
            "student_id_snapshot, identity_captured_at, identity_source",
        ),
        "contest_pairings": (
            "CREATE TABLE {n}_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE, "
            "round_num INTEGER NOT NULL DEFAULT 1, "
            "entry_a_id INTEGER, entry_b_id INTEGER, "  # P0：entry 身份键（换 Bot 不丢分）
            "bot_a_id INTEGER REFERENCES bots(id) ON DELETE SET NULL, "
            "bot_b_id INTEGER REFERENCES bots(id) ON DELETE SET NULL, "
            "match_id TEXT, status TEXT NOT NULL DEFAULT 'pending', "
            "stage_idx INTEGER NOT NULL DEFAULT 0, stage_key TEXT NOT NULL DEFAULT '', "
            "group_id TEXT NOT NULL DEFAULT '', bracket_slot INTEGER, color_first INTEGER NOT NULL DEFAULT 0)",
            "id, contest_id, round_num, bot_a_id, bot_b_id, match_id, status, "
            "stage_idx, stage_key, group_id, bracket_slot, color_first",
        ),
        "contest_stage_results": (
            "CREATE TABLE {n}_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE, "
            "stage_idx INTEGER NOT NULL, stage_key TEXT NOT NULL DEFAULT '', "
            "entry_id INTEGER, "  # P0：排名键改 entry（唯一键含 entry_id）
            "bot_id INTEGER REFERENCES bots(id) ON DELETE SET NULL, "
            "points REAL NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0, "
            "draws INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0, "
            "delta_total INTEGER NOT NULL DEFAULT 0, group_id TEXT NOT NULL DEFAULT '', "
            "rank_in_group INTEGER, payload_json TEXT NOT NULL DEFAULT '{{}}', "
            "UNIQUE(contest_id, stage_idx, entry_id))",
            "id, contest_id, stage_idx, stage_key, bot_id, points, wins, draws, losses, "
            "delta_total, group_id, rank_in_group, payload_json",
        ),
    }
    for _ctable, (_ddl_tpl, _cols) in _CONTEST_TABLE_REBUILDS.items():
        # 触发重建：FK 非 SET NULL，或新身份列缺失
        _need = _ctable not in tables or not _bot_fk_is_set_null(conn, _ctable)
        if _ctable == "contest_pairings" and _ctable in tables:
            _need = _need or "entry_a_id" not in _table_cols(conn, _ctable)
        if _ctable == "contest_stage_results" and _ctable in tables:
            _need = _need or "entry_id" not in _table_cols(conn, _ctable)
            # P0 fix：旧迁移重建漏了 UNIQUE(contest_id,stage_idx,entry_id) → upsert ON CONFLICT 崩。
            # 检测表 DDL 是否含该 UNIQUE，缺则重建。
            if not _need:
                _ddl = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (_ctable,),
                ).fetchone()
                _ddl_text = (_ddl[0] if _ddl else "") or ""
                if "UNIQUE(contest_id, stage_idx, entry_id)" not in _ddl_text:
                    _need = True
        if not _need:
            continue
        # 清理上次失败残留的 _new 表（保幂等）
        conn.execute(f"DROP TABLE IF EXISTS {_ctable}_new")
        # 取实际存在的列（旧库可能少列），只迁移都有的
        _have = _table_cols(conn, _ctable) if _ctable in tables else set()
        _present = [c.strip() for c in _cols.split(",") if c.strip() in _have]
        _select = list(_present)
        # 旧 stage snapshot 在身份/FK 重建时先把历史筹码列映射到中性列，
        # 避免后续契约重建只能看到默认 0 而永久丢失破同分依据。
        if (
            _ctable == "contest_stage_results"
            and "delta_total" not in _have
            and "net_chips" in _have
        ):
            _present.append("delta_total")
            _select.append("net_chips")
        _col_list = ", ".join(_present)
        _select_list = ", ".join(_select)
        conn.execute(_ddl_tpl.format(n=_ctable))
        if _col_list:
            conn.execute(
                f"INSERT INTO {_ctable}_new ({_col_list}) "
                f"SELECT {_select_list} FROM {_ctable} "
                f"WHERE contest_id IN (SELECT id FROM contests)"
            )
        if _ctable in tables:
            conn.execute(f"DROP TABLE {_ctable}")
        conn.execute(f"ALTER TABLE {_ctable}_new RENAME TO {_ctable}")
        # 重建索引（SCHEMA 的 IF NOT EXISTS 不会对已 DROP 的表生效，手动补）
        if _ctable == "contest_entries":
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contest_entries_c ON contest_entries(contest_id)")
        elif _ctable == "contest_pairings":
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contest_pairings_c ON contest_pairings(contest_id)")
        elif _ctable == "contest_stage_results":
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contest_stage_results_c ON contest_stage_results(contest_id)")

    # Legacy contest_entries tables sometimes had only a plain contest_id index.
    # Concurrent registration uses ON CONFLICT(contest_id, user_id), which SQLite
    # rejects unless a real UNIQUE constraint/index exists.  Keep this after every
    # contest-table rebuild because rebuilding also drops the fresh-schema inline
    # UNIQUE constraint.
    if "contest_entries" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        # Historical duplicate registrations are corruption, but must not make an
        # upgrade impossible.  Preserve the earliest entry and repoint every durable
        # entry identity before removing duplicates.
        conn.execute("DROP TABLE IF EXISTS temp._contest_entry_dedup")
        conn.execute(
            "CREATE TEMP TABLE _contest_entry_dedup AS "
            "SELECT e.id AS drop_id, k.keep_id "
            "FROM contest_entries e "
            "JOIN (SELECT contest_id, user_id, MIN(id) AS keep_id "
            "      FROM contest_entries GROUP BY contest_id, user_id HAVING COUNT(*) > 1) k "
            "ON k.contest_id=e.contest_id AND k.user_id=e.user_id "
            "WHERE e.id<>k.keep_id"
        )
        _current_tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "contest_pairings" in _current_tables:
            for _entry_col in ("entry_a_id", "entry_b_id"):
                if _entry_col in _table_cols(conn, "contest_pairings"):
                    conn.execute(
                        f"UPDATE contest_pairings SET {_entry_col}=("
                        "SELECT keep_id FROM _contest_entry_dedup d "
                        f"WHERE d.drop_id=contest_pairings.{_entry_col}) "
                        f"WHERE {_entry_col} IN (SELECT drop_id FROM _contest_entry_dedup)"
                    )
        if (
            "contest_stage_results" in _current_tables
            and "entry_id" in _table_cols(conn, "contest_stage_results")
        ):
            # Resolve every row to its final keeper identity before the bulk UPDATE.
            # With 3+ duplicate entries, two drop rows may both have a result while
            # the keeper has none; checking only for an existing keeper row leaves
            # both rows alive and the UPDATE then violates the table UNIQUE key.
            # Prefer an existing keeper row, otherwise preserve the earliest result.
            conn.execute(
                "DELETE FROM contest_stage_results AS duplicate "
                "WHERE duplicate.entry_id IN (SELECT drop_id FROM _contest_entry_dedup) "
                "AND EXISTS (SELECT 1 FROM contest_stage_results preferred "
                "LEFT JOIN _contest_entry_dedup preferred_map "
                "ON preferred_map.drop_id=preferred.entry_id "
                "JOIN _contest_entry_dedup duplicate_map "
                "ON duplicate_map.drop_id=duplicate.entry_id "
                "WHERE preferred.contest_id=duplicate.contest_id "
                "AND preferred.stage_idx=duplicate.stage_idx "
                "AND COALESCE(preferred_map.keep_id, preferred.entry_id)="
                "duplicate_map.keep_id "
                "AND (preferred.entry_id=duplicate_map.keep_id "
                "OR preferred.id<duplicate.id))"
            )
            conn.execute(
                "UPDATE contest_stage_results SET entry_id=("
                "SELECT keep_id FROM _contest_entry_dedup d "
                "WHERE d.drop_id=contest_stage_results.entry_id) "
                "WHERE entry_id IN (SELECT drop_id FROM _contest_entry_dedup)"
            )
        if (
            "contest_official_results" in _current_tables
            and "entry_id" in _table_cols(conn, "contest_official_results")
        ):
            conn.execute(
                "DELETE FROM contest_official_results AS duplicate "
                "WHERE duplicate.entry_id IN (SELECT drop_id FROM _contest_entry_dedup) "
                "AND EXISTS (SELECT 1 FROM contest_official_results preferred "
                "LEFT JOIN _contest_entry_dedup preferred_map "
                "ON preferred_map.drop_id=preferred.entry_id "
                "JOIN _contest_entry_dedup duplicate_map "
                "ON duplicate_map.drop_id=duplicate.entry_id "
                "WHERE preferred.contest_id=duplicate.contest_id "
                "AND COALESCE(preferred_map.keep_id, preferred.entry_id)="
                "duplicate_map.keep_id "
                "AND (preferred.entry_id=duplicate_map.keep_id "
                "OR preferred.id<duplicate.id))"
            )
            conn.execute(
                "UPDATE contest_official_results SET entry_id=("
                "SELECT keep_id FROM _contest_entry_dedup d "
                "WHERE d.drop_id=contest_official_results.entry_id) "
                "WHERE entry_id IN (SELECT drop_id FROM _contest_entry_dedup)"
            )
        conn.execute(
            "DELETE FROM contest_entries "
            "WHERE id IN (SELECT drop_id FROM _contest_entry_dedup)"
        )
        conn.execute("DROP TABLE temp._contest_entry_dedup")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_contest_entries_contest_user "
            "ON contest_entries(contest_id, user_id)"
        )
        conn.execute(
            CONTEST_ENTRY_PAGE_INDEX_SQL.replace(
                "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1
            )
        )
        entry_page_index = conn.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?",
            ("idx_contest_entries_page_order",),
        ).fetchall()
        if (
            len(entry_page_index) != 1
            or str(entry_page_index[0]["type"]) != "index"
            or _normalize_schema_sql(str(entry_page_index[0]["sql"] or ""))
            != _normalize_schema_sql(CONTEST_ENTRY_PAGE_INDEX_SQL)
        ):
            raise RuntimeError(
                "contest entry page index definition mismatch"
            )

    # ── 跨游戏中性持久化列收敛 ─────────────────────────────
    # SQLite 删列与 FK/PK 变更统一用「新表→全量拷贝→换名」，
    # 并位于 Store.__init__ 的单一事务中；任一语句失败都会回滚，
    # 不会留下半套 schema。旧名只在此迁移边界读取，运行契约不再兼容。
    _contract_tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }

    if "ratings" in _contract_tables:
        _rating_cols = _table_cols(conn, "ratings")
        if "net_chips" in _rating_cols or "delta_total" not in _rating_cols:
            _delta_source = (
                "delta_total"
                if "delta_total" in _rating_cols
                else "net_chips"
                if "net_chips" in _rating_cols
                else "0"
            )
            conn.execute("DROP TABLE IF EXISTS ratings_contract_new")
            conn.execute(
                """
                CREATE TABLE ratings_contract_new (
                    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    game_id         TEXT    NOT NULL,
                    rating          REAL    NOT NULL DEFAULT 1500.0,
                    rd              REAL    NOT NULL DEFAULT 350.0,
                    vol             REAL    NOT NULL DEFAULT 0.06,
                    wins            INTEGER NOT NULL DEFAULT 0,
                    losses          INTEGER NOT NULL DEFAULT 0,
                    draws           INTEGER NOT NULL DEFAULT 0,
                    delta_total     INTEGER NOT NULL DEFAULT 0,
                    matches_played  INTEGER NOT NULL DEFAULT 0,
                    last_played_at  TEXT,
                    PRIMARY KEY (bot_id, game_id)
                )
                """
            )
            conn.execute(
                "INSERT INTO ratings_contract_new"
                "(bot_id,game_id,rating,rd,vol,wins,losses,draws,delta_total,"
                "matches_played,last_played_at) "
                "SELECT bot_id,game_id,rating,rd,vol,wins,losses,draws,"
                f"{_delta_source},matches_played,last_played_at FROM ratings"
            )
            conn.execute("DROP TABLE ratings")
            conn.execute("ALTER TABLE ratings_contract_new RENAME TO ratings")

    if "pair_stats" in _contract_tables:
        _pair_cols = _table_cols(conn, "pair_stats")
        _retired_pair_cols = {"bb_per_100_mean", "ci_low", "ci_high"}
        if _retired_pair_cols.intersection(_pair_cols):
            conn.execute("DROP TABLE IF EXISTS pair_stats_contract_new")
            conn.execute(
                """
                CREATE TABLE pair_stats_contract_new (
                    bot_a_id       INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    bot_b_id       INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    samples        INTEGER NOT NULL DEFAULT 0,
                    last_played_at TEXT    NOT NULL,
                    a_wins         INTEGER NOT NULL DEFAULT 0,
                    a_losses       INTEGER NOT NULL DEFAULT 0,
                    draws          INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (bot_a_id, bot_b_id)
                )
                """
            )
            conn.execute(
                "INSERT INTO pair_stats_contract_new"
                "(bot_a_id,bot_b_id,samples,last_played_at,a_wins,a_losses,draws) "
                "SELECT bot_a_id,bot_b_id,a_wins+a_losses+draws,last_played_at,"
                "a_wins,a_losses,draws FROM pair_stats"
            )
            conn.execute("DROP TABLE pair_stats")
            conn.execute("ALTER TABLE pair_stats_contract_new RENAME TO pair_stats")

    if "match_replays" in _contract_tables:
        _replay_cols = _table_cols(conn, "match_replays")
        if "hands_json" in _replay_cols:
            conn.execute("DROP TABLE IF EXISTS match_replays_contract_new")
            conn.execute(
                """
                CREATE TABLE match_replays_contract_new (
                    match_id    TEXT PRIMARY KEY,
                    events_json TEXT NOT NULL DEFAULT '[]',
                    updated_at  TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "INSERT INTO match_replays_contract_new(match_id,events_json,updated_at) "
                "SELECT match_id,events_json,updated_at FROM match_replays"
            )
            conn.execute("DROP TABLE match_replays")
            conn.execute(
                "ALTER TABLE match_replays_contract_new RENAME TO match_replays"
            )

    if "contest_stage_results" in _contract_tables:
        _stage_cols = _table_cols(conn, "contest_stage_results")
        if "net_chips" in _stage_cols or "delta_total" not in _stage_cols:
            _stage_delta_source = (
                "delta_total"
                if "delta_total" in _stage_cols
                else "net_chips"
                if "net_chips" in _stage_cols
                else "0"
            )
            conn.execute("DROP TABLE IF EXISTS contest_stage_results_contract_new")
            conn.execute(
                """
                CREATE TABLE contest_stage_results_contract_new (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    contest_id     INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
                    stage_idx      INTEGER NOT NULL,
                    stage_key      TEXT    NOT NULL DEFAULT '',
                    entry_id       INTEGER,
                    bot_id         INTEGER REFERENCES bots(id) ON DELETE SET NULL,
                    points         REAL    NOT NULL DEFAULT 0,
                    wins           INTEGER NOT NULL DEFAULT 0,
                    draws          INTEGER NOT NULL DEFAULT 0,
                    losses         INTEGER NOT NULL DEFAULT 0,
                    delta_total    INTEGER NOT NULL DEFAULT 0,
                    group_id       TEXT    NOT NULL DEFAULT '',
                    rank_in_group  INTEGER,
                    payload_json   TEXT    NOT NULL DEFAULT '{}',
                    UNIQUE(contest_id, stage_idx, entry_id)
                )
                """
            )
            conn.execute(
                "INSERT INTO contest_stage_results_contract_new"
                "(id,contest_id,stage_idx,stage_key,entry_id,bot_id,points,wins,"
                "draws,losses,delta_total,group_id,rank_in_group,payload_json) "
                "SELECT id,contest_id,stage_idx,stage_key,entry_id,bot_id,points,wins,"
                f"draws,losses,{_stage_delta_source},group_id,rank_in_group,payload_json "
                "FROM contest_stage_results"
            )
            conn.execute("DROP TABLE contest_stage_results")
            conn.execute(
                "ALTER TABLE contest_stage_results_contract_new "
                "RENAME TO contest_stage_results"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_contest_stage_results_c "
                "ON contest_stage_results(contest_id)"
            )

    # ── per-game matches 表自动建（解耦审计：让"新增第 4 游戏"真正零改动 DB）────────
    # schema.py 里 matches_holdem/gomoku/pencil 三张表是字面 CREATE 语句；新增注册游戏
    # （如 reversi）后 SCHEMA executescript 不会建 matches_<new>，create_match 会崩
    # `no such table`。这里对每个已注册游戏幂等建表 + 索引（CREATE TABLE IF NOT EXISTS），
    # 让 DB 层随注册表自动扩展，无需手改 schema.py 的 DDL。
    # 每游戏表的统一索引列（与 schema.py:404-421 的三套索引一一对应）。
    _PER_GAME_INDEX_COLS = ("bot_a_id", "bot_b_id", "owner_id", "contest_id", "status", "created_at")
    for _gid in _all_game_ids():
        _tbl = _matches_table(_gid)
        if _tbl not in tables:
            conn.execute(_CREATE_MATCHES_TABLE_SQL.format(suffix=_gid))
    # 重新读取当前物理表集合（上面的建表/FK 重建可能改变了它），再幂等补索引。
    _tables_after = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    _missing_registered_tables = sorted(
        _matches_table(gid)
        for gid in _all_game_ids()
        if _matches_table(gid) not in _tables_after
    )
    assert not _missing_registered_tables, (
        "注册表里的游戏缺物理表（_migrate 自动建表应覆盖此场景）："
        f"{_missing_registered_tables}。检查 games/__init__.py 注册 vs schema.py DDL。"
    )
    for _gid in _all_game_ids():
        _tbl = _matches_table(_gid)
        if _tbl not in _tables_after:
            continue  # 表确实没建出来（如 _CREATE_MATCHES_TABLE_SQL 被破坏）→ 跳过，交给启动断言报错
        for _col in _PER_GAME_INDEX_COLS:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_m{_gid}_{_col} ON {_tbl}({_col})"
            )
        # P4：matches 表加 match_seed + technical_loss 列（幂等）
        _add_col(conn, _tbl, "match_seed", "INTEGER")
        _add_col(conn, _tbl, "technical_loss", "INTEGER NOT NULL DEFAULT 0")
        # match_config + result 双 JSON 通路收敛（删 total_hands/n_dots/旧进度列/
        # earnings_a/earnings_b/旧标准化列）。历史规则值仅归档进 JSON 后 DROP，
        # 现行编排不会读取或应用它们；幂等：列已不存在则跳过。
        _add_col(conn, _tbl, "match_config", "TEXT NOT NULL DEFAULT '{}'")
        _add_col(conn, _tbl, "result", "TEXT NOT NULL DEFAULT '{}'")
        _mcols = _table_cols(conn, _tbl)
        # 历史库可能绕过了现行 schema，留下非法 JSON 或非 object JSON。先把它们
        # 规范为空对象，避免下面的 json_set/json_type 在迁移中途抛错。物理旧列仅
        # 用于补齐 JSON 尚不存在的新键；若新键已存在（即使值为 JSON null），新
        # 契约值始终优先，绝不能被旧列覆盖。
        conn.execute(
            f"UPDATE {_tbl} SET result='{{}}' "
            "WHERE result IS NULL OR json_valid(result)=0"
        )
        conn.execute(
            f"UPDATE {_tbl} SET result='{{}}' "
            "WHERE COALESCE(json_type(result),'') <> 'object'"
        )
        if "total_hands" in _mcols:
            # 历史归档：total_hands → match_config.hands（按行原值，不再作为规则输入）
            conn.execute(
                f"UPDATE {_tbl} SET match_config=json_set(match_config,'$.hands',total_hands) "
                "WHERE total_hands IS NOT NULL"
            )
            conn.execute(f"ALTER TABLE {_tbl} DROP COLUMN total_hands")
        if "n_dots" in _mcols:
            # 历史归档：n_dots → match_config.n_dots（按行原值，不再作为规则输入）
            conn.execute(
                f"UPDATE {_tbl} SET match_config=json_set(match_config,'$.n_dots',n_dots) "
                "WHERE n_dots IS NOT NULL"
            )
            conn.execute(f"ALTER TABLE {_tbl} DROP COLUMN n_dots")
        if "hands_played" in _mcols:
            # 旧物理进度列先归档到唯一中性键；零轮技术判负也必须保留。
            conn.execute(
                f"UPDATE {_tbl} SET result=json_set(result,'$.rounds_played',hands_played) "
                "WHERE hands_played IS NOT NULL "
                "AND json_type(result,'$.rounds_played') IS NULL"
            )
        if "earnings_a" in _mcols or "earnings_b" in _mcols:
            _earnings_a_source = "earnings_a" if "earnings_a" in _mcols else "0"
            _earnings_b_source = "earnings_b" if "earnings_b" in _mcols else "0"
            _earnings_where = " OR ".join(
                f"{column} IS NOT NULL"
                for column in ("earnings_a", "earnings_b")
                if column in _mcols
            )
            conn.execute(
                f"UPDATE {_tbl} SET result=json_set(result,'$.deltas',"
                f"json_array({_earnings_a_source},{_earnings_b_source})) "
                f"WHERE ({_earnings_where}) "
                "AND json_type(result,'$.deltas') IS NULL"
            )
        if {
            "hands_played",
            "earnings_a",
            "earnings_b",
            "net_bb_a",
        }.intersection(_mcols):
            for _dead in ("hands_played", "earnings_a", "earnings_b", "net_bb_a"):
                if _dead in _table_cols(conn, _tbl):
                    conn.execute(f"ALTER TABLE {_tbl} DROP COLUMN {_dead}")

    # ── 三张对局表 result JSON 收敛为唯一公共契约 ────────────────
    # 所有游戏走注册表能力和同一 builder；旧键仅在此升级边界被读取，更新后的
    # JSON 不再残留兼容别名。完成态即使是零轮技术判负也必须拥有三个公共字段。
    from bzplat.backend.games import registry as _game_registry
    from bzplat.backend.matches.result_contract import (
        build_result_payload as _build_result_payload,
        canonical_deltas as _canonical_deltas,
    )

    _retired_result_keys = {
        "hands_played",
        "net_bb",
        "net_bb_per_100",
    }
    # 结果单位/进度属于 GameSpec 能力；只处理实际装配了 spec 的游戏。
    # DB 物理表扩展测试可独立模拟 schema ID，而不会迫使迁移猜测一个不存在的游戏契约。
    for _gid in _game_registry.all_ids():
        _tbl = _matches_table(_gid)
        if _tbl not in _tables_after:
            continue
        _spec = _game_registry.get(_gid)
        for _match_row in conn.execute(
            f"SELECT id,status,winner,technical_loss,result FROM {_tbl}"
        ).fetchall():
            _raw_text = _match_row["result"]
            try:
                _raw_result = json.loads(_raw_text) if _raw_text else {}
            except (TypeError, ValueError):
                _raw_result = {}
            if not isinstance(_raw_result, dict):
                _raw_result = {}

            _has_legacy_rounds = (
                "rounds_played" in _raw_result
                or "hands_played" in _raw_result
            )
            _rounds_candidate = _raw_result.get(
                "rounds_played", _raw_result.get("hands_played", 0)
            )
            if (
                isinstance(_rounds_candidate, bool)
                or not isinstance(_rounds_candidate, int)
                or _rounds_candidate < 0
            ):
                _rounds_candidate = 0
            elif (
                not _has_legacy_rounds
                and _match_row["status"] == STATUS_COMPLETED
                and _match_row["technical_loss"] in (0, False)
                and _spec.fixed_rounds_per_match is not None
            ):
                # Old normal Holdem rows predate the public progress field.  A
                # missing value has one unambiguous GameSpec default; explicit
                # zero/malformed values remain untouched and fail closed.  A
                # legacy duplicate stores two physical games in ``legs``.
                _legacy_legs = _raw_result.get("legs")
                _game_count = (
                    len(_legacy_legs)
                    if isinstance(_legacy_legs, list) and _legacy_legs
                    else 1
                )
                _rounds_candidate = (
                    _spec.fixed_rounds_per_match * _game_count
                )

            try:
                _migrated_deltas = _canonical_deltas(_raw_result.get("deltas"))
            except (TypeError, ValueError):
                _winner = _match_row["winner"]
                _migrated_deltas = (
                    [1, -1]
                    if _winner == 0
                    else [-1, 1]
                    if _winner == 1
                    else [0, 0]
                )

            _extras = {
                key: value
                for key, value in _raw_result.items()
                if key
                not in {
                    "rounds_played",
                    "deltas",
                    "normalized_delta",
                    *_retired_result_keys,
                }
            }
            if _match_row["status"] == STATUS_COMPLETED:
                _migrated_result = _build_result_payload(
                    _spec,
                    rounds_played=_rounds_candidate,
                    deltas=_migrated_deltas,
                    extra=_extras,
                )
            else:
                # pending/running/aborted 没有已裁决公共结果：删除公共/退役键，
                # 仅保留非公共内部扩展，不能捏造一个完成态结果。
                _migrated_result = _extras

            if _migrated_result != _raw_result:
                # Once a settlement exists these bytes are immutable replay
                # source.  Legacy normalization may report a cosmetic delta,
                # but must never rewrite an already-settled event in place.
                if conn.execute(
                    "SELECT 1 FROM match_rating_settlements WHERE match_id=?",
                    (_match_row["id"],),
                ).fetchone():
                    continue
                conn.execute(
                    f"UPDATE {_tbl} SET result=? WHERE id=?",
                    (
                        json.dumps(
                            _migrated_result,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        _match_row["id"],
                    ),
                )

    # 正式榜只换破同分字段名，不重算 rank/points/排序；因此升级前后名次顺序严格不变。
    if "contest_official_results" in _tables_after:
        for _official_row in conn.execute(
            "SELECT id,tiebreaks_json FROM contest_official_results"
        ).fetchall():
            try:
                _tiebreaks = json.loads(_official_row["tiebreaks_json"] or "{}")
            except (TypeError, ValueError):
                _tiebreaks = {}
            if not isinstance(_tiebreaks, dict):
                _tiebreaks = {}
            _before_tiebreaks = dict(_tiebreaks)
            if "normalized_delta" not in _tiebreaks and "net_bb_per_100" in _tiebreaks:
                _tiebreaks["normalized_delta"] = _tiebreaks["net_bb_per_100"]
            _tiebreaks.pop("net_bb_per_100", None)
            if _tiebreaks != _before_tiebreaks:
                conn.execute(
                    "UPDATE contest_official_results SET tiebreaks_json=? WHERE id=?",
                    (
                        json.dumps(
                            _tiebreaks,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        _official_row["id"],
                    ),
                )

    # comments/likes 使用跨表多态 target_id，无法声明数据库外键。升级时删除旧版
    # 留下的未知类型和悬空目标，并以 likes 真相重算每场缓存计数。删完悬空
    # comment 后再清理 comment-like，避免「评论已删但点赞永存」。
    if "comments" in _tables_after and "likes" in _tables_after:
        valid_match_ids_sql = " UNION ALL ".join(
            f"SELECT id FROM {_matches_table(gid)}"
            for gid in sorted(_all_game_ids())
            if _matches_table(gid) in _tables_after
        )
        conn.execute(
            "DELETE FROM comments WHERE target_type NOT IN ('match','bot')"
        )
        conn.execute(
            "DELETE FROM comments WHERE user_id NOT IN (SELECT id FROM users)"
        )
        conn.execute(
            "DELETE FROM comments WHERE target_type='bot' AND target_id NOT IN "
            "(SELECT CAST(id AS TEXT) FROM bots)"
        )
        if valid_match_ids_sql:
            conn.execute(
                "DELETE FROM comments WHERE target_type='match' AND target_id NOT IN ("
                f"{valid_match_ids_sql})"
            )
        conn.execute(
            "DELETE FROM likes WHERE target_type NOT IN ('match','bot','comment')"
        )
        conn.execute(
            "DELETE FROM likes WHERE user_id NOT IN (SELECT id FROM users)"
        )
        conn.execute(
            "DELETE FROM likes WHERE target_type='bot' AND target_id NOT IN "
            "(SELECT CAST(id AS TEXT) FROM bots)"
        )
        conn.execute(
            "DELETE FROM likes WHERE target_type='comment' AND target_id NOT IN "
            "(SELECT CAST(id AS TEXT) FROM comments)"
        )
        if valid_match_ids_sql:
            conn.execute(
                "DELETE FROM likes WHERE target_type='match' AND target_id NOT IN ("
                f"{valid_match_ids_sql})"
            )
        for _gid in _all_game_ids():
            _tbl = _matches_table(_gid)
            if _tbl in _tables_after:
                conn.execute(
                    f"UPDATE {_tbl} SET likes_count=(SELECT COUNT(*) FROM likes "
                    f"WHERE target_type='match' AND target_id={_tbl}.id)"
                )
    if "follows" in _tables_after:
        conn.execute(
            "DELETE FROM follows WHERE follower_id NOT IN (SELECT id FROM users) "
            "OR followee_id NOT IN (SELECT id FROM users)"
        )
    if "favorites" in _tables_after:
        conn.execute(
            "DELETE FROM favorites WHERE user_id NOT IN (SELECT id FROM users) "
            "OR bot_id NOT IN (SELECT id FROM bots)"
        )

    # ── 去重：删 schema.py 旧字面索引（与上面 _PER_GAME_INDEX_COLS 循环建的重复）─────
    # 旧索引名后缀 bot_a/bot_b/owner/contest/time（无 _id/_at）；
    # 新索引名后缀为完整列名（bot_a_id/owner_id/contest_id/created_at）。
    # 注意 status 列两套同名（都是 idx_m{g}_status），不能删（删了会误删新索引）。
    # 幂等：DROP INDEX IF EXISTS 对不存在的索引是 no-op。
    _LEGACY_IDX_SUFFIXES = ("bot_a", "bot_b", "owner", "contest", "time")
    for _gid in _all_game_ids():
        for _suf in _LEGACY_IDX_SUFFIXES:
            conn.execute(f"DROP INDEX IF EXISTS idx_m{_gid}_{_suf}")

    # ── 清理已下线游戏的残留 matches_<game> 表（审计：生产 matches_reversi 孤儿）──
    # reversi 在 commit f1c92fc 下线，但生产库表残留。泛化：任何 matches_* 表
    # 若其 game_id 不在注册表，则 DROP（数据随游戏下线一并丢弃，与 reversi 决策一致）。
    # 安全：matches_index 是路由表（非 matches_<game> 形式），显式排除防误删。
    _registered = {f"matches_{gid}" for gid in _all_game_ids()}
    for (_name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'matches\\_%' ESCAPE '\\'"
    ):
        if _name not in _registered and _name != "matches_index":
            conn.execute(f"DROP TABLE IF EXISTS {_name}")

    # ── contest_pairings 轮次冻结列（预赛/决赛 P1：版本/seed/发布闸门）────────
    if "contest_pairings" in _tables_after:
        # Some legacy FK/identity rebuilds above intentionally reconstruct the
        # narrow historical table and can drop columns added earlier in this
        # same migration pass.  Reassert scheduler columns at the final index
        # boundary so first-open upgrades do not require a second reopen.
        _add_col(conn, "contest_pairings", "scheduled_at", "TEXT")
        _add_col(conn, "contest_pairings", "bot_a_version_id", "INTEGER")
        _add_col(conn, "contest_pairings", "bot_b_version_id", "INTEGER")
        _add_col(conn, "contest_pairings", "pairing_seed", "INTEGER")
        _add_col(conn, "contest_pairings", "published_at", "TEXT")
        _add_col(
            conn,
            "contest_pairings",
            "series_index",
            "INTEGER NOT NULL DEFAULT 1 CHECK(series_index>=1)",
        )
        _add_col(
            conn,
            "contest_pairings",
            "series_size",
            "INTEGER NOT NULL DEFAULT 1 CHECK(series_size>=1)",
        )
        _add_col(
            conn,
            "contest_pairings",
            "tiebreak_group",
            "INTEGER NOT NULL DEFAULT 0 CHECK(tiebreak_group>=0)",
        )
        _add_col(
            conn,
            "contest_pairings",
            "tiebreak_game",
            "INTEGER NOT NULL DEFAULT 0 CHECK(tiebreak_game>=0)",
        )
        duplicate_binding = conn.execute(
            "SELECT match_id, COUNT(*) AS n FROM contest_pairings "
            "WHERE match_id IS NOT NULL GROUP BY match_id HAVING COUNT(*)>1 "
            "LIMIT 1"
        ).fetchone()
        if duplicate_binding:
            raise RuntimeError(
                "contest_pairings 存在重复 match_id 绑定，必须先修复: "
                f"{duplicate_binding['match_id']} ({duplicate_binding['n']} rows)"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_contest_pairings_match_unique "
            "ON contest_pairings(match_id) WHERE match_id IS NOT NULL"
        )
        # Seed allocation/validation runs once per physical pairing.  Unlimited
        # round-robin stages therefore require a covering lookup instead of a
        # full stage scan per row.  It is intentionally non-unique because the
        # two games in one KO paired-swap tiebreak share the same private seed.
        conn.execute(
            CONTEST_PAIRING_SEED_LOOKUP_INDEX_SQL.replace(
                "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1
            )
        )
        seed_index = conn.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?",
            ("idx_contest_pairings_seed_lookup",),
        ).fetchall()
        if (
            len(seed_index) != 1
            or str(seed_index[0]["type"]) != "index"
            or _normalize_schema_sql(str(seed_index[0]["sql"] or ""))
            != _normalize_schema_sql(CONTEST_PAIRING_SEED_LOOKUP_INDEX_SQL)
        ):
            raise RuntimeError(
                "contest pairing seed lookup index definition mismatch"
            )
        # The scheduler must be able to reject an unfinished stage and locate
        # due work from the indexed prefix without materialising an O(n^2)
        # round-robin schedule on every tick.
        conn.execute(
            CONTEST_PAIRING_SCHEDULE_INDEX_SQL.replace(
                "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1
            )
        )
        schedule_index = conn.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?",
            ("idx_contest_pairings_schedule",),
        ).fetchall()
        if (
            len(schedule_index) != 1
            or str(schedule_index[0]["type"]) != "index"
            or _normalize_schema_sql(str(schedule_index[0]["sql"] or ""))
            != _normalize_schema_sql(CONTEST_PAIRING_SCHEDULE_INDEX_SQL)
        ):
            raise RuntimeError(
                "contest pairing schedule index definition mismatch"
            )
        conn.execute(
            CONTEST_PAIRING_SYNC_INDEX_SQL.replace(
                "CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1
            )
        )
        sync_index = conn.execute(
            "SELECT type,sql FROM sqlite_master WHERE name=?",
            ("idx_contest_pairings_completion_sync",),
        ).fetchall()
        if (
            len(sync_index) != 1
            or str(sync_index[0]["type"]) != "index"
            or _normalize_schema_sql(str(sync_index[0]["sql"] or ""))
            != _normalize_schema_sql(CONTEST_PAIRING_SYNC_INDEX_SQL)
        ):
            raise RuntimeError(
                "contest pairing completion sync index definition mismatch"
            )
        duplicate_coordinate = conn.execute(
            "SELECT contest_id,stage_idx,round_num,bracket_slot,"
            "tiebreak_group,tiebreak_game,COUNT(*) AS n "
            "FROM contest_pairings WHERE bracket_slot IS NOT NULL "
            "GROUP BY contest_id,stage_idx,round_num,bracket_slot,"
            "tiebreak_group,tiebreak_game HAVING COUNT(*)>1 LIMIT 1"
        ).fetchone()
        if duplicate_coordinate:
            raise RuntimeError(
                "contest_pairings 存在重复淘汰坐标，必须先修复: "
                f"contest={duplicate_coordinate['contest_id']} "
                f"stage={duplicate_coordinate['stage_idx']} "
                f"round={duplicate_coordinate['round_num']} "
                f"slot={duplicate_coordinate['bracket_slot']}"
            )
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS "
            "idx_contest_pairings_elimination_coordinate "
            "ON contest_pairings(contest_id,stage_idx,round_num,bracket_slot,"
            "tiebreak_group,tiebreak_game) WHERE bracket_slot IS NOT NULL"
        )

    # ── 全来源持久执行队列 v3 ─────────────────────────────────────────
    # Fresh schema 已创建 execution_*；升级库在这里一次性吸收旧 auto-only
    # lookahead。queued 可无损转换；旧 dispatched 的物理容器没有 instance
    # namespace，不能把“没看见新 label”伪装成清理证明，因此保留为仍占容量的
    # settling attempt 并把 dispatcher 置 manual pause，等待维护者确认旧平台已停后
    # 显式恢复；普通进程启动绝不跨过这一人工确认边界。
    tables_now = {
        row[0]
        for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    if "execution_jobs" in tables_now:
        _ensure_execution_environment_schema(conn)
        _add_col(
            conn,
            "execution_jobs",
            "failure_count",
            "INTEGER NOT NULL DEFAULT 0 CHECK (failure_count>=0)",
        )
        _add_col(conn, "execution_jobs", "next_attempt_at", "TEXT")
        for index_name, index_sql in (
            (
                "idx_execution_jobs_claim_source_order",
                EXECUTION_CLAIM_SOURCE_ORDER_INDEX_SQL,
            ),
            (
                "idx_execution_jobs_claim_contest_order",
                EXECUTION_CLAIM_CONTEST_ORDER_INDEX_SQL,
            ),
            (
                "idx_execution_jobs_contest_dispatch_gap",
                EXECUTION_CONTEST_DISPATCH_GAP_INDEX_SQL,
            ),
        ):
            conn.execute(
                index_sql.replace("CREATE INDEX ", "CREATE INDEX IF NOT EXISTS ", 1)
            )
            index_row = conn.execute(
                "SELECT type,sql FROM sqlite_master WHERE name=?",
                (index_name,),
            ).fetchall()
            if (
                len(index_row) != 1
                or str(index_row[0]["type"]) != "index"
                or _normalize_schema_sql(str(index_row[0]["sql"] or ""))
                != _normalize_schema_sql(index_sql)
            ):
                raise RuntimeError(f"execution queue index definition mismatch: {index_name}")
    if "auto_match_control" in tables_now:
        old_switch = conn.execute(
            "SELECT enabled FROM auto_match_control WHERE singleton=1"
        ).fetchone()
        if old_switch is not None:
            conn.execute(
                "UPDATE execution_control SET auto_enabled="
                "CASE WHEN deployment_drain_requested=1 THEN 0 ELSE ? END,"
                "updated_at=? "
                "WHERE singleton=1",
                (1 if int(old_switch["enabled"] or 0) else 0, _now()),
            )
    if "auto_match_decisions" in tables_now:
        _add_col(conn, "auto_match_decisions", "job_public_id", "TEXT")

    legacy_active = 0
    if "auto_match_queue" in tables_now:
        legacy_count = int(
            conn.execute("SELECT COUNT(*) FROM auto_match_queue").fetchone()[0]
        )
        legacy_rows = conn.execute(
            "SELECT q.*,d.created_at AS decision_created_at,"
            "d.lifecycle AS decision_lifecycle,d.match_id AS decision_match_id "
            "FROM auto_match_queue q JOIN auto_match_decisions d "
            "ON d.id=q.decision_id ORDER BY q.id"
        ).fetchall()
        if len(legacy_rows) != legacy_count:
            raise RuntimeError(
                "旧 auto_match_queue 存在缺失 decision 的孤儿行；"
                "为避免丢失执行请求，迁移已中止且不会删除旧队列表"
            )
        for legacy in legacy_rows:
            legacy_status = str(legacy["status"] or "")
            if legacy_status not in {"queued", "dispatched"}:
                raise RuntimeError(
                    f"旧执行队列状态不可迁移: {legacy_status!r}"
                )
            decision_id = int(legacy["decision_id"])
            digest = hashlib.sha256(
                f"legacy-auto:{decision_id}:{legacy['decision_created_at']}".encode(
                    "utf-8"
                )
            ).hexdigest()[:24]
            public_id = f"req_{digest}"
            is_active = legacy_status == "dispatched"
            match_id = str(legacy["match_id"] or "") or None
            decision_match_id = (
                str(legacy["decision_match_id"] or "") or None
            )
            expected_lifecycle = "dispatched" if is_active else "queued"
            if (
                str(legacy["decision_lifecycle"] or "") != expected_lifecycle
                or decision_match_id != match_id
            ):
                raise RuntimeError(
                    "旧自动排位 queue/decision 生命周期不一致；"
                    "为保留审计链，迁移已中止"
                )
            if is_active:
                game_id = str(legacy["game_id"] or "")
                if game_id not in _all_game_ids() or not match_id:
                    raise RuntimeError(
                        "旧 dispatched 执行缺少可识别的 game/match；迁移已中止"
                    )
                indexed = conn.execute(
                    "SELECT game_id FROM matches_index WHERE id=?", (match_id,)
                ).fetchone()
                physical = conn.execute(
                    f"SELECT 1 FROM {_matches_table(game_id)} WHERE id=?",
                    (match_id,),
                ).fetchone()
                if (
                    indexed is None
                    or str(indexed["game_id"] or "") != game_id
                    or physical is None
                ):
                    raise RuntimeError(
                        "旧 dispatched 执行引用的 match 索引或实体缺失；"
                        "迁移已中止且不会删除旧队列表"
                    )
            status = "settling" if is_active else "queued"
            retryable = 0
            terminal_reason = ""
            migration_error = "legacy_execution_unscoped" if is_active else ""
            conn.execute(
                "INSERT INTO execution_jobs("
                "public_id,source,status,priority,owner_user_id,game_id,match_type,"
                "bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id,"
                "bot_a_environment,bot_b_environment,bot_a_local_agent_id,"
                "bot_b_local_agent_id,match_config,rated,rating_reason,match_slots,"
                "sandbox_units,host_cpu_millis,host_memory_mb,profile_version,"
                "current_match_id,"
                "auto_decision_id,cancel_requested,attempt_count,cleanup_state,"
                "retryable,terminal_reason,last_error,created_at,claimed_at,"
                "settling_at,terminal_at) "
                "VALUES(?, 'auto', ?, 10, NULL, ?, 'ladder', ?, ?, ?, ?, "
                "'platform_low','platform_low',NULL,NULL,'{}',1,'eligible',1,"
                "2,2000,1024,0,?,?,0,?,? ,?,?,?, ?,?,?,?) "
                "ON CONFLICT(public_id) DO NOTHING",
                (
                    public_id,
                    status,
                    legacy["game_id"],
                    legacy["bot_a_id"],
                    legacy["bot_b_id"],
                    legacy["bot_a_version_id"],
                    legacy["bot_b_version_id"],
                    match_id if is_active else None,
                    decision_id,
                    1 if is_active else 0,
                    "pending" if is_active else "none",
                    retryable,
                    terminal_reason,
                    migration_error,
                    legacy["created_at"] or legacy["decision_created_at"] or _now(),
                    (
                        legacy["dispatched_at"]
                        or legacy["created_at"]
                        or legacy["decision_created_at"]
                        or _now()
                    )
                    if is_active
                    else None,
                    _now() if is_active else None,
                    None,
                ),
            )
            job = conn.execute(
                "SELECT id FROM execution_jobs WHERE public_id=?", (public_id,)
            ).fetchone()
            if is_active and job is not None and match_id:
                conn.execute(
                    "INSERT OR IGNORE INTO execution_job_attempts("
                    "job_id,attempt_no,match_id,status,events_observed,created_at,"
                    "terminal_at,terminal_reason) VALUES(?,1,?,'settling',0,?,NULL,?)",
                    (
                        int(job["id"]),
                        match_id,
                        legacy["dispatched_at"] or legacy["created_at"] or _now(),
                        migration_error,
                    ),
                )
                legacy_active += 1
            conn.execute(
                "UPDATE auto_match_decisions SET job_public_id=? WHERE id=?",
                (public_id, decision_id),
            )

    if legacy_active:
        conn.execute(
            "UPDATE execution_control SET dispatcher_state='paused',accepting="
            "CASE WHEN deployment_drain_requested=1 THEN 0 ELSE 1 END,"
            "pause_reason=?,retry_count=0,retry_at=NULL,updated_at=? WHERE singleton=1",
            (
                "manual:升级前存在未完成的无命名空间执行；"
                "确认旧平台已停服且旧容器已清理后由管理员恢复",
                _now(),
            ),
        )

    # 先移除旧活跃队列，再重建永久决策审计。execution_jobs 只保存 decision id
    # 数值映射，不声明反向 FK，所以表替换不会影响 job 或历史 match。
    conn.execute("DROP TABLE IF EXISTS auto_match_queue")
    if "auto_match_decisions" in tables_now and {
        "claim_dispatcher_token",
        "execution_scope",
        "execution_daemon_incarnation",
    }.intersection(_table_cols(conn, "auto_match_decisions")):
        conn.execute("DROP TABLE IF EXISTS auto_match_decisions_v3")
        conn.execute(
            "CREATE TABLE auto_match_decisions_v3("
            "id INTEGER PRIMARY KEY AUTOINCREMENT,policy_version TEXT NOT NULL,"
            "state_revision INTEGER NOT NULL,cursor_game_idx INTEGER NOT NULL,"
            "requested_lane TEXT NOT NULL,actual_lane TEXT NOT NULL,"
            "fallback_reason TEXT NOT NULL DEFAULT '',game_id TEXT NOT NULL,"
            "bot_a_id INTEGER NOT NULL,bot_b_id INTEGER NOT NULL,"
            "owner_a_id INTEGER NOT NULL,owner_b_id INTEGER NOT NULL,"
            "bot_a_version_id INTEGER NOT NULL,bot_b_version_id INTEGER NOT NULL,"
            "owner_a_service_before INTEGER NOT NULL,owner_b_service_before INTEGER NOT NULL,"
            "bot_a_service_before INTEGER NOT NULL,bot_b_service_before INTEGER NOT NULL,"
            "bot_pair_count_before INTEGER NOT NULL,owner_pair_count_before INTEGER NOT NULL,"
            "rating_gap REAL NOT NULL,bot_a_seat_debt_before INTEGER NOT NULL,"
            "bot_b_seat_debt_before INTEGER NOT NULL,selection_reason TEXT NOT NULL,"
            "lifecycle TEXT NOT NULL DEFAULT 'queued',match_id TEXT,"
            "attempt_count INTEGER NOT NULL DEFAULT 0,last_attempt_error TEXT NOT NULL DEFAULT '',"
            "created_at TEXT NOT NULL,dispatched_at TEXT,terminal_at TEXT,"
            "terminal_reason TEXT NOT NULL DEFAULT '',settlement_order INTEGER,job_public_id TEXT,"
            "CHECK(bot_a_id<>bot_b_id),CHECK(owner_a_id<>owner_b_id),"
            "CHECK(requested_lane IN ('bootstrap','established')),"
            "CHECK(actual_lane IN ('bootstrap','established')),"
            "CHECK(lifecycle IN ('queued','dispatched','completed','aborted','cancelled')))"
        )
        keep = (
            "id,policy_version,state_revision,cursor_game_idx,requested_lane,actual_lane,"
            "fallback_reason,game_id,bot_a_id,bot_b_id,owner_a_id,owner_b_id,"
            "bot_a_version_id,bot_b_version_id,owner_a_service_before,"
            "owner_b_service_before,bot_a_service_before,bot_b_service_before,"
            "bot_pair_count_before,owner_pair_count_before,rating_gap,"
            "bot_a_seat_debt_before,bot_b_seat_debt_before,selection_reason,"
            "lifecycle,match_id,attempt_count,last_attempt_error,created_at,dispatched_at,"
            "terminal_at,terminal_reason,settlement_order,job_public_id"
        )
        select_keep = keep.replace(
            "requested_lane,actual_lane,",
            "CASE requested_lane WHEN 'placement' THEN 'bootstrap' "
            "WHEN 'formal' THEN 'established' ELSE requested_lane END,"
            "CASE actual_lane WHEN 'placement' THEN 'bootstrap' "
            "WHEN 'formal' THEN 'established' ELSE actual_lane END,",
        )
        conn.execute(
            f"INSERT INTO auto_match_decisions_v3({keep}) "
            f"SELECT {select_keep} FROM auto_match_decisions"
        )
        conn.execute("DROP TABLE auto_match_decisions")
        conn.execute("ALTER TABLE auto_match_decisions_v3 RENAME TO auto_match_decisions")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_match_decisions_match "
            "ON auto_match_decisions(match_id) WHERE match_id IS NOT NULL"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_auto_match_decisions_created "
            "ON auto_match_decisions(id DESC)"
        )

    conn.execute("DROP TABLE IF EXISTS auto_match_dispatcher")
    conn.execute("DROP TABLE IF EXISTS auto_match_control")
    _add_col(conn, "rating_projection_state", "source_digest", "TEXT NOT NULL DEFAULT ''")
    _add_col(conn, "rating_projection_state", "projection_digest", "TEXT NOT NULL DEFAULT ''")
    _add_col(conn, "rating_projection_state", "plan_digest", "TEXT NOT NULL DEFAULT ''")
    _add_col(
        conn, "rating_projection_state", "mutation_revision",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _add_col(
        conn, "rating_projection_state", "trusted_mutation_revision",
        "INTEGER NOT NULL DEFAULT 0",
    )
    # 公平计数只从历史 completed system ladder 一次性引导；此后仅由 queue
    # terminal transaction 更新，绝不读取可被前台挑战影响的 ratings/pair_stats。
    _bootstrap_auto_match_fairness(conn)
    # The global dispatcher now owns all Docker uncertainty retry state.  The
    # auto-only circuit-breaker columns have no writer and must not survive as a
    # misleading second control plane.
    for _dead_auto_fair_column in ("platform_failures", "not_before"):
        if _dead_auto_fair_column in _table_cols(conn, "auto_match_fair_state"):
            conn.execute(
                f"ALTER TABLE auto_match_fair_state DROP COLUMN "
                f"{_dead_auto_fair_column}"
            )
    _add_col(
        conn,
        "auto_match_fair_state",
        "dispatch_policy_version",
        "TEXT NOT NULL DEFAULT ''",
    )
    _add_col(conn, "auto_match_fair_state", "next_eligible_at", "TEXT")
    _add_col(
        conn,
        "auto_match_fair_state",
        "gate_reason",
        "TEXT NOT NULL DEFAULT 'idle_grace'",
    )
    _ensure_match_rating_policy_identity(conn)
    _classify_legacy_match_rating_policies(conn)
    conn.execute("DROP TABLE IF EXISTS auto_match_daily_claims")
    # 唯一可变项只保留 enabled。旧调参记录若继续存在，会让运维误以为仍生效。
    conn.execute(
        "DELETE FROM platform_settings WHERE key IN ("
        "'auto_match_interval_sec','auto_match_min_idle_sec',"
        "'auto_match_bot_cooldown','auto_match_stale_sec',"
        "'auto_match_reserve_slots','auto_match_placement_games',"
        "'auto_match_max_per_round','auto_match_daily_cap','auto_match_enabled')"
    )
    _ensure_game_contract_state(conn, fresh_schema=fresh_schema)
    _ensure_ranked_bot_selection(conn, fresh_schema=fresh_schema)
    # Install owner-tombstone guards only after every legacy table rebuild and
    # the ranked-Bot migration have completed.  Very old ``bots`` tables do not
    # yet have ``is_ranked``; installing a trigger that references NEW.is_ranked
    # before that column exists makes any intervening ALTER TABLE fail.  Entry
    # and pairing rebuilds likewise drop their table-owned triggers, so the
    # migration tail is the one canonical installation boundary for both fresh
    # and upgraded schemas.
    current_tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    if {
        "contests",
        "contest_entries",
        "contest_pairings",
        "contest_stage_results",
    } <= current_tables:
        _invalidate_contest_seals_for_incomplete_lifecycle_epoch(conn)
    if "bots" in current_tables:
        _install_bot_owner_delete_triggers(conn)
        if "contest_entries" in current_tables:
            _install_contest_entry_live_bot_triggers(conn)
        if {
            "contests",
            "contest_entries",
            "contest_pairings",
        } <= current_tables:
            _install_contest_live_state_bot_trigger(conn)
    if {"contests", "contest_pairings", "execution_jobs"} <= current_tables:
        _install_contest_title_schema(conn)
        _install_contest_source_search_schema(conn)
        _install_contest_pairing_topology_triggers(conn)
    if {
        "contests",
        "contest_entries",
        "contest_stage_results",
    } <= current_tables:
        _install_contest_lifecycle_revision_triggers(conn)
    _install_rated_overlap_triggers(conn)

    # ── 非赛事 completed 对局评分结算凭据（恰好一次）────────────────────
    # 升级前的 completed 对局大多已经由旧后处理更新过 ratings，但没有 marker。
    # 若直接让启动恢复扫描它们，会把全部历史评分重复计算。首次迁移先把既有
    # completed 非赛事/非 human 对局回填为已结算，再写哨兵；二者随外层事务
    # 一起提交，失败可安全重试。新库首次初始化时没有对局，只写哨兵。
    migrated = conn.execute(
        "SELECT 1 FROM match_rating_settlements WHERE match_id=?",
        (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,),
    ).fetchone()
    if not migrated:
        migrated_at = _now()
        for _gid in _all_game_ids():
            _tbl = _matches_table(_gid)
            # 新游戏注册与物理表漂移由 Store.__init__ 的既有一致性断言给出
            # 明确诊断；迁移回填不能抢先以 no-such-table 模糊该错误。
            if _tbl not in _tables_after:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO match_rating_settlements(match_id, settled_at) "
                f"SELECT id, COALESCE(ended_at, created_at, ?) FROM {_tbl} "
                "WHERE status=? AND match_type NOT IN (?,?)",
                (migrated_at, STATUS_COMPLETED, TYPE_CONTEST, TYPE_HUMAN),
            )
        conn.execute(
            "INSERT INTO match_rating_settlements(match_id, settled_at) VALUES(?,?)",
            (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL, migrated_at),
        )
    _ensure_rating_settlement_sequence(conn)
    _install_rating_projection_mutation_triggers(conn)


def _certify_fresh_rating_projection(conn: sqlite3.Connection) -> None:
    """Trust the canonical empty projection on a genuinely new schema.

    Existing databases deliberately enter the offline rebuild workflow as
    ``legacy-unverified``.  A connection that had no application tables before
    ``SCHEMA`` ran cannot contain legacy rating business data, however, so
    forcing an operator-only cold-backup rebuild would leave every fresh
    installation unable to claim its first rated match.  Certify only that
    brand-new empty state; reopening or upgrading an existing schema never
    calls this helper.
    """
    live = rating_projection_digests(conn)
    if (
        live["issues"]
        or int(live["source_settlement_count"]) != 0
        or conn.execute("SELECT 1 FROM bots LIMIT 1").fetchone() is not None
        or conn.execute("SELECT 1 FROM ratings LIMIT 1").fetchone() is not None
        or conn.execute("SELECT 1 FROM rating_history LIMIT 1").fetchone() is not None
        or (
            conn.execute("SELECT 1 FROM pair_stats LIMIT 1").fetchone()
            is not None
        )
    ):
        raise RuntimeError("fresh rating projection is not canonically empty")
    conn.execute(
        "UPDATE rating_projection_state SET policy_version=?,rebuilt_at=?,"
        "source_settlement_count=?,source_last_settled_order=?,source_digest=?,"
        "projection_digest=?,plan_digest=?,trusted_mutation_revision=mutation_revision "
        "WHERE singleton=1",
        (
            _RATING_PROJECTION_POLICY_VERSION,
            _now(),
            int(live["source_settlement_count"]),
            int(live["source_last_settled_order"]),
            live["source_digest"],
            live["projection_digest"],
            live["plan_digest"],
        ),
    )


class Store:
    """SQLite 存储。线程安全；持久连接 check_same_thread=False。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        # FK 强制是 SQLite 的连接级设置（默认 OFF）。在连接处一次性开启，覆盖所有
        # 访问路径（_tx / 直接 _conn / 脚本 / 备份恢复）——_tx() 内的重复开启是冗余
        # no-op 但保留以明示意。修前 FK 仅 _tx 内 ON，绕过 _tx 的删除不级联→留孤儿。
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")  # 锁等待 5s，防并发写直接报错
        self._conn.row_factory = sqlite3.Row
        fresh_schema = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' LIMIT 1"
        ).fetchone() is None
        with self._tx() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn, fresh_schema=fresh_schema)
            if fresh_schema:
                _certify_fresh_rating_projection(conn)
            seed_email_templates(conn, _now())
            # 启动一致性断言：每个已注册游戏必须有对应的物理表 matches_<game>。
            # schema.py 的字面 DDL 只覆盖 holdem/gomoku/pencil；第 4 游戏须经
            # _migrate 的自动建表补出来。此断言在 _migrate 之后跑，确保"注册了
            # 但表没建"的 drift 在启动即报（而非 create_match 时才崩 no such table）。
            _existing = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            _missing = [
                f"matches_{gid}" for gid in _all_game_ids()
                if f"matches_{gid}" not in _existing
            ]
            assert not _missing, (
                f"注册表里的游戏缺物理表（_migrate 自动建表应覆盖此场景）："
                f"{_missing}。检查 games/__init__.py 注册 vs schema.py DDL。"
            )
        # Delayed import keeps db.py's schema helpers available while the
        # source-neutral repository imports them.  Callers use this one facade;
        # no execution path is allowed to mutate execution_jobs ad hoc.
        from .execution import ExecutionRepository

        self.executions = ExecutionRepository(self)

    @contextlib.contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    @contextlib.contextmanager
    def offline_cutover_guard(self):
        """Prove that no platform process owns this database's dispatcher.

        The production ASGI process owns this exact flock from startup until
        shutdown.  A hard ruleset cutover must hold it across asset staging and
        the metadata transaction, so an online ``Store`` call cannot race an
        upload, preflight, claim, or scheduler callback.
        """

        with offline_cutover_path_guard(self.path) as guard:
            yield self.bind_offline_cutover_guard(guard)

    def bind_offline_cutover_guard(
        self, guard: _OfflineCutoverGuard
    ) -> _OfflineCutoverGuard:
        """Bind a pre-open path guard to this exact Store and thread."""

        database = str(Path(self.path).expanduser().resolve())
        if (
            not isinstance(guard, _OfflineCutoverGuard)
            or not guard.active
            or guard.database_path != database
            or guard.thread_id != threading.get_ident()
            or guard.store_identity not in (None, id(self))
        ):
            raise RuntimeError(
                "规则 hard cutover 缺少当前 Store 的停服独占 guard"
            )
        guard.store_identity = id(self)
        return guard

    @staticmethod
    def _require_execution_admission_tx(
        c: sqlite3.Connection, *, maintenance_only: bool = False
    ) -> None:
        """Check the deployment/queue gate inside an existing write txn.

        Callers that create contest execution state use the full gate; stage
        recovery paths only need the durable deployment bit.  Keeping this
        check behind the same ``BEGIN IMMEDIATE`` as the mutation linearizes it
        with ``ExecutionRepository.begin_maintenance`` across Store instances.
        """
        control = c.execute(
            "SELECT dispatcher_state,accepting,deployment_drain_requested "
            "FROM execution_control WHERE singleton=1"
        ).fetchone()
        if control is None:
            raise RuntimeError("execution_control singleton missing")
        from .execution import ExecutionQueueClosed

        if int(control["deployment_drain_requested"] or 0):
            raise ExecutionQueueClosed(
                "平台正在部署维护，赛事将在恢复后继续派发",
                code="deployment_maintenance",
            )
        if maintenance_only:
            return
        if (
            control["dispatcher_state"] != "running"
            or int(control["accepting"] or 0) != 1
        ):
            raise ExecutionQueueClosed("执行队列暂未开放，赛事对阵已保留")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── users ─────────────────────────────────────────────────

    def create_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        *,
        display_name: str = "",
        role: str = "user",
        real_name: str = "",
        phone: str = "",
        school: str = "",
        student_id: str = "",
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO users(username, email, password_hash, role, "
                "display_name, created_at, real_name, phone, school, student_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    username,
                    email,
                    password_hash,
                    role,
                    display_name or username,
                    _now(),
                    real_name,
                    phone,
                    school,
                    student_id,
                ),
            )
            uid = cur.lastrowid
            return _row(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def get_user(self, user_id: int) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            )

    def get_user_by_username(self, username: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM users WHERE username=?", (username,)
                ).fetchone()
            )

    def user_profile(self, username: str) -> dict | None:
        """用户主页聚合：用户公开信息（不含 password_hash/email）+ 总战绩。

        不聚合分差：不同游戏的原始分差单位不同，横跨游戏相加没有意义。
        Bot 列表与对局历史用单独端点（避免单次返回过大）。
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT id, username, display_name, role, bio, avatar, "
                "created_at, last_login_at, xp, level, last_active_at "
                "FROM users WHERE username=? AND is_active=1",
                (username,),
            ).fetchone()
            if not row:
                return None
            d = _row(row)
            uid = d["id"]
            agg = c.execute(
                "SELECT COALESCE(SUM(r.wins),0) AS wins, "
                "COALESCE(SUM(r.losses),0) AS losses, "
                "COALESCE(SUM(r.draws),0) AS draws, "
                "COALESCE(SUM(r.matches_played),0) AS matches_played, "
                "COUNT(r.bot_id) AS rated_bots "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE b.owner_id=?",
                (uid,),
            ).fetchone()
            d["stats"] = _row(agg) if agg else {
                "wins": 0, "losses": 0, "draws": 0,
                "matches_played": 0, "rated_bots": 0,
            }
            d["bot_count"] = c.execute(
                "SELECT COUNT(*) FROM bots WHERE owner_id=? "
                "AND owner_deleted_at IS NULL",
                (uid,),
            ).fetchone()[0]
            return d

    def aggregate_owner_stats(self, owner_id: int) -> dict:
        """按 owner 聚合其所有 bot 的战绩（用于用户主页总战绩）。"""
        with self._tx() as c:
            agg = c.execute(
                "SELECT COALESCE(SUM(r.wins),0) AS wins, "
                "COALESCE(SUM(r.losses),0) AS losses, "
                "COALESCE(SUM(r.draws),0) AS draws, "
                "COALESCE(SUM(r.matches_played),0) AS matches_played "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id WHERE b.owner_id=?",
                (owner_id,),
            ).fetchone()
            return _row(agg) if agg else {
                "wins": 0, "losses": 0, "draws": 0,
                "matches_played": 0,
            }

    def award_xp(self, user_id: int, amount: int) -> dict | None:
        """给用户加经验，并重算 level + 更新 last_active_at。返回更新后的 user。"""
        from bzplat.backend.store.schema import level_for_xp
        if amount == 0:
            return self.get_user(user_id)
        with self._tx() as c:
            row = c.execute(
                "SELECT xp FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not row:
                return None
            new_xp = max(0, int(row["xp"] or 0) + max(0, amount))
            new_level = level_for_xp(new_xp)
            c.execute(
                "UPDATE users SET xp=?, level=?, last_active_at=? WHERE id=?",
                (new_xp, new_level, _now(), user_id),
            )
            return _row(
                c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            )

    def search_bots(
        self,
        q: str,
        *,
        limit: int = 20,
        game_id: str | None = None,
    ) -> list[dict]:
        """按 name/display_name 模糊搜索 public bot（含 owner 名 + rating）。"""
        ql = f"%{q.lower()}%" if q else "%"
        with self._tx() as c:
            sql = (
                "SELECT b.id, b.name, b.display_name, b.game_id, b.format, "
                "b.os, b.arch, u.username AS owner_name, "
                "u.display_name AS owner_display, r.rating "
                "FROM bots b LEFT JOIN users u ON b.owner_id=u.id "
                "LEFT JOIN ratings r ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE b.is_active=1 "
                "AND b.format=? AND b.os=? AND b.arch=? "
                "AND (LOWER(b.name) LIKE ? OR LOWER(b.display_name) LIKE ?)"
            )
            params: list[Any] = [
                SUPPORTED_BINARY_FORMAT,
                SUPPORTED_BINARY_OS,
                SUPPORTED_BINARY_ARCH,
                ql,
                ql,
            ]
            if game_id:
                sql += " AND b.game_id=?"
                params.append(game_id)
            sql += " ORDER BY r.rating DESC LIMIT ?"
            params.append(max(1, min(limit, 50)))
            return [_row(r) for r in c.execute(sql, params)]

    def search_matches(
        self,
        q: str,
        *,
        limit: int = 20,
        game_id: str | None = None,
    ) -> list[dict]:
        """按对局、Bot 或公开参与者姓名搜索已完成对局。"""
        ql = f"%{q.lower()}%" if q else "%"
        with self._tx() as c:
            sel = (
                "m.id, m.game_id, m.status, m.winner, m.reason, "
                "m.technical_loss, m.result, m.match_config, "
                "m.match_type, m.contest_id, m.created_at, "
                "m.bot_a_id, m.bot_b_id, m.human_user_id, m.human_seat, "
                "ba.name AS bot_a_name, bb.name AS bot_b_name, "
                "ba.display_name AS bot_a_display, bb.display_name AS bot_b_display, "
                "ua.username AS bot_a_owner_name, ua.display_name AS bot_a_owner_display, "
                "ub.username AS bot_b_owner_name, ub.display_name AS bot_b_owner_display, "
                "hu.username AS human_user_name, hu.display_name AS human_user_display, "
                f"{_contest_expected_duplicate_projection_sql('m')} "
                "AS _contest_expected_duplicate, "
                f"{_contest_require_frozen_duplicate_projection_sql('m')} "
                "AS _contest_require_frozen_duplicate, "
                f"{_contest_stage_config_projection_sql('m')} "
                "AS _contest_stage_config_json"
            )
            join_bots = (
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id "
                "LEFT JOIN users ua ON ba.owner_id=ua.id "
                "LEFT JOIN users ub ON bb.owner_id=ub.id "
                "LEFT JOIN users hu ON m.human_user_id=hu.id"
            )
            searchable = (
                "m.id",
                "ba.name", "bb.name",
                "ba.display_name", "bb.display_name",
                "ua.username", "ub.username",
                "ua.display_name", "ub.display_name",
                "hu.username", "hu.display_name",
            )
            where_sql = " WHERE m.status='completed' AND (" + " OR ".join(
                f"LOWER({column}) LIKE ?" for column in searchable
            ) + ")"
            params: list[Any] = [ql] * len(searchable)
            if game_id:
                where_sql += " AND m.game_id=?"
                params.append(game_id)
            lim = max(1, min(limit, 50))

            if game_id:
                tbl = _matches_table(game_id)
                sql = f"SELECT {sel} FROM {tbl} m {join_bots}{where_sql} ORDER BY m.created_at DESC LIMIT ?"
                return [
                    _parse_match_json_cols(_row(r))
                    for r in c.execute(sql, params + [lim])
                ]

            # 跨游戏 UNION ALL
            subselects = []
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                subselects.append(f"SELECT {sel} FROM {tbl} m {join_bots}{where_sql}")
            union = " UNION ALL ".join(subselects)
            sql = f"SELECT * FROM ({union}) ORDER BY created_at DESC LIMIT ?"
            # 子查询数 = 已注册游戏数，WHERE 参数须按此倍数复制（每个子查询一份）。
            # 不得硬编码 * 3——新增第 4 游戏会触发 Incorrect number of bindings。
            return [
                _parse_match_json_cols(_row(r))
                for r in c.execute(
                    sql, params * len(_all_game_ids()) + [lim]
                )
            ]

    def get_user_by_email(self, email: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            )

    @staticmethod
    def _revoke_local_ai_agents_tx(
        c: sqlite3.Connection,
        *,
        scope_column: str,
        scope_id: int,
        reason: str,
    ) -> list[dict[str, Any]]:
        """Revoke one exact active-agent postimage inside the caller's write tx."""

        if scope_column not in {"bot_id", "owner_id"}:
            raise ValueError("invalid Local-AI revocation scope")
        if not c.in_transaction:
            raise RuntimeError("Local-AI revocation requires an active transaction")
        active_rows = c.execute(
            "SELECT id,public_id,owner_id,bot_id FROM local_ai_agents "
            f"WHERE {scope_column}=? AND status='active' ORDER BY id",
            (int(scope_id),),
        ).fetchall()
        if active_rows:
            agent_ids = [int(row["id"]) for row in active_rows]
            marks = ",".join("?" for _ in agent_ids)
            now = _now()
            changed = c.execute(
                "UPDATE local_ai_agents SET status='revoked',"
                "connection_generation=connection_generation+1,connected_at=NULL,"
                f"disconnected_at=?,updated_at=? WHERE id IN ({marks}) "
                "AND status='active'",
                (now, now, *agent_ids),
            )
            if changed.rowcount != len(agent_ids):  # pragma: no cover - write lock invariant
                raise RuntimeError(
                    "Local-AI revocation postimage changed inside write transaction"
                )
            c.execute(
                "UPDATE local_ai_leases SET status='released',released_at=?,"
                f"terminal_reason=? WHERE agent_id IN ({marks}) AND status='active'",
                (now, str(reason)[:200], *agent_ids),
            )
        # Freeze every newly revoked identity and its immutable authorization
        # scope before commit.  The process-local service retains unfinished
        # items for retry; historical revoked rows never enter request work.
        return [
            {
                "public_id": str(row["public_id"]),
                "owner_id": int(row["owner_id"]),
                "bot_id": int(row["bot_id"]),
            }
            for row in active_rows
        ]

    def update_user(self, user_id: int, **fields: Any) -> dict | None:
        allowed = {
            "password_hash",
            "email",
            "display_name",
            "role",
            "is_active",
            "last_login_at",
            "email_verified",
            "bio",
            "avatar",
            "xp",
            "level",
            "last_active_at",
            "real_name",
            "phone",
            "school",
            "student_id",
        }
        if "is_active" in fields:
            raw_active = fields["is_active"]
            if isinstance(raw_active, bool):
                fields["is_active"] = int(raw_active)
            elif type(raw_active) is not int or raw_active not in (0, 1):
                raise ValueError("is_active 必须是布尔值或整数 0/1")
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            disabling = "is_active" in fields and not bool(fields["is_active"])
            revoked_targets: list[dict[str, Any]] = []
            if disabling:
                c.execute("BEGIN IMMEDIATE")
            if sets:
                vals.append(user_id)
                c.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)
            if disabling:
                # Account disable is a revocation boundary.  Delete every
                # bearer in the same write transaction as the user flag and
                # Local-AI teardown so a dormant token cannot revive after a
                # later re-enable and no partial revocation can commit.
                c.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))
                revoked_targets = self._revoke_local_ai_agents_tx(
                    c,
                    scope_column="owner_id",
                    scope_id=int(user_id),
                    reason="owner_disabled",
                )
            result = _row(
                c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            )
            if result is not None and disabling:
                result["_revoked_local_ai_targets"] = revoked_targets
                result["_local_ai_revocation_scope"] = {
                    "kind": "owner",
                    "id": int(user_id),
                }
            return result

    def list_users(
        self, *, role: str | None = None, active_only: bool = False,
        q: str | None = None, real_name: bool | None = None,
        page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        with self._tx() as c:
            sql = f"SELECT {','.join(_ADMIN_USER_COLUMNS)} FROM users WHERE 1=1"
            params: list[Any] = []
            if role:
                sql += " AND role=?"
                params.append(role)
            if active_only:
                sql += " AND is_active=1"
            if q:
                sql += (
                    " AND (LOWER(username) LIKE ? OR LOWER(email) LIKE ? "
                    "OR LOWER(display_name) LIKE ?)"
                )
                like = f"%{q.strip().lower()}%"
                params.extend((like, like, like))
            if real_name is not None:
                complete = (
                    "TRIM(COALESCE(real_name,''))<>'' AND "
                    "TRIM(COALESCE(phone,''))<>'' AND "
                    "TRIM(COALESCE(school,''))<>'' AND "
                    "TRIM(COALESCE(student_id,''))<>''"
                )
                sql += f" AND ({complete})" if real_name else f" AND NOT ({complete})"
            sql += " ORDER BY created_at"
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, tuple(params), page=page, per_page=pp)
                return {"items": rows, "page": page, "per_page": pp, "total": total}
            return [_row(r) for r in c.execute(sql, params)]

    def search_users(self, q: str, *, limit: int = 20) -> list[dict]:
        """按用户名前缀搜索（仅返回安全字段 id/username/display_name）。"""
        q = (q or "").strip()
        with self._tx() as c:
            if not q:
                sql = (
                    "SELECT id, username, display_name FROM users "
                    "WHERE is_active=1 ORDER BY username LIMIT ?"
                )
                rows = c.execute(sql, (limit,)).fetchall()
            else:
                sql = (
                    "SELECT id, username, display_name FROM users "
                    "WHERE is_active=1 AND username LIKE ? ORDER BY username LIMIT ?"
                )
                rows = c.execute(sql, (q + "%", limit)).fetchall()
            return [_row(r) for r in rows]

    # ── sessions ──────────────────────────────────────────────

    def add_session(
        self,
        token: str,
        user_id: int,
        expires_at: str,
        *,
        ip_addr: str = "",
        user_agent: str = "",
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO sessions(token, user_id, expires_at, "
                "created_at, ip_addr, user_agent) VALUES(?,?,?,?,?,?)",
                (token, user_id, expires_at, _now(), ip_addr, user_agent),
            )

    def add_session_if_user_active(
        self,
        token: str,
        user_id: int,
        expires_at: str,
        *,
        expected_password_hash: str,
        ip_addr: str = "",
        user_agent: str = "",
    ) -> bool:
        """Issue a session only for the exact active credential just verified.

        The immediate transaction is the other half of ``update_user``'s
        disable/revoke and password-rotation boundaries.  Whichever writer wins
        first either creates a token that the subsequent mutation deletes, or
        observes a disabled user / changed password hash and creates nothing.
        """

        if not isinstance(expected_password_hash, str) or not expected_password_hash:
            raise ValueError("expected_password_hash 必须是非空字符串")

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            issued = c.execute(
                "INSERT INTO sessions(token,user_id,expires_at,created_at,ip_addr,user_agent) "
                "SELECT ?,id,?,?,?,? FROM users WHERE id=? AND is_active=1 "
                "AND password_hash=?",
                (
                    token,
                    expires_at,
                    _now(),
                    ip_addr,
                    user_agent,
                    user_id,
                    expected_password_hash,
                ),
            )
            return issued.rowcount == 1

    def get_session(self, token: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
            )

    def delete_session(self, token: str) -> bool:
        with self._tx() as c:
            return (
                c.execute("DELETE FROM sessions WHERE token=?", (token,)).rowcount > 0
            )

    def delete_sessions_for_user(self, user_id: int) -> int:
        with self._tx() as c:
            return c.execute(
                "DELETE FROM sessions WHERE user_id=?", (user_id,)
            ).rowcount

    def rotate_password_if_current(
        self,
        user_id: int,
        *,
        expected_password_hash: str,
        password_hash: str,
    ) -> bool:
        """CAS the active user's password and revoke sessions atomically."""
        if not isinstance(expected_password_hash, str) or not expected_password_hash:
            raise ValueError("expected_password_hash 必须是非空字符串")
        if not isinstance(password_hash, str) or not password_hash:
            raise ValueError("password_hash 必须是非空字符串")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            updated = c.execute(
                "UPDATE users SET password_hash=? "
                "WHERE id=? AND is_active=1 AND password_hash=?",
                (password_hash, int(user_id), expected_password_hash),
            )
            if updated.rowcount != 1:
                return False
            c.execute("DELETE FROM sessions WHERE user_id=?", (int(user_id),))
            return True

    # 兼容别名
    delete_user_sessions = delete_sessions_for_user

    # ── email_codes ───────────────────────────────────────────

    def add_email_code(
        self, user_id: int, purpose: str, code: str, expires_at: str
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO email_codes(user_id, purpose, code, expires_at, "
                "created_at) VALUES(?,?,?,?,?)",
                (user_id, purpose, code, expires_at, _now()),
            )

    def get_latest_email_code(self, user_id: int, purpose: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM email_codes WHERE user_id=? AND purpose=? "
                    "AND used_at IS NULL ORDER BY id DESC LIMIT 1",
                    (user_id, purpose),
                ).fetchone()
            )

    def mark_email_code_used(self, code_id: int) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE email_codes SET used_at=? WHERE id=?", (_now(), code_id)
            )

    # ── password_resets ───────────────────────────────────────

    def add_password_reset(
        self, token: str, user_id: int, expires_at: str
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO password_resets(token, user_id, expires_at, "
                "created_at) VALUES(?,?,?,?)",
                (token, user_id, expires_at, _now()),
            )

    def get_password_reset(self, token: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM password_resets WHERE token=? AND used_at IS NULL",
                    (token,),
                ).fetchone()
            )

    def mark_password_reset_used(self, token: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE password_resets SET used_at=? WHERE token=?",
                (_now(), token),
            )

    def reset_password_with_credential(
        self,
        user_id: int,
        password_hash: str,
        *,
        email_code_id: int | None = None,
        email_code: str | None = None,
        reset_token: str | None = None,
    ) -> str:
        """原子消费一次性凭据、更新密码并撤销该用户的全部会话。

        邮箱验证码与历史迁移兼容的重置 token 二选一。返回 ``ok``、``invalid`` 或
        ``expired``；``invalid`` 同时涵盖不存在、已使用、失败预算耗尽和最终 CAS
        竞争失败。验证码错误计数、凭据 CAS、密码更新与 session 删除共享同一个
        ``BEGIN IMMEDIATE`` 事务，既能跨进程/重启限次，后续步骤异常时也不会留下
        半提交。
        """
        email_selected = email_code_id is not None or email_code is not None
        token_selected = reset_token is not None
        if email_selected == token_selected:
            raise ValueError("邮箱验证码和重置 token 必须且只能提供一种")
        if email_selected and email_code is None:
            raise ValueError("邮箱验证码 code 必须提供")

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            used_at = _now()
            checked_at = datetime.now()
            expiry_cutoff = checked_at.isoformat(timespec="microseconds")
            if email_selected:
                if email_code_id is None:
                    # Always bind attempts to the newest credential, including
                    # an already-consumed/exhausted one.  Falling back to an
                    # older still-unused row would multiply the brute-force
                    # budget whenever a user requested another code.
                    credential = c.execute(
                        "SELECT id,user_id,code,expires_at,used_at,failed_attempts "
                        "FROM email_codes WHERE user_id=? AND purpose=? "
                        "ORDER BY id DESC LIMIT 1",
                        (user_id, CODE_RESET),
                    ).fetchone()
                else:
                    credential = c.execute(
                        "SELECT id,user_id,code,expires_at,used_at,failed_attempts "
                        "FROM email_codes WHERE id=? AND user_id=? AND purpose=?",
                        (email_code_id, user_id, CODE_RESET),
                    ).fetchone()
                if not credential or credential["used_at"] is not None:
                    return "invalid"
                failed_attempts = credential["failed_attempts"]
                if (
                    type(failed_attempts) is not int
                    or failed_attempts < 0
                    or failed_attempts >= EMAIL_CODE_MAX_FAILED_ATTEMPTS
                ):
                    return "invalid"
                submitted_code = email_code if isinstance(email_code, str) else ""
                stored_code = credential["code"]
                code_matches = (
                    isinstance(stored_code, str)
                    and secrets.compare_digest(stored_code, submitted_code)
                )
                if not code_matches:
                    failed_at = _now()
                    failed = c.execute(
                        "UPDATE email_codes SET "
                        "failed_attempts=failed_attempts+1,"
                        "used_at=CASE WHEN failed_attempts+1>=? THEN ? ELSE used_at END "
                        "WHERE id=? AND user_id=? AND purpose=? AND used_at IS NULL "
                        "AND failed_attempts=? AND failed_attempts<?",
                        (
                            EMAIL_CODE_MAX_FAILED_ATTEMPTS,
                            failed_at,
                            credential["id"],
                            user_id,
                            CODE_RESET,
                            failed_attempts,
                            EMAIL_CODE_MAX_FAILED_ATTEMPTS,
                        ),
                    )
                    if failed.rowcount != 1:
                        return "invalid"
                    return "invalid"
                try:
                    expired = (
                        datetime.fromisoformat(credential["expires_at"])
                        < checked_at
                    )
                except (TypeError, ValueError):
                    return "invalid"
                if expired:
                    return "expired"
                consume = c.execute(
                    "UPDATE email_codes SET used_at=? "
                    "WHERE id=? AND user_id=? AND purpose=? AND code=? AND used_at IS NULL "
                    "AND failed_attempts=? AND failed_attempts<? AND expires_at>=?",
                    (
                        used_at,
                        credential["id"],
                        user_id,
                        CODE_RESET,
                        submitted_code,
                        failed_attempts,
                        EMAIL_CODE_MAX_FAILED_ATTEMPTS,
                        expiry_cutoff,
                    ),
                )
            else:
                credential = c.execute(
                    "SELECT user_id, expires_at, used_at FROM password_resets "
                    "WHERE token=? AND user_id=?",
                    (reset_token, user_id),
                ).fetchone()
                if not credential or credential["used_at"] is not None:
                    return "invalid"
                try:
                    expired = (
                        datetime.fromisoformat(credential["expires_at"])
                        < checked_at
                    )
                except (TypeError, ValueError):
                    return "invalid"
                if expired:
                    return "expired"
                consume = c.execute(
                    "UPDATE password_resets SET used_at=? "
                    "WHERE token=? AND user_id=? AND used_at IS NULL "
                    "AND expires_at>=?",
                    (used_at, reset_token, user_id, expiry_cutoff),
                )

            if consume.rowcount != 1:
                return "invalid"
            updated = c.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (password_hash, user_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("重置密码时用户记录不存在")
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return "ok"

    # ── bots ──────────────────────────────────────────────────

    def create_local_ai_agent(
        self,
        *,
        owner_id: int,
        bot_id: int,
        label: str,
        public_id: str,
        token_hash: str,
        token_hint: str,
    ) -> dict:
        """Bind one outbound local-AI connection identity to an owned Bot."""
        clean_label = str(label or "").strip()
        if not 2 <= len(clean_label) <= 32:
            raise ValueError("本地 Bot 名称须为 2–32 个字符")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            bot = c.execute(
                "SELECT b.id,b.owner_id,b.game_id,b.protocol_version,b.is_active,"
                "b.owner_deleted_at,"
                "u.is_active AS owner_active FROM bots b "
                "JOIN users u ON u.id=b.owner_id WHERE b.id=?",
                (int(bot_id),),
            ).fetchone()
            if bot is None or int(bot["owner_id"]) != int(owner_id):
                raise ValueError("只能为自己的 Bot 建立本地连接")
            if bot["owner_deleted_at"] is not None:
                raise BotDeletedError("Bot 已删除，不能再创建本地连接")
            if int(bot["is_active"] or 0) != 1:
                raise ValueError("请先启用这个 Bot")
            if int(bot["owner_active"] or 0) != 1:
                raise ValueError("账号已停用，不能创建本地 Bot 接入")
            contract = _active_game_contract_tx(c, str(bot["game_id"]))
            if str(bot["protocol_version"] or "") != contract["protocol_version"]:
                raise ValueError("Bot 协议与当前游戏契约不一致")
            active_count = int(
                c.execute(
                    "SELECT COUNT(*) FROM local_ai_agents "
                    "WHERE owner_id=? AND status='active'",
                    (int(owner_id),),
                ).fetchone()[0]
            )
            if active_count >= LOCAL_AI_MAX_ACTIVE_AGENTS_PER_OWNER:
                raise ValueError(
                    f"每个账号最多保留 {LOCAL_AI_MAX_ACTIVE_AGENTS_PER_OWNER} "
                    "个本地 Bot 接入，请先撤销不用的接入"
                )
            existing_label = c.execute(
                "SELECT id,status FROM local_ai_agents "
                "WHERE owner_id=? AND label=?",
                (int(owner_id), clean_label),
            ).fetchone()
            if existing_label is not None:
                if str(existing_label["status"] or "") != "revoked":
                    raise ValueError("本地 Bot 名称已存在")
                changed = c.execute(
                    "UPDATE local_ai_agents SET public_id=?,bot_id=?,game_id=?,"
                    "protocol_version=?,"
                    "token_hash=?,token_hint=?,status='active',"
                    "connection_generation=connection_generation+1,"
                    "connected_at=NULL,disconnected_at=NULL,last_seen_at=NULL,"
                    "created_at=?,updated_at=? WHERE id=? AND status='revoked'",
                    (
                        public_id,
                        int(bot_id),
                        str(bot["game_id"]),
                        contract["protocol_version"],
                        token_hash,
                        token_hint,
                        _now(),
                        _now(),
                        int(existing_label["id"]),
                    ),
                )
                if changed.rowcount != 1:  # pragma: no cover - transaction guard
                    raise RuntimeError("本地 Bot 接入复用失败")
                return _row(
                    c.execute(
                        "SELECT * FROM local_ai_agents WHERE id=?",
                        (int(existing_label["id"]),),
                    ).fetchone()
                )
            try:
                cur = c.execute(
                    "INSERT INTO local_ai_agents("
                    "public_id,owner_id,bot_id,label,game_id,protocol_version,"
                    "token_hash,token_hint,status,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,'active',?,?)",
                    (
                        public_id,
                        int(owner_id),
                        int(bot_id),
                        clean_label,
                        str(bot["game_id"]),
                        contract["protocol_version"],
                        token_hash,
                        token_hint,
                        _now(),
                        _now(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("本地 Bot 名称已存在") from exc
            return _row(
                c.execute(
                    "SELECT * FROM local_ai_agents WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def list_local_ai_agents(self, owner_id: int) -> list[dict]:
        with self._tx() as c:
            return [
                _row(row)
                for row in c.execute(
                    "SELECT a.*,b.name AS bot_name,b.display_name AS bot_display_name,"
                    "b.is_active AS bot_active,u.is_active AS owner_active "
                    "FROM local_ai_agents a JOIN bots b ON b.id=a.bot_id "
                    "JOIN users u ON u.id=a.owner_id WHERE a.owner_id=? "
                    "ORDER BY a.status='active' DESC,a.updated_at DESC,a.id DESC",
                    (int(owner_id),),
                )
            ]

    def list_local_ai_agents_admin(
        self, *, page: int = 1, per_page: int = 20
    ) -> dict:
        """Return connection metadata for operators, never credential hashes."""
        normalized_page = max(1, int(page))
        normalized_per_page = max(1, min(100, int(per_page)))
        with self._tx() as c:
            total = int(c.execute("SELECT COUNT(*) FROM local_ai_agents").fetchone()[0])
            items = [
                _row(row)
                for row in c.execute(
                    "SELECT a.id,a.public_id,a.owner_id,a.bot_id,a.label,a.game_id,"
                    "a.status,a.connected_at,a.disconnected_at,a.last_seen_at,"
                    "a.created_at,a.updated_at,b.name AS bot_name,"
                    "b.display_name AS bot_display_name,b.is_active AS bot_active,"
                    "u.username AS owner_name,u.display_name AS owner_display_name,"
                    "u.is_active AS owner_active "
                    "FROM local_ai_agents a JOIN bots b ON b.id=a.bot_id "
                    "JOIN users u ON u.id=a.owner_id "
                    "ORDER BY a.status='active' DESC,a.updated_at DESC,a.id DESC "
                    "LIMIT ? OFFSET ?",
                    (
                        normalized_per_page,
                        (normalized_page - 1) * normalized_per_page,
                    ),
                )
            ]
            return {
                "items": items,
                "total": total,
                "page": normalized_page,
                "per_page": normalized_per_page,
            }

    def get_local_ai_agent(self, agent_id: int) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT a.*,b.name AS bot_name,b.display_name AS bot_display_name,"
                    "b.is_active AS bot_active,u.is_active AS owner_active "
                    "FROM local_ai_agents a JOIN bots b ON b.id=a.bot_id "
                    "JOIN users u ON u.id=a.owner_id WHERE a.id=?",
                    (int(agent_id),),
                ).fetchone()
            )

    def has_active_local_ai_lease(self, agent_id: int) -> bool:
        """Return whether one execution owns this agent between decisions."""
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM local_ai_leases WHERE agent_id=? "
                "AND status='active' LIMIT 1",
                (int(agent_id),),
            ).fetchone() is not None

    def get_local_ai_agent_by_public_id(self, public_id: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT a.*,b.name AS bot_name,b.display_name AS bot_display_name,"
                    "b.is_active AS bot_active,u.is_active AS owner_active "
                    "FROM local_ai_agents a JOIN bots b ON b.id=a.bot_id "
                    "JOIN users u ON u.id=a.owner_id WHERE a.public_id=?",
                    (str(public_id),),
                ).fetchone()
            )

    def get_local_ai_agent_by_token_hash(self, token_hash: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT a.*,b.name AS bot_name,b.display_name AS bot_display_name,"
                    "b.is_active AS bot_active,u.is_active AS owner_active "
                    "FROM local_ai_agents a JOIN bots b ON b.id=a.bot_id "
                    "JOIN users u ON u.id=a.owner_id WHERE a.token_hash=?",
                    (str(token_hash),),
                ).fetchone()
            )

    def rotate_local_ai_agent_token(
        self,
        agent_id: int,
        owner_id: int,
        *,
        public_id: str,
        token_hash: str,
        token_hint: str,
    ) -> dict | None:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute(
                "SELECT 1 FROM local_ai_leases WHERE agent_id=? "
                "AND status='active' LIMIT 1",
                (int(agent_id),),
            ).fetchone() is not None:
                raise LocalAIAgentBusyError(
                    "本地 Bot 正在对局，结束并释放占用后才能更换令牌"
                )
            changed = c.execute(
                "UPDATE local_ai_agents SET public_id=?,token_hash=?,token_hint=?,"
                "connection_generation=connection_generation+1,connected_at=NULL,"
                "disconnected_at=?,last_seen_at=NULL,updated_at=? "
                "WHERE id=? AND owner_id=? AND status='active'",
                (
                    str(public_id), token_hash, token_hint, _now(), _now(),
                    int(agent_id), int(owner_id),
                ),
            )
            if changed.rowcount != 1:
                return None
            return _row(
                c.execute("SELECT * FROM local_ai_agents WHERE id=?", (agent_id,)).fetchone()
            )

    def revoke_local_ai_agent(self, agent_id: int, owner_id: int) -> bool:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            now = _now()
            changed = c.execute(
                "UPDATE local_ai_agents SET status='revoked',"
                "connection_generation=connection_generation+1,connected_at=NULL,"
                "disconnected_at=?,updated_at=? WHERE id=? AND owner_id=? "
                "AND status='active'",
                (now, now, int(agent_id), int(owner_id)),
            )
            c.execute(
                "UPDATE local_ai_leases SET status='released',released_at=?,"
                "terminal_reason='agent_revoked' WHERE agent_id=? AND status='active'",
                (now, int(agent_id)),
            )
            return changed.rowcount == 1

    def revoke_local_ai_agent_admin(self, agent_id: int) -> bool:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            now = _now()
            changed = c.execute(
                "UPDATE local_ai_agents SET status='revoked',"
                "connection_generation=connection_generation+1,connected_at=NULL,"
                "disconnected_at=?,updated_at=? WHERE id=? AND status='active'",
                (now, now, int(agent_id)),
            )
            c.execute(
                "UPDATE local_ai_leases SET status='released',released_at=?,"
                "terminal_reason='admin_revoked' WHERE agent_id=? AND status='active'",
                (now, int(agent_id)),
            )
            return changed.rowcount == 1

    def connect_local_ai_agent(
        self, agent_id: int, *, expected_public_id: str
    ) -> dict | None:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            now = _now()
            candidate = c.execute(
                "SELECT a.owner_id,a.status,b.is_active AS bot_active,"
                "u.is_active AS owner_active FROM local_ai_agents a "
                "JOIN bots b ON b.id=a.bot_id JOIN users u ON u.id=a.owner_id "
                "JOIN rating_pool_state state ON state.game_id=a.game_id "
                "WHERE a.id=? AND a.public_id=? AND a.game_id=b.game_id "
                "AND a.protocol_version=b.protocol_version "
                "AND state.protocol_version=a.protocol_version",
                (int(agent_id), str(expected_public_id)),
            ).fetchone()
            if (
                candidate is None
                or str(candidate["status"] or "") != "active"
                or int(candidate["bot_active"] or 0) != 1
                or int(candidate["owner_active"] or 0) != 1
            ):
                return None
            owner_online = int(
                c.execute(
                    "SELECT COUNT(*) FROM local_ai_agents "
                    "WHERE owner_id=? AND status='active' "
                    "AND connected_at IS NOT NULL AND disconnected_at IS NULL",
                    (int(candidate["owner_id"]),),
                ).fetchone()[0]
            )
            global_online = int(
                c.execute(
                    "SELECT COUNT(*) FROM local_ai_agents WHERE status='active' "
                    "AND connected_at IS NOT NULL AND disconnected_at IS NULL"
                ).fetchone()[0]
            )
            if owner_online >= LOCAL_AI_MAX_ONLINE_PER_OWNER:
                raise ValueError(
                    f"每个账号最多同时在线 {LOCAL_AI_MAX_ONLINE_PER_OWNER} 个本地 Bot"
                )
            if global_online >= LOCAL_AI_MAX_ONLINE_GLOBAL:
                raise ValueError("平台本地 Bot 在线连接已满，请稍后重试")
            changed = c.execute(
                "UPDATE local_ai_agents SET connection_generation=connection_generation+1,"
                "connected_at=?,disconnected_at=NULL,last_seen_at=?,updated_at=? "
                "WHERE id=? AND public_id=? AND status='active'",
                (now, now, now, int(agent_id), str(expected_public_id)),
            )
            if changed.rowcount != 1:
                return None
            return _row(
                c.execute("SELECT * FROM local_ai_agents WHERE id=?", (agent_id,)).fetchone()
            )

    def touch_local_ai_agent(self, agent_id: int, generation: int) -> bool:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            authorized = c.execute(
                "SELECT 1 FROM local_ai_agents a "
                "JOIN bots b ON b.id=a.bot_id JOIN users u ON u.id=a.owner_id "
                "JOIN rating_pool_state state ON state.game_id=a.game_id "
                "WHERE a.id=? AND a.status='active' "
                "AND a.connection_generation=? AND a.connected_at IS NOT NULL "
                "AND a.disconnected_at IS NULL AND b.is_active=1 AND u.is_active=1 "
                "AND a.game_id=b.game_id AND a.protocol_version=b.protocol_version "
                "AND state.protocol_version=a.protocol_version",
                (int(agent_id), int(generation)),
            ).fetchone()
            if authorized is None:
                return False
            now = _now()
            return c.execute(
                "UPDATE local_ai_agents AS a SET last_seen_at=?,updated_at=? "
                "WHERE id=? AND status='active' AND connection_generation=? "
                "AND connected_at IS NOT NULL AND disconnected_at IS NULL",
                (now, now, int(agent_id), int(generation)),
            ).rowcount == 1

    def local_ai_connection_still_authorized(
        self, agent_id: int, generation: int
    ) -> bool:
        """Revalidate a live socket against current owner/Bot state."""

        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM local_ai_agents a "
                "JOIN bots b ON b.id=a.bot_id JOIN users u ON u.id=a.owner_id "
                "JOIN rating_pool_state state ON state.game_id=a.game_id "
                "WHERE a.id=? AND a.status='active' "
                "AND a.connection_generation=? AND a.connected_at IS NOT NULL "
                "AND a.disconnected_at IS NULL AND b.is_active=1 AND u.is_active=1 "
                "AND a.game_id=b.game_id AND a.protocol_version=b.protocol_version "
                "AND state.protocol_version=a.protocol_version",
                (int(agent_id), int(generation)),
            ).fetchone() is not None

    def revoke_local_ai_agents_for_bot(
        self, bot_id: int, *, reason: str = "bot_disabled"
    ) -> list[str]:
        """Atomically revoke all active connector identities for one Bot."""

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT id,public_id FROM local_ai_agents "
                "WHERE bot_id=? AND status='active'",
                (int(bot_id),),
            ).fetchall()
            if not rows:
                return []
            now = _now()
            ids = [int(row["id"]) for row in rows]
            marks = ",".join("?" for _ in ids)
            c.execute(
                f"UPDATE local_ai_agents SET status='revoked',"
                "connection_generation=connection_generation+1,connected_at=NULL,"
                f"disconnected_at=?,updated_at=? WHERE id IN ({marks})",
                (now, now, *ids),
            )
            c.execute(
                f"UPDATE local_ai_leases SET status='released',released_at=?,"
                f"terminal_reason=? WHERE agent_id IN ({marks}) AND status='active'",
                (now, str(reason)[:200], *ids),
            )
            return [str(row["public_id"]) for row in rows]

    def list_active_local_ai_public_ids_for_bot(self, bot_id: int) -> list[str]:
        with self._tx() as c:
            return [
                str(row[0])
                for row in c.execute(
                    "SELECT public_id FROM local_ai_agents "
                    "WHERE bot_id=? AND status='active'",
                    (int(bot_id),),
                )
            ]

    def revoke_local_ai_agents_for_owner(
        self, owner_id: int, *, reason: str = "owner_disabled"
    ) -> list[str]:
        """Atomically revoke all active connector identities for one account."""

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT id,public_id FROM local_ai_agents "
                "WHERE owner_id=? AND status='active'",
                (int(owner_id),),
            ).fetchall()
            if not rows:
                return []
            now = _now()
            ids = [int(row["id"]) for row in rows]
            marks = ",".join("?" for _ in ids)
            c.execute(
                f"UPDATE local_ai_agents SET status='revoked',"
                "connection_generation=connection_generation+1,connected_at=NULL,"
                f"disconnected_at=?,updated_at=? WHERE id IN ({marks})",
                (now, now, *ids),
            )
            c.execute(
                f"UPDATE local_ai_leases SET status='released',released_at=?,"
                f"terminal_reason=? WHERE agent_id IN ({marks}) AND status='active'",
                (now, str(reason)[:200], *ids),
            )
            return [str(row["public_id"]) for row in rows]

    def list_active_local_ai_public_ids_for_owner(
        self, owner_id: int
    ) -> list[str]:
        with self._tx() as c:
            return [
                str(row[0])
                for row in c.execute(
                    "SELECT public_id FROM local_ai_agents "
                    "WHERE owner_id=? AND status='active'",
                    (int(owner_id),),
                )
            ]

    def disconnect_local_ai_agent(
        self, agent_id: int, generation: int
    ) -> bool:
        with self._tx() as c:
            now = _now()
            return c.execute(
                "UPDATE local_ai_agents SET disconnected_at=?,updated_at=? "
                "WHERE id=? AND connection_generation=? AND disconnected_at IS NULL",
                (now, now, int(agent_id), int(generation)),
            ).rowcount == 1

    def reset_local_ai_runtime_state(self) -> None:
        """Fail closed after process restart; no in-memory connection survived."""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            now = _now()
            c.execute(
                "UPDATE local_ai_agents SET disconnected_at=COALESCE(disconnected_at,?),"
                "connected_at=NULL,updated_at=? WHERE connected_at IS NOT NULL",
                (now, now),
            )
            c.execute(
                "UPDATE local_ai_leases SET status='released',released_at=?,"
                "terminal_reason='service_restart' WHERE status='active'",
                (now,),
            )

    def create_bot(
        self,
        owner_id: int | None = None,
        name: str | None = None,
        **fields: Any,
    ) -> dict:
        if owner_id is not None:
            fields["owner_id"] = owner_id
        if name is not None:
            fields["name"] = name
        owner_id = fields["owner_id"]
        name = fields["name"]
        display_name = fields.get("display_name") or name
        description = fields.get("description", "")
        os_ = fields.get("os", SUPPORTED_BINARY_OS)
        arch = fields.get("arch", SUPPORTED_BINARY_ARCH)
        fmt = fields.get("format", SUPPORTED_BINARY_FORMAT)
        require_supported_binary_metadata(fmt, os_, arch)
        binary_path = fields.get("binary_path", "")
        is_builtin = 1 if fields.get("is_builtin") else 0
        is_active = 1 if fields.get("is_active", True) else 0
        # 仅缺省字段使用创建入口默认；显式空值/未知值必须失败。
        if "game_id" in fields:
            requested_game_id = fields["game_id"]
        else:
            requested_game_id = "holdem"
        game_id = _registered_game_id(requested_game_id)
        runtime_mode = fields.get("runtime_mode") or DEFAULT_RUNTIME_MODE
        if runtime_mode not in VALID_RUNTIME_MODES:
            raise ValueError(f"非法 runtime_mode: {runtime_mode}")
        now = _now()
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contract = _active_game_contract_tx(c, game_id)
            projection_guard = self._rating_projection_mutation_guard_tx(c)
            cur = c.execute(
                "INSERT INTO bots(owner_id, name, display_name, description, "
                "os, arch, format, binary_path, is_builtin, is_active, game_id, runtime_mode, "
                "protocol_version,created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    owner_id,
                    name,
                    display_name,
                    description,
                    os_,
                    arch,
                    fmt,
                    binary_path,
                    is_builtin,
                    is_active,
                    game_id,
                    runtime_mode,
                    contract["protocol_version"],
                    now,
                    now,
                ),
            )
            bid = cur.lastrowid
            # A Bot belongs to the replay universe from the instant it exists.
            # Create its default projection row in the same guarded transaction;
            # callers may still use ensure_rating idempotently.
            c.execute(
                "INSERT INTO ratings(bot_id, game_id) VALUES(?, ?)",
                (bid, game_id),
            )
            self._advance_rating_projection_state_tx(c, projection_guard)
            return _row(c.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone())

    def get_active_game_contract(self, game_id: str) -> dict[str, str]:
        """读取当前规则/协议/评分池契约的防御性副本。"""
        with self._tx() as c:
            return _active_game_contract_tx(c, game_id)

    def _validated_cutover_binary_root(
        self, supplied: str | os.PathLike[str]
    ) -> Path:
        """Validate the DB-adjacent asset root without following a symlink leaf."""

        database = Path(self.path).expanduser().resolve()
        expected = database.parent / "bot_uploads"
        candidate = Path(
            os.path.abspath(str(Path(supplied).expanduser()))
        )
        if candidate != expected:
            raise ValueError(
                "cutover 资产根目录必须是目标数据库旁的 canonical bot_uploads"
            )
        try:
            info = candidate.lstat()
        except (FileNotFoundError, OSError) as exc:
            raise ValueError("canonical bot_uploads 根目录不存在") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise ValueError("canonical bot_uploads 不得是符号链接或非目录")
        if int(info.st_uid) != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o022:
            raise ValueError(
                "canonical bot_uploads 必须由当前用户持有且不可被 group/other 写入"
            )
        try:
            if candidate.resolve(strict=True) != candidate:
                raise ValueError("canonical bot_uploads 路径不得经过符号链接")
        except OSError as exc:
            raise ValueError("canonical bot_uploads 根目录不可读") from exc
        return candidate

    def _assert_protocol_cutover_postconditions_tx(
        self,
        c: sqlite3.Connection,
        marker: sqlite3.Row,
        *,
        verify_assets: bool,
        enforce_live_generation: bool,
    ) -> None:
        """Verify the durable hard-cutover generation without blocking evolution.

        Marker versions are immutable audit evidence, but a later compatible
        upload may legitimately become current and the target rating pool may
        legitimately advance.  Conversely, a legacy-protocol version may never
        become runnable again.
        """

        marker_data = dict(marker)
        gid = _registered_game_id(str(marker_data.get("game_id") or ""))
        manifest = _loads_json(marker_data.get("manifest_json"), default=None)
        issues: list[str] = []
        if not isinstance(manifest, list):
            raise RuntimeError("cutover postcondition drift: manifest_json 损坏")
        normalized_manifest = sorted(
            [dict(entry) for entry in manifest],
            key=lambda entry: int(entry.get("bot_id") or 0),
        )
        if _canonical_digest(normalized_manifest) != str(
            marker_data.get("manifest_digest") or ""
        ):
            issues.append("manifest digest 不匹配")
        try:
            marker_bot_count = int(marker_data["bot_count"])
        except (KeyError, TypeError, ValueError):
            marker_bot_count = -1
        if marker_bot_count != len(normalized_manifest):
            issues.append("marker bot_count 不匹配")

        target = {
            "ruleset_version": str(marker_data.get("to_ruleset") or ""),
            "protocol_version": str(marker_data.get("to_protocol") or ""),
            "rating_pool_id": str(marker_data.get("to_rating_pool") or ""),
        }
        source = {
            "ruleset_version": str(marker_data.get("from_ruleset") or ""),
            "protocol_version": str(marker_data.get("from_protocol") or ""),
            "rating_pool_id": str(marker_data.get("from_rating_pool") or ""),
        }
        rule_only = source["protocol_version"] == target["protocol_version"]
        if any(not value for value in (*source.values(), *target.values())):
            issues.append("marker contract 字段为空")
        if source == target:
            issues.append("marker source/target contract 相同")
        if source["rating_pool_id"] == target["rating_pool_id"]:
            issues.append("cutover marker 必须更换 rating pool")
        if rule_only and (
            source["ruleset_version"] == target["ruleset_version"]
            or source["rating_pool_id"] == target["rating_pool_id"]
        ):
            issues.append("同协议 rule-only marker 必须同时更换 ruleset/rating pool")
        if rule_only and normalized_manifest:
            issues.append("同协议 rule-only marker 不得持久 pin Bot version")
        if rule_only and marker_bot_count != 0:
            issues.append("同协议 rule-only marker bot_count 必须为 0")
        if enforce_live_generation and _active_game_contract_tx(c, gid) != target:
            issues.append("active contract 不是 marker target")
        if enforce_live_generation:
            wrong_live_contests = int(
                c.execute(
                    "SELECT COUNT(*) FROM contests WHERE game_id=? "
                    "AND showcase_key IS NULL AND status NOT IN (?,?) AND ("
                    "ruleset_version<>? OR protocol_version<>? OR rating_pool_id<>?)",
                    (
                        gid,
                        CONTEST_FINISHED,
                        CONTEST_CANCELLED,
                        target["ruleset_version"],
                        target["protocol_version"],
                        target["rating_pool_id"],
                    ),
                ).fetchone()[0]
            )
            if wrong_live_contests:
                issues.append(
                    f"{wrong_live_contests} 个未终结赛事 contract 不是 marker target"
                )

        root: Path | None = None
        if verify_assets and normalized_manifest:
            try:
                root = self._validated_cutover_binary_root(
                    Path(self.path).expanduser().resolve().parent / "bot_uploads"
                )
            except ValueError as exc:
                issues.append(str(exc))

        seen_bots: set[int] = set()
        seen_paths: set[str] = set()
        seen_inodes: set[tuple[int, int]] = set()
        immutable_fields = (
            "version",
            "binary_path",
            "checksum",
            "size_bytes",
            "os",
            "arch",
            "format",
            "runtime_mode",
            "upload_note",
        )
        for entry in normalized_manifest:
            try:
                bot_id = int(entry["bot_id"])
                version = int(entry["version"])
            except (KeyError, TypeError, ValueError):
                issues.append("manifest bot_id/version 非法")
                continue
            if bot_id in seen_bots:
                issues.append(f"manifest 重复 Bot {bot_id}")
            seen_bots.add(bot_id)
            version_row = c.execute(
                "SELECT * FROM bot_versions WHERE bot_id=? AND version=?",
                (bot_id, version),
            ).fetchone()
            if version_row is None:
                issues.append(f"marker Bot {bot_id} v{version} 版本行缺失")
                continue
            for field in immutable_fields:
                expected_value = entry.get(field)
                actual_value = version_row[field]
                if field in {"version", "size_bytes"}:
                    try:
                        equal = int(actual_value) == int(expected_value)
                    except (TypeError, ValueError):
                        equal = False
                else:
                    equal = str(actual_value or "") == str(expected_value or "")
                if not equal:
                    issues.append(f"marker Bot {bot_id} v{version} {field} 漂移")
            if str(version_row["protocol_version"] or "") != target[
                "protocol_version"
            ]:
                issues.append(f"marker Bot {bot_id} v{version} protocol 漂移")
            if enforce_live_generation and version_row["retired_at"] is not None:
                issues.append(f"链尾 marker Bot {bot_id} v{version} 已退役")

            binary_path = Path(str(entry.get("binary_path") or ""))
            path_text = str(binary_path)
            if path_text in seen_paths:
                issues.append("多个 marker 版本共用路径")
            seen_paths.add(path_text)
            if root is not None:
                expected_path = root / str(bot_id) / f"v{version}" / "bot.bin"
                if binary_path != expected_path:
                    issues.append(f"marker Bot {bot_id} v{version} 路径非 canonical")
                    continue
                try:
                    version_dir = binary_path.parent
                    bot_dir = version_dir.parent
                    version_dir_stat = version_dir.lstat()
                    bot_dir_stat = bot_dir.lstat()
                    leaf = binary_path.lstat()
                    if (
                        stat.S_ISLNK(version_dir_stat.st_mode)
                        or not stat.S_ISDIR(version_dir_stat.st_mode)
                        or stat.S_IMODE(version_dir_stat.st_mode) != 0o555
                        or int(version_dir_stat.st_uid) != os.geteuid()
                        or stat.S_ISLNK(bot_dir_stat.st_mode)
                        or not stat.S_ISDIR(bot_dir_stat.st_mode)
                        or stat.S_IMODE(bot_dir_stat.st_mode) & 0o022
                        or int(bot_dir_stat.st_uid) != os.geteuid()
                        or stat.S_ISLNK(leaf.st_mode)
                        or not stat.S_ISREG(leaf.st_mode)
                        or stat.S_IMODE(leaf.st_mode) != 0o555
                        or int(leaf.st_uid) != os.geteuid()
                        or int(leaf.st_nlink) != 1
                    ):
                        raise OSError("not a regular nofollow asset")
                    if binary_path.resolve(strict=True) != binary_path:
                        raise OSError("asset path traverses symlink")
                    before = (
                        int(leaf.st_dev), int(leaf.st_ino), int(leaf.st_size),
                        int(leaf.st_mtime_ns), int(leaf.st_ctime_ns),
                    )
                    digest = hashlib.sha256()
                    with binary_path.open("rb") as stream:
                        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                            digest.update(chunk)
                    after_stat = binary_path.lstat()
                    after = (
                        int(after_stat.st_dev), int(after_stat.st_ino),
                        int(after_stat.st_size), int(after_stat.st_mtime_ns),
                        int(after_stat.st_ctime_ns),
                    )
                    if before != after:
                        raise OSError("asset changed while hashing")
                except OSError:
                    issues.append(f"marker Bot {bot_id} v{version} 资产缺失或不安全")
                else:
                    if int(after_stat.st_size) != int(entry.get("size_bytes") or -1):
                        issues.append(f"marker Bot {bot_id} v{version} 资产 size 漂移")
                    if digest.hexdigest() != str(entry.get("checksum") or ""):
                        issues.append(f"marker Bot {bot_id} v{version} 资产 hash 漂移")
                    inode = (int(after_stat.st_dev), int(after_stat.st_ino))
                    if inode in seen_inodes:
                        issues.append("多个 marker 版本共用 inode")
                    seen_inodes.add(inode)

        if enforce_live_generation:
            current_rows = c.execute(
                "SELECT b.id AS bot_id,b.current_version,b.binary_path AS bot_path,"
                "b.os AS bot_os,b.arch AS bot_arch,b.format AS bot_format,"
                "b.runtime_mode AS bot_runtime,b.protocol_version AS bot_protocol,"
                "v.id AS version_id,v.version AS version_number,"
                "v.binary_path AS version_path,v.os AS version_os,v.arch AS version_arch,"
                "v.format AS version_format,v.runtime_mode AS version_runtime,"
                "v.protocol_version AS version_protocol,v.retired_at AS version_retired "
                "FROM bots b LEFT JOIN bot_versions v ON v.bot_id=b.id "
                "AND v.version=b.current_version WHERE b.game_id=? ORDER BY b.id",
                (gid,),
            ).fetchall()
            for row in current_rows:
                bot_id = int(row["bot_id"])
                if row["version_id"] is None:
                    issues.append(f"Bot {bot_id} current version 行缺失")
                    continue
                if row["version_retired"] is not None:
                    issues.append(f"Bot {bot_id} current version 已退役")
                if (
                    str(row["bot_protocol"] or "") != target["protocol_version"]
                    or str(row["version_protocol"] or "") != target["protocol_version"]
                ):
                    issues.append(f"Bot {bot_id} current protocol 非 target")
                mirrors = (
                    ("binary_path", row["bot_path"], row["version_path"]),
                    ("os", row["bot_os"], row["version_os"]),
                    ("arch", row["bot_arch"], row["version_arch"]),
                    ("format", row["bot_format"], row["version_format"]),
                    ("runtime_mode", row["bot_runtime"], row["version_runtime"]),
                )
                if any(
                    str(left or "") != str(right or "")
                    for _, left, right in mirrors
                ):
                    issues.append(f"Bot {bot_id} current mirror 漂移")

            wrong_unretired = int(
                c.execute(
                    "SELECT COUNT(*) FROM bot_versions WHERE bot_id IN "
                    "(SELECT id FROM bots WHERE game_id=?) AND protocol_version<>? "
                    "AND retired_at IS NULL",
                    (gid, target["protocol_version"]),
                ).fetchone()[0]
            )
            if wrong_unretired:
                issues.append(
                    f"{wrong_unretired} 个非 current protocol 版本未退役"
                )
            wrong_active_agents = int(
                c.execute(
                    "SELECT COUNT(*) FROM local_ai_agents a "
                    "JOIN bots b ON b.id=a.bot_id WHERE a.status='active' "
                    "AND (a.game_id=? OR b.game_id=?) AND NOT ("
                    "a.game_id=? AND b.game_id=? AND "
                    "a.protocol_version=? AND b.protocol_version=?)",
                    (
                        gid,
                        gid,
                        gid,
                        gid,
                        target["protocol_version"],
                        target["protocol_version"],
                    ),
                ).fetchone()[0]
            )
            if wrong_active_agents:
                issues.append(
                    f"{wrong_active_agents} 个非 current protocol Local AI agent 仍 active"
                )

        try:
            marker_retired_count = int(marker_data["retired_count"])
        except (KeyError, TypeError, ValueError):
            marker_retired_count = -1
        if rule_only:
            if marker_retired_count != 0:
                issues.append("同协议 rule-only marker retired_count 必须为 0")
        else:
            source_version_count = int(
                c.execute(
                    "SELECT COUNT(*) FROM bot_versions WHERE bot_id IN "
                    "(SELECT id FROM bots WHERE game_id=?) AND protocol_version=?",
                    (gid, source["protocol_version"]),
                ).fetchone()[0]
            )
            if source_version_count != marker_retired_count:
                issues.append("marker retired_count 与 source protocol 审计集不匹配")
        active_legacy_jobs = int(
            c.execute(
                "SELECT COUNT(*) FROM execution_jobs WHERE game_id=? AND "
                "status IN ('queued','starting','running','settling') AND "
                "ruleset_version=? AND protocol_version=? AND rating_pool_id=?",
                (
                    gid,
                    source["ruleset_version"],
                    source["protocol_version"],
                    source["rating_pool_id"],
                ),
            ).fetchone()[0]
        )
        retryable_legacy_jobs = int(
            c.execute(
                "SELECT COUNT(*) FROM execution_jobs WHERE game_id=? "
                "AND status='interrupted' AND retryable<>0 AND "
                "ruleset_version=? AND protocol_version=? AND rating_pool_id=?",
                (
                    gid,
                    source["ruleset_version"],
                    source["protocol_version"],
                    source["rating_pool_id"],
                ),
            ).fetchone()[0]
        )
        if active_legacy_jobs or retryable_legacy_jobs:
            issues.append(
                "legacy contract job 仍可运行/\u91cd\u8bd5: "
                f"active={active_legacy_jobs} retryable={retryable_legacy_jobs}"
            )

        archive = c.execute(
            "SELECT * FROM rating_pool_archives WHERE game_id=? AND pool_id=?",
            (gid, source["rating_pool_id"]),
        ).fetchone()
        if archive is None:
            issues.append("legacy rating archive 缺失")
        else:
            archived_ratings = [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM ratings_archive WHERE game_id=? AND pool_id=? "
                    "ORDER BY bot_id",
                    (gid, source["rating_pool_id"]),
                ).fetchall()
            ]
            archived_history = [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM rating_history_archive WHERE game_id=? AND pool_id=? "
                    "ORDER BY original_id",
                    (gid, source["rating_pool_id"]),
                ).fetchall()
            ]
            archived_pairs = [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM pair_stats_archive WHERE game_id=? AND pool_id=? "
                    "ORDER BY bot_a_id,bot_b_id",
                    (gid, source["rating_pool_id"]),
                ).fetchall()
            ]
            digest = rating_projection_digest(
                archived_ratings, archived_history, archived_pairs
            )
            if (
                str(archive["ruleset_version"] or "") != source["ruleset_version"]
                or str(archive["protocol_version"] or "")
                != source["protocol_version"]
            ):
                issues.append("legacy rating archive contract 漂移")
            if (
                len(archived_ratings) != int(archive["ratings_count"])
                or len(archived_history) != int(archive["history_count"])
                or len(archived_pairs) != int(archive["pair_count"])
            ):
                issues.append("legacy rating archive count 漂移")
            if (
                digest != str(archive["projection_digest"] or "")
                or digest != str(marker_data.get("archive_digest") or "")
            ):
                issues.append("legacy rating archive digest 漂移")
        if enforce_live_generation and not self._rating_projection_status_tx(c)["ready"]:
            issues.append("target rating projection 未 ready")

        if issues:
            raise RuntimeError(
                "cutover postcondition drift: " + "; ".join(issues[:12])
            )

    @staticmethod
    def _protocol_cutover_chain_tx(
        c: sqlite3.Connection, game_id: str
    ) -> list[sqlite3.Row]:
        markers = c.execute(
            "SELECT * FROM protocol_cutovers WHERE game_id=?",
            (game_id,),
        ).fetchall()
        if not markers:
            return []

        def before(row: sqlite3.Row) -> tuple[str, str, str]:
            return (
                str(row["from_ruleset"]), str(row["from_protocol"]),
                str(row["from_rating_pool"]),
            )

        def after(row: sqlite3.Row) -> tuple[str, str, str]:
            return (
                str(row["to_ruleset"]), str(row["to_protocol"]),
                str(row["to_rating_pool"]),
            )

        by_source: dict[tuple[str, str, str], sqlite3.Row] = {}
        targets: set[tuple[str, str, str]] = set()
        for marker in markers:
            source = before(marker)
            target = after(marker)
            if source[1] == target[1] and (
                source[0] == target[0] or source[2] == target[2]
            ):
                raise RuntimeError(
                    "同协议 rule-only marker 必须同时更换 ruleset/rating pool"
                )
            if source == target or source in by_source or target in targets:
                raise RuntimeError(
                    "cutover marker chain 存在环、分叉或合并"
                )
            by_source[source] = marker
            targets.add(target)
        heads = [marker for marker in markers if before(marker) not in targets]
        if len(heads) != 1:
            raise RuntimeError("cutover marker chain 断链或多起点")
        ordered: list[sqlite3.Row] = []
        current = heads[0]
        seen: set[str] = set()
        while current is not None:
            cutover_id = str(current["cutover_id"])
            if cutover_id in seen:
                raise RuntimeError("cutover marker chain 存在环")
            seen.add(cutover_id)
            ordered.append(current)
            current = by_source.get(after(current))
        if len(ordered) != len(markers):
            raise RuntimeError("cutover marker chain 断链或分叉")
        protocol_generations = [str(ordered[0]["from_protocol"])]
        seen_protocols = set(protocol_generations)
        ruleset_generations = [str(ordered[0]["from_ruleset"])]
        seen_rulesets = set(ruleset_generations)
        rating_pool_generations = [str(ordered[0]["from_rating_pool"])]
        seen_rating_pools = set(rating_pool_generations)
        for marker in ordered:
            from_protocol = str(marker["from_protocol"])
            to_protocol = str(marker["to_protocol"])
            if from_protocol != protocol_generations[-1]:
                raise RuntimeError("cutover marker protocol 代际断链")
            if to_protocol != from_protocol:
                if to_protocol in seen_protocols:
                    raise RuntimeError(
                        "cutover marker chain 禁止回用 protocol 代际 ID"
                    )
                seen_protocols.add(to_protocol)
                protocol_generations.append(to_protocol)
            from_ruleset = str(marker["from_ruleset"])
            to_ruleset = str(marker["to_ruleset"])
            if from_ruleset != ruleset_generations[-1]:
                raise RuntimeError("cutover marker ruleset 代际断链")
            if to_ruleset != from_ruleset:
                if to_ruleset in seen_rulesets:
                    raise RuntimeError("cutover marker chain 禁止回用 ruleset 代际 ID")
                seen_rulesets.add(to_ruleset)
                ruleset_generations.append(to_ruleset)
            from_pool = str(marker["from_rating_pool"])
            to_pool = str(marker["to_rating_pool"])
            if from_pool != rating_pool_generations[-1]:
                raise RuntimeError("cutover marker rating pool 代际断链")
            if to_pool != from_pool:
                if to_pool in seen_rating_pools:
                    raise RuntimeError(
                        "cutover marker chain 禁止回用 rating pool 代际 ID"
                    )
                seen_rating_pools.add(to_pool)
                rating_pool_generations.append(to_pool)
        return ordered

    def assert_protocol_cutover_postconditions(
        self,
        cutover_id: str | None = None,
        *,
        expected_manifest_digest: str | None = None,
    ) -> None:
        """Fail closed if an applied cutover marker no longer matches reality."""

        with self._tx() as c:
            selected_id = None
            if cutover_id is not None:
                marker = c.execute(
                    "SELECT * FROM protocol_cutovers WHERE cutover_id=?",
                    (str(cutover_id or "").strip(),),
                ).fetchone()
                if marker is None:
                    raise RuntimeError("cutover marker 不存在")
                selected_id = str(marker["cutover_id"])
                game_ids = [str(marker["game_id"])]
            else:
                game_ids = [
                    str(row["game_id"])
                    for row in c.execute(
                        "SELECT DISTINCT game_id FROM protocol_cutovers ORDER BY game_id"
                    ).fetchall()
                ]
            for game_id in game_ids:
                chain = self._protocol_cutover_chain_tx(c, game_id)
                for index, marker in enumerate(chain):
                    if selected_id is not None and str(marker["cutover_id"]) != selected_id:
                        continue
                    if (
                        expected_manifest_digest is not None
                        and str(marker["manifest_digest"] or "")
                        != str(expected_manifest_digest)
                    ):
                        raise RuntimeError("cutover marker manifest digest 不匹配")
                    self._assert_protocol_cutover_postconditions_tx(
                        c,
                        marker,
                        verify_assets=True,
                        enforce_live_generation=index == len(chain) - 1,
                    )

    def runtime_contract_drift(self) -> list[dict[str, Any]]:
        """Compare durable active contracts with the loaded GameSpec registry.

        This is intentionally not part of ``Store.__init__``: offline migration,
        planning and cutover commands must be able to open a legacy database.
        ASGI runtime startup calls it before acquiring the dispatcher or opening
        any Bot/preflight runtime.
        """

        from bzplat.backend.games import registry

        expected = {
            game_id: {
                "ruleset_version": registry.get(game_id).ruleset_id,
                "protocol_version": registry.get(game_id).protocol_version,
                "rating_pool_id": registry.get(game_id).rating_pool_id,
            }
            for game_id in registry.all_ids()
        }
        with self._tx() as c:
            actual = {
                str(row["game_id"]): {
                    "ruleset_version": str(row["ruleset_version"] or ""),
                    "protocol_version": str(row["protocol_version"] or ""),
                    "rating_pool_id": str(row["active_pool_id"] or ""),
                }
                for row in c.execute(
                    "SELECT game_id,ruleset_version,protocol_version,active_pool_id "
                    "FROM rating_pool_state"
                ).fetchall()
            }
        return [
            {
                "game_id": game_id,
                "expected": expected.get(game_id),
                "actual": actual.get(game_id),
            }
            for game_id in sorted(set(expected) | set(actual))
            if expected.get(game_id) != actual.get(game_id)
        ]

    def assert_runtime_contracts_current(self) -> None:
        """Fail closed before online runtime can reinterpret legacy entities."""

        drift = self.runtime_contract_drift()
        if drift:
            detail = "; ".join(
                f"{item['game_id']}: actual={item['actual']!r}, "
                f"expected={item['expected']!r}"
                for item in drift
            )
            raise RuntimeError(
                "持久化游戏契约与当前裁判不一致；拒绝启动在线 runtime。"
                "请先停服并执行适用的离线 game-rule-cutover 或 "
                "game-contract-cutover（见 doc/RUNTIME.md）。" + detail
            )
        self.assert_protocol_cutover_postconditions()

    def get_bot(self, bot_id: int) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            )

    def get_bot_by_owner_name(self, owner_id: int, name: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM bots WHERE owner_id=? AND name=?",
                    (owner_id, name),
                ).fetchone()
            )

    def update_bot(self, bot_id: int, **fields: Any) -> dict | None:
        allowed = {
            "display_name",
            "description",
            "os",
            "arch",
            "format",
            "binary_path",
            "current_version",
            "is_active",
            "is_builtin",
            "game_id",
            "runtime_mode",
            "updated_at",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            projection_guard: _RatingProjectionMutationGuard | None = None
            revoked_targets: list[dict[str, Any]] = []
            # Active/inactive is the ordinary reversible leaderboard visibility
            # mutation.  Identity/game changes remain fail-closed and can only be
            # reconciled by the offline rebuild workflow.
            if "is_active" in fields:
                c.execute("BEGIN IMMEDIATE")
                if not {"game_id", "format", "os", "arch"}.intersection(fields):
                    projection_guard = self._rating_projection_mutation_guard_tx(c)
            disabling = "is_active" in fields and not bool(fields["is_active"])
            if sets:
                if "updated_at" not in fields:
                    sets.append("updated_at=?")
                    vals.append(_now())
                vals.append(bot_id)
                c.execute(f"UPDATE bots SET {','.join(sets)} WHERE id=?", vals)
            if disabling:
                revoked_targets = self._revoke_local_ai_agents_tx(
                    c,
                    scope_column="bot_id",
                    scope_id=int(bot_id),
                    reason="bot_disabled",
                )
            if projection_guard is not None:
                self._advance_rating_projection_state_tx(c, projection_guard)
            result = _row(
                c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            )
            if result is not None and disabling:
                result["_revoked_local_ai_targets"] = revoked_targets
                result["_local_ai_revocation_scope"] = {
                    "kind": "bot",
                    "id": int(bot_id),
                }
            return result

    def update_admin_bot(self, bot_id: int, **fields: Any) -> dict | None:
        """Apply an admin edit without racing owner deletion into a 500.

        Display metadata remains editable for historical tombstones.  Only an
        activation attempt is rejected, and the tombstone check shares the
        same ``BEGIN IMMEDIATE`` transaction as the update.
        """

        allowed = {
            "display_name",
            "description",
            "os",
            "arch",
            "format",
            "binary_path",
            "current_version",
            "is_active",
            "is_builtin",
            "game_id",
            "runtime_mode",
            "updated_at",
        }
        clean = {key: value for key, value in fields.items() if key in allowed}
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            revoked_targets: list[dict[str, Any]] = []
            bot = c.execute(
                "SELECT * FROM bots WHERE id=?", (int(bot_id),)
            ).fetchone()
            if bot is None:
                return None
            if bot["owner_deleted_at"] is not None and bool(
                clean.get("is_active")
            ):
                raise BotDeletedError("Bot 已删除，不能重新启用")

            projection_guard = (
                self._rating_projection_mutation_guard_tx(c)
                if "is_active" in clean
                else None
            )
            if clean:
                if "updated_at" not in clean:
                    clean["updated_at"] = _now()
                sets = ",".join(f"{key}=?" for key in clean)
                c.execute(
                    f"UPDATE bots SET {sets} WHERE id=?",
                    (*clean.values(), int(bot_id)),
                )
            if "is_active" in clean and not bool(clean["is_active"]):
                revoked_targets = self._revoke_local_ai_agents_tx(
                    c,
                    scope_column="bot_id",
                    scope_id=int(bot_id),
                    reason="bot_disabled",
                )
            if projection_guard is not None:
                self._advance_rating_projection_state_tx(c, projection_guard)
            result = _row(
                c.execute(
                    "SELECT * FROM bots WHERE id=?", (int(bot_id),)
                ).fetchone()
            )
            if result is not None and "is_active" in clean and not bool(
                clean["is_active"]
            ):
                result["_revoked_local_ai_targets"] = revoked_targets
                result["_local_ai_revocation_scope"] = {
                    "kind": "bot",
                    "id": int(bot_id),
                }
            return result

    def update_owned_bot(
        self, owner_id: int, bot_id: int, **fields: Any
    ) -> dict:
        """Atomically apply an owner mutation only to a live inventory row."""

        allowed = {"display_name", "description", "is_active"}
        sets = [f"{key}=?" for key in fields if key in allowed]
        vals = [value for key, value in fields.items() if key in allowed]
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            revoked_targets: list[dict[str, Any]] = []
            bot = c.execute(
                "SELECT * FROM bots WHERE id=?", (int(bot_id),)
            ).fetchone()
            if bot is None:
                raise LookupError("bot 不存在")
            if int(bot["owner_id"]) != int(owner_id):
                raise PermissionError("无权修改他人的 Bot")
            if bot["owner_deleted_at"] is not None:
                raise BotDeletedError("Bot 已删除，不能再修改")

            projection_guard = (
                self._rating_projection_mutation_guard_tx(c)
                if "is_active" in fields
                else None
            )
            if sets:
                if "updated_at" not in fields:
                    sets.append("updated_at=?")
                    vals.append(_now())
                vals.extend((int(bot_id), int(owner_id)))
                changed = c.execute(
                    f"UPDATE bots SET {','.join(sets)} "
                    "WHERE id=? AND owner_id=? AND owner_deleted_at IS NULL",
                    vals,
                )
                if changed.rowcount != 1:
                    raise BotDeletedError("Bot 已删除，不能再修改")
            if "is_active" in fields and not bool(fields["is_active"]):
                revoked_targets = self._revoke_local_ai_agents_tx(
                    c,
                    scope_column="bot_id",
                    scope_id=int(bot_id),
                    reason="bot_disabled",
                )
            if projection_guard is not None:
                self._advance_rating_projection_state_tx(c, projection_guard)
            result = _row(
                c.execute("SELECT * FROM bots WHERE id=?", (int(bot_id),)).fetchone()
            )
            if result is not None and "is_active" in fields and not bool(
                fields["is_active"]
            ):
                result["_revoked_local_ai_targets"] = revoked_targets
                result["_local_ai_revocation_scope"] = {
                    "kind": "bot",
                    "id": int(bot_id),
                }
            return result

    def publish_uploaded_bot(self, owner_id: int, bot_id: int) -> dict:
        """Atomically publish a staged first version and fill an empty rank slot.

        The binary/version commit happens before this final visibility boundary.
        Taking the write lock before re-reading the tombstone makes publication
        linearizable with owner deletion across processes: deletion either sees
        the active Bot, or publication observes ``owner_deleted_at`` and refuses
        to revive it.
        """

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            bot = c.execute(
                "SELECT * FROM bots WHERE id=?", (int(bot_id),)
            ).fetchone()
            if bot is None:
                raise LookupError("bot 不存在")
            if int(bot["owner_id"]) != int(owner_id):
                raise PermissionError("无权发布他人的 Bot")
            if bot["owner_deleted_at"] is not None:
                raise BotDeletedError("Bot 已删除，不能发布上传版本")
            if int(bot["current_version"] or 0) <= 0:
                raise ValueError("Bot 首版尚未提交，不能发布")

            guard = self._rating_projection_mutation_guard_tx(c)
            now = _now()
            activated = c.execute(
                "UPDATE bots SET is_active=1,updated_at=? "
                "WHERE id=? AND owner_id=? AND owner_deleted_at IS NULL",
                (now, int(bot_id), int(owner_id)),
            )
            if activated.rowcount != 1:  # pragma: no cover - write lock invariant
                raise BotDeletedError("Bot 已删除，不能发布上传版本")

            current = c.execute(
                "SELECT id FROM bots WHERE owner_id=? AND game_id=? "
                "AND is_ranked=1",
                (int(owner_id), str(bot["game_id"])),
            ).fetchone()
            if current is None:
                candidate = self._ranked_bot_candidate_tx(
                    c, owner_id=int(owner_id), bot_id=int(bot_id)
                )
                if candidate is None:
                    raise ValueError("Bot 当前不可运行，不能发布")
                selected = c.execute(
                    "UPDATE bots SET is_ranked=1,updated_at=? "
                    "WHERE id=? AND owner_id=? AND game_id=? AND is_active=1 "
                    "AND owner_deleted_at IS NULL",
                    (
                        now,
                        int(bot_id),
                        int(owner_id),
                        str(bot["game_id"]),
                    ),
                )
                if selected.rowcount != 1:  # pragma: no cover - write lock invariant
                    raise RuntimeError("排位 Bot 原子派遣失败")
            self._advance_rating_projection_state_tx(c, guard)
            return _row(
                c.execute("SELECT * FROM bots WHERE id=?", (int(bot_id),)).fetchone()
            )

    @staticmethod
    def _ranked_bot_candidate_tx(
        c: sqlite3.Connection, *, owner_id: int, bot_id: int
    ) -> sqlite3.Row | None:
        bot = c.execute(
            "SELECT * FROM bots WHERE id=? AND owner_id=?",
            (int(bot_id), int(owner_id)),
        ).fetchone()
        if (
            bot is None
            or bot["owner_deleted_at"] is not None
            or int(bot["is_active"] or 0) != 1
        ):
            return None
        if (
            str(bot["binary_path"] or "").strip() == ""
            or str(bot["format"] or "") != SUPPORTED_BINARY_FORMAT
            or str(bot["os"] or "") != SUPPORTED_BINARY_OS
            or str(bot["arch"] or "") != SUPPORTED_BINARY_ARCH
        ):
            return None
        contract = _active_game_contract_tx(c, str(bot["game_id"]))
        if str(bot["protocol_version"] or "") != contract["protocol_version"]:
            return None
        current_version = int(bot["current_version"] or 0)
        if current_version == 0:
            has_versions = c.execute(
                "SELECT 1 FROM bot_versions WHERE bot_id=? LIMIT 1", (int(bot_id),)
            ).fetchone()
            return bot if has_versions is None else None
        version = c.execute(
            "SELECT * FROM bot_versions WHERE bot_id=? AND version=?",
            (int(bot_id), current_version),
        ).fetchone()
        if (
            version is None
            or version["retired_at"] is not None
            or str(version["binary_path"] or "") != str(bot["binary_path"] or "")
            or str(version["runtime_mode"] or "") != str(bot["runtime_mode"] or "")
            or str(version["protocol_version"] or "") != contract["protocol_version"]
            or str(version["format"] or "") != SUPPORTED_BINARY_FORMAT
            or str(version["os"] or "") != SUPPORTED_BINARY_OS
            or str(version["arch"] or "") != SUPPORTED_BINARY_ARCH
        ):
            return None
        return bot

    @staticmethod
    def _ranked_bot_busy_tx(
        c: sqlite3.Connection, *, bot_id: int, game_id: str
    ) -> bool:
        active_job = c.execute(
            "SELECT 1 FROM execution_jobs WHERE rated=1 "
            "AND status IN ('starting','running','settling') "
            "AND game_id=? AND ? IN (bot_a_id,bot_b_id) LIMIT 1",
            (str(game_id), int(bot_id)),
        ).fetchone()
        if active_job is not None:
            return True
        table = _matches_table(game_id)
        busy_match = c.execute(
            f"SELECT 1 FROM {table} m "
            "LEFT JOIN match_rating_policies policy ON policy.match_id=m.id "
            "LEFT JOIN match_rating_settlements settled ON settled.match_id=m.id "
            "WHERE COALESCE(policy.rated,1)=1 "
            "AND ? IN (m.bot_a_id,m.bot_b_id) "
            "AND (m.status IN (?,?) OR "
            "(m.status=? AND settled.match_id IS NULL)) LIMIT 1",
            (
                int(bot_id),
                STATUS_PENDING,
                STATUS_RUNNING,
                STATUS_COMPLETED,
            ),
        ).fetchone()
        return busy_match is not None

    @staticmethod
    def _cancel_ranked_bot_queued_jobs_tx(
        c: sqlite3.Connection, *, bot_id: int, game_id: str, terminal_at: str
    ) -> int:
        rows = c.execute(
            "SELECT id,auto_decision_id FROM execution_jobs WHERE rated=1 "
            "AND status='queued' AND game_id=? AND ? IN (bot_a_id,bot_b_id) "
            "ORDER BY id",
            (str(game_id), int(bot_id)),
        ).fetchall()
        if not rows:
            return 0
        ids = [int(row["id"]) for row in rows]
        placeholders = ",".join("?" for _ in ids)
        c.execute(
            "UPDATE execution_jobs SET status='cancelled',retryable=0,"
            "terminal_reason='ranking_entry_changed',"
            "last_error='ranking_entry_changed',next_attempt_at=NULL,terminal_at=? "
            f"WHERE status='queued' AND id IN ({placeholders})",
            (terminal_at, *ids),
        )
        decision_ids = [
            int(row["auto_decision_id"])
            for row in rows
            if row["auto_decision_id"] is not None
        ]
        if decision_ids:
            decision_placeholders = ",".join("?" for _ in decision_ids)
            c.execute(
                "UPDATE auto_match_decisions SET lifecycle='cancelled',"
                "terminal_reason='ranking_entry_changed',terminal_at=? "
                f"WHERE lifecycle='queued' AND id IN ({decision_placeholders})",
                (terminal_at, *decision_ids),
            )
        return len(ids)

    def select_ranked_bot(
        self, owner_id: int, bot_id: int, *, if_empty: bool = False
    ) -> dict:
        """Atomically replace one owner's ranked representative for a game."""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            target = c.execute(
                "SELECT * FROM bots WHERE id=?", (int(bot_id),)
            ).fetchone()
            if target is None:
                raise LookupError("bot 不存在")
            if int(target["owner_id"]) != int(owner_id):
                raise PermissionError("无权修改他人的 Bot")
            if target["owner_deleted_at"] is not None:
                raise BotDeletedError("Bot 已删除，不能再修改")
            current = c.execute(
                "SELECT * FROM bots WHERE owner_id=? AND game_id=? AND is_ranked=1",
                (int(owner_id), str(target["game_id"])),
            ).fetchone()
            if current is not None and (
                if_empty or int(current["id"]) == int(bot_id)
            ):
                return {
                    "bot": _row(target),
                    "selected_bot_id": int(current["id"]),
                    "previous_bot_id": int(current["id"]),
                    "cancelled_queued_jobs": 0,
                    "changed": False,
                }
            candidate = self._ranked_bot_candidate_tx(
                c, owner_id=int(owner_id), bot_id=int(bot_id)
            )
            if candidate is None:
                raise ValueError("Bot 当前未启用或不可运行，不能参加排位")
            previous_id = int(current["id"]) if current is not None else None
            if current is not None and self._ranked_bot_busy_tx(
                c,
                bot_id=int(current["id"]),
                game_id=str(current["game_id"]),
            ):
                raise RankedBotSelectionBusyError(
                    "当前排位 Bot 仍有进行中或待结算的计分对局"
                )
            now = _now()
            cancelled = 0
            if current is not None:
                cancelled = self._cancel_ranked_bot_queued_jobs_tx(
                    c,
                    bot_id=int(current["id"]),
                    game_id=str(current["game_id"]),
                    terminal_at=now,
                )
            guard = self._rating_projection_mutation_guard_tx(c)
            c.execute(
                "UPDATE bots SET is_ranked=0,updated_at=? "
                "WHERE owner_id=? AND game_id=? AND is_ranked=1",
                (now, int(owner_id), str(target["game_id"])),
            )
            changed = c.execute(
                "UPDATE bots SET is_ranked=1,updated_at=? "
                "WHERE id=? AND owner_id=? AND game_id=? AND is_active=1",
                (
                    now,
                    int(bot_id),
                    int(owner_id),
                    str(target["game_id"]),
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("排位 Bot 原子切换失败")
            self._advance_rating_projection_state_tx(c, guard)
            selected = c.execute(
                "SELECT * FROM bots WHERE id=?", (int(bot_id),)
            ).fetchone()
            return {
                "bot": _row(selected),
                "selected_bot_id": int(bot_id),
                "previous_bot_id": previous_id,
                "cancelled_queued_jobs": cancelled,
                "changed": True,
            }

    def clear_ranked_bot(self, owner_id: int, bot_id: int) -> dict:
        """Withdraw one selected Bot while retaining its complete rating history."""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            bot = c.execute("SELECT * FROM bots WHERE id=?", (int(bot_id),)).fetchone()
            if bot is None:
                raise LookupError("bot 不存在")
            if int(bot["owner_id"]) != int(owner_id):
                raise PermissionError("无权修改他人的 Bot")
            if bot["owner_deleted_at"] is not None:
                raise BotDeletedError("Bot 已删除，不能再修改")
            if int(bot["is_ranked"] or 0) != 1:
                return {
                    "bot": _row(bot),
                    "selected_bot_id": None,
                    "previous_bot_id": None,
                    "cancelled_queued_jobs": 0,
                    "changed": False,
                }
            if self._ranked_bot_busy_tx(
                c, bot_id=int(bot_id), game_id=str(bot["game_id"])
            ):
                raise RankedBotSelectionBusyError(
                    "当前排位 Bot 仍有进行中或待结算的计分对局"
                )
            now = _now()
            cancelled = self._cancel_ranked_bot_queued_jobs_tx(
                c,
                bot_id=int(bot_id),
                game_id=str(bot["game_id"]),
                terminal_at=now,
            )
            guard = self._rating_projection_mutation_guard_tx(c)
            c.execute(
                "UPDATE bots SET is_ranked=0,updated_at=? "
                "WHERE id=? AND owner_id=? AND is_ranked=1",
                (now, int(bot_id), int(owner_id)),
            )
            self._advance_rating_projection_state_tx(c, guard)
            updated = c.execute(
                "SELECT * FROM bots WHERE id=?", (int(bot_id),)
            ).fetchone()
            return {
                "bot": _row(updated),
                "selected_bot_id": None,
                "previous_bot_id": int(bot_id),
                "cancelled_queued_jobs": cancelled,
                "changed": True,
            }

    def owner_delete_bot(self, owner_id: int, bot_id: int) -> dict:
        """Remove one Bot from its owner's inventory without erasing history.

        The tombstone, queue convergence, ranking withdrawal, and local-agent
        revocation share one ``BEGIN IMMEDIATE`` boundary.  A claim/version
        commit in another process therefore lands wholly before deletion (and
        is observed as busy) or wholly after it (and sees an inactive deleted
        identity).
        """

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            bot = c.execute(
                "SELECT * FROM bots WHERE id=?", (int(bot_id),)
            ).fetchone()
            if bot is None:
                raise LookupError("bot 不存在")
            if int(bot["owner_id"]) != int(owner_id):
                raise PermissionError("无权删除他人的 Bot")

            # Repeated owner DELETE is a successful no-op.  Returning the same
            # durable transport identities lets a retry close the in-memory
            # sockets even if the first request was cancelled after DB commit.
            if bot["owner_deleted_at"] is not None:
                public_ids = [
                    str(row[0])
                    for row in c.execute(
                        "SELECT public_id FROM local_ai_agents "
                        "WHERE bot_id=? ORDER BY id",
                        (int(bot_id),),
                    ).fetchall()
                ]
                return {
                    "bot": _row(bot),
                    "changed": False,
                    "cancelled_queued_jobs": 0,
                    "invalidated_retryable_jobs": 0,
                    "revoked_local_ai_public_ids": public_ids,
                }

            active_contest = c.execute(
                "SELECT c.id FROM contests c WHERE c.status IN (?,?,?,?) AND ("
                "EXISTS(SELECT 1 FROM contest_entries entry "
                "WHERE entry.contest_id=c.id AND entry.bot_id=?) OR "
                "EXISTS(SELECT 1 FROM contest_pairings pairing "
                "WHERE pairing.contest_id=c.id "
                "AND ? IN (pairing.bot_a_id,pairing.bot_b_id))) LIMIT 1",
                (
                    CONTEST_OPEN,
                    CONTEST_PUBLISHED,
                    CONTEST_RUNNING,
                    CONTEST_REST,
                    int(bot_id),
                    int(bot_id),
                ),
            ).fetchone()
            if active_contest is not None:
                raise BotOwnerDeleteBusyError(
                    "bot_busy",
                    "Bot 仍在未结束赛事中，请联系赛事组织者移出名册，或等待赛事结束",
                )

            if self._ranked_bot_busy_tx(
                c, bot_id=int(bot_id), game_id=str(bot["game_id"])
            ):
                raise BotOwnerDeleteBusyError(
                    "ranking_busy",
                    "Bot 仍有进行中或待结算的计分对局",
                )

            active_job = c.execute(
                "SELECT 1 FROM execution_jobs WHERE status IN "
                "('starting','running','settling') "
                "AND ? IN (bot_a_id,bot_b_id) LIMIT 1",
                (int(bot_id),),
            ).fetchone()
            if active_job is not None:
                raise BotOwnerDeleteBusyError(
                    "bot_busy", "Bot 仍有正在执行或收尾的对局"
                )
            for game_id in _all_game_ids():
                table = _matches_table(game_id)
                if c.execute(
                    f"SELECT 1 FROM {table} WHERE status IN (?,?) "
                    "AND ? IN (bot_a_id,bot_b_id) LIMIT 1",
                    (STATUS_PENDING, STATUS_RUNNING, int(bot_id)),
                ).fetchone() is not None:
                    raise BotOwnerDeleteBusyError(
                        "bot_busy", "Bot 仍有待开始或进行中的对局"
                    )

            now = _now()
            queued_rows = c.execute(
                "SELECT id,auto_decision_id FROM execution_jobs "
                "WHERE status='queued' AND ? IN (bot_a_id,bot_b_id) ORDER BY id",
                (int(bot_id),),
            ).fetchall()
            queued_ids = [int(row["id"]) for row in queued_rows]
            if queued_ids:
                marks = ",".join("?" for _ in queued_ids)
                c.execute(
                    "UPDATE execution_jobs SET status='cancelled',cancel_requested=1,"
                    "retryable=0,terminal_reason='bot_owner_deleted',"
                    "last_error='bot_owner_deleted',next_attempt_at=NULL,terminal_at=? "
                    f"WHERE status='queued' AND id IN ({marks})",
                    (now, *queued_ids),
                )
                decision_ids = [
                    int(row["auto_decision_id"])
                    for row in queued_rows
                    if row["auto_decision_id"] is not None
                ]
                if decision_ids:
                    decision_marks = ",".join("?" for _ in decision_ids)
                    c.execute(
                        "UPDATE auto_match_decisions SET lifecycle='cancelled',"
                        "terminal_reason='bot_owner_deleted',terminal_at=? "
                        f"WHERE lifecycle='queued' AND id IN ({decision_marks})",
                        (now, *decision_ids),
                    )

            invalidated_retryable = c.execute(
                "UPDATE execution_jobs SET retryable=0,next_attempt_at=NULL "
                "WHERE status='interrupted' AND retryable=1 "
                "AND ? IN (bot_a_id,bot_b_id)",
                (int(bot_id),),
            ).rowcount

            active_agents = c.execute(
                "SELECT id,public_id FROM local_ai_agents "
                "WHERE bot_id=? AND status='active' ORDER BY id",
                (int(bot_id),),
            ).fetchall()
            agent_ids = [int(row["id"]) for row in active_agents]
            if agent_ids:
                marks = ",".join("?" for _ in agent_ids)
                c.execute(
                    "UPDATE local_ai_agents SET status='revoked',"
                    "connection_generation=connection_generation+1,connected_at=NULL,"
                    f"disconnected_at=?,updated_at=? WHERE id IN ({marks})",
                    (now, now, *agent_ids),
                )
                c.execute(
                    "UPDATE local_ai_leases SET status='released',released_at=?,"
                    f"terminal_reason='bot_owner_deleted' WHERE status='active' "
                    f"AND agent_id IN ({marks})",
                    (now, *agent_ids),
                )

            projection_guard = (
                self._rating_projection_mutation_guard_tx(c)
                if int(bot["is_active"] or 0) or int(bot["is_ranked"] or 0)
                else None
            )
            changed = c.execute(
                "UPDATE bots SET owner_deleted_at=?,is_active=0,is_ranked=0,"
                "updated_at=? WHERE id=? AND owner_id=? "
                "AND owner_deleted_at IS NULL",
                (now, now, int(bot_id), int(owner_id)),
            )
            if changed.rowcount != 1:  # pragma: no cover - write lock invariant
                raise RuntimeError("Bot 删除 tombstone 原子写入失败")
            if projection_guard is not None:
                self._advance_rating_projection_state_tx(c, projection_guard)
            updated = c.execute(
                "SELECT * FROM bots WHERE id=?", (int(bot_id),)
            ).fetchone()
            return {
                "bot": _row(updated),
                "changed": True,
                "cancelled_queued_jobs": len(queued_ids),
                "invalidated_retryable_jobs": int(invalidated_retryable),
                "revoked_local_ai_public_ids": [
                    str(row["public_id"]) for row in active_agents
                ],
            }

    def delete_bot(self, bot_id: int) -> bool:
        # 注意：此处不做一般「活跃引用」业务校验——那是 admin_delete_bot
        # 端点的职责。不过 retired version 是规则迁移的不可删审计证据，任何
        # Store 入口都不得借 bots→bot_versions CASCADE 绕过这一硬边界；owner
        # 墓碑也只能由显式 admin hard-delete 流程删除，不能被通用清理旁路。
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            bot = c.execute(
                "SELECT owner_deleted_at FROM bots WHERE id=?", (bot_id,)
            ).fetchone()
            if bot is None:
                return False
            if bot["owner_deleted_at"] is not None:
                raise BotDeletedError("Bot 已由 owner 删除，禁止通用硬删除")
            if _cutover_audit_version_count_tx(c, bot_id=bot_id):
                raise ValueError("规则迁移版本是不可删除审计证据，禁止删除 Bot")
            _delete_social_target(c, "bot", bot_id)
            return c.execute("DELETE FROM bots WHERE id=?", (bot_id,)).rowcount > 0

    def delete_unpublished_bot(self, bot_id: int) -> bool:
        """Roll back a failed upload without dirtying a verified projection.

        This is deliberately narrower than either public/admin hard delete: only
        the hidden, versionless staging row and its untouched default rating may
        be removed.  Any observable reference or rating side effect falls back
        to the generic fail-closed deletion path at the caller.
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            bot = c.execute(
                "SELECT id,game_id,is_active,current_version,owner_deleted_at "
                "FROM bots WHERE id=?",
                (bot_id,),
            ).fetchone()
            if (
                bot is None
                or bot["owner_deleted_at"] is not None
                or int(bot["is_active"] or 0) != 0
                or int(bot["current_version"] or 0) != 0
                or c.execute(
                    "SELECT 1 FROM bot_versions WHERE bot_id=? LIMIT 1", (bot_id,)
                ).fetchone()
            ):
                return False
            rating = c.execute(
                "SELECT rating,rd,vol,wins,losses,draws,delta_total,"
                "matches_played,last_played_at FROM ratings "
                "WHERE bot_id=? AND game_id=?",
                (bot_id, bot["game_id"]),
            ).fetchone()
            if rating is None or tuple(rating) != (
                1500.0, 350.0, 0.06, 0, 0, 0, 0, 0, None
            ):
                return False
            if c.execute(
                "SELECT 1 FROM rating_history WHERE bot_id=? LIMIT 1", (bot_id,)
            ).fetchone() or c.execute(
                "SELECT 1 FROM pair_stats WHERE bot_a_id=? OR bot_b_id=? LIMIT 1",
                (bot_id, bot_id),
            ).fetchone():
                return False
            for game_id in _all_game_ids():
                if c.execute(
                    f"SELECT 1 FROM {_matches_table(game_id)} "
                    "WHERE bot_a_id=? OR bot_b_id=? LIMIT 1",
                    (bot_id, bot_id),
                ).fetchone():
                    return False
            if (
                c.execute(
                    "SELECT 1 FROM execution_jobs "
                    "WHERE status IN ('queued','starting','running','settling') "
                    "AND (bot_a_id=? OR bot_b_id=?) LIMIT 1",
                    (bot_id, bot_id),
                ).fetchone()
                or c.execute(
                    "SELECT 1 FROM contest_entries WHERE bot_id=? LIMIT 1", (bot_id,)
                ).fetchone()
                or c.execute(
                    "SELECT 1 FROM contest_pairings "
                    "WHERE bot_a_id=? OR bot_b_id=? LIMIT 1",
                    (bot_id, bot_id),
                ).fetchone()
            ):
                return False
            projection_guard = self._rating_projection_mutation_guard_tx(c)
            _delete_social_target(c, "bot", bot_id)
            deleted = c.execute("DELETE FROM bots WHERE id=?", (bot_id,)).rowcount == 1
            if deleted:
                self._advance_rating_projection_state_tx(c, projection_guard)
            return deleted

    def delete_bot_if_safe(self, bot_id: int) -> dict:
        """仅硬删从未参赛的 Bot，避免永久破坏历史参与者身份。"""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT id FROM bots WHERE id=?", (bot_id,)).fetchone():
                return {"found": False, "deleted": False, "references": {}}

            match_count = 0
            for gid in _all_game_ids():
                table = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) AS n FROM {table} "
                    "WHERE bot_a_id=? OR bot_b_id=?",
                    (bot_id, bot_id),
                ).fetchone()
                match_count += int(row["n"] if row else 0)
            queued_row = c.execute(
                "SELECT COUNT(*) AS n FROM execution_jobs "
                "WHERE status IN ('queued','starting','running','settling') "
                "AND (bot_a_id=? OR bot_b_id=?)",
                (bot_id, bot_id),
            ).fetchone()
            match_count += int(queued_row["n"] if queued_row else 0)

            pairing_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_pairings pairing "
                "WHERE pairing.bot_a_id=? OR pairing.bot_b_id=?",
                (bot_id, bot_id),
            ).fetchone()
            entry_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_entries WHERE bot_id=?",
                (bot_id,),
            ).fetchone()
            refs = {
                "matches": match_count,
                "pairings": int(pairing_row["n"] if pairing_row else 0)
                + int(entry_row["n"] if entry_row else 0),
                "audit_versions": _cutover_audit_version_count_tx(
                    c, bot_id=bot_id
                ),
            }
            if any(refs.values()):
                return {"found": True, "deleted": False, "references": refs}
            projection_guard = self._rating_projection_mutation_guard_tx(c)
            _delete_social_target(c, "bot", bot_id)
            deleted = c.execute("DELETE FROM bots WHERE id=?", (bot_id,)).rowcount > 0
            if deleted:
                self._advance_rating_projection_state_tx(c, projection_guard)
            return {"found": True, "deleted": deleted, "references": refs}

    def bot_active_references(self, bot_id: int) -> dict:
        """检查 bot 是否被**活跃**对局/赛事引用（会因此被破坏才拒绝）。

        返回 {matches: n, pairings: n}，全 0 表示可安全硬删。
        注意：已完成的历史对局/赛事（status=completed/finished）不阻拦——FK SET NULL 会
        保留历史（bot_id→NULL），这是预期行为（见 test_delete_bot_preserves_contest_data）。
        仅阻拦 pending/running 对局 + 未完成赛事的报名/对阵（硬删会破坏进行中的赛事）。
        """
        out = {"matches": 0, "pairings": 0}
        with self._tx() as c:
            # 活跃对局：pending/running 状态（跨每游戏表）
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) AS n FROM {tbl} "
                    f"WHERE (bot_a_id=? OR bot_b_id=?) AND status IN ('pending','running')",
                    (bot_id, bot_id),
                ).fetchone()
                if row and row["n"]:
                    out["matches"] += int(row["n"])
            queued = c.execute(
                "SELECT COUNT(*) AS n FROM execution_jobs "
                "WHERE status IN ('queued','starting','running','settling') "
                "AND (bot_a_id=? OR bot_b_id=?)",
                (bot_id, bot_id),
            ).fetchone()
            if queued and queued["n"]:
                out["matches"] += int(queued["n"])
            # 进行中赛事（running/published/rest）的报名/对阵：硬删会破坏对阵表（CASCADE）。
            # draft/open 的报名可重建（用户重新报名），不阻拦；finished 的历史 SET NULL 保留。
            row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_pairings cp "
                "JOIN contests c ON c.id=cp.contest_id "
                "WHERE (cp.bot_a_id=? OR cp.bot_b_id=?) "
                "AND c.status IN ('published','running','rest')",
                (bot_id, bot_id),
            ).fetchone()
            if row and row["n"]:
                out["pairings"] += int(row["n"])
            row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_entries ce "
                "JOIN contests c ON c.id=ce.contest_id "
                "WHERE ce.bot_id=? AND c.status IN ('published','running','rest')",
                (bot_id,),
            ).fetchone()
            if row and row["n"]:
                out["pairings"] += int(row["n"])
        return out

    def list_bots(
        self,
        owner_id: int | None = None,
        *,
        active_only: bool = True,
        include_owner_deleted: bool = True,
        include_builtin: bool = True,
        runnable_only: bool = False,
        game_id: str | None = None,
        page: int | None = None,
        per_page: int = 50,
    ) -> list[dict] | dict:
        """列 bot。``page`` 为 None 时返回 list（旧契约，部分调用方需全量）；
        ``page`` 给定时返回 ``{"items", "page", "per_page", "total"}``。"""
        with self._tx() as c:
            sql = "SELECT * FROM bots WHERE 1=1"
            params: list[Any] = []
            if owner_id is not None:
                sql += " AND owner_id=?"
                params.append(owner_id)
            if active_only:
                sql += " AND is_active=1"
            if not include_owner_deleted:
                sql += " AND owner_deleted_at IS NULL"
            if runnable_only:
                sql += (
                    " AND is_active=1 AND owner_deleted_at IS NULL "
                    "AND TRIM(binary_path)<>'' "
                    "AND format=? AND os=? AND arch=?"
                )
                params.extend((
                    SUPPORTED_BINARY_FORMAT,
                    SUPPORTED_BINARY_OS,
                    SUPPORTED_BINARY_ARCH,
                ))
                sql += (
                    " AND protocol_version=(SELECT state.protocol_version "
                    "FROM rating_pool_state state WHERE state.game_id=bots.game_id) "
                    "AND ((current_version=0 AND NOT EXISTS(SELECT 1 "
                    "FROM bot_versions any_version WHERE any_version.bot_id=bots.id)) "
                    "OR EXISTS(SELECT 1 FROM bot_versions v "
                    "WHERE v.bot_id=bots.id AND v.version=bots.current_version "
                    "AND v.retired_at IS NULL AND TRIM(v.binary_path)<>'' "
                    "AND v.binary_path=bots.binary_path "
                    "AND v.runtime_mode=bots.runtime_mode "
                    "AND v.protocol_version=bots.protocol_version "
                    "AND v.format=? AND v.os=? AND v.arch=?))"
                )
                params.extend((
                    SUPPORTED_BINARY_FORMAT,
                    SUPPORTED_BINARY_OS,
                    SUPPORTED_BINARY_ARCH,
                ))
            if not include_builtin:
                sql += " AND is_builtin=0"
            if game_id:
                sql += " AND game_id=?"
                params.append(game_id)
            sql += " ORDER BY is_builtin DESC, name"
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, tuple(params), page=page, per_page=pp)
                return {"items": rows, "page": page, "per_page": pp, "total": total}
            return [_row(r) for r in c.execute(sql, params)]

    # ── bot_versions ──────────────────────────────────────────

    def add_bot_version(
        self,
        bot_id: int,
        *,
        binary_path: str,
        upload_note: str = "",
        checksum: str = "",
        size_bytes: int = 0,
        os: str = SUPPORTED_BINARY_OS,
        arch: str = SUPPORTED_BINARY_ARCH,
        format: str = SUPPORTED_BINARY_FORMAT,
        runtime_mode: str | None = None,
        protocol_version: str | None = None,
        version: int | None = None,
    ) -> dict:
        require_supported_binary_metadata(format, os, arch)
        if runtime_mode is not None and runtime_mode not in VALID_RUNTIME_MODES:
            raise ValueError(f"非法 runtime_mode: {runtime_mode}")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            bot_row = c.execute(
                "SELECT game_id,owner_deleted_at,runtime_mode FROM bots WHERE id=?",
                (bot_id,),
            ).fetchone()
            if bot_row is None:
                raise ValueError("bot 不存在")
            if bot_row["owner_deleted_at"] is not None:
                raise BotDeletedError("Bot 已删除，不能再上传版本")
            if runtime_mode is None:
                # Resolve the inherited mode only after taking the write lock,
                # so it belongs to the same snapshot as the tombstone check.
                runtime_mode = (
                    bot_row["runtime_mode"] or DEFAULT_RUNTIME_MODE
                )
            if runtime_mode not in VALID_RUNTIME_MODES:
                raise ValueError(f"非法 runtime_mode: {runtime_mode}")
            active_contract = _active_game_contract_tx(c, bot_row["game_id"])
            protocol = str(
                protocol_version or active_contract["protocol_version"]
            ).strip()
            if protocol != active_contract["protocol_version"]:
                raise ValueError("新版本协议与当前游戏契约不一致")
            if version is None:
                row = c.execute(
                    "SELECT MAX(version) AS mv FROM bot_versions WHERE bot_id=?",
                    (bot_id,),
                ).fetchone()
                version = (row["mv"] or 0) + 1
            cur = c.execute(
                "INSERT INTO bot_versions(bot_id, version, binary_path, "
                "upload_note, checksum, size_bytes, os, arch, format, runtime_mode, "
                "protocol_version,uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    bot_id,
                    version,
                    binary_path,
                    upload_note,
                    checksum,
                    size_bytes,
                    os,
                    arch,
                    format,
                    runtime_mode,
                    protocol,
                    _now(),
                ),
            )
            vid = cur.lastrowid
            c.execute(
                "UPDATE bots SET current_version=?, binary_path=?, os=?, arch=?, "
                "format=?, runtime_mode=?,protocol_version=?, updated_at=? WHERE id=?",
                (version, binary_path, os, arch, format, runtime_mode, protocol, _now(), bot_id),
            )
            return _row(
                c.execute("SELECT * FROM bot_versions WHERE id=?", (vid,)).fetchone()
            )

    def delete_bot_version(self, bot_id: int, version: int) -> bool:
        """删除非当前、非规则迁移审计、未被执行冻结的普通版本。"""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            # 先读当前版本，判定删的是否是当前版本
            cur_bot = c.execute(
                "SELECT current_version,owner_deleted_at FROM bots WHERE id=?",
                (bot_id,),
            ).fetchone()
            if cur_bot and cur_bot["owner_deleted_at"] is not None:
                raise BotDeletedError("Bot 已删除，历史版本不能删除")
            is_current = cur_bot and cur_bot["current_version"] == version

            version_row = c.execute(
                "SELECT id,retired_at FROM bot_versions WHERE bot_id=? AND version=?",
                (bot_id, version),
            ).fetchone()
            if version_row and version_row["retired_at"] is not None:
                raise ValueError("退役版本是规则迁移审计证据，禁止删除")
            if version_row and _version_in_cutover_manifest_tx(
                c, bot_id=bot_id, version=version
            ):
                raise ValueError("该版本被规则切换 marker 引用，禁止删除")
            if version_row and is_current:
                raise ValueError("当前版本禁止删除；请先显式切换到其他兼容版本")
            if version_row and c.execute(
                "SELECT 1 FROM execution_jobs "
                "WHERE status IN ('queued','starting','running','settling') "
                "AND (bot_a_version_id=? OR bot_b_version_id=?) LIMIT 1",
                (version_row["id"], version_row["id"]),
            ).fetchone():
                raise ValueError("该版本已被冻结到执行队列，暂不可删除")

            cur = c.execute(
                "DELETE FROM bot_versions WHERE bot_id=? AND version=?",
                (bot_id, version),
            )
            if cur.rowcount == 0:
                return False
            return True

    def set_current_version(self, bot_id: int, version: int) -> dict | None:
        """回滚到指定版本（不删除其他版本）：把 bots 镜像切到该版本的
        binary_path/os/arch/format/runtime_mode，current_version=version。

        用于 MyBots「回滚到此版本」。版本不存在返回 None。
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT v.version,v.binary_path,v.os,v.arch,v.format,v.runtime_mode,"
                "v.protocol_version,v.retired_at,b.game_id,b.owner_deleted_at "
                "FROM bot_versions v "
                "JOIN bots b ON b.id=v.bot_id "
                "WHERE v.bot_id=? AND v.version=?",
                (bot_id, version),
            ).fetchone()
            if not row:
                return None
            if row["owner_deleted_at"] is not None:
                raise BotDeletedError("Bot 已删除，不能再切换版本")
            active_contract = _active_game_contract_tx(c, row["game_id"])
            if row["retired_at"] is not None:
                raise ValueError("该版本已退役，不可回滚")
            if str(row["protocol_version"] or "") != active_contract["protocol_version"]:
                raise ValueError("该版本协议与当前游戏不兼容")
            c.execute(
                "UPDATE bots SET current_version=?, binary_path=?, os=?, arch=?, "
                "format=?, runtime_mode=?,protocol_version=?, updated_at=? WHERE id=?",
                (row["version"], row["binary_path"], row["os"], row["arch"],
                 row["format"], row["runtime_mode"], row["protocol_version"], _now(), bot_id),
            )
            return _row(c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone())

    def bot_has_cutover_audit_versions(self, bot_id: int) -> bool:
        with self._tx() as c:
            return bool(_cutover_audit_version_count_tx(c, bot_id=bot_id))

    def get_protocol_cutover(self, cutover_id: str) -> dict[str, Any] | None:
        """Return one immutable cutover marker and its canonical manifest."""

        with self._tx() as c:
            row = c.execute(
                "SELECT * FROM protocol_cutovers WHERE cutover_id=?",
                (str(cutover_id or "").strip(),),
            ).fetchone()
            if row is None:
                return None
            result = dict(row)
            manifest = _loads_json(result.pop("manifest_json", ""), default=None)
            if not isinstance(manifest, list):
                raise RuntimeError("protocol_cutovers manifest_json 已损坏")
            result["version_manifest"] = manifest
            return result

    @staticmethod
    def _normalize_cutover_contract(
        raw: dict[str, str], *, label: str
    ) -> dict[str, str]:
        keys = ("ruleset_version", "protocol_version", "rating_pool_id")
        normalized = {key: str(raw.get(key) or "").strip() for key in keys}
        if any(not value for value in normalized.values()):
            raise ValueError(f"{label} contract 字段不能为空")
        return normalized

    def _rule_cutover_bot_snapshot_tx(
        self,
        c: sqlite3.Connection,
        *,
        game_id: str,
        protocol_version: str,
        verify_assets: bool,
    ) -> dict[str, Any]:
        """Validate current Bot identities without persisting a deletion pin.

        A same-wire rule cutover deliberately keeps every current version in
        place.  Its marker therefore stores an empty manifest; this ephemeral
        snapshot binds dry-run to apply while still allowing ordinary uploads
        and retention after the cutover.
        """

        rows = c.execute(
            "SELECT b.id AS bot_id,b.current_version,b.binary_path AS bot_path,"
            "b.os AS bot_os,b.arch AS bot_arch,b.format AS bot_format,"
            "b.runtime_mode AS bot_runtime,b.protocol_version AS bot_protocol,"
            "v.id AS version_id,v.version AS version_number,"
            "v.binary_path AS version_path,v.checksum,v.size_bytes,"
            "v.os AS version_os,v.arch AS version_arch,v.format AS version_format,"
            "v.runtime_mode AS version_runtime,v.protocol_version AS version_protocol,"
            "v.upload_note,v.retired_at AS version_retired "
            "FROM bots b LEFT JOIN bot_versions v ON v.bot_id=b.id "
            "AND v.version=b.current_version WHERE b.game_id=? ORDER BY b.id",
            (game_id,),
        ).fetchall()
        versioned = [row for row in rows if row["version_id"] is not None]
        root: Path | None = None
        if verify_assets and versioned:
            root = self._validated_cutover_binary_root(
                Path(self.path).expanduser().resolve().parent / "bot_uploads"
            )

        snapshot: list[dict[str, Any]] = []
        seen_paths: set[str] = set()
        seen_inodes: set[tuple[int, int]] = set()
        issues: list[str] = []
        for row in rows:
            bot_id = int(row["bot_id"])
            current_version = int(row["current_version"] or 0)
            if str(row["bot_protocol"] or "") != protocol_version:
                issues.append(f"Bot {bot_id} protocol 非 source")
            if row["version_id"] is None:
                issues.append(f"Bot {bot_id} current version 行缺失")
                continue
            if int(row["version_number"] or 0) != current_version:
                issues.append(f"Bot {bot_id} current version 编号漂移")
            if row["version_retired"] is not None:
                issues.append(f"Bot {bot_id} current version 已退役")
            if str(row["version_protocol"] or "") != protocol_version:
                issues.append(f"Bot {bot_id} current version protocol 非 source")
            mirrors = (
                (row["bot_path"], row["version_path"]),
                (row["bot_os"], row["version_os"]),
                (row["bot_arch"], row["version_arch"]),
                (row["bot_format"], row["version_format"]),
                (row["bot_runtime"], row["version_runtime"]),
            )
            if any(str(left or "") != str(right or "") for left, right in mirrors):
                issues.append(f"Bot {bot_id} current mirror 漂移")

            version_path = Path(str(row["version_path"] or ""))
            digest = ""
            inode: tuple[int, int] | None = None
            asset_stat: dict[str, int] | None = None
            if root is not None:
                expected_path = root / str(bot_id) / f"v{current_version}" / "bot.bin"
                if version_path != expected_path:
                    issues.append(f"Bot {bot_id} current path 非 canonical")
                else:
                    try:
                        version_dir = version_path.parent
                        bot_dir = version_dir.parent
                        version_dir_stat = version_dir.lstat()
                        bot_dir_stat = bot_dir.lstat()
                        before = version_path.lstat()
                        bot_dir_mode = stat.S_IMODE(bot_dir_stat.st_mode)
                        version_dir_mode = stat.S_IMODE(version_dir_stat.st_mode)
                        binary_mode = stat.S_IMODE(before.st_mode)
                        uploader_shape = (
                            version_dir_mode == 0o700
                            and binary_mode == 0o755
                        )
                        hard_cutover_shape = (
                            version_dir_mode == 0o555
                            and binary_mode == 0o555
                        )
                        if (
                            stat.S_ISLNK(version_dir_stat.st_mode)
                            or not stat.S_ISDIR(version_dir_stat.st_mode)
                            or int(version_dir_stat.st_uid) != os.geteuid()
                            or version_dir_mode & 0o022
                            or stat.S_ISLNK(bot_dir_stat.st_mode)
                            or not stat.S_ISDIR(bot_dir_stat.st_mode)
                            or bot_dir_mode & 0o022
                            or int(bot_dir_stat.st_uid) != os.geteuid()
                            or stat.S_ISLNK(before.st_mode)
                            or not stat.S_ISREG(before.st_mode)
                            or binary_mode & 0o022
                            or int(before.st_uid) != os.geteuid()
                            or int(before.st_nlink) != 1
                            or version_path.resolve(strict=True) != version_path
                            or not (uploader_shape or hard_cutover_shape)
                        ):
                            raise OSError("unsafe current asset")
                        hasher = hashlib.sha256()
                        with version_path.open("rb") as stream:
                            for chunk in iter(
                                lambda: stream.read(1024 * 1024), b""
                            ):
                                hasher.update(chunk)
                        after = version_path.lstat()
                        before_fingerprint = (
                            int(before.st_dev), int(before.st_ino), int(before.st_size),
                            int(before.st_mtime_ns), int(before.st_ctime_ns),
                        )
                        after_fingerprint = (
                            int(after.st_dev), int(after.st_ino), int(after.st_size),
                            int(after.st_mtime_ns), int(after.st_ctime_ns),
                        )
                        if before_fingerprint != after_fingerprint:
                            raise OSError("current asset changed while hashing")
                        digest = hasher.hexdigest()
                        inode = (int(after.st_dev), int(after.st_ino))
                        asset_stat = {
                            "dev": int(after.st_dev),
                            "ino": int(after.st_ino),
                            "size": int(after.st_size),
                            "mtime_ns": int(after.st_mtime_ns),
                            "ctime_ns": int(after.st_ctime_ns),
                            "mode": stat.S_IMODE(after.st_mode),
                            "uid": int(after.st_uid),
                            "nlink": int(after.st_nlink),
                        }
                        if int(after.st_size) != int(row["size_bytes"] or -1):
                            issues.append(f"Bot {bot_id} current asset size 漂移")
                        if digest != str(row["checksum"] or ""):
                            issues.append(f"Bot {bot_id} current asset hash 漂移")
                    except OSError:
                        issues.append(f"Bot {bot_id} current asset 缺失或不安全")
            path_text = str(version_path)
            if path_text in seen_paths:
                issues.append("多个 current Bot version 共用路径")
            seen_paths.add(path_text)
            if inode is not None:
                if inode in seen_inodes:
                    issues.append("多个 current Bot version 共用 inode")
                seen_inodes.add(inode)
            snapshot.append(
                {
                    "bot_id": bot_id,
                    "current_version": current_version,
                    "version_id": int(row["version_id"]),
                    "binary_path": path_text,
                    "checksum": str(row["checksum"] or ""),
                    "size_bytes": int(row["size_bytes"] or 0),
                    "os": str(row["version_os"] or ""),
                    "arch": str(row["version_arch"] or ""),
                    "format": str(row["version_format"] or ""),
                    "runtime_mode": str(row["version_runtime"] or ""),
                    "protocol_version": str(row["version_protocol"] or ""),
                    "upload_note": str(row["upload_note"] or ""),
                    "verified_sha256": digest,
                    "asset_stat": asset_stat,
                }
            )

        wrong_active_agents = int(
            c.execute(
                "SELECT COUNT(*) FROM local_ai_agents a JOIN bots b ON b.id=a.bot_id "
                "WHERE a.status='active' AND (a.game_id=? OR b.game_id=?) AND NOT ("
                "a.game_id=? AND b.game_id=? AND a.protocol_version=? "
                "AND b.protocol_version=?)",
                (
                    game_id,
                    game_id,
                    game_id,
                    game_id,
                    protocol_version,
                    protocol_version,
                ),
            ).fetchone()[0]
        )
        if wrong_active_agents:
            issues.append(
                f"{wrong_active_agents} 个 current Local AI agent 身份漂移"
            )
        if issues:
            raise ValueError("rule-only cutover Bot 快照失败: " + "; ".join(issues[:12]))
        return {
            "bot_count": len(rows),
            "current_version_count": len(versioned),
            "snapshot_digest": _canonical_digest(snapshot),
        }

    def _rule_cutover_unstarted_contests_tx(
        self,
        c: sqlite3.Connection,
        *,
        game_id: str,
        source: dict[str, str],
        authorized_ids: tuple[int, ...],
    ) -> list[dict[str, Any]]:
        """Bind explicitly authorized, open and entirely unstarted contests.

        A rule-only cutover may preserve an open roster because pairings freeze
        Bot versions only when the contest is later published/started.  This
        exception is intentionally narrower than a generic "not running"
        check: every persisted execution/result surface must still be empty,
        and the complete contest/roster snapshot is part of the reviewed plan.
        """

        if any(contest_id <= 0 for contest_id in authorized_ids):
            raise ValueError("迁移赛事 ID 必须为正整数")
        if len(set(authorized_ids)) != len(authorized_ids):
            raise ValueError("迁移赛事 ID 不得重复")
        requested_ids = tuple(sorted(authorized_ids))
        rows = c.execute(
            "SELECT * FROM contests WHERE game_id=? AND showcase_key IS NULL "
            "AND status NOT IN (?,?) ORDER BY id",
            (game_id, CONTEST_FINISHED, CONTEST_CANCELLED),
        ).fetchall()
        live_ids = tuple(int(row["id"]) for row in rows)
        if live_ids != requested_ids:
            raise ValueError(
                "仍有未终结的旧规则赛事，且与显式授权迁移 ID 不一致: "
                f"live={list(live_ids)} authorized={list(requested_ids)}"
            )

        migrations: list[dict[str, Any]] = []
        for row in rows:
            contest_id = int(row["id"])
            contract = {
                "ruleset_version": str(row["ruleset_version"] or ""),
                "protocol_version": str(row["protocol_version"] or ""),
                "rating_pool_id": str(row["rating_pool_id"] or ""),
            }
            if contract != source:
                raise ValueError(f"赛事 {contest_id} contract 不是 source")
            if str(row["status"] or "") != CONTEST_OPEN:
                raise ValueError(f"赛事 {contest_id} 不是 open，禁止随规则切换迁移")
            if (
                row["starts_at"] is not None
                or row["ends_at"] is not None
                or row["rest_ends_at"] is not None
                or exact_nonnegative_int(row["current_stage_idx"]) != 0
                or exact_nonnegative_int(row["official_results_ready"]) != 0
            ):
                raise ValueError(f"赛事 {contest_id} 已进入赛程，禁止迁移")
            stages = _loads_json(row["stages_json"], default=None)
            if not isinstance(stages, list) or not stages:
                raise ValueError(f"赛事 {contest_id} 阶段配置无效，禁止迁移")
            current_stage_idx = exact_nonnegative_int(row["current_stage_idx"])
            if current_stage_idx is None or current_stage_idx >= len(stages):
                raise ValueError(f"赛事 {contest_id} 当前阶段越界，禁止迁移")

            entry_rows = c.execute(
                "SELECT * FROM contest_entries WHERE contest_id=? ORDER BY id",
                (contest_id,),
            ).fetchall()
            if any(entry["dispatched_at"] is not None for entry in entry_rows):
                raise ValueError(f"赛事 {contest_id} 已派发报名，禁止迁移")
            graph_counts = {
                "pairings": int(c.execute(
                    "SELECT COUNT(*) FROM contest_pairings WHERE contest_id=?",
                    (contest_id,),
                ).fetchone()[0]),
                "stage_results": int(c.execute(
                    "SELECT COUNT(*) FROM contest_stage_results WHERE contest_id=?",
                    (contest_id,),
                ).fetchone()[0]),
                "official_results": int(c.execute(
                    "SELECT COUNT(*) FROM contest_official_results WHERE contest_id=?",
                    (contest_id,),
                ).fetchone()[0]),
                "execution_jobs": int(c.execute(
                    "SELECT COUNT(*) FROM execution_jobs WHERE contest_id=?",
                    (contest_id,),
                ).fetchone()[0]),
                "matches": sum(
                    int(c.execute(
                        f"SELECT COUNT(*) FROM {_matches_table(other_game)} "
                        "WHERE contest_id=?",
                        (contest_id,),
                    ).fetchone()[0])
                    for other_game in _all_game_ids()
                ),
            }
            if any(graph_counts.values()):
                raise ValueError(
                    f"赛事 {contest_id} 已生成赛程/对局，禁止迁移: {graph_counts}"
                )
            migrations.append(
                {
                    "contest_id": contest_id,
                    "status": CONTEST_OPEN,
                    "entry_count": len(entry_rows),
                    "contest_snapshot_digest": _canonical_digest(dict(row)),
                    "entry_snapshot_digest": _canonical_digest(
                        [dict(entry) for entry in entry_rows]
                    ),
                }
            )
        return migrations

    def _game_rule_cutover_plan_tx(
        self,
        c: sqlite3.Connection,
        *,
        cutover_id: str,
        game_id: str,
        source: dict[str, str],
        target: dict[str, str],
        migrate_unstarted_contest_ids: tuple[int, ...] = (),
    ) -> dict[str, Any]:
        if target != game_rule_contract(game_id):
            raise ValueError("目标 contract 与代码声明的 current contract 不一致")
        active = _active_game_contract_tx(c, game_id)
        if active != source:
            raise ValueError(
                f"active contract 已变化，期望 {source!r}，实际 {active!r}"
            )
        projection = self._rating_projection_status_tx(c)
        if not projection["ready"]:
            raise ValueError("评分投影未通过校验，禁止归档并切换评分池")
        table = _matches_table(game_id)
        unsettled = int(
            c.execute(
                f"SELECT COUNT(*) FROM {table} m "
                "LEFT JOIN match_rating_settlements s ON s.match_id=m.id "
                "WHERE m.status=? AND m.match_type NOT IN (?,?) "
                "AND s.match_id IS NULL",
                (STATUS_COMPLETED, TYPE_CONTEST, TYPE_HUMAN),
            ).fetchone()[0]
        )
        if unsettled:
            raise ValueError("仍有未结算的旧规则 Match，禁止切换")
        contest_migrations = self._rule_cutover_unstarted_contests_tx(
            c,
            game_id=game_id,
            source=source,
            authorized_ids=migrate_unstarted_contest_ids,
        )
        chain = self._protocol_cutover_chain_tx(c, game_id)
        if chain:
            for index, marker in enumerate(chain):
                self._assert_protocol_cutover_postconditions_tx(
                    c,
                    marker,
                    verify_assets=True,
                    enforce_live_generation=index == len(chain) - 1,
                )
            tail = chain[-1]
            previous_target = {
                "ruleset_version": str(tail["to_ruleset"]),
                "protocol_version": str(tail["to_protocol"]),
                "rating_pool_id": str(tail["to_rating_pool"]),
            }
            if previous_target != source:
                raise ValueError("cutover marker chain 断链；from contract 不是链尾")

        wrong_runnable = int(
            c.execute(
                "SELECT COUNT(*) FROM execution_jobs WHERE game_id=? AND "
                "status IN ('queued','starting','running','settling') AND "
                "(ruleset_version<>? OR protocol_version<>? OR rating_pool_id<>?)",
                (
                    game_id,
                    source["ruleset_version"],
                    source["protocol_version"],
                    source["rating_pool_id"],
                ),
            ).fetchone()[0]
        )
        wrong_retryable = int(
            c.execute(
                "SELECT COUNT(*) FROM execution_jobs WHERE game_id=? "
                "AND status='interrupted' AND retryable<>0 AND "
                "(ruleset_version<>? OR protocol_version<>? OR rating_pool_id<>?)",
                (
                    game_id,
                    source["ruleset_version"],
                    source["protocol_version"],
                    source["rating_pool_id"],
                ),
            ).fetchone()[0]
        )
        if wrong_runnable or wrong_retryable:
            raise ValueError("可运行/可重试执行任务 contract 漂移")
        snapshot = self._rule_cutover_bot_snapshot_tx(
            c,
            game_id=game_id,
            protocol_version=source["protocol_version"],
            verify_assets=True,
        )
        queued_ids = [
            int(row["id"])
            for row in c.execute(
                "SELECT id FROM execution_jobs WHERE game_id=? AND status='queued' "
                "AND ruleset_version=? AND protocol_version=? AND rating_pool_id=? "
                "ORDER BY id",
                (
                    game_id,
                    source["ruleset_version"],
                    source["protocol_version"],
                    source["rating_pool_id"],
                ),
            ).fetchall()
        ]
        retryable_ids = [
            int(row["id"])
            for row in c.execute(
                "SELECT id FROM execution_jobs WHERE game_id=? "
                "AND status='interrupted' AND retryable<>0 AND ruleset_version=? "
                "AND protocol_version=? AND rating_pool_id=? ORDER BY id",
                (
                    game_id,
                    source["ruleset_version"],
                    source["protocol_version"],
                    source["rating_pool_id"],
                ),
            ).fetchall()
        ]
        plan_basis = {
            "kind": "same-protocol-rule-only-v1",
            "cutover_id": cutover_id,
            "game_id": game_id,
            "from_contract": source,
            "to_contract": target,
            "bot_snapshot_digest": snapshot["snapshot_digest"],
            "bot_count": snapshot["bot_count"],
            "current_version_count": snapshot["current_version_count"],
            "rating_source_digest": projection["source_digest"],
            "rating_projection_digest": projection["projection_digest"],
            "rating_plan_digest": projection["plan_digest"],
            "queued_job_ids": queued_ids,
            "retryable_interrupted_job_ids": retryable_ids,
            "contest_contract_migrations": contest_migrations,
        }
        return {
            **plan_basis,
            "plan_digest": _canonical_digest(plan_basis),
            "manifest_digest": _canonical_digest([]),
            "version_manifest": [],
            "cancelled_job_count": len(queued_ids) + len(retryable_ids),
        }

    def plan_game_rule_cutover(
        self,
        *,
        cutover_id: str,
        game_id: str,
        from_contract: dict[str, str],
        to_contract: dict[str, str],
        migrate_unstarted_contest_ids: tuple[int, ...] | list[int] = (),
    ) -> dict[str, Any]:
        """Plan a same-wire ruleset/rating-pool cutover without DB writes."""

        gid = _registered_game_id(game_id)
        clean_cutover_id = str(cutover_id or "").strip()
        if not clean_cutover_id:
            raise ValueError("cutover_id 不能为空")
        source = self._normalize_cutover_contract(from_contract, label="from")
        target = self._normalize_cutover_contract(to_contract, label="to")
        if source["protocol_version"] != target["protocol_version"]:
            raise ValueError("game-rule-cutover 要求 from/to protocol 完全相同")
        if source["ruleset_version"] == target["ruleset_version"]:
            raise ValueError("game-rule-cutover 必须更换 ruleset")
        if source["rating_pool_id"] == target["rating_pool_id"]:
            raise ValueError("game-rule-cutover 必须更换 rating pool")
        with self._tx() as c:
            existing = c.execute(
                "SELECT * FROM protocol_cutovers WHERE cutover_id=?",
                (clean_cutover_id,),
            ).fetchone()
            if existing is not None:
                expected = (
                    gid,
                    source["ruleset_version"],
                    target["ruleset_version"],
                    source["protocol_version"],
                    target["protocol_version"],
                    source["rating_pool_id"],
                    target["rating_pool_id"],
                )
                actual = tuple(
                    str(existing[key])
                    for key in (
                        "game_id", "from_ruleset", "to_ruleset", "from_protocol",
                        "to_protocol", "from_rating_pool", "to_rating_pool",
                    )
                )
                if actual != expected:
                    raise ValueError("cutover_id 已被不同切换占用")
                chain = self._protocol_cutover_chain_tx(c, gid)
                marker_index = next(
                    index
                    for index, marker in enumerate(chain)
                    if str(marker["cutover_id"]) == clean_cutover_id
                )
                self._assert_protocol_cutover_postconditions_tx(
                    c,
                    existing,
                    verify_assets=True,
                    enforce_live_generation=marker_index == len(chain) - 1,
                )
                return {
                    "cutover_id": clean_cutover_id,
                    "game_id": gid,
                    "from_contract": source,
                    "to_contract": target,
                    "already_applied": True,
                    "manifest_digest": str(existing["manifest_digest"]),
                    "version_manifest": [],
                }
            return {
                "cutover_id": clean_cutover_id,
                "game_id": gid,
                "from_contract": source,
                "to_contract": target,
                "already_applied": False,
                **self._game_rule_cutover_plan_tx(
                    c,
                    cutover_id=clean_cutover_id,
                    game_id=gid,
                    source=source,
                    target=target,
                    migrate_unstarted_contest_ids=tuple(
                        migrate_unstarted_contest_ids
                    ),
                ),
            }

    def apply_game_rule_cutover(
        self,
        *,
        cutover_id: str,
        game_id: str,
        from_contract: dict[str, str],
        to_contract: dict[str, str],
        expected_plan_digest: str,
        offline_guard: _OfflineCutoverGuard,
        migrate_unstarted_contest_ids: tuple[int, ...] | list[int] = (),
    ) -> dict[str, Any]:
        """Atomically advance ruleset/pool while retaining same-wire Bot assets."""

        resolved_database = str(Path(self.path).expanduser().resolve())
        if (
            not isinstance(offline_guard, _OfflineCutoverGuard)
            or not offline_guard.active
            or offline_guard.store_identity != id(self)
            or offline_guard.database_path != resolved_database
            or offline_guard.thread_id != threading.get_ident()
        ):
            raise RuntimeError("规则 rule-only cutover 缺少当前 Store 的停服独占 guard")
        gid = _registered_game_id(game_id)
        clean_cutover_id = str(cutover_id or "").strip()
        if not clean_cutover_id:
            raise ValueError("cutover_id 不能为空")
        source = self._normalize_cutover_contract(from_contract, label="from")
        target = self._normalize_cutover_contract(to_contract, label="to")
        if source["protocol_version"] != target["protocol_version"]:
            raise ValueError("game-rule-cutover 要求 from/to protocol 完全相同")
        if source["ruleset_version"] == target["ruleset_version"]:
            raise ValueError("game-rule-cutover 必须更换 ruleset")
        if source["rating_pool_id"] == target["rating_pool_id"]:
            raise ValueError("game-rule-cutover 必须更换 rating pool")
        reviewed_plan_digest = str(expected_plan_digest or "").strip().lower()
        if len(reviewed_plan_digest) != 64 or any(
            ch not in "0123456789abcdef" for ch in reviewed_plan_digest
        ):
            raise ValueError("expected_plan_digest 必须是小写 SHA-256")

        empty_manifest: list[dict[str, Any]] = []
        manifest_json = "[]"
        manifest_digest = _canonical_digest(empty_manifest)
        marker_fields = {
            "game_id": gid,
            "from_ruleset": source["ruleset_version"],
            "to_ruleset": target["ruleset_version"],
            "from_protocol": source["protocol_version"],
            "to_protocol": target["protocol_version"],
            "from_rating_pool": source["rating_pool_id"],
            "to_rating_pool": target["rating_pool_id"],
            "manifest_digest": manifest_digest,
            "manifest_json": manifest_json,
        }

        def result(row: sqlite3.Row, *, already_applied: bool) -> dict[str, Any]:
            value = dict(row)
            value["already_applied"] = already_applied
            return value

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            control = c.execute(
                "SELECT dispatcher_state,accepting,auto_enabled,"
                "deployment_drain_requested FROM execution_control WHERE singleton=1"
            ).fetchone()
            if control is None or (
                int(control["deployment_drain_requested"] or 0) != 1
                or int(control["accepting"] or 0) != 0
                or int(control["auto_enabled"] or 0) != 0
                or str(control["dispatcher_state"] or "") != "stopped"
            ):
                raise ValueError(
                    "rule-only cutover 要求已请求部署维护、停止接单/自动排位且 dispatcher 已停服"
                )
            active_jobs = int(
                c.execute(
                    "SELECT COUNT(*) FROM execution_jobs WHERE status IN "
                    "('starting','running','settling')"
                ).fetchone()[0]
            )
            active_attempts = int(
                c.execute(
                    "SELECT COUNT(*) FROM execution_job_attempts WHERE status IN "
                    "('starting','running','settling')"
                ).fetchone()[0]
            )
            active_matches = sum(
                int(
                    c.execute(
                        f"SELECT COUNT(*) FROM {_matches_table(other_game)} "
                        "WHERE status IN (?,?)",
                        (STATUS_PENDING, STATUS_RUNNING),
                    ).fetchone()[0]
                )
                for other_game in _all_game_ids()
            )
            active_leases = int(
                c.execute(
                    "SELECT COUNT(*) FROM local_ai_leases WHERE status='active'"
                ).fetchone()[0]
            )
            launch = c.execute(
                "SELECT state FROM docker_launch_journal WHERE singleton=1"
            ).fetchone()
            if launch is None or str(launch["state"] or "") != "idle":
                raise ValueError("Docker launch journal 未静默，禁止 rule-only cutover")
            if active_jobs or active_attempts or active_matches or active_leases:
                raise ValueError(
                    "rule-only cutover 排空失败: "
                    f"jobs={active_jobs} attempts={active_attempts} "
                    f"matches={active_matches} local_leases={active_leases}"
                )

            existing = c.execute(
                "SELECT * FROM protocol_cutovers WHERE cutover_id=?",
                (clean_cutover_id,),
            ).fetchone()
            if existing is not None:
                mismatched = [
                    key for key, value in marker_fields.items()
                    if str(existing[key]) != str(value)
                ]
                if mismatched:
                    raise ValueError(
                        "cutover_id 已被不同切换占用: " + ",".join(mismatched)
                    )
                chain = self._protocol_cutover_chain_tx(c, gid)
                marker_index = next(
                    index
                    for index, marker in enumerate(chain)
                    if str(marker["cutover_id"]) == clean_cutover_id
                )
                self._assert_protocol_cutover_postconditions_tx(
                    c,
                    existing,
                    verify_assets=True,
                    enforce_live_generation=marker_index == len(chain) - 1,
                )
                return result(existing, already_applied=True)

            plan = self._game_rule_cutover_plan_tx(
                c,
                cutover_id=clean_cutover_id,
                game_id=gid,
                source=source,
                target=target,
                migrate_unstarted_contest_ids=tuple(
                    migrate_unstarted_contest_ids
                ),
            )
            if plan["plan_digest"] != reviewed_plan_digest:
                raise ValueError(
                    "expected_plan_digest 与当前冷库计划不一致；请重新审核 dry-run"
                )

            archived_at = _now()
            rating_rows = [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM ratings WHERE game_id=? ORDER BY bot_id", (gid,)
                ).fetchall()
            ]
            history_rows = [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM rating_history WHERE game_id=? ORDER BY id", (gid,)
                ).fetchall()
            ]
            pair_rows = [
                dict(row)
                for row in c.execute(
                    "SELECT p.* FROM pair_stats p JOIN bots a ON a.id=p.bot_a_id "
                    "JOIN bots b ON b.id=p.bot_b_id WHERE a.game_id=? AND b.game_id=? "
                    "ORDER BY p.bot_a_id,p.bot_b_id",
                    (gid, gid),
                ).fetchall()
            ]
            archive_digest = rating_projection_digest(
                rating_rows, history_rows, pair_rows
            )
            c.execute(
                "INSERT INTO ratings_archive(bot_id,game_id,pool_id,rating,rd,vol,"
                "wins,losses,draws,delta_total,matches_played,last_played_at,archived_at) "
                "SELECT bot_id,game_id,?,rating,rd,vol,wins,losses,draws,delta_total,"
                "matches_played,last_played_at,? FROM ratings WHERE game_id=?",
                (source["rating_pool_id"], archived_at, gid),
            )
            c.execute(
                "INSERT INTO rating_history_archive(original_id,bot_id,game_id,pool_id,"
                "rating,rd,vol,matches_played,reason,created_at,archived_at) "
                "SELECT id,bot_id,game_id,?,rating,rd,vol,matches_played,reason,"
                "created_at,? FROM rating_history WHERE game_id=?",
                (source["rating_pool_id"], archived_at, gid),
            )
            c.execute(
                "INSERT INTO pair_stats_archive(bot_a_id,bot_b_id,game_id,pool_id,"
                "samples,last_played_at,a_wins,a_losses,draws,archived_at) "
                "SELECT p.bot_a_id,p.bot_b_id,?,?,p.samples,p.last_played_at,"
                "p.a_wins,p.a_losses,p.draws,? FROM pair_stats p "
                "JOIN bots a ON a.id=p.bot_a_id JOIN bots b ON b.id=p.bot_b_id "
                "WHERE a.game_id=? AND b.game_id=?",
                (gid, source["rating_pool_id"], archived_at, gid, gid),
            )
            c.execute(
                "INSERT INTO rating_pool_archives(game_id,pool_id,ruleset_version,"
                "protocol_version,archived_at,ratings_count,history_count,pair_count,"
                "projection_digest) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    gid,
                    source["rating_pool_id"],
                    source["ruleset_version"],
                    source["protocol_version"],
                    archived_at,
                    len(rating_rows),
                    len(history_rows),
                    len(pair_rows),
                    archive_digest,
                ),
            )

            queued = int(
                c.execute(
                    "UPDATE execution_jobs SET status='cancelled',retryable=0,"
                    "terminal_reason='ruleset_retired',last_error='ruleset_retired',"
                    "terminal_at=? WHERE game_id=? AND status='queued' "
                    "AND ruleset_version=? AND protocol_version=? AND rating_pool_id=?",
                    (
                        archived_at,
                        gid,
                        source["ruleset_version"],
                        source["protocol_version"],
                        source["rating_pool_id"],
                    ),
                ).rowcount
            )
            interrupted = int(
                c.execute(
                    "UPDATE execution_jobs SET retryable=0,"
                    "terminal_reason='ruleset_retired',last_error='ruleset_retired',"
                    "terminal_at=COALESCE(terminal_at,?) WHERE game_id=? "
                    "AND status='interrupted' AND retryable<>0 AND ruleset_version=? "
                    "AND protocol_version=? AND rating_pool_id=?",
                    (
                        archived_at,
                        gid,
                        source["ruleset_version"],
                        source["protocol_version"],
                        source["rating_pool_id"],
                    ),
                ).rowcount
            )
            c.execute(
                "UPDATE auto_match_decisions SET lifecycle='cancelled',"
                "terminal_reason='ruleset_retired',terminal_at=? "
                "WHERE game_id=? AND lifecycle='queued'",
                (archived_at, gid),
            )

            c.execute(
                "UPDATE ratings SET rating=1500.0,rd=350.0,vol=0.06,wins=0,losses=0,"
                "draws=0,delta_total=0,matches_played=0,last_played_at=NULL "
                "WHERE game_id=?",
                (gid,),
            )
            c.execute("DELETE FROM rating_history WHERE game_id=?", (gid,))
            c.execute(
                "DELETE FROM pair_stats WHERE bot_a_id IN "
                "(SELECT id FROM bots WHERE game_id=?) AND bot_b_id IN "
                "(SELECT id FROM bots WHERE game_id=?)",
                (gid, gid),
            )
            c.execute(
                "INSERT OR IGNORE INTO ratings(bot_id,game_id) "
                "SELECT id,game_id FROM bots WHERE game_id=?",
                (gid,),
            )
            for service_table in (
                "auto_match_owner_service",
                "auto_match_bot_service",
                "auto_match_bot_pair_service",
                "auto_match_owner_pair_service",
            ):
                c.execute(f"DELETE FROM {service_table} WHERE game_id=?", (gid,))
            for migration in plan["contest_contract_migrations"]:
                changed_contest = c.execute(
                    "UPDATE contests SET ruleset_version=?,protocol_version=?,"
                    "rating_pool_id=? WHERE id=? AND game_id=? AND showcase_key IS NULL "
                    "AND status=? AND ruleset_version=? AND protocol_version=? "
                    "AND rating_pool_id=?",
                    (
                        target["ruleset_version"],
                        target["protocol_version"],
                        target["rating_pool_id"],
                        int(migration["contest_id"]),
                        gid,
                        CONTEST_OPEN,
                        source["ruleset_version"],
                        source["protocol_version"],
                        source["rating_pool_id"],
                    ),
                )
                if changed_contest.rowcount != 1:
                    raise RuntimeError("未开赛赛事 contract compare-and-swap 失败")
            changed = c.execute(
                "UPDATE rating_pool_state SET active_pool_id=?,ruleset_version=?,"
                "protocol_version=?,activated_at=? WHERE game_id=? AND "
                "active_pool_id=? AND ruleset_version=? AND protocol_version=?",
                (
                    target["rating_pool_id"],
                    target["ruleset_version"],
                    target["protocol_version"],
                    archived_at,
                    gid,
                    source["rating_pool_id"],
                    source["ruleset_version"],
                    source["protocol_version"],
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("active contract compare-and-swap 失败")

            live = rating_projection_digests(c)
            if live["issues"]:
                raise RuntimeError(
                    "切换后评分源校验失败: " + "; ".join(live["issues"][:5])
                )
            c.execute(
                "UPDATE rating_projection_state SET policy_version=?,rebuilt_at=?,"
                "source_settlement_count=?,source_last_settled_order=?,source_digest=?,"
                "projection_digest=?,plan_digest=?,trusted_mutation_revision="
                "mutation_revision WHERE singleton=1",
                (
                    _RATING_PROJECTION_POLICY_VERSION,
                    archived_at,
                    int(live["source_settlement_count"]),
                    int(live["source_last_settled_order"]),
                    live["source_digest"],
                    live["projection_digest"],
                    live["plan_digest"],
                ),
            )
            c.execute(
                "INSERT INTO protocol_cutovers(cutover_id,game_id,from_ruleset,"
                "to_ruleset,from_protocol,to_protocol,from_rating_pool,to_rating_pool,"
                "manifest_digest,manifest_json,bot_count,retired_count,cancelled_jobs,"
                "archive_digest,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    clean_cutover_id,
                    gid,
                    source["ruleset_version"],
                    target["ruleset_version"],
                    source["protocol_version"],
                    target["protocol_version"],
                    source["rating_pool_id"],
                    target["rating_pool_id"],
                    manifest_digest,
                    manifest_json,
                    0,
                    0,
                    queued + interrupted,
                    archive_digest,
                    archived_at,
                ),
            )
            marker = c.execute(
                "SELECT * FROM protocol_cutovers WHERE cutover_id=?",
                (clean_cutover_id,),
            ).fetchone()
            if marker is None:  # pragma: no cover - transaction invariant
                raise RuntimeError("cutover marker 写入失败")
            self._assert_protocol_cutover_postconditions_tx(
                c, marker, verify_assets=True, enforce_live_generation=True
            )
            chain = self._protocol_cutover_chain_tx(c, gid)
            if not chain or str(chain[-1]["cutover_id"]) != clean_cutover_id:
                raise RuntimeError("cutover marker 未成为唯一链尾")
            return result(marker, already_applied=False)

    def cutover_game_contract(
        self,
        *,
        cutover_id: str,
        game_id: str,
        from_contract: dict[str, str],
        to_contract: dict[str, str],
        version_manifest: list[dict[str, Any]],
        canonical_binary_root: str | os.PathLike[str],
        offline_guard: _OfflineCutoverGuard,
        retirement_reason: str = "ruleset_retired",
    ) -> dict[str, Any]:
        """Atomically replace every Bot binary while advancing one game contract.

        The caller owns asset construction and preflight.  This boundary accepts
        only per-Bot canonical immutable paths plus SHA-256/size metadata,
        verifies their actual ELF headers, then performs the complete metadata,
        rating and queue switch in one SQLite transaction.  The caller must hold
        :meth:`offline_cutover_guard` across staging and this call.
        """

        resolved_database = str(Path(self.path).expanduser().resolve())
        if (
            not isinstance(offline_guard, _OfflineCutoverGuard)
            or not offline_guard.active
            or offline_guard.store_identity != id(self)
            or offline_guard.database_path != resolved_database
            or offline_guard.thread_id != threading.get_ident()
        ):
            raise RuntimeError(
                "规则 hard cutover 缺少当前 Store 的停服独占 guard"
            )
        binary_root = self._validated_cutover_binary_root(canonical_binary_root)

        gid = _registered_game_id(game_id)
        clean_cutover_id = str(cutover_id or "").strip()
        if not clean_cutover_id:
            raise ValueError("cutover_id 不能为空")

        contract_keys = ("ruleset_version", "protocol_version", "rating_pool_id")

        def normalize_contract(raw: dict[str, str], *, label: str) -> dict[str, str]:
            normalized = {
                key: str(raw.get(key) or "").strip() for key in contract_keys
            }
            if any(not value for value in normalized.values()):
                raise ValueError(f"{label} contract 字段不能为空")
            return normalized

        source = normalize_contract(from_contract, label="from")
        target = normalize_contract(to_contract, label="to")
        if source == target:
            raise ValueError("规则切换前后 contract 不能相同")
        if source["protocol_version"] == target["protocol_version"]:
            raise ValueError(
                "game-contract-cutover 仅用于不兼容协议代际，protocol 必须变更"
            )
        if source["rating_pool_id"] == target["rating_pool_id"]:
            raise ValueError("game-contract-cutover 必须更换 rating pool")
        normalized_manifest: list[dict[str, Any]] = []
        for raw in version_manifest:
            bot_id = int(raw["bot_id"])
            version = int(raw["version"])
            candidate = Path(
                os.path.abspath(
                    str(Path(str(raw.get("binary_path") or "")).expanduser())
                )
            )
            try:
                resolved_candidate = candidate.resolve(strict=True)
            except (FileNotFoundError, OSError) as exc:
                raise ValueError(f"cutover 资产不可读: {candidate}") from exc
            expected_path = binary_root / str(bot_id) / f"v{version}" / "bot.bin"
            if candidate != expected_path or resolved_candidate != candidate:
                raise ValueError(
                    "manifest binary_path 必须是逐 Bot 唯一 canonical "
                    "bot_uploads/<bot_id>/vN/bot.bin"
                )
            binary_path = str(candidate)
            entry = {
                "bot_id": bot_id,
                "version": version,
                "expected_current_version": int(raw["expected_current_version"]),
                "expected_current_version_id": (
                    None
                    if raw.get("expected_current_version_id") is None
                    else int(raw["expected_current_version_id"])
                ),
                "expected_current_checksum": str(
                    raw.get("expected_current_checksum") or ""
                ).lower(),
                "expected_current_binary_path": str(
                    raw.get("expected_current_binary_path") or ""
                ),
                "expected_current_runtime_mode": str(
                    raw.get("expected_current_runtime_mode") or DEFAULT_RUNTIME_MODE
                ),
                "binary_path": binary_path,
                "checksum": str(raw.get("checksum") or "").lower(),
                "size_bytes": int(raw["size_bytes"]),
                "os": str(raw.get("os") or SUPPORTED_BINARY_OS),
                "arch": str(raw.get("arch") or SUPPORTED_BINARY_ARCH),
                "format": str(raw.get("format") or SUPPORTED_BINARY_FORMAT),
                "runtime_mode": str(raw.get("runtime_mode") or DEFAULT_RUNTIME_MODE),
                "upload_note": str(raw.get("upload_note") or "ruleset cutover"),
            }
            if entry["bot_id"] <= 0 or entry["version"] <= 0:
                raise ValueError("manifest bot_id/version 必须为正整数")
            if entry["expected_current_version"] < 0:
                raise ValueError("manifest expected_current_version 非法")
            if entry["size_bytes"] <= 0:
                raise ValueError("manifest size_bytes 必须为正整数")
            if (
                len(entry["checksum"]) != 64
                or any(ch not in "0123456789abcdef" for ch in entry["checksum"])
            ):
                raise ValueError("manifest checksum 必须是小写 SHA-256")
            require_supported_binary_metadata(
                entry["format"], entry["os"], entry["arch"]
            )
            if entry["runtime_mode"] not in VALID_RUNTIME_MODES:
                raise ValueError("manifest runtime_mode 非法")
            if entry["expected_current_runtime_mode"] not in VALID_RUNTIME_MODES:
                raise ValueError("manifest expected_current_runtime_mode 非法")
            if entry["runtime_mode"] != entry["expected_current_runtime_mode"]:
                raise ValueError("hard cutover 必须保留每个 Bot 当前 runtime_mode")
            normalized_manifest.append(entry)
        normalized_manifest.sort(key=lambda item: item["bot_id"])
        bot_ids = [entry["bot_id"] for entry in normalized_manifest]
        if len(bot_ids) != len(set(bot_ids)):
            raise ValueError("manifest 含重复 bot_id")
        binary_paths = [entry["binary_path"] for entry in normalized_manifest]
        if len(binary_paths) != len(set(binary_paths)):
            raise ValueError("manifest 的每个 Bot 必须使用唯一版本路径")
        manifest_digest = _canonical_digest(normalized_manifest)
        manifest_json = json.dumps(
            normalized_manifest, ensure_ascii=False, sort_keys=True,
            separators=(",", ":"),
        )

        marker_fields = {
            "game_id": gid,
            "from_ruleset": source["ruleset_version"],
            "to_ruleset": target["ruleset_version"],
            "from_protocol": source["protocol_version"],
            "to_protocol": target["protocol_version"],
            "from_rating_pool": source["rating_pool_id"],
            "to_rating_pool": target["rating_pool_id"],
            "manifest_digest": manifest_digest,
            "manifest_json": manifest_json,
        }

        def marker_result(row: sqlite3.Row, *, already_applied: bool) -> dict[str, Any]:
            result = dict(row)
            result["already_applied"] = already_applied
            return result

        def assert_marker(row: sqlite3.Row) -> None:
            mismatched = [
                key for key, value in marker_fields.items()
                if str(row[key]) != str(value)
            ]
            if mismatched:
                raise ValueError(
                    "cutover_id 已被不同切换占用: " + ",".join(mismatched)
                )

        # Hash assets before taking BEGIN IMMEDIATE; only an inode/stat snapshot
        # is rechecked under the write lock.  Runner integrity still re-hashes at
        # claim, so an asset changed after cutover fails closed before execution.
        from bzplat.backend.bots.classify import (
            classify_binary,
            require_supported_binary,
        )

        asset_stats: dict[int, tuple[int, int, int, int, int]] = {}
        asset_inodes: set[tuple[int, int]] = set()
        for entry in normalized_manifest:
            path = entry["binary_path"]
            try:
                before = os.stat(path)
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError(f"cutover 资产不是普通文件: {path}")
                digest = hashlib.sha256()
                with open(path, "rb") as binary:
                    header = binary.read(4096)
                    require_supported_binary(classify_binary(header))
                    digest.update(header)
                    for chunk in iter(lambda: binary.read(1024 * 1024), b""):
                        digest.update(chunk)
                after = os.stat(path)
            except ValueError:
                raise
            except OSError as exc:
                raise ValueError(f"cutover 资产不可读: {path}") from exc
            fingerprint = (
                int(after.st_dev),
                int(after.st_ino),
                int(after.st_size),
                int(after.st_mtime_ns),
                int(after.st_ctime_ns),
            )
            if (
                (before.st_dev, before.st_ino, before.st_size,
                 before.st_mtime_ns, before.st_ctime_ns)
                != fingerprint
            ):
                raise ValueError(f"cutover 资产校验期间发生变化: {path}")
            if int(after.st_size) != entry["size_bytes"]:
                raise ValueError(f"cutover 资产大小不匹配: {path}")
            if digest.hexdigest() != entry["checksum"]:
                raise ValueError(f"cutover 资产 SHA-256 不匹配: {path}")
            inode = (int(after.st_dev), int(after.st_ino))
            if inode in asset_inodes:
                raise ValueError("每个 Bot 的 cutover 资产必须是独立文件，禁止硬链接共享")
            asset_inodes.add(inode)
            asset_stats[entry["bot_id"]] = fingerprint

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            control = c.execute(
                "SELECT dispatcher_state,accepting,auto_enabled,"
                "deployment_drain_requested FROM execution_control WHERE singleton=1"
            ).fetchone()
            if control is None:
                raise RuntimeError("execution_control singleton missing")
            if (
                int(control["deployment_drain_requested"] or 0) != 1
                or int(control["accepting"] or 0) != 0
                or int(control["auto_enabled"] or 0) != 0
                or str(control["dispatcher_state"] or "") != "stopped"
            ):
                raise ValueError(
                    "hard cutover 要求已请求部署维护、停止接单/自动排位且 dispatcher 已停服"
                )
            active_jobs = int(
                c.execute(
                    "SELECT COUNT(*) FROM execution_jobs WHERE status IN "
                    "('starting','running','settling')"
                ).fetchone()[0]
            )
            active_attempts = int(
                c.execute(
                    "SELECT COUNT(*) FROM execution_job_attempts WHERE status IN "
                    "('starting','running','settling')"
                ).fetchone()[0]
            )
            active_matches = sum(
                int(
                    c.execute(
                        f"SELECT COUNT(*) FROM {_matches_table(other_game)} "
                        "WHERE status IN (?,?)",
                        (STATUS_PENDING, STATUS_RUNNING),
                    ).fetchone()[0]
                )
                for other_game in _all_game_ids()
            )
            active_leases = int(
                c.execute(
                    "SELECT COUNT(*) FROM local_ai_leases WHERE status='active'"
                ).fetchone()[0]
            )
            launch = c.execute(
                "SELECT state FROM docker_launch_journal WHERE singleton=1"
            ).fetchone()
            if launch is None or str(launch["state"] or "") != "idle":
                raise ValueError("Docker launch journal 未静默，禁止 hard cutover")
            if active_jobs or active_attempts or active_matches or active_leases:
                raise ValueError(
                    "hard cutover 排空失败: "
                    f"jobs={active_jobs} attempts={active_attempts} "
                    f"matches={active_matches} local_leases={active_leases}"
                )
            existing = c.execute(
                "SELECT * FROM protocol_cutovers WHERE cutover_id=?",
                (clean_cutover_id,),
            ).fetchone()
            if existing is not None:
                assert_marker(existing)
                chain = self._protocol_cutover_chain_tx(c, gid)
                marker_index = next(
                    (
                        index
                        for index, row in enumerate(chain)
                        if str(row["cutover_id"]) == clean_cutover_id
                    ),
                    -1,
                )
                if marker_index < 0:  # pragma: no cover - same transaction invariant
                    raise RuntimeError("cutover marker chain 缺失当前 marker")
                self._assert_protocol_cutover_postconditions_tx(
                    c,
                    existing,
                    verify_assets=True,
                    enforce_live_generation=marker_index == len(chain) - 1,
                )
                return marker_result(existing, already_applied=True)

            if target != game_rule_contract(gid):
                raise ValueError("目标 contract 与代码声明的 current contract 不一致")
            active = _active_game_contract_tx(c, gid)
            if active != source:
                raise ValueError(
                    f"active contract 已变化，期望 {source!r}，实际 {active!r}"
                )
            if not self._rating_projection_status_tx(c)["ready"]:
                raise ValueError("评分投影未通过校验，禁止归档并切换评分池")
            table = _matches_table(gid)
            unsettled = int(
                c.execute(
                    f"SELECT COUNT(*) FROM {table} m "
                    "LEFT JOIN match_rating_settlements s ON s.match_id=m.id "
                    "WHERE m.status=? AND m.match_type NOT IN (?,?) "
                    "AND s.match_id IS NULL",
                    (STATUS_COMPLETED, TYPE_CONTEST, TYPE_HUMAN),
                ).fetchone()[0]
            )
            if unsettled:
                raise ValueError("仍有未结算的旧规则 Match，禁止切换")
            live_contests = int(
                c.execute(
                    "SELECT COUNT(*) FROM contests WHERE game_id=? "
                    "AND showcase_key IS NULL AND status NOT IN (?,?)",
                    (gid, CONTEST_FINISHED, CONTEST_CANCELLED),
                ).fetchone()[0]
            )
            if live_contests:
                raise ValueError("仍有未终结的旧规则赛事，禁止切换")

            database_bots = [
                dict(row)
                for row in c.execute(
                    "SELECT * FROM bots WHERE game_id=? ORDER BY id", (gid,)
                ).fetchall()
            ]
            if bot_ids != [int(row["id"]) for row in database_bots]:
                raise ValueError("manifest 必须且只能覆盖该游戏的全部 Bot")
            for entry in normalized_manifest:
                current_stat = os.stat(entry["binary_path"])
                current_fingerprint = (
                    int(current_stat.st_dev), int(current_stat.st_ino),
                    int(current_stat.st_size), int(current_stat.st_mtime_ns),
                    int(current_stat.st_ctime_ns),
                )
                if current_fingerprint != asset_stats[entry["bot_id"]]:
                    raise ValueError("cutover 资产在事务开始前发生变化")

            existing_chain = self._protocol_cutover_chain_tx(c, gid)
            if existing_chain:
                chain_tail = existing_chain[-1]
                previous_target = {
                    "ruleset_version": str(chain_tail["to_ruleset"]),
                    "protocol_version": str(chain_tail["to_protocol"]),
                    "rating_pool_id": str(chain_tail["to_rating_pool"]),
                }
                if previous_target != source:
                    raise ValueError("cutover marker chain 断链；from contract 不是链尾")

            drift_checks = (
                (
                    "SELECT COUNT(*) FROM bots WHERE game_id=? AND protocol_version<>?",
                    (gid, source["protocol_version"]),
                    "Bot protocol 漂移",
                ),
                (
                    "SELECT COUNT(*) FROM bot_versions WHERE bot_id IN "
                    "(SELECT id FROM bots WHERE game_id=?) AND retired_at IS NULL "
                    "AND protocol_version<>?",
                    (gid, source["protocol_version"]),
                    "未退役 Bot version protocol 漂移",
                ),
                (
                    "SELECT COUNT(*) FROM bot_versions WHERE bot_id IN "
                    "(SELECT id FROM bots WHERE game_id=?) AND retired_at IS NOT NULL "
                    "AND protocol_version=?",
                    (gid, source["protocol_version"]),
                    "source protocol 世代在切换前已有退役版本",
                ),
                (
                    "SELECT COUNT(*) FROM execution_jobs WHERE game_id=? AND "
                    "status IN ('queued','starting','running','settling') AND "
                    "(ruleset_version<>? OR protocol_version<>? OR rating_pool_id<>?)",
                    (
                        gid,
                        source["ruleset_version"],
                        source["protocol_version"],
                        source["rating_pool_id"],
                    ),
                    "可运行执行任务 contract 漂移",
                ),
            )
            for sql, params, message in drift_checks:
                if int(c.execute(sql, params).fetchone()[0]):
                    raise ValueError(message)

            by_bot = {int(row["id"]): row for row in database_bots}
            for entry in normalized_manifest:
                bot = by_bot[entry["bot_id"]]
                if int(bot["current_version"] or 0) != entry["expected_current_version"]:
                    raise ValueError(f"Bot {entry['bot_id']} current_version 已变化")
                current_version = None
                if int(bot["current_version"] or 0) > 0:
                    current_version = c.execute(
                        "SELECT * FROM bot_versions WHERE bot_id=? AND version=?",
                        (entry["bot_id"], int(bot["current_version"])),
                    ).fetchone()
                    if current_version is None:
                        raise ValueError(f"Bot {entry['bot_id']} 当前版本行缺失")
                    if current_version["retired_at"] is not None:
                        raise ValueError(f"Bot {entry['bot_id']} 当前版本已退役")
                    if str(current_version["protocol_version"] or "") != source[
                        "protocol_version"
                    ]:
                        raise ValueError(f"Bot {entry['bot_id']} 当前版本协议非 source")
                actual_current_id = (
                    int(current_version["id"]) if current_version is not None else None
                )
                actual_current_checksum = (
                    str(current_version["checksum"] or "")
                    if current_version is not None else ""
                )
                actual_current_path = (
                    str(current_version["binary_path"] or "")
                    if current_version is not None
                    else str(bot["binary_path"] or "")
                )
                actual_current_runtime_mode = str(
                    current_version["runtime_mode"]
                    if current_version is not None
                    else bot["runtime_mode"]
                )
                if actual_current_id != entry["expected_current_version_id"]:
                    raise ValueError(f"Bot {entry['bot_id']} 当前版本 id 已变化")
                if actual_current_checksum != entry["expected_current_checksum"]:
                    raise ValueError(f"Bot {entry['bot_id']} 当前 checksum 已变化")
                if actual_current_path != entry["expected_current_binary_path"]:
                    raise ValueError(f"Bot {entry['bot_id']} 当前路径已变化")
                if (
                    actual_current_runtime_mode
                    != entry["expected_current_runtime_mode"]
                ):
                    raise ValueError(f"Bot {entry['bot_id']} 当前 runtime_mode 已变化")
                maximum = int(
                    c.execute(
                        "SELECT COALESCE(MAX(version),0) FROM bot_versions WHERE bot_id=?",
                        (entry["bot_id"],),
                    ).fetchone()[0]
                )
                if entry["version"] != maximum + 1:
                    raise ValueError(
                        f"Bot {entry['bot_id']} 新版本号必须固定为 v{maximum + 1}"
                    )

            archived_at = _now()
            rating_rows = [
                dict(row) for row in c.execute(
                    "SELECT * FROM ratings WHERE game_id=? ORDER BY bot_id", (gid,)
                ).fetchall()
            ]
            history_rows = [
                dict(row) for row in c.execute(
                    "SELECT * FROM rating_history WHERE game_id=? ORDER BY id", (gid,)
                ).fetchall()
            ]
            pair_rows = [
                dict(row) for row in c.execute(
                    "SELECT p.* FROM pair_stats p JOIN bots a ON a.id=p.bot_a_id "
                    "JOIN bots b ON b.id=p.bot_b_id "
                    "WHERE a.game_id=? AND b.game_id=? ORDER BY p.bot_a_id,p.bot_b_id",
                    (gid, gid),
                ).fetchall()
            ]
            archive_digest = rating_projection_digest(
                rating_rows, history_rows, pair_rows
            )
            c.execute(
                "INSERT INTO ratings_archive(bot_id,game_id,pool_id,rating,rd,vol,"
                "wins,losses,draws,delta_total,matches_played,last_played_at,archived_at) "
                "SELECT bot_id,game_id,?,rating,rd,vol,wins,losses,draws,delta_total,"
                "matches_played,last_played_at,? FROM ratings WHERE game_id=?",
                (source["rating_pool_id"], archived_at, gid),
            )
            c.execute(
                "INSERT INTO rating_history_archive(original_id,bot_id,game_id,pool_id,"
                "rating,rd,vol,matches_played,reason,created_at,archived_at) "
                "SELECT id,bot_id,game_id,?,rating,rd,vol,matches_played,reason,"
                "created_at,? FROM rating_history WHERE game_id=?",
                (source["rating_pool_id"], archived_at, gid),
            )
            c.execute(
                "INSERT INTO pair_stats_archive(bot_a_id,bot_b_id,game_id,pool_id,"
                "samples,last_played_at,a_wins,a_losses,draws,archived_at) "
                "SELECT p.bot_a_id,p.bot_b_id,?,?,p.samples,p.last_played_at,"
                "p.a_wins,p.a_losses,p.draws,? FROM pair_stats p "
                "JOIN bots a ON a.id=p.bot_a_id JOIN bots b ON b.id=p.bot_b_id "
                "WHERE a.game_id=? AND b.game_id=?",
                (gid, source["rating_pool_id"], archived_at, gid, gid),
            )
            c.execute(
                "INSERT INTO rating_pool_archives(game_id,pool_id,ruleset_version,"
                "protocol_version,archived_at,ratings_count,history_count,pair_count,"
                "projection_digest) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    gid, source["rating_pool_id"], source["ruleset_version"],
                    source["protocol_version"], archived_at, len(rating_rows),
                    len(history_rows), len(pair_rows), archive_digest,
                ),
            )

            new_version_ids: list[int] = []
            for entry in normalized_manifest:
                cur = c.execute(
                    "INSERT INTO bot_versions(bot_id,version,binary_path,upload_note,"
                    "checksum,size_bytes,os,arch,format,runtime_mode,protocol_version,"
                    "uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry["bot_id"], entry["version"], entry["binary_path"],
                        entry["upload_note"], entry["checksum"], entry["size_bytes"],
                        entry["os"], entry["arch"], entry["format"],
                        entry["runtime_mode"], target["protocol_version"], archived_at,
                    ),
                )
                new_version_ids.append(int(cur.lastrowid))
                c.execute(
                    "UPDATE bots SET current_version=?,binary_path=?,os=?,arch=?,format=?,"
                    "runtime_mode=?,protocol_version=?,updated_at=? WHERE id=?",
                    (
                        entry["version"], entry["binary_path"], entry["os"],
                        entry["arch"], entry["format"], entry["runtime_mode"],
                        target["protocol_version"], archived_at, entry["bot_id"],
                    ),
                )
            retired_count = int(
                c.execute(
                    "UPDATE bot_versions SET retired_at=?,"
                    "retirement_reason=CASE WHEN retirement_reason='' THEN ? "
                    "ELSE retirement_reason END WHERE bot_id IN "
                    "(SELECT id FROM bots WHERE game_id=?) AND retired_at IS NULL "
                    "AND protocol_version<>?",
                    (
                        archived_at,
                        str(retirement_reason)[:200],
                        gid,
                        target["protocol_version"],
                    ),
                ).rowcount
            )

            queued = c.execute(
                "UPDATE execution_jobs SET status='cancelled',retryable=0,"
                "terminal_reason='ruleset_retired',last_error='ruleset_retired',"
                "terminal_at=? WHERE game_id=? AND status='queued'",
                (archived_at, gid),
            ).rowcount
            interrupted = c.execute(
                "UPDATE execution_jobs SET retryable=0,terminal_reason='ruleset_retired',"
                "last_error='ruleset_retired',terminal_at=COALESCE(terminal_at,?) "
                "WHERE game_id=? AND status='interrupted'",
                (archived_at, gid),
            ).rowcount
            c.execute(
                "UPDATE auto_match_decisions SET lifecycle='cancelled',"
                "terminal_reason='ruleset_retired',terminal_at=? "
                "WHERE game_id=? AND lifecycle='queued'",
                (archived_at, gid),
            )
            c.execute(
                "UPDATE local_ai_agents SET status='revoked',"
                "connection_generation=connection_generation+1,connected_at=NULL,"
                "disconnected_at=?,updated_at=? WHERE game_id=? AND status='active'",
                (archived_at, archived_at, gid),
            )
            c.execute(
                "UPDATE local_ai_leases SET status='released',released_at=?,"
                "terminal_reason='ruleset_retired' WHERE status='active' AND agent_id IN "
                "(SELECT id FROM local_ai_agents WHERE game_id=?)",
                (archived_at, gid),
            )

            c.execute(
                "UPDATE ratings SET rating=1500.0,rd=350.0,vol=0.06,wins=0,losses=0,"
                "draws=0,delta_total=0,matches_played=0,last_played_at=NULL "
                "WHERE game_id=?",
                (gid,),
            )
            c.execute("DELETE FROM rating_history WHERE game_id=?", (gid,))
            c.execute(
                "DELETE FROM pair_stats WHERE bot_a_id IN "
                "(SELECT id FROM bots WHERE game_id=?) AND bot_b_id IN "
                "(SELECT id FROM bots WHERE game_id=?)",
                (gid, gid),
            )
            c.execute(
                "INSERT OR IGNORE INTO ratings(bot_id,game_id) "
                "SELECT id,game_id FROM bots WHERE game_id=?",
                (gid,),
            )
            for service_table in (
                "auto_match_owner_service", "auto_match_bot_service",
                "auto_match_bot_pair_service", "auto_match_owner_pair_service",
            ):
                c.execute(f"DELETE FROM {service_table} WHERE game_id=?", (gid,))
            changed = c.execute(
                "UPDATE rating_pool_state SET active_pool_id=?,ruleset_version=?,"
                "protocol_version=?,activated_at=? WHERE game_id=? AND "
                "active_pool_id=? AND ruleset_version=? AND protocol_version=?",
                (
                    target["rating_pool_id"], target["ruleset_version"],
                    target["protocol_version"], archived_at, gid,
                    source["rating_pool_id"], source["ruleset_version"],
                    source["protocol_version"],
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("active contract compare-and-swap 失败")

            live = rating_projection_digests(c)
            if live["issues"]:
                raise RuntimeError(
                    "切换后评分源校验失败: " + "; ".join(live["issues"][:5])
                )
            c.execute(
                "UPDATE rating_projection_state SET policy_version=?,rebuilt_at=?,"
                "source_settlement_count=?,source_last_settled_order=?,source_digest=?,"
                "projection_digest=?,plan_digest=?,"
                "trusted_mutation_revision=mutation_revision WHERE singleton=1",
                (
                    _RATING_PROJECTION_POLICY_VERSION, archived_at,
                    int(live["source_settlement_count"]),
                    int(live["source_last_settled_order"]), live["source_digest"],
                    live["projection_digest"], live["plan_digest"],
                ),
            )
            c.execute(
                "INSERT INTO protocol_cutovers(cutover_id,game_id,from_ruleset,"
                "to_ruleset,from_protocol,to_protocol,from_rating_pool,to_rating_pool,"
                "manifest_digest,manifest_json,bot_count,retired_count,cancelled_jobs,"
                "archive_digest,completed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    clean_cutover_id, gid, source["ruleset_version"],
                    target["ruleset_version"], source["protocol_version"],
                    target["protocol_version"], source["rating_pool_id"],
                    target["rating_pool_id"], manifest_digest, manifest_json,
                    len(database_bots), retired_count,
                    int(queued) + int(interrupted), archive_digest, archived_at,
                ),
            )
            marker = c.execute(
                "SELECT * FROM protocol_cutovers WHERE cutover_id=?",
                (clean_cutover_id,),
            ).fetchone()
            if marker is None:  # pragma: no cover - transaction invariant
                raise RuntimeError("cutover marker 写入失败")
            self._assert_protocol_cutover_postconditions_tx(
                c,
                marker,
                verify_assets=True,
                enforce_live_generation=True,
            )
            chain = self._protocol_cutover_chain_tx(c, gid)
            if not chain or str(chain[-1]["cutover_id"]) != clean_cutover_id:
                raise RuntimeError("cutover marker 未成为唯一链尾")
            return marker_result(marker, already_applied=False)

    def list_bot_versions(self, bot_id: int) -> list[dict]:
        with self._tx() as c:
            return [
                _row(r)
                for r in c.execute(
                    "SELECT * FROM bot_versions WHERE bot_id=? "
                    "ORDER BY version DESC",
                    (bot_id,),
                )
            ]

    def get_bot_version(self, version_id: int) -> dict | None:
        """按 version_id 取 bot_versions 行（含 binary_path，P1 版本冻结用）。"""
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM bot_versions WHERE id=?",
                    (version_id,),
                ).fetchone()
            )

    def get_latest_bot_version(self, bot_id: int) -> dict | None:
        """该 bot 历史中版本号最大的版本行（不一定是当前激活版本）。"""
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM bot_versions WHERE bot_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (bot_id,),
                ).fetchone()
            )

    def get_current_bot_version(self, bot_id: int) -> dict | None:
        """取 ``bots.current_version`` 当前激活版本对应的版本行。

        与 ``get_latest_bot_version``（历史最大版本，用于下一个上传版本号）语义
        刻意分离：用户回滚后，赛事发布必须冻结当前激活版本而非历史最大版本。
        """
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT v.* FROM bots b "
                    "JOIN bot_versions v "
                    "ON v.bot_id=b.id AND v.version=b.current_version "
                    "WHERE b.id=?",
                    (bot_id,),
                ).fetchone()
            )

    def bot_profile(self, bot_id: int) -> dict | None:
        """聚合 Bot 详情：身份、Glicko 数值、公开名次与可靠性样本。

        不含对局历史与对手战绩（单独端点，避免单次返回过大）。
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT b.*, u.username AS owner_name, "
                "u.display_name AS owner_display, "
                "r.rating, r.rd, COALESCE(r.wins,0) AS wins, "
                "COALESCE(r.losses,0) AS losses, COALESCE(r.draws,0) AS draws, "
                "COALESCE(r.matches_played,0) AS matches_played, "
                "COALESCE(r.matches_played,0) AS rated_matches "
                "FROM bots b "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "LEFT JOIN ratings r ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE b.id=?",
                (bot_id,),
            ).fetchone()
            d = _row(row)
            if d is None:
                return None

            gid = _registered_game_id(d.get("game_id"))
            match_table = _matches_table(gid)
            rated_matches = max(0, int(d.get("rated_matches") or 0))
            rating = d.get("rating")
            public_candidate = (
                bool(d.get("is_active"))
                and bool(d.get("is_ranked"))
                and d.get("format") == SUPPORTED_BINARY_FORMAT
                and d.get("os") == SUPPORTED_BINARY_OS
                and d.get("arch") == SUPPORTED_BINARY_ARCH
                and rating is not None
            )
            ranking_eligible = (
                public_candidate and rated_matches >= RANKING_MIN_RATED_MATCHES
            )
            cutoff_30d = (datetime.now() - timedelta(days=30)).isoformat(
                timespec="seconds"
            )
            metrics = c.execute(
                f"""
                SELECT
                  COUNT(*) AS rank_total,
                  COALESCE(SUM(CASE WHEN (
                    r.rating > :target_rating OR
                    (r.rating = :target_rating AND r.matches_played > :target_matches) OR
                    (r.rating = :target_rating AND r.matches_played = :target_matches
                     AND r.bot_id < :target_bot_id)
                  ) THEN 1 ELSE 0 END), 0) AS better_count,
                  (SELECT COUNT(DISTINCT opponent_id) FROM (
                    SELECT bot_b_id AS opponent_id FROM pair_stats WHERE bot_a_id=:target_bot_id
                    UNION ALL
                    SELECT bot_a_id AS opponent_id FROM pair_stats WHERE bot_b_id=:target_bot_id
                  )) AS unique_opponents,
                  (SELECT rh.rating FROM rating_history rh
                   WHERE rh.bot_id=:target_bot_id AND rh.game_id=:game_id
                   ORDER BY rh.id DESC LIMIT 1 OFFSET 1) AS prev_rating,
                  (SELECT rh.rating FROM rating_history rh
                   WHERE rh.bot_id=:target_bot_id AND rh.game_id=:game_id
                     AND rh.created_at <= :cutoff_30d
                   ORDER BY rh.created_at DESC, rh.id DESC LIMIT 1) AS baseline_30d,
                  (SELECT COUNT(*) FROM {match_table} tm
                   JOIN match_rating_settlements settled ON settled.match_id=tm.id
                   WHERE tm.status=:completed
                     AND tm.match_type NOT IN (:contest_type,:human_type)
                     AND tm.bot_a_id <> tm.bot_b_id AND tm.technical_loss=1
                     AND ((tm.bot_a_id=:target_bot_id AND tm.winner=1)
                          OR (tm.bot_b_id=:target_bot_id AND tm.winner=0)))
                    AS technical_failures
                FROM ratings r
                JOIN bots b ON b.id=r.bot_id AND b.game_id=r.game_id
                WHERE b.is_active=1 AND b.is_ranked=1 AND b.format=:binary_format
                  AND b.os=:binary_os AND b.arch=:binary_arch
                  AND b.game_id=:game_id
                  AND r.matches_played >= :ranking_min_matches
                """,
                {
                    "target_rating": rating,
                    "target_matches": rated_matches,
                    "target_bot_id": bot_id,
                    "game_id": gid,
                    "cutoff_30d": cutoff_30d,
                    "completed": STATUS_COMPLETED,
                    "contest_type": TYPE_CONTEST,
                    "human_type": TYPE_HUMAN,
                    "binary_format": SUPPORTED_BINARY_FORMAT,
                    "binary_os": SUPPORTED_BINARY_OS,
                    "binary_arch": SUPPORTED_BINARY_ARCH,
                    "ranking_min_matches": RANKING_MIN_RATED_MATCHES,
                },
            ).fetchone()
            metric = _row(metrics) or {}
            rank_total = int(metric.get("rank_total") or 0)
            rank = int(metric.get("better_count") or 0) + 1 if ranking_eligible else None
            prev_rating = metric.get("prev_rating")
            baseline_30d = metric.get("baseline_30d")
            d["rating_delta"] = (
                round(float(rating) - float(prev_rating), 2)
                if rating is not None and prev_rating is not None else None
            )
            d["recent_delta_30d"] = (
                round(float(rating) - float(baseline_30d), 2)
                if rating is not None and baseline_30d is not None else None
            )
            d["unique_opponents"] = int(metric.get("unique_opponents") or 0)
            d["technical_failures"] = int(metric.get("technical_failures") or 0)
            if rated_matches > 0:
                d["normal_completion_rate"] = round(
                    max(0.0, min(1.0, (rated_matches - d["technical_failures"]) / rated_matches)),
                    4,
                )
            else:
                d["normal_completion_rate"] = None
            _attach_numeric_ranking(
                d,
                eligible=ranking_eligible,
                rank=rank,
                rank_total=rank_total,
            )
            return d

    def bot_opponents_stats(
        self,
        bot_id: int,
        *,
        limit: int = 20,
        page: int | None = None,
        per_page: int = 20,
    ) -> list[dict] | dict:
        """返回该 Bot 对各对手的战绩（按交手次数倒序），从 pair_stats 读。

        每行含 opponent_id/opponent_name/opponent_display/game_id/
        wins/losses/draws/samples/last_played_at（wins 从 bot_id 视角）。

        ``page`` 为空时保留旧 ``limit`` 列表契约；提供 ``page`` 时返回
        ``{items, page, per_page, total}``。分页的总数与当前页在同一个
        SQLite 读事务中取得，避免结算并发写入时两者来自不同快照。
        """

        def project(row: sqlite3.Row | dict) -> dict:
            d = _row(row) if isinstance(row, sqlite3.Row) else row
            a_id, b_id = d["bot_a_id"], d["bot_b_id"]
            opponent_side = "b" if a_id == bot_id else "a"
            opponent_id = b_id if a_id == bot_id else a_id
            # pair_stats 以小 id 为 a 规范化；目标在 b 位时须翻转胜负视角。
            wins = d["a_wins"] if a_id == bot_id else d["a_losses"]
            losses = d["a_losses"] if a_id == bot_id else d["a_wins"]
            opponent_name = d[f"bot_{opponent_side}_name"]
            return {
                "opponent_id": opponent_id,
                "opponent_name": (
                    opponent_name if opponent_name is not None else f"#{opponent_id}"
                ),
                "opponent_display": d[f"bot_{opponent_side}_display"] or "",
                "game_id": d[f"bot_{opponent_side}_game_id"] or "",
                "wins": wins,
                "losses": losses,
                "draws": d["draws"],
                "samples": d["samples"],
                "last_played_at": d["last_played_at"],
            }

        sql = (
            "SELECT ps.bot_a_id, ps.bot_b_id, ps.a_wins, ps.a_losses, "
            "ps.draws, (ps.a_wins+ps.a_losses+ps.draws) AS samples, "
            "ps.last_played_at, "
            "ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
            "ba.game_id AS bot_a_game_id, "
            "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
            "bb.game_id AS bot_b_game_id "
            "FROM pair_stats ps "
            "LEFT JOIN bots ba ON ba.id=ps.bot_a_id "
            "LEFT JOIN bots bb ON bb.id=ps.bot_b_id "
            "WHERE ps.bot_a_id=? OR ps.bot_b_id=? "
            "ORDER BY samples DESC, ps.last_played_at DESC, "
            "ps.bot_a_id, ps.bot_b_id"
        )
        params = (bot_id, bot_id)
        with self._tx() as c:
            if page is not None:
                normalized_page = max(1, int(page))
                normalized_per_page = max(1, min(200, int(per_page)))
                c.execute("BEGIN")
                rows, total = _paginate(
                    c,
                    sql,
                    params,
                    page=normalized_page,
                    per_page=normalized_per_page,
                )
                return {
                    "items": [project(row) for row in rows],
                    "page": normalized_page,
                    "per_page": normalized_per_page,
                    "total": total,
                }

            legacy_limit = max(1, min(int(limit), 100))
            rows = c.execute(f"{sql} LIMIT ?", params + (legacy_limit,)).fetchall()
            return [project(row) for row in rows]

    # ── matches（全面解耦 PR3：拆每游戏一张表 + matches_index 定位）─────

    @staticmethod
    def _bot_has_active_rated_match_tx(
        c: sqlite3.Connection,
        bot_id: int,
        *,
        game_id: str | None = None,
    ) -> bool:
        """Return whether a Bot is already in a rating-bearing active match.

        Pending/running and completed-but-unsettled are one rating lifecycle.
        The immutable policy row is the only authority: current owners or
        ranked selections must never reinterpret a historical Match.  Callers
        hold the same SQLite transaction used for selection or creation;
        per-game triggers enforce the same rule for every writer.
        """
        gids = (game_id,) if game_id is not None else tuple(_all_game_ids())
        for gid in gids:
            tbl = _matches_table(gid)
            row = c.execute(
                f"SELECT 1 FROM {tbl} m "
                "LEFT JOIN match_rating_policies policy ON policy.match_id=m.id "
                "WHERE COALESCE(policy.rated,1)=1 AND "
                "(m.bot_a_id=? OR m.bot_b_id=?) AND ("
                "m.status IN (?,?) OR (m.status=? AND NOT EXISTS ("
                "SELECT 1 FROM match_rating_settlements settled "
                "WHERE settled.match_id=m.id))) LIMIT 1",
                (
                    bot_id,
                    bot_id,
                    STATUS_PENDING,
                    STATUS_RUNNING,
                    STATUS_COMPLETED,
                ),
            ).fetchone()
            if row is not None:
                return True
        return False

    @staticmethod
    def _rating_projection_status_tx(c: sqlite3.Connection) -> dict:
        state = c.execute(
            "SELECT * FROM rating_projection_state WHERE singleton=1"
        ).fetchone()
        state_data = dict(state) if state else {}
        live = rating_projection_digests(c)
        source_count = int(live["source_settlement_count"])
        source_last = int(live["source_last_settled_order"])
        ready = bool(
            state_data.get("policy_version") == _RATING_PROJECTION_POLICY_VERSION
            and int(state_data.get("mutation_revision") or 0)
            == int(state_data.get("trusted_mutation_revision") or 0)
            and int(state_data.get("source_settlement_count") or 0) == source_count
            and int(state_data.get("source_last_settled_order") or 0) == source_last
            and str(state_data.get("source_digest") or "")
            == live["source_digest"]
            and str(state_data.get("projection_digest") or "")
            == live["projection_digest"]
            and str(state_data.get("plan_digest") or "") == live["plan_digest"]
            and not live["issues"]
        )
        return {
            "ready": ready,
            "required_policy_version": _RATING_PROJECTION_POLICY_VERSION,
            "source_settlement_count": source_count,
            "source_last_settled_order": source_last,
            "source_digest": live["source_digest"],
            "projection_digest": live["projection_digest"],
            "plan_digest": live["plan_digest"],
            "bot_universe_digest": live["bot_universe_digest"],
            "sequence_next_order": live["sequence_next_order"],
            "issues": live["issues"],
            "state": state_data,
        }

    @staticmethod
    def _rating_projection_mutation_baseline_tx(
        c: sqlite3.Connection,
    ) -> dict[str, Any]:
        """Build the canonical marker-settled prefix for an online mutation.

        Completion freezes ``settled_order`` before the marker transaction.  A
        valid live database may therefore have a consecutive tail of completed,
        unsettled policies.  That tail is allowed here but remains intentionally
        unready in :meth:`rating_projection_status` until it is settled.
        """
        live = rating_projection_digests(c)
        settled_count = int(live["source_settlement_count"])
        settled_last = int(live["source_last_settled_order"])
        reserved = [
            dict(row)
            for row in c.execute(
                "SELECT policy.match_id,policy.game_id,policy.rating_reason,"
                "policy.settled_order FROM match_rating_policies policy "
                "JOIN rating_pool_state pool ON pool.game_id=policy.game_id "
                "AND pool.active_pool_id=policy.rating_pool_id "
                "LEFT JOIN match_rating_settlements settled "
                "ON settled.match_id=policy.match_id "
                "WHERE policy.settled_order IS NOT NULL "
                "AND settled.match_id IS NULL ORDER BY policy.settled_order"
            ).fetchall()
        ]
        reserved_orders = [int(row["settled_order"]) for row in reserved]
        expected_issues = {
            f"rating policy reserved but unsettled: {row['match_id']}"
            for row in reserved
        }
        shape_valid = bool(
            reserved_orders == sorted(set(reserved_orders))
            and all(order > settled_last for order in reserved_orders)
            and set(live["issues"]) == expected_issues
        )
        if shape_valid:
            for row in reserved:
                game_id = str(row.get("game_id") or "")
                if (
                    game_id not in _all_game_ids()
                    or row.get("rating_reason") in {"contest", "human"}
                ):
                    shape_valid = False
                    break
                match = c.execute(
                    f"SELECT status FROM {_matches_table(game_id)} WHERE id=?",
                    (row["match_id"],),
                ).fetchone()
                if match is None or match["status"] != STATUS_COMPLETED:
                    shape_valid = False
                    break
        return {
            "valid": shape_valid,
            "source_settlement_count": settled_count,
            "source_last_settled_order": settled_last,
            "source_digest": live["settled_source_digest"],
            "projection_digest": live["projection_digest"],
            "plan_digest": live["settled_plan_digest"],
        }

    @classmethod
    def _rating_projection_mutation_guard_tx(
        cls, c: sqlite3.Connection
    ) -> _RatingProjectionMutationGuard:
        """Capture full pre-state trust inside the caller's write transaction."""
        state = c.execute(
            "SELECT * FROM rating_projection_state WHERE singleton=1"
        ).fetchone()
        state_data = dict(state) if state else {}
        baseline = cls._rating_projection_mutation_baseline_tx(c)
        trusted = bool(
            baseline["valid"]
            and state_data.get("policy_version")
            == _RATING_PROJECTION_POLICY_VERSION
            and int(state_data.get("mutation_revision") or 0)
            == int(state_data.get("trusted_mutation_revision") or 0)
            and int(state_data.get("source_settlement_count") or 0)
            == baseline["source_settlement_count"]
            and int(state_data.get("source_last_settled_order") or 0)
            == baseline["source_last_settled_order"]
            and str(state_data.get("source_digest") or "")
            == baseline["source_digest"]
            and str(state_data.get("projection_digest") or "")
            == baseline["projection_digest"]
            and str(state_data.get("plan_digest") or "")
            == baseline["plan_digest"]
        )
        return _RatingProjectionMutationGuard(trusted_before=trusted)

    @classmethod
    def _advance_rating_projection_state_tx(
        cls,
        c: sqlite3.Connection,
        guard: _RatingProjectionMutationGuard,
    ) -> None:
        """Advance all summaries iff this transaction began from trusted state."""
        if not guard.trusted_before:
            return
        baseline = cls._rating_projection_mutation_baseline_tx(c)
        if not baseline["valid"]:
            return
        c.execute(
            "UPDATE rating_projection_state SET source_settlement_count=?,"
            "source_last_settled_order=?,source_digest=?,projection_digest=?,"
            "plan_digest=?,trusted_mutation_revision=mutation_revision "
            "WHERE singleton=1 AND policy_version=?",
            (
                baseline["source_settlement_count"],
                baseline["source_last_settled_order"],
                baseline["source_digest"],
                baseline["projection_digest"],
                baseline["plan_digest"],
                _RATING_PROJECTION_POLICY_VERSION,
            ),
        )

    def rating_projection_status(self) -> dict:
        with self._tx() as c:
            return self._rating_projection_status_tx(c)

    def _auto_queue_candidates_tx(self, c: sqlite3.Connection) -> list[dict]:
        rows = c.execute(
            "SELECT b.id AS bot_id,b.owner_id,b.game_id,b.name AS bot_name,"
            "b.display_name AS bot_display,u.username AS owner_name,"
            "u.display_name AS owner_display,v.id AS version_id,r.rating,r.rd,"
            "v.version AS version_number,v.binary_path AS version_binary_path,"
            "v.checksum AS version_checksum,v.size_bytes AS version_size_bytes,"
            "r.matches_played,COALESCE(os.served_count,0) AS owner_service,"
            "COALESCE(os.last_served_revision,0) AS owner_last_revision,"
            "COALESCE(bs.served_count,0) AS bot_service,"
            "COALESCE(bs.last_served_revision,0) AS bot_last_revision,"
            "COALESCE(bs.seat_a_count,0) AS seat_a_count,"
            "COALESCE(bs.seat_b_count,0) AS seat_b_count "
            "FROM bots b JOIN users u ON u.id=b.owner_id "
            "JOIN ratings r ON r.bot_id=b.id AND r.game_id=b.game_id "
            "JOIN bot_versions v ON v.bot_id=b.id AND v.version=b.current_version "
            "JOIN rating_pool_state pool ON pool.game_id=b.game_id "
            "LEFT JOIN auto_match_owner_service os "
            "ON os.owner_id=b.owner_id AND os.game_id=b.game_id "
            "LEFT JOIN auto_match_bot_service bs "
            "ON bs.bot_id=b.id AND bs.game_id=b.game_id "
            "WHERE b.is_active=1 AND b.is_ranked=1 AND b.is_builtin=0 "
            "AND u.is_active=1 "
            "AND b.binary_path<>'' AND v.binary_path<>'' "
            "AND b.binary_path=v.binary_path AND b.runtime_mode=v.runtime_mode "
            "AND v.retired_at IS NULL AND b.protocol_version=pool.protocol_version "
            "AND v.protocol_version=pool.protocol_version "
            "AND b.format=? AND b.os=? AND b.arch=? "
            "AND v.format=? AND v.os=? AND v.arch=? "
            "AND NOT EXISTS (SELECT 1 FROM execution_jobs q "
            "WHERE q.source='auto' AND q.status IN "
            "('queued','starting','running','settling') "
            "AND b.id IN (q.bot_a_id,q.bot_b_id)) "
            "AND NOT EXISTS (SELECT 1 FROM execution_jobs q "
            "JOIN bots qa ON qa.id=q.bot_a_id JOIN bots qb ON qb.id=q.bot_b_id "
            "WHERE q.source='auto' AND q.status IN "
            "('queued','starting','running','settling') "
            "AND b.owner_id IN (qa.owner_id,qb.owner_id))",
            (
                SUPPORTED_BINARY_FORMAT,
                SUPPORTED_BINARY_OS,
                SUPPORTED_BINARY_ARCH,
                SUPPORTED_BINARY_FORMAT,
                SUPPORTED_BINARY_OS,
                SUPPORTED_BINARY_ARCH,
            ),
        ).fetchall()
        return [
            dict(row)
            for row in rows
            if not self._bot_has_active_rated_match_tx(
                c, int(row["bot_id"]), game_id=str(row["game_id"])
            )
        ]

    @staticmethod
    def _auto_bot_key(bot: dict) -> tuple[int, int, int]:
        return (
            int(bot.get("bot_service") or 0),
            int(bot.get("bot_last_revision") or 0),
            int(bot["bot_id"]),
        )

    @staticmethod
    def _auto_owner_key(bot: dict) -> tuple[int, int, int]:
        return (
            int(bot.get("owner_service") or 0),
            int(bot.get("owner_last_revision") or 0),
            int(bot["owner_id"]),
        )

    @staticmethod
    def _auto_pair_counts_tx(
        c: sqlite3.Connection, anchor: dict, partner: dict
    ) -> tuple[int, int]:
        gid = str(anchor["game_id"])
        bot_lo, bot_hi = sorted((int(anchor["bot_id"]), int(partner["bot_id"])))
        owner_lo, owner_hi = sorted(
            (int(anchor["owner_id"]), int(partner["owner_id"]))
        )
        bot_row = c.execute(
            "SELECT served_count FROM auto_match_bot_pair_service "
            "WHERE game_id=? AND bot_lo_id=? AND bot_hi_id=?",
            (gid, bot_lo, bot_hi),
        ).fetchone()
        owner_row = c.execute(
            "SELECT served_count FROM auto_match_owner_pair_service "
            "WHERE game_id=? AND owner_lo_id=? AND owner_hi_id=?",
            (gid, owner_lo, owner_hi),
        ).fetchone()
        return (
            int(bot_row["served_count"] or 0) if bot_row else 0,
            int(owner_row["served_count"] or 0) if owner_row else 0,
        )

    @staticmethod
    def _auto_queue_seat_debt_tx(
        c: sqlite3.Connection, bot: dict
    ) -> int:
        queued = c.execute(
            "SELECT SUM(CASE WHEN bot_a_id=? THEN 1 ELSE 0 END) AS seat_a,"
            "SUM(CASE WHEN bot_b_id=? THEN 1 ELSE 0 END) AS seat_b "
            "FROM execution_jobs WHERE source='auto' AND status IN "
            "('queued','starting','running','settling') "
            "AND (bot_a_id=? OR bot_b_id=?)",
            (bot["bot_id"], bot["bot_id"], bot["bot_id"], bot["bot_id"]),
        ).fetchone()
        return (
            int(bot.get("seat_a_count") or 0)
            - int(bot.get("seat_b_count") or 0)
            + int(queued["seat_a"] or 0)
            - int(queued["seat_b"] or 0)
        )

    def _auto_choose_pair_tx(
        self,
        c: sqlite3.Connection,
        candidates: list[dict],
        *,
        game_id: str,
        lane: str,
        bootstrap_target_matches: int,
    ) -> tuple[dict, dict, str, int, int, float] | None:
        game = [bot for bot in candidates if bot["game_id"] == game_id]
        if len({int(bot["owner_id"]) for bot in game}) < 2:
            return None
        for bot in game:
            bot["_lane"] = (
                "bootstrap"
                if int(bot.get("matches_played") or 0)
                < bootstrap_target_matches
                else "established"
            )
        lane_bots = [bot for bot in game if bot["_lane"] == lane]
        if not lane_bots:
            return None

        # One representative Bot per owner.  Owner service is the first layer;
        # within the owner, the least auto-served Bot rotates.
        owner_best: dict[int, dict] = {}
        for bot in lane_bots:
            owner = int(bot["owner_id"])
            current = owner_best.get(owner)
            if current is None or self._auto_bot_key(bot) < self._auto_bot_key(current):
                owner_best[owner] = bot
        anchors = sorted(owner_best.values(), key=lambda bot: (
            self._auto_owner_key(bot), self._auto_bot_key(bot)
        ))

        for anchor in anchors:
            exact = [
                bot for bot in game
                if int(bot["owner_id"]) != int(anchor["owner_id"])
                and bot["_lane"] == lane
            ]
            widened_reason = ""
            partners = exact
            if not partners and lane == "bootstrap":
                # A sole bootstrap owner still receives cold-start service,
                # but never against itself; an established owner is the auditable fallback.
                partners = [
                    bot for bot in game
                    if int(bot["owner_id"]) != int(anchor["owner_id"])
                    and bot["_lane"] == "established"
                ]
                if partners:
                    widened_reason = "single_bootstrap_owner"
            elif not partners and lane == "established":
                bootstrap_partners = [
                    bot for bot in game
                    if int(bot["owner_id"]) != int(anchor["owner_id"])
                    and bot["_lane"] == "bootstrap"
                ]
                # A lone established owner must still receive established-lane service.
                # Require at least two bootstrap owners so the fallback does not
                # degenerate into one permanently repeated cross-lane pair.
                if len({int(bot["owner_id"]) for bot in bootstrap_partners}) >= 2:
                    partners = bootstrap_partners
                    widened_reason = "single_established_owner"
            if not partners:
                continue

            partner_owner_best: dict[int, dict] = {}
            for bot in partners:
                owner = int(bot["owner_id"])
                current = partner_owner_best.get(owner)
                if current is None or self._auto_bot_key(bot) < self._auto_bot_key(current):
                    partner_owner_best[owner] = bot
            partners = list(partner_owner_best.values())

            # Waiting debt is an owner-level admission layer, not a late
            # tie-break.  A frequently served owner can never jump ahead merely
            # because its pair count or rating gap is smaller.
            oldest_owner_layer = min(
                (
                    int(bot.get("owner_service") or 0),
                    int(bot.get("owner_last_revision") or 0),
                )
                for bot in partners
            )
            partners = [
                bot for bot in partners
                if (
                    int(bot.get("owner_service") or 0),
                    int(bot.get("owner_last_revision") or 0),
                ) == oldest_owner_layer
            ]

            def partner_key(bot: dict) -> tuple:
                bot_pair, owner_pair = self._auto_pair_counts_tx(c, anchor, bot)
                return (
                    bot_pair,
                    owner_pair,
                    abs(float(anchor.get("rating") or 1500.0)
                        - float(bot.get("rating") or 1500.0)),
                    self._auto_bot_key(bot),
                    int(bot["owner_id"]),
                    int(bot["bot_id"]),
                )

            partner = min(partners, key=partner_key)
            bot_pair, owner_pair = self._auto_pair_counts_tx(c, anchor, partner)
            rating_gap = abs(
                float(anchor.get("rating") or 1500.0)
                - float(partner.get("rating") or 1500.0)
            )
            return (
                anchor,
                partner,
                widened_reason,
                bot_pair,
                owner_pair,
                rating_gap,
            )
        return None

    @staticmethod
    def _auto_complete_service_tx(
        c: sqlite3.Connection, decision: sqlite3.Row | dict, terminal_at: str
    ) -> None:
        d = dict(decision)
        revision = int(d["state_revision"]) + 1
        gid = str(d["game_id"])
        for owner_id in (int(d["owner_a_id"]), int(d["owner_b_id"])):
            c.execute(
                "INSERT INTO auto_match_owner_service("
                "owner_id,game_id,served_count,last_served_revision,last_served_at) "
                "VALUES(?,?,1,?,?) ON CONFLICT(owner_id,game_id) DO UPDATE SET "
                "served_count=auto_match_owner_service.served_count+1,"
                "last_served_revision=excluded.last_served_revision,"
                "last_served_at=excluded.last_served_at",
                (owner_id, gid, revision, terminal_at),
            )
        for bot_id, seat_a, seat_b in (
            (int(d["bot_a_id"]), 1, 0),
            (int(d["bot_b_id"]), 0, 1),
        ):
            c.execute(
                "INSERT INTO auto_match_bot_service("
                "bot_id,game_id,served_count,seat_a_count,seat_b_count,"
                "last_served_revision,last_served_at) VALUES(?,?,1,?,?,?,?) "
                "ON CONFLICT(bot_id,game_id) DO UPDATE SET "
                "served_count=auto_match_bot_service.served_count+1,"
                "seat_a_count=auto_match_bot_service.seat_a_count+excluded.seat_a_count,"
                "seat_b_count=auto_match_bot_service.seat_b_count+excluded.seat_b_count,"
                "last_served_revision=excluded.last_served_revision,"
                "last_served_at=excluded.last_served_at",
                (bot_id, gid, seat_a, seat_b, revision, terminal_at),
            )
        bot_lo, bot_hi = sorted((int(d["bot_a_id"]), int(d["bot_b_id"])))
        owner_lo, owner_hi = sorted((int(d["owner_a_id"]), int(d["owner_b_id"])))
        c.execute(
            "INSERT INTO auto_match_bot_pair_service("
            "game_id,bot_lo_id,bot_hi_id,served_count,last_served_at) "
            "VALUES(?,?,?,1,?) ON CONFLICT(game_id,bot_lo_id,bot_hi_id) "
            "DO UPDATE SET served_count=auto_match_bot_pair_service.served_count+1,"
            "last_served_at=excluded.last_served_at",
            (gid, bot_lo, bot_hi, terminal_at),
        )
        c.execute(
            "INSERT INTO auto_match_owner_pair_service("
            "game_id,owner_lo_id,owner_hi_id,served_count,last_served_at) "
            "VALUES(?,?,?,1,?) ON CONFLICT(game_id,owner_lo_id,owner_hi_id) "
            "DO UPDATE SET served_count=auto_match_owner_pair_service.served_count+1,"
            "last_served_at=excluded.last_served_at",
            (gid, owner_lo, owner_hi, terminal_at),
        )

    @staticmethod
    def _match_rating_policy_tx(
        c: sqlite3.Connection, match: dict
    ) -> tuple[bool, str]:
        match_id = str(match.get("id") or "")
        if match_id:
            frozen = c.execute(
                "SELECT rated,rating_reason FROM match_rating_policies "
                "WHERE match_id=?",
                (match_id,),
            ).fetchone()
            if frozen is not None:
                return bool(int(frozen["rated"])), str(frozen["rating_reason"])
        match_type = str(match.get("match_type") or "")
        if match_type == TYPE_CONTEST:
            return False, "contest"
        if match_type == TYPE_HUMAN:
            return False, "human"
        bot_a_id = match.get("bot_a_id")
        bot_b_id = match.get("bot_b_id")
        if bot_a_id is None or bot_b_id is None:
            config = match.get("match_config") or {}
            if isinstance(config, dict) and type(config.get("_rating_eligible")) is bool:
                return bool(config["_rating_eligible"]), str(
                    config.get("_rating_reason") or "eligible"
                )
            return False, "bot_missing"
        if int(bot_a_id) == int(bot_b_id):
            return False, "self_play"
        owners = c.execute(
            "SELECT id,owner_id FROM bots WHERE id IN (?,?)",
            (int(bot_a_id), int(bot_b_id)),
        ).fetchall()
        owner_by_bot = {int(row["id"]): int(row["owner_id"]) for row in owners}
        if len(owner_by_bot) != 2:
            return False, "bot_missing"
        if owner_by_bot[int(bot_a_id)] == owner_by_bot[int(bot_b_id)]:
            return False, "same_owner"
        return True, "eligible"

    def match_rating_policy(self, match: dict) -> dict:
        with self._tx() as c:
            rated, reason = self._match_rating_policy_tx(c, match)
            return {"rated": rated, "rating_reason": reason}

    def create_match(
        self,
        match_id: str,
        bot_a_id: int,
        bot_b_id: int,
        *,
        owner_id: int | None = None,
        contest_id: int | None = None,
        match_type: str = "challenge",
        game_id: str = "holdem",
        match_config: dict | None = None,
        human_user_id: int | None = None,
        human_seat: int | None = None,
    ) -> dict:
        gid = _registered_game_id(game_id)
        tbl = _matches_table(gid)
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contract = _active_game_contract_tx(c, gid)
            identities = c.execute(
                "SELECT id,owner_id,game_id,protocol_version,is_ranked FROM bots "
                "WHERE id IN (?,?) ORDER BY id",
                (bot_a_id, bot_b_id),
            ).fetchall()
            by_id = {int(row["id"]): row for row in identities}
            if bot_a_id not in by_id or bot_b_id not in by_id:
                raise ValueError("Bot 不存在")
            if by_id[bot_a_id]["game_id"] != gid or by_id[bot_b_id]["game_id"] != gid:
                raise ValueError("Bot 与对局游戏不一致")
            if any(
                str(by_id[bot_id]["protocol_version"] or "")
                != contract["protocol_version"]
                for bot_id in (bot_a_id, bot_b_id)
            ):
                raise ValueError("Bot 协议与当前游戏规则不兼容")
            if match_type == TYPE_CONTEST:
                rating_reason = "contest"
            elif match_type == TYPE_HUMAN:
                rating_reason = "human"
            elif bot_a_id == bot_b_id:
                rating_reason = "self_play"
            elif int(by_id[bot_a_id]["owner_id"]) == int(by_id[bot_b_id]["owner_id"]):
                rating_reason = "same_owner"
            elif not int(by_id[bot_a_id]["is_ranked"] or 0) or not int(
                by_id[bot_b_id]["is_ranked"] or 0
            ):
                rating_reason = "ranked_bot_not_selected"
            else:
                rating_reason = "eligible"
            rated_pair = rating_reason == "eligible"
            config = dict(match_config or {})
            # Rating policy is internal and canonical; callers cannot override it.
            config["_rating_eligible"] = rated_pair
            config["_rating_reason"] = rating_reason
            mc_json = json.dumps(config, ensure_ascii=False)
            if rated_pair:
                if self._bot_has_active_rated_match_tx(c, bot_a_id, game_id=gid):
                    raise ValueError("座位 1 Bot 正在其他计分对局中")
                if self._bot_has_active_rated_match_tx(c, bot_b_id, game_id=gid):
                    raise ValueError("座位 2 Bot 正在其他计分对局中")
            created_at = _now()
            c.execute(
                f"INSERT INTO {tbl}(id, bot_a_id, bot_b_id, owner_id, "
                "contest_id, reason, match_type, status, game_id,ruleset_version,"
                "protocol_version,rating_pool_id,match_config, "
                "human_user_id, human_seat, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    match_id,
                    bot_a_id,
                    bot_b_id,
                    owner_id,
                    contest_id,
                    "",
                    match_type,
                    "pending",
                    gid,
                    contract["ruleset_version"],
                    contract["protocol_version"],
                    contract["rating_pool_id"],
                    mc_json,
                    human_user_id,
                    human_seat,
                    created_at,
                ),
            )
            c.execute(
                "INSERT INTO match_rating_policies("
                "match_id,game_id,rating_pool_id,bot_a_id,bot_b_id,rated,rating_reason,source,"
                "classified_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    match_id,
                    gid,
                    contract["rating_pool_id"],
                    bot_a_id,
                    bot_b_id,
                    1 if rated_pair else 0,
                    rating_reason,
                    "creation_v2",
                    created_at,
                ),
            )
            # 维护定位表
            c.execute(
                "INSERT OR REPLACE INTO matches_index(id, game_id) VALUES(?, ?)",
                (match_id, gid),
            )
            return _row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            )

    def _match_table_of(self, c, match_id: str) -> str | None:
        """经 matches_index 定位 match_id 所在的物理表；不存在返回 None。"""
        row = c.execute(
            "SELECT game_id FROM matches_index WHERE id=?", (match_id,)
        ).fetchone()
        if not row:
            return None
        raw_game_id = row["game_id"]
        game_id = _registered_game_id(raw_game_id)
        if raw_game_id != game_id:
            raise ValueError("matches_index.game_id 不是规范游戏标识")
        return _matches_table(game_id)

    @staticmethod
    def _reserve_rating_settlement_order_tx(
        c: sqlite3.Connection, match_id: str
    ) -> int | None:
        policy = c.execute(
            "SELECT rating_reason,settled_order FROM match_rating_policies "
            "WHERE match_id=?",
            (match_id,),
        ).fetchone()
        if policy is None or policy["rating_reason"] in {"contest", "human"}:
            return None
        if policy["settled_order"] is not None:
            return int(policy["settled_order"])
        sequence = c.execute(
            "SELECT next_order FROM rating_settlement_sequence WHERE singleton=1"
        ).fetchone()
        if sequence is None:
            raise RuntimeError("rating_settlement_sequence singleton missing")
        order = int(sequence["next_order"])
        changed = c.execute(
            "UPDATE match_rating_policies SET settled_order=? "
            "WHERE match_id=? AND settled_order IS NULL",
            (order, match_id),
        )
        if changed.rowcount != 1:
            row = c.execute(
                "SELECT settled_order FROM match_rating_policies WHERE match_id=?",
                (match_id,),
            ).fetchone()
            return int(row["settled_order"]) if row and row["settled_order"] else None
        c.execute(
            "UPDATE rating_settlement_sequence SET next_order=? WHERE singleton=1",
            (order + 1,),
        )
        return order

    @classmethod
    def _rating_settlement_order_for_insert_tx(
        cls, c: sqlite3.Connection, match_id: str
    ) -> int:
        order = cls._reserve_rating_settlement_order_tx(c, match_id)
        if order is not None:
            return order
        # Only legacy low-level fault-injection callers can lack a policy.  A
        # successful production settlement always has one; reserving here keeps
        # the insertion transaction deterministic and lets a later source audit
        # fail closed if such a marker were ever committed.
        sequence = c.execute(
            "SELECT next_order FROM rating_settlement_sequence WHERE singleton=1"
        ).fetchone()
        if sequence is None:
            raise RuntimeError("rating_settlement_sequence singleton missing")
        order = int(sequence["next_order"])
        c.execute(
            "UPDATE rating_settlement_sequence SET next_order=? WHERE singleton=1",
            (order + 1,),
        )
        return order

    @staticmethod
    def _attach_rating_settlement_state_tx(
        c: sqlite3.Connection, match: dict
    ) -> None:
        """Expose creation eligibility separately from the final settlement."""
        match_id = str(match.get("id") or "")
        policy = c.execute(
            "SELECT rated,rating_reason,settled_order FROM match_rating_policies "
            "WHERE match_id=?",
            (match_id,),
        ).fetchone()
        marker = c.execute(
            "SELECT settled_order FROM match_rating_settlements WHERE match_id=?",
            (match_id,),
        ).fetchone()
        rated = bool(int(policy["rated"])) if policy else False
        reason = str(policy["rating_reason"] or "unclassified") if policy else "unclassified"
        settled = marker is not None
        order = (
            int(policy["settled_order"])
            if policy is not None and policy["settled_order"] is not None
            else None
        )
        status = str(match.get("status") or "")
        if status == STATUS_ABORTED:
            settlement_status = "aborted_not_rated"
        elif status == STATUS_COMPLETED:
            if settled:
                settlement_status = "settled" if rated else "settled_neutral"
            else:
                settlement_status = (
                    "pending_settlement" if rated else "pending_neutral_settlement"
                )
        else:
            settlement_status = "eligible" if rated else "rating_neutral"
        match["rated"] = rated
        match["rating_reason"] = reason
        match["rating_settled"] = settled
        match["rating_settled_order"] = order
        match["rating_settlement_status"] = settlement_status
        match["_rating_settled_order"] = order

    def get_match(self, match_id: str) -> dict | None:
        with self._tx() as c:
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            result = _parse_match_json_cols(_row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            ))
            if result is not None:
                self._attach_rating_settlement_state_tx(c, result)
            return result

    def get_match_detailed(
        self,
        match_id: str,
        *,
        include_replay_incidents: bool = True,
    ) -> dict | None:
        """get_match + JOIN bots(ba/bb 名/display) + users(owner 名/display)。
        统一观赛/回放页座位身份显示用（bot_a/bot_b 各含 name/display_name +
        owner_name/owner_display）。人类对局(match_type=human)时 bot_a_id==bot_b_id
        复用同一 bot 行——人类侧靠 human_seat 区分（api 层标 is_human）。

        Public metadata callers pass ``include_replay_incidents=False`` so
        outcome rendering never opens ``match_replays.events_json``.  The
        opt-in default preserves the older diagnostics Store contract for
        internal/admin readers until those callers migrate explicitly.
        """
        with self._tx() as c:
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            replay_projection = (
                f", {_technical_incident_projection_sql('m')} "
                "AS _replay_incident_events_json"
                if include_replay_incidents
                else ""
            )
            sel = (
                "m.*, "
                "ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
                "ua.username AS bot_a_owner_name, ua.display_name AS bot_a_owner_display, "
                "ub.username AS bot_b_owner_name, ub.display_name AS bot_b_owner_display, "
                f"{_contest_expected_duplicate_projection_sql('m')} "
                "AS _contest_expected_duplicate, "
                f"{_contest_require_frozen_duplicate_projection_sql('m')} "
                "AS _contest_require_frozen_duplicate, "
                f"{_contest_stage_config_projection_sql('m')} "
                "AS _contest_stage_config_json"
                f"{replay_projection}"
            )
            sql = (
                f"SELECT {sel} FROM {tbl} m "
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id "
                "LEFT JOIN users ua ON ba.owner_id=ua.id "
                "LEFT JOIN users ub ON bb.owner_id=ub.id "
                "WHERE m.id=?"
            )
            result = _parse_match_json_cols(
                _row(c.execute(sql, (match_id,)).fetchone())
            )
            if result is not None:
                self._attach_rating_settlement_state_tx(c, result)
            return result

    def update_match(
        self,
        match_id: str,
        **fields: Any,
    ) -> dict | None:
        allowed = {
            "winner",
            "reason",
            "result",  # dict → 序列化 JSON 落 result 列
            "status",
            "started_at",
            "ended_at",
            "contest_id",
            "human_user_id",
            "human_seat",
            "match_seed",
            "technical_loss",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [
            (json.dumps(v, ensure_ascii=False) if k == "result" and not isinstance(v, str) else v)
            for k, v in fields.items()
            if k in allowed
        ]
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            projection_guard = (
                self._rating_projection_mutation_guard_tx(c)
                if fields.get("status") == STATUS_COMPLETED
                else None
            )
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            if sets:
                vals.append(match_id)
                status = fields.get("status")
                changed = c.execute(
                    f"UPDATE {tbl} SET {','.join(sets)} WHERE id=?", vals
                )
                if changed.rowcount != 1:
                    return None
                if status == STATUS_RUNNING:
                    started = fields.get("started_at") or _now()
                    c.execute(
                        "UPDATE execution_jobs SET status='running',started_at=? "
                        "WHERE current_match_id=? AND status='starting'",
                        (started, match_id),
                    )
                    c.execute(
                        "UPDATE execution_job_attempts SET status='running',started_at=? "
                        "WHERE match_id=? AND status='starting'",
                        (started, match_id),
                    )
                elif status in (STATUS_COMPLETED, STATUS_ABORTED):
                    settling = _now()
                    c.execute(
                        "UPDATE execution_jobs SET status='settling',settling_at=?,"
                        "cleanup_state=CASE WHEN cleanup_state='confirmed' "
                        "THEN 'confirmed' ELSE 'pending' END "
                        "WHERE current_match_id=? AND status IN ('starting','running')",
                        (settling, match_id),
                    )
                    c.execute(
                        "UPDATE execution_job_attempts SET status='settling' "
                        "WHERE match_id=? AND status IN ('starting','running')",
                        (match_id,),
                    )
            if fields.get("status") == STATUS_COMPLETED:
                self._reserve_rating_settlement_order_tx(c, match_id)
                if projection_guard is not None:
                    self._advance_rating_projection_state_tx(c, projection_guard)
            result = _row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            )
            if result is not None:
                policy = c.execute(
                    "SELECT settled_order FROM match_rating_policies WHERE match_id=?",
                    (match_id,),
                ).fetchone()
                result["_rating_settled_order"] = (
                    int(policy["settled_order"])
                    if policy and policy["settled_order"] is not None
                    else None
                )
            return result

    def abort_match_if_active(self, match_id: str, *, reason: str) -> dict | None:
        """仅把 pending/running 对局原子推进为 aborted；终态绝不倒退。"""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            c.execute(
                f"UPDATE {tbl} SET status=?, reason=?, ended_at=? "
                "WHERE id=? AND status IN (?,?)",
                (
                    STATUS_ABORTED, reason, _now(), match_id,
                    STATUS_PENDING, STATUS_RUNNING,
                ),
            )
            settling = _now()
            c.execute(
                "UPDATE execution_jobs SET status='settling',settling_at=?,"
                "cancel_requested=1,cleanup_state=CASE WHEN cleanup_state='confirmed' "
                "THEN 'confirmed' ELSE 'pending' END "
                "WHERE current_match_id=? AND status IN ('starting','running')",
                (settling, match_id),
            )
            c.execute(
                "UPDATE execution_job_attempts SET status='settling' "
                "WHERE match_id=? AND status IN ('starting','running')",
                (match_id,),
            )
            return _parse_match_json_cols(_row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            ))

    def delete_match(self, match_id: str) -> bool:
        """删除未结算对局；评分源已冻结/已结算时 fail closed。

        统一删除入口，保 matches_index 与 per-game 表不漂移（审计 P0：matches_index
        无清理会导致 like/view 计数静默 drift）。未结算行会同步删除 policy；
        settlement 与已分配序号是不可删除的永久审计证据。返回是否删到了行。
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return False
            if c.execute(
                "SELECT 1 FROM match_rating_settlements settled "
                "WHERE settled.match_id=? UNION ALL "
                "SELECT 1 FROM match_rating_policies policy "
                "WHERE policy.match_id=? AND policy.settled_order IS NOT NULL",
                (match_id, match_id),
            ).fetchone():
                raise ValueError("已结算对局是评分审计证据，禁止删除")
            if c.execute(
                "SELECT 1 FROM execution_jobs WHERE current_match_id=? "
                "AND status IN ('starting','running','settling')",
                (match_id,),
            ).fetchone():
                raise ValueError("执行任务当前 attempt 禁止从通用删除入口移除")
            _delete_social_target(c, "match", match_id)
            cur = c.execute(f"DELETE FROM {tbl} WHERE id=?", (match_id,))
            deleted = cur.rowcount > 0
            if deleted:
                c.execute("DELETE FROM matches_index WHERE id=?", (match_id,))
                c.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
                c.execute(
                    "DELETE FROM match_rating_policies WHERE match_id=?", (match_id,)
                )
                c.execute(
                    "DELETE FROM match_rating_settlements WHERE match_id=?",
                    (match_id,),
                )
            return deleted

    def list_matches(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        owner_id: int | None = None,
        bot_id: int | None = None,
        *,
        contest_id: int | None = None,
        game_id: str | None = None,
        has_technical_incidents: bool | None = None,
    ) -> list[dict]:
        """列对局；可跨游戏并按归一化后的 Bot 技术故障过滤。"""
        with self._tx() as c:
            join_bots = (
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id "
                "LEFT JOIN users ua ON ba.owner_id=ua.id "
                "LEFT JOIN users ub ON bb.owner_id=ub.id "
                "LEFT JOIN users hu ON m.human_user_id=hu.id"
            )
            sel = (
                "m.*, ba.name AS bot_a_name, bb.name AS bot_b_name, "
                "ba.display_name AS bot_a_display, "
                "bb.display_name AS bot_b_display, "
                "ua.username AS bot_a_owner_name, "
                "ua.display_name AS bot_a_owner_display, "
                "ub.username AS bot_b_owner_name, "
                "ub.display_name AS bot_b_owner_display, "
                "hu.username AS human_user_name, "
                "hu.display_name AS human_user_display, "
                f"{_contest_expected_duplicate_projection_sql('m')} "
                "AS _contest_expected_duplicate, "
                f"{_contest_require_frozen_duplicate_projection_sql('m')} "
                "AS _contest_require_frozen_duplicate, "
                f"{_contest_stage_config_projection_sql('m')} "
                "AS _contest_stage_config_json, "
                f"{_technical_incident_projection_sql('m')} "
                "AS _replay_incident_events_json"
            )
            where_parts: list[str] = []
            params: list[Any] = []
            if owner_id is not None:
                where_parts.append("m.owner_id=?")
                params.append(owner_id)
            if bot_id is not None:
                where_parts.append("(m.bot_a_id=? OR m.bot_b_id=?)")
                params.extend([bot_id, bot_id])
            if contest_id is not None:
                where_parts.append("m.contest_id=?")
                params.append(contest_id)
            if status:
                where_parts.append("m.status=?")
                params.append(status)
            if game_id:
                where_parts.append("m.game_id=?")
                params.append(game_id)
            if has_technical_incidents is not None:
                predicate = _technical_incident_filter_sql("m")
                where_parts.append(
                    predicate if has_technical_incidents else f"NOT {predicate}"
                )
            where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

            if game_id:
                # 单表查询
                tbl = _matches_table(game_id)
                sql = f"SELECT {sel} FROM {tbl} m {join_bots}{where_sql}"
                sql += " ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                return [_parse_match_json_cols(_row(r)) for r in c.execute(sql, params)]

            # 跨游戏：UNION ALL 三表，外层排序+分页
            subselects = []
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                subselects.append(
                    f"SELECT {sel} FROM {tbl} m {join_bots}{where_sql}"
                )
            union = " UNION ALL ".join(subselects)
            # UNION 后参数要按子查询数（=已注册游戏数）复制，每子查询一份 where 参数。
            # 不得硬编码 * 3——新增第 4 游戏会触发 Incorrect number of bindings。
            all_params = params * len(_all_game_ids())
            sql = f"SELECT * FROM ({union}) ORDER BY created_at DESC LIMIT ? OFFSET ?"
            all_params.extend([limit, offset])
            return [_parse_match_json_cols(_row(r)) for r in c.execute(sql, all_params)]

    def contest_has_active_matches(self, contest_id: int) -> bool:
        """赛事是否仍有 pending/running 对局（跨所有已注册游戏表）。"""
        with self._tx() as c:
            for gid in _all_game_ids():
                table = _matches_table(gid)
                row = c.execute(
                    f"SELECT 1 FROM {table} WHERE contest_id=? "
                    "AND status IN (?,?) LIMIT 1",
                    (contest_id, STATUS_PENDING, STATUS_RUNNING),
                ).fetchone()
                if row:
                    return True
            return False

    def list_liked_top_matches(self, limit: int = 10) -> list[dict]:
        """对局点赞排行榜（跨三表 UNION ALL，likes_count>0 的已完成对局）。"""
        lim = max(1, min(limit, 50))
        with self._tx() as c:
            sel = (
                "m.id, m.game_id, m.status, m.winner, m.reason, "
                "m.technical_loss, m.result, m.match_config, m.likes_count, "
                "m.views_count, m.created_at, m.match_type, m.contest_id, "
                "m.bot_a_id, m.bot_b_id, m.human_user_id, m.human_seat, "
                "ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
                "ua.username AS bot_a_owner_name, ua.display_name AS bot_a_owner_display, "
                "ub.username AS bot_b_owner_name, ub.display_name AS bot_b_owner_display, "
                "hu.username AS human_user_name, hu.display_name AS human_user_display, "
                f"{_contest_expected_duplicate_projection_sql('m')} "
                "AS _contest_expected_duplicate, "
                f"{_contest_require_frozen_duplicate_projection_sql('m')} "
                "AS _contest_require_frozen_duplicate, "
                f"{_contest_stage_config_projection_sql('m')} "
                "AS _contest_stage_config_json"
            )
            join = (
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id "
                "LEFT JOIN users ua ON ba.owner_id=ua.id "
                "LEFT JOIN users ub ON bb.owner_id=ub.id "
                "LEFT JOIN users hu ON m.human_user_id=hu.id"
            )
            where = "WHERE m.status='completed' AND m.likes_count > 0"
            subs = []
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                subs.append(f"SELECT {sel} FROM {tbl} m {join} {where}")
            union = " UNION ALL ".join(subs)
            sql = f"SELECT * FROM ({union}) ORDER BY likes_count DESC, views_count DESC LIMIT ?"
            return [
                _parse_match_json_cols(_row(r))
                for r in c.execute(sql, (lim,))
            ]

    # ── match_replays ─────────────────────────────────────────

    def upsert_replay(
        self,
        match_id: str,
        events_json: str = "[]",
    ) -> None:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO match_replays(match_id, events_json, updated_at) "
                "VALUES(?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET "
                "events_json=excluded.events_json, "
                "updated_at=excluded.updated_at",
                (match_id, events_json, _now()),
            )

    save_replay = upsert_replay

    def get_replay(self, match_id: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM match_replays WHERE match_id=?", (match_id,)
                ).fetchone()
            )

    def get_public_replay(
        self,
        match_id: str,
        *,
        human_viewer_seat: int | None = None,
    ) -> dict | None:
        """Replay for REST/SSE viewers; terminal derives from the match row.

        Read match + replay in one SQLite snapshot so a viewer cannot observe a
        completed/aborted row paired with a terminal from the previous state.
        Internal replay storage remains unchanged.
        """
        with self._tx() as c:
            c.execute("BEGIN")
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            match_row = _row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            )
            if match_row is None:
                # matches_index is a locator, not proof that the physical match
                # still exists.  Fail closed on a drifted/orphan index row.
                return None
            match = _parse_match_json_cols(match_row)
            replay = _row(
                c.execute(
                    "SELECT * FROM match_replays WHERE match_id=?", (match_id,)
                ).fetchone()
            )
            return _sanitize_public_replay(
                replay,
                match,
                human_viewer_seat=human_viewer_seat,
            )

    # ── 私有 Bot debug sidecar ───────────────────────────────

    def _match_debug_access(
        self,
        c: sqlite3.Connection,
        match_id: str,
        *,
        user_id: int,
        is_admin: bool,
    ) -> dict[str, bool]:
        """在调用方事务内执行唯一一份 debug 授权规则。"""
        tbl = self._match_table_of(c, match_id)
        if not tbl:
            return {"found": False, "allowed": False}
        match = c.execute(
            f"SELECT m.status,m.match_type,m.contest_id,"
            "ba.owner_id AS bot_a_owner_id,bb.owner_id AS bot_b_owner_id "
            f"FROM {tbl} m "
            "LEFT JOIN bots ba ON ba.id=m.bot_a_id "
            "LEFT JOIN bots bb ON bb.id=m.bot_b_id "
            "WHERE m.id=?",
            (match_id,),
        ).fetchone()
        if not match:
            return {"found": False, "allowed": False}

        terminal = match["status"] in (STATUS_COMPLETED, STATUS_ABORTED)
        allowed = False
        if terminal:
            if is_admin:
                allowed = True
            elif match["match_type"] == TYPE_HUMAN:
                allowed = False
            elif (
                match["match_type"] == TYPE_CONTEST
                or match["contest_id"] is not None
            ):
                # 赛事身份或外键任一侧异常都 fail closed。不能把
                # ``TYPE_CONTEST + contest_id=NULL``（例如赛事删除后的
                # ON DELETE SET NULL / 旧库漂移）误当普通对局，绕过整赛终态闸门。
                contest = None
                if (
                    match["match_type"] == TYPE_CONTEST
                    and match["contest_id"] is not None
                ):
                    contest = c.execute(
                        "SELECT organizer_id,status FROM contests WHERE id=?",
                        (match["contest_id"],),
                    ).fetchone()
                if contest and contest["organizer_id"] == user_id:
                    allowed = True
                elif contest and contest["status"] in (
                    CONTEST_FINISHED,
                    CONTEST_CANCELLED,
                ):
                    allowed = user_id in {
                        match["bot_a_owner_id"],
                        match["bot_b_owner_id"],
                    }
            else:
                allowed = user_id in {
                    match["bot_a_owner_id"],
                    match["bot_b_owner_id"],
                }
        return {"found": True, "allowed": allowed}

    def can_read_match_debug(
        self,
        match_id: str,
        *,
        user_id: int,
        is_admin: bool,
    ) -> dict[str, bool]:
        """只做授权探测；不加载任何私有 debug 内容。"""
        with self._tx() as c:
            c.execute("BEGIN")
            return self._match_debug_access(
                c,
                match_id,
                user_id=user_id,
                is_admin=is_admin,
            )

    def replace_match_debug(
        self,
        match_id: str,
        entries: list[dict[str, Any]],
        *,
        dropped_count: int = 0,
    ) -> bool:
        """在 Bot-vs-Bot 对局终态后原子替换整场调试批次。

        运行中和人类对局 fail closed；调用方的写入失败只应记录到私有日志，
        不得回滚已经提交的对局结果。单条/整场硬上限由 collector 与表 CHECK
        双重约束。
        """
        now = _now()
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return False
            match = c.execute(
                f"SELECT status,match_type,bot_a_id,bot_b_id FROM {tbl} WHERE id=?",
                (match_id,),
            ).fetchone()
            if (
                not match
                or match["status"] not in (STATUS_COMPLETED, STATUS_ABORTED)
                or match["match_type"] == TYPE_HUMAN
            ):
                return False

            if len(entries) > MATCH_DEBUG_MAX_ENTRIES_PER_MATCH:
                raise ValueError("Bot debug 超过单场条数上限")
            normalized: list[tuple[int, int, int, str, int]] = []
            seen: set[tuple[int, int, int]] = set()
            seat_counts = [0, 0]
            seat_bytes = [0, 0]
            total_bytes = 0
            for entry in entries:
                seat = int(entry["seat"])
                turn = int(entry["turn"])
                leg = int(entry.get("leg", -1))
                debug_json = entry["debug_json"]
                if seat not in (0, 1) or turn < 1 or leg < -1:
                    raise ValueError("Bot debug 座位/回合/leg 不合法")
                identity = (seat, turn, leg)
                if identity in seen:
                    raise ValueError("Bot debug 座位/回合/leg 重复")
                seen.add(identity)
                if not isinstance(debug_json, str):
                    raise ValueError("Bot debug 必须是已清洗 JSON 文本")
                # 先由 Python 拒绝非 JSON，再由表 json_valid CHECK 作持久层
                # 第二道闸门。容量以实际 UTF-8 长度计算，绝不信任调用方
                # 传入的 size_bytes。
                json.loads(debug_json)
                actual_size = len(debug_json.encode("utf-8"))
                if not 1 <= actual_size <= MATCH_DEBUG_MAX_ENTRY_BYTES:
                    raise ValueError("Bot debug 超过单条容量上限")
                if int(entry.get("size_bytes", actual_size)) != actual_size:
                    raise ValueError("Bot debug size_bytes 与实际内容不一致")
                seat_counts[seat] += 1
                seat_bytes[seat] += actual_size
                total_bytes += actual_size
                if (
                    seat_counts[seat] > MATCH_DEBUG_MAX_ENTRIES_PER_SEAT
                    or seat_bytes[seat] > MATCH_DEBUG_MAX_BYTES_PER_SEAT
                ):
                    raise ValueError("Bot debug 超过单座位上限")
                if total_bytes > MATCH_DEBUG_MAX_BYTES_PER_MATCH:
                    raise ValueError("Bot debug 超过单场容量上限")
                normalized.append((seat, turn, leg, debug_json, actual_size))
            c.execute(
                "INSERT INTO match_debug_sessions("
                "match_id,entry_count,total_bytes,dropped_count,created_at,updated_at"
                ") VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET "
                "entry_count=excluded.entry_count,total_bytes=excluded.total_bytes,"
                "dropped_count=excluded.dropped_count,updated_at=excluded.updated_at",
                (
                    match_id,
                    len(entries),
                    total_bytes,
                    max(0, int(dropped_count)),
                    now,
                    now,
                ),
            )
            c.execute("DELETE FROM match_debug_entries WHERE match_id=?", (match_id,))
            for seat, turn, leg, debug_json, actual_size in normalized:
                bot_id = match["bot_a_id"] if seat == 0 else match["bot_b_id"]
                c.execute(
                    "INSERT INTO match_debug_entries("
                    "match_id,bot_id,seat,turn,leg,debug_json,size_bytes,created_at"
                    ") VALUES(?,?,?,?,?,?,?,?)",
                    (
                        match_id,
                        bot_id,
                        seat,
                        turn,
                        leg,
                        debug_json,
                        actual_size,
                        now,
                    ),
                )
            return True

    def get_match_debug_for_user(
        self,
        match_id: str,
        *,
        user_id: int,
        is_admin: bool,
    ) -> dict[str, Any]:
        """在一个 SQLite 快照内完成权限判定与私有内容读取。

        返回 ``found/allowed``，拒绝响应不携带调试记录是否存在的信息。
        非赛事双方 Bot owner 在单场终态后可见；赛事 owner 必须等整个赛事
        终态，赛事组织者与管理员只需该单场终态。人类对局仅管理员可审计。
        """
        with self._tx() as c:
            c.execute("BEGIN")
            access = self._match_debug_access(
                c,
                match_id,
                user_id=user_id,
                is_admin=is_admin,
            )
            if not access["allowed"]:
                return access

            session = c.execute(
                "SELECT entry_count,total_bytes,dropped_count,updated_at "
                "FROM match_debug_sessions WHERE match_id=?",
                (match_id,),
            ).fetchone()
            rows = c.execute(
                "SELECT seat,turn,leg,debug_json FROM match_debug_entries "
                "WHERE match_id=? ORDER BY seat,leg,turn,id",
                (match_id,),
            ).fetchall()
            entries: list[dict[str, Any]] = []
            for row in rows:
                entries.append(
                    {
                        "seat": int(row["seat"]),
                        "turn": int(row["turn"]),
                        "leg": None if int(row["leg"]) < 0 else int(row["leg"]),
                        "debug": json.loads(row["debug_json"]),
                    }
                )
            return {
                **access,
                "entries": entries,
                "entry_count": int(session["entry_count"]) if session else 0,
                "total_bytes": int(session["total_bytes"]) if session else 0,
                "dropped_count": int(session["dropped_count"]) if session else 0,
                "updated_at": session["updated_at"] if session else None,
            }

    def get_public_replay_payload(
        self,
        match_id: str,
        *,
        human_viewer_seat: int | None = None,
    ) -> dict | None:
        """Return structured public replay data without double JSON encoding.

        The match row and replay are read in one SQLite snapshot so the
        canonical terminal always agrees with the authoritative status.  This
        method deliberately returns only replay transport fields; match
        metadata remains on ``GET /api/matches/{id}``.
        """
        with self._tx() as c:
            c.execute("BEGIN")
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            match_row = _row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            )
            if match_row is None:
                # A stale locator must not turn into a synthetic empty replay.
                return None
            match = _parse_match_json_cols(match_row)
            replay = _row(
                c.execute(
                    "SELECT match_id, events_json, updated_at "
                    "FROM match_replays WHERE match_id=?",
                    (match_id,),
                ).fetchone()
            )
            events = _sanitize_public_replay_events(
                replay,
                match,
                human_viewer_seat=human_viewer_seat,
            )
            return {
                "match_id": match_id,
                "events": events,
                "event_count": len(events),
                "updated_at": (replay or {}).get("updated_at"),
            }

    def get_match_record_source(self, match_id: str) -> dict[str, Any] | None:
        """Read detailed match identity and its finalized public replay atomically.

        Match completion commits before the orchestrator's best-effort terminal
        replay flush.  A record download therefore cannot infer finality from
        ``matches_*.status`` alone: during that narrow window it would otherwise
        export an older live prefix plus a synthesized terminal.  This method
        reads the participant joins, raw replay and canonical public replay in
        one SQLite snapshot and reports finality only when the persisted JSON
        array itself ends in the terminal type required by the match row.

        Raw replay JSON never leaves this Store boundary.
        """
        with self._tx() as c:
            c.execute("BEGIN")
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            sel = (
                "m.*, "
                "ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
                "ua.username AS bot_a_owner_name, ua.display_name AS bot_a_owner_display, "
                "ub.username AS bot_b_owner_name, ub.display_name AS bot_b_owner_display, "
                "hu.username AS human_user_name, hu.display_name AS human_user_display, "
                f"{_technical_incident_projection_sql('m')} "
                "AS _replay_incident_events_json"
            )
            row = c.execute(
                f"SELECT {sel} FROM {tbl} m "
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id "
                "LEFT JOIN users ua ON ba.owner_id=ua.id "
                "LEFT JOIN users ub ON bb.owner_id=ub.id "
                "LEFT JOIN users hu ON m.human_user_id=hu.id "
                "WHERE m.id=?",
                (match_id,),
            ).fetchone()
            match = _parse_match_json_cols(_row(row))
            if match is None:
                # A stale locator is not proof that the physical match exists.
                return None
            expected_game_id = tbl.removeprefix("matches_")
            if match.get("game_id") != expected_game_id:
                raise ValueError("对局行 game_id 与定位表/物理表不一致")
            self._attach_rating_settlement_state_tx(c, match)

            replay = _row(
                c.execute(
                    "SELECT match_id,events_json,updated_at "
                    "FROM match_replays WHERE match_id=?",
                    (match_id,),
                ).fetchone()
            )
            raw_events: Any = None
            raw_json = (replay or {}).get("events_json")
            if isinstance(raw_json, str):
                try:
                    parsed = json.loads(raw_json)
                except (TypeError, ValueError):
                    parsed = None
                if isinstance(parsed, list):
                    raw_events = parsed

            expected_terminal = {
                STATUS_COMPLETED: "match_end",
                STATUS_ABORTED: "error",
            }.get(match.get("status"))
            finalized = bool(
                expected_terminal
                and raw_events
                and isinstance(raw_events[-1], dict)
                and raw_events[-1].get("type") == expected_terminal
            )
            events = _sanitize_public_replay_events(replay, match)
            return {
                "match": match,
                "replay": {
                    "match_id": match_id,
                    "events": events,
                    "event_count": len(events),
                    "updated_at": (replay or {}).get("updated_at"),
                },
                "replay_finalized": finalized,
            }

    # ── ratings（per-game：PK = bot_id + game_id，全面解耦 PR3）─────────

    def _bot_game_id(self, c, bot_id: int) -> str:
        """取 bot 绑定的 game_id；Bot/字段缺失或未知时明确失败。"""
        row = c.execute(
            "SELECT game_id FROM bots WHERE id=?", (bot_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"bot 不存在: {bot_id}")
        return _registered_game_id(row["game_id"])

    def _rating_game_id(self, c, bot_id: int, game_id: str | None) -> str:
        return (
            self._bot_game_id(c, bot_id)
            if game_id is None
            else _registered_game_id(game_id)
        )

    def ensure_rating(self, bot_id: int, *, game_id: str | None = None) -> dict:
        """确保 (bot_id, game_id) 评分行存在。game_id 缺省取 bot 的 game_id。"""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            gid = self._rating_game_id(c, bot_id, game_id)
            existing = c.execute(
                "SELECT * FROM ratings WHERE bot_id=? AND game_id=?", (bot_id, gid)
            ).fetchone()
            if existing:
                return _row(existing)
            projection_guard = self._rating_projection_mutation_guard_tx(c)
            c.execute(
                "INSERT INTO ratings(bot_id, game_id) VALUES(?, ?)",
                (bot_id, gid),
            )
            self._advance_rating_projection_state_tx(c, projection_guard)
            return _row(
                c.execute(
                    "SELECT * FROM ratings WHERE bot_id=? AND game_id=?",
                    (bot_id, gid),
                ).fetchone()
            )

    def get_rating(self, bot_id: int, *, game_id: str | None = None) -> dict | None:
        """取 (bot_id, game_id) 评分行。game_id 缺省取 bot 的 game_id。"""
        with self._tx() as c:
            if game_id is None:
                bot = c.execute(
                    "SELECT game_id FROM bots WHERE id=?", (bot_id,)
                ).fetchone()
                if bot is None:
                    return None
                gid = _registered_game_id(bot["game_id"])
            else:
                gid = _registered_game_id(game_id)
            return _row(
                c.execute(
                    "SELECT * FROM ratings WHERE bot_id=? AND game_id=?",
                    (bot_id, gid),
                ).fetchone()
            )

    def update_rating_row(
        self, bot_id: int, *, game_id: str | None = None, **fields: Any
    ) -> dict | None:
        """更新 (bot_id, game_id) 评分行；不存在则建。game_id 缺省取 bot 的 game_id。

        累加字段（wins/losses/draws/delta_total/matches_played）用 SQL 原子
        ``field = field + ?``（传入增量），防并发 lost-update（审计 P1：同 bot
        并发两局时快照+增量会丢一次）。其余字段（rating/rd/vol/last_played_at）
        是绝对赋值。
        """
        allowed = {
            "rating",
            "rd",
            "vol",
            "wins",
            "losses",
            "draws",
            "delta_total",
            "matches_played",
            "last_played_at",
        }
        # 累加字段：传增量，SQL 原子加（防 lost-update）
        accum = {"wins", "losses", "draws", "delta_total", "matches_played"}
        sets = [
            f"{k} = {k} + ?" if k in accum else f"{k}=?"
            for k in fields
            if k in allowed
        ]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            gid = self._rating_game_id(c, bot_id, game_id)
            existing = c.execute(
                "SELECT bot_id FROM ratings WHERE bot_id=? AND game_id=?",
                (bot_id, gid),
            ).fetchone()
            if not existing:
                c.execute(
                    "INSERT INTO ratings(bot_id, game_id) VALUES(?, ?)",
                    (bot_id, gid),
                )
            if sets:
                vals.extend([bot_id, gid])
                c.execute(
                    f"UPDATE ratings SET {','.join(sets)} WHERE bot_id=? AND game_id=?",
                    vals,
                )
            return _row(
                c.execute(
                    "SELECT * FROM ratings WHERE bot_id=? AND game_id=?",
                    (bot_id, gid),
                ).fetchone()
            )

    def apply_match_ratings_atomic(
        self,
        bot_a_id: int,
        bot_b_id: int,
        *,
        game_id: str,
        rating_a: tuple[float, float, float],
        rating_b: tuple[float, float, float],
        winner: int | None,
        delta_a: int,
        delta_b: int,
        reason: str = "",
        settlement_id: str | None = None,
    ) -> bool:
        """恰好一次地落双边 rating/history、pair_stats 与结算凭据。

        调用方已按 bot 获取评分锁并用同一快照算出 Glicko 新值；本接口把所有
        持久化副作用收进一个 SQLite 事务，任一步失败都会整体回滚（包括最先
        claim 的 settlement marker，因此后续可重试）。同一 ``settlement_id``
        已存在时不产生任何评分副作用并返回 False。

        ``settlement_id=None`` 保留旧调用方的行为（不做幂等 claim）；正常对局
        路径必须传 match_id。同 bot 自博弈只提交 marker、不更新天梯。
        """
        gid = _registered_game_id(game_id)
        wa = int(winner == 0)
        la = int(winner == 1)
        da = int(winner is None)
        wb, lb, db = la, wa, da
        now = _now()
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            projection_guard: _RatingProjectionMutationGuard | None = None
            if settlement_id is not None:
                # Check idempotency before computing/reserving an order.  SQLite
                # runs BEFORE INSERT triggers even for INSERT OR IGNORE, so a
                # duplicate must never reach the strict next-order trigger.
                if c.execute(
                    "SELECT 1 FROM match_rating_settlements WHERE match_id=?",
                    (settlement_id,),
                ).fetchone():
                    return False
                projection_guard = self._rating_projection_mutation_guard_tx(c)
                settlement_order = self._rating_settlement_order_for_insert_tx(
                    c, settlement_id
                )
                c.execute(
                    "INSERT INTO match_rating_settlements("
                    "match_id,settled_at,settled_order) VALUES(?,?,?)",
                    (settlement_id, now, settlement_order),
                )

            # 自博弈没有可用于 Glicko 的对手信息。marker 仍须落盘，否则启动
            # 恢复会在每次重启反复扫描同一 completed 对局。
            if bot_a_id == bot_b_id:
                if projection_guard is not None:
                    self._advance_rating_projection_state_tx(c, projection_guard)
                return True

            for bot_id in (bot_a_id, bot_b_id):
                c.execute(
                    "INSERT OR IGNORE INTO ratings(bot_id, game_id) VALUES(?, ?)",
                    (bot_id, gid),
                )
            for bot_id, values, wins, losses, draws, delta in (
                (bot_a_id, rating_a, wa, la, da, delta_a),
                (bot_b_id, rating_b, wb, lb, db, delta_b),
            ):
                c.execute(
                    "UPDATE ratings SET rating=?, rd=?, vol=?, "
                    "wins=wins+?, losses=losses+?, draws=draws+?, "
                    "delta_total=delta_total+?, matches_played=matches_played+1, "
                    "last_played_at=? WHERE bot_id=? AND game_id=?",
                    (
                        values[0], values[1], values[2], wins, losses, draws,
                        delta, now, bot_id, gid,
                    ),
                )
                row = c.execute(
                    "SELECT rating, rd, vol, matches_played FROM ratings "
                    "WHERE bot_id=? AND game_id=?",
                    (bot_id, gid),
                ).fetchone()
                c.execute(
                    "INSERT INTO rating_history(bot_id, game_id, rating, rd, vol, "
                    "matches_played, reason, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        bot_id, gid, row["rating"], row["rd"], row["vol"],
                        row["matches_played"], reason, now,
                    ),
                )
                c.execute(
                    "DELETE FROM rating_history "
                    "WHERE bot_id=? AND game_id=? AND id NOT IN "
                    "(SELECT id FROM rating_history WHERE bot_id=? AND game_id=? "
                    "ORDER BY id DESC LIMIT 200)",
                    (bot_id, gid, bot_id, gid),
                )

            lo, hi = sorted((bot_a_id, bot_b_id))
            if bot_a_id == lo:
                aw, al, dd = wa, la, da
            else:
                aw, al, dd = wb, lb, db
            c.execute(
                "INSERT INTO pair_stats(bot_a_id, bot_b_id, samples, last_played_at, "
                "a_wins, a_losses, draws) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(bot_a_id, bot_b_id) DO UPDATE SET "
                "samples=pair_stats.samples+excluded.samples, "
                "last_played_at=excluded.last_played_at, "
                "a_wins=pair_stats.a_wins+excluded.a_wins, "
                "a_losses=pair_stats.a_losses+excluded.a_losses, "
                "draws=pair_stats.draws+excluded.draws",
                (lo, hi, 1, now, aw, al, dd),
            )
            if projection_guard is not None:
                self._advance_rating_projection_state_tx(c, projection_guard)
            return True

    def mark_match_rating_settled(
        self,
        match_id: str,
    ) -> bool:
        """原子写入无评分副作用的结算 marker；已存在返回 False。

        仅用于 completed 行已失去 Bot 外键、无法重算评分的收敛场景。自博弈
        仍走 :meth:`apply_match_ratings_atomic` 的同 bot 分支。
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if c.execute(
                "SELECT 1 FROM match_rating_settlements WHERE match_id=?",
                (match_id,),
            ).fetchone():
                return False
            policy = c.execute(
                "SELECT rated FROM match_rating_policies WHERE match_id=?",
                (match_id,),
            ).fetchone()
            projection_guard = self._rating_projection_mutation_guard_tx(c)
            settlement_order = self._rating_settlement_order_for_insert_tx(c, match_id)
            c.execute(
                "INSERT INTO match_rating_settlements("
                "match_id,settled_at,settled_order) VALUES(?,?,?)",
                (match_id, _now(), settlement_order),
            )
            # Neutral markers have no derived rating mutation.  A rated match
            # marked without rating rows (only the deleted-Bot recovery path)
            # intentionally leaves the projection gate stale until offline
            # replay verifies the propagated leaderboard.
            if policy is not None and int(policy["rated"] or 0) == 0:
                self._advance_rating_projection_state_tx(c, projection_guard)
            return True

    def is_match_rating_settled(self, match_id: str) -> bool:
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM match_rating_settlements WHERE match_id=?",
                (match_id,),
            ).fetchone() is not None

    def list_unsettled_completed_rating_matches(self) -> list[dict]:
        """列出需启动补算的 completed Bot 对局（跨游戏、排除赛事/人类）。"""
        with self._tx() as c:
            matches: list[dict] = []
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                rows = c.execute(
                    f"SELECT m.*,policy.settled_order AS _rating_settled_order "
                    f"FROM {tbl} m "
                    "JOIN match_rating_policies policy ON policy.match_id=m.id "
                    "JOIN rating_pool_state pool ON pool.game_id=policy.game_id "
                    "AND pool.active_pool_id=policy.rating_pool_id "
                    "LEFT JOIN match_rating_settlements settled ON settled.match_id=m.id "
                    "WHERE m.status=? AND m.match_type NOT IN (?,?) "
                    "AND settled.match_id IS NULL "
                    "AND policy.settled_order IS NOT NULL "
                    "ORDER BY policy.settled_order,m.id",
                    (STATUS_COMPLETED, TYPE_CONTEST, TYPE_HUMAN),
                ).fetchall()
                matches.extend(
                    _parse_match_json_cols(_row(row)) for row in rows
                )
            matches.sort(
                key=lambda m: (
                    int(m.get("_rating_settled_order") or 0),
                    m.get("id") or "",
                )
            )
            return matches

    def list_leaderboard(
        self, *, game_id: str, limit: int = 50,
        page: int | None = None, per_page: int = 50,
    ) -> dict:
        """返回单一游戏的公开排名、计分样本和紧凑概览。

        Glicko-2 评分池按游戏隔离，因此 ``game_id`` 是不可省略的维度；Store
        也必须 fail closed，不能只依赖 API 层阻止跨游戏混排。最近对局只接受
        同时存在于 rating_history、matches_index 和对应游戏物理表的 completed
        对局，避免损坏/漂移索引生成错误链接。
        """
        gid = _registered_game_id(game_id)
        match_table = _matches_table(gid)
        with self._tx() as c:
            # rating_delta = 当前 rating - 上一条历史评分（升降趋势）；无历史则 NULL
            # ratings/rating_history 现按 (bot_id, game_id) 复合键——所有 join/subquery
            # 都加 game_id 谓词，绝不把其他游戏的历史当作“上次变化”。
            #
            # SELECT 中含 prev_rating 与 recent-match 子查询；总数/分段概览使用
            # 独立 eligibility 查询，行查询再追加窗口名次与分页，避免子查询影响 COUNT。
            eligibility_from = (
                "FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "WHERE b.is_active=1 AND b.is_ranked=1 "
                "AND b.format=? AND b.os=? AND b.arch=? "
                "AND b.game_id=?"
            )
            eligibility_params: tuple[Any, ...] = (
                SUPPORTED_BINARY_FORMAT,
                SUPPORTED_BINARY_OS,
                SUPPORTED_BINARY_ARCH,
                gid,
            )
            ranking_min_matches = max(1, int(RANKING_MIN_RATED_MATCHES))
            eligible_condition = f"r.matches_played >= {ranking_min_matches}"

            summary_row = c.execute(
                "SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN {eligible_condition} THEN 1 ELSE 0 END) AS eligible, "
                f"SUM(CASE WHEN {eligible_condition} THEN 0 ELSE 1 END) AS sample, "
                f"MAX(r.last_played_at) AS last_rated_at {eligibility_from}",
                eligibility_params,
            ).fetchone()
            summary = {
                "total": int(summary_row["total"] or 0),
                "eligible": int(summary_row["eligible"] or 0),
                "sample": int(summary_row["sample"] or 0),
                "last_rated_at": summary_row["last_rated_at"],
            }
            cutoff_30d = (datetime.now() - timedelta(days=30)).isoformat(
                timespec="seconds"
            )

            # last_rh 只接纳三处一致的 completed 对局。rating_history.reason 中
            # 的非 match 原因、错误 game_id 索引和缺失物理行都会被自然跳过。
            opponent_cte = (
                "WITH opponent_counts AS ("
                " SELECT bot_id, COUNT(DISTINCT opponent_id) AS unique_opponents FROM ("
                "  SELECT bot_a_id AS bot_id, bot_b_id AS opponent_id FROM pair_stats "
                "  UNION ALL SELECT bot_b_id AS bot_id, bot_a_id AS opponent_id FROM pair_stats"
                " ) GROUP BY bot_id"
                ") "
            )
            item_from = (
                f"FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "LEFT JOIN opponent_counts oc ON oc.bot_id=r.bot_id "
                "LEFT JOIN rating_history last_rh ON last_rh.id=("
                " SELECT rh.id FROM rating_history rh "
                " JOIN matches_index mi ON mi.id=rh.reason AND mi.game_id=rh.game_id "
                f" JOIN {match_table} lm ON lm.id=mi.id AND lm.game_id=mi.game_id "
                " WHERE rh.bot_id=r.bot_id AND rh.game_id=r.game_id AND lm.status=? "
                " AND (lm.bot_a_id=r.bot_id OR lm.bot_b_id=r.bot_id) "
                " ORDER BY rh.id DESC LIMIT 1"
                ") "
                "WHERE b.is_active=1 AND b.is_ranked=1 "
                "AND b.format=? AND b.os=? AND b.arch=? "
                "AND b.game_id=?"
            )
            item_params: tuple[Any, ...] = (
                cutoff_30d,
                STATUS_COMPLETED,
                *eligibility_params,
            )
            sel = (
                "SELECT r.bot_id, r.rating, r.rd, r.wins, r.losses, "
                "r.draws, r.matches_played AS rated_matches, "
                "b.name AS bot_name, b.display_name AS bot_display, "
                "u.username AS owner_name, "
                "COALESCE(oc.unique_opponents,0) AS unique_opponents, "
                "(SELECT rh.rating FROM rating_history rh "
                " WHERE rh.bot_id=r.bot_id AND rh.game_id=r.game_id "
                " ORDER BY rh.id DESC LIMIT 1 OFFSET 1) AS prev_rating, "
                "(SELECT rh.rating FROM rating_history rh "
                " WHERE rh.bot_id=r.bot_id AND rh.game_id=r.game_id "
                " AND rh.created_at <= ? "
                " ORDER BY rh.created_at DESC, rh.id DESC LIMIT 1) AS baseline_30d, "
                "last_rh.reason AS last_match_id, "
                "last_rh.created_at AS last_match_at, "
                f"ROW_NUMBER() OVER (PARTITION BY CASE WHEN {eligible_condition} "
                "THEN 1 ELSE 0 END ORDER BY r.rating DESC, r.matches_played DESC, "
                "r.bot_id ASC) AS group_rank "
            )
            # 公开排名排在计分样本之前；组内按 rating、场次、bot_id 稳定排序。
            order = (
                f" ORDER BY ({eligible_condition}) DESC, r.rating DESC, "
                "r.matches_played DESC, r.bot_id ASC"
            )
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                off = (max(1, int(page)) - 1) * pp
                sql = f"{opponent_cte}{sel}{item_from}{order} LIMIT ? OFFSET ?"
                rows = [_row(r) for r in c.execute(
                    sql, item_params + (pp, off)
                ).fetchall()]
            else:
                pp = max(1, min(limit, 200))
                sql = f"{opponent_cte}{sel}{item_from}{order} LIMIT ?"
                rows = [_row(r) for r in c.execute(
                    sql, item_params + (pp,)
                )]
            # 计算数值投影（应用层，避免 SQL 嵌套过深）。
            for row in rows:
                prev = row.pop("prev_rating", None)
                baseline_30d = row.pop("baseline_30d", None)
                if prev is not None:
                    row["rating_delta"] = round(row["rating"] - prev, 2)
                else:
                    row["rating_delta"] = None
                row["recent_delta_30d"] = (
                    round(row["rating"] - baseline_30d, 2)
                    if baseline_30d is not None else None
                )
                played = max(0, int(row.get("rated_matches") or 0))
                eligible = played >= ranking_min_matches
                group_rank = row.pop("group_rank", None)
                _attach_numeric_ranking(
                    row,
                    eligible=eligible,
                    rank=int(group_rank or 0) if eligible else None,
                    rank_total=summary["eligible"],
                )

            result: dict[str, Any] = {
                "items": rows,
                "total": summary["total"],
                "summary": summary,
                "game_id": gid,
                "ranking_min_matches": ranking_min_matches,
            }
            if page is not None:
                result.update({
                    "page": max(1, int(page)),
                    "per_page": pp,
                })
            return result

    leaderboard = list_leaderboard

    def least_recently_played(
        self,
        game_id: str | None = None,
        *,
        limit: int = 100,
        stale_since: int | None = None,
        bootstrap_target_matches: int | None = None,
    ) -> list[dict]:
        """按陈旧度返回可对战 bot，供闲时自动对局挑选。

        - stale_since（秒，>0）：只返回 last_played_at 早于 now-stale_since 或从未赛（NULL）的 bot；
          None/0 = 不限。
        - bootstrap_target_matches（>0）：样本数低于内部目标的 bot 排最前，
          其后按陈旧度（NULL 最前，再按时间升序）。
        仅返回 active+public+非内置且有二进制的 bot。
        """
        with self._tx() as c:
            sql = (
                "SELECT r.bot_id, r.rating, r.rd, r.matches_played, r.last_played_at, "
                "b.name AS bot_name, b.game_id, b.binary_path, b.is_active, b.is_builtin "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE b.is_active=1 AND b.is_ranked=1 AND b.is_builtin=0 "
                "AND b.binary_path!='' "
                "AND b.format=? AND b.os=? AND b.arch=?"
            )
            params: list[Any] = [
                SUPPORTED_BINARY_FORMAT,
                SUPPORTED_BINARY_OS,
                SUPPORTED_BINARY_ARCH,
            ]
            if game_id:
                sql += " AND b.game_id=?"
                params.append(game_id)
            if stale_since and stale_since > 0:
                # last_played_at 早于 cutoff 或 NULL。
                # 注意：_now() 用本地时间，SQLite datetime('now') 是 UTC，故在 Python 算 cutoff。
                from datetime import datetime, timedelta
                cutoff = (datetime.now() - timedelta(seconds=int(stale_since))).isoformat(timespec="seconds")
                sql += " AND (r.last_played_at IS NULL OR r.last_played_at < ?)"
                params.append(cutoff)
            # 排序：bootstrap 样本不足的 bot 最前，其后 NULL 最前、再按时间升序
            order = " ORDER BY "
            if bootstrap_target_matches and bootstrap_target_matches > 0:
                order += (
                    f"(r.matches_played < {int(bootstrap_target_matches)}) DESC, "
                )
            order += "r.last_played_at IS NULL DESC, r.last_played_at ASC LIMIT ?"
            sql += order
            params.append(limit)
            return [_row(r) for r in c.execute(sql, params)]

    def count_matches(
        self,
        status: str | None = None,
        *,
        game_id: str | None = None,
        has_technical_incidents: bool | None = None,
    ) -> int:
        """按 status/game_id/Bot 技术故障统计；语义与 list_matches 对齐。"""
        with self._tx() as c:
            where_parts: list[str] = []
            params: list[Any] = []
            if status:
                where_parts.append("status=?")
                params.append(status)
            if has_technical_incidents is not None:
                predicate = _technical_incident_filter_sql("m")
                where_parts.append(
                    predicate if has_technical_incidents else f"NOT {predicate}"
                )
            where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
            total = 0
            gids = [game_id] if game_id else _all_game_ids()
            for gid in gids:
                tbl = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} m{where_sql}", params
                ).fetchone()
                total += int(row[0]) if row else 0
            return total

    def count_bot_matches(self, bot_id: int) -> int:
        """统计某 bot 参与的对局数（跨所有已注册游戏表，bot_a 或 bot_b 均算）。

        供 /api/bots/{id}/matches 分页算 total——list_matches 用 ``(bot_a_id=? OR
        bot_b_id=?)`` 过滤，count 维度需与之对齐。
        """
        with self._tx() as c:
            total = 0
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE bot_a_id=? OR bot_b_id=?",
                    (bot_id, bot_id),
                ).fetchone()
                total += int(row[0]) if row else 0
            return total

    def recover_orphan_matches(self, *, interruption_reason: str) -> int:
        """恢复时清理孤儿对局并持久化调用方提供的精确恢复来源。

        进程重新启动或同进程执行环境恢复前都会先终止旧 Task/Future（尤其
        人类对局的 _human_turns）；这些无对应内存协程的 running 记录不清理会
        永久卡住并发与活跃用户计数。遍历三张 per-game 表清理，返回受影响行数。

        同时清理孤儿 pending 对局：
        - 同时没有 contest_id 与 pairing 引用的非 contest pending
          （challenge/table/ladder/human 等）：恢复边界后已无对应内存
          Task/Future，按同一来源映射为对应 pending reason 后 aborted；
        - contest_id=NULL、无 pairing 引用且 match_type='contest' 的 pending
          （e2e 残留等无主）；

        活跃赛事的 pending contest match 不在本方法粗暴标 aborted：已绑定/
        未绑定的两阶段派发中断由 ``reset_dead_contest_pairings`` 精确删除并重派。
        finished/cancelled 的 Match、pairing 与 replay 是不可变历史，即使旧数据仍
        残留 pending/running 绑定也不得由应用恢复重写。
        """
        interruption_reason = validate_orphan_recovery_reason(
            interruption_reason
        )
        pending_interruption_reason = pending_orphan_recovery_reason(
            interruption_reason
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            n = 0

            def abort_rows(
                table: str,
                rows: list[sqlite3.Row],
                *,
                expected_status: str,
                reason: str,
            ) -> int:
                terminal_at = _now()
                for match_row in rows:
                    match_id = str(match_row["id"])
                    changed = c.execute(
                        f"UPDATE {table} SET status=?,reason=?,ended_at=? "
                        "WHERE id=? AND status=?",
                        (
                            STATUS_ABORTED,
                            reason,
                            terminal_at,
                            match_id,
                            expected_status,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise RuntimeError(
                            "orphan recovery Match CAS failed"
                        )
                    terminal_match = c.execute(
                        f"SELECT * FROM {table} WHERE id=?", (match_id,)
                    ).fetchone()
                    if terminal_match is None:
                        raise RuntimeError("orphan recovery Match disappeared")
                    _finalize_terminal_replay_tx(
                        c,
                        match=terminal_match,
                        updated_at=terminal_at,
                    )
                return len(rows)

            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                running_candidates = c.execute(
                    f"SELECT m.id,m.contest_id FROM {tbl} m WHERE m.status=? "
                    "AND m.id NOT IN (SELECT current_match_id FROM execution_jobs "
                    "WHERE current_match_id IS NOT NULL AND status IN "
                    "('starting','running','settling'))",
                    (STATUS_RUNNING,),
                ).fetchall()
                running_rows = [
                    row
                    for row in running_candidates
                    if _match_recovery_affiliation_tx(
                        c,
                        match_id=str(row["id"]),
                        direct_contest_id=row["contest_id"],
                    )[0]
                    in {"unaffiliated", "active"}
                ]
                n += abort_rows(
                    tbl,
                    running_rows,
                    expected_status=STATUS_RUNNING,
                    reason=interruption_reason,
                )
                # 真正没有 contest_id / pairing 引用的 pending 依赖恢复前的内存
                # task/future，恢复后不可能继续。match_type 只选择稳定审计 reason，
                # 不参与归属；任何赛事关联都留给 pairing 对账按 contest.status 恢复。
                pending_candidates = c.execute(
                    f"SELECT id,contest_id,match_type FROM {tbl} WHERE status=? "
                    "AND id NOT IN (SELECT current_match_id FROM execution_jobs "
                    "WHERE current_match_id IS NOT NULL AND status IN "
                    "('starting','running','settling'))",
                    (STATUS_PENDING,),
                ).fetchall()
                unaffiliated_pending = [
                    row
                    for row in pending_candidates
                    if _match_recovery_affiliation_tx(
                        c,
                        match_id=str(row["id"]),
                        direct_contest_id=row["contest_id"],
                    )[0]
                    == "unaffiliated"
                ]
                non_contest_pending = [
                    row
                    for row in unaffiliated_pending
                    if row["match_type"] != TYPE_CONTEST
                ]
                n += abort_rows(
                    tbl,
                    non_contest_pending,
                    expected_status=STATUS_PENDING,
                    reason=pending_interruption_reason,
                )
                no_contest_pending = [
                    row
                    for row in unaffiliated_pending
                    if row["match_type"] == TYPE_CONTEST
                ]
                n += abort_rows(
                    tbl,
                    no_contest_pending,
                    expected_status=STATUS_PENDING,
                    reason="orphan_pending_no_contest",
                )
            return n

    def reset_dead_contest_pairings(
        self, *, interruption_reason: str
    ) -> int:
        """恢复对账辅助：清理两阶段派发中断留下的死状态。

        1. prepare match 已插入，但进程在 bind pairing 前退出：活跃赛事中会留下
           没有任何 pairing 引用的 pending contest match。这类幽灵对局必须连同
           物理 match 行、matches_index 和 replay 在同一事务内删除。
        2. contest_pairings 里 status='running' 但对应 match 已终态非
           completed（aborted/orphan/pending 或不存在）：复位为 pending +
           match_id=NULL，供 ContestManager.maybe_finish/_dispatch_pending 重派。

        completed 的 pairing 不复位（保留真实比赛结果，防误伤），但 completed/
        aborted Match 都会先从 Match 权威行补齐唯一 replay 终局，避免恢复后日志
        永久停在未完成状态。
        对应 recover_orphan_matches 把 running match 标 aborted 后的赛事善后——
        那些赛事 pairing 仍指 aborted match（_stage_done 不通过 pairing 状态判，而是
        读 match.status，但 _dispatch_pending 只挑 status=pending 且无 match_id 的重派，
        所以 status=running+match_id=aborted 的死 pairing 永远不会被重派 → 赛事卡死）。
        返回重置行数。
        """
        interruption_reason = validate_orphan_recovery_reason(
            interruption_reason
        )
        # pairing bind 已提交、runner 尚未 start 时恢复边界可能中断：该 match 仍 pending。
        # 解绑它时必须在同一事务删除物理 match + index + replay，否则随后重派会留下
        # ghost pending match，阻塞 force-finish 并重复占用数据。
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            recovered = 0
            active_statuses = (
                CONTEST_PUBLISHED,
                CONTEST_RUNNING,
                CONTEST_REST,
            )
            status_marks = ",".join("?" for _ in active_statuses)
            # prepare 成功、bind 前硬崩：match 的 contest_id 已写入，但没有
            # pairing.match_id 指向它。这里只在恢复对账入口调用，内存中已无
            # 可能继续 bind 的 prepared map，因此删除是唯一可恢复收敛。
            for gid in _all_game_ids():
                table = _matches_table(gid)
                ghosts = c.execute(
                    f"SELECT m.id,m.contest_id FROM {table} m "
                    "JOIN contests contest ON contest.id=m.contest_id "
                    "WHERE m.status=? "
                    f"AND contest.status IN ({status_marks}) "
                    "AND contest.showcase_key IS NULL "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM contest_pairings pairing WHERE pairing.match_id=m.id"
                    ") AND NOT EXISTS("
                    "SELECT 1 FROM execution_jobs job WHERE job.current_match_id=m.id "
                    "AND job.status IN ('starting','running','settling'))",
                    (STATUS_PENDING, *active_statuses),
                ).fetchall()
                for ghost in ghosts:
                    match_id = str(ghost["id"])
                    affiliation, authority_contest_id = (
                        _match_recovery_affiliation_tx(
                            c,
                            match_id=match_id,
                            direct_contest_id=ghost["contest_id"],
                        )
                    )
                    if (
                        affiliation != "active"
                        or authority_contest_id != ghost["contest_id"]
                    ):
                        continue
                    _delete_social_target(c, "match", match_id)
                    c.execute(f"DELETE FROM {table} WHERE id=?", (match_id,))
                    c.execute("DELETE FROM matches_index WHERE id=?", (match_id,))
                    c.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
                    c.execute(
                        "DELETE FROM match_rating_policies WHERE match_id=?",
                        (match_id,),
                    )
                    recovered += 1

            pairings = c.execute(
                "SELECT pairing.id,pairing.match_id,pairing.contest_id "
                "FROM contest_pairings pairing "
                "JOIN contests contest ON contest.id=pairing.contest_id "
                "WHERE pairing.status=? AND pairing.match_id IS NOT NULL "
                f"AND contest.status IN ({status_marks}) "
                "AND contest.showcase_key IS NULL "
                "AND NOT EXISTS(SELECT 1 FROM execution_jobs job "
                "WHERE job.current_match_id=pairing.match_id "
                "AND job.status IN ('starting','running','settling'))",
                (STATUS_RUNNING, *active_statuses),
            ).fetchall()
            for pairing in pairings:
                match_id = str(pairing["match_id"])
                indexed = c.execute(
                    "SELECT game_id FROM matches_index WHERE id=?", (match_id,)
                ).fetchone()
                table = _matches_table(indexed["game_id"]) if indexed else None
                match = (
                    c.execute(
                        f"SELECT * FROM {table} WHERE id=?", (match_id,)
                    ).fetchone()
                    if table else None
                )
                affiliation, authority_contest_id = (
                    _match_recovery_affiliation_tx(
                        c,
                        match_id=match_id,
                        direct_contest_id=(
                            match["contest_id"] if match is not None else None
                        ),
                    )
                )
                if (
                    affiliation != "active"
                    or authority_contest_id != pairing["contest_id"]
                ):
                    continue
                if match and match["status"] in {
                    STATUS_COMPLETED,
                    STATUS_ABORTED,
                }:
                    _finalize_terminal_replay_tx(
                        c,
                        match=match,
                        updated_at=_now(),
                    )
                if match and match["status"] == STATUS_COMPLETED:
                    continue
                cur = c.execute(
                    "UPDATE contest_pairings SET status=?, match_id=NULL "
                    "WHERE id=? AND status=? AND match_id=?",
                    (STATUS_PENDING, pairing["id"], STATUS_RUNNING, match_id),
                )
                if cur.rowcount != 1:
                    continue
                recovered += 1
                if not match or not table:
                    continue
                if match["status"] == STATUS_PENDING:
                    _delete_social_target(c, "match", match_id)
                    c.execute(f"DELETE FROM {table} WHERE id=?", (match_id,))
                    c.execute("DELETE FROM matches_index WHERE id=?", (match_id,))
                    c.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
                    c.execute(
                        "DELETE FROM match_rating_policies WHERE match_id=?",
                        (match_id,),
                    )
                elif match["status"] == STATUS_RUNNING:
                    terminal_at = _now()
                    changed = c.execute(
                        f"UPDATE {table} SET status=?, reason=?, "
                        "ended_at=? WHERE id=? AND status=?",
                        (
                            STATUS_ABORTED,
                            interruption_reason,
                            terminal_at,
                            match_id,
                            STATUS_RUNNING,
                        ),
                    )
                    if changed.rowcount != 1:
                        raise RuntimeError(
                            "contest recovery Match CAS failed"
                        )
                    terminal_match = c.execute(
                        f"SELECT * FROM {table} WHERE id=?", (match_id,)
                    ).fetchone()
                    if terminal_match is None:
                        raise RuntimeError(
                            "contest recovery Match disappeared"
                        )
                    _finalize_terminal_replay_tx(
                        c,
                        match=terminal_match,
                        updated_at=terminal_at,
                    )
            return recovered

    def list_contests_by_status(self, statuses: list[str]) -> list[dict]:
        """返回可推进的 active contest；只读演示快照永不进入调度/对账。"""
        if not statuses:
            return []
        with self._tx() as c:
            placeholders = ",".join("?" for _ in statuses)
            rows = c.execute(
                f"SELECT * FROM contests WHERE status IN ({placeholders}) "
                "AND showcase_key IS NULL "
                "ORDER BY id",
                tuple(statuses),
            ).fetchall()
            return [_row(r) for r in rows]

    def list_unready_finished_contests(self) -> list[dict]:
        """Return terminal contests whose durable official ranking is incomplete."""
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM contests WHERE status=? "
                "AND COALESCE(official_results_ready, 0)=0 "
                "AND showcase_key IS NULL ORDER BY id",
                (CONTEST_FINISHED,),
            ).fetchall()
            return [_row(row) for row in rows]

    def upsert_rating(
        self,
        bot_id: int,
        rating: float,
        rd: float,
        vol: float,
        *,
        wins: int = 0,
        losses: int = 0,
        draws: int = 0,
        delta_total: int = 0,
        matches_played: int = 0,
        last_played_at: str | None = None,
    ) -> dict | None:
        return self.update_rating_row(
            bot_id,
            rating=rating,
            rd=rd,
            vol=vol,
            wins=wins,
            losses=losses,
            draws=draws,
            delta_total=delta_total,
            matches_played=matches_played,
            last_played_at=last_played_at or _now(),
        )

    # ── pair_stats ────────────────────────────────────────────

    def upsert_pair_stats(
        self,
        bot_a_id: int,
        bot_b_id: int,
        *,
        a_wins_delta: int = 0,
        a_losses_delta: int = 0,
        draws_delta: int = 0,
    ) -> None:
        """记录双方对战胜负；a_wins/a_losses 从 bot_a 视角计。"""
        outcome_delta = (
            max(0, int(a_wins_delta))
            + max(0, int(a_losses_delta))
            + max(0, int(draws_delta))
        )
        sample_delta = outcome_delta
        with self._tx() as c:
            c.execute(
                "INSERT INTO pair_stats(bot_a_id, bot_b_id, samples, last_played_at, "
                "a_wins, a_losses, draws) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(bot_a_id, bot_b_id) DO UPDATE SET "
                "samples=pair_stats.samples+excluded.samples, "
                "last_played_at=excluded.last_played_at, "
                "a_wins=pair_stats.a_wins+excluded.a_wins, "
                "a_losses=pair_stats.a_losses+excluded.a_losses, "
                "draws=pair_stats.draws+excluded.draws",
                (
                    bot_a_id,
                    bot_b_id,
                    sample_delta,
                    _now(),
                    max(0, a_wins_delta),
                    max(0, a_losses_delta),
                    max(0, draws_delta),
                ),
            )

    def head_to_head(self, bot_a_id: int, bot_b_id: int) -> dict | None:
        """返回 bot_a 视角的对某对手战绩（a_wins/a_losses/draws/samples）。

        pair_stats 以 (min_id, max_id) 规范化存储，读取时按方向还原视角。
        """
        lo, hi = sorted((bot_a_id, bot_b_id))
        with self._tx() as c:
            row = c.execute(
                "SELECT a_wins, a_losses, draws, "
                "(a_wins+a_losses+draws) AS samples, last_played_at "
                "FROM pair_stats WHERE bot_a_id=? AND bot_b_id=?",
                (lo, hi),
            ).fetchone()
            if not row:
                return None
            d = _row(row)
            # 规范化存储时 bot_a = 小 id；若查询的 bot_a 是大 id，则胜负视角翻转
            if bot_a_id == lo:
                return d
            return {
                "a_wins": d["a_losses"],
                "a_losses": d["a_wins"],
                "draws": d["draws"],
                "samples": d["samples"],
                "last_played_at": d["last_played_at"],
            }

    def add_rating_history(
        self,
        bot_id: int,
        rating: float,
        rd: float,
        vol: float,
        matches_played: int,
        reason: str = "",
        *,
        game_id: str | None = None,
    ) -> None:
        """落一条评分快照（per-game），并截断保留每 (bot,game) 最近 N 条（N=200）。"""
        with self._tx() as c:
            gid = self._rating_game_id(c, bot_id, game_id)
            c.execute(
                "INSERT INTO rating_history(bot_id, game_id, rating, rd, vol, "
                "matches_played, reason, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (bot_id, gid, rating, rd, vol, matches_played, reason, _now()),
            )
            # 截断：保留每 (bot, game) 最近 200 条
            c.execute(
                "DELETE FROM rating_history WHERE bot_id=? AND game_id=? AND id NOT IN "
                "(SELECT id FROM rating_history WHERE bot_id=? AND game_id=? "
                "ORDER BY id DESC LIMIT 200)",
                (bot_id, gid, bot_id, gid),
            )

    def list_rating_history(
        self, bot_id: int, *, limit: int = 100, game_id: str | None = None
    ) -> list[dict]:
        """返回评分历史时序（旧→新，per-game），用于画曲线。"""
        with self._tx() as c:
            gid = self._rating_game_id(c, bot_id, game_id)
            rows = c.execute(
                "SELECT id, rating, rd, vol, matches_played, reason, created_at "
                "FROM rating_history WHERE bot_id=? AND game_id=? "
                "ORDER BY id DESC LIMIT ?",
                (bot_id, gid, max(1, min(limit, 500))),
            ).fetchall()
            return [_row(r) for r in reversed(rows)]

    # ── comments（评论）───────────────────────────────────────
    def _social_target_exists_tx(
        self,
        c: sqlite3.Connection,
        target_type: str,
        target_id: str | int,
    ) -> bool:
        """Validate a polymorphic social target on the caller's transaction."""
        tid = str(target_id)
        if target_type == "match":
            table = self._match_table_of(c, tid)
            return bool(
                table
                and c.execute(
                    f"SELECT 1 FROM {table} WHERE id=?", (tid,)
                ).fetchone()
            )
        if target_type in {"bot", "comment"}:
            try:
                numeric_id = int(tid)
            except (TypeError, ValueError):
                return False
            if numeric_id <= 0 or str(numeric_id) != tid:
                return False
            table = "bots" if target_type == "bot" else "comments"
            return c.execute(
                f"SELECT 1 FROM {table} WHERE id=?", (numeric_id,)
            ).fetchone() is not None
        raise ValueError(f"不支持的社交目标类型: {target_type!r}")

    @staticmethod
    def _social_actor_exists_tx(c: sqlite3.Connection, user_id: int) -> bool:
        """Validate the authenticated actor inside the caller's write lock."""
        return c.execute(
            "SELECT 1 FROM users WHERE id=?", (user_id,)
        ).fetchone() is not None

    def social_target_exists(
        self, target_type: str, target_id: str | int
    ) -> bool:
        """Public read-side target check used by comments/likes API routes."""
        if target_type not in LIKE_TARGET_TYPES:
            raise ValueError(f"不支持的社交目标类型: {target_type!r}")
        with self._tx() as c:
            return self._social_target_exists_tx(c, target_type, target_id)

    def add_comment(
        self, user_id: int, target_type: str, target_id: str, body: str
    ) -> dict:
        if target_type not in COMMENT_TARGET_TYPES:
            raise ValueError(f"评论不支持目标类型: {target_type!r}")
        with self._tx() as c:
            # `_tx()` only serializes one Store object.  Acquire SQLite's write
            # reservation before validating either side so another Store/process
            # cannot delete the authenticated actor or polymorphic target between
            # the SELECT and INSERT.
            c.execute("BEGIN IMMEDIATE")
            if not self._social_actor_exists_tx(c, user_id):
                raise LookupError("评论用户不存在")
            if not self._social_target_exists_tx(c, target_type, target_id):
                raise LookupError("评论目标不存在")
            cur = c.execute(
                "INSERT INTO comments(target_type, target_id, user_id, body, "
                "created_at) VALUES(?,?,?,?,?)",
                (target_type, str(target_id), user_id, body, _now()),
            )
            cid = cur.lastrowid
            return _row(
                c.execute(
                    "SELECT c.*, u.username, u.display_name AS user_display "
                    "FROM comments c LEFT JOIN users u ON c.user_id=u.id "
                    "WHERE c.id=?",
                    (cid,),
                ).fetchone()
            )

    def list_comments(
        self, target_type: str, target_id: str, *, limit: int = 100,
        page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        with self._tx() as c:
            sql = (
                "SELECT c.*, u.username, u.display_name AS user_display "
                "FROM comments c LEFT JOIN users u ON c.user_id=u.id "
                "WHERE c.target_type=? AND c.target_id=? "
                "ORDER BY c.id DESC"
            )
            params = (target_type, str(target_id))
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, params, page=page, per_page=pp)
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
            sql += " LIMIT ?"
            return [_row(r) for r in c.execute(
                sql, params + (max(1, min(limit, 500)),)
            )]

    def delete_comment(self, comment_id: int, user_id: int) -> bool:
        """仅作者或 admin 可删；返回是否删除成功。"""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT user_id FROM comments WHERE id=?", (comment_id,)
            ).fetchone()
            if not row:
                return False
            if int(row["user_id"]) != int(user_id):
                return False
            _delete_social_target(c, "comment", comment_id)
            cur = c.execute(
                "DELETE FROM comments WHERE id=? AND user_id=?",
                (comment_id, user_id),
            )
            return cur.rowcount > 0

    def delete_comment_admin(self, comment_id: int) -> bool:
        """admin 强删任意评论（无视作者）；返回是否删除成功（False=评论不存在）。"""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "SELECT 1 FROM comments WHERE id=?", (comment_id,)
            ).fetchone():
                return False
            _delete_social_target(c, "comment", comment_id)
            cur = c.execute("DELETE FROM comments WHERE id=?", (comment_id,))
            return cur.rowcount > 0

    def comment_exists(self, comment_id: int) -> bool:
        """只读探测评论是否存在（DELETE handler 区分 404 vs 403 用）。"""
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM comments WHERE id=?", (comment_id,)
            ).fetchone() is not None

    def comment_count(self, target_type: str, target_id: str) -> int:
        with self._tx() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM comments WHERE target_type=? AND target_id=?",
                (target_type, str(target_id)),
            ).fetchone()[0])

    # ── likes（点赞）──────────────────────────────────────────
    def like(
        self, user_id: int, target_type: str, target_id: str
    ) -> bool:
        """点赞；返回 True 表示新建。"""
        if target_type not in LIKE_TARGET_TYPES:
            raise ValueError(f"点赞不支持目标类型: {target_type!r}")
        tid = str(target_id)
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._social_actor_exists_tx(c, user_id):
                raise LookupError("点赞用户不存在")
            if not self._social_target_exists_tx(c, target_type, tid):
                raise LookupError("点赞目标不存在")
            existing = c.execute(
                "SELECT 1 FROM likes WHERE user_id=? AND target_type=? AND target_id=?",
                (user_id, target_type, tid),
            ).fetchone()
            if existing:
                return False
            c.execute(
                "INSERT INTO likes(user_id, target_type, target_id, created_at) "
                "VALUES(?,?,?,?)",
                (user_id, target_type, tid, _now()),
            )
            # 对 match 点赞顺带 +1 计数（经 matches_index 定位到 per-game 表）
            if target_type == "match":
                tbl = self._match_table_of(c, tid)
                if tbl:
                    c.execute(
                        f"UPDATE {tbl} SET likes_count = likes_count + 1 WHERE id=?",
                        (tid,),
                    )
            return True

    def unlike(
        self, user_id: int, target_type: str, target_id: str
    ) -> bool:
        if target_type not in LIKE_TARGET_TYPES:
            raise ValueError(f"点赞不支持目标类型: {target_type!r}")
        tid = str(target_id)
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._social_actor_exists_tx(c, user_id):
                raise LookupError("点赞用户不存在")
            if not self._social_target_exists_tx(c, target_type, tid):
                raise LookupError("点赞目标不存在")
            cur = c.execute(
                "DELETE FROM likes WHERE user_id=? AND target_type=? AND target_id=?",
                (user_id, target_type, tid),
            )
            if cur.rowcount > 0 and target_type == "match":
                tbl = self._match_table_of(c, tid)
                if tbl:
                    c.execute(
                        f"UPDATE {tbl} SET likes_count = MAX(0, likes_count - 1) WHERE id=?",
                        (tid,),
                    )
            return cur.rowcount > 0

    def is_liked(
        self, user_id: int, target_type: str, target_id: str
    ) -> bool:
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM likes WHERE user_id=? AND target_type=? AND target_id=?",
                (user_id, target_type, str(target_id)),
            ).fetchone() is not None

    def like_count(self, target_type: str, target_id: str) -> int:
        with self._tx() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM likes WHERE target_type=? AND target_id=?",
                (target_type, str(target_id)),
            ).fetchone()[0])

    def incr_match_view(self, match_id: str) -> None:
        with self._tx() as c:
            tbl = self._match_table_of(c, match_id)
            if tbl:
                c.execute(
                    f"UPDATE {tbl} SET views_count = views_count + 1 WHERE id=?",
                    (match_id,),
                )

    # ── follows（关注关系）────────────────────────────────────
    def follow(self, follower_id: int, followee_id: int) -> bool:
        """关注；返回 True 表示新建关注，False 表示已存在。不能关注自己。"""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            users = {
                int(row["id"])
                for row in c.execute(
                    "SELECT id FROM users WHERE id IN (?,?)",
                    (follower_id, followee_id),
                ).fetchall()
            }
            if follower_id not in users or followee_id not in users:
                raise LookupError("关注关系中的用户不存在")
            if follower_id == followee_id:
                return False
            existing = c.execute(
                "SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?",
                (follower_id, followee_id),
            ).fetchone()
            if existing:
                return False
            c.execute(
                "INSERT INTO follows(follower_id, followee_id, created_at) VALUES(?,?,?)",
                (follower_id, followee_id, _now()),
            )
            return True

    def unfollow(self, follower_id: int, followee_id: int) -> bool:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            users = {
                int(row["id"])
                for row in c.execute(
                    "SELECT id FROM users WHERE id IN (?,?)",
                    (follower_id, followee_id),
                ).fetchall()
            }
            if follower_id not in users or followee_id not in users:
                raise LookupError("关注关系中的用户不存在")
            cur = c.execute(
                "DELETE FROM follows WHERE follower_id=? AND followee_id=?",
                (follower_id, followee_id),
            )
            return cur.rowcount > 0

    def is_following(self, follower_id: int, followee_id: int) -> bool:
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?",
                (follower_id, followee_id),
            ).fetchone() is not None

    def list_followers(self, user_id: int, *, limit: int = 50) -> list[dict]:
        with self._tx() as c:
            return [_row(r) for r in c.execute(
                "SELECT u.id, u.username, u.display_name, f.created_at "
                "FROM follows f JOIN users u ON f.follower_id=u.id "
                "WHERE f.followee_id=? ORDER BY f.created_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 200))),
            )]

    def list_following(self, user_id: int, *, limit: int = 50) -> list[dict]:
        with self._tx() as c:
            return [_row(r) for r in c.execute(
                "SELECT u.id, u.username, u.display_name, f.created_at "
                "FROM follows f JOIN users u ON f.followee_id=u.id "
                "WHERE f.follower_id=? ORDER BY f.created_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 200))),
            )]

    def follower_count(self, user_id: int) -> int:
        with self._tx() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM follows WHERE followee_id=?", (user_id,)
            ).fetchone()[0])

    def following_count(self, user_id: int) -> int:
        with self._tx() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM follows WHERE follower_id=?", (user_id,)
            ).fetchone()[0])

    # ── favorites（收藏 Bot）──────────────────────────────────
    def favorite(self, user_id: int, bot_id: int) -> bool:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise LookupError("收藏用户不存在")
            if not c.execute("SELECT 1 FROM bots WHERE id=?", (bot_id,)).fetchone():
                raise LookupError("收藏 Bot 不存在")
            existing = c.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND bot_id=?",
                (user_id, bot_id),
            ).fetchone()
            if existing:
                return False
            c.execute(
                "INSERT INTO favorites(user_id, bot_id, created_at) VALUES(?,?,?)",
                (user_id, bot_id, _now()),
            )
            return True

    def unfavorite(self, user_id: int, bot_id: int) -> bool:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                raise LookupError("收藏用户不存在")
            if not c.execute("SELECT 1 FROM bots WHERE id=?", (bot_id,)).fetchone():
                raise LookupError("收藏 Bot 不存在")
            cur = c.execute(
                "DELETE FROM favorites WHERE user_id=? AND bot_id=?", (user_id, bot_id)
            )
            return cur.rowcount > 0

    def is_favorite(self, user_id: int, bot_id: int) -> bool:
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND bot_id=?",
                (user_id, bot_id),
            ).fetchone() is not None

    def list_favorites(self, user_id: int, *, limit: int = 50) -> list[dict]:
        with self._tx() as c:
            return [_row(r) for r in c.execute(
                "SELECT b.id, b.name, b.display_name, b.game_id, "
                "u.username AS owner_name, u.display_name AS owner_display, "
                "r.rating, fav.created_at "
                "FROM favorites fav JOIN bots b ON fav.bot_id=b.id "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "LEFT JOIN ratings r ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE fav.user_id=? ORDER BY fav.created_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 200))),
            )]

    def favorite_count(self, bot_id: int) -> int:
        with self._tx() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM favorites WHERE bot_id=?", (bot_id,)
            ).fetchone()[0])

    # ── notifications ─────────────────────────────────────────
    def add_notification(
        self,
        user_id: int,
        *,
        type: str = "",
        title: str = "",
        body: str = "",
        link: str = "",
        communication_message_public_id: str | None = None,
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO notifications(user_id, type, title, body, link, "
                "is_read, communication_message_public_id, created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    user_id, type, title, body, link, 0,
                    communication_message_public_id, _now(),
                ),
            )
            nid = cur.lastrowid
            return _row(
                c.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
            )

    def list_notifications(
        self,
        user_id: int,
        *,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
        page: int | None = None,
        per_page: int = 50,
    ) -> list[dict] | dict:
        with self._tx() as c:
            sql = "SELECT * FROM notifications WHERE user_id=?"
            params: list[Any] = [user_id]
            if unread_only:
                sql += " AND is_read=0"
            sql += " ORDER BY id DESC"
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, tuple(params), page=page, per_page=pp)
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
            sql += " LIMIT ? OFFSET ?"
            params.extend([max(1, min(limit, 200)), max(0, offset)])
            return [_row(r) for r in c.execute(sql, params)]

    def unread_notification_count(self, user_id: int) -> int:
        with self._tx() as c:
            return int(
                c.execute(
                    "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
                    (user_id,),
                ).fetchone()[0]
            )

    def mark_notification_read(self, notif_id: int, user_id: int) -> bool:
        with self._tx() as c:
            projection = c.execute(
                "SELECT communication_message_public_id FROM notifications "
                "WHERE id=? AND user_id=?",
                (notif_id, user_id),
            ).fetchone()
            cur = c.execute(
                "UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?",
                (notif_id, user_id),
            )
            if projection and projection["communication_message_public_id"]:
                message = c.execute(
                    "SELECT id,conversation_id FROM messages WHERE public_id=?",
                    (projection["communication_message_public_id"],),
                ).fetchone()
                if message:
                    c.execute(
                        "UPDATE conversation_participants SET last_read_message_id="
                        "CASE WHEN COALESCE(last_read_message_id,0)<? THEN ? "
                        "ELSE last_read_message_id END "
                        "WHERE conversation_id=? AND user_id=?",
                        (message["id"], message["id"], message["conversation_id"], user_id),
                    )
            return cur.rowcount > 0

    def mark_all_notifications_read(self, user_id: int) -> int:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0",
                (user_id,),
            )
            c.execute(
                "UPDATE conversation_participants AS cp SET last_read_message_id=MAX("
                "COALESCE(last_read_message_id,0),COALESCE((SELECT MAX(m.id) "
                "FROM messages m JOIN notifications n "
                "ON n.communication_message_public_id=m.public_id "
                "WHERE n.user_id=? AND m.conversation_id=cp.conversation_id),0)) "
                "WHERE cp.user_id=? AND EXISTS(SELECT 1 FROM messages m "
                "JOIN notifications n ON n.communication_message_public_id=m.public_id "
                "WHERE n.user_id=? AND m.conversation_id=cp.conversation_id)",
                (user_id, user_id, user_id),
            )
            return cur.rowcount

    # ── notification_prefs ────────────────────────────────────
    _NOTIF_PREF_DEFAULTS = {
        "email_match_done": 0,
        "email_followed": 0,
        "email_contest": 0,
        "email_comment": 0,
    }

    def get_notification_prefs(self, user_id: int) -> dict:
        with self._tx() as c:
            row = c.execute(
                "SELECT email_match_done, email_followed, email_contest, "
                "email_comment FROM notification_prefs WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if not row:
                # 懒建默认行
                c.execute(
                    "INSERT INTO notification_prefs(user_id) VALUES(?)",
                    (user_id,),
                )
                row = c.execute(
                    "SELECT email_match_done, email_followed, email_contest, "
                    "email_comment FROM notification_prefs WHERE user_id=?",
                    (user_id,),
                ).fetchone()
            return _row(row)

    def update_notification_prefs(self, user_id: int, **fields: Any) -> dict:
        allowed = {
            "email_match_done", "email_followed", "email_contest", "email_comment",
        }
        clean = {k: (1 if v else 0) for k, v in fields.items() if k in allowed}
        with self._tx() as c:
            existing = c.execute(
                "SELECT user_id FROM notification_prefs WHERE user_id=?", (user_id,)
            ).fetchone()
            if not existing:
                c.execute(
                    "INSERT INTO notification_prefs(user_id) VALUES(?)", (user_id,)
                )
            if clean:
                sets = ",".join(f"{k}=?" for k in clean)
                c.execute(
                    f"UPDATE notification_prefs SET {sets} WHERE user_id=?",
                    [*clean.values(), user_id],
                )
            # 内联读取（避免在 _tx 内递归调用 get_notification_prefs 死锁）
            row = c.execute(
                "SELECT email_match_done, email_followed, email_contest, "
                "email_comment FROM notification_prefs WHERE user_id=?",
                (user_id,),
            ).fetchone()
            return _row(row)

    # ── contests ──────────────────────────────────────────────

    def create_contest(
        self,
        title: str,
        organizer_id: int,
        *,
        description: str = "",
        registration_opens_at: str | None = None,
        registration_closes_at: str | None = None,
        starts_at: str | None = None,
        ends_at: str | None = None,
        status: str = "draft",
        game_id: str = "holdem",
        stages_json: str = "[]",
        template_id: str = "holdem_swiss_ko",
        current_stage_idx: int = 0,
        phase: str = "standalone",
        source_contest_id: int | None = None,
        # Low-level fixture/repair callers preserve their historical ability to
        # model foreign references.  Product paths always pass an explicit ACL
        # decision from ContestManager.
        source_contest_include_all_hidden: bool = True,
        time_control_id: str | None = None,
        require_real_name: int = 0,
    ) -> dict:
        title = validate_contest_title(title)
        validate_contest_times(
            registration_opens_at, registration_closes_at, starts_at
        )
        validate_canonical_naive_timestamp(
            ends_at, "赛事结束时间", allow_none=True
        )
        current_stage_idx = exact_nonnegative_int(current_stage_idx)
        if current_stage_idx is None:
            raise ValueError("赛事当前阶段必须是非负整数")
        gid = _registered_game_id(game_id)
        if not isinstance(source_contest_include_all_hidden, bool):
            raise ValueError("关联赛事隐藏态权限无效")
        with self._tx() as c:
            if source_contest_id is not None:
                # Reserve the writer before validating the target so a
                # concurrent deletion cannot create a dangling edge between
                # this SELECT and the INSERT.
                c.execute("BEGIN IMMEDIATE")
                source_contest_id = _validate_contest_source_tx(
                    c,
                    source_contest_id,
                    game_id=gid,
                    source_owner_id=organizer_id,
                    include_all_hidden=source_contest_include_all_hidden,
                )
            contract = _active_game_contract_tx(c, gid)
            cur = c.execute(
                "INSERT INTO contests(title, description, organizer_id, status, "
                "registration_opens_at, registration_closes_at, starts_at, "
                "ends_at, created_at, game_id, ruleset_version, protocol_version, "
                "rating_pool_id, "
                "stages_json, "
                "current_stage_idx, template_id, phase, "
                "source_contest_id, time_control_id, require_real_name) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    title,
                    description,
                    organizer_id,
                    status,
                    registration_opens_at,
                    registration_closes_at,
                    starts_at,
                    ends_at,
                    _now(),
                    gid,
                    contract["ruleset_version"],
                    contract["protocol_version"],
                    contract["rating_pool_id"],
                    stages_json,
                    current_stage_idx,
                    template_id,
                    phase,
                    source_contest_id,
                    time_control_id,
                    require_real_name,
                ),
            )
            cid = cur.lastrowid
            return _contest_row(
                c.execute("SELECT * FROM contests WHERE id=?", (cid,)).fetchone()
            )

    def get_contest(self, contest_id: int) -> dict | None:
        with self._tx() as c:
            return _contest_row(
                c.execute(
                    "SELECT * FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
            )

    def get_contest_by_showcase_key(self, showcase_key: str) -> dict | None:
        """Resolve one synthetic snapshot by its durable idempotency key."""
        with self._tx() as c:
            return _contest_row(
                c.execute(
                    "SELECT * FROM contests WHERE showcase_key=?", (showcase_key,)
                ).fetchone()
            )

    def freeze_contest_showcase(self, contest_id: int, showcase_key: str) -> dict:
        """Atomically mark a fully generated contest graph as a read-only snapshot.

        The seed must first let Manager/Orchestrator reach the desired state.  A
        snapshot cannot be frozen while it owns an active match, because normal
        startup recovery intentionally ignores frozen graphs afterwards.
        """
        key = str(showcase_key).strip()
        if not key or len(key) > 80:
            raise ValueError("showcase_key 必须为 1..80 个字符")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            current_key = contest["showcase_key"]
            if current_key and current_key != key:
                raise ValueError("赛事已绑定其他 showcase_key")
            duplicate = c.execute(
                "SELECT id FROM contests WHERE showcase_key=? AND id<>? LIMIT 1",
                (key, contest_id),
            ).fetchone()
            if duplicate:
                raise ValueError("showcase_key 已被其他赛事占用")
            for gid in _all_game_ids():
                table = _matches_table(gid)
                active = c.execute(
                    f"SELECT 1 FROM {table} WHERE contest_id=? "
                    "AND status IN (?,?) LIMIT 1",
                    (contest_id, STATUS_PENDING, STATUS_RUNNING),
                ).fetchone()
                if active:
                    raise ValueError("赛事仍有活跃对局，不能冻结为演示快照")
            c.execute(
                "UPDATE contests SET showcase_key=? WHERE id=?",
                (key, contest_id),
            )
            return _contest_row(
                c.execute(
                    "SELECT * FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
            )

    def update_contest(self, contest_id: int, **fields: Any) -> dict | None:
        removed_rule_fields = {"hands_per_match", "match_config_json"}.intersection(fields)
        if removed_rule_fields:
            names = ", ".join(sorted(removed_rule_fields))
            raise ValueError(f"赛事规则字段已移除: {names}")
        frozen_format_fields = {
            "time_control_id",
            "format_snapshot_json",
        }.intersection(fields)
        if frozen_format_fields:
            raise ValueError(
                "赛事时限与抽签快照只能通过零进度 CAS 或发布事务修改"
            )
        allowed = {
            "title",
            "description",
            "status",
            "registration_opens_at",
            "registration_closes_at",
            "starts_at",
            "ends_at",
            "game_id",
            "stages_json",
            "current_stage_idx",
            "template_id",
            "rest_ends_at",
            "phase",
            "source_contest_id",
            "official_results_ready",
            "require_real_name",
        }
        clean = {k: v for k, v in fields.items() if k in allowed}
        if "title" in clean:
            clean["title"] = validate_contest_title(clean["title"])
        if "current_stage_idx" in clean:
            stage_idx = exact_nonnegative_int(clean["current_stage_idx"])
            if stage_idx is None:
                raise ValueError("赛事当前阶段必须是非负整数")
            clean["current_stage_idx"] = stage_idx
        if "official_results_ready" in clean:
            ready = exact_nonnegative_int(clean["official_results_ready"])
            if ready not in (0, 1):
                raise ValueError("正式名次就绪标记必须是 0 或 1")
            clean["official_results_ready"] = ready
        for key, label in (
            ("ends_at", "赛事结束时间"),
            ("rest_ends_at", "赛事休息结束时间"),
        ):
            if key in clean:
                clean[key] = validate_canonical_naive_timestamp(
                    clean[key], label, allow_none=True
                )
        with self._tx() as c:
            # ``require_real_name`` controls whether private PII may be read.
            # Status transitions also re-check every referenced Bot before a
            # draft contest can become live.  Serialize either mutation before
            # the first SELECT so both guards share the writer linearization
            # point with concurrent entry insertion and owner deletion.
            if (
                "status" in clean
                or "require_real_name" in clean
                or "source_contest_id" in clean
                or "game_id" in clean
            ):
                c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not current:
                return None
            candidate_source_id = clean.get(
                "source_contest_id", current["source_contest_id"]
            )
            if candidate_source_id is not None and (
                "source_contest_id" in clean or "game_id" in clean
            ):
                clean["source_contest_id"] = _validate_contest_source_tx(
                    c,
                    candidate_source_id,
                    game_id=clean.get("game_id", current["game_id"]),
                    contest_id=contest_id,
                    source_owner_id=current["organizer_id"],
                )
            if (
                "require_real_name" in clean
                and int(clean["require_real_name"] or 0)
                != int(current["require_real_name"] or 0)
                and c.execute(
                    "SELECT 1 FROM contest_entries WHERE contest_id=? LIMIT 1",
                    (contest_id,),
                ).fetchone()
            ):
                raise ValueError("赛事已有报名，不能修改实名要求")
            # A partial time PATCH is only meaningful after merging the stored
            # values.  Validate that complete candidate before the single UPDATE,
            # so an invalid schedule cannot partially write another field.
            time_fields = {
                "registration_opens_at", "registration_closes_at", "starts_at",
            }
            if time_fields.intersection(clean):
                validate_contest_times(
                    clean.get("registration_opens_at", current["registration_opens_at"]),
                    clean.get("registration_closes_at", current["registration_closes_at"]),
                    clean.get("starts_at", current["starts_at"]),
                )
            # 状态机校验：status 变更须合法（防止 admin PATCH 把 finished/cancelled
            # 错误改写——曾导致 contest3 已完成 96 场却被改成 cancelled 隐藏全部结果）。
            if clean.get("status"):
                cur_status = current["status"]
                new_status = clean["status"]
                if (
                    new_status != cur_status
                    and new_status in (CONTEST_REST, CONTEST_FINISHED)
                ):
                    raise ValueError(
                        "rest/finished 只能通过赛事决策专用原子事务进入"
                    )
                # 终态不可变（finished/cancelled 是终态，不允许再改）
                if cur_status in (CONTEST_FINISHED, CONTEST_CANCELLED) and new_status != cur_status:
                    raise ValueError(
                        f"赛事已处于终态 {cur_status}，不能改为 {new_status}"
                    )
                # cancelled 只能从「未开始」态进入（draft/open/published）
                if new_status == CONTEST_CANCELLED and cur_status not in (
                    CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED,
                ):
                    raise ValueError(
                        f"赛事处于 {cur_status} 态，不能取消（仅 draft/open/published 可取消）"
                    )
                if new_status in (
                    CONTEST_OPEN,
                    CONTEST_PUBLISHED,
                    CONTEST_RUNNING,
                    CONTEST_REST,
                ):
                    _require_contest_without_owner_deleted_bot_tx(c, contest_id)
                if (
                    new_status in (CONTEST_REST, CONTEST_FINISHED)
                    and new_status != cur_status
                    and current["published_stage_pairing_count"] is not None
                ):
                    current_stage_idx = exact_nonnegative_int(
                        current["current_stage_idx"]
                    )
                    if current_stage_idx is None or not (
                        self._contest_stage_manifest_is_valid_tx(
                            c,
                            contest_id,
                            current_stage_idx,
                            include_terminal_orphans=True,
                            require_manifest=True,
                        )
                    ):
                        raise ValueError("赛事当前阶段对阵批次完整性校验失败")
            sets = [f"{key}=?" for key in clean]
            vals = list(clean.values())
            if sets:
                vals.append(contest_id)
                c.execute(
                    f"UPDATE contests SET {','.join(sets)} WHERE id=?", vals
                )
            return _contest_row(
                c.execute(
                    "SELECT * FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
            )

    def compare_and_swap_unstarted_contest_stages(
        self,
        contest_id: int,
        *,
        expected_status: str,
        expected_stages_json: str,
        stages_json: str,
        expected_time_control_id: str | None = None,
        time_control_id: str | None = None,
        update_time_control: bool = False,
    ) -> dict:
        """Replace one draft/open stage snapshot behind a zero-progress CAS gate.

        A pairing is not the only durable proof that a contest has started.  A
        dispatcher may already have created an execution job or Match, and a
        damaged/imported history may retain stage/official results without its
        pairing row.  Recheck every execution/result surface in the same
        ``BEGIN IMMEDIATE`` transaction as the stage update so a settings patch
        can never rewrite the rules underneath such progress.
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not current:
                raise ValueError("比赛不存在")
            if current["showcase_key"]:
                raise ValueError("演示赛事为只读快照")
            if current["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("仅 draft/open 且尚未生成赛程的赛事可修改系列设置")
            if (
                str(current["status"]) != expected_status
                or str(current["stages_json"] or "[]") != expected_stages_json
                or (
                    update_time_control
                    and current["time_control_id"] != expected_time_control_id
                )
            ):
                raise ValueError("赛事设置已被并发修改，请刷新后重试")
            if update_time_control:
                if not isinstance(time_control_id, str) or not time_control_id:
                    raise ValueError("时限快照必须是稳定 ID")
                resolved_target = _resolved_time_control_id(
                    str(current["game_id"]), time_control_id
                )
                if resolved_target != time_control_id:
                    raise ValueError("时限快照必须是稳定 ID")
                if current["time_control_id"] is None:
                    legacy_default = _resolved_time_control_id(
                        str(current["game_id"]), None
                    )
                    if time_control_id != legacy_default:
                        raise ValueError(
                            "历史赛事时限只能补齐为该游戏的旧默认值"
                        )
            if (
                exact_nonnegative_int(current["current_stage_idx"]) != 0
                or exact_nonnegative_int(current["official_results_ready"]) != 0
            ):
                raise ValueError("赛事已有阶段或正式结果进度，不能修改系列设置")
            progress_exists = any(
                (
                    c.execute(
                        "SELECT 1 FROM contest_pairings "
                        "WHERE contest_id=? LIMIT 1",
                        (contest_id,),
                    ).fetchone(),
                    c.execute(
                        "SELECT 1 FROM execution_jobs "
                        "WHERE contest_id=? LIMIT 1",
                        (contest_id,),
                    ).fetchone(),
                    c.execute(
                        "SELECT 1 FROM contest_stage_results "
                        "WHERE contest_id=? LIMIT 1",
                        (contest_id,),
                    ).fetchone(),
                    c.execute(
                        "SELECT 1 FROM contest_official_results "
                        "WHERE contest_id=? LIMIT 1",
                        (contest_id,),
                    ).fetchone(),
                    *(
                        c.execute(
                            f"SELECT 1 FROM {_matches_table(game_id)} "
                            "WHERE contest_id=? LIMIT 1",
                            (contest_id,),
                        ).fetchone()
                        for game_id in _all_game_ids()
                    ),
                )
            )
            if progress_exists:
                raise ValueError(
                    "赛事已生成赛程、执行任务、对局或结果，不能修改系列设置"
                )
            if update_time_control:
                changed = c.execute(
                    "UPDATE contests SET stages_json=?,time_control_id=? "
                    "WHERE id=? AND status=? AND stages_json=? "
                    "AND time_control_id IS ?",
                    (
                        stages_json,
                        time_control_id,
                        contest_id,
                        expected_status,
                        expected_stages_json,
                        expected_time_control_id,
                    ),
                )
            else:
                changed = c.execute(
                    "UPDATE contests SET stages_json=? "
                    "WHERE id=? AND status=? AND stages_json=?",
                    (stages_json, contest_id, expected_status, expected_stages_json),
                )
            if changed.rowcount != 1:
                raise ValueError("赛事设置已被并发修改，请刷新后重试")
            return _contest_row(
                c.execute(
                    "SELECT * FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
            )

    def freeze_initial_group_contest(
        self,
        contest_id: int,
        *,
        expected_status: str,
        expected_stages_json: str,
        expected_time_control_id: str | None,
        stages_json: str,
        format_snapshot_json: str,
        entry_rows: list[dict[str, Any]],
        pairing_rows: list[dict[str, Any]],
        registration_opens_at: str,
        registration_closes_at: str,
        starts_at: str | None,
    ) -> dict:
        """Atomically freeze draw, roster, versions and the first DRR schedule."""
        if not entry_rows or not pairing_rows:
            raise ValueError("分组发布快照与首阶段赛程不能为空")
        validate_contest_times(
            registration_opens_at, registration_closes_at, starts_at
        )
        _validate_pairing_publication_times(
            pairing_rows, require_published_at=True
        )
        if expected_time_control_id is not None and (
            not isinstance(expected_time_control_id, str)
            or not expected_time_control_id
        ):
            raise ValueError("分组发布的预期时限 ID 无效")
        try:
            snapshot = json.loads(format_snapshot_json)
        except (TypeError, ValueError) as exc:
            raise ValueError("分组抽签快照不是有效 JSON") from exc
        if not isinstance(snapshot, dict):
            raise ValueError("分组抽签快照必须是对象")
        allowed_snapshot_keys = {
            "version", "algorithm", "group_count", "group_sizes", "draw_order",
            "groups", "source", "expected_match_count", "audit_nonce",
            "audit_digest",
        }
        if set(snapshot) - allowed_snapshot_keys:
            raise ValueError("分组抽签快照包含未知字段")
        audit = snapshot.get("audit_digest")
        audit_nonce = snapshot.get("audit_nonce")
        unsigned_snapshot = {
            key: value for key, value in snapshot.items() if key != "audit_digest"
        }
        expected_audit = hashlib.sha256(
            json.dumps(
                unsigned_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if (
            isinstance(snapshot.get("version"), bool)
            or snapshot.get("version") != 1
            or snapshot.get("algorithm") not in {
                "secure_random_balanced_v1",
                "protected_seed_random_balanced_v1",
            }
            or not isinstance(audit, str)
            or audit != expected_audit
            or not isinstance(audit_nonce, str)
            or len(audit_nonce) != 64
            or any(char not in "0123456789abcdef" for char in audit_nonce)
        ):
            raise ValueError("分组抽签快照版本或审计值无效")
        normalized_series = _pairing_series_batch(pairing_rows)
        columns = (
            "contest_id", "round_num", "entry_a_id", "entry_b_id",
            "bot_a_id", "bot_b_id", "bot_a_version_id", "bot_b_version_id",
            "pairing_seed", "published_at", "scheduled_at", "match_id", "status",
            "stage_idx", "stage_key", "group_id", "bracket_slot", "color_first",
            "series_index", "series_size", "tiebreak_group", "tiebreak_game",
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_execution_admission_tx(c, maintenance_only=True)
            contest = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if (
                contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN)
                or contest["status"] != expected_status
                or str(contest["stages_json"] or "[]") != expected_stages_json
                or contest["time_control_id"] != expected_time_control_id
                or exact_nonnegative_int(contest["current_stage_idx"]) != 0
                or exact_sqlite_bool(contest["official_results_ready"]) is not False
                or contest["format_snapshot_json"] != "{}"
                or contest["template_id"] not in {
                    "pencil_group_drr", "gomoku_seeded_group_drr_final",
                }
            ):
                raise ValueError("赛事发布快照已变化，请刷新后重试")
            if any(
                c.execute(query, (contest_id,)).fetchone()
                for query in (
                    "SELECT 1 FROM contest_pairings WHERE contest_id=? LIMIT 1",
                    "SELECT 1 FROM execution_jobs WHERE contest_id=? LIMIT 1",
                    "SELECT 1 FROM contest_stage_results WHERE contest_id=? LIMIT 1",
                    "SELECT 1 FROM contest_official_results WHERE contest_id=? LIMIT 1",
                )
            ):
                raise ValueError("赛事已有持久进度，拒绝重新抽签")
            if any(
                c.execute(
                    f"SELECT 1 FROM matches_{game_id} WHERE contest_id=? LIMIT 1",
                    (contest_id,),
                ).fetchone()
                for game_id in sorted(_all_game_ids())
            ):
                raise ValueError("赛事已有持久对局，拒绝重新抽签")

            stored_entries = c.execute(
                "SELECT * FROM contest_entries WHERE contest_id=? ORDER BY registered_at,id",
                (contest_id,),
            ).fetchall()
            expected_identity = {
                (int(row["id"]), int(row["user_id"]), row["bot_id"])
                for row in stored_entries
            }
            if any(
                not isinstance(row, dict)
                or any(
                    isinstance(row.get(key), bool)
                    or not isinstance(row.get(key), int)
                    or row[key] < 1
                    for key in ("id", "user_id", "bot_id")
                )
                for row in entry_rows
            ):
                raise ValueError("抽签名册身份字段无效")
            supplied_identity = {
                (row["id"], row["user_id"], row["bot_id"])
                for row in entry_rows
            }
            if expected_identity != supplied_identity or len(stored_entries) != len(entry_rows):
                raise ValueError("抽签期间报名名册已变化")

            groups = snapshot.get("groups")
            draw_order = snapshot.get("draw_order")
            group_sizes = snapshot.get("group_sizes")
            group_count = snapshot.get("group_count")
            if (
                isinstance(group_count, bool)
                or not isinstance(group_count, int)
                or group_count < 2
                or not isinstance(groups, dict)
                or len(groups) != group_count
                or not isinstance(group_sizes, dict)
                or set(group_sizes) != set(groups)
                or any(
                    isinstance(size, bool)
                    or not isinstance(size, int)
                    or size < 2
                    for size in group_sizes.values()
                )
                or not isinstance(draw_order, list)
                or len(draw_order) != len(entry_rows)
            ):
                raise ValueError("分组抽签快照拓扑无效")
            try:
                frozen_stages = json.loads(stages_json)
            except (TypeError, ValueError) as exc:
                raise ValueError("分组阶段快照损坏") from exc
            stage_zero = (
                frozen_stages[0]
                if isinstance(frozen_stages, list)
                and frozen_stages
                and isinstance(frozen_stages[0], dict)
                else None
            )
            if (
                not stage_zero
                or stage_zero.get("type") != "group_double_round_robin"
                or stage_zero.get("group_count") != group_count
            ):
                raise ValueError("分组阶段与抽签快照拓扑不一致")
            expected_algorithm = (
                "protected_seed_random_balanced_v1"
                if contest["template_id"] == "gomoku_seeded_group_drr_final"
                else "secure_random_balanced_v1"
            )
            if snapshot["algorithm"] != expected_algorithm:
                raise ValueError("分组模板与抽签算法不一致")
            all_entry_ids = {int(row["id"]) for row in stored_entries}
            if (
                any(isinstance(value, bool) or not isinstance(value, int) for value in draw_order)
                or len(set(draw_order)) != len(draw_order)
                or set(draw_order) != all_entry_ids
            ):
                raise ValueError("分组抽签顺序不完整")
            entry_group: dict[int, str] = {}
            for group_id, members in groups.items():
                try:
                    group_id = _parse_stable_group_id(group_id)
                except ValueError as exc:
                    raise ValueError("分组快照成员或人数无效") from exc
                if (
                    not isinstance(members, list)
                    or len(members) < 2
                    or group_sizes.get(group_id) != len(members)
                ):
                    raise ValueError("分组快照成员或人数无效")
                for entry_id in members:
                    if (
                        isinstance(entry_id, bool)
                        or not isinstance(entry_id, int)
                        or entry_id not in all_entry_ids
                        or entry_id in entry_group
                    ):
                        raise ValueError("分组快照存在重复或未知参赛者")
                    entry_group[entry_id] = group_id
            if set(entry_group) != all_entry_ids or max(group_sizes.values()) - min(group_sizes.values()) > 1:
                raise ValueError("分组快照不完整或不均衡")

            entry_by_id = {int(row["id"]): row for row in stored_entries}
            supplied_by_id = {int(row["id"]): row for row in entry_rows}
            # Read the complete roster and every current immutable version in
            # two set-based queries.  Pairing count is quadratic for DRR, but
            # Bot identity/version/artifact validation must remain linear in
            # the number of entrants and share this transaction's write lock.
            bot_rows = {
                int(row["id"]): dict(row)
                for row in c.execute(
                    "SELECT DISTINCT b.* FROM contest_entries e "
                    "JOIN bots b ON b.id=e.bot_id WHERE e.contest_id=?",
                    (contest_id,),
                ).fetchall()
            }
            current_version_rows = {
                int(row["bot_id"]): dict(row)
                for row in c.execute(
                    "SELECT DISTINCT v.* FROM contest_entries e "
                    "JOIN bots b ON b.id=e.bot_id "
                    "JOIN bot_versions v ON v.bot_id=b.id "
                    "AND v.version=b.current_version "
                    "WHERE e.contest_id=?",
                    (contest_id,),
                ).fetchall()
            }
            version_by_bot: dict[int, int | None] = {}
            integrity_cache: set[Any] = set()
            for seed, entry_id in enumerate(draw_order, start=1):
                supplied = supplied_by_id[entry_id]
                supplied_seed = supplied.get("seed")
                if (
                    supplied.get("group_id") != entry_group[entry_id]
                    or isinstance(supplied_seed, bool)
                    or not isinstance(supplied_seed, int)
                    or supplied_seed != seed
                ):
                    raise ValueError("分组快照与报名冻结字段不一致")
                bot_id = int(entry_by_id[entry_id]["bot_id"])
                bot = bot_rows.get(bot_id)
                if (
                    not bot
                    or int(bot["owner_id"]) != int(entry_by_id[entry_id]["user_id"])
                    or int(bot["is_active"]) != 1
                    or bot["game_id"] != contest["game_id"]
                ):
                    raise ValueError("发布时 Bot 身份或可用性已变化")
                current_version = exact_nonnegative_int(bot.get("current_version"))
                version = current_version_rows.get(bot_id)
                if current_version is None or (
                    current_version == 0 and version is not None
                ) or (
                    current_version > 0
                    and (
                        version is None
                        or int(version.get("version") or 0) != current_version
                    )
                ):
                    raise ValueError("Bot 当前版本在发布期间已变化")
                runtime = version or bot
                try:
                    require_supported_binary_metadata(
                        str(runtime.get("format") or ""),
                        str(runtime.get("os") or ""),
                        str(runtime.get("arch") or ""),
                    )
                    path = str(runtime.get("binary_path") or "").strip()
                    if not path:
                        raise ValueError("version_unavailable")
                    require_binary_file_integrity(
                        runtime, path, cache=integrity_cache
                    )
                except (OSError, TypeError, ValueError) as exc:
                    raise ValueError("发布时 Bot 冻结版本文件不可用") from exc
                version_by_bot[bot_id] = (
                    int(version["id"]) if version is not None else None
                )

            encounter_rows: dict[tuple[str, int, int], list[tuple[int, int]]] = {}
            for source in pairing_rows:
                if source.get("stage_idx", 0) not in (0, None):
                    raise ValueError("首阶段抽签只能写入 stage_idx=0")
                first = source.get("entry_a_id")
                second = source.get("entry_b_id")
                group_id = source.get("group_id")
                if (
                    isinstance(first, bool)
                    or not isinstance(first, int)
                    or isinstance(second, bool)
                    or not isinstance(second, int)
                    or first == second
                    or entry_group.get(first) != group_id
                    or entry_group.get(second) != group_id
                    or source.get("bot_a_id") != entry_by_id[first]["bot_id"]
                    or source.get("bot_b_id") != entry_by_id[second]["bot_id"]
                ):
                    raise ValueError("首阶段对阵与冻结分组不一致")
                pair = tuple(sorted((first, second)))
                encounter_rows.setdefault((str(group_id), *pair), []).append((first, second))
                for bot_key, version_key in (
                    ("bot_a_id", "bot_a_version_id"),
                    ("bot_b_id", "bot_b_version_id"),
                ):
                    bot_id = source.get(bot_key)
                    if (
                        isinstance(bot_id, bool)
                        or not isinstance(bot_id, int)
                        or bot_id not in version_by_bot
                        or source.get(version_key) != version_by_bot[bot_id]
                    ):
                        raise ValueError("Bot 当前版本在发布期间已变化")
            expected_pairs = sum(size * (size - 1) // 2 for size in group_sizes.values())
            if len(encounter_rows) != expected_pairs or len(pairing_rows) != expected_pairs * 2:
                raise ValueError("首阶段分组双循环赛程不完整")
            for orientations in encounter_rows.values():
                if len(orientations) != 2 or orientations[0] != tuple(reversed(orientations[1])):
                    raise ValueError("首阶段每对选手必须换边各赛一局")

            source_snapshot = snapshot.get("source")
            if contest["template_id"] == "gomoku_seeded_group_drr_final":
                if len(entry_rows) not in {22, 23, 24, 25, 26}:
                    raise ValueError("保护种子赛事仅允许 22–26 人")
                if not isinstance(source_snapshot, dict) or set(source_snapshot) != {"contest_id", "protected"}:
                    raise ValueError("保护种子来源快照无效")
                if source_snapshot.get("contest_id") != contest["source_contest_id"]:
                    raise ValueError("保护种子来源已变化")
                source_contest_id = exact_nonnegative_int(
                    contest["source_contest_id"]
                )
                try:
                    if source_contest_id is None or source_contest_id < 1:
                        raise ValueError("保护种子来源身份无效")
                    (
                        source,
                        source_entry_rows,
                        source_stage_entry_ids,
                        source_legacy_entry_groups,
                    ) = (
                        _official_result_validation_context_tx(
                            c, source_contest_id
                        )
                    )
                    if (
                        source["game_id"] != contest["game_id"]
                        or source["status"] != CONTEST_FINISHED
                        or exact_sqlite_bool(
                            source["official_results_ready"]
                        )
                        is not True
                    ):
                        raise ValueError("保护种子来源状态无效")
                    persisted_official = c.execute(
                        "SELECT * FROM contest_official_results "
                        "WHERE contest_id=? ORDER BY rank",
                        (source_contest_id,),
                    ).fetchall()
                    official = _validate_complete_official_results(
                        persisted_official,
                        contest_id=source_contest_id,
                        contest=source,
                        roster_rows=source_entry_rows,
                        stage_entry_ids=source_stage_entry_ids,
                        legacy_entry_groups=source_legacy_entry_groups,
                    )
                except ValueError as exc:
                    raise ValueError(
                        "保护种子来源正式榜已变化或损坏"
                    ) from exc
                protected_rows = source_snapshot.get("protected")
                if not isinstance(protected_rows, list) or any(
                    not isinstance(row, dict)
                    or set(row) != {"entry_id", "user_id", "source_entry_id", "source_rank"}
                    or any(
                        isinstance(row.get(key), bool)
                        or not isinstance(row.get(key), int)
                        or row[key] < 1
                        for key in (
                            "entry_id",
                            "user_id",
                            "source_entry_id",
                            "source_rank",
                        )
                    )
                    or row["entry_id"] not in all_entry_ids
                    for row in protected_rows
                ):
                    raise ValueError("保护种子来源冻结不一致")
                expected_protected_count = 4 if len(entry_rows) <= 24 else 5
                # The manager prepares a candidate draw outside SQLite, but the
                # source official table may be replaced by another process before
                # this transaction acquires its write lock.  Recompute the exact
                # first N *currently registered* source finishers here rather than
                # merely checking that the stale selected tuples still exist.
                # This closes the gap where an absent source entrant and a lower
                # registered entrant exchange ranks while all stale protected
                # tuples remain individually valid.
                target_entry_by_user = {
                    int(row["user_id"]): int(row["id"])
                    for row in stored_entries
                }
                expected_protected: list[dict[str, int]] = []
                for source_row in official:
                    target_entry_id = target_entry_by_user.get(
                        int(source_row["user_id"])
                    )
                    if target_entry_id is None:
                        continue
                    expected_protected.append(
                        {
                            "entry_id": target_entry_id,
                            "user_id": int(source_row["user_id"]),
                            "source_entry_id": int(source_row["entry_id"]),
                            "source_rank": int(source_row["rank"]),
                        }
                    )
                    if len(expected_protected) == expected_protected_count:
                        break
                if (
                    len(expected_protected) != expected_protected_count
                    or protected_rows != expected_protected
                ):
                    raise ValueError("保护种子来源冻结不一致")
                protected_groups = {
                    entry_group[row["entry_id"]]
                    for row in protected_rows
                    if isinstance(row, dict)
                    and isinstance(row.get("entry_id"), int)
                    and not isinstance(row.get("entry_id"), bool)
                    and row["entry_id"] in entry_group
                }
                if (
                    len(protected_rows) != expected_protected_count
                    or group_count != expected_protected_count
                    or len(protected_groups) != expected_protected_count
                    or [row["source_rank"] for row in protected_rows]
                    != sorted(row["source_rank"] for row in protected_rows)
                    or any(
                        supplied_by_id[row["entry_id"]].get("user_id")
                        != row["user_id"]
                        for row in protected_rows
                    )
                ):
                    raise ValueError("保护种子数量、顺延顺序或分组不一致")
                expected_totals = {22: 156, 23: 166, 24: 176, 25: 190, 26: 200}
                final_scope = (
                    frozen_stages[1].get("ranking_scope")
                    if isinstance(frozen_stages, list)
                    and len(frozen_stages) == 2
                    and isinstance(frozen_stages[1], dict)
                    else None
                )
                frozen_total = snapshot.get("expected_match_count")
                if (
                    isinstance(final_scope, bool)
                    or not isinstance(final_scope, int)
                    or final_scope != expected_protected_count * 2
                    or frozen_total != expected_totals[len(entry_rows)]
                    or len(pairing_rows) + final_scope * (final_scope - 1)
                    != frozen_total
                ):
                    raise ValueError("保护种子人数带或总赛程快照不一致")
            elif source_snapshot is not None or "expected_match_count" in snapshot:
                raise ValueError("普通随机分组快照不得携带保护种子数据")

            entry_updates = [
                (source["group_id"], source["seed"], entry_id, contest_id)
                for entry_id, source in supplied_by_id.items()
            ]
            updated_entries = c.executemany(
                "UPDATE contest_entries SET group_id=?,seed=?,eliminated=0 "
                "WHERE id=? AND contest_id=?",
                entry_updates,
            )
            if updated_entries.rowcount != len(entry_updates):
                raise ValueError("抽签期间报名名册已变化")
            changed = c.execute(
                "UPDATE contests SET status=?,registration_opens_at=?,"
                "registration_closes_at=?,starts_at=?,stages_json=?,"
                "format_snapshot_json=?,published_stage_pairing_count=?,"
                "current_stage_idx=0,rest_ends_at=NULL "
                "WHERE id=? AND status=? AND stages_json=? "
                "AND time_control_id IS ?",
                (
                    CONTEST_PUBLISHED,
                    registration_opens_at,
                    registration_closes_at,
                    starts_at,
                    stages_json,
                    format_snapshot_json,
                    len(pairing_rows),
                    contest_id,
                    expected_status,
                    expected_stages_json,
                    expected_time_control_id,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("赛事发布快照已变化，请刷新后重试")
            placeholders = ",".join("?" for _ in columns)
            pairing_values: list[tuple[Any, ...]] = []
            for source, (series_index, series_size) in zip(pairing_rows, normalized_series):
                row = {
                    "contest_id": contest_id,
                    "round_num": source.get("round_num", 1),
                    "entry_a_id": source.get("entry_a_id"),
                    "entry_b_id": source.get("entry_b_id"),
                    "bot_a_id": source.get("bot_a_id"),
                    "bot_b_id": source.get("bot_b_id"),
                    "bot_a_version_id": source.get("bot_a_version_id"),
                    "bot_b_version_id": source.get("bot_b_version_id"),
                    "pairing_seed": source.get("pairing_seed"),
                    "published_at": source.get("published_at"),
                    "scheduled_at": source.get("scheduled_at"),
                    "match_id": None,
                    "status": source.get("status") or STATUS_PENDING,
                    "stage_idx": 0,
                    "stage_key": source.get("stage_key") or "",
                    "group_id": source.get("group_id") or "",
                    "bracket_slot": source.get("bracket_slot"),
                    "color_first": 0,
                    "series_index": series_index,
                    "series_size": series_size,
                    "tiebreak_group": source.get("tiebreak_group", 0),
                    "tiebreak_game": source.get("tiebreak_game", 0),
                }
                pairing_values.append(tuple(row[column] for column in columns))
            inserted_pairings = c.executemany(
                f"INSERT INTO contest_pairings({','.join(columns)}) VALUES({placeholders})",
                pairing_values,
            )
            if inserted_pairings.rowcount != len(pairing_values):
                raise RuntimeError("首阶段对阵批次未完整写入")
            sealed = c.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=? AND status=? "
                "AND current_stage_idx=0 AND published_stage_pairing_count=?",
                (contest_id, CONTEST_PUBLISHED, len(pairing_values)),
            )
            if sealed.rowcount != 1:
                raise ValueError("首阶段对阵拓扑冻结 CAS 已失效")
            return _contest_row(
                c.execute("SELECT * FROM contests WHERE id=?", (contest_id,)).fetchone()
            )

    def update_published_contest_schedule(
        self,
        contest_id: int,
        fields: dict[str, Any],
        *,
        stage_idx: int,
        pending_pairing_schedules: list[dict[str, Any]],
    ) -> dict | None:
        """原子修改 published 开赛时间与当前阶段待派对阵排期。

        manager 负责用发布时的唯一公式计算 ``scheduled_at``；Store 在
        ``BEGIN IMMEDIATE`` 后重新核对赛事状态、阶段、pairing ID/轮次及
        ``match_id``，再把赛事字段和全部 pending pairing 一次提交。这样
        admin 改 ``starts_at`` 不会留下“赛事新时间 + 对阵旧时间”的半状态，
        也不会覆盖另一个进程刚刚派发的真实对局。
        """
        allowed = {
            "title",
            "registration_opens_at",
            "registration_closes_at",
            "starts_at",
            "rest_ends_at",
        }
        unknown = set(fields).difference(allowed)
        if unknown:
            raise ValueError(
                f"published 赛事不能修改字段: {', '.join(sorted(unknown))}"
            )
        fields = dict(fields)
        if "title" in fields:
            fields["title"] = validate_contest_title(fields["title"])
        for key in (
            "registration_opens_at",
            "registration_closes_at",
            "starts_at",
            "rest_ends_at",
        ):
            if key in fields:
                validate_canonical_naive_timestamp(
                    fields[key], f"赛事 {key}", allow_none=True
                )
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None:
            raise ValueError("赛事阶段坐标必须是非负整数")
        if not isinstance(pending_pairing_schedules, list):
            raise ValueError("published 对阵重排计划必须是数组")
        plans: list[tuple[int, int, Any]] = []
        for row in pending_pairing_schedules:
            if not isinstance(row, dict):
                raise ValueError("published 对阵重排计划行类型无效")
            pairing_id = exact_nonnegative_int(row.get("id"))
            round_num = exact_nonnegative_int(row.get("round_num"))
            if (
                pairing_id is None
                or pairing_id < 1
                or round_num is None
                or round_num < 1
            ):
                raise ValueError("published 对阵重排计划坐标无效")
            plans.append((pairing_id, round_num, row.get("scheduled_at")))
        for _pairing_id, _round_num, scheduled_at in plans:
            validate_canonical_naive_timestamp(
                scheduled_at, "赛事对阵计划时间", allow_none=True
            )
        if len({pairing_id for pairing_id, _round, _schedule in plans}) != len(plans):
            raise ValueError("published 对阵重排计划包含重复 ID")
        plans.sort(key=lambda plan: (plan[0], plan[1]))

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not current:
                return None
            if current["status"] != CONTEST_PUBLISHED:
                raise ValueError("仅排期已发布赛事可以重排待开赛对局")
            if exact_nonnegative_int(current["current_stage_idx"]) != stage_idx:
                raise ValueError("赛事当前阶段已变化，拒绝重排")
            if not self._contest_stage_manifest_is_valid_tx(
                c,
                contest_id,
                stage_idx,
                include_terminal_orphans=True,
                require_manifest=True,
            ):
                raise ValueError("published 对阵 manifest 或 lifecycle seal 已变化")
            if c.execute(
                "SELECT 1 FROM contest_pairings "
                "WHERE contest_id=? AND match_id IS NOT NULL LIMIT 1",
                (contest_id,),
            ).fetchone():
                raise ValueError("赛事已有对局被派发，不能修改比赛开始时间")

            batch = c.execute(
                "SELECT id,round_num,status,match_id,entry_b_id,bot_b_id "
                "FROM contest_pairings WHERE contest_id=? AND stage_idx=? "
                "ORDER BY id",
                (contest_id, stage_idx),
            ).fetchall()
            manifest_count = exact_nonnegative_int(
                current["published_stage_pairing_count"]
            )
            if manifest_count is None or len(batch) != manifest_count:
                raise ValueError("published 当前阶段不是完整对阵批次")
            current_shape: list[tuple[int, int]] = []
            for row in batch:
                pairing_id = exact_nonnegative_int(row["id"])
                round_num = exact_nonnegative_int(row["round_num"])
                if (
                    pairing_id is None
                    or pairing_id < 1
                    or round_num is None
                    or round_num < 1
                ):
                    raise ValueError("published 当前阶段对阵坐标损坏")
                if row["status"] == STATUS_PENDING and row["match_id"] is None:
                    current_shape.append((pairing_id, round_num))
                    continue
                if (
                    row["status"] == STATUS_COMPLETED
                    and row["match_id"] is None
                    and row["entry_b_id"] is None
                    and row["bot_b_id"] is None
                ):
                    # Swiss/KO odd rosters persist deterministic no-match byes
                    # inside the same manifest.  They are immutable completion
                    # artifacts, not schedules to rewrite.
                    continue
                raise ValueError("published 当前阶段含非 pending 或非轮空对阵")
            current_shape.sort()
            expected_shape = [
                (pairing_id, round_num)
                for pairing_id, round_num, _schedule in plans
            ]
            if current_shape != expected_shape:
                raise ValueError("published 对阵在重排期间已变化，拒绝覆盖")

            validate_contest_times(
                fields.get(
                    "registration_opens_at", current["registration_opens_at"]
                ),
                fields.get(
                    "registration_closes_at", current["registration_closes_at"]
                ),
                fields.get("starts_at", current["starts_at"]),
            )
            if fields:
                sets = ",".join(f"{key}=?" for key in fields)
                c.execute(
                    f"UPDATE contests SET {sets} WHERE id=?",
                    [*fields.values(), contest_id],
                )
            for pairing_id, _round_num, scheduled_at in plans:
                updated = c.execute(
                    "UPDATE contest_pairings SET scheduled_at=? "
                    "WHERE id=? AND contest_id=? AND stage_idx=? "
                    "AND status=? AND match_id IS NULL",
                    (
                        scheduled_at,
                        pairing_id,
                        contest_id,
                        stage_idx,
                        STATUS_PENDING,
                    ),
                )
                if updated.rowcount != 1:
                    raise ValueError("published 对阵在重排期间已被派发，拒绝覆盖")
            return _contest_row(
                c.execute(
                    "SELECT * FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
            )

    def list_contests(
        self, *, status: str | None = None, organizer_id: int | None = None,
        game_id: str | None = None, page: int | None = None, per_page: int = 20,
        exclude_statuses: list[str] | None = None,
        hidden_owner_id: int | None = None,
        exclude_showcases: bool = False,
    ) -> list[dict] | dict:
        """列赛事，并在分页 SQL 内完成隐藏状态的可见性过滤。

        ``exclude_statuses`` 非空时，匿名/普通用户（``hidden_owner_id=None``）
        始终排除这些状态，即使同时传了显式 ``status`` 也不能绕过。
        组织者传自己的 user id，则可额外看到“自己主办”的隐藏赛事，
        不会因 organizer 角色而看到他人草稿/已取消赛事。admin 调用方
        不传 ``exclude_statuses`` 即保持全见。条件必须在 SQL 分页前应用，
        不得拉取一页后再用 Python 裁剪（会使 total/页数泄漏且错位）。
        ``exclude_showcases`` 只用于真实赛事发现列表；演示快照仍保留在库中，
        并可通过已知详情链接读取其只读生命周期图。
        """
        with self._tx() as c:
            sql = "SELECT * FROM contests WHERE 1=1"
            params: list[Any] = []
            if status:
                sql += " AND status=?"
                params.append(status)
            if organizer_id is not None:
                sql += " AND organizer_id=?"
                params.append(organizer_id)
            if game_id:
                sql += " AND game_id=?"
                params.append(game_id)
            if exclude_showcases:
                sql += " AND showcase_key IS NULL"
            # 隐藏状态过滤与显式 status 可同时存在：例如访客显式查
            # draft 仍必须得到空集；组织者则只能看自己的 draft。
            if exclude_statuses:
                placeholders = ",".join("?" for _ in exclude_statuses)
                if hidden_owner_id is None:
                    sql += f" AND status NOT IN ({placeholders})"
                else:
                    sql += (
                        f" AND (status NOT IN ({placeholders}) OR organizer_id=?)"
                    )
                params.extend(exclude_statuses)
                if hidden_owner_id is not None:
                    params.append(hidden_owner_id)
            sql += " ORDER BY created_at DESC"
            if page is not None:
                if (
                    isinstance(page, bool)
                    or not isinstance(page, int)
                    or page < 1
                    or isinstance(per_page, bool)
                    or not isinstance(per_page, int)
                    or not 1 <= per_page <= 200
                ):
                    raise ValueError("赛事分页参数无效")
                pp = per_page
                if page - 1 > ((2**63 - 1) // pp):
                    raise ValueError("赛事分页偏移超出数据库边界")
                rows, total = _paginate(c, sql, tuple(params), page=page, per_page=pp)
                rows = [
                    row
                    for raw in rows
                    if (row := _contest_row(raw)) is not None
                ]
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
            return [
                row
                for raw in c.execute(sql, params)
                if (row := _contest_row(raw)) is not None
            ]

    def list_contest_source_candidates(
        self,
        *,
        game_id: str,
        query: str | None = None,
        limit: int = 50,
        source_kind: str = "protected_seed",
        hidden_owner_id: int | None = None,
        include_all_hidden: bool = False,
    ) -> dict[str, Any]:
        """Return one bounded source search without COUNT/OFFSET.

        Protected-seed sources require completed official results.  Navigation
        sources only require an existing same-game contest, while preserving
        the normal hidden-contest visibility boundary for organizers.
        """
        if (
            not isinstance(game_id, str)
            or not game_id
            or game_id != game_id.strip()
            or game_id not in _all_game_ids()
        ):
            raise ValueError("来源赛事游戏无效")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 50
        ):
            raise ValueError("来源赛事候选数量无效")
        if source_kind not in {"protected_seed", "navigation"}:
            raise ValueError("来源赛事候选类型无效")
        if not isinstance(include_all_hidden, bool):
            raise ValueError("来源赛事隐藏态权限无效")
        owner_id: int | None = None
        if source_kind == "navigation" and not include_all_hidden:
            owner_id = exact_nonnegative_int(hidden_owner_id)
            if owner_id is None or owner_id < 1:
                raise ValueError("来源赛事隐藏态所有者无效")
        if query is not None and not isinstance(query, str):
            raise ValueError("来源赛事搜索词类型无效")
        normalized_query = query.strip() if isinstance(query, str) else ""
        if len(query or "") > 100 or any(
            ord(char) < 32 or ord(char) == 127 for char in (query or "")
        ):
            raise ValueError("来源赛事搜索词无效")

        eligibility: str
        default_index: str
        search_index: str
        search_hint: str
        if source_kind == "protected_seed":
            eligibility = (
                "showcase_key IS NULL AND status='finished' "
                "AND typeof(official_results_ready)='integer' "
                "AND official_results_ready=1"
            )
            default_index = "idx_contests_source_default_protected"
            search_index = "idx_contests_source_protected"
            search_hint = "grams.is_protected=1"
        elif include_all_hidden:
            eligibility = "showcase_key IS NULL"
            default_index = "idx_contests_source_default_navigation_all"
            search_index = "idx_contests_source_navigation_all"
            search_hint = "grams.is_nonshowcase=1"
        else:
            # Organizer navigation is expressed as two disjoint indexed ranges
            # below.  Parameterizing the hidden enum literals would prevent
            # SQLite from proving the partial-index predicate.
            eligibility = ""
            default_index = ""
            search_index = ""
            search_hint = ""

        exact_id: int | None = None
        escaped_query: str | None = None
        anchor: str | None = None
        anchor_len: int | None = None
        if normalized_query:
            if normalized_query.isascii() and normalized_query.isdecimal():
                exact_id = int(normalized_query)
                if exact_id < 1 or exact_id > 2**63 - 1:
                    return {"items": [], "has_more": False}
            else:
                escaped_query = (
                    normalized_query.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                anchor_len = min(3, len(normalized_query))
                # A long query commonly starts with a generic tournament word.
                # Anchor on its trailing trigram so an absent specific suffix
                # can prove an empty result without walking the common prefix.
                anchor = normalized_query[-anchor_len:]

        def branch_sql(
            *,
            branch_eligibility: str,
            branch_default_index: str,
            branch_search_index: str,
            branch_search_hint: str,
            branch_params: list[Any],
            branch_search_params: list[Any],
            branch_limit: bool,
        ) -> tuple[str, list[Any]]:
            params = list(branch_params)
            if exact_id is not None:
                sql = (
                    "SELECT id,title,created_at FROM contests WHERE id=? AND "
                    + branch_eligibility
                )
                return sql, [exact_id, *params]
            if anchor is not None and anchor_len is not None:
                sql = (
                    "SELECT c.id,c.title,grams.created_at "
                    "FROM contest_source_search_grams grams INDEXED BY "
                    + branch_search_index
                    + " CROSS JOIN contests c WHERE "
                    + branch_search_hint
                    + " AND grams.gram_len=? AND grams.gram=? "
                    "AND grams.game_id=? AND c.id=grams.contest_id "
                    "AND c.created_at=grams.created_at AND "
                    + branch_eligibility.replace(
                        "showcase_key", "c.showcase_key"
                    ).replace("status", "c.status").replace(
                        "official_results_ready", "c.official_results_ready"
                    ).replace("organizer_id", "c.organizer_id").replace(
                        "game_id", "c.game_id"
                    )
                    + " AND c.title LIKE ? ESCAPE '\\'"
                )
                params = [
                    *branch_search_params,
                    anchor_len,
                    anchor,
                    game_id,
                    *params,
                    f"%{escaped_query}%",
                ]
            else:
                sql = (
                    "SELECT id,title,created_at FROM contests INDEXED BY "
                    + branch_default_index
                    + " "
                    "WHERE " + branch_eligibility
                )
            sql += (
                " ORDER BY grams.created_at DESC,grams.contest_id DESC"
                if anchor is not None
                else " ORDER BY created_at DESC,id DESC"
            )
            if branch_limit:
                sql += " LIMIT ?"
                params.append(limit + 1)
            return sql, params

        if source_kind == "navigation" and not include_all_hidden:
            public_sql, public_params = branch_sql(
                branch_eligibility=(
                    "game_id=? AND showcase_key IS NULL "
                    "AND status NOT IN ('draft','cancelled')"
                ),
                branch_default_index=(
                    "idx_contests_source_default_navigation_public"
                ),
                branch_search_index="idx_contests_source_navigation_public",
                branch_search_hint="grams.is_nav_public=1",
                branch_params=[game_id],
                branch_search_params=[],
                branch_limit=True,
            )
            owner_sql, owner_params = branch_sql(
                branch_eligibility=(
                    "organizer_id=? AND game_id=? AND showcase_key IS NULL "
                    "AND status IN ('draft','cancelled')"
                ),
                branch_default_index=(
                    "idx_contests_source_default_navigation_owner"
                ),
                branch_search_index="idx_contests_source_navigation_owner",
                branch_search_hint="grams.is_nav_hidden=1 AND grams.organizer_id=?",
                branch_params=[owner_id, game_id],
                branch_search_params=[owner_id],
                branch_limit=True,
            )
            with self._tx() as c:
                # A deferred transaction pins the public and owner ranges to
                # one read snapshot without blocking a concurrent WAL writer.
                # Otherwise an open -> draft transition between these two
                # SELECTs can return the same contest from both branches and
                # manufacture a false ``has_more`` result at the page limit.
                c.execute("BEGIN")
                public_rows = [dict(row) for row in c.execute(public_sql, public_params)]
                owner_rows = [dict(row) for row in c.execute(owner_sql, owner_params)]
            raw_rows = sorted(
                [*public_rows, *owner_rows],
                key=lambda row: (str(row["created_at"]), int(row["id"])),
                reverse=True,
            )[: limit + 1]
        else:
            sql, params = branch_sql(
                branch_eligibility="game_id=? AND " + eligibility,
                branch_default_index=default_index,
                branch_search_index=search_index,
                branch_search_hint=search_hint,
                branch_params=[game_id],
                branch_search_params=[],
                branch_limit=True,
            )
            with self._tx() as c:
                raw_rows = c.execute(sql, params).fetchall()

        has_more = len(raw_rows) > limit
        items: list[dict[str, Any]] = []
        for raw in raw_rows[:limit]:
            row = dict(raw)
            contest_id = exact_nonnegative_int(row.get("id"))
            title = row.get("title")
            if (
                contest_id is None
                or contest_id < 1
                or not isinstance(title, str)
                or not title
                or title != title.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in title)
            ):
                raise ValueError("来源赛事候选数据损坏")
            items.append({"id": contest_id, "title": title})
        return {"items": items, "has_more": has_more}

    # ── contest_entries ───────────────────────────────────────

    def add_entry(self, contest_id: int, user_id: int, bot_id: int) -> dict:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            _require_live_contest_bot_tx(c, bot_id)
            # sqlite3's default deferred mode does not start a transaction for
            # SELECT.  Reserve the writer before reading the contest/profile so
            # the six identity columns and the entry share one linearization point.
            registered_at = _now()
            identity = _registration_identity_tx(
                c, contest_id, user_id, captured_at=registered_at
            )
            cur = c.execute(
                "INSERT INTO contest_entries(contest_id, user_id, bot_id, "
                "registered_at, real_name_snapshot, phone_snapshot, school_snapshot, "
                "student_id_snapshot, identity_captured_at, identity_source) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (contest_id, user_id, bot_id, registered_at, *identity),
            )
            eid = cur.lastrowid
            return _row(
                c.execute(
                    "SELECT * FROM contest_entries WHERE id=?", (eid,)
                ).fetchone()
            )

    add_contest_entry = add_entry

    def add_contest_entry_once(
        self, contest_id: int, user_id: int, bot_id: int
    ) -> dict:
        """原子新增一条用户报名，重复报名统一抛业务 ``ValueError``。

        ``ContestManager.register`` 的资格校验与写入之间可能被另一请求穿插；
        这里在单个 Store 事务内用唯一键冲突策略收口，避免并发重复报名把
        ``sqlite3.IntegrityError`` 泄漏成 500。调用方只有拿到新行后才可执行 XP 等
        后续副作用。
        """
        with self._tx() as c:
            # Must precede the contest-status, duplicate and identity reads.
            # _tx() itself deliberately does not BEGIN, and this method is not
            # called from another Store transaction, so this is not nested.
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status,require_real_name,game_id FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest or contest["status"] != CONTEST_OPEN:
                raise ValueError("比赛未开放报名")
            _require_active_contest_user_tx(c, user_id)
            _require_current_runnable_contest_bot_tx(c, bot_id)
            _require_contest_bot_binding_tx(
                c,
                contest_game_id=str(contest["game_id"]),
                user_id=user_id,
                bot_id=bot_id,
            )
            if c.execute(
                "SELECT 1 FROM contest_entries WHERE contest_id=? AND user_id=?",
                (contest_id, user_id),
            ).fetchone():
                raise ValueError("该用户在此比赛中已报名")
            registered_at = _now()
            identity = _registration_identity_tx(
                c, contest_id, user_id, captured_at=registered_at
            )
            cur = c.execute(
                "INSERT INTO contest_entries(contest_id, user_id, bot_id, registered_at, "
                "real_name_snapshot, phone_snapshot, school_snapshot, student_id_snapshot, "
                "identity_captured_at, identity_source) VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(contest_id, user_id) DO NOTHING",
                (contest_id, user_id, bot_id, registered_at, *identity),
            )
            if cur.rowcount != 1:
                raise ValueError("该用户在此比赛中已报名")
            return _row(
                c.execute(
                    "SELECT * FROM contest_entries WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def add_contest_roster_entries(
        self,
        contest_id: int,
        entries: list[tuple[int, int]],
        *,
        allow_real_name_override: bool = False,
        return_identity_required: bool = False,
    ) -> tuple[list[dict], list[int]] | tuple[list[dict], list[int], bool]:
        """Add a proxy roster atomically, with explicit admin PII override.

        A real-name contest may only use this proxy path when an already
        authorized admin caller passes ``allow_real_name_override=True``.  Normal
        self-registration uses :meth:`add_contest_entry_once` instead.  The Store
        repeats this gate after acquiring the database writer lock so an API or
        Manager pre-check cannot be invalidated by another process toggling the
        contest's identity requirement.
        """
        with self._tx() as c:
            # Reserve the writer before every contest/duplicate/identity SELECT.
            # See add_contest_entry_once: _tx() has not opened a transaction here.
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status,require_real_name,game_id FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            if (
                int(contest["require_real_name"] or 0)
                and not allow_real_name_override
            ):
                raise ContestRealNameRosterForbidden()
            added: list[dict] = []
            skipped: list[int] = []
            for user_id, bot_id in entries:
                try:
                    _require_active_contest_user_tx(c, user_id)
                    _require_current_runnable_contest_bot_tx(c, bot_id)
                    _require_contest_bot_binding_tx(
                        c,
                        contest_game_id=str(contest["game_id"]),
                        user_id=user_id,
                        bot_id=bot_id,
                    )
                except ValueError as exc:
                    raise ContestRosterWriteValidationError(
                        str(exc),
                        identity_required_at_commit=bool(
                            int(contest["require_real_name"] or 0)
                        ),
                    ) from exc
                if c.execute(
                    "SELECT 1 FROM contest_entries WHERE contest_id=? AND user_id=?",
                    (contest_id, user_id),
                ).fetchone():
                    skipped.append(user_id)
                    continue
                registered_at = _now()
                try:
                    identity = _registration_identity_tx(
                        c, contest_id, user_id, captured_at=registered_at
                    )
                except ValueError as exc:
                    raise ContestRosterWriteValidationError(
                        str(exc),
                        identity_required_at_commit=bool(
                            int(contest["require_real_name"] or 0)
                        ),
                    ) from exc
                cur = c.execute(
                    "INSERT INTO contest_entries(contest_id, user_id, bot_id, registered_at, "
                    "real_name_snapshot, phone_snapshot, school_snapshot, "
                    "student_id_snapshot, identity_captured_at, identity_source) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(contest_id, user_id) DO NOTHING",
                    (contest_id, user_id, bot_id, registered_at, *identity),
                )
                if cur.rowcount != 1:
                    skipped.append(user_id)
                    continue
                added.append(_row(c.execute(
                    "SELECT * FROM contest_entries WHERE id=?", (cur.lastrowid,)
                ).fetchone()))
            result = (added, skipped)
            if return_identity_required:
                # This bit was read after BEGIN IMMEDIATE and therefore names
                # the same linearization point as every captured snapshot.
                return (*result, bool(int(contest["require_real_name"] or 0)))
            return result

    def delete_contest_roster_entry(self, contest_id: int, user_id: int) -> bool:
        """组织者/admin 删除名册；状态复核与 DELETE 同一事务。"""
        with self._tx() as c:
            contest = c.execute(
                "SELECT status,game_id,stages_json FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            cur = c.execute(
                "DELETE FROM contest_entries WHERE contest_id=? AND user_id=?",
                (contest_id, user_id),
            )
            return cur.rowcount > 0

    def update_entry(self, contest_id: int, user_id: int, **fields: Any) -> dict | None:
        allowed = {"bot_id", "group_id", "seed", "eliminated", "dispatched_at"}
        if "eliminated" in fields:
            eliminated = fields["eliminated"]
            if (
                isinstance(eliminated, bool)
                or not isinstance(eliminated, int)
                or eliminated not in (0, 1)
            ):
                raise ValueError("eliminated 仅允许整数 0 或 1")
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            if "bot_id" in fields:
                c.execute("BEGIN IMMEDIATE")
                contest = c.execute(
                    "SELECT status FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
                if contest is not None and contest["status"] in (
                    CONTEST_PUBLISHED,
                    CONTEST_RUNNING,
                    CONTEST_REST,
                ):
                    raise ValueError(
                        "已发布或进行中赛事换 Bot 必须走原子重封入口"
                    )
                _require_live_contest_bot_tx(c, int(fields["bot_id"]))
            if sets:
                vals.extend([contest_id, user_id])
                c.execute(
                    f"UPDATE contest_entries SET {','.join(sets)} "
                    "WHERE contest_id=? AND user_id=?",
                    vals,
                )
            return _row(
                c.execute(
                    "SELECT * FROM contest_entries WHERE contest_id=? AND user_id=?",
                    (contest_id, user_id),
                ).fetchone()
            )

    def swap_contest_entry_bot_and_reseal(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        expected_status: str,
        expected_current_stage_idx: int,
        expected_old_bot_id: int | None,
        expected_entries: list[dict[str, Any]],
        expected_revision: int | None,
        expected_game_id: str,
        expected_bot_current_version: int,
        expected_stage_groups: dict[int, str] | None = None,
        dispatched_at: str,
    ) -> dict:
        """Atomically swap one roster Bot and preserve a trustworthy seal.

        ``published`` updates every not-yet-bound current pairing together with
        its frozen executable version.  ``rest`` deliberately leaves the
        completed historical stage untouched; its immutable decision may still
        name the old Bot and is validated before the roster mutation.
        """
        stage_idx = exact_nonnegative_int(expected_current_stage_idx)
        normalized_bot_id = exact_nonnegative_int(bot_id)
        normalized_user_id = exact_nonnegative_int(user_id)
        normalized_old_bot_id = (
            exact_nonnegative_int(expected_old_bot_id)
            if expected_old_bot_id is not None
            else None
        )
        normalized_revision = (
            exact_nonnegative_int(expected_revision)
            if expected_revision is not None
            else None
        )
        normalized_bot_current_version = exact_nonnegative_int(
            expected_bot_current_version
        )
        if (
            stage_idx is None
            or normalized_bot_id is None
            or normalized_bot_id < 1
            or normalized_user_id is None
            or normalized_user_id < 1
            or (
                expected_old_bot_id is not None
                and (
                    normalized_old_bot_id is None
                    or normalized_old_bot_id < 1
                )
            )
            or expected_status
            not in (CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED, CONTEST_REST)
            or not isinstance(expected_game_id, str)
            or not expected_game_id
            or normalized_bot_current_version is None
            or not isinstance(dispatched_at, str)
            or not dispatched_at
        ):
            raise ValueError("赛事换 Bot CAS 坐标无效")
        sealed_status = expected_status in (CONTEST_PUBLISHED, CONTEST_REST)
        if sealed_status and normalized_revision is None:
            raise ValueError("已发布赛事换 Bot 缺少 lifecycle revision")

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if (
                contest is None
                or contest["status"] != expected_status
                or exact_nonnegative_int(contest["current_stage_idx"])
                != stage_idx
                or contest["game_id"] != expected_game_id
            ):
                raise ValueError("赛事换 Bot 状态、阶段游标或游戏已变化")

            _validate_expected_contest_entries_tx(
                c, contest_id, expected_entries
            )
            entry = c.execute(
                "SELECT * FROM contest_entries WHERE contest_id=? AND user_id=?",
                (contest_id, normalized_user_id),
            ).fetchone()
            if (
                entry is None
                or entry["bot_id"] != normalized_old_bot_id
            ):
                raise ValueError("赛事换 Bot 名册身份已变化")
            if c.execute(
                "SELECT 1 FROM contest_entries WHERE contest_id=? "
                "AND id<>? AND bot_id=? LIMIT 1",
                (contest_id, int(entry["id"]), normalized_bot_id),
            ).fetchone():
                raise ValueError("同一赛事不能重复派遣同一个 Bot")

            _require_active_contest_user_tx(c, normalized_user_id)
            _require_current_runnable_contest_bot_tx(c, normalized_bot_id)
            _require_contest_bot_binding_tx(
                c,
                contest_game_id=str(contest["game_id"]),
                user_id=normalized_user_id,
                bot_id=normalized_bot_id,
            )

            if sealed_status:
                revision = exact_nonnegative_int(
                    contest["pairing_topology_revision"]
                )
                sealed = exact_nonnegative_int(
                    contest["sealed_pairing_topology_revision"]
                )
                if (
                    revision is None
                    or revision != normalized_revision
                    or sealed != revision
                    or not self._contest_stage_manifest_is_valid_tx(
                        c,
                        contest_id,
                        stage_idx,
                        include_terminal_orphans=True,
                        require_manifest=True,
                    )
                ):
                    raise ValueError("赛事换 Bot lifecycle seal 已变化")
                if c.execute(
                    "SELECT 1 FROM contest_pairings WHERE contest_id=? "
                    "AND stage_idx>? LIMIT 1",
                    (contest_id, stage_idx),
                ).fetchone():
                    raise ValueError("赛事存在未来阶段对阵，拒绝换 Bot")
                if expected_status == CONTEST_REST:
                    stages = _loads_json(contest["stages_json"], default=None)
                    if (
                        not isinstance(stages, list)
                        or any(not isinstance(item, dict) for item in stages)
                        or stage_idx >= len(stages)
                        or stages[stage_idx].get(
                            "allow_bot_swap_in_rest", True
                        )
                        is not True
                    ):
                        raise ValueError("本阶段休息不允许换 Bot")
                    self._strict_stage_decision_tx(
                        c,
                        contest_id,
                        stage_idx,
                        expected_entries=expected_entries,
                        expected_stage_groups=expected_stage_groups,
                        allow_snapshot_bots=True,
                    )
                if c.execute(
                    "SELECT 1 FROM execution_jobs WHERE contest_id=? "
                    "AND status IN (?,?,?,?) LIMIT 1",
                    (
                        contest_id,
                        EXECUTION_QUEUED,
                        EXECUTION_STARTING,
                        EXECUTION_RUNNING,
                        EXECUTION_SETTLING,
                    ),
                ).fetchone():
                    raise ValueError("赛事仍有 active 执行请求，不能换 Bot")

            bot = c.execute(
                "SELECT owner_id,game_id,current_version FROM bots WHERE id=?",
                (normalized_bot_id,),
            ).fetchone()
            assert bot is not None  # runnable guard above owns missing-row errors
            current_version = exact_nonnegative_int(bot["current_version"])
            if (
                bot["owner_id"] != normalized_user_id
                or bot["game_id"] != expected_game_id
                or current_version is None
                or current_version != normalized_bot_current_version
            ):
                raise ValueError("Bot owner、游戏或当前版本已变化")
            version_id: int | None = None
            if current_version:
                version = c.execute(
                    "SELECT id FROM bot_versions WHERE bot_id=? AND version=?",
                    (normalized_bot_id, current_version),
                ).fetchone()
                if version is None:
                    raise ValueError("Bot 当前可执行版本不存在")
                version_id = int(version["id"])

            if expected_status == CONTEST_PUBLISHED:
                current_pairings = c.execute(
                    "SELECT * FROM contest_pairings WHERE contest_id=? "
                    "AND stage_idx=? "
                    "ORDER BY id",
                    (contest_id, stage_idx),
                ).fetchall()
                stage_type = _contest_stage_type(contest["stages_json"], stage_idx)
                for pairing in current_pairings:
                    if pairing["match_id"] is not None or (
                        pairing["status"] != STATUS_PENDING
                        and not (
                            pairing["status"] == STATUS_COMPLETED
                            and is_authoritative_no_opponent_pairing(
                                stage_type, dict(pairing)
                            )
                        )
                    ):
                        raise ValueError("赛事当前轮已开始，不能更换 Bot")
                affected = [
                    pairing
                    for pairing in current_pairings
                    if entry["id"]
                    in (pairing["entry_a_id"], pairing["entry_b_id"])
                ]
                for pairing in affected:
                    side_bot = (
                        pairing["bot_a_id"]
                        if pairing["entry_a_id"] == entry["id"]
                        else pairing["bot_b_id"]
                    )
                    if side_bot != normalized_old_bot_id:
                        raise ValueError("赛事已发布对阵与冻结名册 Bot 不一致")
                c.execute(
                    "UPDATE contest_pairings SET bot_a_id=?,bot_a_version_id=? "
                    "WHERE contest_id=? AND stage_idx=? AND entry_a_id=? "
                    "AND match_id IS NULL",
                    (
                        normalized_bot_id,
                        version_id,
                        contest_id,
                        stage_idx,
                        int(entry["id"]),
                    ),
                )
                c.execute(
                    "UPDATE contest_pairings SET bot_b_id=?,bot_b_version_id=? "
                    "WHERE contest_id=? AND stage_idx=? AND entry_b_id=? "
                    "AND match_id IS NULL",
                    (
                        normalized_bot_id,
                        version_id,
                        contest_id,
                        stage_idx,
                        int(entry["id"]),
                    ),
                )

            changed = c.execute(
                "UPDATE contest_entries SET bot_id=?,dispatched_at=? "
                "WHERE id=? AND contest_id=? AND user_id=? AND bot_id IS ?",
                (
                    normalized_bot_id,
                    dispatched_at,
                    int(entry["id"]),
                    contest_id,
                    normalized_user_id,
                    normalized_old_bot_id,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("赛事换 Bot 名册 CAS 已变化")

            if sealed_status:
                resealed = c.execute(
                    "UPDATE contests SET sealed_pairing_topology_revision="
                    "pairing_topology_revision WHERE id=? AND status=? "
                    "AND current_stage_idx=? "
                    "AND sealed_pairing_topology_revision=?",
                    (
                        contest_id,
                        expected_status,
                        stage_idx,
                        normalized_revision,
                    ),
                )
                if resealed.rowcount != 1:
                    raise ValueError("赛事换 Bot 后 lifecycle 重封 CAS 失败")
            updated = c.execute(
                "SELECT * FROM contest_entries WHERE id=?", (int(entry["id"]),)
            ).fetchone()
            if updated is None:  # pragma: no cover - same transaction
                raise RuntimeError("赛事换 Bot 后名册行消失")
            return _row(updated)

    def apply_contest_entry_advancement(
        self,
        contest_id: int,
        stage_idx: int,
        entry_updates: list[dict[str, Any]],
        *,
        expected_status: str,
        expected_current_stage_idx: int,
    ) -> list[dict]:
        """Reject advancement detached from its immutable stage decision.

        Production advancement is part of ``create_contest_stage_pairings`` or
        the terminal decision transaction.  Keeping a public standalone writer
        would allow a caller to change the active cohort without atomically
        moving the stage cursor and pairing batch.
        """
        raise ValueError("赛事晋级只能通过跨阶段专用原子事务推进")

    def list_entries(self, contest_id: int) -> list[dict]:
        with self._tx() as c:
            return [
                _row(r)
                for r in c.execute(
                    "SELECT * FROM contest_entries WHERE contest_id=? "
                    "ORDER BY registered_at",
                    (contest_id,),
                )
            ]

    list_contest_entries = list_entries

    def get_entry(self, contest_id: int, user_id: int) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM contest_entries WHERE contest_id=? AND user_id=?",
                    (contest_id, user_id),
                ).fetchone()
            )

    get_contest_entry = get_entry

    # ── contest_pairings ──────────────────────────────────────

    def add_pairing(
        self,
        contest_id: int,
        bot_a_id: int,
        bot_b_id: int,
        *,
        round_num: int = 1,
        match_id: str | None = None,
        status: str = "pending",
        stage_idx: int = 0,
        stage_key: str = "",
        group_id: str = "",
        bracket_slot: int | None = None,
        color_first: int = 0,
        entry_a_id: int | None = None,
        entry_b_id: int | None = None,
        bot_a_version_id: int | None = None,
        bot_b_version_id: int | None = None,
        pairing_seed: int | None = None,
        published_at: str | None = None,
        scheduled_at: str | None = None,
        series_index: int = 1,
        series_size: int = 1,
        tiebreak_group: int = 0,
        tiebreak_game: int = 0,
    ) -> dict:
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None:
            raise ValueError("赛事阶段坐标必须是非负整数")
        series_index, series_size = _pairing_series_fields(
            {"series_index": series_index, "series_size": series_size}
        )
        tiebreak_group, tiebreak_game = _pairing_tiebreak_fields(
            {
                "tiebreak_group": tiebreak_group,
                "tiebreak_game": tiebreak_game,
                "bracket_slot": bracket_slot,
                "bot_b_id": bot_b_id,
                "entry_b_id": entry_b_id,
            }
        )
        pairing_seed = _pairing_seed_field(
            {"pairing_seed": pairing_seed}, required=series_size > 1
        )
        if series_size > 1:
            raise ValueError("多场赛事对阵必须通过原子批次接口写入")
        if tiebreak_group:
            raise ValueError("淘汰决胜组必须通过专用原子追加接口写入")
        validate_canonical_naive_timestamp(
            published_at, "赛事对阵发布时间", allow_none=True
        )
        validate_canonical_naive_timestamp(
            scheduled_at, "赛事对阵计划时间", allow_none=True
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            _require_live_contest_pairing_bots_tx(
                c, contest_id, bot_a_id, bot_b_id
            )
            if pairing_seed is not None and c.execute(
                "SELECT 1 FROM contest_pairings WHERE contest_id=? AND stage_idx=? "
                "AND pairing_seed=? LIMIT 1",
                (contest_id, stage_idx, pairing_seed),
            ).fetchone():
                raise ValueError("多场赛事对阵 pairing_seed 不得重复")
            cur = c.execute(
                "INSERT INTO contest_pairings(contest_id, round_num, entry_a_id, "
                "entry_b_id, bot_a_id, bot_b_id, bot_a_version_id, bot_b_version_id, "
                "pairing_seed, published_at, scheduled_at, match_id, status, stage_idx, "
                "stage_key, group_id, bracket_slot, color_first,series_index,series_size,"
                "tiebreak_group,tiebreak_game) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    contest_id,
                    round_num,
                    entry_a_id,
                    entry_b_id,
                    bot_a_id,
                    bot_b_id,
                    bot_a_version_id,
                    bot_b_version_id,
                    pairing_seed,
                    published_at,
                    scheduled_at,
                    match_id,
                    status,
                    stage_idx,
                    stage_key,
                    group_id,
                    bracket_slot,
                    color_first,
                    series_index,
                    series_size,
                    tiebreak_group,
                    tiebreak_game,
                ),
            )
            pid = cur.lastrowid
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pid,)
                ).fetchone()
            )

    add_contest_pairing = add_pairing

    def ensure_contest_pairing_seed_for_enqueue(
        self,
        contest_id: int,
        pairing: dict[str, Any],
        *,
        expected_stages_json: Any,
    ) -> dict:
        """Return one frozen contest seed, repairing only safe legacy rows.

        This is the last write boundary before a duplicate execution request is
        enqueued.  Markerless and aggregate historical stages may predate
        ``pairing_seed``; their missing seed is allocated under
        ``BEGIN IMMEDIATE`` after rechecking the complete pairing snapshot.  A
        current independent-scoring stage or an elimination tiebreak is never
        repaired here: both were born with an explicit seed contract and must
        fail closed if that contract is missing or damaged.

        A valid existing seed is an idempotent read.  In particular, a retry
        may observe the already-enqueued job for that seed.  The active-job
        exclusion applies to the NULL -> seed CAS itself so an old request can
        never silently acquire a different seed after enqueue.
        """
        if not isinstance(pairing, dict):
            raise ValueError("赛事对阵快照损坏")
        pairing_id = exact_nonnegative_int(pairing.get("id"))
        if pairing_id is None or pairing_id < 1:
            raise ValueError("赛事对阵快照缺少合法 id")

        # list_contest_pairings() exposes effective legacy entry ids while
        # retaining the raw durable columns under these private keys.  The CAS
        # must compare the raw identity, not a derived compatibility view.
        expected = dict(pairing)
        for suffix in ("a", "b"):
            raw_key = f"_raw_entry_{suffix}_id"
            if raw_key in expected:
                expected[f"entry_{suffix}_id"] = expected[raw_key]
        frozen_fields = (
            "contest_id",
            "round_num",
            "entry_a_id",
            "entry_b_id",
            "bot_a_id",
            "bot_b_id",
            "bot_a_version_id",
            "bot_b_version_id",
            "published_at",
            "scheduled_at",
            "status",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
            "series_index",
            "series_size",
            "tiebreak_group",
            "tiebreak_game",
        )

        def same_frozen_value(actual: Any, wanted: Any) -> bool:
            if actual is None or wanted is None:
                return actual is wanted
            # SQLite integer and real values compare equal in Python (1 ==
            # 1.0), but coordinates and identities must not drift by type.
            return type(actual) is type(wanted) and actual == wanted

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_execution_admission_tx(c, maintenance_only=True)
            contest = c.execute(
                "SELECT status,current_stage_idx,stages_json,"
                "published_stage_pairing_count,pairing_topology_revision,"
                "sealed_pairing_topology_revision FROM contests WHERE id=?",
                (int(contest_id),),
            ).fetchone()
            if not contest or contest["status"] not in (
                CONTEST_PUBLISHED,
                CONTEST_RUNNING,
            ):
                raise ValueError("仅 active 赛事可冻结执行随机种子")
            if contest["stages_json"] != expected_stages_json:
                raise ValueError("赛事冻结阶段已变化，拒绝分配随机种子")

            current = c.execute(
                "SELECT * FROM contest_pairings WHERE id=? AND contest_id=?",
                (pairing_id, int(contest_id)),
            ).fetchone()
            if current is None:
                raise ValueError("赛事对阵已不存在")
            stage_idx = exact_nonnegative_int(current["stage_idx"])
            current_stage_idx = exact_nonnegative_int(contest["current_stage_idx"])
            if stage_idx is None or current_stage_idx != stage_idx:
                raise ValueError("赛事当前阶段已变化，拒绝分配随机种子")
            if current["status"] != STATUS_PENDING or current["match_id"] is not None:
                raise ValueError("赛事对阵已开始，拒绝分配随机种子")
            if any(
                field not in expected
                or not same_frozen_value(current[field], expected[field])
                for field in frozen_fields
            ):
                raise ValueError("赛事对阵身份、版本或坐标已变化")
            if not self._contest_stage_manifest_is_valid_tx(
                c,
                int(contest_id),
                stage_idx,
                include_terminal_orphans=True,
                require_manifest=True,
            ):
                raise ValueError("赛事 active 对阵批次完整性校验失败")

            try:
                stages = json.loads(contest["stages_json"])
            except (TypeError, ValueError):
                stages = None
            stage = (
                stages[stage_idx]
                if isinstance(stages, list)
                and stage_idx < len(stages)
                and isinstance(stages[stage_idx], dict)
                else None
            )
            if stage is None:
                raise ValueError("赛事冻结阶段损坏")

            tiebreak_group, _ = _pairing_tiebreak_fields(_row(current))
            marker = stage.get("series_scoring")
            if tiebreak_group > 0 and not (
                stage.get("type") == "single_elimination"
                and stage.get("tiebreak")
                == ELIMINATION_TIEBREAK_PAIRED_SWAP
                and stage.get("duplicate", False) is False
            ):
                raise ValueError("淘汰决胜对阵冻结阶段契约损坏")
            seed = current["pairing_seed"]
            if seed is not None:
                seed = _pairing_seed_field(
                    {"pairing_seed": seed}, required=True
                )
                same_seed = c.execute(
                    "SELECT id,round_num,bracket_slot,tiebreak_group,tiebreak_game,"
                    "entry_a_id,entry_b_id,bot_a_id,bot_b_id,"
                    "bot_a_version_id,bot_b_version_id "
                    "FROM contest_pairings WHERE contest_id=? AND stage_idx=? "
                    "AND pairing_seed=? ORDER BY id",
                    (int(contest_id), stage_idx, seed),
                ).fetchall()
                if tiebreak_group == 0:
                    unique = (
                        len(same_seed) == 1
                        and exact_nonnegative_int(same_seed[0]["id"])
                        == pairing_id
                    )
                else:
                    frozen_round = exact_nonnegative_int(current["round_num"])
                    frozen_slot = exact_nonnegative_int(current["bracket_slot"])
                    coordinate_valid = bool(
                        frozen_round is not None
                        and frozen_round >= 1
                        and frozen_slot is not None
                        and len(same_seed) == 2
                        and {
                            exact_nonnegative_int(row["tiebreak_game"])
                            for row in same_seed
                        }
                        == {1, 2}
                        and all(
                            exact_nonnegative_int(row["round_num"])
                            == frozen_round
                            and exact_nonnegative_int(row["bracket_slot"])
                            == frozen_slot
                            and exact_nonnegative_int(row["tiebreak_group"])
                            == tiebreak_group
                            for row in same_seed
                        )
                    )
                    by_game = (
                        {
                            exact_nonnegative_int(row["tiebreak_game"]): row
                            for row in same_seed
                        }
                        if coordinate_valid
                        else {}
                    )
                    primary = (
                        c.execute(
                            "SELECT entry_a_id,entry_b_id,bot_a_id,bot_b_id,"
                            "bot_a_version_id,bot_b_version_id "
                            "FROM contest_pairings WHERE contest_id=? AND stage_idx=? "
                            "AND round_num=? AND bracket_slot=? "
                            "AND tiebreak_group=0 AND tiebreak_game=0",
                            (
                                int(contest_id),
                                stage_idx,
                                frozen_round,
                                frozen_slot,
                            ),
                        ).fetchall()
                        if coordinate_valid
                        else []
                    )
                    identity_fields = (
                        "entry_a_id",
                        "entry_b_id",
                        "bot_a_id",
                        "bot_b_id",
                        "bot_a_version_id",
                        "bot_b_version_id",
                    )
                    swapped_fields = (
                        "entry_b_id",
                        "entry_a_id",
                        "bot_b_id",
                        "bot_a_id",
                        "bot_b_version_id",
                        "bot_a_version_id",
                    )
                    unique = bool(
                        coordinate_valid
                        and len(primary) == 1
                        and all(
                            same_frozen_value(by_game[1][field], primary[0][field])
                            and same_frozen_value(
                                by_game[2][field], primary[0][swapped]
                            )
                            for field, swapped in zip(
                                identity_fields, swapped_fields
                            )
                        )
                    )
                if not unique:
                    raise ValueError("赛事对阵私密冻结 seed 被其他坐标复用")
                return _row(current)

            if tiebreak_group > 0:
                raise ValueError("淘汰决胜对阵缺少私密冻结 seed")
            if marker == "independent_scoring_game_points_v1":
                raise ValueError("独立计分对阵缺少私密冻结 seed")
            if marker not in (None, "aggregate_match_points_v1"):
                raise ValueError("赛事阶段 series_scoring 损坏")
            legacy_series = bool(
                marker == "aggregate_match_points_v1"
                or (marker is None and "games_per_pair" in stage)
            )
            if not legacy_series:
                # A markerless stage without games_per_pair is an ordinary
                # historical single Match.  It has no seed contract to repair.
                return _row(current)
            games_per_pair = exact_nonnegative_int(stage.get("games_per_pair"))
            if games_per_pair is None or games_per_pair < 1:
                raise ValueError("历史多场阶段 games_per_pair 损坏")
            raw_manifest = contest["published_stage_pairing_count"]
            reseal = raw_manifest is not None
            if not reseal and contest["status"] == CONTEST_PUBLISHED:
                raise ValueError("published 对阵批次未冻结，拒绝补写 seed")
            before_revision: int | None = None
            before_sealed: int | None = None
            manifest_count: int | None = None
            if reseal:
                if not self._contest_stage_manifest_is_valid_tx(
                    c,
                    int(contest_id),
                    stage_idx,
                    include_terminal_orphans=True,
                    require_manifest=True,
                ):
                    raise ValueError("赛事 active 对阵批次完整性校验失败")
                manifest_count = exact_nonnegative_int(raw_manifest)
                before_revision = exact_nonnegative_int(
                    contest["pairing_topology_revision"]
                )
                before_sealed = exact_nonnegative_int(
                    contest["sealed_pairing_topology_revision"]
                )
                if (
                    manifest_count is None
                    or before_revision is None
                    or before_sealed != before_revision
                ):
                    raise ValueError("赛事对阵拓扑冻结损坏")
            active = c.execute(
                "SELECT 1 FROM execution_jobs WHERE source='contest' "
                "AND contest_id=? "
                "AND status IN ('queued','starting','running','settling') LIMIT 1",
                (int(contest_id),),
            ).fetchone()
            if active:
                raise ValueError("赛事已有 active 执行请求，拒绝补写 seed")

            allocated: int | None = None
            for _ in range(16):
                candidate = secrets.randbelow(9_223_372_036_854_775_807) + 1
                if not c.execute(
                    "SELECT 1 FROM contest_pairings WHERE contest_id=? "
                    "AND stage_idx=? AND pairing_seed=? LIMIT 1",
                    (int(contest_id), stage_idx, candidate),
                ).fetchone():
                    allocated = candidate
                    break
            if allocated is None:
                raise RuntimeError("无法分配唯一的赛事随机种子")
            changed = c.execute(
                "UPDATE contest_pairings SET pairing_seed=? "
                "WHERE id=? AND contest_id=? AND status=? "
                "AND match_id IS NULL AND pairing_seed IS NULL",
                (
                    allocated,
                    pairing_id,
                    int(contest_id),
                    STATUS_PENDING,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("赛事对阵 seed CAS 已失效")
            if reseal:
                assert before_revision is not None
                assert before_sealed is not None
                assert manifest_count is not None
                resealed = c.execute(
                    "UPDATE contests SET sealed_pairing_topology_revision="
                    "pairing_topology_revision WHERE id=? AND status=? "
                    "AND current_stage_idx=? AND published_stage_pairing_count=? "
                    "AND pairing_topology_revision=? "
                    "AND sealed_pairing_topology_revision=?",
                    (
                        int(contest_id),
                        contest["status"],
                        stage_idx,
                        manifest_count,
                        before_revision + 1,
                        before_sealed,
                    ),
                )
                if resealed.rowcount != 1:
                    raise ValueError("赛事对阵 seed 补写拓扑冻结 CAS 已失效")
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing_id,)
                ).fetchone()
            )

    def create_contest_stage_pairings(
        self,
        contest_id: int,
        stage_idx: int,
        pairing_rows: list[dict[str, Any]],
        *,
        expected_current_stage_idx: int,
        expected_status: str | None = None,
        activate_running: bool = False,
        entry_updates: list[dict[str, Any]] | None = None,
        source_decision_revision: int | None = None,
        source_stage_groups: dict[int, str] | None = None,
    ) -> list[dict]:
        """Atomically persist one complete stage pairing batch and its state move.

        A stage is a single durability unit: no caller can observe only the first
        few pairings, and advancing ``current_stage_idx``/leaving ``rest`` is
        committed together with the complete batch.  ``BEGIN IMMEDIATE`` plus the
        expected-index check also protects against another process advancing the
        same contest after the manager's read.

        A pre-upgrade crash could have left an unbound partial batch for the *next*
        stage while the contest still points at the previous stage.  That exact
        shape is safe to replace inside this transaction.  Rows with a bound match
        or any other progress are rejected rather than silently overwritten.

        When ``entry_updates`` is supplied, it must describe the complete roster
        CAS used to compute the next-stage pairings.  The entry mutations, every
        pairing INSERT and the contest status/cursor transition then share this
        same transaction; a crash can no longer leave a half-eliminated roster.
        """
        if not pairing_rows:
            raise ValueError("赛事阶段对阵批次不能为空")
        _validate_pairing_publication_times(
            pairing_rows, require_published_at=True
        )
        stage_idx = exact_nonnegative_int(stage_idx)
        expected_current_stage_idx = exact_nonnegative_int(
            expected_current_stage_idx
        )
        if stage_idx is None or expected_current_stage_idx is None:
            raise ValueError("赛事阶段坐标必须是非负整数")
        if expected_status is not None and (
            not isinstance(expected_status, str) or not expected_status
        ):
            raise ValueError("赛事阶段状态 CAS 无效")
        normalized_entry_updates = (
            _contest_entry_advancement_batch(entry_updates)
            if entry_updates is not None
            else None
        )
        if normalized_entry_updates is not None and (
            stage_idx != expected_current_stage_idx + 1 or not activate_running
        ):
            raise ValueError("晋级名册只能与紧邻下一阶段的运行切换同批提交")
        normalized_source_revision = (
            exact_nonnegative_int(source_decision_revision)
            if source_decision_revision is not None
            else None
        )
        if (
            source_decision_revision is not None
            and normalized_source_revision is None
        ):
            raise ValueError("来源阶段决策 revision 无效")
        columns = (
            "contest_id",
            "round_num",
            "entry_a_id",
            "entry_b_id",
            "bot_a_id",
            "bot_b_id",
            "bot_a_version_id",
            "bot_b_version_id",
            "pairing_seed",
            "published_at",
            "scheduled_at",
            "match_id",
            "status",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
            "series_index",
            "series_size",
            "tiebreak_group",
            "tiebreak_game",
        )
        normalized_series = _pairing_series_batch(pairing_rows)
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_execution_admission_tx(c, maintenance_only=True)
            contest = c.execute(
                "SELECT status,current_stage_idx,stages_json,"
                "published_stage_pairing_count,pairing_topology_revision,"
                "sealed_pairing_topology_revision "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] in (CONTEST_FINISHED, CONTEST_CANCELLED):
                raise ValueError("终态赛事不能生成新阶段对阵")
            if expected_status is not None and contest["status"] != expected_status:
                raise ValueError("赛事状态已变化，拒绝生成新阶段对阵")
            current_idx = exact_nonnegative_int(contest["current_stage_idx"])
            if current_idx is None or current_idx != expected_current_stage_idx:
                raise ValueError("赛事当前阶段已变化，拒绝重复生成对阵")
            if stage_idx not in (current_idx, current_idx + 1):
                raise ValueError("赛事阶段只能生成当前阶段或紧邻的下一阶段")
            if stage_idx == current_idx + 1:
                if (
                    normalized_entry_updates is None
                    or normalized_source_revision is None
                ):
                    raise ValueError("跨阶段切换缺少完整名册或来源决策")
                current_revision = exact_nonnegative_int(
                    contest["pairing_topology_revision"]
                )
                sealed_revision = exact_nonnegative_int(
                    contest["sealed_pairing_topology_revision"]
                )
                if (
                    current_revision != normalized_source_revision
                    or sealed_revision != normalized_source_revision
                ):
                    raise ValueError("跨阶段来源决策 revision 已变化")
                source_entries = [
                    {
                        "id": row["id"],
                        "user_id": row["user_id"],
                        "bot_id": row["expected_bot_id"],
                        "seed": row["expected_seed"],
                        "group_id": row["expected_group_id"],
                        "eliminated": row["expected_eliminated"],
                    }
                    for row in normalized_entry_updates
                ]
                self._strict_stage_decision_tx(
                    c,
                    contest_id,
                    current_idx,
                    expected_entries=source_entries,
                    expected_stage_groups=source_stage_groups,
                    allow_snapshot_bots=(contest["status"] == CONTEST_REST),
                )
            manifest_enabled = (
                contest["published_stage_pairing_count"] is not None
            )
            install_initial_manifest = bool(
                not manifest_enabled
                and stage_idx == current_idx
                and contest["status"] == CONTEST_PUBLISHED
            )
            if (
                not manifest_enabled
                and not install_initial_manifest
            ):
                raise ValueError("active 当前阶段缺少冻结对阵批次")
            if (
                manifest_enabled
                and contest["status"] != CONTEST_PUBLISHED
                and not self._contest_stage_manifest_is_valid_tx(
                    c,
                    contest_id,
                    current_idx,
                    include_terminal_orphans=True,
                    require_manifest=True,
                )
            ):
                raise ValueError("赛事当前阶段对阵批次完整性校验失败，拒绝推进")
            stage_type = _contest_stage_type(contest["stages_json"], stage_idx)

            if normalized_entry_updates is not None:
                transition_entries = {
                    row["id"]: row for row in normalized_entry_updates
                }
                active_entry_ids = {
                    entry_id
                    for entry_id, row in transition_entries.items()
                    if row["eliminated"] == 0
                }
                paired_entry_ids: set[int] = set()
                for source in pairing_rows:
                    first = source.get("entry_a_id")
                    second = source.get("entry_b_id")
                    first_bot = source.get("bot_a_id")
                    second_bot = source.get("bot_b_id")
                    if (
                        isinstance(first, bool)
                        or not isinstance(first, int)
                        or first not in active_entry_ids
                        or first_bot
                        != transition_entries[first]["expected_bot_id"]
                    ):
                        raise ValueError("下一阶段对阵与晋级名册身份不一致")
                    paired_entry_ids.add(first)
                    if second is None:
                        if second_bot is not None:
                            raise ValueError("下一阶段轮空对阵的 Bot 身份不一致")
                    elif (
                        isinstance(second, bool)
                        or not isinstance(second, int)
                        or second not in active_entry_ids
                        or second == first
                        or second_bot
                        != transition_entries[second]["expected_bot_id"]
                    ):
                        raise ValueError("下一阶段对阵与晋级名册身份不一致")
                    else:
                        paired_entry_ids.add(second)
                if paired_entry_ids != active_entry_ids:
                    raise ValueError("下一阶段对阵未完整覆盖晋级名册")

            existing = c.execute(
                "SELECT id, entry_b_id, bot_b_id, match_id, status "
                "FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? ORDER BY id",
                (contest_id, stage_idx),
            ).fetchall()
            if existing and stage_idx == current_idx:
                raise ValueError("当前阶段对阵已存在，拒绝重复生成")
            if existing:
                if any(row["match_id"] is not None for row in existing):
                    raise ValueError("下一阶段已有绑定对局，不能覆盖")
                if any(
                    row["status"] not in (STATUS_PENDING, STATUS_COMPLETED)
                    or (
                        row["status"] == STATUS_COMPLETED
                        and not is_authoritative_no_opponent_pairing(
                            stage_type, _row(row)
                        )
                    )
                    for row in existing
                ):
                    raise ValueError("下一阶段已有运行进度，不能覆盖")
                c.execute(
                    "DELETE FROM contest_pairings WHERE contest_id=? AND stage_idx=?",
                    (contest_id, stage_idx),
                )

            if normalized_entry_updates is not None:
                _apply_contest_entry_advancement_tx(
                    c, contest_id, normalized_entry_updates
                )

            inserted: list[dict] = []
            placeholders = ",".join("?" for _ in columns)
            for source, (series_index, series_size) in zip(
                pairing_rows, normalized_series
            ):
                row = {
                    "contest_id": contest_id,
                    "round_num": int(source.get("round_num") or 1),
                    "entry_a_id": source.get("entry_a_id"),
                    "entry_b_id": source.get("entry_b_id"),
                    "bot_a_id": source.get("bot_a_id"),
                    "bot_b_id": source.get("bot_b_id"),
                    "bot_a_version_id": source.get("bot_a_version_id"),
                    "bot_b_version_id": source.get("bot_b_version_id"),
                    "pairing_seed": source.get("pairing_seed"),
                    "published_at": source.get("published_at"),
                    "scheduled_at": source.get("scheduled_at"),
                    "match_id": None,
                    "status": source.get("status") or STATUS_PENDING,
                    "stage_idx": stage_idx,
                    "stage_key": source.get("stage_key") or "",
                    "group_id": source.get("group_id") or "",
                    "bracket_slot": source.get("bracket_slot"),
                    "color_first": int(source.get("color_first") or 0),
                    "series_index": series_index,
                    "series_size": series_size,
                    "tiebreak_group": source.get("tiebreak_group", 0),
                    "tiebreak_game": source.get("tiebreak_game", 0),
                }
                cur = c.execute(
                    f"INSERT INTO contest_pairings({','.join(columns)}) "
                    f"VALUES({placeholders})",
                    tuple(row[column] for column in columns),
                )
                inserted.append(
                    _row(
                        c.execute(
                            "SELECT * FROM contest_pairings WHERE id=?",
                            (cur.lastrowid,),
                        ).fetchone()
                    )
                )

            if (
                stage_idx == current_idx
                and (
                    contest["status"] == CONTEST_PUBLISHED
                    or install_initial_manifest
                )
            ):
                manifest_where = (
                    " AND published_stage_pairing_count IS NULL"
                    if install_initial_manifest
                    else ""
                )
                sealed = c.execute(
                    "UPDATE contests SET published_stage_pairing_count=? "
                    "WHERE id=? AND status=? AND current_stage_idx=?"
                    + manifest_where,
                    (
                        len(inserted),
                        contest_id,
                        contest["status"],
                        stage_idx,
                    ),
                )
                if sealed.rowcount != 1:
                    raise ValueError("published 对阵批次计数冻结 CAS 已失效")

            if activate_running:
                if expected_status is None:
                    changed = c.execute(
                        "UPDATE contests SET status=?, current_stage_idx=?, "
                        "rest_ends_at=NULL WHERE id=? AND current_stage_idx=?",
                        (CONTEST_RUNNING, stage_idx, contest_id, current_idx),
                    )
                else:
                    changed = c.execute(
                        "UPDATE contests SET status=?, current_stage_idx=?, "
                        "rest_ends_at=NULL WHERE id=? AND current_stage_idx=? "
                        "AND status=?",
                        (
                            CONTEST_RUNNING,
                            stage_idx,
                            contest_id,
                            current_idx,
                            expected_status,
                        ),
                    )
                if changed.rowcount != 1:
                    raise ValueError("赛事阶段切换 CAS 已失效")
            elif stage_idx != current_idx:
                if expected_status is None:
                    changed = c.execute(
                        "UPDATE contests SET current_stage_idx=? "
                        "WHERE id=? AND current_stage_idx=?",
                        (stage_idx, contest_id, current_idx),
                    )
                else:
                    changed = c.execute(
                        "UPDATE contests SET current_stage_idx=? "
                        "WHERE id=? AND current_stage_idx=? AND status=?",
                        (stage_idx, contest_id, current_idx, expected_status),
                    )
                if changed.rowcount != 1:
                    raise ValueError("赛事阶段切换 CAS 已失效")
            if contest["status"] != CONTEST_PUBLISHED and manifest_enabled:
                moved_manifest = c.execute(
                    "UPDATE contests SET published_stage_pairing_count=? "
                    "WHERE id=? AND current_stage_idx=? "
                    "AND status IN (?,?,?)",
                    (
                        len(inserted),
                        contest_id,
                        stage_idx,
                        CONTEST_RUNNING,
                        CONTEST_REST,
                        CONTEST_PUBLISHED,
                    ),
                )
                if moved_manifest.rowcount != 1:
                    raise ValueError("赛事阶段批次计数迁移 CAS 已失效")
            if (
                (
                    contest["status"] == CONTEST_PUBLISHED
                    and stage_idx == current_idx
                )
                or install_initial_manifest
                or (
                    contest["status"] != CONTEST_PUBLISHED
                    and manifest_enabled
                )
            ):
                sealed_topology = c.execute(
                    "UPDATE contests SET sealed_pairing_topology_revision="
                    "pairing_topology_revision WHERE id=? "
                    "AND current_stage_idx=? "
                    "AND published_stage_pairing_count=?",
                    (contest_id, stage_idx, len(inserted)),
                )
                if sealed_topology.rowcount != 1:
                    raise ValueError("赛事阶段对阵拓扑冻结 CAS 已失效")
            return inserted

    def append_contest_round_pairings(
        self,
        contest_id: int,
        stage_idx: int,
        pairing_rows: list[dict[str, Any]],
        *,
        expected_current_stage_idx: int,
        expected_previous_max_round: int,
    ) -> list[dict]:
        """Atomically append one complete lazy-generated Swiss/KO round.

        The caller computes every row, including seat order and version snapshots,
        before entering this method.  ``BEGIN IMMEDIATE`` then revalidates the
        durable contest/stage cursor and previous maximum round.  A concurrent or
        retried writer cannot append the same target round twice, and any INSERT
        failure rolls the whole round back.
        """
        if not pairing_rows:
            raise ValueError("赛事轮次对阵批次不能为空")
        _validate_pairing_publication_times(
            pairing_rows, require_published_at=True
        )
        stage_idx = exact_nonnegative_int(stage_idx)
        expected_current_stage_idx = exact_nonnegative_int(
            expected_current_stage_idx
        )
        if stage_idx is None or expected_current_stage_idx is None:
            raise ValueError("赛事阶段坐标必须是非负整数")
        normalized_series = _pairing_series_batch(pairing_rows)
        previous_round = int(expected_previous_max_round)
        target_round = previous_round + 1
        if any(
            int(source.get("round_num") or 0) != target_round
            for source in pairing_rows
        ):
            raise ValueError("赛事轮次批次必须全部属于紧邻的目标轮")

        columns = (
            "contest_id",
            "round_num",
            "entry_a_id",
            "entry_b_id",
            "bot_a_id",
            "bot_b_id",
            "bot_a_version_id",
            "bot_b_version_id",
            "pairing_seed",
            "published_at",
            "scheduled_at",
            "match_id",
            "status",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
            "series_index",
            "series_size",
            "tiebreak_group",
            "tiebreak_game",
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_execution_admission_tx(c, maintenance_only=True)
            contest = c.execute(
                "SELECT status,current_stage_idx,published_stage_pairing_count "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] != CONTEST_RUNNING:
                raise ValueError("仅运行中的赛事可追加后续轮次")
            if (
                exact_nonnegative_int(contest["current_stage_idx"])
                != expected_current_stage_idx
            ):
                raise ValueError("赛事当前阶段已变化，拒绝追加轮次")
            if stage_idx != expected_current_stage_idx:
                raise ValueError("只能向赛事当前阶段追加轮次")
            raw_manifest = contest["published_stage_pairing_count"]
            manifest_enabled = raw_manifest is not None
            if not manifest_enabled or not self._contest_stage_manifest_is_valid_tx(
                c,
                contest_id,
                stage_idx,
                include_terminal_orphans=True,
                require_manifest=True,
            ):
                raise ValueError("赛事当前阶段对阵批次完整性校验失败，拒绝追加轮次")

            round_state = c.execute(
                "SELECT MAX(round_num) AS max_round FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=?",
                (contest_id, stage_idx),
            ).fetchone()
            actual_max = (
                int(round_state["max_round"])
                if round_state and round_state["max_round"] is not None
                else 0
            )
            if actual_max != previous_round:
                raise ValueError("赛事上一轮已变化，拒绝重复或跨轮追加")
            target_exists = c.execute(
                "SELECT 1 FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? AND round_num=? LIMIT 1",
                (contest_id, stage_idx, target_round),
            ).fetchone()
            if target_exists:
                raise ValueError("赛事目标轮已存在，拒绝重复生成")

            inserted: list[dict] = []
            placeholders = ",".join("?" for _ in columns)
            for source, (series_index, series_size) in zip(
                pairing_rows, normalized_series
            ):
                row = {
                    "contest_id": contest_id,
                    "round_num": target_round,
                    "entry_a_id": source.get("entry_a_id"),
                    "entry_b_id": source.get("entry_b_id"),
                    "bot_a_id": source.get("bot_a_id"),
                    "bot_b_id": source.get("bot_b_id"),
                    "bot_a_version_id": source.get("bot_a_version_id"),
                    "bot_b_version_id": source.get("bot_b_version_id"),
                    "pairing_seed": source.get("pairing_seed"),
                    "published_at": source.get("published_at"),
                    "scheduled_at": source.get("scheduled_at"),
                    "match_id": None,
                    "status": source.get("status") or STATUS_PENDING,
                    "stage_idx": stage_idx,
                    "stage_key": source.get("stage_key") or "",
                    "group_id": source.get("group_id") or "",
                    "bracket_slot": source.get("bracket_slot"),
                    # A/B have already been materialized as actual seat 0/1.
                    "color_first": 0,
                    "series_index": series_index,
                    "series_size": series_size,
                    "tiebreak_group": source.get("tiebreak_group", 0),
                    "tiebreak_game": source.get("tiebreak_game", 0),
                }
                cur = c.execute(
                    f"INSERT INTO contest_pairings({','.join(columns)}) "
                    f"VALUES({placeholders})",
                    tuple(row[column] for column in columns),
                )
                inserted.append(
                    _row(
                        c.execute(
                            "SELECT * FROM contest_pairings WHERE id=?",
                            (cur.lastrowid,),
                        ).fetchone()
                    )
                )
            if manifest_enabled:
                frozen_count = exact_nonnegative_int(raw_manifest)
                if frozen_count is None:
                    raise ValueError("赛事动态轮次批次计数损坏")
                changed = c.execute(
                    "UPDATE contests SET published_stage_pairing_count=? "
                    "WHERE id=? AND status=? AND current_stage_idx=? "
                    "AND published_stage_pairing_count=?",
                    (
                        frozen_count + len(inserted),
                        contest_id,
                        CONTEST_RUNNING,
                        stage_idx,
                        frozen_count,
                    ),
                )
                if changed.rowcount != 1:
                    raise ValueError("赛事动态轮次批次计数 CAS 已失效")
                sealed = c.execute(
                    "UPDATE contests SET sealed_pairing_topology_revision="
                    "pairing_topology_revision WHERE id=? AND status=? "
                    "AND current_stage_idx=? AND published_stage_pairing_count=?",
                    (
                        contest_id,
                        CONTEST_RUNNING,
                        stage_idx,
                        frozen_count + len(inserted),
                    ),
                )
                if sealed.rowcount != 1:
                    raise ValueError("赛事动态轮次对阵拓扑冻结 CAS 已失效")
            return inserted

    def append_contest_elimination_tiebreak_pairings(
        self,
        contest_id: int,
        stage_idx: int,
        round_num: int,
        bracket_slot: int,
        pairing_rows: list[dict[str, Any]],
        *,
        expected_current_stage_idx: int,
        expected_previous_tiebreak_group: int,
    ) -> list[dict]:
        """Atomically append one two-game swapped-seat elimination tiebreak.

        The exact ``(stage, round, bracket_slot, group, game)`` coordinate is a
        durable CAS boundary.  A retry or concurrent completion callback either
        wins once or observes the already-appended group; it can never create a
        partial or duplicate deciding group.
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        expected_current_stage_idx = exact_nonnegative_int(
            expected_current_stage_idx
        )
        round_num = exact_nonnegative_int(round_num)
        bracket_slot = exact_nonnegative_int(bracket_slot)
        previous_group = exact_nonnegative_int(expected_previous_tiebreak_group)
        if (
            stage_idx is None
            or expected_current_stage_idx is None
            or round_num is None
            or round_num < 1
            or bracket_slot is None
            or previous_group is None
        ):
            raise ValueError("淘汰决胜追加坐标必须是合法非负整数")
        target_group = previous_group + 1
        if len(pairing_rows) != 2:
            raise ValueError("淘汰决胜组必须恰好包含两场换边对局")
        _validate_pairing_publication_times(
            pairing_rows, require_published_at=True
        )
        normalized_series = _pairing_series_batch(
            pairing_rows, allow_elimination_tiebreak=True
        )
        if any(
            source.get("round_num") != round_num
            or source.get("bracket_slot") != bracket_slot
            or source.get("tiebreak_group") != target_group
            for source in pairing_rows
        ) or {source.get("tiebreak_game") for source in pairing_rows} != {1, 2}:
            raise ValueError("淘汰决胜批次坐标与 CAS 目标不一致")
        if any(source.get("pairing_seed") is not None for source in pairing_rows):
            raise ValueError("淘汰决胜组随机种子只能由存储事务私密分配")

        columns = (
            "contest_id",
            "round_num",
            "entry_a_id",
            "entry_b_id",
            "bot_a_id",
            "bot_b_id",
            "bot_a_version_id",
            "bot_b_version_id",
            "pairing_seed",
            "published_at",
            "scheduled_at",
            "match_id",
            "status",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
            "series_index",
            "series_size",
            "tiebreak_group",
            "tiebreak_game",
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_execution_admission_tx(c, maintenance_only=True)
            contest = c.execute(
                "SELECT status,current_stage_idx,stages_json,game_id,"
                "published_stage_pairing_count "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest or contest["status"] != CONTEST_RUNNING:
                raise ValueError("仅运行中的赛事可追加淘汰决胜组")
            if (
                exact_nonnegative_int(contest["current_stage_idx"])
                != expected_current_stage_idx
                or stage_idx != expected_current_stage_idx
            ):
                raise ValueError("赛事当前阶段已变化，拒绝追加淘汰决胜组")
            raw_manifest = contest["published_stage_pairing_count"]
            manifest_enabled = raw_manifest is not None
            if not manifest_enabled or not self._contest_stage_manifest_is_valid_tx(
                c,
                contest_id,
                stage_idx,
                include_terminal_orphans=True,
                require_manifest=True,
            ):
                raise ValueError(
                    "赛事当前阶段对阵批次完整性校验失败，拒绝追加淘汰决胜组"
                )
            stages = _loads_json(contest["stages_json"], default=[])
            frozen_stage = (
                stages[stage_idx]
                if isinstance(stages, list)
                and stage_idx < len(stages)
                and isinstance(stages[stage_idx], dict)
                else None
            )
            if (
                not frozen_stage
                or frozen_stage.get("type") != "single_elimination"
                or frozen_stage.get("tiebreak")
                != ELIMINATION_TIEBREAK_PAIRED_SWAP
            ):
                raise ValueError(
                    "赛事未冻结 paired_swap_until_decided 决胜策略"
                )

            coordinate_rows = [
                _row(row)
                for row in c.execute(
                    "SELECT cp.id,cp.contest_id,cp.stage_idx,cp.round_num,"
                    "cp.bracket_slot,cp.status,cp.match_id,cp.tiebreak_group,"
                    "cp.tiebreak_game,cp.bot_a_id,cp.bot_b_id,cp.entry_a_id,"
                    "cp.entry_b_id,cp.bot_a_version_id,cp.bot_b_version_id,"
                    "cp.pairing_seed,ea.user_id AS _entry_a_user_id,"
                    "eb.user_id AS _entry_b_user_id,"
                    "ba.owner_id AS _pairing_bot_a_owner_id,"
                    "bb.owner_id AS _pairing_bot_b_owner_id "
                    "FROM contest_pairings cp "
                    "LEFT JOIN contest_entries ea ON ea.id=cp.entry_a_id "
                    "LEFT JOIN contest_entries eb ON eb.id=cp.entry_b_id "
                    "LEFT JOIN bots ba ON ba.id=cp.bot_a_id "
                    "LEFT JOIN bots bb ON bb.id=cp.bot_b_id "
                    "WHERE cp.contest_id=? AND cp.stage_idx=? "
                    "AND cp.round_num=? AND cp.bracket_slot=? "
                    "ORDER BY cp.tiebreak_group,cp.tiebreak_game",
                    (contest_id, stage_idx, round_num, bracket_slot),
                ).fetchall()
            ]
            normalized_coordinates: list[tuple[dict[str, Any], int, int]] = []
            for row in coordinate_rows:
                frozen_group = exact_nonnegative_int(row["tiebreak_group"])
                frozen_game = exact_nonnegative_int(row["tiebreak_game"])
                if frozen_group is None or frozen_game is None:
                    raise ValueError("淘汰决胜坐标损坏")
                normalized_coordinates.append((row, frozen_group, frozen_game))
            primary = [
                row
                for row, frozen_group, frozen_game in normalized_coordinates
                if frozen_group == 0 and frozen_game == 0
            ]
            if (
                len(primary) != 1
                or primary[0]["status"] != STATUS_COMPLETED
                or primary[0]["match_id"] is None
            ):
                raise ValueError("淘汰主赛尚未权威完成，不能追加决胜组")
            primary_row = primary[0]
            by_game = {
                int(source["tiebreak_game"]): source
                for source in pairing_rows
            }
            first = by_game[1]
            second = by_game[2]
            if any(
                (
                    first.get(field) != primary_row[field]
                    or second.get(field) != primary_row[swapped_field]
                )
                for field, swapped_field in (
                    ("bot_a_id", "bot_b_id"),
                    ("bot_b_id", "bot_a_id"),
                    ("entry_a_id", "entry_b_id"),
                    ("entry_b_id", "entry_a_id"),
                    ("bot_a_version_id", "bot_b_version_id"),
                    ("bot_b_version_id", "bot_a_version_id"),
                )
            ):
                raise ValueError(
                    "淘汰决胜组必须沿用主赛冻结身份、版本并精确换边"
                )
            actual_previous = max(
                (
                    frozen_group
                    for _row, frozen_group, _game in normalized_coordinates
                ),
                default=0,
            )
            if actual_previous == target_group:
                target_rows = [
                    row
                    for row, frozen_group, _game in normalized_coordinates
                    if frozen_group == target_group
                ]
                target_by_game = {
                    frozen_game: row
                    for row, frozen_group, frozen_game in normalized_coordinates
                    if frozen_group == target_group
                }
                target_seed = (
                    target_by_game[1]["pairing_seed"]
                    if 1 in target_by_game
                    else None
                )
                seed_reused_elsewhere = bool(
                    target_seed is not None
                    and c.execute(
                        "SELECT 1 FROM contest_pairings WHERE contest_id=? "
                        "AND stage_idx=? AND pairing_seed=? "
                        "AND id NOT IN (SELECT id FROM contest_pairings "
                        "WHERE contest_id=? AND stage_idx=? AND round_num=? "
                        "AND bracket_slot=? AND tiebreak_group=?) LIMIT 1",
                        (
                            contest_id,
                            stage_idx,
                            target_seed,
                            contest_id,
                            stage_idx,
                            round_num,
                            bracket_slot,
                            target_group,
                        ),
                    ).fetchone()
                )
                if (
                    len(target_rows) == 2
                    and set(target_by_game) == {1, 2}
                    and exact_nonnegative_int(target_seed) is not None
                    and target_seed > 0
                    and target_seed
                    == target_by_game[2]["pairing_seed"]
                    and not seed_reused_elsewhere
                    and all(
                        target_by_game[game][field] == by_game[game].get(field)
                        for game in (1, 2)
                        for field in (
                            "bot_a_id",
                            "bot_b_id",
                            "entry_a_id",
                            "entry_b_id",
                            "bot_a_version_id",
                            "bot_b_version_id",
                        )
                    )
                ):
                    return []
                raise ValueError("已存在的淘汰决胜组与重试冻结契约不一致")
            if actual_previous != previous_group:
                raise ValueError("淘汰决胜组已变化，拒绝重复追加")
            if previous_group:
                previous_rows = [
                    row
                    for row, frozen_group, _game in normalized_coordinates
                    if frozen_group == previous_group
                ]
                previous_by_game = {
                    frozen_game: row
                    for row, frozen_group, frozen_game in normalized_coordinates
                    if frozen_group == previous_group
                }
                previous_seed = (
                    previous_by_game[1]["pairing_seed"]
                    if 1 in previous_by_game
                    else None
                )
                previous_seed_reused_elsewhere = bool(
                    previous_seed is not None
                    and c.execute(
                        "SELECT 1 FROM contest_pairings WHERE contest_id=? "
                        "AND stage_idx=? AND pairing_seed=? "
                        "AND id NOT IN (SELECT id FROM contest_pairings "
                        "WHERE contest_id=? AND stage_idx=? AND round_num=? "
                        "AND bracket_slot=? AND tiebreak_group=?) LIMIT 1",
                        (
                            contest_id,
                            stage_idx,
                            previous_seed,
                            contest_id,
                            stage_idx,
                            round_num,
                            bracket_slot,
                            previous_group,
                        ),
                    ).fetchone()
                )
                if (
                    len(previous_rows) != 2
                    or set(previous_by_game) != {1, 2}
                    or any(
                        row["status"] != STATUS_COMPLETED
                        or row["match_id"] is None
                        for row in previous_rows
                    )
                    or exact_nonnegative_int(previous_seed) is None
                    or previous_seed <= 0
                    or previous_seed != previous_by_game[2]["pairing_seed"]
                    or previous_seed_reused_elsewhere
                    or any(
                        previous_by_game[game][field]
                        != by_game[game].get(field)
                        for game in (1, 2)
                        for field in (
                            "bot_a_id",
                            "bot_b_id",
                            "entry_a_id",
                            "entry_b_id",
                            "bot_a_version_id",
                            "bot_b_version_id",
                        )
                    )
                ):
                    raise ValueError("上一淘汰决胜组冻结契约损坏或尚未权威完成")

            from bzplat.backend.contests.series import (
                summarize_elimination_encounter,
            )
            from bzplat.backend.games import registry as game_registry

            roster_rows = c.execute(
                "SELECT id,bot_id,user_id FROM contest_entries WHERE contest_id=?",
                (contest_id,),
            ).fetchall()
            expected_entry_bots = {
                int(row["id"]): row["bot_id"] for row in roster_rows
            }
            expected_entry_users = {
                int(row["id"]): int(row["user_id"]) for row in roster_rows
            }
            try:
                game_spec = game_registry.get(str(contest["game_id"]))
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("赛事游戏契约损坏") from exc

            def match_lookup(match_id: str) -> dict[str, Any] | None:
                table = self._match_table_of(c, match_id)
                if table is None:
                    return None
                return _parse_match_json_cols(
                    _row(
                        c.execute(
                            f"SELECT * FROM {table} WHERE id=?", (match_id,)
                        ).fetchone()
                    )
                )

            summary = summarize_elimination_encounter(
                frozen_stage,
                coordinate_rows,
                match_lookup,
                game_spec=game_spec,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=False,
            )
            if (
                summary.get("state") != "append_tiebreak"
                or summary.get("next_tiebreak_group") != target_group
            ):
                raise ValueError("淘汰遭遇赛果已变化，拒绝追加决胜组")

            pairing_seed: int | None = None
            for _ in range(16):
                candidate = secrets.randbelow(9_223_372_036_854_775_807) + 1
                if not c.execute(
                    "SELECT 1 FROM contest_pairings WHERE contest_id=? "
                    "AND stage_idx=? AND pairing_seed=? LIMIT 1",
                    (contest_id, stage_idx, candidate),
                ).fetchone():
                    pairing_seed = candidate
                    break
            if pairing_seed is None:
                raise RuntimeError("无法分配唯一的淘汰决胜随机种子")

            placeholders = ",".join("?" for _ in columns)
            inserted: list[dict] = []
            for source, (series_index, series_size) in zip(
                pairing_rows, normalized_series
            ):
                row = {
                    "contest_id": contest_id,
                    "round_num": round_num,
                    "entry_a_id": source.get("entry_a_id"),
                    "entry_b_id": source.get("entry_b_id"),
                    "bot_a_id": source.get("bot_a_id"),
                    "bot_b_id": source.get("bot_b_id"),
                    "bot_a_version_id": source.get("bot_a_version_id"),
                    "bot_b_version_id": source.get("bot_b_version_id"),
                    "pairing_seed": pairing_seed,
                    "published_at": source.get("published_at"),
                    "scheduled_at": source.get("scheduled_at"),
                    "match_id": None,
                    "status": STATUS_PENDING,
                    "stage_idx": stage_idx,
                    "stage_key": source.get("stage_key") or "",
                    "group_id": source.get("group_id") or "",
                    "bracket_slot": bracket_slot,
                    "color_first": 0,
                    "series_index": series_index,
                    "series_size": series_size,
                    "tiebreak_group": target_group,
                    "tiebreak_game": source.get("tiebreak_game"),
                }
                cur = c.execute(
                    f"INSERT INTO contest_pairings({','.join(columns)}) "
                    f"VALUES({placeholders})",
                    tuple(row[column] for column in columns),
                )
                inserted.append(
                    _row(
                        c.execute(
                            "SELECT * FROM contest_pairings WHERE id=?",
                            (cur.lastrowid,),
                        ).fetchone()
                    )
                )
            if manifest_enabled:
                frozen_count = exact_nonnegative_int(raw_manifest)
                if frozen_count is None:
                    raise ValueError("赛事淘汰决胜批次计数损坏")
                changed = c.execute(
                    "UPDATE contests SET published_stage_pairing_count=? "
                    "WHERE id=? AND status=? AND current_stage_idx=? "
                    "AND published_stage_pairing_count=?",
                    (
                        frozen_count + len(inserted),
                        contest_id,
                        CONTEST_RUNNING,
                        stage_idx,
                        frozen_count,
                    ),
                )
                if changed.rowcount != 1:
                    raise ValueError("赛事淘汰决胜批次计数 CAS 已失效")
                sealed = c.execute(
                    "UPDATE contests SET sealed_pairing_topology_revision="
                    "pairing_topology_revision WHERE id=? AND status=? "
                    "AND current_stage_idx=? AND published_stage_pairing_count=?",
                    (
                        contest_id,
                        CONTEST_RUNNING,
                        stage_idx,
                        frozen_count + len(inserted),
                    ),
                )
                if sealed.rowcount != 1:
                    raise ValueError("赛事淘汰决胜对阵拓扑冻结 CAS 已失效")
            return inserted

    def list_pairings(
        self, contest_id: int, *, stage_idx: int | None = None
    ) -> list[dict]:
        with self._tx() as c:
            sql = (
                "SELECT p.*, legacy_a.entry_id AS _effective_entry_a_id, "
                "legacy_b.entry_id AS _effective_entry_b_id, "
                "p.entry_a_id AS _raw_entry_a_id, "
                "p.entry_b_id AS _raw_entry_b_id, "
                + _contest_pairing_explicit_series_marker_sql()
                + " AS _explicit_series_marker, "
                "ea.user_id AS _entry_a_user_id, "
                "eb.user_id AS _entry_b_user_id, "
                "ba.owner_id AS _pairing_bot_a_owner_id, "
                "bb.owner_id AS _pairing_bot_b_owner_id "
                "FROM contest_pairings p "
                "LEFT JOIN contests pairing_contest "
                "ON pairing_contest.id=p.contest_id "
                f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy_a "
                "ON p.entry_a_id IS NULL AND p.bot_a_id=legacy_a.bot_id "
                "AND p.contest_id=legacy_a.contest_id "
                f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy_b "
                "ON p.entry_b_id IS NULL AND p.bot_b_id=legacy_b.bot_id "
                "AND p.contest_id=legacy_b.contest_id "
                "LEFT JOIN contest_entries ea "
                "ON ea.id=COALESCE(p.entry_a_id,legacy_a.entry_id) "
                "AND ea.contest_id=p.contest_id "
                "LEFT JOIN contest_entries eb "
                "ON eb.id=COALESCE(p.entry_b_id,legacy_b.entry_id) "
                "AND eb.contest_id=p.contest_id "
                "LEFT JOIN bots ba ON ba.id=p.bot_a_id "
                "LEFT JOIN bots bb ON bb.id=p.bot_b_id "
                "WHERE p.contest_id=?"
            )
            params: list[Any] = [contest_id]
            if stage_idx is not None:
                sql += " AND p.stage_idx=?"
                params.append(stage_idx)
            sql += " ORDER BY p.stage_idx, p.round_num, p.id"
            return [
                _apply_effective_entry_ids(
                    _row(r),
                    ("entry_a_id", "_effective_entry_a_id"),
                    ("entry_b_id", "_effective_entry_b_id"),
                )
                for r in c.execute(sql, params)
            ]

    list_contest_pairings = list_pairings

    def published_stage_has_valid_active_batch(
        self, contest_id: int, stage_idx: int
    ) -> bool:
        """Validate the sealed published batch when durable jobs already exist.

        Return ``False`` only when there is no non-terminal request and strict
        recovery may therefore validate/rebuild the batch.  With active work,
        the publication manifest and current indexed cardinality must agree;
        an orphan/cross-stage request also fails closed.  No pairing rows are
        materialised into Python on this scheduler fast path.
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None:
            raise ValueError("赛事阶段坐标必须是非负整数")
        with self._tx() as c:
            c.execute("BEGIN")
            contest = c.execute(
                "SELECT status,current_stage_idx,published_stage_pairing_count,"
                "pairing_topology_revision,sealed_pairing_topology_revision "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if (
                contest is None
                or contest["status"] != CONTEST_PUBLISHED
                or exact_nonnegative_int(contest["current_stage_idx"])
                != stage_idx
            ):
                return False
            active_params = (
                EXECUTION_SOURCE_CONTEST,
                contest_id,
                EXECUTION_QUEUED,
                EXECUTION_STARTING,
                EXECUTION_RUNNING,
                EXECUTION_SETTLING,
            )
            active = c.execute(
                "SELECT 1 FROM execution_jobs WHERE source=? AND contest_id=? "
                "AND status IN (?,?,?,?) LIMIT 1",
                active_params,
            ).fetchone()
            if active is None:
                return False
            frozen_count = exact_nonnegative_int(
                contest["published_stage_pairing_count"]
            )
            if frozen_count is None:
                raise ValueError("published 赛事存在 active 请求但缺少批次计数冻结")
            current_revision = exact_nonnegative_int(
                contest["pairing_topology_revision"]
            )
            sealed_revision = exact_nonnegative_int(
                contest["sealed_pairing_topology_revision"]
            )
            if (
                current_revision is None
                or sealed_revision is None
                or current_revision != sealed_revision
            ):
                raise ValueError("published 赛事 active 对阵批次完整性校验失败")
            return True

    def seal_published_stage_pairing_count(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        expected_count: int,
        expected_existing_ids: list[int],
    ) -> None:
        """Seal one strictly validated legacy published batch behind a CAS."""
        stage_idx = exact_nonnegative_int(stage_idx)
        expected_count = exact_nonnegative_int(expected_count)
        ids = sorted({int(pairing_id) for pairing_id in expected_existing_ids})
        if (
            stage_idx is None
            or expected_count is None
            or len(ids) != expected_count
        ):
            raise ValueError("published 对阵批次计数冻结参数无效")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status,current_stage_idx,published_stage_pairing_count "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if (
                contest is None
                or contest["status"] != CONTEST_PUBLISHED
                or exact_nonnegative_int(contest["current_stage_idx"])
                != stage_idx
            ):
                raise ValueError("published 赛事状态已变化，拒绝冻结批次计数")
            current_manifest = contest["published_stage_pairing_count"]
            if current_manifest is not None and (
                exact_nonnegative_int(current_manifest) != expected_count
            ):
                raise ValueError("published 对阵批次计数冻结已损坏")
            if c.execute(
                "SELECT 1 FROM execution_jobs WHERE source=? AND contest_id=? "
                "AND status IN (?,?,?,?) LIMIT 1",
                (
                    EXECUTION_SOURCE_CONTEST,
                    contest_id,
                    EXECUTION_QUEUED,
                    EXECUTION_STARTING,
                    EXECUTION_RUNNING,
                    EXECUTION_SETTLING,
                ),
            ).fetchone():
                raise ValueError("published 赛事已有 active 请求，拒绝补写批次计数")
            current_ids = [
                int(row["id"])
                for row in c.execute(
                    "SELECT id FROM contest_pairings WHERE contest_id=? "
                    "AND stage_idx=? ORDER BY id",
                    (contest_id, stage_idx),
                )
            ]
            if current_ids != ids:
                raise ValueError("published 对阵在批次计数冻结期间已变化")
            changed = c.execute(
                "UPDATE contests SET published_stage_pairing_count=? "
                "WHERE id=? AND status=? AND current_stage_idx=?",
                (expected_count, contest_id, CONTEST_PUBLISHED, stage_idx),
            )
            if changed.rowcount != 1:
                raise ValueError("published 对阵批次计数冻结 CAS 已失效")
            sealed = c.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=? AND status=? "
                "AND current_stage_idx=? AND published_stage_pairing_count=?",
                (contest_id, CONTEST_PUBLISHED, stage_idx, expected_count),
            )
            if sealed.rowcount != 1:
                raise ValueError("published 对阵拓扑冻结 CAS 已失效")

    def seal_canonical_published_stage_pairing_batch(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        expected_revision: int,
        expected_pairing_rows: list[dict[str, Any]],
        expected_entries: list[dict[str, Any]],
    ) -> None:
        """Install only a manifest+seal for one exact canonical legacy batch.

        This is the production recovery boundary used by the contest manager.
        Unlike the low-level count sealer retained for migration/corruption
        fixtures, it never creates, deletes or updates pairing rows.  The
        manager proves the generated topology; this transaction then proves the
        exact persisted rows, roster, ownership, game, runnable current Bot
        versions and publication coordinates still match that proof before
        installing the manifest and final lifecycle seal.
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        expected_revision = exact_nonnegative_int(expected_revision)
        if (
            stage_idx != 0
            or expected_revision is None
            or not isinstance(expected_pairing_rows, list)
            or not isinstance(expected_entries, list)
        ):
            raise ValueError("published 规范批次冻结参数无效")

        expected_by_id: dict[int, tuple[Any, ...]] = {}
        for source in expected_pairing_rows:
            if not isinstance(source, dict):
                raise ValueError("published 规范批次行类型无效")
            pairing_id = exact_nonnegative_int(source.get("id"))
            if pairing_id is None or pairing_id < 1 or pairing_id in expected_by_id:
                raise ValueError("published 规范批次 pairing 身份无效")
            try:
                expected_by_id[pairing_id] = tuple(
                    source[field] for field in _PUBLISHED_PAIRING_SEAL_FIELDS
                )
            except KeyError as exc:
                raise ValueError("published 规范批次字段不完整") from exc

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            revision = exact_nonnegative_int(
                contest["pairing_topology_revision"] if contest else None
            )
            if (
                contest is None
                or contest["status"] != CONTEST_PUBLISHED
                or exact_nonnegative_int(contest["current_stage_idx"]) != stage_idx
                or contest["published_stage_pairing_count"] is not None
                or contest["sealed_pairing_topology_revision"] is not None
                or revision != expected_revision
            ):
                raise ValueError("published 规范批次状态、游标或 revision 已变化")

            active_entry_ids, expected_entry_bots = (
                _validate_expected_contest_entries_tx(
                    c, contest_id, expected_entries
                )
            )
            if active_entry_ids != set(expected_entry_bots):
                raise ValueError("published 首阶段冻结名册含已淘汰成员")
            durable_entries = {
                int(row["id"]): row
                for row in c.execute(
                    "SELECT id,user_id,bot_id FROM contest_entries "
                    "WHERE contest_id=? ORDER BY id",
                    (contest_id,),
                ).fetchall()
            }
            bot_rows = {
                int(row["id"]): dict(row)
                for row in c.execute(
                    "SELECT DISTINCT b.* FROM contest_entries e "
                    "JOIN bots b ON b.id=e.bot_id WHERE e.contest_id=?",
                    (contest_id,),
                ).fetchall()
            }
            current_version_rows = {
                int(row["bot_id"]): dict(row)
                for row in c.execute(
                    "SELECT DISTINCT v.* FROM contest_entries e "
                    "JOIN bots b ON b.id=e.bot_id "
                    "JOIN bot_versions v ON v.bot_id=b.id "
                    "AND v.version=b.current_version "
                    "WHERE e.contest_id=?",
                    (contest_id,),
                ).fetchall()
            }
            current_versions: dict[int, int | None] = {}
            integrity_cache: set[Any] = set()
            for entry_id, entry in durable_entries.items():
                user_id = exact_nonnegative_int(entry["user_id"])
                bot_id = exact_nonnegative_int(entry["bot_id"])
                if (
                    user_id is None
                    or user_id < 1
                    or bot_id is None
                    or bot_id < 1
                    or expected_entry_bots.get(entry_id) != bot_id
                ):
                    raise ValueError("published 规范批次名册身份损坏")
                _require_active_contest_user_tx(c, user_id)
                _require_contest_bot_binding_tx(
                    c,
                    contest_game_id=str(contest["game_id"]),
                    user_id=user_id,
                    bot_id=bot_id,
                )
                current_version_id = _require_current_runnable_contest_bot_tx(
                    c, bot_id
                )
                runtime = current_version_rows.get(bot_id) or bot_rows.get(bot_id)
                try:
                    if runtime is None:
                        raise ValueError("version_unavailable")
                    require_supported_binary_metadata(
                        str(runtime.get("format") or ""),
                        str(runtime.get("os") or ""),
                        str(runtime.get("arch") or ""),
                    )
                    path = str(runtime.get("binary_path") or "").strip()
                    if not path:
                        raise ValueError("version_unavailable")
                    require_binary_file_integrity(
                        runtime, path, cache=integrity_cache
                    )
                except (OSError, TypeError, ValueError) as exc:
                    raise ValueError(
                        "published 规范批次 Bot 冻结版本文件不可用"
                    ) from exc
                current_versions[bot_id] = current_version_id

            rows = c.execute(
                "SELECT * FROM contest_pairings WHERE contest_id=? "
                "AND stage_idx=? ORDER BY id",
                (contest_id, stage_idx),
            ).fetchall()
            durable_by_id = {
                int(row["id"]): tuple(
                    row[field] for field in _PUBLISHED_PAIRING_SEAL_FIELDS
                )
                for row in rows
            }
            if durable_by_id != expected_by_id:
                raise ValueError("published 对阵在规范冻结期间已变化")

            stages = _loads_json(contest["stages_json"], default=[])
            if (
                not isinstance(stages, list)
                or not stages
                or not isinstance(stages[0], dict)
            ):
                raise ValueError("published 冻结阶段配置损坏")
            stage = stages[0]
            stage_type = stage.get("type")
            series_stage = "games_per_pair" in stage
            normalized_rows = [dict(row) for row in rows]
            _pairing_series_batch(normalized_rows)
            if series_stage:
                playable_seeds = [
                    _pairing_seed_field(row, required=True)
                    for row in normalized_rows
                    if row.get("entry_b_id") is not None
                ]
                if len(playable_seeds) != len(set(playable_seeds)):
                    raise ValueError("published 多场批次 pairing_seed 必须唯一")
            elif any(row.get("pairing_seed") is not None for row in normalized_rows):
                raise ValueError("published 普通批次不得带私有 pairing_seed")

            seen_participants: set[int] = set()
            for row in normalized_rows:
                entry_a_id = exact_nonnegative_int(row.get("entry_a_id"))
                raw_entry_b_id = row.get("entry_b_id")
                entry_b_id = (
                    exact_nonnegative_int(raw_entry_b_id)
                    if raw_entry_b_id is not None
                    else None
                )
                published_at = validate_canonical_naive_timestamp(
                    row.get("published_at"), "赛事对阵发布时间"
                )
                validate_canonical_naive_timestamp(
                    row.get("scheduled_at"),
                    "赛事对阵计划时间",
                    allow_none=True,
                )
                if (
                    row.get("contest_id") != contest_id
                    or exact_nonnegative_int(row.get("stage_idx")) != stage_idx
                    or entry_a_id is None
                    or entry_a_id not in active_entry_ids
                    or (
                        raw_entry_b_id is not None
                        and (
                            entry_b_id is None
                            or entry_b_id not in active_entry_ids
                            or entry_b_id == entry_a_id
                        )
                    )
                    or row.get("match_id") is not None
                ):
                    raise ValueError("published 规范批次身份或发布坐标损坏")
                entry_a = durable_entries[entry_a_id]
                bot_a_id = exact_nonnegative_int(row.get("bot_a_id"))
                if (
                    bot_a_id is None
                    or bot_a_id != entry_a["bot_id"]
                ):
                    raise ValueError("published 规范批次 A 座身份损坏")
                seen_participants.add(entry_a_id)
                if entry_b_id is None:
                    if (
                        row.get("bot_b_id") is not None
                        or row.get("bot_a_version_id") is not None
                        or row.get("bot_b_version_id") is not None
                        or row.get("status") != STATUS_COMPLETED
                        or not is_authoritative_no_opponent_pairing(
                            stage_type, row
                        )
                    ):
                        raise ValueError("published 规范轮空行损坏")
                    continue
                entry_b = durable_entries[entry_b_id]
                bot_b_id = exact_nonnegative_int(row.get("bot_b_id"))
                if (
                    bot_b_id is None
                    or bot_b_id != entry_b["bot_id"]
                    or row.get("status") != STATUS_PENDING
                    or row.get("bot_a_version_id")
                    != current_versions.get(bot_a_id)
                    or row.get("bot_b_version_id")
                    != current_versions.get(bot_b_id)
                ):
                    raise ValueError("published 规范批次 Bot 版本或状态损坏")
                seen_participants.update((entry_a_id, entry_b_id))
            if seen_participants != active_entry_ids:
                raise ValueError("published 规范批次未覆盖完整冻结名册")

            if c.execute(
                "SELECT 1 FROM execution_jobs WHERE source=? AND contest_id=? "
                "AND status IN (?,?,?,?) LIMIT 1",
                (
                    EXECUTION_SOURCE_CONTEST,
                    contest_id,
                    EXECUTION_QUEUED,
                    EXECUTION_STARTING,
                    EXECUTION_RUNNING,
                    EXECUTION_SETTLING,
                ),
            ).fetchone() or any(
                c.execute(
                    f"SELECT 1 FROM {_matches_table(game_id)} WHERE contest_id=? "
                    "AND status IN (?,?) LIMIT 1",
                    (contest_id, STATUS_PENDING, STATUS_RUNNING),
                ).fetchone()
                for game_id in sorted(_all_game_ids())
            ):
                raise ValueError("published 赛事已有 active 请求或对局")

            changed = c.execute(
                "UPDATE contests SET published_stage_pairing_count=? "
                "WHERE id=? AND status=? AND current_stage_idx=? "
                "AND published_stage_pairing_count IS NULL "
                "AND sealed_pairing_topology_revision IS NULL "
                "AND pairing_topology_revision=?",
                (
                    len(rows),
                    contest_id,
                    CONTEST_PUBLISHED,
                    stage_idx,
                    expected_revision,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("published 规范批次 manifest CAS 已失效")
            after = c.execute(
                "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            after_revision = exact_nonnegative_int(
                after["pairing_topology_revision"] if after else None
            )
            if (
                after_revision is None
                or after_revision <= expected_revision
                or (after and after["sealed_pairing_topology_revision"] is not None)
            ):
                raise ValueError("published 规范批次 lifecycle revision 漂移")
            sealed = c.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=? AND status=? "
                "AND current_stage_idx=? AND published_stage_pairing_count=? "
                "AND pairing_topology_revision=? "
                "AND sealed_pairing_topology_revision IS NULL",
                (
                    contest_id,
                    CONTEST_PUBLISHED,
                    stage_idx,
                    len(rows),
                    after_revision,
                ),
            )
            if sealed.rowcount != 1:
                raise ValueError("published 规范批次 topology seal CAS 已失效")

    @staticmethod
    def _cancel_queued_contest_batch_tx(
        c: sqlite3.Connection, contest_id: int
    ) -> int:
        """Fail closed every not-yet-claimed job for one damaged batch."""
        terminal = _now()
        changed = c.execute(
            "UPDATE execution_jobs SET status='cancelled',retryable=0,"
            "terminal_reason='contest_pairing_batch_changed',"
            "last_error='contest_pairing_batch_changed',next_attempt_at=NULL,"
            "terminal_at=? WHERE source='contest' AND contest_id=? "
            "AND status='queued'",
            (terminal, int(contest_id)),
        )
        return int(changed.rowcount)

    def list_dispatchable_contest_pairings(
        self,
        contest_id: int,
        *,
        stage_idx: int,
        due_at: str,
    ) -> list[dict]:
        """Return only due pairings that have no active execution request.

        A queue-backed contest leaves the pairing ``pending`` and unbound until
        claim creates the Match.  The active execution-job exclusion is
        therefore part of the scheduler read contract, not merely enqueue
        idempotency: repeated ticks must not re-read Bot versions or hash the
        same artifacts while that durable job is already queued.
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None:
            raise ValueError("赛事阶段坐标必须是非负整数")
        if not isinstance(due_at, str) or not due_at:
            raise ValueError("赛事调度时点无效")
        invalid_batch = False
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._contest_stage_manifest_revision_is_valid_tx(
                c, contest_id, stage_idx, require_manifest=True
            ):
                self._cancel_queued_contest_batch_tx(c, contest_id)
                invalid_batch = True
                raw_rows: list[sqlite3.Row] = []
            else:
                raw_rows = []
            sql = (
                "SELECT p.*, legacy_a.entry_id AS _effective_entry_a_id, "
                "legacy_b.entry_id AS _effective_entry_b_id, "
                "p.entry_a_id AS _raw_entry_a_id, "
                "p.entry_b_id AS _raw_entry_b_id, "
                + _contest_pairing_explicit_series_marker_sql()
                + " AS _explicit_series_marker, "
                "ea.user_id AS _entry_a_user_id, "
                "eb.user_id AS _entry_b_user_id, "
                "ba.owner_id AS _pairing_bot_a_owner_id, "
                "bb.owner_id AS _pairing_bot_b_owner_id "
                "FROM contest_pairings p "
                "LEFT JOIN contests pairing_contest "
                "ON pairing_contest.id=p.contest_id "
                f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy_a "
                "ON p.entry_a_id IS NULL AND p.bot_a_id=legacy_a.bot_id "
                "AND p.contest_id=legacy_a.contest_id "
                f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy_b "
                "ON p.entry_b_id IS NULL AND p.bot_b_id=legacy_b.bot_id "
                "AND p.contest_id=legacy_b.contest_id "
                "LEFT JOIN contest_entries ea "
                "ON ea.id=COALESCE(p.entry_a_id,legacy_a.entry_id) "
                "AND ea.contest_id=p.contest_id "
                "LEFT JOIN contest_entries eb "
                "ON eb.id=COALESCE(p.entry_b_id,legacy_b.entry_id) "
                "AND eb.contest_id=p.contest_id "
                "LEFT JOIN bots ba ON ba.id=p.bot_a_id "
                "LEFT JOIN bots bb ON bb.id=p.bot_b_id "
                "WHERE p.contest_id=? AND p.stage_idx=? "
                "AND p.status=? AND p.match_id IS NULL "
                "AND (p.scheduled_at IS NULL OR p.scheduled_at<=?) "
                "AND NOT EXISTS (SELECT 1 FROM execution_jobs j "
                "WHERE j.contest_pairing_id=p.id "
                "AND j.status IN ('queued','starting','running','settling')) "
                "ORDER BY p.stage_idx,p.round_num,p.id"
            )
            if not invalid_batch:
                raw_rows = c.execute(
                    sql,
                    (contest_id, stage_idx, STATUS_PENDING, due_at),
                ).fetchall()
        if invalid_batch:
            raise ValueError("赛事 active 对阵批次完整性校验失败")
        return [
                _apply_effective_entry_ids(
                    _row(row),
                    ("entry_a_id", "_effective_entry_a_id"),
                    ("entry_b_id", "_effective_entry_b_id"),
                )
                for row in raw_rows
            ]

    def contest_stage_has_dispatch_gap(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        due_at: str,
    ) -> bool:
        """Return whether a previously covered stage lost an active request.

        The hot negative case starts from the narrow cancelled/interrupted job
        ranges.  It never anti-joins from every still-pending pairing, so a
        fully queued 100-player double round robin remains an indexed O(1)
        scheduler probe.  A positive result deliberately falls back to the
        strict dispatchable-pairing reader for repair.
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None:
            return True
        if not isinstance(due_at, str) or not due_at:
            return True
        invalid_batch = False
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._contest_stage_manifest_revision_is_valid_tx(
                c,
                contest_id,
                stage_idx,
                require_manifest=True,
            ):
                self._cancel_queued_contest_batch_tx(c, contest_id)
                invalid_batch = True
                has_gap = False
            else:
                has_gap = bool(
                c.execute(
                    "SELECT 1 FROM execution_jobs j INDEXED BY "
                    "idx_execution_jobs_contest_dispatch_gap "
                    "JOIN contest_pairings p ON p.id=j.contest_pairing_id "
                    "WHERE j.source=? AND j.contest_id=? "
                    "AND j.status IN (?,?) AND p.contest_id=? "
                    "AND p.stage_idx=? AND p.status=? AND p.match_id IS NULL "
                    "AND (p.scheduled_at IS NULL OR p.scheduled_at<=?) "
                    "AND NOT EXISTS (SELECT 1 FROM execution_jobs active "
                    "WHERE active.contest_pairing_id=p.id AND active.status IN "
                    "(?,?,?,?)) LIMIT 1",
                    (
                        EXECUTION_SOURCE_CONTEST,
                        contest_id,
                        EXECUTION_CANCELLED,
                        EXECUTION_INTERRUPTED,
                        contest_id,
                        stage_idx,
                        STATUS_PENDING,
                        due_at,
                        EXECUTION_QUEUED,
                        EXECUTION_STARTING,
                        EXECUTION_RUNNING,
                        EXECUTION_SETTLING,
                    ),
                ).fetchone()
                )
        if invalid_batch:
            raise ValueError("赛事 active 对阵批次完整性校验失败")
        return has_gap

    def contest_stage_has_future_pending_pairings(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        due_at: str,
    ) -> bool:
        """Keep a coverage cache open while a staged start time is in future."""
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None or not isinstance(due_at, str) or not due_at:
            return True
        with self._tx() as c:
            return bool(
                c.execute(
                    "SELECT 1 FROM contest_pairings INDEXED BY "
                    "idx_contest_pairings_schedule WHERE contest_id=? "
                    "AND stage_idx=? AND status=? AND scheduled_at>? "
                    "AND match_id IS NULL LIMIT 1",
                    (contest_id, stage_idx, STATUS_PENDING, due_at),
                ).fetchone()
            )

    def contest_stage_has_incomplete_pairings(
        self, contest_id: int, stage_idx: int
    ) -> bool:
        """Index-backed negative gate before strict terminal-stage parsing."""
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None:
            return True
        with self._tx() as c:
            return bool(
                c.execute(
                    "SELECT EXISTS(SELECT 1 FROM contest_pairings "
                    "WHERE contest_id=? AND stage_idx=? AND status<>? LIMIT 1)",
                    (contest_id, stage_idx, STATUS_COMPLETED),
                ).fetchone()[0]
            )

    def list_contest_pairings_needing_completion_sync(
        self, contest_id: int, stage_idx: int
    ) -> list[dict[str, Any]]:
        """Return only bound, not-yet-mirrored rows for completion repair."""
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None:
            return []
        with self._tx() as c:
            exists = c.execute(
                "SELECT 1 FROM contest_pairings WHERE contest_id=? "
                "AND stage_idx=? AND status<>? AND match_id IS NOT NULL LIMIT 1",
                (contest_id, stage_idx, STATUS_COMPLETED),
            ).fetchone()
            if exists is None:
                return []
            return [
                _row(row)
                for row in c.execute(
                    "SELECT id,match_id FROM contest_pairings WHERE contest_id=? "
                    "AND stage_idx=? AND status<>? AND match_id IS NOT NULL "
                    "ORDER BY id",
                    (contest_id, stage_idx, STATUS_COMPLETED),
                )
            ]

    def _contest_stage_manifest_revision_is_valid_tx(
        self,
        c: sqlite3.Connection,
        contest_id: int,
        stage_idx: int,
        *,
        require_manifest: bool = False,
    ) -> bool:
        """Validate one current-stage seal without scanning its pairing batch."""
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None:
            return False
        contest = c.execute(
            "SELECT status,current_stage_idx,published_stage_pairing_count,"
            "pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest_id,),
        ).fetchone()
        if (
            contest is None
            or contest["status"]
            not in (CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST)
            or exact_nonnegative_int(contest["current_stage_idx"]) != stage_idx
        ):
            return False
        raw_manifest = contest["published_stage_pairing_count"]
        if raw_manifest is None:
            return False
        if exact_nonnegative_int(raw_manifest) is None:
            return False
        current_revision = exact_nonnegative_int(
            contest["pairing_topology_revision"]
        )
        sealed_revision = exact_nonnegative_int(
            contest["sealed_pairing_topology_revision"]
        )
        return bool(
            current_revision is not None
            and sealed_revision is not None
            and current_revision == sealed_revision
        )

    def _contest_stage_manifest_is_valid_tx(
        self,
        c: sqlite3.Connection,
        contest_id: int,
        stage_idx: int,
        *,
        include_terminal_orphans: bool = False,
        require_manifest: bool = False,
    ) -> bool:
        """Validate the sealed current-stage cardinality in one DB snapshot.

        Fresh publication freezes ``published_stage_pairing_count`` and every
        subsequent stage/round append moves that seal in the same transaction.
        Historical ``NULL`` manifests remain readable, but no active execution
        or lifecycle writer may use them as authority.  Missing manifests are
        therefore rejected regardless of the compatibility keyword.

        Claim paths only inspect active job orphans.  Terminal/advancement gates
        additionally reject a terminal job whose pairing disappeared, closing
        same-cardinality delete+replace corruption without treating intact jobs
        from earlier stages as current-stage orphans.
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None:
            return False
        contest = c.execute(
            "SELECT status,current_stage_idx,published_stage_pairing_count,"
            "pairing_topology_revision,sealed_pairing_topology_revision "
            "FROM contests WHERE id=?",
            (contest_id,),
        ).fetchone()
        if (
            contest is None
            or contest["status"]
            not in (CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST)
            or exact_nonnegative_int(contest["current_stage_idx"]) != stage_idx
        ):
            return False
        raw_manifest = contest["published_stage_pairing_count"]
        if raw_manifest is None:
            return False
        frozen_count = exact_nonnegative_int(raw_manifest)
        if frozen_count is None:
            return False
        current_revision = exact_nonnegative_int(
            contest["pairing_topology_revision"]
        )
        sealed_revision = exact_nonnegative_int(
            contest["sealed_pairing_topology_revision"]
        )
        if (
            current_revision is None
            or sealed_revision is None
            or current_revision != sealed_revision
        ):
            return False
        actual_count = int(
            c.execute(
                "SELECT COUNT(*) FROM contest_pairings "
                "INDEXED BY idx_contest_pairings_schedule "
                "WHERE contest_id=? AND stage_idx=?",
                (contest_id, stage_idx),
            ).fetchone()[0]
        )
        if actual_count != frozen_count:
            return False
        active_statuses = (
            EXECUTION_QUEUED,
            EXECUTION_STARTING,
            EXECUTION_RUNNING,
            EXECUTION_SETTLING,
        )
        if include_terminal_orphans:
            orphan = c.execute(
                "SELECT 1 FROM execution_jobs j "
                "LEFT JOIN contest_pairings p ON p.id=j.contest_pairing_id "
                "WHERE j.source=? AND j.contest_id=? AND (p.id IS NULL OR ("
                "j.status IN (?,?,?,?) AND (p.contest_id<>? OR p.stage_idx<>?))) "
                "LIMIT 1",
                (
                    EXECUTION_SOURCE_CONTEST,
                    contest_id,
                    *active_statuses,
                    contest_id,
                    stage_idx,
                ),
            ).fetchone()
        else:
            orphan = c.execute(
                "SELECT 1 FROM execution_jobs j "
                "LEFT JOIN contest_pairings p ON p.id=j.contest_pairing_id "
                "WHERE j.source=? AND j.contest_id=? "
                "AND j.status IN (?,?,?,?) "
                "AND (p.id IS NULL OR p.contest_id<>? OR p.stage_idx<>?) LIMIT 1",
                (
                    EXECUTION_SOURCE_CONTEST,
                    contest_id,
                    *active_statuses,
                    contest_id,
                    stage_idx,
                ),
            ).fetchone()
        return orphan is None

    def contest_stage_manifest_is_valid(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        include_terminal_orphans: bool = False,
        require_manifest: bool = False,
    ) -> bool:
        """Read-only wrapper for lifecycle gates outside a Store transaction."""
        with self._tx() as c:
            c.execute("BEGIN")
            return self._contest_stage_manifest_is_valid_tx(
                c,
                contest_id,
                stage_idx,
                include_terminal_orphans=include_terminal_orphans,
                require_manifest=require_manifest,
            )

    def get_contest_pairing_for_match(self, match_id: str) -> dict | None:
        """Return the unique frozen contest pairing linked to one Match.

        Execution validation cannot trust the Match's own ``contest_id`` or
        ``match_type``: those are exactly the imported fields it is checking.
        Resolve by the pairing table's unique ``match_id`` instead and project
        the same roster/owner evidence used by standings and public APIs.
        """
        if not isinstance(match_id, str) or not match_id:
            return None
        with self._tx() as c:
            sql = (
                "SELECT p.*, legacy_a.entry_id AS _effective_entry_a_id, "
                "legacy_b.entry_id AS _effective_entry_b_id, "
                "p.entry_a_id AS _raw_entry_a_id, "
                "p.entry_b_id AS _raw_entry_b_id, "
                + _contest_pairing_explicit_series_marker_sql()
                + " AS _explicit_series_marker, "
                "ea.user_id AS _entry_a_user_id, "
                "eb.user_id AS _entry_b_user_id, "
                "ba.owner_id AS _pairing_bot_a_owner_id, "
                "bb.owner_id AS _pairing_bot_b_owner_id "
                "FROM contest_pairings p "
                "LEFT JOIN contests pairing_contest "
                "ON pairing_contest.id=p.contest_id "
                f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy_a "
                "ON p.entry_a_id IS NULL AND p.bot_a_id=legacy_a.bot_id "
                "AND p.contest_id=legacy_a.contest_id "
                f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy_b "
                "ON p.entry_b_id IS NULL AND p.bot_b_id=legacy_b.bot_id "
                "AND p.contest_id=legacy_b.contest_id "
                "LEFT JOIN contest_entries ea "
                "ON ea.id=COALESCE(p.entry_a_id,legacy_a.entry_id) "
                "AND ea.contest_id=p.contest_id "
                "LEFT JOIN contest_entries eb "
                "ON eb.id=COALESCE(p.entry_b_id,legacy_b.entry_id) "
                "AND eb.contest_id=p.contest_id "
                "LEFT JOIN bots ba ON ba.id=p.bot_a_id "
                "LEFT JOIN bots bb ON bb.id=p.bot_b_id "
                "WHERE p.match_id=? ORDER BY p.id LIMIT 1"
            )
            raw = c.execute(sql, (match_id,)).fetchone()
            if raw is None:
                return None
            return _apply_effective_entry_ids(
                _row(raw),
                ("entry_a_id", "_effective_entry_a_id"),
                ("entry_b_id", "_effective_entry_b_id"),
            )

    def delete_unstarted_contest_pairings(
        self, contest_id: int, pairing_ids: list[int]
    ) -> int:
        """删除一次失败的阶段生成所留下、且尚未绑定对局的 pairing。

        这是赛事生命周期补偿专用的窄接口：调用方必须传入本次生成前后差集得到的
        精确 ID；SQL 再同时约束 contest_id 与 match_id IS NULL，避免误删并发产生或
        已经派发的合法对阵。返回实际删除行数，供调用方决定是否可安全回滚状态。
        """
        ids = sorted({int(pid) for pid in pairing_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._tx() as c:
            cur = c.execute(
                f"DELETE FROM contest_pairings WHERE contest_id=? "
                f"AND match_id IS NULL AND id IN ({placeholders})",
                [contest_id, *ids],
            )
            return int(cur.rowcount)

    def replace_unstarted_contest_stage_pairings(
        self,
        contest_id: int,
        stage_idx: int,
        pairing_rows: list[dict[str, Any]],
        *,
        expected_existing_ids: list[int],
    ) -> list[dict]:
        """published 首阶段硬崩恢复：原子替换未启动的残缺对阵批次。

        这是一个有意窄化的恢复入口，只允许当前仍为 ``published``、
        ``current_stage_idx`` 未改变，且现有 pairing 全部未绑定 match 时重建。
        已绑定、已进入 running/completed，或赛事存在任何 active match
        都是不可自动推断的不一致，必须显式报错而不能静默续跑。

        ``expected_existing_ids`` 是 manager 在同一 per-contest 锁内看到的快照；
        ``BEGIN IMMEDIATE`` 后再比对一次，阻止多进程/外部写在
        check→replace 窗口中被覆盖。
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None:
            raise ValueError("赛事阶段坐标必须是非负整数")
        _validate_pairing_publication_times(
            pairing_rows, require_published_at=True
        )
        expected_ids = sorted({int(pairing_id) for pairing_id in expected_existing_ids})
        normalized_series = _pairing_series_batch(pairing_rows)
        columns = (
            "contest_id",
            "round_num",
            "entry_a_id",
            "entry_b_id",
            "bot_a_id",
            "bot_b_id",
            "bot_a_version_id",
            "bot_b_version_id",
            "pairing_seed",
            "published_at",
            "scheduled_at",
            "match_id",
            "status",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
            "series_index",
            "series_size",
            "tiebreak_group",
            "tiebreak_game",
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_execution_admission_tx(c, maintenance_only=True)
            contest = c.execute(
                "SELECT status, current_stage_idx, stages_json "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest or contest["status"] != CONTEST_PUBLISHED:
                raise ValueError("published 赛事状态已变化，拒绝重建对阵")
            if exact_nonnegative_int(contest["current_stage_idx"]) != stage_idx:
                raise ValueError("published 赛事当前阶段已变化，拒绝重建对阵")
            stage_type = _contest_stage_type(contest["stages_json"], stage_idx)

            current = c.execute(
                "SELECT id, entry_b_id, bot_b_id, match_id, status "
                "FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? ORDER BY id",
                (contest_id, stage_idx),
            ).fetchall()
            current_ids = [int(row["id"]) for row in current]
            if current_ids != expected_ids:
                raise ValueError("published 对阵在恢复期间已变化，拒绝覆盖")
            active_request = c.execute(
                "SELECT 1 FROM execution_jobs WHERE "
                "(contest_id=? OR contest_pairing_id IN ("
                "SELECT id FROM contest_pairings WHERE contest_id=?"
                ")) AND status IN ('queued','starting','running','settling') "
                "LIMIT 1",
                (contest_id, contest_id),
            ).fetchone()
            if active_request:
                raise ValueError("published 赛事已有 active 执行请求，不能自动重建")
            if any(row["match_id"] is not None for row in current):
                raise ValueError("published 对阵已绑定对局，不能自动重建")
            if any(
                row["status"] not in (STATUS_PENDING, STATUS_COMPLETED)
                or (
                    row["status"] == STATUS_COMPLETED
                    and not is_authoritative_no_opponent_pairing(
                        stage_type, _row(row)
                    )
                )
                for row in current
            ):
                raise ValueError("published 对阵已有运行进度，不能自动重建")

            for gid in _all_game_ids():
                table = _matches_table(gid)
                active = c.execute(
                    f"SELECT 1 FROM {table} WHERE contest_id=? "
                    "AND status IN (?,?) LIMIT 1",
                    (contest_id, STATUS_PENDING, STATUS_RUNNING),
                ).fetchone()
                if active:
                    raise ValueError("published 赛事已有 active 对局，不能自动重建")

            c.execute(
                "DELETE FROM contest_pairings WHERE contest_id=? AND stage_idx=?",
                (contest_id, stage_idx),
            )
            inserted: list[dict] = []
            placeholders = ",".join("?" for _ in columns)
            for source, (series_index, series_size) in zip(
                pairing_rows, normalized_series
            ):
                row = {
                    "contest_id": contest_id,
                    "round_num": int(source.get("round_num") or 1),
                    "entry_a_id": source.get("entry_a_id"),
                    "entry_b_id": source.get("entry_b_id"),
                    "bot_a_id": source.get("bot_a_id"),
                    "bot_b_id": source.get("bot_b_id"),
                    "bot_a_version_id": source.get("bot_a_version_id"),
                    "bot_b_version_id": source.get("bot_b_version_id"),
                    "pairing_seed": source.get("pairing_seed"),
                    "published_at": source.get("published_at"),
                    "scheduled_at": source.get("scheduled_at"),
                    "match_id": None,
                    "status": source.get("status") or STATUS_PENDING,
                    "stage_idx": stage_idx,
                    "stage_key": source.get("stage_key") or "",
                    "group_id": source.get("group_id") or "",
                    "bracket_slot": source.get("bracket_slot"),
                    "color_first": int(source.get("color_first") or 0),
                    "series_index": series_index,
                    "series_size": series_size,
                    "tiebreak_group": source.get("tiebreak_group", 0),
                    "tiebreak_game": source.get("tiebreak_game", 0),
                }
                cur = c.execute(
                    f"INSERT INTO contest_pairings({','.join(columns)}) "
                    f"VALUES({placeholders})",
                    tuple(row[column] for column in columns),
                )
                inserted.append(
                    _row(
                        c.execute(
                            "SELECT * FROM contest_pairings WHERE id=?",
                            (cur.lastrowid,),
                        ).fetchone()
                    )
                )
            sealed = c.execute(
                "UPDATE contests SET published_stage_pairing_count=? "
                "WHERE id=? AND status=? AND current_stage_idx=?",
                (len(inserted), contest_id, CONTEST_PUBLISHED, stage_idx),
            )
            if sealed.rowcount != 1:
                raise ValueError("published 对阵恢复计数冻结 CAS 已失效")
            sealed_topology = c.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=? AND status=? "
                "AND current_stage_idx=? AND published_stage_pairing_count=?",
                (contest_id, CONTEST_PUBLISHED, stage_idx, len(inserted)),
            )
            if sealed_topology.rowcount != 1:
                raise ValueError("published 对阵恢复拓扑冻结 CAS 已失效")
            return inserted

    def _contest_bracket_tx(
        self,
        c: sqlite3.Connection,
        contest_id: int,
        game_id: str,
        *,
        stage_idx: int | None = None,
        include_result: bool = False,
    ) -> list[dict]:
        """Read public schedule inputs inside the caller's SQLite snapshot."""
        tbl = _matches_table(_registered_game_id(game_id))
        stage_filter = "" if stage_idx is None else "AND p.stage_idx=? "
        params: tuple[int, ...] = (
            (contest_id,) if stage_idx is None else (contest_id, stage_idx)
        )
        result_projection = (
            "m.created_at AS _match_created_at, "
            "m.id AS _match_id, "
            "m.contest_id AS _match_contest_id, "
            "m.game_id AS _match_game_id, "
            "m.match_type AS _match_type, "
            "m.bot_a_id AS _match_bot_a_id, "
            "m.bot_b_id AS _match_bot_b_id"
        )
        if include_result:
            result_projection += (
                ", m.result AS _match_result_json, "
                "m.match_config AS _match_config_json, "
                "m.reason AS _match_reason, "
                "m.technical_loss AS _match_technical_loss"
            )
        rows = c.execute(
            "SELECT p.*, "
            "COALESCE(p.entry_a_id, legacy_a.entry_id) AS _effective_entry_a_id, "
            "COALESCE(p.entry_b_id, legacy_b.entry_id) AS _effective_entry_b_id, "
            "p.entry_a_id AS _raw_entry_a_id, "
            "p.entry_b_id AS _raw_entry_b_id, "
            + _contest_pairing_explicit_series_marker_sql()
            + " AS _explicit_series_marker, "
            "ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
            "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
            "COALESCE(ua.username,eua.username) AS owner_a_name, "
            "COALESCE(ua.display_name,eua.display_name) AS owner_a_display, "
            "COALESCE(ub.username,eub.username) AS owner_b_name, "
            "COALESCE(ub.display_name,eub.display_name) AS owner_b_display, "
            "m.winner AS match_winner, m.status AS match_status, "
            "m.started_at AS started_at, m.ended_at AS ended_at, "
            "ea.user_id AS _entry_a_user_id, "
            "eb.user_id AS _entry_b_user_id, "
            "ba.owner_id AS _pairing_bot_a_owner_id, "
            "bb.owner_id AS _pairing_bot_b_owner_id, "
            + result_projection
            + " FROM contest_pairings p "
            "LEFT JOIN contests pairing_contest "
            "ON pairing_contest.id=p.contest_id "
            "LEFT JOIN bots ba ON p.bot_a_id=ba.id "
            "LEFT JOIN bots bb ON p.bot_b_id=bb.id "
            "LEFT JOIN users ua ON ba.owner_id=ua.id "
            "LEFT JOIN users ub ON bb.owner_id=ub.id "
            f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy_a "
            "ON p.entry_a_id IS NULL AND p.bot_a_id=legacy_a.bot_id "
            "AND p.contest_id=legacy_a.contest_id "
            f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy_b "
            "ON p.entry_b_id IS NULL AND p.bot_b_id=legacy_b.bot_id "
            "AND p.contest_id=legacy_b.contest_id "
            "LEFT JOIN contest_entries ea "
            "ON COALESCE(p.entry_a_id,legacy_a.entry_id)=ea.id "
            "AND ea.contest_id=p.contest_id "
            "LEFT JOIN contest_entries eb "
            "ON COALESCE(p.entry_b_id,legacy_b.entry_id)=eb.id "
            "AND eb.contest_id=p.contest_id "
            "LEFT JOIN users eua ON ea.user_id=eua.id "
            "LEFT JOIN users eub ON eb.user_id=eub.id "
            f"LEFT JOIN {tbl} m ON p.match_id=m.id "
            "WHERE p.contest_id=? "
            + stage_filter
            + "ORDER BY p.stage_idx, p.round_num, p.id",
            params,
        ).fetchall()
        return [
            _apply_effective_entry_ids(
                _row(raw),
                ("entry_a_id", "_effective_entry_a_id"),
                ("entry_b_id", "_effective_entry_b_id"),
            )
            for raw in rows
        ]

    def contest_bracket(self, contest_id: int) -> list[dict]:
        """返回对阵（带公开 Bot/owner 名、对局状态与结果摘要）。"""
        with self._tx() as c:
            contest = c.execute(
                "SELECT game_id FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            gid = _registered_game_id(contest["game_id"] if contest else None)
            return self._contest_bracket_tx(c, contest_id, gid)

    def contest_projection_snapshot(
        self,
        contest_id: int,
        *,
        stage_idx: int | None = None,
        current_stage_only: bool = False,
        include_entries: bool = True,
        include_entry_identity: bool = False,
        identity_viewer_user_id: int | None = None,
        identity_viewer_is_admin: bool = False,
        include_stage_results: bool = False,
    ) -> dict[str, Any] | None:
        """Return one replay-free contest read snapshot with compact results.

        Query count is constant in pairing count: contest, one joined pairing
        query, optionally one frozen-roster query, and optionally one persisted
        stage-results query. Private result/identity columns are consumed by
        domain projection and never cross the public allowlist.
        """
        with self._tx() as c:
            c.execute("BEGIN")
            contest_sql = "SELECT contests.*"
            if current_stage_only:
                contest_sql += (
                    ", EXISTS(SELECT 1 FROM contest_pairings future "
                    "WHERE future.contest_id=contests.id "
                    "AND future.stage_idx>contests.current_stage_idx) "
                    "AS _has_future_pairings, "
                    "(SELECT COUNT(*) FROM contest_pairings current_rows "
                    "WHERE current_rows.contest_id=contests.id "
                    "AND current_rows.stage_idx=contests.current_stage_idx) "
                    "AS _current_pairing_count"
                )
            contest_sql += " FROM contests WHERE contests.id=?"
            contest = _row(c.execute(contest_sql, (contest_id,)).fetchone())
            if contest is None:
                return None
            has_future_pairings = bool(contest.pop("_has_future_pairings", 0))
            current_pairing_count = contest.pop("_current_pairing_count", None)
            include_entry_identity = bool(
                include_entry_identity
                and int(contest.get("require_real_name") or 0)
                and (
                    identity_viewer_is_admin
                    or (
                        identity_viewer_user_id is not None
                        and int(contest.get("organizer_id") or 0)
                        == int(identity_viewer_user_id)
                    )
                )
            )
            if current_stage_only:
                if stage_idx is not None:
                    raise ValueError("current_stage_only 与 stage_idx 不能同时指定")
                raw_current_stage = contest.get("current_stage_idx", 0)
                stage_idx = (
                    raw_current_stage
                    if isinstance(raw_current_stage, int)
                    and not isinstance(raw_current_stage, bool)
                    and raw_current_stage >= 0
                    else -1
                )
                # Active polling uses the compact lifecycle-chain seal and
                # therefore reads only the current graph.  Finished legacy
                # rows with no trustworthy seal need a bounded slow path: load
                # every reached stage in this same SQLite snapshot so a fully
                # proven predecessor contradiction cannot be downgraded to
                # "unknown" merely because live omitted its pairings.
                revision = exact_nonnegative_int(
                    contest.get("pairing_topology_revision")
                )
                sealed_revision = exact_nonnegative_int(
                    contest.get("sealed_pairing_topology_revision")
                )
                manifest_count = exact_nonnegative_int(
                    contest.get("published_stage_pairing_count")
                )
                compact_chain_seal = bool(
                    revision is not None
                    and sealed_revision is not None
                    and revision == sealed_revision
                    and manifest_count is not None
                    and exact_nonnegative_int(current_pairing_count)
                    == manifest_count
                )
                load_all_pairings = bool(
                    contest.get("status") == CONTEST_FINISHED
                    and stage_idx > 0
                    and not compact_chain_seal
                )
                if load_all_pairings:
                    stage_idx = None
            else:
                load_all_pairings = False
            pairings = self._contest_bracket_tx(
                c,
                contest_id,
                _registered_game_id(contest.get("game_id")),
                stage_idx=stage_idx,
                include_result=True,
            )
            entries: list[dict[str, Any]] = []
            if include_entries:
                identity_columns = ""
                identity_join = ""
                if include_entry_identity:
                    gate = "COALESCE(identity_gate.require_real_name,0)=1"
                    identity_columns = (
                        ", identity_gate.require_real_name AS _identity_required, "
                        + _contest_identity_projection_sql(gate_sql=gate)
                    )
                    identity_join = (
                        "JOIN contests identity_gate "
                        "ON identity_gate.id=e.contest_id "
                    )
                entries_sql = (
                    "SELECT e.id,e.contest_id,e.bot_id,e.user_id,e.registered_at,"
                    "e.seed,e.group_id,e.eliminated,e.dispatched_at,"
                    "b.name AS bot_name,b.display_name AS bot_display,"
                    "b.game_id,u.username AS username,u.username AS owner_name,"
                    "u.display_name AS owner_display"
                    + identity_columns
                    + " FROM contest_entries e "
                    + identity_join
                    + "LEFT JOIN bots b ON b.id=e.bot_id "
                    "LEFT JOIN users u ON u.id=e.user_id "
                    "WHERE e.contest_id=? ORDER BY e.seed,e.registered_at,e.id"
                )
                entries = [
                    _row(row)
                    for row in c.execute(
                        entries_sql,
                        (contest_id,),
                    ).fetchall()
                ]
                for entry in entries:
                    identity_required = bool(
                        int(entry.pop("_identity_required", 0) or 0)
                    )
                    if not identity_required:
                        for field in (
                            *_CONTEST_IDENTITY_PROFILE_FIELDS,
                            "identity_source",
                            "identity_captured_at",
                            "identity_complete",
                        ):
                            entry.pop(field, None)
            stage_results: list[dict[str, Any]] = []
            if include_stage_results:
                stage_results = [
                    _apply_public_stage_result_payload(
                        _apply_effective_entry_ids(
                            _row(row), ("entry_id", "_effective_entry_id")
                        )
                    )
                    for row in c.execute(
                        "SELECT result.*, legacy.entry_id AS _effective_entry_id, "
                        "b.name AS bot_name,b.display_name AS bot_display "
                        "FROM contest_stage_results result "
                        "LEFT JOIN bots b ON b.id=result.bot_id "
                        f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy "
                        "ON result.entry_id IS NULL AND result.bot_id=legacy.bot_id "
                        "AND result.contest_id=legacy.contest_id "
                        "WHERE result.contest_id=? "
                        "ORDER BY result.stage_idx,result.points DESC,"
                        "result.delta_total DESC",
                        (contest_id,),
                    ).fetchall()
                ]
            return {
                "contest": contest,
                "pairings": pairings,
                "entries": entries,
                "stage_results": stage_results,
                "has_future_pairings": has_future_pairings,
                "pairings_include_history": load_all_pairings,
            }

    def contest_live_snapshot(self, contest_id: int) -> dict[str, Any] | None:
        """Return one transactionally consistent, replay-free live projection input.

        The fixed four SELECTs are independent of pairing count: contest state,
        current-stage pairings joined with compact Match results, the frozen
        roster, and compact prior-stage ranking snapshots needed to prove the
        current advancement cohort. Explicit ``BEGIN`` makes those reads one
        SQLite snapshot even when another process advances a stage concurrently.
        """
        return self.contest_projection_snapshot(
            contest_id,
            current_stage_only=True,
            include_entries=True,
            include_stage_results=True,
        )

    def contest_entries_page_snapshot(
        self,
        contest_id: int,
        *,
        page: int,
        per_page: int,
        viewer_user_id: int | None,
        viewer_is_admin: bool,
    ) -> dict[str, Any] | None:
        """Read roster visibility, PII gate, count and page in one snapshot.

        This is the authority boundary for the lightweight public endpoint.  A
        separate contest read followed by a second roster transaction lets an
        organizer/status change revoke access between those reads while the old
        decision still authorizes newer PII.  Explicit ``BEGIN`` fixes one
        SQLite snapshot before any ACL decision or roster column is selected.
        """
        if isinstance(contest_id, bool) or not isinstance(contest_id, int):
            raise ValueError("赛事 ID 无效")
        if not isinstance(viewer_is_admin, bool):
            raise ValueError("赛事名册管理员标记无效")
        normalized_viewer_id: int | None = None
        if viewer_user_id is not None:
            normalized_viewer_id = exact_nonnegative_int(viewer_user_id)
            if normalized_viewer_id is None or normalized_viewer_id < 1:
                raise ValueError("赛事名册查看者无效")
        if viewer_is_admin and normalized_viewer_id is None:
            raise ValueError("赛事名册管理员缺少用户身份")
        # _paginate performs the same strict type/range checks before executing
        # either COUNT or page SQL.  Validate here as well so a hidden contest
        # cannot turn invalid parameters into a misleading 404.
        if (
            isinstance(page, bool)
            or not isinstance(page, int)
            or page < 1
            or isinstance(per_page, bool)
            or not isinstance(per_page, int)
            or not 1 <= per_page <= 200
        ):
            raise ValueError("分页参数无效")

        with self._tx() as c:
            c.execute("BEGIN")
            contest_row = c.execute(
                "SELECT id,status,organizer_id,require_real_name "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if contest_row is None:
                return None
            contest = dict(contest_row)
            status = contest.get("status")
            organizer_id = exact_nonnegative_int(contest.get("organizer_id"))
            if (
                status not in {
                    CONTEST_DRAFT,
                    CONTEST_OPEN,
                    CONTEST_PUBLISHED,
                    CONTEST_RUNNING,
                    CONTEST_REST,
                    CONTEST_FINISHED,
                    CONTEST_CANCELLED,
                }
                or organizer_id is None
                or organizer_id < 1
            ):
                raise ValueError("赛事名册可见性数据损坏")
            is_organizer = bool(
                viewer_is_admin
                or (
                    normalized_viewer_id is not None
                    and normalized_viewer_id == organizer_id
                )
            )
            if status in {CONTEST_DRAFT, CONTEST_CANCELLED} and not is_organizer:
                return None
            identity_flag = exact_sqlite_bool(contest.get("require_real_name"))
            if identity_flag is None:
                raise ValueError("赛事实名标记损坏")
            include_identity = bool(is_organizer and identity_flag)

            identity_columns = ""
            identity_join = ""
            if include_identity:
                gate = "contest_gate.require_real_name=1"
                identity_columns = ", " + _contest_identity_projection_sql(
                    gate_sql=gate
                )
                identity_join = (
                    "JOIN contests contest_gate ON contest_gate.id=e.contest_id "
                )
            sql = (
                "SELECT e.id,e.user_id,e.bot_id,e.registered_at,e.group_id,"
                "e.seed,e.eliminated,b.name AS bot_name,"
                "b.display_name AS bot_display,u.username AS owner_name,"
                "u.display_name AS owner_display"
                + identity_columns
                + " FROM contest_entries e "
                + identity_join
                + "LEFT JOIN bots b ON e.bot_id=b.id "
                "LEFT JOIN users u ON e.user_id=u.id "
                "WHERE e.contest_id=? ORDER BY e.seed,e.registered_at,e.id"
            )
            rows, total = _paginate(
                c,
                sql,
                (contest_id,),
                page=page,
                per_page=per_page,
                count_query=(
                    "SELECT COUNT(*) FROM contest_entries WHERE contest_id=?"
                ),
            )
            return {
                "contest": contest,
                "items": rows,
                "page": page,
                "per_page": per_page,
                "total": total,
                "include_identity": include_identity,
            }

    def contest_entries_named(
        self,
        contest_id: int,
        *,
        page: int | None = None,
        per_page: int = 50,
        include_identity: bool = False,
    ) -> list[dict] | dict:
        """Return named entries; private identity is opt-in and contest-gated.

        LEFT JOIN bots：bot_id 现可为 NULL（删 bot 后保留 entry，P0 SET NULL）。
        默认查询不读取实名列。只有调用方显式请求且赛事本身要求实名时，才投影
        报名快照；历史 NULL 快照明确标为 current_profile_legacy 并回退当前资料。
        非实名赛事即使调用方误传 include_identity=True 也不会读取或返回 PII。
        ``page`` 为 None 时返回 list（旧契约）；给定时返回分页 dict。
        """
        with self._tx() as c:
            identity_columns = ""
            identity_join = ""
            if include_identity:
                gate = "COALESCE(identity_gate.require_real_name,0)=1"
                identity_columns = (
                    ", identity_gate.require_real_name AS _identity_required, "
                    + _contest_identity_projection_sql(gate_sql=gate)
                )
                identity_join = (
                    "JOIN contests identity_gate "
                    "ON identity_gate.id=e.contest_id "
                )
            sql = (
                "SELECT e.id,e.contest_id,e.user_id,e.bot_id,e.registered_at,"
                "e.group_id,e.seed,e.eliminated,e.dispatched_at, "
                "b.name AS bot_name, b.display_name AS bot_display, "
                "b.game_id, u.username AS username, u.username AS owner_name, "
                "u.display_name AS owner_display"
                + identity_columns
                + " FROM contest_entries e "
                + identity_join
                + "LEFT JOIN bots b ON e.bot_id=b.id "
                "LEFT JOIN users u ON e.user_id=u.id "
                "WHERE e.contest_id=? ORDER BY e.seed, e.registered_at, e.id"
            )
            params = (contest_id,)

            def finalize_identity(row: dict) -> dict:
                identity_required = bool(
                    int(row.pop("_identity_required", 0) or 0)
                )
                if not identity_required:
                    for field in (
                        *_CONTEST_IDENTITY_PROFILE_FIELDS,
                        "identity_source",
                        "identity_captured_at",
                        "identity_complete",
                    ):
                        row.pop(field, None)
                return row

            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(
                    c,
                    sql,
                    params,
                    page=page,
                    per_page=pp,
                    count_query=(
                        "SELECT COUNT(*) FROM contest_entries WHERE contest_id=?"
                    ),
                )
                if include_identity:
                    rows = [finalize_identity(row) for row in rows]
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
            rows = [_row(r) for r in c.execute(sql, params).fetchall()]
            return (
                [finalize_identity(row) for row in rows]
                if include_identity
                else rows
            )

    def list_contest_export(self, contest_id: int) -> list[dict]:
        """Private export rows with stable identities, provenance and results.

        Real-name fields are selected only for a real-name contest.  New entries
        use their immutable registration snapshot; legacy entries use an explicit
        current-profile fallback.  For unfinished contests, the latest persisted
        stage result remains visible without being mislabeled as an official rank.
        """
        with self._tx() as c:
            gate = "COALESCE(identity_gate.require_real_name,0)=1"
            identity_columns = _contest_identity_projection_sql(gate_sql=gate)
            rows = c.execute(
                "SELECT e.id AS entry_id,e.user_id,e.bot_id,e.seed,e.group_id,"
                "e.eliminated,e.registered_at, "
                "identity_gate.require_real_name AS identity_required, "
                "u.username,u.display_name AS user_display,"
                "u.username AS owner_name,u.display_name AS owner_display, "
                "b.name AS bot_name, b.display_name AS bot_display, "
                + identity_columns + ", "
                "r.rank,COALESCE(r.points,sr.points) AS points,r.awarded,"
                "COALESCE(r.stage_idx,sr.stage_idx) AS stage_idx,sr.stage_key, "
                "sr.wins,sr.draws,sr.losses,sr.delta_total, "
                "CASE WHEN r.entry_id IS NOT NULL THEN 'official' "
                "WHEN sr.entry_id IS NOT NULL THEN 'stage' ELSE 'none' END AS result_source "
                "FROM contest_entries e "
                "JOIN contests identity_gate ON identity_gate.id=e.contest_id "
                "LEFT JOIN users u ON e.user_id=u.id "
                "LEFT JOIN bots b ON e.bot_id=b.id "
                "LEFT JOIN contest_official_results r "
                "  ON r.entry_id=e.id AND r.contest_id=e.contest_id "
                "LEFT JOIN contest_stage_results sr "
                "  ON sr.entry_id=e.id AND sr.contest_id=e.contest_id "
                "  AND sr.stage_idx=COALESCE(r.stage_idx,("
                "    SELECT MAX(latest.stage_idx) FROM contest_stage_results latest "
                "    WHERE latest.contest_id=e.contest_id AND latest.entry_id=e.id"
                "  )) "
                "WHERE e.contest_id=? "
                "ORDER BY CASE WHEN r.rank IS NULL THEN 999999 ELSE r.rank END,"
                "CASE WHEN e.seed=0 THEN 999999 ELSE e.seed END,e.id",
                (contest_id,),
            ).fetchall()
            return [_row(r) for r in rows]

    def update_pairing(self, pairing_id: int, **fields: Any) -> dict | None:
        immutable = {
            "pairing_seed",
            "published_at",
            "series_index",
            "series_size",
            "tiebreak_group",
            "tiebreak_game",
        }.intersection(fields)
        if immutable:
            raise ValueError("赛事对阵发布身份字段不可修改")
        if "stage_idx" in fields:
            stage_idx = exact_nonnegative_int(fields["stage_idx"])
            if stage_idx is None:
                raise ValueError("赛事阶段坐标必须是非负整数")
            fields["stage_idx"] = stage_idx
        if "scheduled_at" in fields:
            fields["scheduled_at"] = validate_canonical_naive_timestamp(
                fields["scheduled_at"],
                "赛事对阵计划时间",
                allow_none=True,
            )
        allowed = {
            "match_id",
            "status",
            "round_num",
            "entry_a_id",
            "entry_b_id",
            "bot_a_id",
            "bot_b_id",
            "bot_a_version_id",
            "bot_b_version_id",
            "scheduled_at",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        guarded = {"bot_a_id", "bot_b_id"}.intersection(fields)
        with self._tx() as c:
            if guarded:
                c.execute("BEGIN IMMEDIATE")
                current = c.execute(
                    "SELECT contest_id,bot_a_id,bot_b_id "
                    "FROM contest_pairings "
                    "WHERE id=?",
                    (int(pairing_id),),
                ).fetchone()
                if current is not None:
                    if {"bot_a_id", "bot_b_id"}.intersection(fields):
                        _require_live_contest_pairing_bots_tx(
                            c,
                            int(current["contest_id"]),
                            fields.get("bot_a_id", current["bot_a_id"]),
                            fields.get("bot_b_id", current["bot_b_id"]),
                        )
            if sets:
                vals.append(pairing_id)
                c.execute(
                    f"UPDATE contest_pairings SET {','.join(sets)} WHERE id=?",
                    vals,
                )
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing_id,)
                ).fetchone()
            )

    update_contest_pairing = update_pairing

    def _adjudicate_unavailable_contest_pairing(
        self,
        contest_id: int,
        pairing_id: int,
        match_id: str,
        *,
        game_id: str,
        winner: int,
        result: dict[str, Any],
        time_control_id: str,
        duplicate: bool = False,
        activate_running: bool = False,
        require_execution_admission: bool = True,
    ) -> dict | None:
        """Atomically persist one pre-execution technical contest result.

        This is intentionally narrower than the normal execution path.  The
        maintenance gate, synthetic Match, rating policy/index/replay and
        pairing/lifecycle update share one ``BEGIN IMMEDIATE`` transaction, so
        deployment drain can only win before the whole adjudication or after
        it; no temporary technical Match can appear after ready became true.
        """
        gid = _registered_game_id(game_id)
        table = _matches_table(gid)
        if (
            isinstance(winner, bool)
            or not isinstance(winner, int)
            or winner not in (0, 1)
        ):
            raise ValueError("技术赛果 winner 必须为 0 或 1")
        if not isinstance(duplicate, bool):
            raise ValueError("技术赛果 duplicate 必须为布尔值")
        if not isinstance(time_control_id, str) or not time_control_id:
            raise ValueError("技术赛果必须冻结有效时限 ID")
        if _resolved_time_control_id(gid, time_control_id) != time_control_id:
            raise ValueError("技术赛果必须冻结 canonical 时限 ID")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contract = _active_game_contract_tx(c, gid)
            self._require_execution_admission_tx(
                c, maintenance_only=not require_execution_admission
            )
            contest = c.execute(
                "SELECT status,organizer_id,game_id,ruleset_version,protocol_version,"
                "rating_pool_id,stages_json,current_stage_idx,time_control_id "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest or contest["status"] not in (
                CONTEST_PUBLISHED,
                CONTEST_RUNNING,
            ):
                raise ValueError("赛事状态已变化，不能写入技术赛果")
            if (
                contest["game_id"] != gid
                or contest["ruleset_version"] != contract["ruleset_version"]
                or contest["protocol_version"] != contract["protocol_version"]
                or contest["rating_pool_id"] != contract["rating_pool_id"]
            ):
                raise ValueError("赛事规则版本已退役，不能生成新赛果")
            try:
                expected_time_control_id = _resolved_time_control_id(
                    gid, contest["time_control_id"]
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("赛事时限快照损坏") from exc
            if expected_time_control_id != time_control_id:
                raise ValueError("技术赛果时限与赛事冻结值不一致")
            pairing = c.execute(
                "SELECT * FROM contest_pairings WHERE id=? AND contest_id=? "
                "AND status=? AND match_id IS NULL",
                (pairing_id, contest_id, STATUS_PENDING),
            ).fetchone()
            if pairing is None:
                raise ValueError("对阵已被派发或状态已变化")
            pairing_stage_idx = exact_nonnegative_int(pairing["stage_idx"])
            if (
                pairing_stage_idx is None
                or exact_nonnegative_int(contest["current_stage_idx"])
                != pairing_stage_idx
            ):
                raise ValueError("赛事当前阶段已变化，不能写入技术赛果")
            if not self._contest_stage_manifest_revision_is_valid_tx(
                c,
                contest_id,
                pairing_stage_idx,
                require_manifest=True,
            ):
                self._cancel_queued_contest_batch_tx(c, contest_id)
                # Returning from inside _tx is intentional: the context exits
                # normally and commits the whole-batch cancellation, while the
                # public wrapper raises only after that transaction is closed.
                return None
            explicit_marker = _contest_stage_has_explicit_series_marker(
                contest["stages_json"], pairing_stage_idx
            )
            if explicit_marker is None:
                raise ValueError("赛事阶段系列计分契约无效")
            bot_a_id = pairing["bot_a_id"]
            bot_b_id = pairing["bot_b_id"]
            if bot_a_id is None or bot_b_id is None:
                raise ValueError("技术赛果要求双方 Bot 引用完整")
            identities = c.execute(
                "SELECT id,game_id,protocol_version FROM bots WHERE id IN (?,?)",
                (bot_a_id, bot_b_id),
            ).fetchall()
            if len({int(row["id"]) for row in identities}) != 2 or any(
                row["game_id"] != gid
                or row["protocol_version"] != contract["protocol_version"]
                for row in identities
            ):
                raise ValueError("技术赛果 Bot 不存在或游戏/协议不一致")
            for suffix, bot_id in (("a", bot_a_id), ("b", bot_b_id)):
                entry_id = pairing[f"entry_{suffix}_id"]
                if entry_id is None:
                    raise ValueError("技术赛果缺少冻结参赛项身份")
                if entry_id is not None:
                    entry = c.execute(
                        "SELECT contest_id,bot_id FROM contest_entries WHERE id=?",
                        (entry_id,),
                    ).fetchone()
                    if (
                        entry is None
                        or entry["contest_id"] != contest_id
                        or entry["bot_id"] != bot_id
                    ):
                        raise ValueError("技术赛果参赛项与冻结 Bot 身份不一致")
                version_id = pairing[f"bot_{suffix}_version_id"]
                if version_id is not None and c.execute(
                    "SELECT 1 FROM bot_versions WHERE id=? AND bot_id=?",
                    (version_id, bot_id),
                ).fetchone() is None:
                    raise ValueError("技术赛果 Bot 版本与冻结对阵不一致")
            created_at = _now()
            config = {
                "_rating_eligible": False,
                "_rating_reason": "contest",
                "duplicate": duplicate,
                "time_control_id": time_control_id,
            }
            for suffix in ("a", "b"):
                version_id = pairing[f"bot_{suffix}_version_id"]
                if version_id is not None:
                    config[f"_bot_{suffix}_version_id"] = int(version_id)
            config_json = json.dumps(config, ensure_ascii=False)
            c.execute(
                f"INSERT INTO {table}(id,bot_a_id,bot_b_id,owner_id,"
                "contest_id,reason,match_type,status,game_id,ruleset_version,"
                "protocol_version,rating_pool_id,match_config,"
                "result,winner,technical_loss,ended_at,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    match_id,
                    bot_a_id,
                    bot_b_id,
                    contest["organizer_id"],
                    contest_id,
                    "contest_bot_unavailable",
                    TYPE_CONTEST,
                    STATUS_COMPLETED,
                    gid,
                    contract["ruleset_version"],
                    contract["protocol_version"],
                    contract["rating_pool_id"],
                    config_json,
                    json.dumps(result, ensure_ascii=False),
                    int(winner),
                    1,
                    created_at,
                    created_at,
                ),
            )
            c.execute(
                "INSERT INTO match_rating_policies("
                "match_id,game_id,rating_pool_id,bot_a_id,bot_b_id,rated,rating_reason,source,"
                "classified_at) VALUES(?,?,?,?,?,0,'contest','creation_v2',?)",
                (match_id, gid, contract["rating_pool_id"], bot_a_id, bot_b_id, created_at),
            )
            c.execute(
                "INSERT INTO matches_index(id,game_id) VALUES(?,?)",
                (match_id, gid),
            )
            c.execute(
                "INSERT INTO match_replays(match_id,events_json,updated_at) "
                "VALUES(?,?,?)",
                (match_id, "[]", created_at),
            )
            authoritative_match = c.execute(
                f"SELECT * FROM {table} WHERE id=?",
                (match_id,),
            ).fetchone()
            if authoritative_match is None:
                raise RuntimeError("技术赛果 Match 写入后消失")
            _finalize_terminal_replay_tx(
                c,
                match=authoritative_match,
                updated_at=created_at,
            )
            changed = c.execute(
                "UPDATE contest_pairings SET match_id=?,status=? "
                "WHERE id=? AND contest_id=? AND status=? AND match_id IS NULL",
                (
                    match_id,
                    STATUS_COMPLETED,
                    pairing_id,
                    contest_id,
                    STATUS_PENDING,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("对阵已被派发或状态已变化")
            if activate_running:
                changed = c.execute(
                    "UPDATE contests SET status=?,starts_at=COALESCE(starts_at,?) "
                    "WHERE id=? AND status=?",
                    (
                        CONTEST_RUNNING,
                        created_at,
                        contest_id,
                        CONTEST_PUBLISHED,
                    ),
                )
                if changed.rowcount != 1:
                    raise ValueError("赛事已不处于 published 状态")
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing_id,)
                ).fetchone()
            )

    def adjudicate_unavailable_contest_pairing(
        self,
        contest_id: int,
        pairing_id: int,
        match_id: str,
        *,
        game_id: str,
        winner: int,
        result: dict[str, Any],
        time_control_id: str,
        duplicate: bool = False,
        activate_running: bool = False,
        require_execution_admission: bool = True,
    ) -> dict:
        result_row = self._adjudicate_unavailable_contest_pairing(
            contest_id,
            pairing_id,
            match_id,
            game_id=game_id,
            winner=winner,
            result=result,
            time_control_id=time_control_id,
            duplicate=duplicate,
            activate_running=activate_running,
            require_execution_admission=require_execution_admission,
        )
        if result_row is None:
            raise ValueError("赛事 active 对阵批次完整性校验失败")
        return result_row

    def bind_contest_pairing_match(
        self,
        contest_id: int,
        pairing_id: int,
        match_id: str,
        *,
        activate_running: bool = False,
        require_execution_admission: bool = True,
    ) -> dict:
        """原子绑定 prepared match，并可在同一事务把 published 赛事转 running。

        只接受仍属该赛事、仍为 pending 且 ``match_id IS NULL`` 的 pairing；这样
        challenge 准备成功后若绑定/提交失败，调用方可安全删除尚未启动的 match，
        不会留下 pairing 与 contest 状态的半提交。
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            self._require_execution_admission_tx(
                c, maintenance_only=not require_execution_admission
            )
            contest = c.execute(
                "SELECT status,game_id,stages_json,current_stage_idx,time_control_id,"
                "pairing_topology_revision,sealed_pairing_topology_revision "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            allowed = ("published", "running")
            if not contest or contest["status"] not in allowed:
                raise ValueError("赛事状态已变化，不能绑定对局")
            already_bound = c.execute(
                "SELECT id FROM contest_pairings WHERE match_id=? AND id<>? LIMIT 1",
                (match_id, pairing_id),
            ).fetchone()
            if already_bound:
                raise ValueError("同一对局不能绑定到多个赛事对阵")
            pairing = c.execute(
                "SELECT * FROM contest_pairings WHERE id=? AND contest_id=?",
                (pairing_id, contest_id),
            ).fetchone()
            table = self._match_table_of(c, match_id)
            match = (
                c.execute(
                    f"SELECT id,contest_id,game_id,match_type,bot_a_id,bot_b_id,"
                    f"match_config "
                    f"FROM {table} WHERE id=?",
                    (match_id,),
                ).fetchone()
                if table is not None
                else None
            )
            if (
                pairing is None
                or match is None
                or match["contest_id"] != contest_id
                or match["game_id"] != contest["game_id"]
                or match["match_type"] != TYPE_CONTEST
                or match["bot_a_id"] != pairing["bot_a_id"]
                or match["bot_b_id"] != pairing["bot_b_id"]
            ):
                raise ValueError("对局与赛事对阵身份不一致")
            try:
                frozen_match_config = json.loads(match["match_config"] or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError("对局时限快照损坏") from exc
            if not isinstance(frozen_match_config, dict):
                raise ValueError("对局时限快照损坏")
            try:
                expected_time_control_id = _resolved_time_control_id(
                    str(contest["game_id"]), contest["time_control_id"]
                )
                match_time_control_id = _resolved_time_control_id(
                    str(match["game_id"]),
                    frozen_match_config.get("time_control_id"),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("对局或赛事时限快照损坏") from exc
            if (
                match_time_control_id != expected_time_control_id
                or (
                    contest["time_control_id"] is not None
                    and frozen_match_config.get("time_control_id")
                    != contest["time_control_id"]
                )
            ):
                raise ValueError("对局时限与赛事冻结值不一致")
            pairing_stage_idx = exact_nonnegative_int(pairing["stage_idx"])
            if (
                pairing_stage_idx is None
                or exact_nonnegative_int(contest["current_stage_idx"])
                != pairing_stage_idx
            ):
                raise ValueError("赛事当前阶段已变化，不能绑定对局")
            if not self._contest_stage_manifest_revision_is_valid_tx(
                c,
                contest_id,
                pairing_stage_idx,
                require_manifest=True,
            ):
                raise ValueError("赛事 active 对阵批次完整性校验失败")
            explicit_marker = _contest_stage_has_explicit_series_marker(
                contest["stages_json"], pairing_stage_idx
            )
            if explicit_marker is None:
                raise ValueError("赛事阶段系列计分契约无效")
            for suffix in ("a", "b"):
                entry_id = pairing[f"entry_{suffix}_id"]
                if entry_id is None:
                    raise ValueError("对局缺少冻结参赛项身份")
                entry = c.execute(
                    "SELECT contest_id,bot_id FROM contest_entries WHERE id=?",
                    (entry_id,),
                ).fetchone()
                if (
                    entry is None
                    or entry["contest_id"] != contest_id
                    or entry["bot_id"] != pairing[f"bot_{suffix}_id"]
                ):
                    raise ValueError("对阵参赛项与冻结 Bot 身份不一致")
            try:
                frozen_config = json.loads(match["match_config"])
            except (TypeError, ValueError):
                frozen_config = None
            if not isinstance(frozen_config, dict):
                raise ValueError("对局冻结配置无效")
            for suffix in ("a", "b"):
                pairing_version = pairing[f"bot_{suffix}_version_id"]
                match_version = frozen_config.get(f"_bot_{suffix}_version_id")
                if pairing_version is None and match_version is None:
                    continue
                if (
                    isinstance(pairing_version, bool)
                    or not isinstance(pairing_version, int)
                    or isinstance(match_version, bool)
                    or not isinstance(match_version, int)
                    or pairing_version != match_version
                ):
                    raise ValueError("对局与对阵冻结 Bot 版本不一致")
            cur = c.execute(
                "UPDATE contest_pairings SET match_id=?, status='running' "
                "WHERE id=? AND contest_id=? AND status='pending' AND match_id IS NULL",
                (match_id, pairing_id, contest_id),
            )
            if cur.rowcount != 1:
                raise ValueError("对阵已被派发或状态已变化")
            if activate_running:
                cur = c.execute(
                    "UPDATE contests SET status='running', "
                    "starts_at=COALESCE(starts_at, ?) "
                    "WHERE id=? AND status='published'",
                    (_now(), contest_id),
                )
                if cur.rowcount != 1:
                    raise ValueError("赛事已不处于 published 状态")
            bound = _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing_id,)
                ).fetchone()
            )
            # Compensation runs in a later transaction if starting the
            # prepared Match fails.  Return the exact lifecycle epoch proven by
            # this bind so unbind cannot detach a Match after roster/topology
            # authority changed in between.
            bound["_bound_pairing_topology_revision"] = exact_nonnegative_int(
                contest["pairing_topology_revision"]
            )
            return bound

    def complete_contest_pairing_for_match(
        self, contest_id: int, match_id: str
    ) -> dict | None:
        """Atomically mirror one adjudicated match into its pairing status.

        The match row remains the scoring authority.  This method only updates
        the presentation/scheduling state after proving that the exact bound
        match belongs to the contest and is ``completed``; pending, running and
        aborted matches can never be mislabeled as completed.
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if (
                contest is None
                or contest["status"] in (CONTEST_FINISHED, CONTEST_CANCELLED)
            ):
                return None
            table = self._match_table_of(c, match_id)
            if not table:
                return None
            match = c.execute(
                f"SELECT * FROM {table} WHERE id=?", (match_id,)
            ).fetchone()
            if (
                not match
                or match["status"] != STATUS_COMPLETED
                or match["contest_id"] != contest_id
            ):
                return None
            pairings = c.execute(
                "SELECT id FROM contest_pairings "
                "WHERE contest_id=? AND match_id=?",
                (contest_id, match_id),
            ).fetchall()
            if not pairings:
                return None
            if len(pairings) != 1:
                raise RuntimeError(
                    f"match {match_id} 绑定了 {len(pairings)} 个赛事对阵"
                )
            pairing = pairings[0]
            _finalize_terminal_replay_tx(
                c,
                match=match,
                updated_at=_now(),
            )
            c.execute(
                "UPDATE contest_pairings SET status=? "
                "WHERE id=? AND contest_id=? AND match_id=?",
                (STATUS_COMPLETED, pairing["id"], contest_id, match_id),
            )
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing["id"],)
                ).fetchone()
            )

    def backfill_contest_actual_start(self, contest_id: int) -> str | None:
        """Backfill a missing starts_at from an owned, actually-started match.

        Only already-started lifecycle states are eligible.  Published contests
        intentionally keep NULL as the manual-start gate, so corrupt historical
        pairings can never make them start automatically during reconciliation.
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status, starts_at, registration_closes_at, ends_at "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if (
                not contest
                or contest["starts_at"]
                or contest["status"] not in (
                    CONTEST_RUNNING,
                    CONTEST_REST,
                )
            ):
                return None

            candidates: list[tuple[datetime, str]] = []
            rows = c.execute(
                "SELECT match_id FROM contest_pairings "
                "WHERE contest_id=? AND match_id IS NOT NULL",
                (contest_id,),
            ).fetchall()
            for row in rows:
                match_id = row["match_id"]
                table = self._match_table_of(c, match_id)
                if not table:
                    continue
                match = c.execute(
                    f"SELECT contest_id, status, started_at FROM {table} WHERE id=?",
                    (match_id,),
                ).fetchone()
                if (
                    not match
                    or match["contest_id"] != contest_id
                    or match["status"] == STATUS_PENDING
                    or not match["started_at"]
                ):
                    continue
                try:
                    actual = validate_canonical_naive_timestamp(
                        match["started_at"], "赛事对局开始时间"
                    )
                    parsed = datetime.fromisoformat(actual)
                except (TypeError, ValueError):
                    logger.error(
                        "contest %s match %s has invalid started_at",
                        contest_id,
                        match_id,
                    )
                    continue
                candidates.append((parsed, actual))

            if not candidates:
                return None
            _, actual = min(candidates, key=lambda item: item[0])
            actual_dt = datetime.fromisoformat(actual)
            closes = contest["registration_closes_at"]
            ends = contest["ends_at"]
            try:
                closes = validate_canonical_naive_timestamp(
                    closes, "赛事报名截止时间", allow_none=True
                )
                ends = validate_canonical_naive_timestamp(
                    ends, "赛事结束时间", allow_none=True
                )
                if closes and datetime.fromisoformat(closes) > actual_dt:
                    return None
                if ends and actual_dt > datetime.fromisoformat(ends):
                    return None
            except (TypeError, ValueError):
                return None
            cur = c.execute(
                "UPDATE contests SET starts_at=? "
                "WHERE id=? AND starts_at IS NULL "
                "AND status IN (?,?)",
                (
                    actual,
                    contest_id,
                    CONTEST_RUNNING,
                    CONTEST_REST,
                ),
            )
            return actual if cur.rowcount == 1 else None

    def unbind_prepared_contest_match(
        self,
        contest_id: int,
        pairing_id: int,
        match_id: str,
        *,
        expected_pairing_topology_revision: int,
        restore_published: bool = False,
    ) -> bool:
        """prepared Match 启动失败时按冻结 epoch 撤销绑定。

        ``False`` means compensation authority was lost.  The caller must
        retain the prepared Match in that case because it may still be the
        durable pairing authority after a concurrent lifecycle mutation.
        """
        expected_revision = exact_nonnegative_int(
            expected_pairing_topology_revision
        )
        if expected_revision is None:
            return False
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status,current_stage_idx,pairing_topology_revision,"
                "sealed_pairing_topology_revision "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            pairing = c.execute(
                "SELECT stage_idx,status,match_id FROM contest_pairings "
                "WHERE id=? AND contest_id=?",
                (pairing_id, contest_id),
            ).fetchone()
            stage_idx = exact_nonnegative_int(
                pairing["stage_idx"] if pairing else None
            )
            if (
                contest is None
                or pairing is None
                or contest["status"] not in (CONTEST_PUBLISHED, CONTEST_RUNNING)
                or stage_idx is None
                or exact_nonnegative_int(contest["current_stage_idx"])
                != stage_idx
                or pairing["status"] != STATUS_RUNNING
                or pairing["match_id"] != match_id
                or exact_nonnegative_int(contest["pairing_topology_revision"])
                != expected_revision
                or exact_nonnegative_int(
                    contest["sealed_pairing_topology_revision"]
                )
                != expected_revision
                or not self._contest_stage_manifest_revision_is_valid_tx(
                    c,
                    contest_id,
                    stage_idx,
                    require_manifest=True,
                )
            ):
                return False
            cur = c.execute(
                "UPDATE contest_pairings SET match_id=NULL, status='pending' "
                "WHERE id=? AND contest_id=? AND match_id=? AND status='running'",
                (pairing_id, contest_id, match_id),
            )
            if cur.rowcount != 1:
                return False
            if restore_published:
                other = c.execute(
                    "SELECT 1 FROM contest_pairings "
                    "WHERE contest_id=? AND match_id IS NOT NULL LIMIT 1",
                    (contest_id,),
                ).fetchone()
                if not other:
                    c.execute(
                        "UPDATE contests SET status='published' "
                        "WHERE id=? AND status='running'",
                        (contest_id,),
                    )
            return True

    # ── contest_stage_results ─────────────────────────────────

    def reset_aborted_contest_pairing(
        self, contest_id: int, match_id: str
    ) -> dict | None:
        """把一场无裁决的 aborted 赛事局从 pairing 上解绑。

        aborted match 行保留为审计/回放历史；只将仍精确绑定该
        match_id 的 pairing 原子复位为 pending，供后续安全重派。
        completed 或已被别的 match 取代的 pairing 绝不会被改写。
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if (
                contest is None
                or contest["status"]
                not in (CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST)
            ):
                return None
            table = self._match_table_of(c, match_id)
            if not table:
                return None
            match = c.execute(
                f"SELECT * FROM {table} WHERE id=?", (match_id,)
            ).fetchone()
            if (
                not match
                or match["status"] != STATUS_ABORTED
                or match["contest_id"] != contest_id
            ):
                return None
            pairing = c.execute(
                "SELECT * FROM contest_pairings "
                "WHERE contest_id=? AND match_id=? LIMIT 1",
                (contest_id, match_id),
            ).fetchone()
            if not pairing:
                return None
            _finalize_terminal_replay_tx(
                c,
                match=match,
                updated_at=_now(),
            )
            cur = c.execute(
                "UPDATE contest_pairings SET match_id=NULL, status=? "
                "WHERE id=? AND contest_id=? AND match_id=?",
                (STATUS_PENDING, pairing["id"], contest_id, match_id),
            )
            if cur.rowcount != 1:
                return None
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing["id"],)
                ).fetchone()
            )

    def upsert_stage_result(
        self,
        contest_id: int,
        stage_idx: int,
        entry_id: int,
        *,
        bot_id: int | None = None,
        stage_key: str = "",
        points: float = 0,
        wins: int = 0,
        draws: int = 0,
        losses: int = 0,
        delta_total: int = 0,
        group_id: str = "",
        rank_in_group: int | None = None,
        payload_json: str = "{}",
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO contest_stage_results"
                "(contest_id, stage_idx, stage_key, entry_id, bot_id, points, wins, "
                "draws, losses, delta_total, group_id, rank_in_group, payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(contest_id, stage_idx, entry_id) DO UPDATE SET "
                "stage_key=excluded.stage_key, bot_id=excluded.bot_id, "
                "points=excluded.points, wins=excluded.wins, draws=excluded.draws, "
                "losses=excluded.losses, delta_total=excluded.delta_total, "
                "group_id=excluded.group_id, rank_in_group=excluded.rank_in_group, "
                "payload_json=excluded.payload_json",
                (
                    contest_id, stage_idx, stage_key, entry_id, bot_id, points,
                    wins, draws, losses, delta_total, group_id, rank_in_group,
                    payload_json,
                ),
            )

    def _strict_stage_decision_tx(
        self,
        c: sqlite3.Connection,
        contest_id: int,
        stage_idx: int,
        *,
        expected_entries: list[dict[str, Any]],
        expected_stage_groups: dict[int, str] | None,
        allow_snapshot_bots: bool,
    ) -> tuple[list[sqlite3.Row], set[int], dict[int, int | None]]:
        """Validate one complete immutable decision in the caller's transaction."""
        expected_entry_ids, expected_entry_bots = (
            _validate_expected_contest_entries_tx(c, contest_id, expected_entries)
        )
        rows = c.execute(
            "SELECT * FROM contest_stage_results "
            "WHERE contest_id=? AND stage_idx=? ORDER BY entry_id",
            (contest_id, stage_idx),
        ).fetchall()
        decision_bots = (
            {
                int(row["entry_id"]): row["bot_id"]
                for row in rows
                if isinstance(row["entry_id"], int)
                and not isinstance(row["entry_id"], bool)
            }
            if allow_snapshot_bots
            else expected_entry_bots
        )
        _normalize_stage_result_batch(
            contest_id,
            stage_idx,
            [dict(row) for row in rows],
            expected_entry_ids=expected_entry_ids,
            expected_entry_bots=decision_bots,
            expected_stage_groups=expected_stage_groups,
        )
        return rows, expected_entry_ids, expected_entry_bots

    def _stage_decision_input_projection_tx(
        self,
        c: sqlite3.Connection,
        contest_id: int,
        stage_idx: int,
    ) -> tuple[dict[str, Any], str]:
        """Read and hash every current-stage ranking input in one DB snapshot."""
        contest = _row(
            c.execute("SELECT * FROM contests WHERE id=?", (contest_id,)).fetchone()
        )
        if contest is None:
            raise ValueError("赛事不存在，无法读取阶段决策输入")
        entries = [
            _row(row)
            for row in c.execute(
                "SELECT * FROM contest_entries WHERE contest_id=? "
                "ORDER BY registered_at,id",
                (contest_id,),
            ).fetchall()
        ]
        pairings = self._contest_bracket_tx(
            c,
            contest_id,
            _registered_game_id(contest.get("game_id")),
            stage_idx=stage_idx,
            include_result=True,
        )
        projection = {
            "contest": contest,
            "entries": entries,
            "pairings": pairings,
        }
        return projection, _stage_decision_input_token(contest, entries, pairings)

    def contest_stage_decision_input_snapshot(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        expected_status: str,
    ) -> dict[str, Any] | None:
        """Return one immutable candidate projection and its content token.

        Match terminal fields and pairing match bindings deliberately do not
        advance the lifecycle revision.  The typed content token binds the
        ranking replay to those exact bytes; the install transaction recomputes
        it before accepting any candidate.
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None or expected_status not in (CONTEST_RUNNING, CONTEST_REST):
            return None
        with self._tx() as c:
            c.execute("BEGIN")
            projection, token = self._stage_decision_input_projection_tx(
                c, contest_id, stage_idx
            )
            contest = projection["contest"]
            if (
                contest.get("status") != expected_status
                or exact_nonnegative_int(contest.get("current_stage_idx"))
                != stage_idx
                or not self._contest_stage_manifest_is_valid_tx(
                    c,
                    contest_id,
                    stage_idx,
                    include_terminal_orphans=True,
                    require_manifest=True,
                )
                or c.execute(
                    "SELECT 1 FROM contest_pairings WHERE contest_id=? "
                    "AND stage_idx>? LIMIT 1",
                    (contest_id, stage_idx),
                ).fetchone()
                or c.execute(
                    "SELECT 1 FROM contest_stage_results WHERE contest_id=? "
                    "AND stage_idx>? LIMIT 1",
                    (contest_id, stage_idx),
                ).fetchone()
            ):
                return None
            revision = exact_nonnegative_int(
                contest.get("pairing_topology_revision")
            )
            sealed = exact_nonnegative_int(
                contest.get("sealed_pairing_topology_revision")
            )
            if revision is None or sealed != revision:
                return None
            return {
                **projection,
                "decision_input_token": token,
                "decision_revision": revision,
            }

    def _require_stage_decision_inputs_settled_tx(
        self,
        c: sqlite3.Connection,
        contest: sqlite3.Row,
        contest_id: int,
        stage_idx: int,
        expected_entry_ids: set[int],
    ) -> None:
        """Prove the candidate decision reads one settled bound current graph."""
        if c.execute(
            "SELECT 1 FROM contest_pairings WHERE contest_id=? "
            "AND stage_idx>? LIMIT 1",
            (contest_id, stage_idx),
        ).fetchone() or c.execute(
            "SELECT 1 FROM contest_stage_results WHERE contest_id=? "
            "AND stage_idx>? LIMIT 1",
            (contest_id, stage_idx),
        ).fetchone():
            raise ValueError("赛事存在未来阶段 artifact，拒绝固化阶段决策")

        stage_type = _contest_stage_type(contest["stages_json"], stage_idx)
        table = _matches_table(_registered_game_id(contest["game_id"]))
        pairings = c.execute(
            "SELECT p.*,m.id AS _bound_match_id,m.status AS _bound_match_status,"
            "m.contest_id AS _bound_match_contest_id,"
            "m.bot_a_id AS _bound_bot_a_id,m.bot_b_id AS _bound_bot_b_id "
            f"FROM contest_pairings p LEFT JOIN {table} m ON m.id=p.match_id "
            "WHERE p.contest_id=? AND p.stage_idx=? ORDER BY p.id",
            (contest_id, stage_idx),
        ).fetchall()
        if not pairings and len(expected_entry_ids) > 1:
            raise ValueError("非空赛事阶段缺少已裁决对阵")
        if not pairings:
            return
        participants: set[int] = set()
        for raw in pairings:
            pairing = dict(raw)
            entry_a_id = exact_nonnegative_int(pairing.get("entry_a_id"))
            raw_entry_b_id = pairing.get("entry_b_id")
            entry_b_id = (
                exact_nonnegative_int(raw_entry_b_id)
                if raw_entry_b_id is not None
                else None
            )
            if (
                entry_a_id is None
                or entry_a_id < 1
                or entry_a_id not in expected_entry_ids
                or (
                    raw_entry_b_id is not None
                    and (
                        entry_b_id is None
                        or entry_b_id < 1
                        or entry_b_id not in expected_entry_ids
                        or entry_b_id == entry_a_id
                    )
                )
            ):
                raise ValueError("阶段决策对阵成员与权威 cohort 不一致")
            participants.add(entry_a_id)
            if entry_b_id is None:
                if (
                    pairing.get("match_id") is not None
                    or pairing.get("status") != STATUS_COMPLETED
                    or not is_authoritative_no_opponent_pairing(
                        stage_type, pairing
                    )
                ):
                    raise ValueError("阶段决策轮空对阵未权威裁决")
                continue
            participants.add(entry_b_id)
            if (
                not isinstance(pairing.get("match_id"), str)
                or not pairing["match_id"]
                or pairing.get("_bound_match_id") != pairing.get("match_id")
                or pairing.get("_bound_match_status") != STATUS_COMPLETED
                or pairing.get("_bound_match_contest_id") != contest_id
                or pairing.get("_bound_bot_a_id") != pairing.get("bot_a_id")
                or pairing.get("_bound_bot_b_id") != pairing.get("bot_b_id")
            ):
                raise ValueError("阶段决策对阵未绑定同赛事完整赛果")
        if participants != expected_entry_ids:
            raise ValueError("阶段决策对阵未精确覆盖权威 cohort")

    def _require_all_reached_pairings_settled_tx(
        self,
        c: sqlite3.Connection,
        contest: sqlite3.Row,
        contest_id: int,
        current_stage_idx: int,
    ) -> None:
        """Recheck every reached pairing's terminal Match binding in one tx.

        Pairing progress/bind fields and Match terminal fields intentionally do
        not advance the lifecycle-chain revision.  Manager-side topology proof
        plus a revision CAS therefore protects structural drift, while this
        writer-lock check closes the remaining race for completed/status/bind
        inputs before a legacy finished contest is promoted to ready.
        """
        current_stage_idx = exact_nonnegative_int(current_stage_idx)
        if current_stage_idx is None:
            raise ValueError("赛事终态恢复阶段游标损坏")
        stages = _loads_json(contest["stages_json"], default=[])
        if (
            not isinstance(stages, list)
            or current_stage_idx >= len(stages)
            or any(not isinstance(stage, dict) for stage in stages)
        ):
            raise ValueError("赛事终态恢复阶段配置损坏")
        game_id = _registered_game_id(contest["game_id"])
        table = _matches_table(game_id)
        pairings = c.execute(
            "SELECT p.*,m.id AS _bound_match_id,"
            "m.status AS _bound_match_status,"
            "m.contest_id AS _bound_match_contest_id,"
            "m.game_id AS _bound_match_game_id,"
            "m.match_type AS _bound_match_type,"
            "m.bot_a_id AS _bound_bot_a_id,m.bot_b_id AS _bound_bot_b_id "
            f"FROM contest_pairings p LEFT JOIN {table} m ON m.id=p.match_id "
            "WHERE p.contest_id=? AND p.stage_idx<=? "
            "ORDER BY p.stage_idx,p.id",
            (contest_id, current_stage_idx),
        ).fetchall()
        for raw in pairings:
            pairing = dict(raw)
            pairing_stage_idx = exact_nonnegative_int(pairing.get("stage_idx"))
            if (
                pairing_stage_idx is None
                or pairing_stage_idx > current_stage_idx
                or pairing_stage_idx >= len(stages)
            ):
                raise ValueError("赛事终态恢复对阵阶段坐标损坏")
            stage_type = stages[pairing_stage_idx].get("type")
            entry_a_id = exact_nonnegative_int(pairing.get("entry_a_id"))
            raw_entry_b_id = pairing.get("entry_b_id")
            entry_b_id = (
                exact_nonnegative_int(raw_entry_b_id)
                if raw_entry_b_id is not None
                else None
            )
            if entry_a_id is None or entry_a_id < 1:
                raise ValueError("赛事终态恢复对阵成员身份损坏")
            if entry_b_id is None:
                if not is_authoritative_no_opponent_pairing(stage_type, pairing):
                    raise ValueError("赛事终态恢复轮空对阵未权威裁决")
                continue
            bot_a_id = exact_nonnegative_int(pairing.get("bot_a_id"))
            bot_b_id = exact_nonnegative_int(pairing.get("bot_b_id"))
            if (
                entry_b_id < 1
                or entry_b_id == entry_a_id
                or bot_a_id is None
                or bot_a_id < 1
                or bot_b_id is None
                or bot_b_id < 1
                or not isinstance(pairing.get("match_id"), str)
                or not pairing["match_id"]
                or pairing.get("status") != STATUS_COMPLETED
                or pairing.get("_bound_match_id") != pairing.get("match_id")
                or pairing.get("_bound_match_status") != STATUS_COMPLETED
                or pairing.get("_bound_match_contest_id") != contest_id
                or pairing.get("_bound_match_game_id") != game_id
                or pairing.get("_bound_match_type") != TYPE_CONTEST
                or pairing.get("_bound_bot_a_id") != bot_a_id
                or pairing.get("_bound_bot_b_id") != bot_b_id
            ):
                raise ValueError("赛事终态恢复存在未裁决或错误绑定对阵")

        # A pending/running Match owned by the contest but no longer reachable
        # from a pairing is also progress, not a recoverable terminal state.
        for candidate_game_id in sorted(_all_game_ids()):
            candidate_table = _matches_table(candidate_game_id)
            if c.execute(
                f"SELECT 1 FROM {candidate_table} WHERE contest_id=? "
                "AND status IN (?,?) LIMIT 1",
                (contest_id, STATUS_PENDING, STATUS_RUNNING),
            ).fetchone():
                raise ValueError("赛事终态恢复仍存在 active 或孤立对局")

    def contest_stage_decision_revision(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        expected_status: str,
    ) -> int | None:
        """Return one sealed current decision-input revision, or fail closed."""
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None or expected_status not in (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
            CONTEST_REST,
        ):
            return None
        with self._tx() as c:
            c.execute("BEGIN")
            contest = c.execute(
                "SELECT status,current_stage_idx,pairing_topology_revision,"
                "sealed_pairing_topology_revision FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if (
                contest is None
                or contest["status"] != expected_status
                or exact_nonnegative_int(contest["current_stage_idx"]) != stage_idx
                or not self._contest_stage_manifest_is_valid_tx(
                    c,
                    contest_id,
                    stage_idx,
                    include_terminal_orphans=True,
                    require_manifest=True,
                )
            ):
                return None
            revision = exact_nonnegative_int(contest["pairing_topology_revision"])
            sealed = exact_nonnegative_int(
                contest["sealed_pairing_topology_revision"]
            )
            return revision if revision is not None and revision == sealed else None

    def install_contest_stage_results_if_absent(
        self,
        contest_id: int,
        stage_idx: int,
        result_rows: list[dict[str, Any]] | None,
        *,
        expected_revision: int | None,
        expected_input_token: str | None = None,
        expected_status: str,
        expected_entries: list[dict[str, Any]],
        expected_stage_groups: dict[int, str] | None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Install one decision once, or return the exact durable winner.

        Existing rows are never deleted, updated, or recomputed. A partial or
        malformed existing batch rejects the lifecycle transition. A candidate
        is accepted only against the exact sealed revision observed before Match
        replay; its row-trigger revision bump is resealed before commit.
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        normalized_expected_revision = (
            exact_nonnegative_int(expected_revision)
            if expected_revision is not None
            else None
        )
        if (
            stage_idx is None
            or expected_status not in (CONTEST_RUNNING, CONTEST_REST)
            or (result_rows is not None and not isinstance(result_rows, list))
            or (
                expected_revision is not None
                and normalized_expected_revision is None
            )
            or (
                result_rows is not None
                and (
                    not isinstance(expected_input_token, str)
                    or len(expected_input_token) != 64
                    or any(
                        char not in "0123456789abcdef"
                        for char in expected_input_token
                    )
                )
            )
        ):
            raise ValueError("阶段决策安装坐标无效")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if (
                contest is None
                or contest["status"] != expected_status
                or exact_nonnegative_int(contest["current_stage_idx"]) != stage_idx
                or not self._contest_stage_manifest_is_valid_tx(
                    c,
                    contest_id,
                    stage_idx,
                    include_terminal_orphans=True,
                    require_manifest=True,
                )
            ):
                raise ValueError("阶段决策赛事状态、游标或拓扑冻结已变化")
            revision = exact_nonnegative_int(contest["pairing_topology_revision"])
            sealed = exact_nonnegative_int(
                contest["sealed_pairing_topology_revision"]
            )
            if revision is None or sealed != revision:
                raise ValueError("阶段决策生命周期 revision 未冻结")

            existing_rows = c.execute(
                "SELECT * FROM contest_stage_results "
                "WHERE contest_id=? AND stage_idx=? ORDER BY entry_id",
                (contest_id, stage_idx),
            ).fetchall()
            if existing_rows:
                rows, _ids, _bots = self._strict_stage_decision_tx(
                    c,
                    contest_id,
                    stage_idx,
                    expected_entries=expected_entries,
                    expected_stage_groups=expected_stage_groups,
                    allow_snapshot_bots=(expected_status == CONTEST_REST),
                )
                return _stage_result_recovery_rows(rows), revision
            if result_rows is None:
                raise ValueError("阶段决策不存在，拒绝无候选重放")
            if normalized_expected_revision is None or revision != normalized_expected_revision:
                raise ValueError("阶段决策输入 revision 已变化")
            _current_projection, current_input_token = (
                self._stage_decision_input_projection_tx(
                    c, contest_id, stage_idx
                )
            )
            if current_input_token != expected_input_token:
                raise ValueError("阶段决策排名输入已变化")
            expected_entry_ids, expected_entry_bots = (
                _validate_expected_contest_entries_tx(
                    c, contest_id, expected_entries
                )
            )
            self._require_stage_decision_inputs_settled_tx(
                c, contest, contest_id, stage_idx, expected_entry_ids
            )
            normalized = _normalize_stage_result_batch(
                contest_id,
                stage_idx,
                result_rows,
                expected_entry_ids=expected_entry_ids,
                expected_entry_bots=expected_entry_bots,
                expected_stage_groups=expected_stage_groups,
            )
            _insert_stage_result_batch_tx(c, normalized)
            after = c.execute(
                "SELECT pairing_topology_revision,sealed_pairing_topology_revision "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            after_revision = exact_nonnegative_int(
                after["pairing_topology_revision"] if after else None
            )
            if (
                after_revision is None
                or exact_nonnegative_int(
                    after["sealed_pairing_topology_revision"] if after else None
                )
                != revision
            ):
                raise ValueError("阶段决策安装期间 lifecycle revision 漂移")
            resealed = c.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=? AND status=? "
                "AND current_stage_idx=? AND pairing_topology_revision=? "
                "AND sealed_pairing_topology_revision=?",
                (
                    contest_id,
                    expected_status,
                    stage_idx,
                    after_revision,
                    revision,
                ),
            )
            if resealed.rowcount != 1:
                raise ValueError("阶段决策安装后拓扑重封 CAS 失败")
            installed_rows = c.execute(
                "SELECT * FROM contest_stage_results "
                "WHERE contest_id=? AND stage_idx=? ORDER BY entry_id",
                (contest_id, stage_idx),
            ).fetchall()
            return _stage_result_recovery_rows(installed_rows), after_revision

    def enter_contest_rest_from_decision(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        expected_revision: int,
        expected_status: str,
        expected_entries: list[dict[str, Any]],
        expected_stage_groups: dict[int, str] | None,
        rest_ends_at: str,
    ) -> dict[str, Any]:
        """Consume one immutable decision and enter rest with a strict CAS."""
        stage_idx = exact_nonnegative_int(stage_idx)
        expected_revision = exact_nonnegative_int(expected_revision)
        rest_ends_at = validate_canonical_naive_timestamp(
            rest_ends_at, "赛事休息结束时间"
        )
        if (
            stage_idx is None
            or expected_revision is None
            or expected_status != CONTEST_RUNNING
        ):
            raise ValueError("赛事休息转换坐标无效")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            revision = exact_nonnegative_int(
                contest["pairing_topology_revision"] if contest else None
            )
            sealed = exact_nonnegative_int(
                contest["sealed_pairing_topology_revision"] if contest else None
            )
            if (
                contest is None
                or contest["status"] != expected_status
                or exact_nonnegative_int(contest["current_stage_idx"]) != stage_idx
                or revision != expected_revision
                or sealed != expected_revision
                or not self._contest_stage_manifest_is_valid_tx(
                    c,
                    contest_id,
                    stage_idx,
                    include_terminal_orphans=True,
                    require_manifest=True,
                )
            ):
                raise ValueError("赛事休息转换 CAS 已失效")
            self._strict_stage_decision_tx(
                c,
                contest_id,
                stage_idx,
                expected_entries=expected_entries,
                expected_stage_groups=expected_stage_groups,
                allow_snapshot_bots=False,
            )
            changed = c.execute(
                "UPDATE contests SET status=?,rest_ends_at=? WHERE id=? "
                "AND status=? AND current_stage_idx=? "
                "AND pairing_topology_revision=? "
                "AND sealed_pairing_topology_revision=?",
                (
                    CONTEST_REST,
                    rest_ends_at,
                    contest_id,
                    expected_status,
                    stage_idx,
                    expected_revision,
                    expected_revision,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("赛事休息转换 CAS 失败")
            after = c.execute(
                "SELECT pairing_topology_revision FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            after_revision = exact_nonnegative_int(
                after["pairing_topology_revision"] if after else None
            )
            if after_revision is None:
                raise ValueError("赛事休息转换 revision 损坏")
            resealed = c.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=? AND status=? "
                "AND current_stage_idx=? AND pairing_topology_revision=?",
                (contest_id, CONTEST_REST, stage_idx, after_revision),
            )
            if resealed.rowcount != 1:
                raise ValueError("赛事休息转换重封 CAS 失败")
            result = _contest_row(
                c.execute("SELECT * FROM contests WHERE id=?", (contest_id,)).fetchone()
            )
            if result is None:
                raise ValueError("赛事休息转换后记录丢失")
            return result

    def replace_stage_results(
        self,
        contest_id: int,
        stage_idx: int,
        result_rows: list[dict[str, Any]],
        *,
        expected_entries: list[dict[str, Any]] | None = None,
        expected_stage_groups: dict[int, str] | None = None,
    ) -> None:
        """Atomically replace one complete stage-ranking snapshot.

        Stage completion consumes this as one logical artifact.  Per-row
        commits can leave a prefix that later appears to be a smaller complete
        cohort, so validation, deletion, and every insert share one immediate
        transaction.  An empty legitimate cohort also clears stale rows.
        """
        if (
            isinstance(contest_id, bool)
            or not isinstance(contest_id, int)
            or contest_id < 1
            or isinstance(stage_idx, bool)
            or not isinstance(stage_idx, int)
            or stage_idx < 0
            or not isinstance(result_rows, list)
        ):
            raise ValueError("阶段结果批次坐标无效")

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            expected_entry_ids: set[int] | None = None
            expected_entry_bots: dict[int, int | None] | None = None
            if expected_entries is not None:
                contest = c.execute(
                    "SELECT status,current_stage_idx FROM contests WHERE id=?",
                    (contest_id,),
                ).fetchone()
                if (
                    contest is None
                    or contest["status"] not in (CONTEST_RUNNING, CONTEST_REST)
                    or exact_nonnegative_int(contest["current_stage_idx"])
                    != stage_idx
                ):
                    raise ValueError("阶段结果赛事状态或游标已变化")
                expected_entry_ids, expected_entry_bots = (
                    _validate_expected_contest_entries_tx(
                        c, contest_id, expected_entries
                    )
                )
            normalized = _normalize_stage_result_batch(
                contest_id,
                stage_idx,
                result_rows,
                expected_entry_ids=expected_entry_ids,
                expected_entry_bots=expected_entry_bots,
                expected_stage_groups=expected_stage_groups,
            )
            _replace_stage_result_batch_tx(
                c, contest_id, stage_idx, normalized
            )

    def list_stage_results(
        self, contest_id: int, *, stage_idx: int | None = None
    ) -> list[dict]:
        with self._tx() as c:
            sql = (
                "SELECT result.*, legacy.entry_id AS _effective_entry_id, "
                "b.name AS bot_name,b.display_name AS bot_display "
                "FROM contest_stage_results result "
                "LEFT JOIN bots b ON b.id=result.bot_id "
                f"LEFT JOIN {_UNIQUE_CONTEST_ENTRY_SQL} legacy "
                "ON result.entry_id IS NULL AND result.bot_id=legacy.bot_id "
                "AND result.contest_id=legacy.contest_id "
                "WHERE result.contest_id=?"
            )
            params: list[Any] = [contest_id]
            if stage_idx is not None:
                sql += " AND result.stage_idx=?"
                params.append(stage_idx)
            sql += " ORDER BY result.stage_idx, result.points DESC, result.delta_total DESC"
            return [
                _apply_public_stage_result_payload(
                    _apply_effective_entry_ids(
                        _row(r), ("entry_id", "_effective_entry_id")
                    )
                )
                for r in c.execute(sql, params)
            ]

    def list_stage_result_recovery_snapshots(
        self, contest_id: int, *, stage_idx: int
    ) -> list[dict]:
        """Return the private, bounded rank coordinate for crash recovery.

        Public stage-result readers deliberately discard every payload field
        except the allow-listed tie-break projection.  Recovery reads the same
        bounded projection together with the persisted rank column, without
        exposing arbitrary future envelope fields as API data.
        """
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM contest_stage_results "
                "WHERE contest_id=? AND stage_idx=? ORDER BY entry_id",
                (contest_id, stage_idx),
            ).fetchall()
        snapshots: list[dict] = []
        for raw in rows:
            row = _row(raw)
            payload = _loads_json(row.pop("payload_json", None), default={})
            if not isinstance(payload, dict):
                payload = {}
            row["tiebreaks"] = sanitize_public_contest_tiebreaks(
                payload.get("tiebreaks")
            )
            overall_rank = payload.get("overall_rank")
            row["overall_rank"] = (
                overall_rank
                if isinstance(overall_rank, int)
                and not isinstance(overall_rank, bool)
                and overall_rank >= 1
                else None
            )
            snapshots.append(row)
        return snapshots

    # ── contest_official_results（P2 全员正式名次）─────────────

    @staticmethod
    def validate_complete_official_group_coordinates(
        rows: list[dict[str, Any] | sqlite3.Row],
        *,
        expected_entry_groups: dict[int, object] | None = None,
    ) -> None:
        """Apply the shared full-table source-coordinate validation contract."""
        _validate_complete_official_group_coordinates(
            rows, expected_entry_groups=expected_entry_groups
        )

    def replace_official_results(
        self,
        contest_id: int,
        result_rows: list[dict[str, Any]],
    ) -> None:
        """Atomically replace the complete official ranking and publish readiness.

        ``DELETE`` + every replacement row + ``official_results_ready=1`` are one
        transaction.  A constraint error or process failure therefore preserves
        the previous complete ranking (or leaves a new contest at ready=0) instead
        of exposing a partial table behind a ready flag.
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            _replace_official_result_batch_tx(c, contest_id, result_rows)
            c.execute(
                "UPDATE contests SET official_results_ready=1 WHERE id=?",
                (contest_id,),
            )

    def recover_finished_contest_official_results(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        official_result_rows: list[dict[str, Any]],
        expected_revision: int,
        expected_entries: list[dict[str, Any]],
        expected_stage_groups: dict[int, str] | None,
    ) -> dict[str, Any]:
        """Publish a legacy finished/unready table from one exact decision.

        New terminal transitions commit the stage decision, official table and
        finished status atomically, so this path exists only for historical
        crash recovery.  A terminal status is not authority by itself: the
        current manifest, lifecycle revision, full roster, settled binding and
        immutable stage-result batch are revalidated under the same
        ``BEGIN IMMEDIATE`` that installs the official table and ready flag.
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        expected_revision = exact_nonnegative_int(expected_revision)
        if (
            stage_idx is None
            or expected_revision is None
            or not isinstance(official_result_rows, list)
            or not isinstance(expected_entries, list)
        ):
            raise ValueError("赛事终态恢复批次坐标无效")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            revision = exact_nonnegative_int(
                contest["pairing_topology_revision"] if contest else None
            )
            sealed = exact_nonnegative_int(
                contest["sealed_pairing_topology_revision"] if contest else None
            )
            manifest = exact_nonnegative_int(
                contest["published_stage_pairing_count"] if contest else None
            )
            if (
                contest is None
                or contest["status"] != CONTEST_FINISHED
                or exact_nonnegative_int(contest["current_stage_idx"]) != stage_idx
                or exact_sqlite_bool(contest["official_results_ready"]) is not False
                or revision != expected_revision
                or sealed != expected_revision
                or manifest is None
            ):
                raise ValueError("赛事终态恢复状态、游标或拓扑冻结已变化")
            current_count = int(
                c.execute(
                    "SELECT COUNT(*) FROM contest_pairings "
                    "WHERE contest_id=? AND stage_idx=?",
                    (contest_id, stage_idx),
                ).fetchone()[0]
            )
            if current_count != manifest:
                raise ValueError("赛事终态恢复对阵批次计数不一致")
            if c.execute(
                "SELECT 1 FROM execution_jobs WHERE source=? AND contest_id=? "
                "AND status IN (?,?,?,?) LIMIT 1",
                (
                    EXECUTION_SOURCE_CONTEST,
                    contest_id,
                    EXECUTION_QUEUED,
                    EXECUTION_STARTING,
                    EXECUTION_RUNNING,
                    EXECUTION_SETTLING,
                ),
            ).fetchone():
                raise ValueError("赛事终态恢复仍存在 active 执行请求")

            _rows, expected_entry_ids, _bots = self._strict_stage_decision_tx(
                c,
                contest_id,
                stage_idx,
                expected_entries=expected_entries,
                expected_stage_groups=expected_stage_groups,
                # A Bot may be replaced while the contest rests after this
                # decision.  The official batch is rebound to the current
                # roster, while the immutable stage row keeps who played it.
                allow_snapshot_bots=True,
            )
            self._require_stage_decision_inputs_settled_tx(
                c, contest, contest_id, stage_idx, expected_entry_ids
            )
            self._require_all_reached_pairings_settled_tx(
                c, contest, contest_id, stage_idx
            )
            _replace_official_result_batch_tx(
                c, contest_id, official_result_rows
            )
            changed = c.execute(
                "UPDATE contests SET official_results_ready=1 "
                "WHERE id=? AND status=? AND current_stage_idx=? "
                "AND official_results_ready=0 "
                "AND pairing_topology_revision=? "
                "AND sealed_pairing_topology_revision=?",
                (
                    contest_id,
                    CONTEST_FINISHED,
                    stage_idx,
                    expected_revision,
                    expected_revision,
                ),
            )
            if changed.rowcount != 1:
                raise ValueError("赛事终态恢复 ready CAS 已失效")
            after = c.execute(
                "SELECT pairing_topology_revision FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            after_revision = exact_nonnegative_int(
                after["pairing_topology_revision"] if after else None
            )
            if after_revision is None:
                raise ValueError("赛事终态恢复 lifecycle revision 损坏")
            resealed = c.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=? AND status=? "
                "AND current_stage_idx=? AND official_results_ready=1 "
                "AND pairing_topology_revision=?",
                (
                    contest_id,
                    CONTEST_FINISHED,
                    stage_idx,
                    after_revision,
                ),
            )
            if resealed.rowcount != 1:
                raise ValueError("赛事终态恢复重封 CAS 已失效")
            result = _contest_row(
                c.execute(
                    "SELECT * FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
            )
            if result is None:  # pragma: no cover - same transaction invariant
                raise ValueError("赛事终态恢复后记录丢失")
            return result

    def finish_contest_with_results(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        stage_result_rows: list[dict[str, Any]] | None,
        official_result_rows: list[dict[str, Any]],
        expected_decision_revision: int,
        expected_status: str,
        expected_entries: list[dict[str, Any]],
        expected_stage_groups: dict[int, str] | None,
        entry_updates: list[dict[str, Any]] | None = None,
        ends_at: str,
    ) -> dict[str, Any]:
        """Atomically publish the terminal stage, official table and status.

        The completed stage decision is always installed before this transaction
        and consumed in place (entrants may have changed Bot during a rest
        window). ``entry_updates`` is reserved for a legitimate zero/one-person
        next-stage shortcut: its complete advancement CAS is applied after the
        deciding stage snapshot has been validated but before the official
        table is checked.  Any validation, trigger or CAS failure rolls the
        whole unit back and leaves the contest retryable in its prior active
        state.
        """
        stage_idx = exact_nonnegative_int(stage_idx)
        expected_decision_revision = exact_nonnegative_int(
            expected_decision_revision
        )
        ends_at = validate_canonical_naive_timestamp(
            ends_at, "赛事结束时间"
        )
        normalized_entry_updates = (
            _contest_entry_advancement_batch(entry_updates)
            if entry_updates is not None
            else None
        )
        if (
            stage_idx is None
            or expected_decision_revision is None
            or not isinstance(expected_status, str)
            or expected_status not in (CONTEST_RUNNING, CONTEST_REST)
            or not isinstance(official_result_rows, list)
            or stage_result_rows is not None
        ):
            raise ValueError("赛事终态结果批次坐标无效")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if (
                contest is None
                or contest["status"] != expected_status
                or exact_nonnegative_int(contest["current_stage_idx"])
                != stage_idx
                or exact_sqlite_bool(contest["official_results_ready"])
                is not False
                or exact_nonnegative_int(contest["pairing_topology_revision"])
                != expected_decision_revision
                or exact_nonnegative_int(
                    contest["sealed_pairing_topology_revision"]
                )
                != expected_decision_revision
            ):
                raise ValueError("赛事终态状态、游标或就绪标记已变化")
            if not self._contest_stage_manifest_is_valid_tx(
                c,
                contest_id,
                stage_idx,
                include_terminal_orphans=True,
            ):
                raise ValueError("赛事当前阶段对阵批次完整性校验失败")

            expected_entry_ids, expected_entry_bots = (
                _validate_expected_contest_entries_tx(
                    c, contest_id, expected_entries
                )
            )
            self._strict_stage_decision_tx(
                c,
                contest_id,
                stage_idx,
                expected_entries=expected_entries,
                expected_stage_groups=expected_stage_groups,
                allow_snapshot_bots=(expected_status == CONTEST_REST),
            )

            if normalized_entry_updates is not None:
                _apply_contest_entry_advancement_tx(
                    c, contest_id, normalized_entry_updates
                )

            _replace_official_result_batch_tx(
                c, contest_id, official_result_rows
            )
            updated = c.execute(
                "UPDATE contests SET status=?,ends_at=?,rest_ends_at=NULL,"
                "official_results_ready=1 WHERE id=? AND status=? "
                "AND current_stage_idx=? AND official_results_ready=0",
                (
                    CONTEST_FINISHED,
                    ends_at,
                    contest_id,
                    expected_status,
                    stage_idx,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("赛事终态 CAS 失败")
            after = c.execute(
                "SELECT pairing_topology_revision FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            after_revision = exact_nonnegative_int(
                after["pairing_topology_revision"] if after else None
            )
            if after_revision is None:
                raise ValueError("赛事终态 lifecycle revision 损坏")
            resealed = c.execute(
                "UPDATE contests SET sealed_pairing_topology_revision="
                "pairing_topology_revision WHERE id=? AND status=? "
                "AND current_stage_idx=? AND pairing_topology_revision=?",
                (contest_id, CONTEST_FINISHED, stage_idx, after_revision),
            )
            if resealed.rowcount != 1:
                raise ValueError("赛事终态重封 CAS 失败")
            result = _contest_row(
                c.execute(
                    "SELECT * FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
            )
            if result is None:  # pragma: no cover - same transaction invariant
                raise ValueError("赛事终态写入后记录丢失")
            return result

    def clear_official_results(self, contest_id: int) -> None:
        with self._tx() as c:
            c.execute(
                "DELETE FROM contest_official_results WHERE contest_id=?",
                (contest_id,),
            )

    def upsert_official_result(
        self,
        contest_id: int,
        entry_id: int,
        rank: int,
        *,
        stage_idx: int = 0,
        points: float = 0,
        bot_id: int | None = None,
        user_id: int | None = None,
        group_id: str = "",
        rank_in_group: int | None = None,
        tiebreaks_json: str = "{}",
        awarded: str = "",
    ) -> None:
        group_id, rank_in_group = _parse_official_group_coordinates(
            group_id, rank_in_group
        )
        with self._tx() as c:
            c.execute(
                "INSERT INTO contest_official_results"
                "(contest_id, entry_id, stage_idx, rank, points, bot_id, user_id, "
                "group_id, rank_in_group, tiebreaks_json, awarded) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(contest_id, entry_id) DO UPDATE SET "
                "stage_idx=excluded.stage_idx, rank=excluded.rank, "
                "points=excluded.points, bot_id=excluded.bot_id, "
                "user_id=excluded.user_id, group_id=excluded.group_id, "
                "rank_in_group=excluded.rank_in_group, "
                "tiebreaks_json=excluded.tiebreaks_json, "
                "awarded=excluded.awarded",
                (
                    contest_id, entry_id, stage_idx, rank, points, bot_id, user_id,
                    group_id, rank_in_group, tiebreaks_json, awarded,
                ),
            )

    def list_official_results(self, contest_id: int) -> list[dict]:
        """全员正式名次（按 rank 升序，1..N 唯一连续）。"""
        with self._tx() as c:
            c.execute("BEGIN")
            if not c.execute(
                "SELECT 1 FROM contests WHERE id=?", (contest_id,)
            ).fetchone():
                return []
            (
                contest,
                roster_rows,
                stage_entry_ids,
                legacy_entry_groups,
            ) = (
                _official_result_validation_context_tx(c, contest_id)
            )
            rows = c.execute(
                "SELECT r.*, b.name AS bot_name, b.display_name AS bot_display, "
                "u.username AS owner_name, u.display_name AS owner_display "
                "FROM contest_official_results r "
                "LEFT JOIN bots b ON r.bot_id=b.id "
                "LEFT JOIN users u ON r.user_id=u.id "
                "WHERE r.contest_id=? ORDER BY r.rank",
                (contest_id,),
            ).fetchall()
            if exact_sqlite_bool(contest.get("official_results_ready")) is True:
                return _validate_complete_official_results(
                    rows,
                    contest_id=contest_id,
                    contest=contest,
                    roster_rows=roster_rows,
                    stage_entry_ids=stage_entry_ids,
                    legacy_entry_groups=legacy_entry_groups,
                )
            parsed: list[dict[str, Any]] = []
            for raw in rows:
                row = dict(raw)
                row["group_id"], row["rank_in_group"] = (
                    _parse_official_group_coordinates(
                        row.get("group_id"), row.get("rank_in_group")
                    )
                )
                parsed.append(row)
            return parsed

    # ── contest_templates（历史只读；运行模板来自代码注册表）──

    def list_contest_templates(self, *, game_id: str | None = None) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM contest_templates"
            params: list[Any] = []
            if game_id:
                sql += " WHERE game_id=?"
                params.append(game_id)
            sql += " ORDER BY is_builtin DESC, id"
            rows = [_row(r) for r in c.execute(sql, params)]
        for r in rows:
            r["stages"] = _loads_json(r.get("stages_json"), default=[])
            r["match_config"] = _loads_json(r.get("match_config"), default={})
        return rows

    def get_contest_template(self, tid: str) -> dict | None:
        with self._tx() as c:
            r = _row(
                c.execute(
                    "SELECT * FROM contest_templates WHERE id=?", (tid,)
                ).fetchone()
            )
        if not r:
            return None
        r["stages"] = _loads_json(r.get("stages_json"), default=[])
        r["match_config"] = _loads_json(r.get("match_config"), default={})
        return r

    # ── platform_settings ─────────────────────────────────────

    def get_auto_match_enabled(self) -> bool:
        """Return the producer-only switch from the global queue singleton."""
        with self._tx() as c:
            row = c.execute(
                "SELECT auto_enabled FROM execution_control WHERE singleton=1"
            ).fetchone()
            # SCHEMA always seeds the singleton.  Missing/corrupt state fails closed.
            return bool(row and int(row["auto_enabled"]) == 1)

    def set_auto_match_enabled(self, enabled: bool) -> bool:
        if type(enabled) is not bool:  # bool is deliberately strict at Store boundary.
            raise ValueError("自动排位总开关必须是布尔值")
        return self.executions.set_auto_enabled(enabled)

    def rating_integrity_diagnostics(self) -> dict:
        """Read-only No-Go audit for legacy eligibility and projection state.

        ``direct_polluted_bot_ids`` only names Bots in the invalid same-owner
        matches.  It deliberately does *not* claim to be the full impact set:
        Glicko propagation can change every later opponent, so only the offline
        rebuild dry-run's whole-leaderboard diff/hash is authoritative.
        """
        with self._tx() as c:
            matches: list[dict[str, Any]] = []
            direct_polluted: set[int] = set()
            for gid in sorted(_all_game_ids()):
                table = _matches_table(gid)
                rows = c.execute(
                    f"SELECT m.id,m.game_id,policy.bot_a_id,policy.bot_b_id,"
                    "m.created_at,m.ended_at,policy.rating_reason,"
                    "settled.settled_order,"
                    "a.owner_id AS owner_a_id,b.owner_id AS owner_b_id "
                    f"FROM {table} m "
                    "JOIN match_rating_policies policy ON policy.match_id=m.id "
                    "JOIN match_rating_settlements settled ON settled.match_id=m.id "
                    "LEFT JOIN bots a ON a.id=policy.bot_a_id "
                    "LEFT JOIN bots b ON b.id=policy.bot_b_id "
                    "WHERE policy.source='legacy_migration' "
                    "AND policy.rating_reason IN ('same_owner','self_play') "
                    "AND m.status=? ORDER BY settled.settled_order,m.ended_at,m.id",
                    (STATUS_COMPLETED,),
                ).fetchall()
                for row in rows:
                    item = dict(row)
                    matches.append(item)
                    if item["rating_reason"] == "same_owner":
                        for key in ("bot_a_id", "bot_b_id"):
                            if item.get(key) is not None:
                                direct_polluted.add(int(item[key]))
            matches.sort(
                key=lambda item: (
                    int(item.get("settled_order") or 0),
                    str(item.get("ended_at") or ""),
                    str(item["id"]),
                )
            )
            polluted = [
                row for row in matches if row["rating_reason"] == "same_owner"
            ]
            neutral_self = [
                row for row in matches if row["rating_reason"] == "self_play"
            ]
            state_row = c.execute(
                "SELECT * FROM rating_projection_state WHERE singleton=1"
            ).fetchone()
            state = dict(state_row) if state_row else {
                "policy_version": _RATING_PROJECTION_LEGACY_VERSION,
                "rebuilt_at": None,
                "source_settlement_count": 0,
                "source_last_settled_order": 0,
            }
            projection = self._rating_projection_status_tx(c)
            rebuild_required = not bool(projection["ready"])
            return {
                "rebuild_required": rebuild_required,
                "required_policy_version": _RATING_PROJECTION_POLICY_VERSION,
                "projection_state": state,
                "source_settlement_count": projection["source_settlement_count"],
                "source_last_settled_order": projection["source_last_settled_order"],
                "legacy_same_owner_polluted_count": len(polluted),
                "legacy_self_play_neutral_count": len(neutral_self),
                # Compatibility name remains explicit about being direct-only.
                "legacy_same_owner_settled_count": len(polluted),
                "direct_polluted_bot_ids": sorted(direct_polluted),
                "affected_bot_ids": sorted(direct_polluted),
                "projected_impact_scope": "full_leaderboard_dry_run_required",
                "authoritative_rebuild_order": "settled_order",
                "matches": matches,
            }

    def get_setting(self, key: str) -> str | None:
        with self._tx() as c:
            row = c.execute(
                "SELECT value FROM platform_settings WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO platform_settings(key, value, updated_at) "
                "VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (key, value, _now()),
            )

    def set_settings(self, values: dict[str, str]) -> None:
        """Upsert a validated settings batch in one transaction.

        Callers must validate the whole batch first.  If any statement fails,
        ``_tx`` rolls every preceding upsert back, preventing mixed old/new
        runtime configuration.
        """
        if not values:
            return
        updated_at = _now()
        with self._tx() as c:
            c.executemany(
                "INSERT INTO platform_settings(key, value, updated_at) "
                "VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                [(key, value, updated_at) for key, value in values.items()],
            )

    def get_settings(self, keys: list[str] | None = None) -> dict[str, str]:
        with self._tx() as c:
            if keys:
                placeholders = ",".join("?" * len(keys))
                rows = c.execute(
                    f"SELECT key, value FROM platform_settings WHERE key IN ({placeholders})",
                    keys,
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT key, value FROM platform_settings"
                ).fetchall()
            return {r[0]: r[1] for r in rows}

    def seed_setting_if_absent(self, key: str, value: str) -> None:
        with self._tx() as c:
            exists = c.execute(
                "SELECT 1 FROM platform_settings WHERE key=?", (key,)
            ).fetchone()
            if not exists:
                c.execute(
                    "INSERT INTO platform_settings(key, value, updated_at) "
                    "VALUES(?,?,?)",
                    (key, value, _now()),
                )

    # ── email_templates ───────────────────────────────────────

    def get_template(self, key: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM email_templates WHERE key=?", (key,)
                ).fetchone()
            )

    get_email_template = get_template

    def list_templates(self) -> list[dict]:
        with self._tx() as c:
            return [
                _row(r)
                for r in c.execute("SELECT * FROM email_templates ORDER BY key")
            ]

    list_email_templates = list_templates

    def update_template(
        self, key: str, *, subject: str, body_html: str, body_text: str
    ) -> dict:
        with self._tx() as c:
            c.execute(
                "INSERT INTO email_templates"
                "(key, subject, body_html, body_text, updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "subject=excluded.subject, body_html=excluded.body_html, "
                "body_text=excluded.body_text, updated_at=excluded.updated_at",
                (key, subject, body_html, body_text, _now()),
            )
            return _row(
                c.execute(
                    "SELECT * FROM email_templates WHERE key=?", (key,)
                ).fetchone()
            )

    upsert_email_template = update_template

    # ── email_outbox ──────────────────────────────────────────

    def add_outbox(
        self,
        to_addr: str,
        subject: str,
        *,
        template_key: str = "",
        status: str = "sent",
        error: str = "",
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO email_outbox"
                "(to_addr, subject, template_key, status, error, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (to_addr, subject, template_key, status, error, _now()),
            )

    add_email_outbox = add_outbox

    def list_outbox(
        self,
        *,
        status: str | None = None,
        template_key: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """发件箱查询（管理员面板用）。"""
        with self._tx() as c:
            sql = "SELECT * FROM email_outbox WHERE 1=1"
            params: list[Any] = []
            if status:
                sql += " AND status=?"
                params.append(status)
            if template_key:
                sql += " AND template_key=?"
                params.append(template_key)
            sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            return [_row(r) for r in c.execute(sql, params)]

    # ── sessions 查询（管理端） ─────────────────────────────

    def list_sessions(
        self, user_id: int | None = None, *, limit: int = 100
    ) -> list[dict]:
        """列会话（可选按用户）。关联用户名便于展示。"""
        with self._tx() as c:
            sql = (
                f"SELECT {','.join(_ADMIN_SESSION_COLUMNS)} FROM sessions s "
                "LEFT JOIN users u ON s.user_id=u.id WHERE 1=1"
            )
            params: list[Any] = []
            if user_id is not None:
                sql += " AND s.user_id=?"
                params.append(user_id)
            sql += " ORDER BY s.created_at DESC LIMIT ?"
            params.append(limit)
            return [_row(r) for r in c.execute(sql, params)]

    # ── 删除（管理端，schema 均 ON DELETE CASCADE） ─────────

    def delete_user_if_safe(self, user_id: int) -> dict:
        """原子拒绝会破坏历史或活跃参与者身份的管理员用户硬删。

        删除用户会经 ``users → bots`` 级联，不能只依赖 Bot 删除端点的保护。
        本方法在 ``BEGIN IMMEDIATE`` 事务内先汇总该用户全部 Bot 的活跃引用及
        其组织的赛事，再决定是否删除；这样另一个连接也不能在检查和 DELETE
        之间插入新的引用。已参赛用户应改为停用，不能依赖 SET NULL/CASCADE
        把历史身份改成不可恢复的“已删除”。

        返回 ``found/deleted/bot_ids/blockers``；成功时调用方可用删除前保存的
        ``bot_ids`` 清理对应上传目录。
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            user = c.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                return {
                    "found": False,
                    "deleted": False,
                    "bot_ids": [],
                    "blockers": {},
                }

            bot_ids = [
                int(row["id"])
                for row in c.execute(
                    "SELECT id FROM bots WHERE owner_id=? ORDER BY id", (user_id,)
                ).fetchall()
            ]
            match_count = 0
            for gid in _all_game_ids():
                table = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) AS n FROM {table} "
                    "WHERE ("
                    "bot_a_id IN (SELECT id FROM bots WHERE owner_id=?) OR "
                    "bot_b_id IN (SELECT id FROM bots WHERE owner_id=?) OR "
                    "owner_id=? OR human_user_id=?)",
                    (user_id, user_id, user_id, user_id),
                ).fetchone()
                match_count += int(row["n"] if row else 0)

            pairing_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_pairings cp "
                "WHERE ("
                "cp.bot_a_id IN (SELECT id FROM bots WHERE owner_id=?) OR "
                "cp.bot_b_id IN (SELECT id FROM bots WHERE owner_id=?))",
                (user_id, user_id),
            ).fetchone()
            entry_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_entries entry "
                "WHERE entry.user_id=? OR "
                "entry.bot_id IN (SELECT id FROM bots WHERE owner_id=?)",
                (user_id, user_id),
            ).fetchone()
            organized_row = c.execute(
                "SELECT COUNT(*) AS n FROM contests WHERE organizer_id=?", (user_id,)
            ).fetchone()
            execution_row = c.execute(
                "SELECT COUNT(*) AS n FROM execution_jobs job "
                "WHERE job.status IN ('queued','starting','running','settling') "
                "AND (job.owner_user_id=? OR job.human_user_id=? OR "
                "job.bot_a_id IN (SELECT id FROM bots WHERE owner_id=?) OR "
                "job.bot_b_id IN (SELECT id FROM bots WHERE owner_id=?))",
                (user_id, user_id, user_id, user_id),
            ).fetchone()
            blockers = {
                "matches": match_count,
                "contest_pairings": int(pairing_row["n"] if pairing_row else 0),
                "contest_entries": int(entry_row["n"] if entry_row else 0),
                "organized_contests": int(organized_row["n"] if organized_row else 0),
                "active_execution_jobs": int(execution_row["n"] if execution_row else 0),
                "audit_versions": _cutover_audit_version_count_tx(
                    c, owner_id=user_id
                ),
            }
            if any(blockers.values()):
                return {
                    "found": True,
                    "deleted": False,
                    "bot_ids": bot_ids,
                    "blockers": blockers,
                }

            # users→comments/bots 会级联，但 likes.target_id 是多态文本列，不具备
            # DB 外键。先清该用户所写评论收到的赞，再清其每个 Bot 的社交目标。
            projection_guard = self._rating_projection_mutation_guard_tx(c)
            _delete_comment_likes_for(c, "user_id=?", (user_id,))
            _delete_user_likes(c, user_id)
            for bot_id in bot_ids:
                _delete_social_target(c, "bot", bot_id)
            deleted = c.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount > 0
            if deleted:
                self._advance_rating_projection_state_tx(c, projection_guard)
            return {
                "found": True,
                "deleted": deleted,
                "bot_ids": bot_ids,
                "blockers": blockers,
            }

    def delete_user(self, user_id: int) -> bool:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM users WHERE id=?", (user_id,)).fetchone():
                return False
            if _cutover_audit_version_count_tx(c, owner_id=user_id):
                raise ValueError("规则迁移版本是不可删除审计证据，禁止删除用户")
            _delete_comment_likes_for(c, "user_id=?", (user_id,))
            _delete_user_likes(c, user_id)
            for row in c.execute(
                "SELECT id FROM bots WHERE owner_id=?", (user_id,)
            ).fetchall():
                _delete_social_target(c, "bot", int(row["id"]))
            cur = c.execute("DELETE FROM users WHERE id=?", (user_id,))
            return cur.rowcount > 0

    def delete_contest(self, contest_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM contests WHERE id=?", (contest_id,))
            return cur.rowcount > 0

    def delete_entry(self, contest_id: int, user_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM contest_entries WHERE contest_id=? AND user_id=?",
                (contest_id, user_id),
            )
            return cur.rowcount > 0

    # ── 聚合统计（仪表盘） ──────────────────────────────────

    def count_stats(self) -> dict:
        """一次性聚合各表计数 + 对局按状态分组 + 最近趋势。

        对局计数跨三张 per-game 表求和（全面解耦 PR3）。
        """
        with self._tx() as c:
            def one(sql: str, *p: Any) -> int:
                return int(c.execute(sql, p).fetchone()[0])

            def visible_user(column: str) -> str:
                return (
                    "NOT EXISTS (SELECT 1 FROM contests sc "
                    f"WHERE sc.showcase_key IS NOT NULL AND sc.organizer_id={column}) "
                    "AND NOT EXISTS (SELECT 1 FROM contest_entries sce "
                    "JOIN contests sc ON sc.id=sce.contest_id "
                    f"WHERE sc.showcase_key IS NOT NULL AND sce.user_id={column})"
                )

            def visible_bot(column: str) -> str:
                return (
                    "NOT EXISTS (SELECT 1 FROM contest_entries sce "
                    "JOIN contests sc ON sc.id=sce.contest_id "
                    f"WHERE sc.showcase_key IS NOT NULL AND sce.bot_id={column}) "
                    "AND NOT EXISTS (SELECT 1 FROM contest_pairings scp "
                    "JOIN contests sc ON sc.id=scp.contest_id "
                    "WHERE sc.showcase_key IS NOT NULL "
                    f"AND (scp.bot_a_id={column} OR scp.bot_b_id={column}))"
                )

            match_visible = (
                "NOT EXISTS (SELECT 1 FROM contests sc "
                "WHERE sc.id=m.contest_id AND sc.showcase_key IS NOT NULL)"
            )

            def match_count(status: str | None = None) -> int:
                """跨三表统计对局数（可选 status 过滤）。"""
                total = 0
                for gid in _all_game_ids():
                    tbl = _matches_table(gid)
                    if status:
                        total += one(
                            f"SELECT COUNT(*) FROM {tbl} m "
                            f"WHERE {match_visible} AND m.status=?",
                            status,
                        )
                    else:
                        total += one(
                            f"SELECT COUNT(*) FROM {tbl} m WHERE {match_visible}"
                        )
                return total

            stats = {
                "users": one(f"SELECT COUNT(*) FROM users u WHERE {visible_user('u.id')}"),
                "users_active": one(
                    f"SELECT COUNT(*) FROM users u WHERE u.is_active=1 "
                    f"AND {visible_user('u.id')}"
                ),
                "users_verified": one(
                    f"SELECT COUNT(*) FROM users u WHERE u.email_verified=1 "
                    f"AND {visible_user('u.id')}"
                ),
                "bots": one(f"SELECT COUNT(*) FROM bots b WHERE {visible_bot('b.id')}"),
                "bots_active": one(
                    f"SELECT COUNT(*) FROM bots b WHERE b.is_active=1 "
                    f"AND {visible_bot('b.id')}"
                ),
                "matches": match_count(),
                "matches_completed": match_count("completed"),
                "matches_aborted": match_count("aborted"),
                "matches_running": match_count("running"),
                "matches_pending": match_count("pending"),
                "contests": one(
                    "SELECT COUNT(*) FROM contests WHERE showcase_key IS NULL"
                ),
                "contests_running": one(
                    "SELECT COUNT(*) FROM contests "
                    "WHERE status='running' AND showcase_key IS NULL"
                ),
                "active_sessions": one(
                    f"SELECT COUNT(*) FROM sessions s WHERE s.expires_at > ? "
                    f"AND {visible_user('s.user_id')}",
                    _now(),
                ),
            }
            # 按对局状态分组（跨三表 UNION ALL 再聚合）
            subs = [
                f"SELECT m.status, COUNT(*) AS n FROM {_matches_table(gid)} m "
                f"WHERE {match_visible} GROUP BY m.status"
                for gid in _all_game_ids()
            ]
            rows = c.execute(
                f"SELECT status, SUM(n) AS n FROM ({' UNION ALL '.join(subs)}) "
                "GROUP BY status"
            ).fetchall()
            stats["matches_by_status"] = {r["status"]: int(r["n"]) for r in rows}
            # 最近 7 天每日新对局数（跨三表）
            subs_recent = [
                f"SELECT substr(created_at,1,10) AS d, COUNT(*) AS n "
                f"FROM {_matches_table(gid)} m "
                f"WHERE m.created_at >= date('now','-7 days') AND {match_visible} "
                "GROUP BY substr(m.created_at,1,10)"
                for gid in _all_game_ids()
            ]
            recent = c.execute(
                f"SELECT d, SUM(n) AS n FROM ({' UNION ALL '.join(subs_recent)}) "
                "GROUP BY d ORDER BY d"
            ).fetchall()
            stats["matches_recent_daily"] = [
                {"date": r["d"], "count": int(r["n"])} for r in recent
            ]
            # 最近 5 个用户
            recent_users = c.execute(
                "SELECT u.id, u.username, u.email, u.role, u.created_at FROM users u "
                f"WHERE {visible_user('u.id')} "
                "ORDER BY u.created_at DESC LIMIT 5"
            ).fetchall()
            stats["recent_users"] = [_row(r) for r in recent_users]
            return stats
