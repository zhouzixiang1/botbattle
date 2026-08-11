"""对抗审计（PR #24/#25）补充测试：覆盖审计发现的盲区。

覆盖：
- SSE 队列 drop-oldest（满时不阻塞、丢最旧、保最新）+ maxsize=2000 边界
- 普通双 bot 对局 BotCrashedError → 技术判负（非 human 主路径）
- gomoku/pencil 引擎层传播 BotCrashedError（PR #24 治本修复在棋类引擎的回归保护）
- start_session 文件不存在 → BotCrashedError（异常类型契约）
"""
from __future__ import annotations

import asyncio
import json
import os
import secrets
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.requests import Request

from bzplat.backend.api_routes import match_events
from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.orchestrator import HumanInactive, MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import (
    BinaryRunner,
    BotCrashedError,
    PlatformRunnerError,
)
from bzplat.backend.store import Store
from bzplat.backend.store.schema import STATUS_ABORTED, STATUS_COMPLETED, STATUS_RUNNING
from bzplat.backend.tests.execution_helpers import (
    challenge_and_start,
    human_and_start,
)


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


def _fixture_binary(store: Store, name: str) -> str:
    fixture_dir = Path(store.path).resolve().parent / "bot-fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    path = fixture_dir / name
    path.write_bytes(b"test fixture")
    return str(path)


def _user_with_bot(store: Store, *, name: str, path: str, game: str = "gomoku"):
    """建一个用户 + 一个 bot；调用方必须显式提供存在的隔离 fixture。"""
    assert os.path.isfile(path), f"测试 Bot fixture 不存在: {path}"
    u = store.create_user(name, f"{name}@ex.com", hash_password("password1"))
    b = store.create_bot(
        u["id"], f"{name}_bot", binary_path=path, format="elf", game_id=game
    )
    store.ensure_rating(b["id"])
    return u, b


def _completed_rating_match(
    store: Store,
    match_id: str,
    bot_a_id: int,
    bot_b_id: int,
    owner_id: int,
    *,
    winner: int | None,
    deltas: list[int],
) -> dict:
    # These ordering tests intentionally model a restart with multiple
    # completed-but-unsettled rated rows.  Live admission now prevents creating
    # that overlap, so seed the already-terminal recovery input directly while
    # preserving the canonical policy/order/projection invariants.
    now = datetime.now().isoformat(timespec="seconds")
    result = {"deltas": deltas, "rounds_played": 1}
    match_config = {
        "_rating_eligible": True,
        "_rating_reason": "eligible",
    }
    with store._tx() as conn:
        conn.execute("BEGIN IMMEDIATE")
        projection_guard = store._rating_projection_mutation_guard_tx(conn)
        conn.execute(
            "INSERT INTO matches_gomoku("
            "id,bot_a_id,bot_b_id,owner_id,winner,reason,match_type,status,"
            "game_id,match_config,result,started_at,ended_at,created_at) "
            "VALUES(?,?,?,?,?,'completed','challenge','completed','gomoku',?,?,?,?,?)",
            (
                match_id,
                bot_a_id,
                bot_b_id,
                owner_id,
                winner,
                json.dumps(match_config, ensure_ascii=False),
                json.dumps(result, ensure_ascii=False),
                now,
                now,
                now,
            ),
        )
        conn.execute(
            "INSERT INTO matches_index(id,game_id) VALUES(?,'gomoku')",
            (match_id,),
        )
        conn.execute(
            "INSERT INTO match_rating_policies("
            "match_id,game_id,bot_a_id,bot_b_id,rated,rating_reason,source,"
            "classified_at) VALUES(?,'gomoku',?,?,1,'eligible','creation_v2',?)",
            (match_id, bot_a_id, bot_b_id, now),
        )
        store._reserve_rating_settlement_order_tx(conn, match_id)
        store._advance_rating_projection_state_tx(conn, projection_guard)
    return store.get_match(match_id)


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


def test_sse_subscribe_failure_never_registers_an_orphan_queue(
    store: Store, monkeypatch: pytest.MonkeyPatch
):
    """快照读取失败时调用方拿不到 queue，因此必须在注册前失败。"""
    orch = _orch(store)
    _, bot = _user_with_bot(
        store,
        name="sse-subscribe-fault",
        path=os.path.abspath("samples/gomokubot_linux_amd64"),
    )
    mid = _new_match_id()
    store.create_match(mid, bot["id"], bot["id"], game_id="gomoku")

    def fail_replay(*_args, **_kwargs):
        raise RuntimeError("simulated replay read failure")

    monkeypatch.setattr(store, "get_public_replay", fail_replay)
    with pytest.raises(RuntimeError, match="simulated replay read failure"):
        orch.subscribe(mid)

    assert mid not in orch._sse
    assert orch._sse_human_views == {}


def test_sse_missing_visibility_metadata_fails_closed(store: Store):
    """内部元数据缺失不得使公开订阅泄露底牌或人类请求。"""
    orch = _orch(store)
    q: asyncio.Queue = asyncio.Queue()
    mid = _new_match_id()
    orch._sse[mid] = [q]

    orch._broadcast(
        mid,
        {"type": "deal_hole", "hand": 0, "holes": [["As", "Ah"], ["Ks", "Kh"]]},
    )
    orch._broadcast(
        mid,
        {"type": "your_turn", "player": 1, "request": {"my_cards": [1, 2]}},
    )

    assert q.get_nowait() == {"type": "deal_hole", "hand": 0, "holes": [[], []]}
    assert q.empty()


def test_sse_error_boundary_keeps_only_one_stable_reason(store: Store):
    orch = _orch(store)
    _, bot = _user_with_bot(
        store,
        name="sse-error-contract",
        path=os.path.abspath("samples/gomokubot_linux_amd64"),
    )
    mid = _new_match_id()
    store.create_match(mid, bot["id"], bot["id"], game_id="gomoku")
    q = orch.subscribe(mid)
    q.get_nowait()

    orch._broadcast(
        mid,
        {
            "type": "error",
            "message": "completed /private/bot.bin",
            "reason": "unknown_private_code",
            "stderr": "secret",
        },
    )
    assert q.get_nowait() == {"type": "error", "reason": "platform_error"}


