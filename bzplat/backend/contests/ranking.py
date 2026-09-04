"""赛事正式排名 + 破同分（预赛/决赛 P2）。

基于 ContestManager.standings（P0 已改 entry_id 键），计算全员唯一连续正式名次，
破同分链：points → buchholz_cut1 → sonneborn_berger → head_to_head → normalized_delta
→ technical_losses → entry.seed。

各破同分项（基于该阶段所有已完成计分记录；复式每条 leg 各是一条记录）：
- Buchholz：该 entry 所有计分记录对应对手的 points 总和（重复交手会重复加权）
- Buchholz Cut1：independent-v1 删去最低一条；历史赛制保持原最高一条算法
- Sonneborn-Berger：该 entry 击败的对手 points 之和（胜场对手强度加权）
- Head-to-head：同分者之间直接对战胜率
- normalized_delta：本游戏的座位 0 归一化分差（Holdem 为大盲注）
- technical_losses：技术负次数（越少越好，P4 落 technical_loss 后用）
- entry.seed：报名序（最后兜底，确定性）
"""
from __future__ import annotations

import json
from typing import Any

from bzplat.backend.games import registry as game_registry
from bzplat.backend.contests.stages import effective_swiss_rounds
from bzplat.backend.contests.series import (
    contest_match_binding_is_valid,
    contest_pairing_roster_binding_is_valid,
    group_conceptual_series,
    is_aggregate_series_stage,
    swiss_bye_record_weights,
    summarize_conceptual_series,
)
from bzplat.backend.contests.templates import points_for_result
from bzplat.backend.contests.validation import (
    SERIES_SCORING_INDEPENDENT,
    contest_current_stage_index,
    reserved_group_markers_match_template,
    stage_duplicate_mode,
    stage_scoring_contract_is_valid,
)
from bzplat.backend.matches.public_outcome import (
    is_duplicate_match,
    normalized_delta_value,
    scoring_games_for_match,
)
from bzplat.backend.store.validation import (
    exact_nonnegative_int,
    is_authoritative_no_opponent_pairing,
)


