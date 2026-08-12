"""Mutating QA helpers keep every artifact in an isolated runtime."""
from __future__ import annotations

import importlib.util
import asyncio
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store
from scripts._qa_polling import QaPollingError, RateAwareJsonPoller


ROOT = Path(__file__).resolve().parents[3]


def load_script(stem: str):
    module_name = f"qa_script_{stem}"
    spec = importlib.util.spec_from_file_location(
        module_name, ROOT / "scripts" / f"{stem}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_contest_stress_default_uploads_follow_database(tmp_path):
    module = load_script("contest_stress")
    db_path = tmp_path / "runtime" / "contest.db"
    bot_ids, _admin_id, _token = module.seed(str(db_path), 1)

    store = Store(str(db_path))
    try:
        expected = (db_path.parent / "bot_uploads").resolve()
        for bot_id in bot_ids.values():
            binary = Path(store.get_bot(bot_id)["binary_path"]).resolve()
            assert expected in binary.parents
    finally:
        store.close()


def test_contest_stress_never_reuses_or_mutates_arbitrary_admin(tmp_path):
    module = load_script("contest_stress")
    db_path = tmp_path / "contest.db"
    store = Store(str(db_path))
    real_admin = store.create_user(
        "admin",
        "owner@example.com",
        hash_password("OwnerSecret1234"),
        role="admin",
    )
    store.update_user(real_admin["id"], is_active=0, email_verified=0)
    store.close()

    _bot_ids, admin_id, token = module.seed(str(db_path), 1)

    store = Store(str(db_path))
    try:
        untouched = store.get_user(real_admin["id"])
        dedicated = store.get_user_by_username(module.CONTEST_ADMIN_NAME)
        assert admin_id == dedicated["id"]
        assert dedicated["email"] == (
            f"{module.CONTEST_ADMIN_NAME}@{module.EMAIL_DOMAIN}"
        )
        assert store.get_session(token)["user_id"] == dedicated["id"]
        assert untouched["is_active"] == 0
        assert untouched["email_verified"] == 0
        assert store._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id=?",
            (real_admin["id"],),
        ).fetchone()[0] == 0
    finally:
        store.close()


def test_contest_stress_rejects_conflicting_dedicated_admin_before_writes(tmp_path):
    module = load_script("contest_stress")
    db_path = tmp_path / "contest.db"
    store = Store(str(db_path))
    namesake = store.create_user(
        module.CONTEST_ADMIN_NAME,
        "foreign@example.com",
        hash_password("ForeignSecret1234"),
        role="admin",
    )
    store.update_user(namesake["id"], is_active=0, email_verified=0)
    store.close()

    with pytest.raises(RuntimeError, match="专用 QA 身份契约不匹配"):
        module.seed(str(db_path), 1)

    store = Store(str(db_path))
    try:
        unchanged = store.get_user(namesake["id"])
        assert unchanged["is_active"] == 0
        assert unchanged["email_verified"] == 0
        assert store.get_user_by_username("cs_u001") is None
        assert store._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        store.close()


def test_contest_stress_rejects_conflicting_seed_user_without_activation(tmp_path):
    module = load_script("contest_stress")
    db_path = tmp_path / "contest.db"
    store = Store(str(db_path))
    namesake = store.create_user(
        "cs_u001",
        "cs_u001@contest.local",
        hash_password(module.PASSWORD),
        role="organizer",
    )
    store.update_user(namesake["id"], is_active=0, email_verified=0)
    store.close()

    with pytest.raises(RuntimeError, match="role"):
        module.seed(str(db_path), 1)

    store = Store(str(db_path))
    try:
        unchanged = store.get_user(namesake["id"])
        assert unchanged["role"] == "organizer"
        assert unchanged["is_active"] == 0
        assert unchanged["email_verified"] == 0
    finally:
        store.close()


def test_api_account_seed_is_idempotent_in_isolated_database(tmp_path):
    module = load_script("api_full_test")
    db_path = tmp_path / "api.db"

    first = module.seed_qa_accounts(str(db_path))
    second = module.seed_qa_accounts(str(db_path))
    assert second == first

    store = Store(str(db_path))
    try:
        rows = [store.get_user_by_username(username) for username in first.values()]
        assert all(rows)
        assert len({row["id"] for row in rows}) == len(module.QA_ACCOUNTS)
        assert store.get_user_by_username(first["unverified"])["email_verified"] == 0
        assert all(
            store.get_user_by_username(first[name])["email_verified"] == 1
            for name in ("admin", "alice", "bob", "carol", "org1")
        )
    finally:
        store.close()


