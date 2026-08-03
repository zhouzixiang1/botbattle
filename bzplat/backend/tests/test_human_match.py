"""人类 vs bot 对战测试（challenge_human / 回合 Future / 超时 / 不计分 / 独立并发）。"""
from __future__ import annotations

import asyncio
import os

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store
from bzplat.backend.store.schema import TYPE_HUMAN


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
    mid = asyncio.run(orch.challenge_human(b["id"], u["id"], human_seat=0, game_id="gomoku"))
    m = store.get_match(mid)
    assert m["match_type"] == TYPE_HUMAN
    assert m["human_user_id"] == u["id"]
    assert m["human_seat"] == 0


def test_human_turn_registry_resolve_and_no_rating(store: Store):
    """challenge_human 走 orchestrator 自带 human_decide：
    它注册 Future + 广播 your_turn；外部（WS）通过 resolve_human_turn 解析。
    完成后人类对战不计 Glicko（bot 的 matches_played 不变）。
    """
    u, b = _setup(store)
    orch = _orch(store)
    done = asyncio.Event()

    async def solver():
        """模拟 WS /move：每次 orchestrator 注册 pending 回合就立即解析。"""
        for _ in range(500):
            for (mid, pidx), entry in list(orch._human_turns.items()):
                if mid == mid_ref["id"] and not entry["future"].done():
                    orch.resolve_human_turn(mid, pidx, {"x": 7, "y": 7})
            if done.is_set():
                return
            await asyncio.sleep(0.05)

    mid_ref: dict = {}

    async def run():
        mid = await orch.challenge_human(b["id"], u["id"], human_seat=0, game_id="gomoku")
        mid_ref["id"] = mid
        await asyncio.gather(
            solver(),
            _wait_done(store, mid, done),
        )
        return store.get_match(mid)

    mm = asyncio.run(run())
    assert mm["status"] == "completed"
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
    # 先占住 user 的名额
    orch._human_active_users.add(u["id"])
    with pytest.raises(ValueError, match="进行中"):
        asyncio.run(orch.challenge_human(b["id"], u["id"], game_id="gomoku"))


def test_human_match_uses_independent_semaphore(store: Store):
    """人类对局走 _human_sem，不占 bot 的 _sem 槽。"""
    u, b = _setup(store)
    orch = _orch(store)
    assert orch._human_sem is not orch._sem
    assert orch.human_max_concurrent >= 1


# ── WebSocket /play + POST /api/matches/human API ──────────────
def test_human_match_api_and_websocket(store: Store, tmp_path):
    """POST /api/matches/human 建局；WS /play 鉴权（拒绝无 token / 接受合法）。"""
    from fastapi.testclient import TestClient
    from bzplat.backend.main import create_app

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
    c = TestClient(app)

    # 建人类对局
    r = c.post(
        "/api/matches/human", headers={"Authorization": f"Bearer {token}"},
        json={"bot_id": b["id"], "human_seat": 0, "game_id": "gomoku"},
    )
    assert r.status_code == 200
    mid = r.json()["match_id"]

    # 无 token → 拒绝
    with c.websocket_connect(f"/api/matches/{mid}/play") as ws:
        msg = ws.receive_json()
    assert msg["type"] == "error"

    # 合法 token → 收 snapshot
    with c.websocket_connect(f"/api/matches/{mid}/play?token={token}") as ws:
        snap = ws.receive_json()
    assert snap["type"] == "snapshot"
    assert snap["match"]["human_seat"] == 0


# ── Bot 启动崩溃快速失败（PR-G1 治本）──────────────────────────
def test_bot_crashed_aborts_human_match_quickly(store: Store):
    """Bot 启动即崩（不存在的二进制）→ BotCrashedError 向上传播 → 对局快速 abort，
    而非吞成 fold 死磕。验证 _run_human_match 的 abort + 锁清理。"""
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    u = store.create_user("crashusr", "c@ex.com", hash_password("password1"))
    b = store.create_bot(
        u["id"], "crashbot", binary_path="/nonexistent/crash_bot", format="elf",
        game_id="gomoku",
    )
    store.ensure_rating(b["id"])
    orch = _orch(store, human_timeout=1.0)

    async def run():
        mid = await orch.challenge_human(
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
    """run_bot_vs_human 在 Bot 崩溃时应抛 BotCrashedError（而非吞成 _fail_response）。"""
    from bzplat.backend.runtime.binary_runner import BotCrashedError

    _, _ = _setup(store)
    runner = MatchRunner(BinaryRunner(prefer_local=True))

    async def human_decide(player_idx, request):
        return {"x": 0, "y": 0}  # 不会到达（bot 先崩）

    async def run():
        return await runner.run_bot_vs_human(
            "/nonexistent/crash_bot", bot_seat=1, human_decide=human_decide,
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
        mid = await orch.challenge_human(b["id"], u["id"], human_seat=0, game_id="gomoku")
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
    复现对局 20260802132013-7eb5087b：holdem 卡 running，hands_played=0，8 手全弃牌。
    """
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    u = store.create_user("touser", "t@ex.com", hash_password("password1"))
    path = os.path.abspath("samples/callbot_linux_amd64")
    b = store.create_bot(u["id"], "holdbot", binary_path=path, format="elf", game_id="holdem")
    orch = _orch(store, human_timeout=0.3)
    orch.human_max_consecutive_timeouts = 3  # 连续 3 次超时即中止

    async def run():
        mid = await orch.challenge_human(
            b["id"], u["id"], human_seat=1, game_id="holdem", hands=70,
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
