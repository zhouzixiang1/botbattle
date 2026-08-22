"""全国机器博弈竞赛五子棋 v2 行协议。

传输层仍使用全站唯一 Botzone JSON 信封；本模块只定义五子棋请求负载与
``response`` 的分阶段动作。旧版仅含 ``x/y`` 的响应不再兼容。
"""
from __future__ import annotations

import json
from typing import Any, Iterable

from bzplat.backend.games import _botzone_protocol as envelope_protocol
from bzplat.backend.games.gomoku.gomoku_judge import BLACK5_CANDIDATE_COUNT

RULESET_ID = "gomoku_ccgc_2013_five_move_two_v2"
LEGACY_RULESET_ID = "gomoku_freestyle_v1"
PROTOCOL_VERSION = 2

PHASE_OPENING = "opening_proposal"
PHASE_SWAP = "swap_choice"
PHASE_WHITE4 = "white4"
PHASE_BLACK5_CANDIDATES = "black5_candidates"
PHASE_BLACK5_SELECT = "black5_select"
PHASE_NORMAL = "normal_play"

ACTION_OPENING = "opening"
ACTION_SWAP = "swap"
ACTION_MOVE = "move"
ACTION_BLACK5_CANDIDATES = "black5_candidates"
ACTION_BLACK5_SELECT = "black5_select"
ACTION_PASS = "pass"

PHASES = frozenset(
    {
        PHASE_OPENING,
        PHASE_SWAP,
        PHASE_WHITE4,
        PHASE_BLACK5_CANDIDATES,
        PHASE_BLACK5_SELECT,
        PHASE_NORMAL,
    }
)


def _point(value: Any, *, field: str = "坐标") -> dict[str, int]:
    if not isinstance(value, dict) or set(value) != {"x", "y"}:
        raise ValueError(f"{field}必须且只能包含 x/y")
    x, y = value["x"], value["y"]
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, int)
        or not isinstance(y, int)
    ):
        raise ValueError(f"{field}.x/{field}.y 必须是整数")
    return {"x": x, "y": y}


def _require_exact(payload: dict[str, Any], fields: set[str], action: str) -> None:
    if set(payload) != fields:
        expected = "/".join(sorted(fields))
        raise ValueError(f"{action} 动作字段必须且只能为 {expected}")


def validate_response_payload(payload: Any) -> dict[str, Any]:
    """校验 v2 动作的判别联合；值域与阶段合法性由裁判负责。"""
    if not isinstance(payload, dict):
        raise ValueError("response 必须是动作对象")
    action = payload.get("action")
    if not isinstance(action, str):
        raise ValueError("response.action 必须是字符串")

    if action == ACTION_OPENING:
        _require_exact(payload, {"action", "white2", "black3", "n"}, action)
        n = payload["n"]
        if isinstance(n, bool) or not isinstance(n, int):
            raise ValueError("opening.n 必须是整数")
        return {
            "action": action,
            "white2": _point(payload["white2"], field="white2"),
            "black3": _point(payload["black3"], field="black3"),
            "n": n,
        }

    if action == ACTION_SWAP:
        _require_exact(payload, {"action", "swap"}, action)
        if not isinstance(payload["swap"], bool):
            raise ValueError("swap.swap 必须是布尔值")
        return {"action": action, "swap": payload["swap"]}

    if action == ACTION_MOVE:
        _require_exact(payload, {"action", "x", "y"}, action)
        point = _point({"x": payload["x"], "y": payload["y"]})
        return {"action": action, **point}

    if action == ACTION_BLACK5_CANDIDATES:
        _require_exact(payload, {"action", "points"}, action)
        points = payload["points"]
        if not isinstance(points, list):
            raise ValueError("black5_candidates.points 必须是坐标列表")
        normalized = [_point(point, field="points[]") for point in points]
        # 候选数必须为固定二打；坐标重复、占用以及相对当前四子盘面是否
        # “不同形”都是有状态的游戏规则，统一留给纯裁判判定。
        return {"action": action, "points": normalized}

    if action == ACTION_BLACK5_SELECT:
        _require_exact(payload, {"action", "index"}, action)
        index = payload["index"]
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError("black5_select.index 必须是整数")
        return {"action": action, "index": index}

    if action == ACTION_PASS:
        _require_exact(payload, {"action"}, action)
        return {"action": action}

    raise ValueError(f"未知五子棋动作: {action!r}")


def dumps_request(request: dict[str, Any]) -> str:
    return json.dumps(request, separators=(",", ":"), ensure_ascii=False)


def loads_response(line: str) -> dict[str, Any]:
    obj = json.loads(line)
    payload = envelope_protocol.extract_response_payload(obj)
    return {"response": validate_response_payload(payload)}


def parse_action(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """从标准响应信封提取动作；直接裁判调用遇到坏形状时返回 ``None``。"""
    if not isinstance(raw, dict) or "response" not in raw:
        return None
    try:
        return validate_response_payload(raw["response"])
    except (TypeError, ValueError, KeyError):
        return None


def build_request(
    *,
    phase: str,
    me: int,
    color: int | None,
    board: list[list[int]],
    seat_colors: Iterable[int],
    n: int | None = None,
    candidates: list[dict[str, int]] | None = None,
    last: dict[str, int] | None = None,
    pass_allowed: bool = False,
) -> dict[str, Any]:
    """构造一个自包含的 v2 请求，便于 Traditional 与 LongRunning 共用。"""
    if phase not in PHASES:
        raise ValueError(f"未知五子棋阶段: {phase}")
    colors = list(seat_colors)
    if len(colors) != 2 or sorted(colors) != [0, 1]:
        raise ValueError("seat_colors 必须是黑白颜色的双射")
    if n is not None and (
        isinstance(n, bool)
        or not isinstance(n, int)
        or n != BLACK5_CANDIDATE_COUNT
    ):
        raise ValueError(
            f"五手候选数固定为 {BLACK5_CANDIDATE_COUNT}，不能设为 {n!r}"
        )
    request: dict[str, Any] = {
        "protocol_version": PROTOCOL_VERSION,
        "ruleset": RULESET_ID,
        "phase": phase,
        "me": me,
        "color": color,
        "seat_colors": colors,
        "board": [list(column) for column in board],
        "pass_allowed": bool(pass_allowed),
    }
    if phase == PHASE_OPENING:
        request["fixed_black1"] = {"x": 7, "y": 7}
        request["n_range"] = [
            BLACK5_CANDIDATE_COUNT,
            BLACK5_CANDIDATE_COUNT,
        ]
    elif n is not None:
        request["n"] = BLACK5_CANDIDATE_COUNT
    if candidates is not None:
        request["candidates"] = [dict(point) for point in candidates]
    if last is not None:
        request["last"] = dict(last)
    return request


def fail_response() -> dict[str, Any]:
    """人类逐回合保护超时时返回的确定性非法动作。"""
    return {"action": ACTION_MOVE, "x": -99, "y": -99}


__all__ = [
    "RULESET_ID",
    "LEGACY_RULESET_ID",
    "PROTOCOL_VERSION",
    "PHASE_OPENING",
    "PHASE_SWAP",
    "PHASE_WHITE4",
    "PHASE_BLACK5_CANDIDATES",
    "PHASE_BLACK5_SELECT",
    "PHASE_NORMAL",
    "ACTION_OPENING",
    "ACTION_SWAP",
    "ACTION_MOVE",
    "ACTION_BLACK5_CANDIDATES",
    "ACTION_BLACK5_SELECT",
    "ACTION_PASS",
    "build_request",
    "dumps_request",
    "loads_response",
    "parse_action",
    "validate_response_payload",
    "fail_response",
]