def test_api_replay_count_uses_nested_result_contract():
    module = load_script("api_full_test")
    assert module.match_rounds_played({"result": {"rounds_played": 70}}) == 70
    assert module.match_rounds_played({"rounds_played": 70}) == 0
    assert module.qa_contest_payload("run1")["template_id"] == "holdem_rr"
    assert module.SEQUENTIAL_MATCH_TIMEOUT_SEC == 360.0
    assert module.CONTENDED_MATCH_TIMEOUT_SEC == 720.0

    load_module = load_script("load_test")
    games = ["holdem", "gomoku", "pencil"] * 4
    assert load_module.SEQUENTIAL_MATCH_TIMEOUT_SEC == 360.0
    assert load_module.CONTENDED_MATCH_TIMEOUT_SEC == 720.0
    assert load_module.load_batch_timeout_seconds(games) == 2880.0

    contest_payload = load_module.bounded_contest_payload("run1")
    assert contest_payload["game_id"] == "gomoku"
    assert load_module.CONTEST_ENTRANT_COUNT == 4
    assert load_module.contest_match_count(contest_payload["stages"]) == 3
    assert load_module.contest_timeout_seconds(contest_payload["stages"]) == 600.0
    assert load_module.selected_phase_numbers(5) == (5, 6, 7)


def test_load_contest_gate_is_bounded_but_keeps_the_full_lifecycle():
    module = load_script("load_test")
    source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    phase5_source = source.split("def phase5_contest", 1)[1].split(
        "def phase6_code_config", 1
    )[0]

    payload = module.bounded_contest_payload("gate")
    assert [stage["type"] for stage in payload["stages"]] == [
        "swiss",
        "single_elimination",
    ]
    assert payload["stages"][0]["rounds"] == 1
    assert payload["stages"][0]["advance_count"] == 2
    assert payload["stages"][0]["rest_after_minutes"] == 1
    assert "time.monotonic() + 400" not in phase5_source
    assert 'f"/api/contests/{cid}/publish"' in phase5_source
    assert 'published.get("status") == "published"' in phase5_source
    assert '"/resume"' not in phase5_source  # URL is formatted with the contest id.
    assert 'f"/api/contests/{cid}/resume"' in phase5_source
    assert "if not resumed:\n                    return\n                saw_running = True" in phase5_source
    assert 'f"/api/contests/{cid}/official-results"' in phase5_source
    assert "ratings_before == ratings_after" in phase5_source
    assert "wait_execution_queue_idle(" in phase5_source


def _clean_execution_queue_snapshot():
    return {
        "dispatcher": {
            "state": "running",
            "accepting": True,
            "auto_enabled": False,
            "pause_reason": "",
            "retry_at": None,
        },
        "capacity": {
            "match_slots": {"used": 0, "capacity": 2},
            "sandbox_units": {"used": 0, "capacity": 4},
            "running_matches": 0,
        },
        "active": [],
        "queued": [],
        "queued_count": 0,
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("dispatcher", "state"), "paused"),
        (("dispatcher", "accepting"), False),
        (("active",), [{"public_id": "req_active"}]),
        (("queued",), [{"public_id": "req_queued"}]),
        (("queued_count",), 1),
        (("capacity", "match_slots", "used"), 1),
        (("capacity", "sandbox_units", "used"), 2),
        (("capacity", "running_matches"), 1),
        (("capacity", "match_slots", "capacity"), 0),
        (("queued_count",), False),
    ],
)
def test_load_continuation_queue_gate_rejects_every_dirty_dimension(path, value):
    module = load_script("load_test")
    payload = _clean_execution_queue_snapshot()
    target = payload
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value

    assert module.execution_queue_health_errors(payload)


def test_load_continuation_queue_gate_accepts_exact_healthy_idle_snapshot():
    module = load_script("load_test")
    assert module.execution_queue_health_errors(_clean_execution_queue_snapshot()) == []
    assert module.needs_continuation_gate(skip_seed=False, start_phase=0) is False
    assert module.needs_continuation_gate(skip_seed=True, start_phase=0) is True
    assert module.needs_continuation_gate(skip_seed=False, start_phase=5) is True


