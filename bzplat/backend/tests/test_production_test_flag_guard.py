"""Test-only escape hatches must never reach a production service process."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
import typer

from bzplat.backend import cli, main


SOURCE_ROOT = Path(main.__file__).resolve().parents[2]
TEST_ONLY_FLAGS = (
    "BZ_BOT_LOCAL",
    "BZ_SKIP_CAPTCHA",
    "BZ_TEST_CAPTCHA",
)


def _clear_test_only_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*TEST_ONLY_FLAGS, "BZ_QA_INSTANCE"):
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("flag", TEST_ONLY_FLAGS)
def test_cli_rejects_test_only_flag_without_qa_before_side_effects(
    flag: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    _clear_test_only_flags(monkeypatch)
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.setenv(flag, "on")

    with pytest.raises(typer.BadParameter, match=rf"{flag}.*BZ_QA_INSTANCE"):
        cli.serve(host="127.0.0.1", port=50491, reload=False)

    assert calls == []
    assert list(tmp_path.iterdir()) == []


def test_cli_rejects_test_only_flag_loaded_from_dotenv_before_side_effects(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    _clear_test_only_flags(monkeypatch)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "BZ_SKIP_CAPTCHA=' TrUe '\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "bzplat.backend.logging_config.setup_logging",
        lambda **_kwargs: calls.append("logging"),
    )
    monkeypatch.setattr(
        cli.uvicorn,
        "run",
        lambda *_args, **_kwargs: calls.append("uvicorn"),
    )

    try:
        with pytest.raises(
            typer.BadParameter,
            match=r"BZ_SKIP_CAPTCHA.*BZ_QA_INSTANCE",
        ):
            cli.serve(host="127.0.0.1", port=50491, reload=False)
    finally:
        # ``_load_dotenv`` mutates os.environ directly, outside monkeypatch's
        # mutation ledger, so the fixture must remove the loaded value itself.
        os.environ.pop("BZ_SKIP_CAPTCHA", None)

    assert calls == []
    assert [path.name for path in tmp_path.iterdir()] == [".env"]


@pytest.mark.parametrize("flag", TEST_ONLY_FLAGS)
def test_cli_allows_test_only_flag_only_for_isolated_qa(
    flag: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    _clear_test_only_flags(monkeypatch)
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.setenv("BZ_QA_INSTANCE", "yes")
    monkeypatch.setenv("BZ_DB_PATH", str(tmp_path / "qa.db"))
    monkeypatch.setenv("BZ_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("BZ_AVATAR_DIR", str(tmp_path / "avatars"))
    monkeypatch.setenv(flag, "on")

    cli.serve(host="127.0.0.1", port=50491, reload=False)

    assert calls == ["logging", "uvicorn"]
    assert not (tmp_path / "qa.db").exists()
    assert not (tmp_path / "logs").exists()
    assert not (tmp_path / "avatars").exists()


@pytest.mark.parametrize("flag", TEST_ONLY_FLAGS)
def test_cli_does_not_treat_false_test_flag_as_enabled(
    flag: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []
    _clear_test_only_flags(monkeypatch)
    monkeypatch.chdir(tmp_path)
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
    monkeypatch.setenv(flag, "0")

    cli.serve(host="127.0.0.1", port=50491, reload=False)

    assert calls == ["logging", "uvicorn"]


@pytest.mark.parametrize("flag", TEST_ONLY_FLAGS)
def test_platform_ctl_rejects_test_only_flag_even_with_qa_marker(
    flag: str,
    tmp_path: Path,
) -> None:
    isolated = tmp_path / "checkout"
    scripts = isolated / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "platform-ctl.sh"
    script.write_bytes((SOURCE_ROOT / "scripts" / "platform-ctl.sh").read_bytes())
    script.chmod(0o700)
    (isolated / ".env").write_text(
        f"BZ_PORT=50491\nBZ_QA_INSTANCE=1\n{flag}=' YES '\n",
        encoding="utf-8",
    )

    # Keep the pre-fix red test isolated from user systemd and every real port.
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    systemctl = fake_bin / "systemctl"
    systemctl.write_text(
        "#!/usr/bin/env bash\n"
        "if [[ \"$*\" == *\"LoadState\"* ]]; then echo not-found; exit 0; fi\n"
        "exit 1\n",
        encoding="utf-8",
    )
    systemctl.chmod(0o700)
    ss = fake_bin / "ss"
    ss.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    ss.chmod(0o700)
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in (*TEST_ONLY_FLAGS, "BZ_QA_INSTANCE")
    }
    env["PATH"] = f"{fake_bin}:{env.get('PATH', '')}"

    result = subprocess.run(
        ["bash", str(script), "status"],
        cwd=isolated,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 1
    assert flag in result.stderr
    assert "test-only" in result.stderr
    assert not (isolated / "platform-ctl").exists()
    assert not (isolated / "logs").exists()
