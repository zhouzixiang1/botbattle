"""Shared-cache boundaries for public endpoints with identity-shaped output."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app


def _vary_fields(response) -> set[str]:
    return {
        field.strip().lower()
        for field in response.headers.get("vary", "").split(",")
        if field.strip()
    }


def _assert_auth_vary(response) -> None:
    assert {"authorization", "cookie"} <= _vary_fields(response)


@pytest.fixture
def cache_context(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "auth-vary.db"))
    store = app.state.store
    owner = store.create_user(
        "cacheowner",
        "cacheowner@example.test",
        hash_password("password1"),
    )
    store.update_user(owner["id"], role="organizer", email_verified=1)
    bot = store.create_bot(
        owner["id"],
        "cachebot",
        binary_path="/private/cachebot.bin",
        format="elf",
        game_id="holdem",
        runtime_mode="longrunning",
    )
    public_contest = store.create_contest(
        "cache public contest",
        owner["id"],
        game_id="holdem",
        status="open",
    )
    hidden_contest = store.create_contest(
        "cache hidden contest",
        owner["id"],
        game_id="holdem",
        status="draft",
    )
    _, token = app.state.auth.authenticate("cacheowner", "password1")
    return {
        "app": app,
        "owner": owner,
        "bot": bot,
        "public_contest": public_contest,
        "hidden_contest": hidden_contest,
        "owner_headers": {"Authorization": f"Bearer {token}"},
    }


def _bot_from_response(path: str, response, bot_id: int) -> dict:
    payload = response.json()
    if path.endswith("/profile"):
        return payload["profile"]
    if path == f"/api/bots/{bot_id}":
        return payload["bot"]
    return next(bot for bot in payload["bots"] if bot["id"] == bot_id)


@pytest.mark.parametrize("owner_first", [False, True])
def test_bot_identity_responses_vary_for_guest_owner_in_both_orders(
    cache_context, owner_first: bool
):
    context = cache_context
    bot_id = context["bot"]["id"]
    paths = (
        "/api/bots/public?game_id=holdem",
        "/api/users/cacheowner/bots",
        f"/api/bots/{bot_id}",
        f"/api/bots/{bot_id}/profile",
    )
    order = ("owner", "guest") if owner_first else ("guest", "owner")

    with TestClient(context["app"]) as client:
        for path in paths:
            responses = {}
            for identity in order:
                headers = (
                    context["owner_headers"] if identity == "owner" else {}
                )
                responses[identity] = client.get(path, headers=headers)

            assert responses["guest"].status_code == 200
            assert responses["owner"].status_code == 200
            _assert_auth_vary(responses["guest"])
            _assert_auth_vary(responses["owner"])
            guest_bot = _bot_from_response(path, responses["guest"], bot_id)
            owner_bot = _bot_from_response(path, responses["owner"], bot_id)
            assert "binary_path" not in guest_bot
            assert "runtime_mode" not in guest_bot
            assert owner_bot["binary_path"] == "/private/cachebot.bin"
            assert owner_bot["runtime_mode"] == "longrunning"


@pytest.mark.parametrize("owner_first", [False, True])
def test_contest_identity_and_hidden_acl_responses_vary_in_both_orders(
    cache_context, owner_first: bool
):
    context = cache_context
    public_id = context["public_contest"]["id"]
    hidden_id = context["hidden_contest"]["id"]
    order = ("owner", "guest") if owner_first else ("guest", "owner")

    def fetch(client: TestClient, identity: str, path: str):
        headers = context["owner_headers"] if identity == "owner" else {}
        return client.get(path, headers=headers)

    with TestClient(context["app"]) as client:
        list_responses = {
            identity: fetch(client, identity, "/api/contests")
            for identity in order
        }
        detail_responses = {
            identity: fetch(client, identity, f"/api/contests/{public_id}")
            for identity in order
        }
        bracket_responses = {
            identity: fetch(
                client, identity, f"/api/contests/{hidden_id}/bracket"
            )
            for identity in order
        }

    for response in (*list_responses.values(), *detail_responses.values()):
        assert response.status_code == 200
        _assert_auth_vary(response)
    for response in bracket_responses.values():
        _assert_auth_vary(response)

    guest_titles = {
        contest["title"] for contest in list_responses["guest"].json()["contests"]
    }
    owner_titles = {
        contest["title"] for contest in list_responses["owner"].json()["contests"]
    }
    assert "cache hidden contest" not in guest_titles
    assert "cache hidden contest" in owner_titles
    assert detail_responses["guest"].json()["is_organizer"] is False
    assert detail_responses["owner"].json()["is_organizer"] is True
    assert bracket_responses["guest"].status_code == 404
    assert bracket_responses["owner"].status_code == 200
