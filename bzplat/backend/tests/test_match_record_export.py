"""Single-match public record export contract."""
from __future__ import annotations

import json
import re
from typing import Any

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.games.gomoku.protocol import (
    PROTOCOL_VERSION as GOMOKU_EVENT_PROTOCOL_VERSION,
)
from bzplat.backend.main import create_app
from bzplat.backend.store.schema import (
    GOMOKU_CURRENT_PROTOCOL,
    GOMOKU_CURRENT_RULESET,
    GOMOKU_LEGACY_PROTOCOL,
    GOMOKU_LEGACY_RULESET,
)


_LEGACY_PAIR = (GOMOKU_LEGACY_RULESET, GOMOKU_LEGACY_PROTOCOL)
_DERIVED_EVENT_FIELDS = {
    "event_seq",
    "seat_no",
    "stone_no",
    "stone_color",
    "algebraic",
    "opening_stones",
    "algebraic_points",
    "candidate_for_stone_no",
    "selected_stone_no",
    "last_algebraic",
    "attempted_algebraic",
    "winner_seat_no",
}


def _strip_record_derivations(event: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in event.items()
        if key not in _DERIVED_EVENT_FIELDS
    }


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
    app = create_app(db_path=str(tmp_path / "record.db"))
    store = app.state.store
    user_a, gomoku_a = _make_user_bot(store, "record_a", game_id="gomoku")
    user_b, gomoku_b = _make_user_bot(store, "record_b", game_id="gomoku")
    _, holdem_a = _make_user_bot(store, "record_h1", game_id="holdem")
    _, holdem_b = _make_user_bot(store, "record_h2", game_id="holdem")
    return (
        TestClient(app),
        store,
        user_a,
        user_b,
        gomoku_a,
        gomoku_b,
        holdem_a,
        holdem_b,
    )


def _create_gomoku_match(store, match_id: str, bot_a: int, bot_b: int):
    return store.create_match(
        match_id,
        bot_a,
        bot_b,
        game_id="gomoku",
        match_type="challenge",
    )


