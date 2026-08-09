"""组织者比赛：阶段模板、休息换 Bot、对阵调度。"""
from __future__ import annotations

import asyncio
import json
import logging
from collections import Counter
from datetime import datetime, timedelta
from typing import Any

from bzplat.backend.contests.stages import (
    PairingSpec,
    estimate_match_count,
    generate_stage_pairings,
    swiss_rounds_needed,
)
from bzplat.backend.contests.templates import (
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
from bzplat.backend.store.validation import (
    validate_contest_times as _validate_contest_times,
)
from bzplat.backend.store.schema import (
    CONTEST_CANCELLED,
    CONTEST_DRAFT,
    CONTEST_FINISHED,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    CONTEST_REST,
    CONTEST_RUNNING,
    REGISTERED_ENGINES,
    SETTING_FULL_RR_MAX_N,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
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
    """赛事对局配置。游戏规则参数（手数/棋盘/点阵）已由 GameSpec 钉死固定值，
    不再从 match_config_json/hands_per_match 读取；此处保留空 dict 占位
    （orchestrator.challenge 不再消费游戏规则参数，仅用版本快照等内部键）。
    """
    return {}


def _estimate_sec_per_match(gid: str, cfg: dict) -> int:
    """粗估每场时长（秒）：经 spec.eta_for_match（各游戏已钉死固定 ETA）。"""
    return game_registry.get(gid).eta_for_match(cfg)


class ContestManager:
    def __init__(self, store: Store, orch: MatchOrchestrator) -> None:
        self.store = store
        self.orch = orch
        # per-contest 锁：串行化所有写状态路径（start/publish/cancel/resume/advance/
        # maybe_finish/_dispatch_pending），防止请求与 scheduler/on_match_done 并发导致
        # 重复生成轮次或取消后继续派发。
        self._locks: dict[int, asyncio.Lock] = {}

    def _lock(self, contest_id: int) -> asyncio.Lock:
        """取（或建）该 contest 的锁。

        P1-9 修复：finished/cancelled 的 contest 锁永不清理导致无界增长。
        惰性清理——超阈值时回收空闲锁（locked()=False 的已结束赛事）。
        """
        lk = self._locks.get(contest_id)
        if lk is None:
            if len(self._locks) > 500:
                self._locks = {k: v for k, v in self._locks.items() if v.locked()}
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
        # 自定义 stages 直接用；否则从模板表（含 admin 覆盖）解析 stages
        if stages:
            tid = template_id or "custom"
            gid = (game_id or "holdem").strip().lower()
            # 即使调用方同时传入自定义 stages，也不能借此把一个具名模板标成
            # 另一款游戏。该组合会污染赛事快照，后续按 gid 启动错误裁判。
            if template_id:
                declared_template = (
                    self.store.get_contest_template(template_id) or get_template(template_id)
                )
                if declared_template:
                    template_gid = str(declared_template["game_id"]).strip().lower()
                    if gid != template_gid:
                        raise ValueError(
                            f"模板 {template_id} 属于游戏 {template_gid}，不能用于游戏 {gid}"
                        )
            stage_list = stages
        else:
            tid, gid, stage_list, _tpl_mc = resolve_template(
                template_id, game_id=game_id, store=self.store
            )
        # P5：phase 优先级：显式传入 > 模板自带 phase > standalone
        if phase == "standalone":
            tpl = get_template(tid)
            if tpl and tpl.get("phase"):
                phase = tpl["phase"]
        # 游戏规则参数（手数/棋盘/点阵）已由 GameSpec 钉死，赛事不再存 match_config；
        # match_config_json 落空 dict（DB 列保留向后兼容，但不再承载游戏规则）。
        cfg: dict = {}
        # 时间校验：开放报名 <= 截止报名 <= 开赛（相同秒合法）
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

    async def open_registration(self, contest_id: int) -> dict:
        """手动开放报名；与发布、开赛等生命周期写路径共用赛事锁。"""
        async with self._lock(contest_id):
            return self._open_registration_locked(contest_id)

    def _open_registration_locked(self, contest_id: int) -> dict:
        """draft→open 的实际逻辑（调用方已持 per-contest 锁）。

        重复 open 是幂等读；其他状态不得倒退为 open。若
        手动提前开放时，以实际开放时刻覆盖未来计划；已到点的计划时间保留。
        """
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        if c["status"] == CONTEST_OPEN:
            return c
        if c["status"] != CONTEST_DRAFT:
            raise ValueError(f"赛事处于 {c['status']} 态，不能开放报名（仅 draft 可开放）")
        now = _now()
        # Legacy/manual data may only have a past close/start time.  Opening the
        # contest must not manufacture ``opens > closes/starts``; use the earliest
        # known lifecycle timestamp.  Registration will then immediately reject
        # callers when that close time has already elapsed.
        opens = min(
            (
                value
                for value in (
                    c.get("registration_opens_at"),
                    c.get("registration_closes_at"),
                    c.get("starts_at"),
                    now,
                )
                if value is not None
            ),
            key=datetime.fromisoformat,
        )
        return self.store.update_contest(
            contest_id, status=CONTEST_OPEN, registration_opens_at=opens
        )

    async def register(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        role: str = "",
    ) -> dict:
        """报名；与 publish/start 共用赛事锁，杜绝关报名后晚插 entry。"""
        async with self._lock(contest_id):
            return self._register_locked(contest_id, user_id, bot_id, role=role)

    def _register_locked(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        role: str = "",
    ) -> dict:
        """register 的锁内实现；Store 写入时还会在同事务复核 open 状态。"""
        # ``role`` 仅为旧调用签名兼容保留。普通 /register 入口永远是本人操作；
        # organizer/admin 的代报名必须走已校验赛事归属的 entries 管理接口。
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
        if bot["owner_id"] != user_id:
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
        return self.store.add_contest_entry_once(contest_id, owner_id, bot_id)

    def _roster_target_error(
        self, contest: dict, user_id: int, bot_id: int
    ) -> str | None:
        if not self.store.get_user(user_id):
            return f"user {user_id} 不存在"
        bot = self.store.get_bot(bot_id)
        if not bot or not bot.get("is_active") or not bot.get("binary_path"):
            return f"bot {bot_id} 不可用"
        if bot.get("owner_id") != user_id:
            return f"bot {bot_id} 不属于 user {user_id}"
        contest_game = (contest.get("game_id") or "holdem").strip().lower()
        bot_game = (bot.get("game_id") or "holdem").strip().lower()
        if bot_game != contest_game:
            return f"bot {bot_id} 游戏 {bot_game} ≠ 赛事 {contest_game}"
        return None

    async def add_roster_entry(
        self, contest_id: int, user_id: int, bot_id: int
    ) -> dict:
        """组织者/admin 单条代报名；仅 draft/open，且与 publish 共用赛事锁。"""
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            error = self._roster_target_error(contest, user_id, bot_id)
            if error:
                raise ValueError(error)
            added, skipped = self.store.add_contest_roster_entries(
                contest_id, [(user_id, bot_id)]
            )
            if skipped or not added:
                raise ValueError("该用户已报名")
            return added[0]

    async def assign_roster_entries(
        self, contest_id: int, targets: list[tuple[int, int]]
    ) -> dict:
        """组织者/admin 批量代报名；校验后整批在 Store 单事务写入。"""
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")

            skipped: list[str] = []
            valid: list[tuple[int, int]] = []
            seen: set[int] = set()
            for user_id, bot_id in targets:
                if user_id in seen:
                    skipped.append(f"user {user_id} 重复，跳过")
                    continue
                seen.add(user_id)
                error = self._roster_target_error(contest, user_id, bot_id)
                if error:
                    skipped.append(f"{error}，跳过")
                    continue
                valid.append((user_id, bot_id))

            added, duplicate_users = self.store.add_contest_roster_entries(
                contest_id, valid
            )
            skipped.extend(
                f"user {user_id} 已报名，跳过" for user_id in duplicate_users
            )
            return {
                "added": len(added),
                "skipped": skipped,
                "total_entries": len(self.store.list_contest_entries(contest_id)),
            }

    async def delete_roster_entry(self, contest_id: int, user_id: int) -> bool:
        """组织者/admin 删名册；仅 draft/open，且与 publish 共用赛事锁。"""
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            return self.store.delete_contest_roster_entry(contest_id, user_id)

    async def dispatch(
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

        P1-4 修复：加 per-contest 锁，与 scheduler 的 resume/_begin_stage 串行化，
        防 bot 交换与下一阶段配对生成竞态（旧代码无锁，TOCTOU 导致配对指向错误 bot/version）。
        """
        async with self._lock(contest_id):
            return await self._dispatch_locked(contest_id, user_id, bot_id, role=role)

    async def _dispatch_locked(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        role: str = "",
    ) -> dict:
        # ``role`` 仅为旧调用签名兼容保留。普通 /dispatch 只允许当前用户更新
        # 自己的 entry；代理名册操作必须走 organizer/admin 专用 entries 接口。
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        # 换人时机：开赛前（draft/open/published）+ 中场休息（rest，受 allow_bot_swap_in_rest 控制）。
        # 不允许 running 态换人（与赛程对齐：比赛中途换 Bot 影响公平性）。
        if c["status"] not in (CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED, CONTEST_REST):
            raise ValueError("当前状态不可更换 Bot（仅开赛前或休息期可换）")
        stages = _parse_stages(c)
        idx = int(c.get("current_stage_idx") or 0)
        stage = stages[idx] if 0 <= idx < len(stages) else {}
        if c["status"] == CONTEST_REST and not stage.get("allow_bot_swap_in_rest", True):
            raise ValueError("本阶段休息不允许换 Bot")

        bot = self.store.get_bot(bot_id)
        if not bot:
            raise ValueError("bot 不存在")
        if bot["owner_id"] != user_id:
            raise ValueError("只能派遣自己的 bot")
        if not bot.get("is_active") or not bot.get("binary_path"):
            raise ValueError("bot 不可用")
        contest_game = (c.get("game_id") or "holdem").lower()
        bot_game = (bot.get("game_id") or "holdem").lower()
        if bot_game != contest_game:
            raise ValueError(
                f"Bot 游戏类型 ({bot_game}) 与比赛 ({contest_game}) 不一致"
            )

        entry = self.store.get_entry(contest_id, user_id)
        if not entry:
            raise ValueError("未报名本比赛")

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

    def _bot_unavailable_reason(
        self, bot_id: int | None, *, expected_game: str
    ) -> str | None:
        """返回赛事 Bot 不可用原因；可用时返回 None。

        发布/开赛与中途重派必须共用同一套判定，否则会出现
        “发布时看似可用，实际派发时才失败”的空壳赛事。
        """
        if bot_id is None:
            return "Bot 引用已缺失"
        bot = self.store.get_bot(bot_id)
        if not bot:
            return f"Bot #{bot_id} 不存在"
        if not bot.get("is_active"):
            return f"Bot #{bot_id} 已停用"
        if not bot.get("binary_path"):
            return f"Bot #{bot_id} 未上传可执行文件"
        bot_game = str(bot.get("game_id") or "holdem").lower()
        if bot_game != expected_game:
            return f"Bot #{bot_id} 游戏为 {bot_game}，赛事游戏为 {expected_game}"
        return None

    def _validate_initial_roster(self, contest: dict, entries: list[dict]) -> None:
        """发布/开赛前在赛事锁内复核名册可运行性。

        不允许过滤掉坏 entry 后静默开赛：那会让报名者无声消失。
        只有全部报名 entry 均有 active + binary + 游戏匹配的 Bot，
        且总数至少 2，才能生成公平的首阶段对阵。
        开赛初始化会重置历史 eliminated 标记，因此校验不能先按该标记
        过滤，否则会把实际将参赛的人漏掉。
        """
        game_id = str(contest.get("game_id") or "holdem").lower()
        active_entries = entries
        issues: list[str] = []
        for entry in active_entries:
            reason = self._bot_unavailable_reason(
                entry.get("bot_id"), expected_game=game_id
            )
            if reason:
                issues.append(f"报名 #{entry.get('id')}: {reason}")
        if len(active_entries) < 2 or issues:
            detail = "；".join(issues[:5])
            suffix = f"：{detail}" if detail else ""
            raise ValueError(f"至少需要 2 名持有可用 Bot 的参赛者{suffix}")

    async def start(self, contest_id: int) -> dict:
        """立即开赛（手动触发，跳过排期等待）。

        - **open/draft**：生成对阵 + 设 scheduled_at=now（立即开打）+ dispatch 全部。
        - **published**：排期已发布（pairing 已生成），**不重新生成**——仅把现有 pending
          pairing 的 scheduled_at 改成 now（立即到点）+ dispatch。避免重复生成 pairing。
        若要走两阶段（截止报名→出排期→到开赛时间再开打），用 publish() + 调度器。
        """
        async with self._lock(contest_id):
            return await self._start_locked(contest_id)

    async def _start_locked(self, contest_id: int) -> dict:
        """start 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        if c["status"] not in (CONTEST_OPEN, CONTEST_DRAFT, CONTEST_PUBLISHED):
            raise ValueError("仅 open/draft/published 可开赛")
        game_id = c.get("game_id") or "holdem"
        self._assert_engine(game_id)

        # 必须先校验、后改 scheduled_at/status；校验失败时整个
        # start 对赛事状态与已发布排期零副作用。
        entries = self.store.list_contest_entries(contest_id)
        self._validate_initial_roster(c, entries)

        # published 态：pairing 已存在，直接改 scheduled_at=now 立即开打（不重新生成）
        if c["status"] == CONTEST_PUBLISHED:
            now = _now()
            stage_idx = int(c.get("current_stage_idx") or 0)
            # 硬崩可能留下“有行但只有半批”的首阶段。手动开赛前
            # 先做完整性对账，不得只把残缺的几场改成 now 就开打。
            self._ensure_published_pairings_locked(contest_id, stage_idx)
            pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
            old_match_ids = {p["id"]: p.get("match_id") for p in pairings}
            old_schedules = {p["id"]: p.get("scheduled_at") for p in pairings}
            old_opens_at = c.get("registration_opens_at")
            old_closes_at = c.get("registration_closes_at")
            old_starts_at = c.get("starts_at")
            old_rest_ends_at = c.get("rest_ends_at")
            for p in pairings:
                if p.get("status") == "pending" and not p.get("match_id"):
                    self.store.update_contest_pairing(p["id"], scheduled_at=now)
            planned_opens = c.get("registration_opens_at")
            opens = (
                planned_opens
                if planned_opens
                and datetime.fromisoformat(planned_opens) <= datetime.fromisoformat(now)
                else now
            )
            self.store.update_contest(
                contest_id,
                registration_opens_at=opens,
                registration_closes_at=now,
                starts_at=now,
                rest_ends_at=None,
            )
            try:
                await self._dispatch_pending_locked(contest_id, stage_idx)
            except Exception:
                # challenge 在首场成功前失败：仍是 published，尚无新 match，可精确恢复
                # 原排期供组织者修复后重试。若已有 pairing 成功派发，状态已是 running，
                # 保留已发生的真实进度，剩余 pending 由 scheduler 收敛。
                current = self.store.get_contest(contest_id)
                refreshed = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
                started = any(
                    not old_match_ids.get(q["id"]) and q.get("match_id")
                    for q in refreshed
                )
                if current and current["status"] == CONTEST_PUBLISHED and not started:
                    for p in refreshed:
                        if p["id"] in old_schedules and not p.get("match_id"):
                            self.store.update_contest_pairing(
                                p["id"], scheduled_at=old_schedules[p["id"]]
                            )
                    self.store.update_contest(
                        contest_id,
                        registration_opens_at=old_opens_at,
                        registration_closes_at=old_closes_at,
                        starts_at=old_starts_at,
                        rest_ends_at=old_rest_ends_at,
                    )
                raise
            return self.store.get_contest(contest_id)

        stages = _parse_stages(c)
        if not stages:
            _, _, stages = resolve_stages(
                c.get("template_id") or "holdem_swiss_ko", store=self.store
            )

        self._guard_full_rr(stages, len(entries))

        snapshot = self._initial_lifecycle_snapshot(c, entries)
        try:
            now = _now()
            planned_opens = c.get("registration_opens_at")
            opens_at = (
                planned_opens
                if planned_opens
                and datetime.fromisoformat(planned_opens) <= datetime.fromisoformat(now)
                else now
            )
            self._prepare_initial_contest(
                contest_id,
                entries,
                stages,
                opens_at=opens_at,
                closes_at=now,
                starts_at=now,
            )
            await self._begin_stage(
                contest_id,
                0,
                schedule_immediately=True,
                dispatch_pending=False,
                activate_running=False,
            )
            await self._dispatch_pending_locked(contest_id, 0)
        except Exception:
            self._rollback_initial_lifecycle(contest_id, snapshot)
            raise
        return self.store.get_contest(contest_id)

    async def publish(self, contest_id: int) -> dict:
        """截止报名 + 出排期（status=open→published）。

        生成对阵 + 逐场排期 scheduled_at + 冻结版本，但**不 dispatch**——等开赛时间到
        调度器到点 dispatch（scheduled_at<=now 的 pairing 才开打）。
        组织者可手动调本方法提前出排期；调度器到 registration_closes_at 自动调。
        """
        async with self._lock(contest_id):
            return await self._publish_locked(contest_id)

    async def _publish_locked(self, contest_id: int) -> dict:
        """publish 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        if c["status"] not in (CONTEST_OPEN, CONTEST_DRAFT):
            raise ValueError("仅 open/draft 可出排期")
        game_id = c.get("game_id") or "holdem"
        self._assert_engine(game_id)

        entries = self.store.list_contest_entries(contest_id)
        self._validate_initial_roster(c, entries)

        stages = _parse_stages(c)
        if not stages:
            _, _, stages = resolve_stages(
                c.get("template_id") or "holdem_swiss_ko", store=self.store
            )

        self._guard_full_rr(stages, len(entries))

        snapshot = self._initial_lifecycle_snapshot(c, entries)
        try:
            # 截止报名盖戳：手动提前发布时使用实际时刻；调度器到点发布时
            # 保留原计划时刻。这样不会留下 closes_at > starts_at 的倒挂时间线。
            # 先完整生成排期、但不 dispatch；这样生成失败可删除本次未启动 pairing
            # 并恢复原状态，不会出现 published/running 空壳赛事。
            now = _now()
            planned_opens = c.get("registration_opens_at")
            planned_closes = c.get("registration_closes_at")
            now_dt = datetime.fromisoformat(now)
            opens_at = (
                planned_opens
                if planned_opens and datetime.fromisoformat(planned_opens) <= now_dt
                else now
            )
            closes_at = (
                planned_closes
                if planned_closes and datetime.fromisoformat(planned_closes) <= now_dt
                else now
            )
            starts_at = c.get("starts_at")
            if (
                starts_at is not None
                and datetime.fromisoformat(starts_at)
                < datetime.fromisoformat(closes_at)
            ):
                starts_at = closes_at
            self._prepare_initial_contest(
                contest_id,
                entries,
                stages,
                opens_at=opens_at,
                closes_at=closes_at,
                starts_at=starts_at,
            )
            await self._begin_stage(
                contest_id,
                0,
                schedule_immediately=False,
                dispatch_pending=False,
                activate_running=False,
            )
        except Exception:
            self._rollback_initial_lifecycle(contest_id, snapshot)
            raise
        return self.store.get_contest(contest_id)

    def _initial_lifecycle_snapshot(self, contest: dict, entries: list[dict]) -> dict:
        """记录初始阶段会修改的最小字段，供失败补偿。调用方须持赛事锁。"""
        return {
            "contest": {
                key: contest.get(key)
                for key in (
                    "status",
                    "registration_opens_at",
                    "registration_closes_at",
                    "starts_at",
                    "stages_json",
                    "current_stage_idx",
                    "rest_ends_at",
                )
            },
            "entries": {
                e["user_id"]: {
                    "seed": e.get("seed") or 0,
                    "eliminated": int(e.get("eliminated") or 0),
                }
                for e in entries
            },
            "pairing_ids": {
                p["id"] for p in self.store.list_contest_pairings(contest["id"])
            },
        }

    def _prepare_initial_contest(
        self,
        contest_id: int,
        entries: list[dict],
        stages: list[dict],
        *,
        opens_at: str,
        closes_at: str,
        starts_at: str | None,
    ) -> None:
        """写入首阶段 seed 与 published 准备态；调用方须持赛事锁。"""
        for i, entry in enumerate(entries):
            self.store.update_entry(
                contest_id, entry["user_id"], seed=i + 1, eliminated=0
            )
        self.store.update_contest(
            contest_id,
            status=CONTEST_PUBLISHED,
            registration_opens_at=opens_at,
            registration_closes_at=closes_at,
            starts_at=starts_at,
            stages_json=json.dumps(stages, ensure_ascii=False),
            current_stage_idx=0,
            rest_ends_at=None,
        )

    def _rollback_initial_lifecycle(self, contest_id: int, snapshot: dict) -> bool:
        """首阶段生成/首次派发失败时做保守补偿。

        仅当赛事仍为 published 且本次新增 pairing 全部未绑定 match 时回滚；若已有
        对局成功派发，真实状态应保留为 running，剩余 pending 交给 scheduler 重试。
        因调用方仍持 per-contest 锁，补偿不会覆盖 cancel/start 等合法生命周期变化。
        """
        current = self.store.get_contest(contest_id)
        original_status = snapshot["contest"]["status"]
        if not current or current["status"] not in (CONTEST_PUBLISHED, original_status):
            return False
        before_ids = snapshot["pairing_ids"]
        generated = [
            p for p in self.store.list_contest_pairings(contest_id)
            if p["id"] not in before_ids
        ]
        if any(p.get("match_id") for p in generated):
            return False
        generated_ids = [p["id"] for p in generated]
        deleted = self.store.delete_unstarted_contest_pairings(contest_id, generated_ids)
        if deleted != len(generated_ids):
            logger.error(
                "contest lifecycle rollback refused: contest=%s expected_pairings=%s deleted=%s",
                contest_id,
                len(generated_ids),
                deleted,
            )
            return False
        for user_id, fields in snapshot["entries"].items():
            self.store.update_entry(contest_id, user_id, **fields)
        self.store.update_contest(contest_id, **snapshot["contest"])
        return True

    @staticmethod
    def _materialize_pairing_seats(spec: PairingSpec) -> tuple[int, int | None]:
        """Turn PairingSpec.color_first into the durable seat 0/1 A/B order.

        Pairing generators keep a stable conceptual A/B identity while choosing
        which side should move first.  Persistence and every downstream consumer
        use A as authoritative seat 0, so a ``color_first=1`` spec is swapped here
        and stored with the normalized ``color_first=0`` representation.
        """
        bot_a_id = spec.bot_a_id
        bot_b_id = spec.bot_b_id
        if int(spec.color_first or 0) == 1 and bot_b_id is not None:
            return bot_b_id, bot_a_id
        return bot_a_id, bot_b_id

    def _stage_pairing_plan(
        self, contest: dict, stage_idx: int
    ) -> tuple[dict, list, dict[int, int]]:
        """纯计算当前阶段首批 pairing spec，不产生 DB 副作用。

        publish 硬崩恢复必须用与 ``_begin_stage`` 完全相同的规则重算
        期望批次，否则只按行数判断会把“数量相同但参赛者错了”的
        损坏数据误当完整。首阶段没有“上一阶段积分”，不读当前残缺
        pairing 的 standings，避免已落盘 bye 分反过来改变恢复排序。
        """
        stages = _parse_stages(contest)
        if stage_idx < 0 or stage_idx >= len(stages):
            raise ValueError("赛事当前阶段不存在")
        stage = stages[stage_idx]
        entries = [
            entry
            for entry in self.store.list_contest_entries(contest["id"])
            if not entry.get("eliminated")
        ]
        prior_scores: dict[int, float] = {}
        if stage_idx > 0:
            prior_scores = {
                row["entry_id"]: row["points"]
                for row in self.standings(contest["id"], stage_idx=stage_idx - 1)
            }
        entries.sort(
            key=lambda entry: (
                -prior_scores.get(entry["id"], 0),
                entry.get("seed") or 0,
            )
        )
        bot_ids = [
            entry["bot_id"] for entry in entries if entry.get("bot_id") is not None
        ]
        bot_to_entry = {
            entry["bot_id"]: entry["id"]
            for entry in entries
            if entry.get("bot_id") is not None
        }
        if len(bot_ids) < 2 and stage.get("type") != "single_elimination":
            return stage, [], bot_to_entry
        if stage.get("type") == "swiss":
            rounds = int(stage.get("rounds") or 0) or swiss_rounds_needed(len(bot_ids))
            stage = {**stage, "rounds": rounds}
            specs = generate_stage_pairings(stage, bot_ids, swiss_round=1)
        else:
            specs = generate_stage_pairings(stage, bot_ids)
        return stage, specs, bot_to_entry

    async def _begin_stage(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        schedule_immediately: bool = False,
        dispatch_pending: bool = True,
        activate_running: bool = True,
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
        stage, specs, bot_to_entry = self._stage_pairing_plan(c, stage_idx)
        # specs 为空（如 single_elimination 收到 <2 bot → 无对手）：阶段无对阵 →
        # 直接 finished（防 maybe_finish 反复尝试空阶段）。
        if not specs:
            self.store.update_contest(
                contest_id, status=CONTEST_FINISHED, ends_at=_now(), rest_ends_at=None
            )
            return

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
        pairing_rows: list[dict[str, Any]] = []
        for sp in specs:
            bot_a_id, bot_b_id = self._materialize_pairing_seats(sp)
            sched = self._compute_scheduled_at(sp.round_num, base, stagger_min)
            if not sp.requires_match:
                # 轮空占位：bot_b_id=None、无 match、status=completed（轮空者直接晋级）。
                pairing_rows.append(
                    {
                        "bot_a_id": bot_a_id,
                        "bot_b_id": None,
                        "round_num": sp.round_num,
                        "status": sp.status,
                        "stage_key": key,
                        "group_id": sp.group_id,
                        "bracket_slot": sp.bracket_slot,
                        "color_first": 0,
                        "entry_a_id": bot_to_entry.get(bot_a_id),
                        "entry_b_id": None,
                        "published_at": published_at,
                        "scheduled_at": None,
                    }
                )
                continue
            pairing_rows.append(
                {
                    "bot_a_id": bot_a_id,
                    "bot_b_id": bot_b_id,
                    "round_num": sp.round_num,
                    "status": "pending",
                    "stage_key": key,
                    "group_id": sp.group_id,
                    "bracket_slot": sp.bracket_slot,
                    "color_first": 0,
                    "entry_a_id": bot_to_entry.get(bot_a_id),
                    "entry_b_id": bot_to_entry.get(bot_b_id),
                    "published_at": published_at,
                    "scheduled_at": sched,
                    **self._version_snapshot(bot_a_id, bot_b_id),
                }
            )

        # 完整 pairing 批次 + 阶段游标/状态是一个持久化单元。首阶段 publish/start
        # 显式传 activate_running=False，仍由首场 bind 把 published 切 running；后续
        # stage 则在批次提交时离开 rest/推进 current_stage_idx，崩溃后可直接重派。
        current_idx = int(c.get("current_stage_idx") or 0)
        transition_to_running = bool(
            activate_running
            and (schedule_immediately or stage_idx > current_idx)
        )
        self.store.create_contest_stage_pairings(
            contest_id,
            stage_idx,
            pairing_rows,
            expected_current_stage_idx=current_idx,
            activate_running=transition_to_running,
        )
        if dispatch_pending:
            await self._dispatch_pending_locked(contest_id, stage_idx)

    async def ensure_published_pairings(self, contest_id: int, stage_idx: int) -> None:
        """修复 published 空壳/残缺首批对阵；与取消/开赛共用赛事锁。"""
        async with self._lock(contest_id):
            self._ensure_published_pairings_locked(contest_id, stage_idx)

    @staticmethod
    def _pairing_batch_signature(rows: list[dict]) -> Counter:
        """对阵批次的业务签名（忽略 DB id/时间/版本快照）。"""
        return Counter(
            (
                int(row.get("round_num") or 1),
                row.get("entry_a_id"),
                row.get("entry_b_id"),
                row.get("bot_a_id"),
                row.get("bot_b_id"),
                row.get("stage_key") or "",
                row.get("group_id") or "",
                row.get("bracket_slot"),
                int(row.get("color_first") or 0),
                row.get("status") or "pending",
            )
            for row in rows
        )

    def _ensure_published_pairings_locked(
        self, contest_id: int, stage_idx: int
    ) -> None:
        """锁内校验 published 批次完整性，必要时原子重建。

        不再以“有一行 pairing”当作完整的证据：精确重算首批 spec
        并比对参赛者/轮次/分组/轮空状态。只有全部未绑定且无 active
        match 的残缺批次可自动重建；已有真实进度必须报不一致。
        """
        contest = self.store.get_contest(contest_id)
        if not contest or contest["status"] != CONTEST_PUBLISHED:
            return
        stage, specs, bot_to_entry = self._stage_pairing_plan(contest, stage_idx)
        if not specs:
            raise ValueError("published 赛事无法生成完整对阵")

        existing = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        key = stage.get("key") or f"stage{stage_idx}"
        expected_shape: list[dict] = []
        for spec in specs:
            bot_a_id, bot_b_id = self._materialize_pairing_seats(spec)
            expected_shape.append(
                {
                    "round_num": spec.round_num,
                    "entry_a_id": bot_to_entry.get(bot_a_id),
                    "entry_b_id": bot_to_entry.get(bot_b_id),
                    "bot_a_id": bot_a_id,
                    "bot_b_id": bot_b_id,
                    "stage_key": key,
                    "group_id": spec.group_id,
                    "bracket_slot": spec.bracket_slot,
                    "color_first": 0,
                    "status": spec.status,
                }
            )

        complete = self._pairing_batch_signature(existing) == self._pairing_batch_signature(
            expected_shape
        )
        if complete:
            # published 态不应存在任何 active match；即使 pairing 外形完整，
            # prepare→bind 硬崩留下的未绑定幽灵也不能被静默忽略。
            if self.store.contest_has_active_matches(contest_id):
                raise ValueError("published 赛事对阵完整但存在 active 对局，数据不一致")
            return

        # 尽量保留硬崩前已写入的批次时间；若一行都没有则以
        # contest.starts_at / 当前时间为恢复基准。
        base = contest.get("starts_at") or next(
            (row.get("scheduled_at") for row in existing if row.get("scheduled_at")),
            None,
        ) or _now()
        published_at = next(
            (row.get("published_at") for row in existing if row.get("published_at")),
            None,
        ) or _now()
        stagger_min = max(0, int(stage.get("round_stagger_minutes") or 0))
        replacement: list[dict] = []
        for spec, shape in zip(specs, expected_shape):
            versions = self._version_snapshot(
                shape.get("bot_a_id"), shape.get("bot_b_id")
            )
            replacement.append(
                {
                    **shape,
                    **versions,
                    "published_at": published_at,
                    "scheduled_at": (
                        None
                        if not spec.requires_match
                        else self._compute_scheduled_at(spec.round_num, base, stagger_min)
                    ),
                }
            )
        self.store.replace_unstarted_contest_stage_pairings(
            contest_id,
            stage_idx,
            replacement,
            expected_existing_ids=[row["id"] for row in existing],
        )
        logger.warning(
            "published contest %s stage %s pairing batch was incomplete; rebuilt %s rows",
            contest_id,
            stage_idx,
            len(replacement),
        )

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
            v = self.store.get_current_bot_version(bid)
            if v:
                out[key] = v["id"]
        return out

    async def _dispatch_pending(self, contest_id: int, stage_idx: int) -> None:
        """派发 pending pairing（对外入口，获取 per-contest 锁串行化）。

        所有调度路径（scheduler tick / start / publish / reconcile）都应调本方法，
        它会获取 per-contest 锁，与 maybe_finish 的锁串行化，防并发双发孤儿对局
        （审计 P1：scheduler 锁外调 _dispatch_pending 与 maybe_finish 持锁并发，
        challenge() 的 await 让出期间另一路径读到同一 pending pairing 二次派发）。

        注意：maybe_finish 持锁链路（_begin_stage/_maybe_next_*）调
        _dispatch_pending_locked（不重复获锁，防 asyncio.Lock 不可重入死锁）。
        """
        async with self._lock(contest_id):
            await self._dispatch_pending_locked(contest_id, stage_idx)

    def _adjudicate_unavailable_pairing(
        self,
        contest: dict,
        pairing: dict,
        *,
        gid: str,
        activate_running: bool,
    ) -> str:
        """在派发前处理中途变为不可用的 Bot。

        返回 ``ready`` / ``completed`` / ``blocked``：
        - 双方可用：继续真实派发；
        - 仅一方不可用：生成有 winner 的 completed 技术判负；
        - 双方不可用：保留 pending，显式记录阻塞原因。

        绝不用 bot_id=0 伪造 aborted match；0 既违反外键，也没有
        任何可用于积分/晋级的裁决信息。
        """
        reason_a = self._bot_unavailable_reason(
            pairing.get("bot_a_id"), expected_game=gid
        )
        reason_b = self._bot_unavailable_reason(
            pairing.get("bot_b_id"), expected_game=gid
        )
        if reason_a is None and reason_b is None:
            return "ready"
        if reason_a is not None and reason_b is not None:
            logger.error(
                "contest pairing blocked: contest=%s pairing=%s both bots unavailable "
                "(a=%s; b=%s)",
                contest["id"],
                pairing["id"],
                reason_a,
                reason_b,
            )
            return "blocked"

        winner = 1 if reason_a is not None else 0
        ea, eb = ((-1, 1) if winner == 1 else (1, -1))
        import secrets

        mid = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
        try:
            self.store.create_match(
                mid,
                bot_a_id=pairing.get("bot_a_id"),
                bot_b_id=pairing.get("bot_b_id"),
                owner_id=contest.get("organizer_id"),
                contest_id=contest["id"],
                match_type=TYPE_CONTEST,
                game_id=gid,
                match_config={},
            )
            self.store.update_match(
                mid,
                status=STATUS_COMPLETED,
                reason="contest_bot_unavailable",
                winner=winner,
                result={"deltas": [ea, eb]},
                technical_loss=1,
                ended_at=_now(),
            )
            self.store.upsert_replay(mid, "[]", "[]")
            self.store.bind_contest_pairing_match(
                contest["id"],
                pairing["id"],
                mid,
                activate_running=activate_running,
            )
        except Exception:
            # 绑定竞态失败时不留下无 pairing 引用的伪对局。
            self.store.delete_match(mid)
            raise
        logger.warning(
            "contest technical loss: contest=%s pairing=%s match=%s winner=%s "
            "unavailable=%s",
            contest["id"],
            pairing["id"],
            mid,
            winner,
            reason_a or reason_b,
        )
        return "completed"

    async def _dispatch_pending_locked(self, contest_id: int, stage_idx: int) -> None:
        """_dispatch_pending 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        # P1-5 修复：锁内重检状态——published 可能在 scheduler snapshot 后被取消，
        # finished/cancelled 的 pending pairing 不应再派发（否则产孤儿对局）。
        if not c or c["status"] not in (CONTEST_PUBLISHED, CONTEST_RUNNING):
            return
        if c["status"] == CONTEST_PUBLISHED:
            self._ensure_published_pairings_locked(contest_id, stage_idx)
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        cfg = _match_config(c)  # 每游戏对局参数（holdem→hands, pencil→n_dots）
        gid = c.get("game_id") or "holdem"
        now = _now()
        # P2 residual：阶段配置 duplicate=True 且游戏 spec 支持 build_match_plan（仅 holdem）
        # 时走复式赛制——每对阵跑 1 场 duplicate 对局（2 leg 同副牌交换座位，合并 net 判胜）。
        # 棋类（build_match_plan is None）即便误标 duplicate 也走原单 leg 路径（不破坏现有赛制）。
        stages = _parse_stages(c)
        stage_cfg = stages[stage_idx] if 0 <= stage_idx < len(stages) else {}
        spec = game_registry.get(gid) if gid in REGISTERED_ENGINES else None
        want_duplicate = bool(stage_cfg.get("duplicate")) and spec is not None and spec.build_match_plan is not None
        # cfg 的键就是该游戏的 match_config 字段（holdem→{"hands"}, pencil→{"n_dots"},
        # 第 4 游戏自带其字段）。challenge() 透传整包，无需按字段名逐条硬判断。
        # ``running`` 或已有 match_id 表示本批次前已有真实进度。此时某一场准备失败
        # 不能把整个 start API 报成“全失败”：保留已启动场，失败 pairing 仍 pending，
        # 记录日志并让 scheduler 后续重试。仅 published 且零进度的首场失败向上抛。
        had_progress = c.get("status") == CONTEST_RUNNING or any(
            p.get("match_id") for p in pairings
        )
        technical_adjudicated = False
        for p in pairings:
            if p.get("status") != "pending" or p.get("match_id"):
                continue
            # 逐场排期：scheduled_at 未到则跳过（等调度器到点再 dispatch）
            sched = p.get("scheduled_at")
            if sched and sched > now:
                continue
            unavailable = self._adjudicate_unavailable_pairing(
                c,
                p,
                gid=gid,
                activate_running=(
                    c.get("status") == CONTEST_PUBLISHED and not had_progress
                ),
            )
            if unavailable == "blocked":
                continue
            if unavailable == "completed":
                had_progress = True
                technical_adjudicated = True
                continue
            # 冻结快照已在 pairing 行；直接开打
            # cfg 是该游戏的 match_config（holdem→{"hands"}, pencil→{"n_dots"}），
            # 整包传给 challenge(match_config=...)，无需按字段名逐条具名传递。
            # duplicate=True 时用对阵 pair 派生的确定性 seed（pairing.id 稳定），
            # 保证两 leg 同副牌可复现。
            try:
                await self._prepare_bind_start_pairing(
                    c,
                    p,
                    gid=gid,
                    cfg=cfg,
                    want_duplicate=want_duplicate,
                    activate_running=(
                        c.get("status") == CONTEST_PUBLISHED and not had_progress
                    ),
                )
                had_progress = True
            except Exception:
                if not had_progress:
                    raise
                logger.exception(
                    "contest dispatch partial failure: contest=%s pairing=%s; "
                    "已有对局继续，失败对阵保持 pending 等待重试",
                    contest_id,
                    p["id"],
                )
        # 技术判负没有 runner task，也就没有 on_match_done 回调。
        # 在已持锁的调度链内主动检查阶段，避免“全部是技术结果”
        # 的赛事永久卡 running。
        if technical_adjudicated:
            await self._maybe_finish_locked(contest_id)

    async def _prepare_bind_start_pairing(
        self,
        contest: dict,
        pairing: dict,
        *,
        gid: str,
        cfg: dict,
        want_duplicate: bool,
        activate_running: bool,
    ) -> str:
        """两阶段派发一场：prepare match → 原子绑定 pairing → 启动 runner。

        MatchOrchestrator 的真实实现支持 defer/start/discard。少量只用于单元测试的
        legacy fake 没有显式 start/discard 方法时，仍沿用其 challenge 即启动契约。
        """
        common = {
            "owner_user_id": contest["organizer_id"],
            "match_type": TYPE_CONTEST,
            "contest_id": contest["id"],
            "game_id": gid,
            "match_config": cfg,
            "bot_a_version_id": pairing.get("bot_a_version_id"),
            "bot_b_version_id": pairing.get("bot_b_version_id"),
            "defer_start": True,
        }
        mid: str | None = None
        bound = False
        try:
            if want_duplicate:
                mid = await self.orch.challenge_duplicate(
                    pairing["bot_a_id"],
                    pairing["bot_b_id"],
                    duplicate_seed=int(pairing["id"]) * 7919 + 1,
                    **common,
                )
            else:
                mid = await self.orch.challenge(
                    pairing["bot_a_id"], pairing["bot_b_id"], **common
                )
            if not mid:
                raise RuntimeError("challenge 未返回 match_id")
            self.store.bind_contest_pairing_match(
                contest["id"],
                pairing["id"],
                mid,
                activate_running=activate_running,
            )
            bound = True
            starter = getattr(self.orch, "start_prepared_match", None)
            if starter is not None:
                starter(mid)
            return mid
        except Exception:
            if mid is not None:
                if bound:
                    self.store.unbind_prepared_contest_match(
                        contest["id"],
                        pairing["id"],
                        mid,
                        restore_published=activate_running,
                    )
                discard = getattr(self.orch, "discard_prepared_match", None)
                if discard is not None and not discard(mid):
                    logger.error(
                        "prepared match compensation refused: contest=%s pairing=%s match=%s",
                        contest["id"], pairing["id"], mid,
                    )
            raise

    async def cancel(self, contest_id: int) -> dict:
        """取消未开赛赛事；与 publish/start/dispatch 共用锁并在锁内复核状态。"""
        async with self._lock(contest_id):
            c = self.store.get_contest(contest_id)
            if not c:
                raise ValueError("比赛不存在")
            if c["status"] == CONTEST_CANCELLED:
                return c
            if c["status"] not in (CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED):
                raise ValueError(
                    f"赛事处于 {c['status']} 态，不能取消（仅 draft/open/published 可取消）"
                )
            return self.store.update_contest(contest_id, status=CONTEST_CANCELLED)

    async def delete(self, contest_id: int) -> bool:
        """安全删除赛事：与 start/dispatch 共锁，拒绝运行态或任何 active match。

        published 尚未开打时先转 cancelled 再删除，明确其“取消排期后删除”语义；
        running/rest、finished 或任何已有正式榜的赛事一律拒绝，避免抹掉正式赛果。
        """
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                return False
            if (
                contest["status"] == CONTEST_FINISHED
                or int(contest.get("official_results_ready") or 0)
                or self.store.list_official_results(contest_id)
            ):
                raise ValueError("已完成或已有正式赛果的赛事不能删除")
            if contest["status"] in (CONTEST_RUNNING, CONTEST_REST):
                raise ValueError("运行中或休息期赛事不能删除，请先完成或中止在途对局")
            if self.store.contest_has_active_matches(contest_id):
                raise ValueError("赛事仍有 pending/running 对局，不能删除")
            if contest["status"] == CONTEST_PUBLISHED:
                self.store.update_contest(contest_id, status=CONTEST_CANCELLED)
            return self.store.delete_contest(contest_id)

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
                # Swiss 奇数轮的 bye 是显式 completed/no-match pairing。
                # 轮空获得本赛制的“胜场分”，但它不是一场对局：不增
                # wins/draws/losses、net_chips，也没有对手记录。KO bye
                # 是直接晋级，不在此计分。
                if (
                    stage.get("type") == "swiss"
                    and p.get("bot_b_id") is None
                    and p.get("status") == "completed"
                ):
                    entry_id = p.get("entry_a_id")
                    if entry_id in stats:
                        stats[entry_id]["points"] += points_for_result(
                            scoring, 0, 0
                        )
                continue
            m = self.store.get_match(mid)
            if not m or m["status"] != STATUS_COMPLETED:
                continue
            ea_id = p.get("entry_a_id")
            eb_id = p.get("entry_b_id")
            if ea_id not in stats or eb_id not in stats:
                continue
            # result.legs（复式赛制）：每 leg 独立判胜负，按"打了两场"逐场累加。
            # 无 legs（普通赛制）：单场胜负累加（原逻辑）。
            result = m.get("result") or {}
            legs_data = result.get("legs") if isinstance(result, dict) else None
            if legs_data:
                # 复式：逐 leg 累加 points/wins/draws/losses/net_chips
                for lg in legs_data:
                    lg_winner = lg.get("winner")
                    lg_deltas = lg.get("deltas") or [0, 0]
                    stats[ea_id]["net_chips"] += int(lg_deltas[0])
                    stats[eb_id]["net_chips"] += int(lg_deltas[1])
                    stats[ea_id]["points"] += points_for_result(scoring, lg_winner, 0)
                    stats[eb_id]["points"] += points_for_result(scoring, lg_winner, 1)
                    if lg_winner == 0:
                        stats[ea_id]["wins"] += 1
                        stats[eb_id]["losses"] += 1
                    elif lg_winner == 1:
                        stats[eb_id]["wins"] += 1
                        stats[ea_id]["losses"] += 1
                    else:
                        stats[ea_id]["draws"] += 1
                        stats[eb_id]["draws"] += 1
            else:
                # 普通赛制：单场胜负累加
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
            # 轮空占位 pairing（bot_b_id=None、无 match、status=completed）视为已完成。
            if (
                p.get("bot_b_id") is None
                and not p.get("match_id")
                and p.get("status") == "completed"
            ):
                continue
            mid = p.get("match_id")
            if not mid:
                return False
            m = self.store.get_match(mid)
            # aborted 只表示对局被取消/未产生裁决，绝不是赛制上的
            # “已完成”。把它算作终态会让 KO 在 winner=None 时固定
            # 晋级座位 0，也会给 RR/Swiss 静默吞分。
            if not m or m["status"] != STATUS_COMPLETED:
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
            if m and m["status"] == STATUS_COMPLETED:
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

    async def handle_match_done(
        self,
        match_id: str,
        contest_id: int,
        *,
        retry_aborted: bool = False,
    ) -> dict | None:
        """赛事对局收尾的唯一回调入口。

        completed 才能进入积分/晋级检查。aborted 对局保留历史行，
        对应 pairing 原子复位 pending。只有 orchestrator 通过短暂 handoff
        显式证明是管理员主动中止时，才立即安全重派；platform_error
        等平台故障不在回调栈里无限快速重试，留给 scheduler/reconcile。
        """
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            match = self.store.get_match(match_id)
            if not contest or not match:
                return None
            if match.get("status") == STATUS_ABORTED:
                pairing = self.store.reset_aborted_contest_pairing(
                    contest_id, match_id
                )
                if pairing:
                    if not retry_aborted:
                        backoff_at = (
                            datetime.now() + timedelta(seconds=30)
                        ).isoformat(timespec="seconds")
                        # 不要把原本更远的排期拉近；平台故障至少退避
                        # 30 秒，避免 scheduler 每个 tick 立即重创 match。
                        scheduled_at = max(
                            str(pairing.get("scheduled_at") or ""), backoff_at
                        )
                        pairing = self.store.update_contest_pairing(
                            pairing["id"], scheduled_at=scheduled_at
                        ) or pairing
                    logger.warning(
                        "contest match aborted without adjudication: contest=%s "
                        "pairing=%s match=%s reason=%s; reset to pending%s",
                        contest_id,
                        pairing["id"],
                        match_id,
                        match.get("reason"),
                        " with backoff" if not retry_aborted else " for admin redispatch",
                    )
                    if (
                        retry_aborted
                        and contest.get("status") == CONTEST_RUNNING
                    ):
                        await self._dispatch_pending_locked(
                            contest_id, int(pairing.get("stage_idx") or 0)
                        )
                return self.store.get_contest(contest_id)
            return await self._maybe_finish_locked(contest_id)

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
        """启动对账：让 active contest 与缺正式榜的 finished contest 收敛。

        解决三类「赛事卡 running」：
        1. match 全完成但 maybe_finish 回调丢失/异常被吞（生产 contest 25）→ 直接 maybe_finish。
        2. match 被 orphan_after_restart 清成 aborted，pairing 仍指它（生产 contest 24）→
           reset_dead_contest_pairings 复位后重派。
        3. pairing 建了 match 行但 _run_match 从未跑完（pending match，started_at=None）→
           识别为死 pairing 复位重派。
        4. prepare match 成功但 bind 前硬崩→删除未被 pairing 引用的 pending
           match/index/replay，保留原 pending pairing 重派。
        5. published 首阶段只写入部分 pairing 就硬崩→校验完整批次，
           仅在全部未绑定时原子重建；已有进度则显式报不一致。
        6. contest 已 finished、正式榜事务尚未提交就硬崩→幂等补算完整榜，
           避免 official-results 永久 409。

        maybe_finish 在 _stage_done=False 时只生成下一轮、不重派 pending pairing，
        所以对账须在 maybe_finish 之后显式 _dispatch_pending 死而复生的 pending pairing。
        返回处理的 contest 数。
        """
        # 1. 清理未绑定 prepared 幽灵 + 复位已绑定死 pairing。
        reset_n = self.store.reset_dead_contest_pairings()
        if reset_n:
            logger.info("启动对账：清理/复位 %d 个幽灵对局或死 pairing", reset_n)

        contests = self.store.list_contests_by_status(
            [CONTEST_PUBLISHED, CONTEST_RUNNING, CONTEST_REST]
        )
        contests.extend(self.store.list_unready_finished_contests())
        for c in contests:
            cid = c["id"]
            try:
                await self._reconcile_one(cid)
            except Exception:
                # 单个 contest 对账失败不阻塞其他——但必须可见（防静默卡死再复发）
                logger.exception("reconcile contest %s failed", cid)
        return len(contests)

    async def _reconcile_one(self, contest_id: int) -> None:
        """对账单个 contest：恢复 published 批次或收敛 running/rest。"""
        initial = self.store.get_contest(contest_id)
        if initial and initial["status"] == CONTEST_FINISHED:
            # finished 是终态，maybe_finish 不会再进入；正式榜落库若在终态提交后
            # 失败，只能由启动恢复显式补算。持赛事锁并重读，避免与同进程内的
            # force-finish/回调竞态；replace_official_results 自身是完整批次事务。
            async with self._lock(contest_id):
                latest = self.store.get_contest(contest_id)
                if (
                    latest
                    and latest["status"] == CONTEST_FINISHED
                    and not int(latest.get("official_results_ready") or 0)
                ):
                    stage_idx = int(latest.get("current_stage_idx") or 0)
                    self._finalize_official_results(contest_id, stage_idx)
            return
        if initial and initial["status"] == CONTEST_PUBLISHED:
            stage_idx = int(initial.get("current_stage_idx") or 0)
            await self.ensure_published_pairings(contest_id, stage_idx)
            # 恢复后仅派发 scheduled_at<=now 的场次；未到点的仍保持
            # published，不把“启动恢复”偷换成“手动立即开赛”。
            await self._dispatch_pending(contest_id, stage_idx)
            await self.maybe_finish(contest_id)
            return
        # 第一轮 maybe_finish：能 finish 的直接 finish（match 全完成的场景）
        await self.maybe_finish(contest_id)
        c = self.store.get_contest(contest_id)
        if not c or c["status"] not in (CONTEST_RUNNING, CONTEST_REST):
            return  # 已 finish/advance
        if c["status"] == CONTEST_REST:
            return  # rest 期交由 _maybe_auto_resume（启动时点未到则等）

        stage_idx = int(c.get("current_stage_idx") or 0)
        # 第二轮：重派 pending 无 match_id 的 pairing（死而复生 + 新生成轮）。
        # 单侧 Bot 不可用时会落 completed 技术判负；双方不可用时
        # 明确保持 pending 阻塞，不伪造无 winner 的 aborted 结果。
        await self._dispatch_pending_safe(contest_id, stage_idx)
        # 第三轮：重派/技术裁决后再 maybe_finish，让阶段真正推进
        await self.maybe_finish(contest_id)

    async def _dispatch_pending_safe(
        self, contest_id: int, stage_idx: int
    ) -> None:
        """重派 pending pairing，对单个 pairing 的 Bot 不可用做公平裁决。

        _dispatch_pending 是批量 dispatch，任一 pairing 的 bot 删了会抛 ValueError 中断后续。
        此方法逐 pairing 隔离其他派发错误；Bot 缺失则与正常派发共用
        ``_adjudicate_unavailable_pairing`` 的单侧技术判负/双侧阻塞契约。
        """
        async with self._lock(contest_id):
            await self._dispatch_pending_safe_locked(contest_id, stage_idx)

    async def _dispatch_pending_safe_locked(self, contest_id: int, stage_idx: int) -> None:
        """_dispatch_pending_safe 的实际逻辑（调用方已持锁）。"""
        c = self.store.get_contest(contest_id)
        # reconcile 在锁外按 running 快照选中赛事后，可能先被 finish 收尾；
        # 锁内必须重检，终态不得再派发或制造 aborted 占位对局。
        if not c or c["status"] != CONTEST_RUNNING:
            return
        cfg = _match_config(c)
        gid = c.get("game_id") or "holdem"
        # 复式赛制判断（与 _dispatch_pending_locked 一致）——reconcile 重派也保留
        # duplicate 标志（复审 P2-2），否则同赛事出现 duplicate/单 leg 混合。
        stages = _parse_stages(c)
        stage_cfg = stages[stage_idx] if 0 <= stage_idx < len(stages) else {}
        spec = game_registry.get(gid) if gid in REGISTERED_ENGINES else None
        want_duplicate = bool(stage_cfg.get("duplicate")) and spec is not None and spec.build_match_plan is not None
        pending = [
            p
            for p in self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
            if p.get("status") == "pending" and not p.get("match_id")
        ]
        for p in pending:
            unavailable = self._adjudicate_unavailable_pairing(
                c, p, gid=gid, activate_running=False
            )
            if unavailable != "ready":
                continue
            try:
                await self._prepare_bind_start_pairing(
                    c,
                    p,
                    gid=gid,
                    cfg=cfg,
                    want_duplicate=want_duplicate,
                    activate_running=False,
                )
            except Exception:
                logger.exception(
                    "reconcile: contest=%s pairing=%s 重派失败，保持 pending",
                    contest_id,
                    p["id"],
                )

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
            if (
                p.get("bot_b_id") is None
                and not p.get("match_id")
                and p.get("status") == "completed"
            ):
                continue
            mid = p.get("match_id")
            if not mid:
                return False
            m = self.store.get_match(mid)
            if not m or m["status"] != STATUS_COMPLETED:
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
        bye_counts_by_entry: Counter[int] = Counter()
        color_counts_by_entry: Counter[int] = Counter()
        for p in pairings:
            entry_a = p.get("entry_a_id")
            entry_b = p.get("entry_b_id")
            if p.get("bot_b_id") is None and not p.get("match_id"):
                if entry_a is not None:
                    bye_counts_by_entry[int(entry_a)] += 1
                continue
            # Persisted A is the actual seat 0 after color_first materialization.
            # Count by stable entry identity so a rest-period Bot swap does not
            # reset that participant's first-move history.
            if entry_a is not None:
                color_counts_by_entry[int(entry_a)] += 1
            # 对手历史以 entry 身份为真相源；休息期换 Bot 后映射到当前
            # bot_id，避免换版本/换 Bot 后把同两名选手误当“未交手”。
            current_a = entry_to_bot.get(entry_a)
            current_b = entry_to_bot.get(entry_b)
            if current_a is not None and current_b is not None:
                played.add((min(current_a, current_b), max(current_a, current_b)))
        bye_counts = {
            bot_id: int(bye_counts_by_entry.get(entry_id, 0))
            for bot_id, entry_id in bot_to_entry.items()
        }
        color_counts = {
            bot_id: int(color_counts_by_entry.get(entry_id, 0))
            for bot_id, entry_id in bot_to_entry.items()
        }
        specs = generate_stage_pairings(
            stage,
            bot_ids,
            scores=scores,
            played=played,
            swiss_round=max_round + 1,
            color_counts=color_counts,
            bye_counts=bye_counts,
        )
        key = stage.get("key") or f"stage{stage_idx}"
        published_at = _now()
        pairing_rows: list[dict[str, Any]] = []
        for sp in specs:
            bot_a_id, bot_b_id = self._materialize_pairing_seats(sp)
            if not sp.requires_match:
                pairing_rows.append(
                    {
                        "bot_a_id": bot_a_id,
                        "bot_b_id": None,
                        "round_num": sp.round_num,
                        "status": sp.status,
                        "stage_key": key,
                        "group_id": sp.group_id,
                        "bracket_slot": sp.bracket_slot,
                        "color_first": 0,
                        "entry_a_id": bot_to_entry.get(bot_a_id),
                        "entry_b_id": None,
                        "published_at": published_at,
                    }
                )
                continue
            pairing_rows.append(
                {
                    "bot_a_id": bot_a_id,
                    "bot_b_id": bot_b_id,
                    "round_num": sp.round_num,
                    "status": STATUS_PENDING,
                    "stage_key": key,
                    "group_id": sp.group_id,
                    "bracket_slot": sp.bracket_slot,
                    "color_first": 0,
                    "entry_a_id": bot_to_entry.get(bot_a_id),
                    "entry_b_id": bot_to_entry.get(bot_b_id),
                    "published_at": published_at,
                    **self._version_snapshot(bot_a_id, bot_b_id),
                }
            )
        self.store.append_contest_round_pairings(
            contest_id,
            stage_idx,
            pairing_rows,
            expected_current_stage_idx=stage_idx,
            expected_previous_max_round=max_round,
        )
        await self._dispatch_pending_locked(contest_id, stage_idx)
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
            # 轮空占位 pairing（bot_b_id=None、无 match、status=completed）：
            # 视为已完成，胜者为轮空者 bot_a。
            if p.get("bot_b_id") is None and not p.get("match_id"):
                winners.append((p["bot_a_id"], p.get("entry_a_id")))
                continue
            mid = p.get("match_id")
            if not mid:
                return False
            m = self.store.get_match(mid)
            if not m or m["status"] != STATUS_COMPLETED:
                return False
            w = m.get("winner")
            if w is None:
                # 淘汰赛没有权威 winner 时不得以座位 0 兜底晋级。
                # 显式阻塞，等待裁判/管理员按业务规则处理。
                logger.error(
                    "elimination pairing has no adjudicated winner: "
                    "contest=%s pairing=%s match=%s",
                    contest_id,
                    p["id"],
                    mid,
                )
                return False
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
        pairing_rows: list[dict[str, Any]] = []
        for i in range(0, len(winners), 2):
            a_bot, a_entry = winners[i]
            if i + 1 < len(winners):
                # 相邻两胜者配对
                b_bot, b_entry = winners[i + 1]
                pairing_rows.append(
                    {
                        "bot_a_id": a_bot,
                        "bot_b_id": b_bot,
                        "round_num": next_round,
                        "status": STATUS_PENDING,
                        "stage_key": key,
                        "bracket_slot": slot,
                        "color_first": 0,
                        "entry_a_id": a_entry,
                        "entry_b_id": b_entry,
                        "published_at": published_at,
                        **self._version_snapshot(a_bot, b_bot),
                    }
                )
                slot += 1
            else:
                # 奇数末位胜者：轮空自动晋级（不打本轮）。
                # 创建「轮空占位 pairing」：bot_b_id=None、无 match、直接标 completed，
                # winner 固定为 bot_a（轮空者）。这样 _stage_done 把它视为已完成、
                # _maybe_next_elim_round 能从它收集到轮空胜者，下一轮配对时正常带入——
                # 确保奇数胜者（非 2 幂人数）无人丢失、阶段能 finish。
                pairing_rows.append(
                    {
                        "bot_a_id": a_bot,
                        "bot_b_id": None,
                        "round_num": next_round,
                        "status": STATUS_COMPLETED,
                        "stage_key": key,
                        "bracket_slot": slot,
                        "color_first": 0,
                        "entry_a_id": a_entry,
                        "entry_b_id": None,
                        "published_at": published_at,
                    }
                )
                slot += 1
        self.store.append_contest_round_pairings(
            contest_id,
            stage_idx,
            pairing_rows,
            expected_current_stage_idx=stage_idx,
            expected_previous_max_round=max_round,
        )
        await self._dispatch_pending_locked(contest_id, stage_idx)
        return True

    async def _maybe_auto_resume(self, contest_id: int) -> dict | None:
        """maybe_finish 持锁链路调（rest→running 自动恢复）。调用方已持锁。"""
        c = self.store.get_contest(contest_id)
        if not c or c["status"] != CONTEST_REST:
            return None
        ends = c.get("rest_ends_at")
        if ends and ends <= _now():
            return await self._resume_locked(contest_id)
        return None

    async def resume(self, contest_id: int) -> dict:
        """rest→running（对外入口，获取 per-contest 锁）。

        scheduler tick（锁外）调本方法；maybe_finish 锁内链路调 _resume_locked
        （防 asyncio.Lock 不可重入死锁 + 防双发竞态，与 _dispatch_pending 同模式）。
        """
        async with self._lock(contest_id):
            return await self._resume_locked(contest_id)

    async def _resume_locked(self, contest_id: int) -> dict:
        """resume 的实际逻辑（调用方已持 per-contest 锁）。"""
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

    async def finish(self, contest_id: int) -> dict:
        """组织者/admin 强制结束赛事（running/rest → finished）。

        用于所有已派发对局都进入终态、但自动阶段推进卡住时的手动出口。
        当前 runner 没有 contest-aware abort，因此仍有 pending/running 对局时明确拒绝，
        避免先写 finished 后后台任务继续晚写结果。
        """
        async with self._lock(contest_id):
            return self._finish_locked(contest_id)

    def _finish_locked(self, contest_id: int) -> dict:
        """finish 的实际逻辑（调用方已持 per-contest 锁并在此重读状态）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        if c["status"] not in (CONTEST_RUNNING, CONTEST_REST):
            raise ValueError("仅运行中/休息中的赛事可强制结束")
        if self._has_unfinished_pairings(contest_id):
            raise ValueError(
                "赛事仍有未完成对阵，无法强制结束；请等待对局完成或先安全中止对局"
            )
        stage_idx = int(c.get("current_stage_idx") or 0)
        self.store.update_contest(
            contest_id, status=CONTEST_FINISHED, ends_at=_now(), rest_ends_at=None
        )
        try:
            self._finalize_official_results(contest_id, stage_idx)
        except Exception:
            logger.exception("force-finish official results failed contest=%s", contest_id)
        return self.store.get_contest(contest_id)

    def _has_unfinished_pairings(self, contest_id: int) -> bool:
        """强制结束前的安全闸门；调用方须持赛事锁。

        当前 orchestrator 没有能等待 runner 收敛的 contest-aware abort。与其先写
        finished 后让后台任务晚写结果，保守拒绝任何未绑定、缺失或仍活跃的对阵；
        同时检查未被 pairing 正确绑定的赛事活跃 match。
        """
        if self.store.contest_has_active_matches(contest_id):
            return True
        for pairing in self.store.list_contest_pairings(contest_id):
            if (
                pairing.get("bot_b_id") is None
                and not pairing.get("match_id")
                and pairing.get("status") == STATUS_COMPLETED
            ):
                continue
            match_id = pairing.get("match_id")
            if not match_id:
                return True
            match = self.store.get_match(match_id)
            if not match or match.get("status") != STATUS_COMPLETED:
                return True
        return False

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
