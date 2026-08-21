"""Read models for contest stage history.

Lifecycle writes stay in :mod:`manager`; this module only combines immutable
stage snapshots with current match progress for the detail API.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from bzplat.backend.store.schema import STATUS_COMPLETED
from bzplat.backend.store.validation import is_authoritative_no_opponent_pairing


def _stages(contest: dict[str, Any]) -> list[dict[str, Any]]:
    raw = contest.get("stages_json") or "[]"
    if isinstance(raw, list):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def _participants(pairings: list[dict[str, Any]]) -> set[int]:
    result: set[int] = set()
    for pairing in pairings:
        for key in ("entry_a_id", "entry_b_id"):
            value = pairing.get(key)
            if value is not None:
                result.add(int(value))
    return result


def _swiss_byes(
    stage: dict[str, Any], pairings: list[dict[str, Any]]
) -> dict[int, int]:
    """Derive awarded Swiss byes from the durable stage pairing graph.

    Stage snapshots predate the public ``byes`` field and deliberately do not
    persist it.  Keeping this projection pairing-backed makes both historical
    and live summaries accurate without a schema migration.  All authoritative
    no-opponent conditions are required so a deleted opponent or incomplete
    pairing is never presented as an awarded bye.
    """
    stage_type = stage.get("type")
    if stage_type != "swiss":
        return {}
    counts: dict[int, int] = defaultdict(int)
    for pairing in pairings:
        entry_a_id = pairing.get("entry_a_id")
        if entry_a_id is not None and is_authoritative_no_opponent_pairing(
            stage_type, pairing
        ):
            counts[int(entry_a_id)] += 1
    return dict(counts)


def _rank_rows(rows: list[dict[str, Any]], *, grouped: bool) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    if grouped:
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            groups[str(row.get("group_id") or "未分组")].append(row)
        for group_id in sorted(groups):
            group_rows = sorted(
                groups[group_id],
                key=lambda row: (
                    -float(row.get("points") or 0),
                    -int(row.get("delta_total") or 0),
                    int(row.get("entry_id") or 0),
                ),
            )
            for rank, row in enumerate(group_rows, 1):
                row["rank"] = rank
                row["group_id"] = group_id
                ordered.append(row)
        return ordered

    ordered = sorted(
        rows,
        key=lambda row: (
            -float(row.get("points") or 0),
            -int(row.get("delta_total") or 0),
            int(row.get("entry_id") or 0),
        ),
    )
    for rank, row in enumerate(ordered, 1):
        row["rank"] = rank
    return ordered


def _advancement_zone(stage: dict[str, Any], rows: list[dict[str, Any]]) -> set[int]:
    if stage.get("advance_per_group"):
        per_group = int(stage["advance_per_group"])
        by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in rows:
            by_group[str(row.get("group_id") or "未分组")].append(row)
        return {
            int(row["entry_id"])
            for group_rows in by_group.values()
            for row in group_rows[:per_group]
        }
    if stage.get("advance_count"):
        return {
            int(row["entry_id"])
            for row in rows[: int(stage["advance_count"])]
        }
    return set()


def build_stage_summaries(
    manager: Any,
    contest: dict[str, Any],
    entries: list[dict[str, Any]],
    pairings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return persisted/live standings for each stage's actual participants.

    ``ContestManager.standings`` intentionally initializes every contest entry.
    That is useful for lifecycle calculations, but would leak non-qualifiers as
    zero-point rows in a later knockout stage.  The presentation contract instead
    intersects every ranking with entry ids materialized in that stage's pairing
    graph.
    """
    stages = _stages(contest)
    current_idx = int(contest.get("current_stage_idx") or 0)
    entry_by_id = {int(entry["id"]): entry for entry in entries}
    pairing_by_stage: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for pairing in pairings:
        pairing_by_stage[int(pairing.get("stage_idx") or 0)].append(pairing)

    persisted_by_stage: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in manager.store.list_stage_results(int(contest["id"])):
        persisted_by_stage[int(row.get("stage_idx") or 0)].append(row)
    persisted_bot_ids = {
        int(row["bot_id"])
        for stage_rows in persisted_by_stage.values()
        for row in stage_rows
        if row.get("bot_id") is not None
    }
    persisted_bots = {
        bot_id: manager.store.get_bot(bot_id) for bot_id in persisted_bot_ids
    }

    result: list[dict[str, Any]] = []
    for stage_idx, stage in enumerate(stages):
        stage_pairings = pairing_by_stage.get(stage_idx, [])
        participant_ids = _participants(stage_pairings)
        bye_counts = _swiss_byes(stage, stage_pairings)
        persisted = persisted_by_stage.get(stage_idx, [])
        if persisted:
            source_rows = persisted
            source = "persisted"
        elif stage_pairings:
            source_rows = manager.standings(int(contest["id"]), stage_idx=stage_idx)
            source = "live" if any(p.get("match_id") for p in stage_pairings) else "scheduled"
        else:
            source_rows = []
            source = "pending"

        rows: list[dict[str, Any]] = []
        for source_row in source_rows:
            entry_id = source_row.get("entry_id")
            if entry_id is None or int(entry_id) not in participant_ids:
                continue
            entry = entry_by_id.get(int(entry_id), {})
            if source == "persisted":
                historical_bot_id = source_row.get("bot_id")
                historical_bot = (
                    persisted_bots.get(int(historical_bot_id))
                    if historical_bot_id is not None
                    else None
                )
                bot_id = historical_bot_id
                bot_name = (
                    historical_bot.get("display_name")
                    or historical_bot.get("name")
                    or "历史 Bot"
                ) if historical_bot else "历史 Bot（已删除）"
            else:
                bot_id = entry.get("bot_id")
                bot_name = entry.get("bot_display") or entry.get("bot_name")
            rows.append(
                {
                    "entry_id": int(entry_id),
                    "bot_id": bot_id,
                    "bot_name": bot_name,
                    "owner_name": entry.get("owner_name") or entry.get("username"),
                    "owner_display": entry.get("owner_display"),
                    "points": float(source_row.get("points") or 0),
                    "wins": int(source_row.get("wins") or 0),
                    "draws": int(source_row.get("draws") or 0),
                    "losses": int(source_row.get("losses") or 0),
                    "byes": bye_counts.get(int(entry_id), 0),
                    "delta_total": int(source_row.get("delta_total") or 0),
                    "group_id": source_row.get("group_id") or entry.get("group_id") or "",
                }
            )

        grouped = str(stage.get("type") or "").startswith("group_")
        rows = _rank_rows(rows, grouped=grouped)
        stage_type = stage.get("type")
        completed_count = sum(
            1
            for pairing in stage_pairings
            if is_authoritative_no_opponent_pairing(stage_type, pairing)
            or (
                pairing.get("match_id") is not None
                and pairing.get("match_status") == STATUS_COMPLETED
            )
        )
        all_completed = bool(stage_pairings) and completed_count == len(stage_pairings)

        next_ids = _participants(pairing_by_stage.get(stage_idx + 1, []))
        advancement_final = bool(next_ids) or all_completed
        advancement_ids = next_ids or (
            _advancement_zone(stage, rows) if rows and (all_completed or completed_count) else set()
        )
        for row in rows:
            if not advancement_ids:
                row["advancement"] = None
            elif int(row["entry_id"]) in advancement_ids:
                row["advancement"] = "advanced" if advancement_final else "in_zone"
            elif advancement_final:
                row["advancement"] = "eliminated"
            else:
                row["advancement"] = "outside_zone"

        if not stage_pairings:
            status = "pending"
        elif all_completed:
            status = "completed"
        elif stage_idx == current_idx and contest.get("status") == "published":
            status = "published"
        elif stage_idx == current_idx:
            status = "running"
        else:
            status = "pending"

        result.append(
            {
                "stage_idx": stage_idx,
                "stage_key": stage.get("key") or f"stage{stage_idx}",
                "status": status,
                "source": source,
                "completed_pairings": completed_count,
                "total_pairings": len(stage_pairings),
                "advancement_final": advancement_final,
                "rows": rows,
            }
        )
    return result


__all__ = ["build_stage_summaries"]
