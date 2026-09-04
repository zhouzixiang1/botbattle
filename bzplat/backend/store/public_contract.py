"""Public match/event projection shared by REST, SSE and human WebSocket.

Internal logs may retain diagnostic text. Public transports expose only stable
reason codes and bounded, translated technical-incident fields.
"""
from __future__ import annotations

import json
import math
import re
from typing import Any

from .schema import (
    PUBLIC_MATCH_COMPLETED_REASONS,
    PUBLIC_MATCH_ERROR_FALLBACK,
    PUBLIC_MATCH_ERROR_REASONS,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TECHNICAL_INCIDENT_EVENT,
    TECHNICAL_INCIDENT_MESSAGES,
    EXECUTION_ENVIRONMENTS,
    EXECUTION_ENV_HUMAN,
    EXECUTION_ENV_PLATFORM_LOW,
    TYPE_HUMAN,
)

HISTORICAL_TECHNICAL_INCIDENT_EVENTS = frozenset(
    {"bot_decide_error", "bot_technical_error"}
)
READ_TECHNICAL_INCIDENT_EVENTS = (
    HISTORICAL_TECHNICAL_INCIDENT_EVENTS | {TECHNICAL_INCIDENT_EVENT}
)

# Stage-result payloads are durable internal envelopes.  Public contest views
# expose only this stable ranking projection, never arbitrary future payload
# keys.  Keep the order canonical so pre-completion and persisted rows serialize
# identically and clients do not need to know the storage envelope.
PUBLIC_CONTEST_TIEBREAK_FIELDS = (
    "points",
    "buchholz",
    "buchholz_cut1",
    "sonneborn_berger",
    "head_to_head",
    "normalized_delta",
    "technical_losses",
    "seed",
)

# Present only for the two code-owned random-group formats.  They form one
# atomic public contract: exposing a partial rate chain would make a damaged
# snapshot look rankable and would let detail/live disagree with official
# results.  Historical non-grouped rows legitimately omit the whole set.
PUBLIC_CROSS_GROUP_TIEBREAK_FIELDS = (
    "group_rank",
    "points_rate",
    "opponent_strength",
    "normalized_delta_rate",
    "technical_loss_rate",
    "draw_order",
)

# Replay/live events are a public protocol, not an arbitrary JSON transport.
# Keep the complete set here so a future game/adapter must deliberately extend
# the public projection before a new event type or field can cross REST/SSE/WS.
_PUBLIC_EVENT_FIELDS: dict[str, frozenset[str]] = {
    "match_start": frozenset(
        {
            "game_id",
            "num_hands",
            "n_dots",
            "size",
            "first",
            "scores",
            "leg",
            "ruleset",
            "protocol_version",
            "time_budget_per_side",
            "time_control",
        }
    ),
    "turn": frozenset(
        {"player", "color", "phase", "last", "pass_", "pass_allowed", "scores", "leg"}
    ),
    "move": frozenset(
        {
            "player",
            "color",
            "phase",
            "selected_by",
            "x",
            "y",
            "move_index",
            "scored",
            "scores",
            "closed_boxes",
            "leg",
        }
    ),
    "illegal": frozenset({"player", "phase", "action", "why", "leg"}),
    "pass": frozenset({"player", "color", "move_index", "leg"}),
    "opening": frozenset(
        {"player", "opening_code", "n", "black1", "white2", "black3"}
    ),
    "swap": frozenset({"player", "swapped", "seat_colors"}),
    "black5_candidates": frozenset({"player", "n", "points"}),
    "black5_selected": frozenset({"player", "index", "point"}),
    "forbidden": frozenset(
        {"player", "color", "x", "y", "forbidden_kind"}
    ),
    "hand_start": frozenset({"hand", "sb", "bb", "chips", "leg"}),
    "deal_hole": frozenset({"hand", "holes", "leg"}),
    "action": frozenset({"hand", "player", "action", "amount", "leg"}),
    "deal_board": frozenset({"hand", "street", "board", "dealt", "leg"}),
    "settle": frozenset(
        {"hand", "winners", "deltas", "chips", "net", "pot", "board", "reason", "leg"}
    ),
    "time_out": frozenset({"seat", "used", "budget", "leg"}),
    "time_used": frozenset({"seat", "used", "remaining", "budget", "leg"}),
    "your_turn": frozenset({"player", "request", "leg"}),
}
_PUBLIC_NESTED_EVENT_FIELDS = frozenset(
    {"x", "y", "owner", "round", "player_id", "action", "action_type"}
)
_PUBLIC_REQUEST_FIELDS = frozenset(
    {
        "x",
        "y",
        "pass",
        "me",
        "scores",
        "num_players",
        "dealer_id",
        "my_id",
        "my_chips",
        "my_cards",
        "public_cards",
        "history",
        "hand",
        "max_hand",
        "total_win_chips",
        "total_win_games",
        "protocol_version",
        "ruleset",
        "phase",
        "color",
        "seat_colors",
        "board",
        "pass_allowed",
        "fixed_black1",
        "n_range",
        "n",
        "candidates",
        "last",
    }
)


