"""代码唯一运行配置与只读诊断端点测试。"""
from __future__ import annotations

import sqlite3
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.runtime.config import (
    ACTION_TIMEOUT_SEC,
    AUTO_MATCH_BOOTSTRAP_TARGET_MATCHES,
    CONFIGURATION_SOURCE,
    MAX_CONCURRENT_MATCHES,
    RANKING_MIN_RATED_MATCHES,
)
from bzplat.backend.runtime.limits import (
    clamp_concurrent,
    concurrent_ceiling,
    default_max_concurrent,
)
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    SETTING_ACTION_TIMEOUT,
    SETTING_CONTEST_REST,
    SETTING_MAX_CONCURRENT,
)


def test_slot_ceiling_is_six_and_does_not_prejudge_job_resource_profiles():
    for logical_cpus in (1, 7, 8, 16, 24, 64):
        with mock.patch(
            "bzplat.backend.runtime.limits.os.cpu_count",
            return_value=logical_cpus,
        ):
            assert concurrent_ceiling() == 6
            assert clamp_concurrent(99) == 6
            assert clamp_concurrent(2) == 2
            assert default_max_concurrent() == 6


def test_explicit_startup_override_cannot_bypass_global_match_cap(tmp_path):
    with mock.patch("bzplat.backend.runtime.limits.os.cpu_count", return_value=8):
        client, app = _admin_client(tmp_path, max_concurrent=99)

    assert app.state.runtime_ceiling == 6
    assert app.state.orch.max_concurrent == 6
    assert app.state.execution_dispatcher.max_match_slots == 6
    assert app.state.execution_dispatcher.max_sandbox_units == 12

    response = client.get("/api/admin/settings/runtime")
    assert response.status_code == 200
    capacity = response.json()["queue"]["capacity"]
    assert capacity["match_slots"]["capacity"] == 6
    assert capacity["sandbox_units"]["capacity"] == 12


def test_six_slot_hard_cap_is_stable_on_large_hosts():
    with mock.patch("bzplat.backend.runtime.limits.os.cpu_count", return_value=24):
        assert concurrent_ceiling() == 6
        assert clamp_concurrent(99) == 6
        assert default_max_concurrent() == 6
    with mock.patch("bzplat.backend.runtime.limits.os.cpu_count", return_value=64):
        assert concurrent_ceiling() == 6


def _admin_client(tmp_path, *, max_concurrent: int | None = None):
    db = str(tmp_path / "t.db")
    app = create_app(db_path=db, max_concurrent=max_concurrent)
    store: Store = app.state.store
    user = store.create_user(
        "admin", "a@ex.com", hash_password("password12"), role="admin"
    )
    store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate("admin", "password12")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client, app


def test_runtime_diagnostics_are_explicitly_code_owned(tmp_path):
    client, app = _admin_client(tmp_path)
    response = client.get("/api/admin/settings/runtime")

    assert response.status_code == 200
    data = response.json()
    assert data["source"] == CONFIGURATION_SOURCE
    assert data["mutable"] is False
    assert data["action_timeout_sec"] == ACTION_TIMEOUT_SEC
    expected = default_max_concurrent()
    assert data["max_concurrent_matches"] == app.state.orch.max_concurrent == expected
    assert data["bot_cpus"] == 1
    assert data["bot_memory_mb"] == 512
    assert data["full_rr_max_n"] is None
    assert "full_rr_max_n" in data["readonly"]
    assert data["auto_match"]["enabled"] is True
    assert data["auto_match"]["mutable"] is True
    assert data["queue"]["capacity"]["match_slots"]["capacity"] == expected
    assert data["queue"]["capacity"]["sandbox_units"]["capacity"] == expected * 2
    assert data["queue"]["fairness"] == {
        "contest": "round_robin_v1",
        "bot_exclusivity": "active_execution_v1",
    }
    assert "interval_sec" not in data["auto_match"]
    assert data["contest_scheduler"] == {"enabled": True, "interval": 15}
    assert "auto_match" not in data["readonly"]


def test_qa_instance_uses_code_disabled_auto_match_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("BZ_QA_INSTANCE", "1")
    client, app = _admin_client(tmp_path)

    assert app.state.execution_dispatcher.auto_capability_enabled is False

    response = client.get("/api/admin/settings/runtime")
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == CONFIGURATION_SOURCE
    assert data["mutable"] is False
    assert data["auto_match"]["enabled"] is True
    assert app.state.execution_dispatcher.auto_capability_enabled is False
    assert (
        data["max_concurrent_matches"]
        == app.state.orch.max_concurrent
        == default_max_concurrent()
    )


@pytest.mark.parametrize(
    "payload",
    [
        {"max_concurrent_matches": 1},
        {"action_timeout_sec": 1},
        {"contest_default_rest_minutes": 0},
        {"auto_match_enabled": False},
        {"bot_cpus": 8},
    ],
)
def test_runtime_patch_route_does_not_exist(tmp_path, payload):
    client, app = _admin_client(tmp_path)
    before = app.state.store.get_settings()
    response = client.patch("/api/admin/settings/runtime", json=payload)

    assert response.status_code == 404
    assert app.state.store.get_settings() == before
    assert app.state.orch.max_concurrent == default_max_concurrent()
    assert app.state.orch.runner.action_timeout == ACTION_TIMEOUT_SEC


def test_startup_ignores_legacy_runtime_rows_and_does_not_rewrite_them(tmp_path):
    db = str(tmp_path / "legacy.db")
    legacy = {
        SETTING_ACTION_TIMEOUT: "1",
        SETTING_MAX_CONCURRENT: "999",
        SETTING_CONTEST_REST: "99",
        "auto_match_enabled": "0",
        "auto_match_interval_sec": "1",
    }
    store = Store(db)
    store.set_settings(legacy)
    store.close()

    app = create_app(db_path=db)

    assert app.state.orch.max_concurrent == default_max_concurrent()
    assert app.state.orch.runner.action_timeout == ACTION_TIMEOUT_SEC
    assert app.state.execution_dispatcher.auto_capability_enabled is True
    assert app.state.store.get_settings(
        [SETTING_ACTION_TIMEOUT, SETTING_MAX_CONCURRENT, SETTING_CONTEST_REST]
    ) == {
        SETTING_ACTION_TIMEOUT: "1",
        SETTING_MAX_CONCURRENT: "999",
        SETTING_CONTEST_REST: "99",
    }
    assert app.state.store.get_settings(
        ["auto_match_enabled", "auto_match_interval_sec"]
    ) == {}


def test_fresh_app_does_not_seed_legacy_runtime_settings(tmp_path):
    app = create_app(db_path=str(tmp_path / "fresh.db"))
    keys = [
        SETTING_ACTION_TIMEOUT,
        SETTING_MAX_CONCURRENT,
        SETTING_CONTEST_REST,
        "auto_match_enabled",
        "auto_match_interval_sec",
    ]
    assert app.state.store.get_settings(keys) == {}


def test_code_configuration_is_immutable():
    import bzplat.backend.runtime.config as config

    assert AUTO_MATCH_BOOTSTRAP_TARGET_MATCHES == 10
    assert MAX_CONCURRENT_MATCHES == 6
    assert RANKING_MIN_RATED_MATCHES == 10
    assert not hasattr(config, "AUTO_MATCH_CONFIG")
    assert not hasattr(config, "QA_AUTO_MATCH_CONFIG")


def test_store_set_settings_rolls_back_whole_batch_on_statement_failure(tmp_path):
    """保留通用 KV 的事务保障；站点文案仍使用该设施。"""
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
