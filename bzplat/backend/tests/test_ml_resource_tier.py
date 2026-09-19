"""ML 运行库资源档：platform_ml 派生、注册表 v2 追加与预检环境平权。

背景事故（2026-09-19，AGHX/EZ1）：python 源码 Bot 选择 source_runtime=ml
后，torch 导入（~400 MiB）叠加云盘模型（~208 MiB）在 platform_low 的
512 MiB 无 swap 硬顶下被 cgroup OOM 杀（exit 137，空 stderr），而预检既
不挂云盘也不升内存档，用户只能靠“无模型回退”骗过预检。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

import pytest

from bzplat.backend.runtime.limits import (
    EXECUTION_RESOURCE_PROFILE_REGISTRY,
    LATEST_EXECUTION_RESOURCE_PROFILE_VERSION,
    ML_PY_RUNTIME_IMAGE,
    PLATFORM_ML_PROFILE,
    execution_resource_snapshot,
    resolve_execution_resource_profile,
)
from bzplat.backend.store.db import Store
from bzplat.backend.store.schema import (
    EXECUTION_ENV_HUMAN,
    EXECUTION_ENV_PLATFORM_HIGH,
    EXECUTION_ENV_PLATFORM_LOW,
    EXECUTION_ENV_PLATFORM_ML,
    EXECUTION_PROFILE_VERSION,
    EXECUTION_SOURCE_AUTO,
    EXECUTION_SOURCE_MANUAL,
    TYPE_CHALLENGE,
    TYPE_LADDER,
)


# ---------------------------------------------------------------- 注册表


def test_profile_registry_v2_appends_platform_ml():
    resolved = resolve_execution_resource_profile(
        EXECUTION_ENV_PLATFORM_ML, 2
    )
    assert resolved.name == "platform_ml"
    assert resolved.cpus == 1
    assert resolved.memory_mb == 2048
    # 旧版本不可解析新档位（历史 job 的规格不被改绑）。
    with pytest.raises(ValueError):
        resolve_execution_resource_profile(EXECUTION_ENV_PLATFORM_ML, 1)
    # v0/v1 历史映射保持不可变：追加不回写。
    assert EXECUTION_ENV_PLATFORM_ML not in EXECUTION_RESOURCE_PROFILE_REGISTRY[1]
    assert EXECUTION_ENV_PLATFORM_HIGH in EXECUTION_RESOURCE_PROFILE_REGISTRY[1]
    assert LATEST_EXECUTION_RESOURCE_PROFILE_VERSION == 2
    assert EXECUTION_PROFILE_VERSION == 2


def test_resource_snapshot_charges_ml_memory_not_extra_units():
    # ML 档 CPU 与单位记账与低配一致；只有内存翻倍。
    units, cpu_millis, memory_mb = execution_resource_snapshot(
        (EXECUTION_ENV_PLATFORM_ML, EXECUTION_ENV_HUMAN), 2
    )
    assert units == 1
    assert cpu_millis == 1000
    assert memory_mb == 2048
    mixed_units, mixed_cpu, mixed_memory = execution_resource_snapshot(
        (EXECUTION_ENV_PLATFORM_ML, EXECUTION_ENV_PLATFORM_LOW), 2
    )
    assert mixed_units == 2
    assert mixed_cpu == 2000
    assert mixed_memory == 2048 + 512


# ---------------------------------------------------------------- 入队派生


def _bot(store: Store, key: str, *, runtime_image: str = "") -> dict:
    user = store.create_user(
        f"user-{key}", f"{key}@example.test", "test-password-hash"
    )
    binary = Path(store.path).parent / f"mltier-{key}.elf"
    binary.write_bytes(f"fixture-{key}".encode())
    bot = store.create_bot(
        int(user["id"]),
        f"bot-{key}",
        binary_path=str(binary),
        format="elf",
        game_id="pencil",
    )
    version = store.add_bot_version(
        bot["id"], binary_path=str(binary), runtime_image=runtime_image
    )
    return {
        "user_id": int(user["id"]),
        "bot_id": int(bot["id"]),
        "version_id": int(version["id"]),
    }


@pytest.fixture
def tier_store(tmp_path):
    store = Store(str(tmp_path / "mltier.db"))
    store.executions.resume()
    yield store
    store.close()


def test_manual_enqueue_derives_platform_ml_for_ml_version(tier_store):
    store = tier_store
    ml = _bot(store, "ml-a", runtime_image=ML_PY_RUNTIME_IMAGE)
    std = _bot(store, "std-b")
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=ml["user_id"],
        game_id="pencil",
        match_type=TYPE_CHALLENGE,
        bot_a_id=ml["bot_id"],
        bot_b_id=std["bot_id"],
        bot_a_version_id=ml["version_id"],
        bot_b_version_id=std["version_id"],
        bot_a_environment=EXECUTION_ENV_PLATFORM_LOW,
        bot_b_environment=EXECUTION_ENV_PLATFORM_LOW,
    )
    assert job["bot_a_environment"] == EXECUTION_ENV_PLATFORM_ML
    assert job["bot_b_environment"] == EXECUTION_ENV_PLATFORM_LOW
    assert job["profile_version"] == 2
    assert job["sandbox_units"] == 2
    assert job["host_memory_mb"] == 2048 + 512
    assert job["host_cpu_millis"] == 2000


def test_manual_enqueue_keeps_platform_low_for_std_versions(tier_store):
    store = tier_store
    a = _bot(store, "std-a")
    b = _bot(store, "std-b")
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=a["user_id"],
        game_id="pencil",
        match_type=TYPE_CHALLENGE,
        bot_a_id=a["bot_id"],
        bot_b_id=b["bot_id"],
        bot_a_version_id=a["version_id"],
        bot_b_version_id=b["version_id"],
    )
    assert job["bot_a_environment"] == EXECUTION_ENV_PLATFORM_LOW
    assert job["bot_b_environment"] == EXECUTION_ENV_PLATFORM_LOW
    assert job["profile_version"] == 2
    assert job["host_memory_mb"] == 512 + 512


def test_manual_enqueue_rejects_unknown_version_fail_closed(tier_store):
    store = tier_store
    a = _bot(store, "bad-a")
    b = _bot(store, "bad-b")
    with pytest.raises(ValueError, match="版本不存在"):
        store.executions.enqueue(
            source=EXECUTION_SOURCE_MANUAL,
            owner_user_id=a["user_id"],
            game_id="pencil",
            match_type=TYPE_CHALLENGE,
            bot_a_id=a["bot_id"],
            bot_b_id=b["bot_id"],
            bot_a_version_id=987654,
            bot_b_version_id=b["version_id"],
        )


def test_human_source_ml_bot_seat_derives_platform_ml(tier_store):
    """人机局：Bot 座位 ML 版本升档，人类座位不受影响（CHECK 交互钉住）。"""
    from bzplat.backend.store.schema import EXECUTION_SOURCE_HUMAN, TYPE_HUMAN

    store = tier_store
    ml = _bot(store, "human-ml", runtime_image=ML_PY_RUNTIME_IMAGE)
    human = store.create_user(
        "human-ml-u", "human-ml-u@example.test", "hash"
    )
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_HUMAN,
        owner_user_id=int(human["id"]),
        game_id="pencil",
        match_type=TYPE_HUMAN,
        bot_a_id=ml["bot_id"],
        bot_b_id=ml["bot_id"],
        bot_a_version_id=ml["version_id"],
        bot_b_version_id=None,
        human_user_id=int(human["id"]),
        human_seat=1,
    )
    assert job["bot_a_environment"] == EXECUTION_ENV_PLATFORM_ML
    assert job["bot_b_environment"] == "human"
    assert job["sandbox_units"] == 1
    assert job["host_cpu_millis"] == 1000
    assert job["host_memory_mb"] == 2048


def test_auto_source_ml_rep_derives_platform_ml(tier_store):
    store = tier_store
    ml = _bot(store, "auto-ml", runtime_image=ML_PY_RUNTIME_IMAGE)
    std = _bot(store, "auto-std")
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_AUTO,
        owner_user_id=None,
        game_id="pencil",
        match_type=TYPE_LADDER,
        bot_a_id=ml["bot_id"],
        bot_b_id=std["bot_id"],
        bot_a_version_id=ml["version_id"],
        bot_b_version_id=std["version_id"],
    )
    assert job["bot_a_environment"] == EXECUTION_ENV_PLATFORM_ML
    assert job["bot_b_environment"] == EXECUTION_ENV_PLATFORM_LOW


# ---------------------------------------------------------------- 预检平权


class _RecordingRunner:
    """记录 start_session 收到的挂载/档位参数；回放一条合法 pencil 响应。"""

    def __init__(self) -> None:
        self.starts: list[dict] = []

    async def start_session(self, binary_path, **kwargs):
        entry = {"path": str(binary_path), **kwargs}
        mounts = {
            target: host
            for host, target in kwargs.get("extra_volumes", ())
        }
        entry["_mounts"] = mounts
        mnt = mounts.get("/mnt/data")
        if mnt is not None:
            entry["_mnt_mode"] = stat.S_IMODE(os.stat(mnt).st_mode)
            sample = next(iter(Path(mnt).iterdir()), None)
            entry["_file_mode"] = (
                stat.S_IMODE(os.stat(sample).st_mode) if sample else None
            )
        self.starts.append(entry)
        return f"sid-{len(self.starts)}"

    async def send(self, sid, line, timeout=60.0):
        return '{"response":{"x":1,"y":0}}'

    async def stop_session(self, sid):
        return None


def _make_python_zip(path: Path) -> Path:
    import zipfile

    payload = (
        "import json,sys\n"
        "j=json.loads(sys.stdin.readline())\n"
        'print(json.dumps({"response":{"x":1,"y":0}}))\n'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("__main__.py", payload)
    return path


def _upload_python_bot(store, manager, tmp_path, *, source_runtime, runner):
    user = store.create_user(
        "pfuser", "pfuser@example.test", "test-password-hash"
    )
    zp = _make_python_zip(tmp_path / "src.zip")
    staged = manager.new_staged_upload()
    shutil.copyfile(zp, staged.path)
    staged.size = zp.stat().st_size
    bot = manager.create_from_upload(
        int(user["id"]),
        f"pfbot{source_runtime or 'std'}",
        staged,
        game_id="pencil",
        runtime_mode="traditional",
        source_format="python",
        source_runtime=source_runtime,
        binary_runner=runner,
    )
    return user, bot


def test_preflight_mounts_owner_drive_and_ml_profile(tmp_path):
    """ML 变体预检：挂 owner 云盘（0755/0644 可读）+ platform_ml 档。"""
    from bzplat.backend.bots.manager import BotManager
    from bzplat.backend.user_storage import UserStorageManager

    store = Store(str(tmp_path / "pf.db"))
    us = UserStorageManager(store, root=tmp_path / "assets")
    manager = BotManager(
        store, upload_root=tmp_path / "up", user_storage=us
    )
    runner = _RecordingRunner()
    user, _bot_row = _upload_python_bot(
        store, manager, tmp_path, source_runtime="ml", runner=runner
    )
    # 上传前先给 owner 放一个云盘文件（模型占位）。
    # 顺序：先建用户与文件，再触发上传——上面 helper 已经完成上传；
    # 这里改为第二次上传（新版本），确保快照在预检时存在。
    payload = b"model-bytes"
    digest = hashlib.sha256(payload).hexdigest()
    staging = us.new_staging() / "part"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(payload)
    us.promote_staged(staging, int(user["id"]), digest)
    store.replace_user_storage_file(
        int(user["id"]), "model_from_bin.pth", digest, len(payload)
    )

    runner2 = _RecordingRunner()
    zp = _make_python_zip(tmp_path / "src2.zip")
    staged2 = manager.new_staged_upload()
    shutil.copyfile(zp, staged2.path)
    staged2.size = zp.stat().st_size
    manager.upload_version(
        int(_bot_row["id"]),
        int(user["id"]),
        staged2,
        runtime_mode="traditional",
        source_format="python",
        source_runtime="ml",
        binary_runner=runner2,
    )

    start = runner2.starts[0]
    assert start["_mounts"]["/mnt/data"] == start["_mounts"]["/app/data"]
    assert start["_mnt_mode"] == 0o755
    assert start["_file_mode"] == 0o644
    assert start.get("profile") == PLATFORM_ML_PROFILE
    assert start["image"] == ML_PY_RUNTIME_IMAGE
    assert "/app/src" in start["_mounts"]
    # 预检结束后快照目录整树回收。
    leftovers = [
        entry
        for entry in (tmp_path / "up").iterdir()
        if entry.name.startswith(".preflight-drive-")
    ]
    assert leftovers == []
    store.close()


def test_preflight_std_python_keeps_default_profile_but_still_mounts_drive(
    tmp_path,
):
    from bzplat.backend.bots.manager import BotManager
    from bzplat.backend.user_storage import UserStorageManager

    store = Store(str(tmp_path / "pfstd.db"))
    us = UserStorageManager(store, root=tmp_path / "assets2")
    manager = BotManager(
        store, upload_root=tmp_path / "up2", user_storage=us
    )
    runner = _RecordingRunner()
    user, _bot_row = _upload_python_bot(
        store, manager, tmp_path, source_runtime="", runner=runner
    )
    payload = b"data-file"
    digest = hashlib.sha256(payload).hexdigest()
    staging = us.new_staging() / "part"
    staging.parent.mkdir(parents=True, exist_ok=True)
    staging.write_bytes(payload)
    us.promote_staged(staging, int(user["id"]), digest)
    store.replace_user_storage_file(
        int(user["id"]), "weights.bin", digest, len(payload)
    )

    runner2 = _RecordingRunner()
    zp = _make_python_zip(tmp_path / "src3.zip")
    staged2 = manager.new_staged_upload()
    shutil.copyfile(zp, staged2.path)
    staged2.size = zp.stat().st_size
    manager.upload_version(
        int(_bot_row["id"]),
        int(user["id"]),
        staged2,
        runtime_mode="traditional",
        source_format="python",
        source_runtime="",
        binary_runner=runner2,
    )
    start = runner2.starts[0]
    assert "/mnt/data" in start["_mounts"]
    # 标准库变体不显式传档位（保持 runner 默认低配）。
    assert "profile" not in start
    store.close()


def test_preflight_without_cloud_files_skips_drive_mount(tmp_path):
    from bzplat.backend.bots.manager import BotManager

    store = Store(str(tmp_path / "pfnodrive.db"))
    manager = BotManager(store, upload_root=tmp_path / "up3")
    runner = _RecordingRunner()
    _upload_python_bot(
        store, manager, tmp_path, source_runtime="", runner=runner
    )
    start = runner.starts[0]
    assert "/mnt/data" not in start["_mounts"]
    assert "/app/src" in start["_mounts"]
    store.close()


# ------------------------------------------------ 迁移重建路径（旧形状→v2）


def _legacy_shaped_db(path, tmp_path):
    """构造 v1.6 形状的 execution_jobs：CHECK 不含 platform_ml，
    并带一个模板未声明的追加列（模拟 _add_col 契约回填），
    附带 attempts 行——覆盖迁移的重建/补列/复制/回填全路径。"""
    import sqlite3

    from bzplat.backend.store.db import _schema_create_table_sql

    store = Store(str(path))
    store.executions.resume()
    ml = _bot(store, "mig-a")
    std = _bot(store, "mig-b")
    job = store.executions.enqueue(
        source=EXECUTION_SOURCE_MANUAL,
        owner_user_id=ml["user_id"],
        game_id="pencil",
        match_type=TYPE_CHALLENGE,
        bot_a_id=ml["bot_id"],
        bot_b_id=std["bot_id"],
        bot_a_version_id=ml["version_id"],
        bot_b_version_id=std["version_id"],
    )
    store.executions.resume()
    store.close()

    con = sqlite3.connect(str(path))
    new_sql = _schema_create_table_sql("execution_jobs")
    # 逆放宽（正则）：还原 v1.6 约束文本。
    import re as _re

    legacy_sql = new_sql
    # human 对局 bot 座位回单值。
    legacy_sql = _re.sub(
        r"bot_b_environment IN \('platform_low','platform_ml'\)",
        "bot_b_environment='platform_low'", legacy_sql,
    )
    legacy_sql = _re.sub(
        r"bot_a_environment IN \('platform_low','platform_ml'\)",
        "bot_a_environment='platform_low'", legacy_sql,
    )
    # 其余 IN 列表与 CASE 单位记账里的 platform_ml 一律摘除。
    legacy_sql = legacy_sql.replace(",'platform_ml'", "")
    # 去掉本 PR 追加的两段 v2 公式钉住。
    legacy_sql = _re.sub(
        r"\n        AND \(profile_version<>2 OR host_cpu_millis =.*?ELSE 0 END\)\)\n        AND \(profile_version<>2 OR host_memory_mb =.*?ELSE 0 END\)\)\n    \),",
        "\n    ),",
        legacy_sql,
        flags=_re.S,
    )
    assert "platform_ml" not in legacy_sql, "legacy 形状还原失败"
    # 旁路替换：建 legacy 形状临时表 → 复制既有行 → 换名接管 → 追加回填列。
    tmp_sql = legacy_sql.replace(
        "CREATE TABLE IF NOT EXISTS execution_jobs",
        "CREATE TABLE execution_jobs_legacy_tmp",
        1,
    )
    con.execute(tmp_sql)
    con.execute(
        "INSERT INTO execution_jobs_legacy_tmp SELECT * FROM execution_jobs"
    )
    con.execute("DROP TABLE execution_jobs")
    con.execute(
        "ALTER TABLE execution_jobs_legacy_tmp RENAME TO execution_jobs"
    )
    # 附带一条 attempts 行，覆盖子表备份/回填路径。
    job_id, match_id = con.execute(
        "SELECT id,current_match_id FROM execution_jobs WHERE public_id=?",
        (job["public_id"],),
    ).fetchone()
    con.execute(
        "INSERT INTO execution_job_attempts("
        "job_id,attempt_no,match_id,status,events_observed,created_at,"
        "started_at,terminal_at,terminal_reason) "
        "VALUES(?,1,?,'completed',0,'2026-09-19T15:00:00',"
        "'2026-09-19T15:00:01','2026-09-19T15:00:02','done')",
        (job_id, match_id or "m-mig"),
    )
    # 模拟契约回填追加列（模板未声明），带列级 CHECK 供 decl 保真断言。
    con.execute(
        "ALTER TABLE execution_jobs ADD COLUMN legacy_backfill "
        "TEXT NOT NULL DEFAULT 'old' CHECK (legacy_backfill IN ('old','new'))"
    )
    con.execute("UPDATE execution_jobs SET legacy_backfill='new'")
    # 行本身也还原为 v1.6 冻结形状（v1 档位版本）。
    con.execute("UPDATE execution_jobs SET profile_version=1")
    con.commit()
    con.close()
    return job["public_id"]


def test_ml_environment_migration_rebuilds_legacy_shape(tmp_path):
    """旧形状（CHECK 无 platform_ml + 追加列）重开即迁移：行/attempt 保真、
    追加列声明（含 CHECK）保真、platform_ml 可插入、二次 reopen 幂等、
    索引列集与 schema 正典一致。"""
    import sqlite3

    db = tmp_path / "legacy.db"
    public_id = _legacy_shaped_db(db, tmp_path)

    store = Store(str(db))
    store.executions.resume()
    job = store.executions.get(public_id)
    assert job is not None
    assert job["legacy_backfill"] == "new"
    assert job["bot_a_environment"] == "platform_low"  # 存量行不重释
    assert job["profile_version"] == 1

    # 迁移后允许派生新档（v2 入队路径可用）。
    con = sqlite3.connect(str(db))
    kept_attempts = con.execute(
        "SELECT COUNT(*) FROM execution_job_attempts WHERE job_id=?",
        (job["id"],),
    ).fetchone()[0]
    assert kept_attempts == 1  # 子表行保真
    sql = con.execute(
        "SELECT sql FROM sqlite_master WHERE name='execution_jobs'"
    ).fetchone()[0]
    assert "platform_ml" in sql
    # 追加列的列级 CHECK 在补列 decl 中保真（DDL 原文提取）。
    assert "legacy_backfill IN ('old','new')" in sql
    # source_terminal 索引列集与 schema 正典一致（不带多余 id 尾列）。
    idx = con.execute(
        "SELECT sql FROM sqlite_master WHERE type='index' "
        "AND name='idx_execution_jobs_source_terminal'"
    ).fetchone()
    assert idx is not None and "(source,status,terminal_at)" in idx[0]
    con.close()
    store.close()

    # 二次 reopen 幂等（行数不丢、不再重建）。
    store2 = Store(str(db))
    store2.executions.resume()
    assert store2.executions.get(public_id) is not None
    store2.close()
