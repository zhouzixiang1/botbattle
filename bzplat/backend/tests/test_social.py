"""关注用户 + 收藏 Bot 测试（PR-4）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "soc.db"))


def test_follow_unfollow_and_counts(tmp_path):
    s = _store(tmp_path)
    u1 = s.create_user("alice", "a@ex.com", "x")
    u2 = s.create_user("bob", "b@ex.com", "x")
    assert s.follow(u1["id"], u2["id"]) is True
    assert s.follow(u1["id"], u2["id"]) is False  # 重复关注幂等
    assert s.is_following(u1["id"], u2["id"]) is True
    assert s.is_following(u2["id"], u1["id"]) is False
    assert s.follower_count(u2["id"]) == 1
    assert s.following_count(u1["id"]) == 1
    fl = s.list_followers(u2["id"])
    assert len(fl) == 1 and fl[0]["username"] == "alice"
    fg = s.list_following(u1["id"])
    assert len(fg) == 1 and fg[0]["username"] == "bob"
    assert s.unfollow(u1["id"], u2["id"]) is True
    assert s.follower_count(u2["id"]) == 0
    s.close()


def test_no_self_follow(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("alice", "a@ex.com", "x")
    assert s.follow(u["id"], u["id"]) is False
    assert s.follower_count(u["id"]) == 0
    s.close()


def test_favorite_unfavorite_and_count(tmp_path):
    s = _store(tmp_path)
    u1 = s.create_user("alice", "a@ex.com", "x")
    u2 = s.create_user("bob", "b@ex.com", "x")
    b = s.create_bot(u1["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    assert s.favorite(u2["id"], b["id"]) is True
    assert s.favorite(u2["id"], b["id"]) is False  # 重复收藏幂等
    assert s.is_favorite(u2["id"], b["id"]) is True
    assert s.favorite_count(b["id"]) == 1
    favs = s.list_favorites(u2["id"])
    assert len(favs) == 1 and favs[0]["name"] == "botA" and favs[0]["owner_name"] == "alice"
    assert s.unfavorite(u2["id"], b["id"]) is True
    assert s.favorite_count(b["id"]) == 0
    s.close()


# ── HTTP 端点 ────────────────────────────────────────────
def _app(tmp_path):
    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u1 = store.create_user("alice", "a@ex.com", hash_password("pw123456"))
    u2 = store.create_user("bob", "b@ex.com", hash_password("pw123456"))
    store.update_user(u1["id"], email_verified=1)
    store.update_user(u2["id"], email_verified=1)
    b = store.create_bot(u1["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    _, t1 = app.state.auth.authenticate("alice", "pw123456")
    _, t2 = app.state.auth.authenticate("bob", "pw123456")
    c = TestClient(app)
    return c, b["id"], u1["id"], u2["id"], t1, t2


def test_follow_endpoints(tmp_path):
    c, bid, u1, u2, t1, t2 = _app(tmp_path)
    h1 = {"Authorization": f"Bearer {t1}"}  # alice
    h2 = {"Authorization": f"Bearer {t2}"}  # bob
    # bob 关注 alice
    r = c.post(f"/api/users/{u1}/follow", headers=h2)
    assert r.status_code == 200 and r.json()["following"] is True
    # alice 视角看自己被 bob 关注：follow-status（alice 查自己 vs bob？查 alice 是否关注 bob = False）
    r = c.get(f"/api/users/{u2}/follow-status", headers=h1)
    assert r.json()["following"] is False
    # bob 视角：bob 关注 alice = True，alice 粉丝数 = 1
    r = c.get(f"/api/users/{u1}/follow-status", headers=h2)
    assert r.json()["following"] is True
    assert r.json()["follower_count"] == 1
    # followers 列表（alice 的粉丝 = [bob]）
    r = c.get(f"/api/users/{u1}/followers")
    assert len(r.json()["followers"]) == 1
    # alice 不能关注自己
    r = c.post(f"/api/users/{u1}/follow", headers=h1)
    assert r.status_code == 400
    # bob 取关 alice
    r = c.delete(f"/api/users/{u1}/follow", headers=h2)
    assert r.status_code == 200 and r.json()["following"] is False


def test_favorite_endpoints(tmp_path):
    c, bid, u1, u2, t1, t2 = _app(tmp_path)
    h2 = {"Authorization": f"Bearer {t2}"}
    r = c.post(f"/api/bots/{bid}/favorite", headers=h2)
    assert r.status_code == 200 and r.json()["favorited"] is True
    r = c.get(f"/api/bots/{bid}/favorite-status", headers=h2)
    assert r.json()["favorited"] is True and r.json()["favorite_count"] == 1
    r = c.get("/api/auth/me/favorites", headers=h2)
    assert len(r.json()["favorites"]) == 1
    r = c.delete(f"/api/bots/{bid}/favorite", headers=h2)
    assert r.json()["favorited"] is False


def test_follow_triggers_notification(tmp_path):
    c, bid, u1, u2, t1, t2 = _app(tmp_path)
    h2 = {"Authorization": f"Bearer {t2}"}
    # bob 关注 alice（u2 关注 u1）
    c.post(f"/api/users/{u1}/follow", headers=h2)
    # alice 应收到 followed 通知
    h1 = {"Authorization": f"Bearer {t1}"}
    r = c.get("/api/notifications?unread_only=true", headers=h1)
    notifs = r.json()["notifications"]
    assert any(n["type"] == "followed" for n in notifs)
