#!/usr/bin/env python3
"""赛事功能可行性 + 多赛制验证工具（不是纯压测）。

种子 N 个模拟用户 + 每人 1 个 holdem Bot（6 种策略分布），建赛事（指定模板），
admin 批量指派全部，生成赛程表（打印各轮场次/估计时长），可选真跑（--run）。

用法：
  python scripts/contest_stress.py --users 500 --template holdem_swiss_ko
  python scripts/contest_stress.py --users 16 --template holdem_rr --run   # 小规模真跑验证排名

默认 dry-run：只建/指派/生成赛程表，不真跑对局（验证建/指派/展示可行性）。
--run 才真执行对局（带进度+超时，大规模如 swiss@500 约几小时，慎用）。

前置：dev 服务在线（scripts/platform-ctl.sh status）。
"""
from __future__ import annotations

import argparse
import math
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bzplat.backend.crypto import hash_password, new_session_token, session_expires  # noqa: E402
from bzplat.backend.store import Store  # noqa: E402
from bzplat.backend.bots.manager import BotManager  # noqa: E402

PASSWORD = "Stress1234"
EMAIL_DOMAIN = "contest.local"
PREFIX = "cs"  # 用户名前缀（便于识别清理）
# 6 种策略 Bot 二进制（均匀分布）
STRATEGY_BOTS = [
    "samples/holdem_bots/foldbot",
    "samples/callbot_linux_amd64",        # callbot（在 samples 根）
    "samples/holdem_bots/loosebot",
    "samples/holdem_bots/tightbot",
    "samples/holdem_bots/raisebot",
    "samples/holdem_bots/allinbot",
    "samples/holdem_bots/randombot",
]


def seed(db_path: str, n_users: int) -> tuple[dict[str, int], int, str]:
    """seed n_users 用户 + 每人 1 个 holdem Bot（策略均匀分布）。返回 (user_id→bot_id, admin_id, admin_token)。"""
    store = Store(db_path)
    store._conn.execute("PRAGMA busy_timeout=10000")
    bm = BotManager(store, upload_root="bot_uploads")
    bin_bytes = [Path(b).read_bytes() for b in STRATEGY_BOTS if Path(b).is_file()]
    if not bin_bytes:
        raise SystemExit("找不到策略 Bot 二进制，先跑 bash samples/holdem_bots/gen.sh")

    uid2bot: dict[str, int] = {}
    for i in range(1, n_users + 1):
        uname = f"{PREFIX}_u{i:03d}"
        u = store.get_user_by_username(uname)
        if not u:
            u = store.create_user(uname, f"{uname}@{EMAIL_DOMAIN}", hash_password(PASSWORD), display_name=uname)
            store.update_user(u["id"], email_verified=1)
        # 每用户 1 个 holdem bot，策略按 i 轮转分布
        raw = bin_bytes[(i - 1) % len(bin_bytes)]
        bname = f"{uname}_holdem"
        existing = store.get_bot_by_owner_name(u["id"], bname)
        if existing:
            b = existing
        else:
            b = bm.create_from_upload(u["id"], bname, raw, display_name=bname, game_id="holdem")
        store.ensure_rating(b["id"])
        uid2bot[uname] = b["id"]

    # admin（复用现有或建 cs_admin）
    admin = None
    for cand in ("admin", "load_admin", f"{PREFIX}_admin"):
        u = store.get_user_by_username(cand)
        if u and u.get("role") == "admin":
            admin = u
            break
    if not admin:
        admin = store.create_user(f"{PREFIX}_admin", f"{PREFIX}_admin@{EMAIL_DOMAIN}", hash_password(PASSWORD), role="admin")
        store.update_user(admin["id"], email_verified=1)
    tok = new_session_token()
    store.add_session(tok, admin["id"], session_expires())
    store.close()
    print(f"=== 种子完成：{n_users} 用户 × 1 holdem Bot（{len(bin_bytes)} 种策略分布），admin={admin['username']}")
    return uid2bot, admin["id"], tok


