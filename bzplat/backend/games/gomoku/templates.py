"""五子棋赛事模板（per-game，全面解耦 PR5）。"""
from __future__ import annotations

from typing import Any

# 棋类用 2-1-0 计分（胜2/平1/负0）。常量内联，避免 import contests.templates 循环。
SCORING_CCGC = "ccgc_2_1_0"
PAIRED_SWAP_TIEBREAK = "paired_swap_until_decided"
SWISS_ROUND_BANDS: list[dict[str, int | None]] = [
    {"min_participants": 13, "max_participants": 15, "rounds": 7},
    {"min_participants": 16, "max_participants": 20, "rounds": 9},
    {"min_participants": 21, "max_participants": None, "rounds": 11},
]

TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "gomoku_seeded_group_drr_final",
        "name": "保护种子分组双循环 → 决赛双循环",
        "summary": (
            "限 22–26 人；从已完成模拟赛正式榜递补 4 或 5 名保护种子，"
            "随机均衡分组后每组前二进入积分清零的决赛双循环。"
        ),
        "recommended_min": 22,
        "recommended_max": 26,
        "participant_range_is_strict": True,
        "purpose": "championship",
        "time_class": "long",
        "game_id": "gomoku",
        "time_control_ids": ["gomoku_per_side_total_300s_v1"],
        "default_time_control_id": "gomoku_per_side_total_300s_v1",
        "requires_source_contest": True,
        "stages": [
            {
                "key": "groups",
                "type": "group_double_round_robin",
                "group_count": 4,
                "group_assignment": "protected_seed_random_balanced_v1",
                "overall_ranking": "cross_group_fair_v1",
                "advance_per_group": 2,
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 10,
                "allow_bot_swap_in_rest": True,
            },
            {
                "key": "final",
                "type": "double_round_robin",
                "ranking_mode": "replace_top",
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "board_rr",
        "name": "五子棋：双循环",
        "summary": "每对 Bot 交换先后手各赛一局，完整覆盖全部对手，适合小规模正式赛事。",
        "recommended": True,
        "recommended_min": 2,
        "recommended_max": 6,
        "purpose": "fairness",
        "time_class": "long",
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
    {
        "id": "gomoku_rr",
        "name": "五子棋：单循环",
        "summary": "每对 Bot 交手一次并产生全员排名；覆盖完整对手，场数为双循环的一半。",
        "recommended_min": 7,
        "recommended_max": 12,
        "purpose": "ranking",
        "time_class": "medium",
        "game_id": "gomoku",
        "stages": [
            {
                "key": "rr",
                "type": "round_robin",
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "gomoku_swiss_ranked",
        "name": "五子棋：瑞士制最终排名",
        "summary": (
            "按报名人数冻结轮数：13–15 人 7 轮、16–20 人 9 轮、21 人以上 11 轮；"
            "控制大规模赛事场数并产生全员正式排名。"
        ),
        "recommended_min": 13,
        "recommended_max": None,
        "purpose": "ranking",
        "time_class": "medium",
        "game_id": "gomoku",
        "stages": [
            {
                "key": "swiss",
                "type": "swiss",
                "rounds": 0,
                "swiss_round_bands": SWISS_ROUND_BANDS,
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "gomoku_swiss_top8_ranked",
        "name": "五子棋：瑞士 → Top 8 排位循环",
        "summary": (
            "瑞士阶段按 13–15 人 7 轮、16–20 人 9 轮、21 人以上 11 轮冻结，"
            "筛出 8 强后以双循环决定冠军和完整 Top 8 顺序。"
        ),
        "recommended_min": 13,
        "recommended_max": None,
        "purpose": "championship",
        "time_class": "long",
        "game_id": "gomoku",
        "stages": [
            {
                "key": "swiss",
                "type": "swiss",
                "rounds": 0,
                "swiss_round_bands": SWISS_ROUND_BANDS,
                "advance_count": 8,
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 10,
                "allow_bot_swap_in_rest": True,
            },
            {
                "key": "final8",
                "type": "double_round_robin",
                "ranking_mode": "replace_top",
                "ranking_scope": 8,
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "gomoku_group_drr_ko",
        "name": "五子棋：分组双循环 → 单败",
        "summary": (
            "四组双循环后每组前二晋级单败；淘汰对局若和棋会追加换先后手的"
            "两场决胜组，直到产生晋级者。"
        ),
        "recommended_min": 9,
        "recommended_max": 24,
        "purpose": "championship",
        "time_class": "long",
        "game_id": "gomoku",
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
                "tiebreak": PAIRED_SWAP_TIEBREAK,
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "gomoku_swiss_ko",
        "name": "五子棋：瑞士 → 单败",
        "summary": (
            "瑞士阶段按 13–15 人 7 轮、16–20 人 9 轮、21 人以上 11 轮冻结；"
            "筛出 8 强后进行单败，淘汰和棋以换先后手的"
            "两场决胜组继续比赛。"
        ),
        "recommended_min": 13,
        "recommended_max": None,
        "purpose": "championship",
        "time_class": "medium",
        "game_id": "gomoku",
        "stages": [
            {
                "key": "swiss",
                "type": "swiss",
                "rounds": 0,
                "swiss_round_bands": SWISS_ROUND_BANDS,
                "scoring": SCORING_CCGC,
                "advance_count": 8,
                "rest_after_minutes": 10,
                "allow_bot_swap_in_rest": True,
            },
            {
                "key": "ko",
                "type": "single_elimination",
                "tiebreak": PAIRED_SWAP_TIEBREAK,
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
]
