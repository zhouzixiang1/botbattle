"""对局编排：challenge 入队、评分更新、SSE 扇出。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import stat as stat_module
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from bzplat.backend.games import registry as game_registry
from bzplat.backend.games import normalize_game_id
from bzplat.backend.matches.runner import MatchRunner, _fail_response
from bzplat.backend.matches.result_contract import (
    build_engine_result_payload,
    build_result_payload,
    build_technical_result_payload,
)
from bzplat.backend.rating.glicko2 import Rating, match_scores, update_rating
from bzplat.backend.runtime.config import (
    HUMAN_ACTION_TIMEOUT_SEC,
    HUMAN_MAX_CONCURRENT_MATCHES,
    HUMAN_MAX_CONSECUTIVE_TIMEOUTS,
    MAX_CONCURRENT_MATCHES,
)
from bzplat.backend.runtime.binary_runner import (
    BinaryRunner,
    BotCrashedError,
    BotTechnicalError,
    PlatformRunnerError,
)
from bzplat.backend.store import Store
from bzplat.backend.store.public_contract import (
    canonical_public_completed_reason,
    canonical_public_error_reason,
    sanitize_public_event,
    sanitize_public_event_prefix,
    sanitize_public_match,
)
from bzplat.backend.store.schema import (
    BOT_CAPACITY_EXHAUSTED_REASON,
    DEFAULT_RUNTIME_MODE,
    REGISTERED_ENGINES,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TECHNICAL_INCIDENT_EVENT,
    TYPE_CHALLENGE,
    TYPE_CONTEST,
    TYPE_HUMAN,
    TYPE_TABLE,
    VALID_GAME_IDS,
    VALID_RUNTIME_MODES,
    require_supported_binary_metadata,
)

logger = logging.getLogger(__name__)


_BinaryIntegrityCacheKey = tuple[str, str, int, int, int, int, int, int]
VERSION_UNAVAILABLE_REASON = "version_unavailable"


def require_binary_file_integrity(
    runtime: dict,
    path: str,
    *,
    cache: set[_BinaryIntegrityCacheKey] | None = None,
) -> None:
    """Validate persisted size/SHA metadata without exposing ``path`` in errors.

    Empty checksum plus zero size identifies a pre-integrity historical row: its
    digest is unavailable, but the referenced file must still exist and be a
    regular file.  Any supplied integrity field is authoritative.  Cache identity
    includes device, inode, size, mtime and ctime, so replacement or in-place
    modification cannot reuse an earlier digest merely by restoring the old mtime.
    """

    expected_checksum = str(runtime.get("checksum") or "").strip().lower()
    expected_size = int(runtime.get("size_bytes") or 0)
    if expected_size < 0:
        raise ValueError(VERSION_UNAVAILABLE_REASON)

    binary_path = Path(path)
    stat_before = binary_path.stat()
    if not stat_module.S_ISREG(stat_before.st_mode):
        raise ValueError(VERSION_UNAVAILABLE_REASON)
    if not expected_checksum and expected_size == 0:
        return
    signature = (
        int(stat_before.st_dev),
        int(stat_before.st_ino),
        int(stat_before.st_size),
        int(stat_before.st_mtime_ns),
        int(stat_before.st_ctime_ns),
    )
    if expected_size > 0 and stat_before.st_size != expected_size:
        raise ValueError(VERSION_UNAVAILABLE_REASON)
    if not expected_checksum:
        return

    cache_key: _BinaryIntegrityCacheKey = (
        path,
        expected_checksum,
        expected_size,
        *signature,
    )
    if cache is not None and cache_key in cache:
        return

    digest = hashlib.sha256()
    with binary_path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    stat_after = binary_path.stat()
    signature_after = (
        int(stat_after.st_dev),
        int(stat_after.st_ino),
        int(stat_after.st_size),
        int(stat_after.st_mtime_ns),
        int(stat_after.st_ctime_ns),
    )
    if signature_after != signature or digest.hexdigest().lower() != expected_checksum:
        raise ValueError(VERSION_UNAVAILABLE_REASON)

    if cache is not None:
        if len(cache) >= 1024:
            cache.pop()
        cache.add(cache_key)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _technical_incident_event(exc: BotTechnicalError) -> dict:
    return {"type": TECHNICAL_INCIDENT_EVENT, **exc.incident()}


def _ensure_technical_incident(
    events: list[dict], exc: BotTechnicalError
) -> None:
    """Persist exactly one terminal incident even for injected/fake runners."""
    target = exc.incident()
    if not any(
        event.get("type") == TECHNICAL_INCIDENT_EVENT
        and event.get("reason") == target["reason"]
        and event.get("seat") == target["seat"]
        and event.get("turn") == target["turn"]
        for event in events
    ):
        events.append(_technical_incident_event(exc))


def _technical_incident_summary(events: list[dict]) -> dict:
    """Bound technical diagnostics kept in result JSON (counts + at most 3 samples)."""
    incidents = [
        event
        for event in events
        if event.get("type") == TECHNICAL_INCIDENT_EVENT
    ]
    samples = [
        {
            key: event[key]
            for key in ("reason", "code", "seat", "turn", "leg", "error")
            if key in event
        }
        for event in incidents[:3]
    ]
    by_seat = {0: 0, 1: 0}
    for event in incidents:
        seat = event.get("seat")
        if seat in by_seat:
            by_seat[seat] += 1
    return {
        "technical_incident_count": len(incidents),
        "technical_incidents_by_seat": by_seat,
        "technical_incident_samples": samples,
    }


def _bounded_replay_events(events: list[dict]) -> list[dict]:
    """Keep the complete game replay but at most three technical diagnostics."""
    incident_samples = 0
    bounded: list[dict] = []
    for event in events:
        if event.get("type") == TECHNICAL_INCIDENT_EVENT:
            incident_samples += 1
            if incident_samples > 3:
                continue
        bounded.append(event)
    return bounded


def _live_replay_events(events: list[dict]) -> list[dict]:
    """Return a running-match snapshot without engine terminal markers.

    A game session emits its own ``match_end`` immediately before returning its
    result to the orchestrator.  At that point the match row is still
    ``running``.  Persisting that marker into a live snapshot lets a reconnecting
    WebSocket client mistake the replay for an authoritative terminal state.
    Engine terminal events are retained only as internal inputs.  After the
    match row/result commit, the final replay replaces all of them with the same
    single canonical terminal event used by the live transport.
    """
    return [
        event
        for event in events
        if event.get("type") not in {"match_end", "error"}
    ]


def _authoritative_match_end(
    winner: int | None,
    reason: str,
    deltas: list[int],
) -> dict[str, Any]:
    """Build the one public live completion event emitted by the orchestrator."""
    return {
        "type": "match_end",
        "winner": winner,
        "reason": reason,
        "deltas": [int(deltas[0]), int(deltas[1])],
    }


def _authoritative_error(reason: str) -> dict[str, str]:
    """Build the single public aborted terminal event (diagnostics stay in logs)."""
    return {"type": "error", "reason": canonical_public_error_reason(reason)}


def _completed_match_reason(result: Any, events: list[dict]) -> str:
    """Preserve a game's adjudicated terminal reason without game-name branches.

    Board games expose ``five/draw/illegal/...`` directly on their result. Dropping
    those to the generic ``completed`` makes persisted state and replay disagree
    with the public judge. Hold'em has no match-level normal reason and therefore
    keeps ``completed``; its mid-match crash remains available on the engine event.
    """
    reason = getattr(result, "reason", None)
    canonical_reason = canonical_public_completed_reason(reason)
    if canonical_reason != "completed" or reason == "completed":
        return canonical_reason
    result_events = getattr(result, "events", None)
    sources = result_events if isinstance(result_events, list) else events
    for event in reversed(sources):
        if event.get("type") == "match_end" and event.get("reason") == "crash":
            return "crash"
    return "completed"


class HumanInactive(Exception):
    """人类玩家连续超时不响应（连续 ≥ human_max_consecutive_timeouts 次）。

    由 _run_human_match 的 human_decide 在达到阈值时抛出，向上经 runner（人类侧
    不吞异常）→ holdem 引擎（run_async 的 try 仅捕 BotCrashedError，故透传）→
    回到 _run_human_match 的 except HumanInactive 分支中止对局。
    棋类一手非法即结束，不会累积到此阈值。
    """


class BotVersionUnavailableError(ValueError):
    """A frozen Bot version cannot be resolved to its original runtime.

    This is a platform data-integrity failure, not a Bot game result.  Keep the
    exception message deliberately generic: it may be forwarded to a live
    client and therefore must never contain a private binary path.
    """

    code = VERSION_UNAVAILABLE_REASON

    def __init__(
        self,
        *,
        bot_id: int | None,
        version_id: int | None,
        seat: int | None,
    ) -> None:
        super().__init__(self.code)
        self.bot_id = bot_id
        self.version_id = version_id
        self.seat = seat


class BotCapacityError(ValueError):
    """No global Bot execution slot is available for a new match."""

    code = BOT_CAPACITY_EXHAUSTED_REASON

    def __init__(self) -> None:
        super().__init__("Bot 对局并发已满，请稍后重试")


class MatchOrchestrator:
    def __init__(
        self,
        store: Store,
        *,
        runner: MatchRunner | None = None,
        max_concurrent: int = MAX_CONCURRENT_MATCHES,
    ) -> None:
        self.store = store
        self.runner = runner or MatchRunner(BinaryRunner())
        self.max_concurrent = max_concurrent
        self._sem = asyncio.Semaphore(max_concurrent)
        self._tasks: dict[str, asyncio.Task] = {}
        # Global admission tokens.  A Bot match reserves one token before its
        # DB row/task is created and releases it only after task cleanup (or a
        # prepared-match compensation).  This prevents callers from piling an
        # unbounded pending queue behind ``_sem``.
        self._bot_admitted: set[str] = set()
        # admin abort 正在接管的 match：被取消任务的 finally 只移除 task，不提前
        # 清 SSE/触发回调；abort_match 落稳 aborted 后统一广播与回调一次。
        self._admin_aborting: set[str] = set()
        # abort_match 落稳后到 on_match_done 返回前的短暂 handoff 标记。
        # ContestManager 只对这种显式管理员中止立即重派；平台故障
        # 产生的 aborted 必须留 pending 给 scheduler 有节制地重试。
        self._admin_abort_handoffs: set[str] = set()
        # 实际占用 bot 对局槽（已 acquire _sem）的任务数。区别于 _tasks（含等信号量的）。
        # auto_matcher._is_idle 据此判定空闲，避免大量 pending 任务排队等槽时误判不空闲。
        self._bot_running = 0
        self._sse: dict[str, list[asyncio.Queue]] = {}
        # Per-subscriber visibility for active human poker. Public spectators
        # receive no hole cards/turn request; the authenticated human receives
        # only their own cards and decision request.
        self._sse_human_views: dict[asyncio.Queue, tuple[bool, int | None]] = {}
        # Runner 正在追加的事件列表引用。运行 replay 为崩溃恢复而节流落库，
        # 新订阅必须读取这份完整前缀，不能永久漏掉最近 1–4 条事件。前缀
        # 仍在 subscribe() 按观看者身份经过公开投影与真人底牌脱敏。
        self._active_replay_events: dict[str, list[dict]] = {}
        self._lock = asyncio.Lock()
        # 评分串行化锁：按 (bot_id, game_id) 维度串行化 _apply_ratings，防同 bot 两场
        # 并发完成时快照读+绝对写 rating/rd/vol 互相覆盖（lost-update，审计 FRAGILE 5a）。
        self._rating_locks: dict[tuple[int, str], asyncio.Lock] = {}
        # 全局评分结算顺序锁：completed 业务结果与评分事务之间若失败，
        # 后来对局必须先补齐已冻结 settled_order 更早的缺口，不得越过它
        # 在旧 rating 快照上结算。per-bot 锁仍作为单场双边快照的内层防线。
        self._rating_settlement_order_lock = asyncio.Lock()
        # Immutable upload verification cache.  A cache hit still performs stat;
        # device/inode/size/mtime/ctime changes force a fresh SHA-256 read.
        self._binary_integrity_cache: set[_BinaryIntegrityCacheKey] = set()
        # 对局完成后的回调（由外部注入，如比赛归档）。签名: (match_id, contest_id|None) -> None
        self.on_match_done: "Callable[[str, int | None], None] | None" = None
        # 通知管理器（由 main.py 注入；对局完成时通知双方 owner）
        self.notifier = None
        # ── 人类对战（独立并发，不占 bot 对局槽）──────────────────
        self.human_max_concurrent = HUMAN_MAX_CONCURRENT_MATCHES
        self._human_sem = asyncio.Semaphore(self.human_max_concurrent)
        # (match_id, player_idx) → pending 人类回合 {request, future, ts}
        self._human_turns: dict[tuple[str, int], dict] = {}
        # 每 user 同时进行的人类局 ≤ 1（节流，防挂机占满人类槽）
        self._human_active_users: set[int] = set()
        self.human_action_timeout = HUMAN_ACTION_TIMEOUT_SEC
        # 连续超时阈值：人类连续 N 次不响应则中止对局（避免 70 手最长 2.3h 死磕，
        # 占用人类槽 + 锁死 _human_active_users）。棋类一手非法即结束，仅 holdem 触发。
        self.human_max_consecutive_timeouts = HUMAN_MAX_CONSECUTIVE_TIMEOUTS

    def rebuild_concurrency(self, max_concurrent: int) -> None:
        """热更新并发上限。

        P0-2 修复：不重置 _bot_running=0（在途任务仍会在 finally 里 -1，重置会导致
        计数失真→auto_matcher 误判 idle→超额调度）；只换 Semaphore（新任务用新上限，
        旧任务在旧 sem 上自然排空）。_bot_running 保持真实值，auto_matcher 据此准确判断。
        """
        self.max_concurrent = max(1, int(max_concurrent))
        self._sem = asyncio.Semaphore(self.max_concurrent)

    def available_bot_slots(self) -> int:
        """Return globally reservable Bot slots (human matches are separate)."""
        return max(0, int(self.max_concurrent) - len(self._bot_admitted))

    def _reserve_bot_slot(self, match_id: str) -> None:
        if match_id in self._bot_admitted:
            return
        if self.available_bot_slots() <= 0:
            raise BotCapacityError()
        self._bot_admitted.add(match_id)

    def _release_bot_slot(self, match_id: str) -> None:
        self._bot_admitted.discard(match_id)

    def reserve_prepared_match_slot(
        self, match_id: str, *, keep_free: int = 0
    ) -> None:
        """Reserve admission before an external atomic prepared-match claim.

        Automatic ranking uses this before its Store transaction.  The claim can
        therefore never create a pending DB row and only then discover that a
        foreground request took the last slot. ``keep_free`` makes the foreground
        reserve part of the same synchronous admission operation instead of a
        separate capacity observation. ``start_prepared_match`` is idempotent for
        the already-held token.
        """
        if match_id in self._bot_admitted:
            return
        if self.available_bot_slots() <= max(0, int(keep_free)):
            raise BotCapacityError()
        self._bot_admitted.add(match_id)

    def release_prepared_match_slot(self, match_id: str) -> None:
        """Compensate a reservation when the external Store claim did not win."""
        if match_id not in self._tasks:
            self._release_bot_slot(match_id)

    def rebuild_human_concurrency(self, max_concurrent: int) -> None:
        """热更新人类对局独立并发上限。"""
        self.human_max_concurrent = max(1, int(max_concurrent))
        self._human_sem = asyncio.Semaphore(self.human_max_concurrent)

    def set_action_timeout(self, timeout_sec: float) -> None:
        self.runner.action_timeout = float(timeout_sec)

    def set_human_action_timeout(self, timeout_sec: float) -> None:
        self.human_action_timeout = float(timeout_sec)

    def _release_human_match_state(
        self, match_id: str, human_user_id: int | None
    ) -> None:
        """Release all in-memory ownership for one human match."""
        self._tasks.pop(match_id, None)
        # Admin abort owns a short terminal-event handoff: cancellation reaches
        # this cleanup before ``abort_match`` has committed/broadcast its error.
        # Preserve subscribers during that window so the live human page receives
        # the authoritative terminal event; ``_finish_match_task`` removes them
        # immediately after the broadcast.  All other exits still clean eagerly.
        if match_id not in self._admin_aborting:
            for queue in self._sse.pop(match_id, []):
                self._sse_human_views.pop(queue, None)
            self._active_replay_events.pop(match_id, None)
        self._human_turns = {
            key: value for key, value in self._human_turns.items()
            if key[0] != match_id
        }
        if human_user_id is not None:
            self._human_active_users.discard(int(human_user_id))

    def _snapshot_bot_version(
        self,
        bot_id: int,
        requested_version_id: int | None,
        *,
        seat_label: str,
    ) -> dict | None:
        """Resolve the immutable version reference stored when a match is created.

        An omitted version means the bot's *currently active* version, not an
        instruction for the runner to look at ``bots.binary_path`` later.  Legacy
        bots created before ``bot_versions`` existed legitimately have no row; in
        that case ``None`` preserves the binary-path compatibility fallback.

        Explicit IDs retain the strict ownership validation used by the challenge
        API so a caller cannot execute another bot's private version by ID.
        """
        if requested_version_id is None:
            version = self.store.get_current_bot_version(bot_id)
        else:
            version = self.store.get_bot_version(requested_version_id)
            if not version or version.get("bot_id") != bot_id:
                raise ValueError(f"{seat_label} 指定的版本不存在或不属于该 bot")

        # Resolve through the same fail-closed runtime boundary before creating
        # the match.  This includes canonical metadata plus non-empty persisted
        # checksum/size, so a known-corrupt upload cannot first create an orphan
        # task and only fail after entering the queue.
        bot = self.store.get_bot(bot_id)
        if not bot:
            raise ValueError(f"{seat_label} bot 不存在")
        binary = version or bot
        try:
            require_supported_binary_metadata(
                str(binary.get("format") or ""),
                str(binary.get("os") or ""),
                str(binary.get("arch") or ""),
            )
        except ValueError as exc:
            raise ValueError(f"{seat_label} unsupported_binary：{exc}") from exc
        self._runtime_for_bot_version(
            bot,
            int(version["id"]) if version is not None else None,
        )
        return version

    def _runtime_for_bot_version(
        self,
        bot: dict,
        version_id: int | None,
        *,
        seat: int | None = None,
    ) -> tuple[str, str]:
        """Resolve exactly one immutable runtime or fail closed.

        A non-null ``version_id`` is authoritative: missing rows, cross-Bot
        references and incomplete/non-canonical rows must never fall back to the
        mutable ``bots`` mirror.  ``None`` is accepted only for a genuine
        pre-version Bot (``current_version=0`` and no version rows), and that
        mirror is subjected to the same canonical binary/runtime validation.
        """
        bot_id_raw = bot.get("id")
        bot_id = int(bot_id_raw) if bot_id_raw is not None else None

        if version_id is None:
            has_version_history = (
                bot_id is None
                or int(bot.get("current_version") or 0) != 0
                or self.store.get_latest_bot_version(bot_id) is not None
            )
            if has_version_history:
                raise BotVersionUnavailableError(
                    bot_id=bot_id, version_id=None, seat=seat
                )
            runtime = bot
        else:
            version = self.store.get_bot_version(version_id)
            if not version or version.get("bot_id") != bot_id:
                raise BotVersionUnavailableError(
                    bot_id=bot_id, version_id=version_id, seat=seat
                )
            runtime = version

        path = str(runtime.get("binary_path") or "").strip()
        mode = str(runtime.get("runtime_mode") or DEFAULT_RUNTIME_MODE)
        try:
            require_supported_binary_metadata(
                str(runtime.get("format") or ""),
                str(runtime.get("os") or ""),
                str(runtime.get("arch") or ""),
            )
        except ValueError as exc:
            raise BotVersionUnavailableError(
                bot_id=bot_id, version_id=version_id, seat=seat
            ) from exc
        if not path or mode not in VALID_RUNTIME_MODES:
            raise BotVersionUnavailableError(
                bot_id=bot_id, version_id=version_id, seat=seat
            )
        self._verify_runtime_binary_integrity(
            runtime,
            path=path,
            bot_id=bot_id,
            version_id=version_id,
            seat=seat,
        )
        return path, mode

    def _verify_runtime_binary_integrity(
        self,
        runtime: dict,
        *,
        path: str,
        bot_id: int | None,
        version_id: int | None,
        seat: int | None,
    ) -> None:
        """Verify immutable upload metadata when the historical row provides it.

        Pre-checksum rows have both fields empty/zero and remain runnable.  Newer
        uploads carry both fields; a missing file, size mismatch, checksum mismatch
        or file mutation during hashing is a ``version_unavailable`` integrity
        failure and never reaches the runner.
        """

        try:
            require_binary_file_integrity(
                runtime, path, cache=self._binary_integrity_cache
            )
        except (OSError, TypeError, ValueError) as exc:
            raise BotVersionUnavailableError(
                bot_id=bot_id, version_id=version_id, seat=seat
            ) from exc

    def _abort_version_unavailable(
        self,
        match_id: str,
        exc: BotVersionUnavailableError,
        events: list[dict],
    ) -> None:
        """Persist one non-adjudicated terminal result without exposing paths."""
        logger.error(
            "match version unavailable match_id=%s bot_id=%s version_id=%s seat=%s",
            match_id,
            exc.bot_id,
            exc.version_id,
            exc.seat,
        )
        self.store.abort_match_if_active(
            match_id, reason=BotVersionUnavailableError.code
        )
        terminal_event = _authoritative_error(BotVersionUnavailableError.code)
        self._safe_flush_terminal_replay(match_id, events, terminal_event)
        self._broadcast(match_id, terminal_event)

    async def shutdown(self) -> None:
        """Cancel and await every in-flight match while the event loop is alive.

        Letting ``asyncio.run``/the ASGI server cancel these tasks as part of the
        loop-wide shutdown can race with ``create_subprocess_exec`` while asyncio
        is still connecting the child pipes.  In that window the match task and
        the pipe connector are cancelled together and loop teardown can wait
        forever.  Cancelling only the owned match tasks first gives the transport
        callbacks a live loop in which to finish their cleanup.
        """
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

        self._tasks.clear()
        self._bot_admitted.clear()
        self._admin_aborting.clear()
        self._admin_abort_handoffs.clear()
        self._human_turns.clear()
        self._human_active_users.clear()
        self._sse.clear()
        self._sse_human_views.clear()
        self._active_replay_events.clear()

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
        gid = normalize_game_id(bot.get("game_id") if game_id is None else game_id)
        if gid != normalize_game_id(bot.get("game_id")):
            raise ValueError(f"指定游戏 {gid} 与 Bot 游戏 {bot.get('game_id')} 不一致")
        if gid not in VALID_GAME_IDS or gid not in REGISTERED_ENGINES:
            raise ValueError(f"游戏引擎未注册: {gid}")

        bot_seat = 1 - human_seat
        # None 在创建瞬间解析为当前激活版本并落 match_config；runner 排队期间即使
        # owner 上传新版本或回滚，也继续执行这份快照。无 bot_versions 行的 legacy
        # bot 保持空配置，运行时安全回退 bots.binary_path。
        bot_version = self._snapshot_bot_version(
            bot_id, None, seat_label=f"座位{bot_seat}"
        )
        mc: dict[str, Any] = {}
        if bot_version is not None:
            mc[
                "_bot_a_version_id" if bot_seat == 0 else "_bot_b_version_id"
            ] = int(bot_version["id"])

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
        try:
            self.store.upsert_replay(match_id, "[]")
        except Exception:
            # create_match 与 replay 分属两个 Store 事务。初始化 replay 失败时
            # 调用方不会拿到 match_id，因此必须精确删除 pending 对局及索引，
            # 不能留下一个没有 runner/task 的人类对局孤儿。
            self.store.delete_match(match_id)
            raise
        self._human_active_users.add(human_user_id)
        task = asyncio.create_task(self._run_human_match(match_id), name=f"human-{match_id}")
        self._tasks[match_id] = task

        def release_if_body_never_started(done_task: asyncio.Task) -> None:
            # Cancellation can happen while waiting for _human_sem, or even before
            # the coroutine's first instruction.  In both cases its inner finally
            # is unreachable, so the task completion callback owns cleanup.  If
            # the body did run, its finally already removed this exact task and the
            # identity guard prevents a late callback from clearing a newer match.
            if self._tasks.get(match_id) is done_task:
                self._release_human_match_state(match_id, human_user_id)

        task.add_done_callback(release_if_body_never_started)
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
        bot_a_version_id: int | None = None,
        bot_b_version_id: int | None = None,
        duplicate: bool = False,
        duplicate_seed: int | None = None,
        defer_start: bool = False,
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
        ga = normalize_game_id(bot_a.get("game_id"))
        gb = normalize_game_id(bot_b.get("game_id"))
        if ga != gb:
            raise ValueError(f"双方 Bot 游戏类型不一致：{ga} vs {gb}")
        gid = normalize_game_id(game_id) if game_id is not None else ga
        if gid != ga:
            raise ValueError(f"指定游戏 {gid} 与 Bot 游戏 {ga} 不一致")
        if gid not in VALID_GAME_IDS:
            raise ValueError(f"未知游戏: {gid}")
        if gid not in REGISTERED_ENGINES:
            raise ValueError(
                f"游戏引擎未注册: {gid}（当前支持 {sorted(REGISTERED_ENGINES)}）"
            )

        # 自博弈同 bot 同版本时，座位区分（seat 0/1）即可，不阻拦。显式 ID
        # 仍严格校验归属；None 则在此刻解析当前激活版本，而非推迟到 runner 启动。
        version_a = self._snapshot_bot_version(
            challenger_bot_id, bot_a_version_id, seat_label="座位0"
        )
        version_b = self._snapshot_bot_version(
            opponent_bot_id, bot_b_version_id, seat_label="座位1"
        )

        # 游戏规则参数（手数/棋盘/点阵）已由 GameSpec 钉死固定值，不再走 match_config。
        # match_config 仅保留版本快照等内部键（_run_match 读 _bot_a/b_version_id 解析版本路径）。
        # P2 residual：duplicate=True 时把标志 + seed 落 match_config，
        # __run_match_inner 据此走 run_duplicate（2 leg 合并），并落 match_seed 供回放。
        spec = game_registry.get(gid)
        if duplicate and spec.build_match_plan is None:
            raise ValueError(f"游戏 {gid} 不支持 duplicate 对局")
        mc: dict[str, Any] = {}
        if version_a is not None:
            mc["_bot_a_version_id"] = int(version_a["id"])
        if version_b is not None:
            mc["_bot_b_version_id"] = int(version_b["id"])
        if duplicate:
            mc["duplicate"] = True
            if duplicate_seed is not None:
                mc["duplicate_seed"] = int(duplicate_seed)

        match_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + secrets.token_hex(4)
        self._reserve_bot_slot(match_id)
        try:
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
            # duplicate 落 match_seed（确定性回放/复现用）。create_match 后的
            # 两次写都必须处在同一补偿边界内；任一步失败，调用方都尚未拿到 id。
            if duplicate and duplicate_seed is not None:
                self.store.update_match(match_id, match_seed=int(duplicate_seed))
            self.store.upsert_replay(match_id, "[]")
            if not defer_start:
                self.start_prepared_match(match_id)
        except Exception:
            # create_match 与 replay 分属两个 Store 事务；第二步失败时精确清理，
            # 不把一个调用方从未拿到 id 的 pending match 留成孤儿。
            if match_id not in self._tasks:
                self.store.delete_match(match_id)
                self._release_bot_slot(match_id)
            raise
        return match_id

    def start_prepared_match(self, match_id: str) -> None:
        """启动由 ``challenge(..., defer_start=True)`` 准备好的 pending 对局。

        赛事调度先准备 match、原子绑定 pairing，再调用本方法；这样 pairing 提交
        失败时 runner 尚未启动，可用 :meth:`discard_prepared_match` 精确补偿。
        """
        if match_id in self._tasks:
            return
        match = self.store.get_match(match_id)
        if not match:
            self._release_bot_slot(match_id)
            raise ValueError("待启动对局不存在")
        if match.get("status") != STATUS_PENDING:
            self._release_bot_slot(match_id)
            raise ValueError(f"仅 pending 对局可启动，当前状态: {match.get('status')}")
        self._reserve_bot_slot(match_id)
        try:
            task = asyncio.create_task(self._run_match(match_id), name=f"match-{match_id}")
            self._tasks[match_id] = task
        except Exception:
            self._release_bot_slot(match_id)
            raise

    def discard_prepared_match(self, match_id: str) -> bool:
        """删除尚未启动的 prepared match；已启动/非 pending 时拒绝删除。"""
        if match_id in self._tasks:
            return False
        match = self.store.get_match(match_id)
        if not match:
            self._release_bot_slot(match_id)
            return True
        if match.get("status") != STATUS_PENDING:
            return False
        deleted = self.store.delete_match(match_id)
        if deleted:
            self._release_bot_slot(match_id)
        return deleted

    async def abort_match(self, match_id: str) -> dict:
        """取消/drain 编排器拥有的任务并稳定落 aborted，终态不可倒退。"""
        reason = "admin_aborted"
        match = self.store.get_match(match_id)
        if not match:
            raise ValueError("对局不存在")
        if match.get("status") == STATUS_ABORTED:
            return match
        if match.get("status") == STATUS_COMPLETED:
            raise ValueError("已完成对局不能中止")

        terminal_error: ValueError | None = None
        updated: dict | None = None
        handoff_required = False
        task = self._tasks.get(match_id)
        self._admin_aborting.add(match_id)
        try:
            # Commit the terminal state before cancelling the owned task.  This
            # synchronous Store call cannot race the task on the same event loop;
            # if SQLite fails, the task and its subscribers remain intact instead
            # of being cancelled into a permanently ``running`` orphan.
            updated = self.store.abort_match_if_active(match_id, reason=reason)
            if not updated:
                raise ValueError("对局不存在")
            if updated.get("status") == STATUS_COMPLETED:
                # runner 在取消到达前已经完成；以真实 completed 为准，绝不倒退。
                terminal_error = ValueError("对局已完成，不能中止")
            elif updated.get("status") != STATUS_ABORTED:
                terminal_error = ValueError(
                    f"对局处于 {updated.get('status')} 态，不能中止"
                )
            else:
                # From this point the aborted row is authoritative. Even if a
                # best-effort replay/transport step fails, the cancelled task
                # cannot perform its own cleanup while _admin_aborting is set;
                # the handoff in finally therefore becomes mandatory.
                handoff_required = True
                if task is not None and not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                # The cancelled task normally releases human ownership in its
                # finally block. Keep this identity-guarded fallback for alternate
                # event-loop scheduling; match_id keys are immutable.
                if (
                    match.get("match_type") == TYPE_HUMAN
                    and task is not None
                    and self._tasks.get(match_id) is task
                ):
                    self._release_human_match_state(
                        match_id, match.get("human_user_id")
                    )
                try:
                    replay = self.store.get_replay(match_id) or {}
                except Exception:
                    logger.exception(
                        "admin abort replay read failed; preserving stored replay match=%s",
                        match_id,
                    )
                    replay_events = None
                else:
                    try:
                        replay_events = json.loads(replay.get("events_json") or "[]")
                    except (TypeError, ValueError):
                        replay_events = []
                    if not isinstance(replay_events, list):
                        replay_events = []
                terminal_event = _authoritative_error(reason)
                # Never replace a possibly complete replay with terminal-only
                # data after a transient read failure. Public reads synthesize
                # the authoritative error from the already-aborted match row.
                if replay_events is not None:
                    self._safe_flush_terminal_replay(
                        match_id, replay_events, terminal_event
                    )
                self._broadcast(match_id, terminal_event)
        finally:
            self._admin_aborting.discard(match_id)
            # The cancelled task's finally deliberately yields ownership while
            # admin abort is active. Once the terminal row exists, this handoff
            # must run even when replay reading/flushing/broadcasting raises.
            if handoff_required:
                self._admin_abort_handoffs.add(match_id)
                try:
                    await self._finish_match_task(
                        match_id, (updated or match).get("contest_id")
                    )
                finally:
                    self._admin_abort_handoffs.discard(match_id)
        if terminal_error is not None:
            raise terminal_error
        return self.store.get_match(match_id)

    def is_admin_abort_handoff(self, match_id: str) -> bool:
        """当前 on_match_done 是否由管理员 abort 显式触发。"""
        return match_id in self._admin_abort_handoffs

    async def challenge_duplicate(
        self,
        challenger_bot_id: int,
        opponent_bot_id: int,
        owner_user_id: int | None,
        *,
        match_type: str = TYPE_CHALLENGE,
        contest_id: int | None = None,
        game_id: str | None = None,
        bot_a_version_id: int | None = None,
        bot_b_version_id: int | None = None,
        duplicate_seed: int | None = None,
        defer_start: bool = False,
    ) -> str:
        """复式赛制（duplicate）对局：跑 2 leg（同副牌交换座位）合并 net 判胜负。

        签名与 challenge 一致，duplicate 标志只在编排层内部写入
        match_config，不是对外游戏规则配置。
        内部走 runner.run_duplicate（每 leg 同 deal_sequence，seat_swap 翻转 deltas
        累加到物理 bot）。游戏不支持 duplicate 时创建入口直接拒绝。

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
            bot_a_version_id=bot_a_version_id,
            bot_b_version_id=bot_b_version_id,
            duplicate=True,
            duplicate_seed=duplicate_seed,
            defer_start=defer_start,
        )

    def subscribe(
        self,
        match_id: str,
        *,
        human_viewer_seat: int | None = None,
    ) -> asyncio.Queue:
        # 统一 detailed + 嵌套 seats（含人类座真人用户名）
        from bzplat.backend.matches.seat_info import match_for_viewer

        m = sanitize_public_match(match_for_viewer(self.store, match_id))
        active_human = bool(
            m
            and m.get("match_type") == TYPE_HUMAN
            and m.get("status") in {STATUS_PENDING, STATUS_RUNNING}
        )
        active_events = self._active_replay_events.get(match_id)
        active_status = bool(
            m and m.get("status") in {STATUS_PENDING, STATUS_RUNNING}
        )
        if active_events is not None and active_status:
            snapshot_events = sanitize_public_event_prefix(
                list(_live_replay_events(active_events)),
                redact_active_human=active_human,
                human_viewer_seat=human_viewer_seat,
            )
        else:
            # 终态提交后、runner 收尾前也可能仍有内存引用；此时必须从
            # Store 读取由权威 match 行合成唯一终局的公开 replay。
            replay = self.store.get_public_replay(
                match_id,
                human_viewer_seat=human_viewer_seat,
            ) or {}
            snapshot_events = json.loads(replay.get("events_json") or "[]")
        snapshot = {
            "type": "snapshot",
            "match": m or {},
            "events": snapshot_events,
        }

        # 快照与可见性全部构造成功后再注册队列。否则 DB/JSON 异常会
        # 留下调用方永远拿不到、也无法 unsubscribe 的孤儿订阅。
        # maxsize=2000：减少 Bot 决策极快时丢事件（原 500 太小）；满时 drop oldest 见 _broadcast
        q: asyncio.Queue = asyncio.Queue(maxsize=2000)
        self._sse_human_views[q] = (active_human, human_viewer_seat)
        self._sse.setdefault(match_id, []).append(q)
        try:
            q.put_nowait(snapshot)
        except BaseException:
            self.unsubscribe(match_id, q)
            raise
        return q

    def unsubscribe(self, match_id: str, q: asyncio.Queue) -> None:
        lst = self._sse.get(match_id) or []
        if q in lst:
            lst.remove(q)
        self._sse_human_views.pop(q, None)
        # P1-7 修复：列表空时清 key，防 _sse dict 无界增长（每个曾观看的 match_id 永留空 list）。
        if not lst:
            self._sse.pop(match_id, None)

    def _broadcast(self, match_id: str, event: dict[str, Any]) -> None:
        for q in list(self._sse.get(match_id) or []):
            redact_active_human, human_viewer_seat = self._sse_human_views.get(
                # 可见性元数据丢失时 fail closed：隐藏底牌与人类请求，
                # 绝不能把缺省解释为“可公开”。
                q, (True, None)
            )
            public_event = sanitize_public_event(
                event,
                redact_active_human=redact_active_human,
                human_viewer_seat=human_viewer_seat,
            )
            if public_event is None:
                continue
            try:
                q.put_nowait(public_event)
            except asyncio.QueueFull:
                # 队列满：丢最旧事件腾位，保最新（避免观赛画面卡在最旧处）
                try:
                    q.get_nowait()
                    q.put_nowait(public_event)
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
            await self._finish_match_task(match_id, None)
            return
        try:
            gid = normalize_game_id(m.get("game_id"))
            spec = game_registry.get(gid)
        except (KeyError, ValueError) as exc:
            logger.error("match %s has invalid stored game_id: %s", match_id, exc)
            self.store.update_match(
                match_id,
                status=STATUS_ABORTED,
                reason="invalid_game_id",
                winner=None,
                ended_at=_now(),
            )
            terminal_event = _authoritative_error("invalid_game_id")
            self._safe_flush_terminal_replay(match_id, [], terminal_event)
            self._broadcast(match_id, terminal_event)
            await self._finish_match_task(match_id, m.get("contest_id"))
            return
        bot_a = self.store.get_bot(m["bot_a_id"])
        bot_b = self.store.get_bot(m["bot_b_id"])
        # 防护：bot 被删除后（ON DELETE SET NULL → bot_a_id/bot_b_id 为 NULL），
        # get_bot(None) 返 None，下一行解引用 binary_path 会崩。单侧缺失
        # 可明确技术判负；双方都缺失时没有公平 winner，必须保持无裁决。
        if bot_a is None or bot_b is None:
            logger.warning("match %s has null bot (a=%s b=%s) — bot deleted, aborting",
                           match_id, m.get("bot_a_id"), m.get("bot_b_id"))
            if bot_a is None and bot_b is None:
                self.store.update_match(
                    match_id,
                    status=STATUS_ABORTED,
                    reason="contest_both_bots_unavailable",
                    winner=None,
                    ended_at=_now(),
                )
                terminal_event = _authoritative_error(
                    "contest_both_bots_unavailable"
                )
                self._safe_flush_terminal_replay(match_id, [], terminal_event)
                self._broadcast(match_id, terminal_event)
                await self._finish_match_task(match_id, m.get("contest_id"))
                return
            winner = 1 if bot_a is None else 0  # 缺失方判负，存活方赢
            ea, eb = (-1, 1) if winner == 1 else (1, -1)
            self.store.update_match(
                match_id, status=STATUS_COMPLETED, reason="bot_deleted",
                winner=winner,
                result=build_result_payload(
                    spec, rounds_played=0, deltas=[ea, eb]
                ),
                technical_loss=1, ended_at=_now(),
            )
            terminal_event = _authoritative_match_end(
                winner, "bot_deleted", [ea, eb]
            )
            self._safe_flush_terminal_replay(match_id, [], terminal_event)
            await self._safe_postprocess_completed_match(m, match_id, winner, ea, eb)
            self._broadcast(match_id, terminal_event)
            # P0-1 回归修复：必须走收尾（清 _tasks + on_match_done 触发赛事推进），
            # 不能裸 return 绕过 finally——否则赛事对局卡死。
            await self._finish_match_task(match_id, m.get("contest_id"))
            return
        # 赛事从 pairing、其他对局从 match_config 读取创建时冻结的 version_id。
        # path/runtime_mode 始终来自同一版本行；只有 pre-version-schema legacy bot
        # 没有快照时才回退 bots 镜像。
        version_a_id: int | None = None
        version_b_id: int | None = None
        if m.get("match_type") == TYPE_CONTEST and m.get("contest_id"):
            pairing = self._find_contest_pairing(m["contest_id"], match_id)
            if pairing:
                version_a_id = pairing.get("bot_a_version_id")
                version_b_id = pairing.get("bot_b_version_id")
        else:
            # challenge/table/ladder：无论 API 是否显式选版本，创建时都已把当时
            # 的实际版本 ID 写入配置，排队期间切换 current 不会改变执行程序。
            mc = m.get("match_config") or {}
            if isinstance(mc, str):
                try:
                    mc = json.loads(mc)
                except Exception:
                    mc = {}
            version_a_id = mc.get("_bot_a_version_id")
            version_b_id = mc.get("_bot_b_version_id")
        # match_config.duplicate=True 时必须由 game spec 提供明确的多 leg 计划。
        stored_mc = m.get("match_config") or {}
        if isinstance(stored_mc, str):
            try:
                stored_mc = json.loads(stored_mc)
            except Exception:
                stored_mc = {}
        want_duplicate = bool(stored_mc.get("duplicate"))
        if want_duplicate and spec.build_match_plan is None:
            logger.error("match %s has unsupported duplicate config for %s", match_id, gid)
            self.store.update_match(
                match_id,
                status=STATUS_ABORTED,
                reason="invalid_match_config",
                winner=None,
                ended_at=_now(),
            )
            terminal_event = _authoritative_error("invalid_match_config")
            self._safe_flush_terminal_replay(match_id, [], terminal_event)
            self._broadcast(match_id, terminal_event)
            await self._finish_match_task(match_id, m.get("contest_id"))
            return
        # duplicate 用确定性 seed（落库供回放/复现；单 leg 不强制 seed，沿用随机）。
        dup_seed = int(stored_mc.get("duplicate_seed")) if stored_mc.get("duplicate_seed") is not None else None
        events: list[dict] = []
        self._active_replay_events[match_id] = events

        def on_event(kind: str, ev: dict) -> None:
            # The engine terminal is an internal result signal, not a public
            # transport/replay terminal: result/status have not committed yet.
            # This is especially important for duplicate matches, whose every
            # leg emits a game-level match_end while the platform match remains
            # running. The final flush replaces all of them with one canonical
            # platform terminal.
            if kind in {"match_end", "error"} or ev.get("type") in {
                "match_end",
                "error",
            }:
                return
            events.append(ev)
            self._broadcast(match_id, ev)
            if kind in ("settle", "hand_start", "move", "match_start") or len(events) % 5 == 0:
                self.store.upsert_replay(
                    match_id,
                    json.dumps(_live_replay_events(events), ensure_ascii=False),
                )

        try:
            path_a, mode_a = self._runtime_for_bot_version(
                bot_a, version_a_id, seat=0
            )
            path_b, mode_b = self._runtime_for_bot_version(
                bot_b, version_b_id, seat=1
            )
            logger.info(
                "match start id=%s game=%s type=%s a=%s(%s) b=%s(%s) duplicate=%s",
                match_id, gid, m.get("match_type"),
                m["bot_a_id"], bot_a.get("name"), m["bot_b_id"], bot_b.get("name"),
                want_duplicate,
            )
            self.store.update_match(
                match_id, status=STATUS_RUNNING, started_at=_now()
            )
            if want_duplicate:
                result = await self.runner.run_duplicate(
                    path_a,
                    path_b,
                    game_id=gid,
                    on_event=on_event,
                    seed=dup_seed,
                    runtime_modes=(mode_a, mode_b),
                    time_budget_per_side=spec.time_budget_per_side,
                    duplicate=True,
                )
            else:
                result = await self.runner.run_binaries(
                    path_a,
                    path_b,
                    game_id=gid,
                    on_event=on_event,
                    runtime_modes=(mode_a, mode_b),
                    time_budget_per_side=spec.time_budget_per_side,
                )
            # duplicate：每 leg 独立判胜负（result.legs），不把净筹码合并判 1 场。
            # 胜负完全由 standings/ranking 读 result.legs 决定；match.winner 留 None。
            if want_duplicate:
                winner = None  # 胜负由 standings 读 result.legs 决定（无单一 match 胜者）
                terminal_reason = "completed"
                legs_data = getattr(result, "legs", None) or []
                # 破同分用：两 leg 物理 deltas 累加
                ea = sum(int(lg.get("deltas", [0, 0])[0]) for lg in legs_data) if legs_data else 0
                eb = sum(int(lg.get("deltas", [0, 0])[1]) for lg in legs_data) if legs_data else 0
                self.store.update_match(
                    match_id,
                    status=STATUS_COMPLETED,
                    winner=None,  # 胜负由 standings 读 result.legs 决定
                    reason=terminal_reason,
                    result=build_engine_result_payload(
                        spec,
                        result,
                        deltas=[ea, eb],
                        extra={"legs": legs_data},
                    ),
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
                terminal_reason = _completed_match_reason(result, events)
                self.store.update_match(
                    match_id,
                    status=STATUS_COMPLETED,
                    winner=winner,
                    reason=terminal_reason,
                    result=build_engine_result_payload(
                        spec, result, deltas=[ea, eb]
                    ),
                    ended_at=_now(),
                )
            terminal_event = _authoritative_match_end(
                winner, terminal_reason, [ea, eb]
            )
            self._safe_flush_terminal_replay(match_id, events, terminal_event)
            await self._safe_postprocess_completed_match(m, match_id, winner, ea, eb)
            self._broadcast(match_id, terminal_event)
            logger.info(
                "match done id=%s winner=%s rounds=%s ea=%s eb=%s rated=%s",
                match_id, winner, result.rounds_played, ea, eb,
                m["match_type"] != TYPE_CONTEST,
            )
        except BotVersionUnavailableError as exc:
            self._abort_version_unavailable(match_id, exc, events)
        except BotTechnicalError as exc:
            # Bot stdout protocol faults and Bot decision timeouts are attributable,
            # terminal failures.  Score one deterministic technical loss instead of
            # fabricating 70 fallback folds / illegal board moves.
            _ensure_technical_incident(events, exc)
            failed_seat = exc.failed_seat
            winner = 1 - failed_seat
            ea, eb = (-1, 1) if failed_seat == 0 else (1, -1)
            failed_bot_id = m["bot_a_id"] if failed_seat == 0 else m["bot_b_id"]
            failed_version_id = version_a_id if failed_seat == 0 else version_b_id
            failed_runtime = mode_a if failed_seat == 0 else mode_b
            logger.warning(
                "bot technical failure match_id=%s reason=%s code=%s "
                "bot_id=%s version_id=%s runtime=%s seat=%s turn=%s leg=%s error=%s",
                match_id,
                exc.reason,
                exc.error_code,
                failed_bot_id,
                failed_version_id,
                failed_runtime,
                failed_seat,
                exc.turn,
                exc.leg,
                str(exc)[:200],
            )
            self.store.update_match(
                match_id,
                status=STATUS_COMPLETED,
                reason=exc.reason,
                winner=winner,
                result=build_technical_result_payload(
                    spec,
                    events,
                    deltas=[ea, eb],
                    extra=_technical_incident_summary(events),
                ),
                technical_loss=1,
                ended_at=_now(),
            )
            terminal_event = _authoritative_match_end(
                winner, exc.reason, [ea, eb]
            )
            self._safe_flush_terminal_replay(match_id, events, terminal_event)
            await self._safe_postprocess_completed_match(
                m, match_id, winner, ea, eb
            )
            self._broadcast(match_id, terminal_event)
        except PlatformRunnerError as exc:
            # Docker daemon/image/runtime failures are platform faults, not Bot
            # behaviour.  Abort without a winner/technical loss and, crucially,
            # without invoking the rating/XP/notification completion pipeline.
            logger.error("match %s sandbox unavailable — %s", match_id, exc)
            self.store.update_match(
                match_id,
                status=STATUS_ABORTED,
                reason="platform_error",
                ended_at=_now(),
            )
            terminal_event = _authoritative_error("platform_error")
            self._safe_flush_terminal_replay(match_id, events, terminal_event)
            self._broadcast(match_id, terminal_event)
        except BotCrashedError as exc:
            logger.warning("match %s bot crashed — %s", match_id, exc)
            # Bot 启动崩溃 → 技术判负（completed + winner=对手 + technical_loss=1）。
            # 统一所有对局类型（原仅 contest 走 completed，challenge/ladder/table 走 aborted
            # 无结果无胜者——这是「游戏结束显示已取消而非已完成」的根因）。
            # 崩溃方从 exc.crashed_seat 取（runner 在 start_session 失败时注解）；
            # 未注解（游戏内崩溃已由引擎处理产出正常 result，不会到这；bot_a start
            # 失败未注解→默认 0）。
            crashed_seat = getattr(exc, "crashed_seat", None) or 0
            winner = 1 - crashed_seat
            ea, eb = (-1, 1) if crashed_seat == 0 else (1, -1)
            self.store.update_match(
                match_id, status=STATUS_COMPLETED, reason="technical_loss",
                winner=winner,
                result=build_technical_result_payload(
                    spec, events, deltas=[ea, eb]
                ),
                technical_loss=1, ended_at=_now(),
            )
            terminal_event = _authoritative_match_end(
                winner, "technical_loss", [ea, eb]
            )
            self._safe_flush_terminal_replay(match_id, events, terminal_event)
            await self._safe_postprocess_completed_match(m, match_id, winner, ea, eb)
            self._broadcast(match_id, terminal_event)
        except Exception as exc:
            logger.exception("match %s failed", match_id)
            self.store.update_match(
                match_id,
                status=STATUS_ABORTED,
                reason="platform_error",
                ended_at=_now(),
            )
            terminal_event = _authoritative_error("platform_error")
            self._safe_flush_terminal_replay(match_id, events, terminal_event)
            self._broadcast(match_id, terminal_event)
        finally:
            await self._finish_match_task(match_id, m.get("contest_id"))

    async def _safe_postprocess_completed_match(
        self,
        match: dict,
        match_id: str,
        winner: int | None,
        ea: int,
        eb: int,
    ) -> None:
        """后处理失败只记日志；不得把已落库的 completed 业务结果改写 aborted。"""
        try:
            await self._postprocess_completed_match(match, match_id, winner, ea, eb)
        except Exception:
            logger.exception("completed match postprocess failed match=%s", match_id)

    def _safe_flush_terminal_replay(
        self,
        match_id: str,
        events: list[dict],
        terminal_event: dict[str, Any] | None = None,
    ) -> None:
        """终态后的最后一次 replay flush 失败不得让状态/原因倒退。

        对局进行中已由 ``on_event`` 持续写快照；最后 flush 是补强持久化。此时
        completed/aborted 业务结果已经提交，故写失败只记录诊断；completed 后的
        评分仍必须执行，aborted 的明确原因也必须保留。
        游戏 engine 自己发出的 ``match_end`` 是内部中间事件，一律移除；completed
        路径传入的 ``terminal_event`` 会作为唯一终态追加，确保公开 replay 与 live
        transport 使用完全相同的 ``winner/reason/deltas`` schema。复式每 leg 的
        明细由 ``result.legs`` 保存，不再伪装成多个整场终态。
        challenge/challenge_human 创建阶段的初始 replay 写入不走本 helper，仍保持
        失败即补偿删除 pending 对局的强约束。
        """
        try:
            replay_events = _bounded_replay_events(_live_replay_events(events))
            if terminal_event is not None:
                replay_events.append(dict(terminal_event))
            self.store.upsert_replay(
                match_id,
                json.dumps(replay_events, ensure_ascii=False),
            )
        except Exception:
            logger.exception("terminal match final replay flush failed match=%s", match_id)

    async def _postprocess_completed_match(
        self,
        match: dict,
        match_id: str,
        winner: int | None,
        ea: int,
        eb: int,
    ) -> None:
        """统一 completed 后处理：评分/pair_stats、通知与 XP。

        正常结果、启动崩溃技术判负和 Bot 被删技术判负都走同一契约；contest
        对局仍只计赛事积分，人类对局也不计 Glicko，二者均不走本后处理。

        rating settlement marker 同评分事务提交；全局顺序屏障会先补齐
        ``settled_order <= 当前对局`` 的所有缺口。重复调用在 marker
        claim 处返回，避免 rating/history/pair_stats 及通知/XP 重复执行。
        """
        if match.get("match_type") in (TYPE_CONTEST, TYPE_HUMAN):
            return

        # winner/ea/eb 是运行时便捷参数；顺序补算必须以 DB 中已落稳的
        # completed result 为真相源，才能对早场与当前场走同一条恢复路径。
        del winner, ea, eb
        target = self.store.get_match(match_id)
        if target is None:
            raise RuntimeError(f"completed match disappeared before settlement: {match_id}")
        _, target_settled = await self._settle_rating_sequence_through(
            target_match=target,
            emit_side_effects=True,
            suppress_errors=False,
        )
        if not target_settled:
            logger.info("match rating already settled; skip postprocess match=%s", match_id)

    def _completed_match_side_effects(
        self,
        match: dict,
        match_id: str,
        winner: int | None,
    ) -> None:
        """评分 marker 新提交后的最佳努力通知/XP 副作用。"""
        bot_a_id = match.get("bot_a_id")
        bot_b_id = match.get("bot_b_id")

        if self.notifier is not None and bot_a_id is not None and bot_b_id is not None:
            try:
                wl = "平局" if winner is None else f"座位 {winner + 1} 胜"
                self.notifier.notify_both_owners(
                    bot_a_id,
                    bot_b_id,
                    type="match_done",
                    title=f"对局完成：{wl}",
                    body=f"对局 {match_id} 已结束（{match.get('game_id', '')}）。",
                    link=f"/match/{match_id}",
                )
            except Exception:
                logger.debug("notify match_done failed", exc_info=True)

        try:
            from bzplat.backend.store.schema import XP_MATCH_PARTICIPATE, XP_MATCH_WIN

            ba = self.store.get_bot(bot_a_id) if bot_a_id is not None else None
            bb = self.store.get_bot(bot_b_id) if bot_b_id is not None else None
            for bot, won in ((ba, winner == 0), (bb, winner == 1)):
                if bot and bot.get("owner_id"):
                    xp = XP_MATCH_PARTICIPATE + (XP_MATCH_WIN if won else 0)
                    self.store.award_xp(int(bot["owner_id"]), xp)
        except Exception:
            logger.debug("award_xp failed", exc_info=True)

    @staticmethod
    def _rating_settlement_order_key(match: dict) -> tuple[int, str]:
        """Use the completion-frozen global order, never a timestamp guess."""
        order = match.get("_rating_settled_order")
        if order is None:
            raise ValueError(
                f"match {match.get('id')} lacks frozen rating settlement order"
            )
        return (int(order), str(match.get("id") or ""))

    @staticmethod
    def _persisted_rating_inputs(match: dict) -> tuple[int | None, int, int]:
        """从已持久 completed match 提取可恢复的评分输入。"""
        match_id = str(match.get("id") or "")
        raw_deltas = (match.get("result") or {}).get("deltas")
        if not isinstance(raw_deltas, list) or len(raw_deltas) < 2:
            raise ValueError(
                f"cannot recover rating settlement match={match_id}: "
                "result.deltas missing"
            )
        try:
            ea, eb = int(raw_deltas[0]), int(raw_deltas[1])
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"cannot recover rating settlement match={match_id}: "
                f"invalid deltas={raw_deltas!r}"
            ) from exc
        winner_raw = match.get("winner")
        winner = int(winner_raw) if winner_raw in (0, 1) else None
        return winner, ea, eb

    async def _settle_rating_sequence_through(
        self,
        *,
        target_match: dict | None,
        emit_side_effects: bool,
        suppress_errors: bool,
    ) -> tuple[int, bool]:
        """按全局稳定顺序结算到 target（含），或 target=None 时全量恢复。

        同一把异步锁覆盖「扫描→评分 marker 提交→通知/XP」，因此较晚
        target 即使抢先进入，也会代为完成较早场的完整后处理；较早场
        随后进入时由 marker 幂等跳过。绝不结算 target 之后的场次，避免
        其本身后处理因 marker 已存在而丢通知/XP。

        ``suppress_errors=True`` 仅用于启动恢复：记录第一个阻塞点并停止，
        绝不跳过坏记录继续结算后面场次。
        """
        target_id = (
            str(target_match.get("id") or "") if target_match is not None else None
        )
        target_key = (
            self._rating_settlement_order_key(target_match)
            if target_match is not None
            else None
        )
        settled_count = 0
        target_settled = False

        async with self._rating_settlement_order_lock:
            pending = sorted(
                self.store.list_unsettled_completed_rating_matches(),
                key=self._rating_settlement_order_key,
            )
            for candidate in pending:
                candidate_key = self._rating_settlement_order_key(candidate)
                if target_key is not None and candidate_key > target_key:
                    break
                candidate_id = str(candidate.get("id") or "")
                try:
                    candidate_winner, candidate_ea, candidate_eb = (
                        self._persisted_rating_inputs(candidate)
                    )
                    settled = await self._settle_completed_match_rating(
                        candidate,
                        candidate_id,
                        candidate_winner,
                        candidate_ea,
                        candidate_eb,
                    )
                except Exception:
                    if suppress_errors:
                        logger.exception(
                            "rating settlement sequence blocked match=%s", candidate_id
                        )
                        break
                    raise
                if not settled:
                    continue
                settled_count += 1
                if candidate_id == target_id:
                    target_settled = True
                if emit_side_effects:
                    self._completed_match_side_effects(
                        candidate, candidate_id, candidate_winner
                    )

        return settled_count, target_settled

    async def _settle_completed_match_rating(
        self,
        match: dict,
        match_id: str,
        winner: int | None,
        ea: int,
        eb: int,
    ) -> bool:
        """在稳定锁顺序下结算一场 Bot 对局；重复 settlement 返回 False。"""
        rating_policy = self.store.match_rating_policy(match)
        if not rating_policy.get("rated"):
            logger.info(
                "rating-neutral match=%s reason=%s",
                match_id,
                rating_policy.get("rating_reason"),
            )
            return self.store.mark_match_rating_settled(match_id)
        bot_a_id = match.get("bot_a_id")
        bot_b_id = match.get("bot_b_id")
        if bot_a_id is None or bot_b_id is None:
            # Bot 已硬删除后无法再构造 Glicko 对手快照；标记为已处理，避免每次
            # 重启无限扫描。正常 bot_deleted 判负若双方仍在则仍按技术结果计分。
            logger.warning(
                "completed match %s lost bot reference; mark rating settlement without rating",
                match_id,
            )
            return self.store.mark_match_rating_settled(match_id)

        bot_a_id = int(bot_a_id)
        bot_b_id = int(bot_b_id)
        gid = normalize_game_id(match.get("game_id"))
        first, second = sorted((bot_a_id, bot_b_id))
        async with self._rating_lock_for(first, gid):
            if first != second:
                async with self._rating_lock_for(second, gid):
                    return self._apply_ratings(
                        bot_a_id,
                        bot_b_id,
                        winner,
                        ea,
                        eb,
                        reason=match_id,
                        settlement_id=match_id,
                        game_id=gid,
                    )
            return self._apply_ratings(
                bot_a_id,
                bot_b_id,
                winner,
                ea,
                eb,
                reason=match_id,
                settlement_id=match_id,
                game_id=gid,
            )

    async def recover_unsettled_match_ratings(self) -> int:
        """启动时补算 completed 非赛事 Bot 对局，且不重复通知或 XP。

        completed 结果先于评分提交；进程若在两者之间退出，marker 不存在。
        本方法与运行时后处理共用全局顺序屏障，只补评分、不重发通知或 XP。
        任一早场损坏/失败时停在该处，不允许后场越过它改变 Glicko 顺序。
        """
        recovered, _ = await self._settle_rating_sequence_through(
            target_match=None,
            emit_side_effects=False,
            suppress_errors=True,
        )
        return recovered

    async def _finish_match_task(self, match_id: str, contest_id: int | None) -> None:
        """对局任务收尾：清理 _tasks + 触发 on_match_done（赛事推进必须经此）。

        P0-1 回归修复：原 null-bot 防护分支 return 在 try/finally 外，绕过此收尾
        → _tasks 泄漏 + 赛事对局 on_match_done 不触发 → 赛事卡死。现抽成方法，
        所有对局结束路径（含 null-bot/崩溃/正常完成）统一调用。
        """
        self._tasks.pop(match_id, None)
        self._release_bot_slot(match_id)
        if match_id in self._admin_aborting:
            return
        # P1-7：对局结束后清 SSE dict 的该 match_id 条目（直播已结束，防无界增长）。
        # 残留订阅者会在 _broadcast 时因 list 为空自然无操作；unsubscribe 也会清空 list。
        for queue in self._sse.pop(match_id, []):
            self._sse_human_views.pop(queue, None)
        self._active_replay_events.pop(match_id, None)
        if self.on_match_done is not None:
            try:
                result = self.on_match_done(match_id, contest_id)
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
            human_seat = int(m["human_seat"]) if m.get("human_seat") is not None else 1
            bot_seat = 1 - human_seat
            bot_id = m["bot_a_id"] if bot_seat == 0 else m["bot_b_id"]
            bot = self.store.get_bot(bot_id)
            try:
                gid = normalize_game_id(m.get("game_id"))
            except ValueError as exc:
                logger.error("human match %s has invalid stored game_id: %s", match_id, exc)
                self.store.update_match(
                    match_id,
                    status=STATUS_ABORTED,
                    reason="invalid_game_id",
                    winner=None,
                    ended_at=_now(),
                )
                terminal_event = _authoritative_error("invalid_game_id")
                self._safe_flush_terminal_replay(match_id, [], terminal_event)
                self._broadcast(match_id, terminal_event)
                await self._finish_match_task(match_id, m.get("contest_id"))
                return
            stored_mc = m.get("match_config") or {}
            if isinstance(stored_mc, str):
                try:
                    stored_mc = json.loads(stored_mc)
                except Exception:
                    stored_mc = {}
            version_id = stored_mc.get(
                "_bot_a_version_id" if bot_seat == 0 else "_bot_b_version_id"
            )
            events: list[dict] = []
            self._active_replay_events[match_id] = events
            try:
                bot_path, bot_mode = self._runtime_for_bot_version(
                    bot, version_id, seat=bot_seat
                )
            except BotVersionUnavailableError as exc:
                self._abort_version_unavailable(match_id, exc, events)
                self._release_human_match_state(
                    match_id, m.get("human_user_id")
                )
                return
            self.store.update_match(match_id, status=STATUS_RUNNING, started_at=_now())
            consecutive_timeouts = {"n": 0}  # 闭包内可变计数器

            def on_event(kind: str, ev: dict) -> None:
                # See the Bot-vs-Bot path above: a game-level match_end is an
                # internal result signal, not permission to close WebSocket or
                # create a second public replay contract before the result commits.
                if kind in {"match_end", "error"} or ev.get("type") in {
                    "match_end",
                    "error",
                }:
                    return
                events.append(ev)
                self._broadcast(match_id, ev)
                if kind in ("settle", "hand_start", "move", "match_start", "turn") or len(events) % 5 == 0:
                    self.store.upsert_replay(
                        match_id,
                        json.dumps(_live_replay_events(events), ensure_ascii=False),
                    )
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
                self.store.upsert_replay(
                    match_id,
                    json.dumps(_live_replay_events(events), ensure_ascii=False),
                )
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
                spec = game_registry.get(gid)
                result = await self.runner.run_bot_vs_human(
                    bot_path,
                    bot_seat=bot_seat,
                    human_decide=human_decide,
                    game_id=gid,
                    on_event=on_event,
                    runtime_mode=bot_mode,
                    time_budget_per_side=spec.time_budget_per_side,
                )
                ea = sum(r.deltas[0] for r in result.rounds)
                eb = sum(r.deltas[1] for r in result.rounds)
                # winner：引擎 result.winner 已权威化（见 _run_match 同款逻辑）
                winner = result.winner
                if winner is None:
                    winner = 0 if ea > eb else 1 if eb > ea else None
                terminal_reason = _completed_match_reason(result, events)
                self.store.update_match(
                    match_id, status=STATUS_COMPLETED,
                    winner=winner, reason=terminal_reason,
                    result=build_engine_result_payload(
                        spec, result, deltas=[ea, eb]
                    ),
                    ended_at=_now(),
                )
                terminal_event = _authoritative_match_end(
                    winner, terminal_reason, [ea, eb]
                )
                self._safe_flush_terminal_replay(
                    match_id, events, terminal_event
                )
                # 人类对战不计 Glicko-2（人类无 rating 行）
                self._broadcast(match_id, terminal_event)
            except BotTechnicalError as exc:
                # Only the subprocess path can construct BotTechnicalError. Human
                # WebSocket payloads keep their existing game-validation/inactivity
                # semantics and are never mislabeled as Bot protocol failures.
                _ensure_technical_incident(events, exc)
                winner = 1 - bot_seat
                ea, eb = (-1, 1) if bot_seat == 0 else (1, -1)
                logger.warning(
                    "bot technical failure match_id=%s reason=%s code=%s "
                    "bot_id=%s version_id=%s runtime=%s seat=%s turn=%s leg=%s error=%s",
                    match_id,
                    exc.reason,
                    exc.error_code,
                    bot.get("id"),
                    version_id,
                    bot_mode,
                    bot_seat,
                    exc.turn,
                    exc.leg,
                    str(exc)[:200],
                )
                self.store.update_match(
                    match_id,
                    status=STATUS_COMPLETED,
                    winner=winner,
                    reason=exc.reason,
                    result=build_technical_result_payload(
                        spec,
                        events,
                        deltas=[ea, eb],
                        extra=_technical_incident_summary(events),
                    ),
                    technical_loss=1,
                    ended_at=_now(),
                )
                terminal_event = _authoritative_match_end(
                    winner, exc.reason, [ea, eb]
                )
                self._safe_flush_terminal_replay(
                    match_id, events, terminal_event
                )
                # Human matches never enter Glicko/pair_stats settlement.
                self._broadcast(match_id, terminal_event)
            except PlatformRunnerError as exc:
                logger.error("human match %s sandbox unavailable — %s", match_id, exc)
                self.store.update_match(
                    match_id,
                    status=STATUS_ABORTED,
                    reason="platform_error",
                    ended_at=_now(),
                )
                terminal_event = _authoritative_error("platform_error")
                self._safe_flush_terminal_replay(
                    match_id, events, terminal_event
                )
                self._broadcast(match_id, terminal_event)
            except BotCrashedError as exc:
                # Bot 启动即崩/EOF——快速 abort，广播清晰错误（而非吞成默认动作死磕数小时）
                logger.warning("human match %s aborted: bot crashed — %s", match_id, exc)
                self.store.update_match(match_id, status=STATUS_ABORTED, reason="bot_crashed", ended_at=_now())
                terminal_event = _authoritative_error("bot_crashed")
                self._safe_flush_terminal_replay(
                    match_id, events, terminal_event
                )
                self._broadcast(match_id, terminal_event)
            except HumanInactive as exc:
                # 人类连续超时不响应 → 中止对局，释放人类槽（避免死磕占用 + 锁死用户）
                logger.warning("human match %s aborted: human inactive — %s", match_id, exc)
                self.store.update_match(
                    match_id, status=STATUS_ABORTED, reason="human_inactive", ended_at=_now(),
                )
                terminal_event = _authoritative_error("human_inactive")
                self._safe_flush_terminal_replay(
                    match_id, events, terminal_event
                )
                self._broadcast(match_id, terminal_event)
            except Exception as exc:
                logger.exception("human match %s failed", match_id)
                self.store.update_match(
                    match_id,
                    status=STATUS_ABORTED,
                    reason="platform_error",
                    ended_at=_now(),
                )
                terminal_event = _authoritative_error("platform_error")
                self._safe_flush_terminal_replay(
                    match_id, events, terminal_event
                )
                self._broadcast(match_id, terminal_event)
            finally:
                self._release_human_match_state(
                    match_id, m.get("human_user_id")
                )

    def _rating_lock_for(self, bot_id: int, game_id: str) -> asyncio.Lock:
        """获取/创建某 (bot, game) 的评分串行化锁（防同 bot 并发评分 lost-update）。

        P1-8 修复：锁永不清理会导致 dict 随 bot 数无界增长。采用惰性清理——
        当 dict 超阈值（如 2000）时，清掉所有未被持有的空闲锁（asyncio.Lock.locked()）。
        """
        key = (bot_id, game_id)
        lock = self._rating_locks.get(key)
        if lock is None:
            # 惰性清理：超阈值时回收空闲锁（防无界增长；活跃锁 locked()=True 保留）
            if len(self._rating_locks) > 2000:
                self._rating_locks = {
                    k: v for k, v in self._rating_locks.items() if v.locked()
                }
            lock = asyncio.Lock()
            self._rating_locks[key] = lock
        return lock

    def _apply_ratings(
        self,
        bot_a_id: int,
        bot_b_id: int,
        winner: int | None,
        ea: int,
        eb: int,
        *,
        reason: str = "",
        settlement_id: str | None = None,
        game_id: str | None = None,
    ) -> bool:
        # 自博弈（同 bot 对战）：不计 Glicko 评分——同 bot 评分无信息量，且 update_rating_row
        # 同一行被写两次（ra/rb 是同一快照），第二次覆盖第一次，导致胜负/评分错乱。
        # 自博弈仅作功能验证/版本对比，不进天梯，但必须原子 claim settlement，
        # 否则启动恢复会反复扫描。同 contest（match_type=contest）不会进这里。
        if game_id is None:
            bot = self.store.get_bot(bot_a_id)
            gid = normalize_game_id((bot or {}).get("game_id"))
        else:
            gid = normalize_game_id(game_id)
        if bot_a_id == bot_b_id:
            logger.info("self-play match %s vs %s: skip rating update", bot_a_id, bot_b_id)
            return self.store.apply_match_ratings_atomic(
                bot_a_id,
                bot_b_id,
                game_id=gid,
                rating_a=(0.0, 0.0, 0.0),
                rating_b=(0.0, 0.0, 0.0),
                winner=winner,
                delta_a=ea,
                delta_b=eb,
                reason=reason,
                settlement_id=settlement_id,
            )
        self.store.ensure_rating(bot_a_id, game_id=gid)
        self.store.ensure_rating(bot_b_id, game_id=gid)
        ra = self.store.get_rating(bot_a_id, game_id=gid)
        rb = self.store.get_rating(bot_b_id, game_id=gid)
        sa, sb = match_scores(winner)
        ra_new = update_rating(
            Rating(ra["rating"], ra["rd"], ra["vol"]),
            [(Rating(rb["rating"], rb["rd"], rb["vol"]), sa)],
        )
        rb_new = update_rating(
            Rating(rb["rating"], rb["rd"], rb["vol"]),
            [(Rating(ra["rating"], ra["rd"], ra["vol"]), sb)],
        )
        return self.store.apply_match_ratings_atomic(
            bot_a_id,
            bot_b_id,
            game_id=gid,
            rating_a=(ra_new.mu, ra_new.phi, ra_new.sigma),
            rating_b=(rb_new.mu, rb_new.phi, rb_new.sigma),
            winner=winner,
            delta_a=ea,
            delta_b=eb,
            reason=reason,
            settlement_id=settlement_id,
        )