def estimate(template: str, n: int) -> dict[str, Any]:
    """估算各赛制场次（不含 holdem 单手时间）。返回 {format, rounds, matches, note}。"""
    t = template
    if "swiss" in t and "ko" in t:
        swiss_r = max(1, math.ceil(math.log2(max(2, n))))
        swiss_m = swiss_r * (n // 2)
        adv = 8  # 默认 advance_count
        ko_m = adv - 1
        return {"format": "swiss→单败", "rounds": f"swiss {swiss_r}轮 + 淘汰", "matches": swiss_m + ko_m,
                "note": f"swiss {swiss_m}场 + 淘汰 {ko_m}场（advance={adv}）"}
    if t == "holdem_rr":
        return {"format": "单循环", "rounds": f"{n-1}轮", "matches": n * (n - 1) // 2,
                "note": "FULL_RR_MAX_N=12 护栏会拒 n>12；500 人不可行"}
    if t == "holdem_swiss_only":
        swiss_r = max(1, math.ceil(math.log2(max(2, n))))
        return {"format": "纯瑞士", "rounds": f"{swiss_r}轮", "matches": swiss_r * (n // 2), "note": ""}
    if t == "holdem_elim_only":
        return {"format": "单败淘汰", "rounds": f"{math.ceil(math.log2(n))}轮", "matches": n - 1, "note": ""}
    return {"format": t, "rounds": "?", "matches": "?", "note": "未知模板"}


def main():
    ap = argparse.ArgumentParser(description="赛事功能可行性 + 多赛制验证")
    ap.add_argument("--base", default="http://127.0.0.1:50380")
    ap.add_argument("--db", default="botzone.db")
    ap.add_argument("--users", type=int, default=500)
    ap.add_argument("--template", default="holdem_swiss_ko",
                    choices=["holdem_swiss_ko", "holdem_rr"])
    ap.add_argument("--run", action="store_true", help="真跑对局（默认 dry-run 只生成赛程表）")
    ap.add_argument("--timeout", type=int, default=3600, help="--run 时的总超时秒数")
    args = ap.parse_args()

    print(f"\n=== 1. 种子 {args.users} 用户 ===")
    uid2bot, admin_id, tok = seed(args.db, args.users)

    base = args.base.rstrip("/")
    H = {"Authorization": f"Bearer {tok}"}
    c = httpx.Client(base_url=base, timeout=60, headers=H)

    print(f"\n=== 2. 赛制可行性估算（template={args.template}, n={args.users}）===")
    est = estimate(args.template, args.users)
    print(f"  赛制：{est['format']}")
    print(f"  轮次：{est['rounds']}")
    print(f"  场次：{est['matches']}")
    if est["note"]:
        print(f"  注意：{est['note']}")
    if isinstance(est["matches"], int):
        print(f"  @8并发 ≈ {est['matches'] // 8} 批（每批 holdem 70 手约数分钟）")

    print(f"\n=== 3. 建赛事（template={args.template}）===")
    body: dict[str, Any] = {"title": f"压测赛 {args.users}人 {args.template}", "game_id": "holdem", "template_id": args.template}
    r = c.post("/api/contests", json=body)
    if r.status_code != 200:
        print(f"  建赛失败：{r.status_code} {r.text[:200]}")
        return 1
    cid = r.json()["contest"]["id"]
    print(f"  赛事 id={cid} status={r.json()['contest']['status']}")

    print(f"\n=== 4. admin 批量指派全部 {args.users} 用户 ===")
    r = c.post(f"/api/admin/contests/{cid}/entries/bulk", json={"assign_all": True, "game_id": "holdem", "name_prefix": PREFIX})
    if r.status_code != 200:
        print(f"  指派失败：{r.status_code} {r.text[:200]}")
        return 1
    print(f"  指派完成：added={r.json()['added']} total_entries={r.json()['total_entries']} skipped={len(r.json().get('skipped', []))}")

    if not args.run:
        print(f"\n=== 5. dry-run：查看赛程表（不真跑）===")
        print("  如需真跑加 --run（大规模 swiss@500 约几小时）")
        r = c.get(f"/api/contests/{cid}")
        if r.status_code == 200:
            d = r.json()
            print(f"  赛事状态：{d['contest']['status']}")
            print(f"  报名数：{len(d.get('entries', []))}")
            print(f"  阶段：{[s.get('key') for s in d.get('stages', [])]}")
            print(f"  估计场次：{d.get('estimate', est['matches'])}")
        print(f"\n  赛事页：{base}/#/contests/{cid}")
        return 0

    print(f"\n=== 5. 真跑：启动赛事（start），等待完成（超时 {args.timeout}s）===")
    r = c.post(f"/api/contests/{cid}/start")
    if r.status_code != 200:
        print(f"  启动失败：{r.status_code} {r.text[:200]}")
        return 1
    print(f"  已启动，状态={r.json()['contest']['status']}")

    t0 = time.time()
    last_pairs = -1
    while time.time() - t0 < args.timeout:
        r = c.get(f"/api/contests/{cid}")
        if r.status_code != 200:
            time.sleep(5); continue
        d = r.json()
        st = d["contest"]["status"]
        pairs = d.get("pairings", [])
        done = sum(1 for p in pairs if p.get("status") == "completed")
        if len(pairs) != last_pairs or done % 10 == 0:
            print(f"  [{int(time.time()-t0)}s] status={st} 对阵={len(pairs)} 已完成={done}")
            last_pairs = len(pairs)
        if st in ("finished", "cancelled"):
            print(f"\n=== 赛事结束：{st}（耗时 {int(time.time()-t0)}s）===")
            # 打印前 10 名
            standings = d.get("standings", [])
            print("  前 10 名：")
            for i, s in enumerate(standings[:10], 1):
                print(f"    {i}. {s.get('bot_name','?')} 积分={s.get('points',0)} W{ s.get('wins',0)}-D{s.get('draws',0)}-L{s.get('losses',0)} 净筹码={s.get('net_chips',0)}")
            print(f"\n  赛事页：{base}/#/contests/{cid}")
            return 0
        time.sleep(5)
    print(f"\n=== 超时（{args.timeout}s）未完成 ===")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
