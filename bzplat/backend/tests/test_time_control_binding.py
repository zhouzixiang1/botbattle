"""Execution-request to Match time-control binding regressions."""
from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.games import registry
from bzplat.backend.matches.orchestrator import (
    _execution_time_control_binding_is_valid,
    MatchOrchestrator,
)
from bzplat.backend.matches.seat_info import match_for_viewer, with_seat_info
from bzplat.backend.store import Store
from bzplat.backend.store.execution import (
    ExecutionInvariantError,
    _time_control_binding_is_valid as _queued_time_control_binding_is_valid,
)
from bzplat.backend.store.public_contract import sanitize_public_match
from bzplat.backend.store.schema import (
    EXECUTION_SOURCE_CONTEST,
    EXECUTION_SOURCE_MANUAL,
    TYPE_CONTEST,
)
from bzplat.backend.tests.execution_helpers import (
    challenge_and_start,
    claim_request,
    enable_execution_queue,
    human_and_start,
    start_claimed_match,
)


SAMPLES = Path(__file__).resolve().parents[3] / "samples"


class _NeverRunMatch:
    def __init__(self) -> None:
        self.calls = 0

    async def run_binaries(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("drifted time control reached runner")

    async def run_duplicate(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("drifted time control reached runner")

    async def run_bot_vs_human(self, *_args, **_kwargs):
        self.calls += 1
        raise AssertionError("drifted time control reached human runner")


class _PublicTimeControlRunner:
    """Emit the same authoritative start payload as MatchRunner."""

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def run_binaries(self, *_args, **kwargs):
        return self._finish(kwargs, applies_to="both_bots")

    async def run_bot_vs_human(self, *_args, **kwargs):
        return self._finish(kwargs, applies_to="bot_only")

    def _finish(self, kwargs: dict, *, applies_to: str):
        game_id = str(kwargs["game_id"])
        time_control_id = str(kwargs["time_control_id"])
        control = registry.get(game_id).resolve_time_control(time_control_id)
        payload = control.public_payload(applies_to=applies_to)
        self.calls.append(
            {
                "game_id": game_id,
                "time_control_id": time_control_id,
                "time_control": payload,
            }
        )
        kwargs["on_event"](
            "match_start",
            {
                "type": "match_start",
                "game_id": game_id,
                "time_control": payload,
            },
        )
        return SimpleNamespace(
            rounds_played=1,
            rounds=[SimpleNamespace(deltas=[0, 0])],
            winner=None,
            events=[],
        )


def test_legacy_missing_ids_compare_as_the_same_game_default() -> None:
    match = {"id": "m1", "match_config": {}}
    job = {
        "current_match_id": "m1",
        "game_id": "gomoku",
        "match_config": "{}",
    }
    spec = registry.get("gomoku")
    assert _execution_time_control_binding_is_valid(job, match, spec)

    match["match_config"] = {
        "time_control_id": "gomoku_per_side_total_300s_v1"
    }
    assert not _execution_time_control_binding_is_valid(job, match, spec)


def test_legacy_contest_null_compares_as_the_same_game_default() -> None:
    spec = registry.get("gomoku")
    match = {
        "id": "m1",
        "game_id": "gomoku",
        "match_type": TYPE_CONTEST,
        "contest_id": 7,
        "match_config": {},
    }
    job = {
        "current_match_id": "m1",
        "game_id": "gomoku",
        "match_type": TYPE_CONTEST,
        "contest_id": 7,
        "match_config": "{}",
    }
    contest = {"id": 7, "game_id": "gomoku", "time_control_id": None}
    assert _execution_time_control_binding_is_valid(
        job, match, spec, contest=contest
    )


@pytest.mark.parametrize(
    ("match_control", "binds"),
    [
        (None, True),
        ("gomoku_per_side_total_900s_v1", True),
        ("gomoku_per_side_total_300s_v1", False),
    ],
)
def test_store_bind_interprets_legacy_contest_null_as_default_only(
    tmp_path: Path,
    match_control: str | None,
    binds: bool,
) -> None:
    store = Store(str(tmp_path / f"legacy-bind-{match_control or 'missing'}.db"))
    user, bot_a, _opponent, bot_b, version_a, version_b, pairing = (
        _active_contest_pairing(
            store,
            name="legacy null control",
            time_control_id=None,
        )
    )
    contest = store.get_contest(pairing["contest_id"])
    assert contest is not None
    config = {
        "duplicate": False,
        "_bot_a_version_id": version_a["id"],
        "_bot_b_version_id": version_b["id"],
    }
    if match_control is not None:
        config["time_control_id"] = match_control
    match = store.create_match(
        f"legacy-bind-{pairing['id']}",
        bot_a["id"],
        bot_b["id"],
        owner_id=user["id"],
        contest_id=contest["id"],
        match_type=TYPE_CONTEST,
        game_id="gomoku",
        match_config=config,
    )
    if binds:
        bound = store.bind_contest_pairing_match(
            contest["id"],
            pairing["id"],
            match["id"],
            require_execution_admission=False,
        )
        assert bound["match_id"] == match["id"]
    else:
        with pytest.raises(ValueError, match="时限与赛事冻结值不一致"):
            store.bind_contest_pairing_match(
                contest["id"],
                pairing["id"],
                match["id"],
                require_execution_admission=False,
            )
        assert store.list_pairings(contest["id"])[0]["match_id"] is None


@pytest.mark.parametrize(
    ("rated", "reason", "expected"),
    [
        (0, "alternate_time_control", True),
        (1, "eligible", False),
        (0, "eligible", False),
        (1, "alternate_time_control", False),
    ],
)
def test_queued_manual_alternate_control_binds_rating_policy(
    rated: int,
    reason: str,
    expected: bool,
) -> None:
    job = {
        "source": EXECUTION_SOURCE_MANUAL,
        "game_id": "gomoku",
        "match_config": json.dumps(
            {"time_control_id": "gomoku_per_side_total_300s_v1"}
        ),
        "rated": rated,
        "rating_reason": reason,
    }
    assert _queued_time_control_binding_is_valid(job) is expected


def _setup(store: Store) -> tuple[dict, dict]:
    user = store.create_user(
        "clock-binding-user",
        "clock-binding@example.com",
        hash_password("password1"),
    )
    bot = store.create_bot(
        user["id"],
        "clock-binding-bot",
        binary_path=str(SAMPLES / "gomokubot_linux_amd64"),
        format="elf",
        game_id="gomoku",
    )
    store.ensure_rating(bot["id"])
    return user, bot


def _setup_eligible_pair(store: Store) -> tuple[dict, dict, dict, dict]:
    users: list[dict] = []
    bots: list[dict] = []
    for index in range(2):
        user = store.create_user(
            f"clock-rating-user-{index}",
            f"clock-rating-{index}@example.com",
            hash_password("password1"),
        )
        bot = store.create_bot(
            user["id"],
            f"clock-rating-bot-{index}",
            binary_path=str(SAMPLES / "gomokubot_linux_amd64"),
            format="elf",
            game_id="gomoku",
        )
        store.select_ranked_bot(user["id"], bot["id"], if_empty=True)
        users.append(user)
        bots.append(bot)
    return users[0], bots[0], users[1], bots[1]


_CONTEST_PAIRING_PUBLISHED_AT = "2026-01-01T00:00:00"


def _active_contest_pairing(
    store: Store,
    *,
    name: str,
    time_control_id: str | None,
) -> tuple[dict, dict, dict, dict, dict, dict, dict]:
    """Build one canonical sealed active Contest before testing later drift."""
    owner, bot_a, opponent, bot_b = _setup_eligible_pair(store)
    version_a = store.add_bot_version(
        bot_a["id"], binary_path=str(bot_a["binary_path"]), version=1
    )
    version_b = store.add_bot_version(
        bot_b["id"], binary_path=str(bot_b["binary_path"]), version=1
    )
    enable_execution_queue(store)
    contest = store.create_contest(
        name,
        owner["id"],
        status="published",
        game_id="gomoku",
        stages_json=json.dumps(
            [{"key": "rr", "type": "round_robin", "scoring": "ccgc_2_1_0"}]
        ),
        template_id="gomoku_drr",
        time_control_id=time_control_id,
    )
    entry_a = store.add_contest_entry(contest["id"], owner["id"], bot_a["id"])
    entry_b = store.add_contest_entry(
        contest["id"], opponent["id"], bot_b["id"]
    )
    pairing = store.create_contest_stage_pairings(
        contest["id"],
        0,
        [
            {
                "entry_a_id": entry_a["id"],
                "entry_b_id": entry_b["id"],
                "bot_a_id": bot_a["id"],
                "bot_b_id": bot_b["id"],
                "bot_a_version_id": version_a["id"],
                "bot_b_version_id": version_b["id"],
                "round_num": 1,
                "stage_key": "rr",
                "series_index": 1,
                "series_size": 1,
                "published_at": _CONTEST_PAIRING_PUBLISHED_AT,
            }
        ],
        expected_current_stage_idx=0,
        expected_status="published",
        activate_running=True,
    )[0]
    assert store.contest_stage_manifest_is_valid(contest["id"], 0)
    return owner, bot_a, opponent, bot_b, version_a, version_b, pairing


def test_claim_cancels_default_rated_job_whose_config_drifted_to_alternate(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "rating-drift.db"))
    owner, bot_a, _opponent, bot_b = _setup_eligible_pair(store)
    runner = _NeverRunMatch()
    orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
    enable_execution_queue(store)
    request_id = asyncio.run(
        orch.challenge(
            bot_a["id"],
            bot_b["id"],
            owner["id"],
            game_id="gomoku",
            time_control_id="gomoku_per_side_total_900s_v1",
        )
    )
    queued = store.executions.get(request_id)
    assert queued is not None
    assert queued["rated"] == 1
    assert queued["rating_reason"] == "eligible"
    config = json.loads(str(queued["match_config"]))
    config["time_control_id"] = "gomoku_per_side_total_300s_v1"
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE execution_jobs SET match_config=? WHERE public_id=?",
            (json.dumps(config), request_id),
        )

    claimed = store.executions.claim_next(
        max_match_slots=1,
        max_sandbox_units=2,
        aging_seconds=60,
        user_active_limit=64,
        contest_share_slots=64,
    )
    assert claimed is None
    terminal = store.executions.get(request_id)
    assert terminal is not None
    assert terminal["status"] == "cancelled"
    assert terminal["terminal_reason"] == "time_control_changed"
    assert terminal["current_match_id"] is None
    with store._tx() as conn:
        assert conn.execute("SELECT COUNT(*) FROM matches_gomoku").fetchone()[0] == 0
    assert runner.calls == 0


