"""Focused RIF forbidden-move tests for Gomoku."""
from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest

from bzplat.backend.games.gomoku.forbidden import (
    BOARD_SIZE,
    BlackMoveKind,
    classify_black_move,
)

Point = tuple[int, int]


def _board(
    black: Iterable[Point], white: Iterable[Point] = ()
) -> list[list[int]]:
    board = [[-1 for _ in range(BOARD_SIZE)] for _ in range(BOARD_SIZE)]
    for x, y in black:
        board[x][y] = 0
    for x, y in white:
        assert board[x][y] == -1
        board[x][y] = 1
    return board


def _classify(
    anchor: Point,
    existing_black: Iterable[Point],
    white: Iterable[Point] = (),
) -> BlackMoveKind:
    board = _board([*existing_black, anchor], white)
    return classify_black_move(board, *anchor)


def test_exact_five_precedes_a_simultaneous_overline() -> None:
    anchor = (7, 7)
    existing = [
        (3, 7),
        (4, 7),
        (5, 7),
        (6, 7),  # horizontal exact five through anchor
        (7, 4),
        (7, 5),
        (7, 6),
        (7, 8),
        (7, 9),  # vertical overline through anchor
    ]
    assert _classify(anchor, existing) is BlackMoveKind.EXACT_FIVE


def test_overline_without_exact_five_is_forbidden() -> None:
    assert _classify(
        (7, 7),
        [(3, 7), (4, 7), (5, 7), (6, 7), (8, 7)],
    ) is BlackMoveKind.OVERLINE


def test_straight_four_has_two_winning_points_but_counts_once() -> None:
    assert _classify(
        (7, 7), [(5, 7), (6, 7), (8, 7)]
    ) is BlackMoveKind.LEGAL


def test_two_distinct_fours_in_one_direction_count_as_double_four() -> None:
    # Completing x=2 makes x=1..5; completing x=6 makes x=3..7.  The
    # four-stone sets differ even though both horizontal fours contain anchor.
    assert _classify(
        (5, 7), [(1, 7), (3, 7), (4, 7), (7, 7)]
    ) is BlackMoveKind.DOUBLE_FOUR


def test_crossing_open_threes_are_double_three() -> None:
    assert _classify(
        (7, 7), [(6, 7), (8, 7), (7, 6), (7, 8)]
    ) is BlackMoveKind.DOUBLE_THREE


def test_board_edge_makes_apparent_three_false() -> None:
    # Horizontal 0-1-2 cannot become a straight four with two winning ends.
    # The vertical row remains the only real three.
    assert _classify(
        (1, 7), [(0, 7), (2, 7), (1, 6), (1, 8)]
    ) is BlackMoveKind.LEGAL


def test_overline_winning_end_makes_apparent_three_false() -> None:
    # Horizontal continuation at x=6 would leave only one exact-five end:
    # filling x=4 connects x=3..8 into an overline.
    assert _classify(
        (7, 7), [(3, 7), (5, 7), (8, 7), (7, 6), (7, 8)]
    ) is BlackMoveKind.LEGAL


def test_double_four_continuation_makes_parent_three_false() -> None:
    # The apparent horizontal three can become straight only at e=(7,7).
    # That continuation simultaneously creates a horizontal and vertical four.
    # The anti-diagonal through anchor is therefore the sole real three.
    assert _classify(
        (6, 7),
        [(5, 7), (8, 7), (7, 4), (7, 5), (7, 6), (5, 8)],
    ) is BlackMoveKind.LEGAL


def test_forbidden_double_three_continuation_makes_parent_three_false() -> None:
    # At e=(7,7), vertical and main-diagonal real threes form a forbidden 3x3.
    assert _classify(
        (6, 7),
        [(5, 7), (8, 7), (7, 6), (7, 8), (6, 6), (8, 8), (5, 8)],
    ) is BlackMoveKind.LEGAL


def test_recursive_allowed_double_three_restores_parent_real_three() -> None:
    # White blockers make e's main-diagonal apparent three false.  Therefore
    # e=(7,7) is a legal continuation, the parent horizontal three is real,
    # and it combines with anchor's anti-diagonal real three into a forbidden
    # double-three.  A non-recursive detector gets this case wrong.
    assert _classify(
        (6, 7),
        [(5, 7), (8, 7), (7, 6), (7, 8), (6, 6), (8, 8), (5, 8)],
        [(4, 4), (10, 10)],
    ) is BlackMoveKind.DOUBLE_THREE


def _identity(point: Point) -> Point:
    return point


def _rotate90(point: Point) -> Point:
    x, y = point
    return y, BOARD_SIZE - 1 - x


def _reflect_x(point: Point) -> Point:
    x, y = point
    return BOARD_SIZE - 1 - x, y


def _compose(
    left: Callable[[Point], Point], right: Callable[[Point], Point]
) -> Callable[[Point], Point]:
    return lambda point: left(right(point))


def _symmetries() -> list[Callable[[Point], Point]]:
    rotations = [_identity]
    for _ in range(3):
        rotations.append(_compose(_rotate90, rotations[-1]))
    return [*rotations, *[_compose(_reflect_x, rotation) for rotation in rotations]]


@pytest.mark.parametrize("transform", _symmetries())
def test_double_four_is_invariant_under_board_symmetry(
    transform: Callable[[Point], Point],
) -> None:
    anchor = transform((5, 7))
    existing = [transform(point) for point in [(1, 7), (3, 7), (4, 7), (7, 7)]]
    assert _classify(anchor, existing) is BlackMoveKind.DOUBLE_FOUR


def test_hypothetical_search_does_not_mutate_callers_board() -> None:
    anchor = (6, 7)
    board = _board(
        [
            anchor,
            (5, 7),
            (8, 7),
            (7, 6),
            (7, 8),
            (6, 6),
            (8, 8),
            (5, 8),
        ],
        [(4, 4), (10, 10)],
    )
    before = [column[:] for column in board]

    assert classify_black_move(board, *anchor) is BlackMoveKind.DOUBLE_THREE
    assert board == before


@pytest.mark.parametrize(
    ("anchor", "cell"),
    [((15, 0), None), ((7, 7), -1), ((7, 7), 1)],
)
def test_invalid_anchor_fails_loudly(anchor: Point, cell: int | None) -> None:
    board = _board([])
    if cell is not None:
        board[anchor[0]][anchor[1]] = cell
    with pytest.raises(ValueError):
        classify_black_move(board, *anchor)
