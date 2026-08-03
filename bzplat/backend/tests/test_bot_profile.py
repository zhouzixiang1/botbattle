"""Bot 详情页相关 store 方法与端点测试。

覆盖 PR-1：bot_profile 聚合、pair_stats 胜负计数 + head_to_head（视角翻转）、
rating_history 落盘与读取、bot_opponents_stats、migration 幂等、HTTP 端点。
"""
from __future__ import annotations

import pytest

from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "bp.db"))


def _seed_two_bots(s: Store):
    """建 1 用户 + 2 个 holdem bot（带 rating 行）。"""
    u = s.create_user("alice", "a@ex.com", "xhash")
    b1 = s.create_bot(
        u["id"], "botA", binary_path="/tmp/a", format="elf", game_id="holdem"
    )
    b2 = s.create_bot(
        u["id"], "botB", binary_path="/tmp/b", format="elf", game_id="holdem"
    )
    s.ensure_rating(b1["id"])
    s.ensure_rating(b2["id"])
    return u, b1, b2


def test_bot_profile_returns_owner_and_rating(tmp_path):
    s = _store(tmp_path)
    u, b1, b2 = _seed_two_bots(s)
    s.update_rating_row(b1["id"], rating=1650.0, wins=3, losses=1, draws=0, matches_played=4)
    p = s.bot_profile(b1["id"])
    assert p is not None
    assert p["name"] == "botA"
    assert p["owner_name"] == "alice"
    assert p["owner_id"] == u["id"]
    assert p["game_id"] == "holdem"
    assert p["rating"] == 1650.0
    assert p["wins"] == 3
    assert p["matches_played"] == 4
    s.close()


def test_bot_profile_nonexistent_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.bot_profile(99999) is None
    s.close()


def test_pair_stats_win_loss_accumulation_and_head_to_head(tmp_path):
    s = _store(tmp_path)
    u, b1, b2 = _seed_two_bots(s)
    # 模拟 b1 胜 2 次、负 1 次、平 1 次（规范化为小 id 在前）
    lo, hi = sorted((b1["id"], b2["id"]))
    # 假设 b1.id < b2.id（按创建顺序），bot_a 视角 = b1
    is_b1_a = b1["id"] == lo
    # b1 视角：2 胜 1 负 1 平
    aw = 2 if is_b1_a else 1
    al = 1 if is_b1_a else 2
    s.upsert_pair_stats(lo, hi, 0.0, None, None, 0, a_wins_delta=aw, a_losses_delta=al, draws_delta=1)
    s.upsert_pair_stats(lo, hi, 0.0, None, None, 0, a_wins_delta=0, a_losses_delta=0, draws_delta=0)

    # b1 视角看 b2
    h_b1 = s.head_to_head(b1["id"], b2["id"])
    assert h_b1 is not None
    assert h_b1["a_wins"] == 2
    assert h_b1["a_losses"] == 1
    assert h_b1["draws"] == 1

    # b2 视角看 b1：胜负翻转
    h_b2 = s.head_to_head(b2["id"], b1["id"])
    assert h_b2 is not None
    assert h_b2["a_wins"] == 1
    assert h_b2["a_losses"] == 2
    assert h_b2["draws"] == 1
    s.close()


def test_head_to_head_never_played_returns_none(tmp_path):
    s = _store(tmp_path)
    u, b1, b2 = _seed_two_bots(s)
    assert s.head_to_head(b1["id"], b2["id"]) is None
    s.close()


def test_bot_opponents_stats_resolves_names_and_view(tmp_path):
    s = _store(tmp_path)
    u, b1, b2 = _seed_two_bots(s)
    lo, hi = sorted((b1["id"], b2["id"]))
    aw = 3 if b1["id"] == lo else 0
    al = 0 if b1["id"] == lo else 3
    s.upsert_pair_stats(lo, hi, 0.0, None, None, 0, a_wins_delta=aw, a_losses_delta=al)
    opps = s.bot_opponents_stats(b1["id"])
    assert len(opps) == 1
    o = opps[0]
    assert o["opponent_id"] == b2["id"]
    assert o["opponent_name"] == "botB"
    assert o["wins"] == 3
    assert o["losses"] == 0
    s.close()


