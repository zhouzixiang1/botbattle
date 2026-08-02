"""全面解耦 PR3：DB 迁移测试——matches 拆每游戏表 + matches_index + ratings/rating_history 加 game_id。

验证：
1. 新库 schema 正确（三张 per-game 表 + matches_index + ratings 复合 PK + rating_history.game_id）
2. matches 路由：create/get/update/list/count_stats/like/incr_view 经 matches_index 正确
3. ratings per-game：ensure/get/update/history 按 (bot_id, game_id)
4. 跨游戏 UNION 查询（list_matches 无 game_id、count_stats、matchpack_months）正确
5. 旧库迁移：旧单表 matches 被丢弃（对局数据不保留），用户/bot/赛事数据保留；
   ratings 加 game_id 维度回填；contest_pairings.match_id 清空
"""
from __future__ import annotations

import os

import pytest

from bzplat.backend.store import Store


# ── 新库 schema 正确性 ────────────────────────────────────────
def test_new_db_has_per_game_match_tables(tmp_path):
    """新库建出三张 per-game 表 + matches_index，无旧单表 matches。"""
    s = Store(str(tmp_path / "new.db"))
    with s._tx() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    s.close()
    assert "matches_holdem" in tables
    assert "matches_gomoku" in tables
    assert "matches_pencil" in tables
    assert "matches_index" in tables
    assert "matches" not in tables  # 旧单表不存在
    assert "match_replays" in tables  # replay 表保留（全局）


def test_new_db_ratings_composite_pk(tmp_path):
    """ratings 表 PK = (bot_id, game_id)，含 game_id 列。"""
    s = Store(str(tmp_path / "new.db"))
    with s._tx() as c:
        cols = {r[1]: r for r in c.execute("PRAGMA table_info(ratings)")}
    s.close()
    assert "game_id" in cols
    # PK 标志：pk 字段在 PRAGMA table_info 里，bot_id 和 game_id 都是 pk=1
    assert cols["bot_id"]["pk"] >= 1
    assert cols["game_id"]["pk"] >= 1


def test_new_db_rating_history_has_game_id(tmp_path):
    s = Store(str(tmp_path / "new.db"))
    with s._tx() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(rating_history)")}
    s.close()
    assert "game_id" in cols


def test_contest_pairings_match_id_no_db_fk(tmp_path):
    """contest_pairings.match_id 无 DB 级 FK（逻辑外键，避免引用已删除的 matches 表）。"""
    s = Store(str(tmp_path / "new.db"))
    with s._tx() as c:
        fk_rows = c.execute("PRAGMA foreign_key_list(contest_pairings)").fetchall()
    s.close()
    # 不应有引用 matches_holdem/gomoku/pencil 或 matches 的 FK
    ref_tables = {r[2] for r in fk_rows}  # r[2] = referenced table
    assert not any("matches" in t for t in ref_tables)


