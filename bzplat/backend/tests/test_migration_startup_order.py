"""迁移原子性与启动时序回归。

覆盖三件事：
1. ``_execute_script_statements`` 逐句执行且留在调用方事务内（executescript
   的隐式 COMMIT 会让 Store.__init__ 的"失败整体回滚"名存实亡）；
2. journal CHECK 重建的检测精确匹配 owner_kind 白名单（裸子串 'build'
   会被注释/列名误触发），非 idle 时 fail closed 并附恢复 SQL；
3. ``match_mounts`` 的启动清场发生在 lifespan（dispatcher 单例锁之后），
   create_app 阶段不触碰任何目录。
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bzplat.backend.main import create_app
from bzplat.backend.store import Store
from bzplat.backend.store.db import _execute_script_statements


def test_execute_script_statements_stays_in_caller_transaction(
    tmp_path: Path,
) -> None:
    conn = sqlite3.connect(str(tmp_path / "tx.db"))
    conn.execute("BEGIN IMMEDIATE")
    _execute_script_statements(
        conn, "CREATE TABLE t1(a INTEGER);\nCREATE TABLE t2(b TEXT);\n"
    )
    conn.execute("ROLLBACK")
    left = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
        "AND name IN ('t1','t2')"
    ).fetchone()[0]
    assert left == 0, "逐句 DDL 必须可随调用方事务回滚（executescript 会隐式 COMMIT）"
    conn.close()


def test_execute_script_statements_rejects_incomplete_tail() -> None:
    conn = sqlite3.connect(":memory:")
    with pytest.raises(RuntimeError, match="不完整"):
        _execute_script_statements(conn, "CREATE TABLE ok(a);\nCREATE TABLE bad(")
    conn.close()


def test_journal_rebuild_detection_ignores_build_word_in_comment(
    tmp_path: Path,
) -> None:
    """owner_kind 白名单不含 build 时必须重建——即使 DDL 注释里出现 build。"""
    db_path = tmp_path / "legacy-journal.db"
    store = Store(str(db_path))
    store.close()
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE docker_launch_journal")
    conn.execute(
        """
        CREATE TABLE docker_launch_journal (
            singleton       INTEGER PRIMARY KEY CHECK (singleton=1),
            state           TEXT    NOT NULL DEFAULT 'idle' CHECK (
                state IN ('idle','creating','created')
            ),
            launch_token    TEXT,
            instance_key    TEXT,
            /* rebuild later: build owner arrives in a newer release */
            owner_kind      TEXT CHECK (
                owner_kind IS NULL OR owner_kind IN ('execution','preflight')
            ),
            job_public_id   TEXT,
            attempt_no      INTEGER CHECK (attempt_no IS NULL OR attempt_no>=1),
            slot            INTEGER CHECK (slot IS NULL OR slot>=0),
            container_name  TEXT,
            host_boot_id    TEXT,
            updated_at      TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO docker_launch_journal(singleton,state,updated_at) "
        "VALUES(1,'idle','2020-01-01')"
    )
    conn.commit()
    conn.close()

    reopened = Store(str(db_path))
    with reopened._tx() as c:
        journal_sql = str(
            c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' "
                "AND name='docker_launch_journal'"
            ).fetchone()[0]
        )
        row = c.execute(
            "SELECT state FROM docker_launch_journal WHERE singleton=1"
        ).fetchone()
    reopened.close()
    assert "'build'" in journal_sql, "旧白名单（注释含 build 字样）必须被识别并重建"
    assert row[0] == "idle"


def test_journal_rebuild_non_idle_fails_closed_with_recovery_sql(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "stuck-journal.db"
    store = Store(str(db_path))
    store.close()
    conn = sqlite3.connect(str(db_path))
    conn.execute("DROP TABLE docker_launch_journal")
    conn.execute(
        """
        CREATE TABLE docker_launch_journal (
            singleton       INTEGER PRIMARY KEY CHECK (singleton=1),
            state           TEXT    NOT NULL DEFAULT 'idle',
            launch_token    TEXT,
            instance_key    TEXT,
            owner_kind      TEXT,
            job_public_id   TEXT,
            attempt_no      INTEGER,
            slot            INTEGER,
            container_name  TEXT,
            host_boot_id    TEXT,
            updated_at      TEXT
        )
        """
    )
    conn.execute(
        "INSERT INTO docker_launch_journal(singleton,state,updated_at) "
        "VALUES(1,'creating','2020-01-01')"
    )
    conn.commit()
    conn.close()

    with pytest.raises(RuntimeError) as exc:
        Store(str(db_path))
    message = str(exc.value)
    assert "非 idle" in message
    assert "UPDATE docker_launch_journal" in message, "报错必须附人工恢复 SQL"
    assert "doc/RUNTIME.md" in message


def test_match_mounts_wipe_runs_in_lifespan_not_create_app(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "wipe.db"
    app = create_app(db_path=str(db_path))
    mounts = db_path.parent / "match_mounts" / "legacy-match"
    mounts.mkdir(parents=True)
    (mounts / "seat0").mkdir()
    (mounts / "seat0" / "weights.bin").write_bytes(b"x" * 8)

    # create_app 阶段（dispatcher 单例锁获取之前）不得清任何目录：
    # 误启的第二实例会先破坏存活对局的云盘快照、之后才因 flock 退出。
    assert (mounts / "seat0" / "weights.bin").is_file()

    with TestClient(app) as client:
        assert client.get("/api/site/info").status_code == 200
        # lifespan 内、dispatcher 启动对账之后才回收崩溃残留。
        assert not mounts.exists()
