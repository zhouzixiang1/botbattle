"""对局执行：BinaryRunner ×2 + 按 game_id 路由引擎。"""
from __future__ import annotations

import inspect
import logging
from typing import Any, Callable

from bzplat.backend.games import (
    GAME_HOLDEM,
    dumps as _reg_dumps,
    fail_response as _reg_fail,
    loads as _reg_loads,
    normalize_game_id,
    run_session,
)
# 全面解耦：runner 不再按 game_id 切协议模块，统一委托 games 注册表。
# 注：不 import 具体游戏模块（审计 P1：通用层不得依赖 games/holdem）。
# 游戏规则参数（num_hands/n_dots/board_size/...）经 **match_params 透传，runner 不持有
# 任何游戏专属默认值（第 4 游戏带新参数无需改本签名）。
from bzplat.backend.games import _botzone_protocol as _bz
from bzplat.backend.runtime.binary_runner import BinaryRunner, BotCrashedError, DEFAULT_ACTION_TIMEOUT

logger = logging.getLogger(__name__)

EventSink = Callable[[str, dict[str, Any]], None]


def _dumps(game_id: str, request: dict[str, Any]) -> str:
    return _reg_dumps(game_id, request)


def _loads(game_id: str, line: str) -> dict[str, Any]:
    return _reg_loads(game_id, line)


def _fail_response(game_id: str) -> dict[str, Any]:
    """超时/异常时的兜底响应（按游戏：扑克 fold；棋类非法坐标）。"""
    return _reg_fail(game_id)


