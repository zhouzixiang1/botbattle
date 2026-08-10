"""Linux x86_64 ELF Bot 沙箱运行器（Docker；测试可显式本机执行）。"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import shutil
import socket
import struct
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from bzplat.backend.runtime.config import ACTION_TIMEOUT_SEC
from bzplat.backend.runtime.limits import MAX_BOT_RESPONSE_LINE_BYTES

from ..bots.classify import (
    BinaryInfo,
    BinaryRejectError,
    classify_binary,
    require_supported_binary,
)
from ..store.schema import (
    DEFAULT_RUNTIME_MODE,
    SUPPORTED_BINARY_ERROR,
    VALID_RUNTIME_MODES,
)

logger = logging.getLogger(__name__)

DEFAULT_ACTION_TIMEOUT = ACTION_TIMEOUT_SEC
DEFAULT_MEMORY = "512m"
DEFAULT_CPUS = "1"
DEFAULT_LINUX_IMAGE = "debian:bookworm-slim"
DEFAULT_IMAGE_PREPARE_TIMEOUT = 300.0
_DOCKER_INSPECT_TIMEOUT_SEC = 15.0
_STDERR_DRAIN_GRACE_SEC = 0.5
_AUTO_EXECUTION_LABEL = "bzplat.auto_execution"
_AUTO_EXECUTION_LAUNCH_LABEL = "bzplat.auto_execution_launch"
_AUTO_EXECUTION_CLEANUP_POLLS = 6
_AUTO_EXECUTION_CLEANUP_ZERO_POLLS = 2
_AUTO_EXECUTION_VISIBILITY_POLL_SEC = 0.05
_AUTO_EXECUTION_CONFIRM_TIMEOUT_SEC = 10.0
_IMAGE_READY_LOCK = threading.Lock()
_IMAGE_READY_KEYS: set[tuple[str, str]] = set()


def _host_boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="utf-8"
        ).strip()
    except OSError:
        value = ""
    return value or "unknown"


def _docker_daemon_incarnation() -> str:
    """Identify the local daemon without relying on Docker API availability."""
    boot_id = _host_boot_id()
    daemon = "unknown"
    docker_host = os.environ.get("DOCKER_HOST", "").strip()
    socket_path: str | None
    if not docker_host:
        socket_path = "/var/run/docker.sock"
    elif docker_host.startswith("unix://"):
        socket_path = docker_host.removeprefix("unix://")
    else:
        socket_path = None
    if socket_path:
        peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            peer.settimeout(0.5)
            peer.connect(socket_path)
            raw = peer.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i")
            )
            pid, _uid, _gid = struct.unpack("3i", raw)
            stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
            # After the final ') ' the first token is field 3; starttime is 22.
            tail = stat.rsplit(") ", 1)[1].split()
            daemon = f"{pid}:{tail[19]}"
        except (OSError, IndexError, ValueError):
            daemon = "unknown"
        finally:
            peer.close()
    return f"boot:{boot_id};daemon:{daemon}"


@dataclass(frozen=True)
class ExecutionScope:
    """Cross-process auto-match execution identity and spawn fence.

    The cross-process file lock spans the final Store fence check and actual
    subprocess spawn.  A takeover first changes epoch and then takes the same
    lock before cleanup, closing the otherwise possible check/spawn race without
    holding SQLite's synchronous lock across an ``await``.
    """

    token: str
    launch_lock_path: str
    fence_check: Callable[[], None]
    recovery_mark: Callable[[str], None] | None = None
    launch_mark: Callable[[str, str, str | None], None] | None = None
    incarnation_mark: Callable[[str, str], None] | None = None
    cleanup_mark: Callable[[], None] | None = None

    def assert_current(self) -> None:
        self.fence_check()

    def mark_recovery_pending(self, reason: str) -> None:
        if self.recovery_mark is not None:
            self.recovery_mark(reason)

    def mark_launch_state(
        self,
        state: str,
        launch_token: str,
        daemon_incarnation: str | None = None,
    ) -> None:
        if self.launch_mark is not None:
            self.launch_mark(state, launch_token, daemon_incarnation)

    def mark_cleanup_confirmed(self) -> None:
        if self.cleanup_mark is not None:
            self.cleanup_mark()

    def mark_ambiguous_incarnation(
        self, launch_token: str, daemon_incarnation: str
    ) -> None:
        if self.incarnation_mark is not None:
            self.incarnation_mark(launch_token, daemon_incarnation)

    @asynccontextmanager
    async def launch_guard(self) -> AsyncIterator[None]:
        async with _execution_file_lock(self.launch_lock_path):
            # This is the only check inside the launch critical section.  It is
            # a short Store transaction and has completed before spawn awaits.
            self.fence_check()
            yield


@asynccontextmanager
async def _execution_file_lock(path: str) -> AsyncIterator[None]:
    """Cancellation-safe async wrapper around the per-database ``flock``."""

    def acquire() -> int:
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        return fd

    acquire_task = asyncio.create_task(asyncio.to_thread(acquire))
    cancellation: asyncio.CancelledError | None = None
    try:
        while True:
            try:
                fd = await asyncio.shield(acquire_task)
                break
            except asyncio.CancelledError as exc:
                # Repeated shutdown cancellation still must not detach the
                # blocking flock thread and leak a subsequently acquired lock.
                cancellation = cancellation or exc
                continue
        if cancellation is not None:
            # The caller was cancelled before entering the critical section.
            # The exception path releases it after acquisition is certain.
            raise cancellation
    except BaseException:
        if acquire_task.done() and not acquire_task.cancelled():
            # ``result`` either raises the worker error or returns the fd; only
            # the latter needs cleanup here.
            try:
                acquired_fd = acquire_task.result()
            except BaseException:
                pass
            else:
                fcntl.flock(acquired_fd, fcntl.LOCK_UN)
                os.close(acquired_fd)
        raise
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@dataclass
class BotSession:
    session_id: str
    info: BinaryInfo
    binary_path: Path
    proc: asyncio.subprocess.Process | None = None
    mode: str = "docker"  # docker | local（local 仅 BZ_BOT_LOCAL 测试开关）
    container_name: str = ""
    _buf: bytes = field(default_factory=bytes)
    _stderr_tail: bytearray = field(default_factory=bytearray)  # bot stderr 末尾（排查崩溃用）
    _stderr_task: asyncio.Task | None = None
    # ── Botzone 协议会话状态（传输层维护，runner 读写）──
    runtime_mode: str = DEFAULT_RUNTIME_MODE
    requests: list = field(default_factory=list)   # 累积下发的请求负载（Traditional 重放用）
    responses: list = field(default_factory=list)  # 累积 Bot 响应负载（Traditional 信封 responses[]）
    turn: int = 0                                  # 已完成的回合数（0=首回合尚未握手判定）
    long_running: bool = False  # LongRunning Bot 首回合握手后置 True（之后发单 request 信封）
    execution_scope: ExecutionScope | None = None
    def start_stderr_drain(self) -> None:
        """异步读取 bot stderr 到尾部缓冲（保留末尾 4KB，排查崩溃）。"""
        proc = self.proc
        if proc is None or proc.stderr is None:
            return

        async def _drain() -> None:
            try:
                while True:
                    chunk = await proc.stderr.read(1024)
                    if not chunk:
                        break
                    self._stderr_tail.extend(chunk)
                    # 仅保留末尾 4KB
                    if len(self._stderr_tail) > 4096:
                        del self._stderr_tail[: len(self._stderr_tail) - 4096]
            except Exception:
                pass

        try:
            self._stderr_task = asyncio.create_task(_drain(), name=f"stderr-{self.session_id}")
        except RuntimeError:
            pass

    def stderr_tail(self) -> str:
        return self._stderr_tail.decode("utf-8", errors="replace").strip()


class BotCrashedError(RuntimeError):
    """Bot 进程崩溃（启动即退出/EOF），不可恢复。区别于普通的决策超时/格式错误——
    决策超时是「Bot 慢」，崩溃是「Bot 死了」；两者都必须立即收敛为明确终局，
    不能吞成默认动作继续。具体 completed/aborted 语义由引擎与编排层按阶段决定。"""

    def __init__(self, *args: object, crashed_seat: int | None = None) -> None:
        super().__init__(*args)
        # 崩溃方座位号（0=bot_a, 1=bot_b）；None=未知（如 start_session 阶段未注解）。
        # 由 runner 在 start_session 失败时注解，供 orchestrator 判技术判负的胜方。
        self.crashed_seat = crashed_seat


class BotResponseLineTooLargeError(RuntimeError):
    """Bot stdout 在传输层超过单行硬顶；runner 将其归责为协议故障。"""


class PlatformRunnerError(RuntimeError):
    """Sandbox/container infrastructure failed before the Bot could be judged.

    This must never be converted into a Bot technical loss: Docker daemon/image/
    invocation failures are platform faults and therefore abort without rating.
    """


def _docker_control_command(
    args: list[str],
    *,
    timeout: float,
    timeout_message: str,
) -> subprocess.CompletedProcess[str]:
    """Run one Docker control-plane command outside the Bot decision clock.

    The caller executes this synchronous helper through ``asyncio.to_thread``.
    Raw registry/daemon stderr is intentionally not returned in public errors.
    """
    try:
        return subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error(
            "docker image control timeout phase=%s image=%s timeout=%ss",
            " ".join(args[1:3]),
            args[-1] if args else "unknown",
            timeout,
        )
        raise PlatformRunnerError(timeout_message) from exc
    except OSError as exc:
        logger.error(
            "docker image control spawn failed phase=%s image=%s error=%s",
            " ".join(args[1:3]),
            args[-1] if args else "unknown",
            type(exc).__name__,
        )
        raise PlatformRunnerError(
            f"无法执行 Docker 镜像命令（{type(exc).__name__}）"
        ) from exc
    except subprocess.SubprocessError as exc:
        logger.error(
            "docker image control failed phase=%s image=%s error=%s",
            " ".join(args[1:3]),
            args[-1] if args else "unknown",
            type(exc).__name__,
        )
        raise PlatformRunnerError(
            f"Docker 镜像命令失败（{type(exc).__name__}）"
        ) from exc


def _ensure_linux_image_ready_sync(
    docker_bin: str,
    image: str,
    *,
    prepare_timeout: float,
) -> None:
    """Ensure exactly one local linux/amd64 sandbox image per process.

    A module-level threading lock coordinates the orchestrator event loop and
    upload-preflight worker loops without binding an asyncio primitive to one
    loop.  The potentially blocking Docker calls always run in a worker thread.
    """
    key = (docker_bin, image)
    with _IMAGE_READY_LOCK:
        if key in _IMAGE_READY_KEYS:
            return

        inspect_args = [
            docker_bin,
            "image",
            "inspect",
            "--format",
            "{{.Os}}/{{.Architecture}}",
            image,
        ]
        inspect_timeout = min(_DOCKER_INSPECT_TIMEOUT_SEC, prepare_timeout)
        inspected = _docker_control_command(
            inspect_args,
            timeout=inspect_timeout,
            timeout_message="Linux Bot 沙箱镜像检查超时",
        )
        platform = inspected.stdout.strip() if inspected.returncode == 0 else ""
        if platform != "linux/amd64":
            logger.info(
                "linux bot image not ready; pulling image=%s inspect_exit=%s platform=%s",
                image,
                inspected.returncode,
                platform or "unknown",
            )
            pulled = _docker_control_command(
                [
                    docker_bin,
                    "pull",
                    "--platform",
                    "linux/amd64",
                    image,
                ],
                timeout=prepare_timeout,
                timeout_message="Linux Bot 沙箱镜像拉取超时",
            )
            if pulled.returncode != 0:
                logger.error(
                    "linux bot image pull failed image=%s exit=%s stderr=%s",
                    image,
                    pulled.returncode,
                    pulled.stderr[-1000:].strip().replace("\n", " | "),
                )
                raise PlatformRunnerError(
                    "Linux Bot 沙箱镜像拉取失败"
                    f"（docker pull exit {pulled.returncode}）"
                )
            inspected = _docker_control_command(
                inspect_args,
                timeout=inspect_timeout,
                timeout_message="Linux Bot 沙箱镜像检查超时",
            )
            if inspected.returncode != 0:
                logger.error(
                    "linux bot image inspect failed after pull image=%s exit=%s stderr=%s",
                    image,
                    inspected.returncode,
                    inspected.stderr[-1000:].strip().replace("\n", " | "),
                )
                raise PlatformRunnerError(
                    "Linux Bot 沙箱镜像不可用"
                    f"（docker image inspect exit {inspected.returncode}）"
                )
            platform = inspected.stdout.strip()

        if platform != "linux/amd64":
            logger.error(
                "linux bot image architecture mismatch image=%s platform=%s",
                image,
                platform or "unknown",
            )
            raise PlatformRunnerError(
                "Linux Bot 沙箱镜像架构不符（需要 linux/amd64）"
            )
        _IMAGE_READY_KEYS.add(key)
        logger.info("linux bot image ready image=%s platform=linux/amd64", image)


def _invalidate_linux_image_ready_sync(docker_bin: str, image: str) -> None:
    with _IMAGE_READY_LOCK:
        _IMAGE_READY_KEYS.discard((docker_bin, image))


class BotTechnicalError(RuntimeError):
    """A terminal, attributable Bot fault with safe structured diagnostics.

    Unlike :class:`PlatformRunnerError`, these failures are caused by the uploaded
    program and therefore produce a scored technical loss in Bot-vs-Bot matches.
    ``message`` must be safe for replay/result persistence: never include the raw
    stdout line, host paths, container arguments, or other private runtime data.
    """

    reason = "technical_loss"

    def __init__(
        self,
        message: str,
        *,
        error_code: str,
        failed_seat: int,
        turn: int,
        leg: int | None = None,
    ) -> None:
        super().__init__(message)
        if failed_seat not in (0, 1):
            raise ValueError(f"failed_seat 必须是 0/1，得到 {failed_seat!r}")
        self.error_code = error_code
        self.failed_seat = failed_seat
        # 1-based attempted decision number for operator/Bot-author diagnostics.
        self.turn = max(1, int(turn))
        self.leg = leg

    def incident(self) -> dict[str, int | str]:
        """Return the bounded, public-safe payload stored in replay/result JSON."""
        item: dict[str, int | str] = {
            "reason": self.reason,
            "code": self.error_code,
            "seat": self.failed_seat,
            "turn": self.turn,
            "error": str(self)[:200],
        }
        if self.leg is not None:
            item["leg"] = int(self.leg)
        return item


class BotProtocolError(BotTechnicalError):
    """Bot stdout did not satisfy the selected game's response contract."""

    reason = "protocol_error"


