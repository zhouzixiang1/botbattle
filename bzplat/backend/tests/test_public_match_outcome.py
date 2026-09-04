"""Replay-free public outcome and shared scoring-game parser contracts."""
from __future__ import annotations

import pytest

from bzplat.backend.games import registry
from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.ranking import compute_official_ranking
from bzplat.backend.matches.public_outcome import (
    build_public_outcome,
    is_duplicate_match,
    scoring_games_for_match,
)
from bzplat.backend.store.public_contract import (
    sanitize_public_contest_tiebreaks,
    sanitize_public_event,
    sanitize_public_event_prefix,
)


HOLDEM = registry.get("holdem")
GOMOKU = registry.get("gomoku")


def test_cross_group_tiebreak_projection_is_atomic_and_bounded():
    base = {
        "points": 6,
        "buchholz": 8,
        "buchholz_cut1": 5,
        "sonneborn_berger": 4,
        "head_to_head": 0.5,
        "normalized_delta": -2.0,
        "technical_losses": 0,
        "seed": 7,
    }
    group = {
        "group_rank": 1,
        "points_rate": 0.75,
        "opponent_strength": 0.625,
        "normalized_delta_rate": -0.25,
        "technical_loss_rate": 0.0,
        "draw_order": 9,
    }
    assert sanitize_public_contest_tiebreaks({**base, **group}) == {
        **base,
        **group,
    }
    assert sanitize_public_contest_tiebreaks(base) == base
    assert sanitize_public_contest_tiebreaks(
        {**base, **group, "private_seed": "never public"}
    ) == {**base, **group}
    for malformed in (
        {**base, **group, "draw_order": True},
        {**base, **group, "technical_loss_rate": 1.1},
        {**base, **group, "opponent_strength": float("inf")},
        {**base, **{key: value for key, value in group.items() if key != "draw_order"}},
    ):
        assert sanitize_public_contest_tiebreaks(malformed) is None


def test_match_start_time_control_binds_registry_and_historical_match_default():
    control = {
        "id": "gomoku_per_side_total_300s_v1",
        "mode": "per_side_total",
        "seconds": 300,
        "applies_to": "both_bots",
    }
    start = {
        "type": "match_start",
        "game_id": "gomoku",
        "size": 15,
        "time_control": control,
    }
    assert sanitize_public_event(start)["time_control"] == control
    malformed = {
        **start,
        "time_control": {**control, "mode": "per_decision", "seconds": 1},
    }
    assert "time_control" not in sanitize_public_event(malformed)

    historical = sanitize_public_event_prefix(
        [{"type": "match_start", "game_id": "gomoku", "size": 15}],
        expected_time_control=control,
    )
    assert historical[0]["time_control"] == control
    historical_without_game = sanitize_public_event_prefix(
        [{"type": "match_start", "size": 15}],
        expected_time_control=control,
        expected_game_id="gomoku",
    )
    assert historical_without_game[0]["time_control"] == control
    wrong_game = sanitize_public_event_prefix(
        [{"type": "match_start", "game_id": "pencil", "size": 15}],
        expected_time_control=control,
        expected_game_id="gomoku",
    )
    assert wrong_game[0]["time_control"] is None
    forged_expected = sanitize_public_event_prefix(
        [{"type": "match_start", "game_id": "gomoku", "size": 15}],
        expected_time_control={**control, "seconds": 301},
        expected_game_id="gomoku",
    )
    assert forged_expected[0]["time_control"] is None
    missing_authoritative = sanitize_public_event_prefix(
        [start],
        expected_time_control=None,
        expected_game_id="gomoku",
    )
    assert missing_authoritative[0]["time_control"] is None
    contradictory = sanitize_public_event_prefix(
        [
            {
                "type": "match_start",
                "game_id": "gomoku",
                "size": 15,
                "time_control": {
                    "id": "gomoku_per_side_total_900s_v1",
                    "mode": "per_side_total",
                    "seconds": 900,
                    "applies_to": "both_bots",
                },
            }
        ],
        expected_time_control=control,
    )
    assert contradictory[0]["time_control"] is None
    genuinely_unbound = sanitize_public_event_prefix(
        [{"type": "match_start", "game_id": "gomoku", "size": 15}]
    )
    assert "time_control" not in genuinely_unbound[0]


