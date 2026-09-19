"""执行队列可观测性：入队/领起/空转原因/对局关联键的日志契约。

定位「我的比赛为何还没跑」「对局为何无声消失」此前只能查 DB：
- 入队成功、claim 成功（含等待时长）、claim 空转原因翻转各有基准事件；
- match start/done/aborted/crashed 携带 job/attempt/source 关联键。
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from bzplat.backend.matches.execution_queue import ExecutionDispatcher
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.store.db import Store
from bzplat.backend.store.schema import (
    EXECUTION_SOURCE_MANUAL,
    TYPE_CHALLENGE,
)


def _bot(store: Store, key: str) -> dict:
    user = store.create_user(
        f"user-{key}", f"{key}@example.test", "test-password-hash"
    )
    binary = Path(store.path).parent / f"obs-{key}.elf"
    binary.write_bytes(f"fixture-{key}".encode())
    bot = store.create_bot(
        int(user["id"]), f"bot-{key}", binary_path=str(binary),
        format="elf", game_id="gomoku",
    )
    version = store.add_bot_version(bot["id"], binary_path=str(binary))
    return {
        "user_id": int(user["id"]),
        "bot_id": int(bot["id"]),
        "version_id": int(version["id"]),
    }


@pytest.fixture
def obs_store(tmp_path):
    store = Store(str(tmp_path / "obs.db"))
    store.executions.resume()
    yield store
    store.close()


def test_enqueue_logs_public_id_source_and_env(obs_store, caplog):
    store = obs_store
    a, b = _bot(store, "obs-a"), _bot(store, "obs-b")
    caplog.set_level(
        logging.INFO, logger="bzplat.backend.store.execution"
    )
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=a["user_id"],
        game_id="gomoku",
        match_type=TYPE_CHALLENGE,
        bot_a_id=a["bot_id"],
        bot_b_id=b["bot_id"],
        bot_a_version_id=a["version_id"],
        bot_b_version_id=b["version_id"],
    )
    lines = [
        r.getMessage() for r in caplog.records
        if "execution enqueued" in r.getMessage()
    ]
    assert len(lines) == 1
    assert f"public_id={job['public_id']}" in lines[0]
    assert "source=manual" in lines[0]
    assert "env=platform_low/platform_low" in lines[0]


def test_claim_denial_reason_is_exposed(obs_store):
    store = obs_store
    a, b = _bot(store, "den-a"), _bot(store, "den-b")
    store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=a["user_id"],
        game_id="gomoku",
        match_type=TYPE_CHALLENGE,
        bot_a_id=a["bot_id"],
        bot_b_id=b["bot_id"],
        bot_a_version_id=a["version_id"],
        bot_b_version_id=b["version_id"],
    )
    # 先领起唯一一单占满 1 个 slot（starting 态持续占用容量）。
    first = store.executions.claim_next(
        max_match_slots=2,
        max_sandbox_units=4,
        aging_seconds=60,
        user_active_limit=1,
        contest_share_slots=1,
    )
    assert first is not None
    assert store.executions.last_claim_denial is None

    # 槽位占满后再 claim：容量闸拒绝并暴露原因。
    second = store.executions.claim_next(
        max_match_slots=1,
        max_sandbox_units=4,
        aging_seconds=60,
        user_active_limit=1,
        contest_share_slots=1,
    )
    assert second is None
    assert store.executions.last_claim_denial == "match_slots_full"


def test_dispatcher_denial_memo_is_per_claim_class(caplog):
    """foreground 与 auto 每 tick 各判一次空转：memo 必须按类分槽。
    v1.7 上线实证的回归——共用单槽时两类互相顶掉，每秒双行刷屏。"""
    caplog.set_level(
        logging.INFO, logger="bzplat.backend.matches.execution_queue"
    )
    fake = SimpleNamespace(
        repo=SimpleNamespace(last_claim_denial=None),
        _last_claim_denial={},
        _claim_wait_seconds=ExecutionDispatcher._claim_wait_seconds,
    )
    for _ in range(3):
        fake.repo.last_claim_denial = "no_eligible_job"
        ExecutionDispatcher._note_claim_denial(fake, "foreground")
        fake.repo.last_claim_denial = "auto_gate"
        ExecutionDispatcher._note_claim_denial(fake, "auto")
    lines = [
        r.getMessage() for r in caplog.records
        if "execution claim idle" in r.getMessage()
    ]
    assert lines == [
        "execution claim idle class=foreground reason=no_eligible_job",
        "execution claim idle class=auto reason=auto_gate",
    ]
    # 该类拿到一单后只复位自己的槽。
    ExecutionDispatcher._log_claim(
        fake, {"public_id": "req_x", "source": "manual",
               "current_match_id": "m", "attempt_count": 1,
               "created_at": "2026-09-19T16:00:00",
               "claimed_at": "2026-09-19T16:00:05"},
        "foreground",
    )
    fake.repo.last_claim_denial = "no_eligible_job"
    ExecutionDispatcher._note_claim_denial(fake, "foreground")
    fake.repo.last_claim_denial = "auto_gate"
    ExecutionDispatcher._note_claim_denial(fake, "auto")
    lines = [
        r.getMessage() for r in caplog.records
        if "execution claim idle" in r.getMessage()
    ]
    assert len(lines) == 3  # 只有 foreground 复位后重记，auto 仍被抑制
    assert lines[-1].startswith("execution claim idle class=foreground")


def test_dispatcher_denial_memo_logs_on_change_only(obs_store, caplog):
    caplog.set_level(
        logging.INFO, logger="bzplat.backend.matches.execution_queue"
    )
    fake = SimpleNamespace(
        repo=SimpleNamespace(last_claim_denial="match_slots_full"),
        _last_claim_denial={},
    )
    ExecutionDispatcher._note_claim_denial(fake, "foreground")
    ExecutionDispatcher._note_claim_denial(fake, "foreground")
    lines = [
        r.getMessage() for r in caplog.records
        if "execution claim idle" in r.getMessage()
    ]
    assert lines == [
        "execution claim idle class=foreground reason=match_slots_full"
    ]
    # 原因变化后重记一次。
    fake.repo.last_claim_denial = "no_eligible_job"
    ExecutionDispatcher._note_claim_denial(fake, "foreground")
    lines = [
        r.getMessage() for r in caplog.records
        if "execution claim idle" in r.getMessage()
    ]
    assert len(lines) == 2


def test_claim_wait_seconds_parses_iso_timestamps():
    job = {
        "created_at": "2026-09-19T15:00:00",
        "claimed_at": "2026-09-19T15:00:37",
    }
    assert ExecutionDispatcher._claim_wait_seconds(job) == 37
    assert ExecutionDispatcher._claim_wait_seconds({}) == -1


def test_match_lifecycle_logs_carry_job_and_attempt(tmp_path, caplog):
    """真实 orchestrator 对局：start/done 都携带 job/attempt/source 关联键。"""
    from types import SimpleNamespace

    from bzplat.backend.tests.test_audit_coverage import (
        _fixture_binary,
        _user_with_bot,
        challenge_and_start,
    )

    class SuccessRunner:
        async def run_binaries(self, *args, **kwargs):
            return SimpleNamespace(
                rounds_played=1,
                rounds=[SimpleNamespace(deltas=[1, -1])],
                winner=0,
            )

    store = Store(str(tmp_path / "obs-match.db"))
    owner, bot = _user_with_bot(
        store, name="obsloga", path=_fixture_binary(store, "obs-log")
    )
    orch = MatchOrchestrator(store, runner=SuccessRunner(), max_concurrent=1)
    caplog.set_level(
        logging.INFO, logger="bzplat.backend.matches.orchestrator"
    )

    async def run():
        mid = await challenge_and_start(
            orch, bot["id"], bot["id"], owner["id"], game_id="gomoku"
        )
        await asyncio.wait_for(orch._tasks[mid], timeout=2)
        return mid

    mid = asyncio.run(run())
    started = [
        r.getMessage() for r in caplog.records
        if f"match start id={mid}" in r.getMessage()
    ]
    done = [
        r.getMessage() for r in caplog.records
        if f"match done id={mid}" in r.getMessage()
    ]
    assert len(started) == 1 and len(done) == 1
    for line in (*started, *done):
        assert "job=req_" in line
        assert "attempt=1" in line
        assert "source=manual" in line
    store.close()
