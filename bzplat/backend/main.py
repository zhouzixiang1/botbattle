"""FastAPI 应用工厂。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
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
from bzplat.backend.contests.templates import list_templates
from bzplat.backend.mail import Mailer
from bzplat.backend.matches import MatchOrchestrator, MatchRunner
from bzplat.backend.matches.auto_matcher import AutoMatchScheduler
from bzplat.backend.runtime import BinaryRunner
from bzplat.backend.runtime.limits import (
    BOT_CPUS,
    BOT_MEMORY_MB,
    clamp_concurrent,
    concurrent_ceiling,
    default_max_concurrent,
)
from bzplat.backend.security import RateLimitMiddleware, SecurityHeadersMiddleware
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    SETTING_ACTION_TIMEOUT,
    SETTING_AUTO_MATCH_BOT_COOLDOWN,
    SETTING_AUTO_MATCH_ENABLED,
    SETTING_AUTO_MATCH_INTERVAL_SEC,
    SETTING_AUTO_MATCH_MIN_IDLE_SEC,
    SETTING_AUTO_MATCH_RESERVE_SLOTS,
    SETTING_AUTO_MATCH_STALE_SEC,
    SETTING_BOT_CPUS,
    SETTING_BOT_MEMORY,
    SETTING_CONTEST_REST,
    SETTING_CONTEST_TEMPLATES,
    SETTING_FULL_RR_MAX_N,
    SETTING_JUDGE_GOMOKU_SIZE,
    SETTING_JUDGE_HOLDEM_BB,
    SETTING_JUDGE_HOLDEM_HANDS,
    SETTING_JUDGE_HOLDEM_SB,
    SETTING_JUDGE_HOLDEM_STACK,
    SETTING_MAX_CONCURRENT,
)

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


def _seed_runtime_settings(store: Store, env_max: int | None) -> int:
    """写入默认 platform_settings；返回生效并发。"""
    ceiling = concurrent_ceiling()
    seed_conc = clamp_concurrent(
        env_max if env_max is not None else default_max_concurrent()
    )
    store.seed_setting_if_absent(SETTING_ACTION_TIMEOUT, "60")
    store.seed_setting_if_absent(SETTING_MAX_CONCURRENT, str(seed_conc))
    store.seed_setting_if_absent(SETTING_BOT_CPUS, str(BOT_CPUS))
    store.seed_setting_if_absent(SETTING_BOT_MEMORY, str(BOT_MEMORY_MB))
    store.seed_setting_if_absent(SETTING_CONTEST_REST, "10")
    store.seed_setting_if_absent(SETTING_FULL_RR_MAX_N, "12")
    # 裁判规则参数默认值（与各引擎常量对齐；admin 可在 Web 上热调）
    store.seed_setting_if_absent(SETTING_JUDGE_GOMOKU_SIZE, "15")
    store.seed_setting_if_absent(SETTING_JUDGE_HOLDEM_STACK, "20000")
    store.seed_setting_if_absent(SETTING_JUDGE_HOLDEM_SB, "50")
    store.seed_setting_if_absent(SETTING_JUDGE_HOLDEM_BB, "100")
    store.seed_setting_if_absent(SETTING_JUDGE_HOLDEM_HANDS, "70")
    store.seed_setting_if_absent(
        SETTING_CONTEST_TEMPLATES,
        json.dumps(list_templates(), ensure_ascii=False),
    )
    # 闲时自动对局（默认启用，admin 可关）
    store.seed_setting_if_absent(SETTING_AUTO_MATCH_ENABLED, "1")
    store.seed_setting_if_absent(SETTING_AUTO_MATCH_INTERVAL_SEC, "30")
    store.seed_setting_if_absent(SETTING_AUTO_MATCH_MIN_IDLE_SEC, "5")
    store.seed_setting_if_absent(SETTING_AUTO_MATCH_BOT_COOLDOWN, "600")
    store.seed_setting_if_absent(SETTING_AUTO_MATCH_STALE_SEC, "3600")
    store.seed_setting_if_absent(SETTING_AUTO_MATCH_RESERVE_SLOTS, "1")
    # 生效值永远压在 ceiling 内
    raw = store.get_setting(SETTING_MAX_CONCURRENT)
    try:
        requested = int(raw or seed_conc)
    except ValueError:
        requested = seed_conc
    effective = clamp_concurrent(requested)
    if effective != requested:
        store.set_setting(SETTING_MAX_CONCURRENT, str(effective))
    return effective


def create_app(
    *,
    db_path: str | None = None,
    upload_root: str | Path = "bot_uploads",
    max_concurrent: int | None = None,
) -> FastAPI:
    _load_dotenv()
    db_path = db_path or os.environ.get("BZ_DB_PATH", "botzone.db")
    env_max = max_concurrent
    if env_max is None and os.environ.get("BZ_MAX_CONCURRENT_MATCHES"):
        env_max = int(os.environ["BZ_MAX_CONCURRENT_MATCHES"])
    prefer_local = os.environ.get("BZ_BOT_LOCAL", "").lower() in ("1", "true", "yes")

    store = Store(db_path)
    effective_conc = _seed_runtime_settings(store, env_max)

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

    timeout_raw = store.get_setting(SETTING_ACTION_TIMEOUT) or "60"
    try:
        action_timeout = float(timeout_raw)
    except ValueError:
        action_timeout = 60.0
    match_runner = MatchRunner(binary_runner, action_timeout=action_timeout)
    orch = MatchOrchestrator(store, runner=match_runner, max_concurrent=effective_conc)
    contest_manager = ContestManager(store, orch)

    async def _on_match_done(_match_id: str, contest_id: int | None) -> None:
        if contest_id is not None:
            await contest_manager.maybe_finish(contest_id)

    orch.on_match_done = _on_match_done

    # 闲时自动对局调度器（单进程单事件循环；启动即挂载后台任务）
    auto_matcher = AutoMatchScheduler(orch, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        task = asyncio.create_task(auto_matcher.loop(), name="auto-match")
        _app.state.auto_matcher = auto_matcher
        _app.state._auto_match_task = task
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="botzone-platform", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.auth = auth
    app.state.auth_manager = auth
    app.state.captcha = captcha
    app.state.captcha_store = captcha
    app.state.bot_manager = bot_manager
    app.state.orch = orch
    app.state.contest_manager = contest_manager
    app.state.mailer = mailer
    app.state.auto_matcher = auto_matcher
    app.state.runtime_ceiling = concurrent_ceiling()

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
            "max_concurrent": orch.max_concurrent,
            "ceiling": concurrent_ceiling(),
        }

    # 静态前端
    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    wiki_assets = Path(__file__).resolve().parents[2] / "wiki" / "assets"
    if wiki_assets.is_dir():
        app.mount(
            "/wiki-assets",
            StaticFiles(directory=str(wiki_assets)),
            name="wiki-assets",
        )

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
