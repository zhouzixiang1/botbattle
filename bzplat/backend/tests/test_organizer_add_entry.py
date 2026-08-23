"""组织者单条加人校验测试（审计 P1：organizer_add_entry 缺校验→500）。

覆盖 POST /api/contests/{id}/entries：
- 缺 user_id/bot_id → 400（原 int(None) → TypeError → 500）
- 非整数 → 400
- user 不存在 → 400
- 实名赛普通组织者单条/批量代报名均 403、零写入并审计
- admin 通过同一组织者路由显式 override，冻结快照并审计
- 正常加人 → 200
- 重复报名 → 400
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import ContestRealNameRosterForbidden, Store


def _app(tmp_path):
    db = str(tmp_path / "oe.db")
    app = create_app(db_path=db)
    st = app.state.store
    org = st.create_user("organizer", "o@ex.com", hash_password("pw"), role="organizer")
    st.update_user(org["id"], email_verified=1)
    u = st.create_user("player1", "p@ex.com", hash_password("pw"))
    st.update_user(u["id"], email_verified=1)
    b = st.create_bot(u["id"], "p1bot", binary_path="/tmp/b", format="elf", game_id="holdem")
    cid = st.create_contest("Cup", organizer_id=org["id"], game_id="holdem")["id"]
    _, tok = app.state.auth.authenticate("organizer", "pw")
    c = TestClient(app)
    return c, cid, u, b, tok


def test_missing_user_id_returns_400(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400


def test_missing_bot_id_returns_400(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": u["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400


def test_non_integer_ids_returns_400(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": "abc", "bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400


def test_nonexistent_user_returns_400(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": 99999, "bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400


def test_valid_entry_succeeds(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": u["id"], "bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_real_name_proxy_is_forbidden_for_organizer_and_audited(
    tmp_path, monkeypatch
):
    c, cid, user, bot, tok = _app(tmp_path)
    store = c.app.state.store
    store.update_contest(cid, require_real_name=1)
    store.update_user(
        user["id"], real_name="受害者姓名", phone="01001234567",
        school="受害者学校", student_id="000042",
    )
    headers = {"Authorization": f"Bearer {tok}"}
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )

    single = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": user["id"], "bot_id": bot["id"]},
        headers=headers,
    )
    bulk = c.post(
        f"/api/contests/{cid}/entries/bulk",
        json={"assign_all": True, "game_id": "holdem"},
        headers=headers,
    )

    for response in (single, bulk):
        assert response.status_code == 403, response.text
        assert response.json()["detail"] == (
            "实名赛事仅允许参赛者本人报名，组织者不可代报名"
        )
    assert store.list_contest_entries(cid) == []
    assert len(audits) == 2
    assert {audit["result"] for audit in audits} == {"fail"}
    assert {audit["action"] for audit in audits} == {
        "contest_real_name_roster_override"
    }
    assert {audit["target"] for audit in audits} == {cid}
    serialized_audit = repr(audits)
    assert all(
        private not in serialized_audit
        for private in ("受害者姓名", "01001234567", "受害者学校", "000042")
    )


def test_real_name_proxy_is_forbidden_at_manager_and_store_boundaries(tmp_path):
    c, cid, user, bot, _tok = _app(tmp_path)
    store = c.app.state.store
    store.update_contest(cid, require_real_name=1)
    store.update_user(
        user["id"], real_name="完整姓名", phone="13800138000",
        school="完整学校", student_id="COMPLETE001",
    )
    manager = c.app.state.contest_manager

    with pytest.raises(ContestRealNameRosterForbidden):
        asyncio.run(manager.add_roster_entry(cid, user["id"], bot["id"]))
    with pytest.raises(ContestRealNameRosterForbidden):
        asyncio.run(
            manager.assign_roster_entries(cid, [(user["id"], bot["id"])])
        )
    with pytest.raises(ContestRealNameRosterForbidden):
        store.add_contest_roster_entries(cid, [(user["id"], bot["id"])])
    assert store.list_contest_entries(cid) == []


@pytest.mark.parametrize("mode", ["single", "bulk"])
def test_admin_real_name_override_on_organizer_routes_is_audited(
    tmp_path, monkeypatch, mode
):
    c, cid, user, bot, tok = _app(tmp_path)
    store = c.app.state.store
    organizer = store.get_user_by_username("organizer")
    store.update_user(organizer["id"], role="admin")
    store.update_user(
        user["id"], real_name="管理员补录姓名", phone="01001234567",
        school="管理员补录学校", student_id="000042",
    )
    store.update_contest(cid, require_real_name=1)
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )
    headers = {"Authorization": f"Bearer {tok}"}

    if mode == "single":
        response = c.post(
            f"/api/contests/{cid}/entries",
            json={"user_id": user["id"], "bot_id": bot["id"]},
            headers=headers,
        )
    else:
        response = c.post(
            f"/api/contests/{cid}/entries/bulk",
            json={"entries": [{"user_id": user["id"], "bot_id": bot["id"]}]},
            headers=headers,
        )

    assert response.status_code == 200, response.text
    stored = store.get_entry(cid, user["id"])
    assert stored["real_name_snapshot"] == "管理员补录姓名"
    assert stored["phone_snapshot"] == "01001234567"
    assert stored["student_id_snapshot"] == "000042"
    assert stored["identity_source"] == "registration_profile"
    assert stored["identity_captured_at"] == stored["registered_at"]
    assert len(audits) == 1
    assert audits[0]["action"] == "contest_real_name_roster_override"
    assert audits[0]["result"] == "ok"
    assert audits[0]["user"] == "organizer"
    assert audits[0]["target"] == cid
    assert f"mode={mode}" in audits[0]["detail"]
    serialized_audit = repr(audits)
    assert all(
        private not in serialized_audit
        for private in ("管理员补录姓名", "01001234567", "管理员补录学校", "000042")
    )


@pytest.mark.parametrize(
    ("path_suffix", "payload_factory", "reason"),
    [
        ("entries", lambda user, bot: {"bot_id": bot["id"]}, "missing_ids"),
        (
            "entries",
            lambda user, bot: {"user_id": "bad", "bot_id": bot["id"]},
            "invalid_ids",
        ),
        (
            "entries/bulk",
            lambda user, bot: {"entries": [{"user_id": user["id"]}]},
            "invalid_ids",
        ),
        (
            "entries/bulk",
            lambda user, bot: {"assign_all": True, "game_id": "gomoku"},
            "game_mismatch",
        ),
        (
            "entries/bulk",
            lambda user, bot: {"assign_all": True, "game_id": "unknown"},
            "invalid_game",
        ),
    ],
)
def test_admin_real_name_override_invalid_common_route_requests_are_audited(
    tmp_path, monkeypatch, path_suffix, payload_factory, reason
):
    c, cid, user, bot, tok = _app(tmp_path)
    store = c.app.state.store
    organizer = store.get_user_by_username("organizer")
    store.update_user(organizer["id"], role="admin")
    store.update_contest(cid, require_real_name=1)
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append(
            {"action": action, **fields}
        ),
    )

    response = c.post(
        f"/api/contests/{cid}/{path_suffix}",
        json=payload_factory(user, bot),
        headers={"Authorization": f"Bearer {tok}"},
    )

    assert response.status_code == 400, response.text
    assert store.list_contest_entries(cid) == []
    assert len(audits) == 1
    assert audits[0]["action"] == "contest_real_name_roster_override"
    assert audits[0]["result"] == "fail"
    assert audits[0]["user"] == "organizer"
    assert audits[0]["target"] == cid
    assert f"reason={reason}" in audits[0]["detail"]


def test_duplicate_entry_returns_400(tmp_path):
    c, cid, u, b, tok = _app(tmp_path)
    # 第一次成功
    c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": u["id"], "bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    # 重复 → 400
    r = c.post(
        f"/api/contests/{cid}/entries",
        json={"user_id": u["id"], "bot_id": b["id"]},
        headers={"Authorization": f"Bearer {tok}"},
    )
    assert r.status_code == 400
