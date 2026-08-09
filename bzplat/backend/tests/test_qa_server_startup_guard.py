"""QA server isolation must fail before the first filesystem/database writer."""
from __future__ import annotations

import os
from pathlib import Path

import pytest
import typer

from bzplat.backend import cli, main
from bzplat.backend.logging_config import setup_logging
from bzplat.backend.qa_safety import primary_checkout_root


SOURCE_ROOT = Path(main.__file__).resolve().parents[2]
PRIMARY_ROOT = primary_checkout_root(SOURCE_ROOT)


def test_cli_rejects_primary_logs_before_logging_or_uvicorn(monkeypatch, tmp_path):
    assert PRIMARY_ROOT is not None
    calls: list[str] = []
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        "bzplat.backend.logging_config.setup_logging",
        lambda **_kwargs: calls.append("logging"),
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *_args, **_kwargs: calls.append("uvicorn"))
    # ``on`` used to be parsed by CLI but not by create_app; keep it as a
    # regression value so both layers stay on the shared parser.
    monkeypatch.setenv("BZ_QA_INSTANCE", "on")
    monkeypatch.setenv("BZ_DB_PATH", str(tmp_path / "qa.db"))
    monkeypatch.setenv("BZ_LOG_DIR", str(PRIMARY_ROOT / "logs"))
    monkeypatch.setenv("BZ_AVATAR_DIR", str(tmp_path / "avatars"))

    with pytest.raises(typer.BadParameter, match="logs"):
        cli.serve(host="127.0.0.1", port=50381, reload=False)

    assert calls == []
    assert not (tmp_path / "qa.db").exists()
    assert not (tmp_path / "avatars").exists()


def test_cli_rejects_main_port_before_logging_or_uvicorn(monkeypatch, tmp_path):
    calls: list[str] = []
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        "bzplat.backend.logging_config.setup_logging",
        lambda **_kwargs: calls.append("logging"),
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *_args, **_kwargs: calls.append("uvicorn"))
    monkeypatch.setenv("BZ_QA_INSTANCE", "on")
    monkeypatch.setenv("BZ_DB_PATH", str(tmp_path / "qa.db"))
    monkeypatch.setenv("BZ_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BZ_AVATAR_DIR", str(tmp_path / "avatars"))

    with pytest.raises(typer.BadParameter, match="50380"):
        cli.serve(host="127.0.0.1", port=50380, reload=False)

    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_create_app_rejects_primary_avatars_before_store_or_mkdir(
    monkeypatch, tmp_path
):
    assert PRIMARY_ROOT is not None
    calls: list[str] = []
    monkeypatch.setattr(main, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        main,
        "Store",
        lambda *_args, **_kwargs: calls.append("store"),
    )
    monkeypatch.setattr(
        Path,
        "mkdir",
        lambda *_args, **_kwargs: calls.append("mkdir"),
    )
    monkeypatch.setenv("BZ_QA_INSTANCE", "on")
    monkeypatch.setenv("BZ_AVATAR_DIR", str(PRIMARY_ROOT / "avatars"))

    with pytest.raises(RuntimeError, match="avatars"):
        main.create_app(
            db_path=str(tmp_path / "qa.db"),
            upload_root=tmp_path / "bot_uploads",
        )

    assert calls == []
    assert not (tmp_path / "qa.db").exists()


def test_cli_pins_default_qa_runtime_dirs_beside_database(monkeypatch, tmp_path):
    captured: dict[str, str] = {}
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        "bzplat.backend.logging_config.setup_logging",
        lambda **_kwargs: captured.update(
            db=os.environ["BZ_DB_PATH"],
            logs=os.environ["BZ_LOG_DIR"],
            avatars=os.environ["BZ_AVATAR_DIR"],
        ),
    )
    monkeypatch.setattr(cli.uvicorn, "run", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("BZ_QA_INSTANCE", "1")
    monkeypatch.setenv("BZ_DB_PATH", "runtime/qa.db")
    monkeypatch.delenv("BZ_LOG_DIR", raising=False)
    monkeypatch.delenv("BZ_AVATAR_DIR", raising=False)

    cli.serve(host="127.0.0.1", port=50381, reload=False)

    runtime = (tmp_path / "runtime").resolve()
    assert captured == {
        "db": str(runtime / "qa.db"),
        "logs": str(runtime / "logs"),
        "avatars": str(runtime / "avatars"),
    }
    assert not runtime.exists(), "validation and env pinning must not create paths"


def test_create_app_defaults_qa_uploads_and_avatars_beside_database(
    monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    from bzplat.backend.crypto import hash_password

    runtime = tmp_path / "runtime"
    runtime.mkdir()
    db_path = runtime / "qa.db"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "_load_dotenv", lambda: None)
    monkeypatch.setenv("BZ_QA_INSTANCE", "1")
    monkeypatch.delenv("BZ_AVATAR_DIR", raising=False)

    app = main.create_app(db_path=str(db_path))
    try:
        assert app.state.bot_manager.upload_root.resolve() == (
            runtime / "bot_uploads"
        ).resolve()
        assert (runtime / "avatars").is_dir()
        assert not (tmp_path / "avatars").exists()

        user = app.state.store.create_user(
            "qa-avatar", "qa-avatar@example.com", hash_password("password1")
        )
        app.state.store.update_user(user["id"], email_verified=1)
        _, token = app.state.auth.authenticate("qa-avatar", "password1")
        with TestClient(app) as client:
            response = client.post(
                "/api/auth/avatar",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("avatar.png", b"\x89PNG\r\n", "image/png")},
            )
            assert response.status_code == 200, response.text
            rel = response.json()["avatar"]
            assert (runtime / "avatars" / rel).read_bytes() == b"\x89PNG\r\n"
            served = client.get(f"/avatars/{rel}")
            assert served.status_code == 200
            assert served.content == b"\x89PNG\r\n"
        assert not (tmp_path / "avatars").exists()
    finally:
        app.state.store.close()


def test_qa_logging_never_falls_back_to_process_cwd(monkeypatch, tmp_path):
    monkeypatch.setenv("BZ_QA_INSTANCE", "1")
    monkeypatch.setattr(
        Path,
        "mkdir",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("isolated log target unavailable")
        ),
    )

    with pytest.raises(PermissionError, match="isolated log target unavailable"):
        setup_logging(log_dir=tmp_path / "isolated-logs")
