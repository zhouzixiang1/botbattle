"""默认赛事阶段模板。"""
from __future__ import annotations

import copy
from typing import Any

# scoring: poker_3_1_0 | ccgc_2_1_0
SCORING_POKER = "poker_3_1_0"
SCORING_CCGC = "ccgc_2_1_0"

# 每款游戏的对局参数（替代德扑专属的 hands_per_match）
#   holdem: {"hands": 70}      每场手数
#   gomoku: {}                  单局，无可调参数
#   pencil: {"n_dots": 11}      点阵边长
DEFAULT_MATCH_CONFIG: dict[str, dict[str, Any]] = {
    "holdem": {"hands": 70},
    "gomoku": {},
    "pencil": {"n_dots": 11},
}


def default_match_config(game_id: str | None) -> dict[str, Any]:
    """返回指定游戏的默认 match_config（深拷贝）。"""
    return copy.deepcopy(DEFAULT_MATCH_CONFIG.get((game_id or "holdem").strip().lower(), {"hands": 70}))


DEFAULT_TEMPLATES: dict[str, dict[str, Any]] = {
    "holdem_swiss_ko": {
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
    "holdem_rr": {
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
    "gomoku_group_drr_ko": {
        "id": "gomoku_group_drr_ko",
        "name": "五子棋：分组双循环 → 单败",
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
                "scoring": SCORING_CCGC,
                "rest_after_minutes": 0,
                "allow_bot_swap_in_rest": False,
            },
        ],
    },
    "gomoku_swiss_ko": {
        "id": "gomoku_swiss_ko",
        "name": "五子棋：瑞士 → 单败",
        "game_id": "gomoku",
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
    "pencil_group_drr_ko": {
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
    "pencil_swiss_ko": {
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
    "board_rr": {
        "id": "board_rr",
        "name": "棋类：双循环（课堂演示）",
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
}


def get_template(template_id: str) -> dict[str, Any] | None:
    t = DEFAULT_TEMPLATES.get(template_id)
    return copy.deepcopy(t) if t else None


def list_templates() -> list[dict[str, Any]]:
    return [copy.deepcopy(t) for t in DEFAULT_TEMPLATES.values()]


def resolve_stages(
    template_id: str | None,
    stages: list[dict[str, Any]] | None = None,
    *,
    game_id: str | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """返回 (template_id, game_id, stages)。"""
    if stages:
        tid = template_id or "custom"
        gid = game_id or "holdem"
        return tid, gid, copy.deepcopy(stages)
    tid = template_id or "holdem_swiss_ko"
    tpl = get_template(tid)
    if not tpl:
        raise ValueError(f"未知模板: {tid}")
    return tid, game_id or tpl["game_id"], copy.deepcopy(tpl["stages"])


def points_for_result(scoring: str, winner: int | None, side: int) -> float:
    """side: 0=A, 1=B。"""
    if scoring == SCORING_CCGC:
        win_pts, draw_pts, loss_pts = 2.0, 1.0, 0.0
    else:
        win_pts, draw_pts, loss_pts = 3.0, 1.0, 0.0
    if winner is None:
        return draw_pts
    if winner == side:
        return win_pts
    return loss_pts