def _claimed_manual(
    store: Store,
    *,
    time_control_id: str = "gomoku_per_side_total_900s_v1",
) -> tuple[MatchOrchestrator, dict, str]:
    owner, bot_a, _opponent, bot_b = _setup_eligible_pair(store)
    orch = MatchOrchestrator(store, runner=_NeverRunMatch(), max_concurrent=1)
    enable_execution_queue(store)
    request_id = asyncio.run(
        orch.challenge(
            bot_a["id"],
            bot_b["id"],
            owner["id"],
            game_id="gomoku",
            time_control_id=time_control_id,
        )
    )
    claimed = claim_request(orch, request_id, start=False)
    return orch, claimed, str(claimed["current_match_id"])


def _damage_claimed_time_control_replay(
    store: Store,
    claimed: dict,
    match_id: str,
    *,
    match_status: str,
    replay_shape: str,
    for_retry: bool = False,
) -> list[dict]:
    if match_status == "running":
        store.update_match(match_id, status="running")
    prefix = [{"type": "match_start", "game_id": "gomoku"}]
    match = store.get_match(match_id)
    assert match is not None
    match_config = dict(match["match_config"])
    match_config["time_control_id"] = "gomoku_per_side_total_300s_v1"
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET match_config=? WHERE id=?",
            (json.dumps(match_config), match_id),
        )
        if replay_shape == "missing":
            conn.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
        elif replay_shape == "stale":
            conn.execute(
                "UPDATE match_replays SET events_json=? WHERE match_id=?",
                (
                    json.dumps(
                        [
                            *prefix,
                            {"type": "error", "reason": "orphan_after_restart"},
                        ]
                    ),
                    match_id,
                ),
            )
        elif replay_shape == "malformed":
            conn.execute(
                "UPDATE match_replays SET events_json='{' WHERE match_id=?",
                (match_id,),
            )
        else:
            raise AssertionError(f"unexpected replay shape: {replay_shape}")
        if for_retry:
            terminal_at = datetime.now().isoformat(timespec="seconds")
            conn.execute(
                "UPDATE execution_jobs SET status='interrupted',retryable=1,"
                "cleanup_state='confirmed',terminal_reason='runtime_failure',"
                "terminal_at=? "
                "WHERE id=?",
                (terminal_at, int(claimed["id"])),
            )
            conn.execute(
                "UPDATE execution_job_attempts SET status='interrupted',terminal_at=? "
                "WHERE job_id=? AND match_id=?",
                (terminal_at, int(claimed["id"]), match_id),
            )
    return prefix


