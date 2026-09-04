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
from bzplat.backend.games.base import TimeControlSpec
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
from bzplat.backend.runtime.local_ai import LocalAIHub, LocalAITechnicalError
from bzplat.backend.store.schema import (
    EXECUTION_ENV_PLATFORM_LOW,
    EXECUTION_ENV_REMOTE_LOCAL,
    TECHNICAL_INCIDENT_EVENT,
    TECHNICAL_INCIDENT_MESSAGES,
)

EventSink = Callable[[str, dict[str, Any]], None]
DebugSink = Callable[[int, int, int | None, Any], None]
ElapsedSink = Callable[[float], None]


def _decision_remaining(
    runner: BinaryRunner,
    session_id: str,
    *,
    timeout: float,
    fallback_started: float,
) -> float:
    """Read BinaryRunner's transport clock; retain fake-runner compatibility."""

    getter = getattr(runner, "decision_remaining", None)
    if callable(getter):
        remaining = getter(session_id)
        if remaining is not None:
            return max(0.0, float(remaining))
    return max(0.0, float(timeout) - (_time.monotonic() - fallback_started))


def _consume_decision_elapsed(
    runner: BinaryRunner,
    session_id: str,
    *,
    fallback_started: float,
) -> float:
    """Consume authoritative Bot transport time when the runner provides it."""

    consume = getattr(runner, "consume_decision_elapsed", None)
    if callable(consume):
        elapsed = consume(session_id)
        # A production BinaryRunner with no timer failed before the request was
        # handed to the Bot, so none of that platform interval is chargeable.
        return 0.0 if elapsed is None else max(0.0, float(elapsed))
    return max(0.0, _time.monotonic() - fallback_started)


def _resolve_runner_time_control(
    game_id: str,
    *,
    time_control_id: str | None,
    time_budget_per_side: float | None,
) -> TimeControlSpec:
    """Resolve the new id contract, with a narrow scalar legacy adapter.

    The old scalar is accepted only when it exactly names a registered
    cumulative option.  Arbitrary seconds and mixing both representations are
    rejected, so no caller can create an unregistered referee rule.
    """

    spec = _game_registry.get(game_id)
    if time_control_id is not None and time_budget_per_side is not None:
        raise ValueError("time_control_id 与旧 time_budget_per_side 不能同时传入")
    if time_budget_per_side is None:
        return spec.resolve_time_control(time_control_id)
    if (
        isinstance(time_budget_per_side, bool)
        or not isinstance(time_budget_per_side, (int, float))
        or not _time_control_seconds_are_finite(time_budget_per_side)
    ):
        raise ValueError("time_budget_per_side 必须对应已注册的累计时限")
    candidates = [
        item
        for item in spec.time_controls
        if item.mode == "per_side_total"
        and float(item.seconds) == float(time_budget_per_side)
    ]
    if len(candidates) != 1:
        raise ValueError("time_budget_per_side 必须对应已注册的累计时限")
    return candidates[0]


def _time_control_seconds_are_finite(value: float) -> bool:
    import math

    return math.isfinite(float(value)) and float(value) > 0


