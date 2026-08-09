"""验证 scripts/load_test.py 的 seed() 函数：幂等 + token 可鉴权。

纯单测，不依赖运行服务。只测 seed 逻辑本身（用户/Bot/token 的 DB 直写播种）。
load_test.py 的 HTTP 阶段（对局/赛事/admin）是端到端脚本，由 scripts/load_test.py
本身覆盖（打 dev 服务）。
"""
from __future__ import annotations

import importlib.util
import hashlib
import sys
from pathlib import Path

import pytest

from bzplat.backend.crypto import hash_password
from bzplat.backend.store import Store

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


def test_seed_refreshes_stale_named_bot_by_sample_checksum(tmp_path):
    """同名 QA Bot 不能让旧协议二进制永久存活；内容漂移时发布新版本。"""
    mod = _load_module()
    db = str(tmp_path / "load.db")
    upload_root = str(tmp_path / "uploads")
    first = mod.seed(db, 1, upload_root)
    bot_id = first["bots"]["load_u01"]["gomoku"]

    store = Store(db)
    stale = store.get_current_bot_version(bot_id)
    assert stale is not None and stale["version"] == 1
    # 用另一款合法 ELF 模拟名字相同、内容仍停在旧协议的历史产物。
    Path(stale["binary_path"]).write_bytes((ROOT / "samples/callbot_linux_amd64").read_bytes())
    store.close()

    second = mod.seed(db, 1, upload_root)
    assert second["bots"]["load_u01"]["gomoku"] == bot_id
    repaired = Store(db)
    current = repaired.get_current_bot_version(bot_id)
    expected = (ROOT / "samples/gomokubot_linux_amd64").read_bytes()
    assert current["version"] == 2
    assert current["checksum"] == hashlib.sha256(expected).hexdigest()
    assert Path(current["binary_path"]).read_bytes() == expected
    repaired.close()


def test_seed_reactivates_valid_dedicated_qa_bot_without_new_version(tmp_path):
    mod = _load_module()
    db = str(tmp_path / "load.db")
    upload_root = str(tmp_path / "uploads")
    first = mod.seed(db, 1, upload_root)
    bot_id = first["bots"]["load_u01"]["gomoku"]
    store = Store(db)
    assert store.update_bot(bot_id, is_active=0)["is_active"] == 0
    assert len(store.list_bot_versions(bot_id)) == 1
    store.close()

    second = mod.seed(db, 1, upload_root)
    assert second["bots"]["load_u01"]["gomoku"] == bot_id
    restored = Store(db)
    assert restored.get_bot(bot_id)["is_active"] == 1
    assert len(restored.list_bot_versions(bot_id)) == 1
    restored.close()


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


def test_seed_default_upload_root_follows_database(tmp_path):
    """省略 upload_root 时，所有 Bot 文件必须落在隔离 DB 旁。"""
    mod = _load_module()
    db = tmp_path / "runtime" / "load.db"
    ctx = mod.seed(str(db), 1)

    from bzplat.backend.store import Store

    store = Store(str(db))
    try:
        expected = (db.parent / "bot_uploads").resolve()
        for game_bot_ids in ctx["bots"].values():
            for bot_id in game_bot_ids.values():
                binary = Path(store.get_bot(bot_id)["binary_path"]).resolve()
                assert expected in binary.parents
    finally:
        store.close()


def test_seed_rejects_primary_checkout_upload_root_before_opening_db(tmp_path):
    mod = _load_module()
    from bzplat.backend.qa_safety import primary_checkout_root

    primary = primary_checkout_root(mod.ROOT)
    assert primary is not None
    db = tmp_path / "load.db"
    with pytest.raises(SystemExit, match="bot_uploads"):
        mod.seed(str(db), 1, str(primary / "bot_uploads"))
    assert not db.exists()


def test_seed_never_reuses_or_mutates_arbitrary_admin(tmp_path):
    mod = _load_module()
    db = tmp_path / "load.db"
    store = Store(str(db))
    real_admin = store.create_user(
        "admin",
        "owner@example.com",
        hash_password("OwnerSecret1234"),
        role="admin",
    )
    store.update_user(real_admin["id"], is_active=0, email_verified=0)
    store.close()

    ctx = mod.seed(str(db), 1, str(tmp_path / "uploads"))

    store = Store(str(db))
    try:
        untouched = store.get_user(real_admin["id"])
        assert ctx["admin_name"] == mod.LOAD_ADMIN_NAME
        assert untouched["is_active"] == 0
        assert untouched["email_verified"] == 0
        assert store._conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE user_id=?",
            (real_admin["id"],),
        ).fetchone()[0] == 0
        dedicated = store.get_user_by_username(mod.LOAD_ADMIN_NAME)
        assert dedicated["role"] == "admin"
        assert dedicated["email"] == f"{mod.LOAD_ADMIN_NAME}@{mod.EMAIL_DOMAIN}"
    finally:
        store.close()


def test_seed_rejects_conflicting_dedicated_admin_before_other_writes(tmp_path):
    mod = _load_module()
    db = tmp_path / "load.db"
    store = Store(str(db))
    namesake = store.create_user(
        mod.LOAD_ADMIN_NAME,
        "foreign@example.com",
        hash_password("ForeignSecret1234"),
        role="admin",
    )
    store.update_user(namesake["id"], is_active=0, email_verified=0)
    store.close()

    with pytest.raises(RuntimeError, match="专用 QA 身份契约不匹配"):
        mod.seed(str(db), 1, str(tmp_path / "uploads"))

    store = Store(str(db))
    try:
        unchanged = store.get_user(namesake["id"])
        assert unchanged["is_active"] == 0
        assert unchanged["email_verified"] == 0
        assert store.get_user_by_username("load_u01") is None
        assert store._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    finally:
        store.close()


def test_seed_rejects_conflicting_load_user_without_changing_role(tmp_path):
    mod = _load_module()
    db = tmp_path / "load.db"
    store = Store(str(db))
    namesake = store.create_user(
        "load_u01",
        "load_u01@loadtest.local",
        hash_password(mod.PASSWORD),
        role="organizer",
    )
    store.update_user(namesake["id"], is_active=0, email_verified=0)
    store.close()

    with pytest.raises(RuntimeError, match="role"):
        mod.seed(str(db), 1, str(tmp_path / "uploads"))

    store = Store(str(db))
    try:
        unchanged = store.get_user(namesake["id"])
        assert unchanged["role"] == "organizer"
        assert unchanged["is_active"] == 0
        assert unchanged["email_verified"] == 0
    finally:
        store.close()


def test_rebuild_ctx_rejects_tampered_dedicated_admin_before_new_sessions(tmp_path):
    mod = _load_module()
    db = tmp_path / "load.db"
    mod.seed(str(db), 1, str(tmp_path / "uploads"))

    store = Store(str(db))
    admin = store.get_user_by_username(mod.LOAD_ADMIN_NAME)
    store.update_user(admin["id"], password_hash=hash_password("Tampered1234"))
    before = store._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    store.close()

    with pytest.raises(RuntimeError, match="password"):
        mod._rebuild_ctx(str(db))

    store = Store(str(db))
    try:
        after = store._conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        assert after == before
    finally:
        store.close()
