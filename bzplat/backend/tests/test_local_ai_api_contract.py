"""HTTP/WebSocket contracts for user-hosted Bot connections.

These tests keep the durable identity, transport credential and public match
projection as three separate boundaries.  A raw token is one-time private
material; neither it nor the internal local-agent binding may cross a public
match/execution response.
"""
from __future__ import annotations

import asyncio
import functools
import json
import time
from pathlib import Path

import pytest
import bzplat.backend.api_routes as api_routes_module
import bzplat.backend.runtime.local_ai_service as local_ai_service_module
import bzplat.backend.store.db as store_db_module
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.runtime.local_ai import LocalAITechnicalError
from bzplat.backend.runtime.local_ai_service import LocalAIRateLimitError
from bzplat.backend.runtime.local_ai_service import LocalAIHandshakeGate


PASSWORD = "pw123456"


def _auth(app, username: str) -> dict[str, str]:
    _, token = app.state.auth.authenticate(username, PASSWORD)
    return {"Authorization": f"Bearer {token}"}


def _user(app, username: str, *, role: str = "user") -> dict:
    user = app.state.store.create_user(
        username,
        f"{username}@example.com",
        hash_password(PASSWORD),
        role=role,
    )
    app.state.store.update_user(
        int(user["id"]), email_verified=1, is_active=1
    )
    return user


def _bot(app, owner: dict, name: str, *, game_id: str = "gomoku") -> dict:
    binary = Path(app.state.store.path).parent / f"{name}.elf"
    binary.write_bytes(b"local-ai-api-test")
    bot = app.state.store.create_bot(
        int(owner["id"]),
        name,
        binary_path=str(binary),
        format="elf",
        game_id=game_id,
    )
    app.state.store.add_bot_version(bot["id"], binary_path=str(binary))
    return bot


def _assert_private_no_store(response) -> None:
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert {
        item.strip() for item in response.headers["vary"].split(",")
    } == {"Authorization", "Cookie"}


def _assert_no_credential_fields(value) -> None:
    serialized = json.dumps(value, ensure_ascii=False)
    assert "token_hash" not in serialized
    assert "bzlai_" not in serialized


def test_owner_crud_rotates_identity_and_admin_never_receives_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-crud.db"))
    owner = _user(app, "localowner")
    other = _user(app, "localother")
    admin = _user(app, "localadmin", role="admin")
    bot = _bot(app, owner, "owner_gomoku")

    client = TestClient(app)
    owner_headers = _auth(app, owner["username"])
    other_headers = _auth(app, other["username"])
    admin_headers = _auth(app, admin["username"])

    created = client.post(
        "/api/local-ai/agents",
        headers=owner_headers,
        json={"bot_id": bot["id"], "label": "宿舍电脑"},
    )
    assert created.status_code == 201, created.text
    _assert_private_no_store(created)
    first = created.json()
    first_public_id = first["agent"]["public_id"]
    first_token = first["token"]
    assert first_token.startswith("bzlai_")
    assert first["agent"]["token_hint"] == first_token[-6:]
    assert first["connection_url"] == "/api/local-ai/connect"
    assert "token_hash" not in first["agent"]
    assert app.state.local_ai_service.authenticate(first_token) is not None

    own_list = client.get("/api/local-ai/agents", headers=owner_headers)
    assert own_list.status_code == 200
    _assert_private_no_store(own_list)
    assert [item["public_id"] for item in own_list.json()["items"]] == [
        first_public_id
    ]
    _assert_no_credential_fields(own_list.json())

    other_list = client.get("/api/local-ai/agents", headers=other_headers)
    assert other_list.status_code == 200
    assert other_list.json() == {"items": []}
    denied = client.delete(
        f"/api/local-ai/agents/{first_public_id}", headers=other_headers
    )
    assert denied.status_code == 404

    admin_list = client.get(
        "/api/admin/local-ai/agents", headers=admin_headers
    )
    assert admin_list.status_code == 200
    _assert_private_no_store(admin_list)
    assert admin_list.json()["total"] == 1
    assert admin_list.json()["page"] == 1
    assert admin_list.json()["per_page"] == 20
    assert admin_list.json()["items"][0]["owner_name"] == owner["username"]
    assert "token_hint" not in admin_list.json()["items"][0]
    _assert_no_credential_fields(admin_list.json())

    rotated = client.post(
        f"/api/local-ai/agents/{first_public_id}/rotate",
        headers=owner_headers,
    )
    assert rotated.status_code == 200, rotated.text
    _assert_private_no_store(rotated)
    second = rotated.json()
    second_public_id = second["agent"]["public_id"]
    second_token = second["token"]
    assert second_public_id != first_public_id
    assert second_token != first_token
    assert app.state.local_ai_service.authenticate(first_token) is None
    assert app.state.local_ai_service.authenticate(second_token) is not None
    assert (
        client.delete(
            f"/api/local-ai/agents/{first_public_id}", headers=owner_headers
        ).status_code
        == 404
    )

    revoked = client.delete(
        f"/api/admin/local-ai/agents/{second_public_id}",
        headers=admin_headers,
    )
    assert revoked.status_code == 200
    _assert_private_no_store(revoked)
    assert app.state.local_ai_service.authenticate(second_token) is None


