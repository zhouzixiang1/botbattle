"""Linux x86_64 ELF Bot 沙箱运行器（Docker；测试可显式本机执行）。"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

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

DEFAULT_ACTION_TIMEOUT = 60.0
DEFAULT_MEMORY = "512m"
DEFAULT_CPUS = "1"
DEFAULT_LINUX_IMAGE = "debian:bookworm-slim"
DEFAULT_IMAGE_PREPARE_TIMEOUT = 300.0
_DOCKER_INSPECT_TIMEOUT_SEC = 15.0
_STDERR_DRAIN_GRACE_SEC = 0.5
_IMAGE_READY_LOCK = threading.Lock()
_IMAGE_READY_KEYS: set[tuple[str, str]] = set()


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

    def _new_session(
        self,
        binary_path: str | Path,
        *,
        info: BinaryInfo | None,
        runtime_mode: str,
    ) -> BotSession:
        """校验二进制并创建逻辑会话；不启动进程。"""
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
            runtime_mode=runtime_mode,
        )
        return session

    async def prepare_session(
        self,
        binary_path: str | Path,
        *,
        info: BinaryInfo | None = None,
        runtime_mode: str = DEFAULT_RUNTIME_MODE,
    ) -> str:
        """只登记 Traditional 的历史状态，不启动整场闲置 Bot 进程。"""
        if runtime_mode != DEFAULT_RUNTIME_MODE:
            raise ValueError("prepare_session 只用于 Traditional 逻辑会话")
        session = self._new_session(
            binary_path,
            info=info,
            runtime_mode=runtime_mode,
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
                            runtime_mode: str = DEFAULT_RUNTIME_MODE) -> str:
        session = self._new_session(
            binary_path,
            info=info,
            runtime_mode=runtime_mode,
        )
        sid = session.session_id
        mode = session.mode
        try:
            if mode == "local":
                await self._start_local(session)
            else:
                # 镜像 inspect/pull 属于平台准备阶段，必须先于 Bot 响应计时；
                # ``docker run --pull=never`` 再保证计时窗口内不会隐式拉镜像。
                await self.ensure_runtime_ready()
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
            limit=1024 * 1024,
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
        cmd = [
            self._docker_bin, "run", "-i", "--rm",
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
        session.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )

    async def send(self, session_id: str, line: str, *,
                   timeout: float = DEFAULT_ACTION_TIMEOUT) -> str:
        session = self._sessions.get(session_id)
        if not session or not session.proc or not session.proc.stdin or not session.proc.stdout:
            raise RuntimeError(f"session {session_id} 不可用")
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
        if not raw:
            raise await self._process_exit_error(session, "stdout EOF")
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
        try:
            raw = await asyncio.wait_for(session.proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        if not raw:
            return None
        return raw.decode("utf-8", errors="replace").rstrip("\r\n") or None

    async def stop_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
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
                await asyncio.create_subprocess_exec(
                    self._docker_bin, "rm", "-f", session.container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
            except Exception:
                logger.debug("docker rm failed", exc_info=True)


def _classify_container_platform_exit(
    *,
    mode: str,
    returncode: int | None,
) -> str | None:
    """Docker exit 125 is infrastructure; all other exits belong to the Bot."""
    if mode == "docker" and returncode == 125:
        return "docker exit 125"
    return None