def test_current_gomoku_record_is_public_lossless_deterministic_and_downloadable(
    tmp_path,
):
    client, store, user_a, user_b, bot_a, bot_b, _, _ = _fixture(tmp_path)
    match_id = "current-v2"
    _create_gomoku_match(store, match_id, bot_a["id"], bot_b["id"])
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        reason="forbidden_overline",
        result={
            "rounds_played": 2,
            "deltas": [1, -1],
            "normalized_delta": 1,
            "raw_private_result": "/private/result",
        },
        started_at="2026-08-17T10:00:00",
        ended_at="2026-08-17T10:03:00",
    )
    private = "/private/bot_uploads/secret stderr token"
    events = [
        {
            "type": "match_start",
            "game_id": "gomoku",
            "size": 15,
            "first": 0,
            "ruleset": "gomoku_ccgc_2013_v1",
            "protocol_version": GOMOKU_EVENT_PROTOCOL_VERSION,
            "private": private,
        },
        {
            "type": "opening",
            "player": 0,
            "opening_code": "D1",
            "n": 2,
            "black1": {"x": 7, "y": 7, "path": private},
            "white2": {"x": 7, "y": 8},
            "black3": {"x": 8, "y": 8},
            "debug": private,
        },
        {"type": "swap", "player": 1, "swapped": True, "seat_colors": [1, 0]},
        {
            "type": "move",
            "player": 0,
            "color": 1,
            "phase": "white4",
            "x": 6,
            "y": 8,
            "move_index": 4,
            "binary_path": private,
        },
        {"type": "turn", "player": 1, "last": {"x": 6, "y": 8}},
        {
            "type": "black5_candidates",
            "player": 1,
            "n": 2,
            "points": [{"x": 9, "y": 9}, {"x": 5, "y": 5}],
        },
        {
            "type": "black5_selected",
            "player": 0,
            "index": 0,
            "point": {"x": 9, "y": 9},
        },
        {
            "type": "move",
            "player": 1,
            "color": 0,
            "phase": "black5_select",
            "selected_by": 0,
            "x": 9,
            "y": 9,
            "move_index": 5,
        },
        {
            "type": "forbidden",
            "player": 1,
            "color": 0,
            "x": 9,
            "y": 9,
            "forbidden_kind": "overline",
        },
        {
            "type": "illegal",
            "player": 1,
            "phase": "normal",
            "action": {"action": "move", "x": 14, "y": 14},
            "why": "occupied_or_out_of_board",
        },
        {"type": "time_used", "seat": 0, "used": 12.5, "remaining": 887.5, "budget": 900},
        {"type": "time_out", "seat": 1, "used": 900, "budget": 900},
        {"type": "diagnostic", "stderr": private, "path": private},
        {"type": "match_end", "winner": 1, "reason": "completed"},
    ]
    store.upsert_replay(match_id, json.dumps(events, ensure_ascii=False))
    # Frozen execution details and private debug exist in persistence but must
    # never enter the public record source.
    with store._tx() as conn:
        conn.execute(
            "UPDATE matches_gomoku SET match_config=? WHERE id=?",
            (json.dumps({"_bot_a_version_id": 999, "path": private}), match_id),
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

    first = client.get(f"/api/matches/{match_id}/record")
    second = client.get(f"/api/matches/{match_id}/record")
    store.update_user(user_b["id"], role="admin")
    _, user_token = client.app.state.auth.authenticate("record_a", "pw123456")
    _, admin_token = client.app.state.auth.authenticate("record_b", "pw123456")
    authenticated = client.get(
        f"/api/matches/{match_id}/record",
        headers={"Authorization": f"Bearer {user_token}"},
    )
    admin = client.get(
        f"/api/matches/{match_id}/record",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    replay = client.get(f"/api/matches/{match_id}/replay").json()
    detail = client.get(f"/api/matches/{match_id}").json()["match"]

    assert first.status_code == 200
    assert first.content == second.content
    assert first.content == authenticated.content == admin.content
    assert first.headers["content-type"] == "application/json"
    assert first.headers["content-disposition"] == (
        'attachment; filename="botbattle-gomoku-current-v2.json"'
    )
    assert first.headers["cache-control"] == "no-store"
    assert first.headers["x-content-type-options"] == "nosniff"
    assert not first.content.startswith(b"\xef\xbb\xbf")
    assert first.content.endswith(b"\n")
    assert not first.content.endswith(b"\n\n")
    assert b'\n  "' in first.content

    record = first.json()
    assert set(record) == {
        "format",
        "format_version",
        "match",
        "seats",
        "coordinate_system",
        "updated_at",
        "event_count",
        "events",
    }
    assert record["format"] == "botbattle.gomoku.record"
    assert record["format_version"] == 1
    assert record["match"]["ruleset_version"] == "gomoku_ccgc_2013_v1"
    assert record["match"]["protocol_version"] == "gomoku_action_v2"
    assert record["updated_at"] == replay["updated_at"]
    assert record["event_count"] == replay["event_count"]
    assert [_strip_record_derivations(event) for event in record["events"]] == replay[
        "events"
    ]
    assert [event["event_seq"] for event in record["events"]] == list(
        range(1, record["event_count"] + 1)
    )

    opening = next(event for event in record["events"] if event["type"] == "opening")
    assert opening["opening_stones"] == [
        {
            "source_field": "black1",
            "stone_no": 1,
            "stone_color": "black",
            "algebraic": "H8",
        },
        {
            "source_field": "white2",
            "stone_no": 2,
            "stone_color": "white",
            "algebraic": "H7",
        },
        {
            "source_field": "black3",
            "stone_no": 3,
            "stone_color": "black",
            "algebraic": "I7",
        },
    ]
    moves = [event for event in record["events"] if event["type"] == "move"]
    assert [
        (event["seat_no"], event["stone_no"], event["stone_color"], event["algebraic"])
        for event in moves
    ] == [(1, 4, "white", "G7"), (2, 5, "black", "J6")]
    candidates = next(
        event for event in record["events"] if event["type"] == "black5_candidates"
    )
    assert candidates["algebraic_points"] == ["J6", "F10"]
    assert candidates["candidate_for_stone_no"] == 5
    selected = next(
        event for event in record["events"] if event["type"] == "black5_selected"
    )
    assert selected["algebraic"] == "J6"
    assert selected["selected_stone_no"] == 5
    forbidden = next(
        event for event in record["events"] if event["type"] == "forbidden"
    )
    assert (forbidden["algebraic"], forbidden["stone_no"]) == ("J6", 5)
    illegal = next(event for event in record["events"] if event["type"] == "illegal")
    assert illegal["attempted_algebraic"] == "O1"
    assert [
        (event["type"], event["seat"], event["seat_no"])
        for event in record["events"]
        if event["type"] in {"time_used", "time_out"}
    ] == [("time_used", 0, 1), ("time_out", 1, 2)]
    assert record["events"][-1]["winner_seat_no"] == 1

    assert record["match"]["winner_seat_no"] == 1
    assert record["seats"][0]["name"] == detail["bot_a"]["name"]
    assert record["seats"][1]["owner_name"] == detail["bot_b"]["owner_name"]
    encoded = first.content.decode("utf-8")
    assert private not in encoded
    for forbidden_key in (
        "raw_private_result",
        "match_config",
        "binary_path",
        "version_id",
        "debug_json",
    ):
        assert forbidden_key not in encoded


def test_legacy_gomoku_record_uses_frozen_contract_and_algebraic_coordinates(
    tmp_path,
):
    client, store, _, _, bot_a, bot_b, _, _ = _fixture(tmp_path)
    match_id = "legacy-gomoku"
    _create_gomoku_match(store, match_id, bot_a["id"], bot_b["id"])
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        reason="five",
        result={"rounds_played": 3, "deltas": [1, -1], "normalized_delta": 1},
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE matches_gomoku SET ruleset_version=?,protocol_version=? WHERE id=?",
            (GOMOKU_LEGACY_RULESET, GOMOKU_LEGACY_PROTOCOL, match_id),
        )
    store.upsert_replay(
        match_id,
        json.dumps(
            [
                {"type": "match_start", "game_id": "gomoku", "size": 15},
                {"type": "move", "player": 0, "x": 0, "y": 0, "move_index": 0},
                {"type": "move", "player": 1, "x": 14, "y": 14, "move_index": 1},
                {"type": "move", "player": 0, "x": 7, "y": 7, "move_index": 2},
                {"type": "match_end", "winner": 0, "reason": "five"},
            ]
        ),
    )

    response = client.get(f"/api/matches/{match_id}/record")
    assert response.status_code == 200
    record = response.json()
    assert record["match"]["ruleset_version"] == GOMOKU_LEGACY_RULESET
    assert record["match"]["protocol_version"] == GOMOKU_LEGACY_PROTOCOL
    moves = [event for event in record["events"] if event["type"] == "move"]
    assert [event["stone_no"] for event in moves] == [1, 2, 3]
    assert [event["stone_color"] for event in moves] == ["black", "white", "black"]
    assert [event["algebraic"] for event in moves] == ["A15", "O1", "H8"]


def test_aborted_gomoku_record_uses_authoritative_public_terminal(tmp_path):
    client, store, _, _, bot_a, bot_b, _, _ = _fixture(tmp_path)
    match_id = "aborted-gomoku"
    _create_gomoku_match(store, match_id, bot_a["id"], bot_b["id"])
    private = "traceback /private/runtime/bot.bin"
    store.update_match(match_id, status="aborted", reason=private)
    store.upsert_replay(
        match_id,
        json.dumps(
            [
                {
                    "type": "match_start",
                    "game_id": "gomoku",
                    "size": 15,
                    "ruleset": GOMOKU_CURRENT_RULESET,
                    "protocol_version": GOMOKU_EVENT_PROTOCOL_VERSION,
                },
                {"type": "move", "player": 0, "x": 7, "y": 7, "move_index": 1},
                {"type": "match_end", "winner": 0, "reason": "five"},
                {"type": "error", "reason": "version_unavailable", "message": private},
            ]
        ),
    )

    response = client.get(f"/api/matches/{match_id}/record")
    replay = client.get(f"/api/matches/{match_id}/replay").json()
    assert response.status_code == 200
    record = response.json()
    assert record["match"]["status"] == "aborted"
    assert record["match"]["reason"] == "platform_error"
    assert [_strip_record_derivations(event) for event in record["events"]] == replay[
        "events"
    ]
    assert record["events"][-1] == {
        "type": "error",
        "reason": "platform_error",
        "event_seq": 3,
    }
    assert private not in response.text


def test_record_rejects_active_unsupported_missing_and_corrupt_contracts(tmp_path):
    client, store, _, _, bot_a, bot_b, holdem_a, holdem_b = _fixture(tmp_path)

    # Self-play keeps these two active-state fixtures rating-neutral, so the
    # Store's production active-rated-pair admission guard remains intact.
    _create_gomoku_match(store, "pending-record", bot_a["id"], bot_a["id"])
    _create_gomoku_match(store, "running-record", bot_a["id"], bot_a["id"])
    store.update_match("running-record", status="running")
    for match_id in ("pending-record", "running-record"):
        response = client.get(f"/api/matches/{match_id}/record")
        assert response.status_code == 409
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"

    store.create_match(
        "unsupported-holdem",
        holdem_a["id"],
        holdem_b["id"],
        game_id="holdem",
    )
    store.update_match(
        "unsupported-holdem",
        status="completed",
        winner=0,
        reason="completed",
    )
    assert client.get("/api/matches/unsupported-holdem/record").status_code == 409
    assert client.get("/api/matches/does-not-exist/record").status_code == 404

    corrupt_values = {
        "missing-contract": ("", "gomoku_action_v2"),
        "nonstring-contract": (b"gomoku_ccgc_2013_v1", "gomoku_action_v2"),
        "illegal-contract": ("gomoku/../../private", "gomoku_action_v2"),
        "oversized-contract": ("g" * 129, "gomoku_action_v2"),
        "bad-protocol": ("gomoku_ccgc_2013_v1", "gomoku action v2"),
    }
    for match_id, (ruleset, protocol) in corrupt_values.items():
        _create_gomoku_match(store, match_id, bot_a["id"], bot_a["id"])
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            reason="five",
            result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
        )
        with store._tx() as conn:
            conn.execute(
                "UPDATE matches_gomoku SET ruleset_version=?,protocol_version=? WHERE id=?",
                (ruleset, protocol, match_id),
            )
        store.upsert_replay(
            match_id,
            json.dumps([{"type": "match_end", "winner": 0, "reason": "five"}]),
        )
        response = client.get(f"/api/matches/{match_id}/record")
        assert response.status_code == 409
        if isinstance(ruleset, str) and ruleset:
            assert ruleset not in response.text
        if protocol:
            assert protocol not in response.text


