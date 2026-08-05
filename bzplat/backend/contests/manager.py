"""组织者比赛：阶段模板、休息换 Bot、对阵调度。"""
from __future__ import annotations

import asyncio
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
    get_template,
    points_for_result,
    resolve_stages,
    resolve_template,
)
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.games import registry as game_registry
from bzplat.backend.runtime.limits import FULL_RR_MAX_N
from bzplat.backend.store import Store
from bzplat.backend.store.db import match_deltas
from bzplat.backend.store.schema import (
    CONTEST_FINISHED,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
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


def _validate_contest_times(
    opens_at: str | None, closes_at: str | None, starts_at: str | None
) -> None:
    """校验赛事时间窗口逻辑：开放报名 < 截止报名 < 开赛（三者非 None 时）。

    时间是 naive 本地 ISO 字符串，字符串字典序 == 时间序（因格式固定到秒）。
    """
    for label, t in (("开放报名", opens_at), ("报名截止", closes_at), ("比赛开始", starts_at)):
        if t is not None:
            _validate_iso(t, label)
    if opens_at and closes_at and opens_at > closes_at:
        raise ValueError("报名截止时间必须晚于开放报名时间")
    if closes_at and starts_at and closes_at > starts_at:
        raise ValueError("比赛开始时间必须晚于报名截止时间")


def _validate_iso(t: str, label: str = "时间") -> None:
    """校验 naive 本地 ISO 字符串格式（YYYY-MM-DDTHH:MM:SS，无时区）。

    非标准格式（带毫秒/时区/非零填充）会破坏字符串字典序比较。规范化到秒级。
    """
    if not isinstance(t, str):
        raise ValueError(f"{label}必须是 ISO 时间字符串")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        raise ValueError(f"{label}格式非法（需 YYYY-MM-DDTHH:MM:SS）: {t}")
    # 拒绝带时区的（naive 约定）
    if dt.tzinfo is not None:
        raise ValueError(f"{label}不应带时区（平台用 naive 本地时间）: {t}")


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
        # 向后兼容：旧比赛无 match_config，德扑用 hands_per_match 列兜底
        # （仅 holdem 行有该列显式值；非 holdem 行 hands_per_match 恒为默认 70 但无意义，
        # 故只认非零显式值——解耦审计 I2：不再按 game_id 名分支）
        hpm = int(c.get("hands_per_match") or 0)
        if hpm:
            cfg = {"hands": hpm}
    return cfg if isinstance(cfg, dict) else {}


def _estimate_sec_per_match(gid: str, cfg: dict) -> int:
    """粗估每场时长（秒）：经 spec.eta_for_match（消除 if game_id 分支）。

    holdem=hands×2；pencil=n_dots 缩放；gomoku 固定——这些游戏特化逻辑封装在
    各 spec.eta_for_match 里，通用层不再 if gid==。cfg 的 hands 缺省由 _match_config
    的 hands_per_match 回退已处理（旧 holdem 比赛）。
    """
    return game_registry.get(gid).eta_for_match(cfg)


class ContestManager:
    def __init__(self, store: Store, orch: MatchOrchestrator) -> None:
        self.store = store
        self.orch = orch
        # per-contest 锁：串行化所有写状态路径（start/publish/resume/advance/maybe_finish/
        # _dispatch_pending），防止 on_match_done 并发回调 + scheduler 并发导致重复生成轮次。
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, contest_id: int) -> asyncio.Lock:
        """取（或建）该 contest 的锁。"""
        lk = self._locks.get(contest_id)
        if lk is None:
            lk = asyncio.Lock()
            self._locks[contest_id] = lk
        return lk

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
        phase: str = "standalone",
        source_contest_id: int | None = None,
        require_real_name: int = 0,
        registration_opens_at: str | None = None,
        registration_closes_at: str | None = None,
        starts_at: str | None = None,
    ) -> dict:
        # 自定义 stages 直接用；否则从模板表（含 admin 覆盖）解析 stages+match_config
        if stages:
            tid = template_id or "custom"
            gid = (game_id or "holdem").strip().lower()
            stage_list = stages
            tpl_mc = default_match_config(gid)
        else:
            tid, gid, stage_list, tpl_mc = resolve_template(
                template_id, game_id=game_id, store=self.store
            )
        # P5：phase 优先级：显式传入 > 模板自带 phase > standalone
        if phase == "standalone":
            tpl = get_template(tid)
            if tpl and tpl.get("phase"):
                phase = tpl["phase"]
        # match_config 优先级：显式传入 > 模板自带 > game 默认
        cfg = match_config if match_config is not None else tpl_mc
        # 时间校验：开放报名 < 截止报名 < 开赛（三者非 None 时）
        _validate_contest_times(registration_opens_at, registration_closes_at, starts_at)
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
            match_config_json=json.dumps(cfg, ensure_ascii=False),
            phase=phase,
            source_contest_id=source_contest_id,
            require_real_name=require_real_name,
            registration_opens_at=registration_opens_at,
            registration_closes_at=registration_closes_at,
            starts_at=starts_at,
        )

    def open_registration(self, contest_id: int) -> dict:
        """手动开放报名。若 registration_opens_at 未预设则盖 now（手动触发兼容）；
        已预设则调度器到点自动调本方法。"""
        c = self.store.get_contest(contest_id)
        opens = (c or {}).get("registration_opens_at") or _now()
        self.store.update_contest(
            contest_id, status=CONTEST_OPEN, registration_opens_at=opens
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
        # 报名截止时间校验：若 registration_closes_at 已预设且当前已过，拒绝报名
        closes = c.get("registration_closes_at")
        if closes and _now() > closes:
            raise ValueError("报名已截止")
        # 实名校验：赛事要求实名时，报名者必须已填完整实名信息
        if int(c.get("require_real_name") or 0):
            u = self.store.get_user(user_id)
            if not u or not all((u.get(k) or "").strip() for k in ("real_name", "phone", "school", "student_id")):
                raise ValueError("本赛事要求实名，请先在个人资料填写实名信息（姓名/手机号/学校/学号）")
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

        # proxy（admin/organizer）按 bot owner 查 entry；普通用户 owner_id==user_id（line 207 已保证）
        owner_id = bot["owner_id"]
        entry = self.store.get_entry(contest_id, owner_id)
        if not entry:
            raise ValueError("未报名本比赛")
        if entry["user_id"] != user_id and not can_proxy:
            raise ValueError("只能更换自己的派遣")

        old_bot = entry["bot_id"]
        updated = self.store.update_entry(
            contest_id, entry["user_id"], bot_id=bot_id, dispatched_at=_now()
        )

        # P1：轮次冻结——已发布轮（published_at 非空）的 pairing 不改写 bot/version/seed。
        # 仅未发布的 pending pairing（理论不存在，因生成即发布）才用新 bot 替换。
        # 换 Bot 只影响下一轮生成（_maybe_next_swiss_round 读 entry 当前 bot_id）。
        for p in self.store.list_contest_pairings(contest_id):
            if p.get("published_at"):
                continue  # 已发布轮冻结
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
        """人数护栏：防止超大规模循环赛静默生成海量对局（@500 全员单循环=124750 场）。

        - round_robin / double_round_robin：全员互打，校验总人数 n ≤ limit。
          stage.allow_large_round_robin=True 时旁路（仅白名单 builtin 模板，如决赛）。
        - group_round_robin / group_double_round_robin：组内循环，校验**每组**人数
          （蛇形分组后每组 ≈ ceil(n/group_count)）≤ limit。
        """
        import math

        limit = self._full_rr_max_n()
        for st in stages:
            t = st.get("type") or ""
            if t in ("round_robin", "double_round_robin"):
                if st.get("allow_large_round_robin"):
                    continue  # 决赛等白名单模板旁路护栏（组织者自负规模）
                if n > limit:
                    raise ValueError(
                        f"全员{t} 人数 {n} 超过上限 {limit}，请改用 Swiss/分组模板"
                    )
            elif t in ("group_round_robin", "group_double_round_robin"):
                gc = max(1, int(st.get("group_count") or 4))
                per_group = math.ceil(n / gc)
                if per_group > limit:
                    raise ValueError(
                        f"{t} 每组人数 {per_group}（{n}人÷{gc}组）超过上限 {limit}，"
                        f"请增加 group_count 或改用 Swiss 模板"
                    )

    def _assert_engine(self, game_id: str) -> None:
        if game_id not in REGISTERED_ENGINES:
            raise ValueError(
                f"游戏引擎未注册: {game_id}（当前仅支持 {sorted(REGISTERED_ENGINES)}）"
            )

    async def start(self, contest_id: int) -> dict:
        """立即开赛（手动触发，跳过排期等待）。

        - **open/draft**：生成对阵 + 设 scheduled_at=now（立即开打）+ dispatch 全部。
        - **published**：排期已发布（pairing 已生成），**不重新生成**——仅把现有 pending
          pairing 的 scheduled_at 改成 now（立即到点）+ dispatch。避免重复生成 pairing。
        若要走两阶段（截止报名→出排期→到开赛时间再开打），用 publish() + 调度器。
        """
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        if c["status"] not in (CONTEST_OPEN, "draft", CONTEST_PUBLISHED):
            raise ValueError("仅 open/draft/published 可开赛")
        game_id = c.get("game_id") or "holdem"
        self._assert_engine(game_id)

        # published 态：pairing 已存在，直接改 scheduled_at=now 立即开打（不重新生成）
        if c["status"] == CONTEST_PUBLISHED:
            now = _now()
            for p in self.store.list_contest_pairings(contest_id, stage_idx=int(c.get("current_stage_idx") or 0)):
                if p.get("status") == "pending" and not p.get("match_id"):
                    self.store.update_contest_pairing(p["id"], scheduled_at=now)
            self.store.update_contest(contest_id, starts_at=now, rest_ends_at=None)
            await self._dispatch_pending(contest_id, int(c.get("current_stage_idx") or 0))
            return self.store.get_contest(contest_id)

        entries = self.store.list_contest_entries(contest_id)
        if len(entries) < 2:
            raise ValueError("至少需要 2 名参赛")

        stages = _parse_stages(c)
        if not stages:
            _, _, stages = resolve_stages(
                c.get("template_id") or "holdem_swiss_ko", store=self.store
            )
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
        await self._begin_stage(contest_id, 0, schedule_immediately=True)
        return self.store.get_contest(contest_id)

    async def publish(self, contest_id: int) -> dict:
        """截止报名 + 出排期（status=open→published）。

        生成对阵 + 逐场排期 scheduled_at + 冻结版本，但**不 dispatch**——等开赛时间到
        调度器到点 dispatch（scheduled_at<=now 的 pairing 才开打）。
        组织者可手动调本方法提前出排期；调度器到 registration_closes_at 自动调。
        """
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        if c["status"] not in (CONTEST_OPEN, "draft"):
            raise ValueError("仅 open/draft 可出排期")
        game_id = c.get("game_id") or "holdem"
        self._assert_engine(game_id)

        entries = self.store.list_contest_entries(contest_id)
        if len(entries) < 2:
            raise ValueError("至少需要 2 名参赛")

        stages = _parse_stages(c)
        if not stages:
            _, _, stages = resolve_stages(
                c.get("template_id") or "holdem_swiss_ko", store=self.store
            )
            self.store.update_contest(
                contest_id, stages_json=json.dumps(stages, ensure_ascii=False)
            )

        self._guard_full_rr(stages, len(entries))

        # 按报名序赋 seed
        for i, e in enumerate(entries):
            self.store.update_entry(contest_id, e["user_id"], seed=i + 1, eliminated=0)

        # 截止报名盖戳（用预设的 closes_at 或 now）+ 进 published 态
        closes = c.get("registration_closes_at") or _now()
        self.store.update_contest(
            contest_id,
            status=CONTEST_PUBLISHED,
            registration_closes_at=closes,
            current_stage_idx=0,
            rest_ends_at=None,
        )
        await self._begin_stage(contest_id, 0, schedule_immediately=False)
        return self.store.get_contest(contest_id)

    async def _begin_stage(
        self, contest_id: int, stage_idx: int, *, schedule_immediately: bool = False
    ) -> None:
        """生成阶段对阵。schedule_immediately=True 时 scheduled_at 全设 now（立即开打）；
        False 时按赛事 starts_at + 轮次 stagger 逐场排期（published 态，等调度器到点 dispatch）。
        """
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
        # 按 seed / 上一阶段积分排序（P0：standings 键改 entry_id，score_map 用 entry_id）
        standings = self.standings(contest_id, stage_idx=max(0, stage_idx - 1))
        score_map = {s["entry_id"]: s["points"] for s in standings}
        entries.sort(
            key=lambda e: (-score_map.get(e["id"], 0), e.get("seed") or 0)
        )
        bot_ids = [e["bot_id"] for e in entries if e.get("bot_id") is not None]
        # P0：bot_id → entry_id 映射（生成 pairing 时快照 entry 身份）
        bot_to_entry = {e["bot_id"]: e["id"] for e in entries if e.get("bot_id") is not None}
        if len(bot_ids) < 2 and stage.get("type") != "single_elimination":
            self.store.update_contest(
                contest_id, status=CONTEST_FINISHED, ends_at=_now(), rest_ends_at=None
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

        # 逐场排期：schedule_immediately 时全 now；否则按 base + round stagger。
        # base = starts_at（仅第一阶段用赛事开赛时间）；后续阶段（stage_idx>0）用 now
        # （阶段间排期基准：rest 恢复/晋级后的新阶段从当前时刻起排）。
        if schedule_immediately:
            base = _now()
        elif stage_idx > 0:
            base = _now()  # 后续阶段从当前时刻排期（不用最初 starts_at，已过期）
        else:
            base = c.get("starts_at") or _now()
        stagger_min = max(0, int(stage.get("round_stagger_minutes") or 0))  # 非负
        key = stage.get("key") or f"stage{stage_idx}"
        published_at = _now()
        for sp in specs:
            sched = self._compute_scheduled_at(sp.round_num, base, stagger_min)
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
                entry_a_id=bot_to_entry.get(sp.bot_a_id),
                entry_b_id=bot_to_entry.get(sp.bot_b_id),
                published_at=published_at,
                scheduled_at=sched,
                **self._version_snapshot(sp.bot_a_id, sp.bot_b_id),
            )
        # published 态不立即改 status=running（等 dispatch 才 running）；
        # schedule_immediately 时直接 running（start() 路径）。
        if schedule_immediately:
            self.store.update_contest(
                contest_id, status=CONTEST_RUNNING, current_stage_idx=stage_idx, rest_ends_at=None
            )
        await self._dispatch_pending(contest_id, stage_idx)

    @staticmethod
    def _compute_scheduled_at(round_num: int, base: str, stagger_min: int) -> str:
        """逐场排期：scheduled_at = base + (round_num-1) * stagger_min 分钟。

        round_num 从 1 开始；stagger_min=0 时全用 base（同批同时）。
        """
        if not stagger_min or round_num <= 1:
            return base
        from datetime import datetime, timedelta
        try:
            dt = datetime.fromisoformat(base)
        except (ValueError, TypeError):
            return base
        return (dt + timedelta(minutes=stagger_min * (round_num - 1))).isoformat(timespec="seconds")

    def _version_snapshot(self, bot_a_id: int | None, bot_b_id: int | None) -> dict:
        """P1：发布轮时冻结 bot 版本（取各自 current_version 的 version_id）。

        返回 {bot_a_version_id, bot_b_version_id}；bot 不存在/无版本时对应值为 None。
        _run_match 读 version_id → bot_versions.binary_path，保证赛事用发布时的版本，
        不受选手中途上传新版本影响。
        """
        out: dict[str, Any] = {"bot_a_version_id": None, "bot_b_version_id": None}
        for key, bid in (("bot_a_version_id", bot_a_id), ("bot_b_version_id", bot_b_id)):
            if bid is None:
                continue
            v = self.store.get_latest_bot_version(bid)
            if v:
                out[key] = v["id"]
        return out

    async def _dispatch_pending(self, contest_id: int, stage_idx: int) -> None:
        c = self.store.get_contest(contest_id)
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        cfg = _match_config(c)  # 每游戏对局参数（holdem→hands, pencil→n_dots）
        gid = c.get("game_id") or "holdem"
        now = _now()
        # cfg 的键就是该游戏的 match_config 字段（holdem→{"hands"}, pencil→{"n_dots"},
        # 第 4 游戏自带其字段）。challenge() 透传整包，无需按字段名逐条硬判断。
        dispatched_any = False
        for p in pairings:
            if p.get("status") != "pending" or p.get("match_id"):
                continue
            # 逐场排期：scheduled_at 未到则跳过（等调度器到点再 dispatch）
            sched = p.get("scheduled_at")
            if sched and sched > now:
                continue
            # published 态首次 dispatch → 转 running（排期到点开打）
            if not dispatched_any and c.get("status") == CONTEST_PUBLISHED:
                self.store.update_contest(contest_id, status=CONTEST_RUNNING)
                dispatched_any = True
            # 冻结快照已在 pairing 行；直接开打
            # cfg 是该游戏的 match_config（holdem→{"hands"}, pencil→{"n_dots"}），
            # 整包传给 challenge(match_config=...)，无需按字段名逐条具名传递。
            mid = await self.orch.challenge(
                p["bot_a_id"],
                p["bot_b_id"],
                owner_user_id=c["organizer_id"],
                match_type=TYPE_CONTEST,
                contest_id=contest_id,
                game_id=gid,
                match_config=cfg,
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
        # 默认 scoring 从该游戏 spec 派生（而非硬编码 poker_3_1_0——否则棋类赛事被套 3-1-0）
        try:
            default_scoring = game_registry.get((c or {}).get("game_id") or "holdem").default_scoring
        except Exception:
            default_scoring = "poker_3_1_0"
        scoring = stage.get("scoring") or default_scoring

        entries = self.store.list_contest_entries(contest_id)
        # P0：排名/积分键改为 entry.id（换 Bot 不丢历史分）。
        # pairing 存 entry_a_id/entry_b_id（生成时快照），用它定位 stats；
        # match 的 winner(座位0/1) 对应 pairing 的 a/b 侧。
        stats = {
            e["id"]: {
                "entry_id": e["id"],
                "bot_id": e["bot_id"],
                "user_id": e["user_id"],
                "seed": e.get("seed") or 0,
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
            ea_id = p.get("entry_a_id")
            eb_id = p.get("entry_b_id")
            if ea_id not in stats or eb_id not in stats:
                continue
            # net_chips 从 result.deltas 取（取代旧 earnings_a/b 物理列）
            ea_earn, eb_earn = match_deltas(m)
            stats[ea_id]["net_chips"] += ea_earn
            stats[eb_id]["net_chips"] += eb_earn
            wa = points_for_result(scoring, m["winner"], 0)
            wb = points_for_result(scoring, m["winner"], 1)
            stats[ea_id]["points"] += wa
            stats[eb_id]["points"] += wb
            if m["winner"] == 0:
                stats[ea_id]["wins"] += 1
                stats[eb_id]["losses"] += 1
            elif m["winner"] == 1:
                stats[eb_id]["wins"] += 1
                stats[ea_id]["losses"] += 1
            else:
                stats[ea_id]["draws"] += 1
                stats[eb_id]["draws"] += 1
            gid = p.get("group_id") or ""
            if gid:
                stats[ea_id]["group_id"] = gid
                stats[eb_id]["group_id"] = gid
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
                s["entry_id"],
                bot_id=s.get("bot_id"),
                stage_key=key,
                points=s["points"],
                wins=s["wins"],
                draws=s["draws"],
                losses=s["losses"],
                net_chips=s["net_chips"],
                group_id=s.get("group_id") or "",
                rank_in_group=i + 1,
            )

    def _mark_stage_pairings_done(self, contest_id: int, stage_idx: int) -> None:
        """阶段真正完成时（_stage_done 通过后），把该 stage 已完成 match 的 pairing 标
        status='completed'。积分逻辑只读 match，不依赖 pairing.status——但前端对阵图 /
        管理端读 pairing.status 显示进度，原实现只在 dispatch 时设 'running'、从不收尾，
        导致阶段完成后 pairing 永显 running。"""
        for p in self.store.list_contest_pairings(contest_id, stage_idx=stage_idx):
            if p.get("status") == "completed":
                continue
            mid = p.get("match_id")
            if not mid:
                continue
            m = self.store.get_match(mid)
            if m and m["status"] in (STATUS_COMPLETED, STATUS_ABORTED):
                self.store.update_contest_pairing(p["id"], status="completed")

    def _advance_participants(self, contest_id: int, stage_idx: int) -> None:
        """根据阶段配置标记淘汰（不晋级者）。"""
        c = self.store.get_contest(contest_id)
        stages = _parse_stages(c)
        if stage_idx < 0 or stage_idx >= len(stages):
            return
        stage = stages[stage_idx]
        standings = self.standings(contest_id, stage_idx=stage_idx)
        # P0：advance 以 entry_id 为键（与 standings 一致，换 Bot 不影响晋级判定）
        advance: set[int] = set()
        if stage.get("advance_per_group"):
            per = int(stage["advance_per_group"])
            by_g: dict[str, list[dict]] = {}
            for s in standings:
                by_g.setdefault(s.get("group_id") or "_", []).append(s)
            for rows in by_g.values():
                for s in rows[:per]:
                    advance.add(s["entry_id"])
        elif stage.get("advance_count"):
            n = int(stage["advance_count"])
            for s in standings[:n]:
                advance.add(s["entry_id"])
        else:
            # 默认全部晋级（如单阶段 RR）
            advance = {s["entry_id"] for s in standings}

        for e in self.store.list_contest_entries(contest_id):
            if e["id"] not in advance:
                self.store.update_entry(contest_id, e["user_id"], eliminated=1)

    async def maybe_finish(self, contest_id: int) -> dict | None:
        """对局结束回调：检查当前阶段是否完成，进入 rest 或下一阶段。

        加 per-contest 锁串行化——防止多场对局同时完成的 on_match_done 并发回调
        + scheduler 并发调用导致重复生成轮次/重复对局。
        """
        async with self._lock(contest_id):
            return await self._maybe_finish_locked(contest_id)

    async def _maybe_finish_locked(self, contest_id: int) -> dict | None:
        """maybe_finish 的实际逻辑（调用方已持锁）。"""
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

        # 多轮赛制推进（500 人压测发现的 bug 修复）：
        # swiss / single_elimination 是「懒生成」轮次——_stage_done 只看现有 pairing 是否全完成，
        # 但 R1 完成时该阶段可能还需要更多轮（swiss 未到 total_rounds；淘汰赛胜者>1）。
        # 在判定阶段真正结束前，尝试生成下一轮；生成了则阶段未完成（return），否则继续。
        if stages and 0 <= stage_idx < len(stages):
            stage = stages[stage_idx]
            stype = stage.get("type") or ""
            if stype == "swiss":
                if await self._maybe_next_swiss_round(contest_id, stage_idx, stage):
                    return None  # 生成了下一轮，阶段未完成
            elif stype == "single_elimination":
                if await self._maybe_next_elim_round(contest_id, stage_idx, stage):
                    return None  # 生成了下一轮（半决赛/决赛），阶段未完成

        self._mark_stage_pairings_done(contest_id, stage_idx)
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
        # P2：末阶段完成 → 计算全员唯一正式名次（破同分链）并落库
        try:
            self._finalize_official_results(contest_id, stage_idx)
        except Exception:
            logger.exception("compute official results failed contest=%s", contest_id)
        return self.store.get_contest(contest_id)

    async def reconcile_running_contests(self) -> int:
        """启动对账：让所有 running/rest 的 contest 收敛到正确终态。

        解决三类「赛事卡 running」：
        1. match 全完成但 maybe_finish 回调丢失/异常被吞（生产 contest 25）→ 直接 maybe_finish。
        2. match 被 orphan_after_restart 清成 aborted，pairing 仍指它（生产 contest 24）→
           reset_dead_contest_pairings 复位后重派。
        3. pairing 建了 match 行但 _run_match 从未跑完（pending match，started_at=None）→
           识别为死 pairing 复位重派。

        maybe_finish 在 _stage_done=False 时只生成下一轮、不重派 pending pairing，
        所以对账须在 maybe_finish 之后显式 _dispatch_pending 死而复生的 pending pairing。
        返回处理的 contest 数。
        """
        # 1. 复位死 pairing（status=running 但 match 已 aborted/pending/不存在）→ pending+match_id=NULL
        reset_n = self.store.reset_dead_contest_pairings()
        if reset_n:
            logger.info("启动对账：复位 %d 个死 pairing（待重派或标 aborted）", reset_n)

        contests = self.store.list_contests_by_status(
            [CONTEST_RUNNING, CONTEST_REST]
        )
        for c in contests:
            cid = c["id"]
            try:
                await self._reconcile_one(cid)
            except Exception:
                # 单个 contest 对账失败不阻塞其他——但必须可见（防静默卡死再复发）
                logger.exception("reconcile contest %s failed", cid)
        return len(contests)

    async def _reconcile_one(self, contest_id: int) -> None:
        """对账单个 contest：maybe_finish → 重派 pending → 再 maybe_finish。"""
        # 第一轮 maybe_finish：能 finish 的直接 finish（match 全完成的场景）
        await self.maybe_finish(contest_id)
        c = self.store.get_contest(contest_id)
        if not c or c["status"] not in (CONTEST_RUNNING, CONTEST_REST):
            return  # 已 finish/advance
        if c["status"] == CONTEST_REST:
            return  # rest 期交由 _maybe_auto_resume（启动时点未到则等）

        stage_idx = int(c.get("current_stage_idx") or 0)
        # 第二轮：重派 pending 无 match_id 的 pairing（死而复生 + 新生成轮）。
        # _dispatch_pending 内部 challenge() 可能抛 ValueError（bot 已删/不可用）——
        # 此时该 pairing 挂一条 aborted match（_stage_done 接受 aborted），让阶段仍能推进。
        await self._dispatch_pending_safe(contest_id, stage_idx)
        # 第三轮：重派/标 aborted 后再 maybe_finish，让阶段真正推进
        await self.maybe_finish(contest_id)

    async def _dispatch_pending_safe(
        self, contest_id: int, stage_idx: int
    ) -> None:
        """重派 pending pairing，对单个 pairing 的 bot 不可用做容错（标 aborted 而非整体崩溃）。

        _dispatch_pending 是批量 dispatch，任一 pairing 的 bot 删了会抛 ValueError 中断后续。
        此方法逐 pairing try/except：失败则给该 pairing 挂一条 aborted match
        （reason='contest_bot_unavailable'），保证 _stage_done 仍通过。
        """
        c = self.store.get_contest(contest_id)
        if not c:
            return
        cfg = _match_config(c)
        gid = c.get("game_id") or "holdem"
        pending = [
            p
            for p in self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
            if p.get("status") == "pending" and not p.get("match_id")
        ]
        for p in pending:
            try:
                kw: dict = {
                    "match_type": TYPE_CONTEST,
                    "contest_id": contest_id,
                    "game_id": gid,
                }
                kw.update({k: int(v) for k, v in cfg.items() if v is not None})
                mid = await self.orch.challenge(
                    p["bot_a_id"],
                    p["bot_b_id"],
                    owner_user_id=c["organizer_id"],
                    **kw,
                )
                self.store.update_contest_pairing(p["id"], match_id=mid, status="running")
            except Exception as exc:
                # bot 已删/不可用：建 aborted match 挂回 pairing，让 _stage_done 通过
                logger.warning(
                    "reconcile: contest=%s pairing=%s 重派失败，标记 aborted: %s",
                    contest_id, p["id"], exc,
                )
                mid = self._force_aborted_match_row(
                    c, p, reason="contest_bot_unavailable"
                )
                self.store.update_contest_pairing(p["id"], match_id=mid, status="running")

    def _force_aborted_match_row(self, contest: dict, pairing: dict, *, reason: str) -> str:
        """bot 不可用时建一条 aborted match 行挂回 pairing（绕过 challenge 的 bot 校验）。

        contest 对局的 bot 可能已被删（owner_id 改变/标 inactive）——challenge() 会拒。
        但赛事要能推进，必须让该 pairing 有终态 match。用 0 占位 bot id 建行后立即标 aborted。
        """
        from datetime import datetime
        import secrets
        mid = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
        gid = (contest.get("game_id") or "holdem").lower()
        self.store.create_match(
            mid,
            bot_a_id=pairing.get("bot_a_id") or 0,
            bot_b_id=pairing.get("bot_b_id") or 0,
            owner_id=contest.get("organizer_id"),
            contest_id=contest["id"],
            match_type=TYPE_CONTEST,
            game_id=gid,
            match_config={},
        )
        self.store.update_match(
            mid, status=STATUS_ABORTED, reason=reason, ended_at=_now(),
        )
        return mid

    def _finalize_official_results(self, contest_id: int, stage_idx: int) -> None:
        """计算全员正式名次（破同分）并落库 contest_official_results。

        若末阶段 stage.ranking_mode=replace_top：合成榜（1..scope 取末阶段 Top，
        scope+1..N 取前一阶段未晋级者相对序）。
        """
        from bzplat.backend.contests import ranking as _ranking
        from bzplat.backend.games import registry as _reg

        c = self.store.get_contest(contest_id)
        if not c:
            return
        stages = _parse_stages(c)
        cur_stage = stages[stage_idx] if 0 <= stage_idx < len(stages) else {}
        gid = (c.get("game_id") or "holdem").lower()
        try:
            normalize_earnings = _reg.get(gid).normalize_earnings
        except Exception:
            normalize_earnings = None

        def _rank_stage(sidx: int) -> list[dict]:
            standings = self.standings(contest_id, stage_idx=sidx)
            pairings = self.store.list_contest_pairings(contest_id, stage_idx=sidx)
            match_ids = [p["match_id"] for p in pairings if p.get("match_id")]
            matches = {mid: self.store.get_match(mid) for mid in match_ids if mid}
            matches = {k: v for k, v in matches.items() if v}
            return _ranking.compute_official_ranking(
                standings, pairings, matches, normalize_earnings=normalize_earnings
            )

        ranking_rows = _rank_stage(stage_idx)
        # replace_top 合成榜（决赛：末阶段 Top8 + 前一阶段未晋级者）
        if cur_stage.get("ranking_mode") == "replace_top" and stage_idx > 0:
            scope = int(cur_stage.get("ranking_scope") or 8)
            stage1_ranking = _rank_stage(stage_idx - 1)
            ranking_rows = _ranking.merge_replace_top(stage1_ranking, ranking_rows, scope=scope)
        _ranking.persist_official_results(
            self.store, contest_id, ranking_rows, stage_idx=stage_idx
        )

    async def _maybe_next_swiss_round(
        self, contest_id: int, stage_idx: int, stage: dict
    ) -> bool:
        """瑞士轮当前轮完成后生成下一轮。返回是否生成了新一轮（True=阶段未完成）。"""
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        if not pairings:
            return False
        max_round = max(int(p.get("round_num") or 1) for p in pairings)
        # 当前轮是否全部结束
        cur = [p for p in pairings if int(p.get("round_num") or 1) == max_round]
        for p in cur:
            mid = p.get("match_id")
            if not mid:
                return False
            m = self.store.get_match(mid)
            if not m or m["status"] not in (STATUS_COMPLETED, STATUS_ABORTED):
                return False
        total_rounds = int(stage.get("rounds") or swiss_rounds_needed(
            len(self.store.list_contest_entries(contest_id))
        ))
        if max_round >= total_rounds:
            return False
        # 生成下一轮（P0：standings 键 entry_id；P1：bot_id 取 entry 当前值——
        # dispatch 换 Bot 后下一轮用新 Bot，已发布轮冻结不受影响）
        standings = self.standings(contest_id, stage_idx=stage_idx)
        # entry_id → 该 entry 当前 bot_id（dispatch 后是新 Bot）
        entries = {e["id"]: e for e in self.store.list_contest_entries(contest_id)}
        entry_to_bot = {s["entry_id"]: entries.get(s["entry_id"], {}).get("bot_id") for s in standings}
        # 仍用发布轮的 bot_id 算 scores/played（积分/对手历史键稳定，不变）
        scores = {}
        bot_to_entry = {}
        for s in standings:
            cur_bot = entry_to_bot.get(s["entry_id"])
            if cur_bot is not None:
                scores[cur_bot] = s["points"]
                bot_to_entry[cur_bot] = s["entry_id"]
        bot_ids = [
            entry_to_bot[s["entry_id"]]
            for s in standings
            if not s.get("eliminated") and entry_to_bot.get(s["entry_id"]) is not None
        ]
        played: set[tuple[int, int]] = set()
        for p in pairings:
            if p.get("bot_a_id") is not None and p.get("bot_b_id") is not None:
                played.add((min(p["bot_a_id"], p["bot_b_id"]), max(p["bot_a_id"], p["bot_b_id"])))
        specs = generate_stage_pairings(
            stage, bot_ids, scores=scores, played=played, swiss_round=max_round + 1
        )
        key = stage.get("key") or f"stage{stage_idx}"
        published_at = _now()
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
                entry_a_id=bot_to_entry.get(sp.bot_a_id),
                entry_b_id=bot_to_entry.get(sp.bot_b_id),
                published_at=published_at,
                **self._version_snapshot(sp.bot_a_id, sp.bot_b_id),
            )
        await self._dispatch_pending(contest_id, stage_idx)
        return True

    async def _maybe_next_elim_round(
        self, contest_id: int, stage_idx: int, stage: dict
    ) -> bool:
        """单败淘汰：当前轮完成后用胜者生成下一轮（半决赛/决赛）。返回是否生成了新一轮。

        复现修复：500 人压测发现 KO 只跑四分之一就 finished——_stage_done 只看现有 pairing，
        但 single_elimination 只生成首轮，后续轮需根据胜者推进。
        """
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        if not pairings:
            return False
        max_round = max(int(p.get("round_num") or 1) for p in pairings)
        cur = [p for p in pairings if int(p.get("round_num") or 1) == max_round]
        # 当前轮全部完成
        winners: list[tuple[int, int | None]] = []  # (bot_id, entry_id)
        for p in cur:
            mid = p.get("match_id")
            if not mid:
                return False
            m = self.store.get_match(mid)
            if not m or m["status"] not in (STATUS_COMPLETED, STATUS_ABORTED):
                return False
            w = m.get("winner")
            if w is None:
                # 平局/异常：取 bot_a 兜底（淘汰赛不应平局，但兜底防卡死）
                w = 0
            if w == 0:
                winners.append((p["bot_a_id"], p.get("entry_a_id")))
            else:
                winners.append((p["bot_b_id"], p.get("entry_b_id")))
        # 胜者 ≤1 → 已决出冠军，阶段真正完成
        if len(winners) <= 1:
            return False
        # 用胜者生成下一轮（按 bracket_slot 顺序配对：相邻两胜者一组）
        key = stage.get("key") or f"stage{stage_idx}"
        next_round = max_round + 1
        published_at = _now()
        slot = 0
        for i in range(0, len(winners) - 1, 2):
            a_bot, a_entry = winners[i]
            b_bot, b_entry = winners[i + 1]
            self.store.add_contest_pairing(
                contest_id, a_bot, b_bot,
                round_num=next_round, status="pending",
                stage_idx=stage_idx, stage_key=key,
                bracket_slot=slot, color_first=0,
                entry_a_id=a_entry, entry_b_id=b_entry,
                published_at=published_at,
                **self._version_snapshot(a_bot, b_bot),
            )
            slot += 1
        await self._dispatch_pending(contest_id, stage_idx)
        return True

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
        # P3：estimate 按 advance_count 传播各 stage 人数。
        # stage1 用 n；stage2 用 stage1 的 advance_count（决赛 Top8 循环等）。
        total = 0
        cur_n = n
        for st in stages:
            total += estimate_match_count(st, cur_n)
            ac = st.get("advance_count")
            if ac and int(ac) > 0:
                cur_n = int(ac)
        conc_raw = self.store.get_setting("max_concurrent_matches")
        try:
            conc = max(1, int(conc_raw or 2))
        except ValueError:
            conc = 2
        # 粗估每场时长：经 spec.eta_for_match（消除 if game_id）
        gid = c.get("game_id") or "holdem"
        cfg = _match_config(c)
        sec_per = _estimate_sec_per_match(gid, cfg)
        eta_sec = (total / conc) * sec_per if conc else 0
        return {
            "entries": n,
            "estimated_matches": total,
            "max_concurrent": conc,
            "eta_seconds": int(eta_sec),
        }
