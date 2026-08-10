"""自动排位公开队列与唯一管理员开关 API。"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import rating_projection_digests


def _user_and_token(app, username: str, role: str = "user"):
    user = app.state.store.create_user(
        username,
        f"{username}@example.com",
        hash_password("password12"),
        role=role,
    )
    app.state.store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate(username, "password12")
    return user, token


def _bot(app, owner_id: int, key: str, game_id: str):
    path = Path(app.state.store.path).parent / f"api-{key}.elf"
    path.write_bytes(b"test binary fixture")
    bot = app.state.store.create_bot(
        owner_id,
        f"bot-{key}",
        binary_path=str(path),
        format="elf",
        game_id=game_id,
    )
    app.state.store.add_bot_version(bot["id"], binary_path=str(path))
    app.state.store.ensure_rating(bot["id"], game_id=game_id)
    return bot


def _mark_projection_verified(app) -> None:
    with app.state.store._tx() as conn:
        live = rating_projection_digests(conn)
        assert live["issues"] == []
        conn.execute(
            "UPDATE rating_projection_state SET policy_version='owner-neutral-v2',"
            "rebuilt_at='test',source_settlement_count=?,"
            "source_last_settled_order=?,source_digest=?,projection_digest=?,"
            "plan_digest=? WHERE singleton=1",
            (
                live["source_settlement_count"],
                live["source_last_settled_order"],
                live["source_digest"],
                live["projection_digest"],
                live["plan_digest"],
            ),
        )


def test_public_queue_is_filterable_ordered_and_sanitized(tmp_path):
    app = create_app(db_path=str(tmp_path / "public.db"), max_concurrent=2)
    for game in ("holdem", "gomoku"):
        for index in range(2):
            user, _ = _user_and_token(app, f"{game}-owner-{index}")
            _bot(app, user["id"], f"{game}-{index}", game)
    token = "api-read-leader"
    _mark_projection_verified(app)
    lease = app.state.store.acquire_auto_match_dispatcher(token, lease_seconds=30)
    app.state.store.refill_auto_match_queue(
        target_queued=2, placement_required=10, dispatcher_token=token,
        dispatcher_epoch=int(lease["lease_epoch"]),
    )
    client = TestClient(app)

    response = client.get("/api/auto-match/queue?game_id=holdem")
    assert response.status_code == 200
    payload = response.json()
    assert payload["game_id"] == "holdem"
    assert payload["enabled"] is True
    assert payload["policy"]["serial"] is True
    assert payload["active"] is None
    assert payload["upcoming"]
    assert all(row["game_id"] == "holdem" for row in payload["upcoming"])
    row = payload["upcoming"][0]
    assert row["position"] >= 1
    assert set(row) >= {
        "id", "status", "position", "game_id", "reason", "bot_a", "bot_b"
    }
    serialized = response.text
    for secret_field in (
        "version_id", "dispatcher_token", "decision_id", "binary_path"
    ):
        assert secret_field not in serialized

    assert client.get("/api/auto-match/queue?game_id=unknown").status_code == 400


def test_admin_toggle_is_strict_rbac_audited_and_persistent(tmp_path):
    app = create_app(db_path=str(tmp_path / "toggle.db"), max_concurrent=2)
    _, admin_token = _user_and_token(app, "toggle-admin", role="admin")
    _, user_token = _user_and_token(app, "toggle-user")
    client = TestClient(app)

    assert client.put(
        "/api/admin/auto-match",
        json={"enabled": False},
        headers={"Authorization": f"Bearer {user_token}"},
    ).status_code == 403
    assert client.put(
        "/api/admin/auto-match",
        json={"enabled": "false"},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).status_code == 422
    assert client.put(
        "/api/admin/auto-match",
        json={"enabled": False, "daily_cap": 1},
        headers={"Authorization": f"Bearer {admin_token}"},
    ).status_code == 422

    with mock.patch("bzplat.backend.api_routes.audit_log") as audit:
        response = client.put(
            "/api/admin/auto-match",
            json={"enabled": False},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert response.status_code == 200
    assert response.json()["enabled"] is False
    assert app.state.store.get_auto_match_enabled() is False
    audit.assert_called_once()
    assert audit.call_args.args[1] == "admin_auto_match_toggle"
    assert audit.call_args.kwargs["result"] == "ok"

    reopened = type(app.state.store)(app.state.store.path)
    assert reopened.get_auto_match_enabled() is False
    reopened.close()


def test_qa_capability_guard_rejects_enable_but_allows_disable(monkeypatch, tmp_path):
    monkeypatch.setenv("BZ_QA_INSTANCE", "1")
    app = create_app(db_path=str(tmp_path / "qa.db"), max_concurrent=2)
    _, admin_token = _user_and_token(app, "qa-toggle-admin", role="admin")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {admin_token}"}

    denied = client.put(
        "/api/admin/auto-match", json={"enabled": True}, headers=headers
    )
    assert denied.status_code == 409
    disabled = client.put(
        "/api/admin/auto-match", json={"enabled": False}, headers=headers
    )
    assert disabled.status_code == 200
    assert disabled.json()["effective_enabled"] is False


def test_same_owner_challenge_response_and_detail_are_explicitly_neutral(
    monkeypatch, tmp_path
):
    app = create_app(db_path=str(tmp_path / "neutral-api.db"), max_concurrent=2)
    owner, token = _user_and_token(app, "neutral-owner")
    bot_a = _bot(app, owner["id"], "neutral-a", "gomoku")
    bot_b = _bot(app, owner["id"], "neutral-b", "gomoku")
    original = app.state.orch.challenge

    async def deferred(*args, **kwargs):
        kwargs["defer_start"] = True
        return await original(*args, **kwargs)

    monkeypatch.setattr(app.state.orch, "challenge", deferred)
    client = TestClient(app)
    response = client.post(
        "/api/matches/challenge",
        json={"my_bot_id": bot_a["id"], "opponent_bot_id": bot_b["id"]},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    created = response.json()
    assert created["rated"] is False
    assert created["rating_reason"] == "same_owner"

    detail = client.get(f"/api/matches/{created['match_id']}")
    assert detail.status_code == 200
    payload = detail.json()["match"]
    assert payload["rated"] is False
    assert payload["rating_reason"] == "same_owner"
    app.state.orch.release_prepared_match_slot(created["match_id"])
    app.state.store.delete_match(created["match_id"])
