"""对局执行：BinaryRunner ×2 + 按 game_id 路由引擎。"""
from __future__ import annotations

import asyncio
import inspect
import secrets
import time as _time
from dataclasses import dataclass, field
from typing import Any, Callable

from bzplat.backend.games import (
    fail_response as _reg_fail,
    normalize_game_id,
    registry as _game_registry,
    run_session,
)
# 全面解耦：runner 不再按 game_id 切协议模块，统一委托 games 注册表。
# 注：不 import 具体游戏模块（审计 P1：通用层不得依赖 games/holdem）。
# ``match_params`` 只承载游戏 spec 明确允许的平台内部复现参数；固定规则键会在
# session_factory 入口显式拒绝，runner 不持有任何游戏专属默认值。
from bzplat.backend.games import _botzone_protocol as _bz
from bzplat.backend.matches.bot_debug import MAX_RESPONSE_LINE_BYTES
from bzplat.backend.runtime.binary_runner import (
    BinaryRunner,
    BotCrashedError,
    BotDecisionTimeoutError,
    BotProtocolError,
    BotResponseLineTooLargeError,
    BotTechnicalError,
    PlatformRunnerError,
    ExecutionScope,
    DEFAULT_ACTION_TIMEOUT,
)
from bzplat.backend.runtime.limits import (
    DockerResourceProfile,
    LATEST_EXECUTION_RESOURCE_PROFILE_VERSION,
    PLATFORM_LOW_PROFILE,
    execution_resource_snapshot,
    resolve_execution_resource_profile,
)
from bzplat.backend.runtime.local_ai import LocalAIHub
from bzplat.backend.store.schema import (
    EXECUTION_ENV_PLATFORM_LOW,
    EXECUTION_ENV_REMOTE_LOCAL,
    TECHNICAL_INCIDENT_EVENT,
    TECHNICAL_INCIDENT_MESSAGES,
)

EventSink = Callable[[str, dict[str, Any]], None]
DebugSink = Callable[[int, int, int | None, Any], None]


@dataclass(slots=True)
class _LocalAISession:
    agent_id: str
    requests: list[dict[str, Any]] = field(default_factory=list)
    responses: list[Any] = field(default_factory=list)
    turn: int = 0


def _profile_for_environment(
    environment: str,
    profile_version: int,
) -> DockerResourceProfile:
    return resolve_execution_resource_profile(environment, profile_version)


def _fail_response(game_id: str) -> dict[str, Any]:
    """人类输入超时等游戏内兜底（Bot 技术故障禁止再走此路径）。"""
    return _reg_fail(game_id)


def _protocol_payload(
    game_id: str,
    line: str,
    *,
    failed_seat: int,
    turn: int,
    leg: int | None,
) -> tuple[Any, Any | None]:
    """Strictly decode one Botzone response without persisting raw Bot output."""
    if len(line.encode("utf-8")) > MAX_RESPONSE_LINE_BYTES:
        raise BotProtocolError(
            TECHNICAL_INCIDENT_MESSAGES["response_line_too_large"],
            error_code="response_line_too_large",
            failed_seat=failed_seat,
            turn=turn,
            leg=leg,
        )
    try:
        return _bz.decode_response_with_debug(
            line,
            _game_registry.get(game_id).protocol.validate_response_payload,
        )
    except _bz.ResponseProtocolError as exc:
        raise BotProtocolError(
            str(exc),
            error_code=exc.code,
            failed_seat=failed_seat,
            turn=turn,
            leg=leg,
        ) from exc


def _capture_debug(
    sink: DebugSink | None,
    *,
    seat: int,
    turn: int,
    leg: int | None,
    debug: Any,
) -> None:
    """调试采集是尽力 sidecar；异常绝不能改变已校验的 Bot 动作。"""
    if sink is None or debug is None:
        return
    try:
        sink(seat, turn, leg, debug)
    except Exception:
        # 不记录 traceback/异常文本：即使未来的 sink 把 Bot 内容
        # 放进异常消息，日志也只保留结构化上下文。
        import logging

        logging.getLogger(__name__).warning(
            "bot debug collector failed seat=%s turn=%s leg=%s",
            seat,
            turn,
            leg,
        )


async def _open_match_session(
    runner: BinaryRunner,
    binary_path: str,
    runtime_mode: str,
    *,
    failed_seat: int,
    profile: DockerResourceProfile = PLATFORM_LOW_PROFILE,
    execution_scope: ExecutionScope | None = None,
) -> str:
    """建立一方逻辑会话；Traditional 只登记历史，不预启动闲置进程。"""
    try:
        scope_kwargs = (
            {"execution_scope": execution_scope}
            if execution_scope is not None
            else {}
        )
        if runtime_mode == _bz.RUNTIME_TRADITIONAL:
            return await runner.prepare_session(
                binary_path,
                runtime_mode=runtime_mode,
                profile=profile,
                **scope_kwargs,
            )
        if runtime_mode == _bz.RUNTIME_LONGRUNNING:
            return await runner.start_session(
                binary_path,
                runtime_mode=runtime_mode,
                profile=profile,
                **scope_kwargs,
            )
        raise ValueError(f"未知运行模式: {runtime_mode}")
    except BotCrashedError as exc:
        exc.crashed_seat = failed_seat
        raise


