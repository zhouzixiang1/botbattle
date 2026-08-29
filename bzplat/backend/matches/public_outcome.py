"""Small, replay-free public projection of one adjudicated Match.

The persisted ``result`` JSON remains an internal execution contract.  Contest
and match list UIs need a much smaller semantic object: independently scored
games, their scoreline, and a bounded technical-terminal marker.  Keeping the
parser here also gives standings and ranking one authority for duplicate legs.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
import json
import math
from numbers import Integral, Real
from typing import Any, Callable

from bzplat.backend.games.base import GameSpec
from bzplat.backend.matches.result_contract import canonical_deltas
from bzplat.backend.store.public_contract import canonical_public_completed_reason
from bzplat.backend.store.schema import (
    PUBLIC_MATCH_COMPLETED_REASONS,
    PUBLIC_MATCH_ERROR_REASONS,
    STATUS_COMPLETED,
    TECHNICAL_MATCH_COMPLETED_REASONS,
)


@dataclass(frozen=True)
class ScoringGame:
    """One result that contributes directly to standings W/D/L and points."""

    index: int
    winner: int | None
    deltas: tuple[int, int] | None
    rounds_played: int | None


def _result_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (TypeError, ValueError):
            return {}
    return raw if isinstance(raw, dict) else {}


def _winner(raw: Any) -> int | None | object:
    if raw is None:
        return None
    # Equality membership is not a type check in Python: ``0.0 in (0, 1)`` is
    # true.  Persisted JSON seat winners are exact integers; floats, strings
    # and bools are malformed and must be rejected by every scoring consumer.
    if (
        isinstance(raw, bool)
        or not isinstance(raw, Integral)
        or int(raw) not in (0, 1)
    ):
        return _INVALID
    return int(raw)


def _rounds(raw: Any) -> int | None:
    if isinstance(raw, bool) or not isinstance(raw, Integral):
        return None
    value = int(raw)
    return value if value >= 0 else None


def _deltas(raw: Any) -> tuple[int, int] | None:
    try:
        values = canonical_deltas(raw)
    except (TypeError, ValueError):
        return None
    return values[0], values[1]


def _winner_matches_deltas(
    winner: int | None, deltas: tuple[int, int]
) -> bool:
    """Reject contradictory adjudications before any standings consumer sees them.

    The platform result contract is zero-sum: seat 0 wins only with a positive
    seat-0 delta, seat 1 wins only with a negative one, and an ordinary draw is
    exactly zero.  A duplicate's *top-level* ``winner=None`` is deliberately not
    checked here because it means "no aggregate winner"; each independent
    scoring game is checked instead.
    """
    if winner is None:
        return deltas[0] == 0
    return deltas[0] > 0 if winner == 0 else deltas[0] < 0


_INVALID = object()


def _technical_flag(raw: Any) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, Integral):
        return int(raw) == 1
    return False


def _valid_technical_flag(raw: Any) -> bool:
    """Accept SQLite's boolean wire while rejecting ambiguous truthy values."""
    return raw is None or isinstance(raw, bool) or (
        isinstance(raw, Integral) and not isinstance(raw, bool) and int(raw) in (0, 1)
    )


def _technical_reason_matches_flag(match: dict[str, Any], technical: bool) -> bool:
    """Fail closed on an explicit terminal-reason/technical-flag conflict.

    Blank reasons predate the stable reason code and remain readable.  Once a
    reason is present, however, the finite technical-only set and the durable
    flag must agree in both directions; otherwise standings, lifecycle gates,
    and public outcome would assign different semantics to the same Match.
    """
    raw_reason = match.get("reason")
    if raw_reason is None:
        return True
    if not isinstance(raw_reason, str):
        return False
    reason = raw_reason.strip()
    if not reason:
        return True
    if (
        reason in PUBLIC_MATCH_ERROR_REASONS
        and reason not in PUBLIC_MATCH_COMPLETED_REASONS
    ):
        # Stable platform/abort terminal codes can only describe an aborted
        # Match.  A damaged row marked completed must not be awarded points or
        # allowed to advance a contest.  Unknown old free-form referee reasons
        # remain readable for backward compatibility.
        return False
    return (reason in TECHNICAL_MATCH_COMPLETED_REASONS) is technical


