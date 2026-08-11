"""Helpers for the durable execution-request API used by QA scripts.

Challenge and human-match creation returns an opaque ``public_id`` with HTTP
202.  The match does not exist until the global dispatcher admits the request,
so callers must poll the owner-scoped execution-request endpoint first.
"""
from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

ExecutionFetch = Callable[[str], tuple[int, Any, str]]

_ACTIVE_STATES = frozenset({"queued", "starting", "running", "settling"})
_TERMINAL_STATES = frozenset({"completed", "cancelled", "interrupted"})


class ExecutionRequestError(RuntimeError):
    """The execution request failed its public API contract or terminated."""


def execution_request_path(public_id: str) -> str:
    """Build the detail path while treating ``public_id`` as fully opaque."""
    return f"/api/execution-requests/{quote(public_id, safe='')}"


def require_execution_request(
    status_code: int,
    payload: Any,
    *,
    label: str,
    detail: str = "",
) -> dict[str, Any]:
    """Validate a 202 response without making assumptions about the ID format."""
    if status_code != 202:
        suffix = detail or repr(payload)[:240]
        raise ExecutionRequestError(
            f"{label} 未被执行队列接受：HTTP {status_code}: {suffix}"
        )
    if not isinstance(payload, dict):
        raise ExecutionRequestError(f"{label} 的 202 响应不是对象：{payload!r}")
    public_id = payload.get("public_id")
    request = payload.get("request")
    if not isinstance(public_id, str) or not public_id:
        raise ExecutionRequestError(f"{label} 的 202 响应缺少 opaque public_id")
    if not isinstance(request, dict):
        raise ExecutionRequestError(
            f"{label} 执行请求 {public_id} 的响应缺少 request 对象"
        )
    if request.get("public_id") not in (None, public_id):
        raise ExecutionRequestError(
            f"{label} 执行请求标识不一致：{public_id!r} != "
            f"{request.get('public_id')!r}"
        )
    return payload


def wait_for_execution_match(
    initial: dict[str, Any],
    fetch: ExecutionFetch,
    *,
    label: str,
    timeout: float = 120,
    interval: float = 0.5,
) -> str:
    """Poll until admission creates a match, or raise with terminal diagnostics."""
    public_id = str(initial["public_id"])
    snapshot: Any = initial
    deadline = time.monotonic() + timeout

    while True:
        if not isinstance(snapshot, dict) or not isinstance(
            snapshot.get("request"), dict
        ):
            raise ExecutionRequestError(
                f"{label} 执行请求 {public_id} 的轮询响应缺少 request 对象"
            )
        request = snapshot["request"]
        status = str(request.get("status") or "")
        reason = str(request.get("reason") or "")
        match_id = request.get("match_id")

        if status in {"cancelled", "interrupted"}:
            raise ExecutionRequestError(
                f"{label} 执行请求 {public_id} 已{status}，"
                f"reason={reason or '-'} retryable={bool(request.get('retryable'))} "
                f"cancel_requested={bool(request.get('cancel_requested'))}"
            )
        # A running request may expose match_id while a safe cancellation is
        # still draining.  Do not hand that match to callers as a success.
        if match_id not in (None, "") and not request.get("cancel_requested"):
            return str(match_id)
        if status == "completed":
            raise ExecutionRequestError(
                f"{label} 执行请求 {public_id} completed 但没有 match_id，"
                f"reason={reason or '-'}"
            )
        if status not in _ACTIVE_STATES | _TERMINAL_STATES:
            raise ExecutionRequestError(
                f"{label} 执行请求 {public_id} 返回未知状态 {status!r}"
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"{label} 执行请求 {public_id} 在 {timeout:.1f}s 内未生成 match_id："
                f"status={status or '-'} ahead_jobs={snapshot.get('ahead_jobs', '-')} "
                f"reason={reason or '-'} "
                f"cancel_requested={bool(request.get('cancel_requested'))}"
            )
        time.sleep(min(interval, remaining))
        try:
            poll_status, poll_payload, poll_detail = fetch(public_id)
        except Exception as exc:
            raise ExecutionRequestError(
                f"{label} 执行请求 {public_id} 轮询失败：{type(exc).__name__}: {exc}"
            ) from exc
        if poll_status != 200:
            raise ExecutionRequestError(
                f"{label} 执行请求 {public_id} 轮询失败："
                f"HTTP {poll_status}: {poll_detail or repr(poll_payload)[:240]}"
            )
        snapshot = poll_payload
