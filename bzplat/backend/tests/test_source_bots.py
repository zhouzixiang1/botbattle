"""源码 Bot：zip 校验、构建配方、版本冻结列与 python 端到端冒烟。"""
from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from bzplat.backend.bots.source_build import (
    SourceBuildError,
    build_recipe,
    inspect_source_zip,
)
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app


def _make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return path


def test_source_zip_guards(tmp_path):
    good = _make_zip(
        tmp_path / "g.zip", {"main.cpp": "int main(){}", "util/h": "#pragma once"}
    )
    spec = inspect_source_zip(good, language="cpp")
    assert (spec.entry, spec.files) == ("main.cpp", 2)
    assert build_recipe(spec)["kind"] == "compile"
    assert "_BOTZONE_ONLINE=1" in build_recipe(spec)["command"]

    bad = _make_zip(tmp_path / "t.zip", {"../x.cpp": "1"})
    with pytest.raises(SourceBuildError) as e:
        inspect_source_zip(bad, language="cpp")
    assert e.value.code == "invalid_source_zip"

    noentry = _make_zip(tmp_path / "n.zip", {"other.py": "1"})
    with pytest.raises(SourceBuildError) as e:
        inspect_source_zip(noentry, language="python")
    assert e.value.code == "invalid_entry"

    # 非法语言
    with pytest.raises(SourceBuildError) as e:
        inspect_source_zip(good, language="rust")
    assert e.value.code == "unsupported_source_language"

    # 显式入口存在性
    with pytest.raises(SourceBuildError) as e:
        inspect_source_zip(good, language="cpp", declared_entry="missing.cpp")
    assert e.value.code == "invalid_entry"

    # python 配方无容器命令
    pyspec = inspect_source_zip(
        _make_zip(tmp_path / "p.zip", {"__main__.py": "print(1)"}),
        language="python",
    )
    assert build_recipe(pyspec)["kind"] == "python-bundle"


def test_python_source_upload_and_version_columns(tmp_path):
    import shutil

    app = create_app(db_path=str(tmp_path / "sb.db"))
    store = app.state.store
    user = store.create_user("sbu", "sbu@e.com", hash_password("pw123456"))
    store.update_user(user["id"], email_verified=1)
    zp = _make_zip(
        tmp_path / "src.zip",
        {
            "__main__.py": (
                "import json,sys\n"
                "j=json.loads(sys.stdin.readline())\n"
                'print(json.dumps({"response":0}))\n'
                "sys.stdout.flush()\n"
            )
        },
    )
    staged = app.state.bot_manager.new_staged_upload()
    shutil.copyfile(zp, staged.path)
    staged.size = zp.stat().st_size

    bot = app.state.bot_manager.create_from_upload(
        user["id"],
        "pysrc",
        staged,
        game_id="holdem",
        runtime_mode="traditional",
        source_format="python",
        # 全量套件清 BZ_BOT_LOCAL 后 Docker 预检会拉起 python 运行镜像并
        # 挂载 tmp src；此处只验证版本列/launcher/源码持久化，跳过预检。
        binary_runner=None,
    )
    assert bot["current_version"] == 1
    version = store.get_latest_bot_version(bot["id"])
    assert version["source_format"] == "python"
    assert version["runtime_image"]  # python 必须声明运行镜像
    assert version["source_path"].endswith("source.zip")
    recipe = json.loads(version["build_recipe_json"]) if (json := __import__("json")) else {}
    assert recipe["kind"] == "python-bundle"
    launcher = Path(version["binary_path"])
    assert launcher.is_file() and launcher.read_text().startswith("#!/bin/sh")
    assert (launcher.parent / "src" / "__main__.py").is_file()
    assert (Path(version["source_path"])).is_file()
    store.close()


def test_legacy_bot_versions_gain_source_columns(tmp_path):
    app = create_app(db_path=str(tmp_path / "legacy.db"))
    store = app.state.store
    user = store.create_user("lgu", "lgu@e.com", hash_password("pw123456"))
    bot = store.create_bot(
        user["id"], "lgbot", binary_path="/tmp/x", format="elf", game_id="holdem"
    )
    store.add_bot_version(bot["id"], binary_path="/tmp/x", checksum="a" * 64, size_bytes=1)
    version = store.get_latest_bot_version(bot["id"])
    assert version["source_format"] == "elf"
    assert version["runtime_image"] == ""
    assert version["build_recipe_json"] == ""
    store.close()


