"""Renju-style black forbidden-move adjudication for Gomoku.

This module is deliberately independent from the platform and session layers.  It
implements the RIF definitions used by the Chinese competition rules:

* an exact five takes precedence over every forbidden pattern;
* an overline is a maximal unbroken row of six or more black stones;
* a four is identified by its four constituent stones, not by its winning point;
* a three is real only when a *legal, non-winning* continuation can turn it into
  a straight four.  Continuation legality is therefore checked recursively.

The public function only reads the supplied board.  Hypothetical positions are
represented by integer bitboards, so recursive analysis cannot leak mutations
back into the caller's board.
"""
from __future__ import annotations

from enum import Enum
from typing import Iterator, Sequence

BOARD_SIZE = 15
EMPTY = -1
BLACK = 0
WHITE = 1

_DIRECTIONS = ((1, 0), (0, 1), (1, 1), (1, -1))


class BlackMoveKind(str, Enum):
    """Classification of a black stone that has just been placed."""

    EXACT_FIVE = "exact_five"
    OVERLINE = "overline"
    DOUBLE_FOUR = "double_four"
    DOUBLE_THREE = "double_three"
    LEGAL = "legal"


_MemoKey = tuple[int, int, int, int]
_Memo = dict[_MemoKey, BlackMoveKind]
_Pattern = tuple[int, frozenset[int]]


def _index(x: int, y: int, size: int) -> int:
    return x * size + y


def _coords(point: int, size: int) -> tuple[int, int]:
    return divmod(point, size)


def _in_board(x: int, y: int, size: int) -> bool:
    return 0 <= x < size and 0 <= y < size


def _occupied(black: int, white: int, point: int) -> bool:
    bit = 1 << point
    return bool((black | white) & bit)


def _is_black(black: int, point: int) -> bool:
    return bool(black & (1 << point))


def _windows_containing(
    anchor: int, length: int, direction: tuple[int, int], size: int
) -> Iterator[tuple[int, ...]]:
    """Yield every in-board ``length`` segment containing ``anchor``."""

    ax, ay = _coords(anchor, size)
    dx, dy = direction
    for anchor_offset in range(length):
        sx = ax - anchor_offset * dx
        sy = ay - anchor_offset * dy
        ex = sx + (length - 1) * dx
        ey = sy + (length - 1) * dy
        if not (_in_board(sx, sy, size) and _in_board(ex, ey, size)):
            continue
        yield tuple(_index(sx + i * dx, sy + i * dy, size) for i in range(length))


def _run_length(
    black: int, anchor: int, direction: tuple[int, int], size: int
) -> int:
    """Return the maximal unbroken black run through ``anchor``."""

    ax, ay = _coords(anchor, size)
    dx, dy = direction
    total = 1
    for sign in (-1, 1):
        x, y = ax + sign * dx, ay + sign * dy
        while _in_board(x, y, size):
            point = _index(x, y, size)
            if not _is_black(black, point):
                break
            total += 1
            x += sign * dx
            y += sign * dy
    return total


def _encode_board(
    board: Sequence[Sequence[int]], size: int
) -> tuple[int, int]:
    if len(board) != size or any(len(column) != size for column in board):
        raise ValueError(f"board must be {size}x{size}")

    black = 0
    white = 0
    for x, column in enumerate(board):
        for y, cell in enumerate(column):
            point = _index(x, y, size)
            if cell == BLACK:
                black |= 1 << point
            elif cell == WHITE:
                white |= 1 << point
            elif cell != EMPTY:
                raise ValueError(f"unsupported board cell {cell!r} at ({x}, {y})")
    return black, white


def _four_patterns(
    black: int, white: int, anchor: int, size: int
) -> set[_Pattern]:
    """Return distinct fours made by ``anchor``, stopping once two are known.

    A straight four has two winning points but remains one four because the
    constituent four-stone row is the same.  Conversely, two distinct
    four-stone rows in the same direction are two fours.
    """

    patterns: set[_Pattern] = set()
    for direction_index, direction in enumerate(_DIRECTIONS):
        for window in _windows_containing(anchor, 5, direction, size):
            stones = tuple(point for point in window if _is_black(black, point))
            empties = tuple(
                point for point in window if not _occupied(black, white, point)
            )
            if len(stones) != 4 or len(empties) != 1 or anchor not in stones:
                continue

            completion = empties[0]
            completed = black | (1 << completion)
            if _run_length(completed, completion, direction, size) != 5:
                continue

            patterns.add((direction_index, frozenset(stones)))
            if len(patterns) >= 2:
                return patterns
    return patterns


