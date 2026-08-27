"""赛制模板与阶段配置校验。

非法配置抛 ValueError（API 层转 HTTP 400），供 CRUD / preview / 建赛复用，
确保写入表的赛制始终可被 stages.py 的对阵生成器与 manager 状态机消费。
"""
from __future__ import annotations

import re
from typing import Any

from bzplat.backend.games import registry as _reg
from bzplat.backend.contests.stages import PAIR_SERIES_STAGE_TYPES
from bzplat.backend.store.schema import VALID_GAME_IDS

# 阶段类型（与 stages.generate_stage_pairings 对齐）
STAGE_TYPES = {
    "round_robin",
    "double_round_robin",
    "group_round_robin",
    "group_double_round_robin",
    "swiss",
    "single_elimination",
}
GROUP_TYPES = {"group_round_robin", "group_double_round_robin"}
SCORINGS = {"poker_3_1_0", "ccgc_2_1_0"}

_COMMON_STAGE_KEYS = frozenset({
    "key",
    "type",
    "scoring",
    "advance_count",
    "rest_after_minutes",
    "allow_bot_swap_in_rest",
    "round_stagger_minutes",
})
_STAGE_TYPE_KEYS: dict[str, frozenset[str]] = {
    "round_robin": frozenset({
        "duplicate",
        "allow_large_round_robin",
        "games_per_pair",
        "series_scoring",
    }),
    "double_round_robin": frozenset({
        "duplicate",
        "allow_large_round_robin",
        "ranking_mode",
        "ranking_scope",
        "games_per_pair",
        "series_scoring",
    }),
    "group_round_robin": frozenset({"group_count", "advance_per_group"}),
    "group_double_round_robin": frozenset({"group_count", "advance_per_group"}),
    "swiss": frozenset({
        "rounds",
        "games_per_pair",
        "series_scoring",
        "swiss_extra_rounds",
        "effective_rounds",
    }),
    "single_elimination": frozenset(),
}

_ID_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

SERIES_SCORING_AGGREGATE = "aggregate_match_points_v1"
_SERIES_STAGE_FIELDS = frozenset(
    {"games_per_pair", "series_scoring", "swiss_extra_rounds", "effective_rounds"}
)


def validate_template_id(tid: str) -> None:
    if not isinstance(tid, str) or not _ID_RE.match(tid):
        raise ValueError(
            f"模板 id 非法：{tid!r}（须以字母开头、仅小写字母数字下划线、2–32 字符）"
        )


def _game_spec(game_id: str | None):
    if not isinstance(game_id, str) or not game_id.strip():
        raise ValueError("game_id 不可为空")
    gid = game_id.strip().lower()
    try:
        return _reg.get(gid)
    except KeyError as exc:
        raise ValueError(
            f"game_id 非法：{gid!r}（允许 {sorted(VALID_GAME_IDS)}）"
        ) from exc


def _int_field(
    stage: dict[str, Any], key: str, *, minimum: int, label: str
) -> int | None:
    if key not in stage:
        return None
    value = stage[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} 须为 ≥{minimum} 的整数")
    return value


def _bool_field(stage: dict[str, Any], key: str, *, label: str) -> bool | None:
    if key not in stage:
        return None
    value = stage[key]
    if not isinstance(value, bool):
        raise ValueError(f"{label} 须为布尔值")
    return value


def validate_match_config(cfg: Any, game_id: str) -> dict:
    """赛制模板不接受游戏规则覆盖；仅空对象是合法内部结构。"""
    _game_spec(game_id)
    if not isinstance(cfg, dict):
        raise ValueError("match_config 必须是对象")
    if cfg:
        fields = ", ".join(sorted(str(key) for key in cfg))
        raise ValueError(f"游戏规则已固定，不接受 match_config 字段：{fields}")
    return {}


