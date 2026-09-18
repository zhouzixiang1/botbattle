"""上传/构建通道加固回归：内存准入、损坏 zip 4xx、产物上限与静态链接、
状态码语义与空 source_format 归一。"""
from __future__ import annotations

import struct
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from bzplat.backend.bots import manager as manager_module
from bzplat.backend.bots.classify import program_header_has_dynamic_interpreter
from bzplat.backend.bots.manager import BotManager
from bzplat.backend.bots.source_build import (
    _BUILD_COMMANDS,
    SourceBuildError,
    build_recipe,
    inspect_source_zip,
)
from bzplat.backend.crypto import hash_password
from bzplat.backend.main import create_app
from bzplat.backend.store import Store


def _make_zip(path: Path, files: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return path


def _elf_head(*, pt_interp: bool, phnum: int = 1) -> bytes:
    """最小可分类 ELF64 LE x86-64 EXEC 头 + 程序头（p_type 可控）。"""
    head = bytearray(64 + 56 * phnum)
    head[0:4] = b"\x7fELF"
    head[4] = 2  # ELFCLASS64
    head[5] = 1  # little endian
    head[6] = 1  # version
    struct.pack_into("<H", head, 16, 2)  # e_type = ET_EXEC
    struct.pack_into("<H", head, 18, 62)  # e_machine = x86-64
    struct.pack_into("<I", head, 20, 1)  # e_version
    struct.pack_into("<Q", head, 0x20, 64)  # e_phoff
    struct.pack_into("<H", head, 0x36, 56)  # e_phentsize
    struct.pack_into("<H", head, 0x38, phnum)  # e_phnum
    struct.pack_into("<I", head, 64, 3 if pt_interp else 1)
    return bytes(head)


def _manager(tmp_path: Path) -> BotManager:
    store = Store(str(tmp_path / "mgr.db"))
    return BotManager(store, upload_root=tmp_path / "uploads")


def test_corrupt_zip_member_data_is_user_error(tmp_path: Path) -> None:
    """central directory 合法但成员数据损坏 → invalid_source_zip（4xx），非 500。"""
    zp = _make_zip(
        tmp_path / "bad.zip", {"__main__.py": "print('x' * 200)"}
    )
    raw = bytearray(zp.read_bytes())
    # 破坏成员压缩数据中段（文件头之后、central directory 之前）。
    raw[60] ^= 0xFF
    mgr = _manager(tmp_path)
    temp_dir = tmp_path / "prep"
    temp_dir.mkdir()
    with pytest.raises(SourceBuildError) as e:
        mgr._prepare_source_version(
            bytes(raw),
            temp_dir,
            temp_dir / "bot.bin",
            source_format="python",
            source_entry="",
            source_builder=None,
        )
    assert e.value.code == "invalid_source_zip"


def test_go_build_command_disables_cgo(tmp_path: Path) -> None:
    assert "CGO_ENABLED=0" in _BUILD_COMMANDS["go"]
    zp = _make_zip(tmp_path / "main.zip", {"main.go": "package main\nfunc main(){}\n"})
    spec = inspect_source_zip(zp, language="go")
    assert "CGO_ENABLED=0" in build_recipe(spec)["command"]


def test_program_header_has_dynamic_interpreter() -> None:
    assert program_header_has_dynamic_interpreter(_elf_head(pt_interp=True)) is True
    assert program_header_has_dynamic_interpreter(_elf_head(pt_interp=False)) is False
    # 头截断放不下完整程序头表 → None（无法判定，跳过检查）。
    truncated = _elf_head(pt_interp=True, phnum=4)[:100]
    assert program_header_has_dynamic_interpreter(truncated) is None
    assert program_header_has_dynamic_interpreter(b"#!/bin/sh\n") is None


def test_dynamic_artifact_rejected(tmp_path: Path) -> None:
    """带 PT_INTERP 的产物按 build_failed 拒绝，不进入版本目录。"""

    def fake_builder(src_dir: Path, out_dir: Path, recipe: dict) -> None:
        (out_dir / "bot").write_bytes(_elf_head(pt_interp=True) + b"\x00" * 32)

    zp = _make_zip(tmp_path / "dyn.zip", {"main.cpp": "int main(){}"})
    mgr = _manager(tmp_path)
    temp_dir = tmp_path / "prep"
    temp_dir.mkdir()
    with pytest.raises(SourceBuildError) as e:
        mgr._prepare_source_version(
            zp.read_bytes(),
            temp_dir,
            temp_dir / "bot.bin",
            source_format="cpp",
            source_entry="",
            source_builder=fake_builder,
        )
    assert e.value.code == "build_failed"
    assert "动态链接" in e.value.message


def test_oversize_artifact_rejected(tmp_path: Path, monkeypatch) -> None:
    """产物大小超过 MAX_BYTES → build_failed（版本目录/磁盘不允许无上限）。"""
    monkeypatch.setattr(manager_module, "MAX_BYTES", 8)

    def fake_builder(src_dir: Path, out_dir: Path, recipe: dict) -> None:
        (out_dir / "bot").write_bytes(b"\x7fELF" + b"\x00" * 12)

    zp = _make_zip(tmp_path / "big.zip", {"main.cpp": "int main(){}"})
    mgr = _manager(tmp_path)
    temp_dir = tmp_path / "prep"
    temp_dir.mkdir()
    with pytest.raises(SourceBuildError) as e:
        mgr._prepare_source_version(
            zp.read_bytes(),
            temp_dir,
            temp_dir / "bot.bin",
            source_format="cpp",
            source_entry="",
            source_builder=fake_builder,
        )
    assert e.value.code == "build_failed"
    assert "平台上限" in e.value.message


def test_build_busy_admission_and_api_503(tmp_path: Path, monkeypatch) -> None:
    """内存不足时构建通道保守拒绝（build_busy），API 映射 503。"""
    from bzplat.backend.runtime import limits as limits_module

    # 本用例需要 docker 模式的 shared_supervisor 才能到达内存准入；
    # 套件中部分旧测试以 setdefault 方式注入 BZ_BOT_LOCAL 且不清理
    # （条件性泄漏，见 PR 说明的已知债务），此处显式声明环境。
    monkeypatch.delenv("BZ_BOT_LOCAL", raising=False)
    app = create_app(db_path=str(tmp_path / "busy.db"))
    store = app.state.store
    user = store.create_user("buu", "buu@e.com", hash_password("pw123456"))
    store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate("buu", "pw123456")

    monkeypatch.setattr(
        limits_module,
        "effective_host_resource_budget",
        lambda: SimpleNamespace(memory_mb=4096),
    )
    monkeypatch.setattr(
        store.executions,
        "execution_memory_in_use_mb",
        lambda: 8192,
    )
    with pytest.raises(SourceBuildError) as direct:
        app.state.source_builder(
            tmp_path / "src", tmp_path / "out", {"image": "x", "timeout_sec": 1}
        )
    assert direct.value.code == "build_busy"

    # API 映射：503 + code（不再是 400）。
    from fastapi.testclient import TestClient

    zp = _make_zip(tmp_path / "api.zip", {"main.cpp": "int main(){}"})

    def busy_builder(src_dir, out_dir, recipe):
        raise SourceBuildError("build_busy", "平台内存资源紧张")

    monkeypatch.setitem(app.state.__dict__, "source_builder", busy_builder)
    client = TestClient(app)
    response = client.post(
        "/api/bots",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "name": "busybot",
            "game_id": "holdem",
            "runtime_mode": "traditional",
            "source_format": "cpp",
        },
        files={"file": ("src.zip", zp.read_bytes(), "application/zip")},
    )
    assert response.status_code == 503, response.text
    assert response.json()["detail"]["code"] == "build_busy"
    store.close()


