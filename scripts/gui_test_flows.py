#!/usr/bin/env python3
"""GUI 测试配套的功能流程黑盒验证（API 层）。

IAB 浏览器对状态变更操作（goto/click）有 broker 路由不稳问题，但只读 DOM/screenshot 正常。
此脚本用 API（urllib）覆盖各用户角色的核心功能流程，与浏览器视觉检查互补：
- 分页正确性（各端点 page/per_page/total）
- 认证流程（登录/权限）
- 对战/赛事/人类对局/评论/通知
- admin 防护（B3 强删活跃 bot → 409）

用法：python scripts/gui_test_flows.py
（只读为主；会创建少量测试赛事/对局/评论——用户已确认允许脏数据）
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from _execution_request import (
    execution_request_path,
    require_execution_request,
    wait_for_execution_match,
)
from _qa_target import assert_qa_instance, qa_base

BASE = qa_base("http://127.0.0.1:50381")
assert_qa_instance(BASE)
PASS = 0
FAIL = 0
RESULTS: list[tuple[str, str, str]] = []  # (name, status, detail)


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    status = "PASS" if ok else "FAIL"
    if ok:
        PASS += 1
    else:
        FAIL += 1
    RESULTS.append((name, status, detail))
    print(f"[{status}] {name}" + (f" — {detail}" if detail else ""))


def api(method: str, path: str, *, token: str | None = None, body=None, expect=None):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            d = json.loads(r.read())
            if expect is not None and r.status != expect:
                return r.status, d
            return r.status, d
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read())
        except Exception:
            d = {"raw": str(e)}
        return e.code, d


def login(name: str, pw: str) -> str | None:
    _, cap = api("GET", "/api/auth/captcha")
    ans = cap.get("answer")
    if not ans:
        return None
    st, d = api("POST", "/api/auth/login", body={
        "username": name, "password": pw,
        "captcha_id": cap["captcha_id"], "captcha_answer": ans,
    })
    return d.get("token") if st == 200 else None


def execution_match_id(
    token: str,
    status: int,
    payload: dict,
    *,
    label: str,
    timeout: float = 120,
) -> tuple[str, str]:
    initial = require_execution_request(status, payload, label=label)

    def fetch(public_id: str):
        poll_status, poll_payload = api(
            "GET", execution_request_path(public_id), token=token
        )
        return poll_status, poll_payload, str(poll_payload)[:240]

    match_id = wait_for_execution_match(
        initial,
        fetch,
        label=label,
        timeout=timeout,
    )
    return str(initial["public_id"]), match_id


def assert_pagination(name: str, path: str, key: str, per_page: int = 5):
    """验证分页端点：page=1 返回 items + total，page=2 返回不同行。"""
    st1, d1 = api("GET", f"{path}&page=1&per_page={per_page}" if "?" in path else f"{path}?page=1&per_page={per_page}")
    if st1 != 200:
        check(name + " (fetch)", False, f"status {st1}: {d1}")
        return
    items1 = d1.get(key, [])
    total = d1.get("total")
    has_meta = "page" in d1 and "per_page" in d1 and total is not None
    check(name + " (shape: items+page+per_page+total)", has_meta,
          f"keys={list(d1.keys())} total={total}")
    if has_meta and total and total > per_page:
        st2, d2 = api("GET", f"{path}&page=2&per_page={per_page}" if "?" in path else f"{path}?page=2&per_page={per_page}")
        items2 = d2.get(key, [])
        ids1 = {x.get("id") for x in items1 if isinstance(x, dict)}
        ids2 = {x.get("id") for x in items2 if isinstance(x, dict)}
        check(name + " (page2 disjoint)", len(ids1 & ids2) == 0,
              f"page1={len(items1)} page2={len(items2)} overlap={ids1 & ids2}")
    check(name + " (per_page respected)", len(items1) <= per_page,
          f"got {len(items1)} expected <= {per_page}")


def main() -> int:
    print("=" * 60)
    print("GUI 测试配套：API 功能流程黑盒验证")
    print("=" * 60)

    # ── 访客（未登录）可访问的公开端点 ──
    print("\n--- 访客视角：公开端点 ---")
    check("首页加载", api("GET", "/api/matches?limit=5")[0] == 200)
    assert_pagination("排行榜分页", "/api/leaderboard?game_id=holdem", "leaderboard", per_page=5)
    assert_pagination("公开Bot分页", "/api/bots/public?game_id=holdem", "bots", per_page=5)
    st, d = api("GET", "/api/contests?per_page=3")
    check("赛事列表", st == 200 and "contests" in d)
    # 站点信息
    check("站点信息", api("GET", "/api/site/info")[0] == 200)
    # 三游戏段位曲线
    for g in ("holdem", "gomoku", "pencil"):
        check(f"段位曲线 {g}", api("GET", f"/api/tiers?game_id={g}")[0] == 200)

    # ── contest 27 详情（115 报名分页）──
    print("\n--- Contest 27 详情（115 报名分页）---")
    st, d = api("GET", "/api/contests/27?entries_page=1&entries_per_page=20")
    check("contest 27 detail", st == 200 and "contest" in d)
    if st == 200:
        check("entries 分页 total=115", d.get("entries_total") == 115, f"total={d.get('entries_total')}")
        check("entries page1=20条", len(d.get("entries", [])) == 20, f"got {len(d.get('entries', []))}")
        check("my_entry 字段存在", "my_entry" in d)
        st2, d2 = api("GET", "/api/contests/27?entries_page=6&entries_per_page=20")
        check("entries page6=15条(末页)", len(d2.get("entries", [])) == 15, f"got {len(d2.get('entries', []))}")

    # ── 登录测试（4 类用户）──
    print("\n--- 认证流程 ---")
    tok_tester1 = login("tester1", "Test1234")
    check("tester1 登录", tok_tester1 is not None)
    tok_tester2 = login("tester2", "Test1234")
    check("tester2 登录", tok_tester2 is not None)
    tok_org = login("guitest_org", "Test1234")
    check("guitest_org 登录", tok_org is not None)
    tok_admin = login("guitest_admin", "Test1234")
    check("guitest_admin 登录", tok_admin is not None)
    # 错误密码
    bad = login("tester1", "wrongpassword")
    check("错误密码拒绝", bad is None)

    # ── 普通用户视角 ──
    print("\n--- 普通用户 tester1 ---")
    if tok_tester1:
        assert_pagination("MyBots 分页", "/api/bots/mine", "bots", per_page=3)
        # 带 token 重测（需要登录态端点）
        st, d = api("GET", "/api/bots/mine?page=1&per_page=3", token=tok_tester1)
        check("MyBots(登录)", st == 200 and "bots" in d)
        # 通知分页
        st, d = api("GET", "/api/notifications?page=1&per_page=5", token=tok_tester1)
        check("通知分页", st == 200 and "total" in d, f"keys={list(d.keys())}")
        # 用户主页 bots
        st, d = api("GET", "/api/users/tester1/bots?page=1&per_page=5")
        check("用户主页 bots", st == 200 and "bots" in d)
        # bot 详情 + 对局历史分页
        st, d = api("GET", "/api/bots/2/matches?page=1&per_page=5")
        check("Bot对局历史分页", st == 200 and "total" in d, f"keys={list(d.keys())}")
        # 发起挑战（tester1 vs tester2）
        st, d = api("POST", "/api/matches/challenge", token=tok_tester1, body={
            "my_bot_id": 2, "opponent_bot_id": 3,
        })
        try:
            public_id, mid = execution_match_id(
                tok_tester1,
                st,
                d,
                label="GUI 流程挑战",
            )
        except Exception as exc:
            check("发起对战(202 + match_id)", False, str(exc))
        else:
            check(
                "发起对战(202 + match_id)",
                True,
                f"public_id={public_id} match_id={mid}",
            )
        # 评论
        st, d = api("GET", "/api/comments?target_type=match&target_id=20260804213844-3ecdfb1c&page=1&per_page=5")
        check("评论分页", st == 200 and "total" in d, f"keys={list(d.keys())}")

    # ── 组织者视角 ──
    print("\n--- 组织者 guitest_org ---")
    if tok_org:
        # 创建赛事
        st, d = api("POST", "/api/contests", token=tok_org, body={
            "title": "[GUI测试]组织者创建赛", "game_id": "holdem",
            "template_id": "holdem_swiss_ko",
        })
        cid = d.get("id") if st == 200 else None
        check("组织者创建赛事", st == 200 and cid is not None, f"status={st} {str(d)[:100]}")
        if cid:
            # 开放报名
            check("开放报名", api("POST", f"/api/contests/{cid}/open", token=tok_org)[0] == 200)
            # 报名（用 tester1 的 bot）
            st, d = api("POST", f"/api/contests/{cid}/entries", token=tok_tester1, body={"bot_id": 2})
            check("tester1 报名赛事", st == 200, f"status={st} {str(d)[:80]}")
            # 出排期（publish）
            st, d = api("POST", f"/api/contests/{cid}/publish", token=tok_org)
            check("出排期 publish", st == 200, f"status={st} {str(d)[:100]}")
            # 详情有 pairings
            st, d = api("GET", f"/api/contests/{cid}")
            check("publish后有排期", st == 200 and len(d.get("pairings", [])) > 0,
                  f"pairings={len(d.get('pairings', []))}")

    # ── 管理员视角 ──
    print("\n--- 管理员 guitest_admin ---")
    if tok_admin:
        assert_pagination("admin users", "/api/admin/users", "users", per_page=5)
        # admin bots/contests 已支持分页
        st, d = api("GET", "/api/admin/bots?page=1&per_page=5", token=tok_admin)
        check("admin bots 分页", st == 200 and "total" in d, f"keys={list(d.keys())}")
        st, d = api("GET", "/api/admin/contests?page=1&per_page=5", token=tok_admin)
        check("admin contests 分页", st == 200 and "total" in d, f"keys={list(d.keys())}")
        # admin 日志
        check("admin 日志", api("GET", "/api/admin/logs?limit=5", token=tok_admin)[0] == 200)
        # B3 防护：强删活跃 bot → 409
        # 先建一个 pending 对局的 bot
        st, d = api("POST", "/api/matches/challenge", token=tok_tester1, body={
            "my_bot_id": 2, "opponent_bot_id": 786,
        })
        try:
            public_id, active_mid = execution_match_id(
                tok_tester1,
                st,
                d,
                label="B3 活跃 Bot 挑战",
            )
        except Exception as exc:
            check("B3 前置挑战进入队列", False, str(exc))
        else:
            # dispatcher 已建立 match；running/pending 引用均应阻止强删。
            st, d = api("DELETE", "/api/admin/bots/2", token=tok_admin)
            check(
                "B3 强删活跃bot→409",
                st == 409,
                f"status={st} public_id={public_id} match_id={active_mid} {str(d)[:80]}",
            )
        # 强删无引用的 bot → 允许（用测试上传的 bot 1906，它无对局）
        st, d = api("DELETE", "/api/admin/bots/1906", token=tok_admin)
        check("强删无引用bot→200", st == 200, f"status={st} {str(d)[:80]}")
        # 权限：普通用户不能访问 admin
        st, d = api("GET", "/api/admin/users", token=tok_tester1)
        check("普通用户禁访admin", st in (401, 403), f"status={st}")

    # ── 权限/IDOR 抽查 ──
    print("\n--- 权限抽查 ---")
    # 未登录访问 admin → 401
    check("未登录禁访admin", api("GET", "/api/admin/users")[0] in (401, 403))
    # 未登录访问 my-bots → 401
    check("未登录禁访MyBots", api("GET", "/api/bots/mine")[0] in (401, 403))

    # ── 总结 ──
    print("\n" + "=" * 60)
    print(f"总计：{PASS} PASS / {FAIL} FAIL")
    print("=" * 60)
    if FAIL:
        print("\n失败项：")
        for n, s, d in RESULTS:
            if s == "FAIL":
                print(f"  - {n}: {d}")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
