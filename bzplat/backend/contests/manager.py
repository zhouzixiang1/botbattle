"""组织者比赛：阶段模板、休息换 Bot、对阵调度。"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

from bzplat.backend.contests.stages import (
    estimate_match_count,
    generate_stage_pairings,
    swiss_rounds_needed,
)
from bzplat.backend.contests.templates import (
    default_match_config,
    points_for_result,
    resolve_stages,
)
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.runtime.limits import FULL_RR_MAX_N
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    CONTEST_FINISHED,
    CONTEST_OPEN,
    CONTEST_REST,
    CONTEST_RUNNING,
    REGISTERED_ENGINES,
    ROLE_ADMIN,
    ROLE_ORGANIZER,
    SETTING_FULL_RR_MAX_N,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    TYPE_CONTEST,
)

logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_stages(c: dict) -> list[dict]:
    raw = c.get("stages_json") or "[]"
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return []


def _match_config(c: dict) -> dict:
    """解析比赛的 match_config（每游戏一份对局参数）；回退 hands_per_match。"""
    raw = c.get("match_config_json") or "{}"
    try:
        cfg = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (json.JSONDecodeError, TypeError):
        cfg = {}
    if not cfg:
        # 向后兼容：旧比赛无 match_config，德扑用 hands_per_match
        hpm = int(c.get("hands_per_match") or 70)
        if hpm and c.get("game_id", "holdem") == "holdem":
            cfg = {"hands": hpm}
    return cfg if isinstance(cfg, dict) else {}


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
        template_id: str | None = None,
        game_id: str | None = None,
        stages: list[dict] | None = None,
        match_config: dict | None = None,
    ) -> dict:
        tid, gid, stage_list = resolve_stages(
            template_id, stages, game_id=game_id
        )
        # match_config：显式传入优先；否则按 game 取默认
        if match_config is None:
            match_config = default_match_config(gid)
        return self.store.create_contest(
            title,
            organizer_id,
            description=description,
            hands_per_match=hands_per_match,
            status="draft",
            game_id=gid,
            template_id=tid,
            stages_json=json.dumps(stage_list, ensure_ascii=False),
            current_stage_idx=0,
            match_config_json=json.dumps(match_config, ensure_ascii=False),
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
        can_proxy = role in (ROLE_ADMIN, ROLE_ORGANIZER)
        if bot["owner_id"] != user_id and not can_proxy:
            raise ValueError("只能派遣自己的 bot")
        if not bot.get("is_active") or not bot.get("binary_path"):
            raise ValueError("bot 不可用")
        contest_game = (c.get("game_id") or "holdem").lower()
        bot_game = (bot.get("game_id") or "holdem").lower()
        if bot_game != contest_game:
            raise ValueError(
                f"Bot 游戏类型 ({bot_game}) 与比赛 ({contest_game}) 不一致"
            )
        owner_id = bot["owner_id"]
        if self.store.get_entry(contest_id, owner_id):
            raise ValueError("该用户在此比赛中已报名")
        return self.store.add_contest_entry(contest_id, owner_id, bot_id)

    def dispatch(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        role: str = "",
    ) -> dict:
        """休息期（或允许换 Bot 的阶段间歇）更换派遣 Bot。

        已 running/completed 的 pairing 不变；仅更新 entry，影响尚未创建
        match 的 pending pairing 与后续阶段。
        """
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        if c["status"] not in (CONTEST_REST, CONTEST_OPEN, CONTEST_RUNNING):
            raise ValueError("当前状态不可更换 Bot")
        stages = _parse_stages(c)
        idx = int(c.get("current_stage_idx") or 0)
        stage = stages[idx] if 0 <= idx < len(stages) else {}
        if c["status"] == CONTEST_RUNNING and not stage.get("allow_bot_swap_in_rest"):
            raise ValueError("当前阶段不允许中途换 Bot（请等休息期）")
        if c["status"] == CONTEST_REST and not stage.get("allow_bot_swap_in_rest", True):
            raise ValueError("本阶段休息不允许换 Bot")

        bot = self.store.get_bot(bot_id)
        if not bot:
            raise ValueError("bot 不存在")
        can_proxy = role in (ROLE_ADMIN, ROLE_ORGANIZER)
        if bot["owner_id"] != user_id and not can_proxy:
            raise ValueError("只能派遣自己的 bot")
        if not bot.get("is_active") or not bot.get("binary_path"):
            raise ValueError("bot 不可用")
        contest_game = (c.get("game_id") or "holdem").lower()
        bot_game = (bot.get("game_id") or "holdem").lower()
        if bot_game != contest_game:
            raise ValueError(
                f"Bot 游戏类型 ({bot_game}) 与比赛 ({contest_game}) 不一致"
            )

        entry = self.store.get_entry(contest_id, bot["owner_id"] if can_proxy else user_id)
        # 普通用户按自己 user_id；proxy 时按 bot owner
        owner_id = bot["owner_id"]
        entry = self.store.get_entry(contest_id, owner_id)
        if not entry and not can_proxy:
            entry = self.store.get_entry(contest_id, user_id)
        if not entry:
            raise ValueError("未报名本比赛")
        if entry["user_id"] != user_id and not can_proxy:
            raise ValueError("只能更换自己的派遣")

        old_bot = entry["bot_id"]
        updated = self.store.update_entry(
            contest_id, entry["user_id"], bot_id=bot_id, dispatched_at=_now()
        )

        # 尚未开打的 pending pairing：用新 bot 替换旧 bot 快照
        for p in self.store.list_contest_pairings(contest_id):
            if p.get("status") != "pending" or p.get("match_id"):
                continue
            fields: dict[str, Any] = {}
            if p["bot_a_id"] == old_bot:
                fields["bot_a_id"] = bot_id
            if p["bot_b_id"] == old_bot:
                fields["bot_b_id"] = bot_id
            if fields:
                self.store.update_contest_pairing(p["id"], **fields)
        return updated

    def _full_rr_max_n(self) -> int:
        raw = self.store.get_setting(SETTING_FULL_RR_MAX_N)
        try:
            return int(raw) if raw else FULL_RR_MAX_N
        except ValueError:
            return FULL_RR_MAX_N

    def _guard_full_rr(self, stages: list[dict], n: int) -> None:
        limit = self._full_rr_max_n()
        for st in stages:
            t = st.get("type") or ""
            if t in ("round_robin", "double_round_robin") and n > limit:
                raise ValueError(
                    f"全员{t} 人数 {n} 超过上限 {limit}，请改用 Swiss/分组模板"
                )

    def _assert_engine(self, game_id: str) -> None:
        if game_id not in REGISTERED_ENGINES:
            raise ValueError(
                f"游戏引擎未注册: {game_id}（当前仅支持 {sorted(REGISTERED_ENGINES)}）"
            )

    async def start(self, contest_id: int) -> dict:
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        if c["status"] not in (CONTEST_OPEN, "draft"):
            raise ValueError("仅 open/draft 可开赛")
        game_id = c.get("game_id") or "holdem"
        self._assert_engine(game_id)

        entries = self.store.list_contest_entries(contest_id)
        if len(entries) < 2:
            raise ValueError("至少需要 2 名参赛")

        stages = _parse_stages(c)
        if not stages:
            _, _, stages = resolve_stages(c.get("template_id") or "holdem_swiss_ko")
            self.store.update_contest(
                contest_id, stages_json=json.dumps(stages, ensure_ascii=False)
            )

        self._guard_full_rr(stages, len(entries))

        # 按报名序赋 seed
        for i, e in enumerate(entries):
            self.store.update_entry(contest_id, e["user_id"], seed=i + 1, eliminated=0)

        self.store.update_contest(
            contest_id,
            status=CONTEST_RUNNING,
            starts_at=_now(),
            registration_closes_at=_now(),
            current_stage_idx=0,
            rest_ends_at=None,
        )
        await self._begin_stage(contest_id, 0)
        return self.store.get_contest(contest_id)

    async def _begin_stage(self, contest_id: int, stage_idx: int) -> None:
        c = self.store.get_contest(contest_id)
        stages = _parse_stages(c)
        if stage_idx < 0 or stage_idx >= len(stages):
            self.store.update_contest(
                contest_id, status=CONTEST_FINISHED, ends_at=_now(), rest_ends_at=None
            )
            return
        stage = stages[stage_idx]
        entries = [
            e
            for e in self.store.list_contest_entries(contest_id)
            if not e.get("eliminated")
        ]
        # 按 seed / 上一阶段积分排序
        standings = self.standings(contest_id, stage_idx=max(0, stage_idx - 1))
        score_map = {s["bot_id"]: s["points"] for s in standings}
        entries.sort(
            key=lambda e: (-score_map.get(e["bot_id"], 0), e.get("seed") or 0)
        )
        bot_ids = [e["bot_id"] for e in entries]
        if len(bot_ids) < 2 and stage.get("type") != "single_elimination":
            self.store.update_contest(
                contest_id, status=CONTEST_FINISHED, ends_at=_now()
            )
            return

        stype = stage.get("type")
        if stype == "swiss":
            rounds = int(stage.get("rounds") or 0) or swiss_rounds_needed(len(bot_ids))
            stage = {**stage, "rounds": rounds}
            # 生成全部瑞士轮（简化：开赛时一次性按当前积分预排第 1 轮；后续轮在 advance 时补）
            specs = generate_stage_pairings(stage, bot_ids, swiss_round=1)
        else:
            specs = generate_stage_pairings(stage, bot_ids)

        key = stage.get("key") or f"stage{stage_idx}"
        for sp in specs:
            self.store.add_contest_pairing(
                contest_id,
                sp.bot_a_id,
                sp.bot_b_id,
                round_num=sp.round_num,
                status="pending",
                stage_idx=stage_idx,
                stage_key=key,
                group_id=sp.group_id,
                bracket_slot=sp.bracket_slot,
                color_first=sp.color_first,
            )
        self.store.update_contest(
            contest_id, status=CONTEST_RUNNING, current_stage_idx=stage_idx, rest_ends_at=None
        )
        await self._dispatch_pending(contest_id, stage_idx)

    async def _dispatch_pending(self, contest_id: int, stage_idx: int) -> None:
        c = self.store.get_contest(contest_id)
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        cfg = _match_config(c)  # 每游戏对局参数（holdem→hands, pencil→n_dots）
        for p in pairings:
            if p.get("status") != "pending" or p.get("match_id"):
                continue
            # 冻结快照已在 pairing 行；直接开打
            mid = await self.orch.challenge(
                p["bot_a_id"],
                p["bot_b_id"],
                owner_user_id=c["organizer_id"],
                hands=int(cfg.get("hands", c.get("hands_per_match") or 70)),
                match_type=TYPE_CONTEST,
                contest_id=contest_id,
                game_id=c.get("game_id") or "holdem",
                n_dots=int(cfg["n_dots"]) if cfg.get("n_dots") is not None else None,
            )
            self.store.update_contest_pairing(p["id"], match_id=mid, status="running")

    def standings(
        self, contest_id: int, *, stage_idx: int | None = None
    ) -> list[dict]:
        c = self.store.get_contest(contest_id)
        stages = _parse_stages(c or {})
        if stage_idx is None:
            stage_idx = int((c or {}).get("current_stage_idx") or 0)
        stage = stages[stage_idx] if stages and 0 <= stage_idx < len(stages) else {}
        scoring = stage.get("scoring") or "poker_3_1_0"

        entries = self.store.list_contest_entries(contest_id)
        stats = {
            e["bot_id"]: {
                "bot_id": e["bot_id"],
                "user_id": e["user_id"],
                "points": 0.0,
                "wins": 0,
                "draws": 0,
                "losses": 0,
                "net_chips": 0,
                "group_id": e.get("group_id") or "",
                "eliminated": int(e.get("eliminated") or 0),
            }
            for e in entries
        }
        for p in self.store.list_contest_pairings(contest_id, stage_idx=stage_idx):
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
            wa = points_for_result(scoring, m["winner"], 0)
            wb = points_for_result(scoring, m["winner"], 1)
            stats[a]["points"] += wa
            stats[b]["points"] += wb
            if m["winner"] == 0:
                stats[a]["wins"] += 1
                stats[b]["losses"] += 1
            elif m["winner"] == 1:
                stats[b]["wins"] += 1
                stats[a]["losses"] += 1
            else:
                stats[a]["draws"] += 1
                stats[b]["draws"] += 1
            gid = p.get("group_id") or ""
            if gid:
                stats[a]["group_id"] = gid
                stats[b]["group_id"] = gid
        rows = list(stats.values())
        rows.sort(key=lambda r: (-r["points"], -r["net_chips"]))
        return rows

    def _stage_done(self, contest_id: int, stage_idx: int) -> bool:
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        if not pairings:
            return False
        for p in pairings:
            mid = p.get("match_id")
            if not mid:
                return False
            m = self.store.get_match(mid)
            if not m or m["status"] not in (STATUS_COMPLETED, STATUS_ABORTED):
                return False
        return True

    def _snapshot_stage_results(self, contest_id: int, stage_idx: int) -> None:
        c = self.store.get_contest(contest_id)
        stages = _parse_stages(c)
        key = ""
        if 0 <= stage_idx < len(stages):
            key = stages[stage_idx].get("key") or f"stage{stage_idx}"
        for i, s in enumerate(self.standings(contest_id, stage_idx=stage_idx)):
            self.store.upsert_stage_result(
                contest_id,
                stage_idx,
                s["bot_id"],
                stage_key=key,
                points=s["points"],
                wins=s["wins"],
                draws=s["draws"],
                losses=s["losses"],
                net_chips=s["net_chips"],
                group_id=s.get("group_id") or "",
                rank_in_group=i + 1,
            )

    def _advance_participants(self, contest_id: int, stage_idx: int) -> None:
        """根据阶段配置标记淘汰（不晋级者）。"""
        c = self.store.get_contest(contest_id)
        stages = _parse_stages(c)
        if stage_idx < 0 or stage_idx >= len(stages):
            return
        stage = stages[stage_idx]
        standings = self.standings(contest_id, stage_idx=stage_idx)
        advance: set[int] = set()
        if stage.get("advance_per_group"):
            per = int(stage["advance_per_group"])
            by_g: dict[str, list[dict]] = {}
            for s in standings:
                by_g.setdefault(s.get("group_id") or "_", []).append(s)
            for rows in by_g.values():
                for s in rows[:per]:
                    advance.add(s["bot_id"])
        elif stage.get("advance_count"):
            n = int(stage["advance_count"])
            for s in standings[:n]:
                advance.add(s["bot_id"])
        else:
            # 默认全部晋级（如单阶段 RR）
            advance = {s["bot_id"] for s in standings}

        for e in self.store.list_contest_entries(contest_id):
            if e["bot_id"] not in advance:
                self.store.update_entry(contest_id, e["user_id"], eliminated=1)

    async def maybe_finish(self, contest_id: int) -> dict | None:
        """对局结束回调：检查当前阶段是否完成，进入 rest 或下一阶段。"""
        c = self.store.get_contest(contest_id)
        if not c or c["status"] not in (CONTEST_RUNNING, CONTEST_REST):
            return None
        if c["status"] == CONTEST_REST:
            return await self._maybe_auto_resume(contest_id)

        stage_idx = int(c.get("current_stage_idx") or 0)
        stages = _parse_stages(c)
        if not self._stage_done(contest_id, stage_idx):
            # 瑞士制：当前轮完成则生成下一轮
            if stages and 0 <= stage_idx < len(stages):
                stage = stages[stage_idx]
                if stage.get("type") == "swiss":
                    await self._maybe_next_swiss_round(contest_id, stage_idx, stage)
            return None

        self._snapshot_stage_results(contest_id, stage_idx)
        rest_min = int((stages[stage_idx].get("rest_after_minutes") or 0) if stages else 0)
        has_next = stage_idx + 1 < len(stages)

        if has_next and rest_min > 0:
            ends = (datetime.now() + timedelta(minutes=rest_min)).isoformat(
                timespec="seconds"
            )
            self.store.update_contest(
                contest_id, status=CONTEST_REST, rest_ends_at=ends
            )
            return self.store.get_contest(contest_id)

        if has_next:
            self._advance_participants(contest_id, stage_idx)
            await self._begin_stage(contest_id, stage_idx + 1)
            return self.store.get_contest(contest_id)

        self.store.update_contest(
            contest_id, status=CONTEST_FINISHED, ends_at=_now(), rest_ends_at=None
        )
        return self.store.get_contest(contest_id)

    async def _maybe_next_swiss_round(
        self, contest_id: int, stage_idx: int, stage: dict
    ) -> None:
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        if not pairings:
            return
        max_round = max(int(p.get("round_num") or 1) for p in pairings)
        # 当前轮是否全部结束
        cur = [p for p in pairings if int(p.get("round_num") or 1) == max_round]
        for p in cur:
            mid = p.get("match_id")
            if not mid:
                return
            m = self.store.get_match(mid)
            if not m or m["status"] not in (STATUS_COMPLETED, STATUS_ABORTED):
                return
        total_rounds = int(stage.get("rounds") or swiss_rounds_needed(
            len(self.store.list_contest_entries(contest_id))
        ))
        if max_round >= total_rounds:
            return
        # 生成下一轮
        standings = self.standings(contest_id, stage_idx=stage_idx)
        scores = {s["bot_id"]: s["points"] for s in standings}
        bot_ids = [s["bot_id"] for s in standings if not s.get("eliminated")]
        played: set[tuple[int, int]] = set()
        for p in pairings:
            played.add((min(p["bot_a_id"], p["bot_b_id"]), max(p["bot_a_id"], p["bot_b_id"])))
        specs = generate_stage_pairings(
            stage, bot_ids, scores=scores, played=played, swiss_round=max_round + 1
        )
        key = stage.get("key") or f"stage{stage_idx}"
        for sp in specs:
            self.store.add_contest_pairing(
                contest_id,
                sp.bot_a_id,
                sp.bot_b_id,
                round_num=sp.round_num,
                status="pending",
                stage_idx=stage_idx,
                stage_key=key,
                group_id=sp.group_id,
                color_first=sp.color_first,
            )
        await self._dispatch_pending(contest_id, stage_idx)

    async def _maybe_auto_resume(self, contest_id: int) -> dict | None:
        c = self.store.get_contest(contest_id)
        if not c or c["status"] != CONTEST_REST:
            return None
        ends = c.get("rest_ends_at")
        if ends and ends <= _now():
            return await self.resume(contest_id)
        return None

    async def resume(self, contest_id: int) -> dict:
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        if c["status"] != CONTEST_REST:
            raise ValueError("当前不在休息期")
        stage_idx = int(c.get("current_stage_idx") or 0)
        stages = _parse_stages(c)
        if stage_idx + 1 >= len(stages):
            self.store.update_contest(
                contest_id, status=CONTEST_FINISHED, ends_at=_now(), rest_ends_at=None
            )
            return self.store.get_contest(contest_id)
        self._advance_participants(contest_id, stage_idx)
        await self._begin_stage(contest_id, stage_idx + 1)
        return self.store.get_contest(contest_id)

    async def advance(self, contest_id: int) -> dict:
        """组织者强制推进（跳过未完成检查时仅在阶段已完成时可用）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        if c["status"] == CONTEST_REST:
            return await self.resume(contest_id)
        stage_idx = int(c.get("current_stage_idx") or 0)
        if not self._stage_done(contest_id, stage_idx):
            raise ValueError("当前阶段对阵尚未全部完成")
        return (await self.maybe_finish(contest_id)) or self.store.get_contest(contest_id)

    def estimate(self, contest_id: int) -> dict:
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        n = len(self.store.list_contest_entries(contest_id))
        stages = _parse_stages(c)
        total = sum(estimate_match_count(st, n) for st in stages)
        conc_raw = self.store.get_setting("max_concurrent_matches")
        try:
            conc = max(1, int(conc_raw or 2))
        except ValueError:
            conc = 2
        # 粗估每场时长：holdem=每手约 2s；棋类单局按固定估算（n_dots 越大越久）
        gid = c.get("game_id") or "holdem"
        cfg = _match_config(c)
        if gid == "holdem":
            sec_per = int(cfg.get("hands", c.get("hands_per_match") or 70)) * 2
        elif gid == "pencil":
            # n_dots=11 → 约 120s；按 (n_dots/11)*120 线性估
            n_dots = int(cfg.get("n_dots") or 11)
            sec_per = max(30, int(n_dots / 11 * 120))
        else:  # gomoku
            sec_per = 60
        eta_sec = (total / conc) * sec_per if conc else 0
        return {
            "entries": n,
            "estimated_matches": total,
            "max_concurrent": conc,
            "eta_seconds": int(eta_sec),
        }
