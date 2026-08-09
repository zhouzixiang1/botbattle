"""运行时 ceiling / settings 测试。"""
from __future__ import annotations

import sqlite3
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.runtime.limits import clamp_concurrent, concurrent_ceiling
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    SETTING_ACTION_TIMEOUT,
    SETTING_CONTEST_REST,
    SETTING_MAX_CONCURRENT,
)


def test_ceiling_formula():
    with mock.patch("bzplat.backend.runtime.limits.os.cpu_count", return_value=8):
        assert concurrent_ceiling() == 2  # 8//4
    with mock.patch("bzplat.backend.runtime.limits.os.cpu_count", return_value=1):
        assert concurrent_ceiling() == 1
    with mock.patch("bzplat.backend.runtime.limits.os.cpu_count", return_value=16):
        assert concurrent_ceiling() == 4
        assert clamp_concurrent(99) == 4
        assert clamp_concurrent(2) == 2


def _admin_client(tmp_path):
    db = str(tmp_path / "t.db")
    app = create_app(db_path=db, max_concurrent=1)
    store: Store = app.state.store
    u = store.create_user(
        "admin", "a@ex.com", hash_password("password12"), role="admin"
    )
    store.update_user(u["id"], email_verified=1)
    _, token = app.state.auth.authenticate("admin", "password12")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client, app


def test_patch_concurrent_over_ceiling_rejected(tmp_path):
    client, _app = _admin_client(tmp_path)
    ceiling = concurrent_ceiling()
    r = client.patch(
        "/api/admin/settings/runtime",
        json={"max_concurrent_matches": ceiling + 100},
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "ceiling" in detail.lower() or "硬顶" in detail


def test_patch_bot_cpus_rejected(tmp_path):
    client, _ = _admin_client(tmp_path)
    r = client.patch("/api/admin/settings/runtime", json={"bot_cpus": 8})
    assert r.status_code == 400


def test_get_runtime_ok(tmp_path):
    client, _app = _admin_client(tmp_path)
    r = client.get("/api/admin/settings/runtime")
    assert r.status_code == 200
    data = r.json()
    assert data["ceiling"] == concurrent_ceiling()
    assert data["bot_cpus"] == 1
    assert data["bot_memory_mb"] == 512
    assert data["max_concurrent_matches"] <= data["ceiling"]


def test_runtime_returns_auto_match_block(tmp_path):
    client, _ = _admin_client(tmp_path)
    r = client.get("/api/admin/settings/runtime")
    am = r.json()["auto_match"]
    assert set(am) == {
        "enabled", "interval_sec", "min_idle_sec",
        "bot_cooldown", "stale_sec", "reserve_slots",
        "placement_games", "max_per_round", "daily_cap", "daily_count",
    }
    assert am["enabled"] is True
    assert am["placement_games"] == 10
    assert am["daily_cap"] == 200
    assert am["daily_count"] == 0


def test_patch_auto_match_updates_and_hot_reloads(tmp_path):
    client, _ = _admin_client(tmp_path)
    r = client.patch(
        "/api/admin/settings/runtime",
        json={"auto_match_enabled": False, "auto_match_interval_sec": 60},
    )
    assert r.status_code == 200
    am = r.json()["runtime"]["auto_match"]
    assert am["enabled"] is False
    assert am["interval_sec"] == 60


def test_patch_auto_match_validates_bounds(tmp_path):
    client, _ = _admin_client(tmp_path)
    r = client.patch(
        "/api/admin/settings/runtime",
        json={"auto_match_interval_sec": 0},
    )
    assert r.status_code == 400


def test_runtime_multifield_validation_has_no_db_or_hot_partial_effect(tmp_path, monkeypatch):
    client, app = _admin_client(tmp_path)
    store = app.state.store
    before = {
        SETTING_MAX_CONCURRENT: store.get_setting(SETTING_MAX_CONCURRENT),
        SETTING_CONTEST_REST: store.get_setting(SETTING_CONTEST_REST),
    }
    hot_calls: list[tuple[str, float]] = []
    monkeypatch.setattr(
        app.state.orch,
        "rebuild_concurrency",
        lambda value: hot_calls.append(("concurrency", value)),
    )

    response = client.patch(
        "/api/admin/settings/runtime",
        json={
            "max_concurrent_matches": 1,
            "contest_default_rest_minutes": 12,
            "auto_match_interval_sec": 0,
        },
    )

    assert response.status_code == 400
    assert store.get_setting(SETTING_MAX_CONCURRENT) == before[SETTING_MAX_CONCURRENT]
    assert store.get_setting(SETTING_CONTEST_REST) == before[SETTING_CONTEST_REST]
    assert hot_calls == []


def test_runtime_batch_commits_before_hot_reload_and_is_audited(tmp_path, monkeypatch):
    client, app = _admin_client(tmp_path)
    store = app.state.store
    order: list[str] = []
    audits: list[dict] = []
    original_set_settings = store.set_settings

    def record_commit(values):
        original_set_settings(values)
        order.append("commit")

    def record_concurrency(value):
        assert store.get_setting(SETTING_MAX_CONCURRENT) == str(value)
        order.append("concurrency")

    def record_timeout(value):
        assert store.get_setting(SETTING_ACTION_TIMEOUT) == str(value)
        order.append("timeout")

    monkeypatch.setattr(store, "set_settings", record_commit)
    monkeypatch.setattr(app.state.orch, "rebuild_concurrency", record_concurrency)
    monkeypatch.setattr(app.state.orch, "set_action_timeout", record_timeout)
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append({"action": action, **fields}),
    )

    response = client.patch(
        "/api/admin/settings/runtime",
        json={"max_concurrent_matches": 1, "action_timeout_sec": 30},
    )

    assert response.status_code == 200, response.text
    assert order == ["commit", "concurrency", "timeout"]
    assert audits == [{
        "action": "admin_patch_runtime",
        "result": "ok",
        "user": "admin",
        "target": "runtime",
        "detail": "max_concurrent_matches=1; action_timeout_sec=30.0",
    }]


def test_runtime_validation_failure_is_audited(tmp_path, monkeypatch):
    client, _app = _admin_client(tmp_path)
    audits: list[dict] = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **fields: audits.append({"action": action, **fields}),
    )

    response = client.patch(
        "/api/admin/settings/runtime",
        json={"contest_default_rest_minutes": 10, "auto_match_interval_sec": 0},
    )

    assert response.status_code == 400
    assert audits[0]["action"] == "admin_patch_runtime"
    assert audits[0]["result"] == "fail"


def test_store_set_settings_rolls_back_whole_batch_on_statement_failure(tmp_path):
    store = Store(str(tmp_path / "settings-atomic.db"))
    store._conn.execute(
        "CREATE TRIGGER reject_setting BEFORE INSERT ON platform_settings "
        "WHEN NEW.key='reject_setting' BEGIN "
        "SELECT RAISE(ABORT, 'rejected'); END"
    )
    store._conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="rejected"):
        store.set_settings({"first_setting": "1", "reject_setting": "2"})

    assert store.get_setting("first_setting") is None
    assert store.get_setting("reject_setting") is None
