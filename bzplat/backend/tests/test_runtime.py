"""二进制分类与样例 bot 对局冒烟。"""
from __future__ import annotations

import asyncio
import concurrent.futures
import subprocess
import struct
import time
from pathlib import Path

import pytest

from bzplat.backend.bots.classify import (
    BinaryInfo,
    BinaryRejectError,
    classify_binary,
)
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.games.holdem import protocol as proto
from bzplat.backend.runtime.binary_runner import (
    BinaryRunner,
    BotCrashedError,
    BotSession,
    PlatformRunnerError,
)
from bzplat.backend.runtime.docker_supervisor import (
    CANONICAL_DOCKER_HOST,
    DockerExecutionIdentity,
    DockerSupervisor,
    DockerSupervisorError,
)

SAMPLES = Path(__file__).resolve().parents[3] / "samples"
ELF = SAMPLES / "callbot_linux_amd64"


def test_classify_elf():
    data = ELF.read_bytes()
    info = classify_binary(data)
    assert info.format == "elf"
    assert info.runnable
    assert info.arch == "amd64"
    assert info.os == "linux"


def test_classify_macho_rejected():
    data = b"\xfe\xed\xfa\xce" + b"\x00" * 20
    info = classify_binary(data)
    assert info.format == "macho"
    assert not info.runnable
    assert "仅支持 Linux x86_64 ELF" in info.reject_reason


def _valid_pe(machine: int = 0x8664) -> bytes:
    data = bytearray(0x80)
    data[:2] = b"MZ"
    struct.pack_into("<I", data, 0x3C, 0x40)
    data[0x40:0x44] = b"PE\0\0"
    struct.pack_into("<H", data, 0x44, machine)
    return bytes(data)


def _mutated_sample_elf(*, elf_class=2, endian=1, osabi=0, machine=0x3E) -> bytes:
    data = bytearray(ELF.read_bytes()[:4096])
    data[4] = elf_class
    data[5] = endian
    data[7] = osabi
    byteorder = "<" if endian == 1 else ">"
    struct.pack_into(f"{byteorder}H", data, 18, machine)
    return bytes(data)


@pytest.mark.parametrize(
    ("data", "expected_format"),
    [
        (_valid_pe(), "pe"),
        (b"#!/usr/bin/env python3\nprint(1)\n", "unknown"),
        (_mutated_sample_elf(elf_class=1, machine=0x03), "elf"),
        (_mutated_sample_elf(machine=0xB7), "elf"),
        (_mutated_sample_elf(endian=2), "elf"),
        (_mutated_sample_elf(osabi=9), "elf"),
    ],
    ids=["windows-pe", "python-script", "elf32-i386", "elf64-arm64", "big-endian", "foreign-osabi"],
)
def test_classifier_rejects_every_non_linux_x86_64_elf64_target(data, expected_format):
    info = classify_binary(data)
    assert info.format == expected_format
    assert not info.runnable
    assert "仅支持 Linux x86_64 ELF" in info.reject_reason


def test_pe_is_diagnostic_only_even_when_machine_is_amd64():
    info = classify_binary(_valid_pe())
    assert info == BinaryInfo(
        "pe",
        "windows",
        "amd64",
        False,
        info.reject_reason,
    )
    assert "Windows PE 不受支持" in info.reject_reason


def _docker_payload(args) -> tuple[str, ...]:
    """Return the command after the mandatory local-socket prefix."""
    argv = tuple(args)
    assert argv[1:3] == ("--host", CANONICAL_DOCKER_HOST)
    return argv[3:]


def _allow_fake_docker_binary(monkeypatch) -> None:
    """Let command-boundary tests use a synthetic Docker executable name."""
    import bzplat.backend.runtime.docker_supervisor as supervisor_mod

    monkeypatch.setattr(supervisor_mod.shutil, "which", lambda _name: "/fake/docker")


def test_missing_docker_fails_closed_unless_local_mode_is_explicit():
    """Production may never execute an uploaded ELF directly on the host."""
    info = BinaryInfo("elf", "linux", "amd64", True)

    with pytest.raises(DockerSupervisorError, match="Docker CLI 不可用"):
        BinaryRunner(docker_bin="definitely-no-such-docker", prefer_local=False)

    explicit_test_mode = BinaryRunner(
        docker_bin="definitely-no-such-docker", prefer_local=True
    )
    assert explicit_test_mode._select_mode(info) == "local"


