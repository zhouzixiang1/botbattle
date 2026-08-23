"""运行时资源硬顶：全局双场执行与 Bot 上传容量。"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping, Sequence

from bzplat.backend.runtime.config import FULL_RR_MAX_N, MAX_CONCURRENT_MATCHES
from bzplat.backend.store.schema import (
    EXECUTION_ENV_HUMAN,
    EXECUTION_ENV_REMOTE_LOCAL,
)


@dataclass(frozen=True, slots=True)
class DockerResourceProfile:
    """平台管理的 Docker 资源档位。

    运行时只接受下方白名单中的完整档位，不向业务调用方开放
    任意 CPU/内存组合。
    """

    name: str
    cpus: int
    memory_mb: int

    @property
    def docker_cpus(self) -> str:
        return str(self.cpus)

    @property
    def docker_memory(self) -> str:
        return f"{self.memory_mb}m"


_LEGACY_PLATFORM_LOW_PROFILE = DockerResourceProfile(
    name="platform_low",
    cpus=1,
    memory_mb=512,
)
PLATFORM_LOW_PROFILE = DockerResourceProfile(
    name="platform_low",
    cpus=1,
    memory_mb=512,
)
PLATFORM_HIGH_PROFILE = DockerResourceProfile(
    name="platform_high",
    cpus=2,
    memory_mb=2048,
)
_EXECUTION_RESOURCE_PROFILE_V0: Mapping[str, DockerResourceProfile] = (
    MappingProxyType(
        {_LEGACY_PLATFORM_LOW_PROFILE.name: _LEGACY_PLATFORM_LOW_PROFILE}
    )
)
_EXECUTION_RESOURCE_PROFILE_V1: Mapping[str, DockerResourceProfile] = MappingProxyType(
    {
        PLATFORM_LOW_PROFILE.name: PLATFORM_LOW_PROFILE,
        PLATFORM_HIGH_PROFILE.name: PLATFORM_HIGH_PROFILE,
    }
)

# execution job 会长期跨版本排队/恢复，因此 profile_version 必须解析到创建
# job 时的历史规格，而不是部署时“当前”的同名常量。只允许追加新版本；旧映射
# 一旦发布不得修改或删除。v0 是迁移前仅有节能沙箱的历史契约，v1 增加赛事档。
EXECUTION_RESOURCE_PROFILE_REGISTRY: Mapping[
    int, Mapping[str, DockerResourceProfile]
] = MappingProxyType(
    {
        0: _EXECUTION_RESOURCE_PROFILE_V0,
        1: _EXECUTION_RESOURCE_PROFILE_V1,
    }
)
LATEST_EXECUTION_RESOURCE_PROFILE_VERSION = max(
    EXECUTION_RESOURCE_PROFILE_REGISTRY
)
LEGACY_EXECUTION_RESOURCE_PROFILE_VERSION = min(
    EXECUTION_RESOURCE_PROFILE_REGISTRY
)

# 当前非持久调用（上传预检等）的兼容入口；持久 execution 必须同时带版本并
# 通过 resolve_execution_resource_profile 解析。
DOCKER_RESOURCE_PROFILES: Mapping[str, DockerResourceProfile] = (
    EXECUTION_RESOURCE_PROFILE_REGISTRY[
        LATEST_EXECUTION_RESOURCE_PROFILE_VERSION
    ]
)

# 这两种执行端不消费平台 Docker 资源。名称与持久 execution contract 一致；
# 它们仍需一个已知 profile_version，以便损坏/未来未知 job 一律 fail closed。
_ZERO_RESOURCE_EXECUTION_ENVIRONMENTS = frozenset(
    {EXECUTION_ENV_REMOTE_LOCAL, EXECUTION_ENV_HUMAN}
)


@dataclass(frozen=True, slots=True)
class HostResourceBudget:
    """当前进程实际可用的主机 CPU 与内存硬预算。"""

    cpu_millis: int
    memory_mb: int


def _read_text(path: str) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return None


def _current_cgroups() -> list[tuple[tuple[str, ...], str]]:
    rows: list[tuple[tuple[str, ...], str]] = []
    for line in (_read_text("/proc/self/cgroup") or "").splitlines():
        fields = line.split(":", 2)
        if len(fields) != 3:
            continue
        controllers = tuple(
            item for item in fields[1].split(",") if item
        )
        relative = fields[2].strip().strip("/")
        if any(part in {"", ".", ".."} for part in relative.split("/") if relative):
            continue
        rows.append((controllers, relative))
    return rows


def _cgroup_ancestors(relative: str) -> tuple[str, ...]:
    """Return every non-root cgroup segment from parent through leaf.

    CPU and memory controllers are hierarchical: a leaf may say ``max`` while
    its parent slice imposes the effective limit.  Admission must therefore
    inspect every ancestor, not just the filesystem root and current leaf.
    """

    parts = tuple(part for part in str(relative).split("/") if part)
    return tuple("/".join(parts[:index]) for index in range(1, len(parts) + 1))


def _cgroup_v2_paths(filename: str) -> tuple[str, ...]:
    paths = [f"/sys/fs/cgroup/{filename}"]
    for controllers, relative in _current_cgroups():
        if controllers or not relative:
            continue
        paths.extend(
            f"/sys/fs/cgroup/{ancestor}/{filename}"
            for ancestor in _cgroup_ancestors(relative)
        )
    return tuple(dict.fromkeys(paths))


def _cgroup_v1_bases(controller: str) -> tuple[str, ...]:
    bases = [f"/sys/fs/cgroup/{controller}"]
    for controllers, relative in _current_cgroups():
        if controller not in controllers:
            continue
        mount_names = (controller, ",".join(controllers))
        for mount_name in mount_names:
            base = f"/sys/fs/cgroup/{mount_name}"
            bases.append(base)
            bases.extend(
                f"{base}/{ancestor}"
                for ancestor in _cgroup_ancestors(relative)
            )
    return tuple(dict.fromkeys(bases))


def _quota_cpu_millis(raw: str | None) -> int | None:
    if not raw:
        return None
    fields = raw.split()
    if len(fields) != 2 or fields[0] == "max":
        return None
    try:
        quota, period = (int(field) for field in fields)
    except ValueError:
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, quota * 1000 // period)


def _legacy_quota_cpu_millis(
    quota_raw: str | None, period_raw: str | None
) -> int | None:
    try:
        quota = int(str(quota_raw))
        period = int(str(period_raw))
    except (TypeError, ValueError):
        return None
    if quota <= 0 or period <= 0:
        return None
    return max(1, quota * 1000 // period)


def effective_host_cpu_millis() -> int:
    """返回 affinity 与 cgroup 共同约束后的 CPU 毫核数。"""

    candidates: list[int] = []
    try:
        affinity = len(os.sched_getaffinity(0))
    except (AttributeError, OSError):
        affinity = 0
    if affinity > 0:
        candidates.append(affinity * 1000)
    logical = os.cpu_count() or 0
    if logical > 0:
        candidates.append(int(logical) * 1000)
    for path in _cgroup_v2_paths("cpu.max"):
        quota = _quota_cpu_millis(_read_text(path))
        if quota is not None:
            candidates.append(quota)
    for base in _cgroup_v1_bases("cpu"):
        legacy = _legacy_quota_cpu_millis(
            _read_text(f"{base}/cpu.cfs_quota_us"),
            _read_text(f"{base}/cpu.cfs_period_us"),
        )
        if legacy is not None:
            candidates.append(legacy)
    # One millicore is a fail-closed fallback: no Docker profile can start.
    return max(1, min(candidates)) if candidates else 1


def _bounded_memory_mb(raw: str | None) -> int | None:
    if not raw or raw == "max":
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    # cgroup v1 uses values close to signed 64-bit max for "unlimited".
    if value <= 0 or value >= 1 << 60:
        return None
    return max(1, value // (1024 * 1024))


def effective_host_memory_mb() -> int:
    """返回物理内存与 cgroup 共同约束后的 MiB 数。"""

    candidates: list[int] = []
    memory_paths = list(_cgroup_v2_paths("memory.max"))
    memory_paths.extend(
        f"{base}/memory.limit_in_bytes"
        for base in _cgroup_v1_bases("memory")
    )
    for path in dict.fromkeys(memory_paths):
        bounded = _bounded_memory_mb(_read_text(path))
        if bounded is not None:
            candidates.append(bounded)
    meminfo = _read_text("/proc/meminfo") or ""
    for line in meminfo.splitlines():
        fields = line.split()
        if len(fields) >= 2 and fields[0] == "MemTotal:":
            try:
                candidates.append(max(1, int(fields[1]) // 1024))
            except ValueError:
                pass
            break
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        pages = page_size = 0
    if pages > 0 and page_size > 0:
        candidates.append(max(1, pages * page_size // (1024 * 1024)))
    # One MiB is deliberately below every runnable Docker profile.
    return max(1, min(candidates)) if candidates else 1


def effective_host_resource_budget() -> HostResourceBudget:
    """冻结供执行队列使用的进程可见主机资源上限。"""

    return HostResourceBudget(
        cpu_millis=effective_host_cpu_millis(),
        memory_mb=effective_host_memory_mb(),
    )


def _execution_profiles_for_version(
    profile_version: int,
) -> Mapping[str, DockerResourceProfile]:
    if isinstance(profile_version, bool) or not isinstance(profile_version, int):
        raise ValueError("执行资源档位版本必须是整数")
    profiles = EXECUTION_RESOURCE_PROFILE_REGISTRY.get(profile_version)
    if profiles is None:
        raise ValueError(f"未知执行资源档位版本: {profile_version}")
    return profiles


def resolve_execution_resource_profile(
    environment: str,
    profile_version: int,
) -> DockerResourceProfile:
    """按持久 job 的版本解析 Docker 档位，未知组合一律拒绝。"""

    profiles = _execution_profiles_for_version(profile_version)
    resolved = profiles.get(environment)
    if resolved is None:
        raise ValueError(
            f"执行资源档位版本 {profile_version} 不支持 Docker 环境: "
            f"{environment}"
        )
    return resolved


def execution_resource_snapshot(
    environments: Sequence[str],
    profile_version: int,
) -> tuple[int, int, int]:
    """用同一历史注册表计算 ``(sandbox units, CPU 毫核, MiB)``。"""

    profiles = _execution_profiles_for_version(profile_version)
    selected: list[DockerResourceProfile] = []
    for environment in environments:
        if environment in _ZERO_RESOURCE_EXECUTION_ENVIRONMENTS:
            continue
        profile = profiles.get(environment)
        if profile is None:
            raise ValueError(
                f"执行资源档位版本 {profile_version} 不支持执行环境: "
                f"{environment}"
            )
        selected.append(profile)
    return (
        len(selected),
        sum(int(profile.cpus) * 1000 for profile in selected),
        sum(int(profile.memory_mb) for profile in selected),
    )


def maximum_execution_match_resource_snapshot() -> tuple[int, int, int]:
    """Return a fail-closed resource vector for an untracked two-Bot match.

    A legacy running Match has no durable execution job from which to recover
    its historical profile.  Admission therefore charges the independent
    maximum of every immutable profile version.  Keeping this derivation next
    to the append-only registry prevents recovery accounting from drifting
    when a higher resource tier is added later.
    """

    snapshots = [
        execution_resource_snapshot((name, name), profile_version)
        for profile_version, profiles in EXECUTION_RESOURCE_PROFILE_REGISTRY.items()
        for name in profiles
    ]
    if not snapshots:
        raise RuntimeError("执行资源档位注册表不能为空")
    return (
        max(snapshot[0] for snapshot in snapshots),
        max(snapshot[1] for snapshot in snapshots),
        max(snapshot[2] for snapshot in snapshots),
    )


def resolve_docker_resource_profile(
    profile: str | DockerResourceProfile = PLATFORM_LOW_PROFILE,
) -> DockerResourceProfile:
    """返回白名单中的规范档位，拒绝伪造或未知资源值。"""
    if isinstance(profile, str):
        resolved = DOCKER_RESOURCE_PROFILES.get(profile)
        if resolved is None:
            raise ValueError(f"未知 Docker 资源档位: {profile}")
        return resolved
    if not isinstance(profile, DockerResourceProfile):
        raise TypeError("Docker 资源档位必须是平台白名单值")
    # 先按 identity 找到调用方已经从历史注册表取得的规范对象，避免当前和
    # legacy 规格值暂时相同的时候把旧 job 悄悄改绑到当前版本。
    for profiles in EXECUTION_RESOURCE_PROFILE_REGISTRY.values():
        resolved = profiles.get(profile.name)
        if resolved is profile:
            return resolved
    # 保留旧 API 对值相等 dataclass 的兼容；未知/任意放大的组合仍拒绝。
    for profiles in EXECUTION_RESOURCE_PROFILE_REGISTRY.values():
        resolved = profiles.get(profile.name)
        if resolved == profile:
            return resolved
    raise ValueError(f"未知 Docker 资源档位: {profile.name}")


# 保留旧名作为默认低配的只读兼容常量。
BOT_CPUS = float(PLATFORM_LOW_PROFILE.cpus)
BOT_MEMORY_MB = PLATFORM_LOW_PROFILE.memory_mb
# 上传会在进程内保留一份 bytes，并由进程级 admission 串行预检。100 MiB
# 足以覆盖常见 PyInstaller 单文件产物，同时避免无界内存/磁盘占用。
MAX_BOT_UPLOAD_BYTES = 100 * 1024 * 1024
# 单次 Bot stdout 响应行的传输硬顶。StreamReader 与协议解析共用同一常量，
# 防止超长无换行输出先撑大进程内存、随后才在业务层判错。
MAX_BOT_RESPONSE_LINE_BYTES = 64 * 1024
# 本地 Bot WebSocket 控制消息除 stdout 响应外还包含固定信封字段；传输层
# 必须在解码前使用同一上限，不能依赖应用收到整帧后再拒绝。
MAX_LOCAL_AI_WEBSOCKET_MESSAGE_BYTES = MAX_BOT_RESPONSE_LINE_BYTES * 4


def cpu_count() -> int:
    return os.cpu_count() or 1


def concurrent_ceiling() -> int:
    """全站执行硬顶：每 4 个可见逻辑核一场，且合计最多两场。"""
    return min(MAX_CONCURRENT_MATCHES, max(1, cpu_count() // 4))


def clamp_concurrent(requested: int) -> int:
    return max(1, min(int(requested), concurrent_ceiling()))


def default_max_concurrent() -> int:
    return min(MAX_CONCURRENT_MATCHES, concurrent_ceiling())
