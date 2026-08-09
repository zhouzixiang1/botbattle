"""段位称号 + 排名变化趋势测试（PR-5）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.games import registry
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def test_tier_boundaries():
    tier_for = lambda rating: registry.tier_for("holdem", rating)
    assert tier_for(0).key == "novice"
    assert tier_for(1599).key == "novice"
    assert tier_for(1600).key == "bronze"      # 边界含
    assert tier_for(1749).key == "bronze"
    assert tier_for(1750).key == "silver"
    assert tier_for(1900).key == "gold"
    assert tier_for(2050).key == "expert"
    assert tier_for(2200).key == "master"
    assert tier_for(9999).key == "master"
    assert tier_for(None).key == "novice"


def test_tier_dict_structure():
    d = registry.tier_dict("holdem", 1800)
    for k in ("level", "key", "name", "color", "bg", "min_rating"):
        assert k in d
    assert d["name"] == "熟练"


def test_all_tiers_descending():
    ts = registry.all_tiers("holdem")
    assert len(ts) == 6
    # level 降序
    levels = [t["level"] for t in ts]
    assert levels == sorted(levels, reverse=True)


def test_leaderboard_includes_tier_and_delta(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    s.ensure_rating(b["id"])
    s.update_rating_row(b["id"], rating=1850, matches_played=2)
    # 落两条历史：prev=1700, current=1850 → delta=+150
    s.add_rating_history(b["id"], 1700, 80, 0.06, 1)
    s.add_rating_history(b["id"], 1850, 80, 0.06, 2)
    lb = s.list_leaderboard()
    assert len(lb) == 1
    row = lb[0]
    assert row["tier_name"] == "熟练"
    assert row["tier_level"] == 2
    assert row["rating_delta"] == 150.0
    s.close()


def test_leaderboard_no_history_delta_none(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    s.ensure_rating(b["id"])
    s.update_rating_row(b["id"], rating=1500)
    lb = s.list_leaderboard()
    assert lb[0]["rating_delta"] is None
    assert lb[0]["tier_name"] == "新手"
    s.close()


def test_bot_profile_includes_tier(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    s.ensure_rating(b["id"])
    s.update_rating_row(b["id"], rating=2100)
    p = s.bot_profile(b["id"])
    assert p["tier_name"] == "专家"
    assert p["tier_level"] == 4
    s.close()


def test_tiers_endpoint_and_leaderboard_endpoint(tmp_path):
    app = create_app(db_path=str(tmp_path / "app.db"))
    c = TestClient(app)
    r = c.get("/api/tiers?game_id=holdem")
    assert r.status_code == 200
    assert len(r.json()["tiers"]) == 6
    assert r.json()["placement_required"] == 10
    r = c.get("/api/leaderboard")
    assert r.status_code == 200
    # 空榜单也应 200
    assert "leaderboard" in r.json()


def test_public_placement_contract_and_formal_ranking_order(tmp_path):
    app = create_app(db_path=str(tmp_path / "placement.db"))
    store = app.state.store
    user = store.create_user("alice", "a@ex.com", "x")
    provisional = store.create_bot(
        user["id"], "provisional", binary_path="/tmp/p", format="elf", game_id="gomoku"
    )
    placed = store.create_bot(
        user["id"], "placed", binary_path="/tmp/f", format="elf", game_id="gomoku"
    )
    for bot in (provisional, placed):
        store.ensure_rating(bot["id"])
    store.update_rating_row(
        provisional["id"], rating=2300, matches_played=9,
        wins=9, losses=0, draws=0,
    )
    store.update_rating_row(
        placed["id"], rating=1400, matches_played=10,
        wins=5, losses=5, draws=0,
    )

    client = TestClient(app)
    rows = client.get("/api/leaderboard?game_id=gomoku").json()["leaderboard"]
    assert [row["bot_id"] for row in rows] == [placed["id"], provisional["id"]]
    assert rows[0]["is_placement"] is False
    assert rows[0]["placement_remaining"] == 0
    assert rows[1]["is_placement"] is True
    assert rows[1]["placement_required"] == 10
    assert rows[1]["placement_remaining"] == 1

    profile = client.get(f"/api/bots/{provisional['id']}/profile").json()["profile"]
    assert profile["is_placement"] is True
    assert profile["placement_required"] == 10
    assert profile["placement_remaining"] == 1


def test_leaderboard_rejects_unknown_game(tmp_path):
    app = create_app(db_path=str(tmp_path / "unknown.db"))
    client = TestClient(app)
    assert client.get("/api/leaderboard?game_id=unknown").status_code == 400


def test_disabled_placement_contract_does_not_hide_tier_or_reorder(tmp_path):
    store = Store(str(tmp_path / "placement-off.db"))
    user = store.create_user("alice", "a@ex.com", "x")
    lower = store.create_bot(user["id"], "lower", game_id="holdem")
    higher = store.create_bot(user["id"], "higher", game_id="holdem")
    for bot in (lower, higher):
        store.ensure_rating(bot["id"])
    store.update_rating_row(lower["id"], rating=1400, matches_played=20)
    store.update_rating_row(higher["id"], rating=2000, matches_played=0)
    rows = store.list_leaderboard(game_id="holdem", placement_games=0)
    assert [row["bot_id"] for row in rows] == [higher["id"], lower["id"]]
    assert all(row["placement_required"] == 0 for row in rows)
    assert all(row["is_placement"] is False for row in rows)
    store.close()
