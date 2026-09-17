"""FastAPI 应用工厂。"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, HTTPException
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from bzplat.backend.api_routes import router as api_router
from bzplat.backend.auth.auth_manager import COOKIE_NAME, AuthManager
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
from bzplat.backend.runtime.binary_runner import (
    DEFAULT_IMAGE_PREPARE_TIMEOUT,
    BinaryRunner,
    _ensure_linux_image_ready_sync,
)
from bzplat.backend.runtime.config import (
    ACTION_TIMEOUT_SEC,
    BOT_UPLOAD_ADMISSION_SLOTS,
    HUMAN_WS_HANDSHAKE_MAX_ATTEMPTS,
    HUMAN_WS_HANDSHAKE_MAX_BUCKETS,
    HUMAN_WS_HANDSHAKE_MAX_INFLIGHT,
    HUMAN_WS_HANDSHAKE_WINDOW_SECONDS,
    USER_STORAGE_SWEEP_INTERVAL_SEC,
)
from bzplat.backend.runtime.docker_supervisor import (
    DockerExecutionIdentity,
    DockerSupervisor,
    validate_local_docker_configuration,
)
from bzplat.backend.runtime.limits import (
    BOT_BUILD_PROFILE,
    clamp_concurrent,
    concurrent_ceiling,
    default_max_concurrent,
    effective_host_resource_budget,
)
from bzplat.backend.runtime.local_ai_service import LocalAIService
from bzplat.backend.runtime.websocket_gate import WebSocketHandshakeGate
from bzplat.backend.security import (
    AccessLogMiddleware,
    BotUploadBodyLimitMiddleware,
    CookieOriginCSRFMiddleware,
    CredentialedAPINoStoreMiddleware,
    RateLimitMiddleware,
    SecurityHeadersMiddleware,
    trusted_proxy_networks,
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
    store.seed_setting_if_absent(SETTING_SITE_NAME, "botarena")
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
    # Parse once before any Store/runtime writer is created. A malformed proxy
    # trust boundary must fail startup rather than silently trust a wider peer.
    trusted_proxy_cidrs = trusted_proxy_networks()
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
    user_assets_raw = os.environ.get("BZ_USER_ASSETS_DIR")
    user_assets_dir = (
        Path(user_assets_raw)
        if user_assets_raw
        else Path(db_path).expanduser().resolve().parent / "user_assets"
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
        bug_attachments_dir = assert_qa_runtime_path_isolated(
            bug_attachments_dir,
            source_root,
            purpose="BZ_QA_INSTANCE Bug 附件目录",
        )
        user_assets_dir = assert_qa_runtime_path_isolated(
            user_assets_dir,
            source_root,
            purpose="BZ_QA_INSTANCE 用户云存储目录",
        )
    prefer_local = os.environ.get("BZ_BOT_LOCAL", "").lower() in ("1", "true", "yes")
    if not prefer_local:
        # Reject remote/custom production configuration before Store can create
        # or migrate the selected DB. Commands independently pin the same socket.
        validate_local_docker_configuration()
    store = Store(db_path)
    local_ai_service = LocalAIService(store)
    human_play_handshake_gate = WebSocketHandshakeGate(
        max_attempts=HUMAN_WS_HANDSHAKE_MAX_ATTEMPTS,
        window_seconds=HUMAN_WS_HANDSHAKE_WINDOW_SECONDS,
        max_inflight=HUMAN_WS_HANDSHAKE_MAX_INFLIGHT,
        max_buckets=HUMAN_WS_HANDSHAKE_MAX_BUCKETS,
    )
    store.executions.set_local_agent_available(local_ai_service.is_available_now)
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
    from bzplat.backend.user_storage import UserStorageManager

    user_storage = UserStorageManager(store, root=user_assets_dir)
    # 启动时回收无清单引用且空闲超宽限期的实体与崩溃暂存残留。
    user_storage.sweep_unreferenced()
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
    # Admission starts before multipart parsing and stays held through
    # staging/preflight/commit (or rollback). It belongs to the app's one ASGI
    # loop, so an asyncio semaphore avoids consuming the default thread pool
    # while uploads wait. Keep it separate from the cross-thread preflight gate.
    bot_upload_gate = asyncio.Semaphore(BOT_UPLOAD_ADMISSION_SLOTS)
    # Deployment drain and upload admission share this loop-local mutex.  An
    # upload becomes active before multipart parsing and remains counted until
    # staging, preflight and DB commit/rollback have all converged.
    bot_upload_activity_lock = asyncio.Lock()
    bot_upload_activity = {"active": 0}

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

    def _source_builder(src_dir, out_dir, recipe):
        """上传 admission 内的同步源码构建（与预检共用 supervisor 纪律）。"""
        import uuid as _uuid

        from bzplat.backend.runtime.docker_supervisor import (
            DockerBuildTimeout,
            DockerSupervisorError,
        )
        from bzplat.backend.bots.source_build import (
            SourceBuildError,
            build_command,
        )

        if shared_supervisor is None:
            raise SourceBuildError(
                "build_unavailable", "当前实例未配置 Docker 构建通道"
            )
        # 构建容器（2C/2G）不占 match slot、不进 claim 准入；为守住
        # “内存维度严格不超卖”，启动前按与 claim 同口径的占用视图做
        # 准入检查。这是 advisory 闸：与并发 claim 之间仍有小窗口，
        # 结果是保守拒绝（503 重试）而不是超卖。
        from bzplat.backend.runtime.limits import (
            effective_host_resource_budget,
        )

        budget_mb = effective_host_resource_budget().memory_mb
        used_mb = store.executions.execution_memory_in_use_mb()
        if used_mb + BOT_BUILD_PROFILE.memory_mb > budget_mb:
            raise SourceBuildError(
                "build_busy",
                "平台内存资源紧张，源码编译通道繁忙，请稍后重试",
            )
        command = build_command(recipe)
        token = _uuid.uuid4().hex
        try:
            _ensure_linux_image_ready_sync(
                shared_supervisor.docker_bin,
                recipe["image"],
                prepare_timeout=DEFAULT_IMAGE_PREPARE_TIMEOUT,
            )
            exit_code = shared_supervisor.run_build(
                identity=DockerExecutionIdentity(
                    instance=shared_supervisor.instance,
                    job_public_id=f"build-{token[:12]}",
                    attempt_no=1,
                ),
                slot=150,
                launch_token=token,
                src_dir=src_dir,
                out_dir=out_dir,
                command=command,
                image=recipe["image"],
                profile=BOT_BUILD_PROFILE,
                timeout_sec=float(recipe["timeout_sec"]),
                # create 结果不确定时让唯一 dispatcher 进入与 journal
                # 状态匹配的 pause（creating → manual，否则 bounded retry），
                # 不允许构建失败静默遗留 creating 卡死对局启动。
                docker_uncertain_callback=_pause_for_unscoped_docker,
            )
        except DockerBuildTimeout as exc:
            raise SourceBuildError(
                "build_timeout", str(exc)
            ) from exc
        except SourceBuildError:
            raise
        except (DockerSupervisorError, RuntimeError) as exc:
            # PlatformRunnerError / DockerLaunchInvariantError 均 RuntimeError 系：
            # 统一转用户可读错误，不让源码上传产生 500。固定文案——
            # exc 的 str 可能携带 docker stderr 尾部或内部路径。
            logger.warning("source build sandbox error: %s", exc)
            raise SourceBuildError(
                "build_unavailable", "构建沙箱暂不可用，请稍后重试"
            ) from exc
        if exit_code != 0:
            raise SourceBuildError(
                "build_failed",
                f"编译失败（exit {exit_code}）；请在本地用同样的静态链接命令验证",
            )

    match_runner = MatchRunner(
        binary_runner,
        action_timeout=ACTION_TIMEOUT_SEC,
        local_ai_hub=local_ai_service.hub,
    )
    match_mounts_dir = Path(db_path).expanduser().resolve().parent / "match_mounts"
    if match_mounts_dir.is_dir():
        # 对局快照属进程生命周期；启动时不存在合法存活的挂载，整体回收崩溃残留。
        import shutil as _shutil

        for _stale in match_mounts_dir.iterdir():
            _shutil.rmtree(_stale, ignore_errors=True)
    if qa_instance:
        match_mounts_dir = assert_qa_runtime_path_isolated(
            match_mounts_dir,
            source_root,
            purpose="BZ_QA_INSTANCE 云盘对局快照目录",
        )
    orch = MatchOrchestrator(
        store,
        runner=match_runner,
        max_concurrent=effective_conc,
        mount_root=match_mounts_dir,
        user_storage=user_storage,
    )
    execution_dispatcher: ExecutionDispatcher | None = None
    contest_manager = ContestManager(
        store,
        orch,
        execution_admission_required=lambda: bool(
            execution_dispatcher is not None
            and execution_dispatcher._started
        ),
    )
    # QA capability guard is independent from the persisted administrator switch:
    # a copied production DB may say enabled, but an isolated QA process must never
    # write background ladder matches.
    host_budget = effective_host_resource_budget()
    # QA 隔离实例不执行进程启动前已入队的赛事任务（复制库的 running 赛事
    # 无法经状态机提前收束，只能在 claim 处按入队时间切断；与 auto producer
    # 的 capability guard 同一先例）。生产实例恒为 None。
    qa_inherited_contest_cutoff = (
        datetime.now().isoformat(timespec="seconds") if qa_instance else None
    )
    execution_dispatcher = ExecutionDispatcher(
        orch,
        store,
        max_match_slots=effective_conc,
        max_sandbox_units=effective_conc * 2,
        auto_capability_enabled=not qa_instance,
        qa_inherited_contest_cutoff=qa_inherited_contest_cutoff,
        contest_reconciler=contest_manager.reconcile_running_contests,
        singleton_acquired=store.reset_local_ai_runtime_state,
        uploads_in_flight=lambda: int(bot_upload_activity["active"]),
        max_host_cpu_millis=host_budget.cpu_millis,
        max_host_memory_mb=host_budget.memory_mb,
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
        # Store remains openable by the offline migration/cutover CLI, but the
        # online process must never run a current GameSpec against a legacy
        # persisted contract.  Check before the dispatcher flock, Docker
        # cleanup, schedulers, uploads or delivery workers start.
        store.assert_runtime_contracts_current()
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

        # 用户云盘 blob 延迟回收：启动清一次不够——运行期“配额拒绝保留
        # + DELETE 只删清单行”会持续产生无引用实体，认证用户循环上传+删除
        # 即可线性吃满磁盘。回收安全由 promote 刷 mtime 与 unlink 前引用
        # 复核两道防线保证，扫描本体在 worker 线程执行不阻塞事件循环。
        async def _user_storage_sweep_loop() -> None:
            while True:
                try:
                    await asyncio.sleep(USER_STORAGE_SWEEP_INTERVAL_SEC)
                    result = await asyncio.to_thread(
                        user_storage.sweep_unreferenced
                    )
                    if result["blobs"] or result["staging"]:
                        logger.info(
                            "user storage sweep removed blobs=%s staging=%s"
                            " bytes=%s",
                            result["blobs"],
                            result["staging"],
                            result["bytes"],
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("user storage sweep failed")

        storage_sweep_task = asyncio.create_task(
            _user_storage_sweep_loop(), name="user-storage-sweep"
        )
        _app.state._user_storage_sweep_task = storage_sweep_task
        try:
            yield
        finally:
            await execution_dispatcher.stop()
            task.cancel()
            sched_task.cancel()
            delivery_task.cancel()
            storage_sweep_task.cancel()
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
            try:
                await storage_sweep_task
            except asyncio.CancelledError:
                pass
            # Match tasks can be inside asyncio subprocess pipe setup.  Drain
            # them explicitly before the server closes the event loop; relying
            # on loop-wide cancellation can otherwise hang shutdown forever.
            try:
                await orch.shutdown()
            finally:
                # Drain local transports and reset their durable volatile
                # state while this process still owns the dispatcher
                # singleton.  The flock release is deliberately final, even
                # if draining a match task reported an error.
                try:
                    await local_ai_service.hub.shutdown()
                finally:
                    try:
                        store.reset_local_ai_runtime_state()
                    finally:
                        await execution_dispatcher.close()

    app = FastAPI(title="botzone-platform", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(RequestValidationError)
    async def _request_validation_error(request, exc: RequestValidationError):
        # Keep FastAPI's public 422 shape, including loc/type/msg, while making
        # the response safe for every value that JSON parsing/Pydantic may echo
        # in ``input``. In particular, a raw ``\ud800`` escape becomes a lone
        # surrogate in Python; Starlette's default ensure_ascii=False renderer
        # then raises UnicodeEncodeError while trying to report the validation
        # failure. ASCII JSON escaping is lossless and always UTF-8 encodable.
        errors = exc.errors()
        path = request.url.path
        if path == "/api/auth" or path.startswith("/api/auth/"):
            # Authentication bodies contain passwords, verification codes and
            # personal identifiers. Pydantic's diagnostic-only ``input``,
            # ``ctx`` and ``url`` fields must never reflect those values back to
            # clients (or any downstream response capture). Keep only the stable
            # public fields already consumed by form validation UIs.
            errors = [
                {
                    key: error[key]
                    for key in ("loc", "msg", "type")
                    if key in error
                }
                for error in errors
            ]
        content = {"detail": jsonable_encoder(errors)}
        payload = json.dumps(
            content,
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("ascii")
        return Response(
            content=payload,
            status_code=422,
            media_type="application/json",
        )

    app.state.store = store
    app.state.auth = auth
    app.state.auth_manager = auth
    app.state.captcha = captcha
    app.state.captcha_store = captcha
    app.state.bot_manager = bot_manager
    app.state.source_builder = _source_builder
    app.state.user_storage = user_storage
    app.state.binary_runner = binary_runner
    app.state.local_ai_service = local_ai_service
    app.state.human_play_handshake_gate = human_play_handshake_gate
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
    app.state.bot_upload_gate = bot_upload_gate
    app.state.bot_upload_activity_lock = bot_upload_activity_lock
    app.state.bot_upload_activity = bot_upload_activity
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
    app.state.trusted_proxy_cidrs = trusted_proxy_cidrs

    # Added first = innermost user middleware: still before FastAPI body parsing,
    # while the existing security/access layers can decorate and log its 413.
    app.add_middleware(BotUploadBodyLimitMiddleware)
    app.add_middleware(
        CookieOriginCSRFMiddleware,
        cookie_name=COOKIE_NAME,
        public_origin=os.environ.get("BZ_PUBLIC_ORIGIN", ""),
    )
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(
        RateLimitMiddleware,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
    )
    # Wrap rate-limit/CSRF/FastAPI responses alike so credential-varying API
    # success and error bodies can never enter browser or shared caches.
    app.add_middleware(
        CredentialedAPINoStoreMiddleware,
        cookie_name=COOKIE_NAME,
    )
    # AccessLog 最后 add = 最外层，记录所有请求（含被限流的 429）
    app.add_middleware(
        AccessLogMiddleware,
        trusted_proxy_cidrs=trusted_proxy_cidrs,
    )
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
        dist_root = dist.resolve()
        assets = dist / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/")
        def index():
            return FileResponse(dist / "index.html")

        @app.get("/{full_path:path}")
        def spa(full_path: str):
            # API 已由前面路由处理。只允许解析后仍位于 dist 内的普通
            # 文件；encoded ``..``、绝对路径和指向目录外的 symlink 均
            # 稳定 404，不能借 SPA catch-all 读取服务器文件。
            if "\\" in full_path or ".." in full_path.split("/"):
                raise HTTPException(404, "Not Found")
            try:
                candidate = (dist_root / full_path).resolve()
                candidate.relative_to(dist_root)
            except (OSError, RuntimeError, ValueError):
                raise HTTPException(404, "Not Found") from None
            if candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(dist_root / "index.html")

    return app
