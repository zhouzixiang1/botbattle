"""点格棋赛事模板（per-game，全面解耦 PR5）。"""
from __future__ import annotations

from typing import Any

# 棋类用 2-1-0 计分（胜2/平1/负0）。常量内联，避免 import contests.templates 循环。
SCORING_CCGC = "ccgc_2_1_0"

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "pencil_group_drr_ko",
        "name": "点格棋：分组双循环 → 单败",
        "game_id": "pencil",
        "stages": [
            {
                "key": "group",
                "type": "group_double_round_robin",
                "group_count": 4,
                "advance_per_group": 2,
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 10,
                "allow_bot_swap_in_rest": True,
            },
            {
                "key": "ko",
                "type": "single_elimination",
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "pencil_swiss_ko",
        "name": "点格棋：瑞士 → 单败",
        "game_id": "pencil",
        "stages": [
            {
                "key": "swiss",
                "type": "swiss",
                "rounds": 0,
                "scoring": SCORING_CCGC,
                "advance_count": 8,
                "rest_after_minutes": 10,
                "allow_bot_swap_in_rest": True,
            },
            {
                "key": "ko",
                "type": "single_elimination",
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
]
