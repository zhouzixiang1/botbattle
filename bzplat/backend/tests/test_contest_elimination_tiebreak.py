"""Paired-seat elimination tiebreak lifecycle and persistence contracts."""
from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.api_routes import _public_contest_pairings
from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.presentation import build_stage_summaries
from bzplat.backend.contests.series import summarize_elimination_encounter
from bzplat.backend.contests.stages import effective_swiss_rounds
from bzplat.backend.contests.validation import (
    ELIMINATION_TIEBREAK_PAIRED_SWAP,
    stage_scoring_contract_is_valid,
    validate_stage,
)
from bzplat.backend.games import registry as game_registry
from bzplat.backend.main import create_app
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store
from bzplat.backend.tests.execution_helpers import claim_request, enable_execution_queue


STAGE = {
    "key": "ko",
    "type": "single_elimination",
    "scoring": "poker_3_1_0",
    "tiebreak": ELIMINATION_TIEBREAK_PAIRED_SWAP,
    "rest_after_minutes": 0,
}


class _RecordingOrchestrator:
    """Legacy prepared-Match double which records the internal seed contract."""

    max_concurrent = 32

    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls: list[dict] = []

    async def challenge(
        self,
        bot_a_id: int,
        bot_b_id: int,
        owner_user_id: int,
        *,
        match_type: str = "contest",
        contest_id: int | None = None,
        game_id: str | None = None,
        **kwargs,
    ) -> str:
        call = {
            "bot_a_id": bot_a_id,
            "bot_b_id": bot_b_id,
            "owner_user_id": owner_user_id,
            "contest_id": contest_id,
            "game_id": game_id,
            **kwargs,
        }
        self.calls.append(call)
        match_id = f"elim-tiebreak-{contest_id}-{len(self.calls)}"
        match_config: dict[str, object] = {"duplicate": False}
        for suffix in ("a", "b"):
            version = kwargs.get(f"bot_{suffix}_version_id")
            if version is not None:
                match_config[f"_bot_{suffix}_version_id"] = version
        if kwargs.get("match_seed") is not None:
            match_config["match_seed"] = kwargs["match_seed"]
        self.store.create_match(
            match_id,
            bot_a_id,
            bot_b_id,
            owner_id=owner_user_id,
            contest_id=contest_id,
            match_type=match_type,
            game_id=game_id,
            match_config=match_config,
        )
        if kwargs.get("match_seed") is not None:
            self.store.update_match(
                match_id, match_seed=kwargs["match_seed"]
            )
        return match_id


def _players(store: Store, tmp_path: Path) -> tuple[list[dict], list[dict], list[dict]]:
    users: list[dict] = []
    bots: list[dict] = []
    versions: list[dict] = []
    for index in range(2):
        user = store.create_user(
            f"elim-u{index}", f"elim-u{index}@example.com", "hash"
        )
        binary = tmp_path / f"elim-bot-{index}-v0"
        binary.write_bytes(b"initial")
        bot = store.create_bot(
            user["id"],
            f"elim-b{index}",
            binary_path=str(binary),
            format="elf",
            game_id="holdem",
        )
        version_path = tmp_path / f"elim-bot-{index}-v1"
        version_path.write_bytes(b"v1")
        version = store.add_bot_version(
            bot["id"], binary_path=str(version_path), version=1
        )
        users.append(user)
        bots.append(bot)
        versions.append(version)
    return users, bots, versions


