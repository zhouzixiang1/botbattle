from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[3]


def load_client():
    name = "qa_script_local_ai_client"
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "scripts" / "local_ai_client.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def turn_payload(**updates):
    payload = {
        "type": "turn",
        "request_id": "req-1",
        "match_id": "match-1",
        "turn": 3,
        "seat": 1,
        "input_line": '{"requests":[{}],"responses":[]}',
        "timeout_ms": 1000,
    }
    payload.update(updates)
    return payload


def prepare_payload(**updates):
    payload = {
        "type": "prepare_turn",
        "request_id": "req-1",
        "match_id": "match-1",
        "turn": 3,
        "seat": 1,
        "prepare_timeout_ms": 8000,
    }
    payload.update(updates)
    return payload


def test_local_ai_client_has_no_cli_or_url_token_surface():
    module = load_client()
    parser = module.build_parser()
    args = parser.parse_args(
        ["--url", "wss://bot.example/api/local-ai/connect", "--command", "./bot"]
    )

    assert vars(args) == {
        "url": "wss://bot.example/api/local-ai/connect",
        "command": ["./bot"],
    }
    with pytest.raises(SystemExit):
        parser.parse_args(
            [
                "--url",
                "wss://bot.example/api/local-ai/connect",
                "--token",
                "secret",
                "--command",
                "./bot",
            ]
        )
    with pytest.raises(module.ClientConfigError):
        module.validate_server_url(
            "wss://bot.example/api/local-ai/connect?token=secret"
        )
    with pytest.raises(module.ClientConfigError):
        module.validate_server_url("ws://bot.example/api/local-ai/connect")
    with pytest.raises(module.ClientConfigError):
        module.validate_server_url("wss://user:secret@bot.example/local-ai")
    with pytest.raises(module.ClientConfigError):
        module.validate_server_url(" wss://bot.example/local-ai")

    source = (ROOT / "scripts" / "local_ai_client.py").read_text(encoding="utf-8")
    assert '"--token"' not in source
    assert "Authorization" in source


def test_local_ai_token_is_read_only_from_the_named_environment():
    module = load_client()
    token = "local_" + "a" * 32
    assert module.read_token({module.TOKEN_ENV: token}) == token
    for environment in ({}, {module.TOKEN_ENV: " short "}, {module.TOKEN_ENV: "x\ny"}):
        with pytest.raises(module.ClientConfigError):
            module.read_token(environment)


def test_turn_and_response_contract_are_small_and_explicit():
    module = load_client()
    turn = module.parse_turn(turn_payload())
    assert module.response_message(turn, '{"response":0}') == {
        "type": "response",
        "request_id": "req-1",
        "match_id": "match-1",
        "turn": 3,
        "output": '{"response":0}',
    }
    secret = "local_" + "s" * 32
    assert secret not in json.dumps(module.response_message(turn, "ok"))
    assert module.failure_message(turn, "bot_start_failed") == {
        "type": "failure",
        "request_id": "req-1",
        "match_id": "match-1",
        "turn": 3,
        "reason": "bot_start_failed",
    }
    with pytest.raises(ValueError, match="未知"):
        module.failure_message(turn, "private:/home/user/bot")

    preparation = module.parse_prepare_turn(prepare_payload())
    assert module.prepared_message(preparation) == {
        "type": "prepared",
        "request_id": "req-1",
        "match_id": "match-1",
        "turn": 3,
    }
    for malformed in (
        prepare_payload(input_line="must-not-be-present"),
        prepare_payload(prepare_timeout_ms=0),
        prepare_payload(seat=True),
    ):
        with pytest.raises(module.TurnError):
            module.parse_prepare_turn(malformed)

    for updates in (
        {"turn": True},
        {"timeout_ms": 0},
        {"input_line": "one\ntwo"},
        {"request_id": ""},
    ):
        with pytest.raises(module.TurnError):
            module.parse_turn(turn_payload(**updates))


def test_traditional_turn_writes_one_line_and_reads_only_first_line():
    module = load_client()

    async def scenario():
        turn = module.parse_turn(turn_payload())
        command = (
            sys.executable,
            "-c",
            "import sys; line=sys.stdin.readline().strip(); print(line, flush=True); print('ignored')",
        )
        return await module.run_traditional_turn(command, turn)

    assert asyncio.run(scenario()) == turn_payload()["input_line"]


