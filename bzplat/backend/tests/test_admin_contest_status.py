"""管理员赛事状态接口必须复用 ContestManager 生命周期，而非直接改状态列。"""
from __future__ import annotations

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
        "user": "statusadmin", "target": contest_id, "detail": "cancelled",
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
