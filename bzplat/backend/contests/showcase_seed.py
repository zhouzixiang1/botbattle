"""Idempotent generator for the six long-lived contest showcase snapshots.

All match results and replay events are produced by the production
Manager/Orchestrator/GameSpec pipeline.  This module never fabricates match or
pairing terminal rows.  Callers must provide an explicit database and upload
directory; the CLI wrapper owns primary-database confirmation.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import time
from pathlib import Path
from typing import Any, Callable

from bzplat.backend.bots import BotManager
from bzplat.backend.contests.manager import ContestManager
from bzplat.backend.contests.showcase import SHOWCASE_KEYS
from bzplat.backend.crypto import hash_password
from bzplat.backend.matches.orchestrator import MatchOrchestrator
from bzplat.backend.matches.runner import MatchRunner
from bzplat.backend.runtime.binary_runner import BinaryRunner
from bzplat.backend.store import Store
from bzplat.backend.store.schema import (
    CONTEST_DRAFT,
    CONTEST_FINISHED,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    CONTEST_REST,
    CONTEST_RUNNING,
    ROLE_ORGANIZER,
    ROLE_USER,
    STATUS_COMPLETED,
    STATUS_ABORTED,
    STATUS_PENDING,
    STATUS_RUNNING,
)

logger = logging.getLogger(__name__)

SEED_VERSION = "contest-showcase-v1"
ORGANIZER_USERNAME = "showcase_organizer"
PLAYER_PREFIX = "showcase_player_"
BOT_PREFIX = "showcase_gomoku_"
SHOWCASE_GAME_ID = "gomoku"
PLAYER_COUNT = 12
TARGET_STATUS = {
    "contest_lifecycle_draft": CONTEST_DRAFT,
    "contest_lifecycle_open": CONTEST_OPEN,
    "contest_lifecycle_published": CONTEST_PUBLISHED,
    "contest_lifecycle_running": CONTEST_RUNNING,
    "contest_lifecycle_rest": CONTEST_REST,
    "contest_lifecycle_finished": CONTEST_FINISHED,
}
ENTRY_COUNT = {
    "contest_lifecycle_draft": 4,
    "contest_lifecycle_open": 6,
    "contest_lifecycle_published": 12,
    "contest_lifecycle_running": 12,
    "contest_lifecycle_rest": 12,
    "contest_lifecycle_finished": 12,
}
TITLE = {
    "contest_lifecycle_draft": "【合成演示】01 草稿与名册准备",
    "contest_lifecycle_open": "【合成演示】02 报名开放中",
    "contest_lifecycle_published": "【合成演示】03 排期已发布（手动开赛）",
    "contest_lifecycle_running": "【合成演示】04 小组赛进行中",
    "contest_lifecycle_rest": "【合成演示】05 小组赛结束·晋级确认",
    "contest_lifecycle_finished": "【合成演示】06 完整赛事·正式名次",
}


class ShowcaseSeedError(RuntimeError):
    pass


def _marker(key: str) -> str:
    return f"[{SEED_VERSION}:{key}]"


def _description(key: str) -> str:
    descriptions = {
        "contest_lifecycle_draft": "展示组织者建赛后配置模板、准备名册但尚未开放报名。",
        "contest_lifecycle_open": "展示实名报名窗口、当前参赛名单与手动时间编排。",
        "contest_lifecycle_published": "展示 12 人分组双循环排期；starts_at 为空，等待组织者手动开赛。",
        "contest_lifecycle_running": "冻结在小组赛首轮完成后：同时展示真实回放、实时积分和后续待赛。",
        "contest_lifecycle_rest": "24 场小组双循环已完成，展示每组排名、Top 2 晋级与阶段休息。",
        "contest_lifecycle_finished": "完整展示 24 场小组双循环与 7 场 Top 8 单败、连续正式名次和全部回放。",
    }
    return f"{_marker(key)}\n合成演示快照（只读，不代表真实活动赛事）。{descriptions[key]}"


def _ensure_user(
    store: Store,
    *,
    username: str,
    email: str,
    role: str,
    display_name: str,
    index: int | None = None,
) -> dict[str, Any]:
    by_name = store.get_user_by_username(username)
    by_email = store.get_user_by_email(email)
    if by_name is not None:
        if by_name.get("email") != email or by_name.get("role") != role:
            raise ShowcaseSeedError(f"专用演示账号冲突: {username}")
        if by_email is None or by_email.get("id") != by_name.get("id"):
            raise ShowcaseSeedError(f"专用演示邮箱归属冲突: {email}")
        user = by_name
    elif by_email is not None:
        raise ShowcaseSeedError(f"专用演示邮箱已被占用: {email}")
    else:
        user = store.create_user(
            username,
            email,
            hash_password(secrets.token_urlsafe(32)),
            display_name=display_name,
            role=role,
            real_name=(f"演示选手{index:02d}" if index is not None else "演示组织者"),
            phone=(f"1390000{index:04d}" if index is not None else ""),
            school=("智算实验学校" if index is not None else ""),
            student_id=(f"DEMO2026{index:03d}" if index is not None else ""),
        )
    return store.update_user(
        int(user["id"]),
        display_name=display_name,
        is_active=1,
        email_verified=1,
        bio="合成演示账号；仅用于客户展示，不代表真实参赛者。",
    )


def _canonical_version(
    store: Store, bot: dict[str, Any], upload_root: Path, checksum: str
) -> bool:
    current = store.get_current_bot_version(int(bot["id"]))
    if not current:
        return False
    try:
        path = Path(str(current["binary_path"])).resolve()
        canonical = (
            upload_root.resolve()
            / str(bot["id"])
            / f"v{int(current['version'])}"
            / "bot.bin"
        ).resolve()
        return bool(
            path == canonical
            and path.is_file()
            and current.get("checksum") == checksum
            and current.get("runtime_mode") == "longrunning"
            and bot.get("runtime_mode") == "longrunning"
            and int(bot.get("current_version") or 0) == int(current["version"])
        )
    except (KeyError, OSError, TypeError, ValueError):
        return False


def provision_showcase_identities(
    store: Store,
    upload_root: Path,
    sample_binary: Path,
    *,
    emit: Callable[[str], None] = print,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if not sample_binary.is_file():
        raise ShowcaseSeedError(f"五子棋样例 ELF 不存在: {sample_binary}")
    raw = sample_binary.read_bytes()
    checksum = hashlib.sha256(raw).hexdigest()
    organizer = _ensure_user(
        store,
        username=ORGANIZER_USERNAME,
        email="showcase-organizer@invalid.example",
        role=ROLE_ORGANIZER,
        display_name="赛事演示组织者",
    )
    players = [
        _ensure_user(
            store,
            username=f"{PLAYER_PREFIX}{index:02d}",
            email=f"showcase-player-{index:02d}@invalid.example",
            role=ROLE_USER,
            display_name=f"演示选手 {index:02d}",
            index=index,
        )
        for index in range(1, PLAYER_COUNT + 1)
    ]

    upload_root.mkdir(parents=True, exist_ok=True)
    manager = BotManager(store, upload_root=upload_root)
    preflight = BinaryRunner(prefer_local=True)
    bots: list[dict[str, Any]] = []
    for index, player in enumerate(players, 1):
        name = f"{BOT_PREFIX}{index:02d}"
        bot = store.get_bot_by_owner_name(int(player["id"]), name)
        if bot is None:
            bot = manager.create_from_upload(
                int(player["id"]),
                name,
                raw,
                display_name=f"演示棋手 {index:02d}",
                description="合成演示 LongRunning 五子棋 Bot",
                upload_note=f"{SEED_VERSION} canonical sample",
                game_id=SHOWCASE_GAME_ID,
                runtime_mode="longrunning",
                binary_runner=preflight,
            )
            emit(f"Bot {index:02d}/12：已创建 #{bot['id']}")
        else:
            if bot.get("game_id") != SHOWCASE_GAME_ID or int(bot.get("owner_id") or 0) != int(player["id"]):
                raise ShowcaseSeedError(f"专用演示 Bot 冲突: {name}")
            if not _canonical_version(store, bot, upload_root, checksum):
                bot = manager.upload_version(
                    int(bot["id"]),
                    int(player["id"]),
                    raw,
                    upload_note=f"{SEED_VERSION} canonical refresh",
                    runtime_mode="longrunning",
                    binary_runner=preflight,
                )
                emit(f"Bot {index:02d}/12：已刷新 canonical LongRunning 版本")
            if not bot.get("is_active"):
                bot = manager.set_active(int(bot["id"]), int(player["id"]), True)
        bots.append(store.get_bot(int(bot["id"])))
    return organizer, players, bots


def _find_seed_contest(store: Store, organizer_id: int, key: str) -> dict[str, Any] | None:
    frozen = store.get_contest_by_showcase_key(key)
    if frozen:
        return frozen
    candidates = [
        contest
        for contest in store.list_contests(organizer_id=organizer_id)
        if _marker(key) in str(contest.get("description") or "")
    ]
    if len(candidates) > 1:
        raise ShowcaseSeedError(f"发现多个未完成演示生成记录: {key}")
    return candidates[0] if candidates else None


def _running_stages() -> list[dict[str, Any]]:
    return [
        {
            "key": "group",
            "type": "group_double_round_robin",
            "group_count": 4,
            "advance_per_group": 2,
            "scoring": "ccgc_2_1_0",
            "rest_after_minutes": 10,
            "allow_bot_swap_in_rest": True,
            "round_stagger_minutes": 60,
        },
        {
            "key": "ko",
            "type": "single_elimination",
            "scoring": "ccgc_2_1_0",
            "rest_after_minutes": 0,
            "allow_bot_swap_in_rest": False,
        },
    ]


async def _ensure_roster(
    manager: ContestManager,
    contest: dict[str, Any],
    players: list[dict[str, Any]],
    bots: list[dict[str, Any]],
    count: int,
) -> dict[str, Any]:
    cid = int(contest["id"])
    for player, bot in zip(players[:count], bots[:count]):
        if manager.store.get_entry(cid, int(player["id"])):
            continue
        latest = manager.store.get_contest(cid)
        if latest["status"] == CONTEST_DRAFT:
            await manager.add_roster_entry(cid, int(player["id"]), int(bot["id"]))
        elif latest["status"] == CONTEST_OPEN:
            await manager.register(cid, int(player["id"]), int(bot["id"]))
        else:
            raise ShowcaseSeedError(f"赛事 #{cid} 已进入 {latest['status']} 但名册不完整")
    entries = manager.store.list_contest_entries(cid)
    if len(entries) != count:
        raise ShowcaseSeedError(f"赛事 #{cid} 名册应为 {count}，实际 {len(entries)}")
    return manager.store.get_contest(cid)


def _active_matches(store: Store, contest_id: int) -> list[dict[str, Any]]:
    return store.list_matches(
        limit=1000,
        contest_id=contest_id,
        game_id=SHOWCASE_GAME_ID,
    )


async def _recover_incomplete_showcases(
    manager: ContestManager,
    organizer_id: int,
    *,
    emit: Callable[[str], None],
) -> int:
    """Recover only this seed namespace after an interrupted invocation.

    The platform-wide startup recovery is intentionally *not* called here: a
    deployment may run the seed beside unrelated active contests, and an
    operational data command must not adopt or abort their tasks.  Seed-owned
    pending/running matches have no surviving in-memory task, so they are
    aborted, their exact pairing is reset, and only those contests are passed
    through the normal one-contest reconciler.
    """
    candidates = [
        contest
        for contest in manager.store.list_contests(organizer_id=organizer_id)
        if not contest.get("showcase_key")
        and any(
            _marker(key) in str(contest.get("description") or "")
            for key in SHOWCASE_KEYS
        )
    ]
    recovered = 0
    for contest in candidates:
        contest_id = int(contest["id"])
        pairings = manager.store.list_contest_pairings(contest_id)
        bound_ids = {
            str(pairing["match_id"])
            for pairing in pairings
            if pairing.get("match_id")
        }
        matches = manager.store.list_matches(
            limit=1000, contest_id=contest_id, game_id=SHOWCASE_GAME_ID
        )
        for match in matches:
            match_id = str(match["id"])
            if match.get("status") in (STATUS_PENDING, STATUS_RUNNING):
                match = manager.store.abort_match_if_active(
                    match_id, reason="showcase_seed_interrupted"
                ) or match
                recovered += 1
            if match.get("status") != STATUS_ABORTED:
                continue
            if manager.store.reset_aborted_contest_pairing(contest_id, match_id):
                recovered += 1
            elif match_id not in bound_ids:
                # prepare succeeded but pairing bind did not: keep no ghost row.
                recovered += int(manager.store.delete_match(match_id))

        for pairing in manager.store.list_contest_pairings(contest_id):
            if pairing.get("status") != STATUS_RUNNING or not pairing.get("match_id"):
                continue
            match = manager.store.get_match(str(pairing["match_id"]))
            if match is None:
                manager.store.update_contest_pairing(
                    int(pairing["id"]), status=STATUS_PENDING, match_id=None
                )
                recovered += 1
            elif match.get("status") == STATUS_COMPLETED:
                recovered += int(
                    bool(
                        manager.store.complete_contest_pairing_for_match(
                            contest_id, str(pairing["match_id"])
                        )
                    )
                )
            elif match.get("status") == STATUS_ABORTED:
                recovered += int(
                    bool(
                        manager.store.reset_aborted_contest_pairing(
                            contest_id, str(pairing["match_id"])
                        )
                    )
                )

        await manager._reconcile_one(contest_id)
    if recovered:
        emit(f"恢复上次中断的演示生成状态：{recovered} 项")
    return recovered


async def _wait_until(
    manager: ContestManager,
    contest_id: int,
    predicate: Callable[[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]], bool],
    *,
    timeout: float,
    emit: Callable[[str], None],
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last_progress: tuple[str, int, int] | None = None
    while time.monotonic() < deadline:
        contest = manager.store.get_contest(contest_id)
        pairings = manager.store.list_contest_pairings(contest_id)
        matches = _active_matches(manager.store, contest_id)
        active = [m for m in matches if m.get("status") in (STATUS_PENDING, STATUS_RUNNING)]
        progress = (
            str(contest.get("status")),
            sum(1 for pairing in pairings if pairing.get("status") == STATUS_COMPLETED),
            len(pairings),
        )
        if progress != last_progress:
            emit(f"  #{contest_id} {progress[0]}：对阵 {progress[1]}/{progress[2]}，活跃 {len(active)}")
            last_progress = progress
        tasks = [task for task in manager.orch._tasks.values() if not task.done()]
        if predicate(contest, pairings, active) and not tasks:
            return contest
        if not tasks and contest.get("status") == CONTEST_RUNNING:
            await manager._dispatch_pending(contest_id, int(contest.get("current_stage_idx") or 0))
        await asyncio.sleep(0.1)
    raise ShowcaseSeedError(f"等待赛事 #{contest_id} 收敛超时（{timeout:.0f}s）")


async def _drive_contest(
    manager: ContestManager,
    key: str,
    organizer: dict[str, Any],
    players: list[dict[str, Any]],
    bots: list[dict[str, Any]],
    *,
    timeout: float,
    emit: Callable[[str], None],
) -> dict[str, Any]:
    existing = _find_seed_contest(manager.store, int(organizer["id"]), key)
    if existing and existing.get("showcase_key"):
        if existing.get("status") != TARGET_STATUS[key]:
            raise ShowcaseSeedError(f"冻结快照 {key} 状态异常: {existing.get('status')}")
        emit(f"{key}：已存在 #{existing['id']}，跳过")
        return existing

    if existing is None:
        starts_at = None
        stages = None
        if key == "contest_lifecycle_running":
            from datetime import datetime

            starts_at = datetime.now().isoformat(timespec="seconds")
            stages = _running_stages()
        existing = manager.create(
            int(organizer["id"]),
            TITLE[key],
            description=_description(key),
            template_id="gomoku_group_drr_ko",
            game_id=SHOWCASE_GAME_ID,
            stages=stages,
            require_real_name=1 if key == "contest_lifecycle_open" else 0,
            starts_at=starts_at,
        )
        emit(f"{key}：创建赛事 #{existing['id']}")

    cid = int(existing["id"])
    existing = await _ensure_roster(
        manager, existing, players, bots, ENTRY_COUNT[key]
    )
    target = TARGET_STATUS[key]

    if target == CONTEST_DRAFT:
        pass
    else:
        if existing["status"] == CONTEST_DRAFT:
            existing = await manager.open_registration(cid)
        if target == CONTEST_OPEN:
            pass
        elif target == CONTEST_PUBLISHED:
            if existing["status"] == CONTEST_OPEN:
                existing = await manager.publish(cid)
            if existing["status"] != CONTEST_PUBLISHED or existing.get("starts_at") is not None:
                raise ShowcaseSeedError("published 演示必须保持 starts_at=NULL")
        elif target == CONTEST_RUNNING:
            if existing["status"] == CONTEST_OPEN:
                existing = await manager.publish(cid)
            if existing["status"] == CONTEST_PUBLISHED:
                await manager._dispatch_pending(cid, 0)
            existing = await _wait_until(
                manager,
                cid,
                lambda contest, pairings, active: (
                    contest.get("status") == CONTEST_RUNNING
                    and not active
                    and any(p.get("status") == STATUS_COMPLETED for p in pairings)
                    and any(p.get("status") == STATUS_PENDING and not p.get("match_id") for p in pairings)
                ),
                timeout=timeout,
                emit=emit,
            )
        else:
            if existing["status"] in (CONTEST_OPEN, CONTEST_PUBLISHED):
                existing = await manager.start(cid)
            if target == CONTEST_REST:
                existing = await _wait_until(
                    manager,
                    cid,
                    lambda contest, _pairings, active: contest.get("status") == CONTEST_REST and not active,
                    timeout=timeout,
                    emit=emit,
                )
            elif target == CONTEST_FINISHED:
                while True:
                    existing = manager.store.get_contest(cid)
                    if existing["status"] == CONTEST_REST:
                        await manager.resume(cid)
                    if existing["status"] == CONTEST_FINISHED:
                        break
                    existing = await _wait_until(
                        manager,
                        cid,
                        lambda contest, _pairings, active: contest.get("status") in (CONTEST_REST, CONTEST_FINISHED) and not active,
                        timeout=timeout,
                        emit=emit,
                    )

    frozen = manager.store.freeze_contest_showcase(cid, key)
    emit(f"{key}：冻结完成 #{cid} ({frozen['status']})")
    return frozen


async def seed_showcases(
    db_path: Path,
    upload_root: Path,
    sample_binary: Path,
    *,
    max_concurrent: int = 2,
    timeout_per_contest: float = 1800,
    emit: Callable[[str], None] = print,
) -> dict[str, Any]:
    store = Store(str(db_path))
    orch: MatchOrchestrator | None = None
    try:
        organizer, players, bots = provision_showcase_identities(
            store, upload_root, sample_binary, emit=emit
        )
        runner = MatchRunner(BinaryRunner(prefer_local=True))
        orch = MatchOrchestrator(
            store,
            runner=runner,
            max_concurrent=max(1, int(max_concurrent)),
        )
        manager = ContestManager(store, orch)

        async def on_done(match_id: str, contest_id: int | None) -> None:
            if contest_id is not None:
                await manager.handle_match_done(match_id, contest_id)

        orch.on_match_done = on_done
        await _recover_incomplete_showcases(
            manager, int(organizer["id"]), emit=emit
        )
        contests = []
        for key in SHOWCASE_KEYS:
            contests.append(
                await _drive_contest(
                    manager,
                    key,
                    organizer,
                    players,
                    bots,
                    timeout=timeout_per_contest,
                    emit=emit,
                )
            )
        return verify_showcases(store)
    finally:
        if orch is not None:
            await orch.shutdown()
        store.close()


def verify_showcases(store: Store) -> dict[str, Any]:
    contests: dict[str, dict[str, Any]] = {}
    all_match_ids: set[str] = set()
    expected_graph = {
        "contest_lifecycle_draft": (0, 0),
        "contest_lifecycle_open": (0, 0),
        "contest_lifecycle_published": (24, 0),
        "contest_lifecycle_running": (24, 4),
        "contest_lifecycle_rest": (24, 24),
        "contest_lifecycle_finished": (31, 31),
    }
    for key in SHOWCASE_KEYS:
        contest = store.get_contest_by_showcase_key(key)
        if not contest:
            raise ShowcaseSeedError(f"缺少演示快照: {key}")
        if contest.get("status") != TARGET_STATUS[key]:
            raise ShowcaseSeedError(f"{key} 状态应为 {TARGET_STATUS[key]}")
        if not str(contest.get("title") or "").startswith("【合成演示】"):
            raise ShowcaseSeedError(f"{key} 未明确标注合成演示")
        cid = int(contest["id"])
        entries = store.list_contest_entries(cid)
        pairings = store.list_contest_pairings(cid)
        matches = store.list_matches(
            limit=1000, contest_id=cid, game_id=SHOWCASE_GAME_ID
        )
        if len(entries) != ENTRY_COUNT[key]:
            raise ShowcaseSeedError(f"{key} 名册数量异常")
        expected_pairings, expected_matches = expected_graph[key]
        if len(pairings) != expected_pairings or len(matches) != expected_matches:
            raise ShowcaseSeedError(
                f"{key} 图规模异常: pairings={len(pairings)}, matches={len(matches)}"
            )
        if any(match.get("status") in (STATUS_PENDING, STATUS_RUNNING) for match in matches):
            raise ShowcaseSeedError(f"{key} 仍有活跃 match")
        if any(match.get("status") != STATUS_COMPLETED for match in matches):
            raise ShowcaseSeedError(f"{key} 含非 completed 历史 match")
        match_ids = {str(match["id"]) for match in matches}
        bound_ids = {
            str(pairing["match_id"])
            for pairing in pairings
            if pairing.get("match_id")
        }
        if bound_ids != match_ids:
            raise ShowcaseSeedError(f"{key} pairing/match 绑定集合不一致")
        for match in matches:
            replay = store.get_replay(str(match["id"]))
            if not replay or not json.loads(replay.get("events_json") or "[]"):
                raise ShowcaseSeedError(f"{key} 对局缺少真实回放: {match['id']}")
        if all_match_ids.intersection(match_ids):
            raise ShowcaseSeedError("演示赛事之间复用了 match_id")
        all_match_ids.update(match_ids)
        contests[key] = {
            "contest_id": cid,
            "status": contest["status"],
            "entries": len(entries),
            "pairings": len(pairings),
            "matches": len(matches),
        }

    published_id = contests["contest_lifecycle_published"]["contest_id"]
    published = store.get_contest(published_id)
    published_pairings = store.list_contest_pairings(published_id)
    if published.get("starts_at") is not None or len(published_pairings) != 24:
        raise ShowcaseSeedError("published 快照必须为手动开赛的 24 场待赛排期")
    if any(p.get("match_id") or p.get("status") != STATUS_PENDING for p in published_pairings):
        raise ShowcaseSeedError("published 快照不应含已派发对局")

    running_id = contests["contest_lifecycle_running"]["contest_id"]
    running_pairings = store.list_contest_pairings(running_id)
    completed_running = sum(
        1 for pairing in running_pairings if pairing.get("status") == STATUS_COMPLETED
    )
    pending_running = sum(
        1
        for pairing in running_pairings
        if pairing.get("status") == STATUS_PENDING and not pairing.get("match_id")
    )
    if (completed_running, pending_running) != (4, 20):
        raise ShowcaseSeedError("running 快照必须冻结为 4 completed + 20 pending")

    rest_id = contests["contest_lifecycle_rest"]["contest_id"]
    rest_stage0 = store.list_contest_pairings(rest_id, stage_idx=0)
    if len(rest_stage0) != 24 or any(p.get("status") != STATUS_COMPLETED for p in rest_stage0):
        raise ShowcaseSeedError("rest 快照必须完成全部 24 场小组赛")
    if len(store.list_stage_results(rest_id, stage_idx=0)) != 12:
        raise ShowcaseSeedError("rest 快照缺少 12 条持久化小组排名")

    finished_id = contests["contest_lifecycle_finished"]["contest_id"]
    finished_pairings = store.list_contest_pairings(finished_id)
    if len(finished_pairings) != 31:
        raise ShowcaseSeedError("finished 快照必须为 24 场小组赛 + 7 场淘汰赛")
    if any(p.get("status") != STATUS_COMPLETED for p in finished_pairings):
        raise ShowcaseSeedError("finished 快照存在未完成 pairing")
    finished_matches = store.list_matches(
        limit=1000, contest_id=finished_id, game_id=SHOWCASE_GAME_ID
    )
    if len(finished_matches) != 31:
        raise ShowcaseSeedError("finished 快照的 31 个 pairing 必须各有独立 match")
    official = store.list_official_results(finished_id)
    if [row["rank"] for row in official] != list(range(1, 13)):
        raise ShowcaseSeedError("finished 正式名次必须为连续 1..12")
    if len(store.list_stage_results(finished_id, stage_idx=0)) != 12:
        raise ShowcaseSeedError("finished 缺少小组阶段结果")
    if len(store.list_stage_results(finished_id, stage_idx=1)) < 8:
        raise ShowcaseSeedError("finished 缺少淘汰阶段结果")
    group_points = {
        float(row.get("points") or 0)
        for row in store.list_stage_results(finished_id, stage_idx=0)
    }
    if len(group_points) < 2:
        raise ShowcaseSeedError("finished 小组排名积分全平，不适合作为展示数据")
    return {
        "seed_version": SEED_VERSION,
        "showcases": contests,
        "total_showcase_matches": len(all_match_ids),
        "verified": True,
    }


def rollback_showcases(
    store: Store,
    upload_root: Path,
    *,
    emit: Callable[[str], None] = print,
) -> dict[str, int]:
    organizer = store.get_user_by_username(ORGANIZER_USERNAME)
    if not organizer:
        return {"contests": 0, "matches": 0, "bots": 0, "users": 0}
    if (
        organizer.get("email") != "showcase-organizer@invalid.example"
        or organizer.get("role") != ROLE_ORGANIZER
    ):
        raise ShowcaseSeedError("专用演示组织者账号身份不匹配，拒绝回滚")
    allowed_keys = set(SHOWCASE_KEYS)
    candidates = [
        contest
        for contest in store.list_contests(organizer_id=int(organizer["id"]))
        if contest.get("showcase_key") in allowed_keys
        or any(_marker(key) in str(contest.get("description") or "") for key in allowed_keys)
    ]
    for contest in candidates:
        key = contest.get("showcase_key")
        if key is not None and key not in allowed_keys:
            raise ShowcaseSeedError(f"拒绝回滚非白名单 showcase_key: {key}")
        marker_keys = [
            candidate_key
            for candidate_key in SHOWCASE_KEYS
            if _marker(candidate_key) in str(contest.get("description") or "")
        ]
        if len(marker_keys) != 1 or (key is not None and marker_keys[0] != key):
            raise ShowcaseSeedError(f"赛事 #{contest['id']} 缺少唯一 seed 标记，拒绝回滚")
        if not str(contest.get("title") or "").startswith("【合成演示】"):
            raise ShowcaseSeedError(f"赛事 #{contest['id']} 未明确标注合成演示")
        matches = store.list_matches(
            limit=1000,
            contest_id=int(contest["id"]),
            game_id=SHOWCASE_GAME_ID,
        )
        if any(match.get("status") in (STATUS_PENDING, STATUS_RUNNING) for match in matches):
            raise ShowcaseSeedError(f"赛事 #{contest['id']} 仍有活跃对局，拒绝回滚")

    candidate_ids = {int(contest["id"]) for contest in candidates}
    organized_ids = {
        int(contest["id"])
        for contest in store.list_contests(organizer_id=int(organizer["id"]))
    }
    if organized_ids != candidate_ids:
        raise ShowcaseSeedError("专用演示组织者仍有关联的非白名单赛事，拒绝回滚")
    if store.list_bots(owner_id=int(organizer["id"]), active_only=False):
        raise ShowcaseSeedError("专用演示组织者持有非预期 Bot，拒绝回滚")
    if any(
        match.get("contest_id") not in candidate_ids
        for match in store.list_matches(limit=10000, owner_id=int(organizer["id"]))
    ):
        raise ShowcaseSeedError("专用演示组织者存在非白名单对局，拒绝回滚")

    players = [
        store.get_user_by_username(f"{PLAYER_PREFIX}{index:02d}")
        for index in range(1, PLAYER_COUNT + 1)
    ]
    player_bots: list[tuple[dict[str, Any], dict[str, Any] | None]] = []
    resolved_upload_root = upload_root.resolve()
    for index, player in enumerate(players, 1):
        if not player:
            continue
        expected_email = f"showcase-player-{index:02d}@invalid.example"
        if player.get("email") != expected_email or player.get("role") != ROLE_USER:
            raise ShowcaseSeedError(f"专用演示账号身份不匹配: {PLAYER_PREFIX}{index:02d}")
        owned = store.list_bots(owner_id=int(player["id"]), active_only=False)
        expected_name = f"{BOT_PREFIX}{index:02d}"
        if len(owned) > 1 or (owned and owned[0].get("name") != expected_name):
            raise ShowcaseSeedError(f"专用演示账号 Bot 清单异常: {player['username']}")
        if not owned:
            player_bots.append((player, None))
            continue
        bot = owned[0]
        if bot.get("game_id") != SHOWCASE_GAME_ID or "合成演示" not in str(bot.get("description") or ""):
            raise ShowcaseSeedError(f"专用演示 Bot 元数据异常: {expected_name}")
        bot_root = (resolved_upload_root / str(bot["id"])).resolve()
        versions = store.list_bot_versions(int(bot["id"]))
        if not versions or any(
            not Path(str(version.get("binary_path") or "")).resolve().is_relative_to(bot_root)
            for version in versions
        ):
            raise ShowcaseSeedError(f"专用演示 Bot 文件不属于指定 --upload-root: {expected_name}")
        foreign_matches = [
            match
            for match in store.list_matches(limit=10000, bot_id=int(bot["id"]))
            if match.get("contest_id") not in candidate_ids
        ]
        if foreign_matches:
            raise ShowcaseSeedError(f"专用演示 Bot 存在非白名单对局: {expected_name}")
        player_bots.append((player, bot))

    player_ids = {int(player["id"]) for player, _bot in player_bots}
    bot_ids = {int(bot["id"]) for _player, bot in player_bots if bot is not None}
    for contest in store.list_contests():
        if int(contest["id"]) in candidate_ids:
            continue
        if any(
            int(entry.get("user_id") or 0) in player_ids
            or int(entry.get("bot_id") or 0) in bot_ids
            for entry in store.list_contest_entries(int(contest["id"]))
        ):
            raise ShowcaseSeedError("专用演示账号存在非白名单赛事报名，拒绝回滚")

    deleted_matches = 0
    for contest in candidates:
        cid = int(contest["id"])
        for match in store.list_matches(
            limit=1000, contest_id=cid, game_id=SHOWCASE_GAME_ID
        ):
            deleted_matches += int(store.delete_match(str(match["id"])))
        store.delete_contest(cid)
        emit(f"已删除演示赛事 #{cid}")

    bot_manager = BotManager(store, upload_root=upload_root)
    deleted_bots = 0
    deleted_users = 0
    for player, bot in player_bots:
        if bot is not None:
            bot_manager.purge_bot_files(int(bot["id"]))
            deleted_bots += int(store.delete_bot(int(bot["id"])))
        deleted_users += int(store.delete_user(int(player["id"])))
    deleted_users += int(store.delete_user(int(organizer["id"])))
    return {
        "contests": len(candidates),
        "matches": deleted_matches,
        "bots": deleted_bots,
        "users": deleted_users,
    }


__all__ = [
    "ShowcaseSeedError",
    "rollback_showcases",
    "seed_showcases",
    "verify_showcases",
]