def test_rating_history_append_and_truncate(tmp_path):
    s = _store(tmp_path)
    u, b1, b2 = _seed_two_bots(s)
    # 落多条历史
    for i in range(5):
        s.add_rating_history(b1["id"], 1500 + i, 80, 0.06, i + 1, "test")
    hist = s.list_rating_history(b1["id"])
    assert len(hist) == 5
    # 时序旧→新
    assert hist[0]["rating"] == 1500
    assert hist[4]["rating"] == 1504
    # 截断：落超过 200 条后只保留最近 200
    for i in range(250):
        s.add_rating_history(b1["id"], 1600 + i, 80, 0.06, 100, "bulk")
    hist2 = s.list_rating_history(b1["id"], limit=500)
    assert len(hist2) == 200
    # 最新的应是最后落的
    assert hist2[-1]["rating"] == 1600 + 249
    s.close()


def test_rating_history_empty_for_unrated_bot(tmp_path):
    s = _store(tmp_path)
    u, b1, b2 = _seed_two_bots(s)
    assert s.list_rating_history(b1["id"]) == []
    s.close()


def test_migration_idempotent_pair_stats_columns(tmp_path):
    """迁移幂等：多次打开同一库不重复加列、不报错。"""
    db = str(tmp_path / "mig.db")
    s1 = Store(db)
    s1.close()
    # 第二次打开（触发 _migrate 再跑）
    s2 = Store(db)
    cols = [r[1] for r in s2._conn.execute("PRAGMA table_info(pair_stats)")]
    for c in ("a_wins", "a_losses", "draws"):
        assert c in cols, f"缺列 {c}"
    # rating_history 表存在
    rh_cols = [r[1] for r in s2._conn.execute("PRAGMA table_info(rating_history)")]
    assert "bot_id" in rh_cols and "rating" in rh_cols
    s2.close()


# ── HTTP 端点测试（ASGI TestClient，无需运行服务）──────────────
def _app_with_admin(tmp_path):
    from fastapi.testclient import TestClient
    from bzplat.backend.crypto import hash_password
    from bzplat.backend.main import create_app

    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u = store.create_user("admin", "a@ex.com", hash_password("password12"), role="admin")
    store.update_user(u["id"], email_verified=1)
    _, token = app.state.auth.authenticate("admin", "password12")
    # 建 2 bot + rating + 历史
    b1 = store.create_bot(
        u["id"], "botA", binary_path="/tmp/a", format="elf", game_id="holdem"
    )
    b2 = store.create_bot(
        u["id"], "botB", binary_path="/tmp/b", format="elf", game_id="holdem"
    )
    store.ensure_rating(b1["id"])
    store.ensure_rating(b2["id"])
    store.update_rating_row(b1["id"], rating=1700, wins=2, matches_played=2)
    store.add_rating_history(b1["id"], 1700, 80, 0.06, 2, "test")
    lo, hi = sorted((b1["id"], b2["id"]))
    store.upsert_pair_stats(lo, hi, 0, None, None, 0, a_wins_delta=2, a_losses_delta=0)
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {token}"
    return c, b1, b2


def test_bot_profile_endpoint(tmp_path):
    c, b1, b2 = _app_with_admin(tmp_path)
    r = c.get(f"/api/bots/{b1['id']}/profile")
    assert r.status_code == 200
    p = r.json()["profile"]
    assert p["name"] == "botA"
    assert p["rating"] == 1700
    assert p["owner_name"] == "admin"


def test_bot_profile_404(tmp_path):
    c, b1, b2 = _app_with_admin(tmp_path)
    r = c.get("/api/bots/99999/profile")
    assert r.status_code == 404


def test_bot_matches_endpoint(tmp_path):
    c, b1, b2 = _app_with_admin(tmp_path)
    r = c.get(f"/api/bots/{b1['id']}/matches?limit=5")
    assert r.status_code == 200
    assert "matches" in r.json()


def test_bot_opponents_endpoint(tmp_path):
    c, b1, b2 = _app_with_admin(tmp_path)
    r = c.get(f"/api/bots/{b1['id']}/opponents")
    assert r.status_code == 200
    opps = r.json()["opponents"]
    assert len(opps) == 1
    assert opps[0]["opponent_name"] == "botB"
    assert opps[0]["wins"] == 2


def test_bot_rating_history_endpoint(tmp_path):
    c, b1, b2 = _app_with_admin(tmp_path)
    r = c.get(f"/api/bots/{b1['id']}/rating-history")
    assert r.status_code == 200
    hist = r.json()["history"]
    assert len(hist) == 1
    assert hist[0]["rating"] == 1700
