#!/usr/bin/env python3
"""一次性迁移脚本：把生产库所有 holdem Bot 的二进制换成 Botzone 协议样例。

背景：平台德州扑克协议全面切换到 Botzone 标准（裸整数 response / raise delta /
牌 0-51）。旧 Bot（紧凑 {a,x} 协议）无法在新协议下对战，须替换成 Botzone 协议
样例（不同风格随机分布，不追求强度）。

本脚本（PR-1，仅 holdem；棋类在 PR-3）：
- 遍历 ``bots WHERE game_id='holdem'``。
- 对每个 bot：用固定 seed 随机选 8 种风格之一，把对应编译好的样例 ELF 复制到
  ``bot_uploads/<id>/v<N>/bot.bin``（新建版本），调 ``add_bot_version``（v=N+1）
  + 设 current_version，同步 bots.binary_path。
- 幂等：已迁移的 bot（最新 bot_versions.upload_note == 'botzone-migrated'）跳过。
- 风格分布用固定 seed（确定性可复现，便于审计/回放）。

用法：
    source .venv/bin/activate
    python scripts/migrate_bots_to_botzone.py [--db botzone.db] [--game holdem] [--dry-run]

``--dry-run``：只打印将迁移的 bot 与分配的风格，不写库不复制文件。
"""
from __future__ import annotations

import argparse
import hashlib
import random
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from bzplat.backend.store import Store  # noqa: E402

MIGRATION_NOTE = "botzone-migrated"

# 每游戏的样例风格（Botzone 协议 ELF）。路径相对仓库根。
# holdem 8 种；gomoku/pencil 各 1 种（多风格待补；棋类策略差异对赛制排名影响较小）。
STYLE_BINARIES = {
    "holdem": {
        "foldbot": "samples/holdem_bots/foldbot",
        "allinbot": "samples/holdem_bots/allinbot",
        "raisebot": "samples/holdem_bots/raisebot",
        "randombot": "samples/holdem_bots/randombot",
        "tightbot": "samples/holdem_bots/tightbot",
        "loosebot": "samples/holdem_bots/loosebot",
        "callbot": "samples/callbot_linux_amd64",
        "aggressivebot": "samples/aggressivebot_bin",
    },
    "gomoku": {
        "random": "samples/gomokubot_linux_amd64",
    },
    "pencil": {
        "random": "samples/pencilbot_linux_amd64",
    },
}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_migrated(store: Store, bot_id: int) -> bool:
    """最新版本是否已标记为 botzone-migrated。"""
    versions = store.list_bot_versions(bot_id)
    if not versions:
        return False
    latest = max(versions, key=lambda v: v["version"])
    return (latest.get("upload_note") or "") == MIGRATION_NOTE


def migrate(store: Store, *, game_id: str = "holdem", dry_run: bool = False,
            seed: int = 20260804) -> dict:
    """执行迁移，返回统计 {total, migrated, skipped, styles}。"""
    rng = random.Random(seed)
    rows = store._conn.execute(
        "SELECT id, name, binary_path, current_version, game_id FROM bots "
        "WHERE game_id=? ORDER BY id",
        (game_id,),
    ).fetchall()

    upload_root = Path("bot_uploads")
    stats = {"total": len(rows), "migrated": 0, "skipped": 0, "styles": {}}

    for row in rows:
        bot_id = row["id"]
        name = row["name"]
        if _is_migrated(store, bot_id):
            stats["skipped"] += 1
            print(f"[skip] bot {bot_id} ({name}) 已迁移")
            continue

        style = rng.choice(list(STYLE_BINARIES[game_id].keys()))
        src = ROOT / STYLE_BINARIES[game_id][style]
        if not src.is_file():
            print(f"[ERROR] 样例 binary 不存在: {src}", file=sys.stderr)
            stats["skipped"] += 1
            continue

        if dry_run:
            print(f"[dry-run] bot {bot_id} ({name}) → 风格 {style} ({src.name})")
            stats["styles"][style] = stats["styles"].get(style, 0) + 1
            continue

        # 新版本号 = 当前最大版本 + 1
        versions = store.list_bot_versions(bot_id)
        next_ver = max((v["version"] for v in versions), default=0) + 1
        dest_dir = upload_root / str(bot_id) / f"v{next_ver}"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / "bot.bin"
        shutil.copyfile(str(src), str(dest))
        checksum = _sha256(dest)
        size = dest.stat().st_size

        store.add_bot_version(
            bot_id,
            binary_path=str(dest),
            upload_note=MIGRATION_NOTE,
            checksum=checksum,
            size_bytes=size,
            os="linux",
            arch="amd64",
            format="elf",
            version=next_ver,
        )
        stats["migrated"] += 1
        stats["styles"][style] = stats["styles"].get(style, 0) + 1
        print(f"[ok] bot {bot_id} ({name}) → v{next_ver} 风格 {style}")

    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", default="botzone.db", help="SQLite 数据库路径")
    ap.add_argument("--game", default="all", choices=["all", "holdem", "gomoku", "pencil"],
                    help="迁移哪个游戏（all = 三游戏全迁）")
    ap.add_argument("--dry-run", action="store_true", help="只打印不写库")
    ap.add_argument("--seed", type=int, default=20260804, help="风格分布随机种子")
    args = ap.parse_args()

    games = ["holdem", "gomoku", "pencil"] if args.game == "all" else [args.game]

    # 确保所需游戏的样例已编译
    for gid in games:
        for style, rel in STYLE_BINARIES[gid].items():
            if not (ROOT / rel).is_file():
                print(f"样例 binary 缺失：{rel}。请先编译（holdem: bash samples/holdem_bots/gen.sh；"
                      f"棋类: cc -O2 -o samples/{gid}bot_linux_amd64 samples/{gid}bot.c）",
                      file=sys.stderr)
                return 2

    store = Store(args.db)
    total_stats = {"total": 0, "migrated": 0, "skipped": 0, "styles": {}}
    for gid in games:
        stats = migrate(store, game_id=gid, dry_run=args.dry_run, seed=args.seed + hash(gid) % 1000)
        for k in ("total", "migrated", "skipped"):
            total_stats[k] += stats[k]
        for s, c in stats["styles"].items():
            key = f"{gid}/{s}"
            total_stats["styles"][key] = total_stats["styles"].get(key, 0) + c
        if stats["total"]:
            print(f"\n=== {gid} 迁移统计 ===")
            print(f"总数: {stats['total']}  已迁移: {stats['migrated']}  跳过: {stats['skipped']}")

    print("\n=== 总迁移统计 ===")
    print(f"总数: {total_stats['total']}  已迁移: {total_stats['migrated']}  跳过: {total_stats['skipped']}")
    print("风格分布:")
    for style, count in sorted(total_stats["styles"].items()):
        print(f"  {style}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
