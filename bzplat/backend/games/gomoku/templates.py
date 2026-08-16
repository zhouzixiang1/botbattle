"""五子棋赛事模板（per-game，全面解耦 PR5）。"""
from __future__ import annotations

from typing import Any

# 棋类用 2-1-0 计分（胜2/平1/负0）。常量内联，避免 import contests.templates 循环。
SCORING_CCGC = "ccgc_2_1_0"

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "gomoku_group_drr_ko",
        "name": "五子棋：分组双循环 → 单败",
        "game_id": "gomoku",
        # 历史赛事/演示快照仍需解析名称和冻结阶段；但五子棋允许
        # 和棋，本模板未声明破和规则，不得继续用于新建赛事。
        "creation_enabled": False,
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
        "id": "gomoku_swiss_ko",
        "name": "五子棋：瑞士 → 单败",
        "game_id": "gomoku",
        "creation_enabled": False,
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
        "id": "board_rr",
        "name": "五子棋：双循环",
        "game_id": "gomoku",
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
