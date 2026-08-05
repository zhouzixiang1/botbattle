"""闲时自动对局调度器：系统空闲时自动安排 bot 对战以维护天梯榜。

设计：
- 单进程单事件循环（uvicorn factory，无 workers），后台 asyncio 任务周期轮询。
- 仅当存在空闲并发槽（已为用户挑战预留 reserve_slots）且连续空闲达 min_idle_sec 才触发。
- 配对策略：陈旧度优先（last_played_at 最旧 / 从未赛）+ rating 就近（Swiss 式）。
- 节流：同一 bot 两场间隔不低于 bot_cooldown；内存 recent_pairs 去重近期配对。
- owner_user_id=None（系统发起，无 owner）；match_type=ladder，更新全局 Glicko-2 评分。

不与 orchestrator 的并发上限冲突：每场对局经 orch._tasks 计数，_sem 仍硬限制执行。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from bzplat.backend.store.schema import (
    SETTING_AUTO_MATCH_BOT_COOLDOWN,
    SETTING_AUTO_MATCH_DAILY_CAP,
    SETTING_AUTO_MATCH_ENABLED,
    SETTING_AUTO_MATCH_INTERVAL_SEC,
    SETTING_AUTO_MATCH_MAX_PER_ROUND,
    SETTING_AUTO_MATCH_MIN_IDLE_SEC,
    SETTING_AUTO_MATCH_PLACEMENT_GAMES,
    SETTING_AUTO_MATCH_RESERVE_SLOTS,
    SETTING_AUTO_MATCH_STALE_SEC,
    TYPE_LADDER,
    VALID_GAME_IDS,
)

logger = logging.getLogger(__name__)


class AutoMatchScheduler:
    """后台闲时自动对局调度器。"""

    def __init__(self, orch: Any, store: Any) -> None:
        self.orch = orch
        self.store = store
        # 内存节流状态
        self._bot_last_scheduled: dict[int, float] = {}  # bot_id -> monotonic ts
        self._recent_pairs: dict[tuple[int, int], float] = {}  # (min,max) -> ts
        # 连续空闲计时（monotonic）
        self._idle_since: float | None = None
        # 每日计数（按本地日期串重置）
        self._daily_count: int = 0
        self._daily_date: str = ""

    @property
    def daily_count(self) -> int:
        """今日已调度场数（供 admin 可见性展示）。"""
        self._maybe_reset_daily()
        return self._daily_count

    def _maybe_reset_daily(self) -> None:
        from datetime import date

        today = date.today().isoformat()
        if self._daily_date != today:
            self._daily_date = today
            self._daily_count = 0

    # ------------------------------------------------------------------ config
    def _cfg(self) -> dict[str, Any]:
        s = self.store.get_settings(
            [
                SETTING_AUTO_MATCH_ENABLED,
                SETTING_AUTO_MATCH_INTERVAL_SEC,
                SETTING_AUTO_MATCH_MIN_IDLE_SEC,
                SETTING_AUTO_MATCH_BOT_COOLDOWN,
                SETTING_AUTO_MATCH_STALE_SEC,
                SETTING_AUTO_MATCH_RESERVE_SLOTS,
                SETTING_AUTO_MATCH_PLACEMENT_GAMES,
                SETTING_AUTO_MATCH_MAX_PER_ROUND,
                SETTING_AUTO_MATCH_DAILY_CAP,
            ]
        )

        def _int(key: str, default: int) -> int:
            try:
                return int(s.get(key) or default)
            except (TypeError, ValueError):
                return default

        return {
            "enabled": (s.get(SETTING_AUTO_MATCH_ENABLED) or "1") in ("1", "true", "yes"),
            "interval": _int(SETTING_AUTO_MATCH_INTERVAL_SEC, 30),
            "min_idle": _int(SETTING_AUTO_MATCH_MIN_IDLE_SEC, 5),
            "cooldown": _int(SETTING_AUTO_MATCH_BOT_COOLDOWN, 600),
            "stale": _int(SETTING_AUTO_MATCH_STALE_SEC, 3600),
            "reserve": _int(SETTING_AUTO_MATCH_RESERVE_SLOTS, 1),
            "placement_games": _int(SETTING_AUTO_MATCH_PLACEMENT_GAMES, 10),
            "max_per_round": _int(SETTING_AUTO_MATCH_MAX_PER_ROUND, 2),
            "daily_cap": _int(SETTING_AUTO_MATCH_DAILY_CAP, 200),
        }

    # ------------------------------------------------------------------ loop
    async def loop(self) -> None:
        """周期轮询：闲时则挑配对并 challenge。"""
        while True:
            try:
                cfg = self._cfg()
                await asyncio.sleep(max(1, cfg["interval"]))
                if not cfg["enabled"]:
                    self._idle_since = None
                    continue
                idle = self._is_idle(cfg)
                if idle:
                    await self._schedule_some(cfg)
                # 注意：不在 else 里重置 _idle_since——_is_idle 内部管理：
                # free<=0 时重置（真忙）；计时中（第一轮）保留供下一轮判断。
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 调度器不得因单轮异常退出
                logger.exception("auto-match loop iteration failed")

    def _is_idle(self, cfg: dict[str, Any]) -> bool:
        """有预留后的空闲并发槽，且连续空闲达 min_idle 秒。

        注意：用 _bot_running（实际占用 _sem 槽位的）而非 _tasks（含等信号量的）。
        否则大量 pending 任务排队等槽时（如赛事排期积压），running 虚高 → 永不空闲 →
        定级对局永远打不起来。_bot_running 准确反映真正占用的槽位。
        """
        running = getattr(self.orch, "_bot_running", 0) or 0
        free = self.orch.max_concurrent - cfg["reserve"] - running
        if free <= 0:
            self._idle_since = None
            return False
        now = time.monotonic()
        if self._idle_since is None:
            self._idle_since = now
            return False  # 本轮开始计时，下一轮才可能触发
        return (now - self._idle_since) >= cfg["min_idle"]

    async def _schedule_some(self, cfg: dict[str, Any]) -> int:
        """在空闲槽内尽量安排对局；返回本轮安排场数。"""
        self._maybe_reset_daily()
        # 每日总量上限
        if cfg["daily_cap"] > 0 and self._daily_count >= cfg["daily_cap"]:
            logger.info("auto-match daily cap reached %d/%d，今日停止", self._daily_count, cfg["daily_cap"])
            return 0
        running = getattr(self.orch, "_bot_running", 0) or 0  # 同 _is_idle：用实际占信号量槽位数
        free = self.orch.max_concurrent - cfg["reserve"] - running
        if free <= 0:
            return 0
        # 本轮上限：空闲槽、每轮上限、每日剩余 取最小
        max_this_round = min(free, cfg["max_per_round"] if cfg["max_per_round"] > 0 else free)
        if cfg["daily_cap"] > 0:
            max_this_round = min(max_this_round, cfg["daily_cap"] - self._daily_count)
        if max_this_round <= 0:
            return 0
        now = time.monotonic()
        placement = cfg["placement_games"]
        # 定级期 bot 用更短 cooldown，加快定级
        placement_cd = max(30, cfg["cooldown"] // 10)
        scheduled = 0
        for gid in VALID_GAME_IDS:
            if scheduled >= max_this_round:
                break
            candidates = self.store.least_recently_played(
                gid,
                limit=64,
                stale_since=cfg["stale"] if cfg["stale"] > 0 else None,
                placement_games=placement if placement > 0 else None,
            )
            if len(candidates) < 2:
                continue
            # 过滤：cooldown 内的 bot 跳过（定级期 bot 用短 cooldown）
            def _cd_for(b: dict) -> int:
                in_placement = placement > 0 and int(b.get("matches_played") or 0) < placement
                return placement_cd if in_placement else cfg["cooldown"]

            avail = [
                b for b in candidates
                if (now - self._bot_last_scheduled.get(b["bot_id"], 0.0)) >= _cd_for(b)
            ]
            if len(avail) < 2:
                continue
            # 取最优先的 A（avail 已按定级优先+陈旧度排序），按 rating 就近选 B
            a = avail[0]
            partner = self._pick_partner(a, avail[1:], now, cfg["cooldown"])
            if partner is None:
                continue
            try:
                await self.orch.challenge(
                    a["bot_id"],
                    partner["bot_id"],
                    owner_user_id=None,
                    match_type=TYPE_LADDER,
                    game_id=gid,
                )
            except Exception:  # noqa: BLE001
                logger.warning(
                    "auto-match challenge failed %s vs %s",
                    a.get("bot_name"),
                    partner.get("bot_name"),
                    exc_info=True,
                )
                continue
            self._bot_last_scheduled[a["bot_id"]] = now
            self._bot_last_scheduled[partner["bot_id"]] = now
            self._recent_pairs[
                (min(a["bot_id"], partner["bot_id"]), max(a["bot_id"], partner["bot_id"]))
            ] = now
            self._evict_recent(now, cfg["cooldown"])
            scheduled += 1
            self._daily_count += 1
            a_pl = int(a.get("matches_played") or 0) < placement if placement > 0 else False
            logger.info(
                "auto-match scheduled: %s(%s) vs %s(%s) [%s] placement=%s daily=%d/%d",
                a.get("bot_name"), a["bot_id"], partner.get("bot_name"), partner["bot_id"],
                gid, a_pl, self._daily_count, cfg["daily_cap"],
            )
        return scheduled

    def _pick_partner(
        self, a: dict, rest: list[dict], now: float, cooldown: int
    ) -> dict | None:
        """从 rest 中按 rating 就近选 B，跳过近期已配对与自身。"""
        best: dict | None = None
        best_gap = float("inf")
        key_a = a["bot_id"]
        a_rating = float(a.get("rating") or 1500.0)
        for b in rest:
            pair = (min(key_a, b["bot_id"]), max(key_a, b["bot_id"]))
            last = self._recent_pairs.get(pair)
            if last is not None and (now - last) < cooldown:
                continue
            gap = abs(a_rating - float(b.get("rating") or 1500.0))
            if gap < best_gap:
                best_gap = gap
                best = b
        return best

    def _evict_recent(self, now: float, cooldown: int) -> None:
        """清理过期的 recent_pairs 条目，避免无界增长。"""
        stale = [k for k, t in self._recent_pairs.items() if (now - t) > cooldown * 4]
        for k in stale:
            self._recent_pairs.pop(k, None)


__all__ = ["AutoMatchScheduler"]
