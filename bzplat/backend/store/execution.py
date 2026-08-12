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
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from bzplat.backend.runtime.binary_integrity import (
    BinaryIntegrityCacheKey,
    require_binary_file_integrity,
)

from .db import _all_game_ids, _matches_table, _now, _registered_game_id, _row
from .schema import (
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

if TYPE_CHECKING:
    from .db import Store


SOURCE_PRIORITY = {
    EXECUTION_SOURCE_MANUAL: 40,
    EXECUTION_SOURCE_HUMAN: 40,
    EXECUTION_SOURCE_CONTEST: 30,
    EXECUTION_SOURCE_AUTO: 10,
}


class ExecutionQueueClosed(ValueError):
    """The process is starting/stopping and must not accept a new request."""


class ExecutionInvariantError(RuntimeError):
    """Persisted execution state violates a fail-closed invariant."""


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

    def __init__(self, store: Store) -> None:
        self.store = store
        # Claim/refill run inside one SQLite write transaction.  Re-hashing up
        # to 50 MiB per candidate on every dispatcher tick would extend that
        # lock by seconds or gigabytes of I/O.  The helper's cache identity
        # includes device/inode/size/mtime/ctime, so replacement and in-place
        # tampering still force a fresh digest while stable immutable versions
        # remain a bounded O(1) check.
        self._binary_integrity_cache: set[BinaryIntegrityCacheKey] = set()

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
                "SELECT dispatcher_state FROM execution_control WHERE singleton=1"
            ).fetchone()
            if control is None or control["dispatcher_state"] != "running":
                raise DockerLaunchInvariantError(
                    "dispatcher 未运行，禁止创建 Docker 容器"
                )
            if launch["state"] != "idle":
                raise DockerLaunchInvariantError(
                    "前一 Docker launch 尚未收敛"
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

    def pause(self, reason: str, *, bounded_retry: bool = True) -> dict:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute(
                "SELECT retry_count,dispatcher_state,accepting,pause_reason "
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
            accepting = 1
            if current and (
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
        return self.set_control(
            dispatcher_state="running",
            accepting=True,
            pause_reason="",
            retry_count=0,
            retry_at=None,
        )

    def get_auto_enabled(self) -> bool:
        return bool(int(self.control()["auto_enabled"]))

    def set_auto_enabled(self, enabled: bool) -> bool:
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "UPDATE execution_control SET auto_enabled=?,updated_at=? "
                "WHERE singleton=1",
                (1 if enabled else 0, _now()),
            )
        return bool(enabled)

    # ------------------------------------------------------------------
    # Enqueue and read models
    # ------------------------------------------------------------------
    @staticmethod
    def _rating_policy_tx(
        conn: sqlite3.Connection,
        *,
        source: str,
        bot_a_id: int,
        bot_b_id: int,
    ) -> tuple[bool, str]:
        if source == EXECUTION_SOURCE_CONTEST:
            return False, "contest"
        if source == EXECUTION_SOURCE_HUMAN:
            return False, "human"
        if bot_a_id == bot_b_id:
            return False, "self_play"
        rows = conn.execute(
            "SELECT id,owner_id FROM bots WHERE id IN (?,?)",
            (bot_a_id, bot_b_id),
        ).fetchall()
        owner_by_bot = {int(row["id"]): int(row["owner_id"]) for row in rows}
        if len(owner_by_bot) != 2:
            return False, "bot_missing"
        if owner_by_bot[bot_a_id] == owner_by_bot[bot_b_id]:
            return False, "same_owner"
        return True, "eligible"

    def _version_identity_tx(
        self,
        conn: sqlite3.Connection,
        *,
        bot_id: int,
        version_id: int | None,
    ) -> bool:
        bot = conn.execute(
            "SELECT id,is_active,game_id,current_version,binary_path,runtime_mode,"
            "format,os,arch "
            "FROM bots WHERE id=?",
            (bot_id,),
        ).fetchone()
        if (
            bot is None
            or int(bot["is_active"] or 0) != 1
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
                "size_bytes FROM bot_versions WHERE id=?",
                (version_id,),
            ).fetchone()
            if not (
                version is not None
                and int(version["bot_id"]) == bot_id
                and str(version["binary_path"] or "").strip()
                and str(version["runtime_mode"] or "") in VALID_RUNTIME_MODES
                and str(version["format"] or "") == SUPPORTED_BINARY_FORMAT
                and str(version["os"] or "") == SUPPORTED_BINARY_OS
                and str(version["arch"] or "") == SUPPORTED_BINARY_ARCH
            ):
                return False
            runtime = dict(version)
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
        rated, rating_reason = self._rating_policy_tx(
            conn,
            source=source,
            bot_a_id=int(bot_a_id),
            bot_b_id=int(bot_b_id),
        )
        public = public_id or _new_public_id()
        now = created_at or _now()
        units = 1 if source == EXECUTION_SOURCE_HUMAN else 2
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
            "public_id,source,status,priority,owner_user_id,game_id,match_type,"
            "bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id,human_user_id,"
            "human_seat,contest_id,contest_pairing_id,match_config,rated,"
            "rating_reason,match_slots,sandbox_units,auto_decision_id,created_at) "
            "VALUES(?,?,'queued',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?,?)",
            (
                public,
                source,
                priority,
                owner_user_id,
                gid,
                match_type,
                bot_a_id,
                bot_b_id,
                bot_a_version_id,
                bot_b_version_id,
                human_user_id,
                human_seat,
                contest_id,
                contest_pairing_id,
                json.dumps(config, ensure_ascii=False, separators=(",", ":")),
                1 if rated else 0,
                rating_reason,
                units,
                auto_decision_id,
                now,
            ),
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
                "SELECT accepting FROM execution_control WHERE singleton=1"
            ).fetchone()
            if control is None or int(control["accepting"] or 0) != 1:
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
            return self._insert_job_tx(
                conn,
                source=source,
                owner_user_id=owner_user_id,
                game_id=game_id,
                match_type=match_type,
                bot_a_id=bot_a_id,
                bot_b_id=bot_b_id,
                bot_a_version_id=bot_a_version_id,
                bot_b_version_id=bot_b_version_id,
                match_config=match_config,
                human_user_id=human_user_id,
                human_seat=human_seat,
                contest_id=contest_id,
                contest_pairing_id=contest_pairing_id,
                auto_decision_id=auto_decision_id,
                public_id=public_id,
                idempotency_fingerprint=idempotency_fingerprint,
            )

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
        # Aging is deliberately unbounded.  A finite priority offset means an
        # old automatic job eventually outranks every request arriving after it,
        # even under a permanently saturated foreground stream.
        bonus = int(age // max(1, aging_seconds))
        return int(row.get("priority") or 0) + bonus

    def _ordered_queued_tx(
        self,
        conn: sqlite3.Connection,
        *,
        aging_seconds: int,
        include_held_auto: bool = False,
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
            if include_held_auto
            or auto_enabled
            or row["source"] != EXECUTION_SOURCE_AUTO
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
    ) -> dict:
        marks = "'starting','running','settling'"
        used = conn.execute(
            "SELECT COUNT(*) AS jobs,COALESCE(SUM(match_slots),0) AS slots,"
            "COALESCE(SUM(sandbox_units),0) AS units FROM execution_jobs "
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
        return {
            "used_jobs": int(used["jobs"] or 0),
            "used_match_slots": int(used["slots"] or 0),
            # A legacy/untracked running match has no durable resource vector.
            # Charge the conservative Bot-vs-Bot maximum so such a live row can
            # never make the global sandbox-unit limit under-report capacity.
            "used_sandbox_units": int(used["units"] or 0)
            + untracked_running * 2,
            "running_matches": running_matches,
            "untracked_running_matches": untracked_running,
            "occupied_match_slots": occupied_match_slots,
            "max_match_slots": max(1, int(max_match_slots)),
            "max_sandbox_units": max(1, int(max_sandbox_units)),
        }

    def snapshot(
        self,
        *,
        max_match_slots: int,
        max_sandbox_units: int,
        aging_seconds: int,
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
            )
            ordered = self._ordered_queued_tx(
                conn,
                aging_seconds=aging_seconds,
                include_held_auto=True,
            )
            dispatchable = self._ordered_queued_tx(
                conn, aging_seconds=aging_seconds
            )
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
            return {
                "control": control,
                "capacity": capacity,
                "active": active,
                "queued": ordered,
                "target": dict(target) if target is not None else None,
                "ahead_jobs": ahead_jobs,
                "ahead_sandbox_units": ahead_units,
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
        if job.get("bot_a_version_id") is not None:
            config["_bot_a_version_id"] = int(job["bot_a_version_id"])
        if job.get("bot_b_version_id") is not None:
            config["_bot_b_version_id"] = int(job["bot_b_version_id"])
        now = _now()
        conn.execute(
            f"INSERT INTO {table}(id,bot_a_id,bot_b_id,owner_id,contest_id,"
            "reason,match_type,status,game_id,match_config,human_user_id,human_seat,"
            "match_seed,created_at) VALUES(?,?,?,?,?,'',?,'pending',?,?,?,?,?,?)",
            (
                match_id,
                bot_a_id,
                bot_b_id,
                job.get("owner_user_id"),
                job.get("contest_id"),
                job["match_type"],
                gid,
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
            "match_id,game_id,bot_a_id,bot_b_id,rated,rating_reason,source,classified_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                match_id,
                gid,
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
            capacity = self._capacity_tx(
                conn,
                max_match_slots=max_match_slots,
                max_sandbox_units=max_sandbox_units,
            )
            if (
                capacity["occupied_match_slots"] >= capacity["max_match_slots"]
                or capacity["running_matches"] >= capacity["max_match_slots"]
            ):
                return None
            queued = self._ordered_queued_tx(
                conn, aging_seconds=aging_seconds
            )
            non_contest_waiting = any(
                row["source"] != EXECUTION_SOURCE_CONTEST for row in queued
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
            # First preserve the configured contest share while any runnable
            # foreground/automatic work exists.  If that pass finds no runnable
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
                    if (
                        int(job["sandbox_units"])
                        + capacity["used_sandbox_units"]
                        > capacity["max_sandbox_units"]
                    ):
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
                    seats = (0, 1)
                    if job["source"] == EXECUTION_SOURCE_HUMAN:
                        seats = (1 - int(job["human_seat"]),)
                    invalid_versions: list[tuple[int, int | None]] = []
                    for seat in seats:
                        bot_id = int(
                            job[f"bot_{'a' if seat == 0 else 'b'}_id"]
                        )
                        frozen = job.get(
                            f"bot_{'a' if seat == 0 else 'b'}_version_id"
                        )
                        version_id = int(frozen) if frozen is not None else None
                        if not self._version_identity_tx(
                            conn,
                            bot_id=bot_id,
                            version_id=version_id,
                        ):
                            invalid_versions.append((bot_id, version_id))
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
                            "SELECT p.*,c.status AS contest_status,c.starts_at "
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
                        )
                        if (
                            pairing is None
                            or pairing["status"] != STATUS_PENDING
                            or pairing["match_id"] is not None
                            or pairing["contest_status"] not in ("published", "running")
                            or not identity_unchanged
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
    def assert_active_attempt(self, public_id: str, attempt_no: int) -> None:
        with self.store._tx() as conn:
            row = conn.execute(
                "SELECT status,attempt_count FROM execution_jobs WHERE public_id=?",
                (public_id,),
            ).fetchone()
            if (
                row is None
                or row["status"] not in EXECUTION_ACTIVE_STATES
                or int(row["attempt_count"] or 0) != int(attempt_no)
            ):
                raise ExecutionInvariantError("execution attempt is no longer current")

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
            conn.execute(
                "UPDATE execution_job_attempts SET status='interrupted',"
                "terminal_at=?,terminal_reason=? WHERE job_id=? AND match_id=?",
                (now, str(reason)[:200], int(job["id"]), match_id),
            )
            failure_count = int(job.get("failure_count") or 0) + 1
            if job["source"] in {
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
                next_attempt_at = (
                    datetime.now() + timedelta(seconds=delay)
                ).isoformat(timespec="seconds")
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
                conn.execute(
                    "UPDATE auto_match_decisions SET lifecycle='queued',match_id=NULL,"
                    "dispatched_at=NULL,last_attempt_error=? WHERE id=?",
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
                        next_attempt_at = (
                            datetime.now() + timedelta(seconds=delay)
                        ).isoformat(timespec="seconds")
                if match["status"] in (STATUS_COMPLETED, STATUS_ABORTED):
                    conn.execute(
                        "UPDATE execution_jobs SET status='settling',settling_at=?,"
                        "cleanup_state='confirmed' WHERE id=?",
                        (_now(), int(job["id"])),
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
        with self.store._tx() as conn:
            conn.execute("BEGIN IMMEDIATE")
            control = conn.execute(
                "SELECT auto_enabled FROM execution_control WHERE singleton=1"
            ).fetchone()
            if not control or int(control["auto_enabled"] or 0) != 1:
                return {"outcome": "disabled", "inserted": 0}
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
            while queued_count < max(0, int(target_queued)) and games:
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
                    + f" · owner/Bot 轮转 · Bot交手 {bot_pair} · "
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
                        "owner-game-lane-v4-bootstrap",
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
    "ExecutionInvariantError",
    "ExecutionQueueClosed",
    "ExecutionRepository",
    "SOURCE_PRIORITY",
]
