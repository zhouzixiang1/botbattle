"""对抗审计（PR #24/#25）补充测试：覆盖审计发现的盲区。

覆盖：
- SSE 队列 drop-oldest（满时不阻塞、丢最旧、保最新）+ maxsize=2000 边界
- 普通双 bot 对局 BotCrashedError → abort（非 human 主路径，原仅测了 human 半边）
- gomoku/pencil 引擎层传播 BotCrashedError（PR #24 治本修复在棋类引擎的回归保护）
- start_session 文件不存在 → BotCrashedError（异常类型契约）
"""
from __future__ import annotations

import asyncio
import os
import secrets
from datetime import datetime

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner, BotCrashedError
from bzplat.backend.store import Store


def _new_match_id() -> str:
    return datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "a.db"))


def _orch(store: Store) -> MatchOrchestrator:
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    return MatchOrchestrator(
        store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=2
    )


def _user_with_bot(store: Store, *, name: str, path: str, game: str = "gomoku"):
    """建一个用户 + 一个 bot（path 可指向不存在文件以模拟崩溃）。"""
    u = store.create_user(name, f"{name}@ex.com", hash_password("password1"))
    b = store.create_bot(
        u["id"], f"{name}_bot", binary_path=path, format="elf", is_public=1, game_id=game
    )
    store.ensure_rating(b["id"])
    return u, b


# ── SSE 队列 drop-oldest + maxsize（审计 P0 测试盲区）──────────────────────


def test_sse_subscribe_maxsize_is_2000(store: Store):
    """subscribe 返回的队列 maxsize=2000（PR #25 的 500→2000，防回退）。"""
    orch = _orch(store)
    # 先建一个 match 记录，subscribe 内会读 store.get_match
    u, b = _user_with_bot(store, name="sseu", path=os.path.abspath("samples/gomokubot_linux_amd64"))
    mid = _new_match_id()
    store.create_match(mid, b["id"], b["id"], match_type="challenge", game_id="gomoku")
    q = orch.subscribe(mid)
    assert q.maxsize == 2000
    # snapshot 事件已入队
    assert q.qsize() == 1
    assert q.get_nowait()["type"] == "snapshot"


def test_sse_broadcast_drops_oldest_when_full(store: Store):
    """队列满时 _broadcast 丢最旧、保最新、不阻塞（审计 P0-1）。"""
    orch = _orch(store)
    u, b = _user_with_bot(store, name="sseu2", path=os.path.abspath("samples/gomokubot_linux_amd64"))
    mid = _new_match_id()
    store.create_match(mid, b["id"], b["id"], match_type="challenge", game_id="gomoku")

    # 用一个小 maxsize 队列模拟「满」状态，验证 drop-oldest 行为本身
    # （_broadcast 对任意 asyncio.Queue 生效，与 subscribe 的 maxsize 解耦）
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    orch._sse[mid] = [q]
    q.put_nowait({"type": "e", "seq": 1})
    q.put_nowait({"type": "e", "seq": 2})

    # 队列已满（2/2），广播第 3 条 → 应丢最旧（seq=1）、保最新（seq=3）
    orch._broadcast(mid, {"type": "e", "seq": 3})

    assert q.qsize() == 2, "drop-oldest 后队列应仍为 maxsize，不应增长也不应空"
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    seqs = [e["seq"] for e in drained]
    assert seqs == [2, 3], f"应丢最旧(seq=1)保最新(seq=3)，实际 {seqs}"
    # 最旧的 seq=1 被丢弃
    assert 1 not in seqs


def test_sse_broadcast_does_not_block_on_full_queue(store: Store):
    """_broadcast 是同步函数，满队列时必须在毫秒级返回（审计 P1-3，防阻塞事件循环）。"""
    orch = _orch(store)
    u, b = _user_with_bot(store, name="sseu3", path=os.path.abspath("samples/gomokubot_linux_amd64"))
    mid = _new_match_id()
    store.create_match(mid, b["id"], b["id"], match_type="challenge", game_id="gomoku")

    async def run():
        q: asyncio.Queue = asyncio.Queue(maxsize=1)
        orch._sse[mid] = [q]
        q.put_nowait({"type": "full"})
        # 满队列下广播——若误用阻塞 put 会卡住，wait_for 必须秒级返回
        await asyncio.wait_for(asyncio.to_thread(orch._broadcast, mid, {"type": "new"}), timeout=2.0)
        return q.get_nowait()["type"]

    result = asyncio.run(run())
    assert result == "new", "drop-oldest 后最新事件应可被取到"


# ── 普通双 bot 对局 BotCrashedError → abort（审计 P1-1，主路径盲区）──────────


