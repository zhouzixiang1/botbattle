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

    # 统一日志：文件 + 控制台（uvicorn.run 前生效，确保所有模块落 app.log）
    from bzplat.backend.logging_config import setup_logging
    setup_logging(level=os.environ.get("BZ_LOG_LEVEL", "INFO"))
    logging.getLogger(__name__).info("botbattle 启动 host=%s port=%s reload=%s", host, port, reload)

    host = host or os.environ.get("BZ_HOST", "127.0.0.1")
    port = port or int(os.environ.get("BZ_PORT", "50380"))
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
