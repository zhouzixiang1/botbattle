"""Rate-aware JSON polling shared by mutating QA scripts.

The development service keeps the production API rate limiter enabled.  QA
workers all originate from the same local address, so tight independent loops
can consume the default read budget before a real Docker match finishes.  This
helper coordinates those loops without weakening the server-side boundary.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Protocol


class JsonResponse(Protocol):
    status_code: int
    headers: Any
    text: str

    def json(self) -> Any: ...


class QaPollingError(RuntimeError):
    """A polling response violated the expected HTTP/JSON contract."""


def retry_after_seconds(value: str | None, *, default: float) -> float:
    """Return a bounded delay for the numeric Retry-After emitted by the API."""
    if value is None:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed < 0 or parsed != parsed:  # negative or NaN
        return default
    return parsed


class RateAwareJsonPoller:
    """Serialize local QA polls and retry HTTP 429 after the advertised delay."""

    def __init__(
        self,
        *,
        min_interval: float = 1.0,
        retry_padding: float = 0.05,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if min_interval <= 0:
            raise ValueError("min_interval must be positive")
        self.min_interval = min_interval
        self.retry_padding = max(0.0, retry_padding)
        self._monotonic = monotonic
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def _reserve_turn(self, *, deadline: float, label: str, last: str) -> None:
        while True:
            with self._lock:
                now = self._monotonic()
                if now >= deadline:
                    suffix = f"；最后响应 {last}" if last else ""
                    raise TimeoutError(f"{label} 轮询超时{suffix}")
                wait = max(0.0, self._next_request_at - now)
                if wait == 0:
                    self._next_request_at = now + self.min_interval
                    return
            self._sleep(min(wait, max(0.0, deadline - self._monotonic())))

    def _defer_all(self, delay: float) -> None:
        with self._lock:
            self._next_request_at = max(
                self._next_request_at,
                self._monotonic() + delay,
            )

    def get_json(
        self,
        request: Callable[[], JsonResponse],
        *,
        label: str,
        deadline: float,
    ) -> dict[str, Any]:
        """Return one valid HTTP 200 object, retrying only explicit rate limits."""
        last = ""
        while True:
            self._reserve_turn(deadline=deadline, label=label, last=last)
            try:
                response = request()
            except Exception as exc:
                raise QaPollingError(
                    f"{label} 轮询请求失败：{type(exc).__name__}: {exc}"
                ) from exc

            status = int(response.status_code)
            body = response.text[:240]
            try:
                payload: Any = response.json()
            except (TypeError, ValueError) as exc:
                raise QaPollingError(
                    f"{label} 轮询返回不可解析 JSON：HTTP {status} body={body!r}"
                ) from exc

            if status == 429:
                retry_after = retry_after_seconds(
                    response.headers.get("Retry-After"),
                    default=self.min_interval,
                )
                delay = max(self.min_interval, retry_after + self.retry_padding)
                last = f"HTTP 429 body={body!r} Retry-After={retry_after:g}s"
                self._defer_all(delay)
                continue

            if status != 200:
                raise QaPollingError(
                    f"{label} 轮询失败：HTTP {status} body={body!r}"
                )
            if not isinstance(payload, dict):
                raise QaPollingError(
                    f"{label} 轮询响应必须是 JSON 对象：HTTP 200 body={body!r}"
                )
            return payload
