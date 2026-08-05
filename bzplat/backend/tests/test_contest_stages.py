"""赛事阶段引擎与休息换 Bot 测试。"""
from __future__ import annotations

import asyncio
import json

import pytest

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.stages import (
    generate_stage_pairings,
    group_round_robin,
    round_robin,
    single_elimination,
    swiss_pairings,
)
from bzplat.backend.contests.templates import resolve_stages
from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store
from bzplat.backend.store.schema import CONTEST_REST, STATUS_COMPLETED


def test_round_robin_and_double():
    bots = [1, 2, 3, 4]
    rr = round_robin(bots)
    assert len(rr) == 6
    drr = round_robin(bots, double=True)
    assert len(drr) == 12


def test_group_drr():
    bots = list(range(1, 9))
    specs = group_round_robin(bots, group_count=2, double=True)
    assert all(s.group_id for s in specs)
    # 每组 4 人双循环 = 12，两组 = 24
    assert len(specs) == 24


def test_swiss_and_ko():
    bots = [10, 20, 30, 40]
    swiss = swiss_pairings(bots, scores={10: 3, 20: 3, 30: 1, 40: 0})
    assert len(swiss) == 2
    ko = single_elimination(bots)
    assert len(ko) == 2  # 4 人首轮 2 场


def test_full_rr_guard_and_templates():
    tid, gid, stages = resolve_stages("holdem_swiss_ko")
    assert gid == "holdem"
    assert stages[0]["type"] == "swiss"
    tid2, gid2, st2 = resolve_stages("pencil_group_drr_ko")
    assert gid2 == "pencil"
    assert st2[0]["type"] == "group_double_round_robin"


def test_generate_rejects_unknown():
    with pytest.raises(ValueError):
        generate_stage_pairings({"type": "nope"}, [1, 2])


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "c.db"))


def _mk_bots(store: Store, n: int = 4):
    users = []
    bots = []
    for i in range(n):
        u = store.create_user(f"user{i}", f"u{i}@ex.com", hash_password("password1"))
        users.append(u)
        b = store.create_bot(
            u["id"],
            f"bot{i}",
            binary_path=f"/tmp/fake{i}",
            format="elf",
            is_active=1,
        )
        bots.append(b)
    return users, bots


def test_dispatch_does_not_touch_running_pairings(store: Store):
    users, bots = _mk_bots(store, 2)
    org = users[0]
    c = store.create_contest(
        "t",
        org["id"],
        game_id="holdem",
        template_id="holdem_rr",
        stages_json=json.dumps(
            [{"key": "rr", "type": "round_robin", "allow_bot_swap_in_rest": True}]
        ),
    )
    store.update_contest(c["id"], status="rest", rest_ends_at="2099-01-01T00:00:00")
    store.add_contest_entry(c["id"], users[0]["id"], bots[0]["id"])
    store.add_contest_entry(c["id"], users[1]["id"], bots[1]["id"])
    store.create_match(
        "m1",
        bots[0]["id"],
        bots[1]["id"],
        owner_id=org["id"],
        contest_id=c["id"],
        match_type="contest",
    )
    p_run = store.add_contest_pairing(
        c["id"], bots[0]["id"], bots[1]["id"], status="running", match_id="m1"
    )
    p_pend = store.add_contest_pairing(
        c["id"], bots[0]["id"], bots[1]["id"], status="pending"
    )
    b_new = store.create_bot(
        users[0]["id"],
        "bot0b",
        binary_path="/tmp/fake0b",
        format="elf",
        is_active=1,
    )
    orch = MatchOrchestrator(
        store,
        runner=MatchRunner(BinaryRunner(prefer_local=True)),
        max_concurrent=1,
    )
    mgr = ContestManager(store, orch)
    mgr.dispatch(c["id"], users[0]["id"], b_new["id"])

    rows = {p["id"]: p for p in store.list_contest_pairings(c["id"])}
    assert rows[p_run["id"]]["bot_a_id"] == bots[0]["id"]
    assert rows[p_run["id"]]["status"] == "running"
    assert rows[p_pend["id"]]["bot_a_id"] == b_new["id"]


