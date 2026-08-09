"""CLI：serve / 管理辅助。"""
from __future__ import annotations

import logging
import os
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


if __name__ == "__main__":
    app()
