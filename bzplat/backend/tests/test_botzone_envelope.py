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
    # 行传输层会去掉 CRLF；协议层逐字符匹配，不接受额外空白。
    assert not bz.is_keep_running_signal("\n>>>BOTZONE_REQUEST_KEEP_RUNNING<<<\n")
    assert not bz.is_keep_running_signal("  >>>BOTZONE_REQUEST_KEEP_RUNNING<<<  ")
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


def test_request_envelopes_have_one_canonical_shape():
    trad = json.loads(bz.dumps_traditional([1], [2]))
    longrunning = json.loads(bz.dumps_longrunning_single({"turn": 2}))
    assert set(trad) == {"requests", "responses"}
    assert set(longrunning) == {"request"}
    with pytest.raises(TypeError):
        bz.dumps_traditional([1], [2], data={"obsolete": True})


def test_dumps_longrunning_single():
    """LongRunning 单 request 信封：只有 request 字段，无完整历史。"""
    line = bz.dumps_longrunning_single({"hand": 5})
    env = json.loads(line)
    assert env["request"] == {"hand": 5}
    assert "requests" not in env
    assert "responses" not in env


def test_loads_response_and_extract_payload():
    """解析 Bot 输出信封，取 response 字段。"""
    line = json.dumps({"response": -1})
    env = bz.loads_response(line)
    assert bz.extract_response_payload(env) == -1

    line = json.dumps({"response": 250})
    assert bz.extract_response_payload(bz.loads_response(line)) == 250


def test_extract_payload_missing_response_raises():
    """无 response 字段 → 精确协议错误。"""
    env = {"debug": "no response"}
    with pytest.raises(bz.ResponseProtocolError) as raised:
        bz.extract_response_payload(env)
    assert raised.value.code == "missing_response"


@pytest.mark.parametrize("field", ["debug", "data", "globaldata", "a"])
def test_response_envelope_rejects_every_non_response_field(field):
    with pytest.raises(bz.ResponseProtocolError) as raised:
        bz.extract_response_payload({"response": 0, field: "obsolete"})
    assert raised.value.code == "unexpected_fields"


def test_loads_response_invalid_raises():
    with pytest.raises(json.JSONDecodeError):
        bz.loads_response("not json")


@pytest.mark.parametrize("line", ["0", "[]", "{}", '{"response":0,"debug":1}'])
def test_loads_response_itself_enforces_the_unique_top_level(line):
    with pytest.raises(bz.ResponseProtocolError):
        bz.loads_response(line)


def test_traditional_vs_longrunning_envelope_difference():
    """关键差异：Traditional 有 requests[] 数组；LongRunning 后续只有 request。"""
    trad = json.loads(bz.dumps_traditional([{"h": 0}], [-1]))
    longr = json.loads(bz.dumps_longrunning_single({"h": 1}))
    assert "requests" in trad and isinstance(trad["requests"], list)
    assert "request" in longr and not isinstance(longr["request"], list)
