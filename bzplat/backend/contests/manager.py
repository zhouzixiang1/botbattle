"""组织者比赛：阶段模板、休息换 Bot、对阵调度。"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable, Literal

from bzplat.backend.contests.stages import (
    PairingSpec,
    effective_swiss_rounds,
    effective_group_count,
    estimate_match_count,
    generate_stage_pairings,
)
from bzplat.backend.contests.templates import (
    get_template,
    points_for_result,
    resolve_template,
)
from bzplat.backend.contests.series import (
    conceptual_series_key,
    contest_match_binding_is_valid,
    contest_pairing_roster_binding_is_valid,
    group_conceptual_series,
    is_aggregate_series_stage,
    match_scoring_result_is_valid,
    series_rows_settled,
    summarize_elimination_encounter,
    swiss_bye_points,
    summarize_conceptual_series,
)
from bzplat.backend.contests.showcase import is_showcase, require_mutable
from bzplat.backend.contests.validation import (
    ELIMINATION_TIEBREAK_PAIRED_SWAP,
    SERIES_SCORING_AGGREGATE,
    SERIES_SCORING_INDEPENDENT,
    active_contest_entries,
    contest_current_stage_index,
    contest_entry_eliminated,
    stage_duplicate_mode,
    stage_scoring_contract_is_valid,
)
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.runtime.binary_integrity import require_binary_file_integrity
from bzplat.backend.matches.result_contract import build_result_payload
from bzplat.backend.matches.public_outcome import (
    normalized_delta_value,
    planned_match_games,
    scoring_games_for_match,
)
from bzplat.backend.games import normalize_game_id, registry as game_registry
from bzplat.backend.runtime.config import MAX_CONCURRENT_MATCHES
from bzplat.backend.store import (
    ContestRealNameRosterForbidden,
    ExecutionQueueClosed,
    Store,
)
from bzplat.backend.store.public_contract import (
    sanitize_public_contest_tiebreaks,
)
from bzplat.backend.store.validation import (
    exact_nonnegative_int,
    exact_sqlite_bool,
    is_authoritative_no_opponent_pairing,
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
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    TYPE_CONTEST,
    require_supported_binary_metadata,
)


logger = logging.getLogger(__name__)

EliminationAdvanceState = Literal["created", "champion", "blocked"]


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _parse_stages(c: dict) -> list[dict]:
    raw = c.get("stages_json") or "[]"
    if isinstance(raw, list):
        parsed = raw
    else:
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
    if not isinstance(parsed, list) or any(
        not isinstance(stage, dict) for stage in parsed
    ):
        # Preserve stage coordinates by rejecting the whole malformed snapshot;
        # filtering non-object elements would shift every later stage index.
        return []
    return parsed


def _estimate_sec_per_match(gid: str, cfg: dict) -> int:
    """粗估每场时长（秒）：经 spec.eta_for_match（各游戏已钉死固定 ETA）。"""
    return game_registry.get(gid).eta_for_match(cfg)


def _stored_game_id(row: dict, *, entity: str) -> str:
    """读取已存实体的游戏维度；缺失/未知必须失败，不能猜成 Holdem。"""
    raw = row.get("game_id")
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{entity} 缺少 game_id")
    gid = raw.strip().lower()
    try:
        game_registry.get(gid)
    except KeyError as exc:
        raise ValueError(f"{entity} 使用未注册游戏: {gid!r}") from exc
    return gid


class ContestManager:
    def __init__(
        self,
        store: Store,
        orch: MatchOrchestrator,
        *,
        execution_admission_required: Callable[[], bool] | None = None,
    ) -> None:
        self.store = store
        self.orch = orch
        # The production app enables the full dispatcher-state gate only once
        # its singleton dispatcher owns the queue.  Pure contest unit-test
        # managers keep their historical synchronous behavior, while the
        # persistent deployment-drain bit is enforced for every caller.
        self._execution_admission_required = execution_admission_required
        # A deployment request and every contest path that can create/bind an
        # execution unit share this process-local boundary.  SQLite guards the
        # final writes as well; this lock closes the wider multi-transaction
        # start/resume lifecycle so the maintenance API cannot acknowledge a
        # drain halfway through it.
        self.deployment_activity_lock = asyncio.Lock()
        # per-contest 锁：串行化所有写状态路径（start/publish/cancel/resume/advance/
        # maybe_finish/_dispatch_pending），防止请求与 scheduler/on_match_done 并发导致
        # 重复生成轮次或取消后继续派发。
        self._locks: dict[int, asyncio.Lock] = {}

    def _requires_live_admission(self) -> bool:
        """Whether this process currently owns the live execution queue."""
        if self._execution_admission_required is None:
            return False
        return bool(self._execution_admission_required())

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

    def _execution_admission_error(
        self, *, maintenance_only: bool = False
    ) -> ExecutionQueueClosed | None:
        """Return the queue gate without mutating any contest state."""
        control = self.store.executions.control()
        if self.store.executions.is_maintenance_control(control):
            return ExecutionQueueClosed(
                "平台正在部署维护，赛事将在恢复后继续派发",
                code="deployment_maintenance",
            )
        if maintenance_only:
            return None
        if self._requires_live_admission() and (
            control.get("dispatcher_state") != "running"
            or int(control.get("accepting") or 0) != 1
        ):
            return ExecutionQueueClosed(
                "执行队列暂未开放，赛事对阵已保留",
            )
        return None

    def _require_execution_admission(self) -> None:
        error = self._execution_admission_error()
        if error is not None:
            raise error

    def create(
        self,
        organizer_id: int,
        title: str,
        *,
        description: str = "",
        template_id: str | None = None,
        game_id: str | None = None,
        stages: list[dict] | None = None,
        phase: str = "standalone",
        source_contest_id: int | None = None,
        require_real_name: int = 0,
        registration_opens_at: str | None = None,
        registration_closes_at: str | None = None,
        starts_at: str | None = None,
        games_per_pair: int | None = None,
        stage_series_settings: dict[str, dict[str, Any]] | None = None,
    ) -> dict:
        series_capability: dict[str, Any] | None = None
        stage_series_capabilities: list[dict[str, Any]] | None = None
        # 自定义 stages 直接用；否则只从游戏注册表中的代码模板解析 stages。
        if stages is not None:
            if not stages:
                raise ValueError("自定义 stages 须为非空数组")
            tid = "custom" if template_id is None else template_id
            # 未指定游戏是创建入口的产品默认；显式空值/未知值不得退化为 holdem。
            gid = normalize_game_id("holdem" if game_id is None else game_id)
            # 即使调用方同时传入自定义 stages，也不能借此把一个具名模板标成
            # 另一款游戏。该组合会污染赛事快照，后续按 gid 启动错误裁判。
            if template_id:
                declared_template = get_template(template_id)
                if declared_template:
                    template_gid = str(declared_template["game_id"]).strip().lower()
                    if gid != template_gid:
                        raise ValueError(
                            f"模板 {template_id} 属于游戏 {template_gid}，不能用于游戏 {gid}"
                        )
                    if declared_template.get("creation_enabled", True) is False:
                        raise ValueError(
                            f"模板 {template_id} 已停用新建，仅供历史赛事展示"
                        )
            stage_list = stages
        else:
            tid, gid, stage_list, _tpl_mc = resolve_template(
                template_id, game_id=game_id
            )
            template = get_template(tid)
            if template is not None:
                raw_capability = template.get("games_per_pair_config")
                series_capability = (
                    dict(raw_capability)
                    if isinstance(raw_capability, dict)
                    else None
                )
                raw_stage_capabilities = template.get("stage_series_configs")
                stage_series_capabilities = (
                    list(raw_stage_capabilities)
                    if isinstance(raw_stage_capabilities, list)
                    else None
                )
        # 无论来自 API 自定义内容还是代码模板，都通过同一严格 schema。未知键、
        # 错拼字段和不属于该阶段类型的配置必须在落赛事快照前失败。
        from bzplat.backend.contests.validation import configure_games_per_pair

        stage_list = configure_games_per_pair(
            stage_list,
            gid,
            games_per_pair,
            capability=series_capability,
            stage_series_settings=stage_series_settings,
            stage_capabilities=stage_series_capabilities,
        )
        # P5：phase 优先级：显式传入 > 模板自带 phase > standalone
        if phase == "standalone":
            tpl = get_template(tid)
            if tpl and tpl.get("phase"):
                phase = tpl["phase"]
        # 时间校验：开放报名 <= 截止报名 <= 开赛（相同秒合法）
        _validate_contest_times(registration_opens_at, registration_closes_at, starts_at)
        return self.store.create_contest(
            title,
            organizer_id,
            description=description,
            status="draft",
            game_id=gid,
            template_id=tid,
            stages_json=json.dumps(stage_list, ensure_ascii=False),
            current_stage_idx=0,
            phase=phase,
            source_contest_id=source_contest_id,
            require_real_name=require_real_name,
            registration_opens_at=registration_opens_at,
            registration_closes_at=registration_closes_at,
            starts_at=starts_at,
        )

    @staticmethod
    def _stage_series_capabilities(template_id: object) -> list[dict[str, Any]] | None:
        template = get_template(str(template_id or ""))
        raw = template.get("stage_series_configs") if template else None
        return list(raw) if isinstance(raw, list) else None

    @staticmethod
    def _games_per_pair_capability(template_id: object) -> dict[str, Any] | None:
        template = get_template(str(template_id or ""))
        raw = template.get("games_per_pair_config") if template else None
        return dict(raw) if isinstance(raw, dict) else None

    @staticmethod
    def _matches_template_stage_topology(
        template_id: object, stages: list[dict[str, Any]]
    ) -> bool:
        """Whether a persisted snapshot still matches its code-owned topology.

        Older callers may combine a built-in ``template_id`` with explicit custom
        stages.  The identifier alone therefore cannot authorize injecting new
        code-template defaults at publish time.  Only the fields advertised by
        ``stage_series_configs`` are mutable here; every other persisted stage
        field is part of the frozen template snapshot.  Comparing just key/type
        would, for example, let a custom ``rounds`` or ``advance_count`` inherit
        defaults intended for a different tournament graph.
        """
        template = get_template(str(template_id or ""))
        template_stages = template.get("stages") if template else None
        if not isinstance(template_stages, list) or len(template_stages) != len(stages):
            return False

        configurable_series_fields = frozenset(
            {
                "games_per_pair",
                "series_scoring",
                "swiss_extra_rounds",
                "effective_rounds",
            }
        )

        def topology(
            rows: list[dict[str, Any]],
        ) -> tuple[tuple[tuple[str, Any], ...], ...] | None:
            out: list[tuple[tuple[str, Any], ...]] = []
            for row in rows:
                if not isinstance(row, dict):
                    return None
                out.append(
                    tuple(
                        sorted(
                            (key, value)
                            for key, value in row.items()
                            if key not in configurable_series_fields
                        )
                    )
                )
            return tuple(out)

        return topology(template_stages) == topology(stages)

    def _configured_unstarted_series_stages(
        self,
        contest: dict[str, Any],
        stages: list[dict[str, Any]],
        settings: dict[str, dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Apply current code-template defaults only at the unstarted boundary."""
        capabilities = self._stage_series_capabilities(contest.get("template_id"))
        pair_capability = self._games_per_pair_capability(
            contest.get("template_id")
        )
        if capabilities is None and pair_capability is None:
            if settings is not None:
                raise ValueError("当前赛事模板不支持 stage_series_settings")
            return stages
        if capabilities is None and settings is not None:
            raise ValueError("当前赛事模板不支持 stage_series_settings")
        if not self._matches_template_stage_topology(
            contest.get("template_id"), stages
        ):
            if settings is not None:
                raise ValueError("自定义阶段拓扑不支持 stage_series_settings")
            return stages
        from bzplat.backend.contests.validation import configure_games_per_pair

        if capabilities is None:
            # The first configurable RR templates persisted ``games_per_pair``
            # but predated the scoring marker.  Preserve their frozen K and
            # duplicate topology while upgrading only an omitted/legacy
            # aggregate marker.  Explicit unknown marker values remain damaged
            # input and are rejected by the lifecycle validator below.
            assert pair_capability is not None
            if len(stages) != 1:
                return stages
            stage = dict(stages[0])
            marker_present = "series_scoring" in stage
            marker = stage.get("series_scoring")
            if marker == SERIES_SCORING_INDEPENDENT:
                return stages
            if marker_present and marker != SERIES_SCORING_AGGREGATE:
                return stages
            selected_games = stage.get(
                "games_per_pair", pair_capability.get("default")
            )
            stage.pop("games_per_pair", None)
            stage.pop("series_scoring", None)
            return configure_games_per_pair(
                [stage],
                _stored_game_id(
                    contest, entity=f"赛事 #{contest.get('id')}"
                ),
                selected_games,
                capability=pair_capability,
                stage_series_settings=None,
                stage_capabilities=None,
            )

        return configure_games_per_pair(
            stages,
            _stored_game_id(contest, entity=f"赛事 #{contest.get('id')}"),
            None,
            capability=None,
            stage_series_settings=settings,
            stage_capabilities=capabilities,
        )

    def _migrate_unstarted_series_snapshot_for_lifecycle(
        self,
        contest: dict[str, Any],
        stages: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        """Persist a required built-in default migration behind the full CAS.

        Publish/start may read an older built-in snapshot that predates the
        independent-scoring marker.  Merely computing current defaults and
        letting ``_prepare_initial_contest`` overwrite ``stages_json`` would
        bypass the zero-progress gate used by the explicit settings endpoint.
        Only when the semantic stage snapshot actually changes do we perform
        the same transactional CAS, then re-read its authoritative result.
        Participant-dependent ``effective_rounds`` is frozen later at the
        normal publication boundary and is not itself a migration trigger.
        """
        configured = self._configured_unstarted_series_stages(contest, stages)
        # Publishing is the last boundary before schedule rows become durable.
        # Validate the would-be migrated snapshot *before* its CAS write; a
        # malformed custom/built-in drift must not receive even a partial marker
        # migration and then fail later after pairings exist.
        self._validated_lifecycle_stages(contest, configured)
        if configured == stages:
            return contest, stages
        updated = self.store.compare_and_swap_unstarted_contest_stages(
            int(contest["id"]),
            expected_status=str(contest["status"]),
            expected_stages_json=str(contest.get("stages_json") or "[]"),
            stages_json=json.dumps(configured, ensure_ascii=False),
        )
        migrated = _parse_stages(updated)
        if not migrated:
            raise ValueError(f"赛事 #{contest.get('id')} 缺少有效阶段快照")
        return updated, migrated

    @staticmethod
    def _validated_lifecycle_stages(
        contest: dict[str, Any], stages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return canonical stages safe to publish/start, or fail before writes.

        Read models intentionally accept bounded legacy history.  Draft/open
        lifecycle transitions are stricter: they create new schedule and Match
        rows, so every stage must pass the complete creation validator under the
        contest's registered game.  This also freezes legacy omitted defaults
        without truthy/int coercion.
        """
        from bzplat.backend.contests.validation import validate_stage

        game_id = _stored_game_id(
            contest, entity=f"赛事 #{contest.get('id')}"
        )
        if not stages:
            raise ValueError(f"赛事 #{contest.get('id')} 缺少有效阶段快照")
        if contest_current_stage_index(contest, stage_count=len(stages)) is None:
            raise ValueError(f"赛事 #{contest.get('id')} 当前阶段游标无效")
        return [
            validate_stage(stage, index, game_id)
            for index, stage in enumerate(stages)
        ]

    @staticmethod
    def _validated_active_lifecycle_stages(
        contest: dict[str, Any], stages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Validate reached history without rewriting its scoring semantics.

        Published/running pre-v1 stages can legitimately omit fields which the
        current creation schema freezes explicitly.  Legacy aggregate stages
        must also retain their frozen one-series result.  Both use the bounded
        read contract without being rewritten; an explicit aggregate marker is
        nevertheless checked by that predicate through the same full structural
        validator as new stages.  New v1 stages use the creation validator.
        """
        from bzplat.backend.contests.validation import validate_stage

        game_id = _stored_game_id(
            contest, entity=f"赛事 #{contest.get('id')}"
        )
        if not stages:
            raise ValueError(f"赛事 #{contest.get('id')} 缺少有效阶段快照")
        validated: list[dict[str, Any]] = []
        for index, stage in enumerate(stages):
            mode = stage.get("series_scoring")
            if (
                stage.get("type") == "swiss"
                and (
                    "swiss_round_bands" in stage
                    or (
                        mode == SERIES_SCORING_INDEPENDENT
                        and "swiss_extra_rounds" in stage
                    )
                )
                and "effective_rounds" not in stage
            ):
                raise ValueError(
                    f"阶段 {index + 1} 缺少已发布的 effective_rounds"
                )
            if mode != SERIES_SCORING_INDEPENDENT:
                if not stage_scoring_contract_is_valid(stage, game_id=game_id):
                    raise ValueError(f"阶段 {index + 1} 计分契约无效")
                validated.append(dict(stage))
            else:
                validated.append(validate_stage(stage, index, game_id))
        return validated

    @staticmethod
    def _freeze_effective_stage_values(
        stages: list[dict[str, Any]], participant_count: int
    ) -> list[dict[str, Any]]:
        """Freeze participant-dependent Swiss rounds for each planned cohort.

        Later stages do not necessarily receive the initial registration
        roster.  Propagate the same bounded advancement contract used by the
        public estimator so a final Swiss stage freezes against its planned
        finalists rather than every entrant.
        """
        frozen = [dict(stage) for stage in stages]
        current_participants = participant_count
        for stage in frozen:
            if stage.get("type") == "swiss":
                stage["effective_rounds"] = effective_swiss_rounds(
                    {key: value for key, value in stage.items() if key != "effective_rounds"},
                    current_participants,
                )
            advance_per_group = stage.get("advance_per_group")
            if advance_per_group and int(advance_per_group) > 0:
                group_count = effective_group_count(
                    current_participants,
                    int(stage.get("group_count") or 4),
                )
                current_participants = min(
                    current_participants,
                    group_count * int(advance_per_group),
                )
                continue
            advance_count = stage.get("advance_count")
            if advance_count and int(advance_count) > 0:
                current_participants = min(
                    current_participants,
                    int(advance_count),
                )
        return frozen

    async def revise_stage_series_settings(
        self,
        contest_id: int,
        settings: dict[str, dict[str, Any]],
    ) -> dict | None:
        """CAS-update an unstarted template snapshot before any schedule exists."""
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                return None
            require_mutable(contest)
            if contest.get("status") not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("仅 draft/open 且尚未生成赛程的赛事可修改系列设置")
            stages = _parse_stages(contest)
            if not stages:
                raise ValueError("赛事缺少有效阶段快照")
            configured = self._configured_unstarted_series_stages(
                contest, stages, settings
            )
            return self.store.compare_and_swap_unstarted_contest_stages(
                contest_id,
                expected_status=str(contest["status"]),
                expected_stages_json=str(contest.get("stages_json") or "[]"),
                stages_json=json.dumps(configured, ensure_ascii=False),
            )

    async def revise_schedule(
        self, contest_id: int, fields: dict[str, Any]
    ) -> dict | None:
        """按赛事阶段安全修改管理端时间字段。

        draft 可调整完整排期；open 已经发生开放动作，只能调整仍在未来的
        截止/开赛时间（或清空为手动）；published 已冻结对阵，只能在尚未
        派发任何 match 时修改 ``starts_at``，并同步重排当前阶段 pending
        pairing。running/rest/终态的排期均为只读历史。

        ``title`` 是可与时间一起提交的展示元数据；若时间校验或重排失败，
        Store 仍保证它不会先行写入。
        """
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                return None
            require_mutable(contest)
            time_fields = {
                "registration_opens_at", "registration_closes_at", "starts_at",
            }
            changed_times = time_fields.intersection(fields)
            if not changed_times:
                return self.store.update_contest(contest_id, **fields)

            status = contest["status"]
            allowed_by_status = {
                CONTEST_DRAFT: time_fields,
                CONTEST_OPEN: {"registration_closes_at", "starts_at"},
                CONTEST_PUBLISHED: {"starts_at"},
            }
            allowed = allowed_by_status.get(status)
            if allowed is None:
                raise ValueError(f"赛事处于 {status} 态，时间编排只读")
            forbidden = changed_times.difference(allowed)
            if forbidden:
                labels = {
                    "registration_opens_at": "开放报名时间",
                    "registration_closes_at": "报名截止时间",
                    "starts_at": "比赛开始时间",
                }
                raise ValueError(
                    f"赛事处于 {status} 态，不能修改"
                    + "、".join(labels[key] for key in sorted(forbidden))
                )

            candidate = {
                key: fields.get(key, contest.get(key)) for key in time_fields
            }
            _validate_contest_times(
                candidate["registration_opens_at"],
                candidate["registration_closes_at"],
                candidate["starts_at"],
            )
            if status == CONTEST_OPEN:
                now = datetime.now()
                for key in ("registration_closes_at", "starts_at"):
                    value = candidate[key]
                    if value is not None and datetime.fromisoformat(value) <= now:
                        label = (
                            "报名截止时间"
                            if key == "registration_closes_at"
                            else "比赛开始时间"
                        )
                        raise ValueError(
                            f"报名中赛事的{label}必须晚于当前时间，或清空为手动"
                        )

            if status != CONTEST_PUBLISHED:
                return self.store.update_contest(contest_id, **fields)

            stages = _parse_stages(contest)
            stage_idx = contest_current_stage_index(
                contest, stage_count=len(stages)
            )
            if stage_idx is None:
                raise ValueError("赛事当前阶段不存在，不能重排")
            pairings = self.store.list_contest_pairings(contest_id)
            if any(pairing.get("match_id") for pairing in pairings):
                raise ValueError("赛事已有对局被派发，不能修改比赛开始时间")
            stage = stages[stage_idx]
            base = candidate["starts_at"]
            plans = [
                {
                    "id": pairing["id"],
                    "round_num": int(pairing.get("round_num") or 1),
                    "scheduled_at": self._stage_scheduled_at(
                        stage,
                        int(pairing.get("round_num") or 1),
                        base,
                    ),
                }
                for pairing in pairings
                if exact_nonnegative_int(pairing.get("stage_idx")) == stage_idx
                and pairing.get("status") == STATUS_PENDING
                and not pairing.get("match_id")
            ]
            return self.store.update_published_contest_schedule(
                contest_id,
                fields,
                stage_idx=stage_idx,
                pending_pairing_schedules=plans,
            )

    async def open_registration(self, contest_id: int) -> dict:
        """手动开放报名；与发布、开赛等生命周期写路径共用赛事锁。"""
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                error = self._execution_admission_error(
                    maintenance_only=True
                )
                if error is not None:
                    raise error
                return self._open_registration_locked(contest_id)

    def _open_registration_locked(self, contest_id: int) -> dict:
        """draft→open 的实际逻辑（调用方已持 per-contest 锁）。

        重复 open 是幂等读；其他状态不得倒退为 open。若
        手动提前开放时，以实际开放时刻覆盖未来计划；已到点的计划时间保留。
        """
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
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
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] != CONTEST_OPEN:
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
        contest_game = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        bot_game = _stored_game_id(bot, entity=f"Bot #{bot_id}")
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
        target_user = self.store.get_user(user_id)
        if not target_user:
            return f"user {user_id} 不存在"
        if not int(target_user.get("is_active") or 0):
            return f"user {user_id} 已停用"
        if int(contest.get("require_real_name") or 0) and not all(
            (target_user.get(field) or "").strip()
            for field in ("real_name", "phone", "school", "student_id")
        ):
            return f"user {user_id} 实名信息不完整"
        bot = self.store.get_bot(bot_id)
        if not bot or not bot.get("is_active") or not bot.get("binary_path"):
            return f"bot {bot_id} 不可用"
        if bot.get("owner_id") != user_id:
            return f"bot {bot_id} 不属于 user {user_id}"
        try:
            contest_game = _stored_game_id(contest, entity="赛事")
            bot_game = _stored_game_id(bot, entity=f"Bot #{bot_id}")
        except ValueError as exc:
            return str(exc)
        if bot_game != contest_game:
            return f"bot {bot_id} 游戏 {bot_game} ≠ 赛事 {contest_game}"
        return None

    async def add_roster_entry(
        self,
        contest_id: int,
        user_id: int,
        bot_id: int,
        *,
        allow_real_name_override: bool = False,
    ) -> dict:
        """Proxy-register one entrant; real-name capture needs admin override."""
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                raise ValueError("赛事不存在")
            require_mutable(contest)
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            if (
                int(contest.get("require_real_name") or 0)
                and not allow_real_name_override
            ):
                raise ContestRealNameRosterForbidden()
            error = self._roster_target_error(contest, user_id, bot_id)
            if error:
                raise ValueError(error)
            added, skipped, _identity_required = self.store.add_contest_roster_entries(
                contest_id,
                [(user_id, bot_id)],
                allow_real_name_override=allow_real_name_override,
                return_identity_required=True,
            )
            if skipped or not added:
                raise ValueError("该用户已报名")
            return added[0]

    async def assign_roster_entries(
        self,
        contest_id: int,
        targets: list[tuple[int, int]],
        *,
        allow_real_name_override: bool = False,
    ) -> dict:
        """Proxy-register a roster; real-name capture needs admin override."""
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                raise ValueError("赛事不存在")
            require_mutable(contest)
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            if (
                int(contest.get("require_real_name") or 0)
                and not allow_real_name_override
            ):
                raise ContestRealNameRosterForbidden()

            skipped: list[str] = []
            identity_incomplete_users: list[int] = []
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
                    if error == f"user {user_id} 实名信息不完整":
                        identity_incomplete_users.append(user_id)
                    continue
                valid.append((user_id, bot_id))

            (
                added,
                duplicate_users,
                identity_required_at_commit,
            ) = self.store.add_contest_roster_entries(
                contest_id,
                valid,
                allow_real_name_override=allow_real_name_override,
                return_identity_required=True,
            )
            skipped.extend(
                f"user {user_id} 已报名，跳过" for user_id in duplicate_users
            )
            return {
                "added": len(added),
                "skipped": skipped,
                "identity_incomplete_count": len(identity_incomplete_users),
                "identity_incomplete_users": identity_incomplete_users,
                "total_entries": len(self.store.list_contest_entries(contest_id)),
                # Private Manager/API coordination metadata.  Handlers consume
                # and remove it before serializing the public response.
                "_identity_required_at_commit": identity_required_at_commit,
            }

    async def delete_roster_entry(self, contest_id: int, user_id: int) -> bool:
        """组织者/admin 删名册；仅 draft/open，且与 publish 共用赛事锁。"""
        async with self._lock(contest_id):
            contest = self.store.get_contest(contest_id)
            if not contest:
                raise ValueError("赛事不存在")
            require_mutable(contest)
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
        require_mutable(c)
        # 换人时机：开赛前（draft/open/published）+ 中场休息（rest，受 allow_bot_swap_in_rest 控制）。
        # 不允许 running 态换人（与赛程对齐：比赛中途换 Bot 影响公平性）。
        if c["status"] not in (CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED, CONTEST_REST):
            raise ValueError("当前状态不可更换 Bot（仅开赛前或休息期可换）")
        stages = _parse_stages(c)
        idx = contest_current_stage_index(c, stage_count=len(stages))
        if idx is None:
            raise ValueError("赛事当前阶段游标损坏，不能更换 Bot")
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
        contest_game = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        bot_game = _stored_game_id(bot, entity=f"Bot #{bot_id}")
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

    def _guard_round_robin_size(self, stages: list[dict], n: int) -> None:
        """循环赛不限人数，只保留分组坐标的严格类型校验。

        物理执行仍受全局 match slots / sandbox capacity 硬顶约束，因此取消
        排期人数限制只会增加持久队列长度，不会放大同时运行的对局数。

        - round_robin / double_round_robin：全员互打，不设人数上限。
          stage.allow_large_round_robin 是历史旁路标记，现为兼容 no-op。
        - group_round_robin / group_double_round_robin：组内循环同样不限人数；
          ``group_count`` 仍须是正整数，不能让损坏快照改变分组拓扑。
        """
        del n
        for st in stages:
            t = st.get("type") or ""
            if t in ("round_robin", "double_round_robin"):
                continue
            elif t in ("group_round_robin", "group_double_round_robin"):
                gc = st.get("group_count", 4)
                if isinstance(gc, bool) or not isinstance(gc, int) or gc < 1:
                    raise ValueError("group_count 须为 ≥1 的整数")

    def _assert_engine(self, game_id: str) -> None:
        if game_id not in REGISTERED_ENGINES:
            raise ValueError(
                f"游戏引擎未注册: {game_id}（当前仅支持 {sorted(REGISTERED_ENGINES)}）"
            )

    def _bot_unavailable_reason(
        self,
        bot_id: int | None,
        *,
        expected_game: str,
        version_id: int | None = None,
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
        try:
            bot_game = _stored_game_id(bot, entity=f"Bot #{bot_id}")
        except ValueError as exc:
            return str(exc)
        if bot_game != expected_game:
            return f"Bot #{bot_id} 游戏为 {bot_game}，赛事游戏为 {expected_game}"
        version = (
            self.store.get_bot_version(int(version_id))
            if version_id is not None
            else self.store.get_current_bot_version(bot_id)
        )
        if version_id is not None and (
            version is None or int(version.get("bot_id") or 0) != int(bot_id)
        ):
            return f"Bot #{bot_id} 冻结版本不可用"
        runtime = version or bot
        try:
            require_supported_binary_metadata(
                str(runtime.get("format") or ""),
                str(runtime.get("os") or ""),
                str(runtime.get("arch") or ""),
            )
            path = str(runtime.get("binary_path") or "").strip()
            if not path:
                raise ValueError("version_unavailable")
            require_binary_file_integrity(runtime, path)
        except (OSError, TypeError, ValueError):
            return f"Bot #{bot_id} 冻结版本文件不可用"
        return None

    def _validate_initial_roster(self, contest: dict, entries: list[dict]) -> None:
        """发布/开赛前在赛事锁内复核名册可运行性。

        不允许过滤掉坏 entry 后静默开赛：那会让报名者无声消失。
        只有全部报名 entry 均有 active + binary + 游戏匹配的 Bot，
        且总数至少 2，才能生成公平的首阶段对阵。
        开赛初始化会重置历史 eliminated 标记，因此校验不能先按该标记
        过滤，否则会把实际将参赛的人漏掉。
        """
        game_id = _stored_game_id(contest, entity="赛事")
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
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                return await self._start_locked(contest_id)

    async def _start_locked(self, contest_id: int) -> dict:
        """start 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] not in (CONTEST_OPEN, CONTEST_DRAFT, CONTEST_PUBLISHED):
            raise ValueError("仅 open/draft/published 可开赛")
        # Manual start must fail before validating/mutating schedules, pairing
        # batches or lifecycle state.  The HTTP layer maps this queue gate to a
        # retryable 503 instead of pretending the contest started.
        self._require_execution_admission()
        game_id = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        self._assert_engine(game_id)

        # 必须先校验、后改 scheduled_at/status；校验失败时整个
        # start 对赛事状态与已发布排期零副作用。
        entries = self.store.list_contest_entries(contest_id)
        self._validate_initial_roster(c, entries)

        # published 态：pairing 已存在，直接改 scheduled_at=now 立即开打（不重新生成）
        if c["status"] == CONTEST_PUBLISHED:
            now = _now()
            stages = _parse_stages(c)
            stage_idx = contest_current_stage_index(
                c, stage_count=len(stages)
            )
            if stage_idx is None:
                raise ValueError("赛事当前阶段游标损坏，拒绝开赛")
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
            raise ValueError(f"赛事 #{contest_id} 缺少有效阶段快照")
        c, stages = self._migrate_unstarted_series_snapshot_for_lifecycle(
            c, stages
        )
        stages = self._validated_lifecycle_stages(c, stages)
        stages = self._freeze_effective_stage_values(stages, len(entries))
        stages = self._validated_lifecycle_stages(c, stages)

        self._guard_round_robin_size(stages, len(entries))

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
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                error = self._execution_admission_error(
                    maintenance_only=True
                )
                if error is not None:
                    raise error
                return await self._publish_locked(contest_id)

    async def _publish_locked(self, contest_id: int) -> dict:
        """publish 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] not in (CONTEST_OPEN, CONTEST_DRAFT):
            raise ValueError("仅 open/draft 可出排期")
        game_id = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        self._assert_engine(game_id)

        entries = self.store.list_contest_entries(contest_id)
        self._validate_initial_roster(c, entries)

        stages = _parse_stages(c)
        if not stages:
            raise ValueError(f"赛事 #{contest_id} 缺少有效阶段快照")
        c, stages = self._migrate_unstarted_series_snapshot_for_lifecycle(
            c, stages
        )
        stages = self._validated_lifecycle_stages(c, stages)
        stages = self._freeze_effective_stage_values(stages, len(entries))
        stages = self._validated_lifecycle_stages(c, stages)

        self._guard_round_robin_size(stages, len(entries))

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
        elimination_states: dict[int, int] = {}
        for entry in entries:
            eliminated = contest_entry_eliminated(entry)
            if eliminated is None:
                raise ValueError("参赛者淘汰状态损坏，拒绝启动赛事")
            elimination_states[int(entry["user_id"])] = int(eliminated)
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
                    "eliminated": elimination_states[int(e["user_id"])],
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

    @staticmethod
    def _private_pairing_seed(
        contest_id: int, stage_idx: int, ordinal: int
    ) -> int:
        """Allocate one private CSPRNG seed for a newly frozen pairing.

        The coordinates are validated but deliberately do not participate in
        the value: contest/stage/pairing coordinates are public, while Holdem
        consumes this seed as the actual deal sequence.
        """
        coordinates = (contest_id, stage_idx, ordinal)
        if any(
            isinstance(value, bool) or not isinstance(value, int)
            for value in coordinates
        ) or contest_id < 1 or stage_idx < 0 or ordinal < 1:
            raise ValueError("赛事对阵 seed 坐标超出范围")
        return secrets.randbelow(9_223_372_036_854_775_807) + 1

    @staticmethod
    def _duplicate_seed(pairing: dict[str, Any]) -> int:
        """Return the publication-frozen private seed, or fail closed."""
        frozen = pairing.get("pairing_seed")
        if (
            isinstance(frozen, bool)
            or not isinstance(frozen, int)
            or frozen < 1
            or frozen > 9_223_372_036_854_775_807
        ):
            raise ValueError("多场赛事对阵缺少有效的私密冻结 seed")
        return frozen

    def _stage_pairing_plan(
        self, contest: dict, stage_idx: int
    ) -> tuple[dict, list, dict[int, int]]:
        """纯计算当前阶段首批 pairing spec，不产生 DB 副作用。

        publish 硬崩恢复必须用与 ``_begin_stage`` 完全相同的规则重算
        期望批次，否则只按行数判断会把“数量相同但参赛者错了”的
        损坏数据误当完整。首阶段没有“上一阶段积分”，不读当前残缺
        pairing 的 standings，避免已落盘 bye 分反过来改变恢复排序。
        """
        stages = self._validated_active_lifecycle_stages(
            contest, _parse_stages(contest)
        )
        if stage_idx < 0 or stage_idx >= len(stages):
            raise ValueError("赛事当前阶段不存在")
        stage = stages[stage_idx]
        entry_rows = self.store.list_contest_entries(contest["id"])
        entries = active_contest_entries(entry_rows)
        if entries is None:
            raise ValueError("参赛者淘汰状态损坏，拒绝生成阶段对阵")
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
                entry["id"],
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
            rounds = effective_swiss_rounds(stage, len(bot_ids))
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
        require_mutable(c)
        stages = self._validated_active_lifecycle_stages(c, _parse_stages(c))
        current_idx = contest_current_stage_index(c, stage_count=len(stages))
        if current_idx is None:
            raise ValueError("赛事当前阶段游标损坏，拒绝生成阶段对阵")
        if stage_idx < 0 or stage_idx >= len(stages):
            self._finish_adjudicated_contest_locked(
                contest_id,
                current_idx,
                gate_stage_idx=stage_idx,
                context="invalid-stage",
            )
            return
        stage, specs, bot_to_entry = self._stage_pairing_plan(c, stage_idx)
        # specs 为空（如 single_elimination 收到 <2 bot → 无对手）：阶段无对阵 →
        # 直接 finished（防 maybe_finish 反复尝试空阶段）。
        if not specs:
            self._finish_adjudicated_contest_locked(
                contest_id,
                current_idx,
                gate_stage_idx=stage_idx,
                context="empty-stage",
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
            # starts_at 为空表示“发布后等待手动开始”。首阶段保持 NULL
            # 排期，scheduler 不得把报名截止误当成开赛；手动 start 会在
            # dispatch 前把 pending pairing 统一盖戳为 now。
            base = c.get("starts_at")
        key = stage.get("key") or f"stage{stage_idx}"
        published_at = _now()
        pairing_rows: list[dict[str, Any]] = []
        series_stage = "games_per_pair" in stage
        for ordinal, sp in enumerate(specs, start=1):
            bot_a_id, bot_b_id = self._materialize_pairing_seats(sp)
            sched = self._stage_scheduled_at(stage, sp.round_num, base)
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
                        "series_index": sp.series_index,
                        "series_size": sp.series_size,
                        "tiebreak_group": 0,
                        "tiebreak_game": 0,
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
                    "series_index": sp.series_index,
                    "series_size": sp.series_size,
                    "tiebreak_group": 0,
                    "tiebreak_game": 0,
                    "pairing_seed": (
                        self._private_pairing_seed(contest_id, stage_idx, ordinal)
                        if series_stage
                        else None
                    ),
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
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                if self._execution_admission_error() is not None:
                    return
                self._ensure_published_pairings_locked(contest_id, stage_idx)

    @staticmethod
    def _pairing_batch_signature(
        rows: list[dict], *, include_pairing_seed: bool = True
    ) -> Counter | None:
        """对阵批次的业务签名（忽略 DB id/时间/版本快照）。

        持久坐标只接受精确整数。损坏批次返回 ``None``，让 published
        恢复逻辑在无真实进度时原子重建；不能把 ``False``、浮点或文本
        通过 ``int(... or default)`` 猜成合法坐标。
        """
        signature: Counter = Counter()
        for row in rows:
            raw_round = row["round_num"] if "round_num" in row else 1
            raw_color = row["color_first"] if "color_first" in row else 0
            raw_index = row["series_index"] if "series_index" in row else 1
            raw_size = row["series_size"] if "series_size" in row else 1
            raw_tiebreak_group = (
                row["tiebreak_group"] if "tiebreak_group" in row else 0
            )
            raw_tiebreak_game = (
                row["tiebreak_game"] if "tiebreak_game" in row else 0
            )
            if (
                isinstance(raw_round, bool)
                or not isinstance(raw_round, int)
                or raw_round < 1
                or isinstance(raw_color, bool)
                or not isinstance(raw_color, int)
                or raw_color not in (0, 1)
                or isinstance(raw_index, bool)
                or not isinstance(raw_index, int)
                or raw_index < 1
                or isinstance(raw_size, bool)
                or not isinstance(raw_size, int)
                or raw_size < 1
                or isinstance(raw_tiebreak_group, bool)
                or not isinstance(raw_tiebreak_group, int)
                or isinstance(raw_tiebreak_game, bool)
                or not isinstance(raw_tiebreak_game, int)
                or not (
                    (raw_tiebreak_group == 0 and raw_tiebreak_game == 0)
                    or (
                        raw_tiebreak_group >= 1
                        and raw_tiebreak_game in (1, 2)
                    )
                )
            ):
                return None
            signature[
                (
                    raw_round,
                    row.get("entry_a_id"),
                    row.get("entry_b_id"),
                    row.get("bot_a_id"),
                    row.get("bot_b_id"),
                    row.get("stage_key") or "",
                    row.get("group_id") or "",
                    row.get("bracket_slot"),
                    raw_color,
                    row.get("pairing_seed") if include_pairing_seed else None,
                    raw_index,
                    raw_size,
                    raw_tiebreak_group,
                    raw_tiebreak_game,
                    row.get("status") or "pending",
                )
            ] += 1
        return signature

    @staticmethod
    def _published_series_seeds_are_valid(rows: list[dict]) -> bool:
        """Validate private seeds without deriving them from public coordinates."""
        seeds: list[int] = []
        playable_rows = 0
        for row in rows:
            if row.get("bot_b_id") is None:
                continue
            playable_rows += 1
            seed = row.get("pairing_seed")
            if (
                isinstance(seed, bool)
                or not isinstance(seed, int)
                or seed < 1
                or seed > 9_223_372_036_854_775_807
            ):
                return False
            seeds.append(seed)
        return playable_rows == 0 or len(seeds) == len(set(seeds))

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
        require_mutable(contest)
        frozen_stages = self._validated_active_lifecycle_stages(
            contest, _parse_stages(contest)
        )
        if stage_idx < 0 or stage_idx >= len(frozen_stages):
            raise ValueError("published 赛事阶段索引无效")
        stage, specs, bot_to_entry = self._stage_pairing_plan(contest, stage_idx)
        if not specs:
            raise ValueError("published 赛事无法生成完整对阵")

        existing = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        key = stage.get("key") or f"stage{stage_idx}"
        expected_shape: list[dict] = []
        series_stage = "games_per_pair" in stage
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
                    "series_index": spec.series_index,
                    "series_size": spec.series_size,
                    "tiebreak_group": 0,
                    "tiebreak_game": 0,
                    "status": spec.status,
                }
            )

        complete = (
            self._pairing_batch_signature(
                existing, include_pairing_seed=False
            )
            == self._pairing_batch_signature(
                expected_shape, include_pairing_seed=False
            )
            and (
                not series_stage
                or self._published_series_seeds_are_valid(existing)
            )
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
        )
        published_at = next(
            (row.get("published_at") for row in existing if row.get("published_at")),
            None,
        ) or _now()
        replacement: list[dict] = []
        for ordinal, (spec, shape) in enumerate(
            zip(specs, expected_shape), start=1
        ):
            versions = self._version_snapshot(
                shape.get("bot_a_id"), shape.get("bot_b_id")
            )
            replacement.append(
                {
                    **shape,
                    **versions,
                    "pairing_seed": (
                        self._private_pairing_seed(
                            contest_id, stage_idx, ordinal
                        )
                        if series_stage and shape.get("bot_b_id") is not None
                        else None
                    ),
                    "published_at": published_at,
                    "scheduled_at": (
                        None
                        if not spec.requires_match
                        else self._stage_scheduled_at(stage, spec.round_num, base)
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
    def _stage_scheduled_at(
        stage: dict[str, Any], round_num: int, base: str | None
    ) -> str | None:
        """用发布/恢复/管理端重排共享的阶段排期公式计算一场时间。"""
        stagger_min = max(0, int(stage.get("round_stagger_minutes") or 0))
        return ContestManager._compute_scheduled_at(round_num, base, stagger_min)

    @staticmethod
    def _compute_scheduled_at(
        round_num: int, base: str | None, stagger_min: int
    ) -> str | None:
        """逐场排期：scheduled_at = base + (round_num-1) * stagger_min 分钟。

        round_num 从 1 开始；stagger_min=0 时全用 base（同批同时）。
        """
        if base is None:
            return None
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
            binary = v or self.store.get_bot(bid)
            if binary is None:
                raise ValueError(f"bot {bid} 不存在")
            try:
                require_supported_binary_metadata(
                    str(binary.get("format") or ""),
                    str(binary.get("os") or ""),
                    str(binary.get("arch") or ""),
                )
            except ValueError as exc:
                raise ValueError(f"bot {bid} unsupported_binary：{exc}") from exc
            path = str(binary.get("binary_path") or "").strip()
            if not path:
                raise ValueError(f"bot {bid} version_unavailable")
            try:
                require_binary_file_integrity(binary, path)
            except (OSError, TypeError, ValueError) as exc:
                raise ValueError(f"bot {bid} version_unavailable") from exc
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
        async with self.deployment_activity_lock:
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
            pairing.get("bot_a_id"),
            expected_game=gid,
            version_id=pairing.get("bot_a_version_id"),
        )
        reason_b = self._bot_unavailable_reason(
            pairing.get("bot_b_id"),
            expected_game=gid,
            version_id=pairing.get("bot_b_version_id"),
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
        import secrets

        mid = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
        stages = _parse_stages(contest)
        stage_idx = pairing.get("stage_idx")
        stage_valid = bool(
            isinstance(stage_idx, int)
            and not isinstance(stage_idx, bool)
            and 0 <= stage_idx < len(stages)
        )
        stage = stages[int(stage_idx)] if stage_valid else None
        duplicate = stage_duplicate_mode(stage)
        if not stage_scoring_contract_is_valid(stage, game_id=gid):
            logger.error(
                "contest pairing blocked by malformed duplicate mode: "
                "contest=%s pairing=%s stage=%s",
                contest["id"],
                pairing["id"],
                stage_idx,
            )
            return "blocked"
        # Only the new independent game-points contract makes a technical
        # referee decision margin-neutral. Running aggregate/pre-marker history
        # keeps its frozen +/-1 series/tie-break semantics.
        neutral_technical_delta = (
            stage.get("series_scoring") == SERIES_SCORING_INDEPENDENT
        )
        ea, eb = (
            (0, 0)
            if neutral_technical_delta
            else ((-1, 1) if winner == 1 else (1, -1))
        )
        if duplicate and game_registry.get(gid).build_match_plan is None:
            raise ValueError(f"游戏 {gid} 不支持 duplicate 技术赛果")
        self.store.adjudicate_unavailable_contest_pairing(
            contest["id"],
            pairing["id"],
            mid,
            game_id=gid,
            winner=winner,
            result=build_result_payload(
                game_registry.get(gid),
                rounds_played=0,
                deltas=[ea, eb],
            ),
            duplicate=duplicate,
            activate_running=activate_running,
            require_execution_admission=self._requires_live_admission(),
        )
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

    def _dispatch_slot_budget(self) -> int | None:
        """Return the orchestrator's global Bot admission budget when supported.

        Legacy test doubles predate admission control; ``None`` preserves their
        synchronous contract without weakening the production orchestrator.
        """
        # Queue-aware orchestrators persist every due pairing without creating
        # a match.  Global capacity/contest share is enforced later by claim.
        if callable(getattr(self.orch, "start_execution_job", None)):
            return None
        capacity_fn = getattr(self.orch, "available_bot_slots", None)
        if not callable(capacity_fn):
            return None
        return max(0, int(capacity_fn()))

    async def _dispatch_pending_locked(self, contest_id: int, stage_idx: int) -> None:
        """_dispatch_pending 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        # P1-5 修复：锁内重检状态——published 可能在 scheduler snapshot 后被取消，
        # finished/cancelled 的 pending pairing 不应再派发（否则产孤儿对局）。
        if not c or c["status"] not in (CONTEST_PUBLISHED, CONTEST_RUNNING):
            return
        stages = _parse_stages(c)
        persisted_stage_idx = contest_current_stage_index(
            c, stage_count=len(stages)
        )
        requested_stage_idx = exact_nonnegative_int(stage_idx)
        if (
            persisted_stage_idx is None
            or requested_stage_idx is None
            or requested_stage_idx != persisted_stage_idx
        ):
            logger.error(
                "contest dispatch blocked by malformed/stale stage cursor: "
                "contest=%s requested=%r persisted=%r",
                contest_id,
                stage_idx,
                c.get("current_stage_idx"),
            )
            return
        stage_idx = requested_stage_idx
        # Scheduler/reconcile/completion callbacks are retry loops.  Hold the
        # existing pairing exactly as-is during deployment instead of creating
        # a technical result, binding a match or moving published -> running.
        if self._execution_admission_error() is not None:
            return
        require_mutable(c)
        now = _now()
        if c["status"] == CONTEST_PUBLISHED:
            # ``starts_at`` 为空表示发布后等待组织者手动开赛；未来时间则
            # 仍处于候场。把赛事级闸门放在 manager，而不是只依赖
            # scheduler，避免启动对账或直接调用绕过后提前派发。
            starts_at = c.get("starts_at")
            if not starts_at or starts_at > now:
                return
            self._ensure_published_pairings_locked(contest_id, stage_idx)
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        gid = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        # duplicate 阶段由 GameSpec 冻结多场计划；每个物理 Match 内的场次
        # 独立判胜计分，同牌换座后的组合 delta 只用于破同分。
        stage_cfg = stages[stage_idx] if 0 <= stage_idx < len(stages) else None
        spec = game_registry.get(gid) if gid in REGISTERED_ENGINES else None
        duplicate = stage_duplicate_mode(stage_cfg)
        if not stage_scoring_contract_is_valid(stage_cfg, game_id=gid):
            logger.error(
                "contest dispatch blocked by malformed duplicate mode: "
                "contest=%s stage=%s",
                contest_id,
                stage_idx,
            )
            return
        want_duplicate = bool(
            duplicate and spec is not None and spec.build_match_plan is not None
        )
        # ``running`` 或已有 match_id 表示本批次前已有真实进度。此时某一场准备失败
        # 不能把整个 start API 报成“全失败”：保留已启动场，失败 pairing 仍 pending，
        # 记录日志并让 scheduler 后续重试。仅 published 且零进度的首场失败向上抛。
        had_progress = c.get("status") == CONTEST_RUNNING or any(
            p.get("match_id") for p in pairings
        )
        slot_budget = self._dispatch_slot_budget()
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
            # Keep not-yet-admitted pairings genuinely pending: no match row,
            # no task waiting behind the semaphore, and no misleading running
            # badge.  A completion callback or scheduler tick will fill the next
            # free slot.
            if slot_budget is not None and slot_budget <= 0:
                break
            # 冻结快照已在 pairing 行；直接开打
            # duplicate=True 时使用发布批次私密冻结的 pairing_seed；正常重启
            # 继续读取同一持久值，保证两个换座计分场同牌可复现。
            try:
                await self._prepare_bind_start_pairing(
                    c,
                    p,
                    gid=gid,
                    want_duplicate=want_duplicate,
                    activate_running=(
                        c.get("status") == CONTEST_PUBLISHED and not had_progress
                    ),
                )
                had_progress = True
                if slot_budget is not None:
                    slot_budget -= 1
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
        want_duplicate: bool,
        activate_running: bool,
    ) -> str:
        """两阶段派发一场：prepare match → 原子绑定 pairing → 启动 runner。

        MatchOrchestrator 的真实实现支持 defer/start/discard。少量只用于单元测试的
        legacy fake 没有显式 start/discard 方法时，仍沿用其 challenge 即启动契约。
        """
        tiebreak_group = pairing.get("tiebreak_group", 0)
        if (
            isinstance(tiebreak_group, bool)
            or not isinstance(tiebreak_group, int)
            or tiebreak_group < 0
        ):
            raise ValueError("赛事对阵淘汰决胜坐标损坏")
        pairing = self.store.ensure_contest_pairing_seed_for_enqueue(
            int(contest["id"]),
            pairing,
            expected_stages_json=contest.get("stages_json"),
        )
        if callable(getattr(self.orch, "start_execution_job", None)):
            common = {
                "owner_user_id": contest["organizer_id"],
                "match_type": TYPE_CONTEST,
                "contest_id": contest["id"],
                "contest_pairing_id": pairing["id"],
                "game_id": gid,
                "bot_a_version_id": pairing.get("bot_a_version_id"),
                "bot_b_version_id": pairing.get("bot_b_version_id"),
            }
            if tiebreak_group > 0:
                common["match_seed"] = self._duplicate_seed(pairing)
            if want_duplicate:
                return await self.orch.challenge_duplicate(
                    pairing["bot_a_id"],
                    pairing["bot_b_id"],
                    duplicate_seed=self._duplicate_seed(pairing),
                    **common,
                )
            return await self.orch.challenge(
                pairing["bot_a_id"], pairing["bot_b_id"], **common
            )

        common = {
            "owner_user_id": contest["organizer_id"],
            "match_type": TYPE_CONTEST,
            "contest_id": contest["id"],
            "game_id": gid,
            "bot_a_version_id": pairing.get("bot_a_version_id"),
            "bot_b_version_id": pairing.get("bot_b_version_id"),
            "defer_start": True,
        }
        if tiebreak_group > 0:
            common["match_seed"] = self._duplicate_seed(pairing)
        mid: str | None = None
        bound = False
        try:
            if want_duplicate:
                mid = await self.orch.challenge_duplicate(
                    pairing["bot_a_id"],
                    pairing["bot_b_id"],
                    duplicate_seed=self._duplicate_seed(pairing),
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
                require_execution_admission=self._requires_live_admission(),
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
            require_mutable(c)
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
            require_mutable(contest)
            official_ready = exact_sqlite_bool(
                contest.get("official_results_ready")
            )
            if (
                contest["status"] == CONTEST_FINISHED
                or official_ready is None
                or official_ready
                or self.store.list_official_results(contest_id)
            ):
                raise ValueError("已完成或已有正式赛果的赛事不能删除")
            if contest["status"] in (CONTEST_RUNNING, CONTEST_REST):
                raise ValueError("运行中或休息期赛事不能删除，请先完成或中止在途对局")
            if self.store.executions.contest_has_active_jobs(contest_id):
                raise ValueError("赛事仍有排队或执行中的请求，不能删除")
            if self.store.contest_has_active_matches(contest_id):
                raise ValueError("赛事仍有 pending/running 对局，不能删除")
            if contest["status"] == CONTEST_PUBLISHED:
                self.store.update_contest(contest_id, status=CONTEST_CANCELLED)
            return self.store.delete_contest(contest_id)

    def standings(
        self,
        contest_id: int,
        *,
        stage_idx: int | None = None,
        pairings: list[dict[str, Any]] | None = None,
        entries: list[dict[str, Any]] | None = None,
        contest: dict[str, Any] | None = None,
    ) -> list[dict]:
        if contest is not None:
            try:
                snapshot_id = int(contest.get("id"))
            except (TypeError, ValueError):
                raise ValueError("赛事快照缺少有效 id") from None
            if snapshot_id != int(contest_id):
                raise ValueError("赛事快照 id 与 standings 请求不一致")
            c = contest
        else:
            c = self.store.get_contest(contest_id)
        if not c:
            return []
        stages = _parse_stages(c)
        current_stage_idx = contest_current_stage_index(
            c, stage_count=len(stages)
        )
        if current_stage_idx is None:
            return []
        if stage_idx is None:
            stage_idx = current_stage_idx
        if (
            isinstance(stage_idx, bool)
            or not isinstance(stage_idx, int)
            or stage_idx < 0
        ):
            return []
        stage_valid = bool(0 <= stage_idx < len(stages))
        stage = stages[stage_idx] if stage_valid else {}
        # 默认 scoring 只能从赛事声明的已注册游戏派生。
        gid = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        default_scoring = game_registry.get(gid).default_scoring
        game_spec = game_registry.get(gid)
        stage_contract_valid = bool(
            stage_valid
            and stage_scoring_contract_is_valid(stage, game_id=gid)
        )
        duplicate = stage_duplicate_mode(stage) if stage_contract_valid else None
        planned_games = (
            planned_match_games(game_spec, duplicate=duplicate)
            if duplicate is not None
            else 1
        )
        scoring = stage["scoring"] if "scoring" in stage else default_scoring

        entry_rows = (
            entries
            if entries is not None
            else self.store.list_contest_entries(contest_id)
        )
        active_entry_rows = active_contest_entries(entry_rows)
        if active_entry_rows is None:
            # The persisted SQLite flag has no CHECK constraint.  Imported
            # values such as -1/2 cannot be interpreted as elimination because
            # doing so would silently shrink the authoritative cohort.
            return []
        pairing_rows = (
            pairings
            if pairings is not None
            else self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        )
        if (
            stage.get("type") == "single_elimination"
            and stage.get("tiebreak") == ELIMINATION_TIEBREAK_PAIRED_SWAP
        ):
            # 决胜组只决定晋级，不回写原阶段积分、胜负、分差或破同分。
            # 缺失坐标按历史主赛 0/0 解释；显式损坏坐标会在生命周期
            # validator/淘汰 resolver 处 fail closed，不能被当作加赛吞掉。
            pairing_rows = [
                row
                for row in pairing_rows
                if row.get("tiebreak_group", 0) == 0
                and row.get("tiebreak_game", 0) == 0
            ]
        expected_entry_bots = {
            int(entry["id"]): entry.get("bot_id") for entry in entry_rows
        }
        expected_entry_users = {
            int(entry["id"]): int(entry["user_id"]) for entry in entry_rows
        }
        require_current_entry_bots = bool(
            stage_idx == current_stage_idx
            and c.get("status") in (CONTEST_PUBLISHED, CONTEST_RUNNING)
        )
        if not stage_valid or duplicate is None:
            # A corrupt/non-object stage snapshot has no trustworthy scoring or
            # participant topology.  Keep the roster visible with zero totals,
            # but never reinterpret linked results as a default single stage.
            entry_rows = active_entry_rows
        elif (
            stage.get("series_scoring")
            in {SERIES_SCORING_AGGREGATE, SERIES_SCORING_INDEPENDENT}
            and stage_idx > 0
        ):
            # Explicit-series topology is derived from the frozen active
            # cohort.  The lifecycle marks non-qualifiers eliminated before it
            # materializes the next stage, so surviving pairing rows are not an
            # authoritative participant list: an imported/deleted whole
            # opponent group must leave that entrant visible at zero/pending.
            entry_rows = active_entry_rows
        elif stage_idx > 0 and pairing_rows:
            participant_entry_ids = {
                int(entry_id)
                for pairing in pairing_rows
                for entry_id in (
                    pairing.get("entry_a_id"),
                    pairing.get("entry_b_id"),
                )
                if isinstance(entry_id, int) and not isinstance(entry_id, bool)
            }
            entry_rows = [
                entry
                for entry in entry_rows
                if int(entry["id"]) in participant_entry_ids
            ]
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
                "byes": 0,
                "delta_total": 0,
                "group_id": e.get("group_id") or "",
                "eliminated": int(contest_entry_eliminated(e) is True),
                "counts": {
                    "encounter_groups": 0,
                    "unique_opponents": 0,
                    "match_jobs": 0,
                    "scoring_games": 0,
                },
            }
            for e in entry_rows
        }
        encounter_keys: dict[int, set[tuple[int, int, int]]] = {
            int(entry_id): set() for entry_id in stats
        }
        opponent_ids: dict[int, set[int]] = {
            int(entry_id): set() for entry_id in stats
        }
        matches_by_id: dict[str, dict[str, Any]] = {}
        if not stage_valid or duplicate is None:
            return list(stats.values())
        if is_aggregate_series_stage(stage):
            for pairing in pairing_rows:
                match_id = pairing.get("match_id")
                if not match_id:
                    if (
                        stage.get("type") == "swiss"
                        and is_authoritative_no_opponent_pairing(
                            stage.get("type"), pairing
                        )
                        and contest_pairing_roster_binding_is_valid(
                            pairing,
                            expected_contest_id=contest_id,
                            expected_entry_bots=expected_entry_bots,
                            expected_entry_users=expected_entry_users,
                            require_current_entry_bots=require_current_entry_bots,
                            require_opponent=False,
                        )
                    ):
                        entry_id = pairing.get("entry_a_id")
                        if entry_id in stats:
                            stats[entry_id]["points"] += points_for_result(
                                scoring, 0, 0
                            )
                            stats[entry_id]["byes"] += 1
                    continue
                if "_match_result_json" in pairing:
                    raw_result = pairing.get("_match_result_json")
                    if isinstance(raw_result, str):
                        try:
                            result = json.loads(raw_result)
                        except (TypeError, ValueError):
                            result = {}
                    else:
                        result = raw_result if isinstance(raw_result, dict) else {}
                    matches_by_id[str(match_id)] = {
                        "id": str(match_id),
                        "status": pairing.get("match_status"),
                        "winner": pairing.get("match_winner"),
                        "result": result,
                        "reason": pairing.get("_match_reason"),
                        "technical_loss": pairing.get("_match_technical_loss"),
                        "match_config": pairing.get("_match_config_json"),
                        "contest_id": pairing.get("_match_contest_id"),
                        "game_id": pairing.get("_match_game_id"),
                        "match_type": pairing.get("_match_type"),
                        "bot_a_id": pairing.get("_match_bot_a_id"),
                        "bot_b_id": pairing.get("_match_bot_b_id"),
                    }
                else:
                    match = self.store.get_match(str(match_id))
                    if match:
                        matches_by_id[str(match_id)] = match

            for rows in group_conceptual_series(stage, pairing_rows).values():
                summary = summarize_conceptual_series(
                    stage,
                    rows,
                    matches_by_id.get,
                    game_spec=game_spec,
                    expected_contest_id=contest_id,
                    expected_entry_bots=expected_entry_bots,
                    expected_entry_users=expected_entry_users,
                    require_current_entry_bots=require_current_entry_bots,
                )
                first, second = summary["entries"]
                if first not in stats or second not in stats:
                    continue
                completed_matches = int(summary["completed_matches"])
                for entry_id in (first, second):
                    stats[entry_id]["counts"]["match_jobs"] += completed_matches
                    if completed_matches:
                        stats[entry_id]["counts"]["encounter_groups"] += 1
                if completed_matches:
                    opponent_ids[first].add(second)
                    opponent_ids[second].add(first)
                if summary["settled"]:
                    for entry_id in (first, second):
                        stats[entry_id]["delta_total"] += int(
                            summary["deltas"][entry_id]
                        )
                        stats[entry_id]["points"] += float(
                            summary["standings_points"][entry_id]
                        )
                        stats[entry_id]["counts"]["scoring_games"] += 1
                    winner_entry = summary["winner_entry"]
                    if winner_entry is None:
                        stats[first]["draws"] += 1
                        stats[second]["draws"] += 1
                    else:
                        loser_entry = second if winner_entry == first else first
                        stats[winner_entry]["wins"] += 1
                        stats[loser_entry]["losses"] += 1
                group_id = str(rows[0].get("group_id") or "")
                if group_id:
                    stats[first]["group_id"] = group_id
                    stats[second]["group_id"] = group_id
            for entry_id, opponents in opponent_ids.items():
                stats[entry_id]["counts"]["unique_opponents"] = len(opponents)
            rows = list(stats.values())
            rows.sort(key=lambda row: (-row["points"], -row["delta_total"]))
            # Running legacy aggregate stages remain on their frozen one-score-
            # per-series semantics, but their live order must use the exact
            # historical official tie-break chain that advancement/finalize
            # will consume.  A points/delta-only preview could otherwise show
            # a different qualifier until the instant the stage is snapshotted.
            from bzplat.backend.contests import ranking as _ranking

            return _ranking.compute_official_ranking(
                rows,
                pairing_rows,
                matches_by_id,
                normalize_delta=game_spec.normalize_delta,
                stage=stage,
                planned_games_per_match=planned_games,
                fixed_rounds_per_match=game_spec.fixed_rounds_per_match,
                game_id=gid,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )

        for p in pairing_rows:
            mid = p.get("match_id")
            if not mid:
                # Swiss 奇数轮的 bye 是显式 completed/no-match pairing。
                # 轮空获得本赛制的“胜场分”，但它不是一场对局：不增
                # wins/draws/losses、delta_total，也没有对手记录。KO bye
                # 是直接晋级，不在此计分。
                if stage.get("type") == "swiss" and (
                    is_authoritative_no_opponent_pairing(stage.get("type"), p)
                    and contest_pairing_roster_binding_is_valid(
                        p,
                        expected_contest_id=contest_id,
                        expected_entry_bots=expected_entry_bots,
                        expected_entry_users=expected_entry_users,
                        require_current_entry_bots=require_current_entry_bots,
                        require_opponent=False,
                    )
                ):
                    entry_id = p.get("entry_a_id")
                    if entry_id in stats:
                        stats[entry_id]["points"] += swiss_bye_points(
                            stage,
                            scoring=scoring,
                            scoring_games_per_match=planned_games,
                        )
                        stats[entry_id]["byes"] += 1
                continue
            if "_match_result_json" in p:
                raw_result = p.get("_match_result_json")
                if isinstance(raw_result, str):
                    try:
                        result = json.loads(raw_result)
                    except (TypeError, ValueError):
                        result = {}
                else:
                    result = raw_result if isinstance(raw_result, dict) else {}
                m = {
                    "id": str(mid),
                    "status": p.get("match_status"),
                    "winner": p.get("match_winner"),
                    "result": result,
                    "reason": p.get("_match_reason"),
                    "technical_loss": p.get("_match_technical_loss"),
                    "match_config": p.get("_match_config_json"),
                    "contest_id": p.get("_match_contest_id"),
                    "game_id": p.get("_match_game_id"),
                    "match_type": p.get("_match_type"),
                    "bot_a_id": p.get("_match_bot_a_id"),
                    "bot_b_id": p.get("_match_bot_b_id"),
                }
            else:
                m = self.store.get_match(mid)
            if not m or m["status"] != STATUS_COMPLETED:
                continue
            matches_by_id[str(mid)] = m
            ea_id = p.get("entry_a_id")
            eb_id = p.get("entry_b_id")
            if ea_id not in stats or eb_id not in stats:
                continue
            if not contest_match_binding_is_valid(
                p,
                m,
                expected_contest_id=contest_id,
                expected_game_id=gid,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            ):
                continue
            games = scoring_games_for_match(
                m,
                duplicate=duplicate,
                planned_games=planned_games,
                fixed_rounds_per_match=game_spec.fixed_rounds_per_match,
                require_frozen_duplicate=(
                    stage.get("series_scoring") == SERIES_SCORING_INDEPENDENT
                ),
                normalize_delta=game_spec.normalize_delta,
            )
            if not games:
                continue
            # A completed database row is not an authoritative played job until
            # its shared scoring parser succeeds.  Keep personal job/opponent/
            # encounter counts aligned with stage progress and W/D/L instead of
            # exposing malformed history as a zero-game opponent encounter.
            stats[ea_id]["counts"]["match_jobs"] += 1
            stats[eb_id]["counts"]["match_jobs"] += 1
            opponent_ids[int(ea_id)].add(int(eb_id))
            opponent_ids[int(eb_id)].add(int(ea_id))
            encounter_key = conceptual_series_key(stage, p)
            if encounter_key is not None:
                encounter_keys[int(ea_id)].add(encounter_key)
                encounter_keys[int(eb_id)].add(encounter_key)
            for game in games:
                if game.deltas is not None:
                    stats[ea_id]["delta_total"] += int(game.deltas[0])
                    stats[eb_id]["delta_total"] += int(game.deltas[1])
                stats[ea_id]["points"] += points_for_result(
                    scoring, game.winner, 0
                )
                stats[eb_id]["points"] += points_for_result(
                    scoring, game.winner, 1
                )
                stats[ea_id]["counts"]["scoring_games"] += 1
                stats[eb_id]["counts"]["scoring_games"] += 1
                if game.winner == 0:
                    stats[ea_id]["wins"] += 1
                    stats[eb_id]["losses"] += 1
                elif game.winner == 1:
                    stats[eb_id]["wins"] += 1
                    stats[ea_id]["losses"] += 1
                else:
                    stats[ea_id]["draws"] += 1
                    stats[eb_id]["draws"] += 1
            group_id = p.get("group_id") or ""
            if group_id:
                stats[ea_id]["group_id"] = group_id
                stats[eb_id]["group_id"] = group_id
        for entry_id, keys in encounter_keys.items():
            stats[entry_id]["counts"]["encounter_groups"] = len(keys)
            stats[entry_id]["counts"]["unique_opponents"] = len(
                opponent_ids[entry_id]
            )
        rows = list(stats.values())
        rows.sort(key=lambda r: (-r["points"], -r["delta_total"]))
        if stage.get("series_scoring") == SERIES_SCORING_INDEPENDENT:
            from bzplat.backend.contests import ranking as _ranking

            return _ranking.compute_official_ranking(
                rows,
                pairing_rows,
                matches_by_id,
                normalize_delta=game_spec.normalize_delta,
                stage=stage,
                planned_games_per_match=planned_games,
                fixed_rounds_per_match=game_spec.fixed_rounds_per_match,
                game_id=gid,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )
        return rows

    def _stage_done(self, contest_id: int, stage_idx: int) -> bool:
        contest = self.store.get_contest(contest_id)
        if not contest:
            return False
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        stages = _parse_stages(contest or {})
        current_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            return False
        stage_type = (
            stages[stage_idx].get("type")
            if 0 <= stage_idx < len(stages)
            else None
        )
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        stage = (
            stages[stage_idx]
            if 0 <= stage_idx < len(stages) and isinstance(stages[stage_idx], dict)
            else None
        )
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            return False
        assert stage is not None
        game_spec = game_registry.get(game_id)
        duplicate = stage_duplicate_mode(stage)
        if duplicate is None:
            return False
        planned_games = planned_match_games(game_spec, duplicate=duplicate)
        entries = self.store.list_contest_entries(contest_id)
        active_entries = active_contest_entries(entries)
        if active_entries is None:
            return False
        expected_entry_ids = [
            int(entry["id"])
            for entry in active_entries
        ]
        cumulative_deltas: dict[int, int] = {
            int(entry_id): 0 for entry_id in expected_entry_ids
        }
        if not pairings:
            # An explicit series marker has a derivable empty topology only for
            # a zero/one-person cohort.  This also keeps already-running legacy
            # aggregate stages from wedging after the scoring cutover.  Any
            # larger empty graph is missing durable fixture rows.
            return bool(
                stage.get("series_scoring")
                in {SERIES_SCORING_AGGREGATE, SERIES_SCORING_INDEPENDENT}
                and len(expected_entry_ids) <= 1
            )
        if "games_per_pair" in stage:
            real_pairings = [
                pairing
                for pairing in pairings
                if not is_authoritative_no_opponent_pairing(stage_type, pairing)
            ]
            if not series_rows_settled(
                stage,
                real_pairings,
                self.store.get_match,
                game_spec=game_spec,
                all_pairings=pairings,
                expected_entry_ids=expected_entry_ids,
                expected_swiss_rounds=(
                    effective_swiss_rounds(stage, len(expected_entry_ids))
                    if stage.get("type") == "swiss"
                    else None
                ),
                expected_contest_id=contest_id,
                expected_entry_bots={
                    int(entry["id"]): entry.get("bot_id") for entry in entries
                },
                expected_entry_users={
                    int(entry["id"]): int(entry["user_id"]) for entry in entries
                },
                require_current_entry_bots=contest.get("status") in (
                    CONTEST_PUBLISHED,
                    CONTEST_RUNNING,
                ),
            ):
                return False
        for p in pairings:
            # 只有赛制允许且身份/对局/状态四项完整的 no-opponent 行才是
            # 已完成轮空。真实对手 Bot 被 FK SET NULL 后仍保留 entry_b_id，
            # 必须阻断阶段推进。
            if is_authoritative_no_opponent_pairing(stage_type, p):
                if not contest_pairing_roster_binding_is_valid(
                    p,
                    expected_contest_id=contest_id,
                    expected_entry_bots={
                        int(entry["id"]): entry.get("bot_id") for entry in entries
                    },
                    expected_entry_users={
                        int(entry["id"]): int(entry["user_id"])
                        for entry in entries
                    },
                    require_current_entry_bots=contest.get("status") in (
                        CONTEST_PUBLISHED,
                        CONTEST_RUNNING,
                    ),
                    require_opponent=False,
                ):
                    return False
                continue
            mid = p.get("match_id")
            if not mid:
                return False
            m = self.store.get_match(mid)
            # aborted 只表示对局被取消/未产生裁决，绝不是赛制上的
            # “已完成”。把它算作终态会让 KO 在 winner=None 时固定
            # 晋级座位 0，也会给 RR/Swiss 静默吞分。
            if not match_scoring_result_is_valid(
                stage,
                m,
                game_spec=game_spec,
                pairing=p,
                expected_contest_id=contest_id,
                expected_entry_bots={
                    int(entry["id"]): entry.get("bot_id") for entry in entries
                },
                expected_entry_users={
                    int(entry["id"]): int(entry["user_id"]) for entry in entries
                },
                require_current_entry_bots=contest.get("status") in (
                    CONTEST_PUBLISHED,
                    CONTEST_RUNNING,
                ),
            ):
                return False
            games = scoring_games_for_match(
                m,
                duplicate=duplicate,
                planned_games=planned_games,
                fixed_rounds_per_match=game_spec.fixed_rounds_per_match,
                require_frozen_duplicate=(
                    stage.get("series_scoring") == SERIES_SCORING_INDEPENDENT
                ),
                normalize_delta=game_spec.normalize_delta,
            )
            if not games:
                return False
            entry_a = int(p["entry_a_id"])
            entry_b = int(p["entry_b_id"])
            cumulative_deltas[entry_a] = cumulative_deltas.get(entry_a, 0) + sum(
                int(game.deltas[0]) for game in games
            )
            cumulative_deltas[entry_b] = cumulative_deltas.get(entry_b, 0) + sum(
                int(game.deltas[1]) for game in games
            )
        if any(
            normalized_delta_value(game_spec.normalize_delta, delta) is None
            for delta in cumulative_deltas.values()
        ):
            return False
        return True

    def _snapshot_stage_results(self, contest_id: int, stage_idx: int) -> None:
        c = self.store.get_contest(contest_id)
        stages = _parse_stages(c)
        key = ""
        grouped = False
        if 0 <= stage_idx < len(stages):
            key = stages[stage_idx].get("key") or f"stage{stage_idx}"
            grouped = str(stages[stage_idx].get("type") or "").startswith("group_")
        group_ranks: Counter = Counter()
        for i, s in enumerate(self._rank_stage_rows(contest_id, stage_idx)):
            group_key = s.get("group_id") or "_"
            group_ranks[group_key] += 1
            tiebreaks = sanitize_public_contest_tiebreaks(s.get("tiebreaks"))
            if tiebreaks is None:
                # A completed independent snapshot must retain the exact
                # ranking chain that selected advancement.  Refuse a partial
                # durable row instead of later presenting a different order.
                raise ValueError("阶段破同分明细无效，无法固化阶段结果")
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
                delta_total=s["delta_total"],
                group_id=s.get("group_id") or "",
                rank_in_group=(group_ranks[group_key] if grouped else i + 1),
                payload_json=json.dumps(
                    {"tiebreaks": tiebreaks},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
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

    def _sync_completed_pairings(self, contest_id: int, stage_idx: int) -> int:
        """Idempotently repair per-match pairing status for one stage."""
        changed = 0
        for pairing in self.store.list_contest_pairings(
            contest_id, stage_idx=stage_idx
        ):
            if pairing.get("status") == STATUS_COMPLETED:
                continue
            match_id = pairing.get("match_id")
            if not match_id:
                continue
            if self.store.complete_contest_pairing_for_match(
                contest_id, match_id
            ):
                changed += 1
        return changed

    def _backfill_actual_start(self, contest: dict) -> bool:
        """Repair legacy contests whose first match started with NULL starts_at."""
        if contest.get("status") not in (
            CONTEST_RUNNING,
            CONTEST_REST,
            CONTEST_FINISHED,
        ):
            return False
        actual = self.store.backfill_contest_actual_start(contest["id"])
        if actual is None:
            return False
        logger.warning(
            "contest %s repaired missing starts_at from first match: %s",
            contest["id"], actual,
        )
        return True

    def _advance_participants(self, contest_id: int, stage_idx: int) -> None:
        """根据阶段配置标记淘汰（不晋级者）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            return
        stages = _parse_stages(c)
        current_stage_idx = contest_current_stage_index(c, stage_count=len(stages))
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            raise ValueError("赛事当前阶段游标损坏或已变化，拒绝计算晋级名单")
        stage = stages[stage_idx]
        game_id = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            # Recovery/resume can reach this helper independently of the usual
            # completed-stage gate.  Never coerce a damaged frozen advance
            # count and permanently eliminate entrants under invented rules.
            raise ValueError("阶段计分契约无效，拒绝计算晋级名单")
        entries = self.store.list_contest_entries(contest_id)
        active_entries = active_contest_entries(entries)
        if active_entries is None:
            raise ValueError("参赛者淘汰状态损坏，拒绝计算晋级名单")
        standings = self._rank_stage_rows(contest_id, stage_idx)
        expected_entry_ids = {int(entry["id"]) for entry in active_entries}
        ranked_entry_ids = {
            row.get("entry_id")
            for row in standings
            if isinstance(row.get("entry_id"), int)
            and not isinstance(row.get("entry_id"), bool)
        }
        if ranked_entry_ids != expected_entry_ids:
            raise ValueError("阶段排名不完整，拒绝计算晋级名单")
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

        for e in entries:
            if e["id"] not in advance:
                self.store.update_entry(contest_id, e["user_id"], eliminated=1)

    def _rank_stage_rows(self, contest_id: int, stage_idx: int) -> list[dict]:
        """Rank one frozen stage with the same tie-break chain used at finish."""
        from bzplat.backend.contests import ranking as _ranking

        contest = self.store.get_contest(contest_id)
        if not contest:
            return []
        stages = _parse_stages(contest)
        stage = stages[stage_idx] if 0 <= stage_idx < len(stages) else None
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        standings = self.standings(contest_id, stage_idx=stage_idx)
        if stage is None:
            return standings
        pairings = self.store.list_contest_pairings(
            contest_id, stage_idx=stage_idx
        )
        match_ids = [pairing["match_id"] for pairing in pairings if pairing.get("match_id")]
        matches = {
            match_id: self.store.get_match(match_id)
            for match_id in match_ids
            if match_id
        }
        entries = self.store.list_contest_entries(contest_id)
        game_spec = game_registry.get(game_id)
        duplicate = stage_duplicate_mode(stage)
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            return standings
        current_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        if current_stage_idx is None:
            return []
        scoring_pairings = pairings
        if (
            stage.get("type") == "single_elimination"
            and stage.get("tiebreak") == ELIMINATION_TIEBREAK_PAIRED_SWAP
        ):
            scoring_pairings = [
                row
                for row in pairings
                if row.get("tiebreak_group", 0) == 0
                and row.get("tiebreak_game", 0) == 0
            ]
        ranked = _ranking.compute_official_ranking(
            standings,
            scoring_pairings,
            {
                str(row["match_id"]): matches[str(row["match_id"])]
                for row in scoring_pairings
                if row.get("match_id")
                and matches.get(str(row["match_id"]))
            },
            normalize_delta=game_spec.normalize_delta,
            stage=stage,
            planned_games_per_match=planned_match_games(
                game_spec, duplicate=duplicate
            ),
            fixed_rounds_per_match=game_spec.fixed_rounds_per_match,
            game_id=game_id,
            expected_contest_id=contest_id,
            expected_entry_bots={
                int(entry["id"]): entry.get("bot_id") for entry in entries
            },
            expected_entry_users={
                int(entry["id"]): int(entry["user_id"]) for entry in entries
            },
            require_current_entry_bots=bool(
                stage_idx == current_stage_idx
                and contest.get("status") in (
                    CONTEST_PUBLISHED,
                    CONTEST_RUNNING,
                )
            ),
        )
        if stage.get("tiebreak") != ELIMINATION_TIEBREAK_PAIRED_SWAP:
            return ranked

        expected_entry_bots = {
            int(entry["id"]): entry.get("bot_id") for entry in entries
        }
        expected_entry_users = {
            int(entry["id"]): int(entry["user_id"]) for entry in entries
        }
        progress = {int(row["entry_id"]): 0 for row in ranked}
        by_encounter: dict[tuple[int, int], list[dict[str, Any]]] = {}
        for pairing in pairings:
            round_num = pairing.get("round_num")
            slot = pairing.get("bracket_slot")
            if (
                isinstance(round_num, bool)
                or not isinstance(round_num, int)
                or round_num < 1
                or isinstance(slot, bool)
                or not isinstance(slot, int)
                or slot < 0
            ):
                return []
            by_encounter.setdefault((round_num, slot), []).append(pairing)
        for (round_num, _slot), rows in by_encounter.items():
            if len(rows) == 1 and is_authoritative_no_opponent_pairing(
                stage.get("type"), rows[0]
            ):
                entry_id = rows[0].get("entry_a_id")
                if entry_id not in progress:
                    return []
                progress[int(entry_id)] = max(
                    progress[int(entry_id)], round_num + 1
                )
                continue
            summary = summarize_elimination_encounter(
                stage,
                rows,
                self.store.get_match,
                game_spec=game_spec,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=bool(
                    stage_idx == current_stage_idx
                    and contest.get("status") in (
                        CONTEST_PUBLISHED,
                        CONTEST_RUNNING,
                    )
                ),
            )
            winner_entry = summary.get("winner_entry")
            primary = next(
                (
                    row
                    for row in rows
                    if row.get("tiebreak_group", 0) == 0
                    and row.get("tiebreak_game", 0) == 0
                ),
                None,
            )
            if summary.get("state") != "decided" or primary is None:
                return []
            entrants = {primary.get("entry_a_id"), primary.get("entry_b_id")}
            if winner_entry not in entrants or None in entrants:
                return []
            loser_entry = next(entry for entry in entrants if entry != winner_entry)
            if winner_entry not in progress or loser_entry not in progress:
                return []
            progress[int(loser_entry)] = max(
                progress[int(loser_entry)], round_num
            )
            progress[int(winner_entry)] = max(
                progress[int(winner_entry)], round_num + 1
            )
        ranked.sort(key=lambda row: (-progress[int(row["entry_id"])], row["rank"]))
        for rank, row in enumerate(ranked, start=1):
            row["rank"] = rank
        return ranked

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
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                contest = self.store.get_contest(contest_id)
                match = self.store.get_match(match_id)
                if not contest or not match:
                    return None
                if is_showcase(contest):
                    return contest
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
                            # The queue keeps a terminal match's job in ``settling``
                            # until exact sandbox cleanup is confirmed.  Enqueue is
                            # idempotent for an active contest_pairing_id, so trying
                            # to redispatch before finalization merely returns the old
                            # job and makes "immediate" admin redispatch a no-op.
                            # Finalize after the orchestrator's cleanup barrier, then
                            # verify this specific job no longer occupies the pairing.
                            old_execution = self.store.executions.get_by_match(match_id)
                            self.store.executions.finalize_ready()
                            if old_execution is not None:
                                latest_execution = self.store.executions.get(
                                    str(old_execution["public_id"])
                                )
                                if latest_execution and latest_execution.get("status") in {
                                    "queued", "starting", "running", "settling"
                                }:
                                    logger.warning(
                                        "contest admin abort awaits execution cleanup: "
                                        "contest=%s pairing=%s request=%s",
                                        contest_id,
                                        pairing["id"],
                                        old_execution["public_id"],
                                    )
                                    return self.store.get_contest(contest_id)
                            pairing_stage_idx = exact_nonnegative_int(
                                pairing.get("stage_idx")
                            )
                            if pairing_stage_idx is None:
                                logger.error(
                                    "contest pairing has malformed stage cursor: "
                                    "contest=%s pairing=%s",
                                    contest_id,
                                    pairing.get("id"),
                                )
                                return self.store.get_contest(contest_id)
                            await self._dispatch_pending_locked(
                                contest_id, pairing_stage_idx
                            )
                    return self.store.get_contest(contest_id)
                if match.get("status") == STATUS_COMPLETED:
                    self.store.complete_contest_pairing_for_match(
                        contest_id, match_id
                    )
                result = await self._maybe_finish_locked(contest_id)
                latest = self.store.get_contest(contest_id)
                if latest and latest.get("status") == CONTEST_RUNNING:
                    latest_stages = _parse_stages(latest)
                    latest_stage_idx = contest_current_stage_index(
                        latest, stage_count=len(latest_stages)
                    )
                    if latest_stage_idx is not None:
                        await self._dispatch_pending_locked(
                            contest_id, latest_stage_idx
                        )
                return result or self.store.get_contest(contest_id)

    async def maybe_finish(self, contest_id: int) -> dict | None:
        """对局结束回调：检查当前阶段是否完成，进入 rest 或下一阶段。

        加 per-contest 锁串行化——防止多场对局同时完成的 on_match_done 并发回调
        + scheduler 并发调用导致重复生成轮次/重复对局。
        """
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                return await self._maybe_finish_locked(contest_id)

    async def _maybe_finish_locked(self, contest_id: int) -> dict | None:
        """maybe_finish 的实际逻辑（调用方已持锁）。"""
        c = self.store.get_contest(contest_id)
        if not c or c["status"] not in (CONTEST_RUNNING, CONTEST_REST):
            return None
        if is_showcase(c):
            return c
        if c["status"] == CONTEST_REST:
            return await self._maybe_auto_resume(contest_id)

        raw_stages = _parse_stages(c)
        stage_idx = contest_current_stage_index(
            c, stage_count=len(raw_stages)
        )
        if stage_idx is None:
            logger.error(
                "contest %s has malformed current_stage_idx; lifecycle blocked",
                contest_id,
            )
            return None
        self._sync_completed_pairings(contest_id, stage_idx)
        # Mirroring an already completed match is part of draining that active
        # work.  New rounds, stage snapshots and lifecycle transitions are not:
        # hold them until explicit maintenance end so ready cannot race a
        # scheduler write after the execution callback has quiesced.
        if self._execution_admission_error() is not None:
            return self.store.get_contest(contest_id)
        try:
            stages = self._validated_active_lifecycle_stages(c, raw_stages)
        except ValueError as exc:
            # A damaged future stage is part of the same frozen lifecycle
            # contract.  Block before snapshotting the current ranking,
            # eliminating entrants, or creating any next-stage pairing; those
            # writes are irreversible even if dispatch later fails closed.
            logger.error(
                "contest %s has invalid frozen stage contract: %s",
                contest_id,
                exc,
            )
            return None
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
                elimination_state = await self._maybe_next_elim_round(
                    contest_id, stage_idx, stage
                )
                if elimination_state == "created":
                    return None  # 生成了下一轮（半决赛/决赛），阶段未完成
                if elimination_state == "blocked":
                    # 淘汰赛已有 completed 和棋但没有权威晋级者。
                    # 保持 running，不 snapshot/finish/advance，也不擅自重赛。
                    return None
                if elimination_state != "champion":  # pragma: no cover - typed guard
                    logger.error(
                        "unknown elimination advance state: contest=%s stage=%s state=%r",
                        contest_id,
                        stage_idx,
                        elimination_state,
                    )
                    return None

        has_next = stage_idx + 1 < len(stages)
        if not has_next and self._has_unfinished_pairings(contest_id):
            # ``_stage_done`` intentionally checks only the current stage so it
            # can drive ordinary stage generation.  Freezing a terminal result
            # is stricter: a low-level delete/abort in any earlier stage must
            # not be hidden merely because the final stage completed.
            logger.error(
                "skip automatic finalization for unadjudicated contest=%s",
                contest_id,
            )
            return None

        self._mark_stage_pairings_done(contest_id, stage_idx)
        self._snapshot_stage_results(contest_id, stage_idx)
        rest_min = int((stages[stage_idx].get("rest_after_minutes") or 0) if stages else 0)

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

        return self._finish_adjudicated_contest_locked(
            contest_id,
            stage_idx,
            context="automatic",
        ) or self.store.get_contest(contest_id)

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
        maintenance = self.store.executions.is_maintenance_control(
            self.store.executions.control()
        )
        # Under deployment drain the dispatcher still invokes recovery so
        # already-active attempts can be compensated, but proactive contest
        # lifecycle writes must wait for explicit maintenance end.
        if maintenance:
            return 0

        # 0. 修复旧版本留下的观测时间线。此步骤幂等，覆盖 finished
        # 历史赛事以及当前 running 赛事，不改任何裁决结果。
        for contest in self.store.list_contests():
            if is_showcase(contest):
                continue
            self._backfill_actual_start(contest)
            raw_pairings = self.store.list_contest_pairings(contest["id"])
            stage_indices = {
                exact_nonnegative_int(pairing.get("stage_idx"))
                for pairing in raw_pairings
            }
            if None in stage_indices:
                logger.error(
                    "skip contest reconciliation for malformed pairing stage: "
                    "contest=%s",
                    contest["id"],
                )
                continue
            for stage_idx in stage_indices:
                assert stage_idx is not None
                self._sync_completed_pairings(contest["id"], stage_idx)

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
                official_ready = (
                    exact_sqlite_bool(latest.get("official_results_ready"))
                    if latest
                    else None
                )
                if (
                    latest
                    and latest["status"] == CONTEST_FINISHED
                    and official_ready is False
                ):
                    latest_stages = _parse_stages(latest)
                    stage_idx = contest_current_stage_index(
                        latest, stage_count=len(latest_stages)
                    )
                    if stage_idx is None:
                        logger.error(
                            "skip official-results recovery for malformed "
                            "current_stage_idx contest=%s",
                            contest_id,
                        )
                        return
                    # Stage snapshots are written before the terminal official
                    # batch.  If that final transaction alone failed, recover
                    # the already adjudicated order verbatim: reopening the DB
                    # may normalize old Match JSON under a newer result schema,
                    # and recomputing would silently rewrite history.
                    if self._finalize_official_results_from_stage_snapshots(
                        contest_id, stage_idx
                    ):
                        return
                    # A terminal status alone is not proof that every durable
                    # pairing was adjudicated.  Recovery uses the same
                    # all-stage fail-closed gate as automatic and force finish.
                    if self._has_unfinished_pairings(contest_id):
                        logger.error(
                            "skip official-results recovery for unfinished "
                            "terminal contest=%s",
                            contest_id,
                        )
                        return
                    self._finalize_official_results(contest_id, stage_idx)
            return
        if initial and initial["status"] == CONTEST_PUBLISHED:
            initial_stages = _parse_stages(initial)
            stage_idx = contest_current_stage_index(
                initial, stage_count=len(initial_stages)
            )
            if stage_idx is None:
                logger.error(
                    "skip published recovery for malformed current_stage_idx "
                    "contest=%s",
                    contest_id,
                )
                return
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

        current_stages = _parse_stages(c)
        stage_idx = contest_current_stage_index(
            c, stage_count=len(current_stages)
        )
        if stage_idx is None:
            logger.error(
                "skip running recovery for malformed current_stage_idx contest=%s",
                contest_id,
            )
            return
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
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                await self._dispatch_pending_safe_locked(contest_id, stage_idx)

    async def _dispatch_pending_safe_locked(self, contest_id: int, stage_idx: int) -> None:
        """_dispatch_pending_safe 的实际逻辑（调用方已持锁）。"""
        c = self.store.get_contest(contest_id)
        # reconcile 在锁外按 running 快照选中赛事后，可能先被 finish 收尾；
        # 锁内必须重检，终态不得再派发或制造 aborted 占位对局。
        if not c or c["status"] != CONTEST_RUNNING:
            return
        if self._execution_admission_error() is not None:
            return
        gid = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        # 复式赛制判断（与 _dispatch_pending_locked 一致）——reconcile 重派也保留
        # duplicate 标志（复审 P2-2），否则同赛事会混入普通单场对局。
        stages = self._validated_active_lifecycle_stages(c, _parse_stages(c))
        stage_cfg = stages[stage_idx] if 0 <= stage_idx < len(stages) else None
        spec = game_registry.get(gid) if gid in REGISTERED_ENGINES else None
        duplicate = stage_duplicate_mode(stage_cfg)
        if not stage_scoring_contract_is_valid(stage_cfg, game_id=gid):
            logger.error(
                "contest redispatch blocked by malformed duplicate mode: "
                "contest=%s stage=%s",
                contest_id,
                stage_idx,
            )
            return
        want_duplicate = bool(
            duplicate and spec is not None and spec.build_match_plan is not None
        )
        pending = [
            p
            for p in self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
            if p.get("status") == "pending" and not p.get("match_id")
        ]
        slot_budget = self._dispatch_slot_budget()
        for p in pending:
            unavailable = self._adjudicate_unavailable_pairing(
                c, p, gid=gid, activate_running=False
            )
            if unavailable != "ready":
                continue
            if slot_budget is not None and slot_budget <= 0:
                break
            try:
                await self._prepare_bind_start_pairing(
                    c,
                    p,
                    gid=gid,
                    want_duplicate=want_duplicate,
                    activate_running=False,
                )
                if slot_budget is not None:
                    slot_budget -= 1
            except Exception:
                logger.exception(
                    "reconcile: contest=%s pairing=%s 重派失败，保持 pending",
                    contest_id,
                    p["id"],
                )

    def _stage_ranking_from_recovery_snapshot(
        self, contest_id: int, stage_idx: int
    ) -> list[dict[str, Any]] | None:
        """Validate and restore one exact stage ranking without Match replay.

        A partial snapshot must never become an official table.  Participant
        identity is derived from the durable pairing topology (or the only
        active entrant in the legitimate zero-pairing case), while the exact
        global rank and tie-break values come from the pre-terminal snapshot.
        """
        contest = self.store.get_contest(contest_id)
        if not contest:
            return None
        stages = _parse_stages(contest)
        if contest_current_stage_index(contest, stage_count=len(stages)) is None:
            return None
        stage_idx = exact_nonnegative_int(stage_idx)
        if stage_idx is None or not 0 <= stage_idx < len(stages):
            return None
        stage = stages[stage_idx]
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            return None

        entry_rows = self.store.list_contest_entries(contest_id)
        active_entries = active_contest_entries(entry_rows)
        if active_entries is None:
            return None
        entries = {int(entry["id"]): entry for entry in entry_rows}
        pairings = self.store.list_contest_pairings(
            contest_id, stage_idx=stage_idx
        )
        expected: set[int] = set()
        for pairing in pairings:
            for key in ("entry_a_id", "entry_b_id"):
                value = pairing.get(key)
                if value is None:
                    continue
                if isinstance(value, bool) or not isinstance(value, int):
                    return None
                expected.add(value)
        if not expected:
            active = {int(entry["id"]) for entry in active_entries}
            if len(active) > 1:
                return None
            expected = active

        snapshots = self.store.list_stage_result_recovery_snapshots(
            contest_id, stage_idx=stage_idx
        )
        if len(snapshots) != len(expected):
            return None
        grouped = str(stage.get("type") or "").startswith("group_")
        restored: list[dict[str, Any]] = []
        seen_entries: set[int] = set()
        seen_ranks: set[int] = set()
        for snapshot in snapshots:
            entry_id = snapshot.get("entry_id")
            if (
                isinstance(entry_id, bool)
                or not isinstance(entry_id, int)
                or entry_id not in expected
                or entry_id not in entries
                or entry_id in seen_entries
            ):
                return None
            # Non-group snapshots persist the exact stage-global selection as
            # rank_in_group.  A grouped snapshot only records per-group ranks,
            # so it fails closed instead of inventing an inter-group order.
            rank = None if grouped else snapshot.get("rank_in_group")
            if (
                isinstance(rank, bool)
                or not isinstance(rank, int)
                or rank < 1
                or rank in seen_ranks
                or not isinstance(snapshot.get("tiebreaks"), dict)
            ):
                return None
            seen_entries.add(entry_id)
            seen_ranks.add(rank)
            entry = entries[entry_id]
            restored.append(
                {
                    "entry_id": entry_id,
                    "bot_id": snapshot.get("bot_id"),
                    "user_id": entry.get("user_id"),
                    "rank": rank,
                    "points": snapshot.get("points") or 0,
                    "wins": snapshot.get("wins") or 0,
                    "draws": snapshot.get("draws") or 0,
                    "losses": snapshot.get("losses") or 0,
                    "delta_total": snapshot.get("delta_total") or 0,
                    "group_id": snapshot.get("group_id") or "",
                    "tiebreaks": snapshot["tiebreaks"],
                }
            )
        if seen_entries != expected or seen_ranks != set(
            range(1, len(expected) + 1)
        ):
            return None
        restored.sort(key=lambda row: row["rank"])
        return restored

    def _finalize_official_results_from_stage_snapshots(
        self, contest_id: int, stage_idx: int
    ) -> bool:
        """Publish a complete pre-terminal snapshot after an interrupted commit."""
        from bzplat.backend.contests import ranking as _ranking

        contest = self.store.get_contest(contest_id)
        if not contest:
            return False
        stages = _parse_stages(contest)
        if not 0 <= stage_idx < len(stages):
            return False
        current = self._stage_ranking_from_recovery_snapshot(
            contest_id, stage_idx
        )
        if current is None:
            return False
        stage = stages[stage_idx]
        ranking_rows = current
        if stage.get("ranking_mode") == "replace_top" and stage_idx > 0:
            previous = self._stage_ranking_from_recovery_snapshot(
                contest_id, stage_idx - 1
            )
            scope = stage.get("ranking_scope", 8)
            if (
                previous is None
                or isinstance(scope, bool)
                or not isinstance(scope, int)
            ):
                return False
            ranking_rows = _ranking.merge_replace_top(
                previous, current, scope=scope
            )
        _ranking.persist_official_results(
            self.store, contest_id, ranking_rows, stage_idx=stage_idx
        )
        return True

    def _finalize_official_results(self, contest_id: int, stage_idx: int) -> None:
        """计算全员正式名次（破同分）并落库 contest_official_results。

        若末阶段 stage.ranking_mode=replace_top：合成榜（1..scope 取末阶段 Top，
        scope+1..N 取前一阶段未晋级者相对序）。
        """
        from bzplat.backend.contests import ranking as _ranking
        c = self.store.get_contest(contest_id)
        if not c:
            return
        stages = _parse_stages(c)
        cur_stage = stages[stage_idx] if 0 <= stage_idx < len(stages) else None
        game_id = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(cur_stage, game_id=game_id):
            raise ValueError("阶段计分契约无效，拒绝固化正式排名")
        assert cur_stage is not None

        def _rank_stage(sidx: int) -> list[dict]:
            return self._rank_stage_rows(contest_id, sidx)

        ranking_rows = _rank_stage(stage_idx)
        # replace_top 合成榜（决赛：末阶段 Top8 + 前一阶段未晋级者）
        if cur_stage.get("ranking_mode") == "replace_top" and stage_idx > 0:
            scope = cur_stage.get("ranking_scope", 8)
            stage1_ranking = _rank_stage(stage_idx - 1)
            ranking_rows = _ranking.merge_replace_top(
                stage1_ranking, ranking_rows, scope=scope
            )
        _ranking.persist_official_results(
            self.store, contest_id, ranking_rows, stage_idx=stage_idx
        )

    async def _maybe_next_swiss_round(
        self, contest_id: int, stage_idx: int, stage: dict
    ) -> bool:
        """瑞士轮当前轮完成后生成下一轮。返回是否生成了新一轮（True=阶段未完成）。"""
        contest = self.store.get_contest(contest_id)
        if not contest:
            return False
        stages = _parse_stages(contest)
        current_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            return False
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            return False
        game_spec = game_registry.get(game_id)
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        if not pairings:
            return False
        round_numbers: list[int] = []
        for pairing in pairings:
            raw_round = pairing.get("round_num")
            if (
                isinstance(raw_round, bool)
                or not isinstance(raw_round, int)
                or raw_round < 1
            ):
                return False
            round_numbers.append(raw_round)
        max_round = max(round_numbers)
        entries = self.store.list_contest_entries(contest_id)
        active_entries = active_contest_entries(entries)
        if active_entries is None:
            return False
        expected_entry_bots = {
            int(entry["id"]): entry.get("bot_id") for entry in entries
        }
        expected_entry_users = {
            int(entry["id"]): int(entry["user_id"]) for entry in entries
        }
        require_current_entry_bots = contest.get("status") in (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
        )

        def _is_adjudicated(pairing: dict[str, Any]) -> bool:
            """A Swiss history row is either a strict bye or a completed match."""
            if is_authoritative_no_opponent_pairing(
                stage.get("type"), pairing
            ):
                return contest_pairing_roster_binding_is_valid(
                    pairing,
                    expected_contest_id=contest_id,
                    expected_entry_bots=expected_entry_bots,
                    expected_entry_users=expected_entry_users,
                    require_current_entry_bots=require_current_entry_bots,
                    require_opponent=False,
                )
            match_id = pairing.get("match_id")
            if not match_id:
                return False
            match = self.store.get_match(match_id)
            return match_scoring_result_is_valid(
                stage,
                match,
                game_spec=game_spec,
                pairing=pairing,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )

        expected_entry_ids = [
            int(entry["id"])
            for entry in active_entries
        ]
        if "games_per_pair" in stage:
            real_pairings = [
                pairing
                for pairing in pairings
                if not is_authoritative_no_opponent_pairing(
                    stage.get("type"), pairing
                )
            ]
            if not series_rows_settled(
                stage,
                real_pairings,
                self.store.get_match,
                game_spec=game_spec,
                all_pairings=pairings,
                expected_entry_ids=expected_entry_ids,
                expected_swiss_rounds=max_round,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            ):
                return False

        # Every earlier round is part of the Swiss score/opponent/seat history.
        # A later round must never be generated while any row is unadjudicated;
        # otherwise drift in R1 could be skipped merely because R2 completed.
        if not all(_is_adjudicated(pairing) for pairing in pairings):
            return False
        total_rounds = effective_swiss_rounds(stage, len(expected_entry_ids))
        if max_round >= total_rounds:
            return False
        # 生成下一轮（P0：standings 键 entry_id；P1：bot_id 取 entry 当前值——
        # dispatch 换 Bot 后下一轮用新 Bot，已发布轮冻结不受影响）
        standings = self.standings(contest_id, stage_idx=stage_idx)
        standings_entry_ids = {
            row.get("entry_id")
            for row in standings
            if isinstance(row.get("entry_id"), int)
            and not isinstance(row.get("entry_id"), bool)
        }
        if standings_entry_ids != set(expected_entry_ids):
            # The shared ranking path returns no rows when cumulative normalized
            # deltas or another frozen scoring invariant are malformed.  Never
            # turn that fail-closed signal into an empty next-round batch.
            return False
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
            if s.get("eliminated") == 0
            and entry_to_bot.get(s["entry_id"]) is not None
        ]
        played: set[tuple[int, int]] = set()
        bye_counts_by_entry: Counter[int] = Counter()
        color_counts_by_entry: Counter[int] = Counter()
        for p in pairings:
            entry_a = p.get("entry_a_id")
            entry_b = p.get("entry_b_id")
            if is_authoritative_no_opponent_pairing(stage.get("type"), p):
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
        series_stage = "games_per_pair" in stage
        prior_row_count = len(pairings)
        for ordinal, sp in enumerate(specs, start=1):
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
                        "series_index": sp.series_index,
                        "series_size": sp.series_size,
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
                    "series_index": sp.series_index,
                    "series_size": sp.series_size,
                    "pairing_seed": (
                        self._private_pairing_seed(
                            contest_id, stage_idx, prior_row_count + ordinal
                        )
                        if series_stage
                        else None
                    ),
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
    ) -> EliminationAdvanceState:
        """Resolve one elimination round, appending swapped tiebreaks as needed."""
        contest = self.store.get_contest(contest_id)
        if not contest:
            return "blocked"
        stages = _parse_stages(contest)
        current_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        stage_idx = exact_nonnegative_int(stage_idx)
        if current_stage_idx is None or stage_idx != current_stage_idx:
            return "blocked"
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        if not stage_scoring_contract_is_valid(stage, game_id=game_id):
            return "blocked"
        game_spec = game_registry.get(game_id)
        pairings = self.store.list_contest_pairings(contest_id, stage_idx=stage_idx)
        if not pairings:
            return "blocked"
        entries = self.store.list_contest_entries(contest_id)
        active_entries = active_contest_entries(entries)
        if active_entries is None:
            return "blocked"
        expected_entry_bots = {
            int(entry["id"]): entry.get("bot_id") for entry in entries
        }
        expected_entry_users = {
            int(entry["id"]): int(entry["user_id"]) for entry in entries
        }
        require_current_entry_bots = contest.get("status") in (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
        )
        round_numbers = [exact_nonnegative_int(p.get("round_num")) for p in pairings]
        if any(round_num is None or round_num < 1 for round_num in round_numbers):
            return "blocked"
        max_round = max(round_num for round_num in round_numbers if round_num is not None)
        cur = [
            p
            for p, round_num in zip(pairings, round_numbers)
            if round_num == max_round
        ]
        by_slot: dict[int, list[dict[str, Any]]] = {}
        for pairing in cur:
            slot = pairing.get("bracket_slot")
            if (
                isinstance(slot, bool)
                or not isinstance(slot, int)
                or slot < 0
            ):
                return "blocked"
            by_slot.setdefault(slot, []).append(pairing)

        winners: list[tuple[int, int]] = []  # (bot_id, entry_id)
        appended_tiebreak = False
        for slot in sorted(by_slot):
            slot_rows = by_slot[slot]
            byes = [
                row
                for row in slot_rows
                if is_authoritative_no_opponent_pairing(stage.get("type"), row)
            ]
            if byes:
                if len(slot_rows) != 1 or len(byes) != 1:
                    return "blocked"
                p = byes[0]
                if not contest_pairing_roster_binding_is_valid(
                    p,
                    expected_contest_id=contest_id,
                    expected_entry_bots=expected_entry_bots,
                    expected_entry_users=expected_entry_users,
                    require_current_entry_bots=require_current_entry_bots,
                    require_opponent=False,
                ):
                    return "blocked"
                if (
                    isinstance(p.get("bot_a_id"), bool)
                    or not isinstance(p.get("bot_a_id"), int)
                    or isinstance(p.get("entry_a_id"), bool)
                    or not isinstance(p.get("entry_a_id"), int)
                ):
                    return "blocked"
                winners.append((int(p["bot_a_id"]), int(p["entry_a_id"])))
                continue

            summary = summarize_elimination_encounter(
                stage,
                slot_rows,
                self.store.get_match,
                game_spec=game_spec,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=require_current_entry_bots,
            )
            if summary["state"] == "decided":
                winner_entry = summary.get("winner_entry")
                if (
                    isinstance(winner_entry, bool)
                    or not isinstance(winner_entry, int)
                    or winner_entry not in expected_entry_bots
                    or not isinstance(expected_entry_bots[winner_entry], int)
                ):
                    return "blocked"
                winners.append(
                    (int(expected_entry_bots[winner_entry]), winner_entry)
                )
                continue
            if summary["state"] == "append_tiebreak":
                next_group = summary.get("next_tiebreak_group")
                if (
                    isinstance(next_group, bool)
                    or not isinstance(next_group, int)
                    or next_group < 1
                ):
                    return "blocked"
                primary = next(
                    (
                        row
                        for row in slot_rows
                        if row.get("tiebreak_group", 0) == 0
                        and row.get("tiebreak_game", 0) == 0
                    ),
                    None,
                )
                if primary is None:
                    return "blocked"
                published_at = _now()
                bot_a = int(primary["bot_a_id"])
                bot_b = int(primary["bot_b_id"])
                entry_a = int(primary["entry_a_id"])
                entry_b = int(primary["entry_b_id"])
                # A tiebreak is part of the already-published encounter.  Its
                # programs must therefore be the primary pairing's frozen
                # versions, even if an entrant activates a newer Bot version
                # between the draw and this callback.
                first_versions = {
                    "bot_a_version_id": primary.get("bot_a_version_id"),
                    "bot_b_version_id": primary.get("bot_b_version_id"),
                }
                second_versions = {
                    "bot_a_version_id": primary.get("bot_b_version_id"),
                    "bot_b_version_id": primary.get("bot_a_version_id"),
                }
                rows = [
                    {
                        "bot_a_id": bot_a,
                        "bot_b_id": bot_b,
                        "entry_a_id": entry_a,
                        "entry_b_id": entry_b,
                        "round_num": max_round,
                        "status": STATUS_PENDING,
                        "stage_key": stage.get("key") or f"stage{stage_idx}",
                        "bracket_slot": slot,
                        "color_first": 0,
                        "series_index": 1,
                        "series_size": 1,
                        "published_at": published_at,
                        "scheduled_at": published_at,
                        "tiebreak_group": next_group,
                        "tiebreak_game": 1,
                        **first_versions,
                    },
                    {
                        "bot_a_id": bot_b,
                        "bot_b_id": bot_a,
                        "entry_a_id": entry_b,
                        "entry_b_id": entry_a,
                        "round_num": max_round,
                        "status": STATUS_PENDING,
                        "stage_key": stage.get("key") or f"stage{stage_idx}",
                        "bracket_slot": slot,
                        "color_first": 0,
                        "series_index": 1,
                        "series_size": 1,
                        "published_at": published_at,
                        "scheduled_at": published_at,
                        "tiebreak_group": next_group,
                        "tiebreak_game": 2,
                        **second_versions,
                    },
                ]
                self.store.append_contest_elimination_tiebreak_pairings(
                    contest_id,
                    stage_idx,
                    max_round,
                    slot,
                    rows,
                    expected_current_stage_idx=stage_idx,
                    expected_previous_tiebreak_group=next_group - 1,
                )
                appended_tiebreak = True
                continue

            if summary["state"] in {
                "awaiting_results",
                "legacy_draw",
                "invalid",
            }:
                logger.error(
                    "elimination encounter cannot advance: contest=%s stage=%s "
                    "round=%s slot=%s state=%s",
                    contest_id,
                    stage_idx,
                    max_round,
                    slot,
                    summary["state"],
                )
                return "blocked"

        if appended_tiebreak:
            await self._dispatch_pending_locked(contest_id, stage_idx)
            return "created"
        # 胜者 ≤1 → 已决出冠军，阶段真正完成
        if len(winners) <= 1:
            return "champion"
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
                        "tiebreak_group": 0,
                        "tiebreak_game": 0,
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
                        "tiebreak_group": 0,
                        "tiebreak_game": 0,
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
        return "created"

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
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                return await self._resume_locked(contest_id)

    async def _resume_locked(self, contest_id: int) -> dict:
        """resume 的实际逻辑（调用方已持 per-contest 锁）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] != CONTEST_REST:
            raise ValueError("当前不在休息期")
        # Resuming creates the next stage and moves the contest back to
        # running.  Gate before either write so deployment cannot leave a
        # misleading running stage with no admissible execution.
        self._require_execution_admission()
        stages = self._validated_active_lifecycle_stages(c, _parse_stages(c))
        stage_idx = contest_current_stage_index(c, stage_count=len(stages))
        if stage_idx is None:
            raise ValueError("赛事当前阶段游标损坏，拒绝恢复")
        if stage_idx + 1 >= len(stages):
            return self._finish_adjudicated_contest_locked(
                contest_id,
                stage_idx,
                context="resume-terminal",
            ) or self.store.get_contest(contest_id)
        self._advance_participants(contest_id, stage_idx)
        await self._begin_stage(contest_id, stage_idx + 1)
        return self.store.get_contest(contest_id)

    async def advance(self, contest_id: int) -> dict:
        """组织者强制推进（跳过未完成检查时仅在阶段已完成时可用）。"""
        async with self.deployment_activity_lock:
            async with self._lock(contest_id):
                c = self.store.get_contest(contest_id)
                if not c:
                    raise ValueError("比赛不存在")
                require_mutable(c)
                self._require_execution_admission()
                if c["status"] == CONTEST_REST:
                    return await self._resume_locked(contest_id)
                stages = _parse_stages(c)
                stage_idx = contest_current_stage_index(
                    c, stage_count=len(stages)
                )
                if stage_idx is None:
                    raise ValueError("赛事当前阶段游标损坏，拒绝推进")
                if not self._stage_done(contest_id, stage_idx):
                    raise ValueError("当前阶段对阵尚未全部完成")
                return (
                    await self._maybe_finish_locked(contest_id)
                ) or self.store.get_contest(contest_id)

    async def finish(self, contest_id: int) -> dict:
        """组织者/admin 强制结束赛事（running/rest → finished）。

        用于所有已派发对局都进入终态、但自动阶段推进卡住时的手动出口。
        当前 runner 没有 contest-aware abort，因此仍有 pending/running 对局时明确拒绝，
        避免先写 finished 后后台任务继续晚写结果。
        """
        async with self._lock(contest_id):
            return self._finish_locked(contest_id)

    def _finish_adjudicated_contest_locked(
        self,
        contest_id: int,
        stage_idx: int,
        *,
        gate_stage_idx: int | None = None,
        context: str,
        raise_on_unfinished: bool = False,
    ) -> dict | None:
        """Publish one terminal status and official table behind the shared gate.

        ``gate_stage_idx`` is normally the persisted current stage.  Stage
        creation passes its intended target so an entirely missing next-stage
        batch cannot be hidden by the previous stage's complete graph.
        """
        if self._has_unfinished_pairings(
            contest_id,
            through_stage_idx=gate_stage_idx,
        ):
            message = (
                "赛事仍有未完成对阵，无法强制结束；"
                "请等待对局完成或先安全中止对局"
            )
            if raise_on_unfinished:
                raise ValueError(message)
            logger.error(
                "skip %s finalization for unadjudicated contest=%s",
                context,
                contest_id,
            )
            return None
        self.store.update_contest(
            contest_id, status=CONTEST_FINISHED, ends_at=_now(), rest_ends_at=None
        )
        try:
            self._finalize_official_results(contest_id, stage_idx)
        except Exception:
            logger.exception(
                "%s official results failed contest=%s", context, contest_id
            )
        return self.store.get_contest(contest_id)

    def _finish_locked(self, contest_id: int) -> dict:
        """finish 的实际逻辑（调用方已持 per-contest 锁并在此重读状态）。"""
        c = self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        require_mutable(c)
        if c["status"] not in (CONTEST_RUNNING, CONTEST_REST):
            raise ValueError("仅运行中/休息中的赛事可强制结束")
        stages = _parse_stages(c)
        stage_idx = contest_current_stage_index(c, stage_count=len(stages))
        if stage_idx is None:
            raise ValueError("赛事当前阶段游标损坏，拒绝结束")
        result = self._finish_adjudicated_contest_locked(
            contest_id,
            stage_idx,
            context="force-finish",
            raise_on_unfinished=True,
        )
        assert result is not None  # raise_on_unfinished guarantees a result.
        return result

    def _has_unfinished_pairings(
        self,
        contest_id: int,
        *,
        through_stage_idx: int | None = None,
    ) -> bool:
        """全赛事终局裁决门禁；调用方须持赛事锁。

        自动终局、finished 恢复和强制结束都只能通过这一门禁。它检查所有已到达
        阶段，而非只看当前阶段：每行必须是权威轮空，或绑定一场真实 completed
        Match。持久化的后续空阶段一律 fail closed，防止按空 standings 固化/反转
        名次。仅初始 0/1 人阶段，或 ``_begin_stage`` 明确尝试紧邻下一阶段且只剩
        一名 active 参赛者时，空图才是合法终局。

        当前 orchestrator 没有能等待 runner 收敛的 contest-aware abort。与其先写
        finished 后让后台任务晚写结果，保守拒绝任何未绑定、缺失或仍活跃的对阵；
        同时检查未被 pairing 正确绑定的赛事活跃 Match。
        """
        if self.store.contest_has_active_matches(contest_id):
            return True
        contest = self.store.get_contest(contest_id)
        if not contest:
            return True
        game_id = _stored_game_id(contest, entity=f"赛事 #{contest_id}")
        game_spec = game_registry.get(game_id)
        stages = _parse_stages(contest or {})
        persisted_stage_idx = contest_current_stage_index(
            contest, stage_count=len(stages)
        )
        if persisted_stage_idx is None:
            return True
        explicit_next_stage = through_stage_idx is not None
        if through_stage_idx is None:
            current_stage_idx = persisted_stage_idx
        elif (
            isinstance(through_stage_idx, bool)
            or not isinstance(through_stage_idx, int)
        ):
            return True
        else:
            current_stage_idx = through_stage_idx
        if current_stage_idx < 0 or current_stage_idx >= len(stages):
            return True

        entries = self.store.list_contest_entries(contest_id)
        active_entries = active_contest_entries(entries)
        if active_entries is None:
            return True
        expected_entry_bots = {
            int(entry["id"]): entry.get("bot_id") for entry in entries
        }
        expected_entry_users = {
            int(entry["id"]): int(entry["user_id"]) for entry in entries
        }
        pairings_by_stage: dict[int, list[dict[str, Any]]] = {
            stage_idx: [] for stage_idx in range(current_stage_idx + 1)
        }
        for pairing in self.store.list_contest_pairings(contest_id):
            stage_idx = exact_nonnegative_int(pairing.get("stage_idx"))
            # Future/unknown-stage rows are lifecycle drift, not evidence that
            # the reached stage graph is complete.
            if (
                stage_idx is None
                or stage_idx > current_stage_idx
                or stage_idx >= len(stages)
            ):
                return True
            pairings_by_stage[stage_idx].append(pairing)
            stage_type = (
                stages[stage_idx].get("type")
                if 0 <= stage_idx < len(stages)
                else None
            )
            if is_authoritative_no_opponent_pairing(stage_type, pairing):
                if not contest_pairing_roster_binding_is_valid(
                    pairing,
                    expected_contest_id=contest_id,
                    expected_entry_bots=expected_entry_bots,
                    expected_entry_users=expected_entry_users,
                    require_current_entry_bots=bool(
                        stage_idx >= persisted_stage_idx
                        and contest.get("status") in (
                            CONTEST_PUBLISHED,
                            CONTEST_RUNNING,
                        )
                    ),
                    require_opponent=False,
                ):
                    return True
                continue
            match_id = pairing.get("match_id")
            if not match_id:
                return True
            match = self.store.get_match(match_id)
            if not match_scoring_result_is_valid(
                stages[stage_idx],
                match,
                game_spec=game_spec,
                pairing=pairing,
                expected_contest_id=contest_id,
                expected_entry_bots=expected_entry_bots,
                expected_entry_users=expected_entry_users,
                require_current_entry_bots=bool(
                    stage_idx >= persisted_stage_idx
                    and contest.get("status") in (
                        CONTEST_PUBLISHED,
                        CONTEST_RUNNING,
                    )
                ),
            ):
                return True

        active_entry_count = len(active_entries)
        active_entry_ids = {int(entry["id"]) for entry in active_entries}
        frozen_stage_participants: dict[int, set[int]] = {}
        for result in self.store.list_stage_results(contest_id):
            result_stage_idx = result.get("stage_idx")
            entry_id = result.get("entry_id")
            if (
                isinstance(result_stage_idx, int)
                and not isinstance(result_stage_idx, bool)
                and isinstance(entry_id, int)
                and not isinstance(entry_id, bool)
            ):
                frozen_stage_participants.setdefault(
                    result_stage_idx, set()
                ).add(entry_id)
        for stage_idx, stage_pairings in pairings_by_stage.items():
            if stage_pairings:
                stage = stages[stage_idx]
                if not stage_scoring_contract_is_valid(stage, game_id=game_id):
                    return True
                if "games_per_pair" in stage:
                    real_pairings = [
                        pairing
                        for pairing in stage_pairings
                        if not is_authoritative_no_opponent_pairing(
                            stage.get("type"), pairing
                        )
                    ]
                    if not series_rows_settled(
                        stage,
                        real_pairings,
                        self.store.get_match,
                        game_spec=game_spec,
                        all_pairings=stage_pairings,
                        expected_entry_ids=(
                            active_entry_ids
                            if stage_idx >= persisted_stage_idx
                            else frozen_stage_participants.get(
                                stage_idx, active_entry_ids
                            )
                        ),
                        expected_swiss_rounds=(
                            effective_swiss_rounds(
                                stage,
                                len(
                                    active_entry_ids
                                    if stage_idx >= persisted_stage_idx
                                    else frozen_stage_participants.get(
                                        stage_idx, active_entry_ids
                                    )
                                ),
                            )
                            if stage.get("type") == "swiss"
                            else None
                        ),
                        expected_contest_id=contest_id,
                        expected_entry_bots=expected_entry_bots,
                        expected_entry_users=expected_entry_users,
                        require_current_entry_bots=bool(
                            stage_idx >= persisted_stage_idx
                            and contest.get("status") in (
                                CONTEST_PUBLISHED,
                                CONTEST_RUNNING,
                            )
                        ),
                    ):
                        return True
                continue
            initial_zero_or_one = (
                stage_idx == current_stage_idx == 0 and len(entries) <= 1
            )
            generated_next_stage_champion = (
                explicit_next_stage
                and stage_idx == current_stage_idx == persisted_stage_idx + 1
                and active_entry_count <= 1
            )
            if initial_zero_or_one or generated_next_stage_champion:
                continue
            return True
        return False

    def estimate(
        self,
        contest_id: int,
        *,
        contest: dict[str, Any] | None = None,
        entries: list[dict[str, Any]] | None = None,
        pairings: list[dict[str, Any]] | None = None,
    ) -> dict:
        """Estimate from one optional frozen read snapshot.

        The public detail endpoint injects contest/roster/pairings from a single
        Store transaction. Other callers retain the historical Store-backed
        behavior without duplicating the estimation formula.
        """
        c = contest if contest is not None else self.store.get_contest(contest_id)
        if not c:
            raise ValueError("比赛不存在")
        entry_rows = (
            entries
            if entries is not None
            else self.store.list_contest_entries(contest_id)
        )
        if active_contest_entries(entry_rows) is None:
            raise ValueError("参赛者淘汰状态损坏，无法估算赛事")
        pairing_rows = (
            pairings
            if pairings is not None
            else self.store.list_contest_pairings(contest_id)
        )
        n = len(entry_rows)
        stages = _parse_stages(c)
        # 旧 draft/open 内建模板可以尚未持久化当前系列默认值。发布/启动会在
        # 冻结边界注入这些值，因此预估也必须基于同一份内存投影，避免 API
        # 先低报 K/轮数、实际发布后突然膨胀。这里只读计算，不静默改写快照。
        if (
            c.get("status") in (CONTEST_DRAFT, CONTEST_OPEN)
            and not pairing_rows
        ):
            stages = self._configured_unstarted_series_stages(c, stages)
        if contest_current_stage_index(c, stage_count=len(stages)) is None:
            raise ValueError("赛事当前阶段游标损坏，无法估算赛事")
        gid = _stored_game_id(c, entity=f"赛事 #{contest_id}")
        spec = game_registry.get(gid)
        # estimate 按晋级契约传播各 stage 人数。与 _advance_participants 一致，
        # 分组 Top-N 优先于全局 advance_count；两者都不能放大当前人数。
        total = 0
        execution_legs = 0
        cur_n = n
        conc = max(
            1,
            int(getattr(self.orch, "max_concurrent", MAX_CONCURRENT_MATCHES)),
        )
        sec_per = _estimate_sec_per_match(gid, {})
        stage_estimates: list[dict[str, Any]] = []
        for st in stages:
            if not stage_scoring_contract_is_valid(st, game_id=gid):
                raise ValueError("阶段计分版本配置无效")
            stage_matches = estimate_match_count(st, cur_n)
            total += stage_matches
            leg_count = 1
            duplicate = stage_duplicate_mode(st)
            if duplicate:
                if spec.build_match_plan is None:
                    raise ValueError(f"游戏 {gid} 不支持 duplicate 赛制")
                match_plan = spec.build_match_plan(0, {"duplicate": True})
                if not match_plan:
                    raise ValueError(f"游戏 {gid} 的 duplicate 对局计划为空")
                leg_count = len(match_plan)
            stage_execution_legs = stage_matches * leg_count
            execution_legs += stage_execution_legs
            stage_type = str(st.get("type") or "round_robin")
            games_per_pair = int(
                st.get("games_per_pair")
                or (2 if "double_round_robin" in stage_type else 1)
            )
            conceptual_pairings = (
                stage_matches // max(1, games_per_pair)
            )
            stage_estimates.append(
                {
                    "stage_key": st.get("key") or f"stage{len(stage_estimates) + 1}",
                    "participant_count": cur_n,
                    "conceptual_pairings": conceptual_pairings,
                    "effective_rounds": (
                        effective_swiss_rounds(st, cur_n)
                        if stage_type == "swiss"
                        else None
                    ),
                    "games_per_pair": games_per_pair,
                    "estimated_matches": stage_matches,
                    "estimated_execution_legs": stage_execution_legs,
                    "eta_seconds": int((stage_execution_legs / conc) * sec_per),
                    "unbounded_tiebreak": bool(
                        stage_type == "single_elimination"
                        and st.get("tiebreak")
                        == ELIMINATION_TIEBREAK_PAIRED_SWAP
                    ),
                }
            )
            advance_per_group = st.get("advance_per_group")
            if advance_per_group and int(advance_per_group) > 0:
                group_count = effective_group_count(
                    cur_n,
                    int(st.get("group_count") or 4),
                )
                cur_n = min(
                    cur_n,
                    group_count * int(advance_per_group),
                )
                continue
            ac = st.get("advance_count")
            if ac and int(ac) > 0:
                cur_n = min(cur_n, int(ac))
        # Production uses MatchOrchestrator.max_concurrent.  Lightweight
        # read-only estimators/test doubles need no execution interface, so
        # fall back to the same immutable code policy instead of a DB setting.
        eta_sec = (execution_legs / conc) * sec_per if conc else 0
        return {
            "entries": n,
            "estimated_matches": total,
            "estimated_scoring_games": execution_legs,
            "max_concurrent": conc,
            "eta_seconds": int(eta_sec),
            "stages": stage_estimates,
            "unbounded_tiebreak": any(
                bool(stage.get("unbounded_tiebreak"))
                for stage in stage_estimates
            ),
        }
