"""Real-server regression for Local AI pre-auth WebSocket denial."""
from __future__ import annotations

import logging
import socket
import threading
import time

import uvicorn
from fastapi import FastAPI, WebSocket

from bzplat.backend.api_routes import _deny_local_ai_websocket


class _MessageCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def test_pre_auth_denial_is_valid_http_under_real_uvicorn() -> None:
    """A connector must receive a parseable 403, not a broken handshake."""

    app = FastAPI()

    @app.websocket("/api/local-ai/connect")
    async def reject(websocket: WebSocket) -> None:
        await _deny_local_ai_websocket(websocket)

    loggers = {
        name: logging.getLogger(name)
        for name in ("uvicorn.error", "uvicorn.access")
    }
    logger_states = {
        name: (
            logger.level,
            list(logger.handlers),
            logger.propagate,
            logger.disabled,
        )
        for name, logger in loggers.items()
    }
    capture = _MessageCapture()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server: uvicorn.Server | None = None
    thread: threading.Thread | None = None
    response = b""

    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        port = int(listener.getsockname()[1])

        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            lifespan="off",
            ws="websockets-sansio",
            proxy_headers=False,
            access_log=True,
            log_config=None,
            log_level=None,
        )
        server = uvicorn.Server(config)
        thread = threading.Thread(
            target=server.run,
            kwargs={"sockets": [listener]},
            daemon=True,
        )
        loggers["uvicorn.error"].disabled = False
        loggers["uvicorn.error"].setLevel(logging.ERROR)
        loggers["uvicorn.error"].addHandler(capture)
        thread.start()
        deadline = time.monotonic() + 5.0
        while (
            not server.started
            and thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert server.started

        request = (
            "GET /api/local-ai/connect HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        with socket.create_connection(("127.0.0.1", port), timeout=5.0) as peer:
            peer.sendall(request)
            chunks: list[bytes] = []
            while chunk := peer.recv(4096):
                chunks.append(chunk)
            response = b"".join(chunks)
    finally:
        if server is not None:
            server.should_exit = True
        if thread is not None:
            thread.join(timeout=5.0)
        listener.close()
        for name, (level, handlers, propagate, disabled) in logger_states.items():
            logger = loggers[name]
            logger.setLevel(level)
            logger.handlers[:] = handlers
            logger.propagate = propagate
            logger.disabled = disabled

    assert thread is not None and not thread.is_alive()
    header_block, separator, body = response.partition(b"\r\n\r\n")
    assert separator == b"\r\n\r\n"
    lines = header_block.split(b"\r\n")
    assert lines[0] == b"HTTP/1.1 403 Forbidden"
    headers: list[tuple[bytes, bytes]] = []
    for line in lines[1:]:
        name, delimiter, value = line.partition(b":")
        assert delimiter == b":"
        headers.append((name.strip().lower(), value.strip()))
    header_names = [name for name, _ in headers]
    assert len(header_names) == len(set(header_names))
    assert header_names.count(b"content-length") == 1
    content_length = next(
        value for name, value in headers if name == b"content-length"
    )
    assert int(content_length) == len(body)
    assert header_names.count(b"content-type") == 1
    assert (b"connection", b"close") in [
        (name, value.lower()) for name, value in headers
    ]
    assert not any(
        "without completing handshake" in message for message in capture.messages
    )
