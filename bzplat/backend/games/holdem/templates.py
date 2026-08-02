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
    {
        # 预赛：单阶段瑞士，全员唯一正式名次（公开报名，无人数上限）。
        "id": "holdem_prelim_swiss",
        "name": "德州：预赛（瑞士全员排名）",
        "game_id": "holdem",
        "phase": "preliminary",
        "stages": [
            {
                "key": "prelim",
                "type": "swiss",
                "rounds": 0,  # ceil(log2(n)) 自动
                "scoring": SCORING_POKER,
                "allow_bot_swap_in_rest": True,
                "rest_after_minutes": 0,
            },
        ],
    },
    {
        # 决赛：Stage1 全员单循环（allow_large_round_robin 旁路 FULL_RR_MAX_N）
        # → Stage2 Top8 双循环（ranking_mode=replace_top 合成榜）。
        "id": "holdem_final_ranked",
        "name": "德州：决赛（循环→Top8）",
        "game_id": "holdem",
        "phase": "final",
        "stages": [
            {
                "key": "qualify",
                "type": "round_robin",
                "allow_large_round_robin": True,  # 旁路 FULL_RR_MAX_N=12（仅白名单模板）
                "advance_count": 8,
                "scoring": SCORING_POKER,
                "rest_after_minutes": 10,
                "allow_bot_swap_in_rest": True,
            },
            {
                "key": "final8",
                "type": "double_round_robin",
                "ranking_mode": "replace_top",
                "ranking_scope": 8,
                "scoring": SCORING_POKER,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
]
