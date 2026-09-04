"""Dedicated contest live spectator projection contracts."""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.series import series_rows_settled
from bzplat.backend.contests.validation import stage_scoring_contract_is_valid
from bzplat.backend.crypto import hash_password
from bzplat.backend.games import registry as game_registry
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


class _ReadOnlyOrchestrator:
    max_concurrent = 2


_MISSING = object()


def _people(
    store: Store, tmp_path: Path, count: int, *, prefix: str
) -> tuple[list[dict], list[dict]]:
    users: list[dict] = []
    bots: list[dict] = []
    for index in range(count):
        user = store.create_user(
            f"{prefix}-u{index}",
            f"{prefix}-u{index}@example.com",
            "hash",
        )
        binary = tmp_path / f"{prefix}-{index}.bin"
        binary.write_bytes(b"test fixture")
        bot = store.create_bot(
            user["id"],
            f"{prefix}-b{index}",
            binary_path=str(binary),
            format="elf",
            game_id="holdem",
        )
        users.append(user)
        bots.append(bot)
    return users, bots


def _canonical_tiebreaks(points: int | float, seed: int) -> dict[str, int | float]:
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


def _bind_match(
    store: Store,
    contest_id: int,
    pairing: dict,
    match_id: str,
    *,
    status: str,
    duplicate: bool = False,
    frozen_duplicate: bool | None = None,
) -> None:
    store.create_match(
        match_id,
        pairing["bot_a_id"],
        pairing["bot_b_id"],
        owner_id=pairing["bot_a_id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
        match_config=(
            {"duplicate": frozen_duplicate}
            if frozen_duplicate is not None
            else ({"duplicate": True} if duplicate else None)
        ),
    )
    store.bind_contest_pairing_match(
        contest_id,
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    if status == "running":
        store.update_match(
            match_id,
            status="running",
            started_at="2026-08-27T10:02:00+08:00",
        )
    elif status == "completed":
        result = (
            {
                "rounds_played": 140,
                "deltas": [20, -20],
                "legs": [
                    {
                        "winner": 0,
                        "deltas": [10, -10],
                        "rounds_played": 70,
                    },
                    {
                        "winner": 0,
                        "deltas": [10, -10],
                        "rounds_played": 70,
                    },
                ],
            }
            if duplicate
            else {"rounds_played": 70, "deltas": [10, -10]}
        )
        store.update_match(
            match_id,
            status="completed",
            winner=None if duplicate else 0,
            result=result,
            ended_at="2026-08-27T10:03:00+08:00",
        )
        store.complete_contest_pairing_for_match(contest_id, match_id)


def _live_fixture(tmp_path: Path):
    app = create_app(db_path=str(tmp_path / "live-api.db"))
    store = app.state.store
    organizer = store.create_user(
        "live-org", "live-org@example.com", "hash", role="organizer"
    )
    users, bots = _people(store, tmp_path, 4, prefix="live")
    stage = {
        "key": "dup_rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "duplicate": True,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
    }
    contest = store.create_contest(
        "Live projection",
        organizer["id"],
        status="published",
        starts_at="2099-12-31T23:59:59",
        game_id="holdem",
        template_id="holdem_dup_rr",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    pairs = [(0, 1), (0, 2), (0, 3), (1, 2), (1, 3), (2, 3)]
    pairings = store.create_contest_stage_pairings(
        contest["id"],
        0,
        [
            {
                "bot_a_id": bots[first]["id"],
                "bot_b_id": bots[second]["id"],
                "entry_a_id": entries[first]["id"],
                "entry_b_id": entries[second]["id"],
                "stage_idx": 0,
                "stage_key": "dup_rr",
                "round_num": ordinal,
                "pairing_seed": 7000 + ordinal,
                "published_at": "2026-08-27T10:00:00",
                "scheduled_at": (
                    "2099-12-31T23:59:59" if ordinal == 4 else None
                ),
            }
            for ordinal, (first, second) in enumerate(pairs, start=1)
        ],
        expected_current_stage_idx=0,
        expected_status="published",
        activate_running=True,
    )
    assert store.contest_stage_manifest_is_valid(contest["id"], 0)
    _bind_match(
        store,
        contest["id"],
        pairings[0],
        "live-completed",
        status="completed",
        duplicate=True,
    )
    _bind_match(
        store,
        contest["id"],
        pairings[1],
        "live-running",
        status="running",
    )
    _bind_match(
        store,
        contest["id"],
        pairings[2],
        "live-queued",
        status="pending",
    )
    return app, contest, entries, bots


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value)) if value else set()
    return set()


def test_live_projection_is_four_select_snapshot_without_n_plus_one(
    tmp_path, monkeypatch
):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    client = TestClient(app)

    # The common bracket must not read or even synthesize a result key.  Passing
    # it to standings therefore keeps the established get_match fallback.
    common_trace: list[str] = []
    store._conn.set_trace_callback(common_trace.append)
    try:
        common_rows = store.contest_bracket(contest["id"])
    finally:
        store._conn.set_trace_callback(None)
    assert all("_match_result_json" not in row for row in common_rows)
    bracket_selects = [
        sql
        for sql in common_trace
        if sql.lstrip().upper().startswith("SELECT")
        and "FROM contest_pairings" in sql
    ]
    assert len(bracket_selects) == 1, bracket_selects
    projected_sql = bracket_selects[0].lower()
    assert "m.result" not in projected_sql
    assert "events_json" not in projected_sql
    common_standings = ContestManager(
        store, _ReadOnlyOrchestrator()
    ).standings(contest["id"], pairings=common_rows)
    assert common_standings[0]["points"] == 6

    forbidden_store_calls = (
        "get_contest",
        "list_contest_entries",
        "get_match",
        "get_bot",
        "contest_bracket",
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("live projection escaped its frozen Store snapshot")

    for name in forbidden_store_calls:
        monkeypatch.setattr(store, name, unexpected)

    traced: list[str] = []
    store._conn.set_trace_callback(traced.append)
    try:
        response = client.get(f"/api/contests/{contest['id']}/live")
    finally:
        store._conn.set_trace_callback(None)
    assert response.status_code == 200, response.text
    selects = [sql for sql in traced if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 4, selects
    assert any("FROM contests" in sql for sql in selects)
    assert any("FROM contest_pairings" in sql and "m.result" in sql for sql in selects)
    assert any("FROM contest_entries" in sql for sql in selects)
    assert any("FROM contest_stage_results" in sql for sql in selects)

    payload = response.json()
    assert payload["progress"] == {
        "completed": 1,
            # Strict-v1 totals come from the complete four-entry cohort.
        "total": 6,
        "running": 1,
        "pending": 4,
    }
    assert payload["counts"] == {
        "encounter_groups": {"completed": 1, "total": 6},
        "match_jobs": {"completed": 1, "total": 6},
        "scoring_games": {"completed": 2, "planned": 12, "terminal_unplayed": 0},
    }
    assert [row["display_status"] for row in payload["active"]] == ["running"]
    assert {row["display_status"] for row in payload["upcoming"]} == {
        "queued",
        "pending",
    }
    assert [row["display_status"] for row in payload["recent"]] == ["completed"]
    assert payload["series"] == {
        "games_per_pair": 1,
        "duplicate": True,
        "scoring_mode": "independent_scoring_game_points_v1",
        "scoring_legs_per_match": 2,
        "scoring_legs_per_pair": 2,
    }
    assert payload["recent"][0]["outcome"]["score"] == {
        "wins_a": 2,
        "draws": 0,
        "wins_b": 0,
    }
    assert payload["contest"]["showcase"] is False
    assert payload["contest"]["immutable"] is False
    assert payload["contest"]["official_results_ready"] is False
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert "Authorization" in response.headers["vary"]
    assert "Cookie" in response.headers["vary"]

    detail = client.get(f"/api/contests/{contest['id']}")
    assert detail.status_code == 200, detail.text
    detail_payload = detail.json()
    assert set(detail_payload["contest"]) == {
        "id",
        "title",
        "description",
        "organizer_id",
        "status",
        "registration_opens_at",
        "registration_closes_at",
        "starts_at",
        "ends_at",
        "created_at",
        "game_id",
        "ruleset_version",
        "protocol_version",
        "rating_pool_id",
        "stages_json",
        "current_stage_idx",
        "template_id",
        "rest_ends_at",
        "phase",
        "source_contest_id",
        "time_control_id",
        "official_results_ready",
        "require_real_name",
        "showcase_key",
        "format_snapshot",
        "games_per_pair",
        "stage_series_settings",
        "stage_format_settings",
        "time_control",
        "template_name",
    }
    stage_summary = detail_payload["stage_standings"][0]
    assert set(stage_summary) == {
        "stage_idx",
        "stage_key",
        "status",
        "source",
        "completed_pairings",
        "total_pairings",
        "counts",
        "advancement_final",
        "elimination_tiebreak",
        "rows",
    }
    assert stage_summary["status"] == "running"
    assert stage_summary["total_pairings"] == 6
    assert stage_summary["counts"] == payload["counts"]
    assert len(stage_summary["rows"]) == 4

    forbidden_fields = {
        "contest_id",
        "organizer_id",
        "entry_a_id",
        "entry_b_id",
        "user_id",
        "bot_a_version_id",
        "bot_b_version_id",
        "pairing_seed",
        "published_at",
        "match_status",
        "_match_created_at",
        "_match_result_json",
        "result",
        "events",
        "email",
        "real_name_snapshot",
        "phone_snapshot",
    }
    assert not (_all_keys(payload) & forbidden_fields)
    assert "@example.com" not in response.text

    generated = datetime.fromisoformat(payload["generated_at"])
    assert generated.tzinfo is not None
    actual_update = datetime.fromisoformat(payload["updated_at"])
    assert actual_update.year == 2026
    assert actual_update < datetime.fromisoformat("2099-12-31T23:59:59")
    assert payload["updated_at"] not in {
        "2099-12-31T23:59:59",
        payload["contest"]["starts_at"],
    }


def _installed_two_player_decision(tmp_path: Path, suffix: str):
    app = create_app(db_path=str(tmp_path / f"active-decision-{suffix}.db"))
    store = app.state.store
    organizer = store.create_user(
        f"decision-{suffix}-org",
        f"decision-{suffix}-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 2, prefix=f"decision-{suffix}")
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
    }
    contest = store.create_contest(
        "Installed decision authority",
        organizer["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots, strict=True)
    ]
    manager = app.state.contest_manager
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    pairing = store.list_contest_pairings(contest["id"], stage_idx=0)[0]
    match_id = f"active-decision-{suffix}"
    _bind_match(
        store,
        contest["id"],
        pairing,
        match_id,
        status="completed",
    )
    _ranked, decision_revision, decision_entries, decision_groups = (
        manager._ensure_stage_decision(contest["id"], 0)
    )
    frozen_order = [
        row["bot_id"]
        for row in store.list_stage_results(contest["id"], stage_idx=0)
    ]
    assert frozen_order == [bots[0]["id"], bots[1]["id"]]
    return (
        app,
        contest,
        entries,
        bots,
        match_id,
        frozen_order,
        decision_revision,
        decision_entries,
        decision_groups,
    )


@pytest.mark.parametrize("lifecycle_status", ["running", "rest"])
def test_active_installed_decision_is_the_only_detail_and_live_ranking(
    tmp_path, monkeypatch, lifecycle_status
):
    """A transition retry and public Top table consume one immutable decision."""
    (
        app,
        contest,
        _entries,
        _bots,
        match_id,
        frozen_order,
        decision_revision,
        decision_entries,
        decision_groups,
    ) = (
        _installed_two_player_decision(tmp_path, lifecycle_status)
    )
    store = app.state.store
    manager = app.state.contest_manager

    # Model the transition-failure window: the immutable decision is already
    # installed, but a corrupt/imported Match now replays the opposite result.
    # The public read must not expose that newer replay while retry logic still
    # advances from the old decision.
    with store._tx() as connection:
        connection.execute(
            "UPDATE matches_holdem SET winner=1,result=? WHERE id=?",
            (
                json.dumps({"rounds_played": 70, "deltas": [-10, 10]}),
                match_id,
            ),
        )
    replayed_order = [
        row["bot_id"] for row in manager.standings(contest["id"], stage_idx=0)
    ]
    assert replayed_order == list(reversed(frozen_order))
    if lifecycle_status == "rest":
        store.enter_contest_rest_from_decision(
            contest["id"],
            0,
            expected_revision=decision_revision,
            expected_status="running",
            expected_entries=decision_entries,
            expected_stage_groups=decision_groups,
            rest_ends_at="2099-01-01T00:00:00",
        )

    def unexpected_replay(*_args, **_kwargs):
        raise AssertionError("installed stage decision was replayed from Match")

    monkeypatch.setattr(manager, "standings", unexpected_replay)
    client = TestClient(app)
    detail_response = client.get(f"/api/contests/{contest['id']}")
    live_response = client.get(f"/api/contests/{contest['id']}/live")
    assert detail_response.status_code == 200, detail_response.text
    assert live_response.status_code == 200, live_response.text
    detail = detail_response.json()
    live = live_response.json()
    assert detail["stage_standings"][0]["source"] == "persisted"
    assert [row["bot_id"] for row in detail["stage_standings"][0]["rows"]] == frozen_order
    assert [row["bot_id"] for row in detail["standings"]] == frozen_order
    assert [row["bot_id"] for row in live["standings"]] == frozen_order


@pytest.mark.parametrize(
    ("column", "damaged_value"),
    [
        ("points", "bad"),
        ("points", float("inf")),
        ("wins", -1),
        ("wins", "bad"),
        ("delta_total", "bad"),
        ("points", 999),
    ],
)
def test_detail_and_live_reject_malformed_persisted_stage_statistics(
    tmp_path, column, damaged_value
):
    """Weak SQLite values cannot crash or contradict a decision artifact."""
    suffix = f"bad-stat-{column}-{str(damaged_value).replace('.', '_')}"
    (
        app,
        contest,
        entries,
        _bots,
        _match_id,
        _frozen_order,
        _decision_revision,
        _decision_entries,
        _decision_groups,
    ) = (
        _installed_two_player_decision(tmp_path, suffix)
    )
    store = app.state.store
    with store._tx() as connection:
        connection.execute(
            f"UPDATE contest_stage_results SET {column}=? "
            "WHERE contest_id=? AND stage_idx=0 AND entry_id=?",
            (damaged_value, contest["id"], entries[0]["id"]),
        )

    client = TestClient(app)
    detail_response = client.get(f"/api/contests/{contest['id']}")
    live_response = client.get(f"/api/contests/{contest['id']}/live")
    assert detail_response.status_code == 200, detail_response.text
    assert live_response.status_code == 200, live_response.text
    detail = detail_response.json()
    live = live_response.json()
    assert detail["stage_standings"][0]["rows"] == []
    assert detail["standings"] == []
    assert live["standings"] == []
    assert live["progress"]["completed"] == 0


@pytest.mark.parametrize("corruption", ["delete", "coordinate"])
def test_detail_and_live_reject_current_topology_seal_drift(tmp_path, corruption):
    """A changed current graph cannot keep publishing its old sealed ranking."""
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    client = TestClient(app)
    before = client.get(f"/api/contests/{contest['id']}/live")
    assert before.status_code == 200, before.text
    assert before.json()["standings"]
    target = store.list_contest_pairings(contest["id"], stage_idx=0)[-1]
    with store._tx() as connection:
        if corruption == "delete":
            connection.execute(
                "DELETE FROM contest_pairings WHERE id=?", (target["id"],)
            )
        else:
            connection.execute(
                "UPDATE contest_pairings SET round_num=round_num+100 WHERE id=?",
                (target["id"],),
            )

    detail_response = client.get(f"/api/contests/{contest['id']}")
    live_response = client.get(f"/api/contests/{contest['id']}/live")
    assert detail_response.status_code == 200, detail_response.text
    assert live_response.status_code == 200, live_response.text
    detail = detail_response.json()
    live = live_response.json()
    assert detail["standings"] == []
    assert detail["stage_standings"][0]["rows"] == []
    assert live["standings"] == []
    assert live["progress"]["completed"] == 0


@pytest.mark.parametrize("future_artifact", ["pairing", "stage-result"])
def test_detail_and_live_reject_future_stage_artifacts(tmp_path, future_artifact):
    """No artifact beyond the persisted cursor may authenticate current reads."""
    app, contest, entries, bots = _live_fixture(tmp_path)
    store = app.state.store
    client = TestClient(app)
    current_stage = json.loads(store.get_contest(contest["id"])["stages_json"])[0]
    stages = [current_stage, {**current_stage, "key": "future"}]
    store.update_contest(contest["id"], stages_json=json.dumps(stages))
    if future_artifact == "pairing":
        store.add_pairing(
            contest["id"],
            bots[0]["id"],
            bots[1]["id"],
            entry_a_id=entries[0]["id"],
            entry_b_id=entries[1]["id"],
            stage_idx=1,
            stage_key="future",
        )
    else:
        with store._tx() as connection:
            connection.execute(
                "INSERT INTO contest_stage_results("
                "contest_id,stage_idx,stage_key,entry_id,bot_id,points,wins,draws,"
                "losses,delta_total,group_id,rank_in_group,payload_json) "
                "VALUES(?,1,'future',?,?,0,0,0,0,0,'',1,'{}')",
                (contest["id"], entries[0]["id"], bots[0]["id"]),
            )

    detail_response = client.get(f"/api/contests/{contest['id']}")
    live_response = client.get(f"/api/contests/{contest['id']}/live")
    assert detail_response.status_code == 200, detail_response.text
    assert live_response.status_code == 200, live_response.text
    detail = detail_response.json()
    live = live_response.json()
    assert detail["standings"] == []
    assert detail["stage_standings"][0]["rows"] == []
    assert live["standings"] == []
    assert live["progress"]["completed"] == 0


def test_live_uses_compact_topology_seal_for_omitted_predecessor_graph(tmp_path):
    """Current-only polling proves history via the transition seal, not rows alone."""
    app = create_app(db_path=str(tmp_path / "live-predecessor-seal.db"))
    store = app.state.store
    organizer = store.create_user(
        "live-seal-org", "live-seal-org@example.com", "hash", role="organizer"
    )
    users, bots = _people(store, tmp_path, 4, prefix="live-seal")
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 2,
        },
        {
            "key": "final",
            "type": "double_round_robin",
            "scoring": "poker_3_1_0",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
        },
    ]
    contest = store.create_contest(
        "Live predecessor seal",
        organizer["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps(stages),
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    manager = ContestManager(store, _ReadOnlyOrchestrator())
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    for pairing in store.list_contest_pairings(contest["id"], stage_idx=0):
        _bind_match(
            store,
            contest["id"],
            pairing,
            f"live-seal-qualifier-{pairing['id']}",
            status="completed",
        )
    ranked, decision_revision, _decision_entries, expected_groups = (
        manager._ensure_stage_decision(contest["id"], 0)
    )
    projected, updates = manager._plan_participant_advancement(
        contest["id"], 0, ranked_rows=ranked
    )
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            1,
            dispatch_pending=False,
            entry_rows=projected,
            entry_updates=updates,
            source_decision_revision=decision_revision,
            source_stage_groups=expected_groups,
        )
    )
    compact_snapshot = store.contest_live_snapshot(contest["id"])
    assert compact_snapshot is not None
    assert compact_snapshot["pairings_include_history"] is False
    assert {
        row["stage_idx"] for row in compact_snapshot["pairings"]
    } == {1}
    assert store.contest_stage_manifest_is_valid(contest["id"], 1)

    client = TestClient(app)
    detail_ok = client.get(f"/api/contests/{contest['id']}")
    live_ok = client.get(f"/api/contests/{contest['id']}/live")
    assert detail_ok.status_code == live_ok.status_code == 200
    assert len(detail_ok.json()["standings"]) == 2
    assert len(live_ok.json()["standings"]) == 2

    historical_pairing_id = store.list_contest_pairings(
        contest["id"], stage_idx=0
    )[0]["id"]
    with store._tx() as connection:
        connection.execute(
            "DELETE FROM contest_pairings WHERE id=?", (historical_pairing_id,)
        )

    detail_damaged = client.get(f"/api/contests/{contest['id']}")
    live_damaged = client.get(f"/api/contests/{contest['id']}/live")
    assert detail_damaged.status_code == live_damaged.status_code == 200
    assert detail_damaged.json()["standings"] == []
    assert live_damaged.json()["standings"] == []
    assert live_damaged.json()["progress"]["completed"] == 0
    store.close()


def test_finished_unsealed_live_reads_history_before_accepting_current_snapshot(
    tmp_path,
):
    """An omitted predecessor graph cannot downgrade a proven contradiction."""
    app = create_app(db_path=str(tmp_path / "finished-unsealed-history.db"))
    store = app.state.store
    organizer = store.create_user(
        "finished-unsealed-org",
        "finished-unsealed-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 4, prefix="finished-unsealed")
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 2,
        },
        {
            "key": "final",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
        },
    ]
    contest = store.create_contest(
        "Finished unsealed history",
        organizer["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps(stages),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots, strict=True)
    ]
    manager = app.state.contest_manager
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    for pairing in store.list_contest_pairings(contest["id"], stage_idx=0):
        _bind_match(
            store,
            contest["id"],
            pairing,
            f"finished-unsealed-q-{pairing['id']}",
            status="completed",
        )
    ranked, _decision_revision, _decision_entries, _expected_groups = (
        manager._ensure_stage_decision(contest["id"], 0)
    )
    qualifier_order = [
        int(row["entry_id"]) for row in sorted(ranked, key=lambda row: row["rank"])
    ]
    expected_finalists = set(qualifier_order[:2])
    wrong_finalists = set(qualifier_order[2:])
    assert expected_finalists.isdisjoint(wrong_finalists)

    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_entries SET eliminated=1 WHERE contest_id=?",
            (contest["id"],),
        )
        connection.executemany(
            "UPDATE contest_entries SET eliminated=0 WHERE contest_id=? AND id=?",
            [(contest["id"], entry_id) for entry_id in wrong_finalists],
        )
        connection.execute(
            "UPDATE contests SET current_stage_idx=1,"
            "published_stage_pairing_count=NULL,"
            "sealed_pairing_topology_revision=NULL WHERE id=?",
            (contest["id"],),
        )
    entry_by_id = {int(entry["id"]): entry for entry in entries}
    wrong_order = sorted(wrong_finalists)
    first, second = (entry_by_id[entry_id] for entry_id in wrong_order)
    final_pairing = store.add_pairing(
        contest["id"],
        first["bot_id"],
        second["bot_id"],
        entry_a_id=first["id"],
        entry_b_id=second["id"],
        stage_idx=1,
        stage_key="final",
        published_at="2026-08-27T10:00:00",
    )
    # This fixture deliberately models an imported legacy terminal artifact.
    # Active bind APIs correctly reject the missing seal, so create the
    # historical Match first and attach it in the same low-level import below.
    store.create_match(
        "finished-unsealed-final",
        first["bot_id"],
        second["bot_id"],
        owner_id=first["user_id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(
        "finished-unsealed-final",
        status="completed",
        winner=0,
        result={"rounds_played": 70, "deltas": [10, -10]},
        ended_at="2026-08-27T10:03:00+08:00",
    )
    current_rows = []
    for rank, entry_id in enumerate(wrong_order, 1):
        entry = entry_by_id[entry_id]
        points = 3 if rank == 1 else 0
        current_rows.append(
            (
                contest["id"],
                entry_id,
                entry["bot_id"],
                points,
                1 if rank == 1 else 0,
                0,
                0 if rank == 1 else 1,
                10 if rank == 1 else -10,
                rank,
                json.dumps(
                    {
                        "tiebreaks": _canonical_tiebreaks(
                            points, int(entry["seed"])
                        )
                    }
                ),
            )
        )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_pairings SET match_id=?,status='completed' "
            "WHERE id=?",
            ("finished-unsealed-final", final_pairing["id"]),
        )
        connection.executemany(
            "INSERT INTO contest_stage_results("
            "contest_id,stage_idx,stage_key,entry_id,bot_id,points,wins,draws,"
            "losses,delta_total,group_id,rank_in_group,payload_json) "
            "VALUES(?,1,'final',?,?,?,?,?,?,?,'',?,?)",
            current_rows,
        )
        connection.execute(
            "UPDATE contests SET status='finished',"
            "published_stage_pairing_count=1,"
            "sealed_pairing_topology_revision=NULL WHERE id=?",
            (contest["id"],),
        )

    live_snapshot = store.contest_live_snapshot(contest["id"])
    assert live_snapshot is not None
    assert live_snapshot["pairings_include_history"] is True
    assert {row["stage_idx"] for row in live_snapshot["pairings"]} == {0, 1}
    client = TestClient(app)
    detail_response = client.get(f"/api/contests/{contest['id']}")
    live_response = client.get(f"/api/contests/{contest['id']}/live")
    assert detail_response.status_code == 200, detail_response.text
    assert live_response.status_code == 200, live_response.text
    detail = detail_response.json()
    live = live_response.json()
    assert detail["stage_standings"][1]["rows"] == []
    assert detail["standings"] == []
    assert live["standings"] == []


def test_outcome_is_identical_across_match_detail_contest_bracket_and_live(tmp_path):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    client = TestClient(app)

    match_outcome = client.get("/api/matches/live-completed").json()["match"][
        "outcome"
    ]
    detail = client.get(f"/api/contests/{contest['id']}").json()
    detail_outcome = next(
        row["outcome"]
        for row in detail["pairings"]
        if row.get("match_id") == "live-completed"
    )
    bracket = client.get(f"/api/contests/{contest['id']}/bracket").json()
    bracket_outcome = next(
        row["outcome"]
        for row in bracket["pairings"]
        if row.get("match_id") == "live-completed"
    )
    live = client.get(f"/api/contests/{contest['id']}/live").json()
    live_outcome = next(
        row["outcome"]
        for row in live["recent"]
        if row.get("match_id") == "live-completed"
    )

    assert match_outcome is not None
    assert match_outcome == detail_outcome == bracket_outcome == live_outcome
    assert match_outcome["games"] == [
        {
            "index": 1,
            "winner": 0,
            "rounds_played": 70,
            "normalized_delta_a": 0.1,
        },
        {
            "index": 2,
            "winner": 0,
            "rounds_played": 70,
            "normalized_delta_a": 0.1,
        },
    ]


@pytest.mark.parametrize(
    ("reason", "technical_loss"),
    [("technical_loss", 0), ("completed", 1)],
)
def test_reason_flag_conflict_is_null_across_match_and_contest_surfaces(
    tmp_path, reason, technical_loss
):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE matches_holdem SET reason=?,technical_loss=? "
            "WHERE id='live-completed'",
            (reason, technical_loss),
        )
    client = TestClient(app)

    match_outcome = client.get("/api/matches/live-completed").json()["match"][
        "outcome"
    ]
    listed = client.get("/api/matches?status=completed&game_id=holdem").json()
    list_outcome = next(
        row["outcome"]
        for row in listed["matches"]
        if row["id"] == "live-completed"
    )
    detail = client.get(f"/api/contests/{contest['id']}").json()
    detail_outcome = next(
        row["outcome"]
        for row in detail["pairings"]
        if row.get("match_id") == "live-completed"
    )
    bracket = client.get(f"/api/contests/{contest['id']}/bracket").json()
    bracket_outcome = next(
        row["outcome"]
        for row in bracket["pairings"]
        if row.get("match_id") == "live-completed"
    )
    live = client.get(f"/api/contests/{contest['id']}/live").json()
    live_outcome = next(
        row["outcome"]
        for row in live["recent"]
        if row.get("match_id") == "live-completed"
    )

    assert match_outcome is None
    assert match_outcome == list_outcome == detail_outcome == bracket_outcome == live_outcome
    assert all(float(row["points"]) == 0 for row in detail["standings"])
    assert all(float(row["points"]) == 0 for row in live["standings"])
    assert detail["stage_standings"][0]["status"] != "completed"
    manager = app.state.contest_manager
    assert manager._stage_done(contest["id"], 0) is False
    assert manager._has_unfinished_pairings(contest["id"]) is True


