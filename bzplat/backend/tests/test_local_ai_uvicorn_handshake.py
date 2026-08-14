"""Real-server regression for Local AI pre-auth WebSocket denial."""
from __future__ import annotations

import asyncio
import logging
import socket
import threading
import time

import pytest
import uvicorn
import websockets
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

    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
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
        access_log=False,
        log_config=None,
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [listener]},
        daemon=True,
    )
    capture = _MessageCapture()
    uvicorn_logger = logging.getLogger("uvicorn.error")
    uvicorn_logger.addHandler(capture)

    try:
        thread.start()
        deadline = time.monotonic() + 5.0
        while (
            not server.started
            and thread.is_alive()
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
        assert server.started

        async def connect_without_credentials() -> None:
            with pytest.raises(websockets.InvalidStatus) as denied:
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/api/local-ai/connect"
                ):
                    pass
            assert denied.value.response.status_code == 403

        asyncio.run(connect_without_credentials())
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
        listener.close()
        uvicorn_logger.removeHandler(capture)

    assert not thread.is_alive()
    assert not any(
        "without completing handshake" in message for message in capture.messages
    )
