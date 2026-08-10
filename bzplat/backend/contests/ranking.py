"""赛事正式排名 + 破同分（预赛/决赛 P2）。

基于 ContestManager.standings（P0 已改 entry_id 键），计算全员唯一连续正式名次，
破同分链：points → buchholz_cut1 → sonneborn_berger → head_to_head → normalized_delta
→ technical_losses → entry.seed。

各破同分项（基于该阶段所有已完成对局）：
- Buchholz：该 entry 所有对手的 points 总和（衡量对手强度）
- Buchholz Cut1：去掉最高对手分后的 Buchholz（减少一场极端对手的影响）
- Sonneborn-Berger：该 entry 击败的对手 points 之和（胜场对手强度加权）
- Head-to-head：同分者之间直接对战胜率
- normalized_delta：本游戏的座位 0 归一化分差（Holdem 为大盲注）
- technical_losses：技术负次数（越少越好，P4 落 technical_loss 后用）
- entry.seed：报名序（最后兜底，确定性）
"""
from __future__ import annotations

import json
from typing import Any


def _entry_opponents_map(
    pairings: list[dict], matches: dict[str, dict]
) -> dict[int, list[dict]]:
    """entry_id → 该阶段它打过的所有对手记录 [{opp_entry, won, draw, opp_points}]。

    matches: match_id → match dict（含 status/winner/result.deltas）。
    """
    from bzplat.backend.store.db import match_deltas

    out: dict[int, list[dict]] = {}
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
                ld = lg.get("deltas") or [0, 0]
                ea_earn, eb_earn = int(ld[0]), int(ld[1])
                for me, opp, side, my_earn in (
                    (ea, eb, 0, ea_earn),
                    (eb, ea, 1, eb_earn),
                ):
                    won = 1 if w == side else 0
                    draw = 1 if w is None else 0
                    out.setdefault(me, []).append(
                        {"opp_entry": opp, "won": won, "draw": draw, "earn": my_earn}
                    )
        else:
            w = m.get("winner")
            ea_earn, eb_earn = match_deltas(m)  # 从 result.deltas 取（取代旧 earnings_a/b 列）
            for me, opp, side, my_earn in (
                (ea, eb, 0, ea_earn),
                (eb, ea, 1, eb_earn),
            ):
                won = 1 if w == side else 0
                draw = 1 if w is None else 0
                out.setdefault(me, []).append(
                    {"opp_entry": opp, "won": won, "draw": draw, "earn": my_earn}
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
    opp_map = _entry_opponents_map(pairings, matches)
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
