"""构建容器 launch journal 收尾回归。

P1 不变量：``run_build`` 在 create 阶段的三条失败路径（create 不确定 /
create 确定性失败 / mark_created 持久化失败）都必须把 journal 收敛回
idle，或在同 host boot 零证据时转为可见的 dispatcher pause（经
``docker_uncertain_callback``），绝不静默遗留 creating 卡死后续对局启动。
"""
from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest

from bzplat.backend.runtime import binary_runner as binary_runner_module
from bzplat.backend.runtime.docker_supervisor import (
    ATTEMPT_LABEL,
    INSTANCE_LABEL,
    JOB_LABEL,
    LAUNCH_LABEL,
    SLOT_LABEL,
    DockerControlUncertain,
    DockerCreateAmbiguous,
    DockerExecutionIdentity,
    DockerSupervisor,
)
from bzplat.backend.runtime.limits import BOT_BUILD_PROFILE
from bzplat.backend.store.db import Store
from bzplat.backend.store.execution import DockerLaunchInvariantError

INSTANCE = "test-build-journal"


class FakeDocker:
    """按 args[0] 分发的最小 docker CLI 假体，维护 name→(id, labels) 状态。

    ``create_mode``：``ok`` 正常创建；``fail`` 确定性 rc=1（容器未创建）；
    ``uncertain`` 模拟 subprocess 超时——但 ``late=True`` 时容器实际已
    建成，用于复现"迟到容器"竞态。
    """

    def __init__(
        self,
        *,
        create_mode: str = "ok",
        late: bool = False,
        rename_after_create: bool = False,
    ) -> None:
        self.containers: dict[str, dict[str, Any]] = {}
        self._next = 0
        self.create_mode = create_mode
        self.late = late
        self.rename_after_create = rename_after_create

    def _new_id(self) -> str:
        self._next += 1
        return f"cid{self._next:04d}"

    def _ref_lookup(self, ref: str) -> dict[str, Any] | None:
        for info in self.containers.values():
            if info["id"] == ref or info["name"] == ref:
                return info
        return None

    def run(
        self,
        args: list[str],
        *,
        timeout: float = 15.0,
        uncertain: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        argv = list(args)
        cmd = argv[0]
        if cmd == "create":
            name = ""
            labels: dict[str, str] = {}
            rest = iter(argv[1:])
            for token in rest:
                if token == "--name":
                    name = next(rest)
                elif token == "--label":
                    key, _, value = next(rest).partition("=")
                    labels[key] = value
            if self.create_mode == "uncertain":
                if self.late:
                    info = {
                        "id": self._new_id(), "name": name, "labels": labels,
                    }
                    if self.rename_after_create:
                        info["name"] = f"{name}-renamed"
                    self.containers[info["name"]] = info
                raise DockerControlUncertain("Docker 控制命令结果不确定（TimeoutExpired）")
            if self.create_mode == "fail":
                return subprocess.CompletedProcess(
                    argv, 1, stdout="", stderr="docker: no such image"
                )
            self.containers[name] = {
                "id": self._new_id(), "name": name, "labels": labels,
            }
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if cmd == "ps":
            specs: list[str] = []
            it = iter(argv[2:])
            for token in it:
                if token == "--filter":
                    specs.append(next(it))
            label_filters = {
                spec[len("label="):].partition("=")[0]: spec[len("label="):].partition("=")[2]
                for spec in specs
                if spec.startswith("label=")
            }
            name_spec = next(
                (spec for spec in specs if spec.startswith("name=")), None
            )
            name_filter = (
                name_spec[len("name="):].removeprefix("^").removesuffix("$")
                if name_spec is not None
                else None
            )
            matched = [
                info["id"]
                for info in self.containers.values()
                if all(
                    info["labels"].get(key) == value
                    for key, value in label_filters.items()
                )
                and (
                    name_filter is None
                    or f"/{info['name']}" == name_filter
                )
            ]
            return subprocess.CompletedProcess(argv, 0, "\n".join(matched), "")
        if cmd == "inspect":
            ref = argv[-1]
            fmt = argv[argv.index("--format") + 1]
            info = self._ref_lookup(ref)
            if info is None:
                return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no such object")
            if "Config.Labels" in fmt:
                import json

                return subprocess.CompletedProcess(
                    argv, 0, json.dumps(info["labels"]), ""
                )
            if "StartedAt" in fmt:
                return subprocess.CompletedProcess(
                    argv, 0, "2026-01-01T00:00:00Z", ""
                )
            raise AssertionError(f"unexpected inspect format: {fmt}")
        if cmd == "rm":
            for ref in argv[2:]:
                info = self._ref_lookup(ref)
                if info is not None:
                    self.containers.pop(info["name"])
            return subprocess.CompletedProcess(argv, 0, "", "")
        if cmd in {"start", "kill"}:
            return subprocess.CompletedProcess(argv, 0, "", "")
        if cmd == "wait":
            return subprocess.CompletedProcess(argv, 0, "0\n", "")
        raise AssertionError(f"unexpected docker command: {argv}")


def make_supervisor(tmp_path: Path, journal: Any, fake: FakeDocker) -> DockerSupervisor:
    sup = object.__new__(DockerSupervisor)
    sup.docker_bin = "docker"
    sup.instance = INSTANCE
    sup.launch_journal = journal
    sup._launch_lock_path = tmp_path / "launch.lock"
    sup._run = fake.run  # type: ignore[method-assign]
    return sup


@pytest.fixture()
def store(tmp_path: Path):
    s = Store(str(tmp_path / "journal.db"))
    s.executions.resume()
    yield s
    s.close()


def begin_build_intent(journal: Any, token: str, name: str) -> None:
    journal.begin_docker_launch(
        launch_token=token,
        instance_key=INSTANCE,
        owner_kind="build",
        job_public_id="build-x",
        attempt_no=1,
        slot=150,
        container_name=name,
        host_boot_id="boot-current",
    )


def run_build_with(
    supervisor: DockerSupervisor, tmp_path: Path, *, callback: Any = None
) -> int:
    identity = DockerExecutionIdentity(
        instance=INSTANCE, job_public_id="build-x", attempt_no=1
    )
    return supervisor.run_build(
        identity=identity,
        slot=150,
        launch_token="tok-1",
        src_dir=tmp_path / "src",
        out_dir=tmp_path / "out",
        command="true",
        image="botbattle-builder:bookworm-1",
        profile=BOT_BUILD_PROFILE,
        timeout_sec=1.0,
        docker_uncertain_callback=callback,
    )


def test_definitive_create_failure_settles_journal_idle(
    store: Store, tmp_path: Path
) -> None:
    """create rc!=0（确定性、容器未创建）→ journal 清回 idle，可继续 launch。"""
    fake = FakeDocker(create_mode="fail")
    sup = make_supervisor(tmp_path, store.executions, fake)
    with pytest.raises(DockerCreateAmbiguous, match="构建容器 create 失败"):
        run_build_with(sup, tmp_path)
    assert store.executions.docker_launch()["state"] == "idle"
    # P1 核心：下一次 launch 不再被遗留 creating 拒绝。
    begin_build_intent(store.executions, "tok-next", "bzplat-next")
    assert store.executions.docker_launch()["state"] == "creating"
    store.executions.clear_docker_launch_failed("tok-next")


def test_uncertain_create_without_container_keeps_manual_pause(
    store: Store, tmp_path: Path
) -> None:
    """create 不确定且零证据 → journal 保持 creating 并触发 pause 回调。"""
    fake = FakeDocker(create_mode="uncertain", late=False)
    sup = make_supervisor(tmp_path, store.executions, fake)
    paused: list[str] = []

    def callback(reason: str) -> None:
        paused.append(reason)

    with pytest.raises(DockerCreateAmbiguous, match="manual:"):
        run_build_with(sup, tmp_path, callback=callback)
    assert paused, "uncertain create 必须通知 dispatcher pause，不得静默"
    launch = store.executions.docker_launch()
    assert launch["state"] == "creating"
    # 保守语义落在 Store 守卫上：同 host boot 不允许以 boot-change 名义清
    # creating（迟到容器不可排除），只能经 manual pause 或重启收敛。
    with pytest.raises(DockerLaunchInvariantError):
        store.executions.clear_docker_launch_after_boot_change(
            "tok-1",
            previous_boot_id=str(launch["host_boot_id"]),
            current_boot_id=str(launch["host_boot_id"]),
        )


def test_uncertain_create_with_late_container_removes_and_clears(
    store: Store, tmp_path: Path
) -> None:
    """create 超时但容器实际建成 → 观察/删除/清 journal 回 idle。"""
    fake = FakeDocker(create_mode="uncertain", late=True)
    sup = make_supervisor(tmp_path, store.executions, fake)
    paused: list[str] = []

    def callback(reason: str) -> None:
        paused.append(reason)

    with pytest.raises(DockerCreateAmbiguous, match="构建容器 create 未确认"):
        run_build_with(sup, tmp_path, callback=callback)
    assert fake.containers == {}, "迟到容器必须被精确删除"
    assert store.executions.docker_launch()["state"] == "idle"
    assert paused, "uncertain 结果仍须让 dispatcher 做有界 pause"


def test_uncertain_create_with_renamed_container_found_by_label(
    store: Store, tmp_path: Path
) -> None:
    """迟到容器仅 label 可见（name 被外部改动）也能被发现、删除并清 journal。"""
    fake = FakeDocker(
        create_mode="uncertain", late=True, rename_after_create=True
    )
    sup = make_supervisor(tmp_path, store.executions, fake)
    paused: list[str] = []

    def callback(reason: str) -> None:
        paused.append(reason)

    with pytest.raises(DockerCreateAmbiguous, match="构建容器 create 未确认"):
        run_build_with(sup, tmp_path, callback=callback)
    assert fake.containers == {}, "label 兜底查询必须发现并删除改名容器"
    assert store.executions.docker_launch()["state"] == "idle"


def test_mark_created_failure_with_clean_rm_settles_idle(
    store: Store, tmp_path: Path
) -> None:
    """mark_docker_launch_created 持久化失败 + rm 成功 → 清 creating 回 idle。"""

    class FlakyJournal:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def mark_docker_launch_created(self, token: str) -> dict:
            raise RuntimeError("database is locked")

    fake = FakeDocker(create_mode="ok")
    flaky = FlakyJournal(store.executions)
    sup = make_supervisor(tmp_path, flaky, fake)
    with pytest.raises(DockerControlUncertain, match="journal 无法确认"):
        run_build_with(sup, tmp_path)
    assert fake.containers == {}
    assert store.executions.docker_launch()["state"] == "idle"
    begin_build_intent(store.executions, "tok-next", "bzplat-next")
    store.executions.clear_docker_launch_failed("tok-next")


def test_clear_docker_launch_failed_store_semantics(store: Store) -> None:
    """clear_docker_launch_failed 只接受本 token 的 creating 状态。"""
    begin_build_intent(store.executions, "tok-a", "bzplat-a")
    with pytest.raises(DockerLaunchInvariantError):
        store.executions.clear_docker_launch_failed("tok-wrong")
    store.executions.clear_docker_launch_failed("tok-a")
    assert store.executions.docker_launch()["state"] == "idle"

    begin_build_intent(store.executions, "tok-b", "bzplat-b")
    store.executions.mark_docker_launch_created("tok-b")
    with pytest.raises(DockerLaunchInvariantError):
        store.executions.clear_docker_launch_failed("tok-b")
    store.executions.clear_docker_launch_created("tok-b")
    assert store.executions.docker_launch()["state"] == "idle"


def test_run_build_happy_path_clears_journal(store: Store, tmp_path: Path) -> None:
    """正常链路回归：begin→create→mark→start→clear 后 journal 回 idle。"""
    fake = FakeDocker(create_mode="ok")
    sup = make_supervisor(tmp_path, store.executions, fake)
    exit_code = run_build_with(sup, tmp_path)
    assert exit_code == 0
    assert store.executions.docker_launch()["state"] == "idle"
    assert fake.containers == {}, "一次性构建容器必须清理"


def test_start_session_prewarms_seat_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """start_session 对 seat 自定义镜像与 prepare_session 同款预热。

    预检/LongRunning 走 start_session：缺镜像时必须在此显式 inspect/pull，
    而不是把确定性的 create 失败升级为全局 Docker 不确定。
    """
    runner = object.__new__(binary_runner_module.BinaryRunner)
    runner._docker_bin = "docker"
    runner._prefer_local = False
    runner._docker_ok = True
    runner._sessions = {}
    runner._docker_uncertain_callback = None
    runner._preflight_gate = threading.BoundedSemaphore(1)
    runner._linux_image = "debian:bookworm-slim"
    runner._image_prepare_timeout = 5.0
    runner.supervisor = None

    prewarmed: list[tuple[str, str]] = []

    def fake_ensure(docker_bin: str, image: str, *, prepare_timeout: float) -> None:
        prewarmed.append((docker_bin, image))

    monkeypatch.setattr(
        binary_runner_module, "_ensure_linux_image_ready_sync", fake_ensure
    )
    ensured: list[bool] = []

    async def fake_ready() -> None:
        ensured.append(True)

    monkeypatch.setattr(runner, "ensure_runtime_ready", fake_ready)

    class StopAfterPrewarm(RuntimeError):
        pass

    async def boom(session: Any) -> None:
        raise StopAfterPrewarm("stop after prewarm")

    monkeypatch.setattr(runner, "_start_docker", boom)

    entry = tmp_path / "launcher"
    entry.write_text("#!/bin/sh\nexec python3 src/__main__.py\n")
    with pytest.raises(StopAfterPrewarm):
        asyncio.run(
            runner.start_session(
                entry,
                runtime_mode="longrunning",
                image="botbattle-builder:bookworm-1",
                allow_script_entry=True,
            )
        )
    assert prewarmed == [("docker", "botbattle-builder:bookworm-1")]
    assert ensured == [], "seat 自定义镜像分支不得退回默认镜像 ensure"
