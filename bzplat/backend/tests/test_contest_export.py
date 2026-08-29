"""组织者导出 + 实名信息测试。

覆盖：
- GET /api/contests/{id}/export：组织者 CSV 含实名+排名；非组织者 403。
- GET /api/contests/{id}：组织者 entries 含 real_name；非组织者脱敏（无 real_name）。
- is_organizer 字段正确。
"""
from __future__ import annotations

import csv
import io
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
import bzplat.backend.store.db as store_db_module
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store
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
    """无 schema 的 CSV v1 保持列契约，并读取报名快照而非当前资料。"""
    app = _app(tmp_path)
    store, org_tok, user_tok, cid = _setup(app)
    entrant = store.get_user_by_username("ent1")
    store.update_user(
        entrant["id"], real_name="报名后姓名", phone="13999999999",
        school="报名后学校", student_id="CHANGED",
    )
    client = TestClient(app)
    h = {"Authorization": f"Bearer {org_tok}"}
    r = client.get(f"/api/contests/{cid}/export?format=csv", headers=h)
    assert r.status_code == 200, f"组织者导出应 200，实际 {r.status_code}"
    assert r.headers["content-disposition"] == (
        f'attachment; filename="contest-{cid}-export.csv"'
    )
    assert r.headers["cache-control"] == "private, no-store, max-age=0"
    assert r.headers["pragma"] == "no-cache"
    assert r.headers["vary"] == "Authorization, Cookie"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    # 解析 CSV（去 BOM）
    text = r.content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    rows = list(reader)
    assert reader.fieldnames == [
        "rank", "seed", "group_id", "bot_name", "owner_name", "real_name",
        "phone", "school", "student_id", "points", "wins", "draws",
        "losses", "eliminated", "awarded", "registered_at",
    ]
    assert len(rows) >= 1, "应有至少 1 行报名"
    row = rows[0]
    assert row.get("real_name") == "张三", f"CSV 应含 real_name=张三，实际 {row.get('real_name')}"
    assert row.get("phone") == "'13800000001"
    assert row.get("school") == "测试大学"
    assert row.get("student_id") == "'2024001"
    assert row.get("bot_name") == "ent1bot"
    assert "报名后姓名" not in text
    store.close()


def test_export_non_organizer_forbidden(tmp_path):
    """非组织者不能导出（403）。"""
    app = _app(tmp_path)
    store, org_tok, user_tok, cid = _setup(app)
    client = TestClient(app)
    h = {"Authorization": f"Bearer {user_tok}"}
    r = client.get(f"/api/contests/{cid}/export?format=csv", headers=h)
    assert r.status_code == 403, f"非组织者应 403，实际 {r.status_code}"
    assert r.headers["cache-control"] == "private, no-store, max-age=0"
    assert r.headers["vary"] == "Authorization, Cookie"
    assert r.headers["referrer-policy"] == "no-referrer"
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-disposition" not in r.headers
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
    assert entries_org[0]["identity_source"] == "registration_profile"
    assert entries_org[0]["identity_captured_at"]
    assert entries_org[0]["identity_complete"] == 1
    assert r_org.headers["cache-control"] == "private, no-store, max-age=0"
    assert r_org.headers["vary"] == "Authorization, Cookie"
    assert r_org.headers["referrer-policy"] == "no-referrer"
    assert r_org.headers["x-content-type-options"] == "nosniff"
    assert d_org["my_entry"] is None
    # 非组织者视角（另一个报名者）
    r_user = client.get(f"/api/contests/{cid}", headers={"Authorization": f"Bearer {user_tok}"})
    d_user = r_user.json()
    assert d_user["is_organizer"] is False, "非组织者 is_organizer 应 False"
    entries_user = d_user["entries"]
    assert "real_name" not in entries_user[0], "非组织者不应见 real_name（脱敏）"
    assert "phone" not in entries_user[0], "非组织者不应见 phone（脱敏）"
    assert not any("snapshot" in key for key in d_user["my_entry"])
    assert "identity_source" not in d_user["my_entry"]
    dispatched = client.post(
        f"/api/contests/{cid}/dispatch",
        headers={"Authorization": f"Bearer {user_tok}"},
        json={"bot_id": entries_user[0]["bot_id"]},
    )
    assert dispatched.status_code == 200, dispatched.text
    assert set(dispatched.json()["entry"]) == {
        "id", "contest_id", "user_id", "bot_id", "registered_at", "group_id",
        "seed", "eliminated", "dispatched_at",
    }
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


