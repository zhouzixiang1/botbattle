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


# 模块级 __getattr__：让 DEFAULT_TEMPLATES / DEFAULT_MATCH_CONFIG 延迟派生，避免
# 模块顶部 import 注册表时的循环依赖（games/<game>/templates.py 会导入计分常量）。
def __getattr__(name: str):
    if name == "DEFAULT_TEMPLATES":
        return _get_default_templates()
    if name == "DEFAULT_MATCH_CONFIG":
        return _get_default_match_config()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def default_match_config(game_id: str) -> dict[str, Any]:
    """返回指定游戏的默认 match_config（深拷贝）。"""
    from bzplat.backend.games import normalize_game_id, registry as _reg

    gid = normalize_game_id(game_id)
    return copy.deepcopy(_reg.get(gid).default_match_params)


def get_template(template_id: str) -> dict[str, Any] | None:
    t = _get_default_templates().get(template_id)
    return copy.deepcopy(t) if t else None


def list_templates(
    *, game_id: str | None = None, include_disabled: bool = False
) -> list[dict[str, Any]]:
    """列出代码注册表中可新建的内置模板。

    ``creation_enabled=false`` 的模板只供历史赛事和演示快照解析；
    ``get_template`` 仍可读，但默认列表不对新建入口暴露。
    """
    templates = _get_default_templates().values()
    if game_id is not None:
        from bzplat.backend.games import normalize_game_id

        gid = normalize_game_id(game_id)
        templates = (t for t in templates if t.get("game_id") == gid)
    if not include_disabled:
        templates = (
            t for t in templates if t.get("creation_enabled", True) is not False
        )
    return [copy.deepcopy(t) for t in templates]


def _require_creation_enabled(template_id: str, template: dict[str, Any]) -> None:
    if template.get("creation_enabled", True) is False:
        raise ValueError(
            f"模板 {template_id} 已停用新建，仅供历史赛事展示"
        )


def resolve_stages(
    template_id: str | None,
    stages: list[dict[str, Any]] | None = None,
    *,
    game_id: str | None = None,
) -> tuple[str, str, list[dict[str, Any]]]:
    """返回代码模板的 (template_id, game_id, stages)。"""
    if stages is not None:
        if not stages:
            raise ValueError("自定义 stages 须为非空数组")
        tid = "custom" if template_id is None else template_id
        if template_id is not None:
            declared = get_template(template_id)
            if declared is not None:
                _require_creation_enabled(template_id, declared)
        if not isinstance(game_id, str) or not game_id.strip():
            raise ValueError("自定义阶段必须明确指定 game_id")
        from bzplat.backend.games import normalize_game_id

        gid = normalize_game_id(game_id)
        return tid, gid, copy.deepcopy(stages)
    tid = "holdem_swiss_ko" if template_id is None else template_id
    tpl = get_template(tid)
    if not tpl:
        raise ValueError(f"未知模板: {tid}")
    _require_creation_enabled(tid, tpl)
    from bzplat.backend.games import normalize_game_id

    template_game_id = normalize_game_id(tpl["game_id"])
    if game_id is not None:
        requested_game_id = normalize_game_id(game_id)
        if requested_game_id != template_game_id:
            raise ValueError(
                f"模板 {tid} 属于游戏 {template_game_id}，不能用于游戏 {requested_game_id}"
            )
    return tid, template_game_id, copy.deepcopy(tpl["stages"])


def resolve_template(
    template_id: str | None,
    *,
    game_id: str | None = None,
) -> tuple[str, str, list[dict[str, Any]], dict[str, Any]]:
    """返回代码模板的 (template_id, game_id, stages, match_config)。"""
    tid = "holdem_swiss_ko" if template_id is None else template_id
    tpl = None
    base = get_template(tid)
    if base:
        _require_creation_enabled(tid, base)
        tpl = {
            "game_id": base["game_id"],
            "stages": base["stages"],
            "match_config": default_match_config(base["game_id"]),
        }
    if not tpl:
        raise ValueError(f"未知模板: {tid}")
    from bzplat.backend.games import normalize_game_id

    template_game_id = normalize_game_id(tpl["game_id"])
    requested_game_id = normalize_game_id(game_id) if game_id is not None else None
    if requested_game_id is not None and requested_game_id != template_game_id:
        raise ValueError(
            f"模板 {tid} 属于游戏 {template_game_id}，不能用于游戏 {requested_game_id}"
        )
    return (
        tid,
        template_game_id,
        copy.deepcopy(tpl["stages"]),
        copy.deepcopy(tpl["match_config"]),
    )


def points_for_result(scoring: str, winner: int | None, side: int) -> float:
    """side: 0=A, 1=B。"""
    if scoring == SCORING_CCGC:
        win_pts, draw_pts, loss_pts = 2.0, 1.0, 0.0
    elif scoring == SCORING_POKER:
        win_pts, draw_pts, loss_pts = 3.0, 1.0, 0.0
    else:
        raise ValueError(f"未知计分规则: {scoring!r}")
    if winner is None:
        return draw_pts
    if winner == side:
        return win_pts
    return loss_pts
