"""用户主页 + 全局搜索相关测试（PR-2）。

覆盖：user_profile 聚合、aggregate_owner_stats、search_bots/search_matches、
profile/avatar 端点、migration 幂等（bio/avatar 列）。
"""
from __future__ import annotations

import io

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "up.db"))


def test_user_profile_aggregates_stats_and_bio(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("alice", "a@ex.com", "x")
    s.update_user(u["id"], bio="hello", avatar="1.png", display_name="Alice")
    b1 = s.create_bot(u["id"], "botA", binary_path="/tmp/x", format="elf", game_id="holdem")
    b2 = s.create_bot(u["id"], "botB", binary_path="/tmp/x", format="elf", game_id="gomoku")
    s.ensure_rating(b1["id"])
    s.ensure_rating(b2["id"])
    s.update_rating_row(b1["id"], wins=3, losses=1, matches_played=4)
    s.update_rating_row(b2["id"], wins=2, losses=2, draws=1, matches_played=5)
    p = s.user_profile("alice")
    assert p is not None
    assert p["display_name"] == "Alice"
    assert p["bio"] == "hello"
    assert p["avatar"] == "1.png"
    assert p["stats"]["wins"] == 5  # 3 + 2
    assert p["stats"]["matches_played"] == 9  # 4 + 5
    assert p["bot_count"] == 2
    # 不含敏感字段
    assert "password_hash" not in p
    assert "email" not in p
    s.close()


def test_user_profile_nonexistent_returns_none(tmp_path):
    s = _store(tmp_path)
    assert s.user_profile("nobody") is None
    s.close()


def test_search_bots_by_name_and_display(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("alice", "a@ex.com", "x")
    s.create_bot(u["id"], "alphaBot", binary_path="/tmp/x", format="elf", game_id="holdem", display_name="阿尔法")
    s.create_bot(u["id"], "betaBot", binary_path="/tmp/x", format="elf", game_id="gomoku")
    s.create_bot(u["id"], "gammaBot", binary_path="/tmp/x", format="elf", game_id="holdem")  # is_public=0 已下线——不再隐藏
    # 按 name 搜
    r = s.search_bots("alpha")
    assert len(r) == 1 and r[0]["name"] == "alphaBot"
    # 按 display_name 搜（中文）
    r2 = s.search_bots("阿尔法")
    assert len(r2) == 1 and r2[0]["name"] == "alphaBot"
    # 空 q 返回全部（私有 bot 功能已下线，全部可见）
    r3 = s.search_bots("")
    assert len(r3) == 3  # gammaBot 不再隐藏
    # 按 game 过滤
    r4 = s.search_bots("", game_id="gomoku")
    assert len(r4) == 1 and r4[0]["name"] == "betaBot"
    s.close()


def test_search_matches_by_bot_name(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("alice", "a@ex.com", "x")
    b1 = s.create_bot(u["id"], "findme", binary_path="/tmp/x", format="elf", game_id="holdem")
    b2 = s.create_bot(u["id"], "otherbot", binary_path="/tmp/x", format="elf", game_id="holdem")
    s.create_match("m1", bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=u["id"])
    s.update_match("m1", status="completed")
    s.create_match("m2", bot_a_id=b2["id"], bot_b_id=b1["id"], owner_id=u["id"])  # 默认 pending
    # 搜 "findme"：只命中 completed 的对局（m1）
    r = s.search_matches("findme")
    assert len(r) == 1
    assert r[0]["id"] == "m1"
    s.close()


def test_search_matches_by_match_id(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("idowner", "idowner@ex.com", "x")
    b1 = s.create_bot(u["id"], "idbot_a", binary_path="/tmp/a", format="elf")
    b2 = s.create_bot(u["id"], "idbot_b", binary_path="/tmp/b", format="elf")
    match_id = "20260809-searchable-id"
    s.create_match(match_id, bot_a_id=b1["id"], bot_b_id=b2["id"])
    s.update_match(match_id, status="completed")

    rows = s.search_matches("searchable-id")

    assert [row["id"] for row in rows] == [match_id]
    s.close()


def test_migration_bio_avatar_columns_idempotent(tmp_path):
    db = str(tmp_path / "mig.db")
    Store(db).close()
    s = Store(db)  # 第二次打开触发迁移
    cols = [r[1] for r in s._conn.execute("PRAGMA table_info(users)")]
    assert "bio" in cols and "avatar" in cols
    # 第三次打开仍不报错
    s.close()
    s2 = Store(db)
    cols2 = [r[1] for r in s2._conn.execute("PRAGMA table_info(users)")]
    assert "bio" in cols2 and "avatar" in cols2
    s2.close()


# ── HTTP 端点 ────────────────────────────────────────────
def _app(tmp_path) -> tuple[TestClient, dict, int]:
    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u = store.create_user("alice", "a@ex.com", hash_password("pw123456"), display_name="Alice")
    store.update_user(u["id"], email_verified=1, bio="hi")
    b = store.create_bot(u["id"], "botA", binary_path="/tmp/x", format="elf", game_id="holdem")
    store.ensure_rating(b["id"])
    store.update_rating_row(b["id"], wins=1, matches_played=1)
    _, token = app.state.auth.authenticate("alice", "pw123456")
    c = TestClient(app)
    return c, {"token": token, "uid": u["id"]}, b["id"]


def test_user_profile_endpoint(tmp_path):
    c, ctx, bid = _app(tmp_path)
    r = c.get("/api/users/alice/profile")
    assert r.status_code == 200
    p = r.json()["profile"]
    assert p["display_name"] == "Alice"
    assert p["bio"] == "hi"
    assert p["stats"]["wins"] == 1


def test_user_profile_404(tmp_path):
    c, ctx, bid = _app(tmp_path)
    r = c.get("/api/users/nobody/profile")
    assert r.status_code == 404


def test_user_bots_endpoint(tmp_path):
    c, ctx, bid = _app(tmp_path)
    r = c.get("/api/users/alice/bots")
    assert r.status_code == 200
    assert len(r.json()["bots"]) == 1


def test_search_endpoints(tmp_path):
    c, ctx, bid = _app(tmp_path)
    r = c.get("/api/search?q=ali&type=users")
    assert r.status_code == 200 and len(r.json()["users"]) >= 1
    r = c.get("/api/search?q=bot&type=bots")
    assert r.status_code == 200 and len(r.json()["bots"]) >= 1
    # 默认 type=users
    r = c.get("/api/search?q=a")
    assert r.status_code == 200 and "users" in r.json()


def test_update_profile_endpoint(tmp_path):
    c, ctx, bid = _app(tmp_path)
    r = c.put(
        "/api/auth/profile",
        json={"display_name": "Alice2", "bio": "updated bio"},
        headers={"Authorization": f"Bearer {ctx['token']}"},
    )
    assert r.status_code == 200
    u = r.json()["user"]
    assert u["display_name"] == "Alice2"
    assert u["bio"] == "updated bio"


def test_update_profile_rejects_angle_brackets_in_display_name(tmp_path):
    """显示名禁止 < >，避免脏数据/伪 HTML 污染侧栏与主页（审计 U10）。"""
    c, ctx, bid = _app(tmp_path)
    r = c.put(
        "/api/auth/profile",
        json={"display_name": "测1_<script>alert(1)</script>"},
        headers={"Authorization": f"Bearer {ctx['token']}"},
    )
    assert r.status_code == 422, r.text


def test_update_profile_requires_auth(tmp_path):
    c, ctx, bid = _app(tmp_path)
    r = c.put("/api/auth/profile", json={"bio": "x"})
    assert r.status_code == 401


def test_avatar_upload_and_serve(tmp_path):
    c, ctx, bid = _app(tmp_path)
    # 1x1 PNG
    png = bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000d49444154789c63f8cf00000003000100184bbb6e0000000049454e44ae426082"
    )
    r = c.post(
        "/api/auth/avatar",
        files={"file": ("a.png", io.BytesIO(png), "image/png")},
        headers={"Authorization": f"Bearer {ctx['token']}"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["avatar"].endswith(".png")
    # 头像可通过 /avatars/<file> 访问
    av = r.json()["avatar"]
    r2 = c.get(f"/avatars/{av}")
    assert r2.status_code == 200


def test_avatar_rejects_bad_type(tmp_path):
    c, ctx, bid = _app(tmp_path)
    r = c.post(
        "/api/auth/avatar",
        files={"file": ("a.txt", io.BytesIO(b"notimage"), "text/plain")},
        headers={"Authorization": f"Bearer {ctx['token']}"},
    )
    assert r.status_code == 400
