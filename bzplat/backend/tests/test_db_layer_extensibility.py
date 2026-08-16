"""数据层第 4 游戏扩展性测试（解耦深度整改 PR-1）。

验证"新增第 4 款游戏 DB 零改动"承诺的真实性：
1. 当注册表多出一个游戏 id（如 reversi）时，_migrate 自动建 matches_<new> 表 + 索引
2. 启动断言捕获"注册了但表没建"的 drift
3. 跨游戏 UNION ALL 查询的参数绑定数 = 子查询数（不得硬编码 * 3）
4. 第 4 游戏的 create/get 经 matches_index 正确路由

背景：上一轮把硬编码 3-game 元组改成 _all_game_ids() 循环，但漏了两个孪生耦合点——
① db.py 的 `params * 3`（UNION 子查询循环化了，参数倍数没同步）；
② schema.py 的 matches_<game> DDL 是字面 CREATE（新游戏表建不出来）。
本测试用 monkeypatch 模拟第 4 个注册游戏，验证这两处修复生效。
"""
from __future__ import annotations

import pytest

from bzplat.backend.store import Store
from bzplat.backend.store import db as dbmod


# ── monkeypatch 辅助：把假游戏 reversi 注入注册表 ────────────────
@pytest.fixture
def fake_fourth_game(monkeypatch):
    """让 _all_game_ids() 返回 4 个游戏（含 reversi），并让 _matches_table 认 reversi。

    用 monkeypatch 改 db 模块内的两个函数，模拟"games/__init__.py 注册了 reversi"
    的效果——Store 建库时就会经 _migrate 自动建 matches_reversi。
    """
    real_ids = frozenset({"holdem", "gomoku", "pencil", "reversi"})
    real_contract = dbmod.game_rule_contract

    def _fake_all_ids():
        return real_ids

    def _fake_matches_table(game_id):
        gid = (game_id or "holdem").strip().lower()
        if gid not in real_ids:
            raise ValueError(f"未知 game_id: {game_id!r}")
        return f"matches_{gid}"

    def _fake_game_rule_contract(game_id, *, legacy=False):
        if game_id == "reversi":
            return {
                "ruleset_version": "reversi_test_v1",
                "protocol_version": "reversi_action_test_v1",
                "rating_pool_id": "reversi_rating_test_v1",
            }
        return real_contract(game_id, legacy=legacy)

    monkeypatch.setattr(dbmod, "_all_game_ids", _fake_all_ids)
    monkeypatch.setattr(dbmod, "_matches_table", _fake_matches_table)
    monkeypatch.setattr(dbmod, "game_rule_contract", _fake_game_rule_contract)
    return real_ids


# ── 1. 自动建表 + 索引 ─────────────────────────────────────────
def test_migrate_creates_table_for_fourth_game(tmp_path, fake_fourth_game):
    """注册第 4 游戏后，Store 初始化应自动建 matches_reversi 表。"""
    s = Store(str(tmp_path / "four.db"))
    with s._tx() as c:
        tables = {
            r[0]
            for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        # 第 4 张表被自动建出来
        assert "matches_reversi" in tables, (
            "注册了 reversi 但 matches_reversi 表未被 _migrate 自动建——"
            "DB 层未真正随注册表扩展，新增游戏会崩 no such table"
        )
        # 三张原表仍在
        assert {"matches_holdem", "matches_gomoku", "matches_pencil"} <= tables
    s.close()


def test_migrate_creates_indexes_for_fourth_game(tmp_path, fake_fourth_game):
    """第 4 游戏表应有与三原表一致的 6 条索引（bot_a/bot_b/owner/contest/status/time）。"""
    s = Store(str(tmp_path / "four_idx.db"))
    with s._tx() as c:
        idxs = {
            r[0]
            for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='matches_reversi'"
            )
        }
    s.close()
    expected = {
        "idx_mreversi_bot_a_id",
        "idx_mreversi_bot_b_id",
        "idx_mreversi_owner_id",
        "idx_mreversi_contest_id",
        "idx_mreversi_status",
        "idx_mreversi_created_at",
    }
    assert expected <= idxs, f"matches_reversi 缺索引：缺 {expected - idxs}"