@pytest.mark.parametrize(
    "interruption_reason",
    ("orphan_after_service_restart", "orphan_after_runtime_recovery"),
)
@pytest.mark.parametrize("match_status", ("pending", "running"))
@pytest.mark.parametrize("replay_shape", ("missing", "stale", "malformed"))
def test_namespace_time_control_drift_persists_canonical_terminal_replay(
    tmp_path: Path,
    interruption_reason: str,
    match_status: str,
    replay_shape: str,
) -> None:
    store = Store(
        str(
            tmp_path
            / f"recovery-replay-{interruption_reason}-{match_status}-{replay_shape}.db"
        )
    )
    _orch, claimed, match_id = _claimed_manual(store)
    prefix = _damage_claimed_time_control_replay(
        store,
        claimed,
        match_id,
        match_status=match_status,
        replay_shape=replay_shape,
    )

    assert store.executions.recover_after_namespace_cleanup(
        interruption_reason=interruption_reason
    ) == {"requeued": 0, "interrupted": 0, "settling": 0}

    match = store.get_match(match_id)
    job = store.executions.get(str(claimed["public_id"]))
    assert match is not None and job is not None
    assert (match["status"], match["reason"]) == (
        "aborted",
        "time_control_changed",
    )
    assert (job["status"], job["terminal_reason"]) == (
        "cancelled",
        "time_control_changed",
    )
    expected = (
        [*prefix, {"type": "error", "reason": "platform_error"}]
        if replay_shape == "stale"
        else [{"type": "error", "reason": "platform_error"}]
    )
    assert json.loads(store.get_replay(match_id)["events_json"]) == expected