def _completed(
    *,
    winner=0,
    result=None,
    technical_loss=0,
) -> dict:
    return {
        "status": "completed",
        "winner": winner,
        "reason": "technical_loss" if technical_loss else "completed",
        "technical_loss": technical_loss,
        "result": result
        if result is not None
        else {"rounds_played": 70, "deltas": [100, -100]},
    }


def _duplicate_result(*legs: dict) -> dict:
    return {
        "rounds_played": sum(int(leg.get("rounds_played", 70)) for leg in legs),
        "deltas": [
            sum(int(leg["deltas"][0]) for leg in legs),
            sum(int(leg["deltas"][1]) for leg in legs),
        ],
        "legs": list(legs),
    }


def test_single_draw_is_distinct_from_unavailable_outcome():
    draw = _completed(
        winner=None,
        result={"rounds_played": 70, "deltas": [0, 0]},
    )
    outcome = build_public_outcome(draw, HOLDEM, duplicate=False)
    assert outcome == {
        "kind": "single",
        "planned_games": 1,
        "completed_games": 1,
        "score": {"wins_a": 0, "draws": 1, "wins_b": 0},
        "rounds_played": 70,
        "normalized_delta_a": 0.0,
        "games": [
            {
                "index": 1,
                "winner": None,
                "rounds_played": 70,
                "normalized_delta_a": 0.0,
            }
        ],
        "termination": {"kind": "normal", "reason": "completed", "loser": None},
    }
    assert build_public_outcome({**draw, "status": "running"}, HOLDEM) is None


def test_unrepresentable_historical_delta_fails_closed_in_public_projection():
    huge = 10**10_000
    match = _completed(
        winner=0,
        result={"rounds_played": 70, "deltas": [huge, -huge]},
    )
    assert build_public_outcome(match, HOLDEM, duplicate=False) is None


@pytest.mark.parametrize(
    ("technical_loss", "reason"),
    [
        (0, "technical_loss"),
        (0, "protocol_error"),
        (1, "completed"),
        (1, "crash"),
    ],
)
def test_explicit_technical_reason_and_flag_must_agree(
    technical_loss, reason
):
    match = _completed(
        winner=0,
        technical_loss=technical_loss,
        result={"rounds_played": 70, "deltas": [100, -100]},
    )
    match["reason"] = reason
    assert scoring_games_for_match(
        match,
        duplicate=False,
        planned_games=1,
        fixed_rounds_per_match=70,
    ) == ()
    assert build_public_outcome(match, HOLDEM, duplicate=False) is None

    # Old rows without a stable reason remain readable in either adjudication
    # mode; only an explicit contradictory code is rejected.
    match["reason"] = ""
    match["result"] = {
        "rounds_played": 0 if technical_loss else 70,
        "deltas": [0, 0] if technical_loss else [100, -100],
    }
    assert build_public_outcome(match, HOLDEM, duplicate=False) is not None


@pytest.mark.parametrize(
    "reason",
    [
        "platform_error",
        "admin_aborted",
        "orphan_after_restart",
        "version_unavailable",
    ],
)
@pytest.mark.parametrize("duplicate", [False, True])
def test_completed_platform_or_abort_reason_has_no_scoring_outcome(
    reason, duplicate
):
    if duplicate:
        match = _completed(
            winner=None,
            result=_duplicate_result(
                {"winner": 0, "rounds_played": 70, "deltas": [100, -100]},
                {"winner": 1, "rounds_played": 70, "deltas": [-100, 100]},
            ),
        )
        match["match_config"] = {"duplicate": True}
    else:
        match = _completed()
        match["match_config"] = {"duplicate": False}
    match["reason"] = reason
    assert scoring_games_for_match(
        match,
        duplicate=duplicate,
        planned_games=2 if duplicate else 1,
        fixed_rounds_per_match=70,
        require_frozen_duplicate=True,
    ) == ()
    assert build_public_outcome(
        match,
        HOLDEM,
        duplicate=duplicate,
        expected_duplicate=duplicate,
        require_frozen_duplicate=True,
    ) is None


