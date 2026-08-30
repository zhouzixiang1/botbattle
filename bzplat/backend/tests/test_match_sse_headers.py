"""SSE 观赛端点的反代兼容契约。

`GET /api/matches/{id}/events` 必须逐帧下发且禁止缓存：缺失
`X-Accel-Buffering: no` 时 nginx 的默认 proxy_buffering 会扣住首帧
snapshot，前端直播棋盘将永远停在「加载中」。
"""

import json

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store.db import Store


def _make_user(store: Store, name: str):
    return store.create_user(
        name, f"{name}@example.test", hash_password("password1")
    )


def _make_bot(store: Store, owner_id: int, name: str):
    return store.create_bot(
        owner_id,
        name,
        binary_path="/tmp/fake",
        format="elf",
        os="linux",
        arch="amd64",
        game_id="holdem",
    )


def test_match_events_stream_is_not_bufferable_or_cacheable(tmp_path):
    app = create_app(db_path=str(tmp_path / "sse.db"))
    store = app.state.store
    owner_a = _make_user(store, "sse_a")
    owner_b = _make_user(store, "sse_b")
    bot_a = _make_bot(store, owner_a["id"], "sse-a")
    bot_b = _make_bot(store, owner_b["id"], "sse-b")
    store.create_match(
        "sse-match",
        bot_a["id"],
        bot_b["id"],
        contest_id=None,
        match_type="challenge",
        game_id="holdem",
    )
    store.update_match(
        "sse-match",
        status="completed",
        winner=0,
        reason="completed",
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 0.01},
    )
    store.upsert_replay(
        "sse-match",
        json.dumps(
            [{"type": "match_end", "winner": 0, "reason": "completed", "deltas": [1, -1]}]
        ),
    )

    client = TestClient(app)
    response = client.get("/api/matches/sse-match/events")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # 反代兼容：nginx 依据该头对本响应禁用 proxy_buffering，否则首帧
    # snapshot 会被缓冲扣住，直播端收不到初始局面。
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["cache-control"] == "no-store"
    # 首帧契约：订阅后第一条 data 帧必须是完整 snapshot，终局对局
    # 发完 snapshot 即断流。
    first_data = next(
        line for line in response.text.splitlines() if line.startswith("data: ")
    )
    snapshot = json.loads(first_data[len("data: ") :])
    assert snapshot["type"] == "snapshot"
    assert snapshot["match"]["id"] == "sse-match"
    assert snapshot["match"]["status"] == "completed"
    assert any(ev["type"] == "match_end" for ev in snapshot["events"])
    # 终局对局发完 snapshot 即断流：整个响应只允许这一条 data 帧。
    data_frames = [
        line for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert len(data_frames) == 1
