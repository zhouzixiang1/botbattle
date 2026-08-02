"""全面解耦 PR2 测试：编排/赛制/段位去特化。

验证通用层的 if game_id 分支已被 spec 能力取代：
- orchestrator 用 spec.rounds_per_match / spec.normalize_earnings（不再 GAME_HOLDEM）
- _judge_params(gid) per-game（只返回该游戏字段）
- contests estimate 经 spec.eta_per_match_sec
- validate_match_config 经 spec.validate_match_params
- 段位 per-game：/api/tiers?game_id= 返回该游戏曲线；bot_profile/leaderboard 按 bot 的 game_id 取段位
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from bzplat.backend.games import registry
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


# ── orchestrator 编排特化经 spec ───────────────────────────────
def test_normalize_earnings_via_spec():
    """orchestrator 的 net_bb_a 计算经 spec.normalize_earnings（holdem /100；棋类透传）。"""
    assert registry.get("holdem").normalize_earnings(500) == 5.0
    assert registry.get("gomoku").normalize_earnings(1) == 1.0
    assert registry.get("pencil").normalize_earnings(-2) == -2.0


def test_rounds_per_match_via_spec():
    """orchestrator 的 total_hands 经 spec.rounds_per_match（holdem=hands；棋类=1）。"""
    assert registry.get("holdem").rounds_per_match({"hands": 80}) == 80
    assert registry.get("holdem").rounds_per_match({"hands": 1}) == 1
    assert registry.get("gomoku").rounds_per_match({}) == 1
    assert registry.get("pencil").rounds_per_match({}) == 1


# ── contests estimate 经 spec ETA ──────────────────────────────
def test_estimate_holdem_uses_hands(tmp_path):
    """holdem ETA 按手数线性（每手 ~2s），经 _estimate_sec_per_match。"""
    from bzplat.backend.contests.manager import _estimate_sec_per_match

    # 70 手 → 140s
    assert _estimate_sec_per_match("holdem", {"hands": 70}, 70) == 140
    # 35 手 → 70s
    assert _estimate_sec_per_match("holdem", {"hands": 35}, 70) == 70
    # 无 config 用 fallback
    assert _estimate_sec_per_match("holdem", {}, 50) == 100


def test_estimate_pencil_scales_with_n_dots(tmp_path):
    """pencil ETA 按 n_dots 线性缩放（N=11 标定）。"""
    from bzplat.backend.contests.manager import _estimate_sec_per_match

    base = _estimate_sec_per_match("pencil", {"n_dots": 11}, 70)
    half = _estimate_sec_per_match("pencil", {"n_dots": 5}, 70)
    assert base > half > 0


def test_estimate_gomoku_fixed():
    from bzplat.backend.contests.manager import _estimate_sec_per_match

    # gomoku 单局固定 ETA
    a = _estimate_sec_per_match("gomoku", {}, 70)
    b = _estimate_sec_per_match("gomoku", {"foo": 1}, 70)
    assert a == b > 0


# ── validate_match_config 经 spec ──────────────────────────────
def test_validate_match_config_delegates_to_spec():
    from bzplat.backend.contests.validation import validate_match_config

    # holdem
    assert validate_match_config({"hands": 100}, "holdem") == {"hands": 100}
    assert validate_match_config({}, "holdem") == {"hands": 70}
    # pencil
    assert validate_match_config({"n_dots": 9}, "pencil") == {"n_dots": 9}
    # gomoku 无参数
    assert validate_match_config({"x": 1}, "gomoku") == {}
    # 非法值抛 ValueError
    import pytest
    with pytest.raises(ValueError):
        validate_match_config({"hands": 0}, "holdem")
    with pytest.raises(ValueError):
        validate_match_config({"n_dots": 99}, "pencil")


# ── DEFAULT_MATCH_CONFIG 从注册表派生 ──────────────────────────
def test_default_match_config_derived_from_registry():
    from bzplat.backend.contests.templates import DEFAULT_MATCH_CONFIG, default_match_config

    # 与各 spec.default_match_params 一致
    for gid in registry.all_ids():
        assert DEFAULT_MATCH_CONFIG[gid] == registry.get(gid).default_match_params
        assert default_match_config(gid) == registry.get(gid).default_match_params


# ── 段位 per-game：API 端点 ────────────────────────────────────
def test_tiers_endpoint_per_game(tmp_path):
    app = create_app(db_path=str(tmp_path / "tiers.db"))
    c = TestClient(app)
    # 不传 game_id → 默认 holdem（向后兼容）
    r = c.get("/api/tiers")
    assert r.status_code == 200
    data = r.json()
    assert len(data["tiers"]) == 6
    assert data["game_id"] == "holdem"
    # 传 game_id=gomoku
    r = c.get("/api/tiers?game_id=gomoku")
    assert r.status_code == 200
    data = r.json()
    assert data["game_id"] == "gomoku"
    assert len(data["tiers"]) == 6
    # 未知 game_id 回退 holdem（公开端点容错）
    r = c.get("/api/tiers?game_id=chess")
    assert r.status_code == 200
    assert r.json()["game_id"] == "holdem"


def test_tiers_endpoint_per_game_curves_independent(tmp_path):
    """三游戏的段位曲线经注册表独立（PR2：可独立调阈值，初始一致）。"""
    app = create_app(db_path=str(tmp_path / "tiers2.db"))
    c = TestClient(app)
    h = c.get("/api/tiers?game_id=holdem").json()["tiers"]
    g = c.get("/api/tiers?game_id=gomoku").json()["tiers"]
    p = c.get("/api/tiers?game_id=pencil").json()["tiers"]
    # 初始阈值一致（后续可独立调整）
    assert [t["min_rating"] for t in h] == [t["min_rating"] for t in g] == [t["min_rating"] for t in p]
    assert [t["key"] for t in h] == ["master", "expert", "gold", "silver", "bronze", "novice"]


def test_bot_profile_tier_uses_bot_game_id(tmp_path):
    """bot_profile 的段位按 bot 的 game_id 取对应曲线（经注册表）。"""
    s = Store(str(tmp_path / "prof.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    # gomoku bot rating 2100 → 经 gomoku 曲线 = 专家
    b = s.create_bot(u["id"], "gomuBot", binary_path="/tmp", format="elf", is_public=1, game_id="gomoku")
    s.ensure_rating(b["id"])
    s.update_rating_row(b["id"], rating=2100)
    p = s.bot_profile(b["id"])
    assert p["tier_name"] == "专家"
    assert p["tier_level"] == 4
    s.close()


def test_leaderboard_tier_uses_game_id(tmp_path):
    """leaderboard 的段位按每行 bot 的 game_id 取对应曲线。"""
    s = Store(str(tmp_path / "lb.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b = s.create_bot(u["id"], "penBot", binary_path="/tmp", format="elf", is_public=1, game_id="pencil")
    s.ensure_rating(b["id"])
    s.update_rating_row(b["id"], rating=1900)
    lb = s.list_leaderboard(game_id="pencil")
    assert len(lb) == 1
    assert lb[0]["tier_name"] == "高手"  # 1900 → gold
    s.close()


# ── admin JUDGE_GAMES 从注册表派生 ─────────────────────────────
def test_judge_games_derived_from_registry():
    from bzplat.backend.api_routes import JUDGE_GAMES, JUDGE_PARAM_BOUNDS, JUDGE_PARAM_DEFAULTS

    gids = {g["game_id"] for g in JUDGE_GAMES}
    assert gids == registry.all_ids()
    # 与 registry.judge_param_table 一致
    d, b = registry.judge_param_table()
    assert JUDGE_PARAM_DEFAULTS == d
    assert JUDGE_PARAM_BOUNDS == b
