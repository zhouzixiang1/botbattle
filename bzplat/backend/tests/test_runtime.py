"""二进制分类与样例 bot 对局冒烟。"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from bzplat.backend.bots.classify import classify_binary
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.games.holdem import protocol as proto
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
    from bzplat.backend.games.holdem.cards import Card

    req = proto.build_act_request(
        hand=0,
        total_hands=70,
        my_id=0,
        dealer_id=0,
        my_cards=[Card(12, 0), Card(11, 1)],
        board=[],
        history=[],
        my_chips=20000,
    )
    line = proto.dumps_request(req)
    # Botzone 全名字段
    assert '"num_players":2' in line
    assert '"dealer_id":0' in line
    assert '"my_cards":[' in line
    # 裸整数 response 解析（信封 + 裸 int 两种）
    action, _x = proto.parse_response({"response": 0})
    assert action == "call"
    action, _x = proto.parse_response(0)
    assert action == "call"


def test_orchestrator_resolves_holdem_winner_non_null():
    """回归：holdem 多手对局 winner 不得被 match_end.winner=None 覆盖成平局。

    场景：foldbot vs callbot（foldbot 每手弃牌输盲注，70 手后 callbot 净筹码远高）。
    orchestrator 应按 ea/eb 判 winner=1（callbot 胜），而非 None（平局）。
    根因修复（L298-303）：仅当 match_end.winner 非 None 才覆盖兜底判定的 winner。
    """
    import os
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    foldbot = SAMPLES / "holdem_bots" / "foldbot"
    callbot = ELF
    if not foldbot.is_file() or not callbot.is_file():
        pytest.skip("foldbot/callbot binary missing")

    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.store import Store
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store = Store(str(td + "/w.db"))
        u = store.create_user("wintest", "w@e.com", "x")
        ba = store.create_bot(u["id"], "foldbotA", binary_path=str(foldbot), format="elf", game_id="holdem")
        bb = store.create_bot(u["id"], "callbotB", binary_path=str(callbot), format="elf", game_id="holdem")
        store.ensure_rating(ba["id"]); store.ensure_rating(bb["id"])
        orch = MatchOrchestrator(store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
        import asyncio

        async def _run():
            mid = await orch.challenge(ba["id"], bb["id"], u["id"], match_config={"hands": 10}, game_id="holdem")
            task = orch._tasks.get(mid)
            if task:
                await asyncio.wait_for(task, timeout=60)
            return mid

        mid = asyncio.run(_run())
        m = store.get_match(mid)
        # foldbot 每手弃 → callbot 净筹码高 → winner 应是 1（非 None 平局）
        assert m["winner"] is not None, (
            f"holdem 多手 winner 不应是 None（平局）；result={m.get('result')}"
        )
        assert m["winner"] == 1, f"callbot 应胜（foldbot 每手弃），winner={m['winner']}"
        store.close()


def test_challenge_validates_match_params_per_game():
    """PR3：challenge 经 spec.validate_match_params 校验 hands 范围（取代 API 层 le=70）。

    原 api_routes Field(le=70) 把 holdem 上限泄漏成所有游戏上限；现放宽到 le=1000，
    真正的范围校验交给 spec.validate_match_params（holdem 1-500）。超范围应 ValueError。
    """
    import os
    import tempfile

    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.store import Store

    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    with tempfile.TemporaryDirectory() as td:
        store = Store(str(td + "/v.db"))
        u = store.create_user("vtest", "v@e.com", "x")
        # 用 callable bot 避免 ELF 依赖（本测试只验校验，不真跑）
        ba = store.create_bot(u["id"], "vbotA", binary_path="/dev/null", format="elf", game_id="holdem")
        bb = store.create_bot(u["id"], "vbotB", binary_path="/dev/null", format="elf", game_id="holdem")
        store.ensure_rating(ba["id"]); store.ensure_rating(bb["id"])
        orch = MatchOrchestrator(store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)

        # holdem hands 超出 500 → ValueError（spec.validate_match_params 校验）
        with pytest.raises(ValueError, match="match 参数非法"):
            asyncio.run(orch.challenge(ba["id"], bb["id"], u["id"], match_config={"hands": 999}, game_id="holdem"))
        # hands=0 非法
        with pytest.raises(ValueError, match="match 参数非法"):
            asyncio.run(orch.challenge(ba["id"], bb["id"], u["id"], match_config={"hands": 0}, game_id="holdem"))
        # 合法 hands=100 不抛（校验通过；之后真跑会因 /dev/null 失败，但那不是本测试关注）
        try:
            asyncio.run(orch.challenge(ba["id"], bb["id"], u["id"], match_config={"hands": 100}, game_id="holdem"))
        except ValueError as e:
            assert "match 参数非法" not in str(e), "合法 hands 不应触发校验失败"
        except Exception:
            pass  # /dev/null 跑不起来是预期的，校验已过即可
        store.close()
