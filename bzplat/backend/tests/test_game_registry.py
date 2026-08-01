"""游戏注册表框架测试（PR1：GameSpec + GameRegistry + 派生分发）。

验证全面解耦的核心契约：
- 注册表是单一真相（三游戏已注册、id 一致）
- 未知 game_id 报错而非静默兜底 holdem（行为修正）
- run_session 经注册表分发到正确引擎
- 协议 dumps/loads/fail_response 按游戏路由
- validate_match_config / default_match_config 按游戏
- 段位曲线 per-game
- schema 的 REGISTERED_ENGINES/VALID_GAME_IDS 与注册表一致
- judge_games 元信息从注册表派生
"""
from __future__ import annotations

import asyncio

import pytest

from bzplat.backend.engine.registry import (
    GAME_LABELS,
    GAME_GOMOKU,
    GAME_HOLDEM,
    GAME_PENCIL,
    normalize_game_id,
    run_session,
)
from bzplat.backend.games import (
    default_match_config,
    dumps,
    fail_response,
    loads,
    registry,
    validate_match_config,
)
from bzplat.backend.store import schema


# ── 注册表一致性 ──────────────────────────────────────────────
def test_registry_has_three_games():
    assert registry.all_ids() == frozenset({"holdem", "gomoku", "pencil"})


def test_schema_frozensets_match_registry():
    """schema.REGISTERED_ENGINES / VALID_GAME_IDS 必须与注册表一致（防漂移）。"""
    ids = registry.all_ids()
    assert ids == schema.REGISTERED_ENGINES
    assert ids == schema.VALID_GAME_IDS


def test_is_registered():
    assert registry.is_registered("holdem")
    assert registry.is_registered("gomoku")
    assert registry.is_registered("pencil")
    assert not registry.is_registered("nonexistent")
    assert not registry.is_registered("")


def test_game_labels_derived_from_registry():
    """GAME_LABELS 从注册表派生（不再手写字典）。"""
    assert GAME_LABELS == {"holdem": "德州扑克", "gomoku": "五子棋", "pencil": "点格棋"}
    # 与各 spec.label 一致
    for gid in registry.all_ids():
        assert GAME_LABELS[gid] == registry.get(gid).label


# ── 未知 game_id 报错（行为修正）──────────────────────────────
def test_unknown_game_id_raises_in_registry_get():
    """registry.get 对未知 game_id 抛 KeyError（不再静默兜底 holdem）。"""
    with pytest.raises(KeyError):
        registry.get("chess")
    with pytest.raises(KeyError):
        registry.get("")


def test_unknown_game_id_raises_in_dumps():
    with pytest.raises(KeyError):
        dumps("chess", {"x": 1})


def test_unknown_game_id_raises_in_fail_response():
    with pytest.raises(KeyError):
        fail_response("chess")


def test_unknown_game_id_raises_in_validate():
    with pytest.raises(KeyError):
        validate_match_config("chess", {})


def test_unknown_game_id_raises_in_default_match_config():
    with pytest.raises(KeyError):
        default_match_config("chess")


def test_normalize_still_falls_back_to_holdem_for_empty():
    """旧 normalize_game_id 保留空值兜底 holdem 语义（向后兼容）。

    但 normalize 后若不在注册表（如 'chess'），run_session 仍会经 registry.get 报错。
    """
    assert normalize_game_id("") == "holdem"
    assert normalize_game_id(None) == "holdem"
    assert normalize_game_id("  PENCIL  ") == "pencil"


# ── run_session 经注册表分发 ──────────────────────────────────
def test_run_session_gomoku_via_registry():
    """registry.run_session('gomoku') 实际跑五子棋引擎（黑连五胜）。"""
    black_moves = [(0, 0), (0, 1), (0, 2), (0, 3), (0, 4)]
    white_moves = [(1, 0), (1, 1), (1, 2), (1, 3)]
    bi = wi = 0

    async def decide(player, req):
        nonlocal bi, wi
        if player == 0:
            x, y = black_moves[bi]
            bi += 1
            return {"x": x, "y": y}
        x, y = white_moves[wi]
        wi += 1
        return {"x": x, "y": y}

    result = asyncio.run(run_session("gomoku", decide))
    assert result.winner == 0
    assert result.reason == "five"


def test_run_session_unknown_raises():
    async def decide(player, req):
        return {}

    with pytest.raises(KeyError):
        asyncio.run(run_session("chess", decide))


# ── 协议按游戏路由 ────────────────────────────────────────────
def test_protocol_fail_response_per_game():
    assert fail_response("holdem") == {"a": "f"}
    assert fail_response("gomoku") == {"x": -99, "y": -99}
    assert fail_response("pencil") == {"x": -99, "y": -99}


def test_protocol_dumps_loads_roundtrip():
    req = {"v": 1, "t": "mv", "x": 5, "y": 6}
    line = dumps("gomoku", req)
    back = loads("gomoku", line)
    assert back["x"] == 5 and back["y"] == 6
    # holdem 协议
    hreq = {"a": "c"}
    hline = dumps("holdem", hreq)
    assert loads("holdem", hline) == {"a": "c"}


