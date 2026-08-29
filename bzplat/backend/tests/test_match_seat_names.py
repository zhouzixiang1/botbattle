"""对局详情座位身份 JOIN 测试（get_match_detailed / _with_seat_info）。

验证统一观赛/回放页能拿到双方 BOT 名 + @用户名（canvas 绘制座位标签用），
以及人类对局标 is_human。覆盖 GET /api/matches/{id} 与 store.get_match_detailed。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "sn.db"))


def test_get_match_detailed_joins_bot_and_owner_names(tmp_path):
    """get_match_detailed 返回 bot_a/bot_b 名 + owner 名（JOIN bots+users）。"""
    s = _store(tmp_path)
    ua = s.create_user("alice", "a@ex.com", hash_password("pw"))
    ub = s.create_user("bob", "b@ex.com", hash_password("pw"))
    ba = s.create_bot(ua["id"], "AlphaBot", binary_path="/tmp", format="elf", game_id="holdem")
    bb = s.create_bot(ub["id"], "DeepHoldem", binary_path="/tmp", format="elf", game_id="holdem")
    s.ensure_rating(ba["id"]); s.ensure_rating(bb["id"])
    mid = "20260802-seat-1"
    s.create_match(mid, bot_a_id=ba["id"], bot_b_id=bb["id"], owner_id=ua["id"], game_id="holdem")

    m = s.get_match_detailed(mid)
    assert m is not None
    assert m["bot_a_name"] == "AlphaBot"
    assert m["bot_b_name"] == "DeepHoldem"
    assert m["bot_a_owner_name"] == "alice"
    assert m["bot_b_owner_name"] == "bob"
    # 原 match 基础字段仍在
    assert m["game_id"] == "holdem"
    assert m["bot_a_id"] == ba["id"]
    s.close()


def test_match_detail_route_returns_nested_seat_info(tmp_path):
    """GET /api/matches/{id} 返回嵌套 bot_a/bot_b（含 name/owner_name/is_human）。"""
    s = _store(tmp_path)
    app = create_app(db_path=s.path)
    st = app.state.store
    ua = st.create_user("alice", "a@ex.com", hash_password("pw"))
    ub = st.create_user("bob", "b@ex.com", hash_password("pw"))
    ba = st.create_bot(ua["id"], "AlphaBot", binary_path="/tmp", format="elf", game_id="holdem")
    bb = st.create_bot(ub["id"], "DeepHoldem", binary_path="/tmp", format="elf", game_id="holdem")
    st.ensure_rating(ba["id"]); st.ensure_rating(bb["id"])
    mid = "20260802-seat-2"
    st.create_match(mid, bot_a_id=ba["id"], bot_b_id=bb["id"], owner_id=ua["id"], game_id="holdem")
    c = TestClient(app)

    r = c.get(f"/api/matches/{mid}")
    assert r.status_code == 200
    m = r.json()["match"]
    assert m["bot_a"]["name"] == "AlphaBot"
    assert m["bot_a"]["owner_name"] == "alice"
    assert m["bot_a"]["is_human"] is False
    assert m["bot_b"]["name"] == "DeepHoldem"
    assert m["bot_b"]["owner_name"] == "bob"
    assert m["bot_b"]["is_human"] is False
    # 扁平 JOIN 列应被清理（已挪进 bot_a/bot_b）
    assert "bot_a_name" not in m
    assert "bot_b_owner_name" not in m
    for internal in (
        "owner_id", "human_user_id", "match_seed", "_replay_events_json",
    ):
        assert internal not in m
    allowed = {
        "id", "bot_a_id", "bot_b_id", "contest_id", "winner", "reason",
        "match_type", "status", "game_id", "result", "human_seat",
        "technical_loss", "started_at", "ended_at", "created_at",
        "likes_count", "views_count", "rated", "rating_reason",
        "rating_settled", "bot_a_environment", "bot_b_environment",
        "bot_a", "bot_b", "can_view_debug", "outcome",
    }
    assert set(m) <= allowed


def test_match_detail_exposes_winner_and_earnings_for_viewer(tmp_path):
    """观赛页顶栏胜者/累计分差依赖 match.winner + result.deltas。"""
    s = _store(tmp_path)
    app = create_app(db_path=s.path)
    st = app.state.store
    ua = st.create_user("alice", "a@ex.com", hash_password("pw"))
    ub = st.create_user("bob", "b@ex.com", hash_password("pw"))
    ba = st.create_bot(ua["id"], "AlphaBot", binary_path="/tmp", format="elf", game_id="holdem")
    bb = st.create_bot(ub["id"], "DeepHoldem", binary_path="/tmp", format="elf", game_id="holdem")
    st.ensure_rating(ba["id"]); st.ensure_rating(bb["id"])
    mid = "20260802-seat-winner"
    st.create_match(mid, bot_a_id=ba["id"], bot_b_id=bb["id"], owner_id=ua["id"], game_id="holdem")
    st.update_match(
        mid, status="completed", winner=0,
        result={"rounds_played": 70, "deltas": [1500, -1500]},
        reason="completed",
    )
    c = TestClient(app)

    r = c.get(f"/api/matches/{mid}")
    assert r.status_code == 200
    m = r.json()["match"]
    assert m["winner"] == 0
    assert m["result"]["deltas"][0] == 1500
    assert m["result"]["deltas"][1] == -1500
    assert m["bot_a"]["name"] == "AlphaBot"
    assert m["bot_b"]["name"] == "DeepHoldem"


def test_match_detail_human_match_marks_is_human(tmp_path):
    """人类对局：human_seat 那侧标 is_human=True，且 owner 为真人用户名（非 bot 主人复用）。"""
    s = _store(tmp_path)
    app = create_app(db_path=s.path)
    st = app.state.store
    u_human = st.create_user("human_player", "h@ex.com", hash_password("pw"))
    u_bot_owner = st.create_user("bot_owner", "o@ex.com", hash_password("pw"))
    b = st.create_bot(
        u_bot_owner["id"], "AlphaBot", binary_path="/tmp", format="elf", game_id="gomoku",
    )
    st.ensure_rating(b["id"])
    mid = "20260802-seat-3"
    # 人类坐座 1（bot_seat=0，两侧复用同一 bot_id 满足 NOT NULL FK）
    st.create_match(
        mid, bot_a_id=b["id"], bot_b_id=b["id"], owner_id=u_human["id"],
        game_id="gomoku", match_type="human", human_user_id=u_human["id"], human_seat=1,
    )
    c = TestClient(app)

    r = c.get(f"/api/matches/{mid}")
    assert r.status_code == 200
    m = r.json()["match"]
    assert m["bot_a"]["is_human"] is False   # seat 0 = bot
    assert m["bot_a"]["name"] == "AlphaBot"
    assert m["bot_a"]["owner_name"] == "bot_owner"
    assert m["bot_b"]["is_human"] is True    # seat 1 = human
    assert m["bot_b"]["owner_name"] == "human_player"
    assert m["bot_b"]["name"] in ("human_player", "人类") or m["bot_b"]["display_name"]
    assert m["human_seat"] == 1
    for internal in (
        "owner_id", "human_user_id", "match_seed", "_replay_events_json",
    ):
        assert internal not in m


def test_match_list_exposes_bot_owners_and_real_human_without_private_ids(tmp_path):
    """History 列表与详情共用座位身份，不把 Bot 主人冒充真人。"""
    app = create_app(db_path=str(tmp_path / "history-identities.db"))
    st = app.state.store
    alice = st.create_user(
        "alice", "alice@example.com", hash_password("pw"), display_name="Alice"
    )
    bob = st.create_user(
        "bob", "bob@example.com", hash_password("pw"), display_name="Bob"
    )
    bot_owner = st.create_user(
        "bot_owner", "owner@example.com", hash_password("pw"),
        display_name="Bot 主人",
    )
    alpha = st.create_bot(
        alice["id"], "alpha", display_name="Alpha Bot", binary_path="/tmp",
        format="elf", game_id="gomoku",
    )
    beta = st.create_bot(
        bot_owner["id"], "beta", display_name="Beta Bot", binary_path="/tmp",
        format="elf", game_id="gomoku",
    )
    st.create_match(
        "history-challenge", bot_a_id=alpha["id"], bot_b_id=beta["id"],
        owner_id=alice["id"], game_id="gomoku", match_type="challenge",
    )
    st.create_match(
        "history-human", bot_a_id=beta["id"], bot_b_id=beta["id"],
        owner_id=bob["id"], game_id="gomoku", match_type="human",
        human_user_id=bob["id"], human_seat=1,
    )
    st.create_match(
        "history-selfplay", bot_a_id=alpha["id"], bot_b_id=alpha["id"],
        owner_id=alice["id"], game_id="gomoku", match_type="challenge",
    )
    st.update_match(
        "history-challenge", status="completed", reason="five", winner=0,
        match_seed=101,
        result={"rounds_played": 9, "deltas": [1, -1], "normalized_delta": 1},
    )
    st.update_match(
        "history-human", status="completed", reason="five", winner=1,
        match_seed=202,
        result={"rounds_played": 11, "deltas": [-1, 1], "normalized_delta": -1},
    )
    st.update_match(
        "history-selfplay", status="completed", reason="five", winner=1,
        result={"rounds_played": 7, "deltas": [-1, 1], "normalized_delta": -1},
    )
    st.like(alice["id"], "match", "history-human")
    st.like(alice["id"], "match", "history-selfplay")

    client = TestClient(app)
    response = client.get("/api/matches?limit=20")
    assert response.status_code == 200
    rows = {row["id"]: row for row in response.json()["matches"]}

    challenge = rows["history-challenge"]
    assert challenge["bot_a"] == {
        "id": alpha["id"],
        "name": "alpha",
        "display_name": "Alpha Bot",
        "owner_name": "alice",
        "owner_display": "Alice",
        "is_human": False,
    }
    assert challenge["bot_b"]["owner_name"] == "bot_owner"
    assert challenge["bot_b"]["is_human"] is False

    human = rows["history-human"]
    assert human["bot_a"]["name"] == "beta"
    assert human["bot_a"]["owner_name"] == "bot_owner"
    assert human["bot_b"]["id"] is None
    assert human["bot_b"]["name"] == "Bob"
    assert human["bot_b"]["owner_name"] == "bob"
    assert human["bot_b"]["is_human"] is True
    for private_or_flat in (
        "human_user_id",
        "human_seat",
        "human_user_name",
        "human_user_display",
        "bot_a_owner_name",
        "bot_b_owner_name",
        "owner_id",
        "match_seed",
    ):
        assert private_or_flat not in human

    selfplay = rows["history-selfplay"]
    assert selfplay["bot_a"]["id"] == alpha["id"]
    assert selfplay["bot_b"]["id"] == alpha["id"]
    assert selfplay["bot_a"]["owner_name"] == "alice"
    assert selfplay["bot_b"]["owner_name"] == "alice"
    assert selfplay["bot_a"]["is_human"] is False
    assert selfplay["bot_b"]["is_human"] is False
    assert "likes_count" not in selfplay and "views_count" not in selfplay

    # Home、Bot 详情、搜索和热门对局都必须收到同一个嵌套身份契约。
    bot_rows = client.get(
        f"/api/bots/{beta['id']}/matches?page=1&per_page=20"
    ).json()["matches"]
    search_rows = client.get(
        "/api/search?q=bob&type=matches&limit=20"
    ).json()["matches"]
    liked_rows = client.get("/api/matches/liked-top?limit=20").json()["matches"]
    endpoint_rows = [bot_rows, search_rows, liked_rows]
    for public_rows in endpoint_rows:
        projected = next(row for row in public_rows if row["id"] == "history-human")
        assert projected["match_type"] == "human"
        assert projected["bot_a"]["owner_name"] == "bot_owner"
        assert projected["bot_b"]["owner_name"] == "bob"
        assert projected["bot_b"]["is_human"] is True
        for internal in (
            "owner_id", "human_user_id", "human_seat", "match_seed",
            "bot_a_owner_name", "bot_b_owner_name", "human_user_name",
        ):
            assert internal not in projected
    assert all(
        "likes_count" not in row and "views_count" not in row
        for rows_without_engagement in (bot_rows, search_rows)
        for row in rows_without_engagement
    )
    liked_human = next(row for row in liked_rows if row["id"] == "history-human")
    assert liked_human["likes_count"] == 1
    assert "views_count" in liked_human

    # 自博弈在每个公开列表入口都必须保留两个独立座位，
    # 前端才能以相同 Bot id 识别其性质，同时仍按座位展示胜负。
    selfplay_endpoint_rows = [
        client.get(
            f"/api/bots/{alpha['id']}/matches?page=1&per_page=20"
        ).json()["matches"],
        client.get(
            "/api/search?q=alpha&type=matches&limit=20"
        ).json()["matches"],
        liked_rows,
    ]
    for public_rows in selfplay_endpoint_rows:
        projected = next(
            row for row in public_rows if row["id"] == "history-selfplay"
        )
        assert projected["bot_a"]["id"] == alpha["id"]
        assert projected["bot_b"]["id"] == alpha["id"]
        assert projected["bot_a"]["owner_name"] == "alice"
        assert projected["bot_b"]["owner_name"] == "alice"
        assert projected["bot_a"]["is_human"] is False
        assert projected["bot_b"]["is_human"] is False
