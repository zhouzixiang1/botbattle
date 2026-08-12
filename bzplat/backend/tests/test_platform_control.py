"""Production service control must never create a second platform process."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from bzplat.backend import main


SOURCE_ROOT = Path(main.__file__).resolve().parents[2]


def _write_executable(path: Path, source: str) -> None:
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def _isolated_control(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    checkout = tmp_path / "checkout"
    scripts = checkout / "scripts"
    fake_bin = tmp_path / "fake-bin"
    scripts.mkdir(parents=True)
    fake_bin.mkdir()

    control = scripts / "platform-ctl.sh"
    control.write_bytes((SOURCE_ROOT / "scripts" / "platform-ctl.sh").read_bytes())
    control.chmod(0o700)

    systemctl_log = tmp_path / "systemctl.log"
    curl_log = tmp_path / "curl.log"
    nohup_log = tmp_path / "nohup.log"
    kill_log = tmp_path / "kill.log"
    process_state = tmp_path / "process.state"
    systemd_state = tmp_path / "systemd.state"
    process_state.write_text("dead\n", encoding="utf-8")

    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"$FAKE_SYSTEMCTL_LOG"
if [[ "${1:-}" != "--user" ]]; then exit 91; fi
case "${2:-}" in
  show)
    property=""
    for arg in "$@"; do
      case "$arg" in --property=*) property="${arg#--property=}" ;; esac
    done
    case "$property" in
      LoadState) printf '%s\n' "${FAKE_LOAD_STATE:-not-found}" ;;
      WorkingDirectory) printf '%s\n' "${FAKE_WORKING_DIRECTORY:-}" ;;
      ActiveState)
        if [[ -s "$FAKE_SYSTEMD_STATE" ]]; then
          cat "$FAKE_SYSTEMD_STATE"
        else
          printf '%s\n' "${FAKE_ACTIVE_STATE:-active}"
        fi
        ;;
      MainPID) printf '%s\n' "${FAKE_MAIN_PID:-4242}" ;;
      *) exit 92 ;;
    esac
    ;;
  start|restart) printf 'active\n' >"$FAKE_SYSTEMD_STATE" ;;
  stop) printf 'inactive\n' >"$FAKE_SYSTEMD_STATE" ;;
  *) exit 93 ;;
esac
""",
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"$FAKE_CURL_LOG"
exit "${FAKE_CURL_RC:-0}"
""",
    )
    _write_executable(
        fake_bin / "ss",
        """#!/usr/bin/env bash
set -eu
if [[ "${FAKE_SS_FAIL:-0}" == "1" ]]; then exit 2; fi
state="$(cat "$FAKE_PROCESS_STATE")"
if [[ "${FAKE_SS_LISTENING:-0}" == "1" || "$state" == "live" ]]; then
  printf 'LISTEN 0 128 127.0.0.1:50491 0.0.0.0:*\n'
fi
""",
    )
    _write_executable(
        fake_bin / "kill",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"$FAKE_KILL_LOG"
if [[ "${1:-}" == "-0" ]]; then
  [[ "$(cat "$FAKE_PROCESS_STATE")" == "live" ]] && exit 0
  [[ "${2:-}" != "4242" && "${FAKE_NEW_PID_LIVE:-0}" == "1" ]] && exit 0
  exit 1
fi
/bin/kill "$@"
printf 'dead\n' >"$FAKE_PROCESS_STATE"
""",
    )
    _write_executable(
        fake_bin / "nohup",
        """#!/usr/bin/env bash
set -eu
printf '%s\n' "$*" >>"$FAKE_NOHUP_LOG"
printf 'live\n' >"$FAKE_PROCESS_STATE"
exec "$@"
""",
    )

    bash_env = tmp_path / "bash-env"
    bash_env.write_text("enable -n kill\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "BASH_ENV": str(bash_env),
            "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
            "FAKE_CURL_LOG": str(curl_log),
            "FAKE_NOHUP_LOG": str(nohup_log),
            "FAKE_KILL_LOG": str(kill_log),
            "FAKE_PROCESS_STATE": str(process_state),
            "FAKE_SYSTEMD_STATE": str(systemd_state),
            "BZ_HOST": "127.0.0.1",
            "BZ_PORT": "50491",
        }
    )
    return control, env


def _run(control: Path, env: dict[str, str], action: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(control), action],
        cwd=control.parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=3,
        check=False,
    )


