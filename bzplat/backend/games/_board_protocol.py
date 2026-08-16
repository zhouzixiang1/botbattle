"""Pencil 使用的 Botzone 标准坐标行协议工具。

棋类协议**完全遵循 [Botzone](https://wiki.botzone.org.cn/index.php?title=Bot) 标准**：
- 请求经 Botzone 信封包裹（Traditional 完整历史 / LongRunning 单 request），由
  ``games/_botzone_protocol.py`` + ``matches/runner._botzone_decide`` 传输层处理。
- 请求负载：``{x, y, pass, ...}``。
- 响应信封：``{"response": {"x":.., "y":..}}``（Botzone 标准）。

本模块是**平台协议工具**（请求负载 builder + 响应解析），不是游戏规则——共享安全。
它是 Pencil 坐标 JSON 原语的唯一实现，并通过 Pencil GameSpec 的
``shared_source_files`` 随公开裁判源码提供。Gomoku v2 是分阶段动作协议，拥有
独立实现，不能复用本模块或退回旧 ``x/y`` 协议。
"""
from __future__ import annotations

import json
from typing import Any

def dumps_request(req: dict[str, Any]) -> str:
    """序列化请求负载为单行 JSON（信封化由 runner 传输层做）。"""
    return json.dumps(req, separators=(",", ":"), ensure_ascii=False)


def loads_response(line: str) -> dict[str, Any]:
    """解析响应信封并只返回平台消费的规范化坐标。"""
    from bzplat.backend.games import _botzone_protocol as envelope_protocol

    obj = json.loads(line)
    payload = envelope_protocol.extract_response_payload(obj)
    payload = validate_response_payload(payload)
    return {"response": payload}


def parse_xy(raw: dict[str, Any] | None) -> tuple[int | None, int | None]:
    """从响应取落子坐标 ``(x, y)``。

    唯一现行信封必须包含 ``response``；其他顶层字段忽略。游戏 payload
    仍严格为整数 ``x/y``，避免把另一套落子结构带入裁判。
    """
    if not isinstance(raw, dict) or "response" not in raw:
        return None, None
    payload = raw["response"]
    if not isinstance(payload, dict) or set(payload) != {"x", "y"}:
        return None, None
    x, y = payload["x"], payload["y"]
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, int)
        or not isinstance(y, int)
    ):
        return None, None
    return x, y


def validate_response_payload(payload: Any) -> Any:
    """校验棋类 ``response`` 负载形状，不判坐标对应的游戏内合法性。"""
    if not isinstance(payload, dict):
        raise ValueError("response 必须是包含 x/y 的对象")
    if set(payload) != {"x", "y"}:
        raise ValueError("response 必须且仅能包含 x/y 坐标")
    x, y = payload["x"], payload["y"]
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, int)
        or not isinstance(y, int)
    ):
        raise ValueError("response.x/response.y 必须是整数")
    return {"x": x, "y": y}


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
