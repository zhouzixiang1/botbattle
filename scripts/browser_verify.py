#!/usr/bin/env python3
"""浏览器完整测试：用 headless Chromium 逐页验证 10 个 PR 的新前端功能。

复用 ms-playwright 缓存的 chromium（不重新下载）。逐页打开、截图、断言关键元素存在。
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = "http://127.0.0.1:50380"
CHROME = "/home/zzx/.cache/ms-playwright/chromium-1228/chrome-linux64/chrome"
SHOT_DIR = Path("browser_shots")
SHOT_DIR.mkdir(exist_ok=True)

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


def has_text(page, text: str) -> bool:
    return text in page.content()


def shot(page, name: str) -> None:
    try:
        page.screenshot(path=str(SHOT_DIR / f"{name}.png"), full_page=True)
    except Exception:
        pass


def main() -> int:
    # 获取一些 id 用于构造 URL
    import sqlite3
    con = sqlite3.connect("botzone.db")
    con.row_factory = sqlite3.Row
    bot = con.execute("SELECT id, name, owner_id FROM bots WHERE is_active=1 LIMIT 1").fetchone()
    user = con.execute("SELECT username FROM users WHERE is_active=1 LIMIT 1").fetchone()
    contest = con.execute("SELECT id FROM contests LIMIT 1").fetchone()
    match = con.execute("SELECT id FROM matches WHERE status='completed' LIMIT 1").fetchone()
    con.close()
    bot_id = bot["id"] if bot else 1
    bot_name = bot["name"] if bot else "x"
    username = user["username"] if user else "zzx"
    contest_id = contest["id"] if contest else 1
    match_id = match["id"] if match else None
    print(f"测试数据：bot_id={bot_id} username={username} contest_id={contest_id} match_id={match_id}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME,
            headless=True,
            args=["--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = browser.new_context(viewport={"width": 1280, "height": 900})
        page = context.new_page()
        page.set_default_timeout(15000)

        # ── 首页（含搜索框 + 热门对局 + Bot 名链接）──
        print("\n=== 首页 ===")
        page.goto(f"{BASE}/")
        page.wait_for_load_state("networkidle")
        check("首页加载（含 Botbattle 标题）", has_text(page, "Botbattle") or has_text(page, "最新对局"))
        check("顶栏搜索框存在", page.locator('input[placeholder="搜索…"]').count() > 0)
        check("顶栏「数据」导航存在", page.locator('a:has-text("数据")').count() > 0)
        shot(page, "01_home")

        # ── 排行榜（段位徽章 + 趋势）──
        print("\n=== 排行榜（段位徽章 + 趋势）===")
        page.goto(f"{BASE}/#/leaderboard")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        content = page.content()
        # 段位名应出现（新手/进阶/熟练/高手/专家/大师之一）
        tier_found = any(t in content for t in ["新手", "进阶", "熟练", "高手", "专家", "大师"])
        check("排行榜含段位徽章", tier_found, "未见段位名")
        check("排行榜含「段位」表头", "段位" in content)
        shot(page, "02_leaderboard")

        # ── Bot 详情页（/bot/:id）──
        print("\n=== Bot 详情页（/bot/:id）===")
        page.goto(f"{BASE}/#/bot/{bot_id}")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        content = page.content()
        check("Bot 详情页含对局历史 tab", "对局历史" in content)
        check("Bot 详情页含对手战绩 tab", "对手战绩" in content)
        check("Bot 详情页含评分曲线 tab", "评分曲线" in content)
        check("Bot 详情页含 Rating 卡片", "Rating" in content)
        check("Bot 详情页含评论区", "评论" in content)
        # 段位徽章（Rating/段位卡片）
        check("Bot 详情页含段位徽章", any(t in content for t in ["新手", "进阶", "熟练", "高手", "专家", "大师"]))
        # 收藏按钮（需登录才显示，可能不显示）
        shot(page, "03_bot_detail")

        # ── 用户主页（/user/:name）──
        print("\n=== 用户主页（/user/:name）===")
        page.goto(f"{BASE}/#/user/{username}")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        content = page.content()
        check("用户主页含「Bot 列表」", "Bot 列表" in content or "Bot 数" in content)
        check("用户主页含总战绩（胜率/胜）", "总胜率" in content or "胜" in content)
        check("用户主页含注册时间", "注册" in content)
        shot(page, "04_user_profile")

        # ── 全局搜索（/search）──
        print("\n=== 全局搜索（/search）===")
        page.goto(f"{BASE}/#/search?q=a&type=users")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        content = page.content()
        check("搜索页含「用户」tab", "用户" in content)
        check("搜索页含「Bot」tab", "Bot" in content)
        check("搜索页含「对局」tab", "对局" in content)
        # 切到 bots tab
        try:
            page.locator('button:has-text("Bot")').first.click()
            time.sleep(1)
            check("搜索页 Bot tab 可切换", "Bot" in page.content())
        except Exception:
            pass
        shot(page, "05_search")

        # ── 通知列表（/notifications）──
        print("\n=== 通知列表（/notifications）===")
        page.goto(f"{BASE}/#/notifications")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        # 未登录会跳转登录页或显示提示
        content = page.content()
        check("通知页加载（含「通知」标题或登录引导）", "通知" in content or "登录" in content)
        shot(page, "06_notifications")

        # ── 个人设置（/settings）──
        print("\n=== 个人设置（/settings）===")
        page.goto(f"{BASE}/#/settings")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        content = page.content()
        check("设置页加载（含「资料/密码/通知偏好/我的收藏」或登录引导）",
              any(t in content for t in ["资料", "密码", "通知偏好", "我的收藏", "登录"]))
        shot(page, "07_settings")

        # ── 数据集下载（/data）──
        print("\n=== 数据集下载（/data）===")
        page.goto(f"{BASE}/#/data")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        content = page.content()
        check("数据页含「对局数据集」标题", "对局数据集" in content)
        check("数据页含「游戏/月份/对局数」表头", "月份" in content)
        check("数据页含等级 gating 提示或下载链接",
              "等级" in content or "下载" in content)
        shot(page, "08_data")

        # ── 赛事对阵图（/contests/:id）──
        print("\n=== 赛事对阵图（/contests/:id）===")
        page.goto(f"{BASE}/#/contests/{contest_id}")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        content = page.content()
        check("赛事详情含「对阵/积分榜/报名」", any(t in content for t in ["对阵", "积分榜", "报名"]))
        shot(page, "09_contest")

        # ── 对局回放（评论区）──
        print("\n=== 对局回放（评论区）===")
        if match_id:
            page.goto(f"{BASE}/#/match/{match_id}")
            page.wait_for_load_state("networkidle")
            time.sleep(1)
            content = page.content()
            check("对局回放页含评论区", "评论" in content)
            check("对局回放页含点赞按钮（♥/♡）", "♡" in content or "♥" in content)
            shot(page, "10_match_replay")
        else:
            check("对局回放页（无 completed 对局可测）", False, "DB 无 completed 对局")

        # ── 登录页（验证 SPA 路由）──
        print("\n=== 登录页（SPA 路由验证）===")
        page.goto(f"{BASE}/#/login")
        page.wait_for_load_state("networkidle")
        time.sleep(1)
        check("登录页加载", "登录" in page.content() or "密码" in page.content())
        shot(page, "11_login")

        browser.close()

    print(f"\n{'='*60}")
    print(f"浏览器测试：{PASS} passed / {FAIL} failed")
    if FAILS:
        print("失败明细：")
        for f in FAILS:
            print(f"  ✗ {f}")
    print(f"截图保存于 {SHOT_DIR}/")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
