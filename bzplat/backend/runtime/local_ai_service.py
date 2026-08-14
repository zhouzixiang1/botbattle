"""Persistent identities and live transport adapter for user-hosted Bots."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque
from dataclasses import asdict
from typing import Any

from bzplat.backend.runtime.local_ai import LocalAIConnectionError, LocalAIHub
from bzplat.backend.store.db import LocalAIAgentBusyError


LOCAL_AI_TOKEN_PREFIX = "bzlai_"
LOCAL_AI_ROTATE_MAX_ATTEMPTS = 5
LOCAL_AI_ROTATE_WINDOW_SECONDS = 60.0
LOCAL_AI_HANDSHAKE_MAX_ATTEMPTS = 20
LOCAL_AI_HANDSHAKE_WINDOW_SECONDS = 60.0
LOCAL_AI_HANDSHAKE_MAX_INFLIGHT = 16


class LocalAIRateLimitError(RuntimeError):
    """A stable identity exceeded a sensitive Local AI operation budget."""

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class LocalAIHandshakeGate:
    """Bound pre-auth DB work by peer rate and process-wide concurrency."""

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._inflight = 0
        self._lock = asyncio.Lock()

    async def begin(self, peer_ip: str) -> bool:
        now = time.monotonic()
        cutoff = now - LOCAL_AI_HANDSHAKE_WINDOW_SECONDS
        key = str(peer_ip or "unknown")
        async with self._lock:
            bucket = self._hits[key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if (
                len(bucket) >= LOCAL_AI_HANDSHAKE_MAX_ATTEMPTS
                or self._inflight >= LOCAL_AI_HANDSHAKE_MAX_INFLIGHT
            ):
                return False
            bucket.append(now)
            self._inflight += 1
            if len(self._hits) > 2048:
                self._hits = defaultdict(
                    deque,
                    {
                        item_key: item_bucket
                        for item_key, item_bucket in self._hits.items()
                        if item_bucket and item_bucket[-1] > cutoff
                    },
                )
            return True

    async def end(self) -> None:
        async with self._lock:
            self._inflight = max(0, self._inflight - 1)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _new_token() -> str:
    return LOCAL_AI_TOKEN_PREFIX + secrets.token_urlsafe(32)


def _new_public_id() -> str:
    return "lai_" + secrets.token_urlsafe(12)


class LocalAIService:
    """Own one process-local hub while keeping credentials in SQLite as hashes."""

    def __init__(self, store, *, hub: LocalAIHub | None = None) -> None:
        self.store = store
        self.hub = hub or LocalAIHub()
        self.handshake_gate = LocalAIHandshakeGate()
        self._rotate_attempts: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        # The execution repository calls ``is_available_now`` while holding its
        # SQLite write transaction.  Keep that callback entirely process-local:
        # durable identity/activity is validated by the same claim transaction,
        # while this map only links a successfully persisted live connection to
        # its hub identity.  The companion connection id prevents a stale
        # socket cleanup from deleting a newer reconnect for the same agent.
        self._connected_public_ids: dict[int, str] = {}
        self._connected_connection_ids: dict[int, str] = {}

    @staticmethod
    def _private_projection(agent: dict) -> dict[str, Any]:
        return {
            "id": int(agent["id"]),
            "public_id": str(agent["public_id"]),
            "bot_id": int(agent["bot_id"]),
            "bot_name": str(agent.get("bot_name") or ""),
            "bot_display_name": str(agent.get("bot_display_name") or ""),
            "label": str(agent["label"]),
            "game_id": str(agent["game_id"]),
            "status": str(agent["status"]),
            "bot_active": bool(int(agent.get("bot_active") or 0)),
            "owner_active": bool(int(agent.get("owner_active") or 0)),
            "token_hint": str(agent.get("token_hint") or ""),
            "created_at": agent.get("created_at"),
            "last_seen_at": agent.get("last_seen_at"),
        }

    async def _with_live_status(self, agent: dict) -> dict[str, Any]:
        projected = self._private_projection(agent)
        status = await self.hub.status(str(agent["public_id"]))
        active_lease = self.store.has_active_local_ai_lease(int(agent["id"]))
        bot_active = bool(int(agent.get("bot_active") or 0))
        owner_active = bool(int(agent.get("owner_active") or 0))
        active = agent["status"] == "active" and owner_active
        online = bool(status.online and active)
        busy = bool((status.busy or active_lease) and active)
        available = bool(active and bot_active and online and not busy)
        if agent["status"] != "active":
            unavailable_reason = "revoked"
        elif not owner_active:
            unavailable_reason = "owner_disabled"
        elif not bot_active:
            unavailable_reason = "bot_disabled"
        elif not online:
            unavailable_reason = "offline"
        elif busy:
            unavailable_reason = "busy"
        else:
            unavailable_reason = ""
        projected.update(
            {
                "connection_state": (
                    "revoked" if agent["status"] == "revoked" else status.state
                ),
                "is_online": online,
                "is_busy": busy,
                "is_available": available,
                "unavailable_reason": unavailable_reason,
            }
        )
        return projected

    async def list_for_owner(self, owner_id: int) -> list[dict[str, Any]]:
        return [
            await self._with_live_status(agent)
            for agent in self.store.list_local_ai_agents(int(owner_id))
        ]

    async def list_for_admin(
        self, *, page: int = 1, per_page: int = 20
    ) -> dict[str, Any]:
        result = self.store.list_local_ai_agents_admin(
            page=int(page), per_page=int(per_page)
        )
        items: list[dict[str, Any]] = []
        for agent in result["items"]:
            projected = await self._with_live_status(agent)
            projected.pop("token_hint", None)
            projected.update(
                {
                    "owner_id": int(agent["owner_id"]),
                    "owner_name": str(agent.get("owner_name") or ""),
                    "owner_display_name": str(
                        agent.get("owner_display_name") or ""
                    ),
                }
            )
            items.append(projected)
        return {
            "items": items,
            "total": int(result["total"]),
            "page": int(result["page"]),
            "per_page": int(result["per_page"]),
        }

    async def get_for_owner(self, agent_id: int, owner_id: int) -> dict[str, Any] | None:
        agent = self.store.get_local_ai_agent(int(agent_id))
        if agent is None or int(agent["owner_id"]) != int(owner_id):
            return None
        return await self._with_live_status(agent)

    async def create(
        self, *, owner_id: int, bot_id: int, label: str
    ) -> tuple[dict[str, Any], str]:
        token = _new_token()
        agent = self.store.create_local_ai_agent(
            owner_id=int(owner_id),
            bot_id=int(bot_id),
            label=label,
            public_id=_new_public_id(),
            token_hash=_token_hash(token),
            token_hint=token[-6:],
        )
        agent = self.store.get_local_ai_agent(int(agent["id"])) or agent
        return await self._with_live_status(agent), token

    async def rotate(
        self, *, agent_id: int, owner_id: int
    ) -> tuple[dict[str, Any], str] | None:
        current = self.store.get_local_ai_agent(int(agent_id))
        if current is None or int(current["owner_id"]) != int(owner_id):
            return None
        self._check_rotate_rate(int(owner_id), int(agent_id))
        token = _new_token()
        old_public_id = str(current["public_id"])
        # The Store checks an active execution lease in the same write
        # transaction as rotation.  Only after the durable identity changes may
        # the old idle transport be revoked; a 409 must never decide a match.
        updated = self.store.rotate_local_ai_agent_token(
            int(agent_id),
            int(owner_id),
            public_id=_new_public_id(),
            token_hash=_token_hash(token),
            token_hint=token[-6:],
        )
        if updated is None:
            return None
        self._forget_connection(int(agent_id), public_id=old_public_id)
        await self.hub.revoke(old_public_id)
        updated = self.store.get_local_ai_agent(int(updated["id"])) or updated
        return await self._with_live_status(updated), token

    async def revoke(self, *, agent_id: int, owner_id: int) -> bool:
        current = self.store.get_local_ai_agent(int(agent_id))
        if current is None or int(current["owner_id"]) != int(owner_id):
            return False
        changed = self.store.revoke_local_ai_agent(int(agent_id), int(owner_id))
        if changed:
            self._forget_connection(int(agent_id), public_id=str(current["public_id"]))
            await self.hub.revoke(str(current["public_id"]))
        return changed

    async def revoke_as_admin(self, *, agent_id: int) -> bool:
        current = self.store.get_local_ai_agent(int(agent_id))
        if current is None:
            return False
        changed = self.store.revoke_local_ai_agent_admin(int(agent_id))
        if changed:
            self._forget_connection(int(agent_id), public_id=str(current["public_id"]))
            await self.hub.revoke(str(current["public_id"]))
        return changed

    def authenticate(self, token: str) -> dict | None:
        token = str(token or "").strip()
        if not token.startswith(LOCAL_AI_TOKEN_PREFIX) or len(token) < 40:
            return None
        candidate = self.store.get_local_ai_agent_by_token_hash(_token_hash(token))
        if (
            candidate is None
            or candidate.get("status") != "active"
            or not int(candidate.get("bot_active") or 0)
            or not int(candidate.get("owner_active") or 0)
        ):
            return None
        # The SQL lookup is indexed; a final constant-time comparison keeps the
        # credential boundary explicit if storage representation changes later.
        if not hmac.compare_digest(str(candidate["token_hash"]), _token_hash(token)):
            return None
        return candidate

    async def is_available(self, agent_id: int) -> bool:
        agent = self.store.get_local_ai_agent(int(agent_id))
        if (
            agent is None
            or agent.get("status") != "active"
            or not int(agent.get("bot_active") or 0)
            or not int(agent.get("owner_active") or 0)
        ):
            return False
        status = await self.hub.status(str(agent["public_id"]))
        return status.online and not status.busy

    def is_available_now(self, agent_id: int) -> bool:
        """Return one pure-memory claim snapshot without re-entering Store."""

        public_id = self._connected_public_ids.get(int(agent_id))
        return bool(public_id and self.hub.available_now(public_id))

    async def connect(self, agent: dict):
        agent_id = int(agent["id"])
        public_id = str(agent["public_id"])
        connection_id = secrets.token_urlsafe(18)
        try:
            # Supply the id before awaiting registration so cancellation at the
            # register boundary can still target exactly the state it may have
            # created. ``close`` is harmless when registration never completed.
            connection = await self.hub.register(
                public_id, connection_id=connection_id
            )
            persisted = self.store.connect_local_ai_agent(
                agent_id, expected_public_id=public_id
            )
            if persisted is None:
                raise RuntimeError("本地 Bot 已被撤销")
            self._connected_public_ids[agent_id] = public_id
            self._connected_connection_ids[agent_id] = connection.connection_id
        except BaseException:
            # The in-memory registration must never outlive a failed durable
            # connect write (including cancellation); otherwise every future
            # reconnect is rejected as already_connected.
            try:
                await self.hub.close(public_id, connection_id)
            except LocalAIConnectionError:
                # A different connection may have won before this registration
                # entered the hub. It must never be closed by this cleanup.
                pass
            finally:
                self._forget_connection(
                    agent_id,
                    public_id=public_id,
                    connection_id=connection_id,
                )
            raise
        return connection, int(persisted["connection_generation"])

    async def disconnect(
        self,
        agent: dict,
        connection_id: str,
        generation: int,
    ) -> None:
        agent_id = int(agent["id"])
        public_id = str(agent["public_id"])
        try:
            await self.hub.close(public_id, connection_id)
        finally:
            self._forget_connection(
                agent_id,
                public_id=public_id,
                connection_id=connection_id,
            )
            self.store.disconnect_local_ai_agent(agent_id, int(generation))

    async def touch_connection(
        self, agent: dict, connection_id: str, generation: int
    ) -> None:
        """Persist one coalesced heartbeat and fail a stale socket closed."""

        if self.store.touch_local_ai_agent(int(agent["id"]), int(generation)):
            return
        try:
            await self.hub.close(str(agent["public_id"]), str(connection_id))
        except LocalAIConnectionError:
            # A stale transport cannot close or de-register its replacement.
            pass
        finally:
            self._forget_connection(
                int(agent["id"]),
                public_id=str(agent["public_id"]),
                connection_id=str(connection_id),
            )
        raise LocalAIConnectionError("agent_no_longer_authorized")

    async def revoke_public_ids(self, public_ids: list[str]) -> None:
        """Notify live transports after a Store transaction disabled identity."""

        unique_ids = tuple(dict.fromkeys(str(item) for item in public_ids if item))
        revoked = set(unique_ids)
        for agent_id, public_id in tuple(self._connected_public_ids.items()):
            if public_id in revoked:
                self._forget_connection(agent_id, public_id=public_id)
        for public_id in unique_ids:
            await self.hub.revoke(public_id)

    def _forget_connection(
        self,
        agent_id: int,
        *,
        public_id: str | None = None,
        connection_id: str | None = None,
    ) -> None:
        """Forget only the connection generation selected by the caller."""

        agent_id = int(agent_id)
        if (
            public_id is not None
            and self._connected_public_ids.get(agent_id) != str(public_id)
        ):
            return
        if (
            connection_id is not None
            and self._connected_connection_ids.get(agent_id) != str(connection_id)
        ):
            return
        self._connected_public_ids.pop(agent_id, None)
        self._connected_connection_ids.pop(agent_id, None)

    def _check_rotate_rate(self, owner_id: int, agent_id: int) -> None:
        now = time.monotonic()
        cutoff = now - LOCAL_AI_ROTATE_WINDOW_SECONDS
        key = (int(owner_id), int(agent_id))
        bucket = self._rotate_attempts[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= LOCAL_AI_ROTATE_MAX_ATTEMPTS:
            retry = int(bucket[0] + LOCAL_AI_ROTATE_WINDOW_SECONDS - now) + 1
            raise LocalAIRateLimitError(
                "令牌更换过于频繁，请稍后再试", retry_after=retry
            )
        bucket.append(now)
        if len(self._rotate_attempts) > 2048:
            self._rotate_attempts = defaultdict(
                deque,
                {
                    item_key: item_bucket
                    for item_key, item_bucket in self._rotate_attempts.items()
                    if item_bucket and item_bucket[-1] > cutoff
                },
            )

    async def status_payload(self, public_id: str) -> dict[str, Any]:
        return asdict(await self.hub.status(public_id))


__all__ = [
    "LOCAL_AI_TOKEN_PREFIX",
    "LocalAIAgentBusyError",
    "LocalAIHandshakeGate",
    "LocalAIRateLimitError",
    "LocalAIService",
]
