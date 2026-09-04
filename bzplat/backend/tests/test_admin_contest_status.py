"""管理员赛事状态接口必须复用 ContestManager 生命周期，而非直接改状态列。"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.manager import ContestManager
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


def _published_pairings(store, contest_id):
    """构造带 round stagger 的最小 published 当前阶段。"""
    owner = store.get_user_by_username("statusadmin")
    bot_a = store.create_bot(owner["id"], f"schedule-a-{contest_id}")
    bot_b = store.create_bot(owner["id"], f"schedule-b-{contest_id}")
    store.update_contest(
        contest_id,
        status="published",
        registration_opens_at="2099-01-01T00:00:00",
        registration_closes_at="2099-01-02T00:00:00",
        starts_at="2099-01-03T10:00:00",
        current_stage_idx=0,
        stages_json=json.dumps([{
            "key": "rr",
            "type": "round_robin",
            "round_stagger_minutes": 15,
        }]),
    )
    first = store.add_contest_pairing(
        contest_id,
        bot_a["id"],
        bot_b["id"],
        round_num=1,
        stage_idx=0,
        stage_key="rr",
        scheduled_at="2099-01-03T10:00:00",
    )
    third = store.add_contest_pairing(
        contest_id,
        bot_b["id"],
        bot_a["id"],
        round_num=3,
        stage_idx=0,
        stage_key="rr",
        scheduled_at="2099-01-03T10:30:00",
    )
    # This helper intentionally builds a low-level two-row schedule rather
    # than a complete RR graph.  Give that imported fixture an exact manifest
    # so schedule-transaction tests reach their intended CAS/write boundary.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET published_stage_pairing_count=2 WHERE id=?",
            (contest_id,),
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest_id,),
        )
    return bot_a, bot_b, first, third


def _set_imported_contest_status(
    store,
    contest_id: int,
    status: str,
    *,
    official_results_ready: int | None = None,
) -> None:
    """Create an intentional legacy status fixture without using a live writer."""
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if official_results_ready is None:
            connection.execute(
                "UPDATE contests SET status=? WHERE id=?",
                (status, contest_id),
            )
        else:
            connection.execute(
                "UPDATE contests SET status=?,official_results_ready=? WHERE id=?",
                (status, official_results_ready, contest_id),
            )


def _running_two_player_contest(
    store, contest_id, *, materialize_pairings: bool = True
):
    """Build a valid current-stage roster without bypassing entry identity."""
    owner = store.get_user_by_username("statusadmin")
    opponent = store.create_user(
        f"status-player-{contest_id}",
        f"status-player-{contest_id}@example.com",
        "hash",
    )
    fixture_root = Path(store.path).resolve().parent
    bot_a_path = fixture_root / f"status-finish-a-{contest_id}.bin"
    bot_b_path = fixture_root / f"status-finish-b-{contest_id}.bin"
    bot_a_path.write_bytes(b"status fixture a")
    bot_b_path.write_bytes(b"status fixture b")
    bot_a = store.create_bot(
        owner["id"],
        f"status-finish-a-{contest_id}",
        binary_path=str(bot_a_path),
        format="elf",
        game_id="holdem",
    )
    bot_b = store.create_bot(
        opponent["id"],
        f"status-finish-b-{contest_id}",
        binary_path=str(bot_b_path),
        format="elf",
        game_id="holdem",
    )
    entry_a = store.add_contest_entry(contest_id, owner["id"], bot_a["id"])
    entry_b = store.add_contest_entry(
        contest_id, opponent["id"], bot_b["id"]
    )
    store.update_contest(
        contest_id,
        status="published",
        current_stage_idx=0,
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    pairing = None
    if materialize_pairings:
        manager = ContestManager(store, object())  # type: ignore[arg-type]
        asyncio.run(
            manager._begin_stage(
                contest_id,
                0,
                schedule_immediately=True,
                dispatch_pending=False,
            )
        )
        pairing = store.list_contest_pairings(contest_id, stage_idx=0)[0]
    else:
        _set_imported_contest_status(store, contest_id, "running")
    return owner, bot_a, bot_b, entry_a, entry_b, pairing


def test_admin_finish_uses_manager_and_returns_success(tmp_path):
    """旧实现先写 finished，随后引用未定义 admin 而 500；现在须完整收尾并返回 200。"""
    app, store, contest_id, headers = _setup(tmp_path)
    owner, bot_a, bot_b, _entry_a, _entry_b, pairing = _running_two_player_contest(
        store, contest_id
    )
    assert pairing is not None
    match_id = f"admin-finish-completed-{contest_id}"
    store.create_match(
        match_id,
        bot_a["id"],
        bot_b["id"],
        owner_id=owner["id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.bind_contest_pairing_match(
        contest_id,
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        result={"deltas": [100, -100]},
    )
    assert store.complete_contest_pairing_for_match(contest_id, match_id)

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}", json={"status": "finished"}, headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["contest"]["status"] == "finished"
    saved = store.get_contest(contest_id)
    assert saved["status"] == "finished"
    assert saved["ends_at"]
    assert saved["official_results_ready"] == 1
    assert len(store.list_official_results(contest_id)) == 2


def test_admin_finish_rejects_two_player_zero_pairing_graph(tmp_path):
    """Admin cannot freeze a missing current-stage batch into a zero table."""
    app, store, contest_id, headers = _setup(tmp_path)
    _running_two_player_contest(
        store, contest_id, materialize_pairings=False
    )

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}",
        json={"status": "finished"},
        headers=headers,
    )

    assert response.status_code == 400
    assert any(
        marker in response.json()["detail"]
        for marker in ("未完成对阵", "批次完整性")
    )
    saved = store.get_contest(contest_id)
    assert saved["status"] == "running"
    assert saved["official_results_ready"] == 0
    assert store.list_official_results(contest_id) == []


def test_admin_terminal_contest_cannot_be_cancelled(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    _set_imported_contest_status(store, contest_id, "finished")

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


def test_admin_open_schedule_only_accepts_future_close_and_start(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    store.update_contest(
        contest_id,
        status="open",
        registration_opens_at="2020-01-01T00:00:00",
        registration_closes_at="2099-01-02T00:00:00",
        starts_at="2099-01-03T00:00:00",
    )
    client = TestClient(app)

    frozen_open = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"registration_opens_at": "2099-01-01T00:00:00"},
        headers=headers,
    )
    assert frozen_open.status_code == 400
    assert store.get_contest(contest_id)["registration_opens_at"] == "2020-01-01T00:00:00"

    past_close = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"registration_closes_at": "2020-01-02T00:00:00"},
        headers=headers,
    )
    assert past_close.status_code == 400
    assert "晚于当前时间" in past_close.json()["detail"]

    future = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={
            "registration_closes_at": "2099-02-01T00:00:00",
            "starts_at": "2099-02-02T00:00:00",
        },
        headers=headers,
    )
    assert future.status_code == 200, future.text

    manual = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"registration_closes_at": None, "starts_at": None},
        headers=headers,
    )
    assert manual.status_code == 200, manual.text
    saved = store.get_contest(contest_id)
    assert saved["registration_closes_at"] is None
    assert saved["starts_at"] is None


def test_admin_published_start_recomputes_pending_round_schedule_and_can_clear(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    _published_pairings(store, contest_id)
    client = TestClient(app)

    frozen_close = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"registration_closes_at": "2099-02-01T00:00:00"},
        headers=headers,
    )
    assert frozen_close.status_code == 400
    assert "不能修改报名截止时间" in frozen_close.json()["detail"]

    rescheduled = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"starts_at": "2099-03-01T12:00:00"},
        headers=headers,
    )
    assert rescheduled.status_code == 200, rescheduled.text
    assert rescheduled.json()["contest"]["starts_at"] == "2099-03-01T12:00:00"
    assert [
        pairing["scheduled_at"]
        for pairing in store.list_contest_pairings(contest_id, stage_idx=0)
    ] == ["2099-03-01T12:00:00", "2099-03-01T12:30:00"]

    manual = client.patch(
        f"/api/admin/contests/{contest_id}",
        json={"starts_at": None},
        headers=headers,
    )
    assert manual.status_code == 200, manual.text
    assert store.get_contest(contest_id)["starts_at"] is None
    assert all(
        pairing["scheduled_at"] is None
        for pairing in store.list_contest_pairings(contest_id, stage_idx=0)
    )


def test_admin_published_start_rejects_any_bound_match_without_partial_write(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    bot_a, bot_b, first, _third = _published_pairings(store, contest_id)
    store.create_match(
        f"bound-{contest_id}",
        bot_a["id"],
        bot_b["id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.update_contest_pairing(first["id"], match_id=f"bound-{contest_id}")
    before = store.get_contest(contest_id)
    before_schedules = [
        pairing["scheduled_at"] for pairing in store.list_contest_pairings(contest_id)
    ]

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}",
        json={"title": "不得先写标题", "starts_at": "2099-04-01T00:00:00"},
        headers=headers,
    )

    assert response.status_code == 400
    assert "已有对局被派发" in response.json()["detail"]
    saved = store.get_contest(contest_id)
    assert saved["title"] == before["title"]
    assert saved["starts_at"] == before["starts_at"]
    assert [
        pairing["scheduled_at"] for pairing in store.list_contest_pairings(contest_id)
    ] == before_schedules


def test_store_published_schedule_update_rolls_back_contest_when_pairing_write_fails(tmp_path):
    _app, store, contest_id, _headers = _setup(tmp_path)
    _bot_a, _bot_b, first, _third = _published_pairings(store, contest_id)
    before = store.get_contest(contest_id)
    before_schedules = [
        pairing["scheduled_at"] for pairing in store.list_contest_pairings(contest_id)
    ]
    store._conn.execute(
        "CREATE TRIGGER fail_schedule_update BEFORE UPDATE OF scheduled_at "
        "ON contest_pairings WHEN OLD.id=%d BEGIN "
        "SELECT RAISE(ABORT, 'forced schedule failure'); END" % first["id"]
    )
    store._conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="forced schedule failure"):
        store.update_published_contest_schedule(
            contest_id,
            {"starts_at": "2099-05-01T00:00:00"},
            stage_idx=0,
            pending_pairing_schedules=[
                {
                    "id": pairing["id"],
                    "round_num": pairing["round_num"],
                    "scheduled_at": "2099-05-01T00:00:00",
                }
                for pairing in store.list_contest_pairings(contest_id)
            ],
        )

    assert store.get_contest(contest_id)["starts_at"] == before["starts_at"]
    assert [
        pairing["scheduled_at"] for pairing in store.list_contest_pairings(contest_id)
    ] == before_schedules


def test_store_published_schedule_update_requires_exact_lifecycle_seal(tmp_path):
    _app, store, contest_id, _headers = _setup(tmp_path)
    _published_pairings(store, contest_id)
    before = store.get_contest(contest_id)
    pairings = store.list_contest_pairings(contest_id)
    before_schedules = [pairing["scheduled_at"] for pairing in pairings]
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision=NULL "
            "WHERE id=?",
            (contest_id,),
        )

    with pytest.raises(ValueError, match="manifest|seal"):
        store.update_published_contest_schedule(
            contest_id,
            {"starts_at": "2099-05-01T00:00:00"},
            stage_idx=0,
            pending_pairing_schedules=[
                {
                    "id": pairing["id"],
                    "round_num": pairing["round_num"],
                    "scheduled_at": "2099-05-01T00:00:00",
                }
                for pairing in pairings
            ],
        )

    saved = store.get_contest(contest_id)
    assert saved["starts_at"] == before["starts_at"]
    assert [
        pairing["scheduled_at"]
        for pairing in store.list_contest_pairings(contest_id)
    ] == before_schedules


@pytest.mark.parametrize("status", ["running", "rest", "finished", "cancelled"])
def test_admin_active_and_terminal_schedule_is_read_only(tmp_path, status):
    app, store, contest_id, headers = _setup(tmp_path)
    _set_imported_contest_status(store, contest_id, status)

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}",
        json={"starts_at": "2099-01-01T00:00:00"},
        headers=headers,
    )

    assert response.status_code == 400
    assert "时间编排只读" in response.json()["detail"]
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


@pytest.mark.parametrize(
    "value",
    ["2099-01-03 00:00:00", "20990103T000000"],
)
def test_contest_create_rejects_noncanonical_time_without_insert(
    tmp_path, value
):
    app, store, _contest_id, headers = _setup(tmp_path)
    before = len(store.list_contests())

    response = TestClient(app).post(
        "/api/contests",
        json={"title": "非规范时间赛", "starts_at": value},
        headers=headers,
    )

    assert response.status_code == 400
    assert "规范" in response.json()["detail"]
    assert len(store.list_contests()) == before


@pytest.mark.parametrize(
    "value",
    ["2099-01-03 00:00:00", "20990103T000000"],
)
def test_admin_time_patch_rejects_noncanonical_value_atomically(
    tmp_path, value
):
    app, store, contest_id, headers = _setup(tmp_path)

    response = TestClient(app).patch(
        f"/api/admin/contests/{contest_id}",
        json={"title": "不得写入", "starts_at": value},
        headers=headers,
    )

    assert response.status_code == 400
    assert "规范" in response.json()["detail"]
    saved = store.get_contest(contest_id)
    assert saved["title"] == "状态回归赛"
    assert saved["starts_at"] is None


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
    _set_imported_contest_status(store, contest_id, status)

    response = TestClient(app).delete(
        f"/api/admin/contests/{contest_id}", headers=headers
    )

    assert response.status_code == 409
    assert store.get_contest(contest_id)["status"] == status


def test_admin_delete_rejects_finished_and_preserves_official_result_container(tmp_path):
    app, store, contest_id, headers = _setup(tmp_path)
    _set_imported_contest_status(
        store,
        contest_id,
        "finished",
        official_results_ready=1,
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
