"""默认赛事阶段模板（全面解耦 PR5：模板从 games/<game>/templates.py 派生）。

本模块保留通用赛事设施（SCORING 常量、get/list/resolve、points_for_result），
但 DEFAULT_TEMPLATES 不再手写——从 games 注册表（各 spec.templates）派生，
单一真相。每游戏的内置模板在 games/<game>/templates.py。
"""
from __future__ import annotations

import copy
from typing import Any

# scoring: poker_3_1_0 | ccgc_2_1_0（通用常量，各游戏模板引用）
SCORING_POKER = "poker_3_1_0"
SCORING_CCGC = "ccgc_2_1_0"


def _build_default_match_config() -> dict[str, dict[str, Any]]:
    """从注册表派生每游戏默认 match_config（延迟，避免循环 import）。"""
    from bzplat.backend.games import registry as _reg

    return {gid: copy.deepcopy(_reg.get(gid).default_match_params) for gid in _reg.all_ids()}


def _build_default_templates() -> dict[str, dict[str, Any]]:
    """从注册表派生 DEFAULT_TEMPLATES（各 spec.templates 聚合，延迟避免循环 import）。"""
    from bzplat.backend.games import registry as _reg

    out: dict[str, dict[str, Any]] = {}
    for gid in _reg.all_ids():
        for t in _reg.get(gid).templates:
            out[t["id"]] = copy.deepcopy(t)
    return out


# 模块级缓存（首次访问时构建，之后复用；注册表在 import 后稳定）
_DEFAULT_MATCH_CONFIG_CACHE: dict[str, dict[str, Any]] | None = None
_DEFAULT_TEMPLATES_CACHE: dict[str, dict[str, Any]] | None = None


def _get_default_match_config() -> dict[str, dict[str, Any]]:
    """每游戏默认 match_config（从注册表派生，缓存）。"""
    global _DEFAULT_MATCH_CONFIG_CACHE
    if _DEFAULT_MATCH_CONFIG_CACHE is None:
        _DEFAULT_MATCH_CONFIG_CACHE = _build_default_match_config()
    return _DEFAULT_MATCH_CONFIG_CACHE


def _get_default_templates() -> dict[str, dict[str, Any]]:
    """默认赛事模板（从注册表派生，缓存）。"""
    global _DEFAULT_TEMPLATES_CACHE
    if _DEFAULT_TEMPLATES_CACHE is None:
        _DEFAULT_TEMPLATES_CACHE = _build_default_templates()
    return _DEFAULT_TEMPLATES_CACHE


# 模块级 __getattr__：让 DEFAULT_TEMPLATES / DEFAULT_MATCH_CONFIG 作为延迟字典属性
# 访问（保旧 DEFAULT_TEMPLATES["id"] / DEFAULT_MATCH_CONFIG[gid] 字典语法），
# 避免模块顶部 import 注册表时的循环依赖（games/<game>/templates.py import 本模块取 SCORING_*）。
def __getattr__(name: str):
    if name == "DEFAULT_TEMPLATES":
        return _get_default_templates()
    if name == "DEFAULT_MATCH_CONFIG":
        return _get_default_match_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def default_match_config(game_id: str | None) -> dict[str, Any]:
    """返回指定游戏的默认 match_config（深拷贝）。"""
    gid = (game_id or "holdem").strip().lower()
    from bzplat.backend.games import registry as _reg

    try:
        return copy.deepcopy(_reg.get(gid).default_match_params)
    except KeyError:
        return copy.deepcopy(_reg.get("holdem").default_match_params)


def get_template(template_id: str) -> dict[str, Any] | None:
    t = _get_default_templates().get(template_id)
    return copy.deepcopy(t) if t else None


def list_templates() -> list[dict[str, Any]]:
    return [copy.deepcopy(t) for t in _get_default_templates().values()]


def resolve_stages(
    template_id: str | None,
    stages: list[dict[str, Any]] | None = None,
    *,
    game_id: str | None = None,
    store=None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """返回 (template_id, game_id, stages)。

    若提供 store，优先从 contest_templates 表读模板（含 admin 覆盖）；
    否则回退注册表派生的 DEFAULT_TEMPLATES（供无 store 的测试用）。
    """
    if stages:
        tid = template_id or "custom"
        gid = game_id or "holdem"
        return tid, gid, copy.deepcopy(stages)
    tid = template_id or "holdem_swiss_ko"
    tpl = None
    if store is not None:
        row = store.get_contest_template(tid)
        if row:
            tpl = {
                "id": row["id"],
                "name": row["name"],
                "game_id": row["game_id"],
                "stages": row.get("stages") or [],
                "match_config": row.get("match_config") or {},
            }
    if tpl is None:
        tpl = get_template(tid)
    if not tpl:
        raise ValueError(f"未知模板: {tid}")
    return tid, game_id or tpl["game_id"], copy.deepcopy(tpl["stages"])


def resolve_template(
    template_id: str | None,
    *,
    game_id: str | None = None,
    store=None,
) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
    """返回 (template_id, game_id, stages, match_config)。优先读 store 表。"""
    tid = template_id or "holdem_swiss_ko"
    tpl = None
    if store is not None:
        row = store.get_contest_template(tid)
        if row:
            tpl = {
                "game_id": row["game_id"],
                "stages": row.get("stages") or [],
                "match_config": row.get("match_config") or {},
            }
    if tpl is None:
        base = get_template(tid)
        if base:
            tpl = {
                "game_id": base["game_id"],
                "stages": base["stages"],
                "match_config": default_match_config(base["game_id"]),
            }
    if not tpl:
        raise ValueError(f"未知模板: {tid}")
    return tid, game_id or tpl["game_id"], copy.deepcopy(tpl["stages"]), copy.deepcopy(tpl["match_config"])


def points_for_result(scoring: str, winner: int | None, side: int) -> float:
    """side: 0=A, 1=B。"""
    if scoring == SCORING_CCGC:
        win_pts, draw_pts, loss_pts = 2.0, 1.0, 0.0
    else:
        win_pts, draw_pts, loss_pts = 3.0, 1.0, 0.0
    if winner is None:
        return draw_pts
    if winner == side:
        return win_pts
    return loss_pts
