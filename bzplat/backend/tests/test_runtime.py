"""二进制分类与样例 bot 对局冒烟。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bzplat.backend.bots.classify import classify_binary
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.protocol import json_protocol as proto
from bzplat.backend.runtime.binary_runner import BinaryRunner

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
ELF = SAMPLES / "callbot_linux_amd64"


def test_classify_elf():
    data = ELF.read_bytes()
    info = classify_binary(data)
    assert info.format == "elf"
    assert info.runnable
    assert info.arch in ("amd64", "arm64", "i386")


def test_classify_macho_rejected():
    data = b"\xfe\xed\xfa\xce" + b"\x00" * 20
    info = classify_binary(data)
    assert info.format == "macho"
    assert not info.runnable


def test_match_two_callbots_short():
    if not ELF.is_file():
        pytest.skip("sample ELF missing")
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    result = asyncio.run(runner.run_binaries(str(ELF), str(ELF), num_hands=2, seed=1))
    assert result.hands_played == 2
    # Botzone 计分：final_chips = 累计净输赢 net（零和），不再守恒于 2*STARTING_STACK
    assert sum(result.final_chips) == 0


def test_protocol_roundtrip():
    from bzplat.backend.engine.cards import Card

    req = proto.build_act_request(
        hand=0,
        total_hands=70,
        my_id=0,
        dealer_or_sb=0,
        my_cards=[Card(12, 0), Card(11, 1)],
        board=[],
        hist=[],
        my_chips=20000,
        opp_chips=20000,
        sb=50,
        bb=100,
        to_call=50,
    )
    line = proto.dumps_request(req)
    assert '"t":"act"' in line
    action, _x = proto.parse_response({"a": "c"})
    assert action == "call"
