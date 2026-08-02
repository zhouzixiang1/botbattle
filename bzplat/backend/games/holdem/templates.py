"""德州扑克赛事模板（per-game，全面解耦 PR5）。

本游戏自带的内置赛事阶段模板。contests/templates.py 的 DEFAULT_TEMPLATES 从
注册表（各 spec.templates）派生，单一真相。
"""
from __future__ import annotations

from typing import Any

# 德州用 3-1-0 计分（胜3/平1/负0）。常量内联，避免 import contests.templates
# 触发 contests 包重初始化（与 games ↔ matches ↔ orchestrator 循环）。
SCORING_POKER = "poker_3_1_0"

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "holdem_swiss_ko",
        "name": "德州：瑞士 → 单败",
        "game_id": "holdem",
        "stages": [
            {
                "key": "swiss",
                "type": "swiss",
                "rounds": 0,  # 0 = 按 log2(n) 自动
                "scoring": SCORING_POKER,
                "advance_count": 8,
                "rest_after_minutes": 10,
                "allow_bot_swap_in_rest": True,
            },
            {
                "key": "ko",
                "type": "single_elimination",
                "scoring": SCORING_POKER,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "holdem_rr",
        "name": "德州：单循环（小规模）",
        "game_id": "holdem",
        "stages": [
            {
                "key": "rr",
                "type": "round_robin",
                "scoring": SCORING_POKER,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
]
