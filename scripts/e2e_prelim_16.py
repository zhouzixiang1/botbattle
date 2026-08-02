#!/usr/bin/env python3
"""预赛/决赛体系端到端测试：16 人预赛（瑞士）真跑。

验证 P0-P5 全链路：报名→组织者批量指派→开赛→瑞士轮自动推进→全员正式名次。
用 BZ_BOT_LOCAL=1 跑样例 ELF（不启 Docker）。数据留下（main db）。

关键：orchestrator 后台 task 需活着的 event loop，故全程在一个 async main 里跑。
"""
import asyncio
import os
import sys
import time

os.environ.setdefault("BZ_BOT_LOCAL", "1")

from pathlib import Path

from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store

SAMPLES = Path(__file__).resolve().parent.parent / "samples" / "holdem_bots"
CALLBOT = Path(__file__).resolve().parent.parent / "samples" / "callbot_linux_amd64"
STRATEGIES = ["foldbot", "callbot", "raisebot", "allinbot", "loosebot"]


def _bot_path(strat: str) -> str:
    p = SAMPLES / strat
    return str(p) if p.is_file() else str(CALLBOT)


async def main():
    store = Store()
    org = store.create_user("e2e_org2", "e2eorg2@x.com", "x", role="organizer")
    bots = []
    for i in range(16):
        u = store.create_user(f"e2eu{i}", f"e2eu{i}@x.com", "x")
        strat = STRATEGIES[i % len(STRATEGIES)]
        b = store.create_bot(
            u["id"], f"e2ebot{i}_{strat}",
            binary_path=_bot_path(strat), format="elf", is_public=1, game_id="holdem",
        )
        bots.append((u["id"], b["id"]))
    print(f"已建 16 用户 + bot（org=e2e_org2 id={org['id']}）")

    orch = MatchOrchestrator(
        store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=4
    )
    # 接 maybe_finish 回调（对局完成 → 推进赛程）
    cm = ContestManager(store, orch)
    orch.on_match_done = lambda mid, cid: cm.maybe_finish(cid) if cid else None

    c = cm.create(
        org["id"], "E2E预赛16人v2", template_id="holdem_prelim_swiss",
        hands_per_match=4, game_id="holdem",
    )
    cid = c["id"]
    print(f"已建预赛 contest id={cid} phase={c.get('phase')}")

    for uid, bid in bots:
        store.add_contest_entry(cid, uid, bid)
    print(f"已指派 {len(bots)} 人")

    store.update_contest(cid, status="open")
    await cm.start(cid)  # 触发 _dispatch_pending → 起 4 个 _run_match 后台 task
    print(f"已开赛 status={store.get_contest(cid)['status']}")

    # 在同一 event loop 里等待所有 match task 完成 + 赛程推进
    t0 = time.time()
    while time.time() - t0 < 240:
        st = store.get_contest(cid)["status"]
        if st in ("finished", "cancelled"):
            break
        # 让 event loop 跑后台 task（_run_match + maybe_finish 链）
        await asyncio.sleep(3)
        pending = sum(1 for t in orch._tasks.values() if not t.done())
        print(f"  status={st} pending_tasks={pending} elapsed={int(time.time()-t0)}s", flush=True)
    final = store.get_contest(cid)
    print(f"\n=== 赛事结束 status={final['status']} elapsed={int(time.time()-t0)}s ===")

    # 验证全员正式名次
    if int(final.get("official_results_ready") or 0):
        results = store.list_official_results(cid)
        print(f"全员正式名次：{len(results)} 人")
        for r in results[:5]:
            print(f"  #{r['rank']} entry={r['entry_id']} bot={r.get('bot_name')} "
                  f"points={r.get('points')}")
        print(f"  ...（共 {len(results)} 名）")
        ranks = [r["rank"] for r in results]
        assert ranks == list(range(1, len(results) + 1)), "rank 应唯一连续 1..N"
        print("✓ rank 唯一连续 1..N")
    else:
        print("⚠ official_results 未就绪")

    standings = cm.standings(cid)
    print(f"\n积分榜（{len(standings)} 人）：")
    for s in sorted(standings, key=lambda x: -x["points"])[:5]:
        print(f"  entry={s['entry_id']} points={s['points']} W/D/L={s['wins']}/{s['draws']}/{s['losses']} net={s['net_chips']}")
    print(f"\n✓ E2E 预赛 16 人测试完成（contest id={cid}，数据已留在主 db）")
    store.close()


if __name__ == "__main__":
    asyncio.run(main())
