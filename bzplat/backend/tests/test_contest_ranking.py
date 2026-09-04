"""预赛/决赛 P2：官方排名 + 破同分 + 导出测试。

验证：
1. compute_official_ranking：破同分链（points→buchholz_cut1→sonneborn→h2h→...）
2. 全员唯一连续 rank（1..N）
3. merge_replace_top：决赛合成榜（1..8 取 Top8，9..M 取 Stage1 未晋级）
4. 赛事 finished 后 official_results_ready=1 + list_official_results
5. /api/contests/{id}/official-results 导出 csv/json
"""
from __future__ import annotations

import asyncio
import csv
import io
import json

import pytest

from bzplat.backend.contests import ranking
from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.validation import ELIMINATION_TIEBREAK_PAIRED_SWAP
from bzplat.backend.store import Store


def _store(tmp_path):
    return Store(str(tmp_path / "p2.db"))


def _complete_tiebreaks(points: int | float, seed: int) -> dict:
    return {
        "points": points,
        "buchholz": 0,
        "buchholz_cut1": 0,
        "sonneborn_berger": 0,
        "head_to_head": 0,
        "normalized_delta": 0,
        "technical_losses": 0,
        "seed": seed,
    }


def _live_contest(stage: dict, *, current_stage_idx: int = 0) -> dict:
    return {
        "id": 7,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": current_stage_idx,
        "stages_json": [stage],
    }


def _live_entries(
    count: int,
    *,
    groups: dict[int, str] | None = None,
) -> list[dict]:
    return [
        {
            "id": entry_id,
            "bot_id": 99 + entry_id,
            "user_id": 999 + entry_id,
            "seed": entry_id,
            "group_id": (groups or {}).get(entry_id, ""),
            "eliminated": 0,
        }
        for entry_id in range(1, count + 1)
    ]


def _completed_live_pairing(
    pairing_id: int,
    entry_a_id: int,
    entry_b_id: int,
    *,
    winner: int | None,
    delta: int,
    round_num: int,
    group_id: str = "",
) -> dict:
    match_id = f"live-ranking-{pairing_id}"
    signed_delta = delta if winner == 0 else -delta
    return {
        "id": pairing_id,
        "contest_id": 7,
        "stage_idx": 0,
        "stage_key": "stage",
        "entry_a_id": entry_a_id,
        "entry_b_id": entry_b_id,
        "_raw_entry_a_id": entry_a_id,
        "_raw_entry_b_id": entry_b_id,
        "_entry_a_user_id": 999 + entry_a_id,
        "_entry_b_user_id": 999 + entry_b_id,
        "bot_a_id": 99 + entry_a_id,
        "bot_b_id": 99 + entry_b_id,
        "_pairing_bot_a_owner_id": 999 + entry_a_id,
        "_pairing_bot_b_owner_id": 999 + entry_b_id,
        "match_id": match_id,
        "status": "running",
        "match_status": "completed",
        "match_winner": winner,
        "_match_result_json": {
            "rounds_played": 70,
            "deltas": [signed_delta, -signed_delta],
        },
        "_match_config_json": {"duplicate": False},
        "_match_technical_loss": 0,
        "_match_contest_id": 7,
        "_match_game_id": "holdem",
        "_match_type": "contest",
        "_match_bot_a_id": 99 + entry_a_id,
        "_match_bot_b_id": 99 + entry_b_id,
        "series_index": 1,
        "series_size": 1,
        "round_num": round_num,
        "group_id": group_id,
    }


def _scheduled_group_pairing(
    pairing_id: int,
    entry_a_id: int,
    entry_b_id: int,
    *,
    round_num: int,
    group_id: str,
) -> dict:
    return {
        "id": pairing_id,
        "contest_id": 7,
        "stage_idx": 0,
        "stage_key": "stage",
        "entry_a_id": entry_a_id,
        "entry_b_id": entry_b_id,
        "_raw_entry_a_id": entry_a_id,
        "_raw_entry_b_id": entry_b_id,
        "_entry_a_user_id": 999 + entry_a_id,
        "_entry_b_user_id": 999 + entry_b_id,
        "bot_a_id": 99 + entry_a_id,
        "bot_b_id": 99 + entry_b_id,
        "_pairing_bot_a_owner_id": 999 + entry_a_id,
        "_pairing_bot_b_owner_id": 999 + entry_b_id,
        "match_id": None,
        "status": "pending",
        "round_num": round_num,
        "group_id": group_id,
    }