@pytest.mark.parametrize("terminal_status", [STATUS_COMPLETED, STATUS_ABORTED])
def test_sse_terminal_snapshot_closes_and_unsubscribes(store: Store, terminal_status: str):
    """详情判断 live 后、真正订阅前若对局已终结，terminal snapshot 必须收流。

    否则 StreamingResponse 会每 25 秒发送 ping 且永久保留订阅，浏览器也会自动重连。
    """
    orch = _orch(store)
    _, bot = _user_with_bot(
        store,
        name=f"sseterm{terminal_status}",
        path=os.path.abspath("samples/gomokubot_linux_amd64"),
    )
    mid = _new_match_id()
    store.create_match(mid, bot["id"], bot["id"], match_type="challenge", game_id="gomoku")
    store.update_match(mid, status=terminal_status)
    app = SimpleNamespace(state=SimpleNamespace(store=store, orch=orch))
    request = Request({"type": "http", "method": "GET", "path": f"/api/matches/{mid}/events", "headers": [], "app": app})

    async def consume() -> list[str]:
        response = await match_events(mid, request)
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(asyncio.wait_for(consume(), timeout=1.0))
    assert len(chunks) == 1
    assert '"type": "snapshot"' in chunks[0]
    assert f'"status": "{terminal_status}"' in chunks[0]
    assert mid not in orch._sse


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
    q.put_nowait({"type": "move", "move_index": 1})
    q.put_nowait({"type": "move", "move_index": 2})

    # 队列已满（2/2），广播第 3 条 → 应丢最旧（seq=1）、保最新（seq=3）
    orch._broadcast(mid, {"type": "move", "move_index": 3})

    assert q.qsize() == 2, "drop-oldest 后队列应仍为 maxsize，不应增长也不应空"
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    seqs = [e["move_index"] for e in drained]
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
        await asyncio.wait_for(
            asyncio.to_thread(
                orch._broadcast,
                mid,
                {"type": "move", "player": 0, "x": 1, "y": 2},
            ),
            timeout=2.0,
        )
        return q.get_nowait()["type"]

    result = asyncio.run(run())
    assert result == "move", "drop-oldest 后最新事件应可被取到"


def test_sse_reconnect_snapshot_uses_complete_active_prefix(store: Store):
    """运行 snapshot 不退回节流落库点，终态收尾释放内存前缀。"""

    class PausingRunner:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def run_binaries(self, *args, **kwargs):
            on_event = kwargs["on_event"]
            on_event("match_start", {"type": "match_start", "game_id": "gomoku"})
            # match_start 已落库；下面两条尚未达到每 5 条的节流点。
            on_event("turn", {"type": "turn", "player": 0})
            on_event("turn", {"type": "turn", "player": 1})
            self.started.set()
            await self.release.wait()
            return SimpleNamespace(
                rounds_played=1,
                rounds=[SimpleNamespace(deltas=[1, -1])],
                events=[],
                winner=0,
                reason="five",
            )

    runner = PausingRunner()
    orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
    owner, bot_a = _user_with_bot(
        store,
        name="activeprefixa",
        path=os.path.abspath("samples/gomokubot_linux_amd64"),
    )
    _, bot_b = _user_with_bot(
        store,
        name="activeprefixb",
        path=os.path.abspath("samples/gomokubot_linux_amd64"),
    )

    async def run() -> None:
        match_id = await challenge_and_start(
            orch,
            bot_a["id"], bot_b["id"], owner["id"], game_id="gomoku"
        )
        task = orch._tasks[match_id]
        await asyncio.wait_for(runner.started.wait(), timeout=2)

        persisted = store.get_public_replay(match_id) or {}
        assert '"turn"' not in (persisted.get("events_json") or "")

        queue = orch.subscribe(match_id)
        snapshot = queue.get_nowait()
        assert [event["type"] for event in snapshot["events"]] == [
            "match_start",
            "turn",
            "turn",
        ]
        orch._active_replay_events[match_id].append(
            {"type": "turn", "player": 0}
        )
        assert len(snapshot["events"]) == 3

        runner.release.set()
        await asyncio.wait_for(task, timeout=2)
        assert match_id not in orch._active_replay_events

    asyncio.run(run())


def test_sse_terminal_snapshot_ignores_stale_active_prefix(store: Store):
    """终态提交是快照权威边界，runner 收尾前的旧内存引用不能遮住终局。"""
    orch = _orch(store)
    owner, bot = _user_with_bot(
        store,
        name="terminalprefix",
        path=os.path.abspath("samples/gomokubot_linux_amd64"),
    )
    match_id = _new_match_id()
    store.create_match(
        match_id,
        bot["id"],
        bot["id"],
        owner_id=owner["id"],
        game_id="gomoku",
        match_type="challenge",
    )
    store.save_replay(
        match_id,
        events_json=json.dumps(
            [
                {"type": "match_start", "game_id": "gomoku"},
                {"type": "match_end", "winner": 1, "reason": "forged"},
            ]
        ),
    )
    store.update_match(
        match_id,
        status=STATUS_COMPLETED,
        winner=0,
        reason="five",
        result={"deltas": [1, -1]},
    )
    orch._active_replay_events[match_id] = [
        {"type": "match_start", "game_id": "gomoku"},
        {"type": "turn", "player": 0},
        {"type": "turn", "player": 1},
    ]

    snapshot = orch.subscribe(match_id).get_nowait()
    assert snapshot["match"]["status"] == STATUS_COMPLETED
    assert snapshot["events"] == [
        {"type": "match_start", "game_id": "gomoku"},
        {"type": "match_end", "winner": 0, "reason": "five", "deltas": [1, -1]},
    ]


# ── 普通双 bot 对局 BotCrashedError → 技术判负（主路径盲区）───────────────


def test_bot_crashed_is_technical_loss_in_normal_match(store: Store):
    """双 Bot 对局启动崩溃统一结算为技术判负，并保留明确胜者与比分。"""
    class CrashingRunner:
        async def run_binaries(self, *args, **kwargs):
            raise BotCrashedError("controlled startup crash", crashed_seat=1)

    orch = MatchOrchestrator(store, runner=CrashingRunner(), max_concurrent=2)
    ua, ba = _user_with_bot(
        store, name="goodu", path=os.path.abspath("samples/gomokubot_linux_amd64")
    )
    ub, bb = _user_with_bot(
        store, name="badu", path=os.path.abspath("samples/gomokubot_linux_amd64")
    )

    async def run():
        mid = await challenge_and_start(
            orch, ba["id"], bb["id"], ua["id"], game_id="gomoku"
        )
        task = orch._tasks.get(mid)
        if task:
            try:
                await asyncio.wait_for(task, timeout=20)
            except Exception:
                pass
        return mid

    mid = asyncio.run(run())
    m = store.get_match(mid)
    assert m["status"] == STATUS_COMPLETED
    assert m["reason"] == "technical_loss"
    assert m["winner"] == 0  # 座位 1 的 bb 崩溃，座位 0 胜
    assert m["technical_loss"] == 1
    assert m["result"]["deltas"] == [1, -1]
    # 技术判负也是 completed：非赛事对局须与正常完成走同一评分/pair_stats 契约。
    rating_a = store.get_rating(ba["id"])
    rating_b = store.get_rating(bb["id"])
    assert rating_a["matches_played"] == rating_b["matches_played"] == 1
    assert rating_a["wins"] == 1 and rating_a["losses"] == 0
    assert rating_b["wins"] == 0 and rating_b["losses"] == 1
    h2h = store.head_to_head(ba["id"], bb["id"])
    assert h2h is not None
    assert h2h["a_wins"] == 1 and h2h["a_losses"] == 0
    assert store.is_match_rating_settled(mid)


