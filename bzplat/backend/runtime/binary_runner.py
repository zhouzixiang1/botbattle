"""二进制 Bot 沙箱运行器：Docker（ELF）/ Wine 容器（PE）/ 本机 subprocess（测试）。"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from ..bots.classify import BinaryInfo, BinaryRejectError, classify_binary

logger = logging.getLogger(__name__)

DEFAULT_ACTION_TIMEOUT = 60.0
DEFAULT_MEMORY = "512m"
DEFAULT_CPUS = "1"
LINUX_IMAGE = os.environ.get("BZ_LINUX_BOT_IMAGE", "debian:bookworm-slim")
WINE_IMAGE = os.environ.get("BZ_WINE_BOT_IMAGE", "scottyhardy/docker-wine:stable")
_RUNTIME_ERROR_PREFIX = "BZPLAT_RUNTIME_ERROR"
_TRUSTED_RUNTIME_FAILURE_REASONS = frozenset(
    {
        "wine_tmpfs_init",
        "wine_tmpfs_permissions",
        "wine_not_found",
        "wine_not_executable",
    }
)
_STDERR_DRAIN_GRACE_SEC = 0.5


@dataclass
class BotSession:
    session_id: str
    info: BinaryInfo
    binary_path: Path
    proc: asyncio.subprocess.Process | None = None
    mode: str = "docker"  # docker | wine | local
    container_name: str = ""
    _buf: bytes = field(default_factory=bytes)
    _stderr_tail: bytearray = field(default_factory=bytearray)  # bot stderr 末尾（排查崩溃用）
    _stderr_task: asyncio.Task | None = None
    # ── Botzone 协议会话状态（传输层维护，runner 读写）──
    runtime_mode: str = "longrunning"  # "traditional" | "longrunning"
    requests: list = field(default_factory=list)   # 累积下发的请求负载（Traditional 重放用）
    responses: list = field(default_factory=list)  # 累积 Bot 响应负载（Traditional 信封 responses[]）
    turn: int = 0                                  # 已完成的回合数（0=首回合尚未握手判定）
    long_running: bool = False  # LongRunning Bot 首回合握手后置 True（之后发单 request 信封）
    # 只写入容器启动 wrapper，不放入 Bot 环境或 exec 后的 argv。
    # 据此区分平台 runtime/entrypoint 故障与 Bot 自行伪造的 stderr。
    runtime_error_token: str = field(default_factory=lambda: uuid.uuid4().hex, repr=False)

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
    决策超时是「Bot 慢」，崩溃是「Bot 死了」，后者应快速 abort 对局而非吞成默认动作死磕。"""

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


