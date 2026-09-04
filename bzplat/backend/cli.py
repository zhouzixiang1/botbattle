"""CLI：serve / 管理辅助。"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
from pathlib import Path

import typer
import uvicorn

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store
from bzplat.backend.store.schema import ROLE_ADMIN

app = typer.Typer(help="botzone-platform CLI", no_args_is_help=True)

_TEST_ONLY_SERVE_FLAGS = (
    "BZ_BOT_LOCAL",
    "BZ_SKIP_CAPTCHA",
    "BZ_TEST_CAPTCHA",
)


def _load_dotenv(path: Path = Path(".env")) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


@app.command()
def serve(
    host: str = typer.Option(None, help="绑定地址"),
    port: int = typer.Option(None, help="端口"),
    reload: bool = typer.Option(False, help="热重载"),
):
    """启动 Web 服务。"""
    _load_dotenv()

    host = host or os.environ.get("BZ_HOST", "127.0.0.1")
    port = port or int(os.environ.get("BZ_PORT", "50380"))
    from bzplat.backend.security import validate_server_bind

    try:
        host = validate_server_bind(host)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    # QA 模式必须在任何日志 handler、数据库连接或运行时目录创建之前
    # 一次性验证全部写目标。普通 main 服务仍可按约定绑定 50380。
    from bzplat.backend.qa_safety import qa_instance_enabled

    qa_instance = qa_instance_enabled(os.environ.get("BZ_QA_INSTANCE"))
    enabled_test_flags = tuple(
        name
        for name in _TEST_ONLY_SERVE_FLAGS
        if qa_instance_enabled(os.environ.get(name))
    )
    if enabled_test_flags and not qa_instance:
        raise typer.BadParameter(
            f"{', '.join(enabled_test_flags)} 仅允许隔离 QA 服务使用；"
            "必须同时显式设置 BZ_QA_INSTANCE=1"
        )
    if qa_instance:
        from bzplat.backend.qa_safety import assert_qa_server_startup_isolated

        cwd = Path.cwd()
        db_raw = os.environ.get("BZ_DB_PATH") or "botzone.db"
        db_candidate = Path(db_raw).expanduser()
        if not db_candidate.is_absolute():
            db_candidate = cwd / db_candidate
        runtime_parent = db_candidate.resolve().parent
        log_raw = os.environ.get("BZ_LOG_DIR") or runtime_parent / "logs"
        avatar_raw = os.environ.get("BZ_AVATAR_DIR") or runtime_parent / "avatars"
        try:
            database, logs, avatars = assert_qa_server_startup_isolated(
                port=port,
                db_path=db_raw,
                log_dir=log_raw,
                avatar_dir=avatar_raw,
                source_root=Path(__file__).resolve().parents[2],
                cwd=cwd,
            )
        except RuntimeError as exc:
            raise typer.BadParameter(str(exc)) from exc
        # Pin every downstream consumer to the exact paths that were validated.
        os.environ["BZ_DB_PATH"] = str(database)
        os.environ["BZ_LOG_DIR"] = str(logs)
        os.environ["BZ_AVATAR_DIR"] = str(avatars)

    # 统一日志：文件 + 控制台（uvicorn.run 前生效，确保所有模块落 app.log）
    from bzplat.backend.logging_config import setup_logging
    from bzplat.backend.runtime.limits import (
        MAX_LOCAL_AI_WEBSOCKET_MESSAGE_BYTES,
    )
    setup_logging(level=os.environ.get("BZ_LOG_LEVEL", "INFO"))
    logging.getLogger(__name__).info("botbattle 启动 host=%s port=%s reload=%s", host, port, reload)

    uvicorn.run(
        "bzplat.backend.main:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        # The application validates proxy headers against the original ASGI
        # socket peer. Never let Uvicorn rewrite scope['client'] first.
        proxy_headers=False,
        # Reject oversized Local AI frames before Uvicorn buffers and decodes
        # them. Uvicorn's current SansIO backend pauses socket reads after
        # each complete message, so no unsupported queue-size promise is made.
        ws_max_size=MAX_LOCAL_AI_WEBSOCKET_MESSAGE_BYTES,
        log_level="info",
        # setup_logging() owns Uvicorn's handlers and request-target filter.
        # Reapplying Uvicorn's default config here would bypass both and write
        # path+query request targets to the stdout-backed web.log.
        log_config=None,
    )


@app.command("create-admin")
def create_admin(
    username: str,
    email: str,
    password: str,
    db: str = typer.Option("botzone.db", "--db"),
):
    """创建或提升管理员账号（开发用；跳过邮箱验证）。"""
    store = Store(db)
    existing = store.get_user_by_username(username)
    if existing:
        store.update_user(
            existing["id"],
            role=ROLE_ADMIN,
            email_verified=1,
            password_hash=hash_password(password),
            is_active=1,
        )
        typer.echo(f"updated admin id={existing['id']}")
    else:
        u = store.create_user(
            username, email, hash_password(password), role=ROLE_ADMIN
        )
        store.update_user(u["id"], email_verified=1)
        typer.echo(f"created admin id={u['id']}")


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now().isoformat(timespec="seconds")


@app.command()
def maintenance(
    action: str = typer.Argument(..., help="begin | status | end"),
    db: str = typer.Option(
        None, "--db", help="目标数据库（默认 BZ_DB_PATH/.env，再退回 botzone.db）"
    ),
    reason: str = typer.Option("", "--reason", help="begin 时的排空原因（≤200 字符）"),
    confirm_service_restarted: bool = typer.Option(
        False,
        "--confirm-service-restarted",
        help="end 必须显式确认：目标服务已用同版本代码重启（进程内上传/任务探针不在 CLI 判定内）",
    ),
):
    """部署维护排空控制（与 admin HTTP「准备维护/结束维护」同事务语义）。

    供本机运维使用：与 platform-ctl.sh 同一信任层，不经 HTTP 认证。
    dispatcher 下一轮轮询（≤1s）感知状态变化。end 的 ready CAS 与
    Store 派生语义一致；运行中服务进程内的上传/任务探针不在本命令
    判定内，非「服务已重启」场景优先用 admin HTTP 端点。操作会向
    数据库邻接的 <db>.maintenance-cli.log 追加一行审计记录。
    """
    import sqlite3

    from bzplat.backend.store.execution import (
        ExecutionMaintenanceConflict,
        ExecutionRepository,
    )

    _load_dotenv()
    database = db or os.environ.get("BZ_DB_PATH") or "botzone.db"
    candidate = Path(database).expanduser()
    if not candidate.is_file():
        raise typer.BadParameter(f"数据库不存在（拒绝新建）: {database}")
    action = action.strip().lower()
    if action not in {"begin", "status", "end"}:
        raise typer.BadParameter("action 必须是 begin/status/end")
    if len(reason) > 200:
        raise typer.BadParameter("--reason 最长 200 字符（与 admin HTTP 一致）")
    if action == "end" and not confirm_service_restarted:
        raise typer.BadParameter(
            "end 需要 --confirm-service-restarted：确认目标服务已用同版本代码"
            "重启；旧进程仍在运行时请改用 admin HTTP 端点"
        )
    # 只读预检 drain 列：本命令可能在新代码下对仍在运行旧服务的库执行，
    # 缺列说明该库来自更旧版本——静默 _migrate 会把迁移前置到停服/冷备
    # 之前，破坏计划部署序列，必须拒绝。
    try:
        probe = sqlite3.connect(f"file:{candidate}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise typer.BadParameter(f"无法只读打开目标库: {exc}") from exc
    try:
        probe.execute("PRAGMA busy_timeout=3000")
        columns = {row[1] for row in probe.execute(
            "PRAGMA table_info(execution_control)"
        )}
    except sqlite3.Error as exc:
        raise typer.BadParameter(f"目标库预检失败: {exc}") from exc
    finally:
        probe.close()
    required = {"accepting", "auto_enabled", "deployment_drain_requested"}
    missing = required - columns
    if missing:
        raise typer.BadParameter(
            f"目标库缺少 execution_control 排空列 {sorted(missing)}："
            "先按计划部署完成升级，再执行 maintenance"
        )

    store = Store(str(candidate))
    repo = ExecutionRepository(store)
    try:
        if action == "begin":
            repo.begin_maintenance(reason)
        elif action == "end":
            repo.end_maintenance()
        status = repo.maintenance_status()
    except ExecutionMaintenanceConflict as exc:
        # 退出码 3 区分状态冲突与参数错误（typer.BadParameter 恒为 2）。
        typer.echo(f"maintenance {action} failed: {exc.code}: {exc.message}", err=True)
        raise typer.Exit(code=3) from exc
    finally:
        store.close()
    if action in {"begin", "end"}:
        audit_path = candidate.parent / f"{candidate.name}.maintenance-cli.log"
        try:
            with open(audit_path, "a", encoding="utf-8") as fh:
                fh.write(
                    f"{_now_iso()} action={action} reason={reason[:200]!r} "
                    f"requested={int(status['requested'])} "
                    f"ready={int(bool(status['ready']))}\n"
                )
        except OSError as exc:
            # 状态已变更，审计失败只降级为告警，不能让运维误判操作失败。
            typer.echo(f"maintenance audit write failed: {exc}", err=True)
    typer.echo(f"db={candidate.resolve()}")
    typer.echo(json.dumps(status, ensure_ascii=False))


def _validated_cold_backup(database: Path, backup_raw: str | None) -> Path:
    if not backup_raw:
        raise typer.BadParameter("dry-run/apply 必须提供 --backup 的冷备绝对路径")
    backup = Path(backup_raw)
    if not backup.is_absolute():
        raise typer.BadParameter("--backup 必须是绝对路径")
    try:
        backup = backup.resolve(strict=True)
    except FileNotFoundError as exc:
        raise typer.BadParameter("--backup 文件不存在") from exc
    if not backup.is_file() or backup.stat().st_size <= 0:
        raise typer.BadParameter("--backup 必须是非空普通文件")
    if backup.samefile(database):
        raise typer.BadParameter("--backup 不能与目标数据库是同一个文件")
    try:
        with sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()
    except sqlite3.Error as exc:
        raise typer.BadParameter(f"--backup 不是可读 SQLite 冷备: {exc}") from exc
    if not result or result[0] != "ok":
        raise typer.BadParameter(f"--backup integrity_check 失败: {result}")
    return backup


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite_readonly_health(path: Path, *, label: str) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if os.path.lexists(sidecar):
            raise typer.BadParameter(f"{label} 存在 SQLite {suffix}，不是冷库")
    try:
        with path.open("rb") as database_file:
            header = database_file.read(20)
    except OSError as exc:
        raise typer.BadParameter(f"{label} 无法读取 SQLite 文件头: {exc}") from exc
    if header[:16] != b"SQLite format 3\x00":
        raise typer.BadParameter(f"{label} 不是 SQLite 3 数据库")
    if header[18:20] != b"\x01\x01":
        raise typer.BadParameter(f"{label} 不是 journal_mode=delete 的冷库")
    try:
        with sqlite3.connect(
            path.as_uri() + "?mode=ro&immutable=1",
            uri=True,
            isolation_level=None,
        ) as conn:
            conn.execute("PRAGMA query_only=ON")
            conn.execute("PRAGMA trusted_schema=OFF")
            conn.execute("PRAGMA foreign_keys=ON")
            if (
                conn.execute("PRAGMA query_only").fetchone() != (1,)
                or conn.execute("PRAGMA trusted_schema").fetchone() != (0,)
                or conn.execute("PRAGMA foreign_keys").fetchone() != (1,)
            ):
                raise sqlite3.DatabaseError("只读 SQLite 安全参数无法启用")
            conn.execute("BEGIN")
            integrity = conn.execute("PRAGMA integrity_check").fetchall()
            foreign_keys = conn.execute("PRAGMA foreign_key_check").fetchall()
    except sqlite3.Error as exc:
        raise typer.BadParameter(f"{label} 不是可读 SQLite 冷库: {exc}") from exc
    if integrity != [("ok",)]:
        raise typer.BadParameter(f"{label} integrity_check 失败: {integrity[:3]}")
    if foreign_keys:
        raise typer.BadParameter(
            f"{label} foreign_key_check 失败: {foreign_keys[:3]}"
        )


def _validated_cutover_cold_backup(
    database: Path,
    backup_raw: str | None,
    *,
    require_target_equality: bool,
) -> tuple[Path, str, str]:
    """Validate healthy stable target/backup bytes while raw flock is held."""

    backup = _validated_cold_backup(database, backup_raw)
    _sqlite_readonly_health(database, label="目标数据库")
    _sqlite_readonly_health(backup, label="冷备")
    def stable_stat(path: Path) -> tuple[int, int, int, int, int]:
        current = path.stat()
        return (
            current.st_dev,
            current.st_ino,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        )

    database_stat = stable_stat(database)
    backup_stat = stable_stat(backup)
    if require_target_equality and database_stat[2] != backup_stat[2]:
        raise typer.BadParameter("--backup 与停服目标数据库不是逐字节同一冷备")
    database_digest = _sha256_file(database)
    backup_digest = _sha256_file(backup)
    if require_target_equality and database_digest != backup_digest:
        raise typer.BadParameter("--backup 与停服目标数据库不是逐字节同一冷备")
    # Refuse a concurrent replacement/change during validation.
    if stable_stat(database) != database_stat or stable_stat(backup) != backup_stat:
        raise typer.BadParameter("目标数据库或冷备在校验期间发生变化")
    return backup, database_digest, backup_digest


def _readonly_cutover_marker_digest(database: Path, cutover_id: str) -> str | None:
    """Read the raw marker before allowing a post-commit idempotent retry."""

    try:
        with sqlite3.connect(
            database.as_uri() + "?mode=ro&immutable=1", uri=True
        ) as conn:
            row = conn.execute(
                "SELECT manifest_digest FROM protocol_cutovers WHERE cutover_id=?",
                (str(cutover_id or "").strip(),),
            ).fetchone()
    except sqlite3.Error:
        return None
    return None if row is None else str(row[0] or "")


def _readonly_cutover_marker_contract(
    database: Path, cutover_id: str
) -> dict[str, str] | None:
    """Bind a rule-only lost-output retry to its complete immutable edge."""

    columns = (
        "game_id",
        "from_ruleset",
        "to_ruleset",
        "from_protocol",
        "to_protocol",
        "from_rating_pool",
        "to_rating_pool",
        "manifest_digest",
        "manifest_json",
    )
    try:
        with sqlite3.connect(
            database.as_uri() + "?mode=ro&immutable=1", uri=True
        ) as conn:
            row = conn.execute(
                f"SELECT {','.join(columns)} FROM protocol_cutovers "
                "WHERE cutover_id=?",
                (str(cutover_id or "").strip(),),
            ).fetchone()
    except sqlite3.Error:
        return None
    if row is None:
        return None
    return {column: str(row[index] or "") for index, column in enumerate(columns)}


def _cutover_plan_copy_from(source_database: Path, target_database: Path) -> Path:
    """Copy reviewed bytes beside the target so canonical assets resolve there."""

    fd, raw_path = tempfile.mkstemp(
        prefix=f".{target_database.name}.cutover-plan-",
        suffix=".db",
        dir=target_database.parent,
    )
    candidate = Path(raw_path)
    try:
        target_file = os.fdopen(fd, "wb")
        fd = -1
        with source_database.open("rb") as source, target_file as target:
            shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()
            os.fsync(target.fileno())
        candidate.chmod(0o600)
        return candidate
    except Exception:
        if fd >= 0:
            os.close(fd)
        candidate.unlink(missing_ok=True)
        raise


def _cutover_plan_copy(database: Path) -> Path:
    """Create one fsynced same-directory DB copy for zero-target-write dry-run."""

    return _cutover_plan_copy_from(database, database)


def _remove_cutover_plan_copy(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal", ""):
        Path(str(path) + suffix).unlink(missing_ok=True)


@app.command("contest-official-repair")
def contest_official_repair(
    db: str = typer.Option(..., "--db", help="待修复冷库的 canonical 绝对路径"),
    backup: str = typer.Option(..., "--backup", help="逐字节冷备的 canonical 绝对路径"),
    contest_id: int | None = typer.Option(None, "--contest-id"),
    scan_all: bool = typer.Option(False, "--scan-all", help="只读扫描全部 ready 终态赛事"),
    apply: bool = typer.Option(False, "--apply", help="提交唯一缺失末行；默认 dry-run"),
    verify: bool = typer.Option(False, "--verify", help="只读验证审核 postimage"),
    confirm_db: str = typer.Option(..., "--confirm-db"),
    confirm_contest_id: int | None = typer.Option(None, "--confirm-contest-id"),
    confirm_service_stopped: bool = typer.Option(False, "--confirm-service-stopped"),
    confirm_maintenance_ready: bool = typer.Option(False, "--confirm-maintenance-ready"),
    confirm_cold_backup: bool = typer.Option(False, "--confirm-cold-backup"),
    expect_authority_digest: str | None = typer.Option(
        None, "--expect-authority-digest"
    ),
    expect_old_official_digest: str | None = typer.Option(
        None, "--expect-old-official-digest"
    ),
    expect_repaired_official_digest: str | None = typer.Option(
        None, "--expect-repaired-official-digest"
    ),
    expect_plan_digest: str | None = typer.Option(None, "--expect-plan-digest"),
    expect_target_preimage_sha256: str | None = typer.Option(
        None, "--expect-target-preimage-sha256"
    ),
    expect_source_business_digest: str | None = typer.Option(
        None, "--expect-source-business-digest"
    ),
    expect_post_business_digest: str | None = typer.Option(
        None, "--expect-post-business-digest"
    ),
):
    """Audit or repair one exact legacy Pencil Swiss-to-KO official tail.

    This command is a cold, explicit maintenance tool.  It does not construct
    ``Store`` for the target database, run migrations, replay Match standings,
    replace existing official rows or establish a lifecycle seal.
    """
    from bzplat.backend.contests.official_repair import (
        OfficialRepairError,
        apply_official_results_repair,
        finalize_official_repair_guard,
        offline_official_repair_guard,
        plan_official_results_repair,
        scan_official_results_repairs,
        validate_official_repair_file,
        validate_official_repair_inventory,
        validate_offline_repair_database_state,
    )

    def begin_readonly_snapshot(connection: sqlite3.Connection) -> None:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        if (
            connection.execute("PRAGMA query_only").fetchone()[0] != 1
            or connection.execute("PRAGMA trusted_schema").fetchone()[0] != 0
            or connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1
        ):
            raise RuntimeError("计划副本无法启用只读 SQLite 安全参数")
        connection.execute("BEGIN")

    def read_plan(path: Path, selected_contest_id: int):
        _sqlite_readonly_health(path, label="计划副本")
        with sqlite3.connect(
            path.as_uri() + "?mode=ro&immutable=1",
            uri=True,
            isolation_level=None,
        ) as connection:
            begin_readonly_snapshot(connection)
            validate_offline_repair_database_state(connection)
            return plan_official_results_repair(connection, selected_contest_id)

    def read_scan(path: Path) -> list[dict]:
        _sqlite_readonly_health(path, label="计划副本")
        with sqlite3.connect(
            path.as_uri() + "?mode=ro&immutable=1",
            uri=True,
            isolation_level=None,
        ) as connection:
            begin_readonly_snapshot(connection)
            validate_offline_repair_database_state(connection)
            return scan_official_results_repairs(connection)

    try:
        database = validate_official_repair_file(
            db, label="目标数据库", mode=0o600
        )
        cold_backup = validate_official_repair_file(
            backup, label="冷备", mode=0o400
        )
        if database.samefile(cold_backup):
            raise RuntimeError("冷备不能与目标数据库是同一个 inode")
        if confirm_db != str(database):
            raise RuntimeError("--confirm-db 必须逐字等于目标 canonical 绝对路径")
        if not (
            confirm_service_stopped
            and confirm_maintenance_ready
            and confirm_cold_backup
        ):
            raise RuntimeError("必须分别确认停服、维护排空与冷备封存")
        if scan_all:
            if (
                apply
                or verify
                or contest_id is not None
                or confirm_contest_id is not None
            ):
                raise RuntimeError("--scan-all 仅允许独立 dry-run，不能指定赛事或 apply")
        else:
            if apply and verify:
                raise RuntimeError("--apply 与 --verify 不能同时使用")
            if (
                isinstance(contest_id, bool)
                or not isinstance(contest_id, int)
                or contest_id < 1
                or confirm_contest_id != contest_id
            ):
                raise RuntimeError("必须用相同正整数确认 --contest-id")

        with offline_official_repair_guard(database) as guard:
            _sqlite_readonly_health(database, label="目标数据库")
            _sqlite_readonly_health(cold_backup, label="冷备")
            def stable_file_stat(
                path: Path,
            ) -> tuple[int, int, int, int, int, int, int, int]:
                current = path.stat()
                return (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                    current.st_mode,
                    current.st_uid,
                    current.st_nlink,
                )

            target_stat = stable_file_stat(database)
            backup_stat = stable_file_stat(cold_backup)
            target_digest = _sha256_file(database)
            backup_digest = _sha256_file(cold_backup)
            expected_target_stat = target_stat
            expected_target_digest = target_digest
            if (
                stable_file_stat(database) != target_stat
                or stable_file_stat(cold_backup) != backup_stat
            ):
                raise RuntimeError("目标数据库或冷备在审计期间变化")

            target_copy = _cutover_plan_copy_from(database, database)
            backup_copy: Path | None = None
            try:
                if scan_all:
                    scan = read_scan(target_copy)
                    report = {
                        "mode": "scan",
                        "backup_sha256": backup_digest,
                        "blocked": sum(
                            row["eligibility"] == "blocked" for row in scan
                        ),
                        "contests": scan,
                        "repairable": sum(
                            row["eligibility"] == "repairable" for row in scan
                        ),
                        "valid": sum(
                            row["eligibility"] == "valid" for row in scan
                        ),
                        "target_preimage_sha256": target_digest,
                    }
                elif not apply and not verify:
                    if target_digest != backup_digest:
                        raise RuntimeError("dry-run 要求目标与冷备逐字节相同")
                    assert contest_id is not None
                    plan = read_plan(target_copy, contest_id)
                    if not plan.eligible:
                        raise RuntimeError("目标赛事已经修复，无需生成 apply 计划")
                    validate_official_repair_inventory(
                        read_scan(target_copy),
                        contest_id,
                        repaired=False,
                    )
                    report = {
                        "mode": "dry-run",
                        **plan.public_report(),
                        "backup_sha256": backup_digest,
                        "target_preimage_sha256": target_digest,
                    }
                else:
                    assert contest_id is not None
                    reviewed_preimage = str(
                        expect_target_preimage_sha256 or ""
                    ).lower()
                    if len(reviewed_preimage) != 64 or reviewed_preimage != backup_digest:
                        raise RuntimeError("审核 target preimage digest 与冷备不一致")
                    expected = {
                        "authority_digest": str(expect_authority_digest or "").lower(),
                        "old_official_digest": str(
                            expect_old_official_digest or ""
                        ).lower(),
                        "repaired_official_digest": str(
                            expect_repaired_official_digest or ""
                        ).lower(),
                        "plan_digest": str(expect_plan_digest or "").lower(),
                        "source_business_digest": str(
                            expect_source_business_digest or ""
                        ).lower(),
                        "expected_post_business_digest": str(
                            expect_post_business_digest or ""
                        ).lower(),
                    }
                    if any(len(value) != 64 for value in expected.values()):
                        raise RuntimeError("apply 缺少完整审核 digest")
                    backup_copy = _cutover_plan_copy_from(cold_backup, database)
                    backup_plan = read_plan(backup_copy, contest_id)
                    validate_official_repair_inventory(
                        read_scan(backup_copy),
                        contest_id,
                        repaired=False,
                    )
                    if (
                        not backup_plan.eligible
                        or backup_plan.authority_digest != expected["authority_digest"]
                        or backup_plan.old_official_digest
                        != expected["old_official_digest"]
                        or backup_plan.repaired_official_digest
                        != expected["repaired_official_digest"]
                        or backup_plan.plan_digest != expected["plan_digest"]
                        or backup_plan.source_business_digest
                        != expected["source_business_digest"]
                        or backup_plan.expected_post_business_digest
                        != expected["expected_post_business_digest"]
                    ):
                        raise RuntimeError("冷备计划与审核 digest 不一致")
                    if verify or target_digest != reviewed_preimage:
                        # Lost-output retry: prove both preimage and current
                        # postimage from immutable copies, then return before
                        # opening the target in read-write mode.
                        current_plan = read_plan(target_copy, contest_id)
                        validate_official_repair_inventory(
                            read_scan(target_copy),
                            contest_id,
                            repaired=True,
                        )
                        if (
                            not current_plan.already_applied
                            or current_plan.authority_digest
                            != backup_plan.authority_digest
                            or current_plan.repaired_official_digest
                            != backup_plan.repaired_official_digest
                            or current_plan.old_official_digest
                            != backup_plan.repaired_official_digest
                            or current_plan.source_business_digest
                            != backup_plan.expected_post_business_digest
                            or current_plan.expected_post_business_digest
                            != backup_plan.expected_post_business_digest
                        ):
                            raise RuntimeError("目标偏离审核 preimage 且不是精确 postimage")
                        report = {
                            "mode": "verify" if verify else "already-applied",
                            **current_plan.public_report(),
                            "backup_sha256": backup_digest,
                            "target_postimage_sha256": target_digest,
                            "target_preimage_sha256": reviewed_preimage,
                            "zero_write": True,
                        }
                    else:
                        current_plan = read_plan(target_copy, contest_id)
                        if current_plan.public_report() != backup_plan.public_report():
                            raise RuntimeError("目标计划与冷备计划不一致")
                        repaired = apply_official_results_repair(
                            database,
                            contest_id,
                            expected_authority_digest=expected["authority_digest"],
                            expected_old_official_digest=expected[
                                "old_official_digest"
                            ],
                            expected_repaired_official_digest=expected[
                                "repaired_official_digest"
                            ],
                            expected_plan_digest=expected["plan_digest"],
                            expected_source_business_digest=expected[
                                "source_business_digest"
                            ],
                            expected_post_business_digest=expected[
                                "expected_post_business_digest"
                            ],
                            expected_target_stat=target_stat,
                            expected_target_preimage_sha256=reviewed_preimage,
                            cold_backup_path=cold_backup,
                            expected_backup_stat=backup_stat,
                            expected_backup_sha256=backup_digest,
                            guard=guard,
                        )
                        committed_target_stat = stable_file_stat(database)
                        committed_target_digest = _sha256_file(database)
                        if stable_file_stat(database) != committed_target_stat:
                            raise RuntimeError(
                                "修复后目标数据库在建立文件基线期间变化"
                            )
                        _sqlite_readonly_health(database, label="修复后目标数据库")
                        post_plan = read_plan(database, contest_id)
                        validate_official_repair_inventory(
                            read_scan(database),
                            contest_id,
                            repaired=True,
                        )
                        if (
                            not post_plan.already_applied
                            or post_plan.authority_digest
                            != repaired.authority_digest
                            or post_plan.old_official_digest
                            != repaired.old_official_digest
                            or post_plan.repaired_official_digest
                            != repaired.repaired_official_digest
                            or post_plan.source_business_digest
                            != repaired.source_business_digest
                            or post_plan.expected_post_business_digest
                            != repaired.expected_post_business_digest
                        ):
                            raise RuntimeError(
                                "修复后目标数据库不等于事务已验证 postimage"
                            )
                        if (
                            stable_file_stat(database) != committed_target_stat
                            or _sha256_file(database) != committed_target_digest
                            or stable_file_stat(database) != committed_target_stat
                        ):
                            raise RuntimeError(
                                "修复后目标数据库在逻辑复核期间变化"
                            )
                        if _sha256_file(cold_backup) != backup_digest:
                            raise RuntimeError("冷备在 apply 期间变化")
                        expected_target_stat = committed_target_stat
                        expected_target_digest = committed_target_digest
                        report = {
                            "mode": "applied",
                            **repaired.public_report(),
                            "backup_sha256": backup_digest,
                            "target_postimage_sha256": expected_target_digest,
                            "target_preimage_sha256": reviewed_preimage,
                            "zero_write": False,
                        }
            finally:
                _remove_cutover_plan_copy(target_copy)
                if backup_copy is not None:
                    _remove_cutover_plan_copy(backup_copy)
            rendered_report = json.dumps(
                report, ensure_ascii=False, sort_keys=True, indent=2
            )

            def validate_final_state() -> None:
                validate_official_repair_file(
                    cold_backup, label="冷备", mode=0o400
                )
                if (
                    stable_file_stat(cold_backup) != backup_stat
                    or _sha256_file(cold_backup) != backup_digest
                    or stable_file_stat(cold_backup) != backup_stat
                ):
                    raise RuntimeError("冷备在 repair 审计期间变化")
                if (
                    stable_file_stat(database) != expected_target_stat
                    or _sha256_file(database) != expected_target_digest
                    or stable_file_stat(database) != expected_target_stat
                ):
                    raise RuntimeError("目标数据库在最终 repair 复核后发生变化")

            finalize_official_repair_guard(
                guard,
                database,
                validate_final_state=validate_final_state,
            )
            try:
                typer.echo(rendered_report)
                sys.stdout.flush()
            except BrokenPipeError as exc:
                raise typer.Exit(code=1) from exc
            if report.get("mode") == "scan" and int(report.get("blocked") or 0) > 0:
                raise typer.Exit(code=1)
    except typer.Exit:
        raise
    except (OfficialRepairError, OSError, RuntimeError, sqlite3.Error) as exc:
        raise typer.BadParameter(str(exc)) from exc


@app.command("rating-rebuild")
def rating_rebuild(
    db: str = typer.Option(..., "--db", help="待审计数据库的绝对路径"),
    apply: bool = typer.Option(False, "--apply", help="提交重建；默认只读 dry-run"),
    verify: bool = typer.Option(False, "--verify", help="只读校验，不一致时退出码 1"),
    expect_source_digest: str | None = typer.Option(
        None,
        "--expect-source-digest",
        "--source-digest",
        help="apply 必须回填刚审核的 dry-run source_digest",
    ),
    expect_plan_digest: str | None = typer.Option(
        None,
        "--expect-plan-digest",
        help="apply 必须回填同一 dry-run 的 plan_digest（Bot universe）",
    ),
    expect_rebuilt_projection_digest: str | None = typer.Option(
        None,
        "--expect-rebuilt-projection-digest",
        help="apply 必须回填同一 dry-run 的 rebuilt_projection_digest",
    ),
    confirm_db: str | None = typer.Option(
        None, "--confirm-db", help="apply 时逐字确认目标数据库绝对路径"
    ),
    backup: str | None = typer.Option(
        None, "--backup", help="apply 前已完成的冷备 SQLite 绝对路径"
    ),
    confirm_service_stopped: bool = typer.Option(
        False, "--confirm-service-stopped", help="确认 API/worker/scheduler 已全部停止"
    ),
    confirm_cold_backup: bool = typer.Option(
        False, "--confirm-cold-backup", help="确认 backup 是停服后生成且已校验的冷备"
    ),
):
    """按冻结评分资格和 settled_order 审计/重建排行榜投影。"""
    if apply and verify:
        raise typer.BadParameter("--apply 与 --verify 不能同时使用")
    database = Path(db)
    if not database.is_absolute():
        raise typer.BadParameter("--db 必须是显式绝对路径")
    try:
        database = database.resolve(strict=True)
    except FileNotFoundError as exc:
        raise typer.BadParameter("--db 文件不存在") from exc

    from bzplat.backend.rating.rebuild import apply_rebuild_plan, build_rebuild_plan

    plan = build_rebuild_plan(database)
    report = dict(plan.report)
    report["mode"] = "verify" if verify else "dry-run"
    if apply:
        if not confirm_service_stopped:
            raise typer.BadParameter("缺少 --confirm-service-stopped 停服确认")
        if not confirm_cold_backup:
            raise typer.BadParameter("缺少 --confirm-cold-backup 冷备确认")
        cold_backup = _validated_cold_backup(database, backup)
        if not confirm_db or not Path(confirm_db).is_absolute():
            raise typer.BadParameter("--confirm-db 必须再次填写目标绝对路径")
        try:
            confirmed = Path(confirm_db).resolve(strict=True)
        except FileNotFoundError as exc:
            raise typer.BadParameter("--confirm-db 文件不存在") from exc
        if confirmed != database:
            raise typer.BadParameter("--confirm-db 与 --db 不是同一个目标")
        reviewed_digests = {
            "--expect-source-digest": (
                expect_source_digest,
                report["source_digest"],
            ),
            "--expect-plan-digest": (
                expect_plan_digest,
                report["plan_digest"],
            ),
            "--expect-rebuilt-projection-digest": (
                expect_rebuilt_projection_digest,
                report["rebuilt_projection_digest"],
            ),
        }
        mismatched = [
            flag
            for flag, (supplied, current) in reviewed_digests.items()
            if supplied != current
        ]
        if mismatched:
            raise typer.BadParameter(
                f"{', '.join(mismatched)} 缺失或与当前单快照 dry-run 不一致；"
                "请重新审核三项摘要"
            )
        if not report["ready_to_apply"]:
            raise typer.BadParameter(
                f"评分重建 No-Go: issues={report['issues']} "
                f"running={report['running_match_count']} "
                f"execution_active={report['execution_active_count']} "
                f"dispatcher_state={report['dispatcher_state']}"
            )
        report = apply_rebuild_plan(
            database,
            expect_source_digest,
            expect_plan_digest,
            expect_rebuilt_projection_digest,
            confirmed_database=confirmed,
            backup_path=cold_backup,
            service_stopped=confirm_service_stopped,
            cold_backup_confirmed=confirm_cold_backup,
        )
        report["backup_path"] = str(cold_backup)

    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    if verify and not (
        report.get("projection_matches")
        and report.get("projection_state_current")
        and not report.get("issues")
    ):
        raise typer.Exit(code=1)


@app.command("game-contract-cutover")
def game_contract_cutover(
    db: str = typer.Option(..., "--db", help="待切换数据库的绝对路径"),
    cutover_id: str = typer.Option(..., "--cutover-id"),
    game_id: str = typer.Option(..., "--game-id"),
    from_ruleset: str = typer.Option(..., "--from-ruleset"),
    from_protocol: str = typer.Option(..., "--from-protocol"),
    from_rating_pool: str = typer.Option(..., "--from-rating-pool"),
    source_binary: str = typer.Option(
        ..., "--source-binary", help="已构建标准 ELF 的绝对路径"
    ),
    source_sha256: str = typer.Option(..., "--source-sha256"),
    source_size_bytes: int = typer.Option(..., "--source-size-bytes", min=1),
    upload_note: str = typer.Option(
        "platform standard ruleset cutover", "--upload-note"
    ),
    apply: bool = typer.Option(False, "--apply", help="提交切换；默认只生成计划"),
    expect_manifest_digest: str | None = typer.Option(
        None,
        "--expect-manifest-digest",
        help="apply 必须回填刚审核的 dry-run manifest_digest",
    ),
    expect_target_preimage_sha256: str | None = typer.Option(
        None,
        "--expect-target-preimage-sha256",
        help="apply 必须回填 dry-run 输出的 target_preimage_sha256",
    ),
    confirm_db: str | None = typer.Option(
        None, "--confirm-db", help="apply 时逐字确认目标数据库绝对路径"
    ),
    backup: str | None = typer.Option(
        None, "--backup", help="停服后生成的 SQLite 冷备绝对路径"
    ),
    confirm_service_stopped: bool = typer.Option(
        False,
        "--confirm-service-stopped",
        help="确认 API/worker/scheduler/上传预检均已停止",
    ),
    confirm_cold_backup: bool = typer.Option(
        False, "--confirm-cold-backup", help="确认 backup 是停服后的完整冷备"
    ),
):
    """规划或执行一款游戏的停服规则代际 hard cutover。"""

    database = Path(db)
    source = Path(source_binary)
    if not database.is_absolute():
        raise typer.BadParameter("--db 必须是显式绝对路径")
    if not source.is_absolute():
        raise typer.BadParameter("--source-binary 必须是显式绝对路径")
    try:
        database = database.resolve(strict=True)
        source = source.resolve(strict=True)
    except FileNotFoundError as exc:
        raise typer.BadParameter("数据库或标准 ELF 不存在") from exc
    if not database.is_file() or not source.is_file():
        raise typer.BadParameter("数据库与标准 ELF 必须是普通文件")

    if not confirm_service_stopped:
        raise typer.BadParameter("dry-run/apply 缺少 --confirm-service-stopped 停服确认")
    if not confirm_cold_backup:
        raise typer.BadParameter("dry-run/apply 缺少 --confirm-cold-backup 冷备确认")
    if not backup:
        raise typer.BadParameter("dry-run/apply 必须提供 --backup 的冷备绝对路径")
    if apply:
        if os.environ.get("BZ_BOT_LOCAL", "").lower() in {"1", "true", "yes"}:
            raise typer.BadParameter(
                "hard cutover apply 拒绝 BZ_BOT_LOCAL；标准 ELF 必须由生产 Docker runtime 预检"
            )
        if not confirm_db or not Path(confirm_db).is_absolute():
            raise typer.BadParameter("--confirm-db 必须再次填写目标绝对路径")
        try:
            confirmed = Path(confirm_db).resolve(strict=True)
        except FileNotFoundError as exc:
            raise typer.BadParameter("--confirm-db 文件不存在") from exc
        if confirmed != database:
            raise typer.BadParameter("--confirm-db 与 --db 不是同一个目标")
        if not expect_manifest_digest:
            raise typer.BadParameter("--apply 必须提供 --expect-manifest-digest")
        if not expect_target_preimage_sha256:
            raise typer.BadParameter(
                "--apply 必须提供 --expect-target-preimage-sha256"
            )

    from bzplat.backend.bots.manager import BotError, BotManager
    from bzplat.backend.store.db import offline_cutover_path_guard
    from bzplat.backend.store.schema import game_rule_contract

    from_contract = {
        "ruleset_version": from_ruleset,
        "protocol_version": from_protocol,
        "rating_pool_id": from_rating_pool,
    }
    to_contract = game_rule_contract(game_id)
    try:
        # Store.__init__ executes schema migration, so the dispatcher flock is
        # acquired from the raw absolute path before even opening SQLite.
        with offline_cutover_path_guard(database) as path_guard:
            cold_backup, target_digest, backup_digest = (
                _validated_cutover_cold_backup(
                    database,
                    backup,
                    require_target_equality=not apply,
                )
            )
            preimage_digest = target_digest
            if apply:
                reviewed_preimage = str(
                    expect_target_preimage_sha256 or ""
                ).strip().lower()
                if backup_digest != reviewed_preimage:
                    raise typer.BadParameter(
                        "--expect-target-preimage-sha256 与冷备不一致；"
                        "请使用 dry-run 时的同一冷备"
                    )
                if target_digest != reviewed_preimage:
                    marker_digest = _readonly_cutover_marker_digest(
                        database, cutover_id
                    )
                    if marker_digest != str(expect_manifest_digest or ""):
                        raise typer.BadParameter(
                            "目标 DB 已偏离审核 preimage 且不是同一"
                            " cutover marker；拒绝打开 Store"
                        )
                    # This is the lost-output/post-commit retry branch.  The
                    # migration-capable Store may open only after the raw marker
                    # binds the target to the reviewed manifest.  Manager/Store
                    # then revalidate the complete chain, assets and projection.
                    preimage_digest = reviewed_preimage
            plan_copy: Path | None = None
            store_path = database
            if not apply:
                plan_copy = _cutover_plan_copy(database)
                store_path = plan_copy
            try:
                store = Store(str(store_path))
                try:
                    offline_guard = (
                        store.bind_offline_cutover_guard(path_guard)
                        if apply
                        else None
                    )
                    manager = BotManager(
                        store,
                        upload_root=database.parent / "bot_uploads",
                        create_upload_root=apply,
                    )
                    plan = manager.plan_game_contract_cutover(
                        cutover_id=cutover_id,
                        game_id=game_id,
                        from_contract=from_contract,
                        to_contract=to_contract,
                        source_binary_path=source,
                        expected_sha256=source_sha256,
                        expected_size_bytes=source_size_bytes,
                        upload_note=upload_note,
                    )
                    report: dict = {
                        "mode": "dry-run",
                        **plan,
                        "database": str(database),
                        "backup_path": str(cold_backup),
                        "target_preimage_sha256": preimage_digest,
                        "canonical_upload_root": str(
                            database.parent / "bot_uploads"
                        ),
                    }
                    if apply:
                        if plan["manifest_digest"] != expect_manifest_digest:
                            raise typer.BadParameter(
                                "--expect-manifest-digest 与当前冷库计划不一致；"
                                "请重新审核 dry-run"
                            )
                        from bzplat.backend.runtime.binary_runner import BinaryRunner

                        from bzplat.backend.runtime.docker_supervisor import (
                            DockerSupervisor,
                        )

                        supervisor = DockerSupervisor(
                            db_path=database,
                            launch_journal=store.executions,
                        )
                        preflight_runner = BinaryRunner(
                            db_path=database,
                            supervisor=supervisor,
                            preflight_gate=threading.BoundedSemaphore(1),
                        )
                        result = manager.apply_game_contract_cutover(
                            cutover_id=cutover_id,
                            game_id=game_id,
                            from_contract=from_contract,
                            to_contract=to_contract,
                            source_binary_path=source,
                            expected_sha256=source_sha256,
                            expected_size_bytes=source_size_bytes,
                            expected_manifest_digest=expect_manifest_digest,
                            upload_note=upload_note,
                            binary_runner=preflight_runner,
                            offline_guard=offline_guard,
                        )
                        report = {
                            "mode": "applied",
                            "database": str(database),
                            "backup_path": str(cold_backup),
                            "target_preimage_sha256": preimage_digest,
                            **result,
                        }
                finally:
                    store.close()
            finally:
                if plan_copy is not None:
                    _remove_cutover_plan_copy(plan_copy)
    except BotError as exc:
        raise typer.BadParameter(f"{exc.code}: {exc.message}") from exc
    except (ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


@app.command("game-rule-cutover")
def game_rule_cutover(
    db: str = typer.Option(..., "--db", help="待切换数据库的绝对路径"),
    cutover_id: str = typer.Option(..., "--cutover-id"),
    game_id: str = typer.Option(..., "--game-id"),
    from_ruleset: str = typer.Option(..., "--from-ruleset"),
    from_protocol: str = typer.Option(..., "--from-protocol"),
    from_rating_pool: str = typer.Option(..., "--from-rating-pool"),
    migrate_unstarted_contest_id: list[int] | None = typer.Option(
        None,
        "--migrate-unstarted-contest-id",
        help="显式授权随切换迁移的未开赛 open 赛事 ID；可重复",
    ),
    apply: bool = typer.Option(False, "--apply", help="提交切换；默认只生成计划"),
    expect_plan_digest: str | None = typer.Option(
        None,
        "--expect-plan-digest",
        help="apply 必须回填刚审核的 dry-run plan_digest",
    ),
    expect_manifest_digest: str | None = typer.Option(
        None,
        "--expect-manifest-digest",
        help="apply 必须回填 dry-run 的空 manifest 摘要",
    ),
    expect_target_preimage_sha256: str | None = typer.Option(
        None,
        "--expect-target-preimage-sha256",
        help="apply 必须回填 dry-run 输出的 target_preimage_sha256",
    ),
    confirm_db: str | None = typer.Option(
        None, "--confirm-db", help="apply 时逐字确认目标数据库绝对路径"
    ),
    backup: str | None = typer.Option(
        None, "--backup", help="停服后生成的 SQLite 冷备绝对路径"
    ),
    confirm_service_stopped: bool = typer.Option(
        False,
        "--confirm-service-stopped",
        help="确认 API/worker/scheduler/上传预检均已停止",
    ),
    confirm_cold_backup: bool = typer.Option(
        False, "--confirm-cold-backup", help="确认 backup 是停服后的完整冷备"
    ),
):
    """规划或执行协议不变、规则与评分池同时换代的离线切换。"""

    database = Path(db)
    if not database.is_absolute():
        raise typer.BadParameter("--db 必须是显式绝对路径")
    try:
        database = database.resolve(strict=True)
    except FileNotFoundError as exc:
        raise typer.BadParameter("数据库不存在") from exc
    if not database.is_file():
        raise typer.BadParameter("--db 必须是普通文件")
    if not confirm_service_stopped:
        raise typer.BadParameter("dry-run/apply 缺少 --confirm-service-stopped 停服确认")
    if not confirm_cold_backup:
        raise typer.BadParameter("dry-run/apply 缺少 --confirm-cold-backup 冷备确认")
    if not backup:
        raise typer.BadParameter("dry-run/apply 必须提供 --backup 的冷备绝对路径")
    if apply:
        if not confirm_db or not Path(confirm_db).is_absolute():
            raise typer.BadParameter("--confirm-db 必须再次填写目标绝对路径")
        try:
            confirmed = Path(confirm_db).resolve(strict=True)
        except FileNotFoundError as exc:
            raise typer.BadParameter("--confirm-db 文件不存在") from exc
        if confirmed != database:
            raise typer.BadParameter("--confirm-db 与 --db 不是同一个目标")
        if not expect_plan_digest:
            raise typer.BadParameter("--apply 必须提供 --expect-plan-digest")
        if not expect_manifest_digest:
            raise typer.BadParameter("--apply 必须提供 --expect-manifest-digest")
        if not expect_target_preimage_sha256:
            raise typer.BadParameter(
                "--apply 必须提供 --expect-target-preimage-sha256"
            )

    from bzplat.backend.store.db import offline_cutover_path_guard
    from bzplat.backend.store.schema import game_rule_contract

    source = {
        "ruleset_version": from_ruleset,
        "protocol_version": from_protocol,
        "rating_pool_id": from_rating_pool,
    }
    target = game_rule_contract(game_id)
    contest_ids = tuple(migrate_unstarted_contest_id or ())
    expected_empty_manifest = hashlib.sha256(b"[]").hexdigest()
    try:
        with offline_cutover_path_guard(database) as path_guard:
            cold_backup, target_digest, backup_digest = (
                _validated_cutover_cold_backup(
                    database,
                    backup,
                    require_target_equality=not apply,
                )
            )
            preimage_digest = target_digest
            lost_output_retry = False
            if apply:
                reviewed_preimage = str(
                    expect_target_preimage_sha256 or ""
                ).strip().lower()
                if backup_digest != reviewed_preimage:
                    raise typer.BadParameter(
                        "--expect-target-preimage-sha256 与冷备不一致；"
                        "请使用 dry-run 时的同一冷备"
                    )
                if str(expect_manifest_digest or "") != expected_empty_manifest:
                    raise typer.BadParameter(
                        "--expect-manifest-digest 不是 rule-only 空 manifest 摘要"
                    )
                if target_digest != reviewed_preimage:
                    marker = _readonly_cutover_marker_contract(database, cutover_id)
                    expected_marker = {
                        "game_id": game_id,
                        "from_ruleset": source["ruleset_version"],
                        "to_ruleset": target["ruleset_version"],
                        "from_protocol": source["protocol_version"],
                        "to_protocol": target["protocol_version"],
                        "from_rating_pool": source["rating_pool_id"],
                        "to_rating_pool": target["rating_pool_id"],
                        "manifest_digest": expected_empty_manifest,
                        "manifest_json": "[]",
                    }
                    if marker != expected_marker:
                        raise typer.BadParameter(
                            "目标 DB 已偏离审核 preimage 且没有完整匹配的"
                            " rule-only cutover marker；拒绝打开 Store"
                        )
                    lost_output_retry = True
                    preimage_digest = reviewed_preimage

            if lost_output_retry:
                reviewed_copy = _cutover_plan_copy_from(cold_backup, database)
                try:
                    reviewed_store = Store(str(reviewed_copy))
                    try:
                        reviewed_plan = reviewed_store.plan_game_rule_cutover(
                            cutover_id=cutover_id,
                            game_id=game_id,
                            from_contract=source,
                            to_contract=target,
                            migrate_unstarted_contest_ids=contest_ids,
                        )
                    finally:
                        reviewed_store.close()
                finally:
                    _remove_cutover_plan_copy(reviewed_copy)
                if reviewed_plan.get("plan_digest") != str(
                    expect_plan_digest or ""
                ).strip().lower():
                    raise typer.BadParameter(
                        "--expect-plan-digest 与已绑定冷备中的原审核计划不一致"
                    )
                if reviewed_plan.get("manifest_digest") != str(
                    expect_manifest_digest or ""
                ).strip().lower():
                    raise typer.BadParameter(
                        "--expect-manifest-digest 与已绑定冷备计划不一致"
                    )

            plan_copy: Path | None = None
            store_path = database
            if not apply:
                plan_copy = _cutover_plan_copy(database)
                store_path = plan_copy
            try:
                store = Store(str(store_path))
                try:
                    if not apply:
                        plan = store.plan_game_rule_cutover(
                            cutover_id=cutover_id,
                            game_id=game_id,
                            from_contract=source,
                            to_contract=target,
                            migrate_unstarted_contest_ids=contest_ids,
                        )
                        report: dict[str, object] = {
                            "mode": "dry-run",
                            **plan,
                            "database": str(database),
                            "backup_path": str(cold_backup),
                            "target_preimage_sha256": preimage_digest,
                        }
                    else:
                        offline_guard = store.bind_offline_cutover_guard(path_guard)
                        if not lost_output_retry:
                            plan = store.plan_game_rule_cutover(
                                cutover_id=cutover_id,
                                game_id=game_id,
                                from_contract=source,
                                to_contract=target,
                                migrate_unstarted_contest_ids=contest_ids,
                            )
                            if plan.get("plan_digest") != str(
                                expect_plan_digest or ""
                            ).strip().lower():
                                raise typer.BadParameter(
                                    "--expect-plan-digest 与当前冷库计划不一致；"
                                    "请重新审核 dry-run"
                                )
                            if plan.get("manifest_digest") != str(
                                expect_manifest_digest or ""
                            ).strip().lower():
                                raise typer.BadParameter(
                                    "--expect-manifest-digest 与当前计划不一致"
                                )
                        applied = store.apply_game_rule_cutover(
                            cutover_id=cutover_id,
                            game_id=game_id,
                            from_contract=source,
                            to_contract=target,
                            expected_plan_digest=str(expect_plan_digest or ""),
                            offline_guard=offline_guard,
                            migrate_unstarted_contest_ids=contest_ids,
                        )
                        report = {
                            "mode": "applied",
                            "database": str(database),
                            "backup_path": str(cold_backup),
                            "target_preimage_sha256": preimage_digest,
                            **applied,
                        }
                finally:
                    store.close()
            finally:
                if plan_copy is not None:
                    _remove_cutover_plan_copy(plan_copy)
    except (ValueError, RuntimeError) as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))


if __name__ == "__main__":
    app()
