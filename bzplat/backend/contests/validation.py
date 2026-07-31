"""赛制模板与阶段配置校验。

非法配置抛 ValueError（API 层转 HTTP 400），供 CRUD / preview / 建赛复用，
确保写入表的赛制始终可被 stages.py 的对阵生成器与 manager 状态机消费。
"""
from __future__ import annotations

import re
from typing import Any

from bzplat.backend.store.schema import VALID_GAME_IDS

# 阶段类型（与 stages.generate_stage_pairings 对齐）
STAGE_TYPES = {
    "round_robin",
    "double_round_robin",
    "group_round_robin",
    "group_double_round_robin",
    "swiss",
    "single_elimination",
}
GROUP_TYPES = {"group_round_robin", "group_double_round_robin"}
SCORINGS = {"poker_3_1_0", "ccgc_2_1_0"}

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


def validate_template_id(tid: str) -> None:
    if not isinstance(tid, str) or not _ID_RE.match(tid):
        raise ValueError(
            f"模板 id 非法：{tid!r}（须以字母开头、仅小写字母数字下划线、2–32 字符）"
        )


def validate_match_config(cfg: Any, game_id: str) -> dict:
    """校验并返回规整后的 match_config。"""
    if not isinstance(cfg, dict):
        raise ValueError("match_config 必须是对象")
    gid = (game_id or "holdem").strip().lower()
    out: dict[str, Any] = {}
    if gid == "holdem":
        hands = cfg.get("hands", 70)
        if not isinstance(hands, int) or not (1 <= hands <= 500):
            raise ValueError(f"holdem match_config.hands 须为 1–500 的整数（得到 {hands}）")
        out["hands"] = hands
    elif gid == "pencil":
        n_dots = cfg.get("n_dots", 11)
        if not isinstance(n_dots, int) or not (3 <= n_dots <= 15):
            raise ValueError(f"pencil match_config.n_dots 须为 3–15 的整数（得到 {n_dots}）")
        out["n_dots"] = n_dots
    elif gid == "gomoku":
        # gomoku 单局，无可调参数；忽略任何字段
        pass
    # 未知 game_id 在 validate_template 阶段已拦截
    return out


def validate_stage(stage: dict, idx: int) -> dict:
    """校验并返回规整后的单个阶段配置。"""
    if not isinstance(stage, dict):
        raise ValueError(f"阶段 {idx + 1} 必须是对象")
    stype = stage.get("type") or "round_robin"
    if stype not in STAGE_TYPES:
        raise ValueError(
            f"阶段 {idx + 1} type 非法：{stype!r}（允许 {sorted(STAGE_TYPES)}）"
        )
    out: dict[str, Any] = {
        "key": str(stage.get("key") or f"stage{idx + 1}"),
        "type": stype,
        "scoring": stage.get("scoring") or "poker_3_1_0",
    }
    if out["scoring"] not in SCORINGS:
        raise ValueError(
            f"阶段 {idx + 1} scoring 非法：{out['scoring']!r}（允许 {sorted(SCORINGS)}）"
        )

    # group_* 专属
    if stype in GROUP_TYPES:
        gc = stage.get("group_count", 4)
        if not isinstance(gc, int) or gc < 1:
            raise ValueError(f"阶段 {idx + 1} group_count 须为 ≥1 的整数")
        out["group_count"] = gc
        if stage.get("advance_per_group") is not None:
            apg = stage["advance_per_group"]
            if not isinstance(apg, int) or apg < 1:
                raise ValueError(f"阶段 {idx + 1} advance_per_group 须为 ≥1 的整数")
            out["advance_per_group"] = apg
    else:
        if stage.get("group_count") is not None:
            raise ValueError(f"阶段 {idx + 1} group_count 仅对分组阶段有效")

    # swiss 专属
    if stype == "swiss":
        rounds = stage.get("rounds", 0)
        if not isinstance(rounds, int) or rounds < 0:
            raise ValueError(f"阶段 {idx + 1} rounds 须为 ≥0 的整数（0=按 log2(n) 自动）")
        out["rounds"] = rounds
        if stage.get("advance_count") is not None:
            ac = stage["advance_count"]
            if not isinstance(ac, int) or ac < 1:
                raise ValueError(f"阶段 {idx + 1} advance_count 须为 ≥1 的整数")
            out["advance_count"] = ac

    # 通用可选
    if stage.get("advance_count") is not None and stype != "swiss":
        ac = stage["advance_count"]
        if not isinstance(ac, int) or ac < 1:
            raise ValueError(f"阶段 {idx + 1} advance_count 须为 ≥1 的整数")
        out["advance_count"] = ac
    if stage.get("rest_after_minutes") is not None:
        rm = stage["rest_after_minutes"]
        if not isinstance(rm, int) or rm < 0:
            raise ValueError(f"阶段 {idx + 1} rest_after_minutes 须为 ≥0 的整数")
        out["rest_after_minutes"] = rm
    if stage.get("allow_bot_swap_in_rest") is not None:
        out["allow_bot_swap_in_rest"] = bool(stage["allow_bot_swap_in_rest"])
    return out


def validate_template(
    tid: str, name: str, game_id: str, match_config: Any, stages: Any
) -> dict:
    """完整校验一个模板；返回规整后的 {id,name,game_id,match_config,stages}。"""
    validate_template_id(tid)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("模板 name 不可为空")
    gid = (game_id or "holdem").strip().lower()
    if gid not in VALID_GAME_IDS:
        raise ValueError(f"game_id 非法：{gid!r}（允许 {sorted(VALID_GAME_IDS)}）")
    if not isinstance(stages, list) or len(stages) == 0:
        raise ValueError("stages 须为非空数组")
    norm_stages = [validate_stage(s, i) for i, s in enumerate(stages)]
    norm_mc = validate_match_config(match_config or {}, gid)
    return {
        "id": tid,
        "name": name.strip(),
        "game_id": gid,
        "match_config": norm_mc,
        "stages": norm_stages,
    }


__all__ = [
    "STAGE_TYPES",
    "SCORINGS",
    "validate_template_id",
    "validate_match_config",
    "validate_stage",
    "validate_template",
]