def test_docker_exit_125_is_platform_fault_not_bot_crash(tmp_path, monkeypatch):
    """docker run exit 125 is an infrastructure failure and must not rate a Bot."""
    import bzplat.backend.runtime.binary_runner as runtime_mod

    class FakeStdin:
        def write(self, _data):
            return None

        async def drain(self):
            return None

    class FakeStdout:
        async def readline(self):
            return b""

    class FakeProc:
        def __init__(self, returncode: int):
            self.returncode = returncode
            self.stdin = FakeStdin()
            self.stdout = FakeStdout()

        async def wait(self):
            return self.returncode

    path = tmp_path / "bot"
    path.write_bytes(b"unused")
    info = BinaryInfo("elf", "linux", "amd64", True)

    _allow_fake_docker_binary(monkeypatch)
    infra = BinaryRunner(
        docker_bin="docker-exit-125-cache-test",
        prefer_local=False,
        linux_image="test.invalid/exit-125:latest",
    )
    cache_key = (infra._docker_bin, infra._linux_image)
    with runtime_mod._IMAGE_READY_LOCK:
        runtime_mod._IMAGE_READY_KEYS.add(cache_key)
    infra._sessions["infra"] = BotSession(
        "infra", info, path, proc=FakeProc(125), mode="docker",
    )
    with pytest.raises(PlatformRunnerError, match="docker exit 125"):
        asyncio.run(infra.send("infra", "{}"))
    with runtime_mod._IMAGE_READY_LOCK:
        assert cache_key not in runtime_mod._IMAGE_READY_KEYS

    bot_fault = BinaryRunner(prefer_local=False)
    bot_fault._sessions["bot"] = BotSession(
        "bot", info, path, proc=FakeProc(126), mode="docker",
    )
    with pytest.raises(BotCrashedError):
        asyncio.run(bot_fault.send("bot", "{}"))


def test_decision_timeout_log_does_not_expose_bot_stderr_paths(tmp_path, caplog):
    class FakeStdin:
        def write(self, _data):
            return None

        async def drain(self):
            return None

    class SlowStdout:
        async def readline(self):
            await asyncio.sleep(1)
            return b""

    class FakeProc:
        returncode = None
        stdin = FakeStdin()
        stdout = SlowStdout()

    path = tmp_path / "bot"
    path.write_bytes(b"unused")
    info = BinaryInfo("elf", "linux", "amd64", True)
    runner = BinaryRunner(prefer_local=False)
    session = BotSession("timeout-session", info, path, proc=FakeProc(), mode="docker")
    session._stderr_tail.extend(b"/private/bot_uploads/secret-version")
    runner._sessions[session.session_id] = session

    with caplog.at_level("WARNING"), pytest.raises(TimeoutError):
        asyncio.run(runner.send(session.session_id, "{}", timeout=0.001))
    assert "timeout-session" in caplog.text
    assert "/private" not in caplog.text
    assert "secret-version" not in caplog.text


def test_docker_argv_is_linux_amd64_and_enforces_sandbox_baseline(tmp_path):
    """唯一生产容器路径固定 linux/amd64，并在 argv 层强制硬隔离。"""
    elf_path = tmp_path / "bot"
    elf_path.write_bytes(b"unused")
    identity = DockerExecutionIdentity("runtime-test", "req_runtime", 3)
    args = tuple(
        DockerSupervisor.sandbox_options(
            identity=identity,
            slot=1,
            name=identity.container_name(1),
            binary_path=elf_path,
            image="debian:bookworm-slim",
            memory="512m",
            cpus="1",
        )
    )
    assert "--pull=never" in args
    assert "--network=none" in args
    assert "--read-only" in args
    assert "--cap-drop=ALL" in args
    assert ("--security-opt", "no-new-privileges") == (
        args[args.index("--security-opt")],
        args[args.index("--security-opt") + 1],
    )
    assert ("--user", "65534:65534") == (
        args[args.index("--user")], args[args.index("--user") + 1]
    )
    assert "--memory=512m" in args
    assert "--memory-swap=512m" in args
    assert "--cpus=1" in args
    assert "--pids-limit=64" in args
    assert "--log-driver=none" in args
    tmpfs = args[args.index("--tmpfs") + 1]
    assert tmpfs == "/tmp:rw,exec,nosuid,nodev,size=64m"
    assert ("--platform", "linux/amd64") == (
        args[args.index("--platform")], args[args.index("--platform") + 1]
    )
    mounts = [args[i + 1] for i, arg in enumerate(args[:-1]) if arg == "--volume"]
    assert mounts and all(mount.endswith(":ro") for mount in mounts)
    assert ("--entrypoint", "/app/bot") == (
        args[args.index("--entrypoint")], args[args.index("--entrypoint") + 1]
    )
    assert args[-1] == "debian:bookworm-slim"


