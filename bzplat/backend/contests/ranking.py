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


def merge_replace_top(
    stage1_ranking: list[dict], stage2_ranking: list[dict], scope: int = 8
) -> list[dict]:
    """决赛合成榜（replace_top 模式）：1..scope 取 stage2（Top8 双循环），

    scope+1..N 取 stage1 未晋级者相对序。
    stage1_ranking/stage2_ranking 都是 compute_official_ranking 返回（含 rank）。
    """
    top = stage2_ranking[:scope]
    top_entry_ids = {r["entry_id"] for r in top}
    rest = [r for r in stage1_ranking if r["entry_id"] not in top_entry_ids]
    merged = list(top) + list(rest)
    for i, r in enumerate(merged):
        r["rank"] = i + 1
    return merged


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
        and final_stage.get("ranking_mode") == "replace_top"
        and final_stage_idx > 0
    )
    raw_scope = final_stage.get("ranking_scope", 8) if final_stage_valid else 8
    scope = (
        raw_scope
        if isinstance(raw_scope, int)
        and not isinstance(raw_scope, bool)
        and raw_scope >= 1
        else 8
    )
    final_entries = (stage_entry_ids or {}).get(final_stage_idx, set())

    enriched: list[dict] = []
    for row in rows:
        public = dict(row)
        if replace_top:
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


def persist_official_results(
    store,
    contest_id: int,
    ranking: list[dict],
    *,
    stage_idx: int = 0,
    awarded_fn=None,
) -> None:
    """把全员正式名次作为完整批次原子落库（幂等替换）。"""
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
                "tiebreaks_json": json.dumps(
                    r["tiebreaks"], ensure_ascii=False
                ),
                "awarded": awarded,
            }
        )
    store.replace_official_results(contest_id, result_rows)
