"""Pure helpers for aggregate per-opponent contest series scoring."""
from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Callable, Mapping

from bzplat.backend.games.base import GameSpec
from bzplat.backend.matches.public_outcome import (
    normalized_delta_value,
    planned_match_games,
    scoring_games_for_match,
)
from bzplat.backend.contests.templates import points_for_result
from bzplat.backend.contests.validation import SERIES_SCORING_AGGREGATE
from bzplat.backend.contests.validation import SERIES_SCORING_INDEPENDENT
from bzplat.backend.contests.validation import stage_duplicate_mode
from bzplat.backend.contests.validation import stage_scoring_contract_is_valid
from bzplat.backend.store.schema import TYPE_CONTEST
from bzplat.backend.store.validation import is_authoritative_no_opponent_pairing


def contest_pairing_roster_binding_is_valid(
    pairing: dict[str, Any] | None,
    *,
    expected_contest_id: int,
    expected_entry_bots: Mapping[int, int | None] | None = None,
    expected_entry_users: Mapping[int, int] | None = None,
    require_current_entry_bots: bool = False,
    require_opponent: bool = True,
) -> bool:
    """Validate one real/bye pairing against its frozen roster identity.

    ``contest_entries.bot_id`` is deliberately *not* authoritative on the read
    path: a published entrant may later swap or delete its current Bot while
    already materialized pairings and Matches keep their historical Bot/version
    identity.  The current-Bot equality is enforced transactionally when a new
    pairing is bound or dispatched.  Read models instead prove that the entry
    belongs to this contest and that the frozen pairing Bot is owned by that
    entry's user.

    ``require_current_entry_bots`` remains as a compatibility keyword for
    callers being migrated from the former read-side policy; it intentionally
    has no effect here.
    """
    if (
        isinstance(expected_contest_id, bool)
        or not isinstance(expected_contest_id, int)
        or not isinstance(pairing, dict)
        or pairing.get("contest_id") != expected_contest_id
    ):
        return False
    for suffix in (("a", "b") if require_opponent else ("a",)):
        field = f"bot_{suffix}_id"
        entry_field = f"entry_{suffix}_id"
        entry_id = pairing.get(entry_field)
        pairing_bot = pairing.get(field)
        explicit_marker = pairing.get("_explicit_series_marker")
        if explicit_marker is not None and explicit_marker not in (False, True, 0, 1):
            return False
        raw_entry_field = f"_raw_entry_{suffix}_id"
        if explicit_marker in (True, 1) and raw_entry_field in pairing:
            raw_entry_id = pairing.get(raw_entry_field)
            if (
                isinstance(raw_entry_id, bool)
                or not isinstance(raw_entry_id, int)
                or raw_entry_id != entry_id
            ):
                return False
        if (
            isinstance(entry_id, bool)
            or not isinstance(entry_id, int)
            or isinstance(pairing_bot, bool)
            or (pairing_bot is not None and not isinstance(pairing_bot, int))
        ):
            return False
        if expected_entry_bots is not None and entry_id not in expected_entry_bots:
            return False
        if expected_entry_users is not None:
            if entry_id not in expected_entry_users:
                return False
        entry_user = pairing.get(f"_entry_{suffix}_user_id")
        if (
            isinstance(entry_user, bool)
            or not isinstance(entry_user, int)
            or (
                expected_entry_users is not None
                and entry_user != expected_entry_users[entry_id]
            )
        ):
            return False
        if pairing_bot is not None:
            owner_id = pairing.get(f"_pairing_bot_{suffix}_owner_id")
            if (
                isinstance(owner_id, bool)
                or not isinstance(owner_id, int)
                or owner_id != entry_user
            ):
                return False
    if not require_opponent and (
        pairing.get("entry_b_id") is not None or pairing.get("bot_b_id") is not None
    ):
        return False
    return True