async def _botzone_decide(
    runner: BinaryRunner,
    session_id: str,
    request: dict[str, Any],
    *,
    game_id: str,
    action_timeout: float,
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
    session.requests.append(request)

    is_first_turn = session.turn == 0
    if session.runtime_mode == _bz.RUNTIME_TRADITIONAL or (session.runtime_mode == _bz.RUNTIME_LONGRUNNING and is_first_turn):
        # 首回合 / Traditional：发完整历史信封。
        line = _bz.dumps_traditional(session.requests, session.responses)
    else:
        # LongRunning 后续回合：发单 request 信封。
        line = _bz.dumps_longrunning_single(request)

    resp_line = await runner.send(session_id, line, timeout=action_timeout)

    # LongRunning 首回合响应后探测 keep_running 握手。
    if (session.runtime_mode == _bz.RUNTIME_LONGRUNNING and is_first_turn):
        extra = await runner.read_extra_line(session_id, timeout=1.0)
        if extra is not None and _bz.is_keep_running_signal(extra):
            session.long_running = True

    envelope = _bz.loads_response(resp_line)
    payload = _bz.extract_response_payload(envelope)
    session.responses.append(payload)
    session.turn += 1
    # 返回信封 dict（引擎 parse_response 接受 {"response": int} 与裸 int 两种；这里统一
    # 包成信封以满足引擎「decide 必返回 dict」的契约）。
    return {"response": payload}



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
        **match_params: Any,
    ) -> MatchResult:
        """跑两个二进制 bot。游戏规则参数（num_hands/n_dots/board_size/starting_stack/sb/bb/...）
        经 **match_params 透传给 run_session——新增第 4 游戏带新参数（如 komi）无需改本签名。

        决定哪些参数、各参数默认值/校验，全在 GameSpec（default_match_params /
        validate_match_params / judge_params）里声明；runner 不持有任何游戏专属知识。

        ``runtime_modes``：(bot_a 的 Botzone 运行模式, bot_b 的)。None → 均默认 longrunning。
        PR-2 将从 bots/runtime_mode 读取后传入；PR-1 默认 longrunning 跑通。
        """
        import random

        gid = normalize_game_id(game_id)
        rm_a, rm_b = runtime_modes or (_bz.RUNTIME_LONGRUNNING, _bz.RUNTIME_LONGRUNNING)
        sid_a = await self.runner.start_session(path_a, runtime_mode=rm_a)
        try:
            sid_b = await self.runner.start_session(path_b, runtime_mode=rm_b)
        except BotCrashedError as exc:
            # 第二个 session（bot_b）启动失败：释放已启动的 bot_a，并注解崩溃方=1
            # 供 orchestrator 的技术判负判胜方（bot_b 崩 → winner=0）。
            await self.runner.stop_session(sid_a)
            exc.crashed_seat = 1
            raise
        except BaseException:
            # 其他异常（非 BotCrashedError）：仅释放 bot_a，不注解座位。
            await self.runner.stop_session(sid_a)
            raise
        try:
            rng = random.Random(seed) if seed is not None else random.Random()

            async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                sid = sid_a if player_idx == 0 else sid_b
                try:
                    return await _botzone_decide(
                        self.runner, sid, request,
                        game_id=gid, action_timeout=self.action_timeout,
                    )
                except BotCrashedError:
                    # Bot 进程已死，不可恢复——向上传播触发对局 abort（而非吞成默认动作死磕）
                    raise
                except Exception as exc:
                    logger.warning("bot %s decide failed: %s", player_idx, exc)
                    return _fail_response(gid)

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
        **match_params: Any,
    ) -> MatchResult:
        """Bot vs 人类：bot 侧走 BinaryRunner，人类侧走 human_decide 协程。

        bot_seat 为 bot 坐位（0/1）；人类坐另一侧。human_decide(player_idx, request)
        由调用方实现（通常经 asyncio.Future 等待 WS 回传），超时由其内部处理。
        游戏规则参数经 **match_params 透传（同 run_binaries）。
        ``runtime_mode``：Bot 的 Botzone 运行模式（None → 默认 longrunning）。
        """
        import random

        gid = normalize_game_id(game_id)
        rm = runtime_mode or _bz.RUNTIME_LONGRUNNING
        sid_bot = await self.runner.start_session(bot_path, runtime_mode=rm)
        try:
            rng = random.Random(seed) if seed is not None else random.Random()

            async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                if player_idx == bot_seat:
                    try:
                        return await _botzone_decide(
                            self.runner, sid_bot, request,
                            game_id=gid, action_timeout=self.action_timeout,
                        )
                    except BotCrashedError:
                        # Bot 进程已死——向上传播触发对局 abort
                        raise
                    except Exception as exc:
                        logger.warning("bot %s decide failed: %s", player_idx, exc)
                        return _fail_response(gid)
                # 人类侧
                out = human_decide(player_idx, request)
                if inspect.isawaitable(out):
                    out = await out
                return out if isinstance(out, dict) else _fail_response(gid)

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
            return out

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
        **match_params: Any,
    ) -> Any:
        """P4 duplicate：跑多 leg（经 spec.build_match_plan），合并 net 判胜负。

        每 leg 用同 deal_sequence（消除运气）；seat_swap=True 的 leg 对调 decide 回调
        （B 在 seat0）；合并各 leg 的 deltas 为最终结果。
        """
        from bzplat.backend.games import registry as _reg

        spec = _reg.get(game_id)
        if spec.build_match_plan is None:
            # 游戏不支持 duplicate → 退化为单 leg
            return await self.run_binaries(
                path_a, path_b, game_id=game_id, on_event=on_event, seed=seed,
                **match_params,
            )
        legs = spec.build_match_plan(seed or 0, match_params)
        # 合并结果：用第一 leg 的 MatchResult 结构，累加 deltas；winner 按 net 判
        merged_deltas = [0, 0]
        merged_rounds: list[Any] = []
        merged_events: list[dict[str, Any]] = []
        final_result = None
        sid_a = await self.runner.start_session(path_a)
        try:
            sid_b = await self.runner.start_session(path_b)
        except BotCrashedError as exc:
            # 第二个 session（bot_b）启动失败：释放已启动的 bot_a，并注解崩溃方=1
            # 供 orchestrator 的技术判负判胜方（bot_b 崩 → winner=0）。镜像 run_binaries。
            await self.runner.stop_session(sid_a)
            exc.crashed_seat = 1
            raise
        except BaseException:
            await self.runner.stop_session(sid_a)
            raise
        try:
            for li, leg in enumerate(legs):
                lp = dict(leg.get("params") or {})
                lp.pop("match_seed", None)
                swap = bool(leg.get("seat_swap"))

                async def decide(player_idx: int, request: dict[str, Any]) -> dict[str, Any]:
                    # seat_swap：seat0 → 物理 B（对调座位），seat1 → 物理 A
                    if swap:
                        sid = sid_b if player_idx == 0 else sid_a
                    else:
                        sid = sid_a if player_idx == 0 else sid_b
                    line = _dumps(game_id, request)
                    try:
                        resp_line = await self.runner.send(sid, line, timeout=self.action_timeout)
                        return _loads(game_id, resp_line)
                    except BotCrashedError:
                        raise
                    except Exception as exc:
                        logger.warning("bot decide failed (leg%s seat%s): %s", li, player_idx, exc)
                        return _fail_response(game_id)

                def leg_on_event(kind: str, ev: dict[str, Any]) -> None:
                    ev2 = {**ev, "leg": li}
                    if on_event:
                        on_event(kind, ev2)

                res = await run_session(
                    game_id, decide, on_event=leg_on_event, **lp,
                )
                if final_result is None:
                    final_result = res
                # 累加 deltas（注意 seat_swap 的 leg2：B 在 seat0，其 delta[0] 属于 B）
                for r in getattr(res, "rounds", []):
                    d = r.deltas
                    if swap:
                        merged_deltas[1] += d[0]  # leg2 seat0=B 的净筹码加给 B（seat1）
                        merged_deltas[0] += d[1]
                    else:
                        merged_deltas[0] += d[0]
                        merged_deltas[1] += d[1]
                    merged_rounds.append(r)
                merged_events.extend(getattr(res, "events", []))
        finally:
            await self.runner.stop_session(sid_a)
            await self.runner.stop_session(sid_b)
        # 构造合并后的结果（用首 leg 的 result 类型，覆盖 deltas/rounds/winner）
        if final_result is not None:
            ea, eb = merged_deltas[0], merged_deltas[1]
            winner = 0 if ea > eb else 1 if eb > ea else None
            try:
                final_result.rounds = merged_rounds
                final_result.events = merged_events
                # holdem result：覆盖 net/final_chips（如果属性存在）
                if hasattr(final_result, "net"):
                    final_result.net = list(merged_deltas)
                if hasattr(final_result, "final_chips"):
                    final_result.final_chips = list(merged_deltas)
            except Exception:
                pass
        return final_result