# ── 2. 启动断言：注册了但表没建应崩 ─────────────────────────────
def test_startup_assertion_catches_missing_table(tmp_path, monkeypatch):
    """若 _all_game_ids 含某游戏但 _migrate 没建表（模拟 drift），Store 初始化应断言失败。"""
    # 让 _all_game_ids 返回 reversi，但 _migrate 对 reversi 不建表
    real_ids = frozenset({"holdem", "gomoku", "pencil", "reversi"})
    monkeypatch.setattr(dbmod, "_all_game_ids", lambda: real_ids)
    monkeypatch.setattr(dbmod, "_matches_table",
                        lambda g: f"matches_{(g or 'holdem').strip().lower()}")
    # 用 str 子类劫持 .format：reversi 返回空操作语句 → matches_reversi 不会被建出来
    orig_create = dbmod._CREATE_MATCHES_TABLE_SQL

    class _FakeCreateStr(str):
        def format(self, **kw):
            if kw.get("suffix") == "reversi":
                return "SELECT 1"  # 不是建表语句 → matches_reversi 不会被建
            return orig_create.format(**kw)

    monkeypatch.setattr(dbmod, "_CREATE_MATCHES_TABLE_SQL", _FakeCreateStr(orig_create))

    with pytest.raises(AssertionError, match="缺物理表"):
        Store(str(tmp_path / "drift.db"))


# ── 3. UNION ALL 参数绑定数 = 子查询数（不得 * 3 硬编码）──────────
def test_union_query_param_count_matches_subquery_count(tmp_path, fake_fourth_game):
    """4 游戏时 UNION ALL 子查询数=4，参数须复制 4 份（不是 3）。

    若仍硬编码 * 3，sql 模板有 4 个子查询占位但只传 3*params → Incorrect number of bindings。
    本测试通过实际执行 search_matches（跨游戏 UNION，带 4 个 LIKE 参数）验证不崩。
    """
    s = Store(str(tmp_path / "union.db"))
    # 无需插数据——空库也能跑 SELECT，参数数不对会直接 ProgrammingError
    # search_matches 跨游戏分支：params=[q]*5，UNION 4 子查询 → 需 5*4=20 + [lim]
    results = s.search_matches("anything", limit=5)
    assert results == [], "空库搜索应返回空列表（重点是没崩 Incorrect number of bindings）"
    s.close()


def test_list_matches_union_param_count(tmp_path, fake_fourth_game):
    """list_matches（无 game_id）跨游戏 UNION，参数数须匹配 4 子查询。"""
    s = Store(str(tmp_path / "union2.db"))
    # list_matches 带 status 过滤 → UNION 每子查询一份 status 参数
    results = s.list_matches(limit=5, offset=0, status="completed")
    assert results == [], "空库应返回空（重点是没崩 Incorrect number of bindings）"
    s.close()


# ── 4. 第 4 游戏 create/get 经 matches_index 路由 ───────────────
def test_fourth_game_match_routes_correctly(tmp_path, fake_fourth_game):
    """第 4 游戏的对局应写入 matches_reversi，matches_index 指向它，get_match 能取回。"""
    s = Store(str(tmp_path / "route.db"))
    # 先建两个 bot + 一个用户（create_match 需要 owner + bots）
    uid = s.create_user("revu1", "revu1@x.com", "hashed", role="user")["id"]
    ba = s.create_bot(uid, "revbot_a", game_id="reversi")["id"]
    bb = s.create_bot(uid, "revbot_b", game_id="reversi")["id"]

    mid = s.create_match(
        "20260802-reversi-test-0001",
        ba,
        bb,
        game_id="reversi",
        owner_id=uid,
        match_type="challenge",
    )["id"]
    # 写入了正确的物理表
    with s._tx() as c:
        row = c.execute("SELECT game_id FROM matches_reversi WHERE id=?", (mid,)).fetchone()
        assert row is not None, "对局没写入 matches_reversi"
        assert row["game_id"] == "reversi"
        idx = c.execute("SELECT game_id FROM matches_index WHERE id=?", (mid,)).fetchone()
        assert idx["game_id"] == "reversi", "matches_index 定位指向 reversi"
    # get_match 经 index 路由取回
    got = s.get_match(mid)
    assert got is not None and got["game_id"] == "reversi"
    s.close()
