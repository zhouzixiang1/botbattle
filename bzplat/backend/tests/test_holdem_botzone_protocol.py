"""德州扑克 Botzone 协议集成测试。

覆盖：
1. 牌编码 0-51（Botzone 花色映射）；
2. raise delta↔total 转换（引擎边界）；
3. history 对象格式（round/player_id/action/action_type）；
4. 信封双模式（traditional/longrunning）经 _botzone_decide 传输；
5. 真实样例 Bot（编译后 ELF）端到端跑通整场（70 手）。
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest

from bzplat.backend.games import _botzone_protocol as bz
from bzplat.backend.games.holdem import protocol as proto
from bzplat.backend.games.holdem.holdem_judge import Card, Suit
from bzplat.backend.matches.runner import MatchRunner, _botzone_decide
from bzplat.backend.runtime.binary_runner import BinaryRunner

SAMPLES = Path(__file__).resolve().parents[3] / "samples"


# ── 牌编码 ────────────────────────────────────────────────────────────

def test_card_encoding_botzone_suit_order():
    """Botzone: card % 4 == 0 → ♥, 1 → ♦, 2 → ♠, 3 → ♣。裁判 Card 花色编码同此。"""
    # 2♠ (Suit.SPADE=2, number2) → (2-2)*4+2 = 2（%4=2 → ♠ ✓）
    assert proto.encode_card(Card(Suit.SPADE, 2)) == 2
    # 2♥ (Suit.HEART=0, number2) → (2-2)*4+0 = 0（%4=0 → ♥ ✓）
    assert proto.encode_card(Card(Suit.HEART, 2)) == 0
    # A♣ (Suit.CLUB=3, number14) → (14-2)*4+3 = 51
    assert proto.encode_card(Card(Suit.CLUB, 14)) == 51


def test_card_decode_botzone_formula():
    """Botzone poker rank = card // 4 + 2（2..14）。"""
    assert proto.encode_card(Card(Suit.HEART, 2)) // 4 + 2 == 2      # '2'
    assert proto.encode_card(Card(Suit.HEART, 14)) // 4 + 2 == 14    # 'A'


def test_card_roundtrip_all_52():
    for number in range(2, 15):
        for suit in Suit:
            c = Card(suit, number)
            assert proto.decode_card(proto.encode_card(c)) == c


# ── raise delta 转换 ──────────────────────────────────────────────────

def test_raise_delta_conversion():
    """Bot 返回 raise delta；引擎转 raise_to = street_bet + delta。"""
    action, delta = proto.parse_response({"response": 150})
    assert action == "raise" and delta == 150
    with pytest.raises(ValueError):
        proto.parse_response(150)


def test_response_codes():
    assert proto.parse_response({"response": -1})[0] == "fold"
    assert proto.parse_response({"response": -2})[0] == "allin"
    assert proto.parse_response({"response": 0})[0] == "call"


def test_action_to_history_int_raise_delta():
    """history.action：raise 存 delta（额外量）。"""
    assert proto.action_to_history_int("raise", 250) == 250
    assert proto.action_to_history_int("fold", None) == -1
    assert proto.action_to_history_int("allin", None) == -2


# ── history 对象格式 ──────────────────────────────────────────────────

def test_build_request_history_format():
    req = proto.build_act_request(
        hand=0, total_hands=70, my_id=0, dealer_id=0,
        my_cards=[Card(Suit.SPADE, 14), Card(Suit.HEART, 14)], board=[Card(Suit.SPADE, 5)],
        history=[
            {"round": 0, "player_id": 0, "action": 50, "action_type": "raise"},
            {"round": 1, "player_id": 1, "action": -1, "action_type": "fold"},
        ],
        my_chips=19900,
    )
    h = req["history"]
    assert h[0] == {"round": 0, "player_id": 0, "action": 50, "action_type": "raise"}
    assert h[1]["action_type"] == "fold"
    assert h[1]["action"] == -1


# ── _botzone_decide 双模式传输 ─────────────────────────────────────────

class _FakeSession:
    """模拟 BotSession 协议状态。"""
    def __init__(self, *, runtime_mode="longrunning", turn=0, long_running=False):
        self.runtime_mode = runtime_mode
        self.requests = []
        self.responses = []
        self.turn = turn
        self.long_running = long_running
        self.binary_path = "/fake/bot"  # traditional 每回合重启用


class _FakeRunner:
    """模拟 BinaryRunner：记录下发的行，按队列返回 Bot 响应行 + 握手行。"""
    def __init__(self, session, response_lines):
        self._sessions = {"fake": session}
        self._responses = list(response_lines)
        self.sent_lines: list[str] = []
        self._tmp_counter = 0

    async def start_session(self, binary_path, *, runtime_mode="longrunning"):
        """traditional 每回合重启：返回临时 session id（复用同一 session 的响应队列）。"""
        self._tmp_counter += 1
        sid = f"tmp_trad_{self._tmp_counter}"
        self._sessions[sid] = _FakeSession(runtime_mode=runtime_mode)
        return sid

    async def stop_session(self, sid):
        self._sessions.pop(sid, None)

    async def send(self, sid, line, *, timeout=None):
        self.sent_lines.append(line)
        return self._responses.pop(0)

    async def read_extra_line(self, sid, *, timeout=1.0):
        if self._responses and bz.is_keep_running_signal(self._responses[0]):
            return self._responses.pop(0)
        return None