@pytest.mark.parametrize("explicit_marker", [True, False])
def test_missing_frozen_entry_is_rejected_but_unique_markerless_history_recovers(
    tmp_path, explicit_marker
):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    if not explicit_marker:
        current = store.get_contest(contest["id"])
        stages = json.loads(current["stages_json"])
        stages[0].pop("series_scoring")
        store.update_contest(contest["id"], stages_json=json.dumps(stages))
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_pairings SET entry_a_id=NULL "
            "WHERE match_id='live-completed'"
        )

    client = TestClient(app)
    match_outcome = client.get("/api/matches/live-completed").json()["match"][
        "outcome"
    ]
    detail = client.get(f"/api/contests/{contest['id']}").json()
    bracket = client.get(f"/api/contests/{contest['id']}/bracket").json()
    live = client.get(f"/api/contests/{contest['id']}/live").json()
    surfaces = [
        match_outcome,
        next(
            row["outcome"]
            for row in detail["pairings"]
            if row.get("match_id") == "live-completed"
        ),
        next(
            row["outcome"]
            for row in bracket["pairings"]
            if row.get("match_id") == "live-completed"
        ),
        next(
            row["outcome"]
            for row in live["recent"]
            if row.get("match_id") == "live-completed"
        ),
    ]
    if explicit_marker:
        assert surfaces == [None, None, None, None]
        assert all(float(row["points"]) == 0 for row in detail["standings"])
        assert all(float(row["points"]) == 0 for row in live["standings"])
        assert app.state.contest_manager._stage_done(contest["id"], 0) is False
        assert live["counts"]["scoring_games"] == {
            "completed": 0,
            "planned": 12,
            "terminal_unplayed": 0,
        }
    else:
        assert all(outcome is not None for outcome in surfaces)
        # Markerless legacy outcome decoding remains available for the one
        # Match, but mutating a frozen pairing invalidates the current topology
        # seal.  Neither detail nor live may aggregate that damaged graph.
        assert detail["standings"] == []
        assert live["standings"] == []
    assert detail["stage_standings"][0]["status"] != "completed"
    manager = app.state.contest_manager
    assert manager._stage_done(contest["id"], 0) is False
    assert manager._has_unfinished_pairings(contest["id"]) is True


