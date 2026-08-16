"""Shared Gomoku v2 responders for backend regression fixtures.

These helpers deliberately live under ``tests``: production bots and the referee
remain the authority, while legacy tests can express their original scenario
without reimplementing the specified-opening state machine in every file.
"""
from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from bzplat.backend.games.gomoku import protocol as proto


def standard_response(
    request: dict[str, Any],
    *,
    swap: bool = False,
    normal_action: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return one legal canonical envelope for the requested v2 phase."""
    phase = request["phase"]
    if phase == proto.PHASE_OPENING:
        payload: dict[str, Any] = {
            "action": proto.ACTION_OPENING,
            "white2": {"x": 7, "y": 8},
            "black3": {"x": 8, "y": 8},
            "n": 2,
        }
    elif phase == proto.PHASE_SWAP:
        payload = {"action": proto.ACTION_SWAP, "swap": swap}
    elif phase == proto.PHASE_WHITE4:
        payload = {"action": proto.ACTION_MOVE, "x": 6, "y": 8}
    elif phase == proto.PHASE_BLACK5_CANDIDATES:
        payload = {
            "action": proto.ACTION_BLACK5_CANDIDATES,
            "points": [{"x": 9, "y": 9}, {"x": 5, "y": 5}],
        }
    elif phase == proto.PHASE_BLACK5_SELECT:
        payload = {"action": proto.ACTION_BLACK5_SELECT, "index": 0}
    elif phase == proto.PHASE_NORMAL:
        payload = normal_action or {"action": proto.ACTION_PASS}
    else:  # pragma: no cover - a new production phase must update fixtures
        raise AssertionError(f"unexpected Gomoku phase: {phase}")
    return {"response": payload}


def seat_zero_winning_decider() -> Callable[
    [int, dict[str, Any]], Awaitable[dict[str, Any]]
]:
    """Build a no-swap responder whose seat 0 completes an exact diagonal five."""
    normal_moves = {
        1: iter(((0, 0), (0, 1))),
        0: iter(((10, 10), (11, 11))),
    }

    async def decide(seat: int, request: dict[str, Any]) -> dict[str, Any]:
        if request["phase"] != proto.PHASE_NORMAL:
            return standard_response(request)
        x, y = next(normal_moves[seat])
        return {
            "response": {"action": proto.ACTION_MOVE, "x": x, "y": y}
        }

    return decide


ILLEGAL_OPENING_ENVELOPE = {
    "response": {
        "action": proto.ACTION_OPENING,
        "white2": {"x": 999, "y": 999},
        "black3": {"x": 998, "y": 999},
        "n": 2,
    }
}
ILLEGAL_OPENING_LINE = json.dumps(
    ILLEGAL_OPENING_ENVELOPE, separators=(",", ":")
)
