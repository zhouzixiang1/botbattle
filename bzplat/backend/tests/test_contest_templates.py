"""赛制模板 CRUD / 校验 / 迁移 / preview / 前后端一致性测试。"""
from __future__ import annotations

import pytest

from bzplat.backend.contests.templates import (
    default_match_config,
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


# ── 迁移：内置模板导入 ──────────────────────────────────────────
def test_builtin_templates_seeded(store: Store):
    tpls = store.list_contest_templates()
    ids = {t["id"] for t in tpls}
    assert {"holdem_swiss_ko", "gomoku_group_drr_ko", "pencil_swiss_ko"}.issubset(ids)
    # 内置标记
    by_id = {t["id"]: t for t in tpls}
    assert by_id["holdem_swiss_ko"]["is_builtin"] in (1, True)
    # match_config 已钉死（规则参数固定），恒为空 dict
    assert by_id["holdem_swiss_ko"]["match_config"] == {}
    assert by_id["gomoku_group_drr_ko"]["match_config"] == {}
    # stages 已解析
    assert len(by_id["holdem_swiss_ko"]["stages"]) >= 1


def test_migration_idempotent(store: Store):
    n1 = len(store.list_contest_templates())
    s2 = Store(store.path)  # 再开一次
    n2 = len(s2.list_contest_templates())
    assert n1 == n2


# ── Store CRUD ─────────────────────────────────────────────────
def test_crud_create_get_update_delete(store: Store):
    t = store.upsert_contest_template(
        "mycup", name="我的杯", game_id="gomoku",
        match_config={}, stages=[{"key": "rr", "type": "round_robin"}],
    )
    assert t["id"] == "mycup" and t["is_builtin"] in (0, False)
    assert store.get_contest_template("mycup") is not None
    # 更新
    t2 = store.upsert_contest_template(
        "mycup", name="改名", game_id="gomoku", match_config={},
        stages=[{"key": "rr", "type": "round_robin"}, {"key": "ko", "type": "single_elimination"}],
    )
    assert t2["name"] == "改名" and len(t2["stages"]) == 2
    # 删自定义 OK；删内置拒绝
    assert store.delete_contest_template("mycup") is True
    assert store.delete_contest_template("holdem_swiss_ko") is False
    assert store.get_contest_template("mycup") is None


def test_list_filter_by_game(store: Store):
    gomoku = store.list_contest_templates(game_id="gomoku")
    assert all(t["game_id"] == "gomoku" for t in gomoku)
    assert any(t["id"] == "gomoku_group_drr_ko" for t in gomoku)


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


# ── resolve 读表（含 admin 覆盖）──────────────────────────────
def test_resolve_template_reads_table(store: Store):
    # admin 覆盖内置模板的 stages
    store.upsert_contest_template(
        "holdem_swiss_ko", name="改", game_id="holdem", match_config={},
        stages=[{"key": "x", "type": "round_robin"}], is_builtin=True,
    )
    tid, gid, stages, mc = resolve_template("holdem_swiss_ko", store=store)
    assert tid == "holdem_swiss_ko" and gid == "holdem"
    assert stages == [{"key": "x", "type": "round_robin"}]
    assert mc == {}


def test_stored_template_with_unknown_game_does_not_become_holdem(store: Store):
    with store._tx() as conn:
        conn.execute(
            "UPDATE contest_templates SET game_id='unknown' WHERE id='holdem_swiss_ko'"
        )
    with pytest.raises(ValueError, match="未知游戏"):
        resolve_template("holdem_swiss_ko", store=store)


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
            store.upsert_contest_template(
                f"bad_{bad or 'empty'}",
                name="bad",
                game_id=bad,
                match_config={},
                stages=[{"type": "round_robin"}],
            )
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


def test_resolve_stages_falls_back_to_defaults_without_store():
    # 不传 store 时回退内存 DEFAULT_TEMPLATES（供无 store 测试）
    tid, gid, stages = resolve_stages("gomoku_swiss_ko")
    assert gid == "gomoku" and len(stages) >= 1


# ── 前后端一致性：public 与 admin 同源 ─────────────────────────
def test_public_admin_templates_same_source(store: Store):
    # 模拟：admin 新增一个模板后，public 端应能看到
    store.upsert_contest_template(
        "shared1", name="共享杯", game_id="gomoku", match_config={},
        stages=[{"type": "round_robin"}], is_builtin=False,
    )
    # admin 列表与 public 列表（list_contest_templates）是同一方法
    admin_ids = {t["id"] for t in store.list_contest_templates()}
    assert "shared1" in admin_ids
