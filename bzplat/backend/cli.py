"""CLI：serve / 管理辅助。"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
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
    setup_logging(level=os.environ.get("BZ_LOG_LEVEL", "INFO"))
    logging.getLogger(__name__).info("botbattle 启动 host=%s port=%s reload=%s", host, port, reload)

    uvicorn.run(
        "bzplat.backend.main:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level="info",
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
        raise typer.BadParameter("--apply 必须提供 --backup 的冷备绝对路径")
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


@app.command("rating-rebuild")
def rating_rebuild(
    db: str = typer.Option(..., "--db", help="待审计数据库的绝对路径"),
    apply: bool = typer.Option(False, "--apply", help="提交重建；默认只读 dry-run"),
    verify: bool = typer.Option(False, "--verify", help="只读校验，不一致时退出码 1"),
    source_digest: str | None = typer.Option(
        None, "--source-digest", help="apply 必须回填刚审核的 dry-run source_hash"
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
        if source_digest != report["source_hash"]:
            raise typer.BadParameter(
                "--source-digest 与当前只读 dry-run 不一致；请重新审核报告"
            )
        if not report["ready_to_apply"]:
            raise typer.BadParameter(
                f"评分源未收敛，拒绝 apply: {report['issues']} "
                f"running={report['running_match_count']}"
            )
        report = apply_rebuild_plan(
            database,
            source_digest,
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


if __name__ == "__main__":
    app()