class BinaryRunner:
    """管理 bot 进程/容器的 stdin/stdout 行协议会话。"""

    def __init__(self, *, docker_bin: str = "docker",
                 prefer_local: bool | None = None) -> None:
        self._docker_bin = docker_bin
        self._sessions: dict[str, BotSession] = {}
        # 测试环境可强制本机跑同架构 ELF
        if prefer_local is None:
            prefer_local = os.environ.get("BZ_BOT_LOCAL", "").lower() in ("1", "true", "yes")
        self._prefer_local = prefer_local
        self._docker_ok = shutil.which(docker_bin) is not None

    async def start_session(self, binary_path: str | Path, *,
                            info: BinaryInfo | None = None,
                            action_timeout: float = DEFAULT_ACTION_TIMEOUT,
                            runtime_mode: str = "longrunning") -> str:
        path = Path(binary_path).resolve()
        if not path.is_file():
            raise BotCrashedError(f"bot 二进制不存在: {path}")
        raw = path.read_bytes()[:4096]
        info = info or classify_binary(raw if len(raw) >= 4 else path.read_bytes())
        if not info.runnable:
            raise BinaryRejectError(info.reject_reason or "不可执行的二进制")

        sid = uuid.uuid4().hex[:12]
        mode = self._select_mode(info)
        session = BotSession(
            session_id=sid, info=info, binary_path=path, mode=mode,
            runtime_mode=runtime_mode,
        )
        try:
            if mode == "local":
                await self._start_local(session)
            elif mode == "wine":
                await self._start_wine(session)
            else:
                await self._start_docker(session)
        except OSError as exc:
            if mode in ("docker", "wine"):
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

    def _select_mode(self, info: BinaryInfo) -> str:
        if info.format == "pe":
            if not self._docker_ok:
                raise PlatformRunnerError("Windows PE bot 需要 Docker + Wine 镜像")
            return "wine"
        if info.format == "elf":
            # Running an uploaded executable directly on the host is an explicit
            # test-only escape hatch.  Production must fail closed when Docker is
            # unavailable; silently falling back would bypass every sandbox limit.
            if self._prefer_local:
                return "local"
            if not self._docker_ok:
                raise PlatformRunnerError("Linux ELF bot 需要 Docker 沙箱")
            return "docker"
        raise BinaryRejectError(info.reject_reason or "不支持的格式")

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
        platform = {
            "amd64": "linux/amd64",
            "arm64": "linux/arm64",
            "i386": "linux/386",
        }.get(session.info.arch, "linux/amd64")
        cmd = [
            self._docker_bin, "run", "-i", "--rm",
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
            "--platform", platform,
            "-v", f"{session.binary_path}:/app/bot:ro",
            "--workdir", "/app",
            LINUX_IMAGE,
            "/app/bot",
        ]
        session.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
        )

    async def _start_wine(self, session: BotSession) -> None:
        name = f"bzbot-wine-{session.session_id}"
        session.container_name = name
        # 防御性补执行权限（与 _start_local/_start_docker 对齐：wine 容器内虽不经 host exec 位，
        # 但保持一致防御，避免某些 wine 版本检查文件 mode）。
        try:
            session.binary_path.chmod(session.binary_path.stat().st_mode | 0o111)
        except (OSError, PermissionError):
            pass
        runtime_marker = f"{_RUNTIME_ERROR_PREFIX}:{session.runtime_error_token}:"
        # 强制绕过公共镜像默认的 root entrypoint，以 nobody 运行固定
        # wrapper。Wine 需要可写 HOME/WINEPREFIX，它们只落在独立 tmpfs；
        # 根文件系统仍为只读，Bot 挂载仍为 ro。
        wine_wrapper = (
            'runtime_error() { code="$1"; reason="$2"; '
            f'printf "%s\\n" "{runtime_marker}$reason" >&2; exit "$code"; }}; '
            'mkdir -p "$WINEPREFIX" "$XDG_RUNTIME_DIR" '
            '|| runtime_error 126 wine_tmpfs_init; '
            'chmod 700 "$HOME" "$WINEPREFIX" "$XDG_RUNTIME_DIR" '
            '|| runtime_error 126 wine_tmpfs_permissions; '
            'wine_bin="$(command -v wine)" '
            '|| runtime_error 127 wine_not_found; '
            '[ -x "$wine_bin" ] '
            '|| runtime_error 126 wine_not_executable; '
            'exec "$wine_bin" /app/bot.exe'
        )
        cmd = [
            self._docker_bin, "run", "-i", "--rm",
            "--name", name,
            "--network=none",
            f"--memory={DEFAULT_MEMORY}",
            f"--cpus={DEFAULT_CPUS}",
            "--read-only",
            "--tmpfs", "/tmp:rw,exec,nosuid,nodev,size=64m,mode=1777",
            # Wine 首次启动会初始化 prefix；size 是上限而非预分配，
            # 实际占用仍受容器 512 MiB 内存硬限统一约束。
            "--tmpfs", "/winehome:rw,exec,nosuid,nodev,size=384m,uid=65534,gid=65534,mode=0700",
            "--cap-drop=ALL",
            "--security-opt", "no-new-privileges",
            "--user", "65534:65534",
            "--env", "HOME=/winehome",
            "--env", "WINEPREFIX=/winehome/prefix",
            "--env", "XDG_RUNTIME_DIR=/winehome/runtime",
            "-v", f"{session.binary_path}:/app/bot.exe:ro",
            "--workdir", "/tmp",
            "--entrypoint", "/bin/sh",
            WINE_IMAGE,
            "-c", wine_wrapper,
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
            tail = session.stderr_tail()
            logger.warning("bot %s 决策超时 (%ss) stderr=%s", session_id, timeout, tail[:500])
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
        # proc.wait() 可能先于异步 stderr drain 任务收到 EOF；诊断标记
        # 若在这个窗口丢失，会把 Wine 入口故障错记为 Bot 技术负。
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
            stderr_tail=tail,
            runtime_error_token=session.runtime_error_token,
        )
        if platform_reason is not None:
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
    stderr_tail: str,
    runtime_error_token: str,
) -> str | None:
    """Return a trusted platform-fault reason, otherwise leave blame on the Bot.

    Docker exit 125 is reserved for the Docker client/daemon failing to launch a
    container.  Exit 126/127 is ambiguous: a Bot may deliberately return either
    value, so those codes are platform faults only when the pre-exec wrapper emits
    this session's unguessable marker.  The marker is not inherited by the Bot.
    """
    if mode not in ("docker", "wine"):
        return None
    if returncode == 125:
        return "docker exit 125"
    if returncode not in (126, 127):
        return None

    marker = f"{_RUNTIME_ERROR_PREFIX}:{runtime_error_token}:"
    marker_at = stderr_tail.find(marker)
    if marker_at < 0:
        return None
    reason = stderr_tail[marker_at + len(marker):].splitlines()[0].strip()
    if reason not in _TRUSTED_RUNTIME_FAILURE_REASONS:
        return None
    return f"{reason}, exit {returncode}"
