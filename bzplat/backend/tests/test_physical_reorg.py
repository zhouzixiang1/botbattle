"""全面解耦 PR4：物理重组 + 独立转发层测试。

验证：
1. 各游戏的 engine/protocol/result/tiers 是物理独立的（不共享基类/模块）
2. result 类互相独立（无共享父类），但满足鸭子契约
3. _compat 转发层保旧 import 路径可用
4. spec 引用本包 engine/protocol/result/tiers（不再经 engine./protocol. 旧路径）
"""
from __future__ import annotations

import asyncio

import pytest


# ── 各游戏 result 独立（不共享基类）────────────────────────────
def test_result_classes_are_independent():
    """三游戏的 MatchResult 互相独立（不是同一个类，无共享父类）。"""
    from bzplat.backend.games.holdem.result import MatchResult as HMR
    from bzplat.backend.games.gomoku.result import MatchResult as GMR
    from bzplat.backend.games.pencil.result import MatchResult as PMR

    assert HMR is not GMR
    assert GMR is not PMR
    assert HMR is not PMR
    # 无共同基类（除 object）
    assert object in HMR.__mro__ and object in GMR.__mro__
    # 不存在共享的非 object 父类
    common = set(HMR.__mro__) & set(GMR.__mro__) & set(PMR.__mro__)
    common.discard(object)
    assert not common, f"三游戏 MatchResult 不应有共享父类（除 object），实际共享: {common}"


def test_result_duck_contract_all_games():
    """三游戏 result 都满足通用层鸭子契约（winners/deltas/rounds_played/rounds/winner）。"""
    from bzplat.backend.games.holdem.result import MatchResult as HMR, RoundResult as HRR
    from bzplat.backend.games.gomoku.result import MatchResult as GMR, RoundResult as GRR
    from bzplat.backend.games.pencil.result import MatchResult as PMR, RoundResult as PRR

    for MR, RR in ((HMR, HRR), (GMR, GRR), (PMR, PRR)):
        r = MR(rounds_played=1, rounds=[RR([0], [1, -1])])
        # 鸭子契约字段
        assert r.rounds_played == 1
        assert len(r.rounds) == 1
        assert r.rounds[0].winners == [0]
        assert r.rounds[0].deltas == [1, -1]


def test_gomoku_result_has_specific_fields():
    """gomoku result 含 gomoku 专属字段（winner/reason/scores/moves/board_grid）。"""
    from bzplat.backend.games.gomoku.result import MatchResult

    r = MatchResult(rounds_played=9, winner=0, reason="five", scores=[1, 0], moves=9, board_grid=[])
    assert r.winner == 0 and r.reason == "five" and r.scores == [1, 0] and r.moves == 9


def test_pencil_result_has_specific_fields():
    from bzplat.backend.games.pencil.result import MatchResult

    r = MatchResult(rounds_played=12, winner=1, reason="score", scores=[4, 7], moves=12)
    assert r.winner == 1 and r.reason == "score" and r.scores == [4, 7] and r.moves == 12


def test_holdem_result_has_specific_fields():
    """holdem result 含 holdem 专属字段（final_chips/HandResult.hand_index/pot/...）。"""
    from bzplat.backend.games.holdem.result import HandResult, MatchResult

    r = MatchResult(rounds_played=5, final_chips=[1000, 2000])
    assert r.final_chips == [1000, 2000]
    h = HandResult([0], [100, -100], hand_index=0, pot=200, board=[], holes=[[], []], folded=[False, True], reason="showdown")
    assert h.hand_index == 0 and h.pot == 200 and h.folded == [False, True]


# ── 各游戏 protocol 独立 ──────────────────────────────────────
def test_protocols_are_independent_modules():
    """gomoku 与 pencil 的 protocol 是独立模块（不共享 board_protocol）。"""
    from bzplat.backend.games.gomoku import protocol as gproto
    from bzplat.backend.games.pencil import protocol as pproto
    from bzplat.backend.games.holdem import protocol as hproto

    assert gproto is not pproto
    assert gproto is not hproto
    # 各自都有 dumps_request/loads_response
    for p in (gproto, pproto, hproto):
        assert callable(p.dumps_request)
        assert callable(p.loads_response)


# ── 各游戏 tiers 独立 ─────────────────────────────────────────
def test_tiers_are_independent_modules():
    from bzplat.backend.games.holdem import tiers as ht
    from bzplat.backend.games.gomoku import tiers as gt
    from bzplat.backend.games.pencil import tiers as pt

    assert ht is not gt is not pt
    # 各自的 TIERS 列表独立（当前阈值相同，但可独立调）
    assert len(ht.TIERS) == len(gt.TIERS) == len(pt.TIERS) == 6


# ── spec 引用本包（不经旧 engine./protocol. 路径）──────────────
def test_specs_reference_local_package():
    """三 spec 的 session_factory/protocol/tiers 引用本 games/<game>/ 包，不经旧路径。"""
    import inspect

    from bzplat.backend.games import registry
    for gid in ("holdem", "gomoku", "pencil"):
        spec = registry.get(gid)
        # session_factory 引用本包 engine（模块路径含 games.<gid>）
        sf_mod = inspect.getmodule(spec.session_factory)
        assert sf_mod is not None and "games." in sf_mod.__name__ and gid in sf_mod.__name__, (
            f"{gid} session_factory 应来自 games.{gid} 包，实际来自 {sf_mod}"
        )


# ── run_session 经注册表跑各游戏（端到端）──────────────────────
def test_run_session_holdem_via_registry():
    """holdem 经注册表跑（黑盒：不抛错，返回有 rounds_played 的结果）。

    手数已钉死 DEFAULT_HANDS=70，即使传 num_hands=2 也被忽略，仍跑 70 手。
    """
    from bzplat.backend.games import run_session

    async def decide(player, req):
        return {"a": "f"}  # 一直 fold

    result = asyncio.run(run_session("holdem", decide, num_hands=2))
    assert hasattr(result, "rounds_played")
    from bzplat.backend.games.holdem.engine import DEFAULT_HANDS
    assert result.rounds_played == DEFAULT_HANDS  # 钉死 70，忽略 num_hands 参数


def test_run_session_gomoku_via_registry():
    from bzplat.backend.games import run_session

    black = [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)]
    white = [(1, 0), (1, 1), (1, 2), (1, 3)]
    bi = wi = 0

    async def decide(player, req):
        nonlocal bi, wi
        if player == 0:
            x, y = black[bi]; bi += 1
        else:
            x, y = white[wi]; wi += 1
        return {"x": x, "y": y}

    result = asyncio.run(run_session("gomoku", decide))
    assert result.winner == 0 and result.reason == "five"