@pytest.mark.parametrize(
    ("status", "blocked"),
    [
        ("draft", False),
        ("open", True),
        ("published", True),
        ("running", True),
        ("rest", True),
        ("finished", False),
        ("cancelled", False),
    ],
)
def test_load_continuation_db_gate_only_blocks_unfinished_load_contests(
    tmp_path, status, blocked
):
    module = load_script("load_test")
    db_path = tmp_path / "qa.db"
    with sqlite3.connect(db_path) as con:
        con.execute(
            "CREATE TABLE contests(id INTEGER PRIMARY KEY,title TEXT,status TEXT)"
        )
        con.execute(
            "INSERT INTO contests(title,status) VALUES(?,?)",
            ("LoadTest stale", status),
        )
        con.execute(
            "INSERT INTO contests(title,status) VALUES(?,?)",
            ("Other contest", "running"),
        )

    rows = module.active_loadtest_contests(str(db_path))

    assert bool(rows) is blocked
    if rows:
        assert rows == [{"id": 1, "title": "LoadTest stale", "status": status}]


def test_load_continuation_gate_rejects_second_snapshot_that_becomes_dirty(
    monkeypatch,
):
    module = load_script("load_test")
    clean = _clean_execution_queue_snapshot()
    dirty = _clean_execution_queue_snapshot()
    dirty["queued"] = [{"public_id": "req_late"}]
    dirty["queued_count"] = 1

    class FakeApi:
        db_path = "/tmp/unused-qa.db"

        def __init__(self):
            self.snapshots = iter([clean, dirty])

        def poll_json(self, *_args, **_kwargs):
            return next(self.snapshots)

    monkeypatch.setattr(module, "active_loadtest_contests", lambda _path: [])

    with pytest.raises(module.ContinuationCleanGateError, match="queued"):
        module.require_continuation_clean(FakeApi())


def test_load_admin_phase_and_main_keep_the_final_queue_gate():
    source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    phase7_source = source.split("def phase7_admin", 1)[1].split("def main", 1)[0]
    main_source = source.split("def main", 1)[1].split("def _rebuild_ctx", 1)[0]

    assert '"request_id": phase7_request_id' in phase7_source
    assert "阶段7 强制 abort 挑战取得 Match" in phase7_source
    assert "warn(f\"阶段7 强制 abort 对局发起失败" not in phase7_source
    assert "全部阶段结束后队列与调度器健康归零" in main_source
    assert "wait_execution_queue_idle(" in main_source
    assert 'label="全部阶段结束后的稳定 clean gate"' in main_source


class _FakePollClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


def _poll_response(status: int, payload, *, retry_after: str | None = None):
    headers = {} if retry_after is None else {"Retry-After": retry_after}
    return SimpleNamespace(
        status_code=status,
        headers=headers,
        text=json.dumps(payload),
        json=lambda: payload,
    )


def test_load_challenge_retry_is_bounded_and_never_reposts_accepted_request(
    monkeypatch,
):
    module = load_script("load_test")
    payload = {"my_bot_id": 1, "opponent_bot_id": 2, "game_id": "gomoku"}
    sleeps: list[float] = []

    class FakeApi:
        def __init__(self, responses):
            self.responses = iter(responses)
            self.calls: list[tuple[str, str, object]] = []

        def authed(self, token, method, path, **kwargs):
            self.calls.append((method, path, kwargs.get("json")))
            return next(self.responses)

    monkeypatch.setattr(module.time, "sleep", sleeps.append)
    accepted_api = FakeApi(
        [
            _poll_response(429, {"code": "rate_limit_exceeded"}, retry_after="2"),
            _poll_response(202, {"request": {"public_id": "request-1"}}),
            pytest.fail,
        ]
    )

    response = module._paced_challenge(
        accepted_api, "token", payload, max_attempts=3
    )

    assert response.status_code == 202
    assert accepted_api.calls == [
        ("POST", "/api/matches/challenge", payload),
        ("POST", "/api/matches/challenge", payload),
    ]
    assert all(call[2] is payload for call in accepted_api.calls)
    assert sleeps == [3.0]

    sleeps.clear()
    limited_api = FakeApi(
        [
            _poll_response(429, {}, retry_after="1"),
            _poll_response(429, {}, retry_after="1"),
            _poll_response(429, {}, retry_after="1"),
        ]
    )

    response = module._paced_challenge(
        limited_api, "token", payload, max_attempts=3
    )

    assert response.status_code == 429
    assert len(limited_api.calls) == 3
    assert sleeps == [2.0, 2.0]