def test_swiss_ko_smoke_pairings(store: Store):
    """小规模 Swiss→KO：生成阶段对阵数量合理。"""
    users, bots = _mk_bots(store, 4)
    org = users[0]
    tid, gid, stages = resolve_stages("holdem_swiss_ko")
    # 缩短：swiss 1 轮，advance 2，然后 KO
    stages[0]["rounds"] = 1
    stages[0]["advance_count"] = 2
    stages[0]["rest_after_minutes"] = 0
    c = store.create_contest(
        "swissko",
        org["id"],
        game_id=gid,
        template_id=tid,
        stages_json=json.dumps(stages),
        hands_per_match=1,
    )
    for u, b in zip(users, bots):
        store.add_contest_entry(c["id"], u["id"], b["id"])
    store.update_contest(c["id"], status="open")

    # 不真正跑 bot：只测 begin_stage 生成
    class FakeOrch:
        def __init__(self) -> None:
            self.n = 0

        async def challenge(
            self, a, b, owner_user_id, *, hands=1, contest_id=None, **k
        ):
            self.n += 1
            mid = f"fake-match-{self.n}"
            store.create_match(
                mid,
                a,
                b,
                owner_id=owner_user_id,
                contest_id=contest_id,
                total_hands=hands,
                match_type="contest",
            )
            return mid

    mgr = ContestManager(store, FakeOrch())  # type: ignore

    async def run():
        store.update_contest(c["id"], status="running", current_stage_idx=0)
        await mgr._begin_stage(c["id"], 0)
        pairs = store.list_contest_pairings(c["id"], stage_idx=0)
        assert len(pairs) == 2

        for p in pairs:
            mid = p["match_id"]
            assert mid
            store.update_match(
                mid,
                status=STATUS_COMPLETED,
                winner=0,
                earnings_a=100,
                earnings_b=-100,
            )
            store.update_contest_pairing(p["id"], status="completed")

        await mgr.maybe_finish(c["id"])
        c2 = store.get_contest(c["id"])
        assert c2["current_stage_idx"] == 1 or c2["status"] in ("running", "finished")
        ko_pairs = store.list_contest_pairings(c["id"], stage_idx=1)
        assert len(ko_pairs) >= 1

    asyncio.run(run())


def test_group_drr_ko_estimate():
    specs = generate_stage_pairings(
        {"type": "group_double_round_robin", "group_count": 2},
        list(range(8)),
    )
    assert len(specs) == 24
    ko = generate_stage_pairings({"type": "single_elimination"}, [1, 2, 3, 4])
    assert len(ko) == 2


def test_start_rejects_unregistered_engine(store: Store):
    users, bots = _mk_bots(store, 2)
    c = store.create_contest(
        "g",
        users[0]["id"],
        game_id="unknown_game",
        template_id="gomoku_group_drr_ko",
        stages_json=json.dumps(
            [{"key": "g", "type": "group_double_round_robin", "group_count": 1}]
        ),
    )
    for u, b in zip(users, bots):
        store.add_contest_entry(c["id"], u["id"], b["id"])
    store.update_contest(c["id"], status="open")
    mgr = ContestManager(store, MatchOrchestrator(store, max_concurrent=1))

    async def run():
        with pytest.raises(ValueError, match="未注册"):
            await mgr.start(c["id"])

    asyncio.run(run())


def test_gomoku_engine_registered(store: Store):
    """gomoku 已注册：start 不再因引擎拒绝（缺人会走后续逻辑）。"""
    from bzplat.backend.store.schema import REGISTERED_ENGINES

    assert "gomoku" in REGISTERED_ENGINES
    assert "pencil" in REGISTERED_ENGINES


def test_full_rr_rejects_large_n(store: Store):
    users, bots = _mk_bots(store, 4)
    store.set_setting("full_rr_max_n", "2")
    stages = [{"key": "rr", "type": "round_robin"}]
    c = store.create_contest(
        "big",
        users[0]["id"],
        game_id="holdem",
        template_id="holdem_rr",
        stages_json=json.dumps(stages),
    )
    for u, b in zip(users, bots):
        store.add_contest_entry(c["id"], u["id"], b["id"])
    store.update_contest(c["id"], status="open")
    mgr = ContestManager(store, MatchOrchestrator(store, max_concurrent=1))

    async def run():
        with pytest.raises(ValueError, match="上限"):
            await mgr.start(c["id"])

    asyncio.run(run())


