"""seat runtime 注入与云盘快照回归。

覆盖：
1. runner 三条执行路径（Bot-vs-Bot / duplicate 换座 / 人类对局 bot 侧）
   的 seat runtime（镜像/卷/脚本豁免）绑定到正确物理座位，Traditional
   每回合重启透传冻结会话的扩展；
2. `_seat_runtime_extras` 白名单 fail-closed（未知 source_format、镜像
   不在白名单、python 缺镜像声明）；
3. `_runtime_for_bot_version` 的 legacy 镜像行显式返回 None（消除列名
   巧合依赖）；
4. `_user_drive_snapshot`：清单名复核（手改 DB 不得借硬链接逃逸）、
   blob 缺失 fail-closed、对局中 replace/delete 不影响已建快照 inode。
"""
from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.matches.orchestrator import (
    MatchOrchestrator,
    BotVersionContractError,
)
from bzplat.backend.store import Store
from bzplat.backend.user_storage import UserStorageManager


class _RecordingTransport:
    """Traditional 传输假体：按会话记录 seat runtime 三元组。"""

    def __init__(self) -> None:
        self.prepared: list[tuple[str, dict]] = []
        self.started: list[tuple[str, dict]] = []
        self._sessions: dict[str, SimpleNamespace] = {}

    @staticmethod
    def _extras(image: str, extra_volumes, allow_script_entry: bool) -> dict:
        return {
            "image": image,
            "extra_volumes": tuple(extra_volumes),
            "allow_script_entry": allow_script_entry,
        }

    async def prepare_session(
        self,
        path,
        *,
        runtime_mode,
        profile,
        execution_scope=None,
        image="",
        extra_volumes=(),
        allow_script_entry=False,
    ) -> str:
        sid = f"logical-{len(self.prepared)}"
        self.prepared.append((str(path), self._extras(image, extra_volumes, allow_script_entry)))
        self._sessions[sid] = SimpleNamespace(
            binary_path=str(path),
            runtime_mode=runtime_mode,
            profile=profile,
            execution_scope=execution_scope,
            image=image,
            extra_volumes=tuple(extra_volumes),
            allow_script_entry=allow_script_entry,
            requests=[],
            responses=[],
            turn=0,
            long_running=False,
        )
        return sid

    async def start_session(
        self,
        path,
        *,
        runtime_mode,
        profile,
        execution_scope=None,
        image="",
        extra_volumes=(),
        allow_script_entry=False,
    ) -> str:
        sid = f"process-{len(self.started)}"
        self.started.append((str(path), self._extras(image, extra_volumes, allow_script_entry)))
        self._sessions[sid] = SimpleNamespace(
            binary_path=str(path),
            runtime_mode=runtime_mode,
            profile=profile,
            execution_scope=execution_scope,
            image=image,
            extra_volumes=tuple(extra_volumes),
            allow_script_entry=allow_script_entry,
            requests=[],
            responses=[],
            turn=0,
            long_running=False,
        )
        return sid

    async def write_line(self, session_id, line):
        self._sessions[session_id].requests.append(line)

    async def read_line(self, session_id, *, timeout):
        from bzplat.backend.tests._gomoku_v2 import ILLEGAL_OPENING_LINE

        return ILLEGAL_OPENING_LINE

    async def read_extra_line(self, _session_id, *, timeout):
        return ">>>BOTZONE_REQUEST_KEEP_RUNNING<<<"

    async def stop_session(self, session_id):
        self._sessions.pop(session_id, None)


_SR_A = {"image": "img-seat-a", "extra_volumes": (("/vol/a", "/app/src"),), "allow_script_entry": True}
_SR_B = {"image": "img-seat-b", "extra_volumes": (("/vol/b", "/app/src"),), "allow_script_entry": False}


