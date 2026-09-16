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
