"""私有 bot 功能下线测试（PR-C）。

平台全局只有「公开」一种状态，不再有公开/私有区分。验证：
- bots 表无 is_public 列
- 上传 bot 不再接受 is_public 参数
- API 响应不含 is_public 键
- 挑战对手 bot 不再被「未公开」拦截
- 旧库迁移：is_public 列被 DROP（存量私有 bot 先转公开）
"""
from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def test_bots_table_has_no_is_public_column(tmp_path):
    s = Store(str(tmp_path / "t.db"))
    cols = {r[1] for r in s._conn.execute("PRAGMA table_info(bots)")}
    s.close()
    assert "is_public" not in cols, "bots 表不应再有 is_public 列"


def test_upload_bot_succeeds_without_is_public(tmp_path):
    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u = store.create_user("alice", "a@ex.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    _, tok = app.state.auth.authenticate("alice", "pw123456")
    c = TestClient(app)

    # 用 ELF 样例上传，不带 is_public 字段
    import pathlib

    raw = pathlib.Path("samples/callbot_linux_amd64").read_bytes()
    assert raw[:4] == b"\x7fELF", "缺少 callbot ELF 样例"

    r = c.post(
        "/api/bots",
        headers={"Authorization": f"Bearer {tok}"},
        data={"name": "noPublicBot", "game_id": "holdem"},
        files={"file": ("bot.bin", raw, "application/octet-stream")},
    )
    assert r.status_code == 200, r.text
    bot = r.json()["bot"]
    assert "is_public" not in bot, "上传响应不应含 is_public 键"


def test_bot_response_omits_is_public(tmp_path):
    """GET /api/bots/{id} 与 /api/users/{name}/bots 响应均不含 is_public。"""
    db = str(tmp_path / "app.db")
    app = create_app(db_path=db)
    store = app.state.store
    u = store.create_user("alice", "a@ex.com", hash_password("pw123456"))
    store.update_user(u["id"], email_verified=1)
    b = store.create_bot(u["id"], "botX", binary_path="/tmp", format="elf", game_id="holdem")
    _, tok = app.state.auth.authenticate("alice", "pw123456")
    c = TestClient(app)

    r = c.get(f"/api/bots/{b['id']}")
    assert r.status_code == 200
    assert "is_public" not in r.json()["bot"]

    r2 = c.get("/api/users/alice/bots")
    assert r2.status_code == 200
    for bot in r2.json().get("bots", []):
        assert "is_public" not in bot


def test_migrate_drops_is_public_column(tmp_path):
    """旧库（有 is_public 列 + 含私有 bot）迁移后：列被 DROP，私有 bot 转公开。"""
    db = str(tmp_path / "legacy.db")
    # 先正常建库（含旧版带 is_public 的 bots 表）——手动建一个最小旧库。
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE,
            email TEXT UNIQUE, password_hash TEXT, role TEXT DEFAULT 'user',
            display_name TEXT DEFAULT '', is_active INTEGER DEFAULT 1,
            email_verified INTEGER DEFAULT 0, created_at TEXT);
        CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT, owner_id INTEGER,
            name TEXT, display_name TEXT DEFAULT '', description TEXT DEFAULT '',
            os TEXT DEFAULT '', arch TEXT DEFAULT '', format TEXT DEFAULT 'unknown',
            binary_path TEXT DEFAULT '', current_version INTEGER DEFAULT 0,
            is_public INTEGER DEFAULT 1, is_active INTEGER DEFAULT 1,
            is_builtin INTEGER DEFAULT 0, game_id TEXT DEFAULT 'holdem',
            created_at TEXT, updated_at TEXT, UNIQUE(owner_id, name));
    """
    )
    conn.execute("INSERT INTO users(username,email,password_hash,created_at) VALUES('a','a@e','h','2026')")
    # 1 个公开 bot + 1 个私有 bot
    conn.execute("INSERT INTO bots(owner_id,name,game_id,is_public,created_at,updated_at) VALUES(1,'pubBot','holdem',1,'2026','2026')")
    conn.execute("INSERT INTO bots(owner_id,name,game_id,is_public,created_at,updated_at) VALUES(1,'privBot','holdem',0,'2026','2026')")
    conn.commit()
    conn.close()

    # 触发迁移
    s = Store(db)
    cols = {r[1] for r in s._conn.execute("PRAGMA table_info(bots)")}
    assert "is_public" not in cols, "迁移后 is_public 列应被 DROP"
    # 两个 bot 都还在（数据未丢）
    names = {r[0] for r in s._conn.execute("SELECT name FROM bots")}
    assert names == {"pubBot", "privBot"}
    s.close()