def test_run_binaries_binds_seat_runtime_per_physical_seat():
    transport = _RecordingTransport()
    asyncio.run(
        MatchRunner(transport).run_binaries(
            "/bots/a",
            "/bots/b",
            game_id="gomoku",
            seat_runtime_a=_SR_A,
            seat_runtime_b=_SR_B,
        )
    )
    paths = {path: extras for path, extras in transport.prepared}
    assert paths["/bots/a"] == {
        "image": "img-seat-a",
        "extra_volumes": (("/vol/a", "/app/src"),),
        "allow_script_entry": True,
    }
    assert paths["/bots/b"] == {
        "image": "img-seat-b",
        "extra_volumes": (("/vol/b", "/app/src"),),
        "allow_script_entry": False,
    }


def test_duplicate_keeps_physical_seat_runtime_across_swap_legs(monkeypatch):
    """复式两场换座：镜像/卷固定物理 A/B，不随逻辑座位交换。"""

    async def fake_run_session(*_args, **_kwargs):
        return SimpleNamespace(
            rounds=[],
            rounds_played=0,
            events=[],
            net=[0, 0],
            final_chips=[0, 0],
        )

    from bzplat.backend.matches import runner as runner_module

    monkeypatch.setattr(runner_module, "run_session", fake_run_session)
    transport = _RecordingTransport()
    result = asyncio.run(
        MatchRunner(transport).run_duplicate(
            "/bots/a",
            "/bots/b",
            game_id="holdem",
            seed=7,
            seat_runtime_a=_SR_A,
            seat_runtime_b=_SR_B,
            duplicate=True,
        )
    )
    assert len(result.legs) == 2
    binding = [(path, extras["image"]) for path, extras in transport.prepared]
    # 物理 A 永远 img-seat-a、物理 B 永远 img-seat-b，两场共 4 次会话。
    assert binding == [
        ("/bots/a", "img-seat-a"),
        ("/bots/b", "img-seat-b"),
        ("/bots/a", "img-seat-a"),
        ("/bots/b", "img-seat-b"),
    ]


def test_human_match_injects_seat_runtime_on_bot_side_only():
    transport = _RecordingTransport()

    async def human_turn(_request):
        return {"response": {"action": "pass"}}

    asyncio.run(
        MatchRunner(transport).run_bot_vs_human(
            "/bots/a",
            bot_seat=0,
            human_decide=human_turn,
            game_id="gomoku",
            seat_runtime=_SR_A,
        )
    )
    assert transport.prepared and transport.started
    for _path, extras in transport.prepared + transport.started:
        assert extras["image"] == "img-seat-a"
        assert extras["allow_script_entry"] is True


def test_seat_runtime_extras_whitelist_fail_closed(tmp_path):
    store = Store(str(tmp_path / "extras.db"))
    orch = MatchOrchestrator(store, runner=None, max_concurrent=1)

    src_dir = tmp_path / "v1" / "src"
    src_dir.mkdir(parents=True)
    ok_python = {
        "source_format": "python",
        "runtime_image": "botbattle-builder:bookworm-1",
        "binary_path": str(tmp_path / "v1" / "bot"),
    }
    extras = orch._seat_runtime_extras({}, ok_python)
    assert extras["image"] == "botbattle-builder:bookworm-1"
    assert extras["allow_script_entry"] is True
    assert extras["extra_volumes"] == ((str(tmp_path / "v1" / "src"), "/app/src"),)

    assert orch._seat_runtime_extras({}, None) == {}
    assert orch._seat_runtime_extras({}, {"source_format": "elf"}) == {}

    with pytest.raises(BotVersionContractError, match="白名单"):
        orch._seat_runtime_extras({}, {"source_format": "rust"})
    with pytest.raises(BotVersionContractError, match="白名单"):
        orch._seat_runtime_extras(
            {},
            {
                "source_format": "elf",
                "runtime_image": "attacker/whatever:latest",
            },
        )
    with pytest.raises(BotVersionContractError, match="运行镜像"):
        orch._seat_runtime_extras({}, {"source_format": "python", "runtime_image": ""})
    store.close()