async def _ensure_traditional_runtime_ready(
    runner: BinaryRunner,
    session_id: str,
) -> None:
    """Refresh sandbox readiness before a cumulative game clock starts.

    ``prepare_session`` performs the initial image gate before the game Session
    exists.  This second, cached check covers an operator removing/invalidating
    the image during a long Traditional match; a necessary re-pull still stays
    outside the acting seat's Pencil clock.
    """
    session = runner._sessions.get(session_id)
    if getattr(session, "runtime_mode", None) != _bz.RUNTIME_TRADITIONAL:
        return
    ensure_ready = getattr(runner, "ensure_runtime_ready", None)
    if ensure_ready is not None:
        await ensure_ready()


def _emit_technical_incident(
    on_event: EventSink | None, exc: BotTechnicalError
) -> None:
    if on_event is None:
        return
    on_event(
        TECHNICAL_INCIDENT_EVENT,
        {"type": TECHNICAL_INCIDENT_EVENT, **exc.incident()},
    )


async def _traditional_decide_one_shot(
    runner: BinaryRunner,
    session: Any,
    request: dict[str, Any],
    action_timeout: float,
    *,
    game_id: str,
    failed_seat: int,
    turn: int,
    leg: int | None,
    on_debug: DebugSink | None,
) -> dict[str, Any]:
    """Traditional 模式单次决策：启动 Bot → 发完整历史信封 → 读响应 → 停 Bot。

    Botzone 官方 traditional 语义：Bot 是无状态一次性程序，每个决策点平台重启 Bot
    喂完整 ``{"requests":[...], "responses":[...]}`` 信封。Bot 处理后输出
    ``{"response": ...}`` 并退出（不常驻）。

    本函数每回合用 ``session.binary_path`` 启动临时 session，发完整信封（含当前
    request + 历史 responses），读响应后 stop 临时 session。主 session 的
    requests/responses/turn 仍维护（供信封累积）。
    """
    # 暂存当前 request 到会话历史（信封含它）
    full_requests = session.requests + [request]
    line = _bz.dumps_traditional(full_requests, session.responses)
    # 启动临时 bot 进程（每回合重启——traditional 语义）
    try:
        scope = getattr(session, "execution_scope", None)
        scope_kwargs = {"execution_scope": scope} if scope is not None else {}
        tmp_sid = await runner.start_session(
            session.binary_path,
            runtime_mode=_bz.RUNTIME_TRADITIONAL,
            profile=session.profile,
            **scope_kwargs,
        )
    except BotCrashedError as exc:
        exc.crashed_seat = failed_seat
        raise
    try:
        try:
            resp_line = await runner.send(tmp_sid, line, timeout=action_timeout)
        except BotCrashedError as exc:
            exc.crashed_seat = failed_seat
            raise
    finally:
        await runner.stop_session(tmp_sid)
    payload, debug = _protocol_payload(
        game_id,
        resp_line,
        failed_seat=failed_seat,
        turn=turn,
        leg=leg,
    )
    # 提交会话状态（与 longrunning 路径一致）
    session.requests.append(request)
    session.responses.append(payload)
    session.turn += 1
    _capture_debug(
        on_debug,
        seat=failed_seat,
        turn=turn,
        leg=leg,
        debug=debug,
    )
    return {"response": payload}


async def _local_ai_decide(
    hub: LocalAIHub,
    session: _LocalAISession,
    request: dict[str, Any],
    *,
    match_id: str,
    game_id: str,
    action_timeout: float,
    failed_seat: int,
    leg: int | None = None,
    on_debug: DebugSink | None = None,
) -> dict[str, Any]:
    """Send one complete Traditional envelope to a user-hosted Bot.

    The connector is only a transport: protocol validation and the absolute
    decision deadline remain authoritative on the platform referee.
    """

    attempted_turn = session.turn + 1
    line = _bz.dumps_traditional(session.requests + [request], session.responses)
    output = await hub.request_decision(
        session.agent_id,
        request_id="turn_" + secrets.token_urlsafe(18),
        match_id=match_id,
        seat=failed_seat,
        turn=attempted_turn,
        deadline_at=_time.monotonic() + max(0.001, float(action_timeout)),
        input=line,
    )
    if not isinstance(output, str):
        raise BotProtocolError(
            "本地 Bot 响应必须是一行 JSON 文本",
            error_code="invalid_response_type",
            failed_seat=failed_seat,
            turn=attempted_turn,
            leg=leg,
        )
    payload, debug = _protocol_payload(
        game_id,
        output,
        failed_seat=failed_seat,
        turn=attempted_turn,
        leg=leg,
    )
    session.requests.append(request)
    session.responses.append(payload)
    session.turn += 1
    _capture_debug(
        on_debug,
        seat=failed_seat,
        turn=attempted_turn,
        leg=leg,
        debug=debug,
    )
    return {"response": payload}


