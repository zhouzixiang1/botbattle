"""游戏注册表框架测试（PR1：GameSpec + GameRegistry + 派生分发）。

验证全面解耦的核心契约：
- 注册表是单一真相（三游戏已注册、id 一致）
- 未知 game_id 报错而非静默兜底 holdem（行为修正）
- run_session 经注册表分发到正确引擎
- 协议 dumps/loads/fail_response 按游戏路由
- validate_match_config / default_match_config 按游戏
- schema 的 REGISTERED_ENGINES/VALID_GAME_IDS 与注册表一致
- 公开裁判源码元信息从注册表派生
"""
from __future__ import annotations

import asyncio
import json

import pytest

from bzplat.backend.games import (
    GAME_LABELS,
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
def test_registry_has_all_games():
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
    # 与各 spec.label 一致
    for gid in registry.all_ids():
        assert GAME_LABELS[gid] == registry.get(gid).label


def test_time_control_registry_has_stable_ids_and_defaults():
    expected = {
        "holdem": (
            "holdem_per_decision_60s_v1",
            ["holdem_per_decision_60s_v1"],
        ),
        "gomoku": (
            "gomoku_per_side_total_900s_v1",
            [
                "gomoku_per_side_total_900s_v1",
                "gomoku_per_side_total_300s_v1",
            ],
        ),
        "pencil": (
            "pencil_per_side_total_900s_v1",
            [
                "pencil_per_side_total_900s_v1",
                "pencil_per_decision_1s_v1",
            ],
        ),
    }
    for game_id, (default_id, allowed_ids) in expected.items():
        spec = registry.get(game_id)
        assert spec.default_time_control_id == default_id
        assert [item.id for item in spec.time_controls] == allowed_ids
        assert spec.resolve_time_control(None).id == default_id
        assert all(item.applies_to == "both_bots" for item in spec.time_controls)


@pytest.mark.parametrize(
    ("game_id", "bad_id"),
    [
        ("gomoku", "pencil_per_decision_1s_v1"),
        ("pencil", "PENCIL_PER_DECISION_1S_V1"),
        ("pencil", " pencil_per_decision_1s_v1"),
        ("holdem", ""),
    ],
)
def test_time_control_resolution_is_exact_and_cross_game_closed(game_id, bad_id):
    with pytest.raises(ValueError, match="不支持时限"):
        registry.get(game_id).resolve_time_control(bad_id)


def test_time_control_eta_uses_frozen_option():
    assert registry.get("gomoku").eta_for_match(
        {"time_control_id": "gomoku_per_side_total_300s_v1"}
    ) == 600
    assert registry.get("gomoku").eta_for_match(
        {"time_control_id": "gomoku_per_side_total_900s_v1"}
    ) == 1800
    assert registry.get("pencil").eta_for_match(
        {"time_control_id": "pencil_per_decision_1s_v1"}
    ) == 84


def test_contest_source_candidate_capabilities_are_game_registered():
    """来源候选类型由 GameSpec 声明；通用 API 不枚举具体游戏。"""
    assert registry.get("gomoku").contest_source_candidate_kind == "protected_seed"
    assert registry.get("pencil").contest_source_candidate_kind == "navigation"
    assert registry.get("holdem").contest_source_candidate_kind is None


def test_contest_source_candidate_capability_must_match_local_templates():
    """GameSpec 与模板能力漂移应在注册阶段失败，而不是路由时猜测。"""
    from dataclasses import replace

    with pytest.raises(ValueError, match="必须与赛事模板来源能力一致"):
        replace(
            registry.get("gomoku"),
            contest_source_candidate_kind="navigation",
        )
    with pytest.raises(ValueError, match="不是受支持"):
        replace(
            registry.get("holdem"),
            contest_source_candidate_kind="unknown",
        )


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


def test_normalize_requires_registered_explicit_game():
    """规范化不猜测游戏；空值和未知值都明确失败。"""
    with pytest.raises(ValueError, match="不可为空"):
        normalize_game_id("")
    with pytest.raises(ValueError, match="不可为空"):
        normalize_game_id(None)
    with pytest.raises(ValueError, match="未知游戏"):
        normalize_game_id("chess")
    assert normalize_game_id("  PENCIL  ") == "pencil"


# ── run_session 经注册表分发 ──────────────────────────────────
def test_run_session_gomoku_via_registry():
    """registry.run_session('gomoku') 实际跑竞赛规则状态机。"""
    normal = {0: iter([(10, 10), (11, 11)]), 1: iter([(0, 0), (0, 1)])}

    async def decide(player, req):
        phase = req["phase"]
        if phase == "opening_proposal":
            return {"response": {"action": "opening", "white2": {"x": 7, "y": 8}, "black3": {"x": 8, "y": 8}, "n": 2}}
        if phase == "swap_choice":
            return {"response": {"action": "swap", "swap": False}}
        if phase == "white4":
            return {"response": {"action": "move", "x": 6, "y": 8}}
        if phase == "black5_candidates":
            return {"response": {"action": "black5_candidates", "points": [{"x": 9, "y": 9}, {"x": 5, "y": 5}]}}
        if phase == "black5_select":
            return {"response": {"action": "black5_select", "index": 0}}
        x, y = next(normal[player])
        return {"response": {"action": "move", "x": x, "y": y}}

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
    # holdem：Botzone 裸整数 -1（fold）；棋类：非法坐标 {-99,-99}。
    assert fail_response("holdem") == -1
    assert fail_response("gomoku") == {"action": "move", "x": -99, "y": -99}
    assert fail_response("pencil") == {"x": -99, "y": -99}


def test_protocol_dumps_loads_roundtrip():
    response = {"response": {"action": "move", "x": 5, "y": 6}}
    line = json.dumps(response)
    back = loads("gomoku", line)
    assert back == response
    # holdem 协议
    holdem_response = {"response": 0}
    assert loads("holdem", json.dumps(holdem_response)) == holdem_response


def test_protocol_loads_board_rejects_garbage():
    """棋类协议不得把空串/坏 JSON/非对象静默降级成非法落子。"""
    for game_id, line in (
        ("gomoku", ""),
        ("gomoku", "not json"),
        ("pencil", ""),
    ):
        with pytest.raises(json.JSONDecodeError):
            loads(game_id, line)
    with pytest.raises(ValueError, match="JSON 对象"):
        loads("gomoku", "[]")


# ── validate / default match_config ───────────────────────────
def test_validate_match_config_holdem():
    assert validate_match_config("holdem", {}) == {}
    with pytest.raises(ValueError, match="游戏规则已固定"):
        validate_match_config("holdem", {"hands": 100})


def test_validate_match_config_pencil():
    assert validate_match_config("pencil", {}) == {}
    with pytest.raises(ValueError, match="游戏规则已固定"):
        validate_match_config("pencil", {"n_dots": 11})


def test_validate_match_config_gomoku_no_params():
    """五子棋单局无可调参数，非空对象显式拒绝。"""
    assert validate_match_config("gomoku", {}) == {}
    with pytest.raises(ValueError, match="游戏规则已固定"):
        validate_match_config("gomoku", {"foo": 1})


def test_default_match_config_per_game():
    # 所有游戏规则参数已钉死，default_match_config 恒为空 dict。
    assert default_match_config("holdem") == {}
    assert default_match_config("gomoku") == {}
    assert default_match_config("pencil") == {}


# ── 编排特化函数（spec 上的能力）──────────────────────────────
def test_normalize_delta_per_game():
    # Holdem 筹码差除以大盲 100，得到整场大盲分差；棋类透传。
    assert registry.get("holdem").normalize_delta(500) == 5.0
    assert registry.get("gomoku").normalize_delta(1) == 1.0
    assert registry.get("pencil").normalize_delta(-1) == -1.0


# ── judge 元信息从注册表派生 ──────────────────────────────────
def test_judge_games_derived():
    games = registry.judge_games()
    ids = {g["game_id"] for g in games}
    assert ids == {"holdem", "gomoku", "pencil"}
    # 公开源码清单必须包含真正的纯规则实现；engine.py 只是平台适配层。
    # 由 GameSpec 按 game_id 派生可避免新增游戏时忘记公开权威规则文件。
    for game in games:
        assert f'{game["game_id"]}_judge.py' in game["source_files"]
        assert {"engine.py", "protocol.py", "result.py"}.issubset(game["source_files"])
        assert game["ruleset_id"]
        assert game["protocol_version"]
        assert game["rating_pool_id"]
        expected_shared = ["_board_protocol.py"] if game["game_id"] == "pencil" else []
        assert game["shared_source_files"] == expected_shared
        assert "params" not in game


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
        ok, detail = asyncio.run(
            preflight_bot(
                gid,
                path,
                runner,
                runtime_mode="traditional",
            )
        )
        assert ok, f"{gid}/traditional sample 预检失败: {detail}"


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


def test_dead_fields_removed():
    """无生产消费者的字段不得继续冒充 GameSpec 契约。"""
    from bzplat.backend.games.base import GameSpec
    import dataclasses

    field_names = {f.name for f in dataclasses.fields(GameSpec)}
    assert "eta_per_match_sec" not in field_names, "eta_per_match_sec 是死字段（通用层读 eta_for_match），已删"
    assert "frontend_module" not in field_names, "frontend_module 后端从不读，已删"
    assert "rounds_per_match" not in field_names
    assert "num_seats" not in field_names
    assert "judge_params" not in field_names


def test_session_factory_protocol_has_on_event():
    """SessionFactory Protocol 声明 on_event kwarg（与 run_session 唯一调用点对齐）。"""
    import inspect
    from bzplat.backend.games.base import SessionFactory

    sig = inspect.signature(SessionFactory.__call__)
    assert "on_event" in sig.parameters, "SessionFactory 须声明 on_event（run_session 必传）"
    # on_event 应是 keyword-only
    assert sig.parameters["on_event"].kind == inspect.Parameter.KEYWORD_ONLY
