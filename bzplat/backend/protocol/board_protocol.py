"""棋类（Gomoku / Pencil）紧凑 JSON 行协议。

与德州共用「一行一条 JSON」长驻 stdin/stdout 模型；字段语义对齐 Botzone。
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
