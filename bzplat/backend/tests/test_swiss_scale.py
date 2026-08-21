"""预赛/决赛 P3：瑞士扩容 + estimate 传播测试。

验证：
1. 小规模 Swiss 正确性（4/8 人，避重，奇数 bye）
2. 座位平衡（color_first 轮换）
3. 10k 纯编排压测（单轮 < 5s，总场次 70000，避重）
4. estimate 按 advance_count 传播
"""
from __future__ import annotations

import ast
import inspect
import textwrap
import time

from bzplat.backend.contests.stages import (
    estimate_match_count,
    generate_stage_pairings,
    swiss_pairings,
    swiss_rounds_needed,
)


def test_swiss_4_players_avoids_repeat():
    """4 人瑞士 2 轮：第 2 轮不重复第 1 轮对局。"""
    bots = [1, 2, 3, 4]
    r1 = swiss_pairings(bots, scores={b: 0 for b in bots}, played=set(), round_num=1)
    played = {(min(p.bot_a_id, p.bot_b_id), max(p.bot_a_id, p.bot_b_id)) for p in r1}
    # 模拟第 1 轮结果：1/3 赢
    scores = {1: 3, 2: 0, 3: 3, 4: 0}
    r2 = swiss_pairings(bots, scores=scores, played=played, round_num=2)
    played2 = {(min(p.bot_a_id, p.bot_b_id), max(p.bot_a_id, p.bot_b_id)) for p in r2}
    # 第 2 轮不应重复第 1 轮
    assert played.isdisjoint(played2), f"第 2 轮重复第 1 轮: {played & played2}"


def test_swiss_odd_count_bye():
    """奇数 N：最低分者以显式 completed/no-match pairing 记录 bye。"""
    bots = [1, 2, 3, 4, 5]
    r1 = swiss_pairings(bots, scores={b: 0 for b in bots}, played=set())
    matches = [pairing for pairing in r1 if pairing.requires_match]
    byes = [pairing for pairing in r1 if not pairing.requires_match]
    assert len(matches) == 2 and len(byes) == 1, "5 人应 2 对 + 1 bye"
    assert byes[0].bot_a_id == 5
    assert byes[0].bot_b_id is None and byes[0].status == "completed"


def test_swiss_color_balance():
    """座位平衡：color_first 轮换（先手多的下轮后手）。"""
    bots = [1, 2]
    # 1 已先手 3 次，2 先手 0 次 → 本轮 2 应先手（color_first=1）
    r = swiss_pairings(
        bots, scores={1: 0, 2: 0}, played=set(), color_counts={1: 3, 2: 0}
    )
    assert len(r) == 1
    assert r[0].color_first == 1, "先手累计少者（2）本轮应先手（color_first=1）"


def test_generate_stage_pairings_forwards_swiss_color_counts():
    """统一阶段入口不能丢掉 manager 统计出的历史先手次数。"""
    pairings = generate_stage_pairings(
        {"type": "swiss"},
        [1, 2],
        color_counts={1: 4, 2: 0},
        swiss_round=2,
    )
    assert len(pairings) == 1
    assert pairings[0].color_first == 1


def test_swiss_8_players_correctness():
    """8 人瑞士 3 轮：每轮全员配对，无重复对局。"""
    bots = list(range(1, 9))
    played: set[tuple[int, int]] = set()
    scores = {b: 0 for b in bots}
    for rnd in range(1, 4):
        ps = swiss_pairings(bots, scores=scores, played=played, round_num=rnd)
        assert len(ps) == 4, f"第 {rnd} 轮应 4 对（8 人）"
        for p in ps:
            key = (min(p.bot_a_id, p.bot_b_id), max(p.bot_a_id, p.bot_b_id))
            assert key not in played, f"第 {rnd} 轮重复对局 {key}"
            played.add(key)
        # 模拟积分（胜方+3）
        for p in ps:
            scores[p.bot_a_id] += 3


def test_swiss_10k_single_round_under_5s():
    """10k 人单轮配对 < 5s（纯编排，不启 Docker）。"""
    bots = list(range(1, 10001))
    scores = {b: 0.0 for b in bots}
    t0 = time.time()
    r1 = swiss_pairings(bots, scores=scores, played=set(), round_num=1)
    elapsed = time.time() - t0
    assert elapsed < 5.0, f"10k 单轮应 < 5s，实际 {elapsed:.2f}s"
    assert len(r1) == 5000, f"10k 人应配 5000 对，实际 {len(r1)}"