def test_malformed_technical_flag_is_raw_null_across_all_contest_surfaces(tmp_path):
    """Compact contest projections must not turn a corrupt flag into normal."""
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE matches_holdem SET technical_loss='' "
            "WHERE id='live-completed'"
        )
    client = TestClient(app)

    match = client.get("/api/matches/live-completed")
    listed = client.get("/api/matches?status=completed&game_id=holdem")
    detail = client.get(f"/api/contests/{contest['id']}")
    bracket = client.get(f"/api/contests/{contest['id']}/bracket")
    live = client.get(f"/api/contests/{contest['id']}/live")
    for response in (match, listed, detail, bracket, live):
        assert response.status_code == 200, response.text

    list_outcome = next(
        row["outcome"]
        for row in listed.json()["matches"]
        if row["id"] == "live-completed"
    )
    detail_payload = detail.json()
    detail_outcome = next(
        row["outcome"]
        for row in detail_payload["pairings"]
        if row.get("match_id") == "live-completed"
    )
    bracket_outcome = next(
        row["outcome"]
        for row in bracket.json()["pairings"]
        if row.get("match_id") == "live-completed"
    )
    live_payload = live.json()
    live_outcome = next(
        row["outcome"]
        for row in live_payload["recent"]
        if row.get("match_id") == "live-completed"
    )
    assert match.json()["match"]["outcome"] is None
    assert list_outcome is None
    assert detail_outcome is None
    assert bracket_outcome is None
    assert live_outcome is None
    assert all(float(row["points"]) == 0 for row in detail_payload["standings"])
    assert all(float(row["points"]) == 0 for row in live_payload["standings"])
    assert app.state.contest_manager._stage_done(contest["id"], 0) is False
    assert app.state.contest_manager._has_unfinished_pairings(contest["id"]) is True


