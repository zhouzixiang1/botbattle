"""Read models for contest stage history.

Lifecycle writes stay in :mod:`manager`; this module only combines immutable
stage snapshots with current match progress for the detail API.
"""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any

from bzplat.backend.contests.series import (
    contest_match_binding_is_valid,
    contest_pairing_roster_binding_is_valid,
    group_conceptual_series,
    is_aggregate_series_stage,
    match_scoring_result_is_valid,
    series_rows_settled,
    summarize_conceptual_series,
)
from bzplat.backend.contests.stages import (
    effective_group_count,
    effective_swiss_rounds,
)
from bzplat.backend.contests.validation import (
    SERIES_SCORING_AGGREGATE,
    SERIES_SCORING_INDEPENDENT,
    active_contest_entries,
    stage_duplicate_mode,
    stage_scoring_contract_is_valid,
)
from bzplat.backend.games import registry as game_registry
from bzplat.backend.matches.public_outcome import (
    planned_match_games,
    scoring_games_for_match,
)
from bzplat.backend.store.public_contract import (
    sanitize_public_contest_tiebreaks,
)
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
    if not isinstance(parsed, list) or any(
        not isinstance(stage, dict) for stage in parsed
    ):
        return []
    return parsed


def _participants(pairings: list[dict[str, Any]]) -> set[int]:
    result: set[int] = set()
    for pairing in pairings:
        for key in ("entry_a_id", "entry_b_id"):
            value = pairing.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                result.add(value)
    return result


def _future_stage_cohort_sizes(
    stages: list[dict[str, Any]],
    *,
    current_idx: int,
    active_count: int,
    game_id: str,
) -> dict[int, int | None]:
    """Project future participant cardinality from the frozen advancement chain.

    Entry identities do not exist until the next stage is materialized, but its
    bounded size is already authoritative through ``advance_count`` /
    ``advance_per_group`` / elimination topology (and a replace-top scope).
    Never substitute the whole active roster for that unknown identity set.
    """
    sizes: dict[int, int | None] = {}
    cohort: int | None = max(0, int(active_count))
    for stage_idx in range(max(0, current_idx), len(stages)):
        stage = stages[stage_idx]
        if cohort is None or not stage_scoring_contract_is_valid(
            stage, game_id=game_id
        ):
            sizes[stage_idx] = None
            cohort = None
            continue
        if stage_idx > current_idx and stage.get("ranking_mode") == "replace_top":
            scope = stage.get("ranking_scope")
            if isinstance(scope, int) and not isinstance(scope, bool):
                cohort = min(cohort, scope)
            else:  # guarded by the shared validator; keep this branch fail closed
                sizes[stage_idx] = None
                cohort = None
                continue
        sizes[stage_idx] = cohort
        if "advance_per_group" in stage:
            group_count = effective_group_count(
                cohort, int(stage.get("group_count", 4))
            )
            cohort = min(
                cohort, group_count * int(stage["advance_per_group"])
            )
        elif "advance_count" in stage:
            cohort = min(cohort, int(stage["advance_count"]))
        elif stage.get("type") == "single_elimination":
            cohort = min(cohort, 1)

    return sizes


def _pairing_match(row: dict[str, Any]) -> dict[str, Any] | None:
    if not row.get("match_id"):
        return None
    return {
        "id": str(row["match_id"]),
        "status": row.get("match_status"),
        "winner": row.get("match_winner"),
        "reason": row.get("_match_reason"),
        "technical_loss": row.get("_match_technical_loss"),
        "result": row.get("_match_result_json"),
        "match_config": row.get("_match_config_json"),
        "contest_id": row.get("_match_contest_id"),
        "game_id": row.get("_match_game_id"),
        "match_type": row.get("_match_type"),
        "bot_a_id": row.get("_match_bot_a_id"),
        "bot_b_id": row.get("_match_bot_b_id"),
    }


