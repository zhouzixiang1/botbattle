"""Pure helpers for aggregate per-opponent contest series scoring."""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Callable

from bzplat.backend.contests.templates import points_for_result
from bzplat.backend.contests.validation import SERIES_SCORING_AGGREGATE


def is_aggregate_series_stage(stage: dict[str, Any]) -> bool:
    return stage.get("series_scoring") == SERIES_SCORING_AGGREGATE


def conceptual_series_key(
    stage: dict[str, Any], pairing: dict[str, Any]
) -> tuple[int, int, int] | None:
    """Return a seat-independent conceptual encounter identity.

    Swiss can pair the same entrants again only after exhausting alternatives,
    so its round remains part of the identity.  Round-robin stages have exactly
    one conceptual encounter per unordered entry pair even though their K
    physical Matches may be staggered across schedule rounds.
    """
    first = pairing.get("entry_a_id")
    second = pairing.get("entry_b_id")
    if (
        isinstance(first, bool)
        or not isinstance(first, int)
        or isinstance(second, bool)
        or not isinstance(second, int)
        or first == second
    ):
        return None
    round_key = int(pairing.get("round_num") or 1) if stage.get("type") == "swiss" else 0
    low, high = sorted((first, second))
    return round_key, low, high


def group_conceptual_series(
    stage: dict[str, Any], pairings: list[dict[str, Any]]
) -> dict[tuple[int, int, int], list[dict[str, Any]]]:
    grouped: dict[tuple[int, int, int], list[dict[str, Any]]] = defaultdict(list)
    for pairing in pairings:
        key = conceptual_series_key(stage, pairing)
        if key is not None:
            grouped[key].append(pairing)
    return dict(grouped)


def summarize_conceptual_series(
    stage: dict[str, Any],
    pairings: list[dict[str, Any]],
    get_match: Callable[[str], dict[str, Any] | None],
) -> dict[str, Any]:
    """Aggregate physical Match outcomes into one conceptual encounter.

    The physical score is 1/0.5/0.  Only a complete, coordinate-consistent
    series settles one 3/1/0 standings result.  Raw deltas are accumulated from
    every completed physical Match even while the encounter is still running.
    """
    if not is_aggregate_series_stage(stage) or not pairings:
        raise ValueError("阶段不是系列聚合计分")
    key = conceptual_series_key(stage, pairings[0])
    if key is None or any(conceptual_series_key(stage, row) != key for row in pairings):
        raise ValueError("系列对阵参赛身份不一致")
    first_entry, second_entry = key[1], key[2]
    declared_sizes = {
        int(row.get("series_size") or 1) for row in pairings
        if not isinstance(row.get("series_size"), bool)
    }
    series_size = next(iter(declared_sizes)) if len(declared_sizes) == 1 else 0
    indexes = [
        int(row.get("series_index") or 1) for row in pairings
        if not isinstance(row.get("series_index"), bool)
    ]
    expected_size = stage.get("games_per_pair")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int):
        expected_size = 0
    coordinate_complete = bool(
        expected_size >= 1
        and series_size == expected_size
        and len(pairings) == expected_size
        and sorted(indexes) == list(range(1, expected_size + 1))
    )
    game_points = {first_entry: 0.0, second_entry: 0.0}
    deltas = {first_entry: 0, second_entry: 0}
    completed_matches = 0
    for pairing in pairings:
        match_id = pairing.get("match_id")
        match = get_match(str(match_id)) if match_id else None
        if not match or match.get("status") != "completed":
            continue
        entry_a = int(pairing["entry_a_id"])
        entry_b = int(pairing["entry_b_id"])
        winner = match.get("winner")
        if winner == 0:
            game_points[entry_a] += 1.0
        elif winner == 1:
            game_points[entry_b] += 1.0
        else:
            game_points[entry_a] += 0.5
            game_points[entry_b] += 0.5
        result = match.get("result") or {}
        raw_deltas = result.get("deltas") if isinstance(result, dict) else None
        if (
            isinstance(raw_deltas, list)
            and len(raw_deltas) == 2
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in raw_deltas)
        ):
            deltas[entry_a] += int(raw_deltas[0])
            deltas[entry_b] += int(raw_deltas[1])
        completed_matches += 1

    settled = coordinate_complete and completed_matches == series_size
    winner_entry: int | None = None
    standings_points = {first_entry: None, second_entry: None}
    if settled:
        if game_points[first_entry] > game_points[second_entry]:
            winner_entry = first_entry
        elif game_points[second_entry] > game_points[first_entry]:
            winner_entry = second_entry
        scoring = stage.get("scoring")
        if not isinstance(scoring, str):
            raise ValueError("系列聚合阶段缺少 scoring")
        conceptual_winner = (
            None if winner_entry is None else 0 if winner_entry == first_entry else 1
        )
        standings_points = {
            first_entry: points_for_result(scoring, conceptual_winner, 0),
            second_entry: points_for_result(scoring, conceptual_winner, 1),
        }
    return {
        "series_size": series_size,
        "completed_matches": completed_matches,
        "entries": (first_entry, second_entry),
        "game_points": game_points,
        "deltas": deltas,
        "settled": settled,
        "winner_entry": winner_entry,
        "standings_points": standings_points,
    }


def aggregate_series_rows_settled(
    stage: dict[str, Any],
    real_pairings: list[dict[str, Any]],
    get_match: Callable[[str], dict[str, Any] | None],
) -> bool:
    """Fail-closed integrity gate for every real series row in one stage."""
    if not is_aggregate_series_stage(stage):
        return True
    grouped = group_conceptual_series(stage, real_pairings)
    if sum(len(rows) for rows in grouped.values()) != len(real_pairings):
        return False
    for rows in grouped.values():
        try:
            if not summarize_conceptual_series(stage, rows, get_match)["settled"]:
                return False
        except (TypeError, ValueError):
            return False
    return True
