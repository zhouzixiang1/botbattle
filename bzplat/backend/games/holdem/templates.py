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
        # 公平优先默认：每对选手用同一副牌交换座位进行两场 70 手计分场，
        # 两场独立计分。循环赛仍受 FULL_RR_MAX_N=12 的通用门禁约束。
        "id": "holdem_dup_rr",
        "name": "德州：复式单循环（公平优先，≤12 人）",
        "summary": (
            "推荐用于 12 人以内赛事：每对 Bot 使用同一副牌交换座位各赛 70 手，"
            "两个 70 手计分场分别按 3/1/0 独立计分，组合筹码差只用于破同分；"
            "降低发牌与座位差异，耗时高于瑞士制。"
        ),
        "recommended": True,
        "game_id": "holdem",
        "games_per_pair_config": {"default": 1, "min": 1, "max": 10},
        "stages": [
            {
                "key": "dup_rr",
                "type": "round_robin",
                "duplicate": True,
                "scoring": SCORING_POKER,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "holdem_swiss_ko",
        "name": "德州：瑞士 → 单败（大规模快速）",
        "summary": (
            "适合参赛人数较多、需要控制总场次的赛事；瑞士轮只覆盖部分对手，"
            "样本少于循环赛。"
        ),
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
        "summary": (
            "12 人以内每对 Bot 交手一次，覆盖完整对手；不使用同副牌换座，"
            "公平性与耗时介于复式单循环和瑞士制之间。"
        ),
        "game_id": "holdem",
        "games_per_pair_config": {"default": 1, "min": 1, "max": 10},
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
        "name": "德州：预赛（大规模瑞士快速排名）",
        "summary": (
            "面向大规模公开预赛，默认在 ceil(log2(人数)) 基础上额外进行 2 轮"
            "（受无重复对手图上限约束）；相较循环赛总场次较少，同时通过更多"
            "不同对手与重复交锋提高稳定性。"
        ),
        "game_id": "holdem",
        "phase": "preliminary",
        "stage_series_configs": [
            {
                "stage_key": "prelim",
                "label": "预赛瑞士轮",
                "games_per_pair": {
                    "default": 2,
                    "allowed_values": [1, 2, 4],
                },
                "swiss_extra_rounds": {"default": 2, "min": 0, "max": 4},
            },
        ],
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
        "stage_series_configs": [
            {
                "stage_key": "qualify",
                "label": "决赛全员循环排位",
                "games_per_pair": {
                    "default": 2,
                    "allowed_values": [1, 2, 4],
                },
            },
            {
                "stage_key": "final8",
                "label": "Top 8 决胜",
                "games_per_pair": {
                    "default": 4,
                    "allowed_values": [2, 4, 6, 8, 10],
                },
            },
        ],
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