def test_stage_match_config_drift_is_null_across_public_outcome_surfaces(tmp_path):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    client = TestClient(app)

    # Stage says duplicate, but the linked Match explicitly froze single and
    # carries a valid single result.  Publishing either interpretation would
    # make generic Match pages disagree with the contest views.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE matches_holdem SET match_config=?,winner=0,result=?,likes_count=1 "
            "WHERE id=?",
            (
                "{}",
                json.dumps({"rounds_played": 70, "deltas": [100, -100]}),
                "live-completed",
            ),
        )

    match_response = client.get("/api/matches/live-completed")
    assert match_response.status_code == 200, match_response.text
    assert match_response.json()["match"]["outcome"] is None
    assert "_contest_expected_duplicate" not in match_response.json()["match"]

    surfaces = [
        client.get("/api/matches?status=completed&game_id=holdem").json()["matches"],
        client.get("/api/search?type=matches&q=live-completed").json()["matches"],
        client.get("/api/matches/liked-top").json()["matches"],
    ]
    for rows in surfaces:
        row = next(item for item in rows if item["id"] == "live-completed")
        assert row["outcome"] is None
        assert "_contest_expected_duplicate" not in row

    detail_payload = client.get(f"/api/contests/{contest['id']}").json()
    detail_pairing = next(
        row
        for row in detail_payload["pairings"]
        if row["match_id"] == "live-completed"
    )
    assert detail_pairing["outcome"] is None
    assert all(float(row["points"]) == 0 for row in detail_payload["standings"])

    bracket_payload = client.get(
        f"/api/contests/{contest['id']}/bracket"
    ).json()
    bracket_pairing = next(
        row
        for row in bracket_payload["pairings"]
        if row["match_id"] == "live-completed"
    )
    assert bracket_pairing["outcome"] is None

    live_payload = client.get(f"/api/contests/{contest['id']}/live").json()
    live_pairing = next(
        row for row in live_payload["recent"] if row["match_id"] == "live-completed"
    )
    assert live_pairing["outcome"] is None
    assert all(float(row["points"]) == 0 for row in live_payload["standings"])

    # Corrupt historical coordinates must still be a bounded metadata response,
    # not a SQLite ``bad JSON path`` 500 or a guessed single-game outcome.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_pairings SET stage_idx=-1 WHERE match_id=?",
            ("live-completed",),
        )
    negative_stage = client.get("/api/matches/live-completed")
    assert negative_stage.status_code == 200, negative_stage.text
    assert negative_stage.json()["match"]["outcome"] is None

    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_pairings SET stage_idx=99 WHERE match_id=?",
            ("live-completed",),
        )
    missing_stage = client.get("/api/matches/live-completed")
    assert missing_stage.status_code == 200, missing_stage.text
    assert missing_stage.json()["match"]["outcome"] is None


@pytest.mark.parametrize(
    "raw_match_config",
    [
        "null",
        json.dumps({"duplicate": 1}),
        json.dumps({"duplicate": "false"}),
        "{bad-json",
        json.dumps([]),
    ],
)
def test_malformed_frozen_match_config_is_null_and_blocks_strict_stage(
    tmp_path, raw_match_config
):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE matches_holdem SET match_config=?,likes_count=1 WHERE id=?",
            (raw_match_config, "live-completed"),
        )

    client = TestClient(app)
    match = client.get("/api/matches/live-completed")
    listed = client.get("/api/matches?status=completed&game_id=holdem")
    detail = client.get(f"/api/contests/{contest['id']}")
    bracket = client.get(f"/api/contests/{contest['id']}/bracket")
    live = client.get(f"/api/contests/{contest['id']}/live")
    for response in (match, listed, detail, bracket, live):
        assert response.status_code == 200, response.text

    list_row = next(
        row
        for row in listed.json()["matches"]
        if row["id"] == "live-completed"
    )
    detail_payload = detail.json()
    detail_pairing = next(
        row
        for row in detail_payload["pairings"]
        if row.get("match_id") == "live-completed"
    )
    bracket_pairing = next(
        row
        for row in bracket.json()["pairings"]
        if row.get("match_id") == "live-completed"
    )
    live_payload = live.json()
    live_pairing = next(
        row
        for row in live_payload["recent"]
        if row.get("match_id") == "live-completed"
    )
    assert match.json()["match"]["outcome"] is None
    assert list_row["outcome"] is None
    assert detail_pairing["outcome"] is None
    assert bracket_pairing["outcome"] is None
    assert live_pairing["outcome"] is None
    assert app.state.contest_manager._stage_done(contest["id"], 0) is False
    assert app.state.contest_manager._has_unfinished_pairings(contest["id"]) is True
    assert all(float(row["points"]) == 0 for row in detail_payload["standings"])
    assert all(float(row["points"]) == 0 for row in live_payload["standings"])


@pytest.mark.parametrize(
    ("invalid_field", "invalid_value"),
    [
        ("duplicate", "false"),
        ("duplicate", 1),
        ("series_scoring", "unknown_scoring_v9"),
        ("series_scoring", 1),
        ("type", _MISSING),
        ("scoring", _MISSING),
        ("scoring", "ccgc_2_1_0"),
        ("games_per_pair", "1"),
        ("games_per_pair", True),
        ("advance_count", "1"),
        ("ranking_scope", "4"),
    ],
)
def test_malformed_stage_scoring_contract_is_fail_closed_across_lifecycle_and_api(
    tmp_path, invalid_field, invalid_value
):
    app = create_app(db_path=str(tmp_path / "invalid-stage-duplicate.db"))
    store = app.state.store
    organizer = store.create_user(
        "invalid-dup-org",
        "invalid-dup-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 2, prefix="invalid-dup")
    stage = {
        "key": "dup_rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "duplicate": True,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
    }
    contest = store.create_contest(
        "Invalid duplicate history",
        organizer["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    manager = app.state.contest_manager
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    pairing = store.list_contest_pairings(contest["id"], stage_idx=0)[0]
    _bind_match(
        store,
        contest["id"],
        pairing,
        "invalid-stage-duplicate-match",
        status="completed",
        duplicate=True,
    )
    assert manager._stage_done(contest["id"], 0) is True
    manager._ensure_stage_decision(contest["id"], 0)

    if invalid_value is _MISSING:
        stage.pop(invalid_field)
    else:
        stage[invalid_field] = invalid_value
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET stages_json=? WHERE id=?",
            (json.dumps([stage]), contest["id"]),
        )
        # Isolate the malformed stage contract from the lifecycle-seal gate:
        # this is an explicit damaged import whose revision is otherwise exact.
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest["id"],),
        )

    client = TestClient(app)
    match = client.get("/api/matches/invalid-stage-duplicate-match")
    detail = client.get(f"/api/contests/{contest['id']}")
    bracket = client.get(f"/api/contests/{contest['id']}/bracket")
    live = client.get(f"/api/contests/{contest['id']}/live")
    for response in (match, detail, bracket, live):
        assert response.status_code == 200, response.text

    listed = client.get("/api/matches?status=completed&game_id=holdem")
    assert listed.status_code == 200, listed.text
    list_outcome = next(
        row["outcome"]
        for row in listed.json()["matches"]
        if row["id"] == "invalid-stage-duplicate-match"
    )
    detail_payload = detail.json()
    detail_outcome = detail_payload["pairings"][0]["outcome"]
    bracket_outcome = bracket.json()["pairings"][0]["outcome"]
    live_payload = live.json()
    live_outcome = live_payload["recent"][0]["outcome"]
    assert match.json()["match"]["outcome"] is None
    assert list_outcome is None
    assert detail_outcome is None
    assert bracket_outcome is None
    assert live_outcome is None

    assert manager._stage_done(contest["id"], 0) is False
    assert manager._has_unfinished_pairings(contest["id"]) is True
    ranked_rows = manager._rank_stage_rows(contest["id"], 0)
    assert ranked_rows == []
    with pytest.raises(ValueError, match="拒绝计算晋级名单"):
        manager._advance_participants(contest["id"], 0)
    assert all(
        int(row.get("eliminated") or 0) == 0
        for row in store.list_contest_entries(contest["id"])
    )
    assert detail_payload["standings"] == []
    stage_summary = detail_payload["stage_standings"][0]
    assert stage_summary["status"] != "completed"
    assert stage_summary["source"] == "persisted"
    assert stage_summary["rows"] == []
    assert live_payload["series"] is None
    assert live_payload["counts"]["scoring_games"] == {
        "completed": 0,
        "planned": 0,
        "terminal_unplayed": 0,
    }
    assert live_payload["standings"] == []


