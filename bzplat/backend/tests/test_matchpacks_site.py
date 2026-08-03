"""对局数据集下载 + 站点配置测试（PR-10）。"""
from __future__ import annotations

import gzip
import json
from datetime import datetime

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store

CUR_MONTH = datetime.now().strftime("%Y-%m")  # 当前年月（created_at 用当前时间）


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "ms.db"))


def test_matchpack_months_and_rows(tmp_path):
    s = _store(tmp_path)
    u = s.create_user("alice", "a@ex.com", "x")
    b1 = s.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = s.create_bot(u["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    s.create_match("m1", bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=u["id"])
    s.update_match("m1", status="completed")
    s.create_match("m2", bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=u["id"])
    s.update_match("m2", status="completed")
    s.upsert_replay("m1", '[{"type":"match_end"}]', "[]")
    months = s.matchpack_months()
    assert len(months) == 1
    assert months[0]["game_id"] == "holdem"
    assert months[0]["month"] == CUR_MONTH
    assert months[0]["cnt"] == 2
    rows = s.matchpack_rows("holdem", CUR_MONTH)
    assert len(rows) == 2
    assert rows[0]["bot_a_name"] == "botA"
    s.close()


# ── HTTP 端点 ────────────────────────────────────────────
def _app(tmp_path):
    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u = store.create_user("alice", "a@ex.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    b1 = store.create_bot(u["id"], "botA", binary_path="/tmp", format="elf", game_id="holdem")
    b2 = store.create_bot(u["id"], "botB", binary_path="/tmp", format="elf", game_id="holdem")
    mid = "m1"
    store.create_match(mid, bot_a_id=b1["id"], bot_b_id=b2["id"], owner_id=u["id"])
    store.update_match(mid, status="completed")
    store.upsert_replay(mid, '[{"type":"match_end"}]', "[]")
    ad = store.create_user("admin", "ad@ex.com", hash_password("pw123456"), role="admin")
    store.update_user(ad["id"], email_verified=1)
    _, tok = app.state.auth.authenticate("alice", "pw123456")
    _, atok = app.state.auth.authenticate("admin", "pw123456")
    c = TestClient(app)
    return c, tok, atok, u["id"]


def test_site_info_endpoint(tmp_path):
    c, tok, atok, uid = _app(tmp_path)
    r = c.get("/api/site/info")
    assert r.status_code == 200
    assert r.json()["name"] == "Botbattle"
    assert r.json()["about"]


def test_admin_patch_site(tmp_path):
    c, tok, atok, uid = _app(tmp_path)
    h = {"Authorization": f"Bearer {atok}"}
    r = c.patch("/api/admin/settings/site", json={"name": "MyArena", "announcement": "hello"}, headers=h)
    assert r.status_code == 200
    assert r.json()["site"]["name"] == "MyArena"
    assert r.json()["site"]["announcement"] == "hello"
    # 公开读更新后
    r = c.get("/api/site/info")
    assert r.json()["name"] == "MyArena"


def test_admin_patch_site_requires_admin(tmp_path):
    c, tok, atok, uid = _app(tmp_path)
    h = {"Authorization": f"Bearer {tok}"}  # 普通用户
    r = c.patch("/api/admin/settings/site", json={"name": "x"}, headers=h)
    assert r.status_code == 403


def test_matchpacks_list_endpoint(tmp_path):
    c, tok, atok, uid = _app(tmp_path)
    r = c.get("/api/matchpacks")
    assert r.status_code == 200
    assert len(r.json()["packs"]) >= 1


def test_download_gated_by_level(tmp_path):
    c, tok, atok, uid = _app(tmp_path)
    h = {"Authorization": f"Bearer {tok}"}
    # level 0 → 403
    r = c.get(f"/api/matchpacks/download?game_id=holdem&month={CUR_MONTH}", headers=h)
    assert r.status_code == 403
    # 升到 level 1
    c.app.state.store.award_xp(uid, 200)
    r = c.get(f"/api/matchpacks/download?game_id=holdem&month={CUR_MONTH}", headers=h)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/gzip"
    # 解压验证内容
    data = gzip.decompress(r.content)
    lines = data.decode("utf-8").strip().split("\n")
    assert len(lines) == 1
    obj = json.loads(lines[0])
    assert obj["game_id"] == "holdem"
    assert obj["events"] == [{"type": "match_end"}]


def test_download_requires_auth(tmp_path):
    c, tok, atok, uid = _app(tmp_path)
    r = c.get(f"/api/matchpacks/download?game_id=holdem&month={CUR_MONTH}")
    assert r.status_code == 401
