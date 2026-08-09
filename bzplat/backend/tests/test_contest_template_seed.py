"""历史 contest_templates 表不再参与初始化或运行。"""
from __future__ import annotations

import json
import sqlite3

from bzplat.backend.contests.templates import get_template, list_templates
from bzplat.backend.store import Store


def test_fresh_database_does_not_copy_code_templates_into_history_table(tmp_path):
    store = Store(str(tmp_path / "fresh.db"))

    assert store.list_contest_templates() == []
    assert {t["id"] for t in list_templates()}


def test_reopen_preserves_history_rows_without_seeding_or_reconciling(tmp_path):
    db_path = str(tmp_path / "legacy.db")
    store = Store(db_path)
    with store._tx() as conn:
        conn.execute(
            "INSERT INTO contest_templates"
            "(id,name,game_id,match_config,stages_json,is_builtin,updated_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                "legacy_only",
                "历史模板",
                "holdem",
                "{}",
                json.dumps([{"type": "round_robin"}]),
                0,
                "2026-08-09T00:00:00",
            ),
        )
    store.close()

    Store(db_path).close()

    conn = sqlite3.connect(db_path)
    ids = {row[0] for row in conn.execute("SELECT id FROM contest_templates")}
    conn.close()
    assert ids == {"legacy_only"}
    assert get_template("legacy_only") is None


def test_legacy_platform_blob_is_not_imported(tmp_path):
    db_path = str(tmp_path / "blob.db")
    store = Store(db_path)
    store.set_setting(
        "contest_templates",
        json.dumps(
            [
                {
                    "id": "blob_only",
                    "name": "旧 KV 模板",
                    "game_id": "holdem",
                    "stages": [{"type": "round_robin"}],
                }
            ]
        ),
    )
    store.close()

    reopened = Store(db_path)
    assert reopened.list_contest_templates() == []
    assert get_template("blob_only") is None
