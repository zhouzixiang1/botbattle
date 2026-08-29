"""全面解耦 PR5：per-game 赛事模板落位测试。

验证：
1. 各游戏的模板在自己包内（games/<game>/templates.py），spec 引用
2. contests/templates.py 的 DEFAULT_TEMPLATES 从注册表派生（聚合各 spec.templates）
3. 历史模板仍可解析，``creation_enabled=false`` 不进新建列表
4. get_template/list_templates/resolve_stages/resolve_template 经注册表派生工作
5. 新建目录冻结推荐人数、用途、时长、Swiss 轮数分档与淘汰决胜契约
"""
from __future__ import annotations

import pytest

from bzplat.backend.contests.templates import (
    DEFAULT_TEMPLATES,
    default_template_id,
    get_template,
    list_templates,
    resolve_stages,
    resolve_template,
)
from bzplat.backend.games import registry


# ── 各游戏模板在自己包内 ──────────────────────────────────────
def test_each_game_has_templates_module():
    """各游戏包有自己的 templates.py（独立模块）。"""
    from bzplat.backend.games import holdem, gomoku, pencil

    for mod in (holdem.templates, gomoku.templates, pencil.templates):
        assert hasattr(mod, "TEMPLATES")
        assert isinstance(mod.TEMPLATES, list) and len(mod.TEMPLATES) >= 1


def test_specs_reference_local_templates():
    """各 spec.templates 引用本包 templates.TEMPLATES（不经 contests）。"""
    assert len(registry.get("holdem").templates) == 8
    assert len(registry.get("gomoku").templates) == 6
    assert len(registry.get("pencil").templates) == 5


# ── DEFAULT_TEMPLATES 从注册表派生 ────────────────────────────
def test_default_templates_derived_from_registry():
    """DEFAULT_TEMPLATES 是三个 GameSpec 共 19 个模板的无损聚合。"""
    # 聚合注册表
    aggregated = {}
    for gid in registry.all_ids():
        for t in registry.get(gid).templates:
            aggregated[t["id"]] = t
    assert set(aggregated.keys()) == set(DEFAULT_TEMPLATES.keys())
    assert len(DEFAULT_TEMPLATES) == 19


def test_default_templates_has_all():
    ids = set(DEFAULT_TEMPLATES.keys())
    expected = {
        "holdem_dup_rr", "holdem_rr", "holdem_swiss_ranked",
        "holdem_swiss_top8_ranked", "holdem_swiss_ko",
        "holdem_top8_ranked", "holdem_prelim_swiss",
        "holdem_final_ranked",
        "board_rr", "gomoku_rr", "gomoku_swiss_ranked",
        "gomoku_swiss_top8_ranked", "gomoku_group_drr_ko",
        "gomoku_swiss_ko",
        "pencil_drr", "pencil_group_drr_ko", "pencil_swiss_ranked",
        "pencil_swiss_ko", "pencil_ko",
    }
    assert ids == expected


# ── 模板内容正确性 ────────────────────────────────────────────
def test_template_game_ids_correct():
    """每个模板的 game_id 与其归属游戏一致。"""
    for tid, t in DEFAULT_TEMPLATES.items():
        gid = t["game_id"]
        # 该模板应在该游戏的 spec.templates 里
        game_tpls = {x["id"] for x in registry.get(gid).templates}
        assert tid in game_tpls, f"{tid} 声称 game_id={gid} 但不在该游戏 spec.templates"


def test_template_scoring_correct():
    """holdem 模板用 poker_3_1_0；棋类模板用 ccgc_2_1_0。"""
    for tid, t in DEFAULT_TEMPLATES.items():
        scorings = {s.get("scoring") for s in t["stages"]}
        if t["game_id"] == "holdem":
            assert scorings == {"poker_3_1_0"}, f"{tid} 应全用 poker_3_1_0"
        else:
            assert scorings == {"ccgc_2_1_0"}, f"{tid} 应全用 ccgc_2_1_0"


def test_templates_have_stages():
    """每个模板有非空 stages。"""
    for tid, t in DEFAULT_TEMPLATES.items():
        assert isinstance(t["stages"], list) and len(t["stages"]) >= 1
        for s in t["stages"]:
            assert "type" in s and "scoring" in s


def test_holdem_recommends_duplicate_round_robin_as_fair_default():
    holdem = list_templates(game_id="holdem")
    recommended = [template for template in holdem if template.get("recommended")]
    assert [template["id"] for template in recommended] == ["holdem_dup_rr"]
    assert holdem[0]["id"] == "holdem_dup_rr"
    assert recommended[0]["name"] == "德州：复式单循环（公平优先）"
    assert "同一副牌交换座位" in recommended[0]["summary"]
    assert "场次数随人数平方增长" in recommended[0]["summary"]
    assert default_template_id("holdem") == "holdem_dup_rr"


def test_swiss_templates_remain_explicit_large_scale_options():
    holdem = {template["id"]: template for template in list_templates(game_id="holdem")}
    for template_id in (
        "holdem_swiss_ranked",
        "holdem_swiss_top8_ranked",
        "holdem_swiss_ko",
        "holdem_prelim_swiss",
    ):
        template = holdem[template_id]
        assert template.get("recommended") is not True
        assert template["recommended_min"] >= 9
        assert any(stage["type"] == "swiss" for stage in template["stages"])


