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
        u["id"], "mybot", binary_path=path, format="elf", is_public=1, game_id=game
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
