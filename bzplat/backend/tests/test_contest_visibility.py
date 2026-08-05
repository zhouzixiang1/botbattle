"""赛事可见性测试（审计 P1-E）。

GET /api/contests 默认应排除 draft/cancelled 赛事（组织者未发布的结构不应
提前暴露给访客）。组织者/admin 可见全部；显式传 status 尊重调用方。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password


def _app(tmp_path):
    from bzplat.backend.main import create_app
    os.environ["BZ_BOT_LOCAL"] = "1"
    os.environ["BZ_SKIP_CAPTCHA"] = "1"
    return create_app(db_path=str(tmp_path / "cv.db"))


def _setup(app):
    """建组织者 + admin + 普通用户，各造一个不同状态的赛事。"""
    store = app.state.store
    org = store.create_user("org", "org@e.com", hash_password("pw123456"))
    store.update_user(org["id"], role="organizer", email_verified=1)
    adm = store.create_user("adm", "a@e.com", hash_password("pw123456"))
    store.update_user(adm["id"], role="admin", email_verified=1)
    usr = store.create_user("usr", "u@e.com", hash_password("pw123456"))
    store.update_user(usr["id"], email_verified=1)
    # 3 个赛事：draft（默认）/ open / cancelled
    c_draft = store.create_contest("草稿赛", org["id"], game_id="holdem", status="draft")
    c_open = store.create_contest("公开赛", org["id"], game_id="holdem", status="open")
    c_cancel = store.create_contest("已取消赛", org["id"], game_id="holdem", status="cancelled")
    return store, {"org": org, "adm": adm, "usr": usr,
                   "c_draft": c_draft, "c_open": c_open, "c_cancel": c_cancel}


def _tok(app, username):
    _, t = app.state.auth.authenticate(username, "pw123456")
    return {"Authorization": f"Bearer {t}"}


def test_visitor_does_not_see_draft_or_cancelled(tmp_path):
    """访客（未登录）GET /api/contests 不应见 draft/cancelled 赛事。"""
    app = _app(tmp_path)
    _setup(app)
    client = TestClient(app)
    r = client.get("/api/contests")
    assert r.status_code == 200
    titles = [c["title"] for c in r.json()["contests"]]
    assert "公开赛" in titles
    assert "草稿赛" not in titles, "访客不应见 draft 赛事"
    assert "已取消赛" not in titles, "访客不应见 cancelled 赛事"


def test_normal_user_does_not_see_draft(tmp_path):
    """普通用户登录后仍不见 draft/cancelled。"""
    app = _app(tmp_path)
    _setup(app)
    client = TestClient(app)
    r = client.get("/api/contests", headers=_tok(app, "usr"))
    assert r.status_code == 200
    titles = [c["title"] for c in r.json()["contests"]]
    assert "草稿赛" not in titles
    assert "已取消赛" not in titles
    assert "公开赛" in titles


def test_organizer_sees_all(tmp_path):
    """组织者/admin 可见全部状态赛事（含自己的 draft）。"""
    app = _app(tmp_path)
    _setup(app)
    client = TestClient(app)
    for role in ("org", "adm"):
        r = client.get("/api/contests", headers=_tok(app, role))
        assert r.status_code == 200
        titles = [c["title"] for c in r.json()["contests"]]
        assert "草稿赛" in titles, f"{role} 应见 draft"
        assert "公开赛" in titles
        assert "已取消赛" in titles, f"{role} 应见 cancelled"


def test_explicit_status_respected(tmp_path):
    """显式传 status 参数时尊重调用方（不做默认排除）。"""
    app = _app(tmp_path)
    _setup(app)
    client = TestClient(app)
    # 访客显式查 draft —— 应返回 draft 赛事（调用方明确要 draft）
    r = client.get("/api/contests?status=draft")
    assert r.status_code == 200
    titles = [c["title"] for c in r.json()["contests"]]
    assert "草稿赛" in titles
    assert "公开赛" not in titles  # 显式 status=draft 只返回 draft