@pytest.mark.parametrize("match_status", ("pending", "running"))
@pytest.mark.parametrize("replay_shape", ("missing", "stale", "malformed"))
def test_retry_time_control_drift_persists_canonical_terminal_replay(
    tmp_path: Path,
    match_status: str,
    replay_shape: str,
) -> None:
    store = Store(str(tmp_path / f"retry-replay-{match_status}-{replay_shape}.db"))
    _orch, claimed, match_id = _claimed_manual(store)
    prefix = _damage_claimed_time_control_replay(
        store,
        claimed,
        match_id,
        match_status=match_status,
        replay_shape=replay_shape,
        for_retry=True,
    )

    with pytest.raises(ValueError, match="^time_control_changed$"):
        store.executions.retry(
            str(claimed["public_id"]),
            owner_user_id=int(claimed["owner_user_id"]),
        )

    match = store.get_match(match_id)
    job = store.executions.get(str(claimed["public_id"]))
    assert match is not None and job is not None
    assert (match["status"], match["reason"]) == (
        "aborted",
        "time_control_changed",
    )
    assert (job["status"], job["terminal_reason"]) == (
        "cancelled",
        "time_control_changed",
    )
    expected = (
        [*prefix, {"type": "error", "reason": "platform_error"}]
        if replay_shape == "stale"
        else [{"type": "error", "reason": "platform_error"}]
    )
    assert json.loads(store.get_replay(match_id)["events_json"]) == expected


@pytest.mark.parametrize("path", ("retry", "recovery"))
@pytest.mark.parametrize("match_status", ("pending", "running"))
def test_time_control_terminal_replay_failure_rolls_back_entire_transition(
    tmp_path: Path,
    path: str,
    match_status: str,
) -> None:
    store = Store(str(tmp_path / f"clock-replay-failure-{path}-{match_status}.db"))
    _orch, claimed, match_id = _claimed_manual(store)
    _damage_claimed_time_control_replay(
        store,
        claimed,
        match_id,
        match_status=match_status,
        replay_shape="missing",
        for_retry=path == "retry",
    )
    with store._tx() as conn:
        conn.execute(
            "CREATE TRIGGER fail_clock_terminal_replay BEFORE INSERT ON match_replays "
            f"WHEN NEW.match_id='{match_id}' BEGIN "
            "SELECT RAISE(ABORT, 'forced clock replay failure'); END"
        )
    before_job = store.executions.get(str(claimed["public_id"]))
    assert before_job is not None

    with pytest.raises(sqlite3.IntegrityError, match="forced clock replay failure"):
        if path == "retry":
            store.executions.retry(
                str(claimed["public_id"]),
                owner_user_id=int(claimed["owner_user_id"]),
            )
        else:
            store.executions.recover_after_namespace_cleanup(
                interruption_reason="orphan_after_runtime_recovery"
            )

    after_match = store.get_match(match_id)
    after_job = store.executions.get(str(claimed["public_id"]))
    assert after_match is not None and after_job is not None
    assert after_match["status"] == match_status
    assert (after_job["status"], after_job["terminal_reason"]) == (
        before_job["status"],
        before_job["terminal_reason"],
    )
    assert store.get_replay(match_id) is None


def _interrupt_claimed_with_event(
    store: Store,
    claimed: dict,
    match_id: str,
) -> dict:
    store.upsert_replay(
        match_id,
        json.dumps([{"type": "match_start", "game_id": "gomoku"}]),
    )
    assert store.executions.recover_after_namespace_cleanup(
        interruption_reason="orphan_after_service_restart"
    ) == {
        "requeued": 0,
        "interrupted": 1,
        "settling": 0,
    }
    interrupted = store.executions.get(str(claimed["public_id"]))
    assert interrupted is not None
    assert (interrupted["status"], interrupted["retryable"]) == (
        "interrupted",
        1,
    )
    return interrupted