def test_non_real_name_contest_never_projects_current_pii_for_org_or_admin(tmp_path):
    """非实名赛即使是组织者/admin，也不读出当前用户实名资料。"""
    app = _app(tmp_path)
    store, org_tok, _user_tok, cid = _setup(app, require_real_name=False)
    admin = store.create_user(
        "expadmin", "expadmin@e.com", hash_password("pw123456"), role="admin"
    )
    store.update_user(admin["id"], email_verified=1)
    _, admin_tok = app.state.auth.authenticate("expadmin", "pw123456")
    client = TestClient(app)
    private_values = ("张三", "13800000001", "测试大学", "2024001")

    for token in (org_tok, admin_tok):
        headers = {"Authorization": f"Bearer {token}"}
        detail = client.get(f"/api/contests/{cid}", headers=headers)
        assert detail.status_code == 200
        entry = detail.json()["entries"][0]
        for field in (
            "real_name", "phone", "school", "student_id", "identity_source",
            "identity_captured_at", "identity_complete",
        ):
            assert field not in entry
        assert "cache-control" not in detail.headers

        admin_entries = (
            client.get(f"/api/admin/contests/{cid}/entries", headers=headers)
            if token == admin_tok else None
        )
        if admin_entries is not None:
            assert admin_entries.status_code == 200
            assert not any(
                field in admin_entries.json()["entries"][0]
                for field in ("real_name", "phone", "school", "student_id")
            )

        for schema_query in ("", "&schema=2"):
            exported = client.get(
                f"/api/contests/{cid}/export?format=csv{schema_query}",
                headers=headers,
            )
            assert exported.status_code == 200
            decoded = exported.content.decode("utf-8-sig")
            assert all(value not in decoded for value in private_values)
            row = next(csv.DictReader(io.StringIO(decoded)))
            pii_headers = (
                ("real_name", "phone", "school", "student_id")
                if not schema_query
                else (
                    "实名姓名(real_name)", "手机号(phone)", "学校(school)",
                    "学号(student_id)", "实名来源(identity_source)",
                )
            )
            assert all(row[field] == "" for field in pii_headers)
    store.close()


