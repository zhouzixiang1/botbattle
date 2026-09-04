"""Deterministic, public single-match log export contract."""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app


def _make_user_bot(store, username: str, *, game_id: str):
    user = store.create_user(
        username,
        f"{username}@example.test",
        hash_password("pw123456"),
    )
    store.update_user(user["id"], email_verified=1)
    bot = store.create_bot(
        user["id"],
        f"{username}-bot",
        display_name=f"{username} display",
        binary_path=f"/private/bot_uploads/{username}/binary",
        format="elf",
        game_id=game_id,
    )
    return user, bot


def _fixture(tmp_path):
    app = create_app(db_path=str(tmp_path / "match-log.db"))
    store = app.state.store
    users: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for game_id in ("holdem", "gomoku", "pencil"):
        users[f"{game_id}_a"] = _make_user_bot(
            store, f"log_{game_id}_a", game_id=game_id
        )
        users[f"{game_id}_b"] = _make_user_bot(
            store, f"log_{game_id}_b", game_id=game_id
        )
    admin = store.create_user(
        "log_admin",
        "log_admin@example.test",
        hash_password("pw123456"),
        role="admin",
    )
    store.update_user(admin["id"], email_verified=1)
    return TestClient(app), store, users, admin


def _events(game_id: str, *, terminal: str = "match_end") -> list[dict[str, Any]]:
    starts: dict[str, dict[str, Any]] = {
        "holdem": {"type": "match_start", "game_id": "holdem", "num_hands": 70},
        "gomoku": {"type": "match_start", "game_id": "gomoku", "size": 15},
        "pencil": {"type": "match_start", "game_id": "pencil", "n_dots": 6},
    }
    actions: dict[str, dict[str, Any]] = {
        "holdem": {
            "type": "action",
            "hand": 1,
            "player": 0,
            "action": "call",
            "amount": 100,
        },
        "gomoku": {
            "type": "move",
            "player": 0,
            "color": 0,
            "x": 7,
            "y": 7,
            "move_index": 1,
        },
        "pencil": {
            "type": "move",
            "player": 0,
            "x": 0,
            "y": 0,
            "scored": False,
            "scores": [0, 0],
        },
    }
    end = (
        {"type": "error", "reason": "platform_error"}
        if terminal == "error"
        else {
            "type": "match_end",
            "winner": 0,
            "reason": "completed",
            "deltas": [1, -1],
        }
    )
    return [starts[game_id], actions[game_id], end]


def _terminal_match(
    store,
    users,
    game_id: str,
    match_id: str,
    *,
    status: str = "completed",
    events: list[dict[str, Any]] | None = None,
):
    bot_a = users[f"{game_id}_a"][1]
    bot_b = users[f"{game_id}_b"][1]
    store.create_match(
        match_id,
        bot_a["id"],
        bot_b["id"],
        game_id=game_id,
        match_type="challenge",
    )
    if status == "completed":
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            reason="completed",
            result={
                "rounds_played": 1,
                "deltas": [1, -1],
                "normalized_delta": 1,
            },
            ended_at="2026-08-20T10:00:00",
        )
    elif status == "aborted":
        store.update_match(
            match_id,
            status="aborted",
            winner=None,
            reason="platform_error",
            ended_at="2026-08-20T10:00:00",
        )
    else:
        store.update_match(match_id, status=status)
    if events is not None:
        store.upsert_replay(match_id, json.dumps(events, ensure_ascii=False))
    return match_id


def _running_contest_match(store, users, match_id: str):
    user_a, bot_a = users["gomoku_a"]
    user_b, bot_b = users["gomoku_b"]
    contest = store.create_contest(
        f"recovery-{match_id}",
        user_a["id"],
        status="running",
        game_id="gomoku",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    entry_a = store.add_contest_entry(contest["id"], user_a["id"], bot_a["id"])
    entry_b = store.add_contest_entry(contest["id"], user_b["id"], bot_b["id"])
    pairing = store.add_contest_pairing(
        contest["id"],
        bot_a["id"],
        bot_b["id"],
        status="pending",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=entry_a["id"],
        entry_b_id=entry_b["id"],
        published_at="2026-09-03T00:00:00",
        scheduled_at="2026-09-03T00:00:00",
    )
    # This helper intentionally builds a low-level recovery state instead of
    # running the dispatcher.  Give its complete two-entry RR batch the same
    # exact manifest/revision proof a live contest must have before bind/reset;
    # leaving it unsealed would only exercise the active lifecycle guard.
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE contests SET published_stage_pairing_count=1 WHERE id=?",
            (contest["id"],),
        )
        # Updating the manifest advances the lifecycle revision via trigger;
        # seal only after that revision is durable inside this transaction.
        conn.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest["id"],),
        )
    store.create_match(
        match_id,
        bot_a["id"],
        bot_b["id"],
        owner_id=user_a["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="gomoku",
    )
    store.update_match(match_id, status="running")
    store.bind_contest_pairing_match(
        contest["id"],
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    return contest, pairing


def _pending_contest_pairing(store, users, *, status: str = "published"):
    user_a, bot_a = users["gomoku_a"]
    user_b, bot_b = users["gomoku_b"]
    contest = store.create_contest(
        f"pending-technical-{status}",
        user_a["id"],
        status=status,
        game_id="gomoku",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    entry_a = store.add_contest_entry(contest["id"], user_a["id"], bot_a["id"])
    entry_b = store.add_contest_entry(contest["id"], user_b["id"], bot_b["id"])
    pairing = store.add_contest_pairing(
        contest["id"],
        bot_a["id"],
        bot_b["id"],
        status="pending",
        stage_idx=0,
        stage_key="rr",
        entry_a_id=entry_a["id"],
        entry_b_id=entry_b["id"],
    )
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE contests SET published_stage_pairing_count=1 WHERE id=?",
            (contest["id"],),
        )
        conn.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest["id"],),
        )
    return store.get_contest(contest["id"]), pairing


