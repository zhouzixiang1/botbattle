"""代码内置赛制模板、校验与解析测试。"""
from __future__ import annotations

import json

import pytest

from bzplat.backend.contests.templates import (
    default_match_config,
    get_template,
    list_templates,
    points_for_result,
    resolve_template,
    resolve_stages,
)
from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.validation import (
    validate_match_config,
    validate_stage,
    validate_template,
    validate_template_id,
)
from bzplat.backend.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


def _insert_legacy_template(
    store: Store,
    tid: str,
    *,
    name: str,
    game_id: str,
    stages: list[dict],
    is_builtin: bool = False,
) -> None:
    with store._tx() as conn:
        conn.execute(
            "INSERT INTO contest_templates"
            "(id,name,game_id,match_config,stages_json,is_builtin,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                tid,
                name,
                game_id,
                "{}",
                json.dumps(stages),
                1 if is_builtin else 0,
                "2026-08-09T00:00:00",
            ),
        )


# ── 唯一来源：游戏注册表代码 ────────────────────────────────────
def test_builtin_templates_come_from_code_registry(store: Store):
    tpls = list_templates()
    ids = {t["id"] for t in tpls}
    assert {"holdem_swiss_ko", "board_rr", "pencil_swiss_ko"}.issubset(ids)
    assert "gomoku_group_drr_ko" not in ids
    assert get_template("gomoku_group_drr_ko")["creation_enabled"] is False
    assert store.list_contest_templates() == []


def test_code_template_list_filters_by_game():
    gomoku = list_templates(game_id="gomoku")
    assert all(t["game_id"] == "gomoku" for t in gomoku)
    assert {t["id"] for t in gomoku} == {"board_rr"}


# ── 校验 ───────────────────────────────────────────────────────
def test_validate_template_ok():
    norm = validate_template(
        "cup1", "杯赛", "pencil",
        {}, [{"key": "g", "type": "group_double_round_robin", "group_count": 2}],
    )
    assert norm["game_id"] == "pencil"
    assert norm["match_config"] == {}
    assert norm["stages"][0]["group_count"] == 2


def test_validate_rejects_bad_type():
    with pytest.raises(ValueError, match="type"):
        validate_stage({"type": "nope"}, 0, "holdem")


def test_validate_rejects_bad_scoring():
    with pytest.raises(ValueError, match="scoring"):
        validate_stage({"type": "round_robin", "scoring": "xxx"}, 0, "holdem")


def test_validate_rejects_group_count_on_non_group():
    with pytest.raises(ValueError, match="group_count"):
        validate_stage({"type": "swiss", "group_count": 4}, 0, "holdem")


@pytest.mark.parametrize("field", ["max_hand", "maxHand", "roundz", "board_size"])
def test_validate_stage_rejects_every_unknown_key(field):
    with pytest.raises(ValueError, match=field):
        validate_stage({"type": "round_robin", field: 1}, 0, "holdem")


@pytest.mark.parametrize(
    ("stage", "message"),
    [
        ({"type": None}, "type"),
        ({"type": []}, "type"),
        ({"type": "round_robin", "scoring": None}, "scoring"),
        ({"type": "round_robin", "scoring": []}, "scoring"),
        ({"type": "swiss", "rounds": True}, "rounds"),
        ({"type": "round_robin", "duplicate": 1}, "duplicate"),
        ({"type": "round_robin", "duplicate": None}, "duplicate"),
        ({"type": "round_robin", "allow_large_round_robin": "yes"}, "allow_large"),
        ({"type": "round_robin", "round_stagger_minutes": 1.5}, "round_stagger"),
    ],
)
def test_validate_stage_rejects_wrong_typed_values(stage, message):
    with pytest.raises(ValueError, match=message):
        validate_stage(stage, 0, "holdem")


def test_validate_stage_preserves_supported_specialized_fields():
    duplicate = validate_stage(
        {"type": "round_robin", "duplicate": True, "allow_large_round_robin": True},
        0,
        "holdem",
    )
    assert duplicate["duplicate"] is True
    assert duplicate["allow_large_round_robin"] is True
    ranked = validate_stage(
        {
            "type": "double_round_robin",
            "ranking_mode": "replace_top",
            "ranking_scope": 8,
        },
        0,
        "holdem",
    )
    assert ranked["ranking_mode"] == "replace_top"
    assert ranked["ranking_scope"] == 8
    with pytest.raises(ValueError, match="不支持 duplicate"):
        validate_stage({"type": "round_robin", "duplicate": True}, 0, "gomoku")


def test_validate_match_config_holdem_rejects_old_fields():
    assert validate_match_config({}, "holdem") == {}
    for config in ({"hands": 1}, {"num_hands": 70}, {"starting_stack": 20_000}):
        with pytest.raises(ValueError, match="游戏规则已固定"):
            validate_match_config(config, "holdem")


def test_validate_match_config_pencil_rejects_old_fields():
    assert validate_match_config({}, "pencil") == {}
    for config in ({"n_dots": 3}, {"time_limit": 900}):
        with pytest.raises(ValueError, match="游戏规则已固定"):
            validate_match_config(config, "pencil")