def test_linux_image_gate_pulls_once_across_worker_event_loops(monkeypatch):
    """两个上传 worker/事件循环共享一次镜像准备，且不依赖 asyncio 锁。"""
    import bzplat.backend.runtime.binary_runner as runtime_mod

    calls: list[tuple[str, ...]] = []
    inspect_count = 0

    def fake_run(args, **_kwargs):
        nonlocal inspect_count
        argv = tuple(args)
        calls.append(argv)
        payload = _docker_payload(argv)
        if payload[:2] == ("image", "inspect"):
            inspect_count += 1
            if inspect_count == 1:
                return subprocess.CompletedProcess(args, 1, "", "missing")
            return subprocess.CompletedProcess(args, 0, "linux/amd64\n", "")
        assert payload[:3] == ("pull", "--platform", "linux/amd64")
        time.sleep(0.02)
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)
    _allow_fake_docker_binary(monkeypatch)
    image = "test-registry.invalid/bot-image:pull-once"
    first = BinaryRunner(
        docker_bin="docker-image-gate-test",
        prefer_local=False,
        linux_image=image,
    )
    second = BinaryRunner(
        docker_bin="docker-image-gate-test",
        prefer_local=False,
        linux_image=image,
    )

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(lambda r=runner: asyncio.run(r._ensure_linux_image_ready()))
            for runner in (first, second)
        ]
        for future in futures:
            future.result(timeout=2)
    asyncio.run(first._ensure_linux_image_ready())
    assert sum(_docker_payload(argv)[0] == "pull" for argv in calls) == 1
    assert sum(
        _docker_payload(argv)[:2] == ("image", "inspect") for argv in calls
    ) == 2


def test_cached_linux_amd64_image_never_pulls(monkeypatch):
    import bzplat.backend.runtime.binary_runner as runtime_mod

    calls: list[tuple[str, ...]] = []

    def fake_run(args, **_kwargs):
        argv = tuple(args)
        calls.append(argv)
        assert _docker_payload(argv)[:2] == ("image", "inspect")
        return subprocess.CompletedProcess(args, 0, "linux/amd64\n", "")

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)
    _allow_fake_docker_binary(monkeypatch)
    runner = BinaryRunner(
        docker_bin="docker-cached-image-test",
        prefer_local=False,
        linux_image="test.invalid/cached-linux-amd64:latest",
    )
    asyncio.run(runner._ensure_linux_image_ready())
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("pull_effect", "message"),
    [
        (subprocess.CompletedProcess([], 1, "", "registry EOF"), "镜像拉取失败"),
        (subprocess.TimeoutExpired(["docker", "pull"], 0.01), "镜像拉取超时"),
    ],
    ids=["registry-failure", "pull-timeout"],
)
def test_linux_image_prepare_failure_is_platform_fault_before_container_start(
    monkeypatch, pull_effect, message, caplog
):
    """registry/镜像故障不得算 Bot 慢，也不得创建物理 session。"""
    import bzplat.backend.runtime.binary_runner as runtime_mod

    docker_starts = 0

    def fake_run(args, **_kwargs):
        if _docker_payload(args)[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 1, "", "missing")
        if isinstance(pull_effect, BaseException):
            raise pull_effect
        return subprocess.CompletedProcess(
            args,
            pull_effect.returncode,
            pull_effect.stdout,
            pull_effect.stderr,
        )

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)
    _allow_fake_docker_binary(monkeypatch)
    runner = BinaryRunner(
        docker_bin=f"docker-image-failure-{message}",
        prefer_local=False,
        linux_image=f"test.invalid/{message}:latest",
        image_prepare_timeout=0.01,
    )
    runner._docker_ok = True

    async def should_not_start(_session):
        nonlocal docker_starts
        docker_starts += 1

    monkeypatch.setattr(runner, "_start_docker", should_not_start)
    with caplog.at_level("ERROR"), pytest.raises(PlatformRunnerError, match=message):
        asyncio.run(runner.start_session(ELF, runtime_mode="traditional"))
    assert docker_starts == 0
    assert runner._sessions == {}
    assert f"image=test.invalid/{message}:latest" in caplog.text
    if not isinstance(pull_effect, BaseException):
        assert "registry EOF" in caplog.text