def canonical_public_error_reason(raw: Any) -> str:
    """Return one allowed public terminal-error reason without text inference."""
    reason = str(raw or "").strip()
    if reason in PUBLIC_MATCH_ERROR_REASONS:
        return reason
    return PUBLIC_MATCH_ERROR_FALLBACK


def canonical_public_completed_reason(raw: Any) -> str:
    """Return one allowed completion reason; hide every free-form value."""
    reason = str(raw or "").strip()
    if reason in PUBLIC_MATCH_COMPLETED_REASONS:
        return reason
    return "completed"


def _public_number(raw: Any) -> int | float | None:
    """Return one finite JSON number; booleans are not result numbers."""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    if isinstance(raw, float) and not math.isfinite(raw):
        return None
    return raw


def sanitize_public_contest_tiebreaks(raw: Any) -> dict[str, int | float] | None:
    """Return one complete allow-listed contest tie-break projection.

    New stage snapshots always persist all fields.  Empty legacy payloads and
    malformed/imported history intentionally return ``None`` so presentation
    can omit the detail instead of guessing values or leaking envelope fields.
    """
    if not isinstance(raw, dict):
        return None
    public: dict[str, int | float] = {}
    for key in PUBLIC_CONTEST_TIEBREAK_FIELDS:
        value = raw.get(key)
        if key in {"technical_losses", "seed"}:
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 0
            ):
                return None
            public[key] = value
            continue
        number = _public_number(value)
        if number is None:
            return None
        public[key] = number
    present = [key in raw for key in PUBLIC_CROSS_GROUP_TIEBREAK_FIELDS]
    if any(present):
        if not all(present):
            return None
        for key in ("group_rank", "draw_order"):
            value = raw.get(key)
            if (
                isinstance(value, bool)
                or not isinstance(value, int)
                or value < 1
            ):
                return None
            public[key] = value
        for key in (
            "points_rate",
            "opponent_strength",
            "normalized_delta_rate",
            "technical_loss_rate",
        ):
            number = _public_number(raw.get(key))
            if number is None:
                return None
            if key in {
                "points_rate",
                "opponent_strength",
                "technical_loss_rate",
            } and not 0 <= number <= 1:
                return None
            public[key] = number
    return public


def sanitize_public_stage_result_payload(raw: Any) -> dict[str, Any]:
    """Parse a persisted stage envelope and retain bounded ranking fields."""
    payload = raw
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    if not isinstance(payload, dict):
        return {}
    tiebreaks = sanitize_public_contest_tiebreaks(payload.get("tiebreaks"))
    if tiebreaks is None:
        return {}
    public: dict[str, Any] = {"tiebreaks": tiebreaks}
    if "overall_rank" in payload:
        overall_rank = payload.get("overall_rank")
        if (
            isinstance(overall_rank, bool)
            or not isinstance(overall_rank, int)
            or overall_rank < 1
        ):
            return {}
        public["overall_rank"] = overall_rank
    return public