def _event_sink_with_time_control(
    on_event: EventSink | None,
    control: TimeControlSpec,
    *,
    applies_to: str,
    suppress_match_start: bool = False,
) -> EventSink:
    """Inject the canonical public time-control object into match_start."""

    payload = control.public_payload(applies_to=applies_to)

    def emit(kind: str, event: dict[str, Any]) -> None:
        if kind == "match_start" or event.get("type") == "match_start":
            event.pop("time_budget_per_side", None)
            event["time_control"] = dict(payload)
            if suppress_match_start:
                return
        if on_event is not None:
            on_event(kind, event)

    return emit


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
    on_decision_elapsed: ElapsedSink | None = None,
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
        started = _time.monotonic()
        try:
            resp_line = await runner.send(tmp_sid, line, timeout=action_timeout)
        except BotCrashedError as exc:
            exc.crashed_seat = failed_seat
            raise
        finally:
            elapsed = _consume_decision_elapsed(
                runner,
                tmp_sid,
                fallback_started=started,
            )
            if on_decision_elapsed is not None:
                on_decision_elapsed(elapsed)
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
    on_decision_elapsed: ElapsedSink | None = None,
) -> dict[str, Any]:
    """Send one complete Traditional envelope to a user-hosted Bot.

    The connector is only a transport: protocol validation and the absolute
    decision deadline remain authoritative on the platform referee.
    """

    attempted_turn = session.turn + 1
    line = _bz.dumps_traditional(session.requests + [request], session.responses)
    request_id = "turn_" + secrets.token_urlsafe(18)
    try:
        output = await hub.request_decision(
            session.agent_id,
            request_id=request_id,
            match_id=match_id,
            seat=failed_seat,
            turn=attempted_turn,
            decision_timeout=float(action_timeout),
            input=line,
            on_decision_elapsed=on_decision_elapsed,
        )
    except LocalAITechnicalError as exc:
        if exc.error_code in {"local_ai_timeout", "decision_timeout"}:
            raise BotDecisionTimeoutError(
                TECHNICAL_INCIDENT_MESSAGES["decision_timeout"],
                error_code="decision_timeout",
                failed_seat=failed_seat,
                turn=attempted_turn,
                leg=leg,
            ) from exc
        raise
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
    on_decision_elapsed: ElapsedSink | None = None,
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
                on_decision_elapsed=on_decision_elapsed,
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

    started = _time.monotonic()
    try:
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

        # The first LongRunning response is complete only after its mandatory
        # keep_running line arrives. Read transport bytes before parsing so
        # referee CPU time is outside the player's clock.
        extra: str | None = None
        needs_handshake = (
            session.runtime_mode == _bz.RUNTIME_LONGRUNNING and is_first_turn
        )
        if needs_handshake:
            remaining = _decision_remaining(
                runner,
                session_id,
                timeout=action_timeout,
                fallback_started=started,
            )
            if remaining <= 0:
                raise BotDecisionTimeoutError(
                    TECHNICAL_INCIDENT_MESSAGES["decision_timeout"],
                    error_code="decision_timeout",
                    failed_seat=failed_seat,
                    turn=attempted_turn,
                    leg=leg,
                )
            try:
                extra = await runner.read_extra_line(
                    session_id, timeout=remaining
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
    except BotCrashedError as exc:
        # LongRunning sessions are opened before either seat acts, so a later
        # stdin/stdout failure originates inside this shared decision boundary.
        # Preserve the physical seat exactly as Traditional one-shot does.
        exc.crashed_seat = failed_seat
        raise
    finally:
        elapsed = _consume_decision_elapsed(
            runner,
            session_id,
            fallback_started=started,
        )
        if on_decision_elapsed is not None:
            on_decision_elapsed(elapsed)

    payload, debug = _protocol_payload(
        game_id,
        resp_line,
        failed_seat=failed_seat,
        turn=attempted_turn,
        leg=leg,
    )

    # LongRunning 首回合响应后必须精确输出 keep_running 握手。
    if needs_handshake:
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


class _TimeControlClock:
    """Per-match clock implementing one frozen ``TimeControlSpec``.

    Only elapsed transport intervals are recorded.  For ``per_decision`` the
    available budget resets before every request; for ``per_side_total`` each
    seat consumes its own cumulative budget for this scoring game.
    """

    def __init__(self, control: TimeControlSpec):
        self.control = control
        self._used = [0.0, 0.0]
        self._last = [0.0, 0.0]

    def begin(self, seat: int) -> None:
        self._last[seat] = 0.0

    def record(self, seat: int, elapsed: float) -> None:
        amount = max(0.0, float(elapsed))
        self._last[seat] += amount
        if self.control.mode == "per_side_total":
            self._used[seat] += amount

    def timeout_for(self, seat: int) -> float:
        if self.control.mode == "per_decision":
            return float(self.control.seconds)
        return max(0.0, float(self.control.seconds) - self._used[seat])

    def is_exhausted(self, seat: int) -> bool:
        return (
            self.control.mode == "per_side_total"
            and self.timeout_for(seat) <= 0
        )

    def decision_exceeded(self, seat: int) -> bool:
        """Fail closed if a transport returns after its authoritative limit."""

        if self.control.mode == "per_decision":
            return self._last[seat] > float(self.control.seconds)
        return self._used[seat] > float(self.control.seconds)

    def event_values(self, seat: int) -> tuple[float, float]:
        if self.control.mode == "per_side_total":
            used = self._used[seat]
        else:
            used = self._last[seat]
        return used, max(0.0, float(self.control.seconds) - used)


def _emit_time_out(
    on_event: EventSink | None,
    control: TimeControlSpec,
    clock: _TimeControlClock,
    seat: int,
    *,
    applies_to: str = "both_bots",
) -> None:
    if on_event is None:
        return
    used, _remaining = clock.event_values(seat)
    on_event(
        "time_out",
        {
            "type": "time_out",
            "seat": seat,
            "used": round(used, 3),
            "budget": control.seconds,
            "time_control": control.public_payload(applies_to=applies_to),
        },
    )


def _emit_time_used(
    on_event: EventSink | None,
    control: TimeControlSpec,
    clock: _TimeControlClock,
    seat: int,
) -> None:
    if on_event is None:
        return
    used, remaining = clock.event_values(seat)
    on_event(
        "time_used",
        {
            "type": "time_used",
            "seat": seat,
            "used": round(used, 3),
            "remaining": round(remaining, 3),
            "budget": control.seconds,
            "time_control_id": control.id,
            "mode": control.mode,
        },
    )


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
        first_error = await self._stop_execution_sessions(session_ids)
        if execution_scope is not None:
            # Cleanup is one job-level operation: both seats, every Traditional
            # one-shot container and any uncertain create are removed by the
            # same exact instance/job/attempt labels before capacity is released.
            await self.runner.cleanup_execution(execution_scope)
            first_error = None
        if first_error is not None:
            raise first_error

    async def _stop_execution_sessions(
        self, session_ids: tuple[str, ...]
    ) -> BaseException | None:
        """Stop one scoring game's sessions without closing the durable job.

        Duplicate games reuse one execution attempt but must not reuse either
        Bot process or Traditional history.  Job-wide namespace cleanup remains
        a once-only operation after the last game so the execution queue cannot
        observe a mid-series ``cleanup_confirmed`` state.
        """
        first_error: BaseException | None = None
        for session_id in session_ids:
            try:
                await self.runner.stop_session(session_id)
            except BaseException as exc:  # cleanup must continue for the other seat
                if first_error is None:
                    first_error = exc
        return first_error

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
        time_control_id: str | None = None,
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
        time_control = _resolve_runner_time_control(
            gid,
            time_control_id=time_control_id,
            time_budget_per_side=time_budget_per_side,
        )
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
            clock = _TimeControlClock(time_control)
            engine_event = _event_sink_with_time_control(
                on_event, time_control, applies_to="both_bots"
            )

            async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                sid = sid_a if player_idx == 0 else sid_b
                local_session = local_a if player_idx == 0 else local_b
                if clock.is_exhausted(player_idx):
                    _emit_time_out(on_event, time_control, clock, player_idx)
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
                effective_timeout = clock.timeout_for(player_idx)
                clock.begin(player_idx)

                def record_elapsed(elapsed: float) -> None:
                    clock.record(player_idx, elapsed)

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
                            on_decision_elapsed=record_elapsed,
                        )
                    else:
                        if sid is None:  # pragma: no cover - guarded by open_seat
                            raise RuntimeError("Docker Bot 会话不存在")
                        resp = await _botzone_decide(
                            self.runner, sid, request,
                            game_id=gid, action_timeout=effective_timeout,
                            failed_seat=player_idx,
                            on_debug=on_debug,
                            on_decision_elapsed=record_elapsed,
                        )
                except BotTechnicalError as exc:
                    # 首个协议错误/超时即结束对局；绝不伪造成游戏默认动作继续跑。
                    if isinstance(exc, BotDecisionTimeoutError):
                        _emit_time_out(on_event, time_control, clock, player_idx)
                    _emit_technical_incident(on_event, exc)
                    raise
                except (BotCrashedError, PlatformRunnerError):
                    # Bot 崩溃向上传播判技术负；平台沙箱故障也必须向上传播，
                    # 由 orchestrator 中止且不评分，绝不能吞成 Bot 默认动作。
                    raise
                if clock.decision_exceeded(player_idx):
                    _emit_time_out(on_event, time_control, clock, player_idx)
                    timeout_exc = BotDecisionTimeoutError(
                        "Bot 决策时间已耗尽",
                        error_code="decision_timeout",
                        failed_seat=player_idx,
                        turn=(
                            local_session.turn
                            if local_session is not None
                            else getattr(
                                self.runner._sessions.get(sid), "turn", 1
                            )
                        ),
                    )
                    _emit_technical_incident(on_event, timeout_exc)
                    raise timeout_exc
                _emit_time_used(on_event, time_control, clock, player_idx)
                return resp

            return await run_session(
                gid, decide, on_event=engine_event, rng=rng, **match_params,
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
        time_control_id: str | None = None,
        time_budget_per_side: float | None = None,
        execution_scope: ExecutionScope | None = None,
        **match_params: Any,
    ) -> MatchResult:
        """Bot vs 人类：bot 侧走 BinaryRunner，人类侧走 human_decide 协程。

        bot_seat 为 bot 坐位（0/1）；人类坐另一侧。human_decide(player_idx, request)
        由调用方实现（通常经 asyncio.Future 等待 WS 回传），超时由其内部处理。
        冻结的时限只约束 Bot 传输决策区间；真人仍由编排层现有防挂机
        deadline 管理，不与 Bot 棋钟共享预算。
        ``match_params`` 只承载 spec 允许的平台内部参数（同 run_binaries）。
        ``runtime_mode``：Bot 的 Botzone 运行模式（None → 平台默认 Traditional）。
        """
        import random

        gid = normalize_game_id(game_id)
        time_control = _resolve_runner_time_control(
            gid,
            time_control_id=time_control_id,
            time_budget_per_side=time_budget_per_side,
        )
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
            clock = _TimeControlClock(time_control)
            engine_event = _event_sink_with_time_control(
                on_event, time_control, applies_to="bot_only"
            )

            async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                if player_idx == bot_seat and clock.is_exhausted(player_idx):
                    _emit_time_out(
                        on_event,
                        time_control,
                        clock,
                        player_idx,
                        applies_to="bot_only",
                    )
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
                try:
                    if player_idx == bot_seat:
                        await _ensure_traditional_runtime_ready(
                            self.runner, sid_bot
                        )
                        effective_timeout = clock.timeout_for(player_idx)
                        clock.begin(player_idx)
                        resp = await _botzone_decide(
                            self.runner, sid_bot, request,
                            game_id=gid, action_timeout=effective_timeout,
                            failed_seat=bot_seat,
                            on_decision_elapsed=lambda elapsed: clock.record(
                                player_idx, elapsed
                            ),
                        )
                    else:
                        # 人类侧：生产实现返回等待 WebSocket Future 的 coroutine。
                        out = human_decide(player_idx, request)
                        if inspect.isawaitable(out):
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
                    if isinstance(exc, BotDecisionTimeoutError):
                        _emit_time_out(
                            on_event,
                            time_control,
                            clock,
                            player_idx,
                            applies_to="bot_only",
                        )
                    _emit_technical_incident(on_event, exc)
                    raise
                except (BotCrashedError, PlatformRunnerError):
                    # Bot 崩溃或平台沙箱故障都不可吞成默认动作。
                    raise
                if player_idx == bot_seat:
                    if clock.decision_exceeded(player_idx):
                        _emit_time_out(
                            on_event,
                            time_control,
                            clock,
                            player_idx,
                            applies_to="bot_only",
                        )
                        timeout_exc = BotDecisionTimeoutError(
                            "Bot 决策时间已耗尽",
                            error_code="decision_timeout",
                            failed_seat=bot_seat,
                            turn=getattr(
                                self.runner._sessions.get(sid_bot), "turn", 1
                            ),
                        )
                        _emit_technical_incident(on_event, timeout_exc)
                        raise timeout_exc
                    _emit_time_used(on_event, time_control, clock, player_idx)
                return resp

            return await run_session(
                gid, decide, on_event=engine_event, rng=rng, **match_params,
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
        time_control_id: str | None = None,
        **match_params: Any,
    ) -> MatchResult:
        """跑两个 callable bot（测试用）；固定规则参数仍会被 spec 明确拒绝。"""
        import random

        gid = normalize_game_id(game_id)
        time_control = _game_registry.get(gid).resolve_time_control(time_control_id)
        rng = random.Random(seed) if seed is not None else random.Random()
        engine_event = _event_sink_with_time_control(
            on_event, time_control, applies_to="both_bots"
        )

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
            gid, decide, on_event=engine_event, rng=rng, **match_params,
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
        time_control_id: str | None = None,
        time_budget_per_side: float | None = None,
        execution_scope: ExecutionScope | None = None,
        **match_params: Any,
    ) -> Any:
        """复式：按计划跑多个计分场，每场独立判胜负。

        各场用同 deal_sequence（消除运气）；seat_swap=True 的计划对调 decide 回调
        （B 在 seat0）。每场独立产出 winner + deltas（物理 bot A/B 视角，换座场翻转），
        结果持久化在 ``legs`` 字段，编排层按每个计分场逐场累加 3/1/0。

        每个计分场都重新建立并关闭两方逻辑会话：Traditional 不继承上一场的
        requests/responses，LongRunning 也不复用上一场的常驻进程/进程内存。
        decide 闭包仍复用 `_botzone_decide`（与 run_binaries 一致），支持两种
        runtime mode；两场之间只共享冻结 deal_sequence 与换座计划。
        游戏未声明 duplicate 计划时明确拒绝，不把请求悄悄改成单场。

        返回带 ``legs`` 字段的结果（首场 MatchResult 结构 + 逐场列表）。
        final_chips/net 保留各场物理累加（仅作分差破同分，不作为胜负判据）。
        """
        from bzplat.backend.games import registry as _reg

        gid = normalize_game_id(game_id)
        spec = _reg.get(gid)
        time_control = _resolve_runner_time_control(
            gid,
            time_control_id=time_control_id,
            time_budget_per_side=time_budget_per_side,
        )
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
        try:
            for li, leg in enumerate(legs_plan):
                lp = dict(leg.get("params") or {})
                lp.pop("match_seed", None)
                swap = bool(leg.get("seat_swap"))
                def leg_on_event(kind: str, ev: dict[str, Any]) -> None:
                    ev2 = {**ev, "leg": li}
                    if on_event:
                        on_event(kind, ev2)

                # Publicly delimit every independent scoring game before the
                # first Bot process/session is opened.  A startup crash at the
                # first request therefore still has an unambiguous ``leg`` in
                # replay/live state and cannot be mistaken for a single game.
                leg_on_event(
                    "match_start",
                    {
                        "type": "match_start",
                        "game_id": gid,
                        "time_control": time_control.public_payload(),
                    },
                )
                clock = _TimeControlClock(time_control)
                engine_event = _event_sink_with_time_control(
                    leg_on_event,
                    time_control,
                    applies_to="both_bots",
                    suppress_match_start=True,
                )

                sid_a: str | None = None
                sid_b: str | None = None
                try:
                    sid_a = await _open_match_session(
                        self.runner,
                        path_a,
                        rm_a,
                        failed_seat=0,
                        profile=_profile_for_environment(
                            env_a, execution_profile_version
                        ),
                        execution_scope=execution_scope,
                    )
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

                    async def decide(
                        player_idx: int, request: dict[str, Any]
                    ) -> dict[str, Any]:
                        # seat_swap：seat0 → 物理 B（对调座位），seat1 → 物理 A
                        if swap:
                            sid = sid_b if player_idx == 0 else sid_a
                        else:
                            sid = sid_a if player_idx == 0 else sid_b
                        assert sid is not None
                        physical_seat = 1 - player_idx if swap else player_idx
                        if clock.is_exhausted(physical_seat):
                            _emit_time_out(
                                leg_on_event, time_control, clock, physical_seat
                            )
                            timeout_exc = BotDecisionTimeoutError(
                                "Bot 累计决策时间已耗尽",
                                error_code="decision_timeout",
                                failed_seat=physical_seat,
                                turn=getattr(
                                    self.runner._sessions.get(sid), "turn", 0
                                ) + 1,
                                leg=li,
                            )
                            _emit_technical_incident(leg_on_event, timeout_exc)
                            raise timeout_exc
                        await _ensure_traditional_runtime_ready(self.runner, sid)
                        clock.begin(physical_seat)
                        try:
                            response = await _botzone_decide(
                                self.runner,
                                sid,
                                request,
                                game_id=gid,
                                action_timeout=clock.timeout_for(physical_seat),
                                failed_seat=physical_seat,
                                leg=li,
                                on_debug=on_debug,
                                on_decision_elapsed=lambda elapsed: clock.record(
                                    physical_seat, elapsed
                                ),
                            )
                        except BotTechnicalError as exc:
                            if isinstance(exc, BotDecisionTimeoutError):
                                _emit_time_out(
                                    leg_on_event,
                                    time_control,
                                    clock,
                                    physical_seat,
                                )
                            _emit_technical_incident(leg_on_event, exc)
                            raise
                        except BotCrashedError as exc:
                            # Preserve the current independent game for the
                            # orchestrator's per-game technical progress contract.
                            exc.crashed_leg = li
                            raise
                        except PlatformRunnerError:
                            # 平台沙箱故障向上传播（与 run_binaries 一致）。
                            raise
                        if clock.decision_exceeded(physical_seat):
                            _emit_time_out(
                                leg_on_event,
                                time_control,
                                clock,
                                physical_seat,
                            )
                            timeout_exc = BotDecisionTimeoutError(
                                "Bot 决策时间已耗尽",
                                error_code="decision_timeout",
                                failed_seat=physical_seat,
                                turn=getattr(
                                    self.runner._sessions.get(sid), "turn", 1
                                ),
                                leg=li,
                            )
                            _emit_technical_incident(
                                leg_on_event, timeout_exc
                            )
                            raise timeout_exc
                        _emit_time_used(
                            leg_on_event, time_control, clock, physical_seat
                        )
                        return response

                    res = await run_session(
                        gid, decide, on_event=engine_event, **lp,
                    )
                except BotCrashedError as exc:
                    # Session startup can fail before ``decide`` is entered.
                    exc.crashed_leg = li
                    raise
                finally:
                    session_ids = tuple(
                        sid for sid in (sid_a, sid_b) if sid is not None
                    )
                    stop_error = await self._stop_execution_sessions(session_ids)
                    if stop_error is not None:
                        raise stop_error
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
                leg_results.append(
                    {
                        "winner": leg_winner,
                        "deltas": list(leg_deltas),
                        "rounds_played": int(getattr(res, "rounds_played", 0)),
                    }
                )
        finally:
            if execution_scope is not None:
                # Prove the whole attempt namespace empty exactly once, after
                # every independent scoring game's sessions have been stopped.
                await self._close_execution_sessions((), execution_scope)
        # 构造结果：首场结构 + legs 逐场结果 + 仅供破同分的组合 deltas
        if final_result is not None:
            try:
                final_result.rounds = merged_rounds
                final_result.rounds_played = merged_rounds_played
                final_result.events = merged_events
                # net/final_chips 留作分差破同分（各场物理累加），不作胜负判据
                if hasattr(final_result, "net"):
                    final_result.net = list(merged_deltas)
                if hasattr(final_result, "final_chips"):
                    final_result.final_chips = list(merged_deltas)
                # legs 字段：编排层按每个计分场独立判胜负并累加积分
                final_result.legs = leg_results
            except Exception:
                pass
        return final_result