@pytest.mark.parametrize(
    ("inspect_effect", "message"),
    [
        (
            subprocess.TimeoutExpired(["docker", "image", "inspect"], 0.01),
            "镜像检查超时",
        ),
        (OSError("docker daemon unavailable"), "无法执行 Docker 镜像命令"),
    ],
    ids=["inspect-timeout", "inspect-spawn-failure"],
)
def test_linux_image_initial_inspect_failure_never_starts_container(
    monkeypatch, inspect_effect, message
):
    """初次 inspect 自身失败也是平台故障，不能进入 pull/run 或 Bot 计时。"""
    import bzplat.backend.runtime.binary_runner as runtime_mod

    calls: list[tuple[str, ...]] = []

    def fake_run(args, **_kwargs):
        calls.append(tuple(args))
        raise inspect_effect

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)
    _allow_fake_docker_binary(monkeypatch)
    runner = BinaryRunner(
        docker_bin=f"docker-initial-inspect-{message}",
        prefer_local=False,
        linux_image=f"test.invalid/initial-inspect-{message}:latest",
        image_prepare_timeout=0.01,
    )
    runner._docker_ok = True
    docker_starts = 0

    async def should_not_start(_session):
        nonlocal docker_starts
        docker_starts += 1

    monkeypatch.setattr(runner, "_start_docker", should_not_start)
    with pytest.raises(PlatformRunnerError, match=message):
        asyncio.run(runner.start_session(ELF, runtime_mode="traditional"))
    assert calls and all(
        _docker_payload(argv)[:2] == ("image", "inspect") for argv in calls
    )
    assert docker_starts == 0
    assert runner._sessions == {}


def test_linux_image_final_inspect_failure_never_starts_container(
    monkeypatch, caplog
):
    """pull 成功仍须二次 inspect 成功，不能凭 pull exit=0 直接运行。"""
    import bzplat.backend.runtime.binary_runner as runtime_mod

    inspect_count = 0
    docker_starts = 0

    def fake_run(args, **_kwargs):
        nonlocal inspect_count
        if _docker_payload(args)[:2] == ("image", "inspect"):
            inspect_count += 1
            return subprocess.CompletedProcess(
                args,
                1,
                "",
                "missing" if inspect_count == 1 else "manifest EOF",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)
    _allow_fake_docker_binary(monkeypatch)
    runner = BinaryRunner(
        docker_bin="docker-final-inspect-failure",
        prefer_local=False,
        linux_image="test.invalid/final-inspect-failure:latest",
    )
    runner._docker_ok = True

    async def should_not_start(_session):
        nonlocal docker_starts
        docker_starts += 1

    monkeypatch.setattr(runner, "_start_docker", should_not_start)
    with caplog.at_level("ERROR"), pytest.raises(
        PlatformRunnerError, match="镜像不可用"
    ):
        asyncio.run(runner.start_session(ELF, runtime_mode="traditional"))
    assert inspect_count == 2
    assert "manifest EOF" in caplog.text
    assert docker_starts == 0
    assert runner._sessions == {}


def test_linux_image_wrong_arch_after_pull_is_platform_fault(monkeypatch, caplog):
    import bzplat.backend.runtime.binary_runner as runtime_mod

    def fake_run(args, **_kwargs):
        if _docker_payload(args)[:2] == ("image", "inspect"):
            return subprocess.CompletedProcess(args, 0, "linux/arm64\n", "")
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(runtime_mod.subprocess, "run", fake_run)
    _allow_fake_docker_binary(monkeypatch)
    runner = BinaryRunner(
        docker_bin="docker-wrong-arch-test",
        prefer_local=False,
        linux_image="test.invalid/wrong-arch:latest",
    )
    with caplog.at_level("ERROR"), pytest.raises(
        PlatformRunnerError, match="镜像架构不符"
    ):
        asyncio.run(runner._ensure_linux_image_ready())
    assert "platform=linux/arm64" in caplog.text


def test_preflight_image_prepare_precedes_eight_second_bot_timeout():
    """镜像准备结束后才调用 send；上传健康检查仍获得完整 8 秒。"""
    from bzplat.backend.games import preflight_bot

    class OrderedRunner(BinaryRunner):
        def __init__(self):
            super().__init__(prefer_local=False)
            self._docker_ok = True
            self.events: list[str] = []
            self.send_timeouts: list[float] = []

        async def _ensure_linux_image_ready(self):
            self.events.append("image-prepare")
            await asyncio.sleep(0.01)

        async def _start_docker(self, _session):
            self.events.append("container-start")

        async def send(self, _session_id, _line, *, timeout):
            self.events.append("bot-send")
            self.send_timeouts.append(timeout)
            return '{"response":{"x":0,"y":1}}'

    runner = OrderedRunner()
    ok, detail = asyncio.run(
        preflight_bot(
            "pencil",
            str(SAMPLES / "pencilbot_linux_amd64"),
            runner,
            runtime_mode="traditional",
        )
    )
    assert ok, detail
    assert runner.events == ["image-prepare", "container-start", "bot-send"]
    assert runner.send_timeouts == [8.0]


