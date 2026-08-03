"""Botzone 标准信封传输层单测（games/_botzone_protocol.py）。

覆盖：Traditional 完整历史信封、LongRunning 单 request 信封、keep_running 握手识别、
信封解析与 payload 提取。
"""
from __future__ import annotations

import json

import pytest

from bzplat.backend.games import _botzone_protocol as bz


def test_runtime_mode_constants():
    assert bz.RUNTIME_TRADITIONAL == "traditional"
    assert bz.RUNTIME_LONGRUNNING == "longrunning"
    assert bz.RUNTIME_MODES == frozenset({"traditional", "longrunning"})


def test_keep_running_signal():
    assert bz.KEEP_RUNNING_SIGNAL == ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"
    assert bz.is_keep_running_signal(bz.KEEP_RUNNING_SIGNAL)
    # 带空白/换行仍识别
    assert bz.is_keep_running_signal("\n>>>BOTZONE_REQUEST_KEEP_RUNNING<<<\n")
    assert bz.is_keep_running_signal("  >>>BOTZONE_REQUEST_KEEP_RUNNING<<<  ")
    assert not bz.is_keep_running_signal('{"response":0}')
    assert not bz.is_keep_running_signal("")


def test_dumps_traditional_envelope():
    """Traditional 信封含 requests[]/responses[] 完整历史。"""
    reqs = [{"hand": 0}, {"hand": 1}]
    resps = [-1, 0]
    line = bz.dumps_traditional(reqs, resps)
    env = json.loads(line)
    assert env["requests"] == reqs
    assert env["responses"] == resps
    # 紧凑（无空白）
    assert " " not in line


def test_dumps_traditional_with_data():
    line = bz.dumps_traditional([1], [2], data={"k": "v"}, globaldata={"g": 1})
    env = json.loads(line)
    assert env["data"] == {"k": "v"}
    assert env["globaldata"] == {"g": 1}


def test_dumps_longrunning_single():
    """LongRunning 单 request 信封：只有 request 字段，无完整历史。"""
    line = bz.dumps_longrunning_single({"hand": 5})
    env = json.loads(line)
    assert env["request"] == {"hand": 5}
    assert "requests" not in env
    assert "responses" not in env


def test_loads_response_and_extract_payload():
    """解析 Bot 输出信封，取 response 字段。"""
    line = json.dumps({"response": -1, "debug": "x"})
    env = bz.loads_response(line)
    assert bz.extract_response_payload(env) == -1

    line = json.dumps({"response": 250})
    assert bz.extract_response_payload(bz.loads_response(line)) == 250


def test_extract_payload_missing_response_raises():
    """无 response 字段 → KeyError（交上游兜底）。"""
    env = {"debug": "no response"}
    with pytest.raises(KeyError):
        bz.extract_response_payload(env)


def test_loads_response_invalid_raises():
    with pytest.raises(json.JSONDecodeError):
        bz.loads_response("not json")


def test_traditional_vs_longrunning_envelope_difference():
    """关键差异：Traditional 有 requests[] 数组；LongRunning 后续只有 request。"""
    trad = json.loads(bz.dumps_traditional([{"h": 0}], [-1]))
    longr = json.loads(bz.dumps_longrunning_single({"h": 1}))
    assert "requests" in trad and isinstance(trad["requests"], list)
    assert "request" in longr and not isinstance(longr["request"], list)