def test_validate_template_id_format():
    with pytest.raises(ValueError):
        validate_template_id("1bad")  # 数字开头
    with pytest.raises(ValueError):
        validate_template_id("UP")  # 大写
    validate_template_id("my_cup1")


def test_validate_rejects_bad_game_id():
    with pytest.raises(ValueError, match="game_id"):
        validate_template("goodid", "n", "unknown", {}, [{"type": "round_robin"}])


@pytest.mark.parametrize("game_id", [None, "", "unknown"])
def test_direct_config_lookup_does_not_guess_holdem(game_id):
    with pytest.raises((ValueError, KeyError)):
        default_match_config(game_id)  # type: ignore[arg-type]


def test_custom_empty_stages_does_not_resolve_a_default_template():
    with pytest.raises(ValueError, match="非空"):
        resolve_stages(None, [], game_id="holdem")


def test_unknown_scoring_does_not_fall_back_to_poker():
    with pytest.raises(ValueError, match="未知计分规则"):
        points_for_result("typo", None, 0)


# ── resolve 始终读取代码，不接受历史表覆盖 ─────────────────────
def test_resolve_template_ignores_legacy_table_override(store: Store):
    _insert_legacy_template(
        store,
        "holdem_swiss_ko",
        name="改",
        game_id="holdem",
        stages=[{"key": "x", "type": "round_robin"}],
        is_builtin=True,
    )
    tid, gid, stages, mc = resolve_template("holdem_swiss_ko")
    assert tid == "holdem_swiss_ko" and gid == "holdem"
    assert stages == get_template("holdem_swiss_ko")["stages"]
    assert stages != [{"key": "x", "type": "round_robin"}]
    assert mc == {}


def test_malformed_legacy_template_row_cannot_poison_code_template(store: Store):
    _insert_legacy_template(
        store,
        "holdem_swiss_ko",
        name="旧记录",
        game_id="holdem",
        stages=[{"key": "x", "type": "round_robin"}],
        is_builtin=True,
    )
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_templates SET game_id='unknown' WHERE id='holdem_swiss_ko'"
        )
    tid, gid, _stages, _mc = resolve_template("holdem_swiss_ko")
    assert tid == "holdem_swiss_ko"
    assert gid == "holdem"


def test_store_creation_and_rating_reject_invalid_game_ids(store: Store):
    user_id = store.create_user("strictgid", "strictgid@example.com", "x")["id"]
    for bad in ("", "unknown"):
        with pytest.raises(ValueError, match="game_id"):
            store.create_bot(
                user_id,
                f"bad_{bad or 'empty'}",
                binary_path="/tmp/bot",
                format="elf",
                game_id=bad,
            )
        with pytest.raises(ValueError, match="game_id"):
            store.create_contest("bad", user_id, game_id=bad)
        with pytest.raises(ValueError, match="game_id"):
            store.create_match("bad", 1, 2, game_id=bad)

    bot = store.create_bot(
        user_id,
        "strict_bot",
        binary_path="/tmp/bot",
        format="elf",
        game_id="gomoku",
    )
    with store._tx() as conn:
        conn.execute("UPDATE bots SET game_id='unknown' WHERE id=?", (bot["id"],))
    with pytest.raises(ValueError, match="game_id"):
        store.ensure_rating(bot["id"])


def test_create_rejects_template_game_mismatch(store: Store):
    """请求 game_id 不能覆盖模板所属游戏，避免跨游戏 stages 混搭。"""
    manager = ContestManager(store, object())  # create 不消费 orchestrator

    with pytest.raises(ValueError, match=r"holdem.*不能用于游戏 gomoku"):
        manager.create(
            1,
            "错误混搭",
            template_id="holdem_swiss_ko",
            game_id="gomoku",
        )

    # 显式 stages 也不能绕过具名模板的所属游戏约束。
    with pytest.raises(ValueError, match=r"holdem.*不能用于游戏 gomoku"):
        manager.create(
            1,
            "自定义阶段绕过",
            template_id="holdem_swiss_ko",
            game_id="gomoku",
            stages=[{"key": "rr", "type": "round_robin"}],
        )

    assert store.list_contests() == []


def test_disabled_gomoku_template_cannot_be_used_for_new_contest(store: Store):
    with pytest.raises(ValueError, match="已停用新建"):
        resolve_stages("gomoku_swiss_ko")

    manager = ContestManager(store, object())  # create 不消费 orchestrator
    with pytest.raises(ValueError, match="已停用新建"):
        manager.create(1, "停用模板", template_id="gomoku_swiss_ko")
    with pytest.raises(ValueError, match="已停用新建"):
        manager.create(
            1,
            "自定义阶段也不能伪装停用模板",
            template_id="gomoku_swiss_ko",
            game_id="gomoku",
            stages=[{"key": "rr", "type": "round_robin"}],
        )


# ── 历史表内容不进入公开/运行模板集合 ───────────────────────────
def test_custom_legacy_row_is_not_a_runtime_template(store: Store):
    _insert_legacy_template(
        store,
        "shared1",
        name="共享杯",
        game_id="gomoku",
        stages=[{"type": "round_robin"}],
    )
    assert store.get_contest_template("shared1") is not None
    assert get_template("shared1") is None
    assert "shared1" not in {t["id"] for t in list_templates()}