def test_botzone_decide_longrunning_first_turn_full_envelope():
    """LongRunning 首回合：发完整历史信封 {requests[], responses[]}，握手后置长驻。"""
    session = _FakeSession(runtime_mode="longrunning", turn=0)
    fake = _FakeRunner(session, ['{"response":0}', bz.KEEP_RUNNING_SIGNAL])
    result = asyncio.run(_botzone_decide(
        fake, "fake", {"hand": 0}, game_id="holdem", action_timeout=5,
    ))
    assert result == {"response": 0}
    import json
    env = json.loads(fake.sent_lines[0])
    assert "requests" in env and "responses" in env  # 首回合完整信封
    assert session.long_running is True  # 握手成功
    assert session.turn == 1


def test_botzone_decide_longrunning_subsequent_single_request():
    """LongRunning 后续回合（turn>=1）：发单 request 信封 {request}。"""
    session = _FakeSession(runtime_mode="longrunning", turn=1, long_running=True)
    session.requests = [{"hand": 0}]
    session.responses = [0]
    fake = _FakeRunner(session, ['{"response":0}'])
    asyncio.run(_botzone_decide(
        fake, "fake", {"hand": 1}, game_id="holdem", action_timeout=5,
    ))
    import json
    env = json.loads(fake.sent_lines[0])
    assert "request" in env and "requests" not in env  # 单 request 信封


def test_botzone_decide_traditional_always_full_envelope():
    """Traditional：每回合都发完整历史信封（不论 turn）。"""
    session = _FakeSession(runtime_mode="traditional", turn=0)
    fake = _FakeRunner(session, ['{"response":-1}'])
    asyncio.run(_botzone_decide(
        fake, "fake", {"hand": 0}, game_id="holdem", action_timeout=5,
    ))
    import json
    env = json.loads(fake.sent_lines[0])
    assert "requests" in env and "responses" in env
    assert session.long_running is False  # Traditional 不握手机制



# ── 真实样例 Bot 端到端 ───────────────────────────────────────────────

_BOTS = {
    "foldbot": SAMPLES / "holdem_bots" / "foldbot",
    "allinbot": SAMPLES / "holdem_bots" / "allinbot",
    "raisebot": SAMPLES / "holdem_bots" / "raisebot",
    "randombot": SAMPLES / "holdem_bots" / "randombot",
    "tightbot": SAMPLES / "holdem_bots" / "tightbot",
    "loosebot": SAMPLES / "holdem_bots" / "loosebot",
    "callbot": SAMPLES / "callbot_linux_amd64",
    "aggressivebot": SAMPLES / "aggressivebot_bin",
}


@pytest.fixture(autouse=True)
def _local_bot():
    os.environ["BZ_BOT_LOCAL"] = "1"


@pytest.mark.parametrize("botname", list(_BOTS.keys()))
def test_sample_bot_responds_botzone_envelope(botname):
    """每个样例 Bot 单回合响应必须是 Botzone 信封 {"response": int}。"""
    path = _BOTS[botname]
    if not path.is_file():
        pytest.skip(f"{botname} binary missing")
    import subprocess
    req = (
        '{"num_players":2,"dealer_id":0,"my_id":0,"my_chips":19950,'
        '"my_cards":[48,0],"public_cards":[],"history":[],'
        '"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}'
    )
    # LongRunning 首回合信封
    envelope = '{"requests":[' + req + '],"responses":[]}'
    out = subprocess.run(
        [str(path)], input=envelope + "\n", capture_output=True, text=True, timeout=5,
    )
    line = out.stdout.strip().splitlines()[0] if out.stdout.strip() else ""
    import json
    payload = json.loads(line)
    assert "response" in payload
    assert isinstance(payload["response"], int)


def test_foldbot_vs_callbot_full_match():
    """foldbot vs callbot 跑 10 手：foldbot 每手弃，callbot 净筹码高。"""
    if not _BOTS["foldbot"].is_file() or not _BOTS["callbot"].is_file():
        pytest.skip("sample bots missing")
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    result = asyncio.run(runner.run_binaries(
        str(_BOTS["foldbot"]), str(_BOTS["callbot"]),
        game_id="holdem", seed=1,
    ))
    # 手数已钉死 DEFAULT_HANDS（70，#123）；num_hands 参数被忽略
    from bzplat.backend.games.holdem.engine import DEFAULT_HANDS
    assert result.rounds_played == DEFAULT_HANDS
    # callbot (seat 1) 应净胜
    assert result.final_chips[1] > result.final_chips[0]


def test_raisebot_emits_raise_delta():
    """raisebot 单回合返回的 response > 0（raise delta）。"""
    path = _BOTS["raisebot"]
    if not path.is_file():
        pytest.skip("raisebot missing")
    import subprocess, json
    # preflop SB → raisebot 加注（纯 Botzone 11 字段信封）
    req = (
        '{"num_players":2,"dealer_id":0,"my_id":0,"my_chips":19950,'
        '"my_cards":[48,0],"public_cards":[],"history":[],'
        '"hand":0,"max_hand":70,"total_win_chips":[0,0],"total_win_games":[0,0]}'
    )
    envelope = '{"requests":[' + req + '],"responses":[]}'
    out = subprocess.run([str(path)], input=envelope + "\n", capture_output=True, text=True, timeout=5)
    payload = json.loads(out.stdout.strip().splitlines()[0])
    assert payload["response"] > 0, f"raisebot 应加注，得到 {payload}"