def _public_deltas(raw: Any) -> list[int | float] | None:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        return None
    first = _public_number(raw[0])
    second = _public_number(raw[1])
    if first is None or second is None:
        return None
    return [first, second]


def _public_event_value(raw: Any, *, nested: bool = False) -> Any:
    """Return bounded JSON data for one already allow-listed event field."""
    if raw is None or isinstance(raw, bool):
        return raw
    number = _public_number(raw)
    if number is not None:
        return number
    if isinstance(raw, str):
        # Public event strings are short codes, card labels, or street names.
        # Reject diagnostic prose instead of truncating it into a second truth.
        return raw if len(raw) <= 64 else None
    if isinstance(raw, (list, tuple)):
        return [
            value
            for item in list(raw)[:512]
            if (value := _public_event_value(item, nested=True)) is not None
        ]
    if isinstance(raw, dict) and nested:
        return {
            key: value
            for key, item in raw.items()
            if key in _PUBLIC_NESTED_EVENT_FIELDS
            if (value := _public_event_value(item, nested=True)) is not None
        }
    return None


def _sanitize_public_request(raw: Any) -> dict[str, Any] | None:
    """Project a human-turn request onto the three canonical game payloads."""
    if not isinstance(raw, dict):
        return None
    public: dict[str, Any] = {}
    for key in _PUBLIC_REQUEST_FIELDS:
        if key not in raw:
            continue
        value = _public_event_value(raw[key], nested=True)
        if value is not None:
            public[key] = value
    return public


def _sanitize_public_time_control(
    raw: Any, *, game_id: Any = None
) -> dict[str, Any] | None:
    """Return the bounded public clock contract embedded in match_start."""

    if not isinstance(raw, dict):
        return None
    control_id = raw.get("id")
    mode = raw.get("mode")
    seconds = raw.get("seconds")
    applies_to = raw.get("applies_to")
    if (
        not isinstance(control_id, str)
        or re.fullmatch(
            r"[a-z0-9]+(?:_[a-z0-9]+)*_v[1-9][0-9]*", control_id
        ) is None
        or mode not in {"per_decision", "per_side_total"}
        or isinstance(seconds, bool)
        or not isinstance(seconds, int)
        or seconds <= 0
        or applies_to not in {"both_bots", "bot_only"}
    ):
        return None
    try:
        from bzplat.backend.games import registry as game_registry

        if not isinstance(game_id, str) or not game_id:
            return None
        frozen = game_registry.get(game_id).resolve_time_control(control_id)
    except (KeyError, TypeError, ValueError):
        return None
    if frozen.mode != mode or frozen.seconds != seconds:
        return None
    return {
        "id": control_id,
        "mode": mode,
        "seconds": seconds,
        "applies_to": applies_to,
    }