def _technical_scoring_game_index(
    result: dict[str, Any], *, planned_games: int
) -> int | object:
    """Resolve the physical 1-based game adjudicated by a technical terminal.

    The replay is intentionally not read by metadata endpoints.  Current
    results persist an explicit index; older protocol/timeout results already
    carry the same 0-based leg in their bounded incident sample.  Pre-dispatch
    and older records without either marker necessarily refer to game 1.
    """
    explicit = result.get("technical_game_index")
    resolved: int | None = None
    if explicit is not None:
        if (
            isinstance(explicit, bool)
            or not isinstance(explicit, Integral)
            or not 1 <= int(explicit) <= planned_games
        ):
            return _INVALID
        resolved = int(explicit)
    samples = result.get("technical_incident_samples")
    if samples is not None:
        if not isinstance(samples, list):
            return _INVALID
        sample_indexes: set[int] = set()
        for sample in samples:
            if not isinstance(sample, dict) or "leg" not in sample:
                continue
            leg = sample.get("leg")
            if (
                isinstance(leg, bool)
                or not isinstance(leg, Integral)
                or not 0 <= int(leg) < planned_games
            ):
                return _INVALID
            sample_indexes.add(int(leg) + 1)
        if len(sample_indexes) > 1:
            return _INVALID
        if sample_indexes:
            sampled = next(iter(sample_indexes))
            if resolved is not None and sampled != resolved:
                return _INVALID
            resolved = sampled
    return resolved or 1


def _frozen_duplicate_mode(
    match: dict[str, Any], *, require_explicit: bool
) -> bool | None:
    """Read the frozen flag with optional strict-v1 explicitness."""
    if match.get("_match_config_malformed"):
        return None
    config = match.get("match_config")
    if config is None:
        return None if require_explicit else False
    if isinstance(config, str):
        try:
            config = json.loads(config)
        except (TypeError, ValueError):
            return None
    if not isinstance(config, dict):
        return None
    if "duplicate" not in config:
        return None if require_explicit else False
    value = config.get("duplicate")
    return value if isinstance(value, bool) else None


def is_duplicate_match(match: dict[str, Any]) -> bool | None:
    """Read the legacy-compatible execution flag, or ``None`` if malformed.

    Missing/NULL configuration and an object without ``duplicate`` predate the
    flag and remain ordinary single-game Matches.  Once the field is present it
    must be a JSON boolean; treating ``1``/``"false"``/bad JSON as false would
    let generic Match pages disagree with strict contest projections.
    """
    return _frozen_duplicate_mode(match, require_explicit=False)


def frozen_duplicate_mode(
    match: dict[str, Any], *, require_explicit: bool = False
) -> bool | None:
    """Shared execution/read parser for the persisted duplicate flag.

    Runtime code must use the same exact-boolean rule as public outcome and
    standings.  Strict independent contests additionally require the field to
    exist so a missing execution contract cannot be guessed from truthiness.
    """
    return _frozen_duplicate_mode(match, require_explicit=require_explicit)


@lru_cache(maxsize=None)
def _duplicate_plan_size(
    game_id: str,
    ruleset_id: str,
    build_match_plan: Any,
) -> int:
    plan = build_match_plan(0, {"duplicate": True})
    if not isinstance(plan, list) or not plan:
        raise ValueError(f"游戏 {game_id} 的 duplicate 对局计划为空")
    return len(plan)


def planned_match_games(spec: GameSpec, *, duplicate: bool) -> int:
    if not isinstance(duplicate, bool):
        raise ValueError("duplicate 必须是布尔值")
    if not duplicate:
        return 1
    if spec.build_match_plan is None:
        raise ValueError(f"游戏 {spec.game_id} 不支持 duplicate 对局")
    return _duplicate_plan_size(
        spec.game_id, spec.ruleset_id, spec.build_match_plan
    )