def test_seat_runtime_image_allowlist_is_deliberate():
    """白名单只含已知运行镜像：新增必须连镜像 Dockerfile 与文档一起评审。"""
    from bzplat.backend.runtime.limits import (
        ML_PY_RUNTIME_IMAGE,
        PYTHON_RUNTIME_IMAGE,
        SEAT_RUNTIME_IMAGE_ALLOWLIST,
    )

    assert PYTHON_RUNTIME_IMAGE == "botbattle-builder:bookworm-1"
    assert ML_PY_RUNTIME_IMAGE == "botbattle-ml-py3:bookworm-1"
    assert SEAT_RUNTIME_IMAGE_ALLOWLIST == frozenset(
        {PYTHON_RUNTIME_IMAGE, ML_PY_RUNTIME_IMAGE}
    )


def test_seat_runtime_extras_accepts_ml_image(tmp_path):
    """ML 运行库镜像在白名单内：python 版本声明后 seat 注入其镜像。"""
    store = Store(str(tmp_path / "ml.db"))
    orch = MatchOrchestrator(store, runner=None, max_concurrent=1)
    src_dir = tmp_path / "v1" / "src"
    src_dir.mkdir(parents=True)
    extras = orch._seat_runtime_extras(
        {},
        {
            "source_format": "python",
            "runtime_image": "botbattle-ml-py3:bookworm-1",
            "binary_path": str(tmp_path / "v1" / "bot"),
        },
    )
    assert extras["image"] == "botbattle-ml-py3:bookworm-1"
    assert extras["allow_script_entry"] is True
    assert extras["extra_volumes"] == ((str(src_dir), "/app/src"),)
    store.close()


def test_runtime_for_bot_version_legacy_mirror_returns_none(tmp_path):
    """legacy bots 行经全部校验后，第三元素必须是 None 而不是 bot 行。"""
    from bzplat.backend.crypto import hash_password

    store = Store(str(tmp_path / "legacy.db"))
    user = store.create_user("lgu", "lgu@e.com", hash_password("pw123456"))
    binary = tmp_path / "legacy-bot"
    binary.write_bytes(b"\x7fELFlegacy")
    bot = store.create_bot(
        user["id"],
        "lgbot",
        binary_path=str(binary),
        format="elf",
        game_id="holdem",
    )
    orch = MatchOrchestrator(store, runner=None, max_concurrent=1)
    bot = store.get_bot(bot["id"])
    path, mode, version_row = orch._runtime_for_bot_version(bot, None, seat=0)
    assert path == str(binary)
    assert version_row is None, "legacy 镜像行不得被当成版本行走 seat 扩展"
    assert orch._seat_runtime_extras(bot, version_row) == {}
    store.close()


def test_user_drive_snapshot_name_recheck_and_injection_guard(tmp_path):
    """手改 DB 的带路径清单名必须在快照侧被拒绝（防硬链接逃逸）。"""
    from bzplat.backend.crypto import hash_password

    store = Store(str(tmp_path / "drive.db"))
    user = store.create_user("dru", "dru@e.com", hash_password("pw123456"))
    storage = UserStorageManager(store, root=tmp_path / "assets")
    orch = MatchOrchestrator(
        store,
        runner=None,
        max_concurrent=1,
        mount_root=tmp_path / "mounts",
        user_storage=storage,
    )
    payload = b"weights-bytes"
    sha = hashlib.sha256(payload).hexdigest()
    staging = Path(storage.new_staging()) / "f"
    staging.write_bytes(payload)
    storage.promote_staged(staging, user["id"], sha)
    # 正常清单：合法名字可快照。
    store.replace_user_storage_file(
        user["id"], "w.bin", sha256=sha, size_bytes=len(payload)
    )
    root = orch._user_drive_snapshot("m-1", 0, user["id"])
    assert root is not None and (root / "w.bin").is_file()

    # 手改 DB 注入带路径名字：快照必须 fail-closed，不产生任何链接。
    with store._tx() as conn:
        conn.execute(
            "UPDATE user_storage_files SET name='../escape' "
            "WHERE user_id=? AND name='w.bin'",
            (user["id"],),
        )
    with pytest.raises(BotVersionContractError, match="非法文件名"):
        orch._user_drive_snapshot("m-2", 0, user["id"])
    # 快照目录可以存在（首条清单前即建），但必须没有任何链接产物。
    leftover = [
        item
        for item in (tmp_path / "mounts" / "m-2").rglob("*")
        if item.is_file()
    ]
    assert leftover == []

    # blob 缺失（清单悬空）同样 fail-closed。
    with store._tx() as conn:
        conn.execute(
            "UPDATE user_storage_files SET name='w.bin' "
            "WHERE user_id=? AND name='../escape'",
            (user["id"],),
        )
    (tmp_path / "assets" / str(user["id"]) / sha).unlink()
    with pytest.raises(BotVersionContractError, match="云盘文件缺失"):
        orch._user_drive_snapshot("m-3", 0, user["id"])
    store.close()


