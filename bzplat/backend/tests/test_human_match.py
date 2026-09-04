"""人类 vs bot 对战测试（challenge_human / 回合 Future / 超时 / 不计分 / 独立并发）。"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading

import pytest

from bzplat.backend.auth.auth_manager import COOKIE_NAME
from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.orchestrator import (
    HumanWebSocketLimitError,
    MatchOrchestrator,
)
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner, BotCrashedError
from bzplat.backend.runtime.config import (
    HUMAN_ACTION_MAX_BYTES,
    HUMAN_ACTION_RATE_BURST,
)
from bzplat.backend.store import Store
from bzplat.backend.store.schema import TYPE_HUMAN
from bzplat.backend.tests.execution_helpers import (
    challenge_and_start,
    claim_request,
    enable_execution_queue,
    human_and_start,
    start_claimed_match,
)
from bzplat.backend.tests._gomoku_v2 import standard_response


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "h.db"))


def _setup(store: Store, game: str = "gomoku"):
    """建用户 + 一个本地 ELF bot（prefer_local，无需 Docker）。"""
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    u = store.create_user("usr1", "u@ex.com", hash_password("password1"))
    path = os.path.abspath("samples/gomokubot_linux_amd64")
    b = store.create_bot(
        u["id"], "mybot", binary_path=path, format="elf", game_id=game
    )
    store.ensure_rating(b["id"])
    return u, b


def _orch(store: Store, *, human_timeout: float = 120.0) -> MatchOrchestrator:
    o = MatchOrchestrator(
        store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=2
    )
    o.set_human_action_timeout(human_timeout)
    return o


def test_challenge_human_creates_match(store: Store):
    u, b = _setup(store)
    orch = _orch(store)

    async def run():
        mid = await human_and_start(
            orch,
            b["id"], u["id"], human_seat=0, game_id="gomoku"
        )
        # challenge_human intentionally starts a background match task.  Drain it
        # while this loop is alive instead of leaving asyncio.run() to cancel a
        # subprocess during loop teardown (which can hang in pipe connection).
        await orch.shutdown()
        return mid

    mid = asyncio.run(run())
    m = store.get_match(mid)
    assert m["match_type"] == TYPE_HUMAN
    assert m["human_user_id"] == u["id"]
    assert m["human_seat"] == 0


def test_human_match_freezes_current_bot_version_before_task_runs(
    store: Store, tmp_path
):
    """人类局实际 bot 座位也冻结 current；任务排队后切版本不改变路径/模式。"""
    from types import SimpleNamespace

    base_path = tmp_path / "human-base"
    v1_path = tmp_path / "human-v1"
    v2_path = tmp_path / "human-v2"
    for path in (base_path, v1_path, v2_path):
        path.write_bytes(b"test fixture")
    u = store.create_user("human-pin", "human-pin@e.com", hash_password("password1"))
    bot = store.create_bot(
        u["id"], "human-pin-bot", binary_path=str(base_path),
        format="elf", game_id="gomoku",
    )
    v1 = store.add_bot_version(
        bot["id"], binary_path=str(v1_path), version=1,
        runtime_mode="traditional",
    )
    store.add_bot_version(
        bot["id"], binary_path=str(v2_path), version=2,
        runtime_mode="longrunning",
    )
    store.set_current_version(bot["id"], 1)
    captured: dict = {}

    class CapturingRunner:
        async def run_bot_vs_human(
            self, bot_path, *, bot_seat, runtime_mode=None, **_kwargs
        ):
            captured.update(
                path=bot_path, bot_seat=bot_seat, runtime_mode=runtime_mode
            )
            return SimpleNamespace(
                rounds_played=1,
                rounds=[SimpleNamespace(deltas=[0, 0])],
                winner=None,
                events=[],
            )

    async def exercise():
        orch = MatchOrchestrator(store, runner=CapturingRunner(), max_concurrent=1)
        # 人类坐座位 0，因此实际 Bot 在座位 1，快照键必须对应 bot_b。
        mid = await human_and_start(
            orch,
            bot["id"],
            u["id"],
            human_seat=0,
            game_id="gomoku",
            defer_start=True,
        )
        match = store.get_match(mid)
        assert match["match_config"]["_bot_b_version_id"] == v1["id"]
        assert match["match_config"]["_execution_request_id"].startswith("req_")

        # claim 已冻结 v1，但 task 尚未启动；先切到 v2 再执行。
        store.set_current_version(bot["id"], 2)
        start_claimed_match(orch, mid)
        task = orch._tasks[mid]
        await task
        return mid

    mid = asyncio.run(exercise())
    assert store.get_match(mid)["status"] == "completed"
    assert captured == {
        "path": str(v1_path),
        "bot_seat": 1,
        "runtime_mode": "traditional",
    }


def test_pencil_human_match_passes_frozen_time_control_to_runner(store: Store):
    """Human match freezes the stable id and passes it to the Bot-only clock."""
    from types import SimpleNamespace

    user, bot = _setup(store, game="pencil")
    captured: dict = {}

    class CapturingRunner:
        async def run_bot_vs_human(
            self, _bot_path, *, time_control_id=None, **_kwargs
        ):
            captured["time_control_id"] = time_control_id
            return SimpleNamespace(
                rounds_played=1,
                rounds=[SimpleNamespace(deltas=[0, 0])],
                winner=None,
                events=[],
            )

    async def exercise():
        orch = MatchOrchestrator(store, runner=CapturingRunner(), max_concurrent=1)
        match_id = await human_and_start(
            orch,
            bot["id"], user["id"], human_seat=1, game_id="pencil"
        )
        task = orch._tasks.get(match_id)
        if task is not None:
            await task
        return match_id

    match_id = asyncio.run(exercise())
    assert store.get_match(match_id)["status"] == "completed"
    assert captured == {"time_control_id": "pencil_per_side_total_900s_v1"}


def test_human_claim_replay_failure_rolls_back_atomically(store: Store):
    """Claim 内 replay 初始化失败时，job 保持 queued 且不残留 match。"""
    u, b = _setup(store)
    orch = _orch(store)
    enable_execution_queue(store)
    request_id = asyncio.run(
        orch.challenge_human(
            b["id"], u["id"], human_seat=0, game_id="gomoku"
        )
    )
    with store._tx() as conn:
        conn.execute(
            "CREATE TRIGGER fail_human_replay BEFORE INSERT ON match_replays "
            "BEGIN SELECT RAISE(ABORT, 'replay write failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="replay write failed"):
        claim_request(orch, request_id, start=False)

    assert store.list_matches() == []
    assert orch._tasks == {}
    assert u["id"] not in orch._human_active_users
    assert store.executions.get(request_id)["status"] == "queued"
    with store._tx() as c:
        assert c.execute("SELECT COUNT(*) FROM matches_index").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM match_replays").fetchone()[0] == 0


def test_duplicate_seed_is_frozen_by_atomic_claim(store: Store):
    """duplicate seed 与 match/replay/index 在 claim 的同一事务中持久化。"""
    u, b = _setup(store, game="holdem")
    orch = _orch(store)
    match_id = asyncio.run(
        challenge_and_start(
            orch,
            b["id"],
            b["id"],
            u["id"],
            game_id="holdem",
            duplicate=True,
            duplicate_seed=42,
            defer_start=True,
        )
    )
    match = store.get_match(match_id)
    assert match["match_seed"] == 42
    assert match["match_config"]["duplicate"] is True
    assert match["match_config"]["duplicate_seed"] == 42
    assert json.loads(store.get_replay(match_id)["events_json"]) == []
    with store._tx() as c:
        assert c.execute(
            "SELECT game_id FROM matches_index WHERE id=?", (match_id,)
        ).fetchone()[0] == "holdem"


def test_human_turn_registry_resolve_and_no_rating(store: Store):
    """challenge_human 走 orchestrator 自带 human_decide：
    它注册 Future + 广播 your_turn；外部（WS）通过 resolve_human_turn 解析。
    完成后人类对战不计 Glicko（bot 的 matches_played 不变）。
    """
    u, b = _setup(store)
    orch = _orch(store)
    done = asyncio.Event()

    async def solver():
        """模拟 WS：按裁判给出的阶段提交 canonical v2 动作。"""
        for _ in range(500):
            for (mid, pidx), entry in list(orch._human_turns.items()):
                if mid == mid_ref["id"] and not entry["future"].done():
                    request = entry["request"]
                    orch.resolve_human_turn(
                        mid,
                        pidx,
                        standard_response(request),
                    )
            if done.is_set():
                return
            await asyncio.sleep(0.05)

    mid_ref: dict = {}

    async def run():
        mid = await human_and_start(
            orch, b["id"], u["id"], human_seat=1, game_id="gomoku"
        )
        mid_ref["id"] = mid
        await asyncio.gather(
            solver(),
            _wait_done(store, mid, done),
        )
        return store.get_match(mid)

    mm = asyncio.run(run())
    assert mm["status"] == "completed"
    assert mm["reason"] in {"five", "double_pass", "board_full"}
    assert mm["result"]["rounds_played"] >= 5
    replay = store.get_replay(mm["id"])
    replay_events = json.loads(replay["events_json"])
    assert not [ev for ev in replay_events if ev.get("type") == "illegal"]
    terminals = [ev for ev in replay_events if ev.get("type") == "match_end"]
    assert len(terminals) == 1
    assert terminals[0]["reason"] == mm["reason"]
    # 人类对战不计 Glicko
    r = store.get_rating(b["id"])
    assert r["matches_played"] == 0


async def _wait_done(store, mid, done):
    for _ in range(400):
        mm = store.get_match(mid)
        if mm["status"] in ("completed", "aborted"):
            done.set()
            return
        await asyncio.sleep(0.1)
    done.set()


def test_human_timeout_returns_fail_response(store: Store):
    """人类决策超时 → 回 fail_response（棋类非法坐标 → 判负）。"""
    u, b = _setup(store)
    orch = _orch(store, human_timeout=0.2)

    async def never_respond(player_idx, request):
        # 注册一个永不解析的 Future，让 wait_for 超时
        import asyncio as _a
        loop = _a.get_running_loop()
        fut = loop.create_future()
        orch._human_turns[("__test__", player_idx)] = {"request": request, "future": fut}
        try:
            return await _a.wait_for(fut, timeout=orch.human_action_timeout + 5)
        except _a.TimeoutError:
            from bzplat.backend.matches.runner import _fail_response
            return _fail_response("gomoku")

    async def run():
        result = await orch.runner.run_bot_vs_human(
            b["binary_path"], bot_seat=1, human_decide=never_respond,
            game_id="gomoku", on_event=lambda k, e: None,
        )
        return result

    result = asyncio.run(run())
    # 人类（座0）超时回非法坐标 → bot（座1）胜
    assert result.winner == 1


def test_per_user_throttle_one_concurrent_human(store: Store):
    u, b = _setup(store)
    orch = _orch(store)
    enable_execution_queue(store)
    first_request = asyncio.run(
        orch.challenge_human(b["id"], u["id"], game_id="gomoku")
    )
    with pytest.raises(ValueError, match="已有一场人类对局请求"):
        asyncio.run(orch.challenge_human(b["id"], u["id"], game_id="gomoku"))
    assert store.executions.get(first_request)["status"] == "queued"


def test_admin_abort_releases_human_task_queued_on_full_semaphore(store: Store):
    """Queued human abort releases ownership and delivers the terminal event."""
    u, b = _setup(store)
    orch = _orch(store)

    async def exercise():
        # Zero permits models the one shared global match slot being occupied.
        orch._sem = asyncio.Semaphore(0)
        first = await human_and_start(
            orch,
            b["id"], u["id"], human_seat=0, game_id="gomoku"
        )
        await asyncio.sleep(0)
        first_task = orch._tasks[first]
        assert not first_task.done()
        assert u["id"] in orch._human_active_users
        queue = orch.subscribe(first)
        assert queue.get_nowait()["type"] == "snapshot"

        # Also prove all per-match turn state is cleared by the same ownership path.
        orch._human_turns[(first, 0)] = {
            "future": asyncio.get_running_loop().create_future()
        }
        aborted = await orch.abort_match(first)
        assert aborted["status"] == "aborted"
        assert first not in orch._tasks
        assert not any(key[0] == first for key in orch._human_turns)
        assert u["id"] not in orch._human_active_users
        terminal = queue.get_nowait()
        assert terminal == {"type": "error", "reason": "admin_aborted"}
        assert first not in orch._sse
        assert store.executions.finalize_ready() == 1

        # Regression assertion: the leaked set entry formerly rejected this call.
        second = await human_and_start(
            orch,
            b["id"], u["id"], human_seat=0, game_id="gomoku"
        )
        await asyncio.sleep(0)
        await orch.abort_match(second)
        return second

    second = asyncio.run(exercise())
    assert store.get_match(second)["status"] == "aborted"
    assert u["id"] not in orch._human_active_users


def test_human_match_uses_shared_semaphore_and_one_sandbox_unit(store: Store):
    """人类局占共享 match slot，但资源向量只计一个 Bot sandbox。"""
    u, b = _setup(store)
    orch = _orch(store)
    enable_execution_queue(store)
    request_id = asyncio.run(
        orch.challenge_human(b["id"], u["id"], game_id="gomoku")
    )
    request = store.executions.get(request_id)
    assert not hasattr(orch, "_human_sem")
    assert request["match_slots"] == 1
    assert request["sandbox_units"] == 1


# ── WebSocket /play + POST /api/matches/human API ──────────────
def test_human_match_api_and_websocket(store: Store, tmp_path, monkeypatch):
    """POST /api/matches/human 建局；WS /play 仅接受 HttpOnly 会话 Cookie。"""
    from fastapi.testclient import TestClient
    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "http://testserver")
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    # 复用 store 的 db 路径建 app（同库）
    app = create_app(db_path=store.path)
    s = app.state.store
    u = s.create_user("usr2", "u2@ex.com", hash_password("password1"))
    s.update_user(u["id"], email_verified=1)
    b = s.create_bot(
        u["id"], "wb", binary_path=os.path.abspath("samples/gomokubot_linux_amd64"),
        format="elf", game_id="gomoku",
    )
    _, token = app.state.auth.authenticate("usr2", "password1")
    # TestClient 必须作为 context 使用，让 FastAPI lifespan 在退出时调用
    # orchestrator.shutdown()，收敛本用例创建但尚未完成的人类对局任务。
    with TestClient(app) as c:
        # 产品入口固定人类为座位 2（交换决策方，棋色由交换结果决定）；
        # 内部 seat 0 不对外开放。
        rejected = c.post(
            "/api/matches/human", headers={"Authorization": f"Bearer {token}"},
            json={"bot_id": b["id"], "human_seat": 0, "game_id": "gomoku"},
        )
        assert rejected.status_code == 422
        # 建人类对局
        r = c.post(
            "/api/matches/human", headers={"Authorization": f"Bearer {token}"},
            json={"bot_id": b["id"], "human_seat": 1, "game_id": "gomoku"},
        )
        assert r.status_code == 202
        request_id = r.json()["public_id"]
        mid = None
        for _ in range(100):
            request = s.executions.get(request_id)
            mid = request.get("current_match_id") if request else None
            if mid:
                break
            import time
            time.sleep(0.02)
        assert mid is not None

        # 无 Cookie → 拒绝
        with c.websocket_connect(
            f"/api/matches/{mid}/play",
            headers={"origin": "http://testserver"},
        ) as ws:
            msg = ws.receive_json()
        assert msg["type"] == "reject"
        assert msg["reason"] == "forbidden"

        # 即使 query 里是合法长期 token 也必须拒绝，防止进入 access log。
        with c.websocket_connect(
            f"/api/matches/{mid}/play?token={token}",
            headers={"origin": "http://testserver"},
        ) as ws:
            msg = ws.receive_json()
        assert msg["type"] == "reject"
        assert msg["reason"] == "forbidden"

        # 合法 HttpOnly 会话 Cookie → 收 snapshot。TestClient 在此显式模拟
        # 浏览器在同源 WebSocket 握手中自动携带 Cookie。
        c.cookies.set(COOKIE_NAME, token)
        with c.websocket_connect(
            f"/api/matches/{mid}/play?token={token}",
            headers={"origin": "http://testserver"},
        ) as ws:
            msg = ws.receive_json()
        assert msg["type"] == "reject"
        assert msg["reason"] == "forbidden"

        for headers in ({}, {"origin": "https://evil.example"}):
            with c.websocket_connect(
                f"/api/matches/{mid}/play",
                headers=headers,
            ) as ws:
                msg = ws.receive_json()
            assert msg["type"] == "reject"
            assert msg["reason"] == "forbidden"

        with c.websocket_connect(
            f"/api/matches/{mid}/play",
            headers={"origin": "HTTP://TESTSERVER:80/"},
        ) as ws:
            snap = ws.receive_json()
        assert snap["type"] == "snapshot"
        assert snap["match"]["human_seat"] == 1
        for internal in (
            "owner_id", "human_user_id", "match_seed", "_replay_events_json",
        ):
            assert internal not in snap["match"]
        assert app.state.orch._human_ws_total == 0
        assert app.state.orch._human_ws_subscriptions == {}

        # 该局刻意只读取快照、不落子；退出 client 前它应仍是编排器拥有的
        # 后台任务，精确覆盖曾导致 pytest 退出挂起的场景。
        assert mid in app.state.orch._tasks

    # TestClient.__exit__ 必须跑完 lifespan，并在事件循环仍存活时 cancel +
    # gather 对局任务。仅关闭 WebSocket 不足以收敛 subprocess/Future。
    assert not app.state.orch._tasks
    assert not app.state.orch._human_turns
    assert u["id"] not in app.state.orch._human_active_users


def test_completed_human_websocket_closes_after_one_snapshot(
    store: Store, monkeypatch
):
    """A terminal reconnect is server-closed and removed from the SSE registry."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "http://testserver")
    app = create_app(db_path=store.path)
    s = app.state.store
    user = s.create_user("donews", "donews@example.com", hash_password("password1"))
    s.update_user(user["id"], email_verified=1)
    bot = s.create_bot(
        user["id"], "donews_bot", binary_path="/tmp/donews", format="elf", game_id="gomoku",
    )
    mid = "20260809-human-terminal"
    s.create_match(
        mid,
        bot["id"],
        bot["id"],
        owner_id=user["id"],
        match_type="human",
        game_id="gomoku",
        human_user_id=user["id"],
        human_seat=1,
    )
    s.update_match(mid, status="completed", winner=0)
    _, token = app.state.auth.authenticate("donews", "password1")

    with TestClient(app) as client:
        client.cookies.set(COOKIE_NAME, token)
        with client.websocket_connect(
            f"/api/matches/{mid}/play",
            headers={"origin": "http://testserver"},
        ) as ws:
            snapshot = ws.receive_json()
            assert snapshot["type"] == "snapshot"
            assert snapshot["match"]["status"] == "completed"
            with pytest.raises(WebSocketDisconnect) as closed:
                ws.receive_json()
            assert closed.value.code == 1000
    assert mid not in app.state.orch._sse
    assert app.state.orch._human_ws_total == 0
    assert app.state.orch._human_ws_subscriptions == {}