@pytest.mark.parametrize(
    ("drift_kind", "initial_control"),
    [
        ("job_clock", "gomoku_per_side_total_900s_v1"),
        ("match_clock", "gomoku_per_side_total_900s_v1"),
        ("one_missing", "gomoku_per_side_total_300s_v1"),
        ("wrong_type", "gomoku_per_side_total_900s_v1"),
        ("job_rating", "gomoku_per_side_total_900s_v1"),
        ("match_rating", "gomoku_per_side_total_900s_v1"),
        ("policy_rating", "gomoku_per_side_total_900s_v1"),
    ],
)
def test_retry_terminalizes_clock_or_manual_rating_policy_drift(
    tmp_path: Path,
    drift_kind: str,
    initial_control: str,
) -> None:
    store = Store(str(tmp_path / f"retry-{drift_kind}.db"))
    _orch, claimed, match_id = _claimed_manual(
        store,
        time_control_id=initial_control,
    )
    interrupted = _interrupt_claimed_with_event(store, claimed, match_id)
    job_config = json.loads(str(interrupted["match_config"]))
    match = store.get_match(match_id)
    assert match is not None
    match_config = dict(match["match_config"])
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        if drift_kind == "job_clock":
            job_config["time_control_id"] = "gomoku_per_side_total_300s_v1"
            conn.execute(
                "UPDATE execution_jobs SET match_config=? WHERE id=?",
                (json.dumps(job_config), interrupted["id"]),
            )
        elif drift_kind == "match_clock":
            match_config["time_control_id"] = "gomoku_per_side_total_300s_v1"
            conn.execute(
                "UPDATE matches_gomoku SET match_config=? WHERE id=?",
                (json.dumps(match_config), match_id),
            )
        elif drift_kind == "one_missing":
            match_config.pop("time_control_id")
            conn.execute(
                "UPDATE matches_gomoku SET match_config=? WHERE id=?",
                (json.dumps(match_config), match_id),
            )
        elif drift_kind == "wrong_type":
            match_config["time_control_id"] = 300
            conn.execute(
                "UPDATE matches_gomoku SET match_config=? WHERE id=?",
                (json.dumps(match_config), match_id),
            )
        elif drift_kind == "job_rating":
            conn.execute(
                "UPDATE execution_jobs SET rated=0,rating_reason='eligible' WHERE id=?",
                (interrupted["id"],),
            )
        elif drift_kind == "match_rating":
            match_config["_rating_eligible"] = False
            conn.execute(
                "UPDATE matches_gomoku SET match_config=? WHERE id=?",
                (json.dumps(match_config), match_id),
            )
        else:
            policy = conn.execute(
                "SELECT * FROM match_rating_policies WHERE match_id=?",
                (match_id,),
            ).fetchone()
            assert policy is not None
            conn.execute(
                "DELETE FROM match_rating_policies WHERE match_id=?",
                (match_id,),
            )
            conn.execute(
                "INSERT INTO match_rating_policies("
                "match_id,game_id,rating_pool_id,bot_a_id,bot_b_id,rated,"
                "rating_reason,source,classified_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    match_id,
                    policy["game_id"],
                    policy["rating_pool_id"],
                    policy["bot_a_id"],
                    policy["bot_b_id"],
                    0,
                    "eligible",
                    policy["source"],
                    policy["classified_at"],
                ),
            )

    with pytest.raises(ValueError, match="^time_control_changed$"):
        store.executions.retry(
            str(claimed["public_id"]),
            owner_user_id=int(claimed["owner_user_id"]),
        )
    terminal = store.executions.get(str(claimed["public_id"]))
    assert terminal is not None
    assert (terminal["status"], terminal["retryable"]) == ("cancelled", 0)
    assert terminal["terminal_reason"] == "time_control_changed"
    attempt = store._conn.execute(
        "SELECT status,terminal_reason FROM execution_job_attempts WHERE match_id=?",
        (match_id,),
    ).fetchone()
    assert tuple(attempt) == ("cancelled", "time_control_changed")


def test_retry_accepts_legacy_default_missing_from_job_and_match(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "retry-legacy-default.db"))
    _orch, claimed, match_id = _claimed_manual(store)
    interrupted = _interrupt_claimed_with_event(store, claimed, match_id)
    job_config = json.loads(str(interrupted["match_config"]))
    match = store.get_match(match_id)
    assert match is not None
    match_config = dict(match["match_config"])
    job_config.pop("time_control_id")
    match_config.pop("time_control_id")
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE execution_jobs SET match_config=? WHERE id=?",
            (json.dumps(job_config), interrupted["id"]),
        )
        conn.execute(
            "UPDATE matches_gomoku SET match_config=? WHERE id=?",
            (json.dumps(match_config), match_id),
        )

    retried = store.executions.retry(
        str(claimed["public_id"]),
        owner_user_id=int(claimed["owner_user_id"]),
    )
    assert (retried["status"], retried["current_match_id"]) == ("queued", None)


