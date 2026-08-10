#!/usr/bin/env python3
"""Generate, verify or roll back the six read-only contest showcase snapshots.

Examples (database path is intentionally mandatory and absolute):

    python scripts/seed_contest_showcase.py seed \
      --db /abs/worktree/botzone.db --yes
    python scripts/seed_contest_showcase.py verify \
      --db /abs/worktree/botzone.db
    python scripts/seed_contest_showcase.py rollback \
      --db /abs/worktree/botzone.db --yes

The primary checkout database is refused unless ``--allow-primary`` is supplied
together with ``--yes``.  This task only runs the command against a copied DB.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bzplat.backend.contests.showcase_seed import (  # noqa: E402
    ShowcaseSeedError,
    rollback_showcases,
    seed_showcases,
    validate_showcase_upload_target,
    verify_showcases,
)
from bzplat.backend.qa_safety import primary_checkout_root  # noqa: E402
from bzplat.backend.runtime.config import MAX_CONCURRENT_MATCHES  # noqa: E402
from bzplat.backend.store import Store  # noqa: E402


def _absolute_existing(path_raw: str, *, label: str) -> Path:
    candidate = Path(path_raw).expanduser()
    if not candidate.is_absolute():
        raise ShowcaseSeedError(f"{label} 必须使用绝对路径")
    if candidate.is_symlink():
        raise ShowcaseSeedError(f"{label} 不得为符号链接")
    path = candidate.resolve()
    if not path.is_file():
        raise ShowcaseSeedError(f"{label} 不存在: {path}")
    return path


def _absolute_directory(path_raw: str, *, label: str) -> Path:
    candidate = Path(path_raw).expanduser()
    if not candidate.is_absolute():
        raise ShowcaseSeedError(f"{label} 必须使用绝对路径")
    if candidate.is_symlink():
        raise ShowcaseSeedError(f"{label} 不得为符号链接")
    path = candidate.resolve()
    if not path.is_dir():
        raise ShowcaseSeedError(f"{label} 不存在: {path}")
    return path


def _is_primary_db(db_path: Path) -> bool:
    primary = primary_checkout_root(ROOT)
    if primary is None:
        return False
    truth = (primary / "botzone.db").resolve()
    if db_path == truth:
        return True
    try:
        return truth.exists() and db_path.samefile(truth)
    except OSError:
        return False


def _is_primary_upload_root(upload_root: Path) -> bool:
    """Reject the two production upload roots even when the DB is a copy."""
    primary = primary_checkout_root(ROOT)
    if primary is None:
        return False
    for expected in (primary / "bot_uploads", primary / "bot_uploads_showcase"):
        resolved = expected.resolve()
        if upload_root == resolved:
            return True
        try:
            if resolved.exists() and upload_root.exists() and upload_root.samefile(resolved):
                return True
        except OSError:
            continue
    return False


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("seed", "verify", "rollback"))
    parser.add_argument("--db", required=True, help="现有 SQLite 数据库绝对路径（必填）")
    parser.add_argument(
        "--upload-root",
        help="演示 Bot 上传目录绝对路径（默认 <db.parent>/bot_uploads_showcase）",
    )
    parser.add_argument(
        "--profile-dir",
        default=str(ROOT / "samples/gomoku_showcase"),
        help="checksum 锁定的三档 canonical LongRunning 五子棋 ELF 目录",
    )
    parser.add_argument(
        "--max-concurrent",
        type=int,
        default=MAX_CONCURRENT_MATCHES,
        help=f"生成期并发（1..{MAX_CONCURRENT_MATCHES}，不越过代码运行上限）",
    )
    parser.add_argument("--timeout-per-contest", type=float, default=1800)
    parser.add_argument("--allow-primary", action="store_true")
    parser.add_argument(
        "--primary-service-stopped",
        action="store_true",
        help="确认主运行时 50380 已停服（主库/主 Bot 目录写操作必填）",
    )
    parser.add_argument("--yes", action="store_true", help="确认 seed/rollback 写操作")
    args = parser.parse_args()

    try:
        db_path = _absolute_existing(args.db, label="--db")
        primary_db = _is_primary_db(db_path)
        if primary_db and not args.allow_primary:
            raise ShowcaseSeedError(
                "拒绝写/迁移主 checkout 的 botzone.db；若确需部署演示数据，"
                "须在评审后停服，并显式传 --allow-primary "
                "--primary-service-stopped（写操作另需 --yes）"
            )
        if args.action in ("seed", "rollback") and not args.yes:
            raise ShowcaseSeedError(f"{args.action} 会写数据库，须显式传 --yes")
        if not 1 <= args.max_concurrent <= MAX_CONCURRENT_MATCHES:
            raise ShowcaseSeedError(
                f"--max-concurrent 必须在 1..{MAX_CONCURRENT_MATCHES}"
            )
        upload_root = (
            Path(args.upload_root).expanduser()
            if args.upload_root
            else db_path.parent / "bot_uploads_showcase"
        )
        upload_root = validate_showcase_upload_target(
            upload_root, db_path=db_path, checkout_root=ROOT
        )
        primary_upload = _is_primary_upload_root(upload_root)
        if (
            args.action in ("seed", "rollback")
            and primary_upload
            and not args.allow_primary
        ):
            raise ShowcaseSeedError(
                "拒绝写入或清理主 checkout 的 Bot 目录；即使 --db 是副本，"
                "也须在评审后停服，并显式传 --allow-primary "
                "--primary-service-stopped --yes"
            )

        print(f"动作：{args.action}")
        print(f"数据库：{db_path}")
        print(f"Bot 目录：{upload_root}")
        primary_target = primary_db or (
            args.action in ("seed", "rollback") and primary_upload
        )
        if primary_target and not args.primary_service_stopped:
            raise ShowcaseSeedError(
                "主运行时目标必须先停 50380，再显式传 --primary-service-stopped；"
                "独立 seed 进程不能与线上 orchestrator 叠加并发"
            )
        print(f"主运行时目标：{'是（已显式授权）' if primary_target else '否'}")

        if args.action == "seed":
            profile_dir = _absolute_directory(args.profile_dir, label="--profile-dir")
            result = asyncio.run(
                seed_showcases(
                    db_path,
                    upload_root,
                    profile_dir,
                    max_concurrent=args.max_concurrent,
                    timeout_per_contest=args.timeout_per_contest,
                    emit=print,
                )
            )
        else:
            store = Store(str(db_path))
            try:
                result = (
                    verify_showcases(store, upload_root)
                    if args.action == "verify"
                    else rollback_showcases(store, upload_root, emit=print)
                )
            finally:
                store.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (OSError, ValueError, ShowcaseSeedError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