def test_swiss_10k_total_matches_70000():
    """10k 人瑞士总场次 ≈ 14 轮 × 5000 = 70000。"""
    n = 10000
    rounds = swiss_rounds_needed(n)
    assert rounds == 14, f"10k 应 14 轮（ceil(log2(10000))），实际 {rounds}"
    total = estimate_match_count({"type": "swiss", "rounds": rounds}, n)
    assert total == 14 * (n // 2), f"总场次应 70000，实际 {total}"
    assert total == 70000


def test_estimate_propagates_advance_count(tmp_path):
    """estimate 按 advance_count 传播（stage1 用 n，stage2 用 advance_count）。"""
    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.store import Store

    s = Store(str(tmp_path / "est.db"))
    u = s.create_user("org", "o@e.com", "x", role="organizer")["id"]
    # 建 2 阶段赛事：stage1 RR(6 人 15 场) advance 4 → stage2 RR(4 人 6 场)
    c = s.create_contest(
        "est2", organizer_id=u, game_id="holdem",
        stages_json='[{"key":"s1","type":"round_robin","advance_count":4},'
        '{"key":"s2","type":"round_robin"}]',
    )["id"]
    for i in range(6):
        uu = s.create_user(f"usr{i}", f"u{i}@e.com", "x")["id"]
        bid = s.create_bot(uu, f"bot{i}", binary_path="/tmp", format="elf", game_id="holdem")["id"]
        s.add_contest_entry(c, uu, bid)
    cm = ContestManager(s, type("X", (), {"challenge": lambda self, *a, **k: None})())
    est = cm.estimate(c)
    # stage1: 6 人 RR = 15；stage2: 4 人 RR = 6 → 总 21
    assert est["estimated_matches"] == 21, (
        f"advance 传播后应 15+6=21，实际 {est['estimated_matches']}"
    )
    s.close()


def test_estimate_advance_count_cannot_inflate_small_roster(tmp_path):
    """A configured Top 8 cannot turn four Swiss entrants into an eight-player KO."""
    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.store import Store

    store = Store(str(tmp_path / "small-advance-estimate.db"))
    organizer = store.create_user(
        "small-advance-org", "small-advance-org@example.com", "hash"
    )
    contest_id = store.create_contest(
        "small advance estimate",
        organizer_id=organizer["id"],
        game_id="holdem",
        stages_json=(
            '[{"key":"swiss","type":"swiss","rounds":1,'
            '"advance_count":8},'
            '{"key":"ko","type":"single_elimination"}]'
        ),
    )["id"]
    for index in range(4):
        user = store.create_user(
            f"small-advance-user-{index}",
            f"small-advance-{index}@example.com",
            "hash",
        )
        bot = store.create_bot(
            user["id"],
            f"small-advance-bot-{index}",
            binary_path="/tmp",
            format="elf",
            game_id="holdem",
        )
        store.add_contest_entry(contest_id, user["id"], bot["id"])

    estimate = ContestManager(
        store,
        type("EstimateOrchestrator", (), {"max_concurrent": 1})(),
    ).estimate(contest_id)
    assert estimate["estimated_matches"] == 5  # Swiss 2 + four-player KO 3.
    store.close()


def test_estimate_duplicate_round_robin_keeps_jobs_but_counts_both_legs(
    tmp_path,
):
    """4-player duplicate RR is 6 queued jobs but 12 leg-duration units."""
    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.store import Store

    store = Store(str(tmp_path / "duplicate-estimate.db"))
    organizer = store.create_user(
        "duplicate-org", "duplicate-org@example.com", "hash", role="organizer"
    )
    contest_id = store.create_contest(
        "duplicate estimate",
        organizer_id=organizer["id"],
        game_id="holdem",
        template_id="holdem_dup_rr",
        stages_json=(
            '[{"key":"dup_rr","type":"round_robin","duplicate":true}]'
        ),
    )["id"]
    for index in range(4):
        user = store.create_user(
            f"duplicate-user-{index}", f"du{index}@example.com", "hash"
        )
        bot = store.create_bot(
            user["id"],
            f"duplicate-bot-{index}",
            binary_path="/tmp",
            format="elf",
            game_id="holdem",
        )
        store.add_contest_entry(contest_id, user["id"], bot["id"])

    manager = ContestManager(
        store,
        type("EstimateOrchestrator", (), {"max_concurrent": 1})(),
    )
    estimate = manager.estimate(contest_id)
    assert estimate["estimated_matches"] == 6
    # Holdem's registry ETA is 70 hands * 2s = 140s per leg.
    assert estimate["eta_seconds"] == 12 * 140
    store.close()


def test_estimate_eta_has_no_game_name_branch():
    """Duplicate ETA dispatches through GameSpec capabilities, not game ids."""
    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.store.schema import VALID_GAME_IDS

    tree = ast.parse(textwrap.dedent(inspect.getsource(ContestManager.estimate)))
    string_literals = {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert set(VALID_GAME_IDS).isdisjoint(string_literals)
    assert "build_match_plan" in inspect.getsource(ContestManager.estimate)