@pytest.mark.parametrize("endpoint", ["detail", "export_v1", "export_v2"])
def test_private_api_identity_gate_cannot_authorize_replacement_non_real_entry(
    tmp_path, monkeypatch, endpoint
):
    """The API's earlier contest view cannot authorize a newer identity row."""
    app = _app(tmp_path)
    store, org_tok, _user_tok, cid = _setup(app)
    entrant = store.get_user_by_username("ent1")
    original_entry = store.get_entry(cid, entrant["id"])
    bot = store.get_bot(original_entry["bot_id"])
    second = Store(store.path)
    original_projection = store_db_module._contest_identity_projection_sql
    projection_boundary = threading.Barrier(2)
    release_reader = threading.Event()
    writer_started = threading.Event()
    first_call_lock = threading.Lock()
    first_call = True
    audits: list[dict] = []

    def paused_projection(*args, **kwargs):
        nonlocal first_call
        with first_call_lock:
            should_pause = first_call
            first_call = False
        if should_pause:
            projection_boundary.wait(timeout=5)
            if not release_reader.wait(timeout=5):
                raise TimeoutError("private API reader release timed out")
        return original_projection(*args, **kwargs)

    monkeypatch.setattr(
        store_db_module, "_contest_identity_projection_sql", paused_projection
    )
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )
    client = TestClient(app)
    path = {
        "detail": f"/api/contests/{cid}",
        "export_v1": f"/api/contests/{cid}/export?format=csv",
        "export_v2": f"/api/contests/{cid}/export?format=csv&schema=2",
    }[endpoint]

    def replace_with_non_real_entry():
        writer_started.set()
        assert second.delete_contest_roster_entry(cid, entrant["id"])
        second.update_contest(cid, require_real_name=0)
        return second.add_entry(cid, entrant["id"], bot["id"])

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            future = executor.submit(
                client.get,
                path,
                headers={"Authorization": f"Bearer {org_tok}"},
            )
            projection_boundary.wait(timeout=5)
            if endpoint == "detail":
                # Detail now reads contest, pairings, roster and identity under
                # one SQLite snapshot.  Start the writer while that read
                # transaction is open, then release the reader: the first
                # response must remain the old authorized snapshot, and only a
                # later request may observe the replacement non-real roster.
                writer = executor.submit(replace_with_non_real_entry)
                assert writer_started.wait(timeout=5)
                release_reader.set()
                response = future.result(timeout=10)
                replacement = writer.result(timeout=10)
            else:
                # Export is one joined SELECT whose identity gate and row are
                # evaluated together.  Its query has not begun at this paused
                # SQL-construction boundary, so the replacement wins first.
                replacement = replace_with_non_real_entry()
                release_reader.set()
                response = future.result(timeout=10)

        assert response.status_code == 200, response.text
        assert int(store.get_contest(cid)["require_real_name"]) == 0
        assert replacement["identity_source"] is None
        private_values = ("张三", "13800000001", "测试大学", "2024001")
        identity_fields = (
            "real_name",
            "phone",
            "school",
            "student_id",
            "identity_source",
            "identity_captured_at",
            "identity_complete",
        )
        if endpoint == "detail":
            row = response.json()["entries"][0]
            assert row["real_name"] == "张三"
            assert row["identity_source"] == "registration_profile"
            assert all(value in response.text for value in private_values)
            replacement_view = client.get(
                path,
                headers={"Authorization": f"Bearer {org_tok}"},
            )
            assert replacement_view.status_code == 200
            replacement_row = replacement_view.json()["entries"][0]
            assert all(field not in replacement_row for field in identity_fields)
            assert all(value not in replacement_view.text for value in private_values)
        elif endpoint == "export_v1":
            assert all(value not in response.text for value in private_values)
            row = next(
                csv.DictReader(
                    io.StringIO(response.content.decode("utf-8-sig"))
                )
            )
            assert all(
                row[field] == ""
                for field in ("real_name", "phone", "school", "student_id")
            )
            assert audits == [{
                "action": "contest_export",
                "result": "ok",
                "user": "orgexp",
                "target": cid,
                "detail": (
                    "schema=1; rows=1; identity=excluded; "
                    "legacy_fallback_rows=0"
                ),
            }]
        else:
            assert all(value not in response.text for value in private_values)
            row = next(
                csv.DictReader(
                    io.StringIO(response.content.decode("utf-8-sig"))
                )
            )
            assert all(
                row[field] == ""
                for field in (
                    "实名姓名(real_name)",
                    "手机号(phone)",
                    "学校(school)",
                    "学号(student_id)",
                    "实名来源(identity_source)",
                    "实名采集时间(identity_captured_at)",
                    "实名完整性(identity_completeness)",
                )
            )
            assert audits == [{
                "action": "contest_export",
                "result": "ok",
                "user": "orgexp",
                "target": cid,
                "detail": (
                    "schema=2; rows=1; identity=excluded; "
                    "legacy_fallback_rows=0"
                ),
            }]
    finally:
        release_reader.set()
        second.close()
        store.close()


def test_legacy_real_name_entry_uses_explicit_current_profile_fallback(tmp_path):
    """旧实名报名不回填伪快照，私有读取显式标注当前资料回退。"""
    app = _app(tmp_path)
    store, org_tok, _user_tok, cid = _setup(app)
    entrant = store.get_user_by_username("ent1")
    entry = store.get_entry(cid, entrant["id"])
    store._conn.execute(
        "UPDATE contest_entries SET real_name_snapshot=NULL,phone_snapshot=NULL,"
        "school_snapshot=NULL,student_id_snapshot=NULL,identity_captured_at=NULL,"
        "identity_source=NULL WHERE id=?",
        (entry["id"],),
    )
    store._conn.commit()
    store.update_user(
        entrant["id"], real_name="旧赛当前姓名", phone="01001234567",
        school="旧赛当前学校", student_id="000042",
    )
    headers = {"Authorization": f"Bearer {org_tok}"}
    client = TestClient(app)

    detail = client.get(f"/api/contests/{cid}", headers=headers)
    projected = detail.json()["entries"][0]
    assert projected["real_name"] == "旧赛当前姓名"
    assert projected["identity_source"] == "current_profile_legacy"
    assert projected["identity_captured_at"] is None
    assert projected["identity_complete"] == 1

    exported = client.get(
        f"/api/contests/{cid}/export?format=csv&schema=2", headers=headers
    )
    row = next(csv.DictReader(io.StringIO(exported.content.decode("utf-8-sig"))))
    assert row["实名姓名(real_name)"] == "旧赛当前姓名"
    assert row["手机号(phone)"] == "'01001234567"
    assert row["学号(student_id)"] == "'000042"
    assert row["实名来源(identity_source)"] == "历史报名：当前资料回退（非快照）"
    assert row["实名采集时间(identity_captured_at)"] == ""
    assert row["实名完整性(identity_completeness)"] == "完整"
    store.close()


