"""对局编排：challenge 入队、评分更新、SSE 扇出。"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from datetime import datetime
from typing import Any, Callable

from bzplat.backend.engine.game import (
    BIG_BLIND,
    DEFAULT_HANDS,
    SMALL_BLIND,
    STARTING_STACK,
)
from bzplat.backend.engine.gomoku import BOARD_SIZE
from bzplat.backend.engine.registry import (
    GAME_HOLDEM,
    normalize_game_id,
)
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.rating.glicko2 import Rating, match_scores, update_rating
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    REGISTERED_ENGINES,
    SETTING_JUDGE_GOMOKU_SIZE,
    SETTING_JUDGE_HOLDEM_BB,
    SETTING_JUDGE_HOLDEM_SB,
    SETTING_JUDGE_HOLDEM_STACK,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TYPE_CHALLENGE,
    TYPE_CONTEST,
    TYPE_TABLE,
    VALID_GAME_IDS,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


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

    def rebuild_concurrency(self, max_concurrent: int) -> None:
        """热更新并发上限：重建 Semaphore（不修改 _value）。"""
        self.max_concurrent = max(1, int(max_concurrent))
        self._sem = asyncio.Semaphore(self.max_concurrent)

    def set_action_timeout(self, timeout_sec: float) -> None:
        self.runner.action_timeout = float(timeout_sec)

    async def challenge(
        self,
        challenger_bot_id: int,
        opponent_bot_id: int,
        owner_user_id: int | None,
        *,
        hands: int = DEFAULT_HANDS,
        match_type: str = TYPE_CHALLENGE,
        contest_id: int | None = None,
        game_id: str | None = None,
        n_dots: int | None = None,
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
        if not bot_b.get("is_public") and bot_b.get("owner_id") != owner_user_id:
            raise ValueError("对手 bot 未公开")

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

        # 棋类单局；扑克沿用 hands
        total_hands = hands if gid == GAME_HOLDEM else 1

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
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        self._sse.setdefault(match_id, []).append(q)
        m = self.store.get_match(match_id)
        replay = self.store.get_replay(match_id) or {}
        q.put_nowait({"type": "snapshot", "match": m, "events": json.loads(replay.get("events_json") or "[]")})
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
                pass

    async def _run_match(self, match_id: str) -> None:
        async with self._sem:
            m = self.store.get_match(match_id)
            if not m:
                return
            bot_a = self.store.get_bot(m["bot_a_id"])
            bot_b = self.store.get_bot(m["bot_b_id"])
            gid = normalize_game_id(m.get("game_id") or bot_a.get("game_id"))
            self.store.update_match(match_id, status=STATUS_RUNNING, started_at=_now())
            events: list[dict] = []

            def on_event(kind: str, ev: dict) -> None:
                events.append(ev)
                self._broadcast(match_id, ev)
                if kind in ("settle", "hand_start", "match_end", "move", "match_start") or len(events) % 5 == 0:
                    self.store.upsert_replay(match_id, json.dumps(events, ensure_ascii=False), "[]")

            try:
                jp = self._judge_params()
                result = await self.runner.run_binaries(
                    bot_a["binary_path"],
                    bot_b["binary_path"],
                    game_id=gid,
                    num_hands=int(m["total_hands"]),
                    n_dots=m.get("n_dots"),
                    board_size=jp["board_size"],
                    starting_stack=jp["starting_stack"],
                    sb=jp["sb"],
                    bb=jp["bb"],
                    on_event=on_event,
                )
                ea = sum(r.deltas[0] for r in result.rounds)
                eb = sum(r.deltas[1] for r in result.rounds)
                # 棋类（单轮）直接取该轮胜者；德州（多轮）按筹码差判断
                winner: int | None = result.winner
                if winner is None:
                    if ea > eb:
                        winner = 0
                    elif eb > ea:
                        winner = 1
                    else:
                        winner = None
                # 棋类若 match_end 带 winner 更准
                for ev in reversed(events):
                    if ev.get("type") == "match_end" and "winner" in ev:
                        winner = ev.get("winner")
                        break
                net_bb_a = (ea / 100.0) if gid == GAME_HOLDEM else float(ea)
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

    def _judge_params(self) -> dict[str, int | None]:
        """从 platform_settings 读裁判规则参数（热生效）；缺失或非法时用引擎常量兜底。

        返回 board_size/starting_stack/sb/bb，None 表示用引擎默认。
        n_dots 不在此处（走 match 列）；num_hands 走 match.total_hands。
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

        return {
            "board_size": _int(SETTING_JUDGE_GOMOKU_SIZE, BOARD_SIZE),
            "starting_stack": _int(SETTING_JUDGE_HOLDEM_STACK, STARTING_STACK),
            "sb": _int(SETTING_JUDGE_HOLDEM_SB, SMALL_BLIND),
            "bb": _int(SETTING_JUDGE_HOLDEM_BB, BIG_BLIND),
        }

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