def test_record_filename_is_ascii_safe_for_malicious_match_id(tmp_path):
    client, store, _, _, bot_a, bot_b, _, _ = _fixture(tmp_path)
    match_id = 'evil"; filename=owned-雪 .. __'
    _create_gomoku_match(store, match_id, bot_a["id"], bot_b["id"])
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        reason="five",
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
    )
    store.upsert_replay(
        match_id,
        json.dumps([{"type": "match_end", "winner": 0, "reason": "five"}]),
    )

    response = client.get(f"/api/matches/{match_id}/record")
    assert response.status_code == 200
    disposition = response.headers["content-disposition"]
    assert disposition == (
        'attachment; filename="botbattle-gomoku-evil-filename-owned.json"'
    )
    assert disposition.isascii()
    assert "\r" not in disposition and "\n" not in disposition
    assert ".." not in disposition
    assert re.fullmatch(
        r'attachment; filename="botbattle-gomoku-[A-Za-z0-9_-]+\.json"',
        disposition,
    )
    # The record identity stays exact; only the attachment filename is reduced.
    assert response.json()["match"]["id"] == match_id


def test_record_requires_matching_raw_terminal_and_uses_atomic_store_source(
    tmp_path, monkeypatch
):
    client, store, _, _, bot_a, bot_b, _, _ = _fixture(tmp_path)
    match_id = "terminal-flush-window"
    _create_gomoku_match(store, match_id, bot_a["id"], bot_a["id"])
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        reason="five",
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
    )

    # Completion commits before the best-effort final replay flush.  Neither a
    # missing row nor an old live prefix may be mistaken for a finished record.
    assert client.get(f"/api/matches/{match_id}/record").status_code == 409
    start = {
        "type": "match_start",
        "game_id": "gomoku",
        "size": 15,
        "first": 0,
        "ruleset": GOMOKU_CURRENT_RULESET,
        "protocol_version": GOMOKU_EVENT_PROTOCOL_VERSION,
    }
    store.upsert_replay(match_id, json.dumps([start]))
    assert client.get(f"/api/matches/{match_id}/record").status_code == 409
    store.upsert_replay(
        match_id,
        json.dumps([start, {"type": "error", "reason": "platform_error"}]),
    )
    assert client.get(f"/api/matches/{match_id}/record").status_code == 409

    store.upsert_replay(
        match_id,
        json.dumps([start, {"type": "match_end", "winner": 0, "reason": "five"}]),
    )

    def forbidden_split_read(*_args, **_kwargs):
        raise AssertionError("record route must use Store's one-snapshot source")

    monkeypatch.setattr(store, "get_match_detailed", forbidden_split_read)
    monkeypatch.setattr(store, "get_public_replay_payload", forbidden_split_read)
    assert client.get(f"/api/matches/{match_id}/record").status_code == 200

    aborted_id = "aborted-terminal-type"
    _create_gomoku_match(store, aborted_id, bot_a["id"], bot_a["id"])
    store.update_match(aborted_id, status="aborted", reason="platform_error")
    store.upsert_replay(
        aborted_id,
        json.dumps([{"type": "match_end", "winner": None, "reason": "completed"}]),
    )
    assert client.get(f"/api/matches/{aborted_id}/record").status_code == 409
    store.upsert_replay(
        aborted_id,
        json.dumps([{"type": "error", "reason": "platform_error"}]),
    )
    assert client.get(f"/api/matches/{aborted_id}/record").status_code == 200