def validate_stage(stage: dict, idx: int, game_id: str) -> dict:
    """校验并返回规整后的单个阶段配置。

    scoring 默认值从该游戏的 spec.default_scoring 派生（而非硬编码 holdem 的
    poker_3_1_0——否则棋类赛事不显式传 scoring 时被悄悄套用 3-1-0 计分）。
    """
    if not isinstance(stage, dict):
        raise ValueError(f"阶段 {idx + 1} 必须是对象")
    stype = stage["type"] if "type" in stage else "round_robin"
    if not isinstance(stype, str) or stype not in STAGE_TYPES:
        raise ValueError(
            f"阶段 {idx + 1} type 非法：{stype!r}（允许 {sorted(STAGE_TYPES)}）"
        )
    unexpected = set(stage) - (_COMMON_STAGE_KEYS | _STAGE_TYPE_KEYS[stype])
    if unexpected:
        fields = ", ".join(sorted(str(key) for key in unexpected))
        raise ValueError(f"阶段 {idx + 1} 不接受字段：{fields}")
    # 默认 scoring 只能从已注册游戏的 spec 派生；未知游戏必须明确失败，不能
    # 悄悄套用德州扑克计分。
    spec = _game_spec(game_id)
    default_scoring = spec.default_scoring
    scoring = stage["scoring"] if "scoring" in stage else default_scoring
    if not isinstance(scoring, str) or scoring not in SCORINGS:
        raise ValueError(
            f"阶段 {idx + 1} scoring 非法：{scoring!r}（允许 {sorted(SCORINGS)}）"
        )
    out: dict[str, Any] = {
        "key": str(stage.get("key") or f"stage{idx + 1}"),
        "type": stype,
        "scoring": scoring,
    }

    # group_* 专属
    if stype in GROUP_TYPES:
        gc = stage.get("group_count", 4)
        if isinstance(gc, bool) or not isinstance(gc, int) or gc < 1:
            raise ValueError(f"阶段 {idx + 1} group_count 须为 ≥1 的整数")
        out["group_count"] = gc
        apg = _int_field(
            stage,
            "advance_per_group",
            minimum=1,
            label=f"阶段 {idx + 1} advance_per_group",
        )
        if apg is not None:
            out["advance_per_group"] = apg

    # swiss 专属
    if stype == "swiss":
        rounds = stage.get("rounds", 0)
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds < 0:
            raise ValueError(f"阶段 {idx + 1} rounds 须为 ≥0 的整数（0=按 log2(n) 自动）")
        out["rounds"] = rounds
        extra_rounds = _int_field(
            stage,
            "swiss_extra_rounds",
            minimum=0,
            label=f"阶段 {idx + 1} swiss_extra_rounds",
        )
        if extra_rounds is not None:
            out["swiss_extra_rounds"] = extra_rounds
        effective_rounds = _int_field(
            stage,
            "effective_rounds",
            minimum=1,
            label=f"阶段 {idx + 1} effective_rounds",
        )
        if effective_rounds is not None:
            out["effective_rounds"] = effective_rounds

    # 通用可选
    ac = _int_field(
        stage,
        "advance_count",
        minimum=1,
        label=f"阶段 {idx + 1} advance_count",
    )
    if ac is not None:
        out["advance_count"] = ac
    rm = _int_field(
        stage,
        "rest_after_minutes",
        minimum=0,
        label=f"阶段 {idx + 1} rest_after_minutes",
    )
    if rm is not None:
        out["rest_after_minutes"] = rm
    swap = _bool_field(
        stage,
        "allow_bot_swap_in_rest",
        label=f"阶段 {idx + 1} allow_bot_swap_in_rest",
    )
    if swap is not None:
        out["allow_bot_swap_in_rest"] = swap
    stagger = _int_field(
        stage,
        "round_stagger_minutes",
        minimum=0,
        label=f"阶段 {idx + 1} round_stagger_minutes",
    )
    if stagger is not None:
        out["round_stagger_minutes"] = stagger

    duplicate = _bool_field(
        stage, "duplicate", label=f"阶段 {idx + 1} duplicate"
    )
    if duplicate is not None:
        if duplicate and spec.build_match_plan is None:
            raise ValueError(f"游戏 {spec.game_id} 不支持 duplicate 赛制")
        out["duplicate"] = duplicate
    allow_large = _bool_field(
        stage,
        "allow_large_round_robin",
        label=f"阶段 {idx + 1} allow_large_round_robin",
    )
    if allow_large is not None:
        out["allow_large_round_robin"] = allow_large

    games_per_pair = _int_field(
        stage,
        "games_per_pair",
        minimum=1,
        label=f"阶段 {idx + 1} games_per_pair",
    )
    if games_per_pair is not None:
        maximum = spec.contest_games_per_pair_max
        if maximum is None:
            raise ValueError(f"游戏 {spec.game_id} 不支持 games_per_pair")
        if games_per_pair > maximum:
            raise ValueError(
                f"阶段 {idx + 1} games_per_pair 须为 1..{maximum} 的整数"
            )
        out["games_per_pair"] = games_per_pair

    if "series_scoring" in stage:
        series_scoring = stage["series_scoring"]
        if series_scoring != SERIES_SCORING_AGGREGATE:
            raise ValueError(
                f"阶段 {idx + 1} series_scoring 仅允许 "
                f"{SERIES_SCORING_AGGREGATE!r}"
            )
        if stype not in PAIR_SERIES_STAGE_TYPES:
            raise ValueError(f"阶段 {idx + 1} type 不支持系列聚合计分")
        out["series_scoring"] = series_scoring

    if out.get("series_scoring") == SERIES_SCORING_AGGREGATE:
        if "games_per_pair" not in out:
            raise ValueError(f"阶段 {idx + 1} 系列聚合计分缺少 games_per_pair")
    elif stype in {"double_round_robin", "swiss"} and "games_per_pair" in out:
        raise ValueError(
            f"阶段 {idx + 1} {stype} 的 games_per_pair 必须使用系列聚合计分"
        )
    if (
        "swiss_extra_rounds" in out or "effective_rounds" in out
    ) and out.get("series_scoring") != SERIES_SCORING_AGGREGATE:
        raise ValueError(
            f"阶段 {idx + 1} 瑞士附加轮数必须使用系列聚合计分"
        )

    if "ranking_mode" in stage:
        if stage["ranking_mode"] != "replace_top":
            raise ValueError(
                f"阶段 {idx + 1} ranking_mode 仅允许 'replace_top'"
            )
        out["ranking_mode"] = "replace_top"
    scope = _int_field(
        stage,
        "ranking_scope",
        minimum=1,
        label=f"阶段 {idx + 1} ranking_scope",
    )
    if scope is not None:
        if stage.get("ranking_mode") != "replace_top":
            raise ValueError(
                f"阶段 {idx + 1} ranking_scope 仅能与 ranking_mode=replace_top 同时使用"
            )
        out["ranking_scope"] = scope
    return out


