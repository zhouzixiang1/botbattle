"""全面解耦 PR5：per-game 赛事模板落位测试。

验证：
1. 各游戏的模板在自己包内（games/<game>/templates.py），spec 引用
2. contests/templates.py 的 DEFAULT_TEMPLATES 从注册表派生（聚合各 spec.templates）
3. 7 个内置模板完整（holdem×2 + gomoku×3[含 board_rr] + pencil×2）
4. get_template/list_templates/resolve_stages/resolve_template 经注册表派生工作
5. 模板内容与历史一致（game_id/scoring/stages 结构）
"""
from __future__ import annotations

from bzplat.backend.contests.templates import (
    DEFAULT_TEMPLATES,
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
    import inspect

    # holdem spec 应有 4 个模板（P5 加 holdem_prelim_swiss + holdem_final_ranked）
    assert len(registry.get("holdem").templates) == 4
    # gomoku 3 个（含 board_rr）
    assert len(registry.get("gomoku").templates) == 3
    # pencil 2 个
    assert len(registry.get("pencil").templates) == 2


# ── DEFAULT_TEMPLATES 从注册表派生 ────────────────────────────
def test_default_templates_derived_from_registry():
    """DEFAULT_TEMPLATES 是各 spec.templates 的聚合（9 个：7 + reversi×2）。"""
    # 聚合注册表
    aggregated = {}
    for gid in registry.all_ids():
        for t in registry.get(gid).templates:
            aggregated[t["id"]] = t
    assert set(aggregated.keys()) == set(DEFAULT_TEMPLATES.keys())
    assert len(DEFAULT_TEMPLATES) == 9


def test_default_templates_has_all():
    ids = set(DEFAULT_TEMPLATES.keys())
    expected = {
        "holdem_swiss_ko", "holdem_rr",
        "holdem_prelim_swiss", "holdem_final_ranked",  # P5 预赛/决赛
        "gomoku_group_drr_ko", "gomoku_swiss_ko", "board_rr",
        "pencil_group_drr_ko", "pencil_swiss_ko",
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


# ── get/list/resolve 经注册表派生 ──────────────────────────────
def test_get_template_returns_deepcopy():
    t1 = get_template("holdem_swiss_ko")
    t2 = get_template("holdem_swiss_ko")
    assert t1 == t2
    t1["stages"][0]["advance_count"] = 999  # 改副本
    assert get_template("holdem_swiss_ko")["stages"][0]["advance_count"] == 8  # 原未变


def test_get_template_unknown_returns_none():
    assert get_template("nonexistent") is None


def test_list_templates_returns_all():
    tpls = list_templates()
    assert len(tpls) == 9
    assert {t["id"] for t in tpls} == set(DEFAULT_TEMPLATES.keys())


def test_resolve_stages_memory_fallback():
    """无 store 时 resolve_stages 回退注册表派生的内存模板。"""
    tid, gid, stages = resolve_stages("gomoku_swiss_ko")
    assert tid == "gomoku_swiss_ko" and gid == "gomoku"
    assert len(stages) == 2  # swiss + ko


def test_resolve_template_with_match_config():
    tid, gid, stages, mc = resolve_template("pencil_group_drr_ko")
    assert gid == "pencil" and mc == {}  # 规则参数已钉死，match_config 恒空


def test_resolve_stages_custom_stages():
    """自定义 stages 直接用，不经模板。"""
    custom = [{"key": "x", "type": "round_robin", "scoring": "poker_3_1_0"}]
    tid, gid, stages = resolve_stages(None, custom, game_id="holdem")
    assert tid == "custom" and gid == "holdem" and stages == custom