def expected_stage_topology(
    stage: dict[str, Any],
    expected_entry_ids: set[int] | list[int] | tuple[int, ...] | None,
    *,
    expected_entry_count: int | None = None,
    expected_swiss_rounds: int | None = None,
    game_id: str | None = None,
) -> dict[str, int] | None:
    """Derive explicit-series totals from the frozen cohort, never existing rows."""
    if (
        not stage_scoring_contract_is_valid(stage, game_id=game_id)
        or stage.get("series_scoring") not in {
            SERIES_SCORING_INDEPENDENT,
            SERIES_SCORING_AGGREGATE,
        }
        or (expected_entry_ids is None and expected_entry_count is None)
    ):
        return None
    if expected_entry_ids is not None:
        raw_cohort = list(expected_entry_ids)
        if any(
            isinstance(entry_id, bool) or not isinstance(entry_id, int)
            for entry_id in raw_cohort
        ):
            return None
        cohort = set(raw_cohort)
        if len(cohort) != len(raw_cohort):
            return None
        cohort_count = len(cohort)
        if expected_entry_count is not None and expected_entry_count != cohort_count:
            return None
    else:
        if (
            isinstance(expected_entry_count, bool)
            or not isinstance(expected_entry_count, int)
            or expected_entry_count < 0
        ):
            return None
        cohort_count = expected_entry_count
    games_per_pair = stage.get("games_per_pair")
    if (
        isinstance(games_per_pair, bool)
        or not isinstance(games_per_pair, int)
        or games_per_pair < 1
    ):
        return None
    stage_type = stage.get("type")
    if stage_type in {"round_robin", "double_round_robin"}:
        encounters = cohort_count * max(0, cohort_count - 1) // 2
        byes = 0
    elif (
        stage_type == "swiss"
        and isinstance(expected_swiss_rounds, int)
        and not isinstance(expected_swiss_rounds, bool)
        and expected_swiss_rounds >= 0
    ):
        encounters = (cohort_count // 2) * expected_swiss_rounds
        byes = (cohort_count % 2) * expected_swiss_rounds
    else:
        return None
    match_jobs = encounters * games_per_pair
    return {
        "encounter_groups": encounters,
        "match_jobs": match_jobs,
        "pairing_rows": match_jobs + byes,
    }


def build_stage_counts(
    stage: dict[str, Any],
    pairings: list[dict[str, Any]],
    *,
    game_id: str,
    expected_entry_ids: set[int] | list[int] | tuple[int, ...] | None = None,
    expected_entry_count: int | None = None,
    expected_swiss_rounds: int | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: dict[int, int | None] | None = None,
    expected_entry_users: dict[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> dict[str, Any]:
    """Count conceptual encounters, physical jobs, and direct scoring games."""
    stage_type = stage.get("type")
    real = [
        row
        for row in pairings
        if not is_authoritative_no_opponent_pairing(stage_type, row)
    ]
    matches = {
        str(row["match_id"]): match
        for row in real
        if row.get("match_id")
        if (match := _pairing_match(row)) is not None
    }
    grouped = group_conceptual_series(stage, real)
    duplicate = stage_duplicate_mode(stage)
    stage_contract_valid = stage_scoring_contract_is_valid(
        stage, game_id=game_id
    )
    spec = game_registry.get(game_id)
    expected_topology = expected_stage_topology(
        stage,
        expected_entry_ids,
        expected_entry_count=expected_entry_count,
        expected_swiss_rounds=expected_swiss_rounds,
        game_id=game_id,
    )
    encounter_completed = 0
    for rows in grouped.values():
        if not stage_contract_valid:
            completed = False
        elif "games_per_pair" in stage:
            completed = series_rows_settled(
                stage,
                rows,
                matches.get,
                game_spec=game_registry.get(game_id),
                expected_contest_id=expected_contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )
        else:
            completed = bool(rows) and all(
                match_scoring_result_is_valid(
                    stage,
                    matches.get(str(row.get("match_id"))),
                    game_spec=spec,
                    pairing=row,
                    expected_contest_id=expected_contest_id,
                    expected_entry_bots=expected_entry_bots,
                    expected_entry_users=expected_entry_users,
                    require_current_entry_bots=require_current_entry_bots,
                )
                for row in rows
            )
        encounter_completed += int(completed)

    match_completed = sum(
        match_scoring_result_is_valid(
            stage,
            matches.get(str(row.get("match_id"))),
            game_spec=spec,
            pairing=row,
            expected_contest_id=expected_contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
        for row in real
    )
    if not stage_contract_valid:
        scoring_planned = 0
        scoring_completed = 0
        terminal_unplayed = 0
    elif is_aggregate_series_stage(stage):
        scoring_planned = (
            expected_topology["encounter_groups"]
            if expected_topology is not None
            else len(grouped)
        )
        scoring_completed = 0
        for rows in grouped.values():
            try:
                scoring_completed += int(
                    summarize_conceptual_series(
                        stage,
                        rows,
                        matches.get,
                        game_spec=spec,
                        expected_contest_id=expected_contest_id,
                        expected_entry_bots=expected_entry_bots,
                        expected_entry_users=expected_entry_users,
                        require_current_entry_bots=require_current_entry_bots,
                    )["settled"]
                )
            except (TypeError, ValueError):
                continue
        terminal_unplayed = 0
    else:
        per_match_planned = planned_match_games(spec, duplicate=duplicate)
        match_jobs_total = (
            expected_topology["match_jobs"]
            if expected_topology is not None
            else len(real)
        )
        scoring_planned = match_jobs_total * per_match_planned
        scoring_completed = 0
        terminal_unplayed = 0
        for row in real:
            match = matches.get(str(row.get("match_id")))
            games = (
                scoring_games_for_match(
                    match,
                    duplicate=duplicate,
                    planned_games=per_match_planned,
                    fixed_rounds_per_match=spec.fixed_rounds_per_match,
                    require_frozen_duplicate=(
                        stage.get("series_scoring")
                        == SERIES_SCORING_INDEPENDENT
                    ),
                    normalize_delta=spec.normalize_delta,
                )
                if contest_match_binding_is_valid(
                    row,
                    match,
                    expected_contest_id=expected_contest_id,
                    expected_game_id=(
                        game_id if expected_contest_id is not None else None
                    ),
                    expected_entry_bots=expected_entry_bots,
                    expected_entry_users=expected_entry_users,
                    require_current_entry_bots=require_current_entry_bots,
                )
                else ()
            )
            scoring_completed += len(games)
            if (
                games
                and match
                and match.get("status") == STATUS_COMPLETED
                and match.get("technical_loss") in (True, 1)
            ):
                terminal_unplayed += max(0, per_match_planned - len(games))
    return {
        "encounter_groups": {
            "completed": encounter_completed,
            "total": (
                expected_topology["encounter_groups"]
                if expected_topology is not None
                else len(grouped)
            ),
        },
        "match_jobs": {
            "completed": match_completed,
            "total": (
                expected_topology["match_jobs"]
                if expected_topology is not None
                else len(real)
            ),
        },
        "scoring_games": {
            "completed": scoring_completed,
            "planned": scoring_planned,
            "terminal_unplayed": terminal_unplayed,
        },
    }


def _swiss_byes(
    stage: dict[str, Any],
    pairings: list[dict[str, Any]],
    *,
    expected_contest_id: int | None = None,
    expected_entry_bots: dict[int, int | None] | None = None,
    expected_entry_users: dict[int, int] | None = None,
    require_current_entry_bots: bool = False,
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
        ) and (
            expected_contest_id is None
            or contest_pairing_roster_binding_is_valid(
                pairing,
                expected_contest_id=expected_contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
                require_opponent=False,
            )
        ):
            counts[int(entry_a_id)] += 1
    return dict(counts)


def _rank_rows(
    rows: list[dict[str, Any]],
    *,
    grouped: bool,
    use_persisted_rank: bool = False,
    use_computed_rank: bool = False,
) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    if use_persisted_rank and rows and all(
        isinstance(row.get("_persisted_rank"), int)
        and not isinstance(row.get("_persisted_rank"), bool)
        and int(row["_persisted_rank"]) >= 1
        for row in rows
    ):
        ordered = sorted(
            rows,
            key=lambda row: (
                str(row.get("group_id") or "") if grouped else "",
                int(row["_persisted_rank"]),
                int(row.get("entry_id") or 0),
            ),
        )
        for row in ordered:
            row["rank"] = int(row.pop("_persisted_rank"))
        return ordered
    if (
        use_computed_rank
        and rows
        and all(
            isinstance(row.get("_computed_rank"), int)
            and not isinstance(row.get("_computed_rank"), bool)
            and int(row["_computed_rank"]) >= 1
            for row in rows
        )
    ):
        if grouped:
            groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for row in rows:
                groups[str(row.get("group_id") or "未分组")].append(row)
            for group_id in sorted(groups):
                group_rows = sorted(
                    groups[group_id],
                    key=lambda row: (
                        int(row["_computed_rank"]),
                        int(row.get("entry_id") or 0),
                    ),
                )
                for rank, row in enumerate(group_rows, 1):
                    row["rank"] = rank
                    row["group_id"] = group_id
                    ordered.append(row)
        else:
            ordered = sorted(
                rows,
                key=lambda row: (
                    int(row["_computed_rank"]),
                    int(row.get("entry_id") or 0),
                ),
            )
            for rank, row in enumerate(ordered, 1):
                row["rank"] = rank
        return ordered
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
    *,
    stage_results: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return persisted/live standings for each stage's actual participants.

    ``ContestManager.standings`` intentionally initializes every contest entry.
    That is useful for lifecycle calculations, but would leak non-qualifiers as
    zero-point rows in a later knockout stage.  The presentation contract instead
    intersects every ranking with entry ids materialized in that stage's pairing
    graph.
    """
    stages = _stages(contest)
    raw_current_idx = contest.get("current_stage_idx", 0)
    current_idx_valid = bool(
        isinstance(raw_current_idx, int)
        and not isinstance(raw_current_idx, bool)
        and 0 <= raw_current_idx < len(stages)
    )
    current_idx = raw_current_idx if current_idx_valid else -1
    identity_roster_valid = all(
        isinstance(entry.get("id"), int)
        and not isinstance(entry.get("id"), bool)
        and isinstance(entry.get("user_id"), int)
        and not isinstance(entry.get("user_id"), bool)
        for entry in entries
    )
    valid_entries = [
        entry
        for entry in entries
        if isinstance(entry.get("id"), int)
        and not isinstance(entry.get("id"), bool)
        and isinstance(entry.get("user_id"), int)
        and not isinstance(entry.get("user_id"), bool)
    ]
    active_entries = active_contest_entries(valid_entries)
    elimination_roster_valid = bool(
        len(valid_entries) == len(entries) and active_entries is not None
    )
    roster_valid = identity_roster_valid and elimination_roster_valid
    entry_by_id = {entry["id"]: entry for entry in valid_entries}
    expected_entry_bots = {
        entry["id"]: entry.get("bot_id") for entry in valid_entries
    }
    expected_entry_users = {
        entry["id"]: entry["user_id"] for entry in valid_entries
    }
    pairing_by_stage: dict[int, list[dict[str, Any]]] = defaultdict(list)
    pairing_coordinates_valid = True
    for pairing in pairings:
        raw_stage_idx = pairing.get("stage_idx")
        if (
            isinstance(raw_stage_idx, bool)
            or not isinstance(raw_stage_idx, int)
            or raw_stage_idx < 0
            or raw_stage_idx >= len(stages)
        ):
            pairing_coordinates_valid = False
            continue
        pairing_by_stage[raw_stage_idx].append(pairing)

    persisted_by_stage: dict[int, list[dict[str, Any]]] = defaultdict(list)
    persisted_rows = (
        stage_results
        if stage_results is not None
        else manager.store.list_stage_results(int(contest["id"]))
    )
    persisted_coordinates_valid = True
    for row in persisted_rows:
        raw_stage_idx = row.get("stage_idx")
        if (
            isinstance(raw_stage_idx, bool)
            or not isinstance(raw_stage_idx, int)
            or raw_stage_idx < 0
            or raw_stage_idx >= len(stages)
            or isinstance(row.get("entry_id"), bool)
            or not isinstance(row.get("entry_id"), int)
        ):
            persisted_coordinates_valid = False
            continue
        persisted_by_stage[raw_stage_idx].append(row)

    active_participants = {
        entry["id"] for entry in (active_entries or [])
    }
    future_cohort_sizes = _future_stage_cohort_sizes(
        stages,
        current_idx=current_idx,
        active_count=len(active_participants),
        game_id=str(contest.get("game_id") or ""),
    )

    result: list[dict[str, Any]] = []
    for stage_idx, stage in enumerate(stages):
        game_id = str(contest.get("game_id") or "")
        stage_semantics_valid = bool(
            current_idx_valid
            and roster_valid
            and pairing_coordinates_valid
            and persisted_coordinates_valid
            and stage_scoring_contract_is_valid(stage, game_id=game_id)
        )
        require_current_entry_bots = bool(
            stage_idx == current_idx
            and contest.get("status") in ("published", "running")
        )
        stage_pairings = pairing_by_stage.get(stage_idx, [])
        participant_ids = _participants(stage_pairings)
        bye_counts = _swiss_byes(
            stage,
            stage_pairings,
            expected_contest_id=int(contest["id"]),
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
        persisted = persisted_by_stage.get(stage_idx, [])
        persisted_participants = {
            int(row["entry_id"])
            for row in persisted
            if isinstance(row.get("entry_id"), int)
            and not isinstance(row.get("entry_id"), bool)
        }
        if not roster_valid:
            # A malformed SQLite elimination flag makes the frozen cohort
            # unknowable.  Preserve bounded fixture totals below, but do not
            # guess participants, ranking completeness, or advancement.
            expected_participants: set[int] = set()
            expected_participant_count = None
            topology_participants = None
            visible_participants: set[int] = set()
        else:
            expected_participants = (
                persisted_participants
                if persisted_participants
                else active_participants
                if stage_idx == current_idx
                and stage.get("series_scoring") in {
                    SERIES_SCORING_INDEPENDENT,
                    SERIES_SCORING_AGGREGATE,
                }
                else participant_ids
                if participant_ids
                else active_participants
                if stage_idx == current_idx
                else participant_ids
            )
            expected_participant_count = (
                len(expected_participants)
                if expected_participants
                else future_cohort_sizes.get(stage_idx)
                if stage_idx > current_idx
                else len(expected_participants)
            )
            topology_participants = (
                expected_participants
                if expected_participants or expected_participant_count == 0
                else None
            )
            visible_participants = (
                expected_participants
                if stage.get("series_scoring") in {
                    SERIES_SCORING_INDEPENDENT,
                    SERIES_SCORING_AGGREGATE,
                }
                else participant_ids
            )
        expected_rounds = (
            effective_swiss_rounds(stage, expected_participant_count)
            if stage_semantics_valid and stage.get("type") == "swiss"
            and expected_participant_count is not None
            else None
        )
        live_rows = (
            manager.standings(
                int(contest["id"]),
                stage_idx=stage_idx,
                pairings=stage_pairings,
                entries=entries,
                contest=contest,
            )
            if stage_pairings and stage_semantics_valid
            else []
        )
        live_counts = {
            row["entry_id"]: row.get("counts") or {
                "encounter_groups": 0,
                "unique_opponents": 0,
                "match_jobs": 0,
                "scoring_games": 0,
            }
            for row in live_rows
            if isinstance(row.get("entry_id"), int)
            and not isinstance(row.get("entry_id"), bool)
        }
        if persisted and stage_semantics_valid:
            source_rows = persisted
            source = "persisted"
        elif stage_pairings:
            source_rows = live_rows
            source = "live" if any(p.get("match_id") for p in stage_pairings) else "scheduled"
        else:
            source_rows = []
            source = "pending"

        ranking_complete = bool(
            stage_semantics_valid
            and (
                set(persisted_participants) == set(expected_participants)
                if source == "persisted"
                else {
                    row.get("entry_id")
                    for row in live_rows
                    if isinstance(row.get("entry_id"), int)
                    and not isinstance(row.get("entry_id"), bool)
                }
                == set(expected_participants)
            )
        )

        rows: list[dict[str, Any]] = []
        for source_row in source_rows:
            entry_id = source_row.get("entry_id")
            if (
                isinstance(entry_id, bool)
                or not isinstance(entry_id, int)
                or entry_id not in visible_participants
            ):
                continue
            entry = entry_by_id.get(entry_id, {})
            if source == "persisted":
                historical_bot_id = source_row.get("bot_id")
                bot_id = historical_bot_id
                bot_name = (
                    source_row.get("bot_display")
                    or source_row.get("bot_name")
                    or "历史 Bot"
                ) if source_row.get("bot_name") or source_row.get("bot_display") else "历史 Bot（已删除）"
            else:
                bot_id = entry.get("bot_id")
                bot_name = entry.get("bot_display") or entry.get("bot_name")
            rows.append(
                {
                    "entry_id": entry_id,
                    "bot_id": bot_id,
                    "bot_name": bot_name,
                    "owner_name": entry.get("owner_name") or entry.get("username"),
                    "owner_display": entry.get("owner_display"),
                    "points": float(source_row.get("points") or 0),
                    "wins": int(source_row.get("wins") or 0),
                    "draws": int(source_row.get("draws") or 0),
                    "losses": int(source_row.get("losses") or 0),
                    "byes": bye_counts.get(int(entry_id), 0),
                    "counts": live_counts.get(
                        int(entry_id),
                        {
                            "encounter_groups": 0,
                            "unique_opponents": 0,
                            "match_jobs": 0,
                            "scoring_games": 0,
                        },
                    ),
                    "delta_total": int(source_row.get("delta_total") or 0),
                    "group_id": source_row.get("group_id") or entry.get("group_id") or "",
                    "_persisted_rank": (
                        int(source_row.get("rank_in_group") or 0)
                        if source == "persisted"
                        else None
                    ),
                    "_computed_rank": (
                        int(source_row.get("rank") or 0)
                        if source != "persisted"
                        else None
                    ),
                    "tiebreaks": sanitize_public_contest_tiebreaks(
                        source_row.get("tiebreaks")
                    ),
                }
            )

        grouped = str(stage.get("type") or "").startswith("group_")
        rows = _rank_rows(
            rows,
            grouped=grouped,
            use_persisted_rank=(source == "persisted"),
            use_computed_rank=(
                source != "persisted"
                and stage.get("series_scoring")
                in {
                    SERIES_SCORING_AGGREGATE,
                    SERIES_SCORING_INDEPENDENT,
                }
            ),
        )
        for row in rows:
            row.pop("_persisted_rank", None)
            row.pop("_computed_rank", None)
            if row.get("tiebreaks") is None:
                row.pop("tiebreaks", None)
        stage_type = stage.get("type")
        game_spec = game_registry.get(game_id)
        completed_count = 0
        for pairing in stage_pairings if stage_semantics_valid else []:
            if is_authoritative_no_opponent_pairing(stage_type, pairing):
                completed_count += int(
                    contest_pairing_roster_binding_is_valid(
                        pairing,
                        expected_contest_id=int(contest["id"]),
                        expected_entry_bots=expected_entry_bots,
                        expected_entry_users=expected_entry_users,
                        require_current_entry_bots=require_current_entry_bots,
                        require_opponent=False,
                    )
                )
                continue
            if pairing.get("match_id") is not None and match_scoring_result_is_valid(
                stage,
                _pairing_match(pairing),
                game_spec=game_spec,
                pairing=pairing,
                expected_contest_id=int(contest["id"]),
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            ):
                completed_count += 1
        aggregate_complete = stage_semantics_valid
        if stage_semantics_valid and "games_per_pair" in stage:
            real_pairings = [
                pairing
                for pairing in stage_pairings
                if not is_authoritative_no_opponent_pairing(stage_type, pairing)
            ]
            # ``contest_bracket`` 已在同一批查询里附带公开 Match 摘要。阶段详情
            # 这里只需要 series 坐标与 completed 状态，禁止再按 pairing 逐条
            # ``get_match``，否则大规模 K 系列会退化成 N+1 查询。
            match_projection = {
                str(pairing["match_id"]): _pairing_match(pairing)
                for pairing in real_pairings
                if pairing.get("match_id") is not None
            }
            aggregate_complete = series_rows_settled(
                stage,
                real_pairings,
                match_projection.get,
                game_spec=game_spec,
                all_pairings=stage_pairings,
                expected_entry_ids=topology_participants,
                expected_swiss_rounds=expected_rounds,
                expected_contest_id=int(contest["id"]),
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )
        all_completed = stage_semantics_valid and ranking_complete and bool(stage_pairings) and (
            completed_count == len(stage_pairings) and aggregate_complete
        )

        next_ids = _participants(pairing_by_stage.get(stage_idx + 1, []))
        advancement_final = bool(next_ids) or all_completed
        advancement_ids = next_ids or (
            _advancement_zone(stage, rows)
            if stage_semantics_valid
            and rows
            and (all_completed or completed_count)
            else set()
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

        counts = build_stage_counts(
            stage,
            stage_pairings,
            game_id=str(contest.get("game_id") or ""),
            expected_entry_ids=topology_participants,
            expected_entry_count=expected_participant_count,
            expected_swiss_rounds=expected_rounds,
            expected_contest_id=int(contest["id"]),
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
        if not stage_semantics_valid:
            counts["encounter_groups"]["completed"] = 0
            counts["match_jobs"]["completed"] = 0
            counts["scoring_games"]["completed"] = 0
            counts["scoring_games"]["terminal_unplayed"] = 0
        topology = expected_stage_topology(
            stage,
            topology_participants,
            expected_entry_count=expected_participant_count,
            expected_swiss_rounds=expected_rounds,
            game_id=game_id,
        )
        expected_pairing_rows = (
            int(topology["pairing_rows"])
            if topology is not None
            else len(stage_pairings)
        )
        result.append(
            {
                "stage_idx": stage_idx,
                "stage_key": stage.get("key") or f"stage{stage_idx}",
                "status": status,
                "source": source,
                "completed_pairings": completed_count,
                "total_pairings": expected_pairing_rows,
                "counts": counts,
                "advancement_final": advancement_final,
                "rows": rows,
            }
        )
    return result


__all__ = [
    "build_stage_counts",
    "build_stage_summaries",
    "expected_stage_topology",
]