def test_human_websocket_quota_rejection_closes_with_stable_code(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "http://testserver")
    app = create_app(db_path=store.path)
    with TestClient(app) as client:
        s = app.state.store
        user = s.create_user(
            "limitedws", "limitedws@example.test", hash_password("password1")
        )
        s.update_user(user["id"], email_verified=1)
        bot = s.create_bot(
            user["id"],
            "limitedws-bot",
            binary_path="/tmp/limitedws-bot",
            format="elf",
            game_id="gomoku",
        )
        match_id = "human-ws-limit"
        s.create_match(
            match_id,
            bot["id"],
            bot["id"],
            owner_id=user["id"],
            match_type="human",
            game_id="gomoku",
            human_user_id=user["id"],
            human_seat=1,
        )
        _, token = app.state.auth.authenticate("limitedws", "password1")
        client.cookies.set(COOKIE_NAME, token)

        def reject_subscribe(*_args, **_kwargs):
            raise HumanWebSocketLimitError("user")

        monkeypatch.setattr(app.state.orch, "subscribe", reject_subscribe)
        with client.websocket_connect(
            f"/api/matches/{match_id}/play",
            headers={"origin": "http://testserver"},
        ) as websocket:
            with pytest.raises(WebSocketDisconnect) as rejected:
                websocket.receive_json()

        assert rejected.value.code == 1013
        assert rejected.value.reason == "connection_limit"


