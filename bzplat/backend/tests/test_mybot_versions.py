"""MyBots 版本管理 + runtime_mode 测试（PR-2）。

覆盖：
1. db 层：create_bot/add_bot_version 写 runtime_mode；set_current_version 回滚恢复模式。
2. API：POST /api/bots 带 runtime_mode 入库；GET /api/bots/{id}/versions 列历史；
   POST /api/bots/{id}/versions 上传新版本；POST /.../versions/{v}/activate 回滚。
3. orchestrator 透传 runtime_modes 给 runner（单元级，mock runner）。
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.store.schema import DEFAULT_RUNTIME_MODE, VALID_RUNTIME_MODES

SAMPLES = Path(__file__).resolve().parents[3] / "samples"


def _app(tmp_path):
    from bzplat.backend.main import create_app
    os.environ["BZ_BOT_LOCAL"] = "1"
    os.environ["BZ_SKIP_CAPTCHA"] = "1"
    return create_app(db_path=str(tmp_path / "mv.db"))


def _login(app, username="mvu", password="pw123456"):
    _, tok = app.state.auth.authenticate(username, password)
    return {"Authorization": f"Bearer {tok}"}


def _setup(app):
    store = app.state.store
    u = store.create_user("mvu", "mvu@a.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    return store, u


# ── db 层 ─────────────────────────────────────────────────────────────

def test_create_bot_runtime_mode_default(tmp_path):
    from bzplat.backend.store import Store
    store = Store(str(tmp_path / "t.db"))
    store.create_user("u01", "u@e.com", "hx")
    uid = store._conn.execute("SELECT id FROM users WHERE username='u01'").fetchone()["id"]
    b = store.create_bot(uid, "b1", binary_path="x", format="elf", game_id="holdem")
    assert b["runtime_mode"] == DEFAULT_RUNTIME_MODE


def test_create_bot_runtime_mode_explicit(tmp_path):
    from bzplat.backend.store import Store
    store = Store(str(tmp_path / "t.db"))
    store.create_user("u01", "u@e.com", "hx")
    uid = store._conn.execute("SELECT id FROM users WHERE username='u01'").fetchone()["id"]
    b = store.create_bot(uid, "b1", binary_path="x", format="elf", game_id="holdem", runtime_mode="traditional")
    assert b["runtime_mode"] == "traditional"


def test_create_bot_invalid_runtime_mode(tmp_path):
    from bzplat.backend.store import Store
    store = Store(str(tmp_path / "t.db"))
    store.create_user("u01", "u@e.com", "hx")
    uid = store._conn.execute("SELECT id FROM users WHERE username='u01'").fetchone()["id"]
    with pytest.raises(ValueError):
        store.create_bot(uid, "b1", binary_path="x", format="elf", game_id="holdem", runtime_mode="bogus")


def test_add_bot_version_writes_runtime_mode(tmp_path):
    """上传新版本写 bot_versions.runtime_mode + 同步 bots.runtime_mode。"""
    from bzplat.backend.store import Store
    store = Store(str(tmp_path / "t.db"))
    store.create_user("u01", "u@e.com", "hx")
    uid = store._conn.execute("SELECT id FROM users WHERE username='u01'").fetchone()["id"]
    b = store.create_bot(uid, "b1", binary_path="x", format="elf", game_id="holdem", runtime_mode="longrunning")
    # 上传 v1（traditional）
    v1 = store.add_bot_version(b["id"], binary_path="x1", format="elf", runtime_mode="traditional")
    assert v1["runtime_mode"] == "traditional"
    assert store.get_bot(b["id"])["runtime_mode"] == "traditional"
    # 上传 v2（longrunning）
    v2 = store.add_bot_version(b["id"], binary_path="x2", format="elf", runtime_mode="longrunning")
    assert v2["version"] == 2
    assert store.get_bot(b["id"])["runtime_mode"] == "longrunning"


def test_set_current_version_restores_runtime_mode(tmp_path):
    """回滚到指定版本时，runtime_mode 也恢复到该版本的值。"""
    from bzplat.backend.store import Store
    store = Store(str(tmp_path / "t.db"))
    store.create_user("u01", "u@e.com", "hx")
    uid = store._conn.execute("SELECT id FROM users WHERE username='u01'").fetchone()["id"]
    b = store.create_bot(uid, "b1", binary_path="x", format="elf", game_id="holdem", runtime_mode="longrunning")
    store.add_bot_version(b["id"], binary_path="x1", format="elf", runtime_mode="traditional")   # v1
    store.add_bot_version(b["id"], binary_path="x2", format="elf", runtime_mode="longrunning")  # v2
    assert store.get_bot(b["id"])["current_version"] == 2
    assert store.get_bot(b["id"])["runtime_mode"] == "longrunning"
    # 回滚到 v1
    rb = store.set_current_version(b["id"], 1)
    assert rb["current_version"] == 1
    assert rb["runtime_mode"] == "traditional"  # 恢复 v1 的模式
    assert rb["binary_path"] == "x1"


def test_set_current_version_nonexistent(tmp_path):
    from bzplat.backend.store import Store
    store = Store(str(tmp_path / "t.db"))
    store.create_user("u01", "u@e.com", "hx")
    uid = store._conn.execute("SELECT id FROM users WHERE username='u01'").fetchone()["id"]
    b = store.create_bot(uid, "b1", binary_path="x", format="elf", game_id="holdem")
    assert store.set_current_version(b["id"], 99) is None


# ── API 层 ────────────────────────────────────────────────────────────

def _bot_binary():
    """返回一个可用的样例 ELF 路径（callbot）。"""
    p = SAMPLES / "callbot_linux_amd64"
    return p if p.is_file() else None


def test_api_upload_bot_with_runtime_mode(tmp_path):
    app = _app(tmp_path)
    store, u = _setup(app)
    client = TestClient(app)
    h = _login(app)
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    with open(elf, "rb") as f:
        r = client.post(
            "/api/bots",
            headers=h,
            data={"name": "apibot1", "game_id": "holdem", "runtime_mode": "traditional"},
            files={"file": ("bot.bin", f, "application/octet-stream")},
        )
    assert r.status_code == 200, r.text
    bot = r.json()["bot"]
    assert bot["runtime_mode"] == "traditional"


def test_api_list_versions_and_rollback(tmp_path):
    app = _app(tmp_path)
    store, u = _setup(app)
    client = TestClient(app)
    h = _login(app)
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    # 建第一个 bot（v1，traditional）
    with open(elf, "rb") as f:
        r = client.post(
            "/api/bots",
            headers=h,
            data={"name": "apibot2", "game_id": "holdem", "runtime_mode": "traditional"},
            files={"file": ("bot.bin", f, "application/octet-stream")},
        )
    assert r.status_code == 200, r.text
    bot_id = r.json()["bot"]["id"]

    # 上传 v2（longrunning）
    with open(elf, "rb") as f:
        r2 = client.post(
            f"/api/bots/{bot_id}/versions",
            headers=h,
            data={"upload_note": "v2", "runtime_mode": "longrunning"},
            files={"file": ("bot.bin", f, "application/octet-stream")},
        )
    assert r2.status_code == 200, r2.text
    assert r2.json()["bot"]["current_version"] == 2
    assert r2.json()["bot"]["runtime_mode"] == "longrunning"

    # 列版本历史
    r3 = client.get(f"/api/bots/{bot_id}/versions", headers=h)
    assert r3.status_code == 200
    versions = r3.json()["versions"]
    assert len(versions) == 2
    assert versions[0]["version"] == 2  # DESC
    assert versions[1]["runtime_mode"] == "traditional"

    # 回滚到 v1
    r4 = client.post(f"/api/bots/{bot_id}/versions/1/activate", headers=h)
    assert r4.status_code == 200, r4.text
    assert r4.json()["bot"]["current_version"] == 1
    assert r4.json()["bot"]["runtime_mode"] == "traditional"


def test_api_versions_owner_only(tmp_path):
    """非 owner 不能看他人 Bot 的版本历史（403）。"""
    app = _app(tmp_path)
    store = _setup(app)[0]
    store.create_user("other", "o@e.com", hash_password("pw123456"))
    store.update_user(
        store._conn.execute("SELECT id FROM users WHERE username='other'").fetchone()["id"],
        email_verified=1,
    )
    client = TestClient(app)
    h_owner = _login(app, "mvu")
    h_other = _login(app, "other")
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    with open(elf, "rb") as f:
        r = client.post(
            "/api/bots",
            headers=h_owner,
            data={"name": "apibot3", "game_id": "holdem"},
            files={"file": ("bot.bin", f, "application/octet-stream")},
        )
    bot_id = r.json()["bot"]["id"]
    # other 看不到 mvu 的 bot 版本
    r2 = client.get(f"/api/bots/{bot_id}/versions", headers=h_other)
    assert r2.status_code == 403


# ── orchestrator 透传 ────────────────────────────────────────────────

def test_orchestrator_passes_runtime_modes_to_runner(tmp_path):
    """orchestrator 读 bot.runtime_mode 并以 runtime_modes=(a,b) 传给 runner。"""
    app = _app(tmp_path)
    store, u = _setup(app)
    # 两个 bot，一个 traditional 一个 longrunning
    ba = store.create_bot(u["id"], "oba", binary_path="/tmp/a", format="elf", game_id="holdem", runtime_mode="traditional")
    bb = store.create_bot(u["id"], "obb", binary_path="/tmp/b", format="elf", game_id="holdem", runtime_mode="longrunning")
    captured: dict = {}

    class _FakeRunner:
        async def run_binaries(self, path_a, path_b, *, runtime_modes=None, **kw):
            captured["modes"] = runtime_modes
            captured["paths"] = (path_a, path_b)
            # 返回一个最小 result-like 对象
            class _R:
                rounds = []
                deltas = [0, 0]
                winner = None
                final_chips = [0, 0]
                hands_played = 0
                events = []
            return _R()

    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    orch = MatchOrchestrator(store, runner=_FakeRunner(), max_concurrent=1)
    mid = asyncio.run(orch.challenge(ba["id"], bb["id"], u["id"], match_config={"hands": 2}, game_id="holdem"))
    # 等对局完成
    for _ in range(40):
        import time
        time.sleep(0.1)
        m = store.get_match(mid)
        if m and m["status"] in ("completed", "aborted"):
            break
    assert captured.get("modes") == ("traditional", "longrunning"), captured
    assert captured.get("paths") == ("/tmp/a", "/tmp/b")