def test_record_rejects_unknown_mixed_and_conflicting_gomoku_contracts(tmp_path):
    client, store, _, _, bot_a, bot_b, _, _ = _fixture(tmp_path)

    def completed_record(
        match_id: str,
        *,
        pair: tuple[str, str] | None = None,
        prefix: list[dict[str, Any]] | None = None,
    ):
        _create_gomoku_match(store, match_id, bot_a["id"], bot_a["id"])
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            reason="five",
            result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
        )
        if pair is not None:
            with store._tx() as conn:
                conn.execute(
                    "UPDATE matches_gomoku SET ruleset_version=?,protocol_version=? WHERE id=?",
                    (*pair, match_id),
                )
        events = list(prefix or [])
        events.append({"type": "match_end", "winner": 0, "reason": "five"})
        store.upsert_replay(match_id, json.dumps(events))
        return client.get(f"/api/matches/{match_id}/record")

    for match_id, pair in {
        "unknown-contract-pair": ("gomoku_future_v9", "gomoku_action_v9"),
        "mixed-contract-pair": (GOMOKU_CURRENT_RULESET, GOMOKU_LEGACY_PROTOCOL),
        "reverse-mixed-pair": (GOMOKU_LEGACY_RULESET, GOMOKU_CURRENT_PROTOCOL),
    }.items():
        assert completed_record(match_id, pair=pair).status_code == 409

    valid_start = {
        "type": "match_start",
        "game_id": "gomoku",
        "size": 15,
        "ruleset": GOMOKU_CURRENT_RULESET,
        "protocol_version": GOMOKU_EVENT_PROTOCOL_VERSION,
    }
    conflicts = {
        "start-wrong-game": {**valid_start, "game_id": "holdem"},
        "start-wrong-size": {**valid_start, "size": 14},
        "start-wrong-ruleset": {
            **valid_start,
            "ruleset": GOMOKU_LEGACY_RULESET,
        },
        "start-wrong-protocol": {
            **valid_start,
            "protocol_version": GOMOKU_LEGACY_PROTOCOL,
        },
        "start-missing-protocol": {
            key: value
            for key, value in valid_start.items()
            if key != "protocol_version"
        },
    }
    for match_id, start in conflicts.items():
        assert completed_record(match_id, prefix=[start]).status_code == 409

    legacy_conflict = {
        "type": "match_start",
        "game_id": "gomoku",
        "size": 15,
        "ruleset": GOMOKU_CURRENT_RULESET,
    }
    assert completed_record(
        "legacy-start-conflict",
        pair=_LEGACY_PAIR,
        prefix=[legacy_conflict],
    ).status_code == 409


