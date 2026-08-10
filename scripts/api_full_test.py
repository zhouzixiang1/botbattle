#!/usr/bin/env python3
"""通过 HTTP API 做关键业务链路集成测试。

覆盖：
  1. 无 SMTP 注册回滚契约 + 隔离 DB 专用账号播种 → 验证码登录 → /me
  2. Bot 上传 / 版本 / 上架 / 公开列表
  3. 挑战对局 + 结果完整性（零和、winner、净筹码）
  4. 已完成对局的 SSE 终态 snapshot vs 落盘回放 events_json 一致性
  5. 全局 admission 与补槽（首波只接纳代码并发上限，超额 429，释放后补齐）
  6. 排行榜 Glicko-2 更新、组织者比赛循环赛

前置：
  - 后端以 BZ_TEST_CAPTCHA=1 启动（captcha 接口返回 answer）
  - 后端以 BZ_QA_INSTANCE=1 标记为隔离 QA 实例
  - 不发送测试邮件：账号在隔离 DB 幂等播种；无 SMTP 注册原子性在独立临时 app 验证
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
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bzplat.backend.crypto import hash_password, verify_password  # noqa: E402
from bzplat.backend.store import Store  # noqa: E402
from bzplat.backend.store.schema import ROLE_ADMIN, ROLE_ORGANIZER, ROLE_USER  # noqa: E402
from scripts._qa_target import (  # noqa: E402
    assert_qa_instance,
    ensure_qa_base,
    qa_db_path,
    qa_runtime_path,
)

PASS = 0
FAIL = 0
FAILS: list[str] = []
PASSWORD = "ApiQa1234"
QA_ACCOUNTS = {
    "admin": ("apiqa_admin", "apiqa_admin@test.invalid", ROLE_ADMIN, True),
    "alice": ("apiqa_alice", "apiqa_alice@test.invalid", ROLE_USER, True),
    "bob": ("apiqa_bob", "apiqa_bob@test.invalid", ROLE_USER, True),
    "carol": ("apiqa_carol", "apiqa_carol@test.invalid", ROLE_USER, True),
    "org1": ("apiqa_org1", "apiqa_org1@test.invalid", ROLE_ORGANIZER, True),
    "unverified": (
        "apiqa_unverified",
        "apiqa_unverified@test.invalid",
        ROLE_USER,
        False,
    ),
}


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


def verify_no_smtp_registration_rollback() -> tuple[bool, str]:
    """Exercise the HTTP registration compensation contract without real SMTP.

    This uses a disposable app/runtime, explicitly forces ``mailer=None`` and runs
    the same request twice.  It cannot send mail or mutate the target QA database.
    """
    from fastapi.testclient import TestClient
    from bzplat.backend.main import create_app

    with tempfile.TemporaryDirectory(prefix="botbattle-api-register-") as raw_runtime:
        runtime = Path(raw_runtime)
        db_path = runtime / "register.db"
        env = {
            "BZ_QA_INSTANCE": "1",
            "BZ_SKIP_CAPTCHA": "1",
            "BZ_DB_PATH": str(db_path),
            "BZ_AVATAR_DIR": str(runtime / "avatars"),
            "BZ_LOG_DIR": str(runtime / "logs"),
            "SMTP_HOST": "",
            "SMTP_USER": "",
            "SMTP_PASSWORD": "",
            "SMTP_FROM": "",
        }
        with patch.dict(os.environ, env, clear=False):
            app = create_app(
                db_path=str(db_path),
                upload_root=runtime / "bot_uploads",
            )
            app.state.auth.mailer = None
            client = TestClient(app)
            payload = {
                "username": "apirollback",
                "email": "apirollback@test.invalid",
                "password": PASSWORD,
                "captcha_id": "skip",
                "captcha_answer": "skip",
            }
            statuses: list[int] = []
            remains: list[bool] = []
            try:
                for _ in range(2):
                    response = client.post("/api/auth/register", json=payload)
                    statuses.append(response.status_code)
                    remains.append(
                        app.state.store.get_user_by_username("apirollback") is not None
                    )
            finally:
                client.close()
                app.state.store.close()
    ok = statuses == [503, 503] and remains == [False, False]
    return ok, f"statuses={statuses} user_remains={remains}"


def seed_qa_accounts(db_path: str) -> dict[str, str]:
    """Idempotently seed dedicated accounts into an already-isolated QA DB."""
    resolved_db = qa_db_path(db_path, ROOT)
    resolved_db.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(resolved_db))
    usernames: dict[str, str] = {}
    try:
        for logical, (username, email, role, verified) in QA_ACCOUNTS.items():
            existing = store.get_user_by_username(username)
            if existing:
                if (
                    existing.get("email") != email
                    or existing.get("role") != role
                    or not verify_password(PASSWORD, existing.get("password_hash") or "")
                ):
                    raise RuntimeError(
                        f"拒绝改写已有专用账号 {username!r}：邮箱、角色或密码不匹配"
                    )
                user = existing
            else:
                user = store.create_user(
                    username,
                    email,
                    hash_password(PASSWORD),
                    display_name=username,
                    role=role,
                )
            store.update_user(
                user["id"],
                email_verified=1 if verified else 0,
                is_active=1,
            )
            usernames[logical] = username
    finally:
        store.close()
    return usernames


def bot_artifacts_are_isolated(api: Api, bot_ids: list[int]) -> tuple[bool, str]:
    """Verify the target service did not persist uploaded binaries in main runtime dirs."""
    con = sqlite3.connect(f"file:{api.db_path}?mode=ro", uri=True)
    try:
        placeholders = ",".join("?" for _ in bot_ids)
        rows = con.execute(
            f"SELECT id, binary_path FROM bots WHERE id IN ({placeholders})",
            bot_ids,
        ).fetchall()
    finally:
        con.close()
    if len(rows) != len(bot_ids):
        return False, f"expected={len(bot_ids)} rows={len(rows)}"
    try:
        for _bot_id, binary_path in rows:
            if not binary_path:
                return False, f"bot {_bot_id} binary_path 为空"
            qa_runtime_path(str(Path(binary_path).resolve().parent), api.db_path, ROOT, "bot_uploads")
    except SystemExit as exc:
        return False, str(exc)
    return True, ""


def match_rounds_played(match: dict[str, Any]) -> int:
    """Read the unique persisted progress field from the result contract."""
    raw = (match.get("result") or {}).get("rounds_played")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def qa_contest_payload(run_id: str) -> dict[str, Any]:
    """Pin the flow to the single-stage round-robin contract it asserts."""
    return {
        "title": f"API QA Cup {run_id}",
        "template_id": "holdem_rr",
        "game_id": "holdem",
    }


# ─────────────────────────────────────────────────────────────
def test_auth_flow(api: Api) -> dict[str, str]:
    print("\n[1/6] 鉴权流程：无 SMTP 回滚 → 隔离播种 → 验证码登录")
    users: dict[str, str] = {}
    rollback_ok, rollback_detail = verify_no_smtp_registration_rollback()
    check("无 SMTP 注册失败会删除用户且可安全重试", rollback_ok, rollback_detail)

    usernames = seed_qa_accounts(api.db_path)
    for logical in ("admin", "alice", "bob", "carol", "org1"):
        token = api.login(usernames[logical], PASSWORD)
        check(f"专用账号登录 {logical}", bool(token), "无 token")
        users[logical] = token

    # 未验证用户不可登录（该账号由隔离 DB 播种，不调用发信接口）
    # 不验证邮箱直接登录应失败
    try:
        cap = api.captcha()
        r = api.client.post("/api/auth/login", json={
            "username": usernames["unverified"], "password": PASSWORD,
            "captcha_id": cap["captcha_id"], "captcha_answer": cap["answer"],
        })
        check("未验证邮箱登录被拒", r.status_code == 403, f"status={r.status_code}")
    except Exception as e:
        check("未验证邮箱登录被拒", False, str(e))

    # /me
    r = api.authed(users["alice"], "GET", "/api/auth/me")
    check(
        "/me 返回当前用户",
        r.json().get("user", {}).get("username") == usernames["alice"],
        r.text[:80],
    )
    return users


def test_bots(api: Api, users: dict[str, str], run_id: str) -> dict[str, int]:
    print("\n[2/6] Bot 上传 / 版本 / 上架")
    elf = read_sample("samples/callbot_linux_amd64")
    bot_ids: dict[str, int] = {}
    bot_names = {
        "AliceBot": f"ApiAlice_{run_id}",
        "BobBot": f"ApiBob_{run_id}",
        "CarolBot": f"ApiCarol_{run_id}",
    }
    for name, tag in (("alice", "AliceBot"), ("bob", "BobBot"), ("carol", "CarolBot")):
        upload_name = bot_names[tag]
        headers, body = multipart(
            {"name": upload_name, "is_public": "true"},
            "file",
            "bot.bin",
            elf,
        )
        r = api.authed(users[name], "POST", "/api/bots", headers=headers, content=body)
        check(
            f"上传 {tag} ({upload_name})",
            r.status_code == 200 and "bot" in r.json(),
            f"{r.status_code} {r.text[:80]}",
        )
        bot_ids[tag] = r.json()["bot"]["id"]

    artifacts_ok, artifacts_detail = bot_artifacts_are_isolated(
        api, list(bot_ids.values())
    )
    check("上传文件未落入 primary checkout 运行时目录", artifacts_ok, artifacts_detail)

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
        "my_bot_id": bot_ids["AliceBot"], "opponent_bot_id": bot_ids["BobBot"],
    })
    check("发起挑战", r.status_code == 200 and "match_id" in r.json(), f"{r.status_code} {r.text[:80]}")
    mid = r.json()["match_id"]
    m = wait_match(api, users["alice"], mid)
    check("对局完成(非 aborted)", m["status"] == "completed", f"status={m['status']} reason={m.get('reason')}")

    # 完整性：三游戏统一结果契约为 result.deltas（旧物理列已删除）。
    result = m.get("result") or {}
    deltas = result.get("deltas")
    valid_deltas = isinstance(deltas, list) and len(deltas) >= 2
    check("result.deltas 含双方结果", valid_deltas, str(result)[:120])
    if not valid_deltas:
        return mid
    ea, eb = int(deltas[0]), int(deltas[1])
    check("deltas 零和 (ea+eb==0)", ea + eb == 0, f"ea={ea} eb={eb}")
    # winner 与 deltas 一致
    if m["winner"] == 0:
        check("winner=0 时 ea>eb", ea > eb, f"ea={ea} eb={eb} winner={m['winner']}")
    elif m["winner"] == 1:
        check("winner=1 时 eb>ea", eb > ea, f"ea={ea} eb={eb} winner={m['winner']}")
    else:
        check("平局 ea==eb", ea == eb, f"ea={ea} eb={eb}")
    normalized_delta = result.get("normalized_delta")
    check(
        "result.normalized_delta == ea/100（Holdem 大盲单位）",
        normalized_delta is not None
        and abs(float(normalized_delta) - ea / 100.0) < 1e-6,
        f"normalized_delta={normalized_delta}",
    )
    return mid


def test_sse_vs_replay(api: Api, users: dict[str, str], bot_ids: dict[str, int]) -> None:
    print("\n[4/6] SSE 终态 snapshot vs 落盘回放一致性")
    # 发起并等待一局完成，再订阅 SSE。这里验证终态 snapshot 的完整历史，
    # 不订阅运行中对局，也不声称覆盖 snapshot 之后的实时增量。
    r = api.authed(users["alice"], "POST", "/api/matches/challenge", json={
        "my_bot_id": bot_ids["AliceBot"], "opponent_bot_id": bot_ids["CarolBot"],
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
    # 结构完整性：hand_start 数 == settle 数 == result.rounds_played
    hs = rep_types.count("hand_start")
    st = rep_types.count("settle")
    rounds_played = match_rounds_played(m)
    check(
        "replay hand_start==settle==result.rounds_played",
        rounds_played > 0 and hs == st == rounds_played,
        f"hs={hs} settle={st} rounds_played={rounds_played}",
    )
    # 每手都有 deal_hole
    dh = rep_types.count("deal_hole")
    check("replay deal_hole==hand_start", dh == hs, f"deal_hole={dh} hand_start={hs}")

    # 重新订阅已完成对局；终态 snapshot 应携带与 replay 完全一致的历史事件。
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
    print(f"\n[5/6] 并发 admission + 补槽（总计 {n} 局）")
    # 不同 bot 对，避免冲突：Alice vs Bob, Alice vs Carol, Carol vs Bob, Bob vs Carol
    pairs = [
        (bot_ids["AliceBot"], bot_ids["BobBot"], users["alice"]),
        (bot_ids["AliceBot"], bot_ids["CarolBot"], users["alice"]),
        (bot_ids["CarolBot"], bot_ids["BobBot"], users["carol"]),
        (bot_ids["BobBot"], bot_ids["CarolBot"], users["bob"]),
    ]
    pairs = (pairs * ((n + 3) // 4))[:n]

    health = api.client.get("/api/health").json()
    capacity = max(1, int(health.get("max_concurrent") or 1))
    expected_first_wave = min(n, capacity)
    results: dict[int, dict] = {}
    first_match_ids: dict[int, str] = {}
    rejected: dict[int, tuple[int, int, str]] = {}
    errors: dict[int, str] = {}
    barrier = threading.Barrier(n)

    def submit_first_wave(i: int, a: int, b: int, tok: str):
        try:
            barrier.wait()  # 尽量同时发起
            r = api.authed(tok, "POST", "/api/matches/challenge", json={
                "my_bot_id": a, "opponent_bot_id": b,
            })
            if r.status_code == 200:
                first_match_ids[i] = r.json()["match_id"]
                return
            if r.status_code == 429 and "并发已满" in r.text:
                rejected[i] = (a, b, tok)
                return
            errors[i] = f"challenge {r.status_code} {r.text[:80]}"
        except Exception as e:
            errors[i] = str(e)

    threads = [
        threading.Thread(target=submit_first_wave, args=(i, a, b, t))
        for i, (a, b, t) in enumerate(pairs)
    ]
    t0 = time.time()
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    first_wave_ok = (
        not errors
        and len(first_match_ids) == expected_first_wave
        and len(rejected) == n - expected_first_wave
    )
    check(
        f"首波精确接纳 {expected_first_wave} 局，超额请求明确 429",
        first_wave_ok,
        (
            f"accepted={len(first_match_ids)} rejected={len(rejected)} "
            + "; ".join(f"#{k}:{v}" for k, v in errors.items())
        ),
    )

    # 先等首波释放全局槽位，再把明确因 admission 拒绝的请求
    # 按同一 capacity 分批补进。这才是代码配置为 2 时的真实契约，
    # 不应把任意数量的 pending task 塞进 semaphore 后再声称“并发成功”。
    for i, mid in sorted(first_match_ids.items()):
        try:
            results[i] = wait_match(api, pairs[i][2], mid, timeout=120)
        except Exception as exc:
            errors[i] = str(exc)

    retry_items = sorted(rejected.items())
    for start in range(0, len(retry_items), capacity):
        batch = retry_items[start : start + capacity]
        retry_barrier = threading.Barrier(len(batch))

        def retry(i: int, a: int, b: int, tok: str) -> None:
            try:
                retry_barrier.wait()
                r = api.authed(tok, "POST", "/api/matches/challenge", json={
                    "my_bot_id": a, "opponent_bot_id": b,
                })
                if r.status_code != 200:
                    errors[i] = f"retry challenge {r.status_code} {r.text[:80]}"
                    return
                results[i] = wait_match(api, tok, r.json()["match_id"], timeout=120)
            except Exception as exc:
                errors[i] = str(exc)

        retry_threads = [
            threading.Thread(target=retry, args=(i, a, b, tok))
            for i, (a, b, tok) in batch
        ]
        for th in retry_threads:
            th.start()
        for th in retry_threads:
            th.join()

    dt = time.time() - t0
    completed = [m for m in results.values() if m["status"] == "completed"]
    check(
        f"补槽后全部 {n} 局 completed",
        len(completed) == n and not errors,
        (
            f"completed={len(completed)} "
            f"aborted={sum(1 for m in results.values() if m['status']=='aborted')} "
            + "; ".join(f"#{k}:{v}" for k, v in errors.items())
        ),
    )
    # 每局零和
    zero_sum = all(
        isinstance((m.get("result") or {}).get("deltas"), list)
        and len((m.get("result") or {})["deltas"]) >= 2
        and int((m.get("result") or {})["deltas"][0])
        + int((m.get("result") or {})["deltas"][1]) == 0
        for m in results.values()
    )
    check("每局 result.deltas 零和", zero_sum, "存在缺失或非零和对局")
    # 无重复 match_id
    check("无超时(<120s 总)", dt < 125, f"耗时 {dt:.1f}s")
    print(f"    {n} 局并发总耗时 {dt:.1f}s")


def test_leaderboard_and_contest(
    api: Api,
    users: dict[str, str],
    bot_ids: dict[str, int],
    run_id: str,
) -> None:
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
    r = api.authed(
        users["org1"],
        "POST",
        "/api/contests",
        json=qa_contest_payload(run_id),
    )
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
        # 积分按 (points desc, delta_total desc) 排序
        pts = [s.get("points", 0) for s in standings]
        check("standings 按积分降序", pts == sorted(pts, reverse=True), str(pts))
        check("standings 含 delta_total", "delta_total" in standings[0], str(standings[0])[:80])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=os.environ.get("BZ_TEST_BASE", "http://127.0.0.1:50382"))
    ap.add_argument("--db", default=os.environ.get("BZ_DB_PATH", ""))
    args = ap.parse_args()

    if not args.db:
        print("✗ 需要指定 --db 或 BZ_DB_PATH（读取验证码表）", file=sys.stderr)
        return 2

    base = ensure_qa_base(args.base)
    db_path = str(qa_db_path(args.db, ROOT))
    assert_qa_instance(base)
    print(f"API 关键链路集成测试  base={base}  db={db_path}")
    api = Api(base, db_path)

    # 健康检查
    try:
        h = api.client.get("/api/health").json()
        print(f"  health: {h}")
        assert h.get("ok"), "health.ok=False"
    except Exception as e:
        print(f"✗ 后端不可达: {e}", file=sys.stderr)
        return 2

    run_id = uuid.uuid4().hex[:8]
    try:
        users = test_auth_flow(api)
        bot_ids = test_bots(api, users, run_id)
        test_single_match(api, users, bot_ids)
        test_sse_vs_replay(api, users, bot_ids)
        test_concurrent_matches(api, users, bot_ids, n=4)
        test_leaderboard_and_contest(api, users, bot_ids, run_id)
    finally:
        api.client.close()

    print("\n" + "=" * 60)
    print(f"结果：{PASS} 通过 / {FAIL} 失败")
    if FAILS:
        print("失败项：")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("✅ ALL CONFIGURED API CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
