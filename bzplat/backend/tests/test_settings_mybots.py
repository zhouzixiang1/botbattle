"""个人设置 + MyBots 管理增强测试（PR-8）：owner PATCH/DELETE bot。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app


def _app(tmp_path):
    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u1 = store.create_user("alice", "a@ex.com", hash_password("pw123456"))
    store.update_user(u1["id"], email_verified=1)
    u2 = store.create_user("bob", "b@ex.com", hash_password("pw123456"))
    store.update_user(u2["id"], email_verified=1)
    b = store.create_bot(u1["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    _, t1 = app.state.auth.authenticate("alice", "pw123456")
    _, t2 = app.state.auth.authenticate("bob", "pw123456")
    c = TestClient(app)
    return c, b["id"], t1, t2


def test_owner_patch_bot(tmp_path):
    c, bid, t1, t2 = _app(tmp_path)
    h1 = {"Authorization": f"Bearer {t1}"}
    r = c.patch(f"/api/bots/{bid}", json={"display_name": "Renamed", "description": "desc", "is_active": False}, headers=h1)
    assert r.status_code == 200
    b = r.json()["bot"]
    assert b["display_name"] == "Renamed"
    assert b["description"] == "desc"
    assert b["is_active"] in (0, False)
    # is_public 字段已下线，响应不应再包含该键
    assert "is_public" not in b


def test_other_user_cannot_patch(tmp_path):
    c, bid, t1, t2 = _app(tmp_path)
    h2 = {"Authorization": f"Bearer {t2}"}
    r = c.patch(f"/api/bots/{bid}", json={"display_name": "hack"}, headers=h2)
    assert r.status_code == 403


def test_owner_delete_bot(tmp_path):
    c, bid, t1, t2 = _app(tmp_path)
    h1 = {"Authorization": f"Bearer {t1}"}
    r = c.delete(f"/api/bots/{bid}", headers=h1)
    assert r.status_code == 200
    # 软删：is_active=0（私有 bot 功能已下线，不再有 is_public 字段）
    b = c.get(f"/api/bots/{bid}").json()["bot"]
    assert b["is_active"] in (0, False)
    assert "is_public" not in b


def test_other_user_cannot_delete(tmp_path):
    c, bid, t1, t2 = _app(tmp_path)
    h2 = {"Authorization": f"Bearer {t2}"}
    r = c.delete(f"/api/bots/{bid}", headers=h2)
    assert r.status_code == 403


def test_patch_404(tmp_path):
    c, bid, t1, t2 = _app(tmp_path)
    h1 = {"Authorization": f"Bearer {t1}"}
    r = c.patch("/api/bots/99999", json={"display_name": "x"}, headers=h1)
    assert r.status_code == 404
