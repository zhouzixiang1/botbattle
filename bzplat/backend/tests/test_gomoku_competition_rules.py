"""2025 全国机器博弈竞赛五子棋状态机回归。"""
from __future__ import annotations

import asyncio
from collections import defaultdict

import pytest

from bzplat.backend.games.gomoku import protocol as proto
from bzplat.backend.games.gomoku.engine import GomokuSession
from bzplat.backend.games.gomoku.gomoku_judge import (
    OPENING_CATALOG,
    new_board,
    validate_black5_candidates,
    validate_opening,
)
from bzplat.backend.store.public_contract import sanitize_public_event


def _base_action(request: dict, *, swap: bool = False) -> dict:
    phase = request["phase"]
    if phase == proto.PHASE_OPENING:
        return {
            "response": {
                "action": "opening",
                "white2": {"x": 7, "y": 8},
                "black3": {"x": 8, "y": 8},
                "n": 2,
            }
        }
    if phase == proto.PHASE_SWAP:
        return {"response": {"action": "swap", "swap": swap}}
    if phase == proto.PHASE_WHITE4:
        return {"response": {"action": "move", "x": 6, "y": 8}}
    if phase == proto.PHASE_BLACK5_CANDIDATES:
        return {
            "response": {
                "action": "black5_candidates",
                "points": [{"x": 9, "y": 9}, {"x": 5, "y": 5}],
            }
        }
    if phase == proto.PHASE_BLACK5_SELECT:
        return {"response": {"action": "black5_select", "index": 0}}
    return {"response": {"action": "pass"}}


def test_opening_geometry_is_exactly_26_symmetry_classes():
    assert len(OPENING_CATALOG) == 26
    assert sum(key[0] == "straight" for key in OPENING_CATALOG) == 13
    assert sum(key[0] == "diagonal" for key in OPENING_CATALOG) == 13
    assert validate_opening((7, 8), (8, 8), 2)
    assert validate_opening((8, 8), (9, 7), 5)
    assert validate_opening((7, 9), (8, 8), 2) is None
    assert validate_opening((7, 8), (10, 10), 2) is None
    assert validate_opening((7, 8), (8, 8), 1) is None


def _four_stone_board(
    *,
    black3: tuple[int, int] = (7, 9),
    white2: tuple[int, int] = (7, 8),
    white4: tuple[int, int] = (7, 6),
) -> list[list[int]]:
    board = new_board()
    board[7][7] = 0
    board[black3[0]][black3[1]] = 0
    board[white2[0]][white2[1]] = 1
    board[white4[0]][white4[1]] = 1
    return board


def test_black5_candidates_reject_same_shape_under_remaining_reflection():
    board = _four_stone_board()

    # 当前四子都在 x=7 上，镜像 x -> 14-x 保持彩色盘面不变；
    # (6,5) 与 (8,5) 因此是同形打点，虽然坐标不同也必须拒绝。
    assert not validate_black5_candidates(board, [(6, 5), (8, 5)])


def test_black5_candidates_accept_distinct_orbits_on_symmetric_board():
    board = _four_stone_board()

    assert validate_black5_candidates(board, [(6, 5), (6, 4)])


def test_black5_candidates_only_need_distinct_coordinates_without_residual_symmetry():
    board = _four_stone_board(black3=(8, 8), white4=(2, 2))

    # 白2/白4 与黑1/黑3 的彩色布局打破所有非恒等 D4 对称；
    # 因此即使两点相对棋盘中心旋转对称，在当前盘面下仍不同形。
    assert validate_black5_candidates(board, [(0, 0), (14, 14)])


def test_engine_rejects_distinct_coordinates_that_are_same_shape():
    async def decide(_seat: int, request: dict):
        phase = request["phase"]
        if phase == proto.PHASE_OPENING:
            return {
                "response": {
                    "action": "opening",
                    "white2": {"x": 7, "y": 8},
                    "black3": {"x": 7, "y": 9},
                    "n": 2,
                }
            }
        if phase == proto.PHASE_SWAP:
            return {"response": {"action": "swap", "swap": False}}
        if phase == proto.PHASE_WHITE4:
            return {"response": {"action": "move", "x": 7, "y": 6}}
        if phase == proto.PHASE_BLACK5_CANDIDATES:
            return {
                "response": {
                    "action": "black5_candidates",
                    "points": [{"x": 6, "y": 5}, {"x": 8, "y": 5}],
                }
            }
        raise AssertionError("same-shape candidates must end the match")

    result = asyncio.run(GomokuSession().run_async(decide))

    assert result.winner == 1
    assert result.reason == "illegal_candidates"
    incident = next(event for event in result.events if event["type"] == "illegal")
    assert incident["phase"] == proto.PHASE_BLACK5_CANDIDATES
    assert incident["why"] == "candidate_not_empty_distinct_shape"