def _entry_opponents_map(
    pairings: list[dict],
    matches: dict[str, dict],
    *,
    stage: dict[str, Any] | None = None,
    planned_games_per_match: int | None = None,
    fixed_rounds_per_match: int | None = None,
    game_id: str | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: dict[int, int | None] | None = None,
    expected_entry_users: dict[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> dict[int, list[dict]]:
    """entry_id → 该阶段它打过的所有对手记录 [{opp_entry, won, draw, opp_points}]。

    matches: match_id → match dict（含 status/winner）。
    """
    out: dict[int, list[dict]] = {}
    stage_supplied = stage is not None
    stage = stage or {}
    stage_duplicate = stage_duplicate_mode(stage) if stage_supplied else None
    if stage_supplied and not stage_scoring_contract_is_valid(
        stage, game_id=game_id
    ):
        return out
    stage_planned_games = planned_games_per_match
    if stage_supplied and stage_planned_games is None:
        stage_planned_games = 2 if stage_duplicate else 1
    if is_aggregate_series_stage(stage):
        game_spec = (
            game_registry.get(game_id)
            if isinstance(game_id, str) and game_id in game_registry.all_ids()
            else None
        )
        for rows in group_conceptual_series(stage, pairings).values():
            summary = summarize_conceptual_series(
                stage,
                rows,
                matches.get,
                game_spec=game_spec,
                expected_contest_id=expected_contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )
            if not summary["settled"]:
                continue
            first, second = summary["entries"]
            winner_entry = summary["winner_entry"]
            for me, opponent in ((first, second), (second, first)):
                out.setdefault(me, []).append(
                    {
                        "opp_entry": opponent,
                        "won": 1 if winner_entry == me else 0,
                        "draw": 1 if winner_entry is None else 0,
                    }
                )
        return out
    for p in pairings:
        mid = p.get("match_id")
        if not mid:
            if (
                stage.get("series_scoring") == SERIES_SCORING_INDEPENDENT
                and stage.get("type") == "swiss"
                and is_authoritative_no_opponent_pairing(stage.get("type"), p)
                and p.get("entry_a_id") is not None
                and (
                    expected_contest_id is None
                    or contest_pairing_roster_binding_is_valid(
                        p,
                        expected_contest_id=expected_contest_id,
                        expected_entry_bots=expected_entry_bots,
                        expected_entry_users=expected_entry_users,
                        require_current_entry_bots=require_current_entry_bots,
                        require_opponent=False,
                    )
                )
            ):
                entry_id = int(p["entry_a_id"])
                for weight in swiss_bye_record_weights(
                    stage,
                    scoring_games_per_match=(stage_planned_games or 1),
                ):
                    out.setdefault(entry_id, []).append(
                        {
                            "virtual": True,
                            "weight": weight,
                            "won": int(weight == 1.0),
                            "draw": int(weight == 0.5),
                        }
                    )
            continue
        if mid not in matches:
            continue
        m = matches[mid]
        if m.get("status") != "completed":
            continue
        if not contest_match_binding_is_valid(
            p,
            m,
            expected_contest_id=expected_contest_id,
            expected_game_id=(
                game_id if expected_contest_id is not None else None
            ),
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        ):
            continue
        ea = p.get("entry_a_id")
        eb = p.get("entry_b_id")
        if ea is None or eb is None:
            continue
        duplicate = (
            is_duplicate_match(m)
            if not stage_supplied
            else stage_duplicate
        )
        planned_games = stage_planned_games
        if planned_games is None:
            # Direct ranking callers historically do not carry a GameSpec.
            # The only registered duplicate plan currently has two games;
            # production manager callers always pass the spec-derived value.
            planned_games = 2 if duplicate else 1
        games = scoring_games_for_match(
            m,
            duplicate=duplicate,
            planned_games=planned_games,
            fixed_rounds_per_match=fixed_rounds_per_match,
            require_frozen_duplicate=(
                (stage or {}).get("series_scoring")
                == SERIES_SCORING_INDEPENDENT
            ),
            normalize_delta=(
                game_registry.get(game_id).normalize_delta
                if isinstance(game_id, str) and game_id in game_registry.all_ids()
                else None
            ),
        )
        for game in games:
            w = game.winner
            for me, opp, side in (
                (ea, eb, 0),
                (eb, ea, 1),
            ):
                won = 1 if w == side else 0
                draw = 1 if w is None else 0
                out.setdefault(me, []).append(
                    {"opp_entry": opp, "won": won, "draw": draw}
                )
    return out


def _technical_losses_map(
    pairings: list[dict],
    matches: dict[str, dict],
    *,
    stage: dict[str, Any] | None = None,
    planned_games_per_match: int | None = None,
    fixed_rounds_per_match: int | None = None,
    game_id: str | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: dict[int, int | None] | None = None,
    expected_entry_users: dict[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> dict[int, int]:
    """Count adjudicated technical losses by durable entry identity."""
    losses: dict[int, int] = {}
    stage_supplied = stage is not None
    stage = stage or {}
    stage_duplicate = stage_duplicate_mode(stage) if stage_supplied else None
    if stage_supplied and not stage_scoring_contract_is_valid(
        stage, game_id=game_id
    ):
        return losses
    for pairing in pairings:
        match_id = pairing.get("match_id")
        match = matches.get(match_id) if match_id else None
        raw_technical = match.get("technical_loss") if match else None
        technical = raw_technical is True or (
            isinstance(raw_technical, int)
            and not isinstance(raw_technical, bool)
            and raw_technical == 1
        )
        if (
            not match
            or match.get("status") != "completed"
            or not technical
        ):
            continue
        if not contest_match_binding_is_valid(
            pairing,
            match,
            expected_contest_id=expected_contest_id,
            expected_game_id=(
                game_id if expected_contest_id is not None else None
            ),
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        ):
            continue
        duplicate = (
            is_duplicate_match(match)
            if not stage_supplied
            else stage_duplicate
        )
        planned_games = planned_games_per_match
        if planned_games is None:
            planned_games = 2 if duplicate else 1
        if not scoring_games_for_match(
            match,
            duplicate=duplicate,
            planned_games=planned_games,
            fixed_rounds_per_match=fixed_rounds_per_match,
            require_frozen_duplicate=(
                (stage or {}).get("series_scoring")
                == SERIES_SCORING_INDEPENDENT
            ),
            normalize_delta=(
                game_registry.get(game_id).normalize_delta
                if isinstance(game_id, str) and game_id in game_registry.all_ids()
                else None
            ),
        ):
            continue
        winner = match.get("winner")
        if winner == 0:
            losing_entry = pairing.get("entry_b_id")
        elif winner == 1:
            losing_entry = pairing.get("entry_a_id")
        else:
            continue
        if losing_entry is not None:
            entry_id = int(losing_entry)
            losses[entry_id] = losses.get(entry_id, 0) + 1
    return losses


def compute_official_ranking(
    standings: list[dict],
    pairings: list[dict],
    matches: dict[str, dict],
    *,
    normalize_delta=None,
    stage: dict[str, Any] | None = None,
    planned_games_per_match: int | None = None,
    fixed_rounds_per_match: int | None = None,
    game_id: str | None = None,
    expected_contest_id: int | None = None,
    expected_entry_bots: dict[int, int | None] | None = None,
    expected_entry_users: dict[int, int] | None = None,
    require_current_entry_bots: bool = False,
) -> list[dict]:
    """计算全员唯一连续正式名次（含破同分明细）。

    standings: ContestManager.standings() 返回（每行含 entry_id/bot_id/points/delta_total/seed/...）。
    pairings: 该阶段对阵（含 entry_a_id/entry_b_id/match_id）。
    matches: match_id → match dict。
    normalize_delta: GameSpec.normalize_delta（Holdem: 筹码差/BB；棋类透传）。
    返回排序后的列表，每行加 rank + tiebreaks（dict）。
    """
    if stage is not None and not stage_scoring_contract_is_valid(
        stage, game_id=game_id
    ):
        return []

    # points 查表（entry_id → points）
    pts = {s["entry_id"]: float(s.get("points") or 0) for s in standings}
    opp_map = _entry_opponents_map(
        pairings,
        matches,
        stage=stage,
        planned_games_per_match=planned_games_per_match,
        fixed_rounds_per_match=fixed_rounds_per_match,
        game_id=game_id,
        expected_contest_id=expected_contest_id,
        expected_entry_bots=expected_entry_bots,
        expected_entry_users=expected_entry_users,
        require_current_entry_bots=require_current_entry_bots,
    )
    technical_losses = _technical_losses_map(
        pairings,
        matches,
        stage=stage,
        planned_games_per_match=planned_games_per_match,
        fixed_rounds_per_match=fixed_rounds_per_match,
        game_id=game_id,
        expected_contest_id=expected_contest_id,
        expected_entry_bots=expected_entry_bots,
        expected_entry_users=expected_entry_users,
        require_current_entry_bots=require_current_entry_bots,
    )
    independent_v1 = (stage or {}).get("series_scoring") == SERIES_SCORING_INDEPENDENT
    planned_swiss_games = 0
    draw_points = 0.0
    if independent_v1 and (stage or {}).get("type") == "swiss":
        games_per_pair = int((stage or {}).get("games_per_pair") or 1)
        per_match_games = planned_games_per_match
        if per_match_games is None:
            per_match_games = 2 if stage_duplicate_mode(stage or {}) else 1
        planned_swiss_games = (
            effective_swiss_rounds(stage or {}, len(standings))
            * games_per_pair
            * per_match_games
        )
        scoring = (stage or {}).get("scoring")
        if isinstance(scoring, str):
            draw_points = points_for_result(scoring, None, 0)

    rows: list[dict] = []
    for s in standings:
        eid = s["entry_id"]
        opps = opp_map.get(eid, [])
        virtual_points = min(
            pts.get(eid, 0), draw_points * planned_swiss_games
        )
        opp_pts = [
            virtual_points if o.get("virtual") else pts.get(o["opp_entry"], 0)
            for o in opps
        ]
        # Buchholz：对手 points 总和
        buchholz = sum(opp_pts)
        # v1 修正为删最低单条记录；历史 aggregate/无 marker 快照绝不重写。
        cut_value = (
            min(opp_pts)
            if independent_v1 and opp_pts
            else max(opp_pts, default=0)
        )
        buchholz_cut1 = buchholz - cut_value if opp_pts else 0
        # Sonneborn-Berger：击败/平的对手 points 加权（胜×1 + 平×0.5）
        sonneborn = sum(
            (
                (virtual_points if o.get("virtual") else pts.get(o["opp_entry"], 0))
                * (1 if o["won"] else 0.5 if o["draw"] else 0)
            )
            for o in opps
        )
        # head-to-head 胜率（在该 entry 同分对手间的胜率）
        same_pts_opps = [
            o for o in opps
            if not o.get("virtual")
            and pts.get(o["opp_entry"], 0) == pts.get(eid, 0)
        ]
        # Aggregate-series stages treat a conceptual draw as half a direct-
        # encounter point.  Keep the historical wins/records calculation for
        # legacy stages so old official rankings are not rewritten.
        if independent_v1 or is_aggregate_series_stage(stage or {}):
            h2h_wins = sum(
                o["won"] + (0.5 if o["draw"] else 0.0)
                for o in same_pts_opps
            )
        else:
            h2h_wins = sum(o["won"] for o in same_pts_opps)
        h2h_total = len(same_pts_opps)
        h2h_rate = h2h_wins / h2h_total if h2h_total else 0.0
        # 原始分差经当前游戏 spec 换算为可比较的单位。
        delta_total = int(s.get("delta_total") or 0)
        normalized = normalized_delta_value(
            normalize_delta if normalize_delta else float,
            delta_total,
        )
        if normalized is None:
            # Individually representable Match deltas may still overflow when
            # a damaged/imported stage accumulates them.  Returning no ranking
            # keeps lifecycle callers from freezing an unstable tie-break.
            return []
        tiebreaks = {
            "points": pts.get(eid, 0),
            "buchholz": buchholz,
            "buchholz_cut1": buchholz_cut1,
            "sonneborn_berger": sonneborn,
            "head_to_head": h2h_rate,
            "normalized_delta": normalized,
            "technical_losses": technical_losses.get(eid, 0),
            "seed": int(s.get("seed") or 0),
        }
        rows.append({**s, "tiebreaks": tiebreaks})

    # 排序：破同分链（注意 technical_losses 升序=越少越好，其余降序=越大越好）
    rows.sort(
        key=lambda r: (
            -r["tiebreaks"]["points"],
            -r["tiebreaks"]["buchholz_cut1"],
            -r["tiebreaks"]["sonneborn_berger"],
            -r["tiebreaks"]["head_to_head"],
            -r["tiebreaks"]["normalized_delta"],
            r["tiebreaks"]["technical_losses"],  # 升序
            r["tiebreaks"]["seed"],  # 升序（确定性兜底）
        )
    )
    # 赋唯一连续 rank（1..N）
    for i, r in enumerate(rows):
        r["rank"] = i + 1
    return rows


def compute_cross_group_ranking(
    standings: list[dict],
    pairings: list[dict],
    matches: dict[str, dict],
    *,
    normalize_delta=None,
    stage: dict[str, Any],
    planned_games_per_match: int,
    fixed_rounds_per_match: int | None,
    game_id: str,
    expected_contest_id: int,
    expected_entry_bots: dict[int, int | None],
    expected_entry_users: dict[int, int],
    require_current_entry_bots: bool = False,
) -> list[dict]:
    """Rank unequal random groups without comparing their raw point totals.

    Each group's existing 2/1/0 chain first determines ``rank_in_group``.
    Cross-group order then uses only normalized rates and the frozen draw order;
    direct encounters never cross group boundaries.
    """
    grouped_standings: dict[str, list[dict]] = {}
    entry_groups: dict[int, str] = {}
    for row in standings:
        entry_id = row.get("entry_id")
        group_id = row.get("group_id")
        if (
            isinstance(entry_id, bool)
            or not isinstance(entry_id, int)
            or entry_id < 1
            or entry_id in entry_groups
            or not isinstance(group_id, str)
            or not group_id
        ):
            return []
        entry_groups[entry_id] = group_id
        grouped_standings.setdefault(group_id, []).append(row)
    grouped_pairings: dict[str, list[dict]] = {}
    for pairing in pairings:
        entry_a_id = pairing.get("entry_a_id")
        entry_b_id = pairing.get("entry_b_id")
        group_id = pairing.get("group_id")
        if (
            not isinstance(group_id, str)
            or group_id not in grouped_standings
            or isinstance(entry_a_id, bool)
            or not isinstance(entry_a_id, int)
            or isinstance(entry_b_id, bool)
            or not isinstance(entry_b_id, int)
            or entry_a_id == entry_b_id
            or entry_groups.get(entry_a_id) != group_id
            or entry_groups.get(entry_b_id) != group_id
        ):
            return []
        grouped_pairings.setdefault(group_id, []).append(pairing)
    if set(grouped_pairings) != set(grouped_standings):
        return []

    all_rows: list[dict] = []
    points_rates: dict[int, float] = {}
    opponents: dict[int, list[dict]] = {}
    for group_id in sorted(grouped_standings):
        group_pairings = grouped_pairings[group_id]
        group_match_ids = {
            str(pairing["match_id"])
            for pairing in group_pairings
            if pairing.get("match_id") is not None
        }
        group_matches = {
            match_id: match
            for match_id, match in matches.items()
            if str(match_id) in group_match_ids
        }
        ranked = compute_official_ranking(
            grouped_standings[group_id],
            group_pairings,
            group_matches,
            normalize_delta=normalize_delta,
            stage=stage,
            planned_games_per_match=planned_games_per_match,
            fixed_rounds_per_match=fixed_rounds_per_match,
            game_id=game_id,
            expected_contest_id=expected_contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
        if len(ranked) != len(grouped_standings[group_id]):
            return []
        group_opponents = _entry_opponents_map(
            group_pairings,
            group_matches,
            stage=stage,
            planned_games_per_match=planned_games_per_match,
            fixed_rounds_per_match=fixed_rounds_per_match,
            game_id=game_id,
            expected_contest_id=expected_contest_id,
            expected_entry_bots=expected_entry_bots,
            expected_entry_users=expected_entry_users,
            require_current_entry_bots=require_current_entry_bots,
        )
        opponents.update(group_opponents)
        for row in ranked:
            games = int(row.get("wins") or 0) + int(row.get("draws") or 0) + int(row.get("losses") or 0)
            entry_id = int(row["entry_id"])
            # 2/1/0 scoring: divide by the maximum two points per game.
            points_rates[entry_id] = (
                float(row.get("points") or 0) / (2.0 * games)
                if games
                else 0.0
            )
            all_rows.append({**row, "rank_in_group": int(row["rank"])})

    for row in all_rows:
        entry_id = int(row["entry_id"])
        games = int(row.get("wins") or 0) + int(row.get("draws") or 0) + int(row.get("losses") or 0)
        opponent_rows = [
            record for record in opponents.get(entry_id, []) if not record.get("virtual")
        ]
        if len(opponent_rows) != games:
            return []
        opponent_strength = (
            sum(points_rates.get(int(record["opp_entry"]), 0.0) for record in opponent_rows)
            / games
        ) if games else 0.0
        normalized_delta = float(row["tiebreaks"]["normalized_delta"])
        technical_losses = int(row["tiebreaks"]["technical_losses"])
        draw_order = int(row.get("seed") or 0)
        if draw_order < 1:
            return []
        row["tiebreaks"] = {
            **row["tiebreaks"],
            "group_rank": int(row["rank_in_group"]),
            "points_rate": points_rates[entry_id],
            "opponent_strength": opponent_strength,
            "normalized_delta_rate": normalized_delta / games if games else 0.0,
            "technical_loss_rate": technical_losses / games if games else 0.0,
            "draw_order": draw_order,
        }

    all_rows.sort(
        key=lambda row: (
            row["tiebreaks"]["group_rank"],
            -row["tiebreaks"]["points_rate"],
            -row["tiebreaks"]["opponent_strength"],
            -row["tiebreaks"]["normalized_delta_rate"],
            row["tiebreaks"]["technical_loss_rate"],
            row["tiebreaks"]["draw_order"],
        )
    )
    for index, row in enumerate(all_rows, start=1):
        row["rank"] = index
        row["overall_rank"] = index
    return all_rows


def merge_replace_top(
    stage1_ranking: list[dict],
    stage2_ranking: list[dict],
    scope: int = 8,
    *,
    expected_entry_groups: dict[int, object] | None = None,
) -> list[dict]:
    """决赛合成榜（replace_top 模式）：1..scope 取 stage2（Top8 双循环），

    scope+1..N 取 stage1 未晋级者相对序。
    stage1_ranking/stage2_ranking 都是 compute_official_ranking 返回（含 rank）。
    """
    if (
        isinstance(scope, bool)
        or not isinstance(scope, int)
        or scope < 1
        or not stage1_ranking
        or not stage2_ranking
    ):
        return []
    previous_by_entry: dict[int, dict] = {}
    for row in stage1_ranking:
        entry_id = row.get("entry_id")
        if (
            isinstance(entry_id, bool)
            or not isinstance(entry_id, int)
            or entry_id < 1
            or entry_id in previous_by_entry
        ):
            return []
        previous_by_entry[entry_id] = row
    final_rows = stage2_ranking[:scope]
    final_entry_ids = [row.get("entry_id") for row in final_rows]
    if (
        any(
            isinstance(entry_id, bool)
            or not isinstance(entry_id, int)
            or entry_id < 1
            for entry_id in final_entry_ids
        )
        or len(set(final_entry_ids)) != len(final_entry_ids)
        or any(entry_id not in previous_by_entry for entry_id in final_entry_ids)
    ):
        return []
    top = [
        {
            **row,
            "group_id": previous_by_entry.get(row["entry_id"], {}).get("group_id")
            or row.get("group_id")
            or "",
            "rank_in_group": previous_by_entry.get(row["entry_id"], {}).get(
                "rank_in_group"
            ),
        }
        for row in final_rows
    ]
    top_entry_ids = {r["entry_id"] for r in top}
    rest = [
        dict(r) for r in stage1_ranking if r["entry_id"] not in top_entry_ids
    ]
    merged = list(top) + list(rest)
    if expected_entry_groups is not None:
        if set(expected_entry_groups) != set(previous_by_entry):
            return []
        normalized_groups: dict[int, str] = {}
        for entry_id, raw_group in expected_entry_groups.items():
            if (
                not isinstance(raw_group, str)
                or raw_group != raw_group.strip()
                or any(ord(char) < 32 or ord(char) == 127 for char in raw_group)
            ):
                return []
            normalized_groups[entry_id] = raw_group
        grouped = [bool(group_id) for group_id in normalized_groups.values()]
        if any(grouped) and not all(grouped):
            return []
        for row in merged:
            entry_id = int(row["entry_id"])
            group_id = normalized_groups[entry_id]
            if group_id:
                previous = previous_by_entry[entry_id]
                rank_in_group = previous.get("rank_in_group")
                if (
                    previous.get("group_id") != group_id
                    or isinstance(rank_in_group, bool)
                    or not isinstance(rank_in_group, int)
                    or rank_in_group < 1
                ):
                    return []
                row["group_id"] = group_id
                row["rank_in_group"] = rank_in_group
            else:
                # Legacy group templates did not freeze group_id on the roster.
                # Keep their official table historically ungrouped while using
                # the private completed-stage snapshot only for fallback order.
                row["group_id"] = ""
                row["rank_in_group"] = None
    for i, r in enumerate(merged):
        r["rank"] = i + 1
    return merged


def final_stage_replaces_previous_ranking(
    stage: dict | None, *, stage_idx: int
) -> bool:
    """Whether a terminal stage supplies only the leading ranking cohort.

    ``replace_top`` is the explicit contract used by ranking finals.  Legacy
    qualifier-to-KO templates predate that marker, but have the same durable
    meaning: the KO orders its finalists while the frozen qualifier table
    remains authoritative for everyone who did not advance.
    """
    return bool(
        stage_idx > 0
        and isinstance(stage, dict)
        and (
            stage.get("ranking_mode") == "replace_top"
            or stage.get("type") == "single_elimination"
        )
    )


def with_official_result_provenance(
    contest: dict,
    rows: list[dict],
    *,
    stage_entry_ids: dict[int, set[int]] | None = None,
) -> list[dict]:
    """为正式榜读模型补充积分来源阶段和可比较分组。

    ``replace_top`` 榜单把决赛选手与预赛未晋级者拼在一起；两组积分与
    破同分数据并不互相比较。来源可以由冻结的赛制、阶段结果成员关系和
    最终名次边界稳定派生，无需为这次展示修复修改生产数据库 schema。
    """
    raw_stages = contest.get("stages_json") or "[]"
    if isinstance(raw_stages, list):
        stages = raw_stages
    else:
        try:
            parsed = json.loads(raw_stages)
        except (TypeError, ValueError):
            parsed = []
        stages = parsed if isinstance(parsed, list) else []
    if not stages:
        from bzplat.backend.contests.templates import get_template

        template = get_template(str(contest.get("template_id") or ""))
        template_stages = template.get("stages") if template else []
        stages = template_stages if isinstance(template_stages, list) else []

    if not reserved_group_markers_match_template(
        contest.get("template_id"), stages, game_id=contest.get("game_id")
    ):
        return []

    final_stage_idx = contest_current_stage_index(
        contest, stage_count=len(stages)
    )
    if final_stage_idx is None:
        return [
            {
                **dict(row),
                "source_stage": None,
                "ranking_cohort": "unknown",
            }
            for row in rows
        ]
    final_stage = stages[final_stage_idx] if stages else {}
    final_stage_valid = bool(
        isinstance(final_stage, dict)
        and stage_scoring_contract_is_valid(
            final_stage, game_id=contest.get("game_id")
        )
    )
    replace_top = (
        final_stage_valid
        and final_stage_replaces_previous_ranking(
            final_stage, stage_idx=final_stage_idx
        )
    )
    final_entries = (stage_entry_ids or {}).get(final_stage_idx, set())
    implicit_knockout = bool(
        replace_top
        and final_stage.get("type") == "single_elimination"
        and final_stage.get("ranking_mode") != "replace_top"
    )
    if implicit_knockout:
        # Legacy qualifier-to-KO stages did not persist a ranking_scope marker.
        # Their exact finalist cohort is the completed final-stage snapshot.
        # Without that evidence, expose an unknown cohort rather than guessing
        # from a template's nominal advance count (small rosters are clamped).
        scope = len(final_entries) if final_entries else None
    else:
        raw_scope = (
            final_stage.get("ranking_scope", 8) if final_stage_valid else 8
        )
        scope = (
            raw_scope
            if isinstance(raw_scope, int)
            and not isinstance(raw_scope, bool)
            and raw_scope >= 1
            else 8
        )

    enriched: list[dict] = []
    for row in rows:
        public = dict(row)
        if replace_top:
            if scope is None:
                public["source_stage"] = None
                public["ranking_cohort"] = "unknown"
                enriched.append(public)
                continue
            try:
                rank = int(public.get("rank") or 0)
                entry_id = int(public.get("entry_id"))
            except (TypeError, ValueError):
                rank = 0
                entry_id = -1
            # replace_top 的权威合榜边界始终是最终名次 Top-N。阶段成员证据
            # 只用于确认这行确实参加过末阶段，不能把末阶段落选者也误算进
            # 最终 cohort；旧快照完全没有成员证据时才只依赖名次边界。
            in_final_cohort = 0 < rank <= scope
            if final_entries:
                in_final_cohort = in_final_cohort and entry_id in final_entries
            source_stage = (
                final_stage_idx if in_final_cohort else final_stage_idx - 1
            )
        else:
            source_stage = exact_nonnegative_int(public.get("stage_idx"))
            if source_stage is None and "stage_idx" not in public:
                source_stage = final_stage_idx
        public["source_stage"] = source_stage
        public["ranking_cohort"] = (
            f"stage:{source_stage}" if source_stage is not None else "unknown"
        )
        enriched.append(public)
    return enriched


def build_official_result_rows(
    ranking: list[dict],
    *,
    stage_idx: int = 0,
    awarded_fn=None,
) -> list[dict]:
    """Build the one canonical Store batch for an official ranking."""
    result_rows: list[dict] = []
    for r in ranking:
        awarded = ""
        if awarded_fn:
            awarded = awarded_fn(r) or ""
        result_rows.append(
            {
                "entry_id": r["entry_id"],
                "rank": r["rank"],
                "stage_idx": stage_idx,
                "points": r["tiebreaks"]["points"],
                "bot_id": r.get("bot_id"),
                "user_id": r.get("user_id"),
                "group_id": r.get("group_id") or "",
                "rank_in_group": r.get("rank_in_group"),
                "tiebreaks_json": json.dumps(
                    r["tiebreaks"], ensure_ascii=False
                ),
                "awarded": awarded,
            }
        )
    return result_rows


def persist_official_results(
    store,
    contest_id: int,
    ranking: list[dict],
    *,
    stage_idx: int = 0,
    awarded_fn=None,
) -> None:
    """把全员正式名次作为完整批次原子落库（幂等替换）。"""
    result_rows = build_official_result_rows(
        ranking,
        stage_idx=stage_idx,
        awarded_fn=awarded_fn,
    )
    store.replace_official_results(contest_id, result_rows)