def _fixture(
    tmp_path: Path,
) -> tuple[Store, ContestManager, _RecordingOrchestrator, dict, list[dict], list[dict], list[dict]]:
    store = Store(str(tmp_path / "elimination.db"))
    users, bots, versions = _players(store, tmp_path)
    contest = store.create_contest(
        "paired tiebreak",
        users[0]["id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps([STAGE]),
        current_stage_idx=0,
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    orch = _RecordingOrchestrator(store)
    manager = ContestManager(store, orch)  # type: ignore[arg-type]
    asyncio.run(manager._begin_stage(contest["id"], 0))
    return store, manager, orch, contest, entries, bots, versions


def _finish(
    store: Store,
    pairing: dict,
    winner: int | None,
    *,
    technical: bool = False,
) -> None:
    match_id = pairing.get("match_id")
    assert isinstance(match_id, str) and match_id
    delta = 0 if winner is None else 100 if winner == 0 else -100
    store.update_match(
        match_id,
        status="completed",
        winner=winner,
        reason="timeout" if technical else "",
        technical_loss=1 if technical else 0,
        result={
            "rounds_played": 0 if technical else 70,
            "deltas": [delta, -delta],
            "normalized_delta": delta / 100,
        },
    )
    completed = store.complete_contest_pairing_for_match(
        int(pairing["contest_id"]), match_id
    )
    assert completed and completed["status"] == "completed"


def _by_group(store: Store, contest_id: int) -> dict[int, list[dict]]:
    grouped: dict[int, list[dict]] = {}
    for row in store.list_contest_pairings(contest_id, stage_idx=0):
        grouped.setdefault(int(row["tiebreak_group"]), []).append(row)
    for rows in grouped.values():
        rows.sort(key=lambda row: int(row["tiebreak_game"]))
    return grouped


def _append_first_tiebreak_group(
    store: Store, contest: dict, primary: dict
) -> list[dict]:
    return store.append_contest_elimination_tiebreak_pairings(
        contest["id"],
        0,
        1,
        0,
        _tiebreak_group_rows(primary, 1),
        expected_current_stage_idx=0,
        expected_previous_tiebreak_group=0,
    )


def _tiebreak_group_rows(primary: dict, group: int) -> list[dict]:
    return [
        {
            "bot_a_id": primary["bot_a_id"],
            "bot_b_id": primary["bot_b_id"],
            "entry_a_id": primary["entry_a_id"],
            "entry_b_id": primary["entry_b_id"],
            "bot_a_version_id": primary["bot_a_version_id"],
            "bot_b_version_id": primary["bot_b_version_id"],
            "round_num": 1,
            "stage_key": "ko",
            "bracket_slot": 0,
            "tiebreak_group": group,
            "tiebreak_game": 1,
        },
        {
            "bot_a_id": primary["bot_b_id"],
            "bot_b_id": primary["bot_a_id"],
            "entry_a_id": primary["entry_b_id"],
            "entry_b_id": primary["entry_a_id"],
            "bot_a_version_id": primary["bot_b_version_id"],
            "bot_b_version_id": primary["bot_a_version_id"],
            "round_num": 1,
            "stage_key": "ko",
            "bracket_slot": 0,
            "tiebreak_group": group,
            "tiebreak_game": 2,
        },
    ]


def test_primary_win_decides_without_extra_games(tmp_path):
    store, manager, _orch, contest, entries, _bots, _versions = _fixture(tmp_path)
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, 0)

    finished = asyncio.run(manager.maybe_finish(contest["id"]))

    assert finished and finished["status"] == "finished"
    assert set(_by_group(store, contest["id"])) == {0}
    official = store.list_official_results(contest["id"])
    assert official[0]["entry_id"] == entries[0]["id"]
    store.close()


def test_draw_appends_private_same_seed_group_and_keeps_primary_versions(
    tmp_path, monkeypatch
):
    store, manager, orch, contest, entries, bots, versions = _fixture(tmp_path)
    monkeypatch.setattr(
        "bzplat.backend.store.db.secrets.randbelow", lambda _upper: 777_776
    )
    primary = _by_group(store, contest["id"])[0][0]
    frozen_versions = (
        primary["bot_a_version_id"],
        primary["bot_b_version_id"],
    )
    assert frozen_versions == (versions[0]["id"], versions[1]["id"])

    # Activating a new program after the primary was published must not change
    # either physical game in the deciding group.
    replacement = tmp_path / "elim-bot-0-v2"
    replacement.write_bytes(b"v2")
    new_version = store.add_bot_version(
        bots[0]["id"], binary_path=str(replacement), version=2
    )
    assert new_version["id"] != frozen_versions[0]
    _finish(store, primary, None)
    asyncio.run(manager.maybe_finish(contest["id"]))

    group = _by_group(store, contest["id"])[1]
    first, second = group
    assert first["pairing_seed"] == 777_777
    assert first["pairing_seed"] == second["pairing_seed"]
    assert isinstance(first["pairing_seed"], int) and first["pairing_seed"] > 0
    assert (first["bot_a_id"], first["bot_b_id"]) == (
        second["bot_b_id"], second["bot_a_id"]
    )
    assert (first["entry_a_id"], first["entry_b_id"]) == (
        second["entry_b_id"], second["entry_a_id"]
    )
    assert (first["bot_a_version_id"], first["bot_b_version_id"]) == frozen_versions
    assert (second["bot_a_version_id"], second["bot_b_version_id"]) == frozen_versions[::-1]
    tiebreak_calls = orch.calls[-2:]
    assert [call["match_seed"] for call in tiebreak_calls] == [
        first["pairing_seed"], first["pairing_seed"]
    ]
    for row in group:
        match = store.get_match(row["match_id"])
        assert match["match_seed"] == first["pairing_seed"]
        assert match["match_config"]["match_seed"] == first["pairing_seed"]

    raw = store.contest_bracket(contest["id"])
    projected = _public_contest_pairings(
        raw,
        stage_types={0: "single_elimination"},
        stage_configs=[STAGE],
        game_id="holdem",
        expected_entry_bots={entry["id"]: entry["bot_id"] for entry in entries},
        expected_entry_users={entry["id"]: entry["user_id"] for entry in entries},
        current_stage_idx=0,
        require_current_entry_bots=True,
    )
    public_group = [row for row in projected if row["tiebreak_group"] == 1]
    assert {(row["tiebreak_group"], row["tiebreak_game"]) for row in public_group} == {
        (1, 1), (1, 2)
    }
    assert all("pairing_seed" not in row and "result" not in row for row in projected)
    store.close()


def test_one_one_group_appends_again_then_logical_entry_wins(tmp_path):
    store, manager, _orch, contest, entries, _bots, _versions = _fixture(tmp_path)
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)
    asyncio.run(manager.maybe_finish(contest["id"]))

    group_one = _by_group(store, contest["id"])[1]
    # Seat 0 wins both games: after the swap each logical entry wins once.
    _finish(store, group_one[0], 0)
    _finish(store, group_one[1], 0)
    asyncio.run(manager.maybe_finish(contest["id"]))
    assert set(_by_group(store, contest["id"])) == {0, 1, 2}

    group_two = _by_group(store, contest["id"])[2]
    # The primary A entry wins once from each physical seat.
    _finish(store, group_two[0], 0)
    _finish(store, group_two[1], 1)
    finished = asyncio.run(manager.maybe_finish(contest["id"]))

    assert finished and finished["status"] == "finished"
    official = store.list_official_results(contest["id"])
    assert official[0]["entry_id"] == entries[0]["id"]
    # Deciding games choose advancement only; the original draw remains the
    # official stage-points record and margin never becomes a hidden decider.
    ranked = {row["entry_id"]: row for row in manager._rank_stage_rows(contest["id"], 0)}
    assert ranked[entries[0]["id"]]["points"] == 1
    assert ranked[entries[1]["id"]]["points"] == 1
    store.close()