def test_rating_history_failure_rolls_back_both_ratings_and_pair_stats(store: Store):
    """双边 rating/history/pair_stats 必须同事务；中途失败不得半结算。"""
    _, ba = _user_with_bot(
        store, name="atomicra", path=_fixture_binary(store, "atomic-ra")
    )
    _, bb = _user_with_bot(
        store, name="atomicrb", path=_fixture_binary(store, "atomic-rb")
    )
    before_a = store.get_rating(ba["id"])
    before_b = store.get_rating(bb["id"])
    with store._tx() as c:
        c.execute(
            "CREATE TRIGGER fail_atomic_rating_history "
            "BEFORE INSERT ON rating_history BEGIN "
            "SELECT RAISE(ABORT, 'rating history exploded'); END"
        )

    with pytest.raises(Exception, match="rating history exploded"):
        store.apply_match_ratings_atomic(
            ba["id"],
            bb["id"],
            game_id="gomoku",
            rating_a=(1510.0, 300.0, 0.06),
            rating_b=(1490.0, 300.0, 0.06),
            winner=0,
            delta_a=1,
            delta_b=-1,
            reason="fault-injection",
            settlement_id="fault-injection-match",
        )

    after_a = store.get_rating(ba["id"])
    after_b = store.get_rating(bb["id"])
    for key in ("rating", "rd", "vol", "wins", "losses", "draws", "delta_total", "matches_played"):
        assert after_a[key] == before_a[key]
        assert after_b[key] == before_b[key]
    assert store.list_rating_history(ba["id"], game_id="gomoku") == []
    assert store.list_rating_history(bb["id"], game_id="gomoku") == []
    assert store.head_to_head(ba["id"], bb["id"]) is None
    assert not store.is_match_rating_settled("fault-injection-match")


def test_completed_rating_recovery_and_repeated_postprocess_are_exactly_once(store: Store):
    """completed 后评分失败可重启补算；重复恢复/后处理不重复任何评分副作用。"""
    owner, ba = _user_with_bot(
        store, name="recoverra", path=_fixture_binary(store, "recover-ra")
    )
    _, bb = _user_with_bot(
        store, name="recoverrb", path=_fixture_binary(store, "recover-rb")
    )
    mid = "rating-recovery-once"
    store.create_match(
        mid,
        ba["id"],
        bb["id"],
        owner_id=owner["id"],
        game_id="gomoku",
        match_type="challenge",
    )
    store.update_match(
        mid,
        status=STATUS_COMPLETED,
        winner=0,
        result={"deltas": [1, -1], "rounds_played": 1},
    )
    match = store.get_match(mid)
    with store._tx() as c:
        c.execute(
            "CREATE TRIGGER fail_recover_rating_history "
            "BEFORE INSERT ON rating_history BEGIN "
            "SELECT RAISE(ABORT, 'first postprocess exploded'); END"
        )

    first_orch = _orch(store)
    asyncio.run(
        first_orch._safe_postprocess_completed_match(match, mid, 0, 1, -1)
    )
    assert not store.is_match_rating_settled(mid)
    assert store.get_rating(ba["id"])["matches_played"] == 0
    assert store.get_rating(bb["id"])["matches_played"] == 0
    assert store.head_to_head(ba["id"], bb["id"]) is None

    with store._tx() as c:
        c.execute("DROP TRIGGER fail_recover_rating_history")

    restarted_orch = _orch(store)
    assert asyncio.run(restarted_orch.recover_unsettled_match_ratings()) == 1
    assert store.is_match_rating_settled(mid)
    rating_a = store.get_rating(ba["id"])
    rating_b = store.get_rating(bb["id"])
    assert rating_a["matches_played"] == rating_b["matches_played"] == 1
    assert rating_a["wins"] == 1 and rating_b["losses"] == 1
    assert len(store.list_rating_history(ba["id"], game_id="gomoku")) == 1
    assert len(store.list_rating_history(bb["id"], game_id="gomoku")) == 1
    h2h_once = dict(store.head_to_head(ba["id"], bb["id"]))
    snapshot = {
        "a": dict(rating_a),
        "b": dict(rating_b),
        "ha": list(store.list_rating_history(ba["id"], game_id="gomoku")),
        "hb": list(store.list_rating_history(bb["id"], game_id="gomoku")),
        "h2h": h2h_once,
    }

    # 启动恢复扫描不到 marker 已存在的对局；即便完整 postprocess 被盲重试，
    # claim 也会在 rating/history/pair_stats 和 XP 前返回。
    assert asyncio.run(restarted_orch.recover_unsettled_match_ratings()) == 0
    asyncio.run(
        restarted_orch._postprocess_completed_match(match, mid, 0, 1, -1)
    )
    assert store.get_rating(ba["id"]) == snapshot["a"]
    assert store.get_rating(bb["id"]) == snapshot["b"]
    assert store.list_rating_history(ba["id"], game_id="gomoku") == snapshot["ha"]
    assert store.list_rating_history(bb["id"], game_id="gomoku") == snapshot["hb"]
    assert store.head_to_head(ba["id"], bb["id"]) == snapshot["h2h"]
    assert store.get_user(owner["id"])["xp"] == 0  # 恢复只补评分，不补通知/XP