def test_load_phase2_uses_the_bounded_challenge_retry_helper():
    source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    phase2_source = source.split("def phase2_matches", 1)[1].split(
        "def _rate_limited_post", 1
    )[0]

    assert "r = _paced_challenge(api, owner_tok, payload)" in phase2_source
    assert (
        'api.authed(owner_tok, "POST", "/api/matches/challenge"'
        not in phase2_source
    )
    assert "_check_phase2_transport_outcomes(" in phase2_source


@pytest.mark.parametrize(
    ("accepted_count", "terminal_count", "errors"),
    [
        (11, 11, ["challenge gomoku: HTTP 429 rate_limit_exceeded"]),
        (12, 11, ["wait pencil: 阶段2批次超时"]),
    ],
)
def test_load_phase2_transport_errors_make_the_final_exit_nonzero(
    monkeypatch,
    accepted_count,
    terminal_count,
    errors,
):
    module = load_script("load_test")
    monkeypatch.setattr(module, "PASS", 0)
    monkeypatch.setattr(module, "FAIL", 0)
    monkeypatch.setattr(module, "FAILS", [])

    module._check_phase2_transport_outcomes(
        accepted_count=accepted_count,
        terminal_count=terminal_count,
        errors=errors,
    )

    assert module.FAIL > 0
    assert (0 if module.FAIL == 0 else 1) == 1


def test_load_phase2_selfplay_ignores_the_phase1_soft_deleted_extra_bot():
    module = load_script("load_test")
    users = [f"load_u{i:02d}" for i in range(1, 9)]
    bots = {
        username: {
            game: user_idx * 10 + game_idx
            for game_idx, game in enumerate(module.GAMES, start=1)
        }
        for user_idx, username in enumerate(users, start=1)
    }
    active_seed_bot_ids = {
        bot_id for by_game in bots.values() for bot_id in by_game.values()
    }
    phase1_soft_deleted_extra_id = 999_999

    pairs = module._phase2_pairs({"user_names": users, "bots": bots})

    assert len(pairs) == module.TARGET_MATCHES == 12
    assert all(
        my_bot_id in active_seed_bot_ids and opponent_bot_id in active_seed_bot_ids
        for my_bot_id, opponent_bot_id, _game in pairs
    )
    assert all(
        phase1_soft_deleted_extra_id not in (my_bot_id, opponent_bot_id)
        for my_bot_id, opponent_bot_id, _game in pairs
    )
    for index in range(0, module.TARGET_MATCHES, 4):
        my_bot_id, opponent_bot_id, game = pairs[index]
        owner = users[index % len(users)]
        assert my_bot_id == opponent_bot_id == bots[owner][game]


def test_api_leaderboard_check_uses_public_numeric_ranking_contract():
    module = load_script("api_full_test")
    payload = {
        "leaderboard": [
            {
                "rating": 1510.0,
                "rated_matches": 10,
                "ranking_eligible": True,
                "rank": 1,
            },
            {
                "rating": 1500.0,
                "rated_matches": 2,
                "ranking_eligible": False,
                "rank": None,
            },
        ],
        "ranking_min_matches": 10,
    }
    body, rows = module.require_leaderboard_payload(_poll_response(200, payload))
    assert body is payload
    assert rows == payload["leaderboard"]
    assert module.validate_leaderboard_numeric_contract(body, rows)[0] is True

    old_field = {
        "leaderboard": [
            {
                "rating": 1510.0,
                "matches_played": 10,
                "ranking_eligible": True,
                "rank": 1,
            }
        ],
        "ranking_min_matches": 10,
    }
    assert module.validate_leaderboard_numeric_contract(
        old_field, old_field["leaderboard"]
    )[0] is False

    mismatch = {**payload, "leaderboard": [{**payload["leaderboard"][1], "rank": 2}]}
    assert module.validate_leaderboard_numeric_contract(
        mismatch, mismatch["leaderboard"]
    )[0] is False

    with pytest.raises(QaPollingError, match=r"HTTP 503"):
        module.require_leaderboard_payload(
            _poll_response(503, {"detail": "unavailable"})
        )
    malformed = SimpleNamespace(
        status_code=200,
        headers={},
        text="not-json",
        json=lambda: (_ for _ in ()).throw(ValueError("invalid JSON")),
    )
    with pytest.raises(QaPollingError, match="不可解析为 JSON"):
        module.require_leaderboard_payload(malformed)
    with pytest.raises(QaPollingError, match="leaderboard 必须是对象列表"):
        module.require_leaderboard_payload(
            _poll_response(200, {"leaderboard": ["not-an-object"]})
        )


