#!/usr/bin/env python3
"""按真实用户习惯的三身份浏览器巡检（访客 / 普通用户 / 管理员）。

允许写入脏数据：长文本、特殊字符、边界值、错误密码、XSS 风格字符串等。
产物：browser_shots/journey_audit/{*.png, REPORT.md, results.json}

用法：
    source .venv/bin/activate
    python scripts/ui_user_journey_audit.py
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from _execution_request import (
    execution_request_path,
    require_execution_request,
    wait_for_execution_match,
)
from _qa_target import assert_qa_instance, qa_base

BASE = qa_base()
assert_qa_instance(BASE)
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "browser_shots" / "journey_audit"
OUT.mkdir(parents=True, exist_ok=True)
for f in list(OUT.glob("*.png")) + list(OUT.glob("*.md")) + list(OUT.glob("*.json")):
    f.unlink()

USER = ("tester1", "Test1234")
USER2 = ("tester2", "Test1234")
ADMIN = ("auditadmin", "Test1234")

# 脏数据载荷
DIRTY_SHORT = "<script>alert(1)</script>"
DIRTY_LONG = "超长标题" + ("🤡" * 8) + ("A" * 120) + " end"
DIRTY_BIO = "简介脏数据\n" + ("行" * 40) + "\n" + DIRTY_SHORT + "\n" + ("x" * 200)
DIRTY_SEARCH = "'; DROP TABLE users; -- <img src=x onerror=1> " + ("搜" * 30)
DIRTY_COMMENT = "评论脏 " + DIRTY_SHORT + " " + ("字" * 80)

_NOISE = re.compile(
    r"401|Unauthorized|429|favicon|React DevTools|net::ERR_ABORTED|Failed to load resource",
    re.I,
)

results: list[dict] = []
issues: list[dict] = []
_issue_n = 0


def add_issue(sev: str, role: str, step: str, kind: str, desc: str, shot: str = ""):
    global _issue_n
    _issue_n += 1
    issues.append(
        {
            "id": f"J{_issue_n}",
            "severity": sev,
            "role": role,
            "step": step,
            "kind": kind,
            "desc": desc,
            "shot": shot,
        }
    )
    print(f"  !! [{sev}] {role}/{step}: {desc}")


def api(method: str, path: str, body=None, token: str | None = None, cookie: str | None = None):
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if cookie:
        headers["Cookie"] = cookie
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode() or "{}")
        except Exception:
            payload = {"error": str(e)}
        return e.code, payload


def login_api(username: str, password: str) -> str | None:
    _, cap = api("GET", "/api/auth/captcha")
    code, data = api(
        "POST",
        "/api/auth/login",
        {
            "username": username,
            "password": password,
            "captcha_id": cap.get("captcha_id") or "",
            "captcha_answer": str(cap.get("answer") or "0"),
        },
    )
    if code != 200:
        print(f"  login_api fail {username}: {code} {data}")
        return None
    return data.get("token")


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


def attach(page):
    cons, perrs = [], []

    def on_console(m):
        if m.type == "error" and not _NOISE.search(m.text or ""):
            cons.append((m.text or "")[:240])

    def on_pageerror(e):
        perrs.append(str(e)[:240])

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    return cons, perrs


def analyze(page) -> dict:
    return page.evaluate(
        """() => {
      const text = (document.body.innerText || '').replace(/\\s+/g, ' ').trim();
      const clientW = document.documentElement.clientWidth;
      const docW = document.documentElement.scrollWidth;
      const overflowX = docW > clientW + 8;
      const issues = [];
      const els = document.querySelectorAll('h1,h2,h3,p,span,button,a,td,th,label,li,badge');
      let n = 0;
      for (const el of els) {
        if (n++ > 350) break;
        const st = getComputedStyle(el);
        if (st.display === 'none' || st.visibility === 'hidden') continue;
        const r = el.getBoundingClientRect();
        if (r.width < 2 || r.height < 2) continue;
        if (r.right > clientW + 6 && r.left < clientW) {
          const t = (el.innerText || '').trim().slice(0, 48);
          if (t) issues.push({kind:'clip', text:t, right:Math.round(r.right), vw:clientW});
          if (issues.length >= 8) break;
        }
        if (el.scrollWidth > el.clientWidth + 6 && !['auto','scroll','hidden'].includes(st.overflowX)
            && st.overflow !== 'hidden') {
          const t = (el.innerText || '').trim().slice(0, 48);
          if (t) issues.push({kind:'elem-ow', text:t});
          if (issues.length >= 8) break;
        }
      }
      let broken = 0;
      for (const img of document.querySelectorAll('img')) {
        if (img.complete && img.naturalWidth === 0 && img.src) broken++;
      }
      return {
        textLen: text.replace(/\\s/g,'').length,
        sample: text.slice(0, 200),
        overflowX, docW, clientW,
        elemIssues: issues,
        brokenImgs: broken,
        nativeSelects: document.querySelectorAll('select').length,
        hasEmoji: /[\\u{1F300}-\\u{1F9FF}]/u.test(text),
        url: location.href,
      };
    }"""
    )


def shot(page, name: str) -> str:
    fname = f"{name}.png"
    page.screenshot(path=str(OUT / fname), full_page=False)
    return fname


def login_spa(page, username: str, password: str) -> bool:
    """API 登录后注入 cookie bz_session + localStorage（比页面 fetch 更稳）。"""
    token = login_api(username, password)
    if not token:
        return False
    # 需要 user 对象：再调 me 或从 login 取 — login_api 只返 token，这里二次 captcha 登录拿 full
    _, cap = api("GET", "/api/auth/captcha")
    code, data = api(
        "POST",
        "/api/auth/login",
        {
            "username": username,
            "password": password,
            "captcha_id": cap.get("captcha_id") or "",
            "captcha_answer": str(cap.get("answer") or "0"),
        },
    )
    if code != 200 or not data.get("token"):
        return False
    token = data["token"]
    user_obj = data.get("user") or {}
    try:
        page.context.add_cookies(
            [
                {
                    "name": "bz_session",
                    "value": token,
                    "domain": "127.0.0.1",
                    "path": "/",
                    "httpOnly": True,
                    "sameSite": "Lax",
                }
            ]
        )
    except Exception:
        pass
    page.goto(f"{BASE}/#/", wait_until="domcontentloaded", timeout=20000)
    page.evaluate(
        """(payload) => {
          localStorage.setItem('bzplat_token', payload.t);
          localStorage.setItem('bzplat_user', JSON.stringify(payload.u));
        }""",
        {"t": token, "u": user_obj},
    )
    page.reload(wait_until="networkidle", timeout=25000)
    page.wait_for_timeout(500)
    return True


def visit(page, role: str, step: str, path: str, cons, perrs, wait=900) -> tuple[str, dict]:
    perrs.clear()
    cons.clear()
    page.goto(f"{BASE}/#{path}", wait_until="networkidle", timeout=30000)
    page.wait_for_timeout(wait)
    d = analyze(page)
    fname = shot(page, f"{role}_{step}")
    status = "OK"
    if perrs:
        add_issue("P0", role, step, "崩溃", "; ".join(perrs[:2]), fname)
        status = "PAGEERROR"
    if cons:
        add_issue("P1", role, step, "console", "; ".join(cons[:2]), fname)
        status = "CONSOLE" if status == "OK" else status
    if d.get("textLen", 0) < 12:
        add_issue("P0", role, step, "显示", f"疑似白屏 textLen={d.get('textLen')}", fname)
        status = "BLANK"
    if d.get("overflowX"):
        add_issue("P2", role, step, "布局", f"横向溢出 {d.get('docW')}>{d.get('clientW')}", fname)
    for ei in d.get("elemIssues") or []:
        add_issue("P2", role, step, "布局", f"{ei.get('kind')}: «{ei.get('text')}»", fname)
    if d.get("brokenImgs"):
        add_issue("P1", role, step, "显示", f"破损图 {d['brokenImgs']}", fname)
    if d.get("nativeSelects"):
        add_issue("P1", role, step, "规范", f"裸<select>×{d['nativeSelects']}", fname)
    if d.get("hasEmoji") and role != "user_dirty":  # dirty payload 自己带 emoji 不算产品问题
        # 仅记录，不作为 P2 噪声；若是我们注入的脏数据页会标记
        pass
    results.append(
        {
            "role": role,
            "step": step,
            "path": path,
            "status": status,
            "shot": fname,
            "textLen": d.get("textLen"),
            "sample": (d.get("sample") or "")[:140],
            "overflowX": d.get("overflowX"),
        }
    )
    print(f"  [{status:8}] {role}/{step}  {path}")
    return fname, d


def try_click(page, *names, timeout=1500) -> bool:
    for name in names:
        try:
            loc = page.get_by_role("button", name=re.compile(name))
            if loc.count():
                loc.first.click(timeout=timeout)
                return True
        except Exception:
            pass
        try:
            loc = page.get_by_text(name, exact=False)
            if loc.count():
                loc.first.click(timeout=timeout)
                return True
        except Exception:
            pass
    return False


def fill_first(page, selector: str, value: str) -> bool:
    try:
        loc = page.locator(selector).first
        if loc.count():
            loc.fill(value, timeout=2000)
            return True
    except Exception:
        pass
    return False


def main() -> int:
    # health
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=5) as r:
            assert json.load(r).get("ok")
    except Exception as e:
        print("service down", e)
        return 2

    # discover entities
    match_id, bot_id, contest_id, uname = "0", "1", "0", "tester1"
    try:
        ms = json.load(urllib.request.urlopen(f"{BASE}/api/matches?limit=20", timeout=10)).get("matches") or []
        done = [m for m in ms if m.get("status") == "completed"] or ms
        if done:
            match_id = str(done[0]["id"])
            bot_id = str(done[0].get("bot_a_id") or bot_id)
        lb = json.load(urllib.request.urlopen(f"{BASE}/api/leaderboard?game_id=holdem", timeout=10)).get("leaderboard") or []
        if lb:
            uname = lb[0].get("owner_name") or uname
            bot_id = str(lb[0].get("bot_id") or bot_id)
        cs = json.load(urllib.request.urlopen(f"{BASE}/api/contests", timeout=10)).get("contests") or []
        prefer = [c for c in cs if c.get("status") in ("open", "running", "draft", "rest")] or cs
        if prefer:
            contest_id = str(prefer[0]["id"])
    except Exception as e:
        print("discover warn", e)
    print(f"ids match={match_id} bot={bot_id} contest={contest_id} user={uname}")

    # API dirty data pre-seed (user + admin) so UI has messy content
    print("== API dirty seed ==")
    utoken = login_api(*USER)
    atoken = login_api(*ADMIN)
    if not utoken:
        print("FATAL: tester1 login failed")
        return 2
    if not atoken:
        print("FATAL: auditadmin login failed")
        return 2

    # dirty profile
    code, resp = api(
        "PUT",
        "/api/auth/profile",
        {"display_name": "测1_" + DIRTY_SHORT[:20], "bio": DIRTY_BIO[:500]},
        token=utoken,
    )
    print(f"  profile dirty: {code}")
    # dirty comment on match if possible（真实路由 POST /api/comments）
    code, resp = api(
        "POST",
        "/api/comments",
        {"target_type": "match", "target_id": str(match_id), "body": DIRTY_COMMENT[:300]},
        token=utoken,
    )
    print(f"  comment dirty: {code} {str(resp)[:80]}")
    # favorite bot
    api("POST", f"/api/bots/{bot_id}/favorite", {}, token=utoken)
    # follow someone
    try:
        users = json.load(urllib.request.urlopen(f"{BASE}/api/users?q=tester2", timeout=10))
        # flexible shape
        ulist = users.get("users") or users.get("items") or []
        if ulist:
            tid = ulist[0].get("id") or ulist[0].get("username")
            api("POST", f"/api/users/{tid}/follow", {}, token=utoken)
            print(f"  follow: {tid}")
    except Exception as e:
        print("  follow skip", e)

    # admin dirty contest via API
    code, cresp = api(
        "POST",
        "/api/contests",
        {
            "title": DIRTY_LONG[:80],
            "description": DIRTY_BIO[:300],
            "template_id": "holdem_prelim_swiss",
            "game_id": "holdem",
        },
        token=atoken,
    )
    print(f"  admin create contest: {code} {str(cresp)[:120]}")
    dirty_contest_id = None
    if code in (200, 201):
        dirty_contest_id = str((cresp.get("contest") or cresp).get("id") or "")
        if dirty_contest_id:
            contest_id = dirty_contest_id
            # open registration + inject entries if possible
            api("POST", f"/api/contests/{dirty_contest_id}/open", {}, token=atoken)
            # bulk dirty entries from existing bots
            api(
                "POST",
                f"/api/contests/{dirty_contest_id}/entries/bulk",
                {"bot_ids": [int(bot_id)] if str(bot_id).isdigit() else []},
                token=atoken,
            )

    # challenge match（字段 my_bot_id / opponent_bot_id，非 bot_a_id）
    code, ch = api(
        "POST",
        "/api/matches/challenge",
        {"my_bot_id": 787, "opponent_bot_id": 790, "game_id": "holdem"},
        token=utoken,
    )
    print(f"  challenge create: {code} {str(ch)[:120]}")
    live_match = None
    try:
        public_id, live_match = execution_match_id(
            utoken,
            code,
            ch,
            label="用户旅程挑战",
        )
        print(
            f"  challenge admitted: public_id={public_id} match_id={live_match}"
        )
    except Exception as exc:
        print(f"  challenge queue failure: {exc}")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # ═══════════════════════════════════════════
        # ROLE 1: GUEST — 浏览、搜索脏词、注册脏数据、错误登录
        # ═══════════════════════════════════════════
        print("\n== GUEST journey (desk) ==")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        cons, perrs = attach(page)

        visit(page, "guest", "01_home", "/", cons, perrs)
        visit(page, "guest", "02_leaderboard", "/leaderboard", cons, perrs)
        # switch game tabs if present
        try_click(page, "五子棋", "点格棋", "德州")
        page.wait_for_timeout(500)
        shot(page, "guest_02b_leaderboard_game_switch")

        visit(page, "guest", "03_wiki", "/wiki", cons, perrs)
        # wiki slug dirty / known
        visit(page, "guest", "03b_wiki_protocol", "/wiki?slug=protocol", cons, perrs)
        visit(page, "guest", "03c_wiki_contest", "/wiki?slug=guide", cons, perrs)

        visit(page, "guest", "04_search_dirty", f"/search?q={urllib.request.quote(DIRTY_SEARCH[:60])}", cons, perrs)
        visit(page, "guest", "05_contests", "/contests", cons, perrs)
        visit(page, "guest", "06_contest_detail", f"/contests/{contest_id}", cons, perrs)
        visit(page, "guest", "07_match", f"/match/{match_id}", cons, perrs)
        visit(page, "guest", "08_bot", f"/bot/{bot_id}", cons, perrs)
        visit(page, "guest", "09_user", f"/user/{uname}", cons, perrs)
        visit(page, "guest", "11_history", "/history", cons, perrs)

        # gated pages as guest
        visit(page, "guest", "12_challenge_gate", "/challenge", cons, perrs)
        visit(page, "guest", "13_mybots_gate", "/my-bots", cons, perrs)
        visit(page, "guest", "14_admin_gate", "/admin", cons, perrs)

        # login fail with dirty password
        visit(page, "guest", "15_login", "/login", cons, perrs)
        try:
            page.locator('input[type="text"], input[name="username"]').first.fill("not_exist_user_xxx", timeout=2000)
            page.locator('input[type="password"]').first.fill("wrong!!!", timeout=2000)
            try_click(page, "登录")
            page.wait_for_timeout(800)
            shot(page, "guest_15b_login_fail")
            sample = page.evaluate("() => document.body.innerText")
            if "错误" not in sample and "失败" not in sample and "验证" not in sample:
                add_issue("P2", "guest", "15_login", "功能", "错误密码登录后未见明显错误提示", "guest_15b_login_fail.png")
        except Exception as e:
            add_issue("P1", "guest", "15_login", "功能", f"登录表单交互失败: {e}")

        # register form dirty fill (don't necessarily submit if captcha hard)
        visit(page, "guest", "16_register", "/register", cons, perrs)
        try:
            inputs = page.locator("input")
            n = inputs.count()
            dirty_vals = ["脏用户" + DIRTY_SHORT[:12], "bad@@email", "1", "1", "xxx"]
            for i in range(min(n, len(dirty_vals))):
                try:
                    t = inputs.nth(i).get_attribute("type") or "text"
                    if t in ("hidden", "file"):
                        continue
                    inputs.nth(i).fill(dirty_vals[i % len(dirty_vals)], timeout=1000)
                except Exception:
                    pass
            shot(page, "guest_16b_register_dirty_filled")
            try_click(page, "注册")
            page.wait_for_timeout(600)
            shot(page, "guest_16c_register_submit")
        except Exception as e:
            print("  register dirty fill skip", e)

        visit(page, "guest", "17_reset", "/reset-password", cons, perrs)
        visit(page, "guest", "18_verify", "/verify-email", cons, perrs)

        # mobile guest
        print("== GUEST journey (mob) ==")
        ctx.close()
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN", is_mobile=True)
        page = ctx.new_page()
        cons, perrs = attach(page)
        for step, path in [
            ("m01_home", "/"),
            ("m02_leaderboard", "/leaderboard"),
            ("m03_contests", "/contests"),
            ("m04_match", f"/match/{match_id}"),
            ("m05_wiki", "/wiki"),
            ("m06_login", "/login"),
            ("m07_contest_detail", f"/contests/{contest_id}"),
        ]:
            visit(page, "guest", step, path, cons, perrs)
            # open mobile nav if hamburger exists
            try:
                btn = page.locator('button').filter(has=page.locator("svg")).first
                # try menu
                for sel in ['[aria-label*="菜单"]', '[aria-label*="menu"]', 'button:has-text("菜单")']:
                    if page.locator(sel).count():
                        page.locator(sel).first.click(timeout=800)
                        page.wait_for_timeout(300)
                        shot(page, f"guest_{step}_nav_open")
                        page.keyboard.press("Escape")
                        break
            except Exception:
                pass
        ctx.close()

        # ═══════════════════════════════════════════
        # ROLE 2: USER — 登录、挑战、设置脏数据、历史、通知、赛事
        # ═══════════════════════════════════════════
        print("\n== USER journey (desk) ==")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        cons, perrs = attach(page)
        if not login_spa(page, *USER):
            add_issue("P0", "user", "login", "功能", "tester1 SPA 登录失败")
        else:
            visit(page, "user", "01_home", "/", cons, perrs)
            visit(page, "user", "02_challenge", "/challenge", cons, perrs)
            # open selects (shadcn)
            try:
                boxes = page.get_by_role("combobox")
                for i in range(min(boxes.count(), 4)):
                    boxes.nth(i).click(timeout=1500)
                    page.wait_for_timeout(350)
                    shot(page, f"user_02b_select_{i}")
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(200)
            except Exception as e:
                print("  select open", e)
            # try submit challenge if buttons exist
            try_click(page, "发起", "挑战", "开始")
            page.wait_for_timeout(800)
            shot(page, "user_02c_challenge_submit")

            visit(page, "user", "03_mybots", "/my-bots", cons, perrs)
            visit(page, "user", "04_history", "/history", cons, perrs)
            visit(page, "user", "05_notifications", "/notifications", cons, perrs)
            visit(page, "user", "06_settings", "/settings", cons, perrs)
            # dirty fill profile fields
            try:
                tas = page.locator("textarea")
                if tas.count():
                    tas.first.fill(DIRTY_BIO[:400], timeout=2000)
                # display name input
                for sel in ['input[name="display_name"]', 'input']:
                    loc = page.locator(sel)
                    if loc.count():
                        # skip password fields
                        for i in range(min(loc.count(), 3)):
                            t = loc.nth(i).get_attribute("type") or "text"
                            if t in ("password", "file", "hidden", "checkbox"):
                                continue
                            loc.nth(i).fill("脏名_" + DIRTY_SHORT[:15], timeout=1000)
                            break
                shot(page, "user_06b_settings_dirty")
                try_click(page, "保存")
                page.wait_for_timeout(700)
                shot(page, "user_06c_settings_saved")
            except Exception as e:
                print("  settings dirty", e)

            # settings tabs
            for tab in ("密码", "通知偏好", "我的收藏"):
                try:
                    page.get_by_role("tab", name=tab).click(timeout=2000)
                    page.wait_for_timeout(400)
                    shot(page, f"user_06d_tab_{tab}")
                    d = analyze(page)
                    if d.get("overflowX"):
                        add_issue("P2", "user", f"settings_{tab}", "布局", "横向溢出", f"user_06d_tab_{tab}.png")
                except Exception as e:
                    add_issue("P2", "user", f"settings_{tab}", "功能", f"Tab 不可点: {e}")

            visit(page, "user", "07_contests", "/contests", cons, perrs)
            visit(page, "user", "08_contest_detail", f"/contests/{contest_id}", cons, perrs)
            # try register button
            try_click(page, "报名", "注册")
            page.wait_for_timeout(600)
            shot(page, "user_08b_contest_register")

            visit(page, "user", "09_match", f"/match/{match_id}", cons, perrs)
            # replay controls
            for name in ("播放", "暂停", "下一步", "上一步"):
                try_click(page, name)
                page.wait_for_timeout(200)
            shot(page, "user_09b_match_controls")
            # try comment box dirty
            try:
                tas = page.locator("textarea")
                if tas.count():
                    tas.first.fill(DIRTY_COMMENT[:200], timeout=1500)
                    try_click(page, "发送", "评论", "提交")
                    page.wait_for_timeout(600)
                    shot(page, "user_09c_comment_dirty")
            except Exception as e:
                print("  comment ui", e)

            visit(page, "user", "10_bot", f"/bot/{bot_id}", cons, perrs)
            try_click(page, "收藏", "取消收藏")
            page.wait_for_timeout(400)
            shot(page, "user_10b_bot_fav")
            # bot tabs
            for tab in ("对局历史", "对手战绩", "评分曲线"):
                try:
                    page.get_by_role("tab", name=re.compile(tab)).click(timeout=2000)
                    page.wait_for_timeout(500)
                    shot(page, f"user_10c_{tab}")
                except Exception:
                    pass

            visit(page, "user", "11_user_profile", f"/user/{USER[0]}", cons, perrs)
            visit(page, "user", "12_search", "/search?q=tester", cons, perrs)
            visit(page, "user", "14_leaderboard", "/leaderboard", cons, perrs)
            if live_match:
                visit(page, "user", "16_live_match", f"/match/{live_match}", cons, perrs)

            # second user light pass (different data owner)
            page.evaluate("() => { localStorage.clear(); }")
            if login_spa(page, *USER2):
                visit(page, "user2", "01_home", "/", cons, perrs)
                visit(page, "user2", "02_mybots", "/my-bots", cons, perrs)
                visit(page, "user2", "03_challenge", "/challenge", cons, perrs)
                visit(page, "user2", "04_history", "/history", cons, perrs)

        ctx.close()

        # user mobile
        print("== USER journey (mob) ==")
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN", is_mobile=True)
        page = ctx.new_page()
        cons, perrs = attach(page)
        if login_spa(page, *USER):
            for step, path in [
                ("m01_home", "/"),
                ("m02_challenge", "/challenge"),
                ("m03_mybots", "/my-bots"),
                ("m04_history", "/history"),
                ("m05_settings", "/settings"),
                ("m06_notifications", "/notifications"),
                ("m07_match", f"/match/{match_id}"),
                ("m08_contest", f"/contests/{contest_id}"),
            ]:
                visit(page, "user", step, path, cons, perrs)
        ctx.close()

        # ═══════════════════════════════════════════
        # ROLE 3: ADMIN — 7 个只保留业务管理的 Tab
        # ═══════════════════════════════════════════
        print("\n== ADMIN journey (desk) ==")
        ctx = browser.new_context(viewport={"width": 1440, "height": 900}, locale="zh-CN")
        page = ctx.new_page()
        cons, perrs = attach(page)
        if not login_spa(page, *ADMIN):
            add_issue("P0", "admin", "login", "功能", "auditadmin SPA 登录失败")
        else:
            visit(page, "admin", "01_admin_home", "/admin", cons, perrs)
            sample = page.evaluate("() => document.body.innerText || ''")
            if "仅管理员" in sample:
                add_issue("P0", "admin", "01_admin_home", "功能", "admin 账号无法进入管理端", "admin_01_admin_home.png")
            else:
                tabs = ["仪表盘", "用户", "Bot", "对局记录", "锦标赛", "日志", "邮件"]
                for tab in tabs:
                    try:
                        page.get_by_role("button", name=tab, exact=True).click(timeout=2500)
                        page.wait_for_timeout(700)
                        fname = shot(page, f"admin_tab_{tab}")
                        d = analyze(page)
                        status = "OK"
                        if d.get("textLen", 0) < 15:
                            add_issue("P0", "admin", tab, "显示", "空 Tab", fname)
                            status = "BLANK"
                        if d.get("overflowX"):
                            add_issue("P2", "admin", tab, "布局", "横向溢出", fname)
                        for ei in d.get("elemIssues") or []:
                            add_issue("P2", "admin", tab, "布局", f"{ei.get('kind')}: «{ei.get('text')}»", fname)
                        if d.get("nativeSelects"):
                            add_issue("P1", "admin", tab, "规范", f"裸<select>×{d['nativeSelects']}", fname)
                        results.append(
                            {
                                "role": "admin",
                                "step": f"tab_{tab}",
                                "path": "/admin",
                                "status": status,
                                "shot": fname,
                                "textLen": d.get("textLen"),
                                "sample": (d.get("sample") or "")[:140],
                            }
                        )
                        print(f"  [{status:8}] admin/tab/{tab} textLen={d.get('textLen')}")
                    except Exception as e:
                        add_issue("P1", "admin", tab, "功能", f"切换失败: {e}")

                # 用户 Tab：搜索脏词
                try:
                    page.get_by_role("button", name="用户", exact=True).click(timeout=2000)
                    page.wait_for_timeout(400)
                    inp = page.locator("input").first
                    if inp.count():
                        inp.fill(DIRTY_SEARCH[:40], timeout=1500)
                        page.wait_for_timeout(500)
                        shot(page, "admin_users_dirty_search")
                except Exception as e:
                    print("  admin users dirty", e)

                # 日志三文件
                try:
                    page.get_by_role("button", name="日志", exact=True).click(timeout=2000)
                    page.wait_for_timeout(600)
                    shot(page, "admin_logs_default")
                    for lab in ("access", "audit", "app", "访问", "审计", "应用"):
                        try_click(page, lab)
                        page.wait_for_timeout(400)
                    shot(page, "admin_logs_switched")
                except Exception as e:
                    print("  admin logs", e)

                # 邮件
                try:
                    page.get_by_role("button", name="邮件", exact=True).click(timeout=2000)
                    page.wait_for_timeout(600)
                    shot(page, "admin_email")
                except Exception:
                    pass

                # 锦标赛管理
                try:
                    page.get_by_role("button", name="锦标赛", exact=True).click(timeout=2000)
                    page.wait_for_timeout(600)
                    shot(page, "admin_contests_tab")
                except Exception:
                    pass

            # admin also views front pages with admin session
            visit(page, "admin", "02_home_front", "/", cons, perrs)
            visit(page, "admin", "03_contest_front", f"/contests/{contest_id}", cons, perrs)
            visit(page, "admin", "04_match_front", f"/match/{match_id}", cons, perrs)

        # admin mobile
        print("== ADMIN journey (mob) ==")
        ctx.close()
        ctx = browser.new_context(viewport={"width": 390, "height": 844}, locale="zh-CN", is_mobile=True)
        page = ctx.new_page()
        cons, perrs = attach(page)
        if login_spa(page, *ADMIN):
            visit(page, "admin", "m01_admin", "/admin", cons, perrs)
            visit(page, "admin", "m02_contests", "/contests", cons, perrs)
            for tab in ("用户", "对局记录", "日志"):
                try:
                    page.goto(f"{BASE}/#/admin", wait_until="networkidle", timeout=20000)
                    page.wait_for_timeout(400)
                    page.get_by_role("button", name=tab, exact=True).click(timeout=2000)
                    page.wait_for_timeout(500)
                    shot(page, f"admin_m_tab_{tab}")
                    d = analyze(page)
                    if d.get("overflowX"):
                        add_issue("P2", "admin", f"mob_{tab}", "布局", "移动端横向溢出", f"admin_m_tab_{tab}.png")
                except Exception as e:
                    print("  admin mob tab", tab, e)
        ctx.close()
        browser.close()

    # write report
    ok = sum(1 for r in results if r.get("status") == "OK")
    lines = [
        "# User Journey Audit Report",
        "",
        f"- time: {datetime.now(timezone.utc).isoformat()}",
        f"- base: {BASE}",
        f"- identities: guest / tester1(user) / tester2(user) / auditadmin(admin)",
        f"- dirty data: profile/bio/comment/contest/search/register 已注入",
        f"- ids: match={match_id} bot={bot_id} contest={contest_id} live_match={live_match}",
        f"- steps: {len(results)}  OK: {ok}/{len(results)}  issues: {len(issues)}",
        "",
        "## Issues",
        "",
        "| ID | 严重度 | 身份 | 步骤 | 类型 | 描述 | 截图 |",
        "|----|--------|------|------|------|------|------|",
    ]
    if not issues:
        lines.append("| — | — | — | — | — | 自动规则未检出（仍需看图） | — |")
    else:
        order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        for it in sorted(issues, key=lambda x: (order.get(x["severity"], 9), x["id"])):
            lines.append(
                f"| {it['id']} | {it['severity']} | {it['role']} | {it['step']} | {it['kind']} | "
                f"{it['desc'].replace('|','/')} | {it['shot']} |"
            )
    lines += ["", "## Steps", ""]
    for r in results:
        lines.append(
            f"- **{r['role']}/{r['step']}** `{r.get('status')}` textLen={r.get('textLen')} — {(r.get('sample') or '')[:90]}"
        )
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (OUT / "results.json").write_text(
        json.dumps({"results": results, "issues": issues}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nDone issues={len(issues)} → {OUT/'REPORT.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