def test_traditional_logical_session_prepares_image_before_game_clock(monkeypatch):
    """正式 Traditional 会话建成前先完成镜像准备，棋钟尚未启动。"""
    runner = BinaryRunner(prefer_local=False)
    runner._docker_ok = True
    image_checks = 0

    async def image_ready():
        nonlocal image_checks
        image_checks += 1

    monkeypatch.setattr(runner, "_ensure_linux_image_ready", image_ready)
    sid = asyncio.run(runner.prepare_session(ELF, runtime_mode="traditional"))
    assert image_checks == 1
    asyncio.run(runner.stop_session(sid))


def test_local_test_mode_never_checks_or_pulls_docker_image(monkeypatch):
    runner = BinaryRunner(prefer_local=True)
    image_checks = 0

    async def forbidden_image_check():
        nonlocal image_checks
        image_checks += 1

    async def fake_local_start(_session):
        return None

    monkeypatch.setattr(runner, "_ensure_linux_image_ready", forbidden_image_check)
    monkeypatch.setattr(runner, "_start_local", fake_local_start)
    sid = asyncio.run(runner.start_session(ELF, runtime_mode="traditional"))
    assert image_checks == 0
    asyncio.run(runner.stop_session(sid))


def test_linux_image_is_code_owned_and_ignores_environment(monkeypatch):
    """生产镜像不是环境可切换的执行边界；显式测试参数仍可覆盖。"""
    monkeypatch.setenv("BZ_LINUX_BOT_IMAGE", "registry.example/bots/linux:v2")
    runner = BinaryRunner(prefer_local=True)
    assert runner._linux_image == "debian:bookworm-slim"
    explicit = BinaryRunner(prefer_local=True, linux_image="test.invalid/fixture:v1")
    assert explicit._linux_image == "test.invalid/fixture:v1"


@pytest.mark.parametrize(
    "payload",
    [_valid_pe(), b"#!/usr/bin/env python3\nprint(1)\n", _mutated_sample_elf(machine=0xB7)],
    ids=["pe", "script", "arm64-elf"],
)
def test_binary_runner_reclassifies_file_and_never_local_fallback(tmp_path, payload):
    path = tmp_path / "legacy-bot"
    path.write_bytes(payload)
    runner = BinaryRunner(docker_bin="definitely-no-such-docker", prefer_local=True)

    # A forged historical/caller value must not override actual file bytes.
    forged = BinaryInfo("elf", "linux", "amd64", True)
    with pytest.raises(BinaryRejectError, match="仅支持 Linux x86_64 ELF"):
        asyncio.run(runner.start_session(path, info=forged))
    assert runner._sessions == {}


@pytest.mark.parametrize("payload", [_valid_pe(), b"print('python')\n"], ids=["pe", "script"])
def test_bot_upload_rejects_non_elf_before_creating_user_data(tmp_path, payload):
    from bzplat.backend.bots.manager import BotError, BotManager
    from bzplat.backend.store import Store

    store = Store(str(tmp_path / "upload.db"))
    owner = store.create_user("elfowner", "elfowner@example.com", "hash")
    manager = BotManager(store, upload_root=tmp_path / "uploads")

    with pytest.raises(BotError, match="仅支持 Linux x86_64 ELF") as failure:
        manager.create_from_upload(owner["id"], "badbot", payload)
    assert failure.value.code == "unsupported_binary"
    assert store.get_bot_by_owner_name(owner["id"], "badbot") is None
    assert list((tmp_path / "uploads").iterdir()) == []


@pytest.mark.parametrize("game_id", ["holdem", "gomoku", "pencil"])
@pytest.mark.parametrize("runtime_mode", ["traditional", "longrunning"])
def test_preflight_rejects_non_elf_identically_for_every_game_and_mode(
    tmp_path, game_id, runtime_mode
):
    from bzplat.backend.games import preflight_bot

    path = tmp_path / "windows.exe"
    path.write_bytes(_valid_pe())
    ok, detail = asyncio.run(
        preflight_bot(
            game_id,
            str(path),
            BinaryRunner(prefer_local=True),
            runtime_mode=runtime_mode,
        )
    )
    assert not ok
    assert "仅支持 Linux x86_64 ELF" in detail


