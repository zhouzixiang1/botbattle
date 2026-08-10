#!/usr/bin/env python3
"""赛事名册容量造数 + 赛制静态估算，可选小规模真跑。

种子 N 个模拟用户 + 每人 1 个 holdem Bot（6 种策略分布），建赛事（指定模板），
admin 批量指派全部，并按模板公式打印轮次/场次估算；可选真跑（--run）。

用法：
  python scripts/contest_stress.py --users 500 --template holdem_swiss_ko
  python scripts/contest_stress.py --users 16 --template holdem_rr --run   # 小规模真跑验证排名

默认 dry-run：只建 draft 赛事、批量指派名册并读取详情；不会 publish/start，
因此不会生成 pairings，也不验证真实排期或吞吐。--run 才启动并等待真实对局
（带进度+超时，大规模如 swiss@500 约几小时，慎用）。

前置：worktree dev 服务在线；50380 与主 checkout botzone.db 会被硬拒绝。
"""
from __future__ import annotations

import argparse
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bzplat.backend.crypto import new_session_token, session_expires  # noqa: E402
from bzplat.backend.store import Store  # noqa: E402
from bzplat.backend.bots.manager import BotManager  # noqa: E402
from bzplat.backend.store.schema import ROLE_ADMIN, ROLE_USER  # noqa: E402
from scripts._qa_accounts import (  # noqa: E402
    QaAccountSpec,
    get_or_create_dedicated_account,
    preflight_dedicated_accounts,
)
from scripts._qa_target import (  # noqa: E402
    assert_qa_instance,
    ensure_qa_base,
    qa_db_path,
    qa_upload_root,
)

PASSWORD = "Stress1234"
EMAIL_DOMAIN = "contest.local"
PREFIX = "cs"  # 用户名前缀（便于识别清理）
CONTEST_ACCOUNT_NAMESPACE = "contest-stress-v1"
CONTEST_ADMIN_NAME = f"{PREFIX}_admin"
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


def contest_account_spec(username: str, role: str) -> QaAccountSpec:
    return QaAccountSpec(
        CONTEST_ACCOUNT_NAMESPACE,
        username,
        f"{username}@{EMAIL_DOMAIN}",
        PASSWORD,
        role,
    )


def seed(
    db_path: str,
    n_users: int,
    upload_root: str | None = None,
) -> tuple[dict[str, int], int, str]:
    """seed n_users 用户 + 每人 1 个 holdem Bot（策略均匀分布）。返回 (user_id→bot_id, admin_id, admin_token)。"""
    resolved_db = qa_db_path(db_path, ROOT)
    resolved_uploads = qa_upload_root(upload_root, resolved_db, ROOT)
    resolved_db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(resolved_db))
    store._conn.execute("PRAGMA busy_timeout=10000")
    user_specs = [
        contest_account_spec(f"{PREFIX}_u{i:03d}", ROLE_USER)
        for i in range(1, n_users + 1)
    ]
    admin_spec = contest_account_spec(CONTEST_ADMIN_NAME, ROLE_ADMIN)
    try:
        preflight_dedicated_accounts(store, [*user_specs, admin_spec])
    except Exception:
        store.close()
        raise
    bm = BotManager(store, upload_root=resolved_uploads)
    bin_bytes = [
        (ROOT / rel).read_bytes()
        for rel in STRATEGY_BOTS
        if (ROOT / rel).is_file()
    ]
    if not bin_bytes:
        raise SystemExit("找不到策略 Bot 二进制，先跑 bash samples/holdem_bots/gen.sh")

    uid2bot: dict[str, int] = {}
    for i in range(1, n_users + 1):
        uname = f"{PREFIX}_u{i:03d}"
        u = get_or_create_dedicated_account(
            store, contest_account_spec(uname, ROLE_USER)
        )
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

    # 仅使用脚本自己的 cs_admin，绝不复用 copied DB 的任意管理员。
    admin = get_or_create_dedicated_account(store, admin_spec)
    tok = new_session_token()
    store.add_session(tok, admin["id"], session_expires())
    store.close()
    print(
        f"=== 种子完成：{n_users} 用户 × 1 holdem Bot（{len(bin_bytes)} 种策略分布），"
        f"admin={admin['username']} uploads={resolved_uploads}"
    )
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
    ap = argparse.ArgumentParser(description="赛事名册容量造数 + 赛制静态估算；可选真跑")
    ap.add_argument("--base", default="http://127.0.0.1:50381")
    ap.add_argument("--db", default=os.environ.get("BZ_DB_PATH", "botzone.db"))
    ap.add_argument("--users", type=int, default=500)
    ap.add_argument(
        "--upload-root",
        default=None,
        help="Bot 产物目录（默认 <db.parent>/bot_uploads；相对路径也基于 db.parent）",
    )
    ap.add_argument("--template", default="holdem_swiss_ko",
                    choices=["holdem_swiss_ko", "holdem_rr"])
    ap.add_argument(
        "--run",
        action="store_true",
        help="真跑对局（默认 dry-run 只验证 draft 名册容量，不生成 pairings）",
    )
    ap.add_argument("--timeout", type=int, default=3600, help="--run 时的总超时秒数")
    args = ap.parse_args()

    base = ensure_qa_base(args.base)
    db_path = qa_db_path(args.db, ROOT)
    assert_qa_instance(base)

    print(f"\n=== 1. 种子 {args.users} 用户 ===")
    uid2bot, admin_id, tok = seed(str(db_path), args.users, args.upload_root)

    H = {"Authorization": f"Bearer {tok}"}
    c = httpx.Client(base_url=base, timeout=60, headers=H)

    print(f"\n=== 2. 赛制静态估算（template={args.template}, n={args.users}）===")
    est = estimate(args.template, args.users)
    print(f"  赛制：{est['format']}")
    print(f"  轮次：{est['rounds']}")
    print(f"  场次：{est['matches']}")
    if est["note"]:
        print(f"  注意：{est['note']}")
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
        print("\n=== 5. dry-run：验证 draft 名册容量（不发布、不生成 pairings）===")
        print("  上述场次仅为公式估算；如需生成并执行真实对阵，请加 --run")
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
                print(f"    {i}. {s.get('bot_name','?')} 积分={s.get('points',0)} W{ s.get('wins',0)}-D{s.get('draws',0)}-L{s.get('losses',0)} 累计分差={s.get('delta_total',0)}")
            print(f"\n  赛事页：{base}/#/contests/{cid}")
            return 0
        time.sleep(5)
    print(f"\n=== 超时（{args.timeout}s）未完成 ===")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