def sanitize_public_result(raw: Any) -> dict[str, Any]:
    """Project persisted results onto the fields consumed by public clients."""
    if not isinstance(raw, dict):
        return {}
    public: dict[str, Any] = {}

    rounds_played = raw.get("rounds_played")
    if isinstance(rounds_played, int) and not isinstance(rounds_played, bool):
        public["rounds_played"] = max(0, rounds_played)

    deltas = _public_deltas(raw.get("deltas"))
    if deltas is not None:
        public["deltas"] = deltas

    normalized_delta = _public_number(raw.get("normalized_delta"))
    if normalized_delta is not None:
        public["normalized_delta"] = normalized_delta

    raw_legs = raw.get("legs")
    if isinstance(raw_legs, list):
        legs: list[dict[str, Any]] = []
        for raw_leg in raw_legs:
            if not isinstance(raw_leg, dict):
                continue
            leg_deltas = _public_deltas(raw_leg.get("deltas"))
            if leg_deltas is None:
                continue
            winner = raw_leg.get("winner")
            if winner is not None and (
                isinstance(winner, bool)
                or not isinstance(winner, int)
                or winner not in (0, 1)
            ):
                continue
            leg = {"winner": winner, "deltas": leg_deltas}
            rounds_played = raw_leg.get("rounds_played")
            if (
                isinstance(rounds_played, int)
                and not isinstance(rounds_played, bool)
                and rounds_played >= 0
            ):
                leg["rounds_played"] = rounds_played
            legs.append(leg)
        if legs:
            public["legs"] = legs

    raw_counts = raw.get("technical_incidents_by_seat")
    if isinstance(raw_counts, dict):
        counts: dict[str, int] = {}
        for seat in (0, 1):
            value = raw_counts.get(str(seat), raw_counts.get(seat))
            if isinstance(value, int) and not isinstance(value, bool):
                counts[str(seat)] = max(0, value)
        if counts:
            public["technical_incidents_by_seat"] = counts

    count = raw.get("technical_incident_count")
    if isinstance(count, int) and not isinstance(count, bool) and count > 0:
        public["technical_incident_count"] = count

    samples = raw.get("technical_incident_samples")
    if isinstance(samples, list):
        safe_samples = []
        for sample in samples:
            safe = sanitize_public_incident(sample)
            if safe is not None:
                safe_samples.append(safe)
            if len(safe_samples) == 3:
                break
        if safe_samples:
            public["technical_incident_samples"] = safe_samples
    return public


def sanitize_public_match(match: dict | None) -> dict | None:
    """Copy one match and expose only public result/config semantics."""
    if match is None:
        return None
    public = dict(match)
    # Rows created before execution environments were frozen all used the
    # original low-resource platform sandbox. Preserve that historical fact
    # instead of leaving the UI blank or guessing that an old contest was high
    # performance. A historical human seat remains human.
    environments = [EXECUTION_ENV_PLATFORM_LOW, EXECUTION_ENV_PLATFORM_LOW]
    if public.get("match_type") == TYPE_HUMAN:
        human_seat = public.get("human_seat")
        if human_seat in (0, 1) and not isinstance(human_seat, bool):
            environments[int(human_seat)] = EXECUTION_ENV_HUMAN
    raw_config = public.get("match_config")
    # Store parsing preserves a private marker when invalid JSON had to be
    # replaced with an empty dict.  Do not reinterpret that replacement as a
    # genuine legacy row, and never expose the marker itself.
    config_malformed = bool(public.pop("_match_config_malformed", False))
    if isinstance(raw_config, str):
        try:
            raw_config = json.loads(raw_config)
        except (TypeError, ValueError):
            raw_config = None
            config_malformed = True
    elif raw_config is not None and not isinstance(raw_config, dict):
        raw_config = None
        config_malformed = True
    if isinstance(raw_config, dict):
        for side in ("a", "b"):
            value = raw_config.get(f"_bot_{side}_environment")
            if value in EXECUTION_ENVIRONMENTS:
                environments[0 if side == "a" else 1] = value
    if config_malformed:
        public["time_control"] = None
    else:
        try:
            from bzplat.backend.games import registry as game_registry

            spec = game_registry.get(str(public.get("game_id") or ""))
            raw_id = (
                raw_config.get("time_control_id")
                if isinstance(raw_config, dict)
                else None
            )
            if isinstance(raw_config, dict) and "time_control_id" in raw_config and (
                not isinstance(raw_id, str) or not raw_id
            ):
                raise ValueError("invalid time_control_id")
            control = spec.resolve_time_control(raw_id)
            public["time_control"] = {
                "id": control.id,
                "mode": control.mode,
                "seconds": control.seconds,
                "applies_to": (
                    "bot_only"
                    if public.get("match_type") == TYPE_HUMAN
                    else "both_bots"
                ),
            }
        except (KeyError, TypeError, ValueError):
            public["time_control"] = None
    public["bot_a_environment"] = environments[0]
    public["bot_b_environment"] = environments[1]
    # match_config stores frozen version ids and duplicate seeds for execution;
    # it is not part of the public match contract.
    public.pop("match_config", None)
    # Reserved order/status are internal sequencing evidence.  Public callers
    # get only the marker-backed boolean and derive presentation from match status.
    public.pop("rating_settled_order", None)
    public.pop("_rating_settled_order", None)
    public.pop("rating_settlement_status", None)
    if "result" in public:
        public["result"] = sanitize_public_result(public.get("result"))
    status = public.get("status")
    if status in {STATUS_PENDING, STATUS_RUNNING}:
        # Active rows have no adjudicated result. Historical/default/free-form
        # values are never meaningful to a viewer and may contain diagnostics.
        public["reason"] = ""
    elif status == STATUS_COMPLETED:
        public["reason"] = canonical_public_completed_reason(public.get("reason"))
    elif status == STATUS_ABORTED:
        public["reason"] = canonical_public_error_reason(public.get("reason"))
    return public


