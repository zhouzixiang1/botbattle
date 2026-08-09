"""评论 + 点赞 + 浏览测试（PR-7）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "cl.db"))


def test_comment_add_list_delete(tmp_path):
    s = _store(tmp_path)
    u1 = s.create_user("alice", "a@ex.com", "x")
    u2 = s.create_user("bob", "b@ex.com", "x")
    bot = s.create_bot(
        u1["id"], "botA", binary_path="/tmp/a", format="elf", game_id="holdem"
    )
    target_id = str(bot["id"])
    c1 = s.add_comment(u1["id"], "bot", target_id, "nice")
    c2 = s.add_comment(u2["id"], "bot", target_id, "gg")
    assert c1["username"] == "alice"
    lst = s.list_comments("bot", target_id)
    assert len(lst) == 2
    assert s.comment_count("bot", target_id) == 2
    # 作者可删
    assert s.delete_comment(c1["id"], u1["id"]) is True
    # 非作者不可删
    assert s.delete_comment(c2["id"], u1["id"]) is False
    assert s.comment_count("bot", target_id) == 1
    s.close()


def test_delete_comment_admin_ignores_author(tmp_path):
    """admin 强删任意评论（无视作者）；评论不存在返回 False。"""
    s = _store(tmp_path)
    u1 = s.create_user("alice", "a@ex.com", "x")
    u2 = s.create_user("bob", "b@ex.com", "x")
    bot = s.create_bot(
        u1["id"], "botA", binary_path="/tmp/a", format="elf", game_id="holdem"
    )
    c = s.add_comment(u2["id"], "bot", str(bot["id"]), "bob的评论")
    # admin（u1，非作者）强删 → 成功
    assert s.delete_comment_admin(c["id"]) is True
    assert s.comment_count("bot", str(bot["id"])) == 0
    # 不存在的评论 → False（不崩）
    assert s.delete_comment_admin(99999) is False
    s.close()


def test_like_unlike_count_and_match_counter(tmp_path):
    s = _store(tmp_path)
    u1 = s.create_user("alice", "a@ex.com", "x")
    u2 = s.create_user("bob", "b@ex.com", "x")
    b1 = s.create_bot(u1["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = s.create_bot(u2["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    mid = "m1"
    s.create_match(mid, bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=u1["id"])
    assert s.like(u1["id"], "match", mid) is True
    assert s.like(u1["id"], "match", mid) is False  # 重复幂等
    assert s.like(u2["id"], "match", mid) is True
    assert s.like_count("match", mid) == 2
    assert s.is_liked(u1["id"], "match", mid) is True
    # match 的 likes_count 同步
    m = s.get_match(mid)
    assert m["likes_count"] == 2
    assert s.unlike(u1["id"], "match", mid) is True
    assert s.like_count("match", mid) == 1
    m = s.get_match(mid)
    assert m["likes_count"] == 1
    s.close()


def test_incr_view(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("alice", "a@ex.com", "x")
    b1 = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = s.create_bot(u["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    mid = "m1"
    s.create_match(mid, bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=u["id"])
    s.incr_match_view(mid)
    s.incr_match_view(mid)
    assert s.get_match(mid)["views_count"] == 2
    s.close()


# ── HTTP 端点 ────────────────────────────────────────────
def _app(tmp_path):
    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u1 = store.create_user("alice", "a@ex.com", hash_password("pw123456"))
    store.update_user(u1["id"], email_verified=1)
    u2 = store.create_user("bob", "b@ex.com", hash_password("pw123456"))
    store.update_user(u2["id"], email_verified=1)
    b1 = store.create_bot(u1["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = store.create_bot(u2["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    mid = "20260101-test"
    store.create_match(mid, bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=u1["id"])
    store.update_match(mid, status="completed")
    _, t1 = app.state.auth.authenticate("alice", "pw123456")
    _, t2 = app.state.auth.authenticate("bob", "pw123456")
    c = TestClient(app)
    return c, mid, b1["id"], t1, t2


def test_comment_endpoints(tmp_path):
    c, mid, bid, t1, t2 = _app(tmp_path)
    h1 = {"Authorization": f"Bearer {t1}"}
    h2 = {"Authorization": f"Bearer {t2}"}
    # alice 评论
    r = c.post("/api/comments", json={"target_type": "match", "target_id": mid, "body": "gg"}, headers=h1)
    assert r.status_code == 200 and r.json()["comment"]["body"] == "gg"
    own_notifications = c.get(
        "/api/notifications?unread_only=true", headers=h1
    ).json()["notifications"]
    assert not any(n["type"] == "comment" for n in own_notifications)
    # 列表（公开）
    r = c.get(f"/api/comments?target_type=match&target_id={mid}")
    assert len(r.json()["comments"]) == 1
    # bob 评论触发 alice 通知（alice 是 botA owner）
    c.post("/api/comments", json={"target_type": "match", "target_id": mid, "body": "nice"}, headers=h2)
    r = c.get("/api/notifications?unread_only=true", headers=h1)
    assert any(n["type"] == "comment" for n in r.json()["notifications"])
    # bob 不能删 alice 的评论（列表倒序：最新在前，alice 的评论是最后一条）
    cids = c.get(f"/api/comments?target_type=match&target_id={mid}").json()["comments"]
    alice_cid = cids[-1]["id"]  # alice 的评论（最早，在列表末尾）
    r = c.delete(f"/api/comments/{alice_cid}", headers=h2)
    assert r.status_code == 403
    # alice 删自己的
    r = c.delete(f"/api/comments/{alice_cid}", headers=h1)
    assert r.status_code == 200


def test_delete_comment_nonexistent_returns_404(tmp_path):
    """删除不存在的评论统一返 404（对抗审计：原非 admin 返 403 误导，admin 返 404）。"""
    c, mid, bid, t1, t2 = _app(tmp_path)
    h1 = {"Authorization": f"Bearer {t1}"}
    # 非 admin 删不存在的评论 → 404（不是 403「无权删除」）
    r = c.delete("/api/comments/99999", headers=h1)
    assert r.status_code == 404, f"不存在的评论应 404，实际 {r.status_code}"


def test_like_endpoints(tmp_path):
    c, mid, bid, t1, t2 = _app(tmp_path)
    h1 = {"Authorization": f"Bearer {t1}"}
    r = c.post("/api/likes", json={"target_type": "match", "target_id": mid}, headers=h1)
    assert r.status_code == 200 and r.json()["liked"] is True
    r = c.get(f"/api/likes/status?target_type=match&target_id={mid}", headers=h1)
    assert r.json()["liked"] is True and r.json()["count"] == 1
    r = c.request("DELETE", "/api/likes", json={"target_type": "match", "target_id": mid}, headers=h1)
    assert r.json()["liked"] is False


def test_social_writes_reject_unknown_or_missing_targets(tmp_path):
    c, mid, bid, t1, t2 = _app(tmp_path)
    h1 = {"Authorization": f"Bearer {t1}"}
    before_xp = c.app.state.store.get_user_by_username("alice")["xp"]
    for payload in (
        {"target_type": "match", "target_id": "missing", "body": "orphan"},
        {"target_type": "bot", "target_id": "99999", "body": "orphan"},
    ):
        r = c.post("/api/comments", json=payload, headers=h1)
        assert r.status_code == 404
    r = c.post(
        "/api/comments",
        json={"target_type": "anything", "target_id": mid, "body": "bad"},
        headers=h1,
    )
    assert r.status_code == 422
    r = c.post(
        "/api/likes",
        json={"target_type": "comment", "target_id": "99999"},
        headers=h1,
    )
    assert r.status_code == 404
    assert c.request(
        "DELETE",
        "/api/likes",
        json={"target_type": "match", "target_id": "missing"},
        headers=h1,
    ).status_code == 404
    assert c.get(
        "/api/likes/status?target_type=bot&target_id=99999", headers=h1
    ).status_code == 404
    assert c.get(
        "/api/comments?target_type=match&target_id=missing"
    ).status_code == 404
    assert c.app.state.store.get_user_by_username("alice")["xp"] == before_xp


def test_target_deletion_cleans_comments_likes_and_cached_count(tmp_path):
    s = _store(tmp_path)
    u1 = s.create_user("alice", "a@ex.com", "x")
    u2 = s.create_user("bob", "b@ex.com", "x")
    b1 = s.create_bot(u1["id"], "botA", binary_path="/tmp/a", format="elf", game_id="holdem")
    b2 = s.create_bot(u2["id"], "botB", binary_path="/tmp/b", format="elf", game_id="holdem")
    mid = "social-cleanup"
    s.create_match(mid, b1["id"], b2["id"], game_id="holdem")
    comment = s.add_comment(u1["id"], "match", mid, "gg")
    assert s.like(u2["id"], "comment", str(comment["id"])) is True
    assert s.like(u2["id"], "match", mid) is True
    assert s.get_match(mid)["likes_count"] == 1

    assert s.delete_match(mid) is True
    assert s.comment_count("match", mid) == 0
    assert s.like_count("match", mid) == 0
    assert s.like_count("comment", str(comment["id"])) == 0

    bot_comment = s.add_comment(u2["id"], "bot", str(b1["id"]), "bot comment")
    s.like(u2["id"], "bot", str(b1["id"]))
    s.like(u1["id"], "comment", str(bot_comment["id"]))
    assert s.delete_bot(b1["id"]) is True
    assert s.comment_count("bot", str(b1["id"])) == 0
    assert s.like_count("bot", str(b1["id"])) == 0
    assert s.like_count("comment", str(bot_comment["id"])) == 0
    s.close()


def test_comment_deletion_cleans_comment_likes(tmp_path):
    s = _store(tmp_path)
    u1 = s.create_user("alice", "a@ex.com", "x")
    u2 = s.create_user("bob", "b@ex.com", "x")
    bot = s.create_bot(u1["id"], "botA", binary_path="/tmp/a", format="elf", game_id="holdem")
    comment = s.add_comment(u1["id"], "bot", str(bot["id"]), "hello")
    s.like(u2["id"], "comment", str(comment["id"]))
    assert s.delete_comment(comment["id"], u1["id"]) is True
    assert s.like_count("comment", str(comment["id"])) == 0
    s.close()


def test_user_deletion_removes_authored_like_and_updates_match_cache(tmp_path):
    s = _store(tmp_path)
    owner = s.create_user("owner", "owner@ex.com", "x")
    liker = s.create_user("liker", "liker@ex.com", "x")
    b1 = s.create_bot(owner["id"], "botA", game_id="holdem")
    b2 = s.create_bot(owner["id"], "botB", game_id="holdem")
    s.create_match("liked-match", b1["id"], b2["id"], game_id="holdem")
    assert s.like(liker["id"], "match", "liked-match") is True
    assert s.get_match("liked-match")["likes_count"] == 1

    assert s.delete_user(liker["id"]) is True
    assert s.like_count("match", "liked-match") == 0
    assert s.get_match("liked-match")["likes_count"] == 0
    s.close()


def test_migration_repairs_polymorphic_orphans_and_match_like_cache(tmp_path):
    import sqlite3

    db = str(tmp_path / "social-migration.db")
    s = Store(db)
    u = s.create_user("alice", "a@ex.com", "x")
    bot = s.create_bot(u["id"], "botA", binary_path="/tmp/a", format="elf", game_id="holdem")
    s.create_match("kept", bot["id"], bot["id"], game_id="holdem")
    s.close()

    conn = sqlite3.connect(db)
    conn.execute(
        "INSERT INTO likes(user_id,target_type,target_id,created_at) VALUES(?,?,?,?)",
        (u["id"], "match", "kept", "2026"),
    )
    conn.execute(
        "INSERT INTO comments(target_type,target_id,user_id,body,created_at) VALUES(?,?,?,?,?)",
        ("bot", "99999", u["id"], "orphan", "2026"),
    )
    orphan_comment_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    conn.execute(
        "INSERT INTO likes(user_id,target_type,target_id,created_at) VALUES(?,?,?,?)",
        (u["id"], "comment", str(orphan_comment_id), "2026"),
    )
    conn.execute(
        "INSERT INTO comments(target_type,target_id,user_id,body,created_at) "
        "VALUES(?,?,?,?,?)",
        ("match", "kept", 99998, "missing author", "2026"),
    )
    conn.execute(
        "INSERT INTO likes(user_id,target_type,target_id,created_at) VALUES(?,?,?,?)",
        (99998, "match", "kept", "2026"),
    )
    conn.execute(
        "INSERT INTO follows(follower_id,followee_id,created_at) VALUES(?,?,?)",
        (99998, u["id"], "2026"),
    )
    conn.execute(
        "INSERT INTO favorites(user_id,bot_id,created_at) VALUES(?,?,?)",
        (99998, bot["id"], "2026"),
    )
    conn.execute("UPDATE matches_holdem SET likes_count=99 WHERE id='kept'")
    conn.commit()
    conn.close()

    repaired = Store(db)
    assert repaired.get_match("kept")["likes_count"] == 1
    assert repaired.comment_count("bot", "99999") == 0
    assert repaired.like_count("comment", str(orphan_comment_id)) == 0
    with repaired._tx() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM comments WHERE user_id=99998"
        ).fetchone()[0] == 0
        assert c.execute(
            "SELECT COUNT(*) FROM likes WHERE user_id=99998"
        ).fetchone()[0] == 0
        assert c.execute(
            "SELECT COUNT(*) FROM follows WHERE follower_id=99998"
        ).fetchone()[0] == 0
        assert c.execute(
            "SELECT COUNT(*) FROM favorites WHERE user_id=99998"
        ).fetchone()[0] == 0
    repaired.close()


def test_view_and_liked_top_endpoints(tmp_path):
    c, mid, bid, t1, t2 = _app(tmp_path)
    h1 = {"Authorization": f"Bearer {t1}"}
    # view
    r = c.post(f"/api/matches/{mid}/view")
    assert r.status_code == 200
    # like + liked-top
    c.post("/api/likes", json={"target_type": "match", "target_id": mid}, headers=h1)
    r = c.get("/api/matches/liked-top?limit=5")
    assert r.status_code == 200
    assert len(r.json()["matches"]) == 1
    assert r.json()["matches"][0]["likes_count"] == 1
