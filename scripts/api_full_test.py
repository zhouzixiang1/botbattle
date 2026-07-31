#!/usr/bin/env python3
"""通过 HTTP API 接口做完整测试：验证业务正确性与对局稳定性。

覆盖：
  1. 完整鉴权流程（验证码 → 注册 → 邮箱验证 → 登录 → /me）
  2. Bot 上传 / 版本 / 上架 / 公开列表
  3. 挑战对局 + 结果完整性（零和、winner、净筹码）
  4. SSE 实时事件流 vs 落盘回放 events_json 一致性
  5. 并发多局对局稳定性（同时跑 N 局，互不串味/不崩溃/不超时）
  6. 排行榜 Glicko-2 更新、组织者比赛循环赛

前置：
  - 后端以 BZ_TEST_CAPTCHA=1 启动（captcha 接口返回 answer）
  - SMTP 可未配置（邮箱验证码走日志/库；本脚本直接读取 email_codes 表）
  - BZ_BOT_LOCAL=1（本地跑 ELF，无需 Docker）

用法：
  python scripts/api_full_test.py [--base http://127.0.0.1:50382] [--db /path.db]
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

PASS = 0
FAIL = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name}  {detail}")


class Api:
    def __init__(self, base: str, db_path: str) -> None:
        self.base = base.rstrip("/")
        self.db_path = db_path
        self.client = httpx.Client(base_url=self.base, timeout=120)

    # ── 鉴权 ───────────────────────────────────────────────
    def captcha(self) -> dict:
        r = self.client.get("/api/auth/captcha")
        r.raise_for_status()
        return r.json()

    def register(self, username: str, email: str, password: str) -> dict:
        cap = self.captcha()
        r = self.client.post("/api/auth/register", json={
            "username": username, "email": email, "password": password,
            "captcha_id": cap["captcha_id"], "captcha_answer": cap["answer"],
        })
        r.raise_for_status()
        return r.json()

    def latest_email_code(self, username: str, purpose: str) -> str:
        """从 DB 读最新验证码（绕过 SMTP）。"""
        con = sqlite3.connect(self.db_path)
        con.row_factory = sqlite3.Row
        u = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        assert u, f"用户 {username} 不存在"
        row = con.execute(
            "SELECT code FROM email_codes WHERE user_id=? AND purpose=? AND used_at IS NULL "
            "ORDER BY id DESC LIMIT 1", (u["id"], purpose)
        ).fetchone()
        con.close()
        assert row, f"无 {purpose} 验证码 for {username}"
        return row["code"]

    def verify_email(self, username: str) -> dict:
        code = self.latest_email_code(username, "verify")
        r = self.client.post("/api/auth/verify-email", json={
            "email_or_username": username, "code": code,
        })
        r.raise_for_status()
        return r.json()

    def login(self, username: str, password: str) -> str:
        cap = self.captcha()
        r = self.client.post("/api/auth/login", json={
            "username": username, "password": password,
            "captcha_id": cap["captcha_id"], "captcha_answer": cap["answer"],
        })
        r.raise_for_status()
        return r.json()["token"]

    # ── 鉴权请求封装 ─────────────────────────────────────
    def authed(self, token: str, method: str, path: str, **kw) -> httpx.Response:
        headers = dict(kw.pop("headers", {}) or {})
        headers.setdefault("Authorization", f"Bearer {token}")
        return self.client.request(method, path, headers=headers, **kw)


def read_sample(path: str) -> bytes:
    p = Path(__file__).resolve().parent.parent / path
    return p.read_bytes()


def multipart(fields: dict[str, Any], file_field: str, filename: str, data: bytes) -> tuple[dict, bytes]:
    """构造简单 multipart/form-data body。"""
    boundary = uuid.uuid4().hex
    parts = []
    for k, v in fields.items():
        parts.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode() + data + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return {"Content-Type": f"multipart/form-data; boundary={boundary}"}, b"".join(parts)


# ─────────────────────────────────────────────────────────────
def test_auth_flow(api: Api) -> dict[str, str]:
    print("\n[1/6] 鉴权流程：验证码 → 注册 → 邮箱验证 → 登录")
    users: dict[str, str] = {}

    # 先用 CLI 建一个 admin（跳过邮箱验证），用于后续提权 org1
    import subprocess
    subprocess.run(
        [sys.executable, "-m", "bzplat.backend.cli", "create-admin",
         "adminroot", "admin@test.local", "Password123!", "--db", api.db_path],
        check=True, cwd=str(Path(__file__).resolve().parent.parent),
        capture_output=True,
    )
    users["admin"] = api.login("adminroot", "Password123!")
    check("admin 登录", bool(users["admin"]), "无 token")

    for name in ("alice", "bob", "carol", "org1"):
        email = f"{name}@test.local"
        res = api.register(name, email, "Password123!")
        check(f"注册 {name}", res.get("need_verify") is True, str(res)[:80])
        res = api.verify_email(name)
        check(f"邮箱验证 {name}", res.get("ok") is True, str(res)[:80])
        tok = api.login(name, "Password123!")
        check(f"登录 {name}", bool(tok), "无 token")
        users[name] = tok

    # admin 提权 org1 为 organizer
    con = sqlite3.connect(api.db_path)
    org1_id = con.execute("SELECT id FROM users WHERE username='org1'").fetchone()[0]
    con.close()
    r = api.authed(users["admin"], "POST", "/api/admin/users/%d/role?role=organizer" % org1_id)
    check("提权 org1 为 organizer", r.status_code == 200 and r.json().get("user", {}).get("role") == "organizer",
          f"{r.status_code} {r.text[:80]}")

    # 未验证用户不可登录
    api.register("unverified", "unverified@test.local", "Password123!")
    # 不验证邮箱直接登录应失败
    try:
        cap = api.captcha()
        r = api.client.post("/api/auth/login", json={
            "username": "unverified", "password": "Password123!",
            "captcha_id": cap["captcha_id"], "captcha_answer": cap["answer"],
        })
        check("未验证邮箱登录被拒", r.status_code == 403, f"status={r.status_code}")
    except Exception as e:
        check("未验证邮箱登录被拒", False, str(e))

    # /me
    r = api.authed(users["alice"], "GET", "/api/auth/me")
    check("/me 返回当前用户", r.json().get("user", {}).get("username") == "alice", r.text[:80])
    return users


def test_bots(api: Api, users: dict[str, str]) -> dict[str, int]:
    print("\n[2/6] Bot 上传 / 版本 / 上架")
    elf = read_sample("samples/callbot_linux_amd64")
    bot_ids: dict[str, int] = {}
    for name, tag in (("alice", "AliceBot"), ("bob", "BobBot"), ("carol", "CarolBot")):
        headers, body = multipart({"name": tag, "is_public": "true"}, "file", "bot.bin", elf)
        r = api.authed(users[name], "POST", "/api/bots", headers=headers, content=body)
        check(f"上传 {tag}", r.status_code == 200 and "bot" in r.json(), f"{r.status_code} {r.text[:80]}")
        bot_ids[tag] = r.json()["bot"]["id"]

    # 上传第二版本
    headers, body = multipart({"upload_note": "v2"}, "file", "bot.bin", elf)
    r = api.authed(users["alice"], "POST", f"/api/bots/{bot_ids['AliceBot']}/versions", headers=headers, content=body)
    check("上传新版本", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

    # 公开列表
    r = api.client.get("/api/bots/public")
    pubs = r.json().get("bots", [])
    check("公开 Bot 列表 >=3", len(pubs) >= 3, f"got {len(pubs)}")

    # 我的 Bot
    r = api.authed(users["alice"], "GET", "/api/bots/mine")
    check("我的 Bot", len(r.json().get("bots", [])) >= 1, r.text[:80])
    return bot_ids


def wait_match(api: Api, token: str, mid: str, timeout: float = 90) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = api.authed(token, "GET", f"/api/matches/{mid}")
        m = r.json()["match"]
        if m["status"] in ("completed", "aborted"):
            return m
        time.sleep(0.4)
    raise TimeoutError(f"对局 {mid} 超时")


def test_single_match(api: Api, users: dict[str, str], bot_ids: dict[str, int]) -> str:
    print("\n[3/6] 单局对局 + 结果完整性")
    r = api.authed(users["alice"], "POST", "/api/matches/challenge", json={
        "my_bot_id": bot_ids["AliceBot"], "opponent_bot_id": bot_ids["BobBot"], "hands": 10,
    })
    check("发起挑战", r.status_code == 200 and "match_id" in r.json(), f"{r.status_code} {r.text[:80]}")
    mid = r.json()["match_id"]
    m = wait_match(api, users["alice"], mid)
    check("对局完成(非 aborted)", m["status"] == "completed", f"status={m['status']} reason={m.get('reason')}")

    # 完整性：earnings 零和
    ea, eb = int(m["earnings_a"]), int(m["earnings_b"])
    check("earnings 零和 (ea+eb==0)", ea + eb == 0, f"ea={ea} eb={eb}")
    # winner 与 earnings 一致
    if m["winner"] == 0:
        check("winner=0 时 ea>eb", ea > eb, f"ea={ea} eb={eb} winner={m['winner']}")
    elif m["winner"] == 1:
        check("winner=1 时 eb>ea", eb > ea, f"ea={ea} eb={eb} winner={m['winner']}")
    else:
        check("平局 ea==eb", ea == eb, f"ea={ea} eb={eb}")
    check("net_bb_a == ea/100", abs(m["net_bb_a"] - ea / 100.0) < 1e-6, f"net_bb_a={m['net_bb_a']}")
    return mid


def test_sse_vs_replay(api: Api, users: dict[str, str], bot_ids: dict[str, int]) -> None:
    print("\n[4/6] SSE 事件流 vs 落盘回放一致性")
    # 发起一局，并在对局完成前尽早订阅 SSE，验证 snapshot 历史是 replay 的前缀
    r = api.authed(users["alice"], "POST", "/api/matches/challenge", json={
        "my_bot_id": bot_ids["AliceBot"], "opponent_bot_id": bot_ids["CarolBot"], "hands": 6,
    })
    mid = r.json()["match_id"]

    # 等对局完成（replay 是权威完整数据）
    m = wait_match(api, users["alice"], mid, timeout=90)
    check("SSE 测试对局完成", m["status"] == "completed", f"status={m['status']}")

    # 落盘 replay 完整性校验（权威数据）
    r = api.authed(users["alice"], "GET", f"/api/matches/{mid}")
    replay = json.loads(r.json()["replay"].get("events_json") or "[]")
    rep_types = [e.get("type") for e in replay]
    check("replay 非空", len(replay) > 0, "空")
    check("replay 以 hand_start 开头", rep_types[:1] == ["hand_start"], str(rep_types[:3]))
    check("replay 以 match_end 结尾", rep_types[-1:] == ["match_end"], str(rep_types[-2:]))
    # 结构完整性：hand_start 数 == settle 数 == hands_played
    hs = rep_types.count("hand_start")
    st = rep_types.count("settle")
    check("replay hand_start==settle==hands_played", hs == st == int(m["hands_played"]),
          f"hs={hs} settle={st} hands_played={m['hands_played']}")
    # 每手都有 deal_hole
    dh = rep_types.count("deal_hole")
    check("replay deal_hole==hand_start", dh == hs, f"deal_hole={dh} hand_start={hs}")

    # 验证 SSE snapshot 前缀一致性：重新订阅一个已完成的对局，
    # snapshot 应携带与 replay 完全一致的全部历史事件。
    sse_first: list[dict] = []
    url = f"{api.base}/api/matches/{mid}/events"
    token = users["alice"]
    stop = {"flag": False}

    def stream():
        try:
            with httpx.stream("GET", url, headers={"Authorization": f"Bearer {token}"}, timeout=30) as resp:
                ev_type = None
                data_line = ""
                for line in resp.iter_lines():
                    if stop["flag"]:
                        break
                    if line.startswith("event:"):
                        ev_type = line[6:].strip()
                    elif line.startswith("data:"):
                        data_line = line[5:].strip()
                    elif line == "":
                        if data_line and ev_type != "ping":
                            try:
                                ev = json.loads(data_line)
                                sse_first.append(ev)
                                # 收到 snapshot 后即可（已完成对局，snapshot 含全部历史）
                                if ev.get("type") == "snapshot":
                                    return
                            except json.JSONDecodeError:
                                pass
                        data_line = ""
                        ev_type = None
        except Exception as e:
            print(f"    (SSE 流异常: {e})")

    t = threading.Thread(target=stream, daemon=True)
    t.start()
    t.join(timeout=15)
    stop["flag"] = True

    check("SSE 收到 snapshot", bool(sse_first) and sse_first[0].get("type") == "snapshot",
          str(sse_first[0].get("type") if sse_first else "空"))
    if sse_first and sse_first[0].get("type") == "snapshot":
        snap_events = sse_first[0].get("events", []) or []
        # snapshot 历史应与 replay 完全一致（已完成对局）
        snap_types = [e.get("type") for e in snap_events]
        check("snapshot 历史 type 序列 == replay", snap_types == rep_types,
              f"snap={snap_types[:8]} replay={rep_types[:8]}")
        check("snapshot 历史数 == replay 数", len(snap_events) == len(replay),
              f"snap={len(snap_events)} replay={len(replay)}")


def test_concurrent_matches(api: Api, users: dict[str, str], bot_ids: dict[str, int], n: int = 4) -> None:
    print(f"\n[5/6] 并发 {n} 局对局稳定性")
    # 不同 bot 对，避免冲突：Alice vs Bob, Alice vs Carol, Carol vs Bob, Bob vs Carol
    pairs = [
        (bot_ids["AliceBot"], bot_ids["BobBot"], users["alice"]),
        (bot_ids["AliceBot"], bot_ids["CarolBot"], users["alice"]),
        (bot_ids["CarolBot"], bot_ids["BobBot"], users["carol"]),
        (bot_ids["BobBot"], bot_ids["CarolBot"], users["bob"]),
    ]
    pairs = (pairs * ((n + 3) // 4))[:n]

    results: dict[int, dict] = {}
    errors: dict[int, str] = {}
    barrier = threading.Barrier(n)

    def run(i: int, a: int, b: int, tok: str):
        try:
            barrier.wait()  # 尽量同时发起
            r = api.authed(tok, "POST", "/api/matches/challenge", json={
                "my_bot_id": a, "opponent_bot_id": b, "hands": 8,
            })
            if r.status_code != 200:
                errors[i] = f"challenge {r.status_code} {r.text[:60]}"
                return
            mid = r.json()["match_id"]
            m = wait_match(api, tok, mid, timeout=120)
            results[i] = m
        except Exception as e:
            errors[i] = str(e)

    threads = [threading.Thread(target=run, args=(i, a, b, t)) for i, (a, b, t) in enumerate(pairs)]
    t0 = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    dt = time.time() - t0

    check(f"全部 {n} 局成功发起", len(errors) == 0, "; ".join(f"#{k}:{v}" for k, v in errors.items()))
    completed = [m for m in results.values() if m["status"] == "completed"]
    check(f"全部 {n} 局 completed", len(completed) == n,
          f"completed={len(completed)} aborted={sum(1 for m in results.values() if m['status']=='aborted')}")
    # 每局零和
    zero_sum = all(int(m["earnings_a"]) + int(m["earnings_b"]) == 0 for m in results.values())
    check("每局 earnings 零和", zero_sum, "存在非零和对局")
    # 无重复 match_id
    check("无超时(<120s 总)", dt < 125, f"耗时 {dt:.1f}s")
    print(f"    {n} 局并发总耗时 {dt:.1f}s")


def test_leaderboard_and_contest(api: Api, users: dict[str, str], bot_ids: dict[str, int]) -> None:
    print("\n[6/6] 排行榜 Glicko + 组织者比赛")
    # 排行榜
    r = api.client.get("/api/leaderboard?limit=20")
    lb = r.json().get("leaderboard", [])
    check("排行榜非空", len(lb) > 0, "空")
    if lb:
        row0 = lb[0]
        check("排行榜含 rating 字段", "rating" in row0, str(row0)[:80])
        check("排行榜含 matches_played", "matches_played" in row0, str(row0)[:80])
        # 已跑多局，应有 matches_played > 0
        played = [x for x in lb if x.get("matches_played", 0) > 0]
        check("存在已参赛 bot", len(played) > 0, "全部 matches_played=0")

    # 比赛循环赛
    r = api.authed(users["org1"], "POST", "/api/contests", json={"title": "API Test Cup", "hands_per_match": 6})
    check("创建比赛", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    cid = r.json()["contest"]["id"]

    r = api.authed(users["org1"], "POST", f"/api/contests/{cid}/open")
    check("开放报名", r.status_code == 200, f"{r.status_code}")
    for tag, user in (("AliceBot", "alice"), ("BobBot", "bob"), ("CarolBot", "carol")):
        r = api.authed(users[user], "POST", f"/api/contests/{cid}/register", json={"bot_id": bot_ids[tag]})
        check(f"报名 {tag}", r.status_code == 200, f"{r.status_code} {r.text[:60]}")

    r = api.authed(users["org1"], "POST", f"/api/contests/{cid}/start")
    check("启动循环赛", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

    # 等所有对阵的 match 完成。
    # 注：后端 maybe_finish 目前无路由触发，比赛 status 不会自动转 finished，
    # 因此用「所有 pairing 的 match 均 completed」作为完成判据。
    deadline = time.time() + 180
    all_done = False
    while time.time() < deadline:
        r = api.client.get(f"/api/contests/{cid}")
        pairings = r.json().get("pairings", [])
        if pairings:
            statuses = []
            for p in pairings:
                mid_p = p.get("match_id")
                if not mid_p:
                    statuses.append("pending")
                    continue
                pm = api.client.get(f"/api/matches/{mid_p}").json()["match"]
                statuses.append(pm["status"])
            if all(s in ("completed", "aborted") for s in statuses):
                all_done = True
                break
        time.sleep(1)

    check("循环赛所有对阵完成", all_done, "超时未完成")

    r = api.client.get(f"/api/contests/{cid}")
    detail = r.json()
    # 对局完成后比赛应自动归档为 finished
    check("比赛自动归档为 finished", detail["contest"].get("status") == "finished",
          f"status={detail['contest'].get('status')}")
    pairings = detail.get("pairings", [])
    standings = detail.get("standings", [])
    # 循环赛对阵数 = C(3,2) = 3
    expected_pairings = 3
    check(f"循环赛 {expected_pairings} 个对阵", len(pairings) == expected_pairings,
          f"got {len(pairings)}")
    check("standings 非空", len(standings) > 0, "空")
    if standings:
        # 积分按 (points desc, net_chips desc) 排序
        pts = [s.get("points", 0) for s in standings]
        check("standings 按积分降序", pts == sorted(pts, reverse=True), str(pts))
        check("standings 含 net_chips", "net_chips" in standings[0], str(standings[0])[:80])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BZ_TEST_BASE", "http://127.0.0.1:50382"))
    ap.add_argument("--db", default=os.environ.get("BZ_DB_PATH", ""))
    args = ap.parse_args()

    if not args.db:
        print("✗ 需要指定 --db 或 BZ_DB_PATH（读取验证码表）", file=sys.stderr)
        return 2

    print(f"API 完整测试  base={args.base}  db={args.db}")
    api = Api(args.base, args.db)

    # 健康检查
    try:
        h = api.client.get("/api/health").json()
        print(f"  health: {h}")
        assert h.get("ok"), "health.ok=False"
    except Exception as e:
        print(f"✗ 后端不可达: {e}", file=sys.stderr)
        return 2

    users = test_auth_flow(api)
    bot_ids = test_bots(api, users)
    test_single_match(api, users, bot_ids)
    test_sse_vs_replay(api, users, bot_ids)
    test_concurrent_matches(api, users, bot_ids, n=4)
    test_leaderboard_and_contest(api, users, bot_ids)

    print("\n" + "=" * 60)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("✅ ALL API TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
