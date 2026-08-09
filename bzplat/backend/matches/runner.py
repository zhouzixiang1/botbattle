"""对局执行：BinaryRunner ×2 + 按 game_id 路由引擎。"""
from __future__ import annotations

import asyncio
import inspect
import time as _time
from typing import Any, Callable

from bzplat.backend.games import (
    GAME_HOLDEM,
    fail_response as _reg_fail,
    normalize_game_id,
    registry as _game_registry,
    run_session,
)
# 全面解耦：runner 不再按 game_id 切协议模块，统一委托 games 注册表。
# 注：不 import 具体游戏模块（审计 P1：通用层不得依赖 games/holdem）。
# 游戏规则参数（num_hands/n_dots/board_size/...）经 **match_params 透传，runner 不持有
# 任何游戏专属默认值（第 4 游戏带新参数无需改本签名）。
from bzplat.backend.games import _botzone_protocol as _bz
from bzplat.backend.runtime.binary_runner import (
    BinaryRunner,
    BotCrashedError,
    BotDecisionTimeoutError,
    BotProtocolError,
    BotTechnicalError,
    PlatformRunnerError,
    DEFAULT_ACTION_TIMEOUT,
)
from bzplat.backend.store.schema import (
    TECHNICAL_INCIDENT_EVENT,
    TECHNICAL_INCIDENT_MESSAGES,
)