@pytest.mark.parametrize("game_id", ["holdem", "gomoku", "pencil"])
def test_terminal_log_is_deterministic_public_replay_for_every_game(
    tmp_path, game_id
):
    client, store, users, _ = _fixture(tmp_path)
    match_id = f"{game_id}-completed-log"
    _terminal_match(
        store,
        users,
        game_id,
        match_id,
        events=_events(game_id),
    )

    with store._tx() as conn:
        before_dump = tuple(conn.iterdump())

    first = client.get(f"/api/matches/{match_id}/log")
    second = client.get(f"/api/matches/{match_id}/log")
    replay = client.get(f"/api/matches/{match_id}/replay")

    with store._tx() as conn:
        after_dump = tuple(conn.iterdump())

    assert first.status_code == replay.status_code == 200
    assert first.content == second.content
    assert after_dump == before_dump
    assert first.headers["content-type"] == "application/json"
    assert first.headers["content-disposition"] == (
        f'attachment; filename="botbattle-{game_id}-{match_id}-log.json"'
    )
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert not first.content.startswith(b"\xef\xbb\xbf")
    assert first.content.endswith(b"\n")
    assert not first.content.endswith(b"\n\n")

    exported = first.json()
    assert set(exported) == {"format", "format_version", "match", "replay"}
    assert exported["format"] == "botbattle.match.log"
    assert exported["format_version"] == 1
    assert exported["match"]["id"] == match_id
    assert exported["match"]["game_id"] == game_id
    assert exported["match"]["bot_a"]["owner_name"] == f"log_{game_id}_a"
    assert exported["match"]["bot_b"]["owner_name"] == f"log_{game_id}_b"
    assert exported["replay"] == replay.json()

    if game_id != "gomoku":
        # A platform log is available independently of the optional game-level
        # record exporter, which remains Gomoku-only.
        assert client.get(f"/api/matches/{match_id}/record").status_code == 409


@pytest.mark.parametrize("game_id", ["holdem", "gomoku", "pencil"])
def test_aborted_log_uses_the_same_authoritative_public_error(tmp_path, game_id):
    client, store, users, _ = _fixture(tmp_path)
    match_id = f"{game_id}-aborted-log"
    private = "traceback /private/runtime/bot.bin token=secret"
    events = _events(game_id, terminal="error")
    events[-1] = {"type": "error", "reason": private, "stderr": private}
    _terminal_match(
        store,
        users,
        game_id,
        match_id,
        status="aborted",
        events=events,
    )

    response = client.get(f"/api/matches/{match_id}/log")
    replay = client.get(f"/api/matches/{match_id}/replay").json()
    assert response.status_code == 200
    assert response.json()["replay"] == replay
    assert replay["events"][-1] == {"type": "error", "reason": "platform_error"}
    assert private not in response.text


@pytest.mark.parametrize(
    "interruption_reason",
    (
        "orphan_after_service_restart",
        "orphan_after_runtime_recovery",
    ),
)
@pytest.mark.parametrize("recovery_path", ("orphan_scan", "contest_reset"))
def test_recovery_paths_atomically_finalize_replay_and_enable_log_export(
    tmp_path,
    interruption_reason,
    recovery_path,
):
    client, store, users, _ = _fixture(tmp_path)
    match_id = f"{recovery_path}-{interruption_reason}"
    prefix: list[dict[str, Any]] = []
    if recovery_path == "orphan_scan":
        user_a, bot_a = users["gomoku_a"]
        bot_b = users["gomoku_b"][1]
        store.create_match(
            match_id,
            bot_a["id"],
            bot_b["id"],
            owner_id=user_a["id"],
            game_id="gomoku",
            match_type="challenge",
        )
        store.update_match(match_id, status="running")
        prior = _events("gomoku", terminal="error")
        prefix = prior[:-1]
        store.upsert_replay(match_id, json.dumps(prior, ensure_ascii=False))
        assert store.recover_orphan_matches(
            interruption_reason=interruption_reason
        ) == 1
    else:
        _running_contest_match(store, users, match_id)
        assert store.get_replay(match_id) is None
        assert store.reset_dead_contest_pairings(
            interruption_reason=interruption_reason
        ) == 1

    match = store.get_match(match_id)
    terminal = {"type": "error", "reason": interruption_reason}
    assert (match["status"], match["reason"]) == (
        "aborted",
        interruption_reason,
    )
    assert json.loads(store.get_replay(match_id)["events_json"]) == [
        *prefix,
        terminal,
    ]
    raw_replay = store.get_replay(match_id)["events_json"]
    assert (
        store.recover_orphan_matches(interruption_reason=interruption_reason)
        if recovery_path == "orphan_scan"
        else store.reset_dead_contest_pairings(
            interruption_reason=interruption_reason
        )
    ) == 0
    assert store.get_replay(match_id)["events_json"] == raw_replay
    source = store.get_match_record_source(match_id)
    assert source is not None and source["replay_finalized"] is True
    response = client.get(f"/api/matches/{match_id}/log")
    assert response.status_code == 200
    exported = response.json()
    assert exported["match"]["reason"] == interruption_reason
    assert exported["replay"]["events"][-1] == terminal


