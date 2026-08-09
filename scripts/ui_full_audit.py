#!/usr/bin/env python3
"""全站 UI/功能巡检：访客 + 登录用户 + admin，多视口，截图 + console + 轻量交互。

用法（worktree Vite 默认在 127.0.0.1:5173）：
    source .venv/bin/activate
    BZ_E2E_BASE_URL=http://127.0.0.1:5173 python scripts/ui_full_audit.py

产物：
    browser_shots/full_audit/*.png
    browser_shots/full_audit/REPORT.md
    browser_shots/full_audit/results.json
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright
from _qa_target import assert_qa_instance, qa_base

BASE = qa_base()
assert_qa_instance(BASE)
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "browser_shots" / "full_audit"
OUT.mkdir(parents=True, exist_ok=True)
for f in OUT.glob("*.png"):
    f.unlink()

# noise filters for console
_NOISE = re.compile(
    r"401|Unauthorized|429|Too Many Requests|favicon|Download the React DevTools|net::ERR_ABORTED",
    re.I,
)


def api_json(path: str, token: str | None = None):
    req = urllib.request.Request(f"{BASE}{path}")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r)


def login_api(username: str, password: str) -> str | None:
    """Return session token via captcha login."""
    try:
        cap = api_json("/api/auth/captcha")
        body = json.dumps(
            {
                "username": username,
                "password": password,
                "captcha_id": cap.get("captcha_id") or "",
                "captcha_answer": str(cap.get("answer") or "0"),
            }
        ).encode()
        req = urllib.request.Request(
            f"{BASE}/api/auth/login",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.load(r)
        return data.get("token") or data.get("session_token")
    except Exception as e:
        print(f"  login_api {username} fail: {e}", file=sys.stderr)
        return None


def discover_ids():
    match_id, bot_id, username, contest_id = "0", "1", "tester1", "0"
    try:
        md = api_json("/api/matches?limit=20")
        matches = md.get("matches") or []
        done = [m for m in matches if m.get("status") == "completed"]
        any_m = done or matches
        if any_m:
            match_id = str(any_m[0]["id"])
            bot_id = str(any_m[0].get("bot_a_id") or bot_id)
        lb = api_json("/api/leaderboard?game_id=holdem").get("leaderboard") or []
        if lb and lb[0].get("owner_name"):
            username = lb[0]["owner_name"]
        cs = api_json("/api/contests").get("contests") or []
        if cs:
            contest_id = str(cs[0]["id"])
    except Exception as e:
        print(f"  discover_ids: {e}", file=sys.stderr)
    return match_id, bot_id, username, contest_id


def page_routes(match_id, bot_id, username, contest_id):
    """List of (name, path, roles) roles in guest|user|admin."""
    public = [
        ("home", "/"),
        ("leaderboard", "/leaderboard"),
        ("wiki", "/wiki"),
        ("search", "/search?q=test"),
        ("contests", "/contests"),
        ("contest_detail", f"/contests/{contest_id}"),
        ("match", f"/match/{match_id}"),
        ("bot", f"/bot/{bot_id}"),
        ("user", f"/user/{username}"),
        ("history", "/history"),
        ("login", "/login"),
        ("register", "/register"),
        ("verify_email", "/verify-email"),
        ("reset_password", "/reset-password"),
    ]
    user_only = [
        ("challenge", "/challenge"),
        ("my_bots", "/my-bots"),
        ("history", "/history"),
        ("notifications", "/notifications"),
        ("settings", "/settings"),
    ]
    admin_only = [
        ("admin", "/admin"),
    ]
    routes = []
    for name, path in public:
        routes.append((name, path, "guest"))
        routes.append((f"{name}_user", path, "user"))
    for name, path in user_only:
        routes.append((name, path, "user"))
    for name, path in admin_only:
        routes.append((name, path, "admin"))
    return routes


def login_in_context(page, username: str, password: str) -> bool:
    """Login and seed SPA localStorage (token + user) so AuthProvider stays logged in."""
    page.goto(f"{BASE}/#/login", wait_until="domcontentloaded", timeout=20000)
    try:
        ok = page.evaluate(
            """async ({username, password}) => {
            const cap = await (await fetch('/api/auth/captcha')).json();
            const r = await fetch('/api/auth/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                credentials: 'include',
                body: JSON.stringify({
                    username, password,
                    captcha_id: cap.captcha_id || '',
                    captcha_answer: cap.answer || '0',
                }),
            });
            if (!r.ok) return false;
            const d = await r.json();
            if (d.token) localStorage.setItem('bzplat_token', d.token);
            if (d.user) localStorage.setItem('bzplat_user', JSON.stringify(d.user));
            return true;
        }""",
            {"username": username, "password": password},
        )
        if ok:
            # Remount SPA so AuthProvider.refresh picks up token/cookie
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(400)
        return bool(ok)
    except Exception:
        return False


def attach_collectors(page):
    console_errs: list[str] = []
    page_errs: list[str] = []
    failed_reqs: list[str] = []

    def on_console(m):
        if m.type != "error":
            return
        t = m.text or ""
        if _NOISE.search(t):
            return
        console_errs.append(t[:300])

    def on_pageerror(e):
        page_errs.append(str(e)[:300])

    def on_response(r):
        try:
            if r.status >= 500 and "/api/" in r.url:
                failed_reqs.append(f"{r.status} {r.url[:120]}")
        except Exception:
            pass

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("response", on_response)
    return console_errs, page_errs, failed_reqs


def analyze_page(page) -> dict:
    """DOM heuristics for display/function issues."""
    return page.evaluate(
        """() => {
        const body = document.body;
        const text = (body.innerText || '').replace(/\\s+/g, ' ').trim();
        const textLen = text.replace(/\\s/g, '').length;
        const hasEmoji = /[\\u{1F300}-\\u{1F9FF}\\u{2600}-\\u{26FF}]/u.test(body.innerText || '');
        const overlays = [...document.querySelectorAll('[role="dialog"], [data-state="open"]')].length;
        const tables = document.querySelectorAll('table').length;
        const buttons = document.querySelectorAll('button').length;
        const inputs = document.querySelectorAll('input,textarea,select').length;
        const emptyHints = /暂无|没有|空|Empty|未找到|加载失败|出错|Error|500|404/.test(text);
        const loadingStuck = textLen < 20 && /加载|Loading|…|\\.\\.\\./.test(text);
        const whiteish = textLen < 15;
        // horizontal overflow
        const docW = document.documentElement.scrollWidth;
        const clientW = document.documentElement.clientWidth;
        const overflowX = docW > clientW + 8;
        // main content area
        const main = document.querySelector('main') || body;
        const mainRect = main.getBoundingClientRect();
        return {
            textLen,
            textSample: text.slice(0, 180),
            hasEmoji,
            overlays,
            tables,
            buttons,
            inputs,
            emptyHints,
            loadingStuck,
            whiteish,
            overflowX,
            mainH: Math.round(mainRect.height),
            title: document.title || '',
        };
    }"""
    )


VIEWPORTS = [
    ("desk", 1440, 900),
    ("tab", 768, 900),
    ("mob", 390, 800),
]


def main() -> int:
    # health
    try:
        with urllib.request.urlopen(f"{BASE}/api/health", timeout=5) as r:
            health = json.load(r)
        if not health.get("ok"):
            print("service health not ok", health)
            return 2
    except Exception as e:
        print(f"service down: {e}")
        return 2

    match_id, bot_id, username, contest_id = discover_ids()
    print(f"ids match={match_id} bot={bot_id} user={username} contest={contest_id}")

    routes = page_routes(match_id, bot_id, username, contest_id)
    results: list[dict] = []
    issues: list[dict] = []
    issue_n = 0

    def add_issue(sev, page_name, kind, desc, shot=""):
        nonlocal issue_n
        issue_n += 1
        issues.append(
            {
                "id": f"A{issue_n}",
                "severity": sev,
                "page": page_name,
                "kind": kind,
                "desc": desc,
                "shot": shot,
                "status": "open",
            }
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        # Pre-login contexts
        sessions = {
            "guest": None,
            "user": ("tester1", "Test1234"),
            "admin": ("auditadmin", "Test1234"),
        }

        # Desktop full matrix (primary)
        for role, creds in sessions.items():
            role_routes = [r for r in routes if r[2] == role]
            # also guest routes for user already covered as _user suffix
            for name, path, _role in role_routes:
                vw_name, w, h = VIEWPORTS[0]
                ctx = browser.new_context(viewport={"width": w, "height": h})
                page = ctx.new_page()
                console_errs, page_errs, failed_reqs = attach_collectors(page)
                logged = True
                if creds:
                    logged = login_in_context(page, creds[0], creds[1])
                    if not logged:
                        add_issue("P0", f"{name}@{role}", "功能", f"登录失败 {creds[0]}")
                url = f"{BASE}/#{path}"
                status = "OK"
                detail = {}
                try:
                    page.goto(url, wait_until="networkidle", timeout=25000)
                    page.wait_for_timeout(1200)
                    detail = analyze_page(page)
                    fname = f"{role}_{name}_{vw_name}.png"
                    page.screenshot(path=str(OUT / fname), full_page=False)
                    # heuristics
                    if page_errs:
                        add_issue("P0", f"{name}@{role}", "崩溃", "; ".join(page_errs[:3]), fname)
                        status = "PAGEERROR"
                    if console_errs:
                        add_issue("P1", f"{name}@{role}", "console", "; ".join(console_errs[:3]), fname)
                        status = "CONSOLE" if status == "OK" else status
                    if failed_reqs:
                        add_issue("P1", f"{name}@{role}", "API5xx", "; ".join(failed_reqs[:3]), fname)
                    if detail.get("whiteish") or detail.get("loadingStuck"):
                        add_issue("P0", f"{name}@{role}", "显示", f"疑似白屏/卡加载 textLen={detail.get('textLen')}", fname)
                    if detail.get("overflowX"):
                        add_issue("P2", f"{name}@{role}", "显示", "横向溢出 overflowX", fname)
                    if detail.get("hasEmoji"):
                        add_issue("P2", f"{name}@{role}", "显示", "页面文本含 emoji（规范禁 emoji）", fname)
                    # logged-in pages still showing login prompt
                    sample = (detail.get("textSample") or "")
                    if role in ("user", "admin") and "请先登录" in sample:
                        add_issue("P0", f"{name}@{role}", "功能", "已登录仍显示「请先登录」", fname)
                    # raw English match status on home/history style pages
                    if role == "guest" and name in ("home",) and re.search(
                        r"\b(completed|aborted|pending|running)\b", sample
                    ):
                        add_issue(
                            "P2",
                            f"{name}@{role}",
                            "显示",
                            "对局状态显示英文 raw status（应中文化 StatusBadge）",
                            fname,
                        )
                except Exception as e:
                    status = "FAIL"
                    fname = f"{role}_{name}_{vw_name}_ERR.png"
                    try:
                        page.screenshot(path=str(OUT / fname), full_page=False)
                    except Exception:
                        fname = ""
                    add_issue("P0", f"{name}@{role}", "崩溃", str(e)[:200], fname)
                    detail = {"error": str(e)[:200]}

                results.append(
                    {
                        "role": role,
                        "name": name,
                        "path": path,
                        "viewport": vw_name,
                        "status": status,
                        "logged": logged,
                        "console": console_errs[:5],
                        "page_errors": page_errs[:5],
                        "failed_reqs": failed_reqs[:5],
                        "detail": detail,
                    }
                )
                print(f"  [{status}] {role} {name} {path}")
                ctx.close()

        # Responsive sample on key pages (user)
        key_paths = [("/", "home"), ("/leaderboard", "leaderboard"), ("/challenge", "challenge"), ("/admin", "admin")]
        for vw_name, w, h in VIEWPORTS[1:]:
            for path, short in key_paths:
                role = "admin" if short == "admin" else "user"
                creds = sessions[role]
                ctx = browser.new_context(viewport={"width": w, "height": h})
                page = ctx.new_page()
                console_errs, page_errs, failed_reqs = attach_collectors(page)
                login_in_context(page, creds[0], creds[1])
                try:
                    page.goto(f"{BASE}/#{path}", wait_until="networkidle", timeout=25000)
                    page.wait_for_timeout(900)
                    detail = analyze_page(page)
                    fname = f"{role}_{short}_{vw_name}.png"
                    page.screenshot(path=str(OUT / fname), full_page=False)
                    if page_errs:
                        add_issue("P0", f"{short}@{role}/{vw_name}", "崩溃", "; ".join(page_errs[:2]), fname)
                    if detail.get("overflowX"):
                        add_issue("P2", f"{short}@{role}/{vw_name}", "显示", "横向溢出", fname)
                    if detail.get("whiteish"):
                        add_issue("P0", f"{short}@{role}/{vw_name}", "显示", "疑似白屏", fname)
                    # mobile menu
                    if vw_name == "mob":
                        menu = page.locator('button[aria-label="菜单"]')
                        if menu.count() == 0:
                            add_issue("P1", f"{short}@mob", "显示", "移动端未见汉堡菜单按钮", fname)
                    results.append(
                        {
                            "role": role,
                            "name": f"{short}_{vw_name}",
                            "path": path,
                            "viewport": vw_name,
                            "status": "OK" if not page_errs else "PAGEERROR",
                            "detail": detail,
                            "console": console_errs[:3],
                            "page_errors": page_errs[:3],
                        }
                    )
                    print(f"  [resp] {role} {short} {vw_name}")
                except Exception as e:
                    add_issue("P0", f"{short}/{vw_name}", "崩溃", str(e)[:200])
                ctx.close()

        # Functional clicks (user desk)
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})
        page = ctx.new_page()
        console_errs, page_errs, _ = attach_collectors(page)
        if login_in_context(page, "tester1", "Test1234"):
            # theme toggle
            try:
                page.goto(f"{BASE}/#/", wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(600)
                before = page.eval_on_selector("html", "el => el.classList.contains('dark')")
                btn = page.locator('button[aria-label*="主题"], button[aria-label*="深色"], button[aria-label*="浅色"]').first
                if btn.count() == 0:
                    add_issue("P1", "theme", "功能", "未找到主题切换按钮")
                else:
                    btn.click()
                    page.wait_for_timeout(400)
                    after = page.eval_on_selector("html", "el => el.classList.contains('dark')")
                    if before == after:
                        add_issue("P1", "theme", "功能", "主题切换点击后 html.dark 未变化")
                    else:
                        print("  [ok] theme toggle")
                    page.screenshot(path=str(OUT / "func_theme.png"), full_page=False)
            except Exception as e:
                add_issue("P1", "theme", "功能", f"主题切换异常: {e}")

            # challenge form present
            try:
                page.goto(f"{BASE}/#/challenge", wait_until="networkidle", timeout=20000)
                page.wait_for_timeout(800)
                d = analyze_page(page)
                page.screenshot(path=str(OUT / "func_challenge.png"), full_page=False)
                if d.get("buttons", 0) < 2:
                    add_issue("P1", "challenge", "功能", f"挑战页按钮过少 buttons={d.get('buttons')}")
                print("  [ok] challenge page")
            except Exception as e:
                add_issue("P1", "challenge", "功能", str(e)[:200])

            # match playback controls if match exists
            if match_id and match_id != "0":
                try:
                    page.goto(f"{BASE}/#/match/{match_id}", wait_until="networkidle", timeout=25000)
                    page.wait_for_timeout(1200)
                    page.screenshot(path=str(OUT / "func_match.png"), full_page=False)
                    d = analyze_page(page)
                    if d.get("whiteish"):
                        add_issue("P0", "match", "显示", "对局页疑似白屏")
                    if page_errs:
                        add_issue("P0", "match", "崩溃", "; ".join(page_errs[:3]))
                    print("  [ok] match viewer")
                except Exception as e:
                    add_issue("P1", "match", "功能", str(e)[:200])

            # admin tabs as admin
            ctx2 = browser.new_context(viewport={"width": 1440, "height": 900})
            page2 = ctx2.new_page()
            _, page2_errs, _ = attach_collectors(page2)
            if login_in_context(page2, "auditadmin", "Test1234"):
                try:
                    page2.goto(f"{BASE}/#/admin", wait_until="networkidle", timeout=25000)
                    page2.wait_for_timeout(1000)
                    page2.screenshot(path=str(OUT / "func_admin.png"), full_page=False)
                    labels = ["用户", "Bot", "对局", "赛事", "邮件", "运行时", "裁判", "模板", "日志", "仪表"]
                    found = 0
                    for lab in labels:
                        try:
                            loc = page2.locator(f'[role="tab"]:has-text("{lab}")')
                            if loc.count() == 0:
                                loc = page2.get_by_text(lab, exact=True)
                            if loc.count() > 0:
                                loc.first.click(timeout=2000)
                                page2.wait_for_timeout(500)
                                found += 1
                                safe = re.sub(r"\W+", "_", lab)
                                page2.screenshot(path=str(OUT / f"admin_tab_{safe}.png"), full_page=False)
                        except Exception:
                            pass
                    if found < 5:
                        add_issue("P1", "admin", "功能", f"admin Tab 可点击数偏少 found={found}")
                    if page2_errs:
                        add_issue("P0", "admin", "崩溃", "; ".join(page2_errs[:3]))
                    print(f"  [ok] admin tabs clicked={found}")
                except Exception as e:
                    add_issue("P0", "admin", "功能", str(e)[:200])
            else:
                add_issue("P0", "admin", "功能", "admin 登录失败")
            ctx2.close()
        else:
            add_issue("P0", "login", "功能", "tester1 登录失败，功能用例跳过")
        ctx.close()
        browser.close()

    # write reports
    (OUT / "results.json").write_text(json.dumps({"results": results, "issues": issues}, ensure_ascii=False, indent=2), encoding="utf-8")

    lines = [
        f"# UI Full Audit Report",
        f"",
        f"- time: {datetime.now(timezone.utc).isoformat()}",
        f"- base: {BASE}",
        f"- pages scanned: {len(results)}",
        f"- issues: {len(issues)}",
        f"",
        f"## Issues",
        f"",
        f"| ID | 严重度 | 页面 | 类型 | 描述 | 截图 | 状态 |",
        f"|----|--------|------|------|------|------|------|",
    ]
    for i in issues:
        desc = i["desc"].replace("|", "\\|").replace("\n", " ")[:160]
        lines.append(f"| {i['id']} | {i['severity']} | {i['page']} | {i['kind']} | {desc} | {i.get('shot','')} | {i['status']} |")
    if not issues:
        lines.append("| — | — | — | — | 未自动检出问题（仍需人工看图） | — | — |")

    lines += ["", "## Scan summary", ""]
    ok = sum(1 for r in results if r.get("status") == "OK")
    lines.append(f"- OK: {ok}/{len(results)}")
    bad = [r for r in results if r.get("status") != "OK"]
    for r in bad[:40]:
        lines.append(f"- **{r.get('status')}** {r.get('role')} {r.get('name')} {r.get('path')} errs={r.get('page_errors')}")

    report = "\n".join(lines) + "\n"
    (OUT / "REPORT.md").write_text(report, encoding="utf-8")
    (OUT / "ISSUES.md").write_text(report, encoding="utf-8")
    print(f"\nDone. issues={len(issues)} report={OUT/'REPORT.md'}")
    return 0 if not any(i["severity"] == "P0" for i in issues) else 1


if __name__ == "__main__":
    raise SystemExit(main())
