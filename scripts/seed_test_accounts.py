#!/usr/bin/env python3
"""种子脚本：建普通用户、组织者、管理员测试账号及普通用户样例 Bot。

便于人工/自动对战与人类对战测试。可重复运行（幂等：用户/bot 已存在则跳过）。

用法：
    source .venv/bin/activate
    python scripts/seed_test_accounts.py [--db botzone.db]

完成后打印账号密码与 bot id。默认游戏：holdem/gomoku/pencil。
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bzplat.backend.bots import BotManager  # noqa: E402
from bzplat.backend.bots.manager import BotError  # noqa: E402
from bzplat.backend.store import Store  # noqa: E402
from bzplat.backend.store.schema import ROLE_USER  # noqa: E402
from scripts._qa_accounts import (  # noqa: E402
    QaAccountSpec,
    get_or_create_dedicated_account,
    preflight_dedicated_accounts,
)
from scripts._qa_target import (  # noqa: E402
    primary_checkout_root,
    qa_db_path,
    qa_upload_root,
)

# 测试账号（密码固定，仅测试用）
TEST_ACCOUNTS = [
    ("tester1", "tester1@example.com", "Test1234"),
    ("tester2", "tester2@example.com", "Test1234"),
]

# 权限导航/管理端 E2E 账号（不上传 Bot）。仅用于隔离测试数据库。
ROLE_ACCOUNTS = [
    ("qa_organizer", "qa_organizer@example.com", "Test1234", "organizer"),
    ("qa_admin", "qa_admin@example.com", "Test1234", "admin"),
]

# 每游戏对应的样例二进制（仓库内 samples/）
SAMPLE_BINARIES = {
    "holdem": "samples/callbot_linux_amd64",
    "gomoku": "samples/gomokubot_linux_amd64",
    "pencil": "samples/pencilbot_linux_amd64",
}

TEST_ACCOUNT_NAMESPACE = "seed-test-players-v1"
ROLE_ACCOUNT_NAMESPACE = "seed-test-roles-v1"


def test_account_spec(
    username: str, email: str, password: str
) -> QaAccountSpec:
    return QaAccountSpec(
        TEST_ACCOUNT_NAMESPACE, username, email, password, ROLE_USER
    )


def role_account_spec(
    username: str, email: str, password: str, role: str
) -> QaAccountSpec:
    return QaAccountSpec(
        ROLE_ACCOUNT_NAMESPACE, username, email, password, role
    )


def get_or_create_user(store: Store, username: str, email: str, password: str) -> dict:
    """Create/reuse a test player only after its full identity matches."""
    return get_or_create_dedicated_account(
        store, test_account_spec(username, email, password)
    )


def get_or_create_role_user(
    store: Store, username: str, email: str, password: str, role: str
) -> dict:
    """Create a dedicated privileged E2E account without ever promoting a namesake.

    Role accounts are opt-in below. If a same-name user does not match the exact
    namespace/email/role/password contract, fail rather than changing it.
    """
    return get_or_create_dedicated_account(
        store, role_account_spec(username, email, password, role)
    )


def assert_role_account_target(root: Path, db_path: Path) -> None:
    """Reject the primary checkout's default DB for all fixed test credentials."""
    try:
        primary_root = primary_checkout_root(root)
    except RuntimeError as exc:
        raise RuntimeError(f"无法确认测试账号数据库隔离边界：{exc}") from exc
    if primary_root is not None and db_path.resolve() == (primary_root / "botzone.db").resolve():
        raise RuntimeError("拒绝在主 checkout 的 botzone.db 创建固定凭据测试账号")


def resolve_seed_paths(
    root: Path,
    db_raw: str,
    upload_raw: str | None = None,
) -> tuple[Path, Path]:
    """Resolve both mutation targets before opening the Store/BotManager.

    The upload directory follows ``db.parent`` by default.  A relative explicit
    value follows the same rule, never the caller's current working directory.
    """
    db_path = qa_db_path(db_raw, root)
    upload_root = qa_upload_root(upload_raw, db_path, root)
    return db_path, upload_root


def get_or_create_bot(bm: BotManager, owner_id: int, name: str, game_id: str, path: Path) -> dict | None:
    existing = bm.store.get_bot_by_owner_name(owner_id, name)
    if existing:
        return existing
    raw = path.read_bytes()
    try:
        return bm.create_from_upload(
            owner_id, name, raw, display_name=name, game_id=game_id,
        )
    except BotError as e:
        print(f"  ! 上传 {name}({game_id}) 失败：{e.code} {e.message}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("BZ_DB_PATH", "botzone.db"))
    ap.add_argument(
        "--upload-root",
        default=None,
        help="Bot 产物目录（默认 <db.parent>/bot_uploads；相对路径也基于 db.parent）",
    )
    ap.add_argument(
        "--with-role-accounts",
        action="store_true",
        help="在隔离 worktree DB 创建 qa_organizer/qa_admin（固定测试密码）",
    )
    args = ap.parse_args()

    db_path, upload_root = resolve_seed_paths(ROOT, args.db, args.upload_root)
    # 主 checkout 的 ROOT/.git 是目录；worktree 的 .git 是指针文件。即使调用者
    # 显式 opt-in，也不得向主 checkout 默认库写入固定凭据特权账号。
    try:
        assert_role_account_target(ROOT, db_path)
    except RuntimeError as exc:
        ap.error(str(exc))

    db_path.parent.mkdir(parents=True, exist_ok=True)
    store = Store(str(db_path))
    try:
        specs = [test_account_spec(*account) for account in TEST_ACCOUNTS]
        if args.with_role_accounts:
            specs.extend(role_account_spec(*account) for account in ROLE_ACCOUNTS)
        # Validate every existing username/email before activating an account or
        # uploading a Bot, so a late privileged-name collision cannot leave a
        # partially seeded runtime.
        preflight_dedicated_accounts(store, specs)
        bm = BotManager(store, upload_root=upload_root)

        print(f"种子数据库：{db_path}")
        print(f"Bot 产物目录：{upload_root}")
        for username, email, password in TEST_ACCOUNTS:
            u = get_or_create_user(store, username, email, password)
            print(f"\n账号 {username}（id={u['id']}）  密码：{password}")
            for gid, rel in SAMPLE_BINARIES.items():
                path = ROOT / rel
                if not path.is_file():
                    print(f"  ! 样例缺失：{rel}（跳过 {gid}）", file=sys.stderr)
                    continue
                botname = f"{username}_{gid}"
                b = get_or_create_bot(bm, u["id"], botname, gid, path)
                if b:
                    print(f"  {gid:7s} bot: {b['name']} (id={b['id']}, game={b['game_id']})")
        if args.with_role_accounts:
            for username, email, password, role in ROLE_ACCOUNTS:
                u = get_or_create_role_user(store, username, email, password, role)
                print(f"\n账号 {username}（id={u['id']}，role={role}）  密码：{password}")
    finally:
        store.close()
    print(
        "\n种子完成。普通用户可直接测试；权限 E2E 需在 worktree 使用 "
        "--with-role-accounts 显式创建专用账号。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