@pytest.mark.parametrize("stage_type", ["round_robin", "swiss"])
def test_live_holdem_tie_uses_same_full_chain_as_official(stage_type):
    """Live RR/Swiss must not preview a different qualifier by raw delta."""
    stage = {
        "key": "stage",
        "type": stage_type,
        "scoring": "poker_3_1_0",
        **({"rounds": 2} if stage_type == "swiss" else {}),
    }
    entries = _live_entries(4)
    pairings = [
        _completed_live_pairing(1, 1, 4, winner=0, delta=100, round_num=1),
        _completed_live_pairing(2, 2, 3, winner=0, delta=1, round_num=1),
        _completed_live_pairing(3, 3, 1, winner=0, delta=10, round_num=2),
    ]

    rows = ContestManager(None, None).standings(
        7,
        contest=_live_contest(stage),
        entries=entries,
        pairings=pairings,
    )

    # Entries 1/2/3 all have three points.  Raw delta prefers 1, but the
    # authoritative Cut1/SB chain prefers 3 then 2, exactly as finalize does.
    assert [row["entry_id"] for row in rows] == [3, 2, 1, 4]
    assert [row["rank"] for row in rows] == [1, 2, 3, 4]
    assert all(row["tiebreaks"] == _complete_tiebreaks(
        row["points"], row["seed"]
    ) or set(row["tiebreaks"]) == set(_complete_tiebreaks(0, 0)) for row in rows)
    assert rows[0]["tiebreaks"]["buchholz_cut1"] > rows[2]["tiebreaks"]["buchholz_cut1"]