def test_rating_sequence_repairs_earlier_failure_before_settling_target(
    tmp_path, monkeypatch
):
    """M1 事务失败后 M2 须先补 M1，结果与正常 M1→M2 完全一致。"""
    match_1_id = "rating-order-01"
    match_2_id = "rating-order-02"

    def setup(path):
        case_store = Store(str(path))
        owner_a, bot_a = _user_with_bot(
            case_store, name="ordera", path=_fixture_binary(case_store, "order-a")
        )
        owner_b, bot_b = _user_with_bot(
            case_store, name="orderb", path=_fixture_binary(case_store, "order-b")
        )
        owner_c, bot_c = _user_with_bot(
            case_store, name="orderc", path=_fixture_binary(case_store, "order-c")
        )
        match_1 = _completed_rating_match(
            case_store,
            match_1_id,
            bot_a["id"],
            bot_b["id"],
            owner_a["id"],
            winner=0,
            deltas=[1, -1],
        )
        match_2 = _completed_rating_match(
            case_store,
            match_2_id,
            bot_a["id"],
            bot_c["id"],
            owner_a["id"],
            winner=1,
            deltas=[-1, 1],
        )
        assert [
            row["id"]
            for row in case_store.list_unsettled_completed_rating_matches()
        ] == [match_1_id, match_2_id]
        return (
            case_store,
            (owner_a, owner_b, owner_c),
            (bot_a, bot_b, bot_c),
            (match_1, match_2),
        )

    def business_snapshot(case_store, owners, bots):
        rating_fields = (
            "rating",
            "rd",
            "vol",
            "wins",
            "losses",
            "draws",
            "delta_total",
            "matches_played",
        )
        history_fields = ("rating", "rd", "vol", "matches_played", "reason")
        pair_fields = ("a_wins", "a_losses", "draws", "samples")

        def pair_snapshot(bot_a_id, bot_b_id):
            row = case_store.head_to_head(bot_a_id, bot_b_id)
            return None if row is None else tuple(row[key] for key in pair_fields)

        return {
            "ratings": [
                tuple(case_store.get_rating(bot["id"])[key] for key in rating_fields)
                for bot in bots
            ],
            "history": [
                [
                    tuple(row[key] for key in history_fields)
                    for row in case_store.list_rating_history(
                        bot["id"], game_id="gomoku"
                    )
                ]
                for bot in bots
            ],
            # last_played_at is deliberately excluded: two independent stores can
            # cross a wall-clock second while still producing identical business
            # state. The ordering contract concerns counters/ratings/history.
            "pair_ab": pair_snapshot(bots[0]["id"], bots[1]["id"]),
            "pair_ac": pair_snapshot(bots[0]["id"], bots[2]["id"]),
            "xp": [case_store.get_user(owner["id"])["xp"] for owner in owners],
        }

    normal_store, normal_owners, normal_bots, normal_matches = setup(
        tmp_path / "rating-order-normal.db"
    )
    repaired_store, repaired_owners, repaired_bots, repaired_matches = setup(
        tmp_path / "rating-order-repaired.db"
    )
    normal_orch = _orch(normal_store)
    repaired_orch = _orch(repaired_store)

    async def settle_normally():
        await normal_orch._postprocess_completed_match(
            normal_matches[0], match_1_id, 0, 1, -1
        )
        await normal_orch._postprocess_completed_match(
            normal_matches[1], match_2_id, 1, -1, 1
        )

    asyncio.run(settle_normally())
    expected = business_snapshot(normal_store, normal_owners, normal_bots)

    original_apply = repaired_store.apply_match_ratings_atomic
    attempts: list[str] = []
    failed_once = False

    def fail_first_m1(*args, **kwargs):
        nonlocal failed_once
        settlement_id = str(kwargs.get("settlement_id") or "")
        attempts.append(settlement_id)
        if settlement_id == match_1_id and not failed_once:
            failed_once = True
            raise RuntimeError("injected M1 rating failure")
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(repaired_store, "apply_match_ratings_atomic", fail_first_m1)

    async def fail_then_repair_from_m2():
        await repaired_orch._safe_postprocess_completed_match(
            repaired_matches[0], match_1_id, 0, 1, -1
        )
        assert not repaired_store.is_match_rating_settled(match_1_id)
        assert not repaired_store.is_match_rating_settled(match_2_id)

        # M2 后处理重扫全局缺口：必须 M1 成功后才能结算 M2。
        await repaired_orch._postprocess_completed_match(
            repaired_matches[1], match_2_id, 1, -1, 1
        )
        snapshot_once = business_snapshot(
            repaired_store, repaired_owners, repaired_bots
        )

        # 重复后处理/启动恢复均不得重复 rating/history/XP。
        await repaired_orch._postprocess_completed_match(
            repaired_matches[1], match_2_id, 1, -1, 1
        )
        assert await repaired_orch.recover_unsettled_match_ratings() == 0
        assert business_snapshot(
            repaired_store, repaired_owners, repaired_bots
        ) == snapshot_once

    asyncio.run(fail_then_repair_from_m2())

    assert attempts == [match_1_id, match_1_id, match_2_id]
    assert repaired_store.is_match_rating_settled(match_1_id)
    assert repaired_store.is_match_rating_settled(match_2_id)
    assert business_snapshot(
        repaired_store, repaired_owners, repaired_bots
    ) == expected
    normal_store.close()
    repaired_store.close()


def test_concurrent_rating_postprocess_is_globally_ordered_and_exactly_once(
    store: Store, monkeypatch
):
    """较晚 target 先到也须按全局顺序补算，并且每场通知/XP 只一次。"""
    from bzplat.backend.store.schema import XP_MATCH_PARTICIPATE, XP_MATCH_WIN

    owner_a, bot_a = _user_with_bot(
        store,
        name="concurrentra",
        path=_fixture_binary(store, "concurrent-ra"),
    )
    owner_b, bot_b = _user_with_bot(
        store,
        name="concurrentrb",
        path=_fixture_binary(store, "concurrent-rb"),
    )
    match_1_id = "rating-concurrent-01"
    match_2_id = "rating-concurrent-02"
    match_1 = _completed_rating_match(
        store,
        match_1_id,
        bot_a["id"],
        bot_b["id"],
        owner_a["id"],
        winner=0,
        deltas=[1, -1],
    )
    match_2 = _completed_rating_match(
        store,
        match_2_id,
        bot_a["id"],
        bot_b["id"],
        owner_a["id"],
        winner=1,
        deltas=[-1, 1],
    )
    orch = _orch(store)

    class RecordingNotifier:
        def __init__(self):
            self.links: list[str] = []
            self.titles: list[str] = []

        def notify_both_owners(self, *_args, **kwargs):
            self.links.append(str(kwargs["link"]))
            self.titles.append(str(kwargs["title"]))

    notifier = RecordingNotifier()
    orch.notifier = notifier

    apply_order: list[str] = []
    original_apply = store.apply_match_ratings_atomic

    def recording_apply(*args, **kwargs):
        apply_order.append(str(kwargs.get("settlement_id") or ""))
        return original_apply(*args, **kwargs)

    monkeypatch.setattr(store, "apply_match_ratings_atomic", recording_apply)

    active_settlers = 0
    max_active_settlers = 0
    original_settle = orch._settle_completed_match_rating

    async def yielding_settle(*args, **kwargs):
        nonlocal active_settlers, max_active_settlers
        active_settlers += 1
        max_active_settlers = max(max_active_settlers, active_settlers)
        try:
            # 强制较晚 target 持有全局锁时让出事件循环，
            # 让较早 target 的后处理真实发生锁竞争。
            await asyncio.sleep(0.01)
            return await original_settle(*args, **kwargs)
        finally:
            active_settlers -= 1

    monkeypatch.setattr(orch, "_settle_completed_match_rating", yielding_settle)

    async def settle_concurrently():
        later = asyncio.create_task(
            orch._postprocess_completed_match(match_2, match_2_id, 1, -1, 1)
        )
        await asyncio.sleep(0)
        earlier = asyncio.create_task(
            orch._postprocess_completed_match(match_1, match_1_id, 0, 1, -1)
        )
        await asyncio.gather(later, earlier)
        # 再并发重试一次，marker 必须使全部副作用幂等。
        await asyncio.gather(
            orch._postprocess_completed_match(match_2, match_2_id, 1, -1, 1),
            orch._postprocess_completed_match(match_1, match_1_id, 0, 1, -1),
        )

    asyncio.run(settle_concurrently())

    assert max_active_settlers == 1
    assert apply_order == [match_1_id, match_2_id]
    assert notifier.links == [f"/match/{match_1_id}", f"/match/{match_2_id}"]
    assert notifier.titles == ["对局完成：座位 1 胜", "对局完成：座位 2 胜"]
    assert store.get_rating(bot_a["id"])["matches_played"] == 2
    assert store.get_rating(bot_b["id"])["matches_played"] == 2
    assert len(store.list_rating_history(bot_a["id"], game_id="gomoku")) == 2
    assert len(store.list_rating_history(bot_b["id"], game_id="gomoku")) == 2
    expected_xp = 2 * XP_MATCH_PARTICIPATE + XP_MATCH_WIN
    assert store.get_user(owner_a["id"])["xp"] == expected_xp
    assert store.get_user(owner_b["id"])["xp"] == expected_xp


