"""赛事阶段对阵生成器。"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass
class PairingSpec:
    bot_a_id: int
    bot_b_id: int
    round_num: int = 1
    group_id: str = ""
    bracket_slot: int | None = None
    color_first: int = 0  # 0 = a 先手/座位0；1 = b 先手


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
                p.bot_b_id,
                p.bot_a_id,
                round_num=p.round_num + rounds,
                group_id=p.group_id,
                color_first=1,
            )
            for p in out
        ]
        out.extend(flipped)
    return out


def snake_groups(bot_ids: list[int], group_count: int) -> dict[str, list[int]]:
    """蛇形种子分组。bot_ids 已按种子排序（强→弱）。"""
    g = max(1, min(group_count, len(bot_ids)))
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
    """首轮单败对阵；bye 直接晋级（不写 pairing，由调用方标 seed）。"""
    bots = list(bot_ids)
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
            # bye：不生成 pairing
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
) -> list[PairingSpec]:
    """Dutch-Swiss 配对（O(N log N)，万人赛单轮秒级）。

    - 按 scores 降序分组（同分组内配对，跨组只在偶数补缺时）
    - 避开已交手（played set）；组内全交手过则跨组找
    - 座位平衡（color_first 轮换：历史先手多的本轮后手）
    - 奇数 N：最低分者 bye（不生成 pairing，上层记轮空分）
    """
    scores = scores or {b: 0.0 for b in bot_ids}
    played = played or set()
    color_counts = color_counts or {}  # bot_id → 累计先手次数（正=先手多）
    # 按 (-score, bot_id) 排序（确定性，积分同按 id）
    ordered = sorted(bot_ids, key=lambda b: (-scores.get(b, 0.0), b))
    # 奇数 N：移除最低分者作 bye（返回的 out 不含它）
    bye_bot = None
    if len(ordered) % 2 == 1:
        bye_bot = ordered.pop()  # 最低分（排序末尾）
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
            # 组内全交手过 → 取下一个（跨组强配，避免卡死）
            if idx + 1 < len(unpaired):
                partner = idx + 1
            else:
                break
        b = unpaired.pop(partner)
        # 座位平衡：先手累计少者本轮先手（color_first=该侧）
        ca = color_counts.get(a, 0)
        cb = color_counts.get(b, 0)
        # color_first=0 表示 a 先手；若 b 先手累计少（更该先手），则 color_first=1
        color_first = 0 if ca <= cb else 1
        out.append(PairingSpec(a, b, round_num=round_num, color_first=color_first))
        idx += 1
    return out


def swiss_rounds_needed(n: int) -> int:
    """推荐瑞士轮数 ≈ ceil(log2(n))。"""
    if n <= 2:
        return 1
    return max(1, math.ceil(math.log2(n)))


def generate_stage_pairings(
    stage: dict[str, Any],
    bot_ids: list[int],
    *,
    scores: dict[int, float] | None = None,
    played: set[tuple[int, int]] | None = None,
    swiss_round: int = 1,
) -> list[PairingSpec]:
    stype = stage.get("type") or "round_robin"
    if stype == "round_robin":
        return round_robin(bot_ids, double=False)
    if stype == "double_round_robin":
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
        return swiss_pairings(
            bot_ids, scores=scores, played=played, round_num=swiss_round
        )
    if stype == "single_elimination":
        return single_elimination(bot_ids)
    raise ValueError(f"未知阶段类型: {stype}")


def estimate_match_count(stage: dict[str, Any], n: int) -> int:
    stype = stage.get("type") or "round_robin"
    if stype in ("round_robin",):
        return n * (n - 1) // 2
    if stype == "double_round_robin":
        return n * (n - 1)
    if stype.startswith("group_"):
        g = max(1, min(int(stage.get("group_count") or 4), n))
        sizes = [n // g + (1 if i < n % g else 0) for i in range(g)]
        double = "double" in stype
        total = 0
        for s in sizes:
            c = s * (s - 1) // 2
            total += c * (2 if double else 1)
        return total
    if stype == "swiss":
        rounds = int(stage.get("rounds") or swiss_rounds_needed(n))
        return rounds * (n // 2)
    if stype == "single_elimination":
        return max(0, n - 1)
    return 0
