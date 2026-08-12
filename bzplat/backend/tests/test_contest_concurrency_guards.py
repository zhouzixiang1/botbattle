"""赛事生命周期、报名并发与普通端点权限边界回归。"""
from __future__ import annotations

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    EXECUTION_SOURCE_CONTEST,
    TYPE_CONTEST,
    XP_CONTEST_PARTICIPATE,
)
from bzplat.backend.tests.execution_helpers import (
    claim_next_queued,
    enable_execution_queue,
)


class _CountingOrch:
    def __init__(self) -> None:
        self.calls = 0

    async def challenge(self, *args, **kwargs):
        self.calls += 1
        return f"counting-{self.calls}"


class _BlockingMatchOrch:
    """challenge 建 running match 后等待，模拟仍在执行的 runner。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.calls = 0

    async def challenge(self, bot_a_id, bot_b_id, owner_user_id, **kwargs):
        self.calls += 1
        match_id = f"blocking-{self.calls}"
        self.store.create_match(
            match_id,
            bot_a_id=bot_a_id,
            bot_b_id=bot_b_id,
            owner_id=owner_user_id,
            contest_id=kwargs.get("contest_id"),
            match_type=kwargs.get("match_type", "contest"),
            game_id=kwargs.get("game_id", "holdem"),
            match_config={},
        )
        self.store.update_match(match_id, status="running")
        self.entered.set()
        await self.release.wait()
        return match_id


def _manager_fixture(tmp_path, *, status: str = "draft"):
    store = Store(str(tmp_path / "contest-guards.db"))
    organizer = store.create_user("guard-org", "guard-org@example.com", "hash")
    users = [
        store.create_user(f"guard-u{i}", f"guard-u{i}@example.com", "hash")
        for i in range(2)
    ]
    bot_paths = [tmp_path / f"guard-{i}" for i in range(len(users))]
    for path in bot_paths:
        path.write_bytes(b"test fixture")
    bots = [
        store.create_bot(
            user["id"], f"guard-bot-{i}", binary_path=str(bot_paths[i]),
            format="elf", game_id="holdem",
        )
        for i, user in enumerate(users)
    ]
    contest = store.create_contest(
        "Guarded",
        organizer["id"],
        status=status,
        game_id="holdem",
        template_id="holdem_rr",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    return store, contest["id"], users, bots


def test_open_is_idempotent_but_cannot_reopen_later_state(tmp_path):
    async def exercise():
        store, contest_id, _, _ = _manager_fixture(tmp_path)
        manager = ContestManager(store, _CountingOrch())

        first = await manager.open_registration(contest_id)
        second = await manager.open_registration(contest_id)
        assert first["status"] == second["status"] == "open"
        assert second["registration_opens_at"] == first["registration_opens_at"]

        store.update_contest(contest_id, status="published")
        with pytest.raises(ValueError, match="不能开放报名"):
            await manager.open_registration(contest_id)
        assert store.get_contest(contest_id)["status"] == "published"

    asyncio.run(exercise())


def test_open_waits_for_publish_lock_and_rechecks_published_state(
    tmp_path, monkeypatch
):
    async def exercise():
        store, contest_id, _, _ = _manager_fixture(tmp_path)
        manager = ContestManager(store, _CountingOrch())
        entered = asyncio.Event()
        release = asyncio.Event()
        original_begin = manager._begin_stage

        async def paused_begin(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_begin(*args, **kwargs)

        monkeypatch.setattr(manager, "_begin_stage", paused_begin)
        publish_task = asyncio.create_task(manager.publish(contest_id))
        await entered.wait()
        open_task = asyncio.create_task(manager.open_registration(contest_id))
        await asyncio.sleep(0)
        assert not open_task.done(), "open 必须等待 publish 持有的同一赛事锁"

        release.set()
        assert (await publish_task)["status"] == "published"
        with pytest.raises(ValueError, match="不能开放报名"):
            await open_task
        assert store.get_contest(contest_id)["status"] == "published"

    asyncio.run(exercise())


def test_concurrent_register_unique_conflict_is_business_error(tmp_path, monkeypatch):
    store, contest_id, users, bots = _manager_fixture(tmp_path, status="open")
    # 只保留第一个用户未报名，制造两个请求同时通过 manager 层前置查重。
    store.delete_entry(contest_id, users[0]["id"])
    # 两个 manager 模拟不同 worker：per-process Lock 不共享，Store UNIQUE/事务仍须兜底。
    managers = [
        ContestManager(store, _CountingOrch()),
        ContestManager(store, _CountingOrch()),
    ]
    original_get_entry = store.get_entry
    barrier = threading.Barrier(2)

    def synchronized_empty_read(cid: int, uid: int):
        row = original_get_entry(cid, uid)
        if cid == contest_id and uid == users[0]["id"] and row is None:
            barrier.wait(timeout=5)
        return row

    monkeypatch.setattr(store, "get_entry", synchronized_empty_read)

    def register_once(manager):
        try:
            return asyncio.run(
                manager.register(contest_id, users[0]["id"], bots[0]["id"])
            )
        except Exception as exc:  # 返回异常以便同时断言类型与数量
            return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(register_once, managers))

    assert sum(isinstance(result, dict) for result in results) == 1
    errors = [result for result in results if isinstance(result, Exception)]
    assert len(errors) == 1
    assert isinstance(errors[0], ValueError)
    assert "已报名" in str(errors[0])
    assert len([e for e in store.list_entries(contest_id) if e["user_id"] == users[0]["id"]]) == 1


def test_same_manager_concurrent_register_is_serialized(tmp_path):
    """同一进程的快速双击经赛事锁串行，仍只有一次报名成功。"""
    async def exercise():
        store, contest_id, users, bots = _manager_fixture(tmp_path, status="open")
        store.delete_entry(contest_id, users[0]["id"])
        manager = ContestManager(store, _CountingOrch())
        results = await asyncio.gather(
            manager.register(contest_id, users[0]["id"], bots[0]["id"]),
            manager.register(contest_id, users[0]["id"], bots[0]["id"]),
            return_exceptions=True,
        )
        assert sum(isinstance(result, dict) for result in results) == 1
        errors = [result for result in results if isinstance(result, Exception)]
        assert len(errors) == 1 and isinstance(errors[0], ValueError)
        assert len(store.list_entries(contest_id)) == 2

    asyncio.run(exercise())


def test_register_waits_for_publish_and_cannot_create_unpaired_late_entry(
    tmp_path, monkeypatch
):
    """publish 持锁生成 2 人排期时，排队报名不得在 published 后晚插第 3 人。"""
    async def exercise():
        store, contest_id, users, _bots = _manager_fixture(tmp_path, status="open")
        late_user = store.create_user("late-user", "late-user@example.com", "hash")
        late_bot = store.create_bot(
            late_user["id"], "late-bot", binary_path="/tmp/late-bot",
            format="elf", game_id="holdem",
        )
        manager = ContestManager(store, _CountingOrch())
        entered = asyncio.Event()
        release = asyncio.Event()
        original_begin = manager._begin_stage

        async def paused_begin(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_begin(*args, **kwargs)

        monkeypatch.setattr(manager, "_begin_stage", paused_begin)
        publish_task = asyncio.create_task(manager.publish(contest_id))
        await entered.wait()
        register_task = asyncio.create_task(
            manager.register(contest_id, late_user["id"], late_bot["id"])
        )
        await asyncio.sleep(0)
        assert not register_task.done(), "报名必须等待 publish 的同一赛事锁"

        release.set()
        assert (await publish_task)["status"] == "published"
        with pytest.raises(ValueError, match="未开放报名"):
            await register_task

        entries = store.list_contest_entries(contest_id)
        assert {entry["user_id"] for entry in entries} == {
            user["id"] for user in users
        }
        pairings = store.list_contest_pairings(contest_id)
        assert len(pairings) == 1
        paired_users = {
            pairings[0]["entry_a_id"], pairings[0]["entry_b_id"]
        }
        assert paired_users == {entry["id"] for entry in entries}

    asyncio.run(exercise())


def test_roster_add_delete_wait_for_publish_and_reject_late_mutation(
    tmp_path, monkeypatch
):
    """组织者/admin 名册增删与 publish 串行，published 后不得晚插或删人。"""
    async def exercise():
        store, contest_id, users, _bots = _manager_fixture(tmp_path, status="open")
        late_user = store.create_user("roster-late", "roster-late@example.com", "hash")
        late_bot = store.create_bot(
            late_user["id"], "roster-late-bot", binary_path="/tmp/roster-late",
            format="elf", game_id="holdem",
        )
        manager = ContestManager(store, _CountingOrch())
        entered = asyncio.Event()
        release = asyncio.Event()
        original_begin = manager._begin_stage

        async def paused_begin(*args, **kwargs):
            entered.set()
            await release.wait()
            return await original_begin(*args, **kwargs)

        monkeypatch.setattr(manager, "_begin_stage", paused_begin)
        publish_task = asyncio.create_task(manager.publish(contest_id))
        await entered.wait()
        add_task = asyncio.create_task(
            manager.add_roster_entry(contest_id, late_user["id"], late_bot["id"])
        )
        delete_task = asyncio.create_task(
            manager.delete_roster_entry(contest_id, users[0]["id"])
        )
        await asyncio.sleep(0)
        assert not add_task.done() and not delete_task.done()

        release.set()
        await publish_task
        results = await asyncio.gather(add_task, delete_task, return_exceptions=True)
        assert all(isinstance(result, ValueError) for result in results)
        entries = store.list_contest_entries(contest_id)
        assert {entry["user_id"] for entry in entries} == {
            user["id"] for user in users
        }
        assert len(store.list_contest_pairings(contest_id)) == 1

    asyncio.run(exercise())


def test_duplicate_register_api_awards_xp_once(tmp_path):
    app = create_app(db_path=str(tmp_path / "contest-xp.db"), upload_root=tmp_path / "uploads")
    store = app.state.store
    user = store.create_user(
        "xp-player", "xp-player@example.com", hash_password("pw123456")
    )
    store.update_user(user["id"], email_verified=1)
    bot = store.create_bot(
        user["id"], "xp-bot", binary_path="/tmp/xp-bot", format="elf",
        game_id="holdem",
    )
    contest = store.create_contest(
        "XP once", user["id"], status="open", game_id="holdem"
    )
    _, token = app.state.auth.authenticate("xp-player", "pw123456")
    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}

    first = client.post(
        f"/api/contests/{contest['id']}/register",
        json={"bot_id": bot["id"]}, headers=headers,
    )
    second = client.post(
        f"/api/contests/{contest['id']}/register",
        json={"bot_id": bot["id"]}, headers=headers,
    )

    assert first.status_code == 200
    assert second.status_code == 400
    assert store.get_user(user["id"])["xp"] == XP_CONTEST_PARTICIPATE


def test_finish_waits_for_dispatch_then_rejects_active_match(tmp_path):
    async def exercise():
        store, contest_id, _, _ = _manager_fixture(tmp_path, status="open")
        manager = ContestManager(store, _CountingOrch())
        await manager.publish(contest_id)
        published = store.get_contest(contest_id)
        store.update_contest(
            contest_id, starts_at=published["registration_closes_at"]
        )
        blocker = _BlockingMatchOrch(store)
        manager.orch = blocker

        dispatch_task = asyncio.create_task(manager._dispatch_pending(contest_id, 0))
        await blocker.entered.wait()
        finish_task = asyncio.create_task(manager.finish(contest_id))
        await asyncio.sleep(0)
        assert not finish_task.done(), "finish 必须等待 dispatch 持有的同一赛事锁"

        blocker.release.set()
        await dispatch_task
        with pytest.raises(ValueError, match="未完成对阵"):
            await finish_task
        assert store.get_contest(contest_id)["status"] == "running"

        match_id = store.list_contest_pairings(contest_id)[0]["match_id"]
        store.update_match(
            match_id, status="completed", winner=0,
            result={"deltas": [1, -1]}, reason="completed",
        )
        assert (await manager.finish(contest_id))["status"] == "finished"

    asyncio.run(exercise())


def test_delete_waits_for_dispatch_then_rejects_running_contest(tmp_path):
    """admin delete 与 dispatch 共锁；不能删赛事后把运行 task 留成孤儿。"""
    async def exercise():
        store, contest_id, _, _ = _manager_fixture(tmp_path, status="open")
        manager = ContestManager(store, _CountingOrch())
        await manager.publish(contest_id)
        published = store.get_contest(contest_id)
        store.update_contest(
            contest_id, starts_at=published["registration_closes_at"]
        )
        blocker = _BlockingMatchOrch(store)
        manager.orch = blocker

        dispatch_task = asyncio.create_task(manager._dispatch_pending(contest_id, 0))
        await blocker.entered.wait()
        delete_task = asyncio.create_task(manager.delete(contest_id))
        await asyncio.sleep(0)
        assert not delete_task.done(), "delete 必须等待 dispatch 的同一赛事锁"

        blocker.release.set()
        await dispatch_task
        with pytest.raises(ValueError, match="不能删除"):
            await delete_task
        assert store.get_contest(contest_id)["status"] == "running"
        assert len(store.list_matches(contest_id=contest_id)) == 1

    asyncio.run(exercise())


def test_delete_published_without_matches_cancels_schedule_then_deletes(tmp_path):
    async def exercise():
        store, contest_id, _, _ = _manager_fixture(tmp_path, status="open")
        manager = ContestManager(store, _CountingOrch())
        await manager.publish(contest_id)
        assert store.get_contest(contest_id)["status"] == "published"
        assert await manager.delete(contest_id) is True
        assert store.get_contest(contest_id) is None

    asyncio.run(exercise())


def test_delete_published_rejects_queued_execution_request(tmp_path):
    async def exercise():
        store, contest_id, users, bots = _manager_fixture(
            tmp_path, status="open"
        )
        manager = ContestManager(store, _CountingOrch())
        await manager.publish(contest_id)
        pairings = store.list_contest_pairings(contest_id)
        pairing = pairings[0]
        store.executions.resume()
        request = store.executions.enqueue(
            source=EXECUTION_SOURCE_CONTEST,
            owner_user_id=users[0]["id"],
            game_id="holdem",
            match_type=TYPE_CONTEST,
            bot_a_id=bots[0]["id"],
            bot_b_id=bots[1]["id"],
            bot_a_version_id=pairing.get("bot_a_version_id"),
            bot_b_version_id=pairing.get("bot_b_version_id"),
            contest_id=contest_id,
            contest_pairing_id=pairing["id"],
        )

        with pytest.raises(ValueError, match="排队或执行中的请求"):
            await manager.delete(contest_id)

        assert store.get_contest(contest_id)["status"] == "published"
        assert store.executions.get(request["public_id"])["status"] == "queued"
        assert len(store.list_contest_pairings(contest_id)) == len(pairings)

    asyncio.run(exercise())


def test_force_finish_rejects_while_real_runner_is_blocked(tmp_path):
    """UI 的强制结束不得把仍在 runner 中的赛事固化为 finished。"""
    async def exercise():
        from types import SimpleNamespace

        from bzplat.backend.matches.orchestrator import MatchOrchestrator

        class BlockingRunner:
            def __init__(self) -> None:
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def run_binaries(self, *args, **kwargs):
                self.entered.set()
                await self.release.wait()
                return SimpleNamespace(
                    rounds_played=1,
                    rounds=[SimpleNamespace(deltas=[1, -1])],
                    winner=0,
                )

        store, contest_id, _, _ = _manager_fixture(tmp_path, status="open")
        runner = BlockingRunner()
        orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
        manager = ContestManager(store, orch)
        enable_execution_queue(store)
        await manager.publish(contest_id)
        await manager.start(contest_id)
        claim_next_queued(orch)
        await asyncio.wait_for(runner.entered.wait(), timeout=1)

        with pytest.raises(ValueError, match="未完成对阵"):
            await manager.finish(contest_id)
        assert store.get_contest(contest_id)["status"] == "running"

        runner.release.set()
        match_id = store.list_contest_pairings(contest_id)[0]["match_id"]
        task = orch._tasks.get(match_id)
        if task:
            await asyncio.wait_for(task, timeout=1)
        assert (await manager.finish(contest_id))["status"] == "finished"

    asyncio.run(exercise())


def test_safe_reconcile_dispatch_rechecks_terminal_state_after_lock(tmp_path):
    async def exercise():
        store, contest_id, _, bots = _manager_fixture(tmp_path, status="running")
        store.add_contest_pairing(
            contest_id, bots[0]["id"], bots[1]["id"], status="pending",
            stage_idx=0, stage_key="rr",
        )
        orch = _CountingOrch()
        manager = ContestManager(store, orch)
        lock = manager._lock(contest_id)
        await lock.acquire()
        try:
            dispatch_task = asyncio.create_task(manager._dispatch_pending_safe(contest_id, 0))
            await asyncio.sleep(0)
            assert not dispatch_task.done()
            store.update_contest(contest_id, status="finished")
        finally:
            lock.release()

        await dispatch_task
        assert orch.calls == 0
        assert store.list_matches(contest_id=contest_id) == []

    asyncio.run(exercise())


def test_ordinary_endpoints_never_gain_proxy_rights_from_global_role(tmp_path):
    app = create_app(db_path=str(tmp_path / "contest-authz.db"), upload_root=tmp_path / "uploads")
    store = app.state.store

    def account(name: str, role: str = "user"):
        user = store.create_user(
            name, f"{name}@example.com", hash_password("pw123456"), role=role
        )
        store.update_user(user["id"], email_verified=1)
        _, token = app.state.auth.authenticate(name, "pw123456")
        return user, {"Authorization": f"Bearer {token}"}

    org_a, headers_a = account("proxy-org-a", "organizer")
    org_b, headers_b = account("proxy-org-b", "organizer")
    admin, headers_admin = account("proxy-admin", "admin")
    victim, _ = account("proxy-victim")
    explicit_org_target, _ = account("proxy-org-target")
    explicit_admin_target, _ = account("proxy-admin-target")
    mismatched_target, _ = account("proxy-mismatched-target")
    victim_bots = [
        store.create_bot(
            victim["id"], f"victim-bot-{i}", binary_path=f"/tmp/victim-{i}",
            format="elf", game_id="holdem",
        )
        for i in range(2)
    ]
    org_target_bot = store.create_bot(
        explicit_org_target["id"], "org-target-bot", binary_path="/tmp/org-target",
        format="elf", game_id="holdem",
    )
    admin_target_bot = store.create_bot(
        explicit_admin_target["id"], "admin-target-bot", binary_path="/tmp/admin-target",
        format="elf", game_id="holdem",
    )
    contest = store.create_contest(
        "Owned by B", org_b["id"], status="open", game_id="holdem"
    )
    store.add_contest_entry(contest["id"], victim["id"], victim_bots[0]["id"])
    client = TestClient(app)

    for headers in (headers_a, headers_admin):
        register = client.post(
            f"/api/contests/{contest['id']}/register",
            json={"bot_id": victim_bots[1]["id"]}, headers=headers,
        )
        dispatch = client.post(
            f"/api/contests/{contest['id']}/dispatch",
            json={"bot_id": victim_bots[1]["id"]}, headers=headers,
        )
        assert register.status_code == 400
        assert dispatch.status_code == 400
        assert "自己的 bot" in register.json()["detail"]
        assert "自己的 bot" in dispatch.json()["detail"]
    assert store.get_entry(contest["id"], victim["id"])["bot_id"] == victim_bots[0]["id"]

    # 赛事所有者的显式代理入口仍可替目标用户加其 Bot。
    organizer_proxy = client.post(
        f"/api/contests/{contest['id']}/entries",
        json={"user_id": explicit_org_target["id"], "bot_id": org_target_bot["id"]},
        headers=headers_b,
    )
    assert organizer_proxy.status_code == 200

    # admin 显式代理入口同样保持可用；普通 /register、/dispatch 并未借 role 越权。
    admin_proxy = client.post(
        f"/api/admin/contests/{contest['id']}/entries/bulk",
        json={
            "entries": [{
                "user_id": explicit_admin_target["id"],
                "bot_id": admin_target_bot["id"],
            }]
        },
        headers=headers_admin,
    )
    assert admin_proxy.status_code == 200
    assert admin_proxy.json()["added"] == 1

    # 代理入口也必须保持 entry.user_id 与 bot.owner_id 一致，不能伪造身份绑定。
    mismatch_single = client.post(
        f"/api/contests/{contest['id']}/entries",
        json={"user_id": mismatched_target["id"], "bot_id": victim_bots[0]["id"]},
        headers=headers_b,
    )
    assert mismatch_single.status_code == 400
    assert "不属于" in mismatch_single.json()["detail"]
    mismatch_bulk = client.post(
        f"/api/admin/contests/{contest['id']}/entries/bulk",
        json={"entries": [{
            "user_id": mismatched_target["id"], "bot_id": victim_bots[0]["id"],
        }]},
        headers=headers_admin,
    )
    assert mismatch_bulk.status_code == 200
    assert mismatch_bulk.json()["added"] == 0
    assert any("不属于" in item for item in mismatch_bulk.json()["skipped"])

    # running/终态下 organizer 与 admin 的批量/单条增删全部拒绝。
    late_user, _ = account("proxy-late")
    late_bot = store.create_bot(
        late_user["id"], "proxy-late-bot", binary_path="/tmp/proxy-late",
        format="elf", game_id="holdem",
    )
    store.update_contest(contest["id"], status="running")
    blocked = [
        client.post(
            f"/api/contests/{contest['id']}/entries",
            json={"user_id": late_user["id"], "bot_id": late_bot["id"]},
            headers=headers_b,
        ),
        client.post(
            f"/api/contests/{contest['id']}/entries/bulk",
            json={"entries": [{"user_id": late_user["id"], "bot_id": late_bot["id"]}]},
            headers=headers_b,
        ),
        client.delete(
            f"/api/contests/{contest['id']}/entries/{victim['id']}",
            headers=headers_b,
        ),
        client.post(
            f"/api/admin/contests/{contest['id']}/entries/bulk",
            json={"entries": [{"user_id": late_user["id"], "bot_id": late_bot["id"]}]},
            headers=headers_admin,
        ),
        client.delete(
            f"/api/admin/contests/{contest['id']}/entries/{explicit_admin_target['id']}",
            headers=headers_admin,
        ),
    ]
    assert all(response.status_code == 400 for response in blocked)