def test_rating_recovery_excludes_contest_and_human_but_marks_selfplay(store: Store):
    """赛事/人类不进全局评分；自博弈无评分但需 marker 令恢复收敛。"""
    owner, ba = _user_with_bot(
        store, name="semanticsa", path=_fixture_binary(store, "semantics-a")
    )
    _, bb = _user_with_bot(
        store, name="semanticsb", path=_fixture_binary(store, "semantics-b")
    )
    contest = store.create_contest("rating semantics", owner["id"], status="running")
    cases = (
        ("rating-contest", ba["id"], bb["id"], "contest", contest["id"]),
        ("rating-human", ba["id"], bb["id"], "human", None),
        ("rating-selfplay", ba["id"], ba["id"], "challenge", None),
    )
    for mid, bot_a, bot_b, match_type, contest_id in cases:
        store.create_match(
            mid,
            bot_a,
            bot_b,
            owner_id=owner["id"],
            contest_id=contest_id,
            match_type=match_type,
            game_id="gomoku",
            human_user_id=owner["id"] if match_type == "human" else None,
            human_seat=1 if match_type == "human" else None,
        )
        store.update_match(
            mid,
            status=STATUS_COMPLETED,
            winner=0,
            result={"deltas": [1, -1]},
        )

    orch = _orch(store)
    assert asyncio.run(orch.recover_unsettled_match_ratings()) == 1
    assert not store.is_match_rating_settled("rating-contest")
    assert not store.is_match_rating_settled("rating-human")
    assert store.is_match_rating_settled("rating-selfplay")
    assert store.get_rating(ba["id"])["matches_played"] == 0
    assert store.get_rating(bb["id"])["matches_played"] == 0
    assert store.list_rating_history(ba["id"], game_id="gomoku") == []
    assert store.head_to_head(ba["id"], ba["id"]) is None
    assert asyncio.run(orch.recover_unsettled_match_ratings()) == 0


def test_completed_result_survives_postprocess_exception(store: Store, monkeypatch):
    """评分/通知等后处理异常不得把已 completed 的正常业务结果覆盖为 aborted。"""
    class SuccessRunner:
        async def run_binaries(self, *args, **kwargs):
            return SimpleNamespace(
                rounds_played=1,
                rounds=[SimpleNamespace(deltas=[1, -1])],
                winner=0,
            )

    ua, ba = _user_with_bot(
        store, name="postua", path=_fixture_binary(store, "post-a")
    )
    _, bb = _user_with_bot(
        store, name="postub", path=_fixture_binary(store, "post-b")
    )
    orch = MatchOrchestrator(store, runner=SuccessRunner(), max_concurrent=1)

    async def fail_postprocess(*args, **kwargs):
        raise RuntimeError("postprocess exploded")

    monkeypatch.setattr(orch, "_postprocess_completed_match", fail_postprocess)

    async def run():
        mid = await challenge_and_start(
            orch, ba["id"], bb["id"], ua["id"], game_id="gomoku"
        )
        await asyncio.wait_for(orch._tasks[mid], timeout=1)
        return mid

    mid = asyncio.run(run())
    match = store.get_match(mid)
    assert match["status"] == STATUS_COMPLETED
    assert match["reason"] == "completed"
    assert match["winner"] == 0


def test_platform_sandbox_fault_aborts_without_technical_loss_or_rating(store: Store):
    """Docker/platform failures stay private and retry only after exact cleanup."""

    class PlatformFailingRunner:
        async def run_binaries(self, *args, **kwargs):
            raise PlatformRunnerError("docker daemon unavailable")

    owner, bot_a = _user_with_bot(
        store, name="platforma", path=_fixture_binary(store, "platform-a")
    )
    _, bot_b = _user_with_bot(
        store, name="platformb", path=_fixture_binary(store, "platform-b")
    )
    orch = MatchOrchestrator(store, runner=PlatformFailingRunner(), max_concurrent=1)

    async def run():
        mid = await challenge_and_start(
            orch,
            bot_a["id"], bot_b["id"], owner["id"], game_id="gomoku"
        )
        task = orch._tasks.get(mid)
        if task is not None:
            await task
        return mid

    mid = asyncio.run(run())
    match = store.get_match(mid)
    assert match["status"] == STATUS_RUNNING
    assert match["winner"] is None
    assert not match["technical_loss"]
    assert store.get_rating(bot_a["id"])["matches_played"] == 0
    assert store.get_rating(bot_b["id"])["matches_played"] == 0
    assert not store.is_match_rating_settled(mid)
    job = store.executions.get_by_match(mid)
    assert job and job["status"] == "running"
    assert store.executions.control()["dispatcher_state"] == "paused"

    # Simulate the dispatcher's verified zero-label recovery boundary.  An
    # event-free manual runtime failure is deleted, but remains an explicit
    # owner-retryable interruption; no synthetic public platform_error match
    # survives and no hidden automatic retry is started on the user's behalf.
    recovered = store.executions.recover_after_namespace_cleanup()
    assert recovered == {"requeued": 0, "interrupted": 1, "settling": 0}
    assert store.get_match(mid) is None
    interrupted = store.executions.get(job["public_id"])
    assert interrupted and interrupted["status"] == "interrupted"
    assert interrupted["retryable"] == 1
    assert interrupted["current_match_id"] is None