@pytest.mark.parametrize(
    ("invalid_field", "invalid_value"),
    [
        ("type", _MISSING),
        ("scoring", _MISSING),
        ("scoring", "ccgc_2_1_0"),
        ("ranking_scope", 4),
        ("advance_per_group", 1),
        ("ranking_mode", "replace_top"),
        ("unexpected", 1),
    ],
)
def test_malformed_explicit_aggregate_contract_is_null_without_read_model_errors(
    tmp_path, invalid_field, invalid_value
):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    stage = {
        "key": "dup_rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "duplicate": True,
        "games_per_pair": 1,
        "series_scoring": "aggregate_match_points_v1",
    }
    if invalid_value is _MISSING:
        stage.pop(invalid_field)
    else:
        stage[invalid_field] = invalid_value
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET stages_json=? WHERE id=?",
            (json.dumps([stage]), contest["id"]),
        )

    assert not stage_scoring_contract_is_valid(stage, game_id="holdem")
    raw_pairings = store.contest_live_snapshot(contest["id"])["pairings"]
    completed = [
        pairing
        for pairing in raw_pairings
        if pairing.get("match_id") == "live-completed"
    ]
    matches = {
        "live-completed": store.get_match("live-completed"),
    }
    assert not series_rows_settled(
        stage,
        completed,
        matches.get,
        game_spec=game_registry.get("holdem"),
    )
    standings = app.state.contest_manager.standings(contest["id"], stage_idx=0)
    assert len(standings) == 4
    assert all(float(row["points"]) == 0 for row in standings)
    assert all(row["counts"]["match_jobs"] == 0 for row in standings)
    assert app.state.contest_manager._stage_done(contest["id"], 0) is False

    client = TestClient(app)
    responses = {
        "match": client.get("/api/matches/live-completed"),
        "list": client.get("/api/matches?status=completed&game_id=holdem"),
        "detail": client.get(f"/api/contests/{contest['id']}"),
        "bracket": client.get(f"/api/contests/{contest['id']}/bracket"),
        "live": client.get(f"/api/contests/{contest['id']}/live"),
    }
    assert all(response.status_code == 200 for response in responses.values())
    listed_outcome = next(
        row["outcome"]
        for row in responses["list"].json()["matches"]
        if row["id"] == "live-completed"
    )
    detail_payload = responses["detail"].json()
    detail_pairing = next(
        row
        for row in detail_payload["pairings"]
        if row.get("match_id") == "live-completed"
    )
    bracket_pairing = next(
        row
        for row in responses["bracket"].json()["pairings"]
        if row.get("match_id") == "live-completed"
    )
    live_payload = responses["live"].json()
    live_pairing = next(
        row
        for row in live_payload["recent"]
        if row.get("match_id") == "live-completed"
    )
    assert responses["match"].json()["match"]["outcome"] is None
    assert listed_outcome is None
    assert detail_pairing["outcome"] is None
    assert bracket_pairing["outcome"] is None
    assert live_pairing["outcome"] is None
    assert all(float(row["points"]) == 0 for row in detail_payload["standings"])
    assert all(float(row["points"]) == 0 for row in live_payload["standings"])
    assert detail_payload["stage_standings"][0]["status"] != "completed"


def test_cross_contest_pairing_binding_is_null_across_public_outcomes(tmp_path):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    other = store.create_contest(
        "Wrong linked contest",
        contest["organizer_id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps(
            [
                {
                    "key": "other",
                    "type": "round_robin",
                    "scoring": "poker_3_1_0",
                }
            ]
        ),
    )
    # Corrupt-import shape: the unique pairing still belongs to the original
    # contest, while Match.contest_id points elsewhere.  Neither side may infer
    # score semantics from one half of that contradictory relationship.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE matches_holdem SET contest_id=? WHERE id='live-completed'",
            (other["id"],),
        )
    client = TestClient(app)

    match_outcome = client.get("/api/matches/live-completed").json()["match"][
        "outcome"
    ]
    listed = client.get("/api/matches?status=completed&game_id=holdem").json()
    list_outcome = next(
        row["outcome"]
        for row in listed["matches"]
        if row["id"] == "live-completed"
    )
    detail = client.get(f"/api/contests/{contest['id']}").json()
    detail_outcome = next(
        row["outcome"]
        for row in detail["pairings"]
        if row.get("match_id") == "live-completed"
    )
    bracket = client.get(f"/api/contests/{contest['id']}/bracket").json()
    bracket_outcome = next(
        row["outcome"]
        for row in bracket["pairings"]
        if row.get("match_id") == "live-completed"
    )
    live = client.get(f"/api/contests/{contest['id']}/live").json()
    live_outcome = next(
        row["outcome"]
        for row in live["recent"]
        if row.get("match_id") == "live-completed"
    )

    assert match_outcome is None
    assert match_outcome == list_outcome == detail_outcome == bracket_outcome == live_outcome


def test_non_object_stage_is_bounded_null_across_public_contest_surfaces(tmp_path):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET stages_json='[5]' WHERE id=?",
            (contest["id"],),
        )
    client = TestClient(app)

    match = client.get("/api/matches/live-completed")
    detail = client.get(f"/api/contests/{contest['id']}")
    bracket = client.get(f"/api/contests/{contest['id']}/bracket")
    live = client.get(f"/api/contests/{contest['id']}/live")
    for response in (match, detail, bracket, live):
        assert response.status_code == 200, response.text

    detail_payload = detail.json()
    detail_outcome = next(
        row["outcome"]
        for row in detail_payload["pairings"]
        if row.get("match_id") == "live-completed"
    )
    bracket_outcome = next(
        row["outcome"]
        for row in bracket.json()["pairings"]
        if row.get("match_id") == "live-completed"
    )
    live_payload = live.json()
    live_outcome = next(
        row["outcome"]
        for row in live_payload["recent"]
        if row.get("match_id") == "live-completed"
    )

    assert match.json()["match"]["outcome"] is None
    assert detail_outcome is None
    assert bracket_outcome is None
    assert live_outcome is None
    assert detail_payload["stage_standings"] == []
    assert live_payload["stage"] is None
    assert all(float(row["points"]) == 0 for row in detail_payload["standings"])
    assert all(float(row["points"]) == 0 for row in live_payload["standings"])


@pytest.mark.parametrize(
    "corruption",
    [
        "pairing_stage_idx",
        "current_stage_idx",
        "current_stage_idx_real",
        "stage_result_stage_idx",
    ],
)
def test_malformed_stage_coordinates_are_bounded_across_contest_views(
    tmp_path, corruption
):
    app, contest, entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    if corruption == "stage_result_stage_idx":
        store.upsert_stage_result(
            contest["id"],
            0,
            entries[0]["id"],
            bot_id=entries[0]["bot_id"],
            points=6,
            wins=2,
        )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        if corruption == "pairing_stage_idx":
            connection.execute(
                "UPDATE contest_pairings SET stage_idx='bad' "
                "WHERE match_id='live-completed'"
            )
        elif corruption in {"current_stage_idx", "current_stage_idx_real"}:
            connection.execute(
                "UPDATE contests SET current_stage_idx=? WHERE id=?",
                (
                    "bad" if corruption == "current_stage_idx" else 0.5,
                    contest["id"],
                ),
            )
        else:
            connection.execute(
                "UPDATE contest_stage_results SET stage_idx='bad' "
                "WHERE contest_id=?",
                (contest["id"],),
            )
        connection.execute("PRAGMA ignore_check_constraints=OFF")

    client = TestClient(app)
    detail = client.get(f"/api/contests/{contest['id']}")
    bracket = client.get(f"/api/contests/{contest['id']}/bracket")
    live = client.get(f"/api/contests/{contest['id']}/live")
    match = client.get("/api/matches/live-completed")
    assert all(
        response.status_code == 200
        for response in (detail, bracket, live, match)
    )
    detail_payload = detail.json()
    assert not detail_payload.get("stage_standings") or all(
        stage["status"] != "completed"
        for stage in detail_payload["stage_standings"]
    )
    live_payload = live.json()
    assert live_payload.get("stage") is None or live_payload["progress"]["completed"] < live_payload["progress"]["total"]


def test_malformed_official_ready_flag_is_not_public_or_recovered(tmp_path):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    # Explicit imported terminal residue: generic lifecycle writes correctly
    # cannot skip the immutable stage-decision transaction.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status='finished',official_results_ready=0.5 "
            "WHERE id=?",
            (contest["id"],),
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest["id"],),
        )

    manager = app.state.contest_manager
    assert manager._stage_ranking_from_recovery_snapshot(contest["id"], 0) is None
    # A malformed flag is neither ready nor an invitation to recompute history.
    asyncio.run(manager._reconcile_one(contest["id"]))
    assert store.get_contest(contest["id"])["official_results_ready"] == 0.5
    assert store.list_official_results(contest["id"]) == []

    client = TestClient(app)
    live = client.get(f"/api/contests/{contest['id']}/live")
    official = client.get(f"/api/contests/{contest['id']}/official-results")
    assert live.status_code == 200, live.text
    assert live.json()["contest"]["official_results_ready"] is False
    assert official.status_code == 409


