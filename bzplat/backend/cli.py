"""CLI：serve / 管理辅助。"""
from __future__ import annotations

import json
import hashlib
import logging
import os
import shutil
import sqlite3
import tempfile
import threading
from pathlib import Path

import typer
import uvicorn

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store
from bzplat.backend.store.schema import ROLE_ADMIN

app = typer.Typer(help="botzone-platform CLI", no_args_is_help=True)


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
    for suffix in ("-wal", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise typer.BadParameter(f"{label} 存在非空 SQLite {suffix}，不是冷库")
    try:
        with sqlite3.connect(
            path.as_uri() + "?mode=ro&immutable=1", uri=True
        ) as conn:
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


def _cutover_plan_copy(database: Path) -> Path:
    """Create one fsynced same-directory DB copy for zero-target-write dry-run."""

    fd, raw_path = tempfile.mkstemp(
        prefix=f".{database.name}.cutover-plan-",
        suffix=".db",
        dir=database.parent,
    )
    candidate = Path(raw_path)
    try:
        target_file = os.fdopen(fd, "wb")
        fd = -1
        with database.open("rb") as source, target_file as target:
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


def _remove_cutover_plan_copy(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal", ""):
        Path(str(path) + suffix).unlink(missing_ok=True)


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


if __name__ == "__main__":
    app()
