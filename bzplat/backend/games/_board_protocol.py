"""棋类（Gomoku / Pencil）共享的紧凑 JSON 行协议工具。

全面解耦 PR-D：gomoku 与 pencil 的行协议序列化逻辑（dumps/loads/parse_xy）字节相同，
此前各存一份副本（还交叉定义了对方的 builder）——审计发现这是有害重复（改一处忘另一处）。
本模块集中序列化逻辑 + 两个游戏的请求 builder，各游戏 protocol.py 仅 re-export。

这是**平台协议工具**（一行一条 JSON 的序列化），不是游戏规则——共享安全，与
"不要共享游戏逻辑"不冲突。各游戏 rule/tier/result 仍独立。
"""
from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 1


def dumps_request(req: dict[str, Any]) -> str:
    return json.dumps(req, separators=(",", ":"), ensure_ascii=False)


def loads_response(line: str) -> dict[str, Any]:
    line = (line or "").strip()
    if not line:
        return {}
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def parse_xy(raw: dict[str, Any] | None) -> tuple[int | None, int | None]:
    if not isinstance(raw, dict):
        return None, None
    try:
        x = int(raw["x"])
        y = int(raw["y"])
    except (KeyError, TypeError, ValueError):
        return None, None
    return x, y


def build_gomoku_request(*, x: int, y: int, me: int) -> dict[str, Any]:
    return {"v": PROTOCOL_VERSION, "t": "mv", "x": x, "y": y, "me": me}


def build_pencil_request(
    *,
    x: int,
    y: int,
    pass_: int,
    me: int,
    scores: list[int],
) -> dict[str, Any]:
    return {
        "v": PROTOCOL_VERSION,
        "t": "mv",
        "x": x,
        "y": y,
        "pass": int(pass_),
        "me": me,
        "scores": list(scores),
    }


def build_xy_response(x: int, y: int) -> dict[str, int]:
    return {"x": x, "y": y}