def test_windows_pe_is_rejected_before_any_docker_start(tmp_path):
    """PE 的文件头闸门必须早于 Docker；不得再触发 Wine/镜像拉取。"""
    from bzplat.backend.games import preflight_bot

    class SpyRunner(BinaryRunner):
        def __init__(self) -> None:
            super().__init__(prefer_local=False)
            self.image_checks = 0
            self.docker_starts = 0

        async def _ensure_linux_image_ready(self):
            self.image_checks += 1
            raise AssertionError("PE 不应进入 Docker 镜像检查路径")

        async def _start_docker(self, session):
            self.docker_starts += 1
            raise AssertionError("PE 不应进入 Docker 启动路径")

    path = tmp_path / "windows-x64.exe"
    path.write_bytes(_valid_pe())
    runner = SpyRunner()

    ok, detail = asyncio.run(
        preflight_bot(
            "pencil",
            str(path),
            runner,
            runtime_mode="traditional",
        )
    )

    assert not ok
    assert "Windows PE 不受支持" in detail
    assert runner.image_checks == 0
    assert runner.docker_starts == 0
    assert runner._sessions == {}


def test_fresh_store_accepts_only_current_binary_metadata(tmp_path):
    from bzplat.backend.store import Store

    store = Store(str(tmp_path / "fresh.db"))
    owner = store.create_user("freshfmt", "freshfmt@example.com", "hash")
    bot = store.create_bot(owner["id"], "goodbot")
    assert (bot["format"], bot["os"], bot["arch"]) == ("elf", "linux", "amd64")
    with pytest.raises(ValueError, match="仅支持 Linux x86_64 ELF"):
        store.create_bot(
            owner["id"], "legacybot", format="pe", os="windows", arch="amd64"
        )
    with pytest.raises(ValueError, match="仅支持 Linux x86_64 ELF"):
        store.add_bot_version(
            bot["id"],
            binary_path="legacy.exe",
            format="pe",
            os="windows",
            arch="amd64",
        )
    assert store.list_bot_versions(bot["id"]) == []


def test_historical_pe_version_remains_readable_but_cannot_activate_or_execute(tmp_path):
    """Opening a legacy DB neither rewrites nor deletes PE rows; all run paths reject it."""
    from bzplat.backend.bots.manager import BotError, BotManager
    from bzplat.backend.store import Store

    db_path = tmp_path / "legacy.db"
    pe_path = tmp_path / "legacy.exe"
    pe_path.write_bytes(_valid_pe())
    store = Store(str(db_path))
    owner = store.create_user("legacyfmt", "legacyfmt@example.com", "hash")
    bot = store.create_bot(owner["id"], "legacybot", binary_path=str(ELF))
    store.add_bot_version(bot["id"], binary_path=str(ELF), version=1)
    store.add_bot_version(bot["id"], binary_path=str(pe_path), version=2)
    store.set_current_version(bot["id"], 1)

    # Simulate a row written by the historical permissive schema. This is test DB
    # data only; production migrations intentionally do not perform such a write.
    store._conn.execute("PRAGMA ignore_check_constraints=ON")
    store._conn.execute(
        "UPDATE bot_versions SET format='pe', os='windows', arch='amd64' "
        "WHERE bot_id=? AND version=2",
        (bot["id"],),
    )
    store._conn.execute("PRAGMA ignore_check_constraints=OFF")
    store._conn.commit()
    store.close()

    reopened = Store(str(db_path))
    legacy = next(v for v in reopened.list_bot_versions(bot["id"]) if v["version"] == 2)
    assert (legacy["format"], legacy["os"], legacy["arch"]) == (
        "pe", "windows", "amd64"
    )
    manager = BotManager(reopened, upload_root=tmp_path / "legacy-uploads")
    with pytest.raises(BotError, match="仅支持 Linux x86_64 ELF") as failure:
        manager.activate_version(bot["id"], owner["id"], 2)
    assert failure.value.code == "unsupported_binary"
    assert reopened.get_bot(bot["id"])["current_version"] == 1
    assert next(v for v in reopened.list_bot_versions(bot["id"]) if v["version"] == 2)[
        "format"
    ] == "pe"

    with pytest.raises(BinaryRejectError, match="仅支持 Linux x86_64 ELF"):
        asyncio.run(BinaryRunner(prefer_local=True).start_session(legacy["binary_path"]))

    # Simulate a historical database whose active mirror still points at PE.
    reopened._conn.execute("PRAGMA ignore_check_constraints=ON")
    reopened._conn.execute(
        "UPDATE bots SET current_version=2, binary_path=?, format='pe', "
        "os='windows', arch='amd64' WHERE id=?",
        (str(pe_path), bot["id"]),
    )
    reopened._conn.execute("PRAGMA ignore_check_constraints=OFF")
    reopened._conn.commit()

    from bzplat.backend.contests.manager import ContestManager
    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    orchestrator = MatchOrchestrator(reopened, max_concurrent=1)
    before_matches = reopened._conn.execute(
        "SELECT COUNT(*) AS n FROM matches_index"
    ).fetchone()["n"]
    with pytest.raises(ValueError, match="unsupported_binary"):
        asyncio.run(
            orchestrator.challenge(
                bot["id"], bot["id"], owner["id"], game_id="holdem"
            )
        )
    with pytest.raises(ValueError, match="unsupported_binary"):
        asyncio.run(
            orchestrator.challenge_human(
                bot["id"], owner["id"], game_id="holdem"
            )
        )
    # Contest publish/version-freeze uses the same pre-match metadata gate; dispatch
    # also rechecks through orchestrator.challenge with the frozen version id.
    with pytest.raises(ValueError, match="unsupported_binary"):
        ContestManager(reopened, orchestrator)._version_snapshot(bot["id"], None)
    after_matches = reopened._conn.execute(
        "SELECT COUNT(*) AS n FROM matches_index"
    ).fetchone()["n"]
    assert after_matches == before_matches
    assert reopened.get_bot(bot["id"])["format"] == "pe"


