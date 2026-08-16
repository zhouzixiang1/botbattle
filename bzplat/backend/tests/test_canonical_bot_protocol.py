"""唯一现行 Bot 通信协议的跨游戏回归门禁。"""
from __future__ import annotations

import asyncio
import inspect
import json
from types import SimpleNamespace

import pytest

from bzplat.backend.games import _botzone_protocol as botzone
from bzplat.backend.games import preflight_bot, registry
from bzplat.backend.matches.runner import MatchRunner, _botzone_decide
from bzplat.backend.runtime.binary_runner import (
    BinaryRunner,
    BotCrashedError,
    BotProtocolError,
)
from bzplat.backend.store import Store
from bzplat.backend.store.schema import DEFAULT_RUNTIME_MODE


VALID_LINES = {
    "holdem": '{"response":0}',
    "gomoku": '{"response":{"action":"opening","white2":{"x":7,"y":8},"black3":{"x":8,"y":8},"n":2}}',
    "pencil": '{"response":{"x":0,"y":1}}',
}


class _PreflightTransport:
    def __init__(self, response_line: str, *, handshake: str | None = None) -> None:
        self.response_line = response_line
        self.handshake = handshake
        self.started: list[tuple[str, str]] = []
        self.sent: list[dict] = []
        self.send_timeouts: list[float] = []
        self.extra_reads = 0
        self.extra_timeouts: list[float] = []
        self.stopped: list[str] = []

    async def start_session(self, path, *, runtime_mode):
        sid = f"s{len(self.started)}"
        self.started.append((str(path), runtime_mode))
        return sid

    async def send(self, _sid, line, *, timeout):
        self.sent.append(json.loads(line))
        self.send_timeouts.append(timeout)
        return self.response_line

    async def read_extra_line(self, _sid, *, timeout):
        self.extra_reads += 1
        self.extra_timeouts.append(timeout)
        return self.handshake

    async def stop_session(self, sid):
        self.stopped.append(sid)


class _TimeoutPreflightTransport(_PreflightTransport):
    async def send(self, _sid, line, *, timeout):
        self.sent.append(json.loads(line))
        self.send_timeouts.append(timeout)
        raise asyncio.TimeoutError


@pytest.mark.parametrize("game_id", ["holdem", "gomoku", "pencil"])
@pytest.mark.parametrize("runtime_mode", ["traditional", "longrunning"])
def test_preflight_uses_canonical_first_turn_for_all_games_and_modes(
    game_id, runtime_mode
):
    transport = _PreflightTransport(
        VALID_LINES[game_id],
        handshake=botzone.KEEP_RUNNING_SIGNAL,
    )
    ok, detail = asyncio.run(
        preflight_bot(
            game_id,
            "/staged/bot.bin",
            transport,
            runtime_mode=runtime_mode,
        )
    )
    assert ok, detail
    assert transport.started == [("/staged/bot.bin", runtime_mode)]
    assert len(transport.sent) == 1
    assert set(transport.sent[0]) == {"requests", "responses"}
    assert len(transport.sent[0]["requests"]) == 1
    assert transport.sent[0]["responses"] == []
    assert transport.send_timeouts == [8.0]
    if game_id == "holdem":
        # 上传预检必须与正式第 1 手完全相同；max_hand 不能伪装成 1 手短局。
        from bzplat.backend.games.holdem.engine import DEFAULT_HANDS

        assert transport.sent[0]["requests"][0]["max_hand"] == DEFAULT_HANDS == 70
    elif game_id == "gomoku":
        request = transport.sent[0]["requests"][0]
        assert request["protocol_version"] == 2
        assert request["ruleset"] == "gomoku_ccgc_2013_v1"
        assert request["phase"] == "opening_proposal"
        assert request["fixed_black1"] == {"x": 7, "y": 7}
    elif game_id == "pencil":
        assert transport.sent[0]["requests"][0] == {
            "x": -1,
            "y": -1,
            "pass": 0,
            "me": 0,
            "scores": [0, 0],
        }
    assert transport.extra_reads == (1 if runtime_mode == "longrunning" else 0)
    assert transport.extra_timeouts == ([1.0] if runtime_mode == "longrunning" else [])
    assert transport.stopped == ["s0"]


