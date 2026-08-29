"""赛事阶段对阵生成器。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class PairingSpec:
    bot_a_id: int
    bot_b_id: int | None
    round_num: int = 1
    group_id: str = ""
    bracket_slot: int | None = None
    color_first: int = 0  # 0 = a 先手/座位0；1 = b 先手
    status: str = "pending"
    requires_match: bool = True
    series_index: int = 1
    series_size: int = 1


PAIR_SERIES_STAGE_TYPES = frozenset(
    {"round_robin", "double_round_robin", "swiss"}
)


def _series_extra_seat(
    bot_a_id: int,
    bot_b_id: int,
    cohort: list[int],
) -> int:
    """Choose the odd-series extra seat with a deterministic balanced orientation.

    A complete round-robin graph on an odd number of vertices has a regular
    tournament orientation.  For an even number of players, orient the graph as
    if one deterministic bye vertex existed, then remove it; every real player
    is left with seat imbalance exactly one.  The result is independent of DB
    ids, process order and retry timing.

    Return ``0`` when conceptual A receives the extra seat-0 game, otherwise 1.
    """
    # Preserve the caller's frozen entry/seed order.  Bot ids are used only to
    # locate the two cohort positions; their numeric values never affect seats.
    ordered = list(dict.fromkeys(int(bot_id) for bot_id in cohort))
    if bot_a_id not in ordered or bot_b_id not in ordered:
        raise ValueError("系列对阵选手不在冻结的参赛序列中")
    cycle_size = len(ordered) if len(ordered) % 2 == 1 else len(ordered) + 1
    pos_a = ordered.index(bot_a_id)
    pos_b = ordered.index(bot_b_id)
    distance = (pos_b - pos_a) % cycle_size
    return 0 if 0 < distance <= cycle_size // 2 else 1


def expand_pairing_series(
    pairings: list[PairingSpec],
    games_per_pair: int,
    *,
    cohort: list[int],
    preserve_round_num: bool = False,
) -> list[PairingSpec]:
    """Expand one RR fixture into independent physical Matches.

    Even series split seat 0 exactly evenly.  Odd series differ by one within
    every opponent pair, while ``_series_extra_seat`` also balances that extra
    seat across the complete round-robin graph.
    ``round_num`` advances by one full base schedule for each series cycle so
    existing round staggering remains meaningful and deterministic.
    """
    if isinstance(games_per_pair, bool) or not isinstance(games_per_pair, int):
        raise ValueError("games_per_pair 须为整数")
    if games_per_pair < 1:
        raise ValueError("games_per_pair 须为 >=1 的整数")
    if not pairings:
        return []

    rounds_per_cycle = max(int(pairing.round_num or 1) for pairing in pairings)
    expanded: list[PairingSpec] = []
    if preserve_round_num:
        # Swiss dispatches one series coordinate across the whole round before
        # moving to the next coordinate.  With the contest's shared execution
        # slot this prevents one opponent pair monopolising K consecutive jobs.
        real_pairings = [
            pairing
            for pairing in pairings
            if pairing.bot_b_id is not None and pairing.requires_match
        ]
        for series_index in range(1, games_per_pair + 1):
            for pairing in real_pairings:
                first = int(pairing.color_first or 0)
                expanded.append(
                    PairingSpec(
                        bot_a_id=pairing.bot_a_id,
                        bot_b_id=pairing.bot_b_id,
                        round_num=int(pairing.round_num or 1),
                        group_id=pairing.group_id,
                        bracket_slot=pairing.bracket_slot,
                        color_first=(
                            first if series_index % 2 == 1 else 1 - first
                        ),
                        status=pairing.status,
                        requires_match=True,
                        series_index=series_index,
                        series_size=games_per_pair,
                    )
                )
        expanded.extend(
            PairingSpec(
                **{
                    **pairing.__dict__,
                    "series_index": 1,
                    "series_size": 1,
                }
            )
            for pairing in pairings
            if pairing.bot_b_id is None or not pairing.requires_match
        )
        return expanded

    for pairing in pairings:
        if pairing.bot_b_id is None or not pairing.requires_match:
            expanded.append(
                PairingSpec(
                    **{
                        **pairing.__dict__,
                        "series_index": 1,
                        "series_size": 1,
                    }
                )
            )
            continue
        first = _series_extra_seat(
            pairing.bot_a_id,
            pairing.bot_b_id,
            cohort,
        )
        for series_index in range(1, games_per_pair + 1):
            expanded.append(
                PairingSpec(
                    bot_a_id=pairing.bot_a_id,
                    bot_b_id=pairing.bot_b_id,
                    round_num=(
                        int(pairing.round_num or 1)
                        if preserve_round_num
                        else int(pairing.round_num or 1)
                        + (series_index - 1) * rounds_per_cycle
                    ),
                    group_id=pairing.group_id,
                    bracket_slot=pairing.bracket_slot,
                    color_first=first if series_index % 2 == 1 else 1 - first,
                    status=pairing.status,
                    requires_match=pairing.requires_match,
                    series_index=series_index,
                    series_size=games_per_pair,
                )
            )
    return expanded


def round_robin(bot_ids: list[int], *, double: bool = False) -> list[PairingSpec]:
    """圆桌法单循环；double=True 时补先后手对调的第二循环。"""
    bots = list(bot_ids)
    if len(bots) < 2:
        return []
    bye = None
    if len(bots) % 2 == 1:
        bye = -1
        bots.append(bye)
    n = len(bots)
    rounds = n - 1
    half = n // 2
    fixed = bots[0]
    rotating = bots[1:]
    out: list[PairingSpec] = []
    for r in range(rounds):
        left = [fixed] + rotating[: half - 1]
        right = list(reversed(rotating[half - 1 :]))
        for a, b in zip(left, right):
            if a == bye or b == bye:
                continue
            out.append(PairingSpec(a, b, round_num=r + 1, color_first=0))
        rotating = rotating[1:] + rotating[:1]
    if double:
        flipped = [
            PairingSpec(
                p.bot_a_id,
                p.bot_b_id,
                round_num=p.round_num + rounds,
                group_id=p.group_id,
                color_first=1,
            )
            for p in out
        ]
        out.extend(flipped)
    return out


def effective_group_count(participant_count: int, requested_group_count: int) -> int:
    """Return the shared group count that guarantees two players per group."""
    return max(1, min(requested_group_count, participant_count // 2))


def snake_groups(bot_ids: list[int], group_count: int) -> dict[str, list[int]]:
    """蛇形种子分组。bot_ids 已按种子排序（强→弱）。

    两人以上时组数不得超过人数的一半，确保每组至少两人并能生成
    实际 round-robin 对阵；否则默认四组会让 2–4 人赛事全空、5–7 人
    赛事遗漏落入单人组的参赛者。
    """
    g = effective_group_count(len(bot_ids), group_count)
    groups: dict[str, list[int]] = {f"G{i+1}": [] for i in range(g)}
    keys = list(groups.keys())
    direction = 1
    idx = 0
    for bot in bot_ids:
        groups[keys[idx]].append(bot)
        idx += direction
        if idx >= g or idx < 0:
            direction *= -1
            idx += direction
    return {k: v for k, v in groups.items() if v}


def group_round_robin(
    bot_ids: list[int],
    *,
    group_count: int = 4,
    double: bool = False,
) -> list[PairingSpec]:
    groups = snake_groups(bot_ids, group_count)
    out: list[PairingSpec] = []
    for gid, members in groups.items():
        for p in round_robin(members, double=double):
            p.group_id = gid
            out.append(p)
    return out


def next_power_of_two(n: int) -> int:
    if n <= 1:
        return 1
    return 1 << (n - 1).bit_length()


def single_elimination(bot_ids: list[int]) -> list[PairingSpec]:
    """首轮单败对阵；bye 直接晋级。

    轮空者用 PairingSpec(bot_b_id=None) 表示——由调用方（manager）创建「轮空占位 pairing」
    （bot_b_id=None、无 match、status=completed），这样轮空者被追踪、阶段可 finish、
    下一轮配对时正常带入（见 _maybe_next_elim_round 的 bye 收集逻辑）。
    """
    bots = list(bot_ids)
    # n<=1：无对手不生成对阵（防 IndexError——_seed_bracket 对 len<2 返回原样，
    # 配对 loop 取 seeded[i+1] 越界崩溃）。调用方 _begin_stage 对 KO 阶段收到
    # 空 specs 会判定阶段无对阵 → 标 finished。
    if len(bots) <= 1:
        return []
    size = next_power_of_two(len(bots))
    # 补 bye：强种子轮空
    byes = size - len(bots)
    bracket: list[int | None] = list(bots) + [None] * byes
    # 标准种子位：1 vs n, 2 vs n-1 ...
    seeded = _seed_bracket(bracket)
    out: list[PairingSpec] = []
    slot = 0
    for i in range(0, len(seeded), 2):
        a, b = seeded[i], seeded[i + 1]
        if a is None and b is None:
            continue
        if a is None or b is None:
            # bye：生成 bot_b_id=None 的轮空占位 spec（调用方据此标 completed）
            advancer = a if a is not None else b
            out.append(
                PairingSpec(
                    advancer,
                    None,
                    round_num=1,
                    bracket_slot=slot,
                    color_first=0,
                    status="completed",
                    requires_match=False,
                )
            )
            slot += 1
            continue
        out.append(
            PairingSpec(a, b, round_num=1, bracket_slot=slot, color_first=0)
        )
        slot += 1
    return out


def _seed_bracket(bots: list[int | None]) -> list[int | None]:
    """将按种子序的名单放入单败括号位置。"""
    n = len(bots)
    if n <= 1:
        return bots
    # 递归：上半 1..n/2 种子，下半其余
    positions = [0] * n
    positions[0] = 0
    order = [0]
    size = 1
    while size < n:
        nxt: list[int] = []
        for pos in order:
            nxt.append(pos)
            nxt.append(pos + size)
        order = nxt
        size *= 2
    result: list[int | None] = [None] * n
    for seed_idx, pos in enumerate(order):
        if seed_idx < len(bots):
            result[pos] = bots[seed_idx]
    return result


def swiss_pairings(
    bot_ids: list[int],
    *,
    scores: dict[int, float] | None = None,
    played: set[tuple[int, int]] | None = None,
    round_num: int = 1,
    color_counts: dict[int, int] | None = None,
    bye_counts: dict[int, int] | None = None,
) -> list[PairingSpec]:
    """Dutch-Swiss 配对（O(N log N)，万人赛单轮秒级）。

    - 按 scores 降序分组（同分组内配对，跨组只在偶数补缺时）
    - 避开已交手（played set）；组内全交手过则跨组找
    - 座位平衡（color_first 轮换：历史先手多的本轮后手）
    - 奇数 N：优先选历史 bye 最少者，其次选最低分者；返回显式
      ``completed`` / ``requires_match=False`` 轮空占位，上层可持久化积分
    """
    scores = scores or {b: 0.0 for b in bot_ids}
    played = played or set()
    color_counts = color_counts or {}  # bot_id → 累计先手次数（正=先手多）
    bye_counts = bye_counts or {}
    # 按 (-score, bot_id) 排序（确定性，积分同按 id）
    ordered = sorted(bot_ids, key=lambda b: (-scores.get(b, 0.0), b))
    # 奇数 N：先轮换历史 bye 最少者，再按低分/当前排序末位决定。
    # ``ordered`` 同分时 bot_id 升序，原逻辑 pop() 会选最大 id；用 -id
    # 保留这个确定性兜底。
    bye_bot = None
    if len(ordered) % 2 == 1:
        bye_bot = min(
            ordered,
            key=lambda b: (
                int(bye_counts.get(b, 0)),
                float(scores.get(b, 0.0)),
                -b,
            ),
        )
        ordered.remove(bye_bot)
    out: list[PairingSpec] = []
    unpaired = list(ordered)
    idx = 0
    while idx < len(unpaired):
        a = unpaired[idx]
        # 在剩余未配对里找首个未交手过的（从 idx+1 起，同分组优先）
        partner = None
        for j in range(idx + 1, len(unpaired)):
            b = unpaired[j]
            key = (min(a, b), max(a, b))
            if key not in played:
                partner = j
                break
        if partner is None:
            # 组内全交手过 → played 的契约是 set，只能判断是否交手、没有重复次数。
            # 此时所有候选都已交手，稳定取剩余首个避免卡死；不得对 set 调 .get。
            best_j = -1
            best_repeat = None
            for j in range(idx + 1, len(unpaired)):
                b = unpaired[j]
                rep = int((min(a, b), max(a, b)) in played)
                if best_repeat is None or rep < best_repeat:
                    best_repeat = rep
                    best_j = j
            partner = best_j if best_j >= 0 else (idx + 1 if idx + 1 < len(unpaired) else -1)
            if partner < 0:
                break
        b = unpaired.pop(partner)
        # 座位平衡：先手累计少者本轮先手（color_first=该侧）
        ca = color_counts.get(a, 0)
        cb = color_counts.get(b, 0)
        # color_first=0 表示 a 先手；若 b 先手累计少（更该先手），则 color_first=1
        color_first = 0 if ca <= cb else 1
        out.append(PairingSpec(a, b, round_num=round_num, color_first=color_first))
        idx += 1
    if bye_bot is not None:
        out.append(
            PairingSpec(
                bye_bot,
                None,
                round_num=round_num,
                status="completed",
                requires_match=False,
            )
        )
    return out


def swiss_rounds_needed(n: int) -> int:
    """推荐瑞士轮数 ≈ ceil(log2(n))。"""
    if n <= 2:
        return 1
    return max(1, math.ceil(math.log2(n)))


def swiss_coverage_round_limit(n: int) -> int:
    """Maximum no-repeat Swiss rounds for a complete opponent graph.

    Even cohorts need ``n-1`` rounds.  Odd cohorts need ``n`` rounds because
    every round contains one bye.  ``1`` keeps the historical small-cohort
    estimator well-defined; the manager still treats fewer than two entrants
    as an empty stage.
    """
    if n <= 2:
        return 1
    return n - 1 if n % 2 == 0 else n


def effective_swiss_rounds(stage: dict[str, Any], n: int) -> int:
    """Resolve the frozen/requested Swiss round count for one cohort."""
    frozen = stage.get("effective_rounds")
    if isinstance(frozen, int) and not isinstance(frozen, bool) and frozen >= 1:
        return frozen
    if "swiss_round_bands" in stage:
        raw_bands = stage.get("swiss_round_bands")
        if not isinstance(raw_bands, list) or not raw_bands:
            raise ValueError("swiss_round_bands 须为非空数组")
        previous_max: int | None = 0
        selected: int | None = None
        for band in raw_bands:
            if not isinstance(band, dict) or set(band) != {
                "min_participants",
                "max_participants",
                "rounds",
            }:
                raise ValueError("swiss_round_bands 结构非法")
            minimum = band["min_participants"]
            maximum = band["max_participants"]
            rounds = band["rounds"]
            if (
                isinstance(minimum, bool)
                or not isinstance(minimum, int)
                or minimum < 1
                or isinstance(rounds, bool)
                or not isinstance(rounds, int)
                or rounds < 1
                or (
                    maximum is not None
                    and (
                        isinstance(maximum, bool)
                        or not isinstance(maximum, int)
                        or maximum < minimum
                    )
                )
                or previous_max is None
                or minimum <= previous_max
            ):
                raise ValueError("swiss_round_bands 人数区间非法")
            if minimum <= n and (maximum is None or n <= maximum):
                selected = rounds
            previous_max = maximum
        if selected is not None:
            return selected
    configured = stage.get("rounds")
    if isinstance(configured, int) and not isinstance(configured, bool) and configured > 0:
        base = configured
    else:
        base = swiss_rounds_needed(n)
    if "swiss_extra_rounds" not in stage:
        # Historical/custom snapshots retain their exact rounds contract even
        # when it intentionally repeats opponents beyond full coverage.
        return base
    extra = stage.get("swiss_extra_rounds", 0)
    if isinstance(extra, bool) or not isinstance(extra, int) or extra < 0:
        raise ValueError("swiss_extra_rounds 须为非负整数")
    return min(base + extra, swiss_coverage_round_limit(n))


def generate_stage_pairings(
    stage: dict[str, Any],
    bot_ids: list[int],
    *,
    scores: dict[int, float] | None = None,
    played: set[tuple[int, int]] | None = None,
    swiss_round: int = 1,
    color_counts: dict[int, int] | None = None,
    bye_counts: dict[int, int] | None = None,
) -> list[PairingSpec]:
    stype = stage.get("type") or "round_robin"
    if stype == "round_robin":
        pairings = round_robin(bot_ids, double=False)
        if "games_per_pair" in stage:
            return expand_pairing_series(
                pairings,
                stage["games_per_pair"],
                cohort=bot_ids,
            )
        return pairings
    if stype == "double_round_robin":
        if "games_per_pair" in stage:
            return expand_pairing_series(
                round_robin(bot_ids, double=False),
                stage["games_per_pair"],
                cohort=bot_ids,
            )
        return round_robin(bot_ids, double=True)
    if stype == "group_round_robin":
        return group_round_robin(
            bot_ids,
            group_count=int(stage.get("group_count") or 4),
            double=False,
        )
    if stype == "group_double_round_robin":
        return group_round_robin(
            bot_ids,
            group_count=int(stage.get("group_count") or 4),
            double=True,
        )
    if stype == "swiss":
        pairings = swiss_pairings(
            bot_ids,
            scores=scores,
            played=played,
            round_num=swiss_round,
            color_counts=color_counts,
            bye_counts=bye_counts,
        )
        if "games_per_pair" in stage:
            return expand_pairing_series(
                pairings,
                stage["games_per_pair"],
                cohort=bot_ids,
                preserve_round_num=True,
            )
        return pairings
    if stype == "single_elimination":
        return single_elimination(bot_ids)
    raise ValueError(f"未知阶段类型: {stype}")


def estimate_match_count(stage: dict[str, Any], n: int) -> int:
    stype = stage.get("type") or "round_robin"
    if stype in ("round_robin",):
        return n * (n - 1) // 2 * int(stage.get("games_per_pair") or 1)
    if stype == "double_round_robin":
        if "games_per_pair" in stage:
            return n * (n - 1) // 2 * int(stage["games_per_pair"])
        return n * (n - 1)
    if stype.startswith("group_"):
        g = effective_group_count(n, int(stage.get("group_count") or 4))
        sizes = [n // g + (1 if i < n % g else 0) for i in range(g)]
        double = "double" in stype
        total = 0
        for s in sizes:
            c = s * (s - 1) // 2
            total += c * (2 if double else 1)
        return total
    if stype == "swiss":
        rounds = effective_swiss_rounds(stage, n)
        return rounds * (n // 2) * int(stage.get("games_per_pair") or 1)
    if stype == "single_elimination":
        return max(0, n - 1)
    return 0