def test_missing_all_pairings_for_one_strict_entry_invalidates_detail_live_seal(
    tmp_path,
):
    app, contest, entries, bots = _live_fixture(tmp_path)
    store = app.state.store
    # Put the strict stage after a historical preliminary stage.  The original
    # regression was specific to stage_idx>0, where standings used the surviving
    # pairing graph as a participant filter and erased an entrant whose complete
    # opponent groups were missing.
    current = store.get_contest(contest["id"])
    final_stage = json.loads(current["stages_json"])[0]
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET stages_json=?,current_stage_idx=1 WHERE id=?",
            (
                json.dumps(
                    [
                        {
                            "key": "prelim",
                            "type": "round_robin",
                            "scoring": "poker_3_1_0",
                        },
                        final_stage,
                    ]
                ),
                contest["id"],
            ),
        )
        connection.execute(
            "UPDATE contest_pairings SET stage_idx=1 WHERE contest_id=?",
            (contest["id"],),
        )
    # Entry 4 only appears in the 0-vs-3 fixture row.  Removing that whole
    # opponent group must not make the entrant disappear from read models.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "DELETE FROM contest_pairings WHERE contest_id=? "
            "AND (entry_a_id=? OR entry_b_id=?)",
            (contest["id"], entries[3]["id"], entries[3]["id"]),
        )

    client = TestClient(app)
    detail = client.get(f"/api/contests/{contest['id']}")
    assert detail.status_code == 200, detail.text
    summary = next(
        row
        for row in detail.json()["stage_standings"]
        if row["stage_idx"] == 1
    )
    assert summary["status"] == "running"
    assert summary["total_pairings"] == 6
    assert summary["rows"] == []

    live = client.get(f"/api/contests/{contest['id']}/live")
    assert live.status_code == 200, live.text
    assert live.json()["standings"] == []


@pytest.mark.parametrize("stage_type", ["round_robin", "swiss"])
@pytest.mark.parametrize("damaged_eliminated", [-1, 2])
def test_damaged_eliminated_flag_blocks_later_stage_ranking_and_completion(
    tmp_path, stage_type, damaged_eliminated
):
    app = create_app(db_path=str(tmp_path / "damaged-eliminated.db"))
    store = app.state.store
    organizer = store.create_user(
        "damaged-elim-org",
        "damaged-elim-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 3, prefix="damaged-elim")
    strict_stage = {
        "key": "strict_stage",
        "type": stage_type,
        "scoring": "poker_3_1_0",
        "duplicate": False,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
    }
    if stage_type == "swiss":
        strict_stage["rounds"] = 1
    contest = store.create_contest(
        "Damaged elimination state",
        organizer["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps(
            [
                {
                    "key": "prelim",
                    "type": "round_robin",
                    "scoring": "poker_3_1_0",
                },
                strict_stage,
            ]
        ),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    manager = ContestManager(store, _ReadOnlyOrchestrator())
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    for pairing in store.list_contest_pairings(contest["id"], stage_idx=0):
        _bind_match(
            store,
            contest["id"],
            pairing,
            f"damaged-elim-prelim-{stage_type}-{pairing['id']}",
            status="completed",
        )
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
    match_pairings = [row for row in current_pairings if row.get("bot_b_id")]
    for pairing in match_pairings:
        _bind_match(
            store,
            contest["id"],
            pairing,
            f"damaged-elim-{stage_type}-{pairing['id']}",
            status="completed",
            frozen_duplicate=False,
        )
    assert manager._stage_done(contest["id"], 1) is True
    assert len(manager.standings(contest["id"], stage_idx=1)) == 3
    with pytest.raises(ValueError, match="eliminated"):
        store.update_entry(
            contest["id"],
            users[-1]["id"],
            eliminated=damaged_eliminated,
        )
    # Simulate a low-level import/damaged historical database that bypassed the
    # Store write boundary. Read models and lifecycle still have to fail closed.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_entries SET eliminated=? WHERE id=?",
            (damaged_eliminated, entries[-1]["id"]),
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest["id"],),
        )

    assert manager.standings(contest["id"], stage_idx=1) == []
    assert manager._stage_done(contest["id"], 1) is False
    assert manager._has_unfinished_pairings(contest["id"]) is True

    client = TestClient(app)
    detail_response = client.get(f"/api/contests/{contest['id']}")
    live_response = client.get(f"/api/contests/{contest['id']}/live")
    assert detail_response.status_code == 200, detail_response.text
    assert live_response.status_code == 200, live_response.text

    detail = detail_response.json()
    assert detail["standings"] == []
    stage_summary = next(
        row for row in detail["stage_standings"] if row["stage_idx"] == 1
    )
    assert stage_summary["status"] != "completed"
    assert stage_summary["rows"] == []
    assert stage_summary["completed_pairings"] == 0
    assert stage_summary["total_pairings"] == len(current_pairings)
    assert stage_summary["counts"]["encounter_groups"]["completed"] == 0
    assert (
        stage_summary["counts"]["encounter_groups"]["total"]
        == len(match_pairings)
    )
    assert stage_summary["counts"]["match_jobs"]["completed"] == 0
    assert stage_summary["counts"]["match_jobs"]["total"] == len(match_pairings)
    assert stage_summary["counts"]["scoring_games"]["completed"] == 0
    assert stage_summary["counts"]["scoring_games"]["planned"] == len(match_pairings)

    live = live_response.json()
    assert live["standings"] == []
    assert live["series"] is None
    assert live["progress"]["completed"] == 0
    assert live["progress"]["total"] == len(current_pairings)
    assert live["counts"]["encounter_groups"]["completed"] == 0
    assert live["counts"]["encounter_groups"]["total"] == len(match_pairings)
    assert live["counts"]["match_jobs"]["completed"] == 0
    assert live["counts"]["match_jobs"]["total"] == len(match_pairings)
    assert live["counts"]["scoring_games"]["completed"] == 0
    assert live["counts"]["scoring_games"]["planned"] == len(match_pairings)


def test_detail_projects_future_final_counts_from_frozen_advance_cohort(tmp_path):
    """A future final-8 must not inherit the whole 32-player active roster."""
    app = create_app(db_path=str(tmp_path / "future-final-counts.db"))
    store = app.state.store
    organizer = store.create_user(
        "future-count-org",
        "future-count-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 32, prefix="future-count")
    stages = [
        {
            "key": "qualify",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "games_per_pair": 1,
            "series_scoring": "independent_scoring_game_points_v1",
            "advance_count": 8,
            "allow_large_round_robin": True,
        },
        {
            "key": "final8",
            "type": "double_round_robin",
            "scoring": "poker_3_1_0",
            "games_per_pair": 4,
            "series_scoring": "independent_scoring_game_points_v1",
            "ranking_mode": "replace_top",
            "ranking_scope": 8,
        },
    ]
    contest = store.create_contest(
        "Future final counts",
        organizer["id"],
        status="running",
        game_id="holdem",
        current_stage_idx=0,
        stages_json=json.dumps(stages),
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])

    response = TestClient(app).get(f"/api/contests/{contest['id']}")
    assert response.status_code == 200, response.text
    future = next(
        summary
        for summary in response.json()["stage_standings"]
        if summary["stage_key"] == "final8"
    )
    assert future["status"] == "pending"
    assert future["total_pairings"] == 112
    assert future["counts"] == {
        "encounter_groups": {"completed": 0, "total": 28},
        "match_jobs": {"completed": 0, "total": 112},
        "scoring_games": {
            "completed": 0,
            "planned": 112,
            "terminal_unplayed": 0,
        },
    }


