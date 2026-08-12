"""P2 residual：复式赛制（duplicate）接入赛事的端到端测试。

验证：
1. holdem_dup_rr 模板存在 + 阶段标 duplicate=True
2. 赛事跑 duplicate 后每对阵只产 1 条 merged match（非 2 条 per leg）
3. merged result 的 deltas 零和（2 leg 累加，seat_swap 翻转后仍零和）
4. winner 按 merged net 判（foldbot vs callbot：callbot 应净胜）
5. standings 按 merged deltas 排序（callbot 选手积分/净筹码高于 foldbot 选手）
6. 单 leg 赛制（duplicate=False）行为不变——duplicate 是新增分支

真跑样例 Bot（foldbot 每手弃牌输盲注，callbot 净筹码高）。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from bzplat.backend.games import registry
from bzplat.backend.games.holdem.templates import TEMPLATES
from bzplat.backend.tests.execution_helpers import (
    claim_next_queued,
    claim_request,
    enable_execution_queue,
)

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
FOLDBOT = SAMPLES / "holdem_bots" / "foldbot"
CALLBOT = SAMPLES / "callbot_linux_amd64"


def test_holdem_dup_rr_template_exists():
    """holdem_dup_rr 模板存在 + 阶段标 duplicate=True。"""
    tpl = next((t for t in TEMPLATES if t["id"] == "holdem_dup_rr"), None)
    assert tpl is not None, "应有 holdem_dup_rr 复式赛制模板"
    assert tpl["stages"][0].get("duplicate") is True, "dup_rr 阶段应标 duplicate=True"


def test_challenge_duplicate_flag_persisted_to_match_config(tmp_path):
    """challenge_duplicate 落的 match 行 match_config 含 duplicate=True（不真跑 bot）。"""
    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.matches.runner import MatchRunner
    from bzplat.backend.runtime.binary_runner import BinaryRunner
    from bzplat.backend.store import Store
    import json as _json

    s = Store(str(tmp_path / "dup_cfg.db"))
    u = s.create_user("orgcfg", "c@e.com", "x")["id"]
    path_a = tmp_path / "cfg-a"
    path_b = tmp_path / "cfg-b"
    path_a.write_bytes(b"test fixture")
    path_b.write_bytes(b"test fixture")
    ba = s.create_bot(u, "cfgbotA", binary_path=str(path_a), format="elf", game_id="holdem", is_active=1)["id"]
    bb = s.create_bot(u, "cfgbotB", binary_path=str(path_b), format="elf", game_id="holdem", is_active=1)["id"]
    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)

    async def _go():
        enable_execution_queue(s)
        request_id = await orch.challenge_duplicate(ba, bb, u, duplicate_seed=42)
        job = claim_request(orch, request_id, start=False)
        m = s.get_match(job["current_match_id"])
        return m

    m = asyncio.run(_go())
    mc = m["match_config"]
    if isinstance(mc, str):
        mc = _json.loads(mc or "{}")
    assert mc.get("duplicate") is True, "challenge_duplicate 落的 match_config 应含 duplicate=True"
    assert m.get("match_seed") == 42, "duplicate_seed 应落 match_seed 列"
    s.close()


@pytest.mark.skipif(not (FOLDBOT.is_file() and CALLBOT.is_file()),
                    reason="foldbot/callbot binary missing")
def test_duplicate_contest_two_legs_independent_scoring(tmp_path):
    """端到端：2 选手 duplicate 赛事 → 1 match 内 2 leg 各独立判胜负（result.legs）。"""
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.matches.runner import MatchRunner
    from bzplat.backend.runtime.binary_runner import BinaryRunner
    from bzplat.backend.store import Store
    from bzplat.backend.store.db import match_deltas

    s = Store(str(tmp_path / "dup_contest.db"))
    org = s.create_user("duporg", "do@e.com", "x", role="organizer")["id"]
    ua = s.create_user("dupA", "da@e.com", "x")["id"]
    ub = s.create_user("dupB", "db@e.com", "x")["id"]
    ba = s.create_bot(ua, "dupFold", binary_path=str(FOLDBOT), format="elf", game_id="holdem", is_active=1)["id"]
    bb = s.create_bot(ub, "dupCall", binary_path=str(CALLBOT), format="elf", game_id="holdem", is_active=1)["id"]
    c = s.create_contest(
        "复式赛", organizer_id=org, game_id="holdem",
        stages_json='[{"key":"dup","type":"round_robin","duplicate":true,"scoring":"poker_3_1_0"}]',
    )["id"]
    s.add_contest_entry(c, ua, ba)
    s.add_contest_entry(c, ub, bb)
    s.update_contest(c, status="open")

    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
    cm = ContestManager(s, orch)

    async def _run_to_finish():
        # start() 内部 _begin_stage → _dispatch_pending_locked → challenge_duplicate
        enable_execution_queue(s)
        await cm.start(c)
        claim_next_queued(orch)
        # 等所有对局 task 跑完（round_robin 2 人 = 1 场 match）
        tasks = list(orch._tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=180)
        # 推进赛事到 finished（maybe_finish 在 on_match_done 回调里跑，但赛事用回调而非 await；
        # 显式调一次确保 standings/official results 落库）
        await cm.maybe_finish(c)

    asyncio.run(_run_to_finish())

    # 1. 赛事应已完成（finished）
    cc = s.get_contest(c)
    assert cc["status"] == "finished", f"赛事应 finished，实为 {cc['status']}"

    # 2. 1 pairing = 1 match（2 leg 同副牌在 match 内）
    ps = s.list_contest_pairings(c, stage_idx=0)
    real_pairings = [p for p in ps if p.get("match_id")]
    assert len(real_pairings) == 1, f"2 人 round_robin 应 1 场对阵，实有 {len(real_pairings)}"
    mid = real_pairings[0]["match_id"]
    m = s.get_match(mid)
    assert m["status"] == "completed", f"match 应 completed，实为 {m['status']}"

    # 3. result.legs：2 leg 各独立判胜负（不是 merged 净筹码判 1 胜）
    result = m.get("result") or {}
    legs = result.get("legs") or []
    assert len(legs) == 2, f"duplicate 应有 2 leg，实有 {len(legs)}（result={result}）"
    assert result.get("rounds_played") == 140, "Holdem 复式两条 70 手 leg 必须累计为 140 手"
    # 每 leg 有独立 winner + deltas（物理 A/B 视角）
    for lg in legs:
        assert "winner" in lg, f"leg 缺 winner: {lg}"
        assert "deltas" in lg and len(lg["deltas"]) == 2, f"leg deltas 应长 2: {lg}"
    # match.winner 应为 None（胜负由 standings 读 legs 决定，无单一 match 胜者）
    assert m["winner"] is None, f"duplicate match.winner 应 None（由 legs 判），实={m['winner']}"

    # 4. 两 leg 的 deltas 累加零和（delta_total tiebreak 用）
    ea, eb = match_deltas(m)
    assert ea + eb == 0, f"两 leg deltas 累加应零和，实 ea={ea} eb={eb}"
    assert result.get("normalized_delta") == ea / 100.0

    # 5. foldbot 每手弃 → callbot 两 leg 都应赢（winner=1，callbot 是 bot_b=seat1 物理）
    #    leg_winner 按 leg_deltas 比较：callbot 净筹码高 → winner=1
    for i, lg in enumerate(legs):
        assert lg["winner"] == 1, f"leg{i} callbot 应胜（foldbot 弃），winner={lg['winner']}"

    # 6. match_seed 落库（确定性回放）
    assert m.get("match_seed") is not None, "duplicate 对局应落 match_seed"

    s.close()


@pytest.mark.skipif(not (FOLDBOT.is_file() and CALLBOT.is_file()),
                    reason="foldbot/callbot binary missing")
def test_duplicate_standings_two_legs_accumulated(tmp_path):
    """standings 按 2 场独立胜负累加：callbot 两 leg 都赢 → 6 分（poker_3_1_0×2）。"""
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.matches.runner import MatchRunner
    from bzplat.backend.runtime.binary_runner import BinaryRunner
    from bzplat.backend.store import Store

    s = Store(str(tmp_path / "dup_stand.db"))
    org = s.create_user("stdorg", "so@e.com", "x", role="organizer")["id"]
    ua = s.create_user("stdA", "sa@e.com", "x")["id"]
    ub = s.create_user("stdB", "sb@e.com", "x")["id"]
    ba = s.create_bot(ua, "stdFold", binary_path=str(FOLDBOT), format="elf", game_id="holdem", is_active=1)["id"]
    bb = s.create_bot(ub, "stdCall", binary_path=str(CALLBOT), format="elf", game_id="holdem", is_active=1)["id"]
    c = s.create_contest(
        "复式排行", organizer_id=org, game_id="holdem",
        stages_json='[{"key":"dup","type":"round_robin","duplicate":true,"scoring":"poker_3_1_0"}]',
    )["id"]
    s.add_contest_entry(c, ua, ba)
    s.add_contest_entry(c, ub, bb)
    s.update_contest(c, status="open")

    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
    cm = ContestManager(s, orch)

    async def _run():
        enable_execution_queue(s)
        await cm.start(c)
        claim_next_queued(orch)
        tasks = list(orch._tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=180)
        await cm.maybe_finish(c)

    asyncio.run(_run())

    rows = cm.standings(c)
    assert len(rows) == 2, f"应有 2 选手 standings，实有 {len(rows)}"
    by_bot = {r["bot_id"]: r for r in rows}
    call_row = by_bot[bb]
    fold_row = by_bot[ba]
    # callbot 两 leg 都赢 → 2 场 × 3 分 = 6 分；foldbot 两 leg 都输 → 0 分
    assert call_row["points"] == 6, f"callbot 两场都赢应 6 分，实={call_row['points']}"
    assert fold_row["points"] == 0, f"foldbot 两场都输应 0 分，实={fold_row['points']}"
    # 胜场数：callbot 2 胜 0 负；foldbot 0 胜 2 负
    assert call_row["wins"] == 2 and call_row["losses"] == 0, f"callbot 应 2胜0负: {call_row}"
    assert fold_row["wins"] == 0 and fold_row["losses"] == 2, f"foldbot 应 0胜2负: {fold_row}"
    # delta_total：callbot 正、foldbot 负（两 leg 累加）
    assert call_row["delta_total"] > 0 and fold_row["delta_total"] < 0, (
        f"callbot 净筹码应正 foldbot 应负: call={call_row['delta_total']} fold={fold_row['delta_total']}"
    )
    # 零和：两人 delta_total 互为相反数
    assert call_row["delta_total"] + fold_row["delta_total"] == 0, "双人 delta_total 应零和"
    s.close()


def test_duplicate_flag_rejected_for_game_without_plan(tmp_path):
    """棋类没有 duplicate 计划时创建入口明确拒绝且不落 match。"""
    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.matches.runner import MatchRunner
    from bzplat.backend.runtime.binary_runner import BinaryRunner
    from bzplat.backend.store import Store

    s = Store(str(tmp_path / "dup_nongame.db"))
    # 注册棋类 spec（若未注册则跳过——取决于测试环境是否注册 gomoku/pencil）
    non_holdem = None
    for gid in ("gomoku", "pencil"):
        if gid in registry.all_ids() and registry.get(gid).build_match_plan is None:
            non_holdem = gid
            break
    if non_holdem is None:
        pytest.skip("no non-holdem game registered (build_match_plan is None)")

    u = s.create_user("ngorg", "ng@e.com", "x")["id"]
    path_a = tmp_path / "unsupported-a"
    path_b = tmp_path / "unsupported-b"
    path_a.write_bytes(b"test fixture")
    path_b.write_bytes(b"test fixture")
    ba = s.create_bot(u, "ngA", binary_path=str(path_a), format="elf", game_id=non_holdem, is_active=1)["id"]
    bb = s.create_bot(u, "ngB", binary_path=str(path_b), format="elf", game_id=non_holdem, is_active=1)["id"]
    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)

    with pytest.raises(ValueError, match="不支持 duplicate"):
        asyncio.run(orch.challenge_duplicate(ba, bb, u, game_id=non_holdem))
    assert s.list_matches() == []
    s.close()