def sanitize_public_incident(raw: Any) -> dict[str, Any] | None:
    """Normalize one technical incident without exposing raw Bot output/paths."""
    if not isinstance(raw, dict):
        return None
    try:
        seat = int(raw.get("seat"))
    except (TypeError, ValueError):
        return None
    if seat not in (0, 1):
        return None
    sample: dict[str, Any] = {"seat": seat}
    code = raw.get("code")
    if isinstance(code, str) and code in TECHNICAL_INCIDENT_MESSAGES:
        sample["code"] = code
        sample["error"] = TECHNICAL_INCIDENT_MESSAGES[code]
    else:
        sample["error"] = "Bot 响应格式错误（历史记录）"
    reason = raw.get("reason")
    if reason in ("protocol_error", "timeout", "technical_loss"):
        sample["reason"] = reason
    for key in ("turn", "leg"):
        value = raw.get(key)
        if value is None:
            continue
        try:
            sample[key] = int(value)
        except (TypeError, ValueError):
            continue
    return sample


def sanitize_public_event(
    raw: Any,
    *,
    redact_active_human: bool = False,
    human_viewer_seat: int | None = None,
) -> dict[str, Any] | None:
    """Copy one canonical public event, stripping all private error metadata."""
    if not isinstance(raw, dict):
        return None
    event_type = raw.get("type")
    if event_type == "error":
        return {
            "type": "error",
            "reason": canonical_public_error_reason(raw.get("reason")),
        }
    if event_type == "match_end":
        winner = raw.get("winner")
        if winner is not None and (
            isinstance(winner, bool)
            or not isinstance(winner, int)
            or winner not in (0, 1)
        ):
            winner = None
        public_terminal: dict[str, Any] = {
            "type": "match_end",
            "winner": winner,
            "reason": canonical_public_completed_reason(raw.get("reason")),
            "deltas": _public_deltas(raw.get("deltas")) or [0, 0],
        }
        leg = raw.get("leg")
        if isinstance(leg, int) and not isinstance(leg, bool) and leg >= 0:
            public_terminal["leg"] = leg
        return public_terminal
    if event_type in READ_TECHNICAL_INCIDENT_EVENTS:
        sample = sanitize_public_incident(raw)
        if sample is None:
            return None
        return {"type": TECHNICAL_INCIDENT_EVENT, **sample}
    allowed = _PUBLIC_EVENT_FIELDS.get(event_type)
    if allowed is None:
        # Unknown/diagnostic events are internal by default. A new public event
        # must add an explicit field projection above and corresponding tests.
        return None
    public: dict[str, Any] = {"type": event_type}
    for key in allowed:
        if key not in raw:
            continue
        if key == "request":
            request = _sanitize_public_request(raw[key])
            if request is not None:
                public[key] = request
            continue
        if key == "time_control":
            control = _sanitize_public_time_control(
                raw[key], game_id=raw.get("game_id")
            )
            if control is not None:
                public[key] = control
            continue
        value = _public_event_value(raw[key], nested=True)
        if value is not None:
            public[key] = value
    if redact_active_human and event_type == "deal_hole":
        holes = public.get("holes")
        redacted: list[list[Any]] = [[], []]
        if (
            human_viewer_seat in (0, 1)
            and isinstance(holes, list)
            and len(holes) > human_viewer_seat
            and isinstance(holes[human_viewer_seat], list)
        ):
            redacted[human_viewer_seat] = list(holes[human_viewer_seat])
        public["holes"] = redacted
    if redact_active_human and event_type == "your_turn":
        if human_viewer_seat not in (0, 1) or public.get("player") != human_viewer_seat:
            return None
    return public


