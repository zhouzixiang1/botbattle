"""Read models for contest stage history.

Lifecycle writes stay in :mod:`manager`; this module only combines immutable
stage snapshots with current match progress for the detail API.
"""
from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from typing import Any

from bzplat.backend.contests.manager import (
    _clean_group_id,
    advancing_entry_ids as _advancing_entry_ids,
    complete_round_robin_pairing_topology as _complete_round_robin_pairing_topology,
    complete_single_elimination_pairing_topology as _shared_complete_single_elimination_topology,
    complete_swiss_pairing_topology as _shared_complete_swiss_pairing_topology,
    complete_traditional_group_pairing_topology as _complete_group_pairing_topology,
    current_stage_topology_seal_is_valid as _current_stage_topology_seal_is_valid,
    prove_current_stage_participants as _prove_current_stage_participants,
    traditional_group_authority as _traditional_group_authority,
)
from bzplat.backend.contests.series import (
    contest_match_binding_is_valid,
    contest_pairing_roster_binding_is_valid,
    group_conceptual_series,
    is_aggregate_series_stage,
    match_scoring_result_is_valid,
    series_rows_settled,
    summarize_elimination_encounter,
    summarize_conceptual_series,
)
from bzplat.backend.contests.stages import (
    effective_group_count,
    effective_swiss_rounds,
    next_power_of_two,
)
from bzplat.backend.contests.validation import (
    ELIMINATION_TIEBREAK_PAIRED_SWAP,
    SERIES_SCORING_AGGREGATE,
    SERIES_SCORING_INDEPENDENT,
    active_contest_entries,
    complete_group_rank_coordinates,
    reserved_group_markers_match_template,
    stage_duplicate_mode,
    stage_scoring_contract_is_valid,
    validate_nonterminal_elimination_advancement,
    validate_stage_ranking_topology,
    validated_random_group_format_snapshot,
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
from bzplat.backend.store.validation import (
    exact_nonnegative_int,
    is_authoritative_no_opponent_pairing,
)


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


def public_format_snapshot(contest: dict[str, Any]) -> dict[str, Any] | None:
    """Return the bounded public audit projection of a frozen format draw.

    Full draw order and membership stay in the durable snapshot for recovery,
    but are exposed through entry ``group_id`` and official tie-break fields.
    This function never returns the raw JSON column or any private random seed.
    """
    snapshot = validated_random_group_format_snapshot(contest)
    if snapshot is None:
        return None
    version = snapshot["version"]
    algorithm = snapshot["algorithm"]
    group_count = snapshot["group_count"]
    audit = snapshot["audit_digest"]
    sizes = snapshot["group_sizes"]
    public: dict[str, Any] = {
        "version": version,
        "algorithm": algorithm,
        "audit_digest": audit,
        "group_count": group_count,
        "group_size_min": min(sizes.values()),
        "group_size_max": max(sizes.values()),
    }
    # Small official formats benefit from the exact per-label summary.  Large
    # Pencil draws remain bounded here; full membership is already public via
    # paginated contest entries and never needs to duplicate a huge JSON map.
    if group_count <= 64:
        public["group_sizes"] = dict(sorted(sizes.items()))
    if algorithm == "protected_seed_random_balanced_v1":
        public["expected_match_count"] = snapshot["expected_match_count"]
    source = snapshot.get("source")
    if source is not None:
        public["source"] = {
            "contest_id": source["contest_id"],
            "protected": [dict(row) for row in source["protected"]],
        }
    return public


def _participants(pairings: list[dict[str, Any]]) -> set[int]:
    result: set[int] = set()
    for pairing in pairings:
        for key in ("entry_a_id", "entry_b_id"):
            value = pairing.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                result.add(value)
    return result


def _complete_legacy_pairing_cohort(
    stage: dict[str, Any],
    stage_pairings: list[dict[str, Any]],
    participants: set[int],
    expected_participant_count: int | None,
) -> bool:
    """Prove a legacy cohort from a complete materialized pairing graph."""
    if (
        not stage_pairings
        or isinstance(expected_participant_count, bool)
        or not isinstance(expected_participant_count, int)
        or expected_participant_count < 1
        or len(participants) != expected_participant_count
    ):
        return False
    stage_type = stage.get("type")
    if str(stage_type or "").startswith("group_"):
        return (
            _complete_group_pairing_topology(
                stage, participants, stage_pairings
            )
            is not None
        )

    observed_pairs: Counter[tuple[int, int]] = Counter()
    observed_directions: Counter[tuple[int, int]] = Counter()
    for pairing in stage_pairings:
        entry_a_id = pairing.get("entry_a_id")
        entry_b_id = pairing.get("entry_b_id")
        if (
            isinstance(entry_a_id, bool)
            or not isinstance(entry_a_id, int)
            or entry_a_id not in participants
            or (
                entry_b_id is not None
                and (
                    isinstance(entry_b_id, bool)
                    or not isinstance(entry_b_id, int)
                    or entry_b_id not in participants
                    or entry_b_id == entry_a_id
                )
            )
        ):
            return False
        if entry_b_id is not None:
            observed_pairs[tuple(sorted((entry_a_id, entry_b_id)))] += 1
            observed_directions[(entry_a_id, entry_b_id)] += 1

    if stage_type not in {"round_robin", "double_round_robin"}:
        # Swiss and elimination histories may contain several incrementally
        # materialized rounds.  Exact projected cardinality plus every pairing
        # seat covering only that cohort is the strongest bounded legacy proof.
        return _participants(stage_pairings) == participants

    members = sorted(participants)
    expected_pairs = {
        (members[left], members[right])
        for left in range(len(members))
        for right in range(left + 1, len(members))
    }
    if "games_per_pair" in stage:
        expected_multiplicity = stage.get("games_per_pair")
        if (
            isinstance(expected_multiplicity, bool)
            or not isinstance(expected_multiplicity, int)
            or expected_multiplicity < 1
        ):
            return False
    else:
        expected_multiplicity = 2 if stage_type == "double_round_robin" else 1
    if observed_pairs != Counter(
        {pair: expected_multiplicity for pair in expected_pairs}
    ):
        return False
    if stage_type == "double_round_robin" and "games_per_pair" not in stage:
        return all(
            observed_directions[(first, second)] == 1
            and observed_directions[(second, first)] == 1
            for first, second in expected_pairs
        )
    return True


def _frozen_group_membership(
    expected_participants: set[int],
    entry_by_id: dict[int, dict[str, Any]],
) -> tuple[dict[int, str] | None, bool]:
    """Read an all-or-nothing frozen group assignment from the roster."""
    groups: dict[int, str] = {}
    missing = False
    for entry_id in expected_participants:
        raw_group = entry_by_id.get(entry_id, {}).get("group_id")
        if raw_group == "":
            missing = True
            continue
        group_id = _clean_group_id(raw_group)
        if group_id is None:
            return None, False
        groups[entry_id] = group_id
    if missing:
        return (None, True) if not groups else (None, False)
    return groups, True


def _complete_swiss_pairing_topology(
    stage: dict[str, Any],
    expected_participants: set[int],
    stage_pairings: list[dict[str, Any]],
    *,
    expected_rounds: int | None,
) -> bool:
    """Prove every Swiss round covers the exact cohort once conceptually."""
    if (
        isinstance(expected_rounds, bool)
        or not isinstance(expected_rounds, int)
        or expected_rounds < 0
    ):
        return False
    if not stage_pairings:
        return len(expected_participants) <= 1 and expected_rounds == 0
    games_per_pair = stage.get("games_per_pair", 1)
    if (
        isinstance(games_per_pair, bool)
        or not isinstance(games_per_pair, int)
        or games_per_pair < 1
    ):
        return False

    by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for pairing in stage_pairings:
        round_num = pairing.get("round_num")
        if (
            isinstance(round_num, bool)
            or not isinstance(round_num, int)
            or round_num < 1
        ):
            return False
        by_round[round_num].append(pairing)
    if sorted(by_round) != list(range(1, expected_rounds + 1)):
        return False

    for round_rows in by_round.values():
        participants: set[int] = set()
        observed_pairs: Counter[tuple[int, int]] = Counter()
        byes = 0
        for pairing in round_rows:
            entry_a_id = pairing.get("entry_a_id")
            entry_b_id = pairing.get("entry_b_id")
            if (
                isinstance(entry_a_id, bool)
                or not isinstance(entry_a_id, int)
                or entry_a_id not in expected_participants
            ):
                return False
            if entry_b_id is None:
                if (
                    entry_a_id in participants
                    or not is_authoritative_no_opponent_pairing("swiss", pairing)
                ):
                    return False
                participants.add(entry_a_id)
                byes += 1
                continue
            if (
                isinstance(entry_b_id, bool)
                or not isinstance(entry_b_id, int)
                or entry_b_id not in expected_participants
                or entry_b_id == entry_a_id
            ):
                return False
            observed_pairs[tuple(sorted((entry_a_id, entry_b_id)))] += 1
        for (entry_a_id, entry_b_id), multiplicity in observed_pairs.items():
            if (
                multiplicity != games_per_pair
                or entry_a_id in participants
                or entry_b_id in participants
            ):
                return False
            participants.update((entry_a_id, entry_b_id))
        if (
            participants != expected_participants
            or byes != len(expected_participants) % 2
        ):
            return False
    return True


def _complete_single_elimination_topology(
    stage: dict[str, Any],
    expected_participants: set[int],
    stage_pairings: list[dict[str, Any]],
    *,
    game_id: str,
    contest_id: int,
    expected_entry_bots: dict[int, int | None],
    expected_entry_users: dict[int, int],
    require_current_entry_bots: bool,
) -> bool:
    """Follow every decided bracket round until exactly one champion remains."""
    if len(expected_participants) < 2 or not stage_pairings:
        return False
    by_round_slot: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for pairing in stage_pairings:
        round_num = pairing.get("round_num")
        bracket_slot = pairing.get("bracket_slot")
        if (
            isinstance(round_num, bool)
            or not isinstance(round_num, int)
            or round_num < 1
            or isinstance(bracket_slot, bool)
            or not isinstance(bracket_slot, int)
            or bracket_slot < 0
        ):
            return False
        by_round_slot[(round_num, bracket_slot)].append(pairing)
    rounds = sorted({round_num for round_num, _slot in by_round_slot})
    if rounds != list(range(1, rounds[-1] + 1)):
        return False

    matches = {
        str(pairing["match_id"]): match
        for pairing in stage_pairings
        if pairing.get("match_id")
        if (match := _pairing_match(pairing)) is not None
    }
    game_spec = game_registry.get(game_id)
    current_participants = set(expected_participants)
    previous_winners: list[int] | None = None
    for round_num in rounds:
        slots = {
            slot: rows
            for (row_round, slot), rows in by_round_slot.items()
            if row_round == round_num
        }
        expected_slot_count = (
            next_power_of_two(len(expected_participants)) // 2
            if round_num == 1
            else (len(previous_winners or []) + 1) // 2
        )
        if sorted(slots) != list(range(expected_slot_count)):
            return False
        round_participants: set[int] = set()
        winners: list[int] = []
        for slot, rows in sorted(slots.items()):
            expected_slot_participants = (
                None
                if previous_winners is None
                else set(previous_winners[slot * 2 : slot * 2 + 2])
            )
            if len(rows) == 1 and is_authoritative_no_opponent_pairing(
                "single_elimination", rows[0]
            ):
                pairing = rows[0]
                entry_a_id = pairing.get("entry_a_id")
                tiebreak_group = pairing.get("tiebreak_group", 0)
                tiebreak_game = pairing.get("tiebreak_game", 0)
                if (
                    isinstance(entry_a_id, bool)
                    or not isinstance(entry_a_id, int)
                    or entry_a_id not in current_participants
                    or entry_a_id in round_participants
                    or (
                        expected_slot_participants is not None
                        and expected_slot_participants != {entry_a_id}
                    )
                    or isinstance(tiebreak_group, bool)
                    or not isinstance(tiebreak_group, int)
                    or tiebreak_group != 0
                    or isinstance(tiebreak_game, bool)
                    or not isinstance(tiebreak_game, int)
                    or tiebreak_game != 0
                    or not contest_pairing_roster_binding_is_valid(
                        pairing,
                        expected_contest_id=contest_id,
                        expected_entry_bots=expected_entry_bots,
                        expected_entry_users=expected_entry_users,
                        require_current_entry_bots=require_current_entry_bots,
                        require_opponent=False,
                    )
                ):
                    return False
                round_participants.add(entry_a_id)
                winners.append(entry_a_id)
                continue

            summary = summarize_elimination_encounter(
                stage,
                rows,
                matches.get,
                game_spec=game_spec,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )
            entry_a_id = summary.get("entry_a_id")
            entry_b_id = summary.get("entry_b_id")
            winner_entry = summary.get("winner_entry")
            if (
                summary.get("state") != "decided"
                or isinstance(entry_a_id, bool)
                or not isinstance(entry_a_id, int)
                or isinstance(entry_b_id, bool)
                or not isinstance(entry_b_id, int)
                or entry_a_id == entry_b_id
                or entry_a_id not in current_participants
                or entry_b_id not in current_participants
                or entry_a_id in round_participants
                or entry_b_id in round_participants
                or (
                    expected_slot_participants is not None
                    and expected_slot_participants != {entry_a_id, entry_b_id}
                )
                or winner_entry not in {entry_a_id, entry_b_id}
            ):
                return False
            round_participants.update((entry_a_id, entry_b_id))
            winners.append(int(winner_entry))
        if round_participants != current_participants:
            return False
        if len(winners) == 1:
            return round_num == rounds[-1]
        if round_num == 1 and sum(
            len(rows) == 1
            and is_authoritative_no_opponent_pairing("single_elimination", rows[0])
            for rows in slots.values()
        ) != next_power_of_two(len(expected_participants)) - len(
            expected_participants
        ):
            return False
        previous_winners = winners
        current_participants = set(winners)
    return False


def _complete_stage_pairing_topology(
    stage: dict[str, Any],
    expected_participants: set[int],
    stage_pairings: list[dict[str, Any]],
    *,
    expected_groups: dict[int, str] | None,
    group_authority_valid: bool,
    expected_swiss_rounds: int | None,
    game_id: str,
    contest_id: int,
    expected_entry_bots: dict[int, int | None],
    expected_entry_users: dict[int, int],
    require_current_entry_bots: bool,
) -> bool:
    """Gate completion on the full frozen topology without hiding live rows."""
    stage_type = stage.get("type")
    if stage_type in {"round_robin", "double_round_robin"}:
        return _complete_round_robin_pairing_topology(
            stage, expected_participants, stage_pairings
        )
    if stage_type in {"group_round_robin", "group_double_round_robin"}:
        return bool(
            group_authority_valid
            and _complete_group_pairing_topology(
                stage,
                expected_participants,
                stage_pairings,
                expected_groups=expected_groups,
            )
            is not None
        )
    if stage_type == "swiss":
        return _shared_complete_swiss_pairing_topology(
            stage,
            expected_participants,
            stage_pairings,
            expected_rounds=expected_swiss_rounds,
        )
    if stage_type == "single_elimination":
        matches = {
            str(pairing["match_id"]): match
            for pairing in stage_pairings
            if pairing.get("match_id")
            if (match := _pairing_match(pairing)) is not None
        }
        return _shared_complete_single_elimination_topology(
            stage,
            expected_participants,
            stage_pairings,
            get_match=matches.get,
            game_id=game_id,
            contest_id=contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
            require_champion=True,
        )
    return False


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


def build_elimination_tiebreak_projection(
    stage: dict[str, Any],
    pairings: list[dict[str, Any]],
    *,
    game_id: str,
    contest_id: int,
    expected_entry_bots: dict[int, int | None],
    expected_entry_users: dict[int, int],
    require_current_entry_bots: bool,
    project_legacy_draw_blocked: bool = False,
) -> dict[str, Any] | None:
    """Build bounded public state for one authoritative elimination policy.

    Current paired-swap stages expose their complete decision-group progress.
    A running historical stage without that marker exposes only an explicit
    blocked sentinel when the authoritative encounter summarizer proves that a
    completed draw is the latest bracket round.  Pending, decisive and already
    advanced historical encounters remain absent, so readers never infer a
    block from ``winner=NULL`` or raw result payloads.
    """
    if stage.get("type") != "single_elimination":
        return None
    paired_swap = stage.get("tiebreak") == ELIMINATION_TIEBREAK_PAIRED_SWAP
    legacy_stage = "tiebreak" not in stage
    if not paired_swap and not (project_legacy_draw_blocked and legacy_stage):
        return None
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for row in pairings:
        round_num = row.get("round_num")
        slot = row.get("bracket_slot")
        if (
            isinstance(round_num, bool)
            or not isinstance(round_num, int)
            or round_num < 1
            or isinstance(slot, bool)
            or not isinstance(slot, int)
            or slot < 0
        ):
            if not paired_swap:
                return None
            return {
                "mode": ELIMINATION_TIEBREAK_PAIRED_SWAP,
                "unbounded": True,
                "state": "invalid",
                "encounters": [],
            }
        grouped[(round_num, slot)].append(row)
    spec = game_registry.get(game_id)
    matches = {
        str(row["match_id"]): match
        for row in pairings
        if row.get("match_id")
        if (match := _pairing_match(row)) is not None
    }
    encounters: list[dict[str, Any]] = []
    legacy_blocked: list[dict[str, Any]] = []
    latest_round = max((coordinate[0] for coordinate in grouped), default=0)
    for (round_num, slot), rows in sorted(grouped.items()):
        primary = next(
            (
                row
                for row in rows
                if row.get("tiebreak_group", 0) == 0
                and row.get("tiebreak_game", 0) == 0
            ),
            rows[0],
        )
        label_a = primary.get("bot_a_display") or primary.get("bot_a_name")
        label_b = primary.get("bot_b_display") or primary.get("bot_b_name")
        if len(rows) == 1 and is_authoritative_no_opponent_pairing(
            stage.get("type"), rows[0]
        ):
            encounters.append(
                {
                    "round_num": round_num,
                    "bracket_slot": slot,
                    "state": "bye",
                    "entry_a_id": rows[0].get("entry_a_id"),
                    "entry_b_id": rows[0].get("entry_b_id"),
                    "entry_a_label": label_a,
                    "entry_b_label": label_b,
                    "winner_entry_id": rows[0].get("entry_a_id"),
                    "next_tiebreak_group": None,
                    "current_tiebreak_group": 0,
                    "completed_tiebreak_games": 0,
                    "tiebreak_games_in_group": 0,
                    "groups": [],
                }
            )
            continue
        summary = summarize_elimination_encounter(
            stage,
            rows,
            matches.get,
            game_spec=spec,
            expected_contest_id=contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
        if not paired_swap:
            if summary["state"] == "legacy_draw" and round_num == latest_round:
                legacy_blocked.append(
                    {
                        "round_num": round_num,
                        "bracket_slot": slot,
                        "state": "legacy_draw_blocked",
                        "entry_a_label": label_a,
                        "entry_b_label": label_b,
                    }
                )
            continue
        encounters.append(
            {
                "round_num": round_num,
                "bracket_slot": slot,
                "state": summary["state"],
                "entry_a_id": summary.get("entry_a_id"),
                "entry_b_id": summary.get("entry_b_id"),
                "entry_a_label": label_a,
                "entry_b_label": label_b,
                "winner_entry_id": summary.get("winner_entry"),
                "next_tiebreak_group": summary.get("next_tiebreak_group"),
                "current_tiebreak_group": int(
                    summary.get("current_tiebreak_group") or 0
                ),
                "completed_tiebreak_games": int(
                    summary.get("completed_tiebreak_games") or 0
                ),
                "tiebreak_games_in_group": int(
                    summary.get("tiebreak_games_in_group") or 0
                ),
                "groups": [
                    {
                        "group": int(group["group"]),
                        "state": str(group["state"]),
                        "completed_games": int(group["completed_games"]),
                        "planned_games": int(group["planned_games"]),
                        "points_a": float(group["points_a"]),
                        "points_b": float(group["points_b"]),
                    }
                    for group in summary.get("groups", [])
                    if isinstance(group, dict)
                ],
            }
        )
    if not paired_swap:
        if not legacy_blocked:
            return None
        return {
            "mode": "legacy_draw_blocked",
            "unbounded": False,
            "state": "legacy_draw_blocked",
            "encounters": legacy_blocked,
        }
    state = "active"
    if any(item["state"] == "invalid" for item in encounters):
        state = "invalid"
    elif encounters and all(item["state"] in {"decided", "bye"} for item in encounters):
        state = "decided"
    return {
        "mode": ELIMINATION_TIEBREAK_PAIRED_SWAP,
        "unbounded": True,
        "state": state,
        "encounters": encounters,
    }


def expected_stage_topology(
    stage: dict[str, Any],
    expected_entry_ids: set[int] | list[int] | tuple[int, ...] | None,
    *,
    expected_entry_count: int | None = None,
    expected_entry_groups: dict[int, str] | None = None,
    expected_swiss_rounds: int | None = None,
    game_id: str | None = None,
) -> dict[str, int] | None:
    """Derive fixed stage totals from the frozen cohort, never existing rows."""
    if (
        not stage_scoring_contract_is_valid(stage, game_id=game_id)
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
    stage_type = stage.get("type")
    explicit_games = "games_per_pair" in stage
    games_per_pair = stage.get("games_per_pair", 1)
    if (
        isinstance(games_per_pair, bool)
        or not isinstance(games_per_pair, int)
        or games_per_pair < 1
    ):
        return None
    if stage_type in {"round_robin", "double_round_robin"}:
        encounters = cohort_count * max(0, cohort_count - 1) // 2
        byes = 0
        match_multiplier = (
            games_per_pair
            if explicit_games
            else 2 if stage_type == "double_round_robin" else 1
        )
    elif stage_type in {"group_round_robin", "group_double_round_robin"}:
        if expected_entry_groups is not None:
            if (
                expected_entry_ids is None
                or set(expected_entry_groups) != cohort
                or any(
                    _clean_group_id(group_id) != group_id
                    for group_id in expected_entry_groups.values()
                )
            ):
                return None
            group_sizes = Counter(expected_entry_groups.values()).values()
        else:
            requested_group_count = stage.get("group_count", 4)
            if (
                isinstance(requested_group_count, bool)
                or not isinstance(requested_group_count, int)
                or requested_group_count < 1
            ):
                return None
            group_count = effective_group_count(
                cohort_count, requested_group_count
            )
            base_size, larger_groups = divmod(cohort_count, group_count)
            group_sizes = [
                base_size + int(index < larger_groups)
                for index in range(group_count)
            ]
        encounters = sum(size * max(0, size - 1) // 2 for size in group_sizes)
        byes = 0
        match_multiplier = (
            2 if stage_type == "group_double_round_robin" else 1
        )
    elif (
        stage_type == "swiss"
        and isinstance(expected_swiss_rounds, int)
        and not isinstance(expected_swiss_rounds, bool)
        and expected_swiss_rounds >= 0
    ):
        encounters = (cohort_count // 2) * expected_swiss_rounds
        byes = (cohort_count % 2) * expected_swiss_rounds
        match_multiplier = games_per_pair
    else:
        return None
    match_jobs = encounters * match_multiplier
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
    expected_entry_groups: dict[int, str] | None = None,
    expected_swiss_rounds: int | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: dict[int, int | None] | None = None,
    expected_entry_users: dict[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> dict[str, Any]:
    """Count conceptual encounters, physical jobs, and direct scoring games."""
    stage_type = stage.get("type")
    all_real = [
        row
        for row in pairings
        if not is_authoritative_no_opponent_pairing(stage_type, row)
    ]
    elimination_tiebreak = bool(
        stage_type == "single_elimination"
        and stage.get("tiebreak") == ELIMINATION_TIEBREAK_PAIRED_SWAP
    )
    # Paired-swap games decide advancement only.  Keep the stage's base
    # match/scoring counts aligned with individual standings, and expose the
    # unbounded decision groups through ``elimination_tiebreak`` instead of
    # pretending they are additional stage scoring games.
    real = (
        [
            row
            for row in all_real
            if row.get("tiebreak_group", 0) == 0
            and row.get("tiebreak_game", 0) == 0
        ]
        if elimination_tiebreak
        else all_real
    )
    matches = {
        str(row["match_id"]): match
        for row in all_real
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
        expected_entry_groups=expected_entry_groups,
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

    if (
        elimination_tiebreak
        and expected_contest_id is not None
        and expected_entry_bots is not None
        and expected_entry_users is not None
    ):
        tiebreak_projection = build_elimination_tiebreak_projection(
            stage,
            pairings,
            game_id=game_id,
            contest_id=expected_contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
        encounter_completed = (
            sum(
                encounter.get("state") in {"decided", "bye"}
                for encounter in tiebreak_projection["encounters"]
            )
            if tiebreak_projection.get("state") != "invalid"
            else 0
        )

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
    cross_group_overall: bool = False,
    use_persisted_rank: bool = False,
    use_computed_rank: bool = False,
) -> list[dict[str, Any]]:
    ordered: list[dict[str, Any]] = []
    if cross_group_overall:
        # Random-group stages freeze two independent coordinates.  The table
        # itself is an overall leaderboard, so group_id/rank_in_group must not
        # regroup rows after the fair cross-group chain has assigned its unique
        # overall rank.  A damaged coordinate set cannot safely fall back to
        # raw points, delta, producer order, or entry id.
        if not grouped or not complete_group_rank_coordinates(rows):
            return []
        overall_ranks: list[int] = []
        for row in rows:
            overall_rank = row.get("overall_rank")
            if (
                isinstance(overall_rank, bool)
                or not isinstance(overall_rank, int)
                or overall_rank < 1
                or (
                    use_computed_rank
                    and row.get("_computed_rank") != overall_rank
                )
            ):
                return []
            overall_ranks.append(overall_rank)
        if set(overall_ranks) != set(range(1, len(rows) + 1)):
            return []
        ordered = sorted(rows, key=lambda row: int(row["overall_rank"]))
        for row in ordered:
            row["rank"] = int(row["overall_rank"])
        return ordered
    if use_persisted_rank:
        # Persisted rank coordinates are the historical decision itself.  A
        # duplicate, gap, missing value, or wrong type cannot be reconstructed
        # from points/delta without silently rewriting that decision.
        ranks_by_scope: dict[str, list[int]] = defaultdict(list)
        for row in rows:
            rank = row.get("_persisted_rank")
            if (
                isinstance(rank, bool)
                or not isinstance(rank, int)
                or rank < 1
            ):
                return []
            scope = str(row.get("group_id") or "") if grouped else ""
            ranks_by_scope[scope].append(rank)
        if any(
            sorted(ranks) != list(range(1, len(ranks) + 1))
            for ranks in ranks_by_scope.values()
        ):
            return []
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
    if use_computed_rank:
        # Live/computed rank is already the official decision.  It must be one
        # exact permutation per ranking scope; a missing/duplicate/gapped/bool
        # coordinate cannot fall through to points/delta or be renumbered.
        ranks_by_scope: dict[str, list[int]] = defaultdict(list)
        seen_entries: set[int] = set()
        for row in rows:
            entry_id = row.get("entry_id")
            rank = row.get("_computed_rank")
            if (
                isinstance(entry_id, bool)
                or not isinstance(entry_id, int)
                or entry_id < 1
                or entry_id in seen_entries
                or isinstance(rank, bool)
                or not isinstance(rank, int)
                or rank < 1
            ):
                return []
            seen_entries.add(entry_id)
            scope = str(row.get("group_id") or "") if grouped else ""
            ranks_by_scope[scope].append(rank)
        if any(
            sorted(ranks) != list(range(1, len(ranks) + 1))
            for ranks in ranks_by_scope.values()
        ):
            return []
        ordered = sorted(
            rows,
            key=lambda row: (
                str(row.get("group_id") or "") if grouped else "",
                int(row["_computed_rank"]),
                int(row["entry_id"]),
            ),
        )
        for row in ordered:
            row["rank"] = int(row["_computed_rank"])
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
    advanced = _advancing_entry_ids(stage, rows, default_all=False)
    return advanced if advanced is not None else set()


def _exact_persisted_ranking_rows(
    stage: dict[str, Any],
    rows: object,
    expected_entry_ids: set[int],
) -> list[dict[str, Any]] | None:
    """Validate one sealed ranking before it can authenticate a cohort.

    Historical snapshots without canonical tie-break payloads remain displayable
    in their own stage, but they cannot authorize a later active stage or the
    narrow finished-history fallback.  Those two boundaries require the exact
    auditable ranking written by the current snapshot contract.
    """
    if not isinstance(rows, list) or len(rows) != len(expected_entry_ids):
        return None
    normalized: list[dict[str, Any]] = []
    seen_entries: set[int] = set()
    for row in rows:
        if not isinstance(row, dict):
            return None
        entry_id = row.get("entry_id")
        if (
            isinstance(entry_id, bool)
            or not isinstance(entry_id, int)
            or entry_id < 1
            or entry_id not in expected_entry_ids
            or entry_id in seen_entries
            or not _persisted_stage_stats_are_valid(
                row, require_tiebreaks=True
            )
        ):
            return None
        seen_entries.add(entry_id)
        normalized.append(row)
    if seen_entries != expected_entry_ids:
        return None

    grouped = str(stage.get("type") or "").startswith("group_")
    if grouped:
        if not complete_group_rank_coordinates(normalized):
            return None
        overall_ranks = [row.get("overall_rank") for row in normalized]
        if any(
            isinstance(rank, bool)
            or not isinstance(rank, int)
            or rank < 1
            for rank in overall_ranks
        ) or set(overall_ranks) != set(range(1, len(normalized) + 1)):
            return None
    else:
        ranks = [row.get("rank") for row in normalized]
        if any(
            isinstance(rank, bool)
            or not isinstance(rank, int)
            or rank < 1
            for rank in ranks
        ) or set(ranks) != set(range(1, len(normalized) + 1)):
            return None
    return normalized


def _persisted_stage_stats_are_valid(
    row: object, *, require_tiebreaks: bool
) -> bool:
    """Validate weakly typed SQLite stage statistics without coercion.

    Old non-authoritative history may predate the canonical tie-break payload,
    so callers that merely display such a stage can leave it optional.  If the
    payload is present it must agree with the stored score; every cohort or
    current-decision authority passes ``require_tiebreaks=True``.
    """
    if not isinstance(row, dict):
        return False
    points = row.get("points")
    delta_total = row.get("delta_total")
    if (
        isinstance(points, bool)
        or not isinstance(points, (int, float))
        or not math.isfinite(points)
        or isinstance(delta_total, bool)
        or not isinstance(delta_total, int)
        or any(
            exact_nonnegative_int(row.get(field)) is None
            for field in ("wins", "draws", "losses")
        )
    ):
        return False
    tiebreaks = sanitize_public_contest_tiebreaks(row.get("tiebreaks"))
    if tiebreaks is None:
        return not require_tiebreaks
    return tiebreaks.get("points") == points


def _raw_persisted_ranking_is_exact(
    stage: dict[str, Any],
    rows: list[dict[str, Any]],
    expected_entry_ids: set[int],
) -> bool:
    """Validate raw Store stage rows for sticky cohort contradictions."""
    if len(rows) != len(expected_entry_ids):
        return False
    seen: set[int] = set()
    for row in rows:
        entry_id = row.get("entry_id")
        if (
            isinstance(entry_id, bool)
            or not isinstance(entry_id, int)
            or entry_id not in expected_entry_ids
            or entry_id in seen
            or not _persisted_stage_stats_are_valid(
                row, require_tiebreaks=True
            )
        ):
            return False
        seen.add(entry_id)
    if seen != expected_entry_ids:
        return False
    grouped = str(stage.get("type") or "").startswith("group_")
    if grouped:
        overall = [row.get("overall_rank") for row in rows]
        return bool(
            complete_group_rank_coordinates(rows)
            and all(
                isinstance(rank, int)
                and not isinstance(rank, bool)
                and rank >= 1
                for rank in overall
            )
            and set(overall) == set(range(1, len(rows) + 1))
        )
    ranks = [row.get("rank_in_group") for row in rows]
    return bool(
        all(
            isinstance(rank, int)
            and not isinstance(rank, bool)
            and rank >= 1
            for rank in ranks
        )
        and set(ranks) == set(range(1, len(rows) + 1))
    )


def _exact_persisted_summary_ranking(
    summary: object,
    stage: dict[str, Any],
    expected_entry_ids: set[int],
    *,
    require_completed: bool,
) -> list[dict[str, Any]] | None:
    if (
        not isinstance(summary, dict)
        or summary.get("source") != "persisted"
        or (require_completed and summary.get("status") != "completed")
    ):
        return None
    return _exact_persisted_ranking_rows(
        stage, summary.get("rows"), expected_entry_ids
    )


def current_stage_cohort_from_summaries(
    contest: dict[str, Any],
    entries: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
) -> set[int] | None:
    """Recover the same exact current cohort accepted by stage presentation."""
    stages = _stages(contest)
    raw_current_idx = contest.get("current_stage_idx", 0)
    if (
        isinstance(raw_current_idx, bool)
        or not isinstance(raw_current_idx, int)
        or not 0 <= raw_current_idx < len(stages)
    ):
        return None
    status = contest.get("status")
    try:
        if status in ("published", "running", "rest"):
            validate_stage_ranking_topology(stages)
        elif status != "finished":
            validate_nonterminal_elimination_advancement(stages)
    except (TypeError, ValueError):
        return None
    active_entries = active_contest_entries(entries)
    if active_entries is None:
        return None
    active_ids = {int(entry["id"]) for entry in active_entries}
    full_roster_ids = {
        int(entry["id"])
        for entry in entries
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), int)
        and not isinstance(entry.get("id"), bool)
        and int(entry["id"]) >= 1
    }
    if len(full_roster_ids) != len(entries):
        return None
    strict_active_lifecycle = status in (
        "published",
        "running",
        "rest",
    )
    summaries_by_stage = {
        summary.get("stage_idx"): summary
        for summary in summaries
        if isinstance(summary, dict)
        and isinstance(summary.get("stage_idx"), int)
        and not isinstance(summary.get("stage_idx"), bool)
    }

    current_summary = summaries_by_stage.get(raw_current_idx)
    authority_state = (
        current_summary.get("_cohort_authority_state")
        if isinstance(current_summary, dict)
        else None
    )
    if strict_active_lifecycle:
        return active_ids if authority_state == "proven" else None
    if status != "finished" or authority_state == "contradicted":
        return None

    # Finished compatibility is deliberately one-way.  Whether the predecessor
    # chain is proven or merely unavailable, the current table itself must be an
    # exact persisted artifact.  This prevents detail/live from replaying Match
    # rows under newer ranking code after the contest was sealed.  A zero-roster
    # contest has a naturally exact empty artifact.
    if not active_ids:
        current_exact = bool(
            isinstance(current_summary, dict)
            and current_summary.get("rows") == []
            and current_summary.get("total_pairings") == 0
        )
    else:
        current_exact = bool(
            _exact_persisted_summary_ranking(
                current_summary,
                stages[raw_current_idx],
                active_ids,
                require_completed=False,
            )
            is not None
        )
    return (
        active_ids
        if current_exact and authority_state in {"proven", "unknown"}
        else None
    )


def current_stage_ranking_from_summaries(
    contest: dict[str, Any],
    entries: list[dict[str, Any]],
    summaries: list[dict[str, Any]],
) -> list[dict[str, Any]] | None:
    """Return an exact immutable current-stage decision artifact.

    Finished views must never replay Match rows under the currently deployed
    ranking implementation.  The same rule applies while a completed decision
    is waiting for a rest/advance transition: once the exact artifact exists,
    both transition retries and public Top standings consume that one decision.
    A scheduled/live stage with no decision still returns ``None`` so callers
    may compute its provisional standings from the current Match projection.
    """
    if contest.get("status") not in {"published", "running", "rest", "finished"}:
        return None
    stages = _stages(contest)
    current_idx = contest.get("current_stage_idx")
    if (
        isinstance(current_idx, bool)
        or not isinstance(current_idx, int)
        or not 0 <= current_idx < len(stages)
    ):
        return None
    cohort = current_stage_cohort_from_summaries(contest, entries, summaries)
    if cohort is None:
        return None
    summary = next(
        (
            row
            for row in summaries
            if isinstance(row, dict) and row.get("stage_idx") == current_idx
        ),
        None,
    )
    if not cohort:
        return [] if isinstance(summary, dict) and summary.get("rows") == [] else None
    exact = _exact_persisted_summary_ranking(
        summary, stages[current_idx], cohort, require_completed=False
    )
    return [dict(row) for row in exact] if exact is not None else None


def build_stage_summaries(
    manager: Any,
    contest: dict[str, Any],
    entries: list[dict[str, Any]],
    pairings: list[dict[str, Any]],
    *,
    stage_results: list[dict[str, Any]] | None = None,
    historical_topology_sealed: bool = False,
    current_topology_sealed: bool | None = None,
) -> list[dict[str, Any]]:
    """Return persisted/live standings for each stage's actual participants.

    ``ContestManager.standings`` intentionally initializes every contest entry.
    That is useful for lifecycle calculations, but would leak non-qualifiers as
    zero-point rows in a later knockout stage.  The presentation contract instead
    accepts a complete ranking only after a pairing graph, the exact current
    active cohort, or a bounded historical cohort projection proves its members.
    """
    if not isinstance(historical_topology_sealed, bool) or (
        current_topology_sealed is not None
        and not isinstance(current_topology_sealed, bool)
    ):
        return []
    stages = _stages(contest)
    if not reserved_group_markers_match_template(
        contest.get("template_id"), stages, game_id=contest.get("game_id")
    ):
        return []
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

    active_reached_scope_valid = bool(
        contest.get("status") not in ("published", "running", "rest")
        or (
            current_idx_valid
            and all(stage_idx <= current_idx for stage_idx in pairing_by_stage)
            and all(stage_idx <= current_idx for stage_idx in persisted_by_stage)
        )
    )
    if current_topology_sealed is None:
        current_topology_sealed = bool(
            current_idx_valid
            and _current_stage_topology_seal_is_valid(
                contest, pairing_by_stage.get(current_idx, [])
            )
        )

    active_participants = {
        entry["id"] for entry in (active_entries or [])
    }
    future_cohort_sizes = _future_stage_cohort_sizes(
        stages,
        current_idx=current_idx,
        active_count=len(active_participants),
        game_id=str(contest.get("game_id") or ""),
    )
    roster_cohort_sizes = _future_stage_cohort_sizes(
        stages,
        current_idx=0,
        active_count=len(valid_entries),
        game_id=str(contest.get("game_id") or ""),
    )
    try:
        if contest.get("status") in ("published", "running", "rest"):
            validate_stage_ranking_topology(stages)
        elif contest.get("status") != "finished":
            validate_nonterminal_elimination_advancement(stages)
        ranking_topology_valid = True
    except (TypeError, ValueError):
        ranking_topology_valid = False

    result: list[dict[str, Any]] = []
    verified_advancement_cohort: set[int] | None = None
    verified_advancement_ranking: list[dict[str, Any]] | None = None
    predecessor_chain_state = "proven"
    for stage_idx, stage in enumerate(stages):
        game_id = str(contest.get("game_id") or "")
        lifecycle_has_completed_stage = bool(
            stage_idx < current_idx
            or (
                stage_idx == current_idx
                and contest.get("status") == "finished"
            )
        )
        stage_semantics_valid = bool(
            current_idx_valid
            and roster_valid
            and pairing_coordinates_valid
            and persisted_coordinates_valid
            and active_reached_scope_valid
            and ranking_topology_valid
            and stage_scoring_contract_is_valid(stage, game_id=game_id)
            and (
                contest.get("status") not in ("published", "running", "rest")
                or stage_idx != current_idx
                or current_topology_sealed
            )
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
        persisted_stats_valid = all(
            _persisted_stage_stats_are_valid(
                row, require_tiebreaks=False
            )
            for row in persisted
        )
        if persisted and not persisted_stats_valid:
            # SQLite imports can bypass the strict Store writer.  Reject the
            # entire artifact before any float()/int() projection so malformed
            # values neither raise a public 500 nor publish scores that
            # contradict their canonical tie-break proof.
            stage_semantics_valid = False
        persisted_participants = {
            int(row["entry_id"])
            for row in persisted
            if isinstance(row.get("entry_id"), int)
            and not isinstance(row.get("entry_id"), bool)
        }
        premature_pairing_free_snapshot = False
        finished_current_snapshot_fallback = False
        stage_chain_authority = False
        stage_cohort_contradiction = False
        if (
            participant_ids - set(entry_by_id)
            or persisted_participants - set(entry_by_id)
        ):
            # A stage artifact tied to an entry outside the contest's frozen
            # roster is contradictory evidence, not merely an incomplete
            # legacy snapshot.  Keep that contradiction sticky so a later
            # exact-looking current snapshot cannot revive a foreign cohort.
            stage_semantics_valid = False
            stage_cohort_contradiction = True
        if not roster_valid:
            # A malformed SQLite elimination flag makes the frozen cohort
            # unknowable.  Preserve bounded fixture totals below, but do not
            # guess participants, ranking completeness, or advancement.
            expected_participants: set[int] = set()
            expected_participant_count = None
            topology_participants = None
            visible_participants: set[int] = set()
            expected_identities_known = False
            persisted_complete = False
        else:
            projected_participant_count = (
                roster_cohort_sizes.get(stage_idx)
                if stage_idx < current_idx
                else future_cohort_sizes.get(stage_idx)
                if stage_idx > current_idx
                else len(active_participants)
            )
            previous_requires_exact_advancement = bool(
                stage_idx > 0
                and (
                    "advance_per_group" in stages[stage_idx - 1]
                    or "advance_count" in stages[stage_idx - 1]
                    or stages[stage_idx - 1].get("type")
                    == "single_elimination"
                )
            )

            # One cohort state machine governs every consumer.  Current stages
            # are bound to the active roster; historical stage zero to the full
            # frozen roster; later history to the preceding verified
            # advancement decision.  A complete legacy pairing graph is only a
            # fallback when no previous advancement contract exists.  Future
            # rows never authenticate themselves.
            authoritative_participants: set[int] | None
            if (
                stage_idx > 0
                and predecessor_chain_state == "proven"
                and verified_advancement_cohort is not None
                and (
                    (
                        persisted
                        and bool(
                            persisted_participants - verified_advancement_cohort
                        )
                    )
                    or (
                        stage_pairings
                        and bool(participant_ids - verified_advancement_cohort)
                    )
                )
            ):
                # Any downstream artifact that names participants outside the
                # fully proven upstream cohort is contradictory, even when its
                # own snapshot is partial.  That contradiction is sticky:
                # later exact-looking snapshots cannot reset it to the weaker
                # finished compatibility path.
                stage_cohort_contradiction = True
            if stage_idx == current_idx:
                if contest.get("status") in ("published", "running", "rest"):
                    authoritative_participants = (
                        _prove_current_stage_participants(
                            stages,
                            current_idx,
                            valid_entries,
                            active_entries or [],
                            previous_verified_ranking=verified_advancement_ranking,
                        )
                        if predecessor_chain_state == "proven"
                        else None
                    )
                    stage_chain_authority = (
                        authoritative_participants is not None
                    )
                elif current_idx == 0:
                    authoritative_participants = _prove_current_stage_participants(
                        stages,
                        current_idx,
                        valid_entries,
                        active_entries or [],
                        previous_verified_ranking=None,
                    )
                    stage_chain_authority = (
                        authoritative_participants is not None
                    )
                    if active_participants != set(entry_by_id):
                        stage_cohort_contradiction = True
                elif (
                    predecessor_chain_state == "proven"
                    and verified_advancement_cohort is not None
                ):
                    authoritative_participants = (
                        set(verified_advancement_cohort)
                        if active_participants == verified_advancement_cohort
                        else None
                    )
                    if active_participants != verified_advancement_cohort:
                        stage_cohort_contradiction = True
                    stage_chain_authority = (
                        authoritative_participants is not None
                    )
                elif predecessor_chain_state == "unknown":
                    # Immutable finished history may have no usable predecessor
                    # proof (missing/partial artifacts or an old ambiguous KO).
                    # The current persisted ranking is validated below before
                    # this provisional active set is allowed to escape.
                    authoritative_participants = set(active_participants)
                    finished_current_snapshot_fallback = True
                else:
                    authoritative_participants = None
                if authoritative_participants is None:
                    stage_semantics_valid = False
            elif stage_idx < current_idx and stage_idx == 0:
                authoritative_participants = set(entry_by_id)
                stage_chain_authority = True
            elif (
                stage_idx < current_idx
                and stage_idx > 0
                and predecessor_chain_state == "proven"
                and verified_advancement_cohort is not None
            ):
                authoritative_participants = set(verified_advancement_cohort)
                stage_chain_authority = True
            elif (
                stage_idx < current_idx
                and not previous_requires_exact_advancement
                and _complete_legacy_pairing_cohort(
                    stage,
                    stage_pairings,
                    participant_ids,
                    projected_participant_count,
                )
            ):
                authoritative_participants = set(participant_ids)
            elif (
                stage_idx < current_idx
                and contest.get("status") == "finished"
                and predecessor_chain_state == "unknown"
                and persisted
                and len(persisted) == len(persisted_participants)
                and persisted_participants <= set(entry_by_id)
                and projected_participant_count == len(persisted_participants)
            ):
                # Pairing-free immutable history predates durable advancement
                # proofs.  Its exact bounded snapshot may display that stage,
                # but ``stage_chain_authority`` deliberately remains false so
                # it cannot authorize any later cohort or lifecycle write.
                authoritative_participants = set(persisted_participants)
            else:
                authoritative_participants = None

            # Strict current series standings deliberately initialize the full
            # active cohort so a wholly missing opponent group remains a zero
            # row; its expected topology then blocks completion.  Every other
            # format treats a pairing-set mismatch as an invalid stage graph.
            if (
                authoritative_participants is not None
                and stage_pairings
                and participant_ids != authoritative_participants
                and not (
                    stage_idx == current_idx
                    and stage.get("series_scoring")
                    in {
                        SERIES_SCORING_INDEPENDENT,
                        SERIES_SCORING_AGGREGATE,
                    }
                )
            ):
                stage_semantics_valid = False

            expected_participant_count = (
                len(authoritative_participants)
                if authoritative_participants is not None
                else projected_participant_count
            )
            persisted_members_valid = bool(
                len(persisted) == len(persisted_participants)
                and persisted_participants <= set(entry_by_id)
            )
            persisted_matches_authority = bool(
                authoritative_participants is not None
                and persisted_participants == authoritative_participants
            )
            premature_pairing_free_snapshot = bool(
                persisted
                and (
                    stage_idx > current_idx
                    or (not stage_pairings and not lifecycle_has_completed_stage)
                )
            )
            persisted_complete = bool(
                persisted
                and stage_idx <= current_idx
                and persisted_members_valid
                and persisted_matches_authority
                and (
                    stage_pairings
                    or lifecycle_has_completed_stage
                    or (
                        stage_idx == current_idx
                        and contest.get("status")
                        in ("published", "running", "rest")
                        and authoritative_participants is not None
                        and len(authoritative_participants) <= 1
                    )
                )
            )
            expected_identities_known = bool(
                authoritative_participants is not None
                or expected_participant_count == 0
            )
            expected_participants = (
                set(authoritative_participants)
                if authoritative_participants is not None
                else set()
            )
            topology_participants = (
                expected_participants
                if expected_identities_known
                else None
            )
            visible_participants = (
                expected_participants
                if expected_identities_known
                else set()
            )
            if (
                persisted
                and not persisted_complete
                and not premature_pairing_free_snapshot
            ):
                stage_semantics_valid = False
        expected_rounds = (
            effective_swiss_rounds(stage, expected_participant_count)
            if stage_semantics_valid and stage.get("type") == "swiss"
            and expected_participant_count is not None
            else None
        )
        # A persisted decision is immutable ranking authority.  Do not replay
        # Match rows merely to build the same response: after a transition
        # failure (or a deployment that changes ranking code), doing so can
        # expose a Top table that disagrees with the stage artifact that will
        # actually drive advancement.  Malformed/partial artifacts also fail
        # closed here instead of falling through to a live recomputation.
        live_rows = (
            manager.standings(
                int(contest["id"]),
                stage_idx=stage_idx,
                pairings=stage_pairings,
                entries=entries,
                contest=contest,
                expected_current_entry_ids=(
                    expected_participants
                    if stage_idx == current_idx
                    else None
                ),
            )
            if stage_pairings
            and stage_idx <= current_idx
            and stage_semantics_valid
            and not persisted
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
        if stage_idx > current_idx:
            source_rows = []
            source = "pending"
        elif persisted and persisted_complete and stage_semantics_valid:
            source_rows = persisted
            source = "persisted"
        elif persisted and premature_pairing_free_snapshot:
            source_rows = []
            source = "pending"
        elif persisted:
            source_rows = []
            source = (
                "pending"
                if stage_idx > current_idx and not stage_pairings
                else "persisted"
            )
        elif stage_pairings:
            source_rows = live_rows
            source = "live" if any(p.get("match_id") for p in stage_pairings) else "scheduled"
        else:
            source_rows = []
            source = "pending"

        ranking_complete = bool(
            stage_semantics_valid
            and expected_identities_known
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

        grouped = str(stage.get("type") or "").startswith("group_")
        topology_group_by_entry: dict[int, str] | None = None
        topology_group_authority_valid = not grouped
        if grouped and expected_identities_known:
            (
                topology_group_by_entry,
                topology_group_authority_valid,
            ) = _frozen_group_membership(expected_participants, entry_by_id)
        authoritative_group_by_entry: dict[int, str] | None = None
        if (
            grouped
            and stage.get("overall_ranking") == "cross_group_fair_v1"
            and source_rows
        ):
            expected_entry_groups = {
                entry_id: entry_by_id.get(entry_id, {}).get("group_id")
                for entry_id in expected_participants
            }
            overall_ranks = [row.get("overall_rank") for row in source_rows]
            if (
                not complete_group_rank_coordinates(
                    source_rows,
                    expected_entry_groups=expected_entry_groups,
                )
                or any(
                    isinstance(rank, bool)
                    or not isinstance(rank, int)
                    or rank < 1
                    for rank in overall_ranks
                )
                or set(overall_ranks) != set(range(1, len(source_rows) + 1))
            ):
                # A gap, duplicate, or roster/group mismatch makes both the
                # overall table and its advancement badges untrustworthy.
                stage_semantics_valid = False
                ranking_complete = False
                source_rows = []
            else:
                authoritative_group_by_entry = expected_entry_groups
        elif grouped and source == "persisted" and source_rows:
            (
                authoritative_group_by_entry,
                group_authority_valid,
            ) = _traditional_group_authority(
                stage,
                expected_participants,
                entry_by_id,
                stage_pairings,
            )
            group_coordinates_valid = bool(
                group_authority_valid
                and complete_group_rank_coordinates(
                    source_rows,
                    expected_entry_groups=authoritative_group_by_entry,
                )
            )
            if authoritative_group_by_entry is None and group_coordinates_valid:
                requested_group_count = stage.get("group_count", 4)
                persisted_group_sizes = Counter(
                    str(row["group_id"]) for row in source_rows
                )
                expected_group_count = (
                    effective_group_count(
                        len(expected_participants), requested_group_count
                    )
                    if isinstance(requested_group_count, int)
                    and not isinstance(requested_group_count, bool)
                    and requested_group_count >= 1
                    else 0
                )
                base_group_size, larger_group_count = divmod(
                    len(expected_participants), expected_group_count
                ) if expected_group_count else (0, 0)
                expected_group_sizes = sorted(
                    [base_group_size]
                    * (expected_group_count - larger_group_count)
                    + [base_group_size + 1] * larger_group_count
                )
                group_coordinates_valid = bool(
                    expected_group_count
                    and len(persisted_group_sizes) == expected_group_count
                    and sorted(persisted_group_sizes.values())
                    == expected_group_sizes
                )
            if not group_coordinates_valid:
                # Traditional group ranks are meaningful only inside the
                # frozen group.  A swapped or partially inferred assignment
                # invalidates the whole stage; points/delta cannot repair it.
                stage_semantics_valid = False
                ranking_complete = False
                source_rows = []

        def positive_rank(value: object) -> int | None:
            return (
                value
                if isinstance(value, int)
                and not isinstance(value, bool)
                and value >= 1
                else None
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
            row = {
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
                "group_id": (
                    authoritative_group_by_entry[entry_id]
                    if authoritative_group_by_entry is not None
                    else source_row.get("group_id")
                    or entry.get("group_id")
                    or ""
                ),
                "_persisted_rank": (
                    positive_rank(source_row.get("rank_in_group"))
                    if source == "persisted"
                    else None
                ),
                "_computed_rank": (
                    positive_rank(source_row.get("rank"))
                    if source != "persisted"
                    else None
                ),
                "tiebreaks": sanitize_public_contest_tiebreaks(
                    source_row.get("tiebreaks")
                ),
            }
            if grouped:
                # Traditional groups keep a local display rank.  Random-group
                # stages additionally carry two independent, authoritative
                # coordinates; _rank_rows uses overall_rank for their global
                # table and rank_in_group only for group display/advancement.
                # Never reconstruct either from raw points or array position.
                overall_rank = positive_rank(source_row.get("overall_rank"))
                rank_in_group = positive_rank(source_row.get("rank_in_group"))
                if overall_rank is not None:
                    row["overall_rank"] = overall_rank
                if rank_in_group is not None:
                    row["rank_in_group"] = rank_in_group
            rows.append(row)

        if source != "persisted" and source_rows and any(
            sanitize_public_contest_tiebreaks(source_row.get("tiebreaks"))
            is None
            for source_row in source_rows
        ):
            # A computed rank without its complete canonical tie-break proof is
            # not authoritative.  Reject the whole stage instead of exposing a
            # partly sanitized table whose order cannot be audited.
            stage_semantics_valid = False
            ranking_complete = False
            source_rows = []
            rows = []

        rows = _rank_rows(
            rows,
            grouped=grouped,
            cross_group_overall=bool(
                grouped
                and stage.get("overall_ranking") == "cross_group_fair_v1"
            ),
            use_persisted_rank=(source == "persisted"),
            use_computed_rank=(source != "persisted"),
        )
        if source_rows and not rows:
            # A complete participant set is not a complete ranking when its
            # authoritative coordinates are damaged.  Keep stage status and
            # advancement fail-closed along with the empty table.
            stage_semantics_valid = False
            ranking_complete = False
        for row in rows:
            row.pop("_persisted_rank", None)
            row.pop("_computed_rank", None)
            if row.get("tiebreaks") is None:
                row.pop("tiebreaks", None)
        current_finished_snapshot_exact = bool(
            stage_idx == current_idx
            and contest.get("status") == "finished"
            and (
                (
                    not active_participants
                    and not rows
                    and not stage_pairings
                    and not persisted
                )
                or (
                    bool(active_participants)
                    and source == "persisted"
                    and _exact_persisted_ranking_rows(
                        stage, rows, set(active_participants)
                    )
                    is not None
                )
            )
        )
        if (
            stage_idx == current_idx
            and contest.get("status") == "finished"
            and not current_finished_snapshot_exact
        ):
            # Every finished current table is an immutable artifact read.  A
            # proven predecessor does not grant permission to replay surviving
            # Match rows, and an unknown predecessor may use compatibility only
            # after this same exact snapshot check.
            stage_semantics_valid = False
            ranking_complete = False
            source_rows = []
            rows = []
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
        pairing_topology_complete = bool(
            stage_semantics_valid
            and expected_identities_known
            and _complete_stage_pairing_topology(
                stage,
                expected_participants,
                stage_pairings,
                expected_groups=topology_group_by_entry,
                group_authority_valid=topology_group_authority_valid,
                expected_swiss_rounds=expected_rounds,
                game_id=game_id,
                contest_id=int(contest["id"]),
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )
        )
        all_completed = (
            stage_semantics_valid
            and ranking_complete
            and bool(stage_pairings)
            and pairing_topology_complete
            and completed_count == len(stage_pairings)
            and aggregate_complete
        )
        exact_chain_ranking = _exact_persisted_ranking_rows(
            stage, rows, set(expected_participants)
        )
        active_current_snapshot_conflict = bool(
            stage_idx == current_idx
            and contest.get("status") in ("published", "running", "rest")
            and persisted
            and not (
                source == "persisted"
                and exact_chain_ranking is not None
                and (
                    all_completed
                    or (
                        not stage_pairings
                        and len(expected_participants) <= 1
                    )
                )
            )
        )
        if active_current_snapshot_conflict:
            # A current decision artifact is immutable only after the stage is
            # settled.  Partial/malformed rows, or an exact-looking decision
            # beside pending/running pairings, cannot coexist with dispatch.
            # Fail the whole current authority instead of continuing from the
            # pairing graph while silently ignoring the conflicting artifact.
            stage_semantics_valid = False
            ranking_complete = False
            stage_cohort_contradiction = True
            source_rows = []
            rows = []
            completed_count = 0
            pairing_topology_complete = False
            all_completed = False
            exact_chain_ranking = None
        pairing_free_completed = bool(
            not stage_pairings
            and lifecycle_has_completed_stage
            and stage_semantics_valid
            and ranking_complete
            and (source == "persisted" or not expected_participants)
            and (
                len(expected_participants) <= 1
                or contest.get("status") == "finished"
            )
        )
        sealed_historical_completed = bool(
            historical_topology_sealed
            and stage_idx < current_idx
            and lifecycle_has_completed_stage
            and stage_semantics_valid
            and stage_chain_authority
            and source == "persisted"
            and exact_chain_ranking is not None
            and not stage_pairings
        )
        stage_completed = (
            all_completed or pairing_free_completed or sealed_historical_completed
        )
        durable_chain_verified = bool(
            stage_chain_authority
            and lifecycle_has_completed_stage
            and source == "persisted"
            and exact_chain_ranking is not None
            and (
                all_completed
                or (
                    pairing_free_completed
                    and len(expected_participants) <= 1
                )
                or sealed_historical_completed
            )
        )

        ranked_advancement_ids = (
            _advancement_zone(stage, rows)
            if stage_semantics_valid and ranking_complete and rows
            else set()
        )
        raw_next_ids = _participants(pairing_by_stage.get(stage_idx + 1, []))
        next_ids = (
            raw_next_ids
            if raw_next_ids
            and lifecycle_has_completed_stage
            and stage_semantics_valid
            and ranking_complete
            and pairing_topology_complete
            and (source == "persisted" or all_completed)
            and raw_next_ids == ranked_advancement_ids
            else set()
        )
        advancement_final = bool(next_ids) or stage_completed
        advancement_ids = next_ids or (
            ranked_advancement_ids
            if stage_semantics_valid
            and rows
            and (stage_completed or completed_count)
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

        if stage_completed:
            status = "completed"
        elif not stage_pairings:
            status = "pending"
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
            expected_entry_groups=(
                topology_group_by_entry
                if topology_group_authority_valid
                else None
            ),
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
        elif pairing_free_completed:
            # A bounded legacy terminal snapshot with no durable fixture graph
            # remains a zero-pairing historical projection.  Do not invent the
            # schedule that current code would generate for the same cohort.
            counts = {
                "encounter_groups": {"completed": 0, "total": 0},
                "match_jobs": {"completed": 0, "total": 0},
                "scoring_games": {
                    "completed": 0,
                    "planned": 0,
                    "terminal_unplayed": 0,
                },
            }
        topology = expected_stage_topology(
            stage,
            topology_participants,
            expected_entry_count=expected_participant_count,
            expected_entry_groups=(
                topology_group_by_entry
                if topology_group_authority_valid
                else None
            ),
            expected_swiss_rounds=expected_rounds,
            game_id=game_id,
        )
        expected_pairing_rows = (
            0
            if pairing_free_completed
            else int(topology["pairing_rows"])
            if topology is not None
            else len(stage_pairings)
        )
        if stage_idx == current_idx:
            if predecessor_chain_state == "contradicted" or stage_cohort_contradiction:
                cohort_authority_state = "contradicted"
            elif stage_chain_authority and stage_semantics_valid:
                cohort_authority_state = "proven"
            else:
                cohort_authority_state = "unknown"
        else:
            cohort_authority_state = None
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
                "elimination_tiebreak": (
                    build_elimination_tiebreak_projection(
                        stage,
                        stage_pairings,
                        game_id=game_id,
                        contest_id=int(contest["id"]),
                        expected_entry_bots=expected_entry_bots,
                        expected_entry_users=expected_entry_users,
                        require_current_entry_bots=require_current_entry_bots,
                        project_legacy_draw_blocked=bool(
                            stage_idx == current_idx
                            and contest.get("status") == "running"
                        ),
                    )
                    if stage_semantics_valid
                    else None
                ),
                "rows": rows,
                # Internal-only state consumed before API allow-listing.  It
                # distinguishes unavailable legacy evidence from a proven
                # cohort contradiction without exposing either as a public
                # response field.
                "_cohort_authority_state": cohort_authority_state,
                "_durable_chain_verified": durable_chain_verified,
                "_materialized_topology_valid": pairing_topology_complete,
            }
        )
        if stage_idx < current_idx:
            if (
                durable_chain_verified
                and stage.get("type") == "single_elimination"
                and "advance_count" not in stage
            ):
                # The artifact is exact, but old nonterminal KO config omitted
                # the advancement rule.  Finished readers may display it while
                # treating every later cohort as unprovable; active lifecycles
                # were rejected by the frozen-stage validator above.
                predecessor_chain_state = "unknown"
                verified_advancement_cohort = None
                verified_advancement_ranking = None
            elif durable_chain_verified and exact_chain_ranking is not None:
                next_cohort = _advancing_entry_ids(
                    stage, exact_chain_ranking, default_all=True
                )
                if next_cohort is None:
                    predecessor_chain_state = "unknown"
                    verified_advancement_cohort = None
                    verified_advancement_ranking = None
                else:
                    predecessor_chain_state = "proven"
                    verified_advancement_cohort = next_cohort
                    verified_advancement_ranking = [
                        dict(row) for row in exact_chain_ranking
                    ]
            else:
                predecessor_chain_state = (
                    "contradicted"
                    if predecessor_chain_state == "contradicted"
                    or stage_cohort_contradiction
                    else "unknown"
                )
                verified_advancement_cohort = None
                verified_advancement_ranking = None
    return result


__all__ = [
    "build_elimination_tiebreak_projection",
    "build_stage_counts",
    "build_stage_summaries",
    "current_stage_cohort_from_summaries",
    "current_stage_ranking_from_summaries",
    "expected_stage_topology",
]