def test_creation_templates_publish_complete_guidance_metadata():
    allowed_purposes = {"fairness", "speed", "ranking", "championship"}
    allowed_times = {"short", "medium", "long"}
    for template in list_templates():
        minimum = template.get("recommended_min")
        maximum = template.get("recommended_max")
        assert isinstance(minimum, int) and not isinstance(minimum, bool)
        assert minimum >= 2
        assert maximum is None or (
            isinstance(maximum, int)
            and not isinstance(maximum, bool)
            and maximum >= minimum
        )
        assert template.get("purpose") in allowed_purposes
        assert template.get("time_class") in allowed_times

    for game_id in registry.all_ids():
        assert sum(
            template.get("recommended") is True
            for template in list_templates(game_id=game_id)
        ) == 1


def test_gomoku_swiss_templates_freeze_official_round_bands():
    expected = [
        {"min_participants": 13, "max_participants": 15, "rounds": 7},
        {"min_participants": 16, "max_participants": 20, "rounds": 9},
        {"min_participants": 21, "max_participants": None, "rounds": 11},
    ]
    gomoku = {
        template["id"]: template
        for template in list_templates(game_id="gomoku")
    }
    for template_id in (
        "gomoku_swiss_ranked",
        "gomoku_swiss_top8_ranked",
        "gomoku_swiss_ko",
    ):
        swiss = next(
            stage
            for stage in gomoku[template_id]["stages"]
            if stage["type"] == "swiss"
        )
        assert swiss["rounds"] == 0
        assert swiss["swiss_round_bands"] == expected


def test_holdem_and_gomoku_knockouts_freeze_paired_swap_tiebreak():
    marker = "paired_swap_until_decided"
    for game_id in ("holdem", "gomoku"):
        for template in list_templates(game_id=game_id):
            for stage in template["stages"]:
                if stage["type"] == "single_elimination":
                    assert stage["tiebreak"] == marker
    for template in list_templates(game_id="pencil"):
        for stage in template["stages"]:
            if stage["type"] == "single_elimination":
                assert "tiebreak" not in stage


def test_omitted_template_uses_game_scoped_registry_default():
    tid, gid, stages, match_config = resolve_template(None, game_id="holdem")
    assert (tid, gid, match_config) == ("holdem_dup_rr", "holdem", {})
    assert stages == get_template("holdem_dup_rr")["stages"]

    # 没有 recommended 元数据的游戏继续使用自己的代码顺序，不猜成德州。
    board_default = list_templates(game_id="gomoku")[0]
    assert default_template_id("gomoku") == board_default["id"]
    _tid, board_gid, _stages, _config = resolve_template(None, game_id="gomoku")
    assert board_gid == "gomoku"


def test_omitted_game_stays_with_first_game_when_another_game_is_recommended(
    monkeypatch: pytest.MonkeyPatch,
):
    first_template = list_templates()[0]
    assert first_template["game_id"] == registry.judge_games()[0]["game_id"]
    other_game_template = next(
        template
        for template in DEFAULT_TEMPLATES.values()
        if template["game_id"] != first_template["game_id"]
        and template.get("creation_enabled", True) is not False
    )
    monkeypatch.setitem(other_game_template, "recommended", True)

    expected = default_template_id(first_template["game_id"])
    assert default_template_id() == expected == "holdem_dup_rr"
    tid, gid, _stages, _config = resolve_template(None)
    assert (tid, gid) == (expected, first_template["game_id"])


# ── get/list/resolve 经注册表派生 ──────────────────────────────
def test_get_template_returns_deepcopy():
    t1 = get_template("holdem_swiss_ko")
    t2 = get_template("holdem_swiss_ko")
    assert t1 == t2
    t1["stages"][0]["advance_count"] = 999  # 改副本
    assert get_template("holdem_swiss_ko")["stages"][0]["advance_count"] == 8  # 原未变


def test_get_template_unknown_returns_none():
    assert get_template("nonexistent") is None


def test_list_templates_returns_only_creation_enabled_by_default():
    tpls = list_templates()
    ids = {t["id"] for t in tpls}
    assert len(tpls) == 18
    assert ids == set(DEFAULT_TEMPLATES) - {"holdem_final_ranked"}
    assert {t["id"] for t in list_templates(include_disabled=True)} == set(
        DEFAULT_TEMPLATES
    )


def test_disabled_historical_holdem_final_remains_readable_but_cannot_resolve_new():
    historical = get_template("holdem_final_ranked")
    assert historical is not None
    assert historical["creation_enabled"] is False
    with pytest.raises(ValueError, match="已停用新建"):
        resolve_stages("holdem_final_ranked")
    with pytest.raises(ValueError, match="已停用新建"):
        resolve_template("holdem_final_ranked")

    for template_id in ("gomoku_group_drr_ko", "gomoku_swiss_ko"):
        assert resolve_stages(template_id)[0] == template_id


def test_resolve_template_with_match_config():
    tid, gid, stages, mc = resolve_template("pencil_group_drr_ko")
    assert gid == "pencil" and mc == {}  # 规则参数已钉死，match_config 恒空


def test_resolve_stages_custom_stages():
    """自定义 stages 直接用，不经模板。"""
    custom = [{"key": "x", "type": "round_robin", "scoring": "poker_3_1_0"}]
    tid, gid, stages = resolve_stages(None, custom, game_id="holdem")
    assert tid == "custom" and gid == "holdem" and stages == custom