def test_unsupported_duplicate_history_fails_closed_instead_of_raising():
    match = _completed(
        winner=0,
        result={"rounds_played": 1, "deltas": [1, -1]},
    )
    match["match_config"] = {"duplicate": True}
    assert build_public_outcome(match, GOMOKU) is None


@pytest.mark.parametrize(
    "match_config",
    [
        {"duplicate": 1},
        {"duplicate": "false"},
        "{bad-json",
        [],
    ],
)
def test_explicit_malformed_frozen_duplicate_flag_fails_closed(match_config):
    match = _completed()
    match["match_config"] = match_config
    assert is_duplicate_match(match) is None
    assert build_public_outcome(match, HOLDEM) is None
    assert scoring_games_for_match(
        match,
        duplicate=False,
        planned_games=1,
        fixed_rounds_per_match=70,
    ) == ()


def test_missing_frozen_duplicate_flag_remains_legacy_single_compatible():
    assert is_duplicate_match({}) is False
    assert is_duplicate_match({"match_config": None}) is False
    assert is_duplicate_match({"match_config": {}}) is False

    strict_match = {
        **_completed(),
        "match_config": None,
        "_contest_expected_duplicate": 0,
        "_contest_require_frozen_duplicate": 1,
    }
    assert build_public_outcome(strict_match, HOLDEM) is None
    assert scoring_games_for_match(
        strict_match,
        duplicate=False,
        planned_games=1,
        fixed_rounds_per_match=70,
        require_frozen_duplicate=True,
    ) == ()


def test_duplicate_games_are_one_based_and_legacy_rounds_are_filled():
    match = _completed(
        winner=None,
        result=_duplicate_result(
            {"winner": 0, "deltas": [500, -500]},
            {"winner": None, "deltas": [0, 0]},
        ),
    )
    outcome = build_public_outcome(match, HOLDEM, duplicate=True)
    assert outcome is not None
    assert outcome["kind"] == "duplicate"
    assert outcome["planned_games"] == outcome["completed_games"] == 2
    assert outcome["score"] == {"wins_a": 1, "draws": 1, "wins_b": 0}
    assert [game["index"] for game in outcome["games"]] == [1, 2]
    assert [game["rounds_played"] for game in outcome["games"]] == [70, 70]
    assert outcome["rounds_played"] == 140
    assert outcome["normalized_delta_a"] == 5.0


def test_legacy_contest_stage_can_supply_duplicate_shape_without_frozen_flag():
    match = _completed(
        winner=None,
        result=_duplicate_result(
            {"winner": 0, "deltas": [100, -100]},
            {"winner": 1, "deltas": [-100, 100]},
        ),
    )
    match.update(
        {
            "match_config": {},
            "_contest_expected_duplicate": 1,
            "_contest_require_frozen_duplicate": 0,
        }
    )
    outcome = build_public_outcome(match, HOLDEM)
    assert outcome is not None
    assert outcome["kind"] == "duplicate"
    assert outcome["completed_games"] == 2

    # New v1 contests require creation-time Match config to agree with the
    # stage; the same historical omission is rejected only under that marker.
    assert build_public_outcome(
        {**match, "_contest_require_frozen_duplicate": 1}, HOLDEM
    ) is None
    without_frozen_config = {
        key: value
        for key, value in match.items()
        if key
        not in {
            "match_config",
            "_contest_expected_duplicate",
            "_contest_require_frozen_duplicate",
        }
    }
    assert build_public_outcome(
        without_frozen_config,
        HOLDEM,
        duplicate=True,
        expected_duplicate=True,
        require_frozen_duplicate=True,
    ) is None
    assert len(
        scoring_games_for_match(
            match,
            duplicate=True,
            planned_games=2,
            fixed_rounds_per_match=70,
        )
    ) == 2
    assert scoring_games_for_match(
        match,
        duplicate=True,
        planned_games=2,
        fixed_rounds_per_match=70,
        require_frozen_duplicate=True,
    ) == ()


