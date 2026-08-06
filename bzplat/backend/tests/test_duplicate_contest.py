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

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
FOLDBOT = SAMPLES / "holdem_bots" / "foldbot"
CALLBOT = SAMPLES / "callbot_linux_amd64"


def test_holdem_dup_rr_template_exists():
    """holdem_dup_rr 模板存在 + 阶段标 duplicate=True。"""
    tpl = next((t for t in TEMPLATES if t["id"] == "holdem_dup_rr"), None)
    assert tpl is not None, "应有 holdem_dup_rr 复式赛制模板"
    assert tpl["stages"][0].get("duplicate") is True, "dup_rr 阶段应标 duplicate=True"


def test_challenge_duplicate_flag_persisted_to_match_config(tmp_path):
    """challenge_duplicate 落的 match 行 match_config 含 duplicate=True（不真跑 bot）。
    用 /tmp 伪路径建 match（不 await _run_match），仅校验落库的 config 标志。
    """
    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.matches.runner import MatchRunner
    from bzplat.backend.runtime.binary_runner import BinaryRunner
    from bzplat.backend.store import Store
    import json as _json

    s = Store(str(tmp_path / "dup_cfg.db"))
    u = s.create_user("orgcfg", "c@e.com", "x")["id"]
    ba = s.create_bot(u, "cfgbotA", binary_path="/tmp/a", format="elf", game_id="holdem", is_active=1)["id"]
    bb = s.create_bot(u, "cfgbotB", binary_path="/tmp/b", format="elf", game_id="holdem", is_active=1)["id"]
    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)

    async def _go():
        # challenge_duplicate 立即返回（建 match 行 + 起 task）；不 await task 避免 bot 崩
        mid = await orch.challenge_duplicate(ba, bb, u, duplicate_seed=42)
        # 取刚建的 match 行（task 可能已在跑，但 match_config 已落库）
        m = s.get_match(mid)
        return m

    m = asyncio.run(_go())
    mc = m["match_config"]
    if isinstance(mc, str):
        mc = _json.loads(mc or "{}")
    assert mc.get("duplicate") is True, "challenge_duplicate 落的 match_config 应含 duplicate=True"
    assert m.get("match_seed") == 42, "duplicate_seed 应落 match_seed 列"
    # 取消后台 task，避免 bot 崩污染日志
    for t in list(orch._tasks.values()):
        t.cancel()
    s.close()


@pytest.mark.skipif(not (FOLDBOT.is_file() and CALLBOT.is_file()),
                    reason="foldbot/callbot binary missing")
def test_duplicate_contest_one_merged_match_zero_sum(tmp_path):
    """端到端：2 选手 duplicate 赛事 → 每对阵 1 条 merged match，deltas 零和，winner 正确。"""
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
        await cm.start(c)
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

    # 2. 每对阵只产 1 条 merged match（2 人 round_robin = 1 场）
    ps = s.list_contest_pairings(c, stage_idx=0)
    real_pairings = [p for p in ps if p.get("match_id")]
    assert len(real_pairings) == 1, f"2 人 round_robin 应 1 场对阵，实有 {len(real_pairings)}"
    mid = real_pairings[0]["match_id"]
    m = s.get_match(mid)
    assert m["status"] == "completed", f"match 应 completed，实为 {m['status']}"

    # 3. merged deltas 零和（2 leg 累加，seat_swap 翻转后仍零和）
    ea, eb = match_deltas(m)
    assert ea + eb == 0, f"merged deltas 应零和，实 ea={ea} eb={eb}（和={ea+eb}）"

    # 4. winner 按 merged net 判：foldbot 每手弃 → callbot 净筹码高 → callbot 侧胜。
    # callbot 是 bot_b（pairing 的 b 侧 / seat 1）。
    w = m["winner"]
    assert w is not None, f"duplicate winner 不应是 None（平局）；result={m.get('result')}"
    assert w == 1, f"callbot (seat1) 应胜（foldbot 每手弃），winner={w}"

    # 5. match_seed 落库（确定性回放）
    assert m.get("match_seed") is not None, "duplicate 对局应落 match_seed"

    s.close()


@pytest.mark.skipif(not (FOLDBOT.is_file() and CALLBOT.is_file()),
                    reason="foldbot/callbot binary missing")
def test_duplicate_standings_ordered_by_merged_deltas(tmp_path):
    """standings 按 merged deltas 排序：callbot 选手积分/净筹码高于 foldbot 选手。"""
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
        await cm.start(c)
        tasks = list(orch._tasks.values())
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=180)
        await cm.maybe_finish(c)

    asyncio.run(_run())

    rows = cm.standings(c)
    assert len(rows) == 2, f"应有 2 选手 standings，实有 {len(rows)}"
    # 排行按 points 降序（同 points 时 net_chips 降序）。callbot 胜 → 3 分 + 正净筹码。
    by_bot = {r["bot_id"]: r for r in rows}
    call_row = by_bot[bb]
    fold_row = by_bot[ba]
    assert call_row["points"] > fold_row["points"], (
        f"callbot 选手积分应更高：call={call_row['points']} fold={fold_row['points']}"
    )
    assert call_row["net_chips"] > fold_row["net_chips"], (
        f"callbot 选手净筹码应更高：call={call_row['net_chips']} fold={fold_row['net_chips']}"
    )
    # 零和：两人 net_chips 互为相反数
    assert call_row["net_chips"] + fold_row["net_chips"] == 0, "双人 net_chips 应零和"
    s.close()


def test_duplicate_flag_ignored_for_non_holdem(tmp_path):
    """棋类（无 build_match_plan）即便误标 duplicate 也走单 leg（不破坏现有赛制）。

    用伪 bot（不真跑）+ 直接验 challenge_duplicate 内部降级：返回的 match_config
    不含 duplicate（spec.build_match_plan is None → 降级）。
    """
    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.matches.runner import MatchRunner
    from bzplat.backend.runtime.binary_runner import BinaryRunner
    from bzplat.backend.store import Store
    import json as _json

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
    ba = s.create_bot(u, "ngA", binary_path="/tmp/a", format="elf", game_id=non_holdem, is_active=1)["id"]
    bb = s.create_bot(u, "ngB", binary_path="/tmp/b", format="elf", game_id=non_holdem, is_active=1)["id"]
    orch = MatchOrchestrator(s, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)

    async def _go():
        mid = await orch.challenge_duplicate(ba, bb, u, game_id=non_holdem)
        return s.get_match(mid)

    m = asyncio.run(_go())
    mc = m["match_config"]
    if isinstance(mc, str):
        mc = _json.loads(mc or "{}")
    # 棋类不支持 duplicate → challenge 内部降级（duplicate=False），match_config 不含 duplicate 标志
    assert not mc.get("duplicate"), (
        f"棋类 {non_holdem} 不支持 duplicate，应降级为单 leg，match_config={mc}"
    )
    for t in list(orch._tasks.values()):
        t.cancel()
    s.close()
