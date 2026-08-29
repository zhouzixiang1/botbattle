"""全面解耦 PR2 测试：编排与赛制去特化。

验证通用层的 if game_id 分支已被 spec 能力取代：
- orchestrator 用 spec.normalize_delta（不再按游戏名分支）
- contests estimate 经 spec.eta_for_match
- validate_match_config 对固定规则只接受空对象
"""
from __future__ import annotations

import pytest
from bzplat.backend.games import registry


# ── orchestrator 编排特化经 spec ───────────────────────────────
def test_normalize_delta_via_spec():
    """持久化 normalized_delta 经 spec 换算（Holdem 为大盲单位；棋类透传）。"""
    assert registry.get("holdem").normalize_delta(500) == 5.0
    assert registry.get("gomoku").normalize_delta(1) == 1.0
    assert registry.get("pencil").normalize_delta(-2) == -2.0


# ── contests estimate 经 spec ETA（已钉死固定值）─────────────────
def test_estimate_holdem_fixed(tmp_path):
    """holdem ETA 固定（手数钉死 70 → 140s），拒绝规则参数。"""
    from bzplat.backend.contests.manager import _estimate_sec_per_match

    assert _estimate_sec_per_match("holdem", {}) == 140
    with pytest.raises(ValueError, match="规则固定"):
        _estimate_sec_per_match("holdem", {"hands": 35})


def test_estimate_pencil_fixed(tmp_path):
    """pencil ETA 使用双方 900 秒棋钟上界，并拒绝 n_dots。"""
    from bzplat.backend.contests.manager import _estimate_sec_per_match

    assert _estimate_sec_per_match("pencil", {}) == 1800
    with pytest.raises(ValueError, match="规则固定"):
        _estimate_sec_per_match("pencil", {"n_dots": 5})


def test_estimate_gomoku_fixed():
    from bzplat.backend.contests.manager import _estimate_sec_per_match

    assert _estimate_sec_per_match("gomoku", {}) > 0
    with pytest.raises(ValueError, match="规则固定"):
        _estimate_sec_per_match("gomoku", {"foo": 1})


# ── validate_match_config 固定规则拒绝旧配置 ────────────────────
def test_validate_match_config_rejects_nonempty_config():
    from bzplat.backend.contests.validation import validate_match_config

    for game_id, config in (
        ("holdem", {"hands": 100}),
        ("pencil", {"n_dots": 9}),
        ("gomoku", {"board_size": 19}),
    ):
        with pytest.raises(ValueError, match="游戏规则已固定"):
            validate_match_config(config, game_id)
    assert validate_match_config({}, "holdem") == {}
# ── DEFAULT_MATCH_CONFIG 从注册表派生 ──────────────────────────
def test_default_match_config_derived_from_registry():
    from bzplat.backend.contests.templates import DEFAULT_MATCH_CONFIG, default_match_config

    # 与各 spec.default_match_params 一致
    for gid in registry.all_ids():
        assert DEFAULT_MATCH_CONFIG[gid] == registry.get(gid).default_match_params
        assert default_match_config(gid) == registry.get(gid).default_match_params


# ── admin JUDGE_GAMES 从注册表派生 ─────────────────────────────
def test_judge_games_derived_from_registry():
    from bzplat.backend.api_routes import JUDGE_GAMES

    gids = {g["game_id"] for g in JUDGE_GAMES}
    assert gids == registry.all_ids()
