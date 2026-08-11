"""History 分页（PR fix/history-pagination）测试：

- /api/matches 返回 total / limit / offset
- offset 分页正确切片
- status / game_id 过滤下 total 同步收敛
"""
from __future__ import annotations

import asyncio
import json

from fastapi.testclient import TestClient
from starlette.requests import Request

from bzplat.backend.crypto import hash_password
from bzplat.backend.api_routes import match_events, router
from bzplat.backend.main import create_app


def _app(tmp_path):
    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u = store.create_user("alice", "a@ex.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    b1 = store.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = store.create_bot(u["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    g1 = store.create_bot(u["id"], "gomokuA", binary_path="/tmp", format="elf", game_id="gomoku")
    g2 = store.create_bot(u["id"], "gomokuB", binary_path="/tmp", format="elf", game_id="gomoku")
    # 建若干 holdem + gomoku 对局（completed / aborted 混合）
    for i in range(7):
        mid = f"mh{i}"
        store.create_match(mid, bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=u["id"], game_id="holdem")
        store.update_match(mid, status="completed", winner=0)
    for i in range(3):
        mid = f"mg{i}"
        store.create_match(mid, bot_a_id=g1["id"], bot_b_id=g2["id"], owner_id=u["id"], game_id="gomoku")
        store.update_match(mid, status="aborted")
    c = TestClient(app)
    return c, store


def _replay_events(client: TestClient, match_id: str) -> list[dict]:
    response = client.get(f"/api/matches/{match_id}/replay")
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["event_count"] == len(payload["events"])
    return payload["events"]


def test_matches_list_returns_total(tmp_path):
    c, store = _app(tmp_path)
    r = c.get("/api/matches?limit=100")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 10  # 7 holdem + 3 gomoku
    assert body["limit"] == 100
    assert body["offset"] == 0
    assert len(body["matches"]) == 10


def test_matches_pagination_offset(tmp_path):
    c, store = _app(tmp_path)
    # 第 1 页（limit=4）
    p1 = c.get("/api/matches?limit=4&offset=0").json()
    assert p1["total"] == 10
    assert len(p1["matches"]) == 4
    assert p1["offset"] == 0
    # 第 3 页（offset=8，剩 2 条）
    p3 = c.get("/api/matches?limit=4&offset=8").json()
    assert p3["total"] == 10
    assert len(p3["matches"]) == 2
    assert p3["offset"] == 8
    # 两页 match id 不重叠
    ids1 = {m["id"] for m in p1["matches"]}
    ids3 = {m["id"] for m in p3["matches"]}
    assert ids1.isdisjoint(ids3)


def test_matches_total_filtered_by_game(tmp_path):
    c, store = _app(tmp_path)
    holdem = c.get("/api/matches?game_id=holdem&limit=100").json()
    assert holdem["total"] == 7
    assert all(m["game_id"] == "holdem" for m in holdem["matches"])
    gomoku = c.get("/api/matches?game_id=gomoku&limit=100").json()
    assert gomoku["total"] == 3
    assert all(m["game_id"] == "gomoku" for m in gomoku["matches"])


def test_matches_total_filtered_by_status(tmp_path):
    c, store = _app(tmp_path)
    completed = c.get("/api/matches?status=completed&limit=100").json()
    assert completed["total"] == 7
    aborted = c.get("/api/matches?status=aborted&limit=100").json()
    assert aborted["total"] == 3
    # 组合：holdem + completed
    both = c.get("/api/matches?status=completed&game_id=holdem&limit=100").json()
    assert both["total"] == 7


def test_match_metadata_is_small_and_replay_events_load_separately(tmp_path):
    c, store = _app(tmp_path)
    events = [
        {
            "type": "move",
            "player": index % 2,
            "x": index % 15,
            "y": (index // 15) % 15,
            "move_index": index,
            "scores": [0, 0],
        }
        for index in range(2_500)
    ]
    store.update_match(
        "mg1",
        status="completed",
        winner=0,
        reason="five",
        result={"rounds_played": 2_500, "deltas": [1, -1], "normalized_delta": 1},
    )
    store.upsert_replay("mg1", json.dumps(events))

    detail = c.get("/api/matches/mg1")
    assert detail.status_code == 200
    assert set(detail.json()) == {"match"}
    assert "replay" not in detail.json()
    # Metadata size is independent of replay length; the old endpoint embedded
    # the escaped event string and grew to hundreds of kilobytes here.
    assert len(detail.content) < 8_000

    replay = c.get("/api/matches/mg1/replay")
    assert replay.status_code == 200
    payload = replay.json()
    assert set(payload) == {"match_id", "events", "event_count", "updated_at"}
    assert payload["match_id"] == "mg1"
    assert payload["event_count"] == 2_501
    assert payload["events"][0]["type"] == "move"
    assert payload["events"][-1] == {
        "type": "match_end",
        "winner": 0,
        "reason": "five",
        "deltas": [1, -1],
    }


def test_replay_contract_handles_missing_malformed_and_drifted_matches(tmp_path):
    c, store = _app(tmp_path)
    source = store.get_match("mh0")
    store.create_match(
        "active-without-replay",
        bot_a_id=source["bot_a_id"],
        bot_b_id=source["bot_b_id"],
        owner_id=source["owner_id"],
        game_id="holdem",
    )

    empty = c.get("/api/matches/active-without-replay/replay")
    assert empty.status_code == 200
    assert empty.json() == {
        "match_id": "active-without-replay",
        "events": [],
        "event_count": 0,
        "updated_at": None,
    }

    store.update_match(
        "mh4",
        status="completed",
        winner=1,
        reason="completed",
        result={"rounds_played": 0, "deltas": [-7, 7], "normalized_delta": -0.07},
    )
    store.upsert_replay("mh4", "not-json")
    malformed = c.get("/api/matches/mh4/replay")
    assert malformed.status_code == 200
    assert malformed.json()["events"] == [
        {
            "type": "match_end",
            "winner": 1,
            "reason": "completed",
            "deltas": [-7, 7],
        }
    ]

    # matches_index only locates a physical row. A deliberately drifted index
    # must not make either endpoint synthesize a match/replay response.
    with store._tx() as conn:
        conn.execute("DELETE FROM matches_holdem WHERE id=?", ("mh5",))
    assert c.get("/api/matches/mh5").status_code == 404
    assert c.get("/api/matches/mh5/replay").status_code == 404


def test_active_human_replay_route_is_spectator_safe(tmp_path):
    c, store = _app(tmp_path)
    source = store.get_match("mh0")
    store.create_match(
        "active-human-replay",
        bot_a_id=source["bot_a_id"],
        bot_b_id=source["bot_b_id"],
        owner_id=source["owner_id"],
        match_type="human",
        game_id="holdem",
        human_user_id=source["owner_id"],
        human_seat=1,
    )
    store.update_match("active-human-replay", status="running")
    store.upsert_replay(
        "active-human-replay",
        json.dumps(
            [
                {
                    "type": "deal_hole",
                    "hand": 0,
                    "holes": [["As", "Ah"], ["Ks", "Kh"]],
                },
                {
                    "type": "your_turn",
                    "player": 1,
                    "request": {"my_id": 1, "my_cards": [44, 45]},
                },
            ]
        ),
    )

    payload = c.get("/api/matches/active-human-replay/replay").json()
    assert payload["events"] == [
        {"type": "deal_hole", "hand": 0, "holes": [[], []]},
    ]
    assert "my_cards" not in json.dumps(payload)


def test_list_and_detail_project_only_replay_incidents(tmp_path):
    c, store = _app(tmp_path)
    events = [
        {"type": "move", "player": index % 2, "x": index % 15, "y": 0}
        for index in range(2_000)
    ]
    events.append(
        {
            "type": "bot_technical_error",
            "reason": "protocol_error",
            "code": "missing_response",
            "seat": 1,
            "turn": 2,
            "error": "/private/raw-output",
        }
    )
    store.upsert_replay("mh1", json.dumps(events))
    statements: list[str] = []
    store._conn.set_trace_callback(statements.append)
    try:
        listed = store.list_matches(game_id="holdem", limit=100)
        detailed = store.get_match_detailed("mh1")
    finally:
        store._conn.set_trace_callback(None)

    listed_match = next(row for row in listed if row["id"] == "mh1")
    for match in (listed_match, detailed):
        assert match["result"]["technical_incidents_by_seat"] == {0: 0, 1: 1}
        assert match["result"]["technical_incident_samples"][0]["code"] == "missing_response"
        assert "/private" not in json.dumps(match, ensure_ascii=False)

    match_selects = [
        sql for sql in statements
        if sql.lstrip().upper().startswith("SELECT") and "match_replays mr" in sql
    ]
    assert len(match_selects) >= 2
    assert all("json_group_array(json(je.value))" in sql for sql in match_selects)
    assert all("SELECT mr.events_json" not in sql for sql in match_selects)


def test_public_match_and_replay_hide_free_form_terminal_errors(tmp_path):
    c, store = _app(tmp_path)
    private = "error:/private/bot_uploads/secret traceback"
    store.update_match("mg0", status="aborted", reason=private)
    store.upsert_replay(
        "mg0",
        json.dumps(
            [
                {"type": "match_start", "game_id": "gomoku"},
                {"type": "match_end", "winner": 0, "reason": "completed"},
                {"type": "error", "reason": "version_unavailable", "message": private},
                {
                    "type": "error",
                    "reason": "unknown_private_code",
                    "message": private,
                    "path": "/private/bot.bin",
                },
            ]
        ),
    )

    listed = c.get("/api/matches?status=aborted&limit=100").json()["matches"]
    assert next(row for row in listed if row["id"] == "mg0")["reason"] == "platform_error"
    detail = c.get("/api/matches/mg0").json()
    assert detail["match"]["reason"] == "platform_error"
    assert "replay" not in detail
    public_events = _replay_events(c, "mg0")
    assert public_events == [
        {"type": "match_start", "game_id": "gomoku"},
        {"type": "error", "reason": "platform_error"},
    ]
    assert "/private" not in json.dumps(detail, ensure_ascii=False)

    # Public reads are projections only; the raw historical storage is retained
    # for administrator log/data repair and is not silently rewritten here.
    assert private in store.get_replay("mg0")["events_json"]

    # The authoritative match row, not whichever terminal happened to be last
    # in a historical replay, decides the public outcome. A completed row drops
    # stale errors and repairs the old winner=null/final_chips terminal shape.
    store.update_match(
        "mh0",
        status="completed",
        winner=1,
        reason="completed",
        result={"deltas": [-5250, 5250]},
    )
    store.upsert_replay(
        "mh0",
        json.dumps(
            [
                {"type": "match_start", "game_id": "holdem"},
                {"type": "error", "message": private},
                {
                    "type": "match_end",
                    "winner": None,
                    "reason": None,
                    "final_chips": [-5250, 5250],
                },
            ]
        ),
    )
    completed = c.get("/api/matches/mh0").json()
    assert "replay" not in completed
    assert _replay_events(c, "mh0") == [
        {"type": "match_start", "game_id": "holdem"},
        {
            "type": "match_end",
            "winner": 1,
            "reason": "completed",
            "deltas": [-5250, 5250],
        },
    ]

    # Active rows never have a terminal reason/event, even if a corrupted old
    # row/replay contains private text before the startup migration repairs it.
    source = store.get_match("mh0")
    store.create_match(
        "active-private",
        bot_a_id=source["bot_a_id"],
        bot_b_id=source["bot_b_id"],
        owner_id=source["owner_id"],
        game_id="holdem",
    )
    store.update_match(
        "active-private", status="running", reason=private,
    )
    store.upsert_replay(
        "active-private",
        json.dumps([{"type": "error", "message": private}]),
    )
    active = c.get("/api/matches/active-private").json()
    assert active["match"]["reason"] == ""
    assert _replay_events(c, "active-private") == []
    assert "/private" not in json.dumps(active, ensure_ascii=False)

    # Global match search is a minimal public projection. It must not return the
    # raw result/match_config blobs or a free-form completed reason.
    store.update_match(
        "mh1",
        status="completed",
        reason="privateadapterfailure",
        result={"deltas": [1, -1], "diagnostic": private},
    )
    searched = c.get("/api/search?type=matches&q=mh1").json()["matches"]
    assert len(searched) == 1
    assert searched[0]["id"] == "mh1"
    assert searched[0]["reason"] == "completed"
    assert "result" not in searched[0]
    assert "match_config" not in searched[0]
    assert "/private" not in json.dumps(searched, ensure_ascii=False)

    detail = c.get("/api/matches/mh1").json()["match"]
    assert detail["result"] == {"deltas": [1, -1]}
    assert "match_config" not in detail
    listed = c.get("/api/matches?limit=100").json()["matches"]
    listed_mh1 = next(row for row in listed if row["id"] == "mh1")
    assert listed_mh1["result"] == {"deltas": [1, -1]}
    assert "match_config" not in listed_mh1
    bot_history = c.get(
        f"/api/bots/{source['bot_a_id']}/matches?limit=100"
    ).json()["matches"]
    history_mh1 = next(row for row in bot_history if row["id"] == "mh1")
    assert history_mh1["result"] == {"deltas": [1, -1]}
    assert "match_config" not in history_mh1
    assert "/private" not in json.dumps(
        {"detail": detail, "listed": listed_mh1, "history": history_mh1},
        ensure_ascii=False,
    )

    store.update_match("mh2", status="completed", reason="内部异常路径")
    chinese_private = c.get("/api/matches/mh2").json()
    assert chinese_private["match"]["reason"] == "completed"
    assert _replay_events(c, "mh2")[-1]["reason"] == "completed"
    assert "内部异常路径" not in json.dumps(chinese_private, ensure_ascii=False)


def test_matches_normalize_legacy_incidents_to_one_current_contract(tmp_path):
    c, store = _app(tmp_path)
    # Historical completed bug: result already has the authoritative 70-count,
    # while replay contains the same 70 events. Aggregation must not double it or
    # expose the historical raw exception/path.
    legacy_events = [
        {
            "type": "bot_decide_error",
            "seat": 0,
            "turn": turn + 1,
            "error": "/private/bot_uploads/secret: missing response",
        }
        for turn in range(70)
    ]
    store.update_match(
        "mh0",
        result={
            "bot_decide_errors": {"0": 70, "1": 0},
            "bot_decide_error_samples": [legacy_events[0]],
        },
    )
    store.upsert_replay("mh0", json.dumps(legacy_events))

    # Another historical row has diagnostics only in replay.
    store.upsert_replay(
        "mh1",
        json.dumps(
            [
                {
                    "type": "bot_technical_error",
                    "reason": "protocol_error",
                    "code": "missing_response",
                    "seat": 1,
                    "turn": 2,
                    "error": "safe",
                }
            ]
        ),
    )

    # Canonical bounded replay has fewer samples than its persisted total.
    current_samples = [
        {
            "type": "technical_incident",
            "reason": "protocol_error",
            "code": code,
            "seat": 0,
            "turn": turn,
            "error": "safe",
        }
        for turn, code in enumerate(
            (
                "invalid_response",
                "missing_keep_running",
                "invalid_keep_running",
            ),
            start=1,
        )
    ]
    store.update_match(
        "mh2",
        result={
            "technical_incident_count": 8,
            "technical_incident_samples": current_samples,
        },
    )
    store.upsert_replay("mh2", json.dumps(current_samples))
    # Canonical replay-only rows must also participate in the SQL filter; this
    # guards against recognizing only result counts or historical event names.
    store.upsert_replay("mh3", json.dumps([current_samples[0]]))

    # Cross-game + malformed replay coverage for the SQL JSON filter.
    store.upsert_replay("mg0", json.dumps([legacy_events[0]]))
    store.upsert_replay("mg1", "not-json")

    all_rows = c.get("/api/matches?limit=100").json()
    assert all_rows["total"] == 10  # default still includes every status/result
    by_id = {row["id"]: row for row in all_rows["matches"]}
    assert by_id["mh0"]["result"]["technical_incidents_by_seat"] == {"0": 70, "1": 0}
    assert len(by_id["mh0"]["result"]["technical_incident_samples"]) == 3
    assert "bot_decide_errors" not in by_id["mh0"]["result"]
    assert "bot_decide_error_samples" not in by_id["mh0"]["result"]
    assert "/private" not in json.dumps(by_id["mh0"]["result"], ensure_ascii=False)
    assert by_id["mh1"]["result"]["technical_incidents_by_seat"] == {"0": 0, "1": 1}
    assert by_id["mh2"]["result"]["technical_incidents_by_seat"] == {"0": 8, "1": 0}
    assert len(by_id["mh2"]["result"]["technical_incident_samples"]) == 3
    assert {
        sample["code"]
        for sample in by_id["mh2"]["result"]["technical_incident_samples"]
    } == {"invalid_response", "missing_keep_running", "invalid_keep_running"}
    assert all(
        "历史记录" not in sample["error"]
        for sample in by_id["mh2"]["result"]["technical_incident_samples"]
    )
    assert by_id["mh3"]["result"]["technical_incident_count"] == 1
    assert by_id["mh3"]["result"]["technical_incidents_by_seat"] == {"0": 1, "1": 0}

    only_errors = c.get("/api/matches?has_technical_incidents=true&limit=100").json()
    assert only_errors["total"] == 5
    assert {row["id"] for row in only_errors["matches"]} == {
        "mh0",
        "mh1",
        "mh2",
        "mh3",
        "mg0",
    }
    assert c.get("/api/matches?has_technical_incidents=false&limit=100").json()["total"] == 5
    completed_errors = c.get(
        "/api/matches?status=completed&has_technical_incidents=true&limit=100"
    ).json()
    assert completed_errors["total"] == 4
    for retired_name in ("has_bot_incidents", "has_bot_errors"):
        rejected_legacy_filter = c.get(
            f"/api/matches?{retired_name}=true&limit=100"
        )
        assert rejected_legacy_filter.status_code == 400
        assert retired_name in rejected_legacy_filter.text
        assert "has_technical_incidents" in rejected_legacy_filter.text

    match_route = next(
        route
        for route in router.routes
        if getattr(route, "path", None) == "/api/matches"
        and "GET" in getattr(route, "methods", set())
    )
    match_query_parameters = {
        parameter.name for parameter in match_route.dependant.query_params
    }
    assert "has_technical_incidents" in match_query_parameters
    assert not {"has_bot_incidents", "has_bot_errors"} & match_query_parameters

    detail_body = c.get("/api/matches/mh1").json()
    detail = detail_body["match"]
    assert detail["result"]["technical_incidents_by_seat"] == {"0": 0, "1": 1}
    assert detail["result"]["technical_incident_samples"][0]["code"] == "missing_response"

    legacy_detail = c.get("/api/matches/mh0").json()
    public_events = _replay_events(c, "mh0")
    assert len([e for e in public_events if e["type"] == "technical_incident"]) == 3
    assert not [
        e
        for e in public_events
        if e["type"] in {"bot_decide_error", "bot_technical_error"}
    ]
    assert "/private" not in json.dumps(public_events, ensure_ascii=False)

    # A terminal SSE subscription must use the same public read boundary. This
    # exercises the actual route generator, not only Store.get_public_replay().
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/matches/mh1/events",
            "headers": [],
            "app": c.app,
        }
    )

    async def consume_terminal_snapshot() -> list[str]:
        response = await match_events("mh1", request)
        return [chunk async for chunk in response.body_iterator]

    chunks = asyncio.run(consume_terminal_snapshot())
    assert len(chunks) == 1
    assert '"type": "technical_incident"' in chunks[0]
    assert '"type": "bot_decide_error"' not in chunks[0]
    assert '"type": "bot_technical_error"' not in chunks[0]
