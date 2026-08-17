"""Deterministic public Gomoku match-record export (JSON v1).

The input event list is the platform's canonical public replay projection.
This module never reads persistence rows, Bot binaries, frozen execution
configuration, or the private debug sidecar.
"""
from __future__ import annotations

from typing import Any

from bzplat.backend.games.base import MatchRecordExportError
from bzplat.backend.games.gomoku.gomoku_judge import BOARD_SIZE
from bzplat.backend.games.gomoku.protocol import (
    PROTOCOL_VERSION as CURRENT_EVENT_PROTOCOL_VERSION,
)
from bzplat.backend.store.schema import (
    GOMOKU_CURRENT_PROTOCOL,
    GOMOKU_CURRENT_RULESET,
    GOMOKU_LEGACY_PROTOCOL,
    GOMOKU_LEGACY_RULESET,
)


FORMAT = "botbattle.gomoku.record"
FORMAT_VERSION = 1
_CURRENT_CONTRACT = (GOMOKU_CURRENT_RULESET, GOMOKU_CURRENT_PROTOCOL)
_LEGACY_CONTRACT = (GOMOKU_LEGACY_RULESET, GOMOKU_LEGACY_PROTOCOL)

_MATCH_FIELDS = (
    "id",
    "game_id",
    "ruleset_version",
    "protocol_version",
    "status",
    "winner",
    "reason",
    "match_type",
    "contest_id",
    "human_seat",
    "created_at",
    "started_at",
    "ended_at",
    "technical_loss",
)
_SEAT_FIELDS = (
    "id",
    "name",
    "display_name",
    "owner_name",
    "owner_display",
    "is_human",
)


def _exact_int(value: Any) -> int | None:
    return value if type(value) is int else None


def _algebraic(raw: Any) -> str | None:
    """Translate one internal point using the fixed initial-black view."""
    if not isinstance(raw, dict):
        return None
    x = _exact_int(raw.get("x"))
    y = _exact_int(raw.get("y"))
    if x is None or y is None or not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
        return None
    return f"{chr(ord('A') + x)}{BOARD_SIZE - y}"


def _point(raw: Any) -> tuple[int, int] | None:
    if not isinstance(raw, dict):
        return None
    x = _exact_int(raw.get("x"))
    y = _exact_int(raw.get("y"))
    if x is None or y is None or not (0 <= x < BOARD_SIZE and 0 <= y < BOARD_SIZE):
        return None
    return (x, y)


def _point_algebraic(point: tuple[int, int]) -> str:
    return f"{chr(ord('A') + point[0])}{BOARD_SIZE - point[1]}"


def _seat_number(value: Any) -> int | None:
    seat = _exact_int(value)
    return seat + 1 if seat in (0, 1) else None


def _public_result(raw: Any) -> dict[str, Any]:
    """Keep only the stable public result fields needed by a board record."""
    if not isinstance(raw, dict):
        return {}
    result: dict[str, Any] = {}
    rounds = _exact_int(raw.get("rounds_played"))
    if rounds is not None and rounds >= 0:
        result["rounds_played"] = rounds
    deltas = raw.get("deltas")
    if (
        isinstance(deltas, list)
        and len(deltas) == 2
        and all(type(value) in (int, float) for value in deltas)
    ):
        result["deltas"] = list(deltas)
    normalized = raw.get("normalized_delta")
    if type(normalized) in (int, float):
        result["normalized_delta"] = normalized
    return result


def _public_seat(raw: Any, index: int) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    seat = {
        key: source.get(key)
        for key in _SEAT_FIELDS
        if key in source
    }
    return {"seat": index, "seat_no": index + 1, **seat}


def _opening_stones(event: dict[str, Any]) -> list[dict[str, Any]]:
    stones: list[dict[str, Any]] = []
    for source_field, stone_no, stone_color in (
        ("black1", 1, "black"),
        ("white2", 2, "white"),
        ("black3", 3, "black"),
    ):
        point = _point(event.get(source_field))
        if point is None:
            return []
        stones.append(
            {
                "source_field": source_field,
                "stone_no": stone_no,
                "stone_color": stone_color,
                "algebraic": _point_algebraic(point),
            }
        )
    points = [_point(event.get(stone["source_field"])) for stone in stones]
    if len(set(points)) != 3:
        return []
    return stones