@pytest.mark.parametrize(
    ("interruption_reason", "pending_reason"),
    (
        (
            "orphan_after_service_restart",
            "orphan_pending_after_service_restart",
        ),
        (
            "orphan_after_runtime_recovery",
            "orphan_pending_after_runtime_recovery",
        ),
    ),
)
def test_pending_orphan_recovery_finalizes_replay_and_log(
    tmp_path,
    interruption_reason,
    pending_reason,
):
    client, store, users, _ = _fixture(tmp_path)
    user_a, bot_a = users["gomoku_a"]
    bot_b = users["gomoku_b"][1]
    match_id = f"pending-{interruption_reason}"
    store.create_match(
        match_id,
        bot_a["id"],
        bot_b["id"],
        owner_id=user_a["id"],
        game_id="gomoku",
        match_type="challenge",
    )
    assert store.get_replay(match_id) is None

    assert store.recover_orphan_matches(
        interruption_reason=interruption_reason
    ) == 1

    terminal = {"type": "error", "reason": pending_reason}
    match = store.get_match(match_id)
    assert (match["status"], match["reason"]) == ("aborted", pending_reason)
    assert json.loads(store.get_replay(match_id)["events_json"]) == [terminal]
    raw_replay = store.get_replay(match_id)["events_json"]
    assert store.recover_orphan_matches(
        interruption_reason=interruption_reason
    ) == 0
    assert store.get_replay(match_id)["events_json"] == raw_replay
    response = client.get(f"/api/matches/{match_id}/log")
    assert response.status_code == 200
    assert response.json()["replay"]["events"][-1] == terminal


@pytest.mark.parametrize("recovery_path", ("orphan_scan", "contest_reset"))
def test_recovery_rebuilds_malformed_replay_as_audited_terminal_only(
    tmp_path,
    recovery_path,
    caplog,
):
    client, store, users, _ = _fixture(tmp_path)
    match_id = f"malformed-{recovery_path}"
    if recovery_path == "orphan_scan":
        user_a, bot_a = users["gomoku_a"]
        bot_b = users["gomoku_b"][1]
        store.create_match(
            match_id,
            bot_a["id"],
            bot_b["id"],
            owner_id=user_a["id"],
            game_id="gomoku",
            match_type="challenge",
        )
        store.update_match(match_id, status="running")
        pairing_before = None
    else:
        contest, _pairing = _running_contest_match(store, users, match_id)
        pairing_before = store.list_contest_pairings(contest["id"])
    store.upsert_replay(match_id, "{")

    recovered = (
        store.recover_orphan_matches(
            interruption_reason="orphan_after_service_restart"
        )
        if recovery_path == "orphan_scan"
        else store.reset_dead_contest_pairings(
            interruption_reason="orphan_after_service_restart"
        )
    )

    match = store.get_match(match_id)
    terminal = {"type": "error", "reason": "orphan_after_service_restart"}
    assert recovered == 1
    assert (match["status"], match["reason"]) == (
        "aborted",
        "orphan_after_service_restart",
    )
    assert json.loads(store.get_replay(match_id)["events_json"]) == [terminal]
    if recovery_path == "contest_reset":
        after = store.list_contest_pairings(contest["id"])
        assert after != pairing_before
        assert (after[0]["status"], after[0]["match_id"]) == ("pending", None)
    assert any(
        "recovery_replay_rebuilt" in record.message
        and match_id in record.message
        for record in caplog.records
    )
    assert "{" not in caplog.text
    assert client.get(f"/api/matches/{match_id}/log").status_code == 200