def test_final_replay_failure_preserves_terminal_bot_and_human_matches(
    store: Store, monkeypatch
):
    """终态 replay flush 失败：completed 仍评分，human aborted 原因不丢。"""
    result = SimpleNamespace(
        rounds_played=1,
        rounds=[SimpleNamespace(deltas=[1, -1])],
        winner=0,
    )

    class SuccessRunner:
        async def run_binaries(self, *args, **kwargs):
            return result

        async def run_bot_vs_human(self, *args, **kwargs):
            return result

    owner, ba = _user_with_bot(
        store, name="replayflusha", path=_fixture_binary(store, "replay-a")
    )
    _, bb = _user_with_bot(
        store, name="replayflushb", path=_fixture_binary(store, "replay-b")
    )
    orch = MatchOrchestrator(store, runner=SuccessRunner(), max_concurrent=2)
    original_upsert = store.upsert_replay

    def fail_only_after_terminal(match_id, events_json):
        match = store.get_match(match_id)
        if match and match.get("status") in (STATUS_COMPLETED, STATUS_ABORTED):
            raise RuntimeError("final replay flush exploded")
        return original_upsert(match_id, events_json)

    monkeypatch.setattr(store, "upsert_replay", fail_only_after_terminal)

    async def exercise():
        bot_mid = await challenge_and_start(
            orch,
            ba["id"], bb["id"], owner["id"], game_id="gomoku"
        )
        bot_task = orch._tasks.get(bot_mid)
        if bot_task is not None:
            await bot_task
        store.executions.finalize_ready()
        human_mid = await human_and_start(
            orch,
            ba["id"], owner["id"], human_seat=1, game_id="gomoku",
        )
        human_task = orch._tasks.get(human_mid)
        if human_task is not None:
            await human_task
        return bot_mid, human_mid

    bot_mid, human_mid = asyncio.run(exercise())
    bot_match = store.get_match(bot_mid)
    human_match = store.get_match(human_mid)
    assert bot_match["status"] == STATUS_COMPLETED
    assert human_match["status"] == STATUS_COMPLETED
    assert bot_match["winner"] == human_match["winner"] == 0
    assert store.is_match_rating_settled(bot_mid)
    assert not store.is_match_rating_settled(human_mid)
    assert store.get_rating(ba["id"])["matches_played"] == 1
    assert store.get_rating(bb["id"])["matches_played"] == 1
    # 初始 replay 写入未被吞；只有 completed 后的补强 flush 故障。
    assert store.get_replay(bot_mid) is not None
    assert store.get_replay(human_mid) is not None
    store.executions.finalize_ready()

    class AbortedHumanRunner:
        def __init__(self, exc: Exception):
            self.exc = exc

        async def run_bot_vs_human(self, *args, **kwargs):
            raise self.exc

    async def exercise_aborted_humans():
        mids = []
        for exc in (BotCrashedError("bot gone"), HumanInactive("human idle")):
            aborted_orch = MatchOrchestrator(
                store, runner=AbortedHumanRunner(exc), max_concurrent=1
            )
            mid = await human_and_start(
                aborted_orch,
                ba["id"], owner["id"], human_seat=1, game_id="gomoku"
            )
            task = aborted_orch._tasks.get(mid)
            if task is not None:
                await task
            mids.append(mid)
            store.executions.finalize_ready()
        return mids

    crashed_mid, inactive_mid = asyncio.run(exercise_aborted_humans())
    crashed = store.get_match(crashed_mid)
    inactive = store.get_match(inactive_mid)
    assert crashed["status"] == inactive["status"] == STATUS_ABORTED
    assert crashed["reason"] == "bot_crashed"
    assert inactive["reason"] == "human_inactive"
    assert store.get_replay(crashed_mid) is not None
    assert store.get_replay(inactive_mid) is not None