def test_connector_token_is_not_inherited_by_the_bot_process(monkeypatch):
    module = load_client()
    monkeypatch.setenv(module.TOKEN_ENV, "local_" + "s" * 32)

    async def scenario():
        turn = module.parse_turn(turn_payload())
        return await module.run_traditional_turn(
            (
                sys.executable,
                "-c",
                f"import os; print(os.environ.get('{module.TOKEN_ENV}', 'missing'), flush=True)",
            ),
            turn,
        )

    assert asyncio.run(scenario()) == "missing"


def test_traditional_turn_timeout_and_output_limit_are_enforced():
    module = load_client()

    async def timeout_scenario():
        # A Bot that never reads stdin must not evade the same turn deadline by
        # filling the OS pipe before stdout waiting begins.
        turn = module.parse_turn(
            turn_payload(timeout_ms=50, input_line="x" * module.MAX_INPUT_BYTES)
        )
        await module.run_traditional_turn(
            (sys.executable, "-c", "import time; time.sleep(2)"), turn
        )

    async def oversized_scenario():
        turn = module.parse_turn(turn_payload())
        await module.run_traditional_turn(
            (
                sys.executable,
                "-c",
                f"import sys; sys.stdout.write('x'*({module.MAX_OUTPUT_BYTES}+1)); sys.stdout.flush()",
            ),
            turn,
        )

    with pytest.raises(module.TurnError, match="超时") as timed_out:
        asyncio.run(timeout_scenario())
    assert timed_out.value.reason == "bot_decision_timeout"
    with pytest.raises(module.TurnError, match="64 KiB") as oversized:
        asyncio.run(oversized_scenario())
    assert oversized.value.reason == "bot_output_too_large"


def test_two_phase_client_excludes_process_startup_from_decision_timeout(monkeypatch):
    module = load_client()
    preparation = module.parse_prepare_turn(prepare_payload(prepare_timeout_ms=100))
    turn = module.parse_turn(turn_payload(timeout_ms=10))
    process = object()
    stopped: list[object] = []

    async def slow_spawn(_command):
        await asyncio.sleep(0.03)
        return process

    async def immediate_response(actual_process, actual_turn):
        assert actual_process is process
        assert actual_turn is turn
        return '{"response":0}'

    async def stop(actual_process):
        stopped.append(actual_process)

    monkeypatch.setattr(module, "_spawn_traditional_process", slow_spawn)
    monkeypatch.setattr(module, "_communicate_traditional_process", immediate_response)
    monkeypatch.setattr(module, "_stop_process", stop)

    async def scenario():
        prepared = await module.prepare_traditional_process(("./bot",), preparation)
        return await module.run_prepared_traditional_turn(prepared, turn)

    assert asyncio.run(scenario()) == '{"response":0}'
    assert stopped == [process]


def test_response_is_relayed_before_slow_process_teardown(monkeypatch):
    module = load_client()
    turn = module.parse_turn(turn_payload(timeout_ms=20))
    process = object()
    events: list[str] = []

    async def immediate_response(actual_process, actual_turn):
        assert actual_process is process
        assert actual_turn is turn
        events.append("output")
        return '{"response":0}'

    async def slow_stop(actual_process):
        assert actual_process is process
        events.append("cleanup_started")
        await asyncio.sleep(0.05)
        events.append("cleanup_finished")

    async def relay(output):
        assert output == '{"response":0}'
        events.append("relayed")

    monkeypatch.setattr(module, "_communicate_traditional_process", immediate_response)
    monkeypatch.setattr(module, "_stop_process", slow_stop)

    async def scenario():
        return await module.run_prepared_traditional_turn(
            process, turn, on_output=relay
        )

    assert asyncio.run(scenario()) == '{"response":0}'
    assert events == ["output", "relayed", "cleanup_started", "cleanup_finished"]


def test_failure_is_relayed_before_slow_process_teardown(monkeypatch):
    module = load_client()
    turn = module.parse_turn(turn_payload(timeout_ms=20))
    process = object()
    events: list[str] = []

    async def invalid_response(actual_process, actual_turn):
        assert actual_process is process
        assert actual_turn is turn
        events.append("invalid_output")
        raise module.TurnError("无效输出", reason="bot_output_invalid")

    async def slow_stop(actual_process):
        assert actual_process is process
        events.append("cleanup_started")
        await asyncio.sleep(0.05)
        events.append("cleanup_finished")

    async def relay(exc):
        assert exc.reason == "bot_output_invalid"
        events.append("failure_relayed")

    monkeypatch.setattr(module, "_communicate_traditional_process", invalid_response)
    monkeypatch.setattr(module, "_stop_process", slow_stop)

    async def scenario():
        await module.run_prepared_traditional_turn(
            process, turn, on_failure=relay
        )

    with pytest.raises(module.TurnError) as failed:
        asyncio.run(scenario())
    assert failed.value.reason == "bot_output_invalid"
    assert events == [
        "invalid_output",
        "failure_relayed",
        "cleanup_started",
        "cleanup_finished",
    ]


