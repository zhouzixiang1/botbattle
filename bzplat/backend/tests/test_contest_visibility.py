"""赛事可见性测试（审计 P1-E）。

GET /api/contests 始终应对访客/普通用户排除 draft/cancelled，不能靠显式
status 绕过。组织者仅可见自己主办的隐藏赛事，admin 可见全部。
"""
from __future__ import annotations

import os
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password


def _app(tmp_path):
    from bzplat.backend.main import create_app
    os.environ["BZ_BOT_LOCAL"] = "1"
    os.environ["BZ_SKIP_CAPTCHA"] = "1"
    return create_app(db_path=str(tmp_path / "cv.db"))


def _setup(app):
    """建两个组织者 + admin + 普通用户及公开/隐藏赛事。"""
    store = app.state.store
    org = store.create_user("org", "org@e.com", hash_password("pw123456"))
    store.update_user(org["id"], role="organizer", email_verified=1)
    adm = store.create_user("adm", "a@e.com", hash_password("pw123456"))
    store.update_user(adm["id"], role="admin", email_verified=1)
    other_org = store.create_user(
        "otherorg", "otherorg@e.com", hash_password("pw123456")
    )
    store.update_user(other_org["id"], role="organizer", email_verified=1)
    usr = store.create_user("usr", "u@e.com", hash_password("pw123456"))
    store.update_user(usr["id"], email_verified=1)
    # 3 个赛事：draft（默认）/ open / cancelled
    c_draft = store.create_contest("草稿赛", org["id"], game_id="holdem", status="draft")
    c_open = store.create_contest("公开赛", org["id"], game_id="holdem", status="open")
    c_cancel = store.create_contest("已取消赛", org["id"], game_id="holdem", status="cancelled")
    c_other_draft = store.create_contest(
        "其他组织者草稿", other_org["id"], game_id="holdem", status="draft"
    )
    return store, {"org": org, "other_org": other_org, "adm": adm, "usr": usr,
                   "c_draft": c_draft, "c_open": c_open, "c_cancel": c_cancel,
                   "c_other_draft": c_other_draft}


def _tok(app, username):
    _, t = app.state.auth.authenticate(username, "pw123456")
    return {"Authorization": f"Bearer {t}"}


def test_visitor_does_not_see_draft_or_cancelled(tmp_path):
    """访客（未登录）GET /api/contests 不应见 draft/cancelled 赛事。"""
    app = _app(tmp_path)
    store, ctx = _setup(app)
    client = TestClient(app)
    r = client.get("/api/contests")
    assert r.status_code == 200
    titles = [c["title"] for c in r.json()["contests"]]
    assert "公开赛" in titles
    assert "草稿赛" not in titles, "访客不应见 draft 赛事"
    assert "已取消赛" not in titles, "访客不应见 cancelled 赛事"

    # contest_detail 也不能直读 draft（审计 P1-E：list 已守，detail 漏守）
    r2 = client.get(f"/api/contests/{ctx['c_draft']['id']}")
    assert r2.status_code == 404, "访客不应直读 draft 赛事 detail（id 枚举泄漏）"
    r3 = client.get(f"/api/contests/{ctx['c_cancel']['id']}")
    assert r3.status_code == 404, "访客不应直读 cancelled 赛事 detail"
    r3b = client.get(f"/api/contests/{ctx['c_draft']['id']}/bracket")
    assert r3b.status_code == 404, "访客不应绕过 detail 直读 draft bracket"
    # organizer 可读自己的 draft
    r4 = client.get(f"/api/contests/{ctx['c_draft']['id']}", headers=_tok(app, "org"))
    assert r4.status_code == 200
    assert r4.json()["contest"]["status"] == "draft"


def test_normal_user_does_not_see_draft(tmp_path):
    """普通用户登录后仍不见 draft/cancelled。"""
    app = _app(tmp_path)
    _, ctx = _setup(app)
    client = TestClient(app)
    r = client.get("/api/contests", headers=_tok(app, "usr"))
    assert r.status_code == 200
    titles = [c["title"] for c in r.json()["contests"]]
    assert "草稿赛" not in titles
    assert "已取消赛" not in titles
    assert "公开赛" in titles

    # 登录普通用户显式 status 与直达子资源同样不可绕过。
    assert client.get(
        "/api/contests?status=draft", headers=_tok(app, "usr")
    ).json()["contests"] == []
    assert client.get(
        f"/api/contests/{ctx['c_draft']['id']}/bracket",
        headers=_tok(app, "usr"),
    ).status_code == 404


def test_hidden_detail_and_bracket_only_owner_or_admin(tmp_path):
    """其他 organizer 也不能读取不属于自己的隐藏赛事。"""
    app = _app(tmp_path)
    _, ctx = _setup(app)
    client = TestClient(app)
    did = ctx["c_draft"]["id"]
    for username in ("usr", "otherorg"):
        headers = _tok(app, username)
        assert client.get(f"/api/contests/{did}", headers=headers).status_code == 404
        assert client.get(
            f"/api/contests/{did}/bracket", headers=headers
        ).status_code == 404
    for username in ("org", "adm"):
        headers = _tok(app, username)
        assert client.get(f"/api/contests/{did}", headers=headers).status_code == 200
        assert client.get(
            f"/api/contests/{did}/bracket", headers=headers
        ).status_code == 200


def test_organizer_sees_only_own_hidden_while_admin_sees_all(tmp_path):
    """组织者只额外看到自己的 hidden；admin 才能看到所有 hidden。"""
    app = _app(tmp_path)
    _setup(app)
    client = TestClient(app)
    org_titles = [
        c["title"]
        for c in client.get("/api/contests", headers=_tok(app, "org")).json()["contests"]
    ]
    assert "草稿赛" in org_titles
    assert "已取消赛" in org_titles
    assert "公开赛" in org_titles
    assert "其他组织者草稿" not in org_titles

    admin_titles = [
        c["title"]
        for c in client.get("/api/contests", headers=_tok(app, "adm")).json()["contests"]
    ]
    assert {"草稿赛", "已取消赛", "公开赛", "其他组织者草稿"} <= set(admin_titles)

    # ACL 必须在 SQL 分页/COUNT 之前生效，不能先取一页再在 Python 裁剪。
    anon_page = client.get("/api/contests?page=1&per_page=1").json()
    org_page = client.get(
        "/api/contests?page=1&per_page=1", headers=_tok(app, "org")
    ).json()
    admin_page = client.get(
        "/api/contests?page=1&per_page=1", headers=_tok(app, "adm")
    ).json()
    assert anon_page["total"] == 1
    assert org_page["total"] == 3
    assert admin_page["total"] == 4


def test_explicit_hidden_status_cannot_bypass_visibility(tmp_path):
    """显式 status 仍受隐藏状态 ACL；owner/admin 仅看各自授权集合。"""
    app = _app(tmp_path)
    _setup(app)
    client = TestClient(app)
    # 访客显式查 draft 也只能得到空集。
    r = client.get("/api/contests?status=draft")
    assert r.status_code == 200
    assert r.json()["contests"] == []

    owner_titles = [
        c["title"]
        for c in client.get(
            "/api/contests?status=draft", headers=_tok(app, "org")
        ).json()["contests"]
    ]
    assert owner_titles == ["草稿赛"]

    admin_titles = {
        c["title"]
        for c in client.get(
            "/api/contests?status=draft", headers=_tok(app, "adm")
        ).json()["contests"]
    }
    assert admin_titles == {"草稿赛", "其他组织者草稿"}
