"""棋类（Gomoku / Pencil）共享的 Botzone 标准行协议工具。

棋类协议**完全遵循 [Botzone](https://wiki.botzone.org.cn/index.php?title=Bot) 标准**：
- 请求经 Botzone 信封包裹（Traditional 完整历史 / LongRunning 单 request），由
  ``games/_botzone_protocol.py`` + ``matches/runner._botzone_decide`` 传输层处理。
- 请求负载（棋类）：``{x, y, ...}``（gomoku）/ ``{x, y, pass, ...}``（pencil）。
- 响应信封：``{"response": {"x":.., "y":..}}``（Botzone 标准）。

本模块是**平台协议工具**（请求负载 builder + 响应解析），不是游戏规则——共享安全。
各游戏 rule/tier/result 仍独立。gomoku/pencil 的 protocol.py 仅 re-export。
"""
from __future__ import annotations

import json
from typing import Any

PROTOCOL_VERSION = 2  # Botzone 标准（v1 是旧的紧凑协议，已废弃）


def dumps_request(req: dict[str, Any]) -> str:
    """序列化请求负载为单行 JSON（信封化由 runner 传输层做）。"""
    return json.dumps(req, separators=(",", ":"), ensure_ascii=False)


def loads_response(line: str) -> dict[str, Any]:
    """解析 Bot 输出一行（返回信封 dict，payload 由 parse_xy 取）。"""
    line = (line or "").strip()
    if not line:
        return {}
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return obj if isinstance(obj, dict) else {}


def parse_xy(raw: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """从响应取落子坐标 ``(x, y)``。

    接受两种输入（兼容 Botzone 信封 + 旧裸 ``{x,y}``）：
    1. Botzone 信封：``{"response": {"x":.., "y":..}}``。
    2. 裸 ``{"x":.., "y":..}``（测试/兼容）。
    """
    if not isinstance(raw, dict):
        return None, None
    # Botzone 信封：先取 response 字段
    if "response" in raw and isinstance(raw["response"], dict):
        raw = raw["response"]
    try:
        x = int(raw["x"])
        y = int(raw["y"])
    except (KeyError, TypeError, ValueError):
        return None, None
    return x, y


def build_gomoku_request(*, x: int, y: int, me: int) -> dict[str, Any]:
    """gomoku 请求负载（信封由传输层包）。Botzone 标准：对手最近一手 {x,y} + 本方座位 me。

    黑方首手：``x=y=-1``（无上一手）。坐标 0-based。
    """
    return {"x": x, "y": y, "me": me}


def build_pencil_request(
    *,
    x: int,
    y: int,
    pass_: int,
    me: int,
    scores: list[int],
) -> dict[str, Any]:
    """pencil 请求负载（信封由传输层包）。

    ``x,y`` = 对手最近落点（或 -1）；``pass`` = 对手是否 pass；``me`` = 本方座位；
    ``scores`` = [红,蓝] 当前得分。
    """
    return {
        "x": x,
        "y": y,
        "pass": int(pass_),
        "me": me,
        "scores": list(scores),
    }


def build_xy_response(x: int, y: int) -> dict[str, int]:
    """构造落子响应负载（信封由传输层包成 {"response": {x,y}}）。"""
    return {"x": x, "y": y}