def scoring_games_for_match(
    match: dict[str, Any] | None,
    *,
    duplicate: bool,
    planned_games: int,
    fixed_rounds_per_match: int | None = None,
    require_frozen_duplicate: bool = False,
    normalize_delta: Callable[[int], Real] | None = None,
) -> tuple[ScoringGame, ...]:
    """Return authoritative standings records for a completed physical Match.

    A normal duplicate must carry its independent ``legs``.  A technical
    terminal without legs contributes exactly one top-level adjudication, as
    required by the historical standings/ranking contract; it never invents
    the unplayed second game.
    """
    if (
        not isinstance(duplicate, bool)
        or not match
        or match.get("status") != STATUS_COMPLETED
    ):
        return ()
    frozen_duplicate = _frozen_duplicate_mode(
        match, require_explicit=require_frozen_duplicate
    )
    if frozen_duplicate is None:
        return ()
    if not require_frozen_duplicate:
        explicit_duplicate = _frozen_duplicate_mode(
            match, require_explicit=True
        )
        if explicit_duplicate is not None and explicit_duplicate != duplicate:
            # Legacy rows may omit the flag and use the stage as authority, but
            # an explicit frozen Match value is not optional evidence.  A
            # disagreement must fail closed for aggregate and pre-marker
            # histories just as it does for strict-v1.
            return ()
    if require_frozen_duplicate and frozen_duplicate != duplicate:
        # The stage is an expected contract, while match_config is the frozen
        # execution contract.  A disagreement is corrupt history and must be
        # rejected by standings, ranking, outcome and lifecycle alike.  Older
        # in-memory/direct-ranking callers legitimately have no frozen config;
        # ``None`` therefore keeps the historical parser-compatible path.
        return ()
    if (
        isinstance(planned_games, bool)
        or not isinstance(planned_games, Integral)
        or int(planned_games) < 1
        or (not duplicate and int(planned_games) != 1)
    ):
        return ()
    planned_games = int(planned_games)
    if not _valid_technical_flag(match.get("technical_loss")):
        return ()
    technical = _technical_flag(match.get("technical_loss"))
    if not _technical_reason_matches_flag(match, technical):
        return ()
    result = _result_dict(match.get("result"))
    raw_legs = result.get("legs")
    # A single Match has exactly one top-level adjudication.  ``legs`` is a
    # duplicate-only execution contract; accepting it here would let malformed
    # payloads silently inflate standings.
    if not duplicate and "legs" in result:
        return ()
    if duplicate and isinstance(raw_legs, list) and raw_legs:
        if (technical and len(raw_legs) != 1) or (
            not technical and len(raw_legs) != planned_games
        ):
            return ()
        top_winner = _winner(match.get("winner"))
        if (
            top_winner is _INVALID
            or (technical and top_winner is None)
            or (not technical and top_winner is not None)
        ):
            return ()
        technical_index = (
            _technical_scoring_game_index(result, planned_games=planned_games)
            if technical
            else None
        )
        if technical_index is _INVALID:
            return ()
        games: list[ScoringGame] = []
        for index, raw_leg in enumerate(raw_legs, start=1):
            if not isinstance(raw_leg, dict):
                return ()
            winner = _winner(raw_leg.get("winner"))
            deltas = _deltas(raw_leg.get("deltas"))
            if (
                winner is _INVALID
                or deltas is None
                or (not technical and not _winner_matches_deltas(winner, deltas))
                or (
                    technical
                    and require_frozen_duplicate
                    and deltas != (0, 0)
                )
            ):
                return ()
            rounds_played = _rounds(raw_leg.get("rounds_played"))
            if rounds_played is None:
                if "rounds_played" in raw_leg or technical:
                    return ()
                rounds_played = fixed_rounds_per_match
            if fixed_rounds_per_match is not None and (
                (technical and int(rounds_played) > fixed_rounds_per_match)
                or (not technical and int(rounds_played) != fixed_rounds_per_match)
            ):
                return ()
            games.append(
                ScoringGame(
                    index=(int(technical_index) if technical else index),
                    winner=winner,
                    deltas=deltas,
                    rounds_played=rounds_played,
                )
            )
        total_deltas = _deltas(result.get("deltas"))
        total_rounds = _rounds(result.get("rounds_played"))
        if total_deltas is None or total_rounds is None:
            return ()
        if total_deltas != (
            sum(game.deltas[0] for game in games if game.deltas is not None),
            sum(game.deltas[1] for game in games if game.deltas is not None),
        ):
            return ()
        if normalize_delta is not None and (
            normalized_delta_value(normalize_delta, total_deltas[0]) is None
            or any(
                game.deltas is None
                or normalized_delta_value(normalize_delta, game.deltas[0]) is None
                for game in games
            )
        ):
            return ()
        if any(game.rounds_played is None for game in games) or total_rounds != sum(
            int(game.rounds_played or 0) for game in games
        ):
            return ()
        if technical and games[0].winner != top_winner:
            return ()
        return tuple(games)

    if duplicate and raw_legs is not None:
        # Empty, non-list, and otherwise malformed duplicate leg containers
        # are never reinterpreted as a top-level game.
        return ()
    if duplicate and not technical:
        # ``winner=None`` on a normal duplicate means there is no aggregate
        # winner.  Without legs it is incomplete data, never a draw.
        return ()
    winner = _winner(match.get("winner"))
    if winner is _INVALID or (technical and winner is None):
        return ()
    deltas = _deltas(result.get("deltas"))
    if deltas is None or (
        not technical and not _winner_matches_deltas(winner, deltas)
    ) or (
        technical and require_frozen_duplicate and deltas != (0, 0)
    ):
        return ()
    if normalize_delta is not None and normalized_delta_value(
        normalize_delta, deltas[0]
    ) is None:
        return ()
    rounds_played = _rounds(result.get("rounds_played"))
    if rounds_played is None:
        if "rounds_played" in result or technical:
            return ()
        rounds_played = fixed_rounds_per_match
    if fixed_rounds_per_match is not None and rounds_played is not None and (
        (technical and rounds_played > fixed_rounds_per_match)
        or (not technical and rounds_played != fixed_rounds_per_match)
    ):
        return ()
    technical_index = (
        _technical_scoring_game_index(result, planned_games=planned_games)
        if technical
        else 1
    )
    if technical_index is _INVALID:
        return ()
    return (
        ScoringGame(
            index=int(technical_index),
            winner=winner,
            deltas=deltas,
            rounds_played=rounds_played,
        ),
    )


