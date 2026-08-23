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

    # Owner inventory must retain inactive Bots so the My Bots page can offer
    # reactivation.  Public discovery remains active-only.
    mine = c.get("/api/bots/mine?page=1&per_page=20", headers=h1)
    assert mine.status_code == 200
    listed = next(bot for bot in mine.json()["bots"] if bot["id"] == bid)
    assert listed["is_active"] in (0, False)
    assert mine.json()["total"] == 1

    public = c.get("/api/bots/public?game_id=holdem")
    assert public.status_code == 200
    assert bid not in {bot["id"] for bot in public.json()["bots"]}


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
    assert r.json() == {"ok": True, "changed": True}

    # Owner tombstone hides the Bot from inventory while preserving its public
    # historical identity without exposing the exact deletion timestamp.
    mine = c.get("/api/bots/mine?page=1&per_page=20", headers=h1)
    assert mine.status_code == 200
    assert mine.json()["bots"] == []
    assert mine.json()["total"] == 0
    b = c.get(f"/api/bots/{bid}", headers=h1).json()["bot"]
    assert b["is_active"] in (0, False)
    assert b["is_ranked"] in (0, False)
    assert b["is_deleted"] is True
    assert b["runnable"] is False
    assert b["unsupported_reason"] == "Bot 已删除"
    assert "owner_deleted_at" not in b
    assert "is_public" not in b

    repeated = c.delete(f"/api/bots/{bid}", headers=h1)
    assert repeated.status_code == 200
    assert repeated.json() == {"ok": True, "changed": False}

    for method, path, payload in (
        ("patch", f"/api/bots/{bid}", {"display_name": "revive"}),
        ("post", f"/api/bots/{bid}/active?active=true", None),
        ("put", f"/api/bots/{bid}/ranking", None),
    ):
        blocked = getattr(c, method)(path, json=payload, headers=h1)
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "bot_deleted"


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
