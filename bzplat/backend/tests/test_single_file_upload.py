"""单文件源码直传：扩展名策略、单成员 zip 归一化与上传全链路。

后端契约：非 zip 载荷在扩展名校验通过后包成规范单成员 zip，后续
inspect/构建/冻结管线零改动；扩展名与语言不符 fail-closed 400。
"""
from __future__ import annotations

import hashlib
import shutil
import zipfile
from pathlib import Path

import pytest

from bzplat.backend.bots.source_build import (
    SINGLE_FILE_ENTRIES,
    SourceBuildError,
    build_single_file_zip,
    inspect_source_zip,
    single_file_entry,
)
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app

_PY_CODE = (
    "import json,sys\n"
    "j=json.loads(sys.stdin.readline())\n"
    'print(json.dumps({"response":0}))\n'
    "sys.stdout.flush()\n"
)


def test_single_file_entry_policy() -> None:
    assert single_file_entry("c", "bot.c") == "main.c"
    assert single_file_entry("cpp", "bot.cpp") == "main.cpp"
    assert single_file_entry("cpp", "BOT.CC") == "main.cpp"
    assert single_file_entry("cpp", "a/b/bot.cxx") == "main.cpp"
    assert single_file_entry("go", "bot.go") == "main.go"
    assert single_file_entry("python", "bot.py") == "main.py"
    # 客户端路径噪声只取 basename；入口名恒为规范值。
    assert single_file_entry("python", "..\\..\\bot.py") == "main.py"

    for bad in ("", None, "bot.zip", "main", "bot.py.exe", "bot.c++"):
        with pytest.raises(SourceBuildError) as e:
            single_file_entry("python", bad)
        assert e.value.code == "invalid_source_file"
    with pytest.raises(SourceBuildError) as e:
        single_file_entry("python", "bot.cpp")
    assert e.value.code == "invalid_source_file"
    with pytest.raises(SourceBuildError) as e:
        single_file_entry("rust", "bot.rs")
    assert e.value.code == "unsupported_source_language"
    # elf 不走单文件路线（调用方先短路）。
    assert "elf" not in SINGLE_FILE_ENTRIES


def test_build_single_file_zip_is_deterministic_and_inspectable(tmp_path) -> None:
    data = _PY_CODE.encode()
    first = build_single_file_zip(data, "main.py")
    second = build_single_file_zip(data, "main.py")
    assert first == second  # 固定时间戳/属性：同输入同输出

    spec = inspect_source_zip(_bytes_to_zip_path(first, tmp_path), language="python")
    assert (spec.entry, spec.files) == ("main.py", 1)

    # 四种语言的规范入口名都能被默认入口解析命中。
    for lang, payload in (
        ("c", b"int main(){}"),
        ("cpp", b"int main(){}"),
        ("go", b"package main\n"),
        ("python", _PY_CODE.encode()),
    ):
        zipped = build_single_file_zip(payload, SINGLE_FILE_ENTRIES[lang])
        spec = inspect_source_zip(
            _bytes_to_zip_path(zipped, tmp_path), language=lang
        )
        assert spec.entry == SINGLE_FILE_ENTRIES[lang]
        assert spec.files == 1


def _bytes_to_zip_path(data: bytes, tmp_path: Path) -> Path:
    p = tmp_path / "single.zip"
    p.write_bytes(data)
    return p


def test_manager_wraps_single_python_file(tmp_path) -> None:
    """bytes 载荷：非 zip 的 .py 文件经归一化创建 python 源码 Bot。"""
    app = create_app(db_path=str(tmp_path / "sf.db"), upload_root=tmp_path / "up")
    store = app.state.store
    user = store.create_user("sfu", "sfu@e.com", hash_password("pw123456"))
    store.update_user(user["id"], email_verified=1)

    bot = app.state.bot_manager.create_from_upload(
        user["id"],
        "sfpysrc",
        _PY_CODE.encode(),  # 裸源文件，非 zip
        game_id="holdem",
        runtime_mode="traditional",
        source_format="python",
        source_filename="my_bot.py",
        binary_runner=None,  # 与 test_source_bots 同口径：跳过预检验证版本链
    )
    version = store.get_latest_bot_version(bot["id"])
    assert version["source_format"] == "python"
    launcher = Path(version["binary_path"])
    assert launcher.read_text().startswith("#!/bin/sh")
    assert (launcher.parent / "src" / "main.py").is_file()
    assert (launcher.parent / "src" / "main.py").read_text() == _PY_CODE
    store.close()