def normalized_delta_value(
    normalize_delta: Callable[[int], Real], delta: int
) -> float | None:
    try:
        value = normalize_delta(delta)
        if isinstance(value, bool) or not isinstance(value, Real):
            return None
        numeric = float(value)
    except (TypeError, ValueError, ArithmeticError):
        return None
    if not math.isfinite(numeric):
        return None
    return 0.0 if numeric == 0 else numeric


def _normalized(spec: GameSpec, delta: int) -> float | None:
    return normalized_delta_value(spec.normalize_delta, delta)


def build_public_outcome(
    match: dict[str, Any] | None,
    spec: GameSpec,
    *,
    duplicate: bool | None = None,
    expected_duplicate: bool | None = None,
    require_frozen_duplicate: bool = False,
) -> dict[str, Any] | None:
    """Build the frozen public ``outcome`` wire without result/replay leakage."""
    if not match or match.get("status") != STATUS_COMPLETED:
        return None
    if duplicate is not None and not isinstance(duplicate, bool):
        return None
    if expected_duplicate is not None and not isinstance(expected_duplicate, bool):
        return None
    frozen_duplicate = is_duplicate_match(match)
    if frozen_duplicate is None:
        return None
    projected_expected = match.get("_contest_expected_duplicate")
    projected_strict = match.get("_contest_require_frozen_duplicate")
    if projected_expected is not None:
        if (
            isinstance(projected_expected, bool)
            or not isinstance(projected_expected, Integral)
            or int(projected_expected) not in (0, 1)
        ):
            # A linked contest with malformed/missing stage topology is not a
            # license for the generic match endpoints to guess single/duplicate.
            return None
        projected_expected = bool(projected_expected)
    if projected_strict is not None:
        if (
            isinstance(projected_strict, bool)
            or not isinstance(projected_strict, Integral)
            or int(projected_strict) not in (0, 1)
        ):
            return None
        require_frozen_duplicate = (
            require_frozen_duplicate or bool(projected_strict)
        )
    projected_stage = match.get("_contest_stage_config_json")
    if projected_stage is not None:
        if isinstance(projected_stage, str):
            try:
                projected_stage = json.loads(projected_stage)
            except (TypeError, ValueError):
                return None
        if not isinstance(projected_stage, dict):
            return None
        # Import lazily to keep the low-level Match parser usable during game
        # registry/contest module initialization.
        from bzplat.backend.contests.validation import (
            stage_scoring_contract_is_valid,
        )

        if not stage_scoring_contract_is_valid(
            projected_stage, game_id=spec.game_id
        ):
            return None
    duplicate = (
        projected_expected
        if duplicate is None and projected_expected is not None
        else frozen_duplicate
        if duplicate is None
        else duplicate
    )
    if expected_duplicate is not None and duplicate != expected_duplicate:
        # A contest stage and its already-frozen Match execution contract must
        # never disagree.  Publishing either interpretation would make match
        # detail and contest views report different score semantics.
        return None
    if require_frozen_duplicate:
        strict_frozen_duplicate = _frozen_duplicate_mode(
            match, require_explicit=True
        )
        if strict_frozen_duplicate is None:
            return None
        authority = (
            expected_duplicate
            if expected_duplicate is not None
            else projected_expected
            if projected_expected is not None
            else duplicate
        )
        if strict_frozen_duplicate != authority:
            return None
    try:
        planned_games = planned_match_games(spec, duplicate=duplicate)
    except (TypeError, ValueError):
        # Corrupt history may claim duplicate for a game that has no duplicate
        # plan.  Public metadata must fail closed, never turn one bad row into a
        # list/detail 500.
        return None
    games = scoring_games_for_match(
        match,
        duplicate=duplicate,
        planned_games=planned_games,
        fixed_rounds_per_match=spec.fixed_rounds_per_match,
        require_frozen_duplicate=require_frozen_duplicate,
        normalize_delta=spec.normalize_delta,
    )
    if not games:
        return None
    technical = _technical_flag(match.get("technical_loss"))
    if len(games) > planned_games or (not duplicate and len(games) != 1):
        return None

    result = _result_dict(match.get("result"))
    total_deltas = _deltas(result.get("deltas"))
    if total_deltas is None and all(game.deltas is not None for game in games):
        total_deltas = (
            sum(int(game.deltas[0]) for game in games if game.deltas is not None),
            sum(int(game.deltas[1]) for game in games if game.deltas is not None),
        )
    total_rounds = _rounds(result.get("rounds_played"))
    if total_rounds is None and all(game.rounds_played is not None for game in games):
        total_rounds = sum(int(game.rounds_played or 0) for game in games)
    if total_deltas is None or total_rounds is None:
        return None
    normalized_total = _normalized(spec, total_deltas[0])
    if normalized_total is None:
        return None

    public_games: list[dict[str, Any]] = []
    for game in games:
        if game.deltas is None:
            return None
        normalized_delta = _normalized(spec, game.deltas[0])
        if normalized_delta is None:
            return None
        public_games.append(
            {
                "index": game.index,
                "winner": game.winner,
                "rounds_played": game.rounds_played,
                "normalized_delta_a": normalized_delta,
            }
        )

    wins_a = sum(game.winner == 0 for game in games)
    wins_b = sum(game.winner == 1 for game in games)
    draws = sum(game.winner is None for game in games)
    top_winner = _winner(match.get("winner"))
    loser = (
        1 - int(top_winner)
        if technical and top_winner is not _INVALID and top_winner is not None
        else None
    )
    return {
        "kind": "duplicate" if duplicate else "single",
        "planned_games": planned_games,
        "completed_games": len(games),
        "score": {"wins_a": wins_a, "draws": draws, "wins_b": wins_b},
        "rounds_played": total_rounds,
        "normalized_delta_a": normalized_total,
        "games": public_games,
        "termination": {
            "kind": "technical" if technical else "normal",
            "reason": canonical_public_completed_reason(match.get("reason")),
            "loser": loser,
        },
    }


__all__ = [
    "ScoringGame",
    "build_public_outcome",
    "is_duplicate_match",
    "normalized_delta_value",
    "planned_match_games",
    "scoring_games_for_match",
]