def test_live_traditional_groups_rank_only_inside_frozen_roster_group():
    stage = {
        "key": "stage",
        "type": "group_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    groups = {1: "A", 2: "A", 3: "B", 4: "B"}
    pairings = [
        _completed_live_pairing(
            1, 1, 2, winner=0, delta=1, round_num=1, group_id="A"
        ),
        _completed_live_pairing(
            2, 3, 4, winner=0, delta=100, round_num=1, group_id="B"
        ),
    ]

    rows = ContestManager(None, None).standings(
        7,
        contest=_live_contest(stage),
        entries=_live_entries(4, groups=groups),
        pairings=pairings,
    )

    assert [
        (row["group_id"], row["rank"], row["entry_id"])
        for row in rows
    ] == [("A", 1, 1), ("A", 2, 2), ("B", 1, 3), ("B", 2, 4)]
    assert all(set(row["tiebreaks"]) == set(_complete_tiebreaks(0, 0)) for row in rows)


def test_live_traditional_groups_with_no_results_rank_by_seed_per_group():
    stage = {
        "key": "stage",
        "type": "group_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    groups = {1: "A", 2: "A", 3: "B", 4: "B"}
    entries = _live_entries(4, groups=groups)
    seeds = {1: 8, 2: 2, 3: 7, 4: 1}
    for entry in entries:
        entry["seed"] = seeds[entry["id"]]
    pairings = [
        _scheduled_group_pairing(
            1, 1, 2, round_num=1, group_id="A"
        ),
        _scheduled_group_pairing(
            2, 3, 4, round_num=1, group_id="B"
        ),
    ]

    rows = ContestManager(None, None).standings(
        7,
        contest=_live_contest(stage),
        entries=entries,
        pairings=pairings,
    )

    assert [
        (row["group_id"], row["rank"], row["entry_id"])
        for row in rows
    ] == [("A", 1, 2), ("A", 2, 1), ("B", 1, 4), ("B", 2, 3)]
    assert all(row["points"] == 0 for row in rows)
    assert all(
        row["tiebreaks"]["seed"] == seeds[row["entry_id"]]
        and set(row["tiebreaks"]) == set(_complete_tiebreaks(0, 0))
        for row in rows
    )


def test_live_traditional_group_result_does_not_reorder_zero_result_group():
    stage = {
        "key": "stage",
        "type": "group_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    groups = {1: "A", 2: "A", 3: "B", 4: "B"}
    entries = _live_entries(4, groups=groups)
    entries[2]["seed"] = 9
    entries[3]["seed"] = 1
    pairings = [
        _completed_live_pairing(
            1, 1, 2, winner=0, delta=100, round_num=1, group_id="A"
        ),
        _scheduled_group_pairing(
            2, 3, 4, round_num=1, group_id="B"
        ),
    ]

    rows = ContestManager(None, None).standings(
        7,
        contest=_live_contest(stage),
        entries=entries,
        pairings=pairings,
    )

    assert [
        (row["group_id"], row["rank"], row["entry_id"], row["points"])
        for row in rows
    ] == [
        ("A", 1, 1, 3.0),
        ("A", 2, 2, 0.0),
        ("B", 1, 4, 0.0),
        ("B", 2, 3, 0.0),
    ]


@pytest.mark.parametrize("malformed_seed", ["bad", True, -1])
def test_live_standings_reject_malformed_entry_seed(malformed_seed):
    stage = {
        "key": "stage",
        "type": "group_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    groups = {1: "A", 2: "A", 3: "B", 4: "B"}
    entries = _live_entries(4, groups=groups)
    entries[0]["seed"] = malformed_seed
    pairings = [
        _scheduled_group_pairing(
            1, 1, 2, round_num=1, group_id="A"
        ),
        _scheduled_group_pairing(
            2, 3, 4, round_num=1, group_id="B"
        ),
    ]

    assert ContestManager(None, None).standings(
        7,
        contest=_live_contest(stage),
        entries=entries,
        pairings=pairings,
    ) == []


def test_live_group_drr_rejects_two_legs_with_same_seat_direction():
    stage = {
        "key": "stage",
        "type": "group_double_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    groups = {1: "A", 2: "A", 3: "B", 4: "B"}
    pairings = [
        _completed_live_pairing(
            1, 1, 2, winner=0, delta=1, round_num=1, group_id="A"
        ),
        _completed_live_pairing(
            2, 1, 2, winner=0, delta=1, round_num=2, group_id="A"
        ),
        _completed_live_pairing(
            3, 3, 4, winner=0, delta=1, round_num=1, group_id="B"
        ),
        _completed_live_pairing(
            4, 4, 3, winner=0, delta=1, round_num=2, group_id="B"
        ),
    ]

    assert ContestManager(None, None).standings(
        7,
        contest=_live_contest(stage),
        entries=_live_entries(4, groups=groups),
        pairings=pairings,
    ) == []


@pytest.mark.parametrize(
    "groups,pairing_group",
    [
        ({1: "A", 2: "", 3: "B", 4: "B"}, "A"),
        ({1: "A", 2: "A", 3: "B", 4: "B"}, "B"),
    ],
)
def test_live_traditional_groups_fail_closed_on_partial_or_conflicting_authority(
    groups,
    pairing_group,
):
    stage = {
        "key": "stage",
        "type": "group_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    pairings = [
        _completed_live_pairing(
            1, 1, 2, winner=0, delta=1, round_num=1, group_id=pairing_group
        ),
        _completed_live_pairing(
            2, 3, 4, winner=0, delta=1, round_num=1, group_id="B"
        ),
    ]

    assert ContestManager(None, None).standings(
        7,
        contest=_live_contest(stage),
        entries=_live_entries(4, groups=groups),
        pairings=pairings,
    ) == []


def test_current_later_swiss_keeps_active_bye_without_a_match_pairing(tmp_path):
    """A proven later-stage cohort still includes its pairing-free Swiss bye."""
    store = _store(tmp_path)
    users = [
        store.create_user(
            f"later-stage-u{index}",
            f"later-stage-u{index}@example.com",
            "hash",
            role="organizer" if index == 0 else "user",
        )
        for index in range(3)
    ]
    bots = []
    for index, user in enumerate(users):
        binary = tmp_path / f"later-stage-{index}.bin"
        binary.write_bytes(b"test fixture")
        bots.append(
            store.create_bot(
                user["id"],
                f"later-stage-b{index}",
                binary_path=str(binary),
                format="elf",
                game_id="holdem",
            )
        )
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
        },
        {
            "key": "final",
            "type": "swiss",
            "rounds": 1,
            "scoring": "poker_3_1_0",
        },
    ]
    contest = store.create_contest(
        "Proven later-stage bye",
        users[0]["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps(stages),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots, strict=True)
    ]
    manager = ContestManager(store, None)
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    for index, pairing in enumerate(
        store.list_contest_pairings(contest["id"], stage_idx=0), start=1
    ):
        match_id = f"later-stage-qualifier-{index}"
        store.create_match(
            match_id,
            pairing["bot_a_id"],
            pairing["bot_b_id"],
            owner_id=users[0]["id"],
            contest_id=contest["id"],
            match_type="contest",
            game_id="holdem",
        )
        store.bind_contest_pairing_match(
            contest["id"],
            pairing["id"],
            match_id,
            require_execution_admission=False,
        )
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            result={"rounds_played": 70, "deltas": [10, -10]},
        )
        store.complete_contest_pairing_for_match(contest["id"], match_id)

    ranked, decision_revision, _decision_entries, source_groups = (
        manager._ensure_stage_decision(contest["id"], 0)
    )
    projected, entry_updates = manager._plan_participant_advancement(
        contest["id"], 0, ranked_rows=ranked
    )
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            1,
            dispatch_pending=False,
            entry_rows=projected,
            entry_updates=entry_updates,
            source_decision_revision=decision_revision,
            source_stage_groups=source_groups,
        )
    )
    current_pairings = store.list_contest_pairings(contest["id"], stage_idx=1)
    bye = next(pairing for pairing in current_pairings if pairing["bot_b_id"] is None)
    assert store.contest_stage_manifest_is_valid(contest["id"], 1)
    current = store.get_contest(contest["id"])
    assert manager._active_current_stage_authority(current, stages) is not None

    rows = manager.standings(contest["id"], stage_idx=1)
    by_entry = {row["entry_id"]: row for row in rows}
    assert set(by_entry) == {entry["id"] for entry in entries}
    bye_row = by_entry[bye["entry_a_id"]]
    assert bye_row["counts"]["match_jobs"] == 0
    assert bye_row["counts"]["unique_opponents"] == 0
    assert bye_row["byes"] == 1
    assert [row["rank"] for row in rows] == [1, 2, 3]
    store.close()


def test_current_later_stage_without_predecessor_authority_is_empty():
    contest = {
        "id": 7,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 1,
        "stages_json": [
            {"key": "qualifier", "type": "round_robin"},
            {"key": "final", "type": "round_robin"},
        ],
    }
    pairing = _completed_live_pairing(
        1, 1, 2, winner=0, delta=10, round_num=1
    )
    pairing.update(stage_idx=1, stage_key="final")

    assert ContestManager(None, None).standings(
        7,
        stage_idx=1,
        contest=contest,
        entries=_live_entries(4),
        pairings=[pairing],
    ) == []


def test_past_stage_cohort_is_not_shrunk_to_current_active_entries():
    contest = {
        "id": 7,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 2,
        "stages_json": [
            {
                "key": "qualifier",
                "type": "round_robin",
                "scoring": "poker_3_1_0",
            },
            {
                "key": "semifinal",
                "type": "round_robin",
                "scoring": "poker_3_1_0",
            },
            {
                "key": "final",
                "type": "round_robin",
                "scoring": "poker_3_1_0",
            },
        ],
    }
    entries = _live_entries(4)
    entries[0]["eliminated"] = 1
    entries[1]["eliminated"] = 1
    pairing = _completed_live_pairing(
        1, 1, 2, winner=0, delta=10, round_num=1
    )
    pairing.update(stage_idx=1, stage_key="semifinal")

    rows = ContestManager(None, None).standings(
        7,
        stage_idx=1,
        contest=contest,
        entries=entries,
        pairings=[pairing],
    )

    assert [row["entry_id"] for row in rows] == [1, 2]
    assert rows[0]["points"] == 3
    assert rows[0]["eliminated"] == 1


def test_live_paired_swap_ko_orders_champion_by_bracket_progress():
    stage = {
        "key": "stage",
        "type": "single_elimination",
        "tiebreak": ELIMINATION_TIEBREAK_PAIRED_SWAP,
        "scoring": "poker_3_1_0",
    }
    first_round = _completed_live_pairing(
        2, 2, 3, winner=0, delta=100, round_num=1
    )
    first_round["bracket_slot"] = 1
    final = _completed_live_pairing(
        3, 1, 2, winner=None, delta=0, round_num=2
    )
    final.update(
        bracket_slot=0,
        bot_a_version_id=501,
        bot_b_version_id=502,
    )
    final["_match_config_json"].update(
        _bot_a_version_id=501,
        _bot_b_version_id=502,
    )
    deciding_first = _completed_live_pairing(
        4, 1, 2, winner=0, delta=1, round_num=2
    )
    deciding_first.update(
        bracket_slot=0,
        tiebreak_group=1,
        tiebreak_game=1,
        pairing_seed=77,
        bot_a_version_id=501,
        bot_b_version_id=502,
    )
    deciding_first["_match_config_json"].update(
        _bot_a_version_id=501,
        _bot_b_version_id=502,
    )
    deciding_second = _completed_live_pairing(
        5, 2, 1, winner=1, delta=1, round_num=2
    )
    deciding_second.update(
        bracket_slot=0,
        tiebreak_group=1,
        tiebreak_game=2,
        pairing_seed=77,
        bot_a_version_id=502,
        bot_b_version_id=501,
    )
    deciding_second["_match_config_json"].update(
        _bot_a_version_id=502,
        _bot_b_version_id=501,
    )
    bye = {
        "id": 1,
        "contest_id": 7,
        "stage_idx": 0,
        "stage_key": "stage",
        "entry_a_id": 1,
        "entry_b_id": None,
        "_raw_entry_a_id": 1,
        "_raw_entry_b_id": None,
        "_entry_a_user_id": 1000,
        "_entry_b_user_id": None,
        "bot_a_id": 100,
        "bot_b_id": None,
        "_pairing_bot_a_owner_id": 1000,
        "_pairing_bot_b_owner_id": None,
        "match_id": None,
        "status": "completed",
        "round_num": 1,
        "bracket_slot": 0,
        "tiebreak_group": 0,
        "tiebreak_game": 0,
    }

    rows = ContestManager(None, None).standings(
        7,
        contest=_live_contest(stage),
        entries=_live_entries(3),
        pairings=[
            bye,
            first_round,
            final,
            deciding_first,
            deciding_second,
        ],
    )

    # Entry 2 has four primary-stage points while entry 1 has only the drawn
    # final's one point, but entry 1 won the non-scoring paired-swap decider.
    # Live and the terminal snapshot must agree that bracket progress wins.
    assert [row["entry_id"] for row in rows] == [1, 2, 3]
    assert [row["rank"] for row in rows] == [1, 2, 3]


def test_compute_ranking_unique_continuous_ranks():
    """rank 唯一连续 1..N（无并列）。"""
    standings = [
        {"entry_id": 1, "bot_id": 10, "user_id": 100, "points": 6.0, "delta_total": 100, "seed": 1},
        {"entry_id": 2, "bot_id": 20, "user_id": 200, "points": 6.0, "delta_total": 50, "seed": 2},
        {"entry_id": 3, "bot_id": 30, "user_id": 300, "points": 3.0, "delta_total": 0, "seed": 3},
    ]
    rows = ranking.compute_official_ranking(standings, [], {})
    ranks = [r["rank"] for r in rows]
    assert ranks == [1, 2, 3], f"rank 应唯一连续 1..N，实际 {ranks}"


def test_tiebreak_buchholz_breaks_tie():
    """同分时按 buchholz 破同分（对手强者排前）。

    4 个 entry：e1/e2 同分=6（争冠），e3=3（中），e4=0（弱）。
    e1 只打了 e4（弱）→ buchholz 低；e2 打了 e3（中）→ buchholz 高。
    """
    standings = [
        {"entry_id": 1, "bot_id": 10, "user_id": 100, "points": 6.0, "delta_total": 0, "seed": 1},
        {"entry_id": 2, "bot_id": 20, "user_id": 200, "points": 6.0, "delta_total": 0, "seed": 2},
        {"entry_id": 3, "bot_id": 30, "user_id": 300, "points": 3.0, "delta_total": 0, "seed": 3},
        {"entry_id": 4, "bot_id": 40, "user_id": 400, "points": 0.0, "delta_total": 0, "seed": 4},
    ]
    # e1 只打 e4（弱）；e2 只打 e3（中）—— e2 对手分更高
    pairings = [
        {"entry_a_id": 1, "entry_b_id": 4, "match_id": "m1", "bot_a_id": 10, "bot_b_id": 40},
        {"entry_a_id": 2, "entry_b_id": 3, "match_id": "m2", "bot_a_id": 20, "bot_b_id": 30},
    ]
    matches = {
        "m1": {"status": "completed", "winner": 0, "result": {"deltas": [100, -100]}},
        "m2": {"status": "completed", "winner": 0, "result": {"deltas": [100, -100]}},
    }
    rows = ranking.compute_official_ranking(standings, pairings, matches)
    by_entry = {r["entry_id"]: r for r in rows}
    # e1 对手 e4(0) → buchholz=0；e2 对手 e3(3) → buchholz=3
    assert by_entry[2]["tiebreaks"]["buchholz"] > by_entry[1]["tiebreaks"]["buchholz"]
    assert by_entry[2]["rank"] == 1, "buchholz 高者应排前（同分时对手强者排前）"
    assert by_entry[1]["rank"] == 2


def test_tiebreak_fewer_technical_losses_ranks_first():
    """其余破同分项相同时，从 pairing+winner 识别的技术负更少者优先。"""
    standings = [
        {"entry_id": 1, "bot_id": 10, "user_id": 100, "points": 3.0,
         "delta_total": 0, "seed": 1},
        {"entry_id": 2, "bot_id": 20, "user_id": 200, "points": 3.0,
         "delta_total": 0, "seed": 2},
        {"entry_id": 3, "bot_id": 30, "user_id": 300, "points": 0.0,
         "delta_total": 0, "seed": 3},
        {"entry_id": 4, "bot_id": 40, "user_id": 400, "points": 0.0,
         "delta_total": 0, "seed": 4},
    ]
    pairings = [
        {"entry_a_id": 1, "entry_b_id": 3, "match_id": "technical"},
        {"entry_a_id": 2, "entry_b_id": 4, "match_id": "normal"},
    ]
    matches = {
        "technical": {
            "status": "completed", "winner": 1, "technical_loss": 1,
            "result": {"rounds_played": 0, "deltas": [-1, 1]},
        },
        "normal": {
            "status": "completed", "winner": 1, "technical_loss": 0,
            "result": {"deltas": [-1, 1]},
        },
    }

    rows = ranking.compute_official_ranking(standings, pairings, matches)
    by_entry = {row["entry_id"]: row for row in rows}
    assert by_entry[1]["tiebreaks"]["technical_losses"] == 1
    assert by_entry[2]["tiebreaks"]["technical_losses"] == 0
    assert by_entry[2]["rank"] < by_entry[1]["rank"]


def test_merge_replace_top():
    """决赛合成榜：1..scope 取 stage2，scope+1..N 取 stage1 未晋级。"""
    stage1 = [
        {"entry_id": i, "rank": i, "bot_id": i * 10} for i in range(1, 9)  # 8 人
    ]
    stage2 = [
        {"entry_id": 1, "rank": 1, "bot_id": 10},
        {"entry_id": 3, "rank": 2, "bot_id": 30},
        {"entry_id": 5, "rank": 3, "bot_id": 50},
        {"entry_id": 7, "rank": 4, "bot_id": 70},
        {"entry_id": 2, "rank": 5, "bot_id": 20},
        {"entry_id": 4, "rank": 6, "bot_id": 40},
        {"entry_id": 6, "rank": 7, "bot_id": 60},
        {"entry_id": 8, "rank": 8, "bot_id": 80},
    ]
    merged = ranking.merge_replace_top(stage1, stage2, scope=8)
    # 全是 Top8，scope=8 → stage2 全取，stage1 全晋级故 rest 空
    assert len(merged) == 8
    ranks = [r["rank"] for r in merged]
    assert ranks == [1, 2, 3, 4, 5, 6, 7, 8]
    # stage2 的第 1 名（entry1）应在 merged 第 1
    assert merged[0]["entry_id"] == 1


def test_persist_and_list_official_results(tmp_path):
    """落库 + list_official_results 按 rank 升序。"""
    s = _store(tmp_path)
    u1 = s.create_user("org", "o@e.com", "x", role="organizer")["id"]
    u2 = s.create_user("entrant", "entrant@e.com", "x")["id"]
    b1 = s.create_bot(u1, "rb1", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    b2 = s.create_bot(u2, "rb2", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    c = s.create_contest("P2持久", organizer_id=u1, game_id="holdem")["id"]
    e1 = s.add_contest_entry(c, u1, b1)
    e2 = s.add_contest_entry(c, u2, b2)
    ranking.persist_official_results(
        s, c,
        [
            {"entry_id": e1["id"], "rank": 2, "bot_id": b1, "user_id": u1,
             "tiebreaks": _complete_tiebreaks(3, 1)},
            {"entry_id": e2["id"], "rank": 1, "bot_id": b2, "user_id": u2,
             "tiebreaks": _complete_tiebreaks(6, 2)},
        ],
    )
    rows = s.list_official_results(c)
    assert [r["rank"] for r in rows] == [1, 2], "应按 rank 升序"
    assert int(s.get_contest(c)["official_results_ready"]) == 1
    s.close()


def test_official_results_endpoint_csv_json(tmp_path):
    """/api/contests/{id}/official-results 导出 csv + json。"""
    from bzplat.backend.crypto import hash_password
    from fastapi.testclient import TestClient

    from bzplat.backend.main import create_app

    app = create_app(db_path=str(tmp_path / "app.db"))
    store = app.state.store
    o = store.create_user("org2", "o2@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(o["id"], email_verified=1)
    entrant = store.create_user(
        "ranking-entrant", "ranking-entrant@example.com", hash_password("pw123456")
    )
    b = store.create_bot(o["id"], "rb", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    b2 = store.create_bot(
        entrant["id"], "rb2", binary_path="/tmp", format="elf", game_id="holdem"
    )["id"]
    c = store.create_contest("P2导出", organizer_id=o["id"], game_id="holdem")["id"]
    e1 = store.add_contest_entry(c, o["id"], b)
    e2 = store.add_contest_entry(c, entrant["id"], b2)
    ranking.persist_official_results(
        store, c,
        [
            {"entry_id": e1["id"], "rank": 1, "bot_id": b, "user_id": o["id"],
             "tiebreaks": {
                 "points": 6, "buchholz": 5, "buchholz_cut1": 3,
                 "sonneborn_berger": 3, "head_to_head": 1,
                 "normalized_delta": 2, "technical_losses": 0, "seed": 1,
             }},
            {"entry_id": e2["id"], "rank": 2, "bot_id": b2, "user_id": entrant["id"],
             "tiebreaks": {
                 "points": 6, "buchholz": 4, "buchholz_cut1": 2,
                 "sonneborn_berger": 2, "head_to_head": 0,
                 "normalized_delta": -2, "technical_losses": 0, "seed": 2,
             }},
        ],
    )
    client = TestClient(app)
    r = client.get(f"/api/contests/{c}/official-results?format=json")
    assert r.status_code == 200
    data = r.json()
    assert data["ready"] is True
    assert len(data["results"]) == 2
    assert [row["rank"] for row in data["results"]] == [1, 2]
    assert [row["overall_rank"] for row in data["results"]] == [1, 2]
    assert [row["group_id"] for row in data["results"]] == ["", ""]
    assert [row["rank_in_group"] for row in data["results"]] == [None, None]
    assert data["results"][0]["points"] == data["results"][1]["points"] == 6
    assert data["results"][0]["tiebreaks"] == {
        "points": 6,
        "buchholz": 5,
        "buchholz_cut1": 3,
        "sonneborn_berger": 3,
        "head_to_head": 1,
        "normalized_delta": 2,
        "technical_losses": 0,
        "seed": 1,
    }
    assert "tiebreaks_json" not in data["results"][0]
    r2 = client.get(f"/api/contests/{c}/official-results?format=csv")
    assert r2.status_code == 200
    assert "text/csv" in r2.headers.get("content-type", "")
    assert "rank" in r2.text


def test_replace_top_endpoint_preserves_comparable_ranking_cohorts(tmp_path):
    """跨阶段相同积分不能被公开成同一组破同分依据。"""
    from bzplat.backend.crypto import hash_password
    from fastapi.testclient import TestClient

    from bzplat.backend.main import create_app

    app = create_app(db_path=str(tmp_path / "cohorts.db"))
    store = app.state.store
    owner = store.create_user(
        "cohort_org", "cohort@example.com", hash_password("pw123456"),
        role="organizer",
    )
    store.update_user(owner["id"], email_verified=1)
    entrant = store.create_user(
        "cohort_entrant", "cohort-entrant@example.com", hash_password("pw123456")
    )
    owner_bot = store.create_bot(
        owner["id"], "cohort-owner-bot", binary_path="/tmp", format="elf", game_id="holdem"
    )
    entrant_bot = store.create_bot(
        entrant["id"], "cohort-entrant-bot", binary_path="/tmp", format="elf", game_id="holdem"
    )
    contest = store.create_contest(
        "分阶段正式榜",
        organizer_id=owner["id"],
        template_id="holdem_final_ranked",
        game_id="holdem",
        current_stage_idx=1,
        status="finished",
        stages_json='[{"type":"round_robin"},{"type":"double_round_robin",'
                    '"ranking_mode":"replace_top","ranking_scope":1}]',
    )
    c_id = contest["id"]
    owner_entry = store.add_contest_entry(c_id, owner["id"], owner_bot["id"])
    entrant_entry = store.add_contest_entry(
        c_id, entrant["id"], entrant_bot["id"]
    )
    store.upsert_official_result(
        c_id, owner_entry["id"], 1, stage_idx=1, points=6,
        bot_id=owner_bot["id"], user_id=owner["id"],
        tiebreaks_json='{"points":6,"buchholz_cut1":9}',
    )
    store.upsert_official_result(
        c_id, entrant_entry["id"], 2, stage_idx=1, points=6,
        bot_id=entrant_bot["id"], user_id=entrant["id"],
        tiebreaks_json='{"points":6,"buchholz_cut1":2}',
    )
    # 只留下一个决赛成员，刻意覆盖“阶段快照部分存在”的旧数据边界；
    # 来源判定必须同时满足末阶段成员身份与 Top-N 合榜边界。
    store.upsert_stage_result(c_id, 1, owner_entry["id"], points=6)
    store.update_contest(c_id, official_results_ready=1)

    response = TestClient(app).get(f"/api/contests/{c_id}/official-results")
    assert response.status_code == 200
    results = response.json()["results"]
    assert [(row["source_stage"], row["ranking_cohort"]) for row in results] == [
        (1, "stage:1"),
        (0, "stage:0"),
    ]
    csv_response = TestClient(app).get(
        f"/api/contests/{c_id}/official-results?format=csv"
    )
    assert csv_response.status_code == 200
    lines = csv_response.text.splitlines()
    assert lines[0].endswith("source_stage,ranking_cohort")
    assert lines[1].endswith("1,stage:1")
    assert lines[2].endswith("0,stage:0")


def test_replace_top_membership_does_not_override_scope_boundary():
    """参加过末阶段但落在 Top-N 外的选手仍沿用前阶段正式排名。"""
    from bzplat.backend.contests.ranking import with_official_result_provenance

    contest = {
        "current_stage_idx": 1,
        "stages_json": '[{"type":"swiss"},{"type":"double_round_robin",'
                       '"ranking_mode":"replace_top","ranking_scope":2}]',
    }
    rows = with_official_result_provenance(
        contest,
        [{"entry_id": 101, "rank": 8, "stage_idx": 1, "points": 6}],
        stage_entry_ids={1: {101}},
    )
    assert rows[0]["source_stage"] == 0
    assert rows[0]["ranking_cohort"] == "stage:0"


def test_official_results_not_ready_returns_409(tmp_path):
    """赛事未 finished/未落库 → 409。"""
    from bzplat.backend.crypto import hash_password
    from fastapi.testclient import TestClient

    from bzplat.backend.main import create_app

    app = create_app(db_path=str(tmp_path / "app2.db"))
    store = app.state.store
    o = store.create_user("org3", "o3@ex.com", hash_password("pw123456"), role="organizer")
    store.update_user(o["id"], email_verified=1)
    c = store.create_contest("P2未就绪", organizer_id=o["id"], game_id="holdem")["id"]
    client = TestClient(app)
    r = client.get(f"/api/contests/{c}/official-results")
    assert r.status_code == 409


def test_replace_official_results_validates_complete_roster_atomically(tmp_path):
    store = _store(tmp_path)
    owner = store.create_user("atomic-owner", "atomic-owner@example.com", "hash")
    entrant = store.create_user(
        "atomic-entrant", "atomic-entrant@example.com", "hash"
    )
    owner_bot = store.create_bot(
        owner["id"], "atomic-owner-bot", binary_path="/tmp", format="elf",
        game_id="holdem",
    )
    entrant_bot = store.create_bot(
        entrant["id"], "atomic-entrant-bot", binary_path="/tmp", format="elf",
        game_id="holdem",
    )
    contest = store.create_contest(
        "atomic official table", owner["id"], game_id="holdem"
    )
    owner_entry = store.add_contest_entry(
        contest["id"], owner["id"], owner_bot["id"]
    )
    entrant_entry = store.add_contest_entry(
        contest["id"], entrant["id"], entrant_bot["id"]
    )
    valid = [
        {
            "entry_id": owner_entry["id"],
            "rank": 1,
            "points": 3,
            "bot_id": owner_bot["id"],
            "user_id": owner["id"],
            "tiebreaks_json": json.dumps(_complete_tiebreaks(3, 1)),
        },
        {
            "entry_id": entrant_entry["id"],
            "rank": 2,
            "points": 0,
            "bot_id": entrant_bot["id"],
            "user_id": entrant["id"],
            "tiebreaks_json": json.dumps(_complete_tiebreaks(0, 2)),
        },
    ]
    store.replace_official_results(contest["id"], valid)
    invalid_batches = (
        [{**valid[0]}, {**valid[1], "rank": 3}],
        [{**valid[0]}, {**valid[1], "rank": 1}],
        [{**valid[0]}],
        [{**valid[0]}, {**valid[1], "entry_id": 999_999}],
        [{**valid[0], "user_id": entrant["id"]}, {**valid[1]}],
        [{**valid[0], "bot_id": entrant_bot["id"]}, {**valid[1]}],
    )
    for invalid in invalid_batches:
        with pytest.raises(ValueError):
            store.replace_official_results(contest["id"], invalid)
        assert [
            (row["entry_id"], row["rank"])
            for row in store.list_official_results(contest["id"])
        ] == [
            (owner_entry["id"], 1),
            (entrant_entry["id"], 2),
        ]
    store.close()


def test_replace_official_results_empty_table_requires_empty_roster(tmp_path):
    store = _store(tmp_path)
    owner = store.create_user(
        "empty-owner", "empty-owner@example.com", "hash", role="organizer"
    )
    empty = store.create_contest(
        "empty official roster", owner["id"], game_id="holdem"
    )
    store.replace_official_results(empty["id"], [])
    assert store.get_contest(empty["id"])["official_results_ready"] == 1
    assert store.list_official_results(empty["id"]) == []

    entrant = store.create_user(
        "nonempty-entrant", "nonempty-entrant@example.com", "hash"
    )
    bot = store.create_bot(
        entrant["id"], "nonempty-bot", binary_path="/tmp", format="elf",
        game_id="holdem",
    )
    nonempty = store.create_contest(
        "nonempty official roster", owner["id"], game_id="holdem"
    )
    store.add_contest_entry(nonempty["id"], entrant["id"], bot["id"])
    with pytest.raises(ValueError, match="名册成员不一致"):
        store.replace_official_results(nonempty["id"], [])
    assert store.get_contest(nonempty["id"])["official_results_ready"] == 0
    assert store.list_official_results(nonempty["id"]) == []
    store.close()


def test_damaged_ready_results_return_bounded_409_and_legacy_ties_stay_null(
    tmp_path,
):
    from bzplat.backend.crypto import hash_password
    from fastapi.testclient import TestClient

    from bzplat.backend.main import create_app

    app = create_app(db_path=str(tmp_path / "damaged-ready.db"))
    store = app.state.store
    owner = store.create_user(
        "damaged-owner", "damaged-owner@example.com", hash_password("pw123456")
    )
    entrant = store.create_user(
        "damaged-entrant", "damaged-entrant@example.com", hash_password("pw123456")
    )
    owner_bot = store.create_bot(
        owner["id"], "damaged-owner-bot", binary_path="/tmp", format="elf",
        game_id="holdem",
    )
    entrant_bot = store.create_bot(
        entrant["id"], "damaged-entrant-bot", binary_path="/tmp", format="elf",
        game_id="holdem",
    )
    contest = store.create_contest(
        "damaged ready", owner["id"], game_id="holdem", status="finished"
    )
    entries = [
        store.add_contest_entry(contest["id"], owner["id"], owner_bot["id"]),
        store.add_contest_entry(
            contest["id"], entrant["id"], entrant_bot["id"]
        ),
    ]
    store.replace_official_results(
        contest["id"],
        [
            {
                "entry_id": entry["id"],
                "rank": rank,
                "points": 0,
                "bot_id": entry["bot_id"],
                "user_id": entry["user_id"],
                "tiebreaks_json": "{}",
            }
            for rank, entry in enumerate(entries, start=1)
        ],
    )
    client = TestClient(app)
    legacy_json = client.get(
        f"/api/contests/{contest['id']}/official-results"
    )
    legacy_csv = client.get(
        f"/api/contests/{contest['id']}/official-results?format=csv"
    )
    assert legacy_json.status_code == legacy_csv.status_code == 200
    assert [row["tiebreaks"] for row in legacy_json.json()["results"]] == [
        None,
        None,
    ]
    csv_rows = list(csv.DictReader(io.StringIO(legacy_csv.text)))
    assert [row["buchholz"] for row in csv_rows] == ["", ""]

    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_official_results SET rank=3 "
            "WHERE contest_id=? AND rank=2",
            (contest["id"],),
        )
    with pytest.raises(ValueError, match="从 1 连续"):
        store.list_official_results(contest["id"])
    damaged_json = client.get(
        f"/api/contests/{contest['id']}/official-results"
    )
    damaged_csv = client.get(
        f"/api/contests/{contest['id']}/official-results?format=csv"
    )
    assert damaged_json.status_code == damaged_csv.status_code == 409
    assert damaged_json.json() == damaged_csv.json() == {
        "detail": "正式名次数据损坏，暂不可用"
    }
    assert "text/csv" not in damaged_csv.headers.get("content-type", "")
    store.close()
