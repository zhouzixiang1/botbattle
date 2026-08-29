"""Independent per-game contest scoring, counts, and tie-break contracts."""
from __future__ import annotations

import asyncio
import itertools
import json

import pytest

from bzplat.backend.games import registry
from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.presentation import build_stage_counts
from bzplat.backend.contests.ranking import compute_official_ranking
from bzplat.backend.contests.series import (
    series_rows_settled,
    swiss_bye_record_weights,
)
from bzplat.backend.contests.validation import (
    SERIES_SCORING_AGGREGATE,
    SERIES_SCORING_INDEPENDENT,
    stage_scoring_contract_is_valid,
)
from bzplat.backend.store import Store


def _single_result(winner: int | None, delta: int = 100) -> dict:
    signed = 0 if winner is None else delta if winner == 0 else -delta
    return {"rounds_played": 70, "deltas": [signed, -signed]}


def _duplicate_result(first: int | None, second: int | None) -> dict:
    legs = []
    for winner in (first, second):
        signed = 0 if winner is None else 100 if winner == 0 else -100
        legs.append(
            {
                "winner": winner,
                "rounds_played": 70,
                "deltas": [signed, -signed],
            }
        )
    return {
        "rounds_played": 140,
        "deltas": [
            sum(leg["deltas"][0] for leg in legs),
            sum(leg["deltas"][1] for leg in legs),
        ],
        "legs": legs,
    }


def _contest(stage: dict) -> dict:
    return {
        "id": 7,
        "game_id": "holdem",
        "current_stage_idx": 0,
        "stages_json": [stage],
    }


def _entries(count: int) -> list[dict]:
    return [
        {
            "id": index + 1,
            "bot_id": 100 + index,
            "user_id": 1_000 + index,
            "seed": index + 1,
        }
        for index in range(count)
    ]


def _projected_pairing(
    *,
    pairing_id: int,
    entry_a: int,
    entry_b: int,
    match_id: str | None,
    result: dict | None,
    winner: int | None,
    series_index: int,
    series_size: int,
    status: str = "completed",
    round_num: int = 1,
    duplicate: bool = False,
) -> dict:
    return {
        "id": pairing_id,
        "contest_id": 7,
        "entry_a_id": entry_a,
        "entry_b_id": entry_b,
        "_raw_entry_a_id": entry_a,
        "_raw_entry_b_id": entry_b,
        "_explicit_series_marker": 1,
        "_entry_a_user_id": 999 + entry_a,
        "_entry_b_user_id": 999 + entry_b,
        "bot_a_id": 99 + entry_a,
        "bot_b_id": 99 + entry_b,
        "_pairing_bot_a_owner_id": 999 + entry_a,
        "_pairing_bot_b_owner_id": 999 + entry_b,
        "match_id": match_id,
        "status": status if match_id is None else "running",
        "match_status": status if match_id is not None else None,
        "match_winner": winner,
        "_match_result_json": result,
        "_match_config_json": {"duplicate": duplicate},
        "_match_technical_loss": 0,
        "_match_contest_id": 7,
        "_match_game_id": "holdem",
        "_match_type": "contest",
        "_match_bot_a_id": 99 + entry_a,
        "_match_bot_b_id": 99 + entry_b,
        "series_index": series_index,
        "series_size": series_size,
        "round_num": round_num,
    }


def test_k_games_score_immediately_but_incomplete_coordinates_block_advance():
    stage = {
        "key": "rr",
        "type": "round_robin",
        "games_per_pair": 2,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
    }
    first = _projected_pairing(
        pairing_id=1,
        entry_a=1,
        entry_b=2,
        match_id="m1",
        result=_single_result(0),
        winner=0,
        series_index=1,
        series_size=2,
    )
    second = _projected_pairing(
        pairing_id=2,
        entry_a=2,
        entry_b=1,
        match_id=None,
        result=None,
        winner=None,
        series_index=2,
        series_size=2,
        status="pending",
        round_num=2,
    )
    manager = ContestManager(None, None)
    standings = manager.standings(
        7,
        contest=_contest(stage),
        entries=_entries(2),
        pairings=[first, second],
    )
    by_entry = {row["entry_id"]: row for row in standings}
    assert (by_entry[1]["points"], by_entry[1]["wins"]) == (3, 1)
    assert by_entry[1]["counts"] == {
        "encounter_groups": 1,
        "unique_opponents": 1,
        "match_jobs": 1,
        "scoring_games": 1,
    }
    matches = {
        "m1": {
            "status": "completed",
            "winner": 0,
            "result": _single_result(0),
            "match_config": {"duplicate": False},
        }
    }
    assert not series_rows_settled(
        stage, [first, second], matches.get, game_spec=registry.get("holdem")
    )

    second["match_id"] = "m2"
    second["match_status"] = "completed"
    second["match_winner"] = 1
    second["_match_result_json"] = _single_result(1)
    matches["m2"] = {
        "status": "completed",
        "winner": 1,
        "result": _single_result(1),
        "match_config": {"duplicate": False},
    }
    assert series_rows_settled(
        stage, [first, second], matches.get, game_spec=registry.get("holdem")
    )
    second["series_index"] = 0
    assert not series_rows_settled(
        stage, [first, second], matches.get, game_spec=registry.get("holdem")
    )


