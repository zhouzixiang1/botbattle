"""FastAPI 应用工厂。"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
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
from bzplat.backend.matches.auto_matcher import AutoMatchScheduler
from bzplat.backend.notifications import NotificationManager
from bzplat.backend.qa_safety import (
    assert_qa_database_isolated,
    assert_qa_runtime_path_isolated,
    assert_qa_upload_root_isolated,
    qa_instance_enabled,
)
from bzplat.backend.runtime import BinaryRunner
from bzplat.backend.runtime.config import ACTION_TIMEOUT_SEC
from bzplat.backend.runtime.limits import (
    clamp_concurrent,
    concurrent_ceiling,
    default_max_concurrent,
)
from bzplat.backend.security import (
    AccessLogMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
)
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


def _seed_site_settings(store: Store) -> None:
    """仅初始化仍由管理端维护的站点文案；运行参数不写数据库。"""
    from bzplat.backend.store.schema import (
        SETTING_SITE_NAME, SETTING_SITE_ANNOUNCEMENT, SETTING_SITE_ABOUT,
    )
    store.seed_setting_if_absent(SETTING_SITE_NAME, "Botbattle")
    store.seed_setting_if_absent(SETTING_SITE_ANNOUNCEMENT, "")
    store.seed_setting_if_absent(SETTING_SITE_ABOUT, "多游戏 Bot 线上对战平台")


def _effective_max_concurrent(requested: int | None) -> int:
    """返回代码配置（或显式测试注入）经机器硬顶钳制后的并发。"""
    return clamp_concurrent(
        default_max_concurrent() if requested is None else requested
    )


def create_app(
    *,
    db_path: str | None = None,
    upload_root: str | Path | None = None,
    max_concurrent: int | None = None,
) -> FastAPI:
    _load_dotenv()
    db_path = db_path or os.environ.get("BZ_DB_PATH", "botzone.db")
    qa_instance = qa_instance_enabled(os.environ.get("BZ_QA_INSTANCE"))
    avatar_raw = os.environ.get("BZ_AVATAR_DIR")
    avatars_dir = (
        Path(avatar_raw)
        if avatar_raw
        else (
            Path(db_path).expanduser().resolve().parent / "avatars"
            if qa_instance
            else Path("avatars")
        )
    )
    if upload_root is None:
        # Explicit/temporary DBs must not silently share the caller's production
        # bot_uploads directory. For the normal CWD botzone.db this remains ./bot_uploads.
        upload_root = Path(db_path).expanduser().resolve().parent / "bot_uploads"
    if qa_instance:
        source_root = Path(__file__).resolve().parents[2]
        # Both checks happen before Store/BotManager constructors can create or migrate
        # anything. The public health marker is emitted only after these pass.
        db_path = str(assert_qa_database_isolated(db_path, source_root))
        upload_root = assert_qa_upload_root_isolated(upload_root, source_root)
        avatars_dir = assert_qa_runtime_path_isolated(
            avatars_dir,
            source_root,
            purpose="BZ_QA_INSTANCE 头像目录",
        )
    prefer_local = os.environ.get("BZ_BOT_LOCAL", "").lower() in ("1", "true", "yes")
    store = Store(db_path)
    _seed_site_settings(store)
    effective_conc = _effective_max_concurrent(max_concurrent)

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

    match_runner = MatchRunner(binary_runner, action_timeout=ACTION_TIMEOUT_SEC)
    orch = MatchOrchestrator(store, runner=match_runner, max_concurrent=effective_conc)
    contest_manager = ContestManager(store, orch)

    async def _on_match_done(match_id: str, contest_id: int | None) -> None:
        if contest_id is not None:
            # 必须传 match_id：completed 才能进积分/晋级；aborted
            # 需先精确复位其 pairing 供重派，不能当作已裁决终态。
            await contest_manager.handle_match_done(
                match_id,
                contest_id,
                retry_aborted=orch.is_admin_abort_handoff(match_id),
            )

    orch.on_match_done = _on_match_done

    # 通知管理器（写站内通知 + 按用户 prefs 可选发邮件）
    notifier = NotificationManager(store, mailer=mailer)
    orch.notifier = notifier

    # 闲时自动对局调度器（单进程单事件循环；启动即挂载后台任务）
    auto_matcher = AutoMatchScheduler(orch, store)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # 启动时清理孤儿对局：上次进程非正常退出时，DB 里残留的 status=running
        # 记录（含人类对局）已无对应内存协程/Future，永久卡死。统一标 aborted。
        recovered = store.recover_orphan_matches()
        if recovered:
            logger.warning(
                "启动清理孤儿对局 %d 场（标记为 aborted）", recovered
            )
        # completed 业务结果与全局评分是两个事务：若上次进程在二者之间退出，
        # 用持久化 result/winner 补算。settlement claim 与评分同事务，重复启动
        # 无副作用；这里只补评分，不重发通知或重复奖励 XP。必须先于 auto-match。
        rating_recovered = await orch.recover_unsettled_match_ratings()
        if rating_recovered:
            logger.warning("启动补算未结算评分 %d 场", rating_recovered)
        # 启动对账：让 published/running/rest 赛事收敛，并补算 finished+ready=0 正式榜。
        # 修复「赛事卡 running」——match 全完成但 maybe_finish 回调丢失/被吞、或 match 被
        # orphan 清成 aborted 但赛事状态未同步。详见 ContestManager.reconcile_running_contests。
        reconciled = await contest_manager.reconcile_running_contests()
        if reconciled:
            logger.info("启动对账 %d 场赛事状态收敛", reconciled)
        task = asyncio.create_task(auto_matcher.loop(), name="auto-match")
        _app.state.auto_matcher = auto_matcher
        _app.state._auto_match_task = task
        # 赛事时间调度器：后台周期扫描赛事 *_at 字段，到点自动推进阶段
        # （开放报名/截止报名出排期/到点开打/rest 恢复）。仿 auto_matcher.loop()。
        from bzplat.backend.contests.scheduler import ContestScheduler
        contest_scheduler = ContestScheduler(contest_manager, store)
        sched_task = asyncio.create_task(contest_scheduler.loop(), name="contest-scheduler")
        _app.state.contest_scheduler = contest_scheduler
        _app.state._contest_sched_task = sched_task
        try:
            yield
        finally:
            task.cancel()
            sched_task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            try:
                await sched_task
            except asyncio.CancelledError:
                pass
            # Match tasks can be inside asyncio subprocess pipe setup.  Drain
            # them explicitly before the server closes the event loop; relying
            # on loop-wide cancellation can otherwise hang shutdown forever.
            await orch.shutdown()

    app = FastAPI(title="botzone-platform", version="0.1.0", lifespan=lifespan)
    app.state.store = store
    app.state.auth = auth
    app.state.auth_manager = auth
    app.state.captcha = captcha
    app.state.captcha_store = captcha
    app.state.bot_manager = bot_manager
    app.state.binary_runner = binary_runner
    # Upload preflight runs in a worker thread and must own its BinaryRunner.
    # Sharing the orchestrator runner across event loops/threads would race its
    # session map and subprocess transports.
    app.state.preflight_runner_factory = lambda: BinaryRunner(
        prefer_local=prefer_local
    )
    app.state.orch = orch
    app.state.contest_manager = contest_manager
    app.state.mailer = mailer
    app.state.notifier = notifier
    app.state.auto_matcher = auto_matcher
    # Avatar writes and StaticFiles must share the exact preflight-validated path.
    # Routes must not resolve BZ_AVATAR_DIR independently after app creation.
    app.state.avatar_dir = avatars_dir
    app.state.runtime_ceiling = concurrent_ceiling()

    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RateLimitMiddleware)
    # AccessLog 最后 add = 最外层，记录所有请求（含被限流的 429）
    app.add_middleware(AccessLogMiddleware)
    app.include_router(auth_router)
    app.include_router(api_router)

    @app.get("/api/health")
    def health():
        return {
            "ok": True,
            "smtp_configured": mailer.config.configured,
            # Never expose the server filesystem path publicly. Browser/API QA uses
            # the explicit marker to reject a Vite proxy accidentally targeting main.
            "qa_instance": qa_instance,
            "max_concurrent": orch.max_concurrent,
            "ceiling": concurrent_ceiling(),
        }

    # /api/* 未匹配路由一律返 JSON 404，绝不走下方 SPA catch-all（否则客户端收到
    # 200 + index.html，前端 api.ts 会把 HTML 当返回值解析成静默错误数据）。
    # 必须在 catch-all（/{full_path:path}）之前注册；放 if dist.is_dir() 块外，
    # 保证 dev 模式（无 dist）也一致返 JSON 404。
    @app.api_route(
        "/api/{rest:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    def api_not_found(rest: str):
        raise HTTPException(404, "Not Found")

    # 静态前端
    dist = Path(__file__).resolve().parents[1] / "frontend" / "dist"
    wiki_assets = Path(__file__).resolve().parents[2] / "wiki" / "assets"
    if wiki_assets.is_dir():
        app.mount(
            "/wiki-assets",
            StaticFiles(directory=str(wiki_assets)),
            name="wiki-assets",
        )

    # 头像静态托管（avatars/<uid>.<ext>）
    avatars_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/avatars", StaticFiles(directory=str(avatars_dir)), name="avatars")

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
