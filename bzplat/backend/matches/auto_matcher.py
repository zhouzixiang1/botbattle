"""Persistent, fair and globally serial automatic ranking queue.

The scheduler continuously maintains a small lookahead queue and dispatches at
most one automatic ladder match.  Foreground challenges/contests still use the
orchestrator's existing global admission, with one slot permanently left free
for them.  SQLite owns queue identity, cross-process dispatch exclusivity and
match creation; the asyncio task only drives that durable state machine.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime
from typing import Any

from bzplat.backend.runtime.config import AUTO_MATCH_PLACEMENT_REQUIRED

logger = logging.getLogger(__name__)

# Fixed product policy, deliberately private and not a runtime settings surface.
_QUEUE_LOOKAHEAD = 6
_FOREGROUND_RESERVED_SLOTS = 1
_FAILSAFE_WAKE_SECONDS = 1.0
_DISPATCHER_LEASE_SECONDS = 30


class AutoMatchScheduler:
    """Drive the durable queue; ``capability_enabled=False`` is the QA guard."""

    def __init__(
        self,
        orch: Any,
        store: Any,
        *,
        capability_enabled: bool = True,
    ) -> None:
        self.orch = orch
        self.store = store
        self.capability_enabled = bool(capability_enabled)
        self._wake = asyncio.Event()
        self._last_pause_reason = ""
        self._dispatcher_token = secrets.token_hex(24)
        self._dispatcher_epoch = 0
        self._leader = False

    @property
    def configured_enabled(self) -> bool:
        return bool(self.store.get_auto_match_enabled())

    @property
    def effective_enabled(self) -> bool:
        return self.capability_enabled and self.configured_enabled

    def wake(self) -> None:
        """Wake promptly after a match finishes or the administrator toggles."""
        self._wake.set()

    def close(self) -> None:
        """Release an idle lease; an active claim keeps it until natural expiry."""
        if not self._leader:
            return
        if any(
            row.get("status") == "dispatched"
            for row in self.store.list_auto_match_queue()
        ):
            return
        self.store.release_auto_match_dispatcher(
            self._dispatcher_token, self._dispatcher_epoch
        )
        self._leader = False

    def _available_bot_slots(self) -> int:
        available = getattr(self.orch, "available_bot_slots", None)
        if callable(available):
            return max(0, int(available()))
        tasks = getattr(self.orch, "_tasks", {}) or {}
        admitted = max(int(getattr(self.orch, "_bot_running", 0) or 0), len(tasks))
        return max(0, int(getattr(self.orch, "max_concurrent", 0)) - admitted)

    def _has_dispatch_capacity(self) -> bool:
        # The automatic queue never consumes the foreground reserve.  With a
        # one-slot machine this correctly pauses instead of starving users.
        return self._available_bot_slots() > _FOREGROUND_RESERVED_SLOTS

    @staticmethod
    def _new_match_id() -> str:
        return datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)

    async def loop(self) -> None:
        """Event-driven loop with a one-second crash/race fail-safe."""
        while True:
            # Clear before work so a completion/toggle arriving during run_once
            # remains set and starts the next convergence turn immediately.
            # Clearing after run_once would lose precisely that wake and turn the
            # one-second fail-safe back into the normal dispatch latency.
            self._wake.clear()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one bad turn must not kill service
                self._last_pause_reason = "调度器本轮异常，正在重试"
                logger.exception("auto-match fair queue iteration failed")
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=_FAILSAFE_WAKE_SECONDS
                )
            except TimeoutError:
                pass

    def _ensure_dispatcher_leadership(self) -> dict:
        lease = self.store.acquire_auto_match_dispatcher(
            self._dispatcher_token,
            lease_seconds=_DISPATCHER_LEASE_SECONDS,
        )
        self._leader = bool(lease.get("owned"))
        if self._leader:
            self._dispatcher_epoch = int(lease.get("lease_epoch") or 0)
        if self._leader and lease.get("changed_owner"):
            lease["takeover"] = self.store.recover_auto_match_dispatcher_takeover(
                dispatcher_token=self._dispatcher_token,
                dispatcher_epoch=self._dispatcher_epoch,
            )
        return lease

    async def _converge_terminal_rows(self) -> dict:
        state = self.store.reconcile_auto_match_queue(
            dispatcher_token=self._dispatcher_token,
            dispatcher_epoch=self._dispatcher_epoch,
        )
        if state.get("outcome") == "not_leader":
            return state
        if int(state.get("waiting_settlement") or 0) > 0:
            # Uses the orchestrator's existing global settlement order lock and
            # exactly-once marker transaction.  A queue row is never deleted
            # merely because the match says completed.
            await self.orch.recover_unsettled_match_ratings(
                auto_fence=(self._dispatcher_token, self._dispatcher_epoch)
            )
            state = self.store.reconcile_auto_match_queue(
                dispatcher_token=self._dispatcher_token,
                dispatcher_epoch=self._dispatcher_epoch,
            )
        return state

    async def run_once(self) -> dict:
        """Converge, refill and (if capacity permits) dispatch exactly one match."""
        if not self.capability_enabled:
            self._last_pause_reason = "隔离 QA 实例强制关闭自动排位"
            return {"outcome": "qa_disabled"}
        lease = self._ensure_dispatcher_leadership()
        if not lease.get("owned"):
            self._last_pause_reason = "另一服务进程持有自动排位调度租约"
            return {"outcome": "standby", "lease": lease}
        reconciled = await self._converge_terminal_rows()
        if not self.configured_enabled:
            self._last_pause_reason = "管理员已关闭自动排位"
            return {"outcome": "disabled", "reconciled": reconciled}

        refill = self.store.refill_auto_match_queue(
            target_queued=_QUEUE_LOOKAHEAD,
            placement_required=AUTO_MATCH_PLACEMENT_REQUIRED,
            dispatcher_token=self._dispatcher_token,
            dispatcher_epoch=self._dispatcher_epoch,
        )
        if refill.get("outcome") in {
            "disabled", "backoff", "not_leader", "rating_unverified"
        }:
            self._last_pause_reason = (
                "管理员已关闭自动排位"
                if refill.get("outcome") == "disabled"
                else "排行榜投影尚未完成离线重建与验证"
                if refill.get("outcome") == "rating_unverified"
                else "平台故障退避中"
                if refill.get("outcome") == "backoff"
                else "另一服务进程持有自动排位调度租约"
            )
            return {
                "outcome": refill.get("outcome"),
                "refill": refill,
                "reconciled": reconciled,
            }
        rows = self.store.list_auto_match_queue()
        if any(row.get("status") == "dispatched" for row in rows):
            self._last_pause_reason = ""
            return {"outcome": "active", "refill": refill, "reconciled": reconciled}
        if not any(row.get("status") == "queued" for row in rows):
            self._last_pause_reason = "同游戏、不同所有者的可用 Bot 不足"
            return {"outcome": "insufficient_pool", "refill": refill}
        if not self._has_dispatch_capacity():
            self._last_pause_reason = "正在为用户挑战或赛事保留执行容量"
            return {"outcome": "capacity", "refill": refill}

        match_id = self._new_match_id()
        # Admission is the first authoritative claim.  No foreground coroutine
        # can take this token between DB creation and start_prepared_match.
        try:
            self.orch.reserve_prepared_match_slot(
                match_id, keep_free=_FOREGROUND_RESERVED_SLOTS
            )
        except Exception:
            self._last_pause_reason = "正在为用户挑战或赛事保留执行容量"
            return {"outcome": "capacity_race", "refill": refill}
        try:
            claim = self.store.claim_next_auto_match(
                match_id,
                dispatcher_token=self._dispatcher_token,
                dispatcher_epoch=self._dispatcher_epoch,
            )
        except Exception:
            self.orch.release_prepared_match_slot(match_id)
            raise
        if claim.get("outcome") != "claimed":
            self.orch.release_prepared_match_slot(match_id)
            self._last_pause_reason = str(claim.get("reason") or "队列等待中")
            return {"outcome": claim.get("outcome"), "claim": claim, "refill": refill}
        try:
            # claim already atomically created pending match/index/replay.  This
            # call reserves the in-process admission token and starts its task.
            self.orch.start_prepared_match(
                match_id,
                auto_dispatcher_token=self._dispatcher_token,
                auto_dispatcher_epoch=self._dispatcher_epoch,
            )
        except Exception:
            logger.exception("auto-match claimed match could not start match=%s", match_id)
            rolled_back = self.store.rollback_auto_match_claim(
                match_id,
                dispatcher_token=self._dispatcher_token,
                dispatcher_epoch=self._dispatcher_epoch,
                reason="start_failure",
            )
            self.orch.release_prepared_match_slot(match_id)
            if not rolled_back:
                raise
            self._last_pause_reason = "启动失败，自动排位已进入退避"
            return {
                "outcome": "start_failure",
                "match_id": match_id,
                "reconciled": reconciled,
            }

        # Keep a fixed upcoming horizon while the claimed Bots remain represented
        # by the dispatched row and therefore cannot be selected again.
        refill_after = self.store.refill_auto_match_queue(
            target_queued=_QUEUE_LOOKAHEAD,
            placement_required=AUTO_MATCH_PLACEMENT_REQUIRED,
            dispatcher_token=self._dispatcher_token,
            dispatcher_epoch=self._dispatcher_epoch,
        )
        self._last_pause_reason = ""
        logger.info(
            "auto-match dispatched queue_id=%s match=%s game=%s upcoming=%s",
            claim.get("queue_id"),
            match_id,
            claim.get("game_id"),
            refill_after.get("queued"),
        )
        return {
            "outcome": "claimed",
            "claim": claim,
            "refill": refill_after,
            "reconciled": reconciled,
        }

    async def on_match_done(self, match_id: str) -> None:
        """Converge an auto terminal, and always wake for newly freed capacity."""
        # ``_finish_match_task`` invokes this after completed post-processing and
        # after releasing global admission.  For non-auto matches reconciliation
        # is a cheap no-op but the wake avoids waiting for the fail-safe tick.
        if self.capability_enabled:
            lease = self._ensure_dispatcher_leadership()
            if lease.get("owned"):
                await self._converge_terminal_rows()
        self.wake()

    @staticmethod
    def _public_bot(row: dict, seat: str) -> dict:
        played = max(0, int(row.get(f"bot_{seat}_matches_played") or 0))
        return {
            "id": int(row[f"bot_{seat}_id"]),
            "name": row.get(f"bot_{seat}_name") or "",
            "display_name": row.get(f"bot_{seat}_display") or "",
            "owner": {
                "username": row.get(f"bot_{seat}_owner") or "",
                "display_name": row.get(f"bot_{seat}_owner_display") or "",
            },
            "rating": float(row.get(f"bot_{seat}_rating") or 1500.0),
            "matches_played": played,
            "is_placement": played < AUTO_MATCH_PLACEMENT_REQUIRED,
            "placement_remaining": max(0, AUTO_MATCH_PLACEMENT_REQUIRED - played),
        }

    @classmethod
    def _public_row(cls, row: dict) -> dict:
        return {
            "id": int(row["id"]),
            "status": row["status"],
            "position": int(row.get("position") or 0),
            "game_id": row["game_id"],
            "match_id": row.get("match_id"),
            "match_status": row.get("match_status"),
            "started_at": row.get("match_started_at"),
            "created_at": row.get("created_at"),
            "reason": row.get("selection_reason") or "公平队列",
            "requested_lane": row.get("requested_lane"),
            "lane": row.get("actual_lane"),
            "fallback_reason": row.get("fallback_reason") or "",
            "bot_a": cls._public_bot(row, "a"),
            "bot_b": cls._public_bot(row, "b"),
        }

    def public_snapshot(self, *, game_id: str | None = None) -> dict:
        all_rows = self.store.list_auto_match_queue()
        fair_state = self.store.get_auto_match_fair_state()
        rating_projection = self.store.rating_projection_status()
        selected = [
            row for row in all_rows
            if game_id is None or row.get("game_id") == game_id
        ]
        active_global = next(
            (row for row in all_rows if row.get("status") == "dispatched"), None
        )
        active_selected = next(
            (row for row in selected if row.get("status") == "dispatched"), None
        )
        upcoming = [row for row in selected if row.get("status") == "queued"]
        configured = self.configured_enabled
        effective = self.capability_enabled and configured
        if not self.capability_enabled:
            paused = True
            pause_reason = "隔离 QA 实例强制关闭自动排位"
        elif not configured:
            paused = True
            pause_reason = "管理员已关闭自动排位"
        elif not rating_projection.get("ready"):
            paused = True
            pause_reason = "排行榜投影尚未完成离线重建与验证"
        elif fair_state.get("not_before") and str(fair_state["not_before"]) > datetime.now().isoformat(timespec="seconds"):
            paused = True
            pause_reason = "平台故障退避中，将自动恢复"
        elif active_global is not None:
            paused = False
            pause_reason = ""
        elif not upcoming and game_id is not None:
            paused = True
            pause_reason = "当前游戏暂无满足公平条件的待赛对局"
        elif not all_rows:
            paused = True
            pause_reason = self._last_pause_reason or "可用 Bot 不足"
        elif not self._has_dispatch_capacity():
            paused = True
            pause_reason = "正在为用户挑战或赛事保留执行容量"
        else:
            paused = False
            pause_reason = ""
        return {
            "game_id": game_id,
            "enabled": configured,
            "effective_enabled": effective,
            "capability_enabled": self.capability_enabled,
            "dispatcher_leader": self._leader,
            "paused": paused,
            "pause_reason": pause_reason,
            "not_before": fair_state.get("not_before"),
            "platform_failures": int(fair_state.get("platform_failures") or 0),
            "rating_projection_ready": bool(rating_projection.get("ready")),
            "placement_required": AUTO_MATCH_PLACEMENT_REQUIRED,
            "policy": {
                "serial": True,
                "lookahead": _QUEUE_LOOKAHEAD,
                "foreground_slot_reserved": True,
            },
            "active": self._public_row(active_selected) if active_selected else None,
            "active_game_id": active_global.get("game_id") if active_global else None,
            "upcoming": [self._public_row(row) for row in upcoming],
        }


__all__ = ["AutoMatchScheduler"]
