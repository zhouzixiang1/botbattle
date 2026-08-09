"""管理员赛事状态接口必须复用 ContestManager 生命周期，而非直接改状态列。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password, new_session_token, session_expires
from bzplat.backend.main import create_app


def _setup(tmp_path):
    app = create_app(db_path=str(tmp_path / "admin-contest-status.db"))
    store = app.state.store
    admin = store.create_user(
        "statusadmin", "statusadmin@example.com", hash_password("pw123456"), role="admin"
    )
    store.update_user(admin["id"], email_verified=1)
    token = new_session_token()
    store.add_session(token, admin["id"], session_expires())
    contest = store.create_contest("状态回归赛", organizer_id=admin["id"], game_id="holdem")
    return app, store, contest["id"], {"Authorization": f"Bearer {token}"}


def test_admin_finish_uses_manager_and_returns_success(tmp_path):
    """旧实现先写 finished，随后引用未定义 admin 而 500；现在须完整收尾并返回 200。"""
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(contest_id, status="running")

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}", json={"status": "finished"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["contest"]["status"] == "finished"
    saved = store.get_contest(contest_id)
    assert saved["status"] == "finished"
    assert saved["ends_at"]


def test_admin_terminal_contest_cannot_be_cancelled(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(contest_id, status="finished")

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}", json={"status": "cancelled"}, headers=headers
    )

    assert response.status_code == 400
    assert store.get_contest(contest_id)["status"] == "finished"


def test_admin_cancelled_terminal_contest_cannot_be_reopened(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(contest_id, status="cancelled")

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}", json={"status": "open"}, headers=headers
    )

    assert response.status_code == 400
    assert "终态" in response.json()["detail"] or "不支持" in response.json()["detail"]
    assert store.get_contest(contest_id)["status"] == "cancelled"


def test_admin_open_transition_uses_registration_lifecycle(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}", json={"status": "open"}, headers=headers
    )

    assert response.status_code == 200, response.text
    contest = store.get_contest(contest_id)
    assert contest["status"] == "open"
    assert contest["registration_opens_at"]


def test_admin_rejects_combined_status_and_field_patch(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}",
        json={"status": "open", "title": "不应部分写入"},
        headers=headers,
    )

    assert response.status_code == 400
    contest = store.get_contest(contest_id)
    assert contest["status"] == "draft"
    assert contest["title"] == "状态回归赛"


def test_admin_published_to_running_delegates_to_manager(tmp_path, monkeypatch):
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(contest_id, status="published")
    called: list[int] = []

    async def fake_start(cid: int):
        called.append(cid)
        return store.update_contest(cid, status="running")

    monkeypatch.setattr(app.state.contest_manager, "start", fake_start)
    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}", json={"status": "running"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert called == [contest_id]
    assert store.get_contest(contest_id)["status"] == "running"


def test_admin_can_cancel_prestart_and_action_is_audited(tmp_path, monkeypatch):
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(contest_id, status="open")
    calls: list[dict] = []

    def record_audit(_request, action, **fields):
        calls.append({"action": action, **fields})

    monkeypatch.setattr("bzplat.backend.api_routes.audit_log", record_audit)
    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}", json={"status": "cancelled"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert store.get_contest(contest_id)["status"] == "cancelled"
    assert calls == [{
        "action": "admin_patch_contest_status", "result": "ok",
        "user": "statusadmin", "target": contest_id,
        "detail": "status=open->cancelled",
    }]


def test_admin_partial_time_patch_merges_existing_and_has_zero_partial_write(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(
        contest_id,
        registration_opens_at="2099-01-01T00:00:00",
        registration_closes_at="2099-01-02T00:00:00",
        starts_at="2099-01-03T00:00:00",
    )

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}",
        json={
            "title": "不得部分写入",
            "registration_closes_at": "2099-01-04T00:00:00",
        },
        headers=headers,
    )

    assert response.status_code == 400
    assert "比赛开始时间不能早于报名截止时间" in response.json()["detail"]
    saved = store.get_contest(contest_id)
    assert saved["title"] == "状态回归赛"
    assert saved["registration_closes_at"] == "2099-01-02T00:00:00"


def test_admin_time_patch_can_clear_or_set_equal_optional_times(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(
        contest_id,
        registration_opens_at="2099-01-01T00:00:00",
        registration_closes_at="2099-01-02T00:00:00",
        starts_at="2099-01-03T00:00:00",
    )
    client = TestClient(app)

    cleared = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"registration_opens_at": None},
        headers=headers,
    )
    assert cleared.status_code == 200, cleared.text
    assert cleared.json()["contest"]["registration_opens_at"] is None

    timestamp = "2099-02-01T00:00:00"
    equal = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={
            "registration_opens_at": timestamp,
            "registration_closes_at": timestamp,
            "starts_at": timestamp,
        },
        headers=headers,
    )
    assert equal.status_code == 200, equal.text

    # 未提交 starts_at 时保留原值；显式 null 才恢复“手动开赛”。这一区分
    # 是管理端完整表单与其他 partial PATCH 调用共同依赖的 API 契约。
    omitted = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"title": "保留自动开赛时间"},
        headers=headers,
    )
    assert omitted.status_code == 200, omitted.text
    assert omitted.json()["contest"]["starts_at"] == timestamp

    manual = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"starts_at": None},
        headers=headers,
    )
    assert manual.status_code == 200, manual.text
    assert manual.json()["contest"]["starts_at"] is None
    assert store.get_contest(contest_id)["starts_at"] is None


def test_dirty_legacy_contest_is_readable_but_invalid_time_patch_is_clear_400(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    # 模拟修复前已存在的倒挂数据；读路径不得因新校验而迁移/隐藏它。
    store._conn.execute(
        "UPDATE contests SET registration_opens_at=?, registration_closes_at=?, starts_at=? "
        "WHERE id=?",
        (
            "2099-01-03T00:00:00",
            "2099-01-02T00:00:00",
            "2099-01-01T00:00:00",
            contest_id,
        ),
    )
    store._conn.commit()
    client = TestClient(app)

    listing = client.get("/api/admin/contests", headers=headers)
    assert listing.status_code == 200
    assert any(row["id"] == contest_id for row in listing.json()["contests"])

    rejected = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"starts_at": "2099-01-04T00:00:00"},
        headers=headers,
    )
    assert rejected.status_code == 400
    assert "报名截止时间不能早于报名开放时间" in rejected.json()["detail"]

    repaired = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={
            "registration_opens_at": "2099-01-01T00:00:00",
            "registration_closes_at": "2099-01-02T00:00:00",
            "starts_at": "2099-01-03T00:00:00",
        },
        headers=headers,
    )
    assert repaired.status_code == 200, repaired.text


def test_contest_create_time_validation_is_audited_and_does_not_insert(tmp_path, monkeypatch):
    app, store, _contest_id, headers = _setup(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: calls.append({"action": action, **fields}),
    )
    before = len(store.list_contests())

    response = TestClient(app).post(
        "/api/contests",
        json={
            "title": "倒挂时间赛",
            "registration_closes_at": "2099-01-03T00:00:00",
            "starts_at": "2099-01-02T00:00:00",
        },
        headers=headers,
    )

    assert response.status_code == 400
    assert len(store.list_contests()) == before
    assert calls[0]["action"] == "contest_create"
    assert calls[0]["result"] == "fail"


def test_admin_time_patch_success_and_failure_are_audited(tmp_path, monkeypatch):
    app, _store, contest_id, headers = _setup(tmp_path)
    calls: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: calls.append({"action": action, **fields}),
    )
    client = TestClient(app)
    timestamp = "2099-01-01T00:00:00"
    assert client.patch(
        f"/api/admin/contests/{contest_id}",
        json={
            "registration_opens_at": timestamp,
            "registration_closes_at": timestamp,
            "starts_at": timestamp,
        },
        headers=headers,
    ).status_code == 200
    assert client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"registration_closes_at": "2099-01-02T00:00:00"},
        headers=headers,
    ).status_code == 400

    actions = [call for call in calls if call["action"] == "admin_patch_contest_fields"]
    assert [call["result"] for call in actions] == ["ok", "fail"]


@pytest.mark.parametrize("status", ["running", "rest"])
def test_admin_delete_rejects_active_contest_states(tmp_path, status):
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(contest_id, status=status)

    response = TestClient(app).delete(
        f"/api/admin/contests/{contest_id}", headers=headers
    )

    assert response.status_code == 409
    assert store.get_contest(contest_id)["status"] == status


def test_admin_delete_rejects_finished_and_preserves_official_result_container(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(
        contest_id, status="finished", official_results_ready=1,
    )

    response = TestClient(app).delete(
        f"/api/admin/contests/{contest_id}", headers=headers
    )

    assert response.status_code == 409
    assert "正式赛果" in response.json()["detail"]
    saved = store.get_contest(contest_id)
    assert saved["status"] == "finished"
    assert saved["official_results_ready"] == 1


def test_admin_delete_rejects_legacy_cancelled_contest_with_official_results(tmp_path):
    """历史上被误标 cancelled 的正式赛事也不得被删除。"""
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(
        contest_id, status="cancelled", official_results_ready=1,
    )

    response = TestClient(app).delete(
        f"/api/admin/contests/{contest_id}", headers=headers
    )

    assert response.status_code == 409
    assert store.get_contest(contest_id)["status"] == "cancelled"


def test_admin_delete_published_cancels_schedule_semantically_and_audits(tmp_path, monkeypatch):
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(contest_id, status="published")
    calls: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: calls.append({"action": action, **fields}),
    )

    response = TestClient(app).delete(
        f"/api/admin/contests/{contest_id}", headers=headers
    )

    assert response.status_code == 200, response.text
    assert store.get_contest(contest_id) is None
    assert calls == [{
        "action": "admin_delete_contest",
        "result": "ok",
        "user": "statusadmin",
        "target": contest_id,
        "detail": "previous_status=published; mode=cancel_published_schedule_then_delete",
    }]


def test_non_admin_cannot_patch_contest_status(tmp_path):
    app, store, contest_id, _headers = _setup(tmp_path)
    user = store.create_user(
        "statususer", "statususer@example.com", hash_password("pw123456"), role="user"
    )
    store.update_user(user["id"], email_verified=1)
    token = new_session_token()
    store.add_session(token, user["id"], session_expires())

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}",
        json={"status": "open"},
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 403
    assert store.get_contest(contest_id)["status"] == "draft"
