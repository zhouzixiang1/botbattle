"""现行游戏规则只有一套：常量固定，旧覆盖字段明确拒绝。"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.games import registry
from bzplat.backend.games.gomoku.engine import BOARD_SIZE
from bzplat.backend.games.holdem.engine import (
    BIG_BLIND,
    DEFAULT_HANDS,
    SMALL_BLIND,
    STARTING_STACK,
)
from bzplat.backend.games.pencil.engine import DEFAULT_N
from bzplat.backend.main import create_app


def _client(tmp_path, *, role: str = "admin"):
    app = create_app(db_path=str(tmp_path / f"canonical-{role}.db"), max_concurrent=1)
    store = app.state.store
    user = store.create_user(
        f"canonical_{role}",
        f"canonical_{role}@example.com",
        hash_password("password12"),
        role=role,
    )
    store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate(user["username"], "password12")
    client = TestClient(app)
    client.headers["Authorization"] = f"Bearer {token}"
    return client, app, user


def _two_bots(app, user):
    store = app.state.store
    a = store.create_bot(
        user["id"], "canonical_a", binary_path="/tmp/a", format="elf", game_id="holdem"
    )
    b = store.create_bot(
        user["id"], "canonical_b", binary_path="/tmp/b", format="elf", game_id="holdem"
    )
    return a, b


def test_platform_rule_constants_are_canonical():
    assert (DEFAULT_HANDS, STARTING_STACK, SMALL_BLIND, BIG_BLIND) == (70, 20_000, 50, 100)
    assert BOARD_SIZE == 15
    assert DEFAULT_N == 6
    assert registry.get("pencil").time_budget_per_side == 900.0


@pytest.mark.parametrize(
    "legacy",
    [
        {"match_config": {"hands": 1}},
        {"hands": 1},
        {"hands_per_match": 1},
        {"num_hands": 1},
        {"starting_stack": 1_000},
        {"sb": 1},
        {"bb": 2},
        {"n_dots": 3},
        {"board_size": 9},
    ],
)
def test_challenge_rejects_every_legacy_rule_override(tmp_path, legacy):
    client, app, user = _client(tmp_path)
    a, b = _two_bots(app, user)
    response = client.post(
        "/api/matches/challenge",
        json={
            "my_bot_id": a["id"],
            "opponent_bot_id": b["id"],
            "game_id": "holdem",
            **legacy,
        },
    )
    assert response.status_code == 422, response.text
    assert "extra_forbidden" in response.text


def test_human_and_contest_creation_reject_legacy_rule_fields(tmp_path):
    client, app, user = _client(tmp_path, role="organizer")
    bot, _ = _two_bots(app, user)

    human = client.post(
        "/api/matches/human",
        json={"bot_id": bot["id"], "game_id": "holdem", "match_config": {}},
    )
    assert human.status_code == 422

    for legacy in ({"hands_per_match": 1}, {"match_config": {"hands": 1}}):
        contest = client.post(
            "/api/contests",
            json={
                "title": "固定规则赛事",
                "game_id": "holdem",
                "template_id": "holdem_swiss_ko",
                **legacy,
            },
        )
        assert contest.status_code == 422, contest.text

    nested = client.post(
        "/api/contests",
        json={
            "title": "嵌套覆盖",
            "game_id": "holdem",
            "stages": [{"type": "round_robin", "hands": 1}],
        },
    )
    assert nested.status_code == 400
    assert "不允许覆盖字段: hands" in nested.json()["detail"]


def test_admin_contest_and_template_apis_do_not_reintroduce_rule_editors(tmp_path):
    client, app, user = _client(tmp_path)
    contest = app.state.contest_manager.create(
        user["id"], "管理端固定规则", template_id="holdem_swiss_ko"
    )

    patch = client.patch(
        f"/api/admin/contests/{contest['id']}", json={"hands_per_match": 1}
    )
    assert patch.status_code == 422

    legacy_template = client.post(
        "/api/admin/templates",
        json={
            "id": "legacy_rules",
            "name": "Legacy Rules",
            "game_id": "holdem",
            "match_config": {},
            "stages": [{"type": "round_robin"}],
        },
    )
    assert legacy_template.status_code == 422

    nested_template = client.post(
        "/api/admin/templates",
        json={
            "id": "nested_rules",
            "name": "Nested Rules",
            "game_id": "pencil",
            "stages": [{"type": "round_robin", "n_dots": 4}],
        },
    )
    assert nested_template.status_code == 400
    assert "n_dots" in nested_template.json()["detail"]

    templates = client.get("/api/admin/templates").json()["templates"]
    assert templates and all("match_config" not in row for row in templates)
    contests = client.get("/api/admin/contests").json()["contests"]
    assert contests and all("hands_per_match" not in row for row in contests)
    assert all("match_config_json" not in row for row in contests)
    assert client.get("/api/admin/judges").status_code == 404
    assert client.patch("/api/admin/judges/params", json={"params": {}}).status_code == 404


def test_contest_responses_hide_historical_rule_columns(tmp_path):
    client, _app, _user = _client(tmp_path, role="organizer")
    created = client.post(
        "/api/contests",
        json={"title": "响应去旧字段", "template_id": "holdem_swiss_ko"},
    )
    assert created.status_code == 200, created.text
    contest = created.json()["contest"]
    assert "hands_per_match" not in contest
    assert "match_config_json" not in contest

    listed = client.get("/api/contests?status=draft").json()["contests"]
    assert listed and "hands_per_match" not in listed[0]
    assert "match_config_json" not in listed[0]

    detail = client.get(f"/api/contests/{contest['id']}")
    assert detail.status_code == 200
    payload = detail.json()["contest"]
    assert "hands_per_match" not in payload
    assert "match_config_json" not in payload
