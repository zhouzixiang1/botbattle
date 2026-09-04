"""Bot debug sidecar：有界采集、私有权限与公开边界回归。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.games import _botzone_protocol as botzone
from bzplat.backend.games.base import MatchResult, RoundResult
from bzplat.backend.main import create_app
from bzplat.backend.matches.bot_debug import (
    BotDebugCollector,
    MAX_DEBUG_BYTES_PER_MATCH,
    MAX_DEBUG_BYTES_PER_SEAT,
    MAX_DEBUG_ENTRIES_PER_MATCH,
    MAX_DEBUG_ENTRIES_PER_SEAT,
    MAX_DEBUG_ENTRY_BYTES,
    MAX_RESPONSE_LINE_BYTES,
    serialize_debug,
)
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import _botzone_decide
from bzplat.backend.runtime.binary_runner import (
    BinaryRunner,
    BotProtocolError,
    BotResponseLineTooLargeError,
)
from bzplat.backend.runtime.limits import PLATFORM_LOW_PROFILE
from bzplat.backend.store import Store
from bzplat.backend.tests.execution_helpers import challenge_and_start


def _entry(debug, *, seat=0, turn=1, leg=None):
    collector = BotDebugCollector()
    collector.capture(seat=seat, turn=turn, leg=leg, debug=debug)
    assert len(collector.entries) == 1
    return collector.entries[0]


def test_debug_sanitizer_bounds_unicode_controls_and_secrets():
    private_key = (
        "-----BEGIN PRIVATE KEY-----\nsecret-material\n"
        "-----END PRIVATE KEY-----"
    )
    partial_private_key = "-----BEGIN OPENSSH PRIVATE KEY-----\npartial-secret"
    value = {
        "password": "plain-password",
        "nested": {
            "api_token": "token-value",
            "message": "e\u0301\x1b[31m red\x1b[0m\u202e hidden",
            "authorization": "Bearer abc.def.ghi",
            "key_dump": private_key,
            "partial_key_dump": partial_private_key,
            "url": "https://example.test/?token=visible-secret&ok=1",
            "loose": (
                "password: colon-secret authorization: Basic dXNlcjpwYXNz "
                "access_key=assigned-secret secret=\"two word value\" "
                "credential: 'another private value' Bearer opaque:bearer/value "
                "session=\"unterminated secret value"
            ),
            "cookie_header": "Cookie: sid=alpha; csrf=beta",
            "set_cookie_header": "Set-Cookie: session=gamma; Path=/; HttpOnly",
            "cookie_assignment": "cookie=sid=delta; second=epsilon",
        },
        "deep": {"a": {"b": {"c": {"d": {"e": "too-deep"}}}}},
        "many": list(range(100)),
        "huge": "x" * 20_000,
    }

    encoded = serialize_debug(value)
    assert encoded is not None
    assert len(encoded.encode("utf-8")) <= MAX_DEBUG_ENTRY_BYTES
    public = json.loads(encoded)
    rendered = json.dumps(public, ensure_ascii=False)
    for secret in (
        "plain-password",
        "token-value",
        "secret-material",
        "visible-secret",
        "abc.def.ghi",
        "colon-secret",
        "dXNlcjpwYXNz",
        "assigned-secret",
        "two word value",
        "another private value",
        "partial-secret",
        "opaque:bearer/value",
        "unterminated secret value",
        "alpha",
        "beta",
        "gamma",
        "delta",
        "epsilon",
    ):
        assert secret not in rendered
    assert "\x1b" not in rendered
    assert "\u202e" not in rendered
    assert "é" in rendered
    assert "[REDACTED]" in rendered
    assert "too-deep" not in rendered
    assert serialize_debug(None) is None
    assert json.loads(serialize_debug(True) or "null") is True
    assert json.loads(serialize_debug(17) or "null") == 17


def test_collector_enforces_per_seat_and_match_caps_without_throwing():
    collector = BotDebugCollector()
    for seat in (0, 1):
        for turn in range(1, MAX_DEBUG_ENTRIES_PER_SEAT + 80):
            collector.capture(seat=seat, turn=turn, debug={"v": turn})

    assert len(collector.entries) == MAX_DEBUG_ENTRIES_PER_MATCH
    assert sum(e["seat"] == 0 for e in collector.entries) == MAX_DEBUG_ENTRIES_PER_SEAT
    assert sum(e["seat"] == 1 for e in collector.entries) == MAX_DEBUG_ENTRIES_PER_SEAT
    assert collector.dropped_count > 0
    assert collector.total_bytes <= MAX_DEBUG_BYTES_PER_MATCH

    bytes_limited = BotDebugCollector()
    for turn in range(1, 200):
        bytes_limited.capture(seat=0, turn=turn, debug="x" * 10_000)
    assert bytes_limited.total_bytes <= MAX_DEBUG_BYTES_PER_SEAT
    assert bytes_limited.dropped_count > 0


@pytest.mark.parametrize("saturated", ["match_count", "seat_count", "match_bytes", "seat_bytes"])
def test_collector_short_circuits_before_sanitizing_when_saturated(
    monkeypatch, saturated,
):
    collector = BotDebugCollector()
    if saturated == "match_count":
        collector.entries = [{}] * MAX_DEBUG_ENTRIES_PER_MATCH
    elif saturated == "seat_count":
        collector._seat_counts[0] = MAX_DEBUG_ENTRIES_PER_SEAT
    elif saturated == "match_bytes":
        collector._total_bytes = MAX_DEBUG_BYTES_PER_MATCH
    else:
        collector._seat_bytes[0] = MAX_DEBUG_BYTES_PER_SEAT

    calls = 0

    def should_not_serialize(_value):
        nonlocal calls
        calls += 1
        raise AssertionError("容量饱和后不得调用 sanitizer")

    monkeypatch.setattr(
        "bzplat.backend.matches.bot_debug.serialize_debug",
        should_not_serialize,
    )
    collector.capture(seat=0, turn=1, debug={"deep": ["x" * 64_000]})
    assert calls == 0
    assert collector.dropped_count == 1


class _Transport:
    def __init__(self, response: str, mode: str, handshake: str | None = None):
        self.response = response
        self.handshake = handshake
        self._sessions = {
            "logic": SimpleNamespace(
                binary_path="/bot/a",
                runtime_mode=mode,
                profile=PLATFORM_LOW_PROFILE,
                requests=[],
                responses=[],
                turn=0,
                long_running=False,
            )
        }
        self.started = 0

    async def start_session(
        self,
        path,
        *,
        runtime_mode,
        profile=PLATFORM_LOW_PROFILE,
        **_kwargs,
    ):
        sid = f"tmp-{self.started}"
        self.started += 1
        self._sessions[sid] = SimpleNamespace(
            binary_path=str(path), runtime_mode=runtime_mode,
            profile=profile,
            requests=[], responses=[], turn=0, long_running=False,
        )
        return sid

    async def send(self, _sid, _line, *, timeout):
        return self.response

    async def read_extra_line(self, _sid, *, timeout):
        return self.handshake

    async def stop_session(self, sid):
        if sid != "logic":
            self._sessions.pop(sid, None)


@pytest.mark.parametrize("mode", ["traditional", "longrunning"])
def test_traditional_and_longrunning_capture_only_debug_sidecar(mode):
    transport = _Transport(
        '{"response":0,"debug":{"branch":"call"},"data":"ignored"}',
        mode,
        handshake=botzone.KEEP_RUNNING_SIGNAL,
    )
    captured = []
    move = asyncio.run(_botzone_decide(
        transport,
        "logic",
        {"hand": 0},
        game_id="holdem",
        action_timeout=1,
        failed_seat=1,
        leg=2,
        on_debug=lambda seat, turn, leg, debug: captured.append(
            (seat, turn, leg, debug)
        ),
    ))

    assert move == {"response": 0}
    assert transport._sessions["logic"].responses == [0]
    assert captured == [(1, 1, 2, {"branch": "call"})]


def test_longrunning_handshake_failure_does_not_capture_debug():
    transport = _Transport(
        '{"response":0,"debug":"not-committed"}',
        "longrunning",
        handshake="wrong",
    )
    captured = []
    with pytest.raises(BotProtocolError):
        asyncio.run(_botzone_decide(
            transport,
            "logic",
            {},
            game_id="holdem",
            action_timeout=1,
            on_debug=lambda *args: captured.append(args),
        ))
    assert captured == []
    assert transport._sessions["logic"].responses == []


def test_response_line_hard_limit_and_preflight_discards_debug():
    too_large = '{"response":0,"debug":"' + ("x" * MAX_RESPONSE_LINE_BYTES) + '"}'
    transport = _Transport(too_large, "traditional")
    with pytest.raises(BotProtocolError) as raised:
        asyncio.run(_botzone_decide(
            transport, "logic", {}, game_id="holdem", action_timeout=1,
        ))
    assert raised.value.error_code == "response_line_too_large"

    preflight = _Transport(
        '{"response":0,"debug":{"password":"must-be-discarded"}}',
        "longrunning",
        handshake=botzone.KEEP_RUNNING_SIGNAL,
    )
    payload = asyncio.run(botzone.preflight_exchange(
        "/staged/bot",
        preflight,
        {"hand": 0},
        lambda value: value,
        runtime_mode="longrunning",
        timeout=8,
    ))
    assert payload == 0


@pytest.mark.parametrize(
    "decoder_error",
    [ValueError, RecursionError],
    ids=["value-error", "recursion-error"],
)
def test_malicious_json_decoder_failures_are_protocol_faults(
    monkeypatch, decoder_error
):
    def reject_malicious_json(_line):
        raise decoder_error("malicious JSON cannot be decoded")

    monkeypatch.setattr(botzone.json, "loads", reject_malicious_json)
    transport = _Transport('{"response":0}', "traditional")
    captured = []
    with pytest.raises(BotProtocolError) as raised:
        asyncio.run(_botzone_decide(
            transport,
            "logic",
            {},
            game_id="holdem",
            action_timeout=1,
            on_debug=lambda *args: captured.append(args),
        ))
    assert raised.value.error_code == "invalid_json"
    assert captured == []


@pytest.mark.parametrize(
    ("mode", "oversized_phase"),
    [
        ("traditional", "response"),
        ("longrunning", "response"),
        ("longrunning", "handshake"),
    ],
)
def test_transport_line_limit_is_always_an_attributable_protocol_fault(
    mode, oversized_phase,
):
    class _BoundedTransport(_Transport):
        async def send(self, sid, line, *, timeout):
            if oversized_phase == "response":
                raise BotResponseLineTooLargeError("bounded")
            return await super().send(sid, line, timeout=timeout)

        async def read_extra_line(self, sid, *, timeout):
            if oversized_phase == "handshake":
                raise BotResponseLineTooLargeError("bounded")
            return await super().read_extra_line(sid, timeout=timeout)

    transport = _BoundedTransport(
        '{"response":0,"debug":"must-not-commit"}',
        mode,
        handshake=botzone.KEEP_RUNNING_SIGNAL,
    )
    captured = []
    with pytest.raises(BotProtocolError) as raised:
        asyncio.run(_botzone_decide(
            transport,
            "logic",
            {},
            game_id="holdem",
            action_timeout=1,
            failed_seat=1,
            on_debug=lambda *args: captured.append(args),
        ))
    assert raised.value.error_code == "response_line_too_large"
    assert raised.value.failed_seat == 1
    assert captured == []


def test_binary_runner_stream_reader_enforces_the_exact_transport_boundary():
    class _Stdin:
        def write(self, _data):
            return None

        async def drain(self):
            return None

    async def read(content_size: int, *, extra: bool = False):
        stdout = asyncio.StreamReader(limit=MAX_RESPONSE_LINE_BYTES + 1)
        stdout.feed_data((b"x" * content_size) + b"\n")
        stdout.feed_eof()
        proc = SimpleNamespace(stdin=_Stdin(), stdout=stdout, stderr=None)
        runner = BinaryRunner(prefer_local=True)
        runner._sessions["bounded"] = SimpleNamespace(
            proc=proc,
            execution_scope=None,
        )
        if extra:
            return await runner.read_extra_line("bounded", timeout=1)
        return await runner.send("bounded", "{}", timeout=1)

    assert len(asyncio.run(read(MAX_RESPONSE_LINE_BYTES))) == MAX_RESPONSE_LINE_BYTES
    with pytest.raises(BotResponseLineTooLargeError):
        asyncio.run(read(MAX_RESPONSE_LINE_BYTES + 2))
    with pytest.raises(BotResponseLineTooLargeError):
        asyncio.run(read(MAX_RESPONSE_LINE_BYTES + 2, extra=True))


def _make_user(store: Store, name: str, role: str = "user"):
    return store.create_user(
        name,
        f"{name}@example.test",
        hash_password("password1"),
        role=role,
    )


def _make_bot(store: Store, owner_id: int, name: str):
    return store.create_bot(
        owner_id,
        name,
        binary_path="/tmp/fake",
        format="elf",
        os="linux",
        arch="amd64",
        game_id="holdem",
    )


def _terminal_match(
    store: Store,
    match_id: str,
    bot_a: int,
    bot_b: int,
    *,
    contest_id: int | None = None,
    match_type: str = "challenge",
):
    store.create_match(
        match_id,
        bot_a,
        bot_b,
        contest_id=contest_id,
        match_type=match_type,
        game_id="holdem",
    )
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        reason="completed",
        result={"rounds_played": 1, "deltas": [1, -1], "normalized_delta": 0.01},
    )


def test_store_permissions_contest_delay_human_and_deletion(tmp_path):
    store = Store(str(tmp_path / "debug.db"))
    owner_a = _make_user(store, "debug_a")
    owner_b = _make_user(store, "debug_b")
    stranger = _make_user(store, "debug_x")
    organizer = _make_user(store, "debug_org", "organizer")
    admin = _make_user(store, "debug_admin", "admin")
    bot_a = _make_bot(store, owner_a["id"], "a")
    bot_b = _make_bot(store, owner_b["id"], "b")
    entries = [
        _entry({"seat": "a"}, seat=0, turn=1),
        _entry({"seat": "b"}, seat=1, turn=1),
    ]

    _terminal_match(store, "ordinary", bot_a["id"], bot_b["id"])
    assert store.replace_match_debug("ordinary", entries)
    for owner in (owner_a, owner_b):
        result = store.get_match_debug_for_user(
            "ordinary", user_id=owner["id"], is_admin=False,
        )
        assert result["allowed"] and len(result["entries"]) == 2
    assert not store.get_match_debug_for_user(
        "ordinary", user_id=stranger["id"], is_admin=False,
    )["allowed"]

    contest = store.create_contest(
        "私有调试赛事", organizer_id=organizer["id"], game_id="holdem"
    )
    store.update_contest(contest["id"], status="running")
    _terminal_match(
        store,
        "contest-match",
        bot_a["id"],
        bot_b["id"],
        contest_id=contest["id"],
        match_type="contest",
    )
    assert store.replace_match_debug("contest-match", entries)
    assert store.get_match_debug_for_user(
        "contest-match", user_id=organizer["id"], is_admin=False,
    )["allowed"]
    assert store.get_match_debug_for_user(
        "contest-match", user_id=admin["id"], is_admin=True,
    )["allowed"]
    assert not store.get_match_debug_for_user(
        "contest-match", user_id=owner_a["id"], is_admin=False,
    )["allowed"]
    # This permission test needs only a historical terminal status; it does not
    # exercise lifecycle advancement or official results.  Seed that imported
    # read-only shape explicitly instead of bypassing the production
    # decision/finish transaction through generic ``update_contest``.
    with store._tx() as connection:
        connection.execute("BEGIN IMMEDIATE")
        updated = connection.execute(
            "UPDATE contests SET status='finished' WHERE id=? AND status='running'",
            (contest["id"],),
        )
        assert updated.rowcount == 1
    assert store.get_match_debug_for_user(
        "contest-match", user_id=owner_a["id"], is_admin=False,
    )["allowed"]

    cancelled = store.create_contest(
        "已取消私有调试赛事", organizer_id=organizer["id"], game_id="holdem"
    )
    store.update_contest(cancelled["id"], status="cancelled")
    _terminal_match(
        store,
        "cancelled-contest-match",
        bot_a["id"],
        bot_b["id"],
        contest_id=cancelled["id"],
        match_type="contest",
    )
    assert store.replace_match_debug("cancelled-contest-match", entries)
    assert store.get_match_debug_for_user(
        "cancelled-contest-match", user_id=owner_b["id"], is_admin=False,
    )["allowed"]

    orphaned = store.create_contest(
        "将删除的赛事", organizer_id=organizer["id"], game_id="holdem"
    )
    _terminal_match(
        store,
        "orphaned-contest-match",
        bot_a["id"],
        bot_b["id"],
        contest_id=orphaned["id"],
        match_type="contest",
    )
    assert store.replace_match_debug("orphaned-contest-match", entries)
    assert store.delete_contest(orphaned["id"])
    assert store.get_match("orphaned-contest-match")["contest_id"] is None
    assert not store.get_match_debug_for_user(
        "orphaned-contest-match", user_id=owner_a["id"], is_admin=False,
    )["allowed"]
    assert store.get_match_debug_for_user(
        "orphaned-contest-match", user_id=admin["id"], is_admin=True,
    )["allowed"]

    store.create_match(
        "active", bot_a["id"], bot_a["id"], game_id="holdem"
    )
    assert not store.replace_match_debug("active", entries)
    assert not store.get_match_debug_for_user(
        "active", user_id=admin["id"], is_admin=True,
    )["allowed"]

    _terminal_match(
        store, "human", bot_a["id"], bot_a["id"], match_type="human"
    )
    assert not store.replace_match_debug("human", entries)
    assert not store.get_match_debug_for_user(
        "human", user_id=owner_a["id"], is_admin=False,
    )["allowed"]
    assert store.get_match_debug_for_user(
        "human", user_id=admin["id"], is_admin=True,
    )["allowed"]

    # 管理端安全删除必须保留历史身份；底层 delete_user 仍单独验证 FK 清理，
    # 但不属于公开/管理端允许调用的业务路径。
    owner_c = _make_user(store, "debug_c")
    bot_c = _make_bot(store, owner_c["id"], "c")
    _terminal_match(store, "user-delete", bot_c["id"], bot_c["id"])
    assert store.replace_match_debug(
        "user-delete", [_entry({"owner": "deleted"}, seat=0)]
    )
    deletion = store.delete_user_if_safe(owner_c["id"])
    assert deletion["deleted"] is False
    assert deletion["blockers"]["matches"] == 1
    assert store.get_user(owner_c["id"]) is not None
    assert store.delete_user(owner_c["id"])
    assert store._conn.execute(
        "SELECT bot_id FROM match_debug_entries "
        "WHERE match_id='user-delete' AND seat=0"
    ).fetchone()[0] is None

    # Bot 删除经 FK SET NULL，不留悬空 bot_id；对局删除经 matches_index FK
    # 级联清理 session + entries。
    assert store.delete_bot(bot_a["id"])
    assert store._conn.execute(
        "SELECT bot_id FROM match_debug_entries "
        "WHERE match_id='ordinary' AND seat=0"
    ).fetchone()[0] is None
    with pytest.raises(ValueError, match="评分审计证据"):
        store.delete_match("ordinary")

    # Rating-bearing rows retain their durable audit source.  Exercise debug
    # cascade deletion on a neutral self-play row, which remains deletable.
    store.create_match("deletable", bot_b["id"], bot_b["id"], game_id="holdem")
    store.update_match("deletable", status="aborted", reason="test_fixture")
    assert store.replace_match_debug("deletable", entries)
    assert store.delete_match("deletable")
    assert store._conn.execute(
        "SELECT COUNT(*) FROM match_debug_sessions WHERE match_id='deletable'"
    ).fetchone()[0] == 0
    assert store._conn.execute(
        "SELECT COUNT(*) FROM match_debug_entries WHERE match_id='deletable'"
    ).fetchone()[0] == 0
    store.close()

    # 二次打开幂等，FK 完整。
    reopened = Store(str(tmp_path / "debug.db"))
    assert reopened._conn.execute("PRAGMA foreign_key_check").fetchall() == []
    reopened.close()


def test_store_recomputes_debug_sizes_and_rejects_bypassed_limits(tmp_path):
    store = Store(str(tmp_path / "debug-limits.db"))
    owner = _make_user(store, "limit_owner")
    bot = _make_bot(store, owner["id"], "limit-bot")
    _terminal_match(store, "limit-match", bot["id"], bot["id"])

    forged = {
        "seat": 0,
        "turn": 1,
        "leg": -1,
        "debug_json": json.dumps("x" * (MAX_DEBUG_ENTRY_BYTES + 1)),
        "size_bytes": 1,
    }
    with pytest.raises(ValueError, match="单条容量"):
        store.replace_match_debug("limit-match", [forged])

    per_seat = [
        {
            "seat": 0,
            "turn": turn,
            "leg": -1,
            "debug_json": "true",
            "size_bytes": 4,
        }
        for turn in range(1, MAX_DEBUG_ENTRIES_PER_SEAT + 2)
    ]
    with pytest.raises(ValueError, match="单座位"):
        store.replace_match_debug("limit-match", per_seat)
    assert store._conn.execute(
        "SELECT COUNT(*) FROM match_debug_sessions WHERE match_id='limit-match'"
    ).fetchone()[0] == 0
    store.close()


class _DebugResultRunner:
    def __init__(self, *, fail_persist: bool = False):
        self.fail_persist = fail_persist

    async def run_binaries(self, *_args, **kwargs):
        kwargs["on_debug"](0, 1, None, {"branch": "safe-sidecar"})
        kwargs["on_debug"](1, 1, None, "opponent-note")
        return MatchResult(
            rounds_played=1,
            rounds=[RoundResult(winners=[0], deltas=[1, -1])],
            events=[],
        )


def _versioned_bot(store: Store, user_name: str, root: Path):
    user = _make_user(store, user_name)
    path = root / f"{user_name}.bin"
    path.write_bytes(b"fixture")
    bot = store.create_bot(
        user["id"], f"{user_name}-bot", binary_path=str(path),
        format="elf", os="linux", arch="amd64", game_id="holdem",
    )
    version = store.add_bot_version(
        bot["id"], binary_path=str(path), version=1,
        format="elf", os="linux", arch="amd64", runtime_mode="traditional",
    )
    store.set_current_version(bot["id"], version["version"])
    store.ensure_rating(bot["id"], game_id="holdem")
    return user, bot


def test_orchestrator_batches_debug_and_persistence_failure_never_changes_result(
    tmp_path, monkeypatch, caplog,
):
    store = Store(str(tmp_path / "orchestrator.db"))
    owner, bot_a = _versioned_bot(store, "orcha", tmp_path)
    _, bot_b = _versioned_bot(store, "orchb", tmp_path)
    orch = MatchOrchestrator(store, runner=_DebugResultRunner(), max_concurrent=1)
    terminal_persistence = []
    original_broadcast = orch._broadcast

    def observing_broadcast(match_id, event):
        if event.get("type") in {"match_end", "error"}:
            row = store._conn.execute(
                "SELECT entry_count FROM match_debug_sessions WHERE match_id=?",
                (match_id,),
            ).fetchone()
            terminal_persistence.append(None if row is None else int(row[0]))
        original_broadcast(match_id, event)

    monkeypatch.setattr(orch, "_broadcast", observing_broadcast)

    async def run_one():
        match_id = await challenge_and_start(
            orch,
            bot_a["id"], bot_b["id"], owner["id"], game_id="holdem"
        )
        await orch._tasks[match_id]
        assert store.executions.finalize_ready() == 1
        return match_id

    match_id = asyncio.run(run_one())
    assert store.get_match(match_id)["status"] == "completed"
    private = store.get_match_debug_for_user(
        match_id, user_id=owner["id"], is_admin=False,
    )
    assert [entry["debug"] for entry in private["entries"]] == [
        {"branch": "safe-sidecar"},
        "opponent-note",
    ]
    assert terminal_persistence[-1] == 2
    assert "safe-sidecar" not in json.dumps(store.get_match(match_id))
    assert "safe-sidecar" not in (store.get_replay(match_id)["events_json"] or "")

    original = store.replace_match_debug
    monkeypatch.setattr(
        store,
        "replace_match_debug",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("db unavailable safe-sidecar")
        ),
    )
    second = asyncio.run(run_one())
    assert store.get_match(second)["status"] == "completed"
    assert "safe-sidecar" not in caplog.text
    monkeypatch.setattr(store, "replace_match_debug", original)
    store.close()


def test_private_api_no_store_audit_and_public_boundaries(tmp_path, monkeypatch):
    app = create_app(db_path=str(tmp_path / "api.db"))
    store = app.state.store
    owner_a = _make_user(store, "apia")
    owner_b = _make_user(store, "apib")
    stranger = _make_user(store, "apix")
    for user in (owner_a, owner_b, stranger):
        store.update_user(user["id"], email_verified=1)
    bot_a = _make_bot(store, owner_a["id"], "api-a")
    bot_b = _make_bot(store, owner_b["id"], "api-b")
    _terminal_match(store, "api-match", bot_a["id"], bot_b["id"])
    store.upsert_replay(
        "api-match",
        json.dumps([{"type": "match_end", "winner": 0, "reason": "completed", "deltas": [1, -1]}]),
    )
    assert store.replace_match_debug(
        "api-match", [_entry({"branch": "river-safe"}, seat=0)]
    )
    _, owner_token = app.state.auth.authenticate("apia", "password1")
    _, stranger_token = app.state.auth.authenticate("apix", "password1")
    audits = []
    monkeypatch.setattr(
        "bzplat.backend.api_routes.audit_log",
        lambda _request, action, **kwargs: audits.append((action, kwargs)),
    )
    client = TestClient(app)

    guest = client.get("/api/matches/api-match")
    assert guest.status_code == 200
    assert guest.json()["match"]["can_view_debug"] is False
    assert "river-safe" not in guest.text
    public_replay = client.get("/api/matches/api-match/replay")
    assert public_replay.status_code == 200
    assert set(public_replay.json()) == {
        "match_id", "events", "event_count", "updated_at",
    }
    assert "river-safe" not in public_replay.text
    public_list = client.get("/api/matches")
    public_search = client.get(
        "/api/search", params={"type": "matches", "q": "api-match"}
    )
    assert public_list.status_code == public_search.status_code == 200
    for public_response in (public_list, public_search, public_replay):
        assert "river-safe" not in public_response.text
        assert "can_view_debug" not in public_response.text

    unauthenticated = client.get("/api/matches/api-match/debug")
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["cache-control"] == "private, no-store, max-age=0"
    assert unauthenticated.headers["pragma"] == "no-cache"
    assert "river-safe" not in unauthenticated.text
    assert audits[-1][1]["detail"] == "unauthenticated"

    owner_headers = {"Authorization": f"Bearer {owner_token}"}
    detail = client.get("/api/matches/api-match", headers=owner_headers)
    assert detail.json()["match"]["can_view_debug"] is True
    assert "authorization" in detail.headers["vary"].lower()
    assert "cookie" in detail.headers["vary"].lower()
    assert "river-safe" not in detail.text
    response = client.get("/api/matches/api-match/debug", headers=owner_headers)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, no-store, max-age=0"
    assert response.headers["pragma"] == "no-cache"
    assert response.json()["entries"][0]["debug"] == {"branch": "river-safe"}
    assert audits[-1][0] == "match_debug_read"
    assert "river-safe" not in json.dumps(audits[-1], ensure_ascii=False)

    denied = client.get(
        "/api/matches/api-match/debug",
        headers={"Authorization": f"Bearer {stranger_token}"},
    )
    assert denied.status_code == 403
    assert "river-safe" not in denied.text
    assert denied.headers["cache-control"] == "private, no-store, max-age=0"
    assert denied.headers["pragma"] == "no-cache"

    snapshot = app.state.orch.subscribe("api-match").get_nowait()
    assert snapshot["type"] == "snapshot"
    assert "river-safe" not in json.dumps(snapshot, ensure_ascii=False)
    assert "river-safe" not in client.get("/api/matches/api-match/events").text