@pytest.mark.parametrize("source", ["manual", "human"])
def test_namespace_recovery_terminalizes_manual_and_human_clock_drift(
    tmp_path: Path,
    source: str,
) -> None:
    store = Store(str(tmp_path / f"recovery-{source}.db"))
    if source == "manual":
        _orch, claimed, match_id = _claimed_manual(store)
    else:
        user, bot = _setup(store)
        orch = MatchOrchestrator(store, runner=_NeverRunMatch(), max_concurrent=1)
        match_id = asyncio.run(
            human_and_start(
                orch,
                bot["id"],
                user["id"],
                human_seat=1,
                game_id="gomoku",
                time_control_id="gomoku_per_side_total_900s_v1",
                defer_start=True,
            )
        )
        claimed = store.executions.get_by_match(match_id)
        assert claimed is not None
    match = store.get_match(match_id)
    assert match is not None
    match_config = dict(match["match_config"])
    match_config["time_control_id"] = "gomoku_per_side_total_300s_v1"
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET match_config=? WHERE id=?",
            (json.dumps(match_config), match_id),
        )

    assert store.executions.recover_after_namespace_cleanup(
        interruption_reason="orphan_after_service_restart"
    ) == {
        "requeued": 0,
        "interrupted": 0,
        "settling": 0,
    }
    terminal = store.executions.get(str(claimed["public_id"]))
    persisted_match = store.get_match(match_id)
    assert terminal is not None and persisted_match is not None
    assert (terminal["status"], terminal["retryable"]) == ("cancelled", 0)
    assert terminal["terminal_reason"] == "time_control_changed"
    assert (persisted_match["status"], persisted_match["reason"]) == (
        "aborted",
        "time_control_changed",
    )


def _claimed_contest(store: Store) -> tuple[dict, dict, str]:
    user, bot_a, _opponent, bot_b, version_a, version_b, pairing = (
        _active_contest_pairing(
            store,
            name="clock recovery contest",
            time_control_id="gomoku_per_side_total_300s_v1",
        )
    )
    contest = store.get_contest(pairing["contest_id"])
    assert contest is not None
    queued = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=user["id"],
        game_id="gomoku",
        match_type=TYPE_CONTEST,
        bot_a_id=bot_a["id"],
        bot_b_id=bot_b["id"],
        bot_a_version_id=version_a["id"],
        bot_b_version_id=version_b["id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
        match_config={
            "time_control_id": "gomoku_per_side_total_300s_v1",
            "duplicate": False,
        },
    )
    claimed = claim_request(
        MatchOrchestrator(store, runner=_NeverRunMatch(), max_concurrent=1),
        queued["public_id"],
        start=False,
    )
    return contest, pairing, str(claimed["current_match_id"])


def test_namespace_recovery_binds_contest_snapshot_before_requeue(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "recovery-contest.db"))
    contest, pairing, match_id = _claimed_contest(store)
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE contests SET time_control_id=? WHERE id=?",
            ("gomoku_per_side_total_900s_v1", contest["id"]),
        )

    assert store.executions.recover_after_namespace_cleanup(
        interruption_reason="orphan_after_service_restart"
    ) == {
        "requeued": 0,
        "interrupted": 0,
        "settling": 0,
    }
    job = store.executions.get_by_match(match_id)
    match = store.get_match(match_id)
    pairing_after = store.list_pairings(contest["id"])[0]
    assert job is not None and match is not None
    assert (job["status"], job["terminal_reason"]) == (
        "cancelled",
        "time_control_changed",
    )
    assert (match["status"], match["reason"]) == (
        "aborted",
        "time_control_changed",
    )
    assert pairing_after["id"] == pairing["id"]
    assert (pairing_after["status"], pairing_after["match_id"]) == (
        "pending",
        None,
    )


def test_namespace_recovery_accepts_legacy_default_missing_from_all_copies(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "recovery-legacy-default.db"))
    _orch, claimed, match_id = _claimed_manual(store)
    job_config = json.loads(str(claimed["match_config"]))
    match = store.get_match(match_id)
    assert match is not None
    match_config = dict(match["match_config"])
    job_config.pop("time_control_id")
    match_config.pop("time_control_id")
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE execution_jobs SET match_config=? WHERE id=?",
            (json.dumps(job_config), claimed["id"]),
        )
        conn.execute(
            "UPDATE matches_gomoku SET match_config=? WHERE id=?",
            (json.dumps(match_config), match_id),
        )

    assert store.executions.recover_after_namespace_cleanup(
        interruption_reason="orphan_after_service_restart"
    ) == {
        "requeued": 1,
        "interrupted": 0,
        "settling": 0,
    }
    recovered = store.executions.get(str(claimed["public_id"]))
    assert recovered is not None
    assert (recovered["status"], recovered["current_match_id"]) == ("queued", None)
    assert store.get_match(match_id) is None


def test_namespace_recovery_never_settles_completed_match_with_clock_drift(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "recovery-completed-drift.db"))
    _orch, claimed, match_id = _claimed_manual(store)
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        reason="normal",
        result={},
        ended_at="2026-08-31T00:00:00",
    )
    match = store.get_match(match_id)
    assert match is not None
    match_config = dict(match["match_config"])
    match_config["time_control_id"] = "gomoku_per_side_total_300s_v1"
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET match_config=? WHERE id=?",
            (json.dumps(match_config), match_id),
        )

    with pytest.raises(ExecutionInvariantError, match="^time_control_changed$"):
        store.executions.recover_after_namespace_cleanup(
            interruption_reason="orphan_after_service_restart"
        )
    unchanged = store.executions.get(str(claimed["public_id"]))
    assert unchanged is not None
    assert unchanged["status"] == "settling"
    assert store._conn.execute(
        "SELECT COUNT(*) FROM match_rating_settlements WHERE match_id=?",
        (match_id,),
    ).fetchone()[0] == 0