async def _botzone_decide(
    runner: BinaryRunner,
    session_id: str,
    request: dict[str, Any],
    *,
    game_id: str,
    action_timeout: float,
    failed_seat: int = 0,
    leg: int | None = None,
    on_debug: DebugSink | None = None,
) -> dict[str, Any]:
    """Botzone 标准协议决策：按 session.runtime_mode 选传输路径，返回信封 dict。

    - **Traditional**：每回合发完整历史信封 ``{requests[], responses[]}``（Bot 自重放）。
    - **LongRunning**：首回合发完整历史信封，随后读 keep_running 握手；后续回合发单
      request 信封 ``{request}``（Bot 内存自维护状态）。

    返回 Bot 响应的**信封** ``{"response": <payload>}``（各引擎 parse 接受此结构）。
    会话的 requests/responses/turn/long_running 状态由本函数维护。

    非 Botzone 协议的游戏（未来）可不走本函数；当前所有游戏均 Botzone 协议。
    """
    session = runner._sessions[session_id]
    attempted_turn = session.turn + 1

    # Traditional 模式：Bot 是一次性程序（处理一个信封→输出→退出），平台每回合重启。
    # 这是 Botzone 官方 traditional 语义——Bot 无状态，每次从完整历史信封重建。
    # LongRunning 模式 Bot 常驻（session 复用，首回合握手后发单 request）。
    if session.runtime_mode == _bz.RUNTIME_TRADITIONAL:
        try:
            return await _traditional_decide_one_shot(
                runner,
                session,
                request,
                action_timeout,
                game_id=game_id,
                failed_seat=failed_seat,
                turn=attempted_turn,
                leg=leg,
                on_debug=on_debug,
            )
        except asyncio.TimeoutError as exc:
            raise BotDecisionTimeoutError(
                TECHNICAL_INCIDENT_MESSAGES["decision_timeout"],
                error_code="decision_timeout",
                failed_seat=failed_seat,
                turn=attempted_turn,
                leg=leg,
            ) from exc
        except BotResponseLineTooLargeError as exc:
            raise BotProtocolError(
                TECHNICAL_INCIDENT_MESSAGES["response_line_too_large"],
                error_code="response_line_too_large",
                failed_seat=failed_seat,
                turn=attempted_turn,
                leg=leg,
            ) from exc

    is_first_turn = session.turn == 0
    # LongRunning 首回合必须完成握手；不存在“同进程完整历史”兼容模式。
    if is_first_turn:
        # 完整历史信封（含本轮 request——暂存到 pending_request，解析成功后才提交）。
        line = _bz.dumps_traditional(session.requests + [request], session.responses)
    else:
        if not session.long_running:
            raise BotProtocolError(
                TECHNICAL_INCIDENT_MESSAGES["missing_keep_running"],
                error_code="missing_keep_running",
                failed_seat=failed_seat,
                turn=attempted_turn,
                leg=leg,
            )
        # LongRunning 后续回合：发单 request 信封。
        line = _bz.dumps_longrunning_single(request)

    try:
        resp_line = await runner.send(session_id, line, timeout=action_timeout)
    except asyncio.TimeoutError as exc:
        raise BotDecisionTimeoutError(
            TECHNICAL_INCIDENT_MESSAGES["decision_timeout"],
            error_code="decision_timeout",
            failed_seat=failed_seat,
            turn=attempted_turn,
            leg=leg,
        ) from exc
    except BotResponseLineTooLargeError as exc:
        raise BotProtocolError(
            TECHNICAL_INCIDENT_MESSAGES["response_line_too_large"],
            error_code="response_line_too_large",
            failed_seat=failed_seat,
            turn=attempted_turn,
            leg=leg,
        ) from exc

    payload, debug = _protocol_payload(
        game_id,
        resp_line,
        failed_seat=failed_seat,
        turn=attempted_turn,
        leg=leg,
    )

    # LongRunning 首回合响应后必须精确输出 keep_running 握手。
    if session.runtime_mode == _bz.RUNTIME_LONGRUNNING and is_first_turn:
        try:
            extra = await runner.read_extra_line(session_id, timeout=1.0)
        except BotResponseLineTooLargeError as exc:
            raise BotProtocolError(
                TECHNICAL_INCIDENT_MESSAGES["response_line_too_large"],
                error_code="response_line_too_large",
                failed_seat=failed_seat,
                turn=attempted_turn,
                leg=leg,
            ) from exc
        try:
            _bz.require_keep_running_signal(extra)
        except _bz.ResponseProtocolError as exc:
            raise BotProtocolError(
                str(exc),
                error_code=exc.code,
                failed_seat=failed_seat,
                turn=attempted_turn,
                leg=leg,
            ) from exc
        session.long_running = True

    # 原子提交会话状态：requests/responses/turn 一起更新。若上面的 loads/extract 抛异常
    # （Bot 输出非法 JSON / 缺 response），不提交——避免 requests 比 responses 多一条导致
    # traditional Bot 后续信封错位（requests[i]↔responses[i] 配对重放错乱）。
    session.requests.append(request)
    session.responses.append(payload)
    session.turn += 1
    _capture_debug(
        on_debug,
        seat=failed_seat,
        turn=attempted_turn,
        leg=leg,
        debug=debug,
    )
    # 返回唯一现行 response 信封，满足引擎的 canonical decide 契约。
    return {"response": payload}


