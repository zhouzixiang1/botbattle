#!/usr/bin/env python3
"""模拟真实用户的视觉+功能巡检（增强版）。

相对 ui_full_audit.py：
- 元素级溢出/裁切检测（不只 document overflowX）
- admin 每个 Tab 单独截图
- 设置/挑战等关键交互轻点
- 多视口 desk/tab/mob 覆盖更多页面
- 输出 browser_shots/visual_audit/{REPORT.md,results.json,*.png}

用法（50380 须在线）：
    source .venv/bin/activate
    python scripts/ui_visual_audit.py
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:50380"
ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "browser_shots" / "visual_audit"
OUT.mkdir(parents=True, exist_ok=True)
for f in OUT.glob("*.png"):
    f.unlink()
for f in OUT.glob("*.md"):
    f.unlink()
for f in OUT.glob("*.json"):
    f.unlink()

_NOISE = re.compile(
    r"401|Unauthorized|429|Too Many Requests|favicon|Download the React DevTools|"
    r"net::ERR_ABORTED|Failed to load resource",
    re.I,
)


def api_json(path: str):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=15) as r:
        return json.load(r)


def discover_ids():
    match_id, bot_id, username, contest_id = "0", "1", "tester1", "0"
    try:
        md = api_json("/api/matches?limit=30")
        matches = md.get("matches") or []
        done = [m for m in matches if m.get("status") == "completed"]
        any_m = done or matches
        if any_m:
            match_id = str(any_m[0]["id"])
            bot_id = str(any_m[0].get("bot_a_id") or bot_id)
        lb = api_json("/api/leaderboard?game_id=holdem").get("leaderboard") or []
        if lb and lb[0].get("owner_name"):
            username = lb[0]["owner_name"]
            bot_id = str(lb[0].get("bot_id") or bot_id)
        cs = api_json("/api/contests").get("contests") or []
        # prefer open/running for functional surface
        prefer = [c for c in cs if c.get("status") in ("open", "running", "rest", "draft")]
        if prefer:
            contest_id = str(prefer[0]["id"])
        elif cs:
            contest_id = str(cs[0]["id"])
    except Exception as e:
        print(f"  discover_ids: {e}", file=sys.stderr)
    return match_id, bot_id, username, contest_id


def login_in_context(page, username: str, password: str) -> bool:
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
            page.goto(f"{BASE}/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(500)
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
                failed_reqs.append(f"{r.status} {r.url[:140]}")
            # also track unexpected 404 on our assets
            if r.status == 404 and ("/assets/" in r.url or r.url.endswith(".js")):
                failed_reqs.append(f"404 {r.url[:140]}")
        except Exception:
            pass

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("response", on_response)
    return console_errs, page_errs, failed_reqs


ANALYZE_JS = """() => {
  const body = document.body;
  const text = (body.innerText || '').replace(/\\s+/g, ' ').trim();
  const textLen = text.replace(/\\s/g, '').length;
  const hasEmoji = /[\\u{1F300}-\\u{1F9FF}\\u{2600}-\\u{26FF}]/u.test(body.innerText || '');
  const docW = document.documentElement.scrollWidth;
  const clientW = document.documentElement.clientWidth;
  const overflowX = docW > clientW + 8;

  // element-level overflow / clip
  const issues = [];
  const seen = new Set();
  const candidates = document.querySelectorAll(
    'h1,h2,h3,p,span,button,a,td,th,label,li,[class*="truncate"],[class*="badge"],nav,main,aside,table'
  );
  let checked = 0;
  for (const el of candidates) {
    if (checked > 400) break;
    checked++;
    try {
      const style = getComputedStyle(el);
      if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
      const r = el.getBoundingClientRect();
      if (r.width < 2 || r.height < 2) continue;
      // off-screen far left/right relative to viewport
      if (r.right > clientW + 4 && r.left < clientW) {
        const key = 'rclip:' + (el.tagName + (el.className||'').toString().slice(0,40));
        if (!seen.has(key)) {
          seen.add(key);
          const t = (el.innerText||'').trim().slice(0,40);
          if (t) issues.push({kind:'viewport-clip', tag: el.tagName, text: t, right: Math.round(r.right), vw: clientW});
        }
      }
      // scrollWidth > clientWidth on text containers
      if (el.scrollWidth > el.clientWidth + 4 && style.overflowX !== 'hidden' && style.overflow !== 'hidden') {
        // ignore intentionally scrollable
        if (style.overflowX === 'auto' || style.overflowX === 'scroll') continue;
        const key = 'ow:' + (el.tagName + (el.innerText||'').slice(0,20));
        if (!seen.has(key) && issues.length < 12) {
          seen.add(key);
          issues.push({kind:'elem-overflow', tag: el.tagName, text: (el.innerText||'').trim().slice(0,50),
            sw: el.scrollWidth, cw: el.clientWidth});
        }
      }
    } catch (e) {}
  }

  // broken images
  let brokenImgs = 0;
  for (const img of document.querySelectorAll('img')) {
    if (img.complete && img.naturalWidth === 0 && img.src) brokenImgs++;
  }

  // native select (policy ban)
  const nativeSelects = document.querySelectorAll('select').length;

  // raw english statuses common leak
  const rawStatus = /\\b(completed|aborted|pending|running|challenge|ladder|contest)\\b/.test(text);

  return {
    textLen,
    textSample: text.slice(0, 220),
    hasEmoji,
    overflowX,
    docW, clientW,
    elemIssues: issues.slice(0, 10),
    brokenImgs,
    nativeSelects,
    rawStatus,
    buttons: document.querySelectorAll('button').length,
    tables: document.querySelectorAll('table').length,
    title: document.title || '',
    url: location.href,
  };
}"""


def analyze_page(page) -> dict:
    return page.evaluate(ANALYZE_JS)


def shot(page, name: str) -> str:
    fname = f"{name}.png"
    page.screenshot(path=str(OUT / fname), full_page=False)
    return fname


def main() -> int:
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

    results: list[dict] = []
    issues: list[dict] = []
    n = 0

    def add(sev, page_name, kind, desc, shot_name=""):
        nonlocal n
        n += 1
        issues.append(
            {
                "id": f"V{n}",
                "severity": sev,
                "page": page_name,
                "kind": kind,
                "desc": desc,
                "shot": shot_name,
                "status": "open",
            }
        )

    # pages to visit: (label, path, role)
    public_pages = [
        ("home", "/"),
        ("leaderboard", "/leaderboard"),
        ("wiki", "/wiki"),
        ("search", "/search?q=test"),
        ("data", "/data"),
        ("contests", "/contests"),
        ("contest_detail", f"/contests/{contest_id}"),
        ("match", f"/match/{match_id}"),
        ("bot", f"/bot/{bot_id}"),
        ("user", f"/user/{username}"),
        ("arena", "/arena"),
        ("login", "/login"),
        ("register", "/register"),
        ("verify_email", "/verify-email"),
        ("reset_password", "/reset-password"),
    ]
    user_pages = [
        ("challenge", "/challenge"),
        ("my_bots", "/my-bots"),
        ("history", "/history"),
        ("notifications", "/notifications"),
        ("settings", "/settings"),
        ("home_user", "/"),
        ("leaderboard_user", "/leaderboard"),
        ("contests_user", "/contests"),
        ("match_user", f"/match/{match_id}"),
        ("bot_user", f"/bot/{bot_id}"),
    ]
    admin_pages = [
        ("admin", "/admin"),
    ]

    viewports = {
        "desk": (1440, 900),
        "tab": (768, 900),
        "mob": (390, 844),
    }

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        def visit(label: str, path: str, role: str, vw: str, creds=None, after=None):
            w, h = viewports[vw]
            ctx = browser.new_context(viewport={"width": w, "height": h})
            page = ctx.new_page()
            cons, perrs, fails = attach_collectors(page)
            status = "OK"
            detail = {}
            fname = ""
            try:
                if creds:
                    if not login_in_context(page, creds[0], creds[1]):
                        add("P0", f"{label}@{role}/{vw}", "功能", f"登录失败 {creds[0]}")
                        results.append({"label": label, "role": role, "vw": vw, "status": "LOGIN_FAIL"})
                        ctx.close()
                        return
                url = f"{BASE}/#{path}"
                page.goto(url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(900)
                if after:
                    after(page)
                    page.wait_for_timeout(500)
                detail = analyze_page(page)
                fname = shot(page, f"{role}_{label}_{vw}")
                # issues
                if perrs:
                    add("P0", f"{label}@{role}/{vw}", "崩溃", "; ".join(perrs[:3]), fname)
                    status = "PAGEERROR"
                if cons:
                    add("P1", f"{label}@{role}/{vw}", "console", "; ".join(cons[:3]), fname)
                    status = "CONSOLE" if status == "OK" else status
                if fails:
                    add("P1", f"{label}@{role}/{vw}", "网络", "; ".join(fails[:3]), fname)
                if detail.get("textLen", 0) < 15:
                    add("P0", f"{label}@{role}/{vw}", "显示", f"疑似白屏 textLen={detail.get('textLen')}", fname)
                    status = "BLANK"
                if detail.get("overflowX"):
                    add("P2", f"{label}@{role}/{vw}", "布局", f"页面横向溢出 docW={detail.get('docW')} > clientW={detail.get('clientW')}", fname)
                for ei in detail.get("elemIssues") or []:
                    add(
                        "P2",
                        f"{label}@{role}/{vw}",
                        "布局",
                        f"{ei.get('kind')}: <{ei.get('tag')}> «{ei.get('text')}»",
                        fname,
                    )
                if detail.get("brokenImgs"):
                    add("P1", f"{label}@{role}/{vw}", "显示", f"破损图片 {detail['brokenImgs']} 张", fname)
                if detail.get("nativeSelects"):
                    add("P1", f"{label}@{role}/{vw}", "规范", f"裸 <select> ×{detail['nativeSelects']}", fname)
                if detail.get("hasEmoji"):
                    add("P2", f"{label}@{role}/{vw}", "规范", "页面含 emoji", fname)
                sample = detail.get("textSample") or ""
                if role in ("user", "admin") and "请先登录" in sample:
                    add("P0", f"{label}@{role}/{vw}", "功能", "已登录仍显示请先登录", fname)
                    status = "AUTH"
                # long unwrapped tokens (bot ids / hashes) heuristic
                if re.search(r"[A-Za-z0-9_-]{48,}", sample):
                    add("P3", f"{label}@{role}/{vw}", "显示", "存在超长连续 token（可能撑破布局）", fname)
            except Exception as e:
                status = "ERR"
                add("P0", f"{label}@{role}/{vw}", "崩溃", str(e)[:200], fname)
            results.append(
                {
                    "label": label,
                    "role": role,
                    "vw": vw,
                    "path": path,
                    "status": status,
                    "shot": fname,
                    "textLen": detail.get("textLen"),
                    "sample": (detail.get("textSample") or "")[:120],
                    "overflowX": detail.get("overflowX"),
                    "elemIssues": detail.get("elemIssues"),
                }
            )
            print(f"  [{status:8}] {role}/{label}/{vw}  {path}")
            ctx.close()

        # --- guest desk ---
        print("== guest desk ==")
        for label, path in public_pages:
            visit(label, path, "guest", "desk")

        # --- guest mobile subset ---
        print("== guest mob ==")
        for label, path in [
            ("home", "/"),
            ("leaderboard", "/leaderboard"),
            ("contests", "/contests"),
            ("match", f"/match/{match_id}"),
            ("wiki", "/wiki"),
            ("login", "/login"),
            ("register", "/register"),
        ]:
            visit(label, path, "guest", "mob")

        # --- guest tablet ---
        print("== guest tab ==")
        for label, path in [
            ("home", "/"),
            ("challenge_gate", "/challenge"),
            ("leaderboard", "/leaderboard"),
            ("match", f"/match/{match_id}"),
        ]:
            visit(label, path, "guest", "tab")

        # --- user desk ---
        print("== user desk ==")
        for label, path in user_pages + [
            ("contest_detail", f"/contests/{contest_id}"),
            ("data", "/data"),
            ("arena", "/arena"),
            ("search", "/search?q=bot"),
        ]:
            visit(label, path, "user", "desk", creds=("tester1", "Test1234"))

        # --- user mobile key pages ---
        print("== user mob ==")
        for label, path in [
            ("challenge", "/challenge"),
            ("my_bots", "/my-bots"),
            ("history", "/history"),
            ("settings", "/settings"),
            ("notifications", "/notifications"),
        ]:
            visit(label, path, "user", "mob", creds=("tester1", "Test1234"))

        # --- settings tabs ---
        print("== settings tabs ==")
        def click_settings_tabs(page):
            for name in ("密码", "通知偏好", "我的收藏"):
                try:
                    page.get_by_role("tab", name=name).click(timeout=2000)
                    page.wait_for_timeout(400)
                    shot(page, f"user_settings_tab_{name}")
                except Exception:
                    pass
            try:
                page.get_by_role("tab", name="资料").click(timeout=2000)
            except Exception:
                pass

        visit("settings_tabs", "/settings", "user", "desk", creds=("tester1", "Test1234"), after=click_settings_tabs)

        # --- challenge interaction: open selects ---
        print("== challenge interact ==")
        def challenge_interact(page):
            # try open a few comboboxes / buttons
            for role_name in ("combobox",):
                boxes = page.get_by_role(role_name)
                nbox = boxes.count()
                for i in range(min(nbox, 3)):
                    try:
                        boxes.nth(i).click(timeout=1500)
                        page.wait_for_timeout(300)
                        shot(page, f"user_challenge_open_select_{i}")
                        page.keyboard.press("Escape")
                        page.wait_for_timeout(200)
                    except Exception:
                        pass

        visit("challenge_interact", "/challenge", "user", "desk", creds=("tester1", "Test1234"), after=challenge_interact)

        # --- match replay controls ---
        print("== match interact ==")
        def match_interact(page):
            for name in ("播放", "暂停", "下一步", "上一步", "倍速"):
                try:
                    btn = page.get_by_role("button", name=re.compile(name))
                    if btn.count():
                        btn.first.click(timeout=1500)
                        page.wait_for_timeout(300)
                except Exception:
                    pass
            shot(page, "user_match_after_controls")

        visit("match_interact", f"/match/{match_id}", "user", "desk", creds=("tester1", "Test1234"), after=match_interact)

        # --- admin all tabs ---
        print("== admin tabs ==")
        def admin_all_tabs(page):
            labels = [
                "仪表盘", "用户", "Bot", "对局", "比赛",
                "赛制模板", "运行时", "裁判", "日志", "邮件",
            ]
            for lab in labels:
                try:
                    # tabs are buttons, not role=tab
                    page.get_by_role("button", name=lab, exact=True).click(timeout=2500)
                    page.wait_for_timeout(700)
                    shot(page, f"admin_tab_{lab}")
                    d = analyze_page(page)
                    if d.get("overflowX"):
                        add("P2", f"admin/{lab}", "布局", "横向溢出", f"admin_tab_{lab}.png")
                    if d.get("nativeSelects"):
                        add("P1", f"admin/{lab}", "规范", f"裸 select ×{d['nativeSelects']}", f"admin_tab_{lab}.png")
                    if d.get("textLen", 0) < 20:
                        add("P0", f"admin/{lab}", "显示", "疑似空 Tab", f"admin_tab_{lab}.png")
                    for ei in d.get("elemIssues") or []:
                        add("P2", f"admin/{lab}", "布局", f"{ei.get('kind')}: «{ei.get('text')}»", f"admin_tab_{lab}.png")
                    print(f"    admin tab {lab}: textLen={d.get('textLen')} overflow={d.get('overflowX')}")
                except Exception as e:
                    add("P1", f"admin/{lab}", "功能", f"无法切换: {e}", "admin_admin_desk.png")

        # try auditadmin then fall back to checking if tester is admin
        admin_ok = False
        for admin_user, admin_pass in (("auditadmin", "Test1234"), ("admin", "admin123"), ("tester1", "Test1234")):
            ctx = browser.new_context(viewport={"width": 1440, "height": 900})
            page = ctx.new_page()
            attach_collectors(page)
            if login_in_context(page, admin_user, admin_pass):
                page.goto(f"{BASE}/#/admin", wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(800)
                sample = page.evaluate("() => document.body.innerText || ''")
                if "仅管理员" in sample or "请先" in sample:
                    print(f"  {admin_user} not admin")
                    ctx.close()
                    continue
                print(f"  admin login as {admin_user}")
                shot(page, "admin_admin_desk")
                admin_all_tabs(page)
                admin_ok = True
                # also mobile admin
                ctx.close()
                ctx = browser.new_context(viewport={"width": 390, "height": 844})
                page = ctx.new_page()
                login_in_context(page, admin_user, admin_pass)
                page.goto(f"{BASE}/#/admin", wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(800)
                shot(page, "admin_admin_mob")
                d = analyze_page(page)
                if d.get("overflowX"):
                    add("P2", "admin@mob", "布局", "管理端移动横向溢出", "admin_admin_mob.png")
                ctx.close()
                break
            ctx.close()
        if not admin_ok:
            add("P1", "admin", "功能", "无可用 admin 账号完成管理端巡检")

        browser.close()

    # write report
    ok = sum(1 for r in results if r.get("status") == "OK")
    report = []
    report.append("# Visual UI Audit Report\n")
    report.append(f"- time: {datetime.now(timezone.utc).isoformat()}")
    report.append(f"- base: {BASE}")
    report.append(f"- ids: match={match_id} bot={bot_id} user={username} contest={contest_id}")
    report.append(f"- pages scanned: {len(results)}")
    report.append(f"- OK: {ok}/{len(results)}")
    report.append(f"- issues: {len(issues)}\n")
    report.append("## Issues\n")
    report.append("| ID | 严重度 | 页面 | 类型 | 描述 | 截图 |")
    report.append("|----|--------|------|------|------|------|")
    if not issues:
        report.append("| — | — | — | — | 自动规则未检出 | — |")
    else:
        sev_order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
        for it in sorted(issues, key=lambda x: (sev_order.get(x["severity"], 9), x["id"])):
            report.append(
                f"| {it['id']} | {it['severity']} | {it['page']} | {it['kind']} | {it['desc'].replace('|','/')} | {it['shot']} |"
            )
    report.append("\n## Scan summary\n")
    report.append("| status | count |")
    report.append("|--------|-------|")
    counts: dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    for k, v in sorted(counts.items()):
        report.append(f"| {k} | {v} |")
    report.append("\n## Page samples\n")
    for r in results:
        report.append(f"- **{r['role']}/{r['label']}/{r['vw']}** `{r.get('status')}` textLen={r.get('textLen')} — {(r.get('sample') or '')[:80]}")

    (OUT / "REPORT.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    (OUT / "results.json").write_text(
        json.dumps({"results": results, "issues": issues, "ids": {
            "match_id": match_id, "bot_id": bot_id, "username": username, "contest_id": contest_id,
        }}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\nDone. issues={len(issues)} report={OUT/'REPORT.md'}")
    return 0 if not any(i["severity"] == "P0" for i in issues) else 1


if __name__ == "__main__":
    raise SystemExit(main())
