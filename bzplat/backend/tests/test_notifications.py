"""通知系统测试（PR-3）：store 方法 + NotificationManager + 端点 + 对局完成触发。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.notifications import NotificationManager
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "n.db"))


def test_add_and_list_notifications(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("alice", "a@ex.com", "x")
    s.add_notification(u["id"], type="match_done", title="对局完成", body="你赢了", link="/match/m1")
    s.add_notification(u["id"], type="followed", title="被关注", body="bob 关注了你")
    all_n = s.list_notifications(u["id"])
    assert len(all_n) == 2
    # 倒序（最新在前）
    assert all_n[0]["type"] == "followed"
    unread = s.list_notifications(u["id"], unread_only=True)
    assert len(unread) == 2
    assert s.unread_notification_count(u["id"]) == 2


def test_mark_read_and_count(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("alice", "a@ex.com", "x")
    n1 = s.add_notification(u["id"], title="a")
    n2 = s.add_notification(u["id"], title="b")
    assert s.mark_notification_read(n1["id"], u["id"]) is True
    assert s.unread_notification_count(u["id"]) == 1
    # 不能读别人的
    u2 = s.create_user("bob", "b@ex.com", "x")
    assert s.mark_notification_read(n2["id"], u2["id"]) is False
    n = s.mark_all_notifications_read(u["id"])
    assert n == 1
    assert s.unread_notification_count(u["id"]) == 0
    s.close()


def test_notification_prefs_default_and_update(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("alice", "a@ex.com", "x")
    p = s.get_notification_prefs(u["id"])
    assert p["email_match_done"] == 0
    p2 = s.update_notification_prefs(u["id"], email_match_done=True, email_followed=1)
    assert p2["email_match_done"] == 1
    assert p2["email_followed"] == 1
    assert p2["email_contest"] == 0  # 未改
    s.close()


def test_notification_manager_notify_writes_and_skips_unknown_user(tmp_path):
    s = _store(tmp_path)
    nm = NotificationManager(s, mailer=None)
    u = s.create_user("alice", "a@ex.com", "x")
    n = nm.notify(u["id"], type="match_done", title="t", body="b", link="/match/m1")
    assert n is not None and n["title"] == "t"
    assert s.unread_notification_count(u["id"]) == 1
    # 不存在的用户返回 None
    assert nm.notify(99999, title="x") is None
    s.close()


def test_notification_manager_notify_both_owners_dedup(tmp_path):
    s = _store(tmp_path)
    u1 = s.create_user("alice", "a@ex.com", "x")
    u2 = s.create_user("bob", "b@ex.com", "x")
    b1 = s.create_bot(u1["id"], "botA", binary_path="/tmp", format="elf", is_public=1, game_id="holdem")
    b2 = s.create_bot(u2["id"], "botB", binary_path="/tmp", format="elf", is_public=1, game_id="holdem")
    nm = NotificationManager(s, mailer=None)
    nm.notify_both_owners(b1["id"], b2["id"], type="match_done", title="完成")
    assert s.unread_notification_count(u1["id"]) == 1
    assert s.unread_notification_count(u2["id"]) == 1
    # 同 owner 两个 bot 去重：只通知一次
    b3 = s.create_bot(u1["id"], "botC", binary_path="/tmp", format="elf", is_public=1, game_id="holdem")
    nm.notify_both_owners(b1["id"], b3["id"], type="match_done", title="自博弈")
    assert s.unread_notification_count(u1["id"]) == 2  # 第二条
    s.close()


# ── HTTP 端点 ────────────────────────────────────────────
def _app(tmp_path):
    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u = store.create_user("alice", "a@ex.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    _, token = app.state.auth.authenticate("alice", "pw123456")
    c = TestClient(app)
    c.headers["Authorization"] = f"Bearer {token}"
    return c, store, u["id"], token


def test_notification_endpoints(tmp_path):
    c, store, uid, token = _app(tmp_path)
    store.add_notification(uid, type="match_done", title="t1", link="/match/m1")
    store.add_notification(uid, type="followed", title="t2")

    # 列表
    r = c.get("/api/notifications")
    assert r.status_code == 200
    d = r.json()
    assert len(d["notifications"]) == 2
    assert d["unread_count"] == 2

    # unread-count
    r = c.get("/api/notifications/unread-count")
    assert r.json()["count"] == 2

    # unread_only 过滤
    nid = d["notifications"][0]["id"]
    c.post("/api/notifications/read", json={"id": nid})
    r = c.get("/api/notifications?unread_only=true")
    assert len(r.json()["notifications"]) == 1
    assert c.get("/api/notifications/unread-count").json()["count"] == 1

    # read-all
    c.post("/api/notifications/read-all")
    assert c.get("/api/notifications/unread-count").json()["count"] == 0


def test_notification_prefs_endpoints(tmp_path):
    c, store, uid, token = _app(tmp_path)
    r = c.get("/api/notification-prefs")
    assert r.status_code == 200
    assert r.json()["prefs"]["email_match_done"] == 0
    r = c.put("/api/notification-prefs", json={"email_match_done": True})
    assert r.json()["prefs"]["email_match_done"] == 1


def test_notification_endpoints_require_auth(tmp_path):
    c, store, uid, token = _app(tmp_path)
    r = TestClient(c.app).get("/api/notifications")
    assert r.status_code == 401
