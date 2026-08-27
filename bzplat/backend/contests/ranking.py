"""赛事正式排名 + 破同分（预赛/决赛 P2）。

基于 ContestManager.standings（P0 已改 entry_id 键），计算全员唯一连续正式名次，
破同分链：points → buchholz_cut1 → sonneborn_berger → head_to_head → normalized_delta
→ technical_losses → entry.seed。

各破同分项（基于该阶段所有已完成计分记录；复式每条 leg 各是一条记录）：
- Buchholz：该 entry 所有计分记录对应对手的 points 总和（重复交手会重复加权）
- Buchholz Cut1：从上述记录中删去一条最高对手分记录后的 Buchholz
- Sonneborn-Berger：该 entry 击败的对手 points 之和（胜场对手强度加权）
- Head-to-head：同分者之间直接对战胜率
- normalized_delta：本游戏的座位 0 归一化分差（Holdem 为大盲注）
- technical_losses：技术负次数（越少越好，P4 落 technical_loss 后用）
- entry.seed：报名序（最后兜底，确定性）
"""
from __future__ import annotations

import json
from typing import Any

from bzplat.backend.contests.series import (
    group_conceptual_series,
    is_aggregate_series_stage,
    summarize_conceptual_series,
)


def _entry_opponents_map(
    pairings: list[dict],
    matches: dict[str, dict],
    *,
    stage: dict[str, Any] | None = None,
) -> dict[int, list[dict]]:
    """entry_id → 该阶段它打过的所有对手记录 [{opp_entry, won, draw, opp_points}]。

    matches: match_id → match dict（含 status/winner）。
    """
    out: dict[int, list[dict]] = {}
    stage = stage or {}
    if is_aggregate_series_stage(stage):
        for rows in group_conceptual_series(stage, pairings).values():
            summary = summarize_conceptual_series(stage, rows, matches.get)
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
        if not mid or mid not in matches:
            continue
        m = matches[mid]
        if m.get("status") != "completed":
            continue
        ea = p.get("entry_a_id")
        eb = p.get("entry_b_id")
        if ea is None or eb is None:
            continue
        # result.legs（复式赛制）：每 leg 独立判胜负，逐场产对手记录。
        # 无 legs（普通赛制）：单场胜负产 1 条（原逻辑）。
        result = m.get("result") or {}
        legs_data = result.get("legs") if isinstance(result, dict) else None
        if legs_data:
            for lg in legs_data:
                w = lg.get("winner")
                for me, opp, side in (
                    (ea, eb, 0),
                    (eb, ea, 1),
                ):
                    won = 1 if w == side else 0
                    draw = 1 if w is None else 0
                    out.setdefault(me, []).append(
                        {"opp_entry": opp, "won": won, "draw": draw}
                    )
        else:
            w = m.get("winner")
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
    pairings: list[dict], matches: dict[str, dict]
) -> dict[int, int]:
    """Count adjudicated technical losses by durable entry identity."""
    losses: dict[int, int] = {}
    for pairing in pairings:
        match_id = pairing.get("match_id")
        match = matches.get(match_id) if match_id else None
        if (
            not match
            or match.get("status") != "completed"
            or not int(match.get("technical_loss") or 0)
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
) -> list[dict]:
    """计算全员唯一连续正式名次（含破同分明细）。

    standings: ContestManager.standings() 返回（每行含 entry_id/bot_id/points/delta_total/seed/...）。
    pairings: 该阶段对阵（含 entry_a_id/entry_b_id/match_id）。
    matches: match_id → match dict。
    normalize_delta: GameSpec.normalize_delta（Holdem: 筹码差/BB；棋类透传）。
    返回排序后的列表，每行加 rank + tiebreaks（dict）。
    """
    # points 查表（entry_id → points）
    pts = {s["entry_id"]: float(s.get("points") or 0) for s in standings}
    opp_map = _entry_opponents_map(pairings, matches, stage=stage)
    technical_losses = _technical_losses_map(pairings, matches)

    rows: list[dict] = []
    for s in standings:
        eid = s["entry_id"]
        opps = opp_map.get(eid, [])
        opp_pts = [pts.get(o["opp_entry"], 0) for o in opps]
        # Buchholz：对手 points 总和
        buchholz = sum(opp_pts)
        # Buchholz Cut1：去掉最高对手分（≥1 个对手时）
        buchholz_cut1 = buchholz - max(opp_pts) if opp_pts else 0
        # Sonneborn-Berger：击败/平的对手 points 加权（胜×1 + 平×0.5）
        sonneborn = sum(
            (pts.get(o["opp_entry"], 0) * (1 if o["won"] else 0.5 if o["draw"] else 0))
            for o in opps
        )
        # head-to-head 胜率（在该 entry 同分对手间的胜率）
        same_pts_opps = [o for o in opps if pts.get(o["opp_entry"], 0) == pts.get(eid, 0)]
        # Aggregate-series stages treat a conceptual draw as half a direct-
        # encounter point.  Keep the historical wins/records calculation for
        # legacy stages so old official rankings are not rewritten.
        if is_aggregate_series_stage(stage or {}):
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
        normalized = (
            normalize_delta(delta_total)
            if normalize_delta
            else float(delta_total)
        )
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

    try:
        final_stage_idx = int(contest.get("current_stage_idx"))
    except (TypeError, ValueError):
        final_stage_idx = len(stages) - 1 if stages else 0
    if stages and not 0 <= final_stage_idx < len(stages):
        final_stage_idx = len(stages) - 1
    final_stage = stages[final_stage_idx] if stages else {}
    replace_top = (
        isinstance(final_stage, dict)
        and final_stage.get("ranking_mode") == "replace_top"
        and final_stage_idx > 0
    )
    try:
        scope = max(1, int(final_stage.get("ranking_scope") or 8))
    except (TypeError, ValueError):
        scope = 8
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
            try:
                source_stage = int(public.get("stage_idx"))
                if source_stage < 0:
                    raise ValueError
            except (TypeError, ValueError):
                source_stage = final_stage_idx
        public["source_stage"] = source_stage
        public["ranking_cohort"] = f"stage:{source_stage}"
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
