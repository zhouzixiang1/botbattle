"""Offline, deterministic rating projection audit and rebuild.

The source of truth is the immutable match rating policy plus the monotonic
settlement order.  Dry-run and verify open SQLite read-only.  Applying a rebuild
is intentionally exposed only through the guarded CLI maintenance command.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from bzplat.backend.rating.glicko2 import Rating, match_scores, update_rating
from bzplat.backend.store.schema import (
    MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    VALID_GAME_IDS,
)

CURRENT_POLICY_VERSION = "owner-neutral-v2"
_RATING_FIELDS = (
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


@dataclass(frozen=True)
class RebuildPlan:
    source: list[dict[str, Any]]
    ratings: list[dict[str, Any]]
    history: list[dict[str, Any]]
    pairs: list[dict[str, Any]]
    settlements: list[dict[str, Any]]
    report: dict[str, Any]


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _source_semantic(source: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return every immutable input that can affect replay or its audit trail."""
    fields = (
        "settled_order",
        "settled_at",
        "match_id",
        "game_id",
        "bot_a_id",
        "bot_b_id",
        "rated",
        "rating_reason",
        "winner",
        "delta_a",
        "delta_b",
        "ended_at",
    )
    return [{key: row[key] for key in fields} for row in source]


def _tables(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }


def _validate_schema(conn: sqlite3.Connection) -> None:
    required = {
        "bots",
        "ratings",
        "rating_history",
        "pair_stats",
        "match_rating_policies",
        "match_rating_settlements",
        "rating_projection_state",
    }
    missing = sorted(required - _tables(conn))
    if missing:
        raise RuntimeError(f"评分重建 schema 尚未迁移，缺少: {missing}")
    for table, columns in (
        (
            "match_rating_policies",
            {"game_id", "bot_a_id", "bot_b_id", "rated", "rating_reason"},
        ),
        ("match_rating_settlements", {"settled_order"}),
    ):
        actual = {
            str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")
        }
        absent = sorted(columns - actual)
        if absent:
            raise RuntimeError(f"{table} 缺少评分重建列: {absent}")


def _load_source(conn: sqlite3.Connection) -> tuple[list[dict[str, Any]], list[str]]:
    issues: list[str] = []
    policies = {
        str(row["match_id"]): dict(row)
        for row in conn.execute(
            "SELECT * FROM match_rating_policies"
        )
    }
    settlements = [
        dict(row)
        for row in conn.execute(
            "SELECT match_id,settled_at,settled_order "
            "FROM match_rating_settlements WHERE settled_order>0 "
            "ORDER BY settled_order"
        )
    ]
    source: list[dict[str, Any]] = []
    seen_orders: set[int] = set()
    for settlement in settlements:
        match_id = str(settlement["match_id"])
        order = int(settlement["settled_order"])
        if order in seen_orders:
            issues.append(f"重复 settled_order={order}")
            continue
        seen_orders.add(order)
        policy = policies.get(match_id)
        if policy is None:
            issues.append(f"settlement 缺 rating policy: {match_id}")
            continue
        game_id = str(policy.get("game_id") or "")
        if game_id not in VALID_GAME_IDS:
            issues.append(f"rating policy 游戏非法: {match_id} game={game_id!r}")
            continue
        table = f"matches_{game_id}"
        match = conn.execute(
            f"SELECT id,status,winner,result,ended_at,created_at,match_type "
            f"FROM {table} WHERE id=?",
            (match_id,),
        ).fetchone()
        if match is None:
            issues.append(f"settlement 对局缺失: {match_id}")
            continue
        if str(match["status"]) != STATUS_COMPLETED:
            issues.append(
                f"settlement 对局不是 completed: {match_id} status={match['status']}"
            )
            continue
        try:
            result = json.loads(match["result"] or "{}")
        except (TypeError, ValueError):
            result = {}
        rated = bool(int(policy["rated"]))
        deltas = result.get("deltas")
        if rated and (
            not isinstance(deltas, list)
            or len(deltas) < 2
            or policy.get("bot_a_id") is None
            or policy.get("bot_b_id") is None
        ):
            issues.append(f"rated settlement 缺 Bot/结果输入: {match_id}")
            continue
        source.append(
            {
                "settled_order": order,
                "settled_at": str(settlement["settled_at"]),
                "match_id": match_id,
                "game_id": game_id,
                "bot_a_id": policy.get("bot_a_id"),
                "bot_b_id": policy.get("bot_b_id"),
                "rated": rated,
                "rating_reason": str(policy["rating_reason"]),
                "winner": int(match["winner"]) if match["winner"] in (0, 1) else None,
                "delta_a": int(deltas[0]) if rated else 0,
                "delta_b": int(deltas[1]) if rated else 0,
                "ended_at": str(match["ended_at"] or match["created_at"] or ""),
            }
        )

    # A stopped maintenance window must not omit a completed match merely
    # because settlement crashed just before its marker transaction.
    settled_ids = {str(row["match_id"]) for row in settlements}
    for game_id in sorted(VALID_GAME_IDS):
        table = f"matches_{game_id}"
        rows = conn.execute(
            f"SELECT m.id FROM {table} m "
            "JOIN match_rating_policies p ON p.match_id=m.id "
            "WHERE m.status=? AND p.rating_reason NOT IN ('contest','human')",
            (STATUS_COMPLETED,),
        ).fetchall()
        for row in rows:
            if str(row["id"]) not in settled_ids:
                issues.append(f"completed 对局尚未结算: {row['id']}")
    return sorted(source, key=lambda row: int(row["settled_order"])), issues