def test_pencil_preflight_timeout_explains_json_and_legacy_sau_incompatibility():
    transport = _TimeoutPreflightTransport("")

    ok, detail = asyncio.run(
        preflight_bot(
            "pencil",
            "/staged/bot.bin",
            transport,
            runtime_mode="traditional",
        )
    )

    assert not ok
    assert "ELF 已在沙箱中启动" in detail
    assert "8.0s" in detail
    assert "Botzone JSON 首回合协议" in detail
    assert "requests/responses" in detail
    assert '{"response":{"x":x,"y":y}}' in detail
    assert "换行" in detail
    assert "flush" in detail
    assert "name?/new/move/take" in detail
    assert "不兼容" in detail
    assert "/wiki?slug=bot-dev" in detail
    assert transport.send_timeouts == [8.0]
    assert transport.stopped == ["s0"]


@pytest.mark.parametrize("game_id", ["holdem", "gomoku", "pencil"])
@pytest.mark.parametrize("runtime_mode", ["traditional", "longrunning"])
def test_preflight_ignores_extra_top_level_response_fields_in_every_mode(
    game_id, runtime_mode
):
    payload = json.loads(VALID_LINES[game_id])["response"]
    transport = _PreflightTransport(
        json.dumps({"response": payload, "debug": "obsolete"}),
        handshake=botzone.KEEP_RUNNING_SIGNAL,
    )
    ok, detail = asyncio.run(
        preflight_bot(
            game_id,
            "/staged/bot.bin",
            transport,
            runtime_mode=runtime_mode,
        )
    )
    assert ok, detail
    assert transport.stopped == ["s0"]


@pytest.mark.parametrize("handshake", [None, "KEEP_RUNNING", " KEEP_RUNNING "])
def test_longrunning_preflight_requires_exact_handshake(handshake):
    transport = _PreflightTransport(VALID_LINES["holdem"], handshake=handshake)
    ok, detail = asyncio.run(
        preflight_bot(
            "holdem",
            "/staged/bot.bin",
            transport,
            runtime_mode="longrunning",
        )
    )
    assert not ok
    assert "KEEP_RUNNING" in detail


class _LiveTransport:
    def __init__(self, response_line: str, handshake: str | None) -> None:
        self._sessions = {
            "live": SimpleNamespace(
                binary_path="/bot.bin",
                runtime_mode="longrunning",
                requests=[],
                responses=[],
                turn=0,
                long_running=False,
            )
        }
        self.response_line = response_line
        self.handshake = handshake

    async def send(self, _sid, _line, *, timeout):
        return self.response_line

    async def read_extra_line(self, _sid, *, timeout):
        return self.handshake


@pytest.mark.parametrize(
    ("handshake", "code"),
    [(None, "missing_keep_running"), ("wrong", "invalid_keep_running")],
)
def test_live_longrunning_never_falls_back_without_exact_handshake(handshake, code):
    transport = _LiveTransport(VALID_LINES["holdem"], handshake)
    session = transport._sessions["live"]
    with pytest.raises(BotProtocolError) as raised:
        asyncio.run(
            _botzone_decide(
                transport,
                "live",
                {"hand": 0},
                game_id="holdem",
                action_timeout=1,
                failed_seat=1,
            )
        )
    assert raised.value.error_code == code
    assert raised.value.failed_seat == 1
    assert session.turn == 0
    assert session.requests == []
    assert session.responses == []
    assert session.long_running is False


@pytest.mark.parametrize(
    ("game_id", "payload"),
    [
        ("holdem", -3),
        ("holdem", True),
        ("holdem", "0"),
        ("gomoku", {"x": 1, "y": 2, "debug": "x"}),
        ("pencil", {"x": True, "y": 2}),
        ("pencil", {"x": 1}),
    ],
)
def test_game_payload_domain_is_exact(game_id, payload):
    validate = registry.get(game_id).protocol.validate_response_payload
    with pytest.raises(ValueError):
        validate(payload)