# ── matches 路由（经 matches_index）────────────────────────────
@pytest.fixture()
def store_with_matches(tmp_path):
    s = Store(str(tmp_path / "m.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    bh = s.create_bot(u["id"], "botH", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    bg = s.create_bot(u["id"], "botG", binary_path="/tmp", format="elf", game_id="gomoku")["id"]
    bp = s.create_bot(u["id"], "botP", binary_path="/tmp", format="elf", game_id="pencil")["id"]
    yield s, u, bh, bg, bp
    s.close()


def test_create_match_routes_to_correct_table(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mh1", bh, bh, game_id="holdem", total_hands=70)
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.create_match("mp1", bp, bp, game_id="pencil", n_dots=11)
    # 验证写到了正确的物理表
    with s._tx() as c:
        assert c.execute("SELECT game_id FROM matches_holdem WHERE id=?", ("mh1",)).fetchone()["game_id"] == "holdem"
        assert c.execute("SELECT game_id FROM matches_gomoku WHERE id=?", ("mg1",)).fetchone()["game_id"] == "gomoku"
        assert c.execute("SELECT game_id FROM matches_pencil WHERE id=?", ("mp1",)).fetchone()["game_id"] == "pencil"
        # matches_index 维护正确
        assert c.execute("SELECT game_id FROM matches_index WHERE id=?", ("mh1",)).fetchone()["game_id"] == "holdem"
        assert c.execute("SELECT game_id FROM matches_index WHERE id=?", ("mp1",)).fetchone()["game_id"] == "pencil"


def test_get_match_routes_via_index(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mh1", bh, bh, game_id="holdem")
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.create_match("mp1", bp, bp, game_id="pencil")
    assert s.get_match("mh1")["game_id"] == "holdem"
    assert s.get_match("mg1")["game_id"] == "gomoku"
    assert s.get_match("mp1")["game_id"] == "pencil"
    assert s.get_match("nonexistent") is None


def test_update_match_routes_via_index(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.update_match("mg1", status="completed", winner=0, hands_played=9)
    m = s.get_match("mg1")
    assert m["status"] == "completed" and m["winner"] == 0 and m["hands_played"] == 9


def test_list_matches_cross_game_union(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mh1", bh, bh, game_id="holdem")
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.create_match("mp1", bp, bp, game_id="pencil")
    # 无 game_id → UNION ALL 三表
    allm = s.list_matches(limit=10)
    assert len(allm) == 3
    gids = {m["game_id"] for m in allm}
    assert gids == {"holdem", "gomoku", "pencil"}
    # 单游戏过滤
    assert len(s.list_matches(game_id="gomoku")) == 1


def test_count_matches_and_stats_cross_game(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mh1", bh, bh, game_id="holdem")
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.update_match("mg1", status="completed")
    assert s.count_matches() == 2
    assert s.count_matches("completed") == 1
    st = s.count_stats()
    assert st["matches"] == 2
    assert st["matches_completed"] == 1


def test_like_and_view_route_via_index(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.incr_match_view("mg1")
    s.like(u["id"], "match", "mg1")
    m = s.get_match("mg1")
    assert m["views_count"] == 1 and m["likes_count"] == 1


# ── ratings per-game ─────────────────────────────────────────
def test_ratings_per_game(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    # ensure_rating 建 (bot, game) 行
    s.ensure_rating(bg)
    r = s.get_rating(bg)
    assert r is not None and r["game_id"] == "gomoku"
    # update_rating_row
    s.update_rating_row(bg, rating=1900, matches_played=3)
    assert s.get_rating(bg)["rating"] == 1900
    # add/list history per-game
    s.add_rating_history(bg, 1900, 80, 0.06, 3)
    hist = s.list_rating_history(bg)
    assert len(hist) == 1 and hist[0]["rating"] == 1900


def test_bot_profile_joins_rating_with_game_id(store_with_matches):
    s, u, bh, bg, bp = store_with_matches
    s.ensure_rating(bg)
    s.update_rating_row(bg, rating=2100)
    p = s.bot_profile(bg)
    assert p["rating"] == 2100
    assert p["tier_name"] == "专家"


# ── 旧库迁移（对局丢弃，用户/bot/赛事保留）──────────────────────
def test_migrate_old_db_drops_matches_keeps_users(tmp_path):
    """旧库（单表 matches）迁移后：matches 表消失，对局数据丢弃，用户/bot 保留。"""
    db = str(tmp_path / "old.db")
    # 用旧 schema 建一个带单表 matches 的库（模拟旧库）
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE, email TEXT UNIQUE, password_hash TEXT,
            role TEXT DEFAULT 'user', display_name TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1, email_verified INTEGER DEFAULT 0,
            created_at TEXT, bio TEXT DEFAULT '', avatar TEXT DEFAULT '',
            xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0, last_active_at TEXT);
        CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER, name TEXT, display_name TEXT DEFAULT '',
            description TEXT DEFAULT '', os TEXT DEFAULT '', arch TEXT DEFAULT '',
            format TEXT DEFAULT 'unknown', binary_path TEXT DEFAULT '',
            current_version INTEGER DEFAULT 0, is_public INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1, is_builtin INTEGER DEFAULT 0,
            game_id TEXT DEFAULT 'holdem', created_at TEXT, updated_at TEXT,
            UNIQUE(owner_id, name));
        CREATE TABLE contests (id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT, description TEXT DEFAULT '', organizer_id INTEGER,
            status TEXT DEFAULT 'draft', registration_opens_at TEXT,
            registration_closes_at TEXT, starts_at TEXT, ends_at TEXT,
            hands_per_match INTEGER DEFAULT 70, created_at TEXT,
            game_id TEXT DEFAULT 'holdem', stages_json TEXT DEFAULT '[]',
            current_stage_idx INTEGER DEFAULT 0, template_id TEXT DEFAULT 'holdem_swiss_ko',
            rest_ends_at TEXT, match_config_json TEXT DEFAULT '{}');
        CREATE TABLE matches (id TEXT PRIMARY KEY, bot_a_id INTEGER, bot_b_id INTEGER,
            owner_id INTEGER, contest_id INTEGER, hands_played INTEGER DEFAULT 0,
            total_hands INTEGER DEFAULT 70, earnings_a INTEGER DEFAULT 0,
            earnings_b INTEGER DEFAULT 0, winner INTEGER, reason TEXT DEFAULT 'completed',
            net_bb_a REAL DEFAULT 0, match_type TEXT DEFAULT 'challenge',
            status TEXT DEFAULT 'pending', game_id TEXT DEFAULT 'holdem',
            created_at TEXT);
        CREATE TABLE contest_pairings (id INTEGER PRIMARY KEY AUTOINCREMENT,
            contest_id INTEGER, round_num INTEGER DEFAULT 1, bot_a_id INTEGER,
            bot_b_id INTEGER, match_id TEXT REFERENCES matches(id) ON DELETE SET NULL,
            status TEXT DEFAULT 'pending', stage_idx INTEGER DEFAULT 0,
            stage_key TEXT DEFAULT '', group_id TEXT DEFAULT '',
            bracket_slot INTEGER, color_first INTEGER DEFAULT 0);
        CREATE TABLE ratings (bot_id INTEGER PRIMARY KEY, rating REAL DEFAULT 1500.0,
            rd REAL DEFAULT 350.0, vol REAL DEFAULT 0.06, wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0, draws INTEGER DEFAULT 0, net_chips INTEGER DEFAULT 0,
            matches_played INTEGER DEFAULT 0, last_played_at TEXT);
    """)
    conn.execute("INSERT INTO users(username,email,password_hash,created_at) VALUES('alice','a@ex.com','h','2026-01-01')")
    conn.execute("INSERT INTO bots(owner_id,name,game_id,created_at,updated_at) VALUES(1,'botH','holdem','2026-01-01','2026-01-01')")
    conn.execute("INSERT INTO contests(id,title,organizer_id,created_at) VALUES(1,'old',1,'2026-01-01')")
    conn.execute("INSERT INTO matches(id,bot_a_id,bot_b_id,game_id,status,created_at) VALUES('m1',1,1,'holdem','completed','2026-01-01')")
    conn.execute("INSERT INTO contest_pairings(contest_id,round_num,bot_a_id,bot_b_id,match_id) VALUES(1,1,1,1,'m1')")
    conn.execute("INSERT INTO ratings(bot_id,rating) VALUES(1,1800)")
    conn.commit()
    conn.close()

    # 打开 → 触发迁移
    s = Store(db)
    with s._tx() as c:
        tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    s.close()

    # 对局表丢弃 + 新三表建出
    assert "matches" not in tables
    assert "matches_holdem" in tables and "matches_gomoku" in tables and "matches_pencil" in tables
    assert "matches_index" in tables
    # 用户/bot 保留
    s2 = Store(db)
    assert s2.get_user_by_email("a@ex.com") is not None
    assert s2.get_bot(1) is not None
    assert s2.get_bot(1)["name"] == "botH"
    # 对局数据丢弃
    assert s2.get_match("m1") is None
    # ratings game_id 回填
    r = s2.get_rating(1)
    assert r is not None and r["game_id"] == "holdem" and r["rating"] == 1800
    # contest_pairings.match_id 清空（旧引用失效）
    with s2._tx() as c:
        cp = c.execute("SELECT match_id FROM contest_pairings WHERE id=1").fetchone()
    assert cp["match_id"] is None
    s2.close()


# ── P0 修复测试（delete_bot FK + delete_match 一致性）─────────

def test_delete_bot_after_match_succeeds(tmp_path):
    """审计 P0：bot 参与过对局后 delete_bot 不再抛 FOREIGN KEY constraint failed。

    分表后 bot_a_id/bot_b_id 改 ON DELETE SET NULL（可空），删 bot 时对局保留、引用置空。
    """
    s = Store(str(tmp_path / "del.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b1 = s.create_bot(u["id"], "bot1", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    b2 = s.create_bot(u["id"], "bot2", binary_path="/tmp", format="elf", game_id="holdem")["id"]
    s.create_match("m1", b1, b2, game_id="holdem")
    # 删 bot1（参与过 m1）——不应抛异常
    assert s.delete_bot(b1) is True
    # 对局保留，bot_a_id 置空（SET NULL）
    m = s.get_match("m1")
    assert m is not None
    assert m["bot_a_id"] is None  # 被删的 bot 引用置空
    assert m["bot_b_id"] == b2  # 另一方保留
    s.close()


def test_delete_user_cascades_through_matches(tmp_path):
    """delete_user 级联到 bots → matches 的 bot_a/b 置空（不再因 RESTRICT 崩）。"""
    s = Store(str(tmp_path / "delu.db"))
    u = s.create_user("alice", "a@ex.com", "x")
    b1 = s.create_bot(u["id"], "bot1", binary_path="/tmp", format="elf", game_id="gomoku")["id"]
    b2 = s.create_bot(u["id"], "bot2", binary_path="/tmp", format="elf", game_id="gomoku")["id"]
    s.create_match("m1", b1, b2, game_id="gomoku")
    # 删用户 → 级联删 bots → matches bot_a/b 置空
    assert s.delete_user(u["id"]) is True
    m = s.get_match("m1")
    assert m is not None and m["bot_a_id"] is None and m["bot_b_id"] is None
    s.close()


def test_delete_match_cleans_index_and_replay(store_with_matches):
    """delete_match 删 per-game 行 + matches_index + replay（保 index 不漂移）。"""
    s, u, bh, bg, bp = store_with_matches
    s.create_match("mg1", bg, bg, game_id="gomoku")
    s.upsert_replay("mg1", '[{"type":"move"}]', "[]")
    # 删前都在
    assert s.get_match("mg1") is not None
    assert s.get_replay("mg1") is not None
    with s._tx() as c:
        assert c.execute("SELECT 1 FROM matches_index WHERE id=?", ("mg1",)).fetchone() is not None
    # 删除
    assert s.delete_match("mg1") is True
    # 删后全清
    assert s.get_match("mg1") is None
    assert s.get_replay("mg1") is None
    with s._tx() as c:
        assert c.execute("SELECT 1 FROM matches_index WHERE id=?", ("mg1",)).fetchone() is None
        assert c.execute("SELECT 1 FROM matches_gomoku WHERE id=?", ("mg1",)).fetchone() is None
    # 再删已删的返回 False
    assert s.delete_match("mg1") is False
    assert s.delete_match("nonexistent") is False


def test_per_game_tables_fk_on_delete_set_null(tmp_path):
    """所有引用 bots 的 FK 都是 CASCADE 或 SET NULL（防 delete_bot 回归）。

    matches_<game>.bot_a/b = SET NULL（对局保留）；contest_*/pair_stats/ratings 等 = CASCADE。
    """
    s = Store(str(tmp_path / "fk.db"))
    with s._tx() as c:
        for t, in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall():
            for r in c.execute(f"PRAGMA foreign_key_list({t})"):
                if r["table"] == "bots":
                    on_del = (r["on_delete"] or "").upper()
                    assert on_del in ("CASCADE", "SET NULL"), (
                        f"{t}.{r['from']} → bots FK 应 CASCADE 或 SET NULL，实际 {on_del}"
                    )
    s.close()


def test_migrate_old_db_orphan_ratings_dropped_not_crash(tmp_path):
    """迁移旧库时孤儿 ratings 行（引用已删 bot）被丢弃而非崩溃启动。"""
    db = str(tmp_path / "orphan.db")
    import sqlite3
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE users (id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE, email TEXT UNIQUE, password_hash TEXT,
            role TEXT DEFAULT 'user', display_name TEXT DEFAULT '',
            is_active INTEGER DEFAULT 1, email_verified INTEGER DEFAULT 0,
            created_at TEXT, bio TEXT DEFAULT '', avatar TEXT DEFAULT '',
            xp INTEGER DEFAULT 0, level INTEGER DEFAULT 0, last_active_at TEXT);
        CREATE TABLE bots (id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER, name TEXT, display_name TEXT DEFAULT '',
            description TEXT DEFAULT '', os TEXT DEFAULT '', arch TEXT DEFAULT '',
            format TEXT DEFAULT 'unknown', binary_path TEXT DEFAULT '',
            current_version INTEGER DEFAULT 0, is_public INTEGER DEFAULT 1,
            is_active INTEGER DEFAULT 1, is_builtin INTEGER DEFAULT 0,
            game_id TEXT DEFAULT 'holdem', created_at TEXT, updated_at TEXT,
            UNIQUE(owner_id, name));
        CREATE TABLE ratings (bot_id INTEGER PRIMARY KEY, rating REAL DEFAULT 1500.0,
            rd REAL DEFAULT 350.0, vol REAL DEFAULT 0.06, wins INTEGER DEFAULT 0,
            losses INTEGER DEFAULT 0, draws INTEGER DEFAULT 0, net_chips INTEGER DEFAULT 0,
            matches_played INTEGER DEFAULT 0, last_played_at TEXT);
    """)
    conn.execute("INSERT INTO users(username,email,password_hash,created_at) VALUES('a','a@e.com','h','2026')")
    conn.execute("INSERT INTO bots(owner_id,name,game_id,created_at,updated_at) VALUES(1,'bot1','holdem','2026','2026')")
    # bot_id=1 存在；bot_id=999 是孤儿（bots 表无此行）
    conn.execute("INSERT INTO ratings(bot_id,rating) VALUES(1,1800)")
    conn.execute("INSERT INTO ratings(bot_id,rating) VALUES(999,1500)")
    conn.commit()
    conn.close()

    # 迁移不应崩溃
    s = Store(db)
    r1 = s.get_rating(1)
    r_orphan = s.get_rating(999)
    s.close()
    assert r1 is not None and r1["rating"] == 1800  # 有效行保留
    assert r_orphan is None  # 孤儿行被丢弃（FK 校验：bots 表无 999）


# ── 审计 P1：跨游戏聚合遍历注册表（防第 4 游戏静默漏统计）─────────

def test_all_game_ids_derived_from_registry():
    """_all_game_ids 从注册表派生（db.py 跨游戏聚合用它，不再硬编码元组）。"""
    from bzplat.backend.store.db import _all_game_ids
    from bzplat.backend.games import registry
    assert _all_game_ids() == registry.all_ids()
    assert "holdem" in _all_game_ids() and "gomoku" in _all_game_ids() and "pencil" in _all_game_ids()


def test_cross_game_stats_cover_all_registered_games(store_with_matches):
    """count_stats / count_matches / list_matches 跨游戏聚合覆盖注册表全部游戏。

    审计 HIGH：曾硬编码 ("holdem","gomoku","pencil")，新增第 4 游戏会静默漏掉。
    此测试用各注册游戏各建一场对局，断言统计含全部——若有人加第 4 游戏但忘了
    更新 db.py，此处仍应覆盖（因 _all_game_ids 从注册表派生）。
    """
    s, u, bh, bg, bp = store_with_matches
    # 各注册游戏各建一场（reversi 是第 4 游戏，自建 bot）
    br = s.create_bot(u["id"], "botR", binary_path="/tmp", format="elf", game_id="reversi")["id"]
    for gid, bot in (("holdem", bh), ("gomoku", bg), ("pencil", bp), ("reversi", br)):
        s.create_match(f"m_{gid}", bot, bot, game_id=gid)
    # count_matches 跨游戏 = 注册游戏数
    from bzplat.backend.games import registry
    assert s.count_matches() == len(registry.all_ids())
    # count_stats
    st = s.count_stats()
    assert st["matches"] == len(registry.all_ids())
    # list_matches 跨游戏 UNION 含全部
    allm = s.list_matches(limit=50)
    gids = {m["game_id"] for m in allm}
    assert gids == registry.all_ids()
    s.close()