def test_rate_aware_qa_poller_coordinates_and_honors_retry_after():
    clock = _FakePollClock()
    poller = RateAwareJsonPoller(
        min_interval=1.0,
        retry_padding=0.05,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    responses = iter(
        [
            _poll_response(
                429,
                {"code": "rate_limit_exceeded"},
                retry_after="2",
            ),
            _poll_response(200, {"match": {"status": "running"}}),
            _poll_response(200, {"match": {"status": "completed"}}),
        ]
    )

    first = poller.get_json(
        lambda: next(responses),
        label="match m1",
        deadline=10,
    )
    second = poller.get_json(
        lambda: next(responses),
        label="match m2",
        deadline=10,
    )

    assert first["match"]["status"] == "running"
    assert second["match"]["status"] == "completed"
    assert clock.sleeps == pytest.approx([2.05, 1.0])


def test_rate_aware_qa_poller_reports_http_status_and_body():
    clock = _FakePollClock()
    poller = RateAwareJsonPoller(
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    with pytest.raises(QaPollingError, match=r"HTTP 503 body=.*unavailable"):
        poller.get_json(
            lambda: _poll_response(503, {"detail": "unavailable"}),
            label="match m1",
            deadline=10,
        )

    malformed = SimpleNamespace(
        status_code=200,
        headers={},
        text="not-json",
        json=lambda: (_ for _ in ()).throw(ValueError("invalid JSON")),
    )
    with pytest.raises(QaPollingError, match=r"HTTP 200 body='not-json'"):
        poller.get_json(
            lambda: malformed,
            label="match m2",
            deadline=10,
        )


def test_qa_match_polling_artifacts_are_rate_aware_and_bounded():
    api_source = (ROOT / "scripts" / "api_full_test.py").read_text(
        encoding="utf-8"
    )
    load_source = (ROOT / "scripts" / "load_test.py").read_text(
        encoding="utf-8"
    )
    load_doc = (ROOT / "doc" / "LOADTEST.md").read_text(encoding="utf-8")

    for source in (api_source, load_source):
        assert "RateAwareJsonPoller(min_interval=1.0)" in source
        assert "execution_request_path(public_id)" in source
        assert "SEQUENTIAL_MATCH_TIMEOUT_SEC = 360.0" in source
        assert "CONTENDED_MATCH_TIMEOUT_SEC = SEQUENTIAL_MATCH_TIMEOUT_SEC * 2" in source
        assert "time.sleep(0.4)" not in source
        assert "time.sleep(0.5)" not in source
    assert "batch_timeout = SEQUENTIAL_MATCH_TIMEOUT_SEC * n" in api_source
    assert "timeout=min(CONTENDED_MATCH_TIMEOUT_SEC, match_remaining)" in api_source
    assert "load_batch_timeout_seconds([game for _, _, game in pairs])" in load_source
    assert "timeout=min(CONTENDED_MATCH_TIMEOUT_SEC, match_remaining)" in load_source
    assert "Retry-After" in load_doc
    assert "2880 秒绝对截止" in load_doc
    assert "不会靠关闭服务端限流绕过边界" in load_doc


def test_api_no_smtp_registration_persists_user_and_queues_delivery():
    module = load_script("api_full_test")
    ok, detail = module.verify_no_smtp_registration_persistence()
    assert ok, detail


def test_e2e_smoke_rejects_main_port_and_cleans_early_temp_runtime(tmp_path):
    script = ROOT / "scripts" / "e2e_smoke.sh"
    syntax = subprocess.run(
        ["bash", "-n", str(script)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert syntax.returncode == 0, syntax.stderr

    env = os.environ.copy()
    env.update(
        {
            "TMPDIR": str(tmp_path),
            "BZ_E2E_PORT": "50380",
            "BZ_PYTHON": sys.executable,
        }
    )
    rejected = subprocess.run(
        ["bash", str(script)],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert rejected.returncode == 2
    assert "50380" in rejected.stderr
    assert list(tmp_path.iterdir()) == [], "early exit must remove its exact mktemp runtime"


def test_qa_scripts_do_not_probe_retired_matchpacks_or_override_fixed_rules():
    load_source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    smoke_source = (ROOT / "scripts" / "e2e_smoke.sh").read_text(encoding="utf-8")

    assert "/api/matchpacks" not in load_source
    assert '"hands": 2' not in smoke_source
    assert "hands_per_match" not in smoke_source


def test_e2e_smoke_requires_completed_numeric_zero_sum_deltas():
    source = (ROOT / "scripts" / "e2e_smoke.sh").read_text(encoding="utf-8")

    assert 'if status == "aborted"' in source
    assert 'if status == "completed"' in source
    assert "len(deltas) != 2" in source
    assert "not isinstance(value, (int, float))" in source
    assert "deltas[0] + deltas[1] != 0" in source
    assert "or [0, 0]" not in source


class _FakeWebSocket:
    def __init__(self, events):
        self._events = iter(events)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def recv(self):
        return json.dumps(next(self._events))

    async def send(self, _payload):
        return None


class _FakeWebSockets:
    def __init__(self, events):
        self._events = events

    def connect(self, *_args, **_kwargs):
        return _FakeWebSocket(self._events)


def test_load_human_websocket_error_is_failure_and_match_end_is_success():
    module = load_script("load_test")
    api = type("ApiStub", (), {"base": "http://qa.invalid"})()

    failed = module._play_human_match(
        api,
        _FakeWebSockets(
            [
                {"type": "your_turn", "request": {}},
                {"type": "error", "reason": "platform_error"},
            ]
        ),
        asyncio,
        "match-1",
        "token",
        "holdem",
    )
    completed = module._play_human_match(
        api,
        _FakeWebSockets([{"type": "match_end"}]),
        asyncio,
        "match-2",
        "token",
        "holdem",
    )

    assert failed is False
    assert completed is True
    assert module.WARN == ["WS holdem 返回 error: platform_error"]


def test_load_human_phase_checks_persisted_completed_status():
    source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    assert "api.wait_match(hu_tok, mid, timeout=30)" in source
    assert 'persisted and m.get("status") == "completed"' in source
    assert "持久化 status=completed" in source


def test_load_missing_websocket_dependency_is_a_hard_failure(monkeypatch):
    module = load_script("load_test")
    monkeypatch.setattr(module, "_websocket_dependencies", lambda: None)

    module.phase4_human(None, {})

    assert module.FAIL == 1
    assert any("WebSocket" in failure for failure in module.FAILS)
    assert not module.WARN


def test_load_test_does_not_mutate_code_owned_auto_match_configuration():
    module = load_script("load_test")
    source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")

    assert not hasattr(module, "_record_auto_match_outcome")
    assert "--allow-auto-match-miss" not in source
    assert "PATCH runtime 写入口不存在" in source
    assert "公开赛制模板来自代码且只读" in source


def test_qa_script_claims_match_the_observed_coverage():
    load_source = (ROOT / "scripts" / "load_test.py").read_text(encoding="utf-8")
    api_source = (ROOT / "scripts" / "api_full_test.py").read_text(encoding="utf-8")
    contest_source = (ROOT / "scripts" / "contest_stress.py").read_text(encoding="utf-8")
    load_doc = (ROOT / "doc" / "LOADTEST.md").read_text(encoding="utf-8")
    doc_index = (ROOT / "doc" / "INDEX.md").read_text(encoding="utf-8")

    assert "CONCURRENCY =" not in load_source
    assert "--allow-auto-match-miss" not in load_source
    assert "不等待也不声称验证" in load_source
    assert "snapshot 之后的实时增量事件" in load_source
    assert "SSE 实时事件流" not in api_source
    assert "终态 snapshot" in api_source
    assert "并发提交的 {n} 个挑战全部返回 202 + opaque public_id" in api_source
    assert "超过并发容量的请求由持久队列保留" in api_source
    assert "首波精确接纳" not in api_source
    assert "超额请求明确 429" not in api_source
    assert "全部 {n} 局成功发起" not in api_source
    assert "持续打满并发对局（8 场）" not in load_doc
    assert "全端点覆盖" not in doc_index
    assert "生成赛程表" not in contest_source
    assert "查看赛程表" not in contest_source
    assert "不生成 pairings" in contest_source


def test_pencil_browser_regression_stays_bound_to_the_production_incident():
    """The browser suite must retain both the real incident and judge-backed replay."""
    source = (
        ROOT / "bzplat" / "frontend" / "e2e" / "qa-regression.spec.ts"
    ).read_text(encoding="utf-8")

    assert "20260810140318-a8752705" in source
    assert "productionBoxClick" in source
    assert "sentActions).toHaveLength(0)" in source
    assert "real Pencil human play accepts several canvas-picked edges" in source
    assert "Pencil human pass request disables the board" in source
    assert "Pencil human canvas exposes legal edges to keyboard" in source
    assert "response: { x: -1, y: -1 }" in source
    assert "event.type === 'illegal'" in source