def _default_rating(bot_id: int, game_id: str) -> dict[str, Any]:
    return {
        "bot_id": int(bot_id),
        "game_id": game_id,
        "rating": 1500.0,
        "rd": 350.0,
        "vol": 0.06,
        "wins": 0,
        "losses": 0,
        "draws": 0,
        "delta_total": 0,
        "matches_played": 0,
        "last_played_at": None,
    }


def _replay(
    conn: sqlite3.Connection,
    source: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    current_bots = {
        int(row["id"]): str(row["game_id"])
        for row in conn.execute("SELECT id,game_id FROM bots")
    }
    states: dict[tuple[int, str], dict[str, Any]] = {
        (bot_id, game_id): _default_rating(bot_id, game_id)
        for bot_id, game_id in current_bots.items()
    }
    history_by_bot: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    pairs: dict[tuple[int, int], dict[str, Any]] = {}

    for event in source:
        if not event["rated"]:
            continue
        game_id = str(event["game_id"])
        bot_a = int(event["bot_a_id"])
        bot_b = int(event["bot_b_id"])
        if bot_a == bot_b:
            raise RuntimeError(f"rated self-play policy 非法: {event['match_id']}")
        key_a, key_b = (bot_a, game_id), (bot_b, game_id)
        states.setdefault(key_a, _default_rating(bot_a, game_id))
        states.setdefault(key_b, _default_rating(bot_b, game_id))
        old_a, old_b = deepcopy(states[key_a]), deepcopy(states[key_b])
        score_a, score_b = match_scores(event["winner"])
        next_a = update_rating(
            Rating(old_a["rating"], old_a["rd"], old_a["vol"]),
            [(Rating(old_b["rating"], old_b["rd"], old_b["vol"]), score_a)],
        )
        next_b = update_rating(
            Rating(old_b["rating"], old_b["rd"], old_b["vol"]),
            [(Rating(old_a["rating"], old_a["rd"], old_a["vol"]), score_b)],
        )
        draw = int(event["winner"] is None)
        for state, next_rating, won, lost, delta in (
            (states[key_a], next_a, int(event["winner"] == 0), int(event["winner"] == 1), event["delta_a"]),
            (states[key_b], next_b, int(event["winner"] == 1), int(event["winner"] == 0), event["delta_b"]),
        ):
            state["rating"] = next_rating.mu
            state["rd"] = next_rating.phi
            state["vol"] = next_rating.sigma
            state["wins"] += won
            state["losses"] += lost
            state["draws"] += draw
            state["delta_total"] += int(delta)
            state["matches_played"] += 1
            state["last_played_at"] = event["settled_at"]
        for bot_id, key in ((bot_a, key_a), (bot_b, key_b)):
            if current_bots.get(bot_id) == game_id:
                state = states[key]
                history_by_bot[key].append(
                    {
                        "bot_id": bot_id,
                        "game_id": game_id,
                        "rating": state["rating"],
                        "rd": state["rd"],
                        "vol": state["vol"],
                        "matches_played": state["matches_played"],
                        "reason": event["match_id"],
                        "created_at": event["settled_at"],
                    }
                )
        if bot_a in current_bots and bot_b in current_bots:
            lo, hi = sorted((bot_a, bot_b))
            pair = pairs.setdefault(
                (lo, hi),
                {
                    "bot_a_id": lo,
                    "bot_b_id": hi,
                    "samples": 0,
                    "last_played_at": event["settled_at"],
                    "a_wins": 0,
                    "a_losses": 0,
                    "draws": 0,
                },
            )
            pair["samples"] += 1
            pair["last_played_at"] = event["settled_at"]
            if event["winner"] is None:
                pair["draws"] += 1
            else:
                winner_bot = bot_a if event["winner"] == 0 else bot_b
                pair["a_wins" if winner_bot == lo else "a_losses"] += 1

    ratings = [
        states[(bot_id, game_id)]
        for bot_id, game_id in sorted(current_bots.items(), key=lambda row: (row[1], row[0]))
    ]
    history = [
        row
        for key in sorted(history_by_bot, key=lambda item: (item[1], item[0]))
        for row in history_by_bot[key][-200:]
    ]
    return ratings, history, [pairs[key] for key in sorted(pairs)]


def _semantic_projection(
    ratings: list[dict[str, Any]],
    history: list[dict[str, Any]],
    pairs: list[dict[str, Any]],
    settlements: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "ratings": [
            {key: row[key] for key in ("bot_id", "game_id", *_RATING_FIELDS)}
            for row in sorted(ratings, key=lambda item: (item["game_id"], item["bot_id"]))
        ],
        "rating_history": [
            {
                key: row[key]
                for key in (
                    "bot_id", "game_id", "rating", "rd", "vol",
                    "matches_played", "reason", "created_at",
                )
            }
            for row in sorted(
                history,
                key=lambda item: (
                    item["game_id"], item["bot_id"],
                    item["matches_played"], item["reason"],
                ),
            )
        ],
        "pair_stats": [
            {
                key: row[key]
                for key in (
                    "bot_a_id", "bot_b_id", "samples", "a_wins",
                    "a_losses", "draws", "last_played_at",
                )
            }
            for row in sorted(pairs, key=lambda item: (item["bot_a_id"], item["bot_b_id"]))
        ],
        "settlements": [
            {"match_id": row["match_id"], "settled_order": row["settled_order"]}
            for row in sorted(settlements, key=lambda item: item["settled_order"])
        ],
    }


def _current_projection(conn: sqlite3.Connection) -> dict[str, Any]:
    ratings = [dict(row) for row in conn.execute("SELECT * FROM ratings")]
    history = [dict(row) for row in conn.execute("SELECT * FROM rating_history")]
    pairs = [dict(row) for row in conn.execute("SELECT * FROM pair_stats")]
    settlements = [
        dict(row)
        for row in conn.execute(
            "SELECT match_id,settled_at,settled_order "
            "FROM match_rating_settlements WHERE settled_order>0"
        )
    ]
    return _semantic_projection(ratings, history, pairs, settlements)


def _rank_map(ratings: list[dict[str, Any]]) -> dict[tuple[int, str], int]:
    result: dict[tuple[int, str], int] = {}
    games = sorted({str(row["game_id"]) for row in ratings})
    for game_id in games:
        rows = sorted(
            (row for row in ratings if row["game_id"] == game_id),
            key=lambda row: (-float(row["rating"]), int(row["bot_id"])),
        )
        for rank, row in enumerate(rows, 1):
            result[(int(row["bot_id"]), game_id)] = rank
    return result


def _rating_diff(
    current: list[dict[str, Any]], rebuilt: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    before = {(int(row["bot_id"]), str(row["game_id"])): row for row in current}
    after = {(int(row["bot_id"]), str(row["game_id"])): row for row in rebuilt}
    before_rank, after_rank = _rank_map(current), _rank_map(rebuilt)
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after), key=lambda item: (item[1], item[0])):
        old = before.get(key, _default_rating(*key))
        new = after.get(key, _default_rating(*key))
        if all(old.get(field) == new.get(field) for field in _RATING_FIELDS):
            continue
        changes.append(
            {
                "bot_id": key[0],
                "game_id": key[1],
                "rating_before": float(old["rating"]),
                "rating_after": float(new["rating"]),
                "rating_delta": float(new["rating"]) - float(old["rating"]),
                "rank_before": before_rank.get(key),
                "rank_after": after_rank.get(key),
                "matches_before": int(old["matches_played"]),
                "matches_after": int(new["matches_played"]),
                "wld_before": [int(old["wins"]), int(old["losses"]), int(old["draws"])],
                "wld_after": [int(new["wins"]), int(new["losses"]), int(new["draws"])],
                "last_played_at_before": old.get("last_played_at"),
                "last_played_at_after": new.get("last_played_at"),
            }
        )
    return changes


