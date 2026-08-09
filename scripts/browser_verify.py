#!/usr/bin/env python3
"""
前端全量功能验收（PR-F1~F7 终态）。
用 headless chromium 逐路由访问，断言关键元素渲染 + 明暗双主题 + 移动端。
"""
import sys, json, urllib.request
from playwright.sync_api import sync_playwright
from _qa_target import assert_qa_instance, qa_base

BASE = qa_base()
assert_qa_instance(BASE)

passed = 0
failed = 0
fails = []

def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"  \033[32m✓\033[0m {name}")
    else:
        failed += 1
        fails.append(name)
        print(f"  \033[31m✗\033[0m {name} {detail}")

def has_text(page, text):
    return text in page.eval_on_selector("body", "el => el.innerText")

def main():
    try:
        with urllib.request.urlopen(f"{BASE}/api/matches?limit=10") as r:
            md = json.load(r)
        done = [m for m in md.get("matches", []) if m["status"] == "completed"]
        match_id = done[0]["id"] if done else "0"
        bot_id = str(done[0].get("bot_a_id", 1)) if done else "1"
        with urllib.request.urlopen(f"{BASE}/api/leaderboard?game_id=holdem") as r:
            lb = json.load(r).get("leaderboard", [])
        username = lb[0]["owner_name"] if lb and lb[0].get("owner_name") else "load_u0"
    except Exception:
        match_id, bot_id, username = "0", "1", "load_u0"

    routes = [
        ("home", "/"), ("leaderboard", "/leaderboard"), ("botdetail", f"/bot/{bot_id}"),
        ("userprofile", f"/user/{username}"), ("search", "/search?q=load"),
        ("notifications", "/notifications"), ("settings", "/settings"),
        ("contests", "/contests"), ("replay", f"/match/{match_id}"), ("login", "/login"),
        ("register", "/register"), ("challenge", "/challenge"), ("mybots", "/my-bots"),
        ("wiki", "/wiki"), ("history", "/history"), ("resetpw", "/reset-password"),
    ]

    with sync_playwright() as p:
        b = p.chromium.launch(headless=True)

        print(f"=== 明色桌面端（{len(routes)} 路由）===")
        for name, path in routes:
            try:
                pg = b.new_page(viewport={"width": 1280, "height": 800})
                pg.goto(f"{BASE}/#{path}", wait_until="networkidle", timeout=15000)
                pg.wait_for_timeout(1000)
                txt_len = pg.eval_on_selector("body", r"el => el.innerText.replace(/\s/g,'').length")
                has_nav = pg.eval_on_selector_all("header a, header button", "els => els.length") > 3
                check(f"light-{name}", txt_len > 30 and has_nav, f"text={txt_len}")
                pg.close()
            except Exception as e:
                check(f"light-{name}", False, str(e)[:60])

        print("=== 暗色模式 ===")
        for name, path in [("home", "/"), ("leaderboard", "/leaderboard"), ("botdetail", f"/bot/{bot_id}"), ("login", "/login")]:
            try:
                pg = b.new_page(viewport={"width": 1280, "height": 800})
                pg.goto(f"{BASE}/#{path}", wait_until="networkidle", timeout=15000)
                pg.wait_for_timeout(900)
                pg.eval_on_selector("html", "el => el.classList.add('dark')")
                pg.wait_for_timeout(400)
                bg = pg.eval_on_selector("body", "el => getComputedStyle(el).backgroundColor")
                is_dark = bg.startswith("oklch(0.") and float(bg.split("(")[1].split()[0]) < 0.3
                check(f"dark-{name}", is_dark, f"bg={bg}")
                pg.close()
            except Exception as e:
                check(f"dark-{name}", False, str(e)[:60])

        print("=== 移动端（375px）===")
        for name, path in [("home", "/"), ("leaderboard", "/leaderboard"), ("login", "/login")]:
            try:
                pg = b.new_page(viewport={"width": 375, "height": 700})
                pg.goto(f"{BASE}/#{path}", wait_until="networkidle", timeout=15000)
                pg.wait_for_timeout(900)
                menu_btn = pg.eval_on_selector_all('button[aria-label="菜单"]', "els => els.length")
                txt_len = pg.eval_on_selector("body", r"el => el.innerText.replace(/\s/g,'').length")
                check(f"mobile-{name}", txt_len > 30, f"text={txt_len} menu={menu_btn}")
                pg.close()
            except Exception as e:
                check(f"mobile-{name}", False, str(e)[:60])

        print("=== 关键功能 ===")
        pg = b.new_page(viewport={"width": 1280, "height": 800})
        pg.goto(f"{BASE}/#/leaderboard", wait_until="networkidle", timeout=15000)
        pg.wait_for_timeout(1200)
        check("排行榜段位徽章", has_text(pg, "新手") or has_text(pg, "进阶") or has_text(pg, "熟练") or has_text(pg, "高手"))
        check("排行榜表格列", pg.eval_on_selector_all("table thead th", "els => els.length") >= 4)

        pg.goto(f"{BASE}/#/login", wait_until="networkidle", timeout=15000)
        pg.wait_for_timeout(1000)
        check("登录页无🔔emoji", pg.eval_on_selector_all("span", 'els => els.filter(e => e.innerText.includes("🔔")).length') == 0)
        check("登录页验证码组件", pg.eval_on_selector_all('button[title="点击刷新"]', "els => els.length") >= 1)

        pg.goto(f"{BASE}/#/leaderboard", wait_until="networkidle", timeout=15000)
        pg.wait_for_timeout(900)
        toggle = pg.eval_on_selector_all('button[aria-label*="主题"], button[aria-label*="深色"], button[aria-label*="浅色"]', "els => els.length")
        check("主题切换按钮存在", toggle >= 1)
        search_btn = pg.eval_on_selector_all('button, [role="button"]', 'els => els.filter(e => /搜索|⌘K/.test(e.innerText)).length')
        check("全局搜索入口", search_btn >= 1)
        pg.close()
        b.close()

    print(f"\n{'='*50}")
    print(f"浏览器功能验收：{passed} passed / {failed} failed")
    if fails:
        print("失败明细：")
        for f in fails:
            print(f"  - {f}")
    sys.exit(1 if failed else 0)

if __name__ == "__main__":
    main()