def test_direct_ranking_infers_duplicate_from_frozen_match_config():
    result = _duplicate_result(
        {"winner": 0, "deltas": [100, -100], "rounds_played": 70},
        {"winner": None, "deltas": [0, 0], "rounds_played": 70},
    )
    standings = [
        {"entry_id": 11, "points": 4, "delta_total": 100, "seed": 1},
        {"entry_id": 22, "points": 1, "delta_total": -100, "seed": 2},
    ]
    pairings = [{"entry_a_id": 11, "entry_b_id": 22, "match_id": "dup"}]
    matches = {
        "dup": {
            "status": "completed",
            "winner": None,
            "technical_loss": 0,
            "result": result,
            "match_config": {"duplicate": True},
        }
    }
    ranked = compute_official_ranking(standings, pairings, matches)
    by_entry = {row["entry_id"]: row for row in ranked}
    assert by_entry[11]["tiebreaks"]["buchholz"] == 2
    assert by_entry[22]["tiebreaks"]["buchholz"] == 8


@pytest.mark.parametrize(
    ("duplicate", "match"),
    [
        # A single match may never smuggle extra standings records through legs.
        (
            False,
            _completed(
                result={
                    "rounds_played": 70,
                    "deltas": [100, -100],
                    "legs": [{"winner": 0, "deltas": [100, -100]}],
                }
            ),
        ),
        # A normal duplicate is complete only with exactly the frozen plan.
        (
            True,
            _completed(
                winner=None,
                result=_duplicate_result(
                    {"winner": 0, "deltas": [100, -100]}
                ),
            ),
        ),
        (
            True,
            _completed(
                winner=None,
                result={
                    "rounds_played": 71,
                    "deltas": [0, 0],
                    "legs": [
                        {
                            "winner": None,
                            "deltas": [0, 0],
                            "rounds_played": True,
                        },
                        {"winner": None, "deltas": [0, 0]},
                    ],
                },
            ),
        ),
        # A normal duplicate has no aggregate winner; non-null is contradictory.
        (
            True,
            _completed(
                winner=0,
                result=_duplicate_result(
                    {"winner": 0, "deltas": [100, -100]},
                    {"winner": 1, "deltas": [-100, 100]},
                ),
            ),
        ),
        (
            True,
            _completed(
                winner=None,
                result=_duplicate_result(
                    {"winner": 0, "deltas": [100, -100]},
                    {"winner": 1, "deltas": [-100, 100]},
                    {"winner": 0, "deltas": [100, -100]},
                ),
            ),
        ),
        # JSON numeric equality must not turn float seats into integer winners.
        (
            False,
            _completed(
                winner=0.0,
                result={"rounds_played": 70, "deltas": [100, -100]},
            ),
        ),
        (
            True,
            _completed(
                winner=None,
                result=_duplicate_result(
                    {"winner": 0.0, "deltas": [100, -100]},
                    {"winner": 1, "deltas": [-100, 100]},
                ),
            ),
        ),
        (
            True,
            _completed(
                winner=1.0,
                technical_loss=1,
                result={"rounds_played": 13, "deltas": [0, 0]},
            ),
        ),
        # Bool is not a legal seat/delta/round integer.
        (
            True,
            _completed(
                winner=None,
                result=_duplicate_result(
                    {"winner": True, "deltas": [100, -100]},
                    {"winner": 1, "deltas": [-100, 100]},
                ),
            ),
        ),
        (
            True,
            _completed(
                winner=None,
                result={
                    "rounds_played": 140,
                    "deltas": [0, 0],
                    "legs": [
                        {"winner": 0, "deltas": [True, -1]},
                        {"winner": 1, "deltas": [-1, 1]},
                    ],
                },
            ),
        ),
        # Aggregate totals must agree with the independently scored games.
        (
            True,
            _completed(
                winner=None,
                result={
                    **_duplicate_result(
                        {"winner": 0, "deltas": [100, -100]},
                        {"winner": 1, "deltas": [-100, 100]},
                    ),
                    "deltas": [50, -50],
                },
            ),
        ),
    ],
)
def test_malformed_scoring_payload_is_rejected_by_parser_and_outcome(
    duplicate: bool, match: dict
):
    planned = 2 if duplicate else 1
    assert scoring_games_for_match(
        match,
        duplicate=duplicate,
        planned_games=planned,
        fixed_rounds_per_match=70,
    ) == ()
    assert build_public_outcome(match, HOLDEM, duplicate=duplicate) is None


