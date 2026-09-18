"""admin 写端点审计覆盖：patch_site / patch_user / patch_bot / revoke_sessions。

其余 admin 写已有 audit_log（admin_delete_user/admin_set_role/admin_delete_bot 等）；
这四个此前无审计，本文件钉住「成功与失败路径都留痕」。
"""
from __future__ import annotations

import bzplat.backend.api_routes as api_routes
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app


def _setup(tmp_path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "audit.db"))
    store = app.state.store
    admin = store.create_user(
        "audit-admin", "audit-admin@example.com",
        hash_password("pw123456"), role="admin",
    )
    store.update_user(admin["id"], email_verified=1)
    victim = store.create_user(
        "audit-victim", "audit-victim@example.com", hash_password("pw123456")
    )
    _, token = app.state.auth.authenticate("audit-admin", "pw123456")
    headers = {"Authorization": f"Bearer {token}"}

    calls: list[dict] = []
    orig = api_routes.audit_log

    def capture(request, action, **kwargs):
        calls.append({"action": action, "result": "ok", **kwargs})
        # 不落盘：只验证调用契约，不依赖日志文件系统状态。
        return orig(request, action, **kwargs)

    monkeypatch.setattr(api_routes, "audit_log", capture)
    from fastapi.testclient import TestClient

    client = TestClient(app)
    return store, client, headers, admin, victim, calls


def _actions(calls):
    return [c["action"] for c in calls]


def test_admin_patch_user_audits_ok_and_fail(tmp_path, monkeypatch):
    _, client, headers, _, victim, calls = _setup(tmp_path, monkeypatch)

    ok = client.patch(
        f"/api/admin/users/{victim['id']}",
        headers=headers, json={"is_active": False},
    )
    assert ok.status_code == 200, ok.text
    hit = [c for c in calls if c["action"] == "admin_patch_user"]
    assert len(hit) == 1
    assert hit[0]["result"] == "ok"
    assert hit[0]["target"] == victim["id"]
    assert hit[0]["user"] == "audit-admin"
    assert hit[0]["detail"] == "is_active=0"

    bad = client.patch(
        f"/api/admin/users/{victim['id']}",
        headers=headers, json={"role": "superuser"},
    )
    assert bad.status_code == 400
    fails = [c for c in calls if c["action"] == "admin_patch_user" and c["result"] == "fail"]
    assert [f["detail"] for f in fails] == ["bad_role"]

    missing = client.patch(
        "/api/admin/users/999999", headers=headers, json={"is_active": True},
    )
    assert missing.status_code == 404
    fails = [c for c in calls if c["action"] == "admin_patch_user" and c["result"] == "fail"]
    assert [f["detail"] for f in fails] == ["bad_role", "not_found"]


def test_admin_revoke_sessions_audits(tmp_path, monkeypatch):
    store, client, headers, _, victim, calls = _setup(tmp_path, monkeypatch)
    store.add_session("tk-audit", victim["id"], "2099-01-01T00:00:00")

    ok = client.delete(
        f"/api/admin/users/{victim['id']}/sessions", headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == {"ok": True, "revoked": 1}
    assert "admin_revoke_sessions" in _actions(calls)
    hit = next(c for c in calls if c["action"] == "admin_revoke_sessions")
    assert hit["detail"] == "revoked=1"
    assert hit["target"] == victim["id"]


def test_admin_patch_bot_audits_ok_and_fail(tmp_path, monkeypatch):
    store, client, headers, admin, _, calls = _setup(tmp_path, monkeypatch)
    bot = store.create_bot(
        admin["id"], "auditbot", binary_path="/tmp/x",
        format="elf", game_id="holdem",
    )

    ok = client.patch(
        f"/api/admin/bots/{bot['id']}",
        headers=headers, json={"is_active": False},
    )
    assert ok.status_code == 200, ok.text
    hit = [c for c in calls if c["action"] == "admin_patch_bot"]
    assert len(hit) == 1
    assert hit[0]["detail"] == "is_active"

    missing = client.patch(
        "/api/admin/bots/999999", headers=headers, json={"is_active": True},
    )
    assert missing.status_code == 404
    fails = [c for c in calls if c["action"] == "admin_patch_bot" and c["result"] == "fail"]
    assert [f["detail"] for f in fails] == ["not_found"]


def test_admin_patch_site_audits_changed_keys_only(tmp_path, monkeypatch):
    _, client, headers, _, _, calls = _setup(tmp_path, monkeypatch)

    ok = client.patch(
        "/api/admin/settings/site",
        headers=headers,
        json={"announcement": "新公告", "about": "多游戏 Bot 线上对战平台"},
    )
    assert ok.status_code == 200, ok.text
    assert "admin_patch_site" in _actions(calls)
    hit = next(c for c in calls if c["action"] == "admin_patch_site")
    # 只记键名，公告正文绝不进审计日志。
    assert set(hit["detail"].split(",")) == {"announcement", "about"}
    assert "新公告" not in hit["detail"]

    noop = client.patch(
        "/api/admin/settings/site", headers=headers, json={},
    )
    assert noop.status_code == 200
    hits = [c for c in calls if c["action"] == "admin_patch_site"]
    assert hits[-1]["detail"] == "noop"