def _contract_generation(match: dict[str, Any], events: list[dict[str, Any]]) -> str:
    if match.get("game_id") != "gomoku":
        raise MatchRecordExportError("record source is not a Gomoku match")
    pair = (match.get("ruleset_version"), match.get("protocol_version"))
    if pair == _CURRENT_CONTRACT:
        generation = "current"
    elif pair == _LEGACY_CONTRACT:
        generation = "legacy"
    else:
        raise MatchRecordExportError("unknown or mixed Gomoku match contract")

    frozen_ruleset, frozen_protocol = pair
    for event in events:
        if event.get("type") != "match_start":
            continue
        if generation == "current":
            # The persisted Match contract uses the stable protocol identifier
            # ``gomoku_action_v2``.  Replay events intentionally carry the
            # wire-format's numeric schema version (currently ``2``), as
            # emitted by the engine.  They are related but not interchangeable.
            expected = {
                "game_id": "gomoku",
                "size": BOARD_SIZE,
                "ruleset": frozen_ruleset,
                "protocol_version": CURRENT_EVENT_PROTOCOL_VERSION,
            }
            if any(event.get(key) != value for key, value in expected.items()):
                raise MatchRecordExportError(
                    "current match_start conflicts with its frozen contract"
                )
        else:
            expected = {
                "game_id": "gomoku",
                "size": BOARD_SIZE,
                "ruleset": frozen_ruleset,
                "protocol_version": frozen_protocol,
            }
            for key, value in expected.items():
                if key in event and event[key] != value:
                    raise MatchRecordExportError(
                        "legacy match_start conflicts with its frozen contract"
                    )

    expected_terminal = {
        "completed": "match_end",
        "aborted": "error",
    }.get(match.get("status"))
    if (
        expected_terminal is None
        or not events
        or events[-1].get("type") != expected_terminal
    ):
        raise MatchRecordExportError("public replay has no matching terminal")
    return generation