def test_record_derivations_fail_closed_for_partial_and_discontinuous_moves(tmp_path):
    client, store, _, _, bot_a, bot_b, _, _ = _fixture(tmp_path)
    start = {
        "type": "match_start",
        "game_id": "gomoku",
        "size": 15,
        "ruleset": GOMOKU_CURRENT_RULESET,
        "protocol_version": GOMOKU_EVENT_PROTOCOL_VERSION,
    }

    def export(match_id: str, prefix: list[dict[str, Any]]) -> dict[str, Any]:
        _create_gomoku_match(store, match_id, bot_a["id"], bot_a["id"])
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            reason="five",
            result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
        )
        store.upsert_replay(
            match_id,
            json.dumps(
                [*prefix, {"type": "match_end", "winner": 0, "reason": "five"}]
            ),
        )
        response = client.get(f"/api/matches/{match_id}/record")
        assert response.status_code == 200
        return response.json()

    partial = export(
        "partial-opening",
        [
            start,
            {
                "type": "opening",
                "player": 0,
                "black1": {"x": 7, "y": 7},
                "white2": {"x": 7, "y": 8},
            },
            {
                "type": "move",
                "player": 0,
                "color": 1,
                "x": 6,
                "y": 8,
                "move_index": 4,
            },
        ],
    )
    partial_opening = next(
        event for event in partial["events"] if event["type"] == "opening"
    )
    partial_move = next(event for event in partial["events"] if event["type"] == "move")
    assert "opening_stones" not in partial_opening
    assert partial_move["algebraic"] == "G7"
    assert "stone_no" not in partial_move and "stone_color" not in partial_move

    complete_opening = {
        "type": "opening",
        "player": 0,
        "black1": {"x": 7, "y": 7},
        "white2": {"x": 7, "y": 8},
        "black3": {"x": 8, "y": 8},
    }
    discontinuous = export(
        "huge-and-invalid-moves",
        [
            start,
            complete_opening,
            {
                "type": "move",
                "player": 0,
                "color": 1,
                "x": 6,
                "y": 8,
                "move_index": 999,
            },
            {"type": "move", "player": 1, "color": 0, "x": 5, "move_index": 5},
            {
                "type": "move",
                "player": 1,
                "color": 0,
                "x": 99,
                "y": 99,
                "move_index": 5,
            },
        ],
    )
    bad_moves = [event for event in discontinuous["events"] if event["type"] == "move"]
    assert bad_moves[0]["algebraic"] == "G7"
    assert all("stone_no" not in event for event in bad_moves)
    assert all("stone_color" not in event for event in bad_moves)

    duplicate = export(
        "duplicate-occupied-move",
        [
            start,
            complete_opening,
            {
                "type": "move",
                "player": 0,
                "color": 1,
                "x": 6,
                "y": 8,
                "move_index": 4,
            },
            {
                "type": "black5_candidates",
                "player": 1,
                "n": 2,
                "points": [{"x": 9, "y": 9}, {"x": 5, "y": 5}],
            },
            {
                "type": "black5_selected",
                "player": 0,
                "index": 0,
                "point": {"x": 9, "y": 9},
            },
            {
                "type": "move",
                "player": 1,
                "color": 0,
                "x": 9,
                "y": 9,
                "move_index": 5,
            },
            {
                "type": "move",
                "player": 0,
                "color": 1,
                "x": 9,
                "y": 9,
                "move_index": 6,
            },
            {
                "type": "move",
                "player": 1,
                "color": 0,
                "x": 10,
                "y": 10,
                "move_index": 6,
            },
        ],
    )
    duplicate_moves = [
        event for event in duplicate["events"] if event["type"] == "move"
    ]
    assert [event.get("stone_no") for event in duplicate_moves[:2]] == [4, 5]
    assert duplicate_moves[2]["algebraic"] == "J6"
    assert "stone_no" not in duplicate_moves[2]
    assert "stone_color" not in duplicate_moves[2]
    assert "stone_no" not in duplicate_moves[3]
    assert "stone_color" not in duplicate_moves[3]


