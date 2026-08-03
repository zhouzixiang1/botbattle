"""Botzone 标准 JSON 信封传输层（全游戏共享）。

Botzone 协议（参考 https://wiki.botzone.org.cn/index.php?title=Bot）有两套运行模式：

- **Traditional（传统）**：每回合进程重启，平台每次发**完整历史**信封
  ``{"requests":[...], "responses":[...], "data":..., "globaldata":...}``，
  Bot 自己重放历史重建状态。Bot 回 ``{"response":..., "data":..., "debug":...}``。
- **LongRunning（长驻）**：进程长驻不重启。**首回合**仍用 Traditional 完整历史信封；
  Bot 首回合响应后额外输出一行握手串 ``>>>BOTZONE_REQUEST_KEEP_RUNNING<<<``
  （前后各带换行）声明它想长驻。之后每回合平台只发**单条 request** 信封
  ``{"request":...}``，Bot 自行在内存里维护状态。握手后 ``data``/``globaldata``/``debug``
  字段失效。

本平台默认长驻模式（进程不重启，每回合一行）——这正好对应 Botzone LongRunning，
但**首回合也只发单 request**（我们不像 Botzone 那样冷启动重放历史）。
因此平台对两种 Bot 的兼容方式是：

- Traditional Bot：平台每回合给它发**累积完整历史**信封（requests[]/responses[]），
  Bot 自己重放。平台内部仍维护同一进程——对 Traditional Bot 来说每回合的 requests
  足够它重建状态（它不依赖进程重启）。
- LongRunning Bot：首回合发完整历史信封，Bot 回完响应后回读握手串；之后每回合
  发单 request 信封。

各游戏不直接处理信封——它只产出/消费**游戏负载**（holdem 的 act request dict、
棋类的 {x,y}）。本模块负责把负载包进/取出信封。各游戏的 ``protocol.py`` 调用本模块。
"""

from __future__ import annotations

import json
from typing import Any

# 运行模式常量（上传时标明，runner 据此选传输路径）。
RUNTIME_TRADITIONAL = "traditional"
RUNTIME_LONGRUNNING = "longrunning"
RUNTIME_MODES = frozenset({RUNTIME_TRADITIONAL, RUNTIME_LONGRUNNING})

# LongRunning 握手串（Bot 首回合响应后输出此行声明长驻；前后各带换行）。
# 参考 Botzone wiki：note that there should be newline characters before and after.
KEEP_RUNNING_SIGNAL = ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"


def _compact(obj: Any) -> str:
    """单行紧凑 JSON（separators 去空白，非 ASCII 原样输出）。"""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


# ── 信封构造 ──────────────────────────────────────────────────────────────

def dumps_traditional(
    requests: list[Any],
    responses: list[Any],
    *,
    data: Any = None,
    globaldata: Any = None,
) -> str:
    """Traditional 信封：完整历史（requests[] + responses[]）。

    用于 Traditional Bot 的每一回合，以及 LongRunning Bot 的**首回合**。
    """
    envelope: dict[str, Any] = {"requests": requests, "responses": responses}
    if data is not None:
        envelope["data"] = data
    if globaldata is not None:
        envelope["globaldata"] = globaldata
    return _compact(envelope)


def dumps_longrunning_single(request: Any, *, data: Any = None) -> str:
    """LongRunning 单条 request 信封（首回合握手之后用）。

    Botzone wiki：subsequent rounds "only have one round of game information (request)"。
    """
    envelope: dict[str, Any] = {"request": request}
    if data is not None:
        envelope["data"] = data
    return _compact(envelope)


# ── 信封解析 ──────────────────────────────────────────────────────────────

def loads_response(line: str) -> dict[str, Any]:
    """解析 Bot 输出的一行 JSON 信封 → ``{"response":..., "data":..., "debug":...}``。

    不要求字段齐全——只保证返回 dict（供 :func:`extract_response_payload` 取负载）。
    """
    return json.loads(line)


def extract_response_payload(envelope: dict[str, Any]) -> Any:
    """从信封取 ``response`` 字段（Bot 本回合的决策负载）。

    Botzone 信封里 ``response`` 是必填字段；缺它视为协议违规（交给上游兜底）。
    """
    if not isinstance(envelope, dict):
        raise ValueError("响应信封不是对象")
    return envelope["response"]


def is_keep_running_signal(line: str) -> bool:
    """判断一行是否为 LongRunning 握手串。"""
    return line.strip() == KEEP_RUNNING_SIGNAL