def test_source_runtime_http_wiring_and_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    """source_runtime 的 HTTP multipart 接线：ml 冻结 ML 镜像；非法值 4xx。

    manager 层已测变体映射；本例钉住 api_routes 的字段名与 kwarg 传递
    （拼错字段名会静默落标准库镜像、只有预检 import 失败才暴露）。
    """
    import zipfile

    from fastapi.testclient import TestClient

    # 成功路径触发预检：用本机 runner（launcher 即 python3 脚本）。
    monkeypatch.setenv("BZ_BOT_LOCAL", "1")
    app = create_app(db_path=str(tmp_path / "sr.db"))
    store = app.state.store
    user = store.create_user("sru", "sru@e.com", hash_password("pw123456"))
    store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate("sru", "pw123456")

    zp = tmp_path / "mlsrc.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("__main__.py", "import json,sys\nprint(json.dumps({'response':0}))\n")

    client = TestClient(app)
    headers = {"Authorization": f"Bearer {token}"}
    ok = client.post(
        "/api/bots",
        headers=headers,
        data={
            "name": "mlhttp",
            "game_id": "holdem",
            "runtime_mode": "traditional",
            "source_format": "python",
            "source_runtime": "ml",
        },
        files={"file": ("src.zip", zp.read_bytes(), "application/zip")},
    )
    assert ok.status_code == 200, ok.text
    version = store.get_latest_bot_version(ok.json()["bot"]["id"])
    assert version["runtime_image"] == "botbattle-ml-py3:bookworm-1"

    for bad in ("cuda", "ML!"):
        rejected = client.post(
            "/api/bots",
            headers=headers,
            data={
                "name": f"bad{bad[:2]}",
                "game_id": "holdem",
                "source_format": "python",
                "source_runtime": bad,
            },
            files={"file": ("src.zip", zp.read_bytes(), "application/zip")},
        )
        assert rejected.status_code == 400, rejected.text
        assert rejected.json()["detail"]["code"] == "invalid_source_runtime"

    cpp_ml = client.post(
        "/api/bots",
        headers=headers,
        data={
            "name": "cppml",
            "game_id": "holdem",
            "source_format": "cpp",
            "source_runtime": "ml",
        },
        files={"file": ("src.zip", zp.read_bytes(), "application/zip")},
    )
    assert cpp_ml.status_code == 400
    assert cpp_ml.json()["detail"]["code"] == "invalid_source_runtime"
    store.close()