def test_admin_abort_cancels_blocking_runner_and_broadcasts_error(tmp_path):
    """admin abort 必须 cancel/drain owned task，稳定 aborted，且直播收到 error。"""
    from httpx import ASGITransport, AsyncClient

    from bzplat.backend.main import create_app

    async def exercise():
        app = create_app(db_path=str(tmp_path / "admin-abort.db"))
        store = app.state.store
        admin = store.create_user(
            "abort-admin", "abort-admin@example.com", hash_password("pw123456"),
            role="admin",
        )
        store.update_user(admin["id"], email_verified=1)
        owner_a, bot_a = _user_with_bot(
            store, name="abort-a", path=_fixture_binary(store, "abort-a")
        )
        _, bot_b = _user_with_bot(
            store, name="abort-b", path=_fixture_binary(store, "abort-b")
        )
        _, admin_token = app.state.auth.authenticate("abort-admin", "pw123456")

        class BlockingRunner:
            def __init__(self) -> None:
                self.entered = asyncio.Event()

            async def run_binaries(self, *args, **kwargs):
                self.entered.set()
                await asyncio.Future()

        runner = BlockingRunner()
        app.state.orch.runner = runner
        match_id = await challenge_and_start(
            app.state.orch,
            bot_a["id"], bot_b["id"], owner_a["id"], game_id="gomoku"
        )
        await asyncio.wait_for(runner.entered.wait(), timeout=1)
        queue = app.state.orch.subscribe(match_id)
        queue.get_nowait()  # snapshot

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            # Admin must not forge lifecycle states while the owned runner task is
            # still alive; doing so would let the runner overwrite the fake result.
            for forbidden in ("pending", "completed"):
                rejected = await client.patch(
                    f"/api/admin/matches/{match_id}",
                    json={"status": forbidden},
                    headers={"Authorization": f"Bearer {admin_token}"},
                )
                assert rejected.status_code == 409, rejected.text
                assert store.get_match(match_id)["status"] == STATUS_RUNNING

            injected = await client.patch(
                f"/api/admin/matches/{match_id}",
                json={"status": "aborted", "reason": "admin_test_abort"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )
            assert injected.status_code == 422, injected.text
            assert store.get_match(match_id)["status"] == STATUS_RUNNING

            response = await client.patch(
                f"/api/admin/matches/{match_id}",
                json={"status": "aborted"},
                headers={"Authorization": f"Bearer {admin_token}"},
            )

        assert response.status_code == 200, response.text
        match = store.get_match(match_id)
        assert match["status"] == STATUS_ABORTED
        assert match["reason"] == "admin_aborted"
        assert match_id not in app.state.orch._tasks
        event = queue.get_nowait()
        assert event == {"type": "error", "reason": "admin_aborted"}
        replay = json.loads(store.get_public_replay(match_id)["events_json"])
        assert replay[-1] == event
        assert store.get_rating(bot_a["id"])["matches_played"] == 0
        assert store.get_rating(bot_b["id"])["matches_played"] == 0

    asyncio.run(exercise())


def test_admin_abort_store_failure_keeps_owned_task_and_subscribers(
    store: Store, monkeypatch,
):
    """A failed terminal DB commit must not strand a cancelled running match."""
    owner, bot_a = _user_with_bot(
        store, name="abort-store-a", path=_fixture_binary(store, "abort-store-a")
    )
    _, bot_b = _user_with_bot(
        store, name="abort-store-b", path=_fixture_binary(store, "abort-store-b")
    )

    class BlockingRunner:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def run_binaries(self, *args, **kwargs):
            self.entered.set()
            await asyncio.Future()

    async def exercise():
        runner = BlockingRunner()
        orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
        match_id = await challenge_and_start(
            orch,
            bot_a["id"], bot_b["id"], owner["id"], game_id="gomoku"
        )
        await asyncio.wait_for(runner.entered.wait(), timeout=1)
        task = orch._tasks[match_id]
        queue = orch.subscribe(match_id)
        queue.get_nowait()
        original_abort = store.abort_match_if_active

        def fail_abort(*args, **kwargs):
            raise RuntimeError("simulated sqlite failure")

        monkeypatch.setattr(store, "abort_match_if_active", fail_abort)
        with pytest.raises(RuntimeError, match="simulated sqlite failure"):
            await orch.abort_match(match_id)

        assert store.get_match(match_id)["status"] == STATUS_RUNNING
        assert orch._tasks.get(match_id) is task
        assert not task.done()
        assert queue in orch._sse.get(match_id, [])
        assert match_id not in orch._admin_aborting

        # Restore the Store method and use the supported path to release the
        # fixture. The same subscriber must receive the authoritative terminal.
        monkeypatch.setattr(store, "abort_match_if_active", original_abort)
        await orch.abort_match(match_id)
        assert queue.get_nowait() == {
            "type": "error",
            "reason": "admin_aborted",
        }

    asyncio.run(exercise())


def test_admin_abort_replay_read_failure_still_hands_off_terminal_state(
    store: Store, monkeypatch,
):
    """A post-commit replay fault cannot strand SSE or contest progression."""
    owner, bot_a = _user_with_bot(
        store, name="abort-replay-a", path=_fixture_binary(store, "abort-replay-a")
    )
    _, bot_b = _user_with_bot(
        store, name="abort-replay-b", path=_fixture_binary(store, "abort-replay-b")
    )

    class BlockingRunner:
        def __init__(self) -> None:
            self.entered = asyncio.Event()

        async def run_binaries(self, *args, **kwargs):
            self.entered.set()
            await asyncio.Future()

    async def exercise():
        runner = BlockingRunner()
        handoffs: list[tuple[str, int | None]] = []

        async def on_match_done(match_id: str, contest_id: int | None):
            handoffs.append((match_id, contest_id))

        orch = MatchOrchestrator(store, runner=runner, max_concurrent=1)
        orch.on_match_done = on_match_done
        match_id = await challenge_and_start(
            orch,
            bot_a["id"], bot_b["id"], owner["id"], game_id="gomoku"
        )
        await asyncio.wait_for(runner.entered.wait(), timeout=1)
        queue = orch.subscribe(match_id)
        queue.get_nowait()
        prior_event = {
            "type": "move",
            "player": 0,
            "x": 7,
            "y": 7,
            "move_index": 1,
        }
        store.upsert_replay(match_id, json.dumps([prior_event]))
        original_get_replay = store.get_replay

        def fail_get_replay(*_args, **_kwargs):
            raise RuntimeError("simulated post-commit replay read failure")

        monkeypatch.setattr(store, "get_replay", fail_get_replay)
        updated = await orch.abort_match(match_id)

        assert updated["status"] == STATUS_ABORTED
        assert updated["reason"] == "admin_aborted"
        assert queue.get_nowait() == {
            "type": "error",
            "reason": "admin_aborted",
        }
        assert handoffs == [(match_id, None)]
        assert match_id not in orch._tasks
        assert match_id not in orch._sse
        assert match_id not in orch._admin_aborting
        assert match_id not in orch._admin_abort_handoffs
        raw_replay = json.loads(original_get_replay(match_id)["events_json"])
        assert raw_replay == [prior_event]
        public_replay = json.loads(store.get_public_replay(match_id)["events_json"])
        assert public_replay == [
            prior_event,
            {"type": "error", "reason": "admin_aborted"},
        ]

    asyncio.run(exercise())


def test_midmatch_crash_reason_is_preserved_for_bot_and_human_matches(store: Store):
    """Engine-adjudicated crashes are scored/completed but remain diagnosable."""
    owner_a, bot_a = _user_with_bot(
        store, name="midcrash-a", path=_fixture_binary(store, "midcrash-a")
    )
    _, bot_b = _user_with_bot(
        store, name="midcrash-b", path=_fixture_binary(store, "midcrash-b")
    )

    result = SimpleNamespace(
        rounds=[SimpleNamespace(deltas=[-1, 1])],
        rounds_played=1,
        winner=1,
        reason="crash",
        events=[{"type": "match_end", "winner": 1, "reason": "crash"}],
    )

    class AdjudicatedCrashRunner:
        async def run_binaries(self, *args, **kwargs):
            return result

        async def run_bot_vs_human(self, *args, **kwargs):
            return result

    orch = MatchOrchestrator(store, runner=AdjudicatedCrashRunner(), max_concurrent=2)

    async def exercise():
        bot_mid = await challenge_and_start(
            orch,
            bot_a["id"], bot_b["id"], owner_a["id"], game_id="gomoku"
        )
        bot_task = orch._tasks.get(bot_mid)
        if bot_task is not None:
            await bot_task
        store.executions.finalize_ready()
        human_mid = await human_and_start(
            orch,
            bot_a["id"], owner_a["id"], human_seat=1, game_id="gomoku"
        )
        human_task = orch._tasks.get(human_mid)
        if human_task is not None:
            await human_task
        return bot_mid, human_mid

    bot_mid, human_mid = asyncio.run(exercise())
    for match_id in (bot_mid, human_mid):
        match = store.get_match(match_id)
        assert match["status"] == STATUS_COMPLETED
        assert match["reason"] == "crash"


def test_orchestrator_shutdown_cancels_and_drains_owned_tasks(store: Store):
    """服务退出先收敛编排器任务，不把清理留给事件循环全局取消。"""
    orch = _orch(store)

    async def run():
        started = asyncio.Event()

        async def pending_match():
            started.set()
            await asyncio.Future()

        task = asyncio.create_task(pending_match(), name="match-shutdown-regression")
        orch._tasks["shutdown-regression"] = task
        orch._human_active_users.add(42)
        orch._human_turns[("shutdown-regression", 1)] = {"future": asyncio.Future()}
        orch._sse["shutdown-regression"] = [asyncio.Queue()]
        await started.wait()

        await asyncio.wait_for(orch.shutdown(), timeout=1)
        assert task.cancelled()
        assert not orch._tasks
        assert not orch._human_turns
        assert not orch._human_active_users
        assert not orch._sse

    asyncio.run(run())


# ── 赛事对局崩溃判责（typed crashed_seat 应判崩溃方输，非固定座位）──


def test_contest_crash_blames_correct_seat_bot_b(store: Store):
    """赛事对局收到 crashed_seat=1 → 技术判负 winner=0（bot_a 赢）。"""
    from bzplat.backend.store.schema import (
        CONTEST_RUNNING,

        TYPE_CONTEST,
    )

    class CrashingRunner:
        async def run_binaries(self, *args, **kwargs):
            raise BotCrashedError("controlled startup crash", crashed_seat=1)

    orch = MatchOrchestrator(store, runner=CrashingRunner(), max_concurrent=2)
    ua, ba = _user_with_bot(
        store, name="goodu2", path=os.path.abspath("samples/gomokubot_linux_amd64")
    )
    ub, bb = _user_with_bot(
        store, name="badu2", path=os.path.abspath("samples/gomokubot_linux_amd64")
    )

    # 建一个 running 赛事 + 报名
    cid = store.create_contest(
        "t", ua["id"], game_id="gomoku", template_id="gomoku_rr",
    )["id"]
    store.update_contest(cid, status=CONTEST_RUNNING)
    store.add_contest_entry(cid, ua["id"], ba["id"])
    store.add_contest_entry(cid, ub["id"], bb["id"])
    mid = _new_match_id()
    store.create_match(
        mid, ba["id"], bb["id"], owner_id=ua["id"], contest_id=cid,
        game_id="gomoku", match_type=TYPE_CONTEST,
    )

    async def run():
        task = orch._run_match(mid)
        try:
            await asyncio.wait_for(task, timeout=20)
        except Exception:
            pass

    asyncio.run(run())
    m = store.get_match(mid)
    assert m["status"] == "completed", f"expected completed, got {m['status']}"
    assert m["reason"] == "technical_loss"
    # bot_b 崩溃 → winner=0（bot_a 赢）
    assert m["winner"] == 0, f"bot_b 崩溃应判 winner=0，实际 {m['winner']}"
    assert m["technical_loss"] == 1


def test_contest_crash_blames_correct_seat_bot_a(store: Store):
    """赛事对局收到 crashed_seat=0 → 技术判负 winner=1（bot_b 赢）。"""
    from bzplat.backend.store.schema import (
        CONTEST_RUNNING,

        TYPE_CONTEST,
    )

    class CrashingRunner:
        async def run_binaries(self, *args, **kwargs):
            raise BotCrashedError("controlled startup crash", crashed_seat=0)

    orch = MatchOrchestrator(store, runner=CrashingRunner(), max_concurrent=2)
    ua, ba = _user_with_bot(
        store, name="badu3", path=os.path.abspath("samples/gomokubot_linux_amd64")
    )
    ub, bb = _user_with_bot(
        store, name="goodu3", path=os.path.abspath("samples/gomokubot_linux_amd64")
    )

    cid = store.create_contest(
        "t2", ub["id"], game_id="gomoku", template_id="gomoku_rr",
    )["id"]
    store.update_contest(cid, status=CONTEST_RUNNING)
    store.add_contest_entry(cid, ua["id"], ba["id"])
    store.add_contest_entry(cid, ub["id"], bb["id"])
    mid = _new_match_id()
    store.create_match(
        mid, ba["id"], bb["id"], owner_id=ub["id"], contest_id=cid,
        game_id="gomoku", match_type=TYPE_CONTEST,
    )

    async def run():
        task = orch._run_match(mid)
        try:
            await asyncio.wait_for(task, timeout=20)
        except Exception:
            pass

    asyncio.run(run())
    m = store.get_match(mid)
    assert m["status"] == "completed", f"expected completed, got {m['status']}"
    assert m["reason"] == "technical_loss"
    # bot_a 崩溃 → winner=1（bot_b 赢）
    assert m["winner"] == 1, f"bot_a 崩溃应判 winner=1，实际 {m['winner']}"


# ── gomoku 引擎层传播 BotCrashedError（审计 P0-1，棋类引擎吞异常回归保护）─────


def test_gomoku_engine_crash_judges_defeat():
    """gomoku 引擎：BotCrashedError 对齐裁判→判负（对手赢），不中止整场向上抛。

    原审计要求传播（防被 except Exception 吞成默认动作死磕）；后续对齐权威裁判时
    改为崩溃=判负（与 pencil 一致），故此处断言判负而非抛错。
    """
    from bzplat.backend.games.gomoku.engine import GomokuSession

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
    from bzplat.backend.games.pencil.engine import PencilSession

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
    from bzplat.backend.games.gomoku.engine import GomokuSession

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
    from bzplat.backend.games.holdem.engine import MatchSession

    sess = MatchSession(num_hands=10)

    def crashing_decide(player_idx, request):
        raise BotCrashedError("simulated holdem bot crash")

    result = asyncio.run(sess.run_async(crashing_decide))
    # Botzone 计分：崩溃方判负 → 本手全筹码（STARTING_STACK）输给对手，net 体现为
    # 崩溃方 -STARTING_STACK、对手 +STARTING_STACK（final_chips = 累计净输赢）
    assert result.final_chips[1] > result.final_chips[0]
    assert result.final_chips[0] == -20000  # 崩溃方净输 20000
    assert result.final_chips[1] == 20000   # 对手净赢 20000


# ── start_session 文件不存在 → BotCrashedError（异常类型契约）─────────────────


def test_start_session_missing_file_raises_bot_crashed():
    """start_session 对不存在的二进制应抛 BotCrashedError（而非 FileNotFoundError），
    依赖方按 BotCrashedError 走 abort 分支（审计 P1-2）。"""
    runner = BinaryRunner(prefer_local=True)

    async def run():
        return await runner.start_session("/nonexistent/definitely_missing_bot")

    with pytest.raises(BotCrashedError):
        asyncio.run(run())