def test_human_websocket_rejects_noncanonical_actions_without_resolving_turn(
    store: Store, monkeypatch
):
    """非 ``{"response": ...}`` 或带额外键的消息不得解析 Future。

    只有通过当前游戏 ``validate_response_payload`` 的唯一信封才进
    ``resolve_human_turn``；因而用户可在同一回合重试，不会被静默折成默认动作。
    """
    from fastapi.testclient import TestClient

    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "http://testserver")
    app = create_app(db_path=store.path)
    mid = "20260809-human-strict-protocol"
    resolved: list[dict] = []

    def record_resolve(match_id, seat, move):
        resolved.append({"match_id": match_id, "seat": seat, "move": move})
        return False

    app.state.orch.resolve_human_turn = record_resolve

    with TestClient(app) as client:
        # Lifespan 会先清理上次进程遗留的 pending 对局；本用例须在该清理完成后
        # 创建活跃对局，避免把启动恢复行为误当作 WS 协议行为。
        s = app.state.store
        user = s.create_user("strictws", "strictws@example.com", hash_password("password1"))
        s.update_user(user["id"], email_verified=1)
        bot = s.create_bot(
            user["id"], "strictws_bot", binary_path="/tmp/strictws", format="elf",
            game_id="gomoku",
        )
        s.create_match(
            mid,
            bot["id"],
            bot["id"],
            owner_id=user["id"],
            match_type="human",
            game_id="gomoku",
            human_user_id=user["id"],
            human_seat=1,
        )
        _, token = app.state.auth.authenticate("strictws", "password1")
        client.cookies.set(COOKIE_NAME, token)
        with client.websocket_connect(
            f"/api/matches/{mid}/play",
            headers={"origin": "http://testserver"},
        ) as ws:
            assert ws.receive_json()["type"] == "snapshot"

            for invalid in (
                {"x": 7, "y": 7},
                {"response": {"x": 7, "y": 7}, "debug": "legacy"},
                {"response": 7},
            ):
                ws.send_json(invalid)
                rejection = ws.receive_json()
                assert rejection["type"] == "reject"
                assert "动作协议错误" in rejection["message"]
                assert resolved == []

            ws.send_json(
                {"response": {"action": "move", "x": 7, "y": 7}}
            )
            current_turn = ws.receive_json()
            assert current_turn["type"] == "reject"
            assert "当前非你的回合" in current_turn["message"]

    assert resolved == [
        {
            "match_id": mid,
            "seat": 1,
            "move": {
                "response": {"action": "move", "x": 7, "y": 7}
            },
        }
    ]