def test_schema2_export_is_readable_stable_safe_and_audited(tmp_path, monkeypatch):
    """v2 同时给出稳定 ID/显示名/结果阶段，并守住表格注入和 PII 审计。"""
    app = _app(tmp_path)
    store = app.state.store
    organizer = store.create_user(
        "orgv2", "orgv2@e.com", hash_password("pw123456"), role="organizer"
    )
    store.update_user(organizer["id"], email_verified=1)
    entrant = store.create_user(
        "account-v2", "account-v2@e.com", hash_password("pw123456"),
        display_name="=用户显示名", real_name="-报名姓名", phone="013800000001",
        school="@报名学校", student_id="000123",
    )
    bot = store.create_bot(
        entrant["id"], "bot-v2", display_name="+Bot显示名", binary_path="/tmp/v2",
        format="elf", game_id="holdem",
    )
    contest = store.create_contest(
        "直观导出赛", organizer_id=organizer["id"], game_id="holdem",
        status="open", require_real_name=1,
        stages_json='[{"key":"final","type":"round_robin"}]',
    )
    entry = store.add_contest_entry(contest["id"], entrant["id"], bot["id"])
    store.update_entry(
        contest["id"], entrant["id"], seed=0, group_id="=A组", eliminated=1
    )
    store.upsert_stage_result(
        contest["id"], 0, entry["id"], bot_id=bot["id"], stage_key="=final",
        points=9.5, wins=3, draws=1, losses=2, delta_total=88,
    )
    store.upsert_official_result(
        contest["id"], entry["id"], 1, stage_idx=0, points=9.5,
        bot_id=bot["id"], user_id=entrant["id"], awarded="@一等奖",
    )
    store.update_contest(
        contest["id"], status="finished", official_results_ready=1
    )
    _, token = app.state.auth.authenticate("orgv2", "pw123456")
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append({"action": action, **fields}),
    )

    response = TestClient(app).get(
        f"/api/contests/{contest['id']}/export?format=csv&schema=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200, response.text
    assert response.headers["content-disposition"] == (
        f'attachment; filename="contest-{contest["id"]}-participants-v2.csv"'
    )
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["vary"] == "Authorization, Cookie"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-type"] == "text/csv; charset=utf-8"

    reader = csv.DictReader(io.StringIO(response.content.decode("utf-8-sig")))
    row = next(reader)
    assert row["报名ID(entry_id)"] == str(entry["id"])
    assert row["用户ID(user_id)"] == str(entrant["id"])
    assert row["用户账号(username)"] == "account-v2"
    assert row["用户显示名(user_display)"] == "'=用户显示名"
    assert row["Bot ID(bot_id)"] == str(bot["id"])
    assert row["Bot内部名(bot_name)"] == "bot-v2"
    assert row["Bot显示名(bot_display)"] == "'+Bot显示名"
    assert row["实名姓名(real_name)"] == "'-报名姓名"
    assert row["手机号(phone)"] == "'013800000001"
    assert row["学校(school)"] == "'@报名学校"
    assert row["学号(student_id)"] == "'000123"
    assert row["实名来源(identity_source)"] == "报名时资料快照"
    assert row["实名采集时间(identity_captured_at)"]
    assert row["实名完整性(identity_completeness)"] == "完整"
    assert row["正式名次(rank)"] == "1"
    assert row["种子(seed)"] == ""
    assert row["分组(group_id)"] == "'=A组"
    assert row["赛事状态(contest_status)"] == "已结束"
    assert row["参赛状态(entry_status)"] == "已淘汰"
    assert row["成绩状态(result_status)"] == "正式成绩"
    assert row["阶段索引(stage_idx)"] == "0"
    assert row["阶段标识(stage_key)"] == "'=final"
    assert row["积分(points)"] == "9.5"
    assert row["胜(wins)"] == "3"
    assert row["平(draws)"] == "1"
    assert row["负(losses)"] == "2"
    assert row["净分(delta_total)"] == "88"
    assert row["奖项(awarded)"] == "'@一等奖"
    assert audits == [{
        "action": "contest_export",
        "result": "ok",
        "user": "orgv2",
        "target": contest["id"],
        "detail": "schema=2; rows=1; identity=required; legacy_fallback_rows=0",
    }]
    assert not any(
        private in audits[0]["detail"]
        for private in ("报名姓名", "013800000001", "报名学校", "000123")
    )
    unsupported = TestClient(app).get(
        f"/api/contests/{contest['id']}/export?format=csv&schema=3",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert unsupported.status_code == 400
    assert unsupported.headers["cache-control"] == "private, no-store, max-age=0"
    assert "content-disposition" not in unsupported.headers
    store.close()


def test_private_export_errors_are_never_cacheable(tmp_path):
    """401/400/404 私有导出错误与成功响应使用同一隐私缓存门禁。"""
    app = _app(tmp_path)
    store, org_tok, _user_tok, cid = _setup(app)
    client = TestClient(app)
    responses = (
        client.get(f"/api/contests/{cid}/export?format=csv&schema=2"),
        client.get(
            f"/api/contests/{cid}/export?format=json&schema=2",
            headers={"Authorization": f"Bearer {org_tok}"},
        ),
        client.get(
            "/api/contests/999999/export?format=csv&schema=2",
            headers={"Authorization": f"Bearer {org_tok}"},
        ),
        client.get(
            f"/api/contests/{cid}/export?format=csv&schema=invalid",
            headers={"Authorization": f"Bearer {org_tok}"},
        ),
    )
    assert [response.status_code for response in responses] == [401, 400, 404, 400]
    for response in responses:
        assert response.headers["cache-control"] == "private, no-store, max-age=0"
        assert response.headers["pragma"] == "no-cache"
        assert response.headers["vary"] == "Authorization, Cookie"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "content-disposition" not in response.headers
    store.close()


def test_public_official_results_strip_all_identity_fields(tmp_path, monkeypatch):
    """公开正式成绩 JSON/CSV 对 Store 行未来扩展也保持 PII 零输出。"""
    app = _app(tmp_path)
    store, _org_tok, _user_tok, cid = _setup(app)
    entrant = store.get_user_by_username("ent1")
    entry = store.get_entry(cid, entrant["id"])
    bot = store.get_bot(entry["bot_id"])
    store.upsert_official_result(
        cid, entry["id"], 1, stage_idx=0, points=3,
        bot_id=bot["id"], user_id=entrant["id"],
    )
    store.update_contest(cid, status="finished", official_results_ready=1)
    original = store.list_official_results

    def rows_with_private_sentinels(contest_id):
        rows = original(contest_id)
        for row in rows:
            row.update({
                "bot_name": "=PUBLIC-BOT",
                "owner_name": "+PUBLIC-OWNER",
                "awarded": "@PUBLIC-AWARD",
                "real_name": "PUBLIC-PII-NAME",
                "phone": "PUBLIC-PII-PHONE",
                "school": "PUBLIC-PII-SCHOOL",
                "student_id": "PUBLIC-PII-STUDENT",
                "real_name_snapshot": "PUBLIC-PII-SNAPSHOT",
                "identity_source": "registration_profile",
                "identity_captured_at": "PUBLIC-PII-TIME",
                "identity_complete": 1,
            })
        return rows

    monkeypatch.setattr(store, "list_official_results", rows_with_private_sentinels)
    client = TestClient(app)
    json_response = client.get(f"/api/contests/{cid}/official-results")
    csv_response = client.get(f"/api/contests/{cid}/official-results?format=csv")
    assert json_response.status_code == csv_response.status_code == 200
    combined = json_response.text + csv_response.text
    assert "PUBLIC-PII" not in combined
    for field in (
        "real_name", "phone", "school", "student_id", "real_name_snapshot",
        "identity_source", "identity_captured_at", "identity_complete",
    ):
        assert field not in json_response.json()["results"][0]
    assert csv_response.headers["x-content-type-options"] == "nosniff"
    csv_row = next(
        csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig")))
    )
    assert csv_row["bot_name"] == "'=PUBLIC-BOT"
    assert csv_row["owner_name"] == "'+PUBLIC-OWNER"
    assert csv_row["awarded"] == "'@PUBLIC-AWARD"
    store.close()