def build_rebuild_plan(db_path: str | Path) -> RebuildPlan:
    path = Path(db_path)
    if not path.is_absolute():
        raise ValueError("评分维护命令只接受显式绝对 --db 路径")
    path = path.resolve(strict=True)
    with _connect_readonly(path) as conn:
        _validate_schema(conn)
        source, issues = _load_source(conn)
        rebuilt_ratings, rebuilt_history, rebuilt_pairs = _replay(conn, source)
        settlements = [
            {
                "match_id": row["match_id"],
                "settled_at": row["settled_at"],
                "settled_order": int(row["settled_order"]),
            }
            for row in source
        ]
        current = _current_projection(conn)
        rebuilt = _semantic_projection(
            rebuilt_ratings, rebuilt_history, rebuilt_pairs, settlements
        )
        current_ratings = current["ratings"]
        changes = _rating_diff(current_ratings, rebuilt_ratings)
        running = 0
        for game_id in sorted(VALID_GAME_IDS):
            running += int(
                conn.execute(
                    f"SELECT COUNT(*) FROM matches_{game_id} WHERE status=?",
                    (STATUS_RUNNING,),
                ).fetchone()[0]
            )
        source_semantic = _source_semantic(source)
        last_order = max(
            (int(row["settled_order"]) for row in source), default=0
        )
        projection_state_row = conn.execute(
            "SELECT * FROM rating_projection_state WHERE singleton=1"
        ).fetchone()
        projection_state = dict(projection_state_row) if projection_state_row else {}
        projection_state_current = bool(
            projection_state.get("policy_version") == CURRENT_POLICY_VERSION
            and int(projection_state.get("source_settlement_count") or 0) == len(source)
            and int(projection_state.get("source_last_settled_order") or 0) == last_order
        )
        dispatcher = conn.execute(
            "SELECT owner_token,lease_until FROM auto_match_dispatcher WHERE singleton=1"
        ).fetchone()
        dispatcher_lease_live = bool(
            dispatcher
            and dispatcher["owner_token"]
            and str(dispatcher["lease_until"] or "")
            > datetime.now().isoformat(timespec="seconds")
        )
        report = {
            "db_path": str(path),
            "mode": "dry-run",
            "policy_version": CURRENT_POLICY_VERSION,
            "authoritative_order": "settled_order",
            "legacy_fallback_order": "ended_at,id",
            "source_settlement_count": len(source),
            "source_last_settled_order": last_order,
            "rated_source_count": sum(1 for row in source if row["rated"]),
            "neutral_source_count": sum(1 for row in source if not row["rated"]),
            "source_hash": _canonical_hash(source_semantic),
            "current_projection_hash": _canonical_hash(current),
            "rebuilt_projection_hash": _canonical_hash(rebuilt),
            "projection_matches": current == rebuilt,
            "projection_state": projection_state,
            "projection_state_current": projection_state_current,
            "changed_bot_count": len(changes),
            "rank_changed_bot_count": sum(
                1 for row in changes if row["rank_before"] != row["rank_after"]
            ),
            "changed_bots": changes,
            "issues": sorted(set(issues)),
            "running_match_count": running,
            "dispatcher_lease_live": dispatcher_lease_live,
            "ready_to_apply": not issues and running == 0 and not dispatcher_lease_live,
        }
        return RebuildPlan(
            source=source,
            ratings=rebuilt_ratings,
            history=rebuilt_history,
            pairs=rebuilt_pairs,
            settlements=settlements,
            report=report,
        )


