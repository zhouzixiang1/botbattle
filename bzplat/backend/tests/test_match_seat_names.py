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


def test_match_detail_exposes_winner_and_earnings_for_viewer(tmp_path):
    """观赛页顶栏胜者/累计筹码依赖 match.winner + earnings_a/b 字段。"""
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
        mid, status="completed", winner=0, earnings_a=1500, earnings_b=-1500,
        hands_played=70, reason="completed",
    )
    c = TestClient(app)

    r = c.get(f"/api/matches/{mid}")
    assert r.status_code == 200
    m = r.json()["match"]
    assert m["winner"] == 0
    assert m["earnings_a"] == 1500
    assert m["earnings_b"] == -1500
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
