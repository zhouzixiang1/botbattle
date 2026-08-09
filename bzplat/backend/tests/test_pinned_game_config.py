"""守护测试：游戏规则参数钉死固定值（防止配置能力被重新加回）。

背景：hands 配置曾因 key 名不一致（match_config.hands vs session_factory 的 num_hands）
导致配置静默失效（首页出现 70/1 量纲矛盾）。现已彻底移除配置能力，由引擎常量钉死：
  - holdem：固定 DEFAULT_HANDS=70 手
  - gomoku：固定 BOARD_SIZE=15×15
  - pencil：固定 DEFAULT_N=6 点

本测试断言这些规则参数不可通过 match_config / judge_params / session_factory 参数改变，
确保「配置钉死」这一设计决策不被后续改动无意中破坏。
"""
import pytest

from bzplat.backend.games import registry
from bzplat.backend.games.holdem.engine import DEFAULT_HANDS
from bzplat.backend.games.gomoku.engine import BOARD_SIZE
from bzplat.backend.games.pencil.engine import DEFAULT_N


# ── default_match_params / validate 忽略配置 ──────────────────────

def test_default_match_params_all_empty():
    """三游戏的 default_match_params 恒为空 dict（无对局级可配参数）。"""
    for gid in registry.all_ids():
        assert registry.get(gid).default_match_params == {}, (
            f"{gid} default_match_params 应为空（规则已钉死），实际: "
            f"{registry.get(gid).default_match_params}"
        )


def test_validate_rejects_config_fields():
    """固定规则只接受空对象，旧字段不得被静默忽略。"""
    for game_id, cfg in (
        ("holdem", {"hands": 999}),
        ("holdem", {"hands": 0}),
        ("pencil", {"n_dots": 99}),
        ("pencil", {"n_dots": 1}),
        ("gomoku", {"board_size": 5}),
    ):
        with pytest.raises(ValueError, match="规则固定"):
            registry.get(game_id).validate_match_params(cfg)


# ── rounds_per_match / eta 返回固定常量 ───────────────────────────

def test_rounds_per_match_fixed():
    """rounds_per_match 忽略 match_config，返回固定值。"""
    assert registry.get("holdem").rounds_per_match({"hands": 1}) == DEFAULT_HANDS
    assert registry.get("holdem").rounds_per_match({"hands": 999}) == DEFAULT_HANDS
    assert registry.get("holdem").rounds_per_match({}) == DEFAULT_HANDS
    assert registry.get("gomoku").rounds_per_match({}) == 1
    assert registry.get("pencil").rounds_per_match({}) == 1


def test_eta_fixed():
    """eta_for_match 忽略 match_config，返回固定值。"""
    assert registry.get("holdem").eta_for_match({"hands": 1}) == DEFAULT_HANDS * 2
    assert registry.get("holdem").eta_for_match({"hands": 999}) == DEFAULT_HANDS * 2
    # pencil/gomoku 固定 ETA，不随配置变
    e1 = registry.get("pencil").eta_for_match({"n_dots": 3})
    e2 = registry.get("pencil").eta_for_match({"n_dots": 15})
    assert e1 == e2 > 0


# ── session_factory 忽略规则参数（用模块常量）──────────────────────

def test_holdem_session_factory_ignores_num_hands():
    """holdem session_factory 收到 num_hands/hands 参数时忽略，仍用 DEFAULT_HANDS。"""
    import asyncio
    from bzplat.backend.games.holdem.spec import _session_factory

    # 构造一个立即 fold 的 decide + 捕获 MatchSession.num_hands
    captured = {}

    async def decide(player_idx, request):
        return -1  # fold

    async def fake_run_async(self, decide=None):
        captured["num_hands"] = self.num_hands
        from bzplat.backend.games.base import MatchResult
        return MatchResult(rounds_played=0)

    # patch MatchSession.run_async 避免真跑对局
    from bzplat.backend.games.holdem.engine import MatchSession
    orig = MatchSession.run_async
    MatchSession.run_async = fake_run_async
    try:
        # 传 num_hands=1 和 hands=1 都应被忽略
        asyncio.run(_session_factory(decide, num_hands=1, hands=1))
        assert captured["num_hands"] == DEFAULT_HANDS, (
            f"holdem session_factory 应钉死 {DEFAULT_HANDS} 手，实际用了 {captured['num_hands']}"
        )
    finally:
        MatchSession.run_async = orig


def test_pencil_session_factory_ignores_n_dots():
    """pencil session_factory 收到 n_dots 参数时忽略，仍用 DEFAULT_N。"""
    import asyncio
    from bzplat.backend.games.pencil.spec import _session_factory

    captured = {}

    async def decide(player_idx, request):
        return {"x": -1, "y": -1}

    async def fake_run_async(self, decide=None):
        captured["n_dots"] = self.n_dots
        from bzplat.backend.games.base import MatchResult
        return MatchResult(rounds_played=0)

    from bzplat.backend.games.pencil.engine import PencilSession
    orig = PencilSession.run_async
    PencilSession.run_async = fake_run_async
    try:
        asyncio.run(_session_factory(decide, n_dots=11))  # 传 11 但应用 DEFAULT_N
        assert captured["n_dots"] == DEFAULT_N, (
            f"pencil session_factory 应钉死 N={DEFAULT_N}，实际用了 {captured['n_dots']}"
        )
    finally:
        PencilSession.run_async = orig


def test_gomoku_session_factory_ignores_board_size():
    """gomoku session_factory 收到 board_size 参数时忽略，仍用 BOARD_SIZE。"""
    import asyncio
    from bzplat.backend.games.gomoku.spec import _session_factory

    captured = {}

    async def decide(player_idx, request):
        return {"x": 0, "y": 0}

    async def fake_run_async(self, decide=None):
        captured["size"] = self.size
        from bzplat.backend.games.base import MatchResult
        return MatchResult(rounds_played=0)

    from bzplat.backend.games.gomoku.engine import GomokuSession
    orig = GomokuSession.run_async
    GomokuSession.run_async = fake_run_async
    try:
        asyncio.run(_session_factory(decide, board_size=9))  # 传 9 但应用 BOARD_SIZE
        assert captured["size"] == BOARD_SIZE, (
            f"gomoku session_factory 应钉死 {BOARD_SIZE}×{BOARD_SIZE}，实际用了 {captured['size']}"
        )
    finally:
        GomokuSession.run_async = orig


# ── judge_params 不含规则参数 ─────────────────────────────────────

def test_judge_params_no_rule_keys():
    """judge_params 不声明手数/棋盘/点阵（已钉死，非 admin 可调项）。"""
    holdem_keys = {p.field for p in registry.get("holdem").judge_params}
    assert "num_hands" not in holdem_keys, "holdem 手数应钉死，不在 judge_params"
    gomoku_keys = {p.field for p in registry.get("gomoku").judge_params}
    assert "board_size" not in gomoku_keys, "gomoku 棋盘应钉死，不在 judge_params"
    pencil_keys = {p.field for p in registry.get("pencil").judge_params}
    assert "n_dots" not in pencil_keys, "pencil 点阵应钉死，不在 judge_params"