def test_detail_and_live_require_complete_predecessor_for_current_cohort(tmp_path):
    """Current pairings/active flags cannot replace a complete qualifier proof."""
    app = create_app(db_path=str(tmp_path / "current-cohort-proof.db"))
    store = app.state.store
    organizer = store.create_user(
        "current-cohort-proof-org",
        "current-cohort-proof-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 4, prefix="current-cohort-proof")
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 2,
        },
        {
            "key": "final",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
        },
    ]
    contest = store.create_contest(
        "Current cohort proof",
        organizer["id"],
        status="running",
        game_id="holdem",
        current_stage_idx=1,
        stages_json=json.dumps(stages),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    for index, entry in enumerate(entries):
        store.update_entry(
            contest["id"],
            users[index]["id"],
            eliminated=int(index >= 2),
        )
    # Both historical artifacts name only the same plausible Top 2. Neither
    # may prove that the missing two entrants actually lost the qualifier.
    store.add_pairing(
        contest["id"],
        bots[0]["id"],
        bots[1]["id"],
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        stage_idx=0,
        stage_key="qualifier",
    )
    store.add_pairing(
        contest["id"],
        bots[0]["id"],
        bots[1]["id"],
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        stage_idx=1,
        stage_key="final",
    )
    with store._tx() as connection:
        connection.executemany(
            "INSERT INTO contest_stage_results("
            "contest_id,stage_idx,stage_key,entry_id,bot_id,points,wins,draws,"
            "losses,delta_total,group_id,rank_in_group,payload_json) "
            "VALUES(?,0,'qualifier',?,?,0,0,0,0,0,'',?, '{}')",
            [
                (contest["id"], entries[0]["id"], bots[0]["id"], 1),
                (contest["id"], entries[1]["id"], bots[1]["id"], 2),
            ],
        )

    manager = ContestManager(store, _ReadOnlyOrchestrator())
    assert manager.standings(contest["id"], stage_idx=1) == []
    client = TestClient(app)
    detail_response = client.get(f"/api/contests/{contest['id']}")
    live_response = client.get(f"/api/contests/{contest['id']}/live")
    assert detail_response.status_code == 200, detail_response.text
    assert live_response.status_code == 200, live_response.text
    detail = detail_response.json()
    assert detail["standings"] == []
    assert detail["stage_standings"][0]["rows"] == []
    assert detail["stage_standings"][1]["rows"] == []
    assert live_response.json()["standings"] == []
    store.close()


def test_detail_and_live_reject_ambiguous_nonterminal_knockout(tmp_path):
    """Imported KO history without an advancement rule stays unpublished."""
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    store.update_contest(
        contest["id"],
        current_stage_idx=0,
        stages_json=json.dumps(
            [
                {"key": "qualifier", "type": "single_elimination"},
                {
                    "key": "final",
                    "type": "double_round_robin",
                    "ranking_mode": "replace_top",
                    "ranking_scope": 2,
                },
            ]
        ),
    )

    client = TestClient(app)
    detail_response = client.get(f"/api/contests/{contest['id']}")
    live_response = client.get(f"/api/contests/{contest['id']}/live")

    assert detail_response.status_code == 200, detail_response.text
    assert live_response.status_code == 200, live_response.text
    detail = detail_response.json()
    live = live_response.json()
    assert detail["standings"] == []
    assert detail["stage_standings"][0]["rows"] == []
    assert detail["stage_standings"][0]["status"] != "completed"
    assert live["standings"] == []
    assert live["progress"]["completed"] == 0
    store.close()


@pytest.mark.parametrize(
    ("series_scoring", "expected_deltas"),
    [
        ("independent_scoring_game_points_v1", [0, 0]),
        ("aggregate_match_points_v1", [1, -1]),
    ],
)
def test_unavailable_duplicate_freezes_match_contract_across_every_projection(
    tmp_path, series_scoring, expected_deltas
):
    app = create_app(db_path=str(tmp_path / "unavailable-duplicate.db"))
    store = app.state.store
    organizer = store.create_user(
        "unavailable-org",
        "unavailable-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 2, prefix="unavailable-dup")
    stage = {
        "key": "dup_rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "duplicate": True,
        "games_per_pair": 1,
        "series_scoring": series_scoring,
    }
    contest = store.create_contest(
        "Unavailable duplicate",
        organizer["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    manager = ContestManager(store, _ReadOnlyOrchestrator())
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    store.update_bot(bots[1]["id"], is_active=0)

    asyncio.run(manager._dispatch_pending(contest["id"], 0))
    pairing = store.list_contest_pairings(contest["id"])[0]
    match_id = pairing["match_id"]
    match = store.get_match(match_id)
    assert match["match_config"]["duplicate"] is True
    assert match["result"]["deltas"] == expected_deltas

    client = TestClient(app)
    match_outcome = client.get(f"/api/matches/{match_id}").json()["match"][
        "outcome"
    ]
    listed = client.get("/api/matches?status=completed&game_id=holdem").json()
    list_outcome = next(
        row["outcome"] for row in listed["matches"] if row["id"] == match_id
    )
    detail = client.get(f"/api/contests/{contest['id']}").json()
    detail_outcome = next(
        row["outcome"] for row in detail["pairings"] if row["match_id"] == match_id
    )
    bracket = client.get(f"/api/contests/{contest['id']}/bracket").json()
    bracket_outcome = next(
        row["outcome"] for row in bracket["pairings"] if row["match_id"] == match_id
    )
    live = client.get(f"/api/contests/{contest['id']}/live").json()
    live_outcome = next(
        row["outcome"] for row in live["recent"] if row["match_id"] == match_id
    )

    assert match_outcome == list_outcome == detail_outcome == bracket_outcome == live_outcome
    assert match_outcome["kind"] == "duplicate"
    assert match_outcome["planned_games"] == 2
    assert match_outcome["completed_games"] == 1
    assert match_outcome["score"] == {"wins_a": 1, "draws": 0, "wins_b": 0}


@pytest.mark.parametrize(
    ("series_scoring", "margins", "expected_first"),
    [
        (
            "independent_scoring_game_points_v1",
            (1000, 1, 1, 1000),
            1,
        ),
        (
            "aggregate_match_points_v1",
            (1, 1000, 1000, 1),
            0,
        ),
    ],
)
def test_live_and_detail_use_frozen_official_tiebreak_chain_before_delta(
    tmp_path, series_scoring, margins, expected_first
):
    app = create_app(
        db_path=str(tmp_path / f"live-{series_scoring}-ranking.db")
    )
    store = app.state.store
    users, bots = _people(store, tmp_path, 6, prefix="rank-v1")
    stage = {
        "key": "swiss",
        "type": "swiss",
        "scoring": "poker_3_1_0",
        "rounds": 0,
        "games_per_pair": 1,
        "series_scoring": series_scoring,
        "effective_rounds": 2,
    }
    contest = store.create_contest(
        "V1 live ranking",
        users[0]["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    # A(0) and B(1) both finish on 3 points.  ``margins`` deliberately gives
    # the player with the weaker raw delta the stronger mode-specific Cut1:
    # independent-v1 drops the lowest record, legacy aggregate drops highest.
    margin_a_win, margin_b_win, margin_a_loss, margin_b_loss = margins
    games = [
        (1, 0, 2, 0, margin_a_win),
        (1, 1, 3, 0, margin_b_win),
        (1, 4, 5, 0, 10),
        (2, 5, 0, 0, margin_a_loss),
        (2, 4, 1, 0, margin_b_loss),
        (2, 2, 3, 0, 10),
    ]
    pairings = store.create_contest_stage_pairings(
        contest["id"],
        0,
        [
            {
                "bot_a_id": bots[first]["id"],
                "bot_b_id": bots[second]["id"],
                "entry_a_id": entries[first]["id"],
                "entry_b_id": entries[second]["id"],
                "stage_idx": 0,
                "stage_key": "swiss",
                "round_num": round_num,
                "series_index": 1,
                "series_size": 1,
                "pairing_seed": 8100 + ordinal,
                "published_at": "2026-08-28T12:00:00",
            }
            for ordinal, (round_num, first, second, _winner, _margin) in enumerate(
                games, 1
            )
        ],
        expected_current_stage_idx=0,
        expected_status="published",
        activate_running=True,
    )
    for ordinal, (pairing, game) in enumerate(zip(pairings, games, strict=True), 1):
        _round_num, first, second, winner, margin = game
        match_id = f"rank-v1-{ordinal}"
        store.create_match(
            match_id,
            bots[first]["id"],
            bots[second]["id"],
            owner_id=users[first]["id"],
            contest_id=contest["id"],
            match_type="contest",
            game_id="holdem",
            match_config={"duplicate": False},
        )
        store.bind_contest_pairing_match(
            contest["id"],
            pairing["id"],
            match_id,
            require_execution_admission=False,
        )
        signed = margin if winner == 0 else -margin
        store.update_match(
            match_id,
            status="completed",
            winner=winner,
            result={"rounds_played": 70, "deltas": [signed, -signed]},
            ended_at=f"2026-08-28T12:{ordinal:02d}:00+08:00",
        )
        store.complete_contest_pairing_for_match(contest["id"], match_id)
    assert store.contest_stage_manifest_is_valid(contest["id"], 0)

    client = TestClient(app)
    detail = client.get(f"/api/contests/{contest['id']}").json()
    live = client.get(f"/api/contests/{contest['id']}/live").json()
    detail_ids = [row["bot_id"] for row in detail["standings"]]
    live_ids = [row["bot_id"] for row in live["standings"]]
    stage_ids = [
        row["bot_id"] for row in detail["stage_standings"][0]["rows"]
    ]

    expected_bot = bots[expected_first]["id"]
    other_bot = bots[1 - expected_first]["id"]
    assert detail_ids.index(expected_bot) < detail_ids.index(other_bot)
    assert live_ids.index(expected_bot) < live_ids.index(other_bot)
    assert stage_ids.index(expected_bot) < stage_ids.index(other_bot)
    row_a = next(row for row in detail["standings"] if row["bot_id"] == bots[0]["id"])
    row_b = next(row for row in detail["standings"] if row["bot_id"] == bots[1]["id"])
    expected_row = row_a if expected_first == 0 else row_b
    other_row = row_b if expected_first == 0 else row_a
    assert other_row["delta_total"] > expected_row["delta_total"]
    assert (
        expected_row["tiebreaks"]["buchholz_cut1"]
        > other_row["tiebreaks"]["buchholz_cut1"]
    )

    # Stage completion must freeze the same tie-break chain that selected
    # advancement.  Persisting only points/rank would make the completed-stage
    # detail lose Cut1/SB/H2H even though its order remains frozen.
    stage_before = detail["stage_standings"][0]
    assert stage_before["source"] == "live"
    tiebreaks_before = {
        row["entry_id"]: row["tiebreaks"] for row in stage_before["rows"]
    }
    app.state.contest_manager._snapshot_stage_results(contest["id"], 0)

    stored_rows = store.list_stage_results(contest["id"], stage_idx=0)
    assert {
        row["entry_id"]: row["tiebreaks"] for row in stored_rows
    } == tiebreaks_before
    assert all("payload_json" not in row for row in stored_rows)
    with store._tx() as conn:
        raw_payloads = [
            json.loads(row["payload_json"])
            for row in conn.execute(
                "SELECT payload_json FROM contest_stage_results "
                "WHERE contest_id=? AND stage_idx=0 ORDER BY entry_id",
                (contest["id"],),
            ).fetchall()
        ]
    assert all(set(payload) == {"tiebreaks"} for payload in raw_payloads)
    assert all(
        set(payload["tiebreaks"]) == {
            "points",
            "buchholz",
            "buchholz_cut1",
            "sonneborn_berger",
            "head_to_head",
            "normalized_delta",
            "technical_losses",
            "seed",
        }
        for payload in raw_payloads
    )

    persisted_response = client.get(f"/api/contests/{contest['id']}")
    assert persisted_response.status_code == 200
    persisted_detail = persisted_response.json()
    stage_after = persisted_detail["stage_standings"][0]
    assert stage_after["source"] == "persisted"
    assert {
        row["entry_id"]: row["tiebreaks"] for row in stage_after["rows"]
    } == tiebreaks_before
    assert not ({"payload_json", "private_metric"} & _all_keys(persisted_detail))

    # Imported/damaged payloads are parsed fail-closed.  Unknown envelope and
    # tie-break fields never cross the public projection; malformed JSON does
    # not turn the detail endpoint into a 500.
    victim_entry_id = entries[0]["id"]
    with store._tx() as conn:
        payload = {
            "tiebreaks": {
                **tiebreaks_before[victim_entry_id],
                "private_metric": 999,
            },
            "private_metric": "do-not-publish",
        }
        conn.execute(
            "UPDATE contest_stage_results SET payload_json=? "
            "WHERE contest_id=? AND stage_idx=0 AND entry_id=?",
            (json.dumps(payload), contest["id"], victim_entry_id),
        )
        conn.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest["id"],),
        )
    bounded = client.get(f"/api/contests/{contest['id']}")
    assert bounded.status_code == 200
    bounded_payload = bounded.json()
    bounded_victim = next(
        row
        for row in bounded_payload["stage_standings"][0]["rows"]
        if row["entry_id"] == victim_entry_id
    )
    assert bounded_victim["tiebreaks"] == tiebreaks_before[victim_entry_id]
    assert not ({"payload_json", "private_metric"} & _all_keys(bounded_payload))
    assert "do-not-publish" not in bounded.text

    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_stage_results SET payload_json='{malformed' "
            "WHERE contest_id=? AND stage_idx=0 AND entry_id=?",
            (contest["id"], victim_entry_id),
        )
        conn.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest["id"],),
        )
    malformed = client.get(f"/api/contests/{contest['id']}")
    assert malformed.status_code == 200
    malformed_payload = malformed.json()
    assert malformed_payload["stage_standings"][0]["rows"] == []
    assert malformed_payload["standings"] == []


def test_contest_detail_and_bracket_do_not_replay_or_n_plus_one_matches(
    tmp_path, monkeypatch
):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    client = TestClient(app)

    def unexpected(*_args, **_kwargs):
        raise AssertionError("contest public projection performed an item lookup")

    for method in (
        "get_contest",
        "list_contest_entries",
        "list_contest_pairings",
        "contest_entries_named",
        "list_stage_results",
        "get_entry",
        "get_match",
        "get_bot",
    ):
        monkeypatch.setattr(store, method, unexpected)
    for path in (
        f"/api/contests/{contest['id']}",
        f"/api/contests/{contest['id']}/bracket",
    ):
        traced: list[str] = []
        store._conn.set_trace_callback(traced.append)
        try:
            response = client.get(path)
        finally:
            store._conn.set_trace_callback(None)
        assert response.status_code == 200, response.text
        selects = [
            sql for sql in traced if sql.lstrip().upper().startswith("SELECT")
        ]
        assert len(selects) == (4 if path.endswith(str(contest["id"])) else 2), selects
        assert not any("match_replays" in sql for sql in selects)
        assert not any("json_each" in sql or "events_json" in sql for sql in selects)
        assert not ({"result", "events"} & _all_keys(response.json()))
        assert not any(key.startswith("_") for key in _all_keys(response.json()))


def test_injected_standings_keeps_snapshot_scoring_after_stage_drift(tmp_path, monkeypatch):
    app, contest, _entries, _bots = _live_fixture(tmp_path)
    store = app.state.store
    snapshot = store.contest_live_snapshot(contest["id"])
    assert snapshot is not None
    participant_ids = {
        entry_id
        for pairing in snapshot["pairings"]
        for entry_id in (pairing.get("entry_a_id"), pairing.get("entry_b_id"))
        if entry_id is not None
    }
    entries = [
        entry for entry in snapshot["entries"] if entry["id"] in participant_ids
    ]
    store.update_contest(
        contest["id"],
        stages_json=json.dumps(
            [
                {
                    "key": "changed-after-snapshot",
                    "type": "round_robin",
                    "scoring": "ccgc_2_1_0",
                }
            ]
        ),
    )

    def unexpected(*_args, **_kwargs):
        raise AssertionError("standings re-read Store after snapshot")

    monkeypatch.setattr(store, "get_contest", unexpected)
    monkeypatch.setattr(store, "list_contest_entries", unexpected)
    monkeypatch.setattr(store, "get_match", unexpected)
    standings = ContestManager(store, _ReadOnlyOrchestrator()).standings(
        contest["id"],
        contest=snapshot["contest"],
        pairings=snapshot["pairings"],
        entries=entries,
    )
    # Frozen poker scoring: two duplicate winning legs x 3 points, not the
    # post-snapshot ccgc score of 2 points per leg.
    assert standings[0]["points"] == 6


def _authenticated_organizer(
    store: Store, app, name: str, *, role: str = "organizer"
):
    user = store.create_user(
        name,
        f"{name}@example.com",
        hash_password("pw123456"),
        role=role,
    )
    store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate(name, "pw123456")
    return user, {"Authorization": f"Bearer {token}"}


def test_live_hidden_acl_404_is_private_no_store(tmp_path):
    app = create_app(db_path=str(tmp_path / "live-acl.db"))
    store = app.state.store
    owner, owner_headers = _authenticated_organizer(store, app, "live-acl-owner")
    _other, other_headers = _authenticated_organizer(store, app, "live-acl-other")
    _admin, admin_headers = _authenticated_organizer(
        store, app, "live-acl-admin", role="admin"
    )
    contest = store.create_contest(
        "Hidden live",
        owner["id"],
        status="draft",
        game_id="holdem",
        template_id="holdem_rr",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )
    client = TestClient(app)

    for headers in (None, other_headers):
        response = client.get(
            f"/api/contests/{contest['id']}/live", headers=headers
        )
        assert response.status_code == 404
        assert response.headers["cache-control"] == "private, no-store, max-age=0"
        assert "Authorization" in response.headers["vary"]
        assert "Cookie" in response.headers["vary"]
    allowed = client.get(
        f"/api/contests/{contest['id']}/live", headers=owner_headers
    )
    assert allowed.status_code == 200
    assert allowed.json()["standings"] == []
    admin_allowed = client.get(
        f"/api/contests/{contest['id']}/live", headers=admin_headers
    )
    assert admin_allowed.status_code == 200

    missing = client.get("/api/contests/999999999/live")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "private, no-store, max-age=0"
    assert "Authorization" in missing.headers["vary"]
    assert "Cookie" in missing.headers["vary"]


def test_live_group_ranks_showcase_and_current_stage_roster_filter(tmp_path):
    app = create_app(db_path=str(tmp_path / "live-groups.db"))
    store = app.state.store
    users, bots = _people(store, tmp_path, 4, prefix="groups")
    contest = store.create_contest(
        "Grouped showcase",
        users[0]["id"],
        status="open",
        game_id="holdem",
        stages_json=json.dumps(
            [
                {
                    "key": "groups",
                    "type": "group_round_robin",
                    "group_count": 2,
                    "scoring": "poker_3_1_0",
                }
            ]
        ),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    for index, entry in enumerate(entries):
        store.update_entry(
            contest["id"], users[index]["id"], group_id="A" if index < 2 else "B"
        )
    for ordinal, (first, second, group_id) in enumerate(
        ((0, 1, "A"), (2, 3, "B")), start=1
    ):
        store.add_pairing(
            contest["id"],
            bots[first]["id"],
            bots[second]["id"],
            entry_a_id=entries[first]["id"],
            entry_b_id=entries[second]["id"],
            stage_key="groups",
            group_id=group_id,
            round_num=ordinal,
        )
    store.update_contest(contest["id"], status="published")
    grouped_pairings = store.list_contest_pairings(contest["id"], stage_idx=0)
    store.seal_published_stage_pairing_count(
        contest["id"],
        0,
        expected_count=len(grouped_pairings),
        expected_existing_ids=[int(row["id"]) for row in grouped_pairings],
    )
    store.update_contest(
        contest["id"], status="running", official_results_ready=1
    )
    store.freeze_contest_showcase(contest["id"], "grouped-live-showcase")

    client = TestClient(app)
    payload = client.get(f"/api/contests/{contest['id']}/live").json()
    assert payload["contest"]["showcase"] is True
    assert payload["contest"]["immutable"] is True
    assert payload["contest"]["official_results_ready"] is True
    ranks_by_group: dict[str, list[int]] = {}
    for row in payload["standings"]:
        ranks_by_group.setdefault(row["group_id"], []).append(row["rank"])
    assert ranks_by_group == {"A": [1, 2], "B": [1, 2]}

    # A direct-Store later stage with no persisted predecessor decision cannot
    # use its active flags or surviving pairing endpoints as cohort authority.
    later = store.create_contest(
        "Filtered final",
        users[0]["id"],
        status="running",
        game_id="holdem",
        current_stage_idx=1,
        stages_json=json.dumps(
            [
                {"key": "qualifier", "type": "round_robin"},
                {"key": "final", "type": "round_robin"},
            ]
        ),
    )
    later_entries = [
        store.add_contest_entry(later["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    store.add_pairing(
        later["id"],
        bots[1]["id"],
        bots[3]["id"],
        entry_a_id=later_entries[1]["id"],
        entry_b_id=later_entries[3]["id"],
        stage_idx=1,
        stage_key="final",
    )
    filtered = client.get(f"/api/contests/{later['id']}/live").json()
    assert filtered["standings"] == []

    empty = store.create_contest(
        "No pairings yet",
        users[0]["id"],
        status="open",
        game_id="holdem",
        stages_json='[{"key":"rr","type":"round_robin"}]',
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(empty["id"], user["id"], bot["id"])
    assert client.get(f"/api/contests/{empty['id']}/live").json()["standings"] == []

    # Historical SQLite schemas do not CHECK the seed type.  A malformed
    # imported value must invalidate the computed ranking without turning
    # either public read model into a 500 response.
    with store._tx() as connection:
        connection.execute(
            "UPDATE contest_entries SET seed=? WHERE id=?",
            ("bad", later_entries[0]["id"]),
        )
    damaged_detail = client.get(f"/api/contests/{later['id']}")
    damaged_live = client.get(f"/api/contests/{later['id']}/live")
    assert damaged_detail.status_code == 200
    assert damaged_live.status_code == 200
    assert damaged_detail.json()["standings"] == []
    assert damaged_live.json()["standings"] == []