@pytest.mark.parametrize(
    ("games_per_pair", "award", "buchholz", "cut1", "sonneborn"),
    [
        (1, 3, 3, 0, 3),
        (2, 4, 8, 4, 6),
        (4, 8, 32, 24, 24),
    ],
)
def test_swiss_bye_award_and_virtual_opponent_records(
    games_per_pair: int,
    award: int,
    buchholz: int,
    cut1: int,
    sonneborn: int,
):
    stage = {
        "key": "swiss",
        "type": "swiss",
        "rounds": 0,
        "games_per_pair": games_per_pair,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
        "effective_rounds": 3,
    }
    bye = {
        "id": 1,
        "entry_a_id": 1,
        "entry_b_id": None,
        "bot_a_id": 100,
        "bot_b_id": None,
        "match_id": None,
        "status": "completed",
        "round_num": 1,
        "series_index": 1,
        "series_size": 1,
        "contest_id": 7,
        "_raw_entry_a_id": 1,
        "_explicit_series_marker": 1,
        "_entry_a_user_id": 1000,
        "_pairing_bot_a_owner_id": 1000,
    }
    standings = ContestManager(None, None).standings(
        7,
        contest=_contest(stage),
        entries=_entries(1),
        pairings=[bye],
    )
    row = standings[0]
    assert row["points"] == award
    assert (row["wins"], row["draws"], row["losses"], row["byes"]) == (
        0,
        0,
        0,
        1,
    )
    assert row["counts"] == {
        "encounter_groups": 0,
        "unique_opponents": 0,
        "match_jobs": 0,
        "scoring_games": 0,
    }
    ranked = compute_official_ranking(
        standings, [bye], {}, stage=stage, planned_games_per_match=1
    )[0]
    assert ranked["tiebreaks"]["buchholz"] == buchholz
    assert ranked["tiebreaks"]["buchholz_cut1"] == cut1
    assert ranked["tiebreaks"]["sonneborn_berger"] == sonneborn


@pytest.mark.parametrize(
    ("games_per_pair", "award", "buchholz", "cut1", "sonneborn"),
    [
        (1, 4, 8, 4, 6),
        (2, 8, 32, 24, 24),
    ],
)
def test_duplicate_swiss_bye_counts_every_planned_scoring_game(
    games_per_pair: int,
    award: int,
    buchholz: int,
    cut1: int,
    sonneborn: int,
):
    """A duplicate Match contributes two virtual games to a Swiss bye."""
    stage = {
        "key": "swiss",
        "type": "swiss",
        "rounds": 0,
        "games_per_pair": games_per_pair,
        "duplicate": True,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
        "effective_rounds": 3,
    }
    bye = {
        "id": 1,
        "entry_a_id": 1,
        "entry_b_id": None,
        "bot_a_id": 100,
        "bot_b_id": None,
        "match_id": None,
        "status": "completed",
        "round_num": 1,
        "series_index": 1,
        "series_size": 1,
        "contest_id": 7,
        "_raw_entry_a_id": 1,
        "_explicit_series_marker": 1,
        "_entry_a_user_id": 1000,
        "_pairing_bot_a_owner_id": 1000,
    }
    standings = ContestManager(None, None).standings(
        7,
        contest=_contest(stage),
        entries=_entries(1),
        pairings=[bye],
    )
    row = standings[0]
    assert row["points"] == award
    assert (row["wins"], row["draws"], row["losses"], row["byes"]) == (
        0,
        0,
        0,
        1,
    )
    assert row["counts"] == {
        "encounter_groups": 0,
        "unique_opponents": 0,
        "match_jobs": 0,
        "scoring_games": 0,
    }
    weights = swiss_bye_record_weights(
        stage, scoring_games_per_match=2
    )
    assert weights == tuple(
        weight
        for _ in range(games_per_pair)
        for weight in (1.0, 0.5)
    )
    ranked = compute_official_ranking(
        standings,
        [bye],
        {},
        stage=stage,
        planned_games_per_match=2,
        game_id="holdem",
    )[0]
    assert ranked["tiebreaks"]["buchholz"] == buchholz
    assert ranked["tiebreaks"]["buchholz_cut1"] == cut1
    assert ranked["tiebreaks"]["sonneborn_berger"] == sonneborn