def test_user_drive_snapshot_survives_concurrent_replace_and_delete(tmp_path):
    """快照 inode 在对局期内恒定：之后 replace/delete 清单不影响已挂副本。"""
    from bzplat.backend.crypto import hash_password

    store = Store(str(tmp_path / "inode.db"))
    user = store.create_user("inu", "inu@e.com", hash_password("pw123456"))
    storage = UserStorageManager(store, root=tmp_path / "assets")
    orch = MatchOrchestrator(
        store,
        runner=None,
        max_concurrent=1,
        mount_root=tmp_path / "mounts",
        user_storage=storage,
    )

    def _put(name: str, payload: bytes) -> str:
        sha = hashlib.sha256(payload).hexdigest()
        staging = Path(storage.new_staging()) / "f"
        staging.write_bytes(payload)
        storage.promote_staged(staging, user["id"], sha)
        store.replace_user_storage_file(
            user["id"], name, sha256=sha, size_bytes=len(payload)
        )
        return sha

    _put("w.bin", b"v1")
    root = orch._user_drive_snapshot("m-stable", 0, user["id"])
    assert root is not None
    linked = root / "w.bin"
    inode_before = linked.stat().st_ino

    # 同名替换（原子 rename 换 inode）与删除清单行都不影响快照内副本。
    _put("w.bin", b"v2-longer-payload")
    store.delete_user_storage_file(user["id"], "w.bin")
    assert linked.is_file()
    assert linked.stat().st_ino == inode_before
    assert linked.read_bytes() == b"v1"
    store.close()


def test_user_drive_snapshot_modes_under_production_umask(tmp_path):
    """umask 0077 下 seat 快照目录必须 0755、内部硬链接 0644（65534 容器可读）。"""
    import os
    import stat
    from bzplat.backend.crypto import hash_password

    store = Store(str(tmp_path / "perm.db"))
    user = store.create_user("pmu", "pmu@e.com", hash_password("pw123456"))
    storage = UserStorageManager(store, root=tmp_path / "assets")
    orch = MatchOrchestrator(
        store,
        runner=None,
        max_concurrent=1,
        mount_root=tmp_path / "mounts",
        user_storage=storage,
    )
    payload = b"perm-payload"
    sha = hashlib.sha256(payload).hexdigest()
    old_umask = os.umask(0o077)
    try:
        staging = Path(storage.new_staging()) / "f"
        staging.write_bytes(payload)
        storage.promote_staged(staging, user["id"], sha)
        store.replace_user_storage_file(
            user["id"], "w.bin", sha256=sha, size_bytes=len(payload)
        )
        root = orch._user_drive_snapshot("m-perm", 0, user["id"])
    finally:
        os.umask(old_umask)
    assert root is not None
    assert stat.S_IMODE(root.stat().st_mode) == 0o755
    # 硬链接与 blob 同 inode：blob 已在 promote 时归一 0644。
    assert stat.S_IMODE((root / "w.bin").stat().st_mode) == 0o644
    # 安全边界钉住：只有 seatN 叶子放宽，match_mounts 根与 <match_id>/
    # 父目录维持 0700（docker daemon 以 root 解析路径，容器不需要穿越）。
    assert stat.S_IMODE((tmp_path / "mounts").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "mounts" / "m-perm").stat().st_mode) == 0o700
    store.close()