def _validate_apply_authorization(
    database: Path,
    *,
    expected_source_hash: str,
    confirmed_database: str | Path,
    backup_path: str | Path,
    service_stopped: bool,
    cold_backup_confirmed: bool,
) -> Path:
    if not service_stopped or not cold_backup_confirmed:
        raise ValueError("apply requires explicit stopped-service and cold-backup confirmations")
    confirmed = Path(confirmed_database)
    backup = Path(backup_path)
    if not confirmed.is_absolute() or confirmed.resolve(strict=True) != database:
        raise ValueError("confirmed database path does not match target")
    if not backup.is_absolute():
        raise ValueError("backup path must be absolute")
    backup = backup.resolve(strict=True)
    if not backup.is_file() or backup.stat().st_size <= 0 or backup.samefile(database):
        raise ValueError("backup must be a distinct non-empty file")
    with _connect_readonly(backup) as backup_conn:
        integrity = backup_conn.execute("PRAGMA integrity_check").fetchone()
        _validate_schema(backup_conn)
        backup_source, backup_issues = _load_source(backup_conn)
    if not integrity or integrity[0] != "ok":
        raise ValueError(f"backup integrity_check failed: {integrity}")
    if backup_issues:
        raise ValueError(f"backup rating source is incomplete: {backup_issues}")
    backup_hash = _canonical_hash(_source_semantic(backup_source))
    if backup_hash != expected_source_hash:
        raise ValueError(
            "backup rating source does not match the reviewed target source"
        )
    return backup