def test_technical_primary_loss_is_a_decisive_result(tmp_path):
    store, manager, _orch, contest, entries, _bots, _versions = _fixture(tmp_path)
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, 1, technical=True)

    finished = asyncio.run(manager.maybe_finish(contest["id"]))

    assert finished and finished["status"] == "finished"
    assert set(_by_group(store, contest["id"])) == {0}
    assert store.list_official_results(contest["id"])[0]["entry_id"] == entries[1]["id"]
    store.close()


@pytest.mark.parametrize("damaged_terminal", ["aborted", "malformed"])
def test_aborted_or_malformed_primary_fails_closed(tmp_path, damaged_terminal):
    store, manager, _orch, contest, entries, _bots, _versions = _fixture(tmp_path)
    primary = _by_group(store, contest["id"])[0][0]
    match_id = primary["match_id"]
    if damaged_terminal == "aborted":
        store.update_match(match_id, status="aborted", reason="platform_error")
    else:
        store.update_match(
            match_id,
            status="completed",
            winner=None,
            result={
                "rounds_played": 70,
                "deltas": [100, -100],
                "normalized_delta": 1.0,
            },
        )
        store.update_contest_pairing(primary["id"], status="completed")
    rows = store.list_contest_pairings(contest["id"], stage_idx=0)
    summary = summarize_elimination_encounter(
        STAGE,
        rows,
        store.get_match,
        game_spec=game_registry.get("holdem"),
        expected_contest_id=contest["id"],
        expected_entry_bots={entry["id"]: entry["bot_id"] for entry in entries},
        expected_entry_users={entry["id"]: entry["user_id"] for entry in entries},
        require_current_entry_bots=True,
    )
    assert summary["state"] == "invalid"
    assert asyncio.run(
        manager._maybe_next_elim_round(contest["id"], 0, STAGE)
    ) == "blocked"
    assert set(_by_group(store, contest["id"])) == {0}
    store.close()


def test_partial_tiebreak_is_waiting_but_completed_without_match_is_invalid(tmp_path):
    store, manager, _orch, contest, entries, _bots, _versions = _fixture(tmp_path)
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)
    asyncio.run(manager.maybe_finish(contest["id"]))
    group = _by_group(store, contest["id"])[1]
    _finish(store, group[0], 0)
    rows = store.list_contest_pairings(contest["id"], stage_idx=0)
    lookup = store.get_match
    summary = summarize_elimination_encounter(
        STAGE,
        rows,
        lookup,
        game_spec=game_registry.get("holdem"),
        expected_contest_id=contest["id"],
        expected_entry_bots={entry["id"]: entry["bot_id"] for entry in entries},
        expected_entry_users={entry["id"]: entry["user_id"] for entry in entries},
        require_current_entry_bots=True,
    )
    assert summary["state"] == "awaiting_results"
    assert summary["completed_tiebreak_games"] == 1

    damaged = [dict(row) for row in rows]
    pending = next(row for row in damaged if row["tiebreak_group"] == 1 and row["tiebreak_game"] == 2)
    pending["status"] = "completed"
    pending["match_id"] = None
    damaged_summary = summarize_elimination_encounter(
        STAGE,
        damaged,
        lookup,
        game_spec=game_registry.get("holdem"),
        expected_contest_id=contest["id"],
        expected_entry_bots={entry["id"]: entry["bot_id"] for entry in entries},
        expected_entry_users={entry["id"]: entry["user_id"] for entry in entries},
        require_current_entry_bots=True,
    )
    assert damaged_summary["state"] == "invalid"

    snapshot = store.contest_projection_snapshot(contest["id"])
    assert snapshot is not None
    summaries = build_stage_summaries(
        manager,
        store.get_contest(contest["id"]),
        store.list_contest_entries(contest["id"]),
        snapshot["pairings"],
    )
    encounter = summaries[0]["elimination_tiebreak"]["encounters"][0]
    assert encounter["state"] == "awaiting_results"
    assert encounter["completed_tiebreak_games"] == 1
    assert encounter["groups"] == [
        {
            "group": 1,
            "state": "awaiting_results",
            "completed_games": 1,
            "planned_games": 2,
            "points_a": 3.0,
            "points_b": 0.0,
        }
    ]
    # Decision games are a separate unbounded unit: the base stage and each
    # participant still have one primary scoring game, while the encounter is
    # not complete until the paired group decides advancement.
    assert summaries[0]["counts"]["encounter_groups"] == {
        "completed": 0,
        "total": 1,
    }
    assert summaries[0]["counts"]["match_jobs"] == {
        "completed": 1,
        "total": 1,
    }
    assert summaries[0]["counts"]["scoring_games"] == {
        "completed": 1,
        "planned": 1,
        "terminal_unplayed": 0,
    }

    app = create_app(db_path=store.path)
    live = TestClient(app).get(f"/api/contests/{contest['id']}/live")
    assert live.status_code == 200
    payload = live.json()
    assert payload["counts"]["match_jobs"] == {
        "completed": 1,
        "total": 1,
    }
    live_encounter = payload["elimination_tiebreak"]["encounters"][0]
    assert live_encounter["state"] == "awaiting_results"
    assert live_encounter["groups"][0]["completed_games"] == 1
    assert live_encounter["groups"][0]["points_a"] == 3.0
    app.state.store.close()
    store.close()