def test_record_derivations_require_complete_candidate_selection_and_forbidden_link(
    tmp_path,
):
    client, store, _, _, bot_a, bot_b, _, _ = _fixture(tmp_path)
    start = {
        "type": "match_start",
        "game_id": "gomoku",
        "size": 15,
        "ruleset": GOMOKU_CURRENT_RULESET,
        "protocol_version": GOMOKU_EVENT_PROTOCOL_VERSION,
    }
    opening = {
        "type": "opening",
        "player": 0,
        "black1": {"x": 7, "y": 7},
        "white2": {"x": 7, "y": 8},
        "black3": {"x": 8, "y": 8},
    }
    white4 = {
        "type": "move",
        "player": 0,
        "color": 1,
        "x": 6,
        "y": 8,
        "move_index": 4,
    }
    candidates = {
        "type": "black5_candidates",
        "player": 1,
        "n": 2,
        "points": [{"x": 9, "y": 9}, {"x": 5, "y": 5}],
    }

    def export(match_id: str, tail: list[dict[str, Any]]) -> dict[str, Any]:
        _create_gomoku_match(store, match_id, bot_a["id"], bot_a["id"])
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            reason="five",
            result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 1},
        )
        store.upsert_replay(
            match_id,
            json.dumps(
                [
                    start,
                    opening,
                    white4,
                    *tail,
                    {"type": "match_end", "winner": 0, "reason": "five"},
                ]
            ),
        )
        response = client.get(f"/api/matches/{match_id}/record")
        assert response.status_code == 200
        return response.json()

    broken = export(
        "broken-black5-chain",
        [
            {"type": "black5_candidates", "player": 1, "n": 2, "points": []},
            candidates,
            {
                "type": "black5_selected",
                "player": 0,
                "index": 0,
                "point": {"x": 5, "y": 5},
            },
            {
                "type": "move",
                "player": 1,
                "color": 0,
                "x": 5,
                "y": 5,
                "move_index": 5,
            },
            {"type": "forbidden", "player": 1, "color": 0, "x": 5, "y": 5},
        ],
    )
    candidate_events = [
        event for event in broken["events"] if event["type"] == "black5_candidates"
    ]
    assert "algebraic_points" not in candidate_events[0]
    assert "candidate_for_stone_no" not in candidate_events[0]
    assert candidate_events[1]["algebraic_points"] == ["J6", "F10"]
    bad_selected = next(
        event for event in broken["events"] if event["type"] == "black5_selected"
    )
    assert "algebraic" not in bad_selected
    assert "selected_stone_no" not in bad_selected
    bad_black5 = [event for event in broken["events"] if event["type"] == "move"][-1]
    assert "stone_no" not in bad_black5
    bad_forbidden = next(
        event for event in broken["events"] if event["type"] == "forbidden"
    )
    assert "stone_no" not in bad_forbidden

    linked = export(
        "wrong-forbidden-link",
        [
            candidates,
            {
                "type": "black5_selected",
                "player": 0,
                "index": 0,
                "point": {"x": 9, "y": 9},
            },
            {
                "type": "move",
                "player": 1,
                "color": 0,
                "x": 9,
                "y": 9,
                "move_index": 5,
            },
            {"type": "forbidden", "player": 1, "color": 0, "x": 5, "y": 5},
        ],
    )
    linked_move = [event for event in linked["events"] if event["type"] == "move"][-1]
    linked_forbidden = next(
        event for event in linked["events"] if event["type"] == "forbidden"
    )
    assert linked_move["stone_no"] == 5
    assert linked_forbidden["algebraic"] == "F10"
    assert "stone_no" not in linked_forbidden

    passed = export(
        "pass-does-not-change-stone-number",
        [
            candidates,
            {
                "type": "black5_selected",
                "player": 0,
                "index": 0,
                "point": {"x": 9, "y": 9},
            },
            {
                "type": "move",
                "player": 1,
                "color": 0,
                "x": 9,
                "y": 9,
                "move_index": 5,
            },
            {"type": "pass", "player": 1, "color": 1, "move_index": 5},
            {
                "type": "move",
                "player": 0,
                "color": 0,
                "x": 10,
                "y": 9,
                "move_index": 6,
            },
        ],
    )
    passed_moves = [event for event in passed["events"] if event["type"] == "move"]
    assert (passed_moves[-1]["stone_no"], passed_moves[-1]["stone_color"]) == (
        6,
        "black",
    )


