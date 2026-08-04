"""迁移脚本 migrate_bots_to_botzone.py 测试。

覆盖：迁移把 holdem bot 的 binary 换成 Botzone 协议样例；幂等（重跑跳过已迁移）；
风格分布用固定 seed 确定性可复现。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SAMPLES = ROOT / "samples"


def _load_migration_module():
    """动态加载迁移脚本（不在包内，故 importlib）。"""
    spec = importlib.util.spec_from_file_location(
        "migrate_bots_to_botzone",
        ROOT / "scripts" / "migrate_bots_to_botzone.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def tmp_store(tmp_path, monkeypatch):
    """建临时 Store + 几个 holdem bot（引用旧协议 binary 路径）。"""
    from bzplat.backend.store import Store
    db = tmp_path / "test.db"
    store = Store(str(db))
    # 建用户（经 create_user，满足约束）
    store.create_user("tester01", "t@e.com", "hash_xxx")
    uid = store._conn.execute("SELECT id FROM users WHERE username='tester01'").fetchone()["id"]
    # 建 6 个 holdem bot（引用旧 binary 路径）
    for i in range(6):
        store.create_bot(
            uid, f"bot{i}",
            binary_path="samples/old_holdem_bot",  # 旧协议
            format="elf", game_id="holdem",
        )
    # 切到 tmp 目录（bot_uploads 写到 tmp_path）
    monkeypatch.chdir(tmp_path)
    return store


def test_migration_replaces_bots_with_botzone_samples(tmp_store):
    """迁移后每个 bot 的 binary 是 Botzone 样例 ELF，版本号 +1。"""
    mod = _load_migration_module()
    stats = mod.migrate(tmp_store, game_id="holdem", seed=42)
    assert stats["total"] == 6
    assert stats["migrated"] == 6
    assert stats["skipped"] == 0
    # 每个 bot 的 binary_path 指向 bot_uploads/<id>/v1/bot.bin
    rows = tmp_store._conn.execute(
        "SELECT id, binary_path, current_version FROM bots WHERE game_id='holdem' ORDER BY id"
    ).fetchall()
    for r in rows:
        assert r["binary_path"] == f"bot_uploads/{r['id']}/v1/bot.bin"
        assert r["current_version"] == 1
        assert Path(r["binary_path"]).is_file()


def test_migration_idempotent(tmp_store):
    """重跑迁移：已迁移的跳过（不新建版本）。"""
    mod = _load_migration_module()
    mod.migrate(tmp_store, game_id="holdem", seed=42)
    # 第二次跑
    stats = mod.migrate(tmp_store, game_id="holdem", seed=42)
    assert stats["migrated"] == 0
    assert stats["skipped"] == 6
    # 版本号仍是 v1（没新建 v2）
    rows = tmp_store._conn.execute(
        "SELECT MAX(version) AS mv FROM bot_versions GROUP BY bot_id"
    ).fetchall()
    for r in rows:
        assert r["mv"] == 1


def test_migration_style_distribution_deterministic(tmp_store):
    """同 seed 两次迁移的风格分布一致；风格都在 8 种里。"""
    mod = _load_migration_module()
    s1 = mod.migrate(tmp_store, game_id="holdem", seed=42)
    # 第二个 store（独立）验证同 seed 同分布
    from bzplat.backend.store import Store
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        os.chdir(d)
        store2 = Store(os.path.join(d, "t.db"))
        store2.create_user("tester01", "t@e.com", "hash_xxx")
        uid = store2._conn.execute("SELECT id FROM users WHERE username='tester01'").fetchone()["id"]
        for i in range(6):
            store2.create_bot(uid, f"bot{i}", binary_path="x", format="elf", game_id="holdem")
        s2 = mod.migrate(store2, game_id="holdem", seed=42)
    assert s1["styles"] == s2["styles"]
    # 风格都在 holdem 8 种样例里
    valid = set(mod.STYLE_BINARIES["holdem"].keys())
    for style in s1["styles"]:
        assert style in valid


def test_migration_dry_run_no_writes(tmp_store, tmp_path):
    """dry-run：不写库、不复制文件。"""
    mod = _load_migration_module()
    stats = mod.migrate(tmp_store, game_id="holdem", dry_run=True, seed=42)
    assert stats["migrated"] == 0
    # bot_uploads 目录不应被创建
    assert not (tmp_path / "bot_uploads").exists()
    # bot 的 binary_path 仍是旧的
    rows = tmp_store._conn.execute("SELECT binary_path FROM bots WHERE game_id='holdem'").fetchall()
    for r in rows:
        assert r["binary_path"] == "samples/old_holdem_bot"


def test_migration_board_games(tmp_store, tmp_path):
    """迁移脚本支持 gomoku/pencil（STYLE_BINARIES 按游戏索引）。"""
    mod = _load_migration_module()
    assert "gomoku" in mod.STYLE_BINARIES
    assert "pencil" in mod.STYLE_BINARIES
    assert "holdem" in mod.STYLE_BINARIES
    # gomoku/pencil 各有样例
    assert len(mod.STYLE_BINARIES["gomoku"]) >= 1
    assert len(mod.STYLE_BINARIES["pencil"]) >= 1
    # 建一个 gomoku bot 迁移它
    tmp_store.create_bot(
        1, "gb1", binary_path="samples/old_gomoku", format="elf", game_id="gomoku",
    )
    stats = mod.migrate(tmp_store, game_id="gomoku", seed=42)
    assert stats["total"] == 1
    assert stats["migrated"] == 1
    # binary 指向 bot_uploads
    row = tmp_store._conn.execute(
        "SELECT binary_path FROM bots WHERE game_id='gomoku'"
    ).fetchone()
    assert "bot_uploads" in row["binary_path"]