def test_manager_wraps_staged_single_file_streaming(tmp_path) -> None:
    """暂存载荷：归一化在同目录流式重写；zip 载荷字节不变。"""
    app = create_app(db_path=str(tmp_path / "st.db"), upload_root=tmp_path / "up")
    manager = app.state.bot_manager

    staged = manager.new_staged_upload()
    staged.path.write_bytes(b"package main\n")
    staged.size = staged.path.stat().st_size
    original_inode_dir = staged.path.parent
    from bzplat.backend.bots.manager import _normalize_source_payload

    raw = _normalize_source_payload(
        staged, source_format="go", source_filename="bot.go"
    )
    assert raw is staged
    assert staged.path.parent == original_inode_dir
    with zipfile.ZipFile(staged.path) as z:
        assert z.namelist() == ["main.go"]
        assert z.read("main.go") == b"package main\n"
    assert staged.size == staged.path.stat().st_size
    staged.close()

    # 已是 zip 的载荷原样返回（哈希不变）。
    staged2 = manager.new_staged_upload()
    payload = build_single_file_zip(b"package main\n", "main.go")
    staged2.path.write_bytes(payload)
    staged2.size = len(payload)
    _normalize_source_payload(
        staged2, source_format="go", source_filename="bot.go"
    )
    assert hashlib.sha256(staged2.path.read_bytes()).hexdigest() == (
        hashlib.sha256(payload).hexdigest()
    )
    staged2.close()
    app.state.store.close()


def test_manager_rejects_bad_single_file_and_empty(tmp_path) -> None:
    app = create_app(db_path=str(tmp_path / "bad.db"), upload_root=tmp_path / "up")
    manager = app.state.bot_manager

    with pytest.raises(SourceBuildError) as e:
        manager.create_from_upload(
            1, "badbot", b"print(1)", source_format="python",
            source_filename="bot.cc", binary_runner=None,
        )
    assert e.value.code == "invalid_source_file"

    with pytest.raises(Exception) as empty:
        manager.create_from_upload(
            1, "emptybot", b"", source_format="python",
            source_filename="bot.py", binary_runner=None,
        )
    from bzplat.backend.bots.manager import BotError

    assert isinstance(empty.value, BotError)
    assert empty.value.code == "invalid_size"
    app.state.store.close()


def test_manager_wraps_single_cpp_file_with_builder(tmp_path) -> None:
    """编译语言单文件：fake_builder 产静态 ELF，走既有冻结链。"""
    static_elf = (
        Path(__file__).resolve().parents[3] / "samples" / "callbot_linux_amd64"
    )
    assert static_elf.is_file()
    captured = {}

    def fake_builder(src_dir, out_dir, recipe):
        captured["entry"] = recipe["entry"]
        captured["src"] = (src_dir / "main.cpp").read_text()
        shutil.copyfile(static_elf, out_dir / "bot")

    app = create_app(db_path=str(tmp_path / "cpp.db"), upload_root=tmp_path / "up")
    store = app.state.store
    user = store.create_user("cppu", "cppu@e.com", hash_password("pw123456"))
    store.update_user(user["id"], email_verified=1)

    bot = app.state.bot_manager.create_from_upload(
        user["id"],
        "sfcpp",
        b"int main(){}",
        game_id="holdem",
        runtime_mode="traditional",
        source_format="cpp",
        source_filename="solver.cc",
        binary_runner=None,
        source_builder=fake_builder,
    )
    assert captured["entry"] == "main.cpp"
    assert captured["src"] == "int main(){}"
    version = store.get_latest_bot_version(bot["id"])
    assert version["source_format"] == "cpp"
    assert Path(version["binary_path"]).is_file()
    store.close()


def test_api_single_file_upload_end_to_end(tmp_path, monkeypatch) -> None:
    """API 层：multipart 单 .py 文件 → 200；扩展名不符 → 400。"""
    from fastapi.testclient import TestClient

    app = create_app(db_path=str(tmp_path / "api.db"), upload_root=tmp_path / "up")
    store = app.state.store
    user = store.create_user("apiu", "apiu@e.com", hash_password("pw123456"))
    store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate("apiu", "pw123456")
    # 与 test_source_bots 同口径：跳过预检，聚焦上传归一化链路。
    monkeypatch.setitem(
        app.state.__dict__, "preflight_runner_factory", lambda: None
    )
    client = TestClient(app)

    ok = client.post(
        "/api/bots",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "name": "apibot",
            "game_id": "holdem",
            "runtime_mode": "traditional",
            "source_format": "python",
        },
        files={"file": ("pasted.py", _PY_CODE.encode(), "text/x-python")},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["bot"]["name"] == "apibot"

    bad = client.post(
        "/api/bots",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "name": "badext",
            "game_id": "holdem",
            "runtime_mode": "traditional",
            "source_format": "python",
        },
        files={"file": ("pasted.cc", b"int main(){}", "text/x-c")},
    )
    assert bad.status_code == 400, bad.text
    assert bad.json()["detail"]["code"] == "invalid_source_file"
    store.close()
