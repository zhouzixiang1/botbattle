"""Linux x86_64 ELF Bot 沙箱运行器（Docker；测试可显式本机执行）。"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from bzplat.backend.runtime.config import ACTION_TIMEOUT_SEC
from bzplat.backend.runtime.docker_supervisor import (
    CANONICAL_DOCKER_HOST,
    DockerControlUncertain,
    DockerExecutionIdentity,
    DockerSupervisor,
    docker_cli_environment,
)
from bzplat.backend.runtime.limits import (
    MAX_BOT_RESPONSE_LINE_BYTES,
    DockerResourceProfile,
    PLATFORM_LOW_PROFILE,
    resolve_docker_resource_profile,
)

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
DEFAULT_LINUX_IMAGE = "debian:bookworm-slim"
DEFAULT_IMAGE_PREPARE_TIMEOUT = 300.0
_DOCKER_INSPECT_TIMEOUT_SEC = 15.0
_STDERR_DRAIN_GRACE_SEC = 0.5
_IMAGE_READY_LOCK = threading.Lock()
_IMAGE_READY_KEYS: set[tuple[str, str]] = set()


class ExecutionAttemptCancelled(asyncio.CancelledError):
    """The durable execution attempt yielded before physical work began."""


def _raise_if_attempt_cancelled(exc: Exception) -> None:
    if getattr(exc, "code", "") == "execution_attempt_not_current":
        raise ExecutionAttemptCancelled(str(exc)) from exc


@dataclass
class ExecutionScope:
    """One durable job attempt shared by all of its Bot sessions."""

    instance: str
    job_public_id: str
    attempt_no: int
    supervisor: DockerSupervisor | None
    attempt_check: Callable[[], None]
    recovery_mark: Callable[[str], None] | None = None
    cleanup_mark: Callable[[], None] | None = None
    _next_slot: int = 0
    _slot_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def identity(self) -> DockerExecutionIdentity:
        return DockerExecutionIdentity(
            self.instance, self.job_public_id, int(self.attempt_no)
        )

    def allocate_slot(self) -> int:
        with self._slot_lock:
            slot = self._next_slot
            self._next_slot += 1
            return slot

    def assert_current(self) -> None:
        try:
            self.attempt_check()
        except Exception as exc:
            _raise_if_attempt_cancelled(exc)
            raise

    def mark_recovery_pending(self, reason: str) -> None:
        if self.recovery_mark is not None:
            self.recovery_mark(reason)

    def mark_cleanup_confirmed(self) -> None:
        if self.cleanup_mark is not None:
            self.cleanup_mark()


@dataclass
class BotSession:
    session_id: str
    info: BinaryInfo
    binary_path: Path
    proc: asyncio.subprocess.Process | None = None
    mode: str = "docker"  # docker | local（local 仅 BZ_BOT_LOCAL 测试开关）
    profile: DockerResourceProfile = PLATFORM_LOW_PROFILE
    container_name: str = ""
    container_slot: int | None = None
    launch_token: str = ""
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
    _preflight_permit_held: bool = False
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


class SandboxControlUncertain(PlatformRunnerError):
    """A Docker create/inspect/rm acknowledgement is not trustworthy."""


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
    if len(args) >= 2 and args[1] != "--host":
        args = [args[0], "--host", CANONICAL_DOCKER_HOST, *args[1:]]
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
            env=docker_cli_environment(),
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
        db_path: str | Path | None = None,
        instance_key: str | None = None,
        docker_uncertain_callback: Callable[[str], None] | None = None,
        supervisor: DockerSupervisor | None = None,
        preflight_gate: threading.BoundedSemaphore | None = None,
    ) -> None:
        self._docker_bin = docker_bin
        self._sessions: dict[str, BotSession] = {}
        # 测试环境可强制本机跑同架构 ELF
        if prefer_local is None:
            prefer_local = os.environ.get("BZ_BOT_LOCAL", "").lower() in ("1", "true", "yes")
        self._prefer_local = prefer_local
        self._docker_uncertain_callback = docker_uncertain_callback
        self._preflight_gate = preflight_gate
        self._docker_ok = shutil.which(docker_bin) is not None
        self.supervisor: DockerSupervisor | None = supervisor
        if not self._prefer_local:
            if self.supervisor is None:
                self.supervisor = DockerSupervisor(
                    db_path=db_path or os.environ.get("BZ_DB_PATH", "botzone.db"),
                    docker_bin=docker_bin,
                    instance_key=instance_key,
                )
            self._docker_ok = True
        elif supervisor is not None:
            raise ValueError("local runner cannot own a Docker supervisor")
        self._linux_image = (
            linux_image
            or DEFAULT_LINUX_IMAGE
        )
        self._image_prepare_timeout = max(0.001, float(image_prepare_timeout))

    def _mark_unscoped_docker_uncertain(self, reason: str) -> None:
        """Pause the shared dispatcher for preflight/control uncertainty."""
        callback = getattr(self, "_docker_uncertain_callback", None)
        if callback is None:
            return
        try:
            callback(reason)
        except Exception:
            logger.exception("failed to persist Docker uncertainty pause")

    def _new_session(
        self,
        binary_path: str | Path,
        *,
        info: BinaryInfo | None,
        runtime_mode: str,
        profile: str | DockerResourceProfile = PLATFORM_LOW_PROFILE,
        execution_scope: ExecutionScope | None = None,
    ) -> BotSession:
        """校验二进制并创建逻辑会话；不启动进程。"""
        if execution_scope is not None:
            execution_scope.assert_current()
        if runtime_mode not in VALID_RUNTIME_MODES:
            raise ValueError(f"未知运行模式: {runtime_mode}")
        resolved_profile = resolve_docker_resource_profile(profile)
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
            profile=resolved_profile, runtime_mode=runtime_mode,
            execution_scope=execution_scope,
        )
        return session

    async def prepare_session(
        self,
        binary_path: str | Path,
        *,
        info: BinaryInfo | None = None,
        runtime_mode: str = DEFAULT_RUNTIME_MODE,
        profile: str | DockerResourceProfile = PLATFORM_LOW_PROFILE,
        execution_scope: ExecutionScope | None = None,
    ) -> str:
        """只登记 Traditional 的历史状态，不启动整场闲置 Bot 进程。"""
        if runtime_mode != DEFAULT_RUNTIME_MODE:
            raise ValueError("prepare_session 只用于 Traditional 逻辑会话")
        session = self._new_session(
            binary_path,
            info=info,
            runtime_mode=runtime_mode,
            profile=profile,
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
                            profile: str | DockerResourceProfile = PLATFORM_LOW_PROFILE,
                            execution_scope: ExecutionScope | None = None) -> str:
        session = self._new_session(
            binary_path,
            info=info,
            runtime_mode=runtime_mode,
            profile=profile,
            execution_scope=execution_scope,
        )
        sid = session.session_id
        mode = session.mode
        try:
            if mode == "docker":
                # 镜像 inspect/pull 属于平台准备阶段，必须先于 Bot 响应计时；
                # ``docker run --pull=never`` 再保证计时窗口内不会隐式拉镜像。
                await self.ensure_runtime_ready()
            if execution_scope is not None:
                execution_scope.assert_current()
            if mode == "local":
                await self._start_local(session)
            else:
                await self._acquire_preflight_permit(session)
                await self._start_docker(session)
        except OSError as exc:
            self._release_preflight_permit(session)
            if mode == "docker":
                # Host process/resource failures while invoking the sandbox are
                # platform faults.  Do not expose absolute paths from OSError to
                # the public match stream or reject the uploaded Bot as invalid.
                raise PlatformRunnerError(
                    f"无法启动 {mode} 沙箱（{type(exc).__name__}）"
                ) from exc
            raise
        except BaseException:
            self._release_preflight_permit(session)
            raise
        session.start_stderr_drain()
        logger.info(
            "bot session started sid=%s mode=%s path=%s fmt=%s/%s-%s",
            sid, mode, session.binary_path, session.info.format, session.info.os, session.info.arch,
        )
        self._sessions[sid] = session
        return sid

    async def _acquire_preflight_permit(self, session: BotSession) -> None:
        """Bound unscoped upload preflight across worker event loops.

        The platform owns one process per DB (dispatcher flock), while upload
        validation may run in several worker threads/event loops.  A
        ``threading.BoundedSemaphore`` is therefore the correct small boundary:
        at most one extra untrusted preflight container may coexist with the
        execution queue.  Job-scoped sessions already consume the durable
        execution capacity and never acquire this dedicated lane.
        """
        gate = self._preflight_gate
        if (
            gate is None
            or session.mode != "docker"
            or session.execution_scope is not None
            or session._preflight_permit_held
        ):
            return
        acquire = asyncio.create_task(
            asyncio.to_thread(gate.acquire),
            name=f"preflight-admission-{session.session_id}",
        )
        cancelled = False
        while not acquire.done():
            try:
                await asyncio.shield(acquire)
            except asyncio.CancelledError:
                cancelled = True
        acquire.result()
        if cancelled:
            gate.release()
            raise asyncio.CancelledError
        session._preflight_permit_held = True

    def _release_preflight_permit(self, session: BotSession) -> None:
        gate = self._preflight_gate
        if gate is None or not session._preflight_permit_held:
            return
        session._preflight_permit_held = False
        gate.release()

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
        try:
            await self._ensure_linux_image_ready()
        except PlatformRunnerError as exc:
            self._mark_unscoped_docker_uncertain(str(exc))
            raise

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
        """Create, label, inspect and start one deterministic local container."""
        supervisor = self.supervisor
        if supervisor is None:
            raise PlatformRunnerError("Docker supervisor 未配置")
        scope = session.execution_scope
        if scope is not None:
            scope.assert_current()
            identity = scope.identity
            slot = scope.allocate_slot()
        else:
            identity = DockerExecutionIdentity(
                supervisor.instance, f"preflight-{session.session_id}", 1
            )
            slot = 0
        session.container_slot = slot
        owner_kind = "execution" if scope is not None else "preflight"
        session.launch_token = uuid.uuid4().hex
        try:
            session.binary_path.chmod(session.binary_path.stat().st_mode | 0o111)
        except (OSError, PermissionError):
            pass
        try:
            async with supervisor.launch_guard():
                # ``to_thread`` cancellation does not stop the worker. Shield and
                # drain it before propagating cancellation; the durable intent
                # remains authoritative even if daemon acknowledgement is lost.
                create_task = asyncio.create_task(
                    asyncio.to_thread(
                        supervisor.create,
                        identity=identity,
                        slot=slot,
                        launch_token=session.launch_token,
                        owner_kind=owner_kind,
                        binary_path=session.binary_path,
                        image=self._linux_image,
                        profile=session.profile,
                    ),
                    name=f"docker-create-{session.session_id}",
                )
                try:
                    session.container_name = await asyncio.shield(create_task)
                except asyncio.CancelledError:
                    try:
                        session.container_name = await create_task
                    except DockerControlUncertain as exc:
                        reason = str(exc)
                        if scope is not None:
                            scope.mark_recovery_pending(reason)
                        else:
                            self._mark_unscoped_docker_uncertain(reason)
                    else:
                        reason = "Docker create 已确认但调用方取消；等待精确清理"
                        if scope is not None:
                            scope.mark_recovery_pending(reason)
                        else:
                            self._mark_unscoped_docker_uncertain(reason)
                    raise
                if scope is not None:
                    scope.assert_current()
                start_task = asyncio.create_task(
                    supervisor.start_attached(
                        session.container_name,
                        stream_limit=MAX_BOT_RESPONSE_LINE_BYTES + 1,
                        launch_token=session.launch_token,
                    ),
                    name=f"docker-start-{session.session_id}",
                )
                try:
                    session.proc = await asyncio.shield(start_task)
                except asyncio.CancelledError:
                    start_task.cancel()
                    reason = "Docker start 期间调用方取消；等待精确清理"
                    try:
                        attached = await start_task
                    except asyncio.CancelledError:
                        attached = None
                    except DockerControlUncertain as exc:
                        reason = str(exc)
                        attached = None
                    if attached is not None:
                        await supervisor._terminate_control_process(attached)
                    if scope is not None:
                        scope.mark_recovery_pending(reason)
                    else:
                        self._mark_unscoped_docker_uncertain(reason)
                    raise
        except DockerControlUncertain as exc:
            reason = str(exc)
            if scope is not None:
                scope.mark_recovery_pending(reason)
            else:
                self._mark_unscoped_docker_uncertain(reason)
            raise SandboxControlUncertain(reason) from exc
        except Exception as exc:
            _raise_if_attempt_cancelled(exc)
            raise

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
        # Docker control failures are rejected by ``start_attached`` before it
        # returns a process. Once a non-zero daemon StartedAt has been proved,
        # ``docker start -a`` forwards the container's exit status; even 125 is
        # therefore Bot-attributable and must not pause the platform or evade a
        # rated result.
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
        """Stop one transport and remove its exact physical container."""
        session = self._sessions.get(session_id)
        if session is None:
            return
        try:
            if session._stderr_task is not None:
                session._stderr_task.cancel()
            proc = session.proc
            if proc is not None and proc.returncode is None:
                try:
                    if proc.stdin is not None:
                        proc.stdin.close()
                    proc.terminate()
                    try:
                        await asyncio.wait_for(proc.wait(), timeout=3)
                    except asyncio.TimeoutError:
                        proc.kill()
                        await proc.wait()
                except ProcessLookupError:
                    pass
            if session.container_name:
                scope = session.execution_scope
                supervisor = self.supervisor
                if supervisor is None:
                    reason = "Docker supervisor 未配置，无法确认单会话清理"
                    if scope is not None:
                        scope.mark_recovery_pending(reason)
                    else:
                        self._mark_unscoped_docker_uncertain(reason)
                    raise SandboxControlUncertain(reason)
                try:
                    if scope is not None:
                        if session.container_slot is None or not session.launch_token:
                            raise DockerControlUncertain(
                                "Docker 会话缺少精确清理身份"
                            )
                        # Traditional 每回合都是一个物理会话；只停
                        # ``docker start -a`` 会留下 running/stopped 容器。
                        # 用该 session 的 slot/name/launch token 立即定向删除，
                        # 不能调用 cleanup_job 误删同局 LongRunning 座位。
                        await supervisor.cleanup_session(
                            scope.identity,
                            slot=session.container_slot,
                            name=session.container_name,
                            launch_token=session.launch_token,
                        )
                    else:
                        # Unscoped preflight owns its one-job namespace here.
                        # Its launch journal may still need the broader cleanup
                        # path when create/start acknowledgement was uncertain.
                        await supervisor.cleanup_job(
                            DockerExecutionIdentity(
                                supervisor.instance,
                                f"preflight-{session.session_id}",
                                1,
                            )
                        )
                except DockerControlUncertain as exc:
                    reason = str(exc)
                    if scope is not None:
                        scope.mark_recovery_pending(reason)
                    else:
                        self._mark_unscoped_docker_uncertain(reason)
                    raise SandboxControlUncertain(reason) from exc
        finally:
            # An uncertain cleanup has already paused the dispatcher/journal,
            # which gates every later create.  Release the in-process lane so a
            # successful administrator namespace recovery does not leave a
            # permanently leaked semaphore token.
            self._release_preflight_permit(session)
        self._sessions.pop(session_id, None)

    async def cleanup_execution(self, scope: ExecutionScope) -> None:
        """Remove one exact job attempt and prove its labels are zero."""
        matching = [
            sid
            for sid, session in list(self._sessions.items())
            if session.execution_scope is scope
        ]
        for sid in matching:
            await self.stop_session(sid)
        if self._prefer_local:
            scope.mark_cleanup_confirmed()
            return
        supervisor = scope.supervisor or self.supervisor
        if supervisor is None:
            reason = "Docker supervisor 未配置，无法确认清理"
            scope.mark_recovery_pending(reason)
            raise SandboxControlUncertain(reason)
        try:
            await supervisor.cleanup_job(scope.identity)
        except DockerControlUncertain as exc:
            reason = str(exc)
            scope.mark_recovery_pending(reason)
            raise SandboxControlUncertain(reason) from exc
        scope.mark_cleanup_confirmed()

    async def cleanup_instance(self) -> None:
        if self._prefer_local:
            return
        if self.supervisor is None:
            raise SandboxControlUncertain("Docker supervisor 未配置")
        try:
            await self.supervisor.cleanup_instance()
        except DockerControlUncertain as exc:
            raise SandboxControlUncertain(str(exc)) from exc