@pytest.mark.parametrize("recovery_path", ("orphan_scan", "contest_reset"))
def test_recovery_replay_write_failure_rolls_back_match_and_pairing(
    tmp_path,
    recovery_path,
):
    _client, store, users, _ = _fixture(tmp_path)
    match_id = f"replay-write-failure-{recovery_path}"
    if recovery_path == "orphan_scan":
        user_a, bot_a = users["gomoku_a"]
        bot_b = users["gomoku_b"][1]
        store.create_match(
            match_id,
            bot_a["id"],
            bot_b["id"],
            owner_id=user_a["id"],
            game_id="gomoku",
            match_type="challenge",
        )
        store.update_match(match_id, status="running")
        pairing_before = None
    else:
        contest, _pairing = _running_contest_match(store, users, match_id)
        pairing_before = store.list_contest_pairings(contest["id"])
    with store._tx() as conn:
        conn.execute(
            "CREATE TRIGGER fail_recovery_replay BEFORE INSERT ON match_replays "
            f"WHEN NEW.match_id='{match_id}' BEGIN "
            "SELECT RAISE(ABORT, 'forced replay failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="forced replay failure"):
        if recovery_path == "orphan_scan":
            store.recover_orphan_matches(
                interruption_reason="orphan_after_service_restart"
            )
        else:
            store.reset_dead_contest_pairings(
                interruption_reason="orphan_after_service_restart"
            )

    match = store.get_match(match_id)
    assert (match["status"], match["reason"]) == ("running", "")
    assert store.get_replay(match_id) is None
    if recovery_path == "contest_reset":
        assert store.list_contest_pairings(contest["id"]) == pairing_before


@pytest.mark.parametrize("terminal_status", ("completed", "aborted"))
@pytest.mark.parametrize("replay_shape", ("missing", "stale", "malformed"))
def test_contest_reset_repairs_terminal_match_replay_before_pairing_decision(
    tmp_path,
    terminal_status,
    replay_shape,
):
    client, store, users, _ = _fixture(tmp_path)
    match_id = f"terminal-contest-{terminal_status}-{replay_shape}"
    contest, _pairing = _running_contest_match(store, users, match_id)
    prefix = [{"type": "match_start", "game_id": "gomoku", "size": 15}]
    authoritative_terminal = (
        {
            "type": "match_end",
            "winner": 0,
            "reason": "five",
            "deltas": [3, -3],
        }
        if terminal_status == "completed"
        else {"type": "error", "reason": "orphan_after_restart"}
    )
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET status=?,winner=?,reason=?,result=?,ended_at=? "
            "WHERE id=?",
            (
                terminal_status,
                0 if terminal_status == "completed" else None,
                "five" if terminal_status == "completed" else "orphan_after_restart",
                json.dumps({"rounds_played": 1, "deltas": [3, -3]}),
                "2026-09-02T10:00:00",
                match_id,
            ),
        )
        if replay_shape == "missing":
            conn.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
        elif replay_shape == "stale":
            conn.execute(
                "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET events_json=excluded.events_json",
                (
                    match_id,
                    json.dumps(
                        [
                            *prefix,
                            {
                                "type": "match_end",
                                "winner": 1,
                                "reason": "completed",
                                "deltas": [-99, 99],
                            },
                            {"type": "error", "reason": "platform_error"},
                        ]
                    ),
                    "2026-09-02T09:00:00",
                ),
            )
        else:
            conn.execute(
                "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET events_json=excluded.events_json",
                (match_id, "{", "2026-09-02T09:00:00"),
            )

    recovered = store.reset_dead_contest_pairings(
        interruption_reason="orphan_after_service_restart"
    )

    assert recovered == (0 if terminal_status == "completed" else 1)
    expected_events = (
        [*prefix, authoritative_terminal]
        if replay_shape == "stale"
        else [authoritative_terminal]
    )
    repaired = store.get_replay(match_id)
    assert json.loads(repaired["events_json"]) == expected_events
    stored_once = (repaired["events_json"], repaired["updated_at"])
    assert store.reset_dead_contest_pairings(
        interruption_reason="orphan_after_service_restart"
    ) == 0
    repaired_twice = store.get_replay(match_id)
    assert (repaired_twice["events_json"], repaired_twice["updated_at"]) == stored_once
    pairing = store.list_contest_pairings(contest["id"])[0]
    assert (pairing["status"], pairing["match_id"]) == (
        ("running", match_id)
        if terminal_status == "completed"
        else ("pending", None)
    )
    source = store.get_match_record_source(match_id)
    assert source is not None and source["replay_finalized"] is True
    exported = client.get(f"/api/matches/{match_id}/log")
    assert exported.status_code == 200
    assert exported.json()["replay"]["events"][-1] == authoritative_terminal


@pytest.mark.parametrize("terminal_status", ("completed", "aborted"))
def test_contest_terminal_replay_failure_rolls_back_pairing_recovery(
    tmp_path,
    terminal_status,
):
    _client, store, users, _ = _fixture(tmp_path)
    match_id = f"terminal-contest-replay-failure-{terminal_status}"
    contest, _pairing = _running_contest_match(store, users, match_id)
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET status=?,winner=?,reason=?,result=? WHERE id=?",
            (
                terminal_status,
                0 if terminal_status == "completed" else None,
                "five" if terminal_status == "completed" else "orphan_after_restart",
                json.dumps({"rounds_played": 1, "deltas": [3, -3]}),
                match_id,
            ),
        )
        conn.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
        conn.execute(
            "CREATE TRIGGER fail_terminal_contest_replay BEFORE INSERT ON match_replays "
            f"WHEN NEW.match_id='{match_id}' BEGIN "
            "SELECT RAISE(ABORT, 'forced terminal contest replay failure'); END"
        )
    pairing_before = store.list_contest_pairings(contest["id"])

    with pytest.raises(
        sqlite3.IntegrityError, match="forced terminal contest replay failure"
    ):
        store.reset_dead_contest_pairings(
            interruption_reason="orphan_after_service_restart"
        )

    assert store.list_contest_pairings(contest["id"]) == pairing_before
    assert store.get_match(match_id)["status"] == terminal_status
    assert store.get_replay(match_id) is None


@pytest.mark.parametrize("replay_shape", ("missing", "stale", "malformed"))
def test_pairing_completion_persists_match_authoritative_replay_before_status(
    tmp_path,
    replay_shape,
):
    client, store, users, _ = _fixture(tmp_path)
    match_id = f"pairing-completion-replay-{replay_shape}"
    contest, _pairing = _running_contest_match(store, users, match_id)
    prefix = [{"type": "match_start", "game_id": "gomoku", "size": 15}]
    terminal = {
        "type": "match_end",
        "winner": 1,
        "reason": "five",
        "deltas": [-4, 4],
    }
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET status='completed',winner=1,reason='five',"
            "result=?,ended_at=? WHERE id=?",
            (
                json.dumps({"deltas": [-4, 4]}),
                "2026-09-02T10:00:00",
                match_id,
            ),
        )
        if replay_shape == "missing":
            conn.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
        elif replay_shape == "stale":
            conn.execute(
                "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET events_json=excluded.events_json",
                (
                    match_id,
                    json.dumps(
                        [
                            *prefix,
                            {
                                "type": "match_end",
                                "winner": 0,
                                "reason": "completed",
                                "deltas": [99, -99],
                            },
                        ]
                    ),
                    "2026-09-02T09:00:00",
                ),
            )
        else:
            conn.execute(
                "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET events_json=excluded.events_json",
                (match_id, "{", "2026-09-02T09:00:00"),
            )

    completed = store.complete_contest_pairing_for_match(contest["id"], match_id)

    assert completed is not None and completed["status"] == "completed"
    expected = [*prefix, terminal] if replay_shape == "stale" else [terminal]
    replay = store.get_replay(match_id)
    assert json.loads(replay["events_json"]) == expected
    stored_once = (replay["events_json"], replay["updated_at"])
    assert store.complete_contest_pairing_for_match(contest["id"], match_id)
    replay_twice = store.get_replay(match_id)
    assert (replay_twice["events_json"], replay_twice["updated_at"]) == stored_once
    source = store.get_match_record_source(match_id)
    assert source is not None and source["replay_finalized"] is True
    exported = client.get(f"/api/matches/{match_id}/log")
    assert exported.status_code == 200
    assert exported.json()["replay"]["events"][-1] == terminal


