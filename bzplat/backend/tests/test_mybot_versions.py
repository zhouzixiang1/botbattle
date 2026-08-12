"""MyBots 版本管理 + runtime_mode 测试（PR-2）。

覆盖：
1. db 层：create_bot/add_bot_version 写 runtime_mode；set_current_version 回滚恢复模式。
2. API：POST /api/bots 带 runtime_mode 入库；GET /api/bots/{id}/versions 列历史；
   POST /api/bots/{id}/versions 上传新版本；POST /.../versions/{v}/activate 回滚。
3. orchestrator 透传 runtime_modes 给 runner（单元级，mock runner）。
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier, Event

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.bots.manager import BotError
from bzplat.backend.runtime.binary_runner import PlatformRunnerError
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


def test_manual_multipart_routes_preserve_openapi_contract(tmp_path):
    """Pre-parse admission must not erase Swagger/generated-client bodies."""
    app = _app(tmp_path)
    paths = app.openapi()["paths"]
    cases = {
        "/api/bots": (
            {"name", "file"},
            {
                "name",
                "display_name",
                "description",
                "upload_note",
                "game_id",
                "runtime_mode",
                "file",
            },
            {"400", "401", "413", "503"},
        ),
        "/api/bots/{bot_id}/versions": (
            {"file"},
            {"upload_note", "runtime_mode", "file"},
            {"400", "401", "413", "503"},
        ),
        "/api/auth/avatar": (
            {"file"},
            {"file"},
            {"400", "401", "413", "422"},
        ),
        "/api/feedback/bugs/{bug_public_id}/attachments": (
            {"file"},
            {"tracking_token", "file"},
            {"400", "404", "413", "422"},
        ),
    }
    for path, (required, properties, responses) in cases.items():
        operation = paths[path]["post"]
        request_body = operation["requestBody"]
        assert request_body["required"] is True
        schema = request_body["content"]["multipart/form-data"]["schema"]
        assert set(schema["required"]) == required
        assert set(schema["properties"]) == properties
        assert schema["properties"]["file"] == {
            "type": "string",
            "format": "binary",
        }
        assert responses <= set(operation["responses"])


def test_upload_preflight_does_not_block_application_event_loop(tmp_path, monkeypatch):
    """A slow/unresponsive Bot upload must not freeze health, SSE or WebSocket tasks."""
    from httpx import ASGITransport, AsyncClient

    app = _app(tmp_path)
    _, owner = _setup(app)
    entered = Event()
    release = Event()

    def blocking_create(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return {
            "id": 999,
            "owner_id": owner["id"],
            "name": "nonblocking",
            "current_version": 1,
        }

    monkeypatch.setattr(app.state.bot_manager, "create_from_upload", blocking_create)

    async def exercise():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            upload = asyncio.create_task(
                client.post(
                    "/api/bots",
                    headers=_login(app),
                    data={"name": "nonblocking", "game_id": "holdem"},
                    files={"file": ("bot.bin", b"fake", "application/octet-stream")},
                )
            )
            try:
                assert await asyncio.wait_for(
                    asyncio.to_thread(entered.wait, 2), timeout=2.5
                )
                # This times out on the old synchronous route while .result()
                # blocks Uvicorn's only event loop.
                health = await asyncio.wait_for(client.get("/api/health"), timeout=0.5)
                assert health.status_code == 200
            finally:
                release.set()
            response = await asyncio.wait_for(upload, timeout=2)
            assert response.status_code == 200, response.text

    asyncio.run(exercise())


def test_upload_endpoint_reads_only_limit_plus_one_before_manager(
    tmp_path, monkeypatch
):
    """The API must reject an oversized body without retaining the whole file."""
    import bzplat.backend.api_routes as api_routes
    from starlette.datastructures import UploadFile as StarletteUploadFile

    app = _app(tmp_path)
    _setup(app)
    read_sizes: list[int] = []
    manager_called = False
    original_read = StarletteUploadFile.read

    async def tracked_read(upload, size=-1):
        read_sizes.append(size)
        return await original_read(upload, size)

    def unexpected_manager(*_args, **_kwargs):
        nonlocal manager_called
        manager_called = True
        raise AssertionError("oversized payload reached BotManager")

    monkeypatch.setattr(api_routes, "MAX_BYTES", 4)
    monkeypatch.setattr(StarletteUploadFile, "read", tracked_read)
    monkeypatch.setattr(
        app.state.bot_manager, "create_from_upload", unexpected_manager
    )

    response = TestClient(app).post(
        "/api/bots",
        headers=_login(app),
        data={"name": "bounded_read", "game_id": "holdem"},
        files={"file": ("bot.bin", b"12345", "application/octet-stream")},
    )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "invalid_size"
    assert read_sizes == [5]
    assert manager_called is False


def test_upload_admission_is_shared_busy_and_worker_cancel_safe(
    tmp_path, monkeypatch
):
    """New-Bot and version routes share one lane; cancel cannot release it early."""
    from httpx import ASGITransport, AsyncClient
    import bzplat.backend.api_routes as api_routes

    app = _app(tmp_path)
    _, owner = _setup(app)
    entered = Event()
    release = Event()
    create_calls = 0
    version_called = False

    def blocking_first_create(*_args, **_kwargs):
        nonlocal create_calls
        create_calls += 1
        if create_calls == 1:
            entered.set()
            assert release.wait(timeout=3)
        return {
            "id": 900 + create_calls,
            "owner_id": owner["id"],
            "name": f"upload-{create_calls}",
            "current_version": 1,
        }

    def unexpected_version(*_args, **_kwargs):
        nonlocal version_called
        version_called = True
        raise AssertionError("busy version upload reached BotManager")

    monkeypatch.setattr(api_routes, "BOT_UPLOAD_ADMISSION_WAIT_SEC", 0.05)
    monkeypatch.setattr(
        app.state.bot_manager, "create_from_upload", blocking_first_create
    )
    monkeypatch.setattr(
        app.state.bot_manager, "upload_version", unexpected_version
    )

    async def exercise():
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            first = asyncio.create_task(
                client.post(
                    "/api/bots",
                    headers=_login(app),
                    data={"name": "cancelled_upload", "game_id": "holdem"},
                    files={"file": ("bot.bin", b"first", "application/octet-stream")},
                )
            )
            assert await asyncio.wait_for(
                asyncio.to_thread(entered.wait, 2), timeout=2.5
            )
            first.cancel()
            await asyncio.sleep(0.01)
            assert not first.done(), "cancel released admission before worker ended"

            busy = await client.post(
                "/api/bots/123/versions",
                headers=_login(app),
                files={"file": ("bot.bin", b"second", "application/octet-stream")},
            )
            assert busy.status_code == 503
            assert busy.json()["detail"]["code"] == "upload_busy"
            assert busy.headers["retry-after"] == "1"
            assert version_called is False

            release.set()
            with pytest.raises(asyncio.CancelledError):
                await first

            after = await client.post(
                "/api/bots",
                headers=_login(app),
                data={"name": "after_cancel", "game_id": "holdem"},
                files={"file": ("bot.bin", b"third", "application/octet-stream")},
            )
            assert after.status_code == 200, after.text

    asyncio.run(exercise())
    assert create_calls == 2


def test_upload_admission_precedes_multipart_receive(tmp_path, monkeypatch):
    """A busy lane rejects another authenticated body before its first byte."""
    from httpx import ASGITransport, AsyncClient
    import bzplat.backend.api_routes as api_routes

    app = _app(tmp_path)
    _, owner = _setup(app)
    monkeypatch.setattr(api_routes, "BOT_UPLOAD_ADMISSION_WAIT_SEC", 0.05)
    monkeypatch.setattr(
        app.state.bot_manager,
        "create_from_upload",
        lambda *_args, **_kwargs: {
            "id": 990,
            "owner_id": owner["id"],
            "name": "parsed-after-admission",
            "current_version": 1,
        },
    )

    boundary = b"admission-before-form"
    body = (
        b"--" + boundary
        + b'\r\nContent-Disposition: form-data; name="name"\r\n\r\nfirst'
        + b"\r\n--" + boundary
        + b'\r\nContent-Disposition: form-data; name="file"; filename="bot.bin"'
        + b"\r\nContent-Type: application/octet-stream\r\n\r\nelf"
        + b"\r\n--" + boundary + b"--\r\n"
    )

    async def exercise():
        first_receive_entered = asyncio.Event()
        release_first_receive = asyncio.Event()
        first_body_reads = 0
        second_body_reads = 0

        async def first_body():
            nonlocal first_body_reads
            first_body_reads += 1
            first_receive_entered.set()
            await release_first_receive.wait()
            yield body

        async def second_body():
            nonlocal second_body_reads
            second_body_reads += 1
            yield body

        transport = ASGITransport(app=app)
        headers = {
            **_login(app),
            "Content-Type": (
                "multipart/form-data; boundary=" + boundary.decode()
            ),
        }
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            first = asyncio.create_task(
                client.post("/api/bots", headers=headers, content=first_body())
            )
            await asyncio.wait_for(first_receive_entered.wait(), timeout=1)

            busy = await client.post(
                "/api/bots/123/versions",
                headers=headers,
                content=second_body(),
            )
            assert busy.status_code == 503
            assert busy.json()["detail"]["code"] == "upload_busy"
            assert second_body_reads == 0

            release_first_receive.set()
            response = await asyncio.wait_for(first, timeout=1)
            assert response.status_code == 200, response.text
            assert first_body_reads == 1

    asyncio.run(exercise())


@pytest.mark.parametrize("path", ["/api/bots", "/api/auth/avatar"])
def test_authenticated_upload_routes_reject_before_reading_guest_body(
    tmp_path, path
):
    """Removing FastAPI File params makes auth run before multipart receive."""
    from httpx import ASGITransport, AsyncClient

    app = _app(tmp_path)
    _setup(app)
    boundary = "guest-body-not-read"

    async def exercise():
        body_reads = 0

        async def guest_body():
            nonlocal body_reads
            body_reads += 1
            yield b"untrusted multipart bytes"

        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://test"
        ) as client:
            response = await client.post(
                path,
                headers={
                    "Content-Type": f"multipart/form-data; boundary={boundary}"
                },
                content=guest_body(),
            )
        assert response.status_code == 401
        assert body_reads == 0

    asyncio.run(exercise())


def test_cancelled_upload_waiter_does_not_leak_permit(
    tmp_path, monkeypatch
):
    """Cancellation removes an asyncio waiter without leaking a permit."""
    from httpx import ASGITransport, AsyncClient
    import bzplat.backend.api_routes as api_routes

    app = _app(tmp_path)
    _setup(app)
    gate = app.state.bot_upload_gate
    monkeypatch.setattr(api_routes, "BOT_UPLOAD_ADMISSION_WAIT_SEC", 0.5)

    async def exercise():
        await gate.acquire()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            waiting = asyncio.create_task(
                client.post(
                    "/api/bots",
                    headers=_login(app),
                    data={"name": "cancelled_waiter", "game_id": "holdem"},
                    files={"file": ("bot.bin", b"waiting", "application/octet-stream")},
                )
            )
            await asyncio.sleep(0.02)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            gate.release()
            await asyncio.wait_for(gate.acquire(), timeout=0.1)
            gate.release()

    asyncio.run(exercise())


def test_upload_preflight_uses_pending_version_runtime_mode(tmp_path, monkeypatch):
    """预检必须验证待发布版本的选择，不能沿用当前版本或隐式默认。"""
    app = _app(tmp_path)
    _, owner = _setup(app)
    manager = app.state.bot_manager
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    raw = elf.read_bytes()
    bot = manager.create_from_upload(
        owner["id"],
        "mode_preflight",
        raw,
        runtime_mode="traditional",
    )
    captured: dict = {}

    def capture(*_args, runtime_mode, **_kwargs):
        captured["runtime_mode"] = runtime_mode
        return True, "ok"

    monkeypatch.setattr(manager, "_run_preflight", capture)
    manager.upload_version(
        bot["id"],
        owner["id"],
        raw + b"\nlongrunning-version",
        runtime_mode="longrunning",
        binary_runner=object(),
    )
    assert captured == {"runtime_mode": "longrunning"}


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

    # 回滚只改变当前激活版本，v2 仍保留；后续上传必须从历史最大版本
    # 继续生成 v3，不能按 current_version + 1 重复插入 v2 而 500。
    with open(elf, "rb") as f:
        r5 = client.post(
            f"/api/bots/{bot_id}/versions",
            headers=h,
            data={"upload_note": "v3 after rollback", "runtime_mode": "traditional"},
            files={"file": ("bot.bin", f, "application/octet-stream")},
        )
    assert r5.status_code == 200, r5.text
    assert r5.json()["bot"]["current_version"] == 3
    assert [v["version"] for v in store.list_bot_versions(bot_id)] == [3, 2, 1]


def test_concurrent_version_uploads_keep_unique_files_and_checksums(tmp_path):
    """Two tabs uploading together must allocate distinct versions atomically.

    The old read-MAX/write-file/INSERT sequence let both requests overwrite v2;
    one DB insert then failed while the surviving row could point at mismatched bytes.
    """
    app = _app(tmp_path)
    store, owner = _setup(app)
    manager = app.state.bot_manager
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    base = elf.read_bytes()
    bot = manager.create_from_upload(owner["id"], "parallel_bot", base, game_id="holdem")
    payloads = [base + b"\nparallel-a", base + b"\nparallel-b"]
    barrier = Barrier(2)

    def upload(raw: bytes):
        barrier.wait(timeout=3)
        return manager.upload_version(bot["id"], owner["id"], raw)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(upload, payloads))

    assert all(result["id"] == bot["id"] for result in results)
    versions = store.list_bot_versions(bot["id"])
    assert [row["version"] for row in versions] == [3, 2, 1]
    uploaded = []
    for row in versions[:2]:
        actual = Path(row["binary_path"]).read_bytes()
        assert hashlib.sha256(actual).hexdigest() == row["checksum"]
        assert len(actual) == row["size_bytes"]
        uploaded.append(actual)
    assert set(uploaded) == set(payloads)


def test_failed_preflight_restores_exact_pre_upload_activation(tmp_path, monkeypatch):
    """v1 active + historical v2 + failed v3 must remain on v1, not max(v2)."""
    app = _app(tmp_path)
    store, owner = _setup(app)
    manager = app.state.bot_manager
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    raw = elf.read_bytes()

    bot = manager.create_from_upload(
        owner["id"], "preflight_restore", raw,
        game_id="holdem", runtime_mode="traditional",
    )
    manager.upload_version(
        bot["id"], owner["id"], raw + b"\nv2", runtime_mode="longrunning",
    )
    manager.activate_version(bot["id"], owner["id"], 1)
    before = store.get_bot(bot["id"])
    assert before["current_version"] == 1
    assert before["runtime_mode"] == "traditional"

    monkeypatch.setattr(
        manager, "_run_preflight", lambda *_args, **_kwargs: (False, "qa failure"),
    )
    with pytest.raises(BotError, match="预检失败") as failed:
        manager.upload_version(
            bot["id"], owner["id"], raw + b"\nv3", binary_runner=object(),
        )
    assert failed.value.code == "preflight_failed"

    after = store.get_bot(bot["id"])
    assert after["current_version"] == 1
    assert after["binary_path"] == before["binary_path"]
    assert after["runtime_mode"] == "traditional"
    assert [row["version"] for row in store.list_bot_versions(bot["id"])] == [2, 1]
    assert not (manager.upload_root / str(bot["id"]) / "v3").exists()


def test_failed_initial_preflight_removes_database_row_and_files(tmp_path, monkeypatch):
    app = _app(tmp_path)
    store, owner = _setup(app)
    manager = app.state.bot_manager
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    monkeypatch.setattr(
        manager, "_run_preflight", lambda *_args, **_kwargs: (False, "qa failure"),
    )

    with pytest.raises(BotError, match="预检失败"):
        manager.create_from_upload(
            owner["id"], "preflight_new", elf.read_bytes(),
            game_id="holdem", binary_runner=object(),
        )

    assert store.get_bot_by_owner_name(owner["id"], "preflight_new") is None
    assert list(manager.upload_root.iterdir()) == []


def test_platform_preflight_failure_restores_activation_and_api_returns_503(
    tmp_path, monkeypatch
):
    """Sandbox outage must roll back upload atomically and never blame the Bot."""
    app = _app(tmp_path)
    store, owner = _setup(app)
    manager = app.state.bot_manager
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    raw = elf.read_bytes()

    bot = manager.create_from_upload(
        owner["id"], "platform_restore", raw,
        game_id="holdem", runtime_mode="traditional",
    )
    manager.upload_version(
        bot["id"], owner["id"], raw + b"\nv2", runtime_mode="longrunning",
    )
    manager.activate_version(bot["id"], owner["id"], 1)
    before = store.get_bot(bot["id"])

    def sandbox_down(*_args, **_kwargs):
        raise PlatformRunnerError("docker daemon unavailable")

    monkeypatch.setattr(manager, "_run_preflight", sandbox_down)
    with pytest.raises(PlatformRunnerError, match="daemon unavailable"):
        manager.upload_version(
            bot["id"], owner["id"], raw + b"\nv3", binary_runner=object(),
        )
    after = store.get_bot(bot["id"])
    assert after["current_version"] == 1
    assert after["binary_path"] == before["binary_path"]
    assert after["runtime_mode"] == "traditional"
    assert [row["version"] for row in store.list_bot_versions(bot["id"])] == [2, 1]
    assert not (manager.upload_root / str(bot["id"]) / "v3").exists()

    client = TestClient(app)
    with open(elf, "rb") as binary:
        response = client.post(
            "/api/bots",
            headers=_login(app),
            data={"name": "platform_new", "game_id": "holdem"},
            files={"file": ("bot.bin", binary, "application/octet-stream")},
        )
    assert response.status_code == 503, response.text
    assert store.get_bot_by_owner_name(owner["id"], "platform_new") is None
    assert not any(
        row["name"] == "platform_new" for row in store.list_bots(owner_id=owner["id"])
    )


def test_version_is_not_published_until_blocking_preflight_succeeds(
    tmp_path, monkeypatch
):
    """A queued match may only snapshot the last validated active version."""
    app = _app(tmp_path)
    store, owner = _setup(app)
    manager = app.state.bot_manager
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    raw = elf.read_bytes()
    bot = manager.create_from_upload(owner["id"], "staged_version", raw)

    entered = Event()
    release = Event()

    def blocking_preflight(*_args, binary_path=None, **_kwargs):
        assert binary_path and ".v2-" in binary_path
        entered.set()
        assert release.wait(timeout=3)
        return True, "ok"

    monkeypatch.setattr(manager, "_run_preflight", blocking_preflight)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            manager.upload_version,
            bot["id"],
            owner["id"],
            raw + b"\nstaged-v2",
            binary_runner=object(),
        )
        assert entered.wait(timeout=3)
        during = store.get_bot(bot["id"])
        assert during["current_version"] == 1
        assert [row["version"] for row in store.list_bot_versions(bot["id"])] == [1]
        release.set()
        result = future.result(timeout=3)

    assert result["current_version"] == 2
    assert [row["version"] for row in store.list_bot_versions(bot["id"])] == [2, 1]


def test_new_bot_stays_inactive_and_unversioned_during_preflight(
    tmp_path, monkeypatch
):
    app = _app(tmp_path)
    store, owner = _setup(app)
    manager = app.state.bot_manager
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    raw = elf.read_bytes()
    entered = Event()
    release = Event()

    def blocking_preflight(*_args, binary_path=None, **_kwargs):
        assert binary_path and ".v1-" in binary_path
        entered.set()
        assert release.wait(timeout=3)
        return True, "ok"

    monkeypatch.setattr(manager, "_run_preflight", blocking_preflight)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            manager.create_from_upload,
            owner["id"],
            "staged_new",
            raw,
            binary_runner=object(),
        )
        assert entered.wait(timeout=3)
        staged = store.get_bot_by_owner_name(owner["id"], "staged_new")
        assert staged["is_active"] == 0
        assert staged["current_version"] == 0
        assert staged["binary_path"] == ""
        assert store.list_bot_versions(staged["id"]) == []
        assert staged["id"] not in {
            row["id"] for row in store.list_bots(owner_id=owner["id"])
        }
        release.set()
        result = future.result(timeout=3)

    assert result["is_active"] == 1
    assert result["current_version"] == 1


def test_legacy_bot_image_survives_platform_failure_before_first_version(
    tmp_path, monkeypatch
):
    app = _app(tmp_path)
    store, owner = _setup(app)
    manager = app.state.bot_manager
    elf = _bot_binary()
    if elf is None:
        pytest.skip("callbot binary missing")
    raw = elf.read_bytes()
    legacy = store.create_bot(
        owner["id"],
        "legacy_platform",
        binary_path="/legacy/original.bin",
        os="linux",
        arch="amd64",
        format="elf",
        runtime_mode="traditional",
    )

    def sandbox_down(*_args, **_kwargs):
        raise PlatformRunnerError("docker daemon unavailable")

    monkeypatch.setattr(manager, "_run_preflight", sandbox_down)
    with pytest.raises(PlatformRunnerError):
        manager.upload_version(
            legacy["id"], owner["id"], raw, binary_runner=object()
        )

    after = store.get_bot(legacy["id"])
    for field in (
        "current_version", "binary_path", "os", "arch", "format", "runtime_mode"
    ):
        assert after[field] == legacy[field]
    assert store.list_bot_versions(legacy["id"]) == []
    assert not (manager.upload_root / str(legacy["id"]) / "v1").exists()


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
    # 非 owner 现在可看脱敏版本（供挑战页版本选择）：200 + 无 binary_path/runtime_mode
    r2 = client.get(f"/api/bots/{bot_id}/versions", headers=h_other)
    assert r2.status_code == 200
    vers = r2.json()["versions"]
    assert len(vers) >= 1
    # 脱敏：不应含 binary_path / runtime_mode
    assert all("binary_path" not in v for v in vers), "非 owner 不应见 binary_path"
    assert all("runtime_mode" not in v for v in vers), "非 owner 不应见 runtime_mode"
    # 但应含 id（挑战页版本选择需要）+ version + upload_note
    assert all("id" in v and "version" in v for v in vers)


# ── orchestrator 透传 ────────────────────────────────────────────────

def test_orchestrator_passes_runtime_modes_to_runner(tmp_path):
    """orchestrator 读 bot.runtime_mode 并以 runtime_modes=(a,b) 传给 runner。"""
    app = _app(tmp_path)
    store, u = _setup(app)
    # 两个 bot，一个 traditional 一个 longrunning
    path_a = tmp_path / "runtime-a"
    path_b = tmp_path / "runtime-b"
    path_a.write_bytes(b"test fixture")
    path_b.write_bytes(b"test fixture")
    ba = store.create_bot(u["id"], "oba", binary_path=str(path_a), format="elf", game_id="holdem", runtime_mode="traditional")
    bb = store.create_bot(u["id"], "obb", binary_path=str(path_b), format="elf", game_id="holdem", runtime_mode="longrunning")
    captured: dict = {}

    class _FakeRunner:
        runner = None

        def __init__(self):
            self.runner = self

        async def run_binaries(self, path_a, path_b, *, runtime_modes=None, **kw):
            captured["modes"] = runtime_modes
            captured["paths"] = (path_a, path_b)
            # 返回一个最小 result-like 对象
            class _R:
                rounds = []
                deltas = [0, 0]
                winner = None
                final_chips = [0, 0]
                rounds_played = 0
                events = []
            return _R()

        async def cleanup_execution(self, scope):
            scope.mark_cleanup_confirmed()

    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    orch = MatchOrchestrator(store, runner=_FakeRunner(), max_concurrent=1)
    async def run():
        store.executions.resume()
        request_id = await orch.challenge(
            ba["id"], bb["id"], u["id"], game_id="holdem"
        )
        job = store.executions.claim_next(
            max_match_slots=1,
            max_sandbox_units=2,
            aging_seconds=60,
            user_active_limit=1,
            contest_share_slots=1,
        )
        assert job is not None and job["public_id"] == request_id
        mid = str(job["current_match_id"])
        orch.start_execution_job(job)
        await orch._tasks[mid]
        assert store.executions.finalize_ready() == 1

    asyncio.run(run())
    assert captured.get("modes") == ("traditional", "longrunning"), captured
    assert captured.get("paths") == (str(path_a), str(path_b))


# ── GET /api/bots/{id} 脱敏（审计 P1-B）─────────────────────────────

def test_get_bot_public_desensitizes_binary_path(tmp_path):
    """GET /api/bots/{id} 公开访问必须脱敏 binary_path/runtime_mode
    （与 /api/bots/{id}/versions 脱敏口径一致；审计 P1-B）。"""
    app = _app(tmp_path)
    store, owner = _setup(app)
    bot = store.create_bot(
        owner["id"], "secretbot", binary_path="/uploads/secret/bot.bin",
        format="elf", game_id="holdem", runtime_mode="longrunning",
    )
    client = TestClient(app)

    # 1. 未登录（访客）：binary_path/runtime_mode 必须被脱敏
    r = client.get(f"/api/bots/{bot['id']}")
    assert r.status_code == 200
    public_bot = r.json()["bot"]
    assert "binary_path" not in public_bot, "访客不应看到 binary_path（泄漏磁盘布局）"
    assert "runtime_mode" not in public_bot, "访客不应看到 runtime_mode"
    # 其余公开字段保留
    assert public_bot["name"] == "secretbot"
    assert public_bot["game_id"] == "holdem"

    # 2. 非 owner 登录：同样脱敏
    other = store.create_user("other", "o@e.com", hash_password("pw123456"))
    store.update_user(other["id"], email_verified=1)
    _, other_tok = app.state.auth.authenticate("other", "pw123456")
    r = client.get(
        f"/api/bots/{bot['id']}", headers={"Authorization": f"Bearer {other_tok}"},
    )
    assert r.status_code == 200
    assert "binary_path" not in r.json()["bot"]

    # 3. owner 登录：完整字段（含 binary_path/runtime_mode）
    _, owner_tok = app.state.auth.authenticate("mvu", "pw123456")
    r = client.get(
        f"/api/bots/{bot['id']}", headers={"Authorization": f"Bearer {owner_tok}"},
    )
    assert r.status_code == 200
    owner_bot = r.json()["bot"]
    assert owner_bot["binary_path"] == "/uploads/secret/bot.bin"
    assert owner_bot["runtime_mode"] == "longrunning"

    # 4. admin 登录：同样可见完整字段
    admin = store.create_user("adm", "a@e.com", hash_password("pw123456"))
    store.update_user(admin["id"], role="admin", email_verified=1)
    _, admin_tok = app.state.auth.authenticate("adm", "pw123456")
    r = client.get(
        f"/api/bots/{bot['id']}", headers={"Authorization": f"Bearer {admin_tok}"},
    )
    assert r.status_code == 200
    assert r.json()["bot"]["binary_path"] == "/uploads/secret/bot.bin"


def test_public_bot_endpoints_desensitize_binary_path(tmp_path):
    """其他公开 bot 端点也必须脱敏 binary_path/runtime_mode（审计 P1-B 扩展）：
    /api/bots/public、/api/bots/{id}/profile、/api/users/{name}/bots。
    PR#125 只修了 get_bot，这 3 个端点遗漏（实测泄漏）。
    """
    app = _app(tmp_path)
    store, owner = _setup(app)
    bot = store.create_bot(
        owner["id"], "leakbot", binary_path="/uploads/leak/bot.bin",
        format="elf", game_id="holdem", runtime_mode="longrunning",
    )
    client = TestClient(app)

    # 1. /api/bots/public —— 访客不应见 binary_path
    r = client.get("/api/bots/public?game_id=holdem")
    assert r.status_code == 200
    for b in r.json().get("bots", []):
        assert "binary_path" not in b, f"/api/bots/public 泄漏 binary_path: {b.get('name')}"
        assert "runtime_mode" not in b

    # 2. /api/bots/{id}/profile —— 访客不应见 binary_path
    r = client.get(f"/api/bots/{bot['id']}/profile")
    assert r.status_code == 200
    prof = r.json()["profile"]
    assert "binary_path" not in prof, "/api/bots/{id}/profile 泄漏 binary_path"
    assert "runtime_mode" not in prof

    # 3. /api/users/{name}/bots —— 访客不应见 binary_path
    r = client.get(f"/api/users/mvu/bots")
    assert r.status_code == 200
    for b in r.json().get("bots", []):
        assert "binary_path" not in b, f"/api/users/{{name}}/bots 泄漏 binary_path: {b.get('name')}"

    # 4. owner 看 profile 应含完整字段
    _, owner_tok = app.state.auth.authenticate("mvu", "pw123456")
    r = client.get(f"/api/bots/{bot['id']}/profile", headers={"Authorization": f"Bearer {owner_tok}"})
    assert r.status_code == 200
    assert r.json()["profile"]["binary_path"] == "/uploads/leak/bot.bin"