def test_transport_close_during_post_response_teardown_cannot_orphan_process():
    module = load_client()

    async def scenario():
        turn = module.parse_turn(turn_payload(timeout_ms=1000))
        process = await module._spawn_traditional_process(
            (
                sys.executable,
                "-c",
                "import signal,sys,time; "
                "signal.signal(signal.SIGTERM, lambda *_: None); "
                "sys.stdin.readline(); print('{\"response\":0}', flush=True); "
                "time.sleep(30)",
            )
        )
        relayed = asyncio.Event()

        class ClosingAfterRelay:
            async def wait_closed(self):
                await relayed.wait()

        async def relay(output):
            assert output == '{"response":0}'
            relayed.set()

        with pytest.raises(module.ConnectionLostDuringTurn):
            await module.run_prepared_turn_while_connected(
                ClosingAfterRelay(), process, turn, on_output=relay
            )
        assert process.returncode is not None

    asyncio.run(scenario())


def test_repeated_outer_cancel_during_teardown_cannot_orphan_process():
    module = load_client()

    async def scenario():
        turn = module.parse_turn(turn_payload(timeout_ms=1000))
        process = await module._spawn_traditional_process(
            (
                sys.executable,
                "-c",
                "import signal,sys,time; "
                "signal.signal(signal.SIGTERM, lambda *_: None); "
                "sys.stdin.readline(); print('{\"response\":0}', flush=True); "
                "time.sleep(30)",
            )
        )
        relayed = asyncio.Event()

        class ClosingAfterRelay:
            async def wait_closed(self):
                await relayed.wait()

        async def relay(output):
            assert output == '{"response":0}'
            relayed.set()

        wrapper = asyncio.create_task(
            module.run_prepared_turn_while_connected(
                ClosingAfterRelay(), process, turn, on_output=relay
            )
        )
        await relayed.wait()
        # The connection-close path has already cancelled the turn task while
        # it owns the 1s TERM grace period.  Simulate a second admin/shutdown
        # cancellation of the wrapper during that exact window.
        await asyncio.sleep(0.05)
        wrapper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await wrapper
        assert process.returncode is not None

    asyncio.run(scenario())


def test_traditional_turn_reports_start_and_empty_output_categories():
    module = load_client()
    turn = module.parse_turn(turn_payload())

    with pytest.raises(module.TurnError) as missing_command:
        asyncio.run(module.run_traditional_turn((), turn))
    assert missing_command.value.reason == "bot_start_failed"

    with pytest.raises(module.TurnError) as missing_executable:
        asyncio.run(
            module.run_traditional_turn(
                ("/definitely/not/a/real/local-bot-executable",), turn
            )
        )
    assert missing_executable.value.reason == "bot_start_failed"

    with pytest.raises(module.TurnError) as no_output:
        asyncio.run(
            module.run_traditional_turn(
                (sys.executable, "-c", "import sys; sys.stdin.readline()"), turn
            )
        )
    assert no_output.value.reason == "bot_no_response"


def test_reconnect_delay_is_exponential_and_capped():
    module = load_client()
    assert module.reconnect_delays(8) == (1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0, 30.0)


def test_connection_rejects_redirects_across_supported_websockets_apis():
    module = load_client()
    token = "local_" + "r" * 32

    class ModernConnect:
        def __init__(self, uri, **kwargs):
            self.uri = uri
            self.kwargs = kwargs

        def process_redirect(self, exc):
            return "wss://attacker.example/connect"

    modern = module._open_connection(
        SimpleNamespace(connect=ModernConnect, __version__="17.0"),
        "wss://bot.example/api/local-ai/connect",
        token,
    )
    marker = RuntimeError("302 with attacker Location")
    assert modern.process_redirect(marker) is marker
    assert modern.kwargs["additional_headers"] == {
        "Authorization": f"Bearer {token}"
    }
    assert modern.kwargs["subprotocols"] == [
        module.LOCAL_AI_WEBSOCKET_SUBPROTOCOL
    ]

    class LegacyConnect:
        def __init__(self, uri, **kwargs):
            self.uri = uri
            self.kwargs = kwargs

        def handle_redirect(self, uri):
            self.uri = uri

    legacy = module._open_connection(
        SimpleNamespace(connect=LegacyConnect, __version__="10.4"),
        "wss://bot.example/api/local-ai/connect",
        token,
    )
    with pytest.raises(module.RedirectRejected, match="不允许重定向"):
        legacy.handle_redirect("wss://attacker.example/connect")
    assert legacy.kwargs["extra_headers"] == {
        "Authorization": f"Bearer {token}"
    }
    assert legacy.kwargs["subprotocols"] == [
        module.LOCAL_AI_WEBSOCKET_SUBPROTOCOL
    ]

    with pytest.raises(module.ClientConfigError, match="重定向策略"):
        module._redirect_rejecting_connect(
            SimpleNamespace(connect=lambda *_args, **_kwargs: None)
        )


