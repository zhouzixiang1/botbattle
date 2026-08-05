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
        if mode == "local":
            await self._start_local(session)
        elif mode == "wine":
            await self._start_wine(session)
        else:
            await self._start_docker(session)
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
                raise RuntimeError("Windows PE bot 需要 Docker + Wine 镜像")
            return "wine"
        if info.format == "elf":
            if self._prefer_local or not self._docker_ok:
                return "local"
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
        cmd = [
            self._docker_bin, "run", "-i", "--rm",
            "--name", name,
            "--network=none",
            f"--memory={DEFAULT_MEMORY}",
            f"--cpus={DEFAULT_CPUS}",
            "--cap-drop=ALL",
            "-v", f"{session.binary_path}:/app/bot.exe:ro",
            WINE_IMAGE,
            "wine", "/app/bot.exe",
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
        session.proc.stdin.write(line.encode("utf-8"))
        await session.proc.stdin.drain()
        try:
            raw = await asyncio.wait_for(session.proc.stdout.readline(), timeout=timeout)
        except asyncio.TimeoutError:
            tail = session.stderr_tail()
            logger.warning("bot %s 决策超时 (%ss) stderr=%s", session_id, timeout, tail[:500])
            raise TimeoutError(f"bot {session_id} 决策超时 ({timeout}s)")
        if not raw:
            tail = session.stderr_tail()
            logger.warning("bot %s stdout EOF（进程退出码=%s）stderr=%s",
                           session_id, session.proc.returncode, tail[:500])
            raise BotCrashedError(
                f"bot {session_id} stdout EOF（进程退出码={session.proc.returncode}）"
            )
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

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