def test_pairing_completion_does_not_rewrite_existing_canonical_terminal(tmp_path):
    _client, store, users, _ = _fixture(tmp_path)
    match_id = "pairing-completion-canonical-no-write"
    contest, _pairing = _running_contest_match(store, users, match_id)
    terminal = {
        "type": "match_end",
        "winner": 0,
        "reason": "five",
        "deltas": [2, -2],
    }
    raw_replay = json.dumps(
        [{"type": "match_start", "game_id": "gomoku"}, terminal],
        ensure_ascii=False,
    )
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET status='completed',winner=0,reason='five',"
            "result=? WHERE id=?",
            (json.dumps({"deltas": [2, -2]}), match_id),
        )
        conn.execute(
            "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?,?,?)",
            (match_id, raw_replay, "2026-09-02T09:00:00"),
        )
        conn.execute(
            "CREATE TRIGGER reject_canonical_replay_update BEFORE UPDATE ON match_replays "
            f"WHEN OLD.match_id='{match_id}' BEGIN "
            "SELECT RAISE(ABORT, 'canonical replay must be zero-write'); END"
        )

    completed = store.complete_contest_pairing_for_match(contest["id"], match_id)

    assert completed is not None and completed["status"] == "completed"
    replay = store.get_replay(match_id)
    assert (replay["events_json"], replay["updated_at"]) == (
        raw_replay,
        "2026-09-02T09:00:00",
    )


@pytest.mark.parametrize(
    "noncanonical_terminal",
    (
        {
            "type": "match_end",
            "winner": False,
            "reason": "five",
            "deltas": [2, -2],
        },
        {
            "type": "match_end",
            "winner": 0,
            "reason": "five",
            "deltas": [2.0, -2.0],
        },
    ),
    ids=("boolean_winner", "float_deltas"),
)
def test_pairing_completion_rewrites_json_type_alias_terminal(
    tmp_path,
    noncanonical_terminal,
):
    _client, store, users, _ = _fixture(tmp_path)
    match_id = "pairing-completion-noncanonical-json-types"
    contest, _pairing = _running_contest_match(store, users, match_id)
    canonical_terminal = {
        "type": "match_end",
        "winner": 0,
        "reason": "five",
        "deltas": [2, -2],
    }
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET status='completed',winner=0,reason='five',"
            "result=? WHERE id=?",
            (json.dumps({"deltas": [2, -2]}), match_id),
        )
        conn.execute(
            "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?,?,?)",
            (
                match_id,
                json.dumps([noncanonical_terminal], separators=(",", ":")),
                "2026-09-02T09:00:00",
            ),
        )

    completed = store.complete_contest_pairing_for_match(contest["id"], match_id)

    assert completed is not None and completed["status"] == "completed"
    replay = store.get_replay(match_id)
    assert replay["events_json"] == json.dumps(
        [canonical_terminal],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def test_pairing_completion_replay_failure_rolls_back_pairing_status(tmp_path):
    _client, store, users, _ = _fixture(tmp_path)
    match_id = "pairing-completion-replay-failure"
    contest, _pairing = _running_contest_match(store, users, match_id)
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET status='completed',winner=0,reason='five',"
            "result=? WHERE id=?",
            (json.dumps({"deltas": [2, -2]}), match_id),
        )
        conn.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
        conn.execute(
            "CREATE TRIGGER fail_pairing_completion_replay BEFORE INSERT ON match_replays "
            f"WHEN NEW.match_id='{match_id}' BEGIN "
            "SELECT RAISE(ABORT, 'forced pairing replay failure'); END"
        )
    pairing_before = store.list_contest_pairings(contest["id"])[0]

    with pytest.raises(sqlite3.IntegrityError, match="forced pairing replay failure"):
        store.complete_contest_pairing_for_match(contest["id"], match_id)

    assert store.list_contest_pairings(contest["id"])[0] == pairing_before
    assert store.get_replay(match_id) is None


