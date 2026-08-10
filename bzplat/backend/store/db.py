"""botzone-platform SQLite 存储层。

持久连接 + threading.Lock；时间戳统一 ISO 秒精度；行返回 dict。
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from bzplat.backend.mail import seed_email_templates

from .public_contract import (
    READ_TECHNICAL_INCIDENT_EVENTS,
    canonical_public_completed_reason,
    sanitize_public_event,
    sanitize_public_event_prefix,
    sanitize_public_incident,
    sanitize_public_match,
)

from .schema import (
    CODE_RESET,
    COMMENT_TARGET_TYPES,
    CONTEST_CANCELLED,
    CONTEST_DRAFT,
    CONTEST_FINISHED,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    CONTEST_REST,
    CONTEST_RUNNING,
    DEFAULT_RUNTIME_MODE,
    MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,
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
    LIKE_TARGET_TYPES,
    MATCH_DEBUG_MAX_BYTES_PER_MATCH,
    MATCH_DEBUG_MAX_BYTES_PER_SEAT,
    MATCH_DEBUG_MAX_ENTRIES_PER_MATCH,
    MATCH_DEBUG_MAX_ENTRIES_PER_SEAT,
    MATCH_DEBUG_MAX_ENTRY_BYTES,
    VALID_RUNTIME_MODES,
    require_supported_binary_metadata,
)
from .validation import validate_contest_times

DEFAULT_DB_PATH = "botzone.db"

_AUTO_MATCH_FAIR_BOOTSTRAP_VERSION = 1
_AUTO_MATCH_POLICY_VERSION = "owner-game-lane-v2"
_RATING_PROJECTION_POLICY_VERSION = "owner-neutral-v3"
_RATING_PROJECTION_LEGACY_VERSION = "legacy-unverified"
_AUTO_MATCH_PLATFORM_ABORT_REASONS = frozenset(
    {
        "platform_error",
        "version_unavailable",
        "orphan_after_restart",
        "orphan_pending_after_restart",
    }
)


def _daemon_incarnation_changed(previous: str, current: str) -> bool:
    """Return true only for a comparable Docker/host restart proof."""

    def parse(value: str) -> dict[str, str]:
        return {
            key: item
            for part in str(value or "").split(";")
            if ":" in part
            for key, item in [part.split(":", 1)]
        }

    before = parse(previous)
    after = parse(current)
    before_boot = before.get("boot", "unknown")
    after_boot = after.get("boot", "unknown")
    if (
        before_boot != "unknown"
        and after_boot != "unknown"
        and before_boot != after_boot
    ):
        return True
    before_daemon = before.get("daemon", "unknown")
    after_daemon = after.get("daemon", "unknown")
    return (
        before_boot == after_boot
        and before_daemon != "unknown"
        and after_daemon != "unknown"
        and before_daemon != after_daemon
    )


class AutoMatchFenceLost(RuntimeError):
    """The automatic-match worker no longer owns its durable dispatch epoch."""


@dataclass(frozen=True)
class _RatingProjectionMutationGuard:
    """Proof that the marker-settled projection was trusted before one write.

    The proof is process-local and is only valid inside the ``BEGIN IMMEDIATE``
    transaction that created it.  Requiring this explicit value at the advance
    site prevents a stale projection from being made current merely because its
    policy-version string happens to match.
    """

    trusted_before: bool


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _after_seconds(seconds: int | float) -> str:
    return (datetime.now() + timedelta(seconds=float(seconds))).isoformat(
        timespec="seconds"
    )


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


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
    try:
        winner = int(winner) if winner is not None else None
    except (TypeError, ValueError):
        winner = None
    if winner not in (0, 1):
        winner = None
    return {
        "type": "match_end",
        "winner": winner,
        "reason": canonical_public_completed_reason(match.get("reason")),
        "deltas": deltas,
    }


def _sanitize_public_replay(
    replay: dict | None,
    match: dict | None,
    *,
    human_viewer_seat: int | None = None,
) -> dict | None:
    """Return a replay whose terminal is derived from the authoritative row."""
    if replay is None and match is None:
        return None
    public = dict(replay or {})
    if match is not None:
        public.setdefault("match_id", match.get("id"))
    raw_events = public.get("events_json")
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
    sanitized = sanitize_public_event_prefix(
        events,
        redact_active_human=redact_active_human,
        human_viewer_seat=human_viewer_seat,
    )

    authoritative = sanitize_public_match(match)
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
    replay_events = _load_replay_bot_incident_events(m.pop("_replay_events_json", None))
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
                m[k] = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                m[k] = {}
        elif m.get(k) is None:
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
) -> tuple[list[dict], int]:
    """通用分页 helper：返回 (rows, total)。

    ``base_query`` 是不含 LIMIT/OFFSET 的 SELECT（不含 ORDER BY 时在 COUNT 里自动裁剪）。
    自动算 total + 加 LIMIT/OFFSET。page 从 1 开始。
    """
    page = max(1, int(page))
    per_page = max(1, min(200, int(per_page)))  # 上限 200 防滥用
    offset = (page - 1) * per_page
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


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_col(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = _table_cols(conn, table)
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


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
    match_config    TEXT    NOT NULL DEFAULT '{{}}',  -- 内部快照 JSON（Bot 版本/duplicate）；{{}} 经 .format 转义为字面空 JSON
    result          TEXT    NOT NULL DEFAULT '{{}}',  -- 对局结果详情 JSON（rounds_played/deltas/normalized_delta）
    human_user_id   INTEGER,
    human_seat      INTEGER,
    match_seed      INTEGER,  -- P4：对局确定性 seed（duplicate 复现/回放用）
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


def _matches_table(game_id: str) -> str:
    """game_id → 对应的物理表名（matches_holdem/gomoku/pencil）。"""
    gid = _registered_game_id(game_id)
    return f"matches_{gid}"


def _all_game_ids() -> frozenset[str]:
    """已注册的全部 game_id（从 games 注册表派生——单一真相，审计 P1 修复）。

    延迟 import 避免循环依赖（games 包加载时 store 已可用）。
    db.py 的跨游戏聚合（UNION ALL / COUNT 遍历）须用此函数，不得硬编码
    ("holdem","gomoku","pencil")——否则新增第 4 游戏会静默漏掉所有跨游戏统计。
    """
    from bzplat.backend.games import registry as _reg

    return _reg.all_ids()


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
            "SELECT match_id,game_id,bot_a_id,bot_b_id,rated,rating_reason,"
            "source,classified_at,settled_order FROM match_rating_policies"
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
            "game_id": str(row["game_id"]),
            "is_active": int(row["is_active"]),
            "format": str(row["format"]),
            "os": str(row["os"]),
            "arch": str(row["arch"]),
        }
        for row in conn.execute(
            "SELECT id,game_id,is_active,format,os,arch FROM bots "
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
        "source_settlement_count": len(settlements),
        "source_last_settled_order": max(orders, default=0),
        "sequence_next_order": next_order,
        "issues": sorted(set(issues)),
    }


def _rating_eligible_sql(alias: str) -> str:
    """SQL expression for the immutable rating policy of one match row.

    The database trigger deliberately derives ownership from canonical Bot rows
    instead of trusting JSON supplied by a caller.  ``match_config`` still
    freezes the same result for public explanation and post-processing.
    """
    return (
        f"({alias}.match_type NOT IN ('{TYPE_CONTEST}','{TYPE_HUMAN}') "
        f"AND {alias}.bot_a_id IS NOT NULL AND {alias}.bot_b_id IS NOT NULL "
        f"AND {alias}.bot_a_id<>{alias}.bot_b_id AND EXISTS ("
        "SELECT 1 FROM bots rating_a JOIN bots rating_b "
        f"ON rating_a.id={alias}.bot_a_id AND rating_b.id={alias}.bot_b_id "
        "WHERE rating_a.owner_id<>rating_b.owner_id))"
    )


def _install_rated_overlap_triggers(conn: sqlite3.Connection) -> None:
    """Enforce one rating-bearing lifecycle per Bot at the SQLite boundary."""
    for gid in sorted(_all_game_ids()):
        table = _matches_table(gid)
        new_rated = _rating_eligible_sql("NEW")
        existing_rated = _rating_eligible_sql("m")
        overlap = (
            f"SELECT 1 FROM {table} m WHERE m.id<>NEW.id "
            f"AND ({existing_rated}) "
            "AND (m.bot_a_id IN (NEW.bot_a_id,NEW.bot_b_id) "
            "OR m.bot_b_id IN (NEW.bot_a_id,NEW.bot_b_id)) "
            "AND (m.status IN ('pending','running') OR "
            "(m.status='completed' AND NOT EXISTS ("
            "SELECT 1 FROM match_rating_settlements settled "
            "WHERE settled.match_id=m.id))) LIMIT 1"
        )
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_rated_overlap_insert")
        conn.execute(
            f"CREATE TRIGGER trg_{table}_rated_overlap_insert "
            f"BEFORE INSERT ON {table} "
            "WHEN NEW.status IN ('pending','running') "
            f"AND ({new_rated}) AND EXISTS ({overlap}) "
            "BEGIN SELECT RAISE(ABORT, 'rated match lifecycle overlap'); END"
        )
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_rated_overlap_update")
        conn.execute(
            f"CREATE TRIGGER trg_{table}_rated_overlap_update "
            f"BEFORE UPDATE OF bot_a_id,bot_b_id,match_type,status ON {table} "
            "WHEN NEW.status IN ('pending','running') "
            f"AND ({new_rated}) AND EXISTS ({overlap}) "
            "BEGIN SELECT RAISE(ABORT, 'rated match lifecycle overlap'); END"
        )


def _install_rating_source_guards(conn: sqlite3.Connection) -> None:
    """Make every settled replay input immutable at the SQLite boundary."""
    conn.execute("DROP TRIGGER IF EXISTS trg_match_rating_policy_source_immutable")
    conn.execute(
        "CREATE TRIGGER trg_match_rating_policy_source_immutable "
        "BEFORE UPDATE OF match_id,game_id,bot_a_id,bot_b_id,rated,rating_reason,source,"
        "classified_at ON match_rating_policies WHEN "
        "OLD.match_id IS NOT NEW.match_id OR OLD.game_id IS NOT NEW.game_id OR "
        "OLD.bot_a_id IS NOT NEW.bot_a_id OR "
        "OLD.bot_b_id IS NOT NEW.bot_b_id OR OLD.rated IS NOT NEW.rated OR "
        "OLD.rating_reason IS NOT NEW.rating_reason OR OLD.source IS NOT NEW.source OR "
        "OLD.classified_at IS NOT NEW.classified_at BEGIN "
        "SELECT RAISE(ABORT,'rating policy source immutable'); END"
    )
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_match_rating_policy_settled_delete "
        "BEFORE DELETE ON match_rating_policies WHEN OLD.settled_order IS NOT NULL OR "
        "EXISTS(SELECT 1 FROM match_rating_settlements s WHERE s.match_id=OLD.match_id) "
        "BEGIN SELECT RAISE(ABORT,'settled rating policy immutable'); END"
    )
    for gid in sorted(_all_game_ids()):
        table = _matches_table(gid)
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_rating_source_update")
        conn.execute(
            f"CREATE TRIGGER trg_{table}_rating_source_update "
            f"BEFORE UPDATE OF id,winner,result,ended_at,status ON {table} WHEN "
            "EXISTS(SELECT 1 FROM match_rating_settlements s WHERE s.match_id=OLD.id) "
            "AND (OLD.id IS NOT NEW.id OR OLD.winner IS NOT NEW.winner OR "
            "OLD.result IS NOT NEW.result OR "
            "OLD.ended_at IS NOT NEW.ended_at OR OLD.status IS NOT NEW.status) "
            "BEGIN SELECT RAISE(ABORT,'settled match rating source immutable'); END"
        )
        conn.execute(f"DROP TRIGGER IF EXISTS trg_{table}_rating_source_delete")
        conn.execute(
            f"CREATE TRIGGER trg_{table}_rating_source_delete BEFORE DELETE ON {table} "
            "WHEN EXISTS(SELECT 1 FROM match_rating_settlements s "
            "WHERE s.match_id=OLD.id) BEGIN "
            "SELECT RAISE(ABORT,'settled match rating source immutable'); END"
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
            conn.execute(f"DROP TRIGGER IF EXISTS {name}")
            conn.execute(
                f"CREATE TRIGGER {name} AFTER {operation} ON {table} "
                f"BEGIN {bump} END"
            )

    conn.execute("DROP TRIGGER IF EXISTS trg_bots_projection_mutation_insert")
    conn.execute(
        "CREATE TRIGGER trg_bots_projection_mutation_insert AFTER INSERT ON bots "
        f"BEGIN {bump} END"
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_bots_projection_mutation_delete")
    conn.execute(
        "CREATE TRIGGER trg_bots_projection_mutation_delete AFTER DELETE ON bots "
        f"BEGIN {bump} END"
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_bots_projection_mutation_update")
    conn.execute(
        "CREATE TRIGGER trg_bots_projection_mutation_update "
        "AFTER UPDATE OF game_id,is_active,format,os,arch ON bots WHEN "
        "OLD.game_id IS NOT NEW.game_id OR OLD.is_active IS NOT NEW.is_active OR "
        "OLD.format IS NOT NEW.format OR OLD.os IS NOT NEW.os OR "
        f"OLD.arch IS NOT NEW.arch BEGIN {bump} END"
    )

    conn.execute(
        "DROP TRIGGER IF EXISTS trg_match_rating_policy_projection_mutation_order"
    )
    conn.execute(
        "CREATE TRIGGER trg_match_rating_policy_projection_mutation_order "
        "AFTER UPDATE OF settled_order ON match_rating_policies WHEN "
        "OLD.settled_order IS NOT NEW.settled_order "
        f"BEGIN {bump} END"
    )
    conn.execute(
        "DROP TRIGGER IF EXISTS trg_rating_settlement_sequence_projection_mutation"
    )
    conn.execute(
        "CREATE TRIGGER trg_rating_settlement_sequence_projection_mutation "
        "AFTER UPDATE OF next_order ON rating_settlement_sequence WHEN "
        "OLD.next_order IS NOT NEW.next_order "
        f"BEGIN {bump} END"
    )
    conn.execute(
        "DROP TRIGGER IF EXISTS trg_match_rating_settlement_projection_mutation_insert"
    )
    conn.execute(
        "CREATE TRIGGER trg_match_rating_settlement_projection_mutation_insert "
        "AFTER INSERT ON match_rating_settlements WHEN NEW.settled_order>0 "
        f"BEGIN {bump} END"
    )
    for game_id in sorted(_all_game_ids()):
        table = _matches_table(game_id)
        name = f"trg_{table}_projection_mutation_source"
        conn.execute(f"DROP TRIGGER IF EXISTS {name}")
        conn.execute(
            f"CREATE TRIGGER {name} AFTER UPDATE OF id,winner,result,ended_at,status "
            f"ON {table} WHEN EXISTS(SELECT 1 FROM match_rating_policies policy "
            "WHERE policy.match_id=OLD.id AND policy.settled_order IS NOT NULL) "
            "AND (OLD.id IS NOT NEW.id OR OLD.winner IS NOT NEW.winner OR "
            "OLD.result IS NOT NEW.result OR OLD.ended_at IS NOT NEW.ended_at OR "
            f"OLD.status IS NOT NEW.status) BEGIN {bump} END"
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
    for gid in game_ids:
        table = _matches_table(gid)
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
        conn.execute(
            f"CREATE TRIGGER IF NOT EXISTS trg_match_rating_policy_identity_{suffix} "
            f"BEFORE {action} ON match_rating_policies WHEN "
            f"NEW.game_id IS NULL OR NEW.game_id NOT IN ({valid_games}) OR "
            "(NEW.rated=1 AND (NEW.bot_a_id IS NULL OR NEW.bot_b_id IS NULL)) "
            "BEGIN SELECT RAISE(ABORT,'rating policy identity invalid'); END"
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
                "match_id,game_id,bot_a_id,bot_b_id,rated,rating_reason,source,"
                "classified_at) VALUES(?,?,?,?,?,?,'legacy_migration',?)",
                (
                    row["id"], gid, row["bot_a_id"], row["bot_b_id"],
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
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_match_rating_policy_order_immutable "
        "BEFORE UPDATE OF settled_order ON match_rating_policies "
        "WHEN OLD.settled_order IS NOT NULL AND OLD.settled_order IS NOT NEW.settled_order "
        "BEGIN SELECT RAISE(ABORT,'rating policy settled_order immutable'); END"
    )

    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_match_rating_settled_order "
        "ON match_rating_settlements(settled_order) "
        "WHERE settled_order IS NOT NULL AND settled_order>0"
    )
    sentinel = MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL.replace("'", "''")
    conn.execute("DROP TRIGGER IF EXISTS trg_match_rating_settlement_order_insert")
    conn.execute(
        "CREATE TRIGGER trg_match_rating_settlement_order_insert "
        "BEFORE INSERT ON match_rating_settlements "
        f"WHEN NEW.match_id<>'{sentinel}' AND ("
        "NEW.settled_order IS NULL OR NEW.settled_order<=0 OR "
        "NEW.settled_order<>(SELECT COALESCE(MAX(settled_order),0)+1 "
        "FROM match_rating_settlements WHERE settled_order>0)) BEGIN "
        "SELECT RAISE(ABORT,'rating settlement order must be next'); END"
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_match_rating_settlement_order_immutable")
    conn.execute(
        "CREATE TRIGGER trg_match_rating_settlement_order_immutable "
        "BEFORE UPDATE OF match_id,settled_at,settled_order "
        "ON match_rating_settlements WHEN OLD.match_id IS NOT NEW.match_id OR "
        "OLD.settled_at IS NOT NEW.settled_at OR "
        "OLD.settled_order IS NOT NEW.settled_order BEGIN "
        "SELECT RAISE(ABORT,'rating settlement source immutable'); END"
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_match_rating_settlement_delete_immutable")
    conn.execute(
        "CREATE TRIGGER trg_match_rating_settlement_delete_immutable "
        "BEFORE DELETE ON match_rating_settlements "
        "WHEN OLD.settled_order IS NOT NULL BEGIN "
        "SELECT RAISE(ABORT,'rating settlement source immutable'); END"
    )
    # Older releases advanced count/last from policy_version alone.  That
    # partially blessed an already-stale state before the application had
    # verified the projection/source/plan baseline.  The Store now advances all
    # five summary fields together behind an explicit pre-mutation guard.
    conn.execute("DROP TRIGGER IF EXISTS trg_match_rating_projection_advance")
    conn.execute(
        "CREATE TRIGGER IF NOT EXISTS trg_match_rating_projection_dirty_on_delete "
        "AFTER DELETE ON match_rating_settlements WHEN OLD.settled_order>0 BEGIN "
        "UPDATE rating_projection_state SET policy_version='projection-dirty',"
        "rebuilt_at=NULL WHERE singleton=1; END"
    )
    _install_rating_source_guards(conn)


def _migrate(conn: sqlite3.Connection) -> None:
    """为已有库补列；必要时重建 contests 以放宽 status CHECK。"""
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "contests" not in tables:
        return

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

    for col, decl in (
        ("game_id", "TEXT NOT NULL DEFAULT 'holdem'"),
        ("stages_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("current_stage_idx", "INTEGER NOT NULL DEFAULT 0"),
        ("template_id", "TEXT NOT NULL DEFAULT 'holdem_swiss_ko'"),
        ("rest_ends_at", "TEXT"),
        ("phase", "TEXT NOT NULL DEFAULT 'standalone'"),  # P2 预赛/决赛
        ("source_contest_id", "INTEGER"),
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
            "rest_ends_at",
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
            "phase", "source_contest_id",
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

    # Long-lived customer-demo contests are explicit immutable snapshots.  Add
    # this only after every legacy contests-table rebuild above, otherwise an old
    # CHECK migration could immediately drop the newly added column again.
    _add_col(conn, "contests", "showcase_key", "TEXT")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_contests_showcase_key "
        "ON contests(showcase_key) WHERE showcase_key IS NOT NULL"
    )

    if "contest_entries" in tables:
        for col, decl in (
            ("group_id", "TEXT NOT NULL DEFAULT ''"),
            ("seed", "INTEGER NOT NULL DEFAULT 0"),
            ("eliminated", "INTEGER NOT NULL DEFAULT 0"),
            ("dispatched_at", "TEXT"),
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
            "dispatched_at TEXT)",
            "contest_id, user_id, bot_id, registered_at, group_id, seed, eliminated, dispatched_at",
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
            f"SELECT id,status,winner,result FROM {_tbl}"
        ).fetchall():
            _raw_text = _match_row["result"]
            try:
                _raw_result = json.loads(_raw_text) if _raw_text else {}
            except (TypeError, ValueError):
                _raw_result = {}
            if not isinstance(_raw_result, dict):
                _raw_result = {}

            _rounds_candidate = _raw_result.get(
                "rounds_played", _raw_result.get("hands_played", 0)
            )
            if (
                isinstance(_rounds_candidate, bool)
                or not isinstance(_rounds_candidate, int)
                or _rounds_candidate < 0
            ):
                _rounds_candidate = 0

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
        _add_col(conn, "contest_pairings", "bot_a_version_id", "INTEGER")
        _add_col(conn, "contest_pairings", "bot_b_version_id", "INTEGER")
        _add_col(conn, "contest_pairings", "pairing_seed", "INTEGER")
        _add_col(conn, "contest_pairings", "published_at", "TEXT")
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

    # ── 自动排位 v2：每日配额下线，单一持久公平队列接管 ────────────────
    _add_col(conn, "auto_match_dispatcher", "lease_epoch", "INTEGER NOT NULL DEFAULT 0")
    _add_col(conn, "auto_match_queue", "dispatcher_epoch", "INTEGER")
    _add_col(conn, "auto_match_decisions", "claim_dispatcher_token", "TEXT")
    _add_col(conn, "auto_match_decisions", "claim_dispatcher_epoch", "INTEGER")
    for table in ("auto_match_queue", "auto_match_decisions"):
        _add_col(conn, table, "execution_scope", "TEXT")
        _add_col(conn, table, "execution_backend", "TEXT")
        _add_col(conn, table, "execution_state", "TEXT")
        _add_col(conn, table, "execution_launch_state", "TEXT")
        _add_col(conn, table, "execution_launch_token", "TEXT")
        _add_col(conn, table, "execution_daemon_incarnation", "TEXT")
        _add_col(conn, table, "cleanup_requested_at", "TEXT")
        _add_col(conn, table, "cleanup_ack_at", "TEXT")
        _add_col(conn, table, "cleanup_error", "TEXT NOT NULL DEFAULT ''")
    # A pre-upgrade active claim has no provable container label.  Preserve its
    # one-dispatched barrier as local/unconfirmable instead of releasing it.
    conn.execute(
        "UPDATE auto_match_queue SET execution_scope='legacy-' || id,"
        "execution_backend='local',execution_state='recovery_pending',"
        "execution_launch_state='creating',"
        "execution_launch_token='legacy-' || id,"
        "execution_daemon_incarnation='boot:unknown;daemon:unknown',"
        "cleanup_requested_at=COALESCE(cleanup_requested_at,?),"
        "cleanup_error='legacy active claim requires operator recovery' "
        "WHERE status='dispatched' AND execution_scope IS NULL",
        (_now(),),
    )
    conn.execute(
        "UPDATE auto_match_decisions SET execution_scope=(SELECT q.execution_scope "
        "FROM auto_match_queue q WHERE q.decision_id=auto_match_decisions.id),"
        "execution_backend='local',execution_state='recovery_pending',"
        "execution_launch_state='creating',"
        "execution_launch_token='legacy-' || id,"
        "execution_daemon_incarnation='boot:unknown;daemon:unknown',"
        "cleanup_requested_at=COALESCE(cleanup_requested_at,?),"
        "cleanup_error='legacy active claim requires operator recovery' "
        "WHERE lifecycle='dispatched' AND execution_scope IS NULL "
        "AND EXISTS(SELECT 1 FROM auto_match_queue q "
        "WHERE q.decision_id=auto_match_decisions.id AND q.status='dispatched')",
        (_now(),),
    )
    # Also fail closed if an operator briefly ran an earlier physical-fence
    # build which already persisted a scope but did not yet have launch tokens.
    conn.execute(
        "UPDATE auto_match_queue SET execution_launch_state='creating',"
        "execution_launch_token='legacy-launch-' || id,"
        "execution_daemon_incarnation='boot:unknown;daemon:unknown',"
        "execution_state='recovery_pending',"
        "cleanup_requested_at=COALESCE(cleanup_requested_at,?),"
        "cleanup_error='legacy launch acknowledgement is unprovable' "
        "WHERE status='dispatched' AND execution_launch_state IS NULL",
        (_now(),),
    )
    conn.execute(
        "UPDATE auto_match_decisions SET "
        "execution_launch_state=(SELECT q.execution_launch_state FROM auto_match_queue q "
        "WHERE q.decision_id=auto_match_decisions.id),"
        "execution_launch_token=(SELECT q.execution_launch_token FROM auto_match_queue q "
        "WHERE q.decision_id=auto_match_decisions.id),"
        "execution_daemon_incarnation=(SELECT q.execution_daemon_incarnation "
        "FROM auto_match_queue q WHERE q.decision_id=auto_match_decisions.id),"
        "execution_state='recovery_pending',"
        "cleanup_requested_at=COALESCE(cleanup_requested_at,?),"
        "cleanup_error='legacy launch acknowledgement is unprovable' "
        "WHERE lifecycle='dispatched' AND execution_launch_state IS NULL "
        "AND EXISTS(SELECT 1 FROM auto_match_queue q "
        "WHERE q.decision_id=auto_match_decisions.id AND q.status='dispatched')",
        (_now(),),
    )
    conn.execute(
        "UPDATE auto_match_queue SET "
        "execution_daemon_incarnation='boot:unknown;daemon:unknown',"
        "execution_state='recovery_pending',"
        "cleanup_requested_at=COALESCE(cleanup_requested_at,?),"
        "cleanup_error='legacy daemon incarnation is unprovable' "
        "WHERE status='dispatched' AND execution_launch_state<>'unstarted' "
        "AND execution_daemon_incarnation IS NULL",
        (_now(),),
    )
    conn.execute(
        "UPDATE auto_match_decisions SET "
        "execution_daemon_incarnation=(SELECT q.execution_daemon_incarnation "
        "FROM auto_match_queue q WHERE q.decision_id=auto_match_decisions.id),"
        "execution_state='recovery_pending',"
        "cleanup_requested_at=COALESCE(cleanup_requested_at,?),"
        "cleanup_error='legacy daemon incarnation is unprovable' "
        "WHERE lifecycle='dispatched' AND execution_launch_state<>'unstarted' "
        "AND execution_daemon_incarnation IS NULL",
        (_now(),),
    )
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
    conn.execute("DROP TRIGGER IF EXISTS trg_auto_match_queue_fence_insert")
    conn.execute(
        "CREATE TRIGGER trg_auto_match_queue_fence_insert "
        "BEFORE INSERT ON auto_match_queue WHEN "
        "(NEW.status='queued' AND (NEW.match_id IS NOT NULL OR "
        "NEW.dispatcher_token IS NOT NULL OR NEW.dispatcher_epoch IS NOT NULL OR "
        "NEW.dispatched_at IS NOT NULL OR NEW.execution_scope IS NOT NULL OR "
        "NEW.execution_backend IS NOT NULL OR NEW.execution_state IS NOT NULL OR "
        "NEW.execution_launch_state IS NOT NULL OR "
        "NEW.execution_launch_token IS NOT NULL OR "
        "NEW.execution_daemon_incarnation IS NOT NULL OR "
        "NEW.cleanup_requested_at IS NOT NULL OR NEW.cleanup_ack_at IS NOT NULL OR "
        "NEW.cleanup_error<>'')) OR "
        "(NEW.status='dispatched' AND (NEW.match_id IS NULL OR "
        "NEW.dispatcher_token IS NULL OR NEW.dispatcher_epoch IS NULL OR "
        "NEW.dispatcher_epoch<=0 OR NEW.dispatched_at IS NULL OR "
        "NEW.execution_scope IS NULL OR NEW.execution_backend IS NULL OR "
        "NEW.execution_state IS NULL OR NEW.execution_launch_state IS NULL OR "
        "(NEW.execution_launch_state='unstarted' AND (NEW.execution_launch_token IS NOT NULL OR "
        "NEW.execution_daemon_incarnation IS NOT NULL)) OR "
        "(NEW.execution_launch_state<>'unstarted' AND (NEW.execution_launch_token IS NULL OR "
        "NEW.execution_daemon_incarnation IS NULL)))) BEGIN "
        "SELECT RAISE(ABORT,'invalid auto-match queue fence'); END"
    )
    conn.execute("DROP TRIGGER IF EXISTS trg_auto_match_queue_fence_update")
    conn.execute(
        "CREATE TRIGGER trg_auto_match_queue_fence_update "
        "BEFORE UPDATE OF status,match_id,dispatcher_token,dispatcher_epoch,dispatched_at,"
        "execution_scope,execution_backend,execution_state,cleanup_requested_at,"
        "execution_launch_state,execution_launch_token,execution_daemon_incarnation,"
        "cleanup_ack_at,cleanup_error "
        "ON auto_match_queue WHEN "
        "(NEW.status='queued' AND (NEW.match_id IS NOT NULL OR "
        "NEW.dispatcher_token IS NOT NULL OR NEW.dispatcher_epoch IS NOT NULL OR "
        "NEW.dispatched_at IS NOT NULL OR NEW.execution_scope IS NOT NULL OR "
        "NEW.execution_backend IS NOT NULL OR NEW.execution_state IS NOT NULL OR "
        "NEW.execution_launch_state IS NOT NULL OR "
        "NEW.execution_launch_token IS NOT NULL OR "
        "NEW.execution_daemon_incarnation IS NOT NULL OR "
        "NEW.cleanup_requested_at IS NOT NULL OR NEW.cleanup_ack_at IS NOT NULL OR "
        "NEW.cleanup_error<>'')) OR "
        "(NEW.status='dispatched' AND (NEW.match_id IS NULL OR "
        "NEW.dispatcher_token IS NULL OR NEW.dispatcher_epoch IS NULL OR "
        "NEW.dispatcher_epoch<=0 OR NEW.dispatched_at IS NULL OR "
        "NEW.execution_scope IS NULL OR NEW.execution_backend IS NULL OR "
        "NEW.execution_state IS NULL OR NEW.execution_launch_state IS NULL OR "
        "(NEW.execution_launch_state='unstarted' AND (NEW.execution_launch_token IS NOT NULL OR "
        "NEW.execution_daemon_incarnation IS NOT NULL)) OR "
        "(NEW.execution_launch_state<>'unstarted' AND (NEW.execution_launch_token IS NULL OR "
        "NEW.execution_daemon_incarnation IS NULL)))) BEGIN "
        "SELECT RAISE(ABORT,'invalid auto-match queue fence'); END"
    )
    # 公平计数只从历史 completed system ladder 一次性引导；此后仅由 queue
    # terminal transaction 更新，绝不读取可被前台挑战影响的 ratings/pair_stats。
    _bootstrap_auto_match_fairness(conn)
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
        with self._tx() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)
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
                "SELECT COUNT(*) FROM bots WHERE owner_id=?", (uid,)
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
        """按对局 ID 或 bot 名模糊搜索已完成对局。"""
        ql = f"%{q.lower()}%" if q else "%"
        with self._tx() as c:
            sel = (
                "m.id, m.game_id, m.status, m.winner, m.reason, "
                "m.match_type, m.created_at, "
                "ba.name AS bot_a_name, bb.name AS bot_b_name, "
                "ba.display_name AS bot_a_display, bb.display_name AS bot_b_display"
            )
            join_bots = (
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id"
            )
            where_sql = (
                " WHERE m.status='completed' "
                "AND (LOWER(m.id) LIKE ? OR LOWER(ba.name) LIKE ? OR LOWER(bb.name) LIKE ? "
                "OR LOWER(ba.display_name) LIKE ? OR LOWER(bb.display_name) LIKE ?)"
            )
            params: list[Any] = [ql, ql, ql, ql, ql]
            if game_id:
                where_sql += " AND m.game_id=?"
                params.append(game_id)
            lim = max(1, min(limit, 50))

            if game_id:
                tbl = _matches_table(game_id)
                sql = f"SELECT {sel} FROM {tbl} m {join_bots}{where_sql} ORDER BY m.created_at DESC LIMIT ?"
                return [_row(r) for r in c.execute(sql, params + [lim])]

            # 跨游戏 UNION ALL
            subselects = []
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                subselects.append(f"SELECT {sel} FROM {tbl} m {join_bots}{where_sql}")
            union = " UNION ALL ".join(subselects)
            sql = f"SELECT * FROM ({union}) ORDER BY created_at DESC LIMIT ?"
            # 子查询数 = 已注册游戏数，WHERE 参数须按此倍数复制（每个子查询一份）。
            # 不得硬编码 * 3——新增第 4 游戏会触发 Incorrect number of bindings。
            return [_row(r) for r in c.execute(sql, params * len(_all_game_ids()) + [lim])]

    def get_user_by_email(self, email: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            )

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
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            if sets:
                vals.append(user_id)
                c.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)
            return _row(
                c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            )

    def list_users(
        self, *, role: str | None = None, active_only: bool = False,
        q: str | None = None, real_name: bool | None = None,
        page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        with self._tx() as c:
            sql = "SELECT * FROM users WHERE 1=1"
            params: list[Any] = []
            if role:
                sql += " AND role=?"
                params.append(role)
            if active_only:
                sql += " AND is_active=1"
            if q:
                sql += " AND (LOWER(username) LIKE ? OR LOWER(email) LIKE ?)"
                like = f"%{q.strip().lower()}%"
                params.extend((like, like))
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
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
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

        邮箱验证码与管理员重置 token 二选一。返回 ``ok``、``invalid`` 或
        ``expired``；``invalid`` 同时涵盖不存在、已使用和最终 CAS 竞争失败。
        凭据 CAS、密码更新与 session 删除共享同一个 ``BEGIN IMMEDIATE``
        事务，后两步异常时凭据消费也会随事务回滚。
        """
        email_selected = email_code_id is not None or email_code is not None
        token_selected = reset_token is not None
        if email_selected == token_selected:
            raise ValueError("邮箱验证码和重置 token 必须且只能提供一种")
        if email_selected and (email_code_id is None or email_code is None):
            raise ValueError("邮箱验证码 id 与 code 必须同时提供")

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            used_at = _now()
            checked_at = datetime.now()
            expiry_cutoff = checked_at.isoformat(timespec="microseconds")
            if email_selected:
                credential = c.execute(
                    "SELECT user_id, expires_at, used_at FROM email_codes "
                    "WHERE id=? AND user_id=? AND purpose=? AND code=?",
                    (email_code_id, user_id, CODE_RESET, email_code),
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
                    "UPDATE email_codes SET used_at=? "
                    "WHERE id=? AND user_id=? AND purpose=? AND code=? "
                    "AND used_at IS NULL AND expires_at>=?",
                    (
                        used_at,
                        email_code_id,
                        user_id,
                        CODE_RESET,
                        email_code,
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
            projection_guard = self._rating_projection_mutation_guard_tx(c)
            cur = c.execute(
                "INSERT INTO bots(owner_id, name, display_name, description, "
                "os, arch, format, binary_path, is_builtin, is_active, game_id, runtime_mode, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            # Active/inactive is the ordinary reversible leaderboard visibility
            # mutation.  Identity/game changes remain fail-closed and can only be
            # reconciled by the offline rebuild workflow.
            if "is_active" in fields and not {
                "game_id", "format", "os", "arch"
            }.intersection(fields):
                c.execute("BEGIN IMMEDIATE")
                projection_guard = self._rating_projection_mutation_guard_tx(c)
            if sets:
                if "updated_at" not in fields:
                    sets.append("updated_at=?")
                    vals.append(_now())
                vals.append(bot_id)
                c.execute(f"UPDATE bots SET {','.join(sets)} WHERE id=?", vals)
            if projection_guard is not None:
                self._advance_rating_projection_state_tx(c, projection_guard)
            return _row(
                c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            )

    def delete_bot(self, bot_id: int) -> bool:
        # 注意：此处不做「活跃引用」业务校验——那是 admin_delete_bot 端点的职责（业务规则）。
        # 本方法保持纯 store 行为：直接删，FK ON DELETE SET NULL（matches，保历史）/ CASCADE
        # （contest_pairings）由 DB 处理。管理端须改调 delete_bot_if_safe() 原子判断。
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM bots WHERE id=?", (bot_id,)).fetchone():
                return False
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
                "SELECT id,game_id,is_active,current_version FROM bots WHERE id=?",
                (bot_id,),
            ).fetchone()
            if (
                bot is None
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
                    "SELECT 1 FROM auto_match_queue "
                    "WHERE bot_a_id=? OR bot_b_id=? LIMIT 1",
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
        """在一个写事务内检查活跃引用并硬删 Bot，消除 check→delete 竞态。"""
        active_contest_statuses = (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
            CONTEST_REST,
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT id FROM bots WHERE id=?", (bot_id,)).fetchone():
                return {"found": False, "deleted": False, "references": {}}

            match_count = 0
            for gid in _all_game_ids():
                table = _matches_table(gid)
                rated = _rating_eligible_sql("m")
                row = c.execute(
                    f"SELECT COUNT(*) AS n FROM {table} m "
                    "WHERE (m.bot_a_id=? OR m.bot_b_id=?) AND ("
                    "m.status IN (?,?) OR (m.status=? AND "
                    f"({rated}) AND NOT EXISTS ("
                    "SELECT 1 FROM match_rating_settlements settled "
                    "WHERE settled.match_id=m.id)))",
                    (
                        bot_id,
                        bot_id,
                        STATUS_PENDING,
                        STATUS_RUNNING,
                        STATUS_COMPLETED,
                    ),
                ).fetchone()
                match_count += int(row["n"] if row else 0)
            queued_row = c.execute(
                "SELECT COUNT(*) AS n FROM auto_match_queue "
                "WHERE bot_a_id=? OR bot_b_id=?",
                (bot_id, bot_id),
            ).fetchone()
            match_count += int(queued_row["n"] if queued_row else 0)

            status_marks = ",".join("?" for _ in active_contest_statuses)
            pairing_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_pairings pairing "
                "JOIN contests contest ON contest.id=pairing.contest_id "
                "WHERE (pairing.bot_a_id=? OR pairing.bot_b_id=?) "
                f"AND contest.status IN ({status_marks})",
                (bot_id, bot_id, *active_contest_statuses),
            ).fetchone()
            entry_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_entries entry "
                "JOIN contests contest ON contest.id=entry.contest_id "
                "WHERE entry.bot_id=? "
                f"AND contest.status IN ({status_marks})",
                (bot_id, *active_contest_statuses),
            ).fetchone()
            refs = {
                "matches": match_count,
                "pairings": int(pairing_row["n"] if pairing_row else 0)
                + int(entry_row["n"] if entry_row else 0),
            }
            if any(refs.values()):
                return {"found": True, "deleted": False, "references": refs}
            _delete_social_target(c, "bot", bot_id)
            deleted = c.execute("DELETE FROM bots WHERE id=?", (bot_id,)).rowcount > 0
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
                "SELECT COUNT(*) AS n FROM auto_match_queue "
                "WHERE bot_a_id=? OR bot_b_id=?",
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
            if runnable_only:
                sql += " AND format=? AND os=? AND arch=?"
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
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
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
        version: int | None = None,
    ) -> dict:
        require_supported_binary_metadata(format, os, arch)
        if runtime_mode is None:
            # 沿用 bot 当前的运行模式（回滚/补传时不强制改模式）
            runtime_mode = self.get_bot(bot_id) or {}
            runtime_mode = runtime_mode.get("runtime_mode") or DEFAULT_RUNTIME_MODE
        if runtime_mode not in VALID_RUNTIME_MODES:
            raise ValueError(f"非法 runtime_mode: {runtime_mode}")
        with self._tx() as c:
            if version is None:
                row = c.execute(
                    "SELECT MAX(version) AS mv FROM bot_versions WHERE bot_id=?",
                    (bot_id,),
                ).fetchone()
                version = (row["mv"] or 0) + 1
            cur = c.execute(
                "INSERT INTO bot_versions(bot_id, version, binary_path, "
                "upload_note, checksum, size_bytes, os, arch, format, runtime_mode, "
                "uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                    _now(),
                ),
            )
            vid = cur.lastrowid
            c.execute(
                "UPDATE bots SET current_version=?, binary_path=?, os=?, arch=?, "
                "format=?, runtime_mode=?, updated_at=? WHERE id=?",
                (version, binary_path, os, arch, format, runtime_mode, _now(), bot_id),
            )
            return _row(
                c.execute("SELECT * FROM bot_versions WHERE id=?", (vid,)).fetchone()
            )

    def delete_bot_version(self, bot_id: int, version: int) -> bool:
        """删除指定版本；若删的是当前版本，回退到 max(version)（含 runtime_mode）。

        删非当前版本时**不动 bots 镜像**——否则会覆盖用户主动回滚到的旧版本状态。
        """
        with self._tx() as c:
            # 先读当前版本，判定删的是否是当前版本
            cur_bot = c.execute(
                "SELECT current_version FROM bots WHERE id=?", (bot_id,)
            ).fetchone()
            is_current = cur_bot and cur_bot["current_version"] == version

            version_row = c.execute(
                "SELECT id FROM bot_versions WHERE bot_id=? AND version=?",
                (bot_id, version),
            ).fetchone()
            if version_row and c.execute(
                "SELECT 1 FROM auto_match_queue WHERE bot_a_version_id=? "
                "OR bot_b_version_id=? LIMIT 1",
                (version_row["id"], version_row["id"]),
            ).fetchone():
                raise ValueError("该版本已被冻结到自动排位队列，暂不可删除")

            cur = c.execute(
                "DELETE FROM bot_versions WHERE bot_id=? AND version=?",
                (bot_id, version),
            )
            if cur.rowcount == 0:
                return False
            # 仅当删的是当前版本，才回退镜像到剩余最新版本
            if is_current:
                row = c.execute(
                    "SELECT MAX(version) AS mv, binary_path, os, arch, format, runtime_mode "
                    "FROM bot_versions WHERE bot_id=?",
                    (bot_id,),
                ).fetchone()
                if row and row["mv"]:
                    c.execute(
                        "UPDATE bots SET current_version=?, binary_path=?, os=?, arch=?, "
                        "format=?, runtime_mode=?, updated_at=? WHERE id=?",
                        (row["mv"], row["binary_path"], row["os"], row["arch"],
                         row["format"], row["runtime_mode"], _now(), bot_id),
                    )
            return True

    def set_current_version(self, bot_id: int, version: int) -> dict | None:
        """回滚到指定版本（不删除其他版本）：把 bots 镜像切到该版本的
        binary_path/os/arch/format/runtime_mode，current_version=version。

        用于 MyBots「回滚到此版本」。版本不存在返回 None。
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT version, binary_path, os, arch, format, runtime_mode "
                "FROM bot_versions WHERE bot_id=? AND version=?",
                (bot_id, version),
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE bots SET current_version=?, binary_path=?, os=?, arch=?, "
                "format=?, runtime_mode=?, updated_at=? WHERE id=?",
                (row["version"], row["binary_path"], row["os"], row["arch"],
                 row["format"], row["runtime_mode"], _now(), bot_id),
            )
            return _row(c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone())

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
        """聚合 Bot 详情：bot 信息 + owner + rating + 胜率 + 段位。

        不含对局历史与对手战绩（单独端点，避免单次返回过大）。
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT b.*, u.username AS owner_name, "
                "u.display_name AS owner_display, "
                "r.rating, r.rd, r.vol, r.wins, r.losses, r.draws, "
                "r.matches_played, r.last_played_at AS rated_at "
                "FROM bots b "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "LEFT JOIN ratings r ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE b.id=?",
                (bot_id,),
            ).fetchone()
            d = _row(row)
            if d is not None:
                from bzplat.backend.games import registry as _game_registry
                t = _game_registry.tier_for(
                    _registered_game_id(d.get("game_id")), d.get("rating")
                )
                d["tier_level"] = t.level
                d["tier_key"] = t.key
                d["tier_name"] = t.name
            return d

    def bot_opponents_stats(
        self, bot_id: int, *, limit: int = 20
    ) -> list[dict]:
        """返回该 Bot 对各对手的战绩（按交手次数倒序），从 pair_stats 读。

        每行含 opponent_id/opponent_name/opponent_display/game_id/
        wins/losses/draws/samples/last_played_at（wins 从 bot_id 视角）。
        """
        with self._tx() as c:
            # bot 可能在 bot_a 或 bot_b 位
            rows = c.execute(
                "SELECT ps.bot_a_id, ps.bot_b_id, ps.a_wins, ps.a_losses, "
                "ps.draws, (ps.a_wins+ps.a_losses+ps.draws) AS samples, "
                "ps.last_played_at "
                "FROM pair_stats ps "
                "WHERE ps.bot_a_id=? OR ps.bot_b_id=? "
                "ORDER BY samples DESC, ps.last_played_at DESC, "
                "ps.bot_a_id, ps.bot_b_id LIMIT ?",
                (bot_id, bot_id, max(1, min(limit, 100))),
            ).fetchall()
            out: list[dict] = []
            for r in rows:
                d = _row(r)
                a_id, b_id = d["bot_a_id"], d["bot_b_id"]
                opp_id = b_id if a_id == bot_id else a_id
                # 视角还原：若 bot 是 a，wins=a_wins；若 bot 是 b，wins=a_losses
                if bot_id == a_id:
                    wins, losses = d["a_wins"], d["a_losses"]
                else:
                    wins, losses = d["a_losses"], d["a_wins"]
                opp = c.execute(
                    "SELECT name, display_name, game_id FROM bots WHERE id=?",
                    (opp_id,),
                ).fetchone()
                out.append({
                    "opponent_id": opp_id,
                    "opponent_name": opp["name"] if opp else f"#{opp_id}",
                    "opponent_display": opp["display_name"] if opp else "",
                    "game_id": opp["game_id"] if opp else "",
                    "wins": wins,
                    "losses": losses,
                    "draws": d["draws"],
                    "samples": d["samples"],
                    "last_played_at": d["last_played_at"],
                })
            return out

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
        Contest, human, self-play and same-owner comparisons are neutral and do
        not block.  Callers hold the same SQLite transaction used for selection
        or creation; per-game triggers enforce the same rule for every writer.
        """
        gids = (game_id,) if game_id is not None else tuple(_all_game_ids())
        for gid in gids:
            tbl = _matches_table(gid)
            rated = _rating_eligible_sql("m")
            row = c.execute(
                f"SELECT 1 FROM {tbl} m WHERE ({rated}) "
                "AND (m.bot_a_id=? OR m.bot_b_id=?) AND ("
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
    def _auto_queue_identity_tx(
        c: sqlite3.Connection,
        row: sqlite3.Row | dict,
    ) -> dict | None:
        """Validate one frozen queue identity against current executable state."""
        q = dict(row)
        identity = c.execute(
            "SELECT a.id AS a_id, a.owner_id AS a_owner_id, a.game_id AS a_game_id, "
            "a.is_active AS a_active, a.is_builtin AS a_builtin, "
            "a.format AS a_format, a.os AS a_os, a.arch AS a_arch, "
            "ua.is_active AS a_owner_active, "
            "b.id AS b_id, b.owner_id AS b_owner_id, b.game_id AS b_game_id, "
            "b.is_active AS b_active, b.is_builtin AS b_builtin, "
            "b.format AS b_format, b.os AS b_os, b.arch AS b_arch, "
            "ub.is_active AS b_owner_active, "
            "va.id AS a_version_id, va.binary_path AS a_version_path, "
            "va.format AS a_version_format, va.os AS a_version_os, "
            "va.arch AS a_version_arch, va.runtime_mode AS a_version_runtime, "
            "vb.id AS b_version_id, vb.binary_path AS b_version_path, "
            "vb.format AS b_version_format, vb.os AS b_version_os, "
            "vb.arch AS b_version_arch, vb.runtime_mode AS b_version_runtime, "
            "d.lifecycle AS decision_lifecycle,d.game_id AS decision_game_id,"
            "d.bot_a_id AS decision_bot_a_id,d.bot_b_id AS decision_bot_b_id,"
            "d.bot_a_version_id AS decision_version_a_id,"
            "d.bot_b_version_id AS decision_version_b_id,"
            "ra.game_id AS a_rating_game, rb.game_id AS b_rating_game "
            "FROM bots a JOIN users ua ON ua.id=a.owner_id "
            "JOIN bots b ON b.id=? JOIN users ub ON ub.id=b.owner_id "
            "JOIN bot_versions va ON va.id=? AND va.bot_id=a.id "
            "JOIN bot_versions vb ON vb.id=? AND vb.bot_id=b.id "
            "JOIN auto_match_decisions d ON d.id=? "
            "JOIN ratings ra ON ra.bot_id=a.id AND ra.game_id=a.game_id "
            "JOIN ratings rb ON rb.bot_id=b.id AND rb.game_id=b.game_id "
            "WHERE a.id=?",
            (
                int(q["bot_b_id"]),
                int(q["bot_a_version_id"]),
                int(q["bot_b_version_id"]),
                int(q["decision_id"]),
                int(q["bot_a_id"]),
            ),
        ).fetchone()
        if identity is None:
            return None
        data = dict(identity)
        gid = str(q.get("game_id") or "")
        if (
            int(data["a_id"]) == int(data["b_id"])
            or int(data["a_owner_id"]) == int(data["b_owner_id"])
            or data["a_game_id"] != gid
            or data["b_game_id"] != gid
            or data["a_rating_game"] != gid
            or data["b_rating_game"] != gid
            or not int(data["a_active"])
            or not int(data["b_active"])
            or not int(data["a_owner_active"])
            or not int(data["b_owner_active"])
            or int(data["a_builtin"])
            or int(data["b_builtin"])
            or data["decision_game_id"] != gid
            or int(data["decision_bot_a_id"]) != int(q["bot_a_id"])
            or int(data["decision_bot_b_id"]) != int(q["bot_b_id"])
            or int(data["decision_version_a_id"]) != int(q["bot_a_version_id"])
            or int(data["decision_version_b_id"]) != int(q["bot_b_version_id"])
            or data["decision_lifecycle"] != (
                "queued" if q.get("status") == "queued" else "dispatched"
            )
        ):
            return None
        for prefix in ("a", "b"):
            if (
                data[f"{prefix}_format"] != SUPPORTED_BINARY_FORMAT
                or data[f"{prefix}_os"] != SUPPORTED_BINARY_OS
                or data[f"{prefix}_arch"] != SUPPORTED_BINARY_ARCH
                or data[f"{prefix}_version_format"] != SUPPORTED_BINARY_FORMAT
                or data[f"{prefix}_version_os"] != SUPPORTED_BINARY_OS
                or data[f"{prefix}_version_arch"] != SUPPORTED_BINARY_ARCH
                or data[f"{prefix}_version_runtime"] not in VALID_RUNTIME_MODES
                or not str(data[f"{prefix}_version_path"] or "")
            ):
                return None
        return data

    @staticmethod
    def _auto_dispatcher_owned_tx(
        c: sqlite3.Connection,
        dispatcher_token: str,
        dispatcher_epoch: int,
    ) -> bool:
        now = _now()
        row = c.execute(
            "SELECT owner_token,lease_epoch,lease_until FROM auto_match_dispatcher "
            "WHERE singleton=1"
        ).fetchone()
        return bool(
            row
            and str(row["owner_token"] or "") == dispatcher_token
            and int(row["lease_epoch"] or 0) == int(dispatcher_epoch)
            and str(row["lease_until"] or "") > now
        )

    @classmethod
    def _auto_match_fence_owned_tx(
        cls,
        c: sqlite3.Connection,
        match_id: str,
        dispatcher_token: str,
        dispatcher_epoch: int,
        *,
        require_claim_fence: bool,
    ) -> sqlite3.Row | None:
        """Return the dispatched queue row only for the live fenced owner."""
        if not cls._auto_dispatcher_owned_tx(
            c, dispatcher_token, dispatcher_epoch
        ):
            return None
        row = c.execute(
            "SELECT q.*,d.claim_dispatcher_token,d.claim_dispatcher_epoch "
            "FROM auto_match_queue q JOIN auto_match_decisions d ON d.id=q.decision_id "
            "WHERE q.status='dispatched' AND q.match_id=? "
            "AND q.dispatcher_token=? AND q.dispatcher_epoch=?",
            (match_id, dispatcher_token, int(dispatcher_epoch)),
        ).fetchone()
        if row is None:
            return None
        if require_claim_fence and (
            str(row["claim_dispatcher_token"] or "") != dispatcher_token
            or int(row["claim_dispatcher_epoch"] or 0) != int(dispatcher_epoch)
        ):
            return None
        return row

    @classmethod
    def _require_auto_match_fence_tx(
        cls,
        c: sqlite3.Connection,
        match_id: str,
        dispatcher_token: str,
        dispatcher_epoch: int,
        *,
        require_claim_fence: bool,
    ) -> sqlite3.Row:
        row = cls._auto_match_fence_owned_tx(
            c,
            match_id,
            dispatcher_token,
            dispatcher_epoch,
            require_claim_fence=require_claim_fence,
        )
        if row is None:
            raise AutoMatchFenceLost(
                f"auto-match dispatch fence lost for match {match_id}"
            )
        if require_claim_fence:
            indexed = c.execute(
                "SELECT game_id FROM matches_index WHERE id=?", (match_id,)
            ).fetchone()
            if indexed is None:
                raise AutoMatchFenceLost(
                    f"auto-match match disappeared for fence {match_id}"
                )
            table = _matches_table(indexed["game_id"])
            match = c.execute(
                f"SELECT match_config FROM {table} WHERE id=?", (match_id,)
            ).fetchone()
            if match is None:
                raise AutoMatchFenceLost(
                    f"auto-match match disappeared for fence {match_id}"
                )
            try:
                config = json.loads(match["match_config"] or "{}")
            except (TypeError, ValueError):
                config = {}
            if (
                str(config.get("_auto_match_claim_token") or "")
                != dispatcher_token
                or int(config.get("_auto_match_claim_epoch") or 0)
                != int(dispatcher_epoch)
            ):
                raise AutoMatchFenceLost(
                    f"auto-match frozen claim fence mismatch for match {match_id}"
                )
        return row

    def assert_auto_match_claim_fence(
        self, match_id: str, dispatcher_token: str, dispatcher_epoch: int
    ) -> None:
        """Fail closed unless ``match_id`` is owned by the live claimed epoch."""
        with self._tx() as c:
            c.execute("BEGIN")
            self._require_auto_match_fence_tx(
                c,
                match_id,
                dispatcher_token,
                dispatcher_epoch,
                require_claim_fence=True,
            )

    @property
    def auto_match_execution_launch_lock_path(self) -> str:
        """Stable cross-process lock shared by every auto execution for this DB."""
        digest = hashlib.sha256(os.path.abspath(self.path).encode("utf-8")).hexdigest()
        return f"/tmp/botbattle-auto-launch-{digest[:24]}.lock"

    def assert_auto_match_execution_fence(
        self,
        match_id: str,
        dispatcher_token: str,
        dispatcher_epoch: int,
        execution_scope: str,
    ) -> None:
        """Fence every physical spawn/decision against takeover recovery."""
        with self._tx() as c:
            c.execute("BEGIN")
            row = self._require_auto_match_fence_tx(
                c,
                match_id,
                dispatcher_token,
                dispatcher_epoch,
                require_claim_fence=True,
            )
            if (
                str(row["execution_scope"] or "") != execution_scope
                or row["execution_state"] not in ("claimed", "running")
            ):
                raise AutoMatchFenceLost(
                    f"auto-match execution scope lost for match {match_id}"
                )

    def mark_auto_match_execution_recovery_pending(
        self,
        match_id: str,
        dispatcher_token: str,
        dispatcher_epoch: int,
        execution_scope: str,
        reason: str,
    ) -> None:
        """Fail closed when the original worker cannot prove sandbox cleanup."""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            row = self._require_auto_match_fence_tx(
                c,
                match_id,
                dispatcher_token,
                dispatcher_epoch,
                require_claim_fence=True,
            )
            if str(row["execution_scope"] or "") != execution_scope:
                raise AutoMatchFenceLost(
                    f"auto-match execution scope lost for match {match_id}"
                )
            message = str(reason or "execution cleanup not confirmed")[:500]
            requested_at = _now()
            if str(row["execution_state"] or "") == "recovery_pending":
                c.execute(
                    "UPDATE auto_match_queue SET cleanup_error=? WHERE id=?",
                    (message, int(row["id"])),
                )
                c.execute(
                    "UPDATE auto_match_decisions SET cleanup_error=? WHERE id=? "
                    "AND lifecycle='dispatched'",
                    (message, int(row["decision_id"])),
                )
                return
            changed = c.execute(
                "UPDATE auto_match_queue SET execution_state='recovery_pending',"
                "cleanup_requested_at=?,cleanup_ack_at=NULL,cleanup_error=? "
                "WHERE id=? AND execution_state IN ('claimed','running')",
                (requested_at, message, int(row["id"])),
            )
            if changed.rowcount != 1:
                raise AutoMatchFenceLost(
                    f"auto-match execution recovery CAS lost for match {match_id}"
                )
            c.execute(
                "UPDATE auto_match_decisions SET execution_state='recovery_pending',"
                "cleanup_requested_at=?,cleanup_ack_at=NULL,cleanup_error=? "
                "WHERE id=? AND lifecycle='dispatched'",
                (requested_at, message, int(row["decision_id"])),
            )

    def mark_auto_match_execution_launch_state(
        self,
        match_id: str,
        dispatcher_token: str,
        dispatcher_epoch: int,
        execution_scope: str,
        launch_state: str,
        launch_token: str | None,
        daemon_incarnation: str | None = None,
    ) -> None:
        """Persist daemon-ack launch progress under the current execution fence."""
        transitions = {
            "creating": {"unstarted", "started"},
            "created": {"creating"},
            "started": {"creating", "created"},  # local skips Docker-created
        }
        if launch_state not in transitions:
            raise ValueError("invalid auto execution launch state")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            row = self._require_auto_match_fence_tx(
                c,
                match_id,
                dispatcher_token,
                dispatcher_epoch,
                require_claim_fence=True,
            )
            if str(row["execution_scope"] or "") != execution_scope:
                raise AutoMatchFenceLost(
                    f"auto-match execution scope lost for match {match_id}"
                )
            current = str(row["execution_launch_state"] or "")
            current_token = str(row["execution_launch_token"] or "")
            token = str(launch_token or "")
            incarnation = str(daemon_incarnation or "")
            if not token:
                raise ValueError("auto execution launch token required")
            if launch_state == "creating" and not incarnation:
                raise ValueError("auto execution daemon incarnation required")
            if current == launch_state and current_token == token:
                if launch_state == "creating" and str(
                    row["execution_daemon_incarnation"] or ""
                ) != incarnation:
                    # After an ambiguous CLI terminal result, advancing this
                    # marker to the then-current daemon is conservative: it can
                    # only require an additional restart before negative proof.
                    c.execute(
                        "UPDATE auto_match_queue SET execution_daemon_incarnation=? "
                        "WHERE id=?",
                        (incarnation, int(row["id"])),
                    )
                    c.execute(
                        "UPDATE auto_match_decisions SET execution_daemon_incarnation=? "
                        "WHERE id=?",
                        (incarnation, int(row["decision_id"])),
                    )
                return
            if current not in transitions[launch_state]:
                raise AutoMatchFenceLost(
                    f"invalid auto launch transition {current}->{launch_state}"
                )
            c.execute(
                "UPDATE auto_match_queue SET execution_launch_state=?,"
                "execution_launch_token=?,execution_daemon_incarnation="
                "CASE WHEN ?='creating' THEN ? ELSE execution_daemon_incarnation END "
                "WHERE id=?",
                (launch_state, token, launch_state, incarnation, int(row["id"])),
            )
            c.execute(
                "UPDATE auto_match_decisions SET execution_launch_state=?,"
                "execution_launch_token=?,execution_daemon_incarnation="
                "CASE WHEN ?='creating' THEN ? ELSE execution_daemon_incarnation END "
                "WHERE id=?",
                (
                    launch_state,
                    token,
                    launch_state,
                    incarnation,
                    int(row["decision_id"]),
                ),
            )

    def mark_auto_match_execution_cleanup_confirmed(
        self,
        match_id: str,
        dispatcher_token: str,
        dispatcher_epoch: int,
        execution_scope: str,
    ) -> None:
        """Persist same-owner physical zero proof before terminal release."""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            row = self._require_auto_match_fence_tx(
                c,
                match_id,
                dispatcher_token,
                dispatcher_epoch,
                require_claim_fence=True,
            )
            if str(row["execution_scope"] or "") != execution_scope:
                raise AutoMatchFenceLost(
                    f"auto-match execution scope lost for match {match_id}"
                )
            if str(row["execution_launch_state"] or "") == "creating":
                raise AutoMatchFenceLost(
                    f"auto-match create evidence missing for match {match_id}"
                )
            if str(row["execution_state"] or "") == "cleanup_confirmed":
                return
            ack_at = _now()
            changed = c.execute(
                "UPDATE auto_match_queue SET execution_state='cleanup_confirmed',"
                "cleanup_ack_at=?,cleanup_error='' WHERE id=? "
                "AND execution_state IN ('claimed','running','recovery_pending')",
                (ack_at, int(row["id"])),
            )
            if changed.rowcount != 1:
                raise AutoMatchFenceLost(
                    f"auto-match cleanup confirmation CAS lost for {match_id}"
                )
            c.execute(
                "UPDATE auto_match_decisions SET execution_state='cleanup_confirmed',"
                "cleanup_ack_at=?,cleanup_error='' WHERE id=? "
                "AND lifecycle='dispatched'",
                (ack_at, int(row["decision_id"])),
            )

    def record_auto_match_execution_launch_observed(
        self,
        match_id: str,
        *,
        execution_scope: str,
        launch_token: str,
    ) -> bool:
        """Durably record exact Docker evidence, even across an epoch switch.

        This is a monotonic evidence-only CAS: it cannot launch, finalize, or
        release a queue slot.  Allowing the old worker to persist the exact
        scope+launch label prevents it from deleting the only evidence after a
        takeover changed the epoch between inspect and this write.
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM auto_match_queue WHERE match_id=? "
                "AND status='dispatched' AND execution_scope=?",
                (match_id, execution_scope),
            ).fetchone()
            if row is None:
                return False
            if str(row["execution_launch_token"] or "") != str(launch_token):
                return False
            state = str(row["execution_launch_state"] or "")
            if state != "creating":
                return state in {"created", "started"}
            c.execute(
                "UPDATE auto_match_queue SET execution_launch_state='created' WHERE id=?",
                (int(row["id"]),),
            )
            c.execute(
                "UPDATE auto_match_decisions SET execution_launch_state='created' WHERE id=?",
                (int(row["decision_id"]),),
            )
            return True

    def record_auto_match_execution_ambiguous_incarnation(
        self,
        match_id: str,
        *,
        execution_scope: str,
        launch_token: str,
        daemon_incarnation: str,
    ) -> bool:
        """Conservatively advance an ambiguous RPC marker across takeover."""
        if not str(daemon_incarnation or ""):
            return False
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT * FROM auto_match_queue WHERE match_id=? "
                "AND status='dispatched' AND execution_scope=?",
                (match_id, execution_scope),
            ).fetchone()
            if (
                row is None
                or str(row["execution_launch_state"] or "") != "creating"
                or str(row["execution_launch_token"] or "") != str(launch_token)
            ):
                return False
            c.execute(
                "UPDATE auto_match_queue SET execution_daemon_incarnation=? WHERE id=?",
                (daemon_incarnation, int(row["id"])),
            )
            c.execute(
                "UPDATE auto_match_decisions SET execution_daemon_incarnation=? WHERE id=?",
                (daemon_incarnation, int(row["decision_id"])),
            )
            return True

    def record_auto_match_execution_daemon_restart(
        self,
        match_id: str,
        *,
        dispatcher_token: str,
        dispatcher_epoch: int,
        execution_scope: str,
        previous_incarnation: str,
        current_incarnation: str,
    ) -> bool:
        """Resolve an ambiguous create only after a comparable daemon restart."""
        if not _daemon_incarnation_changed(
            previous_incarnation, current_incarnation
        ):
            return False
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._auto_dispatcher_owned_tx(
                c, dispatcher_token, dispatcher_epoch
            ):
                return False
            row = c.execute(
                "SELECT * FROM auto_match_queue WHERE match_id=? "
                "AND status='dispatched' AND dispatcher_token=? AND dispatcher_epoch=? "
                "AND execution_scope=? AND execution_state='recovery_pending'",
                (
                    match_id,
                    dispatcher_token,
                    int(dispatcher_epoch),
                    execution_scope,
                ),
            ).fetchone()
            if row is None:
                return False
            if str(row["execution_launch_state"] or "") != "creating":
                return str(row["execution_launch_state"] or "") in {
                    "created",
                    "started",
                }
            if str(row["execution_daemon_incarnation"] or "") != str(
                previous_incarnation
            ):
                return False
            c.execute(
                "UPDATE auto_match_queue SET execution_launch_state='created',"
                "execution_daemon_incarnation=? WHERE id=?",
                (current_incarnation, int(row["id"])),
            )
            c.execute(
                "UPDATE auto_match_decisions SET execution_launch_state='created',"
                "execution_daemon_incarnation=? WHERE id=?",
                (current_incarnation, int(row["decision_id"])),
            )
            return True

    def acquire_auto_match_dispatcher(
        self, dispatcher_token: str, *, lease_seconds: int = 30
    ) -> dict:
        """Acquire/renew the single dispatcher lease with a write-linearized CAS."""
        if not dispatcher_token:
            raise ValueError("dispatcher token required")
        now = _now()
        lease_until = _after_seconds(max(5, int(lease_seconds)))
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT owner_token,lease_epoch,lease_until FROM auto_match_dispatcher "
                "WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise RuntimeError("auto_match_dispatcher singleton missing")
            previous = str(row["owner_token"] or "")
            expired = not row["lease_until"] or str(row["lease_until"]) <= now
            if previous == dispatcher_token or not previous or expired:
                # Expiry is a fencing boundary even when the same process token
                # later reacquires.  Its pre-expiry worker must not inherit the
                # renewed epoch after an event-loop stall.
                changed_owner = previous != dispatcher_token or expired
                epoch = int(row["lease_epoch"] or 0) + (1 if changed_owner else 0)
                c.execute(
                    "UPDATE auto_match_dispatcher SET owner_token=?,lease_epoch=?,lease_until=?,"
                    "heartbeat_at=? WHERE singleton=1",
                    (dispatcher_token, epoch, lease_until, now),
                )
                return {
                    "owned": True,
                    "changed_owner": changed_owner,
                    "previous_owner": previous,
                    "lease_epoch": epoch,
                    "lease_until": lease_until,
                }
            return {
                "owned": False,
                "changed_owner": False,
                "previous_owner": previous,
                "lease_epoch": int(row["lease_epoch"] or 0),
                "lease_until": row["lease_until"],
            }

    def release_auto_match_dispatcher(
        self, dispatcher_token: str, dispatcher_epoch: int
    ) -> bool:
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            cur = c.execute(
                "UPDATE auto_match_dispatcher SET owner_token=NULL,lease_until=NULL,"
                "heartbeat_at=? WHERE singleton=1 AND owner_token=? AND lease_epoch=?",
                (_now(), dispatcher_token, int(dispatcher_epoch)),
            )
            return cur.rowcount == 1

    def auto_match_dispatcher_state(self) -> dict:
        with self._tx() as c:
            row = c.execute(
                "SELECT owner_token,lease_epoch,lease_until,heartbeat_at "
                "FROM auto_match_dispatcher "
                "WHERE singleton=1"
            ).fetchone()
            return dict(row) if row else {}

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
                "LEFT JOIN match_rating_settlements settled "
                "ON settled.match_id=policy.match_id "
                "WHERE policy.settled_order IS NOT NULL "
                "AND settled.match_id IS NULL ORDER BY policy.settled_order"
            ).fetchall()
        ]
        reserved_orders = [int(row["settled_order"]) for row in reserved]
        expected_orders = list(
            range(settled_count + 1, settled_count + len(reserved) + 1)
        )
        expected_issues = {
            f"rating policy reserved but unsettled: {row['match_id']}"
            for row in reserved
        }
        shape_valid = bool(
            settled_last == settled_count
            and reserved_orders == expected_orders
            and int(live["sequence_next_order"])
            == settled_count + len(reserved) + 1
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

    @staticmethod
    def _auto_backoff_tx(c: sqlite3.Connection, reason: str) -> dict:
        state = c.execute(
            "SELECT platform_failures FROM auto_match_fair_state WHERE singleton=1"
        ).fetchone()
        failures = int(state["platform_failures"] or 0) + 1 if state else 1
        delay = min(300, 2 ** min(failures - 1, 8))
        not_before = _after_seconds(delay)
        c.execute(
            "UPDATE auto_match_fair_state SET platform_failures=?,not_before=?,"
            "updated_at=? WHERE singleton=1",
            (failures, not_before, _now()),
        )
        return {"failures": failures, "not_before": not_before, "reason": reason}

    @staticmethod
    def _auto_reset_backoff_tx(c: sqlite3.Connection) -> None:
        c.execute(
            "UPDATE auto_match_fair_state SET platform_failures=0,not_before=NULL,"
            "updated_at=? WHERE singleton=1",
            (_now(),),
        )

    @staticmethod
    def _auto_cancel_queue_row_tx(
        c: sqlite3.Connection, row: sqlite3.Row | dict, reason: str
    ) -> None:
        q = dict(row)
        now = _now()
        c.execute(
            "UPDATE auto_match_decisions SET lifecycle='cancelled',terminal_at=?,"
            "terminal_reason=? WHERE id=? AND lifecycle IN ('queued','dispatched')",
            (now, reason, int(q["decision_id"])),
        )
        c.execute("DELETE FROM auto_match_queue WHERE id=?", (int(q["id"]),))

    def _auto_queue_candidates_tx(self, c: sqlite3.Connection) -> list[dict]:
        rows = c.execute(
            "SELECT b.id AS bot_id,b.owner_id,b.game_id,b.name AS bot_name,"
            "b.display_name AS bot_display,u.username AS owner_name,"
            "u.display_name AS owner_display,v.id AS version_id,r.rating,r.rd,"
            "r.matches_played,COALESCE(os.served_count,0) AS owner_service,"
            "COALESCE(os.last_served_revision,0) AS owner_last_revision,"
            "COALESCE(bs.served_count,0) AS bot_service,"
            "COALESCE(bs.last_served_revision,0) AS bot_last_revision,"
            "COALESCE(bs.seat_a_count,0) AS seat_a_count,"
            "COALESCE(bs.seat_b_count,0) AS seat_b_count "
            "FROM bots b JOIN users u ON u.id=b.owner_id "
            "JOIN ratings r ON r.bot_id=b.id AND r.game_id=b.game_id "
            "JOIN bot_versions v ON v.bot_id=b.id AND v.version=b.current_version "
            "LEFT JOIN auto_match_owner_service os "
            "ON os.owner_id=b.owner_id AND os.game_id=b.game_id "
            "LEFT JOIN auto_match_bot_service bs "
            "ON bs.bot_id=b.id AND bs.game_id=b.game_id "
            "WHERE b.is_active=1 AND b.is_builtin=0 AND u.is_active=1 "
            "AND b.binary_path<>'' AND v.binary_path<>'' "
            "AND b.binary_path=v.binary_path AND b.runtime_mode=v.runtime_mode "
            "AND b.format=? AND b.os=? AND b.arch=? "
            "AND v.format=? AND v.os=? AND v.arch=? "
            "AND NOT EXISTS (SELECT 1 FROM auto_match_queue q "
            "WHERE b.id IN (q.bot_a_id,q.bot_b_id)) "
            "AND NOT EXISTS (SELECT 1 FROM auto_match_queue q "
            "JOIN bots qa ON qa.id=q.bot_a_id JOIN bots qb ON qb.id=q.bot_b_id "
            "WHERE b.owner_id IN (qa.owner_id,qb.owner_id))",
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
            "FROM auto_match_queue WHERE bot_a_id=? OR bot_b_id=?",
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
        placement_required: int,
    ) -> tuple[dict, dict, str, int, int, float] | None:
        game = [bot for bot in candidates if bot["game_id"] == game_id]
        if len({int(bot["owner_id"]) for bot in game}) < 2:
            return None
        for bot in game:
            bot["_lane"] = (
                "placement"
                if int(bot.get("matches_played") or 0) < placement_required
                else "formal"
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
            if not partners and lane == "placement":
                # The sole placement owner still receives calibration service,
                # but never against itself; a formal owner is the auditable fallback.
                partners = [
                    bot for bot in game
                    if int(bot["owner_id"]) != int(anchor["owner_id"])
                    and bot["_lane"] == "formal"
                ]
                if partners:
                    widened_reason = "single_placement_owner"
            elif not partners and lane == "formal":
                placement_partners = [
                    bot for bot in game
                    if int(bot["owner_id"]) != int(anchor["owner_id"])
                    and bot["_lane"] == "placement"
                ]
                # A lone formal owner must still receive formal-lane service.
                # Require at least two placement owners so the fallback does not
                # degenerate into one permanently repeated cross-lane pair.
                if len({int(bot["owner_id"]) for bot in placement_partners}) >= 2:
                    partners = placement_partners
                    widened_reason = "single_formal_owner"
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

    def refill_auto_match_queue(
        self,
        *,
        target_queued: int,
        placement_required: int,
        dispatcher_token: str,
        dispatcher_epoch: int,
    ) -> dict:
        """Fill fixed lookahead under the persistent game/lane/owner policy."""
        target = max(0, int(target_queued))
        placement = max(0, int(placement_required))
        inserted = 0
        removed_invalid = 0
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._auto_dispatcher_owned_tx(
                c, dispatcher_token, dispatcher_epoch
            ):
                return {"outcome": "not_leader", "inserted": 0}
            switch = c.execute(
                "SELECT enabled FROM auto_match_control WHERE singleton=1"
            ).fetchone()
            if not switch or int(switch["enabled"]) != 1:
                return {"outcome": "disabled", "inserted": 0}
            projection = self._rating_projection_status_tx(c)
            if not projection["ready"]:
                return {
                    "outcome": "rating_unverified",
                    "inserted": 0,
                    "rating_projection": projection,
                }
            fair = c.execute(
                "SELECT * FROM auto_match_fair_state WHERE singleton=1"
            ).fetchone()
            if fair is None:
                raise RuntimeError("auto_match_fair_state singleton missing")
            if fair["not_before"] and str(fair["not_before"]) > _now():
                return {
                    "outcome": "backoff",
                    "inserted": 0,
                    "not_before": fair["not_before"],
                    "platform_failures": int(fair["platform_failures"] or 0),
                }

            for row in c.execute(
                "SELECT * FROM auto_match_queue WHERE status='queued' ORDER BY id"
            ).fetchall():
                if self._auto_queue_identity_tx(c, row) is None:
                    self._auto_cancel_queue_row_tx(c, row, "identity_invalid")
                    removed_invalid += 1

            queued_row = c.execute(
                "SELECT COUNT(*) AS n FROM auto_match_queue WHERE status='queued'"
            ).fetchone()
            queued_count = int(queued_row["n"] if queued_row else 0)
            game_ids = sorted(_all_game_ids())
            while queued_count < target:
                candidates = self._auto_queue_candidates_tx(c)
                state = c.execute(
                    "SELECT * FROM auto_match_fair_state WHERE singleton=1"
                ).fetchone()
                cursor = int(state["next_game_idx"] or 0) % max(1, len(game_ids))
                requested_lane = (
                    "placement" if int(state["next_lane"] or 0) == 0 else "formal"
                )
                rotated = game_ids[cursor:] + game_ids[:cursor]
                selected = None
                selected_game_idx = cursor
                actual_lane = requested_lane
                lane_fallback = ""
                for candidate_lane in (requested_lane, (
                    "formal" if requested_lane == "placement" else "placement"
                )):
                    for gid in rotated:
                        choice = self._auto_choose_pair_tx(
                            c,
                            candidates,
                            game_id=gid,
                            lane=candidate_lane,
                            placement_required=placement,
                        )
                        if choice is not None:
                            selected = choice
                            selected_game_idx = game_ids.index(gid)
                            actual_lane = candidate_lane
                            if candidate_lane != requested_lane:
                                lane_fallback = "requested_lane_empty"
                            break
                    if selected is not None:
                        break
                if selected is None:
                    break

                anchor, partner, partner_fallback, bot_pair, owner_pair, rating_gap = selected
                anchor_debt = self._auto_queue_seat_debt_tx(c, anchor)
                partner_debt = self._auto_queue_seat_debt_tx(c, partner)
                normal_cost = abs(anchor_debt + 1) + abs(partner_debt - 1)
                reverse_cost = abs(anchor_debt - 1) + abs(partner_debt + 1)
                if reverse_cost < normal_cost:
                    bot_a, bot_b = partner, anchor
                    debt_a, debt_b = partner_debt, anchor_debt
                elif normal_cost < reverse_cost:
                    bot_a, bot_b = anchor, partner
                    debt_a, debt_b = anchor_debt, partner_debt
                elif int(anchor["bot_id"]) <= int(partner["bot_id"]):
                    bot_a, bot_b = anchor, partner
                    debt_a, debt_b = anchor_debt, partner_debt
                else:
                    bot_a, bot_b = partner, anchor
                    debt_a, debt_b = partner_debt, anchor_debt

                fallback = ",".join(
                    part for part in (lane_fallback, partner_fallback) if part
                )
                reason = (
                    ("定级通道" if actual_lane == "placement" else "正式通道")
                    + f" · owner/Bot 轮转 · Bot交手 {bot_pair} · "
                    + f"owner交手 {owner_pair} · Rating差 {rating_gap:.0f} · 先后手平衡"
                )
                now = _now()
                decision = c.execute(
                    "INSERT INTO auto_match_decisions("
                    "policy_version,state_revision,cursor_game_idx,requested_lane,"
                    "actual_lane,fallback_reason,game_id,bot_a_id,bot_b_id,"
                    "owner_a_id,owner_b_id,bot_a_version_id,bot_b_version_id,"
                    "owner_a_service_before,owner_b_service_before,"
                    "bot_a_service_before,bot_b_service_before,bot_pair_count_before,"
                    "owner_pair_count_before,rating_gap,bot_a_seat_debt_before,"
                    "bot_b_seat_debt_before,selection_reason,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        _AUTO_MATCH_POLICY_VERSION,
                        int(state["revision"] or 0),
                        cursor,
                        requested_lane,
                        actual_lane,
                        fallback,
                        bot_a["game_id"],
                        int(bot_a["bot_id"]),
                        int(bot_b["bot_id"]),
                        int(bot_a["owner_id"]),
                        int(bot_b["owner_id"]),
                        int(bot_a["version_id"]),
                        int(bot_b["version_id"]),
                        int(bot_a.get("owner_service") or 0),
                        int(bot_b.get("owner_service") or 0),
                        int(bot_a.get("bot_service") or 0),
                        int(bot_b.get("bot_service") or 0),
                        bot_pair,
                        owner_pair,
                        rating_gap,
                        debt_a,
                        debt_b,
                        reason,
                        now,
                    ),
                )
                c.execute(
                    "INSERT INTO auto_match_queue("
                    "decision_id,game_id,bot_a_id,bot_b_id,bot_a_version_id,"
                    "bot_b_version_id,status,selection_reason,created_at) "
                    "VALUES(?,?,?,?,?,?,'queued',?,?)",
                    (
                        int(decision.lastrowid),
                        bot_a["game_id"],
                        int(bot_a["bot_id"]),
                        int(bot_b["bot_id"]),
                        int(bot_a["version_id"]),
                        int(bot_b["version_id"]),
                        reason,
                        now,
                    ),
                )
                c.execute(
                    "UPDATE auto_match_fair_state SET next_game_idx=?,next_lane=?,"
                    "revision=revision+1,updated_at=? WHERE singleton=1",
                    (
                        (selected_game_idx + 1) % max(1, len(game_ids)),
                        1 - int(state["next_lane"] or 0),
                        now,
                    ),
                )
                queued_count += 1
                inserted += 1

            return {
                "outcome": "ok",
                "inserted": inserted,
                "removed_invalid": removed_invalid,
                "queued": queued_count,
                "remaining_eligible": len(self._auto_queue_candidates_tx(c)),
            }

    def list_auto_match_queue(self, *, game_id: str | None = None) -> list[dict]:
        """Return active/upcoming rows with public Bot identity and global position."""
        gid = _registered_game_id(game_id) if game_id is not None else None
        with self._tx() as c:
            rows = c.execute(
                "SELECT q.*,d.requested_lane,d.actual_lane,d.fallback_reason, "
                "ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "ua.username AS bot_a_owner, ua.display_name AS bot_a_owner_display, "
                "ra.rating AS bot_a_rating, ra.matches_played AS bot_a_matches_played, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
                "ub.username AS bot_b_owner, ub.display_name AS bot_b_owner_display, "
                "rb.rating AS bot_b_rating, rb.matches_played AS bot_b_matches_played "
                "FROM auto_match_queue q JOIN auto_match_decisions d ON d.id=q.decision_id "
                "JOIN bots ba ON ba.id=q.bot_a_id JOIN users ua ON ua.id=ba.owner_id "
                "JOIN ratings ra ON ra.bot_id=ba.id AND ra.game_id=q.game_id "
                "JOIN bots bb ON bb.id=q.bot_b_id JOIN users ub ON ub.id=bb.owner_id "
                "JOIN ratings rb ON rb.bot_id=bb.id AND rb.game_id=q.game_id "
                "ORDER BY CASE q.status WHEN 'dispatched' THEN 0 ELSE 1 END, q.id"
            ).fetchall()
            projected: list[dict] = []
            queued_position = 0
            for raw in rows:
                item = dict(raw)
                if item["status"] == "queued":
                    queued_position += 1
                    item["position"] = queued_position
                else:
                    item["position"] = 0
                    tbl = self._match_table_of(c, str(item.get("match_id") or ""))
                    match = (
                        c.execute(
                            f"SELECT status,reason,started_at,created_at FROM {tbl} "
                            "WHERE id=?",
                            (item["match_id"],),
                        ).fetchone()
                        if tbl
                        else None
                    )
                    item["match_status"] = match["status"] if match else None
                    item["match_reason"] = match["reason"] if match else None
                    item["match_started_at"] = match["started_at"] if match else None
                if gid is None or item["game_id"] == gid:
                    projected.append(item)
            return projected

    def claim_next_auto_match(
        self,
        match_id: str,
        *,
        dispatcher_token: str,
        dispatcher_epoch: int,
        execution_backend: str = "docker",
    ) -> dict:
        """Atomically claim the global head and create its match/index/replay.

        The partial unique index permits at most one dispatched row across all
        processes.  Switch, lease, frozen versions and rating lifecycle are all
        re-read after BEGIN IMMEDIATE, so an acknowledged ``off`` cannot race a
        later dispatch.
        """
        if execution_backend not in {"docker", "local"}:
            raise ValueError("unknown auto-match execution backend")
        execution_scope = secrets.token_hex(24)
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._auto_dispatcher_owned_tx(
                c, dispatcher_token, dispatcher_epoch
            ):
                return {"outcome": "not_leader", "reason": "调度租约不属于当前进程"}
            switch = c.execute(
                "SELECT enabled FROM auto_match_control WHERE singleton=1"
            ).fetchone()
            if not switch or int(switch["enabled"]) != 1:
                return {"outcome": "disabled", "reason": "管理员已关闭自动排位"}
            projection = self._rating_projection_status_tx(c)
            if not projection["ready"]:
                return {
                    "outcome": "rating_unverified",
                    "reason": "排行榜投影尚未按当前评分策略验证",
                    "rating_projection": projection,
                }
            fair = c.execute(
                "SELECT not_before,platform_failures FROM auto_match_fair_state "
                "WHERE singleton=1"
            ).fetchone()
            if fair and fair["not_before"] and str(fair["not_before"]) > _now():
                return {
                    "outcome": "backoff",
                    "reason": "平台故障退避中",
                    "not_before": fair["not_before"],
                    "platform_failures": int(fair["platform_failures"] or 0),
                }
            if c.execute(
                "SELECT 1 FROM auto_match_queue WHERE status='dispatched' LIMIT 1"
            ).fetchone():
                return {"outcome": "busy", "reason": "已有自动排位正在进行"}
            row = c.execute(
                "SELECT * FROM auto_match_queue WHERE status='queued' ORDER BY id LIMIT 1"
            ).fetchone()
            if row is None:
                return {"outcome": "empty", "reason": "暂无可配对 Bot"}
            identity = self._auto_queue_identity_tx(c, row)
            if identity is None:
                self._auto_cancel_queue_row_tx(c, row, "identity_invalid_at_claim")
                return {"outcome": "invalid", "reason": "队首 Bot 或冻结版本已失效"}
            gid = _registered_game_id(row["game_id"])
            for bot_id in (int(row["bot_a_id"]), int(row["bot_b_id"])):
                if self._bot_has_active_rated_match_tx(c, bot_id, game_id=gid):
                    return {
                        "outcome": "blocked",
                        "reason": "队首 Bot 正在其他计分对局中",
                    }

            tbl = _matches_table(gid)
            created_at = _now()
            match_config = json.dumps(
                {
                    "_bot_a_version_id": int(row["bot_a_version_id"]),
                    "_bot_b_version_id": int(row["bot_b_version_id"]),
                    "_auto_match_queue_id": int(row["id"]),
                    "_auto_match_decision_id": int(row["decision_id"]),
                    "_auto_match_claim_token": dispatcher_token,
                    "_auto_match_claim_epoch": int(dispatcher_epoch),
                    "_auto_match_execution_scope": execution_scope,
                    "_rating_eligible": True,
                    "_rating_reason": "eligible",
                },
                ensure_ascii=False,
            )
            c.execute(
                f"INSERT INTO {tbl}(id,bot_a_id,bot_b_id,owner_id,contest_id,reason,"
                "match_type,status,game_id,match_config,human_user_id,human_seat,created_at) "
                "VALUES(?,?,?,NULL,NULL,'',?,'pending',?,?,NULL,NULL,?)",
                (
                    match_id,
                    int(row["bot_a_id"]),
                    int(row["bot_b_id"]),
                    TYPE_LADDER,
                    gid,
                    match_config,
                    created_at,
                ),
            )
            c.execute(
                "INSERT INTO match_rating_policies("
                "match_id,game_id,bot_a_id,bot_b_id,rated,rating_reason,source,"
                "classified_at) VALUES(?,?,?,?,1,'eligible','auto_v2',?)",
                (
                    match_id,
                    gid,
                    int(row["bot_a_id"]),
                    int(row["bot_b_id"]),
                    created_at,
                ),
            )
            c.execute(
                "INSERT INTO matches_index(id,game_id) VALUES(?,?)",
                (match_id, gid),
            )
            c.execute(
                "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?, '[]', ?)",
                (match_id, created_at),
            )
            c.execute(
                "UPDATE auto_match_queue SET status='dispatched', match_id=?, "
                "dispatcher_token=?,dispatcher_epoch=?,dispatched_at=?,"
                "execution_scope=?,execution_backend=?,execution_state='claimed',"
                "execution_launch_state='unstarted',execution_launch_token=NULL,"
                "execution_daemon_incarnation=NULL,"
                "cleanup_requested_at=NULL,cleanup_ack_at=NULL,cleanup_error='' "
                "WHERE id=? AND status='queued'",
                (
                    match_id,
                    dispatcher_token,
                    int(dispatcher_epoch),
                    created_at,
                    execution_scope,
                    execution_backend,
                    row["id"],
                ),
            )
            if c.execute("SELECT changes()").fetchone()[0] != 1:
                raise RuntimeError("auto-match queue claim CAS lost")
            updated = c.execute(
                "UPDATE auto_match_decisions SET lifecycle='dispatched',match_id=?,"
                "attempt_count=attempt_count+1,last_attempt_error='',dispatched_at=?,"
                "claim_dispatcher_token=?,claim_dispatcher_epoch=?,"
                "execution_scope=?,execution_backend=?,execution_state='claimed',"
                "execution_launch_state='unstarted',execution_launch_token=NULL,"
                "execution_daemon_incarnation=NULL,"
                "cleanup_requested_at=NULL,cleanup_ack_at=NULL,cleanup_error='' "
                "WHERE id=? AND lifecycle='queued'",
                (
                    match_id,
                    created_at,
                    dispatcher_token,
                    int(dispatcher_epoch),
                    execution_scope,
                    execution_backend,
                    int(row["decision_id"]),
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("auto-match decision claim CAS lost")
            return {
                "outcome": "claimed",
                "match_id": match_id,
                "queue_id": int(row["id"]),
                "game_id": gid,
                "dispatcher_token": dispatcher_token,
                "dispatcher_epoch": int(dispatcher_epoch),
                "execution_scope": execution_scope,
                "execution_backend": execution_backend,
                "launch_lock_path": self.auto_match_execution_launch_lock_path,
            }

    def rollback_auto_match_claim(
        self,
        match_id: str,
        *,
        dispatcher_token: str,
        dispatcher_epoch: int,
        reason: str = "start_failure",
    ) -> bool:
        """Delete an unstarted claim and restore the exact queue row atomically."""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._auto_dispatcher_owned_tx(
                c, dispatcher_token, dispatcher_epoch
            ):
                return False
            row = c.execute(
                "SELECT * FROM auto_match_queue WHERE status='dispatched' "
                "AND match_id=? AND dispatcher_token=? AND dispatcher_epoch=?",
                (match_id, dispatcher_token, int(dispatcher_epoch)),
            ).fetchone()
            if row is None:
                return False
            if str(row["execution_launch_state"] or "") != "unstarted":
                # Once any physical launch began, only a zero-proof cleanup
                # transition may release this dispatched barrier.
                return False
            table = self._match_table_of(c, match_id)
            match = (
                c.execute(f"SELECT status FROM {table} WHERE id=?", (match_id,)).fetchone()
                if table else None
            )
            if match is None or match["status"] != STATUS_PENDING:
                return False
            c.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
            c.execute("DELETE FROM matches_index WHERE id=?", (match_id,))
            c.execute("DELETE FROM match_rating_policies WHERE match_id=?", (match_id,))
            c.execute(f"DELETE FROM {table} WHERE id=?", (match_id,))
            c.execute(
                "UPDATE auto_match_queue SET status='queued',match_id=NULL,"
                "dispatcher_token=NULL,dispatcher_epoch=NULL,dispatched_at=NULL,"
                "execution_scope=NULL,execution_backend=NULL,execution_state=NULL,"
                "execution_launch_state=NULL,execution_launch_token=NULL,"
                "execution_daemon_incarnation=NULL,"
                "cleanup_requested_at=NULL,cleanup_ack_at=NULL,cleanup_error='' WHERE id=?",
                (int(row["id"]),),
            )
            c.execute(
                "UPDATE auto_match_decisions SET lifecycle='queued',match_id=NULL,"
                "dispatched_at=NULL,last_attempt_error=?,claim_dispatcher_token=NULL,"
                "claim_dispatcher_epoch=NULL,execution_scope=NULL,"
                "execution_backend=NULL,execution_state=NULL,cleanup_requested_at=NULL,"
                "execution_launch_state=NULL,execution_launch_token=NULL,"
                "execution_daemon_incarnation=NULL,"
                "cleanup_ack_at=NULL,cleanup_error='' WHERE id=? "
                "AND lifecycle='dispatched'",
                (reason, int(row["decision_id"])),
            )
            self._auto_backoff_tx(c, reason)
            return True

    def recover_auto_match_dispatcher_takeover(
        self, *, dispatcher_token: str, dispatcher_epoch: int
    ) -> dict:
        """Adopt a stale claim but retain the global slot until physical cleanup.

        Epoch takeover fences all old Store writes first.  This transaction only
        advances the durable claim to ``recovery_pending``; it deliberately does
        not abort/delete/requeue anything.  The scheduler must acquire the shared
        launch flock, force-stop the persisted scope, prove zero containers, and
        call :meth:`finalize_auto_match_execution_cleanup` separately.
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._auto_dispatcher_owned_tx(
                c, dispatcher_token, dispatcher_epoch
            ):
                return {"outcome": "not_leader"}
            row = c.execute(
                "SELECT * FROM auto_match_queue WHERE status='dispatched' LIMIT 1"
            ).fetchone()
            if row is None:
                return {"outcome": "clean"}
            if (
                row["dispatcher_token"] == dispatcher_token
                and int(row["dispatcher_epoch"] or 0) == int(dispatcher_epoch)
            ):
                if row["execution_state"] == "recovery_pending":
                    return {
                        "outcome": "recovery_pending",
                        "match_id": str(row["match_id"]),
                        "execution_scope": str(row["execution_scope"]),
                        "execution_backend": str(row["execution_backend"]),
                        "execution_launch_state": str(row["execution_launch_state"]),
                        "execution_launch_token": str(row["execution_launch_token"] or ""),
                        "execution_daemon_incarnation": str(
                            row["execution_daemon_incarnation"] or ""
                        ),
                        "launch_lock_path": self.auto_match_execution_launch_lock_path,
                        "cleanup_error": str(row["cleanup_error"] or ""),
                    }
                return {"outcome": "clean"}
            match_id = str(row["match_id"] or "")
            scope = str(row["execution_scope"] or "")
            backend = str(row["execution_backend"] or "")
            if not scope or backend not in {"docker", "local"}:
                raise RuntimeError("stale auto-match claim lacks execution identity")
            requested_at = _now()
            changed = c.execute(
                "UPDATE auto_match_queue SET dispatcher_token=?,dispatcher_epoch=?,"
                "execution_state='recovery_pending',cleanup_requested_at=?,"
                "cleanup_ack_at=NULL,cleanup_error='' "
                "WHERE id=? AND status='dispatched'",
                (
                    dispatcher_token,
                    int(dispatcher_epoch),
                    requested_at,
                    int(row["id"]),
                ),
            )
            if changed.rowcount != 1:
                raise RuntimeError("auto-match takeover CAS lost")
            c.execute(
                "UPDATE auto_match_decisions SET execution_state='recovery_pending',"
                "cleanup_requested_at=?,cleanup_ack_at=NULL,cleanup_error='' "
                "WHERE id=? AND lifecycle='dispatched'",
                (requested_at, int(row["decision_id"])),
            )
            return {
                "outcome": "recovery_pending",
                "match_id": match_id,
                "execution_scope": scope,
                "execution_backend": backend,
                "execution_launch_state": str(row["execution_launch_state"]),
                "execution_launch_token": str(row["execution_launch_token"] or ""),
                "execution_daemon_incarnation": str(
                    row["execution_daemon_incarnation"] or ""
                ),
                "launch_lock_path": self.auto_match_execution_launch_lock_path,
                "cleanup_error": "",
            }

    def get_auto_match_execution_recovery(
        self, *, dispatcher_token: str, dispatcher_epoch: int
    ) -> dict | None:
        """Return the current owner's durable recovery scope, if any."""
        with self._tx() as c:
            c.execute("BEGIN")
            if not self._auto_dispatcher_owned_tx(
                c, dispatcher_token, dispatcher_epoch
            ):
                return None
            row = c.execute(
                "SELECT * FROM auto_match_queue WHERE status='dispatched' "
                "AND dispatcher_token=? AND dispatcher_epoch=? "
                "AND execution_state='recovery_pending' LIMIT 1",
                (dispatcher_token, int(dispatcher_epoch)),
            ).fetchone()
            if row is None:
                return None
            return {
                "match_id": str(row["match_id"]),
                "execution_scope": str(row["execution_scope"]),
                "execution_backend": str(row["execution_backend"]),
                "execution_launch_state": str(row["execution_launch_state"]),
                "execution_launch_token": str(row["execution_launch_token"] or ""),
                "execution_daemon_incarnation": str(
                    row["execution_daemon_incarnation"] or ""
                ),
                "launch_lock_path": self.auto_match_execution_launch_lock_path,
                "cleanup_error": str(row["cleanup_error"] or ""),
            }

    def record_auto_match_execution_cleanup_failure(
        self,
        match_id: str,
        *,
        dispatcher_token: str,
        dispatcher_epoch: int,
        execution_scope: str,
        reason: str,
    ) -> bool:
        """Persist a failed proof while retaining the one-dispatched slot."""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._auto_dispatcher_owned_tx(
                c, dispatcher_token, dispatcher_epoch
            ):
                return False
            message = str(reason or "execution cleanup not confirmed")[:500]
            changed = c.execute(
                "UPDATE auto_match_queue SET cleanup_error=? WHERE match_id=? "
                "AND status='dispatched' AND dispatcher_token=? AND dispatcher_epoch=? "
                "AND execution_scope=? AND execution_state='recovery_pending'",
                (
                    message,
                    match_id,
                    dispatcher_token,
                    int(dispatcher_epoch),
                    execution_scope,
                ),
            )
            if changed.rowcount != 1:
                return False
            c.execute(
                "UPDATE auto_match_decisions SET cleanup_error=? WHERE match_id=? "
                "AND execution_scope=? AND execution_state='recovery_pending'",
                (message, match_id, execution_scope),
            )
            return True

    def finalize_auto_match_execution_cleanup(
        self,
        match_id: str,
        *,
        dispatcher_token: str,
        dispatcher_epoch: int,
        execution_scope: str,
    ) -> dict:
        """Acknowledge zero physical workers, then converge the recovered claim."""
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._auto_dispatcher_owned_tx(
                c, dispatcher_token, dispatcher_epoch
            ):
                return {"outcome": "not_leader"}
            row = c.execute(
                "SELECT * FROM auto_match_queue WHERE match_id=? "
                "AND status='dispatched' AND dispatcher_token=? AND dispatcher_epoch=? "
                "AND execution_scope=? AND execution_state='recovery_pending'",
                (
                    match_id,
                    dispatcher_token,
                    int(dispatcher_epoch),
                    execution_scope,
                ),
            ).fetchone()
            if row is None:
                return {"outcome": "stale"}
            if str(row["execution_launch_state"] or "") == "creating":
                return {
                    "outcome": "awaiting_scope_observation",
                    "match_id": match_id,
                }
            table = self._match_table_of(c, match_id)
            match = (
                c.execute(f"SELECT status FROM {table} WHERE id=?", (match_id,)).fetchone()
                if table else None
            )
            ack_at = _now()
            if match is None or match["status"] == STATUS_PENDING:
                c.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
                c.execute("DELETE FROM matches_index WHERE id=?", (match_id,))
                c.execute("DELETE FROM match_rating_policies WHERE match_id=?", (match_id,))
                if match is not None and table is not None:
                    c.execute(f"DELETE FROM {table} WHERE id=?", (match_id,))
                c.execute(
                    "UPDATE auto_match_queue SET status='queued',match_id=NULL,"
                    "dispatcher_token=NULL,dispatcher_epoch=NULL,dispatched_at=NULL,"
                    "execution_scope=NULL,execution_backend=NULL,execution_state=NULL,"
                    "execution_launch_state=NULL,execution_launch_token=NULL,"
                    "execution_daemon_incarnation=NULL,"
                    "cleanup_requested_at=NULL,cleanup_ack_at=NULL,cleanup_error='' "
                    "WHERE id=?",
                    (int(row["id"]),),
                )
                c.execute(
                    "UPDATE auto_match_decisions SET lifecycle='queued',match_id=NULL,"
                    "dispatched_at=NULL,last_attempt_error='dispatcher_lost_before_start',"
                    "claim_dispatcher_token=NULL,claim_dispatcher_epoch=NULL,"
                    "execution_scope=NULL,execution_backend=NULL,execution_state=NULL,"
                    "execution_launch_state=NULL,execution_launch_token=NULL,"
                    "execution_daemon_incarnation=NULL,"
                    "cleanup_requested_at=NULL,cleanup_ack_at=NULL,cleanup_error='' "
                    "WHERE id=? AND lifecycle='dispatched'",
                    (int(row["decision_id"]),),
                )
                backoff = self._auto_backoff_tx(c, "dispatcher_lost_before_start")
                return {"outcome": "requeued_pending", "backoff": backoff}
            if match["status"] == STATUS_RUNNING:
                c.execute(
                    f"UPDATE {table} SET status=?,reason='orphan_after_restart',"
                    "ended_at=? WHERE id=? AND status=?",
                    (STATUS_ABORTED, ack_at, match_id, STATUS_RUNNING),
                )
            c.execute(
                "UPDATE auto_match_queue SET execution_state='cleanup_confirmed',"
                "cleanup_ack_at=?,cleanup_error='' WHERE id=?",
                (ack_at, int(row["id"])),
            )
            c.execute(
                "UPDATE auto_match_decisions SET execution_state='cleanup_confirmed',"
                "cleanup_ack_at=?,cleanup_error='' WHERE id=?",
                (ack_at, int(row["decision_id"])),
            )
            return {"outcome": "cleanup_confirmed", "match_id": match_id}

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

    def reconcile_auto_match_queue(
        self, *, dispatcher_token: str, dispatcher_epoch: int
    ) -> dict:
        """Converge terminal rows; service counters change exactly once."""
        removed_terminal = 0
        reset_missing = 0
        removed_invalid = 0
        waiting_settlement = 0
        recovery_pending = 0
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not self._auto_dispatcher_owned_tx(
                c, dispatcher_token, dispatcher_epoch
            ):
                return {"outcome": "not_leader"}
            def physical_cleanup_ready(row: sqlite3.Row) -> bool:
                nonlocal recovery_pending
                state = str(row["execution_state"] or "")
                if state == "cleanup_confirmed":
                    return True
                if str(row["execution_launch_state"] or "") == "unstarted":
                    ack_at = _now()
                    c.execute(
                        "UPDATE auto_match_queue SET execution_state='cleanup_confirmed',"
                        "cleanup_ack_at=?,cleanup_error='' WHERE id=?",
                        (ack_at, int(row["id"])),
                    )
                    c.execute(
                        "UPDATE auto_match_decisions SET execution_state='cleanup_confirmed',"
                        "cleanup_ack_at=?,cleanup_error='' WHERE id=?",
                        (ack_at, int(row["decision_id"])),
                    )
                    return True
                if state != "recovery_pending":
                    requested_at = _now()
                    c.execute(
                        "UPDATE auto_match_queue SET execution_state='recovery_pending',"
                        "cleanup_requested_at=?,cleanup_ack_at=NULL,"
                        "cleanup_error='terminal row awaits physical cleanup' WHERE id=?",
                        (requested_at, int(row["id"])),
                    )
                    c.execute(
                        "UPDATE auto_match_decisions SET execution_state='recovery_pending',"
                        "cleanup_requested_at=?,cleanup_ack_at=NULL,"
                        "cleanup_error='terminal row awaits physical cleanup' WHERE id=?",
                        (requested_at, int(row["decision_id"])),
                    )
                recovery_pending += 1
                return False

            for row in c.execute(
                "SELECT * FROM auto_match_queue ORDER BY id"
            ).fetchall():
                if row["status"] == "queued":
                    if self._auto_queue_identity_tx(c, row) is None:
                        self._auto_cancel_queue_row_tx(c, row, "identity_invalid")
                        removed_invalid += 1
                    continue
                if row["execution_state"] == "recovery_pending":
                    # A terminal/missing match row is not proof that an old
                    # Docker container has stopped.  Only explicit cleanup ack
                    # may advance this durable global-serial barrier.
                    recovery_pending += 1
                    continue
                match_id = str(row["match_id"] or "")
                tbl = self._match_table_of(c, match_id)
                if not tbl:
                    if not physical_cleanup_ready(row):
                        continue
                    c.execute(
                        "UPDATE auto_match_queue SET status='queued',match_id=NULL,"
                        "dispatcher_token=NULL,dispatcher_epoch=NULL,dispatched_at=NULL,"
                        "execution_scope=NULL,execution_backend=NULL,execution_state=NULL,"
                        "execution_launch_state=NULL,execution_launch_token=NULL,"
                        "execution_daemon_incarnation=NULL,"
                        "cleanup_requested_at=NULL,cleanup_ack_at=NULL,cleanup_error='' WHERE id=?",
                        (row["id"],),
                    )
                    c.execute(
                        "UPDATE auto_match_decisions SET lifecycle='queued',match_id=NULL,"
                        "dispatched_at=NULL,last_attempt_error='missing_match',"
                        "claim_dispatcher_token=NULL,claim_dispatcher_epoch=NULL,"
                        "execution_scope=NULL,execution_backend=NULL,execution_state=NULL,"
                        "execution_launch_state=NULL,execution_launch_token=NULL,"
                        "execution_daemon_incarnation=NULL,"
                        "cleanup_requested_at=NULL,cleanup_ack_at=NULL,cleanup_error='' WHERE id=?",
                        (row["decision_id"],),
                    )
                    self._auto_backoff_tx(c, "missing_match")
                    reset_missing += 1
                    continue
                match = c.execute(
                    f"SELECT status,reason,ended_at FROM {tbl} WHERE id=?", (match_id,)
                ).fetchone()
                if not match:
                    if not physical_cleanup_ready(row):
                        continue
                    c.execute(
                        "UPDATE auto_match_queue SET status='queued',match_id=NULL,"
                        "dispatcher_token=NULL,dispatcher_epoch=NULL,dispatched_at=NULL,"
                        "execution_scope=NULL,execution_backend=NULL,execution_state=NULL,"
                        "execution_launch_state=NULL,execution_launch_token=NULL,"
                        "execution_daemon_incarnation=NULL,"
                        "cleanup_requested_at=NULL,cleanup_ack_at=NULL,cleanup_error='' WHERE id=?",
                        (row["id"],),
                    )
                    c.execute(
                        "UPDATE auto_match_decisions SET lifecycle='queued',match_id=NULL,"
                        "dispatched_at=NULL,last_attempt_error='missing_match',"
                        "claim_dispatcher_token=NULL,claim_dispatcher_epoch=NULL,"
                        "execution_scope=NULL,execution_backend=NULL,execution_state=NULL,"
                        "execution_launch_state=NULL,execution_launch_token=NULL,"
                        "execution_daemon_incarnation=NULL,"
                        "cleanup_requested_at=NULL,cleanup_ack_at=NULL,cleanup_error='' WHERE id=?",
                        (row["decision_id"],),
                    )
                    self._auto_backoff_tx(c, "missing_match")
                    reset_missing += 1
                elif match["status"] in (STATUS_ABORTED, STATUS_COMPLETED) and not physical_cleanup_ready(row):
                    continue
                elif match["status"] == STATUS_ABORTED:
                    terminal_at = str(match["ended_at"] or _now())
                    reason = str(match["reason"] or "aborted")
                    c.execute(
                        "UPDATE auto_match_decisions SET lifecycle='aborted',"
                        "terminal_at=?,terminal_reason=? WHERE id=? "
                        "AND lifecycle='dispatched'",
                        (terminal_at, reason, row["decision_id"]),
                    )
                    c.execute("DELETE FROM auto_match_queue WHERE id=?", (row["id"],))
                    if reason in _AUTO_MATCH_PLATFORM_ABORT_REASONS:
                        self._auto_backoff_tx(c, reason)
                    removed_terminal += 1
                elif match["status"] == STATUS_COMPLETED:
                    settled = c.execute(
                        "SELECT settled_order FROM match_rating_settlements "
                        "WHERE match_id=?",
                        (match_id,),
                    ).fetchone()
                    if settled:
                        terminal_at = str(match["ended_at"] or _now())
                        decision = c.execute(
                            "SELECT * FROM auto_match_decisions WHERE id=?",
                            (row["decision_id"],),
                        ).fetchone()
                        changed = c.execute(
                            "UPDATE auto_match_decisions SET lifecycle='completed',"
                            "terminal_at=?,terminal_reason=?,settlement_order=? "
                            "WHERE id=? "
                            "AND lifecycle='dispatched'",
                            (
                                terminal_at,
                                str(match["reason"] or "completed"),
                                int(settled["settled_order"]),
                                row["decision_id"],
                            ),
                        )
                        if changed.rowcount == 1 and decision is not None:
                            self._auto_complete_service_tx(c, decision, terminal_at)
                            self._auto_reset_backoff_tx(c)
                        c.execute("DELETE FROM auto_match_queue WHERE id=?", (row["id"],))
                        removed_terminal += 1
                    else:
                        waiting_settlement += 1
            return {
                "outcome": "ok",
                "removed_terminal": removed_terminal,
                "reset_missing": reset_missing,
                "removed_invalid": removed_invalid,
                "waiting_settlement": waiting_settlement,
                "recovery_pending": recovery_pending,
            }

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
            identities = c.execute(
                "SELECT id,owner_id,game_id FROM bots WHERE id IN (?,?) ORDER BY id",
                (bot_a_id, bot_b_id),
            ).fetchall()
            by_id = {int(row["id"]): row for row in identities}
            if bot_a_id not in by_id or bot_b_id not in by_id:
                raise ValueError("Bot 不存在")
            if by_id[bot_a_id]["game_id"] != gid or by_id[bot_b_id]["game_id"] != gid:
                raise ValueError("Bot 与对局游戏不一致")
            if match_type == TYPE_CONTEST:
                rating_reason = "contest"
            elif match_type == TYPE_HUMAN:
                rating_reason = "human"
            elif bot_a_id == bot_b_id:
                rating_reason = "self_play"
            elif int(by_id[bot_a_id]["owner_id"]) == int(by_id[bot_b_id]["owner_id"]):
                rating_reason = "same_owner"
            else:
                rating_reason = "eligible"
            rated_pair = rating_reason == "eligible"
            config = dict(match_config or {})
            # Rating policy is internal and canonical; callers cannot override it.
            config["_rating_eligible"] = rated_pair
            config["_rating_reason"] = rating_reason
            mc_json = json.dumps(config, ensure_ascii=False)
            if rated_pair:
                # A dispatched row remains authoritative until an aborted terminal
                # or completed+settled terminal converges.  Starting another rated
                # match in that window could reorder Glicko snapshots.
                reserved = c.execute(
                    "SELECT 1 FROM auto_match_queue WHERE status='dispatched' "
                    "AND (? IN (bot_a_id,bot_b_id) OR ? IN (bot_a_id,bot_b_id)) "
                    "LIMIT 1",
                    (bot_a_id, bot_b_id),
                ).fetchone()
                if reserved:
                    raise ValueError("Bot 正在自动排位结算中，请稍后重试")
                if self._bot_has_active_rated_match_tx(c, bot_a_id, game_id=gid):
                    raise ValueError("座位 1 Bot 正在其他计分对局中")
                if self._bot_has_active_rated_match_tx(c, bot_b_id, game_id=gid):
                    raise ValueError("座位 2 Bot 正在其他计分对局中")
                # Foreground rated work has priority.  Remove any not-yet-dispatched
                # lookahead pairs that froze either Bot; the scheduler refills from
                # the new rating/last-service truth after this match completes.
                queued = c.execute(
                    "SELECT * FROM auto_match_queue WHERE status='queued' "
                    "AND (? IN (bot_a_id,bot_b_id) OR ? IN (bot_a_id,bot_b_id))",
                    (bot_a_id, bot_b_id),
                ).fetchall()
                for queue_row in queued:
                    self._auto_cancel_queue_row_tx(
                        c, queue_row, "foreground_rated_priority"
                    )
            created_at = _now()
            c.execute(
                f"INSERT INTO {tbl}(id, bot_a_id, bot_b_id, owner_id, "
                "contest_id, reason, match_type, status, game_id, match_config, "
                "human_user_id, human_seat, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    mc_json,
                    human_user_id,
                    human_seat,
                    created_at,
                ),
            )
            c.execute(
                "INSERT INTO match_rating_policies("
                "match_id,game_id,bot_a_id,bot_b_id,rated,rating_reason,source,"
                "classified_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    match_id,
                    gid,
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
        return _matches_table(row["game_id"])

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

    def get_match_detailed(self, match_id: str) -> dict | None:
        """get_match + JOIN bots(ba/bb 名/display) + users(owner 名/display)。
        统一观赛/回放页座位身份显示用（bot_a/bot_b 各含 name/display_name +
        owner_name/owner_display）。人类对局(match_type=human)时 bot_a_id==bot_b_id
        复用同一 bot 行——人类侧靠 human_seat 区分（api 层标 is_human）。
        """
        with self._tx() as c:
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            sel = (
                "m.*, "
                "ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
                "ua.username AS bot_a_owner_name, ua.display_name AS bot_a_owner_display, "
                "ub.username AS bot_b_owner_name, ub.display_name AS bot_b_owner_display, "
                "(SELECT mr.events_json FROM match_replays mr "
                "WHERE mr.match_id=m.id) AS _replay_events_json"
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
        *,
        auto_dispatcher_token: str | None = None,
        auto_dispatcher_epoch: int | None = None,
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
        fenced = auto_dispatcher_token is not None or auto_dispatcher_epoch is not None
        if fenced and (
            not auto_dispatcher_token or auto_dispatcher_epoch is None
        ):
            raise ValueError("auto-match write fence requires token and epoch")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            projection_guard = (
                self._rating_projection_mutation_guard_tx(c)
                if fields.get("status") == STATUS_COMPLETED
                else None
            )
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                if fenced:
                    raise AutoMatchFenceLost(
                        f"auto-match match disappeared for write {match_id}"
                    )
                return None
            match_config_row = c.execute(
                f"SELECT match_config FROM {tbl} WHERE id=?", (match_id,)
            ).fetchone()
            try:
                persisted_config = json.loads(
                    (match_config_row["match_config"] if match_config_row else "") or "{}"
                )
            except (TypeError, ValueError):
                persisted_config = {}
            if persisted_config.get("_auto_match_claim_epoch") and not fenced:
                raise AutoMatchFenceLost(
                    f"auto-match write requires frozen fence for match {match_id}"
                )
            if fenced:
                self._require_auto_match_fence_tx(
                    c,
                    match_id,
                    str(auto_dispatcher_token),
                    int(auto_dispatcher_epoch),
                    require_claim_fence=True,
                )
            if sets:
                vals.append(match_id)
                status = fields.get("status")
                status_guard = ""
                if fenced and status == STATUS_RUNNING:
                    status_guard = " AND status='pending'"
                elif fenced and status in (STATUS_COMPLETED, STATUS_ABORTED):
                    status_guard = " AND status IN ('pending','running')"
                changed = c.execute(
                    f"UPDATE {tbl} SET {','.join(sets)} WHERE id=?{status_guard}", vals
                )
                if fenced and changed.rowcount != 1:
                    raise AutoMatchFenceLost(
                        f"auto-match terminal/state CAS lost for match {match_id}"
                    )
                if fenced and status == STATUS_RUNNING:
                    c.execute(
                        "UPDATE auto_match_queue SET execution_state='running' "
                        "WHERE match_id=? AND execution_state='claimed'",
                        (match_id,),
                    )
                    c.execute(
                        "UPDATE auto_match_decisions SET execution_state='running' "
                        "WHERE match_id=? AND execution_state='claimed'",
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
            _delete_social_target(c, "match", match_id)
            cur = c.execute(f"DELETE FROM {tbl} WHERE id=?", (match_id,))
            deleted = cur.rowcount > 0
            if deleted:
                c.execute("DELETE FROM matches_index WHERE id=?", (match_id,))
                c.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
                c.execute(
                    "DELETE FROM match_rating_policies WHERE match_id=?", (match_id,)
                )
                auto_row = c.execute(
                    "SELECT * FROM auto_match_queue WHERE match_id=?", (match_id,)
                ).fetchone()
                if auto_row is not None:
                    self._auto_cancel_queue_row_tx(c, auto_row, "match_deleted")
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
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id"
            )
            sel = (
                "m.*, ba.name AS bot_a_name, bb.name AS bot_b_name, "
                "ba.display_name AS bot_a_display, "
                "bb.display_name AS bot_b_display, "
                "(SELECT mr.events_json FROM match_replays mr "
                "WHERE mr.match_id=m.id) AS _replay_events_json"
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
                "m.id, m.game_id, m.status, m.winner, m.likes_count, "
                "m.views_count, m.created_at, "
                "ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display"
            )
            join = (
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id"
            )
            where = "WHERE m.status='completed' AND m.likes_count > 0"
            subs = []
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                subs.append(f"SELECT {sel} FROM {tbl} m {join} {where}")
            union = " UNION ALL ".join(subs)
            sql = f"SELECT * FROM ({union}) ORDER BY likes_count DESC, views_count DESC LIMIT ?"
            return [_row(r) for r in c.execute(sql, (lim,))]

    # ── match_replays ─────────────────────────────────────────

    def upsert_replay(
        self,
        match_id: str,
        events_json: str = "[]",
        *,
        auto_dispatcher_token: str | None = None,
        auto_dispatcher_epoch: int | None = None,
    ) -> None:
        fenced = auto_dispatcher_token is not None or auto_dispatcher_epoch is not None
        if fenced and (
            not auto_dispatcher_token or auto_dispatcher_epoch is None
        ):
            raise ValueError("auto-match replay fence requires token and epoch")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            table = self._match_table_of(c, match_id)
            if table is not None:
                match_config_row = c.execute(
                    f"SELECT match_config FROM {table} WHERE id=?", (match_id,)
                ).fetchone()
                try:
                    persisted_config = json.loads(
                        (match_config_row["match_config"] if match_config_row else "")
                        or "{}"
                    )
                except (TypeError, ValueError):
                    persisted_config = {}
                if persisted_config.get("_auto_match_claim_epoch") and not fenced:
                    raise AutoMatchFenceLost(
                        f"auto-match replay requires frozen fence for match {match_id}"
                    )
            if fenced:
                self._require_auto_match_fence_tx(
                    c,
                    match_id,
                    str(auto_dispatcher_token),
                    int(auto_dispatcher_epoch),
                    require_claim_fence=True,
                )
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
            match = _parse_match_json_cols(
                _row(c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone())
            )
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
        auto_dispatcher_token: str | None = None,
        auto_dispatcher_epoch: int | None = None,
    ) -> bool:
        """在 Bot-vs-Bot 对局终态后原子替换整场调试批次。

        运行中和人类对局 fail closed；调用方的写入失败只应记录到私有日志，
        不得回滚已经提交的对局结果。单条/整场硬上限由 collector 与表 CHECK
        双重约束。
        """
        fenced = auto_dispatcher_token is not None or auto_dispatcher_epoch is not None
        if fenced and (
            not auto_dispatcher_token or auto_dispatcher_epoch is None
        ):
            raise ValueError("auto-match debug fence requires token and epoch")
        now = _now()
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if fenced:
                self._require_auto_match_fence_tx(
                    c,
                    match_id,
                    str(auto_dispatcher_token),
                    int(auto_dispatcher_epoch),
                    require_claim_fence=True,
                )
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

    def _require_auto_settlement_fence_tx(
        self,
        c: sqlite3.Connection,
        match_id: str,
        dispatcher_token: str | None,
        dispatcher_epoch: int | None,
    ) -> None:
        """Require the current leader for a frozen auto-match settlement.

        A takeover may adopt a completed match, so settlement validates the
        queue's current owner/epoch but deliberately does not require the
        original claim token recorded in the immutable decision/config.
        """
        table = self._match_table_of(c, match_id)
        if table is None:
            return
        match = c.execute(
            f"SELECT match_config FROM {table} WHERE id=?", (match_id,)
        ).fetchone()
        if match is None:
            return
        try:
            config = json.loads(match["match_config"] or "{}")
        except (TypeError, ValueError):
            config = {}
        if not config.get("_auto_match_claim_epoch"):
            return
        if not dispatcher_token or dispatcher_epoch is None:
            raise AutoMatchFenceLost(
                f"auto-match settlement lacks current fence for match {match_id}"
            )
        self._require_auto_match_fence_tx(
            c,
            match_id,
            dispatcher_token,
            int(dispatcher_epoch),
            require_claim_fence=False,
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
        auto_dispatcher_token: str | None = None,
        auto_dispatcher_epoch: int | None = None,
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
                self._require_auto_settlement_fence_tx(
                    c,
                    settlement_id,
                    auto_dispatcher_token,
                    auto_dispatcher_epoch,
                )
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
        *,
        auto_dispatcher_token: str | None = None,
        auto_dispatcher_epoch: int | None = None,
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
            self._require_auto_settlement_fence_tx(
                c,
                match_id,
                auto_dispatcher_token,
                auto_dispatcher_epoch,
            )
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
        placement_games: int | None = None,
    ) -> dict:
        """返回单一游戏的正式榜、定级区和紧凑概览。

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
                "WHERE b.is_active=1 AND b.format=? AND b.os=? AND b.arch=? "
                "AND b.game_id=?"
            )
            eligibility_params: tuple[Any, ...] = (
                SUPPORTED_BINARY_FORMAT,
                SUPPORTED_BINARY_OS,
                SUPPORTED_BINARY_ARCH,
                gid,
            )
            placement_required = (
                max(0, int(placement_games)) if placement_games is not None else 0
            )
            formal_condition = (
                f"r.matches_played >= {placement_required}"
                if placement_required > 0 else "1=1"
            )

            summary_row = c.execute(
                "SELECT COUNT(*) AS total, "
                f"SUM(CASE WHEN {formal_condition} THEN 1 ELSE 0 END) AS ranked, "
                f"SUM(CASE WHEN {formal_condition} THEN 0 ELSE 1 END) AS placement, "
                f"MAX(r.last_played_at) AS last_rated_at {eligibility_from}",
                eligibility_params,
            ).fetchone()
            summary = {
                "total": int(summary_row["total"] or 0),
                "ranked": int(summary_row["ranked"] or 0),
                "placement": int(summary_row["placement"] or 0),
                "last_rated_at": summary_row["last_rated_at"],
            }

            # last_rh 只接纳三处一致的 completed 对局。rating_history.reason 中
            # 的非 match 原因、错误 game_id 索引和缺失物理行都会被自然跳过。
            item_from = (
                f"FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "LEFT JOIN rating_history last_rh ON last_rh.id=("
                " SELECT rh.id FROM rating_history rh "
                " JOIN matches_index mi ON mi.id=rh.reason AND mi.game_id=rh.game_id "
                f" JOIN {match_table} lm ON lm.id=mi.id AND lm.game_id=mi.game_id "
                " WHERE rh.bot_id=r.bot_id AND rh.game_id=r.game_id AND lm.status=? "
                " AND (lm.bot_a_id=r.bot_id OR lm.bot_b_id=r.bot_id) "
                " ORDER BY rh.id DESC LIMIT 1"
                ") "
                "WHERE b.is_active=1 AND b.format=? AND b.os=? AND b.arch=? "
                "AND b.game_id=?"
            )
            item_params: tuple[Any, ...] = (
                STATUS_COMPLETED,
                *eligibility_params,
            )
            sel = (
                "SELECT r.bot_id, r.rating, r.rd, r.wins, r.losses, "
                "r.draws, r.matches_played, "
                "b.name AS bot_name, b.display_name AS bot_display, "
                "u.username AS owner_name, "
                "(SELECT rh.rating FROM rating_history rh "
                " WHERE rh.bot_id=r.bot_id AND rh.game_id=r.game_id "
                " ORDER BY rh.id DESC LIMIT 1 OFFSET 1) AS prev_rating, "
                "last_rh.reason AS last_match_id, "
                "last_rh.created_at AS last_match_at, "
                f"ROW_NUMBER() OVER (PARTITION BY CASE WHEN {formal_condition} "
                "THEN 1 ELSE 0 END ORDER BY r.rating DESC, r.matches_played DESC, "
                "r.bot_id ASC) AS group_rank "
            )
            # 正式榜排在定级中 Bot 之前；组内按 rating、场次、bot_id 稳定排序。
            # placement_required 来自代码配置并先转 int，不接受 SQL 输入。
            order = (
                f" ORDER BY ({formal_condition}) DESC, r.rating DESC, "
                "r.matches_played DESC, r.bot_id ASC"
            )
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                off = (max(1, int(page)) - 1) * pp
                sql = f"{sel}{item_from}{order} LIMIT ? OFFSET ?"
                rows = [_row(r) for r in c.execute(
                    sql, item_params + (pp, off)
                ).fetchall()]
            else:
                pp = max(1, min(limit, 200))
                sql = f"{sel}{item_from}{order} LIMIT ?"
                rows = [_row(r) for r in c.execute(
                    sql, item_params + (pp,)
                )]
            # 计算并补 tier + delta（应用层，避免 SQL 嵌套过深）
            # 段位 per-game：整个结果集已经钉死 gid，不从行数据猜游戏。
            from bzplat.backend.games import registry as _game_registry
            for row in rows:
                prev = row.pop("prev_rating", None)
                if prev is not None:
                    row["rating_delta"] = round(row["rating"] - prev, 2)
                else:
                    row["rating_delta"] = None
                t = _game_registry.tier_for(gid, row["rating"])
                row["tier_level"] = t.level
                row["tier_key"] = t.key
                row["tier_name"] = t.name
                played = max(0, int(row.get("matches_played") or 0))
                row["placement_required"] = placement_required
                row["placement_remaining"] = max(
                    0, placement_required - played
                )
                row["is_placement"] = (
                    placement_required > 0 and played < placement_required
                )
                group_rank = row.pop("group_rank", None)
                row["rank"] = (
                    None if row["is_placement"] else int(group_rank or 0)
                )

            result: dict[str, Any] = {
                "items": rows,
                "total": summary["total"],
                "summary": summary,
                "game_id": gid,
                "placement_required": placement_required,
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
        placement_games: int | None = None,
    ) -> list[dict]:
        """按陈旧度返回可对战 bot，供闲时自动对局挑选。

        - stale_since（秒，>0）：只返回 last_played_at 早于 now-stale_since 或从未赛（NULL）的 bot；
          None/0 = 不限。
        - placement_games（>0）：matches_played < 该值的「定级期」bot 排最前（新 bot 优先定级），
          其后按陈旧度（NULL 最前，再按时间升序）。
        仅返回 active+public+非内置且有二进制的 bot。
        """
        with self._tx() as c:
            sql = (
                "SELECT r.bot_id, r.rating, r.rd, r.matches_played, r.last_played_at, "
                "b.name AS bot_name, b.game_id, b.binary_path, b.is_active, b.is_builtin "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE b.is_active=1 AND b.is_builtin=0 "
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
            # 排序：定级期 bot 最前（若有），其后 NULL 最前、再按时间升序
            order = " ORDER BY "
            if placement_games and placement_games > 0:
                order += f"(r.matches_played < {int(placement_games)}) DESC, "
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

    def recover_orphan_matches(self) -> int:
        """启动时清理孤儿对局：把残留的 status=running（无对应内存协程）标 aborted。

        服务非正常退出后，DB 里 running 记录的内存 Task/Future 已丢失（尤其
        人类对局的 _human_turns），不清理会永久卡 running、泄漏并发与活跃用户计数。
        遍历三张 per-game 表清理。返回受影响行数。

        同时清理孤儿 pending 赛事对局：
        - 所有非 contest pending（challenge/table/ladder/human 等）：进程重启后
          已无对应内存 Task/Future，统一标 ``orphan_pending_after_restart`` aborted；
        - contest_id=NULL AND match_type='contest' AND status='pending'（e2e 残留等无主）；
        - contest 已终态（finished/cancelled）但仍 pending 的赛事对局（排期积压后赛事已结束，
          这些 pending 永不会被打，堵 orchestrator._tasks → auto_matcher 误判不空闲）。

        活跃赛事的 pending contest match 不在本方法粗暴标 aborted：已绑定/
        未绑定的两阶段派发中断由 ``reset_dead_contest_pairings`` 精确删除并重派。
        """
        from bzplat.backend.store.schema import STATUS_ABORTED

        with self._tx() as c:
            n = 0
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE status=? "
                    "AND (contest_id IS NULL OR contest_id NOT IN ("
                    "SELECT id FROM contests WHERE showcase_key IS NOT NULL)) "
                    "AND id NOT IN (SELECT match_id FROM auto_match_queue "
                    "WHERE match_id IS NOT NULL)",
                    (STATUS_RUNNING,),
                ).fetchone()
                cnt = int(row[0]) if row else 0
                if cnt:
                    c.execute(
                        f"UPDATE {tbl} SET status=?, reason='orphan_after_restart', "
                        "ended_at=datetime('now') WHERE status=? "
                        "AND (contest_id IS NULL OR contest_id NOT IN ("
                        "SELECT id FROM contests WHERE showcase_key IS NOT NULL)) "
                        "AND id NOT IN (SELECT match_id FROM auto_match_queue "
                        "WHERE match_id IS NOT NULL)",
                        (STATUS_ABORTED, STATUS_RUNNING),
                    )
                    n += cnt
                # 非赛事 pending 也依赖上一进程的内存 task/future；重启后
                # 不可能继续。contest pending 必须留给后续 pairing 对账精确恢复。
                non_contest_pending = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} "
                    "WHERE status=? AND match_type<>? "
                    "AND id NOT IN (SELECT match_id FROM auto_match_queue "
                    "WHERE match_id IS NOT NULL)",
                    (STATUS_PENDING, TYPE_CONTEST),
                ).fetchone()
                pending_count = int(non_contest_pending[0]) if non_contest_pending else 0
                if pending_count:
                    c.execute(
                        f"UPDATE {tbl} SET status=?, "
                        "reason='orphan_pending_after_restart', ended_at=? "
                        "WHERE status=? AND match_type<>? "
                        "AND id NOT IN (SELECT match_id FROM auto_match_queue "
                        "WHERE match_id IS NOT NULL)",
                        (
                            STATUS_ABORTED,
                            _now(),
                            STATUS_PENDING,
                            TYPE_CONTEST,
                        ),
                    )
                    n += pending_count
                # 清理孤儿 pending 赛事对局（无 contest 归属的 type=contest pending）
                row2 = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} "
                    f"WHERE status=? AND match_type=? "
                    f"AND contest_id IS NULL",
                    (STATUS_PENDING, TYPE_CONTEST),
                ).fetchone()
                cnt2 = int(row2[0]) if row2 else 0
                if cnt2:
                    c.execute(
                        f"UPDATE {tbl} SET status=?, reason='orphan_pending_no_contest', "
                        "ended_at=datetime('now') "
                        f"WHERE status=? AND match_type=? "
                        f"AND contest_id IS NULL",
                        (STATUS_ABORTED, STATUS_PENDING, TYPE_CONTEST),
                    )
                    n += cnt2
                # 清理已终态赛事的残留 pending 对局（赛事 finished/cancelled 但 match 仍 pending）
                row3 = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} m "
                    f"WHERE m.status=? AND m.contest_id IS NOT NULL "
                    f"AND m.contest_id IN (SELECT id FROM contests "
                    f"WHERE status IN (?,?) AND showcase_key IS NULL)",
                    (STATUS_PENDING, CONTEST_FINISHED, CONTEST_CANCELLED),
                ).fetchone()
                cnt3 = int(row3[0]) if row3 else 0
                if cnt3:
                    c.execute(
                        f"UPDATE {tbl} SET status=?, reason='contest_ended_pending_orphan', "
                        "ended_at=datetime('now') "
                        f"WHERE status=? AND contest_id IS NOT NULL "
                        f"AND contest_id IN (SELECT id FROM contests "
                        f"WHERE status IN (?,?) AND showcase_key IS NULL)",
                        (
                            STATUS_ABORTED,
                            STATUS_PENDING,
                            CONTEST_FINISHED,
                            CONTEST_CANCELLED,
                        ),
                    )
                    n += cnt3
            return n

    def reset_dead_contest_pairings(self) -> int:
        """启动对账辅助：清理两阶段派发中断留下的死状态。

        1. prepare match 已插入，但进程在 bind pairing 前退出：活跃赛事中会留下
           没有任何 pairing 引用的 pending contest match。这类幽灵对局必须连同
           物理 match 行、matches_index 和 replay 在同一事务内删除。
        2. contest_pairings 里 status='running' 但对应 match 已终态非
           completed（aborted/orphan/pending 或不存在）：复位为 pending +
           match_id=NULL，供 ContestManager.maybe_finish/_dispatch_pending 重派。

        completed 的 pairing 不动（保留真实比赛结果，防误伤）。
        对应 recover_orphan_matches 把 running match 标 aborted 后的赛事善后——
        那些赛事 pairing 仍指 aborted match（_stage_done 不通过 pairing 状态判，而是
        读 match.status，但 _dispatch_pending 只挑 status=pending 且无 match_id 的重派，
        所以 status=running+match_id=aborted 的死 pairing 永远不会被重派 → 赛事卡死）。
        返回重置行数。
        """
        # pairing bind 已提交、runner 尚未 start 时进程可能退出：该 match 仍 pending。
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
            # pairing.match_id 指向它。这里只在启动对账入口调用，内存中已无
            # 可能继续 bind 的 prepared map，因此删除是唯一可恢复收敛。
            for gid in _all_game_ids():
                table = _matches_table(gid)
                ghosts = c.execute(
                    f"SELECT m.id FROM {table} m "
                    "JOIN contests contest ON contest.id=m.contest_id "
                    "WHERE m.status=? AND m.match_type=? "
                    f"AND contest.status IN ({status_marks}) "
                    "AND contest.showcase_key IS NULL "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM contest_pairings pairing WHERE pairing.match_id=m.id"
                    ")",
                    (STATUS_PENDING, TYPE_CONTEST, *active_statuses),
                ).fetchall()
                for ghost in ghosts:
                    match_id = str(ghost["id"])
                    _delete_social_target(c, "match", match_id)
                    c.execute(f"DELETE FROM {table} WHERE id=?", (match_id,))
                    c.execute("DELETE FROM matches_index WHERE id=?", (match_id,))
                    c.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
                    c.execute(
                        "DELETE FROM match_rating_policies WHERE match_id=?",
                        (match_id,),
                    )
                    c.execute(
                        "DELETE FROM auto_match_queue WHERE match_id=?",
                        (match_id,),
                    )
                    recovered += 1

            pairings = c.execute(
                "SELECT pairing.id, pairing.match_id FROM contest_pairings pairing "
                "JOIN contests contest ON contest.id=pairing.contest_id "
                "WHERE pairing.status=? AND pairing.match_id IS NOT NULL "
                "AND contest.showcase_key IS NULL",
                (STATUS_RUNNING,),
            ).fetchall()
            for pairing in pairings:
                match_id = str(pairing["match_id"])
                indexed = c.execute(
                    "SELECT game_id FROM matches_index WHERE id=?", (match_id,)
                ).fetchone()
                table = _matches_table(indexed["game_id"]) if indexed else None
                match = (
                    c.execute(
                        f"SELECT status FROM {table} WHERE id=?", (match_id,)
                    ).fetchone()
                    if table else None
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
                    c.execute(
                        "DELETE FROM auto_match_queue WHERE match_id=?",
                        (match_id,),
                    )
                elif match["status"] == STATUS_RUNNING:
                    c.execute(
                        f"UPDATE {table} SET status=?, reason='orphan_after_restart', "
                        "ended_at=? WHERE id=? AND status=?",
                        (STATUS_ABORTED, _now(), match_id, STATUS_RUNNING),
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
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO notifications(user_id, type, title, body, link, "
                "is_read, created_at) VALUES(?,?,?,?,?,?,?)",
                (user_id, type, title, body, link, 0, _now()),
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
            cur = c.execute(
                "UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?",
                (notif_id, user_id),
            )
            return cur.rowcount > 0

    def mark_all_notifications_read(self, user_id: int) -> int:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0",
                (user_id,),
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
        require_real_name: int = 0,
    ) -> dict:
        validate_contest_times(
            registration_opens_at, registration_closes_at, starts_at
        )
        gid = _registered_game_id(game_id)
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO contests(title, description, organizer_id, status, "
                "registration_opens_at, registration_closes_at, starts_at, "
                "ends_at, created_at, game_id, stages_json, "
                "current_stage_idx, template_id, phase, "
                "source_contest_id, require_real_name) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    stages_json,
                    current_stage_idx,
                    template_id,
                    phase,
                    source_contest_id,
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
        sets = [f"{k}=?" for k in clean]
        vals = list(clean.values())
        with self._tx() as c:
            current = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not current:
                return None
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
        allowed = {"title", "starts_at"}
        unknown = set(fields).difference(allowed)
        if unknown:
            raise ValueError(
                f"published 赛事不能修改字段: {', '.join(sorted(unknown))}"
            )
        plans = sorted(
            (
                int(row["id"]),
                int(row.get("round_num") or 1),
                row.get("scheduled_at"),
            )
            for row in pending_pairing_schedules
        )
        if len({pairing_id for pairing_id, _round, _schedule in plans}) != len(plans):
            raise ValueError("published 对阵重排计划包含重复 ID")

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            current = c.execute(
                "SELECT * FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not current:
                return None
            if current["status"] != CONTEST_PUBLISHED:
                raise ValueError("仅排期已发布赛事可以重排待开赛对局")
            if int(current["current_stage_idx"] or 0) != int(stage_idx):
                raise ValueError("赛事当前阶段已变化，拒绝重排")
            if c.execute(
                "SELECT 1 FROM contest_pairings "
                "WHERE contest_id=? AND match_id IS NOT NULL LIMIT 1",
                (contest_id,),
            ).fetchone():
                raise ValueError("赛事已有对局被派发，不能修改比赛开始时间")

            pending = c.execute(
                "SELECT id, round_num FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? AND status=? "
                "AND match_id IS NULL ORDER BY id",
                (contest_id, stage_idx, STATUS_PENDING),
            ).fetchall()
            current_shape = sorted(
                (int(row["id"]), int(row["round_num"] or 1)) for row in pending
            )
            expected_shape = [
                (pairing_id, round_num)
                for pairing_id, round_num, _schedule in plans
            ]
            if current_shape != expected_shape:
                raise ValueError("published 对阵在重排期间已变化，拒绝覆盖")

            validate_contest_times(
                current["registration_opens_at"],
                current["registration_closes_at"],
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
    ) -> list[dict] | dict:
        """列赛事，并在分页 SQL 内完成隐藏状态的可见性过滤。

        ``exclude_statuses`` 非空时，匿名/普通用户（``hidden_owner_id=None``）
        始终排除这些状态，即使同时传了显式 ``status`` 也不能绕过。
        组织者传自己的 user id，则可额外看到“自己主办”的隐藏赛事，
        不会因 organizer 角色而看到他人草稿/已取消赛事。admin 调用方
        不传 ``exclude_statuses`` 即保持全见。条件必须在 SQL 分页前应用，
        不得拉取一页后再用 Python 裁剪（会使 total/页数泄漏且错位）。
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
                pp = max(1, min(200, int(per_page)))
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

    # ── contest_entries ───────────────────────────────────────

    def add_entry(self, contest_id: int, user_id: int, bot_id: int) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO contest_entries(contest_id, user_id, bot_id, "
                "registered_at) VALUES(?,?,?,?)",
                (contest_id, user_id, bot_id, _now()),
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
            contest = c.execute(
                "SELECT status FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not contest or contest["status"] != CONTEST_OPEN:
                raise ValueError("比赛未开放报名")
            cur = c.execute(
                "INSERT INTO contest_entries(contest_id, user_id, bot_id, registered_at) "
                "VALUES(?,?,?,?) ON CONFLICT(contest_id, user_id) DO NOTHING",
                (contest_id, user_id, bot_id, _now()),
            )
            if cur.rowcount != 1:
                raise ValueError("该用户在此比赛中已报名")
            return _row(
                c.execute(
                    "SELECT * FROM contest_entries WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def add_contest_roster_entries(
        self, contest_id: int, entries: list[tuple[int, int]]
    ) -> tuple[list[dict], list[int]]:
        """组织者/admin 批量新增名册；状态复核与整批写入同一事务。"""
        with self._tx() as c:
            contest = c.execute(
                "SELECT status FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            added: list[dict] = []
            skipped: list[int] = []
            for user_id, bot_id in entries:
                cur = c.execute(
                    "INSERT INTO contest_entries(contest_id, user_id, bot_id, registered_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(contest_id, user_id) DO NOTHING",
                    (contest_id, user_id, bot_id, _now()),
                )
                if cur.rowcount != 1:
                    skipped.append(user_id)
                    continue
                added.append(_row(c.execute(
                    "SELECT * FROM contest_entries WHERE id=?", (cur.lastrowid,)
                ).fetchone()))
            return added, skipped

    def delete_contest_roster_entry(self, contest_id: int, user_id: int) -> bool:
        """组织者/admin 删除名册；状态复核与 DELETE 同一事务。"""
        with self._tx() as c:
            contest = c.execute(
                "SELECT status FROM contests WHERE id=?", (contest_id,)
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
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
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
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO contest_pairings(contest_id, round_num, entry_a_id, "
                "entry_b_id, bot_a_id, bot_b_id, bot_a_version_id, bot_b_version_id, "
                "pairing_seed, published_at, scheduled_at, match_id, status, stage_idx, "
                "stage_key, group_id, bracket_slot, color_first) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                ),
            )
            pid = cur.lastrowid
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pid,)
                ).fetchone()
            )

    add_contest_pairing = add_pairing

    def create_contest_stage_pairings(
        self,
        contest_id: int,
        stage_idx: int,
        pairing_rows: list[dict[str, Any]],
        *,
        expected_current_stage_idx: int,
        activate_running: bool = False,
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
        """
        if not pairing_rows:
            raise ValueError("赛事阶段对阵批次不能为空")
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
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status, current_stage_idx FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] in (CONTEST_FINISHED, CONTEST_CANCELLED):
                raise ValueError("终态赛事不能生成新阶段对阵")
            current_idx = int(contest["current_stage_idx"] or 0)
            if current_idx != int(expected_current_stage_idx):
                raise ValueError("赛事当前阶段已变化，拒绝重复生成对阵")
            if stage_idx not in (current_idx, current_idx + 1):
                raise ValueError("赛事阶段只能生成当前阶段或紧邻的下一阶段")

            existing = c.execute(
                "SELECT id, match_id, status, bot_b_id FROM contest_pairings "
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
                        and row["bot_b_id"] is not None
                    )
                    for row in existing
                ):
                    raise ValueError("下一阶段已有运行进度，不能覆盖")
                c.execute(
                    "DELETE FROM contest_pairings WHERE contest_id=? AND stage_idx=?",
                    (contest_id, stage_idx),
                )

            inserted: list[dict] = []
            placeholders = ",".join("?" for _ in columns)
            for source in pairing_rows:
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

            if activate_running:
                c.execute(
                    "UPDATE contests SET status=?, current_stage_idx=?, "
                    "rest_ends_at=NULL WHERE id=?",
                    (CONTEST_RUNNING, stage_idx, contest_id),
                )
            elif stage_idx != current_idx:
                c.execute(
                    "UPDATE contests SET current_stage_idx=? WHERE id=?",
                    (stage_idx, contest_id),
                )
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
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status, current_stage_idx FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] != CONTEST_RUNNING:
                raise ValueError("仅运行中的赛事可追加后续轮次")
            if int(contest["current_stage_idx"] or 0) != int(
                expected_current_stage_idx
            ):
                raise ValueError("赛事当前阶段已变化，拒绝追加轮次")
            if int(stage_idx) != int(expected_current_stage_idx):
                raise ValueError("只能向赛事当前阶段追加轮次")

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
            for source in pairing_rows:
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
            return inserted

    def list_pairings(
        self, contest_id: int, *, stage_idx: int | None = None
    ) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM contest_pairings WHERE contest_id=?"
            params: list[Any] = [contest_id]
            if stage_idx is not None:
                sql += " AND stage_idx=?"
                params.append(stage_idx)
            sql += " ORDER BY stage_idx, round_num, id"
            return [_row(r) for r in c.execute(sql, params)]

    list_contest_pairings = list_pairings

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
        expected_ids = sorted({int(pairing_id) for pairing_id in expected_existing_ids})
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
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status, current_stage_idx FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest or contest["status"] != CONTEST_PUBLISHED:
                raise ValueError("published 赛事状态已变化，拒绝重建对阵")
            if int(contest["current_stage_idx"] or 0) != int(stage_idx):
                raise ValueError("published 赛事当前阶段已变化，拒绝重建对阵")

            current = c.execute(
                "SELECT id, match_id, status, bot_b_id FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? ORDER BY id",
                (contest_id, stage_idx),
            ).fetchall()
            current_ids = [int(row["id"]) for row in current]
            if current_ids != expected_ids:
                raise ValueError("published 对阵在恢复期间已变化，拒绝覆盖")
            if any(row["match_id"] is not None for row in current):
                raise ValueError("published 对阵已绑定对局，不能自动重建")
            if any(
                row["status"] not in (STATUS_PENDING, STATUS_COMPLETED)
                or (row["status"] == STATUS_COMPLETED and row["bot_b_id"] is not None)
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
            for source in pairing_rows:
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
            return inserted

    def contest_bracket(self, contest_id: int) -> list[dict]:
        """返回对阵（带 bot 名/owner 名 + 对局 winner），便于前端画对阵图。

        每行含 pairing 全字段 + bot_a_name/bot_a_display/bot_b_name/bot_b_display
        + owner_a_name/owner_b_name + winner（从 matches 取）。
        """
        with self._tx() as c:
            # 赛事绑定单一游戏——取其 game_id 定位 per-game 对局表 join winner
            ct = c.execute(
                "SELECT game_id FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            gid = _registered_game_id(ct["game_id"] if ct else None)
            tbl = _matches_table(gid)
            rows = c.execute(
                "SELECT p.*, ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
                "ua.username AS owner_a_name, ub.username AS owner_b_name, "
                "m.winner AS match_winner "
                "FROM contest_pairings p "
                "LEFT JOIN bots ba ON p.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON p.bot_b_id=bb.id "
                "LEFT JOIN users ua ON ba.owner_id=ua.id "
                "LEFT JOIN users ub ON bb.owner_id=ub.id "
                f"LEFT JOIN {tbl} m ON p.match_id=m.id "
                "WHERE p.contest_id=? "
                "ORDER BY p.stage_idx, p.round_num, p.id",
                (contest_id,),
            ).fetchall()
            return [_row(r) for r in rows]

    def contest_entries_named(
        self, contest_id: int, *, page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        """返回报名（带 bot 名/owner 名 + seed/group/eliminated + 实名信息）。

        LEFT JOIN bots：bot_id 现可为 NULL（删 bot 后保留 entry，P0 SET NULL）。
        实名字段（real_name/phone/school/student_id）随行返回——**调用方（api 层）负责
        对非组织者脱敏**（contest_detail 仅组织者可见；export 端点组织者 gated）。
        ``page`` 为 None 时返回 list（旧契约）；给定时返回分页 dict。
        """
        with self._tx() as c:
            sql = (
                "SELECT e.*, b.name AS bot_name, b.display_name AS bot_display, "
                "b.game_id, u.username AS username, u.username AS owner_name, "
                "u.display_name AS owner_display, "
                "u.real_name, u.phone, u.school, u.student_id "
                "FROM contest_entries e "
                "LEFT JOIN bots b ON e.bot_id=b.id "
                "LEFT JOIN users u ON e.user_id=u.id "
                "WHERE e.contest_id=? ORDER BY e.seed, e.registered_at"
            )
            params = (contest_id,)
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, params, page=page, per_page=pp)
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
            return [_row(r) for r in c.execute(sql, params).fetchall()]

    def list_contest_export(self, contest_id: int) -> list[dict]:
        """合并导出：一行 per 报名者 = 报名信息（实名）+ 结果排名 + 战绩。

        LEFT JOIN official_results：未完赛/未出排名者 rank/points 列为 NULL（仍出现）。
        stage_results 取末阶段（official_results.stage_idx）。供组织者导出 CSV。
        """
        with self._tx() as c:
            rows = c.execute(
                "SELECT e.id AS entry_id, e.seed, e.group_id, e.eliminated, e.registered_at, "
                "u.username AS owner_name, u.display_name AS owner_display, "
                "u.real_name, u.phone, u.school, u.student_id, "
                "b.name AS bot_name, b.display_name AS bot_display, "
                "r.rank, r.points, r.awarded, r.stage_idx, "
                "sr.wins, sr.draws, sr.losses, sr.delta_total "
                "FROM contest_entries e "
                "LEFT JOIN users u ON e.user_id=u.id "
                "LEFT JOIN bots b ON e.bot_id=b.id "
                "LEFT JOIN contest_official_results r "
                "  ON r.entry_id=e.id AND r.contest_id=e.contest_id "
                "LEFT JOIN contest_stage_results sr "
                "  ON sr.entry_id=e.id AND sr.contest_id=e.contest_id "
                "  AND sr.stage_idx=r.stage_idx "
                "WHERE e.contest_id=? "
                "ORDER BY CASE WHEN r.rank IS NULL THEN 999999 ELSE r.rank END, e.seed",
                (contest_id,),
            ).fetchall()
            return [_row(r) for r in rows]

    def update_pairing(self, pairing_id: int, **fields: Any) -> dict | None:
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
            "pairing_seed",
            "published_at",
            "scheduled_at",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
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

    def bind_contest_pairing_match(
        self,
        contest_id: int,
        pairing_id: int,
        match_id: str,
        *,
        activate_running: bool = False,
    ) -> dict:
        """原子绑定 prepared match，并可在同一事务把 published 赛事转 running。

        只接受仍属该赛事、仍为 pending 且 ``match_id IS NULL`` 的 pairing；这样
        challenge 准备成功后若绑定/提交失败，调用方可安全删除尚未启动的 match，
        不会留下 pairing 与 contest 状态的半提交。
        """
        with self._tx() as c:
            contest = c.execute(
                "SELECT status FROM contests WHERE id=?", (contest_id,)
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
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing_id,)
                ).fetchone()
            )

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
            table = self._match_table_of(c, match_id)
            if not table:
                return None
            match = c.execute(
                f"SELECT status, contest_id FROM {table} WHERE id=?", (match_id,)
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
                    CONTEST_FINISHED,
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
                    parsed = datetime.fromisoformat(str(match["started_at"]))
                except ValueError:
                    logger.error(
                        "contest %s match %s has invalid started_at",
                        contest_id,
                        match_id,
                    )
                    continue
                candidates.append((parsed, str(match["started_at"])))

            if not candidates:
                return None
            _, actual = min(candidates, key=lambda item: item[0])
            actual_dt = datetime.fromisoformat(actual)
            closes = contest["registration_closes_at"]
            ends = contest["ends_at"]
            try:
                if closes and datetime.fromisoformat(str(closes)) > actual_dt:
                    return None
                if ends and actual_dt > datetime.fromisoformat(str(ends)):
                    return None
            except ValueError:
                return None
            cur = c.execute(
                "UPDATE contests SET starts_at=? "
                "WHERE id=? AND starts_at IS NULL "
                "AND status IN (?,?,?)",
                (
                    actual,
                    contest_id,
                    CONTEST_RUNNING,
                    CONTEST_REST,
                    CONTEST_FINISHED,
                ),
            )
            return actual if cur.rowcount == 1 else None

    def unbind_prepared_contest_match(
        self,
        contest_id: int,
        pairing_id: int,
        match_id: str,
        *,
        restore_published: bool = False,
    ) -> bool:
        """prepared match 启动失败时精确撤销刚完成的 pairing 绑定。"""
        with self._tx() as c:
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
            table = self._match_table_of(c, match_id)
            if not table:
                return None
            match = c.execute(
                f"SELECT status, contest_id FROM {table} WHERE id=?", (match_id,)
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

    def list_stage_results(
        self, contest_id: int, *, stage_idx: int | None = None
    ) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM contest_stage_results WHERE contest_id=?"
            params: list[Any] = [contest_id]
            if stage_idx is not None:
                sql += " AND stage_idx=?"
                params.append(stage_idx)
            sql += " ORDER BY stage_idx, points DESC, delta_total DESC"
            return [_row(r) for r in c.execute(sql, params)]

    # ── contest_official_results（P2 全员正式名次）─────────────

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
            if not c.execute(
                "SELECT 1 FROM contests WHERE id=?", (contest_id,)
            ).fetchone():
                raise ValueError("赛事不存在")
            c.execute(
                "DELETE FROM contest_official_results WHERE contest_id=?",
                (contest_id,),
            )
            for row in result_rows:
                c.execute(
                    "INSERT INTO contest_official_results"
                    "(contest_id, entry_id, stage_idx, rank, points, bot_id, user_id, "
                    "tiebreaks_json, awarded) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        contest_id,
                        row["entry_id"],
                        int(row.get("stage_idx") or 0),
                        row["rank"],
                        row.get("points") or 0,
                        row.get("bot_id"),
                        row.get("user_id"),
                        row.get("tiebreaks_json") or "{}",
                        row.get("awarded") or "",
                    ),
                )
            c.execute(
                "UPDATE contests SET official_results_ready=1 WHERE id=?",
                (contest_id,),
            )

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
        tiebreaks_json: str = "{}",
        awarded: str = "",
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO contest_official_results"
                "(contest_id, entry_id, stage_idx, rank, points, bot_id, user_id, "
                "tiebreaks_json, awarded) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(contest_id, entry_id) DO UPDATE SET "
                "stage_idx=excluded.stage_idx, rank=excluded.rank, "
                "points=excluded.points, bot_id=excluded.bot_id, "
                "user_id=excluded.user_id, tiebreaks_json=excluded.tiebreaks_json, "
                "awarded=excluded.awarded",
                (
                    contest_id, entry_id, stage_idx, rank, points, bot_id, user_id,
                    tiebreaks_json, awarded,
                ),
            )

    def list_official_results(self, contest_id: int) -> list[dict]:
        """全员正式名次（按 rank 升序，1..N 唯一连续）。"""
        with self._tx() as c:
            rows = c.execute(
                "SELECT r.*, b.name AS bot_name, b.display_name AS bot_display, "
                "u.username AS owner_name, u.display_name AS owner_display "
                "FROM contest_official_results r "
                "LEFT JOIN bots b ON r.bot_id=b.id "
                "LEFT JOIN users u ON r.user_id=u.id "
                "WHERE r.contest_id=? ORDER BY r.rank",
                (contest_id,),
            ).fetchall()
            return [_row(r) for r in rows]

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
        """Return the v2 singleton switch (independent from legacy settings)."""
        with self._tx() as c:
            row = c.execute(
                "SELECT enabled FROM auto_match_control WHERE singleton=1"
            ).fetchone()
            # SCHEMA always seeds the singleton.  Missing/corrupt state fails closed.
            return bool(row and int(row["enabled"]) == 1)

    def set_auto_match_enabled(self, enabled: bool) -> bool:
        if type(enabled) is not bool:  # bool is deliberately strict at Store boundary.
            raise ValueError("自动排位总开关必须是布尔值")
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            changed = c.execute(
                "UPDATE auto_match_control SET enabled=?,updated_at=? WHERE singleton=1",
                (1 if enabled else 0, _now()),
            )
            if changed.rowcount != 1:
                raise RuntimeError("自动排位控制单例缺失")
        return enabled

    def get_auto_match_fair_state(self) -> dict:
        with self._tx() as c:
            row = c.execute(
                "SELECT next_game_idx,next_lane,revision,platform_failures,"
                "not_before,updated_at FROM auto_match_fair_state WHERE singleton=1"
            ).fetchone()
            return dict(row) if row else {}

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
                "SELECT s.*, u.username FROM sessions s "
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
        """原子拒绝会破坏活跃对局/赛事的管理员用户硬删。

        删除用户会经 ``users → bots`` 级联，不能只依赖 Bot 删除端点的保护。
        本方法在 ``BEGIN IMMEDIATE`` 事务内先汇总该用户全部 Bot 的活跃引用及
        其组织的赛事，再决定是否删除；这样另一个连接也不能在检查和 DELETE
        之间插入新的引用。完成态历史仍按 schema 的 SET NULL/CASCADE 契约保留。

        返回 ``found/deleted/bot_ids/blockers``；成功时调用方可用删除前保存的
        ``bot_ids`` 清理对应上传目录。
        """
        active_contest_statuses = (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
            CONTEST_REST,
        )
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
                    "WHERE status IN (?,?) AND ("
                    "bot_a_id IN (SELECT id FROM bots WHERE owner_id=?) OR "
                    "bot_b_id IN (SELECT id FROM bots WHERE owner_id=?) OR "
                    "owner_id=? OR human_user_id=?)",
                    (
                        STATUS_PENDING,
                        STATUS_RUNNING,
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                    ),
                ).fetchone()
                match_count += int(row["n"] if row else 0)

            status_marks = ",".join("?" for _ in active_contest_statuses)
            pairing_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_pairings cp "
                "JOIN contests contest ON contest.id=cp.contest_id "
                f"WHERE contest.status IN ({status_marks}) AND ("
                "cp.bot_a_id IN (SELECT id FROM bots WHERE owner_id=?) OR "
                "cp.bot_b_id IN (SELECT id FROM bots WHERE owner_id=?))",
                (*active_contest_statuses, user_id, user_id),
            ).fetchone()
            entry_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_entries entry "
                "JOIN contests contest ON contest.id=entry.contest_id "
                f"WHERE contest.status IN ({status_marks}) AND (entry.user_id=? OR "
                "entry.bot_id IN (SELECT id FROM bots WHERE owner_id=?))",
                (*active_contest_statuses, user_id, user_id),
            ).fetchone()
            organized_row = c.execute(
                "SELECT COUNT(*) AS n FROM contests WHERE organizer_id=?", (user_id,)
            ).fetchone()
            blockers = {
                "matches": match_count,
                "contest_pairings": int(pairing_row["n"] if pairing_row else 0),
                "contest_entries": int(entry_row["n"] if entry_row else 0),
                "organized_contests": int(organized_row["n"] if organized_row else 0),
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
            _delete_comment_likes_for(c, "user_id=?", (user_id,))
            _delete_user_likes(c, user_id)
            for bot_id in bot_ids:
                _delete_social_target(c, "bot", bot_id)
            deleted = c.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount > 0
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