def test_duplicate_technical_terminal_counts_only_actual_game():
    match = _completed(
        winner=1,
        technical_loss=1,
        result={"rounds_played": 13, "deltas": [-1, 1]},
    )
    games = scoring_games_for_match(
        match,
        duplicate=True,
        planned_games=2,
        fixed_rounds_per_match=70,
    )
    assert len(games) == 1
    outcome = build_public_outcome(match, HOLDEM, duplicate=True)
    assert outcome is not None
    assert outcome["planned_games"] == 2
    assert outcome["completed_games"] == 1
    assert outcome["score"] == {"wins_a": 0, "draws": 0, "wins_b": 1}
    assert outcome["termination"] == {
        "kind": "technical",
        "reason": "technical_loss",
        "loser": 0,
    }


def test_strict_contest_technical_terminal_rejects_legacy_delta_sentinel():
    legacy = _completed(
        winner=1,
        technical_loss=1,
        result={"rounds_played": 13, "deltas": [-1, 1]},
    )
    legacy["match_config"] = {"duplicate": True}
    # Generic/history projection remains backward compatible with the former
    # +/-1 adjudication sentinel.
    assert len(
        scoring_games_for_match(
            legacy,
            duplicate=True,
            planned_games=2,
            fixed_rounds_per_match=70,
        )
    ) == 1
    # New independent-v1 contests accept only actual prefix chip deltas.  The
    # current technical writer persists zero when no authoritative prefix is
    # available, so a non-zero sentinel is contradictory history.
    assert scoring_games_for_match(
        legacy,
        duplicate=True,
        planned_games=2,
        fixed_rounds_per_match=70,
        require_frozen_duplicate=True,
    ) == ()
    assert build_public_outcome(
        legacy,
        HOLDEM,
        duplicate=True,
        expected_duplicate=True,
        require_frozen_duplicate=True,
    ) is None

    current = _completed(
        winner=1,
        technical_loss=1,
        result={"rounds_played": 13, "deltas": [0, 0]},
    )
    current["match_config"] = {"duplicate": True}
    assert len(
        scoring_games_for_match(
            current,
            duplicate=True,
            planned_games=2,
            fixed_rounds_per_match=70,
            require_frozen_duplicate=True,
        )
    ) == 1


def test_duplicate_technical_outcome_keeps_physical_game_ordinal():
    result = {
        "rounds_played": 5,
        "deltas": [0, 0],
        "technical_game_index": 2,
        "technical_incident_samples": [
            {"reason": "timeout", "seat": 1, "turn": 6, "leg": 1}
        ],
    }
    match = _completed(winner=0, result=result, technical_loss=1)
    match["match_config"] = {"duplicate": True}
    outcome = build_public_outcome(match, HOLDEM)
    assert outcome is not None
    assert outcome["completed_games"] == 1
    assert [game["index"] for game in outcome["games"]] == [2]

    contradictory = {
        **match,
        "result": {**result, "technical_game_index": 1},
    }
    assert build_public_outcome(contradictory, HOLDEM) is None


@pytest.mark.parametrize("rounds", [69, 71])
def test_normal_single_must_use_fixed_scoring_game_length(rounds):
    match = _completed(
        result={"rounds_played": rounds, "deltas": [100, -100]}
    )
    assert scoring_games_for_match(
        match,
        duplicate=False,
        planned_games=1,
        fixed_rounds_per_match=70,
    ) == ()
    assert build_public_outcome(match, HOLDEM, duplicate=False) is None


