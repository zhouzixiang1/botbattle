"""Stage-keyed Holdem fairness series and aggregate scoring contracts."""
from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.presentation import (
    build_stage_counts,
    build_stage_summaries,
)
from bzplat.backend.contests.ranking import compute_official_ranking
from bzplat.backend.contests.series import (
    series_rows_settled,
    summarize_conceptual_series,
)
from bzplat.backend.contests.stages import (
    effective_swiss_rounds,
    generate_stage_pairings,
)
from bzplat.backend.crypto import hash_password
from bzplat.backend.games import registry as game_registry
from bzplat.backend.main import create_app
from bzplat.backend.matches.public_outcome import build_public_outcome
from bzplat.backend.store import Store


SCORING = "poker_3_1_0"


class _NoDispatch:
    max_concurrent = 2

    async def challenge(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("future published contest must not dispatch")

    async def challenge_duplicate(self, *_args, **_kwargs):  # pragma: no cover
        raise AssertionError("future published contest must not dispatch")


class _PreparedMigrationDispatch:
    """Create bounded pending Match rows so the manual-start path can bind."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.calls: list[bool] = []

    async def _challenge(self, a: int, b: int, *, duplicate: bool, **kwargs) -> str:
        self.calls.append(duplicate)
        match_id = f"legacy-series-migration-{len(self.calls)}"
        self.store.create_match(
            match_id,
            a,
            b,
            owner_id=kwargs["owner_user_id"],
            contest_id=kwargs["contest_id"],
            match_type=kwargs["match_type"],
            game_id=kwargs["game_id"],
            match_config={"duplicate": duplicate},
        )
        return match_id

    async def challenge(self, a: int, b: int, **kwargs) -> str:
        return await self._challenge(a, b, duplicate=False, **kwargs)

    async def challenge_duplicate(self, a: int, b: int, **kwargs) -> str:
        return await self._challenge(a, b, duplicate=True, **kwargs)


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
        match_config={"duplicate": False},
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


def test_swiss_cumulative_normalized_delta_overflow_blocks_next_round(tmp_path):
    app = create_app(db_path=str(tmp_path / "swiss-delta-overflow.db"))
    store = app.state.store
    organizer = store.create_user(
        "overflow-org", "overflow-org@example.com", "hash", role="organizer"
    )
    users, bots = _people(store, tmp_path, 2, prefix="overflow")
    stage = {
        "key": "swiss",
        "type": "swiss",
        "rounds": 3,
        "swiss_extra_rounds": 0,
        "effective_rounds": 3,
        "scoring": SCORING,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
    }
    contest = store.create_contest(
        "Overflow Swiss",
        organizer["id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    for round_num in (1, 2):
        pairing = store.add_pairing(
            contest["id"],
            bots[0]["id"],
            bots[1]["id"],
            entry_a_id=entries[0]["id"],
            entry_b_id=entries[1]["id"],
            stage_idx=0,
            stage_key="swiss",
            round_num=round_num,
            series_index=1,
            series_size=1,
        )
        _complete_pairing(
            store,
            contest,
            pairing,
            f"overflow-{round_num}",
            winner=0,
            deltas=[10**310, -(10**310)],
        )

    manager = app.state.contest_manager
    assert manager.standings(contest["id"], stage_idx=0) == []
    before = store.list_contest_pairings(contest["id"], stage_idx=0)
    assert asyncio.run(
        manager._maybe_next_swiss_round(contest["id"], 0, stage)
    ) is False
    assert store.list_contest_pairings(contest["id"], stage_idx=0) == before

    client = TestClient(app)
    detail = client.get(f"/api/contests/{contest['id']}")
    live = client.get(f"/api/contests/{contest['id']}/live")
    assert detail.status_code == 200, detail.text
    assert live.status_code == 200, live.text
    assert detail.json()["stage_standings"][0]["status"] != "completed"
    assert live.json()["progress"]["completed"] < live.json()["progress"]["total"]


@pytest.mark.parametrize("stage_idx", [0, 1])
def test_aggregate_missing_whole_group_keeps_frozen_totals_and_zero_entry(
    tmp_path, stage_idx
):
    app = create_app(db_path=str(tmp_path / "aggregate-missing-group.db"))
    store = app.state.store
    organizer = store.create_user(
        "aggregate-gap-org",
        "aggregate-gap-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 3, prefix="aggregate-gap")
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": SCORING,
        "games_per_pair": 1,
        "series_scoring": "aggregate_match_points_v1",
    }
    stages = (
        [
            {"key": "prelim", "type": "round_robin", "scoring": SCORING},
            stage,
        ]
        if stage_idx == 1
        else [stage]
    )
    contest = store.create_contest(
        "Aggregate missing group",
        organizer["id"],
        status="running",
        current_stage_idx=stage_idx,
        game_id="holdem",
        stages_json=json.dumps(stages),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    pairing = store.add_pairing(
        contest["id"],
        bots[0]["id"],
        bots[1]["id"],
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        stage_idx=stage_idx,
        stage_key="rr",
        series_index=1,
        series_size=1,
    )
    _complete_pairing(
        store,
        contest,
        pairing,
        "aggregate-gap-match",
        winner=0,
        deltas=[100, -100],
    )

    client = TestClient(app)
    detail = client.get(f"/api/contests/{contest['id']}").json()
    live = client.get(f"/api/contests/{contest['id']}/live").json()
    summary = next(
        row for row in detail["stage_standings"] if row["stage_idx"] == stage_idx
    )
    assert summary["status"] != "completed"
    assert summary["total_pairings"] == 3
    assert len(summary["rows"]) == 3
    assert next(
        row for row in summary["rows"] if row["entry_id"] == entries[2]["id"]
    )["points"] == 0
    assert live["counts"]["encounter_groups"] == {"completed": 1, "total": 3}
    assert live["counts"]["match_jobs"] == {"completed": 1, "total": 3}
    assert live["counts"]["scoring_games"] == {
        "completed": 1,
        "planned": 3,
        "terminal_unplayed": 0,
    }
    assert live["series"]["conceptual_completed"] == 1
    assert live["series"]["conceptual_total"] == 3
    assert app.state.contest_manager._stage_done(contest["id"], stage_idx) is False


@pytest.mark.parametrize("entry_count", [0, 1])
def test_explicit_aggregate_empty_small_cohort_is_settled(tmp_path, entry_count):
    store = Store(str(tmp_path / f"aggregate-small-{entry_count}.db"))
    organizer = store.create_user(
        f"aggregate-small-org-{entry_count}",
        f"aggregate-small-org-{entry_count}@example.com",
        "hash",
    )
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": SCORING,
        "games_per_pair": 1,
        "series_scoring": "aggregate_match_points_v1",
    }
    contest = store.create_contest(
        f"Aggregate small {entry_count}",
        organizer["id"],
        status="running",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    if entry_count:
        users, bots = _people(
            store, tmp_path, entry_count, prefix=f"aggregate-small-{entry_count}"
        )
        store.add_contest_entry(contest["id"], users[0]["id"], bots[0]["id"])

    manager = ContestManager(store, _NoDispatch())
    standings = manager.standings(contest["id"], stage_idx=0)
    assert len(standings) == entry_count
    assert manager._stage_done(contest["id"], 0) is True
    store.close()


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
        for config in templates["holdem_swiss_top8_ranked"]["stage_series_configs"]
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
    assert prelim_stage["series_scoring"] == "independent_scoring_game_points_v1"

    final = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "configured final",
            "template_id": "holdem_swiss_top8_ranked",
            "stage_series_settings": {
                "swiss": {"games_per_pair": 4, "swiss_extra_rounds": 2},
                "final8": {"games_per_pair": 8},
            },
        },
    )
    assert final.status_code == 200, final.text
    assert final.json()["contest"]["stage_series_settings"] == {
        "swiss": {"games_per_pair": 4, "swiss_extra_rounds": 2},
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
            "template_id": "holdem_swiss_top8_ranked",
            "stage_series_settings": {"other": {"games_per_pair": 2}},
        },
    )
    assert unknown.status_code == 400
    missing = client.post(
        "/api/contests",
        headers=headers,
        json={
            "title": "missing final stage",
            "template_id": "holdem_swiss_top8_ranked",
            "stage_series_settings": {
                "swiss": {"games_per_pair": 2, "swiss_extra_rounds": 2}
            },
        },
    )
    assert missing.status_code == 400
    assert "缺少阶段" in missing.text
    with pytest.raises(ValueError, match="缺少阶段"):
        ContestManager(store, _NoDispatch()).create(
            owner["id"],
            "manager missing final stage",
            template_id="holdem_swiss_top8_ranked",
            stage_series_settings={
                "swiss": {"games_per_pair": 2, "swiss_extra_rounds": 2}
            },
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
            "unbounded_tiebreak": False,
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


@pytest.mark.parametrize(
    ("status", "winner", "technical_loss", "reason", "result"),
    [
        (
            "completed",
            0,
            0,
            "",
            {"rounds_played": 70, "deltas": [-100, 100]},
        ),
        (
            "completed",
            0,
            "",
            "technical_loss",
            {"rounds_played": 20, "deltas": [0, 0]},
        ),
        (
            "completed",
            0,
            0,
            "technical_loss",
            {"rounds_played": 70, "deltas": [100, -100]},
        ),
        (
            "completed",
            0,
            1,
            "technical_loss",
            {"rounds_played": 71, "deltas": [0, 0]},
        ),
        ("completed", 0, 0, "", {}),
        ("aborted", None, 0, "platform_error", {}),
    ],
)
def test_malformed_aggregate_match_is_null_unsettled_and_cannot_advance(
    tmp_path, status, winner, technical_loss, reason, result
):
    store, manager, contest, entries, _bots, pairings, stage = _series_fixture(
        tmp_path, prefix="aggregate-malformed"
    )
    _complete_pairing(
        store,
        contest,
        pairings[0],
        "aggregate-malformed-one",
        winner=0,
        deltas=[100, -100],
    )
    _complete_pairing(
        store,
        contest,
        pairings[1],
        "aggregate-malformed-two",
        winner=1,
        deltas=[-100, 100],
    )
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE matches_holdem SET status=?,winner=?,technical_loss=?,"
            "reason=?,result=? WHERE id='aggregate-malformed-one'",
            (
                status,
                winner,
                technical_loss,
                reason,
                json.dumps(result),
            ),
        )

    malformed = store.get_match("aggregate-malformed-one")
    assert build_public_outcome(malformed, game_registry.get("holdem")) is None
    rows = manager.standings(contest["id"], stage_idx=0)
    assert {row["entry_id"] for row in rows} == {
        entries[0]["id"],
        entries[1]["id"],
    }
    assert all(float(row["points"]) == 0 for row in rows)
    assert all(int(row["counts"]["scoring_games"]) == 0 for row in rows)
    assert not series_rows_settled(
        stage,
        pairings,
        store.get_match,
        game_spec=game_registry.get("holdem"),
    )
    assert manager._stage_done(contest["id"], 0) is False
    ranked = manager._rank_stage_rows(contest["id"], 0)
    assert all(row["tiebreaks"]["buchholz"] == 0 for row in ranked)
    assert all(row["tiebreaks"]["technical_losses"] == 0 for row in ranked)
    store.close()


@pytest.mark.parametrize("duplicate", [False, True])
def test_premarker_independent_result_parser_blocks_damage_but_legal_history_advances(
    tmp_path, duplicate
):
    suffix = "duplicate" if duplicate else "single"
    store = Store(str(tmp_path / f"premarker-result-{suffix}.db"))
    organizer = store.create_user(
        f"premarker-{suffix}-org",
        f"premarker-{suffix}-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 2, prefix=f"premarker-{suffix}")
    stage = {
        "key": "rr",
        "type": "round_robin",
        "games_per_pair": 1,
        "scoring": SCORING,
        **({"duplicate": True} if duplicate else {}),
    }
    contest = store.create_contest(
        f"Premarker {suffix}",
        organizer["id"],
        status="running",
        game_id="holdem",
        template_id="holdem_dup_rr" if duplicate else "holdem_rr",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    pairing = store.create_contest_stage_pairings(
        contest["id"],
        0,
        [
            {
                "entry_a_id": entries[0]["id"],
                "entry_b_id": entries[1]["id"],
                "bot_a_id": bots[0]["id"],
                "bot_b_id": bots[1]["id"],
                "round_num": 1,
                "stage_key": "rr",
                "pairing_seed": 7001,
                "series_index": 1,
                "series_size": 1,
            }
        ],
        expected_current_stage_idx=0,
    )[0]
    match_id = f"premarker-{suffix}-match"
    store.create_match(
        match_id,
        bots[0]["id"],
        bots[1]["id"],
        owner_id=organizer["id"],
        contest_id=contest["id"],
        match_type="contest",
        game_id="holdem",
        match_config={"duplicate": duplicate},
    )
    store.bind_contest_pairing_match(
        contest["id"], pairing["id"], match_id,
        require_execution_admission=False,
    )
    malformed = (
        {
            "rounds_played": 70,
            "deltas": [100, -100],
            "normalized_delta": 1,
            "legs": [
                {
                    "winner": 0,
                    "rounds_played": 70,
                    "deltas": [100, -100],
                    "normalized_delta": 1,
                }
            ],
        }
        if duplicate
        else {
            "rounds_played": 70,
            "deltas": [-100, 100],
            "normalized_delta": -1,
        }
    )
    store.update_match(
        match_id,
        status="completed",
        winner=None if duplicate else 0,
        result=malformed,
    )
    store.complete_contest_pairing_for_match(contest["id"], match_id)
    manager = ContestManager(store, _NoDispatch())
    current_pairings = store.list_contest_pairings(contest["id"], stage_idx=0)

    assert build_public_outcome(
        store.get_match(match_id), game_registry.get("holdem")
    ) is None
    standings = manager.standings(contest["id"], stage_idx=0)
    assert all(row["points"] == 0 for row in standings)
    assert all(row["counts"]["scoring_games"] == 0 for row in standings)
    assert all(
        row["counts"]
        == {
            "encounter_groups": 0,
            "unique_opponents": 0,
            "match_jobs": 0,
            "scoring_games": 0,
        }
        for row in standings
    )
    assert not series_rows_settled(
        stage,
        current_pairings,
        store.get_match,
        game_spec=game_registry.get("holdem"),
    )
    assert manager._stage_done(contest["id"], 0) is False
    assert manager._has_unfinished_pairings(contest["id"]) is True
    assert asyncio.run(manager.maybe_finish(contest["id"])) is None
    assert store.get_contest(contest["id"])["status"] == "running"

    legal = (
        {
            "rounds_played": 140,
            "deltas": [0, 0],
            "normalized_delta": 0,
            "legs": [
                {
                    "winner": 0,
                    "rounds_played": 70,
                    "deltas": [100, -100],
                    "normalized_delta": 1,
                },
                {
                    "winner": 1,
                    "rounds_played": 70,
                    "deltas": [-100, 100],
                    "normalized_delta": -1,
                },
            ],
        }
        if duplicate
        else {
            "rounds_played": 70,
            "deltas": [100, -100],
            "normalized_delta": 1,
        }
    )
    store.update_match(match_id, result=legal)
    assert series_rows_settled(
        stage,
        current_pairings,
        store.get_match,
        game_spec=game_registry.get("holdem"),
    )
    assert manager._stage_done(contest["id"], 0) is True
    finished = asyncio.run(manager.maybe_finish(contest["id"]))
    assert finished is not None and finished["status"] == "finished"
    store.close()


@pytest.mark.parametrize("stage_type", ["round_robin", "swiss"])
def test_ordinary_stage_malformed_completed_match_cannot_finalize_or_add_swiss_round(
    tmp_path, stage_type
):
    suffix = "swiss" if stage_type == "swiss" else "rr"
    store = Store(str(tmp_path / f"ordinary-malformed-{suffix}.db"))
    organizer = store.create_user(
        f"ordinary-{suffix}-org",
        f"ordinary-{suffix}-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 2, prefix=f"ordinary-{suffix}")
    stage = {
        "key": suffix,
        "type": stage_type,
        "scoring": SCORING,
        **({"rounds": 2} if stage_type == "swiss" else {}),
    }
    contest = store.create_contest(
        f"Ordinary malformed {suffix}",
        organizer["id"],
        status="running",
        game_id="holdem",
        template_id="holdem_swiss_ko" if stage_type == "swiss" else "holdem_rr",
        stages_json=json.dumps([stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    pairing = store.create_contest_stage_pairings(
        contest["id"],
        0,
        [
            {
                "entry_a_id": entries[0]["id"],
                "entry_b_id": entries[1]["id"],
                "bot_a_id": bots[0]["id"],
                "bot_b_id": bots[1]["id"],
                "round_num": 1,
                "stage_key": suffix,
            }
        ],
        expected_current_stage_idx=0,
    )[0]
    _complete_pairing(
        store,
        contest,
        pairing,
        f"ordinary-malformed-{suffix}-match",
        winner=0,
        deltas=[-100, 100],
    )
    manager = ContestManager(store, _NoDispatch())
    projected_pairing = {
        **store.list_contest_pairings(contest["id"], stage_idx=0)[0],
        "match_status": "completed",
        "match_winner": 0,
        "_match_reason": None,
        "_match_technical_loss": 0,
        "_match_result_json": {
            "rounds_played": 70,
            "deltas": [-100, 100],
            "normalized_delta": -1,
        },
        "_match_config_json": {"duplicate": False},
    }

    standings = manager.standings(contest["id"], stage_idx=0)
    assert all(row["points"] == 0 for row in standings)
    assert all(row["counts"]["scoring_games"] == 0 for row in standings)
    assert all(
        row["counts"]
        == {
            "encounter_groups": 0,
            "unique_opponents": 0,
            "match_jobs": 0,
            "scoring_games": 0,
        }
        for row in standings
    )
    counts = build_stage_counts(stage, [projected_pairing], game_id="holdem")
    assert counts["match_jobs"] == {"completed": 0, "total": 1}
    assert counts["scoring_games"]["completed"] == 0
    assert manager._stage_done(contest["id"], 0) is False
    assert manager._has_unfinished_pairings(contest["id"]) is True
    assert asyncio.run(manager.maybe_finish(contest["id"])) is None
    assert store.list_stage_results(contest["id"], stage_idx=0) == []
    assert store.get_contest(contest["id"])["status"] == "running"
    if stage_type == "swiss":
        assert asyncio.run(
            manager._maybe_next_swiss_round(contest["id"], 0, stage)
        ) is False
        assert len(store.list_contest_pairings(contest["id"], stage_idx=0)) == 1
    store.close()


@pytest.mark.parametrize(
    ("result", "winner", "technical_loss", "reason", "expected_winner"),
    [
        (
            {
                "rounds_played": 140,
                "deltas": [200, -200],
                "legs": [
                    {"winner": 0, "rounds_played": 70, "deltas": [100, -100]},
                    {"winner": 0, "rounds_played": 70, "deltas": [100, -100]},
                ],
            },
            None,
            0,
            None,
            None,
        ),
        (
            {
                "rounds_played": 140,
                "deltas": [0, 0],
                "legs": [
                    {"winner": 0, "rounds_played": 70, "deltas": [100, -100]},
                    {"winner": 1, "rounds_played": 70, "deltas": [-100, 100]},
                ],
            },
            None,
            0,
            None,
            None,
        ),
        (
            {"rounds_played": 5, "deltas": [0, 0], "technical_game_index": 2},
            0,
            1,
            "timeout",
            1,
        ),
    ],
)
def test_legacy_duplicate_aggregate_keeps_top_level_winner_semantics(
    result, winner, technical_loss, reason, expected_winner
):
    stage = {
        "key": "legacy-duplicate",
        "type": "round_robin",
        "scoring": SCORING,
        "duplicate": True,
        "games_per_pair": 1,
        "series_scoring": "aggregate_match_points_v1",
    }
    pairing = {
        "entry_a_id": 1,
        "entry_b_id": 2,
        "match_id": "legacy-duplicate-match",
        "series_index": 1,
        "series_size": 1,
    }
    match = {
        "status": "completed",
        "winner": winner,
        "technical_loss": technical_loss,
        "reason": reason,
        "result": result,
        "match_config": {"duplicate": True},
    }
    summary = summarize_conceptual_series(
        stage,
        [pairing],
        {"legacy-duplicate-match": match}.get,
        game_spec=game_registry.get("holdem"),
    )
    assert summary["settled"] is True
    assert summary["winner_entry"] == expected_winner
    assert summary["standings_points"] == (
        {1: 1, 2: 1} if expected_winner is None else {1: 3, 2: 0}
    )


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
    ) == (2, 2, 3, "independent_scoring_game_points_v1")
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


def _add_unstarted_progress_surface(
    store, contest, entries, users, bots, progress_kind, *, prefix
):
    if progress_kind == "pairing":
        store.add_pairing(
            contest["id"],
            bots[0]["id"],
            bots[1]["id"],
            entry_a_id=entries[0]["id"],
            entry_b_id=entries[1]["id"],
            stage_key="prelim",
        )
    elif progress_kind == "execution_job":
        with store._tx() as conn:
            conn.execute(
                "INSERT INTO execution_jobs("
                "public_id,source,status,priority,game_id,match_type,bot_a_id,bot_b_id,"
                "rated,rating_reason,sandbox_units,host_cpu_millis,host_memory_mb,"
                "profile_version,contest_id,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    f"{prefix}-execution-job",
                    "manual",
                    "queued",
                    50,
                    "holdem",
                    "challenge",
                    bots[0]["id"],
                    bots[1]["id"],
                    0,
                    "test",
                    2,
                    2000,
                    1024,
                    1,
                    contest["id"],
                    "2026-01-01T00:00:00",
                ),
            )
    elif progress_kind == "match":
        store.create_match(
            f"{prefix}-match",
            bots[0]["id"],
            bots[1]["id"],
            contest_id=contest["id"],
            match_type="contest",
            game_id="holdem",
        )
    elif progress_kind == "stage_result":
        store.upsert_stage_result(
            contest["id"], 0, entries[0]["id"], bot_id=bots[0]["id"]
        )
    else:
        store.upsert_official_result(
            contest["id"],
            entries[0]["id"],
            1,
            bot_id=bots[0]["id"],
            user_id=users[0]["id"],
        )


@pytest.mark.parametrize(
    "progress_kind",
    ["pairing", "execution_job", "match", "stage_result", "official_result"],
)
def test_unstarted_series_settings_cas_rejects_every_persisted_progress_surface(
    tmp_path, progress_kind
):
    store = Store(str(tmp_path / f"series-cas-{progress_kind}.db"))
    organizer = store.create_user(
        f"cas-{progress_kind}-org",
        f"cas-{progress_kind}-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 2, prefix=f"cas-{progress_kind}")
    manager = ContestManager(store, _NoDispatch())
    contest = manager.create(
        organizer["id"],
        f"CAS {progress_kind}",
        template_id="holdem_prelim_swiss",
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    store.update_contest(contest["id"], status="open")
    current = store.get_contest(contest["id"])
    _add_unstarted_progress_surface(
        store,
        contest,
        entries,
        users,
        bots,
        progress_kind,
        prefix=f"series-cas-{progress_kind}",
    )

    replacement = json.dumps(
        [
            {
                **json.loads(current["stages_json"])[0],
                "games_per_pair": 4,
                "swiss_extra_rounds": 4,
            }
        ],
        ensure_ascii=False,
    )
    with pytest.raises(ValueError, match="执行任务、对局或结果"):
        store.compare_and_swap_unstarted_contest_stages(
            contest["id"],
            expected_status="open",
            expected_stages_json=current["stages_json"],
            stages_json=replacement,
        )
    assert store.get_contest(contest["id"])["stages_json"] == current["stages_json"]
    store.close()


@pytest.mark.parametrize(
    "progress_field,progress_value",
    [
        ("current_stage_idx", 1),
        ("official_results_ready", 1),
        ("current_stage_idx", 0.5),
        ("official_results_ready", 0.5),
    ],
)
def test_unstarted_series_settings_cas_rejects_contest_progress_flags(
    tmp_path, progress_field, progress_value
):
    store = Store(str(tmp_path / f"series-cas-{progress_field}.db"))
    organizer = store.create_user(
        f"cas-{progress_field}-org",
        f"cas-{progress_field}-org@example.com",
        "hash",
        role="organizer",
    )
    contest = ContestManager(store, _NoDispatch()).create(
        organizer["id"],
        f"CAS {progress_field}",
        template_id="holdem_prelim_swiss",
    )
    store.update_contest(contest["id"], status="open")
    if isinstance(progress_value, float):
        with store._tx() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                f"UPDATE contests SET {progress_field}=? WHERE id=?",
                (progress_value, contest["id"]),
            )
    else:
        store.update_contest(
            contest["id"], **{progress_field: progress_value}
        )
    current = store.get_contest(contest["id"])
    stages = json.loads(current["stages_json"])
    stages[0]["games_per_pair"] = 4
    replacement = json.dumps(stages, ensure_ascii=False)

    with pytest.raises(ValueError, match="阶段或正式结果进度"):
        store.compare_and_swap_unstarted_contest_stages(
            contest["id"],
            expected_status="open",
            expected_stages_json=current["stages_json"],
            stages_json=replacement,
        )
    assert store.get_contest(contest["id"])["stages_json"] == current["stages_json"]
    store.close()


@pytest.mark.parametrize("entrypoint", ["publish", "start"])
@pytest.mark.parametrize(
    "progress_kind",
    ["pairing", "execution_job", "match", "stage_result", "official_result"],
)
def test_lifecycle_default_migration_reuses_full_zero_progress_cas(
    tmp_path, entrypoint, progress_kind
):
    store = Store(
        str(tmp_path / f"lifecycle-migration-{entrypoint}-{progress_kind}.db")
    )
    organizer = store.create_user(
        f"migrate-{entrypoint}-{progress_kind}-org",
        f"migrate-{entrypoint}-{progress_kind}-org@example.com",
        "hash",
        role="organizer",
    )
    prefix = f"migrate-{entrypoint}-{progress_kind}"
    users, bots = _people(store, tmp_path, 2, prefix=prefix)
    # Exact built-in topology from before stage-series defaults were persisted.
    # Publish/start would migrate this to independent scoring if and only if no
    # durable execution/result surface exists.
    legacy_stage = {
        "key": "prelim",
        "type": "swiss",
        "rounds": 0,
        "scoring": SCORING,
        "allow_bot_swap_in_rest": True,
        "rest_after_minutes": 0,
    }
    contest = store.create_contest(
        f"Lifecycle migration {entrypoint} {progress_kind}",
        organizer["id"],
        status="open",
        game_id="holdem",
        template_id="holdem_prelim_swiss",
        stages_json=json.dumps([legacy_stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    _add_unstarted_progress_surface(
        store,
        contest,
        entries,
        users,
        bots,
        progress_kind,
        prefix=prefix,
    )
    before = store.get_contest(contest["id"])
    manager = ContestManager(store, _NoDispatch())

    with pytest.raises(ValueError, match="执行任务、对局或结果"):
        asyncio.run(getattr(manager, entrypoint)(contest["id"]))
    after = store.get_contest(contest["id"])
    assert after["status"] == "open"
    assert after["stages_json"] == before["stages_json"]
    assert json.loads(after["stages_json"]) == [legacy_stage]
    store.close()


@pytest.mark.parametrize("entrypoint", ["publish", "start"])
@pytest.mark.parametrize(
    ("template_id", "duplicate"),
    [("holdem_rr", False), ("holdem_dup_rr", True)],
)
@pytest.mark.parametrize("has_progress", [False, True])
def test_legacy_games_per_pair_template_migration_uses_full_zero_progress_cas(
    tmp_path, entrypoint, template_id, duplicate, has_progress
):
    suffix = f"{entrypoint}-{template_id}-{'progress' if has_progress else 'clean'}"
    store = Store(str(tmp_path / f"legacy-pair-capability-{suffix}.db"))
    organizer = store.create_user(
        f"legacy-pair-{suffix}-org",
        f"legacy-pair-{suffix}-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 2, prefix=f"legacy-pair-{suffix}")
    dispatch = _PreparedMigrationDispatch(store)
    manager = ContestManager(store, dispatch)
    contest = manager.create(
        organizer["id"],
        f"Legacy pair capability {suffix}",
        template_id=template_id,
        games_per_pair=3,
    )
    legacy_stages = json.loads(contest["stages_json"])
    assert legacy_stages[0].pop("series_scoring") == (
        "independent_scoring_game_points_v1"
    )
    store.update_contest(
        contest["id"],
        status="open",
        stages_json=json.dumps(legacy_stages),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    if has_progress:
        _add_unstarted_progress_surface(
            store,
            contest,
            entries,
            users,
            bots,
            "execution_job",
            prefix=f"legacy-pair-{suffix}",
        )

    before = store.get_contest(contest["id"])
    if has_progress:
        with pytest.raises(ValueError, match="执行任务、对局或结果"):
            asyncio.run(getattr(manager, entrypoint)(contest["id"]))
        after = store.get_contest(contest["id"])
        assert after["status"] == "open"
        assert after["stages_json"] == before["stages_json"]
        assert store.list_contest_pairings(contest["id"]) == []
        assert dispatch.calls == []
    else:
        after = asyncio.run(getattr(manager, entrypoint)(contest["id"]))
        frozen = json.loads(after["stages_json"])[0]
        assert frozen["games_per_pair"] == 3
        assert frozen["series_scoring"] == "independent_scoring_game_points_v1"
        assert frozen.get("duplicate", False) is duplicate
        pairings = store.list_contest_pairings(contest["id"], stage_idx=0)
        assert len(pairings) == 3
        if entrypoint == "publish":
            assert after["status"] == "published"
            assert all(pairing.get("match_id") is None for pairing in pairings)
            assert dispatch.calls == []
        else:
            assert after["status"] == "running"
            assert all(pairing.get("match_id") for pairing in pairings)
            assert dispatch.calls == [duplicate, duplicate, duplicate]
    store.close()


@pytest.mark.parametrize("entrypoint", ["publish", "start"])
@pytest.mark.parametrize(
    ("invalid_field", "invalid_value", "participant_count"),
    [
        ("scoring", "bogus", 2),
        ("allow_large_round_robin", "false", 13),
    ],
)
def test_unstarted_lifecycle_validates_every_stage_before_schedule_write(
    tmp_path, entrypoint, invalid_field, invalid_value, participant_count
):
    store = Store(
        str(tmp_path / f"invalid-{entrypoint}-{invalid_field}.db")
    )
    organizer = store.create_user(
        f"invalid-{entrypoint}-{invalid_field}-org",
        f"invalid-{entrypoint}-{invalid_field}-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(
        store,
        tmp_path,
        participant_count,
        prefix=f"invalid-{entrypoint}-{invalid_field}",
    )
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": SCORING,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
        invalid_field: invalid_value,
    }
    contest = store.create_contest(
        f"Invalid {entrypoint} {invalid_field}",
        organizer["id"],
        status="open",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    before = store.get_contest(contest["id"])
    manager = ContestManager(store, _NoDispatch())

    with pytest.raises(ValueError):
        asyncio.run(getattr(manager, entrypoint)(contest["id"]))

    after = store.get_contest(contest["id"])
    assert after["status"] == "open"
    assert after["stages_json"] == before["stages_json"]
    assert after["registration_closes_at"] == before["registration_closes_at"]
    assert after["starts_at"] == before["starts_at"]
    assert store.list_contest_pairings(contest["id"]) == []
    assert all(
        int(entry.get("seed") or 0) == 0
        for entry in store.list_contest_entries(contest["id"])
    )
    store.close()


def test_published_start_rejects_damaged_stage_before_schedule_or_time_write(
    tmp_path,
):
    store = Store(str(tmp_path / "published-invalid-stage.db"))
    organizer = store.create_user(
        "published-invalid-org",
        "published-invalid-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(store, tmp_path, 2, prefix="published-invalid")
    stage = {
        "key": "rr",
        "type": "round_robin",
        "scoring": SCORING,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
    }
    contest = store.create_contest(
        "Published invalid stage",
        organizer["id"],
        status="open",
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    manager = ContestManager(store, _NoDispatch())
    asyncio.run(manager.publish(contest["id"]))
    damaged = {**stage, "scoring": "bogus"}
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE contests SET stages_json=? WHERE id=?",
            (json.dumps([damaged]), contest["id"]),
        )
    before = store.get_contest(contest["id"])
    pairings_before = store.list_contest_pairings(contest["id"])

    with pytest.raises(ValueError):
        asyncio.run(manager.start(contest["id"]))

    after = store.get_contest(contest["id"])
    pairings_after = store.list_contest_pairings(contest["id"])
    for field in (
        "status",
        "registration_opens_at",
        "registration_closes_at",
        "starts_at",
        "stages_json",
    ):
        assert after[field] == before[field]
    assert [
        (row["id"], row["status"], row.get("scheduled_at"), row.get("match_id"))
        for row in pairings_after
    ] == [
        (row["id"], row["status"], row.get("scheduled_at"), row.get("match_id"))
        for row in pairings_before
    ]
    store.close()


@pytest.mark.parametrize("entrypoint", ["automatic", "resume"])
@pytest.mark.parametrize(
    "invalid_next_stage",
    [
        {
            "key": "broken-group",
            "type": "group_round_robin",
            "scoring": SCORING,
            "group_count": 0,
            "advance_per_group": 1,
        },
        {"key": "missing-type", "type": "", "scoring": SCORING},
    ],
)
def test_future_stage_is_fully_validated_before_snapshot_or_advancement(
    tmp_path, entrypoint, invalid_next_stage
):
    store = Store(str(tmp_path / f"invalid-future-{entrypoint}.db"))
    organizer = store.create_user(
        f"future-{entrypoint}-org",
        f"future-{entrypoint}-org@example.com",
        "hash",
        role="organizer",
    )
    users, bots = _people(
        store, tmp_path, 2, prefix=f"future-{entrypoint}"
    )
    current_stage = {
        "key": "qualify",
        "type": "round_robin",
        "scoring": SCORING,
        "games_per_pair": 1,
        "series_scoring": "independent_scoring_game_points_v1",
        "advance_count": 1,
    }
    contest = store.create_contest(
        f"Invalid future {entrypoint}",
        organizer["id"],
        status="running",
        current_stage_idx=0,
        game_id="holdem",
        stages_json=json.dumps([current_stage, invalid_next_stage]),
    )
    entries = [
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    pairing = store.add_pairing(
        contest["id"],
        bots[0]["id"],
        bots[1]["id"],
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
        stage_idx=0,
        stage_key="qualify",
        series_index=1,
        series_size=1,
    )
    _complete_pairing(
        store,
        contest,
        pairing,
        f"invalid-future-{entrypoint}-match",
        winner=0,
        deltas=[100, -100],
    )
    if entrypoint == "resume":
        store.update_contest(contest["id"], status="rest")
    manager = ContestManager(store, _NoDispatch())

    if entrypoint == "resume":
        with pytest.raises(ValueError):
            asyncio.run(manager.resume(contest["id"]))
    else:
        assert asyncio.run(manager.maybe_finish(contest["id"])) is None

    persisted = store.get_contest(contest["id"])
    assert persisted["current_stage_idx"] == 0
    assert persisted["status"] == (
        "rest" if entrypoint == "resume" else "running"
    )
    assert store.list_stage_results(contest["id"], stage_idx=0) == []
    assert store.list_contest_pairings(contest["id"], stage_idx=1) == []
    assert all(
        int(entry.get("eliminated") or 0) == 0
        for entry in store.list_contest_entries(contest["id"])
    )
    store.close()


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
    }.intersection(stage)
    assert stage["effective_rounds"] == 1
    assert len(store.list_contest_pairings(contest["id"], stage_idx=0)) == 1
    store.close()


def test_builtin_id_with_same_keys_but_changed_stage_field_is_custom_topology(
    tmp_path,
):
    store = Store(str(tmp_path / "custom-same-key-topology.db"))
    organizer = store.create_user(
        "custom-same-key-org", "custom-same-key-org@example.com", "hash"
    )
    users, bots = _people(store, tmp_path, 2, prefix="custom-same-key")
    manager = ContestManager(store, _NoDispatch())
    contest = manager.create(
        organizer["id"],
        "custom same-key topology",
        template_id="holdem_prelim_swiss",
        stages=[
            {
                "key": "prelim",
                "type": "swiss",
                "rounds": 1,
                "scoring": SCORING,
                "allow_bot_swap_in_rest": True,
                "rest_after_minutes": 0,
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
    assert stage["rounds"] == 1
    assert not {
        "games_per_pair",
        "swiss_extra_rounds",
        "series_scoring",
    }.intersection(stage)
    assert stage["effective_rounds"] == 1
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

    # SQLite can retain text in an INTEGER column after a damaged import.  A
    # malformed aggregate coordinate must degrade to an unsettled read model,
    # never raise from int(...) and turn detail/live into HTTP 500.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE contest_pairings SET series_index='x' WHERE id=?",
            (pairings[0]["id"],),
        )
        connection.execute("PRAGMA ignore_check_constraints=OFF")
    detail = TestClient(app).get(f"/api/contests/{contest['id']}")
    malformed_live = TestClient(app).get(f"/api/contests/{contest['id']}/live")
    assert detail.status_code == 200, detail.text
    assert malformed_live.status_code == 200, malformed_live.text
    assert all(row["points"] == 0 for row in detail.json()["standings"])
    assert detail.json()["stage_standings"][0]["status"] != "completed"
    assert all(row["points"] == 0 for row in malformed_live.json()["standings"])
    assert malformed_live.json()["series"]["conceptual_completed"] == 0
    store.close()


def test_explicit_aggregate_coordinates_require_exact_persisted_integers():
    stage = {
        "key": "legacy",
        "type": "round_robin",
        "scoring": SCORING,
        "games_per_pair": 1,
        "series_scoring": "aggregate_match_points_v1",
    }
    base_pairing = {
        "entry_a_id": 1,
        "entry_b_id": 2,
        "match_id": "aggregate-coordinate",
        "series_index": 1,
        "series_size": 1,
    }
    match = {
        "status": "completed",
        "winner": 0,
        "technical_loss": 0,
        "result": {"rounds_played": 70, "deltas": [100, -100]},
        "match_config": {"duplicate": False},
    }
    for field in ("series_index", "series_size"):
        for value in ("1", "x", 1.5, None):
            pairing = {**base_pairing, field: value}
            summary = summarize_conceptual_series(
                stage,
                [pairing],
                {"aggregate-coordinate": match}.get,
                game_spec=game_registry.get("holdem"),
            )
            assert summary["settled"] is False
            assert not series_rows_settled(
                stage,
                [pairing],
                {"aggregate-coordinate": match}.get,
                game_spec=game_registry.get("holdem"),
            )
        missing = dict(base_pairing)
        missing.pop(field)
        assert summarize_conceptual_series(
            stage,
            [missing],
            {"aggregate-coordinate": match}.get,
            game_spec=game_registry.get("holdem"),
        )["settled"] is False


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
    # Normal lifecycle advancement persists this exclusion before materializing
    # the final stage.  Keep the fixture faithful to that frozen cohort rather
    # than relying on surviving pairing rows to infer who qualified.
    store.update_entry(contest["id"], users[2]["id"], eliminated=1)
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
        f"h2h-{index}": {
            "status": "completed",
            "winner": winner,
            "technical_loss": 0,
            "result": {
                "rounds_played": 70,
                "deltas": [0, 0] if winner is None else [100, -100],
            },
        }
        for index, winner in enumerate(winners, start=1)
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
        {
            "h2h-1": {
                "status": "completed",
                "winner": None,
                "result": {"rounds_played": 70, "deltas": [0, 0]},
            }
        },
    )
    assert [row["tiebreaks"]["head_to_head"] for row in legacy] == [0.0, 0.0]