def test_append_is_concurrent_and_restart_idempotent(tmp_path, monkeypatch):
    store, manager, _orch, contest, entries, _bots, _versions = _fixture(tmp_path)
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)

    async def no_dispatch(*_args, **_kwargs) -> None:
        return None

    restarted = ContestManager(store, _RecordingOrchestrator(store))  # type: ignore[arg-type]
    monkeypatch.setattr(manager, "_dispatch_pending_locked", no_dispatch)
    monkeypatch.setattr(restarted, "_dispatch_pending_locked", no_dispatch)
    rows = [
        {
            "bot_a_id": primary["bot_a_id"],
            "bot_b_id": primary["bot_b_id"],
            "entry_a_id": primary["entry_a_id"],
            "entry_b_id": primary["entry_b_id"],
            "bot_a_version_id": primary["bot_a_version_id"],
            "bot_b_version_id": primary["bot_b_version_id"],
            "round_num": 1,
            "stage_key": "ko",
            "bracket_slot": 0,
            "tiebreak_group": 1,
            "tiebreak_game": 1,
        },
        {
            "bot_a_id": primary["bot_b_id"],
            "bot_b_id": primary["bot_a_id"],
            "entry_a_id": primary["entry_b_id"],
            "entry_b_id": primary["entry_a_id"],
            "bot_a_version_id": primary["bot_b_version_id"],
            "bot_b_version_id": primary["bot_a_version_id"],
            "round_num": 1,
            "stage_key": "ko",
            "bracket_slot": 0,
            "tiebreak_group": 1,
            "tiebreak_game": 2,
        },
    ]
    barrier = threading.Barrier(2)

    def append() -> int:
        barrier.wait(timeout=5)
        return len(
            store.append_contest_elimination_tiebreak_pairings(
                contest["id"],
                0,
                1,
                0,
                rows,
                expected_current_stage_idx=0,
                expected_previous_tiebreak_group=0,
            )
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(append) for _ in range(2)]
        outcomes = sorted(future.result() for future in futures)
    assert outcomes == [0, 2]
    persisted_group = _by_group(store, contest["id"])[1]
    assert len(persisted_group) == 2
    assert len({row["pairing_seed"] for row in persisted_group}) == 1
    assert persisted_group[0]["pairing_seed"] > 0
    assert asyncio.run(
        restarted._maybe_next_elim_round(contest["id"], 0, STAGE)
    ) == "blocked"
    assert len(_by_group(store, contest["id"])[1]) == 2
    store.close()


@pytest.mark.parametrize("seeds", [(919191, 818181), (919191, 919191)])
def test_append_rejects_caller_injected_group_seed(tmp_path, seeds):
    store, _manager, _orch, contest, _entries, _bots, _versions = _fixture(
        tmp_path
    )
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)
    rows = [
        {
            "bot_a_id": primary["bot_a_id"],
            "bot_b_id": primary["bot_b_id"],
            "entry_a_id": primary["entry_a_id"],
            "entry_b_id": primary["entry_b_id"],
            "bot_a_version_id": primary["bot_a_version_id"],
            "bot_b_version_id": primary["bot_b_version_id"],
            "round_num": 1,
            "stage_key": "ko",
            "bracket_slot": 0,
            "pairing_seed": seeds[0],
            "tiebreak_group": 1,
            "tiebreak_game": 1,
        },
        {
            "bot_a_id": primary["bot_b_id"],
            "bot_b_id": primary["bot_a_id"],
            "entry_a_id": primary["entry_b_id"],
            "entry_b_id": primary["entry_a_id"],
            "bot_a_version_id": primary["bot_b_version_id"],
            "bot_b_version_id": primary["bot_a_version_id"],
            "round_num": 1,
            "stage_key": "ko",
            "bracket_slot": 0,
            "pairing_seed": seeds[1],
            "tiebreak_group": 1,
            "tiebreak_game": 2,
        },
    ]

    with pytest.raises(ValueError, match="只能由存储事务私密分配"):
        store.append_contest_elimination_tiebreak_pairings(
            contest["id"],
            0,
            1,
            0,
            rows,
            expected_current_stage_idx=0,
            expected_previous_tiebreak_group=0,
        )

    assert 1 not in _by_group(store, contest["id"])
    store.close()


