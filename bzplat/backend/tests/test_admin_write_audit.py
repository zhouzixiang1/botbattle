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
        # 不默认补 result=ok：调用方漏传时测试能发现（ok 路径要求显式）。
        # 不透传 orig：本文件全部断言只依赖 calls，且不向真实 logs/audit.log
        # 写入测试数据（隔离测试不应污染运行环境日志）。
        calls.append({"action": action, **kwargs})
        return None

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
        headers=headers, json={"is_active": False, "description": "文本值不进审计"},
    )
    assert ok.status_code == 200, ok.text
    hit = [c for c in calls if c["action"] == "admin_patch_bot"]
    assert len(hit) == 1
    # 布尔记 k=v（启停方向），自由文本仅记键名。
    assert hit[0]["detail"] == "description,is_active=0"
    assert "文本值不进审计" not in hit[0]["detail"]

    missing = client.patch(
        "/api/admin/bots/999999", headers=headers, json={"is_active": True},
    )
    assert missing.status_code == 404
    nofields = client.patch(
        f"/api/admin/bots/{bot['id']}", headers=headers, json={},
    )
    assert nofields.status_code == 400
    badtype = client.patch(
        f"/api/admin/bots/{bot['id']}", headers=headers, json={"is_active": "yes"},
    )
    assert badtype.status_code == 422
    unknown = client.patch(
        f"/api/admin/bots/{bot['id']}", headers=headers, json={"owner_id": 5},
    )
    assert unknown.status_code == 422
    fails = [c for c in calls if c["action"] == "admin_patch_bot" and c["result"] == "fail"]
    assert [f["detail"] for f in fails] == ["not_found", "no_fields", "bad_type_is_active", "unknown_fields"]


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


def test_admin_comment_delete_audits_only_admin_force_path(tmp_path, monkeypatch):
    """admin 强删他人评论必须留痕；作者自删/非 admin 403 不产生 admin 审计。"""
    import bzplat.backend.main as main_mod

    app = create_app(db_path=str(tmp_path / "audit.db"))
    store = app.state.store
    admin = store.create_user(
        "audit-admin", "audit-admin@example.com",
        hash_password("pw123456"), role="admin",
    )
    store.update_user(admin["id"], email_verified=1)
    author = store.create_user(
        "c-author", "c-author@example.com", hash_password("pw123456")
    )
    store.update_user(author["id"], email_verified=1)
    stranger = store.create_user(
        "c-stranger", "c-stranger@example.com", hash_password("pw123456")
    )
    store.update_user(stranger["id"], email_verified=1)
    _, token = app.state.auth.authenticate("audit-admin", "pw123456")
    _, author_token = app.state.auth.authenticate("c-author", "pw123456")
    _, stranger_token = app.state.auth.authenticate("c-stranger", "pw123456")

    from pathlib import Path as _P

    bin_path = str(tmp_path / "cmt-bot.elf")
    _P(bin_path).write_bytes(b"fixture")
    bot_a = store.create_bot(author["id"], "cmta", binary_path=bin_path,
                             format="elf", game_id="holdem")
    bot_b = store.create_bot(author["id"], "cmtb", binary_path=bin_path,
                             format="elf", game_id="holdem")
    match = store.create_match("holdem", bot_a_id=bot_a["id"], bot_b_id=bot_b["id"])
    comment = store.add_comment(
        author["id"], "match", match["id"], "内容"
    )

    calls: list[dict] = []
    import bzplat.backend.api_routes as api_routes_mod

    orig = api_routes_mod.audit_log

    def capture(request, action, **kwargs):
        calls.append({"action": action, **kwargs})
        return None

    monkeypatch.setattr(api_routes_mod, "audit_log", capture)
    from fastapi.testclient import TestClient

    client = TestClient(app)
    admin_h = {"Authorization": f"Bearer {token}"}
    author_h = {"Authorization": f"Bearer {author_token}"}

    # 作者自删自己的一条新评论：200，无 admin 审计。
    own = store.add_comment(author["id"], "match", match["id"], "自己的")
    r = client.delete(f"/api/comments/{own['id']}", headers=author_h)
    assert r.status_code == 200
    assert all(c["action"] != "admin_comment_delete" for c in calls)

    # 非 admin 删他人评论：403，无审计。
    r = client.delete(f"/api/comments/{comment['id']}",
                      headers={"Authorization": f"Bearer {stranger_token}"})
    assert r.status_code == 403
    assert all(c["action"] != "admin_comment_delete" for c in calls)

    # admin 强删：审计 ok。
    r = client.delete(f"/api/comments/{comment['id']}", headers=admin_h)
    assert r.status_code == 200, r.text
    hit = [c for c in calls if c["action"] == "admin_comment_delete"]
    assert len(hit) == 1
    assert hit[0].get("result") == "ok"

    # admin 删不存在的评论：404 且留 not_found fail 审计（覆盖 exists 与
    # 删除之间的竞态——返回 False 绝不记 ok）。
    r = client.delete("/api/comments/999999", headers=admin_h)
    assert r.status_code == 404
    hit2 = [c for c in calls if c["action"] == "admin_comment_delete"]
    assert len(hit2) == 2
    assert hit2[1].get("result") == "fail"
    assert hit2[1]["detail"] == "not_found"
    store.close()


