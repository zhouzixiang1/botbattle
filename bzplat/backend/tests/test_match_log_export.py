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