def test_append_rejects_retry_after_persisted_seed_diverges(tmp_path):
    store, _manager, _orch, contest, _entries, _bots, _versions = _fixture(
        tmp_path
    )
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)

    def rows() -> list[dict]:
        return [
            {
                "bot_a_id": primary["bot_a_id"],
                "bot_b_id": primary["bot_b_id"],
                "entry_a_id": primary["entry_a_id"],
                "entry_b_id": primary["entry_b_id"],
                "bot_a_version_id": primary["bot_a_version_id"],
                "bot_b_version_id": primary["bot_b_version_id"],
                "round_num": 1,
                "stage_key": "ko",
                "bracket_slot": 0,
                "tiebreak_group": 1,
                "tiebreak_game": 1,
            },
            {
                "bot_a_id": primary["bot_b_id"],
                "bot_b_id": primary["bot_a_id"],
                "entry_a_id": primary["entry_b_id"],
                "entry_b_id": primary["entry_a_id"],
                "bot_a_version_id": primary["bot_b_version_id"],
                "bot_b_version_id": primary["bot_a_version_id"],
                "round_num": 1,
                "stage_key": "ko",
                "bracket_slot": 0,
                "tiebreak_group": 1,
                "tiebreak_game": 2,
            },
        ]

    inserted = store.append_contest_elimination_tiebreak_pairings(
        contest["id"],
        0,
        1,
        0,
        rows(),
        expected_current_stage_idx=0,
        expected_previous_tiebreak_group=0,
    )
    assert len(inserted) == 2
    persisted_seed = inserted[0]["pairing_seed"]
    assert persisted_seed == inserted[1]["pairing_seed"]
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET pairing_seed=? WHERE id=?",
            (persisted_seed + 1, inserted[1]["id"]),
        )

    with pytest.raises(ValueError, match="重试冻结契约不一致"):
        store.append_contest_elimination_tiebreak_pairings(
            contest["id"],
            0,
            1,
            0,
            rows(),
            expected_current_stage_idx=0,
            expected_previous_tiebreak_group=0,
        )

    persisted = _by_group(store, contest["id"])[1]
    assert {row["pairing_seed"] for row in persisted} == {
        persisted_seed,
        persisted_seed + 1,
    }
    store.close()


def test_append_rejects_non_integer_persisted_previous_group_coordinate(tmp_path):
    store, manager, _orch, contest, _entries, _bots, _versions = _fixture(
        tmp_path
    )
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)
    _append_first_tiebreak_group(store, contest, primary)
    asyncio.run(manager._dispatch_pending(contest["id"], 0))
    first_group = _by_group(store, contest["id"])[1]
    for pairing in first_group:
        _finish(store, pairing, None)
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET tiebreak_group=1.5 "
            "WHERE contest_id=? AND stage_idx=0 AND tiebreak_group=1",
            (contest["id"],),
        )

    next_group = _tiebreak_group_rows(primary, 2)
    with pytest.raises(ValueError, match="淘汰决胜坐标损坏"):
        store.append_contest_elimination_tiebreak_pairings(
            contest["id"],
            0,
            1,
            0,
            next_group,
            expected_current_stage_idx=0,
            expected_previous_tiebreak_group=1,
        )
    assert not [
        row
        for row in store.list_contest_pairings(contest["id"], stage_idx=0)
        if row["tiebreak_group"] == 2
    ]
    store.close()


@pytest.mark.parametrize("damaged_field", ["pairing_seed", "bot_b_version_id"])
def test_append_rechecks_previous_group_seed_and_identity_in_transaction(
    tmp_path, damaged_field
):
    store, manager, _orch, contest, _entries, _bots, _versions = _fixture(
        tmp_path
    )
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)
    _append_first_tiebreak_group(store, contest, primary)
    asyncio.run(manager._dispatch_pending(contest["id"], 0))
    first_group = _by_group(store, contest["id"])[1]
    for pairing in first_group:
        _finish(store, pairing, None)
    second_game = next(
        pairing for pairing in first_group if pairing["tiebreak_game"] == 2
    )
    damaged_value = int(second_game[damaged_field]) + 1
    with store._tx() as conn:
        conn.execute(
            f"UPDATE contest_pairings SET {damaged_field}=? WHERE id=?",
            (damaged_value, second_game["id"]),
        )

    with pytest.raises(ValueError, match="上一淘汰决胜组冻结契约损坏"):
        store.append_contest_elimination_tiebreak_pairings(
            contest["id"],
            0,
            1,
            0,
            _tiebreak_group_rows(primary, 2),
            expected_current_stage_idx=0,
            expected_previous_tiebreak_group=1,
        )
    assert not [
        row
        for row in store.list_contest_pairings(contest["id"], stage_idx=0)
        if row["tiebreak_group"] == 2
    ]
    store.close()


@pytest.mark.parametrize("drift", ["primary", "previous_group"])
def test_append_rechecks_tied_match_results_in_transaction(tmp_path, drift):
    store, manager, _orch, contest, _entries, _bots, _versions = _fixture(
        tmp_path
    )
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)
    target_group = 1
    previous_group = 0

    if drift == "previous_group":
        _append_first_tiebreak_group(store, contest, primary)
        asyncio.run(manager._dispatch_pending(contest["id"], 0))
        first_group = _by_group(store, contest["id"])[1]
        for pairing in first_group:
            _finish(store, pairing, None)
        drift_pairing = next(
            pairing for pairing in first_group if pairing["tiebreak_game"] == 2
        )
        target_group = 2
        previous_group = 1
    else:
        drift_pairing = primary

    store.update_match(
        drift_pairing["match_id"],
        winner=0,
        result={
            "rounds_played": 70,
            "deltas": [100, -100],
            "normalized_delta": 1.0,
        },
    )
    with pytest.raises(ValueError, match="淘汰遭遇赛果已变化"):
        store.append_contest_elimination_tiebreak_pairings(
            contest["id"],
            0,
            1,
            0,
            _tiebreak_group_rows(primary, target_group),
            expected_current_stage_idx=0,
            expected_previous_tiebreak_group=previous_group,
        )
    assert not [
        row
        for row in store.list_contest_pairings(contest["id"], stage_idx=0)
        if row["tiebreak_group"] == target_group
    ]
    store.close()


