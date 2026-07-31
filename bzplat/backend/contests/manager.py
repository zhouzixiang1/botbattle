"""组织者比赛：报名、循环赛对阵、调度。"""
from __future__ import annotations

import itertools
import logging
from datetime import datetime

from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    CONTEST_FINISHED,
    CONTEST_OPEN,
    CONTEST_RUNNING,
    ROLE_ADMIN,
    ROLE_ORGANIZER,
    STATUS_COMPLETED,
    TYPE_CONTEST,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class ContestManager:
    def __init__(self, store: Store, orch: MatchOrchestrator) -> None:
        self.store = store
        self.orch = orch

    def create(
        self,
        organizer_id: int,
        title: str,
        *,
        description: str = "",
        hands_per_match: int = 70,
    ) -> dict:
        return self.store.create_contest(
            title,
            organizer_id,
            description=description,
            hands_per_match=hands_per_match,
            status="draft",
        )

    def open_registration(self, contest_id: int) -> dict:
        self.store.update_contest(
            contest_id, status=CONTEST_OPEN, registration_opens_at=_now()
        )
        return self.store.get_contest(contest_id)

    def register(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        role: str = "",
    ) -> dict:
        c = self.store.get_contest(contest_id)
        if not c or c["status"] != CONTEST_OPEN:
            raise ValueError("比赛未开放报名")
        bot = self.store.get_bot(bot_id)
        if not bot:
            raise ValueError("bot 不存在")
        # admin/organizer 可代报名别人的 bot；普通用户只能派遣自己的 bot。
        # entry 一律归到 bot 的真正 owner 名下，保证积分归属正确且不违反
        # contest_entries 的 UNIQUE(contest_id, user_id) 约束。
        can_proxy = role in (ROLE_ADMIN, ROLE_ORGANIZER)
        if bot["owner_id"] != user_id and not can_proxy:
            raise ValueError("只能派遣自己的 bot")
        if not bot.get("is_active") or not bot.get("binary_path"):
            raise ValueError("bot 不可用")
        owner_id = bot["owner_id"]
        if self.store.get_entry(contest_id, owner_id):
            raise ValueError("该用户在此比赛中已报名")
        return self.store.add_contest_entry(contest_id, owner_id, bot_id)

    async def start(self, contest_id: int) -> dict:
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        entries = self.store.list_contest_entries(contest_id)
        if len(entries) < 2:
            raise ValueError("至少需要 2 名参赛")
        bots = [e["bot_id"] for e in entries]
        self.store.update_contest(
            contest_id, status=CONTEST_RUNNING, starts_at=_now(),
            registration_closes_at=_now(),
        )
        # round-robin pairings
        for a, b in itertools.combinations(bots, 2):
            self.store.add_contest_pairing(
                contest_id, a, b, round_num=1, status="pending"
            )
        pairings = self.store.list_contest_pairings(contest_id)
        for p in pairings:
            mid = await self.orch.challenge(
                p["bot_a_id"],
                p["bot_b_id"],
                owner_user_id=c["organizer_id"],
                hands=int(c["hands_per_match"]),
                match_type=TYPE_CONTEST,
                contest_id=contest_id,
            )
            self.store.update_contest_pairing(p["id"], match_id=mid, status="running")
        return self.store.get_contest(contest_id)

    def standings(self, contest_id: int) -> list[dict]:
        """循环赛积分：胜 3 / 平 1 / 负 0，副指标净筹码。"""
        entries = self.store.list_contest_entries(contest_id)
        stats = {
            e["bot_id"]: {
                "bot_id": e["bot_id"],
                "user_id": e["user_id"],
                "points": 0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "net_chips": 0,
            }
            for e in entries
        }
        for p in self.store.list_contest_pairings(contest_id):
            mid = p.get("match_id")
            if not mid:
                continue
            m = self.store.get_match(mid)
            if not m or m["status"] != STATUS_COMPLETED:
                continue
            a, b = m["bot_a_id"], m["bot_b_id"]
            if a not in stats or b not in stats:
                continue
            stats[a]["net_chips"] += m["earnings_a"]
            stats[b]["net_chips"] += m["earnings_b"]
            if m["winner"] == 0:
                stats[a]["points"] += 3
                stats[a]["wins"] += 1
                stats[b]["losses"] += 1
            elif m["winner"] == 1:
                stats[b]["points"] += 3
                stats[b]["wins"] += 1
                stats[a]["losses"] += 1
            else:
                stats[a]["points"] += 1
                stats[b]["points"] += 1
                stats[a]["draws"] += 1
                stats[b]["draws"] += 1
        rows = list(stats.values())
        rows.sort(key=lambda r: (-r["points"], -r["net_chips"]))
        return rows

    def maybe_finish(self, contest_id: int) -> dict | None:
        pairings = self.store.list_contest_pairings(contest_id)
        if not pairings:
            return None
        done = True
        for p in pairings:
            mid = p.get("match_id")
            if not mid:
                done = False
                break
            m = self.store.get_match(mid)
            if not m or m["status"] not in (STATUS_COMPLETED, "aborted"):
                done = False
                break
        if done:
            self.store.update_contest(
                contest_id, status=CONTEST_FINISHED, ends_at=_now()
            )
            return self.store.get_contest(contest_id)
        return None