def test_admin_rejection_paths_leave_fail_audit(tmp_path, monkeypatch):
    """set_role/delete_user/delete_bot 的 400/404/409 拒绝路径同样留痕。"""
    store, client, headers, admin, victim, calls = _setup(tmp_path, monkeypatch)

    r = client.post(f"/api/admin/users/{victim['id']}/role?role=superuser",
                    headers=headers)
    assert r.status_code == 400
    r = client.post("/api/admin/users/999999/role?role=user", headers=headers)
    assert r.status_code == 404
    r = client.delete(f"/api/admin/users/{admin['id']}", headers=headers)
    assert r.status_code == 400
    r = client.delete("/api/admin/users/999999", headers=headers)
    assert r.status_code == 404
    r = client.delete("/api/admin/bots/999999", headers=headers)
    assert r.status_code == 404

    def fails(action):
        return [c for c in calls if c["action"] == action and c.get("result") == "fail"]

    assert len(fails("admin_set_role")) == 2
    assert {c["detail"] for c in fails("admin_set_role")} == {"invalid_role", "not_found"}
    assert len(fails("admin_delete_user")) == 2
    assert {c["detail"] for c in fails("admin_delete_user")} == {"self_delete", "not_found"}
    assert len(fails("admin_delete_bot")) == 1
    assert fails("admin_delete_bot")[0]["detail"] == "not_found"


def test_admin_patch_site_partial_commit_audits_fail(tmp_path, monkeypatch):
    """set_setting 中途失败：已提交键的 partial 审计必须留痕。"""
    store, client, headers, admin, victim, calls = _setup(tmp_path, monkeypatch)

    real_set = store.set_setting
    state = {"n": 0}

    def flaky(key, value):
        state["n"] += 1
        if state["n"] == 2:
            raise RuntimeError("simulated mid-commit failure")
        return real_set(key, value)

    from fastapi.testclient import TestClient as _TC

    quiet = _TC(client.app, raise_server_exceptions=False)
    store.set_setting = flaky  # 实例级影子，仅影响本测试
    try:
        r = quiet.patch("/api/admin/settings/site", headers=headers,
                        json={"name": "新名字", "announcement": "公告"})
        assert r.status_code == 500
    finally:
        del store.set_setting  # 移除实例影子
    hit = [c for c in calls if c["action"] == "admin_patch_site"]
    assert len(hit) == 1
    assert hit[0]["result"] == "fail"
    assert hit[0]["detail"] == "partial:name"


def test_admin_validation_rejected_422_is_audited(tmp_path, monkeypatch):
    """Pydantic 层 422（handler 前）：/api/admin/* 写方法留痕，无 input 回显。"""
    import bzplat.backend.security as security_mod

    store, client, headers, admin, victim, calls = _setup(tmp_path, monkeypatch)
    orig = security_mod.audit_log

    def capture(request, action, **kwargs):
        calls.append({"action": action, **kwargs})
        return None

    monkeypatch.setattr(security_mod, "audit_log", capture)

    r = client.patch(f"/api/admin/users/{victim['id']}",
                     headers=headers, json={"is_active": "maybe"})
    assert r.status_code == 422
    hit = [c for c in calls if c["action"] == "admin_validation_rejected"]
    assert len(hit) == 1
    assert hit[0]["result"] == "fail"
    assert hit[0]["target"] == f"/api/admin/users/{victim['id']}"
    assert "detail" not in hit[0]  # 不回显任何请求内容
