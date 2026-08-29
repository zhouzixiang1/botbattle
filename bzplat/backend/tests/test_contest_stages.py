"""赛事阶段引擎与休息换 Bot 测试。"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.ranking import with_official_result_provenance
from bzplat.backend.contests.scheduler import ContestScheduler
from bzplat.backend.contests.stages import (
    effective_group_count,
    estimate_match_count,
    generate_stage_pairings,
    group_round_robin,
    round_robin,
    single_elimination,
    swiss_pairings,
)
from bzplat.backend.contests.templates import resolve_stages
from bzplat.backend.crypto import hash_password
from bzplat.backend.games import registry as game_registry
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store
from bzplat.backend.store.schema import CONTEST_REST, STATUS_COMPLETED
from bzplat.backend.tests.execution_helpers import claim_request


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


@pytest.mark.parametrize(
    ("participant_count", "expected_groups", "expected_single_matches"),
    [
        (2, 1, 1),
        (3, 1, 3),
        (4, 2, 2),
        (5, 2, 4),
        (6, 3, 3),
        (7, 3, 5),
        (8, 4, 4),
    ],
)
def test_default_group_count_keeps_every_small_roster_in_round_robin(
    participant_count, expected_groups, expected_single_matches
):
    """Default four groups shrink so n=2..8 has no empty/singleton group."""
    bots = list(range(1, participant_count + 1))
    single = group_round_robin(bots, group_count=4)
    double = group_round_robin(bots, group_count=4, double=True)

    assert len(single) == expected_single_matches
    assert len(double) == expected_single_matches * 2
    assert effective_group_count(participant_count, 4) == expected_groups
    assert estimate_match_count(
        {"type": "group_round_robin", "group_count": 4},
        participant_count,
    ) == len(single)
    assert estimate_match_count(
        {"type": "group_double_round_robin", "group_count": 4},
        participant_count,
    ) == len(double)
    assert len({pairing.group_id for pairing in single}) == expected_groups
    for pairings, expected_appearances in ((single, 1), (double, 2)):
        appearances = {
            bot_id: sum(
                bot_id in (pairing.bot_a_id, pairing.bot_b_id)
                for pairing in pairings
            )
            for bot_id in bots
        }
        assert set(appearances) == set(bots)
        assert min(appearances.values()) >= expected_appearances


def test_swiss_and_ko():
    bots = [10, 20, 30, 40]
    swiss = swiss_pairings(bots, scores={10: 3, 20: 3, 30: 1, 40: 0})
    assert len(swiss) == 2
    ko = single_elimination(bots)
    assert len(ko) == 2  # 4 人首轮 2 场


def test_swiss_repeated_pair_fallback_accepts_played_set():
    """3 人第二轮仅剩已交手候选时不得对 played set 调 .get 崩溃。"""
    pairings = swiss_pairings(
        [1, 2, 3],
        scores={1: 3.0, 2: 1.0, 3: 0.0},
        played={(1, 2)},
        round_num=2,
    )
    matches = [pairing for pairing in pairings if pairing.requires_match]
    byes = [pairing for pairing in pairings if not pairing.requires_match]
    assert len(matches) == len(byes) == 1
    assert {matches[0].bot_a_id, matches[0].bot_b_id} == {1, 2}
    assert matches[0].round_num == byes[0].round_num == 2


def test_swiss_odd_pairings_persist_explicit_rotating_bye_specs():
    """奇数 Swiss 每轮返回 completed/no-match bye，且优先轮换未 bye 者。"""
    bye_counts: dict[int, int] = {}
    bye_order: list[int] = []
    for round_num in range(1, 4):
        pairings = swiss_pairings(
            [1, 2, 3],
            scores={1: 0.0, 2: 0.0, 3: 0.0},
            round_num=round_num,
            bye_counts=bye_counts,
        )
        assert len(pairings) == 2
        bye = next(pairing for pairing in pairings if not pairing.requires_match)
        assert bye.bot_b_id is None
        assert bye.status == "completed"
        assert bye.round_num == round_num
        bye_order.append(bye.bot_a_id)
        bye_counts[bye.bot_a_id] = bye_counts.get(bye.bot_a_id, 0) + 1

    assert set(bye_order) == {1, 2, 3}


def test_round_robin_templates():
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


def _mk_bots(store: Store, n: int = 4, *, game_id: str = "holdem"):
    fixture_dir = Path(store.path).resolve().parent / "bot-fixtures"
    fixture_dir.mkdir(parents=True, exist_ok=True)
    users = []
    bots = []
    for i in range(n):
        u = store.create_user(f"user{i}", f"u{i}@ex.com", hash_password("password1"))
        users.append(u)
        binary_path = fixture_dir / f"fake{i}"
        binary_path.write_bytes(b"test fixture")
        b = store.create_bot(
            u["id"],
            f"bot{i}",
            binary_path=str(binary_path),
            format="elf",
            is_active=1,
            game_id=game_id,
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
    new_binary_path = Path(store.path).resolve().parent / "bot-fixtures" / "fake0b"
    new_binary_path.write_bytes(b"test fixture")
    b_new = store.create_bot(
        users[0]["id"],
        "bot0b",
        binary_path=str(new_binary_path),
        format="elf",
        is_active=1,
    )
    orch = MatchOrchestrator(
        store,
        runner=MatchRunner(BinaryRunner(prefer_local=True)),
        max_concurrent=1,
    )
    mgr = ContestManager(store, orch)
    asyncio.run(mgr.dispatch(c["id"], users[0]["id"], b_new["id"]))

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
    )
    for u, b in zip(users, bots):
        store.add_contest_entry(c["id"], u["id"], b["id"])
    store.update_contest(c["id"], status="open")

    # 不真正跑 bot：只测 begin_stage 生成
    class FakeOrch:
        def __init__(self) -> None:
            self.n = 0

        async def challenge(
            self, a, b, owner_user_id, *, match_type="contest", contest_id=None,
            game_id=None, **k
        ):
            self.n += 1
            mid = f"fake-match-{self.n}"
            store.create_match(
                mid,
                a,
                b,
                owner_id=owner_user_id,
                contest_id=contest_id,
                match_type=match_type,
                game_id=game_id,
                match_config={},
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
                result={"deltas": [100, -100]},
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


def test_create_rejects_unregistered_engine(store: Store):
    users, _ = _mk_bots(store, 2)
    with pytest.raises(ValueError, match="game_id"):
        store.create_contest(
            "g",
            users[0]["id"],
            game_id="unknown_game",
            template_id="gomoku_group_drr_ko",
            stages_json=json.dumps(
                [{"key": "g", "type": "group_double_round_robin", "group_count": 1}]
            ),
        )


def test_gomoku_engine_registered(store: Store):
    """gomoku 已注册：start 不再因引擎拒绝（缺人会走后续逻辑）。"""
    from bzplat.backend.store.schema import REGISTERED_ENGINES

    assert "gomoku" in REGISTERED_ENGINES
    assert "pencil" in REGISTERED_ENGINES


def test_full_double_round_robin_has_no_participant_cap(store: Store):
    """17 人五子棋双循环须生成 272 场并进入受控队列。"""
    users, bots = _mk_bots(store, 17, game_id="gomoku")
    template_id, game_id, stages = resolve_stages("board_rr")
    # 旧 platform_settings 行不得重新形成另一套人数限制。
    store.set_setting("full_rr_max_n", "1")
    c = store.create_contest(
        "big",
        users[0]["id"],
        game_id=game_id,
        template_id=template_id,
        stages_json=json.dumps(stages),
    )
    for u, b in zip(users, bots):
        store.add_contest_entry(c["id"], u["id"], b["id"])
    store.update_contest(c["id"], status="open")
    store.executions.resume()
    mgr = ContestManager(store, MatchOrchestrator(store, max_concurrent=1))

    async def run():
        await mgr.start(c["id"])

    asyncio.run(run())
    assert store.get_contest(c["id"])["status"] == "published"
    pairings = store.list_contest_pairings(c["id"])
    assert len(pairings) == 17 * 16
    assert all(
        row["status"] == "pending" and row["match_id"] is None
        for row in pairings
    )
    queued_jobs = store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs "
        "WHERE contest_id=? AND source='contest' AND status='queued'",
        (c["id"],),
    ).fetchone()[0]
    assert queued_jobs == 17 * 16


def test_scheduler_auto_publishes_and_starts_large_double_round_robin(
    store: Store,
):
    """报名截止与开赛两个 scheduler tick 都不得恢复旧人数硬挡。"""
    users, bots = _mk_bots(store, 17, game_id="gomoku")
    template_id, game_id, stages = resolve_stages("board_rr")
    contest = store.create_contest(
        "scheduled-big",
        users[0]["id"],
        game_id=game_id,
        template_id=template_id,
        stages_json=json.dumps(stages),
        registration_opens_at="2019-12-31T00:00:00",
        registration_closes_at="2020-01-01T00:00:00",
        starts_at="2020-01-01T00:00:00",
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])
    store.update_contest(contest["id"], status="open")
    store.executions.resume()
    orch = MatchOrchestrator(store, max_concurrent=1)
    manager = ContestManager(store, orch)
    scheduler = ContestScheduler(manager, store)

    asyncio.run(scheduler._tick())
    assert store.get_contest(contest["id"])["status"] == "published"
    assert len(store.list_contest_pairings(contest["id"])) == 17 * 16

    asyncio.run(scheduler._tick())
    # 到点先把完整排期送入持久队列；赛事只有在 claim 原子创建 Match 并
    # 绑定首条 pairing 后才进入 running，不能把“已排队”伪装成“已开局”。
    assert store.get_contest(contest["id"])["status"] == "published"
    queued_jobs = store._conn.execute(
        "SELECT COUNT(*) FROM execution_jobs "
        "WHERE contest_id=? AND source='contest' AND status='queued'",
        (contest["id"],),
    ).fetchone()[0]
    assert queued_jobs == 17 * 16
    first_request = store._conn.execute(
        "SELECT public_id FROM execution_jobs WHERE contest_id=? "
        "AND source='contest' AND status='queued' ORDER BY id LIMIT 1",
        (contest["id"],),
    ).fetchone()[0]
    claim_request(orch, first_request, start=False)
    assert store.get_contest(contest["id"])["status"] == "running"


@pytest.mark.parametrize("stage_type", ["round_robin", "double_round_robin"])
def test_full_round_robin_guard_has_no_cap(store: Store, stage_type: str):
    mgr = ContestManager(store, MatchOrchestrator(store, max_concurrent=1))

    mgr._guard_round_robin_size([{"type": stage_type}], 500)


def test_group_round_robin_has_no_participant_cap(store: Store):
    mgr = ContestManager(store, MatchOrchestrator(store, max_concurrent=1))

    mgr._guard_round_robin_size(
        [{"type": "group_double_round_robin", "group_count": 1}],
        500,
    )


@pytest.mark.parametrize(
    "removed",
    [
        {"match_config_json": json.dumps({"hands": 20})},
        {"hands_per_match": 20},
    ],
)
def test_contest_store_creation_rejects_removed_rule_fields(store: Store, removed):
    users, _ = _mk_bots(store, 1)
    with pytest.raises(TypeError):
        store.create_contest(
            "removed",
            users[0]["id"],
            game_id="holdem",
            **removed,
        )


def test_contest_create_does_not_expose_rule_config_columns(store: Store):
    """新赛事结构不再产生 hands_per_match/match_config_json。"""
    from bzplat.backend.contests.manager import ContestManager
    users, _ = _mk_bots(store, 1)
    mgr = ContestManager(store, MatchOrchestrator(store, max_concurrent=1))
    c = mgr.create(users[0]["id"], "t", template_id="holdem_swiss_ko")
    assert "hands_per_match" not in c
    assert "match_config_json" not in c
    c2 = mgr.create(users[0]["id"], "t2", template_id="board_rr")
    assert "hands_per_match" not in c2
    assert "match_config_json" not in c2
    c3 = mgr.create(users[0]["id"], "t3", template_id="pencil_swiss_ko")
    assert "hands_per_match" not in c3
    assert "match_config_json" not in c3


# ── 多轮赛制推进修复（500 人压测发现的 bug）─────────────────────
class _FakeOrch:
    """伪 orchestrator：challenge 直接建 match 行（不真跑 bot）。"""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.n = 0

    async def challenge(self, a, b, owner_user_id, *, match_type="contest",
                        contest_id=None, game_id=None, **k):
        self.n += 1
        mid = f"fake-match-{contest_id}-{self.n}"
        self.store.create_match(
            mid, a, b, owner_id=owner_user_id, contest_id=contest_id,
            match_type=match_type, game_id=game_id, match_config={},
        )
        return mid


@pytest.mark.parametrize(
    (
        "participant_count",
        "expected_group_matches",
        "expected_advancers",
        "expected_total_matches",
    ),
    [
        (5, 8, 4, 11),
        (7, 10, 6, 15),
        (12, 24, 8, 31),
    ],
)
def test_pencil_group_template_manager_estimate_matches_full_lifecycle(
    store: Store,
    participant_count,
    expected_group_matches,
    expected_advancers,
    expected_total_matches,
):
    """Pencil group advancement, KO jobs and ETA share the generated graph."""
    users, bots = _mk_bots(store, participant_count, game_id="pencil")
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    contest = manager.create(
        users[0]["id"],
        "pencil-group-small-roster",
        game_id="pencil",
        template_id="pencil_group_drr_ko",
    )
    assert contest["template_id"] == "pencil_group_drr_ko"
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])

    estimate = manager.estimate(contest["id"])

    async def run_full_lifecycle():
        started = await manager.start(contest["id"])
        assert started["status"] == "running"
        group_pairings = store.list_contest_pairings(
            contest["id"], stage_idx=0
        )
        assert len(group_pairings) == expected_group_matches
        assert {
            bot_id
            for pairing in group_pairings
            for bot_id in (pairing["bot_a_id"], pairing["bot_b_id"])
            if bot_id is not None
        } == {bot["id"] for bot in bots}

        _complete_all_pairs(
            store, contest["id"], 0, winner_fn=lambda _a, _b: 0
        )
        rested = await manager.maybe_finish(contest["id"])
        assert rested["status"] == "rest"
        resumed = await manager.resume(contest["id"])
        assert resumed["status"] == "running"
        assert resumed["current_stage_idx"] == 1
        active_entries = [
            entry
            for entry in store.list_contest_entries(contest["id"])
            if not entry["eliminated"]
        ]
        assert len(active_entries) == expected_advancers

        for _ in range(expected_advancers + 1):
            state = store.get_contest(contest["id"])
            if state["status"] == "finished":
                break
            assert state["current_stage_idx"] == 1
            assert _complete_all_pairs(
                store, contest["id"], 1, winner_fn=lambda _a, _b: 0
            ) > 0
            await manager.maybe_finish(contest["id"])
        else:  # pragma: no cover - bounded lifecycle guard
            pytest.fail("Pencil KO did not converge")

    asyncio.run(run_full_lifecycle())
    pairings = store.list_contest_pairings(contest["id"], stage_idx=0)
    assert len(pairings) == expected_group_matches
    actual_match_jobs = sum(
        pairing.get("match_id") is not None
        for pairing in store.list_contest_pairings(contest["id"])
    )
    assert actual_match_jobs == expected_total_matches
    assert estimate["estimated_matches"] == actual_match_jobs
    assert estimate["max_concurrent"] == 6
    assert estimate["eta_seconds"] == int(
        actual_match_jobs
        * game_registry.get("pencil").eta_for_match({})
        / estimate["max_concurrent"]
    )
    final = store.get_contest(contest["id"])
    assert final["status"] == "finished"
    assert final["official_results_ready"] == 1


def test_group_estimate_prefers_per_group_advancement_like_lifecycle(
    store: Store,
):
    """When both fields exist, four groups × Top1 overrides global Top8."""
    users, bots = _mk_bots(store, 8, game_id="pencil")
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    contest = manager.create(
        users[0]["id"],
        "group-advance-priority",
        game_id="pencil",
        stages=[
            {
                "key": "group",
                "type": "group_double_round_robin",
                "group_count": 4,
                "advance_per_group": 1,
                "advance_count": 8,
            },
            {"key": "ko", "type": "single_elimination"},
        ],
    )
    for user, bot in zip(users, bots):
        store.add_contest_entry(contest["id"], user["id"], bot["id"])

    estimate = manager.estimate(contest["id"])

    async def reach_ko():
        await manager.start(contest["id"])
        assert len(store.list_contest_pairings(contest["id"], stage_idx=0)) == 8
        _complete_all_pairs(
            store, contest["id"], 0, winner_fn=lambda _a, _b: 0
        )
        await manager.maybe_finish(contest["id"])

    asyncio.run(reach_ko())
    active_entries = [
        entry
        for entry in store.list_contest_entries(contest["id"])
        if not entry["eliminated"]
    ]
    assert len(active_entries) == 4
    assert len(store.list_contest_pairings(contest["id"], stage_idx=1)) == 2
    assert estimate["estimated_matches"] == 11  # group DRR 8 + four-player KO 3.


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
            store.update_match(mid, status=STATUS_COMPLETED, winner=w, result={"deltas": [100 if w == 0 else -100, -100 if w == 0 else 100]})
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


def test_swiss_materializes_balanced_seats_into_pairing_and_challenge(store: Store):
    """三轮两人 Swiss 的实际 seat0 应轮换，并原样传给 challenge。"""
    users, bots = _mk_bots(store, 2)
    cid = store.create_contest(
        "swiss-seat-balance",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps(
            [{"key": "s", "type": "swiss", "rounds": 3}]
        ),
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(cid, user["id"], bot["id"])
    store.update_contest(cid, status="running", current_stage_idx=0)
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]

    async def run():
        await manager._begin_stage(cid, 0)
        for _ in range(2):
            _complete_all_pairs(store, cid, 0, winner_fn=lambda _a, _b: 0)
            await manager.maybe_finish(cid)

    asyncio.run(run())
    pairings = store.list_contest_pairings(cid, stage_idx=0)
    actual_seat0 = [
        next(p for p in pairings if p["round_num"] == round_num)["bot_a_id"]
        for round_num in (1, 2, 3)
    ]
    assert actual_seat0 == [bots[0]["id"], bots[1]["id"], bots[0]["id"]]
    assert all(pairing["color_first"] == 0 for pairing in pairings)
    for pairing in pairings:
        match = store.get_match(pairing["match_id"])
        assert (match["bot_a_id"], match["bot_b_id"]) == (
            pairing["bot_a_id"],
            pairing["bot_b_id"],
        )


def test_swiss_odd_multi_round_byes_are_scored_rotated_and_persisted(store: Store):
    """3 人 3 轮 Swiss：bye 每轮落一条 completed/no-match，三人各一次。

    bye 给胜场分，但在尚未完成真实对局的 R1 不应增加 wins/对手。
    每轮完成后 ``maybe_finish`` 必须能识别 bye 已完成并持久化下一轮。
    """
    users, bots = _mk_bots(store, 3)
    stage = {
        "key": "swiss-odd",
        "type": "swiss",
        "rounds": 3,
        "scoring": "poker_3_1_0",
        "rest_after_minutes": 0,
    }
    cid = store.create_contest(
        "swiss-odd-3",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(cid, user["id"], bot["id"])
    store.update_contest(cid, status="running", current_stage_idx=0)
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]

    async def run():
        await manager._begin_stage(cid, 0)
        first_round = store.list_contest_pairings(cid, stage_idx=0)
        first_bye = next(pairing for pairing in first_round if pairing["bot_b_id"] is None)
        initial_standings = {
            row["entry_id"]: row for row in manager.standings(cid, stage_idx=0)
        }
        assert initial_standings[first_bye["entry_a_id"]]["points"] == 3
        assert initial_standings[first_bye["entry_a_id"]]["wins"] == 0
        assert initial_standings[first_bye["entry_a_id"]]["byes"] == 1
        assert {
            row["byes"] for entry_id, row in initial_standings.items()
            if entry_id != first_bye["entry_a_id"]
        } == {0}
        assert first_bye["status"] == "completed" and first_bye["match_id"] is None

        for round_num in range(1, 4):
            _complete_all_pairs(store, cid, 0, winner_fn=lambda _a, _b: 0)
            await manager.maybe_finish(cid)
            persisted = store.list_contest_pairings(cid, stage_idx=0)
            assert any(
                pairing["round_num"] == round_num
                and pairing["bot_b_id"] is None
                and pairing["match_id"] is None
                and pairing["status"] == "completed"
                for pairing in persisted
            )

        pairings = store.list_contest_pairings(cid, stage_idx=0)
        byes = [pairing for pairing in pairings if pairing["bot_b_id"] is None]
        assert len(byes) == 3
        assert {pairing["entry_a_id"] for pairing in byes} == {
            entry["id"] for entry in store.list_contest_entries(cid)
        }
        final_standings = manager.standings(cid, stage_idx=0)
        assert {row["byes"] for row in final_standings} == {1}
        assert all(row["wins"] + row["losses"] == 2 for row in final_standings)
        assert all(
            row["points"] == 3 * (row["wins"] + row["byes"])
            for row in final_standings
        )
        assert store.get_contest(cid)["status"] == "finished"

        # Terminal lifecycle persists the same bye-inclusive points into both
        # the stage snapshot and official ranking.  W/D/L remains match-only:
        # the no-match bye is not a fake win in either live or durable rows.
        standings_by_entry = {row["entry_id"]: row for row in final_standings}
        stage_results = store.list_stage_results(cid, stage_idx=0)
        assert len(stage_results) == 3
        for row in stage_results:
            standing = standings_by_entry[row["entry_id"]]
            assert (
                row["points"], row["wins"], row["draws"], row["losses"]
            ) == (
                standing["points"], standing["wins"],
                standing["draws"], standing["losses"],
            )
            assert row["points"] == 3 * (row["wins"] + standing["byes"])
            assert row["wins"] + row["draws"] + row["losses"] == 2

        official = store.list_official_results(cid)
        assert [row["rank"] for row in official] == [1, 2, 3]
        assert {row["stage_idx"] for row in official} == {0}
        assert {
            row["entry_id"]: row["points"] for row in official
        } == {
            entry_id: standing["points"]
            for entry_id, standing in standings_by_entry.items()
        }
        public_official = with_official_result_provenance(
            store.get_contest(cid), official
        )
        assert {row["source_stage"] for row in public_official} == {0}

    asyncio.run(run())


def _deleted_opponent_no_match_fixture(
    store: Store, stage: dict
) -> tuple[int, list[dict], list[dict], dict]:
    """Persist a completed/no-match real pairing, then trigger FK SET NULL."""
    users, bots = _mk_bots(store, 2)
    contest_id = store.create_contest(
        f"deleted-opponent-{stage['type']}",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )["id"]
    entries = [
        store.add_contest_entry(contest_id, user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    store.update_contest(contest_id, status="running", current_stage_idx=0)
    pairing = store.add_contest_pairing(
        contest_id,
        bots[0]["id"],
        bots[1]["id"],
        status="completed",
        stage_idx=0,
        stage_key=stage.get("key") or "stage0",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    assert store.delete_bot(bots[1]["id"])
    persisted = next(
        row
        for row in store.list_contest_pairings(contest_id, stage_idx=0)
        if row["id"] == pairing["id"]
    )
    assert persisted["entry_b_id"] == entries[1]["id"]
    assert persisted["bot_b_id"] is None
    assert persisted["match_id"] is None
    assert persisted["status"] == "completed"
    return contest_id, entries, bots, persisted


@pytest.mark.parametrize(
    "stage",
    [
        {"key": "swiss", "type": "swiss", "rounds": 2},
        {"key": "rr", "type": "round_robin"},
    ],
)
def test_deleted_opponent_no_match_blocks_swiss_and_round_robin_lifecycle(
    store: Store, stage: dict
):
    """FK SET NULL cannot turn a real Swiss/RR opponent into a completed bye."""
    contest_id, _entries, _bots, _pairing = _deleted_opponent_no_match_fixture(
        store, stage
    )
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]

    assert manager._stage_done(contest_id, 0) is False
    assert manager._has_unfinished_pairings(contest_id) is True
    standings = manager.standings(contest_id, stage_idx=0)
    assert {row["points"] for row in standings} == {0.0}
    assert {row["byes"] for row in standings} == {0}
    if stage["type"] == "swiss":
        assert asyncio.run(
            manager._maybe_next_swiss_round(contest_id, 0, stage)
        ) is False
    with pytest.raises(ValueError, match="仍有未完成对阵"):
        asyncio.run(manager.finish(contest_id))
    assert store.get_contest(contest_id)["status"] == "running"


def test_deleted_opponent_no_match_blocks_elimination_advance(store: Store):
    """KO must not promote seat A when side B survives only as entry identity."""
    stage = {"key": "ko", "type": "single_elimination"}
    contest_id, _entries, _bots, _pairing = _deleted_opponent_no_match_fixture(
        store, stage
    )
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]

    assert manager._stage_done(contest_id, 0) is False
    assert asyncio.run(
        manager._maybe_next_elim_round(contest_id, 0, stage)
    ) == "blocked"
    assert manager._has_unfinished_pairings(contest_id) is True
    with pytest.raises(ValueError, match="仍有未完成对阵"):
        asyncio.run(manager.finish(contest_id))
    assert store.get_contest(contest_id)["status"] == "running"


def test_swiss_history_drift_blocks_later_round_instead_of_counting_fake_bye(
    store: Store,
):
    """An FK-damaged R1 blocks R3 even when every R2 row is adjudicated."""
    users, bots = _mk_bots(store, 3)
    stage = {"key": "swiss", "type": "swiss", "rounds": 3}
    contest_id = store.create_contest(
        "swiss-bye-drift",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )["id"]
    entries = [
        store.add_contest_entry(contest_id, user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    store.update_contest(contest_id, status="running", current_stage_idx=0)

    # R1 says B had a real opponent A, but the match was never bound.  Deleting
    # old A nulls bot_b_id while entry_b_id remains, reproducing the drift that
    # used to increment B's bye counter.
    store.add_contest_pairing(
        contest_id,
        bots[1]["id"],
        bots[0]["id"],
        round_num=1,
        status="completed",
        stage_idx=0,
        stage_key="swiss",
        entry_a_id=entries[1]["id"],
        entry_b_id=entries[0]["id"],
    )
    assert store.delete_bot(bots[0]["id"])
    replacement_path = Path(store.path).resolve().parent / "bot-fixtures" / "replacement-a"
    replacement_path.write_bytes(b"test fixture")
    replacement = store.create_bot(
        users[0]["id"],
        "replacement-a",
        binary_path=str(replacement_path),
        format="elf",
        game_id="holdem",
    )
    store.update_entry(contest_id, users[0]["id"], bot_id=replacement["id"])

    # R2: replacement A beats B; C receives the sole authoritative bye.
    match_id = "swiss-bye-drift-r2"
    store.create_match(
        match_id,
        replacement["id"],
        bots[1]["id"],
        owner_id=users[0]["id"],
        contest_id=contest_id,
        match_type="contest",
        game_id="holdem",
    )
    store.update_match(
        match_id,
        status="completed",
        winner=0,
        result={"deltas": [100, -100]},
    )
    store.add_contest_pairing(
        contest_id,
        replacement["id"],
        bots[1]["id"],
        round_num=2,
        match_id=match_id,
        status="completed",
        stage_idx=0,
        stage_key="swiss",
        entry_a_id=entries[0]["id"],
        entry_b_id=entries[1]["id"],
    )
    store.add_contest_pairing(
        contest_id,
        bots[2]["id"],
        None,
        round_num=2,
        status="completed",
        stage_idx=0,
        stage_key="swiss",
        entry_a_id=entries[2]["id"],
        entry_b_id=None,
    )

    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    assert asyncio.run(
        manager._maybe_next_swiss_round(contest_id, 0, stage)
    ) is False
    pairings = store.list_contest_pairings(contest_id, stage_idx=0)
    assert not any(pairing["round_num"] == 3 for pairing in pairings)
    standings = {
        row["entry_id"]: row for row in manager.standings(contest_id, stage_idx=0)
    }
    assert standings[entries[1]["id"]]["byes"] == 0
    assert standings[entries[2]["id"]]["byes"] == 1
    assert store.get_contest(contest_id)["status"] == "running"


def test_swiss_fully_adjudicated_history_allows_next_round(store: Store):
    """All earlier real matches completed plus strict byes may generate R3."""
    users, bots = _mk_bots(store, 3)
    stage = {"key": "swiss", "type": "swiss", "rounds": 3}
    contest_id = store.create_contest(
        "swiss-complete-history",
        users[0]["id"],
        game_id="holdem",
        stages_json=json.dumps([stage]),
    )["id"]
    entries = [
        store.add_contest_entry(contest_id, user["id"], bot["id"])
        for user, bot in zip(users, bots)
    ]
    store.update_contest(contest_id, status="running", current_stage_idx=0)

    for round_num, seats, bye_index in (
        (1, (0, 1), 2),
        (2, (0, 2), 1),
    ):
        match_id = f"swiss-complete-history-r{round_num}"
        a_index, b_index = seats
        store.create_match(
            match_id,
            bots[a_index]["id"],
            bots[b_index]["id"],
            owner_id=users[0]["id"],
            contest_id=contest_id,
            match_type="contest",
            game_id="holdem",
        )
        store.update_match(
            match_id,
            status="completed",
            winner=0,
            result={"deltas": [100, -100]},
        )
        store.add_contest_pairing(
            contest_id,
            bots[a_index]["id"],
            bots[b_index]["id"],
            round_num=round_num,
            match_id=match_id,
            status="completed",
            stage_idx=0,
            stage_key="swiss",
            entry_a_id=entries[a_index]["id"],
            entry_b_id=entries[b_index]["id"],
        )
        store.add_contest_pairing(
            contest_id,
            bots[bye_index]["id"],
            None,
            round_num=round_num,
            status="completed",
            stage_idx=0,
            stage_key="swiss",
            entry_a_id=entries[bye_index]["id"],
            entry_b_id=None,
        )

    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    assert asyncio.run(
        manager._maybe_next_swiss_round(contest_id, 0, stage)
    ) is True
    round_three = [
        pairing
        for pairing in store.list_contest_pairings(contest_id, stage_idx=0)
        if pairing["round_num"] == 3
    ]
    assert len(round_three) == 2
    assert sum(pairing["entry_b_id"] is None for pairing in round_three) == 1


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


@pytest.mark.parametrize("draw_reason", ["double_pass", "board_full"])
def test_single_elimination_draw_blocks_without_finishing_or_losing_winners(
    store: Store, draw_reason: str
):
    """8 人 KO 首轮任一合法和棋都须显式阻塞，不得提前完赛。"""
    users, bots = _mk_bots(store, 8, game_id="gomoku")
    manager = ContestManager(store, _FakeOrch(store))  # type: ignore[arg-type]
    cid = manager.create(
        users[0]["id"],
        f"ko-draw-{draw_reason}",
        game_id="gomoku",
        stages=[
            {
                "key": "ko",
                "type": "single_elimination",
                "scoring": "ccgc_2_1_0",
                "rest_after_minutes": 0,
            }
        ],
    )["id"]
    for user, bot in zip(users, bots):
        store.add_contest_entry(cid, user["id"], bot["id"])
    store.update_contest(cid, status="running", current_stage_idx=0)

    async def run():
        await manager._begin_stage(cid, 0)
        first_round = store.list_contest_pairings(cid, stage_idx=0)
        assert len(first_round) == 4
        decisive_winners: set[int] = set()
        for index, pairing in enumerate(first_round):
            match_id = pairing["match_id"]
            assert match_id
            winner = None if index == 0 else 0
            if winner == 0:
                decisive_winners.add(pairing["bot_a_id"])
            store.update_match(
                match_id,
                status=STATUS_COMPLETED,
                winner=winner,
                reason=draw_reason if winner is None else "five",
                result={"deltas": [0, 0] if winner is None else [1, -1]},
            )
            store.update_contest_pairing(pairing["id"], status="completed")

        await manager.maybe_finish(cid)

        contest = store.get_contest(cid)
        assert contest["status"] == "running"
        assert contest["current_stage_idx"] == 0
        persisted = store.list_contest_pairings(cid, stage_idx=0)
        assert len(persisted) == 4
        assert {pairing["round_num"] for pairing in persisted} == {1}
        assert store.list_stage_results(cid, stage_idx=0) == []
        surviving_winners = {
            pairing["bot_a_id"]
            for pairing in persisted
            if store.get_match(pairing["match_id"])["winner"] == 0
        }
        assert surviving_winners == decisive_winners

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