def test_broken_stdin_uses_same_docker_exit_classification(tmp_path):
    """docker can exit before stdout is read; BrokenPipe must not be swallowed."""

    class BrokenStdin:
        def write(self, _data):
            return None

        async def drain(self):
            raise BrokenPipeError("connection lost")

    class UnusedStdout:
        async def readline(self):
            raise AssertionError("stdout must not be read after broken stdin")

    class FakeProc:
        def __init__(self, returncode):
            self.returncode = returncode
            self.stdin = BrokenStdin()
            self.stdout = UnusedStdout()

        async def wait(self):
            return self.returncode

    path = tmp_path / "bot"
    path.write_bytes(b"unused")
    info = BinaryInfo("elf", "linux", "amd64", True)

    infra = BinaryRunner(prefer_local=False)
    infra._sessions["infra-pipe"] = BotSession(
        "infra-pipe", info, path, proc=FakeProc(125), mode="docker"
    )
    with pytest.raises(PlatformRunnerError, match="docker exit 125"):
        asyncio.run(infra.send("infra-pipe", "{}"))

    bot_fault = BinaryRunner(prefer_local=False)
    bot_fault._sessions["bot-pipe"] = BotSession(
        "bot-pipe", info, path, proc=FakeProc(126), mode="docker"
    )
    with pytest.raises(BotCrashedError, match="stdin"):
        asyncio.run(bot_fault.send("bot-pipe", "{}"))


def test_docker_spawn_oserror_is_sanitized_platform_fault(monkeypatch):
    if not ELF.is_file():
        pytest.skip("sample ELF missing")
    runner = BinaryRunner(prefer_local=False)
    runner._docker_ok = True

    async def fail_spawn(_session):
        raise OSError("/private/server/path: too many open files")

    async def image_ready():
        return None

    monkeypatch.setattr(runner, "_ensure_linux_image_ready", image_ready)
    monkeypatch.setattr(runner, "_start_docker", fail_spawn)
    with pytest.raises(PlatformRunnerError) as failure:
        asyncio.run(runner.start_session(ELF))
    assert "OSError" in str(failure.value)
    assert "/private/server/path" not in str(failure.value)


def test_match_runner_does_not_swallow_platform_fault_as_default_move():
    class FailingTransport:
        def __init__(self):
            self._sessions = {}
            self._next = 0

        async def start_session(self, binary_path, *, runtime_mode="longrunning", **_kwargs):
            from types import SimpleNamespace

            self._next += 1
            sid = f"s{self._next}"
            self._sessions[sid] = SimpleNamespace(
                binary_path=Path(binary_path),
                runtime_mode=runtime_mode,
                requests=[],
                responses=[],
                turn=0,
                long_running=False,
            )
            return sid

        async def prepare_session(self, binary_path, *, runtime_mode):
            return await self.start_session(binary_path, runtime_mode=runtime_mode)

        async def send(self, *_args, **_kwargs):
            raise PlatformRunnerError("docker daemon unavailable")

        async def read_extra_line(self, *_args, **_kwargs):
            return None

        async def stop_session(self, session_id):
            self._sessions.pop(session_id, None)

    runner = MatchRunner(FailingTransport())
    with pytest.raises(PlatformRunnerError, match="daemon unavailable"):
        asyncio.run(runner.run_binaries("/bot/a", "/bot/b", game_id="gomoku"))


