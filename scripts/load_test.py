#!/usr/bin/env python3
"""大规模系统压测：仅打隔离 worktree 服务与数据库副本。

批量创建用户（DB-direct 绕验证码/SMTP）+ 模拟真实行为，覆盖主要角色与业务端点。

设计要点（调研结论）：
  - dev 服务 .env 配了真实 SMTP 且未设 BZ_TEST_CAPTCHA=1 → HTTP /register 的验证码无法被脚本
    解开，且批量 HTTP 注册会狂发真邮件。故用户与 Bot 用 DB-direct 播种（沿用 seed_test_accounts.py
    与 e2e_smoke.sh 的模式），登录态用 **DB 直写 sessions 表** 生成不透明 Bearer token
    （`store.add_session(new_session_token(), uid, session_expires())`）—— 服务端从同一 botzone.db
    读 sessions，Bearer 真正打通 require_user/admin/organizer 全链路。
  - Bot 执行方式由被测服务决定：生产式 Docker 沙箱，或 QA 服务显式设置 BZ_BOT_LOCAL=1。
  - 并发硬顶 = cpu//4；三款游戏均使用 GameSpec 固定规则（holdem 固定 70 手）。
  - 所有用户名/邮箱/Bot 名均 load_* 前缀，可一键识别清理；seed 幂等可重复跑。

8 个阶段覆盖矩阵：
  0 基础(公开读 + 鉴权)   1 Bot 端点   2 对局(三游戏多局+自博弈)
  3 SSE snapshot         4 人类 vs Bot(WS /play)   5 赛事全生命周期
  6 自动对局(ladder)     7 Admin 关键端点

用法：
  python scripts/load_test.py [--base http://127.0.0.1:50381] [--db botzone.db] [--users 60]
前置：worktree dev 服务在线。50380 与主 checkout botzone.db 会被硬拒绝。
退出码：0=全过，1=有失败。
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

# 让脚本能 import bzplat（仓库根在父目录）
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from bzplat.backend.crypto import hash_password, new_session_token, session_expires  # noqa: E402
from bzplat.backend.store import Store  # noqa: E402
from bzplat.backend.store.schema import ROLE_ADMIN, ROLE_ORGANIZER, ROLE_USER  # noqa: E402
from bzplat.backend.bots.manager import BotManager  # noqa: E402
from scripts._qa_accounts import (  # noqa: E402
    QaAccountSpec,
    get_or_create_dedicated_account,
    inspect_dedicated_account,
    preflight_dedicated_accounts,
)
from scripts._qa_target import (  # noqa: E402
    assert_qa_instance,
    ensure_qa_base,
    qa_db_path,
    qa_upload_root,
)

PASS = 0
FAIL = 0
FAILS: list[str] = []
WARN: list[str] = []

# ── 固定常量 ──────────────────────────────────────────────────
PASSWORD = "LoadTest1234"          # 所有 load 账号统一密码
EMAIL_DOMAIN = "loadtest.local"
N_USERS = 60                       # 普通用户数（可被 --users 覆盖）
N_ORGS = 2                         # 组织者数
TARGET_MATCHES = 12                # 阶段 2 目标对局总数（三游戏×4，配合关限流可在 ~60s 完成）
SAMPLE_BINARIES = {
    "holdem": "samples/callbot_linux_amd64",
    "gomoku": "samples/gomokubot_linux_amd64",
    "pencil": "samples/pencilbot_linux_amd64",
}
GAMES = ("holdem", "gomoku", "pencil")
LOAD_ACCOUNT_NAMESPACE = "load-test-v1"
LOAD_ADMIN_NAME = "load_admin"


def load_account_spec(username: str, email: str, role: str) -> QaAccountSpec:
    return QaAccountSpec(
        LOAD_ACCOUNT_NAMESPACE, username, email, PASSWORD, role
    )


def load_user_spec(username: str) -> QaAccountSpec:
    return load_account_spec(username, f"{username}@{EMAIL_DOMAIN}", ROLE_USER)


def load_org_spec(username: str) -> QaAccountSpec:
    return load_account_spec(
        username, f"{username}@{EMAIL_DOMAIN}", ROLE_ORGANIZER
    )


def load_admin_spec() -> QaAccountSpec:
    return load_account_spec(
        LOAD_ADMIN_NAME,
        f"{LOAD_ADMIN_NAME}@{EMAIL_DOMAIN}",
        ROLE_ADMIN,
    )


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        FAILS.append(f"{name}: {detail}")
        print(f"  ✗ {name}  {detail}")


def warn(msg: str) -> None:
    WARN.append(msg)
    print(f"  ⚠ {msg}")


# ── HTTP 客户端封装 ────────────────────────────────────────────
class Api:
    def __init__(self, base: str, db_path: str) -> None:
        self.base = base.rstrip("/")
        self.db_path = db_path
        self.client = httpx.Client(base_url=self.base, timeout=300)

    def authed(self, token: str, method: str, path: str, **kw) -> httpx.Response:
        headers = dict(kw.pop("headers", {}) or {})
        headers.setdefault("Authorization", f"Bearer {token}")
        return self.client.request(method, path, headers=headers, **kw)

    def wait_match(self, token: str, mid: str, timeout: float = 180) -> dict:
        deadline = time.time() + timeout
        while time.time() < deadline:
            r = self.authed(token, "GET", f"/api/matches/{mid}")
            if r.status_code == 200:
                m = r.json()["match"]
                if m["status"] in ("completed", "aborted"):
                    return m
            time.sleep(0.5)
        raise TimeoutError(f"对局 {mid} {timeout}s 未完成")


def multipart(fields: dict[str, Any], file_field: str, filename: str, data: bytes) -> tuple[dict, bytes]:
    """构造简单 multipart/form-data body（沿用 api_full_test.py）。"""
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []
    for k, v in fields.items():
        parts.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode()
        )
    parts.append(
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; filename=\"{filename}\"\r\n"
        f"Content-Type: application/octet-stream\r\n\r\n".encode()
        + data
        + b"\r\n"
    )
    parts.append(f"--{boundary}--\r\n".encode())
    return {"Content-Type": f"multipart/form-data; boundary={boundary}"}, b"".join(parts)


# ── 种子（DB-direct，幂等）────────────────────────────────────
def seed(
    db_path: str,
    n_users: int,
    upload_root: str | None = None,
) -> dict[str, Any]:
    """批量建 load_* 用户 + Bot + organizer + admin，直写 sessions 表生成 Bearer token。

    返回 {tokens:{username→token}, bots:{username→{game→bot_id}}, admin_token, org_tokens:[...],
          user_names:[...], admin_name}。幂等：已存在的用户/bot 跳过。
    """
    resolved_db = qa_db_path(db_path, ROOT)
    resolved_uploads = qa_upload_root(upload_root, resolved_db, ROOT)
    print(f"\n=== 种子：{n_users} 用户 × 3 Bot + {N_ORGS} 组织者 + admin（DB-direct）===")
    print(f"  db={resolved_db} uploads={resolved_uploads}")
    resolved_db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(resolved_db))
    # 错峰：写前给该连接设 busy_timeout，规避与运行服务的锁竞争
    store._conn.execute("PRAGMA busy_timeout=10000")
    user_specs = [load_user_spec(f"load_u{i:02d}") for i in range(1, n_users + 1)]
    org_specs = [load_org_spec(f"load_org{i}") for i in range(1, N_ORGS + 1)]
    admin_spec = load_admin_spec()
    try:
        preflight_dedicated_accounts(
            store, [*user_specs, *org_specs, admin_spec]
        )
    except Exception:
        store.close()
        raise
    bm = BotManager(store, upload_root=resolved_uploads)

    sample_bytes = {gid: (ROOT / rel).read_bytes() for gid, rel in SAMPLE_BINARIES.items()}

    def get_or_create_user(username: str, email: str, role: str = ROLE_USER) -> dict:
        return get_or_create_dedicated_account(
            store, load_account_spec(username, email, role)
        )

    def get_or_create_bot(owner_id: int, name: str, game_id: str, raw: bytes) -> dict:
        existing = store.get_bot_by_owner_name(owner_id, name)
        if existing:
            return existing
        return bm.create_from_upload(
            owner_id, name, raw, display_name=name, game_id=game_id
        )

    tokens: dict[str, str] = {}
    bots: dict[str, dict[str, int]] = {}
    user_names: list[str] = []

    # 普通用户
    for i in range(1, n_users + 1):
        uname = f"load_u{i:02d}"
        email = f"{uname}@{EMAIL_DOMAIN}"
        u = get_or_create_user(uname, email)
        # 若是新建用户（matches_played=0），确保有 rating 行
        for gid in GAMES:
            bname = f"{uname}_{gid}"
            b = get_or_create_bot(u["id"], bname, gid, sample_bytes[gid])
            bots.setdefault(uname, {})[gid] = b["id"]
            store.ensure_rating(b["id"])
        user_names.append(uname)

    # 组织者
    org_names: list[str] = []
    for i in range(1, N_ORGS + 1):
        uname = f"load_org{i}"
        email = f"{uname}@{EMAIL_DOMAIN}"
        get_or_create_user(uname, email, role=ROLE_ORGANIZER)
        org_names.append(uname)

    # admin：仅允许脚本自己的 load_admin；绝不复用 copied DB 的任意管理员。
    admin_name = LOAD_ADMIN_NAME
    get_or_create_dedicated_account(store, admin_spec)

    # 生成 Bearer token（DB 直写 sessions 表，服务端从同一库读）
    for uname in user_names + org_names + [admin_name]:
        u = store.get_user_by_username(uname)
        tok = new_session_token()
        store.add_session(tok, u["id"], session_expires(), ip_addr="127.0.0.1", user_agent="load_test")
        tokens[uname] = tok

    store.close()
    print(f"  种子完成：{len(user_names)} 用户 + {len(org_names)} 组织者 + admin={admin_name}，"
          f"{sum(len(v) for v in bots.values())} Bot")
    return {
        "tokens": tokens,
        "bots": bots,
        "user_names": user_names,
        "org_names": org_names,
        "admin_name": admin_name,
        "admin_token": tokens[admin_name],
        "org_tokens": [tokens[n] for n in org_names],
    }


# ── 阶段 0：基础（公开读 + 鉴权）──────────────────────────────
def phase0_basics(api: Api, ctx: dict[str, Any]) -> None:
    print("\n=== 阶段 0：基础（公开读 + 鉴权）===")
    # 公开端点
    r = api.client.get("/api/health")
    check("GET /api/health", r.status_code == 200 and r.json().get("ok") is True, r.text[:80])

    r = api.client.get("/api/wiki")
    check("GET /api/wiki", r.status_code == 200 and "markdown" in r.json(), r.text[:80])

    r = api.client.get("/api/wiki?slug=protocol")
    check("GET /api/wiki?slug=protocol", r.status_code == 200 and "markdown" in r.json(), r.text[:80])

    r = api.client.get("/api/leaderboard?limit=20")
    check("GET /api/leaderboard", r.status_code == 200 and "leaderboard" in r.json(), r.text[:80])

    # 段位定义（PR-5）
    r = api.client.get("/api/tiers")
    check("GET /api/tiers", r.status_code == 200 and len(r.json().get("tiers", [])) >= 6, r.text[:80])
    # 经验/等级体系（PR-9）
    r = api.client.get("/api/levels/info")
    check("GET /api/levels/info", r.status_code == 200 and "thresholds" in r.json(), r.text[:80])
    # 站点信息（PR-10）
    r = api.client.get("/api/site/info")
    check("GET /api/site/info", r.status_code == 200 and "name" in r.json(), r.text[:80])

    r = api.client.get("/api/contests/templates")
    check("GET /api/contests/templates", r.status_code == 200 and "templates" in r.json(), r.text[:80])

    r = api.client.get("/api/contests")
    check("GET /api/contests", r.status_code == 200 and "contests" in r.json(), r.text[:80])

    r = api.client.get("/api/matches?limit=5")
    check("GET /api/matches", r.status_code == 200 and "matches" in r.json(), r.text[:80])

    # /api/auth/captcha 公开
    r = api.client.get("/api/auth/captcha")
    check("GET /api/auth/captcha", r.status_code == 200 and "captcha_id" in r.json(), r.text[:80])

    # 用户搜索（公开）
    r = api.client.get("/api/users?q=load_u&limit=10")
    check("GET /api/users?q=load_u", r.status_code == 200 and len(r.json().get("users", [])) > 0, r.text[:80])

    # 用户主页 + 全局搜索（PR-2）
    u1 = ctx["user_names"][0]
    r = api.client.get(f"/api/users/{u1}/profile")
    check("GET /api/users/{name}/profile", r.status_code == 200 and "profile" in r.json()
          and r.json()["profile"]["username"] == u1, r.text[:80])
    r = api.client.get(f"/api/users/{u1}/bots")
    check("GET /api/users/{name}/bots", r.status_code == 200 and "bots" in r.json(), r.text[:80])
    for t in ("users", "bots", "matches"):
        r = api.client.get(f"/api/search?q=load&type={t}&limit=5")
        check(f"GET /api/search?q=load&type={t}", r.status_code == 200 and t in r.json(), r.text[:80])
    # PUT /api/auth/profile（更新显示名/简介）
    tok0 = ctx["tokens"][u1]
    r = api.authed(tok0, "PUT", "/api/auth/profile", json={"bio": "loadtest bio"})
    check("PUT /api/auth/profile", r.status_code == 200 and r.json()["user"]["bio"] == "loadtest bio", r.text[:80])

    # 鉴权：DB token 真正打通 require_user
    tok = ctx["tokens"][ctx["user_names"][0]]
    r = api.authed(tok, "GET", "/api/auth/me")
    check("GET /api/auth/me（DB token 鉴权）",
          r.status_code == 200 and r.json()["user"]["username"] == ctx["user_names"][0], r.text[:80])

    # admin token 打通 require_admin
    r = api.authed(ctx["admin_token"], "GET", "/api/admin/stats")
    check("GET /api/admin/stats（admin token）", r.status_code == 200, r.text[:80])

    # 通知端点（PR-3）
    r = api.authed(tok, "GET", "/api/notifications")
    check("GET /api/notifications", r.status_code == 200 and "notifications" in r.json(), r.text[:80])
    r = api.authed(tok, "GET", "/api/notifications/unread-count")
    check("GET /api/notifications/unread-count", r.status_code == 200 and "count" in r.json(), r.text[:80])
    r = api.authed(tok, "GET", "/api/notification-prefs")
    check("GET /api/notification-prefs", r.status_code == 200 and "prefs" in r.json(), r.text[:80])
    r = api.authed(tok, "PUT", "/api/notification-prefs", json={"email_match_done": True})
    check("PUT /api/notification-prefs", r.status_code == 200 and r.json()["prefs"]["email_match_done"] == 1, r.text[:80])

    # 评论 + 点赞 + 浏览（PR-7）：对 phase0 的某场对局操作
    # 先发起一场对局拿 match_id
    u2 = ctx["user_names"][1]
    rc = _paced_challenge
    if rc:
        rr = rc(api, tok, {"my_bot_id": ctx["bots"][ctx["user_names"][0]]["holdem"],
                           "opponent_bot_id": ctx["bots"][u2]["holdem"], "game_id": "holdem"})
        if rr.status_code == 200:
            tmid = rr.json()["match_id"]
            # 评论
            r = api.authed(tok, "POST", "/api/comments",
                           json={"target_type": "match", "target_id": tmid, "body": "loadtest comment"})
            check("POST /api/comments", r.status_code == 200 and "comment" in r.json(), r.text[:80])
            r = api.client.get(f"/api/comments?target_type=match&target_id={tmid}")
            check("GET /api/comments", r.status_code == 200 and "comments" in r.json(), r.text[:80])
            # 点赞
            r = api.authed(tok, "POST", "/api/likes", json={"target_type": "match", "target_id": tmid})
            check("POST /api/likes", r.status_code == 200 and r.json()["liked"] is True, r.text[:80])
            r = api.authed(tok, "GET", f"/api/likes/status?target_type=match&target_id={tmid}")
            check("GET /api/likes/status", r.status_code == 200 and r.json()["liked"] is True, r.text[:80])
            r = api.authed(tok, "DELETE", "/api/likes", json={"target_type": "match", "target_id": tmid})
            check("DELETE /api/likes", r.status_code == 200, r.text[:80])
            # 浏览
            r = api.client.post(f"/api/matches/{tmid}/view")
            check("POST /api/matches/{id}/view", r.status_code == 200, r.text[:80])
            # 点赞榜（公开）
            r = api.client.get("/api/matches/liked-top?limit=5")
            check("GET /api/matches/liked-top", r.status_code == 200 and "matches" in r.json(), r.text[:80])

    # organizer token 打通 require_organizer（GET /api/admin/contests 需要 admin；用创建比赛权限验证）
    # 这里用「列表自己的比赛报名」间接验证：require_organizer 端点是 POST /api/contests，下面阶段 5 覆盖

    # change-password：改密码后旧会话失效，再用新密码走 authenticate 重登验证（HTTP /login 受 captcha 限制，
    # 这里改为验证「change-password 后该 token 仍可用」—— 实际 change-password 会删全部 session，故应 401）
    probe = ctx["user_names"][-1]
    ptok = ctx["tokens"][probe]
    r = api.authed(ptok, "POST", "/api/auth/change-password",
                   json={"old_password": PASSWORD, "new_password": "NewLoadTest1234"})
    check("POST /api/auth/change-password", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    # change-password 删除全部 session → 该 token 应失效
    r2 = api.authed(ptok, "GET", "/api/auth/me")
    check("change-password 后旧 token 失效（session 被清）", r2.status_code == 401, f"{r2.status_code}")
    # 重置回原密码（DB-direct 重设哈希 + 重新发 token），保证幂等重跑
    store = Store(api.db_path)
    store._conn.execute("PRAGMA busy_timeout=10000")
    u = store.get_user_by_username(probe)
    store.update_user(u["id"], password_hash=hash_password(PASSWORD))
    ntok = new_session_token()
    store.add_session(ntok, u["id"], session_expires())
    store.close()
    ctx["tokens"][probe] = ntok  # 更新 ctx 里的 token
    r3 = api.authed(ntok, "GET", "/api/auth/me")
    check("重置密码后新 token 可用", r3.status_code == 200, f"{r3.status_code}")


# ── 阶段 1：Bot 端点 ──────────────────────────────────────────
def phase1_bots(api: Api, ctx: dict[str, Any]) -> None:
    print("\n=== 阶段 1：Bot 端点 ===")
    u1 = ctx["user_names"][0]
    tok = ctx["tokens"][u1]

    # GET /api/bots/mine?game_id=
    r = api.authed(tok, "GET", "/api/bots/mine?game_id=holdem")
    mine = r.json().get("bots", [])
    check("GET /api/bots/mine?game_id=holdem", r.status_code == 200 and len(mine) >= 1, r.text[:80])
    check("我的 Bot 均 holdem", all(b.get("game_id") == "holdem" for b in mine), str([b.get("game_id") for b in mine]))

    # GET /api/bots/public?owner_id=
    u1_id = _user_id(api.db_path, u1)
    r = api.client.get(f"/api/bots/public?owner_id={u1_id}")
    pubs = r.json().get("bots", [])
    check("GET /api/bots/public?owner_id=", r.status_code == 200 and len(pubs) >= 3, r.text[:80])
    check("public 列表含 owner_name 字段", all("owner_name" in b for b in pubs), str(pubs[0])[:80] if pubs else "")

    # GET /api/bots/{id}
    bid = ctx["bots"][u1]["holdem"]
    r = api.client.get(f"/api/bots/{bid}")
    check("GET /api/bots/{id}", r.status_code == 200 and r.json()["bot"]["id"] == bid, r.text[:80])

    # 社交：关注用户 + 收藏 Bot（PR-4）
    u2 = ctx["user_names"][1]
    u2_id = _user_id(api.db_path, u2)
    r = api.authed(tok, "POST", f"/api/users/{u2_id}/follow")
    check("POST /api/users/{id}/follow", r.status_code == 200 and r.json()["following"] is True, r.text[:80])
    r = api.authed(tok, "GET", f"/api/users/{u2_id}/follow-status")
    check("GET /api/users/{id}/follow-status", r.status_code == 200 and r.json()["following"] is True, r.text[:80])
    r = api.client.get(f"/api/users/{u2_id}/followers")
    check("GET /api/users/{id}/followers", r.status_code == 200 and len(r.json()["followers"]) >= 1, r.text[:80])
    r = api.authed(tok, "DELETE", f"/api/users/{u2_id}/follow")
    check("DELETE /api/users/{id}/follow", r.status_code == 200 and r.json()["following"] is False, r.text[:80])
    r = api.authed(tok, "POST", f"/api/bots/{bid}/favorite")
    check("POST /api/bots/{id}/favorite", r.status_code == 200 and r.json()["favorited"] is True, r.text[:80])
    r = api.authed(tok, "GET", f"/api/bots/{bid}/favorite-status")
    check("GET /api/bots/{id}/favorite-status", r.status_code == 200 and r.json()["favorited"] is True, r.text[:80])
    r = api.authed(tok, "GET", "/api/auth/me/favorites")
    check("GET /api/auth/me/favorites", r.status_code == 200 and len(r.json()["favorites"]) >= 1, r.text[:80])
    r = api.authed(tok, "DELETE", f"/api/bots/{bid}/favorite")
    check("DELETE /api/bots/{id}/favorite", r.status_code == 200 and r.json()["favorited"] is False, r.text[:80])

    # GET /api/bots/{id} 404
    r = api.client.get("/api/bots/9999999")
    check("GET /api/bots/9999999 → 404", r.status_code == 404, f"{r.status_code}")

    # Bot 详情页端点（profile/matches/opponents/rating-history）—— PR-1
    r = api.client.get(f"/api/bots/{bid}/profile")
    check("GET /api/bots/{id}/profile", r.status_code == 200 and "profile" in r.json()
          and r.json()["profile"].get("name") == f"{u1}_holdem", r.text[:80])
    r = api.client.get(f"/api/bots/{bid}/matches?limit=10")
    check("GET /api/bots/{id}/matches", r.status_code == 200 and "matches" in r.json(), r.text[:80])
    r = api.client.get(f"/api/bots/{bid}/opponents")
    check("GET /api/bots/{id}/opponents", r.status_code == 200 and "opponents" in r.json(), r.text[:80])
    r = api.client.get(f"/api/bots/{bid}/rating-history")
    check("GET /api/bots/{id}/rating-history", r.status_code == 200 and "history" in r.json(), r.text[:80])
    r = api.client.get("/api/bots/9999999/profile")
    check("GET /api/bots/9999999/profile → 404", r.status_code == 404, f"{r.status_code}")

    # POST /api/bots（新 HTTP 上传一个 bot，验返回 bot.id）—— 名字带版本后缀避免冲突
    elf = (ROOT / SAMPLE_BINARIES["holdem"]).read_bytes()
    new_name = f"{u1}_extra"
    headers, body = multipart({"name": new_name, "game_id": "holdem"}, "file", "bot.bin", elf)
    r = api.authed(tok, "POST", "/api/bots", headers=headers, content=body)
    # 幂等：重跑压测时同名 bot 已存在（name_taken）也视为通过（端点可达即验证目的达成）
    upload_ok = r.status_code == 200 and "bot" in r.json()
    already_exists = r.status_code == 400 and "name_taken" in r.text
    check("POST /api/bots（HTTP 上传）", upload_ok or already_exists, f"{r.status_code} {r.text[:80]}")
    extra_bid = r.json().get("bot", {}).get("id") if upload_ok else None

    # POST /api/bots/{id}/versions（上 v2）
    if extra_bid:
        h2, b2 = multipart({"upload_note": "v2-loadtest"}, "file", "bot.bin", elf)
        r = api.authed(tok, "POST", f"/api/bots/{extra_bid}/versions", headers=h2, content=b2)
        check("POST /api/bots/{id}/versions（上传新版本）", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
        check("新版本号=2", r.json().get("bot", {}).get("current_version") == 2, str(r.json().get("bot", {}).get("current_version")))

    # POST /api/bots/{id}/active?active=false → true
    if extra_bid:
        r = api.authed(tok, "POST", f"/api/bots/{extra_bid}/active?active=false")
        check("POST /api/bots/{id}/active?active=false", r.status_code == 200 and r.json()["bot"]["is_active"] in (0, False), r.text[:80])
        r = api.authed(tok, "POST", f"/api/bots/{extra_bid}/active?active=true")
        check("POST /api/bots/{id}/active?active=true", r.status_code == 200 and r.json()["bot"]["is_active"] in (1, True), r.text[:80])
        # owner PATCH/DELETE（PR-8 MyBots 管理）
        r = api.authed(tok, "PATCH", f"/api/bots/{extra_bid}", json={"display_name": "lt-renamed", "description": "d"})
        check("PATCH /api/bots/{id}（owner 改名/简介）", r.status_code == 200 and r.json()["bot"]["display_name"] == "lt-renamed", r.text[:80])
        r = api.authed(tok, "DELETE", f"/api/bots/{extra_bid}")
        check("DELETE /api/bots/{id}（owner 软删）", r.status_code == 200, r.text[:80])


def _user_id(db_path: str, username: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        return int(row[0])
    finally:
        con.close()


# ── 阶段 2：对局（三游戏多局 + 自博弈）────────────────────────
# dev 服务按 IP 限流：/api/matches/challenge = 8 req/60s（同一 IP 共享）。
# 压测时建议重启服务设 BZ_RATE_LIMIT=0 关闭限流，则挑战无需节流、可快速并发。
# 若限流开启，所有请求来自 127.0.0.1，挑战需节流：每窗口最多 8 个，间隔 ~7.6s。
CHALLENGE_RATE = 8           # 每 RATE_WINDOW 秒最多发起的挑战数（限流开启时）
RATE_WINDOW = 60.0
CHALLENGE_INTERVAL = RATE_WINDOW / CHALLENGE_RATE  # ~7.5s（限流开启时）
NO_THROTTLE = False          # --no-throttle 标志（服务端关限流时跳过挑战节流）


def phase2_matches(api: Api, ctx: dict[str, Any]) -> None:
    print(f"\n=== 阶段 2：对局（三游戏混跑，目标 {TARGET_MATCHES} 场；"
          f"顺序提交、并行等待，挑战节流 {CHALLENGE_RATE}/{int(RATE_WINDOW)}s）===")
    user_names = ctx["user_names"]

    # 构造对局对：跨用户对战 + 自博弈（同 owner 两不同 bot）
    pairs: list[tuple[int, int, str]] = []  # (my_bot_id, opp_bot_id, game)
    games_cycle = ["holdem", "gomoku", "pencil"] * ((TARGET_MATCHES // 3) + 2)
    extra_bots = {}  # 缓存：username→extra holdem bot id
    for i in range(TARGET_MATCHES):
        game = games_cycle[i % len(games_cycle)]
        if i % 4 == 0 and len(user_names) >= 1:
            # 自博弈：同一 owner 的该游戏 bot vs 该 owner 的 extra bot（仅 holdem 有 extra）
            owner = user_names[i % len(user_names)]
            my = ctx["bots"][owner][game]
            if game == "holdem":
                if owner not in extra_bots:
                    names = _all_bot_names(api.db_path, owner)
                    extra = next((n for n in names if n == f"{owner}_extra"), None)
                    extra_bots[owner] = _bot_id_by_name(api.db_path, extra) if extra else None
                if extra_bots.get(owner):
                    opp = extra_bots[owner]
                    pairs.append((my, opp, game))
                    continue
            # 无 extra：跨账号同游戏 bot
            other = user_names[(i + 1) % len(user_names)]
            pairs.append((my, ctx["bots"][other][game], game))
        else:
            # 跨用户对战
            a = user_names[i % len(user_names)]
            b = user_names[(i + 7) % len(user_names)]
            if a == b:
                b = user_names[(i + 13) % len(user_names)]
            pairs.append((ctx["bots"][a][game], ctx["bots"][b][game], game))

    results: list[dict] = []
    errors: list[str] = []
    done = [0]
    lock = threading.Lock()

    def wait_one(mid: str, game: str, owner_tok: str) -> None:
        try:
            # Hold'em 规则固定为 70 手，不能靠请求参数缩短；给真实整场留足时间。
            m = api.wait_match(owner_tok, mid, timeout=240)
            with lock:
                results.append({"match": m, "game": game})
                done[0] += 1
                if done[0] % 10 == 0:
                    print(f"    进度 {done[0]}/{TARGET_MATCHES}（最近 {game} {m['status']}）")
        except Exception as e:
            with lock:
                errors.append(f"wait {game}: {e}")

    # 单一线程顺序发起挑战；每个成功请求立即起 waiter 线程并行等待终态。
    # 这里不控制或断言服务端同时运行的对局数，也不声称持续打满并发 ceiling。
    t0 = time.time()
    waiters: list[threading.Thread] = []
    for idx, (my_bid, opp_bid, game) in enumerate(pairs):
        owner_tok = ctx["tokens"][user_names[idx % len(user_names)]]
        payload: dict[str, Any] = {"my_bot_id": my_bid, "opponent_bot_id": opp_bid, "game_id": game}
        # 节流：距上次挑战不足 interval 则等待（首个不等待）。
        # --no-throttle 时（服务端关限流）跳过此 sleep，挑战可快速连续提交。
        if idx > 0 and not NO_THROTTLE:
            elapsed = time.time() - t0
            expected = idx * CHALLENGE_INTERVAL
            if elapsed < expected:
                time.sleep(expected - elapsed)
        r = api.authed(owner_tok, "POST", "/api/matches/challenge", json=payload)
        if r.status_code != 200:
            with lock:
                errors.append(f"challenge {game} {r.status_code} {r.text[:60]}")
            # 被限流时多等一个窗口再继续
            if r.status_code == 429:
                time.sleep(RATE_WINDOW / CHALLENGE_RATE)
            continue
        mid = r.json()["match_id"]
        th = threading.Thread(target=wait_one, args=(mid, game, owner_tok), daemon=True)
        th.start()
        waiters.append(th)
    for th in waiters:
        th.join()
    dt = time.time() - t0

    completed = [r for r in results if r["match"]["status"] == "completed"]
    aborted = [r for r in results if r["match"]["status"] == "aborted"]
    launched = len(results) + len([e for e in errors if e.startswith("wait ")])
    check(f"发起挑战（成功+等待）{launched} 场", launched >= TARGET_MATCHES * 0.8,
          f"results={len(results)} challenge_errors={sum(1 for e in errors if e.startswith('challenge'))}")
    check("挑战对局多数完成（completed > aborted）", len(completed) > len(aborted),
          f"completed={len(completed)} aborted={len(aborted)} wait_errors={sum(1 for e in errors if e.startswith('wait'))}")
    challenge_429 = sum(1 for e in errors if "429" in e)
    if challenge_429:
        warn(f"阶段 2 挑战被限流 429 共 {challenge_429} 例（节流仍被触发，可减小 TARGET_MATCHES）")

    # 各游戏至少有 1 场 completed
    for g in GAMES:
        n = sum(1 for r in completed if r["game"] == g)
        check(f"{g} 有 completed 对局", n > 0, f"completed {n}")

    # 三游戏统一结果契约：match.result.deltas（旧 earnings_a/b 物理列已删除）。
    for r in completed:
        m = r["match"]
        deltas = (m.get("result") or {}).get("deltas")
        if not isinstance(deltas, list) or len(deltas) < 2:
            check(f"{r['game']} result.deltas 存在", False, f"match={m.get('id')}")
            break
        ea, eb = int(deltas[0]), int(deltas[1])
        if ea + eb != 0:
            check(f"{r['game']} deltas 零和", False, f"mid ea={ea} eb={eb}")
            break
    else:
        check("全部 completed 对局 result.deltas 合法且零和", True)

    # replay 非空（抽 1 场）。match 行用 id 字段（字符串 match_id）
    if completed:
        mid = completed[0]["match"]["id"]
        owner_tok = ctx["tokens"][user_names[0]]
        r = api.authed(owner_tok, "GET", f"/api/matches/{mid}")
        events = json.loads(r.json()["replay"].get("events_json") or "[]")
        check("replay events 非空", len(events) > 0, "空")

    # 排行榜 Glicko 已更新（challenge 类型会更新）
    r = api.client.get("/api/leaderboard?limit=50")
    lb = r.json().get("leaderboard", [])
    played = [x for x in lb if x.get("matches_played", 0) > 0]
    check("排行榜存在已参赛 bot（Glicko 更新）", len(played) > 0, f"played={len(played)}")

    print(f"    阶段 2 总耗时 {dt:.1f}s，completed={len(completed)} aborted={len(aborted)}")
    # 等一个完整限流窗口，保证后续阶段（SSE/赛事）的零星挑战不被 429。
    # --no-throttle（服务端关限流）时跳过此等待。
    if not NO_THROTTLE:
        print(f"    等待限流窗口 {int(RATE_WINDOW)}s 后进入下一阶段…")
        time.sleep(RATE_WINDOW + 2)


def _paced_challenge(api: Api, token: str, payload: dict, *, retries: int = 3) -> httpx.Response:
    """发起挑战，遇 429 限流则按 Retry-After 等待重试。"""
    for _ in range(retries):
        r = api.authed(token, "POST", "/api/matches/challenge", json=payload)
        if r.status_code != 429:
            return r
        wait = float(r.headers.get("Retry-After", "8")) or 8.0
        time.sleep(wait + 1)
    return r  # 最后一次的响应（仍 429 则交由调用方处理）


def _paced_human(api: Api, token: str, payload: dict, *, retries: int = 2) -> httpx.Response:
    """发起人类对局，遇 429 限流则等待重试（/api/matches/human 走 _API_DEFAULT 120/60s）。"""
    for _ in range(retries):
        r = api.authed(token, "POST", "/api/matches/human", json=payload)
        if r.status_code != 429:
            return r
        wait = float(r.headers.get("Retry-After", "5")) or 5.0
        time.sleep(wait + 1)
    return r


def _all_bot_names(db_path: str, owner_username: str) -> list[str]:
    con = sqlite3.connect(db_path)
    try:
        rows = con.execute(
            "SELECT b.name FROM bots b JOIN users u ON b.owner_id=u.id WHERE u.username=?",
            (owner_username,),
        ).fetchall()
        return [r[0] for r in rows]
    finally:
        con.close()


def _bot_id_by_name(db_path: str, name: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        return int(con.execute("SELECT id FROM bots WHERE name=?", (name,)).fetchone()[0])
    finally:
        con.close()


# ── 阶段 3：SSE snapshot ──────────────────────────────────────
def phase3_sse(api: Api, ctx: dict[str, Any]) -> None:
    print("\n=== 阶段 3：SSE 连接与 snapshot 恢复 ===")
    u1, u2 = ctx["user_names"][0], ctx["user_names"][1]
    tok = ctx["tokens"][u1]
    # 发起一局 holdem，订阅 SSE
    r = _paced_challenge(api, tok, {
        "my_bot_id": ctx["bots"][u1]["holdem"], "opponent_bot_id": ctx["bots"][u2]["holdem"],
        "game_id": "holdem",
    })
    check("发起 SSE 观赛对局", r.status_code == 200, r.text[:80])
    mid = r.json()["match_id"]

    # 订阅并收集非 ping 事件；正常契约下首帧 snapshot 会令线程立即退出。
    # 此阶段只证明连接可建立、首帧 snapshot 结构可用；不等待也不声称验证
    # snapshot 之后的实时增量事件。
    sse_events: list[dict] = []
    stop = {"flag": False}

    def stream():
        url = f"{api.base}/api/matches/{mid}/events"
        try:
            with httpx.stream("GET", url, timeout=40) as resp:
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
                                sse_events.append(ev)
                                if ev.get("type") in ("snapshot", "match_end", "error"):
                                    return
                            except json.JSONDecodeError:
                                pass
                        data_line = ""
                        ev_type = None
        except Exception as e:
            warn(f"SSE 流异常: {e}")

    t = threading.Thread(target=stream, daemon=True)
    t.start()
    t.join(timeout=30)
    stop["flag"] = True
    # 等对局完成，避免遗留
    try:
        api.wait_match(tok, mid, timeout=60)
    except Exception:
        pass

    check("SSE 收到 snapshot 首事件", bool(sse_events) and sse_events[0].get("type") == "snapshot",
          str(sse_events[0].get("type") if sse_events else "空"))
    # snapshot 应含 match 与 events 字段
    if sse_events and sse_events[0].get("type") == "snapshot":
        snap = sse_events[0]
        check("snapshot 含 match 字段", "match" in snap, str(snap.keys()))
        check("snapshot 含 events 历史列表", "events" in snap and isinstance(snap["events"], list), str(snap.keys()))


def _websocket_dependencies():
    """Return the optional WS client modules, or ``None`` when unavailable."""
    try:
        import asyncio
        import websockets
    except ImportError:
        return None
    return websockets, asyncio


# ── 阶段 4：人类 vs Bot（WebSocket /play）─────────────────────
def phase4_human(api: Api, ctx: dict[str, Any]) -> None:
    print("\n=== 阶段 4：人类 vs Bot（WebSocket /play）===")
    dependencies = _websocket_dependencies()
    if dependencies is None:
        check(
            "人类对战 WebSocket 客户端依赖可用",
            False,
            "缺少 Python websockets 包；未执行三游戏 /play，验收不得通过",
        )
        return
    websockets, asyncio = dependencies

    human_user = ctx["user_names"][2]
    htok = ctx["tokens"][human_user]

    # 对手 bot（holdem/gomoku/pencil 各选）；每个场景用不同的人类用户（per-user ≤1 并发人类局）
    opp_user = ctx["user_names"][3]
    human_users = [ctx["user_names"][2], ctx["user_names"][4], ctx["user_names"][5]]
    scenarios = [
        ("holdem", opp_user, {"human_seat": 1}, human_users[0]),
        ("gomoku", opp_user, {"human_seat": 1}, human_users[1]),
        ("pencil", opp_user, {"human_seat": 1}, human_users[2]),
    ]

    # 赛前 Glicko 快照（人类局不应更新 rating）
    opp_ids = [ctx["bots"][opp_user][g] for g, _, _, _ in scenarios]
    before = _rating_snapshot(api.db_path, opp_ids)

    for idx, (game, opp, cfg, hu) in enumerate(scenarios):
        bot_id = ctx["bots"][opp][game]
        hu_tok = ctx["tokens"][hu]
        r = _paced_human(api, hu_tok, {"bot_id": bot_id, "game_id": game, **cfg})
        if r.status_code != 200:
            check(f"人类对战 {game} 建局", False, f"{r.status_code} {r.text[:80]}")
            continue
        mid = r.json()["match_id"]
        check(f"人类对战 {game} 建局", True)

        # 并发提第二局（同一用户）应被拒（per-user ≤1）—— 仅 holdem 场景测一次
        if idx == 0:
            r2 = _paced_human(api, hu_tok, {"bot_id": bot_id, "game_id": game, **cfg})
            check("同一用户并发第二人类局被拒", r2.status_code in (400, 409), f"{r2.status_code} {r2.text[:60]}")

        # WS 玩完这局
        ok = _play_human_match(api, websockets, asyncio, mid, hu_tok, game)
        check(f"人类对战 {game} WS 对局完成", ok, "未正常结束")

        # WS match_end 可能略早于最终 DB commit；经 GET 短暂轮询终态后仍必须是 completed。
        try:
            m = api.wait_match(hu_tok, mid, timeout=30)
            persisted = True
            persist_detail = f"status={m.get('status')}"
        except Exception as exc:
            m = {}
            persisted = False
            persist_detail = str(exc)
        check(
            f"人类对战 {game} 持久化 status=completed",
            persisted and m.get("status") == "completed",
            persist_detail,
        )
        check(f"人类对战 {game} match_type=human", m.get("match_type") == "human", f"mt={m.get('match_type')}")

    # 赛后 Glicko 对比（人类局不更新）
    after = _rating_snapshot(api.db_path, opp_ids)
    unchanged = all(before[bid] == after[bid] for bid in opp_ids)
    check("人类对局不更新 Glicko（对手 rating 不变）", unchanged,
          f"before={before} after={after}")


def _rating_snapshot(db_path: str, bot_ids: list[int]) -> dict[int, tuple]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        out = {}
        for bid in bot_ids:
            row = con.execute(
                "SELECT rating, rd, vol, matches_played FROM ratings WHERE bot_id=?", (bid,)
            ).fetchone()
            out[bid] = (row["rating"], row["rd"], row["vol"], row["matches_played"]) if row else None
        return out
    finally:
        con.close()


def _play_human_match(api: Api, websockets, asyncio, mid: str, token: str, game: str) -> bool:
    """连接 WS /play，回应 your_turn 直到 match_end/error。"""
    ws_base = api.base.replace("http://", "ws://").replace("https://", "wss://")
    url = f"{ws_base}/api/matches/{mid}/play?token={token}"

    async def play() -> bool:
        try:
            async with websockets.connect(url, max_size=None, open_timeout=20) as ws:
                while True:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=180)
                    except asyncio.TimeoutError:
                        return False
                    ev = json.loads(raw) if isinstance(raw, str) else raw
                    et = ev.get("type")
                    if et == "snapshot":
                        continue
                    if et == "your_turn":
                        req = ev.get("request", {})
                        move = _human_move(game, req)
                        await ws.send(json.dumps(move))
                        continue
                    if et == "error":
                        warn(f"WS {game} 返回 error: {ev.get('message') or ev}")
                        return False
                    if et == "match_end":
                        return True
        except Exception as e:
            warn(f"WS {game} 异常: {e}")
            return False

    return asyncio.run(play())


def _human_move(game: str, req: dict) -> dict:
    """根据游戏与引擎 request 生成合法人类着。"""
    if game == "holdem":
        # Botzone TexasHoldem2p 协议：裸整数或 {"response": int}；
        # 0 = check/call。旧 {"a":"c"} 会被协议层判为非法动作。
        return {"response": 0}
    # 棋类：req 含 x/y（对方上一手）+ me；回一个合法空位
    # 简化策略：gomoku 下中心附近；pencil 下一条边
    if game == "gomoku":
        size = req.get("size") or 15
        cx = cy = size // 2
        # 若中心被占，就近找空位（无法查盘，赌概率）
        return {"x": cx, "y": cy}
    if game == "pencil":
        # pass=1 时必须回 pass
        if req.get("pass") == 1:
            return {"x": -1, "y": -1}
        # 否则下一条边：n_dots 决定交错网格
        n = req.get("n_dots") or 6
        # 下一合法边（水平），赌不重复
        return {"x": 1, "y": 0}
    return {"x": 0, "y": 0}


# ── 阶段 5：赛事全生命周期 ────────────────────────────────────
def phase5_contest(api: Api, ctx: dict[str, Any]) -> None:
    print("\n=== 阶段 5：赛事全生命周期 ===")
    # 模板列表（公开）已阶段 0 覆盖；这里覆盖 create/open/register/dispatch/start/resume/advance/detail
    org1_tok = ctx["tokens"][ctx["org_names"][0]]
    org2_tok = ctx["tokens"][ctx["org_names"][1]]

    # org1 办 holdem Swiss-KO，org2 办 gomoku round-robin
    contests = []
    r = api.authed(org1_tok, "POST", "/api/contests", json={
        "title": "LoadTest Holdem Swiss-KO", "template_id": "holdem_swiss_ko", "game_id": "holdem",
        "match_config": {},
    })
    check("org1 创建 holdem Swiss-KO 赛事", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    if r.status_code == 200:
        contests.append(("holdem", r.json()["contest"]["id"], org1_tok, ctx["org_names"][0]))

    r = api.authed(org2_tok, "POST", "/api/contests", json={
        "title": "LoadTest Gomoku RR", "template_id": "gomoku_swiss_ko", "game_id": "gomoku",
    })
    check("org2 创建 gomoku 赛事", r.status_code == 200, f"{r.status_code} {r.text[:80]}")
    if r.status_code == 200:
        contests.append(("gomoku", r.json()["contest"]["id"], org2_tok, ctx["org_names"][1]))

    for game, cid, org_tok, org_name in contests:
        # open
        r = api.authed(org_tok, "POST", f"/api/contests/{cid}/open")
        check(f"[{game}] open 报名", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

        # 8 个用户报名（用各自该游戏 bot）
        entrants = ctx["user_names"][:8]
        for uname in entrants:
            utok = ctx["tokens"][uname]
            bot_id = ctx["bots"][uname][game]
            r = api.authed(utok, "POST", f"/api/contests/{cid}/register", json={"bot_id": bot_id})
            check(f"[{game}] {uname} 报名", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

        # dispatch（换 bot）：第一个报名者换一个 bot（用同游戏另一账号的 bot 不行——dispatch 必须是自己的；
        # 这里用一个报名者重新 dispatch 自己的同一 bot，验证端点可达）
        u0 = entrants[0]
        r = api.authed(ctx["tokens"][u0], "POST", f"/api/contests/{cid}/dispatch",
                       json={"bot_id": ctx["bots"][u0][game]})
        check(f"[{game}] dispatch（换 bot）端点可达", r.status_code in (200, 400), f"{r.status_code} {r.text[:80]}")

        # start
        r = api.authed(org_tok, "POST", f"/api/contests/{cid}/start")
        check(f"[{game}] start 启动", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

        # 轮询直到 finished（maybe_finish 自动推进；遇 rest 期 resume）
        deadline = time.time() + 400
        finished = False
        last_status = None
        while time.time() < deadline:
            r = api.client.get(f"/api/contests/{cid}")
            c = r.json()["contest"]
            last_status = c.get("status")
            if last_status == "finished":
                finished = True
                break
            if last_status == "rest":
                # 主动 resume（organizer）
                api.authed(org_tok, "POST", f"/api/contests/{cid}/resume")
            time.sleep(2)
        check(f"[{game}] 赛事自动/推进到 finished", finished, f"last_status={last_status}")

        # detail 校验
        r = api.client.get(f"/api/contests/{cid}")
        d = r.json()
        pairings = d.get("pairings", [])
        standings = d.get("standings", [])
        stage_results = d.get("stage_results", [])
        entries = d.get("entries", [])
        check(f"[{game}] detail 含 pairings", isinstance(pairings, list) and len(pairings) > 0, "空")
        check(f"[{game}] detail 含 standings", isinstance(standings, list) and len(standings) > 0, "空")
        check(f"[{game}] detail 含 stage_results", isinstance(stage_results, list), "缺")
        # entries/pairings 含 bot 名（PR-6 对阵图显示 Bot 名）
        if entries:
            check(f"[{game}] detail entries 含 bot_name 字段", "bot_name" in entries[0], str(entries[0])[:80])
        if pairings:
            check(f"[{game}] detail pairings 含 bot_a_name 字段", "bot_a_name" in pairings[0], str(pairings[0])[:80])
        # bracket 端点（PR-6）
        r = api.client.get(f"/api/contests/{cid}/bracket")
        check(f"[{game}] GET /api/contests/{{id}}/bracket", r.status_code == 200 and "pairings" in r.json(), r.text[:80])
        # contest 对局 match_type=contest 且不更新 Glicko：抽 1 场
        for p in pairings[:1]:
            pmid = p.get("match_id")
            if pmid:
                pm = api.client.get(f"/api/matches/{pmid}").json()["match"]
                check(f"[{game}] 赛事对局 match_type=contest", pm.get("match_type") == "contest",
                      f"mt={pm.get('match_type')}")
                break


# ── 阶段 6：自动对局（ladder）─────────────────────────────────
def phase6_auto_match(
    api: Api,
    ctx: dict[str, Any],
    *,
    allow_miss: bool = False,
) -> None:
    print("\n=== 阶段 6：自动对局（ladder）===")
    admin_tok = ctx["admin_token"]

    # 记录赛前 daily_count
    r = api.authed(admin_tok, "GET", "/api/admin/settings/runtime")
    before_cfg = r.json()
    before_count = before_cfg.get("auto_match", {}).get("daily_count", 0)
    check("GET runtime settings（含 auto_match）", r.status_code == 200 and "auto_match" in before_cfg, r.text[:80])

    # 记录赛前 ladder 对局数
    before_ladder = _count_ladder_matches(api.db_path)

    # 催化：开 enabled + min_idle=0 + interval=2 + reserve=0 + stale=0（关陈旧过滤，否则刚跑过的 bot 被排除）+ cooldown=0
    orig = {
        "auto_match_enabled": before_cfg["auto_match"]["enabled"],
        "auto_match_min_idle_sec": before_cfg["auto_match"]["min_idle_sec"],
        "auto_match_interval_sec": before_cfg["auto_match"]["interval_sec"],
        "auto_match_reserve_slots": before_cfg["auto_match"]["reserve_slots"],
        "auto_match_stale_sec": before_cfg["auto_match"]["stale_sec"],
        "auto_match_bot_cooldown": before_cfg["auto_match"]["bot_cooldown"],
    }
    r = api.authed(admin_tok, "PATCH", "/api/admin/settings/runtime", json={
        "auto_match_enabled": True, "auto_match_min_idle_sec": 0,
        "auto_match_interval_sec": 2, "auto_match_reserve_slots": 0,
        "auto_match_stale_sec": 0, "auto_match_bot_cooldown": 0,
    })
    check("PATCH runtime 催化 auto-match", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

    # 等待 auto-match 触发（_is_idle 需连续两轮 interval，min_idle=0、interval=2 下约 2 个 interval ≈ 4-8s）。
    time.sleep(25)

    after_count = api.authed(admin_tok, "GET", "/api/admin/settings/runtime").json()["auto_match"]["daily_count"]
    after_ladder = _count_ladder_matches(api.db_path)
    # 恢复原设置
    api.authed(admin_tok, "PATCH", "/api/admin/settings/runtime", json={
        "auto_match_enabled": orig["auto_match_enabled"],
        "auto_match_min_idle_sec": orig["auto_match_min_idle_sec"],
        "auto_match_interval_sec": orig["auto_match_interval_sec"],
        "auto_match_reserve_slots": orig["auto_match_reserve_slots"],
        "auto_match_stale_sec": orig["auto_match_stale_sec"],
        "auto_match_bot_cooldown": orig["auto_match_bot_cooldown"],
    })

    _record_auto_match_outcome(
        before_count,
        after_count,
        before_ladder,
        after_ladder,
        allow_miss=allow_miss,
    )


def _record_auto_match_outcome(
    before_count: int,
    after_count: int,
    before_ladder: int,
    after_ladder: int,
    *,
    allow_miss: bool = False,
) -> None:
    """Record an auto-match observation without allowing silent false coverage."""
    grew = after_count > before_count or after_ladder > before_ladder
    detail = (
        f"daily_count {before_count}→{after_count}; "
        f"ladder {before_ladder}→{after_ladder}"
    )
    if grew:
        check("auto-match 触发（daily_count 或 ladder 对局增长）", True,
              detail)
    elif allow_miss:
        warn(
            f"auto-match 未触发（{detail}）；已显式启用 --allow-auto-match-miss，"
            "本次运行只能用于诊断，不能作为 auto-match 验收证据"
        )
    else:
        check(
            "auto-match 触发（daily_count 或 ladder 对局增长）",
            False,
            f"{detail}；默认验收要求观察到真实 ladder 增长",
        )


def _count_ladder_matches(db_path: str) -> int:
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT COUNT(*) FROM matches WHERE match_type='ladder'").fetchone()
        return int(row[0])
    finally:
        con.close()


# ── 阶段 7：Admin 关键端点 ────────────────────────────────────
def phase7_admin(api: Api, ctx: dict[str, Any]) -> None:
    print("\n=== 阶段 7：Admin 关键端点 ===")
    tok = ctx["admin_token"]

    # GET 列表类
    for path, label in [
        ("/api/admin/users", "users"),
        ("/api/admin/stats", "stats"),
        ("/api/admin/bots", "bots"),
        ("/api/admin/contests", "contests"),
        ("/api/admin/email/templates", "email templates"),
        ("/api/admin/email/outbox?limit=5", "email outbox"),
        ("/api/admin/judges", "judges"),
        ("/api/admin/templates", "templates"),
        ("/api/admin/logs?limit=5", "logs"),
        ("/api/admin/settings/runtime", "runtime settings"),
    ]:
        r = api.authed(tok, "GET", path)
        check(f"GET /api/admin/{label}", r.status_code == 200, f"{r.status_code} {r.text[:60]}")

    # PATCH /api/admin/bots/{id}（改 display_name）
    u1 = ctx["user_names"][0]
    bid = ctx["bots"][u1]["gomoku"]
    r = api.authed(tok, "PATCH", f"/api/admin/bots/{bid}", json={"display_name": "loadtest-renamed"})
    check("PATCH /api/admin/bots/{id}（改 display_name）",
          r.status_code == 200 and r.json()["bot"]["display_name"] == "loadtest-renamed", r.text[:80])
    # 改回
    api.authed(tok, "PATCH", f"/api/admin/bots/{bid}", json={"display_name": f"{u1}_gomoku"})

    # GET /api/admin/bots/{id}/versions
    r = api.authed(tok, "GET", f"/api/admin/bots/{bid}/versions")
    check("GET /api/admin/bots/{id}/versions", r.status_code == 200 and "versions" in r.json(), r.text[:80])

    # PATCH /api/admin/users/{id}（设 email_verified，已 verified 应无副作用）
    uid = _user_id(api.db_path, u1)
    r = api.authed(tok, "PATCH", f"/api/admin/users/{uid}", json={"email_verified": True})
    check("PATCH /api/admin/users/{id}", r.status_code == 200 and r.json()["user"]["email_verified"] in (1, True), r.text[:80])

    # POST /api/admin/users/{id}/role（promote 一个 user→organizer 再降回）
    r = api.authed(tok, "POST", f"/api/admin/users/{uid}/role?role=organizer")
    check("POST role?role=organizer（提权）", r.status_code == 200 and r.json()["user"]["role"] == "organizer", r.text[:80])
    r = api.authed(tok, "POST", f"/api/admin/users/{uid}/role?role=user")
    check("POST role?role=user（降回）", r.status_code == 200 and r.json()["user"]["role"] == "user", r.text[:80])

    # DELETE /api/admin/users/{id}/sessions（撤销会话，验该 token 后续 401）
    victim = ctx["user_names"][5]
    vt = ctx["tokens"][victim]
    vid = _user_id(api.db_path, victim)
    r = api.authed(tok, "DELETE", f"/api/admin/users/{vid}/sessions")
    check("DELETE /api/admin/users/{id}/sessions", r.status_code == 200, r.text[:80])
    r2 = api.authed(vt, "GET", "/api/auth/me")
    check("被撤销 session 后 token 失效", r2.status_code == 401, f"{r2.status_code}")
    # 重新发 token（保证幂等重跑）
    store = Store(api.db_path)
    store._conn.execute("PRAGMA busy_timeout=10000")
    ntok = new_session_token()
    store.add_session(ntok, vid, session_expires())
    store.close()
    ctx["tokens"][victim] = ntok

    # GET /api/admin/users/{id}/sessions
    r = api.authed(tok, "GET", f"/api/admin/users/{vid}/sessions")
    check("GET /api/admin/users/{id}/sessions", r.status_code == 200 and "sessions" in r.json(), r.text[:80])

    # PATCH /api/admin/matches/{id}（强制一场 pending→aborted）：先发起一场，立即 abort
    r = _paced_challenge(api, ctx["tokens"][u1], {
        "my_bot_id": ctx["bots"][u1]["holdem"], "opponent_bot_id": ctx["bots"][ctx["user_names"][1]]["holdem"],
        "game_id": "holdem",
    })
    if r.status_code == 200:
        amid = r.json()["match_id"]
        r = api.authed(tok, "PATCH", f"/api/admin/matches/{amid}", json={"status": "aborted", "reason": "loadtest-abort"})
        check("PATCH /api/admin/matches/{id}（强制 aborted）", r.status_code == 200 and r.json()["match"]["status"] == "aborted", r.text[:80])
    else:
        warn(f"阶段7 强制 abort 对局发起失败 {r.status_code}")

    # PATCH /api/admin/settings/runtime：调 max_concurrent_matches（应被 ceiling 钳制，超限 400）
    r = api.authed(tok, "GET", "/api/admin/settings/runtime")
    ceiling = r.json().get("ceiling")
    cur = r.json().get("max_concurrent_matches")
    # 超限应 400
    if ceiling:
        r = api.authed(tok, "PATCH", "/api/admin/settings/runtime", json={"max_concurrent_matches": ceiling + 5})
        check("PATCH max_concurrent 超 ceiling 被拒", r.status_code == 400, f"{r.status_code} {r.text[:60]}")
    # 合法值
    r = api.authed(tok, "PATCH", "/api/admin/settings/runtime", json={"max_concurrent_matches": max(1, (cur or 2))})
    check("PATCH max_concurrent 合法值", r.status_code == 200, f"{r.status_code} {r.text[:60]}")

    # 站点配置（PR-10）
    r = api.authed(tok, "PATCH", "/api/admin/settings/site", json={"announcement": "loadtest 公告"})
    check("PATCH /api/admin/settings/site", r.status_code == 200 and r.json()["site"]["announcement"] == "loadtest 公告", r.text[:80])

    # GET/PUT /api/admin/email/templates/{key}
    r = api.authed(tok, "GET", "/api/admin/email/templates/welcome")
    if r.status_code == 200:
        tpl = r.json().get("template", {})
        subj = tpl.get("subject", "欢迎")
        r = api.authed(tok, "PUT", "/api/admin/email/templates/welcome",
                       json={"subject": subj, "body_html": "<p>loadtest</p>", "body_text": "loadtest"})
        check("PUT /api/admin/email/templates/welcome", r.status_code == 200, f"{r.status_code} {r.text[:60]}")
    else:
        warn(f"email template welcome 不可读 {r.status_code}（跳过 PUT）")

    # GET/PATCH /api/admin/judges：棋盘/手数等规则已由 GameSpec 钉死；这里只验证
    # 当前仍可调的 holdem 盲注关系校验，避免压测依赖已删除的 gomoku size 参数。
    r = api.authed(tok, "GET", "/api/admin/judges")
    check("GET /api/admin/judges", r.status_code == 200 and "games" in r.json(), r.text[:80])

    # 验 judges bb≤sb 报错（key=judge_holdem_sb / judge_holdem_bb）
    r = api.authed(tok, "PATCH", "/api/admin/judges/params",
                   json={"params": {"judge_holdem_sb": 100, "judge_holdem_bb": 50}})
    check("PATCH judges bb<sb 被拒", r.status_code == 400, f"{r.status_code} {r.text[:60]}")

    # GET/POST/PUT/DELETE /api/admin/templates：建自定义模板→preview→删
    r = api.authed(tok, "POST", "/api/admin/templates", json={
        "id": "loadtest_custom", "name": "LoadTest Custom", "game_id": "holdem",
        "match_config": {},
        "stages": [{"key": "s1", "type": "round_robin", "scoring": "poker_3_1_0"}],
    })
    check("POST /api/admin/templates（建自定义）", r.status_code == 200, f"{r.status_code} {r.text[:60]}")
    # preview
    r = api.authed(tok, "POST", "/api/admin/templates/preview", json={
        "stages": [{"key": "s1", "type": "round_robin", "scoring": "poker_3_1_0"}], "n": 8,
    })
    check("POST /api/admin/templates/preview", r.status_code == 200 and "total" in r.json(), r.text[:80])
    # PUT
    r = api.authed(tok, "PUT", "/api/admin/templates/loadtest_custom", json={
        "id": "loadtest_custom", "name": "LoadTest Custom v2", "game_id": "holdem",
        "match_config": {},
        "stages": [{"key": "s1", "type": "round_robin", "scoring": "poker_3_1_0"}],
    })
    check("PUT /api/admin/templates/{tid}", r.status_code == 200, f"{r.status_code} {r.text[:60]}")
    # DELETE
    r = api.authed(tok, "DELETE", "/api/admin/templates/loadtest_custom")
    check("DELETE /api/admin/templates/{tid}", r.status_code == 200, f"{r.status_code} {r.text[:60]}")

    # POST /api/auth/admin/create-reset-token
    r = api.authed(tok, "POST", "/api/auth/admin/create-reset-token", json={"username_or_email": u1})
    check("POST /api/auth/admin/create-reset-token", r.status_code == 200, f"{r.status_code} {r.text[:80]}")

    # admin GET contest entries（用阶段 5 的赛事，若有）
    r = api.authed(tok, "GET", "/api/admin/contests?status=finished")
    finished = r.json().get("contests", [])
    if finished:
        cid = finished[0]["id"]
        r = api.authed(tok, "GET", f"/api/admin/contests/{cid}/entries")
        check("GET /api/admin/contests/{id}/entries", r.status_code == 200 and "entries" in r.json(), r.text[:80])


# ── 主流程 ────────────────────────────────────────────────────
def main() -> int:
    global PASS, FAIL
    ap = argparse.ArgumentParser(description="botbattle 大规模系统压测")
    ap.add_argument("--base", default=os.environ.get("BZ_TEST_BASE", "http://127.0.0.1:50381"))
    ap.add_argument("--db", default=os.environ.get("BZ_DB_PATH", "botzone.db"))
    ap.add_argument("--users", type=int, default=N_USERS)
    ap.add_argument(
        "--upload-root",
        default=None,
        help="Bot 产物目录（默认 <db.parent>/bot_uploads；相对路径也基于 db.parent）",
    )
    ap.add_argument("--skip-seed", action="store_true", help="跳过种子（假设已种好）")
    ap.add_argument("--no-throttle", action="store_true",
                    help="跳过挑战节流（用于服务端已设 BZ_RATE_LIMIT=0 关限流时，大幅加速阶段 2/3）")
    ap.add_argument(
        "--allow-auto-match-miss",
        action="store_true",
        help=(
            "仅诊断：auto-match 未触发时记 warning 而非失败；"
            "启用后的结果不能作为 auto-match 验收证据"
        ),
    )
    args = ap.parse_args()

    global NO_THROTTLE
    NO_THROTTLE = args.no_throttle
    if NO_THROTTLE:
        print("  ⚡ 已启用 --no-throttle（假设服务端 BZ_RATE_LIMIT=0）；挑战将不节流")

    base = ensure_qa_base(args.base)
    db_path = str(qa_db_path(args.db, ROOT))
    assert_qa_instance(base)
    print(f"botbattle 大规模系统压测\n  base={base}  db={db_path}  users={args.users}")

    # 健康检查
    api = Api(base, db_path)
    try:
        h = api.client.get("/api/health")
        if h.status_code != 200:
            print(f"✗ QA 服务不可达：{h.status_code} {h.text[:80]}")
            print(
                "  请先在 linked worktree 启动："
                "BZ_DB_PATH=$PWD/botzone.db BZ_QA_INSTANCE=1 "
                "python -m bzplat.backend.cli serve --port 50381"
            )
            return 2
    except Exception as e:
        print(f"✗ QA 服务不可达：{e}")
        print(
            "  请先在 linked worktree 启动："
            "BZ_DB_PATH=$PWD/botzone.db BZ_QA_INSTANCE=1 "
            "python -m bzplat.backend.cli serve --port 50381"
        )
        return 2
    print(f"  服务在线：{h.json()}")

    # 种子
    if args.skip_seed:
        # 仍需重建 ctx：从 DB 读 load_* 用户与 bot，并生成 token
        print("\n=== 跳过种子，从 DB 重建上下文 ===")
        ctx = _rebuild_ctx(db_path)
    else:
        ctx = seed(db_path, args.users, args.upload_root)

    t0 = time.time()
    try:
        phase0_basics(api, ctx)
        phase1_bots(api, ctx)
        phase2_matches(api, ctx)
        phase3_sse(api, ctx)
        phase4_human(api, ctx)
        phase5_contest(api, ctx)
        phase6_auto_match(api, ctx, allow_miss=args.allow_auto_match_miss)
        phase7_admin(api, ctx)
    except KeyboardInterrupt:
        print("\n中断")
        return 130

    dt = time.time() - t0
    print(f"\n{'='*60}")
    print(f"压测完成：{PASS} passed / {FAIL} failed / {len(WARN)} warns，总耗时 {dt:.1f}s")
    if WARN:
        print("警告：")
        for w in WARN:
            print(f"  ⚠ {w}")
    if FAILS:
        print("失败明细：")
        for f in FAILS:
            print(f"  ✗ {f}")
    return 0 if FAIL == 0 else 1


def _rebuild_ctx(db_path: str) -> dict[str, Any]:
    """从 DB 重建压测上下文；只给已验证的专用账号签发 token。"""
    db_path = str(qa_db_path(db_path, ROOT))
    store = Store(db_path)
    store._conn.execute("PRAGMA busy_timeout=10000")
    try:
        rows = store._conn.execute(
            "SELECT username FROM users WHERE username LIKE 'load\\_%' ESCAPE '\\' "
            "ORDER BY username"
        ).fetchall()
        names = [row["username"] for row in rows]
        user_names = [
            name
            for name in names
            if name.startswith("load_u")
            and name.removeprefix("load_u").isdigit()
        ]
        org_names = [
            f"load_org{i}"
            for i in range(1, N_ORGS + 1)
            if f"load_org{i}" in names
        ]
        admin_name = LOAD_ADMIN_NAME if LOAD_ADMIN_NAME in names else None
        if not user_names or len(org_names) != N_ORGS or admin_name is None:
            raise RuntimeError(
                "--skip-seed 需要完整的专用 load_* 账号集合；请先安全运行 seed"
            )

        specs = [load_user_spec(name) for name in user_names]
        specs.extend(load_org_spec(name) for name in org_names)
        specs.append(load_admin_spec())
        preflight_dedicated_accounts(store, specs)

        validated_users: list[tuple[QaAccountSpec, dict]] = []
        for spec in specs:
            user = inspect_dedicated_account(store, spec)
            assert user is not None
            if not user.get("is_active") or not user.get("email_verified"):
                raise RuntimeError(
                    f"拒绝为未激活/未验证的专用 QA 账号 {spec.username!r} 签发会话"
                )
            validated_users.append((spec, user))

        tokens: dict[str, str] = {}
        bots: dict[str, dict[str, int]] = {}
        for spec, user in validated_users:
            token = new_session_token()
            store.add_session(token, user["id"], session_expires())
            tokens[spec.username] = token

        for uname in user_names:
            user = store.get_user_by_username(uname)
            for gid in GAMES:
                bname = f"{uname}_{gid}"
                row = store._conn.execute(
                    "SELECT id FROM bots WHERE owner_id=? AND name=?",
                    (user["id"], bname),
                ).fetchone()
                if row:
                    bots.setdefault(uname, {})[gid] = row["id"]
        return {
            "tokens": tokens,
            "bots": bots,
            "user_names": user_names,
            "org_names": org_names,
            "admin_name": admin_name,
            "admin_token": tokens[admin_name],
            "org_tokens": [tokens[name] for name in org_names],
        }
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
