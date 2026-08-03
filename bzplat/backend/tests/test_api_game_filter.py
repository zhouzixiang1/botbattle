"""API game_id 过滤测试——验证 4 个端点正确透传 game_id。

GET /api/contests?game_id=, GET /api/search?type=bots&game_id=,
GET /api/admin/bots?game_id=, GET /api/admin/contests?game_id=
"""
from __future__ import annotations

from bzplat.backend.crypto import hash_password
from fastapi.testclient import TestClient


def _app(tmp_path):
    from bzplat.backend.main import create_app
    return create_app(db_path=str(tmp_path / "gf.db"))


def _setup(app):
    store = app.state.store
    admin = store.create_user("gfadmin", "gf@a.com", hash_password("pw123456"), role="admin")
    store.update_user(admin["id"], email_verified=1)
    u = store.create_user("gfu", "gfu@a.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    # 建不同游戏的 bot（public）
    bh = store.create_bot(u["id"], "gfh", binary_path="/tmp", format="elf", game_id="holdem")
    bg = store.create_bot(u["id"], "gfg", binary_path="/tmp", format="elf", game_id="gomoku")
    # 建不同游戏的赛事
    ch = store.create_contest("GF Holdem赛", organizer_id=admin["id"], game_id="holdem")["id"]
    cg = store.create_contest("GF Gomoku赛", organizer_id=admin["id"], game_id="gomoku")["id"]
    _, atok = app.state.auth.authenticate("gfadmin", "pw123456")
    return store, {"Authorization": f"Bearer {atok}"}, ch, cg, bh, bg


def test_contests_filter_by_game(tmp_path):
    app = _app(tmp_path)
    store, h, ch, cg, _, _ = _setup(app)
    client = TestClient(app)
    r = client.get("/api/contests?game_id=holdem")
    assert r.status_code == 200
    gids = {c["game_id"] for c in r.json()["contests"]}
    assert gids == {"holdem"}, f"应只返回 holdem，实际 {gids}"
    r2 = client.get("/api/contests?game_id=gomoku")
    gids2 = {c["game_id"] for c in r2.json()["contests"]}
    assert gids2 == {"gomoku"}


def test_search_bots_filter_by_game(tmp_path):
    app = _app(tmp_path)
    store, h, _, _, bh, bg = _setup(app)
    client = TestClient(app)
    r = client.get("/api/search?type=bots&q=gf&game_id=holdem")
    bot_ids = {b["id"] for b in r.json().get("bots", [])}
    assert bh["id"] in bot_ids
    assert bg["id"] not in bot_ids, "gomoku bot 不应出现在 holdem 过滤结果"
    r2 = client.get("/api/search?type=bots&q=gf&game_id=gomoku")
    bot_ids2 = {b["id"] for b in r2.json().get("bots", [])}
    assert bg["id"] in bot_ids2
    assert bh["id"] not in bot_ids2


def test_admin_bots_filter_by_game(tmp_path):
    app = _app(tmp_path)
    store, h, _, _, bh, bg = _setup(app)
    client = TestClient(app)
    r = client.get("/api/admin/bots?game_id=gomoku", headers=h)
    gids = {b["game_id"] for b in r.json()["bots"]}
    assert gids == {"gomoku"}, f"admin bots 应只返回 gomoku，实际 {gids}"


def test_admin_contests_filter_by_game(tmp_path):
    app = _app(tmp_path)
    store, h, ch, cg, _, _ = _setup(app)
    client = TestClient(app)
    r = client.get("/api/admin/contests?game_id=holdem", headers=h)
    ids = {c["id"] for c in r.json()["contests"]}
    assert ch in ids
    assert cg not in ids, "gomoku 赛事不应出现在 holdem 过滤结果"