def sanitize_public_event_prefix(
    raw_events: Any,
    *,
    redact_active_human: bool = False,
    human_viewer_seat: int | None = None,
    expected_time_control: dict[str, Any] | None = None,
    expected_game_id: str | None = None,
) -> list[dict[str, Any]]:
    """Project one non-terminal replay prefix onto the public event contract.

    Persisted replays and the orchestrator's in-memory running prefix must cross
    the same boundary. Engine/historical terminal events are excluded because
    the authoritative match row contributes exactly one terminal after commit;
    technical-incident samples stay bounded to the public result limit.
    """
    if not isinstance(raw_events, list):
        return []
    sanitized: list[dict[str, Any]] = []
    incident_samples = 0
    for event in raw_events:
        if not isinstance(event, dict):
            continue
        if event.get("type") in {"match_end", "error"}:
            continue
        if event.get("type") in READ_TECHNICAL_INCIDENT_EVENTS:
            incident_samples += 1
            if incident_samples > 3:
                continue
        public_event = sanitize_public_event(
            event,
            redact_active_human=redact_active_human,
            human_viewer_seat=human_viewer_seat,
        )
        if public_event is not None:
            authority_supplied = (
                expected_game_id is not None
                or isinstance(expected_time_control, dict)
            )
            if public_event.get("type") == "match_start" and authority_supplied:
                event_game_id = public_event.get("game_id")
                binding_game_id = expected_game_id or (
                    event_game_id if isinstance(event_game_id, str) else None
                )
                game_matches = (
                    expected_game_id is None
                    or event_game_id is None
                    or event_game_id == expected_game_id
                )
                bounded_expected = (
                    _sanitize_public_time_control(
                        expected_time_control,
                        game_id=binding_game_id,
                    )
                    if game_matches and isinstance(expected_time_control, dict)
                    else None
                )
                if "time_control" not in event:
                    # Historical events predate the public clock object.  The
                    # Match's frozen config is the authoritative legacy-default
                    # interpretation, so inject exactly that bounded projection.
                    if bounded_expected is not None:
                        public_event["time_control"] = bounded_expected
                    else:
                        # Authority exists but is unusable or contradicts the
                        # event's game.  A present null is the bounded damaged
                        # sentinel consumed by board reducers; omitting the key
                        # would misclassify this as legacy and let clock-event
                        # budget fields invent a valid-looking control.
                        public_event["time_control"] = None
                else:
                    bounded_stored = (
                        _sanitize_public_time_control(
                            event.get("time_control"),
                            game_id=binding_game_id,
                        )
                        if game_matches
                        else None
                    )
                    if (
                        bounded_stored is not None
                        and bounded_stored == bounded_expected
                    ):
                        public_event["time_control"] = bounded_stored
                    else:
                        # An explicit stored clock that contradicts the Match
                        # is damaged evidence. Do not overwrite it into
                        # apparent consistency or omit it into the legacy
                        # fallback path.
                        public_event["time_control"] = None
            sanitized.append(public_event)
    return sanitized
