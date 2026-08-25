"""管理员批量指派参赛者+Bot 测试（测试期 admin 派遣，正式版用户自己报名）。

覆盖 POST /api/admin/contests/{id}/entries/bulk：
- 显式 entries 列表模式
- assign_all 便捷模式（按 game_id 全选）
- 重复报名跳过 / bot 不可用跳过 / 游戏不匹配跳过
- 实名赛事 admin override 冻结快照并写无 PII 审计
- 管理员移除报名成功/失败审计
- 非 admin 403 / 赛事不存在 404
"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password, new_session_token, session_expires
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _store(tmp_path) -> Store:
    return Store(str(tmp_path / "aa.db"))


def _setup(tmp_path, game: str = "holdem"):
    """建 app + admin + 若干用户（各有该游戏 Bot）+ 赛事（draft）。"""
    s = _store(tmp_path)
    app = create_app(db_path=s.path)
    st = app.state.store
    admin = st.create_user("adminusr", "a@ex.com", hash_password("pw"), role="admin")
    users = []
    for i in range(6):
        u = st.create_user(f"usr{i}", f"u{i}@ex.com", hash_password("pw"))
        b = st.create_bot(u["id"], f"bot{i}", binary_path="/tmp/b", format="elf",
                          is_active=1, game_id=game)
        st.ensure_rating(b["id"])
        users.append((u, b))
    cid = st.create_contest("Cup", organizer_id=admin["id"], game_id=game)["id"]
    # admin session token
    tok = new_session_token()
    st.add_session(tok, admin["id"], session_expires())
    c = TestClient(app)
    return s, st, admin, users, cid, tok, c


def test_admin_assign_explicit_entries(tmp_path):
    s, st, admin, users, cid, tok, c = _setup(tmp_path)
    # 显式指派 user0/user1 的 bot
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk",
               headers={"Authorization": f"Bearer {tok}"},
               json={"entries": [{"user_id": users[0][0]["id"], "bot_id": users[0][1]["id"]},
                                 {"user_id": users[1][0]["id"], "bot_id": users[1][1]["id"]}]})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["added"] == 2
    assert body["total_entries"] == 2
    ents = st.list_entries(cid)
    assert len(ents) == 2
    s.close()


def test_admin_bot_picker_filters_owner_runnable_before_pagination(tmp_path):
    s, st, _admin, users, _cid, tok, client = _setup(tmp_path)
    owner = users[0][0]
    good = st.create_bot(
        owner["id"], "zz_good", binary_path="/tmp/good", format="elf",
        is_active=1, game_id="holdem",
    )
    stale = st.create_bot(
        owner["id"], "aa_stale", binary_path="/tmp/stale", format="elf",
        is_active=1, game_id="holdem",
    )
    blank = st.create_bot(
        owner["id"], "ab_blank", binary_path="", format="elf",
        is_active=1, game_id="holdem",
    )
    inactive = st.create_bot(
        owner["id"], "ac_inactive", binary_path="/tmp/inactive", format="elf",
        is_active=0, game_id="holdem",
    )
    st._conn.execute(
        "UPDATE bots SET protocol_version='stale_protocol' WHERE id=?",
        (stale["id"],),
    )
    st._conn.commit()
    headers = {"Authorization": f"Bearer {tok}"}

    first = client.get(
        "/api/admin/bots",
        params={
            "active": "true", "runnable": "true", "game_id": "holdem",
            "owner_id": owner["id"], "page": 1, "per_page": 1,
        },
        headers=headers,
    )
    second = client.get(
        "/api/admin/bots",
        params={
            "active": "true", "runnable": "true", "game_id": "holdem",
            "owner_id": owner["id"], "page": 2, "per_page": 1,
        },
        headers=headers,
    )
    rejected = client.get(
        "/api/admin/bots",
        params={
            "active": "true", "runnable": "false", "game_id": "holdem",
            "owner_id": owner["id"], "page": 1, "per_page": 10,
        },
        headers=headers,
    )
    implicit_active = client.get(
        "/api/admin/bots",
        params={
            "runnable": "true", "game_id": "holdem",
            "owner_id": owner["id"], "page": 1, "per_page": 10,
        },
        headers=headers,
    )
    normalized_zero_page = client.get(
        "/api/admin/bots",
        params={
            "active": "true", "runnable": "true", "game_id": "holdem",
            "owner_id": owner["id"], "page": 0, "per_page": 1,
        },
        headers=headers,
    )

    assert first.status_code == second.status_code == rejected.status_code == 200
    assert implicit_active.status_code == normalized_zero_page.status_code == 200
    assert first.headers["cache-control"].startswith("private, no-store")
    assert first.headers["pragma"] == "no-cache"
    assert first.json()["total"] == second.json()["total"] == 2
    assert {first.json()["bots"][0]["id"], second.json()["bots"][0]["id"]} == {
        users[0][1]["id"], good["id"],
    }
    assert rejected.json()["total"] == 2
    assert {bot["id"] for bot in rejected.json()["bots"]} == {
        stale["id"], blank["id"],
    }
    assert all(bot["runnable"] is False for bot in rejected.json()["bots"])
    assert all(bot["unsupported_reason"] for bot in rejected.json()["bots"])
    assert implicit_active.json()["total"] == 2
    assert {bot["id"] for bot in implicit_active.json()["bots"]} == {
        users[0][1]["id"], good["id"],
    }
    assert normalized_zero_page.json()["page"] == 1
    assert normalized_zero_page.json()["bots"] == first.json()["bots"]
    assert inactive["id"] not in {
        bot["id"]
        for bot in st.list_bots(active_only=False, runnable_only=True)
    }
    s.close()


@pytest.mark.parametrize("writer", ["single", "bulk"])
@pytest.mark.parametrize(
    ("drift", "error"),
    [("owner", "不属于"), ("bot_game", "游戏")],
)
def test_store_roster_writes_recheck_bot_binding_under_writer_lock(
    tmp_path, writer, drift, error
):
    """Both product roster writes fail closed on owner/game drift."""
    s, st, _admin, users, cid, _tok, _client = _setup(tmp_path)
    user, bot = users[0]
    st.update_contest(cid, status="open")
    if drift == "owner":
        st._conn.execute(
            "UPDATE bots SET owner_id=? WHERE id=?",
            (users[1][0]["id"], bot["id"]),
        )
        st._conn.commit()
    else:
        gomoku_contract = st.get_active_game_contract("gomoku")
        st._conn.execute(
            "UPDATE bots SET game_id='gomoku',protocol_version=? WHERE id=?",
            (gomoku_contract["protocol_version"], bot["id"]),
        )
        st._conn.commit()

    with pytest.raises(ValueError, match=error):
        if writer == "single":
            st.add_contest_entry_once(cid, user["id"], bot["id"])
        else:
            st.add_contest_roster_entries(cid, [(user["id"], bot["id"])])

    assert st.get_entry(cid, user["id"]) is None
    s.close()


def test_admin_user_picker_filters_active_and_searches_display_name(tmp_path):
    s, st, _admin, users, _cid, tok, client = _setup(tmp_path)
    visible = users[0][0]
    hidden = users[1][0]
    st.update_user(visible["id"], display_name="精确名册搜索")
    st.update_user(hidden["id"], display_name="精确名册搜索", is_active=0)
    headers = {"Authorization": f"Bearer {tok}"}

    active = client.get(
        "/api/admin/users",
        params={"active": "true", "q": "精确名册", "page": 1, "per_page": 1},
        headers=headers,
    )
    legacy_false = client.get(
        "/api/admin/users",
        params={"active": "false", "q": "精确名册"},
        headers=headers,
    )

    assert active.status_code == legacy_false.status_code == 200
    assert active.json()["total"] == 1
    assert active.json()["users"][0]["id"] == visible["id"]
    assert {user["id"] for user in legacy_false.json()["users"]} == {
        visible["id"], hidden["id"],
    }
    s.close()


def test_admin_assign_skips_currently_non_runnable_bot(tmp_path):
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    user, bot = users[0]
    st._conn.execute(
        "UPDATE bots SET protocol_version='stale_protocol' WHERE id=?",
        (bot["id"],),
    )
    st._conn.commit()

    response = client.post(
        f"/api/admin/contests/{cid}/entries/bulk",
        headers={"Authorization": f"Bearer {tok}"},
        json={"entries": [{"user_id": user["id"], "bot_id": bot["id"]}]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["added"] == 0
    assert response.json()["total_entries"] == 0
    assert response.json()["skipped"] == [
        f"bot {bot['id']} 当前不可运行，跳过"
    ]
    assert st.get_entry(cid, user["id"]) is None
    s.close()


def test_admin_entries_include_user_bot_and_game_labels(tmp_path):
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    st.add_entry(cid, users[0][0]["id"], users[0][1]["id"])

    response = client.get(
        f"/api/admin/contests/{cid}/entries",
        headers={"Authorization": f"Bearer {tok}"},
    )

    assert response.status_code == 200, response.text
    entry = response.json()["entries"][0]
    assert entry["username"] == "usr0"
    assert entry["bot_name"] == "bot0"
    assert entry["game_id"] == "holdem"
    s.close()


def test_admin_entries_identity_false_returns_non_pii_roster(tmp_path):
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    user, bot = users[0]
    st.update_user(
        user["id"], real_name="隐私姓名", phone="13800138000",
        school="隐私学校", student_id="PRIVATE001",
    )
    st.update_contest(cid, require_real_name=1)
    st.add_entry(cid, user["id"], bot["id"])

    response = client.get(
        f"/api/admin/contests/{cid}/entries?identity=false",
        headers={"Authorization": f"Bearer {tok}"},
    )

    assert response.status_code == 200, response.text
    entry = response.json()["entries"][0]
    assert entry["user_id"] == user["id"]
    assert entry["bot_id"] == bot["id"]
    assert not {
        "real_name", "phone", "school", "student_id",
        "real_name_snapshot", "phone_snapshot", "school_snapshot",
        "student_id_snapshot", "identity_captured_at", "identity_source",
    }.intersection(entry)
    assert all(
        private not in response.text
        for private in ("隐私姓名", "13800138000", "隐私学校", "PRIVATE001")
    )
    assert response.headers["cache-control"].startswith("private, no-store")
    s.close()


def test_admin_delete_entry_audits_success_and_missing(tmp_path, monkeypatch):
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    user_id = users[0][0]["id"]
    st.add_entry(cid, user_id, users[0][1]["id"])
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append({"action": action, **fields}),
    )

    response = client.delete(
        f"/api/admin/contests/{cid}/entries/{user_id}",
        headers={"Authorization": f"Bearer {tok}"},
    )
    missing = client.delete(
        f"/api/admin/contests/{cid}/entries/{user_id}",
        headers={"Authorization": f"Bearer {tok}"},
    )

    assert response.status_code == 200, response.text
    assert missing.status_code == 404, missing.text
    assert audits[0] == {
        "action": "admin_delete_contest_entry",
        "result": "ok",
        "user": "adminusr",
        "target": cid,
        "detail": f"user_id={user_id}",
    }
    assert audits[1]["action"] == "admin_delete_contest_entry"
    assert audits[1]["result"] == "fail"
    assert "报名记录不存在" in audits[1]["detail"]
    s.close()


def test_admin_assign_all_by_game(tmp_path):
    s, st, admin, users, cid, tok, c = _setup(tmp_path)
    # assign_all 模式：自动找所有 holdem bot
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk",
               headers={"Authorization": f"Bearer {tok}"},
               json={"assign_all": True, "game_id": "holdem"})
    assert r.status_code == 200, r.text
    body = r.json()
    # 6 个用户各 1 个 bot → 6 条
    assert body["added"] == 6
    assert body["total_entries"] == 6
    s.close()


def test_admin_assign_all_skips_disabled_owner(tmp_path):
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    disabled_user, _disabled_bot = users[0]
    st.update_user(disabled_user["id"], is_active=0)

    response = client.post(
        f"/api/admin/contests/{cid}/entries/bulk",
        headers={"Authorization": f"Bearer {tok}"},
        json={"assign_all": True, "game_id": "holdem"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["added"] == 5
    assert response.json()["total_entries"] == 5
    assert response.json()["skipped"] == [
        f"user {disabled_user['id']} 已停用，跳过"
    ]
    assert st.get_entry(cid, disabled_user["id"]) is None
    s.close()


def test_admin_bulk_real_name_gate_reports_users_freezes_and_audits(
    tmp_path, monkeypatch
):
    """实名赛批量代报名逐项跳过缺资料用户，并返回明确数量/用户。"""
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    st.update_contest(cid, require_real_name=1)
    complete_user = users[0][0]
    incomplete_user = users[1][0]
    st.update_user(
        complete_user["id"], real_name="批量实名", phone="13800138000",
        school="批量大学", student_id="BULK001",
    )
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )

    response = client.post(
        f"/api/admin/contests/{cid}/entries/bulk",
        headers={"Authorization": f"Bearer {tok}"},
        json={"entries": [
            {"user_id": complete_user["id"], "bot_id": users[0][1]["id"]},
            {"user_id": incomplete_user["id"], "bot_id": users[1][1]["id"]},
        ]},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["added"] == 1
    assert body["identity_incomplete_count"] == 1
    assert body["identity_incomplete_users"] == [incomplete_user["id"]]
    assert any(
        f"user {incomplete_user['id']} 实名信息不完整" in item
        for item in body["skipped"]
    )
    stored = st.get_entry(cid, complete_user["id"])
    assert stored["identity_source"] == "registration_profile"
    assert stored["real_name_snapshot"] == "批量实名"
    assert st.get_entry(cid, incomplete_user["id"]) is None
    assert audits == [
        {
            "action": "admin_assign_entries",
            "result": "ok",
            "user": "adminusr",
            "target": cid,
            "detail": (
                "real_name_override=1; requested=2; added=1; skipped=1"
            ),
        }
    ]
    assert all(
        private not in repr(audits)
        for private in ("批量实名", "13800138000", "批量大学", "BULK001")
    )
    s.close()


def test_admin_bulk_audit_uses_identity_gate_at_store_commit(
    tmp_path, monkeypatch
):
    """A 0→1 toggle before Store BEGIN is audited as an actual PII override."""
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    user, bot = users[0]
    st.update_user(
        user["id"], real_name="竞态实名", phone="01001234567",
        school="竞态学校", student_id="000042",
    )
    second = Store(st.path)
    original_add = st.add_contest_roster_entries
    store_boundary = threading.Barrier(2)
    release_store = threading.Event()
    audits: list[dict] = []

    def paused_add(*args, **kwargs):
        store_boundary.wait(timeout=5)
        if not release_store.wait(timeout=5):
            raise TimeoutError("admin roster Store release timed out")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(st, "add_contest_roster_entries", paused_add)
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                f"/api/admin/contests/{cid}/entries/bulk",
                headers={"Authorization": f"Bearer {tok}"},
                json={"entries": [{
                    "user_id": user["id"], "bot_id": bot["id"]
                }]},
            )
            store_boundary.wait(timeout=5)
            try:
                second.update_contest(cid, require_real_name=1)
            finally:
                release_store.set()
            response = future.result(timeout=10)

        assert response.status_code == 200, response.text
        assert "_identity_required_at_commit" not in response.json()
        assert int(st.get_contest(cid)["require_real_name"]) == 1
        entry = st.get_entry(cid, user["id"])
        assert entry["identity_source"] == "registration_profile"
        assert entry["real_name_snapshot"] == "竞态实名"
        assert audits == [{
            "action": "admin_assign_entries",
            "result": "ok",
            "user": "adminusr",
            "target": cid,
            "detail": (
                "real_name_override=1; requested=1; added=1; skipped=0"
            ),
        }]
        assert all(
            private not in repr(audits)
            for private in ("竞态实名", "01001234567", "竞态学校", "000042")
        )
    finally:
        release_store.set()
        second.close()
        s.close()


def test_admin_bulk_failed_audit_uses_identity_gate_at_store_commit(
    tmp_path, monkeypatch
):
    """A failed 0→1 override is audited from the Store transaction gate."""
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    user, bot = users[0]  # Intentionally has no real-name profile.
    second = Store(st.path)
    original_add = st.add_contest_roster_entries
    store_boundary = threading.Barrier(2)
    release_store = threading.Event()
    audits: list[dict] = []

    def paused_add(*args, **kwargs):
        store_boundary.wait(timeout=5)
        if not release_store.wait(timeout=5):
            raise TimeoutError("failed admin roster Store release timed out")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(st, "add_contest_roster_entries", paused_add)
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                f"/api/admin/contests/{cid}/entries/bulk",
                headers={"Authorization": f"Bearer {tok}"},
                json={"entries": [{
                    "user_id": user["id"], "bot_id": bot["id"]
                }]},
            )
            store_boundary.wait(timeout=5)
            try:
                second.update_contest(cid, require_real_name=1)
            finally:
                release_store.set()
            response = future.result(timeout=10)

        assert response.status_code == 400, response.text
        assert "实名" in response.text
        assert int(st.get_contest(cid)["require_real_name"]) == 1
        assert st.list_contest_entries(cid) == []
        assert audits == [{
            "action": "admin_assign_entries",
            "result": "fail",
            "user": "adminusr",
            "target": cid,
            "detail": (
                "real_name_override=1; requested=1; "
                "reason=validation_failed"
            ),
        }]
    finally:
        release_store.set()
        second.close()
        s.close()


def test_store_bulk_rechecks_identity_atomically(tmp_path):
    """绕过 manager 的批量写也会因缺实名整批回滚，不留下半份名册。"""
    s, st, _admin, users, cid, _tok, _client = _setup(tmp_path)
    st.update_contest(cid, require_real_name=1)
    st.update_user(
        users[0][0]["id"], real_name="完整用户", phone="13800138000",
        school="完整学校", student_id="COMPLETE001",
    )
    with pytest.raises(ValueError, match="实名"):
        st.add_contest_roster_entries(
            cid,
            [
                (users[0][0]["id"], users[0][1]["id"]),
                (users[1][0]["id"], users[1][1]["id"]),
            ],
            allow_real_name_override=True,
        )
    assert st.list_contest_entries(cid) == []
    s.close()


def test_admin_assign_skips_duplicates(tmp_path):
    s, st, admin, users, cid, tok, c = _setup(tmp_path)
    # 先指派 user0
    st.add_entry(cid, users[0][0]["id"], users[0][1]["id"])
    # 再批量指派含 user0 → 应跳过 user0
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk",
               headers={"Authorization": f"Bearer {tok}"},
               json={"entries": [{"user_id": users[0][0]["id"], "bot_id": users[0][1]["id"]},
                                 {"user_id": users[1][0]["id"], "bot_id": users[1][1]["id"]}]})
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 1  # user0 已存在跳过
    assert any("已报名" in sk for sk in body["skipped"])
    s.close()


def test_admin_assign_skips_game_mismatch(tmp_path):
    s, st, admin, users, cid, tok, c = _setup(tmp_path, game="holdem")
    # 建一个 gomoku bot，指派进 holdem 赛事 → 应跳过
    ug = st.create_user("gomokuusr", "ug@ex.com", hash_password("pw"))
    bg = st.create_bot(ug["id"], "gobot", binary_path="/tmp/g", format="elf",
                       is_active=1, game_id="gomoku")
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk",
               headers={"Authorization": f"Bearer {tok}"},
               json={"entries": [{"user_id": ug["id"], "bot_id": bg["id"]}]})
    assert r.status_code == 200
    body = r.json()
    assert body["added"] == 0
    assert any("游戏" in sk for sk in body["skipped"])
    s.close()


def test_admin_assign_requires_admin(tmp_path):
    s, st, admin, users, cid, _atok, c = _setup(tmp_path)
    # 普通用户 token → 403
    utok = new_session_token()
    st.add_session(utok, users[0][0]["id"], session_expires())
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk",
               headers={"Authorization": f"Bearer {utok}"},
               json={"assign_all": True, "game_id": "holdem"})
    assert r.status_code == 403
    s.close()


def test_admin_assign_contest_not_found(tmp_path, monkeypatch):
    s, st, admin, users, cid, tok, c = _setup(tmp_path)
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )
    r = c.post("/api/admin/contests/99999/entries/bulk",
               headers={"Authorization": f"Bearer {tok}"},
               json={"assign_all": True, "game_id": "holdem"})
    assert r.status_code == 404
    assert audits == [{
        "action": "admin_assign_entries",
        "result": "fail",
        "user": "adminusr",
        "target": 99999,
        "detail": "real_name_override=0; reason=contest_missing",
    }]
    s.close()


def test_admin_assign_rejects_missing_or_non_integer_ids(tmp_path, monkeypatch):
    s, _st, _admin, _users, cid, tok, c = _setup(tmp_path)
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )
    for entry in ({"user_id": 1}, {"user_id": "x", "bot_id": 2}):
        r = c.post(
            f"/api/admin/contests/{cid}/entries/bulk",
            headers={"Authorization": f"Bearer {tok}"},
            json={"entries": [entry]},
        )
        assert r.status_code == 400, r.text
        assert "必须是整数" in r.text
    assert len(audits) == 2
    assert all(
        audit == {
            "action": "admin_assign_entries",
            "result": "fail",
            "user": "adminusr",
            "target": cid,
            "detail": "real_name_override=0; reason=invalid_ids",
        }
        for audit in audits
    )
    s.close()


def test_admin_assign_rejects_empty_ambiguous_and_extra_payloads(
    tmp_path, monkeypatch
):
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )
    headers = {"Authorization": f"Bearer {tok}"}
    for payload in (
        {},
        {"entries": []},
        {
            "assign_all": True,
            "game_id": "holdem",
            "entries": [{
                "user_id": users[0][0]["id"], "bot_id": users[0][1]["id"],
            }],
        },
    ):
        response = client.post(
            f"/api/admin/contests/{cid}/entries/bulk",
            headers=headers,
            json=payload,
        )
        assert response.status_code == 400, response.text
        assert "必须且只能选择一种" in response.text

    nested_extra = client.post(
        f"/api/admin/contests/{cid}/entries/bulk",
        headers=headers,
        json={"entries": [{
            "user_id": users[0][0]["id"], "bot_id": users[0][1]["id"],
            "role": "ignored-before",
        }]},
    )
    outer_extra = client.post(
        f"/api/admin/contests/{cid}/entries/bulk",
        headers=headers,
        json={
            "entries": [{
                "user_id": users[0][0]["id"], "bot_id": users[0][1]["id"],
            }],
            "unexpected": True,
        },
    )

    assert nested_extra.status_code == 400, nested_extra.text
    assert outer_extra.status_code == 400, outer_extra.text
    assert "不支持的字段" in outer_extra.text
    assert st.list_entries(cid) == []
    assert [audit["detail"] for audit in audits] == [
        "real_name_override=0; reason=invalid_mode",
        "real_name_override=0; reason=invalid_mode",
        "real_name_override=0; reason=invalid_mode",
        "real_name_override=0; reason=invalid_ids",
        "real_name_override=0; reason=invalid_fields; count=1",
    ]
    s.close()


def test_common_bulk_manager_skips_inactive_user(tmp_path):
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    user, bot = users[0]
    st.update_user(user["id"], is_active=0)

    response = client.post(
        f"/api/contests/{cid}/entries/bulk",
        headers={"Authorization": f"Bearer {tok}"},
        json={"entries": [{"user_id": user["id"], "bot_id": bot["id"]}]},
    )

    assert response.status_code == 200, response.text
    assert response.json()["added"] == 0
    assert response.json()["skipped"] == [f"user {user['id']} 已停用，跳过"]
    assert st.get_entry(cid, user["id"]) is None
    s.close()


def test_admin_bulk_store_rechecks_active_user_and_commit_identity_gate(
    tmp_path, monkeypatch
):
    """A disable after Manager validation is rejected at Store BEGIN."""
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    user, bot = users[0]
    second = Store(st.path)
    original_add = st.add_contest_roster_entries
    store_boundary = threading.Barrier(2)
    release_store = threading.Event()
    audits: list[dict] = []

    def paused_add(*args, **kwargs):
        store_boundary.wait(timeout=5)
        if not release_store.wait(timeout=5):
            raise TimeoutError("active-user Store release timed out")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(st, "add_contest_roster_entries", paused_add)
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                f"/api/admin/contests/{cid}/entries/bulk",
                headers={"Authorization": f"Bearer {tok}"},
                json={"entries": [{
                    "user_id": user["id"], "bot_id": bot["id"],
                }]},
            )
            store_boundary.wait(timeout=5)
            try:
                second.update_contest(cid, require_real_name=1)
                second.update_user(user["id"], is_active=0)
            finally:
                release_store.set()
            response = future.result(timeout=10)

        assert response.status_code == 400, response.text
        assert "已停用" in response.text
        assert st.get_entry(cid, user["id"]) is None
        assert audits == [{
            "action": "admin_assign_entries",
            "result": "fail",
            "user": "adminusr",
            "target": cid,
            "detail": (
                "real_name_override=1; requested=1; "
                "reason=validation_failed"
            ),
        }]
    finally:
        release_store.set()
        second.close()
        s.close()


def test_admin_bulk_store_rechecks_runnable_bot_and_commit_identity_gate(
    tmp_path, monkeypatch
):
    """A protocol drift after Manager validation is rejected at Store BEGIN."""
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    user, bot = users[0]
    second = Store(st.path)
    original_add = st.add_contest_roster_entries
    store_boundary = threading.Barrier(2)
    release_store = threading.Event()
    audits: list[dict] = []

    def paused_add(*args, **kwargs):
        store_boundary.wait(timeout=5)
        if not release_store.wait(timeout=5):
            raise TimeoutError("runnable-Bot Store release timed out")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(st, "add_contest_roster_entries", paused_add)
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                f"/api/admin/contests/{cid}/entries/bulk",
                headers={"Authorization": f"Bearer {tok}"},
                json={"entries": [{
                    "user_id": user["id"], "bot_id": bot["id"],
                }]},
            )
            store_boundary.wait(timeout=5)
            try:
                second.update_contest(cid, require_real_name=1)
                second._conn.execute(
                    "UPDATE bots SET protocol_version='stale_protocol' WHERE id=?",
                    (bot["id"],),
                )
                second._conn.commit()
            finally:
                release_store.set()
            response = future.result(timeout=10)

        assert response.status_code == 400, response.text
        assert "不可运行" in response.text
        assert st.get_entry(cid, user["id"]) is None
        assert audits == [{
            "action": "admin_assign_entries",
            "result": "fail",
            "user": "adminusr",
            "target": cid,
            "detail": (
                "real_name_override=1; requested=1; "
                "reason=validation_failed"
            ),
        }]
    finally:
        release_store.set()
        second.close()
        s.close()


@pytest.mark.parametrize(
    ("drift", "error"),
    [("owner", "不属于"), ("bot_game", "游戏")],
)
def test_admin_bulk_store_rechecks_binding_after_manager_precheck(
    tmp_path, monkeypatch, drift, error
):
    """A second connection cannot invalidate Manager owner/game validation."""
    s, st, _admin, users, cid, tok, client = _setup(tmp_path)
    user, bot = users[0]
    second = Store(st.path)
    gomoku_protocol = st.get_active_game_contract("gomoku")["protocol_version"]
    original_add = st.add_contest_roster_entries
    store_boundary = threading.Barrier(2)
    release_store = threading.Event()

    def paused_add(*args, **kwargs):
        store_boundary.wait(timeout=5)
        if not release_store.wait(timeout=5):
            raise TimeoutError("Bot binding Store release timed out")
        return original_add(*args, **kwargs)

    monkeypatch.setattr(st, "add_contest_roster_entries", paused_add)

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(
                client.post,
                f"/api/admin/contests/{cid}/entries/bulk",
                headers={"Authorization": f"Bearer {tok}"},
                json={"entries": [{
                    "user_id": user["id"], "bot_id": bot["id"],
                }]},
            )
            store_boundary.wait(timeout=5)
            try:
                if drift == "owner":
                    second._conn.execute(
                        "UPDATE bots SET owner_id=? WHERE id=?",
                        (users[1][0]["id"], bot["id"]),
                    )
                    second._conn.commit()
                else:
                    second._conn.execute(
                        "UPDATE bots SET game_id='gomoku',protocol_version=? "
                        "WHERE id=?",
                        (gomoku_protocol, bot["id"]),
                    )
                    second._conn.commit()
            finally:
                release_store.set()
            response = future.result(timeout=10)

        assert response.status_code == 400, response.text
        assert error in response.text
        assert st.get_entry(cid, user["id"]) is None
    finally:
        release_store.set()
        second.close()
        s.close()
