"""点格棋赛事模板（per-game，全面解耦 PR5）。"""
from __future__ import annotations

from typing import Any

# 棋类用 2-1-0 计分（胜2/平1/负0）。常量内联，避免 import contests.templates 循环。
SCORING_CCGC = "ccgc_2_1_0"

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "pencil_drr",
        "name": "点格棋：双循环",
        "summary": "每对 Bot 交换先后手各赛一局，重复样本和完整对手覆盖适合小规模赛事。",
        "recommended": True,
        "recommended_min": 2,
        "recommended_max": 8,
        "purpose": "fairness",
        "time_class": "medium",
        "game_id": "pencil",
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
    {
        "id": "pencil_group_drr_ko",
        "name": "点格棋：分组双循环 → 单败",
        "summary": "四组双循环后每组前二晋级单败，在完整小组样本与总赛程之间取平衡。",
        "recommended_min": 9,
        "recommended_max": 24,
        "purpose": "championship",
        "time_class": "medium",
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
        "id": "pencil_swiss_ranked",
        "name": "点格棋：瑞士制最终排名",
        "summary": "以有限瑞士轮控制场数，所有参赛者保留正式名次，适合中大型赛事。",
        "recommended_min": 9,
        "recommended_max": None,
        "purpose": "ranking",
        "time_class": "medium",
        "game_id": "pencil",
        "stages": [
            {
                "key": "swiss",
                "type": "swiss",
                "rounds": 0,
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "pencil_swiss_ko",
        "name": "点格棋：瑞士 → 单败",
        "summary": "瑞士制筛出 8 强后进行单败，适合大规模且需要明确冠军的赛事。",
        "recommended_min": 25,
        "recommended_max": None,
        "purpose": "championship",
        "time_class": "medium",
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
    {
        "id": "pencil_ko",
        "name": "点格棋：纯单败（最短赛程）",
        "summary": "只进行单败淘汰，基础场数固定为参赛人数减一；适合时间紧张的冠军赛。",
        "recommended_min": 2,
        "recommended_max": None,
        "purpose": "speed",
        "time_class": "short",
        "game_id": "pencil",
        "stages": [
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