def contest_match_binding_is_valid(
    pairing: dict[str, Any] | None,
    match: dict[str, Any] | None,
    *,
    expected_contest_id: int | None = None,
    expected_game_id: str | None = None,
    expected_entry_bots: Mapping[int, int | None] | None = None,
    expected_entry_users: Mapping[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> bool:
    """Validate the frozen pairing-to-Match identity without coercion.

    Direct/legacy pure-helper callers may omit the expected contest identity.
    Once a caller supplies it, every durable identity field is mandatory and
    exact: contest, game, Match type, Match id, both Bots, and their seats.  A
    same-contest Match with swapped or unrelated Bots is no more authoritative
    than a Match linked from another contest.
    """
    if expected_contest_id is None and expected_game_id is None:
        return True
    if (
        isinstance(expected_contest_id, bool)
        or not isinstance(expected_contest_id, int)
        or not isinstance(expected_game_id, str)
        or not expected_game_id
        or not isinstance(match, dict)
    ):
        return False
    if (
        match.get("contest_id") != expected_contest_id
        or match.get("game_id") != expected_game_id
        or match.get("match_type") != TYPE_CONTEST
    ):
        return False
    if not contest_pairing_roster_binding_is_valid(
        pairing,
        expected_contest_id=expected_contest_id,
        expected_entry_bots=expected_entry_bots,
        expected_entry_users=expected_entry_users,
        require_current_entry_bots=require_current_entry_bots,
    ):
        return False
    assert pairing is not None
    pairing_match_id = pairing.get("match_id")
    match_id = match.get("id")
    if (
        not isinstance(pairing_match_id, str)
        or not pairing_match_id
        or match_id != pairing_match_id
    ):
        return False
    for suffix in ("a", "b"):
        field = f"bot_{suffix}_id"
        pairing_bot = pairing.get(field)
        match_bot = match.get(field)
        if (
            isinstance(pairing_bot, bool)
            or (pairing_bot is not None and not isinstance(pairing_bot, int))
            or isinstance(match_bot, bool)
            or (match_bot is not None and not isinstance(match_bot, int))
            or match_bot != pairing_bot
        ):
            return False

    raw_config = match.get("match_config")
    if raw_config is None:
        raw_config = {}
    if isinstance(raw_config, str):
        try:
            raw_config = json.loads(raw_config)
        except (TypeError, ValueError):
            return False
    if not isinstance(raw_config, dict):
        return False
    for suffix in ("a", "b"):
        pairing_version = pairing.get(f"bot_{suffix}_version_id")
        match_version = raw_config.get(f"_bot_{suffix}_version_id")
        if pairing_version is None and match_version is None:
            continue
        if (
            isinstance(pairing_version, bool)
            or not isinstance(pairing_version, int)
            or isinstance(match_version, bool)
            or not isinstance(match_version, int)
            or pairing_version != match_version
        ):
            return False
    return True


def is_aggregate_series_stage(stage: dict[str, Any]) -> bool:
    return stage.get("series_scoring") == SERIES_SCORING_AGGREGATE


def is_independent_series_stage(stage: dict[str, Any]) -> bool:
    """Whether every physical/scoring game contributes points immediately.

    Missing ``series_scoring`` with ``games_per_pair`` is the pre-v1 historical
    independent behavior.  New creation paths always freeze the explicit v1
    identifier; keeping this fallback prevents old round-robin contests from
    being reinterpreted as aggregate series.
    """
    mode = stage.get("series_scoring")
    return mode == SERIES_SCORING_INDEPENDENT or (
        mode is None and "games_per_pair" in stage
    )


def match_scoring_result_is_valid(
    stage: dict[str, Any],
    match: dict[str, Any] | None,
    *,
    game_spec: GameSpec | None,
    pairing: dict[str, Any] | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: Mapping[int, int | None] | None = None,
    expected_entry_users: Mapping[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> bool:
    """Whether one real contest Match has authoritative scoring records.

    Every advancement path uses the same result parser as standings and public
    outcome.  The explicit v1 marker additionally requires an exact frozen
    Match duplicate flag; pre-marker history may omit that flag but can never
    bypass winner/delta, technical-terminal, leg-count or fixed-round checks.
    """
    if game_spec is None or not stage_scoring_contract_is_valid(
        stage, game_id=game_spec.game_id
    ):
        return False
    if not contest_match_binding_is_valid(
        pairing,
        match,
        expected_contest_id=expected_contest_id,
        expected_game_id=(
            game_spec.game_id if expected_contest_id is not None else None
        ),
        expected_entry_bots=expected_entry_bots,
        expected_entry_users=expected_entry_users,
        require_current_entry_bots=require_current_entry_bots,
    ):
        return False
    duplicate = stage_duplicate_mode(stage)
    if duplicate is None:
        return False
    try:
        games = scoring_games_for_match(
            match,
            duplicate=duplicate,
            planned_games=planned_match_games(game_spec, duplicate=duplicate),
            fixed_rounds_per_match=game_spec.fixed_rounds_per_match,
            require_frozen_duplicate=(
                stage.get("series_scoring") == SERIES_SCORING_INDEPENDENT
            ),
            normalize_delta=game_spec.normalize_delta,
        )
    except (KeyError, TypeError, ValueError):
        return False
    return bool(games)


def swiss_bye_record_weights(
    stage: dict[str, Any], *, scoring_games_per_match: int = 1
) -> tuple[float, ...]:
    """Return virtual opponent weights for one independent-scoring Swiss bye.

    K=1 is one win.  Even K uses one win plus one draw for every two planned
    games, making the award 2K under poker 3/1/0 without adding fake W/D/L.
    """
    games = stage.get("games_per_pair", 1)
    if isinstance(games, bool) or not isinstance(games, int):
        raise ValueError("Swiss games_per_pair 须为整数")
    if (
        isinstance(scoring_games_per_match, bool)
        or not isinstance(scoring_games_per_match, int)
        or scoring_games_per_match < 1
    ):
        raise ValueError("每个 Match 的计分场数须为正整数")
    games *= scoring_games_per_match
    if games == 1:
        return (1.0,)
    if games < 2 or games % 2:
        raise ValueError("Swiss games_per_pair 仅允许 1 或偶数")
    return tuple(weight for _ in range(games // 2) for weight in (1.0, 0.5))


def swiss_bye_points(
    stage: dict[str, Any], *, scoring: str | None = None,
    scoring_games_per_match: int = 1,
) -> float:
    """Standings award for an authoritative no-opponent Swiss row.

    The caller resolves an omitted legacy stage value from the contest's
    registered ``GameSpec``.  Keeping that fallback outside this game-agnostic
    helper avoids silently hard-coding Holdem's 3/1/0 scoring for old Gomoku or
    Pencil histories.
    """
    scoring = stage.get("scoring", scoring)
    if not isinstance(scoring, str):
        raise ValueError("Swiss 阶段缺少 scoring")
    if not is_independent_series_stage(stage):
        return points_for_result(scoring, 0, 0)
    win = points_for_result(scoring, 0, 0)
    draw = points_for_result(scoring, None, 0)
    return sum(
        win if weight == 1.0 else draw
        for weight in swiss_bye_record_weights(
            stage, scoring_games_per_match=scoring_games_per_match
        )
    )


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
    if stage.get("type") == "swiss":
        raw_round = pairing.get("round_num")
        if stage.get("series_scoring") in {
            SERIES_SCORING_AGGREGATE,
            SERIES_SCORING_INDEPENDENT,
        }:
            if (
                isinstance(raw_round, bool)
                or not isinstance(raw_round, int)
                or raw_round < 1
            ):
                return None
            round_key = raw_round
        else:
            try:
                round_key = int(raw_round or 1)
            except (TypeError, ValueError):
                return None
            if round_key < 1:
                return None
    else:
        round_key = 0
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
    *,
    game_spec: GameSpec | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: Mapping[int, int | None] | None = None,
    expected_entry_users: Mapping[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> dict[str, Any]:
    """Aggregate physical Match outcomes into one conceptual encounter.

    The physical score is 1/0.5/0.  Only a complete, coordinate-consistent
    series whose every completed Match passes the shared result parser settles
    one 3/1/0 standings result.  Raw deltas are accumulated only from those
    authoritative Match records, even while the encounter is still running.
    """
    if not is_aggregate_series_stage(stage) or not pairings:
        raise ValueError("阶段不是系列聚合计分")
    key = conceptual_series_key(stage, pairings[0])
    if key is None or any(conceptual_series_key(stage, row) != key for row in pairings):
        raise ValueError("系列对阵参赛身份不一致")
    first_entry, second_entry = key[1], key[2]
    coordinate_types_valid = all(
        field in row
        and not isinstance(row.get(field), bool)
        and isinstance(row.get(field), int)
        for row in pairings
        for field in ("series_size", "series_index")
    )
    declared_sizes = (
        {row["series_size"] for row in pairings}
        if coordinate_types_valid
        else set()
    )
    series_size = next(iter(declared_sizes)) if len(declared_sizes) == 1 else 0
    indexes = (
        [row["series_index"] for row in pairings]
        if coordinate_types_valid
        else []
    )
    expected_size = stage.get("games_per_pair")
    if isinstance(expected_size, bool) or not isinstance(expected_size, int):
        expected_size = 0
    coordinate_complete = bool(
        coordinate_types_valid
        and expected_size >= 1
        and series_size == expected_size
        and len(pairings) == expected_size
        and sorted(indexes) == list(range(1, expected_size + 1))
    )
    game_points = {first_entry: 0.0, second_entry: 0.0}
    deltas = {first_entry: 0, second_entry: 0}
    completed_matches = 0
    duplicate = stage_duplicate_mode(stage)
    try:
        planned_games = (
            planned_match_games(game_spec, duplicate=duplicate)
            if game_spec is not None and duplicate is not None
            else 2
            if duplicate is True
            else 1
            if duplicate is False
            else 0
        )
    except (TypeError, ValueError):
        planned_games = 0
    for pairing in pairings:
        match_id = pairing.get("match_id")
        match = get_match(str(match_id)) if match_id else None
        if not match or match.get("status") != "completed":
            continue
        if not contest_match_binding_is_valid(
            pairing,
            match,
            expected_contest_id=expected_contest_id,
            expected_game_id=(
                game_spec.game_id
                if game_spec is not None and expected_contest_id is not None
                else None
            ),
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        ):
            continue
        if planned_games < 1 or duplicate is None:
            continue
        games = scoring_games_for_match(
            match,
            duplicate=duplicate,
            planned_games=planned_games,
            fixed_rounds_per_match=(
                game_spec.fixed_rounds_per_match
                if game_spec is not None
                else None
            ),
            normalize_delta=(
                game_spec.normalize_delta if game_spec is not None else None
            ),
        )
        if not games:
            continue
        entry_a = int(pairing["entry_a_id"])
        entry_b = int(pairing["entry_b_id"])
        # Validation is shared with public outcome and independent standings,
        # but the legacy aggregate rule itself remains frozen: one physical
        # Match contributes from its top-level winner.  In particular, an old
        # duplicate Match whose two valid legs favour one seat still has
        # top-level ``winner=None`` and remains a conceptual draw here.
        winner = match.get("winner")
        if winner == 0:
            game_points[entry_a] += 1.0
        elif winner == 1:
            game_points[entry_b] += 1.0
        else:
            game_points[entry_a] += 0.5
            game_points[entry_b] += 0.5
        deltas[entry_a] += sum(int(game.deltas[0]) for game in games if game.deltas)
        deltas[entry_b] += sum(int(game.deltas[1]) for game in games if game.deltas)
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
    *,
    game_spec: GameSpec | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: Mapping[int, int | None] | None = None,
    expected_entry_users: Mapping[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> bool:
    """Fail-closed integrity gate for every real series row in one stage."""
    if not is_aggregate_series_stage(stage):
        return True
    grouped = group_conceptual_series(stage, real_pairings)
    if sum(len(rows) for rows in grouped.values()) != len(real_pairings):
        return False
    cumulative_deltas: dict[int, int] = defaultdict(int)
    for rows in grouped.values():
        try:
            summary = summarize_conceptual_series(
                stage,
                rows,
                get_match,
                game_spec=game_spec,
                expected_contest_id=expected_contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )
            if not summary["settled"]:
                return False
            for entry_id, delta in summary["deltas"].items():
                cumulative_deltas[int(entry_id)] += int(delta)
        except (TypeError, ValueError):
            return False
    if game_spec is not None and any(
        normalized_delta_value(game_spec.normalize_delta, delta) is None
        for delta in cumulative_deltas.values()
    ):
        return False
    return True


def independent_series_rows_settled(
    stage: dict[str, Any],
    real_pairings: list[dict[str, Any]],
    get_match: Callable[[str], dict[str, Any] | None],
    *,
    game_spec: GameSpec | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: Mapping[int, int | None] | None = None,
    expected_entry_users: Mapping[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> bool:
    """Require complete coordinates and parseable scoring records before advance."""
    if not stage_scoring_contract_is_valid(
        stage, game_id=game_spec.game_id if game_spec is not None else None
    ):
        return False
    if not is_independent_series_stage(stage):
        return True
    expected_size = stage.get("games_per_pair")
    if (
        isinstance(expected_size, bool)
        or not isinstance(expected_size, int)
        or expected_size < 1
    ):
        return False
    grouped = group_conceptual_series(stage, real_pairings)
    if sum(len(rows) for rows in grouped.values()) != len(real_pairings):
        return False
    strict_v1 = stage.get("series_scoring") == SERIES_SCORING_INDEPENDENT
    cumulative_deltas: dict[int, int] = defaultdict(int)
    for rows in grouped.values():
        if strict_v1 and any(
            isinstance(row.get(field), bool)
            or not isinstance(row.get(field), int)
            for row in rows
            for field in ("series_size", "series_index")
        ):
            return False
        if strict_v1:
            declared_sizes = {int(row["series_size"]) for row in rows}
            indexes = [int(row["series_index"]) for row in rows]
        else:
            # Markerless rows predate persisted series coordinates.  A truly
            # absent key keeps the historical one-row default, but an imported
            # or damaged explicit value must never be coerced (``True``, 1.5,
            # and numeric text would otherwise settle and advance a stage).
            sizes: list[int] = []
            indexes = []
            for row in rows:
                raw_size = row["series_size"] if "series_size" in row else 1
                raw_index = row["series_index"] if "series_index" in row else 1
                if (
                    isinstance(raw_size, bool)
                    or not isinstance(raw_size, int)
                    or raw_size < 1
                    or isinstance(raw_index, bool)
                    or not isinstance(raw_index, int)
                    or raw_index < 1
                ):
                    return False
                sizes.append(raw_size)
                indexes.append(raw_index)
            declared_sizes = set(sizes)
        if (
            declared_sizes != {expected_size}
            or len(rows) != expected_size
            or sorted(indexes) != list(range(1, expected_size + 1))
        ):
            return False
        for row in rows:
            match_id = row.get("match_id")
            match = get_match(str(match_id)) if match_id else None
            if not match_scoring_result_is_valid(
                stage,
                match,
                game_spec=game_spec,
                pairing=row,
                expected_contest_id=expected_contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            ):
                return False
            if game_spec is not None:
                duplicate = stage_duplicate_mode(stage)
                if duplicate is None:
                    return False
                games = scoring_games_for_match(
                    match,
                    duplicate=duplicate,
                    planned_games=planned_match_games(
                        game_spec, duplicate=duplicate
                    ),
                    fixed_rounds_per_match=game_spec.fixed_rounds_per_match,
                    require_frozen_duplicate=strict_v1,
                    normalize_delta=game_spec.normalize_delta,
                )
                if not games:
                    return False
                entry_a = int(row["entry_a_id"])
                entry_b = int(row["entry_b_id"])
                cumulative_deltas[entry_a] += sum(
                    int(game.deltas[0]) for game in games
                )
                cumulative_deltas[entry_b] += sum(
                    int(game.deltas[1]) for game in games
                )
    if game_spec is not None and any(
        normalized_delta_value(game_spec.normalize_delta, delta) is None
        for delta in cumulative_deltas.values()
    ):
        return False
    return True


def independent_series_topology_complete(
    stage: dict[str, Any],
    pairings: list[dict[str, Any]],
    expected_entry_ids: list[int] | tuple[int, ...] | set[int],
    *,
    expected_swiss_rounds: int | None = None,
    game_id: str | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: Mapping[int, int | None] | None = None,
    expected_entry_users: Mapping[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> bool:
    """Validate the complete conceptual graph for versioned series stages.

    Coordinate validation alone cannot detect an opponent group whose every
    physical row disappeared.  This second half of the lifecycle gate derives
    the expected graph from the frozen participant cohort: all unordered pairs
    for round-robin, or exactly one conceptual encounter/bye per participant in
    every published Swiss round.  Aggregate history retains its one-series
    scoring semantics, but it must not advance from an incomplete topology.
    """
    if not stage_scoring_contract_is_valid(stage, game_id=game_id):
        return False
    if stage.get("series_scoring") not in {
        SERIES_SCORING_AGGREGATE,
        SERIES_SCORING_INDEPENDENT,
    }:
        return True
    raw_expected = list(expected_entry_ids)
    if any(
        isinstance(entry_id, bool) or not isinstance(entry_id, int)
        for entry_id in raw_expected
    ):
        return False
    expected = set(raw_expected)
    if len(expected) != len(raw_expected):
        return False
    stage_type = stage.get("type")
    if expected_contest_id is not None and any(
        not contest_pairing_roster_binding_is_valid(
            pairing,
            expected_contest_id=expected_contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
            require_opponent=not is_authoritative_no_opponent_pairing(
                stage_type, pairing
            ),
        )
        for pairing in pairings
    ):
        return False
    if stage_type in {"round_robin", "double_round_robin"}:
        if any(
            is_authoritative_no_opponent_pairing(stage_type, pairing)
            for pairing in pairings
        ):
            return False
        expected_groups = {
            (0, first, second)
            for first in expected
            for second in expected
            if first < second
        }
        grouped = group_conceptual_series(stage, pairings)
        return (
            sum(len(rows) for rows in grouped.values()) == len(pairings)
            and set(grouped) == expected_groups
        )
    if stage_type != "swiss":
        return False
    if expected_swiss_rounds is not None and (
        isinstance(expected_swiss_rounds, bool)
        or not isinstance(expected_swiss_rounds, int)
        or expected_swiss_rounds < 0
    ):
        return False
    if not pairings:
        return len(expected) <= 1 and expected_swiss_rounds in (None, 0)

    by_round: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for pairing in pairings:
        raw_round = pairing.get("round_num")
        if (
            isinstance(raw_round, bool)
            or not isinstance(raw_round, int)
            or raw_round < 1
        ):
            return False
        by_round[raw_round].append(pairing)
    published_rounds = sorted(by_round)
    round_limit = (
        expected_swiss_rounds
        if expected_swiss_rounds is not None
        else published_rounds[-1]
    )
    if published_rounds != list(range(1, round_limit + 1)):
        return False

    games_per_pair = stage.get("games_per_pair")
    if (
        isinstance(games_per_pair, bool)
        or not isinstance(games_per_pair, int)
        or games_per_pair < 1
    ):
        return False
    for round_rows in by_round.values():
        participants: set[int] = set()
        real_rows: list[dict[str, Any]] = []
        bye_count = 0
        for pairing in round_rows:
            if is_authoritative_no_opponent_pairing(stage_type, pairing):
                entry_id = pairing.get("entry_a_id")
                if (
                    isinstance(entry_id, bool)
                    or not isinstance(entry_id, int)
                    or entry_id not in expected
                    or entry_id in participants
                    or isinstance(pairing.get("series_index"), bool)
                    or not isinstance(pairing.get("series_index"), int)
                    or pairing.get("series_index") != 1
                    or isinstance(pairing.get("series_size"), bool)
                    or not isinstance(pairing.get("series_size"), int)
                    or pairing.get("series_size") != 1
                ):
                    return False
                participants.add(entry_id)
                bye_count += 1
                continue
            real_rows.append(pairing)
        grouped = group_conceptual_series(stage, real_rows)
        if sum(len(rows) for rows in grouped.values()) != len(real_rows):
            return False
        for (_round, first, second), rows in grouped.items():
            if (
                first not in expected
                or second not in expected
                or first in participants
                or second in participants
                or len(rows) != games_per_pair
            ):
                return False
            participants.update((first, second))
        if participants != expected or bye_count != len(expected) % 2:
            return False
    return True


def series_rows_settled(
    stage: dict[str, Any],
    real_pairings: list[dict[str, Any]],
    get_match: Callable[[str], dict[str, Any] | None],
    *,
    game_spec: GameSpec | None = None,
    all_pairings: list[dict[str, Any]] | None = None,
    expected_entry_ids: list[int] | tuple[int, ...] | set[int] | None = None,
    expected_swiss_rounds: int | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: Mapping[int, int | None] | None = None,
    expected_entry_users: Mapping[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> bool:
    """Dispatch the integrity gate without rewriting historical aggregate data."""
    if not stage_scoring_contract_is_valid(
        stage, game_id=game_spec.game_id if game_spec is not None else None
    ):
        return False
    if is_aggregate_series_stage(stage):
        rows_settled = aggregate_series_rows_settled(
            stage,
            real_pairings,
            get_match,
            game_spec=game_spec,
            expected_contest_id=expected_contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
        if not rows_settled:
            return False
        if expected_entry_ids is None:
            return True
        return independent_series_topology_complete(
            stage,
            all_pairings if all_pairings is not None else real_pairings,
            expected_entry_ids,
            expected_swiss_rounds=expected_swiss_rounds,
            game_id=game_spec.game_id if game_spec is not None else None,
            expected_contest_id=expected_contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
    if is_independent_series_stage(stage):
        rows_settled = independent_series_rows_settled(
            stage,
            real_pairings,
            get_match,
            game_spec=game_spec,
            expected_contest_id=expected_contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
        if not rows_settled:
            return False
        if expected_entry_ids is None:
            return True
        return independent_series_topology_complete(
            stage,
            all_pairings if all_pairings is not None else real_pairings,
            expected_entry_ids,
            expected_swiss_rounds=expected_swiss_rounds,
            game_id=game_spec.game_id if game_spec is not None else None,
            expected_contest_id=expected_contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
    return True


__all__ = [
    "aggregate_series_rows_settled",
    "conceptual_series_key",
    "contest_match_binding_is_valid",
    "contest_pairing_roster_binding_is_valid",
    "group_conceptual_series",
    "independent_series_rows_settled",
    "independent_series_topology_complete",
    "is_aggregate_series_stage",
    "is_independent_series_stage",
    "match_scoring_result_is_valid",
    "series_rows_settled",
    "swiss_bye_points",
    "swiss_bye_record_weights",
    "summarize_conceptual_series",
]
