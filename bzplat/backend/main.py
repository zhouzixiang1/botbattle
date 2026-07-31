"""FastAPI 应用工厂。"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from bzplat.backend.api_routes import router as api_router
from bzplat.backend.auth.auth_manager import AuthManager
from bzplat.backend.auth.captcha import CaptchaStore
from bzplat.backend.auth.routes import router as auth_router
from bzplat.backend.bots import BotManager
from bzplat.backend.contests import ContestManager
from bzplat.backend.mail import Mailer
from bzplat.backend.matches import MatchOrchestrator, MatchRunner
from bzplat.backend.runtime import BinaryRunner
from bzplat.backend.security import RateLimitMiddleware, SecurityHeadersMiddleware
from bzplat.backend.store import Store

logger = logging.getLogger(__name__)


def _load_dotenv() -> None:
    """ensure create_app also sees .env when imported outside CLI."""
    path = Path(".env")
    if not path.is_file():
        # also try project root relative to this file
        alt = Path(__file__).resolve().parents[2] / ".env"
        path = alt if alt.is_file() else path
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def create_app(
    *,
    db_path: str | None = None,
    upload_root: str | Path = "bot_uploads",
    max_concurrent: int | None = None,
) -> FastAPI:
    _load_dotenv()
    db_path = db_path or os.environ.get("BZ_DB_PATH", "botzone.db")
    max_concurrent = max_concurrent or int(
        os.environ.get("BZ_MAX_CONCURRENT_MATCHES", "2")
    )
    prefer_local = os.environ.get("BZ_BOT_LOCAL", "").lower() in ("1", "true", "yes")

    store = Store(db_path)
    mailer = Mailer()
    if mailer.config.configured:
        logger.info("SMTP configured host=%s user=%s", mailer.config.host, mailer.config.user)
        auth = AuthManager(store, mailer=mailer)
    else:
        logger.warning("SMTP 未配置：注册/重置密码将无法发信（请设置 SMTP_*）")
        auth = AuthManager(store, mailer=None)
    captcha = CaptchaStore()
    bot_manager = BotManager(store, upload_root=upload_root)
    binary_runner = BinaryRunner(prefer_local=prefer_local)
    match_runner = MatchRunner(binary_runner)
    orch = MatchOrchestrator(store, runner=match_runner, max_concurrent=max_concurrent)
    contest_manager = ContestManager(store, orch)

    # 对局结束后，若属于某比赛则尝试归档（所有对阵完成时 status → finished）
    def _on_match_done(_match_id: str, contest_id: int | None) -> None:
        if contest_id is not None:
            contest_manager.maybe_finish(contest_id)

    orch.on_match_done = _on_match_done

    app = FastAPI(title="botzone-platform", version="0.1.0")
    app.state.store = store
    app.state.auth = auth
    app.state.auth_manager = auth
    app.state.captcha = captcha
    app.state.captcha_store = captcha
    app.state.bot_manager = bot_manager
    app.state.orch = orch
    app.state.contest_manager = contest_manager
    app.state.mailer = mailer

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.include_router(auth_router)
    app.include_router(api_router)

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "smtp_configured": mailer.config.configured,
            "db": db_path,
        }

    # 静态前端
    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    if dist.is_dir():
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/")
        def index():
            return FileResponse(dist / "index.html")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            # API 已由前面路由处理；其余走 SPA
            candidate = dist / full_path
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist / "index.html")

    return app
