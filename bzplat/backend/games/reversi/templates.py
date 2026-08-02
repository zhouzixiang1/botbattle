"""黑白棋赛事模板（per-game）。"""
from __future__ import annotations

from typing import Any

# 棋类用 2-1-0 计分（胜2/平1/负0）。常量内联，避免 import contests.templates 循环。
SCORING_CCGC = "ccgc_2_1_0"

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "reversi_swiss_ko",
        "name": "黑白棋：瑞士 → 单败",
        "game_id": "reversi",
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
    {
        "id": "reversi_rr",
        "name": "黑白棋：双循环（小规模）",
        "game_id": "reversi",
        "stages": [
            {
                "key": "drr",
                "type": "double_round_robin",
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
]