def test_protocol_loads_board_tolerates_garbage():
    """棋类协议对空/非法输入返回 {}（不抛），保对局不崩。"""
    assert loads("gomoku", "") == {}
    assert loads("gomoku", "not json") == {}
    assert loads("pencil", "") == {}


# ── validate / default match_config ───────────────────────────
def test_validate_match_config_holdem():
    assert validate_match_config("holdem", {"hands": 100}) == {"hands": 100}
    assert validate_match_config("holdem", {}) == {"hands": 70}  # 默认
    with pytest.raises(ValueError):
        validate_match_config("holdem", {"hands": 0})
    with pytest.raises(ValueError):
        validate_match_config("holdem", {"hands": 501})


def test_validate_match_config_pencil():
    assert validate_match_config("pencil", {"n_dots": 11}) == {"n_dots": 11}
    assert validate_match_config("pencil", {}) == {"n_dots": 11}
    with pytest.raises(ValueError):
        validate_match_config("pencil", {"n_dots": 2})
    with pytest.raises(ValueError):
        validate_match_config("pencil", {"n_dots": 16})


def test_validate_match_config_gomoku_no_params():
    """五子棋单局无可调参数，返回空 dict，忽略任意字段。"""
    assert validate_match_config("gomoku", {}) == {}
    assert validate_match_config("gomoku", {"foo": 1}) == {}


def test_default_match_config_per_game():
    assert default_match_config("holdem") == {"hands": 70}
    assert default_match_config("gomoku") == {}
    assert default_match_config("pencil") == {"n_dots": 11}
    # 返回深拷贝（改不影响注册表）
    d = default_match_config("holdem")
    d["hands"] = 999
    assert default_match_config("holdem") == {"hands": 70}


# ── 段位 per-game ─────────────────────────────────────────────
def test_tiers_per_game():
    for gid in ("holdem", "gomoku", "pencil"):
        tiers = registry.get(gid).tiers
        assert len(tiers) == 6  # 6 档
        assert tiers[0].level == 5 and tiers[0].key == "master"
        assert tiers[-1].level == 0 and tiers[-1].key == "novice"


def test_tier_for_per_game():
    assert registry.tier_for("holdem", 2300).key == "master"
    assert registry.tier_for("holdem", 1900).key == "gold"
    assert registry.tier_for("gomoku", 1500).key == "novice"
    assert registry.tier_for("pencil", None).key == "novice"


def test_tier_dict_structure():
    d = registry.tier_dict("holdem", 1900)
    assert d["key"] == "gold"
    assert d["level"] == 3
    assert "color" in d and "bg" in d and "min_rating" in d


def test_all_tiers_per_game():
    for gid in ("holdem", "gomoku", "pencil"):
        all_t = registry.all_tiers(gid)
        assert len(all_t) == 6
        assert all_t[0]["key"] == "master"


# ── 编排特化函数（spec 上的能力）──────────────────────────────
def test_rounds_per_match_per_game():
    assert registry.get("holdem").rounds_per_match({"hands": 50}) == 50
    assert registry.get("holdem").rounds_per_match({}) == 70
    assert registry.get("gomoku").rounds_per_match({}) == 1
    assert registry.get("pencil").rounds_per_match({}) == 1


def test_normalize_earnings_per_game():
    # holdem 除 100（bb/100）；棋类透传
    assert registry.get("holdem").normalize_earnings(500) == 5.0
    assert registry.get("gomoku").normalize_earnings(1) == 1.0
    assert registry.get("pencil").normalize_earnings(-1) == -1.0


# ── judge 元信息从注册表派生 ──────────────────────────────────
def test_judge_games_derived():
    games = registry.judge_games()
    ids = {g["game_id"] for g in games}
    assert ids == {"holdem", "gomoku", "pencil"}
    # holdem 有 4 个裁判参数
    holdem = next(g for g in games if g["game_id"] == "holdem")
    assert len(holdem["params"]) == 4
    # gomoku 1 个（棋盘边长）
    gomoku = next(g for g in games if g["game_id"] == "gomoku")
    assert len(gomoku["params"]) == 1
    # pencil 0 个（n_dots 走 match 列）
    pencil = next(g for g in games if g["game_id"] == "pencil")
    assert len(pencil["params"]) == 0


def test_judge_param_table():
    defaults, bounds = registry.judge_param_table()
    # holdem 的 4 个 setting key 都在
    assert schema.SETTING_JUDGE_HOLDEM_STACK in defaults
    assert defaults[schema.SETTING_JUDGE_HOLDEM_STACK] == 20000
    assert bounds[schema.SETTING_JUDGE_GOMOKU_SIZE] == (9, 19)
    # pencil 无全局 judge 参数
    pencil_keys = {p.setting_key for p in registry.get("pencil").judge_params}
    assert pencil_keys == set()