def test_human_websocket_rejects_oversized_frame_before_authority_reads(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "http://testserver")
    app = create_app(db_path=store.path)
    with TestClient(app) as client:
        s = app.state.store
        user = s.create_user(
            "oversizedws", "oversizedws@example.test", hash_password("password1")
        )
        s.update_user(user["id"], email_verified=1)
        bot = s.create_bot(
            user["id"],
            "oversizedws-bot",
            binary_path="/tmp/oversizedws-bot",
            format="elf",
            game_id="gomoku",
        )
        match_id = "human-ws-oversized"
        s.create_match(
            match_id,
            bot["id"],
            bot["id"],
            owner_id=user["id"],
            match_type="human",
            game_id="gomoku",
            human_user_id=user["id"],
            human_seat=1,
        )
        _, token = app.state.auth.authenticate("oversizedws", "password1")
        client.cookies.set(COOKIE_NAME, token)

        authority_reads = {"session": 0, "match": 0}
        original_verify = app.state.auth.verify_session
        original_get_match = s.get_match

        def counted_verify(value):
            authority_reads["session"] += 1
            return original_verify(value)

        def counted_get_match(value):
            authority_reads["match"] += 1
            return original_get_match(value)

        monkeypatch.setattr(app.state.auth, "verify_session", counted_verify)
        monkeypatch.setattr(s, "get_match", counted_get_match)
        with client.websocket_connect(
            f"/api/matches/{match_id}/play",
            headers={"origin": "http://testserver"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "snapshot"
            baseline = dict(authority_reads)
            websocket.send_text("x" * (HUMAN_ACTION_MAX_BYTES + 1))
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "message_too_large",
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1009
            assert authority_reads == baseline


def test_human_websocket_rate_limit_precedes_authority_reads(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "http://testserver")
    app = create_app(db_path=store.path)
    with TestClient(app) as client:
        s = app.state.store
        user = s.create_user(
            "ratelimitedws",
            "ratelimitedws@example.test",
            hash_password("password1"),
        )
        s.update_user(user["id"], email_verified=1)
        bot = s.create_bot(
            user["id"],
            "ratelimitedws-bot",
            binary_path="/tmp/ratelimitedws-bot",
            format="elf",
            game_id="gomoku",
        )
        match_id = "human-ws-message-rate"
        s.create_match(
            match_id,
            bot["id"],
            bot["id"],
            owner_id=user["id"],
            match_type="human",
            game_id="gomoku",
            human_user_id=user["id"],
            human_seat=1,
        )
        _, token = app.state.auth.authenticate("ratelimitedws", "password1")
        client.cookies.set(COOKIE_NAME, token)

        authority_reads = {"session": 0, "match": 0}
        original_verify = app.state.auth.verify_session
        original_get_match = s.get_match

        def counted_verify(value):
            authority_reads["session"] += 1
            return original_verify(value)

        def counted_get_match(value):
            authority_reads["match"] += 1
            return original_get_match(value)

        monkeypatch.setattr(app.state.auth, "verify_session", counted_verify)
        monkeypatch.setattr(s, "get_match", counted_get_match)
        with client.websocket_connect(
            f"/api/matches/{match_id}/play",
            headers={"origin": "http://testserver"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "snapshot"
            for _ in range(HUMAN_ACTION_RATE_BURST):
                websocket.send_json({"not_response": 1})
                assert websocket.receive_json()["type"] == "reject"
            before_limited_frame = dict(authority_reads)

            websocket.send_json({"not_response": 1})
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "rate_limit_exceeded",
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1008
            assert authority_reads == before_limited_frame


@pytest.mark.parametrize(
    "session_mutation",
    ["revoke", "change_password", "disable_user"],
)
def test_human_websocket_rechecks_session_before_each_action(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    session_mutation: str,
):
    """An upgraded socket must not outlive the authority of its session.

    Password changes, explicit session revocation, and account suspension all
    invalidate an already-open browser connection.  The first later action is
    rejected before it can resolve the pending human turn.
    """
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "http://testserver")
    app = create_app(db_path=store.path)
    mid = f"human-session-{session_mutation}"
    resolved: list[dict] = []

    def record_resolve(match_id, seat, move):
        resolved.append({"match_id": match_id, "seat": seat, "move": move})
        # Keep the vulnerable implementation responsive enough to produce a
        # deterministic red assertion instead of waiting forever for a frame.
        return False

    app.state.orch.resolve_human_turn = record_resolve

    with TestClient(app) as client:
        # Create the pending match after lifespan recovery so startup cleanup
        # cannot turn this authorization regression into a terminal reconnect.
        s = app.state.store
        user = s.create_user(
            f"ws-{session_mutation}",
            f"ws-{session_mutation}@example.test",
            hash_password("password1"),
        )
        s.update_user(user["id"], email_verified=1)
        bot = s.create_bot(
            user["id"],
            f"ws-bot-{session_mutation}",
            binary_path="/tmp/session-revalidation",
            format="elf",
            game_id="gomoku",
        )
        s.create_match(
            mid,
            bot["id"],
            bot["id"],
            owner_id=user["id"],
            match_type="human",
            game_id="gomoku",
            human_user_id=user["id"],
            human_seat=1,
        )
        _, token = app.state.auth.authenticate(
            f"ws-{session_mutation}", "password1"
        )
        client.cookies.set(COOKIE_NAME, token)

        with client.websocket_connect(
            f"/api/matches/{mid}/play",
            headers={"origin": "http://testserver"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "snapshot"

            if session_mutation == "revoke":
                s.delete_session(token)
            elif session_mutation == "change_password":
                app.state.auth.change_password(
                    user["id"], "password1", "password2"
                )
            else:
                s.update_user(user["id"], is_active=0)

            websocket.send_json(
                {"response": {"action": "move", "x": 7, "y": 7}}
            )
            rejection = websocket.receive_json()
            assert rejection == {
                "type": "reject",
                "reason": "session_revoked",
                "message": "会话已失效，请重新登录",
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1008

    assert resolved == []
    assert app.state.orch._human_ws_total == 0
    assert app.state.orch._human_ws_subscriptions == {}


@pytest.mark.parametrize(
    "authority_mutation",
    ["delete_session", "disable_user", "owner_drift"],
)
@pytest.mark.parametrize(
    "queued_event",
    [
        {"type": "move", "player": 0, "x": 7, "y": 7},
        {
            "type": "deal_hole",
            "hand": 1,
            "holes": [["AS", "AH"], ["KS", "KH"]],
        },
    ],
    ids=["ordinary-event", "private-deal-hole"],
)
def test_human_websocket_revocation_blocks_next_server_event(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    authority_mutation: str,
    queued_event: dict,
):
    """A passive revoked socket must not receive any later server event."""

    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "http://testserver")
    app = create_app(db_path=store.path)
    match_id = f"human-private-revoke-{authority_mutation}"

    with TestClient(app) as client:
        current = app.state.store.create_user(
            f"private_{authority_mutation}",
            f"private-{authority_mutation}@example.test",
            hash_password("password1"),
        )
        replacement = app.state.store.create_user(
            f"replacement_{authority_mutation}",
            f"replacement-{authority_mutation}@example.test",
            hash_password("password1"),
        )
        app.state.store.update_user(current["id"], email_verified=1)
        bot = app.state.store.create_bot(
            current["id"],
            f"private-bot-{authority_mutation}",
            binary_path="/tmp/private-event-revalidation",
            format="elf",
            game_id="holdem",
        )
        app.state.store.create_match(
            match_id,
            bot["id"],
            bot["id"],
            owner_id=current["id"],
            match_type="human",
            game_id="holdem",
            human_user_id=current["id"],
            human_seat=1,
        )
        _, token = app.state.auth.authenticate(
            current["username"], "password1"
        )
        client.cookies.set(COOKIE_NAME, token)

        with client.websocket_connect(
            f"/api/matches/{match_id}/play",
            headers={"origin": "http://testserver"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "snapshot"

            if authority_mutation == "delete_session":
                assert app.state.store.delete_session(token)
            elif authority_mutation == "disable_user":
                app.state.store.update_user(current["id"], is_active=0)
            else:
                app.state.store.update_match(
                    match_id,
                    human_user_id=replacement["id"],
                )

            app.state.orch._broadcast(match_id, queued_event)
            # The first frame after revocation must be the generic denial, never
            # the queued ordinary event or the human viewer's private cards.
            assert websocket.receive_json() == {
                "type": "reject",
                "reason": "session_revoked",
                "message": "会话已失效，请重新登录",
            }
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1008
            assert closed.value.reason == "session_revoked"

    assert app.state.orch._human_ws_total == 0
    assert app.state.orch._human_ws_subscriptions == {}


def test_human_websocket_concurrent_revocation_waits_for_policy_close(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
):
    """Concurrent sender/receiver revocation must complete one 1008 close."""

    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocket, WebSocketDisconnect

    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "http://testserver")
    app = create_app(db_path=store.path)
    match_id = "human-concurrent-revocation"
    rejection_started = threading.Event()
    release_rejection = threading.Event()
    rejection_cancelled = threading.Event()
    second_revoked_check = threading.Event()
    revocation_armed = threading.Event()
    revoked_checks = 0
    revoked_checks_lock = threading.Lock()
    original_send_json = WebSocket.send_json

    async def gated_send_json(self, data, mode="text"):
        if data.get("reason") == "session_revoked":
            rejection_started.set()
            try:
                await asyncio.to_thread(release_rejection.wait)
            except asyncio.CancelledError:
                rejection_cancelled.set()
                raise
        return await original_send_json(self, data, mode=mode)

    monkeypatch.setattr(WebSocket, "send_json", gated_send_json)

    with TestClient(app) as client:
        current = app.state.store.create_user(
            "concurrent_revoke",
            "concurrent-revoke@example.test",
            hash_password("password1"),
        )
        app.state.store.update_user(current["id"], email_verified=1)
        bot = app.state.store.create_bot(
            current["id"],
            "concurrent-revoke-bot",
            binary_path="/tmp/concurrent-revocation",
            format="elf",
            game_id="holdem",
        )
        app.state.store.create_match(
            match_id,
            bot["id"],
            bot["id"],
            owner_id=current["id"],
            match_type="human",
            game_id="holdem",
            human_user_id=current["id"],
            human_seat=1,
        )
        _, token = app.state.auth.authenticate(
            current["username"], "password1"
        )
        client.cookies.set(COOKIE_NAME, token)
        original_verify = app.state.auth.verify_session

        def tracked_verify(value):
            nonlocal revoked_checks
            result = original_verify(value)
            if revocation_armed.is_set() and value == token and result is None:
                with revoked_checks_lock:
                    revoked_checks += 1
                    if revoked_checks >= 2:
                        second_revoked_check.set()
            return result

        monkeypatch.setattr(app.state.auth, "verify_session", tracked_verify)

        try:
            with client.websocket_connect(
                f"/api/matches/{match_id}/play",
                headers={"origin": "http://testserver"},
            ) as websocket:
                assert websocket.receive_json()["type"] == "snapshot"
                assert app.state.store.delete_session(token)
                revocation_armed.set()

                # Hold the sender inside its policy rejection, then make the
                # receiver discover the same revocation. The receiver must wait
                # for the unique reject + 1008 sequence instead of completing
                # FIRST_COMPLETED and cancelling the sender mid-close.
                app.state.orch._broadcast(
                    match_id,
                    {
                        "type": "deal_hole",
                        "hand": 1,
                        "holes": [["AS", "AH"], ["KS", "KH"]],
                    },
                )
                assert rejection_started.wait(timeout=2)
                websocket.send_json({"response": {"action": "fold"}})
                assert second_revoked_check.wait(timeout=2)
                assert not rejection_cancelled.wait(timeout=0.5)

                release_rejection.set()
                assert websocket.receive_json() == {
                    "type": "reject",
                    "reason": "session_revoked",
                    "message": "会话已失效，请重新登录",
                }
                with pytest.raises(WebSocketDisconnect) as closed:
                    websocket.receive_json()
                assert closed.value.code == 1008
                assert closed.value.reason == "session_revoked"
        finally:
            release_rejection.set()

    assert app.state.orch._human_ws_total == 0
    assert app.state.orch._human_ws_subscriptions == {}


def test_human_websocket_rechecks_match_owner_after_payload_validation(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
):
    """A concurrent ownership change must win before turn resolution."""
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect
    from types import SimpleNamespace

    import bzplat.backend.api_routes as api_routes
    from bzplat.backend.games import registry as game_registry
    from bzplat.backend.main import create_app

    monkeypatch.setenv("BZ_PUBLIC_ORIGIN", "http://testserver")
    app = create_app(db_path=store.path)
    match_id = "human-owner-validation-race"
    resolved: list[dict] = []

    def record_resolve(requested_match_id, seat, move):
        resolved.append({"match_id": requested_match_id, "seat": seat, "move": move})
        return False

    app.state.orch.resolve_human_turn = record_resolve
    with TestClient(app) as client:
        current = app.state.store.create_user(
            "owner_race_current",
            "owner-race-current@example.test",
            hash_password("password1"),
        )
        replacement = app.state.store.create_user(
            "owner_race_replacement",
            "owner-race-replacement@example.test",
            hash_password("password1"),
        )
        app.state.store.update_user(current["id"], email_verified=1)
        bot = app.state.store.create_bot(
            current["id"],
            "owner-race-bot",
            binary_path="/tmp/owner-race-bot",
            format="elf",
            game_id="gomoku",
        )
        app.state.store.create_match(
            match_id,
            bot["id"],
            bot["id"],
            owner_id=current["id"],
            match_type="human",
            game_id="gomoku",
            human_user_id=current["id"],
            human_seat=1,
        )
        _, token = app.state.auth.authenticate(
            current["username"], "password1"
        )
        client.cookies.set(COOKIE_NAME, token)

        protocol = game_registry.get("gomoku").protocol
        original_validate = protocol.validate_response_payload

        def validate_then_reassign(payload):
            validated = original_validate(payload)
            app.state.store.update_match(
                match_id,
                human_user_id=replacement["id"],
            )
            return validated

        monkeypatch.setattr(
            api_routes,
            "game_registry",
            SimpleNamespace(
                get=lambda _game_id: SimpleNamespace(
                    protocol=SimpleNamespace(
                        validate_response_payload=validate_then_reassign
                    )
                )
            ),
        )
        with client.websocket_connect(
            f"/api/matches/{match_id}/play",
            headers={"origin": "http://testserver"},
        ) as websocket:
            assert websocket.receive_json()["type"] == "snapshot"
            websocket.send_json(
                {"response": {"action": "move", "x": 7, "y": 7}}
            )
            rejection = websocket.receive_json()
            assert rejection["reason"] == "session_revoked"
            with pytest.raises(WebSocketDisconnect) as closed:
                websocket.receive_json()
            assert closed.value.code == 1008

    assert resolved == []
    assert app.state.orch._human_ws_total == 0
    assert app.state.orch._human_ws_subscriptions == {}


# ── Bot 启动崩溃快速失败（PR-G1 治本）──────────────────────────
def test_bot_crashed_aborts_human_match_quickly(store: Store):
    """Bot 启动崩溃 → BotCrashedError 向上传播 → 对局快速 abort，
    而非吞成 fold 死磕。验证 _run_human_match 的 abort + 锁清理。"""
    class CrashingHumanRunner:
        async def run_bot_vs_human(self, *args, **kwargs):
            raise BotCrashedError(
                "controlled human-bot startup crash", crashed_seat=1
            )

    u = store.create_user("crashusr", "c@ex.com", hash_password("password1"))
    b = store.create_bot(
        u["id"], "crashbot",
        binary_path=os.path.abspath("samples/gomokubot_linux_amd64"), format="elf",
        game_id="gomoku",
    )
    store.ensure_rating(b["id"])
    orch = MatchOrchestrator(
        store,
        runner=CrashingHumanRunner(),
        max_concurrent=2,
    )
    orch.set_human_action_timeout(1.0)

    async def run():
        mid = await human_and_start(
            orch,
            b["id"], u["id"], human_seat=0, game_id="gomoku",
        )
        # 等对局 task 结束（应在数秒内 abort，而非等超时死磕）
        task = orch._tasks.get(mid)
        if task:
            try:
                await asyncio.wait_for(task, timeout=15)
            except Exception:
                pass
        return mid

    mid = asyncio.run(run())
    m = store.get_match(mid)
    # 对局应被 abort（而非 running/completed）
    assert m["status"] == "aborted", f"expected aborted, got {m['status']} ({m.get('reason')})"
    assert m["reason"] == "bot_crashed"
    # 用户锁应已释放（可再次建局）
    assert u["id"] not in orch._human_active_users


def test_bot_crashed_error_not_swallowed_by_runner(store: Store):
    """run_bot_vs_human 在 Bot 会话启动崩溃时应原样抛 BotCrashedError。"""
    from bzplat.backend.runtime.binary_runner import BotCrashedError

    class CrashingBinaryRunner:
        async def prepare_session(self, *args, **kwargs):
            raise BotCrashedError("controlled prepare crash")

    runner = MatchRunner(CrashingBinaryRunner())

    async def human_decide(player_idx, request):
        return {"x": 0, "y": 0}  # 不会到达（bot 先崩）

    async def run():
        return await runner.run_bot_vs_human(
            os.path.abspath("samples/gomokubot_linux_amd64"),
            bot_seat=1, human_decide=human_decide,
            game_id="gomoku", on_event=lambda k, e: None,
        )

    with pytest.raises(BotCrashedError):
        asyncio.run(run())


# ── 修复：your_turn 持久化 + 连续超时中止（点不动按钮根因） ──────
def test_your_turn_persisted_into_replay_snapshot(store: Store):
    """your_turn 事件必须进入持久化 events_json（snapshot.events），否则：
    - WS 晚连/重连/StrictMode 重挂载时，snapshot 历史里没有 your_turn；
    - 前端 myTurn 无法从历史恢复 → 按钮永远点不动 → 每手超时弃牌。
    复现对局 20260802132013-7eb5087b：8 手全部弃牌（人类从未响应）。
    """
    u, b = _setup(store, game="gomoku")
    orch = _orch(store, human_timeout=1.0)
    first_turn_seen = asyncio.Event()

    async def solver():
        """模拟 WS /move：仅在首个 your_turn 出现后让第一手响应（其余超时）。"""
        for _ in range(500):
            for (mid, pidx), entry in list(orch._human_turns.items()):
                if not first_turn_seen.is_set():
                    first_turn_seen.set()  # 标记已经看到至少一个 your_turn
                if not entry["future"].done():
                    # 只解第一手，其余超时
                    if not getattr(solver, "_first_done", False):
                        orch.resolve_human_turn(mid, pidx, {"x": 7, "y": 7})
                        solver._first_done = True
            await asyncio.sleep(0.02)

    async def run():
        mid = await human_and_start(
            orch, b["id"], u["id"], human_seat=0, game_id="gomoku"
        )
        done = asyncio.Event()
        await asyncio.gather(solver(), _wait_done(store, mid, done))
        return mid

    mid = asyncio.run(run())
    # 等快照稳定：读取持久化事件
    import json as _json
    events = _json.loads(store.get_replay(mid)["events_json"])
    types = [e.get("type") for e in events]
    assert "your_turn" in types, (
        f"your_turn 未持久化到 events_json，前端重连无法恢复 myTurn；"
        f"实际事件类型: {types}"
    )


def test_consecutive_human_timeouts_aborts_match(store: Store):
    """人类连续多次超时不响应 → 应中止对局，而非死磕 70 手最长 2.3 小时。
    否则对局永久卡在 running，占 _human_active_users，用户无法再开新对局。
    用 holdem（超时=弃牌，对局会持续），棋类一手非法即结束不适用此场景。
    复现对局 20260802132013-7eb5087b：holdem 卡 running，rounds_played=0，8 手全弃牌。
    """
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    u = store.create_user("touser", "t@ex.com", hash_password("password1"))
    path = os.path.abspath("samples/callbot_linux_amd64")
    b = store.create_bot(u["id"], "holdbot", binary_path=path, format="elf", game_id="holdem")
    orch = _orch(store, human_timeout=0.3)
    orch.human_max_consecutive_timeouts = 3  # 连续 3 次超时即中止

    async def run():
        mid = await human_and_start(
            orch,
            b["id"], u["id"], human_seat=1, game_id="holdem",
        )
        # 从不响应 → 每手弃牌超时 → 连续达阈值应中止
        for _ in range(400):
            m = store.get_match(mid)
            if m["status"] in ("completed", "aborted"):
                return mid
            await asyncio.sleep(0.1)
        return mid

    mid = asyncio.run(run())
    m = store.get_match(mid)
    assert m["status"] == "aborted", (
        f"连续超时应中止对局，实际 status={m['status']}（卡死占用人类槽）"
    )
    assert "human_inactive" in (m["reason"] or ""), f"reason 应标注人类不活跃: {m['reason']}"
    # 中止后释放用户锁
    assert u["id"] not in orch._human_active_users


# ── resolve_human_turn 幂等性（审计：done 后再 resolve 不抛）──────────────────


def test_resolve_human_turn_after_done_returns_false(store: Store):
    """future 已 done 后再 resolve_human_turn：返 False 不抛（防御性 catch InvalidStateError）。

    注：resolve_human_turn 是同步函数，CPython 下 done() 检查与 set_result 不会真正
    交错——此测试验证的是 done 后的幂等降级（早返 False），不声称测真正的并发竞态。
    fix 的 try/except InvalidStateError 是无害防御，保留。
    """
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    orch = MatchOrchestrator(
        store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=2
    )
    # 注册一个 pending human turn（用 new_event_loop 创建 Future，避免无运行 loop 报错）
    loop = asyncio.new_event_loop()
    try:
        fut = loop.create_future()
        orch._human_turns[("m_race", 0)] = {"future": fut}
        # 第一次 resolve → 成功
        assert orch.resolve_human_turn("m_race", 0, {"row": 7, "col": 7}) is True
        assert fut.done()
        # 第二次（已 done）→ 应返 False，不抛 InvalidStateError
        assert orch.resolve_human_turn("m_race", 0, {"row": 8, "col": 8}) is False
    finally:
        loop.close()


def test_resolve_human_turn_unknown_match_returns_false(store: Store):
    """未注册的 match/seat → 返 False（不抛 KeyError）。"""
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    orch = MatchOrchestrator(
        store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=2
    )
    assert orch.resolve_human_turn("nonexistent", 0, {"row": 0, "col": 0}) is False
