"""FastAPI 应用工厂。"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
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
from bzplat.backend.communications.api import router as communications_router
from bzplat.backend.communications.feedback import FeedbackService
from bzplat.backend.communications.service import CommunicationService
from bzplat.backend.communications.worker import DeliveryWorker
from bzplat.backend.mail import Mailer
from bzplat.backend.matches import MatchOrchestrator, MatchRunner
from bzplat.backend.matches.execution_queue import ExecutionDispatcher
from bzplat.backend.notifications import NotificationManager
from bzplat.backend.qa_safety import (
    assert_qa_database_isolated,
    assert_qa_runtime_path_isolated,
    assert_qa_upload_root_isolated,
    qa_instance_enabled,
)
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.runtime.config import (
    ACTION_TIMEOUT_SEC,
)
from bzplat.backend.runtime.docker_supervisor import (
    DockerSupervisor,
    validate_local_docker_configuration,
)
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
    bug_attachments_dir = Path(db_path).expanduser().resolve().parent / "bug_attachments"
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
        bug_attachments_dir = assert_qa_runtime_path_isolated(
            bug_attachments_dir,
            source_root,
            purpose="BZ_QA_INSTANCE Bug 附件目录",
        )
    prefer_local = os.environ.get("BZ_BOT_LOCAL", "").lower() in ("1", "true", "yes")
    if not prefer_local:
        # Reject remote/custom production configuration before Store can create
        # or migrate the selected DB. Commands independently pin the same socket.
        validate_local_docker_configuration()
    store = Store(db_path)
    _seed_site_settings(store)
    effective_conc = _effective_max_concurrent(max_concurrent)

    mailer = Mailer()
    communications = CommunicationService(store)
    if mailer.config.configured:
        logger.info("SMTP configured host=%s", mailer.config.host)
    else:
        logger.warning("SMTP 未配置：邮件会排队并按退避策略失败，不阻断业务请求")
    auth = AuthManager(store, mailer=mailer, communications=communications)
    captcha = CaptchaStore()
    bot_manager = BotManager(store, upload_root=upload_root)
    execution_dispatcher: ExecutionDispatcher | None = None
    shared_supervisor = (
        None
        if prefer_local
        else DockerSupervisor(
            db_path=db_path,
            launch_journal=store.executions,
        )
    )
    # Upload preflight is intentionally a single, bounded lane outside the
    # match queue. It is shared by every worker-thread runner factory, so the
    # physical upper bound is execution sandbox units + one preflight sandbox.
    preflight_gate = threading.BoundedSemaphore(1)

    def _pause_for_unscoped_docker(reason: str) -> None:
        launch = store.executions.docker_launch()
        store.executions.pause_for_docker_uncertainty(
            f"Docker 控制不确定：{reason}",
            # A live callback runs on the same boot that wrote its create
            # intent.  It therefore cannot use two zero samples as recovery.
            manual=launch["state"] == "creating",
        )
        if execution_dispatcher is not None:
            execution_dispatcher.wake()

    binary_runner = BinaryRunner(
        prefer_local=prefer_local,
        db_path=db_path,
        docker_uncertain_callback=_pause_for_unscoped_docker,
        supervisor=shared_supervisor,
    )

    match_runner = MatchRunner(binary_runner, action_timeout=ACTION_TIMEOUT_SEC)
    orch = MatchOrchestrator(store, runner=match_runner, max_concurrent=effective_conc)
    contest_manager = ContestManager(store, orch)
    # QA capability guard is independent from the persisted administrator switch:
    # a copied production DB may say enabled, but an isolated QA process must never
    # write background ladder matches.
    execution_dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=effective_conc,
        max_sandbox_units=effective_conc * 2,
        auto_capability_enabled=not qa_instance,
        contest_reconciler=contest_manager.reconcile_running_contests,
    )

    async def _on_match_done(match_id: str, contest_id: int | None) -> None:
        try:
            if contest_id is not None:
                # 必须传 match_id：completed 才能进积分/晋级；aborted
                # 需先精确复位其 pairing 供重派，不能当作已裁决终态。
                await contest_manager.handle_match_done(
                    match_id,
                    contest_id,
                    retry_aborted=orch.is_admin_abort_handoff(match_id),
                )
        finally:
            # Match completion wakes the shared dispatcher.  Capacity remains
            # occupied until the attempt's exact label cleanup is confirmed.
            execution_dispatcher.wake()

    orch.on_match_done = _on_match_done

    # 旧通知门面（写 communications 真相 + 兼容投影；邮件只排队）
    notifier = NotificationManager(store, communications=communications)
    orch.notifier = notifier
    feedback = FeedbackService(store, bug_attachments_dir)
    delivery_worker = DeliveryWorker(communications.repository, mailer)

    if qa_instance:
        logger.info("隔离 QA 实例已由 capability guard 强制禁用 auto-match")

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Exact instance-label cleanup is the only recovery gate.  Only after it
        # proves zero may active attempts be requeued/interrupted and legacy
        # untracked running rows be marked orphaned.
        dispatcher_start = await execution_dispatcher.start()
        logger.info("execution dispatcher startup: %s", dispatcher_start["outcome"])
        # Legacy orphan recovery, rating repair and contest reconciliation are
        # owned by ExecutionDispatcher so startup and delayed pause -> resume
        # cannot drift into different recovery pipelines.
        task = asyncio.create_task(
            execution_dispatcher.loop(), name="execution-dispatcher"
        )
        _app.state.execution_dispatcher = execution_dispatcher
        _app.state._execution_dispatcher_task = task
        # 赛事时间调度器：后台周期扫描赛事 *_at 字段，到点自动推进阶段
        # （开放报名/截止报名出排期/到点开打/rest 恢复）。
        from bzplat.backend.contests.scheduler import ContestScheduler
        contest_scheduler = ContestScheduler(contest_manager, store)
        sched_task = asyncio.create_task(contest_scheduler.loop(), name="contest-scheduler")
        _app.state.contest_scheduler = contest_scheduler
        _app.state._contest_sched_task = sched_task
        delivery_task = asyncio.create_task(
            delivery_worker.loop(), name="communications-delivery"
        )
        _app.state.delivery_worker = delivery_worker
        _app.state._delivery_worker_task = delivery_task
        try:
            yield
        finally:
            await execution_dispatcher.stop()
            task.cancel()
            sched_task.cancel()
            delivery_task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            try:
                await sched_task
            except asyncio.CancelledError:
                pass
            try:
                await delivery_task
            except asyncio.CancelledError:
                pass
            # Match tasks can be inside asyncio subprocess pipe setup.  Drain
            # them explicitly before the server closes the event loop; relying
            # on loop-wide cancellation can otherwise hang shutdown forever.
            await orch.shutdown()
            await execution_dispatcher.close()

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
        prefer_local=prefer_local,
        db_path=db_path,
        docker_uncertain_callback=_pause_for_unscoped_docker,
        supervisor=shared_supervisor,
        preflight_gate=preflight_gate,
    )
    app.state.preflight_gate = preflight_gate
    app.state.orch = orch
    app.state.contest_manager = contest_manager
    app.state.mailer = mailer
    app.state.communications = communications
    app.state.feedback = feedback
    app.state.delivery_worker = delivery_worker
    app.state.notifier = notifier
    app.state.execution_dispatcher = execution_dispatcher
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
    app.include_router(communications_router)

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