@pytest.mark.parametrize(
    "invalid_result",
    (
        {},
        {"deltas": [True, -1]},
        {"deltas": [4, -3]},
    ),
    ids=("missing", "wrong_type", "non_zero_sum"),
)
@pytest.mark.parametrize(
    "replay_shape", ("missing", "malformed", "stale_terminal")
)
def test_pairing_completion_rejects_invalid_authoritative_result_without_writes(
    tmp_path,
    invalid_result,
    replay_shape,
):
    _client, store, users, _ = _fixture(tmp_path)
    match_id = f"pairing-invalid-result-{replay_shape}-{invalid_result!s}"
    contest, _pairing = _running_contest_match(store, users, match_id)
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET status='completed',winner=0,reason='five',"
            "result=? WHERE id=?",
            (json.dumps(invalid_result), match_id),
        )
        if replay_shape == "missing":
            conn.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
        elif replay_shape == "malformed":
            conn.execute(
                "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET events_json=excluded.events_json",
                (match_id, "{", "2026-09-02T09:00:00"),
            )
        else:
            conn.execute(
                "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET events_json=excluded.events_json",
                (
                    match_id,
                    json.dumps(
                        [
                            {
                                "type": "match_end",
                                "winner": 1,
                                "reason": "completed",
                                "deltas": [10, -10],
                                "final_chips": [10, -10],
                            }
                        ]
                    ),
                    "2026-09-02T09:00:00",
                ),
            )
    pairing_before = store.list_contest_pairings(contest["id"])[0]
    replay_before = store.get_replay(match_id)

    with pytest.raises(ValueError, match="completed Match result deltas"):
        store.complete_contest_pairing_for_match(contest["id"], match_id)

    assert store.list_contest_pairings(contest["id"])[0] == pairing_before
    assert store.get_replay(match_id) == replay_before


@pytest.mark.parametrize("replay_shape", ("missing", "stale", "malformed"))
def test_reset_aborted_pairing_finalizes_replay_before_unbinding(
    tmp_path,
    replay_shape,
):
    client, store, users, _ = _fixture(tmp_path)
    match_id = f"reset-aborted-replay-{replay_shape}"
    contest, _pairing = _running_contest_match(store, users, match_id)
    prefix = [{"type": "match_start", "game_id": "gomoku", "size": 15}]
    store.update_match(
        match_id,
        status="aborted",
        winner=None,
        reason="admin_aborted",
        ended_at="2026-09-02T10:00:00",
    )
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if replay_shape == "missing":
            conn.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
        elif replay_shape == "stale":
            conn.execute(
                "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET events_json=excluded.events_json",
                (
                    match_id,
                    json.dumps(
                        [
                            *prefix,
                            {"type": "error", "reason": "platform_error"},
                        ]
                    ),
                    "2026-09-02T09:00:00",
                ),
            )
        else:
            conn.execute(
                "INSERT INTO match_replays(match_id,events_json,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET events_json=excluded.events_json",
                (match_id, "{", "2026-09-02T09:00:00"),
            )

    reset = store.reset_aborted_contest_pairing(contest["id"], match_id)

    assert reset is not None
    assert (reset["status"], reset["match_id"]) == ("pending", None)
    terminal = {"type": "error", "reason": "admin_aborted"}
    expected = [*prefix, terminal] if replay_shape == "stale" else [terminal]
    assert json.loads(store.get_replay(match_id)["events_json"]) == expected
    source = store.get_match_record_source(match_id)
    assert source is not None and source["replay_finalized"] is True
    exported = client.get(f"/api/matches/{match_id}/log")
    assert exported.status_code == 200
    assert exported.json()["replay"]["events"][-1] == terminal


def test_reset_aborted_pairing_replay_failure_rolls_back_unbind(tmp_path):
    _client, store, users, _ = _fixture(tmp_path)
    match_id = "reset-aborted-replay-failure"
    contest, _pairing = _running_contest_match(store, users, match_id)
    store.update_match(match_id, status="aborted", reason="admin_aborted")
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
        conn.execute(
            "CREATE TRIGGER fail_reset_aborted_replay BEFORE INSERT ON match_replays "
            f"WHEN NEW.match_id='{match_id}' BEGIN "
            "SELECT RAISE(ABORT, 'forced reset aborted replay failure'); END"
        )
    pairing_before = store.list_contest_pairings(contest["id"])[0]

    with pytest.raises(
        sqlite3.IntegrityError, match="forced reset aborted replay failure"
    ):
        store.reset_aborted_contest_pairing(contest["id"], match_id)

    assert store.list_contest_pairings(contest["id"])[0] == pairing_before
    assert store.get_replay(match_id) is None


def test_unavailable_contest_adjudication_persists_exportable_terminal(tmp_path):
    client, store, users, _ = _fixture(tmp_path)
    contest, pairing = _pending_contest_pairing(store, users, status="published")
    match_id = "unavailable-adjudication-terminal"

    completed = store.adjudicate_unavailable_contest_pairing(
        contest["id"],
        pairing["id"],
        match_id,
        game_id="gomoku",
        winner=1,
        result={"rounds_played": 0, "deltas": [-1, 1], "normalized_delta": -1.0},
        time_control_id=contest["time_control_id"],
        activate_running=True,
        require_execution_admission=False,
    )

    assert completed["status"] == "completed"
    terminal = {
        "type": "match_end",
        "winner": 1,
        "reason": "contest_bot_unavailable",
        "deltas": [-1, 1],
    }
    assert json.loads(store.get_replay(match_id)["events_json"]) == [terminal]
    assert store.get_contest(contest["id"])["status"] == "running"
    exported = client.get(f"/api/matches/{match_id}/log")
    assert exported.status_code == 200
    assert exported.json()["replay"]["events"][-1] == terminal


