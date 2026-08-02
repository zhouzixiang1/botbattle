"""黑白棋（reversi）引擎测试 + 第 4 游戏零改动验证（PR7）。

reversi 是「零改动新增游戏」承诺的终极验证：规则与 holdem/gomoku/pencil 完全不同
（夹击翻转），但经前 6 个 PR 的解耦整改，新增它只需 games/reversi/ 子包 + 注册一行 +
schema 两个 frozenset 各加一项——通用层（matches/contests/store/api_routes）零改动。
"""
from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.games import (
    GAME_LABELS,
    default_match_config,
    registry,
    run_session,
    validate_match_config,
)
from bzplat.backend.games.reversi.engine import (
    BOARD_SIZE,
    ReversiSession,
    _flips_for_move,
    _new_board,
    count_discs,
    legal_moves,
)


# ── 引擎规则正确性 ────────────────────────────────────────────
def test_initial_board_has_four_center_discs():
    """标准 Othello 开局：中心 4 子（白 (3,3)/(4,4)，黑 (3,4)/(4,3)）。"""
    b = _new_board()
    assert b[3][3] == 1 and b[4][4] == 1  # 白
    assert b[3][4] == 0 and b[4][3] == 0  # 黑
    black, white = count_discs(b)
    assert black == 2 and white == 2


def test_legal_moves_at_start():
    """开局黑方有 4 个合法手（(2,3)/(3,2)/(4,5)/(5,4)——夹住白子的位置）。"""
    b = _new_board()
    moves = set(legal_moves(b, 0))
    assert moves == {(2, 3), (3, 2), (4, 5), (5, 4)}


def test_flip_logic():
    """落子须翻转被夹的对方子。"""
    b = _new_board()
    # 黑下 (2,3)：夹住 (3,3) 白（(2,3)-(3,3)-(4,4=黑?)... 实际 (2,3) 向下 (3,3)=白 (4,3)=黑）
    flips = _flips_for_move(b, 2, 3, 0)
    assert (3, 3) in flips  # (3,3) 白被夹（(2,3)新黑 - (3,3)白 - (4,3)黑）→ 翻转


def test_illegal_move_loses():
    """非法手（不夹击）→ 判负。"""

    async def decide(player, req):
        return {"x": 0, "y": 0}  # (0,0) 不夹任何子 → 非法

    result = asyncio.run(run_session("reversi", decide))
    assert result.winner == 1  # 黑非法 → 白胜
    assert result.reason == "illegal"


# ── 经注册表跑（端到端）────────────────────────────────────────
def test_run_session_reversi_via_registry():
    """registry.run_session('reversi') 实际跑黑白棋引擎。"""

    async def decide(player, req):
        # 占第一个合法手（让对局自然进行）
        return {"x": 2, "y": 3} if player == 0 else {"x": 5, "y": 4}

    result = asyncio.run(run_session("reversi", decide))
    # 至少跑了几步（黑 (2,3) 合法，白 (5,4) 合法，之后可能非法）
    assert result.rounds_played >= 1
    assert result.reason in ("score", "illegal", "draw", "crash", "error")


# ── 第 4 游戏零改动验证（核心契约）──────────────────────────────
def test_reversi_registered_with_full_spec():
    """reversi 注册且 spec 字段齐全（与其他三游戏对等）。"""
    spec = registry.get("reversi")
    assert spec.game_id == "reversi"
    assert spec.label == "黑白棋"
    assert spec.num_seats == 2
    assert spec.default_scoring == "ccgc_2_1_0"
    # 关键能力都在（与其他游戏同构）
    assert callable(spec.session_factory)
    assert callable(spec.protocol.dumps_request)
    assert callable(spec.protocol.loads_response)
    assert callable(spec.validate_match_params)
    assert callable(spec.rounds_per_match)
    assert callable(spec.normalize_earnings)
    assert callable(spec.eta_for_match)
    assert len(spec.tiers) == 6  # 6 档段位
    assert len(spec.templates) == 2  # 2 个赛事模板
    assert spec.preflight_check is not None


def test_reversi_uses_shared_infrastructure():
    """reversi 复用共享基础设施（_board_protocol 行协议 + base.tier_for_in 段位）。

    这是「零改动」的关键：新游戏不自造序列化/段位算法，复用平台工具。
    """
    from bzplat.backend.games._board_protocol import dumps_request, parse_xy
    from bzplat.backend.games.base import tier_for_in

    # 行协议复用
    line = dumps_request({"x": 1, "y": 2, "me": 0})
    assert '"x":1' in line and '"y":2' in line
    x, y = parse_xy({"x": 1, "y": 2})
    assert (x, y) == (1, 2)
    # 段位查表复用
    spec = registry.get("reversi")
    t = tier_for_in(2300, spec.tiers)
    assert t.key == "master"


def test_reversi_match_config_validation():
    """reversi 的 validate_match_params/default_match_config 经注册表工作。"""
    assert validate_match_config("reversi", {}) == {}  # 无可调参数
    assert default_match_config("reversi") == {}


def test_reversi_in_game_labels():
    """GAME_LABELS 含 reversi（从注册表派生，非硬编码）。"""
    assert GAME_LABELS["reversi"] == "黑白棋"


def test_reversi_board_auto_created_by_db():
    """PR1 验证：Store 初始化自动建 matches_reversi 表（schema.py 无字面 DDL）。"""
    import tempfile

    from bzplat.backend.store import Store

    with tempfile.TemporaryDirectory() as td:
        s = Store(str(td + "/rev.db"))
        with s._tx() as c:
            tables = {
                r[0]
                for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
        s.close()
        assert "matches_reversi" in tables, "matches_reversi 表应被 _migrate 自动建出"


def test_reversi_match_routes_and_persists():
    """第 4 游戏对局经 matches_index 路由 + 物理表持久化（通用层零改动）。"""
    import tempfile

    from bzplat.backend.store import Store

    with tempfile.TemporaryDirectory() as td:
        s = Store(str(td + "/route.db"))
        u = s.create_user("revusr", "r@e.com", "x")["id"]
        ba = s.create_bot(u, "rbotA", game_id="reversi")["id"]
        bb = s.create_bot(u, "rbotB", game_id="reversi")["id"]
        mid = s.create_match("20260802-rev-test", ba, bb, game_id="reversi")["id"]
        # 写入正确物理表 + index 定位
        with s._tx() as c:
            row = c.execute("SELECT game_id FROM matches_reversi WHERE id=?", (mid,)).fetchone()
            assert row["game_id"] == "reversi"
        assert s.get_match(mid)["game_id"] == "reversi"
        s.close()