class _ChessClock:
    """象棋钟：累计每方决策耗时，判定是否超时。

    纯逻辑类（可独立单测），decide 闭包调它记录耗时 + 查剩余时间。
    budget=None 时不计时（走原单步超时逻辑）。
    """

    def __init__(self, budget: float | None):
        self.budget = budget
        self._used = [0.0, 0.0] if budget is not None else None

    @property
    def active(self) -> bool:
        """是否启用象棋钟（budget 非 None）。"""
        return self._used is not None

    def remaining(self, seat: int) -> float:
        """seat 方的剩余时间（秒）。未启用时返 +inf（不限时）。"""
        if self._used is None:
            return float("inf")
        return max(0.0, self.budget - self._used[seat])

    def used(self, seat: int) -> float:
        """seat 方已用时间（秒）。"""
        return self._used[seat] if self._used is not None else 0.0

    def is_exhausted(self, seat: int) -> bool:
        """seat 方是否时间耗尽。"""
        return self.remaining(seat) <= 0

    def record(self, seat: int, elapsed: float) -> None:
        """记录 seat 方本次决策耗时。"""
        if self._used is not None:
            self._used[seat] += elapsed

    def now(self) -> float:
        """单调时钟（测试可 monkeypatch）。"""
        return _time.monotonic()