@pytest.mark.parametrize("rounds", [69, 71])
def test_normal_duplicate_each_game_must_use_fixed_length(rounds):
    match = _completed(
        winner=None,
        result=_duplicate_result(
            {"winner": 0, "deltas": [100, -100], "rounds_played": rounds},
            {"winner": 1, "deltas": [-100, 100], "rounds_played": 70},
        ),
    )
    assert scoring_games_for_match(
        match,
        duplicate=True,
        planned_games=2,
        fixed_rounds_per_match=70,
    ) == ()
    assert build_public_outcome(match, HOLDEM, duplicate=True) is None


def test_technical_zero_round_terminal_is_valid():
    match = _completed(
        winner=0,
        technical_loss=1,
        result={"rounds_played": 0, "deltas": [0, 0]},
    )
    outcome = build_public_outcome(match, HOLDEM, duplicate=True)
    assert outcome is not None
    assert outcome["completed_games"] == 1
    assert outcome["games"][0]["rounds_played"] == 0
    assert outcome["normalized_delta_a"] == 0


def test_technical_winner_is_independent_from_actual_prefix_delta():
    match = _completed(
        winner=0,
        technical_loss=1,
        result={"rounds_played": 5, "deltas": [-100, 100]},
    )
    outcome = build_public_outcome(match, HOLDEM, duplicate=True)
    assert outcome is not None
    assert outcome["score"] == {"wins_a": 1, "draws": 0, "wins_b": 0}
    assert outcome["normalized_delta_a"] == -1


@pytest.mark.parametrize("technical_loss", [1, True])
def test_technical_terminal_without_winner_is_not_a_draw(technical_loss):
    match = _completed(
        winner=None,
        technical_loss=technical_loss,
        result={"rounds_played": 13, "deltas": [0, 0]},
    )
    assert scoring_games_for_match(
        match,
        duplicate=True,
        planned_games=2,
        fixed_rounds_per_match=70,
    ) == ()
    assert build_public_outcome(match, HOLDEM, duplicate=True) is None


def test_duplicate_technical_overlong_legs_fail_closed():
    match = _completed(
        winner=1,
        technical_loss=1,
        result=_duplicate_result(
            {"winner": 0, "deltas": [100, -100]},
            {"winner": 1, "deltas": [-100, 100]},
            {"winner": 1, "deltas": [-1, 1]},
        ),
    )
    assert scoring_games_for_match(
        match,
        duplicate=True,
        planned_games=2,
        fixed_rounds_per_match=70,
    ) == ()
    assert build_public_outcome(match, HOLDEM, duplicate=True) is None


def test_duplicate_technical_two_legs_fail_closed():
    match = _completed(
        winner=1,
        technical_loss=1,
        result=_duplicate_result(
            {"winner": 0, "deltas": [100, -100]},
            {"winner": 1, "deltas": [-1, 1]},
        ),
    )
    assert scoring_games_for_match(
        match,
        duplicate=True,
        planned_games=2,
        fixed_rounds_per_match=70,
    ) == ()
    assert build_public_outcome(match, HOLDEM, duplicate=True) is None


@pytest.mark.parametrize(
    ("duplicate", "match"),
    [
        (
            False,
            _completed(
                winner=0,
                result={"rounds_played": 70, "deltas": [-100, 100]},
            ),
        ),
        (
            False,
            _completed(
                winner=None,
                result={"rounds_played": 70, "deltas": [100, -100]},
            ),
        ),
        (
            True,
            _completed(
                winner=None,
                result=_duplicate_result(
                    {"winner": 0, "deltas": [-100, 100]},
                    {"winner": 1, "deltas": [-100, 100]},
                ),
            ),
        ),
    ],
)
def test_winner_and_delta_sign_must_be_self_consistent(duplicate, match):
    assert scoring_games_for_match(
        match,
        duplicate=duplicate,
        planned_games=2 if duplicate else 1,
        fixed_rounds_per_match=70,
    ) == ()
    assert build_public_outcome(match, HOLDEM, duplicate=duplicate) is None


