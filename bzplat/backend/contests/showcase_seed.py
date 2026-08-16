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
import os
import secrets
import stat
import time
from dataclasses import dataclass
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
    TYPE_CONTEST,
)

logger = logging.getLogger(__name__)

SEED_VERSION = "contest-showcase-v1"
SHOWCASE_STRATEGY_VERSION = "gomoku-showcase-matrix-v3"
SHOWCASE_UPLOAD_BASENAME = "bot_uploads_showcase"
SHOWCASE_UPLOAD_MARKER = ".botbattle-contest-showcase"
SHOWCASE_UPLOAD_MARKER_CONTENT = f"{SEED_VERSION}\n"
ORGANIZER_USERNAME = "showcase_organizer"
PLAYER_PREFIX = "showcase_player_"
BOT_PREFIX = "showcase_gomoku_"
SHOWCASE_GAME_ID = "gomoku"
HISTORICAL_SHOWCASE_TEMPLATE_ID = "gomoku_group_drr_ko"
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
SHOWCASE_PROFILE_SPECS: dict[str, dict[str, Any]] = {
    "tactical": {
        "label": "战术型",
        "filename": "gomoku_showcase_tactical_linux_amd64",
        "checksum": "b58e1cc3c8dece688332cec3b780da2d32d8fdfc7daf38baa4f3af981251ded7",
        "size": 789760,
    },
    "steady": {
        "label": "稳健型",
        "filename": "gomoku_showcase_steady_linux_amd64",
        "checksum": "895ee49576ef98a3f3001e6d980a49b66f32ec6ab7a88c29fb4f79dd1140e1e2",
        "size": 789760,
    },
    "foundation": {
        "label": "基础型",
        "filename": "gomoku_showcase_foundation_linux_amd64",
        "checksum": "b33cc91ea5d6ff370414e8c9c2fca905d10032866a3560020b3d134132dac4bb",
        "size": 789760,
    },
}
SHOWCASE_PLAYER_PROFILES = (
    *("tactical" for _ in range(4)),
    *("steady" for _ in range(4)),
    *("foundation" for _ in range(4)),
)


class ShowcaseSeedError(RuntimeError):
    pass


@dataclass(frozen=True)
class _RollbackPlan:
    """Fully validated deletion whitelist, frozen before the first mutation."""

    organizer_id: int
    contest_matches: tuple[tuple[int, tuple[str, ...]], ...]
    player_bots: tuple[tuple[int, int | None], ...]
    upload_root: Path


def _log_emit(message: str) -> None:
    logger.info("%s", message)


def _profile_for_player(index: int) -> tuple[str, dict[str, Any]]:
    try:
        name = SHOWCASE_PLAYER_PROFILES[index - 1]
        return name, SHOWCASE_PROFILE_SPECS[name]
    except (IndexError, KeyError) as exc:
        raise ShowcaseSeedError(f"演示 Bot 编号超出 profile 清单: {index}") from exc


def _load_showcase_profile_binaries(profile_dir: Path) -> dict[str, bytes]:
    """Load only the three reviewed, checksum-pinned showcase ELF profiles."""
    candidate = profile_dir.expanduser()
    if not candidate.is_absolute():
        raise ShowcaseSeedError("演示 Bot profile 目录必须使用绝对路径")
    if candidate.is_symlink():
        raise ShowcaseSeedError("演示 Bot profile 目录不得为符号链接")
    root = candidate.resolve()
    if not root.is_dir():
        raise ShowcaseSeedError(f"演示 Bot profile 目录不存在: {root}")
    binaries: dict[str, bytes] = {}
    for name, spec in SHOWCASE_PROFILE_SPECS.items():
        path = root / spec["filename"]
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            raise ShowcaseSeedError(f"演示 Bot profile 文件非法: {path}")
        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        if checksum != spec["checksum"] or len(raw) != int(spec["size"]):
            raise ShowcaseSeedError(
                f"演示 Bot profile checksum/size 不匹配: {name} "
                f"(expected={spec['checksum']}/{spec['size']}, "
                f"actual={checksum}/{len(raw)})"
            )
        binaries[name] = raw
    return binaries


def validate_showcase_upload_target(
    upload_root: Path,
    *,
    db_path: Path,
    checkout_root: Path,
) -> Path:
    """Validate the non-overlapping directory reserved for showcase binaries."""
    candidate = upload_root.expanduser()
    if not candidate.is_absolute():
        raise ShowcaseSeedError("--upload-root 必须使用绝对路径")
    if candidate.is_symlink():
        raise ShowcaseSeedError("--upload-root 不得为符号链接")
    resolved = candidate.resolve()
    if resolved.name != SHOWCASE_UPLOAD_BASENAME:
        raise ShowcaseSeedError(
            f"--upload-root 目录名必须固定为 {SHOWCASE_UPLOAD_BASENAME}"
        )
    db_parent = db_path.resolve().parent
    checkout = checkout_root.resolve()
    dangerous = {Path("/"), Path.home().resolve(), db_parent, checkout}
    if resolved in dangerous:
        raise ShowcaseSeedError("--upload-root 指向高风险目录，拒绝操作")
    if any(parent.name in {"bot_uploads", "uploads"} for parent in resolved.parents):
        raise ShowcaseSeedError("--upload-root 不得位于普通 Bot 上传目录子树")
    for ordinary in (db_parent / "bot_uploads", checkout / "bot_uploads"):
        ordinary = ordinary.resolve()
        if resolved == ordinary or resolved.is_relative_to(ordinary):
            raise ShowcaseSeedError("--upload-root 不得复用普通 bot_uploads")
    return resolved


