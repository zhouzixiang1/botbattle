"""德州扑克赛事模板（per-game，全面解耦 PR5）。

本游戏自带的内置赛事阶段模板。contests/templates.py 的 DEFAULT_TEMPLATES 从
注册表（各 spec.templates）派生，单一真相。
"""
from __future__ import annotations

from typing import Any

# 德州用 3-1-0 计分（胜3/平1/负0）。常量内联，避免 import contests.templates
# 触发 contests 包重初始化（与 games ↔ matches ↔ orchestrator 循环）。
SCORING_POKER = "poker_3_1_0"
PAIRED_SWAP_TIEBREAK = "paired_swap_until_decided"


def _series_config(
    stage_key: str,
    label: str,
    *,
    default: int,
    allowed: list[int],
    swiss_extra: tuple[int, int, int] | None = None,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "stage_key": stage_key,
        "label": label,
        "games_per_pair": {
            "default": default,
            "allowed_values": allowed,
        },
    }
    if swiss_extra is not None:
        extra_default, extra_min, extra_max = swiss_extra
        config["swiss_extra_rounds"] = {
            "default": extra_default,
            "min": extra_min,
            "max": extra_max,
        }
    return config


TEMPLATES: list[dict[str, Any]] = [
    {
        # 公平优先默认：每对选手用同一副牌交换座位进行两场 70 手计分场，
        # 两场独立计分。循环赛不限人数，物理并发仍由执行队列硬顶控制。
        "id": "holdem_dup_rr",
        "name": "德州：复式单循环（公平优先）",
        "summary": (
            "每对 Bot 使用同一副牌交换座位各赛 70 手，两场分别按 3/1/0 "
            "独立计分；覆盖所有对手，场次数随人数平方增长。"
        ),
        "recommended": True,
        "recommended_min": 2,
        "recommended_max": 8,
        "purpose": "fairness",
        "time_class": "long",
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
        "id": "holdem_rr",
        "name": "德州：普通单循环（速度优先）",
        "summary": (
            "每对 Bot 交手一次并覆盖全部对手；不使用同牌换座，场数少于复式循环，"
            "更适合小规模、时间有限的赛事。"
        ),
        "recommended_min": 2,
        "recommended_max": 8,
        "purpose": "speed",
        "time_class": "medium",
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
        "id": "holdem_swiss_ranked",
        "name": "德州：瑞士制最终排名",
        "summary": (
            "以有限轮次覆盖不同强度对手并直接产生全员正式排名；默认自动轮数再加 2 轮，"
            "总场数远低于大规模循环赛。"
        ),
        "recommended_min": 9,
        "recommended_max": None,
        "purpose": "ranking",
        "time_class": "medium",
        "game_id": "holdem",
        "stage_series_configs": [
            _series_config(
                "swiss",
                "瑞士排名",
                default=2,
                allowed=[1, 2, 4],
                swiss_extra=(2, 0, 4),
            )
        ],
        "stages": [
            {
                "key": "swiss",
                "type": "swiss",
                "rounds": 0,
                "scoring": SCORING_POKER,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "holdem_swiss_top8_ranked",
        "name": "德州：瑞士 → Top 8 排位循环",
        "summary": (
            "瑞士制筛出 8 强，再用多场循环决定冠军和完整 Top 8 顺序；"
            "决赛样本充分，但耗时高于淘汰赛。"
        ),
        "recommended_min": 9,
        "recommended_max": None,
        "purpose": "championship",
        "time_class": "long",
        "game_id": "holdem",
        "stage_series_configs": [
            _series_config(
                "swiss",
                "瑞士资格赛",
                default=2,
                allowed=[1, 2, 4],
                swiss_extra=(2, 0, 4),
            ),
            _series_config(
                "final8",
                "Top 8 排位循环",
                default=4,
                allowed=[2, 4, 6, 8, 10],
            ),
        ],
        "stages": [
            {
                "key": "swiss",
                "type": "swiss",
                "rounds": 0,
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
    {
        "id": "holdem_swiss_ko",
        "name": "德州：瑞士 → 单败（大规模快速冠军赛）",
        "summary": (
            "瑞士制筛出 8 强后进入单败；基础赛程短，淘汰对局若打平会自动追加"
            "同牌换座的两场决胜组，直到产生晋级者。"
        ),
        "recommended_min": 9,
        "recommended_max": None,
        "purpose": "championship",
        "time_class": "short",
        "game_id": "holdem",
        "stages": [
            {
                "key": "swiss",
                "type": "swiss",
                "rounds": 0,
                "scoring": SCORING_POKER,
                "advance_count": 8,
                "rest_after_minutes": 10,
                "allow_bot_swap_in_rest": True,
            },
            {
                "key": "ko",
                "type": "single_elimination",
                "tiebreak": PAIRED_SWAP_TIEBREAK,
                "scoring": SCORING_POKER,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        "id": "holdem_top8_ranked",
        "name": "德州：独立 Top 8 排位循环",
        "summary": (
            "供已经完成资格赛的 8 强名单直接使用；只进行多场循环决赛，"
            "不重复全员资格赛。"
        ),
        "recommended_min": 8,
        "recommended_max": 8,
        "purpose": "championship",
        "time_class": "long",
        "game_id": "holdem",
        "phase": "final",
        "stage_series_configs": [
            _series_config(
                "final8",
                "Top 8 排位循环",
                default=4,
                allowed=[2, 4, 6, 8, 10],
            )
        ],
        "stages": [
            {
                "key": "final8",
                "type": "double_round_robin",
                "scoring": SCORING_POKER,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    {
        # 独立 preliminary 工作流的正式模板；与 standalone 最终排名并存。
        "id": "holdem_prelim_swiss",
        "name": "德州：预赛（大规模瑞士快速排名）",
        "summary": (
            "面向独立资格赛，默认在自动瑞士轮数上额外进行 2 轮并产生全员预赛排名；"
            "受无重复对手覆盖上限约束。"
        ),
        "recommended_min": 9,
        "recommended_max": None,
        "purpose": "ranking",
        "time_class": "medium",
        "game_id": "holdem",
        "phase": "preliminary",
        "stage_series_configs": [
            _series_config(
                "prelim",
                "预赛瑞士轮",
                default=2,
                allowed=[1, 2, 4],
                swiss_extra=(2, 0, 4),
            )
        ],
        "stages": [
            {
                "key": "prelim",
                "type": "swiss",
                "rounds": 0,
                "scoring": SCORING_POKER,
                "allow_bot_swap_in_rest": True,
                "rest_after_minutes": 0,
            },
        ],
    },
    {
        # 历史两段式决赛保留给冻结赛事读取；新建不再重复全员循环。
        "id": "holdem_final_ranked",
        "name": "德州：历史决赛（循环 → Top 8）",
        "summary": "旧版全员循环后再进行 Top 8 循环；历史赛事可读，新建不再推荐。",
        "creation_enabled": False,
        "game_id": "holdem",
        "phase": "final",
        "stage_series_configs": [
            _series_config(
                "qualify",
                "决赛全员循环排位",
                default=2,
                allowed=[1, 2, 4],
            ),
            _series_config(
                "final8",
                "Top 8 决胜",
                default=4,
                allowed=[2, 4, 6, 8, 10],
            ),
        ],
        "stages": [
            {
                "key": "qualify",
                "type": "round_robin",
                "allow_large_round_robin": True,
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