@pytest.mark.parametrize("drifted_copy", ["job", "match"])
def test_start_rejects_legal_but_mismatched_frozen_time_control(
    tmp_path: Path,
    drifted_copy: str,
) -> None:
    store = Store(str(tmp_path / f"{drifted_copy}.db"))
    user, bot = _setup(store)
    runner = _NeverRunMatch()
    orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)

    async def exercise() -> str:
        match_id = await challenge_and_start(
            orch,
            bot["id"],
            bot["id"],
            user["id"],
            game_id="gomoku",
            time_control_id="gomoku_per_side_total_900s_v1",
            defer_start=True,
        )
        match = store.get_match(match_id)
        job = store.executions.get_by_match(match_id)
        assert match is not None and job is not None
        match_config = dict(match["match_config"])
        job_config = json.loads(str(job["match_config"]))
        target = "gomoku_per_side_total_300s_v1"
        with store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if drifted_copy == "job":
                job_config["time_control_id"] = target
                conn.execute(
                    "UPDATE execution_jobs SET match_config=? "
                    "WHERE current_match_id=?",
                    (json.dumps(job_config), match_id),
                )
            else:
                match_config["time_control_id"] = target
                conn.execute(
                    "UPDATE matches_gomoku SET match_config=? WHERE id=?",
                    (json.dumps(match_config), match_id),
                )

        start_claimed_match(orch, match_id)
        task = orch._tasks[match_id]
        await task
        return match_id

    match_id = asyncio.run(exercise())
    match = store.get_match(match_id)
    assert match is not None
    assert match["status"] == "aborted"
    assert match["reason"] == "invalid_match_config"
    assert runner.calls == 0


@pytest.mark.parametrize("drift_kind", ["same_alternate", "both_missing"])
def test_contest_start_rejects_job_and_match_drift_from_contest_snapshot(
    tmp_path: Path,
    drift_kind: str,
) -> None:
    store = Store(str(tmp_path / f"contest-{drift_kind}.db"))
    user, bot_a, _opponent, bot_b, version_a, version_b, pairing = (
        _active_contest_pairing(
            store,
            name="clock binding contest",
            time_control_id="gomoku_per_side_total_300s_v1",
        )
    )
    contest = store.get_contest(pairing["contest_id"])
    assert contest is not None
    runner = _NeverRunMatch()
    orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
    queued = store.executions.enqueue(
        source=EXECUTION_SOURCE_CONTEST,
        owner_user_id=user["id"],
        game_id="gomoku",
        match_type=TYPE_CONTEST,
        bot_a_id=bot_a["id"],
        bot_b_id=bot_b["id"],
        bot_a_version_id=version_a["id"],
        bot_b_version_id=version_b["id"],
        contest_id=contest["id"],
        contest_pairing_id=pairing["id"],
        match_config={
            "time_control_id": "gomoku_per_side_total_300s_v1",
            "duplicate": False,
        },
    )
    claimed = claim_request(orch, queued["public_id"], start=False)
    match_id = str(claimed["current_match_id"])
    match = store.get_match(match_id)
    job = store.executions.get_by_match(match_id)
    assert match is not None and job is not None
    match_config = dict(match["match_config"])
    job_config = json.loads(str(job["match_config"]))
    if drift_kind == "same_alternate":
        target = "gomoku_per_side_total_900s_v1"
        match_config["time_control_id"] = target
        job_config["time_control_id"] = target
    else:
        match_config.pop("time_control_id")
        job_config.pop("time_control_id")
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE execution_jobs SET match_config=? WHERE current_match_id=?",
            (json.dumps(job_config), match_id),
        )
        conn.execute(
            "UPDATE matches_gomoku SET match_config=? WHERE id=?",
            (json.dumps(match_config), match_id),
        )

    async def exercise() -> None:
        start_claimed_match(orch, match_id)
        await orch._tasks[match_id]

    asyncio.run(exercise())
    persisted = store.get_match(match_id)
    assert persisted is not None
    assert persisted["status"] == "aborted"
    assert persisted["reason"] == "invalid_match_config"
    assert runner.calls == 0