class MatchRunner:
    def __init__(
        self,
        runner: BinaryRunner | None = None,
        *,
        action_timeout: float = DEFAULT_ACTION_TIMEOUT,
        local_ai_hub: LocalAIHub | None = None,
    ) -> None:
        self.runner = runner or BinaryRunner()
        self.action_timeout = action_timeout
        self.local_ai_hub = local_ai_hub

    async def _close_execution_sessions(
        self,
        session_ids: tuple[str, ...],
        execution_scope: ExecutionScope | None,
    ) -> None:
        """Stop every session and prove a scoped execution has no worker left."""
        first_error: BaseException | None = None
        for session_id in session_ids:
            try:
                await self.runner.stop_session(session_id)
            except BaseException as exc:  # cleanup must continue for the other seat
                if first_error is None:
                    first_error = exc
        if execution_scope is not None:
            # Cleanup is one job-level operation: both seats, every Traditional
            # one-shot container and any uncertain create are removed by the
            # same exact instance/job/attempt labels before capacity is released.
            await self.runner.cleanup_execution(execution_scope)
            first_error = None
        if first_error is not None:
            raise first_error

    async def run_binaries(
        self,
        path_a: str | None,
        path_b: str | None,
        *,
        game_id: str,
        on_event: EventSink | None = None,
        on_debug: DebugSink | None = None,
        seed: int | None = None,
        runtime_modes: tuple[str, str] | None = None,
        execution_environments: tuple[str, str] | None = None,
        execution_profile_version: int = (
            LATEST_EXECUTION_RESOURCE_PROFILE_VERSION
        ),
        local_agent_ids: tuple[str | None, str | None] | None = None,
        match_id: str | None = None,
        time_budget_per_side: float | None = None,
        execution_scope: ExecutionScope | None = None,
        **match_params: Any,
    ) -> MatchResult:
        """跑两个二进制 bot。

        ``game_id`` 必须显式传入；``match_params`` 只透传 spec 允许的平台内部参数。
        任何非内部规则参数都不会被忽略，而会由目标游戏的 session_factory 明确拒绝。

        ``runtime_modes``：(bot_a 的 Botzone 运行模式, bot_b 的)。None → 使用平台
        权威默认模式（Traditional）。
        """
        import random

        gid = normalize_game_id(game_id)
        rm_a, rm_b = runtime_modes or (
            _bz.DEFAULT_RUNTIME_MODE,
            _bz.DEFAULT_RUNTIME_MODE,
        )
        env_a, env_b = execution_environments or (
            EXECUTION_ENV_PLATFORM_LOW,
            EXECUTION_ENV_PLATFORM_LOW,
        )
        # Validate the durable version before opening either seat. This also
        # rejects an unknown version for local-vs-local jobs, whose resource
        # vector is zero but whose persisted execution contract is still invalid.
        execution_resource_snapshot(
            (env_a, env_b), execution_profile_version
        )
        agent_a, agent_b = local_agent_ids or (None, None)
        match_key = str(match_id or "")
        if EXECUTION_ENV_REMOTE_LOCAL in {env_a, env_b} and not match_key:
            raise ValueError("本地 Bot 对局缺少 match_id")

        async def open_seat(
            path: str | None,
            runtime_mode: str,
            environment: str,
            agent_id: str | None,
            seat: int,
        ) -> tuple[str | None, _LocalAISession | None]:
            if environment == EXECUTION_ENV_REMOTE_LOCAL:
                if self.local_ai_hub is None or not agent_id:
                    raise ValueError("本地 Bot 连接未配置")
                if path is not None:
                    raise ValueError("本地 Bot 座位不能启动平台二进制")
                return None, _LocalAISession(str(agent_id))
            if agent_id is not None:
                raise ValueError("Docker 座位不能绑定本地 Bot 连接")
            if not path:
                raise ValueError("Docker 座位缺少 Bot 二进制")
            return (
                await _open_match_session(
                    self.runner,
                    path,
                    runtime_mode,
                    failed_seat=seat,
                    profile=_profile_for_environment(
                        environment, execution_profile_version
                    ),
                    execution_scope=execution_scope,
                ),
                None,
            )

        sid_a, local_a = await open_seat(path_a, rm_a, env_a, agent_a, 0)
        try:
            sid_b, local_b = await open_seat(path_b, rm_b, env_b, agent_b, 1)
        except BaseException:
            await self._close_execution_sessions(
                tuple(item for item in (sid_a,) if item is not None),
                execution_scope,
            )
            raise
        try:
            rng = random.Random(seed) if seed is not None else random.Random()
            clock = _ChessClock(time_budget_per_side)

            async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                sid = sid_a if player_idx == 0 else sid_b
                local_session = local_a if player_idx == 0 else local_b
                # 象棋钟：剩余时间作为本次 timeout；耗尽判超时负
                if clock.active:
                    if clock.is_exhausted(player_idx):
                        if on_event is not None:
                            on_event("time_out", {
                                "type": "time_out", "seat": player_idx,
                                "used": round(clock.used(player_idx), 1),
                                "budget": clock.budget,
                            })
                        timeout_exc = BotDecisionTimeoutError(
                            "Bot 累计决策时间已耗尽",
                            error_code="decision_timeout",
                            failed_seat=player_idx,
                            turn=(
                                local_session.turn + 1
                                if local_session is not None
                                else getattr(self.runner._sessions.get(sid), "turn", 0) + 1
                            ),
                        )
                        _emit_technical_incident(on_event, timeout_exc)
                        raise timeout_exc
                    if sid is not None:
                        await _ensure_traditional_runtime_ready(self.runner, sid)
                    effective_timeout = clock.remaining(player_idx)
                else:
                    effective_timeout = self.action_timeout
                t0 = clock.now()
                try:
                    if local_session is not None:
                        assert self.local_ai_hub is not None
                        resp = await _local_ai_decide(
                            self.local_ai_hub,
                            local_session,
                            request,
                            match_id=match_key,
                            game_id=gid,
                            action_timeout=effective_timeout,
                            failed_seat=player_idx,
                            on_debug=on_debug,
                        )
                    else:
                        if sid is None:  # pragma: no cover - guarded by open_seat
                            raise RuntimeError("Docker Bot 会话不存在")
                        resp = await _botzone_decide(
                            self.runner, sid, request,
                            game_id=gid, action_timeout=effective_timeout,
                            failed_seat=player_idx,
                            on_debug=on_debug,
                        )
                except BotTechnicalError as exc:
                    # 首个协议错误/超时即结束对局；绝不伪造成游戏默认动作继续跑。
                    if isinstance(exc, BotDecisionTimeoutError) and clock.active:
                        elapsed = clock.now() - t0
                        if on_event is not None:
                            on_event("time_out", {
                                "type": "time_out", "seat": player_idx,
                                "used": round(clock.used(player_idx) + elapsed, 1),
                                "budget": clock.budget,
                            })
                    _emit_technical_incident(on_event, exc)
                    raise
                except (BotCrashedError, PlatformRunnerError):
                    # Bot 崩溃向上传播判技术负；平台沙箱故障也必须向上传播，
                    # 由 orchestrator 中止且不评分，绝不能吞成 Bot 默认动作。
                    raise
                finally:
                    if clock.active:
                        clock.record(player_idx, clock.now() - t0)
                # emit 时间更新（前端时钟显示）
                if clock.active and on_event is not None:
                    on_event("time_used", {
                        "type": "time_used", "seat": player_idx,
                        "used": round(clock.used(player_idx), 1),
                        "remaining": round(clock.remaining(player_idx), 1),
                        "budget": clock.budget,
                    })
                return resp

            return await run_session(
                gid, decide, on_event=on_event, rng=rng, **match_params,
            )
        finally:
            await self._close_execution_sessions(
                tuple(
                    item for item in (sid_a, sid_b) if item is not None
                ),
                execution_scope,
            )

    async def run_bot_vs_human(
        self,
        bot_path: str,
        *,
        bot_seat: int,
        human_decide,
        game_id: str,
        on_event: EventSink | None = None,
        seed: int | None = None,
        runtime_mode: str | None = None,
        execution_environment: str = EXECUTION_ENV_PLATFORM_LOW,
        execution_profile_version: int = (
            LATEST_EXECUTION_RESOURCE_PROFILE_VERSION
        ),
        time_budget_per_side: float | None = None,
        execution_scope: ExecutionScope | None = None,
        **match_params: Any,
    ) -> MatchResult:
        """Bot vs 人类：bot 侧走 BinaryRunner，人类侧走 human_decide 协程。

        bot_seat 为 bot 坐位（0/1）；人类坐另一侧。human_decide(player_idx, request)
        由调用方实现（通常经 asyncio.Future 等待 WS 回传），超时由其内部处理。
        ``time_budget_per_side`` 启用双方共享契约的累计棋钟；人类 Future 另以
        棋钟剩余时间作外层 deadline，不能靠逐手及时响应绕过累计预算。
        ``match_params`` 只承载 spec 允许的平台内部参数（同 run_binaries）。
        ``runtime_mode``：Bot 的 Botzone 运行模式（None → 平台默认 Traditional）。
        """
        import random

        gid = normalize_game_id(game_id)
        rm = runtime_mode or _bz.DEFAULT_RUNTIME_MODE
        profile = _profile_for_environment(
            execution_environment, execution_profile_version
        )
        sid_bot = await _open_match_session(
            self.runner,
            bot_path,
            rm,
            failed_seat=bot_seat,
            profile=profile,
            execution_scope=execution_scope,
        )
        try:
            rng = random.Random(seed) if seed is not None else random.Random()
            clock = _ChessClock(time_budget_per_side)

            async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                # 与 run_binaries 相同的累计棋钟契约，但同一只钟同时覆盖 Bot
                # subprocess 与人类 Future 两条决策路径。
                if clock.active:
                    if clock.is_exhausted(player_idx):
                        if on_event is not None:
                            on_event("time_out", {
                                "type": "time_out", "seat": player_idx,
                                "used": round(clock.used(player_idx), 1),
                                "budget": clock.budget,
                            })
                        if player_idx == bot_seat:
                            timeout_exc = BotDecisionTimeoutError(
                                "Bot 累计决策时间已耗尽",
                                error_code="decision_timeout",
                                failed_seat=bot_seat,
                                turn=getattr(
                                    self.runner._sessions.get(sid_bot), "turn", 0
                                ) + 1,
                            )
                            _emit_technical_incident(on_event, timeout_exc)
                            raise timeout_exc
                        raise TimeoutError(
                            f"human seat {player_idx} 时间耗尽（{clock.budget}s）"
                        )
                    if player_idx == bot_seat:
                        await _ensure_traditional_runtime_ready(
                            self.runner, sid_bot
                        )
                    effective_timeout = clock.remaining(player_idx)
                else:
                    effective_timeout = self.action_timeout
                t0 = clock.now()
                try:
                    if player_idx == bot_seat:
                        resp = await _botzone_decide(
                            self.runner, sid_bot, request,
                            game_id=gid, action_timeout=effective_timeout,
                            failed_seat=bot_seat,
                        )
                    else:
                        # 人类侧：生产实现返回等待 WebSocket Future 的 coroutine。
                        out = human_decide(player_idx, request)
                        if inspect.isawaitable(out):
                            if clock.active:
                                out = await asyncio.wait_for(
                                    out, timeout=effective_timeout
                                )
                            else:
                                out = await out
                        # WebSocket 人类动作不是 Bot stdin/stdout 协议：棋类前端发送
                        # 裸坐标，holdem 前端发送 canonical envelope。进入引擎前统一
                        # 成同一个 response 信封，不放宽 Bot 传输层。
                        if isinstance(out, dict) and set(out) == {"response"}:
                            resp = out
                        elif isinstance(out, dict):
                            resp = {"response": out}
                        else:
                            resp = {"response": _fail_response(gid)}
                except BotTechnicalError as exc:
                    if isinstance(exc, BotDecisionTimeoutError) and clock.active:
                        elapsed = clock.now() - t0
                        if on_event is not None:
                            on_event("time_out", {
                                "type": "time_out", "seat": player_idx,
                                "used": round(clock.used(player_idx) + elapsed, 1),
                                "budget": clock.budget,
                            })
                    _emit_technical_incident(on_event, exc)
                    raise
                except (BotCrashedError, PlatformRunnerError):
                    # Bot 崩溃或平台沙箱故障都不可吞成默认动作。
                    raise
                except asyncio.TimeoutError as exc:
                    if clock.active:
                        elapsed = clock.now() - t0
                        if on_event is not None:
                            on_event("time_out", {
                                "type": "time_out", "seat": player_idx,
                                "used": round(clock.used(player_idx) + elapsed, 1),
                                "budget": clock.budget,
                            })
                        raise TimeoutError(
                            f"seat {player_idx} 时间耗尽（{clock.budget}s）"
                        ) from exc
                    # 此分支只可能来自人类 Future；Bot subprocess 的超时已由
                    # _botzone_decide 转成 BotDecisionTimeoutError。
                    raise
                finally:
                    if clock.active:
                        clock.record(player_idx, clock.now() - t0)
                # emit 时间更新（前端时钟显示），双方字段与 Bot-vs-Bot 一致。
                if clock.active and on_event is not None:
                    on_event("time_used", {
                        "type": "time_used", "seat": player_idx,
                        "used": round(clock.used(player_idx), 1),
                        "remaining": round(clock.remaining(player_idx), 1),
                        "budget": clock.budget,
                    })
                return resp

            return await run_session(
                gid, decide, on_event=on_event, rng=rng, **match_params,
            )
        finally:
            await self._close_execution_sessions((sid_bot,), execution_scope)

    async def run_callables(
        self,
        decide_a,
        decide_b,
        *,
        game_id: str,
        on_event: EventSink | None = None,
        seed: int | None = None,
        **match_params: Any,
    ) -> MatchResult:
        """跑两个 callable bot（测试用）；固定规则参数仍会被 spec 明确拒绝。"""
        import random

        gid = normalize_game_id(game_id)
        rng = random.Random(seed) if seed is not None else random.Random()

        async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
            fn = decide_a if player_idx == 0 else decide_b
            out = fn(request)
            if hasattr(out, "__await__"):
                out = await out
            # callable 是可信测试入口；统一包成引擎所消费的 canonical envelope。
            if isinstance(out, dict) and set(out) == {"response"}:
                return out
            return {"response": out}

        return await run_session(
            gid, decide, on_event=on_event, rng=rng, **match_params,
        )

    async def run_duplicate(
        self,
        path_a: str,
        path_b: str,
        *,
        game_id: str,
        seed: int | None = None,
        on_event: EventSink | None = None,
        on_debug: DebugSink | None = None,
        runtime_modes: tuple[str, str] | None = None,
        execution_environments: tuple[str, str] | None = None,
        execution_profile_version: int = (
            LATEST_EXECUTION_RESOURCE_PROFILE_VERSION
        ),
        time_budget_per_side: float | None = None,
        execution_scope: ExecutionScope | None = None,
        **match_params: Any,
    ) -> Any:
        """P4 duplicate：跑多 leg（经 spec.build_match_plan），**每 leg 独立判胜负**。

        每 leg 用同 deal_sequence（消除运气）；seat_swap=True 的 leg 对调 decide 回调
        （B 在 seat0）。每 leg 独立产出 winner + deltas（物理 bot A/B 视角，swap leg 翻转），
        收集进 ``legs`` 字段——编排层（standings/ranking）按"打了两场"逐 leg 累加积分
        （如 2 场 poker_3_1_0），**不再把两场净筹码合并判 1 场胜负**。

        decide 闭包复用 `_botzone_decide`（与 run_binaries 一致），支持 traditional/
        longrunning 协议与 runtime_modes——真 Botzone bot 在两 leg 中均可正确收发。
        游戏未声明 duplicate 计划时明确拒绝，不把请求悄悄改成单 leg。

        返回带 ``legs`` 字段的结果（首 leg 的 MatchResult 结构 + legs 列表）。
        final_chips/net 保留两 leg 物理累加（仅作分差破同分，不作为胜负判据）。
        """
        from bzplat.backend.games import registry as _reg

        spec = _reg.get(game_id)
        if spec.build_match_plan is None:
            raise ValueError(f"游戏 {spec.game_id} 不支持 duplicate 对局")
        legs_plan = spec.build_match_plan(seed or 0, match_params)
        # 每 leg 独立胜负（物理 bot A/B 视角）；累加 deltas 仅留作分差破同分
        leg_results: list[dict[str, Any]] = []
        merged_deltas = [0, 0]  # 物理 A/B，仅破同分用
        merged_rounds: list[Any] = []
        merged_rounds_played = 0
        merged_events: list[dict[str, Any]] = []
        final_result = None
        rm_a, rm_b = runtime_modes or (
            _bz.DEFAULT_RUNTIME_MODE,
            _bz.DEFAULT_RUNTIME_MODE,
        )
        env_a, env_b = execution_environments or (
            EXECUTION_ENV_PLATFORM_LOW,
            EXECUTION_ENV_PLATFORM_LOW,
        )
        execution_resource_snapshot(
            (env_a, env_b), execution_profile_version
        )
        if EXECUTION_ENV_REMOTE_LOCAL in {env_a, env_b}:
            raise ValueError("本地 Bot 不支持复式正式赛制")
        sid_a = await _open_match_session(
            self.runner,
            path_a,
            rm_a,
            failed_seat=0,
            profile=_profile_for_environment(env_a, execution_profile_version),
            execution_scope=execution_scope,
        )
        try:
            sid_b = await _open_match_session(
                self.runner,
                path_b,
                rm_b,
                failed_seat=1,
                profile=_profile_for_environment(
                    env_b, execution_profile_version
                ),
                execution_scope=execution_scope,
            )
        except BaseException:
            await self._close_execution_sessions((sid_a,), execution_scope)
            raise
        gid = normalize_game_id(game_id)
        try:
            for li, leg in enumerate(legs_plan):
                lp = dict(leg.get("params") or {})
                lp.pop("match_seed", None)
                swap = bool(leg.get("seat_swap"))

                async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                    # seat_swap：seat0 → 物理 B（对调座位），seat1 → 物理 A
                    if swap:
                        sid = sid_b if player_idx == 0 else sid_a
                    else:
                        sid = sid_a if player_idx == 0 else sid_b
                    try:
                        physical_seat = 1 - player_idx if swap else player_idx
                        return await _botzone_decide(
                            self.runner, sid, request,
                            game_id=gid, action_timeout=self.action_timeout,
                            failed_seat=physical_seat,
                            leg=li,
                            on_debug=on_debug,
                        )
                    except BotTechnicalError as exc:
                        _emit_technical_incident(leg_on_event, exc)
                        raise
                    except (BotCrashedError, PlatformRunnerError):
                        # Bot 崩溃或平台沙箱故障都向上传播（与 run_binaries 一致）。
                        raise

                def leg_on_event(kind: str, ev: dict[str, Any]) -> None:
                    ev2 = {**ev, "leg": li}
                    if on_event:
                        on_event(kind, ev2)

                res = await run_session(
                    gid, decide, on_event=leg_on_event, **lp,
                )
                if final_result is None:
                    final_result = res
                merged_rounds_played += int(getattr(res, "rounds_played", 0))
                # 该 leg 的 deltas 翻转到**物理 bot A/B 视角**
                leg_deltas = [0, 0]
                for r in getattr(res, "rounds", []):
                    d = r.deltas
                    if swap:
                        leg_deltas[1] += d[0]  # seat0=B
                        leg_deltas[0] += d[1]  # seat1=A
                    else:
                        leg_deltas[0] += d[0]  # seat0=A
                        leg_deltas[1] += d[1]  # seat1=B
                    merged_rounds.append(r)
                merged_deltas[0] += leg_deltas[0]
                merged_deltas[1] += leg_deltas[1]
                merged_events.extend(getattr(res, "events", []))
                # 该 leg 独立 winner（按物理 A/B 的 leg_deltas 比较）
                if leg_deltas[0] > leg_deltas[1]:
                    leg_winner = 0
                elif leg_deltas[1] > leg_deltas[0]:
                    leg_winner = 1
                else:
                    leg_winner = None  # 平局
                leg_results.append({"winner": leg_winner, "deltas": list(leg_deltas)})
        finally:
            await self._close_execution_sessions(
                (sid_a, sid_b), execution_scope
            )
        # 构造结果：首 leg 结构 + legs 字段（每 leg 独立胜负）+ 累加 deltas（tiebreak 用）
        if final_result is not None:
            try:
                final_result.rounds = merged_rounds
                final_result.rounds_played = merged_rounds_played
                final_result.events = merged_events
                # net/final_chips 留作分差破同分（两 leg 物理累加），不作胜负判据
                if hasattr(final_result, "net"):
                    final_result.net = list(merged_deltas)
                if hasattr(final_result, "final_chips"):
                    final_result.final_chips = list(merged_deltas)
                # legs 字段：编排层（standings/ranking）按每 leg 独立判胜负累加积分
                final_result.legs = leg_results
            except Exception:
                pass
        return final_result
