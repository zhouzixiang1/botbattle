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
DEFAULT_CPUS = "0.5"
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
                            action_timeout: float = DEFAULT_ACTION_TIMEOUT) -> str:
        path = Path(binary_path).resolve()
        if not path.is_file():
            raise FileNotFoundError(str(path))
        raw = path.read_bytes()[:4096]
        info = info or classify_binary(raw if len(raw) >= 4 else path.read_bytes())
        if not info.runnable:
            raise BinaryRejectError(info.reject_reason or "不可执行的二进制")

        sid = uuid.uuid4().hex[:12]
        mode = self._select_mode(info)
        session = BotSession(session_id=sid, info=info, binary_path=path, mode=mode)
        if mode == "local":
            await self._start_local(session)
        elif mode == "wine":
            await self._start_wine(session)
        else:
            await self._start_docker(session)
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
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
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
            raise TimeoutError(f"bot {session_id} 决策超时 ({timeout}s)")
        if not raw:
            raise RuntimeError(f"bot {session_id} stdout EOF")
        return raw.decode("utf-8", errors="replace").rstrip("\r\n")

    async def stop_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if not session:
            return
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
