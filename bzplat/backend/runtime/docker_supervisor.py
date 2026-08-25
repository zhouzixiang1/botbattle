"""Small, local-only Docker control plane for execution jobs.

Every command pins the canonical Unix socket.  The supervisor never reads or
switches Docker contexts and never tries to identify a daemon process.  A
control command is either acknowledged and verified by labels, or reported as
uncertain so the durable dispatcher can pause without releasing capacity.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Iterable

from bzplat.backend.runtime.limits import (
    DockerResourceProfile,
    PLATFORM_LOW_PROFILE,
    resolve_docker_resource_profile,
)


logger = logging.getLogger(__name__)

CANONICAL_DOCKER_HOST = "unix:///var/run/docker.sock"
INSTANCE_LABEL = "io.botbattle.instance"
JOB_LABEL = "io.botbattle.job"
ATTEMPT_LABEL = "io.botbattle.attempt"
SLOT_LABEL = "io.botbattle.slot"
LAUNCH_LABEL = "io.botbattle.launch"
_CONTROL_TIMEOUT = 15.0
_START_CONFIRM_TIMEOUT = 5.0
_INSTANCE_RE = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,47}$")


class DockerSupervisorError(RuntimeError):
    """A definite local Docker configuration/control failure."""


class DockerControlUncertain(DockerSupervisorError):
    """Docker may have applied a create/inspect/remove operation."""


class DockerCreateAmbiguous(DockerControlUncertain):
    """A create intent exists without a trustworthy daemon acknowledgement."""


def host_boot_id() -> str:
    """Return the Linux host boot identity used by durable launch recovery."""
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip()
    except OSError as exc:
        raise DockerControlUncertain("无法读取主机 boot id") from exc
    if not value or len(value) > 128:
        raise DockerControlUncertain("主机 boot id 无效")
    return value


async def _drain_shielded(task: asyncio.Task[Any]) -> tuple[Any, bool]:
    """Await a non-abandonable local operation and remember cancellation."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return task.result(), cancelled


def validate_local_docker_configuration() -> None:
    """Reject an explicit Botbattle remote socket override.

    Shell-level Docker context/host variables are deliberately ignored: every
    child command receives a sanitized environment and an explicit ``--host``.
    This keeps the boundary local without making an unrelated developer shell
    context a production startup blocker.
    """

    configured = os.environ.get("BZ_DOCKER_HOST", CANONICAL_DOCKER_HOST).strip()
    if configured != CANONICAL_DOCKER_HOST:
        raise DockerSupervisorError(
            "BZ_DOCKER_HOST 只允许 unix:///var/run/docker.sock"
        )


def docker_cli_environment() -> dict[str, str]:
    """Return a child environment pinned to the canonical local socket."""

    env = os.environ.copy()
    env["DOCKER_HOST"] = CANONICAL_DOCKER_HOST
    for key in ("DOCKER_CONTEXT", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH"):
        env.pop(key, None)
    return env


def instance_namespace(db_path: str | Path, explicit_key: str | None = None) -> str:
    key = (explicit_key or os.environ.get("BZ_INSTANCE_KEY", "")).strip().lower()
    if key:
        if not _INSTANCE_RE.fullmatch(key):
            raise DockerSupervisorError(
                "BZ_INSTANCE_KEY 必须是 1-48 位小写字母、数字或 ._-"
            )
        return key
    absolute = str(Path(db_path).expanduser().resolve())
    return "db-" + hashlib.sha256(absolute.encode("utf-8")).hexdigest()[:20]


def _safe_fragment(value: str, *, size: int = 16) -> str:
    cleaned = re.sub(r"[^a-z0-9_.-]", "-", value.lower()).strip("-._")
    if cleaned:
        return cleaned[:size]
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:size]


@dataclass(frozen=True, slots=True)
class DockerExecutionIdentity:
    instance: str
    job_public_id: str
    attempt_no: int

    def container_name(self, slot: int) -> str:
        job_hash = hashlib.sha256(self.job_public_id.encode("utf-8")).hexdigest()[:12]
        return (
            f"bzplat-{_safe_fragment(self.instance, size=14)}-"
            f"{job_hash}-a{int(self.attempt_no)}-s{int(slot)}"
        )[:63]

    def labels(
        self, slot: int, *, launch_token: str | None = None
    ) -> tuple[tuple[str, str], ...]:
        labels = (
            (INSTANCE_LABEL, self.instance),
            (JOB_LABEL, self.job_public_id),
            (ATTEMPT_LABEL, str(int(self.attempt_no))),
            (SLOT_LABEL, str(int(slot))),
        )
        if launch_token is not None:
            return (*labels, (LAUNCH_LABEL, launch_token))
        return labels