def test_match_two_callbots_short():
    if not ELF.is_file():
        pytest.skip("sample ELF missing")
    runner = MatchRunner(BinaryRunner(prefer_local=True))
    # game_id 必须显式选择；手数由该游戏唯一固定规则决定。
    result = asyncio.run(
        runner.run_binaries(str(ELF), str(ELF), game_id="holdem", seed=1)
    )
    from bzplat.backend.games.holdem.engine import DEFAULT_HANDS
    assert result.rounds_played == DEFAULT_HANDS
    # Botzone 计分：final_chips = 累计净输赢 net（零和），不再守恒于 2*STARTING_STACK
    assert sum(result.final_chips) == 0


def test_protocol_roundtrip():
    from bzplat.backend.games.holdem.holdem_judge import Card, Suit

    req = proto.build_act_request(
        hand=0,
        total_hands=70,
        my_id=0,
        dealer_id=0,
        my_cards=[Card(Suit.SPADE, 14), Card(Suit.HEART, 13)],
        board=[],
        history=[],
        my_chips=20000,
    )
    line = proto.dumps_request(req)
    # Botzone 全名字段
    assert '"num_players":2' in line
    assert '"dealer_id":0' in line
    assert '"my_cards":[' in line
    # 唯一 response 信封；裸整数不再是通信输入。
    action, _x = proto.parse_response({"response": 0})
    assert action == "call"
    with pytest.raises(ValueError):
        proto.parse_response(0)


def test_orchestrator_resolves_holdem_winner_non_null():
    """回归：holdem 多手对局 winner 不得被 match_end.winner=None 覆盖成平局。

    场景：foldbot vs callbot（foldbot 每手弃牌输盲注，70 手后 callbot 净筹码远高）。
    orchestrator 应按 ea/eb 判 winner=1（callbot 胜），而非 None（平局）。
    根因修复（L298-303）：仅当 match_end.winner 非 None 才覆盖兜底判定的 winner。
    """
    import os
    os.environ.setdefault("BZ_BOT_LOCAL", "1")
    foldbot = SAMPLES / "holdem_bots" / "foldbot"
    callbot = ELF
    if not foldbot.is_file() or not callbot.is_file():
        pytest.skip("foldbot/callbot binary missing")

    from bzplat.backend.matches.orchestrator import MatchOrchestrator
    from bzplat.backend.store import Store
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        store = Store(str(td + "/w.db"))
        u = store.create_user("wintest", "w@e.com", "x")
        ba = store.create_bot(u["id"], "foldbotA", binary_path=str(foldbot), format="elf", game_id="holdem")
        bb = store.create_bot(u["id"], "callbotB", binary_path=str(callbot), format="elf", game_id="holdem")
        store.ensure_rating(ba["id"]); store.ensure_rating(bb["id"])
        orch = MatchOrchestrator(store, runner=MatchRunner(BinaryRunner(prefer_local=True)), max_concurrent=1)
        import asyncio

        async def _run():
            store.executions.resume()
            request_id = await orch.challenge(
                ba["id"], bb["id"], u["id"], game_id="holdem"
            )
            job = store.executions.claim_next(
                max_match_slots=1,
                max_sandbox_units=2,
                aging_seconds=60,
                user_active_limit=1,
                contest_share_slots=1,
            )
            assert job is not None and job["public_id"] == request_id
            mid = str(job["current_match_id"])
            orch.start_execution_job(job)
            task = orch._tasks.get(mid)
            if task:
                await asyncio.wait_for(task, timeout=60)
            store.executions.finalize_ready()
            return mid

        mid = asyncio.run(_run())
        m = store.get_match(mid)
        # foldbot 每手弃 → callbot 净筹码高 → winner 应是 1（非 None 平局）
        assert m["winner"] is not None, (
            f"holdem 多手 winner 不应是 None（平局）；result={m.get('result')}"
        )
        assert m["winner"] == 1, f"callbot 应胜（foldbot 每手弃），winner={m['winner']}"
        store.close()


def test_orchestrator_has_no_external_match_config_argument():
    """内部编排 API 不再保留会静默忽略的旧规则入口。"""
    import inspect

    from bzplat.backend.matches.orchestrator import MatchOrchestrator

    assert "match_config" not in inspect.signature(MatchOrchestrator.challenge).parameters
    assert "match_config" not in inspect.signature(MatchOrchestrator.challenge_human).parameters
