"""Single-process driver for the durable, source-neutral execution queue."""
from __future__ import annotations

import asyncio
import fcntl
import logging
import os
from collections.abc import Awaitable, Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from bzplat.backend.runtime.binary_runner import (
    PlatformRunnerError,
    SandboxControlUncertain,
)
from bzplat.backend.runtime.config import (
    AUTO_MATCH_BOOTSTRAP_TARGET_MATCHES,
    EXECUTION_AGING_SECONDS,
    EXECUTION_AUTO_LOOKAHEAD,
    EXECUTION_CONTEST_SHARE_SLOTS,
    EXECUTION_POLL_SECONDS,
    EXECUTION_USER_ACTIVE_LIMIT,
)
from bzplat.backend.store.execution import (
    DockerLaunchInvariantError,
    ExecutionInvariantError,
)


logger = logging.getLogger(__name__)


class DispatcherAlreadyRunning(RuntimeError):
    """The DB-adjacent OS lock is owned by another platform process."""


class ExecutionDispatcher:
    """Own one flock, clean one label namespace, then claim durable jobs."""

    def __init__(
        self,
        orch: Any,
        store: Any,
        *,
        max_match_slots: int,
        max_sandbox_units: int | None = None,
        auto_capability_enabled: bool = True,
        contest_reconciler: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        self.orch = orch
        self.store = store
        self.repo = store.executions
        self.max_match_slots = max(1, int(max_match_slots))
        self.max_sandbox_units = max(
            1,
            int(max_sandbox_units or self.max_match_slots * 2),
        )
        self.auto_capability_enabled = bool(auto_capability_enabled)
        self.contest_reconciler = contest_reconciler
        self._wake = asyncio.Event()
        self._lock_fd: int | None = None
        self._started = False
        self._loop: asyncio.AbstractEventLoop | None = None
        # The dispatcher loop and the administrator endpoint share one event
        # loop but may request recovery concurrently.  Serialize the complete
        # physical cleanup -> DB compensation -> application reconcile gate;
        # the Docker flock alone cannot protect the later DB phase.
        self._recovery_lock = asyncio.Lock()

    @property
    def _lock_path(self) -> Path:
        db = Path(self.store.path).expanduser().resolve()
        return Path(str(db) + ".execution-dispatcher.lock")

    def _acquire_singleton(self) -> None:
        if self._lock_fd is not None:
            return
        fd = os.open(self._lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise DispatcherAlreadyRunning(
                "同一数据库已有执行 dispatcher"
            ) from exc
        self._lock_fd = fd

    def _release_singleton(self) -> None:
        if self._lock_fd is None:
            return
        fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        os.close(self._lock_fd)
        self._lock_fd = None

    async def start(self) -> dict:
        self._loop = asyncio.get_running_loop()
        self._acquire_singleton()
        try:
            previous = self.repo.control()
            manual_pause_reason = (
                str(previous.get("pause_reason") or "")
                if previous.get("dispatcher_state") == "paused"
                and str(previous.get("pause_reason") or "").startswith("manual:")
                else ""
            )
            launch_state = self.repo.docker_launch()["state"]
            if manual_pause_reason and launch_state == "idle":
                # ``manual:`` is an operator acknowledgement boundary (notably
                # for migrated pre-namespace containers).  Zero containers in
                # the new namespace cannot prove those legacy workers are gone,
                # so ordinary startup must neither recover nor arm auto-retry.
                state = self.repo.set_control(
                    dispatcher_state="paused",
                    accepting=True,
                    pause_reason=manual_pause_reason,
                    retry_count=0,
                    retry_at=None,
                )
                self._started = True
                return {
                    "outcome": "paused",
                    "control": state,
                    "recovered": {
                        "requeued": 0,
                        "interrupted": 0,
                        "settling": 0,
                    },
                }
            self.repo.set_control(
                dispatcher_state="starting",
                accepting=False,
                pause_reason="",
                retry_count=0,
                retry_at=None,
            )
            await self.orch.runner.runner.cleanup_instance()
            self.repo.assert_docker_launch_idle()
            await self.orch.runner.runner.ensure_runtime_ready()
            recovered = self.repo.recover_after_namespace_cleanup()
            # Accepting is restored only after physical cleanup and durable
            # attempt compensation.  The remaining application recovery then
            # uses the same ordered pipeline as delayed pause -> resume, notably
            # allowing contest reconciliation to enqueue restored pairings.
            self.repo.resume()
            application_recovered = await self._recover_application_state()
        except (
            SandboxControlUncertain,
            PlatformRunnerError,
            DockerLaunchInvariantError,
        ) as exc:
            # Requests may persist while paused, but none can claim until the
            # exact instance namespace has a verified zero-container result.
            state = self._pause_control_uncertainty(str(exc))
            self._started = True
            return {"outcome": "paused", "control": state}
        except Exception:
            try:
                self.repo.set_control(
                    dispatcher_state="paused",
                    accepting=False,
                    pause_reason="执行队列启动恢复失败；须修复持久状态后重启",
                    retry_count=0,
                    retry_at=None,
                )
            finally:
                self._release_singleton()
            raise
        self._started = True
        return {
            "outcome": "running",
            "recovered": recovered,
            "application_recovered": application_recovered,
        }

    async def stop(self) -> None:
        if not self._started:
            self._release_singleton()
            return
        control = self.repo.control()
        if control["dispatcher_state"] == "paused":
            self.repo.set_control(accepting=False)
        else:
            self.repo.set_control(
                dispatcher_state="stopping",
                accepting=False,
                pause_reason="应用正在停止",
                retry_at=None,
            )
        self._wake.set()

    async def close(self) -> None:
        control = self.repo.control()
        if control["dispatcher_state"] != "paused":
            self.repo.set_control(
                dispatcher_state="stopped",
                accepting=False,
                pause_reason="",
                retry_count=0,
                retry_at=None,
            )
        self._started = False
        self._loop = None
        self._release_singleton()

    def wake(self) -> None:
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._wake.set)
                return
            except RuntimeError:
                pass
        self._wake.set()

    def _pause_control_uncertainty(self, reason: str) -> dict:
        supervisor = getattr(
            getattr(getattr(self.orch, "runner", None), "runner", None),
            "supervisor",
            None,
        )
        manual = False
        if supervisor is not None:
            try:
                manual = bool(supervisor.launch_requires_manual_recovery())
            except Exception:
                # If even the boot/journal classification is unreadable, an
                # unacknowledged intent remains the conservative boundary.
                manual = self.repo.docker_launch()["state"] == "creating"
        return self.repo.pause_for_docker_uncertainty(
            reason, manual=manual
        )

    async def loop(self) -> None:
        while True:
            self._wake.clear()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # keep the one owner alive and diagnosable
                logger.exception("execution dispatcher iteration failed")
                self.repo.pause("执行 dispatcher 本轮异常", bounded_retry=True)
            try:
                await asyncio.wait_for(
                    self._wake.wait(), timeout=EXECUTION_POLL_SECONDS
                )
            except TimeoutError:
                pass

    async def _recover_application_state(self) -> dict[str, int]:
        """Run the post-namespace recovery pipeline used by every resume path.

        Keeping this in the dispatcher prevents startup and delayed recovery
        from drifting apart.  In particular, a Docker pause that recovers hours
        after process startup still resets dead contest pairings and repairs
        finished contests whose official-result transaction never committed.
        The callback is injected to avoid a matches -> contests import cycle.
        """
        legacy_orphans = int(self.store.recover_orphan_matches())
        if legacy_orphans:
            logger.warning(
                "legacy orphan matches recovered after label cleanup: %s",
                legacy_orphans,
            )
        rating_recovered = int(
            await self.orch.recover_unsettled_match_ratings()
        )
        if rating_recovered:
            logger.warning(
                "rating settlements recovered after Docker cleanup: %s",
                rating_recovered,
            )
        contests_reconciled = (
            int(await self.contest_reconciler())
            if self.contest_reconciler is not None
            else 0
        )
        if contests_reconciled:
            logger.info(
                "contest state reconciled after Docker cleanup: %s",
                contests_reconciled,
            )
        return {
            "legacy_orphans": legacy_orphans,
            "ratings": rating_recovered,
            "contests": contests_reconciled,
        }

    async def _resume_paused_if_due(
        self, control: dict, *, administrator: bool = False
    ) -> bool:
        async with self._recovery_lock:
            # The caller's snapshot may have waited behind another recovery.
            # Re-read under the mutex: a completed owner makes every waiter a
            # no-op instead of running a second namespace cleanup.
            control = self.repo.control()
            if control["dispatcher_state"] == "running":
                return True
            if control["dispatcher_state"] != "paused":
                return False
            if (
                str(control.get("pause_reason") or "").startswith("manual:")
                and not administrator
            ):
                return False
            retry_at = str(control.get("retry_at") or "")
            if (
                not administrator
                and retry_at
                and retry_at > datetime.now().isoformat(timespec="seconds")
            ):
                return False
            try:
                # Runtime recovery is deliberately not a live takeover.  Stop
                # and await every task before deleting containers or
                # compensating DB attempts, so stale coroutines cannot write an
                # old match after its request has been requeued.
                await self.orch.quiesce_execution_tasks()
                await self.orch.runner.runner.cleanup_instance()
                self.repo.assert_docker_launch_idle()
                await self.orch.runner.runner.ensure_runtime_ready()
                recovered = self.repo.recover_after_namespace_cleanup()
                logger.warning("execution namespace recovered: %s", recovered)
                # Keep the durable state paused until every awaited business
                # reconciliation completes.  Otherwise a concurrent run_once
                # can claim between resume() and contest/rating recovery.
                await self._recover_application_state()
                self.repo.resume()
                return True
            except (
                SandboxControlUncertain,
                PlatformRunnerError,
                DockerLaunchInvariantError,
            ) as exc:
                self._pause_control_uncertainty(str(exc))
                return False
            except Exception:
                logger.exception("execution application-state recovery failed")
                self.repo.pause(
                    "执行队列业务状态对账失败；等待下一次安全恢复",
                    bounded_retry=True,
                )
                return False

    async def admin_resume(self) -> bool:
        """Retry exact cleanup now; never clear a pause by assertion alone."""
        control = self.repo.control()
        if control["dispatcher_state"] != "paused":
            return control["dispatcher_state"] == "running"
        return await self._resume_paused_if_due(control, administrator=True)

    async def _process_cancellations(self) -> None:
        snapshot = self.repo.snapshot(
            max_match_slots=self.max_match_slots,
            max_sandbox_units=self.max_sandbox_units,
            aging_seconds=EXECUTION_AGING_SECONDS,
        )
        for job in snapshot["active"]:
            if not int(job.get("cancel_requested") or 0):
                continue
            match_id = str(job.get("current_match_id") or "")
            if not match_id:
                continue
            try:
                await self.orch.abort_match(match_id)
            except ValueError:
                logger.info("cancel converged elsewhere match=%s", match_id)

    async def run_once(self) -> dict:
        control = self.repo.control()
        if control["dispatcher_state"] == "paused":
            if not await self._resume_paused_if_due(control):
                return {"outcome": "paused"}
            control = self.repo.control()
        if control["dispatcher_state"] != "running":
            return {"outcome": str(control["dispatcher_state"])}

        try:
            self.repo.assert_docker_launch_idle()
        except DockerLaunchInvariantError as exc:
            self._pause_control_uncertainty(str(exc))
            return {"outcome": "paused"}

        await self._process_cancellations()
        finalized = self.repo.finalize_ready()
        refill: dict = {"outcome": "capability_disabled", "inserted": 0}
        if self.auto_capability_enabled:
            refill = self.repo.refill_auto(
                target_queued=EXECUTION_AUTO_LOOKAHEAD,
                bootstrap_target_matches=AUTO_MATCH_BOOTSTRAP_TARGET_MATCHES,
            )

        claimed = 0
        while True:
            job = self.repo.claim_next(
                max_match_slots=self.max_match_slots,
                max_sandbox_units=self.max_sandbox_units,
                aging_seconds=EXECUTION_AGING_SECONDS,
                user_active_limit=EXECUTION_USER_ACTIVE_LIMIT,
                contest_share_slots=EXECUTION_CONTEST_SHARE_SLOTS,
            )
            if job is None:
                break
            try:
                self.orch.start_execution_job(job)
            except Exception as exc:
                if not self.repo.rollback_unstarted_claim(
                    str(job["public_id"]), reason=f"task_start:{type(exc).__name__}"
                ):
                    self.repo.pause(
                        "claim 后 runner task 启动结果不确定",
                        bounded_retry=True,
                    )
                    raise ExecutionInvariantError(
                        "claimed execution could not be compensated"
                    ) from exc
                logger.exception(
                    "execution task start failed and compensated request=%s",
                    job["public_id"],
                )
                break
            claimed += 1
        return {
            "outcome": "ok",
            "claimed": claimed,
            "finalized": finalized,
            "auto_refill": refill,
        }

    def _capacity_snapshot(self, *, public_id: str | None = None) -> dict:
        return self.repo.snapshot(
            max_match_slots=self.max_match_slots,
            max_sandbox_units=self.max_sandbox_units,
            aging_seconds=EXECUTION_AGING_SECONDS,
            public_id=public_id,
        )

    @staticmethod
    def _public_capacity(raw: dict) -> dict:
        return {
            "match_slots": {
                "used": int(raw.get("occupied_match_slots") or 0),
                "capacity": int(raw.get("max_match_slots") or 0),
            },
            "sandbox_units": {
                "used": int(raw.get("used_sandbox_units") or 0),
                "capacity": int(raw.get("max_sandbox_units") or 0),
            },
            "running_matches": int(raw.get("running_matches") or 0),
        }

    @staticmethod
    def _eta(ahead_jobs: int, max_slots: int) -> dict:
        waves = ahead_jobs // max(1, max_slots)
        return {
            "min_seconds": waves * 30,
            "max_seconds": max(60, (waves + 1) * 300),
            "dynamic": True,
            "note": "区间会随对局时长、优先级与资源变化",
        }

    @staticmethod
    def _public_job(job: dict | None) -> dict | None:
        if job is None:
            return None
        public_id = str(job.get("public_id") or "")
        return {
            "public_id": public_id,
            "request_id": public_id,
            "source": str(job.get("source") or ""),
            "status": str(job.get("status") or ""),
            "game_id": str(job.get("game_id") or ""),
            "match_type": str(job.get("match_type") or ""),
            "match_id": job.get("current_match_id"),
            "sandbox_units": int(job.get("sandbox_units") or 0),
            "rated": bool(int(job.get("rated") or 0)),
            "rating_reason": str(job.get("rating_reason") or ""),
            "retryable": bool(int(job.get("retryable") or 0)),
            "cancel_requested": bool(int(job.get("cancel_requested") or 0)),
            "reason": str(job.get("terminal_reason") or ""),
            "created_at": job.get("created_at"),
            "started_at": job.get("started_at"),
            "terminal_at": job.get("terminal_at"),
        }

    def public_request(self, public_id: str) -> dict | None:
        snap = self._capacity_snapshot(public_id=public_id)
        if snap["target"] is None:
            return None
        ahead_jobs = int(snap["ahead_jobs"])
        return {
            "public_id": public_id,
            "request": self._public_job(snap["target"]),
            "ahead_jobs": ahead_jobs,
            "ahead_sandbox_units": int(snap["ahead_sandbox_units"]),
            "capacity": self._public_capacity(snap["capacity"]),
            "eta": self._eta(ahead_jobs, self.max_match_slots),
        }

    def public_snapshot(
        self,
        *,
        game_id: str | None = None,
        include_internal: bool = False,
    ) -> dict:
        snap = self._capacity_snapshot()
        control = snap["control"]
        active = [
            row for row in snap["active"]
            if game_id is None or row.get("game_id") == game_id
        ]
        queued = [
            row for row in snap["queued"]
            if game_id is None or row.get("game_id") == game_id
        ]
        pause_reason = str(control.get("pause_reason") or "")
        if pause_reason and not include_internal:
            pause_reason = "执行服务暂时不可用，系统正在自动恢复"
        return {
            "dispatcher": {
                "state": str(control.get("dispatcher_state") or "stopped"),
                "accepting": bool(int(control.get("accepting") or 0)),
                "auto_enabled": bool(int(control.get("auto_enabled") or 0)),
                "pause_reason": pause_reason,
                "retry_at": control.get("retry_at"),
            },
            "capacity": self._public_capacity(snap["capacity"]),
            "active": [self._public_job(row) for row in active],
            "queued": [self._public_job(row) for row in queued],
            "queued_count": len(queued),
        }


__all__ = [
    "DispatcherAlreadyRunning",
    "ExecutionDispatcher",
]