def test_match_config_hands_dispatched_for_holdem(store: Store):
    """holdem 比赛：match_config.hands 透传到 challenge。"""
    users, bots = _mk_bots(store, 2)
    org = users[0]
    c = store.create_contest(
        "hcup", org["id"], game_id="holdem", template_id="holdem_rr",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin"}]),
        match_config_json=json.dumps({"hands": 20}),
    )
    for u, b in zip(users, bots):
        store.add_contest_entry(c["id"], u["id"], b["id"])
    # 手工生成一条 pending 配对（等价 _begin_stage 的产物）
    store.add_contest_pairing(c["id"], bots[0]["id"], bots[1]["id"], status="pending")
    store.update_contest(c["id"], status="running", current_stage_idx=0)

    seen = {}

    class FakeOrch:
        async def challenge(self, a, b, owner_user_id, *, hands=70, game_id=None,
                            n_dots=None, **k):
            seen["hands"] = hands
            seen["n_dots"] = n_dots
            seen["game_id"] = game_id
            store.create_match("m1", a, b, owner_id=owner_user_id, contest_id=c["id"],
                               total_hands=hands, match_type="contest", game_id=game_id)
            return "m1"

    mgr = ContestManager(store, FakeOrch())  # type: ignore

    async def run():
        await mgr._dispatch_pending(c["id"], 0)

    asyncio.run(run())
    assert seen["hands"] == 20
    assert seen["n_dots"] is None
    assert seen["game_id"] == "holdem"


def test_match_config_n_dots_dispatched_for_pencil(store: Store):
    """pencil 比赛：match_config.n_dots 透传到 challenge。"""
    users, bots = _mk_bots(store, 2)
    org = users[0]
    c = store.create_contest(
        "pcup", org["id"], game_id="pencil", template_id="pencil_swiss_ko",
        stages_json=json.dumps([{"key": "rr", "type": "round_robin", "scoring": "ccgc_2_1_0"}]),
        match_config_json=json.dumps({"n_dots": 9}),
    )
    for u, b in zip(users, bots):
        store.add_contest_entry(c["id"], u["id"], b["id"])
    store.add_contest_pairing(c["id"], bots[0]["id"], bots[1]["id"], status="pending")
    store.update_contest(c["id"], status="running", current_stage_idx=0)

    seen = {}

    class FakeOrch:
        async def challenge(self, a, b, owner_user_id, *, hands=70, game_id=None,
                            n_dots=None, **k):
            seen["hands"] = hands
            seen["n_dots"] = n_dots
            seen["game_id"] = game_id
            store.create_match("m1", a, b, owner_id=owner_user_id, contest_id=c["id"],
                               total_hands=hands, match_type="contest", game_id=game_id)
            return "m1"

    mgr = ContestManager(store, FakeOrch())  # type: ignore

    async def run():
        await mgr._dispatch_pending(c["id"], 0)

    asyncio.run(run())
    assert seen["n_dots"] == 9
    assert seen["game_id"] == "pencil"


def test_contest_create_uses_template_match_config(store: Store):
    """ContestManager.create 从模板取自带 match_config（无显式传入时）。"""
    from bzplat.backend.contests.manager import ContestManager
    users, _ = _mk_bots(store, 1)
    mgr = ContestManager(store, MatchOrchestrator(store, max_concurrent=1))
    c = mgr.create(users[0]["id"], "t", template_id="holdem_swiss_ko")
    assert c["match_config_json"] == '{"hands": 70}'
    c2 = mgr.create(users[0]["id"], "t2", template_id="gomoku_group_drr_ko")
    assert c2["match_config_json"] == "{}"
    c3 = mgr.create(users[0]["id"], "t3", template_id="pencil_swiss_ko")
    assert c3["match_config_json"] == '{"n_dots": 6}'  # 对齐裁判 25 格