def test_duplicate_candidates_are_game_illegal_not_protocol_fault():
    duplicate_action = {
        "action": "black5_candidates",
        "points": [{"x": 6, "y": 5}, {"x": 6, "y": 5}],
    }
    # 它是类型/形状正确的动作；重复性留给纯裁判。
    assert proto.validate_response_payload(duplicate_action) == duplicate_action

    async def decide(_seat: int, request: dict):
        action = _base_action(request)
        if request["phase"] == proto.PHASE_OPENING:
            action["response"]["black3"] = {"x": 7, "y": 9}
        elif request["phase"] == proto.PHASE_WHITE4:
            action["response"] = {"action": "move", "x": 7, "y": 6}
        elif request["phase"] == proto.PHASE_BLACK5_CANDIDATES:
            action["response"] = duplicate_action
        return action

    result = asyncio.run(GomokuSession().run_async(decide))

    assert result.winner == 1
    assert result.reason == "illegal_candidates"
    incident = next(event for event in result.events if event["type"] == "illegal")
    assert incident["why"] == "candidate_not_empty_distinct_shape"


@pytest.mark.parametrize("n", [1, 6])
def test_opening_n_range_is_game_illegal_not_protocol_fault(n: int):
    opening = {
        "action": "opening",
        "white2": {"x": 7, "y": 8},
        "black3": {"x": 8, "y": 8},
        "n": n,
    }
    assert proto.validate_response_payload(opening) == opening

    async def decide(_seat: int, _request: dict):
        return {"response": opening}

    result = asyncio.run(GomokuSession().run_async(decide))

    assert result.winner == 1
    assert result.reason == "illegal_opening"


@pytest.mark.parametrize("count", [1, 6])
def test_candidate_count_is_game_illegal_not_protocol_fault(count: int):
    wrong_count_action = {
        "action": "black5_candidates",
        "points": [{"x": x, "y": 0} for x in range(count)],
    }
    assert proto.validate_response_payload(wrong_count_action) == wrong_count_action

    async def decide(_seat: int, request: dict):
        action = _base_action(request)
        if request["phase"] == proto.PHASE_BLACK5_CANDIDATES:
            action["response"] = wrong_count_action
        return action

    result = asyncio.run(GomokuSession().run_async(decide))

    assert result.winner == 1
    assert result.reason == "illegal_candidates"


@pytest.mark.parametrize("index", [-1, 2, 5])
def test_candidate_selection_range_is_game_illegal_not_protocol_fault(index: int):
    invalid_selection = {"action": "black5_select", "index": index}
    assert proto.validate_response_payload(invalid_selection) == invalid_selection

    async def decide(_seat: int, request: dict):
        action = _base_action(request)
        if request["phase"] == proto.PHASE_BLACK5_SELECT:
            action["response"] = invalid_selection
        return action

    result = asyncio.run(GomokuSession().run_async(decide))

    assert result.winner == 0
    assert result.reason == "illegal_selection"
    assert not any(event["type"] == "black5_selected" for event in result.events)


def test_v2_protocol_rejects_legacy_xy_and_accepts_every_action_shape():
    try:
        proto.validate_response_payload({"x": 1, "y": 2})
    except ValueError:
        pass
    else:  # pragma: no cover - explicit protocol gate
        raise AssertionError("旧 x/y payload 不得继续兼容")

    assert proto.validate_response_payload({"action": "swap", "swap": True})
    assert proto.validate_response_payload({"action": "move", "x": 1, "y": 2})
    assert proto.validate_response_payload({"action": "pass"})
    assert proto.validate_response_payload(
        {
            "action": "black5_candidates",
            "points": [{"x": 1, "y": 2}, {"x": 2, "y": 3}],
        }
    )
    assert proto.validate_response_payload({"action": "black5_select", "index": 1})


def test_opening_swap_n_choice_and_double_pass_are_replayed_without_candidate_stones():
    calls: list[tuple[int, str, int | None]] = []

    async def decide(seat: int, request: dict):
        calls.append((seat, request["phase"], request["color"]))
        return _base_action(request, swap=True)

    result = asyncio.run(GomokuSession().run_async(decide))

    assert result.winner is None
    assert result.reason == "double_pass"
    assert result.rounds_played == 5
    end = result.events[-1]
    assert end["seat_colors"] == [1, 0]
    assert end["ruleset"] == proto.RULESET_ID
    assert [phase for _, phase, _ in calls[:5]] == [
        proto.PHASE_OPENING,
        proto.PHASE_SWAP,
        proto.PHASE_WHITE4,
        proto.PHASE_BLACK5_CANDIDATES,
        proto.PHASE_BLACK5_SELECT,
    ]
    # swap 后：seat0 执白，故白4/候选选择归 seat0；seat1 执黑提交 N 点。
    assert [seat for seat, _, _ in calls[:5]] == [0, 1, 0, 1, 0]
    candidate_event = next(e for e in result.events if e["type"] == "black5_candidates")
    selected = next(e for e in result.events if e["type"] == "black5_selected")
    assert len(candidate_event["points"]) == 2
    assert selected["point"] == {"x": 9, "y": 9}
    assert result.board_grid[9][9] == 0
    assert result.board_grid[5][5] == -1