@pytest.mark.parametrize("games_per_pair", ["2", True, 3])
def test_invalid_strict_swiss_game_count_never_awards_a_bye(games_per_pair):
    stage = {
        "key": "swiss",
        "type": "swiss",
        "rounds": 0,
        "games_per_pair": games_per_pair,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
        "effective_rounds": 3,
    }
    bye = {
        "id": 1,
        "entry_a_id": 1,
        "entry_b_id": None,
        "bot_a_id": 100,
        "bot_b_id": None,
        "match_id": None,
        "status": "completed",
        "round_num": 1,
        "series_index": 1,
        "series_size": 1,
    }
    assert not stage_scoring_contract_is_valid(stage, game_id="holdem")
    with pytest.raises(ValueError):
        swiss_bye_record_weights(stage)
    standings = ContestManager(None, None).standings(
        7,
        contest=_contest(stage),
        entries=_entries(1),
        pairings=[bye],
    )
    assert len(standings) == 1
    assert standings[0]["points"] == 0
    assert standings[0]["byes"] == 0
    assert compute_official_ranking(
        standings,
        [bye],
        {},
        stage=stage,
        planned_games_per_match=1,
        game_id="holdem",
    ) == []
    counts = build_stage_counts(
        stage,
        [bye],
        game_id="holdem",
        expected_entry_ids={1},
        expected_swiss_rounds=1,
    )
    assert counts["scoring_games"] == {
        "planned": 0,
        "completed": 0,
        "terminal_unplayed": 0,
    }


def test_frozen_stage_validator_keeps_rounds_zero_and_legacy_one_group_valid():
    strict_swiss = {
        "key": "prelim",
        "type": "swiss",
        "rounds": 0,
        "games_per_pair": 2,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
        "effective_rounds": 3,
    }
    assert stage_scoring_contract_is_valid(strict_swiss, game_id="holdem")
    assert stage_scoring_contract_is_valid(strict_swiss)
    assert stage_scoring_contract_is_valid(
        {
            "key": "group",
            "type": "group_round_robin",
            "scoring": "poker_3_1_0",
            "group_count": 1,
        },
        game_id="holdem",
    )


def test_invalid_frozen_ranking_scope_blocks_official_result_finalize(tmp_path):
    store = Store(str(tmp_path / "invalid-ranking-scope.db"))
    organizer = store.create_user(
        "invalid-scope-org", "invalid-scope-org@example.com", "hash"
    )
    stage = {
        "key": "final",
        "type": "double_round_robin",
        "scoring": "poker_3_1_0",
        "games_per_pair": 2,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "ranking_mode": "replace_top",
        "ranking_scope": "4",
    }
    contest = store.create_contest(
        "Invalid frozen ranking scope",
        organizer["id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    manager = ContestManager(store, None)
    with pytest.raises(ValueError, match="阶段计分契约无效"):
        manager._finalize_official_results(contest["id"], 0)
    assert store.list_official_results(contest["id"]) == []
    assert int(store.get_contest(contest["id"])["official_results_ready"] or 0) == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("rounds", "0"),
        ("effective_rounds", "3"),
        ("swiss_extra_rounds", True),
    ],
)
def test_malformed_explicit_swiss_round_contract_is_fail_closed(field, value):
    stage = {
        "key": "prelim",
        "type": "swiss",
        "rounds": 0,
        "games_per_pair": 2,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
        "effective_rounds": 3,
        "swiss_extra_rounds": 0,
    }
    stage[field] = value
    assert not stage_scoring_contract_is_valid(stage, game_id="holdem")
    pairings, matches = _complete_series_rows([(1, 2)])
    for pairing in pairings:
        pairing["round_num"] = 1
    store = _TopologyStore(stage, pairings, matches, entry_count=2)
    manager = ContestManager(store, None)
    assert manager._stage_done(7, 0) is False
    assert asyncio.run(manager._maybe_next_swiss_round(7, 0, stage)) is False


def test_swiss_virtual_points_are_capped_by_personal_planned_draw_points():
    stage = {
        "type": "swiss",
        "rounds": 0,
        "games_per_pair": 2,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
        "effective_rounds": 3,
    }
    bye = {
        "entry_a_id": 1,
        "entry_b_id": None,
        "bot_a_id": 100,
        "bot_b_id": None,
        "match_id": None,
        "status": "completed",
        "round_num": 1,
    }
    standings = [{"entry_id": 1, "points": 100, "delta_total": 0, "seed": 1}]
    row = compute_official_ranking(standings, [bye], {}, stage=stage)[0]
    # min(final points 100, draw 1 * effective rounds 3 * K 2) = 6
    assert row["tiebreaks"]["buchholz"] == 12
    assert row["tiebreaks"]["buchholz_cut1"] == 6
    assert row["tiebreaks"]["sonneborn_berger"] == 9