def _enrich_events(
    events: list[dict[str, Any]], *, generation: str
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    next_stone_no = 1
    numbering_reliable = True
    opening_established = generation == "legacy"
    legacy_index_base: int | None = None
    occupied: set[tuple[int, int]] = set()
    last_move: tuple[tuple[int, int], int, int] | None = None
    candidate_state: tuple[tuple[tuple[int, int], ...], int] | None = None
    selected_state: tuple[tuple[int, int], int] | None = None

    for event_seq, raw in enumerate(events, start=1):
        # ``events`` already crossed the public replay projection.  Copy each
        # event losslessly, then append only deterministic derived fields.
        event = dict(raw)
        event["event_seq"] = event_seq

        seat_no = _seat_number(event.get("player"))
        if seat_no is None:
            seat_no = _seat_number(event.get("seat"))
        if seat_no is not None:
            event["seat_no"] = seat_no

        event_type = event.get("type")
        if event_type == "opening":
            stones = _opening_stones(event)
            if (
                generation == "current"
                and numbering_reliable
                and not opening_established
                and next_stone_no == 1
                and not occupied
                and len(stones) == 3
            ):
                event["opening_stones"] = stones
                occupied.update(
                    point
                    for field in ("black1", "white2", "black3")
                    if (point := _point(event.get(field))) is not None
                )
                next_stone_no = 4
                opening_established = True
            else:
                # A partial/duplicate/unexpected opening means subsequent
                # stone numbers cannot be reconstructed without guessing.
                numbering_reliable = False
                opening_established = False
                last_move = None
            candidate_state = None
            selected_state = None

        elif event_type == "move":
            point = _point(event)
            if point is not None:
                event["algebraic"] = _point_algebraic(point)

            persisted = _exact_int(event.get("move_index"))
            index_ok = False
            if generation == "current":
                index_ok = persisted == next_stone_no
            elif persisted is not None:
                if legacy_index_base is None:
                    if persisted == next_stone_no - 1:
                        legacy_index_base = 0
                    elif persisted == next_stone_no:
                        legacy_index_base = 1
                if legacy_index_base is not None:
                    index_ok = persisted == next_stone_no - (1 - legacy_index_base)

            if generation == "current":
                # PASS consumes a turn without placing a stone, so color can no
                # longer be inferred from stone-number parity.  Current events
                # carry the authoritative color explicitly (also after swap).
                stone_color = _exact_int(event.get("color"))
                color_ok = stone_color in (0, 1)
            else:
                # Legacy freestyle had no swap: seat 0 was black and seat 1
                # white.  This remains valid across PASS, unlike odd/even stone
                # parity.  If a legacy color field exists it must agree.
                player = _exact_int(event.get("player"))
                stone_color = player if player in (0, 1) else None
                color_ok = bool(
                    stone_color in (0, 1)
                    and (
                        "color" not in event
                        or _exact_int(event.get("color")) == stone_color
                    )
                )
            selection_ok = True
            if generation == "current" and next_stone_no == 5:
                selection_ok = bool(
                    selected_state
                    and selected_state[1] == next_stone_no
                    and selected_state[0] == point
                )
            can_count = bool(
                numbering_reliable
                and opening_established
                and point is not None
                and point not in occupied
                and index_ok
                and color_ok
                and selection_ok
            )
            if can_count:
                stone_no = next_stone_no
                event["stone_no"] = stone_no
                event["stone_color"] = "black" if stone_color == 0 else "white"
                occupied.add(point)
                next_stone_no += 1
                last_move = (point, stone_no, stone_color)
                if stone_no == 5:
                    candidate_state = None
                    selected_state = None
            else:
                # A malformed, duplicate or discontinuous move may represent a
                # missing/corrupt placement.  Keep the public event but stop all
                # later hand-number derivation.
                numbering_reliable = False
                last_move = None
                candidate_state = None
                selected_state = None

        elif event_type == "black5_candidates":
            candidate_state = None
            selected_state = None
            points = event.get("points")
            n = _exact_int(event.get("n"))
            parsed_points = (
                [_point(point) for point in points]
                if isinstance(points, list)
                else []
            )
            complete = bool(
                generation == "current"
                and numbering_reliable
                and opening_established
                and next_stone_no == 5
                and n is not None
                and 2 <= n <= 5
                and len(parsed_points) == n
                and all(point is not None for point in parsed_points)
                and len(set(parsed_points)) == n
                and all(point not in occupied for point in parsed_points)
            )
            if complete:
                exact_points = tuple(point for point in parsed_points if point is not None)
                event["algebraic_points"] = [
                    _point_algebraic(point) for point in exact_points
                ]
                event["candidate_for_stone_no"] = next_stone_no
                candidate_state = (exact_points, next_stone_no)

        elif event_type == "black5_selected":
            selected_state = None
            point = _point(event.get("point"))
            index = _exact_int(event.get("index"))
            if candidate_state is not None:
                candidates, target_stone_no = candidate_state
                selection_ok = bool(
                    target_stone_no == next_stone_no
                    and index is not None
                    and 0 <= index < len(candidates)
                    and point == candidates[index]
                )
                if selection_ok and point is not None:
                    event["algebraic"] = _point_algebraic(point)
                    event["selected_stone_no"] = target_stone_no
                    selected_state = (point, target_stone_no)

        elif event_type == "forbidden":
            point = _point(event)
            if point is not None:
                event["algebraic"] = _point_algebraic(point)
            if (
                point is not None
                and last_move is not None
                and point == last_move[0]
                and last_move[2] == 0
                and (
                    "color" not in event
                    or _exact_int(event.get("color")) == last_move[2]
                )
            ):
                event["stone_no"] = last_move[1]

        elif event_type == "turn":
            algebraic = _algebraic(event.get("last"))
            if algebraic is not None:
                event["last_algebraic"] = algebraic

        elif event_type == "illegal":
            algebraic = _algebraic(event.get("action"))
            if algebraic is not None:
                event["attempted_algebraic"] = algebraic

        elif event_type == "match_end":
            winner_seat_no = _seat_number(event.get("winner"))
            if winner_seat_no is not None:
                event["winner_seat_no"] = winner_seat_no

        enriched.append(event)
    return enriched


def build_record(
    *,
    match: dict[str, Any],
    events: list[dict[str, Any]],
    replay_updated_at: str | None,
) -> dict[str, Any]:
    """Build the stable Gomoku JSON-v1 payload from public-only inputs."""
    generation = _contract_generation(match, events)
    public_match = {
        key: match.get(key)
        for key in _MATCH_FIELDS
        if key in match
    }
    public_match["result"] = _public_result(match.get("result"))

    winner_seat_no = _seat_number(public_match.get("winner"))
    if winner_seat_no is not None:
        public_match["winner_seat_no"] = winner_seat_no
    human_seat_no = _seat_number(public_match.get("human_seat"))
    if human_seat_no is not None:
        public_match["human_seat_no"] = human_seat_no

    public_events = _enrich_events(events, generation=generation)
    return {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "match": public_match,
        "seats": [
            _public_seat(match.get("bot_a"), 0),
            _public_seat(match.get("bot_b"), 1),
        ],
        "coordinate_system": {
            "name": "official_algebraic",
            "perspective": "initial_black",
            "board_size": BOARD_SIZE,
            "x_to_file": "A+x",
            "y_to_rank": "15-y",
        },
        "updated_at": (
            replay_updated_at
            if isinstance(replay_updated_at, str) and len(replay_updated_at) <= 64
            else None
        ),
        "event_count": len(public_events),
        "events": public_events,
    }