def test_legacy_xy_bot_loses_at_opening_instead_of_silently_falling_back():
    async def decide(_seat: int, _request: dict):
        return {"response": {"x": 7, "y": 7}}

    result = asyncio.run(GomokuSession().run_async(decide))
    assert result.winner == 1
    assert result.reason == "illegal_opening"
    assert result.rounds_played == 0


def test_black_exact_five_uses_post_swap_seat_identity():
    normal_moves = {
        1: iter([(0, 0), (0, 1)]),
        0: iter([(10, 10), (11, 11)]),
    }

    async def decide(seat: int, request: dict):
        if request["phase"] != proto.PHASE_NORMAL:
            return _base_action(request, swap=False)
        x, y = next(normal_moves[seat])
        return {"response": {"action": "move", "x": x, "y": y}}

    result = asyncio.run(GomokuSession().run_async(decide))
    assert result.winner == 0
    assert result.reason == "five"
    assert result.rounds_played == 9


def test_black_overline_is_immediate_white_win():
    normal_moves = defaultdict(list)
    normal_moves[1] = [(0, 0), (0, 1), (0, 2)]
    normal_moves[0] = [(10, 10), (12, 12), (11, 11)]
    indexes = defaultdict(int)

    async def decide(seat: int, request: dict):
        if request["phase"] != proto.PHASE_NORMAL:
            return _base_action(request, swap=False)
        x, y = normal_moves[seat][indexes[seat]]
        indexes[seat] += 1
        return {"response": {"action": "move", "x": x, "y": y}}

    result = asyncio.run(GomokuSession().run_async(decide))
    assert result.winner == 1
    assert result.reason == "forbidden_overline"
    incident = next(event for event in result.events if event["type"] == "forbidden")
    assert incident["forbidden_kind"] == "overline"


def test_v2_replay_events_and_human_request_cross_public_whitelist():
    opening = sanitize_public_event(
        {
            "type": "opening",
            "player": 0,
            "opening_code": "S09",
            "n": 2,
            "black1": {"x": 7, "y": 7},
            "white2": {"x": 7, "y": 8},
            "black3": {"x": 8, "y": 8},
            "private": "drop",
        }
    )
    assert opening == {
        "type": "opening",
        "player": 0,
        "opening_code": "S09",
        "n": 2,
        "black1": {"x": 7, "y": 7},
        "white2": {"x": 7, "y": 8},
        "black3": {"x": 8, "y": 8},
    }
    forbidden = sanitize_public_event(
        {
            "type": "forbidden",
            "player": 1,
            "color": 0,
            "x": 8,
            "y": 8,
            "forbidden_kind": "double_three",
            "debug": "drop",
        }
    )
    assert forbidden == {
        "type": "forbidden",
        "player": 1,
        "color": 0,
        "x": 8,
        "y": 8,
        "forbidden_kind": "double_three",
    }
    illegal = sanitize_public_event(
        {
            "type": "illegal",
            "player": 0,
            "phase": proto.PHASE_OPENING,
            "action": {
                "action": "opening",
                "white2": {"x": 7, "y": 8},
                "black3": {"x": 8, "y": 8},
                "n": 9,
                "secret": "drop",
            },
            "why": "not_one_of_26_openings",
            "private": "drop",
        }
    )
    assert illegal == {
        "type": "illegal",
        "player": 0,
        "phase": proto.PHASE_OPENING,
        "action": {"action": "opening"},
        "why": "not_one_of_26_openings",
    }

    request = proto.build_request(
        phase=proto.PHASE_BLACK5_SELECT,
        me=1,
        color=1,
        board=[[-1] * 15 for _ in range(15)],
        seat_colors=[0, 1],
        n=2,
        candidates=[{"x": 6, "y": 6}, {"x": 8, "y": 8}],
    )
    your_turn = sanitize_public_event(
        {"type": "your_turn", "player": 1, "request": request, "secret": "drop"}
    )
    assert your_turn is not None
    assert your_turn["request"]["phase"] == proto.PHASE_BLACK5_SELECT
    assert your_turn["request"]["candidates"] == [
        {"x": 6, "y": 6},
        {"x": 8, "y": 8},
    ]
    assert "secret" not in your_turn
