#!/usr/bin/env python3
"""Connect a Traditional Bot running on the user's computer to Botbattle.

The connection is always initiated by this client.  The access token is read
only from ``BZ_LOCAL_AI_TOKEN`` and is sent in the WebSocket Authorization
header; it is never accepted as a command-line argument or URL parameter.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit


TOKEN_ENV = "BZ_LOCAL_AI_TOKEN"
MAX_INPUT_BYTES = 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_TIMEOUT_MS = 15 * 60 * 1000
LOCAL_AI_WEBSOCKET_SUBPROTOCOL = "botbattle.local-ai.v2"
INITIAL_RECONNECT_DELAY = 1.0
MAX_RECONNECT_DELAY = 30.0
STABLE_CONNECTION_SECONDS = 30.0
CLIENT_FAILURE_REASONS = frozenset(
    {
        "bot_start_failed",
        "bot_no_response",
        "bot_output_too_large",
        "bot_output_invalid",
        "bot_io_failed",
        "bot_decision_timeout",
    }
)

LOG = logging.getLogger("botbattle.local_ai_client")


class ClientConfigError(ValueError):
    """The local client configuration is unsafe or incomplete."""


class TurnError(RuntimeError):
    """A single local Bot decision could not produce a bounded response."""

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.reason = reason


class ConnectionLostDuringTurn(ConnectionError):
    """The transport closed while the current local Bot was still deciding."""


class RedirectRejected(ConnectionError):
    """The configured WSS endpoint attempted an unsafe handshake redirect."""


@dataclass(frozen=True, slots=True)
class Turn:
    request_id: str
    match_id: str
    turn: int
    input_line: str
    timeout_ms: int


@dataclass(frozen=True, slots=True)
class PrepareTurn:
    request_id: str
    match_id: str
    turn: int
    prepare_timeout_ms: int


def read_token(environment: Mapping[str, str] | None = None) -> str:
    """Read a bearer token without exposing a CLI or URL token surface."""

    source = os.environ if environment is None else environment
    token = source.get(TOKEN_ENV, "")
    if token != token.strip() or len(token) < 24 or len(token) > 512:
        raise ClientConfigError(f"请通过环境变量 {TOKEN_ENV} 提供有效接入令牌")
    if any(character.isspace() or ord(character) < 0x21 for character in token):
        raise ClientConfigError(f"环境变量 {TOKEN_ENV} 中的令牌格式无效")
    return token


def validate_server_url(value: str) -> str:
    """Accept only a credential-free TLS WebSocket endpoint."""

    if value != value.strip() or any(ord(character) < 0x21 for character in value):
        raise ClientConfigError("接入地址包含空白或控制字符")
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port  # Force validation of a malformed port while still local.
    except ValueError as exc:
        raise ClientConfigError("接入地址格式无效") from exc
    if parsed.scheme.lower() != "wss":
        raise ClientConfigError("接入地址必须使用 wss://")
    if not hostname:
        raise ClientConfigError("接入地址缺少主机名")
    if parsed.username is not None or parsed.password is not None:
        raise ClientConfigError("接入地址不能包含用户名或密码")
    if parsed.query or parsed.fragment:
        raise ClientConfigError("接入地址不能包含查询参数或片段")
    path = parsed.path or "/api/local-ai/connect"
    if not path.startswith("/"):
        raise ClientConfigError("接入地址路径无效")
    # Rebuild the value so logs and connection errors cannot retain hidden URL
    # credentials or parameters even if a caller passed unusual casing.
    return urlunsplit(("wss", parsed.netloc, path, "", ""))


def parse_turn(message: Mapping[str, Any]) -> Turn:
    """Validate the small server-to-client turn envelope."""

    if message.get("type") != "turn":
        raise TurnError("消息不是决策请求")

    request_id = message.get("request_id")
    match_id = message.get("match_id")
    turn = message.get("turn")
    seat = message.get("seat")
    input_line = message.get("input_line")
    timeout_ms = message.get("timeout_ms")

    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise TurnError("request_id 无效")
    if not isinstance(match_id, str) or not match_id or len(match_id) > 128:
        raise TurnError("match_id 无效")
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
        raise TurnError("turn 无效")
    if isinstance(seat, bool) or not isinstance(seat, int) or seat not in (1, 2):
        raise TurnError("seat 无效")
    if not isinstance(input_line, str) or "\n" in input_line or "\r" in input_line:
        raise TurnError("input_line 必须是单行文本")
    if len(input_line.encode("utf-8")) > MAX_INPUT_BYTES:
        raise TurnError("input_line 超过客户端上限")
    if (
        isinstance(timeout_ms, bool)
        or not isinstance(timeout_ms, int)
        or not 0 < timeout_ms <= MAX_TIMEOUT_MS
    ):
        raise TurnError("timeout_ms 无效")

    return Turn(
        request_id=request_id,
        match_id=match_id,
        turn=turn,
        input_line=input_line,
        timeout_ms=timeout_ms,
    )


def parse_prepare_turn(message: Mapping[str, Any]) -> PrepareTurn:
    """Validate a position-free process preparation envelope."""

    expected_fields = {
        "type", "request_id", "match_id", "turn", "seat", "prepare_timeout_ms",
    }
    if message.get("type") != "prepare_turn" or set(message) != expected_fields:
        raise TurnError("消息不是严格的准备请求")
    request_id = message.get("request_id")
    match_id = message.get("match_id")
    turn = message.get("turn")
    seat = message.get("seat")
    prepare_timeout_ms = message.get("prepare_timeout_ms")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 128:
        raise TurnError("request_id 无效")
    if not isinstance(match_id, str) or not match_id or len(match_id) > 128:
        raise TurnError("match_id 无效")
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 0:
        raise TurnError("turn 无效")
    if isinstance(seat, bool) or not isinstance(seat, int) or seat not in (1, 2):
        raise TurnError("seat 无效")
    if (
        isinstance(prepare_timeout_ms, bool)
        or not isinstance(prepare_timeout_ms, int)
        or not 0 < prepare_timeout_ms <= MAX_TIMEOUT_MS
    ):
        raise TurnError("prepare_timeout_ms 无效")
    return PrepareTurn(request_id, match_id, turn, prepare_timeout_ms)


def prepared_message(turn: PrepareTurn) -> dict[str, Any]:
    return {
        "type": "prepared",
        "request_id": turn.request_id,
        "match_id": turn.match_id,
        "turn": turn.turn,
    }


def response_message(turn: Turn, output: str) -> dict[str, Any]:
    return {
        "type": "response",
        "request_id": turn.request_id,
        "match_id": turn.match_id,
        "turn": turn.turn,
        "output": output,
    }


def failure_message(turn: Turn | PrepareTurn, reason: str) -> dict[str, Any]:
    """Return a bounded failure category without leaking local diagnostics."""

    if reason not in CLIENT_FAILURE_REASONS:
        raise ValueError("未知的本地 Bot 故障类别")
    return {
        "type": "failure",
        "request_id": turn.request_id,
        "match_id": turn.match_id,
        "turn": turn.turn,
        "reason": reason,
    }


async def _read_first_line(stream: asyncio.StreamReader) -> str:
    try:
        raw = await stream.readline()
    except ValueError as exc:
        raise TurnError(
            "Bot 输出首行超过 64 KiB", reason="bot_output_too_large"
        ) from exc

    if not raw:
        raise TurnError("Bot 未输出响应", reason="bot_no_response")
    if raw.endswith(b"\n"):
        raw = raw[:-1]
        if raw.endswith(b"\r"):
            raw = raw[:-1]
    if len(raw) > MAX_OUTPUT_BYTES:
        raise TurnError(
            "Bot 输出首行超过 64 KiB", reason="bot_output_too_large"
        )
    if b"\x00" in raw:
        raise TurnError("Bot 输出包含 NUL 字节", reason="bot_output_invalid")
    try:
        output = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TurnError("Bot 输出不是 UTF-8", reason="bot_output_invalid") from exc
    if not output.strip():
        raise TurnError("Bot 输出为空", reason="bot_no_response")
    return output


async def _stop_process_impl(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        await process.wait()
        return

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:  # Windows has no process groups compatible with os.killpg.
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1.0)
        return
    except TimeoutError:
        pass

    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


async def _stop_process(process: asyncio.subprocess.Process) -> None:
    """Own process-group teardown through any number of caller cancellations."""

    cleanup = asyncio.create_task(_stop_process_impl(process))
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError:
            # A WebSocket close can cancel the turn task, then admin/shutdown
            # can cancel its parent while this cleanup is still in the TERM
            # grace period.  Every wait remains shielded; remember cancellation
            # but never pass it into the one task that owns SIGTERM→SIGKILL.
            cancelled = True
    cleanup.result()
    if cancelled:
        raise asyncio.CancelledError


async def _spawn_traditional_process(
    command: Sequence[str],
) -> asyncio.subprocess.Process:
    if not command or not command[0]:
        raise TurnError(
            "--command 后需要提供 Bot 命令", reason="bot_start_failed"
        )
    bot_environment = os.environ.copy()
    # The connector credential belongs to this transport process, never to
    # the untrusted Bot command launched for an individual decision.
    bot_environment.pop(TOKEN_ENV, None)
    try:
        return await asyncio.create_subprocess_exec(
            *command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_OUTPUT_BYTES + 2,
            start_new_session=os.name == "posix",
            env=bot_environment,
        )
    except (FileNotFoundError, PermissionError, OSError, ValueError) as exc:
        raise TurnError("无法启动 Bot 命令", reason="bot_start_failed") from exc


async def prepare_traditional_process(
    command: Sequence[str], prepare_turn: PrepareTurn
) -> asyncio.subprocess.Process:
    """Create the per-turn process under the separate preparation bound."""

    try:
        return await asyncio.wait_for(
            _spawn_traditional_process(command),
            timeout=prepare_turn.prepare_timeout_ms / 1000,
        )
    except TimeoutError as exc:
        raise TurnError("Bot 启动超时", reason="bot_start_failed") from exc


async def _communicate_traditional_process(
    process: asyncio.subprocess.Process, turn: Turn
) -> str:
    assert process.stdin is not None
    assert process.stdout is not None
    try:
        process.stdin.write(turn.input_line.encode("utf-8") + b"\n")
        await process.stdin.drain()
        process.stdin.close()
        if hasattr(process.stdin, "wait_closed"):
            await process.stdin.wait_closed()
        return await _read_first_line(process.stdout)
    except (BrokenPipeError, ConnectionResetError) as exc:
        raise TurnError("Bot 在读取请求前退出", reason="bot_io_failed") from exc


async def run_prepared_traditional_turn(
    process: asyncio.subprocess.Process,
    turn: Turn,
    *,
    on_output: Callable[[str], Awaitable[None]] | None = None,
    on_failure: Callable[[TurnError], Awaitable[None]] | None = None,
) -> str:
    """Deliver the full request and read one response under the game clock."""

    try:
        output = await asyncio.wait_for(
            _communicate_traditional_process(process, turn),
            timeout=turn.timeout_ms / 1000,
        )
        # The authoritative response boundary is the complete first stdout
        # line, not process teardown.  Relay it before SIGTERM/SIGKILL cleanup
        # so a Bot that prints promptly but does not exit cannot lose one extra
        # second of its chess clock.
        if on_output is not None:
            await on_output(output)
        return output
    except TimeoutError as exc:
        failure = TurnError("Bot 决策超时", reason="bot_decision_timeout")
        if on_failure is not None:
            await on_failure(failure)
        raise failure from exc
    except TurnError as exc:
        if on_failure is not None:
            await on_failure(exc)
        raise
    finally:
        await _stop_process(process)


async def run_traditional_turn(
    command: Sequence[str],
    turn: Turn,
    *,
    on_output: Callable[[str], Awaitable[None]] | None = None,
    on_failure: Callable[[TurnError], Awaitable[None]] | None = None,
) -> str:
    """Run a directly delivered/reconnected turn under its remaining deadline.

    Normal first delivery uses ``prepare_traditional_process`` followed by
    ``run_prepared_traditional_turn``.  A decision re-delivered after transport
    loss cannot extend its frozen deadline, so process recreation intentionally
    remains inside the remaining allowance here.
    """

    process: asyncio.subprocess.Process | None = None

    async def execute() -> str:
        nonlocal process
        process = await _spawn_traditional_process(command)
        return await _communicate_traditional_process(process, turn)

    try:
        output = await asyncio.wait_for(execute(), timeout=turn.timeout_ms / 1000)
        if on_output is not None:
            await on_output(output)
        return output
    except TimeoutError as exc:
        failure = TurnError("Bot 决策超时", reason="bot_decision_timeout")
        if on_failure is not None:
            await on_failure(failure)
        raise failure from exc
    except TurnError as exc:
        if on_failure is not None:
            await on_failure(exc)
        raise
    finally:
        if process is not None:
            await _stop_process(process)


async def run_turn_while_connected(
    websocket: Any,
    command: Sequence[str],
    turn: Turn,
    *,
    on_output: Callable[[str], Awaitable[None]] | None = None,
    on_failure: Callable[[TurnError], Awaitable[None]] | None = None,
) -> str:
    """Stop the per-turn Bot promptly when its transport is no longer usable."""

    wait_closed = getattr(websocket, "wait_closed", None)
    if not callable(wait_closed):
        # Lightweight test doubles and older compatible transports may expose
        # only the asynchronous iterator.  Their iterator still terminates at
        # the next receive boundary, so retain the existing bounded behavior.
        if on_output is None and on_failure is None:
            return await run_traditional_turn(command, turn)
        return await run_traditional_turn(
            command, turn, on_output=on_output, on_failure=on_failure
        )

    if on_output is None and on_failure is None:
        bot_task = asyncio.create_task(run_traditional_turn(command, turn))
    else:
        bot_task = asyncio.create_task(
            run_traditional_turn(
                command, turn, on_output=on_output, on_failure=on_failure
            )
        )
    closed_task = asyncio.create_task(wait_closed())
    tasks = (bot_task, closed_task)
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if closed_task in done and bot_task not in done:
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
            raise ConnectionLostDuringTurn("连接在 Bot 决策完成前中断")

        closed_task.cancel()
        await asyncio.gather(closed_task, return_exceptions=True)
        return await bot_task
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run_prepared_turn_while_connected(
    websocket: Any,
    process: asyncio.subprocess.Process,
    turn: Turn,
    *,
    on_output: Callable[[str], Awaitable[None]] | None = None,
    on_failure: Callable[[TurnError], Awaitable[None]] | None = None,
) -> str:
    """Apply the same disconnect cleanup to an already prepared process."""

    wait_closed = getattr(websocket, "wait_closed", None)
    if not callable(wait_closed):
        if on_output is None and on_failure is None:
            return await run_prepared_traditional_turn(process, turn)
        return await run_prepared_traditional_turn(
            process, turn, on_output=on_output, on_failure=on_failure
        )
    if on_output is None and on_failure is None:
        bot_task = asyncio.create_task(run_prepared_traditional_turn(process, turn))
    else:
        bot_task = asyncio.create_task(
            run_prepared_traditional_turn(
                process, turn, on_output=on_output, on_failure=on_failure
            )
        )
    closed_task = asyncio.create_task(wait_closed())
    tasks = (bot_task, closed_task)
    try:
        done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if closed_task in done and bot_task not in done:
            bot_task.cancel()
            await asyncio.gather(bot_task, return_exceptions=True)
            raise ConnectionLostDuringTurn("连接在 Bot 决策完成前中断")
        closed_task.cancel()
        await asyncio.gather(closed_task, return_exceptions=True)
        return await bot_task
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


def reconnect_delays(count: int) -> tuple[float, ...]:
    """Return the deterministic capped delays used after consecutive failures."""

    if count < 0:
        raise ValueError("count must be non-negative")
    delay = INITIAL_RECONNECT_DELAY
    values: list[float] = []
    for _ in range(count):
        values.append(delay)
        delay = min(delay * 2, MAX_RECONNECT_DELAY)
    return tuple(values)


def _connection_headers(websockets_module: Any, token: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {token}"}
    try:
        major = int(str(websockets_module.__version__).split(".", 1)[0])
    except (AttributeError, TypeError, ValueError):
        major = 14
    key = "additional_headers" if major >= 14 else "extra_headers"
    return {key: headers}


def _redirect_rejecting_connect(websockets_module: Any) -> type:
    """Return a ``websockets.connect`` class that never follows HTTP redirects.

    ``websockets`` 10.4--13 exposes ``handle_redirect`` while 14+ exposes
    ``process_redirect``.  The access token is a long-lived Authorization
    credential, so redirect behavior must be owned here rather than inherited
    from whichever library version happens to be installed.
    """

    base = getattr(websockets_module, "connect", None)
    if not isinstance(base, type):
        raise ClientConfigError("当前 websockets 实现不支持安全的连接重定向策略")
    if hasattr(base, "process_redirect"):
        class RedirectRejectingConnect(base):
            def process_redirect(self, exc: Exception) -> Exception:
                return exc

        return RedirectRejectingConnect
    if hasattr(base, "handle_redirect"):
        class RedirectRejectingConnect(base):
            def handle_redirect(self, uri: str) -> None:
                raise RedirectRejected("WSS 接入地址不允许重定向")

        return RedirectRejectingConnect
    raise ClientConfigError("当前 websockets 实现不支持安全的连接重定向策略")


def _open_connection(
    websockets_module: Any,
    url: str,
    token: str,
) -> Any:
    connect = _redirect_rejecting_connect(websockets_module)
    return connect(
        url,
        **_connection_headers(websockets_module, token),
        open_timeout=10,
        close_timeout=5,
        ping_interval=20,
        ping_timeout=20,
        max_size=MAX_INPUT_BYTES + 4096,
        subprotocols=[LOCAL_AI_WEBSOCKET_SUBPROTOCOL],
    )


async def handle_connection(websocket: Any, command: Sequence[str]) -> None:
    """Serve turns sequentially; the server owns the authoritative deadline."""

    prepared: tuple[PrepareTurn, asyncio.subprocess.Process] | None = None
    try:
        async for raw_message in websocket:
            if isinstance(raw_message, bytes):
                try:
                    raw_message = raw_message.decode("utf-8")
                except UnicodeDecodeError:
                    LOG.warning("忽略非 UTF-8 服务端消息")
                    continue
            try:
                message = json.loads(raw_message)
            except (json.JSONDecodeError, TypeError):
                LOG.warning("忽略无法解析的服务端消息")
                continue
            if not isinstance(message, dict):
                LOG.warning("忽略非对象服务端消息")
                continue
            message_type = message.get("type")
            if message_type == "ready":
                LOG.info("本地 Bot 已在线，等待测试对局")
                continue
            if message_type == "ping":
                await websocket.send(
                    json.dumps({"type": "pong"}, separators=(",", ":"))
                )
                continue
            if message_type in {"accepted", "match_end", "pong"}:
                continue
            if message_type in {"error", "reject"}:
                LOG.warning("平台拒绝了一条本地 Bot 消息")
                if prepared is not None:
                    _, process = prepared
                    prepared = None
                    await _stop_process(process)
                continue
            if message_type == "prepare_turn":
                try:
                    prepare_turn = parse_prepare_turn(message)
                except TurnError as exc:
                    LOG.warning("忽略无效的准备请求：%s", exc)
                    continue
                if prepared is not None:
                    _, process = prepared
                    prepared = None
                    await _stop_process(process)
                    LOG.warning("平台在上一准备请求未消费时又下发了请求")
                try:
                    process = await prepare_traditional_process(command, prepare_turn)
                except TurnError as exc:
                    if exc.reason not in CLIENT_FAILURE_REASONS:
                        LOG.error("第 %s 次准备未产生可上报的故障类别", prepare_turn.turn)
                        continue
                    LOG.error("第 %s 次 Bot 准备失败：%s", prepare_turn.turn, exc)
                    await websocket.send(
                        json.dumps(
                            failure_message(prepare_turn, exc.reason),
                            separators=(",", ":"),
                        )
                    )
                    continue
                prepared = (prepare_turn, process)
                await websocket.send(
                    json.dumps(prepared_message(prepare_turn), separators=(",", ":"))
                )
                continue
            if message_type != "turn":
                LOG.warning("忽略未知服务端消息类型")
                continue

            try:
                turn = parse_turn(message)
            except TurnError as exc:
                if prepared is not None:
                    _, process = prepared
                    prepared = None
                    await _stop_process(process)
                LOG.warning("忽略无效的决策请求：%s", exc)
                continue

            prepared_process: asyncio.subprocess.Process | None = None
            if prepared is not None:
                prepare_turn, process = prepared
                prepared = None
                if (
                    prepare_turn.request_id != turn.request_id
                    or prepare_turn.match_id != turn.match_id
                    or prepare_turn.turn != turn.turn
                ):
                    await _stop_process(process)
                    LOG.warning("准备请求与决策请求绑定不一致")
                    continue
                prepared_process = process

            try:
                failure_relayed = False

                async def relay_output(output: str) -> None:
                    await websocket.send(
                        json.dumps(
                            response_message(turn, output), separators=(",", ":")
                        )
                    )

                async def relay_failure(exc: TurnError) -> None:
                    nonlocal failure_relayed
                    if exc.reason not in CLIENT_FAILURE_REASONS:
                        return
                    await websocket.send(
                        json.dumps(
                            failure_message(turn, exc.reason), separators=(",", ":")
                        )
                    )
                    failure_relayed = True

                if prepared_process is None:
                    # A decision can be re-delivered directly after reconnect;
                    # its process recreation must remain inside the old deadline.
                    await run_turn_while_connected(
                        websocket,
                        command,
                        turn,
                        on_output=relay_output,
                        on_failure=relay_failure,
                    )
                else:
                    await run_prepared_turn_while_connected(
                        websocket,
                        prepared_process,
                        turn,
                        on_output=relay_output,
                        on_failure=relay_failure,
                    )
            except TurnError as exc:
                if exc.reason not in CLIENT_FAILURE_REASONS:
                    LOG.error("第 %s 次决策未产生可上报的故障类别", turn.turn)
                    continue
                LOG.error("第 %s 次决策失败：%s", turn.turn, exc)
                if not failure_relayed:
                    await websocket.send(
                        json.dumps(
                            failure_message(turn, exc.reason), separators=(",", ":")
                        )
                    )
                continue

    finally:
        if prepared is not None:
            _, process = prepared
            await _stop_process(process)


async def run_forever(url: str, command: Sequence[str], token: str) -> None:
    try:
        import websockets
    except ImportError as exc:  # pragma: no cover - uvicorn[standard] installs it.
        raise ClientConfigError(
            "缺少 websockets；请先执行 python -m pip install 'websockets>=10.4'"
        ) from exc

    delay = INITIAL_RECONNECT_DELAY
    while True:
        connected_at = time.monotonic()
        try:
            async with _open_connection(websockets, url, token) as websocket:
                await handle_connection(websocket, command)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Deliberately omit exception text: some HTTP stacks echo request
            # headers, and the Authorization token must never enter logs.
            LOG.warning("连接中断（%s），%.0f 秒后重连", type(exc).__name__, delay)

        lifetime = time.monotonic() - connected_at
        if lifetime >= STABLE_CONNECTION_SECONDS:
            delay = INITIAL_RECONNECT_DELAY
        await asyncio.sleep(delay)
        delay = min(delay * 2, MAX_RECONNECT_DELAY)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="让本机 Traditional Bot 主动连接 Botbattle 裁判",
    )
    parser.add_argument(
        "--url",
        required=True,
        help="平台提供的 wss:// 接入地址（不得包含令牌或查询参数）",
    )
    parser.add_argument(
        "--command",
        required=True,
        nargs=argparse.REMAINDER,
        help="每回合启动的 Bot 命令；必须放在所有客户端参数之后",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        url = validate_server_url(args.url)
        token = read_token()
        if not args.command:
            raise ClientConfigError("--command 后需要提供 Bot 命令")
    except ClientConfigError as exc:
        parser.error(str(exc))

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        asyncio.run(run_forever(url, tuple(args.command), token))
    except KeyboardInterrupt:
        LOG.info("本地 Bot 已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