def test_append_idempotent_retry_rejects_seed_reused_by_other_coordinate(tmp_path):
    store, _manager, _orch, contest, _entries, _bots, _versions = _fixture(
        tmp_path
    )
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)
    rows = [
        {
            "bot_a_id": primary["bot_a_id"],
            "bot_b_id": primary["bot_b_id"],
            "entry_a_id": primary["entry_a_id"],
            "entry_b_id": primary["entry_b_id"],
            "bot_a_version_id": primary["bot_a_version_id"],
            "bot_b_version_id": primary["bot_b_version_id"],
            "round_num": 1,
            "stage_key": "ko",
            "bracket_slot": 0,
            "tiebreak_group": 1,
            "tiebreak_game": 1,
        },
        {
            "bot_a_id": primary["bot_b_id"],
            "bot_b_id": primary["bot_a_id"],
            "entry_a_id": primary["entry_b_id"],
            "entry_b_id": primary["entry_a_id"],
            "bot_a_version_id": primary["bot_b_version_id"],
            "bot_b_version_id": primary["bot_a_version_id"],
            "round_num": 1,
            "stage_key": "ko",
            "bracket_slot": 0,
            "tiebreak_group": 1,
            "tiebreak_game": 2,
        },
    ]
    inserted = store.append_contest_elimination_tiebreak_pairings(
        contest["id"],
        0,
        1,
        0,
        rows,
        expected_current_stage_idx=0,
        expected_previous_tiebreak_group=0,
    )
    seed = inserted[0]["pairing_seed"]
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET pairing_seed=? WHERE id=?",
            (seed, primary["id"]),
        )

    with pytest.raises(ValueError, match="重试冻结契约不一致"):
        store.append_contest_elimination_tiebreak_pairings(
            contest["id"],
            0,
            1,
            0,
            rows,
            expected_current_stage_idx=0,
            expected_previous_tiebreak_group=0,
        )
    store.close()


@pytest.mark.parametrize("damaged_seed", [None, 0, "bad"])
def test_tiebreak_seed_is_never_backfilled_when_missing_or_damaged(
    tmp_path, damaged_seed
):
    store, _manager, _orch, contest, _entries, _bots, _versions = _fixture(
        tmp_path
    )
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)
    inserted = _append_first_tiebreak_group(store, contest, primary)
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET pairing_seed=? WHERE id=?",
            (damaged_seed, inserted[0]["id"]),
        )
    damaged = next(
        row
        for row in store.list_contest_pairings(contest["id"], stage_idx=0)
        if row["id"] == inserted[0]["id"]
    )

    with pytest.raises(ValueError, match="seed|pairing_seed"):
        store.ensure_contest_pairing_seed_for_enqueue(
            contest["id"],
            damaged,
            expected_stages_json=contest["stages_json"],
        )
    assert store.list_contest_pairings(contest["id"], stage_idx=0)[1][
        "pairing_seed"
    ] == damaged_seed
    store.close()


def test_existing_tiebreak_seed_rejects_damaged_pair_contract(tmp_path):
    store, _manager, _orch, contest, _entries, _bots, _versions = _fixture(
        tmp_path
    )
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)
    inserted = _append_first_tiebreak_group(store, contest, primary)
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_pairings SET bot_a_version_id=? WHERE id=?",
            (primary["bot_a_version_id"], inserted[1]["id"]),
        )
    first = next(
        row
        for row in store.list_contest_pairings(contest["id"], stage_idx=0)
        if row["id"] == inserted[0]["id"]
    )

    with pytest.raises(ValueError, match="其他坐标复用"):
        store.ensure_contest_pairing_seed_for_enqueue(
            contest["id"],
            first,
            expected_stages_json=contest["stages_json"],
        )
    store.close()


def test_paired_swap_frozen_contract_rejects_duplicate_lifecycle(tmp_path):
    damaged_stage = {**STAGE, "duplicate": True}
    assert stage_scoring_contract_is_valid(damaged_stage, game_id="holdem") is False

    store, manager, _orch, contest, _entries, _bots, _versions = _fixture(
        tmp_path
    )
    primary = _by_group(store, contest["id"])[0][0]
    _finish(store, primary, None)
    store.update_contest(contest["id"], stages_json=json.dumps([damaged_stage]))

    assert asyncio.run(
        manager._maybe_next_elim_round(contest["id"], 0, damaged_stage)
    ) == "blocked"
    assert set(_by_group(store, contest["id"])) == {0}
    store.close()