def _reserved_identity_graph(
    store: Store,
    *,
    require_complete: bool,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate and return only the seed-owned organizer, players and Bots."""
    organizer = store.get_user_by_username(ORGANIZER_USERNAME)
    if organizer is None:
        if require_complete:
            raise ShowcaseSeedError("缺少专用演示组织者账号")
    elif (
        organizer.get("email") != "showcase-organizer@invalid.example"
        or organizer.get("role") != ROLE_ORGANIZER
    ):
        raise ShowcaseSeedError("专用演示组织者账号身份不匹配")

    players: list[dict[str, Any]] = []
    bots: list[dict[str, Any]] = []
    for index in range(1, PLAYER_COUNT + 1):
        username = f"{PLAYER_PREFIX}{index:02d}"
        player = store.get_user_by_username(username)
        if player is None:
            if require_complete:
                raise ShowcaseSeedError(f"缺少专用演示账号: {username}")
            continue
        expected_email = f"showcase-player-{index:02d}@invalid.example"
        if player.get("email") != expected_email or player.get("role") != ROLE_USER:
            raise ShowcaseSeedError(f"专用演示账号身份不匹配: {username}")
        players.append(player)
        owned = store.list_bots(owner_id=int(player["id"]), active_only=False)
        expected_name = f"{BOT_PREFIX}{index:02d}"
        if len(owned) > 1 or (owned and owned[0].get("name") != expected_name):
            raise ShowcaseSeedError(f"专用演示账号 Bot 清单异常: {username}")
        if not owned:
            if require_complete:
                raise ShowcaseSeedError(f"缺少专用演示 Bot: {expected_name}")
            continue
        bot = owned[0]
        if (
            int(bot.get("owner_id") or 0) != int(player["id"])
            or bot.get("game_id") != SHOWCASE_GAME_ID
            or bot.get("runtime_mode") != "longrunning"
            or "合成演示" not in str(bot.get("description") or "")
        ):
            raise ShowcaseSeedError(f"专用演示 Bot 元数据异常: {expected_name}")
        bots.append(bot)
    if require_complete and (len(players), len(bots)) != (PLAYER_COUNT, PLAYER_COUNT):
        raise ShowcaseSeedError("专用演示身份图不完整")
    return organizer, players, bots


def validate_showcase_upload_namespace(
    store: Store,
    upload_root: Path,
    *,
    create: bool = False,
    require_complete: bool = False,
) -> dict[str, int]:
    """Fail closed unless the directory contains exactly DB-owned Bot versions."""
    root = upload_root.expanduser()
    if not root.is_absolute() or root.name != SHOWCASE_UPLOAD_BASENAME:
        raise ShowcaseSeedError(
            f"演示 Bot 目录必须是绝对的 {SHOWCASE_UPLOAD_BASENAME}"
        )
    if root.is_symlink():
        raise ShowcaseSeedError("演示 Bot 目录不得为符号链接")
    root = root.resolve()
    if not root.exists():
        if not create:
            raise ShowcaseSeedError("演示 Bot 目录不存在或缺少 namespace marker")
        root.mkdir(parents=True, exist_ok=False)
    if not root.is_dir():
        raise ShowcaseSeedError("演示 Bot 路径不是目录")

    marker = root / SHOWCASE_UPLOAD_MARKER
    contents = list(root.iterdir())
    if not marker.exists():
        if create and not contents:
            marker.write_text(SHOWCASE_UPLOAD_MARKER_CONTENT, encoding="utf-8")
        else:
            raise ShowcaseSeedError("演示 Bot 目录缺少 namespace marker")
    marker_mode = marker.lstat().st_mode
    if (
        stat.S_ISLNK(marker_mode)
        or not stat.S_ISREG(marker_mode)
        or marker.read_text(encoding="utf-8") != SHOWCASE_UPLOAD_MARKER_CONTENT
    ):
        raise ShowcaseSeedError("演示 Bot namespace marker 非法")

    _organizer, _players, bots = _reserved_identity_graph(
        store, require_complete=require_complete
    )
    allowed_dirs = {root}
    allowed_files = {marker}
    for bot in bots:
        bot_id = int(bot["id"])
        versions = store.list_bot_versions(bot_id)
        if not versions:
            raise ShowcaseSeedError(f"专用演示 Bot #{bot_id} 缺少版本")
        bot_dir = root / str(bot_id)
        allowed_dirs.add(bot_dir)
        current = store.get_current_bot_version(bot_id)
        if (
            not current
            or int(current.get("version") or 0) != int(bot.get("current_version") or 0)
            or current.get("binary_path") != bot.get("binary_path")
            or current.get("runtime_mode") != "longrunning"
        ):
            raise ShowcaseSeedError(f"专用演示 Bot #{bot_id} 当前版本镜像异常")
        for version in versions:
            version_dir = bot_dir / f"v{int(version['version'])}"
            expected_binary = version_dir / "bot.bin"
            configured = Path(str(version.get("binary_path") or "")).expanduser()
            if not configured.is_absolute() or Path(os.path.abspath(configured)) != expected_binary:
                raise ShowcaseSeedError(
                    f"专用演示 Bot #{bot_id} 版本路径不属于 canonical namespace"
                )
            allowed_dirs.add(version_dir)
            allowed_files.add(expected_binary)

    actual_dirs = {root}
    actual_files: set[Path] = set()

    def visit(directory: Path) -> None:
        for child in directory.iterdir():
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ShowcaseSeedError(f"演示 Bot 目录含符号链接: {child}")
            if stat.S_ISDIR(mode):
                actual_dirs.add(child)
                visit(child)
            elif stat.S_ISREG(mode):
                actual_files.add(child)
            else:
                raise ShowcaseSeedError(f"演示 Bot 目录含非法文件类型: {child}")

    visit(root)
    if actual_dirs != allowed_dirs or actual_files != allowed_files:
        unexpected = sorted(
            str(path)
            for path in (actual_dirs - allowed_dirs) | (actual_files - allowed_files)
        )
        missing = sorted(
            str(path)
            for path in (allowed_dirs - actual_dirs) | (allowed_files - actual_files)
        )
        raise ShowcaseSeedError(
            "演示 Bot namespace 与数据库版本白名单不一致"
            f"（unexpected={unexpected}, missing={missing}）"
        )
    return {"bots": len(bots), "files": len(actual_files) - 1}


def _validate_showcase_upload_rollback_scope(
    store: Store,
    upload_root: Path,
) -> dict[str, int]:
    """Prove every existing namespace object is seed-owned; allow missing files.

    Rollback is a recovery operation, so a partially written or manually lost
    expected binary must not make cleanup impossible.  Unknown files,
    symlinks, non-canonical DB paths and an invalid marker remain fatal.
    """
    root = upload_root.expanduser()
    if not root.is_absolute() or root.name != SHOWCASE_UPLOAD_BASENAME:
        raise ShowcaseSeedError(
            f"演示 Bot 目录必须是绝对的 {SHOWCASE_UPLOAD_BASENAME}"
        )
    if root.is_symlink():
        raise ShowcaseSeedError("演示 Bot 目录不得为符号链接")
    root = root.resolve()
    if not root.is_dir():
        raise ShowcaseSeedError("演示 Bot 目录不存在或缺少 namespace marker")

    marker = root / SHOWCASE_UPLOAD_MARKER
    if not marker.exists():
        raise ShowcaseSeedError("演示 Bot 目录缺少 namespace marker")
    marker_mode = marker.lstat().st_mode
    if (
        stat.S_ISLNK(marker_mode)
        or not stat.S_ISREG(marker_mode)
        or marker.read_text(encoding="utf-8") != SHOWCASE_UPLOAD_MARKER_CONTENT
    ):
        raise ShowcaseSeedError("演示 Bot namespace marker 非法")

    _organizer, _players, bots = _reserved_identity_graph(
        store, require_complete=False
    )
    allowed_dirs = {root}
    allowed_files = {marker}
    for bot in bots:
        bot_id = int(bot["id"])
        bot_dir = root / str(bot_id)
        allowed_dirs.add(bot_dir)
        version_paths: set[Path] = set()
        for version in store.list_bot_versions(bot_id):
            version_dir = bot_dir / f"v{int(version['version'])}"
            expected_binary = version_dir / "bot.bin"
            configured = Path(str(version.get("binary_path") or "")).expanduser()
            if (
                not configured.is_absolute()
                or Path(os.path.abspath(configured)) != expected_binary
            ):
                raise ShowcaseSeedError(
                    f"专用演示 Bot #{bot_id} 版本路径不属于 canonical namespace"
                )
            allowed_dirs.add(version_dir)
            allowed_files.add(expected_binary)
            version_paths.add(expected_binary)
        configured_bot = str(bot.get("binary_path") or "")
        if configured_bot:
            bot_path = Path(os.path.abspath(Path(configured_bot).expanduser()))
            if bot_path not in version_paths:
                current_version = int(bot.get("current_version") or 0)
                expected_current = (
                    bot_dir / f"v{current_version}" / "bot.bin"
                    if current_version > 0
                    else None
                )
                if expected_current is None or bot_path != expected_current:
                    raise ShowcaseSeedError(
                        f"专用演示 Bot #{bot_id} 当前路径不属于版本白名单"
                    )
                # The bot_versions row may already be missing after a partial
                # cleanup/corruption.  Its canonical current mirror is still
                # safely scoped to this reserved Bot directory.
                allowed_dirs.add(expected_current.parent)
                allowed_files.add(expected_current)

    actual_dirs = {root}
    actual_files: set[Path] = set()

    def visit(directory: Path) -> None:
        for child in directory.iterdir():
            mode = child.lstat().st_mode
            if stat.S_ISLNK(mode):
                raise ShowcaseSeedError(f"演示 Bot 目录含符号链接: {child}")
            if stat.S_ISDIR(mode):
                actual_dirs.add(child)
                visit(child)
            elif stat.S_ISREG(mode):
                actual_files.add(child)
            else:
                raise ShowcaseSeedError(f"演示 Bot 目录含非法文件类型: {child}")

    visit(root)
    unexpected = (actual_dirs - allowed_dirs) | (actual_files - allowed_files)
    if unexpected:
        raise ShowcaseSeedError(
            "演示 Bot rollback namespace 含非白名单对象: "
            f"{sorted(str(path) for path in unexpected)}"
        )
    return {
        "bots": len(bots),
        "existing_files": len(actual_files) - 1,
        "missing_files": len(allowed_files - actual_files),
    }


def _deactivate_showcase_bots(store: Store, *, strict: bool) -> None:
    """Keep synthetic Bots readable by id but ineligible for queues/rankings."""
    try:
        _organizer, _players, bots = _reserved_identity_graph(
            store, require_complete=strict
        )
        for bot in bots:
            if bot.get("is_active"):
                store.update_bot(int(bot["id"]), is_active=0)
    except Exception:
        if strict:
            raise
        logger.exception("failed to deactivate validated showcase Bots during cleanup")


def _deactivate_tracked_bots(store: Store, bot_ids: set[int]) -> None:
    """Best-effort cleanup for Bots activated by this exact seed invocation."""
    for bot_id in sorted(bot_ids):
        try:
            bot = store.get_bot(bot_id)
            if bot and bot.get("is_active"):
                store.update_bot(bot_id, is_active=0)
        except Exception:
            logger.exception("failed to deactivate tracked showcase Bot %s", bot_id)


def _verify_match_replay_quality(store: Store, match: dict[str, Any]) -> None:
    """Require a clean, canonical Gomoku result suitable for customer demos."""
    match_id = str(match["id"])
    if int(match.get("technical_loss") or 0) != 0:
        raise ShowcaseSeedError(f"演示对局含技术判负: {match_id}")
    reason = match.get("reason")
    if reason not in {"five", "draw"}:
        raise ShowcaseSeedError(f"演示对局终局原因不适合展示: {match_id}")
    replay = store.get_replay(match_id)
    try:
        events = json.loads((replay or {}).get("events_json") or "[]")
    except (TypeError, ValueError) as exc:
        raise ShowcaseSeedError(f"演示对局回放 JSON 损坏: {match_id}") from exc
    if not isinstance(events, list) or not events:
        raise ShowcaseSeedError(f"演示对局缺少真实回放: {match_id}")
    allowed_types = {"match_start", "turn", "move", "match_end"}
    if any(
        not isinstance(event, dict) or event.get("type") not in allowed_types
        for event in events
    ):
        raise ShowcaseSeedError(f"演示对局回放含非法/故障事件: {match_id}")
    terminal_indices = [
        index for index, event in enumerate(events)
        if event.get("type") == "match_end"
    ]
    if terminal_indices != [len(events) - 1]:
        raise ShowcaseSeedError(f"演示对局必须仅有一个末尾 canonical match_end: {match_id}")
    terminal = events[-1]
    result = match.get("result")
    if (
        terminal.get("reason") != reason
        or terminal.get("winner") != match.get("winner")
        or not isinstance(result, dict)
        or terminal.get("deltas") != result.get("deltas")
        or not any(event.get("type") == "move" for event in events)
    ):
        raise ShowcaseSeedError(f"演示对局终局事件与数据库不一致: {match_id}")


def _verify_pairing_identity_graph(
    store: Store,
    contest_id: int,
    entries: list[dict[str, Any]],
    pairings: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    expected_user_bot: dict[int, int],
) -> None:
    """Prove entry → Bot → version → physical match seat identity exactly."""
    expected_by_entry: dict[int, int] = {}
    for entry in entries:
        user_id = int(entry.get("user_id") or 0)
        expected_bot = expected_user_bot.get(user_id)
        if expected_bot is None or int(entry.get("bot_id") or 0) != expected_bot:
            raise ShowcaseSeedError(f"赛事 #{contest_id} entry/Bot 身份错绑")
        expected_by_entry[int(entry["id"])] = expected_bot

    match_by_id = {str(match["id"]): match for match in matches}
    for pairing in pairings:
        side_values: list[tuple[str, int, int, int]] = []
        for side in ("a", "b"):
            entry_id = int(pairing.get(f"entry_{side}_id") or 0)
            bot_id = int(pairing.get(f"bot_{side}_id") or 0)
            expected_bot = expected_by_entry.get(entry_id)
            if expected_bot is None or bot_id != expected_bot:
                raise ShowcaseSeedError(
                    f"赛事 #{contest_id} pairing #{pairing['id']} entry/{side.upper()} Bot 错绑"
                )
            version_id = int(pairing.get(f"bot_{side}_version_id") or 0)
            version = store.get_bot_version(version_id) if version_id else None
            if not version or int(version.get("bot_id") or 0) != bot_id:
                raise ShowcaseSeedError(
                    f"赛事 #{contest_id} pairing #{pairing['id']} 冻结版本错绑"
                )
            side_values.append((side, bot_id, version_id, entry_id))

        match_id = pairing.get("match_id")
        if not match_id:
            continue
        match = match_by_id.get(str(match_id))
        if not match:
            raise ShowcaseSeedError(
                f"赛事 #{contest_id} pairing #{pairing['id']} 指向缺失 match"
            )
        config = match.get("match_config")
        if not isinstance(config, dict):
            raise ShowcaseSeedError(f"演示对局冻结版本配置损坏: {match_id}")
        for side, bot_id, version_id, _entry_id in side_values:
            if (
                int(match.get(f"bot_{side}_id") or 0) != bot_id
                or int(config.get(f"_bot_{side}_version_id") or 0) != version_id
            ):
                raise ShowcaseSeedError(
                    f"赛事 #{contest_id} pairing/match {side.upper()} 座位或版本错绑"
                )


def _verify_pairing_rollback_scope(
    store: Store,
    contest_id: int,
    entries: list[dict[str, Any]],
    pairings: list[dict[str, Any]],
    matches: list[dict[str, Any]],
    expected_user_bot: dict[int, int],
) -> None:
    """Verify existing rollback objects while tolerating already-deleted rows.

    A previous rollback may have committed one match deletion before exiting.
    Missing match/version rows are therefore recovery state, not evidence of a
    foreign object.  Every row that still exists must remain exactly bound to
    the dedicated entry/Bot graph.
    """
    expected_by_entry: dict[int, int] = {}
    for entry in entries:
        expected_bot = expected_user_bot.get(int(entry.get("user_id") or 0))
        if expected_bot is None or int(entry.get("bot_id") or 0) != expected_bot:
            raise ShowcaseSeedError(f"赛事 #{contest_id} entry/Bot 身份错绑")
        expected_by_entry[int(entry["id"])] = expected_bot

    match_by_id = {str(match["id"]): match for match in matches}
    for pairing in pairings:
        sides: list[tuple[str, int, int]] = []
        for side in ("a", "b"):
            entry_id = int(pairing.get(f"entry_{side}_id") or 0)
            bot_id = int(pairing.get(f"bot_{side}_id") or 0)
            if expected_by_entry.get(entry_id) != bot_id:
                raise ShowcaseSeedError(
                    f"赛事 #{contest_id} pairing #{pairing['id']} "
                    f"entry/{side.upper()} Bot 错绑"
                )
            version_id = int(pairing.get(f"bot_{side}_version_id") or 0)
            version = store.get_bot_version(version_id) if version_id else None
            if version is not None and int(version.get("bot_id") or 0) != bot_id:
                raise ShowcaseSeedError(
                    f"赛事 #{contest_id} pairing #{pairing['id']} 冻结版本错绑"
                )
            sides.append((side, bot_id, version_id))

        match_id = pairing.get("match_id")
        if not match_id:
            continue
        match = match_by_id.get(str(match_id))
        if match is None:
            continue
        config = match.get("match_config")
        if not isinstance(config, dict):
            raise ShowcaseSeedError(f"演示对局冻结版本配置损坏: {match_id}")
        for side, bot_id, version_id in sides:
            if int(match.get(f"bot_{side}_id") or 0) != bot_id:
                raise ShowcaseSeedError(
                    f"赛事 #{contest_id} pairing/match {side.upper()} 座位错绑"
                )
            if version_id and int(config.get(f"_bot_{side}_version_id") or 0) != version_id:
                raise ShowcaseSeedError(
                    f"赛事 #{contest_id} pairing/match {side.upper()} 版本错绑"
                )


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
            and hashlib.sha256(path.read_bytes()).hexdigest() == checksum
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
    profile_dir: Path,
    *,
    emit: Callable[[str], None] = _log_emit,
    activated_bot_ids: set[int] | None = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    profile_binaries = _load_showcase_profile_binaries(profile_dir)
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
        profile_name, profile = _profile_for_player(index)
        raw = profile_binaries[profile_name]
        checksum = profile["checksum"]
        name = f"{BOT_PREFIX}{index:02d}"
        profile_marker = (
            f"策略档位：{profile['label']} ({profile_name})；"
            f"策略版本：{SHOWCASE_STRATEGY_VERSION}"
        )
        bot = store.get_bot_by_owner_name(int(player["id"]), name)
        if bot is None:
            bot = manager.create_from_upload(
                int(player["id"]),
                name,
                raw,
                display_name=f"演示棋手 {index:02d}",
                description=f"合成演示 LongRunning 五子棋 Bot；{profile_marker}",
                upload_note=f"{SEED_VERSION} canonical profile={profile_name}",
                game_id=SHOWCASE_GAME_ID,
                runtime_mode="longrunning",
                binary_runner=preflight,
            )
            if activated_bot_ids is not None:
                activated_bot_ids.add(int(bot["id"]))
            emit(f"Bot {index:02d}/12：已创建 #{bot['id']}")
        else:
            if bot.get("game_id") != SHOWCASE_GAME_ID or int(bot.get("owner_id") or 0) != int(player["id"]):
                raise ShowcaseSeedError(f"专用演示 Bot 冲突: {name}")
            if not _canonical_version(store, bot, upload_root, checksum):
                bot = manager.upload_version(
                    int(bot["id"]),
                    int(player["id"]),
                    raw,
                    upload_note=(
                        f"{SEED_VERSION} canonical refresh profile={profile_name}"
                    ),
                    runtime_mode="longrunning",
                    binary_runner=preflight,
                )
                emit(f"Bot {index:02d}/12：已刷新 canonical LongRunning 版本")
            if profile_marker not in str(bot.get("description") or ""):
                bot = store.update_bot(
                    int(bot["id"]),
                    description=(
                        "合成演示 LongRunning 五子棋 Bot；"
                        f"{profile_marker}"
                    ),
                )
            if not bot.get("is_active"):
                bot = manager.set_active(int(bot["id"]), int(player["id"]), True)
                if activated_bot_ids is not None:
                    activated_bot_ids.add(int(bot["id"]))
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


def _showcase_seed_records(store: Store) -> list[dict[str, Any]]:
    return [
        contest
        for contest in store.list_contests()
        if (
        contest.get("showcase_key") in SHOWCASE_KEYS
        or any(
            _marker(key) in str(contest.get("description") or "")
            for key in SHOWCASE_KEYS
        )
        )
    ]


def _has_showcase_seed_records(store: Store) -> bool:
    return bool(_showcase_seed_records(store))


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


def _create_historical_showcase_contest(
    manager: ContestManager,
    key: str,
    organizer_id: int,
    *,
    starts_at: str | None,
) -> dict[str, Any]:
    """Create only the isolated, soon-to-be-frozen historical showcase graph.

    The named Gomoku KO template remains readable for old contests but is not a
    product creation option because a drawn elimination match has no sourced
    tie-break rule.  Showcase snapshots intentionally preserve that historical
    lifecycle, so they persist its validated stage snapshot through the low-level
    Store API instead of weakening ``ContestManager.create`` for real contests.
    """
    from bzplat.backend.contests.templates import get_template
    from bzplat.backend.contests.validation import validate_stage

    template = get_template(HISTORICAL_SHOWCASE_TEMPLATE_ID)
    if template is None or template.get("creation_enabled", True) is not False:
        raise ShowcaseSeedError("历史演示模板契约异常")
    raw_stages = (
        _running_stages()
        if key == "contest_lifecycle_running"
        else template["stages"]
    )
    stages = [
        validate_stage(stage, idx, SHOWCASE_GAME_ID)
        for idx, stage in enumerate(raw_stages)
    ]
    return manager.store.create_contest(
        TITLE[key],
        organizer_id,
        description=_description(key),
        status=CONTEST_DRAFT,
        template_id=HISTORICAL_SHOWCASE_TEMPLATE_ID,
        game_id=SHOWCASE_GAME_ID,
        stages_json=json.dumps(stages, ensure_ascii=False),
        require_real_name=1 if key == "contest_lifecycle_open" else 0,
        starts_at=starts_at,
    )


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
    )


def _delete_interrupted_seed_match(
    store: Store, contest_id: int, match_id: str
) -> bool:
    """Delete only an aborted match proven to belong to this seed contest."""
    match = store.get_match(match_id)
    if not match:
        return False
    if (
        int(match.get("contest_id") or 0) != contest_id
        or match.get("game_id") != SHOWCASE_GAME_ID
        or match.get("match_type") != TYPE_CONTEST
        or match.get("status") != STATUS_ABORTED
    ):
        raise ShowcaseSeedError(f"拒绝删除归属异常的中断演示对局: {match_id}")
    return store.delete_match(match_id)


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
        matches = manager.store.list_matches(
            limit=1000, contest_id=contest_id
        )
        for match in matches:
            match_id = str(match["id"])
            if (
                match.get("game_id") != SHOWCASE_GAME_ID
                or match.get("match_type") != TYPE_CONTEST
            ):
                raise ShowcaseSeedError(
                    f"演示生成命名空间含错误游戏/类型对局: {match_id}"
                )
            if match.get("status") in (STATUS_PENDING, STATUS_RUNNING):
                match = manager.store.abort_match_if_active(
                    match_id, reason="showcase_seed_interrupted"
                ) or match
                recovered += 1
            if match.get("status") != STATUS_ABORTED:
                continue
            if manager.store.reset_aborted_contest_pairing(contest_id, match_id):
                recovered += 1
            # A seed snapshot has no intentional aborted history.  Once an
            # exact pairing is unbound (or prepare never bound it), remove the
            # physical row/index/replay so exact graph cardinality can recover.
            recovered += int(
                _delete_interrupted_seed_match(
                    manager.store, contest_id, match_id
                )
            )

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
                match_id = str(pairing["match_id"])
                reset = manager.store.reset_aborted_contest_pairing(
                    contest_id, match_id
                )
                recovered += int(bool(reset))
                if reset:
                    recovered += int(
                        _delete_interrupted_seed_match(
                            manager.store, contest_id, match_id
                        )
                    )

        # Do not use the startup safe reconciler here: it deliberately retries
        # every pending row and historically ignored future scheduled_at.  The
        # seed drives only its target contest and always goes through the normal
        # dispatcher, whose starts_at/scheduled_at gates are authoritative.
        await manager.maybe_finish(contest_id)
        latest = manager.store.get_contest(contest_id)
        if latest and latest.get("status") in (CONTEST_PUBLISHED, CONTEST_RUNNING):
            await manager._dispatch_pending(
                contest_id, int(latest.get("current_stage_idx") or 0)
            )
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
        if key == "contest_lifecycle_running":
            from datetime import datetime

            starts_at = datetime.now().isoformat(timespec="seconds")
        existing = _create_historical_showcase_contest(
            manager,
            key,
            int(organizer["id"]),
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
    profile_dir: Path,
    *,
    max_concurrent: int = 2,
    timeout_per_contest: float = 1800,
    emit: Callable[[str], None] = _log_emit,
) -> dict[str, Any]:
    store = Store(str(db_path))
    orch: MatchOrchestrator | None = None
    activated_bot_ids: set[int] = set()
    try:
        validate_showcase_upload_namespace(store, upload_root, create=True)
        if all(store.get_contest_by_showcase_key(key) for key in SHOWCASE_KEYS):
            # The common rerun path must not re-upload/preflight/activate all
            # Bots.  Normalize the one permitted mutable bit, then verify the
            # complete frozen graph and return immediately.
            _deactivate_showcase_bots(store, strict=True)
            validate_showcase_upload_namespace(
                store, upload_root, require_complete=True
            )
            emit("六个演示快照已完整：跳过 Bot provisioning，仅执行严格验收")
            return verify_showcases(store, upload_root)
        if _has_showcase_seed_records(store):
            # Frozen pairings retain exact Bot versions.  Updating binaries in
            # place would mix the old strategy with this manifest, so a
            # strategy-version change must be rolled back and reseeded fresh.
            try:
                expected_by_bot = _verify_showcase_profile_quality(
                    store, upload_root
                )
                partial_integrity = {
                    "showcases": {
                        str(contest["id"]): {"contest_id": int(contest["id"])}
                        for contest in _showcase_seed_records(store)
                    }
                }
                _verify_frozen_profile_versions(
                    store, upload_root, partial_integrity, expected_by_bot
                )
            except ShowcaseSeedError as exc:
                raise ShowcaseSeedError(
                    "检测到使用旧策略 manifest 的未完成演示图；"
                    "请先执行 rollback，再用当前 profile 重新 seed"
                ) from exc
        organizer, players, bots = provision_showcase_identities(
            store,
            upload_root,
            profile_dir,
            emit=emit,
            activated_bot_ids=activated_bot_ids,
        )
        validate_showcase_upload_namespace(
            store, upload_root, require_complete=True
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
        _deactivate_showcase_bots(store, strict=True)
        validate_showcase_upload_namespace(
            store, upload_root, require_complete=True
        )
        return verify_showcases(store, upload_root)
    finally:
        if orch is not None:
            await orch.shutdown()
        # This cleanup must run before graph-wide validation: a later reserved
        # identity may be corrupt, but that must not strand an earlier Bot that
        # this invocation successfully activated.
        _deactivate_tracked_bots(store, activated_bot_ids)
        _deactivate_showcase_bots(store, strict=False)
        store.close()


def _verify_showcase_integrity(store: Store, upload_root: Path) -> dict[str, Any]:
    """Strictly verify the complete identity, filesystem and frozen DB graph."""
    organizer, players, bots = _reserved_identity_graph(
        store, require_complete=True
    )
    assert organizer is not None
    validate_showcase_upload_namespace(
        store, upload_root, require_complete=True
    )
    if any(bool(bot.get("is_active")) for bot in bots):
        raise ShowcaseSeedError("专用演示 Bot 必须全部保持停用")
    expected_user_bot = {
        int(player["id"]): int(bot["id"])
        for player, bot in zip(players, bots)
    }
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
        if contest.get("title") != TITLE[key]:
            raise ShowcaseSeedError(f"{key} 标题不符合 seed 契约")
        if contest.get("game_id") != SHOWCASE_GAME_ID:
            raise ShowcaseSeedError(f"{key} 游戏不是 {SHOWCASE_GAME_ID}")
        if contest.get("template_id") != HISTORICAL_SHOWCASE_TEMPLATE_ID:
            raise ShowcaseSeedError(f"{key} 赛制模板异常")
        description = str(contest.get("description") or "")
        present_markers = [
            marker_key
            for marker_key in SHOWCASE_KEYS
            if _marker(marker_key) in description
        ]
        if present_markers != [key] or description.count(_marker(key)) != 1:
            raise ShowcaseSeedError(f"{key} 缺少唯一 seed marker")
        if int(contest.get("organizer_id") or 0) != int(organizer["id"]):
            raise ShowcaseSeedError(f"{key} 组织者不属于专用演示身份")
        cid = int(contest["id"])
        entries = store.list_contest_entries(cid)
        pairings = store.list_contest_pairings(cid)
        matches = store.list_matches(limit=1000, contest_id=cid)
        if len(entries) != ENTRY_COUNT[key]:
            raise ShowcaseSeedError(f"{key} 名册数量异常")
        expected_pairs = set(list(expected_user_bot.items())[: ENTRY_COUNT[key]])
        actual_pairs = {
            (int(entry.get("user_id") or 0), int(entry.get("bot_id") or 0))
            for entry in entries
        }
        if actual_pairs != expected_pairs:
            raise ShowcaseSeedError(f"{key} 名册不是专用演示身份/Bot")
        expected_pairings, expected_matches = expected_graph[key]
        if len(pairings) != expected_pairings or len(matches) != expected_matches:
            raise ShowcaseSeedError(
                f"{key} 图规模异常: pairings={len(pairings)}, matches={len(matches)}"
            )
        if any(match.get("status") in (STATUS_PENDING, STATUS_RUNNING) for match in matches):
            raise ShowcaseSeedError(f"{key} 仍有活跃 match")
        if any(match.get("status") != STATUS_COMPLETED for match in matches):
            raise ShowcaseSeedError(f"{key} 含非 completed 历史 match")
        if any(
            match.get("game_id") != SHOWCASE_GAME_ID
            or match.get("match_type") != TYPE_CONTEST
            or int(match.get("contest_id") or 0) != cid
            for match in matches
        ):
            raise ShowcaseSeedError(f"{key} 含错误游戏/类型/归属的 match")
        _verify_pairing_identity_graph(
            store, cid, entries, pairings, matches, expected_user_bot
        )
        match_ids = {str(match["id"]) for match in matches}
        bound_ids = {
            str(pairing["match_id"])
            for pairing in pairings
            if pairing.get("match_id")
        }
        if bound_ids != match_ids:
            raise ShowcaseSeedError(f"{key} pairing/match 绑定集合不一致")
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
    return {
        "seed_version": SEED_VERSION,
        "showcases": contests,
        "total_showcase_matches": len(all_match_ids),
        "integrity_verified": True,
    }


def _verify_showcase_profile_quality(
    store: Store,
    upload_root: Path,
) -> dict[int, dict[str, Any]]:
    """Require the exact reviewed profile assignment used by the current seed."""
    _organizer, players, bots = _reserved_identity_graph(
        store, require_complete=True
    )
    expected_by_bot: dict[int, dict[str, Any]] = {}
    for index, (player, bot) in enumerate(zip(players, bots), 1):
        profile_name, profile = _profile_for_player(index)
        profile_marker = (
            f"策略档位：{profile['label']} ({profile_name})；"
            f"策略版本：{SHOWCASE_STRATEGY_VERSION}"
        )
        if (
            int(bot.get("owner_id") or 0) != int(player["id"])
            or profile_marker not in str(bot.get("description") or "")
            or not _canonical_version(
                store, bot, upload_root, profile["checksum"]
            )
        ):
            raise ShowcaseSeedError(
                f"演示 Bot {index:02d} 未使用审核锁定的 {profile_name} profile；"
                "旧策略数据请先 rollback 后重新 seed"
            )
        expected_by_bot[int(bot["id"])] = profile
    return expected_by_bot


def _verify_frozen_profile_versions(
    store: Store,
    upload_root: Path,
    integrity: dict[str, Any],
    expected_by_bot: dict[int, dict[str, Any]],
) -> None:
    """Bind every actual pairing seat to its reviewed manifest artifact."""
    root = upload_root.resolve()
    for item in integrity["showcases"].values():
        contest_id = int(item["contest_id"])
        for pairing in store.list_contest_pairings(contest_id):
            for side in ("a", "b"):
                bot_id = int(pairing.get(f"bot_{side}_id") or 0)
                version_id = int(pairing.get(f"bot_{side}_version_id") or 0)
                profile = expected_by_bot.get(bot_id)
                version = store.get_bot_version(version_id) if version_id else None
                if profile is None or version is None:
                    raise ShowcaseSeedError(
                        f"赛事 #{contest_id} pairing #{pairing['id']} "
                        f"{side.upper()} 缺少 manifest 冻结版本"
                    )
                expected_path = (
                    root / str(bot_id) / f"v{int(version['version'])}" / "bot.bin"
                )
                configured = Path(str(version.get("binary_path") or "")).expanduser()
                if (
                    int(version.get("bot_id") or 0) != bot_id
                    or version.get("runtime_mode") != "longrunning"
                    or version.get("checksum") != profile["checksum"]
                    or int(version.get("size_bytes") or -1) != int(profile["size"])
                    or not configured.is_absolute()
                    or Path(os.path.abspath(configured)) != expected_path
                    or configured.is_symlink()
                    or not configured.is_file()
                    or configured.stat().st_size != int(profile["size"])
                    or hashlib.sha256(configured.read_bytes()).hexdigest()
                    != profile["checksum"]
                ):
                    raise ShowcaseSeedError(
                        f"赛事 #{contest_id} pairing #{pairing['id']} "
                        f"{side.upper()} 冻结版本不属于审核 manifest"
                    )


def _verify_group_stage_distribution(
    store: Store,
    contest_id: int,
    *,
    label: str,
) -> None:
    rows = store.list_stage_results(contest_id, stage_idx=0)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("group_id") or ""), []).append(row)
    if set(grouped) != {"G1", "G2", "G3", "G4"}:
        raise ShowcaseSeedError(f"{label} 小组结果必须完整覆盖 G1..G4")
    for group_id, group_rows in grouped.items():
        points = sorted(float(row.get("points") or 0) for row in group_rows)
        ranks = sorted(int(row.get("rank_in_group") or 0) for row in group_rows)
        if points != [0.0, 4.0, 8.0] or ranks != [1, 2, 3]:
            raise ShowcaseSeedError(
                f"{label} {group_id} 应稳定形成 8/4/0 分与连续组内名次，"
                f"实际积分={points}"
            )


def _normalized_replay_trajectory(
    store: Store,
    match_id: str,
) -> tuple[tuple[tuple[int, int, int], ...], int | None, str | None]:
    replay = store.get_replay(match_id)
    events = json.loads((replay or {}).get("events_json") or "[]")
    moves = tuple(
        (int(event["player"]), int(event["x"]), int(event["y"]))
        for event in events
        if event.get("type") == "move"
    )
    terminal = events[-1]
    return moves, terminal.get("winner"), terminal.get("reason")


def _verify_cross_snapshot_trajectories(
    store: Store,
    integrity: dict[str, Any],
) -> None:
    """The same ordered group pairing must replay identically in every snapshot."""

    def trajectories(key: str) -> dict[tuple[int, int], tuple[Any, ...]]:
        contest_id = int(integrity["showcases"][key]["contest_id"])
        output: dict[tuple[int, int], tuple[Any, ...]] = {}
        for pairing in store.list_contest_pairings(contest_id, stage_idx=0):
            match_id = pairing.get("match_id")
            if not match_id:
                continue
            ordered_pair = (
                int(pairing.get("bot_a_id") or 0),
                int(pairing.get("bot_b_id") or 0),
            )
            if ordered_pair in output:
                raise ShowcaseSeedError(
                    f"{key} 小组赛重复有序 Bot 对: {ordered_pair}"
                )
            output[ordered_pair] = _normalized_replay_trajectory(
                store, str(match_id)
            )
        return output

    baseline = trajectories("contest_lifecycle_finished")
    if len(baseline) != 24:
        raise ShowcaseSeedError("finished 小组赛缺少 24 条唯一有序对轨迹")
    for key, expected_count in (
        ("contest_lifecycle_running", 4),
        ("contest_lifecycle_rest", 24),
    ):
        current = trajectories(key)
        if len(current) != expected_count:
            raise ShowcaseSeedError(f"{key} 真实轨迹数量异常")
        for ordered_pair, trajectory in current.items():
            if baseline.get(ordered_pair) != trajectory:
                raise ShowcaseSeedError(
                    f"{key} 有序 Bot 对 {ordered_pair} 的真实轨迹不可复现"
                )

    finished_id = int(
        integrity["showcases"]["contest_lifecycle_finished"]["contest_id"]
    )
    knockout = store.list_contest_pairings(finished_id, stage_idx=1)
    if len(knockout) != 7:
        raise ShowcaseSeedError("finished 淘汰赛必须包含 7 场真实决胜")
    for pairing in knockout:
        match = store.get_match(str(pairing.get("match_id") or ""))
        if not match or match.get("reason") != "five" or match.get("winner") not in (0, 1):
            raise ShowcaseSeedError("finished 淘汰赛不得以平局或无胜者结束")


def _verify_showcase_presentation_quality(
    store: Store,
    upload_root: Path,
    integrity: dict[str, Any],
) -> None:
    """Apply strict, non-destructive customer-demo quality gates."""
    expected_by_bot = _verify_showcase_profile_quality(store, upload_root)
    _verify_frozen_profile_versions(
        store, upload_root, integrity, expected_by_bot
    )
    for item in integrity["showcases"].values():
        for match in store.list_matches(
            limit=1000, contest_id=int(item["contest_id"])
        ):
            _verify_match_replay_quality(store, match)
    for key in ("contest_lifecycle_rest", "contest_lifecycle_finished"):
        _verify_group_stage_distribution(
            store,
            int(integrity["showcases"][key]["contest_id"]),
            label=key,
        )
    _verify_cross_snapshot_trajectories(store, integrity)


def verify_showcases(store: Store, upload_root: Path) -> dict[str, Any]:
    """Strict operator verification: graph integrity plus presentation quality."""
    result = _verify_showcase_integrity(store, upload_root)
    _verify_showcase_presentation_quality(store, upload_root, result)
    return {
        **result,
        "presentation_quality_verified": True,
        "verified": True,
    }


def rollback_showcases(
    store: Store,
    upload_root: Path,
    *,
    emit: Callable[[str], None] = _log_emit,
) -> dict[str, int]:
    organizer = store.get_user_by_username(ORGANIZER_USERNAME)
    if not organizer:
        return {"contests": 0, "matches": 0, "bots": 0, "users": 0}
    if (
        organizer.get("email") != "showcase-organizer@invalid.example"
        or organizer.get("role") != ROLE_ORGANIZER
    ):
        raise ShowcaseSeedError("专用演示组织者账号身份不匹配，拒绝回滚")
    _validated_organizer, validated_players, validated_bots = _reserved_identity_graph(
        store, require_complete=False
    )
    _validate_showcase_upload_rollback_scope(store, upload_root)
    frozen = {
        key: store.get_contest_by_showcase_key(key) for key in SHOWCASE_KEYS
    }
    for key, contest in frozen.items():
        if contest and int(contest.get("organizer_id") or 0) != int(organizer["id"]):
            raise ShowcaseSeedError(f"{key} 不属于专用演示组织者，拒绝回滚")
    validated_user_ids = {int(player["id"]) for player in validated_players}
    validated_bot_ids = {int(bot["id"]) for bot in validated_bots}
    expected_user_bot = {
        int(bot["owner_id"]): int(bot["id"]) for bot in validated_bots
    }
    allowed_keys = set(SHOWCASE_KEYS)
    all_marked_contests = [
        contest
        for contest in store.list_contests()
        if contest.get("showcase_key") in allowed_keys
        or any(
            _marker(key) in str(contest.get("description") or "")
            for key in allowed_keys
        )
    ]
    if any(
        int(contest.get("organizer_id") or 0) != int(organizer["id"])
        for contest in all_marked_contests
    ):
        raise ShowcaseSeedError("演示 key/marker 被非专用组织者占用，拒绝回滚")
    candidates = [
        contest
        for contest in store.list_contests(organizer_id=int(organizer["id"]))
        if contest.get("showcase_key") in allowed_keys
        or any(_marker(key) in str(contest.get("description") or "") for key in allowed_keys)
    ]
    seen_marker_keys: set[str] = set()
    candidate_match_ids: dict[int, tuple[str, ...]] = {}
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
        marker_key = marker_keys[0]
        if marker_key in seen_marker_keys:
            raise ShowcaseSeedError(f"演示 key 重复: {marker_key}")
        seen_marker_keys.add(marker_key)
        if contest.get("title") != TITLE[marker_key]:
            raise ShowcaseSeedError(f"赛事 #{contest['id']} 标题不符合 seed 契约")
        if (
            contest.get("game_id") != SHOWCASE_GAME_ID
            or contest.get("template_id") != HISTORICAL_SHOWCASE_TEMPLATE_ID
        ):
            raise ShowcaseSeedError(f"赛事 #{contest['id']} 游戏/模板异常")
        if key is not None and contest.get("status") != TARGET_STATUS[marker_key]:
            raise ShowcaseSeedError(f"赛事 #{contest['id']} 冻结状态异常")
        entries = store.list_contest_entries(int(contest["id"]))
        if any(
            int(entry.get("user_id") or 0) not in validated_user_ids
            or int(entry.get("bot_id") or 0) not in validated_bot_ids
            for entry in entries
        ):
            raise ShowcaseSeedError(f"赛事 #{contest['id']} 含非专用演示名册")
        if key is not None and len(entries) != ENTRY_COUNT[marker_key]:
            raise ShowcaseSeedError(f"赛事 #{contest['id']} 冻结名册数量异常")
        pairings = store.list_contest_pairings(int(contest["id"]))
        matches = store.list_matches(limit=1000, contest_id=int(contest["id"]))
        if any(
            match.get("game_id") != SHOWCASE_GAME_ID
            or match.get("match_type") != TYPE_CONTEST
            or int(match.get("contest_id") or 0) != int(contest["id"])
            or int(match.get("owner_id") or 0) != int(organizer["id"])
            or int(match.get("bot_a_id") or 0) not in validated_bot_ids
            or int(match.get("bot_b_id") or 0) not in validated_bot_ids
            for match in matches
        ):
            raise ShowcaseSeedError(f"赛事 #{contest['id']} 含非预期身份/游戏/类型对局")
        _verify_pairing_rollback_scope(
            store,
            int(contest["id"]),
            entries,
            pairings,
            matches,
            expected_user_bot,
        )
        if any(match.get("status") in (STATUS_PENDING, STATUS_RUNNING) for match in matches):
            raise ShowcaseSeedError(f"赛事 #{contest['id']} 仍有活跃对局，拒绝回滚")
        candidate_match_ids[int(contest["id"])] = tuple(
            str(match["id"]) for match in matches
        )

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
        bot_root = resolved_upload_root / str(bot["id"])
        versions = store.list_bot_versions(int(bot["id"]))
        if any(
            Path(os.path.abspath(str(version.get("binary_path") or ""))).parent.parent
            != bot_root
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
    if any(
        (
            int(match.get("owner_id") or 0) in player_ids
            or int(match.get("human_user_id") or 0) in player_ids
        )
        and int(match.get("contest_id") or 0) not in candidate_ids
        for match in store.list_matches(limit=100000)
    ):
        raise ShowcaseSeedError("专用演示账号存在非白名单对局身份引用，拒绝回滚")
    for contest in store.list_contests():
        if int(contest["id"]) in candidate_ids:
            continue
        if int(contest.get("source_contest_id") or 0) in candidate_ids:
            raise ShowcaseSeedError("非白名单赛事引用演示来源赛事，拒绝回滚")
        if any(
            int(entry.get("user_id") or 0) in player_ids
            or int(entry.get("bot_id") or 0) in bot_ids
            for entry in store.list_contest_entries(int(contest["id"]))
        ):
            raise ShowcaseSeedError("专用演示账号存在非白名单赛事报名，拒绝回滚")

    # Freeze the exact deletion whitelist only after every DB/filesystem scope
    # assertion has passed.  No mutation is permitted above this point.
    plan = _RollbackPlan(
        organizer_id=int(organizer["id"]),
        contest_matches=tuple(
            (int(contest["id"]), candidate_match_ids[int(contest["id"])])
            for contest in candidates
        ),
        player_bots=tuple(
            (
                int(player["id"]),
                int(bot["id"]) if bot is not None else None,
            )
            for player, bot in player_bots
        ),
        upload_root=resolved_upload_root,
    )

    deleted_matches = 0
    for cid, match_ids in plan.contest_matches:
        for match_id in match_ids:
            deleted_matches += int(store.delete_match(match_id))
        store.delete_contest(cid)
        emit(f"已删除演示赛事 #{cid}")

    bot_manager = BotManager(store, upload_root=plan.upload_root)
    deleted_bots = 0
    deleted_users = 0
    for player_id, bot_id in plan.player_bots:
        if bot_id is not None:
            bot_manager.purge_bot_files(bot_id)
            deleted_bots += int(store.delete_bot(bot_id))
        deleted_users += int(store.delete_user(player_id))
    deleted_users += int(store.delete_user(plan.organizer_id))
    marker = plan.upload_root / SHOWCASE_UPLOAD_MARKER
    if plan.upload_root.exists() and set(plan.upload_root.iterdir()) == {marker}:
        marker.unlink()
        plan.upload_root.rmdir()
    return {
        "contests": len(plan.contest_matches),
        "matches": deleted_matches,
        "bots": deleted_bots,
        "users": deleted_users,
    }


__all__ = [
    "ShowcaseSeedError",
    "rollback_showcases",
    "seed_showcases",
    "validate_showcase_upload_namespace",
    "validate_showcase_upload_target",
    "verify_showcases",
]
