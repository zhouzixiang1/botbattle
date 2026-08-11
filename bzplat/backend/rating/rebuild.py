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
from bzplat.backend.runtime.config import RANKING_MIN_RATED_MATCHES
from bzplat.backend.store import (
    rating_projection_digest,
    rating_projection_digests,
    rating_source_input_issues,
)
from bzplat.backend.store.schema import (
    MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,
    STATUS_COMPLETED,
    STATUS_RUNNING,
    VALID_GAME_IDS,
    is_supported_binary_metadata,
)

CURRENT_POLICY_VERSION = "owner-neutral-v3"
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
    bot_universe: list[dict[str, Any]]
    ratings: list[dict[str, Any]]
    history: list[dict[str, Any]]
    pairs: list[dict[str, Any]]
    settlements: list[dict[str, Any]]
    report: dict[str, Any]


@dataclass(frozen=True)
class _DatabaseFingerprint:
    business_digest: str
    file_digest: str


def _connect_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as database_file:
        for chunk in iter(lambda: database_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sql_value(value: Any) -> Any:
    """Return one typed, JSON-safe SQLite value for the full-business digest."""
    if isinstance(value, bytes):
        return {"type": "blob", "hex": value.hex()}
    if isinstance(value, float):
        return {"type": "float", "value": value.hex()}
    if value is None:
        return {"type": "null"}
    return {"type": type(value).__name__, "value": value}


def _quoted_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _database_business_digest(conn: sqlite3.Connection) -> str:
    """Hash the complete logical schema and every application row.

    The rating source alone is deliberately insufficient backup evidence: an
    older database can have the same settlements while losing newer users,
    contests, notifications, or other business rows.  This digest is streamed
    from the same SQLite transaction as the rebuild plan.
    """
    digest = hashlib.sha256()

    def add(value: Any) -> None:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)

    schema_rows = conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_autoindex_%' ORDER BY type,name"
    ).fetchall()
    add({"schema": [list(row) for row in schema_rows]})

    tables = [
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND (name NOT LIKE 'sqlite_%' OR name='sqlite_sequence') "
            "ORDER BY name"
        )
    ]
    for table in tables:
        columns = [
            dict(row)
            for row in conn.execute("SELECT * FROM pragma_table_info(?)", (table,))
        ]
        names = [str(column["name"]) for column in columns]
        primary_key = [
            str(column["name"])
            for column in sorted(columns, key=lambda column: int(column["pk"] or 0))
            if int(column["pk"] or 0) > 0
        ]
        order_by = primary_key or names
        select_columns = ",".join(_quoted_identifier(name) for name in names)
        order_columns = ",".join(_quoted_identifier(name) for name in order_by)
        query = (
            f"SELECT {select_columns} FROM {_quoted_identifier(table)}"
            + (f" ORDER BY {order_columns}" if order_columns else "")
        )
        add({"table": table, "columns": names})
        for row in conn.execute(query):
            add([_sql_value(value) for value in row])
    return digest.hexdigest()


def _validate_database_health(
    conn: sqlite3.Connection, *, label: str
) -> None:
    integrity = [str(row[0]) for row in conn.execute("PRAGMA integrity_check")]
    if integrity != ["ok"]:
        raise ValueError(f"{label} integrity_check failed: {integrity[:10]}")
    foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    if foreign_keys:
        sample = [list(row) for row in foreign_keys[:10]]
        raise ValueError(
            f"{label} foreign_key_check failed: count={len(foreign_keys)} "
            f"sample={sample}"
        )


