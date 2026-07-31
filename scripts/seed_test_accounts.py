#!/usr/bin/env python3
"""种子脚本：建 2 个测试账号，每账号每游戏各上传 1 个样例 Bot。

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
from bzplat.backend.crypto import hash_password  # noqa: E402
from bzplat.backend.store import Store  # noqa: E402

# 测试账号（密码固定，仅测试用）
TEST_ACCOUNTS = [
    ("tester1", "tester1@example.com", "Test1234"),
    ("tester2", "tester2@example.com", "Test1234"),
]

# 每游戏对应的样例二进制（仓库内 samples/）
SAMPLE_BINARIES = {
    "holdem": "samples/callbot_linux_amd64",
    "gomoku": "samples/gomokubot_linux_amd64",
    "pencil": "samples/pencilbot_linux_amd64",
}


def get_or_create_user(store: Store, username: str, email: str, password: str) -> dict:
    u = store.get_user_by_username(username)
    if u:
        store.update_user(u["id"], email_verified=1, is_active=1)
        return store.get_user(u["id"])
    u = store.create_user(username, email, hash_password(password), display_name=username)
    store.update_user(u["id"], email_verified=1)
    return u


def get_or_create_bot(bm: BotManager, owner_id: int, name: str, game_id: str, path: Path) -> dict | None:
    existing = bm.store.get_bot_by_owner_name(owner_id, name)
    if existing:
        return existing
    raw = path.read_bytes()
    try:
        return bm.create_from_upload(
            owner_id, name, raw, display_name=name, game_id=game_id, is_public=True,
        )
    except BotError as e:
        print(f"  ! 上传 {name}({game_id}) 失败：{e.code} {e.message}", file=sys.stderr)
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("BZ_DB_PATH", "botzone.db"))
    ap.add_argument("--upload-root", default="bot_uploads")
    args = ap.parse_args()

    store = Store(args.db)
    bm = BotManager(store, upload_root=args.upload_root)

    print(f"种子数据库：{args.db}")
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
    print("\n种子完成。可用上述账号登录，在「挑战」页对战或搜索 tester1/tester2。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
