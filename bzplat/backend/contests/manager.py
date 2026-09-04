"""组织者比赛：阶段模板、休息换 Bot、对阵调度。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Literal

from bzplat.backend.contests.stages import (
    PairingSpec,
    effective_swiss_rounds,
    effective_group_count,
    estimate_match_count,
    frozen_group_round_robin,
    generate_stage_pairings,
    next_power_of_two,
)
from bzplat.backend.contests.templates import (
    get_template,
    points_for_result,
    resolve_template,
)
from bzplat.backend.contests.series import (
    conceptual_series_key,
    contest_match_binding_is_valid,
    contest_pairing_roster_binding_is_valid,
    group_conceptual_series,
    is_aggregate_series_stage,
    match_scoring_result_is_valid,
    series_rows_settled,
    summarize_elimination_encounter,
    swiss_bye_points,
    summarize_conceptual_series,
)
from bzplat.backend.contests.showcase import is_showcase, require_mutable
from bzplat.backend.contests.validation import (
    ELIMINATION_TIEBREAK_PAIRED_SWAP,
    SERIES_SCORING_AGGREGATE,
    SERIES_SCORING_INDEPENDENT,
    active_contest_entries,
    complete_group_rank_coordinates,
    contest_current_stage_index,
    contest_entry_eliminated,
    reserved_group_markers_match_template,
    stage_duplicate_mode,
    stage_scoring_contract_is_valid,
    validate_stage_ranking_topology,
    validated_random_group_format_snapshot,
)
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.runtime.binary_integrity import require_binary_file_integrity
from bzplat.backend.matches.result_contract import build_result_payload
from bzplat.backend.matches.public_outcome import (
    normalized_delta_value,
    planned_match_games,
    scoring_games_for_match,
)
from bzplat.backend.games import normalize_game_id, registry as game_registry
from bzplat.backend.runtime.config import MAX_CONCURRENT_MATCHES
from bzplat.backend.store import (
    ContestRealNameRosterForbidden,
    ExecutionQueueClosed,
    Store,
)
from bzplat.backend.store.public_contract import (
    sanitize_public_contest_tiebreaks,
)
from bzplat.backend.store.validation import (
    exact_nonnegative_int,
    exact_sqlite_bool,
    is_authoritative_no_opponent_pairing,
    validate_contest_times as _validate_contest_times,
)
from bzplat.backend.store.schema import (
    CONTEST_CANCELLED,
    CONTEST_DRAFT,
    CONTEST_FINISHED,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    CONTEST_REST,
    CONTEST_RUNNING,
    REGISTERED_ENGINES,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    TYPE_CONTEST,
    require_supported_binary_metadata,
    validate_contest_title,
    validate_orphan_recovery_reason,
)


logger = logging.getLogger(__name__)

PENCIL_RANDOM_GROUP_TEMPLATE = "pencil_group_drr"
GOMOKU_PROTECTED_GROUP_TEMPLATE = "gomoku_seeded_group_drr_final"
FORMAT_DRAW_VERSION = "secure_random_balanced_v1"
GOMOKU_DRAW_VERSION = "protected_seed_random_balanced_v1"

EliminationAdvanceState = Literal["created", "champion", "blocked"]
_CURRENT_COHORT_UNSET = object()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_stages(c: dict) -> list[dict]:
    raw = c.get("stages_json") or "[]"
    if isinstance(raw, list):
        parsed = raw
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(parsed, list) or any(
        not isinstance(stage, dict) for stage in parsed
    ):
        # Preserve stage coordinates by rejecting the whole malformed snapshot;
        # filtering non-object elements would shift every later stage index.
        return []
    return parsed


def _clean_group_id(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        return None
    return value


def current_stage_topology_seal_is_valid(
    contest: dict[str, Any], current_pairings: list[dict[str, Any]]
) -> bool:
    """Prove that the supplied current pairing batch is the sealed batch.

    The topology revision covers the whole contest, while the manifest bounds
    the current stage.  Both coordinates must be exact integers: coercing a
    legacy string/bool into a valid-looking seal would let a damaged graph
    authorize dispatch or a public ranking.
    """
    manifest = exact_nonnegative_int(contest.get("published_stage_pairing_count"))
    revision = exact_nonnegative_int(contest.get("pairing_topology_revision"))
    sealed_revision = exact_nonnegative_int(
        contest.get("sealed_pairing_topology_revision")
    )
    return bool(
        manifest is not None
        and revision is not None
        and revision == sealed_revision
        and len(current_pairings) == manifest
    )


def advancing_entry_ids(
    stage: dict[str, Any],
    ranked_rows: list[dict[str, Any]],
    *,
    default_all: bool,
) -> set[int] | None:
    """Select one exact advancement cohort from an authoritative ranking.

    Both lifecycle writes and read-model labels consume this helper so an
    imported row order cannot make them disagree.  ``None`` means the frozen
    rule or required rank coordinates are malformed; an empty set is a valid
    result only when the caller requests no implicit all-entrant fallback.
    """
    if not isinstance(stage, dict) or not isinstance(ranked_rows, list):
        return None
    has_advance_count = "advance_count" in stage
    has_advance_per_group = "advance_per_group" in stage
    if has_advance_count and has_advance_per_group:
        return None

    entry_ids: set[int] = set()
    for row in ranked_rows:
        if not isinstance(row, dict):
            return None
        entry_id = exact_nonnegative_int(row.get("entry_id"))
        if entry_id is None or entry_id < 1 or entry_id in entry_ids:
            return None
        entry_ids.add(entry_id)

    if has_advance_per_group:
        advance_per_group = exact_nonnegative_int(stage.get("advance_per_group"))
        if advance_per_group is None or advance_per_group < 1:
            return None
        if not complete_group_rank_coordinates(ranked_rows):
            return None
        return {
            int(row["entry_id"])
            for row in ranked_rows
            if int(row["rank_in_group"]) <= advance_per_group
        }

    if has_advance_count:
        advance_count = exact_nonnegative_int(stage.get("advance_count"))
        ranks: list[int] = []
        if advance_count is None or advance_count < 1:
            return None
        for row in ranked_rows:
            rank = exact_nonnegative_int(row.get("rank"))
            if rank is None or rank < 1:
                return None
            ranks.append(rank)
        if sorted(ranks) != list(range(1, len(ranked_rows) + 1)):
            return None
        return {
            int(row["entry_id"])
            for row in ranked_rows
            if int(row["rank"]) <= advance_count
        }

    return entry_ids if default_all else set()


def prove_current_stage_participants(
    stages: list[dict[str, Any]],
    current_stage_idx: int,
    entry_rows: list[dict[str, Any]],
    active_entries: list[dict[str, Any]],
    *,
    previous_verified_ranking: list[dict[str, Any]] | None,
) -> set[int] | None:
    """Prove the exact current cohort from roster and predecessor ranking.

    Active flags are an equality assertion, never an authority. A lifecycle
    without an explicit shrink keeps the full roster. The sole supported
    shrink derives entrants from the complete preceding ranking and the same
    selector used by advancement writes and presentation badges.
    """
    if (
        isinstance(current_stage_idx, bool)
        or not isinstance(current_stage_idx, int)
        or not 0 <= current_stage_idx < len(stages)
    ):
        return None
    full_roster: set[int] = set()
    active: set[int] = set()
    for entry in entry_rows:
        if not isinstance(entry, dict):
            return None
        entry_id = exact_nonnegative_int(entry.get("id"))
        if entry_id is None or entry_id < 1 or entry_id in full_roster:
            return None
        full_roster.add(entry_id)
    for entry in active_entries:
        if not isinstance(entry, dict):
            return None
        entry_id = exact_nonnegative_int(entry.get("id"))
        if entry_id is None or entry_id not in full_roster or entry_id in active:
            return None
        active.add(entry_id)

    if current_stage_idx == 0:
        return full_roster if active == full_roster else None
    previous_stage = stages[current_stage_idx - 1]
    if not isinstance(previous_stage, dict):
        return None
    if previous_verified_ranking is None:
        return None
    previous_ids = {
        int(row["entry_id"])
        for row in previous_verified_ranking
        if isinstance(row, dict)
        and isinstance(row.get("entry_id"), int)
        and not isinstance(row.get("entry_id"), bool)
    }
    if len(previous_ids) != len(previous_verified_ranking) or previous_ids != full_roster:
        return None
    if (
        previous_stage.get("type") == "single_elimination"
        and "advance_count" not in previous_stage
    ):
        return None
    expected = advancing_entry_ids(
        previous_stage, previous_verified_ranking, default_all=True
    )
    if expected is None:
        return None
    return expected if active == expected else None


def complete_traditional_group_pairing_topology(
    stage: dict[str, Any],
    expected_participants: set[int],
    stage_pairings: list[dict[str, Any]],
    *,
    expected_groups: dict[int, str] | None = None,
) -> dict[int, str] | None:
    """Prove a complete traditional group graph and its frozen partition."""
    if expected_groups is not None and (
        set(expected_groups) != expected_participants
        or any(
            _clean_group_id(group_id) != group_id
            for group_id in expected_groups.values()
        )
    ):
        return None
    topology_groups: dict[int, str] = {}
    observed_pairs: Counter[tuple[int, int]] = Counter()
    observed_directions: Counter[tuple[int, int]] = Counter()
    for pairing in stage_pairings:
        entry_a_id = pairing.get("entry_a_id")
        entry_b_id = pairing.get("entry_b_id")
        group_id = _clean_group_id(pairing.get("group_id"))
        if (
            isinstance(entry_a_id, bool)
            or not isinstance(entry_a_id, int)
            or isinstance(entry_b_id, bool)
            or not isinstance(entry_b_id, int)
            or entry_a_id == entry_b_id
            or entry_a_id not in expected_participants
            or entry_b_id not in expected_participants
            or group_id is None
            or (
                expected_groups is not None
                and (
                    expected_groups[entry_a_id] != group_id
                    or expected_groups[entry_b_id] != group_id
                )
            )
        ):
            return None
        for entry_id in (entry_a_id, entry_b_id):
            previous = topology_groups.setdefault(entry_id, group_id)
            if previous != group_id:
                return None
        observed_pairs[tuple(sorted((entry_a_id, entry_b_id)))] += 1
        observed_directions[(entry_a_id, entry_b_id)] += 1

    if set(topology_groups) != expected_participants:
        return None
    groups: dict[str, set[int]] = {}
    authoritative_groups = expected_groups or topology_groups
    for entry_id, group_id in authoritative_groups.items():
        groups.setdefault(group_id, set()).add(entry_id)
    requested_group_count = stage.get("group_count", 4)
    if (
        isinstance(requested_group_count, bool)
        or not isinstance(requested_group_count, int)
        or requested_group_count < 1
        or len(groups)
        != effective_group_count(len(expected_participants), requested_group_count)
    ):
        return None
    expected_pairs: set[tuple[int, int]] = set()
    for members_set in groups.values():
        members = sorted(members_set)
        expected_pairs.update(
            (members[left], members[right])
            for left in range(len(members))
            for right in range(left + 1, len(members))
        )
    expected_multiplicity = (
        2 if stage.get("type") == "group_double_round_robin" else 1
    )
    if observed_pairs != Counter(
        {pair: expected_multiplicity for pair in expected_pairs}
    ):
        return None
    if stage.get("type") == "group_double_round_robin" and any(
        observed_directions[(first, second)] != 1
        or observed_directions[(second, first)] != 1
        for first, second in expected_pairs
    ):
        return None
    return dict(authoritative_groups)


def complete_round_robin_pairing_topology(
    stage: dict[str, Any],
    expected_participants: set[int],
    stage_pairings: list[dict[str, Any]],
) -> bool:
    """Prove the exact ordinary RR/DRR graph, including directed DRR legs."""
    if not stage_pairings:
        return False
    stage_type = stage.get("type")
    if stage_type not in {"round_robin", "double_round_robin"}:
        return False
    observed_pairs: Counter[tuple[int, int]] = Counter()
    observed_directions: Counter[tuple[int, int]] = Counter()
    for pairing in stage_pairings:
        entry_a_id = pairing.get("entry_a_id")
        entry_b_id = pairing.get("entry_b_id")
        if (
            isinstance(entry_a_id, bool)
            or not isinstance(entry_a_id, int)
            or isinstance(entry_b_id, bool)
            or not isinstance(entry_b_id, int)
            or entry_a_id == entry_b_id
            or entry_a_id not in expected_participants
            or entry_b_id not in expected_participants
        ):
            return False
        observed_pairs[tuple(sorted((entry_a_id, entry_b_id)))] += 1
        observed_directions[(entry_a_id, entry_b_id)] += 1
    members = sorted(expected_participants)
    expected_pairs = {
        (members[left], members[right])
        for left in range(len(members))
        for right in range(left + 1, len(members))
    }
    if "games_per_pair" in stage:
        expected_multiplicity = exact_nonnegative_int(
            stage.get("games_per_pair")
        )
        if expected_multiplicity is None or expected_multiplicity < 1:
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


def complete_swiss_pairing_topology(
    stage: dict[str, Any],
    expected_participants: set[int],
    stage_pairings: list[dict[str, Any]],
    *,
    expected_rounds: int,
) -> bool:
    """Prove every materialized Swiss round covers the exact cohort once."""
    if (
        isinstance(expected_rounds, bool)
        or not isinstance(expected_rounds, int)
        or expected_rounds < 1
        or not stage_pairings
    ):
        return False
    games_per_pair = exact_nonnegative_int(stage.get("games_per_pair", 1))
    if games_per_pair is None or games_per_pair < 1:
        return False
    by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for pairing in stage_pairings:
        round_num = exact_nonnegative_int(pairing.get("round_num"))
        if round_num is None or round_num < 1:
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
                    or not is_authoritative_no_opponent_pairing(
                        "swiss", pairing
                    )
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


def complete_single_elimination_pairing_topology(
    stage: dict[str, Any],
    expected_participants: set[int],
    stage_pairings: list[dict[str, Any]],
    *,
    get_match: Callable[[str], dict[str, Any] | None],
    game_id: str,
    contest_id: int,
    expected_entry_bots: dict[int, int | None],
    expected_entry_users: dict[int, int],
    require_current_entry_bots: bool,
    require_champion: bool,
) -> bool:
    """Prove each materialized KO round/slot and its decided winner chain."""
    if len(expected_participants) < 2 or not stage_pairings:
        return False
    by_round_slot: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for pairing in stage_pairings:
        round_num = exact_nonnegative_int(pairing.get("round_num"))
        bracket_slot = exact_nonnegative_int(pairing.get("bracket_slot"))
        if (
            round_num is None
            or round_num < 1
            or bracket_slot is None
        ):
            return False
        by_round_slot[(round_num, bracket_slot)].append(pairing)
    rounds = sorted({round_num for round_num, _slot in by_round_slot})
    if not rounds or rounds != list(range(1, rounds[-1] + 1)):
        return False
    matches = {
        str(pairing["match_id"]): match
        for pairing in stage_pairings
        if pairing.get("match_id")
        if (match := get_match(str(pairing["match_id"]))) is not None
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
        needs_tiebreak = False
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
                if (
                    isinstance(entry_a_id, bool)
                    or not isinstance(entry_a_id, int)
                    or entry_a_id not in current_participants
                    or entry_a_id in round_participants
                    or (
                        expected_slot_participants is not None
                        and expected_slot_participants != {entry_a_id}
                    )
                    or exact_nonnegative_int(
                        pairing.get("tiebreak_group", 0)
                    )
                    != 0
                    or exact_nonnegative_int(
                        pairing.get("tiebreak_game", 0)
                    )
                    != 0
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
                isinstance(entry_a_id, bool)
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
            ):
                return False
            round_participants.update((entry_a_id, entry_b_id))
            if summary.get("state") == "decided":
                if winner_entry not in {entry_a_id, entry_b_id}:
                    return False
                winners.append(int(winner_entry))
            elif (
                summary.get("state") == "append_tiebreak"
                and not require_champion
                and round_num == rounds[-1]
            ):
                # Lifecycle completion of the currently materialized batch is
                # what authorizes `_maybe_next_elim_round` to append the next
                # paired-swap decider.  Presentation/terminal callers require
                # a champion and therefore never accept this intermediate
                # state as a completed stage.
                needs_tiebreak = True
            else:
                return False
        if round_participants != current_participants:
            return False
        if needs_tiebreak:
            if round_num == 1 and sum(
                len(rows) == 1
                and is_authoritative_no_opponent_pairing(
                    "single_elimination", rows[0]
                )
                for rows in slots.values()
            ) != next_power_of_two(len(expected_participants)) - len(
                expected_participants
            ):
                return False
            return True
        if len(winners) == 1:
            return round_num == rounds[-1]
        if round_num == 1 and sum(
            len(rows) == 1
            and is_authoritative_no_opponent_pairing(
                "single_elimination", rows[0]
            )
            for rows in slots.values()
        ) != next_power_of_two(len(expected_participants)) - len(
            expected_participants
        ):
            return False
        previous_winners = winners
        current_participants = set(winners)
    return not require_champion


def traditional_group_authority(
    stage: dict[str, Any],
    expected_participants: set[int],
    entry_by_id: dict[int, dict[str, Any]],
    stage_pairings: list[dict[str, Any]],
    *,
    require_complete_topology: bool = False,
) -> tuple[dict[int, str] | None, bool]:
    """Resolve roster-first group authority with one strict legacy fallback."""
    roster_groups: dict[int, str] = {}
    missing_roster_group = False
    for entry_id in expected_participants:
        raw_group = entry_by_id.get(entry_id, {}).get("group_id")
        if not isinstance(raw_group, str):
            return None, False
        if raw_group:
            group_id = _clean_group_id(raw_group)
            if group_id is None:
                return None, False
            roster_groups[entry_id] = group_id
        else:
            missing_roster_group = True

    if not missing_roster_group:
        if not require_complete_topology:
            return roster_groups, True
        topology = complete_traditional_group_pairing_topology(
            stage,
            expected_participants,
            stage_pairings,
            expected_groups=roster_groups,
        )
        return (roster_groups, True) if topology is not None else (None, False)

    # Pairing topology may recover only an entirely blank legacy roster.  A
    # partial roster assignment would splice two independent authorities.
    if roster_groups:
        return None, False
    if not stage_pairings:
        return (None, True) if not require_complete_topology else (None, False)
    topology = complete_traditional_group_pairing_topology(
        stage, expected_participants, stage_pairings
    )
    return (topology, True) if topology is not None else (None, False)


def _rank_paired_swap_elimination_rows(
    ranked: list[dict[str, Any]],
    pairings: list[dict[str, Any]],
    matches: dict[str, dict[str, Any]],
    *,
    stage: dict[str, Any],
    game_spec,
    expected_contest_id: int,
    expected_entry_bots: dict[int, int | None],
    expected_entry_users: dict[int, int],
    require_current_entry_bots: bool,
    require_decided: bool,
) -> list[dict[str, Any]]:
    """Order paired-swap KO rows by bracket progress, then official chain."""
    progress = {int(row["entry_id"]): 0 for row in ranked}
    if len(progress) != len(ranked):
        return []
    ranked_entries = set(progress)
    by_encounter: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for pairing in pairings:
        round_num = pairing.get("round_num")
        slot = pairing.get("bracket_slot")
        if (
            isinstance(round_num, bool)
            or not isinstance(round_num, int)
            or round_num < 1
            or isinstance(slot, bool)
            or not isinstance(slot, int)
            or slot < 0
        ):
            return []
        by_encounter.setdefault((round_num, slot), []).append(pairing)
    for (round_num, _slot), encounter_rows in by_encounter.items():
        if len(encounter_rows) == 1 and is_authoritative_no_opponent_pairing(
            stage.get("type"), encounter_rows[0]
        ):
            entry_id = encounter_rows[0].get("entry_a_id")
            if entry_id not in progress:
                return []
            progress[int(entry_id)] = max(
                progress[int(entry_id)], round_num + 1
            )
            continue
        primary = next(
            (
                row
                for row in encounter_rows
                if row.get("tiebreak_group", 0) == 0
                and row.get("tiebreak_game", 0) == 0
            ),
            None,
        )
        if primary is None:
            return []
        summary = summarize_elimination_encounter(
            stage,
            encounter_rows,
            matches.get,
            game_spec=game_spec,
            expected_contest_id=expected_contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
        entrants = {primary.get("entry_a_id"), primary.get("entry_b_id")}
        if None in entrants or not entrants <= ranked_entries:
            return []
        winner_entry = summary.get("winner_entry")
        if summary.get("state") == "decided":
            if winner_entry not in entrants:
                return []
            loser_entry = next(
                entry_id for entry_id in entrants if entry_id != winner_entry
            )
            progress[int(loser_entry)] = max(
                progress[int(loser_entry)], round_num
            )
            progress[int(winner_entry)] = max(
                progress[int(winner_entry)], round_num + 1
            )
            continue
        if not require_decided and summary.get("state") in {
            "append_tiebreak",
            "awaiting_results",
        }:
            for entry_id in entrants:
                progress[int(entry_id)] = max(
                    progress[int(entry_id)], round_num
                )
            continue
        return []
    ranked.sort(
        key=lambda row: (-progress[int(row["entry_id"])], int(row["rank"]))
    )
    for rank, row in enumerate(ranked, start=1):
        row["rank"] = rank
    return ranked


def _validated_standings_entries(
    entries: object,
) -> list[dict[str, Any]] | None:
    """Normalize only bounded legacy omissions; reject malformed identities."""
    if not isinstance(entries, list):
        return None
    normalized: list[dict[str, Any]] = []
    seen_entry_ids: set[int] = set()
    seen_user_ids: set[int] = set()
    seen_bot_ids: set[int] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            return None
        entry_id = exact_nonnegative_int(entry.get("id"))
        user_id = exact_nonnegative_int(entry.get("user_id"))
        raw_bot_id = entry.get("bot_id")
        bot_id = (
            None
            if raw_bot_id is None
            else exact_nonnegative_int(raw_bot_id)
        )
        seed = exact_nonnegative_int(entry.get("seed", 0))
        raw_group_id = entry.get("group_id", "")
        eliminated = contest_entry_eliminated(entry)
        if (
            entry_id is None
            or entry_id < 1
            or user_id is None
            or user_id < 1
            or (raw_bot_id is not None and (bot_id is None or bot_id < 1))
            or seed is None
            or not isinstance(raw_group_id, str)
            or (
                raw_group_id != ""
                and _clean_group_id(raw_group_id) != raw_group_id
            )
            or eliminated is None
            or entry_id in seen_entry_ids
            or user_id in seen_user_ids
            or (bot_id is not None and bot_id in seen_bot_ids)
        ):
            return None
        seen_entry_ids.add(entry_id)
        seen_user_ids.add(user_id)
        if bot_id is not None:
            seen_bot_ids.add(bot_id)
        normalized.append(
            {
                **entry,
                "id": entry_id,
                "user_id": user_id,
                "bot_id": bot_id,
                "seed": seed,
                "group_id": raw_group_id,
                "eliminated": int(eliminated),
            }
        )
    return normalized


def _secure_shuffle(values: list[Any]) -> list[Any]:
    """Return a Fisher-Yates permutation backed only by ``secrets``."""
    shuffled = list(values)
    for index in range(len(shuffled) - 1, 0, -1):
        swap = secrets.randbelow(index + 1)
        shuffled[index], shuffled[swap] = shuffled[swap], shuffled[index]
    return shuffled


def _group_labels(count: int) -> list[str]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 2:
        raise ValueError("分组数须为 >=2 的整数")

    def label(index: int) -> str:
        # Excel-style stable labels: A..Z, AA..AZ, BA... .
        value = index + 1
        chars: list[str] = []
        while value:
            value, remainder = divmod(value - 1, 26)
            chars.append(chr(ord("A") + remainder))
        return "".join(reversed(chars))

    return [label(index) for index in range(count)]


def _format_audit_digest(snapshot: dict[str, Any]) -> str:
    canonical = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _estimate_sec_per_match(gid: str, cfg: dict) -> int:
    """粗估每场时长（秒）：经 spec.eta_for_match（各游戏已钉死固定 ETA）。"""
    return game_registry.get(gid).eta_for_match(cfg)


def _stored_game_id(row: dict, *, entity: str) -> str:
    """读取已存实体的游戏维度；缺失/未知必须失败，不能猜成 Holdem。"""
    raw = row.get("game_id")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{entity} 缺少 game_id")
    gid = raw.strip().lower()
    try:
        game_registry.get(gid)
    except KeyError as exc:
        raise ValueError(f"{entity} 使用未注册游戏: {gid!r}") from exc
    return gid


class ContestManager:
    def __init__(
        self,
        store: Store,
        orch: MatchOrchestrator,
        *,
        execution_admission_required: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.orch = orch
        # The production app enables the full dispatcher-state gate only once
        # its singleton dispatcher owns the queue.  Pure contest unit-test
        # managers keep their historical synchronous behavior, while the
        # persistent deployment-drain bit is enforced for every caller.
        self._execution_admission_required = execution_admission_required
        # A deployment request and every contest path that can create/bind an
        # execution unit share this process-local boundary.  SQLite guards the
        # final writes as well; this lock closes the wider multi-transaction
        # start/resume lifecycle so the maintenance API cannot acknowledge a
        # drain halfway through it.
        self.deployment_activity_lock = asyncio.Lock()
        # per-contest 锁：串行化所有写状态路径（start/publish/cancel/resume/advance/
        # maybe_finish/_dispatch_pending），防止请求与 scheduler/on_match_done 并发导致
        # 重复生成轮次或取消后继续派发。
        self._locks: dict[int, asyncio.Lock] = {}
        # A successful full dispatch pass proves that every currently due,
        # unbound pairing is represented by an active durable request.  Keep
        # that proof process-local: restart performs one strict DB scan, while
        # steady scheduler ticks probe only the indexed exceptional terminal
        # range instead of anti-joining an O(n^2) stage on every tick.
        self._dispatch_coverage: dict[int, tuple[int, int | None]] = {}

    def _requires_live_admission(self) -> bool:
        """Whether this process currently owns the live execution queue."""
        if self._execution_admission_required is None:
            return False
        return bool(self._execution_admission_required())

    def _lock(self, contest_id: int) -> asyncio.Lock:
        """取（或建）该 contest 的锁。

        P1-9 修复：finished/cancelled 的 contest 锁永不清理导致无界增长。
        惰性清理——超阈值时回收空闲锁（locked()=False 的已结束赛事）。
        """
        lk = self._locks.get(contest_id)
        if lk is None:
            if len(self._locks) > 500:
                self._locks = {k: v for k, v in self._locks.items() if v.locked()}
            lk = asyncio.Lock()
            self._locks[contest_id] = lk
        return lk

    def _execution_admission_error(
        self, *, maintenance_only: bool = False
    ) -> ExecutionQueueClosed | None:
        """Return the queue gate without mutating any contest state."""
        control = self.store.executions.control()
        if self.store.executions.is_maintenance_control(control):
            return ExecutionQueueClosed(
                "平台正在部署维护，赛事将在恢复后继续派发",
                code="deployment_maintenance",
            )
        if maintenance_only:
            return None
        if self._requires_live_admission() and (
            control.get("dispatcher_state") != "running"
            or int(control.get("accepting") or 0) != 1
        ):
            return ExecutionQueueClosed(
                "执行队列暂未开放，赛事对阵已保留",
            )
        return None

    def _require_execution_admission(self) -> None:
        error = self._execution_admission_error()
        if error is not None:
            raise error

    def create(
        self,
        organizer_id: int,
        title: str,
        *,
        description: str = "",
        template_id: str | None = None,
        game_id: str | None = None,
        stages: list[dict] | None = None,
        phase: str = "standalone",
        source_contest_id: int | None = None,
        source_contest_include_all_hidden: bool = False,
        require_real_name: int = 0,
        registration_opens_at: str | None = None,
        registration_closes_at: str | None = None,
        starts_at: str | None = None,
        games_per_pair: int | None = None,
        stage_series_settings: dict[str, dict[str, Any]] | None = None,
        time_control_id: str | None = None,
        stage_format_settings: dict[str, dict[str, Any]] | None = None,
    ) -> dict:
        title = validate_contest_title(title)
        if not isinstance(source_contest_include_all_hidden, bool):
            raise ValueError("关联赛事隐藏态权限无效")
        series_capability: dict[str, Any] | None = None
        stage_series_capabilities: list[dict[str, Any]] | None = None
        stage_format_capabilities: list[dict[str, Any]] | None = None
        # 自定义 stages 直接用；否则只从游戏注册表中的代码模板解析 stages。
        if stages is not None:
            if not stages:
                raise ValueError("自定义 stages 须为非空数组")
            if template_id in {
                PENCIL_RANDOM_GROUP_TEMPLATE,
                GOMOKU_PROTECTED_GROUP_TEMPLATE,
            }:
                raise ValueError("随机分组内置模板不接受显式 stages 覆盖")
            if any(
                isinstance(stage, dict)
                and ({"group_assignment", "overall_ranking"} & set(stage))
                for stage in stages
            ):
                raise ValueError("随机分组与跨组排名 marker 仅供代码内置模板")
            tid = "custom" if template_id is None else template_id
            # 未指定游戏是创建入口的产品默认；显式空值/未知值不得退化为 holdem。
            gid = normalize_game_id("holdem" if game_id is None else game_id)
            # 即使调用方同时传入自定义 stages，也不能借此把一个具名模板标成
            # 另一款游戏。该组合会污染赛事快照，后续按 gid 启动错误裁判。
            if template_id:
                declared_template = get_template(template_id)
                if declared_template:
                    template_gid = str(declared_template["game_id"]).strip().lower()
                    if gid != template_gid:
                        raise ValueError(
                            f"模板 {template_id} 属于游戏 {template_gid}，不能用于游戏 {gid}"
                        )
                    if declared_template.get("creation_enabled", True) is False:
                        raise ValueError(
                            f"模板 {template_id} 已停用新建，仅供历史赛事展示"
                        )
            stage_list = stages
        else:
            tid, gid, stage_list, _tpl_mc = resolve_template(
                template_id, game_id=game_id
            )
            template = get_template(tid)
            if template is not None:
                raw_capability = template.get("games_per_pair_config")
                series_capability = (
                    dict(raw_capability)
                    if isinstance(raw_capability, dict)
                    else None
                )
                raw_stage_capabilities = template.get("stage_series_configs")
                stage_series_capabilities = (
                    list(raw_stage_capabilities)
                    if isinstance(raw_stage_capabilities, list)
                    else None
                )
                raw_format_capabilities = template.get("stage_format_configs")
                stage_format_capabilities = (
                    list(raw_format_capabilities)
                    if isinstance(raw_format_capabilities, list)
                    else None
                )
        # 无论来自 API 自定义内容还是代码模板，都通过同一严格 schema。未知键、
        # 错拼字段和不属于该阶段类型的配置必须在落赛事快照前失败。
        from bzplat.backend.contests.validation import configure_games_per_pair

        stage_list = configure_games_per_pair(
            stage_list,
            gid,
            games_per_pair,
            capability=series_capability,
            stage_series_settings=stage_series_settings,
            stage_capabilities=stage_series_capabilities,
        )
        from bzplat.backend.contests.validation import configure_stage_format_settings

        if stages is not None and stage_format_settings is not None:
            raise ValueError("自定义阶段不支持 stage_format_settings")
        stage_list = configure_stage_format_settings(
            stage_list,
            gid,
            stage_format_settings,
            capabilities=stage_format_capabilities,
        )
        from bzplat.backend.contests.validation import (
            validate_stage_ranking_topology,
        )

        validate_stage_ranking_topology(stage_list)
        resolved_time_control_id = self._resolve_contest_time_control_id(
            gid, time_control_id, template_id=tid
        )
        selected_template = get_template(tid)
        navigation_capability = (
            selected_template.get("allows_navigation_source_contest", False)
            if selected_template
            else False
        )
        if not isinstance(navigation_capability, bool):
            raise ValueError("赛事模板关联赛事能力配置非法")
        if tid == GOMOKU_PROTECTED_GROUP_TEMPLATE:
            # The source ranking is part of this template's immutable identity,
            # not an optional navigation link.  Reject an absent, unfinished,
            # cross-game or damaged source before creating even a draft so the
            # UI/API cannot leave an unpublishable formal contest behind.
            self._complete_gomoku_source_ranking(
                {"game_id": gid, "source_contest_id": source_contest_id}
            )
        elif source_contest_id is not None:
            if stages is not None or not navigation_capability:
                raise ValueError("当前赛事模板不支持关联来源赛事")
            source_id = exact_nonnegative_int(source_contest_id)
            if source_id is None or source_id < 1:
                raise ValueError("关联赛事 ID 必须是正整数")
        # P5：phase 优先级：显式传入 > 模板自带 phase > standalone
        if phase == "standalone":
            tpl = get_template(tid)
            if tpl and tpl.get("phase"):
                phase = tpl["phase"]
        # 时间校验：开放报名 <= 截止报名 <= 开赛（相同秒合法）
        _validate_contest_times(registration_opens_at, registration_closes_at, starts_at)
        return self.store.create_contest(
            title,
            organizer_id,
            description=description,
            status="draft",
            game_id=gid,
            template_id=tid,
            stages_json=json.dumps(stage_list, ensure_ascii=False),
            current_stage_idx=0,
            phase=phase,
            source_contest_id=source_contest_id,
            source_contest_include_all_hidden=source_contest_include_all_hidden,
            time_control_id=resolved_time_control_id,
            require_real_name=require_real_name,
            registration_opens_at=registration_opens_at,
            registration_closes_at=registration_closes_at,
            starts_at=starts_at,
        )

    @staticmethod
    def _stage_series_capabilities(template_id: object) -> list[dict[str, Any]] | None:
        template = get_template(str(template_id or ""))
        raw = template.get("stage_series_configs") if template else None
        return list(raw) if isinstance(raw, list) else None

    @staticmethod
    def _stage_format_capabilities(template_id: object) -> list[dict[str, Any]] | None:
        template = get_template(str(template_id or ""))
        raw = template.get("stage_format_configs") if template else None
        return list(raw) if isinstance(raw, list) else None

    @staticmethod
    def _resolve_contest_time_control_id(
        game_id: str,
        time_control_id: str | None,
        *,
        template_id: object = None,
        persisted: bool = False,
    ) -> str:
        """Resolve a create-time choice or an already-persisted snapshot.

        Omission while creating a contest selects the template default.  A
        durable SQL ``NULL`` has a different compatibility meaning: it is a
        pre-column historical row and therefore resolves only to the game's
        legacy default before the template allow-list is checked.  Keeping the
        two paths explicit prevents a damaged new fixed-control template from
        silently acquiring today's template default during recovery.
        """
        spec = game_registry.get(game_id)
        template = get_template(str(template_id or ""))
        fixed = template.get("fixed_time_control_id") if template else None
        if fixed is not None:
            if not isinstance(fixed, str) or not fixed:
                raise ValueError("赛事模板固定时限配置非法")
            fixed_resolved = str(spec.resolve_time_control(fixed).id)
            if time_control_id is None and not persisted:
                return fixed_resolved
            selected = str(spec.resolve_time_control(time_control_id).id)
            if selected != fixed_resolved:
                raise ValueError(f"模板 {template_id} 固定使用时限 {fixed_resolved}")
            return selected
        if template and "time_control_ids" in template:
            allowed = template.get("time_control_ids")
            if not isinstance(allowed, list) or not allowed:
                raise ValueError("赛事模板可选时限配置必须是非空列表")
            resolved_allowed: list[str] = []
            for control_id in allowed:
                if not isinstance(control_id, str) or not control_id:
                    raise ValueError("赛事模板可选时限 ID 非法")
                try:
                    resolved = str(spec.resolve_time_control(control_id).id)
                except (KeyError, TypeError, ValueError) as exc:
                    raise ValueError("赛事模板包含未注册或异游戏时限") from exc
                if resolved != control_id or resolved in resolved_allowed:
                    raise ValueError("赛事模板可选时限 ID 必须唯一且稳定")
                resolved_allowed.append(resolved)
            if len(resolved_allowed) == 1:
                fixed_resolved = resolved_allowed[0]
                if time_control_id is None and not persisted:
                    return fixed_resolved
                selected = str(spec.resolve_time_control(time_control_id).id)
                if selected != fixed_resolved:
                    raise ValueError(
                        f"模板 {template_id} 固定使用时限 {fixed_resolved}"
                    )
                return selected
            if time_control_id is None:
                if persisted:
                    selected = str(spec.default_time_control_id)
                elif "default_time_control_id" in template:
                    default_id = template.get("default_time_control_id")
                    if not isinstance(default_id, str) or not default_id:
                        raise ValueError("赛事模板默认时限 ID 非法")
                    selected = str(spec.resolve_time_control(default_id).id)
                else:
                    selected = str(spec.default_time_control_id)
            else:
                selected = str(spec.resolve_time_control(time_control_id).id)
            if selected not in resolved_allowed:
                raise ValueError(f"模板 {template_id} 不允许时限 {selected}")
            return selected
        return str(spec.resolve_time_control(time_control_id).id)

    @staticmethod
    def _games_per_pair_capability(template_id: object) -> dict[str, Any] | None:
        template = get_template(str(template_id or ""))
        raw = template.get("games_per_pair_config") if template else None
        return dict(raw) if isinstance(raw, dict) else None

    @staticmethod
    def _matches_template_stage_topology(
        template_id: object, stages: list[dict[str, Any]]
    ) -> bool:
        """Whether a persisted snapshot still matches its code-owned topology.

        Older callers may combine a built-in ``template_id`` with explicit custom
        stages.  The identifier alone therefore cannot authorize injecting new
        code-template defaults at publish time.  Only the fields advertised by
        ``stage_series_configs`` are mutable here; every other persisted stage
        field is part of the frozen template snapshot.  Comparing just key/type
        would, for example, let a custom ``rounds`` or ``advance_count`` inherit
        defaults intended for a different tournament graph.
        """
        template = get_template(str(template_id or ""))
        template_stages = template.get("stages") if template else None
        if not isinstance(template_stages, list) or len(template_stages) != len(stages):
            return False

        configurable_series_fields = {
                "games_per_pair",
                "series_scoring",
                "swiss_extra_rounds",
                "effective_rounds",
        }
        if ContestManager._stage_format_capabilities(template_id) is not None:
            configurable_series_fields.add("group_count")

        def topology(
            rows: list[dict[str, Any]],
        ) -> tuple[tuple[tuple[str, Any], ...], ...] | None:
            out: list[tuple[tuple[str, Any], ...]] = []
            for row in rows:
                if not isinstance(row, dict):
                    return None
                out.append(
                    tuple(
                        sorted(
                            (key, value)
                            for key, value in row.items()
                            if key not in configurable_series_fields
                        )
                    )
                )
            return tuple(out)

        return topology(template_stages) == topology(stages)

    def _configured_unstarted_series_stages(
        self,
        contest: dict[str, Any],
        stages: list[dict[str, Any]],
        settings: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Apply current code-template defaults only at the unstarted boundary."""
        capabilities = self._stage_series_capabilities(contest.get("template_id"))
        pair_capability = self._games_per_pair_capability(
            contest.get("template_id")
        )
        if capabilities is None and pair_capability is None:
            if settings is not None:
                raise ValueError("当前赛事模板不支持 stage_series_settings")
            return stages
        if capabilities is None and settings is not None:
            raise ValueError("当前赛事模板不支持 stage_series_settings")
        if not self._matches_template_stage_topology(
            contest.get("template_id"), stages
        ):
            if settings is not None:
                raise ValueError("自定义阶段拓扑不支持 stage_series_settings")
            return stages
        from bzplat.backend.contests.validation import configure_games_per_pair

        if capabilities is None:
            # The first configurable RR templates persisted ``games_per_pair``
            # but predated the scoring marker.  Preserve their frozen K and
            # duplicate topology while upgrading only an omitted/legacy
            # aggregate marker.  Explicit unknown marker values remain damaged
            # input and are rejected by the lifecycle validator below.
            assert pair_capability is not None
            if len(stages) != 1:
                return stages
            stage = dict(stages[0])
            marker_present = "series_scoring" in stage
            marker = stage.get("series_scoring")
            if marker == SERIES_SCORING_INDEPENDENT:
                return stages
            if marker_present and marker != SERIES_SCORING_AGGREGATE:
                return stages
            selected_games = stage.get(
                "games_per_pair", pair_capability.get("default")
            )
            stage.pop("games_per_pair", None)
            stage.pop("series_scoring", None)
            return configure_games_per_pair(
                [stage],
                _stored_game_id(
                    contest, entity=f"赛事 #{contest.get('id')}"
                ),
                selected_games,
                capability=pair_capability,
                stage_series_settings=None,
                stage_capabilities=None,
            )

        return configure_games_per_pair(
            stages,
            _stored_game_id(contest, entity=f"赛事 #{contest.get('id')}"),
            None,
            capability=None,
            stage_series_settings=settings,
            stage_capabilities=capabilities,
        )

    def _configured_unstarted_format_stages(
        self,
        contest: dict[str, Any],
        stages: list[dict[str, Any]],
        settings: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        capabilities = self._stage_format_capabilities(contest.get("template_id"))
        if capabilities is None:
            if settings is not None:
                raise ValueError("当前赛事模板不支持 stage_format_settings")
            return stages
        if not self._matches_template_stage_topology(
            contest.get("template_id"), stages
        ):
            if settings is not None:
                raise ValueError("自定义阶段拓扑不支持 stage_format_settings")
            return stages
        from bzplat.backend.contests.validation import configure_stage_format_settings

        return configure_stage_format_settings(
            stages,
            _stored_game_id(contest, entity=f"赛事 #{contest.get('id')}"),
            settings,
            capabilities=capabilities,
        )

    def _migrate_unstarted_series_snapshot_for_lifecycle(
        self,
        contest: dict[str, Any],
        stages: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Persist a required built-in default migration behind the full CAS.

        Publish/start may read an older built-in snapshot that predates the
        independent-scoring marker.  Merely computing current defaults and
        letting ``_prepare_initial_contest`` overwrite ``stages_json`` would
        bypass the zero-progress gate used by the explicit settings endpoint.
        Only when the semantic stage snapshot actually changes do we perform
        the same transactional CAS, then re-read its authoritative result.
        Participant-dependent ``effective_rounds`` is frozen later at the
        normal publication boundary and is not itself a migration trigger.
        """
        configured = self._configured_unstarted_series_stages(contest, stages)
        # Publishing is the last boundary before schedule rows become durable.
        # Validate the would-be migrated snapshot *before* its CAS write; a
        # malformed custom/built-in drift must not receive even a partial marker
        # migration and then fail later after pairings exist.
        self._validated_lifecycle_stages(contest, configured)
        if configured == stages:
            return contest, stages
        updated = self.store.compare_and_swap_unstarted_contest_stages(
            int(contest["id"]),
            expected_status=str(contest["status"]),
            expected_stages_json=str(contest.get("stages_json") or "[]"),
            stages_json=json.dumps(configured, ensure_ascii=False),
        )
        migrated = _parse_stages(updated)
        if not migrated:
            raise ValueError(f"赛事 #{contest.get('id')} 缺少有效阶段快照")
        return updated, migrated

    @staticmethod
    def _validated_lifecycle_stages(
        contest: dict[str, Any], stages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return canonical stages safe to publish/start, or fail before writes.

        Read models intentionally accept bounded legacy history.  Draft/open
        lifecycle transitions are stricter: they create new schedule and Match
        rows, so every stage must pass the complete creation validator under the
        contest's registered game.  This also freezes legacy omitted defaults
        without truthy/int coercion.
        """
        from bzplat.backend.contests.validation import (
            validate_stage,
            validate_stage_ranking_topology,
        )

        game_id = _stored_game_id(
            contest, entity=f"赛事 #{contest.get('id')}"
        )
        if not stages:
            raise ValueError(f"赛事 #{contest.get('id')} 缺少有效阶段快照")
        if contest_current_stage_index(contest, stage_count=len(stages)) is None:
            raise ValueError(f"赛事 #{contest.get('id')} 当前阶段游标无效")
        validated = [
            validate_stage(stage, index, game_id)
            for index, stage in enumerate(stages)
        ]
        validate_stage_ranking_topology(validated)
        if not reserved_group_markers_match_template(
            contest.get("template_id"), validated, game_id=game_id
        ):
            raise ValueError("赛事随机分组 marker 与代码模板不匹配")
        return validated

    @staticmethod
    def _validated_active_lifecycle_stages(
        contest: dict[str, Any], stages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate reached history without rewriting its scoring semantics.

        Published/running pre-v1 stages can legitimately omit fields which the
        current creation schema freezes explicitly.  Legacy aggregate stages
        must also retain their frozen one-series result.  Both use the bounded
        read contract without being rewritten; an explicit aggregate marker is
        nevertheless checked by that predicate through the same full structural
        validator as new stages.  New v1 stages use the creation validator.
        """
        from bzplat.backend.contests.validation import (
            validate_stage,
            validate_stage_ranking_topology,
        )

        game_id = _stored_game_id(
            contest, entity=f"赛事 #{contest.get('id')}"
        )
        if not stages:
            raise ValueError(f"赛事 #{contest.get('id')} 缺少有效阶段快照")
        validated: list[dict[str, Any]] = []
        for index, stage in enumerate(stages):
            mode = stage.get("series_scoring")
            if (
                stage.get("type") == "swiss"
                and (
                    "swiss_round_bands" in stage
                    or (
                        mode == SERIES_SCORING_INDEPENDENT
                        and "swiss_extra_rounds" in stage
                    )
                )
                and "effective_rounds" not in stage
            ):
                raise ValueError(
                    f"阶段 {index + 1} 缺少已发布的 effective_rounds"
                )
            if mode != SERIES_SCORING_INDEPENDENT:
                if not stage_scoring_contract_is_valid(stage, game_id=game_id):
                    raise ValueError(f"阶段 {index + 1} 计分契约无效")
                validated.append(dict(stage))
            else:
                validated.append(validate_stage(stage, index, game_id))
        # No active production contest uses an older unrepresentable
        # multi-shrink graph.  Keep any imported/legacy instance fail-closed
        # before snapshot, advancement or terminal writes instead of retrying
        # an official table that can never cover the full roster.
        validate_stage_ranking_topology(validated)
        if not reserved_group_markers_match_template(
            contest.get("template_id"), validated, game_id=game_id
        ):
            raise ValueError("赛事随机分组 marker 与代码模板不匹配")
        return validated

    @staticmethod
    def _freeze_effective_stage_values(
        stages: list[dict[str, Any]], participant_count: int
    ) -> list[dict[str, Any]]:
        """Freeze participant-dependent Swiss rounds for each planned cohort.

        Later stages do not necessarily receive the initial registration
        roster.  Propagate the same bounded advancement contract used by the
        public estimator so a final Swiss stage freezes against its planned
        finalists rather than every entrant.
        """
        frozen = [dict(stage) for stage in stages]
        current_participants = participant_count
        for stage in frozen:
            if stage.get("type") == "swiss":
                stage["effective_rounds"] = effective_swiss_rounds(
                    {key: value for key, value in stage.items() if key != "effective_rounds"},
                    current_participants,
                )
            advance_per_group = stage.get("advance_per_group")
            if advance_per_group and int(advance_per_group) > 0:
                group_count = effective_group_count(
                    current_participants,
                    int(stage.get("group_count") or 4),
                )
                current_participants = min(
                    current_participants,
                    group_count * int(advance_per_group),
                )
                continue
            advance_count = stage.get("advance_count")
            if advance_count and int(advance_count) > 0:
                current_participants = min(
                    current_participants,
                    int(advance_count),
                )
        return frozen

    async def revise_stage_series_settings(
        self,
        contest_id: int,
        settings: dict[str, dict[str, Any]],
    ) -> dict | None:
        """CAS-update an unstarted template snapshot before any schedule exists."""
        return await self.revise_format_settings(
            contest_id, stage_series_settings=settings
        )

    async def revise_format_settings(
        self,
        contest_id: int,
        *,
        time_control_id: str | None = None,
        stage_format_settings: dict[str, dict[str, Any]] | None = None,
        stage_series_settings: dict[str, dict[str, Any]] | None = None,
    ) -> dict | None:
        """Atomically revise all zero-progress format selectors in one CAS."""
        if (
            time_control_id is None
            and stage_format_settings is None
            and stage_series_settings is None
        ):
            raise ValueError("至少需要修改一项赛事设置")
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                return None
            require_mutable(contest)
            if contest.get("status") not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("仅 draft/open 且尚未生成赛程的赛事可修改赛制设置")
            stages = _parse_stages(contest)
            if not stages:
                raise ValueError("赛事缺少有效阶段快照")
            configured = stages
            if stage_series_settings is not None:
                configured = self._configured_unstarted_series_stages(
                    contest, configured, stage_series_settings
                )
            if stage_format_settings is not None:
                configured = self._configured_unstarted_format_stages(
                    contest, configured, stage_format_settings
                )
            selected_time_control = contest.get("time_control_id")
            update_time_control = time_control_id is not None
            if update_time_control:
                game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
                selected_time_control = self._resolve_contest_time_control_id(
                    game_id,
                    time_control_id,
                    template_id=contest.get("template_id"),
                )
                if contest.get("time_control_id") is None:
                    legacy_default = self._resolve_contest_time_control_id(
                        game_id,
                        None,
                        template_id=contest.get("template_id"),
                        persisted=True,
                    )
                    if selected_time_control != legacy_default:
                        raise ValueError(
                            "历史赛事时限只能补齐为该游戏的旧默认值"
                        )
            return self.store.compare_and_swap_unstarted_contest_stages(
                contest_id,
                expected_status=str(contest["status"]),
                expected_stages_json=str(contest.get("stages_json") or "[]"),
                stages_json=json.dumps(configured, ensure_ascii=False),
                expected_time_control_id=contest.get("time_control_id"),
                time_control_id=selected_time_control,
                update_time_control=update_time_control,
            )

    async def revise_schedule(
        self, contest_id: int, fields: dict[str, Any]
    ) -> dict | None:
        """按赛事阶段安全修改管理端时间字段。

        draft 可调整完整排期；open 已经发生开放动作，只能调整仍在未来的
        截止/开赛时间（或清空为手动）；published 已冻结对阵，只能在尚未
        派发任何 match 时修改 ``starts_at``，并同步重排当前阶段 pending
        pairing。running/rest/终态的排期均为只读历史。

        ``title`` 是可与时间一起提交的展示元数据；若时间校验或重排失败，
        Store 仍保证它不会先行写入。
        """
        if "title" in fields:
            fields = {**fields, "title": validate_contest_title(fields["title"])}
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                return None
            require_mutable(contest)
            time_fields = {
                "registration_opens_at", "registration_closes_at", "starts_at",
            }
            changed_times = time_fields.intersection(fields)
            if not changed_times:
                return self.store.update_contest(contest_id, **fields)

            status = contest["status"]
            allowed_by_status = {
                CONTEST_DRAFT: time_fields,
                CONTEST_OPEN: {"registration_closes_at", "starts_at"},
                CONTEST_PUBLISHED: {"starts_at"},
            }
            allowed = allowed_by_status.get(status)
            if allowed is None:
                raise ValueError(f"赛事处于 {status} 态，时间编排只读")
            forbidden = changed_times.difference(allowed)
            if forbidden:
                labels = {
                    "registration_opens_at": "开放报名时间",
                    "registration_closes_at": "报名截止时间",
                    "starts_at": "比赛开始时间",
                }
                raise ValueError(
                    f"赛事处于 {status} 态，不能修改"
                    + "、".join(labels[key] for key in sorted(forbidden))
                )

            candidate = {
                key: fields.get(key, contest.get(key)) for key in time_fields
            }
            _validate_contest_times(
                candidate["registration_opens_at"],
                candidate["registration_closes_at"],
                candidate["starts_at"],
            )
            if status == CONTEST_OPEN:
                now = datetime.now()
                for key in ("registration_closes_at", "starts_at"):
                    value = candidate[key]
                    if value is not None and datetime.fromisoformat(value) <= now:
                        label = (
                            "报名截止时间"
                            if key == "registration_closes_at"
                            else "比赛开始时间"
                        )
                        raise ValueError(
                            f"报名中赛事的{label}必须晚于当前时间，或清空为手动"
                        )

            if status != CONTEST_PUBLISHED:
                return self.store.update_contest(contest_id, **fields)

            stages = _parse_stages(contest)
            stage_idx = contest_current_stage_index(
                contest, stage_count=len(stages)
            )
            if stage_idx is None:
                raise ValueError("赛事当前阶段不存在，不能重排")
            pairings = self.store.list_contest_pairings(contest_id)
            if any(pairing.get("match_id") for pairing in pairings):
                raise ValueError("赛事已有对局被派发，不能修改比赛开始时间")
            stage = stages[stage_idx]
            base = candidate["starts_at"]
            plans = [
                {
                    "id": pairing["id"],
                    "round_num": int(pairing.get("round_num") or 1),
                    "scheduled_at": self._stage_scheduled_at(
                        stage,
                        int(pairing.get("round_num") or 1),
                        base,
                    ),
                }
                for pairing in pairings
                if exact_nonnegative_int(pairing.get("stage_idx")) == stage_idx
                and pairing.get("status") == STATUS_PENDING
                and not pairing.get("match_id")
            ]
            return self.store.update_published_contest_schedule(
                contest_id,
                fields,
                stage_idx=stage_idx,
                pending_pairing_schedules=plans,
            )

    async def open_registration(self, contest_id: int) -> dict:
        """手动开放报名；与发布、开赛等生命周期写路径共用赛事锁。"""
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                error = self._execution_admission_error(
                    maintenance_only=True
                )
                if error is not None:
                    raise error
                return self._open_registration_locked(contest_id)

    def _open_registration_locked(self, contest_id: int) -> dict:
        """draft→open 的实际逻辑（调用方已持 per-contest 锁）。

        重复 open 是幂等读；其他状态不得倒退为 open。若
        手动提前开放时，以实际开放时刻覆盖未来计划；已到点的计划时间保留。
        """
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] == CONTEST_OPEN:
            return c
        if c["status"] != CONTEST_DRAFT:
            raise ValueError(f"赛事处于 {c['status']} 态，不能开放报名（仅 draft 可开放）")
        now = _now()
        # Legacy/manual data may only have a past close/start time.  Opening the
        # contest must not manufacture ``opens > closes/starts``; use the earliest
        # known lifecycle timestamp.  Registration will then immediately reject
        # callers when that close time has already elapsed.
        opens = min(
            (
                value
                for value in (
                    c.get("registration_opens_at"),
                    c.get("registration_closes_at"),
                    c.get("starts_at"),
                    now,
                )
                if value is not None
            ),
            key=datetime.fromisoformat,
        )
        return self.store.update_contest(
            contest_id, status=CONTEST_OPEN, registration_opens_at=opens
        )

    async def register(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        role: str = "",
    ) -> dict:
        """报名；与 publish/start 共用赛事锁，杜绝关报名后晚插 entry。"""
        async with self._lock(contest_id):
            return self._register_locked(contest_id, user_id, bot_id, role=role)

    def _register_locked(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        role: str = "",
    ) -> dict:
        """register 的锁内实现；Store 写入时还会在同事务复核 open 状态。"""
        # ``role`` 仅为旧调用签名兼容保留。普通 /register 入口永远是本人操作；
        # organizer/admin 的代报名必须走已校验赛事归属的 entries 管理接口。
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] != CONTEST_OPEN:
            raise ValueError("比赛未开放报名")
        # 报名截止时间校验：若 registration_closes_at 已预设且当前已过，拒绝报名
        closes = c.get("registration_closes_at")
        if closes and _now() > closes:
            raise ValueError("报名已截止")
        # 实名校验：赛事要求实名时，报名者必须已填完整实名信息
        if int(c.get("require_real_name") or 0):
            u = self.store.get_user(user_id)
            if not u or not all((u.get(k) or "").strip() for k in ("real_name", "phone", "school", "student_id")):
                raise ValueError("本赛事要求实名，请先在个人资料填写实名信息（姓名/手机号/学校/学号）")
        bot = self.store.get_bot(bot_id)
        if not bot:
            raise ValueError("bot 不存在")
        if bot["owner_id"] != user_id:
            raise ValueError("只能派遣自己的 bot")
        if not bot.get("is_active") or not bot.get("binary_path"):
            raise ValueError("bot 不可用")
        contest_game = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        bot_game = _stored_game_id(bot, entity=f"Bot #{bot_id}")
        if bot_game != contest_game:
            raise ValueError(
                f"Bot 游戏类型 ({bot_game}) 与比赛 ({contest_game}) 不一致"
            )
        owner_id = bot["owner_id"]
        if self.store.get_entry(contest_id, owner_id):
            raise ValueError("该用户在此比赛中已报名")
        return self.store.add_contest_entry_once(contest_id, owner_id, bot_id)

    def _roster_target_error(
        self, contest: dict, user_id: int, bot_id: int
    ) -> str | None:
        target_user = self.store.get_user(user_id)
        if not target_user:
            return f"user {user_id} 不存在"
        if not int(target_user.get("is_active") or 0):
            return f"user {user_id} 已停用"
        if int(contest.get("require_real_name") or 0) and not all(
            (target_user.get(field) or "").strip()
            for field in ("real_name", "phone", "school", "student_id")
        ):
            return f"user {user_id} 实名信息不完整"
        bot = self.store.get_bot(bot_id)
        if not bot or not bot.get("is_active") or not bot.get("binary_path"):
            return f"bot {bot_id} 不可用"
        if bot.get("owner_id") != user_id:
            return f"bot {bot_id} 不属于 user {user_id}"
        try:
            contest_game = _stored_game_id(contest, entity="赛事")
            bot_game = _stored_game_id(bot, entity=f"Bot #{bot_id}")
        except ValueError as exc:
            return str(exc)
        if bot_game != contest_game:
            return f"bot {bot_id} 游戏 {bot_game} ≠ 赛事 {contest_game}"
        return None

    async def add_roster_entry(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        allow_real_name_override: bool = False,
    ) -> dict:
        """Proxy-register one entrant; real-name capture needs admin override."""
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                raise ValueError("赛事不存在")
            require_mutable(contest)
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            if (
                int(contest.get("require_real_name") or 0)
                and not allow_real_name_override
            ):
                raise ContestRealNameRosterForbidden()
            error = self._roster_target_error(contest, user_id, bot_id)
            if error:
                raise ValueError(error)
            added, skipped, _identity_required = self.store.add_contest_roster_entries(
                contest_id,
                [(user_id, bot_id)],
                allow_real_name_override=allow_real_name_override,
                return_identity_required=True,
            )
            if skipped or not added:
                raise ValueError("该用户已报名")
            return added[0]

    async def assign_roster_entries(
        self,
        contest_id: int,
        targets: list[tuple[int, int]],
        *,
        allow_real_name_override: bool = False,
    ) -> dict:
        """Proxy-register a roster; real-name capture needs admin override."""
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                raise ValueError("赛事不存在")
            require_mutable(contest)
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            if (
                int(contest.get("require_real_name") or 0)
                and not allow_real_name_override
            ):
                raise ContestRealNameRosterForbidden()

            skipped: list[str] = []
            identity_incomplete_users: list[int] = []
            valid: list[tuple[int, int]] = []
            seen: set[int] = set()
            for user_id, bot_id in targets:
                if user_id in seen:
                    skipped.append(f"user {user_id} 重复，跳过")
                    continue
                seen.add(user_id)
                error = self._roster_target_error(contest, user_id, bot_id)
                if error:
                    skipped.append(f"{error}，跳过")
                    if error == f"user {user_id} 实名信息不完整":
                        identity_incomplete_users.append(user_id)
                    continue
                valid.append((user_id, bot_id))

            (
                added,
                duplicate_users,
                identity_required_at_commit,
            ) = self.store.add_contest_roster_entries(
                contest_id,
                valid,
                allow_real_name_override=allow_real_name_override,
                return_identity_required=True,
            )
            skipped.extend(
                f"user {user_id} 已报名，跳过" for user_id in duplicate_users
            )
            return {
                "added": len(added),
                "skipped": skipped,
                "identity_incomplete_count": len(identity_incomplete_users),
                "identity_incomplete_users": identity_incomplete_users,
                "total_entries": len(self.store.list_contest_entries(contest_id)),
                # Private Manager/API coordination metadata.  Handlers consume
                # and remove it before serializing the public response.
                "_identity_required_at_commit": identity_required_at_commit,
            }

    async def delete_roster_entry(self, contest_id: int, user_id: int) -> bool:
        """组织者/admin 删名册；仅 draft/open，且与 publish 共用赛事锁。"""
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                raise ValueError("赛事不存在")
            require_mutable(contest)
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            return self.store.delete_contest_roster_entry(contest_id, user_id)

    async def dispatch(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        role: str = "",
    ) -> dict:
        """休息期（或允许换 Bot 的阶段间歇）更换派遣 Bot。

        published 的未开始 pairing 与 entry 在同一事务换绑当前 Bot 版本；
        rest 的 completed 历史 pairing/阶段决策保持旧身份，只让下一阶段与
        最终名册使用新 Bot。两条路径都在写后重封 lifecycle revision。

        P1-4 修复：加 per-contest 锁，与 scheduler 的 resume/_begin_stage 串行化，
        防 bot 交换与下一阶段配对生成竞态（旧代码无锁，TOCTOU 导致配对指向错误 bot/version）。
        """
        async with self._lock(contest_id):
            return await self._dispatch_locked(contest_id, user_id, bot_id, role=role)

    async def _dispatch_locked(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        role: str = "",
    ) -> dict:
        # ``role`` 仅为旧调用签名兼容保留。普通 /dispatch 只允许当前用户更新
        # 自己的 entry；代理名册操作必须走 organizer/admin 专用 entries 接口。
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        # 换人时机：开赛前（draft/open/published）+ 中场休息（rest，受 allow_bot_swap_in_rest 控制）。
        # 不允许 running 态换人（与赛程对齐：比赛中途换 Bot 影响公平性）。
        if c["status"] not in (CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED, CONTEST_REST):
            raise ValueError("当前状态不可更换 Bot（仅开赛前或休息期可换）")
        stages = _parse_stages(c)
        idx = contest_current_stage_index(c, stage_count=len(stages))
        if idx is None:
            raise ValueError("赛事当前阶段游标损坏，不能更换 Bot")
        stage = stages[idx] if 0 <= idx < len(stages) else {}
        if c["status"] == CONTEST_REST and not stage.get("allow_bot_swap_in_rest", True):
            raise ValueError("本阶段休息不允许换 Bot")

        bot = self.store.get_bot(bot_id)
        if not bot:
            raise ValueError("bot 不存在")
        if bot["owner_id"] != user_id:
            raise ValueError("只能派遣自己的 bot")
        if not bot.get("is_active") or not bot.get("binary_path"):
            raise ValueError("bot 不可用")
        contest_game = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        bot_game = _stored_game_id(bot, entity=f"Bot #{bot_id}")
        if bot_game != contest_game:
            raise ValueError(
                f"Bot 游戏类型 ({bot_game}) 与比赛 ({contest_game}) 不一致"
            )

        entry = self.store.get_entry(contest_id, user_id)
        if not entry:
            raise ValueError("未报名本比赛")

        entry_rows = _validated_standings_entries(
            self.store.list_contest_entries(contest_id)
        )
        if entry_rows is None:
            raise ValueError("赛事冻结名册身份或状态损坏，不能更换 Bot")
        expected_stage_groups: dict[int, str] | None = None
        expected_revision: int | None = None
        if c["status"] in (CONTEST_PUBLISHED, CONTEST_REST):
            expected_revision = self.store.contest_stage_decision_revision(
                contest_id,
                idx,
                expected_status=str(c["status"]),
            )
            if expected_revision is None:
                raise ValueError("赛事当前阶段 lifecycle seal 无法验证")
        if c["status"] == CONTEST_REST:
            ranked_rows = self._stage_ranking_from_recovery_snapshot(
                contest_id, idx
            )
            if ranked_rows is None:
                raise ValueError("休息期阶段决策缺失、残缺或损坏")
            expected_stage_groups = self._decision_stage_groups(
                stage, ranked_rows
            )

        return self.store.swap_contest_entry_bot_and_reseal(
            contest_id,
            user_id,
            bot_id,
            expected_status=str(c["status"]),
            expected_current_stage_idx=idx,
            expected_old_bot_id=entry.get("bot_id"),
            expected_entries=entry_rows,
            expected_revision=expected_revision,
            expected_game_id=contest_game,
            expected_bot_current_version=bot.get("current_version"),
            expected_stage_groups=expected_stage_groups,
            dispatched_at=_now(),
        )

    def _guard_round_robin_size(self, stages: list[dict], n: int) -> None:
        """循环赛不限人数，只保留分组坐标的严格类型校验。

        物理执行仍受全局 match slots / sandbox capacity 硬顶约束，因此取消
        排期人数限制只会增加持久队列长度，不会放大同时运行的对局数。

        - round_robin / double_round_robin：全员互打，不设人数上限。
          stage.allow_large_round_robin 是历史旁路标记，现为兼容 no-op。
        - group_round_robin / group_double_round_robin：组内循环同样不限人数；
          ``group_count`` 仍须是正整数，不能让损坏快照改变分组拓扑。
        """
        del n
        for st in stages:
            t = st.get("type") or ""
            if t in ("round_robin", "double_round_robin"):
                continue
            elif t in ("group_round_robin", "group_double_round_robin"):
                gc = st.get("group_count", 4)
                if isinstance(gc, bool) or not isinstance(gc, int) or gc < 1:
                    raise ValueError("group_count 须为 ≥1 的整数")

    def _assert_engine(self, game_id: str) -> None:
        if game_id not in REGISTERED_ENGINES:
            raise ValueError(
                f"游戏引擎未注册: {game_id}（当前仅支持 {sorted(REGISTERED_ENGINES)}）"
            )

    def _bot_unavailable_reason(
        self,
        bot_id: int | None,
        *,
        expected_game: str,
        version_id: int | None = None,
    ) -> str | None:
        """返回赛事 Bot 不可用原因；可用时返回 None。

        发布/开赛与中途重派必须共用同一套判定，否则会出现
        “发布时看似可用，实际派发时才失败”的空壳赛事。
        """
        if bot_id is None:
            return "Bot 引用已缺失"
        bot = self.store.get_bot(bot_id)
        if not bot:
            return f"Bot #{bot_id} 不存在"
        if not bot.get("is_active"):
            return f"Bot #{bot_id} 已停用"
        if not bot.get("binary_path"):
            return f"Bot #{bot_id} 未上传可执行文件"
        try:
            bot_game = _stored_game_id(bot, entity=f"Bot #{bot_id}")
        except ValueError as exc:
            return str(exc)
        if bot_game != expected_game:
            return f"Bot #{bot_id} 游戏为 {bot_game}，赛事游戏为 {expected_game}"
        version = (
            self.store.get_bot_version(int(version_id))
            if version_id is not None
            else self.store.get_current_bot_version(bot_id)
        )
        if version_id is not None and (
            version is None or int(version.get("bot_id") or 0) != int(bot_id)
        ):
            return f"Bot #{bot_id} 冻结版本不可用"
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
            require_binary_file_integrity(runtime, path)
        except (OSError, TypeError, ValueError):
            return f"Bot #{bot_id} 冻结版本文件不可用"
        return None

    def _validate_initial_roster(self, contest: dict, entries: list[dict]) -> None:
        """发布/开赛前在赛事锁内复核名册可运行性。

        不允许过滤掉坏 entry 后静默开赛：那会让报名者无声消失。
        只有全部报名 entry 均有 active + binary + 游戏匹配的 Bot，
        且总数至少 2，才能生成公平的首阶段对阵。
        开赛初始化会重置历史 eliminated 标记，因此校验不能先按该标记
        过滤，否则会把实际将参赛的人漏掉。
        """
        game_id = _stored_game_id(contest, entity="赛事")
        active_entries = entries
        issues: list[str] = []
        for entry in active_entries:
            reason = self._bot_unavailable_reason(
                entry.get("bot_id"), expected_game=game_id
            )
            if reason:
                issues.append(f"报名 #{entry.get('id')}: {reason}")
        if len(active_entries) < 2 or issues:
            detail = "；".join(issues[:5])
            suffix = f"：{detail}" if detail else ""
            raise ValueError(f"至少需要 2 名持有可用 Bot 的参赛者{suffix}")

    @staticmethod
    def _is_random_group_template(contest: dict[str, Any]) -> bool:
        return contest.get("template_id") in {
            PENCIL_RANDOM_GROUP_TEMPLATE,
            GOMOKU_PROTECTED_GROUP_TEMPLATE,
        }

    def _complete_gomoku_source_ranking(
        self, contest: dict[str, Any]
    ) -> list[dict[str, Any]]:
        expected_game_id = _stored_game_id(contest, entity="保护种子赛事")
        source_id = contest.get("source_contest_id")
        if isinstance(source_id, bool) or not isinstance(source_id, int) or source_id < 1:
            raise ValueError("保护种子赛事必须选择已完成的五子棋模拟赛")
        source = self.store.get_contest(source_id)
        if (
            not source
            or source.get("game_id") != expected_game_id
            or source.get("status") != CONTEST_FINISHED
            or exact_sqlite_bool(source.get("official_results_ready")) is not True
        ):
            raise ValueError("保护种子来源必须是已完成且正式榜就绪的五子棋赛事")
        source_entries = self.store.list_contest_entries(source_id)
        try:
            rows = self.store.list_official_results(source_id)
            self.store.validate_complete_official_group_coordinates(
                rows,
                expected_entry_groups={
                    int(entry["id"]): entry.get("group_id")
                    for entry in source_entries
                },
            )
        except (TypeError, ValueError):
            raise ValueError("保护种子来源正式榜已损坏或不完整") from None
        expected_entries = {int(entry["id"]) for entry in source_entries}
        source_identity = {
            int(entry["id"]): (int(entry["user_id"]), entry.get("bot_id"))
            for entry in source_entries
        }
        seen_entries: set[int] = set()
        seen_users: set[int] = set()
        for expected_rank, row in enumerate(rows, start=1):
            entry_id = row.get("entry_id")
            user_id = row.get("user_id")
            rank = row.get("rank")
            if (
                isinstance(entry_id, bool)
                or not isinstance(entry_id, int)
                or entry_id not in expected_entries
                or entry_id in seen_entries
                or isinstance(user_id, bool)
                or not isinstance(user_id, int)
                or user_id < 1
                or user_id in seen_users
                or source_identity.get(entry_id) != (user_id, row.get("bot_id"))
                or isinstance(rank, bool)
                or not isinstance(rank, int)
                or rank != expected_rank
            ):
                raise ValueError("保护种子来源正式榜已损坏或不完整")
            seen_entries.add(entry_id)
            seen_users.add(user_id)
        if (
            seen_entries != expected_entries
            or len(rows) != len(source_entries)
            or len(rows) < 4
        ):
            raise ValueError("保护种子来源正式榜已损坏或不完整")
        return rows

    def _freeze_random_group_format(
        self,
        contest: dict[str, Any],
        entries: list[dict[str, Any]],
        stages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
        """Build one bounded draw snapshot without writing any persistent row."""
        template_id = str(contest.get("template_id") or "")
        frozen_stages = [dict(stage) for stage in stages]
        if not frozen_stages or frozen_stages[0].get("type") != "group_double_round_robin":
            raise ValueError("随机分组模板缺少有效的首阶段分组双循环")
        participant_count = len(entries)
        protected: list[dict[str, Any]] = []
        source_snapshot: dict[str, Any] | None = None
        if template_id == GOMOKU_PROTECTED_GROUP_TEMPLATE:
            if not 22 <= participant_count <= 26:
                raise ValueError("保护种子五子棋正式赛仅允许 22–26 人发布")
            group_count = 4 if participant_count <= 24 else 5
            template = get_template(template_id)
            template_game_id = str((template or {}).get("game_id") or "")
            if _stored_game_id(contest, entity="保护种子赛事") != template_game_id:
                raise ValueError("保护种子模板与赛事游戏不一致")
            expected_control = self._resolve_contest_time_control_id(
                template_game_id,
                (template or {}).get("default_time_control_id"),
                template_id=template_id,
            )
            stored_control = self._resolve_contest_time_control_id(
                template_game_id,
                contest.get("time_control_id"),
                template_id=template_id,
                persisted=True,
            )
            if stored_control != expected_control:
                raise ValueError("保护种子五子棋赛必须使用每方累计 300 秒")
            source_rows = self._complete_gomoku_source_ranking(contest)
            target_by_user = {int(entry["user_id"]): entry for entry in entries}
            for row in source_rows:
                entry = target_by_user.get(int(row["user_id"]))
                if entry is None:
                    continue
                protected.append(
                    {
                        "entry": entry,
                        "source_rank": int(row["rank"]),
                        "source_entry_id": int(row["entry_id"]),
                    }
                )
                if len(protected) == group_count:
                    break
            if len(protected) != group_count:
                raise ValueError(f"已报名选手中不足 {group_count} 名可用保护种子")
            frozen_stages[0]["group_count"] = group_count
            frozen_stages[0]["advance_per_group"] = 2
            if len(frozen_stages) != 2 or frozen_stages[1].get("type") != "double_round_robin":
                raise ValueError("保护种子模板缺少决赛双循环")
            frozen_stages[1]["ranking_mode"] = "replace_top"
            frozen_stages[1]["ranking_scope"] = group_count * 2
            source_snapshot = {
                "contest_id": int(contest["source_contest_id"]),
                "protected": [
                    {
                        "entry_id": int(item["entry"]["id"]),
                        "user_id": int(item["entry"]["user_id"]),
                        "source_entry_id": item["source_entry_id"],
                        "source_rank": item["source_rank"],
                    }
                    for item in protected
                ],
            }
            algorithm = GOMOKU_DRAW_VERSION
        else:
            raw_group_count = frozen_stages[0].get("group_count")
            if (
                isinstance(raw_group_count, bool)
                or not isinstance(raw_group_count, int)
                or raw_group_count < 2
                or raw_group_count > participant_count // 2
            ):
                raise ValueError("分组数必须至少为 2，且每组至少 2 人")
            group_count = raw_group_count
            algorithm = FORMAT_DRAW_VERSION

        labels = _group_labels(group_count)
        capacities = {label: participant_count // group_count for label in labels}
        for label in _secure_shuffle(labels)[: participant_count % group_count]:
            capacities[label] += 1

        protected_entry_ids = {
            int(item["entry"]["id"]) for item in protected
        }
        assignment: dict[int, str] = {}
        for label, item in zip(_secure_shuffle(labels), protected):
            entry_id = int(item["entry"]["id"])
            assignment[entry_id] = label
            capacities[label] -= 1

        remainder = _secure_shuffle(
            [entry for entry in entries if int(entry["id"]) not in protected_entry_ids]
        )
        open_slots = _secure_shuffle(
            [label for label in labels for _ in range(capacities[label])]
        )
        if len(remainder) != len(open_slots):
            raise ValueError("分组容量计算不一致")
        for entry, label in zip(remainder, open_slots):
            entry_id = int(entry["id"])
            assignment[entry_id] = label

        # The last tie-break is an independent fair draw across the *whole*
        # roster.  Protected source rank controls only one-seed-per-group, not
        # the final fallback order inside or across groups.
        draw_order = _secure_shuffle([int(entry["id"]) for entry in entries])
        draw_position = {
            entry_id: index for index, entry_id in enumerate(draw_order, start=1)
        }

        frozen_entries: list[dict[str, Any]] = []
        for entry in entries:
            entry_id = int(entry["id"])
            frozen_entries.append(
                {
                    **entry,
                    "group_id": assignment[entry_id],
                    "seed": draw_position[entry_id],
                    "eliminated": 0,
                }
            )
        groups = {
            label: [
                int(entry["id"])
                for entry in frozen_entries
                if entry["group_id"] == label
            ]
            for label in labels
        }
        snapshot: dict[str, Any] = {
            "version": 1,
            "algorithm": algorithm,
            # High-entropy private salt prevents the public digest from acting
            # as an oracle over the small permutation space of tiny groups.
            "audit_nonce": secrets.token_hex(32),
            "group_count": group_count,
            "group_sizes": {label: len(groups[label]) for label in labels},
            "draw_order": draw_order,
            "groups": groups,
        }
        if source_snapshot is not None:
            snapshot["source"] = source_snapshot
        snapshot["audit_digest"] = _format_audit_digest(snapshot)
        return frozen_entries, frozen_stages, snapshot

    async def start(self, contest_id: int) -> dict:
        """立即开赛（手动触发，跳过排期等待）。

        - **open/draft**：生成对阵 + 设 scheduled_at=now（立即开打）+ dispatch 全部。
        - **published**：排期已发布（pairing 已生成），**不重新生成**——仅把现有 pending
          pairing 的 scheduled_at 改成 now（立即到点）+ dispatch。避免重复生成 pairing。
        若要走两阶段（截止报名→出排期→到开赛时间再开打），用 publish() + 调度器。
        """
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                return await self._start_locked(contest_id)

    async def _start_locked(self, contest_id: int) -> dict:
        """start 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] not in (CONTEST_OPEN, CONTEST_DRAFT, CONTEST_PUBLISHED):
            raise ValueError("仅 open/draft/published 可开赛")
        # Manual start must fail before validating/mutating schedules, pairing
        # batches or lifecycle state.  The HTTP layer maps this queue gate to a
        # retryable 503 instead of pretending the contest started.
        self._require_execution_admission()
        game_id = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        self._assert_engine(game_id)

        # 必须先校验、后改 scheduled_at/status；校验失败时整个
        # start 对赛事状态与已发布排期零副作用。
        entries = self.store.list_contest_entries(contest_id)
        self._validate_initial_roster(c, entries)

        # published 态：pairing 已存在，直接改 scheduled_at=now 立即开打（不重新生成）
        if c["status"] == CONTEST_PUBLISHED:
            now = _now()
            stages = _parse_stages(c)
            stage_idx = contest_current_stage_index(
                c, stage_count=len(stages)
            )
            if stage_idx is None:
                raise ValueError("赛事当前阶段游标损坏，拒绝开赛")
            # 硬崩可能留下“有行但只有半批”的首阶段。手动开赛前
            # 先做完整性对账，不得只把残缺的几场改成 now 就开打。
            self._ensure_published_pairings_locked(contest_id, stage_idx)
            pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
            old_match_ids = {p["id"]: p.get("match_id") for p in pairings}
            old_schedules = {p["id"]: p.get("scheduled_at") for p in pairings}
            old_opens_at = c.get("registration_opens_at")
            old_closes_at = c.get("registration_closes_at")
            old_starts_at = c.get("starts_at")
            old_rest_ends_at = c.get("rest_ends_at")
            planned_opens = c.get("registration_opens_at")
            opens = (
                planned_opens
                if planned_opens
                and datetime.fromisoformat(planned_opens) <= datetime.fromisoformat(now)
                else now
            )
            immediate_plans = [
                {
                    "id": pairing["id"],
                    "round_num": int(pairing.get("round_num") or 1),
                    "scheduled_at": now,
                }
                for pairing in pairings
                if pairing.get("status") == STATUS_PENDING
                and not pairing.get("match_id")
            ]
            self.store.update_published_contest_schedule(
                contest_id,
                {
                    "registration_opens_at": opens,
                    "registration_closes_at": now,
                    "starts_at": now,
                    "rest_ends_at": None,
                },
                stage_idx=stage_idx,
                pending_pairing_schedules=immediate_plans,
            )
            try:
                await self._dispatch_pending_locked(contest_id, stage_idx)
            except Exception:
                # challenge 在首场成功前失败：仍是 published，尚无新 match，可精确恢复
                # 原排期供组织者修复后重试。若已有 pairing 成功派发，状态已是 running，
                # 保留已发生的真实进度，剩余 pending 由 scheduler 收敛。
                current = self.store.get_contest(contest_id)
                refreshed = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
                started = any(
                    not old_match_ids.get(q["id"]) and q.get("match_id")
                    for q in refreshed
                )
                if current and current["status"] == CONTEST_PUBLISHED and not started:
                    restore_plans = [
                        {
                            "id": pairing["id"],
                            "round_num": int(pairing.get("round_num") or 1),
                            "scheduled_at": old_schedules[pairing["id"]],
                        }
                        for pairing in pairings
                        if pairing.get("status") == STATUS_PENDING
                        and not pairing.get("match_id")
                    ]
                    try:
                        self.store.update_published_contest_schedule(
                            contest_id,
                            {
                                "registration_opens_at": old_opens_at,
                                "registration_closes_at": old_closes_at,
                                "starts_at": old_starts_at,
                                "rest_ends_at": old_rest_ends_at,
                            },
                            stage_idx=stage_idx,
                            pending_pairing_schedules=restore_plans,
                        )
                    except Exception:
                        logger.exception(
                            "contest %s manual-start schedule compensation "
                            "could not restore the unchanged published batch",
                            contest_id,
                        )
                raise
            return self.store.get_contest(contest_id)

        stages = _parse_stages(c)
        if not stages:
            raise ValueError(f"赛事 #{contest_id} 缺少有效阶段快照")
        c, stages = self._migrate_unstarted_series_snapshot_for_lifecycle(
            c, stages
        )
        stages = self._validated_lifecycle_stages(c, stages)
        stages = self._freeze_effective_stage_values(stages, len(entries))
        stages = self._validated_lifecycle_stages(c, stages)

        self._guard_round_robin_size(stages, len(entries))

        snapshot = self._initial_lifecycle_snapshot(c, entries)
        random_group_path = self._is_random_group_template(c)
        random_group_frozen = False
        try:
            now = _now()
            planned_opens = c.get("registration_opens_at")
            opens_at = (
                planned_opens
                if planned_opens
                and datetime.fromisoformat(planned_opens) <= datetime.fromisoformat(now)
                else now
            )
            if random_group_path:
                self._freeze_initial_random_group_publication(
                    c,
                    entries,
                    stages,
                    opens_at=opens_at,
                    closes_at=now,
                    starts_at=now,
                    schedule_immediately=True,
                )
                random_group_frozen = True
            else:
                self._prepare_initial_contest(
                    contest_id,
                    entries,
                    stages,
                    opens_at=opens_at,
                    closes_at=now,
                    starts_at=now,
                )
                await self._begin_stage(
                    contest_id,
                    0,
                    schedule_immediately=True,
                    dispatch_pending=False,
                    activate_running=False,
                )
            await self._dispatch_pending_locked(contest_id, 0)
        except Exception:
            # Once a CSPRNG draw commits, keep its complete published schedule.
            # A dispatch retry must consume that exact draw rather than reroll.
            if not random_group_path and not random_group_frozen:
                self._rollback_initial_lifecycle(contest_id, snapshot)
            raise
        return self.store.get_contest(contest_id)

    async def publish(self, contest_id: int) -> dict:
        """截止报名 + 出排期（status=open→published）。

        生成对阵 + 逐场排期 scheduled_at + 冻结版本，但**不 dispatch**——等开赛时间到
        调度器到点 dispatch（scheduled_at<=now 的 pairing 才开打）。
        组织者可手动调本方法提前出排期；调度器到 registration_closes_at 自动调。
        """
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                error = self._execution_admission_error(
                    maintenance_only=True
                )
                if error is not None:
                    raise error
                return await self._publish_locked(contest_id)

    async def _publish_locked(self, contest_id: int) -> dict:
        """publish 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] not in (CONTEST_OPEN, CONTEST_DRAFT):
            raise ValueError("仅 open/draft 可出排期")
        game_id = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        self._assert_engine(game_id)

        entries = self.store.list_contest_entries(contest_id)
        self._validate_initial_roster(c, entries)

        stages = _parse_stages(c)
        if not stages:
            raise ValueError(f"赛事 #{contest_id} 缺少有效阶段快照")
        c, stages = self._migrate_unstarted_series_snapshot_for_lifecycle(
            c, stages
        )
        stages = self._validated_lifecycle_stages(c, stages)
        stages = self._freeze_effective_stage_values(stages, len(entries))
        stages = self._validated_lifecycle_stages(c, stages)

        self._guard_round_robin_size(stages, len(entries))

        snapshot = self._initial_lifecycle_snapshot(c, entries)
        random_group_path = self._is_random_group_template(c)
        random_group_frozen = False
        try:
            # 截止报名盖戳：手动提前发布时使用实际时刻；调度器到点发布时
            # 保留原计划时刻。这样不会留下 closes_at > starts_at 的倒挂时间线。
            # 先完整生成排期、但不 dispatch；这样生成失败可删除本次未启动 pairing
            # 并恢复原状态，不会出现 published/running 空壳赛事。
            now = _now()
            planned_opens = c.get("registration_opens_at")
            planned_closes = c.get("registration_closes_at")
            now_dt = datetime.fromisoformat(now)
            opens_at = (
                planned_opens
                if planned_opens and datetime.fromisoformat(planned_opens) <= now_dt
                else now
            )
            closes_at = (
                planned_closes
                if planned_closes and datetime.fromisoformat(planned_closes) <= now_dt
                else now
            )
            starts_at = c.get("starts_at")
            if (
                starts_at is not None
                and datetime.fromisoformat(starts_at)
                < datetime.fromisoformat(closes_at)
            ):
                starts_at = closes_at
            if random_group_path:
                self._freeze_initial_random_group_publication(
                    c,
                    entries,
                    stages,
                    opens_at=opens_at,
                    closes_at=closes_at,
                    starts_at=starts_at,
                    schedule_immediately=False,
                )
                random_group_frozen = True
            else:
                self._prepare_initial_contest(
                    contest_id,
                    entries,
                    stages,
                    opens_at=opens_at,
                    closes_at=closes_at,
                    starts_at=starts_at,
                )
                await self._begin_stage(
                    contest_id,
                    0,
                    schedule_immediately=False,
                    dispatch_pending=False,
                    activate_running=False,
                )
        except Exception:
            if not random_group_path and not random_group_frozen:
                self._rollback_initial_lifecycle(contest_id, snapshot)
            raise
        return self.store.get_contest(contest_id)

    def _initial_lifecycle_snapshot(self, contest: dict, entries: list[dict]) -> dict:
        """记录初始阶段会修改的最小字段，供失败补偿。调用方须持赛事锁。"""
        elimination_states: dict[int, int] = {}
        for entry in entries:
            eliminated = contest_entry_eliminated(entry)
            if eliminated is None:
                raise ValueError("参赛者淘汰状态损坏，拒绝启动赛事")
            elimination_states[int(entry["user_id"])] = int(eliminated)
        return {
            "contest": {
                key: contest.get(key)
                for key in (
                    "status",
                    "registration_opens_at",
                    "registration_closes_at",
                    "starts_at",
                    "stages_json",
                    "current_stage_idx",
                    "rest_ends_at",
                )
            },
            "entries": {
                e["user_id"]: {
                    "seed": e.get("seed") or 0,
                    "eliminated": elimination_states[int(e["user_id"])],
                }
                for e in entries
            },
            "pairing_ids": {
                p["id"] for p in self.store.list_contest_pairings(contest["id"])
            },
        }

    def _prepare_initial_contest(
        self,
        contest_id: int,
        entries: list[dict],
        stages: list[dict],
        *,
        opens_at: str,
        closes_at: str,
        starts_at: str | None,
    ) -> None:
        """写入首阶段 seed 与 published 准备态；调用方须持赛事锁。"""
        for i, entry in enumerate(entries):
            self.store.update_entry(
                contest_id, entry["user_id"], seed=i + 1, eliminated=0
            )
        self.store.update_contest(
            contest_id,
            status=CONTEST_PUBLISHED,
            registration_opens_at=opens_at,
            registration_closes_at=closes_at,
            starts_at=starts_at,
            stages_json=json.dumps(stages, ensure_ascii=False),
            current_stage_idx=0,
            rest_ends_at=None,
        )

    def _rollback_initial_lifecycle(self, contest_id: int, snapshot: dict) -> bool:
        """首阶段生成/首次派发失败时做保守补偿。

        仅当赛事仍为 published 且本次新增 pairing 全部未绑定 match 时回滚；若已有
        对局成功派发，真实状态应保留为 running，剩余 pending 交给 scheduler 重试。
        因调用方仍持 per-contest 锁，补偿不会覆盖 cancel/start 等合法生命周期变化。
        """
        current = self.store.get_contest(contest_id)
        original_status = snapshot["contest"]["status"]
        if not current or current["status"] not in (CONTEST_PUBLISHED, original_status):
            return False
        before_ids = snapshot["pairing_ids"]
        generated = [
            p for p in self.store.list_contest_pairings(contest_id)
            if p["id"] not in before_ids
        ]
        if any(p.get("match_id") for p in generated):
            return False
        generated_ids = [p["id"] for p in generated]
        deleted = self.store.delete_unstarted_contest_pairings(contest_id, generated_ids)
        if deleted != len(generated_ids):
            logger.error(
                "contest lifecycle rollback refused: contest=%s expected_pairings=%s deleted=%s",
                contest_id,
                len(generated_ids),
                deleted,
            )
            return False
        for user_id, fields in snapshot["entries"].items():
            self.store.update_entry(contest_id, user_id, **fields)
        self.store.update_contest(contest_id, **snapshot["contest"])
        return True

    def _freeze_initial_random_group_publication(
        self,
        contest: dict[str, Any],
        entries: list[dict[str, Any]],
        stages: list[dict[str, Any]],
        *,
        opens_at: str,
        closes_at: str,
        starts_at: str | None,
        schedule_immediately: bool,
    ) -> dict[str, Any]:
        frozen_entries, frozen_stages, format_snapshot = (
            self._freeze_random_group_format(contest, entries, stages)
        )
        frozen_stages = self._validated_lifecycle_stages(contest, frozen_stages)
        if contest.get("template_id") == GOMOKU_PROTECTED_GROUP_TEMPLATE:
            expected_totals = {22: 156, 23: 166, 24: 176, 25: 190, 26: 200}
            expected_total = expected_totals.get(len(entries))
            if expected_total is None:
                raise ValueError("保护种子赛事人数带无效")
            format_snapshot["expected_match_count"] = expected_total
            format_snapshot.pop("audit_digest", None)
            format_snapshot["audit_digest"] = _format_audit_digest(format_snapshot)
        format_snapshot_json = json.dumps(
            format_snapshot,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        candidate = {
            **contest,
            "stages_json": json.dumps(frozen_stages, ensure_ascii=False),
            "format_snapshot_json": format_snapshot_json,
        }
        stage, specs, bot_to_entry = self._stage_pairing_plan(
            candidate, 0, entry_rows=frozen_entries
        )
        if not specs:
            raise ValueError("分组双循环未生成任何对阵")
        base = _now() if schedule_immediately else starts_at
        pairing_rows = self._pairing_rows_for_plan(
            int(contest["id"]),
            0,
            stage,
            specs,
            bot_to_entry,
            base=base,
        )
        if contest.get("template_id") == GOMOKU_PROTECTED_GROUP_TEMPLATE:
            finalists = int(frozen_stages[1]["ranking_scope"])
            total = len(pairing_rows) + finalists * (finalists - 1)
            if total != format_snapshot["expected_match_count"]:
                raise ValueError("保护种子赛程总场数与规则不一致")
        return self.store.freeze_initial_group_contest(
            int(contest["id"]),
            expected_status=str(contest["status"]),
            expected_stages_json=str(contest.get("stages_json") or "[]"),
            expected_time_control_id=contest.get("time_control_id"),
            stages_json=json.dumps(frozen_stages, ensure_ascii=False),
            format_snapshot_json=format_snapshot_json,
            entry_rows=frozen_entries,
            pairing_rows=pairing_rows,
            registration_opens_at=opens_at,
            registration_closes_at=closes_at,
            starts_at=starts_at,
        )

    @staticmethod
    def _materialize_pairing_seats(spec: PairingSpec) -> tuple[int, int | None]:
        """Turn PairingSpec.color_first into the durable seat 0/1 A/B order.

        Pairing generators keep a stable conceptual A/B identity while choosing
        which side should move first.  Persistence and every downstream consumer
        use A as authoritative seat 0, so a ``color_first=1`` spec is swapped here
        and stored with the normalized ``color_first=0`` representation.
        """
        bot_a_id = spec.bot_a_id
        bot_b_id = spec.bot_b_id
        if int(spec.color_first or 0) == 1 and bot_b_id is not None:
            return bot_b_id, bot_a_id
        return bot_a_id, bot_b_id

    @staticmethod
    def _private_pairing_seed(
        contest_id: int, stage_idx: int, ordinal: int
    ) -> int:
        """Allocate one private CSPRNG seed for a newly frozen pairing.

        The coordinates are validated but deliberately do not participate in
        the value: contest/stage/pairing coordinates are public, while Holdem
        consumes this seed as the actual deal sequence.
        """
        coordinates = (contest_id, stage_idx, ordinal)
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in coordinates
        ) or contest_id < 1 or stage_idx < 0 or ordinal < 1:
            raise ValueError("赛事对阵 seed 坐标超出范围")
        return secrets.randbelow(9_223_372_036_854_775_807) + 1

    @staticmethod
    def _duplicate_seed(pairing: dict[str, Any]) -> int:
        """Return the publication-frozen private seed, or fail closed."""
        frozen = pairing.get("pairing_seed")
        if (
            isinstance(frozen, bool)
            or not isinstance(frozen, int)
            or frozen < 1
            or frozen > 9_223_372_036_854_775_807
        ):
            raise ValueError("多场赛事对阵缺少有效的私密冻结 seed")
        return frozen

    def _stage_pairing_plan(
        self,
        contest: dict,
        stage_idx: int,
        *,
        entry_rows: list[dict[str, Any]] | None = None,
    ) -> tuple[dict, list, dict[int, int]]:
        """纯计算当前阶段首批 pairing spec，不产生 DB 副作用。

        publish 硬崩恢复必须用与 ``_begin_stage`` 完全相同的规则重算
        期望批次，否则只按行数判断会把“数量相同但参赛者错了”的
        损坏数据误当完整。首阶段没有“上一阶段积分”，不读当前残缺
        pairing 的 standings，避免已落盘 bye 分反过来改变恢复排序。
        """
        stages = self._validated_active_lifecycle_stages(
            contest, _parse_stages(contest)
        )
        if stage_idx < 0 or stage_idx >= len(stages):
            raise ValueError("赛事当前阶段不存在")
        stage = stages[stage_idx]
        if entry_rows is None:
            entry_rows = self.store.list_contest_entries(contest["id"])
        entries = active_contest_entries(entry_rows)
        if entries is None:
            raise ValueError("参赛者淘汰状态损坏，拒绝生成阶段对阵")
        prior_scores: dict[int, float] = {}
        if stage_idx > 0:
            prior_scores = {
                row["entry_id"]: row["points"]
                for row in self.standings(contest["id"], stage_idx=stage_idx - 1)
            }
        if (
            contest.get("template_id") == "gomoku_seeded_group_drr_final"
            and stage_idx > 0
        ):
            entries.sort(key=lambda entry: (entry.get("seed") or 0, entry["id"]))
        else:
            entries.sort(
                key=lambda entry: (
                    -prior_scores.get(entry["id"], 0),
                    entry.get("seed") or 0,
                    entry["id"],
                )
            )
        bot_ids = [
            entry["bot_id"] for entry in entries if entry.get("bot_id") is not None
        ]
        bot_to_entry = {
            entry["bot_id"]: entry["id"]
            for entry in entries
            if entry.get("bot_id") is not None
        }
        if len(bot_ids) < 2 and stage.get("type") != "single_elimination":
            return stage, [], bot_to_entry
        reserved_random_stage = (
            stage_idx == 0
            and contest.get("template_id")
            in {PENCIL_RANDOM_GROUP_TEMPLATE, GOMOKU_PROTECTED_GROUP_TEMPLATE}
        )
        if reserved_random_stage:
            snapshot = validated_random_group_format_snapshot(contest)
            if snapshot is None:
                raise ValueError("冻结随机分组快照与模板拓扑不一致")
            snapshot_groups = snapshot["groups"]
            expected_entry_group = {
                entry_id: group_id
                for group_id, member_ids in snapshot_groups.items()
                for entry_id in member_ids
            }
            if any(
                isinstance(entry.get("id"), bool)
                or not isinstance(entry.get("id"), int)
                or entry["id"] < 1
                for entry in entries
            ):
                raise ValueError("冻结随机分组名册身份损坏")
            draw_position = {
                entry_id: position
                for position, entry_id in enumerate(snapshot["draw_order"], start=1)
            }
            entry_ids = {entry["id"] for entry in entries}
            if len(entry_ids) != len(entries) or entry_ids != set(expected_entry_group):
                raise ValueError("冻结随机分组名册与抽签快照不一致")
            groups = {group_id: [] for group_id in snapshot_groups}
            for entry in entries:
                entry_id = entry["id"]
                group_id = entry.get("group_id")
                seed = entry.get("seed")
                bot_id = entry.get("bot_id")
                if (
                    group_id != expected_entry_group[entry_id]
                    or isinstance(seed, bool)
                    or not isinstance(seed, int)
                    or seed != draw_position[entry_id]
                    or isinstance(bot_id, bool)
                    or not isinstance(bot_id, int)
                    or bot_id < 1
                ):
                    raise ValueError("冻结随机分组名册字段损坏")
                groups[group_id].append(bot_id)
            if any(
                len(groups[group_id]) != snapshot["group_sizes"][group_id]
                for group_id in groups
            ):
                raise ValueError("冻结随机分组人数与抽签快照不一致")
            specs = frozen_group_round_robin(groups, double=True)
        elif str(stage.get("type") or "").startswith("group_") and all(
            isinstance(entry.get("group_id"), str) and entry.get("group_id")
            for entry in entries
        ):
            groups: dict[str, list[int]] = {}
            for entry in entries:
                groups.setdefault(str(entry["group_id"]), []).append(int(entry["bot_id"]))
            expected_groups = stage.get("group_count")
            if (
                isinstance(expected_groups, bool)
                or not isinstance(expected_groups, int)
                or expected_groups != len(groups)
            ):
                raise ValueError("冻结分组与阶段 group_count 不一致")
            specs = frozen_group_round_robin(
                groups,
                double=stage.get("type") == "group_double_round_robin",
            )
        elif stage.get("type") == "swiss":
            rounds = effective_swiss_rounds(stage, len(bot_ids))
            stage = {**stage, "rounds": rounds}
            specs = generate_stage_pairings(stage, bot_ids, swiss_round=1)
        else:
            specs = generate_stage_pairings(stage, bot_ids)
        return stage, specs, bot_to_entry

    def _pairing_rows_for_plan(
        self,
        contest_id: int,
        stage_idx: int,
        stage: dict[str, Any],
        specs: list[PairingSpec],
        bot_to_entry: dict[int, int],
        *,
        base: str | None,
    ) -> list[dict[str, Any]]:
        key = stage.get("key") or f"stage{stage_idx}"
        published_at = _now()
        series_stage = "games_per_pair" in stage
        materialized = [
            (spec, *self._materialize_pairing_seats(spec)) for spec in specs
        ]
        # A double round robin has O(n^2) rows but only O(n) immutable Bot
        # identities.  Freeze each current version and verify its artifact once,
        # then project that snapshot into every pairing row.
        version_by_bot: dict[int, int | None] = {}
        for spec, bot_a_id, bot_b_id in materialized:
            if not spec.requires_match:
                continue
            for bot_id in (bot_a_id, bot_b_id):
                if bot_id is None or bot_id in version_by_bot:
                    continue
                version_by_bot[bot_id] = self._version_snapshot(
                    bot_id, None
                )["bot_a_version_id"]
        pairing_rows: list[dict[str, Any]] = []
        for ordinal, (spec, bot_a_id, bot_b_id) in enumerate(
            materialized, start=1
        ):
            scheduled_at = self._stage_scheduled_at(stage, spec.round_num, base)
            common = {
                "bot_a_id": bot_a_id,
                "bot_b_id": bot_b_id,
                "round_num": spec.round_num,
                "stage_idx": stage_idx,
                "stage_key": key,
                "group_id": spec.group_id,
                "bracket_slot": spec.bracket_slot,
                "color_first": 0,
                "series_index": spec.series_index,
                "series_size": spec.series_size,
                "tiebreak_group": 0,
                "tiebreak_game": 0,
                "entry_a_id": bot_to_entry.get(bot_a_id),
                "entry_b_id": bot_to_entry.get(bot_b_id),
                "published_at": published_at,
            }
            if not spec.requires_match:
                pairing_rows.append(
                    {
                        **common,
                        "bot_b_id": None,
                        "entry_b_id": None,
                        "status": spec.status,
                        "scheduled_at": None,
                    }
                )
                continue
            pairing_rows.append(
                {
                    **common,
                    "status": STATUS_PENDING,
                    "pairing_seed": (
                        self._private_pairing_seed(contest_id, stage_idx, ordinal)
                        if series_stage
                        else None
                    ),
                    "scheduled_at": scheduled_at,
                    "bot_a_version_id": version_by_bot[bot_a_id],
                    "bot_b_version_id": version_by_bot[bot_b_id],
                }
            )
        return pairing_rows

    async def _begin_stage(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        schedule_immediately: bool = False,
        dispatch_pending: bool = True,
        activate_running: bool = True,
        entry_rows: list[dict[str, Any]] | None = None,
        entry_updates: list[dict[str, Any]] | None = None,
        source_decision_revision: int | None = None,
        source_stage_groups: dict[int, str] | None = None,
    ) -> None:
        """生成阶段对阵。schedule_immediately=True 时 scheduled_at 全设 now（立即开打）；
        False 时按赛事 starts_at + 轮次 stagger 逐场排期（published 态，等调度器到点 dispatch）。
        """
        c = self.store.get_contest(contest_id)
        require_mutable(c)
        stages = self._validated_active_lifecycle_stages(c, _parse_stages(c))
        current_idx = contest_current_stage_index(c, stage_count=len(stages))
        if current_idx is None:
            raise ValueError("赛事当前阶段游标损坏，拒绝生成阶段对阵")
        if stage_idx < 0 or stage_idx >= len(stages):
            self._finish_adjudicated_contest_locked(
                contest_id,
                current_idx,
                gate_stage_idx=stage_idx,
                context="invalid-stage",
            )
            return
        if (entry_rows is None) != (entry_updates is None):
            raise ValueError("晋级名册投影与 CAS 批次必须同时提供")
        if stage_idx > current_idx and source_decision_revision is None:
            raise ValueError("跨阶段生成必须绑定不可变来源决策")
        stage, specs, bot_to_entry = self._stage_pairing_plan(
            c, stage_idx, entry_rows=entry_rows
        )
        # specs 为空（如 single_elimination 收到 <2 bot → 无对手）：阶段无对阵 →
        # 直接 finished（防 maybe_finish 反复尝试空阶段）。
        if not specs:
            self._finish_adjudicated_contest_locked(
                contest_id,
                current_idx,
                gate_stage_idx=stage_idx,
                context="empty-stage",
                allow_unreached_empty_stage=True,
            )
            return

        # 逐场排期：schedule_immediately 时全 now；否则按 base + round stagger。
        # base = starts_at（仅第一阶段用赛事开赛时间）；后续阶段（stage_idx>0）用 now
        # （阶段间排期基准：rest 恢复/晋级后的新阶段从当前时刻起排）。
        if schedule_immediately:
            base = _now()
        elif stage_idx > 0:
            base = _now()  # 后续阶段从当前时刻排期（不用最初 starts_at，已过期）
        else:
            # starts_at 为空表示“发布后等待手动开始”。首阶段保持 NULL
            # 排期，scheduler 不得把报名截止误当成开赛；手动 start 会在
            # dispatch 前把 pending pairing 统一盖戳为 now。
            base = c.get("starts_at")
        pairing_rows = self._pairing_rows_for_plan(
            contest_id,
            stage_idx,
            stage,
            specs,
            bot_to_entry,
            base=base,
        )

        # 完整 pairing 批次 + 阶段游标/状态是一个持久化单元。首阶段 publish/start
        # 显式传 activate_running=False，仍由首场 bind 把 published 切 running；后续
        # stage 则在批次提交时离开 rest/推进 current_stage_idx，崩溃后可直接重派。
        transition_to_running = bool(
            activate_running
            and (schedule_immediately or stage_idx > current_idx)
        )
        self.store.create_contest_stage_pairings(
            contest_id,
            stage_idx,
            pairing_rows,
            expected_current_stage_idx=current_idx,
            expected_status=str(c["status"]),
            activate_running=transition_to_running,
            entry_updates=entry_updates,
            source_decision_revision=source_decision_revision,
            source_stage_groups=source_stage_groups,
        )
        # A successful batch write changes the authoritative pairing set even
        # for legacy contests whose published manifest is NULL.  Never let a
        # process-local "fully dispatched" marker hide the new rows.
        self._dispatch_coverage.pop(contest_id, None)
        if dispatch_pending:
            await self._dispatch_pending_locked(contest_id, stage_idx)

    async def ensure_published_pairings(self, contest_id: int, stage_idx: int) -> None:
        """修复 published 空壳/残缺首批对阵；与取消/开赛共用赛事锁。"""
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                if self._execution_admission_error() is not None:
                    return
                self._ensure_published_pairings_locked(contest_id, stage_idx)

    @staticmethod
    def _pairing_batch_signature(
        rows: list[dict], *, include_pairing_seed: bool = True
    ) -> Counter | None:
        """对阵批次的业务签名（忽略 DB id/时间/版本快照）。

        持久坐标只接受精确整数。损坏批次返回 ``None``，让 published
        恢复逻辑在无真实进度时原子重建；不能把 ``False``、浮点或文本
        通过 ``int(... or default)`` 猜成合法坐标。
        """
        signature: Counter = Counter()
        for row in rows:
            raw_round = row["round_num"] if "round_num" in row else 1
            raw_color = row["color_first"] if "color_first" in row else 0
            raw_index = row["series_index"] if "series_index" in row else 1
            raw_size = row["series_size"] if "series_size" in row else 1
            raw_tiebreak_group = (
                row["tiebreak_group"] if "tiebreak_group" in row else 0
            )
            raw_tiebreak_game = (
                row["tiebreak_game"] if "tiebreak_game" in row else 0
            )
            if (
                isinstance(raw_round, bool)
                or not isinstance(raw_round, int)
                or raw_round < 1
                or isinstance(raw_color, bool)
                or not isinstance(raw_color, int)
                or raw_color not in (0, 1)
                or isinstance(raw_index, bool)
                or not isinstance(raw_index, int)
                or raw_index < 1
                or isinstance(raw_size, bool)
                or not isinstance(raw_size, int)
                or raw_size < 1
                or isinstance(raw_tiebreak_group, bool)
                or not isinstance(raw_tiebreak_group, int)
                or isinstance(raw_tiebreak_game, bool)
                or not isinstance(raw_tiebreak_game, int)
                or not (
                    (raw_tiebreak_group == 0 and raw_tiebreak_game == 0)
                    or (
                        raw_tiebreak_group >= 1
                        and raw_tiebreak_game in (1, 2)
                    )
                )
            ):
                return None
            signature[
                (
                    raw_round,
                    row.get("entry_a_id"),
                    row.get("entry_b_id"),
                    row.get("bot_a_id"),
                    row.get("bot_b_id"),
                    row.get("stage_key") or "",
                    row.get("group_id") or "",
                    row.get("bracket_slot"),
                    raw_color,
                    row.get("pairing_seed") if include_pairing_seed else None,
                    raw_index,
                    raw_size,
                    raw_tiebreak_group,
                    raw_tiebreak_game,
                    row.get("status") or "pending",
                )
            ] += 1
        return signature

    @staticmethod
    def _published_series_seeds_are_valid(rows: list[dict]) -> bool:
        """Validate private seeds without deriving them from public coordinates."""
        seeds: list[int] = []
        playable_rows = 0
        for row in rows:
            if row.get("bot_b_id") is None:
                continue
            playable_rows += 1
            seed = row.get("pairing_seed")
            if (
                isinstance(seed, bool)
                or not isinstance(seed, int)
                or seed < 1
                or seed > 9_223_372_036_854_775_807
            ):
                return False
            seeds.append(seed)
        return playable_rows == 0 or len(seeds) == len(set(seeds))

    def _ensure_published_pairings_locked(
        self, contest_id: int, stage_idx: int
    ) -> None:
        """Validate a published batch; only seal an exact legacy batch.

        A partial or malformed pre-manifest batch is preserved verbatim and
        rejected.  Rebuilding it would erase the only crash evidence and could
        silently change private seeds or frozen Bot versions.  The sole repair
        is therefore installing a manifest+seal around a complete batch that
        still matches the canonical stage-zero plan and current roster.
        """
        contest = self.store.get_contest(contest_id)
        if not contest or contest["status"] != CONTEST_PUBLISHED:
            return
        require_mutable(contest)
        authority = self._active_current_stage_authority(
            contest, _parse_stages(contest)
        )
        if authority is None:
            raise ValueError("published 赛事前序阶段证据不完整")
        frozen_stages, entry_rows, _expected_current = authority
        if stage_idx < 0 or stage_idx >= len(frozen_stages):
            raise ValueError("published 赛事阶段索引无效")
        snapshot = self.store.contest_projection_snapshot(
            contest_id, stage_idx=stage_idx
        )
        if snapshot is None or not isinstance(snapshot.get("contest"), dict):
            raise ValueError("published 赛事快照不存在")
        snapshot_contest = snapshot["contest"]
        existing = snapshot.get("pairings")
        if not isinstance(existing, list) or any(
            not isinstance(row, dict) for row in existing
        ):
            raise ValueError("published 赛事对阵快照损坏")
        # A fresh, sealed publication already crossed the canonical writer.
        # Only the legacy NULL-manifest shape may enter the bounded repair.
        if snapshot_contest.get("published_stage_pairing_count") is not None:
            if not current_stage_topology_seal_is_valid(
                snapshot_contest, existing
            ):
                raise ValueError("published 赛事冻结对阵批次损坏")
            return
        if stage_idx != 0 or snapshot_contest.get(
            "sealed_pairing_topology_revision"
        ) is not None:
            raise ValueError("published 赛事旧批次冻结状态损坏")

        stage, specs, bot_to_entry = self._stage_pairing_plan(
            snapshot_contest, stage_idx, entry_rows=entry_rows
        )
        if not specs:
            raise ValueError("published 赛事无法生成完整对阵")
        key = stage.get("key") or f"stage{stage_idx}"
        expected_shape: list[dict] = []
        series_stage = "games_per_pair" in stage
        for spec in specs:
            bot_a_id, bot_b_id = self._materialize_pairing_seats(spec)
            expected_shape.append(
                {
                    "round_num": spec.round_num,
                    "entry_a_id": bot_to_entry.get(bot_a_id),
                    "entry_b_id": bot_to_entry.get(bot_b_id),
                    "bot_a_id": bot_a_id,
                    "bot_b_id": bot_b_id,
                    "stage_key": key,
                    "group_id": spec.group_id,
                    "bracket_slot": spec.bracket_slot,
                    "color_first": 0,
                    "series_index": spec.series_index,
                    "series_size": spec.series_size,
                    "tiebreak_group": 0,
                    "tiebreak_game": 0,
                    "status": spec.status,
                }
            )

        complete = (
            self._pairing_batch_signature(
                existing, include_pairing_seed=False
            )
            == self._pairing_batch_signature(
                expected_shape, include_pairing_seed=False
            )
            and (
                not series_stage
                or self._published_series_seeds_are_valid(existing)
            )
        )
        if not complete:
            raise ValueError("published 赛事旧对阵批次不完整或不规范")
        # The Store transaction below rechecks the durable current-version
        # identity.  Filesystem integrity cannot be proven by SQLite alone, so
        # validate every frozen roster Bot through the same runtime snapshot
        # path used by a normal publication before blessing a legacy batch.
        # Keep this outside the writer transaction to avoid holding the DB lock
        # while hashing an executable; any concurrent DB version change is
        # caught again by the Store's version/row CAS.
        checked_bots: set[int] = set()
        for entry in entry_rows:
            bot_id = exact_nonnegative_int(entry.get("bot_id"))
            if bot_id is None or bot_id < 1:
                raise ValueError("published 赛事冻结名册 Bot 身份损坏")
            if bot_id in checked_bots:
                continue
            self._version_snapshot(bot_id, None)
            checked_bots.add(bot_id)
        expected_revision = exact_nonnegative_int(
            snapshot_contest.get("pairing_topology_revision")
        )
        if expected_revision is None:
            raise ValueError("published 赛事 lifecycle revision 损坏")
        self.store.seal_canonical_published_stage_pairing_batch(
            contest_id,
            stage_idx,
            expected_revision=expected_revision,
            expected_pairing_rows=existing,
            expected_entries=entry_rows,
        )

    @staticmethod
    def _stage_scheduled_at(
        stage: dict[str, Any], round_num: int, base: str | None
    ) -> str | None:
        """用发布/恢复/管理端重排共享的阶段排期公式计算一场时间。"""
        stagger_min = max(0, int(stage.get("round_stagger_minutes") or 0))
        return ContestManager._compute_scheduled_at(round_num, base, stagger_min)

    @staticmethod
    def _compute_scheduled_at(
        round_num: int, base: str | None, stagger_min: int
    ) -> str | None:
        """逐场排期：scheduled_at = base + (round_num-1) * stagger_min 分钟。

        round_num 从 1 开始；stagger_min=0 时全用 base（同批同时）。
        """
        if base is None:
            return None
        if not stagger_min or round_num <= 1:
            return base
        from datetime import datetime, timedelta
        try:
            dt = datetime.fromisoformat(base)
        except (ValueError, TypeError):
            return base
        return (dt + timedelta(minutes=stagger_min * (round_num - 1))).isoformat(timespec="seconds")

    def _version_snapshot(self, bot_a_id: int | None, bot_b_id: int | None) -> dict:
        """P1：发布轮时冻结 bot 版本（取各自 current_version 的 version_id）。

        返回 {bot_a_version_id, bot_b_version_id}；bot 不存在/无版本时对应值为 None。
        _run_match 读 version_id → bot_versions.binary_path，保证赛事用发布时的版本，
        不受选手中途上传新版本影响。
        """
        out: dict[str, Any] = {"bot_a_version_id": None, "bot_b_version_id": None}
        for key, bid in (("bot_a_version_id", bot_a_id), ("bot_b_version_id", bot_b_id)):
            if bid is None:
                continue
            v = self.store.get_current_bot_version(bid)
            binary = v or self.store.get_bot(bid)
            if binary is None:
                raise ValueError(f"bot {bid} 不存在")
            try:
                require_supported_binary_metadata(
                    str(binary.get("format") or ""),
                    str(binary.get("os") or ""),
                    str(binary.get("arch") or ""),
                )
            except ValueError as exc:
                raise ValueError(f"bot {bid} unsupported_binary：{exc}") from exc
            path = str(binary.get("binary_path") or "").strip()
            if not path:
                raise ValueError(f"bot {bid} version_unavailable")
            try:
                require_binary_file_integrity(binary, path)
            except (OSError, TypeError, ValueError) as exc:
                raise ValueError(f"bot {bid} version_unavailable") from exc
            if v:
                out[key] = v["id"]
        return out

    async def _dispatch_pending(self, contest_id: int, stage_idx: int) -> None:
        """派发 pending pairing（对外入口，获取 per-contest 锁串行化）。

        所有调度路径（scheduler tick / start / publish / reconcile）都应调本方法，
        它会获取 per-contest 锁，与 maybe_finish 的锁串行化，防并发双发孤儿对局
        （审计 P1：scheduler 锁外调 _dispatch_pending 与 maybe_finish 持锁并发，
        challenge() 的 await 让出期间另一路径读到同一 pending pairing 二次派发）。

        注意：maybe_finish 持锁链路（_begin_stage/_maybe_next_*）调
        _dispatch_pending_locked（不重复获锁，防 asyncio.Lock 不可重入死锁）。
        """
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                await self._dispatch_pending_locked(contest_id, stage_idx)

    def _adjudicate_unavailable_pairing(
        self,
        contest: dict,
        pairing: dict,
        *,
        gid: str,
        activate_running: bool,
    ) -> str:
        """在派发前处理中途变为不可用的 Bot。

        返回 ``ready`` / ``completed`` / ``blocked``：
        - 双方可用：继续真实派发；
        - 仅一方不可用：生成有 winner 的 completed 技术判负；
        - 双方不可用：保留 pending，显式记录阻塞原因。

        绝不用 bot_id=0 伪造 aborted match；0 既违反外键，也没有
        任何可用于积分/晋级的裁决信息。
        """
        reason_a = self._bot_unavailable_reason(
            pairing.get("bot_a_id"),
            expected_game=gid,
            version_id=pairing.get("bot_a_version_id"),
        )
        reason_b = self._bot_unavailable_reason(
            pairing.get("bot_b_id"),
            expected_game=gid,
            version_id=pairing.get("bot_b_version_id"),
        )
        if reason_a is None and reason_b is None:
            return "ready"
        if reason_a is not None and reason_b is not None:
            logger.error(
                "contest pairing blocked: contest=%s pairing=%s both bots unavailable "
                "(a=%s; b=%s)",
                contest["id"],
                pairing["id"],
                reason_a,
                reason_b,
            )
            return "blocked"

        winner = 1 if reason_a is not None else 0
        import secrets

        mid = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
        stages = _parse_stages(contest)
        stage_idx = pairing.get("stage_idx")
        stage_valid = bool(
            isinstance(stage_idx, int)
            and not isinstance(stage_idx, bool)
            and 0 <= stage_idx < len(stages)
        )
        stage = stages[int(stage_idx)] if stage_valid else None
        duplicate = stage_duplicate_mode(stage)
        if not stage_scoring_contract_is_valid(stage, game_id=gid):
            logger.error(
                "contest pairing blocked by malformed duplicate mode: "
                "contest=%s pairing=%s stage=%s",
                contest["id"],
                pairing["id"],
                stage_idx,
            )
            return "blocked"
        # Only the new independent game-points contract makes a technical
        # referee decision margin-neutral. Running aggregate/pre-marker history
        # keeps its frozen +/-1 series/tie-break semantics.
        neutral_technical_delta = (
            stage.get("series_scoring") == SERIES_SCORING_INDEPENDENT
        )
        ea, eb = (
            (0, 0)
            if neutral_technical_delta
            else ((-1, 1) if winner == 1 else (1, -1))
        )
        if duplicate and game_registry.get(gid).build_match_plan is None:
            raise ValueError(f"游戏 {gid} 不支持 duplicate 技术赛果")
        self.store.adjudicate_unavailable_contest_pairing(
            contest["id"],
            pairing["id"],
            mid,
            game_id=gid,
            winner=winner,
            result=build_result_payload(
                game_registry.get(gid),
                rounds_played=0,
                deltas=[ea, eb],
            ),
            time_control_id=self._resolve_contest_time_control_id(
                gid,
                contest.get("time_control_id"),
                template_id=contest.get("template_id"),
                persisted=True,
            ),
            duplicate=duplicate,
            activate_running=activate_running,
            require_execution_admission=self._requires_live_admission(),
        )
        logger.warning(
            "contest technical loss: contest=%s pairing=%s match=%s winner=%s "
            "unavailable=%s",
            contest["id"],
            pairing["id"],
            mid,
            winner,
            reason_a or reason_b,
        )
        return "completed"

    def _dispatch_slot_budget(self) -> int | None:
        """Return the orchestrator's global Bot admission budget when supported.

        Legacy test doubles predate admission control; ``None`` preserves their
        synchronous contract without weakening the production orchestrator.
        """
        # Queue-aware orchestrators persist every due pairing without creating
        # a match.  Global capacity/contest share is enforced later by claim.
        if callable(getattr(self.orch, "start_execution_job", None)):
            return None
        capacity_fn = getattr(self.orch, "available_bot_slots", None)
        if not callable(capacity_fn):
            return None
        return max(0, int(capacity_fn()))

    async def _dispatch_pending_locked(self, contest_id: int, stage_idx: int) -> None:
        """_dispatch_pending 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        # P1-5 修复：锁内重检状态——published 可能在 scheduler snapshot 后被取消，
        # finished/cancelled 的 pending pairing 不应再派发（否则产孤儿对局）。
        if not c or c["status"] not in (CONTEST_PUBLISHED, CONTEST_RUNNING):
            return
        stages = _parse_stages(c)
        persisted_stage_idx = contest_current_stage_index(
            c, stage_count=len(stages)
        )
        requested_stage_idx = exact_nonnegative_int(stage_idx)
        if (
            persisted_stage_idx is None
            or requested_stage_idx is None
            or requested_stage_idx != persisted_stage_idx
        ):
            logger.error(
                "contest dispatch blocked by malformed/stale stage cursor: "
                "contest=%s requested=%r persisted=%r",
                contest_id,
                stage_idx,
                c.get("current_stage_idx"),
            )
            return
        stage_idx = requested_stage_idx
        # Scheduler/reconcile/completion callbacks are retry loops.  Hold the
        # existing pairing exactly as-is during deployment instead of creating
        # a technical result, binding a match or moving published -> running.
        if self._execution_admission_error() is not None:
            return
        require_mutable(c)
        now = _now()
        gid = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        # duplicate 阶段由 GameSpec 冻结多场计划；每个物理 Match 内的场次
        # 独立判胜计分，同牌换座后的组合 delta 只用于破同分。
        stage_cfg = stages[stage_idx] if 0 <= stage_idx < len(stages) else None
        spec = game_registry.get(gid) if gid in REGISTERED_ENGINES else None
        duplicate = stage_duplicate_mode(stage_cfg)
        if not stage_scoring_contract_is_valid(stage_cfg, game_id=gid):
            logger.error(
                "contest dispatch blocked by malformed duplicate mode: "
                "contest=%s stage=%s",
                contest_id,
                stage_idx,
            )
            self._dispatch_coverage.pop(contest_id, None)
            return
        want_duplicate = bool(
            duplicate and spec is not None and spec.build_match_plan is not None
        )
        if c["status"] == CONTEST_PUBLISHED:
            # ``starts_at`` 为空表示发布后等待组织者手动开赛；未来时间则
            # 仍处于候场。把赛事级闸门放在 manager，而不是只依赖
            # scheduler，避免启动对账或直接调用绕过后提前派发。
            starts_at = c.get("starts_at")
            if not starts_at or starts_at > now:
                return

        raw_manifest = c.get("published_stage_pairing_count")
        manifest_count = exact_nonnegative_int(raw_manifest)
        # Only a sealed, exact manifest can prove that the pairing set has not
        # changed since the previous tick.  Legacy NULL manifests deliberately
        # stay on the strict scan path: Store-level recovery/repair may append
        # rows without passing through this manager instance.
        manifest_valid = raw_manifest is not None and manifest_count is not None
        coverage_marker = (stage_idx, manifest_count)
        if (
            manifest_valid
            and self._dispatch_coverage.get(contest_id) == coverage_marker
        ):
            if not self.store.contest_stage_has_dispatch_gap(
                contest_id,
                stage_idx,
                due_at=now,
            ):
                return
            self._dispatch_coverage.pop(contest_id, None)

        authority = self._active_current_stage_authority(c, stages)
        if authority is None:
            logger.error(
                "contest dispatch blocked by invalid predecessor/cohort authority: "
                "contest=%s",
                contest_id,
            )
            self._dispatch_coverage.pop(contest_id, None)
            return
        stages, _entry_rows, _expected_current = authority
        stage_cfg = stages[stage_idx]
        duplicate = stage_duplicate_mode(stage_cfg)
        want_duplicate = bool(
            duplicate and spec is not None and spec.build_match_plan is not None
        )

        if c["status"] == CONTEST_PUBLISHED:
            # Publication/recovery freezes a complete batch before any job is
            # queued.  Once one current-stage request is active, a repeated
            # scheduler tick must neither rebuild that batch nor rematerialise
            # its O(n^2) plan.  If all requests become terminal while pairings
            # remain pending, the gate opens again so bounded recovery/requeue
            # keeps its existing fail-closed behaviour.
            if not self.store.published_stage_has_valid_active_batch(
                contest_id, stage_idx
            ):
                self._ensure_published_pairings_locked(contest_id, stage_idx)
        pairings = self.store.list_dispatchable_contest_pairings(
            contest_id,
            stage_idx=stage_idx,
            due_at=now,
        )
        # ``running`` 或已有 match_id 表示本批次前已有真实进度。此时某一场准备失败
        # 不能把整个 start API 报成“全失败”：保留已启动场，失败 pairing 仍 pending，
        # 记录日志并让 scheduler 后续重试。仅 published 且零进度的首场失败向上抛。
        had_progress = c.get("status") == CONTEST_RUNNING
        slot_budget = self._dispatch_slot_budget()
        technical_adjudicated = False
        coverage_complete = not self.store.contest_stage_has_future_pending_pairings(
            contest_id,
            stage_idx,
            due_at=now,
        )
        for p in pairings:
            unavailable = self._adjudicate_unavailable_pairing(
                c,
                p,
                gid=gid,
                activate_running=(
                    c.get("status") == CONTEST_PUBLISHED and not had_progress
                ),
            )
            if unavailable == "blocked":
                coverage_complete = False
                continue
            if unavailable == "completed":
                had_progress = True
                technical_adjudicated = True
                continue
            # Keep not-yet-admitted pairings genuinely pending: no match row,
            # no task waiting behind the semaphore, and no misleading running
            # badge.  A completion callback or scheduler tick will fill the next
            # free slot.
            if slot_budget is not None and slot_budget <= 0:
                coverage_complete = False
                break
            # 冻结快照已在 pairing 行；直接开打
            # duplicate=True 时使用发布批次私密冻结的 pairing_seed；正常重启
            # 继续读取同一持久值，保证两个换座计分场同牌可复现。
            try:
                await self._prepare_bind_start_pairing(
                    c,
                    p,
                    gid=gid,
                    want_duplicate=want_duplicate,
                    activate_running=(
                        c.get("status") == CONTEST_PUBLISHED and not had_progress
                    ),
                )
                had_progress = True
                if slot_budget is not None:
                    slot_budget -= 1
            except Exception:
                coverage_complete = False
                if not had_progress:
                    self._dispatch_coverage.pop(contest_id, None)
                    raise
                logger.exception(
                    "contest dispatch partial failure: contest=%s pairing=%s; "
                    "已有对局继续，失败对阵保持 pending 等待重试",
                    contest_id,
                    p["id"],
                )
        latest = self.store.get_contest(contest_id)
        latest_raw_manifest = (
            latest.get("published_stage_pairing_count") if latest else None
        )
        latest_manifest_count = exact_nonnegative_int(latest_raw_manifest)
        latest_manifest_valid = (
            latest is not None
            and latest_raw_manifest is not None
            and latest_manifest_count is not None
        )
        if coverage_complete and latest_manifest_valid:
            self._dispatch_coverage[contest_id] = (
                stage_idx,
                latest_manifest_count,
            )
        else:
            self._dispatch_coverage.pop(contest_id, None)
        # 技术判负没有 runner task，也就没有 on_match_done 回调。
        # 在已持锁的调度链内主动检查阶段，避免“全部是技术结果”
        # 的赛事永久卡 running。
        if technical_adjudicated:
            await self._maybe_finish_locked(contest_id)

    async def _prepare_bind_start_pairing(
        self,
        contest: dict,
        pairing: dict,
        *,
        gid: str,
        want_duplicate: bool,
        activate_running: bool,
    ) -> str:
        """两阶段派发一场：prepare match → 原子绑定 pairing → 启动 runner。

        MatchOrchestrator 的真实实现支持 defer/start/discard。少量只用于单元测试的
        legacy fake 没有显式 start/discard 方法时，仍沿用其 challenge 即启动契约。
        """
        tiebreak_group = pairing.get("tiebreak_group", 0)
        if (
            isinstance(tiebreak_group, bool)
            or not isinstance(tiebreak_group, int)
            or tiebreak_group < 0
        ):
            raise ValueError("赛事对阵淘汰决胜坐标损坏")
        pairing = self.store.ensure_contest_pairing_seed_for_enqueue(
            int(contest["id"]),
            pairing,
            expected_stages_json=contest.get("stages_json"),
        )
        frozen_time_control_id = self._resolve_contest_time_control_id(
            gid,
            contest.get("time_control_id"),
            template_id=contest.get("template_id"),
            persisted=True,
        )
        if callable(getattr(self.orch, "start_execution_job", None)):
            common = {
                "owner_user_id": contest["organizer_id"],
                "match_type": TYPE_CONTEST,
                "contest_id": contest["id"],
                "contest_pairing_id": pairing["id"],
                "game_id": gid,
                "time_control_id": frozen_time_control_id,
                "bot_a_version_id": pairing.get("bot_a_version_id"),
                "bot_b_version_id": pairing.get("bot_b_version_id"),
            }
            if tiebreak_group > 0:
                common["match_seed"] = self._duplicate_seed(pairing)
            if want_duplicate:
                return await self.orch.challenge_duplicate(
                    pairing["bot_a_id"],
                    pairing["bot_b_id"],
                    duplicate_seed=self._duplicate_seed(pairing),
                    **common,
                )
            return await self.orch.challenge(
                pairing["bot_a_id"], pairing["bot_b_id"], **common
            )

        common = {
            "owner_user_id": contest["organizer_id"],
            "match_type": TYPE_CONTEST,
            "contest_id": contest["id"],
            "game_id": gid,
            "time_control_id": frozen_time_control_id,
            "bot_a_version_id": pairing.get("bot_a_version_id"),
            "bot_b_version_id": pairing.get("bot_b_version_id"),
            "defer_start": True,
        }
        if tiebreak_group > 0:
            common["match_seed"] = self._duplicate_seed(pairing)
        mid: str | None = None
        bound = False
        bound_revision: int | None = None
        try:
            if want_duplicate:
                mid = await self.orch.challenge_duplicate(
                    pairing["bot_a_id"],
                    pairing["bot_b_id"],
                    duplicate_seed=self._duplicate_seed(pairing),
                    **common,
                )
            else:
                mid = await self.orch.challenge(
                    pairing["bot_a_id"], pairing["bot_b_id"], **common
                )
            if not mid:
                raise RuntimeError("challenge 未返回 match_id")
            bound_pairing = self.store.bind_contest_pairing_match(
                contest["id"],
                pairing["id"],
                mid,
                activate_running=activate_running,
                require_execution_admission=self._requires_live_admission(),
            )
            bound = True
            bound_revision = exact_nonnegative_int(
                bound_pairing.get("_bound_pairing_topology_revision")
            )
            if bound_revision is None:
                raise RuntimeError("对局绑定未返回冻结生命周期 revision")
            starter = getattr(self.orch, "start_prepared_match", None)
            if starter is not None:
                starter(mid)
            return mid
        except Exception:
            if mid is not None:
                safe_to_discard = not bound
                if bound:
                    try:
                        safe_to_discard = bool(
                            bound_revision is not None
                            and self.store.unbind_prepared_contest_match(
                                contest["id"],
                                pairing["id"],
                                mid,
                                expected_pairing_topology_revision=bound_revision,
                                restore_published=activate_running,
                            )
                        )
                    except Exception:
                        safe_to_discard = False
                        logger.exception(
                            "prepared match unbind compensation failed: "
                            "contest=%s pairing=%s match=%s",
                            contest["id"],
                            pairing["id"],
                            mid,
                        )
                discard = getattr(self.orch, "discard_prepared_match", None)
                if safe_to_discard and discard is not None and not discard(mid):
                    logger.error(
                        "prepared match compensation refused: contest=%s pairing=%s match=%s",
                        contest["id"], pairing["id"], mid,
                    )
                elif not safe_to_discard:
                    logger.error(
                        "prepared match retained after compensation authority drift: "
                        "contest=%s pairing=%s match=%s",
                        contest["id"], pairing["id"], mid,
                    )
            raise

    async def cancel(self, contest_id: int) -> dict:
        """取消未开赛赛事；与 publish/start/dispatch 共用锁并在锁内复核状态。"""
        async with self._lock(contest_id):
            c = self.store.get_contest(contest_id)
            if not c:
                raise ValueError("比赛不存在")
            require_mutable(c)
            if c["status"] == CONTEST_CANCELLED:
                return c
            if c["status"] not in (CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED):
                raise ValueError(
                    f"赛事处于 {c['status']} 态，不能取消（仅 draft/open/published 可取消）"
                )
            return self.store.update_contest(contest_id, status=CONTEST_CANCELLED)

    async def delete(self, contest_id: int) -> bool:
        """安全删除赛事：与 start/dispatch 共锁，拒绝运行态或任何 active match。

        published 尚未开打时先转 cancelled 再删除，明确其“取消排期后删除”语义；
        running/rest、finished 或任何已有正式榜的赛事一律拒绝，避免抹掉正式赛果。
        """
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                return False
            require_mutable(contest)
            official_ready = exact_sqlite_bool(
                contest.get("official_results_ready")
            )
            if (
                contest["status"] == CONTEST_FINISHED
                or official_ready is None
                or official_ready
                or self.store.list_official_results(contest_id)
            ):
                raise ValueError("已完成或已有正式赛果的赛事不能删除")
            if contest["status"] in (CONTEST_RUNNING, CONTEST_REST):
                raise ValueError("运行中或休息期赛事不能删除，请先完成或中止在途对局")
            if self.store.executions.contest_has_active_jobs(contest_id):
                raise ValueError("赛事仍有排队或执行中的请求，不能删除")
            if self.store.contest_has_active_matches(contest_id):
                raise ValueError("赛事仍有 pending/running 对局，不能删除")
            if contest["status"] == CONTEST_PUBLISHED:
                self.store.update_contest(contest_id, status=CONTEST_CANCELLED)
            return self.store.delete_contest(contest_id)

    def standings(
        self,
        contest_id: int,
        *,
        stage_idx: int | None = None,
        pairings: list[dict[str, Any]] | None = None,
        entries: list[dict[str, Any]] | None = None,
        contest: dict[str, Any] | None = None,
        expected_current_entry_ids: set[int] | None | object = _CURRENT_COHORT_UNSET,
    ) -> list[dict]:
        if contest is not None:
            try:
                snapshot_id = int(contest.get("id"))
            except (TypeError, ValueError):
                raise ValueError("赛事快照缺少有效 id") from None
            if snapshot_id != int(contest_id):
                raise ValueError("赛事快照 id 与 standings 请求不一致")
            c = contest
        else:
            c = self.store.get_contest(contest_id)
        if not c:
            return []
        stages = _parse_stages(c)
        try:
            if c.get("status") != CONTEST_FINISHED:
                validate_stage_ranking_topology(stages)
        except (TypeError, ValueError):
            return []
        current_stage_idx = contest_current_stage_index(
            c, stage_count=len(stages)
        )
        if current_stage_idx is None:
            return []
        if stage_idx is None:
            stage_idx = current_stage_idx
        if (
            isinstance(stage_idx, bool)
            or not isinstance(stage_idx, int)
            or stage_idx < 0
        ):
            return []
        stage_valid = bool(0 <= stage_idx < len(stages))
        stage = stages[stage_idx] if stage_valid else {}
        # 默认 scoring 只能从赛事声明的已注册游戏派生。
        gid = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        default_scoring = game_registry.get(gid).default_scoring
        game_spec = game_registry.get(gid)
        marker_binding_valid = reserved_group_markers_match_template(
            c.get("template_id"), stages, game_id=gid
        )
        stage_contract_valid = bool(
            stage_valid
            and stage_scoring_contract_is_valid(stage, game_id=gid)
            and marker_binding_valid
        )
        if not marker_binding_valid:
            return []
        duplicate = stage_duplicate_mode(stage) if stage_contract_valid else None
        planned_games = (
            planned_match_games(game_spec, duplicate=duplicate)
            if duplicate is not None
            else 1
        )
        scoring = stage["scoring"] if "scoring" in stage else default_scoring

        raw_entry_rows = (
            entries
            if entries is not None
            else self.store.list_contest_entries(contest_id)
        )
        entry_rows = _validated_standings_entries(raw_entry_rows)
        if entry_rows is None:
            # Imported/legacy SQLite rows are not protected by STRICT typing.
            # A malformed identity, group, seed or elimination flag cannot be
            # coerced into a plausible cohort/rank because that may move an
            # advancement boundary (and int("bad") used to make live/detail
            # raise instead of failing closed).
            return []
        active_entry_rows = active_contest_entries(entry_rows)
        if active_entry_rows is None:
            # The persisted SQLite flag has no CHECK constraint.  Imported
            # values such as -1/2 cannot be interpreted as elimination because
            # doing so would silently shrink the authoritative cohort.
            return []
        if stage_idx == current_stage_idx:
            if expected_current_entry_ids is _CURRENT_COHORT_UNSET:
                expected_current = self._expected_current_stage_participants(
                    contest_id,
                    stages,
                    current_stage_idx,
                    entry_rows,
                    active_entry_rows,
                )
            else:
                if not isinstance(expected_current_entry_ids, set):
                    return []
                expected_current = set()
                for entry_id in expected_current_entry_ids:
                    normalized_id = exact_nonnegative_int(entry_id)
                    if normalized_id is None or normalized_id < 1:
                        return []
                    expected_current.add(normalized_id)
                active_ids = {int(entry["id"]) for entry in active_entry_rows}
                if expected_current != active_ids:
                    return []
            if expected_current is None:
                return []
            entry_rows = [
                entry
                for entry in entry_rows
                if int(entry["id"]) in expected_current
            ]
            active_entry_rows = list(entry_rows)
        pairing_rows = (
            pairings
            if pairings is not None
            else self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        )
        all_pairing_rows = pairing_rows
        if (
            stage.get("type") == "single_elimination"
            and stage.get("tiebreak") == ELIMINATION_TIEBREAK_PAIRED_SWAP
        ):
            # 决胜组只决定晋级，不回写原阶段积分、胜负、分差或破同分。
            # 缺失坐标按历史主赛 0/0 解释；显式损坏坐标会在生命周期
            # validator/淘汰 resolver 处 fail closed，不能被当作加赛吞掉。
            pairing_rows = [
                row
                for row in pairing_rows
                if row.get("tiebreak_group", 0) == 0
                and row.get("tiebreak_game", 0) == 0
            ]
        expected_entry_bots = {
            int(entry["id"]): entry.get("bot_id") for entry in entry_rows
        }
        expected_entry_users = {
            int(entry["id"]): int(entry["user_id"]) for entry in entry_rows
        }
        require_current_entry_bots = bool(
            stage_idx == current_stage_idx
            and c.get("status") in (CONTEST_PUBLISHED, CONTEST_RUNNING)
        )
        if not stage_valid or duplicate is None:
            # A corrupt/non-object stage snapshot has no trustworthy scoring or
            # participant topology.  Keep the roster visible with zero totals,
            # but never reinterpret linked results as a default single stage.
            entry_rows = active_entry_rows
        elif require_current_entry_bots:
            # The current stage is defined by the frozen active roster, not by
            # whichever pairings happen to remain readable.  Otherwise a
            # deleted/import-missing whole entrant disappears from standings
            # and can silently move the advancement boundary.
            entry_rows = active_entry_rows
        elif (
            stage.get("series_scoring")
            in {SERIES_SCORING_AGGREGATE, SERIES_SCORING_INDEPENDENT}
            and stage_idx > 0
        ):
            # Explicit-series topology is derived from the frozen active
            # cohort.  The lifecycle marks non-qualifiers eliminated before it
            # materializes the next stage, so surviving pairing rows are not an
            # authoritative participant list: an imported/deleted whole
            # opponent group must leave that entrant visible at zero/pending.
            entry_rows = active_entry_rows
        elif stage_idx > 0 and pairing_rows:
            participant_entry_ids = {
                int(entry_id)
                for pairing in pairing_rows
                for entry_id in (
                    pairing.get("entry_a_id"),
                    pairing.get("entry_b_id"),
                )
                if isinstance(entry_id, int) and not isinstance(entry_id, bool)
            }
            entry_rows = [
                entry
                for entry in entry_rows
                if int(entry["id"]) in participant_entry_ids
            ]
        # P0：排名/积分键改为 entry.id（换 Bot 不丢历史分）。
        # pairing 存 entry_a_id/entry_b_id（生成时快照），用它定位 stats；
        # match 的 winner(座位0/1) 对应 pairing 的 a/b 侧。
        stats = {
            e["id"]: {
                "entry_id": e["id"],
                "bot_id": e["bot_id"],
                "user_id": e["user_id"],
                "seed": e.get("seed") or 0,
                "points": 0.0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "byes": 0,
                "delta_total": 0,
                "group_id": e.get("group_id") or "",
                "eliminated": int(contest_entry_eliminated(e) is True),
                "counts": {
                    "encounter_groups": 0,
                    "unique_opponents": 0,
                    "match_jobs": 0,
                    "scoring_games": 0,
                },
            }
            for e in entry_rows
        }
        encounter_keys: dict[int, set[tuple[int, int, int]]] = {
            int(entry_id): set() for entry_id in stats
        }
        opponent_ids: dict[int, set[int]] = {
            int(entry_id): set() for entry_id in stats
        }
        traditional_grouped = bool(
            stage.get("type")
            in {"group_round_robin", "group_double_round_robin"}
            and stage.get("overall_ranking") != "cross_group_fair_v1"
        )
        traditional_groups: dict[int, str] | None = None
        if traditional_grouped:
            traditional_groups, group_authority_valid = (
                traditional_group_authority(
                    stage,
                    set(stats),
                    {int(entry["id"]): entry for entry in entry_rows},
                    pairing_rows,
                    require_complete_topology=True,
                )
            )
            if not group_authority_valid or traditional_groups is None:
                return []
            for entry_id, group_id in traditional_groups.items():
                stats[entry_id]["group_id"] = group_id
        matches_by_id: dict[str, dict[str, Any]] = {}
        if not stage_valid or duplicate is None:
            return list(stats.values())

        def rank_live_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            from bzplat.backend.contests import ranking as _ranking

            ranking_kwargs = dict(
                normalize_delta=game_spec.normalize_delta,
                stage=stage,
                planned_games_per_match=planned_games,
                fixed_rounds_per_match=game_spec.fixed_rounds_per_match,
                game_id=gid,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )
            if (
                stage_idx == 0
                and c.get("template_id")
                in {
                    PENCIL_RANDOM_GROUP_TEMPLATE,
                    GOMOKU_PROTECTED_GROUP_TEMPLATE,
                }
            ):
                ranked = _ranking.compute_cross_group_ranking(
                    rows,
                    pairing_rows,
                    matches_by_id,
                    **ranking_kwargs,
                )
            elif traditional_groups is not None:
                ranked_groups: list[dict[str, Any]] = []
                rows_by_group: dict[str, list[dict[str, Any]]] = {}
                pairings_by_group: dict[str, list[dict[str, Any]]] = {}
                for row in rows:
                    group_id = traditional_groups[int(row["entry_id"])]
                    rows_by_group.setdefault(group_id, []).append(row)
                for pairing in pairing_rows:
                    pairings_by_group.setdefault(
                        str(pairing["group_id"]), []
                    ).append(pairing)
                for group_id in sorted(rows_by_group):
                    group_rows = rows_by_group[group_id]
                    group_pairings = pairings_by_group.get(group_id, [])
                    ranked = _ranking.compute_official_ranking(
                        group_rows,
                        group_pairings,
                        {
                            str(pairing["match_id"]): matches_by_id[
                                str(pairing["match_id"])
                            ]
                            for pairing in group_pairings
                            if pairing.get("match_id") is not None
                            and str(pairing["match_id"]) in matches_by_id
                        },
                        **ranking_kwargs,
                    )
                    if len(ranked) != len(group_rows):
                        return []
                    ranked_groups.extend(ranked)
                ranked = ranked_groups
            else:
                ranked = _ranking.compute_official_ranking(
                    rows,
                    pairing_rows,
                    matches_by_id,
                    **ranking_kwargs,
                )
            if stage.get("tiebreak") != ELIMINATION_TIEBREAK_PAIRED_SWAP:
                return ranked

            elimination_matches = dict(matches_by_id)
            for pairing in all_pairing_rows:
                match_id = pairing.get("match_id")
                if not match_id or str(match_id) in elimination_matches:
                    continue
                if "_match_result_json" in pairing:
                    raw_result = pairing.get("_match_result_json")
                    if isinstance(raw_result, str):
                        try:
                            result = json.loads(raw_result)
                        except (TypeError, ValueError):
                            result = {}
                    else:
                        result = (
                            raw_result if isinstance(raw_result, dict) else {}
                        )
                    match = {
                        "id": str(match_id),
                        "status": pairing.get("match_status"),
                        "winner": pairing.get("match_winner"),
                        "result": result,
                        "reason": pairing.get("_match_reason"),
                        "technical_loss": pairing.get(
                            "_match_technical_loss"
                        ),
                        "match_config": pairing.get("_match_config_json"),
                        "contest_id": pairing.get("_match_contest_id"),
                        "game_id": pairing.get("_match_game_id"),
                        "match_type": pairing.get("_match_type"),
                        "bot_a_id": pairing.get("_match_bot_a_id"),
                        "bot_b_id": pairing.get("_match_bot_b_id"),
                    }
                else:
                    match = self.store.get_match(str(match_id))
                if match is not None:
                    elimination_matches[str(match_id)] = match
            return _rank_paired_swap_elimination_rows(
                ranked,
                all_pairing_rows,
                elimination_matches,
                stage=stage,
                game_spec=game_spec,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
                require_decided=False,
            )

        if is_aggregate_series_stage(stage):
            for pairing in pairing_rows:
                match_id = pairing.get("match_id")
                if not match_id:
                    if (
                        stage.get("type") == "swiss"
                        and is_authoritative_no_opponent_pairing(
                            stage.get("type"), pairing
                        )
                        and contest_pairing_roster_binding_is_valid(
                            pairing,
                            expected_contest_id=contest_id,
                            expected_entry_bots=expected_entry_bots,
                            expected_entry_users=expected_entry_users,
                            require_current_entry_bots=require_current_entry_bots,
                            require_opponent=False,
                        )
                    ):
                        entry_id = pairing.get("entry_a_id")
                        if entry_id in stats:
                            stats[entry_id]["points"] += points_for_result(
                                scoring, 0, 0
                            )
                            stats[entry_id]["byes"] += 1
                    continue
                if "_match_result_json" in pairing:
                    raw_result = pairing.get("_match_result_json")
                    if isinstance(raw_result, str):
                        try:
                            result = json.loads(raw_result)
                        except (TypeError, ValueError):
                            result = {}
                    else:
                        result = raw_result if isinstance(raw_result, dict) else {}
                    matches_by_id[str(match_id)] = {
                        "id": str(match_id),
                        "status": pairing.get("match_status"),
                        "winner": pairing.get("match_winner"),
                        "result": result,
                        "reason": pairing.get("_match_reason"),
                        "technical_loss": pairing.get("_match_technical_loss"),
                        "match_config": pairing.get("_match_config_json"),
                        "contest_id": pairing.get("_match_contest_id"),
                        "game_id": pairing.get("_match_game_id"),
                        "match_type": pairing.get("_match_type"),
                        "bot_a_id": pairing.get("_match_bot_a_id"),
                        "bot_b_id": pairing.get("_match_bot_b_id"),
                    }
                else:
                    match = self.store.get_match(str(match_id))
                    if match:
                        matches_by_id[str(match_id)] = match

            for rows in group_conceptual_series(stage, pairing_rows).values():
                summary = summarize_conceptual_series(
                    stage,
                    rows,
                    matches_by_id.get,
                    game_spec=game_spec,
                    expected_contest_id=contest_id,
                    expected_entry_bots=expected_entry_bots,
                    expected_entry_users=expected_entry_users,
                    require_current_entry_bots=require_current_entry_bots,
                )
                first, second = summary["entries"]
                if first not in stats or second not in stats:
                    continue
                completed_matches = int(summary["completed_matches"])
                for entry_id in (first, second):
                    stats[entry_id]["counts"]["match_jobs"] += completed_matches
                    if completed_matches:
                        stats[entry_id]["counts"]["encounter_groups"] += 1
                if completed_matches:
                    opponent_ids[first].add(second)
                    opponent_ids[second].add(first)
                if summary["settled"]:
                    for entry_id in (first, second):
                        stats[entry_id]["delta_total"] += int(
                            summary["deltas"][entry_id]
                        )
                        stats[entry_id]["points"] += float(
                            summary["standings_points"][entry_id]
                        )
                        stats[entry_id]["counts"]["scoring_games"] += 1
                    winner_entry = summary["winner_entry"]
                    if winner_entry is None:
                        stats[first]["draws"] += 1
                        stats[second]["draws"] += 1
                    else:
                        loser_entry = second if winner_entry == first else first
                        stats[winner_entry]["wins"] += 1
                        stats[loser_entry]["losses"] += 1
            for entry_id, opponents in opponent_ids.items():
                stats[entry_id]["counts"]["unique_opponents"] = len(opponents)
            rows = list(stats.values())
            rows.sort(key=lambda row: (-row["points"], -row["delta_total"]))
            # Running legacy aggregate stages remain on their frozen one-score-
            # per-series semantics, but their live order must use the exact
            # historical official tie-break chain that advancement/finalize
            # will consume.  A points/delta-only preview could otherwise show
            # a different qualifier until the instant the stage is snapshotted.
            return rank_live_rows(rows)

        for p in pairing_rows:
            mid = p.get("match_id")
            if not mid:
                # Swiss 奇数轮的 bye 是显式 completed/no-match pairing。
                # 轮空获得本赛制的“胜场分”，但它不是一场对局：不增
                # wins/draws/losses、delta_total，也没有对手记录。KO bye
                # 是直接晋级，不在此计分。
                if stage.get("type") == "swiss" and (
                    is_authoritative_no_opponent_pairing(stage.get("type"), p)
                    and contest_pairing_roster_binding_is_valid(
                        p,
                        expected_contest_id=contest_id,
                        expected_entry_bots=expected_entry_bots,
                        expected_entry_users=expected_entry_users,
                        require_current_entry_bots=require_current_entry_bots,
                        require_opponent=False,
                    )
                ):
                    entry_id = p.get("entry_a_id")
                    if entry_id in stats:
                        stats[entry_id]["points"] += swiss_bye_points(
                            stage,
                            scoring=scoring,
                            scoring_games_per_match=planned_games,
                        )
                        stats[entry_id]["byes"] += 1
                continue
            if "_match_result_json" in p:
                raw_result = p.get("_match_result_json")
                if isinstance(raw_result, str):
                    try:
                        result = json.loads(raw_result)
                    except (TypeError, ValueError):
                        result = {}
                else:
                    result = raw_result if isinstance(raw_result, dict) else {}
                m = {
                    "id": str(mid),
                    "status": p.get("match_status"),
                    "winner": p.get("match_winner"),
                    "result": result,
                    "reason": p.get("_match_reason"),
                    "technical_loss": p.get("_match_technical_loss"),
                    "match_config": p.get("_match_config_json"),
                    "contest_id": p.get("_match_contest_id"),
                    "game_id": p.get("_match_game_id"),
                    "match_type": p.get("_match_type"),
                    "bot_a_id": p.get("_match_bot_a_id"),
                    "bot_b_id": p.get("_match_bot_b_id"),
                }
            else:
                m = self.store.get_match(mid)
            if not m or m["status"] != STATUS_COMPLETED:
                continue
            matches_by_id[str(mid)] = m
            ea_id = p.get("entry_a_id")
            eb_id = p.get("entry_b_id")
            if ea_id not in stats or eb_id not in stats:
                continue
            if not contest_match_binding_is_valid(
                p,
                m,
                expected_contest_id=contest_id,
                expected_game_id=gid,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            ):
                continue
            games = scoring_games_for_match(
                m,
                duplicate=duplicate,
                planned_games=planned_games,
                fixed_rounds_per_match=game_spec.fixed_rounds_per_match,
                require_frozen_duplicate=(
                    stage.get("series_scoring") == SERIES_SCORING_INDEPENDENT
                ),
                normalize_delta=game_spec.normalize_delta,
            )
            if not games:
                continue
            # A completed database row is not an authoritative played job until
            # its shared scoring parser succeeds.  Keep personal job/opponent/
            # encounter counts aligned with stage progress and W/D/L instead of
            # exposing malformed history as a zero-game opponent encounter.
            stats[ea_id]["counts"]["match_jobs"] += 1
            stats[eb_id]["counts"]["match_jobs"] += 1
            opponent_ids[int(ea_id)].add(int(eb_id))
            opponent_ids[int(eb_id)].add(int(ea_id))
            encounter_key = conceptual_series_key(stage, p)
            if encounter_key is not None:
                encounter_keys[int(ea_id)].add(encounter_key)
                encounter_keys[int(eb_id)].add(encounter_key)
            for game in games:
                if game.deltas is not None:
                    stats[ea_id]["delta_total"] += int(game.deltas[0])
                    stats[eb_id]["delta_total"] += int(game.deltas[1])
                stats[ea_id]["points"] += points_for_result(
                    scoring, game.winner, 0
                )
                stats[eb_id]["points"] += points_for_result(
                    scoring, game.winner, 1
                )
                stats[ea_id]["counts"]["scoring_games"] += 1
                stats[eb_id]["counts"]["scoring_games"] += 1
                if game.winner == 0:
                    stats[ea_id]["wins"] += 1
                    stats[eb_id]["losses"] += 1
                elif game.winner == 1:
                    stats[eb_id]["wins"] += 1
                    stats[ea_id]["losses"] += 1
                else:
                    stats[ea_id]["draws"] += 1
                    stats[eb_id]["draws"] += 1
        for entry_id, keys in encounter_keys.items():
            stats[entry_id]["counts"]["encounter_groups"] = len(keys)
            stats[entry_id]["counts"]["unique_opponents"] = len(
                opponent_ids[entry_id]
            )
        rows = list(stats.values())
        rows.sort(key=lambda r: (-r["points"], -r["delta_total"]))
        return rank_live_rows(rows)

    def _stage_done(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        _active_authority: tuple[
            list[dict[str, Any]], list[dict[str, Any]], set[int]
        ]
        | None = None,
    ) -> bool:
        contest = self.store.get_contest(contest_id)
        if not contest:
            return False
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        stages = _parse_stages(contest or {})
        current_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            return False
        if not reserved_group_markers_match_template(
            contest.get("template_id"), stages, game_id=game_id
        ):
            return False
        stage_type = (
            stages[stage_idx].get("type")
            if 0 <= stage_idx < len(stages)
            else None
        )
        stage = (
            stages[stage_idx]
            if 0 <= stage_idx < len(stages) and isinstance(stages[stage_idx], dict)
            else None
        )
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            return False
        assert stage is not None
        game_spec = game_registry.get(game_id)
        duplicate = stage_duplicate_mode(stage)
        if duplicate is None:
            return False
        # Pairing status is synchronised from completed Matches immediately
        # before this gate.  Most scheduler ticks therefore reject an active
        # O(n^2) stage with one indexed EXISTS; only the terminal candidate
        # materialises and strictly validates the complete topology/results.
        if self.store.contest_stage_has_incomplete_pairings(
            contest_id, stage_idx
        ):
            return False
        # Fresh contests carry a current-stage manifest that is moved atomically
        # by every stage/round append.  Validate it only after the indexed
        # incomplete-row fast gate, so ordinary active O(n^2) scheduler ticks do
        # not pay for a COUNT/anti-join.  Historical unsealed running rows retain
        # their legacy path; a non-NULL seal always fails closed.
        if not self.store.contest_stage_manifest_is_valid(
            contest_id,
            stage_idx,
            include_terminal_orphans=True,
        ):
            return False
        if contest.get("status") in (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
            CONTEST_REST,
        ):
            authority = _active_authority or self._active_current_stage_authority(
                contest, stages
            )
            if authority is None:
                return False
            stages, _entry_rows, expected_current = authority
        pairings = self.store.list_contest_pairings(
            contest_id, stage_idx=stage_idx
        )
        planned_games = planned_match_games(game_spec, duplicate=duplicate)
        entries = self.store.list_contest_entries(contest_id)
        active_entries = active_contest_entries(entries)
        if active_entries is None:
            return False
        expected_entry_ids = (
            sorted(expected_current)
            if contest.get("status")
            in (CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST)
            else [int(entry["id"]) for entry in active_entries]
        )
        cumulative_deltas: dict[int, int] = {
            int(entry_id): 0 for entry_id in expected_entry_ids
        }
        if not pairings:
            # An explicit series marker has a derivable empty topology only for
            # a zero/one-person cohort.  This also keeps already-running legacy
            # aggregate stages from wedging after the scoring cutover.  Any
            # larger empty graph is missing durable fixture rows.
            return bool(
                stage.get("series_scoring")
                in {SERIES_SCORING_AGGREGATE, SERIES_SCORING_INDEPENDENT}
                and len(expected_entry_ids) <= 1
            )
        expected_participants = set(expected_entry_ids)
        expected_entry_bots = {
            int(entry["id"]): entry.get("bot_id") for entry in entries
        }
        expected_entry_users = {
            int(entry["id"]): int(entry["user_id"]) for entry in entries
        }
        require_current_entry_bots = contest.get("status") in (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
        )
        if stage_type in {"round_robin", "double_round_robin"}:
            if not complete_round_robin_pairing_topology(
                stage, expected_participants, pairings
            ):
                return False
        elif stage_type in {
            "group_round_robin",
            "group_double_round_robin",
        }:
            entry_by_id = {int(entry["id"]): entry for entry in entries}
            _groups, group_authority_valid = traditional_group_authority(
                stage,
                expected_participants,
                entry_by_id,
                pairings,
                require_complete_topology=True,
            )
            if not group_authority_valid:
                return False
        elif stage_type == "swiss":
            round_numbers = [
                exact_nonnegative_int(pairing.get("round_num"))
                for pairing in pairings
            ]
            if any(
                round_num is None or round_num < 1
                for round_num in round_numbers
            ) or not complete_swiss_pairing_topology(
                stage,
                expected_participants,
                pairings,
                expected_rounds=max(
                    round_num
                    for round_num in round_numbers
                    if round_num is not None
                ),
            ):
                return False
        elif stage_type == "single_elimination":
            if not complete_single_elimination_pairing_topology(
                stage,
                expected_participants,
                pairings,
                get_match=self.store.get_match,
                game_id=game_id,
                contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
                require_champion=False,
            ):
                return False
        else:
            return False
        if "games_per_pair" in stage:
            real_pairings = [
                pairing
                for pairing in pairings
                if not is_authoritative_no_opponent_pairing(stage_type, pairing)
            ]
            if not series_rows_settled(
                stage,
                real_pairings,
                self.store.get_match,
                game_spec=game_spec,
                all_pairings=pairings,
                expected_entry_ids=expected_entry_ids,
                expected_swiss_rounds=(
                    effective_swiss_rounds(stage, len(expected_entry_ids))
                    if stage.get("type") == "swiss"
                    else None
                ),
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            ):
                return False
        for p in pairings:
            # 只有赛制允许且身份/对局/状态四项完整的 no-opponent 行才是
            # 已完成轮空。真实对手 Bot 被 FK SET NULL 后仍保留 entry_b_id，
            # 必须阻断阶段推进。
            if is_authoritative_no_opponent_pairing(stage_type, p):
                if not contest_pairing_roster_binding_is_valid(
                    p,
                    expected_contest_id=contest_id,
                    expected_entry_bots={
                        int(entry["id"]): entry.get("bot_id") for entry in entries
                    },
                    expected_entry_users={
                        int(entry["id"]): int(entry["user_id"])
                        for entry in entries
                    },
                    require_current_entry_bots=contest.get("status") in (
                        CONTEST_PUBLISHED,
                        CONTEST_RUNNING,
                    ),
                    require_opponent=False,
                ):
                    return False
                continue
            mid = p.get("match_id")
            if not mid:
                return False
            m = self.store.get_match(mid)
            # aborted 只表示对局被取消/未产生裁决，绝不是赛制上的
            # “已完成”。把它算作终态会让 KO 在 winner=None 时固定
            # 晋级座位 0，也会给 RR/Swiss 静默吞分。
            if not match_scoring_result_is_valid(
                stage,
                m,
                game_spec=game_spec,
                pairing=p,
                expected_contest_id=contest_id,
                expected_entry_bots={
                    int(entry["id"]): entry.get("bot_id") for entry in entries
                },
                expected_entry_users={
                    int(entry["id"]): int(entry["user_id"]) for entry in entries
                },
                require_current_entry_bots=contest.get("status") in (
                    CONTEST_PUBLISHED,
                    CONTEST_RUNNING,
                ),
            ):
                return False
            games = scoring_games_for_match(
                m,
                duplicate=duplicate,
                planned_games=planned_games,
                fixed_rounds_per_match=game_spec.fixed_rounds_per_match,
                require_frozen_duplicate=(
                    stage.get("series_scoring") == SERIES_SCORING_INDEPENDENT
                ),
                normalize_delta=game_spec.normalize_delta,
            )
            if not games:
                return False
            entry_a = int(p["entry_a_id"])
            entry_b = int(p["entry_b_id"])
            cumulative_deltas[entry_a] = cumulative_deltas.get(entry_a, 0) + sum(
                int(game.deltas[0]) for game in games
            )
            cumulative_deltas[entry_b] = cumulative_deltas.get(entry_b, 0) + sum(
                int(game.deltas[1]) for game in games
            )
        if any(
            normalized_delta_value(game_spec.normalize_delta, delta) is None
            for delta in cumulative_deltas.values()
        ):
            return False
        return True

    def _build_stage_result_rows(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        current_ranking: list[dict[str, Any]] | None = None,
        decision_input_snapshot: dict[str, Any] | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        list[dict[str, Any]],
        dict[int, str] | None,
        list[dict[str, Any]],
    ]:
        """Build one complete current-stage snapshot without writing it."""
        c = (
            decision_input_snapshot.get("contest")
            if isinstance(decision_input_snapshot, dict)
            else self.store.get_contest(contest_id)
        )
        if not c:
            raise ValueError("赛事不存在，无法固化阶段结果")
        if exact_nonnegative_int(c.get("id")) != contest_id:
            raise ValueError("阶段决策输入赛事身份不一致")
        stages = _parse_stages(c)
        current_stage_idx = contest_current_stage_index(
            c, stage_count=len(stages)
        )
        if current_stage_idx is None or stage_idx != current_stage_idx:
            raise ValueError("赛事当前阶段游标损坏或已变化，无法固化阶段结果")
        stage = stages[stage_idx]
        game_id = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            raise ValueError("阶段计分契约无效，无法固化阶段结果")
        key = stage.get("key") or f"stage{stage_idx}"
        grouped = str(stage.get("type") or "").startswith("group_")

        raw_entry_rows = (
            decision_input_snapshot.get("entries")
            if isinstance(decision_input_snapshot, dict)
            else self.store.list_contest_entries(contest_id)
        )
        entry_rows = _validated_standings_entries(raw_entry_rows)
        if entry_rows is None:
            raise ValueError("赛事冻结名册身份或状态损坏，无法固化阶段结果")
        active_entries = active_contest_entries(entry_rows)
        if active_entries is None:  # defensive: normalized immediately above
            raise ValueError("赛事冻结名册淘汰状态损坏，无法固化阶段结果")
        expected_by_id = {
            int(entry["id"]): entry for entry in active_entries
        }

        group_ranks: Counter = Counter()
        expected_stage_groups: dict[int, str] | None = {} if grouped else None
        snapshot_rows: list[dict[str, Any]] = []
        if isinstance(decision_input_snapshot, dict):
            raw_pairing_rows = decision_input_snapshot.get("pairings")
            if not isinstance(raw_pairing_rows, list):
                raise ValueError("阶段决策输入对阵快照损坏")
            pairing_rows = [dict(row) for row in raw_pairing_rows]
            # The candidate must be derived solely from the Store transaction
            # that produced its content token.  A caller-supplied ranking may
            # have observed different Match bytes and is intentionally ignored.
            ranked_rows = self._rank_stage_rows(
                contest_id,
                stage_idx,
                contest=dict(c),
                pairings=pairing_rows,
                entries=entry_rows,
                expected_current_entry_ids=set(expected_by_id),
            )
        else:
            ranked_rows = (
                [dict(row) for row in current_ranking]
                if current_ranking is not None
                else self._rank_stage_rows(contest_id, stage_idx)
            )
        seen_entries: set[int] = set()
        seen_ranks: set[int] = set()
        for s in ranked_rows:
            if not isinstance(s, dict):
                raise ValueError("阶段排名行类型无效，无法固化阶段结果")
            entry_id = exact_nonnegative_int(s.get("entry_id"))
            rank = exact_nonnegative_int(s.get("rank"))
            expected_entry = expected_by_id.get(entry_id) if entry_id else None
            if (
                entry_id is None
                or entry_id < 1
                or rank is None
                or rank < 1
                or entry_id in seen_entries
                or rank in seen_ranks
                or expected_entry is None
                or s.get("user_id") != expected_entry["user_id"]
                or s.get("bot_id") != expected_entry["bot_id"]
            ):
                raise ValueError("阶段排名成员、身份或名次无效")
            seen_entries.add(entry_id)
            seen_ranks.add(rank)

            raw_group_id = s.get("group_id", "")
            if not isinstance(raw_group_id, str):
                raise ValueError("阶段排名分组坐标无效")
            group_id = raw_group_id
            if grouped:
                if _clean_group_id(group_id) != group_id:
                    raise ValueError("分组阶段排名缺少合法组标识")
                roster_group = expected_entry.get("group_id", "")
                if roster_group and roster_group != group_id:
                    raise ValueError("阶段排名分组与冻结名册矛盾")
                assert expected_stage_groups is not None
                expected_stage_groups[entry_id] = group_id
                group_key = group_id
            else:
                # Entries retain their qualifier group across later stages.
                # That roster metadata is not a coordinate of this non-group
                # stage and must not leak into its stage snapshot.
                group_id = ""
                group_key = "_"
            group_ranks[group_key] += 1
            rank_in_group = (
                exact_nonnegative_int(s.get("rank_in_group"))
                if grouped and "rank_in_group" in s
                else group_ranks[group_key]
            )
            if rank_in_group != group_ranks[group_key]:
                raise ValueError("阶段组内名次不连续或与排序矛盾")
            tiebreaks = sanitize_public_contest_tiebreaks(s.get("tiebreaks"))
            if tiebreaks is None:
                # A completed independent snapshot must retain the exact
                # ranking chain that selected advancement.  Refuse a partial
                # durable row instead of later presenting a different order.
                raise ValueError("阶段破同分明细无效，无法固化阶段结果")
            snapshot_rows.append(
                {
                    "entry_id": entry_id,
                    "bot_id": s.get("bot_id"),
                    "stage_key": key,
                    "points": s["points"],
                    "wins": s["wins"],
                    "draws": s["draws"],
                    "losses": s["losses"],
                    "delta_total": s["delta_total"],
                    "group_id": group_id,
                    "rank_in_group": rank_in_group if grouped else rank,
                    "payload_json": json.dumps(
                        {
                            "tiebreaks": tiebreaks,
                            **(
                                {"overall_rank": rank}
                                if grouped
                                else {}
                            ),
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ),
                }
            )
        expected_entries = set(expected_by_id)
        if (
            seen_entries != expected_entries
            or seen_ranks != set(range(1, len(expected_entries) + 1))
        ):
            raise ValueError("阶段排名未精确覆盖当前权威参赛 cohort")
        return snapshot_rows, entry_rows, expected_stage_groups, ranked_rows

    @staticmethod
    def _decision_stage_groups(
        stage: dict[str, Any], ranked_rows: list[dict[str, Any]]
    ) -> dict[int, str] | None:
        if not str(stage.get("type") or "").startswith("group_"):
            return None
        groups: dict[int, str] = {}
        for row in ranked_rows:
            entry_id = exact_nonnegative_int(row.get("entry_id"))
            group_id = row.get("group_id")
            if (
                entry_id is None
                or entry_id < 1
                or entry_id in groups
                or _clean_group_id(group_id) != group_id
            ):
                raise ValueError("阶段决策分组坐标损坏")
            groups[entry_id] = group_id
        return groups

    def _ensure_stage_decision(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        current_ranking: list[dict[str, Any]] | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        int,
        list[dict[str, Any]],
        dict[int, str] | None,
    ]:
        """Return one immutable current-stage decision and its sealed revision.

        A durable batch always wins. It is validated before any Match replay and
        never rewritten. Only a wholly absent batch is computed, then installed
        against the exact sealed lifecycle revision observed before replay; a
        concurrent installer returns its own complete winner instead.
        """
        contest = self.store.get_contest(contest_id)
        if not contest:
            raise ValueError("赛事不存在，无法固化阶段决策")
        stages = _parse_stages(contest)
        current_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            raise ValueError("赛事当前阶段游标损坏或已变化")
        if contest.get("status") not in (CONTEST_RUNNING, CONTEST_REST):
            raise ValueError("仅运行中或休息中的赛事可固化阶段决策")
        entry_rows = _validated_standings_entries(
            self.store.list_contest_entries(contest_id)
        )
        if entry_rows is None:
            raise ValueError("赛事冻结名册身份或状态损坏")

        existing = self.store.list_stage_result_recovery_snapshots(
            contest_id, stage_idx=stage_idx
        )
        if not existing and contest.get("status") == CONTEST_REST:
            # REST is entered only after installing the completed stage's
            # immutable decision.  Replaying Match rows here would let a
            # restart or ranking-code upgrade change the advancement cohort.
            raise ValueError("休息阶段缺少不可变阶段决策，拒绝重放排名")
        if existing:
            ranked_rows = self._stage_ranking_from_recovery_snapshot(
                contest_id, stage_idx, _snapshot_rows=existing
            )
            if ranked_rows is None:
                raise ValueError("既有阶段决策缺失、残缺或损坏")
            expected_stage_groups = self._decision_stage_groups(
                stages[stage_idx], ranked_rows
            )
            stored_rows, revision = (
                self.store.install_contest_stage_results_if_absent(
                    contest_id,
                    stage_idx,
                    None,
                    expected_revision=None,
                    expected_input_token=None,
                    expected_status=str(contest["status"]),
                    expected_entries=entry_rows,
                    expected_stage_groups=expected_stage_groups,
                )
            )
        else:
            decision_input_snapshot = (
                self.store.contest_stage_decision_input_snapshot(
                    contest_id,
                    stage_idx,
                    expected_status=str(contest["status"]),
                )
            )
            if decision_input_snapshot is None:
                raise ValueError("阶段决策输入未处于完整冻结快照")
            expected_revision = exact_nonnegative_int(
                decision_input_snapshot.get("decision_revision")
            )
            expected_input_token = decision_input_snapshot.get(
                "decision_input_token"
            )
            if (
                expected_revision is None
                or not isinstance(expected_input_token, str)
            ):
                raise ValueError("阶段决策输入快照 token 损坏")
            (
                candidate_rows,
                candidate_entries,
                candidate_groups,
                _candidate_ranking,
            ) = self._build_stage_result_rows(
                contest_id,
                stage_idx,
                decision_input_snapshot=decision_input_snapshot,
            )
            stored_rows, revision = (
                self.store.install_contest_stage_results_if_absent(
                    contest_id,
                    stage_idx,
                    candidate_rows,
                    expected_revision=expected_revision,
                    expected_input_token=expected_input_token,
                    expected_status=str(contest["status"]),
                    expected_entries=candidate_entries,
                    expected_stage_groups=candidate_groups,
                )
            )
            entry_rows = candidate_entries

        ranked_rows = self._stage_ranking_from_recovery_snapshot(
            contest_id, stage_idx, _snapshot_rows=stored_rows
        )
        if ranked_rows is None:
            raise ValueError("已安装阶段决策无法严格恢复")
        expected_stage_groups = self._decision_stage_groups(
            stages[stage_idx], ranked_rows
        )
        return ranked_rows, revision, entry_rows, expected_stage_groups

    def _snapshot_stage_results(self, contest_id: int, stage_idx: int) -> None:
        self._ensure_stage_decision(contest_id, stage_idx)

    def _mark_stage_pairings_done(self, contest_id: int, stage_idx: int) -> None:
        """阶段真正完成时（_stage_done 通过后），把该 stage 已完成 match 的 pairing 标
        status='completed'。积分逻辑只读 match，不依赖 pairing.status——但前端对阵图 /
        管理端读 pairing.status 显示进度，原实现只在 dispatch 时设 'running'、从不收尾，
        导致阶段完成后 pairing 永显 running。"""
        for p in self.store.list_contest_pairings(contest_id, stage_idx=stage_idx):
            if p.get("status") == "completed":
                continue
            mid = p.get("match_id")
            if not mid:
                continue
            m = self.store.get_match(mid)
            if m and m["status"] == STATUS_COMPLETED:
                self.store.complete_contest_pairing_for_match(contest_id, mid)

    def _sync_completed_pairings(self, contest_id: int, stage_idx: int) -> int:
        """Idempotently repair per-match pairing status for one stage."""
        changed = 0
        for pairing in self.store.list_contest_pairings_needing_completion_sync(
            contest_id, stage_idx
        ):
            match_id = pairing.get("match_id")
            if self.store.complete_contest_pairing_for_match(
                contest_id, match_id
            ):
                changed += 1
        return changed

    def _backfill_actual_start(self, contest: dict) -> bool:
        """Repair legacy contests whose first match started with NULL starts_at."""
        if contest.get("status") not in (
            CONTEST_RUNNING,
            CONTEST_REST,
        ):
            return False
        actual = self.store.backfill_contest_actual_start(contest["id"])
        if actual is None:
            return False
        logger.warning(
            "contest %s repaired missing starts_at from first match: %s",
            contest["id"], actual,
        )
        return True

    def _plan_participant_advancement(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        contest: dict[str, Any] | None = None,
        ranked_rows: list[dict[str, Any]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """Purely project the complete post-advance roster and its CAS batch."""
        c = contest or self.store.get_contest(contest_id)
        if not c:
            raise ValueError("赛事不存在")
        stages = _parse_stages(c)
        current_stage_idx = contest_current_stage_index(c, stage_count=len(stages))
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            raise ValueError("赛事当前阶段游标损坏或已变化，拒绝计算晋级名单")
        stage = stages[stage_idx]
        game_id = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            # Recovery/resume can reach this helper independently of the usual
            # completed-stage gate.  Never coerce a damaged frozen advance
            # count and permanently eliminate entrants under invented rules.
            raise ValueError("阶段计分契约无效，拒绝计算晋级名单")
        entries = self.store.list_contest_entries(contest_id)
        active_entries = active_contest_entries(entries)
        if active_entries is None:
            raise ValueError("参赛者淘汰状态损坏，拒绝计算晋级名单")
        standings = (
            [dict(row) for row in ranked_rows]
            if ranked_rows is not None
            else None
        )
        must_use_snapshot = bool(
            c.get("status") == CONTEST_REST
            or (
                c.get("template_id") == GOMOKU_PROTECTED_GROUP_TEMPLATE
                and stage_idx == 0
            )
        )
        if must_use_snapshot:
            stored_snapshot_rows = self.store.list_stage_result_recovery_snapshots(
                contest_id, stage_idx=stage_idx
            )
            if stored_snapshot_rows:
                frozen_standings = self._stage_ranking_from_recovery_snapshot(
                    contest_id, stage_idx
                )
                if frozen_standings is None:
                    raise ValueError("休息期阶段快照损坏，拒绝重算晋级名单")
                standings = frozen_standings
            elif c.get("status") == CONTEST_REST:
                raise ValueError(
                    "休息阶段缺少不可变阶段决策，拒绝重放排名"
                )
            else:
                raise ValueError("保护种子赛缺少完整小组阶段快照")
        if standings is None:
            standings = self._rank_stage_rows(contest_id, stage_idx)
        normalized_entries: list[dict[str, Any]] = []
        seen_entry_ids: set[int] = set()
        seen_user_ids: set[int] = set()
        for entry in entries:
            entry_id = exact_nonnegative_int(entry.get("id"))
            user_id = exact_nonnegative_int(entry.get("user_id"))
            bot_id = exact_nonnegative_int(entry.get("bot_id"))
            seed = exact_nonnegative_int(entry.get("seed"))
            eliminated = exact_sqlite_bool(entry.get("eliminated"))
            group_id = entry.get("group_id")
            if (
                entry_id is None
                or entry_id < 1
                or user_id is None
                or user_id < 1
                or bot_id is None
                or bot_id < 1
                or seed is None
                or eliminated is None
                or not isinstance(group_id, str)
                or group_id != group_id.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in group_id)
                or entry_id in seen_entry_ids
                or user_id in seen_user_ids
            ):
                raise ValueError("赛事晋级名册身份或状态损坏")
            seen_entry_ids.add(entry_id)
            seen_user_ids.add(user_id)
            normalized_entries.append(
                {
                    **entry,
                    "id": entry_id,
                    "user_id": user_id,
                    "bot_id": bot_id,
                    "seed": seed,
                    "eliminated": int(eliminated),
                    "group_id": group_id,
                }
            )
        active_entry_ids = {
            int(entry["id"])
            for entry in normalized_entries
            if entry["eliminated"] == 0
        }
        expected_entry_ids = {int(entry["id"]) for entry in active_entries}
        if expected_entry_ids != active_entry_ids:
            raise ValueError("赛事晋级名册活跃状态损坏")
        ranked_entry_ids = {
            row.get("entry_id")
            for row in standings
            if isinstance(row.get("entry_id"), int)
            and not isinstance(row.get("entry_id"), bool)
        }
        if ranked_entry_ids != expected_entry_ids:
            raise ValueError("阶段排名不完整，拒绝计算晋级名单")
        # P0：advance 以 entry_id 为键（与 standings 一致，换 Bot 不影响晋级判定）。
        # 与 terminal cohort proof / presentation 共用同一严格选择器，
        # 禁止一路按数组位置、另一路按冻结名次分叉。
        advance = advancing_entry_ids(stage, standings, default_all=True)
        if advance is None:
            raise ValueError("阶段晋级规则或权威名次损坏")

        if (
            c.get("template_id") == GOMOKU_PROTECTED_GROUP_TEMPLATE
            and stage_idx == 0
        ):
            by_group_rank: dict[tuple[str, int], dict[str, Any]] = {}
            for row in standings:
                group_id = row.get("group_id")
                rank_in_group = exact_nonnegative_int(row.get("rank_in_group"))
                if (
                    not isinstance(group_id, str)
                    or not group_id
                    or rank_in_group is None
                    or rank_in_group < 1
                    or (group_id, rank_in_group) in by_group_rank
                ):
                    raise ValueError("保护种子小组排名坐标损坏")
                by_group_rank[(group_id, rank_in_group)] = row
            labels = sorted({key[0] for key in by_group_rank if key[0]})
            group_count = exact_nonnegative_int(stage.get("group_count"))
            if group_count is None or group_count < 2 or len(labels) != group_count:
                raise ValueError("保护种子小组数与冻结拓扑不一致")
            final_order: list[int] = []
            for rank_in_group in (1, 2):
                for label in labels:
                    row = by_group_rank.get((label, rank_in_group))
                    if row is None:
                        raise ValueError("保护种子小组晋级榜不完整")
                    final_order.append(int(row["entry_id"]))
            if len(set(final_order)) != len(final_order) or set(final_order) != advance:
                raise ValueError("保护种子决赛名单与每组前二不一致")
            final_seed = {
                entry_id: index
                for index, entry_id in enumerate(final_order, start=1)
            }
        else:
            final_seed = {}

        projected: list[dict[str, Any]] = []
        updates: list[dict[str, Any]] = []
        for entry in normalized_entries:
            after = dict(entry)
            if c.get("template_id") == GOMOKU_PROTECTED_GROUP_TEMPLATE and stage_idx == 0:
                after["eliminated"] = 0 if entry["id"] in final_seed else 1
                if entry["id"] in final_seed:
                    after["seed"] = final_seed[entry["id"]]
            elif entry["id"] not in advance:
                after["eliminated"] = 1
            projected.append(after)
            updates.append(
                {
                    "id": entry["id"],
                    "user_id": entry["user_id"],
                    "expected_bot_id": entry["bot_id"],
                    "expected_group_id": entry["group_id"],
                    "expected_seed": entry["seed"],
                    "expected_eliminated": entry["eliminated"],
                    "seed": after["seed"],
                    "eliminated": after["eliminated"],
                }
            )
        return projected, updates

    def _advance_participants(self, contest_id: int, stage_idx: int) -> None:
        """根据阶段配置原子标记淘汰（显式调用的兼容入口）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            return
        stages = _parse_stages(c)
        current_stage_idx = contest_current_stage_index(c, stage_count=len(stages))
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            raise ValueError("赛事当前阶段游标损坏或已变化，拒绝计算晋级名单")
        _projected, updates = self._plan_participant_advancement(
            contest_id, stage_idx, contest=c
        )
        self.store.apply_contest_entry_advancement(
            contest_id,
            stage_idx,
            updates,
            expected_status=str(c["status"]),
            expected_current_stage_idx=current_stage_idx,
        )

    async def _advance_and_begin_stage(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        ranked_rows: list[dict[str, Any]] | None = None,
        decision_revision: int | None = None,
        expected_stage_groups: dict[int, str] | None = None,
    ) -> None:
        """Compute the next roster/pairings, then commit the whole transition."""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("赛事不存在")
        stages = self._validated_active_lifecycle_stages(c, _parse_stages(c))
        current_stage_idx = contest_current_stage_index(c, stage_count=len(stages))
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            raise ValueError("赛事当前阶段游标损坏或已变化")
        next_stage_idx = stage_idx + 1
        if next_stage_idx >= len(stages):
            raise ValueError("赛事不存在下一阶段")
        if ranked_rows is None or decision_revision is None:
            (
                ranked_rows,
                decision_revision,
                _decision_entries,
                expected_stage_groups,
            ) = self._ensure_stage_decision(
                contest_id,
                stage_idx,
                current_ranking=ranked_rows,
            )
        if ranked_rows is None or decision_revision is None:
            raise ValueError("阶段排名缺失或损坏，拒绝推进")
        projected, updates = self._plan_participant_advancement(
            contest_id,
            stage_idx,
            contest=c,
            ranked_rows=ranked_rows,
        )
        _next_stage, next_specs, _entry_map = self._stage_pairing_plan(
            c, next_stage_idx, entry_rows=projected
        )
        if not next_specs:
            # A legitimate zero/one-person next stage has no pairing/state
            # batch.  Publish the deciding stage snapshot, the complete roster
            # advancement, official results and terminal status in one Store
            # transaction; applying advancement first would leave a half-
            # advanced running contest when finalization fails.
            result = self._finish_adjudicated_contest_locked(
                contest_id,
                stage_idx,
                context="empty-next-stage",
                entry_updates=updates,
                current_ranking=ranked_rows,
                decision_revision=decision_revision,
                allow_unreached_empty_stage=True,
            )
            if result is None:
                raise ValueError("空下一阶段终态发布失败，赛事保持原状态")
            return
        await self._begin_stage(
            contest_id,
            next_stage_idx,
            entry_rows=projected,
            entry_updates=updates,
            source_decision_revision=decision_revision,
            source_stage_groups=expected_stage_groups,
        )

    def _rank_stage_rows(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        contest: dict[str, Any] | None = None,
        pairings: list[dict[str, Any]] | None = None,
        entries: list[dict[str, Any]] | None = None,
        expected_current_entry_ids: set[int] | None | object = _CURRENT_COHORT_UNSET,
    ) -> list[dict]:
        """Rank one frozen stage with the same tie-break chain used at finish."""
        contest = contest or self.store.get_contest(contest_id)
        if not contest:
            return []
        stages = _parse_stages(contest)
        stage = stages[stage_idx] if 0 <= stage_idx < len(stages) else None
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        standings = self.standings(
            contest_id,
            stage_idx=stage_idx,
            pairings=pairings,
            entries=entries,
            contest=contest,
            expected_current_entry_ids=expected_current_entry_ids,
        )
        if stage is None or not stage_scoring_contract_is_valid(
            stage, game_id=game_id
        ):
            return []
        current_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        if current_stage_idx is None:
            return []
        ranked = [dict(row) for row in standings]
        traditional_grouped = bool(
            stage.get("type")
            in {"group_round_robin", "group_double_round_robin"}
            and stage.get("overall_ranking") != "cross_group_fair_v1"
        )
        if traditional_grouped:
            by_group: dict[str, list[dict[str, Any]]] = {}
            for row in ranked:
                group_id = row.get("group_id")
                rank_in_group = exact_nonnegative_int(row.get("rank"))
                if (
                    _clean_group_id(group_id) != group_id
                    or rank_in_group is None
                    or rank_in_group < 1
                ):
                    return []
                row["rank_in_group"] = rank_in_group
                by_group.setdefault(group_id, []).append(row)
            if any(
                {int(row["rank_in_group"]) for row in rows}
                != set(range(1, len(rows) + 1))
                for rows in by_group.values()
            ):
                return []
            ranked = [
                row
                for group_id in sorted(by_group)
                for row in sorted(
                    by_group[group_id], key=lambda item: item["rank_in_group"]
                )
            ]
            for overall_rank, row in enumerate(ranked, start=1):
                row["rank"] = overall_rank
        else:
            ranks = [exact_nonnegative_int(row.get("rank")) for row in ranked]
            if any(rank is None or rank < 1 for rank in ranks) or set(ranks) != set(
                range(1, len(ranked) + 1)
            ):
                return []
            ranked.sort(key=lambda row: int(row["rank"]))
        if stage.get("tiebreak") != ELIMINATION_TIEBREAK_PAIRED_SWAP:
            return ranked

        pairing_rows = (
            pairings
            if pairings is not None
            else self.store.list_contest_pairings(
                contest_id, stage_idx=stage_idx
            )
        )
        matches: dict[str, dict[str, Any]] = {}
        for pairing in pairing_rows:
            match_id = pairing.get("match_id")
            if not match_id:
                continue
            match: dict[str, Any] | None
            if "_match_result_json" in pairing:
                raw_result = pairing.get("_match_result_json")
                if isinstance(raw_result, str):
                    try:
                        result = json.loads(raw_result)
                    except (TypeError, ValueError):
                        result = {}
                else:
                    result = raw_result if isinstance(raw_result, dict) else {}
                match = {
                    "id": str(match_id),
                    "status": pairing.get("match_status"),
                    "winner": pairing.get("match_winner"),
                    "result": result,
                    "reason": pairing.get("_match_reason"),
                    "technical_loss": pairing.get("_match_technical_loss"),
                    "match_config": pairing.get("_match_config_json"),
                    "contest_id": pairing.get("_match_contest_id"),
                    "game_id": pairing.get("_match_game_id"),
                    "match_type": pairing.get("_match_type"),
                    "bot_a_id": pairing.get("_match_bot_a_id"),
                    "bot_b_id": pairing.get("_match_bot_b_id"),
                }
            else:
                match = self.store.get_match(str(match_id))
            if isinstance(match, dict):
                matches[str(match_id)] = match
        entry_rows = _validated_standings_entries(
            entries
            if entries is not None
            else self.store.list_contest_entries(contest_id)
        )
        if entry_rows is None:
            return []
        game_spec = game_registry.get(game_id)
        return _rank_paired_swap_elimination_rows(
            ranked,
            pairing_rows,
            matches,
            stage=stage,
            game_spec=game_spec,
            expected_contest_id=contest_id,
            expected_entry_bots={
                int(entry["id"]): entry.get("bot_id") for entry in entry_rows
            },
            expected_entry_users={
                int(entry["id"]): int(entry["user_id"])
                for entry in entry_rows
            },
            require_current_entry_bots=bool(
                stage_idx == current_stage_idx
                and contest.get("status") in (
                    CONTEST_PUBLISHED,
                    CONTEST_RUNNING,
                )
            ),
            require_decided=True,
        )

    async def handle_match_done(
        self,
        match_id: str,
        contest_id: int,
        *,
        retry_aborted: bool = False,
    ) -> dict | None:
        """赛事对局收尾的唯一回调入口。

        completed 才能进入积分/晋级检查。aborted 对局保留历史行，
        对应 pairing 原子复位 pending。只有 orchestrator 通过短暂 handoff
        显式证明是管理员主动中止时，才立即安全重派；platform_error
        等平台故障不在回调栈里无限快速重试，留给 scheduler/reconcile。
        """
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                contest = self.store.get_contest(contest_id)
                match = self.store.get_match(match_id)
                if not contest or not match:
                    return None
                if is_showcase(contest):
                    return contest
                if match.get("status") == STATUS_ABORTED:
                    pairing = self.store.reset_aborted_contest_pairing(
                        contest_id, match_id
                    )
                    if pairing:
                        if not retry_aborted:
                            backoff_at = (
                                datetime.now() + timedelta(seconds=30)
                            ).isoformat(timespec="seconds")
                            # 不要把原本更远的排期拉近；平台故障至少退避
                            # 30 秒，避免 scheduler 每个 tick 立即重创 match。
                            scheduled_at = max(
                                str(pairing.get("scheduled_at") or ""), backoff_at
                            )
                            pairing = self.store.update_contest_pairing(
                                pairing["id"], scheduled_at=scheduled_at
                            ) or pairing
                        logger.warning(
                            "contest match aborted without adjudication: contest=%s "
                            "pairing=%s match=%s reason=%s; reset to pending%s",
                            contest_id,
                            pairing["id"],
                            match_id,
                            match.get("reason"),
                            " with backoff" if not retry_aborted else " for admin redispatch",
                        )
                        if (
                            retry_aborted
                            and contest.get("status") == CONTEST_RUNNING
                        ):
                            # The queue keeps a terminal match's job in ``settling``
                            # until exact sandbox cleanup is confirmed.  Enqueue is
                            # idempotent for an active contest_pairing_id, so trying
                            # to redispatch before finalization merely returns the old
                            # job and makes "immediate" admin redispatch a no-op.
                            # Finalize after the orchestrator's cleanup barrier, then
                            # verify this specific job no longer occupies the pairing.
                            old_execution = self.store.executions.get_by_match(match_id)
                            self.store.executions.finalize_ready()
                            if old_execution is not None:
                                latest_execution = self.store.executions.get(
                                    str(old_execution["public_id"])
                                )
                                if latest_execution and latest_execution.get("status") in {
                                    "queued", "starting", "running", "settling"
                                }:
                                    logger.warning(
                                        "contest admin abort awaits execution cleanup: "
                                        "contest=%s pairing=%s request=%s",
                                        contest_id,
                                        pairing["id"],
                                        old_execution["public_id"],
                                    )
                                    return self.store.get_contest(contest_id)
                            pairing_stage_idx = exact_nonnegative_int(
                                pairing.get("stage_idx")
                            )
                            if pairing_stage_idx is None:
                                logger.error(
                                    "contest pairing has malformed stage cursor: "
                                    "contest=%s pairing=%s",
                                    contest_id,
                                    pairing.get("id"),
                                )
                                return self.store.get_contest(contest_id)
                            await self._dispatch_pending_locked(
                                contest_id, pairing_stage_idx
                            )
                    return self.store.get_contest(contest_id)
                if match.get("status") == STATUS_COMPLETED:
                    self.store.complete_contest_pairing_for_match(
                        contest_id, match_id
                    )
                result = await self._maybe_finish_locked(contest_id)
                latest = self.store.get_contest(contest_id)
                if latest and latest.get("status") == CONTEST_RUNNING:
                    latest_stages = _parse_stages(latest)
                    latest_stage_idx = contest_current_stage_index(
                        latest, stage_count=len(latest_stages)
                    )
                    if latest_stage_idx is not None:
                        await self._dispatch_pending_locked(
                            contest_id, latest_stage_idx
                        )
                return result or self.store.get_contest(contest_id)

    async def maybe_finish(self, contest_id: int) -> dict | None:
        """对局结束回调：检查当前阶段是否完成，进入 rest 或下一阶段。

        加 per-contest 锁串行化——防止多场对局同时完成的 on_match_done 并发回调
        + scheduler 并发调用导致重复生成轮次/重复对局。
        """
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                return await self._maybe_finish_locked(contest_id)

    async def _maybe_finish_locked(self, contest_id: int) -> dict | None:
        """maybe_finish 的实际逻辑（调用方已持锁）。"""
        c = self.store.get_contest(contest_id)
        if not c or c["status"] not in (CONTEST_RUNNING, CONTEST_REST):
            return None
        if is_showcase(c):
            return c
        if c["status"] == CONTEST_REST:
            return await self._maybe_auto_resume(contest_id)

        raw_stages = _parse_stages(c)
        stage_idx = contest_current_stage_index(
            c, stage_count=len(raw_stages)
        )
        if stage_idx is None:
            logger.error(
                "contest %s has malformed current_stage_idx; lifecycle blocked",
                contest_id,
            )
            return None
        self._sync_completed_pairings(contest_id, stage_idx)
        # Mirroring an already completed match is part of draining that active
        # work.  New rounds, stage snapshots and lifecycle transitions are not:
        # hold them until explicit maintenance end so ready cannot race a
        # scheduler write after the execution callback has quiesced.
        if self._execution_admission_error() is not None:
            return self.store.get_contest(contest_id)
        # Keep ordinary match callbacks on the indexed O(1) fast path.  Only a
        # candidate completed round/stage pays for the four-query predecessor
        # snapshot and its O(pairings) pure validation.
        if self.store.contest_stage_has_incomplete_pairings(
            contest_id, stage_idx
        ):
            return None
        authority = self._active_current_stage_authority(c, raw_stages)
        if authority is None:
            logger.error(
                "contest %s has incomplete predecessor/cohort authority; "
                "lifecycle blocked",
                contest_id,
            )
            return None
        stages, _entry_rows, _expected_current = authority
        if not self._stage_done(
            contest_id, stage_idx, _active_authority=authority
        ):
            # 瑞士制：当前轮完成则生成下一轮
            if stages and 0 <= stage_idx < len(stages):
                stage = stages[stage_idx]
                if stage.get("type") == "swiss":
                    await self._maybe_next_swiss_round(contest_id, stage_idx, stage)
            return None

        # 多轮赛制推进（500 人压测发现的 bug 修复）：
        # swiss / single_elimination 是「懒生成」轮次——_stage_done 只看现有 pairing 是否全完成，
        # 但 R1 完成时该阶段可能还需要更多轮（swiss 未到 total_rounds；淘汰赛胜者>1）。
        # 在判定阶段真正结束前，尝试生成下一轮；生成了则阶段未完成（return），否则继续。
        if stages and 0 <= stage_idx < len(stages):
            stage = stages[stage_idx]
            stype = stage.get("type") or ""
            if stype == "swiss":
                if await self._maybe_next_swiss_round(contest_id, stage_idx, stage):
                    return None  # 生成了下一轮，阶段未完成
            elif stype == "single_elimination":
                elimination_state = await self._maybe_next_elim_round(
                    contest_id, stage_idx, stage
                )
                if elimination_state == "created":
                    return None  # 生成了下一轮（半决赛/决赛），阶段未完成
                if elimination_state == "blocked":
                    # 淘汰赛已有 completed 和棋但没有权威晋级者。
                    # 保持 running，不 snapshot/finish/advance，也不擅自重赛。
                    return None
                if elimination_state != "champion":  # pragma: no cover - typed guard
                    logger.error(
                        "unknown elimination advance state: contest=%s stage=%s state=%r",
                        contest_id,
                        stage_idx,
                        elimination_state,
                    )
                    return None

        has_next = stage_idx + 1 < len(stages)
        if not has_next and self._has_unfinished_pairings(contest_id):
            # ``_stage_done`` intentionally checks only the current stage so it
            # can drive ordinary stage generation.  Freezing a terminal result
            # is stricter: a low-level delete/abort in any earlier stage must
            # not be hidden merely because the final stage completed.
            logger.error(
                "skip automatic finalization for unadjudicated contest=%s",
                contest_id,
            )
            return None

        self._mark_stage_pairings_done(contest_id, stage_idx)
        rest_min = int((stages[stage_idx].get("rest_after_minutes") or 0) if stages else 0)

        if has_next and rest_min > 0:
            (
                _ranked_rows,
                decision_revision,
                decision_entries,
                expected_stage_groups,
            ) = self._ensure_stage_decision(contest_id, stage_idx)
            ends = (datetime.now() + timedelta(minutes=rest_min)).isoformat(
                timespec="seconds"
            )
            self.store.enter_contest_rest_from_decision(
                contest_id,
                stage_idx,
                expected_revision=decision_revision,
                expected_status=CONTEST_RUNNING,
                expected_entries=decision_entries,
                expected_stage_groups=expected_stage_groups,
                rest_ends_at=ends,
            )
            return self.store.get_contest(contest_id)

        if has_next:
            (
                ranked_rows,
                decision_revision,
                _decision_entries,
                expected_stage_groups,
            ) = self._ensure_stage_decision(contest_id, stage_idx)
            await self._advance_and_begin_stage(
                contest_id,
                stage_idx,
                ranked_rows=ranked_rows,
                decision_revision=decision_revision,
                expected_stage_groups=expected_stage_groups,
            )
            return self.store.get_contest(contest_id)

        return self._finish_adjudicated_contest_locked(
            contest_id,
            stage_idx,
            context="automatic",
        ) or self.store.get_contest(contest_id)

    async def reconcile_running_contests(
        self, *, interruption_reason: str
    ) -> int:
        """恢复对账：让 active contest 与缺正式榜的 finished contest 收敛。

        解决三类「赛事卡 running」：
        1. match 全完成但 maybe_finish 回调丢失/异常被吞（生产 contest 25）→ 直接 maybe_finish。
        2. 历史 match 被 orphan_after_restart 清成 aborted，pairing 仍指它（生产 contest 24）→
           reset_dead_contest_pairings 复位后重派。
        3. pairing 建了 match 行但 _run_match 从未跑完（pending match，started_at=None）→
           识别为死 pairing 复位重派。
        4. prepare match 成功但 bind 前硬崩→删除未被 pairing 引用的 pending
           match/index/replay，保留原 pending pairing 重派。
        5. published 首阶段只写入部分 pairing 就硬崩→校验完整批次，
           仅在全部未绑定时原子重建；已有进度则显式报不一致。
        6. contest 已 finished、正式榜事务尚未提交就硬崩→幂等补算完整榜，
           避免 official-results 永久 409。

        maybe_finish 在 _stage_done=False 时只生成下一轮、不重派 pending pairing，
        所以对账须在 maybe_finish 之后显式 _dispatch_pending 死而复生的 pending pairing。
        返回处理的 contest 数。
        """
        interruption_reason = validate_orphan_recovery_reason(
            interruption_reason
        )
        maintenance = self.store.executions.is_maintenance_control(
            self.store.executions.control()
        )
        # Under deployment drain the dispatcher still invokes recovery so
        # already-active attempts can be compensated, but proactive contest
        # lifecycle writes must wait for explicit maintenance end.
        if maintenance:
            return 0

        # 0. 修复旧版本 active 赛事留下的观测时间线。终态赛事的
        # pairing/Match/replay 是不可变历史，不能在候选过滤前被同步或回填。
        for contest in self.store.list_contests_by_status(
            [CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST]
        ):
            self._backfill_actual_start(contest)
            raw_pairings = self.store.list_contest_pairings(contest["id"])
            stage_indices = {
                exact_nonnegative_int(pairing.get("stage_idx"))
                for pairing in raw_pairings
            }
            if None in stage_indices:
                logger.error(
                    "skip contest reconciliation for malformed pairing stage: "
                    "contest=%s",
                    contest["id"],
                )
                continue
            for stage_idx in stage_indices:
                assert stage_idx is not None
                self._sync_completed_pairings(contest["id"], stage_idx)

        # 1. 清理未绑定 prepared 幽灵 + 复位已绑定死 pairing。
        reset_n = self.store.reset_dead_contest_pairings(
            interruption_reason=interruption_reason
        )
        if reset_n:
            logger.info("恢复对账：清理/复位 %d 个幽灵对局或死 pairing", reset_n)

        contests = self.store.list_contests_by_status(
            [CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST]
        )
        contests.extend(self.store.list_unready_finished_contests())
        for c in contests:
            cid = c["id"]
            try:
                await self._reconcile_one(cid)
            except Exception:
                # 单个 contest 对账失败不阻塞其他——但必须可见（防静默卡死再复发）
                logger.exception("reconcile contest %s failed", cid)
        return len(contests)

    async def _reconcile_one(self, contest_id: int) -> None:
        """对账单个 contest：恢复 published 批次或收敛 running/rest。"""
        initial = self.store.get_contest(contest_id)
        if initial and initial["status"] == CONTEST_FINISHED:
            # finished 是终态，maybe_finish 不会再进入；正式榜落库若在终态提交后
            # 失败，只能由启动恢复显式补算。持赛事锁并重读，避免与同进程内的
            # force-finish/回调竞态；replace_official_results 自身是完整批次事务。
            async with self._lock(contest_id):
                latest = self.store.get_contest(contest_id)
                official_ready = (
                    exact_sqlite_bool(latest.get("official_results_ready"))
                    if latest
                    else None
                )
                if (
                    latest
                    and latest["status"] == CONTEST_FINISHED
                    and official_ready is False
                ):
                    try:
                        latest_stages = self._validated_active_lifecycle_stages(
                            latest, _parse_stages(latest)
                        )
                    except ValueError as exc:
                        logger.error(
                            "skip official-results recovery for invalid frozen "
                            "stage contract contest=%s: %s",
                            contest_id,
                            exc,
                        )
                        return
                    stage_idx = contest_current_stage_index(
                        latest, stage_count=len(latest_stages)
                    )
                    if stage_idx is None:
                        logger.error(
                            "skip official-results recovery for malformed "
                            "current_stage_idx contest=%s",
                            contest_id,
                        )
                        return
                    if stage_idx != len(latest_stages) - 1:
                        logger.error(
                            "skip official-results recovery before configured "
                            "terminal stage contest=%s current_stage=%s "
                            "final_stage=%s",
                            contest_id,
                            stage_idx,
                            len(latest_stages) - 1,
                        )
                        return
                    # A terminal status alone is not proof that every durable
                    # pairing was adjudicated.  Recovery uses the same
                    # all-stage fail-closed gate as automatic and force finish.
                    if self._has_unfinished_pairings(contest_id):
                        logger.error(
                            "skip official-results recovery for unfinished "
                            "terminal contest=%s",
                            contest_id,
                        )
                        return
                    # Stage snapshots are written before the terminal official
                    # batch.  Once the shared all-stage adjudication/topology
                    # gate succeeds, recover the already adjudicated order
                    # verbatim: reopening the DB may normalize old Match JSON
                    # under a newer result schema, and recomputing would
                    # silently rewrite history.
                    if self._finalize_official_results_from_stage_snapshots(
                        contest_id, stage_idx
                    ):
                        return
                    logger.error(
                        "skip official-results recovery without one complete "
                        "persisted stage decision contest=%s stage=%s",
                        contest_id,
                        stage_idx,
                    )
            return
        if initial and initial["status"] == CONTEST_PUBLISHED:
            initial_stages = _parse_stages(initial)
            stage_idx = contest_current_stage_index(
                initial, stage_count=len(initial_stages)
            )
            if stage_idx is None:
                logger.error(
                    "skip published recovery for malformed current_stage_idx "
                    "contest=%s",
                    contest_id,
                )
                return
            await self.ensure_published_pairings(contest_id, stage_idx)
            # 恢复后仅派发 scheduled_at<=now 的场次；未到点的仍保持
            # published，不把“启动恢复”偷换成“手动立即开赛”。
            await self._dispatch_pending(contest_id, stage_idx)
            await self.maybe_finish(contest_id)
            return
        # 第一轮 maybe_finish：能 finish 的直接 finish（match 全完成的场景）
        await self.maybe_finish(contest_id)
        c = self.store.get_contest(contest_id)
        if not c or c["status"] not in (CONTEST_RUNNING, CONTEST_REST):
            return  # 已 finish/advance
        if c["status"] == CONTEST_REST:
            return  # rest 期交由 _maybe_auto_resume（启动时点未到则等）

        current_stages = _parse_stages(c)
        stage_idx = contest_current_stage_index(
            c, stage_count=len(current_stages)
        )
        if stage_idx is None:
            logger.error(
                "skip running recovery for malformed current_stage_idx contest=%s",
                contest_id,
            )
            return
        # 第二轮：重派 pending 无 match_id 的 pairing（死而复生 + 新生成轮）。
        # 单侧 Bot 不可用时会落 completed 技术判负；双方不可用时
        # 明确保持 pending 阻塞，不伪造无 winner 的 aborted 结果。
        await self._dispatch_pending_safe(contest_id, stage_idx)
        # 第三轮：重派/技术裁决后再 maybe_finish，让阶段真正推进
        await self.maybe_finish(contest_id)

    async def _dispatch_pending_safe(
        self, contest_id: int, stage_idx: int
    ) -> None:
        """重派 pending pairing，对单个 pairing 的 Bot 不可用做公平裁决。

        _dispatch_pending 是批量 dispatch，任一 pairing 的 bot 删了会抛 ValueError 中断后续。
        此方法逐 pairing 隔离其他派发错误；Bot 缺失则与正常派发共用
        ``_adjudicate_unavailable_pairing`` 的单侧技术判负/双侧阻塞契约。
        """
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                await self._dispatch_pending_safe_locked(contest_id, stage_idx)

    async def _dispatch_pending_safe_locked(self, contest_id: int, stage_idx: int) -> None:
        """_dispatch_pending_safe 的实际逻辑（调用方已持锁）。"""
        c = self.store.get_contest(contest_id)
        # reconcile 在锁外按 running 快照选中赛事后，可能先被 finish 收尾；
        # 锁内必须重检，终态不得再派发或制造 aborted 占位对局。
        if not c or c["status"] != CONTEST_RUNNING:
            return
        if self._execution_admission_error() is not None:
            return
        authority = self._active_current_stage_authority(c, _parse_stages(c))
        if authority is None:
            logger.error(
                "contest redispatch blocked by invalid predecessor/cohort authority: "
                "contest=%s",
                contest_id,
            )
            return
        stages, _entry_rows, _expected_current = authority
        current_stage_idx = contest_current_stage_index(
            c, stage_count=len(stages)
        )
        requested_stage_idx = exact_nonnegative_int(stage_idx)
        if (
            current_stage_idx is None
            or requested_stage_idx is None
            or requested_stage_idx != current_stage_idx
        ):
            return
        stage_idx = requested_stage_idx
        gid = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        # 复式赛制判断（与 _dispatch_pending_locked 一致）——reconcile 重派也保留
        # duplicate 标志（复审 P2-2），否则同赛事会混入普通单场对局。
        stage_cfg = stages[stage_idx] if 0 <= stage_idx < len(stages) else None
        spec = game_registry.get(gid) if gid in REGISTERED_ENGINES else None
        duplicate = stage_duplicate_mode(stage_cfg)
        if not stage_scoring_contract_is_valid(stage_cfg, game_id=gid):
            logger.error(
                "contest redispatch blocked by malformed duplicate mode: "
                "contest=%s stage=%s",
                contest_id,
                stage_idx,
            )
            return
        want_duplicate = bool(
            duplicate and spec is not None and spec.build_match_plan is not None
        )
        pending = self.store.list_dispatchable_contest_pairings(
            contest_id,
            stage_idx=stage_idx,
            due_at=_now(),
        )
        slot_budget = self._dispatch_slot_budget()
        for p in pending:
            unavailable = self._adjudicate_unavailable_pairing(
                c,
                p,
                gid=gid,
                activate_running=False,
            )
            if unavailable != "ready":
                continue
            if slot_budget is not None and slot_budget <= 0:
                break
            try:
                await self._prepare_bind_start_pairing(
                    c,
                    p,
                    gid=gid,
                    want_duplicate=want_duplicate,
                    activate_running=False,
                )
                if slot_budget is not None:
                    slot_budget -= 1
            except Exception:
                logger.exception(
                    "reconcile: contest=%s pairing=%s 重派失败，保持 pending",
                    contest_id,
                    p["id"],
                )

    def _expected_current_stage_participants(
        self,
        contest_id: int,
        stages: list[dict[str, Any]],
        current_stage_idx: int,
        entry_rows: list[dict[str, Any]],
        active_entries: list[dict[str, Any]],
        *,
        allow_finished_legacy: bool = False,
    ) -> set[int] | None:
        """Prove the persisted current cohort without trusting active flags.

        Supported lifecycle topology either never shrinks the full roster or
        shrinks it once, immediately before the terminal stage. In the latter
        case the complete previous snapshot and its frozen advancement rule
        determine the exact current entrants; ``eliminated`` is accepted only
        when it names that same set.
        """
        if current_stage_idx == 0:
            return prove_current_stage_participants(
                stages,
                current_stage_idx,
                entry_rows,
                active_entries,
                previous_verified_ranking=None,
            )
        if not 0 < current_stage_idx < len(stages) or self.store is None:
            return None
        previous_stage_idx = current_stage_idx - 1
        previous_stage = stages[previous_stage_idx]
        previous_pairings = self.store.list_contest_pairings(
            contest_id, stage_idx=previous_stage_idx
        )
        previous_snapshots = self.store.list_stage_result_recovery_snapshots(
            contest_id, stage_idx=previous_stage_idx
        )
        no_previous_artifacts = not previous_pairings and not previous_snapshots
        if no_previous_artifacts and allow_finished_legacy:
            return {int(entry["id"]) for entry in active_entries}
        if self._has_unfinished_pairings(
            contest_id,
            through_stage_idx=previous_stage_idx,
            _predecessor_prefix_only=True,
        ):
            return None
        previous_ranking = self._stage_ranking_from_recovery_snapshot(
            contest_id, previous_stage_idx
        )
        if previous_ranking is None:
            return None
        if (
            previous_stage.get("type") == "single_elimination"
            and "advance_count" not in previous_stage
        ):
            return (
                {int(entry["id"]) for entry in active_entries}
                if allow_finished_legacy
                else None
            )
        return prove_current_stage_participants(
            stages,
            current_stage_idx,
            entry_rows,
            active_entries,
            previous_verified_ranking=previous_ranking,
        )

    def _active_current_stage_authority(
        self,
        contest: dict[str, Any],
        stages: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], set[int]] | None:
        """Prove an active current cohort from one fixed-query DB snapshot.

        The joined projection reads contest, every reached pairing/compact Match,
        roster and stage results under one SQLite ``BEGIN``.  This avoids both an
        O(pairings) ``get_match`` hot-path and combining artifacts that never
        coexisted.  Callers still use the existing Store status/cursor/manifest
        CAS before any lifecycle or dispatch write.
        """
        contest_id = exact_nonnegative_int(contest.get("id"))
        if contest_id is None or contest_id < 1:
            return None
        snapshot = self.store.contest_projection_snapshot(
            contest_id,
            include_stage_results=True,
        )
        if snapshot is None:
            return None
        snapshot_contest = snapshot.get("contest")
        if not isinstance(snapshot_contest, dict):
            return None
        # Never combine an input contest read with a newer/older projection.
        # A concurrent transition is retried by the surrounding scheduler tick.
        for field in (
            "id",
            "status",
            "current_stage_idx",
            "stages_json",
            "published_stage_pairing_count",
        ):
            if snapshot_contest.get(field) != contest.get(field):
                return None
        snapshot_stages = _parse_stages(snapshot_contest)
        if snapshot_stages != stages:
            return None
        try:
            validated_stages = self._validated_active_lifecycle_stages(
                snapshot_contest, snapshot_stages
            )
        except (TypeError, ValueError):
            return None
        current_stage_idx = contest_current_stage_index(
            snapshot_contest, stage_count=len(validated_stages)
        )
        if current_stage_idx is None:
            return None
        snapshot_pairings = snapshot.get("pairings")
        snapshot_results = snapshot.get("stage_results")
        if not isinstance(snapshot_pairings, list) or not isinstance(
            snapshot_results, list
        ):
            return None
        for artifacts in (snapshot_pairings, snapshot_results):
            for artifact in artifacts:
                if not isinstance(artifact, dict):
                    return None
                artifact_stage_idx = exact_nonnegative_int(
                    artifact.get("stage_idx")
                )
                if (
                    artifact_stage_idx is None
                    or artifact_stage_idx >= len(validated_stages)
                    or artifact_stage_idx > current_stage_idx
                ):
                    return None
        entry_rows = _validated_standings_entries(
            snapshot.get("entries")
        )
        if entry_rows is None:
            return None
        active_entries = active_contest_entries(entry_rows)
        if active_entries is None:
            return None
        # presentation imports manager's pure selectors, so use a local import
        # after module initialization to keep the shared state machine acyclic at
        # import time.  Every dependency below consumes only this snapshot.
        from bzplat.backend.contests.presentation import (
            build_stage_summaries,
            current_stage_cohort_from_summaries,
        )

        current_pairings = [
            pairing
            for pairing in snapshot_pairings
            if pairing.get("stage_idx") == current_stage_idx
        ]
        raw_manifest = snapshot_contest.get("published_stage_pairing_count")
        sealed_current_topology = current_stage_topology_seal_is_valid(
            snapshot_contest, current_pairings
        )
        unsealed_published_stage_zero_repair = bool(
            raw_manifest is None
            and snapshot_contest.get("status") == CONTEST_PUBLISHED
            and current_stage_idx == 0
        )
        summaries = build_stage_summaries(
            self,
            snapshot_contest,
            entry_rows,
            snapshot_pairings,
            stage_results=snapshot_results,
            # An unsealed legacy published stage-zero batch is the sole repair
            # exception.  It is accepted here only provisionally; the strict
            # materialized topology check below must pass before ensure installs
            # the manifest+seal and before any execution write is attempted.
            current_topology_sealed=(
                sealed_current_topology or unsealed_published_stage_zero_repair
            ),
        )
        expected = current_stage_cohort_from_summaries(
            snapshot_contest, entry_rows, summaries
        )
        if expected is None:
            return None
        if raw_manifest is None:
            current_summary = next(
                (
                    summary
                    for summary in summaries
                    if summary.get("stage_idx") == current_stage_idx
                ),
                None,
            )
            # An unsealed running/rest graph can change after this read with no
            # revision CAS in the dispatch transaction.  Reject it entirely.
            # The only bounded legacy repair is a stage-zero published batch:
            # `_ensure_published_pairings_locked` immediately validates its
            # exact IDs and installs the manifest+seal before any execution.
            if not (
                snapshot_contest.get("status") == CONTEST_PUBLISHED
                and current_stage_idx == 0
                and isinstance(current_summary, dict)
                and current_summary.get("_materialized_topology_valid") is True
            ):
                return None
        else:
            manifest = exact_nonnegative_int(raw_manifest)
            revision = exact_nonnegative_int(
                snapshot_contest.get("pairing_topology_revision")
            )
            sealed_revision = exact_nonnegative_int(
                snapshot_contest.get("sealed_pairing_topology_revision")
            )
            if (
                manifest is None
                or revision is None
                or sealed_revision != revision
                or len(current_pairings) != manifest
            ):
                return None
        return validated_stages, entry_rows, expected

    def _stage_ranking_from_recovery_snapshot(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        _snapshot_rows: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]] | None:
        """Validate and restore one exact stage ranking without Match replay.

        A partial snapshot must never become an official table. Participant
        identity comes from the lifecycle authority: every historical stage
        uses the full registered roster, while a legitimately reduced current
        stage is derived from the preceding complete snapshot and its frozen
        advancement rule. Active flags only confirm that derived set. Exact
        rank and tie-break values then come from the pre-terminal snapshot,
        never from its participant subset.
        """
        contest = self.store.get_contest(contest_id)
        if not contest:
            return None
        stages = _parse_stages(contest)
        current_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        if current_stage_idx is None:
            return None
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None or not 0 <= stage_idx < len(stages):
            return None
        stage = stages[stage_idx]
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            return None

        entry_rows = _validated_standings_entries(
            self.store.list_contest_entries(contest_id)
        )
        if entry_rows is None:
            return None
        active_entries = active_contest_entries(entry_rows)
        if active_entries is None:
            return None
        entries = {int(entry["id"]): entry for entry in entry_rows}
        pairings = self.store.list_contest_pairings(
            contest_id, stage_idx=stage_idx
        )
        active = {int(entry["id"]) for entry in active_entries}
        current_stage = stage_idx == current_stage_idx
        # The validated lifecycle topology permits at most one cohort shrink,
        # immediately before the terminal stage.  Consequently every durable
        # historical stage still belongs to the full registered roster, while
        # only the persisted current stage may use the forward active cohort.
        # Neither the pairing graph nor the snapshot being validated may name
        # a smaller historical cohort and thereby authenticate itself.
        expected = (
            self._expected_current_stage_participants(
                contest_id,
                stages,
                current_stage_idx,
                entry_rows,
                active_entries,
            )
            if current_stage
            else set(entries)
        )
        if expected is None:
            return None
        topology_entries: set[int] = set()
        for pairing in pairings:
            for key in ("entry_a_id", "entry_b_id"):
                value = pairing.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, int):
                    return None
                topology_entries.add(value)
        if not topology_entries.issubset(expected):
            return None
        if not pairings and len(expected) > 1:
            return None

        grouped = str(stage.get("type") or "").startswith("group_")
        expected_entry_groups: dict[int, str] | None = None
        if grouped:
            # Current random-group formats freeze the group on both the roster
            # and every pairing. Legacy group templates predate the roster
            # column and retain their authority only in pairing topology plus
            # the completed-stage snapshot. Reconcile both shapes strictly;
            # never infer a group from the snapshot being validated.
            topology_groups: dict[int, str] = {}
            for pairing in pairings:
                group_id = pairing.get("group_id")
                if not isinstance(group_id, str) or not group_id:
                    return None
                for key in ("entry_a_id", "entry_b_id"):
                    entry_id = pairing.get(key)
                    if entry_id is None:
                        continue
                    previous_group = topology_groups.setdefault(
                        int(entry_id), group_id
                    )
                    if previous_group != group_id:
                        return None
            expected_entry_groups = {}
            for entry_id in expected:
                roster_group = entries[entry_id].get("group_id")
                topology_group = topology_groups.get(entry_id)
                if not isinstance(roster_group, str):
                    return None
                if roster_group:
                    if topology_group != roster_group:
                        return None
                    expected_entry_groups[entry_id] = roster_group
                else:
                    if topology_group is None:
                        return None
                    expected_entry_groups[entry_id] = topology_group

        snapshots = (
            [dict(row) for row in _snapshot_rows]
            if _snapshot_rows is not None
            else self.store.list_stage_result_recovery_snapshots(
                contest_id, stage_idx=stage_idx
            )
        )
        if len(snapshots) != len(expected):
            return None
        restored: list[dict[str, Any]] = []
        seen_entries: set[int] = set()
        seen_ranks: set[int] = set()
        for snapshot in snapshots:
            entry_id = snapshot.get("entry_id")
            if (
                isinstance(entry_id, bool)
                or not isinstance(entry_id, int)
                or entry_id not in expected
                or entry_id not in entries
                or entry_id in seen_entries
            ):
                return None
            # New random-group snapshots persist a private exact overall rank;
            # old grouped snapshots keep failing closed rather than inventing
            # an inter-group order from incomparable raw points.
            rank = (
                snapshot.get("overall_rank")
                if grouped
                else snapshot.get("rank_in_group")
            )
            if (
                isinstance(rank, bool)
                or not isinstance(rank, int)
                or rank < 1
                or rank in seen_ranks
                or not isinstance(snapshot.get("tiebreaks"), dict)
            ):
                return None
            seen_entries.add(entry_id)
            seen_ranks.add(rank)
            entry = entries[entry_id]
            restored.append(
                {
                    "entry_id": entry_id,
                    "bot_id": snapshot.get("bot_id"),
                    "user_id": entry.get("user_id"),
                    "rank": rank,
                    "points": snapshot.get("points") or 0,
                    "wins": snapshot.get("wins") or 0,
                    "draws": snapshot.get("draws") or 0,
                    "losses": snapshot.get("losses") or 0,
                    "delta_total": snapshot.get("delta_total") or 0,
                    "group_id": snapshot.get("group_id") or "",
                    "rank_in_group": (
                        snapshot.get("rank_in_group") if grouped else None
                    ),
                    "tiebreaks": snapshot["tiebreaks"],
                }
            )
        if seen_entries != expected or seen_ranks != set(
            range(1, len(expected) + 1)
        ):
            return None
        if grouped and not complete_group_rank_coordinates(
            restored,
            expected_entry_groups=expected_entry_groups,
        ):
            return None
        restored.sort(key=lambda row: row["rank"])
        return restored

    def _official_entry_groups(
        self,
        contest_id: int,
        *,
        source_stage_idx: int | None = None,
    ) -> dict[int, object] | None:
        """Return exact source groups, with one strict all-blank legacy fallback."""
        groups: dict[int, object] = {}
        for entry in self.store.list_contest_entries(contest_id):
            entry_id = exact_nonnegative_int(entry.get("id"))
            if entry_id is None or entry_id < 1 or entry_id in groups:
                return None
            groups[entry_id] = entry.get("group_id")
        if not groups:
            return None
        nonempty_roster_groups = {
            entry_id: group_id
            for entry_id, group_id in groups.items()
            if isinstance(group_id, str) and group_id
        }
        if len(nonempty_roster_groups) == len(groups):
            if any(
                _clean_group_id(group_id) != group_id
                for group_id in nonempty_roster_groups.values()
            ):
                return None
            return nonempty_roster_groups
        if nonempty_roster_groups or any(group_id != "" for group_id in groups.values()):
            return None
        if source_stage_idx is None:
            return groups
        snapshot_rows = self.store.list_stage_results(
            contest_id, stage_idx=source_stage_idx
        )
        if not snapshot_rows:
            return groups
        if len(snapshot_rows) != len(groups):
            return None
        snapshot_groups: dict[int, str] = {}
        for row in snapshot_rows:
            entry_id = exact_nonnegative_int(row.get("entry_id"))
            group_id = row.get("group_id")
            if (
                entry_id is None
                or entry_id < 1
                or entry_id not in groups
                or entry_id in snapshot_groups
            ):
                return None
            if group_id == "":
                if any(
                    candidate.get("group_id") not in (None, "")
                    for candidate in snapshot_rows
                ):
                    return None
                return groups
            if _clean_group_id(group_id) != group_id:
                return None
            snapshot_groups[entry_id] = group_id
        if set(snapshot_groups) != set(groups) or not complete_group_rank_coordinates(
            snapshot_rows,
            expected_entry_groups=snapshot_groups,
        ):
            return None
        return snapshot_groups

    def _ranking_with_current_roster_identities(
        self,
        contest_id: int,
        ranking_rows: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Project historical scores onto the strict current roster identity.

        Stage snapshots intentionally keep the Bot that played that stage. A
        Bot may be replaced during rest, however, and the complete official
        table is keyed to the contest entry's current frozen user/Bot identity.
        Rebind only those identity fields by ``entry_id`` after ranking/merge;
        points, group provenance, tie-breaks, and order remain historical.
        """
        entry_rows = _validated_standings_entries(
            self.store.list_contest_entries(contest_id)
        )
        if entry_rows is None:
            raise ValueError("赛事冻结名册身份损坏，拒绝固化正式排名")
        roster_by_id = {int(entry["id"]): entry for entry in entry_rows}
        rebound: list[dict[str, Any]] = []
        seen_entries: set[int] = set()
        for row in ranking_rows:
            if not isinstance(row, dict):
                raise ValueError("正式排名行类型无效")
            entry_id = exact_nonnegative_int(row.get("entry_id"))
            if (
                entry_id is None
                or entry_id < 1
                or entry_id in seen_entries
                or entry_id not in roster_by_id
            ):
                raise ValueError("正式排名与冻结名册成员不一致")
            seen_entries.add(entry_id)
            entry = roster_by_id[entry_id]
            rebound.append(
                {
                    **row,
                    "entry_id": entry_id,
                    "user_id": entry["user_id"],
                    "bot_id": entry["bot_id"],
                }
            )
        if seen_entries != set(roster_by_id):
            raise ValueError("正式排名未精确覆盖冻结名册")
        return rebound

    def _finalize_official_results_from_stage_snapshots(
        self, contest_id: int, stage_idx: int
    ) -> bool:
        """Publish a complete pre-terminal snapshot after an interrupted commit."""
        from bzplat.backend.contests import ranking as _ranking

        snapshot = self.store.contest_projection_snapshot(
            contest_id, include_stage_results=True
        )
        if snapshot is None or not isinstance(snapshot.get("contest"), dict):
            return False
        contest = snapshot["contest"]
        revision = exact_nonnegative_int(
            contest.get("pairing_topology_revision")
        )
        sealed_revision = exact_nonnegative_int(
            contest.get("sealed_pairing_topology_revision")
        )
        manifest = exact_nonnegative_int(
            contest.get("published_stage_pairing_count")
        )
        official_ready = exact_sqlite_bool(
            contest.get("official_results_ready")
        )
        if (
            contest.get("status") != CONTEST_FINISHED
            or official_ready is not False
            or revision is None
            or sealed_revision != revision
            or manifest is None
        ):
            return False
        stages = _parse_stages(contest)
        if not 0 <= stage_idx < len(stages):
            return False
        entry_rows = _validated_standings_entries(snapshot.get("entries"))
        snapshot_rows = snapshot.get("stage_results")
        if entry_rows is None or not isinstance(snapshot_rows, list):
            return False
        current_snapshot_rows = [
            row
            for row in snapshot_rows
            if isinstance(row, dict) and row.get("stage_idx") == stage_idx
        ]
        current = self._stage_ranking_from_recovery_snapshot(
            contest_id, stage_idx, _snapshot_rows=current_snapshot_rows
        )
        if current is None:
            return False
        stage = stages[stage_idx]
        ranking_rows = current
        if _ranking.final_stage_replaces_previous_ranking(
            stage, stage_idx=stage_idx
        ):
            previous_snapshot_rows = [
                row
                for row in snapshot_rows
                if isinstance(row, dict)
                and row.get("stage_idx") == stage_idx - 1
            ]
            previous = self._stage_ranking_from_recovery_snapshot(
                contest_id,
                stage_idx - 1,
                _snapshot_rows=previous_snapshot_rows,
            )
            scope = (
                stage.get("ranking_scope", 8)
                if stage.get("ranking_mode") == "replace_top"
                else len(current)
            )
            if (
                previous is None
                or isinstance(scope, bool)
                or not isinstance(scope, int)
                or scope < 1
            ):
                return False
            expected_entry_groups = self._official_entry_groups(
                contest_id, source_stage_idx=stage_idx - 1
            )
            if expected_entry_groups is None:
                return False
            ranking_rows = _ranking.merge_replace_top(
                previous,
                current,
                scope=scope,
                expected_entry_groups=expected_entry_groups,
            )
            if len(ranking_rows) != len(previous):
                return False
        try:
            ranking_rows = self._ranking_with_current_roster_identities(
                contest_id, ranking_rows
            )
            expected_stage_groups = self._decision_stage_groups(stage, current)
            official_rows = _ranking.build_official_result_rows(
                ranking_rows, stage_idx=stage_idx
            )
            self.store.recover_finished_contest_official_results(
                contest_id,
                stage_idx,
                official_result_rows=official_rows,
                expected_revision=revision,
                expected_entries=entry_rows,
                expected_stage_groups=expected_stage_groups,
            )
        except ValueError:
            return False
        return True

    def _build_official_result_rows(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        current_ranking: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Build a complete official-result batch without persisting it.

        若末阶段显式 ``ranking_mode=replace_top``，或旧模板以资格阶段接
        单败淘汰：合成榜前段取末阶段顺序，后段取前一阶段未晋级者的
        冻结相对顺序。
        """
        from bzplat.backend.contests import ranking as _ranking
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("赛事不存在，拒绝固化正式排名")
        stages = _parse_stages(c)
        cur_stage = stages[stage_idx] if 0 <= stage_idx < len(stages) else None
        game_id = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(cur_stage, game_id=game_id):
            raise ValueError("阶段计分契约无效，拒绝固化正式排名")
        assert cur_stage is not None

        def _rank_stage(sidx: int) -> list[dict]:
            return self._rank_stage_rows(contest_id, sidx)

        ranking_rows = (
            [dict(row) for row in current_ranking]
            if current_ranking is not None
            else _rank_stage(stage_idx)
        )
        # 合成榜：显式 replace_top 决赛或旧资格赛→KO，都由末阶段排序
        # finalist cohort，再沿用上一阶段冻结顺序排列未晋级者。
        if _ranking.final_stage_replaces_previous_ranking(
            cur_stage, stage_idx=stage_idx
        ):
            scope = (
                cur_stage.get("ranking_scope", 8)
                if cur_stage.get("ranking_mode") == "replace_top"
                else len(ranking_rows)
            )
            if (
                isinstance(scope, bool)
                or not isinstance(scope, int)
                or scope < 1
            ):
                raise ValueError("末阶段正式排名范围无效")
            # Advancement may legitimately repurpose ``contest_entries.seed``
            # as the next-stage seat order.  Recomputing an earlier all-draw
            # group table after that write would silently change both qualifiers
            # and eliminated fallback order.  The stage-completion snapshot is
            # the exact ranking that made the advancement decision, so prefer it
            # for every cross-stage merge and require it for the protected-seed
            # format. Legacy formats without snapshots retain their old replay
            # fallback for compatibility.
            stage1_ranking = self._stage_ranking_from_recovery_snapshot(
                contest_id, stage_idx - 1
            )
            if stage1_ranking is None:
                if c.get("template_id") == GOMOKU_PROTECTED_GROUP_TEMPLATE:
                    raise ValueError("保护种子赛缺少完整小组阶段快照")
                stage1_ranking = _rank_stage(stage_idx - 1)
            expected_entry_groups = self._official_entry_groups(
                contest_id, source_stage_idx=stage_idx - 1
            )
            if expected_entry_groups is None:
                raise ValueError("赛事冻结名册身份损坏")
            ranking_rows = _ranking.merge_replace_top(
                stage1_ranking,
                ranking_rows,
                scope=scope,
                expected_entry_groups=expected_entry_groups,
            )
            if len(ranking_rows) != len(stage1_ranking):
                raise ValueError("跨阶段正式排名成员不完整")
        ranking_rows = self._ranking_with_current_roster_identities(
            contest_id, ranking_rows
        )
        return _ranking.build_official_result_rows(
            ranking_rows, stage_idx=stage_idx
        )

    def _finalize_official_results(self, contest_id: int, stage_idx: int) -> None:
        """计算全员正式名次（破同分）并落库 contest_official_results。"""
        self.store.replace_official_results(
            contest_id,
            self._build_official_result_rows(contest_id, stage_idx),
        )

    def _build_terminal_result_artifacts(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        contest: dict[str, Any],
        current_ranking: list[dict[str, Any]] | None = None,
    ) -> tuple[
        list[dict[str, Any]] | None,
        list[dict[str, Any]],
        dict[int, str] | None,
        list[dict[str, Any]],
    ]:
        """Build terminal official rows from one already-installed decision."""
        entry_rows = _validated_standings_entries(
            self.store.list_contest_entries(contest_id)
        )
        if entry_rows is None:
            raise ValueError("赛事冻结名册身份或状态损坏，无法结束赛事")
        active_entries = active_contest_entries(entry_rows)
        if active_entries is None:  # defensive: normalized above
            raise ValueError("赛事冻结名册淘汰状态损坏，无法结束赛事")
        active_by_id = {
            int(entry["id"]): entry for entry in active_entries
        }
        ranked_rows = (
            [dict(row) for row in current_ranking]
            if current_ranking is not None
            else self._stage_ranking_from_recovery_snapshot(contest_id, stage_idx)
        )
        if ranked_rows is None:
            raise ValueError("终态阶段决策缺失或损坏，拒绝重算排名")
        ranked_entry_ids = {int(row["entry_id"]) for row in ranked_rows}
        if ranked_entry_ids != set(active_by_id):
            raise ValueError("终态阶段决策与当前晋级 cohort 不一致")
        stages = _parse_stages(contest)
        expected_stage_groups = self._decision_stage_groups(
            stages[stage_idx], ranked_rows
        )
        official_ranking = [
            {
                **row,
                "bot_id": active_by_id[int(row["entry_id"])]["bot_id"],
                "user_id": active_by_id[int(row["entry_id"])]["user_id"],
            }
            for row in ranked_rows
        ]
        return (
            None,
            entry_rows,
            expected_stage_groups,
            self._build_official_result_rows(
                contest_id,
                stage_idx,
                current_ranking=official_ranking,
            ),
        )

    async def _maybe_next_swiss_round(
        self, contest_id: int, stage_idx: int, stage: dict
    ) -> bool:
        """瑞士轮当前轮完成后生成下一轮。返回是否生成了新一轮（True=阶段未完成）。"""
        contest = self.store.get_contest(contest_id)
        if not contest:
            return False
        stages = _parse_stages(contest)
        current_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            return False
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            return False
        game_spec = game_registry.get(game_id)
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        if not pairings:
            return False
        round_numbers: list[int] = []
        for pairing in pairings:
            raw_round = pairing.get("round_num")
            if (
                isinstance(raw_round, bool)
                or not isinstance(raw_round, int)
                or raw_round < 1
            ):
                return False
            round_numbers.append(raw_round)
        max_round = max(round_numbers)
        entries = self.store.list_contest_entries(contest_id)
        active_entries = active_contest_entries(entries)
        if active_entries is None:
            return False
        expected_entry_bots = {
            int(entry["id"]): entry.get("bot_id") for entry in entries
        }
        expected_entry_users = {
            int(entry["id"]): int(entry["user_id"]) for entry in entries
        }
        require_current_entry_bots = contest.get("status") in (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
        )

        def _is_adjudicated(pairing: dict[str, Any]) -> bool:
            """A Swiss history row is either a strict bye or a completed match."""
            if is_authoritative_no_opponent_pairing(
                stage.get("type"), pairing
            ):
                return contest_pairing_roster_binding_is_valid(
                    pairing,
                    expected_contest_id=contest_id,
                    expected_entry_bots=expected_entry_bots,
                    expected_entry_users=expected_entry_users,
                    require_current_entry_bots=require_current_entry_bots,
                    require_opponent=False,
                )
            match_id = pairing.get("match_id")
            if not match_id:
                return False
            match = self.store.get_match(match_id)
            return match_scoring_result_is_valid(
                stage,
                match,
                game_spec=game_spec,
                pairing=pairing,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )

        expected_entry_ids = [
            int(entry["id"])
            for entry in active_entries
        ]
        if "games_per_pair" in stage:
            real_pairings = [
                pairing
                for pairing in pairings
                if not is_authoritative_no_opponent_pairing(
                    stage.get("type"), pairing
                )
            ]
            if not series_rows_settled(
                stage,
                real_pairings,
                self.store.get_match,
                game_spec=game_spec,
                all_pairings=pairings,
                expected_entry_ids=expected_entry_ids,
                expected_swiss_rounds=max_round,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            ):
                return False

        # Every earlier round is part of the Swiss score/opponent/seat history.
        # A later round must never be generated while any row is unadjudicated;
        # otherwise drift in R1 could be skipped merely because R2 completed.
        if not all(_is_adjudicated(pairing) for pairing in pairings):
            return False
        total_rounds = effective_swiss_rounds(stage, len(expected_entry_ids))
        if max_round >= total_rounds:
            return False
        # 生成下一轮（P0：standings 键 entry_id；P1：bot_id 取 entry 当前值——
        # dispatch 换 Bot 后下一轮用新 Bot，已发布轮冻结不受影响）
        standings = self.standings(contest_id, stage_idx=stage_idx)
        standings_entry_ids = {
            row.get("entry_id")
            for row in standings
            if isinstance(row.get("entry_id"), int)
            and not isinstance(row.get("entry_id"), bool)
        }
        if standings_entry_ids != set(expected_entry_ids):
            # The shared ranking path returns no rows when cumulative normalized
            # deltas or another frozen scoring invariant are malformed.  Never
            # turn that fail-closed signal into an empty next-round batch.
            return False
        # entry_id → 该 entry 当前 bot_id（dispatch 后是新 Bot）
        entries = {e["id"]: e for e in self.store.list_contest_entries(contest_id)}
        entry_to_bot = {s["entry_id"]: entries.get(s["entry_id"], {}).get("bot_id") for s in standings}
        # 仍用发布轮的 bot_id 算 scores/played（积分/对手历史键稳定，不变）
        scores = {}
        bot_to_entry = {}
        for s in standings:
            cur_bot = entry_to_bot.get(s["entry_id"])
            if cur_bot is not None:
                scores[cur_bot] = s["points"]
                bot_to_entry[cur_bot] = s["entry_id"]
        bot_ids = [
            entry_to_bot[s["entry_id"]]
            for s in standings
            if s.get("eliminated") == 0
            and entry_to_bot.get(s["entry_id"]) is not None
        ]
        played: set[tuple[int, int]] = set()
        bye_counts_by_entry: Counter[int] = Counter()
        color_counts_by_entry: Counter[int] = Counter()
        for p in pairings:
            entry_a = p.get("entry_a_id")
            entry_b = p.get("entry_b_id")
            if is_authoritative_no_opponent_pairing(stage.get("type"), p):
                if entry_a is not None:
                    bye_counts_by_entry[int(entry_a)] += 1
                continue
            # Persisted A is the actual seat 0 after color_first materialization.
            # Count by stable entry identity so a rest-period Bot swap does not
            # reset that participant's first-move history.
            if entry_a is not None:
                color_counts_by_entry[int(entry_a)] += 1
            # 对手历史以 entry 身份为真相源；休息期换 Bot 后映射到当前
            # bot_id，避免换版本/换 Bot 后把同两名选手误当“未交手”。
            current_a = entry_to_bot.get(entry_a)
            current_b = entry_to_bot.get(entry_b)
            if current_a is not None and current_b is not None:
                played.add((min(current_a, current_b), max(current_a, current_b)))
        bye_counts = {
            bot_id: int(bye_counts_by_entry.get(entry_id, 0))
            for bot_id, entry_id in bot_to_entry.items()
        }
        color_counts = {
            bot_id: int(color_counts_by_entry.get(entry_id, 0))
            for bot_id, entry_id in bot_to_entry.items()
        }
        specs = generate_stage_pairings(
            stage,
            bot_ids,
            scores=scores,
            played=played,
            swiss_round=max_round + 1,
            color_counts=color_counts,
            bye_counts=bye_counts,
        )
        key = stage.get("key") or f"stage{stage_idx}"
        published_at = _now()
        pairing_rows: list[dict[str, Any]] = []
        series_stage = "games_per_pair" in stage
        prior_row_count = len(pairings)
        for ordinal, sp in enumerate(specs, start=1):
            bot_a_id, bot_b_id = self._materialize_pairing_seats(sp)
            if not sp.requires_match:
                pairing_rows.append(
                    {
                        "bot_a_id": bot_a_id,
                        "bot_b_id": None,
                        "round_num": sp.round_num,
                        "status": sp.status,
                        "stage_key": key,
                        "group_id": sp.group_id,
                        "bracket_slot": sp.bracket_slot,
                        "color_first": 0,
                        "series_index": sp.series_index,
                        "series_size": sp.series_size,
                        "entry_a_id": bot_to_entry.get(bot_a_id),
                        "entry_b_id": None,
                        "published_at": published_at,
                    }
                )
                continue
            pairing_rows.append(
                {
                    "bot_a_id": bot_a_id,
                    "bot_b_id": bot_b_id,
                    "round_num": sp.round_num,
                    "status": STATUS_PENDING,
                    "stage_key": key,
                    "group_id": sp.group_id,
                    "bracket_slot": sp.bracket_slot,
                    "color_first": 0,
                    "series_index": sp.series_index,
                    "series_size": sp.series_size,
                    "pairing_seed": (
                        self._private_pairing_seed(
                            contest_id, stage_idx, prior_row_count + ordinal
                        )
                        if series_stage
                        else None
                    ),
                    "entry_a_id": bot_to_entry.get(bot_a_id),
                    "entry_b_id": bot_to_entry.get(bot_b_id),
                    "published_at": published_at,
                    **self._version_snapshot(bot_a_id, bot_b_id),
                }
            )
        self.store.append_contest_round_pairings(
            contest_id,
            stage_idx,
            pairing_rows,
            expected_current_stage_idx=stage_idx,
            expected_previous_max_round=max_round,
        )
        self._dispatch_coverage.pop(contest_id, None)
        await self._dispatch_pending_locked(contest_id, stage_idx)
        return True

    async def _maybe_next_elim_round(
        self, contest_id: int, stage_idx: int, stage: dict
    ) -> EliminationAdvanceState:
        """Resolve one elimination round, appending swapped tiebreaks as needed."""
        contest = self.store.get_contest(contest_id)
        if not contest:
            return "blocked"
        stages = _parse_stages(contest)
        current_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            return "blocked"
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            return "blocked"
        game_spec = game_registry.get(game_id)
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        if not pairings:
            return "blocked"
        entries = self.store.list_contest_entries(contest_id)
        active_entries = active_contest_entries(entries)
        if active_entries is None:
            return "blocked"
        expected_entry_bots = {
            int(entry["id"]): entry.get("bot_id") for entry in entries
        }
        expected_entry_users = {
            int(entry["id"]): int(entry["user_id"]) for entry in entries
        }
        require_current_entry_bots = contest.get("status") in (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
        )
        round_numbers = [exact_nonnegative_int(p.get("round_num")) for p in pairings]
        if any(round_num is None or round_num < 1 for round_num in round_numbers):
            return "blocked"
        max_round = max(round_num for round_num in round_numbers if round_num is not None)
        cur = [
            p
            for p, round_num in zip(pairings, round_numbers)
            if round_num == max_round
        ]
        by_slot: dict[int, list[dict[str, Any]]] = {}
        for pairing in cur:
            slot = pairing.get("bracket_slot")
            if (
                isinstance(slot, bool)
                or not isinstance(slot, int)
                or slot < 0
            ):
                return "blocked"
            by_slot.setdefault(slot, []).append(pairing)

        winners: list[tuple[int, int]] = []  # (bot_id, entry_id)
        appended_tiebreak = False
        for slot in sorted(by_slot):
            slot_rows = by_slot[slot]
            byes = [
                row
                for row in slot_rows
                if is_authoritative_no_opponent_pairing(stage.get("type"), row)
            ]
            if byes:
                if len(slot_rows) != 1 or len(byes) != 1:
                    return "blocked"
                p = byes[0]
                if not contest_pairing_roster_binding_is_valid(
                    p,
                    expected_contest_id=contest_id,
                    expected_entry_bots=expected_entry_bots,
                    expected_entry_users=expected_entry_users,
                    require_current_entry_bots=require_current_entry_bots,
                    require_opponent=False,
                ):
                    return "blocked"
                if (
                    isinstance(p.get("bot_a_id"), bool)
                    or not isinstance(p.get("bot_a_id"), int)
                    or isinstance(p.get("entry_a_id"), bool)
                    or not isinstance(p.get("entry_a_id"), int)
                ):
                    return "blocked"
                winners.append((int(p["bot_a_id"]), int(p["entry_a_id"])))
                continue

            summary = summarize_elimination_encounter(
                stage,
                slot_rows,
                self.store.get_match,
                game_spec=game_spec,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )
            if summary["state"] == "decided":
                winner_entry = summary.get("winner_entry")
                if (
                    isinstance(winner_entry, bool)
                    or not isinstance(winner_entry, int)
                    or winner_entry not in expected_entry_bots
                    or not isinstance(expected_entry_bots[winner_entry], int)
                ):
                    return "blocked"
                winners.append(
                    (int(expected_entry_bots[winner_entry]), winner_entry)
                )
                continue
            if summary["state"] == "append_tiebreak":
                next_group = summary.get("next_tiebreak_group")
                if (
                    isinstance(next_group, bool)
                    or not isinstance(next_group, int)
                    or next_group < 1
                ):
                    return "blocked"
                primary = next(
                    (
                        row
                        for row in slot_rows
                        if row.get("tiebreak_group", 0) == 0
                        and row.get("tiebreak_game", 0) == 0
                    ),
                    None,
                )
                if primary is None:
                    return "blocked"
                published_at = _now()
                bot_a = int(primary["bot_a_id"])
                bot_b = int(primary["bot_b_id"])
                entry_a = int(primary["entry_a_id"])
                entry_b = int(primary["entry_b_id"])
                # A tiebreak is part of the already-published encounter.  Its
                # programs must therefore be the primary pairing's frozen
                # versions, even if an entrant activates a newer Bot version
                # between the draw and this callback.
                first_versions = {
                    "bot_a_version_id": primary.get("bot_a_version_id"),
                    "bot_b_version_id": primary.get("bot_b_version_id"),
                }
                second_versions = {
                    "bot_a_version_id": primary.get("bot_b_version_id"),
                    "bot_b_version_id": primary.get("bot_a_version_id"),
                }
                rows = [
                    {
                        "bot_a_id": bot_a,
                        "bot_b_id": bot_b,
                        "entry_a_id": entry_a,
                        "entry_b_id": entry_b,
                        "round_num": max_round,
                        "status": STATUS_PENDING,
                        "stage_key": stage.get("key") or f"stage{stage_idx}",
                        "bracket_slot": slot,
                        "color_first": 0,
                        "series_index": 1,
                        "series_size": 1,
                        "published_at": published_at,
                        "scheduled_at": published_at,
                        "tiebreak_group": next_group,
                        "tiebreak_game": 1,
                        **first_versions,
                    },
                    {
                        "bot_a_id": bot_b,
                        "bot_b_id": bot_a,
                        "entry_a_id": entry_b,
                        "entry_b_id": entry_a,
                        "round_num": max_round,
                        "status": STATUS_PENDING,
                        "stage_key": stage.get("key") or f"stage{stage_idx}",
                        "bracket_slot": slot,
                        "color_first": 0,
                        "series_index": 1,
                        "series_size": 1,
                        "published_at": published_at,
                        "scheduled_at": published_at,
                        "tiebreak_group": next_group,
                        "tiebreak_game": 2,
                        **second_versions,
                    },
                ]
                self.store.append_contest_elimination_tiebreak_pairings(
                    contest_id,
                    stage_idx,
                    max_round,
                    slot,
                    rows,
                    expected_current_stage_idx=stage_idx,
                    expected_previous_tiebreak_group=next_group - 1,
                )
                self._dispatch_coverage.pop(contest_id, None)
                appended_tiebreak = True
                continue

            if summary["state"] in {
                "awaiting_results",
                "legacy_draw",
                "invalid",
            }:
                logger.error(
                    "elimination encounter cannot advance: contest=%s stage=%s "
                    "round=%s slot=%s state=%s",
                    contest_id,
                    stage_idx,
                    max_round,
                    slot,
                    summary["state"],
                )
                return "blocked"

        if appended_tiebreak:
            await self._dispatch_pending_locked(contest_id, stage_idx)
            return "created"
        # 胜者 ≤1 → 已决出冠军，阶段真正完成
        if len(winners) <= 1:
            return "champion"
        # 用胜者生成下一轮（按 bracket_slot 顺序配对：相邻两胜者一组）
        key = stage.get("key") or f"stage{stage_idx}"
        next_round = max_round + 1
        published_at = _now()
        slot = 0
        pairing_rows: list[dict[str, Any]] = []
        for i in range(0, len(winners), 2):
            a_bot, a_entry = winners[i]
            if i + 1 < len(winners):
                # 相邻两胜者配对
                b_bot, b_entry = winners[i + 1]
                pairing_rows.append(
                    {
                        "bot_a_id": a_bot,
                        "bot_b_id": b_bot,
                        "round_num": next_round,
                        "status": STATUS_PENDING,
                        "stage_key": key,
                        "bracket_slot": slot,
                        "color_first": 0,
                        "entry_a_id": a_entry,
                        "entry_b_id": b_entry,
                        "published_at": published_at,
                        "tiebreak_group": 0,
                        "tiebreak_game": 0,
                        **self._version_snapshot(a_bot, b_bot),
                    }
                )
                slot += 1
            else:
                # 奇数末位胜者：轮空自动晋级（不打本轮）。
                # 创建「轮空占位 pairing」：bot_b_id=None、无 match、直接标 completed，
                # winner 固定为 bot_a（轮空者）。这样 _stage_done 把它视为已完成、
                # _maybe_next_elim_round 能从它收集到轮空胜者，下一轮配对时正常带入——
                # 确保奇数胜者（非 2 幂人数）无人丢失、阶段能 finish。
                pairing_rows.append(
                    {
                        "bot_a_id": a_bot,
                        "bot_b_id": None,
                        "round_num": next_round,
                        "status": STATUS_COMPLETED,
                        "stage_key": key,
                        "bracket_slot": slot,
                        "color_first": 0,
                        "entry_a_id": a_entry,
                        "entry_b_id": None,
                        "published_at": published_at,
                        "tiebreak_group": 0,
                        "tiebreak_game": 0,
                    }
                )
                slot += 1
        self.store.append_contest_round_pairings(
            contest_id,
            stage_idx,
            pairing_rows,
            expected_current_stage_idx=stage_idx,
            expected_previous_max_round=max_round,
        )
        self._dispatch_coverage.pop(contest_id, None)
        await self._dispatch_pending_locked(contest_id, stage_idx)
        return "created"

    async def _maybe_auto_resume(self, contest_id: int) -> dict | None:
        """maybe_finish 持锁链路调（rest→running 自动恢复）。调用方已持锁。"""
        c = self.store.get_contest(contest_id)
        if not c or c["status"] != CONTEST_REST:
            return None
        ends = c.get("rest_ends_at")
        if ends and ends <= _now():
            return await self._resume_locked(contest_id)
        return None

    async def resume(self, contest_id: int) -> dict:
        """rest→running（对外入口，获取 per-contest 锁）。

        scheduler tick（锁外）调本方法；maybe_finish 锁内链路调 _resume_locked
        （防 asyncio.Lock 不可重入死锁 + 防双发竞态，与 _dispatch_pending 同模式）。
        """
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                return await self._resume_locked(contest_id)

    async def _resume_locked(self, contest_id: int) -> dict:
        """resume 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] != CONTEST_REST:
            raise ValueError("当前不在休息期")
        # Resuming creates the next stage and moves the contest back to
        # running.  Gate before either write so deployment cannot leave a
        # misleading running stage with no admissible execution.
        self._require_execution_admission()
        authority = self._active_current_stage_authority(c, _parse_stages(c))
        if authority is None:
            raise ValueError("休息期前序阶段 cohort 或正式排名无法验证")
        stages, _entry_rows, _expected_current = authority
        stage_idx = contest_current_stage_index(c, stage_count=len(stages))
        if stage_idx is None:
            raise ValueError("赛事当前阶段游标损坏，拒绝恢复")
        if stage_idx + 1 >= len(stages):
            return self._finish_adjudicated_contest_locked(
                contest_id,
                stage_idx,
                context="resume-terminal",
            ) or self.store.get_contest(contest_id)
        if (
            not self._stage_done(
                contest_id, stage_idx, _active_authority=authority
            )
            or self._has_unfinished_pairings(contest_id)
        ):
            raise ValueError("休息期阶段对阵或裁决不完整，拒绝恢复晋级")
        await self._advance_and_begin_stage(contest_id, stage_idx)
        return self.store.get_contest(contest_id)

    async def advance(self, contest_id: int) -> dict:
        """组织者强制推进（跳过未完成检查时仅在阶段已完成时可用）。"""
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                c = self.store.get_contest(contest_id)
                if not c:
                    raise ValueError("比赛不存在")
                require_mutable(c)
                self._require_execution_admission()
                if c["status"] == CONTEST_REST:
                    return await self._resume_locked(contest_id)
                stages = _parse_stages(c)
                stage_idx = contest_current_stage_index(
                    c, stage_count=len(stages)
                )
                if stage_idx is None:
                    raise ValueError("赛事当前阶段游标损坏，拒绝推进")
                if not self._stage_done(contest_id, stage_idx):
                    raise ValueError("当前阶段对阵尚未全部完成")
                return (
                    await self._maybe_finish_locked(contest_id)
                ) or self.store.get_contest(contest_id)

    async def finish(self, contest_id: int) -> dict:
        """组织者/admin 强制结束赛事（running/rest → finished）。

        用于所有已派发对局都进入终态、但自动阶段推进卡住时的手动出口。
        当前 runner 没有 contest-aware abort，因此仍有 pending/running 对局时明确拒绝，
        避免先写 finished 后后台任务继续晚写结果。
        """
        async with self._lock(contest_id):
            return self._finish_locked(contest_id)

    def _finish_adjudicated_contest_locked(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        gate_stage_idx: int | None = None,
        context: str,
        raise_on_unfinished: bool = False,
        entry_updates: list[dict[str, Any]] | None = None,
        current_ranking: list[dict[str, Any]] | None = None,
        decision_revision: int | None = None,
        allow_unreached_empty_stage: bool = False,
    ) -> dict | None:
        """Publish one terminal status and official table behind the shared gate.

        ``gate_stage_idx`` is normally the persisted current stage.  Stage
        creation passes its intended target so an entirely missing next-stage
        batch cannot be hidden by the previous stage's complete graph.
        """
        contest = self.store.get_contest(contest_id)
        try:
            stages = (
                self._validated_active_lifecycle_stages(
                    contest, _parse_stages(contest)
                )
                if contest
                else []
            )
        except ValueError as exc:
            message = "赛事冻结阶段或正式排名拓扑无效，无法结束赛事"
            if raise_on_unfinished:
                raise ValueError(message) from exc
            logger.error(
                "skip %s finalization for invalid frozen stage contract "
                "contest=%s: %s",
                context,
                contest_id,
                exc,
            )
            return None
        if contest and contest.get("template_id") == GOMOKU_PROTECTED_GROUP_TEMPLATE:
            persisted_stage_idx = contest_current_stage_index(
                contest, stage_count=len(stages)
            )
            requested_stage_idx = exact_nonnegative_int(stage_idx)
            if persisted_stage_idx != 1 or requested_stage_idx != 1:
                message = "保护种子分组赛尚未完成决赛阶段，无法结束赛事"
                if raise_on_unfinished:
                    raise ValueError(message)
                logger.error(
                    "skip %s finalization before protected final stage: "
                    "contest=%s current_stage=%r requested_stage=%r",
                    context,
                    contest_id,
                    persisted_stage_idx,
                    stage_idx,
                )
                return None
        persisted_stage_idx = (
            contest_current_stage_index(contest, stage_count=len(stages))
            if contest
            else None
        )
        if (
            persisted_stage_idx is None
            or not self.store.contest_stage_manifest_is_valid(
                contest_id,
                persisted_stage_idx,
                include_terminal_orphans=True,
            )
        ):
            message = "赛事当前阶段对阵批次完整性校验失败，无法结束赛事"
            if raise_on_unfinished:
                raise ValueError(message)
            logger.error(
                "skip %s finalization for invalid stage manifest contest=%s",
                context,
                contest_id,
            )
            return None
        if persisted_stage_idx != len(stages) - 1:
            active_entries = active_contest_entries(
                self.store.list_contest_entries(contest_id)
            )
            valid_empty_stage_shortcut = bool(
                allow_unreached_empty_stage
                and active_entries is not None
                and (
                    (
                        context == "empty-next-stage"
                        and persisted_stage_idx == len(stages) - 2
                        and entry_updates is not None
                        and current_ranking is not None
                    )
                    or (
                        context == "empty-stage"
                        and gate_stage_idx == persisted_stage_idx == 0
                        and len(active_entries) <= 1
                    )
                )
            )
            if not valid_empty_stage_shortcut:
                message = "赛事尚未到达配置中的最终阶段，无法结束赛事"
                if raise_on_unfinished:
                    raise ValueError(message)
                logger.error(
                    "skip %s finalization before configured terminal stage: "
                    "contest=%s current_stage=%s final_stage=%s",
                    context,
                    contest_id,
                    persisted_stage_idx,
                    len(stages) - 1,
                )
                return None
        if self._has_unfinished_pairings(
            contest_id,
            through_stage_idx=gate_stage_idx,
        ):
            message = (
                "赛事仍有未完成对阵，无法强制结束；"
                "请等待对局完成或先安全中止对局"
            )
            if raise_on_unfinished:
                raise ValueError(message)
            logger.error(
                "skip %s finalization for unadjudicated contest=%s",
                context,
                contest_id,
            )
            return None
        try:
            if current_ranking is None or decision_revision is None:
                (
                    current_ranking,
                    decision_revision,
                    _decision_entries,
                    _decision_groups,
                ) = self._ensure_stage_decision(
                    contest_id,
                    stage_idx,
                    current_ranking=current_ranking,
                )
            (
                stage_result_rows,
                expected_entries,
                expected_stage_groups,
                official_result_rows,
            ) = self._build_terminal_result_artifacts(
                contest_id,
                stage_idx,
                contest=contest,
                current_ranking=current_ranking,
            )
            return self.store.finish_contest_with_results(
                contest_id,
                stage_idx,
                stage_result_rows=stage_result_rows,
                official_result_rows=official_result_rows,
                expected_decision_revision=decision_revision,
                expected_status=str(contest["status"]),
                expected_entries=expected_entries,
                expected_stage_groups=expected_stage_groups,
                entry_updates=entry_updates,
                ends_at=_now(),
            )
        except Exception as exc:
            if raise_on_unfinished:
                raise ValueError(
                    "赛事阶段或正式排名无法完整固化，终态未写入"
                ) from exc
            logger.exception(
                "%s terminal result transaction failed contest=%s",
                context,
                contest_id,
            )
            return None

    def _finish_locked(self, contest_id: int) -> dict:
        """finish 的实际逻辑（调用方已持 per-contest 锁并在此重读状态）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] not in (CONTEST_RUNNING, CONTEST_REST):
            raise ValueError("仅运行中/休息中的赛事可强制结束")
        stages = _parse_stages(c)
        stage_idx = contest_current_stage_index(c, stage_count=len(stages))
        if stage_idx is None:
            raise ValueError("赛事当前阶段游标损坏，拒绝结束")
        result = self._finish_adjudicated_contest_locked(
            contest_id,
            stage_idx,
            context="force-finish",
            raise_on_unfinished=True,
        )
        assert result is not None  # raise_on_unfinished guarantees a result.
        return result

    def _has_unfinished_pairings(
        self,
        contest_id: int,
        *,
        through_stage_idx: int | None = None,
        _predecessor_prefix_only: bool = False,
    ) -> bool:
        """全赛事终局裁决门禁；调用方须持赛事锁。

        自动终局、finished 恢复和强制结束都只能通过这一门禁。它检查所有已到达
        阶段，而非只看当前阶段：每行必须是权威轮空，或绑定一场真实 completed
        Match。持久化的后续空阶段一律 fail closed，防止按空 standings 固化/反转
        名次。仅初始 0/1 人阶段，或 ``_begin_stage`` 明确尝试紧邻下一阶段且只剩
        一名 active 参赛者时，空图才是合法终局。

        当前 orchestrator 没有能等待 runner 收敛的 contest-aware abort。与其先写
        finished 后让后台任务晚写结果，保守拒绝任何未绑定、缺失或仍活跃的对阵；
        同时检查未被 pairing 正确绑定的赛事活跃 Match。
        """
        if (
            not _predecessor_prefix_only
            and self.store.contest_has_active_matches(contest_id)
        ):
            return True
        contest = self.store.get_contest(contest_id)
        if not contest:
            return True
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        game_spec = game_registry.get(game_id)
        stages = _parse_stages(contest or {})
        persisted_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        if persisted_stage_idx is None:
            return True
        explicit_next_stage = through_stage_idx is not None
        if through_stage_idx is None:
            current_stage_idx = persisted_stage_idx
        elif (
            isinstance(through_stage_idx, bool)
            or not isinstance(through_stage_idx, int)
        ):
            return True
        else:
            current_stage_idx = through_stage_idx
        if current_stage_idx < 0 or current_stage_idx >= len(stages):
            return True

        entries = _validated_standings_entries(
            self.store.list_contest_entries(contest_id)
        )
        if entries is None:
            return True
        active_entries = active_contest_entries(entries)
        if active_entries is None:
            return True
        if _predecessor_prefix_only:
            if current_stage_idx >= persisted_stage_idx:
                return True
            expected_current_stage_ids = set()
        else:
            expected_current_stage_ids = self._expected_current_stage_participants(
                contest_id,
                stages,
                persisted_stage_idx,
                entries,
                active_entries,
            )
            if expected_current_stage_ids is None:
                return True
        expected_entry_bots = {
            int(entry["id"]): entry.get("bot_id") for entry in entries
        }
        expected_entry_users = {
            int(entry["id"]): int(entry["user_id"]) for entry in entries
        }
        pairings_by_stage: dict[int, list[dict[str, Any]]] = {
            stage_idx: [] for stage_idx in range(current_stage_idx + 1)
        }
        for pairing in self.store.list_contest_pairings(contest_id):
            stage_idx = exact_nonnegative_int(pairing.get("stage_idx"))
            # Future/unknown-stage rows are lifecycle drift, not evidence that
            # the reached stage graph is complete.
            if (
                stage_idx is None
                or stage_idx >= len(stages)
            ):
                return True
            if stage_idx > current_stage_idx:
                if _predecessor_prefix_only:
                    continue
                return True
            pairings_by_stage[stage_idx].append(pairing)
            stage_type = (
                stages[stage_idx].get("type")
                if 0 <= stage_idx < len(stages)
                else None
            )
            if is_authoritative_no_opponent_pairing(stage_type, pairing):
                if not contest_pairing_roster_binding_is_valid(
                    pairing,
                    expected_contest_id=contest_id,
                    expected_entry_bots=expected_entry_bots,
                    expected_entry_users=expected_entry_users,
                    require_current_entry_bots=bool(
                        stage_idx >= persisted_stage_idx
                        and contest.get("status") in (
                            CONTEST_PUBLISHED,
                            CONTEST_RUNNING,
                        )
                    ),
                    require_opponent=False,
                ):
                    return True
                continue
            match_id = pairing.get("match_id")
            if not match_id:
                return True
            match = self.store.get_match(match_id)
            if not match_scoring_result_is_valid(
                stages[stage_idx],
                match,
                game_spec=game_spec,
                pairing=pairing,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=bool(
                    stage_idx >= persisted_stage_idx
                    and contest.get("status") in (
                        CONTEST_PUBLISHED,
                        CONTEST_RUNNING,
                    )
                ),
            ):
                return True

        active_entry_count = len(active_entries)
        full_roster_entry_ids = set(expected_entry_bots)
        for stage_idx, stage_pairings in pairings_by_stage.items():
            if stage_pairings:
                stage = stages[stage_idx]
                if not stage_scoring_contract_is_valid(stage, game_id=game_id):
                    return True
                expected_stage_ids = (
                    full_roster_entry_ids
                    if stage_idx < persisted_stage_idx
                    else expected_current_stage_ids
                )
                require_current_entry_bots = bool(
                    stage_idx >= persisted_stage_idx
                    and contest.get("status")
                    in (CONTEST_PUBLISHED, CONTEST_RUNNING)
                )
                stage_type = stage.get("type")
                # Settled rows are not proof that the planned competition was
                # actually played.  Terminal publication (including manual
                # finish and finished-unready recovery) additionally requires
                # the exact format topology: every RR edge, every configured
                # Swiss round, or a KO chain ending in one champion.
                if stage_type in {"round_robin", "double_round_robin"}:
                    if not complete_round_robin_pairing_topology(
                        stage, expected_stage_ids, stage_pairings
                    ):
                        return True
                elif stage_type in {
                    "group_round_robin",
                    "group_double_round_robin",
                }:
                    _groups, group_authority_valid = traditional_group_authority(
                        stage,
                        expected_stage_ids,
                        {int(entry["id"]): entry for entry in entries},
                        stage_pairings,
                        require_complete_topology=True,
                    )
                    if not group_authority_valid:
                        return True
                elif stage_type == "swiss":
                    if not complete_swiss_pairing_topology(
                        stage,
                        expected_stage_ids,
                        stage_pairings,
                        expected_rounds=effective_swiss_rounds(
                            stage, len(expected_stage_ids)
                        ),
                    ):
                        return True
                elif stage_type == "single_elimination":
                    if not complete_single_elimination_pairing_topology(
                        stage,
                        expected_stage_ids,
                        stage_pairings,
                        get_match=self.store.get_match,
                        game_id=game_id,
                        contest_id=contest_id,
                        expected_entry_bots=expected_entry_bots,
                        expected_entry_users=expected_entry_users,
                        require_current_entry_bots=require_current_entry_bots,
                        require_champion=True,
                    ):
                        return True
                else:
                    return True
                if "games_per_pair" in stage:
                    real_pairings = [
                        pairing
                        for pairing in stage_pairings
                        if not is_authoritative_no_opponent_pairing(
                            stage.get("type"), pairing
                        )
                    ]
                    if not series_rows_settled(
                        stage,
                        real_pairings,
                        self.store.get_match,
                        game_spec=game_spec,
                        all_pairings=stage_pairings,
                        expected_entry_ids=expected_stage_ids,
                        expected_swiss_rounds=(
                            effective_swiss_rounds(
                                stage,
                                len(expected_stage_ids),
                            )
                            if stage.get("type") == "swiss"
                            else None
                        ),
                        expected_contest_id=contest_id,
                        expected_entry_bots=expected_entry_bots,
                        expected_entry_users=expected_entry_users,
                        require_current_entry_bots=require_current_entry_bots,
                    ):
                        return True
                if (
                    stage_idx < persisted_stage_idx
                    and self._stage_ranking_from_recovery_snapshot(
                        contest_id, stage_idx
                    )
                    is None
                ):
                    return True
                continue
            initial_zero_or_one = (
                stage_idx == current_stage_idx == 0 and len(entries) <= 1
            )
            generated_next_stage_champion = (
                not _predecessor_prefix_only
                and explicit_next_stage
                and stage_idx == current_stage_idx == persisted_stage_idx + 1
                and active_entry_count <= 1
            )
            if initial_zero_or_one or generated_next_stage_champion:
                continue
            return True
        return False

    def estimate(
        self,
        contest_id: int,
        *,
        contest: dict[str, Any] | None = None,
        entries: list[dict[str, Any]] | None = None,
        pairings: list[dict[str, Any]] | None = None,
    ) -> dict:
        """Estimate from one optional frozen read snapshot.

        The public detail endpoint injects contest/roster/pairings from a single
        Store transaction. Other callers retain the historical Store-backed
        behavior without duplicating the estimation formula.
        """
        c = contest if contest is not None else self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        entry_rows = (
            entries
            if entries is not None
            else self.store.list_contest_entries(contest_id)
        )
        if active_contest_entries(entry_rows) is None:
            raise ValueError("参赛者淘汰状态损坏，无法估算赛事")
        pairing_rows = (
            pairings
            if pairings is not None
            else self.store.list_contest_pairings(contest_id)
        )
        n = len(entry_rows)
        stages = _parse_stages(c)
        # 旧 draft/open 内建模板可以尚未持久化当前系列默认值。发布/启动会在
        # 冻结边界注入这些值，因此预估也必须基于同一份内存投影，避免 API
        # 先低报 K/轮数、实际发布后突然膨胀。这里只读计算，不静默改写快照。
        if (
            c.get("status") in (CONTEST_DRAFT, CONTEST_OPEN)
            and not pairing_rows
        ):
            stages = self._configured_unstarted_series_stages(c, stages)
            if c.get("template_id") == GOMOKU_PROTECTED_GROUP_TEMPLATE:
                if not 22 <= n <= 26:
                    raise ValueError("保护种子五子棋正式赛仅允许 22–26 人估算")
                dynamic_groups = 4 if n <= 24 else 5
                stages = [dict(stage) for stage in stages]
                if (
                    len(stages) != 2
                    or stages[0].get("type") != "group_double_round_robin"
                    or stages[1].get("type") != "double_round_robin"
                ):
                    raise ValueError("保护种子模板阶段拓扑无效")
                stages[0]["group_count"] = dynamic_groups
                stages[0]["advance_per_group"] = 2
                stages[1]["ranking_mode"] = "replace_top"
                stages[1]["ranking_scope"] = dynamic_groups * 2
        if contest_current_stage_index(c, stage_count=len(stages)) is None:
            raise ValueError("赛事当前阶段游标损坏，无法估算赛事")
        gid = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        spec = game_registry.get(gid)
        if not reserved_group_markers_match_template(
            c.get("template_id"), stages, game_id=gid
        ):
            raise ValueError("赛事随机分组 marker 与代码模板不匹配，无法估算")
        # estimate 按晋级契约传播各 stage 人数。与 _advance_participants 一致，
        # 分组 Top-N 优先于全局 advance_count；两者都不能放大当前人数。
        total = 0
        execution_legs = 0
        cur_n = n
        conc = max(
            1,
            int(getattr(self.orch, "max_concurrent", MAX_CONCURRENT_MATCHES)),
        )
        frozen_time_control_id = self._resolve_contest_time_control_id(
            gid,
            c.get("time_control_id"),
            template_id=c.get("template_id"),
            persisted=True,
        )
        sec_per = _estimate_sec_per_match(
            gid, {"time_control_id": frozen_time_control_id}
        )
        stage_estimates: list[dict[str, Any]] = []
        for st in stages:
            if not stage_scoring_contract_is_valid(st, game_id=gid):
                raise ValueError("阶段计分版本配置无效")
            stage_matches = estimate_match_count(st, cur_n)
            total += stage_matches
            leg_count = 1
            duplicate = stage_duplicate_mode(st)
            if duplicate:
                if spec.build_match_plan is None:
                    raise ValueError(f"游戏 {gid} 不支持 duplicate 赛制")
                match_plan = spec.build_match_plan(0, {"duplicate": True})
                if not match_plan:
                    raise ValueError(f"游戏 {gid} 的 duplicate 对局计划为空")
                leg_count = len(match_plan)
            stage_execution_legs = stage_matches * leg_count
            execution_legs += stage_execution_legs
            stage_type = str(st.get("type") or "round_robin")
            games_per_pair = int(
                st.get("games_per_pair")
                or (2 if "double_round_robin" in stage_type else 1)
            )
            conceptual_pairings = (
                stage_matches // max(1, games_per_pair)
            )
            stage_estimates.append(
                {
                    "stage_key": st.get("key") or f"stage{len(stage_estimates) + 1}",
                    "participant_count": cur_n,
                    "conceptual_pairings": conceptual_pairings,
                    "effective_rounds": (
                        effective_swiss_rounds(st, cur_n)
                        if stage_type == "swiss"
                        else None
                    ),
                    "games_per_pair": games_per_pair,
                    "estimated_matches": stage_matches,
                    "estimated_execution_legs": stage_execution_legs,
                    "eta_seconds": int((stage_execution_legs / conc) * sec_per),
                    "unbounded_tiebreak": bool(
                        stage_type == "single_elimination"
                        and st.get("tiebreak")
                        == ELIMINATION_TIEBREAK_PAIRED_SWAP
                    ),
                }
            )
            advance_per_group = st.get("advance_per_group")
            if advance_per_group and int(advance_per_group) > 0:
                group_count = effective_group_count(
                    cur_n,
                    int(st.get("group_count") or 4),
                )
                cur_n = min(
                    cur_n,
                    group_count * int(advance_per_group),
                )
                continue
            ac = st.get("advance_count")
            if ac and int(ac) > 0:
                cur_n = min(cur_n, int(ac))
        # Production uses MatchOrchestrator.max_concurrent.  Lightweight
        # read-only estimators/test doubles need no execution interface, so
        # fall back to the same immutable code policy instead of a DB setting.
        eta_sec = (execution_legs / conc) * sec_per if conc else 0
        return {
            "entries": n,
            "estimated_matches": total,
            "estimated_scoring_games": execution_legs,
            "max_concurrent": conc,
            "eta_seconds": int(eta_sec),
            "stages": stage_estimates,
            "unbounded_tiebreak": any(
                bool(stage.get("unbounded_tiebreak"))
                for stage in stage_estimates
            ),
        }
