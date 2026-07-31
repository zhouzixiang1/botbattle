"""运行时 ceiling / settings 测试。"""
from __future__ import annotations

from unittest import mock

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.runtime.limits import clamp_concurrent, concurrent_ceiling
from bzplat.backend.store import Store


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