def test_human_start_rejects_legal_but_mismatched_frozen_time_control(
    tmp_path: Path,
) -> None:
    store = Store(str(tmp_path / "human-drift.db"))
    user, bot = _setup(store)
    runner = _NeverRunMatch()
    orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)

    async def exercise() -> str:
        match_id = await human_and_start(
            orch,
            bot["id"],
            user["id"],
            human_seat=1,
            game_id="gomoku",
            time_control_id="gomoku_per_side_total_900s_v1",
            defer_start=True,
        )
        job = store.executions.get_by_match(match_id)
        assert job is not None
        job_config = json.loads(str(job["match_config"]))
        job_config["time_control_id"] = "gomoku_per_side_total_300s_v1"
        with store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE execution_jobs SET match_config=? "
                "WHERE current_match_id=?",
                (json.dumps(job_config), match_id),
            )

        start_claimed_match(orch, match_id)
        await orch._tasks[match_id]
        return match_id

    match_id = asyncio.run(exercise())
    match = store.get_match(match_id)
    assert match is not None
    assert match["status"] == "aborted"
    assert match["reason"] == "invalid_match_config"
    assert runner.calls == 0


@pytest.mark.parametrize("human", [False, True])
def test_public_detail_and_replay_share_frozen_match_start_time_control(
    tmp_path: Path,
    human: bool,
) -> None:
    store = Store(str(tmp_path / f"public-{'human' if human else 'bot'}.db"))
    user, bot = _setup(store)
    runner = _PublicTimeControlRunner()
    orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)

    async def exercise() -> str:
        if human:
            match_id = await human_and_start(
                orch,
                bot["id"],
                user["id"],
                human_seat=1,
                game_id="gomoku",
                time_control_id="gomoku_per_side_total_300s_v1",
            )
        else:
            match_id = await challenge_and_start(
                orch,
                bot["id"],
                bot["id"],
                user["id"],
                game_id="gomoku",
                time_control_id="gomoku_per_side_total_300s_v1",
            )
        task = orch._tasks.get(match_id)
        if task is not None:
            await task
        return match_id

    match_id = asyncio.run(exercise())
    public_match = sanitize_public_match(store.get_match_detailed(match_id))
    replay = store.get_public_replay(match_id)
    assert public_match is not None and replay is not None
    events = json.loads(replay["events_json"])
    start = next(event for event in events if event.get("type") == "match_start")
    expected = {
        "id": "gomoku_per_side_total_300s_v1",
        "mode": "per_side_total",
        "seconds": 300,
        "applies_to": "bot_only" if human else "both_bots",
    }
    assert public_match["time_control"] == expected
    assert start["time_control"] == expected
    assert with_seat_info(public_match)["time_control"] == expected
    viewer_match = match_for_viewer(store, match_id)
    assert viewer_match is not None
    assert viewer_match["time_control"] == expected
    snapshot = orch.subscribe(match_id).get_nowait()
    assert snapshot["match"]["time_control"] == expected
    snapshot_start = next(
        event
        for event in snapshot["events"]
        if event.get("type") == "match_start"
    )
    assert snapshot_start["time_control"] == expected
    assert runner.calls == [
        {
            "game_id": "gomoku",
            "time_control_id": "gomoku_per_side_total_300s_v1",
            "time_control": expected,
        }
    ]

    # Persisted replays predating the nested object inherit only the Match's
    # registry-resolved frozen control.  An explicit contradictory object is
    # damaged evidence and must not be rewritten into apparent consistency.
    store.upsert_replay(
        match_id,
        json.dumps([{"type": "match_start", "game_id": "gomoku"}]),
    )
    legacy_replay = store.get_public_replay(match_id)
    assert legacy_replay is not None
    legacy_start = json.loads(legacy_replay["events_json"])[0]
    assert legacy_start["time_control"] == expected

    contradictory = {
        "id": "gomoku_per_side_total_900s_v1",
        "mode": "per_side_total",
        "seconds": 900,
        "applies_to": "bot_only" if human else "both_bots",
    }
    store.upsert_replay(
        match_id,
        json.dumps(
            [
                {
                    "type": "match_start",
                    "game_id": "gomoku",
                    "time_control": contradictory,
                }
            ]
        ),
    )
    damaged_replay = store.get_public_replay(match_id)
    assert damaged_replay is not None
    damaged_start = json.loads(damaged_replay["events_json"])[0]
    assert damaged_start["time_control"] is None

    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "UPDATE matches_gomoku SET match_config=? WHERE id=?",
            ("{malformed", match_id),
        )
    store.upsert_replay(
        match_id,
        json.dumps(
            [
                {
                    "type": "match_start",
                    "game_id": "gomoku",
                },
                {
                    "type": "time_used",
                    "seat": 0,
                    "used": 1,
                    "remaining": 899,
                    "budget": 900,
                },
            ]
        ),
    )
    malformed_public = sanitize_public_match(store.get_match_detailed(match_id))
    malformed_replay = store.get_public_replay(match_id)
    assert malformed_public is not None and malformed_replay is not None
    assert malformed_public["time_control"] is None
    assert "_match_config_malformed" not in malformed_public
    malformed_events = json.loads(malformed_replay["events_json"])
    assert malformed_events[0]["time_control"] is None
    assert malformed_events[1]["budget"] == 900