def test_legacy_record_accepts_only_continuous_zero_or_one_based_move_indexes(tmp_path):
    client, store, _, _, bot_a, bot_b, _, _ = _fixture(tmp_path)

    def export(match_id: str, indexes: list[int]) -> list[dict[str, Any]]:
        _create_gomoku_match(store, match_id, bot_a["id"], bot_a["id"])
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            reason="five",
            result={"rounds_played": len(indexes), "deltas": [1, -1], "normalized_delta": 1},
        )
        with store._tx() as conn:
            conn.execute(
                "UPDATE matches_gomoku SET ruleset_version=?,protocol_version=? WHERE id=?",
                (*_LEGACY_PAIR, match_id),
            )
        moves = [
            {
                "type": "move",
                "player": offset % 2,
                "x": offset,
                "y": offset,
                "move_index": move_index,
            }
            for offset, move_index in enumerate(indexes)
        ]
        store.upsert_replay(
            match_id,
            json.dumps([*moves, {"type": "match_end", "winner": 0, "reason": "five"}]),
        )
        response = client.get(f"/api/matches/{match_id}/record")
        assert response.status_code == 200
        return [event for event in response.json()["events"] if event["type"] == "move"]

    one_based = export("legacy-one-based", [1, 2, 3])
    assert [event["stone_no"] for event in one_based] == [1, 2, 3]

    discontinuous = export("legacy-gap", [0, 2, 999])
    assert discontinuous[0]["stone_no"] == 1
    assert all("stone_no" not in event for event in discontinuous[1:])
