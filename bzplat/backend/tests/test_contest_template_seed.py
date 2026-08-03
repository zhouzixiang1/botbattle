"""赛制模板 seed 对账测试（PR-B）。

根因：db.py 的模板 seed 被 `if ntpl == 0:` 守卫，只在表空时跑一次。生产库
（PR#74 前创建）contest_templates 非空，导致之后新增的内置模板（预赛/决赛）
永远不会入库 → 前端 GET /api/contests/templates 读 DB 缺失 → UI 看不到。

修复：seed 之后无条件对账，INSERT 缺失的内置模板（绝不覆盖已有行）。
"""
from __future__ import annotations

import sqlite3

from bzplat.backend.contests.templates import DEFAULT_TEMPLATES
from bzplat.backend.store import Store
from bzplat.backend.store import db as dbmod


def test_empty_db_seeds_all_builtins(tmp_path):
    """空库 → 所有 DEFAULT_TEMPLATES 入库（含预赛/决赛）。"""
    s = Store(str(tmp_path / "t.db"))
    tpls = {t["id"]: t for t in s.list_contest_templates()}
    for tid in DEFAULT_TEMPLATES:
        assert tid in tpls, f"内置模板 {tid} 未入库"
    # holdem 至少 4 个（swiss_ko / rr / prelim_swiss / final_ranked）
    holdem_ids = [tid for tid, t in tpls.items() if t["game_id"] == "holdem"]
    assert {"holdem_swiss_ko", "holdem_rr", "holdem_prelim_swiss", "holdem_final_ranked"}.issubset(
        set(holdem_ids)
    )


def test_production_scenario_backfills_missing(tmp_path):
    """模拟生产库：表非空（已有旧模板）→ 对账应补齐缺失的内置模板。

    关键：验证「绝不覆盖已有行」——admin 覆盖过的自定义模板 is_builtin 值不变。
    """
    db_path = str(tmp_path / "prod.db")
    # 第一步：建一个「陈旧」库——先正常建库跑全量 seed，再人为删模板/覆盖，模拟
    # 「生产旧库缺 PR#74 后新增的预赛/决赛模板 + admin 覆盖过某内置模板」。
    conn = sqlite3.connect(db_path)
    # 先让 dbmod 建出完整 schema（会跑完整 seed，但我们要测的是「再次打开」对账）。
    conn.close()

    # 第一次打开：建库 + 全量 seed（空表场景）。
    Store(db_path).close()

    # 人为删掉部分内置模板 + 覆盖一个为自定义，模拟「生产旧库缺新增模板」。
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM contest_templates WHERE id IN ('holdem_prelim_swiss','holdem_final_ranked')")
    # 把 holdem_swiss_ko 改成 admin 覆盖（is_builtin=0）以验证对账不覆盖已有行。
    conn.execute("UPDATE contest_templates SET is_builtin=0, name='admin覆盖' WHERE id='holdem_swiss_ko'")
    conn.commit()
    swiss_before = conn.execute(
        "SELECT is_builtin, name FROM contest_templates WHERE id='holdem_swiss_ko'"
    ).fetchone()
    conn.close()

    # 第二次打开：触发 _migrate 对账 → 应补齐 prelim/final，且不动 swiss_ko。
    Store(db_path).close()

    conn = sqlite3.connect(db_path)
    ids = {r[0] for r in conn.execute("SELECT id FROM contest_templates").fetchall()}
    # 缺失的内置模板被补齐
    assert "holdem_prelim_swiss" in ids, "对账未补齐 holdem_prelim_swiss"
    assert "holdem_final_ranked" in ids, "对账未补齐 holdem_final_ranked"
    # 已有行不被覆盖（admin 覆盖的 is_builtin=0 + 自定义名保留）
    swiss_after = conn.execute(
        "SELECT is_builtin, name FROM contest_templates WHERE id='holdem_swiss_ko'"
    ).fetchone()
    assert swiss_after == swiss_before, f"对账覆盖了已有行: {swiss_before} -> {swiss_after}"
    # 补齐的是内置（is_builtin=1）
    for tid in ("holdem_prelim_swiss", "holdem_final_ranked"):
        b = conn.execute("SELECT is_builtin FROM contest_templates WHERE id=?", (tid,)).fetchone()
        assert b is not None and b[0] in (1, True)
    conn.close()


def test_reconcile_idempotent(tmp_path):
    """对账幂等：重复打开库不产生重复、不覆盖。"""
    db_path = str(tmp_path / "t.db")
    s1 = Store(db_path)
    before = sorted(t["id"] for t in s1.list_contest_templates())
    s1.close()
    # 再开两次
    Store(db_path).close()
    s3 = Store(db_path)
    after = sorted(t["id"] for t in s3.list_contest_templates())
    s3.close()
    assert before == after
