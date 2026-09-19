"""拒绝归因日志契约：WS 拒绝、Bot 预检拒绝、密码重置审计不落邮箱。"""
from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import bzplat.backend.auth.routes as auth_routes
import bzplat.backend.bots.manager as manager_mod
from bzplat.backend.bots.manager import BotManager, BotError
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store.db import Store


def test_local_ai_denial_is_logged_with_stage(tmp_path, caplog):
    app = create_app(db_path=str(tmp_path / "deny.db"))
    client = TestClient(app)
    caplog.set_level(logging.WARNING, logger="bzplat.backend.api_routes")
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/local-ai/connect"):
            pass
    lines = [
        r.getMessage() for r in caplog.records
        if "ws local-ai denied" in r.getMessage()
    ]
    assert len(lines) == 1
    assert "stage=invalid_credentials" in lines[0]
    assert "ip=" in lines[0]
    assert "origin=" in lines[0]


def test_human_play_query_token_denial_is_logged(tmp_path, caplog):
    app = create_app(db_path=str(tmp_path / "play-deny.db"))
    client = TestClient(app)
    caplog.set_level(logging.WARNING, logger="bzplat.backend.api_routes")
    # human play 的拒绝是 accept 后回 reject 帧再 close：握手本身可进入，
    # 随后的接收以断开告终。
    with client.websocket_connect(
        "/api/matches/20260919-test/play?token=abc"
    ) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "reject"
        with pytest.raises(WebSocketDisconnect):
            ws.receive_json()
    lines = [
        r.getMessage() for r in caplog.records
        if "ws play denied" in r.getMessage()
    ]
    # query token 在 gate/session 之前被拒（origin_or_token 阶段）。
    assert len(lines) == 1
    assert "stage=origin_or_token" in lines[0]
    assert "match=20260919-test" in lines[0]


def test_preflight_rejection_is_attributed(tmp_path, caplog, monkeypatch):
    store = Store(str(tmp_path / "pfattr.db"))
    manager = BotManager(store, upload_root=tmp_path / "up")
    user = store.create_user(
        "pfattr", "pfattr@example.test", "test-password-hash"
    )

    class RejectRunner:
        async def start_session(self, binary_path, **kwargs):
            return "sid-1"

        async def send(self, sid, line, timeout=60.0):
            return '{"response":{"x":1,"y":0}}'

        async def stop_session(self, sid):
            return None

    real_run = manager._run_preflight

    def fake_run(*args, **kwargs):
        return False, "Bot 进程异常退出：exit 255\nstderr 第二行"

    monkeypatch.setattr(manager, "_run_preflight", fake_run)
    caplog.set_level(logging.WARNING, logger="bzplat.backend.bots.manager")

    import zipfile

    zp = tmp_path / "pf.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        zf.writestr("__main__.py", "pass\n")
    with pytest.raises(BotError) as exc:
        manager.create_from_upload(
            int(user["id"]), "pfattrbot", zp.read_bytes(),
            game_id="pencil", source_format="python",
            binary_runner=RejectRunner(),
        )
    assert exc.value.code == "preflight_failed"
    lines = [
        r.getMessage() for r in caplog.records
        if "bot preflight rejected" in r.getMessage()
    ]
    assert len(lines) == 1
    # 单行压平：换行被折成空格，多行 stderr 不再污染日志行结构。
    assert "mode=traditional" in lines[0]
    assert "source_format=python" in lines[0]
    assert "exit 255 stderr 第二行" in lines[0]
    assert "\n" not in lines[0]
    store.close()


def test_reset_password_fail_audit_uses_username_not_raw_input(
    tmp_path, monkeypatch,
):
    app = create_app(db_path=str(tmp_path / "resetattr.db"))
    store = app.state.store
    store.create_user(
        "resetattr", "secret-email@example.test",
        hash_password("pw123456"),
    )
    calls: list[dict] = []
    orig = auth_routes.audit_log

    def capture(request, action, **kwargs):
        calls.append({"action": action, **kwargs})
        return None

    monkeypatch.setattr(auth_routes, "audit_log", capture)
    client = TestClient(app)
    r = client.post("/api/auth/reset-password", json={
        "email_or_username": "secret-email@example.test",
        "code": "wrong-code",
        "new_password": "newpass123",
    })
    assert r.status_code in (400, 404, 422)
    hit = [c for c in calls if c["action"] == "reset_password"]
    assert len(hit) == 1
    # target 是已解析的用户名（或 None），绝不落原始输入（可能是邮箱 PII）。
    assert hit[0].get("target") in ("resetattr", None)
    assert "secret-email" not in str(hit[0])
    assert "secret-email" not in str(calls)
