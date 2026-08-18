"""Test-only adapters for the durable enqueue -> claim -> start contract."""
from __future__ import annotations

from typing import Any

from bzplat.backend.store import rating_projection_digests


class _LocalCleanupProbe:
    """Give legacy fake MatchRunner objects the new cleanup surface."""

    supervisor = None

    async def cleanup_execution(self, scope) -> None:
        scope.mark_cleanup_confirmed()

    async def cleanup_instance(self) -> None:
        return None

    async def ensure_runtime_ready(self) -> None:
        return None


def verify_rating_projection(store) -> None:
    """Mark the current test fixture projection as verified after seeding bots."""
    with store._tx() as conn:
        live = rating_projection_digests(conn)
        assert live["issues"] == []
        conn.execute(
            "UPDATE rating_projection_state SET policy_version='owner-ranked-bot-v4',"
            "rebuilt_at='test',source_settlement_count=?,"
            "source_last_settled_order=?,source_digest=?,projection_digest=?,"
            "plan_digest=?,trusted_mutation_revision=mutation_revision "
            "WHERE singleton=1",
            (
                live["source_settlement_count"],
                live["source_last_settled_order"],
                live["source_digest"],
                live["projection_digest"],
                live["plan_digest"],
            ),
        )


def enable_execution_queue(store) -> None:
    store.executions.resume()
    verify_rating_projection(store)


def ensure_cleanup_surface(orch) -> None:
    if not hasattr(orch.runner, "runner"):
        setattr(orch.runner, "runner", _LocalCleanupProbe())


def claim_request(
    orch,
    request_id: str,
    *,
    start: bool = True,
    max_match_slots: int = 64,
    max_sandbox_units: int = 128,
) -> dict[str, Any]:
    """Claim exactly the named request and optionally start its prepared task."""
    verify_rating_projection(orch.store)
    job = orch.store.executions.claim_next(
        max_match_slots=max_match_slots,
        max_sandbox_units=max_sandbox_units,
        aging_seconds=60,
        user_active_limit=64,
        contest_share_slots=64,
    )
    assert job is not None, f"execution request was not claimable: {request_id}"
    assert job["public_id"] == request_id
    if start:
        ensure_cleanup_surface(orch)
        orch.start_execution_job(job)
    else:
        pending = getattr(orch, "_test_claimed_execution_jobs", None)
        if pending is None:
            pending = {}
            setattr(orch, "_test_claimed_execution_jobs", pending)
        pending[str(job["current_match_id"])] = job
    return job


def queued_execution_jobs(orch) -> list[dict[str, Any]]:
    """Return the durable queue order used by test-side dispatcher steps."""
    snapshot = orch.store.executions.snapshot(
        max_match_slots=64,
        max_sandbox_units=128,
        aging_seconds=60,
    )
    return list(snapshot["queued"])


def claim_next_queued(orch, *, start: bool = True) -> dict[str, Any]:
    """Claim the next queued request through the production atomic claim."""
    queued = queued_execution_jobs(orch)
    assert queued, "expected one queued execution request"
    return claim_request(orch, str(queued[0]["public_id"]), start=start)


def start_claimed_match(orch, match_id: str) -> None:
    pending = getattr(orch, "_test_claimed_execution_jobs", {})
    job = pending.pop(match_id)
    ensure_cleanup_surface(orch)
    orch.start_execution_job(job)


async def challenge_and_start(
    orch,
    *args,
    defer_start: bool = False,
    **kwargs,
) -> str:
    enable_execution_queue(orch.store)
    request_id = await orch.challenge(*args, **kwargs)
    job = claim_request(orch, request_id, start=not defer_start)
    return str(job["current_match_id"])


async def human_and_start(orch, *args, defer_start: bool = False, **kwargs) -> str:
    enable_execution_queue(orch.store)
    request_id = await orch.challenge_human(*args, **kwargs)
    job = claim_request(orch, request_id, start=not defer_start)
    return str(job["current_match_id"])