def test_independent_v1_cut1_removes_lowest_and_draw_h2h_is_half():
    stage = {
        "type": "round_robin",
        "games_per_pair": 1,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
    }
    pairings = [
        {"entry_a_id": 1, "entry_b_id": 2, "match_id": "low"},
        {"entry_a_id": 1, "entry_b_id": 3, "match_id": "high"},
    ]
    matches = {
        "low": {
            "status": "completed",
            "winner": 0,
            "result": _single_result(0),
            "match_config": {"duplicate": False},
        },
        "high": {
            "status": "completed",
            "winner": 0,
            "result": _single_result(0),
            "match_config": {"duplicate": False},
        },
    }
    standings = [
        {"entry_id": 1, "points": 10, "delta_total": 0, "seed": 1},
        {"entry_id": 2, "points": 2, "delta_total": 0, "seed": 2},
        {"entry_id": 3, "points": 8, "delta_total": 0, "seed": 3},
    ]
    independent = compute_official_ranking(
        standings, pairings, matches, stage=stage, fixed_rounds_per_match=70
    )
    legacy = compute_official_ranking(
        standings,
        pairings,
        matches,
        stage={"type": "round_robin", "games_per_pair": 1},
        fixed_rounds_per_match=70,
    )
    independent_one = next(row for row in independent if row["entry_id"] == 1)
    legacy_one = next(row for row in legacy if row["entry_id"] == 1)
    assert independent_one["tiebreaks"]["buchholz_cut1"] == 8
    assert legacy_one["tiebreaks"]["buchholz_cut1"] == 2

    draw_pairing = [{"entry_a_id": 1, "entry_b_id": 2, "match_id": "draw"}]
    draw_match = {
        "draw": {
            "status": "completed",
            "winner": None,
            "result": _single_result(None),
            "match_config": {"duplicate": False},
        }
    }
    tied = [
        {"entry_id": 1, "points": 1, "delta_total": 0, "seed": 1},
        {"entry_id": 2, "points": 1, "delta_total": 0, "seed": 2},
    ]
    ranked = compute_official_ranking(
        tied,
        draw_pairing,
        draw_match,
        stage=stage,
        fixed_rounds_per_match=70,
    )
    assert {row["tiebreaks"]["head_to_head"] for row in ranked} == {0.5}


def test_four_player_duplicate_counts_distinguish_encounters_jobs_and_games():
    stage = {
        "key": "dup_rr",
        "type": "round_robin",
        "games_per_pair": 1,
        "duplicate": True,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
    }
    entries = _entries(4)
    pairings = []
    for pairing_id, (left, right) in enumerate(
        itertools.combinations(range(1, 5), 2), start=1
    ):
        pairings.append(
            _projected_pairing(
                pairing_id=pairing_id,
                entry_a=left,
                entry_b=right,
                match_id=f"d{pairing_id}",
                result=_duplicate_result(0, 1),
                winner=None,
                series_index=1,
                series_size=1,
                round_num=pairing_id,
                duplicate=True,
            )
        )
    standings = ContestManager(None, None).standings(
        7,
        contest=_contest(stage),
        entries=entries,
        pairings=pairings,
    )
    assert all(row["wins"] + row["draws"] + row["losses"] == 6 for row in standings)
    assert all(
        row["counts"]
        == {
            "encounter_groups": 3,
            "unique_opponents": 3,
            "match_jobs": 3,
            "scoring_games": 6,
        }
        for row in standings
    )
    assert build_stage_counts(stage, pairings, game_id="holdem") == {
        "encounter_groups": {"completed": 6, "total": 6},
        "match_jobs": {"completed": 6, "total": 6},
        "scoring_games": {"completed": 12, "planned": 12, "terminal_unplayed": 0},
    }


def test_swiss_repeated_opponent_counts_encounters_but_only_one_unique_opponent():
    stage = {
        "key": "swiss",
        "type": "swiss",
        "rounds": 0,
        "games_per_pair": 1,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
        "effective_rounds": 2,
    }
    pairings = [
        _projected_pairing(
            pairing_id=round_num,
            entry_a=1 if round_num == 1 else 2,
            entry_b=2 if round_num == 1 else 1,
            match_id=f"repeat-{round_num}",
            result=_single_result(0),
            winner=0,
            series_index=1,
            series_size=1,
            round_num=round_num,
        )
        for round_num in (1, 2)
    ]
    standings = ContestManager(None, None).standings(
        7,
        contest=_contest(stage),
        entries=_entries(2),
        pairings=pairings,
    )
    assert all(
        row["counts"] == {
            "encounter_groups": 2,
            "unique_opponents": 1,
            "match_jobs": 2,
            "scoring_games": 2,
        }
        for row in standings
    )