def configure_games_per_pair(
    stages: list[dict[str, Any]],
    game_id: str,
    games_per_pair: int | None,
    *,
    capability: dict[str, Any] | None,
    stage_series_settings: dict[str, dict[str, Any]] | None = None,
    stage_capabilities: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Freeze a code-template series selection into validated stage snapshots.

    ``games_per_pair`` retains the legacy single-stage capability contract.
    ``stage_series_settings`` is the stage-keyed aggregate-series contract; an
    omitted map applies all template defaults, while an explicit map must cover
    every advertised stage.  Both paths remain bounded by ``GameSpec``.
    """
    import copy

    copied = copy.deepcopy(stages)
    if games_per_pair is not None and stage_series_settings is not None:
        raise ValueError(
            "games_per_pair 与 stage_series_settings 不能同时提交"
        )
    if capability is not None and stage_capabilities is not None:
        raise ValueError("赛事模板不能同时声明两种系列配置能力")

    if stage_capabilities is not None:
        return _configure_stage_series_settings(
            copied,
            game_id,
            stage_series_settings,
            stage_capabilities=stage_capabilities,
            legacy_games_per_pair=games_per_pair,
        )
    if stage_series_settings is not None:
        raise ValueError("当前赛事模板不支持 stage_series_settings")
    if capability is not None:
        if not isinstance(capability, dict):
            raise ValueError("赛事模板 games_per_pair_config 非法")
        spec = _game_spec(game_id)
        spec_max = spec.contest_games_per_pair_max
        minimum = capability.get("min")
        maximum = capability.get("max")
        default = capability.get("default")
        if (
            spec_max is None
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in (minimum, maximum, default)
            )
            or minimum != 1
            or not minimum <= default <= maximum <= spec_max
        ):
            raise ValueError("赛事模板 games_per_pair_config 与游戏能力不一致")

    explicit = [stage for stage in copied if "games_per_pair" in stage]
    if explicit and capability is None:
        raise ValueError("当前赛事模板不支持 games_per_pair")
    selected = games_per_pair
    if selected is None and capability is not None and not explicit:
        selected = int(capability["default"])
    if selected is not None:
        if explicit:
            raise ValueError(
                "games_per_pair 不能同时在赛事字段与 stages 中重复设置"
            )
        if isinstance(selected, bool) or not isinstance(selected, int):
            raise ValueError("games_per_pair 须为整数")
        if capability is None:
            raise ValueError("当前赛事模板不支持 games_per_pair")
        if (
            len(copied) != 1
            or copied[0].get("type", "round_robin") not in PAIR_SERIES_STAGE_TYPES
        ):
            raise ValueError(
                "games_per_pair 仅支持标记为可配置的单阶段 round_robin 模板"
            )
        copied[0]["games_per_pair"] = selected

    configured = [stage for stage in copied if "games_per_pair" in stage]
    if configured and (
        len(copied) != 1
        or len(configured) != 1
        or copied[0].get("type", "round_robin") not in PAIR_SERIES_STAGE_TYPES
    ):
        raise ValueError(
            "games_per_pair 仅支持标记为可配置的单阶段 round_robin 模板"
        )

    # Run the same strict stage validator here so direct Manager callers and the
    # HTTP model share one capability/range boundary.
    gid = _game_spec(game_id).game_id
    return [validate_stage(stage, idx, gid) for idx, stage in enumerate(copied)]


def _exact_config_int(
    value: Any, *, label: str, minimum: int | None = None
) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{label} 须为整数")
    if minimum is not None and value < minimum:
        raise ValueError(f"{label} 须为 ≥{minimum} 的整数")
    return value


def _validated_stage_series_capabilities(
    stages: list[dict[str, Any]],
    game_id: str,
    capabilities: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    if not isinstance(capabilities, list) or not capabilities:
        raise ValueError("赛事模板 stage_series_configs 非法")
    stage_by_key = {
        str(stage.get("key") or f"stage{idx + 1}"): stage
        for idx, stage in enumerate(stages)
    }
    spec = _game_spec(game_id)
    spec_max = spec.contest_games_per_pair_max
    if spec_max is None:
        raise ValueError("赛事模板系列能力与游戏能力不一致")
    out: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(capabilities):
        if not isinstance(raw, dict):
            raise ValueError("赛事模板 stage_series_configs 非法")
        allowed_keys = {"stage_key", "label", "games_per_pair", "swiss_extra_rounds"}
        unexpected = set(raw) - allowed_keys
        if unexpected:
            raise ValueError(
                "赛事模板 stage_series_configs 不接受字段："
                + ", ".join(sorted(str(key) for key in unexpected))
            )
        stage_key = raw.get("stage_key")
        label = raw.get("label")
        games = raw.get("games_per_pair")
        if (
            not isinstance(stage_key, str)
            or not stage_key
            or stage_key in out
            or stage_key not in stage_by_key
            or not isinstance(label, str)
            or not label.strip()
            or not isinstance(games, dict)
            or set(games) != {"default", "allowed_values"}
        ):
            raise ValueError("赛事模板 stage_series_configs 非法")
        stage_type = stage_by_key[stage_key].get("type", "round_robin")
        if stage_type not in PAIR_SERIES_STAGE_TYPES:
            raise ValueError(f"阶段 {stage_key} 不支持系列配置")
        default = _exact_config_int(
            games.get("default"), label=f"阶段 {stage_key} games_per_pair.default", minimum=1
        )
        values_raw = games.get("allowed_values")
        if not isinstance(values_raw, list) or not values_raw:
            raise ValueError(f"阶段 {stage_key} games_per_pair.allowed_values 非法")
        values = [
            _exact_config_int(
                value,
                label=f"阶段 {stage_key} games_per_pair.allowed_values",
                minimum=1,
            )
            for value in values_raw
        ]
        if len(values) != len(set(values)) or values != sorted(values):
            raise ValueError(
                f"阶段 {stage_key} games_per_pair.allowed_values 须严格递增且不重复"
            )
        if default not in values or max(values) > spec_max:
            raise ValueError(f"阶段 {stage_key} games_per_pair 能力非法")
        normalized: dict[str, Any] = {
            "stage_key": stage_key,
            "label": label.strip(),
            "games_per_pair": {
                "default": default,
                "allowed_values": values,
            },
        }
        extra = raw.get("swiss_extra_rounds")
        if extra is not None:
            if (
                stage_type != "swiss"
                or not isinstance(extra, dict)
                or set(extra) != {"default", "min", "max"}
            ):
                raise ValueError(f"阶段 {stage_key} swiss_extra_rounds 能力非法")
            minimum = _exact_config_int(
                extra.get("min"), label=f"阶段 {stage_key} swiss_extra_rounds.min", minimum=0
            )
            maximum = _exact_config_int(
                extra.get("max"), label=f"阶段 {stage_key} swiss_extra_rounds.max", minimum=0
            )
            extra_default = _exact_config_int(
                extra.get("default"),
                label=f"阶段 {stage_key} swiss_extra_rounds.default",
                minimum=0,
            )
            if not minimum <= extra_default <= maximum:
                raise ValueError(f"阶段 {stage_key} swiss_extra_rounds 能力非法")
            normalized["swiss_extra_rounds"] = {
                "default": extra_default,
                "min": minimum,
                "max": maximum,
            }
        elif stage_type == "swiss":
            raise ValueError(f"阶段 {stage_key} 缺少 swiss_extra_rounds 能力")
        out[stage_key] = normalized
    return out


def _configure_stage_series_settings(
    stages: list[dict[str, Any]],
    game_id: str,
    settings: dict[str, dict[str, Any]] | None,
    *,
    stage_capabilities: list[dict[str, Any]],
    legacy_games_per_pair: int | None,
) -> list[dict[str, Any]]:
    if legacy_games_per_pair is not None:
        raise ValueError(
            "games_per_pair 与 stage_series_settings 不能同时提交"
        )
    capabilities = _validated_stage_series_capabilities(
        stages, game_id, stage_capabilities
    )
    if settings is not None and not isinstance(settings, dict):
        raise ValueError("stage_series_settings 必须是对象")
    if settings is not None and not settings:
        raise ValueError("stage_series_settings 不能为空对象")
    requested = settings or {}
    unknown = set(requested) - set(capabilities)
    if unknown:
        raise ValueError(
            "stage_series_settings 包含未知阶段："
            + ", ".join(sorted(str(key) for key in unknown))
        )
    if settings is not None:
        missing = set(capabilities) - set(requested)
        if missing:
            raise ValueError(
                "stage_series_settings 缺少阶段："
                + ", ".join(sorted(missing))
            )
    stage_by_key = {
        str(stage.get("key") or f"stage{idx + 1}"): stage
        for idx, stage in enumerate(stages)
    }
    for stage_key, config in capabilities.items():
        raw = requested.get(stage_key)
        if raw is not None and not isinstance(raw, dict):
            raise ValueError(f"阶段 {stage_key} 配置必须是对象")
        raw = raw or {}
        allowed_fields = {"games_per_pair"}
        if "swiss_extra_rounds" in config:
            allowed_fields.add("swiss_extra_rounds")
        unexpected = set(raw) - allowed_fields
        if unexpected:
            raise ValueError(
                f"阶段 {stage_key} 不接受字段："
                + ", ".join(sorted(str(key) for key in unexpected))
            )
        stage = stage_by_key[stage_key]
        existing_games = stage.get("games_per_pair")
        selected_games = raw.get(
            "games_per_pair",
            existing_games
            if existing_games is not None
            else config["games_per_pair"]["default"],
        )
        selected_games = _exact_config_int(
            selected_games, label=f"阶段 {stage_key} games_per_pair", minimum=1
        )
        if selected_games not in config["games_per_pair"]["allowed_values"]:
            allowed = config["games_per_pair"]["allowed_values"]
            raise ValueError(
                f"阶段 {stage_key} games_per_pair 仅允许 {allowed}"
            )
        stage["games_per_pair"] = selected_games
        stage["series_scoring"] = SERIES_SCORING_AGGREGATE
        stage.pop("effective_rounds", None)
        if "swiss_extra_rounds" in config:
            extra_cfg = config["swiss_extra_rounds"]
            existing_extra = stage.get("swiss_extra_rounds")
            selected_extra = raw.get(
                "swiss_extra_rounds",
                existing_extra
                if existing_extra is not None
                else extra_cfg["default"],
            )
            selected_extra = _exact_config_int(
                selected_extra,
                label=f"阶段 {stage_key} swiss_extra_rounds",
                minimum=0,
            )
            if not extra_cfg["min"] <= selected_extra <= extra_cfg["max"]:
                raise ValueError(
                    f"阶段 {stage_key} swiss_extra_rounds 须为 "
                    f"{extra_cfg['min']}..{extra_cfg['max']}"
                )
            stage["swiss_extra_rounds"] = selected_extra

    # A code-template snapshot may not carry hidden series fields on an
    # unadvertised stage.  This also closes the custom-stage smuggling path.
    for stage_key, stage in stage_by_key.items():
        if stage_key not in capabilities and _SERIES_STAGE_FIELDS.intersection(stage):
            raise ValueError(f"阶段 {stage_key} 未声明系列配置能力")
    gid = _game_spec(game_id).game_id
    return [validate_stage(stage, idx, gid) for idx, stage in enumerate(stages)]


def validate_template(
    tid: str, name: str, game_id: str, match_config: Any, stages: Any
) -> dict:
    """完整校验一个模板；返回规整后的 {id,name,game_id,match_config,stages}。"""
    validate_template_id(tid)
    if not isinstance(name, str) or not name.strip():
        raise ValueError("模板 name 不可为空")
    gid = _game_spec(game_id).game_id
    if not isinstance(stages, list) or len(stages) == 0:
        raise ValueError("stages 须为非空数组")
    norm_stages = [validate_stage(s, i, gid) for i, s in enumerate(stages)]
    norm_mc = validate_match_config(match_config, gid)
    return {
        "id": tid,
        "name": name.strip(),
        "game_id": gid,
        "match_config": norm_mc,
        "stages": norm_stages,
    }


__all__ = [
    "STAGE_TYPES",
    "SCORINGS",
    "validate_template_id",
    "validate_match_config",
    "validate_stage",
    "validate_template",
    "configure_games_per_pair",
    "SERIES_SCORING_AGGREGATE",
]