def test_unavailable_terminal_replay_failure_rolls_back_whole_adjudication(tmp_path):
    _client, store, users, _ = _fixture(tmp_path)
    contest, pairing = _pending_contest_pairing(store, users, status="published")
    match_id = "unavailable-adjudication-replay-failure"
    with store._tx() as conn:
        conn.execute(
            "CREATE TRIGGER fail_unavailable_terminal_insert BEFORE INSERT ON match_replays "
            f"WHEN NEW.match_id='{match_id}' AND NEW.events_json<>'[]' BEGIN "
            "SELECT RAISE(ABORT, 'forced unavailable replay failure'); END"
        )
        conn.execute(
            "CREATE TRIGGER fail_unavailable_terminal_update BEFORE UPDATE ON match_replays "
            f"WHEN NEW.match_id='{match_id}' AND NEW.events_json<>'[]' BEGIN "
            "SELECT RAISE(ABORT, 'forced unavailable replay failure'); END"
        )

    with pytest.raises(
        sqlite3.IntegrityError, match="forced unavailable replay failure"
    ):
        store.adjudicate_unavailable_contest_pairing(
            contest["id"],
            pairing["id"],
            match_id,
            game_id="gomoku",
            winner=1,
            result={
                "rounds_played": 0,
                "deltas": [-1, 1],
                "normalized_delta": -1.0,
            },
            time_control_id=contest["time_control_id"],
            activate_running=True,
            require_execution_admission=False,
        )

    assert store.get_match(match_id) is None
    assert store.get_replay(match_id) is None
    with store._tx() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM matches_index WHERE id=?", (match_id,)
        ).fetchone()[0] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM match_rating_policies WHERE match_id=?",
            (match_id,),
        ).fetchone()[0] == 0
    pairing_after = store.list_contest_pairings(contest["id"])[0]
    assert (pairing_after["status"], pairing_after["match_id"]) == (
        "pending",
        None,
    )
    assert store.get_contest(contest["id"])["status"] == "published"


def test_log_is_public_but_private_match_and_debug_data_never_crosses(tmp_path):
    client, store, users, admin = _fixture(tmp_path)
    match_id = "public-private-boundary"
    private = "/private/bot_uploads/secret stderr token checksum"
    events = _events("holdem")
    events[1].update({"debug": private, "stderr": private, "path": private})
    events.insert(2, {"type": "diagnostic", "message": private})
    _terminal_match(store, users, "holdem", match_id, events=events)
    with store._tx() as conn:
        conn.execute(
            "UPDATE matches_holdem SET match_config=?,result=? WHERE id=?",
            (
                json.dumps(
                    {
                        "_bot_a_version_id": 991,
                        "binary_path": private,
                        "token": private,
                    }
                ),
                json.dumps(
                    {
                        "rounds_played": 1,
                        "deltas": [1, -1],
                        "normalized_delta": 1,
                        "raw_private_result": private,
                    }
                ),
                match_id,
            ),
        )
    debug_json = json.dumps({"stderr": private})
    assert store.replace_match_debug(
        match_id,
        [
            {
                "seat": 0,
                "turn": 1,
                "leg": -1,
                "debug_json": debug_json,
                "size_bytes": len(debug_json.encode("utf-8")),
            }
        ],
    )

    owner = users["holdem_a"][0]
    _, owner_token = client.app.state.auth.authenticate(
        owner["username"], "pw123456"
    )
    _, admin_token = client.app.state.auth.authenticate(
        admin["username"], "pw123456"
    )
    guest = client.get(f"/api/matches/{match_id}/log")
    owner_response = client.get(
        f"/api/matches/{match_id}/log",
        headers={"Authorization": f"Bearer {owner_token}"},
    )
    admin_response = client.get(
        f"/api/matches/{match_id}/log",
        headers={"Authorization": f"Bearer {admin_token}"},
    )

    assert guest.status_code == 200
    assert guest.content == owner_response.content == admin_response.content
    assert private not in guest.text
    assert "diagnostic" not in guest.text
    for forbidden in (
        "match_config",
        "binary_path",
        "version_id",
        "raw_private_result",
        "debug_json",
        "can_view_debug",
    ):
        assert forbidden not in guest.text
    action = next(
        event
        for event in guest.json()["replay"]["events"]
        if event["type"] == "action"
    )
    assert set(action) == {"type", "hand", "player", "action", "amount"}