def test_malformed_completed_duplicate_blocks_stage_swiss_and_force_finish(tmp_path):
    store = Store(str(tmp_path / "malformed-gate.db"))
    organizer = store.create_user("gate-org", "gate-org@example.com", "hash")
    users = [
        store.create_user(f"gate-u{i}", f"gate-u{i}@example.com", "hash")
        for i in range(2)
    ]
    bots = []
    for index, user in enumerate(users):
        binary = tmp_path / f"gate-{index}.bin"
        binary.write_bytes(b"test fixture")
        bots.append(
            store.create_bot(
                user["id"],
                f"gate-b{index}",
                binary_path=str(binary),
                format="elf",
                game_id="holdem",
            )
        )
    stage = {
        "key": "swiss",
        "type": "swiss",
        "games_per_pair": 1,
        "duplicate": True,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
        "effective_rounds": 2,
    }
    contest = store.create_contest(
        "malformed gate",
        organizer["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    pairing = store.add_pairing(
        contest["id"],
        bots[0]["id"],
        bots[1]["id"],
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        stage_idx=0,
        stage_key="swiss",
        round_num=1,
        series_index=1,
        series_size=1,
    )
    match_id = "malformed-duplicate"
    store.create_match(
        match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=organizer["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
        match_config={"duplicate": True},
    )
    store.bind_contest_pairing_match(
        contest["id"],
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    # Completed status alone is insufficient: a normal duplicate without its
    # two independent legs is not an adjudicated scoring result.
    store.update_match(
        match_id,
        status="completed",
        winner=None,
        result={"rounds_played": 140, "deltas": [0, 0]},
        ended_at="2026-08-28T12:00:00+08:00",
    )
    store.complete_contest_pairing_for_match(contest["id"], match_id)
    store.update_contest(contest["id"], status="running")
    manager = ContestManager(store, None)

    assert manager._stage_done(contest["id"], 0) is False
    before = len(store.list_contest_pairings(contest["id"], stage_idx=0))
    assert asyncio.run(manager._maybe_next_swiss_round(contest["id"], 0, stage)) is False
    assert len(store.list_contest_pairings(contest["id"], stage_idx=0)) == before
    assert manager._has_unfinished_pairings(contest["id"]) is True
    with pytest.raises(ValueError, match="仍有未完成对阵"):
        asyncio.run(manager.finish(contest["id"]))
    assert store.get_contest(contest["id"])["status"] == "running"
    store.close()


class _TopologyStore:
    def __init__(self, stage, pairings, matches, *, entry_count=4):
        self.contest = _contest(stage)
        self.pairings = pairings
        self.matches = matches
        self.entries = _entries(entry_count)

    def get_contest(self, _contest_id):
        return self.contest

    def list_contest_pairings(self, _contest_id, *, stage_idx=None):
        return list(self.pairings)

    def list_contest_entries(self, _contest_id):
        return list(self.entries)

    def get_match(self, match_id):
        return self.matches.get(str(match_id))

    def contest_has_active_matches(self, _contest_id):
        return False

    def list_stage_results(self, _contest_id):
        return []


def _complete_series_rows(groups, *, games_per_pair=2):
    pairings = []
    matches = {}
    pairing_id = 1
    for round_num, (first, second) in enumerate(groups, 1):
        for series_index in range(1, games_per_pair + 1):
            match_id = f"topology-{first}-{second}-{series_index}"
            pairing = _projected_pairing(
                pairing_id=pairing_id,
                entry_a=first,
                entry_b=second,
                match_id=match_id,
                result=_single_result(0),
                winner=0,
                series_index=series_index,
                series_size=games_per_pair,
                round_num=round_num,
            )
            pairings.append(pairing)
            matches[match_id] = {
                "status": "completed",
                "winner": 0,
                "technical_loss": 0,
                "result": _single_result(0),
                "match_config": {"duplicate": False},
            }
            pairing_id += 1
    return pairings, matches


def test_reason_flag_conflict_scores_nothing_and_blocks_strict_lifecycle():
    stage = {
        "key": "rr",
        "type": "round_robin",
        "games_per_pair": 1,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
    }
    pairings, matches = _complete_series_rows([(1, 2)], games_per_pair=1)
    match_id = pairings[0]["match_id"]
    pairings[0]["_match_reason"] = "technical_loss"
    matches[match_id]["reason"] = "technical_loss"
    # Explicit technical reason with technical_loss=0 is contradictory.  The
    # shared parser must reject it for both the read model and lifecycle gate.
    store = _TopologyStore(stage, pairings, matches, entry_count=2)
    manager = ContestManager(store, None)

    rows = manager.standings(
        7,
        contest=store.contest,
        entries=store.entries,
        pairings=pairings,
    )
    assert all(row["points"] == 0 for row in rows)
    assert all(row["counts"]["scoring_games"] == 0 for row in rows)
    assert manager._stage_done(7, 0) is False
    assert manager._has_unfinished_pairings(7) is True


@pytest.mark.parametrize("shape", ["single", "duplicate_leg", "technical_top"])
def test_non_integer_winner_fails_closed_across_standings_and_lifecycle(shape):
    duplicate = shape != "single"
    technical = shape == "technical_top"
    if shape == "single":
        winner = 0.0
        result = {"rounds_played": 70, "deltas": [100, -100]}
    elif shape == "duplicate_leg":
        winner = None
        result = _duplicate_result(
            {"winner": 0.0, "rounds_played": 70, "deltas": [100, -100]},
            {"winner": 1, "rounds_played": 70, "deltas": [-100, 100]},
        )
    else:
        winner = 1.0
        result = {"rounds_played": 13, "deltas": [0, 0]}
    stage = {
        "key": "rr",
        "type": "round_robin",
        "games_per_pair": 1,
        "duplicate": duplicate,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
    }
    match_id = f"winner-{shape}"
    pairing = _projected_pairing(
        pairing_id=1,
        entry_a=1,
        entry_b=2,
        match_id=match_id,
        result=result,
        winner=winner,
        series_index=1,
        series_size=1,
        duplicate=duplicate,
    )
    pairing["_match_reason"] = "technical_loss" if technical else "completed"
    pairing["_match_technical_loss"] = int(technical)
    match = {
        "id": match_id,
        "status": "completed",
        "winner": winner,
        "reason": "technical_loss" if technical else "completed",
        "technical_loss": int(technical),
        "result": result,
        "match_config": {"duplicate": duplicate},
        "contest_id": 7,
        "game_id": "holdem",
        "match_type": "contest",
        "bot_a_id": 100,
        "bot_b_id": 101,
    }
    store = _TopologyStore(stage, [pairing], {match_id: match}, entry_count=2)
    manager = ContestManager(store, None)
    standings = manager.standings(
        7,
        contest=store.contest,
        entries=store.entries,
        pairings=[pairing],
    )
    assert all(row["points"] == 0 for row in standings)
    assert all(row["counts"]["scoring_games"] == 0 for row in standings)
    assert manager._stage_done(7, 0) is False


@pytest.mark.parametrize(
    ("series_scoring", "duplicate"),
    [
        (SERIES_SCORING_INDEPENDENT, False),
        (SERIES_SCORING_INDEPENDENT, True),
        (SERIES_SCORING_AGGREGATE, False),
    ],
)
def test_completed_platform_reason_scores_nothing_and_blocks_stage(
    series_scoring, duplicate
):
    result = (
        _duplicate_result(
            {"winner": 0, "rounds_played": 70, "deltas": [100, -100]},
            {"winner": 1, "rounds_played": 70, "deltas": [-100, 100]},
        )
        if duplicate
        else {"rounds_played": 70, "deltas": [100, -100]}
    )
    winner = None if duplicate else 0
    stage = {
        "key": "rr",
        "type": "round_robin",
        "games_per_pair": 1,
        "duplicate": duplicate,
        "series_scoring": series_scoring,
        "scoring": "poker_3_1_0",
    }
    match_id = f"platform-reason-{series_scoring}-{int(duplicate)}"
    pairing = _projected_pairing(
        pairing_id=1,
        entry_a=1,
        entry_b=2,
        match_id=match_id,
        result=result,
        winner=winner,
        series_index=1,
        series_size=1,
        duplicate=duplicate,
    )
    pairing["_match_reason"] = "platform_error"
    match = {
        "id": match_id,
        "status": "completed",
        "winner": winner,
        "reason": "platform_error",
        "technical_loss": 0,
        "result": result,
        "match_config": {"duplicate": duplicate},
        "contest_id": 7,
        "game_id": "holdem",
        "match_type": "contest",
        "bot_a_id": 100,
        "bot_b_id": 101,
    }
    store = _TopologyStore(stage, [pairing], {match_id: match}, entry_count=2)
    manager = ContestManager(store, None)
    standings = manager.standings(
        7,
        contest=store.contest,
        entries=store.entries,
        pairings=[pairing],
    )
    assert all(row["points"] == 0 for row in standings)
    assert all(row["counts"]["scoring_games"] == 0 for row in standings)
    assert manager._stage_done(7, 0) is False


def test_round_robin_missing_entire_opponent_group_blocks_stage_and_finish():
    stage = {
        "key": "rr",
        "type": "round_robin",
        "games_per_pair": 2,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
    }
    all_groups = list(itertools.combinations(range(1, 5), 2))
    pairings, matches = _complete_series_rows(all_groups)
    missing_group_rows = [
        row
        for row in pairings
        if {row["entry_a_id"], row["entry_b_id"]} != {3, 4}
    ]
    store = _TopologyStore(stage, missing_group_rows, matches)
    manager = ContestManager(store, None)

    assert manager._stage_done(7, 0) is False
    assert manager._has_unfinished_pairings(7) is True


def test_aggregate_round_robin_missing_entire_group_blocks_legacy_finish():
    stage = {
        "key": "legacy-rr",
        "type": "round_robin",
        "games_per_pair": 1,
        "series_scoring": SERIES_SCORING_AGGREGATE,
        "scoring": "poker_3_1_0",
    }
    pairings, matches = _complete_series_rows(
        [(1, 2), (1, 3), (2, 3)], games_per_pair=1
    )
    assert series_rows_settled(
        stage,
        pairings,
        matches.get,
        game_spec=registry.get("holdem"),
        all_pairings=pairings,
        expected_entry_ids={1, 2, 3},
    )
    missing_group = [
        row
        for row in pairings
        if {row["entry_a_id"], row["entry_b_id"]} != {2, 3}
    ]
    assert not series_rows_settled(
        stage,
        missing_group,
        matches.get,
        game_spec=registry.get("holdem"),
        all_pairings=missing_group,
        expected_entry_ids={1, 2, 3},
    )
    manager = ContestManager(
        _TopologyStore(stage, missing_group, matches, entry_count=3), None
    )
    assert manager._stage_done(7, 0) is False
    assert manager._has_unfinished_pairings(7) is True


def test_aggregate_swiss_missing_group_or_bye_blocks_next_round():
    stage = {
        "key": "legacy-swiss",
        "type": "swiss",
        "rounds": 2,
        "games_per_pair": 1,
        "series_scoring": SERIES_SCORING_AGGREGATE,
        "scoring": "poker_3_1_0",
    }
    real, matches = _complete_series_rows([(1, 2)], games_per_pair=1)
    real[0]["round_num"] = 1
    bye = {
        "id": 99,
        "entry_a_id": 3,
        "entry_b_id": None,
        "bot_a_id": 102,
        "bot_b_id": None,
        "match_id": None,
        "status": "completed",
        "round_num": 1,
        "series_index": 1,
        "series_size": 1,
    }
    complete = real + [bye]
    assert series_rows_settled(
        stage,
        real,
        matches.get,
        game_spec=registry.get("holdem"),
        all_pairings=complete,
        expected_entry_ids={1, 2, 3},
        expected_swiss_rounds=1,
    )
    assert not series_rows_settled(
        stage,
        real,
        matches.get,
        game_spec=registry.get("holdem"),
        all_pairings=real,
        expected_entry_ids={1, 2, 3},
        expected_swiss_rounds=1,
    )
    assert not series_rows_settled(
        stage,
        [],
        matches.get,
        game_spec=registry.get("holdem"),
        all_pairings=[bye],
        expected_entry_ids={1, 2, 3},
        expected_swiss_rounds=1,
    )
    manager = ContestManager(
        _TopologyStore(stage, real, matches, entry_count=3), None
    )
    assert manager._stage_done(7, 0) is False
    assert asyncio.run(manager._maybe_next_swiss_round(7, 0, stage)) is False


def test_strict_later_stage_standings_keep_missing_participant_row():
    stage = {
        "key": "final",
        "type": "round_robin",
        "games_per_pair": 1,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
    }
    contest = {
        "id": 7,
        "game_id": "holdem",
        "current_stage_idx": 1,
        "stages_json": [{"key": "prelim", "type": "swiss"}, stage],
    }
    pairings = [
        _projected_pairing(
            pairing_id=1,
            entry_a=1,
            entry_b=2,
            match_id="later-12",
            result=_single_result(0),
            winner=0,
            series_index=1,
            series_size=1,
        ),
        _projected_pairing(
            pairing_id=2,
            entry_a=1,
            entry_b=3,
            match_id="later-13",
            result=_single_result(0),
            winner=0,
            series_index=1,
            series_size=1,
        ),
    ]
    rows = ContestManager(None, None).standings(
        7,
        stage_idx=1,
        contest=contest,
        entries=_entries(4),
        pairings=pairings,
    )
    assert {row["entry_id"] for row in rows} == {1, 2, 3, 4}
    missing = next(row for row in rows if row["entry_id"] == 4)
    assert missing["points"] == 0
    assert missing["counts"] == {
        "encounter_groups": 0,
        "unique_opponents": 0,
        "match_jobs": 0,
        "scoring_games": 0,
    }


def test_swiss_missing_entire_pair_group_blocks_next_round():
    stage = {
        "key": "swiss",
        "type": "swiss",
        "rounds": 0,
        "games_per_pair": 2,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
        "effective_rounds": 2,
    }
    pairings, matches = _complete_series_rows([(1, 2), (3, 4)])
    # Both opponent groups belong to Swiss round 1; K coordinates share it.
    for row in pairings:
        row["round_num"] = 1
    missing_group_rows = [
        row
        for row in pairings
        if {row["entry_a_id"], row["entry_b_id"]} != {3, 4}
    ]
    assert series_rows_settled(
        stage,
        pairings,
        matches.get,
        game_spec=registry.get("holdem"),
        all_pairings=pairings,
        expected_entry_ids={1, 2, 3, 4},
        expected_swiss_rounds=1,
    ) is True
    assert series_rows_settled(
        stage,
        missing_group_rows,
        matches.get,
        game_spec=registry.get("holdem"),
        all_pairings=missing_group_rows,
        expected_entry_ids={1, 2, 3, 4},
        expected_swiss_rounds=1,
    ) is False
    store = _TopologyStore(stage, missing_group_rows, matches)
    manager = ContestManager(store, None)
    assert asyncio.run(manager._maybe_next_swiss_round(7, 0, stage)) is False

    one_real_group = [
        row
        for row in pairings
        if {row["entry_a_id"], row["entry_b_id"]} == {1, 2}
    ]
    extra_byes = [
        {
            "id": 100 + entry_id,
            "entry_a_id": entry_id,
            "entry_b_id": None,
            "bot_a_id": 99 + entry_id,
            "bot_b_id": None,
            "match_id": None,
            "status": "completed",
            "round_num": 1,
            "series_index": 1,
            "series_size": 1,
        }
        for entry_id in (3, 4)
    ]
    assert series_rows_settled(
        stage,
        one_real_group,
        matches.get,
        game_spec=registry.get("holdem"),
        all_pairings=one_real_group + extra_byes,
        expected_entry_ids={1, 2, 3, 4},
        expected_swiss_rounds=1,
    ) is False


def test_swiss_malformed_round_coordinate_fails_closed_before_max():
    stage = {
        "key": "swiss",
        "type": "swiss",
        "games_per_pair": 2,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
    }
    pairings, matches = _complete_series_rows([(1, 2), (3, 4)])
    pairings[0]["round_num"] = "1"
    store = _TopologyStore(stage, pairings, matches)
    manager = ContestManager(store, None)

    assert asyncio.run(manager._maybe_next_swiss_round(7, 0, stage)) is False
    assert manager._stage_done(7, 0) is False
    assert manager._has_unfinished_pairings(7) is True


@pytest.mark.parametrize("field", ["series_index", "series_size"])
@pytest.mark.parametrize("value", [True, 1.0, "1", "bad", None])
def test_markerless_series_explicit_malformed_coordinates_fail_closed(
    field, value
):
    stage = {
        "key": "legacy-rr",
        "type": "round_robin",
        "games_per_pair": 1,
        "scoring": "poker_3_1_0",
    }
    pairings, matches = _complete_series_rows(
        [(1, 2)], games_per_pair=1
    )
    pairings[0][field] = value
    assert not series_rows_settled(
        stage,
        pairings,
        matches.get,
        game_spec=registry.get("holdem"),
    )

    # A markerless pre-coordinate row may omit the key entirely; that is the
    # only compatibility path which retains the historical one-row default.
    pairings[0].pop(field)
    assert series_rows_settled(
        stage,
        pairings,
        matches.get,
        game_spec=registry.get("holdem"),
    )


@pytest.mark.parametrize("field", ["series_index", "series_size"])
@pytest.mark.parametrize("value", [True, 1.0])
def test_strict_swiss_bye_requires_exact_integer_coordinates(field, value):
    stage = {
        "key": "swiss",
        "type": "swiss",
        "rounds": 1,
        "games_per_pair": 1,
        "series_scoring": SERIES_SCORING_INDEPENDENT,
        "scoring": "poker_3_1_0",
    }
    bye = {
        "entry_a_id": 1,
        "entry_b_id": None,
        "bot_a_id": 100,
        "bot_b_id": None,
        "status": "completed",
        "round_num": 1,
        "series_index": 1,
        "series_size": 1,
    }
    bye[field] = value
    assert not series_rows_settled(
        stage,
        [],
        lambda _match_id: None,
        game_spec=registry.get("holdem"),
        all_pairings=[bye],
        expected_entry_ids={1},
        expected_swiss_rounds=1,
    )
