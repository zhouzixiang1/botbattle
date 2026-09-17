"""审计跟进项回归：文件名双向控制符、bot 上传暂存启动清理、前后端
上传限额常量对齐守护。"""
from __future__ import annotations

import time
from pathlib import Path

from bzplat.backend.bots.manager import BotManager
from bzplat.backend.store import Store
from bzplat.backend.user_storage import (
    UserStorageError,
    validate_storage_name,
)


def test_storage_name_rejects_bidi_and_invisible_format_chars():
    # 合法名不受影响（含中文、空格、点）。
    assert validate_storage_name("权重.bin") == "权重.bin"
    assert validate_storage_name("model final.pt") == "model final.pt"
    # 双向/隔离控制符：视觉伪装面。
    for raw in (
        "evil\u202etxt.exe",  # RLO 反转显示
        "a\u200fb",           # RLM
        "a\u2066b\u2069",     # LRI...PDI 隔离
        "a\u202eb",
    ):
        try:
            validate_storage_name(raw)
        except UserStorageError as exc:
            assert exc.code == "invalid_name"
        else:
            raise AssertionError(f"双向控制符必须被拒绝: {raw!r}")


def test_purge_stale_staging_covers_incoming_and_version_dirs(tmp_path):
    store = Store(str(tmp_path / "purge.db"))
    mgr = BotManager(store, upload_root=tmp_path / "uploads")
    root = tmp_path / "uploads"
    root.mkdir(parents=True, exist_ok=True)

    old = time.time() - 7200.0
    incoming = root / ".incoming-crash"
    incoming.mkdir()
    (incoming / "blob").write_bytes(b"x")
    bot_dir = root / "42"
    stale_version = bot_dir / ".v3-abcd"
    stale_version.mkdir(parents=True)
    (stale_version / "bot.bin").write_bytes(b"x")
    fresh_version = bot_dir / ".v4-efgh"
    fresh_version.mkdir(parents=True)
    keep_incoming = root / ".incoming-live"
    keep_incoming.mkdir()

    import os

    os.utime(incoming, (old, old))
    os.utime(stale_version, (old, old))
    # 正式版本目录与新鲜暂存不得被碰。
    keep_version = bot_dir / "v3"
    keep_version.mkdir(parents=True)

    mgr._purge_stale_staging(min_age_seconds=3600.0)

    assert not incoming.exists()
    assert not stale_version.exists()
    assert keep_incoming.is_dir(), "宽限期内暂存必须保留"
    assert fresh_version.is_dir()
    assert keep_version.is_dir(), "正式版本目录绝不能被清理"
    store.close()


def test_frontend_upload_limits_match_backend_constants():
    """跨语言双份硬编码的对齐守护：前端常量漂移时在此红。"""
    from bzplat.backend.runtime.limits import (
        MAX_BOT_UPLOAD_BYTES,
        SOURCE_UPLOAD_MAX_BYTES,
    )

    ts = (
        Path(__file__).resolve().parents[2]
        / "frontend"
        / "src"
        / "components"
        / "bot-upload-progress.tsx"
    ).read_text(encoding="utf-8")

    def _const(name: str) -> int:
        import re

        match = re.search(
            rf"export const {name} = (\d+) \* 1024 \* 1024", ts
        )
        assert match, f"前端常量 {name} 缺失或不再是 MiB 表达式"
        return int(match.group(1))

    assert _const("BOT_UPLOAD_MAX_BYTES") * 1024 * 1024 == MAX_BOT_UPLOAD_BYTES
    assert (
        _const("SOURCE_UPLOAD_MAX_BYTES") * 1024 * 1024
        == SOURCE_UPLOAD_MAX_BYTES
    )