def test_log_rejects_active_missing_unfinalized_and_corrupt_sources(tmp_path):
    client, store, users, _ = _fixture(tmp_path)
    bot = users["gomoku_a"][1]

    for status in ("pending", "running"):
        match_id = f"{status}-log"
        store.create_match(
            match_id,
            bot["id"],
            bot["id"],
            game_id="gomoku",
            match_type="challenge",
        )
        if status == "running":
            store.update_match(match_id, status="running")
        response = client.get(f"/api/matches/{match_id}/log")
        assert response.status_code == 409
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    missing = client.get("/api/matches/no-such-match/log")
    assert missing.status_code == 404
    assert missing.headers["cache-control"] == "no-store"
    assert missing.headers["x-content-type-options"] == "nosniff"

    _terminal_match(store, users, "gomoku", "missing-replay", events=None)
    assert client.get("/api/matches/missing-replay/log").status_code == 409

    _terminal_match(
        store,
        users,
        "gomoku",
        "malformed-replay",
        events=_events("gomoku"),
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE match_replays SET events_json='{' WHERE match_id=?",
            ("malformed-replay",),
        )
    assert client.get("/api/matches/malformed-replay/log").status_code == 409

    _terminal_match(
        store,
        users,
        "gomoku",
        "old-live-prefix",
        events=_events("gomoku")[:-1],
    )
    assert client.get("/api/matches/old-live-prefix/log").status_code == 409

    _terminal_match(
        store,
        users,
        "gomoku",
        "wrong-terminal",
        events=_events("gomoku", terminal="error"),
    )
    assert client.get("/api/matches/wrong-terminal/log").status_code == 409

    _terminal_match(
        store,
        users,
        "gomoku",
        "corrupt-contract",
        events=_events("gomoku"),
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE matches_gomoku SET ruleset_version=? WHERE id=?",
            ("gomoku/../../private", "corrupt-contract"),
        )
    corrupt = client.get("/api/matches/corrupt-contract/log")
    assert corrupt.status_code == 409
    assert "gomoku/../../private" not in corrupt.text


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("locator", "future-game"),
        ("locator", " GOMOKU "),
        ("row", "holdem"),
        ("row", sqlite3.Binary(b"gomoku")),
    ],
)
def test_log_rejects_unknown_noncanonical_and_cross_table_game_drift(
    tmp_path, column, value
):
    client, store, users, _ = _fixture(tmp_path)
    match_id = f"corrupt-{column}-{type(value).__name__}"
    _terminal_match(
        store,
        users,
        "gomoku",
        match_id,
        events=_events("gomoku"),
    )
    with store._tx() as conn:
        if column == "locator":
            conn.execute(
                "UPDATE matches_index SET game_id=? WHERE id=?",
                (value, match_id),
            )
        else:
            conn.execute(
                "UPDATE matches_gomoku SET game_id=? WHERE id=?",
                (value, match_id),
            )

    response = client.get(f"/api/matches/{match_id}/log")
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_human_terminal_log_uses_public_seat_identity_only(tmp_path):
    client, store, users, human = _fixture(tmp_path)
    bot = users["gomoku_a"][1]
    match_id = "human-terminal-log"
    store.create_match(
        match_id,
        bot["id"],
        bot["id"],
        owner_id=human["id"],
        game_id="gomoku",
        match_type="human",
        human_user_id=human["id"],
        human_seat=1,
    )
    store.update_match(
        match_id,
        status="completed",
        winner=1,
        reason="five",
        result={
            "rounds_played": 1,
            "deltas": [-1, 1],
            "normalized_delta": -1,
        },
        ended_at="2026-08-20T10:00:00",
    )
    store.upsert_replay(
        match_id,
        json.dumps(_events("gomoku"), ensure_ascii=False),
    )

    response = client.get(f"/api/matches/{match_id}/log")
    assert response.status_code == 200
    match = response.json()["match"]
    assert match["bot_a"]["is_human"] is False
    assert match["bot_b"]["is_human"] is True
    assert match["bot_b"]["owner_name"] == human["username"]
    assert match["human_seat"] == 1
    for private_key in ("owner_id", "human_user_id", "match_seed"):
        assert private_key not in response.text


def test_non_json_public_field_returns_controlled_conflict(tmp_path):
    client, store, users, _ = _fixture(tmp_path)
    match_id = "non-json-public-field"
    _terminal_match(
        store,
        users,
        "holdem",
        match_id,
        events=_events("holdem"),
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE matches_holdem SET created_at=? WHERE id=?",
            (sqlite3.Binary(b"not-json"), match_id),
        )

    response = client.get(f"/api/matches/{match_id}/log")
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "not-json" not in response.text


def test_retired_bulk_matchpack_routes_remain_absent(tmp_path):
    client, _, _, _ = _fixture(tmp_path)
    for path in (
        "/api/matchpacks",
        "/api/matchpacks/download",
        "/api/matchpacks/holdem/2026-08",
    ):
        assert client.get(path).status_code == 404


def test_log_filename_is_ascii_safe_and_store_source_is_atomic(tmp_path, monkeypatch):
    client, store, users, _ = _fixture(tmp_path)
    match_id = 'evil"; filename=owned-\r\n雪 .. __'
    _terminal_match(
        store,
        users,
        "pencil",
        match_id,
        events=_events("pencil"),
    )

    def forbidden_split_read(*_args, **_kwargs):
        raise AssertionError("log route must use Store's one-snapshot source")

    monkeypatch.setattr(store, "get_match_detailed", forbidden_split_read)
    monkeypatch.setattr(store, "get_public_replay_payload", forbidden_split_read)
    response = client.get(f"/api/matches/{quote(match_id, safe='')}/log")

    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition == (
        'attachment; filename="botbattle-pencil-evil-filename-owned-log.json"'
    )
    assert disposition.isascii()
    assert "\r" not in disposition and "\n" not in disposition
    assert ".." not in disposition
    assert re.fullmatch(
        r'attachment; filename="botbattle-pencil-[A-Za-z0-9_-]+-log\.json"',
        disposition,
    )
    assert response.json()["match"]["id"] == match_id


def test_historical_technical_events_use_the_canonical_public_shape(tmp_path):
    client, store, users, _ = _fixture(tmp_path)
    match_id = "legacy-technical-event"
    private = "/private/stderr token"
    events = _events("holdem")
    events.insert(
        -1,
        {
            "type": "bot_decide_error",
            "seat": 1,
            "turn": 7,
            "reason": "protocol_error",
            "code": "invalid_json",
            "stderr": private,
        },
    )
    _terminal_match(store, users, "holdem", match_id, events=events)

    response = client.get(f"/api/matches/{match_id}/log")
    replay = client.get(f"/api/matches/{match_id}/replay")
    assert response.status_code == replay.status_code == 200
    assert response.json()["replay"] == replay.json()
    incident = next(
        event
        for event in response.json()["replay"]["events"]
        if event["type"] == "technical_incident"
    )
    assert incident["seat"] == 1
    assert incident["turn"] == 7
    assert incident["reason"] == "protocol_error"
    assert private not in response.text