def test_revoked_label_can_be_reused_with_a_new_identity(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-reuse.db"))
    owner = _user(app, "reuseowner")
    first_bot = _bot(app, owner, "reuse_first")
    second_bot = _bot(app, owner, "reuse_second")
    client = TestClient(app)
    headers = _auth(app, owner["username"])

    first = client.post(
        "/api/local-ai/agents", headers=headers,
        json={"bot_id": first_bot["id"], "label": "常用电脑"},
    ).json()
    conflict = client.post(
        "/api/local-ai/agents", headers=headers,
        json={"bot_id": second_bot["id"], "label": "常用电脑"},
    )
    assert conflict.status_code == 400
    assert client.delete(
        f"/api/local-ai/agents/{first['agent']['public_id']}", headers=headers
    ).status_code == 200

    reused = client.post(
        "/api/local-ai/agents", headers=headers,
        json={"bot_id": second_bot["id"], "label": "常用电脑"},
    )
    assert reused.status_code == 201, reused.text
    assert reused.json()["agent"]["id"] == first["agent"]["id"]
    assert reused.json()["agent"]["bot_id"] == second_bot["id"]
    assert reused.json()["agent"]["public_id"] != first["agent"]["public_id"]
    assert app.state.local_ai_service.authenticate(first["token"]) is None


def test_admin_local_ai_list_is_paginated(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-admin-page.db"))
    owner = _user(app, "pageowner")
    admin = _user(app, "pageadmin", role="admin")
    bot = _bot(app, owner, "page_bot")
    client = TestClient(app)
    headers = _auth(app, owner["username"])
    for number in range(3):
        assert client.post(
            "/api/local-ai/agents", headers=headers,
            json={"bot_id": bot["id"], "label": f"电脑{number}"},
        ).status_code == 201
    response = client.get(
        "/api/admin/local-ai/agents?page=2&per_page=2",
        headers=_auth(app, admin["username"]),
    )
    assert response.status_code == 200
    assert response.json()["total"] == 3
    assert response.json()["page"] == 2
    assert response.json()["per_page"] == 2
    assert len(response.json()["items"]) == 1


def test_active_agent_and_online_connection_caps_are_transactional(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setattr(
        local_ai_service_module, "LOCAL_AI_ROTATE_MAX_ATTEMPTS", 5
    )
    app = create_app(db_path=str(tmp_path / "local-ai-caps.db"))
    owner = _user(app, "capowner")
    bot = _bot(app, owner, "cap_bot")
    service = app.state.local_ai_service

    async def scenario() -> None:
        agents = []
        for number in range(8):
            agent, _ = await service.create(
                owner_id=owner["id"], bot_id=bot["id"], label=f"接入{number}"
            )
            agents.append(agent)
        with pytest.raises(ValueError, match="最多保留 8"):
            await service.create(
                owner_id=owner["id"], bot_id=bot["id"], label="第九个"
            )
        for agent in agents[:4]:
            await service.connect(app.state.store.get_local_ai_agent(agent["id"]))
        fifth = app.state.store.get_local_ai_agent(agents[4]["id"])
        with pytest.raises(ValueError, match="最多同时在线 4"):
            await service.connect(fifth)
        assert (await service.hub.status(fifth["public_id"])).online is False

    asyncio.run(scenario())


def test_global_online_connection_cap_is_transactional(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setattr(store_db_module, "LOCAL_AI_MAX_ONLINE_GLOBAL", 2)
    app = create_app(db_path=str(tmp_path / "local-ai-global-cap.db"))
    service = app.state.local_ai_service

    async def scenario() -> None:
        agents = []
        for number in range(3):
            owner = _user(app, f"globalcap{number}")
            bot = _bot(app, owner, f"global_cap_bot_{number}")
            agent, _ = await service.create(
                owner_id=owner["id"], bot_id=bot["id"], label=f"电脑{number}"
            )
            agents.append(app.state.store.get_local_ai_agent(agent["id"]))
        await service.connect(agents[0])
        await service.connect(agents[1])
        with pytest.raises(ValueError, match="在线连接已满"):
            await service.connect(agents[2])
        assert (await service.hub.status(agents[2]["public_id"])).online is False

    asyncio.run(scenario())


def test_pre_auth_gate_has_a_process_wide_inflight_cap():
    async def scenario() -> None:
        gate = LocalAIHandshakeGate()
        for number in range(16):
            assert await gate.begin(f"192.0.2.{number}") is True
        assert await gate.begin("198.51.100.1") is False
        await gate.end()
        assert await gate.begin("198.51.100.1") is True
        for _ in range(16):
            await gate.end()

    asyncio.run(scenario())


def test_rotate_business_limit_uses_stable_owner_and_agent_id(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-rotate-limit.db"))
    owner = _user(app, "rotateowner")
    bot = _bot(app, owner, "rotate_bot")
    service = app.state.local_ai_service

    async def scenario() -> None:
        agent, _ = await service.create(
            owner_id=owner["id"], bot_id=bot["id"], label="轮换测试"
        )
        for _ in range(5):
            result = await service.rotate(
                agent_id=agent["id"], owner_id=owner["id"]
            )
            assert result is not None
        with pytest.raises(LocalAIRateLimitError):
            await service.rotate(agent_id=agent["id"], owner_id=owner["id"])

    asyncio.run(scenario())


def test_rotate_rejects_claimed_agent_without_dropping_connection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-rotate-busy.db"))
    owner = _user(app, "rotatebusyowner")
    opponent = _user(app, "rotatebusyopponent")
    local_bot = _bot(app, owner, "rotate_busy_local")
    docker_bot = _bot(app, opponent, "rotate_busy_docker")

    async def no_dispatch():
        return {"outcome": "idle"}

    app.state.execution_dispatcher.run_once = no_dispatch
    with TestClient(app) as client:
        headers = _auth(app, owner["username"])
        created = client.post(
            "/api/local-ai/agents",
            headers=headers,
            json={"bot_id": local_bot["id"], "label": "比赛中的电脑"},
        ).json()
        agent = app.state.store.get_local_ai_agent(created["agent"]["id"])
        connection, generation = client.portal.call(
            app.state.local_ai_service.connect, agent
        )
        queued = client.post(
            "/api/matches/challenge",
            headers=headers,
            json={
                "my_bot_id": local_bot["id"],
                "opponent_bot_id": docker_bot["id"],
                "game_id": "gomoku",
                "my_environment": "remote_local",
                "opponent_environment": "platform_low",
                "my_local_agent_id": created["agent"]["public_id"],
            },
        )
        assert queued.status_code == 202, queued.text
        claimed = app.state.store.executions.claim_next(
            max_match_slots=1,
            max_sandbox_units=2,
            aging_seconds=60,
            user_active_limit=1,
            contest_share_slots=1,
        )
        assert claimed is not None
        assert claimed["public_id"] == queued.json()["public_id"]

        busy = client.post(
            f"/api/local-ai/agents/{created['agent']['public_id']}/rotate",
            headers=headers,
        )
        assert busy.status_code == 409
        assert "正在对局" in busy.json()["detail"]
        assert client.portal.call(
            app.state.local_ai_service.hub.status,
            created["agent"]["public_id"],
        ).online is True
        assert app.state.local_ai_service.authenticate(created["token"]) is not None

        app.state.store.executions.mark_cleanup_confirmed(
            claimed["public_id"], int(claimed["attempt_count"])
        )
        rotated = client.post(
            f"/api/local-ai/agents/{created['agent']['public_id']}/rotate",
            headers=headers,
        )
        assert rotated.status_code == 200, rotated.text
        assert rotated.json()["agent"]["public_id"] != created["agent"]["public_id"]
        assert app.state.local_ai_service.authenticate(created["token"]) is None
        assert client.portal.call(
            app.state.local_ai_service.hub.status,
            created["agent"]["public_id"],
        ).online is False
        # The old generation has already been durably disconnected by rotate;
        # this is idempotent cleanup for the synthetic transport fixture.
        client.portal.call(
            app.state.local_ai_service.disconnect,
            agent,
            connection.connection_id,
            generation,
        )


def test_local_agent_creation_is_owner_only_even_for_admin(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-owner.db"))
    owner = _user(app, "bindingowner")
    admin = _user(app, "bindingadmin", role="admin")
    bot = _bot(app, owner, "owner_only_bot")
    response = TestClient(app).post(
        "/api/local-ai/agents",
        headers=_auth(app, admin["username"]),
        json={"bot_id": bot["id"], "label": "管理员电脑"},
    )
    assert response.status_code == 400
    assert "只能为自己的 Bot" in response.json()["detail"]


def test_local_ai_client_download_requires_login_and_contains_no_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-download.db"))
    owner = _user(app, "downloadowner")
    client = TestClient(app)

    anonymous = client.get("/api/local-ai/client")
    assert anonymous.status_code in {401, 403}
    downloaded = client.get(
        "/api/local-ai/client", headers=_auth(app, owner["username"])
    )
    assert downloaded.status_code == 200
    _assert_private_no_store(downloaded)
    assert downloaded.headers["x-content-type-options"] == "nosniff"
    assert downloaded.headers["content-type"].startswith("text/x-python")
    assert (
        'filename="local_ai_client.py"'
        in downloaded.headers["content-disposition"]
    )
    assert "BZ_LOCAL_AI_TOKEN" in downloaded.text
    assert "bzlai_" not in downloaded.text
    assert "token=" not in downloaded.text


def test_local_ai_websocket_rejects_url_and_browser_credentials(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-ws-auth.db"))
    owner = _user(app, "wsowner")
    bot = _bot(app, owner, "ws_owner_bot")
    client = TestClient(app)
    created = client.post(
        "/api/local-ai/agents",
        headers=_auth(app, owner["username"]),
        json={"bot_id": bot["id"], "label": "本地调试"},
    ).json()
    token = created["token"]
    bearer = {"Authorization": f"Bearer {token}"}

    cases = (
        ("/api/local-ai/connect", {}),
        (f"/api/local-ai/connect?token={token}", bearer),
        (
            "/api/local-ai/connect",
            {**bearer, "Origin": "https://bot.example"},
        ),
    )
    for url, headers in cases:
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(url, headers=headers):
                pass
        assert denied.value.code == 1008
        assert denied.value.reason == "invalid_credentials"

    with client.websocket_connect(
        "/api/local-ai/connect", headers=bearer
    ) as websocket:
        ready = websocket.receive_json()
        assert ready == {
            "type": "ready",
            "agent_id": created["agent"]["public_id"],
            "label": "本地调试",
            "game_id": "gomoku",
        }
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {"type": "pong"}


def test_local_ai_websocket_enforces_exact_request_binding(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-ws-binding.db"))
    owner = _user(app, "wsbinding")
    bot = _bot(app, owner, "ws_binding_bot")

    with TestClient(app) as client:
        created = client.post(
            "/api/local-ai/agents",
            headers=_auth(app, owner["username"]),
            json={"bot_id": bot["id"], "label": "赛前联调"},
        ).json()
        public_id = created["agent"]["public_id"]
        with client.websocket_connect(
            "/api/local-ai/connect",
            headers={"Authorization": f"Bearer {created['token']}"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "ready"
            future = client.portal.start_task_soon(
                functools.partial(
                    app.state.local_ai_service.hub.request_decision,
                    public_id,
                    request_id="request-binding-1",
                    match_id="match-binding-1",
                    seat=0,
                    turn=4,
                    deadline_at=time.monotonic() + 10,
                    input='{"requests":[{"x":-1,"y":-1}],"responses":[]}',
                )
            )
            turn = websocket.receive_json()
            assert turn["type"] == "turn"
            assert (turn["request_id"], turn["match_id"], turn["turn"]) == (
                "request-binding-1",
                "match-binding-1",
                4,
            )
            assert turn["seat"] == 1

            websocket.send_json(
                {
                    "type": "response",
                    "request_id": "request-binding-1",
                    "match_id": "different-match",
                    "turn": 4,
                    "output": '{"response":{"x":0,"y":0}}',
                }
            )
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "request_binding_mismatch",
            }
            websocket.send_json(
                {
                    "type": "response",
                    "request_id": "request-binding-1",
                    "match_id": "match-binding-1",
                    "turn": 4,
                    "output": '{"response":{"x":0,"y":0}}',
                }
            )
            assert websocket.receive_json() == {
                "type": "accepted",
                "request_id": "request-binding-1",
                "match_id": "match-binding-1",
                "turn": 4,
            }
            assert future.result(timeout=2) == '{"response":{"x":0,"y":0}}'


def test_local_ai_websocket_failure_is_bound_bounded_and_terminal(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-ws-failure.db"))
    owner = _user(app, "wsfailure")
    bot = _bot(app, owner, "ws_failure_bot")

    with TestClient(app) as client:
        created = client.post(
            "/api/local-ai/agents",
            headers=_auth(app, owner["username"]),
            json={"bot_id": bot["id"], "label": "本机故障测试"},
        ).json()
        public_id = created["agent"]["public_id"]
        with client.websocket_connect(
            "/api/local-ai/connect",
            headers={"Authorization": f"Bearer {created['token']}"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "ready"
            future = client.portal.start_task_soon(
                functools.partial(
                    app.state.local_ai_service.hub.request_decision,
                    public_id,
                    request_id="request-failure-1",
                    match_id="match-failure-1",
                    seat=0,
                    turn=2,
                    deadline_at=time.monotonic() + 10,
                    input='{"requests":[{}],"responses":[]}',
                )
            )
            turn = websocket.receive_json()
            assert turn["type"] == "turn"

            base_failure = {
                "type": "failure",
                "request_id": "request-failure-1",
                "match_id": "match-failure-1",
                "turn": 2,
                "reason": "bot_start_failed",
            }
            websocket.send_json({**base_failure, "match_id": "wrong-match"})
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "request_binding_mismatch",
            }
            websocket.send_json(
                {**base_failure, "detail": "/home/student/private-bot"}
            )
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "invalid_message",
            }
            websocket.send_json(
                {**base_failure, "reason": "private:/home/student/private-bot"}
            )
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "invalid_failure_reason",
            }

            websocket.send_json(base_failure)
            assert websocket.receive_json() == {
                "type": "accepted",
                "request_id": "request-failure-1",
                "match_id": "match-failure-1",
                "turn": 2,
            }
            with pytest.raises(LocalAITechnicalError) as failed:
                future.result(timeout=2)
            assert failed.value.error_code == "local_ai_unavailable"
            assert failed.value.failed_seat == 0

            websocket.send_json(base_failure)
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "request_closed",
            }


def test_local_ai_websocket_coalesces_heartbeat_db_writes(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-ws-heartbeat.db"))
    owner = _user(app, "heartbeatowner")
    bot = _bot(app, owner, "heartbeat_bot")
    client = TestClient(app)
    created = client.post(
        "/api/local-ai/agents",
        headers=_auth(app, owner["username"]),
        json={"bot_id": bot["id"], "label": "心跳测试"},
    ).json()
    original = app.state.store.touch_local_ai_agent
    calls = 0

    def counted_touch(agent_id: int, generation: int) -> bool:
        nonlocal calls
        calls += 1
        return original(agent_id, generation)

    monkeypatch.setattr(app.state.store, "touch_local_ai_agent", counted_touch)
    with client.websocket_connect(
        "/api/local-ai/connect",
        headers={"Authorization": f"Bearer {created['token']}"},
    ) as websocket:
        assert websocket.receive_json()["type"] == "ready"
        for _ in range(10):
            websocket.send_json({"type": "ping"})
            assert websocket.receive_json() == {"type": "pong"}
    assert calls == 0


def test_local_ai_websocket_limits_inbound_ping_bursts(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setattr(api_routes_module, "_LOCAL_AI_INBOUND_BURST", 2.0)
    monkeypatch.setattr(
        api_routes_module, "_LOCAL_AI_INBOUND_REFILL_PER_SECOND", 0.0
    )
    app = create_app(db_path=str(tmp_path / "local-ai-ws-burst.db"))
    owner = _user(app, "burstowner")
    bot = _bot(app, owner, "burst_bot")
    client = TestClient(app)
    created = client.post(
        "/api/local-ai/agents",
        headers=_auth(app, owner["username"]),
        json={"bot_id": bot["id"], "label": "突发测试"},
    ).json()
    with client.websocket_connect(
        "/api/local-ai/connect",
        headers={"Authorization": f"Bearer {created['token']}"},
    ) as websocket:
        assert websocket.receive_json()["type"] == "ready"
        for _ in range(2):
            websocket.send_json({"type": "ping"})
            assert websocket.receive_json() == {"type": "pong"}
        websocket.send_json({"type": "ping"})
        assert websocket.receive_json() == {
            "type": "reject",
            "reason": "rate_limit_exceeded",
        }


def test_local_ai_websocket_does_not_rate_limit_referee_bound_turns(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setattr(api_routes_module, "_LOCAL_AI_INBOUND_BURST", 2.0)
    monkeypatch.setattr(
        api_routes_module, "_LOCAL_AI_INBOUND_REFILL_PER_SECOND", 0.0
    )
    app = create_app(db_path=str(tmp_path / "local-ai-ws-fast-turns.db"))
    owner = _user(app, "fastturnowner")
    bot = _bot(app, owner, "fast_turn_bot")

    with TestClient(app) as client:
        created = client.post(
            "/api/local-ai/agents",
            headers=_auth(app, owner["username"]),
            json={"bot_id": bot["id"], "label": "快速合法回合"},
        ).json()
        public_id = created["agent"]["public_id"]
        with client.websocket_connect(
            "/api/local-ai/connect",
            headers={"Authorization": f"Bearer {created['token']}"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "ready"
            for turn_number in range(1, 26):
                request_id = f"fast-bound-turn-{turn_number}"
                future = client.portal.start_task_soon(
                    functools.partial(
                        app.state.local_ai_service.hub.request_decision,
                        public_id,
                        request_id=request_id,
                        match_id="fast-bound-match",
                        seat=turn_number % 2,
                        turn=turn_number,
                        deadline_at=time.monotonic() + 10,
                        input='{"requests":[{}],"responses":[]}',
                    )
                )
                assert websocket.receive_json()["request_id"] == request_id
                output = f'{{"response":{{"x":{turn_number},"y":0}}}}'
                websocket.send_json(
                    {
                        "type": "response",
                        "request_id": request_id,
                        "match_id": "fast-bound-match",
                        "turn": turn_number,
                        "output": output,
                    }
                )
                assert websocket.receive_json() == {
                    "type": "accepted",
                    "request_id": request_id,
                    "match_id": "fast-bound-match",
                    "turn": turn_number,
                }
                assert future.result(timeout=2) == output

            # Unsolicited heartbeats still consume the same bounded bucket.
            for _ in range(2):
                websocket.send_json({"type": "ping"})
                assert websocket.receive_json() == {"type": "pong"}
            websocket.send_json({"type": "ping"})
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "rate_limit_exceeded",
            }


def test_local_ai_websocket_only_refunds_an_accepted_bound_turn(
    tmp_path, monkeypatch
):
    """Rejected and duplicate frames must still exhaust the abuse bucket."""

    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    monkeypatch.setattr(api_routes_module, "_LOCAL_AI_INBOUND_BURST", 2.0)
    monkeypatch.setattr(
        api_routes_module, "_LOCAL_AI_INBOUND_REFILL_PER_SECOND", 0.0
    )
    app = create_app(db_path=str(tmp_path / "local-ai-ws-refund.db"))
    owner = _user(app, "refundowner")
    bot = _bot(app, owner, "refund_bot")

    with TestClient(app) as client:
        created = client.post(
            "/api/local-ai/agents",
            headers=_auth(app, owner["username"]),
            json={"bot_id": bot["id"], "label": "退款边界"},
        ).json()
        public_id = created["agent"]["public_id"]
        with client.websocket_connect(
            "/api/local-ai/connect",
            headers={"Authorization": f"Bearer {created['token']}"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "ready"
            future = client.portal.start_task_soon(
                functools.partial(
                    app.state.local_ai_service.hub.request_decision,
                    public_id,
                    request_id="refund-request",
                    match_id="refund-match",
                    seat=0,
                    turn=1,
                    deadline_at=time.monotonic() + 10,
                    input='{"requests":[{}],"responses":[]}',
                )
            )
            assert websocket.receive_json()["request_id"] == "refund-request"
            response = {
                "type": "response",
                "request_id": "refund-request",
                "match_id": "refund-match",
                "turn": 1,
                "output": '{"response":{"x":0,"y":0}}',
            }
            websocket.send_json(response)
            assert websocket.receive_json()["type"] == "accepted"
            assert future.result(timeout=2) == response["output"]

            websocket.send_json({**response, "match_id": "wrong-match"})
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "request_binding_mismatch",
            }
            websocket.send_json(response)
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "duplicate_response",
            }
            websocket.send_json({"type": "ping"})
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "rate_limit_exceeded",
            }


def test_local_ai_websocket_closes_after_oversized_message(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-ws-size.db"))
    owner = _user(app, "sizeowner")
    bot = _bot(app, owner, "size_bot")
    client = TestClient(app)
    created = client.post(
        "/api/local-ai/agents",
        headers=_auth(app, owner["username"]),
        json={"bot_id": bot["id"], "label": "消息上限"},
    ).json()
    with client.websocket_connect(
        "/api/local-ai/connect",
        headers={"Authorization": f"Bearer {created['token']}"},
    ) as websocket:
        assert websocket.receive_json()["type"] == "ready"
        websocket.send_text(
            "x"
            * (api_routes_module.MAX_LOCAL_AI_WEBSOCKET_MESSAGE_BYTES + 1)
        )
        assert websocket.receive_json() == {
            "type": "reject",
            "reason": "message_too_large",
        }
        with pytest.raises(WebSocketDisconnect) as exc_info:
            websocket.receive_json()
        assert exc_info.value.code == 1009


def test_local_ai_websocket_ready_failure_releases_live_state(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-ws-ready-fail.db"))
    owner = _user(app, "readyfailowner")
    bot = _bot(app, owner, "readyfail_bot")
    client = TestClient(app, raise_server_exceptions=False)
    created = client.post(
        "/api/local-ai/agents",
        headers=_auth(app, owner["username"]),
        json={"bot_id": bot["id"], "label": "发送失败"},
    ).json()

    async def failing_send_json(*_args, **_kwargs):
        raise RuntimeError("simulated ready send failure")

    monkeypatch.setattr(
        "starlette.websockets.WebSocket.send_json", failing_send_json
    )
    with pytest.raises((WebSocketDisconnect, RuntimeError)):
        with client.websocket_connect(
            "/api/local-ai/connect",
            headers={"Authorization": f"Bearer {created['token']}"},
        ) as websocket:
            websocket.receive_json()

    agent = app.state.store.get_local_ai_agent(created["agent"]["id"])
    assert agent["disconnected_at"] is not None
    assert asyncio.run(
        app.state.local_ai_service.hub.status(created["agent"]["public_id"])
    ).online is False


def test_local_ai_websocket_pre_auth_gate_runs_before_db_lookup(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-ws-gate.db"))
    service = app.state.local_ai_service
    calls = 0
    original = service.authenticate

    def counted_authenticate(token: str):
        nonlocal calls
        calls += 1
        return original(token)

    monkeypatch.setattr(service, "authenticate", counted_authenticate)
    client = TestClient(app)
    for _ in range(20):
        with pytest.raises(WebSocketDisconnect) as denied:
            with client.websocket_connect(
                "/api/local-ai/connect",
                headers={"Authorization": "Bearer bzlai_" + "x" * 44},
            ):
                pass
        assert denied.value.code == 1008
        assert denied.value.reason == "invalid_credentials"
    assert calls == 20
    with pytest.raises(WebSocketDisconnect) as limited:
        with client.websocket_connect(
            "/api/local-ai/connect",
            headers={"Authorization": "Bearer bzlai_" + "y" * 44},
        ):
            pass
    assert limited.value.code == 1008
    assert limited.value.reason == "invalid_credentials"
    assert calls == 20


def test_disabled_owner_or_bot_cannot_authenticate_or_stay_connected(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-disable.db"))
    owner = _user(app, "disableowner")
    admin = _user(app, "disableadmin", role="admin")
    bot = _bot(app, owner, "disable_bot")
    client = TestClient(app)
    created = client.post(
        "/api/local-ai/agents",
        headers=_auth(app, owner["username"]),
        json={"bot_id": bot["id"], "label": "停用测试"},
    ).json()
    token = created["token"]
    assert app.state.local_ai_service.authenticate(token) is not None

    disabled = client.patch(
        f"/api/admin/bots/{bot['id']}",
        headers=_auth(app, admin["username"]),
        json={"is_active": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert app.state.local_ai_service.authenticate(token) is None
    agent = app.state.store.get_local_ai_agent(created["agent"]["id"])
    assert agent["status"] == "revoked"


def test_disabling_owner_revokes_live_connection_and_releases_projection(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-owner-disable.db"))
    owner = _user(app, "ownerdisable")
    admin = _user(app, "ownerdisableadmin", role="admin")
    bot = _bot(app, owner, "owner_disable_bot")
    service = app.state.local_ai_service
    client = TestClient(app)
    created = client.post(
        "/api/local-ai/agents",
        headers=_auth(app, owner["username"]),
        json={"bot_id": bot["id"], "label": "停用断连"},
    ).json()
    agent = app.state.store.get_local_ai_agent(created["agent"]["id"])

    async def connect() -> None:
        await service.connect(agent)
        assert service.is_available_now(agent["id"]) is True

    asyncio.run(connect())
    response = client.patch(
        f"/api/admin/users/{owner['id']}",
        headers=_auth(app, admin["username"]),
        json={"is_active": False},
    )
    assert response.status_code == 200, response.text
    assert service.authenticate(created["token"]) is None
    assert service.is_available_now(agent["id"]) is False
    assert asyncio.run(service.hub.status(agent["public_id"])).online is False


def test_remote_challenge_checks_owner_and_live_state_and_is_unrated(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-challenge.db"))
    owner = _user(app, "challengeowner")
    foreign = _user(app, "challengeforeign")
    admin = _user(app, "challengeadmin", role="admin")
    local_bot = _bot(app, owner, "challenge_local")
    second_local_bot = _bot(app, owner, "challenge_second_local")
    foreign_bot = _bot(app, foreign, "challenge_foreign")
    service = app.state.local_ai_service

    # Keep the durable queue accepting while preventing the background test
    # dispatcher from actually running the queued local Bot jobs.
    async def no_dispatch():
        return {"outcome": "idle"}

    app.state.execution_dispatcher.run_once = no_dispatch
    with TestClient(app) as client:
        first, _ = client.portal.call(
            functools.partial(
                service.create,
                owner_id=owner["id"],
                bot_id=local_bot["id"],
                label="主力本机",
            )
        )
        second, _ = client.portal.call(
            functools.partial(
                service.create,
                owner_id=owner["id"],
                bot_id=second_local_bot["id"],
                label="备用本机",
            )
        )
        offline, _ = client.portal.call(
            functools.partial(
                service.create,
                owner_id=owner["id"],
                bot_id=local_bot["id"],
                label="离线连接",
            )
        )
        first_connection, first_generation = client.portal.call(
            service.connect,
            app.state.store.get_local_ai_agent(first["id"]),
        )
        second_connection, second_generation = client.portal.call(
            service.connect,
            app.state.store.get_local_ai_agent(second["id"]),
        )
        owner_headers = _auth(app, owner["username"])

        local_local = client.post(
            "/api/matches/challenge",
            headers=owner_headers,
            json={
                "my_bot_id": local_bot["id"],
                "opponent_bot_id": second_local_bot["id"],
                "game_id": "gomoku",
                "my_environment": "remote_local",
                "opponent_environment": "remote_local",
                "my_local_agent_id": first["public_id"],
                "opponent_local_agent_id": second["public_id"],
            },
        )
        assert local_local.status_code == 202, local_local.text
        request = local_local.json()["request"]
        assert (
            request["bot_a_environment"],
            request["bot_b_environment"],
            request["sandbox_units"],
            request["rated"],
            request["rating_reason"],
        ) == ("remote_local", "remote_local", 0, False, "remote_local")
        serialized = json.dumps(local_local.json(), ensure_ascii=False)
        assert first["public_id"] not in serialized
        assert second["public_id"] not in serialized
        assert "local_agent" not in serialized

        mixed = client.post(
            "/api/matches/challenge",
            headers=owner_headers,
            json={
                "my_bot_id": local_bot["id"],
                "opponent_bot_id": foreign_bot["id"],
                "game_id": "gomoku",
                "my_environment": "remote_local",
                "opponent_environment": "platform_low",
                "my_local_agent_id": first["public_id"],
            },
        )
        assert mixed.status_code == 202, mixed.text
        assert mixed.json()["request"]["sandbox_units"] == 1
        assert mixed.json()["request"]["rated"] is False

        reversed_mixed = client.post(
            "/api/matches/challenge",
            headers=owner_headers,
            json={
                "my_bot_id": local_bot["id"],
                "opponent_bot_id": foreign_bot["id"],
                "my_seat": 1,
                "game_id": "gomoku",
                "my_environment": "remote_local",
                "opponent_environment": "platform_low",
                "my_local_agent_id": first["public_id"],
            },
        )
        assert reversed_mixed.status_code == 202, reversed_mixed.text
        assert (
            reversed_mixed.json()["request"]["bot_a_environment"],
            reversed_mixed.json()["request"]["bot_b_environment"],
            reversed_mixed.json()["request"]["sandbox_units"],
            reversed_mixed.json()["request"]["rated"],
            reversed_mixed.json()["request"]["rating_reason"],
        ) == ("platform_low", "remote_local", 1, False, "remote_local")
        reversed_row = app.state.store._conn.execute(
            "SELECT bot_a_id,bot_b_id,bot_a_local_agent_id,bot_b_local_agent_id "
            "FROM execution_jobs WHERE public_id=?",
            (reversed_mixed.json()["public_id"],),
        ).fetchone()
        assert tuple(reversed_row) == (
            foreign_bot["id"],
            local_bot["id"],
            None,
            first["id"],
        )

        offline_response = client.post(
            "/api/matches/challenge",
            headers=owner_headers,
            json={
                "my_bot_id": local_bot["id"],
                "opponent_bot_id": foreign_bot["id"],
                "game_id": "gomoku",
                "my_environment": "remote_local",
                "my_local_agent_id": offline["public_id"],
            },
        )
        assert offline_response.status_code == 400
        assert "离线或正在处理" in offline_response.json()["detail"]

        # Admin may select platform Bots globally, but cannot borrow another
        # participant's local process identity.
        borrowed = client.post(
            "/api/matches/challenge",
            headers=_auth(app, admin["username"]),
            json={
                "my_bot_id": local_bot["id"],
                "opponent_bot_id": foreign_bot["id"],
                "game_id": "gomoku",
                "my_environment": "remote_local",
                "my_local_agent_id": first["public_id"],
            },
        )
        assert borrowed.status_code == 400
        assert "当前用户或 Bot 不匹配" in borrowed.json()["detail"]
        client.portal.call(
            service.disconnect,
            app.state.store.get_local_ai_agent(first["id"]),
            first_connection.connection_id,
            first_generation,
        )
        client.portal.call(
            service.disconnect,
            app.state.store.get_local_ai_agent(second["id"]),
            second_connection.connection_id,
            second_generation,
        )


def test_public_match_projects_environments_without_local_agent_bindings(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "local-ai-public.db"))
    first_owner = _user(app, "publicfirst")
    second_owner = _user(app, "publicsecond")
    bot_a = _bot(app, first_owner, "public_a")
    bot_b = _bot(app, second_owner, "public_b")
    secret_binding = "internal-local-agent-secret"
    app.state.store.create_match(
        "public-environment-match",
        bot_a["id"],
        bot_b["id"],
        owner_id=first_owner["id"],
        game_id="gomoku",
        match_config={
            "_bot_a_environment": "remote_local",
            "_bot_b_environment": "platform_high",
            "_bot_a_local_agent_id": secret_binding,
            "_bot_b_local_agent_id": 987654,
            "_internal_runtime_note": "do-not-publish",
        },
    )
    app.state.store.update_match(
        "public-environment-match", status="completed", winner=0
    )
    client = TestClient(app)

    detail = client.get("/api/matches/public-environment-match")
    assert detail.status_code == 200
    match = detail.json()["match"]
    assert (match["bot_a_environment"], match["bot_b_environment"]) == (
        "remote_local",
        "platform_high",
    )
    assert "match_config" not in match
    assert secret_binding not in detail.text
    assert "local_agent_id" not in detail.text
    assert "do-not-publish" not in detail.text

    listing = client.get("/api/matches?game_id=gomoku")
    assert listing.status_code == 200
    row = next(
        item
        for item in listing.json()["matches"]
        if item["id"] == "public-environment-match"
    )
    assert (row["bot_a_environment"], row["bot_b_environment"]) == (
        "remote_local",
        "platform_high",
    )
    assert secret_binding not in listing.text
    assert "local_agent_id" not in listing.text


def test_historical_match_environment_defaults_to_actual_legacy_runtime():
    from bzplat.backend.store.public_contract import sanitize_public_match

    ordinary = sanitize_public_match(
        {"match_type": "challenge", "status": "completed", "reason": "completed"}
    )
    assert (ordinary["bot_a_environment"], ordinary["bot_b_environment"]) == (
        "platform_low",
        "platform_low",
    )

    human = sanitize_public_match(
        {
            "match_type": "human",
            "human_seat": 1,
            "status": "completed",
            "reason": "completed",
        }
    )
    assert (human["bot_a_environment"], human["bot_b_environment"]) == (
        "platform_low",
        "human",
    )
