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

from bzplat.backend.games import (
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
    assert validate_match_config("pencil", {}) == {"n_dots": 6}  # 默认 6（对齐裁判 25 格）
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
    assert default_match_config("pencil") == {"n_dots": 6}  # 对齐裁判 25 格
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


# ── preflight_check（bot 上传时试跑验证）──────────────────────
def test_all_games_have_preflight_check():
    """三游戏的 spec 都声明了 preflight_check（上传时试跑验响应）。"""
    for gid in ("holdem", "gomoku", "pencil"):
        spec = registry.get(gid)
        assert spec.preflight_check is not None, f"{gid} 缺 preflight_check"


def test_preflight_sample_bots_pass():
    """合格 sample bot 通过预检（发首请求，收到合法响应）。"""
    import asyncio
    from bzplat.backend.runtime.binary_runner import BinaryRunner
    from bzplat.backend.games import preflight_bot

    runner = BinaryRunner(prefer_local=True)
    samples = [
        ("holdem", "samples/callbot_linux_amd64"),
        ("gomoku", "samples/gomokubot_linux_amd64"),
        ("pencil", "samples/pencilbot_linux_amd64"),
    ]
    for gid, path in samples:
        ok, detail = asyncio.run(preflight_bot(gid, path, runner))
        assert ok, f"{gid} sample 应通过预检，实际: {detail}"


# ── GameSpec 接口诚实化（PR2：声明=使用，无死字段）──────────────
def test_default_scoring_per_game_not_all_poker():
    """default_scoring 按游戏区分（holdem=扑克 3-1-0；棋类=2-1-0），不再统一 holdem。"""
    assert registry.get("holdem").default_scoring == "poker_3_1_0"
    assert registry.get("gomoku").default_scoring == "ccgc_2_1_0"
    assert registry.get("pencil").default_scoring == "ccgc_2_1_0"


def test_default_scoring_consumed_by_validation_not_hardcoded():
    """validate_stage 的 scoring 默认值从 spec 派生（不再硬编码 poker_3_1_0）。

    棋类赛事不显式传 scoring 时应得 ccgc_2_1_0（而非被悄悄套用 holdem 的 3-1-0）。
    """
    from bzplat.backend.contests.validation import validate_stage

    # gomoku 阶段不传 scoring → 默认 ccgc_2_1_0（从 spec 派生，非硬编码 poker_3_1_0）
    g_stage = validate_stage({"type": "round_robin"}, 0, "gomoku")
    assert g_stage["scoring"] == "ccgc_2_1_0", "棋类 scoring 默认应是 ccgc_2_1_0"
    # holdem 阶段不传 scoring → 默认 poker_3_1_0
    h_stage = validate_stage({"type": "swiss"}, 0, "holdem")
    assert h_stage["scoring"] == "poker_3_1_0"
    # 显式传 scoring 仍生效
    custom = validate_stage({"type": "round_robin", "scoring": "poker_3_1_0"}, 0, "gomoku")
    assert custom["scoring"] == "poker_3_1_0"


def test_num_seats_field_present():
    """GameSpec 声明 num_seats（当前全 2，为 N 人游戏留钩子）。"""
    for gid in registry.all_ids():
        assert registry.get(gid).num_seats == 2, f"{gid} 当前应为 2 人"


def test_dead_fields_removed():
    """PR2 删除的死字段不再存在于 GameSpec（eta_per_match_sec/frontend_module/tier_for）。"""
    from bzplat.backend.games.base import GameSpec
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(GameSpec)}
    assert "eta_per_match_sec" not in field_names, "eta_per_match_sec 是死字段（通用层读 eta_for_match），已删"
    assert "frontend_module" not in field_names, "frontend_module 后端从不读，已删"
    assert "tier_for" not in field_names, "tier_for 字段冗余（registry.tier_for 统一走 tier_for_in），已删"


def test_session_factory_protocol_has_on_event():
    """SessionFactory Protocol 声明 on_event kwarg（与 run_session 唯一调用点对齐）。"""
    import inspect
    from bzplat.backend.games.base import SessionFactory

    sig = inspect.signature(SessionFactory.__call__)
    assert "on_event" in sig.parameters, "SessionFactory 须声明 on_event（run_session 必传）"
    # on_event 应是 keyword-only
    assert sig.parameters["on_event"].kind == inspect.Parameter.KEYWORD_ONLY