def _mode(path: Path) -> int:
    import stat

    return stat.S_IMODE(path.stat().st_mode)


def test_source_tree_permissions_normalized_for_container(tmp_path):
    """生产 umask 0077 下解压树必须是 0755/0644。

    /app/src 以目录 bind mount 进 65534 容器：0700/0600 的解压产物会让
    python 预检与对局挂载一律 Permission denied（回归：python 分支曾跳过
    编译分支的 chmod 归一）。
    """
    import os
    import tempfile as _tempfile

    from bzplat.backend.bots.manager import BotManager
    from bzplat.backend.store.db import Store

    store = Store(str(tmp_path / "perm.db"))
    manager = BotManager(store, upload_root=tmp_path / "up")
    zp = _make_zip(
        tmp_path / "p.zip",
        {
            "__main__.py": "print(1)\n",
            "pkg/mod.py": "x = 1\n",
            "pkg/data/weights.txt": "0.5\n",
        },
    )

    prev = os.umask(0o077)
    try:
        temp_dir = Path(_tempfile.mkdtemp(prefix=".v1-", dir=tmp_path))
        checksum, size, runtime_image, _ = manager._prepare_source_version(
            zp.read_bytes(),
            temp_dir,
            temp_dir / "bot.bin",
            source_format="python",
            source_entry="",
            source_builder=None,
        )
    finally:
        os.umask(prev)

    src = temp_dir / "src"
    assert _mode(src) == 0o755
    assert _mode(src / "pkg") == 0o755
    assert _mode(src / "pkg" / "data") == 0o755
    assert _mode(src / "__main__.py") == 0o644
    assert _mode(src / "pkg" / "mod.py") == 0o644
    assert _mode(src / "pkg" / "data" / "weights.txt") == 0o644
    assert _mode(temp_dir / "bot.bin") == 0o755
    assert runtime_image  # python 版本必须声明运行镜像
    assert checksum and size
    store.close()


def test_compiled_source_build_sees_readable_tree_and_drops_out(tmp_path):
    """编译语言：builder 收到的源码树已归一可读，产物晋升后 out/ 不残留。"""
    import os
    import shutil as _shutil
    import tempfile as _tempfile

    from bzplat.backend.bots.manager import BotManager
    from bzplat.backend.store.db import Store

    static_elf = (
        Path(__file__).resolve().parents[3] / "samples" / "callbot_linux_amd64"
    )
    assert static_elf.is_file()

    captured = {}

    def fake_builder(src_dir, out_dir, recipe):
        captured["src"] = _mode(src_dir)
        captured["entry"] = _mode(src_dir / "main.cpp")
        captured["nested"] = _mode(src_dir / "util" / "h")
        _shutil.copyfile(static_elf, out_dir / "bot")

    store = Store(str(tmp_path / "cpp.db"))
    manager = BotManager(store, upload_root=tmp_path / "up")
    zp = _make_zip(
        tmp_path / "c.zip", {"main.cpp": "int main(){}", "util/h": "#pragma once"}
    )

    prev = os.umask(0o077)
    try:
        temp_dir = Path(_tempfile.mkdtemp(prefix=".v1-", dir=tmp_path))
        checksum, size, runtime_image, _ = manager._prepare_source_version(
            zp.read_bytes(),
            temp_dir,
            temp_dir / "bot.bin",
            source_format="cpp",
            source_entry="",
            source_builder=fake_builder,
        )
    finally:
        os.umask(prev)

    assert captured == {"src": 0o755, "entry": 0o644, "nested": 0o644}
    assert not (temp_dir / "out").exists()  # 构建工作目录不随版本晋升
    assert _mode(temp_dir / "bot.bin") == 0o755
    assert runtime_image == ""  # 编译产物不声明运行镜像
    assert checksum and size
    store.close()