def test_standings_and_ranking_share_malformed_duplicate_refusal():
    stage = {
        "key": "rr",
        "type": "round_robin",
        "games_per_pair": 2,
        "duplicate": True,
        "series_scoring": "independent_scoring_game_points_v1",
        "scoring": "poker_3_1_0",
    }
    contest = {
        "id": 9,
        "game_id": "holdem",
        "status": "running",
        "current_stage_idx": 0,
        "stages_json": [stage],
    }
    entries = [
        {"id": 11, "bot_id": 101, "user_id": 1001, "seed": 1},
        {"id": 22, "bot_id": 202, "user_id": 2002, "seed": 2},
    ]
    valid_result = _duplicate_result(
        {"winner": 0, "deltas": [100, -100]},
        {"winner": None, "deltas": [0, 0]},
    )
    malformed_result = _duplicate_result(
        {"winner": 0, "deltas": [100, -100]},
        {"winner": 1, "deltas": [-100, 100]},
        {"winner": 0, "deltas": [100, -100]},
    )
    pairings = [
        {
            "contest_id": 9,
            "entry_a_id": 11,
            "entry_b_id": 22,
            "bot_a_id": 101,
            "bot_b_id": 202,
            "_entry_a_user_id": 1001,
            "_entry_b_user_id": 2002,
            "_pairing_bot_a_owner_id": 1001,
            "_pairing_bot_b_owner_id": 2002,
            "match_id": "valid",
            "round_num": 1,
            "series_index": 1,
            "series_size": 2,
            "match_status": "completed",
            "match_winner": None,
            "_match_result_json": valid_result,
            "_match_technical_loss": 0,
            "_match_config_json": {"duplicate": True},
            "_match_id": "valid",
            "_match_contest_id": 9,
            "_match_game_id": "holdem",
            "_match_type": "contest",
            "_match_bot_a_id": 101,
            "_match_bot_b_id": 202,
        },
        {
            "contest_id": 9,
            "entry_a_id": 22,
            "entry_b_id": 11,
            "bot_a_id": 202,
            "bot_b_id": 101,
            "_entry_a_user_id": 2002,
            "_entry_b_user_id": 1001,
            "_pairing_bot_a_owner_id": 2002,
            "_pairing_bot_b_owner_id": 1001,
            "match_id": "overlong",
            "round_num": 2,
            "series_index": 2,
            "series_size": 2,
            "match_status": "completed",
            "match_winner": None,
            "_match_result_json": malformed_result,
            "_match_technical_loss": 0,
            "_match_config_json": {"duplicate": True},
            "_match_id": "overlong",
            "_match_contest_id": 9,
            "_match_game_id": "holdem",
            "_match_type": "contest",
            "_match_bot_a_id": 202,
            "_match_bot_b_id": 101,
        },
    ]
    manager = ContestManager(None, None)  # all read-model inputs are injected
    standings = manager.standings(
        9, contest=contest, entries=entries, pairings=pairings
    )
    by_entry = {row["entry_id"]: row for row in standings}
    assert (by_entry[11]["points"], by_entry[11]["wins"], by_entry[11]["draws"]) == (
        4,
        1,
        1,
    )
    assert by_entry[11]["counts"] == {
        "encounter_groups": 1,
        "unique_opponents": 1,
        "match_jobs": 1,
        "scoring_games": 2,
    }
    assert by_entry[22]["counts"] == by_entry[11]["counts"]

    matches = {
        "valid": {
            "status": "completed",
            "winner": None,
            "technical_loss": 0,
            "result": valid_result,
            "match_config": {"duplicate": True},
        },
        "overlong": {
            "status": "completed",
            "winner": None,
            "technical_loss": 0,
            "result": malformed_result,
            "match_config": {"duplicate": True},
        },
    }
    ranked = compute_official_ranking(
        standings,
        pairings,
        matches,
        stage=stage,
        planned_games_per_match=2,
        fixed_rounds_per_match=70,
    )
    ranked_by_entry = {row["entry_id"]: row for row in ranked}
    # Only the valid match's two games enter opponent-strength records.
    assert ranked_by_entry[11]["tiebreaks"]["buchholz"] == 2
    assert ranked_by_entry[22]["tiebreaks"]["buchholz"] == 8
