"""赛事时间调度器——后台周期扫描赛事的 *_at 字段，到点自动推进阶段。

采用单进程 ``while True + asyncio.sleep`` 后台任务，
挂到 ``main.py`` lifespan。每 interval（默认 15s，platform_settings 可配）扫描所有赛事：

1. **draft 且 registration_opens_at<=now** → ``open_registration()``（到点开放报名）
2. **open 且 registration_closes_at<=now** → ``publish()``（到点截止报名 + 出排期，→ published）
3. **published** → ``_dispatch_pending()``（到点开打：scheduled_at<=now 的 pairing 才 dispatch；
   全部 pairing 打完则经 maybe_finish 推进）
4. **rest 且 rest_ends_at<=now** → ``resume()``（到点恢复休息期，修现有「rest 不自动恢复」漏洞）

组织者手动按钮（open/publish/start/resume/advance）始终可用——调度器到点自动 + 手动可提前。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from bzplat.backend.runtime.config import (
    CONTEST_SCHEDULER_CONFIG,
    ContestSchedulerConfig,
)
from bzplat.backend.store.schema import (
    CONTEST_DRAFT,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    CONTEST_REST,
    CONTEST_RUNNING,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    from datetime import datetime
    return datetime.now().isoformat(timespec="seconds")


class ContestScheduler:
    """赛事时间调度器（后台周期扫描，到点推进赛事阶段）。"""

    def __init__(
        self,
        manager: Any,
        store: Any,
        *,
        config: ContestSchedulerConfig = CONTEST_SCHEDULER_CONFIG,
    ) -> None:
        self.manager = manager
        self.store = store
        self.config = config

    def _cfg(self) -> dict[str, Any]:
        return self.config.as_dict()

    async def loop(self) -> None:
        """周期扫描：到点的赛事自动推进阶段。"""
        while True:
            try:
                cfg = self._cfg()
                await asyncio.sleep(cfg["interval"])
                if cfg["enabled"]:
                    await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 调度器不得因单轮异常退出
                logger.exception("contest-scheduler loop iteration failed")

    async def _tick(self) -> None:
        """单轮扫描：检查所有赛事的时间窗口，到点推进。

        **快照分派**：tick 开头一次性取所有相关状态（draft/open/published/running/rest）
        的赛事快照，按快照里的 status 分派处理。避免「published→running 后同 tick 的 running
        循环又处理同一赛事」的双重推进。
        """
        control = self.store.executions.control()
        if self.store.executions.is_maintenance_control(control):
            # Deployment readiness must not turn green while a background
            # scheduler is still creating schedules, rounds or lifecycle
            # transitions.  Existing active match completion is handled by the
            # orchestrator callback; all proactive progression resumes on the
            # next tick after explicit maintenance end.
            return
        now = _now()
        # 一次性快照所有需检查的赛事（按 status 分组）
        snapshot: dict[str, list[dict]] = {}
        for st in (CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST):
            snapshot[st] = self._contests_by_status(st)
        processed: set[int] = set()  # 本轮已处理（避免重复）

        # 1. draft→open：到点开放报名
        for c in snapshot[CONTEST_DRAFT]:
            opens = c.get("registration_opens_at")
            if opens and now >= opens:
                try:
                    await self.manager.open_registration(c["id"])
                    processed.add(c["id"])
                    logger.info("scheduler: contest %s auto-opened (was draft)", c["id"])
                except Exception:
                    logger.exception("scheduler: auto-open contest %s failed", c["id"])

        # 2. open→published：到点截止报名 + 出排期
        for c in snapshot[CONTEST_OPEN]:
            closes = c.get("registration_closes_at")
            if closes and now >= closes:
                try:
                    await self.manager.publish(c["id"])
                    processed.add(c["id"])
                    logger.info("scheduler: contest %s auto-published (registration closed)", c["id"])
                except Exception:
                    logger.exception("scheduler: auto-publish contest %s failed", c["id"])

        # 3. published：到点开打（scheduled_at<=now 的 pairing 才 dispatch）
        for c in snapshot[CONTEST_PUBLISHED]:
            if c["id"] in processed:
                continue
            # ``starts_at`` 为空表示只发布排期、等待组织者手动开始，绝不能
            # 偷换成“报名截止后立即开打”。有计划开赛时间时也必须先过赛事
            # 级闸门，再检查逐场 scheduled_at。
            starts_at = c.get("starts_at")
            if not starts_at or now < starts_at:
                continue
            try:
                stage_idx = int(c.get("current_stage_idx") or 0)
                # 防御：published 态若无 pairing（publish 时 _begin_stage 异常未生成），补生成
                pairings = self.manager.store.list_contest_pairings(c["id"], stage_idx=stage_idx)
                if not pairings:
                    logger.warning("scheduler: published contest %s has 0 pairings, regenerating", c["id"])
                    await self.manager.ensure_published_pairings(c["id"], stage_idx)
                await self.manager._dispatch_pending(c["id"], stage_idx)
                await self.manager.maybe_finish(c["id"])
            except Exception:
                logger.exception("scheduler: published contest %s dispatch failed", c["id"])

        # 4. running：检查是否有到点的 pending pairing 需 dispatch（逐场排期后续轮次）
        #    + maybe_finish 推进阶段。跳过本轮已处理的（如刚 published→running）。
        for c in snapshot[CONTEST_RUNNING]:
            if c["id"] in processed:
                continue
            try:
                stage_idx = int(c.get("current_stage_idx") or 0)
                await self.manager._dispatch_pending(c["id"], stage_idx)
                await self.manager.maybe_finish(c["id"])
            except Exception:
                logger.exception("scheduler: running contest %s tick failed", c["id"])


        # 5. rest→running：到点恢复休息期（修现有「rest 不自动恢复」漏洞）
        for c in snapshot[CONTEST_REST]:
            if c["id"] in processed:
                continue
            ends = c.get("rest_ends_at")
            if ends and now >= ends:
                try:
                    await self.manager.resume(c["id"])
                    logger.info("scheduler: contest %s auto-resumed (rest ended)", c["id"])
                except Exception:
                    logger.exception("scheduler: auto-resume contest %s failed", c["id"])

    def _contests_by_status(self, status: str) -> list[dict]:
        """按状态列赛事（容错：list_contests_by_status 不存在则空）。"""
        fn = getattr(self.store, "list_contests_by_status", None)
        if fn is None:
            return []
        return fn([status])