def _real_three_patterns(
    black: int,
    white: int,
    anchor: int,
    size: int,
    memo: _Memo,
) -> set[_Pattern]:
    """Return real threes made by ``anchor``, stopping once two are known."""

    patterns: set[_Pattern] = set()
    for direction_index, direction in enumerate(_DIRECTIONS):
        dx, dy = direction
        for segment in _windows_containing(anchor, 4, direction, size):
            stones = tuple(point for point in segment if _is_black(black, point))
            empties = tuple(
                point for point in segment if not _occupied(black, white, point)
            )
            if len(stones) != 3 or len(empties) != 1 or anchor not in stones:
                continue

            continuation = empties[0]
            sx, sy = _coords(segment[0], size)
            ex, ey = _coords(segment[-1], size)
            left_xy = (sx - dx, sy - dy)
            right_xy = (ex + dx, ey + dy)
            if not (
                _in_board(*left_xy, size)
                and _in_board(*right_xy, size)
            ):
                continue

            left = _index(*left_xy, size)
            right = _index(*right_xy, size)
            black_after_continuation = black | (1 << continuation)
            if _occupied(black_after_continuation, white, left) or _occupied(
                black_after_continuation, white, right
            ):
                continue

            # Both ends must be winning exact-five continuations for black.
            if _run_length(
                black_after_continuation | (1 << left), left, direction, size
            ) != 5:
                continue
            if _run_length(
                black_after_continuation | (1 << right), right, direction, size
            ) != 5:
                continue

            # A three-making move must itself be legal and non-winning.  This
            # recursive call rejects overline, double-four and *real*
            # double-three continuations, while allowing an apparent
            # double-three containing a false three.
            if _classify(
                black_after_continuation,
                white,
                continuation,
                size,
                memo,
            ) is not BlackMoveKind.LEGAL:
                continue

            patterns.add((direction_index, frozenset(stones)))
            if len(patterns) >= 2:
                return patterns
    return patterns


def _classify(
    black: int,
    white: int,
    anchor: int,
    size: int,
    memo: _Memo,
) -> BlackMoveKind:
    key = (black, white, anchor, size)
    cached = memo.get(key)
    if cached is not None:
        return cached

    run_lengths = tuple(
        _run_length(black, anchor, direction, size)
        for direction in _DIRECTIONS
    )

    # Competition rule precedence: an exact five made by the same move wins
    # even when another direction is forbidden.
    if any(length == 5 for length in run_lengths):
        result = BlackMoveKind.EXACT_FIVE
    elif any(length >= 6 for length in run_lengths):
        result = BlackMoveKind.OVERLINE
    elif len(_four_patterns(black, white, anchor, size)) >= 2:
        result = BlackMoveKind.DOUBLE_FOUR
    elif len(_real_three_patterns(black, white, anchor, size, memo)) >= 2:
        result = BlackMoveKind.DOUBLE_THREE
    else:
        result = BlackMoveKind.LEGAL

    memo[key] = result
    return result


def classify_black_move(
    board: Sequence[Sequence[int]],
    x: int,
    y: int,
    size: int = BOARD_SIZE,
) -> BlackMoveKind:
    """Classify the black move already present at ``(x, y)``.

    The board uses the repository's canonical encoding: ``-1`` empty, ``0``
    black and ``1`` white.  A fresh memo is shared by every hypothetical branch
    of this adjudication.  Invalid board shapes, cells or anchors fail loudly so
    callers cannot accidentally adjudicate an ambiguous position.
    """

    if not _in_board(x, y, size):
        raise ValueError(f"move ({x}, {y}) is outside a {size}x{size} board")

    black, white = _encode_board(board, size)
    anchor = _index(x, y, size)
    if not _is_black(black, anchor):
        raise ValueError(f"move ({x}, {y}) is not a black stone")

    return _classify(black, white, anchor, size, {})


__all__ = [
    "BOARD_SIZE",
    "BlackMoveKind",
    "classify_black_move",
]
