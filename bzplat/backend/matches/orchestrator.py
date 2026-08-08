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
    DEFAULT_RUNTIME_MODE,
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


def _bot_decide_error_summary(events: list[dict]) -> dict:
    """从对局 events 统计 bot_decide_error（Bot 响应格式错误等）。

    返回 ``{"bot_decide_errors": {0: N, 1: M}, "bot_decide_error_samples": [...]}``，
    供前端 Bot 详情/对局记录展示给 Bot 作者调试（"你的 Bot 第 N 手响应缺 response 字段"）。
    无错误时返回空 dict（不污染 result）。
    """
    errors = [e for e in events if e.get("type") == "bot_decide_error"]
    if not errors:
        return {}
    counts: dict[int, int] = {0: 0, 1: 0}
    samples: list[dict] = []
    for e in errors:
        seat = int(e.get("seat", -1))
        if seat in counts:
            counts[seat] += 1
        if len(samples) < 3:  # 最多存 3 条样本错误（防爆 result JSON）
            samples.append({"seat": seat, "error": e.get("error", ""), "turn": e.get("turn")})
    return {"bot_decide_errors": counts, "bot_decide_error_samples": samples}


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
        # 实际占用 bot 对局槽（已 acquire _sem）的任务数。区别于 _tasks（含等信号量的）。
        # auto_matcher._is_idle 据此判定空闲，避免大量 pending 任务排队等槽时误判不空闲。
        self._bot_running = 0
        self._sse: dict[str, list[asyncio.Queue]] = {}
        self._lock = asyncio.Lock()
        # 评分串行化锁：按 (bot_id, game_id) 维度串行化 _apply_ratings，防同 bot 两场
        # 并发完成时快照读+绝对写 rating/rd/vol 互相覆盖（lost-update，审计 FRAGILE 5a）。
        self._rating_locks: dict[tuple[int, str], asyncio.Lock] = {}
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
        self._bot_running = 0  # 重置（重建后当前运行任务的计数由 _run_match 维护）

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
        # done() 检查与 set_result 非原子——并发 WS 消息或超时可能在此间隙已解析，
        # 第二个 set_result 会抛 InvalidStateError→500。捕获视为该消息未生效。
        try:
            entry["future"].set_result(move)
        except asyncio.InvalidStateError:
            return False
        return True

    async def challenge_human(
        self,
        bot_id: int,
        human_user_id: int,
        *,
        human_seat: int = 1,
        game_id: str | None = None,
        match_config: dict[str, Any] | None = None,
    ) -> str:
        """人类 vs bot：human_seat 为人类坐位（0/1），另一侧为 bot_id。

        人类侧无 bot/binary，走 runner.run_bot_vs_human（人类 decide 经 Future
        等待 WS 回传）。不计 Glicko；占用独立 _human_sem（不占 bot 对局槽）。

        match_config：对局级配置（如 {"hands":70}/{"n_dots":6}），None 用 spec 默认。
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

        # 游戏规则参数已由 GameSpec 钉死，match_config 不再承载 hands/n_dots。
        spec = game_registry.get(gid)
        mc: dict[str, Any] = {}

        bot_seat = 1 - human_seat
        # 人类侧用一个伪 bot_id 占位（取 bot_id 自身，仅满足 NOT NULL FK；
        # 真正的人类动作经 _human_turns / WS 回传，不走 binary）
        bot_a_id = bot_id if bot_seat == 0 else bot_id
        bot_b_id = bot_id if bot_seat == 1 else bot_id
        match_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
        self.store.create_match(
            match_id,
            bot_a_id=bot_a_id,
            bot_b_id=bot_b_id,
            owner_id=human_user_id,
            match_type=TYPE_HUMAN,
            game_id=gid,
            match_config=mc,
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
        match_type: str = TYPE_CHALLENGE,
        contest_id: int | None = None,
        game_id: str | None = None,
        match_config: dict[str, Any] | None = None,
        bot_a_version_id: int | None = None,
        bot_b_version_id: int | None = None,
        duplicate: bool = False,
        duplicate_seed: int | None = None,
    ) -> str:
        # 自博弈（同 bot 对战）：允许——用于对比同 bot 的不同版本（如 v1 vs v2），
        # 或同 bot 同版本的对阵。仅 challenge 路径放开（contest 仍各自走 pairing）。
        bot_a = self.store.get_bot(challenger_bot_id)
        bot_b = self.store.get_bot(opponent_bot_id)
        if not bot_a or not bot_b:
            raise ValueError("bot 不存在")
        if not bot_a.get("is_active") or not bot_a.get("binary_path"):
            raise ValueError("座位0 bot 不可用")
        if not bot_b.get("is_active") or not bot_b.get("binary_path"):
            raise ValueError("座位1 bot 不可用")
        # 自博弈同 bot 同版本时，座位区分（seat 0/1）即可，不阻拦。
        # 但校验指定的版本确实属于对应 bot。
        if bot_a_version_id is not None:
            va = self.store.get_bot_version(bot_a_version_id)
            if not va or va.get("bot_id") != challenger_bot_id:
                raise ValueError("座位0 指定的版本不存在或不属于该 bot")
        if bot_b_version_id is not None:
            vb = self.store.get_bot_version(bot_b_version_id)
            if not vb or vb.get("bot_id") != opponent_bot_id:
                raise ValueError("座位1 指定的版本不存在或不属于该 bot")

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

        # 游戏规则参数（手数/棋盘/点阵）已由 GameSpec 钉死固定值，不再走 match_config。
        # match_config 仅保留版本快照等内部键（_run_match 读 _bot_a/b_version_id 解析版本路径）。
        # P2 residual：duplicate=True 时把标志 + seed 落 match_config，
        # __run_match_inner 据此走 run_duplicate（2 leg 合并），并落 match_seed 供回放。
        spec = game_registry.get(gid)
        if duplicate and spec.build_match_plan is None:
            # 游戏（棋类）不支持 duplicate：降级为单 leg（不抛错，保持容错）。
            duplicate = False
        mc: dict[str, Any] = {}
        if bot_a_version_id is not None:
            mc["_bot_a_version_id"] = int(bot_a_version_id)
        if bot_b_version_id is not None:
            mc["_bot_b_version_id"] = int(bot_b_version_id)
        if duplicate:
            mc["duplicate"] = True
            if duplicate_seed is not None:
                mc["duplicate_seed"] = int(duplicate_seed)

        match_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
        self.store.create_match(
            match_id,
            bot_a_id=challenger_bot_id,
            bot_b_id=opponent_bot_id,
            owner_id=owner_user_id,
            contest_id=contest_id,
            match_type=match_type,
            game_id=gid,
            match_config=mc,
        )
        # duplicate 落 match_seed（确定性回放/复现用）
        if duplicate and duplicate_seed is not None:
            self.store.update_match(match_id, match_seed=int(duplicate_seed))
        self.store.upsert_replay(match_id, "[]", "[]")
        task = asyncio.create_task(self._run_match(match_id), name=f"match-{match_id}")
        self._tasks[match_id] = task
        return match_id

    async def challenge_duplicate(
        self,
        challenger_bot_id: int,
        opponent_bot_id: int,
        owner_user_id: int | None,
        *,
        match_type: str = TYPE_CHALLENGE,
        contest_id: int | None = None,
        game_id: str | None = None,
        match_config: dict[str, Any] | None = None,
        bot_a_version_id: int | None = None,
        bot_b_version_id: int | None = None,
        duplicate_seed: int | None = None,
    ) -> str:
        """复式赛制（duplicate）对局：跑 2 leg（同副牌交换座位）合并 net 判胜负。

        签名与 challenge 一致，区别仅在于 match_config 标 duplicate=True。
        内部走 runner.run_duplicate（每 leg 同 deal_sequence，seat_swap 翻转 deltas
        累加到物理 bot）。游戏不支持 duplicate（spec.build_match_plan is None）时
        自动降级为单 leg（challenge 内部兜底，不抛错）。

        match 行落 1 条 merged result（deltas=2 leg 累加、winner 按 merged net 判），
        供 standings/scoring 读取（与单 leg result 鸭子契约一致：result.deltas）。
        """
        return await self.challenge(
            challenger_bot_id,
            opponent_bot_id,
            owner_user_id,
            match_type=match_type,
            contest_id=contest_id,
            game_id=game_id,
            match_config=match_config,
            bot_a_version_id=bot_a_version_id,
            bot_b_version_id=bot_b_version_id,
            duplicate=True,
            duplicate_seed=duplicate_seed,
        )

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
            self._bot_running += 1  # 占用槽位（供 auto_matcher._is_idle 准确判定）
            try:
                return await self.__run_match_inner(match_id)
            finally:
                self._bot_running = max(0, self._bot_running - 1)

    async def __run_match_inner(self, match_id: str) -> None:
        m = self.store.get_match(match_id)
        if not m:
            return
        bot_a = self.store.get_bot(m["bot_a_id"])
        bot_b = self.store.get_bot(m["bot_b_id"])
        # P1：赛事对局读冻结的 bot 版本路径（pairing.bot_a_version_id → bot_versions.binary_path），
        # 不受选手中途上传新版本影响；非 contest 读 bots.binary_path（最新）。
        # runtime_mode 同源：冻结版本优先读 bot_versions.runtime_mode，否则读 bots.runtime_mode。
        path_a = bot_a["binary_path"]
        path_b = bot_b["binary_path"]
        mode_a = bot_a.get("runtime_mode") or DEFAULT_RUNTIME_MODE
        mode_b = bot_b.get("runtime_mode") or DEFAULT_RUNTIME_MODE
        if m.get("match_type") == TYPE_CONTEST and m.get("contest_id"):
            pairing = self._find_contest_pairing(m["contest_id"], match_id)
            if pairing:
                if pairing.get("bot_a_version_id"):
                    v = self.store.get_bot_version(pairing["bot_a_version_id"])
                    if v and v.get("binary_path"):
                        path_a = v["binary_path"]
                        mode_a = v.get("runtime_mode") or mode_a
                if pairing.get("bot_b_version_id"):
                    v = self.store.get_bot_version(pairing["bot_b_version_id"])
                    if v and v.get("binary_path"):
                        path_b = v["binary_path"]
                        mode_b = v.get("runtime_mode") or mode_b
        else:
            # challenge/table/ladder：match_config 可能带版本快照（_bot_a/b_version_id，
            # 由挑战页版本选择注入）。读指定版本的 binary_path，不受中途上传新版本影响。
            mc = m.get("match_config") or {}
            if isinstance(mc, str):
                try:
                    mc = json.loads(mc)
                except Exception:
                    mc = {}
            if mc.get("_bot_a_version_id"):
                v = self.store.get_bot_version(mc["_bot_a_version_id"])
                if v and v.get("binary_path"):
                    path_a = v["binary_path"]
                    mode_a = v.get("runtime_mode") or mode_a
            if mc.get("_bot_b_version_id"):
                v = self.store.get_bot_version(mc["_bot_b_version_id"])
                if v and v.get("binary_path"):
                    path_b = v["binary_path"]
                    mode_b = v.get("runtime_mode") or mode_b
        gid = normalize_game_id(m.get("game_id") or bot_a.get("game_id"))
        # P2 residual：复式赛制（duplicate）——match_config.duplicate=True 且游戏 spec
        # 支持 build_match_plan（仅 holdem）时，走 run_duplicate（2 leg 同副牌交换座位，
        # 合并 net 判胜负）。spec 不支持时退化为单 leg（runner.run_duplicate 内部兜底）。
        stored_mc = m.get("match_config") or {}
        if isinstance(stored_mc, str):
            try:
                stored_mc = json.loads(stored_mc)
            except Exception:
                stored_mc = {}
        spec = game_registry.get(gid)
        want_duplicate = bool(stored_mc.get("duplicate")) and spec.build_match_plan is not None
        # duplicate 用确定性 seed（落库供回放/复现；单 leg 不强制 seed，沿用随机）。
        dup_seed = int(stored_mc.get("duplicate_seed")) if stored_mc.get("duplicate_seed") is not None else None
        logger.info(
            "match start id=%s game=%s type=%s a=%s(%s) b=%s(%s) duplicate=%s",
            match_id, gid, m.get("match_type"),
            m["bot_a_id"], bot_a.get("name"), m["bot_b_id"], bot_b.get("name"),
            want_duplicate,
        )
        self.store.update_match(match_id, status=STATUS_RUNNING, started_at=_now())
        events: list[dict] = []

        def on_event(kind: str, ev: dict) -> None:
            events.append(ev)
            self._broadcast(match_id, ev)
            if kind in ("settle", "hand_start", "match_end", "move", "match_start") or len(events) % 5 == 0:
                self.store.upsert_replay(match_id, json.dumps(events, ensure_ascii=False), "[]")

        try:
            # 游戏规则参数（手数/棋盘/点阵）已由 GameSpec 钉死，不再从 match_config 读取。
            # 此处仅注入 admin judge_params（holdem 的 starting_stack/sb/bb）给 runner。
            # duplicate=True 时透传该标志 + seed 给 runner（build_match_plan 据此生成 2 leg）。
            mc: dict[str, Any] = {
                k: v for k, v in self._judge_params(gid).items() if v is not None
            }
            if want_duplicate:
                mc["duplicate"] = True
                result = await self.runner.run_duplicate(
                    path_a,
                    path_b,
                    game_id=gid,
                    on_event=on_event,
                    seed=dup_seed,
                    runtime_modes=(mode_a, mode_b),
                    **mc,
                )
            else:
                result = await self.runner.run_binaries(
                    path_a,
                    path_b,
                    game_id=gid,
                    on_event=on_event,
                    runtime_modes=(mode_a, mode_b),
                    **mc,
                )
            # duplicate：每 leg 独立判胜负（result.legs），不把净筹码合并判 1 场。
            # 胜负完全由 standings/ranking 读 result.legs 决定；match.winner 留 None。
            if want_duplicate:
                winner = None  # 胜负由 standings 读 result.legs 决定（无单一 match 胜者）
                legs_data = getattr(result, "legs", None) or []
                # net_chips tiebreak 用：两 leg 物理 deltas 累加
                ea = sum(int(lg.get("deltas", [0, 0])[0]) for lg in legs_data) if legs_data else 0
                eb = sum(int(lg.get("deltas", [0, 0])[1]) for lg in legs_data) if legs_data else 0
                self.store.update_match(
                    match_id,
                    status=STATUS_COMPLETED,
                    winner=None,  # 胜负由 standings 读 result.legs 决定
                    reason="completed",
                    result={
                        "hands_played": result.rounds_played,
                        "deltas": [ea, eb],  # 两 leg 累加（net_chips tiebreak）
                        "legs": legs_data,   # 每 leg 独立 winner/deltas（物理 A/B 视角）
                        "net_bb": spec.normalize_earnings(ea),
                        **_bot_decide_error_summary(events),
                    },
                    ended_at=_now(),
                )
            else:
                ea = sum(r.deltas[0] for r in result.rounds)
                eb = sum(r.deltas[1] for r in result.rounds)
                # winner：引擎 result.winner 已权威化（棋类单轮胜者；holdem 多手按累计净筹码比较）。
                # 仅当 result.winner 为 None（平局）时按 ea/eb 兜底——二者一致时返 None（平局）。
                winner: int | None = result.winner
                if winner is None:
                    winner = 0 if ea > eb else 1 if eb > ea else None
                self.store.update_match(
                    match_id,
                    status=STATUS_COMPLETED,
                    winner=winner,
                    reason="completed",
                    result={
                        "hands_played": result.rounds_played,
                        "deltas": [ea, eb],
                        "net_bb": spec.normalize_earnings(ea),
                        **_bot_decide_error_summary(events),
                    },
                    ended_at=_now(),
                )
            self.store.upsert_replay(
                match_id, json.dumps(events, ensure_ascii=False), "[]"
            )
            # 比赛对局只计入赛事内积分，不更新全局 Glicko-2 排行榜
            # （挑战 / table / ladder 对局均更新全局评分）
            if m["match_type"] != TYPE_CONTEST:
                # 串行化同 bot 的评分更新（按 bot_id 排序获取锁，防死锁）：
                # 同 bot 两场并发完成时，快照读+绝对写 rating/rd/vol 会互相覆盖（lost-update）。
                gid = m.get("game_id") or "holdem"
                async with self._rating_lock_for(m["bot_a_id"], gid):
                    if m["bot_a_id"] != m["bot_b_id"]:
                        async with self._rating_lock_for(m["bot_b_id"], gid):
                            self._apply_ratings(m["bot_a_id"], m["bot_b_id"], winner, ea, eb)
                    else:
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
            # 赛事对局崩溃 → 技术判负（completed + winner=对手 + technical_loss=1），
            # 不再静默吞分（aborted 在 standings 不计分会导致赛事卡住/丢分）。
            # 崩溃方从 exc.crashed_seat 取（runner 在 start_session 失败时注解）；
            # 未注解（游戏内崩溃已由引擎处理产出正常 result，不会到这；bot_a start
            # 失败未注解→默认 0）。
            crashed_seat = getattr(exc, "crashed_seat", None) or 0
            winner = 1 - crashed_seat
            ea, eb = (-1, 1) if crashed_seat == 0 else (1, -1)
            if m.get("match_type") == TYPE_CONTEST:
                self.store.update_match(
                    match_id, status=STATUS_COMPLETED, reason="technical_loss",
                    winner=winner,
                    result={"deltas": [ea, eb]},
                    technical_loss=1, ended_at=_now(),
                )
                self._broadcast(match_id, {"type": "match_end", "winner": winner, "reason": "technical_loss"})
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
                    # 对局完成回调（赛事 maybe_finish）失败必须可见——
                    # 原用 debug 级会静默吞掉，导致赛事卡 running 无从排查。
                    logger.exception("on_match_done failed match=%s", match_id)

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
                # 游戏规则参数已由 GameSpec 钉死，仅注入 admin judge_params（同 _run_match）。
                spec = game_registry.get(gid)
                mc: dict[str, Any] = {
                    k: v for k, v in self._judge_params(gid).items() if v is not None
                }
                result = await self.runner.run_bot_vs_human(
                    bot["binary_path"],
                    bot_seat=1 - human_seat,
                    human_decide=human_decide,
                    game_id=gid,
                    on_event=on_event,
                    runtime_mode=bot.get("runtime_mode") or DEFAULT_RUNTIME_MODE,
                    **mc,
                )
                ea = sum(r.deltas[0] for r in result.rounds)
                eb = sum(r.deltas[1] for r in result.rounds)
                # winner：引擎 result.winner 已权威化（见 _run_match 同款逻辑）
                winner = result.winner
                if winner is None:
                    winner = 0 if ea > eb else 1 if eb > ea else None
                self.store.update_match(
                    match_id, status=STATUS_COMPLETED,
                    winner=winner, reason="completed",
                    result={
                        "hands_played": result.rounds_played,
                        "deltas": [ea, eb],
                        "net_bb": spec.normalize_earnings(ea),
                    },
                    ended_at=_now(),
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
        返回 {field: value}，field 对应 run_session 的 kwarg（如 holdem 的
        starting_stack/sb/bb），value=None 表示用引擎默认。
        游戏规则参数（手数/棋盘/点阵）已钉死，不在 judge_params 声明，故不在此返回。
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

    def _rating_lock_for(self, bot_id: int, game_id: str) -> asyncio.Lock:
        """获取/创建某 (bot, game) 的评分串行化锁（防同 bot 并发评分 lost-update）。"""
        key = (bot_id, game_id)
        lock = self._rating_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._rating_locks[key] = lock
        return lock

    def _apply_ratings(
        self, bot_a_id: int, bot_b_id: int, winner: int | None, ea: int, eb: int
    ) -> None:
        # 自博弈（同 bot 对战）：不计 Glicko 评分——同 bot 评分无信息量，且 update_rating_row
        # 同一行被写两次（ra/rb 是同一快照），第二次覆盖第一次，导致胜负/评分错乱。
        # 自博弈仅作功能验证/版本对比，不进天梯。同 contest（match_type=contest）也不会进这里。
        if bot_a_id == bot_b_id:
            logger.info("self-play match %s vs %s: skip rating update", bot_a_id, bot_b_id)
            return
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
            wins=wa, losses=la, draws=da,  # 传增量——update_rating_row 原子累加（防 lost-update，审计 P1）
            net_chips=ea,
            matches_played=1,
            last_played_at=_now(),
        )
        self.store.update_rating_row(
            bot_b_id,
            rating=rb_new.mu, rd=rb_new.phi, vol=rb_new.sigma,
            wins=wb, losses=lb, draws=db,
            net_chips=eb,
            matches_played=1,
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