def test_legacy_draw_without_marker_remains_blocked(tmp_path):
    store, manager, _orch, contest, _entries, _bots, _versions = _fixture(tmp_path)
    legacy = {key: value for key, value in STAGE.items() if key != "tiebreak"}
    store.update_contest(contest["id"], stages_json=json.dumps([legacy]))
    primary = _by_group(store, contest["id"])[0][0]

    app = create_app(db_path=store.path)
    client = TestClient(app)

    def projections():
        detail = client.get(f"/api/contests/{contest['id']}")
        live = client.get(f"/api/contests/{contest['id']}/live")
        assert detail.status_code == 200
        assert live.status_code == 200
        return (
            detail.json()["stage_standings"][0]["elimination_tiebreak"],
            live.json()["elimination_tiebreak"],
        )

    # A historical KO is not blocked merely because its encounter is pending.
    assert projections() == (None, None)

    _finish(store, primary, None)

    detail_projection, live_projection = projections()
    for projection in (detail_projection, live_projection):
        assert projection == {
            "mode": "legacy_draw_blocked",
            "unbounded": False,
            "state": "legacy_draw_blocked",
            "encounters": [
                {
                    "round_num": 1,
                    "bracket_slot": 0,
                    "state": "legacy_draw_blocked",
                    "entry_a_label": "elim-b0",
                    "entry_b_label": "elim-b1",
                }
            ],
        }
        assert set(projection) == {"mode", "unbounded", "state", "encounters"}
        assert set(projection["encounters"][0]) == {
            "round_num",
            "bracket_slot",
            "state",
            "entry_a_label",
            "entry_b_label",
        }

    # A current marker uses the supported unbounded policy, never the legacy
    # blocked sentinel even while its first decision group is still absent.
    store.update_contest(contest["id"], stages_json=json.dumps([STAGE]))
    marker_detail, marker_live = projections()
    assert marker_detail["mode"] == ELIMINATION_TIEBREAK_PAIRED_SWAP
    assert marker_live["mode"] == ELIMINATION_TIEBREAK_PAIRED_SWAP

    # A decisive historical result is not blocked.
    store.update_contest(contest["id"], stages_json=json.dumps([legacy]))
    store.update_match(
        primary["match_id"],
        winner=0,
        result={
            "rounds_played": 70,
            "deltas": [100, -100],
            "normalized_delta": 1,
        },
    )
    assert projections() == (None, None)

    # Even an old draw is not advertised as a current block after the contest
    # has terminated.
    store.update_match(
        primary["match_id"],
        winner=None,
        result={
            "rounds_played": 70,
            "deltas": [0, 0],
            "normalized_delta": 0,
        },
    )
    assert asyncio.run(
        manager._maybe_next_elim_round(contest["id"], 0, legacy)
    ) == "blocked"
    assert set(_by_group(store, contest["id"])) == {0}

    store.update_contest(contest["id"], status="finished")
    assert projections() == (None, None)
    app.state.store.close()
    store.close()


def test_estimate_marks_base_count_and_unbounded_tiebreak(tmp_path):
    store, manager, _orch, contest, _entries, _bots, _versions = _fixture(tmp_path)
    estimate = manager.estimate(contest["id"])
    assert estimate["estimated_matches"] == 1
    assert estimate["estimated_scoring_games"] == 1
    assert estimate["unbounded_tiebreak"] is True
    assert estimate["stages"][0]["unbounded_tiebreak"] is True
    store.close()


def test_ordinary_internal_seed_is_frozen_in_config_and_match_column(tmp_path):
    store = Store(str(tmp_path / "ordinary-seed.db"))
    users, bots, versions = _players(store, tmp_path)
    orch = MatchOrchestrator(
        store,
        runner=MatchRunner(BinaryRunner(prefer_local=True)),
        max_concurrent=1,
    )
    enable_execution_queue(store)
    request_id = asyncio.run(
        orch.challenge(
            bots[0]["id"],
            bots[1]["id"],
            users[0]["id"],
            game_id="holdem",
            bot_a_version_id=versions[0]["id"],
            bot_b_version_id=versions[1]["id"],
            match_seed=8675309,
        )
    )
    job = claim_request(orch, request_id, start=False)
    match = store.get_match(job["current_match_id"])
    assert match["match_seed"] == 8675309
    assert match["match_config"]["match_seed"] == 8675309

    with pytest.raises(ValueError, match="不能同时"):
        asyncio.run(
            orch.challenge(
                bots[0]["id"],
                bots[1]["id"],
                users[0]["id"],
                game_id="holdem",
                duplicate=True,
                duplicate_seed=7,
                match_seed=8,
            )
        )
    store.close()


@pytest.mark.parametrize(("participants", "rounds"), [(13, 7), (16, 9), (21, 11)])
def test_swiss_round_bands_select_requested_thresholds(participants, rounds):
    stage = {
        "key": "swiss",
        "type": "swiss",
        "scoring": "ccgc_2_1_0",
        "rounds": 0,
        "swiss_round_bands": [
            {"min_participants": 13, "max_participants": 15, "rounds": 7},
            {"min_participants": 16, "max_participants": 20, "rounds": 9},
            {"min_participants": 21, "max_participants": None, "rounds": 11},
        ],
    }
    assert effective_swiss_rounds(validate_stage(stage, 0, "gomoku"), participants) == rounds


@pytest.mark.parametrize(
    "bands",
    [
        [],
        [{"min_participants": 13, "max_participants": 15}],
        [{"min_participants": True, "max_participants": 15, "rounds": 7}],
        [
            {"min_participants": 16, "max_participants": None, "rounds": 9},
            {"min_participants": 21, "max_participants": None, "rounds": 11},
        ],
    ],
)
def test_swiss_round_bands_malformed_fail_closed(bands):
    stage = {
        "key": "swiss",
        "type": "swiss",
        "scoring": "ccgc_2_1_0",
        "rounds": 0,
        "swiss_round_bands": bands,
    }
    with pytest.raises(ValueError):
        validate_stage(stage, 0, "gomoku")
    assert stage_scoring_contract_is_valid(stage, game_id="gomoku") is False


