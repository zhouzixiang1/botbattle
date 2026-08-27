"""Dedicated contest live spectator projection contracts."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


class _ReadOnlyOrchestrator:
    max_concurrent = 2


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


def _bind_match(
    store: Store,
    contest_id: int,
    pairing: dict,
    match_id: str,
    *,
    status: str,
    duplicate: bool = False,
) -> None:
    store.create_match(
        match_id,
        pairing["bot_a_id"],
        pairing["bot_b_id"],
        owner_id=pairing["bot_a_id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
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
                "deltas": [20, -20],
                "legs": [
                    {"winner": 0, "deltas": [10, -10]},
                    {"winner": 0, "deltas": [10, -10]},
                ],
            }
            if duplicate
            else {"deltas": [10, -10]}
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
    pairs = [(0, 1), (0, 2), (0, 3), (1, 2)]
    pairings = []
    for ordinal, (first, second) in enumerate(pairs, start=1):
        pairings.append(
            store.add_pairing(
                contest["id"],
                bots[first]["id"],
                bots[second]["id"],
                entry_a_id=entries[first]["id"],
                entry_b_id=entries[second]["id"],
                stage_idx=0,
                stage_key="dup_rr",
                round_num=ordinal,
                pairing_seed=7000 + ordinal,
                published_at="2026-08-27T10:00:00+08:00",
                scheduled_at=(
                    "2099-12-31T23:59:59" if ordinal == 4 else None
                ),
            )
        )
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
    store.update_contest(contest["id"], status="running")
    return app, contest, entries, bots


def _all_keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | set().union(*(_all_keys(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_all_keys(item) for item in value)) if value else set()
    return set()


def test_live_projection_is_three_select_snapshot_without_n_plus_one(
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
    assert len(selects) == 3, selects
    assert any("FROM contests" in sql for sql in selects)
    assert any("FROM contest_pairings" in sql and "m.result" in sql for sql in selects)
    assert any("FROM contest_entries" in sql for sql in selects)

    payload = response.json()
    assert payload["progress"] == {
        "completed": 1,
        "total": 4,
        "running": 1,
        "pending": 2,
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
        "scoring_legs_per_match": 2,
        "scoring_legs_per_pair": 2,
    }
    assert payload["contest"]["showcase"] is False
    assert payload["contest"]["immutable"] is False
    assert payload["contest"]["official_results_ready"] is False
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert "Authorization" in response.headers["vary"]
    assert "Cookie" in response.headers["vary"]

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
            [{"key": "groups", "type": "group_round_robin", "group_count": 2}]
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
    store.update_contest(contest["id"], official_results_ready=1)
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

    # A later stage only includes its persisted pairing participants.  The two
    # non-qualifiers must not reappear as zero-point leaders.
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
    assert {row["bot_id"] for row in filtered["standings"]} == {
        bots[1]["id"],
        bots[3]["id"],
    }

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
