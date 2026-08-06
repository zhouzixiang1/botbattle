"""组织者导出 + 实名信息测试。

覆盖：
- GET /api/contests/{id}/export：组织者 CSV 含实名+排名；非组织者 403。
- GET /api/contests/{id}：组织者 entries 含 real_name；非组织者脱敏（无 real_name）。
- is_organizer 字段正确。
"""
from __future__ import annotations

import csv
import io

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from fastapi.testclient import TestClient


def _app(tmp_path):
    return create_app(db_path=str(tmp_path / "exp.db"))


def _setup(app, *, require_real_name=True):
    """建组织者 + 报名者（带实名）+ 赛事 + 报名。返回 (store, org_token, user_token, contest_id)。"""
    store = app.state.store
    org = store.create_user("orgexp", "orgexp@e.com", hash_password("pw123456"), role="organizer")
    store.update_user(org["id"], email_verified=1)
    u = store.create_user("ent1", "ent1@e.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1, real_name="张三", phone="13800000001",
                      school="测试大学", student_id="2024001")
    bu = store.create_bot(u["id"], "ent1bot", binary_path="/tmp", format="elf", game_id="holdem")
    cid = store.create_contest(
        "导出测试赛", organizer_id=org["id"], game_id="holdem",
        stages_json='[{"key":"s1","type":"round_robin","scoring":"poker_3_1_0"}]',
        status="open",  # 公开赛事（draft 仅 organizer 可见，见 test_contest_visibility）
        require_real_name=1 if require_real_name else 0,
    )["id"]
    store.add_contest_entry(cid, u["id"], bu["id"])
    _, org_tok = app.state.auth.authenticate("orgexp", "pw123456")
    _, user_tok = app.state.auth.authenticate("ent1", "pw123456")
    return store, org_tok, user_tok, cid


def test_export_organizer_csv_has_realname(tmp_path):
    """组织者导出 CSV 含实名信息。"""
    app = _app(tmp_path)
    store, org_tok, user_tok, cid = _setup(app)
    client = TestClient(app)
    h = {"Authorization": f"Bearer {org_tok}"}
    r = client.get(f"/api/contests/{cid}/export?format=csv", headers=h)
    assert r.status_code == 200, f"组织者导出应 200，实际 {r.status_code}"
    # 解析 CSV（去 BOM）
    text = r.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert len(rows) >= 1, "应有至少 1 行报名"
    row = rows[0]
    assert row.get("real_name") == "张三", f"CSV 应含 real_name=张三，实际 {row.get('real_name')}"
    assert row.get("phone") == "13800001"[:8] or row.get("phone") == "13800000001"
    assert row.get("school") == "测试大学"
    assert row.get("student_id") == "2024001"
    assert row.get("bot_name") == "ent1bot"
    store.close()


def test_export_non_organizer_forbidden(tmp_path):
    """非组织者不能导出（403）。"""
    app = _app(tmp_path)
    store, org_tok, user_tok, cid = _setup(app)
    client = TestClient(app)
    h = {"Authorization": f"Bearer {user_tok}"}
    r = client.get(f"/api/contests/{cid}/export?format=csv", headers=h)
    assert r.status_code == 403, f"非组织者应 403，实际 {r.status_code}"
    store.close()


def test_contest_detail_realname_visible_to_organizer_only(tmp_path):
    """contest_detail：组织者 entries 含 real_name；非组织者脱敏。"""
    app = _app(tmp_path)
    store, org_tok, user_tok, cid = _setup(app)
    client = TestClient(app)
    # 组织者视角
    r_org = client.get(f"/api/contests/{cid}", headers={"Authorization": f"Bearer {org_tok}"})
    assert r_org.status_code == 200
    d_org = r_org.json()
    assert d_org["is_organizer"] is True, "组织者 is_organizer 应 True"
    entries_org = d_org["entries"]
    assert len(entries_org) >= 1
    assert entries_org[0].get("real_name") == "张三", "组织者应见 real_name"
    assert entries_org[0].get("phone") == "13800000001", "组织者应见 phone"
    # 非组织者视角（另一个报名者）
    r_user = client.get(f"/api/contests/{cid}", headers={"Authorization": f"Bearer {user_tok}"})
    d_user = r_user.json()
    assert d_user["is_organizer"] is False, "非组织者 is_organizer 应 False"
    entries_user = d_user["entries"]
    assert "real_name" not in entries_user[0], "非组织者不应见 real_name（脱敏）"
    assert "phone" not in entries_user[0], "非组织者不应见 phone（脱敏）"
    store.close()


def test_contest_detail_realname_anonymous_hidden(tmp_path):
    """未登录访客也不应见实名。"""
    app = _app(tmp_path)
    store, org_tok, user_tok, cid = _setup(app)
    client = TestClient(app)
    r = client.get(f"/api/contests/{cid}")
    d = r.json()
    assert d["is_organizer"] is False
    for e in d["entries"]:
        assert "real_name" not in e
        assert "student_id" not in e
    store.close()
