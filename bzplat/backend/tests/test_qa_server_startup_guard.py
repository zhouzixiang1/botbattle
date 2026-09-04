"""QA server isolation must fail before the first filesystem/database writer."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import typer

from bzplat.backend import cli, main
from bzplat.backend.logging_config import setup_logging
from bzplat.backend.qa_safety import primary_checkout_root
from bzplat.backend.runtime.limits import MAX_LOCAL_AI_WEBSOCKET_MESSAGE_BYTES


SOURCE_ROOT = Path(main.__file__).resolve().parents[2]
PRIMARY_ROOT = primary_checkout_root(SOURCE_ROOT)
_INHERITED_TEST_ONLY_FLAGS = (
    "BZ_BOT_LOCAL",
    "BZ_SKIP_CAPTCHA",
    "BZ_TEST_CAPTCHA",
    "BZ_QA_INSTANCE",
)


@pytest.fixture(autouse=True)
def _clear_inherited_test_only_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make every production-startup case declare its own QA state."""

    for name in _INHERITED_TEST_ONLY_FLAGS:
        monkeypatch.delenv(name, raising=False)


def test_platform_ctl_creates_runtime_state_with_private_umask():
    script = (SOURCE_ROOT / "scripts" / "platform-ctl.sh").read_text(
        encoding="utf-8"
    )
    strict_mode = script.index("set -euo pipefail")
    private_umask = script.index("umask 077")
    first_runtime_dir = script.index('PID_DIR="$ROOT/platform-ctl"')
    assert strict_mode < private_umask < first_runtime_dir


def test_platform_ctl_rejects_non_loopback_env_before_creating_runtime_dirs(
    tmp_path,
):
    source = SOURCE_ROOT / "scripts" / "platform-ctl.sh"
    isolated = tmp_path / "checkout"
    (isolated / "scripts").mkdir(parents=True)
    script = isolated / "scripts" / "platform-ctl.sh"
    script.write_bytes(source.read_bytes())
    script.chmod(0o700)
    (isolated / ".env").write_text("BZ_HOST=0.0.0.0\n", encoding="utf-8")

    result = subprocess.run(
        ["bash", str(script), "status"],
        cwd=isolated,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert "refusing non-loopback BZ_HOST=0.0.0.0" in result.stderr
    assert not (isolated / "platform-ctl").exists()
    assert not (isolated / "logs").exists()


def test_systemd_template_creates_runtime_state_with_private_umask():
    service = (SOURCE_ROOT / "deploy" / "botzone-platform.service").read_text(
        encoding="utf-8"
    )
    assert "UMask=0077" in service
    assert "bzplat.backend.cli serve" in service
    assert "--host" not in service
    assert "--port" not in service
    assert "${" not in service


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


def test_invalid_trusted_proxy_cidr_fails_before_store_or_runtime_writers(
    monkeypatch, tmp_path
):
    calls: list[str] = []
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.setenv(
        "BZ_TRUSTED_PROXY_CIDRS",
        "127.0.0.1/32,bad-cidr",
    )

    with pytest.raises(ValueError, match="BZ_TRUSTED_PROXY_CIDRS"):
        main.create_app(db_path=str(tmp_path / "runtime" / "qa.db"))

    assert calls == []
    assert not (tmp_path / "runtime").exists()


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


def test_cli_preserves_application_owned_uvicorn_logging(monkeypatch):
    """Uvicorn must preserve app-owned logging and socket-peer identity."""
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        "bzplat.backend.logging_config.setup_logging",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *_args, **kwargs: captured.update(kwargs),
    )
    monkeypatch.delenv("BZ_QA_INSTANCE", raising=False)

    cli.serve(host="127.0.0.1", port=50381, reload=False)

    assert captured["log_config"] is None
    assert captured["proxy_headers"] is False
    assert captured["ws_max_size"] == MAX_LOCAL_AI_WEBSOCKET_MESSAGE_BYTES


def test_cli_rejects_wildcard_bind_without_explicit_lan_gate(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        "bzplat.backend.logging_config.setup_logging",
        lambda **_kwargs: calls.append("logging"),
    )
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *_args, **_kwargs: calls.append("uvicorn"),
    )
    monkeypatch.delenv("BZ_ALLOW_LAN_BIND", raising=False)
    monkeypatch.delenv("BZ_QA_INSTANCE", raising=False)

    with pytest.raises(typer.BadParameter, match="BZ_ALLOW_LAN_BIND=1"):
        cli.serve(host="0.0.0.0", port=50381, reload=False)

    assert calls == []


def test_cli_allows_explicit_wildcard_lan_bind_and_disables_proxy_rewrite(
    monkeypatch,
):
    captured: dict[str, object] = {}
    monkeypatch.setattr(cli, "_load_dotenv", lambda: None)
    monkeypatch.setattr(
        "bzplat.backend.logging_config.setup_logging",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *_args, **kwargs: captured.update(kwargs),
    )
    monkeypatch.setenv("BZ_ALLOW_LAN_BIND", "1")
    monkeypatch.delenv("BZ_QA_INSTANCE", raising=False)

    cli.serve(host="0.0.0.0", port=50381, reload=False)

    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 50381
    assert captured["proxy_headers"] is False


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
