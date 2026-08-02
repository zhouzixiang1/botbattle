#!/usr/bin/env python3
"""逐页截图渲染验证（全面解耦后零回归检查）。

用 Playwright 驱动 headless Chromium（合适的浏览器后端）：
- 登录 tester1（seed 账号）后访问关键页 + 未登录访问公开页
- 多宽度：桌面 1280 + 移动 390（验证侧边栏/响应式）
- 收集 console 错误 + page 错误（JS 崩溃即回归信号）
- 截图存临时目录，最后汇总报告

用法：python scripts/screenshot_verify.py
"""
import sys, json
from pathlib import Path

BASE = "http://127.0.0.1:50380"
SHOT_DIR = Path("/tmp/bz_shots")
SHOT_DIR.mkdir(exist_ok=True)
for f in SHOT_DIR.glob("*.png"):
    f.unlink()  # 旧截图清理，不留冗余

from playwright.sync_api import sync_playwright

# (路由, 宽度, 登录态, 说明)
PAGES = [
    ("", 1280, False, "首页"),
    ("/leaderboard", 1280, False, "排行榜"),
    ("/wiki", 1280, False, "Wiki"),
    ("/search", 1280, False, "全局搜索"),
    ("/", 1280, True, "首页_登录"),
    ("/leaderboard", 1280, True, "排行榜_登录"),
    ("/challenge", 1280, True, "挑战"),
    ("/contests", 1280, True, "赛事"),
    ("/my-bots", 1280, True, "我的Bot"),
    ("/data", 1280, True, "数据下载"),
    ("/history", 1280, True, "对局历史"),
    ("/notifications", 1280, True, "通知"),
    ("/settings", 1280, True, "设置"),
    ("/admin", 1280, True, "管理端"),
    ("/", 390, True, "首页_移动"),
    ("/leaderboard", 390, True, "排行榜_移动"),
    ("/challenge", 390, True, "挑战_移动"),
    ("/contests", 390, True, "赛事_移动"),
]


def login_in_page(page):
    """在页面同源上下文里登录（fetch 设 cookie，与页面共享）。返回是否成功。"""
    page.goto(f"{BASE}/#/login", wait_until="networkidle", timeout=15000)
    try:
        ok = page.evaluate("""async () => {
            const cap = await (await fetch('/api/auth/captcha')).json();
            const r = await fetch('/api/auth/login', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    username: 'tester1', password: 'Test1234',
                    captcha_id: cap.captcha_id || '',
                    captcha_answer: cap.answer || '0',
                }),
            });
            return r.ok;
        }""")
        return bool(ok)
    except Exception:
        return False


results = []
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)

    for route, width, need_login, desc in PAGES:
        ctx = browser.new_context(viewport={"width": width, "height": 900})
        page = ctx.new_page()
        logged_in = False
        if need_login:
            logged_in = login_in_page(page)
        console_errs = []

        def _on_console(m):
            if m.type != "error":
                return
            t = m.text
            if "401" in t or "Unauthorized" in t or "favicon" in t.lower() or "429" in t or "Too Many Requests" in t:
                return
            console_errs.append(t)

        def _on_pageerror(e):
            console_errs.append(f"PAGEERROR: {e}")

        page.on("console", _on_console)
        page.on("pageerror", _on_pageerror)
        url = f"{BASE}/#{route}"
        try:
            page.goto(url, wait_until="networkidle", timeout=15000)
            page.wait_for_timeout(1000)  # 让 lazy chunk + 数据加载
            fname = f"{desc}_{width}.png"
            page.screenshot(path=str(SHOT_DIR / fname), full_page=False)
            status = "OK" + ("" if logged_in or not need_login else " [未登录!]")
        except Exception as e:
            status = f"ERR: {str(e)[:60]}"
        ctx.close()
        results.append({"desc": desc, "route": route or "/", "width": width, "login": need_login, "logged": logged_in, "status": status, "console_errs": len(console_errs), "errs": console_errs[:3]})
        print(f"  [{status}] {desc} ({route or '/'}, {width}px)  console_errs={len(console_errs)}")

    browser.close()

print("\n=== 汇总 ===")
ok = sum(1 for r in results if r["status"] == "OK" and r["console_errs"] == 0)
err = len(results) - ok
print(f"成功 {ok}/{len(results)}，有问题 {err}")
if err:
    print("\n问题页：")
    for r in results:
        if r["status"] != "OK" or r["console_errs"] > 0:
            print(f"  - {r['desc']} ({r['route']} {r['width']}): {r['status']} errs={r['errs']}")
print(f"\n截图目录：{SHOT_DIR}（{len(list(SHOT_DIR.glob('*.png')))} 张）")
sys.exit(1 if err else 0)
