"""Durable, source-neutral execution queue repository.

The public request survives individual match attempts.  A match/index/replay/
rating-policy row is created only by :meth:`claim_next`, inside the same
``BEGIN IMMEDIATE`` transaction that moves the request to ``starting``.  The
repository deliberately contains no process lease, Docker PID, daemon boot id,
or filesystem path in its public projections.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from bzplat.backend.runtime.binary_integrity import (
    BinaryIntegrityCacheKey,
    require_binary_file_integrity,
)
from bzplat.backend.runtime.config import (
    AUTO_MATCH_COOLDOWN_SECONDS,
    AUTO_MATCH_CONTEST_GUARD_SECONDS,
    AUTO_MATCH_IDLE_GRACE_SECONDS,
    AUTO_MATCH_SCHEDULER_POLICY_VERSION,
    EXECUTION_AUTO_ACTIVE_LIMIT,
    EXECUTION_AUTO_LOOKAHEAD,
)
from .db import (
    _active_game_contract_tx,
    _all_game_ids,
    _matches_table,
    _now,
    _registered_game_id,
    _row,
)
from .schema import (
    AUTO_IDLE_POLICY_CUTOVER_REASON,
    AUTO_YIELD_FOREGROUND_REASON,
    EXECUTION_ACTIVE_STATES,
    EXECUTION_CANCELLED,
    EXECUTION_COMPLETED,
    EXECUTION_INTERRUPTED,
    EXECUTION_QUEUED,
    EXECUTION_RUNNING,
    EXECUTION_SETTLING,
    EXECUTION_SOURCE_AUTO,
    EXECUTION_SOURCE_CONTEST,
    EXECUTION_SOURCE_HUMAN,
    EXECUTION_SOURCE_MANUAL,
    EXECUTION_SOURCES,
    EXECUTION_STARTING,
    EXECUTION_ENV_HUMAN,
    EXECUTION_ENV_PLATFORM_HIGH,
    EXECUTION_ENV_PLATFORM_LOW,
    EXECUTION_ENV_REMOTE_LOCAL,
    EXECUTION_PROFILE_VERSION,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    SUPPORTED_BINARY_ARCH,
    SUPPORTED_BINARY_FORMAT,
    SUPPORTED_BINARY_OS,
    TYPE_CONTEST,
    TYPE_HUMAN,
    TYPE_LADDER,
    VALID_RUNTIME_MODES,
)
from .validation import exact_nonnegative_int

if TYPE_CHECKING:
    from .db import Store


SOURCE_PRIORITY = {
    EXECUTION_SOURCE_MANUAL: 40,
    EXECUTION_SOURCE_HUMAN: 40,
    EXECUTION_SOURCE_CONTEST: 30,
    EXECUTION_SOURCE_AUTO: 10,
}

_PLATFORM_ENVIRONMENTS = frozenset(
    {EXECUTION_ENV_PLATFORM_LOW, EXECUTION_ENV_PLATFORM_HIGH}
)
_MANUAL_ENVIRONMENTS = frozenset(
    {EXECUTION_ENV_PLATFORM_LOW, EXECUTION_ENV_REMOTE_LOCAL}
)
_FOREGROUND_SOURCES = frozenset(
    {EXECUTION_SOURCE_MANUAL, EXECUTION_SOURCE_HUMAN, EXECUTION_SOURCE_CONTEST}
)
_AUTO_YIELD_REASONS = frozenset(
    {AUTO_IDLE_POLICY_CUTOVER_REASON, AUTO_YIELD_FOREGROUND_REASON}
)
_AUTO_GATE_BUSY = "busy"


class ExecutionQueueClosed(ValueError):
    """The process is starting/stopping and must not accept a new request."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "execution_queue_closed",
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


