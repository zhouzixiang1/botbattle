"""段位称号 + 排名变化趋势测试（PR-5）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.engine.tiers import tier_for, tier_dict, all_tiers
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def test_tier_boundaries():
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
    d = tier_dict(1800)
    for k in ("level", "key", "name", "color", "bg", "min_rating"):
        assert k in d
    assert d["name"] == "熟练"


def test_all_tiers_descending():
    ts = all_tiers()
    assert len(ts) == 6
    # level 降序
    levels = [t["level"] for t in ts]
    assert levels == sorted(levels, reverse=True)


def test_leaderboard_includes_tier_and_delta(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", is_public=1, game_id="holdem")
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
    b = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", is_public=1, game_id="holdem")
    s.ensure_rating(b["id"])
    s.update_rating_row(b["id"], rating=1500)
    lb = s.list_leaderboard()
    assert lb[0]["rating_delta"] is None
    assert lb[0]["tier_name"] == "新手"
    s.close()


def test_bot_profile_includes_tier(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", is_public=1, game_id="holdem")
    s.ensure_rating(b["id"])
    s.update_rating_row(b["id"], rating=2100)
    p = s.bot_profile(b["id"])
    assert p["tier_name"] == "专家"
    assert p["tier_level"] == 4
    s.close()


def test_tiers_endpoint_and_leaderboard_endpoint(tmp_path):
    app = create_app(db_path=str(tmp_path / "app.db"))
    c = TestClient(app)
    r = c.get("/api/tiers")
    assert r.status_code == 200
    assert len(r.json()["tiers"]) == 6
    r = c.get("/api/leaderboard")
    assert r.status_code == 200
    # 空榜单也应 200
    assert "leaderboard" in r.json()
