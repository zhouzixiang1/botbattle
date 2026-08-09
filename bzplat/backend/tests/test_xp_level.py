"""经验/等级系统测试（PR-9）。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    XP_MATCH_PARTICIPATE,
    XP_MATCH_WIN,
    XP_CONTEST_PARTICIPATE,
    XP_COMMENT,
    XP_FOLLOWED,
    xp_for_level,
    level_for_xp,
)


def test_xp_level_curves():
    assert xp_for_level(0) == 0
    assert xp_for_level(1) == 100
    assert xp_for_level(2) == 300
    assert xp_for_level(3) == 600
    assert level_for_xp(0) == 0
    assert level_for_xp(99) == 0
    assert level_for_xp(100) == 1
    assert level_for_xp(299) == 1
    assert level_for_xp(300) == 2
    assert level_for_xp(600) == 3


def test_award_xp_updates_level_and_active(tmp_path):
    s = Store(str(tmp_path / "x.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    assert u["xp"] == 0 and u["level"] == 0
    # 加 250 xp → level 1（需 100）
    u2 = s.award_xp(u["id"], 250)
    assert u2["xp"] == 250
    assert u2["level"] == 1
    assert u2["last_active_at"] is not None
    # 再加 100 → 350 → level 2（需 300）
    u3 = s.award_xp(u["id"], 100)
    assert u3["xp"] == 350
    assert u3["level"] == 2
    s.close()


def test_award_xp_nonexistent_user(tmp_path):
    s = Store(str(tmp_path / "x.db"))
    assert s.award_xp(99999, 100) is None
    s.close()


def _app(tmp_path):
    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u1 = store.create_user("alice", "a@ex.com", hash_password("pw123456"))
    store.update_user(u1["id"], email_verified=1)
    _, t1 = app.state.auth.authenticate("alice", "pw123456")
    c = TestClient(app)
    return c, store, u1["id"], t1


def test_levels_info_endpoint(tmp_path):
    c, store, uid, t1 = _app(tmp_path)
    r = c.get("/api/levels/info")
    assert r.status_code == 200
    d = r.json()
    assert "thresholds" in d and len(d["thresholds"]) == 11
    # 全部 XP 键应从 schema.py 常量派生（审计：原硬编码 10/15/50/2/3 会 drift）
    assert d["xp_match_participate"] == XP_MATCH_PARTICIPATE
    assert d["xp_match_win"] == XP_MATCH_WIN
    assert d["xp_contest_participate"] == XP_CONTEST_PARTICIPATE
    assert d["xp_comment"] == XP_COMMENT
    assert d["xp_followed"] == XP_FOLLOWED


def test_me_includes_xp_level(tmp_path):
    c, store, uid, t1 = _app(tmp_path)
    store.award_xp(uid, 150)
    r = c.get("/api/auth/me", headers={"Authorization": f"Bearer {t1}"})
    u = r.json()["user"]
    assert u["xp"] == 150 and u["level"] == 1


def test_user_profile_includes_xp_level(tmp_path):
    c, store, uid, t1 = _app(tmp_path)
    store.award_xp(uid, 350)
    r = c.get("/api/users/alice/profile")
    p = r.json()["profile"]
    assert p["xp"] == 350 and p["level"] == 2


def test_comment_awards_xp(tmp_path):
    c, store, uid, t1 = _app(tmp_path)
    bot = store.create_bot(uid, "xp-comment-target", game_id="holdem")
    h1 = {"Authorization": f"Bearer {t1}"}
    response = c.post(
        "/api/comments",
        json={"target_type": "bot", "target_id": str(bot["id"]), "body": "hi"},
        headers=h1,
    )
    assert response.status_code == 200
    u = c.get("/api/auth/me", headers=h1).json()["user"]
    assert u["xp"] == XP_COMMENT