def test_connection_replies_to_ping_and_relays_one_turn(monkeypatch):
    module = load_client()
    sent: list[dict] = []

    class FakeWebSocket:
        def __init__(self):
            self.messages = iter(
                [
                    json.dumps({"type": "ready"}),
                    json.dumps({"type": "ping"}),
                    json.dumps({"type": "pong"}),
                    json.dumps(turn_payload(seat=1)),
                ]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

        async def send(self, raw):
            sent.append(json.loads(raw))

    async def fake_run(_websocket, _command, _turn, *, on_output, on_failure):
        assert on_failure is not None
        await on_output('{"response":0}')
        return '{"response":0}'

    monkeypatch.setattr(module, "run_turn_while_connected", fake_run)
    asyncio.run(module.handle_connection(FakeWebSocket(), ("./bot",)))

    assert sent == [
        {"type": "pong"},
        {
            "type": "response",
            "request_id": "req-1",
            "match_id": "match-1",
            "turn": 3,
            "output": '{"response":0}',
        },
    ]


def test_connection_uses_position_free_prepare_then_prepared_process(monkeypatch):
    module = load_client()
    sent: list[dict] = []
    process = object()

    class FakeWebSocket:
        def __init__(self):
            self.messages = iter(
                [json.dumps(prepare_payload()), json.dumps(turn_payload())]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

        async def send(self, raw):
            sent.append(json.loads(raw))

    async def fake_prepare(_command, preparation):
        assert preparation.request_id == "req-1"
        return process

    async def fake_run(
        _websocket, actual_process, turn, *, on_output, on_failure
    ):
        assert actual_process is process
        assert turn.input_line == turn_payload()["input_line"]
        assert on_failure is not None
        await on_output('{"response":0}')
        return '{"response":0}'

    monkeypatch.setattr(module, "prepare_traditional_process", fake_prepare)
    monkeypatch.setattr(module, "run_prepared_turn_while_connected", fake_run)
    asyncio.run(module.handle_connection(FakeWebSocket(), ("./bot",)))

    assert sent == [
        {
            "type": "prepared",
            "request_id": "req-1",
            "match_id": "match-1",
            "turn": 3,
        },
        {
            "type": "response",
            "request_id": "req-1",
            "match_id": "match-1",
            "turn": 3,
            "output": '{"response":0}',
        },
    ]


def test_invalid_decision_frame_cleans_already_prepared_process(monkeypatch):
    module = load_client()
    sent: list[dict] = []
    stopped: list[object] = []
    process = object()

    class FakeWebSocket:
        def __init__(self):
            self.messages = iter(
                [
                    json.dumps(prepare_payload()),
                    json.dumps(turn_payload(input_line="malformed\nline")),
                ]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

        async def send(self, raw):
            sent.append(json.loads(raw))

    async def fake_prepare(_command, _preparation):
        return process

    async def stop(actual_process):
        stopped.append(actual_process)

    monkeypatch.setattr(module, "prepare_traditional_process", fake_prepare)
    monkeypatch.setattr(module, "_stop_process", stop)
    asyncio.run(module.handle_connection(FakeWebSocket(), ("./bot",)))

    assert sent == [
        {
            "type": "prepared",
            "request_id": "req-1",
            "match_id": "match-1",
            "turn": 3,
        }
    ]
    assert stopped == [process]


def test_connection_reports_only_bound_failure_category(monkeypatch):
    module = load_client()
    sent: list[dict] = []

    class FakeWebSocket:
        def __init__(self):
            self.messages = iter([json.dumps(turn_payload())])

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

        async def send(self, raw):
            sent.append(json.loads(raw))

    async def failed_run(_command, _turn, *, on_output, on_failure):
        assert on_output is not None
        error = module.TurnError(
            "private /home/student/bot stderr contents",
            reason="bot_start_failed",
        )
        await on_failure(error)
        raise error

    monkeypatch.setattr(module, "run_traditional_turn", failed_run)
    asyncio.run(module.handle_connection(FakeWebSocket(), ("./bot",)))

    assert sent == [
        {
            "type": "failure",
            "request_id": "req-1",
            "match_id": "match-1",
            "turn": 3,
            "reason": "bot_start_failed",
        }
    ]
    assert "private" not in json.dumps(sent)
    assert "/home/student" not in json.dumps(sent)


def test_connection_reports_real_start_empty_and_oversized_failures():
    module = load_client()

    class FakeWebSocket:
        def __init__(self, payload):
            self.messages = iter([json.dumps(payload)])
            self.sent: list[dict] = []

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self.messages)
            except StopIteration:
                raise StopAsyncIteration

        async def send(self, raw):
            self.sent.append(json.loads(raw))

    async def scenario():
        cases = (
            ((), "bot_start_failed"),
            (("/definitely/not/a/real/local-bot-executable",), "bot_start_failed"),
            (
                (sys.executable, "-c", "import sys; sys.stdin.readline()"),
                "bot_no_response",
            ),
            (
                (
                    sys.executable,
                    "-c",
                    f"import sys; sys.stdout.write('x'*({module.MAX_OUTPUT_BYTES}+1)); sys.stdout.flush()",
                ),
                "bot_output_too_large",
            ),
        )
        for command, reason in cases:
            websocket = FakeWebSocket(turn_payload())
            await module.handle_connection(websocket, command)
            assert websocket.sent == [
                {
                    "type": "failure",
                    "request_id": "req-1",
                    "match_id": "match-1",
                    "turn": 3,
                    "reason": reason,
                }
            ]

    asyncio.run(scenario())


def test_in_flight_disconnect_stops_the_real_bot_process(tmp_path):
    module = load_client()
    pid_file = tmp_path / "local-bot.pid"

    class ClosingWebSocket:
        async def wait_closed(self):
            for _ in range(200):
                if pid_file.exists():
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("Bot process did not start")

    async def scenario():
        turn = module.parse_turn(turn_payload())
        with pytest.raises(module.ConnectionLostDuringTurn):
            await asyncio.wait_for(
                module.run_turn_while_connected(
                    ClosingWebSocket(),
                    (
                        sys.executable,
                        "-c",
                        "import os,pathlib,sys,time; "
                        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
                        "sys.stdin.readline(); time.sleep(60)",
                        str(pid_file),
                    ),
                    turn,
                ),
                timeout=3,
            )
        pid = int(pid_file.read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)

    asyncio.run(scenario())


def test_completed_turn_cancels_connection_waiter(monkeypatch):
    module = load_client()
    closed_wait_cancelled = asyncio.Event()

    class OpenWebSocket:
        async def wait_closed(self):
            try:
                await asyncio.Future()
            finally:
                closed_wait_cancelled.set()

    async def completed_run(_command, _turn):
        await asyncio.sleep(0)
        return '{"response":0}'

    async def scenario():
        monkeypatch.setattr(module, "run_traditional_turn", completed_run)
        turn = module.parse_turn(turn_payload())
        assert await module.run_turn_while_connected(
            OpenWebSocket(), ("./bot",), turn
        ) == '{"response":0}'
        assert closed_wait_cancelled.is_set()

    asyncio.run(scenario())


def test_cancelling_turn_race_cleans_up_both_tasks(monkeypatch):
    module = load_client()
    bot_started = asyncio.Event()
    bot_cancelled = asyncio.Event()
    closed_wait_cancelled = asyncio.Event()

    class OpenWebSocket:
        async def wait_closed(self):
            try:
                await asyncio.Future()
            finally:
                closed_wait_cancelled.set()

    async def blocked_run(_command, _turn):
        bot_started.set()
        try:
            await asyncio.Future()
        finally:
            bot_cancelled.set()

    async def scenario():
        monkeypatch.setattr(module, "run_traditional_turn", blocked_run)
        turn = module.parse_turn(turn_payload())
        task = asyncio.create_task(
            module.run_turn_while_connected(OpenWebSocket(), ("./bot",), turn)
        )
        await bot_started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert bot_cancelled.is_set()
        assert closed_wait_cancelled.is_set()

    asyncio.run(scenario())
