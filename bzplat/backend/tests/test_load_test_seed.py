"""验证 scripts/load_test.py 的 seed() 函数：幂等 + token 可鉴权。

纯单测，不依赖运行服务。只测 seed 逻辑本身（用户/Bot/token 的 DB 直写播种）。
load_test.py 的 HTTP 阶段（对局/赛事/admin）是端到端脚本，由 scripts/load_test.py
本身覆盖（打 dev 服务）。
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]  # 仓库根


def _load_module():
    """加载 scripts/load_test.py 为模块（避免 sys.argv 触发 argparse）。"""
    spec = importlib.util.spec_from_file_location("load_test", ROOT / "scripts" / "load_test.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["load_test"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_seed_idempotent(tmp_path):
    """seed() 跑两次：用户数/Bot 数不变，且每次都返回可用 token。"""
    mod = _load_module()
    db = str(tmp_path / "load.db")
    upload_root = str(tmp_path / "uploads")
    n = 3  # 小规模，单元测试快速

    ctx1 = mod.seed(db, n, upload_root)
    ctx2 = mod.seed(db, n, upload_root)  # 第二次：应全部命中「已存在」分支

    # 用户数一致
    assert ctx1["user_names"] == ctx2["user_names"]
    assert len(ctx1["user_names"]) == n
    assert len(ctx1["org_names"]) == 2

    # Bot 映射一致（id 不变，幂等未重建）
    assert ctx1["bots"] == ctx2["bots"]
    for uname in ctx1["user_names"]:
        assert set(ctx1["bots"][uname].keys()) == {"holdem", "gomoku", "pencil"}

    # admin 与 admin_token
    assert ctx1["admin_name"] is not None
    assert ctx2["admin_name"] == ctx1["admin_name"]
    assert ctx1["admin_token"] and ctx2["admin_token"]

    # org_tokens 数量
    assert len(ctx1["org_tokens"]) == 2


def test_seed_token_is_valid_session(tmp_path):
    """seed 返回的 token 是 sessions 表里的合法行（可被 verify_session 验证）。"""
    mod = _load_module()
    db = str(tmp_path / "load.db")
    upload_root = str(tmp_path / "uploads")
    ctx = mod.seed(db, 2, upload_root)

    from bzplat.backend.store import Store

    store = Store(db)
    u0 = ctx["user_names"][0]
    tok = ctx["tokens"][u0]
    user = store.get_user_by_username(u0)
    # token 在 sessions 表存在且绑定该用户
    sess = store.get_session(tok)
    assert sess is not None
    assert sess["user_id"] == user["id"]
    assert sess["expires_at"]  # 非空
    store.close()


def test_seed_users_are_load_prefixed(tmp_path):
    """所有种子用户名都是 load_ 前缀（可一键识别清理，不污染真实数据）。"""
    mod = _load_module()
    db = str(tmp_path / "load.db")
    upload_root = str(tmp_path / "uploads")
    ctx = mod.seed(db, 2, upload_root)

    for name in ctx["user_names"] + ctx["org_names"] + [ctx["admin_name"]]:
        assert name.startswith("load_"), f"用户名 {name} 缺 load_ 前缀"


def test_seed_bots_are_load_prefixed(tmp_path):
    """所有种子 Bot 名都是 load_ 前缀。"""
    mod = _load_module()
    db = str(tmp_path / "load.db")
    upload_root = str(tmp_path / "uploads")
    ctx = mod.seed(db, 2, upload_root)

    from bzplat.backend.store import Store

    store = Store(db)
    for uname, gid_bid in ctx["bots"].items():
        for gid, bid in gid_bid.items():
            bot = store.get_bot(bid)
            assert bot is not None
            assert bot["name"].startswith("load_"), f"bot 名 {bot['name']} 缺 load_ 前缀"
            assert bot["game_id"] == gid
            # 私有 bot 功能已下线，bots 表无 is_public 列
            assert "is_public" not in bot
    store.close()


def test_seed_creates_rating_rows(tmp_path):
    """seed 为每个 bot 建 rating 行（Glicko 默认 1500）。"""
    mod = _load_module()
    db = str(tmp_path / "load.db")
    upload_root = str(tmp_path / "uploads")
    ctx = mod.seed(db, 1, upload_root)

    from bzplat.backend.store import Store

    store = Store(db)
    for uname, gid_bid in ctx["bots"].items():
        for bid in gid_bid.values():
            row = store._conn.execute(
                "SELECT rating, matches_played FROM ratings WHERE bot_id=?", (bid,)
            ).fetchone()
            assert row is not None, f"bot {bid} 无 rating 行"
            assert row["rating"] == 1500
            assert row["matches_played"] == 0
    store.close()


def test_seed_rebuild_ctx_consistent(tmp_path):
    """--skip-seed 的 _rebuild_ctx 能从已种 DB 重建出一致的上下文。"""
    mod = _load_module()
    db = str(tmp_path / "load.db")
    upload_root = str(tmp_path / "uploads")
    ctx = mod.seed(db, 3, upload_root)

    ctx2 = mod._rebuild_ctx(db)
    assert ctx2["user_names"] == ctx["user_names"]
    assert ctx2["org_names"] == ctx["org_names"]
    assert ctx2["admin_name"] == ctx["admin_name"]
    assert ctx2["bots"] == ctx["bots"]
    # token 是新生成的（不同于 seed 的），但都有效
    assert ctx2["admin_token"] and ctx2["admin_token"] != ctx["admin_token"]