def _load_bot_universe(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    bots = [
        dict(row)
        for row in conn.execute(
            "SELECT id,game_id,is_active,format,os,arch FROM bots "
            "ORDER BY game_id,id"
        )
    ]
    invalid = [
        (int(row["id"]), str(row.get("game_id") or ""))
        for row in bots
        if str(row.get("game_id") or "") not in VALID_GAME_IDS
    ]
    if invalid:
        raise RuntimeError(f"Bot universe 含非法 game_id: {invalid[:20]}")
    return bots


def _execution_queue_issues(
    conn: sqlite3.Connection,
) -> tuple[int, list[str]]:
    count = int(
        conn.execute(
            "SELECT COUNT(*) FROM execution_jobs "
            "WHERE status IN ('starting','running','settling')"
        ).fetchone()[0]
    )
    if count == 0:
        return 0, []
    return count, [
        f"execution_jobs 有 {count} 个活跃 attempt；必须先停止服务并完成清场"
    ]


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
        "rating_settlement_sequence",
        "execution_jobs",
        "execution_job_attempts",
        "execution_control",
    }
    missing = sorted(required - _tables(conn))
    if missing:
        raise RuntimeError(f"评分重建 schema 尚未迁移，缺少: {missing}")
    for table, columns in (
        (
            "match_rating_policies",
            {
                "game_id",
                "bot_a_id",
                "bot_b_id",
                "rated",
                "rating_reason",
                "source",
                "classified_at",
                "settled_order",
            },
        ),
        ("match_rating_settlements", {"match_id", "settled_at", "settled_order"}),
        (
            "rating_projection_state",
            {
                "policy_version",
                "rebuilt_at",
                "source_settlement_count",
                "source_last_settled_order",
                "source_digest",
                "projection_digest",
                "plan_digest",
                "mutation_revision",
                "trusted_mutation_revision",
            },
        ),
        ("rating_settlement_sequence", {"next_order"}),
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
        if int(policy.get("settled_order") or 0) != order:
            issues.append(
                f"policy/settlement settled_order 不一致: {match_id} "
                f"policy={policy.get('settled_order')} settlement={order}"
            )
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
            issues.append(f"rating source result invalid: {match_id}")
            continue
        rated = bool(int(policy["rated"]))
        input_issues = rating_source_input_issues(
            match_id=match_id,
            rated=policy["rated"],
            rating_reason=policy["rating_reason"],
            result=result,
        )
        if input_issues:
            issues.extend(input_issues)
            continue
        deltas = result.get("deltas") if isinstance(result, dict) else None
        if rated and (
            policy.get("bot_a_id") is None
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
    if seen_orders:
        expected_orders = set(range(1, max(seen_orders) + 1))
        missing_orders = sorted(expected_orders - seen_orders)
        if missing_orders:
            issues.append(f"settled_order 不连续，缺少: {missing_orders[:20]}")
    for match_id, policy in policies.items():
        policy_order = int(policy.get("settled_order") or 0)
        if policy_order > 0 and match_id not in settled_ids:
            issues.append(
                f"已冻结 settled_order 但缺 settlement marker: "
                f"{match_id} order={policy_order}"
            )
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
    source: list[dict[str, Any]],
    bot_universe: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    current_bots = {
        int(row["id"]): str(row["game_id"])
        for row in bot_universe
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


def _current_projection_rows(
    conn: sqlite3.Connection,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    return (
        [dict(row) for row in conn.execute("SELECT * FROM ratings")],
        [dict(row) for row in conn.execute("SELECT * FROM rating_history")],
        [dict(row) for row in conn.execute("SELECT * FROM pair_stats")],
    )


def _projection_state_current(
    state: dict[str, Any], live: dict[str, Any]
) -> bool:
    """Mirror Store.rating_projection_status without a second connection."""
    return bool(
        state.get("policy_version") == CURRENT_POLICY_VERSION
        and int(state.get("mutation_revision") or 0)
        == int(state.get("trusted_mutation_revision") or 0)
        and int(state.get("source_settlement_count") or 0)
        == int(live["source_settlement_count"])
        and int(state.get("source_last_settled_order") or 0)
        == int(live["source_last_settled_order"])
        and str(state.get("source_digest") or "") == live["source_digest"]
        and str(state.get("projection_digest") or "")
        == live["projection_digest"]
        and str(state.get("plan_digest") or "") == live["plan_digest"]
        and not live["issues"]
    )


def _leaderboard_projection(
    ratings: list[dict[str, Any]],
    bot_universe: list[dict[str, Any]],
) -> dict[tuple[int, str], dict[str, Any]]:
    """Project ratings with the production leaderboard's exact public scope.

    ``Store.list_leaderboard`` admits only active Linux/amd64 ELF Bots whose
    rating game matches the Bot game.  Ranked rows require the code-owned match
    threshold and are ordered by rating, matches played, then Bot ID.  Public
    candidates below the threshold remain visible but have no public rank.
    """
    bots = {int(row["id"]): row for row in bot_universe}
    projected: dict[tuple[int, str], dict[str, Any]] = {}
    ranked_by_game: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for rating in ratings:
        bot_id = int(rating["bot_id"])
        game_id = str(rating["game_id"])
        bot = bots.get(bot_id)
        if (
            bot is None
            or str(bot.get("game_id") or "") != game_id
            or int(bot.get("is_active") or 0) != 1
            or not is_supported_binary_metadata(
                str(bot.get("format") or ""),
                str(bot.get("os") or ""),
                str(bot.get("arch") or ""),
            )
        ):
            continue
        matches_played = max(0, int(rating.get("matches_played") or 0))
        ranking_eligible = matches_played >= max(
            1, int(RANKING_MIN_RATED_MATCHES)
        )
        item = {
            "rank": None,
            "ranking_eligible": ranking_eligible,
            "rating": float(rating["rating"]),
            "matches_played": matches_played,
            "bot_id": bot_id,
        }
        projected[(bot_id, game_id)] = item
        if ranking_eligible:
            ranked_by_game[game_id].append(item)

    for rows in ranked_by_game.values():
        rows.sort(
            key=lambda row: (
                -float(row["rating"]),
                -int(row["matches_played"]),
                int(row["bot_id"]),
            )
        )
        for rank, row in enumerate(rows, 1):
            row["rank"] = rank
    return projected


def _rating_diff(
    current: list[dict[str, Any]],
    rebuilt: list[dict[str, Any]],
    bot_universe: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    before = {(int(row["bot_id"]), str(row["game_id"])): row for row in current}
    after = {(int(row["bot_id"]), str(row["game_id"])): row for row in rebuilt}
    before_board = _leaderboard_projection(current, bot_universe)
    after_board = _leaderboard_projection(rebuilt, bot_universe)
    changes: list[dict[str, Any]] = []
    for key in sorted(set(before) | set(after), key=lambda item: (item[1], item[0])):
        old = before.get(key, _default_rating(*key))
        new = after.get(key, _default_rating(*key))
        old_board = before_board.get(key)
        new_board = after_board.get(key)
        projection_changed = (
            key not in before
            or key not in after
            or any(old.get(field) != new.get(field) for field in _RATING_FIELDS)
        )
        if not projection_changed and old_board == new_board:
            continue
        changes.append(
            {
                "bot_id": key[0],
                "game_id": key[1],
                "projection_changed": projection_changed,
                "rating_before": float(old["rating"]),
                "rating_after": float(new["rating"]),
                "rating_delta": float(new["rating"]) - float(old["rating"]),
                "public_candidate_before": old_board is not None,
                "public_candidate_after": new_board is not None,
                "ranking_eligible_before": (
                    old_board["ranking_eligible"] if old_board is not None else None
                ),
                "ranking_eligible_after": (
                    new_board["ranking_eligible"] if new_board is not None else None
                ),
                "rank_before": old_board["rank"] if old_board is not None else None,
                "rank_after": new_board["rank"] if new_board is not None else None,
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
        # Python's sqlite3 does not start a transaction for SELECT statements.
        # Begin explicitly so source, Bot universe, current projection, rebuilt
        # projection and the complete-business digest are one immutable snapshot.
        conn.execute("BEGIN")
        try:
            _validate_schema(conn)
            _validate_database_health(conn, label="target")
            live = rating_projection_digests(conn)
            source, replay_issues = _load_source(conn)
            execution_active_count, queue_issues = _execution_queue_issues(conn)
            issues = sorted(
                set([*live["issues"], *replay_issues, *queue_issues])
            )
            bot_universe = _load_bot_universe(conn)
            rebuilt_ratings, rebuilt_history, rebuilt_pairs = _replay(
                source, bot_universe
            )
            settlements = [
                {
                    "match_id": row["match_id"],
                    "settled_at": row["settled_at"],
                    "settled_order": int(row["settled_order"]),
                }
                for row in source
            ]
            current_ratings, _, _ = _current_projection_rows(conn)
            changes = _rating_diff(
                current_ratings, rebuilt_ratings, bot_universe
            )
            running = sum(
                int(
                    conn.execute(
                        f"SELECT COUNT(*) FROM matches_{game_id} WHERE status=?",
                        (STATUS_RUNNING,),
                    ).fetchone()[0]
                )
                for game_id in sorted(VALID_GAME_IDS)
            )
            projection_state_row = conn.execute(
                "SELECT * FROM rating_projection_state WHERE singleton=1"
            ).fetchone()
            projection_state = (
                dict(projection_state_row) if projection_state_row else {}
            )
            projection_state_current = _projection_state_current(
                projection_state, live
            )
            dispatcher = conn.execute(
                "SELECT dispatcher_state,accepting FROM execution_control "
                "WHERE singleton=1"
            ).fetchone()
            dispatcher_state = (
                str(dispatcher["dispatcher_state"] or "stopped")
                if dispatcher
                else "missing"
            )
            dispatcher_active = bool(
                dispatcher_state != "stopped"
                or (dispatcher and int(dispatcher["accepting"] or 0) != 0)
            )
            source_digest = str(live["source_digest"])
            plan_digest = str(live["plan_digest"])
            current_projection_digest = str(live["projection_digest"])
            rebuilt_projection_digest = rating_projection_digest(
                rebuilt_ratings, rebuilt_history, rebuilt_pairs
            )
            business_digest = _database_business_digest(conn)
            file_digest = _file_digest(path)
            eligible_rebuilt = _leaderboard_projection(
                rebuilt_ratings, bot_universe
            )
            report = {
                "db_path": str(path),
                "mode": "dry-run",
                "policy_version": CURRENT_POLICY_VERSION,
                "authoritative_order": "settled_order",
                "legacy_migration_order": (
                    "coalesce(ended_at,settled_at),match_id"
                ),
                "source_settlement_count": int(
                    live["source_settlement_count"]
                ),
                "source_last_settled_order": int(
                    live["source_last_settled_order"]
                ),
                "sequence_next_order": int(live["sequence_next_order"]),
                "rated_source_count": sum(1 for row in source if row["rated"]),
                "neutral_source_count": sum(
                    1 for row in source if not row["rated"]
                ),
                "bot_universe_count": len(bot_universe),
                "bot_universe_digest": str(live["bot_universe_digest"]),
                "public_candidate_bot_count": len(eligible_rebuilt),
                "ranking_eligible_bot_count": sum(
                    1
                    for row in eligible_rebuilt.values()
                    if row["ranking_eligible"]
                ),
                "source_digest": source_digest,
                "plan_digest": plan_digest,
                "current_projection_digest": current_projection_digest,
                "rebuilt_projection_digest": rebuilt_projection_digest,
                # Keep the original report keys for machine consumers while the
                # guarded CLI moves to explicit --expect-*-digest options.
                "source_hash": source_digest,
                "current_projection_hash": current_projection_digest,
                "rebuilt_projection_hash": rebuilt_projection_digest,
                "business_digest": business_digest,
                "database_file_digest": file_digest,
                "integrity_check": "ok",
                "foreign_key_violation_count": 0,
                "projection_matches": (
                    current_projection_digest == rebuilt_projection_digest
                ),
                "projection_state": projection_state,
                "projection_state_current": projection_state_current,
                "changed_bot_count": len(changes),
                "projection_changed_bot_count": sum(
                    1 for row in changes if row["projection_changed"]
                ),
                "rank_changed_bot_count": sum(
                    1
                    for row in changes
                    if row["rank_before"] != row["rank_after"]
                ),
                "changed_bots": changes,
                "issues": sorted(set(issues)),
                "running_match_count": running,
                "execution_active_count": execution_active_count,
                "dispatcher_state": dispatcher_state,
                "dispatcher_active": dispatcher_active,
                "ready_to_apply": (
                    not issues and running == 0 and not dispatcher_active
                ),
            }
            return RebuildPlan(
                source=source,
                bot_universe=bot_universe,
                ratings=rebuilt_ratings,
                history=rebuilt_history,
                pairs=rebuilt_pairs,
                settlements=settlements,
                report=report,
            )
        finally:
            conn.rollback()


def _validate_apply_authorization(
    database: Path,
    *,
    expected_source_digest: str,
    expected_plan_digest: str,
    expected_rebuilt_projection_digest: str,
    confirmed_database: str | Path,
    backup_path: str | Path,
    service_stopped: bool,
    cold_backup_confirmed: bool,
) -> _DatabaseFingerprint:
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
        backup_conn.execute("BEGIN")
        try:
            _validate_schema(backup_conn)
            _validate_database_health(backup_conn, label="backup")
            backup_live = rating_projection_digests(backup_conn)
            backup_source, replay_issues = _load_source(backup_conn)
            backup_issues = sorted(
                set([*backup_live["issues"], *replay_issues])
            )
            if backup_issues:
                raise ValueError(
                    f"backup rating source is incomplete: {backup_issues}"
                )
            backup_bots = _load_bot_universe(backup_conn)
            backup_ratings, backup_history, backup_pairs = _replay(
                backup_source, backup_bots
            )
            backup_rebuilt_digest = rating_projection_digest(
                backup_ratings,
                backup_history,
                backup_pairs,
            )
            actual = (
                backup_live["source_digest"],
                backup_live["plan_digest"],
                backup_rebuilt_digest,
            )
            expected = (
                expected_source_digest,
                expected_plan_digest,
                expected_rebuilt_projection_digest,
            )
            if actual != expected:
                raise ValueError(
                    "backup source/plan/rebuilt projection digests do not "
                    "match the reviewed dry-run"
                )
            fingerprint = _DatabaseFingerprint(
                business_digest=_database_business_digest(backup_conn),
                file_digest=_file_digest(backup),
            )
        finally:
            backup_conn.rollback()
    return fingerprint


def apply_rebuild_plan(
    db_path: str | Path,
    expected_source_digest: str,
    expected_plan_digest: str,
    expected_rebuilt_projection_digest: str,
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
    backup_fingerprint = _validate_apply_authorization(
        path,
        expected_source_digest=expected_source_digest,
        expected_plan_digest=expected_plan_digest,
        expected_rebuilt_projection_digest=expected_rebuilt_projection_digest,
        confirmed_database=confirmed_database,
        backup_path=backup_path,
        service_stopped=service_stopped,
        cold_backup_confirmed=cold_backup_confirmed,
    )
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    no_op = False
    try:
        conn.execute("BEGIN EXCLUSIVE")
        _validate_schema(conn)
        _validate_database_health(conn, label="target")
        live = rating_projection_digests(conn)
        source, replay_issues = _load_source(conn)
        execution_active_count, queue_issues = _execution_queue_issues(conn)
        if execution_active_count:
            raise RuntimeError(f"评分重建 No-Go: {queue_issues[0]}")
        issues = sorted(set([*live["issues"], *replay_issues]))
        if issues:
            raise RuntimeError(f"评分源不完整，拒绝重建: {issues}")
        bot_universe = _load_bot_universe(conn)
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
            "SELECT dispatcher_state,accepting FROM execution_control WHERE singleton=1"
        ).fetchone()
        dispatcher_state = (
            str(dispatcher["dispatcher_state"] or "stopped")
            if dispatcher
            else "missing"
        )
        dispatcher_active = bool(
            dispatcher_state != "stopped"
            or (dispatcher and int(dispatcher["accepting"] or 0) != 0)
        )
        if running or dispatcher_active:
            raise RuntimeError(
                "服务未完全停止: "
                f"running_matches={running} dispatcher_state={dispatcher_state}"
            )
        ratings, history, pairs = _replay(source, bot_universe)
        rebuilt_projection_digest = rating_projection_digest(
            ratings, history, pairs
        )
        actual_digests = (
            live["source_digest"],
            live["plan_digest"],
            rebuilt_projection_digest,
        )
        expected_digests = (
            expected_source_digest,
            expected_plan_digest,
            expected_rebuilt_projection_digest,
        )
        if actual_digests != expected_digests:
            names = ("source", "plan", "rebuilt_projection")
            changed = [
                name
                for name, actual, expected in zip(
                    names, actual_digests, expected_digests, strict=True
                )
                if actual != expected
            ]
            raise RuntimeError(
                f"dry-run 后评分重建摘要已变化: {changed}；必须重新 dry-run"
            )

        # A cold backup is evidence for the entire stopped database, not merely
        # the rating source.  Both the logical business image and exact file
        # bytes must match the target before the first write statement.
        target_business_digest = _database_business_digest(conn)
        target_file_digest = _file_digest(path)
        if target_business_digest != backup_fingerprint.business_digest:
            raise RuntimeError(
                "cold backup does not match target complete business digest"
            )
        if target_file_digest != backup_fingerprint.file_digest:
            raise RuntimeError(
                "cold backup does not match target database file digest"
            )

        state = conn.execute(
            "SELECT * FROM rating_projection_state WHERE singleton=1"
        ).fetchone()
        state_current = _projection_state_current(
            dict(state) if state else {}, live
        )
        if (
            live["projection_digest"] == rebuilt_projection_digest
            and state_current
        ):
            # BEGIN EXCLUSIVE itself is read-only until a DML statement. Roll
            # back explicitly so a second semantic apply is a true zero-write:
            # no delete/insert churn and no rebuilt_at/mtime movement.
            no_op = True
            conn.rollback()
        else:
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
            # Policies and settlement rows are immutable source evidence. Rebuild
            # replaces only derived projections and its verification watermark.
            rebuilt_live = rating_projection_digests(conn)
            if (
                rebuilt_live["issues"]
                or rebuilt_live["source_digest"] != expected_source_digest
                or rebuilt_live["plan_digest"] != expected_plan_digest
                or rebuilt_live["projection_digest"]
                != expected_rebuilt_projection_digest
            ):
                raise RuntimeError(
                    "评分重建事务内 digest 验证失败；已回滚，未替换排行榜投影"
                )
            conn.execute(
                "UPDATE rating_projection_state SET policy_version=?,rebuilt_at=?,"
                "source_settlement_count=?,source_last_settled_order=?,"
                "source_digest=?,projection_digest=?,plan_digest=?,"
                "trusted_mutation_revision=mutation_revision "
                "WHERE singleton=1",
                (
                    CURRENT_POLICY_VERSION,
                    datetime.now().isoformat(timespec="seconds"),
                    int(rebuilt_live["source_settlement_count"]),
                    int(rebuilt_live["source_last_settled_order"]),
                    rebuilt_live["source_digest"],
                    rebuilt_live["projection_digest"],
                    rebuilt_live["plan_digest"],
                ),
            )
            state = conn.execute(
                "SELECT * FROM rating_projection_state WHERE singleton=1"
            ).fetchone()
            if not _projection_state_current(
                dict(state) if state else {}, rebuilt_live
            ):
                raise RuntimeError(
                    "评分重建事务内水位验证失败；已回滚，未替换排行榜投影"
                )
            _validate_database_health(conn, label="rebuilt target")
            conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    verified = build_rebuild_plan(path)
    result = dict(verified.report)
    result["mode"] = "apply"
    result["applied"] = not no_op
    result["no_op"] = no_op
    result["rows_written"] = 0 if no_op else None
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