class DockerSupervisor:
    """Deterministic command builder plus bounded label verification."""

    def __init__(
        self,
        *,
        db_path: str | Path,
        docker_bin: str = "docker",
        instance_key: str | None = None,
        launch_journal: Any | None = None,
    ) -> None:
        validate_local_docker_configuration()
        if shutil.which(docker_bin) is None:
            raise DockerSupervisorError("Docker CLI 不可用")
        self.docker_bin = docker_bin
        resolved_db = Path(db_path).expanduser().resolve()
        self.instance = instance_namespace(resolved_db, instance_key)
        self.launch_journal = launch_journal
        self._launch_lock_path = Path(str(resolved_db) + ".docker-launch.lock")

    def _acquire_launch_lock(self) -> int:
        fd = os.open(self._launch_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
        except BaseException:
            os.close(fd)
            raise
        return fd

    @staticmethod
    def _release_launch_lock(fd: int) -> None:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @asynccontextmanager
    async def launch_guard(self) -> AsyncIterator[None]:
        """Serialize every create/start/cleanup across loops and processes."""
        acquire = asyncio.create_task(
            asyncio.to_thread(self._acquire_launch_lock),
            name="docker-launch-flock-acquire",
        )
        fd, cancelled = await _drain_shielded(acquire)
        if cancelled:
            release = asyncio.create_task(
                asyncio.to_thread(self._release_launch_lock, fd),
                name="docker-launch-flock-cancel-release",
            )
            await _drain_shielded(release)
            raise asyncio.CancelledError
        release_cancelled = False
        try:
            yield
        finally:
            release = asyncio.create_task(
                asyncio.to_thread(self._release_launch_lock, fd),
                name="docker-launch-flock-release",
            )
            _, release_cancelled = await _drain_shielded(release)
        if release_cancelled:
            raise asyncio.CancelledError

    @property
    def prefix(self) -> list[str]:
        return [self.docker_bin, "--host", CANONICAL_DOCKER_HOST]

    def command(self, *args: str) -> list[str]:
        return [*self.prefix, *args]

    @staticmethod
    def sandbox_options(
        *,
        identity: DockerExecutionIdentity,
        slot: int,
        name: str,
        binary_path: Path,
        image: str,
        profile: DockerResourceProfile = PLATFORM_LOW_PROFILE,
        launch_token: str | None = None,
    ) -> list[str]:
        profile = resolve_docker_resource_profile(profile)
        labels: list[str] = []
        for key, value in identity.labels(slot, launch_token=launch_token):
            labels.extend(["--label", f"{key}={value}"])
        return [
            "--pull=never",
            "--name",
            name,
            *labels,
            "--network=none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,exec,nosuid,nodev,size=64m",
            "--cap-drop=ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            f"--cpus={profile.docker_cpus}",
            f"--memory={profile.docker_memory}",
            f"--memory-swap={profile.docker_memory}",
            "--pids-limit=64",
            "--ulimit",
            "nofile=64:64",
            "--ulimit",
            "nproc=64:64",
            "--log-driver=none",
            "--platform",
            "linux/amd64",
            "--volume",
            f"{binary_path}:/app/bot:ro",
            "--workdir",
            "/app",
            "--entrypoint",
            "/app/bot",
            image,
        ]

    def _run(
        self,
        args: Iterable[str],
        *,
        timeout: float = _CONTROL_TIMEOUT,
        uncertain: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        command = self.command(*list(args))
        try:
            return subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(0.001, timeout),
                check=False,
                env=docker_cli_environment(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            error = DockerControlUncertain if uncertain else DockerSupervisorError
            raise error(
                f"Docker 控制命令结果不确定（{type(exc).__name__}）"
            ) from exc

    def list_ids(
        self,
        *,
        job_public_id: str | None = None,
        attempt_no: int | None = None,
        launch_token: str | None = None,
    ) -> list[str]:
        args = ["ps", "-aq", "--filter", f"label={INSTANCE_LABEL}={self.instance}"]
        if job_public_id is not None:
            args.extend(["--filter", f"label={JOB_LABEL}={job_public_id}"])
        if attempt_no is not None:
            args.extend(["--filter", f"label={ATTEMPT_LABEL}={int(attempt_no)}"])
        if launch_token is not None:
            args.extend(["--filter", f"label={LAUNCH_LABEL}={launch_token}"])
        result = self._run(args)
        if result.returncode != 0:
            raise DockerControlUncertain("Docker 容器标签查询失败")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def list_name_ids(self, name: str) -> list[str]:
        result = self._run(
            ["ps", "-aq", "--filter", f"name=^/{name}$"]
        )
        if result.returncode != 0:
            raise DockerControlUncertain("Docker 容器名称查询失败")
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    @staticmethod
    def _parse_labels(inspected: subprocess.CompletedProcess[str]) -> dict[str, str]:
        try:
            labels = json.loads(inspected.stdout or "{}")
        except (TypeError, ValueError) as exc:
            raise DockerControlUncertain("Docker 容器 label 响应不可解析") from exc
        if not isinstance(labels, dict):
            raise DockerControlUncertain("Docker 容器 label 响应不是对象")
        return {str(key): str(value) for key, value in labels.items()}

    def inspect_existing_labels(self, reference: str) -> dict[str, str]:
        inspected = self._run(
            ["inspect", "--format", "{{json .Config.Labels}}", reference]
        )
        if inspected.returncode != 0:
            raise DockerControlUncertain("已观察 Docker 容器无法 inspect")
        return self._parse_labels(inspected)

    def inspect_labels(self, name: str) -> dict[str, str] | None:
        """Return exact-name labels, proving absence with a second name query."""
        inspected = self._run(
            ["inspect", "--format", "{{json .Config.Labels}}", name]
        )
        if inspected.returncode != 0:
            if self.list_name_ids(name):
                raise DockerControlUncertain(
                    "Docker exact-name inspect 与名称查询不一致"
                )
            return None
        return self._parse_labels(inspected)

    def create(
        self,
        *,
        identity: DockerExecutionIdentity,
        slot: int,
        launch_token: str,
        owner_kind: str,
        binary_path: Path,
        image: str,
        profile: DockerResourceProfile = PLATFORM_LOW_PROFILE,
    ) -> str:
        profile = resolve_docker_resource_profile(profile)
        name = identity.container_name(slot)
        journal = getattr(self, "launch_journal", None)
        if journal is None:
            raise DockerControlUncertain("Docker launch journal 未配置")
        boot_id = host_boot_id()
        try:
            journal.begin_docker_launch(
                launch_token=launch_token,
                instance_key=self.instance,
                owner_kind=owner_kind,
                job_public_id=identity.job_public_id,
                attempt_no=identity.attempt_no,
                slot=slot,
                container_name=name,
                host_boot_id=boot_id,
            )
        except Exception as exc:
            # A foreground enqueue may have synchronously marked an automatic
            # attempt for yield after claim but before Docker create.  The
            # journal performs the authoritative check under BEGIN IMMEDIATE;
            # this is a definite rejection, not uncertain Docker control.
            if getattr(exc, "code", "") == "execution_attempt_not_current":
                raise
            logger.exception(
                "Docker launch intent persistence failed owner=%s job=%s attempt=%s slot=%s",
                owner_kind,
                identity.job_public_id,
                identity.attempt_no,
                slot,
            )
            raise DockerControlUncertain(
                "Docker create intent 无法持久化"
            ) from exc
        options = self.sandbox_options(
            identity=identity,
            slot=slot,
            name=name,
            binary_path=binary_path,
            image=image,
            profile=profile,
            launch_token=launch_token,
        )
        try:
            result = self._run(["create", "-i", *options])
        except DockerControlUncertain as exc:
            raise DockerCreateAmbiguous("Docker create 控制请求结果不确定") from exc
        if result.returncode != 0:
            raise DockerCreateAmbiguous(
                f"Docker create 未确认（exit {result.returncode}）"
            )
        expected = set(identity.labels(slot, launch_token=launch_token))
        try:
            labels = self.inspect_labels(name)
        except DockerControlUncertain as exc:
            raise DockerCreateAmbiguous(
                "Docker create 后 inspect 结果不确定"
            ) from exc
        if labels is None:
            raise DockerCreateAmbiguous("Docker create 后 exact-name inspect 未确认")
        if any(labels.get(k) != v for k, v in expected):
            raise DockerCreateAmbiguous("Docker 容器 label 与执行任务不匹配")
        try:
            journal.mark_docker_launch_created(launch_token)
        except Exception as exc:
            raise DockerCreateAmbiguous(
                "Docker create ACK 无法写入 launch journal"
            ) from exc
        return name

    def _started_at(self, name: str) -> bool:
        inspected = self._run(
            ["inspect", "--format", "{{.State.StartedAt}}", name],
            timeout=min(_CONTROL_TIMEOUT, _START_CONFIRM_TIMEOUT),
        )
        if inspected.returncode != 0:
            raise DockerControlUncertain("Docker start 后 inspect 未确认")
        started_at = inspected.stdout.strip()
        return bool(
            started_at
            and started_at not in {"<no value>", "0001-01-01T00:00:00Z"}
        )

    @staticmethod
    async def _terminate_control_process(proc: Any) -> None:
        """Terminate and await a local docker CLI process without abandoning it."""
        if getattr(proc, "returncode", None) is None:
            terminate = getattr(proc, "terminate", None)
            if callable(terminate):
                try:
                    terminate()
                except ProcessLookupError:
                    pass
        wait = getattr(proc, "wait", None)
        if not callable(wait):
            return
        try:
            await asyncio.wait_for(wait(), timeout=2.0)
            return
        except (asyncio.TimeoutError, ProcessLookupError):
            pass
        kill = getattr(proc, "kill", None)
        if callable(kill):
            try:
                kill()
            except ProcessLookupError:
                pass
        try:
            await wait()
        except ProcessLookupError:
            pass

    async def start_attached(
        self,
        name: str,
        *,
        stream_limit: int,
        confirm_timeout: float = _START_CONFIRM_TIMEOUT,
        launch_token: str | None = None,
    ) -> asyncio.subprocess.Process:
        """Attach to a container and prove the daemon actually started it.

        Merely spawning ``docker start -a`` is not an acknowledgement: socket,
        image, runtime and mount failures can make that CLI exit before the
        container ever starts.  Only a non-zero daemon ``StartedAt`` transfers
        subsequent process exits into the Bot-attributable path.
        """

        try:
            proc = await asyncio.create_subprocess_exec(
                *self.command("start", "-a", "-i", name),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=stream_limit,
                env=docker_cli_environment(),
            )
        except OSError as exc:
            raise DockerControlUncertain(
                f"Docker start 控制进程未启动（{type(exc).__name__}）"
            ) from exc

        try:
            deadline = asyncio.get_running_loop().time() + max(
                0.05, float(confirm_timeout)
            )
            while True:
                if await asyncio.to_thread(self._started_at, name):
                    if launch_token is not None:
                        journal = getattr(self, "launch_journal", None)
                        if journal is None:
                            raise DockerControlUncertain(
                                "Docker launch journal 未配置"
                            )
                        try:
                            journal.clear_docker_launch_created(launch_token)
                        except Exception as exc:
                            raise DockerControlUncertain(
                                "Docker StartedAt 后 journal 未能收敛"
                            ) from exc
                    return proc
                if proc.returncode is not None:
                    raise DockerControlUncertain(
                        f"Docker start 未进入 StartedAt（exit {proc.returncode}）"
                    )
                if asyncio.get_running_loop().time() >= deadline:
                    raise DockerControlUncertain(
                        "Docker start 在限时内未确认 StartedAt"
                    )
                await asyncio.sleep(0.05)
        except BaseException:
            cleanup = asyncio.create_task(
                self._terminate_control_process(proc),
                name=f"docker-start-control-stop-{name}",
            )
            await _drain_shielded(cleanup)
            raise

    def remove_names(self, names: Iterable[str]) -> None:
        exact = [name for name in names if name]
        if not exact:
            return
        result = self._run(["rm", "-f", *exact])
        if result.returncode != 0:
            raise DockerControlUncertain("Docker rm 未确认")

    def _journal_snapshot(self) -> dict[str, Any]:
        journal = getattr(self, "launch_journal", None)
        if journal is None:
            raise DockerControlUncertain("Docker launch journal 未配置")
        try:
            launch = journal.docker_launch()
        except Exception as exc:
            raise DockerControlUncertain(
                "Docker launch journal 无法读取"
            ) from exc
        if launch.get("state") != "idle" and launch.get("instance_key") != self.instance:
            raise DockerControlUncertain(
                "Docker launch journal instance 与 supervisor 不一致"
            )
        return launch

    @staticmethod
    def _launch_matches_identity(
        launch: dict[str, Any], identity: DockerExecutionIdentity | None
    ) -> bool:
        if launch.get("state") == "idle":
            return False
        if identity is None:
            return True
        return (
            launch.get("job_public_id") == identity.job_public_id
            and int(launch.get("attempt_no") or 0) == int(identity.attempt_no)
        )

    def _expected_journal_labels(
        self, launch: dict[str, Any]
    ) -> dict[str, str]:
        return {
            INSTANCE_LABEL: str(launch["instance_key"]),
            JOB_LABEL: str(launch["job_public_id"]),
            ATTEMPT_LABEL: str(int(launch["attempt_no"])),
            SLOT_LABEL: str(int(launch["slot"])),
            LAUNCH_LABEL: str(launch["launch_token"]),
        }

    def _observe_journal_container(
        self, launch: dict[str, Any]
    ) -> dict[str, Any]:
        """Promote creating only after one exact token/name identity is observed."""
        if launch.get("state") != "creating":
            return launch
        name = str(launch["container_name"])
        labels = self.inspect_labels(name)
        if labels is None:
            launch_ids = self.list_ids(
                launch_token=str(launch["launch_token"])
            )
            if len(launch_ids) > 1:
                raise DockerControlUncertain(
                    "同一 Docker launch token 对应多个容器"
                )
            if launch_ids:
                labels = self.inspect_existing_labels(launch_ids[0])
        if labels is None:
            return launch
        expected = self._expected_journal_labels(launch)
        if any(labels.get(key) != value for key, value in expected.items()):
            raise DockerControlUncertain(
                "Docker journal 容器 identity/launch label 不匹配"
            )
        journal = getattr(self, "launch_journal")
        try:
            return journal.mark_docker_launch_created(
                str(launch["launch_token"])
            )
        except Exception as exc:
            raise DockerControlUncertain(
                "已观察 Docker 容器但 journal 无法确认"
            ) from exc

    def _cleanup_ids(
        self,
        *,
        identity: DockerExecutionIdentity | None,
        launch: dict[str, Any],
        matching_launch: bool,
    ) -> list[str]:
        if identity is None:
            found = set(self.list_ids())
        else:
            found = set(
                self.list_ids(
                    job_public_id=identity.job_public_id,
                    attempt_no=identity.attempt_no,
                )
            )
        if matching_launch:
            found.update(
                self.list_ids(launch_token=str(launch["launch_token"]))
            )
            found.update(self.list_name_ids(str(launch["container_name"])))
        return sorted(found)

    def _settle_clean_journal(
        self, launch: dict[str, Any], *, current_boot_id: str
    ) -> None:
        journal = getattr(self, "launch_journal")
        token = str(launch["launch_token"])
        try:
            if launch["state"] == "created":
                journal.clear_docker_launch_created(token)
                return
            if launch["state"] != "creating":
                return
            previous_boot_id = str(launch["host_boot_id"])
            if previous_boot_id == current_boot_id:
                raise DockerCreateAmbiguous(
                    "manual:同一 host boot 的 Docker create 未获 ACK；"
                    "label/name 双零不能排除迟到容器"
                )
            journal.clear_docker_launch_after_boot_change(
                token,
                previous_boot_id=previous_boot_id,
                current_boot_id=current_boot_id,
            )
        except DockerCreateAmbiguous:
            raise
        except Exception as exc:
            raise DockerControlUncertain(
                "Docker cleanup 后 journal 无法收敛"
            ) from exc

    async def _cleanup_locked(
        self,
        identity: DockerExecutionIdentity | None,
        *,
        zero_polls: int,
        max_polls: int,
    ) -> None:
        launch = self._journal_snapshot()
        matching_launch = self._launch_matches_identity(launch, identity)
        if matching_launch:
            launch = await asyncio.to_thread(
                self._observe_journal_container, launch
            )
        ids = await asyncio.to_thread(
            self._cleanup_ids,
            identity=identity,
            launch=launch,
            matching_launch=matching_launch,
        )
        if ids:
            await asyncio.to_thread(self.remove_names, ids)
        consecutive_zero = 0
        for _ in range(max(1, int(max_polls))):
            launch = self._journal_snapshot()
            matching_launch = self._launch_matches_identity(launch, identity)
            if matching_launch and launch.get("state") == "creating":
                launch = await asyncio.to_thread(
                    self._observe_journal_container, launch
                )
            remaining = await asyncio.to_thread(
                self._cleanup_ids,
                identity=identity,
                launch=launch,
                matching_launch=matching_launch,
            )
            if not remaining:
                consecutive_zero += 1
                if consecutive_zero >= max(1, int(zero_polls)):
                    if matching_launch:
                        await asyncio.to_thread(
                            self._settle_clean_journal,
                            launch,
                            current_boot_id=host_boot_id(),
                        )
                    return
            else:
                consecutive_zero = 0
                await asyncio.to_thread(self.remove_names, remaining)
            await asyncio.sleep(0.05)
        raise DockerControlUncertain("Docker rm 后 label/name 未确认为 0")

    async def cleanup_job(
        self, identity: DockerExecutionIdentity, *, zero_polls: int = 2
    ) -> None:
        async with self.launch_guard():
            await self._cleanup_locked(
                identity,
                zero_polls=zero_polls,
                max_polls=6,
            )

    def _exact_session_ids(
        self,
        identity: DockerExecutionIdentity,
        *,
        slot: int,
        name: str,
        launch_token: str,
    ) -> list[str]:
        """Resolve one session only when name, token and all labels agree."""
        if not launch_token or name != identity.container_name(slot):
            raise DockerControlUncertain("Docker 会话清理身份不完整")
        token_ids = set(
            self.list_ids(
                job_public_id=identity.job_public_id,
                attempt_no=identity.attempt_no,
                launch_token=launch_token,
            )
        )
        name_ids = set(self.list_name_ids(name))
        # Docker names are unique and launch tokens are UUIDs. Any disagreement
        # means an external rename/collision or an incomplete control response;
        # never widen removal to the whole job from this per-turn path.
        if token_ids != name_ids or len(token_ids) > 1:
            raise DockerControlUncertain(
                "Docker 会话 name/token 查询结果不一致"
            )
        expected = dict(identity.labels(slot, launch_token=launch_token))
        for container_id in token_ids:
            labels = self.inspect_existing_labels(container_id)
            if any(labels.get(key) != value for key, value in expected.items()):
                raise DockerControlUncertain(
                    "Docker 会话 label 与执行任务不匹配"
                )
        return sorted(token_ids)

    async def cleanup_session(
        self,
        identity: DockerExecutionIdentity,
        *,
        slot: int,
        name: str,
        launch_token: str,
        zero_polls: int = 2,
    ) -> None:
        """Remove one physical Bot session without touching sibling seats.

        Successful ``start_attached`` has already closed its durable create
        journal entry. Holding the shared launch flock prevents another create
        from racing the exact token/name proof below. Two consecutive zero
        observations make a Traditional turn release its container immediately
        while the job-level cleanup remains the final attempt-wide barrier.
        """
        async with self.launch_guard():
            launch = self._journal_snapshot()
            if launch.get("state") != "idle":
                raise DockerControlUncertain(
                    "Docker launch journal 未闭合，拒绝单会话清理"
                )
            consecutive_zero = 0
            for _ in range(6):
                ids = await asyncio.to_thread(
                    self._exact_session_ids,
                    identity,
                    slot=slot,
                    name=name,
                    launch_token=launch_token,
                )
                if not ids:
                    consecutive_zero += 1
                    if consecutive_zero >= max(1, int(zero_polls)):
                        return
                else:
                    consecutive_zero = 0
                    await asyncio.to_thread(self.remove_names, ids)
                await asyncio.sleep(0.05)
        raise DockerControlUncertain(
            "Docker 会话 rm 后 name/token 未确认为 0"
        )

    async def cleanup_instance(self) -> None:
        """Startup-only exact namespace cleanup and double-zero proof."""
        async with self.launch_guard():
            await self._cleanup_locked(
                None,
                zero_polls=2,
                max_polls=8,
            )

    def launch_requires_manual_recovery(self) -> bool:
        """True only for unacknowledged create on the current host boot."""
        launch = self._journal_snapshot()
        return bool(
            launch.get("state") == "creating"
            and str(launch.get("host_boot_id") or "") == host_boot_id()
        )


__all__ = [
    "ATTEMPT_LABEL",
    "CANONICAL_DOCKER_HOST",
    "DockerControlUncertain",
    "DockerCreateAmbiguous",
    "DockerExecutionIdentity",
    "DockerSupervisor",
    "DockerSupervisorError",
    "INSTANCE_LABEL",
    "JOB_LABEL",
    "LAUNCH_LABEL",
    "docker_cli_environment",
    "host_boot_id",
    "instance_namespace",
    "validate_local_docker_configuration",
]