EventSink = Callable[[str, dict[str, Any]], None]


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
) -> Any:
    """Strictly decode one Botzone response without persisting raw Bot output."""
    try:
        return _bz.decode_response_payload(
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


async def _open_match_session(
    runner: BinaryRunner,
    binary_path: str,
    runtime_mode: str,
    *,
    failed_seat: int,
) -> str:
    """建立一方逻辑会话；Traditional 只登记历史，不预启动闲置进程。"""
    try:
        if runtime_mode == _bz.RUNTIME_TRADITIONAL:
            return await runner.prepare_session(
                binary_path,
                runtime_mode=runtime_mode,
            )
        if runtime_mode == _bz.RUNTIME_LONGRUNNING:
            return await runner.start_session(
                binary_path,
                runtime_mode=runtime_mode,
            )
        raise ValueError(f"未知运行模式: {runtime_mode}")
    except BotCrashedError as exc:
        exc.crashed_seat = failed_seat
        raise


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
        tmp_sid = await runner.start_session(
            session.binary_path,
            runtime_mode=_bz.RUNTIME_TRADITIONAL,
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
    payload = _protocol_payload(
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
            )
        except asyncio.TimeoutError as exc:
            raise BotDecisionTimeoutError(
                TECHNICAL_INCIDENT_MESSAGES["decision_timeout"],
                error_code="decision_timeout",
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

    payload = _protocol_payload(
        game_id,
        resp_line,
        failed_seat=failed_seat,
        turn=attempted_turn,
        leg=leg,
    )

    # LongRunning 首回合响应后必须精确输出 keep_running 握手。
    if session.runtime_mode == _bz.RUNTIME_LONGRUNNING and is_first_turn:
        extra = await runner.read_extra_line(session_id, timeout=1.0)
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
    ) -> None:
        self.runner = runner or BinaryRunner()
        self.action_timeout = action_timeout

    async def run_binaries(
        self,
        path_a: str,
        path_b: str,
        *,
        game_id: str = GAME_HOLDEM,
        on_event: EventSink | None = None,
        seed: int | None = None,
        runtime_modes: tuple[str, str] | None = None,
        time_budget_per_side: float | None = None,
        **match_params: Any,
    ) -> MatchResult:
        """跑两个二进制 bot。游戏规则参数（num_hands/n_dots/board_size/starting_stack/sb/bb/...）
        经 **match_params 透传给 run_session——新增第 4 游戏带新参数（如 komi）无需改本签名。

        决定哪些参数、各参数默认值/校验，全在 GameSpec（default_match_params /
        validate_match_params / judge_params）里声明；runner 不持有任何游戏专属知识。

        ``runtime_modes``：(bot_a 的 Botzone 运行模式, bot_b 的)。None → 使用平台
        权威默认模式（Traditional）。
        """
        import random

        gid = normalize_game_id(game_id)
        rm_a, rm_b = runtime_modes or (
            _bz.DEFAULT_RUNTIME_MODE,
            _bz.DEFAULT_RUNTIME_MODE,
        )
        sid_a = await _open_match_session(
            self.runner,
            path_a,
            rm_a,
            failed_seat=0,
        )
        try:
            sid_b = await _open_match_session(
                self.runner,
                path_b,
                rm_b,
                failed_seat=1,
            )
        except BaseException:
            await self.runner.stop_session(sid_a)
            raise
        try:
            rng = random.Random(seed) if seed is not None else random.Random()
            clock = _ChessClock(time_budget_per_side)

            async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                sid = sid_a if player_idx == 0 else sid_b
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
                            turn=getattr(self.runner._sessions.get(sid), "turn", 0) + 1,
                        )
                        _emit_technical_incident(on_event, timeout_exc)
                        raise timeout_exc
                    effective_timeout = clock.remaining(player_idx)
                else:
                    effective_timeout = self.action_timeout
                t0 = clock.now()
                try:
                    resp = await _botzone_decide(
                        self.runner, sid, request,
                        game_id=gid, action_timeout=effective_timeout,
                        failed_seat=player_idx,
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
            await self.runner.stop_session(sid_a)
            await self.runner.stop_session(sid_b)

    async def run_bot_vs_human(
        self,
        bot_path: str,
        *,
        bot_seat: int,
        human_decide,
        game_id: str = GAME_HOLDEM,
        on_event: EventSink | None = None,
        seed: int | None = None,
        runtime_mode: str | None = None,
        time_budget_per_side: float | None = None,
        **match_params: Any,
    ) -> MatchResult:
        """Bot vs 人类：bot 侧走 BinaryRunner，人类侧走 human_decide 协程。

        bot_seat 为 bot 坐位（0/1）；人类坐另一侧。human_decide(player_idx, request)
        由调用方实现（通常经 asyncio.Future 等待 WS 回传），超时由其内部处理。
        ``time_budget_per_side`` 启用双方共享契约的累计棋钟；人类 Future 另以
        棋钟剩余时间作外层 deadline，不能靠逐手及时响应绕过累计预算。
        游戏规则参数经 **match_params 透传（同 run_binaries）。
        ``runtime_mode``：Bot 的 Botzone 运行模式（None → 平台默认 Traditional）。
        """
        import random

        gid = normalize_game_id(game_id)
        rm = runtime_mode or _bz.DEFAULT_RUNTIME_MODE
        sid_bot = await _open_match_session(
            self.runner,
            bot_path,
            rm,
            failed_seat=bot_seat,
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
            await self.runner.stop_session(sid_bot)

    async def run_callables(
        self,
        decide_a,
        decide_b,
        *,
        game_id: str = GAME_HOLDEM,
        on_event: EventSink | None = None,
        seed: int | None = None,
        **match_params: Any,
    ) -> MatchResult:
        """跑两个 callable bot（测试用）。游戏规则参数经 **match_params 透传。"""
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
        game_id: str = GAME_HOLDEM,
        seed: int | None = None,
        on_event: EventSink | None = None,
        runtime_modes: tuple[str, str] | None = None,
        time_budget_per_side: float | None = None,
        **match_params: Any,
    ) -> Any:
        """P4 duplicate：跑多 leg（经 spec.build_match_plan），**每 leg 独立判胜负**。

        每 leg 用同 deal_sequence（消除运气）；seat_swap=True 的 leg 对调 decide 回调
        （B 在 seat0）。每 leg 独立产出 winner + deltas（物理 bot A/B 视角，swap leg 翻转），
        收集进 ``legs`` 字段——编排层（standings/ranking）按"打了两场"逐 leg 累加积分
        （如 2 场 poker_3_1_0），**不再把两场净筹码合并判 1 场胜负**。

        decide 闭包复用 `_botzone_decide`（与 run_binaries 一致），支持 traditional/
        longrunning 协议与 runtime_modes——真 Botzone bot 在两 leg 中均可正确收发。
        游戏不支持 duplicate（spec.build_match_plan is None）→ 退化为单 leg run_binaries。

        返回带 ``legs`` 字段的结果（首 leg 的 MatchResult 结构 + legs 列表）。
        final_chips/net 保留两 leg 物理累加（仅作 net_chips tiebreak，不作为胜负判据）。
        """
        from bzplat.backend.games import registry as _reg

        spec = _reg.get(game_id)
        if spec.build_match_plan is None:
            # 游戏不支持 duplicate → 退化为单 leg（透传 time_budget_per_side，
            # 与 run_binaries 象棋钟路径一致）
            return await self.run_binaries(
                path_a, path_b, game_id=game_id, on_event=on_event, seed=seed,
                runtime_modes=runtime_modes,
                time_budget_per_side=time_budget_per_side,
                **match_params,
            )
        legs_plan = spec.build_match_plan(seed or 0, match_params)
        # 每 leg 独立胜负（物理 bot A/B 视角）；累加 deltas 仅留作 net_chips tiebreak
        leg_results: list[dict[str, Any]] = []
        merged_deltas = [0, 0]  # 物理 A/B，仅 net_chips tiebreak 用
        merged_rounds: list[Any] = []
        merged_events: list[dict[str, Any]] = []
        final_result = None
        rm_a, rm_b = runtime_modes or (
            _bz.DEFAULT_RUNTIME_MODE,
            _bz.DEFAULT_RUNTIME_MODE,
        )
        sid_a = await _open_match_session(
            self.runner,
            path_a,
            rm_a,
            failed_seat=0,
        )
        try:
            sid_b = await _open_match_session(
                self.runner,
                path_b,
                rm_b,
                failed_seat=1,
            )
        except BaseException:
            await self.runner.stop_session(sid_a)
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
            await self.runner.stop_session(sid_a)
            await self.runner.stop_session(sid_b)
        # 构造结果：首 leg 结构 + legs 字段（每 leg 独立胜负）+ 累加 deltas（tiebreak 用）
        if final_result is not None:
            try:
                final_result.rounds = merged_rounds
                final_result.events = merged_events
                # net/final_chips 留作 net_chips tiebreak（两 leg 物理累加），不作胜负判据
                if hasattr(final_result, "net"):
                    final_result.net = list(merged_deltas)
                if hasattr(final_result, "final_chips"):
                    final_result.final_chips = list(merged_deltas)
                # legs 字段：编排层（standings/ranking）按每 leg 独立判胜负累加积分
                final_result.legs = leg_results
            except Exception:
                pass
        return final_result