def test_status_uses_matching_user_systemd_unit_without_pid_runtime(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    env.update(
        {
            "FAKE_LOAD_STATE": "loaded",
            "FAKE_WORKING_DIRECTORY": str(control.parents[1]),
            "FAKE_ACTIVE_STATE": "active",
            "FAKE_MAIN_PID": "7319",
        }
    )

    result = _run(control, env, "status")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "running (user systemd) pid=7319"
    assert "http://127.0.0.1:50491/api/health" in Path(
        env["FAKE_CURL_LOG"]
    ).read_text(encoding="utf-8")
    assert not (control.parents[1] / "platform-ctl").exists()
    assert not (control.parents[1] / "logs").exists()


def test_restart_delegates_to_matching_user_systemd_and_waits_for_health(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    env.update(
        {
            "FAKE_LOAD_STATE": "loaded",
            "FAKE_WORKING_DIRECTORY": str(control.parents[1]),
            "FAKE_ACTIVE_STATE": "active",
        }
    )

    result = _run(control, env, "restart")

    assert result.returncode == 0, result.stderr
    assert "restarted botzone-platform.service" in result.stdout
    calls = Path(env["FAKE_SYSTEMCTL_LOG"]).read_text(encoding="utf-8").splitlines()
    assert "--user restart botzone-platform.service" in calls
    assert any("--property=MainPID" in call for call in calls)
    assert not Path(env["FAKE_NOHUP_LOG"]).exists()


def test_explicit_lan_bind_uses_loopback_health_probe_in_systemd_mode(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    env.update(
        {
            "BZ_HOST": "0.0.0.0",
            "BZ_ALLOW_LAN_BIND": "1",
            "FAKE_LOAD_STATE": "loaded",
            "FAKE_WORKING_DIRECTORY": str(control.parents[1]),
            "FAKE_ACTIVE_STATE": "active",
        }
    )

    result = _run(control, env, "status")

    assert result.returncode == 0, result.stderr
    assert "running (user systemd)" in result.stdout
    assert "http://127.0.0.1:50491/api/health" in Path(
        env["FAKE_CURL_LOG"]
    ).read_text(encoding="utf-8")


def test_stop_delegates_to_systemd_and_confirms_unit_and_port_are_down(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    env.update(
        {
            "FAKE_LOAD_STATE": "loaded",
            "FAKE_WORKING_DIRECTORY": str(control.parents[1]),
            "FAKE_ACTIVE_STATE": "active",
        }
    )

    result = _run(control, env, "stop")

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "stopped botzone-platform.service"
    calls = Path(env["FAKE_SYSTEMCTL_LOG"]).read_text(encoding="utf-8").splitlines()
    assert "--user stop botzone-platform.service" in calls


def test_systemd_start_refuses_unmanaged_listener_while_unit_is_inactive(
    tmp_path: Path,
):
    control, env = _isolated_control(tmp_path)
    env.update(
        {
            "FAKE_LOAD_STATE": "loaded",
            "FAKE_WORKING_DIRECTORY": str(control.parents[1]),
            "FAKE_ACTIVE_STATE": "inactive",
            "FAKE_SS_LISTENING": "1",
        }
    )

    result = _run(control, env, "start")

    assert result.returncode != 0
    assert "port 50491 is already listening" in result.stderr
    calls = Path(env["FAKE_SYSTEMCTL_LOG"]).read_text(encoding="utf-8").splitlines()
    assert "--user start botzone-platform.service" not in calls


def test_systemd_restart_refuses_active_unit_without_main_pid(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    env.update(
        {
            "FAKE_LOAD_STATE": "loaded",
            "FAKE_WORKING_DIRECTORY": str(control.parents[1]),
            "FAKE_ACTIVE_STATE": "active",
            "FAKE_MAIN_PID": "0",
        }
    )

    result = _run(control, env, "restart")

    assert result.returncode != 0
    assert "without a valid MainPID" in result.stderr
    calls = Path(env["FAKE_SYSTEMCTL_LOG"]).read_text(encoding="utf-8").splitlines()
    assert "--user restart botzone-platform.service" not in calls


def test_pid_fallback_refuses_listener_owned_outside_its_pid_file(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    env.update(
        {
            "FAKE_LOAD_STATE": "loaded",
            "FAKE_WORKING_DIRECTORY": str(tmp_path / "different-checkout"),
            "FAKE_SS_LISTENING": "1",
        }
    )

    result = _run(control, env, "start")

    assert result.returncode != 0
    assert "port 50491 is already listening" in result.stderr
    assert "do not start a second platform process" in result.stderr
    assert not Path(env["FAKE_NOHUP_LOG"]).exists()
    assert "--user start botzone-platform.service" not in Path(
        env["FAKE_SYSTEMCTL_LOG"]
    ).read_text(encoding="utf-8")


def test_pid_fallback_fails_closed_when_port_state_cannot_be_verified(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    env.update({"FAKE_LOAD_STATE": "not-found", "FAKE_SS_FAIL": "1"})

    result = _run(control, env, "start")

    assert result.returncode != 0
    assert "cannot verify port 50491: ss failed" in result.stderr
    assert not Path(env["FAKE_NOHUP_LOG"]).exists()


def test_pid_fallback_refuses_legacy_pid_record_without_signalling(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    checkout = control.parents[1]
    pid_dir = checkout / "platform-ctl"
    python = checkout / ".venv" / "bin" / "python"
    pid_dir.mkdir()
    (pid_dir / "web.pid").write_text("4242\n", encoding="utf-8")
    Path(env["FAKE_PROCESS_STATE"]).write_text("live\n", encoding="utf-8")
    env.update({"FAKE_LOAD_STATE": "not-found"})

    result = _run(control, env, "stop")

    assert result.returncode != 0
    assert "invalid legacy or malformed PID identity record" in result.stderr
    assert (pid_dir / "web.pid").is_file()
    assert not Path(env["FAKE_KILL_LOG"]).exists()


def test_pid_fallback_record_cannot_kill_reused_unrelated_pid(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    pid_dir = control.parents[1] / "platform-ctl"
    pid_dir.mkdir()
    pid = os.getpid()
    stat_line = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    starttime = stat_line.rsplit(") ", 1)[1].split()[19]
    (pid_dir / "web.pid").write_text(
        f"{pid} {'a' * 32} {starttime}\n", encoding="utf-8"
    )
    Path(env["FAKE_PROCESS_STATE"]).write_text("live\n", encoding="utf-8")
    env.update({"FAKE_LOAD_STATE": "not-found"})

    result = _run(control, env, "stop")

    assert result.returncode != 0
    assert "PID identity mismatch" in result.stderr
    kill_calls = Path(env["FAKE_KILL_LOG"]).read_text(encoding="utf-8").splitlines()
    assert kill_calls == [f"-0 {pid}"]
    assert (pid_dir / "web.pid").is_file()


def test_systemd_probe_failure_never_falls_back_to_pid_mode(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    fake_systemctl = Path(env["PATH"].split(":", 1)[0]) / "systemctl"
    _write_executable(
        fake_systemctl,
        "#!/usr/bin/env bash\nprintf '%s\\n' \"$*\" >>\"$FAKE_SYSTEMCTL_LOG\"\nexit 77\n",
    )

    result = _run(control, env, "start")

    assert result.returncode != 0
    assert "refusing PID fallback" in result.stderr
    assert not Path(env["FAKE_NOHUP_LOG"]).exists()
    assert not (control.parents[1] / "platform-ctl").exists()


def test_pid_fallback_owned_identity_can_start_status_and_stop(tmp_path: Path):
    control, env = _isolated_control(tmp_path)
    checkout = control.parents[1]
    python = checkout / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    _write_executable(python, "#!/usr/bin/env bash\nexec /bin/sleep 30\n")
    env.update({"FAKE_LOAD_STATE": "not-found"})

    started = _run(control, env, "start")
    try:
        assert started.returncode == 0, started.stderr
        record = (checkout / "platform-ctl" / "web.pid").read_text(
            encoding="utf-8"
        ).split()
        assert len(record) == 3
        assert record[0].isdigit()
        assert len(record[1]) == 32
        assert record[2].isdigit()

        status = _run(control, env, "status")
        assert status.returncode == 0, status.stderr
        assert f"pid={record[0]}" in status.stdout

        stopped = _run(control, env, "stop")
        assert stopped.returncode == 0, stopped.stderr
        assert not (checkout / "platform-ctl" / "web.pid").exists()
    finally:
        if (checkout / "platform-ctl" / "web.pid").exists():
            pid = (checkout / "platform-ctl" / "web.pid").read_text().split()[0]
            subprocess.run(["/bin/kill", pid], check=False)


def test_default_runtime_flock_names_are_ignored_exactly_and_not_globally():
    ignore_lines = {
        line.strip()
        for line in (SOURCE_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "/botzone.db.docker-launch.lock" in ignore_lines
    assert "/botzone.db.execution-dispatcher.lock" in ignore_lines
    assert "*.lock" not in ignore_lines
