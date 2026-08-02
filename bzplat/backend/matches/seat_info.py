"""对局座位身份：JOIN 扁平列 → 嵌套 bot_a/bot_b，供观赛/人类对战 canvas 标签。

人类对局 (match_type=human) 两侧 bot_id 相同，JOIN 得到的是 bot 主人；
人类座位须改写为 human_user 的用户名（否则两边都显示 bot owner）。
"""
from __future__ import annotations

from typing import Any


def with_seat_info(m: dict | None, human_user: dict | None = None) -> dict | None:
    """把 get_match_detailed 的扁平 JOIN 列整理成嵌套 bot_a/bot_b。

    Parameters
    ----------
    m:
        match 行（可含 bot_a_name / bot_a_owner_name 等 JOIN 列）。
    human_user:
        可选 users 行；人类对局时用于覆盖 human_seat 一侧的 owner/name。
    """
    if not m:
        return m
    out = dict(m)
    for k in (
        "bot_a_name",
        "bot_a_display",
        "bot_b_name",
        "bot_b_display",
        "bot_a_owner_name",
        "bot_a_owner_display",
        "bot_b_owner_name",
        "bot_b_owner_display",
    ):
        out.pop(k, None)

    human_seat = m.get("human_seat")
    is_human_match = m.get("match_type") == "human"

    def _seat(side: int) -> dict[str, Any]:
        prefix = "bot_a" if side == 0 else "bot_b"
        seat_is_human = bool(is_human_match and human_seat == side)
        name = m.get(f"{prefix}_name")
        display = m.get(f"{prefix}_display")
        owner_name = m.get(f"{prefix}_owner_name")
        owner_display = m.get(f"{prefix}_owner_display")
        if seat_is_human and human_user:
            # 人类座：显示真人，不复用 bot 主人
            uname = human_user.get("username") or owner_name
            udisp = human_user.get("display_name") or uname
            name = udisp or uname or "人类"
            display = udisp
            owner_name = uname
            owner_display = udisp
        return {
            "id": m.get(f"{prefix}_id") if not seat_is_human else None,
            "name": name,
            "display_name": display,
            "owner_name": owner_name,
            "owner_display": owner_display,
            "is_human": seat_is_human,
        }

    out["bot_a"] = _seat(0)
    out["bot_b"] = _seat(1)
    return out


def match_for_viewer(store: Any, match_id: str) -> dict | None:
    """观赛/订阅/人类 WS 统一入口：detailed JOIN + 嵌套 seats + 人类座修正。"""
    m = store.get_match_detailed(match_id) or store.get_match(match_id)
    if not m:
        return None
    human_user = None
    hid = m.get("human_user_id")
    if hid is not None and m.get("match_type") == "human":
        try:
            human_user = store.get_user(int(hid))
        except Exception:
            human_user = None
    return with_seat_info(m, human_user=human_user)