class _TraditionalLifecycleTransport:
    """记录逻辑会话与实际进程启动，证明不存在两只整场闲置进程。"""

    def __init__(self) -> None:
        self._sessions: dict[str, SimpleNamespace] = {}
        self.prepared: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []

    def _state(self, path: str, mode: str, profile=None) -> SimpleNamespace:
        return SimpleNamespace(
            binary_path=path,
            runtime_mode=mode,
            profile=profile,
            requests=[],
            responses=[],
            turn=0,
            long_running=False,
        )

    async def prepare_session(self, path, *, runtime_mode, profile=None):
        sid = f"logical-{len(self.prepared)}"
        self.prepared.append(str(path))
        self._sessions[sid] = self._state(str(path), runtime_mode, profile)
        return sid

    async def start_session(self, path, *, runtime_mode, profile=None, **_kwargs):
        sid = f"process-{len(self.started)}"
        self.started.append(str(path))
        self._sessions[sid] = self._state(str(path), runtime_mode, profile)
        return sid

    async def send(self, _sid, _line, *, timeout):
        # v2 格式合法但指定开局越界，裁判立即结束，足以审计启动次数。
        return '{"response":{"action":"opening","white2":{"x":0,"y":0},"black3":{"x":999,"y":999},"n":2}}'

    async def stop_session(self, sid):
        self.stopped.append(sid)
        self._sessions.pop(sid, None)


def test_traditional_match_prepares_history_without_idle_processes():
    transport = _TraditionalLifecycleTransport()
    result = asyncio.run(
        MatchRunner(transport).run_binaries(
            "/bots/a.bin",
            "/bots/b.bin",
            game_id="gomoku",
            runtime_modes=("traditional", "traditional"),
        )
    )
    assert result.reason == "illegal_opening"
    assert transport.prepared == ["/bots/a.bin", "/bots/b.bin"]
    assert transport.started == ["/bots/a.bin"]
    assert len(transport.stopped) == 3  # 单次进程 + 双方逻辑会话


def test_traditional_human_match_prepares_history_without_idle_process():
    transport = _TraditionalLifecycleTransport()

    async def human_decide(_seat, _request):
        raise AssertionError("bot 先下出界坐标后，人类不应被询问")

    result = asyncio.run(
        MatchRunner(transport).run_bot_vs_human(
            "/bots/a.bin",
            bot_seat=0,
            human_decide=human_decide,
            game_id="gomoku",
            runtime_mode="traditional",
        )
    )
    assert result.reason == "illegal_opening"
    assert transport.prepared == ["/bots/a.bin"]
    assert transport.started == ["/bots/a.bin"]
    assert len(transport.stopped) == 2  # 单次进程 + 逻辑会话


class _PrepareFailureTransport(_TraditionalLifecycleTransport):
    async def prepare_session(self, path, *, runtime_mode, profile=None):
        if str(path).endswith("bad.bin"):
            raise BotCrashedError("cannot start")
        return await super().prepare_session(
            path, runtime_mode=runtime_mode, profile=profile
        )


@pytest.mark.parametrize(
    ("path_a", "path_b", "failed_seat"),
    [
        ("/bots/bad.bin", "/bots/good.bin", 0),
        ("/bots/good.bin", "/bots/bad.bin", 1),
    ],
)
def test_traditional_logical_session_start_failure_keeps_physical_seat_attribution(
    path_a, path_b, failed_seat
):
    transport = _PrepareFailureTransport()
    with pytest.raises(BotCrashedError) as failure:
        asyncio.run(
            MatchRunner(transport).run_binaries(
                path_a,
                path_b,
                game_id="gomoku",
                runtime_modes=("traditional", "traditional"),
            )
        )
    assert failure.value.crashed_seat == failed_seat


def test_platform_default_runtime_mode_is_traditional():
    assert DEFAULT_RUNTIME_MODE == "traditional"
    assert botzone.DEFAULT_RUNTIME_MODE == DEFAULT_RUNTIME_MODE
    assert (
        inspect.signature(BinaryRunner.start_session)
        .parameters["runtime_mode"]
        .default
        == DEFAULT_RUNTIME_MODE
    )


def test_sql_schema_defaults_match_platform_runtime_default(tmp_path):
    store = Store(str(tmp_path / "runtime-default.db"))
    try:
        for table in ("bots", "bot_versions"):
            cols = {
                row["name"]: row
                for row in store._conn.execute(f"PRAGMA table_info({table})")
            }
            assert cols["runtime_mode"]["dflt_value"] == "'traditional'"
    finally:
        store.close()