class ExecutionMaintenanceConflict(ValueError):
    """A requested maintenance transition is not safe in the current state."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


class ExecutionInvariantError(RuntimeError):
    """Persisted execution state violates a fail-closed invariant."""


class ExecutionAttemptNotCurrent(ExecutionInvariantError):
    """A claimed attempt lost ownership before a physical launch boundary."""

    code = "execution_attempt_not_current"


class DockerLaunchInvariantError(ExecutionInvariantError):
    """The singleton Docker launch journal has not physically converged."""


class ExecutionIdempotencyConflict(ValueError):
    """A caller reused one opaque request id for a different request."""


def _new_public_id() -> str:
    # 144 bits of opaque entropy; URL-safe and intentionally unrelated to the
    # internal AUTOINCREMENT key or match timestamp.
    return "req_" + secrets.token_urlsafe(18).rstrip("=")


def _new_match_id() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)


def _parse_time(value: Any) -> datetime:
    try:
        return datetime.fromisoformat(str(value or ""))
    except ValueError:
        return datetime.min


class ExecutionRepository:
    """Transactional operations for all executable match sources."""

    def __init__(
        self,
        store: Store,
        *,
        local_agent_available: Callable[[int], bool] | None = None,
    ) -> None:
        self.store = store
        # The repository owns durable identities and leases, but never reaches
        # into the process-local WebSocket hub.  The application injects one
        # cheap synchronous availability snapshot; absence fails remote jobs
        # closed while ordinary Docker work keeps flowing.
        self._local_agent_available = local_agent_available
        # Claim/refill run inside one SQLite write transaction.  Re-hashing up
        # to 100 MiB per candidate on every dispatcher tick would extend that
        # lock by seconds or gigabytes of I/O.  The helper's cache identity
        # includes device/inode/size/mtime/ctime, so replacement and in-place
        # tampering still force a fresh digest while stable immutable versions
        # remain a bounded O(1) check.
        self._binary_integrity_cache: set[BinaryIntegrityCacheKey] = set()

    def set_local_agent_available(
        self, callback: Callable[[int], bool] | None
    ) -> None:
        self._local_agent_available = callback

    def _is_local_agent_available(self, agent_id: int) -> bool:
        callback = self._local_agent_available
        if callback is None:
            return False
        try:
            return bool(callback(int(agent_id)))
        except Exception:
            # A volatile hub lookup must never tear down the durable dispatcher.
            return False

    @staticmethod
    def _foreground_where(alias: str = "") -> str:
        prefix = f"{alias}." if alias else ""
        return (
            f"({prefix}source IN ('manual','human') OR "
            f"({prefix}source='contest' AND NOT EXISTS ("
            "SELECT 1 FROM contests auto_showcase "
            f"WHERE auto_showcase.id={prefix}contest_id "
            "AND auto_showcase.showcase_key IS NOT NULL)))"
        )

    @classmethod
    def _foreground_busy_tx(cls, conn: sqlite3.Connection) -> bool:
        queued_or_active = bool(
            conn.execute(
                "SELECT 1 FROM execution_jobs j WHERE "
                + cls._foreground_where("j")
                + " AND j.status IN ('queued','starting','running','settling') "
                "LIMIT 1"
            ).fetchone()
        )
        return queued_or_active or cls._contest_guard_tx(conn)

    @staticmethod
    def _contest_guard_tx(conn: sqlite3.Connection) -> bool:
        protection_horizon = (
            datetime.now() + timedelta(seconds=AUTO_MATCH_CONTEST_GUARD_SECONDS)
        ).isoformat(timespec="seconds")
        return bool(
            conn.execute(
                "SELECT 1 FROM contests c WHERE c.showcase_key IS NULL AND ("
                "c.status IN ('running','rest') OR (c.status='published' "
                "AND c.starts_at IS NOT NULL AND c.starts_at<=? AND EXISTS ("
                "SELECT 1 FROM contest_pairings p WHERE p.contest_id=c.id "
                "AND p.status='pending' AND p.match_id IS NULL "
                "AND (p.scheduled_at IS NULL OR p.scheduled_at<=?)))) LIMIT 1",
                (protection_horizon, protection_horizon),
            ).fetchone()
        )

    @classmethod
    def _latest_foreground_terminal_tx(
        cls, conn: sqlite3.Connection
    ) -> str | None:
        row = conn.execute(
            "SELECT MAX(j.terminal_at) AS terminal_at FROM execution_jobs j WHERE "
            + cls._foreground_where("j")
            + " AND j.status IN ('completed','cancelled','interrupted')"
        ).fetchone()
        value = str(row["terminal_at"] or "") if row is not None else ""
        return value or None

    @classmethod
    def _advance_auto_gate_tx(
        cls,
        conn: sqlite3.Connection,
        *,
        seconds: int,
        reason: str,
        now: datetime | None = None,
    ) -> str:
        if reason not in {"idle_grace", "cooldown"}:
            raise ValueError(f"unknown auto gate reason: {reason}")
        current = conn.execute(
            "SELECT next_eligible_at,gate_reason FROM auto_match_fair_state "
            "WHERE singleton=1"
        ).fetchone()
        if current is None:
            raise ExecutionInvariantError("auto fair singleton missing")
        target = (now or datetime.now()) + timedelta(seconds=max(0, int(seconds)))
        existing = _parse_time(current["next_eligible_at"])
        if cls._foreground_busy_tx(conn):
            next_eligible = max(existing, target).isoformat(timespec="seconds")
            next_reason = _AUTO_GATE_BUSY
        elif existing > target:
            next_eligible = existing.isoformat(timespec="seconds")
            next_reason = (
                reason
                if str(current["gate_reason"] or "") == _AUTO_GATE_BUSY
                else str(current["gate_reason"] or "idle_grace")
            )
        else:
            next_eligible = target.isoformat(timespec="seconds")
            next_reason = reason
        conn.execute(
            "UPDATE auto_match_fair_state SET next_eligible_at=?,gate_reason=?,"
            "updated_at=? WHERE singleton=1",
            (next_eligible, next_reason, _now()),
        )
        return next_eligible

    @staticmethod
    def _mark_auto_busy_tx(
        conn: sqlite3.Connection, *, now: datetime | None = None
    ) -> str:
        current = conn.execute(
            "SELECT next_eligible_at,gate_reason FROM auto_match_fair_state "
            "WHERE singleton=1"
        ).fetchone()
        if current is None:
            raise ExecutionInvariantError("auto fair singleton missing")
        existing = _parse_time(current["next_eligible_at"])
        if (
            str(current["gate_reason"] or "") == _AUTO_GATE_BUSY
            and existing != datetime.min
        ):
            return existing.isoformat(timespec="seconds")
        target = (now or datetime.now()) + timedelta(
            seconds=AUTO_MATCH_IDLE_GRACE_SECONDS
        )
        next_eligible = max(existing, target).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE auto_match_fair_state SET next_eligible_at=?,gate_reason=?,"
            "updated_at=? WHERE singleton=1",
            (next_eligible, _AUTO_GATE_BUSY, _now()),
        )
        return next_eligible

    @staticmethod
    def _yield_auto_jobs_tx(
        conn: sqlite3.Connection, *, reason: str
    ) -> dict[str, int]:
        if reason not in _AUTO_YIELD_REASONS:
            raise ValueError("unsupported automatic execution yield reason")
        terminal = _now()
        queued_decisions = [
            int(row["auto_decision_id"])
            for row in conn.execute(
                "SELECT auto_decision_id FROM execution_jobs WHERE source='auto' "
                "AND status='queued' AND auto_decision_id IS NOT NULL"
            ).fetchall()
        ]
        if queued_decisions:
            marks = ",".join("?" for _ in queued_decisions)
            conn.execute(
                "UPDATE auto_match_decisions SET lifecycle='cancelled',"
                "terminal_reason=?,terminal_at=? WHERE lifecycle='queued' "
                f"AND id IN ({marks})",
                (reason, terminal, *queued_decisions),
            )
        queued = conn.execute(
            "UPDATE execution_jobs SET status='cancelled',cancel_requested=1,"
            "retryable=0,next_attempt_at=NULL,terminal_reason=?,last_error='',"
            "terminal_at=? WHERE source='auto' AND status='queued'",
            (reason, terminal),
        ).rowcount
        active = conn.execute(
            "UPDATE execution_jobs SET cancel_requested=1,terminal_reason=? "
            "WHERE source='auto' AND status IN ('starting','running') "
            "AND cancel_requested=0",
            (reason,),
        ).rowcount
        return {"queued_cancelled": int(queued), "active_yielding": int(active)}

    @classmethod
    def _yield_auto_to_foreground_tx(
        cls, conn: sqlite3.Connection
    ) -> dict[str, Any]:
        outcome: dict[str, Any] = cls._yield_auto_jobs_tx(
            conn, reason=AUTO_YIELD_FOREGROUND_REASON
        )
        outcome["next_eligible_at"] = cls._mark_auto_busy_tx(conn)
        return outcome

    @classmethod
    def _auto_scheduler_tx(cls, conn: sqlite3.Connection) -> dict[str, Any]:
        control = conn.execute(
            "SELECT auto_enabled FROM execution_control WHERE singleton=1"
        ).fetchone()
        fair = conn.execute(
            "SELECT * FROM auto_match_fair_state WHERE singleton=1"
        ).fetchone()
        if control is None or fair is None:
            raise ExecutionInvariantError("auto scheduler singleton missing")

        active = int(
            conn.execute(
                "SELECT COUNT(*) FROM execution_jobs WHERE source='auto' "
                "AND status IN ('starting','running','settling')"
            ).fetchone()[0]
        )
        queued = int(
            conn.execute(
                "SELECT COUNT(*) FROM execution_jobs WHERE source='auto' "
                "AND status='queued' AND cancel_requested=0"
            ).fetchone()[0]
        )
        yielding = conn.execute(
            "SELECT terminal_reason FROM execution_jobs WHERE source='auto' "
            "AND status IN ('starting','running','settling') "
            "AND cancel_requested=1 AND terminal_reason IN (?,?) "
            "ORDER BY id LIMIT 1",
            tuple(sorted(_AUTO_YIELD_REASONS)),
        ).fetchone()
        yielding_reason = (
            str(yielding["terminal_reason"] or "") if yielding is not None else ""
        )
        contest_guard = cls._contest_guard_tx(conn)
        foreground_busy = cls._foreground_busy_tx(conn)
        candidates: list[tuple[datetime, str]] = []
        persisted = _parse_time(fair["next_eligible_at"])
        if persisted != datetime.min:
            candidates.append(
                (persisted, str(fair["gate_reason"] or "idle_grace"))
            )
        latest_foreground = cls._latest_foreground_terminal_tx(conn)
        if latest_foreground:
            candidates.append(
                (
                    _parse_time(latest_foreground)
                    + timedelta(seconds=AUTO_MATCH_IDLE_GRACE_SECONDS),
                    "idle_grace",
                )
            )
        latest_auto = conn.execute(
            "SELECT MAX(terminal_at) AS terminal_at FROM execution_jobs "
            "WHERE source='auto' AND status IN ('completed','cancelled','interrupted')"
        ).fetchone()
        latest_auto_terminal = (
            str(latest_auto["terminal_at"] or "") if latest_auto is not None else ""
        )
        if latest_auto_terminal:
            candidates.append(
                (
                    _parse_time(latest_auto_terminal)
                    + timedelta(seconds=AUTO_MATCH_COOLDOWN_SECONDS),
                    "cooldown",
                )
            )
        next_dt, gate_reason = max(candidates, default=(datetime.min, "idle_grace"))
        busy_marker = str(fair["gate_reason"] or "") == _AUTO_GATE_BUSY
        next_eligible_at = (
            next_dt.isoformat(timespec="seconds") if next_dt != datetime.min else None
        )
        now = datetime.now()
        if not bool(int(control["auto_enabled"] or 0)):
            # Turning the producer off does not create a new cancellation, but
            # it must not hide a yield that already won the enqueue/claim race.
            state, reason = "disabled", yielding_reason or "auto_disabled"
        elif foreground_busy:
            state = "contest_guard" if contest_guard else "foreground_busy"
            reason = yielding_reason or (
                "contest_guard" if contest_guard else "foreground_queued_or_active"
            )
        elif yielding_reason:
            state, reason = "yielding", yielding_reason
        elif active:
            state, reason = "running", "auto_running"
        elif busy_marker:
            state, reason = "cooldown", "idle_grace"
        elif now < next_dt:
            state = "cooldown"
            reason = "auto_cooldown" if gate_reason == "cooldown" else "idle_grace"
        else:
            state, reason = "ready", "idle_ready"
        return {
            "mode": "idle_only",
            "state": state,
            "reason": reason,
            "idle_required_seconds": AUTO_MATCH_IDLE_GRACE_SECONDS,
            "cooldown_seconds": AUTO_MATCH_COOLDOWN_SECONDS,
            "max_active": EXECUTION_AUTO_ACTIVE_LIMIT,
            "queued_target": EXECUTION_AUTO_LOOKAHEAD,
            "next_eligible_at": next_eligible_at,
            "active_count": active,
            "queued_count": queued,
            "policy_version": str(fair["dispatch_policy_version"] or ""),
        }

    def reconcile_auto_scheduler_policy(self) -> dict[str, Any]:
        """Install the idle-only generation once and reconcile legacy backlog."""
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            fair = conn.execute(
                "SELECT dispatch_policy_version,gate_reason "
                "FROM auto_match_fair_state "
                "WHERE singleton=1"
            ).fetchone()
            if fair is None:
                raise ExecutionInvariantError("auto fair singleton missing")
            changed = (
                str(fair["dispatch_policy_version"] or "")
                != AUTO_MATCH_SCHEDULER_POLICY_VERSION
            )
            yielded = {"queued_cancelled": 0, "active_yielding": 0}
            foreground_busy = self._foreground_busy_tx(conn)
            if changed:
                yielded = self._yield_auto_jobs_tx(
                    conn, reason=AUTO_IDLE_POLICY_CUTOVER_REASON
                )
                next_eligible = self._advance_auto_gate_tx(
                    conn,
                    seconds=AUTO_MATCH_IDLE_GRACE_SECONDS,
                    reason="idle_grace",
                )
                conn.execute(
                    "UPDATE auto_match_fair_state SET dispatch_policy_version=?,"
                    "updated_at=? WHERE singleton=1",
                    (AUTO_MATCH_SCHEDULER_POLICY_VERSION, _now()),
                )
                if foreground_busy:
                    next_eligible = self._mark_auto_busy_tx(conn)
            else:
                next_eligible = None
                if foreground_busy:
                    yielded = self._yield_auto_jobs_tx(
                        conn, reason=AUTO_YIELD_FOREGROUND_REASON
                    )
                    next_eligible = self._mark_auto_busy_tx(conn)
                elif str(fair["gate_reason"] or "") == _AUTO_GATE_BUSY:
                    next_eligible = self._advance_auto_gate_tx(
                        conn,
                        seconds=AUTO_MATCH_IDLE_GRACE_SECONDS,
                        reason="idle_grace",
                    )
            scheduler = self._auto_scheduler_tx(conn)
            return {
                "changed": changed,
                **yielded,
                "next_eligible_at": next_eligible or scheduler["next_eligible_at"],
                "auto_scheduler": scheduler,
            }

    @staticmethod
    def _backoff_contest_pairing_tx(
        conn: sqlite3.Connection, job: dict, *, seconds: int = 30
    ) -> None:
        if job.get("source") != EXECUTION_SOURCE_CONTEST:
            return
        backoff_at = (
            datetime.now() + timedelta(seconds=max(1, int(seconds)))
        ).isoformat(timespec="seconds")
        conn.execute(
            "UPDATE contest_pairings SET scheduled_at=CASE "
            "WHEN scheduled_at IS NULL OR scheduled_at<? THEN ? "
            "ELSE scheduled_at END WHERE id=? AND contest_id=? "
            "AND status='pending' AND match_id IS NULL",
            (
                backoff_at,
                backoff_at,
                job.get("contest_pairing_id"),
                job.get("contest_id"),
            ),
        )

    # ------------------------------------------------------------------
    # Control plane
    # ------------------------------------------------------------------
    @staticmethod
    def _docker_launch_tx(conn: sqlite3.Connection) -> dict:
        row = conn.execute(
            "SELECT * FROM docker_launch_journal WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise DockerLaunchInvariantError(
                "docker_launch_journal singleton missing"
            )
        return dict(row)

    @staticmethod
    def _clear_docker_launch_tx(
        conn: sqlite3.Connection, *, launch_token: str
    ) -> None:
        changed = conn.execute(
            "UPDATE docker_launch_journal SET state='idle',launch_token=NULL,"
            "instance_key=NULL,owner_kind=NULL,job_public_id=NULL,"
            "attempt_no=NULL,slot=NULL,container_name=NULL,host_boot_id=NULL,"
            "updated_at=? WHERE singleton=1 AND launch_token=?",
            (_now(), launch_token),
        )
        if changed.rowcount != 1:
            raise DockerLaunchInvariantError("Docker launch token CAS lost")

    def docker_launch(self) -> dict:
        with self.store._tx() as conn:
            return self._docker_launch_tx(conn)

    def assert_docker_launch_idle(self) -> None:
        with self.store._tx() as conn:
            launch = self._docker_launch_tx(conn)
            if launch["state"] != "idle":
                raise DockerLaunchInvariantError(
                    "Docker launch journal 尚未收敛"
                )

    def begin_docker_launch(
        self,
        *,
        launch_token: str,
        instance_key: str,
        owner_kind: str,
        job_public_id: str,
        attempt_no: int,
        slot: int,
        container_name: str,
        host_boot_id: str,
    ) -> dict:
        """Persist create intent before the daemon can observe a request."""
        if owner_kind not in {"execution", "preflight"}:
            raise ValueError(f"unknown Docker launch owner: {owner_kind}")
        values = (
            str(launch_token),
            str(instance_key),
            owner_kind,
            str(job_public_id),
            int(attempt_no),
            int(slot),
            str(container_name),
            str(host_boot_id),
            _now(),
        )
        if not all(str(value).strip() for value in values[:4]) or not str(
            host_boot_id
        ).strip():
            raise ValueError("Docker launch intent fields must be non-empty")
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            launch = self._docker_launch_tx(conn)
            control = conn.execute(
                "SELECT dispatcher_state,accepting,auto_enabled,"
                "deployment_drain_requested FROM execution_control "
                "WHERE singleton=1"
            ).fetchone()
            online_runtime = bool(
                control is not None
                and control["dispatcher_state"] == "running"
            )
            cold_preflight = bool(
                control is not None
                and owner_kind == "preflight"
                and control["dispatcher_state"] == "stopped"
                and int(control["accepting"] or 0) == 0
                and int(control["auto_enabled"] or 0) == 0
                and int(control["deployment_drain_requested"] or 0) == 1
            )
            # A contract-cutover preflight is the sole Docker create allowed
            # while the dispatcher is stopped.  The operator service must also
            # hold the DB-adjacent dispatcher flock; these durable predicates
            # ensure an arbitrary stopped process cannot use this exception.
            if not online_runtime and not cold_preflight:
                raise DockerLaunchInvariantError(
                    "dispatcher 未运行，禁止创建 Docker 容器"
                )
            # The singleton journal is the host-wide physical fence.  Preserve
            # that uncertainty even when this particular execution has since
            # yielded; a benign stale-attempt result must never mask an
            # unclosed create intent owned by another launch.
            if launch["state"] != "idle":
                raise DockerLaunchInvariantError(
                    "前一 Docker launch 尚未收敛"
                )
            if owner_kind == "execution":
                job = conn.execute(
                    "SELECT status,attempt_count,cancel_requested "
                    "FROM execution_jobs WHERE public_id=?",
                    (str(job_public_id),),
                ).fetchone()
                if (
                    job is None
                    # Settling is durable-active for capacity/finalization but
                    # no longer owns a physical launch boundary.
                    or job["status"] not in {EXECUTION_STARTING, EXECUTION_RUNNING}
                    or int(job["attempt_count"] or 0) != int(attempt_no)
                    or int(job["cancel_requested"] or 0) != 0
                ):
                    raise ExecutionAttemptNotCurrent(
                        "execution attempt is no longer current"
                    )
            conn.execute(
                "UPDATE docker_launch_journal SET state='creating',"
                "launch_token=?,instance_key=?,owner_kind=?,job_public_id=?,"
                "attempt_no=?,slot=?,container_name=?,host_boot_id=?,updated_at=? "
                "WHERE singleton=1 AND state='idle'",
                values,
            )
            return self._docker_launch_tx(conn)

    def mark_docker_launch_created(self, launch_token: str) -> dict:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            launch = self._docker_launch_tx(conn)
            if launch["launch_token"] != launch_token:
                raise DockerLaunchInvariantError("Docker launch token mismatch")
            if launch["state"] == "creating":
                conn.execute(
                    "UPDATE docker_launch_journal SET state='created',updated_at=? "
                    "WHERE singleton=1 AND state='creating' AND launch_token=?",
                    (_now(), launch_token),
                )
            elif launch["state"] != "created":
                raise DockerLaunchInvariantError(
                    "Docker launch cannot become created from idle"
                )
            return self._docker_launch_tx(conn)

    def clear_docker_launch_created(self, launch_token: str) -> None:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            launch = self._docker_launch_tx(conn)
            if (
                launch["state"] != "created"
                or launch["launch_token"] != launch_token
            ):
                raise DockerLaunchInvariantError(
                    "only an acknowledged Docker launch may be cleared"
                )
            self._clear_docker_launch_tx(conn, launch_token=launch_token)

    def clear_docker_launch_after_boot_change(
        self,
        launch_token: str,
        *,
        previous_boot_id: str,
        current_boot_id: str,
    ) -> None:
        """Clear an unacknowledged create only after host restart and zero proof."""
        if not current_boot_id or current_boot_id == previous_boot_id:
            raise DockerLaunchInvariantError(
                "same host boot cannot clear an ambiguous Docker create"
            )
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            launch = self._docker_launch_tx(conn)
            if (
                launch["state"] != "creating"
                or launch["launch_token"] != launch_token
                or launch["host_boot_id"] != previous_boot_id
            ):
                raise DockerLaunchInvariantError(
                    "ambiguous Docker launch boot/token mismatch"
                )
            self._clear_docker_launch_tx(conn, launch_token=launch_token)

    def pause_for_docker_uncertainty(
        self, reason: str, *, manual: bool | None = None
    ) -> dict:
        """Persist the fail-closed admission boundary for Docker uncertainty."""
        if manual is None:
            manual = self.docker_launch()["state"] == "creating"
        message = str(reason)[:900]
        if manual and not message.startswith("manual:"):
            message = "manual:Docker create 结果不确定；" + message
        return self.pause(message, bounded_retry=not manual)

    def control(self) -> dict:
        with self.store._tx() as conn:
            row = conn.execute(
                "SELECT * FROM execution_control WHERE singleton=1"
            ).fetchone()
            if row is None:
                raise ExecutionInvariantError("execution_control singleton missing")
            return dict(row)

    def set_control(
        self,
        *,
        dispatcher_state: str | None = None,
        accepting: bool | None = None,
        pause_reason: str | None = None,
        retry_count: int | None = None,
        retry_at: str | None | object = ...,
    ) -> dict:
        fields: dict[str, Any] = {}
        if dispatcher_state is not None:
            fields["dispatcher_state"] = dispatcher_state
        if accepting is not None:
            fields["accepting"] = 1 if accepting else 0
        if pause_reason is not None:
            fields["pause_reason"] = str(pause_reason)[:1000]
        if retry_count is not None:
            fields["retry_count"] = max(0, int(retry_count))
        if retry_at is not ...:
            fields["retry_at"] = retry_at
        fields["updated_at"] = _now()
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if fields.get("accepting") == 1:
                drain = conn.execute(
                    "SELECT deployment_drain_requested FROM execution_control "
                    "WHERE singleton=1"
                ).fetchone()
                if drain is None:
                    raise ExecutionInvariantError(
                        "execution_control singleton missing"
                    )
                if int(drain["deployment_drain_requested"] or 0):
                    fields["accepting"] = 0
            conn.execute(
                "UPDATE execution_control SET "
                + ",".join(f"{name}=?" for name in fields)
                + " WHERE singleton=1",
                tuple(fields.values()),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM execution_control WHERE singleton=1"
                ).fetchone()
            )

    def pause(
        self,
        reason: str,
        *,
        bounded_retry: bool = True,
        force_closed: bool = False,
    ) -> dict:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT retry_count,dispatcher_state,accepting,pause_reason,"
                "deployment_drain_requested "
                "FROM execution_control WHERE singleton=1"
            ).fetchone()
            existing_manual = bool(
                current
                and current["dispatcher_state"] == "paused"
                and str(current["pause_reason"] or "").startswith("manual:")
            )
            if existing_manual:
                # Once a same-boot create ambiguity has crossed the manual
                # boundary, a later generic cleanup callback must not silently
                # arm automatic retry or replace the diagnostic evidence.
                bounded_retry = False
                reason = str(current["pause_reason"])
            retries = int(current["retry_count"] or 0) + 1 if current else 1
            delay = min(60, 2 ** min(retries - 1, 6)) if bounded_retry else 0
            retry_at = (
                (datetime.now() + timedelta(seconds=delay)).isoformat(
                    timespec="seconds"
                )
                if bounded_retry
                else None
            )
            accepting = 0 if force_closed else 1
            if not force_closed and current and (
                int(current["deployment_drain_requested"] or 0) == 1
                or
                current["dispatcher_state"] in ("stopped", "stopping")
                or (
                    current["dispatcher_state"] == "paused"
                    and int(current["accepting"] or 0) == 0
                )
            ):
                # Shutdown is a hard admission boundary even when cleanup
                # becomes uncertain while draining an in-flight attempt.
                accepting = 0
            conn.execute(
                "UPDATE execution_control SET dispatcher_state='paused',"
                "accepting=?,pause_reason=?,retry_count=?,retry_at=?,updated_at=? "
                "WHERE singleton=1",
                (accepting, str(reason)[:1000], retries, retry_at, _now()),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM execution_control WHERE singleton=1"
                ).fetchone()
            )

    def resume(self) -> dict:
        self.assert_docker_launch_idle()
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT deployment_drain_requested FROM execution_control "
                "WHERE singleton=1"
            ).fetchone()
            if current is None:
                raise ExecutionInvariantError(
                    "execution_control singleton missing"
                )
            conn.execute(
                "UPDATE execution_control SET dispatcher_state='running',"
                "accepting=?,pause_reason='',retry_count=0,retry_at=NULL,"
                "updated_at=? WHERE singleton=1",
                (
                    0
                    if int(current["deployment_drain_requested"] or 0)
                    else 1,
                    _now(),
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM execution_control WHERE singleton=1"
                ).fetchone()
            )

    @staticmethod
    def is_maintenance_control(control: dict[str, Any]) -> bool:
        return int(control.get("deployment_drain_requested") or 0) == 1

    def maintenance_status(self) -> dict[str, Any]:
        """Return durable blockers; readiness is always derived, never stored."""
        with self.store._tx() as conn:
            control_row = conn.execute(
                "SELECT * FROM execution_control WHERE singleton=1"
            ).fetchone()
            if control_row is None:
                raise ExecutionInvariantError(
                    "execution_control singleton missing"
                )
            return self.maintenance_status_tx(
                conn, current=dict(control_row)
            )

    def begin_maintenance(self, reason: str) -> dict:
        """Atomically request drain and close every source of new execution.

        The same ``BEGIN IMMEDIATE`` transaction is used by enqueue and claim,
        so success is the durable no-new-work boundary.  Work already active
        finishes normally; queued requests remain unchanged for the explicit
        post-deployment resume.
        """
        detail = str(reason or "").strip() or "管理员准备部署"
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                "SELECT * FROM execution_control WHERE singleton=1"
            ).fetchone()
            if current_row is None:
                raise ExecutionInvariantError(
                    "execution_control singleton missing"
                )
            current = dict(current_row)
            if self.is_maintenance_control(current):
                conn.execute(
                    "UPDATE execution_control SET accepting=0,auto_enabled=0,"
                    "deployment_drain_reason=?,updated_at=? WHERE singleton=1",
                    (detail[:1000], _now()),
                )
                return dict(
                    conn.execute(
                        "SELECT * FROM execution_control WHERE singleton=1"
                    ).fetchone()
                )
            if current.get("dispatcher_state") != "running":
                raise ExecutionMaintenanceConflict(
                    "maintenance_state_conflict",
                    "执行队列未正常运行，请先完成运行环境恢复再准备部署",
                )
            conn.execute(
                "UPDATE execution_control SET accepting=0,auto_enabled=0,"
                "deployment_drain_requested=1,deployment_drain_reason=?,"
                "updated_at=? WHERE singleton=1",
                (detail[:1000], _now()),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM execution_control WHERE singleton=1"
                ).fetchone()
            )

    def end_maintenance(self) -> dict:
        """Atomically clear only the deployment gate; auto remains disabled."""
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current_row = conn.execute(
                "SELECT * FROM execution_control WHERE singleton=1"
            ).fetchone()
            if current_row is None:
                raise ExecutionInvariantError(
                    "execution_control singleton missing"
                )
            current = dict(current_row)
            if not self.is_maintenance_control(current):
                if (
                    current.get("dispatcher_state") == "running"
                    and int(current.get("accepting") or 0) == 1
                ):
                    return current
                raise ExecutionMaintenanceConflict(
                    "maintenance_not_active",
                    "执行队列当前不在部署维护状态",
                )
            status = self.maintenance_status_tx(conn, current=current)
            if not status["ready"]:
                raise ExecutionMaintenanceConflict(
                    "maintenance_not_ready",
                    "维护边界尚未完全收敛，不能恢复接单",
                )
            conn.execute(
                "UPDATE execution_control SET deployment_drain_requested=0,"
                "deployment_drain_reason='',accepting=1,updated_at=? "
                "WHERE singleton=1",
                (_now(),),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM execution_control WHERE singleton=1"
                ).fetchone()
            )

    def maintenance_status_tx(
        self,
        conn: sqlite3.Connection,
        *,
        current: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Transaction-local form used by the end-maintenance CAS."""
        control = current or dict(
            conn.execute(
                "SELECT * FROM execution_control WHERE singleton=1"
            ).fetchone()
        )
        active_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM execution_jobs WHERE status IN "
                "('starting','running','settling')"
            ).fetchone()[0]
        )
        lease_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM local_ai_leases WHERE status='active'"
            ).fetchone()[0]
        )
        # A legacy or partially recovered Match can still be running without
        # an execution_jobs owner.  The capacity model already charges these
        # rows conservatively; deployment readiness must use the same truth or
        # it could turn green while an untracked sandbox is still live.
        untracked_running = int(
            self._capacity_tx(
                conn,
                max_match_slots=1,
                max_sandbox_units=1,
            )["untracked_running_matches"]
        )
        launch_state = str(self._docker_launch_tx(conn).get("state") or "")
        requested = self.is_maintenance_control(control)
        return {
            "requested": requested,
            "ready": bool(
                requested
                and control.get("dispatcher_state") == "running"
                and int(control.get("accepting") or 0) == 0
                and active_count == 0
                and lease_count == 0
                and untracked_running == 0
                and launch_state == "idle"
            ),
            "reason": str(control.get("deployment_drain_reason") or ""),
            "active_count": active_count,
            "active_local_ai_leases": lease_count,
            "untracked_running_matches": untracked_running,
            "docker_launch_state": launch_state,
        }

    def get_auto_enabled(self) -> bool:
        return bool(int(self.control()["auto_enabled"]))

    def set_auto_enabled(self, enabled: bool) -> bool:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT * FROM execution_control WHERE singleton=1"
            ).fetchone()
            if current is None:
                raise ExecutionInvariantError(
                    "execution_control singleton missing"
                )
            if enabled and self.is_maintenance_control(dict(current)):
                raise ExecutionMaintenanceConflict(
                    "maintenance_active",
                    "维护期间不能开启自动排位，请先恢复执行队列",
                )
            conn.execute(
                "UPDATE execution_control SET auto_enabled=?,updated_at=? "
                "WHERE singleton=1",
                (1 if enabled else 0, _now()),
            )
            if enabled and not bool(int(current["auto_enabled"] or 0)):
                self._advance_auto_gate_tx(
                    conn,
                    seconds=AUTO_MATCH_IDLE_GRACE_SECONDS,
                    reason="idle_grace",
                )
        return bool(enabled)

    # ------------------------------------------------------------------
    # Enqueue and read models
    # ------------------------------------------------------------------
    @staticmethod
    def _resource_snapshot(
        bot_a_environment: str,
        bot_b_environment: str,
    ) -> tuple[int, int, int]:
        # Keep the Store package importable while runtime.limits itself loads
        # store.schema.  The versioned registry is needed only when a job is
        # created or claimed, not while this module is imported.
        from bzplat.backend.runtime.limits import execution_resource_snapshot

        return execution_resource_snapshot(
            (bot_a_environment, bot_b_environment),
            EXECUTION_PROFILE_VERSION,
        )

    @staticmethod
    def _local_agent_snapshot_tx(
        conn: sqlite3.Connection,
        *,
        agent_id: int,
        bot_id: int,
        game_id: str,
        expected_protocol: str | None = None,
        request_owner_id: int | None = None,
        enforce_request_owner: bool = False,
    ) -> dict[str, Any] | None:
        row = conn.execute(
            "SELECT a.id,a.public_id,a.connection_generation,a.owner_id,"
            "a.bot_id,a.game_id,a.protocol_version,a.status,"
            "b.owner_id AS bot_owner_id,b.game_id AS bot_game_id,"
            "b.protocol_version AS bot_protocol_version,"
            "b.is_active AS bot_active,u.is_active AS owner_active "
            "FROM local_ai_agents a JOIN bots b ON b.id=a.bot_id "
            "JOIN users u ON u.id=a.owner_id WHERE a.id=?",
            (int(agent_id),),
        ).fetchone()
        if not (
            row is not None
            and str(row["status"] or "") == "active"
            and int(row["bot_id"]) == int(bot_id)
            and str(row["game_id"] or "") == game_id
            and str(row["bot_game_id"] or "") == game_id
            and (
                expected_protocol is None
                or (
                    str(row["protocol_version"] or "") == expected_protocol
                    and str(row["bot_protocol_version"] or "")
                    == expected_protocol
                )
            )
            and int(row["owner_id"]) == int(row["bot_owner_id"])
            and int(row["bot_active"] or 0) == 1
            and int(row["owner_active"] or 0) == 1
        ):
            return None
        if not enforce_request_owner:
            return dict(row)
        if request_owner_id is None:
            return None
        return (
            dict(row)
            if int(row["owner_id"]) == int(request_owner_id)
            else None
        )

    @classmethod
    def _local_agent_identity_tx(
        cls,
        conn: sqlite3.Connection,
        *,
        agent_id: int,
        bot_id: int,
        game_id: str,
        expected_protocol: str | None = None,
        request_owner_id: int | None = None,
        enforce_request_owner: bool = False,
    ) -> bool:
        return cls._local_agent_snapshot_tx(
            conn,
            agent_id=agent_id,
            bot_id=bot_id,
            game_id=game_id,
            expected_protocol=expected_protocol,
            request_owner_id=request_owner_id,
            enforce_request_owner=enforce_request_owner,
        ) is not None

    def _execution_environment_tx(
        self,
        conn: sqlite3.Connection,
        *,
        source: str,
        owner_user_id: int | None,
        game_id: str,
        protocol_version: str,
        bot_a_id: int,
        bot_b_id: int,
        bot_a_version_id: int | None,
        bot_b_version_id: int | None,
        bot_a_environment: str | None,
        bot_b_environment: str | None,
        bot_a_local_agent_id: int | None,
        bot_b_local_agent_id: int | None,
        human_seat: int | None,
    ) -> dict[str, Any]:
        if source == EXECUTION_SOURCE_CONTEST:
            environments = (
                EXECUTION_ENV_PLATFORM_HIGH,
                EXECUTION_ENV_PLATFORM_HIGH,
            )
            agent_ids: tuple[int | None, int | None] = (None, None)
        elif source == EXECUTION_SOURCE_AUTO:
            environments = (
                EXECUTION_ENV_PLATFORM_LOW,
                EXECUTION_ENV_PLATFORM_LOW,
            )
            agent_ids = (None, None)
        elif source == EXECUTION_SOURCE_HUMAN:
            if human_seat not in (0, 1):
                raise ValueError("人类对局缺少有效座位")
            environments = (
                EXECUTION_ENV_HUMAN
                if int(human_seat) == 0
                else EXECUTION_ENV_PLATFORM_LOW,
                EXECUTION_ENV_HUMAN
                if int(human_seat) == 1
                else EXECUTION_ENV_PLATFORM_LOW,
            )
            agent_ids = (None, None)
        elif source == EXECUTION_SOURCE_MANUAL:
            environments = (
                str(bot_a_environment or EXECUTION_ENV_PLATFORM_LOW),
                str(bot_b_environment or EXECUTION_ENV_PLATFORM_LOW),
            )
            if any(item not in _MANUAL_ENVIRONMENTS for item in environments):
                raise ValueError("普通挑战只允许低配 Docker 或本地 Bot")
            agent_ids = (bot_a_local_agent_id, bot_b_local_agent_id)
        else:  # guarded by the caller, retained as a fail-closed boundary
            raise ValueError(f"unknown execution source: {source}")

        versions: list[int | None] = [bot_a_version_id, bot_b_version_id]
        bots = (int(bot_a_id), int(bot_b_id))
        normalized_agents: list[int | None] = [None, None]
        for seat, environment in enumerate(environments):
            supplied_agent = agent_ids[seat]
            if environment == EXECUTION_ENV_REMOTE_LOCAL:
                if supplied_agent is None:
                    raise ValueError("本地 Bot 座位必须选择连接")
                agent_id = int(supplied_agent)
                if not self._local_agent_identity_tx(
                    conn,
                    agent_id=agent_id,
                    bot_id=bots[seat],
                    game_id=game_id,
                    expected_protocol=protocol_version,
                    request_owner_id=owner_user_id,
                    enforce_request_owner=True,
                ):
                    raise ValueError("本地 Bot 连接与用户、Bot 或游戏不匹配")
                normalized_agents[seat] = agent_id
                # A local connector is its own frozen execution identity; it
                # must never masquerade as one uploaded immutable version.
                versions[seat] = None
            elif supplied_agent is not None:
                raise ValueError("Docker 或人类座位不能绑定本地 Bot 连接")
            elif environment == EXECUTION_ENV_HUMAN:
                versions[seat] = None

        remote_agents = [
            agent_id for agent_id in normalized_agents if agent_id is not None
        ]
        if len(remote_agents) != len(set(remote_agents)):
            raise ValueError("同一个本地 Bot 连接不能同时占用两个座位")

        units, cpu_millis, memory_mb = self._resource_snapshot(*environments)
        return {
            "bot_a_environment": environments[0],
            "bot_b_environment": environments[1],
            "bot_a_local_agent_id": normalized_agents[0],
            "bot_b_local_agent_id": normalized_agents[1],
            "bot_a_version_id": versions[0],
            "bot_b_version_id": versions[1],
            "sandbox_units": units,
            "host_cpu_millis": cpu_millis,
            "host_memory_mb": memory_mb,
            "profile_version": EXECUTION_PROFILE_VERSION,
        }

    @staticmethod
    def _rating_policy_tx(
        conn: sqlite3.Connection,
        *,
        source: str,
        bot_a_id: int,
        bot_b_id: int,
        bot_a_environment: str,
        bot_b_environment: str,
    ) -> tuple[bool, str]:
        if EXECUTION_ENV_REMOTE_LOCAL in {
            bot_a_environment,
            bot_b_environment,
        }:
            return False, "remote_local"
        if source == EXECUTION_SOURCE_CONTEST:
            return False, "contest"
        if source == EXECUTION_SOURCE_HUMAN:
            return False, "human"
        if bot_a_id == bot_b_id:
            return False, "self_play"
        rows = conn.execute(
            "SELECT id,owner_id,is_ranked FROM bots WHERE id IN (?,?)",
            (bot_a_id, bot_b_id),
        ).fetchall()
        owner_by_bot = {int(row["id"]): int(row["owner_id"]) for row in rows}
        if len(owner_by_bot) != 2:
            return False, "bot_missing"
        if owner_by_bot[bot_a_id] == owner_by_bot[bot_b_id]:
            return False, "same_owner"
        ranked_by_bot = {
            int(row["id"]): bool(int(row["is_ranked"] or 0)) for row in rows
        }
        if not ranked_by_bot[bot_a_id] or not ranked_by_bot[bot_b_id]:
            return False, "ranked_bot_not_selected"
        return True, "eligible"

    def _version_identity_tx(
        self,
        conn: sqlite3.Connection,
        *,
        bot_id: int,
        version_id: int | None,
        expected_game_id: str | None = None,
        expected_protocol: str | None = None,
        check_file: bool = True,
    ) -> bool:
        bot = conn.execute(
            "SELECT id,is_active,game_id,current_version,binary_path,runtime_mode,"
            "format,os,arch,protocol_version "
            "FROM bots WHERE id=?",
            (bot_id,),
        ).fetchone()
        if bot is not None:
            active_contract = _active_game_contract_tx(conn, str(bot["game_id"]))
            expected_game_id = expected_game_id or str(bot["game_id"])
            expected_protocol = (
                expected_protocol or active_contract["protocol_version"]
            )
        if (
            bot is None
            or int(bot["is_active"] or 0) != 1
            or str(bot["game_id"] or "") != expected_game_id
            or str(bot["protocol_version"] or "") != expected_protocol
            or str(bot["format"] or "") != SUPPORTED_BINARY_FORMAT
            or str(bot["os"] or "") != SUPPORTED_BINARY_OS
            or str(bot["arch"] or "") != SUPPORTED_BINARY_ARCH
        ):
            return False
        if version_id is None:
            # Only a genuine pre-version Bot may execute through the mirror.
            valid_legacy = bool(
                int(bot["current_version"] or 0) == 0
                and str(bot["binary_path"] or "").strip()
                and str(bot["runtime_mode"] or "") in VALID_RUNTIME_MODES
                and conn.execute(
                    "SELECT 1 FROM bot_versions WHERE bot_id=? LIMIT 1", (bot_id,)
                ).fetchone()
                is None
            )
            runtime = dict(bot)
            if not valid_legacy:
                return False
        else:
            version = conn.execute(
                "SELECT bot_id,binary_path,runtime_mode,format,os,arch,checksum,"
                "size_bytes,protocol_version,retired_at FROM bot_versions WHERE id=?",
                (version_id,),
            ).fetchone()
            if not (
                version is not None
                and int(version["bot_id"]) == bot_id
                and version["retired_at"] is None
                and str(version["protocol_version"] or "") == expected_protocol
                and str(version["binary_path"] or "").strip()
                and str(version["runtime_mode"] or "") in VALID_RUNTIME_MODES
                and str(version["format"] or "") == SUPPORTED_BINARY_FORMAT
                and str(version["os"] or "") == SUPPORTED_BINARY_OS
                and str(version["arch"] or "") == SUPPORTED_BINARY_ARCH
            ):
                return False
            runtime = dict(version)
        if not check_file:
            return True
        try:
            require_binary_file_integrity(
                runtime,
                str(runtime.get("binary_path") or ""),
                cache=self._binary_integrity_cache,
            )
        except (OSError, TypeError, ValueError):
            return False
        return True

    def _insert_job_tx(
        self,
        conn: sqlite3.Connection,
        *,
        source: str,
        owner_user_id: int | None,
        game_id: str,
        match_type: str,
        bot_a_id: int,
        bot_b_id: int,
        bot_a_version_id: int | None,
        bot_b_version_id: int | None,
        bot_a_environment: str | None = None,
        bot_b_environment: str | None = None,
        bot_a_local_agent_id: int | None = None,
        bot_b_local_agent_id: int | None = None,
        match_config: dict[str, Any] | None,
        human_user_id: int | None,
        human_seat: int | None,
        contest_id: int | None,
        contest_pairing_id: int | None,
        auto_decision_id: int | None,
        public_id: str | None = None,
        idempotency_fingerprint: str | None = None,
        created_at: str | None = None,
    ) -> dict:
        if source not in EXECUTION_SOURCES:
            raise ValueError(f"unknown execution source: {source}")
        gid = _registered_game_id(game_id)
        contract = _active_game_contract_tx(conn, gid)
        if source == EXECUTION_SOURCE_CONTEST:
            contest = conn.execute(
                "SELECT game_id,ruleset_version,protocol_version,rating_pool_id "
                "FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if (
                contest is None
                or str(contest["game_id"] or "") != gid
                or str(contest["ruleset_version"] or "")
                != contract["ruleset_version"]
                or str(contest["protocol_version"] or "")
                != contract["protocol_version"]
                or str(contest["rating_pool_id"] or "")
                != contract["rating_pool_id"]
            ):
                raise ValueError("赛事规则版本已退役或与当前游戏契约不一致")
        environment = self._execution_environment_tx(
            conn,
            source=source,
            owner_user_id=owner_user_id,
            game_id=gid,
            protocol_version=contract["protocol_version"],
            bot_a_id=int(bot_a_id),
            bot_b_id=int(bot_b_id),
            bot_a_version_id=bot_a_version_id,
            bot_b_version_id=bot_b_version_id,
            bot_a_environment=bot_a_environment,
            bot_b_environment=bot_b_environment,
            bot_a_local_agent_id=bot_a_local_agent_id,
            bot_b_local_agent_id=bot_b_local_agent_id,
            human_seat=human_seat,
        )
        bot_a_version_id = environment["bot_a_version_id"]
        bot_b_version_id = environment["bot_b_version_id"]
        for suffix, bot_id, version_id in (
            ("a", int(bot_a_id), bot_a_version_id),
            ("b", int(bot_b_id), bot_b_version_id),
        ):
            seat_environment = str(environment[f"bot_{suffix}_environment"])
            if seat_environment in {
                EXECUTION_ENV_HUMAN,
                EXECUTION_ENV_REMOTE_LOCAL,
            }:
                continue
            frozen_version_id = (
                int(version_id) if version_id is not None else None
            )
            if not self._version_identity_tx(
                conn,
                bot_id=bot_id,
                version_id=frozen_version_id,
                expected_game_id=gid,
                expected_protocol=contract["protocol_version"],
                check_file=False,
            ):
                raise ValueError("Bot 版本已退役或与当前游戏协议不兼容")
        rated, rating_reason = self._rating_policy_tx(
            conn,
            source=source,
            bot_a_id=int(bot_a_id),
            bot_b_id=int(bot_b_id),
            bot_a_environment=str(environment["bot_a_environment"]),
            bot_b_environment=str(environment["bot_b_environment"]),
        )
        public = public_id or _new_public_id()
        now = created_at or _now()
        priority = SOURCE_PRIORITY[source]
        config = dict(match_config or {})
        config.pop("_rating_eligible", None)
        config.pop("_rating_reason", None)
        if idempotency_fingerprint:
            config["_execution_idempotency_fingerprint"] = str(
                idempotency_fingerprint
            )
        conn.execute(
            "INSERT INTO execution_jobs("
            "public_id,source,status,priority,owner_user_id,game_id,ruleset_version,"
            "protocol_version,rating_pool_id,match_type,"
            "bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id,"
            "bot_a_environment,bot_b_environment,bot_a_local_agent_id,"
            "bot_b_local_agent_id,human_user_id,human_seat,contest_id,"
            "contest_pairing_id,match_config,rated,rating_reason,match_slots,"
            "sandbox_units,host_cpu_millis,host_memory_mb,profile_version,"
            "auto_decision_id,created_at) "
            "VALUES(:public_id,:source,'queued',:priority,:owner_user_id,:game_id,"
            ":ruleset_version,:protocol_version,:rating_pool_id,"
            ":match_type,:bot_a_id,:bot_b_id,:bot_a_version_id,:bot_b_version_id,"
            ":bot_a_environment,:bot_b_environment,:bot_a_local_agent_id,"
            ":bot_b_local_agent_id,:human_user_id,:human_seat,:contest_id,"
            ":contest_pairing_id,:match_config,:rated,:rating_reason,1,"
            ":sandbox_units,:host_cpu_millis,:host_memory_mb,:profile_version,"
            ":auto_decision_id,:created_at)",
            {
                "public_id": public,
                "source": source,
                "priority": priority,
                "owner_user_id": owner_user_id,
                "game_id": gid,
                "ruleset_version": contract["ruleset_version"],
                "protocol_version": contract["protocol_version"],
                "rating_pool_id": contract["rating_pool_id"],
                "match_type": match_type,
                "bot_a_id": bot_a_id,
                "bot_b_id": bot_b_id,
                "bot_a_version_id": bot_a_version_id,
                "bot_b_version_id": bot_b_version_id,
                "bot_a_environment": environment["bot_a_environment"],
                "bot_b_environment": environment["bot_b_environment"],
                "bot_a_local_agent_id": environment["bot_a_local_agent_id"],
                "bot_b_local_agent_id": environment["bot_b_local_agent_id"],
                "human_user_id": human_user_id,
                "human_seat": human_seat,
                "contest_id": contest_id,
                "contest_pairing_id": contest_pairing_id,
                "match_config": json.dumps(
                    config, ensure_ascii=False, separators=(",", ":")
                ),
                "rated": 1 if rated else 0,
                "rating_reason": rating_reason,
                "sandbox_units": environment["sandbox_units"],
                "host_cpu_millis": environment["host_cpu_millis"],
                "host_memory_mb": environment["host_memory_mb"],
                "profile_version": environment["profile_version"],
                "auto_decision_id": auto_decision_id,
                "created_at": now,
            },
        )
        return dict(
            conn.execute(
                "SELECT * FROM execution_jobs WHERE public_id=?", (public,)
            ).fetchone()
        )

    def enqueue(
        self,
        *,
        source: str,
        owner_user_id: int | None,
        game_id: str,
        match_type: str,
        bot_a_id: int,
        bot_b_id: int,
        bot_a_version_id: int | None,
        bot_b_version_id: int | None,
        bot_a_environment: str | None = None,
        bot_b_environment: str | None = None,
        bot_a_local_agent_id: int | None = None,
        bot_b_local_agent_id: int | None = None,
        match_config: dict[str, Any] | None = None,
        human_user_id: int | None = None,
        human_seat: int | None = None,
        contest_id: int | None = None,
        contest_pairing_id: int | None = None,
        auto_decision_id: int | None = None,
        user_queued_limit: int = 4,
        public_id: str | None = None,
        idempotency_fingerprint: str | None = None,
    ) -> dict:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if public_id is not None:
                existing = conn.execute(
                    "SELECT * FROM execution_jobs WHERE public_id=?", (public_id,)
                ).fetchone()
                if existing is not None:
                    self._assert_idempotent_match(
                        existing,
                        owner_user_id=owner_user_id,
                        source=source,
                        fingerprint=idempotency_fingerprint,
                    )
                    return dict(existing)
            control = conn.execute(
                "SELECT accepting,deployment_drain_requested "
                "FROM execution_control WHERE singleton=1"
            ).fetchone()
            if control is None or int(control["accepting"] or 0) != 1:
                if control is not None and int(
                    control["deployment_drain_requested"] or 0
                ):
                    raise ExecutionQueueClosed(
                        "平台正在部署维护，暂不接收新的对局请求",
                        code="deployment_maintenance",
                    )
                raise ExecutionQueueClosed("执行队列正在启动或停止，请稍后重试")
            if source == EXECUTION_SOURCE_CONTEST:
                # Pairings remain ``pending + match_id=NULL`` until the global
                # claim transaction.  Scheduler/reconcile ticks may therefore
                # see the same due pairing repeatedly; enqueue is deliberately
                # idempotent at that durable boundary instead of relying on a
                # unique-index exception as control flow.
                existing = conn.execute(
                    "SELECT * FROM execution_jobs WHERE contest_pairing_id=? "
                    "AND status IN ('queued','starting','running','settling') "
                    "ORDER BY id DESC LIMIT 1",
                    (contest_pairing_id,),
                ).fetchone()
                if existing is not None:
                    return dict(existing)
            if source in {EXECUTION_SOURCE_MANUAL, EXECUTION_SOURCE_HUMAN}:
                if owner_user_id is None:
                    raise ValueError("用户执行请求缺少 owner")
                count = conn.execute(
                    "SELECT COUNT(*) AS n FROM execution_jobs WHERE owner_user_id=? "
                    "AND source IN ('manual','human') "
                    "AND status IN ('queued','starting','running','settling')",
                    (owner_user_id,),
                ).fetchone()
                if int(count["n"] or 0) >= max(1, int(user_queued_limit)):
                    raise ValueError("你的执行队列已满，请等待现有请求完成")
                if source == EXECUTION_SOURCE_HUMAN:
                    human = conn.execute(
                        "SELECT 1 FROM execution_jobs WHERE owner_user_id=? "
                        "AND source='human' AND status IN "
                        "('queued','starting','running','settling') LIMIT 1",
                        (owner_user_id,),
                    ).fetchone()
                    if human:
                        raise ValueError("你已有一场人类对局请求，请先结束")
            inserted = self._insert_job_tx(
                conn,
                source=source,
                owner_user_id=owner_user_id,
                game_id=game_id,
                match_type=match_type,
                bot_a_id=bot_a_id,
                bot_b_id=bot_b_id,
                bot_a_version_id=bot_a_version_id,
                bot_b_version_id=bot_b_version_id,
                bot_a_environment=bot_a_environment,
                bot_b_environment=bot_b_environment,
                bot_a_local_agent_id=bot_a_local_agent_id,
                bot_b_local_agent_id=bot_b_local_agent_id,
                match_config=match_config,
                human_user_id=human_user_id,
                human_seat=human_seat,
                contest_id=contest_id,
                contest_pairing_id=contest_pairing_id,
                auto_decision_id=auto_decision_id,
                public_id=public_id,
                idempotency_fingerprint=idempotency_fingerprint,
            )
            if source in _FOREGROUND_SOURCES:
                is_showcase = False
                if source == EXECUTION_SOURCE_CONTEST:
                    contest = conn.execute(
                        "SELECT showcase_key FROM contests WHERE id=?",
                        (contest_id,),
                    ).fetchone()
                    is_showcase = bool(
                        contest is not None and contest["showcase_key"] is not None
                    )
                if not is_showcase:
                    self._yield_auto_to_foreground_tx(conn)
            return inserted

    @staticmethod
    def _assert_idempotent_match(
        row: sqlite3.Row | dict,
        *,
        owner_user_id: int | None,
        source: str,
        fingerprint: str | None,
    ) -> None:
        try:
            config = json.loads(str(row["match_config"] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            config = {}
        stored = config.get("_execution_idempotency_fingerprint")
        if (
            owner_user_id is None
            or int(row["owner_user_id"] or 0) != int(owner_user_id)
            or str(row["source"] or "") != source
            or not fingerprint
            or stored != fingerprint
        ):
            raise ExecutionIdempotencyConflict(
                "请求标识已被使用，请刷新后重新提交"
            )

    def get_idempotent(
        self,
        public_id: str,
        *,
        owner_user_id: int,
        source: str,
        fingerprint: str,
    ) -> dict | None:
        """Return an exact owner request without re-running mutable validation.

        This is the response-loss recovery boundary: a retry with the same
        caller-generated opaque id observes the original durable row even if
        the queue has since paused or the selected Bot's current version moved.
        """
        with self.store._tx() as conn:
            row = conn.execute(
                "SELECT * FROM execution_jobs WHERE public_id=?", (public_id,)
            ).fetchone()
            if row is None:
                return None
            self._assert_idempotent_match(
                row,
                owner_user_id=owner_user_id,
                source=source,
                fingerprint=fingerprint,
            )
            return dict(row)

    def get(self, public_id: str) -> dict | None:
        with self.store._tx() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM execution_jobs WHERE public_id=?", (public_id,)
                ).fetchone()
            )

    def get_by_match(self, match_id: str) -> dict | None:
        with self.store._tx() as conn:
            return _row(
                conn.execute(
                    "SELECT * FROM execution_jobs WHERE current_match_id=?",
                    (match_id,),
                ).fetchone()
            )

    @staticmethod
    def _effective_priority(row: dict, *, now: datetime, aging_seconds: int) -> int:
        age = max(0.0, (now - _parse_time(row.get("created_at"))).total_seconds())
        # Aging is deliberately unbounded *within the selected claim class*.
        # Foreground and automatic work are ordered in separate passes, so an
        # old auto request can never use this bonus to cross that class boundary.
        bonus = int(age // max(1, aging_seconds))
        return int(row.get("priority") or 0) + bonus

    def _ordered_queued_tx(
        self,
        conn: sqlite3.Connection,
        *,
        aging_seconds: int,
        include_held_auto: bool = False,
        allowed_sources: frozenset[str] | None = None,
    ) -> list[dict]:
        control = conn.execute(
            "SELECT auto_enabled FROM execution_control WHERE singleton=1"
        ).fetchone()
        auto_enabled = bool(control and int(control["auto_enabled"] or 0))
        due = _now()
        rows = [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM execution_jobs WHERE status='queued' "
                "AND cancel_requested=0 AND (next_attempt_at IS NULL OR next_attempt_at<=?)",
                (due,),
            ).fetchall()
            if (allowed_sources is None or row["source"] in allowed_sources)
            and (
                include_held_auto
                or auto_enabled
                or row["source"] != EXECUTION_SOURCE_AUTO
            )
        ]
        now = datetime.now()
        rows.sort(
            key=lambda row: (
                -self._effective_priority(
                    row, now=now, aging_seconds=aging_seconds
                ),
                _parse_time(row.get("created_at")),
                int(row["id"]),
            )
        )
        return rows

    @staticmethod
    def _capacity_tx(
        conn: sqlite3.Connection,
        *,
        max_match_slots: int,
        max_sandbox_units: int,
        max_host_cpu_millis: int | None = None,
        max_host_memory_mb: int | None = None,
    ) -> dict:
        marks = "'starting','running','settling'"
        used = conn.execute(
            "SELECT COUNT(*) AS jobs,COALESCE(SUM(match_slots),0) AS slots,"
            "COALESCE(SUM(sandbox_units),0) AS units,"
            "COALESCE(SUM(host_cpu_millis),0) AS cpu_millis,"
            "COALESCE(SUM(host_memory_mb),0) AS memory_mb FROM execution_jobs "
            f"WHERE status IN ({marks})"
        ).fetchone()
        # Count *every* registered match table, including historical/legacy
        # runners that have no execution_jobs row.  Those untracked processes
        # consume an additional slot until startup recovery has made them
        # terminal; counting only game IDs currently present in the queue could
        # otherwise over-admit after a deploy.
        running_matches = 0
        tracked_running = 0
        for game_id in sorted(_all_game_ids()):
            table = _matches_table(game_id)
            running_matches += int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE status='running'"
                ).fetchone()[0]
            )
            tracked_running += int(
                conn.execute(
                    f"SELECT COUNT(*) FROM {table} m JOIN execution_jobs j "
                    "ON j.current_match_id=m.id WHERE m.status='running' "
                    "AND j.status IN ('starting','running','settling')"
                ).fetchone()[0]
            )
        untracked_running = max(0, running_matches - tracked_running)
        occupied_match_slots = int(used["slots"] or 0) + untracked_running
        if untracked_running:
            # Keep Store importable while runtime.limits imports store.schema.
            # An untracked legacy Match has no frozen job vector, so derive the
            # fail-closed two-Bot maximum from the append-only resource registry.
            from bzplat.backend.runtime.limits import (
                maximum_execution_match_resource_snapshot,
            )

            untracked_resources = maximum_execution_match_resource_snapshot()
        else:
            untracked_resources = (0, 0, 0)
        (
            untracked_sandbox_units,
            untracked_host_cpu_millis,
            untracked_host_memory_mb,
        ) = untracked_resources
        return {
            "used_jobs": int(used["jobs"] or 0),
            "used_match_slots": int(used["slots"] or 0),
            # A legacy/untracked running match has no durable resource vector.
            # Charge the conservative Bot-vs-Bot maximum so such a live row can
            # never make any global resource dimension under-report capacity.
            "used_sandbox_units": int(used["units"] or 0)
            + untracked_running * untracked_sandbox_units,
            "used_host_cpu_millis": int(used["cpu_millis"] or 0)
            + untracked_running * untracked_host_cpu_millis,
            "used_host_memory_mb": int(used["memory_mb"] or 0)
            + untracked_running * untracked_host_memory_mb,
            "running_matches": running_matches,
            "untracked_running_matches": untracked_running,
            "occupied_match_slots": occupied_match_slots,
            "max_match_slots": max(1, int(max_match_slots)),
            "max_sandbox_units": max(1, int(max_sandbox_units)),
            # The production dispatcher always supplies a process-visible
            # ceiling.  None keeps older direct repository callers compatible.
            "max_host_cpu_millis": (
                max(1, int(max_host_cpu_millis))
                if max_host_cpu_millis is not None
                else 2**63 - 1
            ),
            "max_host_memory_mb": (
                max(1, int(max_host_memory_mb))
                if max_host_memory_mb is not None
                else 2**63 - 1
            ),
        }

    @staticmethod
    def _permanent_host_block(job: dict, capacity: dict) -> tuple[str, str]:
        required_cpu = max(0, int(job.get("host_cpu_millis") or 0))
        required_memory = max(0, int(job.get("host_memory_mb") or 0))
        if (
            required_cpu <= int(capacity["max_host_cpu_millis"])
            and required_memory <= int(capacity["max_host_memory_mb"])
        ):
            return "", ""
        cpu_label = (
            str(required_cpu // 1000)
            if required_cpu % 1000 == 0
            else f"{required_cpu / 1000:.1f}"
        )
        memory_label = (
            f"{required_memory // 1024} GiB"
            if required_memory and required_memory % 1024 == 0
            else f"{required_memory} MiB"
        )
        return (
            "host_resources_insufficient",
            f"该对局需要 {cpu_label} 核 CPU 和 {memory_label} 内存；"
            "当前主机资源不足，请求会保留排队且不会降档",
        )

    def _local_agent_capacity_block_tx(
        self,
        conn: sqlite3.Connection,
        job: dict,
    ) -> tuple[str, str]:
        if str(job.get("status") or "") != EXECUTION_QUEUED:
            return "", ""
        for suffix in ("a", "b"):
            if (
                str(job.get(f"bot_{suffix}_environment") or "")
                != EXECUTION_ENV_REMOTE_LOCAL
            ):
                continue
            frozen_agent = job.get(f"bot_{suffix}_local_agent_id")
            agent_id = int(frozen_agent) if frozen_agent is not None else 0
            has_active_lease = bool(
                agent_id
                and conn.execute(
                    "SELECT 1 FROM local_ai_leases WHERE agent_id=? "
                    "AND status='active' LIMIT 1",
                    (agent_id,),
                ).fetchone()
                is not None
            )
            if (
                not agent_id
                or has_active_lease
                or not self._is_local_agent_available(agent_id)
            ):
                return (
                    "local_agent_unavailable",
                    "等待所选本地 Bot 上线并空闲；保持连接后平台会自动继续排队",
                )
        return "", ""

    def _project_capacity_block_tx(
        self,
        conn: sqlite3.Connection,
        job: dict,
        capacity: dict,
    ) -> dict:
        projected = dict(job)
        code, reason = self._permanent_host_block(projected, capacity)
        if not code:
            code, reason = self._local_agent_capacity_block_tx(conn, projected)
        if code:
            projected["capacity_blocked_code"] = code
            projected["capacity_blocked_reason"] = reason
        return projected

    def snapshot(
        self,
        *,
        max_match_slots: int,
        max_sandbox_units: int,
        aging_seconds: int,
        max_host_cpu_millis: int | None = None,
        max_host_memory_mb: int | None = None,
        public_id: str | None = None,
        game_id: str | None = None,
    ) -> dict:
        with self.store._tx() as conn:
            control = dict(
                conn.execute(
                    "SELECT * FROM execution_control WHERE singleton=1"
                ).fetchone()
            )
            capacity = self._capacity_tx(
                conn,
                max_match_slots=max_match_slots,
                max_sandbox_units=max_sandbox_units,
                max_host_cpu_millis=max_host_cpu_millis,
                max_host_memory_mb=max_host_memory_mb,
            )
            foreground = self._ordered_queued_tx(
                conn,
                aging_seconds=aging_seconds,
                include_held_auto=True,
                allowed_sources=_FOREGROUND_SOURCES,
            )
            automatic = self._ordered_queued_tx(
                conn,
                aging_seconds=aging_seconds,
                include_held_auto=True,
                allowed_sources=frozenset({EXECUTION_SOURCE_AUTO}),
            )
            ordered = foreground + automatic
            scheduler = self._auto_scheduler_tx(conn)
            dispatchable = list(foreground)
            if scheduler["state"] == "ready":
                dispatchable.extend(automatic)
            if game_id is not None:
                ordered = [row for row in ordered if row["game_id"] == game_id]
                dispatchable = [
                    row for row in dispatchable if row["game_id"] == game_id
                ]
            active = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM execution_jobs WHERE status IN "
                    "('starting','running','settling') ORDER BY claimed_at,id"
                ).fetchall()
                if game_id is None or row["game_id"] == game_id
            ]
            target = None
            ahead_jobs = 0
            ahead_units = 0
            if public_id:
                target = conn.execute(
                    "SELECT * FROM execution_jobs WHERE public_id=?", (public_id,)
                ).fetchone()
                if target is not None and target["status"] == EXECUTION_QUEUED:
                    if any(
                        row["public_id"] == public_id for row in dispatchable
                    ):
                        for row in dispatchable:
                            if row["public_id"] == public_id:
                                break
                            ahead_jobs += 1
                            ahead_units += int(row["sandbox_units"])
            projected_ordered = [
                self._project_capacity_block_tx(conn, row, capacity)
                for row in ordered
            ]
            projected_target = (
                self._project_capacity_block_tx(conn, dict(target), capacity)
                if target is not None
                else None
            )
            return {
                "control": control,
                "capacity": capacity,
                "active": active,
                "queued": projected_ordered,
                "target": projected_target,
                "ahead_jobs": ahead_jobs,
                "ahead_sandbox_units": ahead_units,
                "auto_scheduler": scheduler,
            }

    def contest_has_active_jobs(self, contest_id: int) -> bool:
        """Return whether a contest still owns a non-terminal durable request."""
        with self.store._tx() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM execution_jobs WHERE contest_id=? "
                    "AND status IN ('queued','starting','running','settling') "
                    "LIMIT 1",
                    (int(contest_id),),
                ).fetchone()
                is not None
            )

    # ------------------------------------------------------------------
    # Atomic claim
    # ------------------------------------------------------------------
    def _insert_match_tx(
        self,
        conn: sqlite3.Connection,
        *,
        job: dict,
        match_id: str,
    ) -> None:
        gid = _registered_game_id(job["game_id"])
        table = _matches_table(gid)
        bot_a_id = int(job["bot_a_id"])
        bot_b_id = int(job["bot_b_id"])
        config = json.loads(str(job.get("match_config") or "{}"))
        # Browser response-loss idempotency is an execution-request concern;
        # it must not leak into the persisted public Match configuration.
        config.pop("_execution_idempotency_fingerprint", None)
        match_seed = config.get("duplicate_seed")
        config["_rating_eligible"] = bool(int(job["rated"] or 0))
        config["_rating_reason"] = str(job["rating_reason"])
        config["_execution_request_id"] = str(job["public_id"])
        config["_bot_a_environment"] = str(job["bot_a_environment"])
        config["_bot_b_environment"] = str(job["bot_b_environment"])
        config["_execution_profile_version"] = int(job["profile_version"])
        config["_bot_a_local_agent_id"] = job.get("bot_a_local_agent_id")
        config["_bot_b_local_agent_id"] = job.get("bot_b_local_agent_id")
        # A local_ai_agents row may be revoked and later reused for the same
        # owner-visible label.  Its integer primary key is therefore not an
        # immutable transport identity.  Freeze the authenticated public id
        # and durable connection generation in this claim transaction; the
        # runner must never resolve the mutable row again after this point.
        for seat, suffix in enumerate(("a", "b")):
            if str(job[f"bot_{suffix}_environment"]) != EXECUTION_ENV_REMOTE_LOCAL:
                continue
            raw_agent_id = job.get(f"bot_{suffix}_local_agent_id")
            snapshot = (
                self._local_agent_snapshot_tx(
                    conn,
                    agent_id=int(raw_agent_id),
                    bot_id=int(job[f"bot_{suffix}_id"]),
                    game_id=gid,
                    expected_protocol=str(job["protocol_version"]),
                    request_owner_id=job.get("owner_user_id"),
                    enforce_request_owner=True,
                )
                if raw_agent_id is not None
                else None
            )
            if snapshot is None:
                raise ExecutionInvariantError(
                    f"local AI seat {seat} identity changed before claim"
                )
            public_id = str(snapshot.get("public_id") or "").strip()
            generation = int(snapshot.get("connection_generation") or 0)
            if not public_id or generation < 0:
                raise ExecutionInvariantError(
                    f"local AI seat {seat} identity snapshot is invalid"
                )
            config[f"_bot_{suffix}_local_agent_public_id"] = public_id
            config[f"_bot_{suffix}_local_agent_generation"] = generation
        if job.get("bot_a_version_id") is not None:
            config["_bot_a_version_id"] = int(job["bot_a_version_id"])
        if job.get("bot_b_version_id") is not None:
            config["_bot_b_version_id"] = int(job["bot_b_version_id"])
        now = _now()
        conn.execute(
            f"INSERT INTO {table}(id,bot_a_id,bot_b_id,owner_id,contest_id,"
            "reason,match_type,status,game_id,ruleset_version,protocol_version,"
            "rating_pool_id,match_config,human_user_id,human_seat,match_seed,created_at) "
            "VALUES(?,?,?,?,?,'',?,'pending',?,?,?,?,?,?,?,?,?)",
            (
                match_id,
                bot_a_id,
                bot_b_id,
                job.get("owner_user_id"),
                job.get("contest_id"),
                job["match_type"],
                gid,
                job["ruleset_version"],
                job["protocol_version"],
                job["rating_pool_id"],
                json.dumps(config, ensure_ascii=False, separators=(",", ":")),
                job.get("human_user_id"),
                job.get("human_seat"),
                int(match_seed) if match_seed is not None else None,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO matches_index(id,game_id) VALUES(?,?)", (match_id, gid)
        )
        conn.execute(
            "INSERT INTO match_replays(match_id,events_json,updated_at) "
            "VALUES(?,'[]',?)",
            (match_id, now),
        )
        conn.execute(
            "INSERT INTO match_rating_policies("
            "match_id,game_id,rating_pool_id,bot_a_id,bot_b_id,rated,"
            "rating_reason,source,classified_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                match_id,
                gid,
                job["rating_pool_id"],
                bot_a_id,
                bot_b_id,
                int(job["rated"]),
                job["rating_reason"],
                "execution_claim_v3",
                now,
            ),
        )

    def claim_next(
        self,
        *,
        max_match_slots: int,
        max_sandbox_units: int,
        aging_seconds: int,
        user_active_limit: int,
        contest_share_slots: int,
        claim_class: str = "foreground",
        max_host_cpu_millis: int | None = None,
        max_host_memory_mb: int | None = None,
    ) -> dict | None:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            control = conn.execute(
                "SELECT * FROM execution_control WHERE singleton=1"
            ).fetchone()
            launch = self._docker_launch_tx(conn)
            if (
                control is None
                or control["dispatcher_state"] != "running"
                or int(control["accepting"] or 0) != 1
                or launch["state"] != "idle"
            ):
                return None
            if claim_class not in {"foreground", "auto"}:
                raise ValueError(f"unknown execution claim class: {claim_class}")
            if claim_class == "auto":
                scheduler = self._auto_scheduler_tx(conn)
                if (
                    scheduler["state"] != "ready"
                    or int(scheduler["active_count"])
                    >= EXECUTION_AUTO_ACTIVE_LIMIT
                ):
                    return None
            capacity = self._capacity_tx(
                conn,
                max_match_slots=max_match_slots,
                max_sandbox_units=max_sandbox_units,
                max_host_cpu_millis=max_host_cpu_millis,
                max_host_memory_mb=max_host_memory_mb,
            )
            if (
                capacity["occupied_match_slots"] >= capacity["max_match_slots"]
                or capacity["running_matches"] >= capacity["max_match_slots"]
            ):
                return None
            if claim_class == "auto" and (
                capacity["max_match_slots"] < 2
                or capacity["max_sandbox_units"] < 4
                or capacity["occupied_match_slots"] != 0
                or capacity["running_matches"] != 0
                or capacity["untracked_running_matches"] != 0
            ):
                return None
            queued = self._ordered_queued_tx(
                conn,
                aging_seconds=aging_seconds,
                allowed_sources=(
                    frozenset({EXECUTION_SOURCE_AUTO})
                    if claim_class == "auto"
                    else _FOREGROUND_SOURCES
                ),
            )
            non_contest_waiting = any(
                row["source"] in {
                    EXECUTION_SOURCE_MANUAL,
                    EXECUTION_SOURCE_HUMAN,
                }
                for row in queued
            )
            active_contest = int(
                conn.execute(
                    "SELECT COUNT(*) FROM execution_jobs WHERE source='contest' "
                    "AND status IN ('starting','running','settling')"
                ).fetchone()[0]
            )
            selected: dict | None = None
            invalid_job_ids: set[int] = set()
            projection_ready: bool | None = None
            # First preserve the configured contest share while runnable
            # manual/human work exists.  If that pass finds no runnable
            # non-contest job, relax only the share gate so capacity never sits
            # idle behind a temporarily blocked owner/rating/version request.
            for relax_contest_share in (False, True):
                if relax_contest_share and not (
                    non_contest_waiting
                    and active_contest >= max(1, int(contest_share_slots))
                ):
                    break
                for job in queued:
                    if int(job["id"]) in invalid_job_ids:
                        continue
                    active_contract = _active_game_contract_tx(
                        conn, str(job["game_id"])
                    )
                    frozen_contract = {
                        "ruleset_version": str(job.get("ruleset_version") or ""),
                        "protocol_version": str(job.get("protocol_version") or ""),
                        "rating_pool_id": str(job.get("rating_pool_id") or ""),
                    }
                    if frozen_contract != active_contract:
                        terminal = _now()
                        conn.execute(
                            "UPDATE execution_jobs SET status='cancelled',retryable=0,"
                            "terminal_reason='ruleset_retired',"
                            "last_error='ruleset_retired',terminal_at=? "
                            "WHERE id=? AND status='queued'",
                            (terminal, int(job["id"])),
                        )
                        if job.get("auto_decision_id") is not None:
                            conn.execute(
                                "UPDATE auto_match_decisions SET lifecycle='cancelled',"
                                "terminal_reason='ruleset_retired',terminal_at=? "
                                "WHERE id=? AND lifecycle='queued'",
                                (terminal, int(job["auto_decision_id"])),
                            )
                        self._backoff_contest_pairing_tx(conn, job)
                        invalid_job_ids.add(int(job["id"]))
                        continue
                    try:
                        from bzplat.backend.runtime.limits import (
                            execution_resource_snapshot,
                        )

                        frozen_resources = execution_resource_snapshot(
                            (
                                str(job["bot_a_environment"]),
                                str(job["bot_b_environment"]),
                            ),
                            int(job["profile_version"]),
                        )
                    except (TypeError, ValueError) as exc:
                        raise ExecutionInvariantError(
                            "execution resource profile snapshot is unknown"
                        ) from exc
                    persisted_resources = (
                        int(job["sandbox_units"]),
                        int(job.get("host_cpu_millis") or 0),
                        int(job.get("host_memory_mb") or 0),
                    )
                    if frozen_resources != persisted_resources:
                        raise ExecutionInvariantError(
                            "execution resource profile snapshot mismatch"
                        )
                    if claim_class == "auto":
                        from bzplat.backend.runtime.limits import (
                            maximum_execution_match_resource_snapshot,
                        )

                        (
                            reserve_sandbox_units,
                            reserve_cpu_millis,
                            reserve_memory_mb,
                        ) = maximum_execution_match_resource_snapshot()
                        if (
                            int(job["sandbox_units"]) + int(reserve_sandbox_units)
                            > capacity["max_sandbox_units"]
                            or int(job.get("host_cpu_millis") or 0)
                            + int(reserve_cpu_millis)
                            > capacity["max_host_cpu_millis"]
                            or int(job.get("host_memory_mb") or 0)
                            + int(reserve_memory_mb)
                            > capacity["max_host_memory_mb"]
                        ):
                            continue
                    if (
                        int(job["sandbox_units"])
                        + capacity["used_sandbox_units"]
                        > capacity["max_sandbox_units"]
                    ):
                        continue
                    if (
                        int(job.get("host_cpu_millis") or 0)
                        + capacity["used_host_cpu_millis"]
                        > capacity["max_host_cpu_millis"]
                        or int(job.get("host_memory_mb") or 0)
                        + capacity["used_host_memory_mb"]
                        > capacity["max_host_memory_mb"]
                    ):
                        # Resource snapshots are immutable.  In particular,
                        # an official match never falls back from the 2C/2GiB
                        # per-Bot profile to make an undersized host fit.
                        continue
                    if (
                        not relax_contest_share
                        and job["source"] == EXECUTION_SOURCE_CONTEST
                        and non_contest_waiting
                        and active_contest >= max(1, int(contest_share_slots))
                    ):
                        continue
                    owner_id = job.get("owner_user_id")
                    if owner_id is not None and job["source"] in {
                        EXECUTION_SOURCE_MANUAL,
                        EXECUTION_SOURCE_HUMAN,
                    }:
                        active_owner = conn.execute(
                            "SELECT COUNT(*) FROM execution_jobs WHERE owner_user_id=? "
                            "AND source IN ('manual','human') "
                            "AND status IN ('starting','running','settling')",
                            (owner_id,),
                        ).fetchone()[0]
                        if int(active_owner) >= max(1, int(user_active_limit)):
                            continue
                    invalid_versions: list[tuple[int, int | None]] = []
                    invalid_agents: list[int] = []
                    remote_agents: list[int] = []
                    for seat in (0, 1):
                        suffix = "a" if seat == 0 else "b"
                        environment = str(job[f"bot_{suffix}_environment"])
                        if environment == EXECUTION_ENV_HUMAN:
                            continue
                        bot_id = int(job[f"bot_{suffix}_id"])
                        if environment == EXECUTION_ENV_REMOTE_LOCAL:
                            frozen_agent = job.get(
                                f"bot_{suffix}_local_agent_id"
                            )
                            agent_id = (
                                int(frozen_agent)
                                if frozen_agent is not None
                                else 0
                            )
                            if not agent_id or not self._local_agent_identity_tx(
                                conn,
                                agent_id=agent_id,
                                bot_id=bot_id,
                                game_id=str(job["game_id"]),
                                expected_protocol=str(job["protocol_version"]),
                                request_owner_id=job.get("owner_user_id"),
                                enforce_request_owner=True,
                            ):
                                invalid_agents.append(agent_id)
                            else:
                                remote_agents.append(agent_id)
                            continue
                        frozen = job.get(f"bot_{suffix}_version_id")
                        version_id = int(frozen) if frozen is not None else None
                        if not self._version_identity_tx(
                            conn,
                            bot_id=bot_id,
                            version_id=version_id,
                            expected_game_id=str(job["game_id"]),
                            expected_protocol=str(job["protocol_version"]),
                        ):
                            invalid_versions.append((bot_id, version_id))
                    if invalid_agents:
                        conn.execute(
                            "UPDATE execution_jobs SET status='interrupted',retryable=1,"
                            "terminal_reason='local_agent_unavailable',"
                            "last_error='local_agent_unavailable',terminal_at=? "
                            "WHERE id=? AND status='queued'",
                            (_now(), int(job["id"])),
                        )
                        invalid_job_ids.add(int(job["id"]))
                        continue
                    # Offline/busy is volatile, unlike a revoked or mismatched
                    # identity. Skip this row without blocking later jobs.
                    if any(
                        not self._is_local_agent_available(agent_id)
                        or conn.execute(
                            "SELECT 1 FROM local_ai_leases WHERE agent_id=? "
                            "AND status='active' LIMIT 1",
                            (agent_id,),
                        ).fetchone()
                        is not None
                        for agent_id in remote_agents
                    ):
                        continue
                    if invalid_versions:
                        terminal = _now()
                        user_retryable = job["source"] in {
                            EXECUTION_SOURCE_MANUAL,
                            EXECUTION_SOURCE_HUMAN,
                        }
                        conn.execute(
                            "UPDATE execution_jobs SET status=?,retryable=?,"
                            "terminal_reason='version_unavailable',"
                            "last_error='version_unavailable',terminal_at=? "
                            "WHERE id=? AND status='queued'",
                            (
                                EXECUTION_INTERRUPTED
                                if user_retryable
                                else EXECUTION_CANCELLED,
                                1 if user_retryable else 0,
                                terminal,
                                int(job["id"]),
                            ),
                        )
                        if job.get("auto_decision_id") is not None:
                            conn.execute(
                                "UPDATE auto_match_decisions SET lifecycle='cancelled',"
                                "terminal_reason='version_unavailable',terminal_at=? "
                                "WHERE id=? AND lifecycle='queued'",
                                (terminal, int(job["auto_decision_id"])),
                            )
                        if job["source"] == EXECUTION_SOURCE_AUTO:
                            for bot_id, version_id in invalid_versions:
                                if version_id is None:
                                    conn.execute(
                                        "UPDATE bots SET is_active=0,updated_at=? "
                                        "WHERE id=? AND current_version=0",
                                        (terminal, bot_id),
                                    )
                                else:
                                    conn.execute(
                                        "UPDATE bots SET is_active=0,updated_at=? "
                                        "WHERE id=? AND current_version=(SELECT version "
                                        "FROM bot_versions WHERE id=? AND bot_id=?)",
                                        (terminal, bot_id, version_id, bot_id),
                                    )
                        self._backoff_contest_pairing_tx(conn, job)
                        invalid_job_ids.add(int(job["id"]))
                        continue
                    if int(job["rated"] or 0):
                        ranked_rows = conn.execute(
                            "SELECT id FROM bots WHERE is_ranked=1 "
                            "AND id IN (?,?)",
                            (int(job["bot_a_id"]), int(job["bot_b_id"])),
                        ).fetchall()
                        if len({int(row["id"]) for row in ranked_rows}) != 2:
                            terminal = _now()
                            conn.execute(
                                "UPDATE execution_jobs SET status='cancelled',"
                                "retryable=0,terminal_reason='ranking_entry_changed',"
                                "last_error='ranking_entry_changed',next_attempt_at=NULL,"
                                "terminal_at=? WHERE id=? AND status='queued'",
                                (terminal, int(job["id"])),
                            )
                            if job.get("auto_decision_id") is not None:
                                conn.execute(
                                    "UPDATE auto_match_decisions SET "
                                    "lifecycle='cancelled',"
                                    "terminal_reason='ranking_entry_changed',"
                                    "terminal_at=? WHERE id=? AND lifecycle='queued'",
                                    (terminal, int(job["auto_decision_id"])),
                                )
                            invalid_job_ids.add(int(job["id"]))
                            continue
                        if projection_ready is None:
                            projection_ready = bool(
                                self.store._rating_projection_status_tx(conn)["ready"]
                            )
                        if not projection_ready:
                            continue
                        if self.store._bot_has_active_rated_match_tx(
                            conn, int(job["bot_a_id"]), game_id=str(job["game_id"])
                        ) or self.store._bot_has_active_rated_match_tx(
                            conn, int(job["bot_b_id"]), game_id=str(job["game_id"])
                        ):
                            continue
                    if job["source"] == EXECUTION_SOURCE_CONTEST:
                        pairing = conn.execute(
                            "SELECT p.*,c.status AS contest_status,c.starts_at,"
                            "c.current_stage_idx AS contest_current_stage_idx,"
                            "c.stages_json AS contest_stages_json,c.game_id AS contest_game_id,"
                            "c.ruleset_version AS contest_ruleset_version,"
                            "c.protocol_version AS contest_protocol_version,"
                            "c.rating_pool_id AS contest_rating_pool_id "
                            "FROM contest_pairings p JOIN contests c ON c.id=p.contest_id "
                            "WHERE p.id=? AND p.contest_id=?",
                            (job["contest_pairing_id"], job["contest_id"]),
                        ).fetchone()
                        now = _now()
                        published_due = bool(
                            pairing is not None
                            and pairing["contest_status"] == "published"
                            and pairing["starts_at"]
                            and pairing["starts_at"] <= now
                        )
                        scheduled_due = bool(
                            pairing is not None
                            and (
                                not pairing["scheduled_at"]
                                or pairing["scheduled_at"] <= now
                            )
                        )
                        identity_unchanged = bool(
                            pairing is not None
                            and pairing["bot_a_id"] == job["bot_a_id"]
                            and pairing["bot_b_id"] == job["bot_b_id"]
                            and pairing["bot_a_version_id"]
                            == job["bot_a_version_id"]
                            and pairing["bot_b_version_id"]
                            == job["bot_b_version_id"]
                            and pairing["contest_ruleset_version"]
                            == job["ruleset_version"]
                            and pairing["contest_protocol_version"]
                            == job["protocol_version"]
                            and pairing["contest_rating_pool_id"]
                            == job["rating_pool_id"]
                        )
                        stage_contract_valid = False
                        if pairing is not None:
                            pairing_stage_idx = exact_nonnegative_int(
                                pairing["stage_idx"]
                            )
                            current_stage_idx = exact_nonnegative_int(
                                pairing["contest_current_stage_idx"]
                            )
                            try:
                                stages = json.loads(pairing["contest_stages_json"])
                            except (TypeError, ValueError):
                                stages = None
                            if (
                                isinstance(stages, list)
                                and pairing_stage_idx is not None
                                and current_stage_idx == pairing_stage_idx
                                and pairing_stage_idx < len(stages)
                                and isinstance(stages[pairing_stage_idx], dict)
                            ):
                                # Local import avoids store package startup
                                # cycles while preserving the same frozen-stage
                                # validator used by lifecycle/read models.
                                from bzplat.backend.contests.validation import (
                                    stage_scoring_contract_is_valid,
                                )

                                stage_contract_valid = (
                                    pairing["contest_game_id"] == job["game_id"]
                                    and stage_scoring_contract_is_valid(
                                        stages[pairing_stage_idx],
                                        game_id=str(job["game_id"]),
                                    )
                                )
                        if (
                            pairing is None
                            or pairing["status"] != STATUS_PENDING
                            or pairing["match_id"] is not None
                            or pairing["contest_status"] not in ("published", "running")
                            or not identity_unchanged
                            or not stage_contract_valid
                        ):
                            conn.execute(
                                "UPDATE execution_jobs SET status='cancelled',"
                                "terminal_reason='contest_pairing_changed',terminal_at=? "
                                "WHERE id=? AND status='queued'",
                                (_now(), int(job["id"])),
                            )
                            invalid_job_ids.add(int(job["id"]))
                            continue
                        if not scheduled_due or not (
                            pairing["contest_status"] == "running" or published_due
                        ):
                            # A valid future/manual-start pairing is held, not
                            # cancelled.  Its immutable request can be claimed
                            # once both the contest and per-pairing time gates pass.
                            continue
                    selected = job
                    break
                if selected is not None:
                    break
            if selected is None:
                return None

            match_id = _new_match_id()
            self._insert_match_tx(conn, job=selected, match_id=match_id)
            attempt_no = int(selected["attempt_count"] or 0) + 1
            now = _now()
            if selected["source"] == EXECUTION_SOURCE_CONTEST:
                changed = conn.execute(
                    "UPDATE contest_pairings SET match_id=?,status='running' "
                    "WHERE id=? AND contest_id=? AND status='pending' AND match_id IS NULL",
                    (
                        match_id,
                        selected["contest_pairing_id"],
                        selected["contest_id"],
                    ),
                )
                if changed.rowcount != 1:
                    raise ExecutionInvariantError("contest pairing claim lost")
                conn.execute(
                    "UPDATE contests SET status='running',starts_at=COALESCE(starts_at,?) "
                    "WHERE id=? AND status='published'",
                    (now, selected["contest_id"]),
                )
            for seat, field in enumerate(
                ("bot_a_local_agent_id", "bot_b_local_agent_id")
            ):
                agent_id = selected.get(field)
                if agent_id is None:
                    continue
                try:
                    conn.execute(
                        "INSERT INTO local_ai_leases("
                        "agent_id,job_public_id,attempt_no,seat,status,acquired_at) "
                        "VALUES(?,?,?,?,'active',?)",
                        (
                            int(agent_id),
                            str(selected["public_id"]),
                            attempt_no,
                            seat,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise ExecutionInvariantError(
                        "local AI lease claim lost"
                    ) from exc
            changed = conn.execute(
                "UPDATE execution_jobs SET status='starting',current_match_id=?,"
                "attempt_count=?,claimed_at=?,started_at=NULL,settling_at=NULL,"
                "terminal_at=NULL,cleanup_state='none',retryable=0,terminal_reason='',"
                "last_error='',next_attempt_at=NULL WHERE id=? AND status='queued'",
                (match_id, attempt_no, now, int(selected["id"])),
            )
            if changed.rowcount != 1:
                raise ExecutionInvariantError("execution job claim CAS lost")
            conn.execute(
                "INSERT INTO execution_job_attempts("
                "job_id,attempt_no,match_id,status,created_at) "
                "VALUES(?, ?, ?, 'starting', ?)",
                (int(selected["id"]), attempt_no, match_id, now),
            )
            if selected.get("auto_decision_id") is not None:
                conn.execute(
                    "UPDATE auto_match_decisions SET lifecycle='dispatched',match_id=?,"
                    "attempt_count=attempt_count+1,dispatched_at=? "
                    "WHERE id=? AND lifecycle='queued'",
                    (match_id, now, int(selected["auto_decision_id"])),
                )
            result = conn.execute(
                "SELECT * FROM execution_jobs WHERE id=?", (int(selected["id"]),)
            ).fetchone()
            return dict(result)

    # ------------------------------------------------------------------
    # Runtime transitions and recovery
    # ------------------------------------------------------------------
    @staticmethod
    def _release_local_agent_leases_tx(
        conn: sqlite3.Connection,
        *,
        public_id: str,
        attempt_no: int,
        reason: str,
    ) -> None:
        conn.execute(
            "UPDATE local_ai_leases SET status='released',released_at=?,"
            "terminal_reason=? WHERE job_public_id=? AND attempt_no=? "
            "AND status='active'",
            (
                _now(),
                str(reason)[:200],
                str(public_id),
                int(attempt_no),
            ),
        )

    def assert_active_attempt(self, public_id: str, attempt_no: int) -> None:
        with self.store._tx() as conn:
            row = conn.execute(
                "SELECT status,attempt_count,cancel_requested "
                "FROM execution_jobs WHERE public_id=?",
                (public_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] not in EXECUTION_ACTIVE_STATES
                or int(row["attempt_count"] or 0) != int(attempt_no)
                or int(row["cancel_requested"] or 0) != 0
            ):
                raise ExecutionAttemptNotCurrent(
                    "execution attempt is no longer current"
                )

    def mark_cleanup_pending(self, public_id: str, reason: str) -> None:
        manual_pause = False
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE execution_jobs SET cleanup_state='pending',last_error=? "
                "WHERE public_id=? AND status IN ('starting','running','settling')",
                (str(reason)[:1000], public_id),
            )
            manual_pause = self._docker_launch_tx(conn)["state"] == "creating"
        self.pause_for_docker_uncertainty(reason, manual=manual_pause)

    def mark_cleanup_confirmed(self, public_id: str, attempt_no: int) -> None:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._release_local_agent_leases_tx(
                conn,
                public_id=public_id,
                attempt_no=attempt_no,
                reason="cleanup_confirmed",
            )
            conn.execute(
                # Physical cleanup and the cause that required recovery are
                # independent facts.  Preserve ``last_error`` until the
                # recovery state machine has assigned source-specific retry
                # semantics; clearing it here would turn a runtime failure
                # into an apparent ordinary restart and bypass backoff.
                "UPDATE execution_jobs SET cleanup_state='confirmed' "
                "WHERE public_id=? AND attempt_count=? "
                "AND status IN ('starting','running','settling')",
                (public_id, int(attempt_no)),
            )

    def request_cancel(self, public_id: str, *, owner_user_id: int | None) -> dict:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM execution_jobs WHERE public_id=?", (public_id,)
            ).fetchone()
            if job is None:
                raise ValueError("执行请求不存在")
            if owner_user_id is not None and job["owner_user_id"] != owner_user_id:
                raise PermissionError("无权取消该执行请求")
            if job["status"] == EXECUTION_QUEUED:
                now = _now()
                conn.execute(
                    "UPDATE execution_jobs SET status='cancelled',cancel_requested=1,"
                    "terminal_reason='user_cancelled',terminal_at=? WHERE id=?",
                    (now, int(job["id"])),
                )
                if job["auto_decision_id"] is not None:
                    conn.execute(
                        "UPDATE auto_match_decisions SET lifecycle='cancelled',"
                        "terminal_reason='cancelled',terminal_at=? WHERE id=?",
                        (now, int(job["auto_decision_id"])),
                    )
                self._backoff_contest_pairing_tx(conn, dict(job))
            elif job["status"] in {EXECUTION_STARTING, EXECUTION_RUNNING}:
                conn.execute(
                    "UPDATE execution_jobs SET cancel_requested=1 WHERE id=?",
                    (int(job["id"]),),
                )
            elif job["status"] == EXECUTION_SETTLING:
                raise ValueError("执行请求正在收尾，不能再取消")
            return dict(
                conn.execute(
                    "SELECT * FROM execution_jobs WHERE id=?", (int(job["id"]),)
                ).fetchone()
            )

    def retry(self, public_id: str, *, owner_user_id: int | None) -> dict:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute(
                "SELECT * FROM execution_jobs WHERE public_id=?", (public_id,)
            ).fetchone()
            if job is None:
                raise ValueError("执行请求不存在")
            if owner_user_id is not None and job["owner_user_id"] != owner_user_id:
                raise PermissionError("无权重试该执行请求")
            if job["status"] != EXECUTION_INTERRUPTED or not int(job["retryable"]):
                raise ValueError("该执行请求当前不可重试")
            active_contract = _active_game_contract_tx(conn, str(job["game_id"]))
            frozen_contract = {
                "ruleset_version": str(job["ruleset_version"] or ""),
                "protocol_version": str(job["protocol_version"] or ""),
                "rating_pool_id": str(job["rating_pool_id"] or ""),
            }
            if frozen_contract != active_contract:
                raise ValueError("该执行请求使用的规则版本已退役")
            control = conn.execute(
                "SELECT accepting,deployment_drain_requested "
                "FROM execution_control WHERE singleton=1"
            ).fetchone()
            if control is None or int(control["accepting"] or 0) != 1:
                if control is not None and int(
                    control["deployment_drain_requested"] or 0
                ):
                    raise ExecutionQueueClosed(
                        "平台正在部署维护，暂不接收重试请求",
                        code="deployment_maintenance",
                    )
                raise ExecutionQueueClosed(
                    "执行队列正在启动或停止，请稍后重试"
                )
            conn.execute(
                "UPDATE execution_jobs SET status='queued',current_match_id=NULL,"
                "cancel_requested=0,cleanup_state='none',retryable=0,terminal_reason='',"
                "last_error='',claimed_at=NULL,started_at=NULL,settling_at=NULL,"
                "terminal_at=NULL,failure_count=0,next_attempt_at=NULL WHERE id=?",
                (int(job["id"]),),
            )
            if job["auto_decision_id"] is not None:
                conn.execute(
                    "UPDATE auto_match_decisions SET lifecycle='queued',match_id=NULL,"
                    "dispatched_at=NULL,last_attempt_error='manual_retry' WHERE id=?",
                    (int(job["auto_decision_id"]),),
                )
            if job["source"] in {
                EXECUTION_SOURCE_MANUAL,
                EXECUTION_SOURCE_HUMAN,
            }:
                self._yield_auto_to_foreground_tx(conn)
            return dict(
                conn.execute(
                    "SELECT * FROM execution_jobs WHERE id=?", (int(job["id"]),)
                ).fetchone()
            )

    def rollback_unstarted_claim(self, public_id: str, *, reason: str) -> bool:
        """Compensate a definite task-start failure before any public event."""
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job_row = conn.execute(
                "SELECT * FROM execution_jobs WHERE public_id=? AND status='starting'",
                (public_id,),
            ).fetchone()
            if job_row is None:
                return False
            job = dict(job_row)
            match_id = str(job.get("current_match_id") or "")
            replay = conn.execute(
                "SELECT events_json FROM match_replays WHERE match_id=?", (match_id,)
            ).fetchone()
            try:
                events = json.loads((replay["events_json"] if replay else "") or "[]")
            except (TypeError, ValueError):
                return False
            match = conn.execute(
                f"SELECT status FROM {_matches_table(job['game_id'])} WHERE id=?",
                (match_id,),
            ).fetchone()
            if events or match is None or match["status"] != STATUS_PENDING:
                return False
            if job["source"] == EXECUTION_SOURCE_CONTEST:
                conn.execute(
                    "UPDATE contest_pairings SET match_id=NULL,status='pending' "
                    "WHERE id=? AND match_id=?",
                    (job["contest_pairing_id"], match_id),
                )
            self._delete_unstarted_match_tx(
                conn, game_id=str(job["game_id"]), match_id=match_id
            )
            now = _now()
            self._release_local_agent_leases_tx(
                conn,
                public_id=public_id,
                attempt_no=int(job["attempt_count"]),
                reason=str(reason),
            )
            cancel_reason = ""
            if int(job.get("cancel_requested") or 0):
                persisted_reason = str(job.get("terminal_reason") or "")
                cancel_reason = (
                    persisted_reason
                    if persisted_reason in _AUTO_YIELD_REASONS
                    else "user_cancelled"
                )
            conn.execute(
                "UPDATE execution_job_attempts SET status=?,"
                "terminal_at=?,terminal_reason=? WHERE job_id=? AND match_id=?",
                (
                    "cancelled" if cancel_reason else "interrupted",
                    now,
                    cancel_reason or str(reason)[:200],
                    int(job["id"]),
                    match_id,
                ),
            )
            failure_count = int(job.get("failure_count") or 0) + 1
            if cancel_reason:
                conn.execute(
                    "UPDATE execution_jobs SET status='cancelled',"
                    "current_match_id=NULL,cleanup_state='confirmed',retryable=0,"
                    "last_error='',terminal_reason=?,terminal_at=?,"
                    "next_attempt_at=NULL WHERE id=?",
                    (cancel_reason, now, int(job["id"])),
                )
                if job["source"] == EXECUTION_SOURCE_AUTO:
                    self._advance_auto_gate_tx(
                        conn,
                        seconds=AUTO_MATCH_COOLDOWN_SECONDS,
                        reason="cooldown",
                    )
            elif job["source"] in {
                EXECUTION_SOURCE_MANUAL,
                EXECUTION_SOURCE_HUMAN,
            }:
                conn.execute(
                    "UPDATE execution_jobs SET status='interrupted',"
                    "current_match_id=NULL,terminal_at=?,cleanup_state='confirmed',"
                    "retryable=1,last_error=?,terminal_reason='runtime_failure',"
                    "failure_count=?,next_attempt_at=NULL WHERE id=?",
                    (
                        now,
                        str(reason)[:1000],
                        failure_count,
                        int(job["id"]),
                    ),
                )
            else:
                delay = min(60, 2 ** min(failure_count - 1, 6))
                # Flooring to whole seconds can collapse the first retry delay
                # to almost zero when the transaction crosses a clock boundary.
                next_attempt_at = (
                    datetime.now() + timedelta(seconds=delay)
                ).isoformat(timespec="microseconds")
                conn.execute(
                    "UPDATE execution_jobs SET status='queued',current_match_id=NULL,"
                    "claimed_at=NULL,started_at=NULL,settling_at=NULL,terminal_at=NULL,"
                    "cleanup_state='none',last_error=?,terminal_reason='',"
                    "failure_count=?,next_attempt_at=? WHERE id=?",
                    (
                        str(reason)[:1000],
                        failure_count,
                        next_attempt_at,
                        int(job["id"]),
                    ),
                )
                if job["source"] == EXECUTION_SOURCE_CONTEST:
                    conn.execute(
                        "UPDATE contest_pairings SET scheduled_at=CASE "
                        "WHEN scheduled_at IS NULL OR scheduled_at<? THEN ? "
                        "ELSE scheduled_at END WHERE id=? AND match_id IS NULL",
                        (
                            next_attempt_at,
                            next_attempt_at,
                            job["contest_pairing_id"],
                        ),
                    )
            if job.get("auto_decision_id") is not None:
                if cancel_reason:
                    conn.execute(
                        "UPDATE auto_match_decisions SET lifecycle='cancelled',"
                        "match_id=NULL,terminal_reason=?,terminal_at=? WHERE id=?",
                        (cancel_reason, now, int(job["auto_decision_id"])),
                    )
                else:
                    conn.execute(
                        "UPDATE auto_match_decisions SET lifecycle='queued',"
                        "match_id=NULL,dispatched_at=NULL,last_attempt_error=? "
                        "WHERE id=?",
                        (str(reason)[:200], int(job["auto_decision_id"])),
                    )
            return True

    @staticmethod
    def _delete_unstarted_match_tx(
        conn: sqlite3.Connection, *, game_id: str, match_id: str
    ) -> None:
        policy = conn.execute(
            "SELECT settled_order FROM match_rating_policies WHERE match_id=?",
            (match_id,),
        ).fetchone()
        if policy is not None and policy["settled_order"] is not None:
            raise ExecutionInvariantError("cannot delete reserved rating attempt")
        if conn.execute(
            "SELECT 1 FROM match_rating_settlements WHERE match_id=?", (match_id,)
        ).fetchone():
            raise ExecutionInvariantError("cannot delete settled rating attempt")
        conn.execute("DELETE FROM match_debug_entries WHERE match_id=?", (match_id,))
        conn.execute("DELETE FROM match_debug_sessions WHERE match_id=?", (match_id,))
        conn.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
        conn.execute("DELETE FROM match_rating_policies WHERE match_id=?", (match_id,))
        conn.execute(f"DELETE FROM {_matches_table(game_id)} WHERE id=?", (match_id,))
        conn.execute("DELETE FROM matches_index WHERE id=?", (match_id,))

    def recover_after_namespace_cleanup(self) -> dict:
        """Recover active requests only after the exact instance label is zero."""
        recovered = {"requeued": 0, "interrupted": 0, "settling": 0}
        auto_recovered = False
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            launch = self._docker_launch_tx(conn)
            if launch["state"] != "idle":
                raise DockerLaunchInvariantError(
                    "Docker launch journal 尚未收敛，禁止恢复执行请求"
                )
            jobs = conn.execute(
                "SELECT * FROM execution_jobs WHERE status IN "
                "('starting','running','settling') ORDER BY id"
            ).fetchall()
            for raw in jobs:
                job = dict(raw)
                auto_recovered = auto_recovered or (
                    job["source"] == EXECUTION_SOURCE_AUTO
                )
                self._release_local_agent_leases_tx(
                    conn,
                    public_id=str(job["public_id"]),
                    attempt_no=int(job["attempt_count"]),
                    reason="namespace_recovery",
                )
                match_id = str(job.get("current_match_id") or "")
                if not match_id:
                    raise ExecutionInvariantError(
                        f"active job without match: {job['public_id']}"
                    )
                table = _matches_table(job["game_id"])
                match = conn.execute(
                    f"SELECT * FROM {table} WHERE id=?", (match_id,)
                ).fetchone()
                if match is None:
                    raise ExecutionInvariantError(
                        f"active job match missing: {job['public_id']}"
                    )
                replay = conn.execute(
                    "SELECT events_json FROM match_replays WHERE match_id=?",
                    (match_id,),
                ).fetchone()
                try:
                    events = json.loads((replay["events_json"] if replay else "") or "[]")
                except (TypeError, ValueError):
                    events = ["corrupt"]
                events_observed = bool(events)
                runtime_failed = bool(str(job.get("last_error") or "").strip())
                next_failure_count = int(job.get("failure_count") or 0)
                next_attempt_at: str | None = None
                if runtime_failed:
                    next_failure_count += 1
                    if job["source"] in {
                        EXECUTION_SOURCE_AUTO,
                        EXECUTION_SOURCE_CONTEST,
                    }:
                        delay = min(60, 2 ** min(next_failure_count - 1, 6))
                        # Preserve the full backoff across second boundaries.
                        next_attempt_at = (
                            datetime.now() + timedelta(seconds=delay)
                        ).isoformat(timespec="microseconds")
                if match["status"] in (STATUS_COMPLETED, STATUS_ABORTED):
                    # A terminal Match is the durable winner of a crash race.
                    # Reconcile may have attached a scheduler-yield marker to
                    # the still-active job immediately before recovery read the
                    # already-terminal Match.  Preserve a genuine prior yield,
                    # but do not reinterpret natural completion or an
                    # unrelated abort as a scheduler cancellation.
                    persisted_reason = str(job.get("terminal_reason") or "")
                    match_reason = str(match["reason"] or "")
                    if (
                        job["source"] == EXECUTION_SOURCE_AUTO
                        and int(job.get("cancel_requested") or 0)
                        and persisted_reason in _AUTO_YIELD_REASONS
                        and (
                            match["status"] == STATUS_COMPLETED
                            or match_reason != persisted_reason
                        )
                    ):
                        conn.execute(
                            "UPDATE execution_jobs SET cancel_requested=0,"
                            "terminal_reason='' WHERE id=?",
                            (int(job["id"]),),
                        )
                    conn.execute(
                        "UPDATE execution_jobs SET status='settling',settling_at=?,"
                        "cleanup_state='confirmed' WHERE id=?",
                        (_now(), int(job["id"])),
                    )
                    recovered["settling"] += 1
                    continue
                if (
                    job["source"] == EXECUTION_SOURCE_AUTO
                    and int(job.get("cancel_requested") or 0)
                    and str(job.get("terminal_reason") or "")
                    in _AUTO_YIELD_REASONS
                ):
                    yield_reason = str(job["terminal_reason"])
                    now = _now()
                    conn.execute(
                        f"UPDATE {table} SET status='aborted',reason=?,ended_at=? "
                        "WHERE id=? AND status IN ('pending','running')",
                        (yield_reason, now, match_id),
                    )
                    terminal_event = {"type": "error", "reason": yield_reason}
                    if not events or events[-1] != terminal_event:
                        events.append(terminal_event)
                    conn.execute(
                        "UPDATE match_replays SET events_json=?,updated_at=? "
                        "WHERE match_id=?",
                        (
                            json.dumps(
                                events,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                            now,
                            match_id,
                        ),
                    )
                    conn.execute(
                        "UPDATE execution_jobs SET status='settling',settling_at=?,"
                        "cleanup_state='confirmed' WHERE id=?",
                        (now, int(job["id"])),
                    )
                    conn.execute(
                        "UPDATE execution_job_attempts SET status='settling',"
                        "events_observed=?,terminal_reason=? "
                        "WHERE job_id=? AND match_id=?",
                        (
                            1 if events else 0,
                            yield_reason,
                            int(job["id"]),
                            match_id,
                        ),
                    )
                    recovered["settling"] += 1
                    continue
                if not events_observed:
                    if job["source"] == EXECUTION_SOURCE_CONTEST:
                        conn.execute(
                            "UPDATE contest_pairings SET match_id=NULL,status='pending' "
                            "WHERE id=? AND match_id=?",
                            (job["contest_pairing_id"], match_id),
                        )
                    self._delete_unstarted_match_tx(
                        conn, game_id=job["game_id"], match_id=match_id
                    )
                    conn.execute(
                        "UPDATE execution_job_attempts SET status='interrupted',"
                        "terminal_at=?,terminal_reason=? "
                        "WHERE job_id=? AND match_id=?",
                        (
                            _now(),
                            "runtime_failure_before_start"
                            if runtime_failed
                            else "restart_before_start",
                            int(job["id"]),
                            match_id,
                        ),
                    )
                    if runtime_failed and job["source"] in {
                        EXECUTION_SOURCE_MANUAL,
                        EXECUTION_SOURCE_HUMAN,
                    }:
                        now = _now()
                        conn.execute(
                            "UPDATE execution_jobs SET status='interrupted',"
                            "current_match_id=NULL,retryable=1,terminal_at=?,"
                            "cleanup_state='confirmed',terminal_reason='runtime_failure',"
                            "failure_count=?,next_attempt_at=NULL WHERE id=?",
                            (now, next_failure_count, int(job["id"])),
                        )
                        recovered["interrupted"] += 1
                        continue
                    conn.execute(
                        "UPDATE execution_jobs SET status='queued',current_match_id=NULL,"
                        "claimed_at=NULL,started_at=NULL,settling_at=NULL,terminal_at=NULL,"
                        "cleanup_state='none',last_error='',terminal_reason='',"
                        "failure_count=?,next_attempt_at=? WHERE id=?",
                        (
                            next_failure_count,
                            next_attempt_at,
                            int(job["id"]),
                        ),
                    )
                    if (
                        job["source"] == EXECUTION_SOURCE_CONTEST
                        and next_attempt_at is not None
                    ):
                        conn.execute(
                            "UPDATE contest_pairings SET scheduled_at=CASE "
                            "WHEN scheduled_at IS NULL OR scheduled_at<? THEN ? "
                            "ELSE scheduled_at END WHERE id=? AND match_id IS NULL",
                            (
                                next_attempt_at,
                                next_attempt_at,
                                job["contest_pairing_id"],
                            ),
                        )
                    if job.get("auto_decision_id") is not None:
                        conn.execute(
                            "UPDATE auto_match_decisions SET lifecycle='queued',"
                            "match_id=NULL,dispatched_at=NULL,last_attempt_error=? "
                            "WHERE id=?",
                            (
                                str(job.get("last_error") or "restart_before_start")[:200],
                                int(job["auto_decision_id"]),
                            ),
                        )
                    recovered["requeued"] += 1
                    continue

                now = _now()
                conn.execute(
                    f"UPDATE {table} SET status='aborted',reason='orphan_after_restart',"
                    "ended_at=? WHERE id=? AND status IN ('pending','running')",
                    (now, match_id),
                )
                events.append(
                    {"type": "error", "reason": "orphan_after_restart"}
                )
                conn.execute(
                    "UPDATE match_replays SET events_json=?,updated_at=? "
                    "WHERE match_id=?",
                    (
                        json.dumps(
                            events,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ),
                        now,
                        match_id,
                    ),
                )
                conn.execute(
                    "UPDATE execution_job_attempts SET status='interrupted',events_observed=1,"
                    "terminal_at=?,terminal_reason='orphan_after_restart' "
                    "WHERE job_id=? AND match_id=?",
                    (now, int(job["id"]), match_id),
                )
                automatic_retry = job["source"] in {
                    EXECUTION_SOURCE_AUTO,
                    EXECUTION_SOURCE_CONTEST,
                }
                if job["source"] == EXECUTION_SOURCE_CONTEST:
                    conn.execute(
                        "UPDATE contest_pairings SET match_id=NULL,status='pending' "
                        "WHERE id=? AND match_id=?",
                        (job["contest_pairing_id"], match_id),
                    )
                if automatic_retry:
                    conn.execute(
                        "UPDATE execution_jobs SET status='queued',current_match_id=NULL,"
                        "claimed_at=NULL,started_at=NULL,settling_at=NULL,terminal_at=NULL,"
                        "cleanup_state='none',last_error='',terminal_reason='',"
                        "failure_count=?,next_attempt_at=? WHERE id=?",
                        (
                            next_failure_count,
                            next_attempt_at,
                            int(job["id"]),
                        ),
                    )
                    if (
                        job["source"] == EXECUTION_SOURCE_CONTEST
                        and next_attempt_at is not None
                    ):
                        conn.execute(
                            "UPDATE contest_pairings SET scheduled_at=CASE "
                            "WHEN scheduled_at IS NULL OR scheduled_at<? THEN ? "
                            "ELSE scheduled_at END WHERE id=? AND match_id IS NULL",
                            (
                                next_attempt_at,
                                next_attempt_at,
                                job["contest_pairing_id"],
                            ),
                        )
                    if job.get("auto_decision_id") is not None:
                        conn.execute(
                            "UPDATE auto_match_decisions SET lifecycle='queued',"
                            "match_id=NULL,dispatched_at=NULL,last_attempt_error=? "
                            "WHERE id=?",
                            (
                                str(job.get("last_error") or "orphan_after_restart")[:200],
                                int(job["auto_decision_id"]),
                            ),
                        )
                    recovered["requeued"] += 1
                else:
                    conn.execute(
                        "UPDATE execution_jobs SET status='interrupted',retryable=1,"
                        "cleanup_state='confirmed',terminal_reason='orphan_after_restart',"
                        "last_error='',terminal_at=?,failure_count=?,"
                        "next_attempt_at=NULL WHERE id=?",
                        (now, next_failure_count, int(job["id"])),
                    )
                    recovered["interrupted"] += 1
            if auto_recovered:
                self._advance_auto_gate_tx(
                    conn,
                    seconds=AUTO_MATCH_COOLDOWN_SECONDS,
                    reason="cooldown",
                )
        return recovered

    def finalize_ready(self) -> int:
        """Release capacity after terminal persistence and exact cleanup proof.

        Rating settlement is intentionally not a global capacity dimension.
        A completed-but-unsettled rated match remains protected by the per-Bot
        rated-overlap gate, while unrelated neutral/rated jobs may proceed.
        """
        finalized = 0
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            jobs = conn.execute(
                "SELECT * FROM execution_jobs WHERE status='settling' "
                "AND cleanup_state='confirmed' ORDER BY id"
            ).fetchall()
            for raw in jobs:
                job = dict(raw)
                match_id = str(job["current_match_id"])
                match = conn.execute(
                    f"SELECT status,reason FROM {_matches_table(job['game_id'])} "
                    "WHERE id=?",
                    (match_id,),
                ).fetchone()
                if match is None:
                    raise ExecutionInvariantError("settling match missing")
                if match["status"] not in (STATUS_COMPLETED, STATUS_ABORTED):
                    raise ExecutionInvariantError(
                        "settling execution has non-terminal match"
                    )
                terminal = _now()
                terminal_status = (
                    EXECUTION_COMPLETED
                    if match["status"] == STATUS_COMPLETED
                    else EXECUTION_CANCELLED
                    if int(job["cancel_requested"] or 0)
                    else EXECUTION_INTERRUPTED
                )
                retryable = (
                    1
                    if terminal_status == EXECUTION_INTERRUPTED
                    and job["source"] in {
                        EXECUTION_SOURCE_MANUAL,
                        EXECUTION_SOURCE_HUMAN,
                    }
                    else 0
                )
                reason = str(match["reason"] or "")
                conn.execute(
                    "UPDATE execution_jobs SET status=?,retryable=?,terminal_reason=?,"
                    "terminal_at=? WHERE id=?",
                    (terminal_status, retryable, reason, terminal, int(job["id"])),
                )
                conn.execute(
                    "UPDATE execution_job_attempts SET status=?,terminal_reason=?,"
                    "terminal_at=? WHERE job_id=? AND match_id=?",
                    (
                        "completed"
                        if terminal_status == EXECUTION_COMPLETED
                        else "cancelled"
                        if terminal_status == EXECUTION_CANCELLED
                        else "interrupted",
                        reason,
                        terminal,
                        int(job["id"]),
                        match_id,
                    ),
                )
                if job.get("auto_decision_id") is not None:
                    decision = conn.execute(
                        "SELECT * FROM auto_match_decisions WHERE id=?",
                        (int(job["auto_decision_id"]),),
                    ).fetchone()
                    if decision is not None and decision["lifecycle"] == "dispatched":
                        if terminal_status == EXECUTION_COMPLETED:
                            marker = conn.execute(
                                "SELECT settled_order FROM match_rating_settlements "
                                "WHERE match_id=?",
                                (match_id,),
                            ).fetchone()
                            self.store._auto_complete_service_tx(
                                conn, decision, terminal
                            )
                            conn.execute(
                                "UPDATE auto_match_decisions SET lifecycle='completed',"
                                "terminal_at=?,terminal_reason=?,settlement_order=? "
                                "WHERE id=?",
                                (
                                    terminal,
                                    reason,
                                    marker["settled_order"] if marker else None,
                                    int(job["auto_decision_id"]),
                                ),
                            )
                        else:
                            conn.execute(
                                "UPDATE auto_match_decisions SET lifecycle='aborted',"
                                "terminal_at=?,terminal_reason=? WHERE id=?",
                                (terminal, reason, int(job["auto_decision_id"])),
                            )
                if job["source"] == EXECUTION_SOURCE_AUTO:
                    self._advance_auto_gate_tx(
                        conn,
                        seconds=AUTO_MATCH_COOLDOWN_SECONDS,
                        reason="cooldown",
                    )
                elif job["source"] in {
                    EXECUTION_SOURCE_MANUAL,
                    EXECUTION_SOURCE_HUMAN,
                } or (
                    job["source"] == EXECUTION_SOURCE_CONTEST
                    and not conn.execute(
                        "SELECT 1 FROM contests WHERE id=? "
                        "AND showcase_key IS NOT NULL",
                        (job.get("contest_id"),),
                    ).fetchone()
                ):
                    self._advance_auto_gate_tx(
                        conn,
                        seconds=AUTO_MATCH_IDLE_GRACE_SECONDS,
                        reason="idle_grace",
                    )
                finalized += 1
        return finalized

    # ------------------------------------------------------------------
    # Automatic fairness producer
    # ------------------------------------------------------------------
    def refill_auto(
        self,
        *,
        target_queued: int,
        bootstrap_target_matches: int,
    ) -> dict:
        inserted = 0
        effective_target = min(
            max(0, int(target_queued)), EXECUTION_AUTO_LOOKAHEAD
        )
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            control = conn.execute(
                "SELECT auto_enabled FROM execution_control WHERE singleton=1"
            ).fetchone()
            if not control or int(control["auto_enabled"] or 0) != 1:
                return {"outcome": "disabled", "inserted": 0}
            scheduler = self._auto_scheduler_tx(conn)
            if scheduler["state"] != "ready":
                return {
                    "outcome": scheduler["state"],
                    "inserted": 0,
                    "auto_scheduler": scheduler,
                }
            projection = self.store._rating_projection_status_tx(conn)
            if not projection["ready"]:
                return {
                    "outcome": "rating_unverified",
                    "inserted": 0,
                    "rating_projection": projection,
                }
            fair = conn.execute(
                "SELECT * FROM auto_match_fair_state WHERE singleton=1"
            ).fetchone()
            if fair is None:
                raise ExecutionInvariantError("auto fair singleton missing")
            queued_count = int(
                conn.execute(
                    "SELECT COUNT(*) FROM execution_jobs WHERE source='auto' "
                    "AND status='queued'"
                ).fetchone()[0]
            )
            games = sorted(
                row[0]
                for row in conn.execute(
                    "SELECT DISTINCT game_id FROM ratings ORDER BY game_id"
                ).fetchall()
                if row[0]
            )
            while queued_count < effective_target and games:
                candidates = self.store._auto_queue_candidates_tx(conn)
                healthy_candidates: list[dict] = []
                for candidate in candidates:
                    runtime = {
                        "binary_path": candidate.get("version_binary_path"),
                        "checksum": candidate.get("version_checksum"),
                        "size_bytes": candidate.get("version_size_bytes"),
                    }
                    try:
                        require_binary_file_integrity(
                            runtime,
                            str(runtime["binary_path"] or ""),
                            cache=self._binary_integrity_cache,
                        )
                    except (OSError, TypeError, ValueError):
                        # The selected row is the Bot's current immutable
                        # version. Quarantine only while that exact version is
                        # still current, preventing a corrupt artifact from
                        # generating a decision/job every dispatcher tick. A
                        # new upload or explicit owner reactivation is the
                        # recovery boundary.
                        conn.execute(
                            "UPDATE bots SET is_active=0,updated_at=? "
                            "WHERE id=? AND current_version=?",
                            (
                                _now(),
                                int(candidate["bot_id"]),
                                int(candidate["version_number"]),
                            ),
                        )
                        continue
                    healthy_candidates.append(candidate)
                candidates = healthy_candidates
                state = conn.execute(
                    "SELECT * FROM auto_match_fair_state WHERE singleton=1"
                ).fetchone()
                cursor = int(state["next_game_idx"] or 0) % len(games)
                requested_lane = (
                    "bootstrap"
                    if int(state["next_lane"] or 0) == 0
                    else "established"
                )
                rotated = games[cursor:] + games[:cursor]
                selected = None
                selected_game_idx = cursor
                actual_lane = requested_lane
                lane_fallback = ""
                for candidate_lane in (
                    requested_lane,
                    "established"
                    if requested_lane == "bootstrap"
                    else "bootstrap",
                ):
                    for gid in rotated:
                        choice = self.store._auto_choose_pair_tx(
                            conn,
                            candidates,
                            game_id=gid,
                            lane=candidate_lane,
                            bootstrap_target_matches=max(
                                0, int(bootstrap_target_matches)
                            ),
                        )
                        if choice is not None:
                            selected = choice
                            selected_game_idx = games.index(gid)
                            actual_lane = candidate_lane
                            if candidate_lane != requested_lane:
                                lane_fallback = "requested_lane_empty"
                            break
                    if selected is not None:
                        break
                if selected is None:
                    break
                anchor, partner, partner_fallback, bot_pair, owner_pair, gap = selected
                debt_anchor = self.store._auto_queue_seat_debt_tx(conn, anchor)
                debt_partner = self.store._auto_queue_seat_debt_tx(conn, partner)
                normal = abs(debt_anchor + 1) + abs(debt_partner - 1)
                reverse = abs(debt_anchor - 1) + abs(debt_partner + 1)
                if reverse < normal or (
                    reverse == normal
                    and int(anchor["bot_id"]) > int(partner["bot_id"])
                ):
                    bot_a, bot_b = partner, anchor
                    debt_a, debt_b = debt_partner, debt_anchor
                else:
                    bot_a, bot_b = anchor, partner
                    debt_a, debt_b = debt_anchor, debt_partner
                fallback = ",".join(
                    part for part in (lane_fallback, partner_fallback) if part
                )
                reason = (
                    ("冷启动通道" if actual_lane == "bootstrap" else "稳定通道")
                    + f" · owner/排位代表轮转 · Bot交手 {bot_pair} · "
                    + f"owner交手 {owner_pair} · Rating差 {gap:.0f} · 先后手平衡"
                )
                now = _now()
                decision = conn.execute(
                    "INSERT INTO auto_match_decisions("
                    "policy_version,state_revision,cursor_game_idx,requested_lane,"
                    "actual_lane,fallback_reason,game_id,bot_a_id,bot_b_id,owner_a_id,"
                    "owner_b_id,bot_a_version_id,bot_b_version_id,owner_a_service_before,"
                    "owner_b_service_before,bot_a_service_before,bot_b_service_before,"
                    "bot_pair_count_before,owner_pair_count_before,rating_gap,"
                    "bot_a_seat_debt_before,bot_b_seat_debt_before,selection_reason,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        "owner-game-ranked-bot-v5-bootstrap",
                        int(state["revision"] or 0),
                        cursor,
                        requested_lane,
                        actual_lane,
                        fallback,
                        bot_a["game_id"],
                        int(bot_a["bot_id"]),
                        int(bot_b["bot_id"]),
                        int(bot_a["owner_id"]),
                        int(bot_b["owner_id"]),
                        int(bot_a["version_id"]),
                        int(bot_b["version_id"]),
                        int(bot_a.get("owner_service") or 0),
                        int(bot_b.get("owner_service") or 0),
                        int(bot_a.get("bot_service") or 0),
                        int(bot_b.get("bot_service") or 0),
                        int(bot_pair),
                        int(owner_pair),
                        float(gap),
                        int(debt_a),
                        int(debt_b),
                        reason,
                        now,
                    ),
                )
                job = self._insert_job_tx(
                    conn,
                    source=EXECUTION_SOURCE_AUTO,
                    owner_user_id=None,
                    game_id=str(bot_a["game_id"]),
                    match_type=TYPE_LADDER,
                    bot_a_id=int(bot_a["bot_id"]),
                    bot_b_id=int(bot_b["bot_id"]),
                    bot_a_version_id=int(bot_a["version_id"]),
                    bot_b_version_id=int(bot_b["version_id"]),
                    match_config={"_auto_selection_reason": reason},
                    human_user_id=None,
                    human_seat=None,
                    contest_id=None,
                    contest_pairing_id=None,
                    auto_decision_id=int(decision.lastrowid),
                    created_at=now,
                )
                conn.execute(
                    "UPDATE auto_match_decisions SET job_public_id=? WHERE id=?",
                    (job["public_id"], int(decision.lastrowid)),
                )
                conn.execute(
                    "UPDATE auto_match_fair_state SET next_game_idx=?,next_lane=?,"
                    "revision=revision+1,updated_at=? WHERE singleton=1",
                    (
                        (selected_game_idx + 1) % len(games),
                        1 - int(state["next_lane"] or 0),
                        now,
                    ),
                )
                queued_count += 1
                inserted += 1
            return {
                "outcome": "ok",
                "inserted": inserted,
                "queued": queued_count,
                "remaining_eligible": len(
                    self.store._auto_queue_candidates_tx(conn)
                ),
            }


__all__ = [
    "EXECUTION_ENV_HUMAN",
    "EXECUTION_ENV_PLATFORM_HIGH",
    "EXECUTION_ENV_PLATFORM_LOW",
    "EXECUTION_ENV_REMOTE_LOCAL",
    "EXECUTION_PROFILE_VERSION",
    "ExecutionInvariantError",
    "ExecutionMaintenanceConflict",
    "ExecutionQueueClosed",
    "ExecutionRepository",
    "SOURCE_PRIORITY",
]