class BotDecisionTimeoutError(BotTechnicalError):
    """Bot did not produce one complete response line before its deadline."""

    reason = "timeout"


class BinaryRunner:
    """管理 bot 进程/容器的 stdin/stdout 行协议会话。"""

    def __init__(
        self,
        *,
        docker_bin: str = "docker",
        prefer_local: bool | None = None,
        linux_image: str | None = None,
        image_prepare_timeout: float = DEFAULT_IMAGE_PREPARE_TIMEOUT,
    ) -> None:
        self._docker_bin = docker_bin
        self._sessions: dict[str, BotSession] = {}
        # 测试环境可强制本机跑同架构 ELF
        if prefer_local is None:
            prefer_local = os.environ.get("BZ_BOT_LOCAL", "").lower() in ("1", "true", "yes")
        self._prefer_local = prefer_local
        self._docker_ok = shutil.which(docker_bin) is not None
        self._linux_image = (
            linux_image
            or os.environ.get("BZ_LINUX_BOT_IMAGE", "").strip()
            or DEFAULT_LINUX_IMAGE
        )
        self._image_prepare_timeout = max(0.001, float(image_prepare_timeout))

    @property
    def execution_backend(self) -> str:
        """Persisted backend used to decide whether takeover cleanup is provable."""
        return "local" if self._prefer_local else "docker"

    def _new_session(
        self,
        binary_path: str | Path,
        *,
        info: BinaryInfo | None,
        runtime_mode: str,
        execution_scope: ExecutionScope | None = None,
    ) -> BotSession:
        """校验二进制并创建逻辑会话；不启动进程。"""
        if execution_scope is not None:
            execution_scope.assert_current()
        if runtime_mode not in VALID_RUNTIME_MODES:
            raise ValueError(f"未知运行模式: {runtime_mode}")
        path = Path(binary_path).resolve()
        if not path.is_file():
            raise BotCrashedError(f"bot 二进制不存在: {path}")
        # 文件内容始终是权威真相：绝不信任数据库历史值或调用方传入的
        # ``BinaryInfo(runnable=True)``，避免 PE/脚本通过伪造元数据回退到 local。
        with path.open("rb") as binary:
            detected = require_supported_binary(classify_binary(binary.read(4096)))
        if info is not None:
            require_supported_binary(info)
            expected = (info.format, info.os, info.arch, info.runnable)
            actual = (detected.format, detected.os, detected.arch, detected.runnable)
            if expected != actual:
                raise BinaryRejectError(f"{SUPPORTED_BINARY_ERROR}；二进制元数据与文件不一致")
        info = detected

        sid = uuid.uuid4().hex[:12]
        mode = self._select_mode(info)
        session = BotSession(
            session_id=sid, info=info, binary_path=path, mode=mode,
            runtime_mode=runtime_mode, execution_scope=execution_scope,
        )
        return session

    async def prepare_session(
        self,
        binary_path: str | Path,
        *,
        info: BinaryInfo | None = None,
        runtime_mode: str = DEFAULT_RUNTIME_MODE,
        execution_scope: ExecutionScope | None = None,
    ) -> str:
        """只登记 Traditional 的历史状态，不启动整场闲置 Bot 进程。"""
        if runtime_mode != DEFAULT_RUNTIME_MODE:
            raise ValueError("prepare_session 只用于 Traditional 逻辑会话")
        session = self._new_session(
            binary_path,
            info=info,
            runtime_mode=runtime_mode,
            execution_scope=execution_scope,
        )
        if session.mode == "docker":
            # Traditional 的逻辑会话在游戏 Session/棋钟启动前建立；此处完成
            # 镜像准备，避免冷拉取时间计入 Pencil 的 900 秒累计棋钟。
            await self.ensure_runtime_ready()
        self._sessions[session.session_id] = session
        logger.debug(
            "bot protocol session prepared sid=%s path=%s",
            session.session_id,
            session.binary_path,
        )
        return session.session_id

    async def start_session(self, binary_path: str | Path, *,
                            info: BinaryInfo | None = None,
                            action_timeout: float = DEFAULT_ACTION_TIMEOUT,
                            runtime_mode: str = DEFAULT_RUNTIME_MODE,
                            execution_scope: ExecutionScope | None = None) -> str:
        session = self._new_session(
            binary_path,
            info=info,
            runtime_mode=runtime_mode,
            execution_scope=execution_scope,
        )
        sid = session.session_id
        mode = session.mode
        try:
            if mode == "docker":
                # 镜像 inspect/pull 属于平台准备阶段，必须先于 Bot 响应计时；
                # ``docker run --pull=never`` 再保证计时窗口内不会隐式拉镜像。
                await self.ensure_runtime_ready()
            guard = (
                execution_scope.launch_guard()
                if execution_scope is not None
                else contextlib.AsyncExitStack()
            )
            async with guard:
                if mode == "local":
                    if execution_scope is not None:
                        execution_scope.mark_launch_state(
                            "creating",
                            sid,
                            f"boot:{_host_boot_id()};daemon:local",
                        )
                    try:
                        await self._start_local(session)
                        if execution_scope is not None:
                            execution_scope.mark_launch_state("started", sid)
                    except BaseException as exc:
                        if execution_scope is not None:
                            execution_scope.mark_recovery_pending(
                                f"本地执行单元启动未确认（{type(exc).__name__}）"
                            )
                        raise
                else:
                    await self._start_docker(session)
        except OSError as exc:
            if mode == "docker":
                # Host process/resource failures while invoking the sandbox are
                # platform faults.  Do not expose absolute paths from OSError to
                # the public match stream or reject the uploaded Bot as invalid.
                raise PlatformRunnerError(
                    f"无法启动 {mode} 沙箱（{type(exc).__name__}）"
                ) from exc
            raise
        session.start_stderr_drain()
        logger.info(
            "bot session started sid=%s mode=%s path=%s fmt=%s/%s-%s",
            sid, mode, session.binary_path, session.info.format, session.info.os, session.info.arch,
        )
        self._sessions[sid] = session
        return sid

    async def _ensure_linux_image_ready(self) -> None:
        await asyncio.to_thread(
            _ensure_linux_image_ready_sync,
            self._docker_bin,
            self._linux_image,
            prepare_timeout=self._image_prepare_timeout,
        )

    async def ensure_runtime_ready(self) -> None:
        """Prepare the Linux sandbox outside any Bot decision/game clock.

        Local execution is an explicit test-only mode and has no image gate.
        Callers may safely invoke this before every Traditional decision: the
        process-wide cache makes the steady-state path a bounded no-op.
        """
        if self._prefer_local:
            return
        if not self._docker_ok:
            raise PlatformRunnerError("Linux x86_64 ELF bot 需要 Docker 沙箱")
        await self._ensure_linux_image_ready()

    def _select_mode(self, info: BinaryInfo) -> str:
        require_supported_binary(info)
        # Running an uploaded executable directly on the host is an explicit
        # test-only escape hatch.  Production must fail closed when Docker is
        # unavailable; silently falling back would bypass every sandbox limit.
        if self._prefer_local:
            return "local"
        if not self._docker_ok:
            raise PlatformRunnerError("Linux x86_64 ELF bot 需要 Docker 沙箱")
        return "docker"

    async def _start_local(self, session: BotSession) -> None:
        path = session.binary_path
        path.chmod(path.stat().st_mode | 0o111)
        session.proc = await asyncio.create_subprocess_exec(
            str(path),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # 多留一个字节容纳合法上限后的换行；超限时 readline 会抛
            # ValueError，由 send/read_extra_line 转成不携带原文的类型错误。
            limit=MAX_BOT_RESPONSE_LINE_BYTES + 1,
        )

    async def _start_docker(self, session: BotSession) -> None:
        name = f"bzbot-{session.session_id}"
        session.container_name = name
        # 确保二进制可执行（Docker 只读挂载 -v path:/app/bot:ro 不经 _start_local 的 chmod；
        # 若文件缺 exec 位 → exit 126 permission denied）。防御性补权限。
        try:
            session.binary_path.chmod(session.binary_path.stat().st_mode | 0o111)
        except (OSError, PermissionError):
            pass  # 只读挂载/权限不足时忽略（本机路径通常可改）
        options = [
            "-i", "--rm",
            "--pull=never",
            "--name", name,
            "--network=none",
            f"--memory={DEFAULT_MEMORY}",
            f"--cpus={DEFAULT_CPUS}",
            "--read-only",
            # /tmp 需可执行：PyInstaller 解压运行、动态链接 ELF 的 ld.so 延迟绑定都依赖 /tmp exec。
            # noexec 会导致 libz.so.1 等 .so 映射失败（exit 127），Bot 启动即崩。
            "--tmpfs", "/tmp:rw,exec,nosuid,size=64m",
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            "--platform", "linux/amd64",
            "-v", f"{session.binary_path}:/app/bot:ro",
            "--workdir", "/app",
            # 忽略自定义镜像自带的 Entrypoint/CMD，直接执行经文件头闸门
            # 校验的挂载 ELF；镜像只提供 Linux 用户态/动态链接环境。
            "--entrypoint", "/app/bot",
            self._linux_image,
        ]
        scope = session.execution_scope
        if scope is not None:
            launch_token = session.session_id
            daemon_incarnation = await asyncio.to_thread(
                _docker_daemon_incarnation
            )
            scope.mark_launch_state(
                "creating", launch_token, daemon_incarnation
            )

            async def refresh_ambiguous_incarnation(
                *, control_thread_terminal: bool
            ) -> None:
                refreshed = await asyncio.to_thread(_docker_daemon_incarnation)
                if not control_thread_terminal:
                    boot = refreshed.split(";", 1)[0]
                    refreshed = f"{boot};daemon:unknown"
                scope.mark_ambiguous_incarnation(launch_token, refreshed)

            options[5:5] = [
                "--label",
                f"{_AUTO_EXECUTION_LABEL}={scope.token}",
                "--label",
                f"{_AUTO_EXECUTION_LAUNCH_LABEL}={launch_token}",
            ]
            create_options = [item for item in options if item != "--rm"]
            create_cmd = [self._docker_bin, "create", *create_options]
            create_task = asyncio.create_task(
                asyncio.to_thread(
                    _docker_control_command,
                    create_cmd,
                    timeout=_DOCKER_INSPECT_TIMEOUT_SEC,
                    timeout_message="Docker 自动对局容器创建超时",
                )
            )
            cancellation: asyncio.CancelledError | None = None
            while True:
                try:
                    created = await asyncio.shield(create_task)
                    break
                except asyncio.CancelledError as exc:
                    # to_thread cannot be cancelled.  Keep the flock until the
                    # CLI reaches a definite return even under repeated task
                    # cancellation during service shutdown.
                    cancellation = cancellation or exc
                    if create_task.cancelled():
                        await refresh_ambiguous_incarnation(
                            control_thread_terminal=False
                        )
                        scope.mark_recovery_pending(
                            "Docker create 控制任务在结果确认前被取消"
                        )
                        raise cancellation
                    continue
                except PlatformRunnerError as exc:
                    await refresh_ambiguous_incarnation(
                        control_thread_terminal=True
                    )
                    scope.mark_recovery_pending(str(exc))
                    raise
                except BaseException as exc:
                    await refresh_ambiguous_incarnation(
                        control_thread_terminal=True
                    )
                    scope.mark_recovery_pending(
                        f"Docker create 控制任务异常（{type(exc).__name__}）"
                    )
                    raise
            if created.returncode != 0:
                reason = f"Docker 自动对局容器创建失败（exit {created.returncode}）"
                await refresh_ambiguous_incarnation(
                    control_thread_terminal=True
                )
                scope.mark_recovery_pending(reason)
                if cancellation is not None:
                    raise cancellation
                raise PlatformRunnerError(reason)
            confirmation_deadline = (
                asyncio.get_running_loop().time()
                + _AUTO_EXECUTION_CONFIRM_TIMEOUT_SEC
            )
            try:
                # ``docker create`` success is the daemon acknowledgement.  All
                # later awaits stay inside one failure envelope: cancellation,
                # fence loss, inspect failure, and start failure must clean the
                # exact scope or durably leave its recovery barrier occupied.
                # Persist that acknowledgement before inspect: even permanent
                # inspect failure can now safely converge from negative proof.
                scope.mark_launch_state("created", launch_token)
                if cancellation is not None:
                    raise cancellation
                while True:
                    remaining = (
                        confirmation_deadline
                        - asyncio.get_running_loop().time()
                    )
                    if remaining <= 0:
                        reason = "Docker 自动对局容器标签确认超时"
                        scope.mark_recovery_pending(reason)
                        raise PlatformRunnerError(reason)
                    try:
                        labels = await self._docker_container_execution_labels(
                            name,
                            timeout=min(_DOCKER_INSPECT_TIMEOUT_SEC, remaining),
                        )
                    except PlatformRunnerError:
                        labels = None
                    if labels == (scope.token, launch_token):
                        break
                    await asyncio.sleep(
                        min(_AUTO_EXECUTION_VISIBILITY_POLL_SEC, remaining)
                    )
                scope.assert_current()
                start_cmd = [self._docker_bin, "start", "-a", "-i", name]
                session.proc = await asyncio.create_subprocess_exec(
                    *start_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=MAX_BOT_RESPONSE_LINE_BYTES + 1,
                )
                while True:
                    remaining = (
                        confirmation_deadline
                        - asyncio.get_running_loop().time()
                    )
                    if remaining <= 0:
                        reason = "Docker 自动对局容器启动确认超时"
                        scope.mark_recovery_pending(reason)
                        raise PlatformRunnerError(reason)
                    try:
                        state = await self._docker_container_state(
                            name,
                            timeout=min(_DOCKER_INSPECT_TIMEOUT_SEC, remaining),
                        )
                    except PlatformRunnerError:
                        state = None
                    started_at = str((state or {}).get("StartedAt") or "")
                    if started_at and not started_at.startswith("0001-"):
                        scope.mark_launch_state("started", launch_token)
                        break
                    state_error = str((state or {}).get("Error") or "")
                    if state_error or session.proc.returncode is not None:
                        reason = state_error or "Docker 容器未成功启动"
                        raise PlatformRunnerError(reason)
                    await asyncio.sleep(
                        min(_AUTO_EXECUTION_VISIBILITY_POLL_SEC, remaining)
                    )
            except BaseException as launch_exc:
                cleanup_task = asyncio.create_task(
                    self._cleanup_created_scope_unlocked(
                        session,
                        f"Docker 自动对局容器启动未确认（{type(launch_exc).__name__}）",
                    )
                )
                try:
                    await asyncio.shield(cleanup_task)
                except asyncio.CancelledError:
                    await cleanup_task
                raise
            return

        cmd = [self._docker_bin, "run", *options]
        session.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=MAX_BOT_RESPONSE_LINE_BYTES + 1,
        )

    async def _docker_container_execution_labels(
        self,
        container_name: str,
        *,
        timeout: float = _DOCKER_INSPECT_TIMEOUT_SEC,
    ) -> tuple[str, str] | None:
        result = await asyncio.to_thread(
            _docker_control_command,
            [
                self._docker_bin,
                "inspect",
                "--format",
                "{{json .Config.Labels}}",
                container_name,
            ],
            timeout=max(0.001, timeout),
            timeout_message="Docker 执行隔离标签检查超时",
        )
        if result.returncode != 0:
            return None
        try:
            labels = json.loads(result.stdout or "{}")
        except (TypeError, ValueError):
            return None
        if not isinstance(labels, dict):
            return None
        return (
            str(labels.get(_AUTO_EXECUTION_LABEL) or ""),
            str(labels.get(_AUTO_EXECUTION_LAUNCH_LABEL) or ""),
        )

    async def _docker_container_state(
        self,
        container_name: str,
        *,
        timeout: float = _DOCKER_INSPECT_TIMEOUT_SEC,
    ) -> dict | None:
        result = await asyncio.to_thread(
            _docker_control_command,
            [
                self._docker_bin,
                "inspect",
                "--format",
                "{{json .State}}",
                container_name,
            ],
            timeout=max(0.001, timeout),
            timeout_message="Docker 执行容器启动状态检查超时",
        )
        if result.returncode != 0:
            return None
        try:
            state = json.loads(result.stdout or "{}")
        except (TypeError, ValueError):
            return None
        return state if isinstance(state, dict) else None

    async def _cleanup_created_scope_unlocked(
        self, session: BotSession, reason: str
    ) -> None:
        """Remove an exact created container and prove its scope is empty."""
        scope = session.execution_scope
        if scope is None or not session.container_name:
            return
        # ``docker start -a -i`` is only an attach/control process.  A failed
        # StartedAt confirmation must not leave it holding pipes or delaying
        # orchestrator shutdown while the labelled container is recovered.
        proc = session.proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
                except asyncio.TimeoutError:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=0.5)
            except (ProcessLookupError, asyncio.TimeoutError):
                pass
        cleanup = await self._force_stop_execution_unlocked(
            scope.token,
            execution_backend="docker",
            allow_local_ack=True,
            execution_launch_token=session.session_id,
        )
        if cleanup.get("observed_launch"):
            scope.mark_launch_state("created", session.session_id)
        if cleanup.get("confirmed") and cleanup.get("observed_launch"):
            scope.mark_cleanup_confirmed()
            return
        message = str(
            cleanup.get("reason")
            or (
                reason
                if cleanup.get("observed_launch")
                else "Docker create 结果仍不明确，等待观察当前启动单元"
            )
        )
        scope.mark_recovery_pending(message)
        raise PlatformRunnerError(message)

    async def send(self, session_id: str, line: str, *,
                   timeout: float = DEFAULT_ACTION_TIMEOUT) -> str:
        session = self._sessions.get(session_id)
        if not session or not session.proc or not session.proc.stdin or not session.proc.stdout:
            raise RuntimeError(f"session {session_id} 不可用")
        if session.execution_scope is not None:
            session.execution_scope.assert_current()
        if not line.endswith("\n"):
            line = line + "\n"
        try:
            session.proc.stdin.write(line.encode("utf-8"))
            await session.proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise await self._process_exit_error(session, "stdin 已关闭") from exc
        try:
            raw = await asyncio.wait_for(session.proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            # stderr is uploaded-program-controlled and may contain host/container
            # paths. The orchestrator emits the attributable structured record with
            # match/bot/version/runtime/seat/turn after this typed timeout propagates.
            logger.warning("bot session %s 决策超时 (%ss)", session_id, timeout)
            raise TimeoutError(f"bot {session_id} 决策超时 ({timeout}s)")
        except ValueError as exc:
            # StreamReader.readline 将 LimitOverrunError 规范化为 ValueError。
            # 不记录 Bot 控制的原始 stdout，也不把它误判成平台故障。
            raise BotResponseLineTooLargeError("Bot stdout 响应行超过硬顶") from exc
        if not raw:
            raise await self._process_exit_error(session, "stdout EOF")
        if session.execution_scope is not None:
            session.execution_scope.assert_current()
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

    async def _process_exit_error(
        self, session: BotSession, context: str
    ) -> RuntimeError:
        """Classify a dead transport consistently for stdin and stdout races."""
        returncode = session.proc.returncode if session.proc is not None else None
        if returncode is None and session.proc is not None:
            try:
                returncode = await asyncio.wait_for(session.proc.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                returncode = session.proc.returncode
        # proc.wait() 可能先于异步 stderr drain 任务收到 EOF；短暂等待可保留
        # 完整的 Bot stderr 尾部供运维诊断，但 stderr 绝不参与责任分类。
        stderr_task = session._stderr_task
        if stderr_task is not None and not stderr_task.done():
            try:
                await asyncio.wait_for(
                    asyncio.shield(stderr_task), timeout=_STDERR_DRAIN_GRACE_SEC
                )
            except asyncio.TimeoutError:
                pass
            except asyncio.CancelledError:
                pass
        tail = session.stderr_tail()
        logger.warning(
            "bot %s %s（进程退出码=%s）stderr=%s",
            session.session_id,
            context,
            returncode,
            tail[:500],
        )
        platform_reason = _classify_container_platform_exit(
            mode=session.mode,
            returncode=returncode,
        )
        if platform_reason is not None:
            # ``--pull=never`` makes a concurrently removed image fail fast as
            # docker exit 125.  Invalidate the readiness cache so the next
            # attempt can inspect/pull again outside the Bot decision clock.
            await asyncio.to_thread(
                _invalidate_linux_image_ready_sync,
                self._docker_bin,
                self._linux_image,
            )
            return PlatformRunnerError(
                f"sandbox 启动失败（{platform_reason}）"
            )
        return BotCrashedError(
            f"bot {session.session_id} {context}（进程退出码={returncode}）"
        )

    async def read_extra_line(self, session_id: str, *,
                              timeout: float = 1.0) -> str | None:
        """读取 Bot stdout 的一行（带短超时）。无数据返回 None（不报错）。

        用于 LongRunning 模式首回合后探测 ``>>>BOTZONE_REQUEST_KEEP_RUNNING<<<`` 握手：
        Bot 若想长驻，在响应后立即输出该行；平台读到即置 long_running。
        """
        session = self._sessions.get(session_id)
        if not session or not session.proc or not session.proc.stdout:
            return None
        if session.execution_scope is not None:
            session.execution_scope.assert_current()
        try:
            raw = await asyncio.wait_for(session.proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        except ValueError as exc:
            raise BotResponseLineTooLargeError("Bot stdout 握手行超过硬顶") from exc
        if not raw:
            return None
        if session.execution_scope is not None:
            session.execution_scope.assert_current()
        return raw.decode("utf-8", errors="replace").rstrip("\r\n") or None

    async def stop_session(self, session_id: str) -> None:
        session = self._sessions.get(session_id)
        if not session:
            return
        # 取消 stderr drain 任务
        if session._stderr_task is not None:
            session._stderr_task.cancel()
        proc = session.proc
        if proc and proc.returncode is None:
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3)
                except asyncio.TimeoutError:
                    proc.kill()
                    await proc.wait()
            except ProcessLookupError:
                pass
        if session.container_name and self._docker_ok:
            try:
                cleanup = await asyncio.create_subprocess_exec(
                    self._docker_bin, "rm", "-f", session.container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await cleanup.wait()
            except Exception:
                logger.debug("docker rm failed", exc_info=True)
        self._sessions.pop(session_id, None)

    async def _docker_execution_container_ids(self, token: str) -> list[str]:
        result = await asyncio.to_thread(
            _docker_control_command,
            [
                self._docker_bin,
                "ps",
                "-aq",
                "--filter",
                f"label={_AUTO_EXECUTION_LABEL}={token}",
            ],
            timeout=_DOCKER_INSPECT_TIMEOUT_SEC,
            timeout_message="Docker 执行隔离查询超时",
        )
        if result.returncode != 0:
            raise PlatformRunnerError("Docker 执行隔离查询失败")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    async def _docker_launch_container_ids(
        self, execution_token: str, launch_token: str
    ) -> list[str]:
        result = await asyncio.to_thread(
            _docker_control_command,
            [
                self._docker_bin,
                "ps",
                "-aq",
                "--filter",
                f"label={_AUTO_EXECUTION_LAUNCH_LABEL}={launch_token}",
                "--filter",
                f"label={_AUTO_EXECUTION_LABEL}={execution_token}",
            ],
            timeout=_DOCKER_INSPECT_TIMEOUT_SEC,
            timeout_message="Docker 当前启动单元查询超时",
        )
        if result.returncode != 0:
            raise PlatformRunnerError("Docker 当前启动单元查询失败")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    async def force_stop_execution(
        self,
        token: str,
        *,
        launch_lock_path: str,
        execution_backend: str,
        allow_local_ack: bool,
        execution_launch_token: str | None = None,
    ) -> dict[str, object]:
        """Stop one logical auto execution and prove its sandbox is gone.

        Docker proof is label based and therefore survives process death.  Host
        subprocesses have no equally trustworthy cross-process identity; a
        takeover must keep the durable recovery slot occupied instead.
        """
        if execution_backend not in {"docker", "local"}:
            return {
                "confirmed": False,
                "backend": execution_backend,
                "reason": "未知执行后端，无法确认清理",
            }
        async with _execution_file_lock(launch_lock_path):
            current_incarnation = (
                await asyncio.to_thread(_docker_daemon_incarnation)
                if execution_backend == "docker"
                else f"boot:{_host_boot_id()};daemon:local"
            )
            result = await self._force_stop_execution_unlocked(
                token,
                execution_backend=execution_backend,
                allow_local_ack=allow_local_ack,
                execution_launch_token=execution_launch_token,
            )
            result["current_daemon_incarnation"] = current_incarnation
            return result

    async def _force_stop_execution_unlocked(
        self,
        token: str,
        *,
        execution_backend: str,
        allow_local_ack: bool,
        execution_launch_token: str | None = None,
    ) -> dict[str, object]:
        """Cleanup implementation; caller must hold the per-database flock."""
        observed_launch = False
        if execution_backend == "docker" and execution_launch_token:
            try:
                observed_launch = bool(
                    await self._docker_launch_container_ids(
                        token, execution_launch_token
                    )
                )
            except PlatformRunnerError as exc:
                return {
                    "confirmed": False,
                    "backend": "docker",
                    "reason": str(exc),
                    "observed_scope": False,
                    "observed_launch": False,
                }
        matching = [
                sid
                for sid, session in list(self._sessions.items())
                if session.execution_scope is not None
                and session.execution_scope.token == token
        ]
        observed_scope = bool(matching)
        for sid in matching:
            try:
                await self.stop_session(sid)
            except Exception as exc:
                return {
                    "confirmed": False,
                    "backend": execution_backend,
                    "reason": f"执行会话停止失败（{type(exc).__name__}）",
                    "observed_scope": observed_scope,
                    "observed_launch": observed_launch,
                }

        if execution_backend == "local":
            if not allow_local_ack:
                return {
                    "confirmed": False,
                    "backend": "local",
                    "reason": "本地模式无法跨进程确认旧 Bot 已退出",
                    "observed_scope": observed_scope,
                    "observed_launch": observed_launch,
                }
            remaining = any(
                session.execution_scope is not None
                and session.execution_scope.token == token
                for session in self._sessions.values()
            )
            return {
                "confirmed": not remaining,
                "backend": "local",
                "reason": "" if not remaining else "本地 Bot 会话仍在运行",
                "observed_scope": observed_scope,
                "observed_launch": observed_launch,
            }

        try:
            ids = await self._docker_execution_container_ids(token)
            if ids:
                observed_scope = True
                # Close the first-exact-query -> scope-query race.  The current
                # delayed create may become visible as part of this scope only
                # after the first launch-label query; prove it before removal.
                if execution_launch_token and not observed_launch:
                    observed_launch = bool(
                        await self._docker_launch_container_ids(
                            token, execution_launch_token
                        )
                    )
                removed = await asyncio.to_thread(
                    _docker_control_command,
                    [self._docker_bin, "rm", "-f", *ids],
                    timeout=_DOCKER_INSPECT_TIMEOUT_SEC,
                    timeout_message="Docker 执行隔离清理超时",
                )
                if removed.returncode != 0:
                    return {
                        "confirmed": False,
                        "backend": "docker",
                        "reason": "Docker 执行隔离清理失败",
                        "observed_scope": observed_scope,
                        "observed_launch": observed_launch,
                    }
            # A successful ``docker rm`` is not the acknowledgement.  Re-query
            # the label until the daemon proves the scope has zero containers.
            zero_polls = 0
            for attempt in range(_AUTO_EXECUTION_CLEANUP_POLLS):
                remaining = await self._docker_execution_container_ids(token)
                remaining_launch: list[str] = []
                if execution_launch_token:
                    remaining_launch = await self._docker_launch_container_ids(
                        token, execution_launch_token
                    )
                    if remaining_launch:
                        observed_launch = True
                if not remaining and not remaining_launch:
                    zero_polls += 1
                    if zero_polls >= _AUTO_EXECUTION_CLEANUP_ZERO_POLLS:
                        return {
                            "confirmed": True,
                            "backend": "docker",
                            "reason": "",
                            "observed_scope": observed_scope,
                            "observed_launch": observed_launch,
                        }
                else:
                    zero_polls = 0
                if attempt + 1 < _AUTO_EXECUTION_CLEANUP_POLLS:
                    await asyncio.sleep(0.05)
            return {
                "confirmed": False,
                "backend": "docker",
                "reason": "Docker 执行隔离清理后仍有容器存活",
                "observed_scope": observed_scope,
                "observed_launch": observed_launch,
            }
        except PlatformRunnerError as exc:
            logger.error("auto execution cleanup failed token=%s error=%s", token, exc)
            return {
                "confirmed": False,
                "backend": "docker",
                "reason": str(exc),
                "observed_scope": observed_scope,
                "observed_launch": observed_launch,
            }


def _classify_container_platform_exit(
    *,
    mode: str,
    returncode: int | None,
) -> str | None:
    """Docker exit 125 is infrastructure; all other exits belong to the Bot."""
    if mode == "docker" and returncode == 125:
        return "docker exit 125"
    return None
