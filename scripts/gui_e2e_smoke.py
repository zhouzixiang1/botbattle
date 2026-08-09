#!/usr/bin/env python3
"""GUI 端到端冒烟测试的 API 验证脚本。

验证三角色（玩家/组织者/管理员）的核心业务路径在 API 层端到端通畅。
不依赖浏览器（真实 Chromium 回归与隔离要求见 doc/TESTING.md），
聚焦后端功能闭环与角色边界。

用法（在 worktree 根目录，worktree 后端已起在 50381 且 BZ_SKIP_CAPTCHA=1）：
    python scripts/gui_e2e_smoke.py --base http://127.0.0.1:50381

前置：scripts/seed_test_accounts.py 已跑（tester1/tester2 各 3 bot），
      且 seed 脚本已建 qa_organizer(organizer) / qa_admin(admin) 账号。
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
import urllib.error

from _qa_target import assert_qa_instance, ensure_qa_base

PASS = "\033[32m✓\033[0m"
FAIL = "\033[31m✗\033[0m"
fails: list[str] = []


def req(base: str, method: str, path: str, token: str = "", body: dict | None = None) -> dict:
    url = base + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = json.dumps(body).encode() if body else None
    r = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"_status": e.code, "_body": e.read().decode()[:200]}
    except Exception as e:  # noqa: BLE001
        return {"_error": str(e)}


def login(base: str, username: str, password: str) -> str:
    """BZ_SKIP_CAPTCHA=1 时任意 captcha 即可登录。"""
    d = req(base, "POST", "/api/auth/login", body={
        "username": username, "password": password,
        "captcha_id": "smoke", "captcha_answer": "skip",
    })
    assert d.get("token"), f"登录失败 {username}: {d}"
    return d["token"]


def check(name: str, ok: bool, detail: str = "") -> None:
    mark = PASS if ok else FAIL
    print(f"  {mark} {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        fails.append(name)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:50381")
    ap.add_argument("--player", default="tester1")
    ap.add_argument("--player-pw", default="Test1234")
    ap.add_argument("--organizer", default="qa_organizer")
    ap.add_argument("--admin", default="qa_admin")
    args = ap.parse_args()
    base = ensure_qa_base(args.base)
    assert_qa_instance(base)

    print("=== 视角 A：普通玩家 ===")
    t = login(base, args.player, args.player_pw)
    check("登录", True)

    d = req(base, "GET", "/api/bots/mine", token=t)
    bots = d.get("bots", [])
    check("我的 Bot 列表", len(bots) >= 3, f"{len(bots)} 个")

    holdem = next((b for b in bots if b.get("game_id") == "holdem"), None)
    if holdem:
        # 找另一个 owner 的 bot 做对手（不能与自己对战）
        search = req(base, "GET", "/api/search?q=tester2&type=bots", token=t)
        opp_bots = search.get("bots") or search.get("results", {}).get("bots", [])
        opp = next((b for b in opp_bots if b.get("game_id") == "holdem"), None)
        if opp:
            d2 = req(base, "POST", "/api/matches/challenge", token=t, body={
                "game_id": "holdem", "my_bot_id": holdem["id"],
                "opponent_bot_id": opp["id"],
            })
            mid = d2.get("match_id") or d2.get("id")
            check("发起挑战", bool(mid), str(mid or d2.get("_body", ""))[:60])
        else:
            check("发起挑战", False, "找不到对手 bot")

    check("收藏 Bot", req(base, "POST", "/api/bots/1/favorite", token=t).get("ok"))
    profile = req(base, "PUT", "/api/auth/profile", token=t, body={"display_name": "E2E"})
    check("改资料", profile.get("user", {}).get("display_name") == "E2E", str(profile)[:80])

    print("\n=== 视角 B：组织者 ===")
    to = login(base, args.organizer, "Test1234")
    check("登录", True)
    # 组织者应能建赛事
    dc = req(base, "POST", "/api/contests", token=to, body={
        "title": "smoke 赛事", "description": "脚本测试",
        "template_id": "holdem_swiss_ko", "game_id": "holdem",
        "require_real_name": False,
    })
    contest = dc.get("contest") or dc
    check("创建赛事", bool(contest.get("id")), str(contest.get("id") or dc.get("_body", ""))[:60])

    print("\n=== 视角 C：管理员 ===")
    ta = login(base, args.admin, "Test1234")
    check("登录", True)
    stats = req(base, "GET", "/api/admin/stats", token=ta)
    check("admin 统计", stats.get("users") is not None, str(stats)[:80])
    check("admin 用户列表", isinstance(req(base, "GET", "/api/admin/users", token=ta).get("users"), list))

    print("\n=== 角色边界（403 守卫）===")
    # 玩家不能建赛事
    r = req(base, "POST", "/api/contests", token=t, body={
        "title": "x", "template_id": "holdem_swiss_ko", "game_id": "holdem",
        "require_real_name": False,
    })
    check("玩家建赛事被拒(403)", r.get("_status") == 403, f"status={r.get('_status')}")
    # 玩家不能访问 admin 接口
    r = req(base, "GET", "/api/admin/users", token=t)
    check("玩家访问 admin 接口被拒", r.get("_status") in (403, 401), f"status={r.get('_status')}")

    print("\n" + "=" * 40)
    if fails:
        print(f"{FAIL} {len(fails)} 项失败: {fails}")
        return 1
    print(f"{PASS} 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
