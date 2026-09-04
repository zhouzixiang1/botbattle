"""Bound expensive work performed before a WebSocket is authenticated."""

from __future__ import annotations

import asyncio
import time
from collections import deque


class WebSocketHandshakeGate:
    """Rate, concurrency, and memory gate for pre-auth WebSocket work.

    Each successful ``begin`` reserves one process-wide in-flight slot until
    its paired ``end``. Attempts remain in a sliding per-peer window after the
    slot is released. The peer registry has a hard cap: expired entries are
    reclaimed before a new key is admitted, and a full active registry rejects
    new keys without inserting them.
    """

    _MAX_PEER_KEY_CHARS = 128

    def __init__(
        self,
        *,
        max_attempts: int,
        window_seconds: float,
        max_inflight: int,
        max_buckets: int,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        if window_seconds <= 0:
            raise ValueError("window_seconds must be positive")
        if max_inflight < 1:
            raise ValueError("max_inflight must be positive")
        if max_buckets < 1:
            raise ValueError("max_buckets must be positive")
        self.max_attempts = int(max_attempts)
        self.window_seconds = float(window_seconds)
        self.max_inflight = int(max_inflight)
        self.max_buckets = int(max_buckets)
        self._hits: dict[str, deque[float]] = {}
        # Global chronological history makes expiry cleanup amortized O(hits)
        # rather than scanning every active peer for each new attacker key.
        self._history: deque[tuple[float, str]] = deque()
        self._inflight = 0
        self._lock = asyncio.Lock()

    @classmethod
    def _peer_key(cls, peer_ip: object) -> str:
        key = str(peer_ip or "unknown").strip() or "unknown"
        return key if len(key) <= cls._MAX_PEER_KEY_CHARS else "unknown"

    def _expire(self, cutoff: float) -> None:
        while self._history and self._history[0][0] <= cutoff:
            timestamp, key = self._history.popleft()
            bucket = self._hits.get(key)
            if not bucket:
                continue
            # History and buckets are appended in the same critical section.
            # Matching the timestamp keeps cleanup defensive if a future caller
            # supplies a non-monotonic test clock.
            if bucket[0] == timestamp:
                bucket.popleft()
            else:
                try:
                    bucket.remove(timestamp)
                except ValueError:
                    pass
            if not bucket:
                self._hits.pop(key, None)

    async def begin(self, peer_ip: object, *, now: float | None = None) -> bool:
        timestamp = time.monotonic() if now is None else float(now)
        cutoff = timestamp - self.window_seconds
        key = self._peer_key(peer_ip)
        async with self._lock:
            self._expire(cutoff)
            if self._inflight >= self.max_inflight:
                return False
            bucket = self._hits.get(key)
            if bucket is not None and len(bucket) >= self.max_attempts:
                return False
            if bucket is None:
                if len(self._hits) >= self.max_buckets:
                    return False
                bucket = deque()
                self._hits[key] = bucket
            bucket.append(timestamp)
            self._history.append((timestamp, key))
            self._inflight += 1
            return True

    async def end(self) -> None:
        async with self._lock:
            self._inflight = max(0, self._inflight - 1)


__all__ = ["WebSocketHandshakeGate"]
