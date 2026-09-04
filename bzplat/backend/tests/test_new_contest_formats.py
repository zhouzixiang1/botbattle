from __future__ import annotations

import asyncio
import copy
import json
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.store import db as store_db_module
from bzplat.backend.api_routes import _contest_for_api
from bzplat.backend.contests import manager as manager_module
from bzplat.backend.contests import presentation as presentation_module
from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.presentation import (
    _advancement_zone,
    _rank_rows,
    build_stage_summaries,
    current_stage_cohort_from_summaries,
    public_format_snapshot,
)
from bzplat.backend.contests.ranking import compute_cross_group_ranking
from bzplat.backend.contests.stages import frozen_group_round_robin
from bzplat.backend.contests.templates import get_template
from bzplat.backend.contests.validation import reserved_group_markers_match_template
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store
from bzplat.backend.store.schema import validate_contest_title


class _NoDispatchOrchestrator:
    async def challenge(self, *args, **kwargs):  # pragma: no cover - publish never dispatches
        raise AssertionError("publish must not dispatch")


class _PersistingContestOrchestrator:
    """Create prepared contest Matches without running Bot processes."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.count = 0

    async def challenge(
        self,
        bot_a_id,
        bot_b_id,
        owner_user_id,
        *,
        match_type="contest",
        contest_id=None,
        game_id=None,
        **kwargs,
    ):
        self.count += 1
        match_id = f"format-contest-{contest_id}-{self.count}"
        self.store.create_match(
            match_id,
            bot_a_id,
            bot_b_id,
            owner_id=owner_user_id,
            contest_id=contest_id,
            match_type=match_type,
            game_id=game_id,
            match_config={"time_control_id": kwargs["time_control_id"]},
        )
        return match_id


class _SingleSlotEstimator:
    max_concurrent = 1


@pytest.mark.parametrize("title", ("\ud800", "left\udfffright"))
def test_contest_title_rejects_utf16_surrogates_at_shared_write_boundaries(
    tmp_path, title
):
    assert any(0xD800 <= ord(char) <= 0xDFFF for char in title)
    with pytest.raises(ValueError, match="赛事标题"):
        validate_contest_title(title)

    store = Store(str(tmp_path / "surrogate-title-boundaries.db"))
    organizer = store.create_user(
        "surrogate-title-organizer",
        "surrogate-title-organizer@example.com",
        "hash",
        role="organizer",
    )
    manager = ContestManager(store, _NoDispatchOrchestrator())
    with pytest.raises(ValueError, match="赛事标题"):
        manager.create(organizer["id"], title)
    with pytest.raises(ValueError, match="赛事标题"):
        store.create_contest(title, organizer["id"])
    store.close()


def test_contest_title_raw_json_surrogate_returns_422_and_unicode_boundary_stays_valid(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = create_app(db_path=str(tmp_path / "surrogate-title-api.db"))
    store = app.state.store
    organizer = store.create_user(
        "surrogate-title-api-organizer",
        "surrogate-title-api-organizer@example.com",
        hash_password("pw123456"),
        role="organizer",
    )
    store.update_user(organizer["id"], email_verified=1)
    _user, token = app.state.auth.authenticate(
        "surrogate-title-api-organizer", "pw123456"
    )
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

    valid = "界" * 119 + "🎮"
    assert len(valid) == 120
    assert validate_contest_title(valid) == valid
    assert ContestManager(store, _NoDispatchOrchestrator()).create(
        organizer["id"], valid
    )["title"] == valid

    with TestClient(app) as client:
        response = client.post(
            "/api/contests",
            headers=headers,
            content=b'{"title":"\\ud800"}',
        )
    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.content.isascii()
    assert b"\\ud800" in response.content
    detail = response.json()["detail"]
    assert len(detail) == 1
    assert detail[0]["type"] == "string_unicode"
    assert detail[0]["loc"] == ["body", "title"]
    assert detail[0]["msg"]
    assert detail[0]["input"] == "\ud800"
    assert store.list_contests(organizer_id=organizer["id"])[0]["title"] == valid
    store.close()


def _fixture_bot(store: Store, tmp_path: Path, index: int, *, game_id: str):
    user = store.create_user(
        f"format-user-{index}",
        f"format-{index}@example.com",
        "hash",
    )
    binary = tmp_path / f"format-bot-{index}"
    binary.write_bytes(b"contest format fixture")
    bot = store.create_bot(
        user["id"],
        f"format-bot-{index}",
        binary_path=str(binary),
        format="elf",
        game_id=game_id,
    )
    return user, bot


def _official_tiebreaks(
    points: int | float,
    seed: int,
    *,
    group_rank: int | None = None,
    draw_order: int | None = None,
) -> dict:
    tiebreaks = {
        "points": points,
        "buchholz": 0,
        "buchholz_cut1": 0,
        "sonneborn_berger": 0,
        "head_to_head": 0,
        "normalized_delta": 0,
        "technical_losses": 0,
        "seed": seed,
    }
    if group_rank is not None and draw_order is not None:
        tiebreaks.update(
            {
                "group_rank": group_rank,
                "points_rate": 0,
                "opponent_strength": 0,
                "normalized_delta_rate": 0,
                "technical_loss_rate": 0,
                "draw_order": draw_order,
            }
        )
    return tiebreaks


def _mark_imported_contest_finished(
    store: Store,
    contest_id: int,
    *,
    official_results_ready: bool | None = None,
) -> None:
    """Install an explicit historical terminal fixture outside product flow.

    Source-ranking and malformed-export tests need a pre-existing finished
    contest but do not exercise the lifecycle transition itself.  Keep that
    import visible and internally sealed instead of bypassing the dedicated
    decision transaction through ``Store.update_contest``.
    """
    ready_sql = (
        ",official_results_ready=?"
        if official_results_ready is not None
        else ""
    )
    params: tuple[object, ...] = (
        (int(official_results_ready), contest_id)
        if official_results_ready is not None
        else (contest_id,)
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            f"UPDATE contests SET status='finished'{ready_sql} WHERE id=?",
            params,
        )
        connection.execute(
            "UPDATE contests SET sealed_pairing_topology_revision="
            "pairing_topology_revision WHERE id=?",
            (contest_id,),
        )


def test_contest_format_api_creates_revises_and_freezes_time_and_groups(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = create_app(db_path=str(tmp_path / "format-api.db"))
    store = app.state.store
    organizer = store.create_user(
        "format-api-organizer",
        "format-api@example.com",
        hash_password("pw123456"),
        role="organizer",
    )
    store.update_user(organizer["id"], email_verified=1)
    _user, token = app.state.auth.authenticate(
        "format-api-organizer", "pw123456"
    )
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    templates_response = client.get(
        "/api/contests/templates?game=pencil"
    )
    assert templates_response.status_code == 200
    template = next(
        row
        for row in templates_response.json()["templates"]
        if row["id"] == "pencil_group_drr"
    )
    assert [control["id"] for control in template["time_controls"]] == [
        "pencil_per_side_total_900s_v1",
        "pencil_per_decision_1s_v1",
    ]
    assert template["stage_format_configs"] == [
        {"stage_key": "groups", "field": "group_count", "min": 2}
    ]

    created_response = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "API grouped Pencil",
            "game_id": "pencil",
            "template_id": "pencil_group_drr",
            "time_control_id": "pencil_per_decision_1s_v1",
            "stage_format_settings": {"groups": {"group_count": 3}},
        },
    )
    assert created_response.status_code == 200, created_response.text
    created = created_response.json()["contest"]
    assert created["time_control_id"] == "pencil_per_decision_1s_v1"
    assert created["time_control"] == {
        "id": "pencil_per_decision_1s_v1",
        "mode": "per_decision",
        "seconds": 1,
        "applies_to": "both_bots",
    }
    assert created["stage_format_settings"] == {
        "groups": {"group_count": 3}
    }

    revised_response = client.patch(
        f"/api/contests/{created['id']}",
        headers=headers,
        json={
            "time_control_id": "pencil_per_side_total_900s_v1",
            "stage_format_settings": {"groups": {"group_count": 2}},
        },
    )
    assert revised_response.status_code == 200, revised_response.text
    revised = revised_response.json()["contest"]
    assert revised["time_control_id"] == "pencil_per_side_total_900s_v1"
    assert revised["stage_format_settings"] == {
        "groups": {"group_count": 2}
    }

    cross_game = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "wrong game clock",
            "game_id": "pencil",
            "template_id": "pencil_group_drr",
            "time_control_id": "gomoku_per_side_total_300s_v1",
            "stage_format_settings": {"groups": {"group_count": 2}},
        },
    )
    assert cross_game.status_code == 400
    arbitrary_seconds = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "arbitrary seconds",
            "game_id": "pencil",
            "template_id": "pencil_group_drr",
            "time_control_id": "pencil_per_decision_1s_v1",
            "time_control_seconds": 2,
            "stage_format_settings": {"groups": {"group_count": 2}},
        },
    )
    assert arbitrary_seconds.status_code == 422
    for malformed_source in (True, 1.0, "1", 0, -1):
        malformed_source_response = client.post(
            "/api/contests",
            headers=headers,
            json={
                "title": "malformed source identity",
                "game_id": "pencil",
                "template_id": "pencil_group_drr",
                "source_contest_id": malformed_source,
                "stage_format_settings": {"groups": {"group_count": 2}},
            },
        )
        assert malformed_source_response.status_code == 422
    with pytest.raises(ValueError, match="零进度 CAS 或发布事务"):
        store.update_contest(
            created["id"], time_control_id="pencil_per_decision_1s_v1"
        )

    store.update_contest(created["id"], status="published")
    frozen_response = client.patch(
        f"/api/contests/{created['id']}",
        headers=headers,
        json={"time_control_id": "pencil_per_decision_1s_v1"},
    )
    assert frozen_response.status_code in {400, 409}
    assert store.get_contest(created["id"])["time_control_id"] == (
        "pencil_per_side_total_900s_v1"
    )
    client.close()
    store.close()


def test_pencil_navigation_source_is_optional_same_game_and_template_scoped(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = create_app(db_path=str(tmp_path / "pencil-navigation-source.db"))
    store = app.state.store
    organizer = store.create_user(
        "pencil-navigation-organizer",
        "pencil-navigation@example.com",
        hash_password("pw123456"),
        role="organizer",
    )
    store.update_user(organizer["id"], email_verified=1)
    _user, token = app.state.auth.authenticate(
        "pencil-navigation-organizer", "pw123456"
    )
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    source = store.create_contest(
        "independent pencil preliminary",
        organizer["id"],
        game_id="pencil",
        template_id="pencil_drr",
        status="draft",
    )
    with pytest.raises(ValueError, match="不能关联自身"):
        store.update_contest(source["id"], source_contest_id=source["id"])
    wrong_game = store.create_contest(
        "wrong game navigation source",
        organizer["id"],
        game_id="gomoku",
        template_id="board_rr",
        status="open",
    )
    other_organizer = store.create_user(
        "other-pencil-navigation-organizer",
        "other-pencil-navigation@example.com",
        hash_password("pw123456"),
        role="organizer",
    )
    hidden_foreign_source = store.create_contest(
        "other organizer hidden pencil source",
        other_organizer["id"],
        game_id="pencil",
        template_id="pencil_drr",
        status="draft",
    )

    templates = client.get("/api/contests/templates?game=pencil").json()[
        "templates"
    ]
    navigation_templates = {
        row["id"]: row.get("allows_navigation_source_contest")
        for row in templates
        if row["id"] in {"pencil_drr", "pencil_group_drr"}
    }
    assert navigation_templates == {
        "pencil_drr": True,
        "pencil_group_drr": True,
    }

    optional = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "independent pencil final without link",
            "game_id": "pencil",
            "template_id": "pencil_drr",
        },
    )
    assert optional.status_code == 200, optional.text
    assert optional.json()["contest"]["source_contest_id"] is None

    explicit_null = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "explicit null navigation source",
            "game_id": "pencil",
            "template_id": "pencil_drr",
            "source_contest_id": None,
        },
    )
    assert explicit_null.status_code == 400

    linked = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "independent pencil final with navigation",
            "game_id": "pencil",
            "template_id": "pencil_group_drr",
            "source_contest_id": source["id"],
            "stage_format_settings": {"groups": {"group_count": 2}},
        },
    )
    assert linked.status_code == 200, linked.text
    linked_contest = linked.json()["contest"]
    assert linked_contest["source_contest_id"] == source["id"]
    assert store.list_contest_entries(linked_contest["id"]) == []

    for title, template_id, source_id in (
        ("missing navigation source", "pencil_drr", 2**62),
        ("cross-game navigation source", "pencil_drr", wrong_game["id"]),
        ("hidden foreign navigation source", "pencil_drr", hidden_foreign_source["id"]),
        ("unsupported pencil template", "pencil_group_drr_ko", source["id"]),
    ):
        rejected = client.post(
            "/api/contests",
            headers=headers,
            json={
                "title": title,
                "game_id": "pencil",
                "template_id": template_id,
                "source_contest_id": source_id,
            },
        )
        assert rejected.status_code == 400, rejected.text

    patch_rejected = client.patch(
        f"/api/contests/{linked_contest['id']}",
        headers=headers,
        json={"source_contest_id": optional.json()["contest"]["id"]},
    )
    assert patch_rejected.status_code == 422

    client.close()
    store.close()


def test_legacy_null_time_control_api_only_backfills_game_default(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = create_app(db_path=str(tmp_path / "legacy-time-control-api.db"))
    store = app.state.store
    organizer = store.create_user(
        "legacy-clock-organizer",
        "legacy-clock-organizer@example.com",
        hash_password("pw123456"),
        role="organizer",
    )
    store.update_user(organizer["id"], email_verified=1)
    _user, token = app.state.auth.authenticate(
        "legacy-clock-organizer", "pw123456"
    )
    headers = {"Authorization": f"Bearer {token}"}
    manager = ContestManager(store, _NoDispatchOrchestrator())

    def legacy_contest(title: str, *, status: str) -> dict:
        contest = manager.create(
            organizer["id"],
            title,
            game_id="pencil",
            template_id="pencil_group_drr",
            time_control_id="pencil_per_side_total_900s_v1",
            stage_format_settings={"groups": {"group_count": 2}},
        )
        if status == "open":
            store.update_contest(contest["id"], status="open")
        with store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE contests SET time_control_id=NULL WHERE id=?",
                (contest["id"],),
            )
        return store.get_contest(contest["id"])

    with TestClient(app) as client:
        for status in ("draft", "open"):
            backfill = legacy_contest(
                f"legacy {status} default backfill", status=status
            )
            response = client.patch(
                f"/api/contests/{backfill['id']}",
                headers=headers,
                json={"time_control_id": "pencil_per_side_total_900s_v1"},
            )
            assert response.status_code == 200, response.text
            assert response.json()["contest"]["time_control_id"] == (
                "pencil_per_side_total_900s_v1"
            )

            rejected = legacy_contest(
                f"legacy {status} alternate rejected", status=status
            )
            stages_before = rejected["stages_json"]
            response = client.patch(
                f"/api/contests/{rejected['id']}",
                headers=headers,
                json={"time_control_id": "pencil_per_decision_1s_v1"},
            )
            assert response.status_code == 400
            assert "旧默认值" in response.text
            durable = store.get_contest(rejected["id"])
            assert durable["time_control_id"] is None
            assert durable["stages_json"] == stages_before

    store.close()


def test_legacy_null_time_control_store_cas_is_default_only_and_stale_safe(
    tmp_path,
):
    path = tmp_path / "legacy-time-control-store-cas.db"
    winning_store = Store(str(path))
    organizer = winning_store.create_user(
        "legacy-clock-cas-organizer",
        "legacy-clock-cas-organizer@example.com",
        "hash",
        role="organizer",
    )
    contest = ContestManager(
        winning_store, _NoDispatchOrchestrator()
    ).create(
        organizer["id"],
        "legacy clock CAS",
        game_id="pencil",
        template_id="pencil_group_drr",
        time_control_id="pencil_per_side_total_900s_v1",
        stage_format_settings={"groups": {"group_count": 2}},
    )
    winning_store.update_contest(contest["id"], status="open")
    with winning_store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET time_control_id=NULL WHERE id=?",
            (contest["id"],),
        )

    stale_store = Store(str(path))
    winner_snapshot = winning_store.get_contest(contest["id"])
    stale_snapshot = stale_store.get_contest(contest["id"])
    assert winner_snapshot["time_control_id"] is None
    assert stale_snapshot["time_control_id"] is None

    with pytest.raises(ValueError, match="旧默认值"):
        stale_store.compare_and_swap_unstarted_contest_stages(
            contest["id"],
            expected_status="open",
            expected_stages_json=stale_snapshot["stages_json"],
            stages_json=stale_snapshot["stages_json"],
            expected_time_control_id=None,
            time_control_id="pencil_per_decision_1s_v1",
            update_time_control=True,
        )
    assert stale_store.get_contest(contest["id"])["time_control_id"] is None

    updated = winning_store.compare_and_swap_unstarted_contest_stages(
        contest["id"],
        expected_status="open",
        expected_stages_json=winner_snapshot["stages_json"],
        stages_json=winner_snapshot["stages_json"],
        expected_time_control_id=None,
        time_control_id="pencil_per_side_total_900s_v1",
        update_time_control=True,
    )
    assert updated["time_control_id"] == "pencil_per_side_total_900s_v1"

    with pytest.raises(ValueError, match="并发修改"):
        stale_store.compare_and_swap_unstarted_contest_stages(
            contest["id"],
            expected_status="open",
            expected_stages_json=stale_snapshot["stages_json"],
            stages_json=stale_snapshot["stages_json"],
            expected_time_control_id=None,
            time_control_id="pencil_per_side_total_900s_v1",
            update_time_control=True,
        )
    assert stale_store.get_contest(contest["id"])["time_control_id"] == (
        "pencil_per_side_total_900s_v1"
    )
    stale_store.close()
    winning_store.close()


@pytest.mark.parametrize("status", ["published", "running", "finished"])
def test_legacy_null_time_control_nonzero_lifecycle_states_remain_frozen(
    tmp_path, status
):
    store = Store(str(tmp_path / f"legacy-clock-frozen-{status}.db"))
    organizer = store.create_user(
        f"legacy-clock-{status}-organizer",
        f"legacy-clock-{status}-organizer@example.com",
        "hash",
        role="organizer",
    )
    contest = ContestManager(store, _NoDispatchOrchestrator()).create(
        organizer["id"],
        f"legacy clock frozen {status}",
        game_id="pencil",
        template_id="pencil_group_drr",
        time_control_id="pencil_per_side_total_900s_v1",
        stage_format_settings={"groups": {"group_count": 2}},
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET status=?,time_control_id=NULL WHERE id=?",
            (status, contest["id"]),
        )
    manager = ContestManager(store, _NoDispatchOrchestrator())
    with pytest.raises(ValueError, match="draft/open"):
        asyncio.run(
            manager.revise_format_settings(
                contest["id"],
                time_control_id="pencil_per_side_total_900s_v1",
            )
        )
    assert store.get_contest(contest["id"])["time_control_id"] is None
    store.close()


def test_pencil_group_format_requires_explicit_count_and_freezes_once(tmp_path):
    store = Store(str(tmp_path / "pencil-groups.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, index, game_id="pencil")
        for index in range(5)
    ]
    manager = ContestManager(store, _NoDispatchOrchestrator())

    with pytest.raises(ValueError, match="必须选择 group_count"):
        manager.create(
            users_and_bots[0][0]["id"],
            "missing group count",
            game_id="pencil",
            template_id="pencil_group_drr",
        )

    contest = manager.create(
        users_and_bots[0][0]["id"],
        "pencil random groups",
        game_id="pencil",
        template_id="pencil_group_drr",
        time_control_id="pencil_per_decision_1s_v1",
        stage_format_settings={"groups": {"group_count": 2}},
    )
    for user, bot in users_and_bots:
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")

    published = asyncio.run(manager.publish(contest["id"]))
    assert published["status"] == "published"
    assert published["time_control_id"] == "pencil_per_decision_1s_v1"
    entries = store.list_contest_entries(contest["id"])
    group_sizes = sorted(
        sum(entry["group_id"] == group_id for entry in entries)
        for group_id in {entry["group_id"] for entry in entries}
    )
    assert group_sizes == [2, 3]
    pairings = store.list_contest_pairings(contest["id"], stage_idx=0)
    assert len(pairings) == 8
    assert all(pairing["group_id"] for pairing in pairings)
    public_draw = public_format_snapshot(published)
    assert public_draw is not None
    assert public_draw["algorithm"] == "secure_random_balanced_v1"
    assert "groups" not in public_draw and "draw_order" not in public_draw
    summary_rows = build_stage_summaries(
        manager,
        published,
        entries,
        pairings,
        current_topology_sealed=True,
    )[0]["rows"]
    assert len(summary_rows) == len(entries)
    assert all(
        isinstance(row.get("overall_rank"), int)
        and row["overall_rank"] >= 1
        and isinstance(row.get("rank_in_group"), int)
        and row["rank_in_group"] >= 1
        for row in summary_rows
    )
    assert [row["overall_rank"] for row in summary_rows] == list(
        range(1, len(entries) + 1)
    )

    before = [(entry["id"], entry["group_id"], entry["seed"]) for entry in entries]
    with pytest.raises(ValueError, match="仅 open/draft"):
        asyncio.run(manager.publish(contest["id"]))
    assert before == [
        (entry["id"], entry["group_id"], entry["seed"])
        for entry in store.list_contest_entries(contest["id"])
    ]
    store.close()


def test_partial_persisted_cross_group_rows_cannot_define_their_own_cohort(
    tmp_path,
):
    store = Store(str(tmp_path / "partial-persisted-cross-group.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, 7_100 + index, game_id="pencil")
        for index in range(4)
    ]
    manager = ContestManager(store, _NoDispatchOrchestrator())
    contest = manager.create(
        users_and_bots[0][0]["id"],
        "partial persisted cross-group rows",
        game_id="pencil",
        template_id="pencil_group_drr",
        stage_format_settings={"groups": {"group_count": 2}},
    )
    for user, bot in users_and_bots:
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")
    published = asyncio.run(manager.publish(contest["id"]))
    entries = store.list_contest_entries(contest["id"])
    pairings = store.list_contest_pairings(contest["id"], stage_idx=0)
    assert {pairing["entry_a_id"] for pairing in pairings} | {
        pairing["entry_b_id"] for pairing in pairings
    } == {entry["id"] for entry in entries}

    authoritative = manager.standings(
        contest["id"],
        stage_idx=0,
        pairings=pairings,
        entries=entries,
        contest=published,
    )
    partial = [
        {**copy.deepcopy(row), "stage_idx": 0, "stage_key": "groups"}
        for row in authoritative[:2]
    ]
    # One valid first-place coordinate from each group, carrying a seemingly
    # complete overall 1/2 table, must not be allowed to hide the other two
    # entrants from the authoritative four-player pairing graph.
    assert [row["overall_rank"] for row in partial] == [1, 2]
    assert {row["rank_in_group"] for row in partial} == {1}
    assert len({row["group_id"] for row in partial}) == 2

    summary = build_stage_summaries(
        manager,
        published,
        entries,
        pairings,
        stage_results=partial,
    )[0]

    assert summary["source"] == "persisted"
    assert summary["status"] != "completed"
    assert summary["rows"] == []
    assert summary["advancement_final"] is False
    store.close()


class _NoPairingSummaryManager:
    def standings(self, *_args, **_kwargs):  # pragma: no cover - guarded call
        raise AssertionError("pairing-free summary must not compute live standings")


class _EmptyStandingsSummaryManager:
    def standings(self, *_args, **_kwargs):
        return []


class _SyntheticStandingsSummaryManager:
    def standings(self, _contest_id, *, entries, **_kwargs):
        rank_by_group: dict[str, int] = {}
        rows = []
        for overall_position, entry in enumerate(entries, start=1):
            group_id = entry.get("group_id") or ""
            rank_by_group[group_id] = rank_by_group.get(group_id, 0) + 1
            rank = rank_by_group[group_id] if group_id else overall_position
            points = float(len(entries) - overall_position)
            rows.append(
                {
                    "entry_id": entry["id"],
                    "rank": rank,
                    "points": points,
                    "wins": 1 if rank == 1 else 0,
                    "draws": 0,
                    "losses": 0 if rank == 1 else 1,
                    "delta_total": len(entries) - 2 * overall_position,
                    "group_id": group_id,
                    "tiebreaks": _official_tiebreaks(
                        points,
                        int(entry.get("seed") or 0),
                    ),
                }
            )
        return rows


def _summary_entry(entry_id: int, *, eliminated: int = 0) -> dict:
    return {
        "id": entry_id,
        "user_id": 100 + entry_id,
        "bot_id": 200 + entry_id,
        "seed": entry_id,
        "group_id": "",
        "eliminated": eliminated,
    }


def _persisted_summary_row(entry_id: int, stage_idx: int, rank: int) -> dict:
    return {
        "stage_idx": stage_idx,
        "stage_key": f"stage{stage_idx}",
        "entry_id": entry_id,
        "bot_id": 200 + entry_id,
        "points": float(4 - rank),
        "wins": 1 if rank == 1 else 0,
        "draws": 0,
        "losses": 0 if rank == 1 else 1,
        "delta_total": 10 if rank == 1 else -10,
        "group_id": "",
        "rank_in_group": rank,
    }


def _auditable_persisted_summary_row(
    entry_id: int, stage_idx: int, rank: int
) -> dict:
    row = _persisted_summary_row(entry_id, stage_idx, rank)
    row["tiebreaks"] = _official_tiebreaks(
        float(row["points"]), entry_id
    )
    return row


def _synthetic_stage_pairing(
    entry_a_id: int,
    entry_b_id: int,
    *,
    ordinal: int,
    round_num: int = 1,
    group_id: str = "",
    bracket_slot: int | None = None,
    complete: bool = True,
) -> dict:
    return {
        "stage_idx": 0,
        "entry_a_id": entry_a_id,
        "entry_b_id": entry_b_id,
        "round_num": round_num,
        "group_id": group_id,
        "bracket_slot": bracket_slot,
        "match_id": f"synthetic-stage-match-{ordinal}",
        "_complete": complete,
    }


def _synthetic_stage_bye(
    entry_id: int,
    *,
    round_num: int,
    bracket_slot: int,
    tiebreak_group: int = 0,
    tiebreak_game: int = 0,
) -> dict:
    return {
        "stage_idx": 0,
        "entry_a_id": entry_id,
        "entry_b_id": None,
        "bot_a_id": 200 + entry_id,
        "bot_b_id": None,
        "round_num": round_num,
        "bracket_slot": bracket_slot,
        "tiebreak_group": tiebreak_group,
        "tiebreak_game": tiebreak_game,
        "status": "completed",
        "match_id": None,
        "_complete": True,
    }


def _synthetic_current_stage_summary(
    monkeypatch,
    stage: dict,
    entries: list[dict],
    pairings: list[dict],
    *,
    manager=None,
) -> dict:
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    contest = {
        "id": 900,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    return build_stage_summaries(
        manager or _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        pairings,
        stage_results=[],
        # This pure presentation fixture supplies an already-materialized
        # synthetic graph and intentionally has no Store manifest/revision.
        # Production callers derive this proof from the frozen snapshot.
        current_topology_sealed=True,
    )[0]


class _CorruptComputedStandingsSummaryManager:
    def __init__(self, corruption: str) -> None:
        self.corruption = corruption

    def standings(self, contest_id, *, entries, **kwargs):
        rows = _SyntheticStandingsSummaryManager().standings(
            contest_id, entries=entries, **kwargs
        )
        if self.corruption == "rank_missing":
            rows[-1].pop("rank")
        elif self.corruption == "rank_duplicate":
            rows[-1]["rank"] = rows[0]["rank"]
        elif self.corruption == "rank_gap":
            rows[-1]["rank"] += 1
        elif self.corruption == "rank_bool":
            rows[0]["rank"] = True
        elif self.corruption == "tiebreak_missing":
            rows[-1].pop("tiebreaks")
        elif self.corruption == "tiebreak_malformed":
            rows[-1]["tiebreaks"] = {"points": "3"}
        else:  # pragma: no cover - test fixture contract
            raise AssertionError(self.corruption)
        return rows


@pytest.mark.parametrize(
    "corruption",
    [
        "rank_missing",
        "rank_duplicate",
        "rank_gap",
        "rank_bool",
        "tiebreak_missing",
        "tiebreak_malformed",
    ],
)
def test_live_computed_ranking_corruption_fails_closed(
    monkeypatch,
    corruption,
):
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "advance_count": 1,
    }
    entries = [_summary_entry(1), _summary_entry(2)]
    pairings = [_synthetic_stage_pairing(1, 2, ordinal=1)]

    summary = _synthetic_current_stage_summary(
        monkeypatch,
        stage,
        entries,
        pairings,
        manager=_CorruptComputedStandingsSummaryManager(corruption),
    )

    assert summary["rows"] == []
    assert summary["status"] != "completed"
    assert summary["advancement_final"] is False


@pytest.mark.parametrize(
    ("stage", "entry_groups", "pairs", "expected_total"),
    [
        (
            {"key": "rr", "type": "round_robin", "scoring": "poker_3_1_0"},
            None,
            [(1, 2, ""), (1, 3, ""), (1, 4, ""), (2, 3, ""), (2, 4, "")],
            6,
        ),
        (
            {
                "key": "drr",
                "type": "double_round_robin",
                "scoring": "poker_3_1_0",
            },
            None,
            [
                *((a, b, "") for a in range(1, 5) for b in range(a + 1, 5)),
                *((a, b, "") for a in range(1, 5) for b in range(a + 1, 5)),
            ][:-1],
            12,
        ),
        (
            {
                "key": "groups",
                "type": "group_round_robin",
                "group_count": 2,
                "scoring": "poker_3_1_0",
            },
            {1: "A", 2: "A", 3: "B", 4: "B"},
            [(1, 3, "A"), (2, 4, "B")],
            2,
        ),
        (
            {
                "key": "groups",
                "type": "group_double_round_robin",
                "group_count": 2,
                "scoring": "poker_3_1_0",
            },
            {1: "A", 2: "A", 3: "B", 4: "B"},
            [(1, 2, "A"), (1, 2, "A"), (3, 4, "B")],
            4,
        ),
        (
            {"key": "rr", "type": "round_robin", "scoring": "poker_3_1_0"},
            None,
            [(1, 2, ""), (1, 2, ""), (1, 3, ""), (1, 4, ""), (2, 3, ""), (2, 4, "")],
            6,
        ),
        (
            {
                "key": "rr-series",
                "type": "round_robin",
                "scoring": "poker_3_1_0",
                "games_per_pair": 2,
            },
            None,
            [
                *((a, b, "") for a in range(1, 5) for b in range(a + 1, 5)),
                *((a, b, "") for a in range(1, 5) for b in range(a + 1, 5)),
            ][:-1],
            12,
        ),
        (
            {
                "key": "drr-series",
                "type": "double_round_robin",
                "scoring": "poker_3_1_0",
                "games_per_pair": 3,
            },
            None,
            [
                *((a, b, "") for a in range(1, 5) for b in range(a + 1, 5)),
                *((a, b, "") for a in range(1, 5) for b in range(a + 1, 5)),
                *((a, b, "") for a in range(1, 5) for b in range(a + 1, 5)),
            ][:-1],
            18,
        ),
    ],
    ids=[
        "round-robin-missing-edge",
        "double-round-robin-missing-leg",
        "group-round-robin-crosses-frozen-partition",
        "group-double-round-robin-missing-leg",
        "round-robin-duplicate-replaces-edge",
        "round-robin-series-missing-game",
        "double-round-robin-series-uses-explicit-multiplicity",
    ],
)
def test_current_completed_pairings_require_exact_round_robin_topology(
    monkeypatch,
    stage,
    entry_groups,
    pairs,
    expected_total,
):
    if "games_per_pair" in stage:
        monkeypatch.setattr(
            presentation_module,
            "series_rows_settled",
            lambda *_args, **_kwargs: True,
        )
    entries = [
        {
            **_summary_entry(entry_id),
            "group_id": (entry_groups or {}).get(entry_id, ""),
        }
        for entry_id in range(1, 5)
    ]
    pairings = [
        _synthetic_stage_pairing(
            entry_a_id,
            entry_b_id,
            ordinal=ordinal,
            group_id=group_id,
        )
        for ordinal, (entry_a_id, entry_b_id, group_id) in enumerate(
            pairs, start=1
        )
    ]

    summary = _synthetic_current_stage_summary(
        monkeypatch, stage, entries, pairings
    )

    assert summary["status"] == "running"
    assert summary["completed_pairings"] == len(pairings)
    assert summary["total_pairings"] == expected_total
    assert len(summary["rows"]) == len(entries)
    assert summary["advancement_final"] is False


def test_complete_topology_with_unfinished_match_keeps_live_stage_semantics(
    monkeypatch,
):
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": "poker_3_1_0",
        "advance_count": 2,
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]
    pairs = [
        (entry_a_id, entry_b_id)
        for entry_a_id in range(1, 5)
        for entry_b_id in range(entry_a_id + 1, 5)
    ]
    pairings = [
        _synthetic_stage_pairing(
            entry_a_id,
            entry_b_id,
            ordinal=ordinal,
            complete=ordinal != len(pairs),
        )
        for ordinal, (entry_a_id, entry_b_id) in enumerate(pairs, start=1)
    ]

    summary = _synthetic_current_stage_summary(
        monkeypatch, stage, entries, pairings
    )

    assert summary["status"] == "running"
    assert summary["completed_pairings"] == 5
    assert summary["total_pairings"] == 6
    assert len(summary["rows"]) == 4
    assert [row["advancement"] for row in summary["rows"]] == [
        "in_zone",
        "in_zone",
        "outside_zone",
        "outside_zone",
    ]


def test_complete_drr_topology_requires_one_pairing_per_seat_direction(
    monkeypatch,
):
    stage = {
        "key": "drr",
        "type": "double_round_robin",
        "scoring": "poker_3_1_0",
        "advance_count": 2,
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]
    pairings = [
        _synthetic_stage_pairing(
            entry_a_id,
            entry_b_id,
            ordinal=ordinal,
            round_num=1 if ordinal % 2 else 2,
        )
        for ordinal, (entry_a_id, entry_b_id) in enumerate(
            (
                pair
                for first in range(1, 5)
                for second in range(first + 1, 5)
                for pair in ((first, second), (first, second))
            ),
            start=1,
        )
    ]

    summary = _synthetic_current_stage_summary(
        monkeypatch, stage, entries, pairings
    )

    assert summary["status"] == "running"
    assert summary["advancement_final"] is False
    assert summary["advancement_final"] is False


def test_current_swiss_first_round_cannot_complete_whole_stage(monkeypatch):
    stage = {"key": "swiss", "type": "swiss", "scoring": "poker_3_1_0"}
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]
    pairings = [
        _synthetic_stage_pairing(1, 2, ordinal=1, round_num=1),
        _synthetic_stage_pairing(3, 4, ordinal=2, round_num=1),
    ]

    summary = _synthetic_current_stage_summary(
        monkeypatch, stage, entries, pairings
    )

    assert summary["status"] == "running"
    assert summary["completed_pairings"] == 2
    assert summary["total_pairings"] > 2
    assert len(summary["rows"]) == 4
    assert summary["advancement_final"] is False


def test_current_elimination_first_round_requires_unique_champion(monkeypatch):
    stage = {
        "key": "ko",
        "type": "single_elimination",
        "scoring": "poker_3_1_0",
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]
    pairings = [
        _synthetic_stage_pairing(1, 2, ordinal=1, bracket_slot=0),
        _synthetic_stage_pairing(3, 4, ordinal=2, bracket_slot=1),
    ]
    monkeypatch.setattr(
        presentation_module,
        "summarize_elimination_encounter",
        lambda _stage, rows, _lookup, **_kwargs: {
            "state": "decided",
            "entry_a_id": rows[0]["entry_a_id"],
            "entry_b_id": rows[0]["entry_b_id"],
            "winner_entry": rows[0]["entry_a_id"],
        },
    )

    summary = _synthetic_current_stage_summary(
        monkeypatch, stage, entries, pairings
    )

    assert summary["status"] == "running"
    assert summary["completed_pairings"] == 2
    assert len(summary["rows"]) == 4
    assert summary["advancement_final"] is False


def _patch_synthetic_elimination_results(monkeypatch) -> None:
    def fake_summary(_stage, rows, _lookup, **_kwargs):
        return {
            "state": "decided",
            "entry_a_id": rows[0]["entry_a_id"],
            "entry_b_id": rows[0]["entry_b_id"],
            "winner_entry": rows[0]["entry_a_id"],
        }

    def fake_binding(*_args, **_kwargs):
        return True

    # Presentation delegates exact KO topology to the manager-owned helper;
    # patch both modules so this pure synthetic fixture still replaces every
    # external Match/roster dependency of that shared implementation.
    monkeypatch.setattr(
        presentation_module, "summarize_elimination_encounter", fake_summary
    )
    monkeypatch.setattr(
        manager_module, "summarize_elimination_encounter", fake_summary
    )
    monkeypatch.setattr(
        presentation_module,
        "contest_pairing_roster_binding_is_valid",
        fake_binding,
    )
    monkeypatch.setattr(
        manager_module, "contest_pairing_roster_binding_is_valid", fake_binding
    )


def test_five_entry_elimination_rejects_non_power_of_two_first_round(
    monkeypatch,
):
    stage = {
        "key": "ko",
        "type": "single_elimination",
        "scoring": "poker_3_1_0",
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 6)]
    pairings = [
        _synthetic_stage_pairing(1, 2, ordinal=1, round_num=1, bracket_slot=0),
        _synthetic_stage_pairing(3, 4, ordinal=2, round_num=1, bracket_slot=1),
        _synthetic_stage_bye(5, round_num=1, bracket_slot=2),
        _synthetic_stage_pairing(1, 3, ordinal=3, round_num=2, bracket_slot=0),
        _synthetic_stage_bye(5, round_num=2, bracket_slot=1),
        _synthetic_stage_pairing(1, 5, ordinal=4, round_num=3, bracket_slot=0),
    ]
    _patch_synthetic_elimination_results(monkeypatch)

    summary = _synthetic_current_stage_summary(
        monkeypatch, stage, entries, pairings
    )

    assert summary["status"] == "running"
    assert summary["completed_pairings"] == len(pairings)
    assert len(summary["rows"]) == 5
    assert summary["advancement_final"] is False


def test_elimination_rejects_cross_wired_winners_between_rounds(monkeypatch):
    stage = {
        "key": "ko",
        "type": "single_elimination",
        "scoring": "poker_3_1_0",
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 9)]
    pairings = [
        _synthetic_stage_pairing(1, 2, ordinal=1, round_num=1, bracket_slot=0),
        _synthetic_stage_pairing(3, 4, ordinal=2, round_num=1, bracket_slot=1),
        _synthetic_stage_pairing(5, 6, ordinal=3, round_num=1, bracket_slot=2),
        _synthetic_stage_pairing(7, 8, ordinal=4, round_num=1, bracket_slot=3),
        _synthetic_stage_pairing(1, 5, ordinal=5, round_num=2, bracket_slot=0),
        _synthetic_stage_pairing(3, 7, ordinal=6, round_num=2, bracket_slot=1),
        _synthetic_stage_pairing(1, 3, ordinal=7, round_num=3, bracket_slot=0),
    ]
    _patch_synthetic_elimination_results(monkeypatch)

    summary = _synthetic_current_stage_summary(
        monkeypatch, stage, entries, pairings
    )

    assert summary["status"] == "running"
    assert summary["completed_pairings"] == len(pairings)
    assert len(summary["rows"]) == 8
    assert summary["advancement_final"] is False


def test_canonical_five_entry_elimination_tree_can_complete(monkeypatch):
    stage = {
        "key": "ko",
        "type": "single_elimination",
        "scoring": "poker_3_1_0",
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 6)]
    pairings = [
        _synthetic_stage_pairing(1, 2, ordinal=1, round_num=1, bracket_slot=0),
        _synthetic_stage_bye(3, round_num=1, bracket_slot=1),
        _synthetic_stage_bye(4, round_num=1, bracket_slot=2),
        _synthetic_stage_bye(5, round_num=1, bracket_slot=3),
        _synthetic_stage_pairing(1, 3, ordinal=2, round_num=2, bracket_slot=0),
        _synthetic_stage_pairing(4, 5, ordinal=3, round_num=2, bracket_slot=1),
        _synthetic_stage_pairing(1, 4, ordinal=4, round_num=3, bracket_slot=0),
    ]
    _patch_synthetic_elimination_results(monkeypatch)

    summary = _synthetic_current_stage_summary(
        monkeypatch, stage, entries, pairings
    )

    assert summary["status"] == "completed"
    assert summary["completed_pairings"] == len(pairings)
    assert len(summary["rows"]) == 5
    assert summary["advancement_final"] is True


def test_elimination_bye_cannot_carry_tiebreak_coordinates(monkeypatch):
    stage = {
        "key": "ko",
        "type": "single_elimination",
        "scoring": "poker_3_1_0",
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 4)]
    pairings = [
        _synthetic_stage_pairing(1, 2, ordinal=1, round_num=1, bracket_slot=0),
        _synthetic_stage_bye(
            3,
            round_num=1,
            bracket_slot=1,
            tiebreak_group=1,
            tiebreak_game=1,
        ),
        _synthetic_stage_pairing(1, 3, ordinal=2, round_num=2, bracket_slot=0),
    ]
    _patch_synthetic_elimination_results(monkeypatch)

    summary = _synthetic_current_stage_summary(
        monkeypatch, stage, entries, pairings
    )

    assert summary["status"] == "running"
    assert summary["completed_pairings"] == len(pairings)
    assert summary["advancement_final"] is False


@pytest.mark.parametrize("participant_count", [0, 1, 2])
def test_finished_pairing_free_current_accepts_exact_zero_one_or_two_snapshot(
    participant_count,
):
    stage = {"key": "rr", "type": "round_robin", "scoring": "poker_3_1_0"}
    base_contest = {
        "id": 91,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    manager = _NoPairingSummaryManager()

    entries = [_summary_entry(entry_id) for entry_id in range(1, participant_count + 1)]
    persisted = [
        _auditable_persisted_summary_row(entry_id, 0, entry_id)
        for entry_id in range(1, participant_count + 1)
    ]
    summary = build_stage_summaries(
        manager,
        base_contest,
        entries,
        [],
        stage_results=persisted,
    )[0]
    assert summary["source"] == ("persisted" if participant_count else "pending")
    assert [row["entry_id"] for row in summary["rows"]] == list(
        range(1, participant_count + 1)
    )
    assert [row["rank"] for row in summary["rows"]] == list(
        range(1, participant_count + 1)
    )
    assert summary["total_pairings"] == 0
    assert summary["status"] == "completed"
    assert summary["advancement_final"] is True


def test_running_pairing_free_current_ignores_premature_complete_snapshot():
    stage = {"key": "rr", "type": "round_robin", "scoring": "poker_3_1_0"}
    contest = {
        "id": 94,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    summary = build_stage_summaries(
        _NoPairingSummaryManager(),
        contest,
        [_summary_entry(1), _summary_entry(2)],
        [],
        stage_results=[
            _persisted_summary_row(1, 0, 1),
            _persisted_summary_row(2, 0, 2),
        ],
    )[0]

    assert summary["source"] == "pending"
    assert summary["status"] == "pending"
    assert summary["rows"] == []
    assert summary["advancement_final"] is False


def test_future_pairing_free_stage_ignores_premature_persisted_rows():
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 1,
        },
        {"key": "final", "type": "single_elimination", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 92,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 0,
        "stages_json": json.dumps(stages),
    }
    summaries = build_stage_summaries(
        _NoPairingSummaryManager(),
        contest,
        [_summary_entry(1), _summary_entry(2)],
        [],
        stage_results=[_persisted_summary_row(1, 1, 1)],
    )
    future = summaries[1]
    assert future["source"] == "pending"
    assert future["status"] == "pending"
    assert future["rows"] == []
    assert future["advancement_final"] is False


def test_legacy_pairing_free_past_and_current_snapshots_keep_external_cohorts():
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 1,
        },
        {"key": "final", "type": "single_elimination", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 93,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 1,
        "stages_json": json.dumps(stages),
    }
    entries = [_summary_entry(1), _summary_entry(2, eliminated=1)]
    persisted = [
        _persisted_summary_row(1, 0, 1),
        _persisted_summary_row(2, 0, 2),
        _auditable_persisted_summary_row(1, 1, 1),
    ]

    summaries = build_stage_summaries(
        _NoPairingSummaryManager(),
        contest,
        entries,
        [],
        stage_results=persisted,
    )

    assert [row["entry_id"] for row in summaries[0]["rows"]] == [1, 2]
    assert [row["entry_id"] for row in summaries[1]["rows"]] == [1]
    assert [summary["source"] for summary in summaries] == ["persisted", "persisted"]


@pytest.mark.parametrize("swap_middle_cohort", [False, True])
def test_pairing_free_history_chains_exact_four_two_one_advancement_identities(
    swap_middle_cohort,
):
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 2,
        },
        {
            "key": "semifinal",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 1,
        },
        {"key": "final", "type": "single_elimination", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 95,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 2,
        "stages_json": json.dumps(stages),
    }
    entries = [
        _summary_entry(entry_id, eliminated=int(entry_id != 1))
        for entry_id in range(1, 5)
    ]
    middle_ids = [1, 3] if swap_middle_cohort else [1, 2]
    persisted = [
        *[
            _persisted_summary_row(entry_id, 0, rank)
            for rank, entry_id in enumerate([1, 2, 3, 4], start=1)
        ],
        *[
            _persisted_summary_row(entry_id, 1, rank)
            for rank, entry_id in enumerate(middle_ids, start=1)
        ],
        _auditable_persisted_summary_row(1, 2, 1),
    ]

    summaries = build_stage_summaries(
        _NoPairingSummaryManager(),
        contest,
        entries,
        [],
        stage_results=persisted,
    )

    assert [row["entry_id"] for row in summaries[0]["rows"]] == [1, 2, 3, 4]
    assert summaries[0]["status"] == "completed"
    assert [row["entry_id"] for row in summaries[1]["rows"]] == middle_ids
    assert [row["entry_id"] for row in summaries[2]["rows"]] == [1]
    assert [summary["status"] for summary in summaries] == [
        "completed",
        "completed",
        "completed",
    ]
    # Pairing-free legacy snapshots remain independently displayable, but they
    # do not authenticate a cross-stage advancement chain.  The current row is
    # readable only because its own canonical persisted ranking is exact.
    assert summaries[2]["advancement_final"] is True


def test_pairing_free_current_series_cannot_override_carried_exact_cohort():
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 2,
        },
        {
            "key": "final",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "games_per_pair": 2,
            "series_scoring": "independent_scoring_game_points_v1",
        },
    ]
    contest = {
        "id": 100,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 1,
        "stages_json": json.dumps(stages),
    }
    entries = [
        _summary_entry(entry_id, eliminated=int(entry_id not in {1, 3}))
        for entry_id in range(1, 5)
    ]
    persisted = [
        *[
            _persisted_summary_row(entry_id, 0, rank)
            for rank, entry_id in enumerate([1, 2, 3, 4], start=1)
        ],
        _persisted_summary_row(1, 1, 1),
        _persisted_summary_row(3, 1, 2),
    ]

    summaries = build_stage_summaries(
        _NoPairingSummaryManager(), contest, entries, [], stage_results=persisted
    )

    assert summaries[0]["status"] == "completed"
    assert summaries[1]["rows"] == []
    assert summaries[1]["status"] == "pending"
    assert summaries[1]["advancement_final"] is False


@pytest.mark.parametrize(
    "stage",
    [
        {
            "key": "ordinary",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
        },
        {
            "key": "groups",
            "type": "group_round_robin",
            "group_count": 1,
            "scoring": "poker_3_1_0",
        },
        {
            "key": "series",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "games_per_pair": 2,
            "series_scoring": "independent_scoring_game_points_v1",
        },
        {
            "key": "ko",
            "type": "single_elimination",
            "scoring": "poker_3_1_0",
        },
    ],
    ids=["ordinary", "group", "series", "ko"],
)
def test_current_stage_pairing_cannot_shrink_exact_active_cohort(stage):
    grouped = str(stage["type"]).startswith("group_")
    contest = {
        "id": 104,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    entries = [
        {
            **_summary_entry(entry_id),
            "group_id": "G1" if grouped else "",
        }
        for entry_id in range(1, 4)
    ]
    pairing = {
        "stage_idx": 0,
        "entry_a_id": 1,
        "entry_b_id": 2,
        "group_id": "G1" if grouped else "",
        "match_id": None,
    }
    persisted = [
        {
            **_persisted_summary_row(entry_id, 0, rank),
            "group_id": "G1" if grouped else "",
        }
        for rank, entry_id in enumerate([1, 2], start=1)
    ]

    summary = build_stage_summaries(
        _EmptyStandingsSummaryManager(),
        contest,
        entries,
        [pairing],
        stage_results=persisted,
    )[0]

    assert summary["rows"] == []
    assert summary["status"] != "completed"
    assert summary["advancement_final"] is False


@pytest.mark.parametrize("complete_graph", [True, False])
def test_past_stage_without_carried_cohort_requires_complete_legacy_pairing_graph(
    complete_graph,
):
    stages = [
        {"key": "legacy-shell", "type": "round_robin", "scoring": "poker_3_1_0"},
        {
            "key": "legacy-paired",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 1,
        },
        {"key": "current", "type": "single_elimination", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 105,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 2,
        "stages_json": json.dumps(stages),
    }
    entries = [
        _summary_entry(entry_id, eliminated=int(entry_id != 1))
        for entry_id in range(1, 5)
    ]
    pairs = [
        (entry_a_id, entry_b_id)
        for entry_a_id in range(1, 5)
        for entry_b_id in range(entry_a_id + 1, 5)
        if complete_graph or (entry_a_id, entry_b_id) != (3, 4)
    ]
    pairings = [
        {
            "stage_idx": 1,
            "entry_a_id": entry_a_id,
            "entry_b_id": entry_b_id,
            "match_id": None,
        }
        for entry_a_id, entry_b_id in pairs
    ]
    persisted = [
        _persisted_summary_row(entry_id, 1, rank)
        for rank, entry_id in enumerate([1, 2, 3, 4], start=1)
    ]

    summary = build_stage_summaries(
        _EmptyStandingsSummaryManager(),
        contest,
        entries,
        pairings,
        stage_results=persisted,
    )[1]

    assert [row["entry_id"] for row in summary["rows"]] == (
        [1, 2, 3, 4] if complete_graph else []
    )


def test_invalid_rank_cannot_be_finalized_by_unverified_next_pairing_cohort():
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 2,
        },
        {"key": "final", "type": "single_elimination", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 101,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 1,
        "stages_json": json.dumps(stages),
    }
    entries = [
        _summary_entry(entry_id, eliminated=int(entry_id not in {1, 2}))
        for entry_id in range(1, 5)
    ]
    persisted = [
        _persisted_summary_row(entry_id, 0, 1 if rank == 2 else rank)
        for rank, entry_id in enumerate([1, 2, 3, 4], start=1)
    ]
    next_pairing = {
        "stage_idx": 1,
        "entry_a_id": 1,
        "entry_b_id": 2,
        "match_id": None,
    }

    summary = build_stage_summaries(
        _EmptyStandingsSummaryManager(),
        contest,
        entries,
        [next_pairing],
        stage_results=persisted,
    )[0]

    assert summary["rows"] == []
    assert summary["status"] != "completed"
    assert summary["advancement_final"] is False


def test_current_stage_requires_verified_predecessor_ranking_not_active_flags():
    """A partial qualifier cannot let active flags authenticate the final."""
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 2,
        },
        {
            "key": "final",
            "type": "double_round_robin",
            "scoring": "poker_3_1_0",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
        },
    ]
    contest = {
        "id": 108,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 1,
        "stages_json": json.dumps(stages),
    }
    entries = [
        _summary_entry(entry_id, eliminated=int(entry_id not in {1, 2}))
        for entry_id in range(1, 5)
    ]
    pairings = [
        {
            "stage_idx": 0,
            "entry_a_id": 1,
            "entry_b_id": 2,
            "match_id": None,
        },
        {
            "stage_idx": 1,
            "entry_a_id": 1,
            "entry_b_id": 2,
            "match_id": None,
        },
    ]
    partial_qualifier = [
        _persisted_summary_row(entry_id, 0, rank)
        for rank, entry_id in enumerate([1, 2], start=1)
    ]

    summaries = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        pairings,
        stage_results=partial_qualifier,
    )

    assert summaries[0]["rows"] == []
    assert summaries[0]["advancement_final"] is False
    assert summaries[1]["rows"] == []
    assert summaries[1]["status"] != "completed"
    assert summaries[1]["advancement_final"] is False


@pytest.mark.parametrize("status", ["published", "running", "rest"])
def test_active_current_no_shrink_still_requires_completed_predecessor(
    monkeypatch, status
):
    """A no-shrink rule carries a proven cohort, not merely active flags."""
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    stages = [
        {
            "key": "predecessor",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
        },
        {
            "key": "current",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
        },
    ]
    contest = {
        "id": 109,
        "game_id": "holdem",
        "template_id": "custom",
        "status": status,
        "current_stage_idx": 1,
        "stages_json": json.dumps(stages),
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]
    predecessor = _synthetic_stage_pairing(1, 2, ordinal=1)
    current = [
        {
            **_synthetic_stage_pairing(left, right, ordinal=10 + ordinal),
            "stage_idx": 1,
        }
        for ordinal, (left, right) in enumerate(
            (
                (left, right)
                for left in range(1, 5)
                for right in range(left + 1, 5)
            ),
            start=1,
        )
    ]

    summaries = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        [predecessor, *current],
        stage_results=[],
    )

    assert summaries[0]["advancement_final"] is False
    assert summaries[1]["rows"] == []
    assert summaries[1]["status"] != "completed"
    assert current_stage_cohort_from_summaries(contest, entries, summaries) is None


def test_active_current_rejects_pairing_free_multi_entry_predecessor_snapshot(
    monkeypatch,
):
    """Persisted rows do not prove that an omitted multi-player graph completed."""
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    stages = [
        {"key": "predecessor", "type": "round_robin", "scoring": "poker_3_1_0"},
        {"key": "current", "type": "round_robin", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 111,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 1,
        "stages_json": json.dumps(stages),
    }
    entries = [_summary_entry(1), _summary_entry(2)]
    current_pairing = {
        **_synthetic_stage_pairing(1, 2, ordinal=1),
        "stage_idx": 1,
    }
    persisted = [
        _auditable_persisted_summary_row(entry_id, 0, rank)
        for rank, entry_id in enumerate((1, 2), start=1)
    ]

    summaries = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        [current_pairing],
        stage_results=persisted,
    )

    assert summaries[0]["status"] != "completed"
    assert summaries[1]["rows"] == []
    assert current_stage_cohort_from_summaries(contest, entries, summaries) is None


@pytest.mark.parametrize(
    "persisted_kind",
    [
        "missing",
        "partial",
        "bad-tiebreak",
        "points-bool",
        "points-nan",
        "wins-bool",
        "wins-negative",
        "delta-bool",
        "points-mismatch",
    ],
)
def test_finished_current_requires_exact_persisted_snapshot_before_recomputation(
    monkeypatch, persisted_kind
):
    """A finished current table is an artifact read, never a Match replay."""
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    stage = {"key": "current", "type": "round_robin", "scoring": "poker_3_1_0"}
    contest = {
        "id": 112,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    entries = [_summary_entry(1), _summary_entry(2)]
    pairings = [_synthetic_stage_pairing(1, 2, ordinal=1)]
    persisted = [
        _auditable_persisted_summary_row(entry_id, 0, rank)
        for rank, entry_id in enumerate((1, 2), start=1)
    ]
    if persisted_kind == "missing":
        persisted = []
    elif persisted_kind == "partial":
        persisted = persisted[:1]
    elif persisted_kind == "bad-tiebreak":
        persisted[1] = {
            **persisted[1],
            "tiebreaks": {"points": 0},
        }
    elif persisted_kind == "points-bool":
        persisted[1]["points"] = True
    elif persisted_kind == "points-nan":
        persisted[1]["points"] = float("nan")
    elif persisted_kind == "wins-bool":
        persisted[1]["wins"] = True
    elif persisted_kind == "wins-negative":
        persisted[1]["wins"] = -1
    elif persisted_kind == "delta-bool":
        persisted[1]["delta_total"] = False
    else:
        persisted[1]["points"] = 999

    summaries = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        pairings,
        stage_results=persisted,
    )

    assert summaries[0]["rows"] == []
    assert summaries[0]["status"] != "completed"
    assert current_stage_cohort_from_summaries(contest, entries, summaries) is None


def test_finished_contradicted_middle_stage_cannot_fallback_at_later_snapshot(
    monkeypatch,
):
    """A complete wrong middle cohort is a sticky contradiction, not unknown."""
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    stages = [
        {
            "key": "qualifier",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 2,
        },
        {
            "key": "semifinal",
            "type": "round_robin",
            "scoring": "poker_3_1_0",
            "advance_count": 1,
        },
        {"key": "final", "type": "round_robin", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 113,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 2,
        "stages_json": json.dumps(stages),
    }
    entries = [
        _summary_entry(entry_id, eliminated=int(entry_id != 3))
        for entry_id in range(1, 5)
    ]
    pairings = [
        {
            **_synthetic_stage_pairing(left, right, ordinal=ordinal),
            "stage_idx": 0,
        }
        for ordinal, (left, right) in enumerate(
            (
                (left, right)
                for left in range(1, 5)
                for right in range(left + 1, 5)
            ),
            start=1,
        )
    ]
    pairings.append(
        {
            **_synthetic_stage_pairing(3, 4, ordinal=20),
            "stage_idx": 1,
        }
    )
    persisted = [
        *[
            _auditable_persisted_summary_row(entry_id, 0, rank)
            for rank, entry_id in enumerate((1, 2, 3, 4), start=1)
        ],
        _auditable_persisted_summary_row(3, 1, 1),
        _auditable_persisted_summary_row(4, 1, 2),
        _auditable_persisted_summary_row(3, 2, 1),
    ]

    summaries = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        pairings,
        stage_results=persisted,
    )

    assert summaries[1]["rows"] == []
    assert summaries[2]["rows"] == []
    assert current_stage_cohort_from_summaries(contest, entries, summaries) is None


def test_finished_partial_middle_stage_allows_exact_current_snapshot_fallback(
    monkeypatch,
):
    """A strict predecessor subset is unavailable evidence, not contradiction."""
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    stages = [
        {"key": "first", "type": "round_robin", "scoring": "poker_3_1_0"},
        {"key": "partial", "type": "round_robin", "scoring": "poker_3_1_0"},
        {"key": "current", "type": "round_robin", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 114,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 2,
        "stages_json": json.dumps(stages),
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]

    def complete_rr(stage_idx: int, ordinal_base: int) -> list[dict]:
        return [
            {
                **_synthetic_stage_pairing(
                    left, right, ordinal=ordinal_base + ordinal
                ),
                "stage_idx": stage_idx,
            }
            for ordinal, (left, right) in enumerate(
                (
                    (left, right)
                    for left in range(1, 5)
                    for right in range(left + 1, 5)
                ),
                start=1,
            )
        ]

    pairings = [
        *complete_rr(0, 0),
        {
            **_synthetic_stage_pairing(1, 2, ordinal=20),
            "stage_idx": 1,
        },
        *complete_rr(2, 30),
    ]
    persisted = [
        *[
            _auditable_persisted_summary_row(entry_id, 0, rank)
            for rank, entry_id in enumerate(range(1, 5), start=1)
        ],
        _auditable_persisted_summary_row(1, 1, 1),
        _auditable_persisted_summary_row(2, 1, 2),
        *[
            _auditable_persisted_summary_row(entry_id, 2, rank)
            for rank, entry_id in enumerate(range(1, 5), start=1)
        ],
    ]

    summaries = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        pairings,
        stage_results=persisted,
    )

    assert summaries[0]["status"] == "completed"
    assert summaries[1]["rows"] == []
    assert [row["entry_id"] for row in summaries[2]["rows"]] == [1, 2, 3, 4]
    assert summaries[2]["_cohort_authority_state"] == "unknown"
    assert current_stage_cohort_from_summaries(contest, entries, summaries) == {
        1,
        2,
        3,
        4,
    }


@pytest.mark.parametrize("foreign_artifact", ["pairing", "stage-result"])
def test_finished_foreign_historical_entry_is_a_sticky_cohort_contradiction(
    monkeypatch, foreign_artifact
):
    """A historical artifact outside the frozen roster can never authorize fallback."""
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    stages = [
        {"key": "first", "type": "round_robin", "scoring": "poker_3_1_0"},
        {"key": "unknown", "type": "round_robin", "scoring": "poker_3_1_0"},
        {"key": "current", "type": "round_robin", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 117,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 2,
        "stages_json": json.dumps(stages),
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]

    def complete_rr(stage_idx: int, ordinal_base: int) -> list[dict]:
        return [
            {
                **_synthetic_stage_pairing(
                    left, right, ordinal=ordinal_base + ordinal
                ),
                "stage_idx": stage_idx,
            }
            for ordinal, (left, right) in enumerate(
                (
                    (left, right)
                    for left in range(1, 5)
                    for right in range(left + 1, 5)
                ),
                start=1,
            )
        ]

    first_pairings = complete_rr(0, 0)
    first_rows = [
        _auditable_persisted_summary_row(entry_id, 0, rank)
        for rank, entry_id in enumerate(range(1, 5), start=1)
    ]
    if foreign_artifact == "pairing":
        first_pairings[0] = {
            **first_pairings[0],
            "entry_a_id": 999,
            "bot_a_id": 9_999,
        }
    else:
        first_rows[-1] = {
            **first_rows[-1],
            "entry_id": 999,
            "bot_id": 9_999,
        }
    current_pairings = complete_rr(2, 30)
    current_rows = [
        _auditable_persisted_summary_row(entry_id, 2, rank)
        for rank, entry_id in enumerate(range(1, 5), start=1)
    ]

    summaries = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        [*first_pairings, *current_pairings],
        stage_results=[*first_rows, *current_rows],
    )

    assert summaries[0]["rows"] == []
    assert summaries[2]["rows"] == []
    assert summaries[2]["_cohort_authority_state"] == "contradicted"
    assert current_stage_cohort_from_summaries(contest, entries, summaries) is None


@pytest.mark.parametrize("artifact_kind", ["partial", "exact-unsettled"])
def test_active_current_decision_artifact_requires_exact_settled_stage(
    monkeypatch, artifact_kind
):
    """Current decision rows and unfinished work cannot authorize each other."""
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    stage = {"key": "current", "type": "round_robin", "scoring": "poker_3_1_0"}
    contest = {
        "id": 115,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
        "published_stage_pairing_count": 1,
        "pairing_topology_revision": 7,
        "sealed_pairing_topology_revision": 7,
    }
    entries = [_summary_entry(1), _summary_entry(2)]
    pairings = [
        _synthetic_stage_pairing(
            1,
            2,
            ordinal=1,
            complete=artifact_kind == "partial",
        )
    ]
    persisted = [
        _auditable_persisted_summary_row(entry_id, 0, rank)
        for rank, entry_id in enumerate((1, 2), start=1)
    ]
    if artifact_kind == "partial":
        persisted = persisted[:1]

    summaries = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        pairings,
        stage_results=persisted,
    )

    assert summaries[0]["rows"] == []
    assert summaries[0]["_cohort_authority_state"] == "contradicted"
    assert current_stage_cohort_from_summaries(contest, entries, summaries) is None


def test_active_current_sealed_pending_stage_without_decision_is_proven(monkeypatch):
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    stage = {"key": "current", "type": "round_robin", "scoring": "poker_3_1_0"}
    contest = {
        "id": 116,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "running",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
        "published_stage_pairing_count": 1,
        "pairing_topology_revision": 8,
        "sealed_pairing_topology_revision": 8,
    }
    entries = [_summary_entry(1), _summary_entry(2)]
    summaries = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        [_synthetic_stage_pairing(1, 2, ordinal=1, complete=False)],
        stage_results=[],
    )

    assert [row["entry_id"] for row in summaries[0]["rows"]] == [1, 2]
    assert summaries[0]["_cohort_authority_state"] == "proven"
    assert current_stage_cohort_from_summaries(contest, entries, summaries) == {1, 2}


def test_finished_no_shrink_predecessor_constrains_current_when_complete(
    monkeypatch,
):
    """Finished compatibility cannot ignore a complete carry-forward proof."""
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    stages = [
        {"key": "predecessor", "type": "round_robin", "scoring": "poker_3_1_0"},
        {"key": "current", "type": "round_robin", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 110,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 1,
        "stages_json": json.dumps(stages),
    }
    entries = [
        _summary_entry(entry_id, eliminated=int(entry_id not in {1, 2}))
        for entry_id in range(1, 5)
    ]
    predecessor = [
        _synthetic_stage_pairing(left, right, ordinal=ordinal)
        for ordinal, (left, right) in enumerate(
            (
                (left, right)
                for left in range(1, 5)
                for right in range(left + 1, 5)
            ),
            start=1,
        )
    ]
    current = {
        **_synthetic_stage_pairing(1, 2, ordinal=20),
        "stage_idx": 1,
    }
    predecessor_rows = [
        _auditable_persisted_summary_row(entry_id, 0, rank)
        for rank, entry_id in enumerate(range(1, 5), start=1)
    ]
    current_rows = [
        _auditable_persisted_summary_row(entry_id, 1, rank)
        for rank, entry_id in enumerate((1, 2), start=1)
    ]

    constrained = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        [*predecessor, current],
        stage_results=[*predecessor_rows, *current_rows],
    )
    assert constrained[0]["status"] == "completed"
    assert constrained[1]["rows"] == []
    assert current_stage_cohort_from_summaries(contest, entries, constrained) is None

    # When no complete predecessor proof exists, immutable history remains
    # readable only through the current stage's exact persisted artifact.
    compatible = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        [current],
        stage_results=current_rows,
    )
    assert [row["entry_id"] for row in compatible[1]["rows"]] == [1, 2]
    assert current_stage_cohort_from_summaries(
        contest, entries, compatible
    ) == {1, 2}


def test_finished_legacy_nonterminal_knockout_keeps_exact_artifacts_readable(
    monkeypatch,
):
    """The new active KO gate must not erase immutable exact history."""
    monkeypatch.setattr(
        presentation_module,
        "match_scoring_result_is_valid",
        lambda _stage, _match, *, pairing, **_kwargs: bool(
            pairing.get("_complete")
        ),
    )
    stages = [
        {
            "key": "legacy_ko",
            "type": "single_elimination",
            "scoring": "poker_3_1_0",
        },
        {"key": "current", "type": "round_robin", "scoring": "poker_3_1_0"},
    ]
    contest = {
        "id": 111,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 1,
        "stages_json": json.dumps(stages),
    }
    entries = [_summary_entry(1), _summary_entry(2)]
    ko_pairing = _synthetic_stage_pairing(
        1, 2, ordinal=1, round_num=1, bracket_slot=0
    )
    current_pairing = {
        **_synthetic_stage_pairing(1, 2, ordinal=2),
        "stage_idx": 1,
    }
    persisted = [
        _auditable_persisted_summary_row(1, 0, 1),
        _auditable_persisted_summary_row(2, 0, 2),
        _auditable_persisted_summary_row(1, 1, 1),
        _auditable_persisted_summary_row(2, 1, 2),
    ]

    historical = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        contest,
        entries,
        [ko_pairing, current_pairing],
        stage_results=persisted,
    )
    assert [row["entry_id"] for row in historical[0]["rows"]] == [1, 2]
    assert [row["entry_id"] for row in historical[1]["rows"]] == [1, 2]
    assert all(row["advancement"] is None for row in historical[0]["rows"])

    active = build_stage_summaries(
        _SyntheticStandingsSummaryManager(),
        {**contest, "status": "running"},
        entries,
        [ko_pairing, current_pairing],
        stage_results=persisted,
    )
    assert active[0]["rows"] == []
    assert active[1]["rows"] == []


def test_create_rejects_nonterminal_elimination_without_explicit_advancement(
    tmp_path,
):
    store = Store(str(tmp_path / "nonterminal-ko-create.db"))
    organizer = store.create_user(
        "nonterminal-ko-organizer",
        "nonterminal-ko-organizer@example.com",
        "hash",
        role="organizer",
    )
    manager = ContestManager(store, _NoDispatchOrchestrator())
    invalid = [
        {"key": "qualifier", "type": "single_elimination"},
        {
            "key": "final",
            "type": "double_round_robin",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
        },
    ]

    with pytest.raises(ValueError, match="single_elimination.*advance_count"):
        manager.create(
            organizer["id"],
            "ambiguous nonterminal KO",
            game_id="holdem",
            stages=invalid,
        )
    assert store.list_contests(organizer_id=organizer["id"]) == []

    created = manager.create(
        organizer["id"],
        "explicit nonterminal KO",
        game_id="holdem",
        stages=[
            {**invalid[0], "advance_count": 2},
            invalid[1],
        ],
    )
    assert created["status"] == "draft"
    store.close()


def _summary_group_pairing(entry_a_id: int, entry_b_id: int, group_id: str) -> dict:
    return {
        "stage_idx": 0,
        "entry_a_id": entry_a_id,
        "entry_b_id": entry_b_id,
        "group_id": group_id,
        "match_id": None,
    }


def _persisted_group_row(
    entry_id: int,
    group_id: str,
    rank_in_group: int,
) -> dict:
    row = {
        **_persisted_summary_row(entry_id, 0, rank_in_group),
        "group_id": group_id,
        "rank_in_group": rank_in_group,
        # Current grouped snapshots persist two independent coordinates: a
        # roster-wide deterministic order plus the local rank used for group
        # advancement.  This helper's entry ids are intentionally contiguous.
        "overall_rank": entry_id,
    }
    row["tiebreaks"] = _official_tiebreaks(
        float(row["points"]), entry_id
    )
    return row


def test_traditional_group_snapshot_rejects_swapped_frozen_roster_groups():
    stage = {
        "key": "groups",
        "type": "group_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    contest = {
        "id": 96,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    entries = [
        {**_summary_entry(1), "group_id": "A"},
        {**_summary_entry(2), "group_id": "A"},
        {**_summary_entry(3), "group_id": "B"},
        {**_summary_entry(4), "group_id": "B"},
    ]
    persisted = [
        _persisted_group_row(1, "B", 1),
        _persisted_group_row(2, "A", 1),
        _persisted_group_row(3, "A", 2),
        _persisted_group_row(4, "B", 2),
    ]

    summary = build_stage_summaries(
        _NoPairingSummaryManager(), contest, entries, [], stage_results=persisted
    )[0]

    assert summary["source"] == "persisted"
    assert summary["rows"] == []
    assert summary["status"] == "pending"
    assert summary["advancement_final"] is False


def test_traditional_group_snapshot_uses_complete_pairing_topology_for_legacy_groups():
    stage = {
        "key": "groups",
        "type": "group_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    contest = {
        "id": 97,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]
    pairings = [
        _summary_group_pairing(1, 2, "A"),
        _summary_group_pairing(3, 4, "B"),
    ]
    persisted = [
        _persisted_group_row(1, "A", 1),
        _persisted_group_row(2, "A", 2),
        _persisted_group_row(3, "B", 1),
        _persisted_group_row(4, "B", 2),
    ]

    summary = build_stage_summaries(
        _EmptyStandingsSummaryManager(),
        contest,
        entries,
        pairings,
        stage_results=persisted,
    )[0]

    assert [(row["entry_id"], row["group_id"]) for row in summary["rows"]] == [
        (1, "A"),
        (2, "A"),
        (3, "B"),
        (4, "B"),
    ]


def test_traditional_group_snapshot_rejects_group_swap_against_pairing_topology():
    stage = {
        "key": "groups",
        "type": "group_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    contest = {
        "id": 99,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]
    pairings = [
        _summary_group_pairing(1, 2, "A"),
        _summary_group_pairing(3, 4, "B"),
    ]
    persisted = [
        _persisted_group_row(1, "B", 1),
        _persisted_group_row(2, "A", 1),
        _persisted_group_row(3, "A", 2),
        _persisted_group_row(4, "B", 2),
    ]

    summary = build_stage_summaries(
        _EmptyStandingsSummaryManager(),
        contest,
        entries,
        pairings,
        stage_results=persisted,
    )[0]

    assert summary["source"] == "persisted"
    assert summary["rows"] == []
    assert summary["status"] != "completed"
    assert summary["advancement_final"] is False


def test_traditional_double_group_requires_both_pairing_legs_for_group_authority():
    stage = {
        "key": "groups",
        "type": "group_double_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    contest = {
        "id": 102,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]
    incomplete_pairings = [
        _summary_group_pairing(1, 2, "A"),
        _summary_group_pairing(3, 4, "B"),
    ]
    persisted = [
        _persisted_group_row(1, "A", 1),
        _persisted_group_row(2, "A", 2),
        _persisted_group_row(3, "B", 1),
        _persisted_group_row(4, "B", 2),
    ]

    summary = build_stage_summaries(
        _EmptyStandingsSummaryManager(),
        contest,
        entries,
        incomplete_pairings,
        stage_results=persisted,
    )[0]

    assert summary["rows"] == []
    assert summary["status"] != "completed"
    assert summary["advancement_final"] is False


def test_traditional_group_snapshot_keeps_bounded_no_authority_legacy_shape():
    stage = {
        "key": "groups",
        "type": "group_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    contest = {
        "id": 98,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]
    persisted = [
        _persisted_group_row(1, "legacy-a", 1),
        _persisted_group_row(2, "legacy-a", 2),
        _persisted_group_row(3, "legacy-b", 1),
        _persisted_group_row(4, "legacy-b", 2),
    ]

    summary = build_stage_summaries(
        _NoPairingSummaryManager(), contest, entries, [], stage_results=persisted
    )[0]

    assert [(row["entry_id"], row["group_id"]) for row in summary["rows"]] == [
        (1, "legacy-a"),
        (2, "legacy-a"),
        (3, "legacy-b"),
        (4, "legacy-b"),
    ]
    assert summary["status"] == "completed"
    assert summary["advancement_final"] is True


def test_traditional_group_no_authority_legacy_rejects_unbalanced_partition():
    stage = {
        "key": "groups",
        "type": "group_round_robin",
        "group_count": 2,
        "scoring": "poker_3_1_0",
    }
    contest = {
        "id": 103,
        "game_id": "holdem",
        "template_id": "custom",
        "status": "finished",
        "current_stage_idx": 0,
        "stages_json": json.dumps([stage]),
    }
    entries = [_summary_entry(entry_id) for entry_id in range(1, 5)]
    persisted = [
        _persisted_group_row(1, "legacy-a", 1),
        _persisted_group_row(2, "legacy-b", 1),
        _persisted_group_row(3, "legacy-b", 2),
        _persisted_group_row(4, "legacy-b", 3),
    ]

    summary = build_stage_summaries(
        _NoPairingSummaryManager(), contest, entries, [], stage_results=persisted
    )[0]

    assert summary["rows"] == []
    assert summary["status"] == "pending"
    assert summary["advancement_final"] is False


def test_hundred_player_group_publication_keeps_freeze_work_linear(
    tmp_path, monkeypatch
):
    """4,900 DRR rows must not imply 9,800 version/file checks or SELECTs."""
    participant_count = 100
    store = Store(str(tmp_path / "large-pencil-groups.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, 20_000 + index, game_id="pencil")
        for index in range(participant_count)
    ]
    manager = ContestManager(store, _NoDispatchOrchestrator())
    contest = manager.create(
        users_and_bots[0][0]["id"],
        "hundred player pencil groups",
        game_id="pencil",
        template_id="pencil_group_drr",
        time_control_id="pencil_per_decision_1s_v1",
        stage_format_settings={"groups": {"group_count": 2}},
        starts_at="2099-12-31T23:59:59",
    )
    for user, bot in users_and_bots:
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")

    version_snapshots = 0
    manager_integrity_checks = 0
    store_integrity_checks = 0
    original_snapshot = manager._version_snapshot
    original_manager_integrity = manager_module.require_binary_file_integrity
    original_store_integrity = store_db_module.require_binary_file_integrity

    def counted_snapshot(bot_a_id, bot_b_id):
        nonlocal version_snapshots
        version_snapshots += 1
        return original_snapshot(bot_a_id, bot_b_id)

    def counted_manager_integrity(*args, **kwargs):
        nonlocal manager_integrity_checks
        manager_integrity_checks += 1
        return original_manager_integrity(*args, **kwargs)

    def counted_store_integrity(*args, **kwargs):
        nonlocal store_integrity_checks
        store_integrity_checks += 1
        return original_store_integrity(*args, **kwargs)

    monkeypatch.setattr(manager, "_version_snapshot", counted_snapshot)
    monkeypatch.setattr(
        manager_module, "require_binary_file_integrity", counted_manager_integrity
    )
    monkeypatch.setattr(
        store_db_module, "require_binary_file_integrity", counted_store_integrity
    )
    selects: list[str] = []
    store._conn.set_trace_callback(
        lambda statement: selects.append(statement)
        if statement.lstrip().upper().startswith("SELECT")
        else None
    )
    try:
        asyncio.run(manager.publish(contest["id"]))
    finally:
        store._conn.set_trace_callback(None)

    assert len(store.list_contest_pairings(contest["id"], stage_idx=0)) == 4_900
    assert version_snapshots == participant_count
    # One roster preflight plus one plan freeze in the manager; the Store then
    # revalidates every unique artifact once behind BEGIN IMMEDIATE.
    assert manager_integrity_checks == participant_count * 2
    assert store_integrity_checks == participant_count
    assert len(selects) <= participant_count * 10 + 100

    def reject_terminal_materialization(*_args, **_kwargs):
        raise AssertionError("unfinished stage must short-circuit before full pairing load")

    monkeypatch.setattr(
        store, "list_contest_pairings", reject_terminal_materialization
    )
    assert manager._stage_done(contest["id"], 0) is False
    store.close()


def test_grouped_contest_detail_and_live_never_expose_private_draw_material(tmp_path):
    app = create_app(db_path=str(tmp_path / "public-format-boundary.db"))
    store = app.state.store
    users_and_bots = [
        _fixture_bot(store, tmp_path, 4_000 + index, game_id="pencil")
        for index in range(4)
    ]
    manager = ContestManager(store, _NoDispatchOrchestrator())
    contest = manager.create(
        users_and_bots[0][0]["id"],
        "bounded public draw",
        game_id="pencil",
        template_id="pencil_group_drr",
        time_control_id="pencil_per_decision_1s_v1",
        stage_format_settings={"groups": {"group_count": 2}},
    )
    for user, bot in users_and_bots:
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")
    asyncio.run(manager.publish(contest["id"]))

    with TestClient(app) as client:
        list_response = client.get("/api/contests")
        detail_response = client.get(f"/api/contests/{contest['id']}")
        live_response = client.get(f"/api/contests/{contest['id']}/live")
    assert list_response.status_code == 200
    assert detail_response.status_code == 200
    assert live_response.status_code == 200
    detail = detail_response.json()
    live = live_response.json()
    private_keys = {
        "format_snapshot_json",
        "published_stage_pairing_count",
        "audit_nonce",
        "groups",
        "draw_order",
        "pairing_seed",
    }
    listed = next(
        row
        for row in list_response.json()["contests"]
        if row["id"] == contest["id"]
    )
    assert private_keys.isdisjoint(listed)
    assert private_keys.isdisjoint(detail["contest"])
    assert private_keys.isdisjoint(live["contest"])
    assert private_keys.isdisjoint(detail["contest"]["format_snapshot"])
    assert private_keys.isdisjoint(live["contest"]["format_snapshot"])
    assert all(private_keys.isdisjoint(row) for row in detail["pairings"])
    for key in ("active", "upcoming", "recent"):
        assert all(private_keys.isdisjoint(row) for row in live[key])
    assert detail["contest"]["format_snapshot"]["audit_digest"] == (
        live["contest"]["format_snapshot"]["audit_digest"]
    )


def test_live_cross_group_top8_uses_complete_authoritative_overall_order(
    tmp_path, monkeypatch
):
    app = create_app(db_path=str(tmp_path / "live-cross-group-top8.db"))
    store = app.state.store
    users_and_bots = [
        _fixture_bot(store, tmp_path, 4_100 + index, game_id="pencil")
        for index in range(10)
    ]

    # Keep assignment deterministic, but make the independent whole-roster
    # draw put group B before group A at each equal group rank.  The old live
    # projection incorrectly reordered these rows back to A/B.
    def deterministic_shuffle(values):
        ordered = list(values)
        if ordered and isinstance(ordered[0], int):
            midpoint = len(ordered) // 2
            return [
                entry_id
                for pair in zip(ordered[midpoint:], ordered[:midpoint])
                for entry_id in pair
            ]
        return ordered

    monkeypatch.setattr(manager_module, "_secure_shuffle", deterministic_shuffle)
    manager = ContestManager(store, _NoDispatchOrchestrator())
    contest = manager.create(
        users_and_bots[0][0]["id"],
        "cross-group live Top 8",
        game_id="pencil",
        template_id="pencil_group_drr",
        stage_format_settings={"groups": {"group_count": 2}},
    )
    for user, bot in users_and_bots:
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")
    published = asyncio.run(manager.publish(contest["id"]))
    entries = store.list_contest_entries(contest["id"])
    pairings = store.list_contest_pairings(contest["id"], stage_idx=0)
    authoritative = manager.standings(
        contest["id"],
        stage_idx=0,
        pairings=pairings,
        entries=entries,
        contest=published,
    )
    assert [row["overall_rank"] for row in authoritative] == list(range(1, 11))
    assert [row["group_id"] for row in authoritative[:2]] == ["B", "A"]
    expected_bot_ids = [row["bot_id"] for row in authoritative[:8]]

    # The route must sort the authority, rather than trusting producer order or
    # rebuilding a group-interleaved order of its own.
    monkeypatch.setattr(
        app.state.contest_manager,
        "standings",
        lambda *_args, **_kwargs: list(reversed(copy.deepcopy(authoritative))),
    )
    with TestClient(app) as client:
        response = client.get(f"/api/contests/{contest['id']}/live")
        assert response.status_code == 200
        payload = response.json()
        assert payload["stage"]["overall_ranking"] == "cross_group_fair_v1"
        assert [row["bot_id"] for row in payload["standings"]] == expected_bot_ids
        assert [row["rank"] for row in payload["standings"]] == list(range(1, 9))
        assert [row["overall_rank"] for row in payload["standings"]] == list(
            range(1, 9)
        )

        malformed_sets = []
        missing = copy.deepcopy(authoritative)
        missing[0].pop("overall_rank")
        malformed_sets.append(missing)
        duplicate = copy.deepcopy(authoritative)
        duplicate[-1]["overall_rank"] = duplicate[0]["overall_rank"]
        malformed_sets.append(duplicate)
        wrong_type = copy.deepcopy(authoritative)
        wrong_type[0]["overall_rank"] = True
        malformed_sets.append(wrong_type)
        for malformed in malformed_sets:
            monkeypatch.setattr(
                app.state.contest_manager,
                "standings",
                lambda *_args, _rows=malformed, **_kwargs: copy.deepcopy(_rows),
            )
            damaged = client.get(f"/api/contests/{contest['id']}/live")
            assert damaged.status_code == 200
            assert damaged.json()["standings"] == []


def test_concurrent_random_group_publish_freezes_exactly_one_draw(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "concurrent-publish.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, index, game_id="pencil")
        for index in range(4)
    ]
    creator = ContestManager(store, _NoDispatchOrchestrator())
    contest = creator.create(
        users_and_bots[0][0]["id"],
        "concurrent random groups",
        game_id="pencil",
        template_id="pencil_group_drr",
        stage_format_settings={"groups": {"group_count": 2}},
    )
    for user, bot in users_and_bots:
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")

    rendezvous = threading.Barrier(2)
    first_call_lock = threading.Lock()
    first_call_threads: set[int] = set()
    original_shuffle = manager_module._secure_shuffle

    def overlapping_shuffle(values):
        thread_id = threading.get_ident()
        with first_call_lock:
            first_for_thread = thread_id not in first_call_threads
            first_call_threads.add(thread_id)
        if first_for_thread:
            rendezvous.wait(timeout=5)
        return original_shuffle(values)

    monkeypatch.setattr(manager_module, "_secure_shuffle", overlapping_shuffle)
    managers = [
        ContestManager(store, _NoDispatchOrchestrator()),
        ContestManager(store, _NoDispatchOrchestrator()),
    ]

    def publish(manager):
        try:
            return ("ok", asyncio.run(manager.publish(contest["id"])))
        except ValueError as exc:
            return ("error", str(exc))

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(publish, managers))
    assert sorted(kind for kind, _result in outcomes) == ["error", "ok"]
    assert any(
        "发布快照已变化" in str(result) or "仅 open/draft" in str(result)
        for kind, result in outcomes
        if kind == "error"
    )
    frozen = store.get_contest(contest["id"])
    frozen_snapshot = frozen["format_snapshot_json"]
    pairings = store.list_contest_pairings(contest["id"], stage_idx=0)
    assert frozen["status"] == "published"
    assert len(pairings) == 4
    assert len({pairing["id"] for pairing in pairings}) == 4

    with pytest.raises(ValueError, match="仅 open/draft"):
        asyncio.run(creator.publish(contest["id"]))
    assert store.get_contest(contest["id"])["format_snapshot_json"] == frozen_snapshot
    assert [pairing["id"] for pairing in store.list_contest_pairings(
        contest["id"], stage_idx=0
    )] == [pairing["id"] for pairing in pairings]
    store.close()


def test_stale_group_publish_cannot_overwrite_concurrent_time_control_patch(
    tmp_path, monkeypatch
):
    """The publication CAS includes the selector that shaped its stale draw."""
    path = tmp_path / "publish-time-control-cas.db"
    publishing_store = Store(str(path))
    users_and_bots = [
        _fixture_bot(publishing_store, tmp_path, 500 + index, game_id="pencil")
        for index in range(4)
    ]
    manager = ContestManager(publishing_store, _NoDispatchOrchestrator())
    contest = manager.create(
        users_and_bots[0][0]["id"],
        "time-control publish race",
        game_id="pencil",
        template_id="pencil_group_drr",
        time_control_id="pencil_per_side_total_900s_v1",
        stage_format_settings={"groups": {"group_count": 2}},
    )
    for user, bot in users_and_bots:
        publishing_store.add_contest_entry(contest["id"], user["id"], bot["id"])
    publishing_store.update_contest(contest["id"], status="open")
    patching_store = Store(str(path))

    publish_ready = threading.Event()
    patch_committed = threading.Event()
    original_freeze = publishing_store.freeze_initial_group_contest

    def pause_before_freeze(*args, **kwargs):
        publish_ready.set()
        assert patch_committed.wait(timeout=5)
        return original_freeze(*args, **kwargs)

    monkeypatch.setattr(
        publishing_store, "freeze_initial_group_contest", pause_before_freeze
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_publish = executor.submit(
            lambda: asyncio.run(manager.publish(contest["id"]))
        )
        assert publish_ready.wait(timeout=5)
        current = patching_store.get_contest(contest["id"])
        patching_store.compare_and_swap_unstarted_contest_stages(
            contest["id"],
            expected_status="open",
            expected_stages_json=current["stages_json"],
            stages_json=current["stages_json"],
            expected_time_control_id="pencil_per_side_total_900s_v1",
            time_control_id="pencil_per_decision_1s_v1",
            update_time_control=True,
        )
        patch_committed.set()
        with pytest.raises(ValueError, match="发布快照已变化"):
            stale_publish.result(timeout=5)

    durable = patching_store.get_contest(contest["id"])
    assert durable["status"] == "open"
    assert durable["time_control_id"] == "pencil_per_decision_1s_v1"
    assert durable["format_snapshot_json"] == "{}"
    assert patching_store.list_contest_pairings(contest["id"]) == []
    assert all(
        entry["group_id"] == "" and entry["seed"] == 0
        for entry in patching_store.list_contest_entries(contest["id"])
    )
    patching_store.close()
    publishing_store.close()


def test_small_draw_audit_uses_private_nonce_without_public_leak(monkeypatch):
    manager = ContestManager(None, None)
    entries = [
        {
            "id": index,
            "user_id": 100 + index,
            "bot_id": 200 + index,
            "seed": 0,
            "group_id": "",
            "eliminated": 0,
        }
        for index in range(1, 5)
    ]
    stages = copy.deepcopy(get_template("pencil_group_drr")["stages"])
    stages[0]["group_count"] = 2
    monkeypatch.setattr(
        manager_module, "_secure_shuffle", lambda values: list(reversed(values))
    )
    _entries, frozen_stages, snapshot = manager._freeze_random_group_format(
        {
            "id": 7,
            "template_id": "pencil_group_drr",
            "game_id": "pencil",
            "time_control_id": "pencil_per_decision_1s_v1",
        },
        entries,
        stages,
    )
    assert len(snapshot["audit_nonce"]) == 64
    assert set(snapshot["audit_nonce"]) <= set("0123456789abcdef")
    contest = {
        "template_id": "pencil_group_drr",
        "game_id": "pencil",
        "stages_json": json.dumps(frozen_stages, ensure_ascii=False),
        "format_snapshot_json": json.dumps(
            snapshot, ensure_ascii=False, separators=(",", ":")
        )
    }
    first = public_format_snapshot(contest)
    second = public_format_snapshot(contest)
    assert first == second
    assert first is not None and first["audit_digest"] == snapshot["audit_digest"]
    assert not {"audit_nonce", "groups", "draw_order"} & set(first)
    assert public_format_snapshot({**contest, "game_id": "gomoku"}) is None
    with pytest.raises(ValueError, match="marker 与代码模板不匹配"):
        manager._stage_pairing_plan(
            {
                **contest,
                "id": 7,
                "status": "running",
                "current_stage_idx": 0,
                "game_id": "gomoku",
            },
            0,
            entry_rows=_entries,
        )


def test_public_draw_audit_rejects_signed_cross_template_and_bad_protected_band():
    groups = {
        "A": [1, 2],
        "B": [3, 4],
        "C": [5, 6],
        "D": [7, 8],
    }
    snapshot = {
        "version": 1,
        "algorithm": "protected_seed_random_balanced_v1",
        "audit_nonce": "a" * 64,
        "group_count": 4,
        "group_sizes": {group_id: 2 for group_id in groups},
        "draw_order": list(range(1, 9)),
        "groups": groups,
        "source": {
            "contest_id": 99,
            "protected": [
                {
                    "entry_id": 1 + index * 2,
                    "user_id": 101 + index,
                    "source_entry_id": 201 + index,
                    "source_rank": 1 + index,
                }
                for index in range(4)
            ],
        },
        "expected_match_count": 12,
    }
    snapshot["audit_digest"] = manager_module._format_audit_digest(snapshot)
    stages = copy.deepcopy(get_template("gomoku_seeded_group_drr_final")["stages"])
    stages[0]["group_count"] = 4
    stages[0]["advance_per_group"] = 2
    stages[1]["ranking_scope"] = 8
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))

    # The audit is internally signed and its reserved markers are syntactically
    # valid, but eight entrants can never be a protected 22–26 person format.
    assert public_format_snapshot({
        "template_id": "gomoku_seeded_group_drr_final",
        "game_id": "gomoku",
        "source_contest_id": 99,
        "stages_json": json.dumps(stages, ensure_ascii=False),
        "format_snapshot_json": raw,
    }) is None
    # Moving the same signed object onto another template must not authorize
    # protected-source semantics merely because the JSON is self-consistent.
    assert public_format_snapshot({
        "template_id": "pencil_group_drr",
        "game_id": "pencil",
        "stages_json": json.dumps(stages, ensure_ascii=False),
        "format_snapshot_json": raw,
    }) is None


def test_published_random_group_roster_damage_never_falls_back_to_snake(
    tmp_path,
):
    store = Store(str(tmp_path / "random-roster-damage.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, 7_000 + index, game_id="pencil")
        for index in range(4)
    ]
    manager = ContestManager(store, _NoDispatchOrchestrator())
    contest = manager.create(
        users_and_bots[0][0]["id"],
        "damaged frozen random groups",
        game_id="pencil",
        template_id="pencil_group_drr",
        stage_format_settings={"groups": {"group_count": 2}},
    )
    for user, bot in users_and_bots:
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")
    published = asyncio.run(manager.publish(contest["id"]))
    damaged_entry = store.list_contest_entries(contest["id"])[0]
    store.update_entry(contest["id"], damaged_entry["user_id"], group_id="")
    damaged = store.get_contest(contest["id"])
    before_contest = copy.deepcopy(damaged)
    before_entries = copy.deepcopy(store.list_contest_entries(contest["id"]))
    before_pairings = copy.deepcopy(
        store.list_contest_pairings(contest["id"], stage_idx=0)
    )
    before_group_assignment = [
        (entry["id"], entry["group_id"], entry["seed"])
        for entry in before_entries
    ]

    with pytest.raises(ValueError, match="冻结随机分组名册字段损坏"):
        manager._stage_pairing_plan(damaged, 0)
    # The lifecycle entry point rejects the stale lifecycle seal before it is
    # allowed to regenerate a plan from the damaged random-group roster.
    with pytest.raises(ValueError, match="published 赛事前序阶段证据不完整"):
        manager._ensure_published_pairings_locked(contest["id"], 0)
    assert published["format_snapshot_json"] == damaged["format_snapshot_json"]
    assert store.get_contest(contest["id"]) == before_contest
    after_entries = store.list_contest_entries(contest["id"])
    assert after_entries == before_entries
    assert [
        (entry["id"], entry["group_id"], entry["seed"])
        for entry in after_entries
    ] == before_group_assignment
    assert store.list_contest_pairings(
        contest["id"], stage_idx=0
    ) == before_pairings
    store.close()


def test_reserved_random_group_markers_cannot_be_forged_by_custom_stages():
    manager = ContestManager(None, None)
    with pytest.raises(ValueError, match="marker 仅供代码内置模板"):
        manager.create(
            1,
            "forged custom draw",
            game_id="pencil",
            stages=[
                {
                    "key": "groups",
                    "type": "group_double_round_robin",
                    "group_count": 2,
                    "group_assignment": "secure_random_balanced_v1",
                    "overall_ranking": "cross_group_fair_v1",
                }
            ],
        )


def test_reserved_random_group_markers_fail_closed_on_active_read_models():
    forged_stage = {
        "key": "groups",
        "type": "group_double_round_robin",
        "group_count": 2,
        "group_assignment": "secure_random_balanced_v1",
        "overall_ranking": "cross_group_fair_v1",
        "scoring": "ccgc_2_1_0",
    }
    contest = {
        "id": 81,
        "template_id": "custom_imported",
        "game_id": "pencil",
        "status": "running",
        "current_stage_idx": 0,
        "stages_json": json.dumps([forged_stage]),
    }
    manager = ContestManager(None, _SingleSlotEstimator())
    assert not reserved_group_markers_match_template(
        contest["template_id"], [forged_stage], game_id=contest["game_id"]
    )
    with pytest.raises(ValueError, match="marker"):
        manager._validated_active_lifecycle_stages(contest, [forged_stage])
    entries = [
        {
            "id": index,
            "user_id": 100 + index,
            "bot_id": 200 + index,
            "seed": index,
            "group_id": "A" if index < 3 else "B",
            "eliminated": 0,
        }
        for index in range(1, 5)
    ]
    assert manager.standings(
        contest["id"], contest=contest, entries=entries, pairings=[]
    ) == []
    assert build_stage_summaries(manager, contest, entries, []) == []

    reserved_stages = copy.deepcopy(get_template("pencil_group_drr")["stages"])
    wrong_game = {
        **contest,
        "template_id": "pencil_group_drr",
        "game_id": "gomoku",
        "stages_json": json.dumps(reserved_stages),
    }
    assert not reserved_group_markers_match_template(
        wrong_game["template_id"],
        reserved_stages,
        game_id=wrong_game["game_id"],
    )
    with pytest.raises(ValueError, match="marker"):
        manager._validated_lifecycle_stages(wrong_game, reserved_stages)
    with pytest.raises(ValueError, match="marker"):
        manager._validated_active_lifecycle_stages(wrong_game, reserved_stages)
    assert manager.standings(
        wrong_game["id"], contest=wrong_game, entries=entries, pairings=[]
    ) == []


def test_live_does_not_publish_reserved_group_marker_for_wrong_template(tmp_path):
    app = create_app(db_path=str(tmp_path / "forged-live-marker.db"))
    store = app.state.store
    organizer = store.create_user(
        "forged-live-organizer",
        "forged-live@example.com",
        "hash",
        role="organizer",
    )
    forged_stage = {
        "key": "groups",
        "type": "group_double_round_robin",
        "group_count": 2,
        "group_assignment": "secure_random_balanced_v1",
        "overall_ranking": "cross_group_fair_v1",
        "scoring": "ccgc_2_1_0",
    }
    contest = store.create_contest(
        "forged live marker",
        organizer["id"],
        status="running",
        game_id="pencil",
        template_id="custom_imported",
        stages_json=json.dumps([forged_stage]),
        time_control_id="pencil_per_side_total_900s_v1",
    )
    with TestClient(app) as client:
        response = client.get(f"/api/contests/{contest['id']}/live")
    assert response.status_code == 200
    payload = response.json()
    assert payload["stage"]["overall_ranking"] is None
    assert payload["series"] is None
    assert payload["standings"] == []
    assert payload["progress"]["completed"] == 0


def test_group_labels_have_no_twenty_six_group_product_cap():
    labels = manager_module._group_labels(27)
    assert labels[:3] == ["A", "B", "C"]
    assert labels[-2:] == ["Z", "AA"]


@pytest.mark.parametrize(
    ("participant_count", "expected_sizes", "expected_total"),
    [
        (22, [5, 5, 6, 6], 156),
        (23, [5, 6, 6, 6], 166),
        (24, [6, 6, 6, 6], 176),
        (25, [5, 5, 5, 5, 5], 190),
        (26, [5, 5, 5, 5, 6], 200),
    ],
)
def test_gomoku_dynamic_band_topology_and_random_draw_order(
    monkeypatch, participant_count, expected_sizes, expected_total
):
    manager = ContestManager(None, None)
    entries = [
        {
            "id": index + 1,
            "user_id": 1_000 + index,
            "bot_id": 2_000 + index,
            "seed": 0,
            "group_id": "",
            "eliminated": 0,
        }
        for index in range(participant_count)
    ]
    source_rows = [
        {
            "entry_id": 5_000 + index,
            "user_id": entry["user_id"],
            "bot_id": entry["bot_id"],
            "rank": index + 1,
        }
        for index, entry in enumerate(entries)
    ]
    monkeypatch.setattr(
        manager,
        "_complete_gomoku_source_ranking",
        lambda contest: source_rows,
    )
    monkeypatch.setattr(
        manager_module,
        "_secure_shuffle",
        lambda values: list(reversed(values)),
    )
    contest = {
        "id": 9,
        "template_id": "gomoku_seeded_group_drr_final",
        "game_id": "gomoku",
        "source_contest_id": 8,
        "time_control_id": "gomoku_per_side_total_300s_v1",
    }
    stages = copy.deepcopy(get_template("gomoku_seeded_group_drr_final")["stages"])
    frozen_entries, frozen_stages, snapshot = manager._freeze_random_group_format(
        contest, entries, stages
    )

    assert sorted(snapshot["group_sizes"].values()) == expected_sizes
    assert frozen_stages[0]["group_count"] == (4 if participant_count <= 24 else 5)
    finalists = frozen_stages[1]["ranking_scope"]
    group_matches = sum(size * (size - 1) for size in expected_sizes)
    assert group_matches + finalists * (finalists - 1) == expected_total
    protected_count = frozen_stages[0]["group_count"]
    protected = frozen_entries[:protected_count]
    assert len({entry["group_id"] for entry in protected}) == protected_count
    # Protected source rank must not leak into the independent final fallback draw.
    assert [entry["seed"] for entry in protected] != list(range(1, protected_count + 1))


def test_gomoku_source_official_identity_drift_fails_closed(tmp_path):
    store = Store(str(tmp_path / "source-identity.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, index, game_id="gomoku")
        for index in range(4)
    ]
    source = store.create_contest(
        "source",
        users_and_bots[0][0]["id"],
        game_id="gomoku",
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    source_entries = []
    for user, bot in users_and_bots:
        source_entries.append(
            store.add_contest_entry(source["id"], user["id"], bot["id"])
        )
    store.replace_official_results(
        source["id"],
        [
            {
                "entry_id": entry["id"],
                "rank": index + 1,
                "bot_id": entry["bot_id"],
                "user_id": entry["user_id"],
            }
            for index, entry in enumerate(source_entries)
        ],
    )
    _mark_imported_contest_finished(store, source["id"])
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_official_results SET user_id=? WHERE contest_id=? AND rank=1",
            (source_entries[1]["user_id"], source["id"]),
        )

    manager = ContestManager(store, _NoDispatchOrchestrator())
    with pytest.raises(ValueError, match="损坏或不完整"):
        manager._complete_gomoku_source_ranking(
            {"source_contest_id": source["id"], "game_id": "gomoku"}
        )
    store.close()


def test_gomoku_source_group_coordinates_are_complete_and_roster_bound(tmp_path):
    store = Store(str(tmp_path / "source-group-coordinates.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, 100 + index, game_id="gomoku")
        for index in range(4)
    ]
    source = store.create_contest(
        "grouped source",
        users_and_bots[0][0]["id"],
        game_id="gomoku",
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    source_entries = [
        store.add_contest_entry(source["id"], user["id"], bot["id"])
        for user, bot in users_and_bots
    ]
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        for index, entry in enumerate(source_entries):
            connection.execute(
                "UPDATE contest_entries SET group_id=? WHERE id=?",
                ("A" if index < 2 else "B", entry["id"]),
            )
    def official_rows(coordinates):
        return [
            {
                "entry_id": entry["id"],
                "rank": index + 1,
                "bot_id": entry["bot_id"],
                "user_id": entry["user_id"],
                "group_id": group_id,
                "rank_in_group": rank_in_group,
                "tiebreaks_json": json.dumps(
                    _official_tiebreaks(0, index + 1)
                ),
            }
            for index, (entry, (group_id, rank_in_group)) in enumerate(
                zip(source_entries, coordinates)
            )
        ]

    manager = ContestManager(store, _NoDispatchOrchestrator())
    source_ref = {"source_contest_id": source["id"], "game_id": "gomoku"}

    with pytest.raises(ValueError, match="组内名次"):
        store.replace_official_results(
            source["id"],
            official_rows([("A", 1), ("A", 3), ("B", 1), ("B", 2)]),
        )

    with pytest.raises(ValueError, match="冻结名册"):
        store.replace_official_results(
            source["id"],
            official_rows([("A", 1), ("B", 1), ("B", 2), ("B", 3)]),
        )

    # A later non-group final remains authoritative even if its roster still
    # remembers earlier group membership; roster data must not reinterpret it.
    store.replace_official_results(
        source["id"], official_rows([("", None)] * len(source_entries))
    )
    _mark_imported_contest_finished(store, source["id"])
    assert len(manager._complete_gomoku_source_ranking(source_ref)) == 4

    store.replace_official_results(
        source["id"], official_rows([("A", 1), ("A", 2), ("B", 1), ("B", 2)])
    )
    assert len(manager._complete_gomoku_source_ranking(source_ref)) == 4
    store.close()


def test_gomoku_publish_transaction_rechecks_source_group_coordinates(
    tmp_path, monkeypatch
):
    path = tmp_path / "source-group-coordinate-race.db"
    publishing_store = Store(str(path))
    users_and_bots = [
        _fixture_bot(publishing_store, tmp_path, 500 + index, game_id="gomoku")
        for index in range(22)
    ]
    source = publishing_store.create_contest(
        "source before coordinate drift",
        users_and_bots[0][0]["id"],
        game_id="gomoku",
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    source_entries = [
        publishing_store.add_contest_entry(source["id"], user["id"], bot["id"])
        for user, bot in users_and_bots
    ]
    publishing_store.replace_official_results(
        source["id"],
        [
            {
                "entry_id": entry["id"],
                "rank": index + 1,
                "bot_id": entry["bot_id"],
                "user_id": entry["user_id"],
            }
            for index, entry in enumerate(source_entries)
        ],
    )
    _mark_imported_contest_finished(publishing_store, source["id"])
    manager = ContestManager(publishing_store, _NoDispatchOrchestrator())
    target = manager.create(
        users_and_bots[0][0]["id"],
        "transactional source coordinate recheck",
        game_id="gomoku",
        template_id="gomoku_seeded_group_drr_final",
        source_contest_id=source["id"],
    )
    for user, bot in users_and_bots:
        publishing_store.add_contest_entry(target["id"], user["id"], bot["id"])
    publishing_store.update_contest(target["id"], status="open")

    mutating_store = Store(str(path))
    publish_ready = threading.Event()
    mutation_committed = threading.Event()
    original_freeze = publishing_store.freeze_initial_group_contest

    def pause_before_freeze(*args, **kwargs):
        publish_ready.set()
        assert mutation_committed.wait(timeout=5)
        return original_freeze(*args, **kwargs)

    monkeypatch.setattr(
        publishing_store, "freeze_initial_group_contest", pause_before_freeze
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_publish = executor.submit(
            lambda: asyncio.run(manager.publish(target["id"]))
        )
        assert publish_ready.wait(timeout=5)
        with mutating_store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE contest_official_results "
                "SET group_id='A',rank_in_group=1 "
                "WHERE contest_id=? AND rank=1",
                (source["id"],),
            )
        mutation_committed.set()
        with pytest.raises(ValueError, match="来源正式榜已变化或损坏"):
            stale_publish.result(timeout=5)

    durable = mutating_store.get_contest(target["id"])
    assert durable["status"] == "open"
    assert durable["format_snapshot_json"] == "{}"
    assert mutating_store.list_contest_pairings(target["id"]) == []
    mutating_store.close()
    publishing_store.close()


def test_gomoku_publish_transaction_revalidates_complete_source_rows(
    tmp_path, monkeypatch
):
    """Every official-result field is revalidated after manager preparation."""
    path = tmp_path / "source-complete-row-race.db"
    publishing_store = Store(str(path))
    users_and_bots = [
        _fixture_bot(publishing_store, tmp_path, 600 + index, game_id="gomoku")
        for index in range(22)
    ]
    source = publishing_store.create_contest(
        "source before complete-row drift",
        users_and_bots[0][0]["id"],
        game_id="gomoku",
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    source_entries = [
        publishing_store.add_contest_entry(source["id"], user["id"], bot["id"])
        for user, bot in users_and_bots
    ]
    official_rows = [
        {
            "entry_id": entry["id"],
            "rank": index + 1,
            "bot_id": entry["bot_id"],
            "user_id": entry["user_id"],
        }
        for index, entry in enumerate(source_entries)
    ]
    publishing_store.replace_official_results(source["id"], official_rows)
    _mark_imported_contest_finished(publishing_store, source["id"])

    manager = ContestManager(publishing_store, _NoDispatchOrchestrator())
    target = manager.create(
        users_and_bots[0][0]["id"],
        "transactional complete source recheck",
        game_id="gomoku",
        template_id="gomoku_seeded_group_drr_final",
        source_contest_id=source["id"],
    )
    for user, bot in users_and_bots:
        publishing_store.add_contest_entry(target["id"], user["id"], bot["id"])
    publishing_store.update_contest(target["id"], status="open")

    mutating_store = Store(str(path))
    before_contest = mutating_store.get_contest(target["id"])
    before_entries = [
        (row["id"], row["group_id"], row["seed"], row["eliminated"])
        for row in mutating_store.list_contest_entries(target["id"])
    ]
    publish_ready = threading.Event()
    mutation_committed = threading.Event()
    original_freeze = publishing_store.freeze_initial_group_contest

    def pause_before_freeze(*args, **kwargs):
        publish_ready.set()
        assert mutation_committed.wait(timeout=5)
        return original_freeze(*args, **kwargs)

    monkeypatch.setattr(
        publishing_store, "freeze_initial_group_contest", pause_before_freeze
    )
    corruptions = (
        ("tiebreaks_json", "{"),
        ("stage_idx", -1),
        ("points", "not-a-number"),
    )
    for column, value in corruptions:
        publish_ready.clear()
        mutation_committed.clear()
        with ThreadPoolExecutor(max_workers=1) as executor:
            stale_publish = executor.submit(
                lambda: asyncio.run(manager.publish(target["id"]))
            )
            assert publish_ready.wait(timeout=5)
            with mutating_store._tx() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    f"UPDATE contest_official_results SET {column}=? "
                    "WHERE contest_id=? AND rank=1",
                    (value, source["id"]),
                )
            mutation_committed.set()
            with pytest.raises(ValueError, match="来源正式榜已变化或损坏"):
                stale_publish.result(timeout=5)

        durable = mutating_store.get_contest(target["id"])
        assert durable["status"] == before_contest["status"] == "open"
        assert durable["stages_json"] == before_contest["stages_json"]
        assert durable["format_snapshot_json"] == before_contest[
            "format_snapshot_json"
        ] == "{}"
        assert [
            (row["id"], row["group_id"], row["seed"], row["eliminated"])
            for row in mutating_store.list_contest_entries(target["id"])
        ] == before_entries
        assert mutating_store.list_contest_pairings(target["id"]) == []
        mutating_store.replace_official_results(source["id"], official_rows)

    mutating_store.close()
    publishing_store.close()


def test_gomoku_formal_creation_requires_a_completed_same_game_source(tmp_path):
    store = Store(str(tmp_path / "source-required.db"))
    user, _bot = _fixture_bot(store, tmp_path, 900, game_id="gomoku")
    manager = ContestManager(store, _NoDispatchOrchestrator())

    with pytest.raises(ValueError, match="必须选择"):
        manager.create(
            user["id"],
            "missing source",
            game_id="gomoku",
            template_id="gomoku_seeded_group_drr_final",
        )

    unfinished = store.create_contest(
        "unfinished gomoku source",
        user["id"],
        game_id="gomoku",
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    with pytest.raises(ValueError, match="已完成且正式榜就绪"):
        manager.create(
            user["id"],
            "unfinished source",
            game_id="gomoku",
            template_id="gomoku_seeded_group_drr_final",
            source_contest_id=unfinished["id"],
        )

    wrong_game = store.create_contest(
        "finished pencil source",
        user["id"],
        game_id="pencil",
        time_control_id="pencil_per_side_total_900s_v1",
    )
    _mark_imported_contest_finished(
        store, wrong_game["id"], official_results_ready=True
    )
    with pytest.raises(ValueError, match="已完成且正式榜就绪"):
        manager.create(
            user["id"],
            "wrong game source",
            game_id="gomoku",
            template_id="gomoku_seeded_group_drr_final",
            source_contest_id=wrong_game["id"],
        )

    assert all(
        contest.get("template_id") != "gomoku_seeded_group_drr_final"
        for contest in store.list_contests()
    )
    store.close()


def test_gomoku_publication_atomically_freezes_source_draw_versions_and_schedule(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "gomoku-publication.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, index, game_id="gomoku")
        for index in range(22)
    ]
    source = store.create_contest(
        "completed simulation",
        users_and_bots[0][0]["id"],
        game_id="gomoku",
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    source_entries = [
        store.add_contest_entry(source["id"], user["id"], bot["id"])
        for user, bot in users_and_bots
    ]
    store.replace_official_results(
        source["id"],
        [
            {
                "entry_id": entry["id"],
                "rank": index + 1,
                "bot_id": entry["bot_id"],
                "user_id": entry["user_id"],
            }
            for index, entry in enumerate(source_entries)
        ],
    )
    _mark_imported_contest_finished(store, source["id"])
    manager = ContestManager(store, _NoDispatchOrchestrator())
    target = manager.create(
        users_and_bots[0][0]["id"],
        "official seeded groups",
        game_id="gomoku",
        template_id="gomoku_seeded_group_drr_final",
        source_contest_id=source["id"],
    )
    for user, bot in users_and_bots:
        store.add_contest_entry(target["id"], user["id"], bot["id"])
    store.update_contest(target["id"], status="open")
    monkeypatch.setattr(
        manager_module,
        "_secure_shuffle",
        lambda values: list(reversed(values)),
    )

    published = asyncio.run(manager.publish(target["id"]))
    assert published["status"] == "published"
    assert published["time_control_id"] == "gomoku_per_side_total_300s_v1"
    assert len(store.list_contest_pairings(target["id"], stage_idx=0)) == 100
    public_draw = public_format_snapshot(published)
    assert public_draw is not None
    assert public_draw["expected_match_count"] == 156
    assert public_draw["group_sizes"] == {"A": 5, "B": 5, "C": 6, "D": 6}
    assert len({
        entry["group_id"]
        for entry in store.list_contest_entries(target["id"])
        if entry["user_id"] in {row["user_id"] for row in source_entries[:4]}
    }) == 4
    store.close()


def test_gomoku_publish_rechecks_current_top_registered_source_rows_in_tx(
    tmp_path, monkeypatch
):
    """A stale candidate must not freeze after the source table is replaced.

    Rank 2 initially belongs to an absent source entrant, so the candidate
    protected set is ranks 1/3/4/5.  While publication is paused immediately
    before its ``BEGIN IMMEDIATE`` freeze, a second Store swaps that absent row
    with a registered rank-6 entrant.  Every stale protected tuple still exists,
    but the authoritative top-four registered sequence has changed to 1/2/3/4.
    """
    path = tmp_path / "gomoku-source-rank-race.db"
    publishing_store = Store(str(path))
    users_and_bots = [
        _fixture_bot(publishing_store, tmp_path, 3_000 + index, game_id="gomoku")
        for index in range(23)
    ]
    source = publishing_store.create_contest(
        "source with one absent finalist",
        users_and_bots[0][0]["id"],
        game_id="gomoku",
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    source_entries = [
        publishing_store.add_contest_entry(source["id"], user["id"], bot["id"])
        for user, bot in users_and_bots
    ]
    def official_rows(order: list[int]) -> list[dict]:
        return [
            {
                "entry_id": source_entries[source_index]["id"],
                "rank": rank,
                "bot_id": source_entries[source_index]["bot_id"],
                "user_id": source_entries[source_index]["user_id"],
            }
            for rank, source_index in enumerate(order, start=1)
        ]

    initial_order = list(range(23))
    publishing_store.replace_official_results(
        source["id"], official_rows(initial_order)
    )
    _mark_imported_contest_finished(publishing_store, source["id"])
    manager = ContestManager(publishing_store, _NoDispatchOrchestrator())
    target = manager.create(
        users_and_bots[0][0]["id"],
        "stale protected source candidate",
        game_id="gomoku",
        template_id="gomoku_seeded_group_drr_final",
        source_contest_id=source["id"],
    )
    # Source index 1 is absent; index 5 is registered and initially rank 6.
    registered_indices = [0, *range(2, 23)]
    for index in registered_indices:
        user, bot = users_and_bots[index]
        publishing_store.add_contest_entry(target["id"], user["id"], bot["id"])
    publishing_store.update_contest(target["id"], status="open")

    replacing_store = Store(str(path))
    publish_ready = threading.Event()
    replacement_committed = threading.Event()
    original_freeze = publishing_store.freeze_initial_group_contest

    def pause_before_freeze(*args, **kwargs):
        publish_ready.set()
        assert replacement_committed.wait(timeout=5)
        return original_freeze(*args, **kwargs)

    monkeypatch.setattr(
        publishing_store, "freeze_initial_group_contest", pause_before_freeze
    )
    with ThreadPoolExecutor(max_workers=1) as executor:
        stale_publish = executor.submit(
            lambda: asyncio.run(manager.publish(target["id"]))
        )
        assert publish_ready.wait(timeout=5)
        changed_order = list(range(23))
        changed_order[1], changed_order[5] = changed_order[5], changed_order[1]
        replacing_store.replace_official_results(
            source["id"], official_rows(changed_order)
        )
        replacement_committed.set()
        with pytest.raises(ValueError, match="保护种子来源冻结不一致"):
            stale_publish.result(timeout=5)

    durable = replacing_store.get_contest(target["id"])
    assert durable["status"] == "open"
    assert durable["format_snapshot_json"] == "{}"
    assert replacing_store.list_contest_pairings(target["id"]) == []
    assert all(
        entry["group_id"] == "" and entry["seed"] == 0
        for entry in replacing_store.list_contest_entries(target["id"])
    )
    replacing_store.close()
    publishing_store.close()


def test_gomoku_final_transition_is_atomic_and_preserves_all_draw_group_snapshot(
    tmp_path, monkeypatch
):
    """Entry seeds, eliminations, final pairings and cursor commit as one unit.

    The qualifier is intentionally an all-draw table, where overwriting
    ``contest_entries.seed`` for final seating would otherwise change a later
    recomputation.  Official fallback ranks must come from the exact persisted
    qualifier snapshot that selected the finalists.
    """
    store = Store(str(tmp_path / "gomoku-stage-transition.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, 2_000 + index, game_id="gomoku")
        for index in range(22)
    ]
    source = store.create_contest(
        "protected source fixture",
        users_and_bots[0][0]["id"],
        game_id="gomoku",
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    stages = copy.deepcopy(get_template("gomoku_seeded_group_drr_final")["stages"])
    stages[0]["group_count"] = 4
    stages[1]["ranking_scope"] = 8
    contest = store.create_contest(
        "atomic protected final",
        users_and_bots[0][0]["id"],
        status="published",
        game_id="gomoku",
        template_id="gomoku_seeded_group_drr_final",
        source_contest_id=source["id"],
        time_control_id="gomoku_per_side_total_300s_v1",
        stages_json=json.dumps(stages, ensure_ascii=False),
    )
    group_sizes = {"A": 6, "B": 6, "C": 5, "D": 5}
    entries: list[dict] = []
    offset = 0
    for group_id, size in group_sizes.items():
        for user, bot in users_and_bots[offset : offset + size]:
            entry = store.add_contest_entry(contest["id"], user["id"], bot["id"])
            store.update_entry(
                contest["id"],
                user["id"],
                group_id=group_id,
                seed=len(entries) + 1,
                eliminated=0,
            )
            entries.append(store.get_entry(contest["id"], user["id"]))
        offset += size

    by_group: dict[str, list[dict]] = {}
    for entry in entries:
        by_group.setdefault(entry["group_id"], []).append(entry)
    snapshot = {
        "version": 1,
        "algorithm": "protected_seed_random_balanced_v1",
        "audit_nonce": "0" * 64,
        "group_count": 4,
        "group_sizes": group_sizes,
        "draw_order": [entry["id"] for entry in entries],
        "groups": {
            group_id: [entry["id"] for entry in group_entries]
            for group_id, group_entries in by_group.items()
        },
        "source": {
            "contest_id": source["id"],
            "protected": [
                {
                    "entry_id": by_group[group_id][0]["id"],
                    "user_id": by_group[group_id][0]["user_id"],
                    "source_entry_id": index,
                    "source_rank": index,
                }
                for index, group_id in enumerate(sorted(by_group), start=1)
            ],
        },
        "expected_match_count": 156,
    }
    snapshot["audit_digest"] = manager_module._format_audit_digest(snapshot)
    with store._tx() as connection:
        connection.execute(
            "UPDATE contests SET format_snapshot_json=? WHERE id=?",
            (json.dumps(snapshot, ensure_ascii=False), contest["id"]),
        )
    qualifier_order: list[dict] = []
    for rank_in_group in range(1, max(group_sizes.values()) + 1):
        qualifier_order.extend(
            rows[rank_in_group - 1]
            for _group_id, rows in sorted(by_group.items())
            if rank_in_group <= len(rows)
        )

    def qualifier_row(entry, overall_rank):
        rank_in_group = by_group[entry["group_id"]].index(entry) + 1
        return {
            "entry_id": entry["id"],
            "bot_id": entry["bot_id"],
            "user_id": entry["user_id"],
            "rank": overall_rank,
            "overall_rank": overall_rank,
            "rank_in_group": rank_in_group,
            "group_id": entry["group_id"],
            "points": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "delta_total": 0,
            "tiebreaks": {
                "points": 0,
                "buchholz": 0,
                "buchholz_cut1": 0,
                "sonneborn_berger": 0,
                "head_to_head": 0,
                "normalized_delta": 0,
                "technical_losses": 0,
                "seed": entry["seed"],
                "group_rank": rank_in_group,
                "points_rate": 0.5,
                "opponent_strength": 0.5,
                "normalized_delta_rate": 0,
                "technical_loss_rate": 0,
                "draw_order": entry["seed"],
            },
        }

    qualifier_ranking = [
        qualifier_row(entry, rank)
        for rank, entry in enumerate(qualifier_order, start=1)
    ]
    manager = ContestManager(store, _NoDispatchOrchestrator())

    async def no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(manager, "_dispatch_pending_locked", no_dispatch)
    monkeypatch.setattr(
        manager,
        "_rank_stage_rows",
        lambda _contest_id, stage_idx, **_kwargs: (
            copy.deepcopy(qualifier_ranking) if stage_idx == 0 else []
        ),
    )

    # Persist a real frozen stage-0 topology so recovery can prove that the
    # snapshot covers all 22 entrants after only eight remain active.
    bot_groups = {
        group_id: [entry["bot_id"] for entry in group_entries]
        for group_id, group_entries in by_group.items()
    }
    stage = stages[0]
    specs = frozen_group_round_robin(bot_groups, double=True)
    entry_map = {entry["bot_id"]: entry["id"] for entry in entries}
    stage_zero_rows = manager._pairing_rows_for_plan(
        contest["id"], 0, stage, specs, entry_map, base=None
    )
    store.create_contest_stage_pairings(
        contest["id"],
        0,
        stage_zero_rows,
        expected_current_stage_idx=0,
        expected_status="published",
        activate_running=True,
    )
    # Settle the complete materialized qualifier before installing its
    # immutable all-draw decision.  A ranking snapshot may never authorize a
    # transition from topology alone.
    for pairing in store.list_contest_pairings(contest["id"], stage_idx=0):
        match_id = f"protected-qualifier-{pairing['id']}"
        store.create_match(
            match_id,
            pairing["bot_a_id"],
            pairing["bot_b_id"],
            owner_id=users_and_bots[0][0]["id"],
            contest_id=contest["id"],
            match_type="contest",
            game_id="gomoku",
            match_config={
                "time_control_id": "gomoku_per_side_total_300s_v1"
            },
        )
        store.bind_contest_pairing_match(
            contest["id"],
            pairing["id"],
            match_id,
            require_execution_admission=False,
        )
        store.update_match(
            match_id,
            status="completed",
            winner=None,
            result={"deltas": [0, 0]},
        )
        assert store.complete_contest_pairing_for_match(
            contest["id"], match_id
        )
    manager._snapshot_stage_results(contest["id"], 0)

    before = {
        entry["id"]: (entry["seed"], entry["eliminated"])
        for entry in store.list_contest_entries(contest["id"])
    }
    with store._tx() as connection:
        connection.execute(
            "CREATE TEMP TRIGGER fail_protected_final_pairing "
            "BEFORE INSERT ON contest_pairings "
            "WHEN NEW.stage_idx=1 AND (SELECT COUNT(*) FROM contest_pairings "
            "WHERE contest_id=NEW.contest_id AND stage_idx=1)=1 "
            "BEGIN SELECT RAISE(ABORT, 'injected protected final failure'); END"
        )
    with pytest.raises(sqlite3.DatabaseError, match="injected protected final failure"):
        asyncio.run(manager._advance_and_begin_stage(contest["id"], 0))
    failed_transition = store.get_contest(contest["id"])
    assert failed_transition["current_stage_idx"] == 0
    assert failed_transition["published_stage_pairing_count"] == len(stage_zero_rows)
    assert store.list_contest_pairings(contest["id"], stage_idx=1) == []
    assert before == {
        entry["id"]: (entry["seed"], entry["eliminated"])
        for entry in store.list_contest_entries(contest["id"])
    }

    with store._tx() as connection:
        connection.execute("DROP TRIGGER fail_protected_final_pairing")
    asyncio.run(manager._advance_and_begin_stage(contest["id"], 0))
    transitioned = store.get_contest(contest["id"])
    assert transitioned["status"] == "running"
    assert transitioned["current_stage_idx"] == 1
    final_pairings = store.list_contest_pairings(contest["id"], stage_idx=1)
    assert len(final_pairings) == 56
    assert transitioned["published_stage_pairing_count"] == len(final_pairings)
    finalist_ids = {
        row["entry_id"]
        for row in qualifier_ranking
        if row["rank_in_group"] <= 2
    }
    assert {
        pairing[key]
        for pairing in final_pairings
        for key in ("entry_a_id", "entry_b_id")
    } == finalist_ids
    post_transition = store.list_contest_entries(contest["id"])
    assert {row["id"] for row in post_transition if not row["eliminated"]} == finalist_ids
    expected_final_seed_order = [
        by_group[group_id][rank_in_group - 1]["id"]
        for rank_in_group in (1, 2)
        for group_id in sorted(by_group)
    ]
    assert [
        row["id"]
        for row in sorted(
            (row for row in post_transition if not row["eliminated"]),
            key=lambda row: row["seed"],
        )
    ] == expected_final_seed_order

    recovered_qualifier = manager._stage_ranking_from_recovery_snapshot(
        contest["id"], 0
    )
    assert recovered_qualifier is not None
    assert [row["entry_id"] for row in recovered_qualifier] == [
        row["entry_id"] for row in qualifier_ranking
    ]
    with store._tx() as connection:
        connection.execute(
            "UPDATE contest_stage_results SET rank_in_group=rank_in_group+1 "
            "WHERE contest_id=? AND stage_idx=0 AND group_id='A'",
            (contest["id"],),
        )
    assert manager._stage_ranking_from_recovery_snapshot(contest["id"], 0) is None
    with store._tx() as connection:
        connection.execute(
            "UPDATE contest_stage_results SET rank_in_group=rank_in_group-1 "
            "WHERE contest_id=? AND stage_idx=0 AND group_id='A'",
            (contest["id"],),
        )
    assert manager._stage_ranking_from_recovery_snapshot(contest["id"], 0) is not None

    finalist_by_seed = sorted(
        (row for row in post_transition if row["id"] in finalist_ids),
        key=lambda row: row["seed"],
    )
    final_ranking = [
        {
            "entry_id": entry["id"],
            "bot_id": entry["bot_id"],
            "user_id": entry["user_id"],
            "rank": rank,
            "points": 0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "delta_total": 0,
            "group_id": "",
            "rank_in_group": None,
            "tiebreaks": {
                "points": 0,
                "buchholz": 0,
                "buchholz_cut1": 0,
                "sonneborn_berger": 0,
                "head_to_head": 0,
                "normalized_delta": 0,
                "technical_losses": 0,
                "seed": entry["seed"],
            },
        }
        for rank, entry in enumerate(finalist_by_seed, start=1)
    ]
    monkeypatch.setattr(
        manager,
        "_rank_stage_rows",
        lambda _contest_id, stage_idx, **_kwargs: (
            copy.deepcopy(final_ranking)
            if stage_idx == 1
            else list(reversed(copy.deepcopy(qualifier_ranking)))
        ),
    )
    manager._finalize_official_results(contest["id"], 1)
    official = store.list_official_results(contest["id"])
    expected_nonfinalists = [
        row["entry_id"]
        for row in qualifier_ranking
        if row["entry_id"] not in finalist_ids
    ]
    assert [row["entry_id"] for row in official[8:]] == expected_nonfinalists
    fallback_entry_id = expected_nonfinalists[0]
    with store._tx() as connection:
        original_tiebreaks_json = connection.execute(
            "SELECT tiebreaks_json FROM contest_official_results "
            "WHERE contest_id=? AND entry_id=?",
            (contest["id"], fallback_entry_id),
        ).fetchone()[0]
    original_tiebreaks = json.loads(original_tiebreaks_json)
    for field in (
        "group_rank",
        "points_rate",
        "opponent_strength",
        "normalized_delta_rate",
        "technical_loss_rate",
        "draw_order",
    ):
        damaged = dict(original_tiebreaks)
        damaged.pop(field)
        with store._tx() as connection:
            connection.execute(
                "UPDATE contest_official_results SET tiebreaks_json=? "
                "WHERE contest_id=? AND entry_id=?",
                (json.dumps(damaged), contest["id"], fallback_entry_id),
            )
        with pytest.raises(ValueError):
            store.list_official_results(contest["id"])
        with store._tx() as connection:
            connection.execute(
                "UPDATE contest_official_results SET tiebreaks_json=? "
                "WHERE contest_id=? AND entry_id=?",
                (original_tiebreaks_json, contest["id"], fallback_entry_id),
            )
    assert len(store.list_official_results(contest["id"])) == 22
    store.close()


def test_gomoku_force_finish_requires_completed_final_stage_without_writes(
    tmp_path,
):
    store = Store(str(tmp_path / "gomoku-force-finish-stage.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, 8_000 + index, game_id="gomoku")
        for index in range(22)
    ]
    source = store.create_contest(
        "completed gomoku source",
        users_and_bots[0][0]["id"],
        game_id="gomoku",
        status="finished",
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    source_entries = [
        store.add_contest_entry(source["id"], user["id"], bot["id"])
        for user, bot in users_and_bots
    ]
    store.replace_official_results(
        source["id"],
        [
            {
                "entry_id": entry["id"],
                "rank": rank,
                "bot_id": entry["bot_id"],
                "user_id": entry["user_id"],
            }
            for rank, entry in enumerate(source_entries, start=1)
        ],
    )

    manager = ContestManager(store, _PersistingContestOrchestrator(store))
    contest = manager.create(
        users_and_bots[0][0]["id"],
        "protected force-finish gate",
        game_id="gomoku",
        template_id="gomoku_seeded_group_drr_final",
        source_contest_id=source["id"],
    )
    for user, bot in users_and_bots:
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")

    def complete_stage(stage_idx: int) -> list[dict]:
        pairings = store.list_contest_pairings(
            contest["id"], stage_idx=stage_idx
        )
        for pairing in pairings:
            match_id = pairing.get("match_id")
            assert match_id
            store.update_match(
                match_id,
                status="completed",
                winner=0,
                reason="five",
                result={"deltas": [1, -1]},
            )
            assert store.complete_contest_pairing_for_match(
                contest["id"], match_id
            )
        return pairings

    async def exercise() -> None:
        await manager.publish(contest["id"])
        await manager.start(contest["id"])
        stage_zero = complete_stage(0)
        assert len(stage_zero) == 100
        assert manager._has_unfinished_pairings(contest["id"]) is False

        running_before = store.get_contest(contest["id"])
        with pytest.raises(ValueError, match="尚未完成决赛阶段"):
            await manager.finish(contest["id"])
        assert store.get_contest(contest["id"]) == running_before
        assert store.list_official_results(contest["id"]) == []

        resting = await manager.maybe_finish(contest["id"])
        assert resting and resting["status"] == "rest"
        assert resting["current_stage_idx"] == 0
        rest_before = store.get_contest(contest["id"])
        with pytest.raises(ValueError, match="尚未完成决赛阶段"):
            await manager.finish(contest["id"])
        assert store.get_contest(contest["id"]) == rest_before
        assert store.list_official_results(contest["id"]) == []

        advanced = await manager.resume(contest["id"])
        assert advanced["status"] == "running"
        assert advanced["current_stage_idx"] == 1

        final_pairings = complete_stage(1)
        assert len(final_pairings) == 56
        assert manager._has_unfinished_pairings(contest["id"]) is False
        finished = await manager.maybe_finish(contest["id"])
        assert finished and finished["status"] == "finished"
        assert finished["current_stage_idx"] == 1
        assert finished["official_results_ready"] == 1
        official = store.list_official_results(contest["id"])
        assert len(official) == 22
        assert {row["stage_idx"] for row in official} == {1}

    asyncio.run(exercise())
    store.close()


def test_cross_group_ranking_projects_both_coordinates_for_scheduled_draw():
    groups = {"A": [101, 102], "B": [103, 104, 105]}
    specs = frozen_group_round_robin(groups, double=True)
    bot_to_entry = {bot_id: index + 1 for index, bot_id in enumerate(range(101, 106))}
    entry_to_bot = {entry_id: bot_id for bot_id, entry_id in bot_to_entry.items()}
    pairings = [
        {
            "id": index,
            "contest_id": 1,
            "entry_a_id": bot_to_entry[spec.bot_a_id],
            "entry_b_id": bot_to_entry[spec.bot_b_id],
            "bot_a_id": spec.bot_a_id,
            "bot_b_id": spec.bot_b_id,
            "group_id": spec.group_id,
            "match_id": None,
            "series_index": 1,
            "series_size": 1,
        }
        for index, spec in enumerate(specs, start=1)
    ]
    standings = [
        {
            "entry_id": entry_id,
            "user_id": 100 + entry_id,
            "bot_id": bot_id,
            "group_id": "A" if bot_id < 103 else "B",
            "seed": entry_id,
            "points": 0.0,
            "wins": 0,
            "draws": 0,
            "losses": 0,
            "delta_total": 0,
        }
        for entry_id, bot_id in entry_to_bot.items()
    ]
    stage = {
        "key": "groups",
        "type": "group_double_round_robin",
        "group_count": 2,
        "group_assignment": "secure_random_balanced_v1",
        "overall_ranking": "cross_group_fair_v1",
        "scoring": "ccgc_2_1_0",
    }
    ranked = compute_cross_group_ranking(
        standings,
        pairings,
        {},
        normalize_delta=float,
        stage=stage,
        planned_games_per_match=1,
        fixed_rounds_per_match=None,
        game_id="pencil",
        expected_contest_id=1,
        expected_entry_bots=entry_to_bot,
        expected_entry_users={entry_id: 100 + entry_id for entry_id in entry_to_bot},
    )
    assert len(ranked) == 5
    assert [row["overall_rank"] for row in ranked] == [1, 2, 3, 4, 5]
    assert sorted(row["rank_in_group"] for row in ranked) == [1, 1, 2, 2, 3]
    assert all("draw_order" in row["tiebreaks"] for row in ranked)

    mislabeled = copy.deepcopy(pairings)
    original_group = mislabeled[0]["group_id"]
    mislabeled[0]["group_id"] = next(
        group_id for group_id in groups if group_id != original_group
    )
    assert compute_cross_group_ranking(
        standings,
        mislabeled,
        {},
        normalize_delta=float,
        stage=stage,
        planned_games_per_match=1,
        fixed_rounds_per_match=None,
        game_id="pencil",
        expected_contest_id=1,
        expected_entry_bots=entry_to_bot,
        expected_entry_users={
            entry_id: 100 + entry_id for entry_id in entry_to_bot
        },
    ) == []


def test_cross_group_advancement_zone_uses_authoritative_group_rank():
    stage = {
        "type": "group_double_round_robin",
        "advance_per_group": 2,
        "overall_ranking": "cross_group_fair_v1",
    }
    # Deliberately put the third-place row first.  Presentation must not infer
    # qualifiers from list order, points, or delta after the official chain has
    # already frozen rank_in_group.
    rows = [
        {"entry_id": 3, "group_id": "A", "rank_in_group": 3, "points": 9},
        {"entry_id": 2, "group_id": "A", "rank_in_group": 2, "points": 1},
        {"entry_id": 1, "group_id": "A", "rank_in_group": 1, "points": 1},
        {"entry_id": 6, "group_id": "B", "rank_in_group": 3, "points": 9},
        {"entry_id": 4, "group_id": "B", "rank_in_group": 1, "points": 1},
        {"entry_id": 5, "group_id": "B", "rank_in_group": 2, "points": 1},
    ]
    assert _advancement_zone(stage, rows) == {1, 2, 4, 5}
    assert _advancement_zone(
        stage,
        [{**row, "rank_in_group": 1.5} if row["entry_id"] == 1 else row for row in rows],
    ) == set()
    assert _advancement_zone(
        stage,
        [
            {**row, "rank_in_group": row["rank_in_group"] + 1}
            if row["group_id"] == "A"
            else row
            for row in rows
        ],
    ) == set()


@pytest.mark.parametrize(
    ("rank_field", "rank_flag"),
    [
        ("_persisted_rank", "use_persisted_rank"),
        ("_computed_rank", "use_computed_rank"),
    ],
)
def test_cross_group_stage_rows_follow_authoritative_overall_rank(
    rank_field, rank_flag
):
    rows = [
        {
            "entry_id": 2,
            "group_id": "A",
            "overall_rank": 2,
            "rank_in_group": 1,
            rank_field: 1 if rank_field == "_persisted_rank" else 2,
        },
        {
            "entry_id": 1,
            "group_id": "B",
            "overall_rank": 1,
            "rank_in_group": 1,
            rank_field: 1,
        },
    ]

    ranked = _rank_rows(
        copy.deepcopy(rows),
        grouped=True,
        cross_group_overall=True,
        **{rank_flag: True},
    )

    assert [row["overall_rank"] for row in ranked] == [1, 2]
    assert [row["group_id"] for row in ranked] == ["B", "A"]
    assert [row["rank"] for row in ranked] == [1, 2]


def test_ordinary_and_group_only_stage_row_order_stays_scope_local():
    ordinary = _rank_rows(
        [
            {"entry_id": 2, "_persisted_rank": 2},
            {"entry_id": 1, "_persisted_rank": 1},
        ],
        grouped=False,
        use_persisted_rank=True,
    )
    assert [row["entry_id"] for row in ordinary] == [1, 2]
    assert [row["rank"] for row in ordinary] == [1, 2]

    group_only = _rank_rows(
        [
            {"entry_id": 4, "group_id": "B", "_persisted_rank": 2},
            {"entry_id": 2, "group_id": "A", "_persisted_rank": 2},
            {"entry_id": 3, "group_id": "B", "_persisted_rank": 1},
            {"entry_id": 1, "group_id": "A", "_persisted_rank": 1},
        ],
        grouped=True,
        use_persisted_rank=True,
    )
    assert [
        (row["group_id"], row["rank"], row["entry_id"])
        for row in group_only
    ] == [("A", 1, 1), ("A", 2, 2), ("B", 1, 3), ("B", 2, 4)]


@pytest.mark.parametrize("corruption", ["duplicate", "missing", "gap", "bool"])
def test_ordinary_persisted_stage_rejects_incomplete_rank_coordinates(corruption):
    rows = [
        {
            "entry_id": 1,
            "_persisted_rank": 1,
            "points": 0,
            "delta_total": -10,
        },
        {
            "entry_id": 2,
            "_persisted_rank": 2,
            "points": 9,
            "delta_total": 10,
        },
    ]
    if corruption == "duplicate":
        rows[1]["_persisted_rank"] = 1
    elif corruption == "missing":
        rows[1].pop("_persisted_rank")
    elif corruption == "gap":
        rows[1]["_persisted_rank"] = 3
    else:
        rows[0]["_persisted_rank"] = True

    assert _rank_rows(
        rows,
        grouped=False,
        use_persisted_rank=True,
    ) == []


@pytest.mark.parametrize("target_group", ["A", "B"])
@pytest.mark.parametrize("corruption", ["duplicate", "missing", "gap"])
def test_traditional_group_persisted_stage_rejects_incomplete_group_ranks(
    target_group, corruption
):
    rows = [
        {
            "entry_id": 1,
            "group_id": "A",
            "_persisted_rank": 1,
            "points": 0,
            "delta_total": -10,
        },
        {
            "entry_id": 2,
            "group_id": "A",
            "_persisted_rank": 2,
            "points": 9,
            "delta_total": 10,
        },
        {
            "entry_id": 3,
            "group_id": "B",
            "_persisted_rank": 1,
            "points": 0,
            "delta_total": -10,
        },
        {
            "entry_id": 4,
            "group_id": "B",
            "_persisted_rank": 2,
            "points": 9,
            "delta_total": 10,
        },
    ]
    target_rows = [row for row in rows if row["group_id"] == target_group]
    if corruption == "duplicate":
        target_rows[1]["_persisted_rank"] = 1
    elif corruption == "missing":
        target_rows[1].pop("_persisted_rank")
    else:
        target_rows[1]["_persisted_rank"] = 3

    assert _rank_rows(
        rows,
        grouped=True,
        use_persisted_rank=True,
    ) == []


def test_invalid_persisted_rank_cannot_mark_completed_stage_or_advancement(
    tmp_path,
):
    store = Store(str(tmp_path / "invalid-persisted-rank-stage-state.db"))
    users_and_bots = [
        _fixture_bot(store, tmp_path, 31_000 + index, game_id="holdem")
        for index in range(2)
    ]
    contest = store.create_contest(
        "invalid persisted rank stage state",
        users_and_bots[0][0]["id"],
        status="published",
        game_id="holdem",
        stages_json=json.dumps(
            [
                {
                    "key": "rr",
                    "type": "round_robin",
                    "scoring": "poker_3_1_0",
                    "advance_count": 1,
                }
            ]
        ),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in users_and_bots
    ]
    manager = ContestManager(store, _NoDispatchOrchestrator())
    asyncio.run(
        manager._begin_stage(
            contest["id"],
            0,
            schedule_immediately=True,
            dispatch_pending=False,
        )
    )
    [pairing] = store.list_contest_pairings(contest["id"], stage_idx=0)
    match_id = "invalid-persisted-rank-completed-match"
    store.create_match(
        match_id,
        users_and_bots[0][1]["id"],
        users_and_bots[1][1]["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
    )
    store.bind_contest_pairing_match(
        contest["id"],
        pairing["id"],
        match_id,
        require_execution_admission=False,
    )
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        result={"rounds_played": 70, "deltas": [10, -10]},
    )
    store.complete_contest_pairing_for_match(contest["id"], match_id)
    persisted = []
    for index, (entry, (_user, bot)) in enumerate(
        zip(entries, users_and_bots, strict=True)
    ):
        points = 3.0 if index == 0 else 0.0
        persisted.append(
            {
                "stage_idx": 0,
                "stage_key": "rr",
                "entry_id": entry["id"],
                "bot_id": bot["id"],
                "points": points,
                "wins": 1 if index == 0 else 0,
                "draws": 0,
                "losses": 0 if index == 0 else 1,
                "delta_total": 10 if index == 0 else -10,
                "group_id": "",
                # Duplicate persisted coordinates are the sole corruption.
                "rank_in_group": 1,
                "tiebreaks": _official_tiebreaks(
                    points, int(entry["seed"])
                ),
            }
        )

    snapshot = store.contest_projection_snapshot(contest["id"])
    assert snapshot is not None
    assert store.contest_stage_manifest_is_valid(
        contest["id"], 0, require_manifest=True
    )
    assert (
        snapshot["contest"]["pairing_topology_revision"]
        == snapshot["contest"]["sealed_pairing_topology_revision"]
    )
    current_topology_sealed = (
        manager_module.current_stage_topology_seal_is_valid(
            snapshot["contest"], snapshot["pairings"]
        )
    )
    assert current_topology_sealed is True
    stale_contest = {
        **snapshot["contest"],
        "sealed_pairing_topology_revision": (
            snapshot["contest"]["pairing_topology_revision"] - 1
        ),
    }
    assert manager_module.current_stage_topology_seal_is_valid(
        stale_contest, snapshot["pairings"]
    ) is False
    summary = build_stage_summaries(
        manager,
        snapshot["contest"],
        snapshot["entries"],
        snapshot["pairings"],
        stage_results=persisted,
        # Isolate the duplicate persisted-rank contract only after proving the
        # same current topology authority consumed by the production API.
        current_topology_sealed=current_topology_sealed,
    )[0]

    assert summary["source"] == "persisted"
    assert summary["rows"] == []
    assert summary["total_pairings"] == 1
    assert summary["completed_pairings"] == 0
    assert summary["status"] != "completed"
    assert summary["advancement_final"] is False

    valid_persisted = copy.deepcopy(persisted)
    valid_persisted[1]["rank_in_group"] = 2
    valid_summary = build_stage_summaries(
        manager,
        snapshot["contest"],
        snapshot["entries"],
        snapshot["pairings"],
        stage_results=valid_persisted,
        current_topology_sealed=current_topology_sealed,
    )[0]
    assert len(valid_summary["rows"]) == 2
    assert valid_summary["completed_pairings"] == 1
    assert valid_summary["status"] == "completed"
    assert valid_summary["advancement_final"] is True
    store.close()


@pytest.mark.parametrize(
    "overall_ranks",
    [
        [1, 1],
        [1, 3],
        [True, 2],
        [None, 2],
    ],
)
def test_cross_group_stage_rows_reject_incomplete_overall_rank(overall_ranks):
    rows = [
        {
            "entry_id": index,
            "group_id": group_id,
            "overall_rank": overall_rank,
            "rank_in_group": 1,
            "_persisted_rank": 1,
        }
        for index, (group_id, overall_rank) in enumerate(
            zip(("A", "B"), overall_ranks, strict=True),
            start=1,
        )
    ]
    assert _rank_rows(
        rows,
        grouped=True,
        cross_group_overall=True,
        use_persisted_rank=True,
    ) == []


@pytest.mark.parametrize(
    ("game_id", "time_control_id", "expected_seconds"),
    [
        ("gomoku", "gomoku_per_side_total_300s_v1", 600),
        ("gomoku", "gomoku_per_side_total_900s_v1", 1800),
        ("gomoku", None, 1800),
        ("pencil", "pencil_per_decision_1s_v1", 84),
    ],
)
def test_estimate_uses_frozen_time_control_and_legacy_default(
    game_id, time_control_id, expected_seconds
):
    contest = {
        "id": 91,
        "template_id": "",
        "game_id": game_id,
        "status": "draft",
        "current_stage_idx": 0,
        "time_control_id": time_control_id,
        "stages_json": json.dumps(
            [{"key": "only", "type": "round_robin", "scoring": "ccgc_2_1_0"}]
        ),
    }
    entries = [
        {
            "id": index,
            "user_id": 100 + index,
            "bot_id": 200 + index,
            "seed": index,
            "group_id": "",
            "eliminated": 0,
        }
        for index in (1, 2)
    ]
    estimate = ContestManager(None, _SingleSlotEstimator()).estimate(
        contest["id"], contest=contest, entries=entries, pairings=[]
    )
    assert estimate["estimated_matches"] == 1
    assert estimate["eta_seconds"] == expected_seconds
    assert estimate["stages"][0]["eta_seconds"] == expected_seconds


def test_template_multi_time_controls_are_allow_listed_and_defaulted(monkeypatch):
    multi = {
        "id": "pencil_multi_fixture",
        "game_id": "pencil",
        "time_control_ids": [
            "pencil_per_side_total_900s_v1",
            "pencil_per_decision_1s_v1",
        ],
        "default_time_control_id": "pencil_per_decision_1s_v1",
    }
    monkeypatch.setattr(
        manager_module,
        "get_template",
        lambda template_id: multi if template_id == multi["id"] else {},
    )
    resolve = ContestManager._resolve_contest_time_control_id
    assert resolve("pencil", None, template_id=multi["id"]) == (
        "pencil_per_decision_1s_v1"
    )
    assert resolve(
        "pencil", None, template_id=multi["id"], persisted=True
    ) == "pencil_per_side_total_900s_v1"
    assert resolve(
        "pencil",
        "pencil_per_side_total_900s_v1",
        template_id=multi["id"],
    ) == "pencil_per_side_total_900s_v1"
    with pytest.raises(ValueError, match="不支持|未注册|异游戏"):
        resolve(
            "pencil",
            "gomoku_per_side_total_300s_v1",
            template_id=multi["id"],
        )

    single = {
        **multi,
        "time_control_ids": ["pencil_per_side_total_900s_v1"],
        "default_time_control_id": "pencil_per_side_total_900s_v1",
    }
    monkeypatch.setattr(manager_module, "get_template", lambda _template_id: single)
    with pytest.raises(ValueError, match="固定使用"):
        resolve(
            "pencil",
            "pencil_per_decision_1s_v1",
            template_id=single["id"],
        )

    malformed = {
        **multi,
        "time_control_ids": [
            "pencil_per_side_total_900s_v1",
            "gomoku_per_side_total_300s_v1",
        ],
    }
    monkeypatch.setattr(manager_module, "get_template", lambda _template_id: malformed)
    with pytest.raises(ValueError, match="未注册|异游戏"):
        resolve("pencil", None, template_id=malformed["id"])


def test_persisted_null_time_control_is_legacy_default_not_template_default():
    resolve = ContestManager._resolve_contest_time_control_id
    with pytest.raises(ValueError, match="固定使用"):
        resolve(
            "gomoku",
            None,
            template_id="gomoku_seeded_group_drr_final",
            persisted=True,
        )

    stages = copy.deepcopy(get_template("gomoku_seeded_group_drr_final")["stages"])
    damaged = {
        "id": 88,
        "template_id": "gomoku_seeded_group_drr_final",
        "game_id": "gomoku",
        "time_control_id": None,
        "stages_json": json.dumps(stages, ensure_ascii=False),
        "format_snapshot_json": "{}",
    }
    public = _contest_for_api(damaged)
    assert public["time_control_id"] is None
    assert public["time_control"] is None

    legacy = {
        **damaged,
        "template_id": "board_rr",
        "stages_json": json.dumps(get_template("board_rr")["stages"]),
    }
    legacy_public = _contest_for_api(legacy)
    assert legacy_public["time_control_id"] == "gomoku_per_side_total_900s_v1"
    assert legacy_public["time_control"]["seconds"] == 900


@pytest.mark.parametrize(
    ("participant_count", "expected_total"),
    [(22, 156), (23, 166), (24, 176), (25, 190), (26, 200)],
)
def test_gomoku_dynamic_band_estimate_is_exact_without_persisting_draw(
    participant_count, expected_total
):
    contest = {
        "id": 92,
        "template_id": "gomoku_seeded_group_drr_final",
        "game_id": "gomoku",
        "status": "draft",
        "current_stage_idx": 0,
        "time_control_id": "gomoku_per_side_total_300s_v1",
        "stages_json": json.dumps(
            get_template("gomoku_seeded_group_drr_final")["stages"]
        ),
    }
    entries = [
        {
            "id": index,
            "user_id": 100 + index,
            "bot_id": 200 + index,
            "seed": index,
            "group_id": "",
            "eliminated": 0,
        }
        for index in range(1, participant_count + 1)
    ]
    estimate = ContestManager(None, _SingleSlotEstimator()).estimate(
        contest["id"], contest=contest, entries=entries, pairings=[]
    )
    assert estimate["estimated_matches"] == expected_total
    assert [stage["participant_count"] for stage in estimate["stages"]] == [
        participant_count,
        8 if participant_count <= 24 else 10,
    ]


def test_new_contest_columns_are_fresh_and_reopen_idempotent(tmp_path):
    path = tmp_path / "schema.db"
    store = Store(str(path))
    store.close()
    reopened = Store(str(path))
    with sqlite3.connect(path) as connection:
        contest_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(contests)")
        }
        official_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(contest_official_results)")
        }
    assert {"time_control_id", "format_snapshot_json"} <= contest_columns
    assert {"group_id", "rank_in_group"} <= official_columns
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()


@pytest.mark.parametrize(
    ("group_id", "rank_in_group"),
    [
        ("A", 1.5),
        ("A", True),
        ("A", None),
        ("", 1),
        (True, 1),
        (" A", 1),
    ],
)
def test_official_group_coordinates_are_exact_and_replace_is_atomic(
    tmp_path, group_id, rank_in_group
):
    store = Store(str(tmp_path / "official-group-exact.db"))
    owner, bot = _fixture_bot(store, tmp_path, 900, game_id="pencil")
    contest = store.create_contest("official", owner["id"], game_id="pencil")
    entry = store.add_contest_entry(contest["id"], owner["id"], bot["id"])
    valid = {
        "entry_id": entry["id"],
        "rank": 1,
        "bot_id": bot["id"],
        "user_id": owner["id"],
    }
    store.replace_official_results(contest["id"], [valid])

    with pytest.raises(ValueError, match="分组|组内"):
        store.replace_official_results(
            contest["id"],
            [{**valid, "group_id": group_id, "rank_in_group": rank_in_group}],
        )
    rows = store.list_official_results(contest["id"])
    assert [(row["group_id"], row["rank_in_group"]) for row in rows] == [("", None)]
    with pytest.raises(ValueError, match="组内"):
        store.upsert_official_result(
            contest["id"], 2, 2, group_id="B", rank_in_group=False
        )
    store.close()


def test_official_group_coordinates_fail_closed_when_legacy_row_is_malformed(tmp_path):
    store = Store(str(tmp_path / "official-group-malformed.db"))
    owner, bot = _fixture_bot(store, tmp_path, 901, game_id="pencil")
    contest = store.create_contest("malformed", owner["id"], game_id="pencil")
    entry = store.add_contest_entry(contest["id"], owner["id"], bot["id"])
    store.replace_official_results(
        contest["id"],
        [{
            "entry_id": entry["id"],
            "rank": 1,
            "bot_id": bot["id"],
            "user_id": owner["id"],
        }],
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_official_results SET group_id='A',rank_in_group=1.5 "
            "WHERE contest_id=?",
            (contest["id"],),
        )
    with pytest.raises(ValueError, match="组内名次"):
        store.list_official_results(contest["id"])
    store.close()


def test_random_group_official_results_bind_terminal_and_source_stages(tmp_path):
    app = create_app(db_path=str(tmp_path / "random-official-stage.db"))
    store = app.state.store
    users_and_bots = [
        _fixture_bot(store, tmp_path, 9_100 + index, game_id="pencil")
        for index in range(4)
    ]
    manager = ContestManager(store, _PersistingContestOrchestrator(store))
    contest = manager.create(
        users_and_bots[0][0]["id"],
        "random official stage binding",
        game_id="pencil",
        template_id="pencil_group_drr",
        stage_format_settings={"groups": {"group_count": 2}},
    )
    for user, bot in users_and_bots:
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")
    asyncio.run(manager.publish(contest["id"]))
    entries = store.list_contest_entries(contest["id"])
    snapshot = json.loads(store.get_contest(contest["id"])["format_snapshot_json"])
    draw_positions = {
        entry_id: position
        for position, entry_id in enumerate(snapshot["draw_order"], start=1)
    }
    ranks_by_group: dict[str, int] = {}
    rows = []
    for rank, entry in enumerate(entries, start=1):
        group_id = entry["group_id"]
        rank_in_group = ranks_by_group.get(group_id, 0) + 1
        ranks_by_group[group_id] = rank_in_group
        rows.append(
            {
                "entry_id": entry["id"],
                "rank": rank,
                "points": 0,
                "bot_id": entry["bot_id"],
                "user_id": entry["user_id"],
                "group_id": group_id,
                "rank_in_group": rank_in_group,
                "stage_idx": 0,
                "tiebreaks_json": json.dumps(
                    _official_tiebreaks(
                        0,
                        entry["seed"],
                        group_rank=rank_in_group,
                        draw_order=draw_positions[entry["id"]],
                    )
                ),
            }
        )

    # Reach the terminal state through the real one-stage decision transaction;
    # the assertions below concern official-result stage coordinates, not an
    # unsupported published+ready hybrid.
    asyncio.run(manager.start(contest["id"]))
    for pairing in store.list_contest_pairings(contest["id"], stage_idx=0):
        match_id = pairing.get("match_id")
        assert match_id
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            result={"deltas": [1, -1]},
        )
        assert store.complete_contest_pairing_for_match(
            contest["id"], match_id
        )
    finished = asyncio.run(manager.maybe_finish(contest["id"]))
    assert finished and finished["status"] == "finished"
    assert finished["official_results_ready"] == 1

    with pytest.raises(ValueError, match="阶段坐标"):
        store.replace_official_results(
            contest["id"],
            [
                {
                    **row,
                    "stage_idx": 1,
                    "tiebreaks_json": json.dumps(
                        _official_tiebreaks(0, entry["seed"])
                    ),
                }
                for row, entry in zip(rows, entries)
            ],
        )

    store.replace_official_results(contest["id"], rows)
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contest_official_results SET stage_idx=1 WHERE contest_id=?",
            (contest["id"],),
        )
    with pytest.raises(ValueError, match="阶段坐标"):
        store.list_official_results(contest["id"])
    with TestClient(app) as client:
        json_response = client.get(
            f"/api/contests/{contest['id']}/official-results"
        )
        csv_response = client.get(
            f"/api/contests/{contest['id']}/official-results?format=csv"
        )
    assert json_response.status_code == csv_response.status_code == 409
    assert json_response.json()["detail"] == csv_response.json()["detail"]
    store.close()


def test_legacy_contest_format_columns_migrate_without_reinterpreting_active_rows(
    tmp_path,
):
    path = tmp_path / "legacy-format-columns.db"
    store = Store(str(path))
    owner, bot = _fixture_bot(store, tmp_path, 902, game_id="gomoku")
    contest = store.create_contest(
        "active legacy",
        owner["id"],
        game_id="gomoku",
        status="running",
        time_control_id="gomoku_per_side_total_900s_v1",
    )
    entry = store.add_contest_entry(contest["id"], owner["id"], bot["id"])
    store.replace_official_results(
        contest["id"],
        [{
            "entry_id": entry["id"],
            "rank": 1,
            "bot_id": bot["id"],
            "user_id": owner["id"],
        }],
    )
    store.close()
    with sqlite3.connect(path) as connection:
        # Reconstruct the pre-column lifecycle epoch, not a hybrid schema whose
        # current triggers still reference the columns this fixture removes.
        for trigger_name in (
            store_db_module._CONTEST_LIFECYCLE_REVISION_TRIGGER_NAMES
        ):
            connection.execute(f"DROP TRIGGER IF EXISTS {trigger_name}")
        connection.execute("ALTER TABLE contests DROP COLUMN time_control_id")
        connection.execute("ALTER TABLE contests DROP COLUMN format_snapshot_json")
        connection.execute(
            "ALTER TABLE contest_official_results DROP COLUMN group_id"
        )
        connection.execute(
            "ALTER TABLE contest_official_results DROP COLUMN rank_in_group"
        )

    migrated = Store(str(path))
    active = migrated.get_contest(contest["id"])
    assert active["status"] == "running"
    assert active["time_control_id"] is None
    assert active["format_snapshot_json"] == "{}"
    official = migrated.list_official_results(contest["id"])
    assert [(row["group_id"], row["rank_in_group"]) for row in official] == [
        ("", None)
    ]
    migrated.close()

    reopened = Store(str(path))
    assert reopened.get_contest(contest["id"])["time_control_id"] is None
    reopened.close()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