def apply_rebuild_plan(
    db_path: str | Path,
    expected_source_hash: str,
    *,
    confirmed_database: str | Path,
    backup_path: str | Path,
    service_stopped: bool,
    cold_backup_confirmed: bool,
) -> dict[str, Any]:
    """Apply one already-reviewed plan under an exclusive SQLite transaction."""
    raw_path = Path(db_path)
    if not raw_path.is_absolute():
        raise ValueError("评分维护命令只接受显式绝对 --db 路径")
    path = raw_path.resolve(strict=True)
    _validate_apply_authorization(
        path,
        expected_source_hash=expected_source_hash,
        confirmed_database=confirmed_database,
        backup_path=backup_path,
        service_stopped=service_stopped,
        cold_backup_confirmed=cold_backup_confirmed,
    )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        conn.execute("BEGIN EXCLUSIVE")
        _validate_schema(conn)
        source, issues = _load_source(conn)
        if issues:
            raise RuntimeError(f"评分源不完整，拒绝重建: {issues}")
        running = sum(
            int(
                conn.execute(
                    f"SELECT COUNT(*) FROM matches_{game_id} WHERE status=?",
                    (STATUS_RUNNING,),
                ).fetchone()[0]
            )
            for game_id in sorted(VALID_GAME_IDS)
        )
        dispatcher = conn.execute(
            "SELECT owner_token,lease_until FROM auto_match_dispatcher WHERE singleton=1"
        ).fetchone()
        lease_live = bool(
            dispatcher
            and dispatcher["owner_token"]
            and str(dispatcher["lease_until"] or "")
            > datetime.now().isoformat(timespec="seconds")
        )
        if running or lease_live:
            raise RuntimeError(
                f"服务未完全停止: running_matches={running} dispatcher_lease={lease_live}"
            )
        source_semantic = _source_semantic(source)
        if _canonical_hash(source_semantic) != expected_source_hash:
            raise RuntimeError("dry-run 后评分源已变化，必须重新 dry-run")
        ratings, history, pairs = _replay(conn, source)
        settlements = [
            {
                "match_id": row["match_id"],
                "settled_at": row["settled_at"],
                "settled_order": int(row["settled_order"]),
            }
            for row in source
        ]

        conn.execute("DELETE FROM rating_history")
        conn.execute("DELETE FROM pair_stats")
        conn.execute("DELETE FROM ratings")
        conn.executemany(
            "INSERT INTO ratings(bot_id,game_id,rating,rd,vol,wins,losses,draws,"
            "delta_total,matches_played,last_played_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            [
                tuple(row[key] for key in (
                    "bot_id", "game_id", "rating", "rd", "vol", "wins", "losses",
                    "draws", "delta_total", "matches_played", "last_played_at",
                ))
                for row in ratings
            ],
        )
        conn.executemany(
            "INSERT INTO rating_history(bot_id,game_id,rating,rd,vol,matches_played,"
            "reason,created_at) VALUES(?,?,?,?,?,?,?,?)",
            [
                tuple(row[key] for key in (
                    "bot_id", "game_id", "rating", "rd", "vol",
                    "matches_played", "reason", "created_at",
                ))
                for row in history
            ],
        )
        conn.executemany(
            "INSERT INTO pair_stats(bot_a_id,bot_b_id,samples,last_played_at,"
            "a_wins,a_losses,draws) VALUES(?,?,?,?,?,?,?)",
            [
                tuple(row[key] for key in (
                    "bot_a_id", "bot_b_id", "samples", "last_played_at",
                    "a_wins", "a_losses", "draws",
                ))
                for row in pairs
            ],
        )
        # Policies and settlement rows are immutable source evidence.  Rebuild
        # only replaces their derived projections; it never clears/reorders the
        # source merely to make a verification pass.
        conn.execute(
            "UPDATE rating_projection_state SET policy_version=?,rebuilt_at=?,"
            "source_settlement_count=?,source_last_settled_order=? WHERE singleton=1",
            (
                CURRENT_POLICY_VERSION,
                datetime.now().isoformat(timespec="seconds"),
                len(settlements),
                max((row["settled_order"] for row in settlements), default=0),
            ),
        )
        expected_projection = _semantic_projection(
            ratings, history, pairs, settlements
        )
        if _current_projection(conn) != expected_projection:
            raise RuntimeError(
                "评分重建事务内 hash 验证失败；已回滚，未替换排行榜投影"
            )
        state = conn.execute(
            "SELECT policy_version,source_settlement_count,"
            "source_last_settled_order FROM rating_projection_state "
            "WHERE singleton=1"
        ).fetchone()
        expected_last = max(
            (row["settled_order"] for row in settlements), default=0
        )
        if (
            state is None
            or state["policy_version"] != CURRENT_POLICY_VERSION
            or int(state["source_settlement_count"] or 0) != len(settlements)
            or int(state["source_last_settled_order"] or 0) != expected_last
        ):
            raise RuntimeError(
                "评分重建事务内水位验证失败；已回滚，未替换排行榜投影"
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    verified = build_rebuild_plan(path)
    result = dict(verified.report)
    result["mode"] = "apply"
    result["verified_after_apply"] = bool(
        result["projection_matches"]
        and result["projection_state_current"]
        and not result["issues"]
    )
    if not result["verified_after_apply"]:
        raise RuntimeError("评分重建提交后 hash 验证失败")
    return result


def sentinel_match_id() -> str:
    """Expose the marker only for maintenance tests/documentation."""
    return MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL


__all__ = [
    "CURRENT_POLICY_VERSION",
    "RebuildPlan",
    "apply_rebuild_plan",
    "build_rebuild_plan",
    "sentinel_match_id",
]
