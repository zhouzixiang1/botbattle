"""全游戏唯一的 Botzone JSON 信封协议。

Traditional 与 LongRunning 只是进程生命周期不同，二者共享同一份 JSON
契约：Traditional（以及 LongRunning 首回合）接收完整历史信封，LongRunning
握手后的回合接收单 request 信封；Bot 的每个响应都必须是只含
``response`` 的 JSON 对象。

这里同时提供正式对局与上传预检复用的严格响应解码和 LongRunning 握手校验，
避免各游戏维护一套更宽松的“预检协议”。游戏模块只负责校验 ``response``
负载本身的类型与形状。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from bzplat.backend.store.schema import (
    DEFAULT_RUNTIME_MODE,
    RUNTIME_LONGRUNNING,
    RUNTIME_TRADITIONAL,
    TECHNICAL_INCIDENT_MESSAGES,
    VALID_RUNTIME_MODES,
)

# 对外保留协议模块的常量入口，但值只来自 schema.py 这一处真相源。
RUNTIME_MODES = VALID_RUNTIME_MODES

# LongRunning 首回合响应后的精确握手行（换行符由行传输层剥离）。
KEEP_RUNNING_SIGNAL = ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"


class ResponseProtocolError(ValueError):
    """响应信封或 LongRunning 握手违反唯一现行协议。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _compact(obj: Any) -> str:
    """单行紧凑 JSON（非 ASCII 原样输出）。"""
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def dumps_traditional(requests: list[Any], responses: list[Any]) -> str:
    """构造完整历史信封；顶层字段固定为 requests / responses。"""
    return _compact({"requests": requests, "responses": responses})


def dumps_longrunning_single(request: Any) -> str:
    """构造 LongRunning 握手成功后的单 request 信封。"""
    return _compact({"request": request})


def loads_response(line: str) -> dict[str, Any]:
    """解码并验证唯一响应顶层；游戏 payload 由调用方继续校验。"""
    envelope = json.loads(line)
    extract_response_payload(envelope)
    return envelope


def extract_response_payload(envelope: Any) -> Any:
    """从唯一合法响应信封中取 payload。

    顶层必须恰好为 ``{"response": ...}``；裸响应或任何其他字段一律拒绝。
    """
    if not isinstance(envelope, dict):
        raise ResponseProtocolError(
            "invalid_envelope", TECHNICAL_INCIDENT_MESSAGES["invalid_envelope"]
        )
    if "response" not in envelope:
        raise ResponseProtocolError(
            "missing_response", TECHNICAL_INCIDENT_MESSAGES["missing_response"]
        )
    extra_fields = set(envelope) - {"response"}
    if extra_fields:
        raise ResponseProtocolError(
            "unexpected_fields", TECHNICAL_INCIDENT_MESSAGES["unexpected_fields"]
        )
    return envelope["response"]


def decode_response_payload(
    line: str,
    validate_payload: Callable[[Any], Any],
) -> Any:
    """按唯一协议解码一行 Bot 响应，并校验游戏 payload。"""
    try:
        envelope = loads_response(line)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ResponseProtocolError(
            "invalid_json", TECHNICAL_INCIDENT_MESSAGES["invalid_json"]
        ) from exc
    payload = extract_response_payload(envelope)
    try:
        validate_payload(payload)
    except (TypeError, ValueError, KeyError) as exc:
        raise ResponseProtocolError(
            "invalid_response", TECHNICAL_INCIDENT_MESSAGES["invalid_response"]
        ) from exc
    return payload


def is_keep_running_signal(line: str | None) -> bool:
    """只接受逐字符一致的 LongRunning 握手行。"""
    return line == KEEP_RUNNING_SIGNAL


def require_keep_running_signal(line: str | None) -> None:
    """验证 LongRunning 首回合后的必需握手。"""
    if line is None:
        raise ResponseProtocolError(
            "missing_keep_running",
            TECHNICAL_INCIDENT_MESSAGES["missing_keep_running"],
        )
    if not is_keep_running_signal(line):
        raise ResponseProtocolError(
            "invalid_keep_running",
            TECHNICAL_INCIDENT_MESSAGES["invalid_keep_running"],
        )


async def preflight_exchange(
    binary_path: str,
    binary_runner: Any,
    request: dict[str, Any],
    validate_payload: Callable[[Any], Any],
    *,
    runtime_mode: str,
    timeout: float,
) -> Any:
    """按所选运行模式执行与正式对局一致的首回合交换。

    两种模式的首回合都发送完整历史信封；LongRunning 还必须在响应后输出
    精确握手。返回已经过信封和游戏 payload 双重校验的 ``response`` 值。
    """
    if runtime_mode not in VALID_RUNTIME_MODES:
        raise ValueError(f"未知运行模式: {runtime_mode}")
    sid = await binary_runner.start_session(binary_path, runtime_mode=runtime_mode)
    try:
        response_line = await binary_runner.send(
            sid,
            dumps_traditional([request], []),
            timeout=timeout,
        )
        payload = decode_response_payload(response_line, validate_payload)
        if runtime_mode == RUNTIME_LONGRUNNING:
            extra = await binary_runner.read_extra_line(sid, timeout=1.0)
            require_keep_running_signal(extra)
        return payload
    finally:
        await binary_runner.stop_session(sid)


__all__ = [
    "DEFAULT_RUNTIME_MODE",
    "RUNTIME_TRADITIONAL",
    "RUNTIME_LONGRUNNING",
    "RUNTIME_MODES",
    "KEEP_RUNNING_SIGNAL",
    "ResponseProtocolError",
    "dumps_traditional",
    "dumps_longrunning_single",
    "loads_response",
    "extract_response_payload",
    "decode_response_payload",
    "is_keep_running_signal",
    "require_keep_running_signal",
    "preflight_exchange",
]