def test_version_upload_not_found_returns_404(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    app = create_app(db_path=str(tmp_path / "nf.db"))
    store = app.state.store
    user = store.create_user("nfu", "nfu@e.com", hash_password("pw123456"))
    store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate("nfu", "pw123456")
    client = TestClient(app)
    response = client.post(
        "/api/bots/999999/versions",
        headers={"Authorization": f"Bearer {token}"},
        data={"runtime_mode": "traditional"},
        files={"file": ("bot.bin", b"\x7fELF" + b"\x00" * 60, "application/octet-stream")},
    )
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "not_found"
    store.close()


def test_empty_source_format_follows_elf_route(tmp_path: Path) -> None:
    """空 source_format 归一为 elf：zip 载荷按格式错误拒绝，而不是源码路线。"""
    from fastapi.testclient import TestClient

    app = create_app(db_path=str(tmp_path / "fmt.db"))
    store = app.state.store
    user = store.create_user("fmtu", "fmtu@e.com", hash_password("pw123456"))
    store.update_user(user["id"], email_verified=1)
    _, token = app.state.auth.authenticate("fmtu", "pw123456")
    client = TestClient(app)
    zp = _make_zip(tmp_path / "e.zip", {"main.cpp": "int main(){}"})
    response = client.post(
        "/api/bots",
        headers={"Authorization": f"Bearer {token}"},
        data={
            "name": "fmtbot",
            "game_id": "holdem",
            "runtime_mode": "traditional",
            "source_format": "",
        },
        files={"file": ("src.zip", zp.read_bytes(), "application/zip")},
    )
    assert response.status_code == 400
    assert response.json()["detail"]["code"] != "unsupported_source_language"
    store.close()


def test_execution_memory_in_use_mb_reads_capacity_view(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "cap.db"))
    store.executions.resume()
    assert store.executions.execution_memory_in_use_mb() == 0
    store.close()
