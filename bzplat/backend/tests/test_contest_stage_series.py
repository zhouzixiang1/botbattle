"""Stage-keyed Holdem fairness series and aggregate scoring contracts."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.presentation import build_stage_summaries
from bzplat.backend.contests.ranking import compute_official_ranking
from bzplat.backend.contests.stages import (
    effective_swiss_rounds,
    generate_stage_pairings,
)
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


SCORING = "poker_3_1_0"


class _NoDispatch:
    max_concurrent = 2

    async def challenge(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("future published contest must not dispatch")

    async def challenge_duplicate(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("future published contest must not dispatch")


def _people(
    store: Store, tmp_path: Path, count: int, *, prefix: str
) -> tuple[list[dict], list[dict]]:
    users: list[dict] = []
    bots: list[dict] = []
    for index in range(count):
        user = store.create_user(
            f"{prefix}-u{index}", f"{prefix}-u{index}@example.com", "hash"
        )
        binary = tmp_path / f"{prefix}-{index}.elf"
        binary.write_bytes(b"series-test")
        bot = store.create_bot(
            user["id"],
            f"{prefix}-b{index}",
            binary_path=str(binary),
            format="elf",
            game_id="holdem",
        )
        users.append(user)
        bots.append(bot)
    return users, bots


def _complete_pairing(
    store: Store,
    contest: dict,
    pairing: dict,
    match_id: str,
    *,
    winner: int | None,
    deltas: list[int],
    technical_loss: int = 0,
) -> None:
    store.create_match(
        match_id,
        pairing["bot_a_id"],
        pairing["bot_b_id"],
        owner_id=contest["organizer_id"],
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
        winner=winner,
        technical_loss=technical_loss,
        result={
            "rounds_played": 70,
            "deltas": deltas,
            "normalized_delta": deltas[0] / 100,
        },
    )
    store.complete_contest_pairing_for_match(contest["id"], match_id)


def _series_fixture(tmp_path: Path, *, prefix: str = "score"):
    store = Store(str(tmp_path / f"{prefix}.db"))
    organizer = store.create_user(
        f"{prefix}-org", f"{prefix}-org@example.com", "hash", role="organizer"
    )
    users, bots = _people(store, tmp_path, 2, prefix=prefix)
    stage = {
        "key": "prelim",
        "type": "swiss",
        "rounds": 1,
        "games_per_pair": 2,
        "series_scoring": "aggregate_match_points_v1",
        "swiss_extra_rounds": 0,
        "effective_rounds": 1,
        "scoring": SCORING,
    }
    contest = store.create_contest(
        f"{prefix} contest",
        organizer["id"],
        status="running",
        game_id="holdem",
        template_id="holdem_prelim_swiss",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    rows = [
        {
            "entry_a_id": entries[0]["id"],
            "entry_b_id": entries[1]["id"],
            "bot_a_id": bots[0]["id"],
            "bot_b_id": bots[1]["id"],
            "round_num": 1,
            "stage_key": "prelim",
            "pairing_seed": 1001,
            "series_index": 1,
            "series_size": 2,
        },
        {
            "entry_a_id": entries[1]["id"],
            "entry_b_id": entries[0]["id"],
            "bot_a_id": bots[1]["id"],
            "bot_b_id": bots[0]["id"],
            "round_num": 1,
            "stage_key": "prelim",
            "pairing_seed": 1002,
            "series_index": 2,
            "series_size": 2,
        },
    ]
    pairings = store.create_contest_stage_pairings(
        contest["id"], 0, rows, expected_current_stage_idx=0
    )
    return store, ContestManager(store, _NoDispatch()), contest, entries, bots, pairings, stage


def test_template_wire_defaults_overrides_and_strict_rejection(tmp_path, monkeypatch):
    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = create_app(db_path=str(tmp_path / "api.db"))
    store = app.state.store
    owner = store.create_user(
        "stage-series-org",
        "stage-series-org@example.com",
        hash_password("pw123456"),
        role="organizer",
    )
    store.update_user(owner["id"], email_verified=1)
    _, token = app.state.auth.authenticate("stage-series-org", "pw123456")
    headers = {"Authorization": f"Bearer {token}"}
    client = TestClient(app)

    templates = {
        row["id"]: row for row in client.get("/api/contests/templates").json()["templates"]
    }
    assert templates["holdem_prelim_swiss"]["stage_series_configs"] == [
        {
            "stage_key": "prelim",
            "label": "预赛瑞士轮",
            "games_per_pair": {"default": 2, "allowed_values": [1, 2, 4]},
            "swiss_extra_rounds": {"default": 2, "min": 0, "max": 4},
        }
    ]
    assert "额外进行 2 轮" in templates["holdem_prelim_swiss"]["summary"]
    assert [
        config["games_per_pair"]["default"]
        for config in templates["holdem_final_ranked"]["stage_series_configs"]
    ] == [2, 4]

    prelim = client.post(
        "/api/contests",
        headers=headers,
        json={"title": "default prelim", "template_id": "holdem_prelim_swiss"},
    )
    assert prelim.status_code == 200, prelim.text
    prelim_contest = prelim.json()["contest"]
    assert prelim_contest["stage_series_settings"] == {
        "prelim": {"games_per_pair": 2, "swiss_extra_rounds": 2}
    }
    prelim_stage = json.loads(prelim_contest["stages_json"])[0]
    assert prelim_stage["series_scoring"] == "aggregate_match_points_v1"

    final = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "configured final",
            "template_id": "holdem_final_ranked",
            "stage_series_settings": {
                "qualify": {"games_per_pair": 4},
                "final8": {"games_per_pair": 8},
            },
        },
    )
    assert final.status_code == 200, final.text
    assert final.json()["contest"]["stage_series_settings"] == {
        "qualify": {"games_per_pair": 4},
        "final8": {"games_per_pair": 8},
    }

    both = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "ambiguous",
            "template_id": "holdem_prelim_swiss",
            "games_per_pair": 2,
            "stage_series_settings": {
                "prelim": {"games_per_pair": 2, "swiss_extra_rounds": 2}
            },
        },
    )
    assert both.status_code == 400
    assert "不能同时提交" in both.text
    unknown = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "unknown stage",
            "template_id": "holdem_final_ranked",
            "stage_series_settings": {"other": {"games_per_pair": 2}},
        },
    )
    assert unknown.status_code == 400
    missing = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "missing final stage",
            "template_id": "holdem_final_ranked",
            "stage_series_settings": {"qualify": {"games_per_pair": 2}},
        },
    )
    assert missing.status_code == 400
    assert "缺少阶段" in missing.text
    with pytest.raises(ValueError, match="缺少阶段"):
        ContestManager(store, _NoDispatch()).create(
            owner["id"],
            "manager missing final stage",
            template_id="holdem_final_ranked",
            stage_series_settings={"qualify": {"games_per_pair": 2}},
        )
    for bad in (True, 3, 10):
        rejected = client.post(
            "/api/contests",
            headers=headers,
            json={
                "title": f"bad {bad}",
                "template_id": "holdem_prelim_swiss",
                "stage_series_settings": {
                    "prelim": {"games_per_pair": bad, "swiss_extra_rounds": 2}
                },
            },
        )
        assert rejected.status_code == (422 if bad is True else 400)


def test_generation_round_interleaving_coverage_cap_and_stage_estimate(tmp_path):
    stage = {
        "type": "swiss",
        "games_per_pair": 2,
        "series_scoring": "aggregate_match_points_v1",
    }
    rows = generate_stage_pairings(stage, [11, 22, 33, 44], swiss_round=3)
    assert [row.series_index for row in rows] == [1, 1, 2, 2]
    assert {row.round_num for row in rows} == {3}
    by_pair: dict[frozenset[int], list] = defaultdict(list)
    for row in rows:
        by_pair[frozenset((row.bot_a_id, row.bot_b_id))].append(row)
    assert len(by_pair) == 2
    for pair_rows in by_pair.values():
        assert [row.color_first for row in pair_rows] in ([0, 1], [1, 0])

    final8 = generate_stage_pairings(
        {
            "type": "double_round_robin",
            "games_per_pair": 4,
            "series_scoring": "aggregate_match_points_v1",
        },
        [1, 2, 3, 4],
    )
    assert len(final8) == 24
    assert all(row.series_size == 4 for row in final8)

    assert effective_swiss_rounds(
        {"rounds": 0, "swiss_extra_rounds": 4}, 3
    ) == 3
    assert effective_swiss_rounds(
        {"rounds": 0, "swiss_extra_rounds": 4}, 4
    ) == 3
    assert effective_swiss_rounds(
        {"rounds": 10}, 4
    ) == 10, "legacy explicit rounds must not be capped"
    assert effective_swiss_rounds(
        {"effective_rounds": 7, "swiss_extra_rounds": 2}, 4
    ) == 7, "frozen historical value is authoritative"

    store = Store(str(tmp_path / "estimate.db"))
    organizer = store.create_user("estimate-org", "estimate-org@example.com", "hash")
    users, bots = _people(store, tmp_path, 4, prefix="estimate")
    manager = ContestManager(store, _NoDispatch())
    contest = manager.create(
        organizer["id"], "estimate", template_id="holdem_prelim_swiss"
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    estimate = manager.estimate(contest["id"])
    assert estimate["estimated_matches"] == 12
    assert estimate["stages"] == [
        {
            "stage_key": "prelim",
            "participant_count": 4,
            "conceptual_pairings": 6,
            "effective_rounds": 3,
            "games_per_pair": 2,
            "estimated_matches": 12,
            "estimated_execution_legs": 12,
            "eta_seconds": 840,
        }
    ]
    store.close()


def test_aggregate_series_waits_then_scores_once_and_counts_technical_loss(
    tmp_path, monkeypatch
):
    store, manager, contest, entries, _bots, pairings, _stage = _series_fixture(tmp_path)
    _complete_pairing(
        store,
        contest,
        pairings[0],
        "aggregate-one",
        winner=0,
        deltas=[100, -100],
        technical_loss=1,
    )
    partial = {row["entry_id"]: row for row in manager.standings(contest["id"])}
    assert {
        key: (row["points"], row["wins"], row["draws"], row["losses"], row["delta_total"])
        for key, row in partial.items()
    } == {
        entries[0]["id"]: (0, 0, 0, 0, 0),
        entries[1]["id"]: (0, 0, 0, 0, 0),
    }
    assert manager._stage_done(contest["id"], 0) is False

    _complete_pairing(
        store,
        contest,
        pairings[1],
        "aggregate-two",
        winner=1,
        deltas=[-50, 50],
    )
    settled = {row["entry_id"]: row for row in manager.standings(contest["id"])}
    assert (
        settled[entries[0]["id"]]["points"],
        settled[entries[0]["id"]]["wins"],
        settled[entries[0]["id"]]["delta_total"],
    ) == (3, 1, 150)
    assert (
        settled[entries[1]["id"]]["points"],
        settled[entries[1]["id"]]["losses"],
        settled[entries[1]["id"]]["delta_total"],
    ) == (0, 1, -150)
    ranked = {row["entry_id"]: row for row in manager._rank_stage_rows(contest["id"], 0)}
    assert ranked[entries[1]["id"]]["tiebreaks"]["technical_losses"] == 1
    assert manager._stage_done(contest["id"], 0) is True

    # A corrupt/incomplete coordinate graph cannot pass automatic, force, or
    # recovery finalization merely because every remaining Match completed.
    with store._tx() as conn:
        conn.execute("DELETE FROM contest_pairings WHERE id=?", (pairings[1]["id"],))
    assert manager._stage_done(contest["id"], 0) is False
    assert manager._has_unfinished_pairings(contest["id"]) is True
    live_rows = manager.standings(contest["id"], stage_idx=0)

    def reject_presentation_match_lookup(*_args, **_kwargs):
        raise AssertionError("stage presentation must reuse contest_bracket match state")

    with monkeypatch.context() as scoped:
        scoped.setattr(manager, "standings", lambda *_args, **_kwargs: live_rows)
        scoped.setattr(store, "get_match", reject_presentation_match_lookup)
        summaries = build_stage_summaries(
            manager,
            store.get_contest(contest["id"]),
            store.list_contest_entries(contest["id"]),
            store.contest_bracket(contest["id"]),
        )
    assert summaries[0]["status"] == "running"
    assert summaries[0]["advancement_final"] is False
    with pytest.raises(ValueError, match="未完成对阵"):
        asyncio.run(manager.finish(contest["id"]))
    store.close()


def test_legacy_open_publish_freezes_defaults_and_next_round_waits_for_all_k(
    tmp_path, monkeypatch
):
    store = Store(str(tmp_path / "legacy-open.db"))
    organizer = store.create_user("legacy-open-org", "legacy-open@example.com", "hash")
    users, bots = _people(store, tmp_path, 4, prefix="legacy-open")
    legacy_stage = {
        "key": "prelim",
        "type": "swiss",
        "rounds": 0,
        "scoring": SCORING,
        "rest_after_minutes": 0,
        "allow_bot_swap_in_rest": True,
    }
    contest = store.create_contest(
        "legacy open prelim",
        organizer["id"],
        status="open",
        starts_at="2099-12-31T23:59:59",
        game_id="holdem",
        template_id="holdem_prelim_swiss",
        stages_json=json.dumps([legacy_stage]),
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    manager = ContestManager(store, _NoDispatch())
    preview = manager.estimate(contest["id"])
    assert preview["estimated_matches"] == 12
    assert preview["stages"][0]["games_per_pair"] == 2
    assert preview["stages"][0]["effective_rounds"] == 3
    assert "games_per_pair" not in json.loads(
        store.get_contest(contest["id"])["stages_json"]
    )[0], "estimate must not silently persist current defaults"
    published = asyncio.run(manager.publish(contest["id"]))
    frozen = json.loads(published["stages_json"])[0]
    assert (
        frozen["games_per_pair"],
        frozen["swiss_extra_rounds"],
        frozen["effective_rounds"],
        frozen["series_scoring"],
    ) == (2, 2, 3, "aggregate_match_points_v1")
    first_round = store.list_contest_pairings(contest["id"], stage_idx=0)
    assert len(first_round) == 4
    assert [row["series_index"] for row in first_round] == [1, 1, 2, 2]
    assert len({row["pairing_seed"] for row in first_round}) == 4

    store.update_contest(contest["id"], status="running")
    for index, pairing in enumerate(first_round[:3], start=1):
        _complete_pairing(
            store,
            contest,
            pairing,
            f"swiss-partial-{index}",
            winner=0,
            deltas=[10, -10],
        )
    assert asyncio.run(manager._maybe_next_swiss_round(contest["id"], 0, frozen)) is False
    assert len(store.list_contest_pairings(contest["id"], stage_idx=0)) == 4

    _complete_pairing(
        store,
        contest,
        first_round[3],
        "swiss-last",
        winner=1,
        deltas=[-10, 10],
    )

    async def no_dispatch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(manager, "_dispatch_pending_locked", no_dispatch)
    assert asyncio.run(manager._maybe_next_swiss_round(contest["id"], 0, frozen)) is True
    all_rows = store.list_contest_pairings(contest["id"], stage_idx=0)
    second_round = [row for row in all_rows if row["round_num"] == 2]
    assert [row["series_index"] for row in second_round] == [1, 1, 2, 2]
    assert len({row["pairing_seed"] for row in all_rows}) == 8
    store.close()


def test_draft_open_patch_is_owner_scoped_cas_and_rejects_any_schedule(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("BZ_SKIP_CAPTCHA", "1")
    app = create_app(db_path=str(tmp_path / "patch.db"))
    store = app.state.store
    owner = store.create_user(
        "patch-org", "patch-org@example.com", hash_password("pw123456"), role="organizer"
    )
    other = store.create_user(
        "patch-other", "patch-other@example.com", hash_password("pw123456"), role="organizer"
    )
    for user in (owner, other):
        store.update_user(user["id"], email_verified=1)
    _, owner_token = app.state.auth.authenticate("patch-org", "pw123456")
    _, other_token = app.state.auth.authenticate("patch-other", "pw123456")
    client = TestClient(app)
    created = client.post(
        "/api/contests",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={"title": "patchable", "template_id": "holdem_prelim_swiss"},
    ).json()["contest"]
    contest_id = created["id"]
    patch_body = {
        "stage_series_settings": {
            "prelim": {"games_per_pair": 4, "swiss_extra_rounds": 4}
        }
    }
    forbidden = client.patch(
        f"/api/contests/{contest_id}",
        headers={"Authorization": f"Bearer {other_token}"},
        json=patch_body,
    )
    assert forbidden.status_code == 403
    patched = client.patch(
        f"/api/contests/{contest_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
        json=patch_body,
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["contest"]["stage_series_settings"]["prelim"] == {
        "games_per_pair": 4,
        "swiss_extra_rounds": 4,
    }
    store.update_contest(contest_id, status="open")
    open_patch = client.patch(
        f"/api/contests/{contest_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
        json={
            "stage_series_settings": {
                "prelim": {"games_per_pair": 2, "swiss_extra_rounds": 1}
            }
        },
    )
    assert open_patch.status_code == 200

    users, bots = _people(store, tmp_path, 2, prefix="patch-pairing")
    entries = [
        store.add_contest_entry(contest_id, user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    store.add_pairing(
        contest_id,
        bots[0]["id"],
        bots[1]["id"],
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        stage_key="prelim",
    )
    scheduled = client.patch(
        f"/api/contests/{contest_id}",
        headers={"Authorization": f"Bearer {owner_token}"},
        json=patch_body,
    )
    assert scheduled.status_code == 400
    assert "已生成赛程" in scheduled.text

    current = store.get_contest(contest_id)
    with pytest.raises(ValueError, match="并发修改"):
        store.compare_and_swap_unstarted_contest_stages(
            contest_id,
            expected_status="open",
            expected_stages_json='[{"stale":true}]',
            stages_json=current["stages_json"],
        )


def test_custom_topology_with_builtin_id_publishes_legacy_and_rejects_series_patch(
    tmp_path,
):
    store = Store(str(tmp_path / "custom-topology.db"))
    organizer = store.create_user("custom-org", "custom-org@example.com", "hash")
    users, bots = _people(store, tmp_path, 2, prefix="custom-topology")
    manager = ContestManager(store, _NoDispatch())
    contest = manager.create(
        organizer["id"],
        "custom topology",
        template_id="holdem_prelim_swiss",
        stages=[
            {
                "key": "custom_swiss",
                "type": "swiss",
                "rounds": 1,
                "scoring": SCORING,
            }
        ],
    )
    with pytest.raises(ValueError, match="自定义阶段拓扑"):
        asyncio.run(
            manager.revise_stage_series_settings(
                contest["id"],
                {"prelim": {"games_per_pair": 2, "swiss_extra_rounds": 2}},
            )
        )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])

    published = asyncio.run(manager.publish(contest["id"]))
    stage = json.loads(published["stages_json"])[0]
    assert stage["key"] == "custom_swiss"
    assert not {
        "games_per_pair",
        "swiss_extra_rounds",
        "series_scoring",
        "effective_rounds",
    }.intersection(stage)
    assert len(store.list_contest_pairings(contest["id"], stage_idx=0)) == 1
    store.close()


def test_live_series_summary_is_in_memory_and_partial_result_stays_off_main_table(
    tmp_path,
):
    app = create_app(db_path=str(tmp_path / "live-series.db"))
    store = app.state.store
    organizer = store.create_user(
        "live-series-org", "live-series@example.com", "hash", role="organizer"
    )
    users, bots = _people(store, tmp_path, 2, prefix="live-series")
    stage = {
        "key": "prelim",
        "type": "swiss",
        "rounds": 1,
        "games_per_pair": 2,
        "series_scoring": "aggregate_match_points_v1",
        "swiss_extra_rounds": 0,
        "effective_rounds": 1,
        "scoring": SCORING,
    }
    contest = store.create_contest(
        "live aggregate",
        organizer["id"],
        status="running",
        game_id="holdem",
        template_id="holdem_prelim_swiss",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    pairings = store.create_contest_stage_pairings(
        contest["id"],
        0,
        [
            {
                "entry_a_id": entries[0]["id"], "entry_b_id": entries[1]["id"],
                "bot_a_id": bots[0]["id"], "bot_b_id": bots[1]["id"],
                "round_num": 1, "stage_key": "prelim", "pairing_seed": 4001,
                "series_index": 1, "series_size": 2,
            },
            {
                "entry_a_id": entries[1]["id"], "entry_b_id": entries[0]["id"],
                "bot_a_id": bots[1]["id"], "bot_b_id": bots[0]["id"],
                "round_num": 1, "stage_key": "prelim", "pairing_seed": 4002,
                "series_index": 2, "series_size": 2,
            },
        ],
        expected_current_stage_idx=0,
    )
    _complete_pairing(
        store,
        contest,
        pairings[0],
        "live-series-one",
        winner=0,
        deltas=[200, -200],
    )
    traced: list[str] = []
    store._conn.set_trace_callback(traced.append)
    try:
        response = TestClient(app).get(f"/api/contests/{contest['id']}/live")
    finally:
        store._conn.set_trace_callback(None)
    assert response.status_code == 200, response.text
    selects = [sql for sql in traced if sql.lstrip().upper().startswith("SELECT")]
    assert len(selects) == 3, selects
    payload = response.json()
    assert payload["series"] == {
        "games_per_pair": 2,
        "duplicate": False,
        "scoring_mode": "aggregate_match_points_v1",
        "conceptual_completed": 0,
        "conceptual_total": 1,
    }
    assert payload["standings"][0]["points"] == 0
    summary = payload["recent"][0]["series_summary"]
    assert summary == {
        "series_size": 2,
        "completed_matches": 1,
        "game_points_a": 1.0,
        "game_points_b": 0.0,
        "normalized_delta_a": 2.0,
        "settled": False,
        "standings_points_a": None,
        "standings_points_b": None,
    }
    store.close()


def test_final_stage_ranking_excludes_zero_point_nonparticipants(tmp_path):
    store = Store(str(tmp_path / "final-participants.db"))
    organizer = store.create_user("final-org", "final-org@example.com", "hash")
    users, bots = _people(store, tmp_path, 3, prefix="final-participants")
    stages = [
        {"key": "qualify", "type": "round_robin", "scoring": SCORING},
        {
            "key": "final8",
            "type": "double_round_robin",
            "games_per_pair": 2,
            "series_scoring": "aggregate_match_points_v1",
            "ranking_mode": "replace_top",
            "ranking_scope": 2,
            "scoring": SCORING,
        },
    ]
    contest = store.create_contest(
        "final participants",
        organizer["id"],
        status="running",
        current_stage_idx=1,
        game_id="holdem",
        template_id="holdem_final_ranked",
        stages_json=json.dumps(stages),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    # The nonparticipant deliberately has the strongest fallback seed; it must
    # never enter the Top-stage ranking merely because every row has zero points.
    store.update_entry(contest["id"], users[2]["id"], seed=1)
    store.update_entry(contest["id"], users[0]["id"], seed=2)
    store.update_entry(contest["id"], users[1]["id"], seed=3)
    pairings = store.create_contest_stage_pairings(
        contest["id"],
        1,
        [
            {
                "entry_a_id": entries[0]["id"], "entry_b_id": entries[1]["id"],
                "bot_a_id": bots[0]["id"], "bot_b_id": bots[1]["id"],
                "round_num": 1, "stage_key": "final8", "pairing_seed": 8001,
                "series_index": 1, "series_size": 2,
            },
            {
                "entry_a_id": entries[1]["id"], "entry_b_id": entries[0]["id"],
                "bot_a_id": bots[1]["id"], "bot_b_id": bots[0]["id"],
                "round_num": 2, "stage_key": "final8", "pairing_seed": 8002,
                "series_index": 2, "series_size": 2,
            },
        ],
        expected_current_stage_idx=1,
    )
    for index, pairing in enumerate(pairings, start=1):
        _complete_pairing(
            store,
            contest,
            pairing,
            f"final-draw-{index}",
            winner=None,
            deltas=[0, 0],
        )
    manager = ContestManager(store, _NoDispatch())
    final_standings = manager.standings(contest["id"], stage_idx=1)
    assert [row["points"] for row in final_standings] == [1.0, 1.0]
    assert [row["draws"] for row in final_standings] == [1, 1]
    ranked = manager._rank_stage_rows(contest["id"], 1)
    assert {row["entry_id"] for row in ranked} == {
        entries[0]["id"],
        entries[1]["id"],
    }
    assert entries[2]["id"] not in {row["entry_id"] for row in ranked}
    store.close()


@pytest.mark.parametrize("winners", [(None, None), (0, 0)])
def test_aggregate_h2h_draw_is_half_for_two_draws_or_one_win_each(winners):
    stage = {
        "key": "final8",
        "type": "double_round_robin",
        "games_per_pair": 2,
        "series_scoring": "aggregate_match_points_v1",
        "scoring": SCORING,
    }
    pairings = [
        {
            "entry_a_id": 11,
            "entry_b_id": 22,
            "match_id": "h2h-1",
            "round_num": 1,
            "series_index": 1,
            "series_size": 2,
        },
        {
            "entry_a_id": 22,
            "entry_b_id": 11,
            "match_id": "h2h-2",
            "round_num": 2,
            "series_index": 2,
            "series_size": 2,
        },
    ]
    matches = {
        "h2h-1": {"status": "completed", "winner": winners[0], "result": {}},
        "h2h-2": {"status": "completed", "winner": winners[1], "result": {}},
    }
    standings = [
        {"entry_id": 11, "points": 1.0, "delta_total": 0, "seed": 1},
        {"entry_id": 22, "points": 1.0, "delta_total": 0, "seed": 2},
    ]

    ranked = compute_official_ranking(
        standings, pairings, matches, stage=stage
    )
    assert [row["tiebreaks"]["head_to_head"] for row in ranked] == [0.5, 0.5]

    # Historical non-aggregate stages intentionally retain wins/records, where
    # a draw contributes zero to the numerator.
    legacy = compute_official_ranking(
        standings,
        [pairings[0]],
        {"h2h-1": {"status": "completed", "winner": None, "result": {}}},
    )
    assert [row["tiebreaks"]["head_to_head"] for row in legacy] == [0.0, 0.0]