def test_bot_crashed_aborts_normal_match(store: Store):
    """双 bot 对局（非 human）：一方 bot 崩溃 → _run_match 捕获 BotCrashedError
    → status=aborted + 广播 error。PR #24 原仅测了 human 半边，此为主对局路径。"""
    orch = _orch(store)
    ua, ba = _user_with_bot(
        store, name="goodu", path=os.path.abspath("samples/gomokubot_linux_amd64")
    )
    ub, bb = _user_with_bot(store, name="badu", path="/nonexistent/crash_bot")

    async def run():
        mid = await orch.challenge(ba["id"], bb["id"], ua["id"], game_id="gomoku")
        task = orch._tasks.get(mid)
        if task:
            try:
                await asyncio.wait_for(task, timeout=20)
            except Exception:
                pass
        return mid

    mid = asyncio.run(run())
    m = store.get_match(mid)
    assert m["status"] == "aborted", f"expected aborted, got {m['status']} ({m.get('reason')})"
    assert m["reason"] == "bot_crashed"


# ── gomoku 引擎层传播 BotCrashedError（审计 P0-1，棋类引擎吞异常回归保护）─────


def test_gomoku_engine_crash_judges_defeat():
    """gomoku 引擎：BotCrashedError 对齐裁判→判负（对手赢），不中止整场向上抛。

    原审计要求传播（防被 except Exception 吞成默认动作死磕）；后续对齐权威裁判时
    改为崩溃=判负（与 pencil 一致），故此处断言判负而非抛错。
    """
    from bzplat.backend.engine.gomoku import GomokuSession

    sess = GomokuSession()

    def crashing_decide(player_idx, request):
        raise BotCrashedError("simulated bot crash in gomoku decide")

    result = asyncio.run(sess.run_async(crashing_decide))
    # 崩溃方（seat 0，黑方先手第一手就崩）判负 → winner=1（白），scores=[0,1]
    assert result.winner == 1
    assert result.reason == "crash"


def test_pencil_engine_crash_judges_defeat():
    """pencil 引擎：BotCrashedError 对齐裁判→判负 2-0（不再中止整场向上抛）。

    原审计要求 BotCrashedError 传播（防被 except Exception 吞成默认动作死磕）；
    后续对齐权威裁判时改为崩溃=判负 2-0（与超时一致），故此处断言判负而非抛错。
    """
    from bzplat.backend.engine.pencil import PencilSession

    sess = PencilSession()

    def crashing_decide(player_idx, request):
        raise BotCrashedError("simulated bot crash in pencil decide")

    result = asyncio.run(sess.run_async(crashing_decide))
    # 崩溃方（seat 0，红方先手第一手就崩）判负 → winner=1（蓝），scores 归一化 0-2
    assert result.winner == 1
    assert result.reason == "crash"
    assert result.scores == [0, 2]  # 归一化 2-0（对手蓝方 2 分）


def test_board_engine_still_treats_illegal_move_as_error():
    """回归保护：普通落子错误（非崩溃）仍应判对手赢（reason=error），
    不能因为加了 BotCrashedError 传播就把所有异常都向上抛。"""
    from bzplat.backend.engine.gomoku import GomokuSession

    sess = GomokuSession()

    def illegal_decide(player_idx, request):
        # 抛一个非 BotCrashedError 的普通异常（如解析失败）
        raise ValueError("bad response")

    result = asyncio.run(sess.run_async(illegal_decide))
    # 普通异常 → 判对手赢（非平局、非正常完成）
    assert result.winner is not None
    assert result.reason == "error"


def test_holdem_engine_crash_judges_defeat():
    """holdem 引擎：BotCrashedError 对齐裁判→判负（对手赢全部筹码），不中止整场。

    三游戏统一：崩溃=判负（pencil 2-0 / gomoku 对手赢 / holdem 对手赢全部筹码）。
    """
    from bzplat.backend.engine.game import MatchSession

    sess = MatchSession(num_hands=10)

    def crashing_decide(player_idx, request):
        raise BotCrashedError("simulated holdem bot crash")

    result = asyncio.run(sess.run_async(crashing_decide))
    # 崩溃方（第一手轮到 seat 0 决策时崩）判负 → 对手 seat 1 赢全部筹码
    assert result.final_chips[1] > result.final_chips[0]
    assert result.final_chips[0] == 0  # 崩溃方筹码清零


# ── start_session 文件不存在 → BotCrashedError（异常类型契约）─────────────────


def test_start_session_missing_file_raises_bot_crashed():
    """start_session 对不存在的二进制应抛 BotCrashedError（而非 FileNotFoundError），
    依赖方按 BotCrashedError 走 abort 分支（审计 P1-2）。"""
    runner = BinaryRunner(prefer_local=True)

    async def run():
        return await runner.start_session("/nonexistent/definitely_missing_bot")

    with pytest.raises(BotCrashedError):
        asyncio.run(run())
