"""对局编排：challenge 入队、评分更新、SSE 扇出。"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime
from typing import Any, Callable

from bzplat.backend.games import registry as game_registry
from bzplat.backend.games import normalize_game_id
from bzplat.backend.matches.runner import MatchRunner, _fail_response
from bzplat.backend.rating.glicko2 import Rating, match_scores, update_rating
from bzplat.backend.runtime.binary_runner import BinaryRunner, BotCrashedError
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    REGISTERED_ENGINES,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TYPE_CHALLENGE,
    TYPE_CONTEST,
    TYPE_HUMAN,
    TYPE_TABLE,
    VALID_GAME_IDS,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class HumanInactive(Exception):
    """人类玩家连续超时不响应（连续 ≥ human_max_consecutive_timeouts 次）。

    由 _run_human_match 的 human_decide 在达到阈值时抛出，向上经 runner（人类侧
    不吞异常）→ holdem 引擎（run_async 的 try 仅捕 BotCrashedError，故透传）→
    回到 _run_human_match 的 except HumanInactive 分支中止对局。
    棋类一手非法即结束，不会累积到此阈值。
    """


class MatchOrchestrator:
    def __init__(
        self,
        store: Store,
        *,
        runner: MatchRunner | None = None,
        max_concurrent: int = 2,
    ) -> None:
        self.store = store
        self.runner = runner or MatchRunner(BinaryRunner())
        self.max_concurrent = max_concurrent
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task] = {}
        self._sse: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        # 对局完成后的回调（由外部注入，如比赛归档）。签名: (match_id, contest_id|None) -> None
        self.on_match_done: "Callable[[str, int | None], None] | None" = None
        # 通知管理器（由 main.py 注入；对局完成时通知双方 owner）
        self.notifier = None
        # ── 人类对战（独立并发，不占 bot 对局槽）──────────────────
        self.human_max_concurrent = 4
        self._human_sem = asyncio.Semaphore(self.human_max_concurrent)
        # (match_id, player_idx) → pending 人类回合 {request, future, ts}
        self._human_turns: dict[tuple[str, int], dict] = {}
        # 每 user 同时进行的人类局 ≤ 1（节流，防挂机占满人类槽）
        self._human_active_users: set[int] = set()
        self.human_action_timeout = 120.0  # 人类决策超时（秒）
        # 连续超时阈值：人类连续 N 次不响应则中止对局（避免 70 手最长 2.3h 死磕，
        # 占用人类槽 + 锁死 _human_active_users）。棋类一手非法即结束，仅 holdem 触发。
        self.human_max_consecutive_timeouts = 5

    def rebuild_concurrency(self, max_concurrent: int) -> None:
        """热更新并发上限：重建 Semaphore（不修改 _value）。"""
        self.max_concurrent = max(1, int(max_concurrent))
        self._sem = asyncio.Semaphore(self.max_concurrent)

    def rebuild_human_concurrency(self, max_concurrent: int) -> None:
        """热更新人类对局独立并发上限。"""
        self.human_max_concurrent = max(1, int(max_concurrent))
        self._human_sem = asyncio.Semaphore(self.human_max_concurrent)

    def set_action_timeout(self, timeout_sec: float) -> None:
        self.runner.action_timeout = float(timeout_sec)

    def set_human_action_timeout(self, timeout_sec: float) -> None:
        self.human_action_timeout = float(timeout_sec)

    # ── 人类对战：回合 Future 注册表（供 WS /move 解析）─────────
    def get_human_turn(self, match_id: str, player_idx: int) -> dict | None:
        return self._human_turns.get((match_id, player_idx))

    def resolve_human_turn(self, match_id: str, player_idx: int, move: dict) -> bool:
        """WS 收到人类落子：解析 pending Future。返回是否成功。"""
        entry = self._human_turns.get((match_id, player_idx))
        if not entry or entry["future"].done():
            return False
        entry["future"].set_result(move)
        return True

    async def challenge_human(
        self,
        bot_id: int,
        human_user_id: int,
        *,
        human_seat: int = 1,
        game_id: str | None = None,
        hands: int = 70,  # holdem 默认手数（审计 P1：不 import 具体游戏模块）
        n_dots: int | None = None,
    ) -> str:
        """人类 vs bot：human_seat 为人类坐位（0/1），另一侧为 bot_id。

        人类侧无 bot/binary，走 runner.run_bot_vs_human（人类 decide 经 Future
        等待 WS 回传）。不计 Glicko；占用独立 _human_sem（不占 bot 对局槽）。
        """
        bot = self.store.get_bot(bot_id)
        if not bot:
            raise ValueError("bot 不存在")
        if not bot.get("is_active") or not bot.get("binary_path"):
            raise ValueError("bot 不可用")
        if human_user_id in self._human_active_users:
            raise ValueError("你已有一场人类对局进行中，请先结束")
        gid = normalize_game_id(game_id or bot.get("game_id"))
        if gid != normalize_game_id(bot.get("game_id")):
            raise ValueError(f"指定游戏 {gid} 与 Bot 游戏 {bot.get('game_id')} 不一致")
        if gid not in VALID_GAME_IDS or gid not in REGISTERED_ENGINES:
            raise ValueError(f"游戏引擎未注册: {gid}")

        # 校验 match_config（hands 范围按游戏 spec；取代 API 层 le=300 的宽上限）
        try:
            game_registry.get(gid).validate_match_params({"hands": hands})
        except ValueError as e:
            raise ValueError(f"match 参数非法: {e}") from None

        bot_seat = 1 - human_seat
        # 人类侧用一个伪 bot_id 占位（取 bot_id 自身，仅满足 NOT NULL FK；
        # 真正的人类动作经 _human_turns / WS 回传，不走 binary）
        bot_a_id = bot_id if bot_seat == 0 else bot_id
        bot_b_id = bot_id if bot_seat == 1 else bot_id
        # 每游戏轮数：holdem=hands；棋类=1（经 spec.rounds_per_match，消除 if game_id）
        total_hands = game_registry.get(gid).rounds_per_match({"hands": hands})
        match_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
        self.store.create_match(
            match_id,
            bot_a_id=bot_a_id,
            bot_b_id=bot_b_id,
            owner_id=human_user_id,
            total_hands=total_hands,
            match_type=TYPE_HUMAN,
            game_id=gid,
            n_dots=n_dots,
            human_user_id=human_user_id,
            human_seat=human_seat,
        )
        self.store.upsert_replay(match_id, "[]", "[]")
        self._human_active_users.add(human_user_id)
        task = asyncio.create_task(self._run_human_match(match_id), name=f"human-{match_id}")
        self._tasks[match_id] = task
        return match_id

    async def challenge(
        self,
        challenger_bot_id: int,
        opponent_bot_id: int,
        owner_user_id: int | None,
        *,
        hands: int = 70,  # holdem 默认手数（审计 P1：不 import 具体游戏模块）
        match_type: str = TYPE_CHALLENGE,
        contest_id: int | None = None,
        game_id: str | None = None,
        n_dots: int | None = None,
        **extra_match_params: Any,
    ) -> str:
        if challenger_bot_id == opponent_bot_id:
            raise ValueError("不能与自己对战")
        bot_a = self.store.get_bot(challenger_bot_id)
        bot_b = self.store.get_bot(opponent_bot_id)
        if not bot_a or not bot_b:
            raise ValueError("bot 不存在")
        if not bot_a.get("is_active") or not bot_a.get("binary_path"):
            raise ValueError("己方 bot 不可用")
        if not bot_b.get("is_active") or not bot_b.get("binary_path"):
            raise ValueError("对手 bot 不可用")

        ga = normalize_game_id(bot_a.get("game_id"))
        gb = normalize_game_id(bot_b.get("game_id"))
        if ga != gb:
            raise ValueError(f"双方 Bot 游戏类型不一致：{ga} vs {gb}")
        gid = normalize_game_id(game_id) if game_id else ga
        if gid != ga:
            raise ValueError(f"指定游戏 {gid} 与 Bot 游戏 {ga} 不一致")
        if gid not in VALID_GAME_IDS:
            raise ValueError(f"未知游戏: {gid}")
        if gid not in REGISTERED_ENGINES:
            raise ValueError(
                f"游戏引擎未注册: {gid}（当前支持 {sorted(REGISTERED_ENGINES)}）"
            )

        # 校验 match_config（hands 范围按游戏 spec；取代 API 层 le=70 的 holdem 专用上限泄漏）
        try:
            game_registry.get(gid).validate_match_params({"hands": hands})
        except ValueError as e:
            raise ValueError(f"match 参数非法: {e}") from None

        # 棋类单局；扑克沿用 hands
        # 每游戏轮数：holdem=hands；棋类=1（经 spec.rounds_per_match，消除 if game_id）
        total_hands = game_registry.get(gid).rounds_per_match({"hands": hands})

        match_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
        self.store.create_match(
            match_id,
            bot_a_id=challenger_bot_id,
            bot_b_id=opponent_bot_id,
            owner_id=owner_user_id,
            contest_id=contest_id,
            total_hands=total_hands,
            match_type=match_type,
            game_id=gid,
            n_dots=n_dots,
        )
        self.store.upsert_replay(match_id, "[]", "[]")
        task = asyncio.create_task(self._run_match(match_id), name=f"match-{match_id}")
        self._tasks[match_id] = task
        return match_id

    def subscribe(self, match_id: str) -> asyncio.Queue:
        # maxsize=2000：减少 Bot 决策极快时丢事件（原 500 太小）；满时 drop oldest 见 _broadcast
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._sse.setdefault(match_id, []).append(q)
        # 统一 detailed + 嵌套 seats（含人类座真人用户名）
        from bzplat.backend.matches.seat_info import match_for_viewer

        m = match_for_viewer(self.store, match_id)
        replay = self.store.get_replay(match_id) or {}
        q.put_nowait({"type": "snapshot", "match": m or {}, "events": json.loads(replay.get("events_json") or "[]")})
        return q

    def unsubscribe(self, match_id: str, q: asyncio.Queue) -> None:
        lst = self._sse.get(match_id) or []
        if q in lst:
            lst.remove(q)

    def _broadcast(self, match_id: str, event: dict[str, Any]) -> None:
        for q in list(self._sse.get(match_id) or []):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # 队列满：丢最旧事件腾位，保最新（避免观赛画面卡在最旧处）
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def _find_contest_pairing(self, contest_id: int, match_id: str) -> dict | None:
        """P1：按 contest_id + match_id 定位 contest_pairing 行（读冻结 version_id 用）。"""
        for p in self.store.list_contest_pairings(contest_id):
            if p.get("match_id") == match_id:
                return p
        return None

    async def _run_match(self, match_id: str) -> None:
        async with self._sem:
            m = self.store.get_match(match_id)
            if not m:
                return
            bot_a = self.store.get_bot(m["bot_a_id"])
            bot_b = self.store.get_bot(m["bot_b_id"])
            # P1：赛事对局读冻结的 bot 版本路径（pairing.bot_a_version_id → bot_versions.binary_path），
            # 不受选手中途上传新版本影响；非 contest 读 bots.binary_path（最新）。
            path_a = bot_a["binary_path"]
            path_b = bot_b["binary_path"]
            if m.get("match_type") == TYPE_CONTEST and m.get("contest_id"):
                pairing = self._find_contest_pairing(m["contest_id"], match_id)
                if pairing:
                    if pairing.get("bot_a_version_id"):
                        v = self.store.get_bot_version(pairing["bot_a_version_id"])
                        if v and v.get("binary_path"):
                            path_a = v["binary_path"]
                    if pairing.get("bot_b_version_id"):
                        v = self.store.get_bot_version(pairing["bot_b_version_id"])
                        if v and v.get("binary_path"):
                            path_b = v["binary_path"]
            gid = normalize_game_id(m.get("game_id") or bot_a.get("game_id"))
            logger.info(
                "match start id=%s game=%s type=%s a=%s(%s) b=%s(%s)",
                match_id, gid, m.get("match_type"),
                m["bot_a_id"], bot_a.get("name"), m["bot_b_id"], bot_b.get("name"),
            )
            self.store.update_match(match_id, status=STATUS_RUNNING, started_at=_now())
            events: list[dict] = []

            def on_event(kind: str, ev: dict) -> None:
                events.append(ev)
                self._broadcast(match_id, ev)
                if kind in ("settle", "hand_start", "match_end", "move", "match_start") or len(events) % 5 == 0:
                    self.store.upsert_replay(match_id, json.dumps(events, ensure_ascii=False), "[]")

            try:
                jp = self._judge_params(gid)
                # num_hands：admin 全局设置（SETTING_JUDGE_HOLDEM_HANDS）优先，未设回退对局级 total_hands
                num_hands = jp.get("num_hands") or int(m["total_hands"])
                result = await self.runner.run_binaries(
                    path_a,
                    path_b,
                    game_id=gid,
                    num_hands=num_hands,
                    n_dots=m.get("n_dots"),
                    board_size=jp.get("board_size"),
                    starting_stack=jp.get("starting_stack"),
                    sb=jp.get("sb"),
                    bb=jp.get("bb"),
                    on_event=on_event,
                )
                ea = sum(r.deltas[0] for r in result.rounds)
                eb = sum(r.deltas[1] for r in result.rounds)
                # winner：引擎 result.winner 已权威化（棋类单轮胜者；holdem 多手按累计净筹码比较）。
                # 仅当 result.winner 为 None（平局）时按 ea/eb 兜底——二者一致时返 None（平局）。
                winner: int | None = result.winner
                if winner is None:
                    winner = 0 if ea > eb else 1 if eb > ea else None
                net_bb_a = game_registry.get(gid).normalize_earnings(ea)
                self.store.update_match(
                    match_id,
                    status=STATUS_COMPLETED,
                    hands_played=result.rounds_played,
                    earnings_a=ea,
                    earnings_b=eb,
                    winner=winner,
                    reason="completed",
                    net_bb_a=net_bb_a,
                    ended_at=_now(),
                )
                self.store.upsert_replay(
                    match_id, json.dumps(events, ensure_ascii=False), "[]"
                )
                # 比赛对局只计入赛事内积分，不更新全局 Glicko-2 排行榜
                # （挑战 / table / ladder 对局均更新全局评分）
                if m["match_type"] != TYPE_CONTEST:
                    self._apply_ratings(m["bot_a_id"], m["bot_b_id"], winner, ea, eb)
                self._broadcast(match_id, {"type": "match_end", "winner": winner, "earnings_a": ea, "earnings_b": eb})
                logger.info(
                    "match done id=%s winner=%s rounds=%s ea=%s eb=%s rated=%s",
                    match_id, winner, result.rounds_played, ea, eb,
                    m["match_type"] != TYPE_CONTEST,
                )
                # 通知双方 owner（仅 challenge/table/ladder；contest 内部对局不单独通知）
                if self.notifier is not None and m["match_type"] != TYPE_CONTEST:
                    try:
                        wl = "平局" if winner is None else f"座位 {winner} 胜"
                        self.notifier.notify_both_owners(
                            m["bot_a_id"], m["bot_b_id"],
                            type="match_done",
                            title=f"对局完成：{wl}",
                            body=f"对局 {match_id} 已结束（{m.get('game_id', '')}）。",
                            link=f"/match/{match_id}",
                        )
                    except Exception:
                        logger.debug("notify match_done failed", exc_info=True)
                # 经验奖励：双方 owner 各加 XP（参与 + 胜者额外），仅非 contest
                if m["match_type"] != TYPE_CONTEST:
                    try:
                        from bzplat.backend.store.schema import (
                            XP_MATCH_PARTICIPATE, XP_MATCH_WIN,
                        )
                        ba = self.store.get_bot(m["bot_a_id"])
                        bb = self.store.get_bot(m["bot_b_id"])
                        for bot, won in ((ba, winner == 0), (bb, winner == 1)):
                            if bot and bot.get("owner_id"):
                                xp = XP_MATCH_PARTICIPATE + (XP_MATCH_WIN if won else 0)
                                self.store.award_xp(int(bot["owner_id"]), xp)
                    except Exception:
                        logger.debug("award_xp failed", exc_info=True)
            except BotCrashedError as exc:
                logger.warning("match %s bot crashed — %s", match_id, exc)
                # P4：赛事对局崩溃 → 技术判负（completed + winner=对手 + technical_loss=1），
                # 不再静默吞分（aborted 在 standings 不计分会导致赛事卡住/丢分）。
                # 无法判定崩溃方时（start_session 阶段），非 contest 仍 aborted（保旧）。
                if m.get("match_type") == TYPE_CONTEST:
                    self.store.update_match(
                        match_id, status=STATUS_COMPLETED, reason="technical_loss",
                        winner=1, earnings_a=-1, earnings_b=1,  # 兜底判 bot_a 崩溃（seat0 输）
                        technical_loss=1, ended_at=_now(),
                    )
                    self._broadcast(match_id, {"type": "match_end", "winner": 1, "reason": "technical_loss"})
                else:
                    self.store.update_match(
                        match_id, status=STATUS_ABORTED, reason="bot_crashed", ended_at=_now(),
                    )
                    self._broadcast(match_id, {"type": "error", "message": "Bot 启动失败或已崩溃，对局已中止"})
            except Exception as exc:
                logger.exception("match %s failed", match_id)
                self.store.update_match(
                    match_id,
                    status=STATUS_ABORTED,
                    reason=f"error:{exc}",
                    ended_at=_now(),
                )
                self._broadcast(match_id, {"type": "error", "message": str(exc)})
            finally:
                self._tasks.pop(match_id, None)
                if self.on_match_done is not None:
                    try:
                        result = self.on_match_done(match_id, m.get("contest_id"))
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception:
                        logger.debug("on_match_done failed", exc_info=True)

    async def _run_human_match(self, match_id: str) -> None:
        """人类 vs bot 对局：独立信号量；人类侧经 _human_turns Future 等待 WS 落子。"""
        async with self._human_sem:
            m = self.store.get_match(match_id)
            if not m:
                return
            bot = self.store.get_bot(m["bot_a_id"])
            gid = normalize_game_id(m.get("game_id") or bot.get("game_id"))
            human_seat = int(m["human_seat"]) if m.get("human_seat") is not None else 1
            self.store.update_match(match_id, status=STATUS_RUNNING, started_at=_now())
            events: list[dict] = []
            consecutive_timeouts = {"n": 0}  # 闭包内可变计数器

            def on_event(kind: str, ev: dict) -> None:
                events.append(ev)
                self._broadcast(match_id, ev)
                if kind in ("settle", "hand_start", "match_end", "move", "match_start", "turn") or len(events) % 5 == 0:
                    self.store.upsert_replay(match_id, json.dumps(events, ensure_ascii=False), "[]")
                # 注：your_turn 不经 on_event，由 human_decide 直接 append + 立即落库（见下）

            async def human_decide(player_idx: int, request: dict) -> dict:
                # 注册 pending 回合，广播 your_turn，等待 WS /move 解析 Future
                fut: asyncio.Future = asyncio.get_running_loop().create_future()
                self._human_turns[(match_id, player_idx)] = {
                    "request": request,
                    "future": fut,
                    "ts": _now(),
                }
                yt = {"type": "your_turn", "player": player_idx, "request": request}
                events.append(yt)               # 进入持久化事件流（前端可恢复）
                # 立即落库：前端重连走 subscribe() → get_replay() 读快照，必须能看到 your_turn
                self.store.upsert_replay(match_id, json.dumps(events, ensure_ascii=False), "[]")
                self._broadcast(match_id, yt)   # 实时推送（已连接的 WS 立即点亮）
                try:
                    resp = await asyncio.wait_for(fut, timeout=self.human_action_timeout)
                    consecutive_timeouts["n"] = 0  # 人类响应 → 清零
                    return resp
                except asyncio.TimeoutError:
                    consecutive_timeouts["n"] += 1
                    # 连续多次不响应 → 视为挂机，中止对局（避免 70 手死磕占用人类槽）
                    if consecutive_timeouts["n"] >= self.human_max_consecutive_timeouts:
                        raise HumanInactive(
                            f"human seat {player_idx} inactive: {consecutive_timeouts['n']} consecutive timeouts"
                        )
                    return _fail_response(gid)
                finally:
                    self._human_turns.pop((match_id, player_idx), None)

            try:
                jp = self._judge_params(gid)
                # num_hands：admin 全局设置（SETTING_JUDGE_HOLDEM_HANDS）优先，未设回退对局级 total_hands
                num_hands = jp.get("num_hands") or int(m["total_hands"])
                result = await self.runner.run_bot_vs_human(
                    bot["binary_path"],
                    bot_seat=1 - human_seat,
                    human_decide=human_decide,
                    game_id=gid,
                    num_hands=num_hands,
                    n_dots=m.get("n_dots"),
                    on_event=on_event,
                    board_size=jp.get("board_size"),
                    starting_stack=jp.get("starting_stack"),
                    sb=jp.get("sb"),
                    bb=jp.get("bb"),
                )
                ea = sum(r.deltas[0] for r in result.rounds)
                eb = sum(r.deltas[1] for r in result.rounds)
                # winner：引擎 result.winner 已权威化（见 _run_match 同款逻辑）
                winner = result.winner
                if winner is None:
                    winner = 0 if ea > eb else 1 if eb > ea else None
                net_bb_a = game_registry.get(gid).normalize_earnings(ea)
                self.store.update_match(
                    match_id, status=STATUS_COMPLETED,
                    hands_played=result.rounds_played, earnings_a=ea, earnings_b=eb,
                    winner=winner, reason="completed", net_bb_a=net_bb_a, ended_at=_now(),
                )
                self.store.upsert_replay(match_id, json.dumps(events, ensure_ascii=False), "[]")
                # 人类对战不计 Glicko-2（人类无 rating 行）
                self._broadcast(match_id, {"type": "match_end", "winner": winner, "earnings_a": ea, "earnings_b": eb})
            except BotCrashedError as exc:
                # Bot 启动即崩/EOF——快速 abort，广播清晰错误（而非吞成默认动作死磕数小时）
                logger.warning("human match %s aborted: bot crashed — %s", match_id, exc)
                self.store.update_match(match_id, status=STATUS_ABORTED, reason="bot_crashed", ended_at=_now())
                self.store.upsert_replay(match_id, json.dumps(events, ensure_ascii=False), "[]")
                self._broadcast(match_id, {"type": "error", "message": "Bot 启动失败或已崩溃，对局已中止"})
            except HumanInactive as exc:
                # 人类连续超时不响应 → 中止对局，释放人类槽（避免死磕占用 + 锁死用户）
                logger.warning("human match %s aborted: human inactive — %s", match_id, exc)
                self.store.update_match(
                    match_id, status=STATUS_ABORTED, reason="human_inactive", ended_at=_now(),
                )
                self.store.upsert_replay(match_id, json.dumps(events, ensure_ascii=False), "[]")
                self._broadcast(match_id, {"type": "error", "message": "你长时间未响应，对局已中止"})
            except Exception as exc:
                logger.exception("human match %s failed", match_id)
                self.store.update_match(match_id, status=STATUS_ABORTED, reason=f"error:{exc}", ended_at=_now())
                self._broadcast(match_id, {"type": "error", "message": str(exc)})
            finally:
                self._tasks.pop(match_id, None)
                self._human_turns = {k: v for k, v in self._human_turns.items() if k[0] != match_id}
                if m.get("human_user_id") is not None:
                    self._human_active_users.discard(int(m["human_user_id"]))

    def _judge_params(self, gid: str) -> dict[str, int | None]:
        """从 platform_settings 读裁判规则参数（热生效）；缺失或非法时用 spec 默认兜底。

        经 games 注册表取该游戏的 judge_params 声明（消除 if game_id）。
        返回 {field: value}，field 对应 run_session 的 kwarg（如 starting_stack/board_size），
        value=None 表示用引擎默认。n_dots 不在此处（走 match 列）；num_hands 走 match.total_hands。
        """

        def _int(key: str, default: int) -> int | None:
            raw = self.store.get_setting(key)
            if raw is None or raw == "":
                return None
            try:
                v = int(raw)
            except (TypeError, ValueError):
                return None
            return v if v > 0 else None

        out: dict[str, int | None] = {}
        for p in game_registry.get(gid).judge_params:
            out[p.field] = _int(p.setting_key, p.default)
        return out

    def _apply_ratings(
        self, bot_a_id: int, bot_b_id: int, winner: int | None, ea: int, eb: int
    ) -> None:
        self.store.ensure_rating(bot_a_id)
        self.store.ensure_rating(bot_b_id)
        ra = self.store.get_rating(bot_a_id)
        rb = self.store.get_rating(bot_b_id)
        sa, sb = match_scores(winner)
        ra_new = update_rating(
            Rating(ra["rating"], ra["rd"], ra["vol"]),
            [(Rating(rb["rating"], rb["rd"], rb["vol"]), sa)],
        )
        rb_new = update_rating(
            Rating(rb["rating"], rb["rd"], rb["vol"]),
            [(Rating(ra["rating"], ra["rd"], ra["vol"]), sb)],
        )
        wa = int(winner == 0)
        la = int(winner == 1)
        da = int(winner is None)
        wb, lb, db = la, wa, da
        self.store.update_rating_row(
            bot_a_id,
            rating=ra_new.mu, rd=ra_new.phi, vol=ra_new.sigma,
            wins=ra["wins"] + wa, losses=ra["losses"] + la, draws=ra["draws"] + da,
            net_chips=ra["net_chips"] + ea,
            matches_played=ra["matches_played"] + 1,
            last_played_at=_now(),
        )
        self.store.update_rating_row(
            bot_b_id,
            rating=rb_new.mu, rd=rb_new.phi, vol=rb_new.sigma,
            wins=rb["wins"] + wb, losses=rb["losses"] + lb, draws=rb["draws"] + db,
            net_chips=rb["net_chips"] + eb,
            matches_played=rb["matches_played"] + 1,
            last_played_at=_now(),
        )
        # 记录评分历史（段位趋势/曲线用）
        self.store.add_rating_history(
            bot_a_id, ra_new.mu, ra_new.phi, ra_new.sigma,
            ra["matches_played"] + 1,
        )
        self.store.add_rating_history(
            bot_b_id, rb_new.mu, rb_new.phi, rb_new.sigma,
            rb["matches_played"] + 1,
        )
        # 累积对战胜负（pair_stats，规范化为小 id 在前）
        lo, hi = sorted((bot_a_id, bot_b_id))
        if bot_a_id == lo:
            aw, al, dd = wa, la, da
        else:
            aw, al, dd = wb, lb, db
        self.store.upsert_pair_stats(
            lo, hi, 0.0, None, None, 0,
            a_wins_delta=aw, a_losses_delta=al, draws_delta=dd,
        )