@pytest.mark.parametrize(
    ("participant_count", "stage_policy", "expected_rounds"),
    [
        (4, {"rounds": 0}, 2),
        (4, {"rounds": 5}, 5),
        (
            13,
            {
                "rounds": 0,
                "swiss_round_bands": [
                    {"min_participants": 13, "max_participants": 15, "rounds": 7},
                    {"min_participants": 16, "max_participants": None, "rounds": 9},
                ],
            },
            7,
        ),
    ],
)
def test_publish_freezes_every_swiss_round_policy(
    tmp_path, participant_count, stage_policy, expected_rounds
):
    store = Store(str(tmp_path / "swiss-publish.db"))
    users: list[dict] = []
    bots: list[dict] = []
    for index in range(participant_count):
        user = store.create_user(
            f"band-u{index}", f"band-u{index}@example.com", "hash"
        )
        binary = tmp_path / f"band-bot-{index}"
        binary.write_bytes(b"band")
        bot = store.create_bot(
            user["id"],
            f"band-b{index}",
            binary_path=str(binary),
            format="elf",
            game_id="holdem",
        )
        users.append(user)
        bots.append(bot)
    stage = {
        "key": "swiss",
        "type": "swiss",
        "scoring": "poker_3_1_0",
        **stage_policy,
    }
    contest = store.create_contest(
        "band publish",
        users[0]["id"],
        status="open",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    manager = ContestManager(store, _RecordingOrchestrator(store))  # type: ignore[arg-type]

    published = asyncio.run(manager.publish(contest["id"]))

    frozen = json.loads(published["stages_json"])[0]
    assert frozen["effective_rounds"] == expected_rounds
    assert effective_swiss_rounds(frozen, participant_count) == expected_rounds
    store.close()


def test_publish_freezes_later_swiss_against_planned_advanced_cohort(tmp_path):
    store = Store(str(tmp_path / "later-swiss-cohort.db"))
    users: list[dict] = []
    bots: list[dict] = []
    for index in range(16):
        user = store.create_user(
            f"cohort-u{index}", f"cohort-u{index}@example.com", "hash"
        )
        binary = tmp_path / f"cohort-bot-{index}"
        binary.write_bytes(b"cohort")
        bot = store.create_bot(
            user["id"],
            f"cohort-b{index}",
            binary_path=str(binary),
            format="elf",
            game_id="holdem",
        )
        users.append(user)
        bots.append(bot)
    contest = store.create_contest(
        "later Swiss cohort",
        users[0]["id"],
        status="open",
        game_id="holdem",
        stages_json=json.dumps(
            [
                {
                    "key": "qualifier",
                    "type": "round_robin",
                    "scoring": "poker_3_1_0",
                    "advance_count": 4,
                },
                {
                    "key": "final",
                    "type": "swiss",
                    "rounds": 0,
                    "scoring": "poker_3_1_0",
                },
            ]
        ),
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    manager = ContestManager(store, _RecordingOrchestrator(store))  # type: ignore[arg-type]

    estimate = manager.estimate(contest["id"])
    published = asyncio.run(manager.publish(contest["id"]))

    frozen = json.loads(published["stages_json"])
    assert estimate["stages"][1]["participant_count"] == 4
    assert estimate["stages"][1]["effective_rounds"] == 2
    assert frozen[1]["effective_rounds"] == 2
    store.close()


def test_live_stage_projects_tiebreak_marker(tmp_path):
    app = create_app(db_path=str(tmp_path / "live-marker.db"))
    store = app.state.store
    users, bots, _versions = _players(store, tmp_path)
    contest = store.create_contest(
        "live marker",
        users[0]["id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps([STAGE]),
        current_stage_idx=0,
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])

    response = TestClient(app).get(f"/api/contests/{contest['id']}/live")

    assert response.status_code == 200
    assert response.json()["stage"]["tiebreak"] == ELIMINATION_TIEBREAK_PAIRED_SWAP


def test_pairing_coordinate_migration_is_idempotent(tmp_path):
    db_path = tmp_path / "migration.db"
    store = Store(str(db_path))
    store.close()
    connection = sqlite3.connect(db_path)
    connection.executescript(
        """
        DROP INDEX IF EXISTS idx_contest_pairings_elimination_coordinate;
        ALTER TABLE contest_pairings RENAME TO contest_pairings_current;
        CREATE TABLE contest_pairings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id INTEGER NOT NULL,
            round_num INTEGER NOT NULL DEFAULT 1,
            entry_a_id INTEGER,
            entry_b_id INTEGER,
            bot_a_id INTEGER,
            bot_b_id INTEGER,
            bot_a_version_id INTEGER,
            bot_b_version_id INTEGER,
            pairing_seed INTEGER,
            published_at TEXT,
            scheduled_at TEXT,
            match_id TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            stage_idx INTEGER NOT NULL DEFAULT 0,
            stage_key TEXT NOT NULL DEFAULT '',
            group_id TEXT NOT NULL DEFAULT '',
            bracket_slot INTEGER,
            color_first INTEGER NOT NULL DEFAULT 0,
            series_index INTEGER NOT NULL DEFAULT 1,
            series_size INTEGER NOT NULL DEFAULT 1
        );
        DROP TABLE contest_pairings_current;
        """
    )
    connection.commit()
    connection.close()

    migrated = Store(str(db_path))
    with migrated._tx() as connection:
        columns = {
            row[1]: row for row in connection.execute("PRAGMA table_info(contest_pairings)")
        }
        indexes = {
            row[1] for row in connection.execute("PRAGMA index_list(contest_pairings)")
        }
    assert columns["tiebreak_group"]["dflt_value"] == "0"
    assert columns["tiebreak_game"]["dflt_value"] == "0"
    assert "idx_contest_pairings_elimination_coordinate" in indexes
    migrated.close()
    # A second open is the deploy/restart idempotence proof.
    Store(str(db_path)).close()
