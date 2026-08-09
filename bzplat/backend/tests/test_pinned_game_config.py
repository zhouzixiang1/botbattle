"""固定游戏规则入口的回归门禁。

现行平台只有一套规则：Holdem 70 手、Gomoku 15×15、Pencil N=6。
公开配置和直接 Session 入口都必须拒绝规则覆盖；不能接收旧键后静默运行。
"""
from __future__ import annotations

import asyncio
import dataclasses
import random

import pytest

from bzplat.backend.games import registry, run_session
from bzplat.backend.games.holdem.engine import DEFAULT_HANDS


def test_default_match_params_all_empty():
    """三游戏没有对局级规则参数。"""
    for gid in registry.all_ids():
        assert registry.get(gid).default_match_params == {}


@pytest.mark.parametrize(
    ("game_id", "cfg"),
    [
        ("holdem", {"hands": 999}),
        ("holdem", {"num_hands": 1}),
        ("holdem", {"starting_stack": 1}),
        ("gomoku", {"board_size": 5}),
        ("pencil", {"n_dots": 99}),
        ("pencil", {"time_limit": 1}),
    ],
)
def test_validate_rejects_every_rule_field(game_id, cfg):
    with pytest.raises(ValueError, match="规则固定"):
        registry.get(game_id).validate_match_params(cfg)


def test_eta_accepts_only_canonical_empty_config():
    assert registry.get("holdem").eta_for_match({}) == DEFAULT_HANDS * 2
    assert registry.get("gomoku").eta_for_match({}) > 0
    assert registry.get("pencil").eta_for_match({}) > 0
    for game_id, cfg in (
        ("holdem", {"hands": 1}),
        ("gomoku", {"board_size": 9}),
        ("pencil", {"n_dots": 3}),
    ):
        with pytest.raises(ValueError, match="规则固定"):
            registry.get(game_id).eta_for_match(cfg)


@pytest.mark.parametrize(
    ("game_id", "params"),
    [
        ("holdem", {"hands": 1}),
        ("holdem", {"num_hands": 1}),
        ("holdem", {"starting_stack": 10_000}),
        ("holdem", {"sb": 25}),
        ("holdem", {"bb": 50}),
        ("gomoku", {"board_size": 9}),
        ("pencil", {"n_dots": 3}),
        ("pencil", {"num_hands": 1}),
    ],
)
def test_run_session_rejects_rule_kwargs(game_id, params):
    async def decide(_player_idx, _request):
        raise AssertionError("参数应在裁判开始前被拒绝")

    with pytest.raises(TypeError, match="Session 不接受参数"):
        asyncio.run(run_session(game_id, decide, **params))


def test_session_factories_accept_only_internal_replay_controls(monkeypatch):
    """通用 runner 的 rng 与 Holdem duplicate 发牌序列仍是明确内部参数。"""
    captured: dict[str, object] = {}

    async def holdem_run(self, _decide):
        captured["holdem_hands"] = self.num_hands
        captured["holdem_deals"] = self.deal_sequence
        from bzplat.backend.games.base import MatchResult

        return MatchResult(rounds_played=0)

    async def gomoku_run(self, _decide):
        captured["gomoku_size"] = self.size
        from bzplat.backend.games.base import MatchResult

        return MatchResult(rounds_played=0)

    async def pencil_run(self, _decide):
        captured["pencil_n"] = self.n_dots
        from bzplat.backend.games.base import MatchResult

        return MatchResult(rounds_played=0)

    from bzplat.backend.games.gomoku.engine import BOARD_SIZE, GomokuSession
    from bzplat.backend.games.holdem.engine import MatchSession
    from bzplat.backend.games.pencil.engine import DEFAULT_N, PencilSession

    monkeypatch.setattr(MatchSession, "run_async", holdem_run)
    monkeypatch.setattr(GomokuSession, "run_async", gomoku_run)
    monkeypatch.setattr(PencilSession, "run_async", pencil_run)

    async def decide(_player_idx, _request):
        return {"response": 0}

    rng = random.Random(0)
    asyncio.run(run_session("holdem", decide, rng=rng, deal_sequence=[]))
    asyncio.run(run_session("gomoku", decide, rng=rng))
    asyncio.run(run_session("pencil", decide, rng=rng))

    assert captured == {
        "holdem_hands": DEFAULT_HANDS,
        "holdem_deals": [],
        "gomoku_size": BOARD_SIZE,
        "pencil_n": DEFAULT_N,
    }


def test_removed_game_spec_fields_cannot_return():
    """零消费者的旧裁判参数/局数/座位钩子不再伪装成平台契约。"""
    from bzplat.backend.games.base import GameSpec

    fields = {field.name for field in dataclasses.fields(GameSpec)}
    assert {
        "judge_params",
        "rounds_per_match",
        "num_seats",
    }.isdisjoint(fields)
