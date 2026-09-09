"""赛事归档：软隐藏、显式筛选、详情可达、权限与状态守卫。

归档 = 组织者对已结束赛事的软隐藏：默认列表（含 admin/组织者本人）不显示，
``archived=only`` 显式筛选才可见；详情/回放/正式榜保持可达；归档赛事不再
作为新赛创建的来源候选。仅 finished 可归档，showcase 只读，操作幂等并审计。
"""
from __future__ import annotations

import os
import sqlite3

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store


def _app(tmp_path):
    from bzplat.backend.main import create_app
    os.environ["BZ_BOT_LOCAL"] = "1"
    os.environ["BZ_SKIP_CAPTCHA"] = "1"
    return create_app(db_path=str(tmp_path / "ca.db"))


def _setup(app):
    store = app.state.store
    org = store.create_user("org", "org@e.com", hash_password("pw123456"))
    store.update_user(org["id"], role="organizer", email_verified=1)
    other_org = store.create_user(
        "otherorg", "otherorg@e.com", hash_password("pw123456")
    )
    store.update_user(other_org["id"], role="organizer", email_verified=1)
    adm = store.create_user("adm", "a@e.com", hash_password("pw123456"))
    store.update_user(adm["id"], role="admin", email_verified=1)
    usr = store.create_user("usr", "u@e.com", hash_password("pw123456"))
    store.update_user(usr["id"], email_verified=1)
    c_old_a = store.create_contest(
        "旧赛A", org["id"], game_id="holdem", status="finished"
    )
    c_old_b = store.create_contest(
        "旧赛B", org["id"], game_id="holdem", status="finished"
    )
    c_open = store.create_contest("公开中赛", org["id"], game_id="holdem", status="open")
    c_running = store.create_contest(
        "运行中赛", org["id"], game_id="holdem", status="running"
    )
    return store, {
        "org": org, "other_org": other_org, "adm": adm, "usr": usr,
        "c_old_a": c_old_a, "c_old_b": c_old_b,
        "c_open": c_open, "c_running": c_running,
    }


def _tok(app, username):
    _, t = app.state.auth.authenticate(username, "pw123456")
    return {"Authorization": f"Bearer {t}"}


def _titles(client, query=""):
    r = client.get(f"/api/contests{query}")
    assert r.status_code == 200
    return [c["title"] for c in r.json()["contests"]]


def test_archive_hides_from_default_list_and_detail_stays_reachable(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    client = TestClient(app)
    cid = ctx["c_old_a"]["id"]

    r = client.post(f"/api/contests/{cid}/archive", headers=_tok(app, "org"))
    assert r.status_code == 200
    assert r.json()["contest"]["archived_at"]

    # 默认列表对访客/组织者本人/admin 一律隐藏归档
    assert "旧赛A" not in _titles(client)
    for username in ("org", "adm"):
        resp = client.get("/api/contests", headers=_tok(app, username))
        assert "旧赛A" not in [c["title"] for c in resp.json()["contests"]]
    assert "旧赛B" in _titles(client)

    # 显式筛选：only 只看归档；include 全量
    only = _titles(client, "?archived=only")
    assert only == ["旧赛A"]
    include = _titles(client, "?archived=include")
    assert {"旧赛A", "旧赛B"} <= set(include)
    # 非法值 fail closed
    assert client.get("/api/contests?archived=someone").status_code == 422

    # 归档 ≠ 删除：详情（含公开字段 archived_at）仍可达
    d = client.get(f"/api/contests/{cid}")
    assert d.status_code == 200
    assert d.json()["contest"]["archived_at"]

    # 取消归档后回到默认列表；重复取消幂等
    r2 = client.post(f"/api/contests/{cid}/unarchive", headers=_tok(app, "org"))
    assert r2.status_code == 200
    assert r2.json()["contest"]["archived_at"] is None
    assert "旧赛A" in _titles(client)
    r3 = client.post(f"/api/contests/{cid}/unarchive", headers=_tok(app, "org"))
    assert r3.status_code == 200


def test_archive_permission_and_status_guards(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)
    client = TestClient(app)

    # 未登录 401；普通用户 403；他人组织者 403
    assert client.post(f"/api/contests/{ctx['c_old_a']['id']}/archive").status_code == 401
    assert client.post(
        f"/api/contests/{ctx['c_old_a']['id']}/archive",
        headers=_tok(app, "usr"),
    ).status_code == 403
    assert client.post(
        f"/api/contests/{ctx['c_old_a']['id']}/archive",
        headers=_tok(app, "otherorg"),
    ).status_code == 403

    # 非 finished 一律拒绝（open 与 running）
    for bad in ("c_open", "c_running"):
        r = client.post(
            f"/api/contests/{ctx[bad]['id']}/archive", headers=_tok(app, "org")
        )
        assert r.status_code == 400, bad
    assert store.get_contest(ctx["c_running"]["id"])["archived_at"] is None

    # admin 可以归档他人赛事；重复归档幂等（时间戳保持首次值）
    cid = ctx["c_old_b"]["id"]
    r1 = client.post(f"/api/contests/{cid}/archive", headers=_tok(app, "adm"))
    assert r1.status_code == 200
    first_at = r1.json()["contest"]["archived_at"]
    r2 = client.post(f"/api/contests/{cid}/archive", headers=_tok(app, "org"))
    assert r2.status_code == 200
    assert r2.json()["contest"]["archived_at"] == first_at

    # 不存在的赛事 404
    assert client.post(
        "/api/contests/999999/archive", headers=_tok(app, "org")
    ).status_code == 404


def test_archived_contests_leave_source_candidates(tmp_path):
    app = _app(tmp_path)
    store, ctx = _setup(app)

    def _source(title: str) -> dict:
        contest = store.create_contest(
            title, ctx["org"]["id"], game_id="gomoku",
            template_id="gomoku_drr", status="finished",
        )
        store.update_contest(contest["id"], official_results_ready=1)
        return store.get_contest(contest["id"])

    live = _source("合格来源")
    archived = _source("归档来源")
    store.set_contest_archived(archived["id"], archived=True)

    result = store.list_contest_source_candidates(
        game_id="gomoku", source_kind="protected_seed"
    )
    ids = [row["id"] for row in result["items"]]
    assert live["id"] in ids
    assert archived["id"] not in ids


def test_legacy_contests_gain_archived_at_column_idempotently(tmp_path):
    """旧库缺 archived_at 列时，重开自动补列；再次重开不重复迁移。"""
    db_path = str(tmp_path / "legacy.db")
    store = Store(db_path)
    store.close()

    conn = sqlite3.connect(db_path)
    # 旧库形态：列与其 partial index 均不存在（DROP COLUMN 前必须先去索引）
    conn.execute("DROP INDEX IF EXISTS idx_contests_archived_at")
    conn.execute("ALTER TABLE contests DROP COLUMN archived_at")
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(contests)")]
    assert "archived_at" not in cols
    conn.close()

    store = Store(db_path)  # 重开补列
    assert store.set_contest_archived(1, archived=True) is None  # 无该赛事
    version = sqlite3.connect(db_path).execute("PRAGMA schema_version").fetchone()[0]
    store.close()

    store = Store(db_path)  # 再开幂等
    conn = sqlite3.connect(db_path)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(contests)")]
    assert "archived_at" in cols
    assert conn.execute("PRAGMA schema_version").fetchone()[0] == version
    conn.close()
    store.close()