# ── 多轮赛制推进修复（500 人压测发现的 bug）─────────────────────
class _FakeOrch:
    """伪 orchestrator：challenge 直接建 match 行（不真跑 bot）。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.n = 0

    async def challenge(self, a, b, owner_user_id, *, hands=1, contest_id=None, **k):
        self.n += 1
        mid = f"fake-match-{contest_id}-{self.n}"
        self.store.create_match(
            mid, a, b, owner_id=owner_user_id, contest_id=contest_id,
            total_hands=hands, match_type="contest",
        )
        return mid


def _complete_all_pairs(store: Store, cid: int, stage_idx: int, *, winner_fn) -> int:
    """把某 stage 的所有 pending/running pairing 的 match 标完成（winner_fn(a,b)->0|1）。"""
    n = 0
    for p in store.list_contest_pairings(cid, stage_idx=stage_idx):
        mid = p.get("match_id")
        if not mid:
            continue
        m = store.get_match(mid)
        if not m or m["status"] not in (STATUS_COMPLETED, "aborted"):
            w = winner_fn(p["bot_a_id"], p["bot_b_id"])
            store.update_match(mid, status=STATUS_COMPLETED, winner=w, earnings_a=100 if w == 0 else -100, earnings_b=-100 if w == 0 else 100)
            store.update_contest_pairing(p["id"], status="completed")
            n += 1
    return n


def test_swiss_generates_next_round(store: Store):
    """swiss rounds=2：R1 完成后 maybe_finish 应生成 R2（而非直接进下一阶段）。
    复现 500 人压测 bug：swiss 只跑 R1 就进 KO。"""
    users, bots = _mk_bots(store, 4)
    cid = store.create_contest(
        "swiss2", users[0]["id"], game_id="holdem",
        stages_json=json.dumps([{"key": "s", "type": "swiss", "rounds": 2, "scoring": "poker_3_1_0", "advance_count": 2, "rest_after_minutes": 0}]),
    )["id"]
    for u, b in zip(users, bots):
        store.add_contest_entry(cid, u["id"], b["id"])
    store.update_contest(cid, status="running", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0)
        # R1: 2 场
        r1 = store.list_contest_pairings(cid, stage_idx=0)
        assert len(r1) == 2, f"R1 应 2 场，实际 {len(r1)}"
        # 完成 R1（固定 bot_a 胜）
        _complete_all_pairs(store, cid, 0, winner_fn=lambda a, b: 0)
        await mgr.maybe_finish(cid)
        # 关键断言：R1 完成后应生成 R2（swiss 还没到 2 轮），不应进下一阶段/finished
        all_pairs = store.list_contest_pairings(cid, stage_idx=0)
        rounds = {p["round_num"] for p in all_pairs}
        assert 2 in rounds, f"swiss rounds=2 应生成 R2，实际轮次 {rounds}"
        c2 = store.get_contest(cid)
        assert c2["status"] == "running" and c2["current_stage_idx"] == 0, (
            f"swiss R1 完成不应结束阶段，status={c2['status']} stage={c2['current_stage_idx']}"
        )

    asyncio.run(run())


def test_single_elimination_generates_final(store: Store):
    """single_elimination 4 人：四分之一(R1,2场)完成后 maybe_finish 应生成决赛(R2,1场)，
    而非直接 finished。复现 500 人压测 bug：KO 只跑四分之一就结束。"""
    users, bots = _mk_bots(store, 4)
    cid = store.create_contest(
        "ko4", users[0]["id"], game_id="holdem",
        stages_json=json.dumps([{"key": "ko", "type": "single_elimination", "scoring": "poker_3_1_0", "rest_after_minutes": 0}]),
    )["id"]
    for u, b in zip(users, bots):
        store.add_contest_entry(cid, u["id"], b["id"])
    store.update_contest(cid, status="running", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0)
        r1 = store.list_contest_pairings(cid, stage_idx=0)
        assert len(r1) == 2, f"4人淘汰 R1 应 2 场，实际 {len(r1)}"
        # 完成 R1
        _complete_all_pairs(store, cid, 0, winner_fn=lambda a, b: 0)
        await mgr.maybe_finish(cid)
        # 关键断言：R1 完成应生成 R2（决赛），不应 finished
        all_pairs = store.list_contest_pairings(cid, stage_idx=0)
        rounds = {p["round_num"] for p in all_pairs}
        assert 2 in rounds, f"4人淘汰应生成 R2(决赛)，实际轮次 {rounds}"
        r2 = [p for p in all_pairs if p["round_num"] == 2]
        assert len(r2) == 1, f"决赛应 1 场，实际 {len(r2)}"
        c2 = store.get_contest(cid)
        assert c2["status"] != "finished", "R1 完成不应直接 finished，应继续决赛"

    asyncio.run(run())


def test_single_elimination_bye_round1_odd_count():
    """非 2 幂人数（奇数）首轮：bye 者应生成 bot_b_id=None 的占位 spec，
    不能被丢弃。n=5 → size=8, 3 bye → 1 real(1v5) + 3 bye(seed2/3/4) = 4 spec（无人丢失）。"""
    bots = list(range(1, 6))  # 5 人
    specs = single_elimination(bots)
    real = [s for s in specs if s.bot_b_id is not None]
    byes = [s for s in specs if s.bot_b_id is None]
    # 5 人首轮：1 场 real match(1v5) + 3 个 bye 占位 = 4（所有 5 bot 都出现）
    assert len(real) >= 1, f"5人首轮应至少 1 场 real match，实际 {len(real)}"
    assert len(byes) == 3, f"5人首轮应 3 个 bye 占位，实际 {len(byes)}"
    # 所有 5 个 bot 必须出现在某 spec 里（不能丢失）
    appearing = set()
    for s in specs:
        appearing.add(s.bot_a_id)
        if s.bot_b_id is not None:
            appearing.add(s.bot_b_id)
    assert appearing == set(bots), f"5人淘汰首轮丢失 bot：{set(bots) - appearing}"


def _run_single_elim_to_finish(store: Store, n: int, winner_fn):
    """建 n 人 single_elimination 赛事，逐轮完成直到 finished。返回最终 contest 状态。"""
    users, bots = _mk_bots(store, n)
    cid = store.create_contest(
        f"ko{n}", users[0]["id"], game_id="holdem",
        stages_json=json.dumps([{"key": "ko", "type": "single_elimination",
                                 "scoring": "poker_3_1_0", "rest_after_minutes": 0}]),
    )["id"]
    for u, b in zip(users, bots):
        store.add_contest_entry(cid, u["id"], b["id"])
    store.update_contest(cid, status="running", current_stage_idx=0)
    orch = _FakeOrch(store)
    mgr = ContestManager(store, orch)  # type: ignore

    async def run():
        await mgr._begin_stage(cid, 0)
        # 反复完成当前轮 + maybe_finish，直到 finished 或无进展
        for _ in range(20):  # 上限防死循环
            c = store.get_contest(cid)
            if c["status"] == "finished":
                break
            _complete_all_pairs(store, cid, 0, winner_fn=winner_fn)
            await mgr.maybe_finish(cid)
        return store.get_contest(cid)

    return asyncio.run(run()), store, cid, bots


def test_single_elim_5_players_finishes_with_champion(store: Store):
    """5 人（非 2 幂，3 bye）：必须能 finish 且决出唯一冠军。
    复现 bug：range(0,len-1,2) 丢末位胜者 + bye 不记录 → 赛事卡死/错冠军。"""
    c_final, store, cid, bots = _run_single_elim_to_finish(
        store, 5, winner_fn=lambda a, b: 0  # 永远 bot_a 胜（高种子）
    )
    assert c_final["status"] == "finished", f"5人淘汰应 finish，实际 {c_final['status']}"
    # 所有首轮 bot 都应参与（无人丢失）：统计所有出现过 match 的 bot_a/bot_b
    appearing = set()
    for p in store.list_contest_pairings(cid, stage_idx=0):
        if p.get("bot_a_id") is not None:
            appearing.add(p["bot_a_id"])
        if p.get("bot_b_id") is not None:
            appearing.add(p["bot_b_id"])
    assert appearing == {b["id"] for b in bots}, "5人淘汰丢失了 bot"


def test_single_elim_7_players_finishes_with_champion(store: Store):
    """7 人（非 2 幂，1 bye）：必须能 finish。"""
    c_final, store, cid, bots = _run_single_elim_to_finish(
        store, 7, winner_fn=lambda a, b: 1  # 永远 bot_b 胜
    )
    assert c_final["status"] == "finished", f"7人淘汰应 finish，实际 {c_final['status']}"
    appearing = set()
    for p in store.list_contest_pairings(cid, stage_idx=0):
        if p.get("bot_a_id") is not None:
            appearing.add(p["bot_a_id"])
        if p.get("bot_b_id") is not None:
            appearing.add(p["bot_b_id"])
    assert appearing == {b["id"] for b in bots}, "7人淘汰丢失了 bot"


def test_single_elim_8_players_power_of_two(store: Store):
    """8 人（2 幂，0 bye）：回归测试，不应被 bye 修复破坏。"""
    c_final, store, cid, bots = _run_single_elim_to_finish(
        store, 8, winner_fn=lambda a, b: 0
    )
    assert c_final["status"] == "finished", f"8人淘汰应 finish，实际 {c_final['status']}"
