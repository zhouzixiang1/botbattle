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
    # 建若干 holdem + gomoku 对局（completed / aborted 混合）
    for i in range(7):
        mid = f"mh{i}"
        store.create_match(mid, bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=u["id"], game_id="holdem")
        store.update_match(mid, status="completed", winner=0)
    for i in range(3):
        mid = f"mg{i}"
        store.create_match(mid, bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=u["id"], game_id="gomoku")
        store.update_match(mid, status="aborted")
    c = TestClient(app)
    return c, store


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
    store.upsert_replay("mh0", json.dumps(legacy_events), "[]")

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
        "[]",
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
    store.upsert_replay("mh2", json.dumps(current_samples), "[]")
    # Canonical replay-only rows must also participate in the SQL filter; this
    # guards against recognizing only result counts or historical event names.
    store.upsert_replay("mh3", json.dumps([current_samples[0]]), "[]")

    # Cross-game + malformed replay coverage for the SQL JSON filter.
    store.upsert_replay("mg0", json.dumps([legacy_events[0]]), "[]")
    store.upsert_replay("mg1", "not-json", "[]")

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
    public_events = json.loads(legacy_detail["replay"]["events_json"])
    assert len([e for e in public_events if e["type"] == "technical_incident"]) == 3
    assert not [
        e
        for e in public_events
        if e["type"] in {"bot_decide_error", "bot_technical_error"}
    ]
    assert "/private" not in legacy_detail["replay"]["events_json"]

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
