"""botzone-platform SQLite 存储层。

持久连接 + threading.Lock；时间戳统一 ISO 秒精度；行返回 dict。
"""
from __future__ import annotations

import contextlib
import json
import sqlite3
import threading
from datetime import datetime
from typing import Any

from bzplat.backend.mail import seed_email_templates

from .schema import SCHEMA

DEFAULT_DB_PATH = "botzone.db"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _loads_json(raw: str | None, *, default: Any) -> Any:
    """容错 JSON 解析：失败/空返回 default。"""
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _add_col(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    cols = _table_cols(conn, table)
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# 每游戏对局表的建表模板（全面解耦 PR3：matches 拆三表，结构一致）。
# {suffix} = holdem/gomoku/pencil；{gdef} = 该表 game_id 列的 DEFAULT。
_CREATE_MATCHES_TABLE_SQL = """
CREATE TABLE matches_{suffix} (
    id              TEXT    PRIMARY KEY,
    bot_a_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    bot_b_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    owner_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    contest_id      INTEGER REFERENCES contests(id) ON DELETE SET NULL,
    hands_played    INTEGER NOT NULL DEFAULT 0,
    total_hands     INTEGER NOT NULL DEFAULT 70,
    earnings_a      INTEGER NOT NULL DEFAULT 0,
    earnings_b      INTEGER NOT NULL DEFAULT 0,
    winner          INTEGER,
    reason          TEXT    NOT NULL DEFAULT 'completed',
    net_bb_a        REAL    NOT NULL DEFAULT 0,
    match_type      TEXT    NOT NULL DEFAULT 'challenge',
    status          TEXT    NOT NULL DEFAULT 'pending',
    game_id         TEXT    NOT NULL DEFAULT '{gdef}',
    n_dots          INTEGER,
    human_user_id   INTEGER,
    human_seat      INTEGER,
    match_seed      INTEGER,  -- P4：对局确定性 seed（duplicate 复现/回放用）
    technical_loss  INTEGER NOT NULL DEFAULT 0,  -- P4：技术判负标记（崩溃/超时判负但计分）
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT    NOT NULL,
    likes_count     INTEGER NOT NULL DEFAULT 0,
    views_count     INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT chk_winner_{suffix} CHECK (winner IN (0, 1) OR winner IS NULL),
    CONSTRAINT chk_status_{suffix} CHECK (status IN ('pending','running','completed','aborted')),
    CONSTRAINT chk_type_{suffix} CHECK (match_type IN ('challenge','table','contest','ladder','human'))
)
"""


def _matches_table(game_id: str) -> str:
    """game_id → 对应的物理表名（matches_holdem/gomoku/pencil）。"""
    gid = (game_id or "holdem").strip().lower()
    if gid not in _all_game_ids():
        raise ValueError(f"未知 game_id: {game_id!r}（合法: {sorted(_all_game_ids())}）")
    return f"matches_{gid}"


def _all_game_ids() -> frozenset[str]:
    """已注册的全部 game_id（从 games 注册表派生——单一真相，审计 P1 修复）。

    延迟 import 避免循环依赖（games 包加载时 store 已可用）。
    db.py 的跨游戏聚合（UNION ALL / COUNT 遍历）须用此函数，不得硬编码
    ("holdem","gomoku","pencil")——否则新增第 4 游戏会静默漏掉所有跨游戏统计。
    """
    from bzplat.backend.games import registry as _reg

    return _reg.all_ids()


def _migrate(conn: sqlite3.Connection) -> None:
    """为已有库补列；必要时重建 contests 以放宽 status CHECK。"""
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "contests" not in tables:
        return

    # ── 孤儿 FK 行清理（审计 P0：生产 9943 条孤儿源于连接期 FK=OFF，删 bot/user 未级联）──
    # 一次性清理存量孤儿。幂等：DELETE/UPDATE 0 行代价极低，每次迁移都跑。
    # 放在 _migrate 开头（新库早返之后）保证所有后续表重建 INSERT 只看到干净数据
    # （contest_* 重建的 INSERT INTO _new SELECT FROM _ctable 未过滤孤儿，FK ON 时会失败）。
    def _has(table: str) -> bool:
        return table in {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
            )
        }

    # CASCADE 类（删行）：bots→子表 + users→子表
    for _tbl, _col in (
        ("ratings", "bot_id"),
        ("rating_history", "bot_id"),
        ("bot_versions", "bot_id"),
        ("favorites", "bot_id"),
        ("pair_stats", "bot_a_id"),
        ("pair_stats", "bot_b_id"),
        ("password_resets", "user_id"),
        ("sessions", "user_id"),
        ("email_codes", "user_id"),
        ("notifications", "user_id"),
        ("comments", "user_id"),
        ("likes", "user_id"),
        ("follows", "follower_id"),
        ("follows", "followee_id"),
    ):
        if _has(_tbl):
            conn.execute(
                f"DELETE FROM {_tbl} WHERE {_col} IS NOT NULL "
                f"AND {_col} NOT IN (SELECT id FROM {'bots' if _col.endswith('bot_id') or _col in ('bot_a_id', 'bot_b_id') else 'users'})"
            )

    # SET NULL 类（置空保留行）：matches_*.{bot_a/b/owner/contest} + contest_*.{bot_id}
    for _gid in _all_game_ids():
        _tbl = _matches_table(_gid)
        if _has(_tbl):
            for _col in ("bot_a_id", "bot_b_id"):
                conn.execute(
                    f"UPDATE {_tbl} SET {_col}=NULL WHERE {_col} IS NOT NULL "
                    f"AND {_col} NOT IN (SELECT id FROM bots)"
                )
            conn.execute(
                f"UPDATE {_tbl} SET owner_id=NULL WHERE owner_id IS NOT NULL "
                f"AND owner_id NOT IN (SELECT id FROM users)"
            )
            conn.execute(
                f"UPDATE {_tbl} SET contest_id=NULL WHERE contest_id IS NOT NULL "
                f"AND contest_id NOT IN (SELECT id FROM contests)"
            )
    for _tbl, _col in (
        ("contest_entries", "bot_id"),
        ("contest_pairings", "bot_a_id"),
        ("contest_pairings", "bot_b_id"),
        ("contest_stage_results", "bot_id"),
        ("contest_official_results", "bot_id"),
    ):
        if _has(_tbl):
            conn.execute(
                f"UPDATE {_tbl} SET {_col}=NULL WHERE {_col} IS NOT NULL "
                f"AND {_col} NOT IN (SELECT id FROM bots)"
            )

    # ── 赛事侧孤儿 FK 清理（对抗审计：PR #88/#93 仅覆盖 bots/users，漏 contest 侧）──
    # contests.* 的子表 contest_id/user_id 孤儿（CASCADE：删行）+ contests.organizer_id
    # 孤儿（NO ACTION + NOT NULL：只能删整条 contest）。必须在 contests_new / contest_*
    # 重建（下方 INSERT INTO _new SELECT FROM _old，未过滤孤儿）之前完成，否则 FK ON 时
    # 重建 INSERT 抛 IntegrityError 启动崩溃。
    # 顺序：先删 contest 子表孤儿（contest_id/user_id），再删 organizer 孤儿的 contest 本身。
    for _tbl, _col, _parent in (
        ("contest_entries", "contest_id", "contests"),
        ("contest_entries", "user_id", "users"),
        ("contest_pairings", "contest_id", "contests"),
        ("contest_stage_results", "contest_id", "contests"),
        ("contest_official_results", "contest_id", "contests"),
    ):
        if _has(_tbl):
            conn.execute(
                f"DELETE FROM {_tbl} WHERE {_col} IS NOT NULL "
                f"AND {_col} NOT IN (SELECT id FROM {_parent})"
            )
    # contests.organizer_id → users（NO ACTION + NOT NULL）：organizer 不存在的 contest 整条删。
    # 此时其 contest_* 子表孤儿已清（上方），CASCADE 亦带走残留——双保险。
    if _has("contests"):
        conn.execute(
            "DELETE FROM contests WHERE organizer_id IS NOT NULL "
            "AND organizer_id NOT IN (SELECT id FROM users)"
        )
    # 补 PR #88 CASCADE 类遗漏：bots.owner_id / favorites.user_id / notification_prefs.user_id
    for _tbl, _col in (
        ("bots", "owner_id"),
        ("favorites", "user_id"),
        ("notification_prefs", "user_id"),
    ):
        if _has(_tbl):
            conn.execute(
                f"DELETE FROM {_tbl} WHERE {_col} IS NOT NULL "
                f"AND {_col} NOT IN (SELECT id FROM users)"
            )

    create_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='contests'"
    ).fetchone()
    sql_text = (create_sql[0] or "") if create_sql else ""
    cols = _table_cols(conn, "contests")

    for col, decl in (
        ("game_id", "TEXT NOT NULL DEFAULT 'holdem'"),
        ("stages_json", "TEXT NOT NULL DEFAULT '[]'"),
        ("current_stage_idx", "INTEGER NOT NULL DEFAULT 0"),
        ("template_id", "TEXT NOT NULL DEFAULT 'holdem_swiss_ko'"),
        ("rest_ends_at", "TEXT"),
        ("match_config_json", "TEXT NOT NULL DEFAULT '{}'"),
        ("phase", "TEXT NOT NULL DEFAULT 'standalone'"),  # P2 预赛/决赛
        ("source_contest_id", "INTEGER"),
        ("official_results_ready", "INTEGER NOT NULL DEFAULT 0"),
        ("require_real_name", "INTEGER NOT NULL DEFAULT 0"),
    ):
        _add_col(conn, "contests", col, decl)

    cols = _table_cols(conn, "contests")
    if "'rest'" not in sql_text:
        conn.execute(
            """
            CREATE TABLE contests_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                organizer_id INTEGER NOT NULL REFERENCES users(id),
                status TEXT NOT NULL DEFAULT 'draft',
                registration_opens_at TEXT,
                registration_closes_at TEXT,
                starts_at TEXT,
                ends_at TEXT,
                hands_per_match INTEGER NOT NULL DEFAULT 70,
                created_at TEXT NOT NULL,
                game_id TEXT NOT NULL DEFAULT 'holdem',
                stages_json TEXT NOT NULL DEFAULT '[]',
                current_stage_idx INTEGER NOT NULL DEFAULT 0,
                template_id TEXT NOT NULL DEFAULT 'holdem_swiss_ko',
                rest_ends_at TEXT,
                match_config_json TEXT NOT NULL DEFAULT '{}',
                CONSTRAINT chk_contest_status CHECK (
                    status IN ('draft','open','running','rest','finished','cancelled'))
            )
            """
        )
        all_cols = [
            "id", "title", "description", "organizer_id", "status",
            "registration_opens_at", "registration_closes_at", "starts_at",
            "ends_at", "hands_per_match", "created_at",
            "game_id", "stages_json", "current_stage_idx", "template_id",
            "rest_ends_at", "match_config_json",
        ]
        present = [c for c in all_cols if c in cols]
        conn.execute(
            f"INSERT INTO contests_new ({', '.join(present)}) "
            f"SELECT {', '.join(present)} FROM contests "
            f"WHERE organizer_id IN (SELECT id FROM users)"
        )
        conn.execute("DROP TABLE contests")
        conn.execute("ALTER TABLE contests_new RENAME TO contests")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contests_org ON contests(organizer_id)"
        )

    if "contest_entries" in tables:
        for col, decl in (
            ("group_id", "TEXT NOT NULL DEFAULT ''"),
            ("seed", "INTEGER NOT NULL DEFAULT 0"),
            ("eliminated", "INTEGER NOT NULL DEFAULT 0"),
            ("dispatched_at", "TEXT"),
        ):
            _add_col(conn, "contest_entries", col, decl)

    if "contest_pairings" in tables:
        for col, decl in (
            ("stage_idx", "INTEGER NOT NULL DEFAULT 0"),
            ("stage_key", "TEXT NOT NULL DEFAULT ''"),
            ("group_id", "TEXT NOT NULL DEFAULT ''"),
            ("bracket_slot", "INTEGER"),
            ("color_first", "INTEGER NOT NULL DEFAULT 0"),
        ):
            _add_col(conn, "contest_pairings", col, decl)

    if "bots" in tables:
        _add_col(conn, "bots", "game_id", "TEXT NOT NULL DEFAULT 'holdem'")
        # 下线私有 bot 功能（全局只有「公开」一种状态）：旧库的 is_public 列先转公开
        # 再 DROP COLUMN（保数据不丢）。幂等：列已不存在则跳过。
        if "is_public" in _table_cols(conn, "bots"):
            conn.execute("UPDATE bots SET is_public=1 WHERE is_public=0")
            conn.execute("ALTER TABLE bots DROP COLUMN is_public")

    if "users" in tables:
        _add_col(conn, "users", "bio", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "avatar", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "xp", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "users", "level", "INTEGER NOT NULL DEFAULT 0")
        _add_col(conn, "users", "last_active_at", "TEXT")
        _add_col(conn, "users", "real_name", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "phone", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "school", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "student_id", "TEXT NOT NULL DEFAULT ''")

    if "matches" in tables:
        # 全面解耦 PR3：旧单表 matches 拆成每游戏一张表（matches_holdem/gomoku/pencil）
        # + matches_index 定位表。按用户决策：**对局数据不保留**（可后续跑种子脚本重建），
        # 用户/Bot/赛事/评论/评分等数据保留。故这里直接 DROP 旧表，由 SCHEMA 建新表。
        # 同时清空引用旧 matches 的关联数据（match_replays、contest_pairings.match_id）。
        # 注意顺序：先清 contest_pairings.match_id（此时 matches 还在，FK 校验通过——
        # 置 NULL 不触发 FK 拒绝），再 DROP matches（被引用表删除时 SQLite 不校验 FK）。
        cp_cols = _table_cols(conn, "contest_pairings") if "contest_pairings" in tables else set()
        if "match_id" in cp_cols:
            conn.execute("UPDATE contest_pairings SET match_id=NULL WHERE match_id IS NOT NULL")
        conn.execute("DROP TABLE IF EXISTS matches")
        # 注意：不 DROP match_replays——SCHEMA executescript 已用 IF NOT EXISTS 建空表，
        # 若此处 DROP 会让迁移当次进程内 match_replays 缺失到下次重启。旧 replay 数据
        # 本就随对局丢弃（重建库），无需 DROP 再建。
        # 新三张表 + matches_index 由 SCHEMA executescript 创建（IF NOT EXISTS 幂等）

    # pair_stats 补胜负计数列（head-to-head 战绩用）
    if "pair_stats" in tables:
        for col, decl in (
            ("a_wins", "INTEGER NOT NULL DEFAULT 0"),
            ("a_losses", "INTEGER NOT NULL DEFAULT 0"),
            ("draws", "INTEGER NOT NULL DEFAULT 0"),
        ):
            _add_col(conn, "pair_stats", col, decl)

    # 赛制模板：表为空时从代码默认 + 旧 blob 导入
    if "contest_templates" in tables or True:  # 新库也会建表
        # 懒导入避免循环
        from bzplat.backend.contests.templates import (
            DEFAULT_TEMPLATES,
            default_match_config,
        )

        ntpl = conn.execute("SELECT COUNT(*) FROM contest_templates").fetchone()[0]
        if ntpl == 0:
            # 先看旧 platform_settings blob（admin 历史覆盖）
            blob_row = conn.execute(
                "SELECT value FROM platform_settings WHERE key='contest_templates'"
            ).fetchone()
            imported_ids: set[str] = set()
            now = _now()
            if blob_row and blob_row[0]:
                try:
                    blob = json.loads(blob_row[0])
                    if isinstance(blob, list):
                        for t in blob:
                            if not isinstance(t, dict):
                                continue
                            tid = str(t.get("id") or "").strip()
                            if not tid or tid in imported_ids:
                                continue
                            imported_ids.add(tid)
                            gid = (t.get("game_id") or "holdem").strip().lower()
                            conn.execute(
                                "INSERT OR REPLACE INTO contest_templates"
                                "(id, name, game_id, match_config, stages_json, is_builtin, updated_at) "
                                "VALUES(?,?,?,?,?,?,?)",
                                (
                                    tid,
                                    str(t.get("name") or tid),
                                    gid,
                                    json.dumps(t.get("match_config") or default_match_config(gid)),
                                    json.dumps(t.get("stages") or [], ensure_ascii=False),
                                    0,
                                    now,
                                ),
                            )
                except (json.JSONDecodeError, TypeError):
                    pass
            # 再补代码默认模板（未被 blob 覆盖的）
            for tid, t in DEFAULT_TEMPLATES.items():
                if tid in imported_ids:
                    continue
                gid = t.get("game_id") or "holdem"
                conn.execute(
                    "INSERT OR REPLACE INTO contest_templates"
                    "(id, name, game_id, match_config, stages_json, is_builtin, updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        tid,
                        t.get("name") or tid,
                        gid,
                        json.dumps(default_match_config(gid)),
                        json.dumps(t.get("stages") or [], ensure_ascii=False),
                        1,
                        now,
                    ),
                )

        # 对账：补齐代码定义但 DB 缺失的内置模板。生产库 PR#74 前创建时 seed 只在
        # 表空时跑一次（上方 if ntpl==0 守卫），导致之后新增的内置模板（如预赛/决赛）
        # 永远不会入库——前端 GET /api/contests/templates 读 DB 表 → 缺失 → UI 看不到。
        # 每次 _migrate 都跑：仅 INSERT 缺失项，绝不覆盖已有行（尊重 admin 覆盖/旧 blob
        # 导入的 is_builtin=0 行）。幂等：已存在的跳过。
        now2 = _now()
        for tid, t in DEFAULT_TEMPLATES.items():
            exists = conn.execute(
                "SELECT 1 FROM contest_templates WHERE id=?", (tid,)
            ).fetchone()
            if exists:
                continue
            gid = t.get("game_id") or "holdem"
            conn.execute(
                "INSERT INTO contest_templates"
                "(id, name, game_id, match_config, stages_json, is_builtin, updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    tid,
                    t.get("name") or tid,
                    gid,
                    json.dumps(default_match_config(gid)),
                    json.dumps(t.get("stages") or [], ensure_ascii=False),
                    1,
                    now2,
                ),
            )

    # ── ratings / rating_history 加 game_id 维度（全面解耦 PR3）──────────
    # 旧库 ratings PK = bot_id（无 game_id 列）；rating_history 无 game_id 列。
    # 迁移：加 game_id 列，按 bots.game_id 回填，重建表改 PK 为 (bot_id, game_id)。
    # 幂等：若 ratings 已有 game_id 列则跳过（新库 SCHEMA 直接建复合 PK）。
    if "ratings" in tables:
        r_cols = _table_cols(conn, "ratings")
        if "game_id" not in r_cols:
            # 重建 ratings：加 game_id 列 + 复合 PK，按 bots.game_id 回填
            conn.execute(
                """
                CREATE TABLE ratings_new (
                    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    game_id         TEXT    NOT NULL DEFAULT 'holdem',
                    rating          REAL    NOT NULL DEFAULT 1500.0,
                    rd              REAL    NOT NULL DEFAULT 350.0,
                    vol             REAL    NOT NULL DEFAULT 0.06,
                    wins            INTEGER NOT NULL DEFAULT 0,
                    losses          INTEGER NOT NULL DEFAULT 0,
                    draws           INTEGER NOT NULL DEFAULT 0,
                    net_chips       INTEGER NOT NULL DEFAULT 0,
                    matches_played  INTEGER NOT NULL DEFAULT 0,
                    last_played_at  TEXT,
                    PRIMARY KEY (bot_id, game_id)
                )
                """
            )
            # 回填：每行 game_id 取自 bots.game_id（bot 绑定单一游戏）。
            # 只迁移 bots 表里仍存在的 bot（丢弃孤儿 ratings 行，避免 FK 校验崩溃）。
            conn.execute(
                """
                INSERT INTO ratings_new
                    (bot_id, game_id, rating, rd, vol, wins, losses, draws,
                     net_chips, matches_played, last_played_at)
                SELECT r.bot_id, COALESCE(b.game_id, 'holdem'),
                       r.rating, r.rd, r.vol, r.wins, r.losses, r.draws,
                       r.net_chips, r.matches_played, r.last_played_at
                FROM ratings r
                LEFT JOIN bots b ON b.id = r.bot_id
                WHERE r.bot_id IN (SELECT id FROM bots)
                """
            )
            conn.execute("DROP TABLE ratings")
            conn.execute("ALTER TABLE ratings_new RENAME TO ratings")

    if "rating_history" in tables:
        rh_cols = _table_cols(conn, "rating_history")
        if "game_id" not in rh_cols:
            conn.execute(
                """
                CREATE TABLE rating_history_new (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
                    game_id         TEXT    NOT NULL DEFAULT 'holdem',
                    rating          REAL    NOT NULL,
                    rd              REAL    NOT NULL,
                    vol             REAL    NOT NULL,
                    matches_played  INTEGER NOT NULL,
                    reason          TEXT    NOT NULL DEFAULT '',
                    created_at      TEXT    NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO rating_history_new
                    (id, bot_id, game_id, rating, rd, vol, matches_played, reason, created_at)
                SELECT rh.id, rh.bot_id, COALESCE(b.game_id, 'holdem'),
                       rh.rating, rh.rd, rh.vol, rh.matches_played, rh.reason, rh.created_at
                FROM rating_history rh
                LEFT JOIN bots b ON b.id = rh.bot_id
                WHERE rh.bot_id IN (SELECT id FROM bots)
                """
            )
            conn.execute("DROP TABLE rating_history")
            conn.execute("ALTER TABLE rating_history_new RENAME TO rating_history")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_rating_history_bot "
                "ON rating_history(bot_id, game_id, id DESC)"
            )

    # ── per-game matches 表 FK 加固（ON DELETE SET NULL，全面解耦审计 P0 修复）─────
    # 旧库分表后 bot_a_id/bot_b_id 无 ON DELETE 子句（SQLite 默认 RESTRICT）→
    # delete_bot 在 bot 参与过对局后抛 FOREIGN KEY constraint failed。
    # 检测并重建三表（SQLite 不能 ALTER FK，需 CREATE new→INSERT→DROP→RENAME）。
    # 对局数据可丢弃（用户决策），重建后为空也无妨。
    def _match_fk_has_set_null(conn, table: str) -> bool:
        """该 matches_<game> 表的 bot_a_id FK 是否已带 ON DELETE SET NULL。"""
        for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            # row: (id, seq, table, from, to, on_update, on_delete, match)
            if row["table"] == "bots" and row["from"] in ("bot_a_id", "bot_b_id"):
                if (row["on_delete"] or "").upper() != "SET NULL":
                    return False
        return True

    # 解耦审计 P0：从 games 注册表派生 game_id 列表（不得硬编码 ("holdem","gomoku","pencil")），
    # 否则新增第 4 游戏会静默漏掉 FK 重建 → delete_bot 在该表崩 FOREIGN KEY constraint failed。
    for _gid in _all_game_ids():
        _tbl = _matches_table(_gid)
        if _tbl in tables and not _match_fk_has_set_null(conn, _tbl):
            # FK 非 SET NULL → 重建（对局数据丢弃，与分表迁移一致）
            conn.execute(f"DROP TABLE IF EXISTS {_tbl}")
            conn.execute(_CREATE_MATCHES_TABLE_SQL.format(suffix=_gid, gdef=_gid))
            # 清理 matches_index 中指向该表的残留定位（表已空）
            conn.execute(
                "DELETE FROM matches_index WHERE game_id=?", (_gid,)
            )

    # ── contest_* 表 bot FK 改 SET NULL + entry 身份列（预赛/决赛 P0：删 bot 不得抹成绩）──
    # 旧：bot FK 为 CASCADE（删 bot → 清报名/对阵/成绩）。新：SET NULL（删 bot → bot_id 置空，
    # entry/pairing/stage_results 保留，历史成绩不丢）。同时加 entry 身份列：
    #   contest_pairings.entry_a_id/entry_b_id（排名键，换 Bot 不丢分）
    #   contest_stage_results.entry_id（唯一键改 entry）
    def _bot_fk_is_set_null(conn, table: str) -> bool:
        """该表所有 bots FK 都已带 ON DELETE SET NULL。"""
        for row in conn.execute(f"PRAGMA foreign_key_list({table})").fetchall():
            if row["table"] == "bots" and (row["on_delete"] or "").upper() != "SET NULL":
                return False
        return True

    # 各表重建模板：bot FK = SET NULL + 新增 entry 身份列。列与 SCHEMA 一致。
    _CONTEST_TABLE_REBUILDS = {
        "contest_entries": (
            "CREATE TABLE {n}_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE, "
            "user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE, "
            "bot_id INTEGER REFERENCES bots(id) ON DELETE SET NULL, "  # SET NULL：删 bot 留 entry
            "registered_at TEXT NOT NULL, group_id TEXT NOT NULL DEFAULT '', "
            "seed INTEGER NOT NULL DEFAULT 0, eliminated INTEGER NOT NULL DEFAULT 0, "
            "dispatched_at TEXT)",
            "contest_id, user_id, bot_id, registered_at, group_id, seed, eliminated, dispatched_at",
        ),
        "contest_pairings": (
            "CREATE TABLE {n}_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE, "
            "round_num INTEGER NOT NULL DEFAULT 1, "
            "entry_a_id INTEGER, entry_b_id INTEGER, "  # P0：entry 身份键（换 Bot 不丢分）
            "bot_a_id INTEGER REFERENCES bots(id) ON DELETE SET NULL, "
            "bot_b_id INTEGER REFERENCES bots(id) ON DELETE SET NULL, "
            "match_id TEXT, status TEXT NOT NULL DEFAULT 'pending', "
            "stage_idx INTEGER NOT NULL DEFAULT 0, stage_key TEXT NOT NULL DEFAULT '', "
            "group_id TEXT NOT NULL DEFAULT '', bracket_slot INTEGER, color_first INTEGER NOT NULL DEFAULT 0)",
            "id, contest_id, round_num, bot_a_id, bot_b_id, match_id, status, "
            "stage_idx, stage_key, group_id, bracket_slot, color_first",
        ),
        "contest_stage_results": (
            "CREATE TABLE {n}_new ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "contest_id INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE, "
            "stage_idx INTEGER NOT NULL, stage_key TEXT NOT NULL DEFAULT '', "
            "entry_id INTEGER, "  # P0：排名键改 entry（唯一键含 entry_id）
            "bot_id INTEGER REFERENCES bots(id) ON DELETE SET NULL, "
            "points REAL NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0, "
            "draws INTEGER NOT NULL DEFAULT 0, losses INTEGER NOT NULL DEFAULT 0, "
            "net_chips INTEGER NOT NULL DEFAULT 0, group_id TEXT NOT NULL DEFAULT '', "
            "rank_in_group INTEGER, payload_json TEXT NOT NULL DEFAULT '{{}}', "
            "UNIQUE(contest_id, stage_idx, entry_id))",
            "id, contest_id, stage_idx, stage_key, bot_id, points, wins, draws, losses, "
            "net_chips, group_id, rank_in_group, payload_json",
        ),
    }
    for _ctable, (_ddl_tpl, _cols) in _CONTEST_TABLE_REBUILDS.items():
        # 触发重建：FK 非 SET NULL，或新身份列缺失
        _need = _ctable not in tables or not _bot_fk_is_set_null(conn, _ctable)
        if _ctable == "contest_pairings" and _ctable in tables:
            _need = _need or "entry_a_id" not in _table_cols(conn, _ctable)
        if _ctable == "contest_stage_results" and _ctable in tables:
            _need = _need or "entry_id" not in _table_cols(conn, _ctable)
            # P0 fix：旧迁移重建漏了 UNIQUE(contest_id,stage_idx,entry_id) → upsert ON CONFLICT 崩。
            # 检测表 DDL 是否含该 UNIQUE，缺则重建。
            if not _need:
                _ddl = conn.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                    (_ctable,),
                ).fetchone()
                _ddl_text = (_ddl[0] if _ddl else "") or ""
                if "UNIQUE(contest_id, stage_idx, entry_id)" not in _ddl_text:
                    _need = True
        if not _need:
            continue
        # 清理上次失败残留的 _new 表（保幂等）
        conn.execute(f"DROP TABLE IF EXISTS {_ctable}_new")
        # 取实际存在的列（旧库可能少列），只迁移都有的
        _have = _table_cols(conn, _ctable) if _ctable in tables else set()
        _present = [c.strip() for c in _cols.split(",") if c.strip() in _have]
        _col_list = ", ".join(_present)
        conn.execute(_ddl_tpl.format(n=_ctable))
        if _col_list:
            conn.execute(
                f"INSERT INTO {_ctable}_new ({_col_list}) "
                f"SELECT {_col_list} FROM {_ctable} "
                f"WHERE contest_id IN (SELECT id FROM contests)"
            )
        if _ctable in tables:
            conn.execute(f"DROP TABLE {_ctable}")
        conn.execute(f"ALTER TABLE {_ctable}_new RENAME TO {_ctable}")
        # 重建索引（SCHEMA 的 IF NOT EXISTS 不会对已 DROP 的表生效，手动补）
        if _ctable == "contest_entries":
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contest_entries_c ON contest_entries(contest_id)")
        elif _ctable == "contest_pairings":
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contest_pairings_c ON contest_pairings(contest_id)")
        elif _ctable == "contest_stage_results":
            conn.execute("CREATE INDEX IF NOT EXISTS idx_contest_stage_results_c ON contest_stage_results(contest_id)")

    # ── per-game matches 表自动建（解耦审计：让"新增第 4 游戏"真正零改动 DB）────────
    # schema.py 里 matches_holdem/gomoku/pencil 三张表是字面 CREATE 语句；新增注册游戏
    # （如 reversi）后 SCHEMA executescript 不会建 matches_<new>，create_match 会崩
    # `no such table`。这里对每个已注册游戏幂等建表 + 索引（CREATE TABLE IF NOT EXISTS），
    # 让 DB 层随注册表自动扩展，无需手改 schema.py 的 DDL。
    # 每游戏表的统一索引列（与 schema.py:404-421 的三套索引一一对应）。
    _PER_GAME_INDEX_COLS = ("bot_a_id", "bot_b_id", "owner_id", "contest_id", "status", "created_at")
    for _gid in _all_game_ids():
        _tbl = _matches_table(_gid)
        if _tbl not in tables:
            conn.execute(_CREATE_MATCHES_TABLE_SQL.format(suffix=_gid, gdef=_gid))
    # 重新读取当前物理表集合（上面的建表/FK 重建可能改变了它），再幂等补索引。
    _tables_after = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    for _gid in _all_game_ids():
        _tbl = _matches_table(_gid)
        if _tbl not in _tables_after:
            continue  # 表确实没建出来（如 _CREATE_MATCHES_TABLE_SQL 被破坏）→ 跳过，交给启动断言报错
        for _col in _PER_GAME_INDEX_COLS:
            conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_m{_gid}_{_col} ON {_tbl}({_col})"
            )
        # P4：matches 表加 match_seed + technical_loss 列（幂等）
        _add_col(conn, _tbl, "match_seed", "INTEGER")
        _add_col(conn, _tbl, "technical_loss", "INTEGER NOT NULL DEFAULT 0")

    # ── 去重：删 schema.py 旧字面索引（与上面 _PER_GAME_INDEX_COLS 循环建的重复）─────
    # 旧索引名后缀 bot_a/bot_b/owner/contest/time（无 _id/_at）；
    # 新索引名后缀为完整列名（bot_a_id/owner_id/contest_id/created_at）。
    # 注意 status 列两套同名（都是 idx_m{g}_status），不能删（删了会误删新索引）。
    # 幂等：DROP INDEX IF EXISTS 对不存在的索引是 no-op。
    _LEGACY_IDX_SUFFIXES = ("bot_a", "bot_b", "owner", "contest", "time")
    for _gid in _all_game_ids():
        for _suf in _LEGACY_IDX_SUFFIXES:
            conn.execute(f"DROP INDEX IF EXISTS idx_m{_gid}_{_suf}")

    # ── 清理已下线游戏的残留 matches_<game> 表（审计：生产 matches_reversi 孤儿）──
    # reversi 在 commit f1c92fc 下线，但生产库表残留。泛化：任何 matches_* 表
    # 若其 game_id 不在注册表，则 DROP（数据随游戏下线一并丢弃，与 reversi 决策一致）。
    # 安全：matches_index 是路由表（非 matches_<game> 形式），显式排除防误删。
    _registered = {f"matches_{gid}" for gid in _all_game_ids()}
    for (_name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'matches\\_%' ESCAPE '\\'"
    ):
        if _name not in _registered and _name != "matches_index":
            conn.execute(f"DROP TABLE IF EXISTS {_name}")

    # ── contest_pairings 轮次冻结列（预赛/决赛 P1：版本/seed/发布闸门）────────
    if "contest_pairings" in _tables_after:
        _add_col(conn, "contest_pairings", "bot_a_version_id", "INTEGER")
        _add_col(conn, "contest_pairings", "bot_b_version_id", "INTEGER")
        _add_col(conn, "contest_pairings", "pairing_seed", "INTEGER")
        _add_col(conn, "contest_pairings", "published_at", "TEXT")


class Store:
    """SQLite 存储。线程安全；持久连接 check_same_thread=False。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        # FK 强制是 SQLite 的连接级设置（默认 OFF）。在连接处一次性开启，覆盖所有
        # 访问路径（_tx / 直接 _conn / 脚本 / 备份恢复）——_tx() 内的重复开启是冗余
        # no-op 但保留以明示意。修前 FK 仅 _tx 内 ON，绕过 _tx 的删除不级联→留孤儿。
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")  # 锁等待 5s，防并发写直接报错
        self._conn.row_factory = sqlite3.Row
        with self._tx() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)
            seed_email_templates(conn, _now())
            # 启动一致性断言：每个已注册游戏必须有对应的物理表 matches_<game>。
            # schema.py 的字面 DDL 只覆盖 holdem/gomoku/pencil；第 4 游戏须经
            # _migrate 的自动建表补出来。此断言在 _migrate 之后跑，确保"注册了
            # 但表没建"的 drift 在启动即报（而非 create_match 时才崩 no such table）。
            _existing = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            _missing = [
                f"matches_{gid}" for gid in _all_game_ids()
                if f"matches_{gid}" not in _existing
            ]
            assert not _missing, (
                f"注册表里的游戏缺物理表（_migrate 自动建表应覆盖此场景）："
                f"{_missing}。检查 games/__init__.py 注册 vs schema.py DDL。"
            )

    @contextlib.contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ── users ─────────────────────────────────────────────────

    def create_user(
        self,
        username: str,
        email: str,
        password_hash: str,
        *,
        display_name: str = "",
        role: str = "user",
        real_name: str = "",
        phone: str = "",
        school: str = "",
        student_id: str = "",
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO users(username, email, password_hash, role, "
                "display_name, created_at, real_name, phone, school, student_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    username,
                    email,
                    password_hash,
                    role,
                    display_name or username,
                    _now(),
                    real_name,
                    phone,
                    school,
                    student_id,
                ),
            )
            uid = cur.lastrowid
            return _row(c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())

    def get_user(self, user_id: int) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            )

    def get_user_by_username(self, username: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM users WHERE username=?", (username,)
                ).fetchone()
            )

    def user_profile(self, username: str) -> dict | None:
        """用户主页聚合：用户公开信息（不含 password_hash/email）+ 总战绩。

        总战绩 = 该用户所有 bot 的 ratings SUM(wins/losses/draws/net_chips/matches_played)。
        Bot 列表与对局历史用单独端点（避免单次返回过大）。
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT id, username, display_name, role, bio, avatar, "
                "created_at, last_login_at, xp, level, last_active_at "
                "FROM users WHERE username=? AND is_active=1",
                (username,),
            ).fetchone()
            if not row:
                return None
            d = _row(row)
            uid = d["id"]
            agg = c.execute(
                "SELECT COALESCE(SUM(r.wins),0) AS wins, "
                "COALESCE(SUM(r.losses),0) AS losses, "
                "COALESCE(SUM(r.draws),0) AS draws, "
                "COALESCE(SUM(r.matches_played),0) AS matches_played, "
                "COALESCE(SUM(r.net_chips),0) AS net_chips, "
                "COUNT(r.bot_id) AS rated_bots "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE b.owner_id=?",
                (uid,),
            ).fetchone()
            d["stats"] = _row(agg) if agg else {
                "wins": 0, "losses": 0, "draws": 0,
                "matches_played": 0, "net_chips": 0, "rated_bots": 0,
            }
            d["bot_count"] = c.execute(
                "SELECT COUNT(*) FROM bots WHERE owner_id=?", (uid,)
            ).fetchone()[0]
            return d

    def aggregate_owner_stats(self, owner_id: int) -> dict:
        """按 owner 聚合其所有 bot 的战绩（用于用户主页总战绩）。"""
        with self._tx() as c:
            agg = c.execute(
                "SELECT COALESCE(SUM(r.wins),0) AS wins, "
                "COALESCE(SUM(r.losses),0) AS losses, "
                "COALESCE(SUM(r.draws),0) AS draws, "
                "COALESCE(SUM(r.matches_played),0) AS matches_played, "
                "COALESCE(SUM(r.net_chips),0) AS net_chips "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id WHERE b.owner_id=?",
                (owner_id,),
            ).fetchone()
            return _row(agg) if agg else {
                "wins": 0, "losses": 0, "draws": 0,
                "matches_played": 0, "net_chips": 0,
            }

    def award_xp(self, user_id: int, amount: int) -> dict | None:
        """给用户加经验，并重算 level + 更新 last_active_at。返回更新后的 user。"""
        from bzplat.backend.store.schema import level_for_xp
        if amount == 0:
            return self.get_user(user_id)
        with self._tx() as c:
            row = c.execute(
                "SELECT xp FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not row:
                return None
            new_xp = max(0, int(row["xp"] or 0) + max(0, amount))
            new_level = level_for_xp(new_xp)
            c.execute(
                "UPDATE users SET xp=?, level=?, last_active_at=? WHERE id=?",
                (new_xp, new_level, _now(), user_id),
            )
            return _row(
                c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            )

    def search_bots(
        self,
        q: str,
        *,
        limit: int = 20,
        game_id: str | None = None,
    ) -> list[dict]:
        """按 name/display_name 模糊搜索 public bot（含 owner 名 + rating）。"""
        ql = f"%{q.lower()}%" if q else "%"
        with self._tx() as c:
            sql = (
                "SELECT b.id, b.name, b.display_name, b.game_id, b.format, "
                "b.os, b.arch, u.username AS owner_name, "
                "u.display_name AS owner_display, r.rating "
                "FROM bots b LEFT JOIN users u ON b.owner_id=u.id "
                "LEFT JOIN ratings r ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE b.is_active=1 "
                "AND (LOWER(b.name) LIKE ? OR LOWER(b.display_name) LIKE ?)"
            )
            params: list[Any] = [ql, ql]
            if game_id:
                sql += " AND b.game_id=?"
                params.append(game_id)
            sql += " ORDER BY r.rating DESC LIMIT ?"
            params.append(max(1, min(limit, 50)))
            return [_row(r) for r in c.execute(sql, params)]

    def search_matches(
        self,
        q: str,
        *,
        limit: int = 20,
        game_id: str | None = None,
    ) -> list[dict]:
        """按 bot 名/owner 名模糊搜索已完成对局。"""
        ql = f"%{q.lower()}%" if q else "%"
        with self._tx() as c:
            sel = (
                "m.*, ba.name AS bot_a_name, bb.name AS bot_b_name, "
                "ba.display_name AS bot_a_display, bb.display_name AS bot_b_display"
            )
            join_bots = (
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id"
            )
            where_sql = (
                " WHERE m.status='completed' "
                "AND (LOWER(ba.name) LIKE ? OR LOWER(bb.name) LIKE ? "
                "OR LOWER(ba.display_name) LIKE ? OR LOWER(bb.display_name) LIKE ?)"
            )
            params: list[Any] = [ql, ql, ql, ql]
            if game_id:
                where_sql += " AND m.game_id=?"
                params.append(game_id)
            lim = max(1, min(limit, 50))

            if game_id:
                tbl = _matches_table(game_id)
                sql = f"SELECT {sel} FROM {tbl} m {join_bots}{where_sql} ORDER BY m.created_at DESC LIMIT ?"
                return [_row(r) for r in c.execute(sql, params + [lim])]

            # 跨游戏 UNION ALL
            subselects = []
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                subselects.append(f"SELECT {sel} FROM {tbl} m {join_bots}{where_sql}")
            union = " UNION ALL ".join(subselects)
            sql = f"SELECT * FROM ({union}) ORDER BY created_at DESC LIMIT ?"
            # 子查询数 = 已注册游戏数，WHERE 参数须按此倍数复制（每个子查询一份）。
            # 不得硬编码 * 3——新增第 4 游戏会触发 Incorrect number of bindings。
            return [_row(r) for r in c.execute(sql, params * len(_all_game_ids()) + [lim])]

    def get_user_by_email(self, email: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
            )

    def update_user(self, user_id: int, **fields: Any) -> dict | None:
        allowed = {
            "password_hash",
            "email",
            "display_name",
            "role",
            "is_active",
            "last_login_at",
            "email_verified",
            "bio",
            "avatar",
            "xp",
            "level",
            "last_active_at",
            "real_name",
            "phone",
            "school",
            "student_id",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            if sets:
                vals.append(user_id)
                c.execute(f"UPDATE users SET {','.join(sets)} WHERE id=?", vals)
            return _row(
                c.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            )

    def list_users(
        self, *, role: str | None = None, active_only: bool = False
    ) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM users WHERE 1=1"
            params: list[Any] = []
            if role:
                sql += " AND role=?"
                params.append(role)
            if active_only:
                sql += " AND is_active=1"
            sql += " ORDER BY created_at"
            return [_row(r) for r in c.execute(sql, params)]

    def search_users(self, q: str, *, limit: int = 20) -> list[dict]:
        """按用户名前缀搜索（仅返回安全字段 id/username/display_name）。"""
        q = (q or "").strip()
        with self._tx() as c:
            if not q:
                sql = (
                    "SELECT id, username, display_name FROM users "
                    "WHERE is_active=1 ORDER BY username LIMIT ?"
                )
                rows = c.execute(sql, (limit,)).fetchall()
            else:
                sql = (
                    "SELECT id, username, display_name FROM users "
                    "WHERE is_active=1 AND username LIKE ? ORDER BY username LIMIT ?"
                )
                rows = c.execute(sql, (q + "%", limit)).fetchall()
            return [_row(r) for r in rows]

    # ── sessions ──────────────────────────────────────────────

    def add_session(
        self,
        token: str,
        user_id: int,
        expires_at: str,
        *,
        ip_addr: str = "",
        user_agent: str = "",
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR REPLACE INTO sessions(token, user_id, expires_at, "
                "created_at, ip_addr, user_agent) VALUES(?,?,?,?,?,?)",
                (token, user_id, expires_at, _now(), ip_addr, user_agent),
            )

    def get_session(self, token: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute("SELECT * FROM sessions WHERE token=?", (token,)).fetchone()
            )

    def delete_session(self, token: str) -> bool:
        with self._tx() as c:
            return (
                c.execute("DELETE FROM sessions WHERE token=?", (token,)).rowcount > 0
            )

    def delete_sessions_for_user(self, user_id: int) -> int:
        with self._tx() as c:
            return c.execute(
                "DELETE FROM sessions WHERE user_id=?", (user_id,)
            ).rowcount

    # 兼容别名
    delete_user_sessions = delete_sessions_for_user

    # ── email_codes ───────────────────────────────────────────

    def add_email_code(
        self, user_id: int, purpose: str, code: str, expires_at: str
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO email_codes(user_id, purpose, code, expires_at, "
                "created_at) VALUES(?,?,?,?,?)",
                (user_id, purpose, code, expires_at, _now()),
            )

    def get_latest_email_code(self, user_id: int, purpose: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM email_codes WHERE user_id=? AND purpose=? "
                    "AND used_at IS NULL ORDER BY id DESC LIMIT 1",
                    (user_id, purpose),
                ).fetchone()
            )

    def mark_email_code_used(self, code_id: int) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE email_codes SET used_at=? WHERE id=?", (_now(), code_id)
            )

    # ── password_resets ───────────────────────────────────────

    def add_password_reset(
        self, token: str, user_id: int, expires_at: str
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO password_resets(token, user_id, expires_at, "
                "created_at) VALUES(?,?,?,?)",
                (token, user_id, expires_at, _now()),
            )

    def get_password_reset(self, token: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM password_resets WHERE token=? AND used_at IS NULL",
                    (token,),
                ).fetchone()
            )

    def mark_password_reset_used(self, token: str) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE password_resets SET used_at=? WHERE token=?",
                (_now(), token),
            )

    # ── bots ──────────────────────────────────────────────────

    def create_bot(
        self,
        owner_id: int | None = None,
        name: str | None = None,
        **fields: Any,
    ) -> dict:
        if owner_id is not None:
            fields["owner_id"] = owner_id
        if name is not None:
            fields["name"] = name
        owner_id = fields["owner_id"]
        name = fields["name"]
        display_name = fields.get("display_name") or name
        description = fields.get("description", "")
        os_ = fields.get("os", "")
        arch = fields.get("arch", "")
        fmt = fields.get("format", "unknown")
        binary_path = fields.get("binary_path", "")
        is_builtin = 1 if fields.get("is_builtin") else 0
        game_id = fields.get("game_id") or "holdem"
        now = _now()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO bots(owner_id, name, display_name, description, "
                "os, arch, format, binary_path, is_builtin, game_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    owner_id,
                    name,
                    display_name,
                    description,
                    os_,
                    arch,
                    fmt,
                    binary_path,
                    is_builtin,
                    game_id,
                    now,
                    now,
                ),
            )
            bid = cur.lastrowid
            return _row(c.execute("SELECT * FROM bots WHERE id=?", (bid,)).fetchone())

    def get_bot(self, bot_id: int) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            )

    def get_bot_by_owner_name(self, owner_id: int, name: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM bots WHERE owner_id=? AND name=?",
                    (owner_id, name),
                ).fetchone()
            )

    def update_bot(self, bot_id: int, **fields: Any) -> dict | None:
        allowed = {
            "display_name",
            "description",
            "os",
            "arch",
            "format",
            "binary_path",
            "current_version",
            "is_active",
            "game_id",
            "updated_at",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            if sets:
                if "updated_at" not in fields:
                    sets.append("updated_at=?")
                    vals.append(_now())
                vals.append(bot_id)
                c.execute(f"UPDATE bots SET {','.join(sets)} WHERE id=?", vals)
            return _row(
                c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone()
            )

    def delete_bot(self, bot_id: int) -> bool:
        with self._tx() as c:
            return c.execute("DELETE FROM bots WHERE id=?", (bot_id,)).rowcount > 0

    def list_bots(
        self,
        owner_id: int | None = None,
        *,
        active_only: bool = True,
        include_builtin: bool = True,
        game_id: str | None = None,
    ) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM bots WHERE 1=1"
            params: list[Any] = []
            if owner_id is not None:
                sql += " AND owner_id=?"
                params.append(owner_id)
            if active_only:
                sql += " AND is_active=1"
            if not include_builtin:
                sql += " AND is_builtin=0"
            if game_id:
                sql += " AND game_id=?"
                params.append(game_id)
            sql += " ORDER BY is_builtin DESC, name"
            return [_row(r) for r in c.execute(sql, params)]

    # ── bot_versions ──────────────────────────────────────────

    def add_bot_version(
        self,
        bot_id: int,
        *,
        binary_path: str,
        upload_note: str = "",
        checksum: str = "",
        size_bytes: int = 0,
        os: str = "",
        arch: str = "",
        format: str = "unknown",
        version: int | None = None,
    ) -> dict:
        with self._tx() as c:
            if version is None:
                row = c.execute(
                    "SELECT MAX(version) AS mv FROM bot_versions WHERE bot_id=?",
                    (bot_id,),
                ).fetchone()
                version = (row["mv"] or 0) + 1
            cur = c.execute(
                "INSERT INTO bot_versions(bot_id, version, binary_path, "
                "upload_note, checksum, size_bytes, os, arch, format, "
                "uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    bot_id,
                    version,
                    binary_path,
                    upload_note,
                    checksum,
                    size_bytes,
                    os,
                    arch,
                    format,
                    _now(),
                ),
            )
            vid = cur.lastrowid
            c.execute(
                "UPDATE bots SET current_version=?, binary_path=?, os=?, arch=?, "
                "format=?, updated_at=? WHERE id=?",
                (version, binary_path, os, arch, format, _now(), bot_id),
            )
            return _row(
                c.execute("SELECT * FROM bot_versions WHERE id=?", (vid,)).fetchone()
            )

    def delete_bot_version(self, bot_id: int, version: int) -> bool:
        """删除指定版本；若删的是当前版本，回退到 max(version)。"""
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM bot_versions WHERE bot_id=? AND version=?",
                (bot_id, version),
            )
            if cur.rowcount == 0:
                return False
            # 若删的是当前版本，回退到剩余最新版本
            row = c.execute(
                "SELECT MAX(version) AS mv, binary_path, os, arch, format "
                "FROM bot_versions WHERE bot_id=?",
                (bot_id,),
            ).fetchone()
            if row and row["mv"]:
                c.execute(
                    "UPDATE bots SET current_version=?, binary_path=?, os=?, arch=?, "
                    "format=?, updated_at=? WHERE id=?",
                    (row["mv"], row["binary_path"], row["os"], row["arch"],
                     row["format"], _now(), bot_id),
                )
            return True

    def list_bot_versions(self, bot_id: int) -> list[dict]:
        with self._tx() as c:
            return [
                _row(r)
                for r in c.execute(
                    "SELECT * FROM bot_versions WHERE bot_id=? "
                    "ORDER BY version DESC",
                    (bot_id,),
                )
            ]

    def get_bot_version(self, version_id: int) -> dict | None:
        """按 version_id 取 bot_versions 行（含 binary_path，P1 版本冻结用）。"""
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM bot_versions WHERE id=?",
                    (version_id,),
                ).fetchone()
            )

    def get_latest_bot_version(self, bot_id: int) -> dict | None:
        """该 bot 的最新版本行（current_version 对应）。P1：发布轮冻结时取此快照。"""
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM bot_versions WHERE bot_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (bot_id,),
                ).fetchone()
            )

    def bot_profile(self, bot_id: int) -> dict | None:
        """聚合 Bot 详情：bot 信息 + owner + rating + 胜率 + 段位。

        不含对局历史与对手战绩（单独端点，避免单次返回过大）。
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT b.*, u.username AS owner_name, "
                "u.display_name AS owner_display, "
                "r.rating, r.rd, r.vol, r.wins, r.losses, r.draws, "
                "r.net_chips, r.matches_played, r.last_played_at AS rated_at "
                "FROM bots b "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "LEFT JOIN ratings r ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE b.id=?",
                (bot_id,),
            ).fetchone()
            d = _row(row)
            if d is not None:
                from bzplat.backend.games import registry as _game_registry
                t = _game_registry.tier_for(d.get("game_id") or "holdem", d.get("rating"))
                d["tier_level"] = t.level
                d["tier_key"] = t.key
                d["tier_name"] = t.name
            return d

    def bot_opponents_stats(
        self, bot_id: int, *, limit: int = 20
    ) -> list[dict]:
        """返回该 Bot 对各对手的战绩（按交手次数倒序），从 pair_stats 读。

        每行含 opponent_id/opponent_name/opponent_display/game_id/
        wins/losses/draws/samples/last_played_at（wins 从 bot_id 视角）。
        """
        with self._tx() as c:
            # bot 可能在 bot_a 或 bot_b 位
            rows = c.execute(
                "SELECT ps.bot_a_id, ps.bot_b_id, ps.a_wins, ps.a_losses, "
                "ps.draws, ps.samples, ps.last_played_at "
                "FROM pair_stats ps "
                "WHERE ps.bot_a_id=? OR ps.bot_b_id=? "
                "ORDER BY ps.samples DESC LIMIT ?",
                (bot_id, bot_id, max(1, min(limit, 100))),
            ).fetchall()
            out: list[dict] = []
            for r in rows:
                d = _row(r)
                a_id, b_id = d["bot_a_id"], d["bot_b_id"]
                opp_id = b_id if a_id == bot_id else a_id
                # 视角还原：若 bot 是 a，wins=a_wins；若 bot 是 b，wins=a_losses
                if bot_id == a_id:
                    wins, losses = d["a_wins"], d["a_losses"]
                else:
                    wins, losses = d["a_losses"], d["a_wins"]
                opp = c.execute(
                    "SELECT name, display_name, game_id FROM bots WHERE id=?",
                    (opp_id,),
                ).fetchone()
                out.append({
                    "opponent_id": opp_id,
                    "opponent_name": opp["name"] if opp else f"#{opp_id}",
                    "opponent_display": opp["display_name"] if opp else "",
                    "game_id": opp["game_id"] if opp else "",
                    "wins": wins,
                    "losses": losses,
                    "draws": d["draws"],
                    "samples": d["samples"],
                    "last_played_at": d["last_played_at"],
                })
            return out

    # ── matches（全面解耦 PR3：拆每游戏一张表 + matches_index 定位）─────

    def create_match(
        self,
        match_id: str,
        bot_a_id: int,
        bot_b_id: int,
        *,
        owner_id: int | None = None,
        contest_id: int | None = None,
        total_hands: int = 70,
        match_type: str = "challenge",
        game_id: str = "holdem",
        n_dots: int | None = None,
        human_user_id: int | None = None,
        human_seat: int | None = None,
    ) -> dict:
        gid = (game_id or "holdem").strip().lower()
        tbl = _matches_table(gid)
        with self._tx() as c:
            c.execute(
                f"INSERT INTO {tbl}(id, bot_a_id, bot_b_id, owner_id, "
                "contest_id, total_hands, match_type, status, game_id, n_dots, "
                "human_user_id, human_seat, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    match_id,
                    bot_a_id,
                    bot_b_id,
                    owner_id,
                    contest_id,
                    total_hands,
                    match_type,
                    "pending",
                    gid,
                    n_dots,
                    human_user_id,
                    human_seat,
                    _now(),
                ),
            )
            # 维护定位表
            c.execute(
                "INSERT OR REPLACE INTO matches_index(id, game_id) VALUES(?, ?)",
                (match_id, gid),
            )
            return _row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            )

    def _match_table_of(self, c, match_id: str) -> str | None:
        """经 matches_index 定位 match_id 所在的物理表；不存在返回 None。"""
        row = c.execute(
            "SELECT game_id FROM matches_index WHERE id=?", (match_id,)
        ).fetchone()
        if not row:
            return None
        return _matches_table(row["game_id"])

    def get_match(self, match_id: str) -> dict | None:
        with self._tx() as c:
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            return _row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            )

    def get_match_detailed(self, match_id: str) -> dict | None:
        """get_match + JOIN bots(ba/bb 名/display) + users(owner 名/display)。
        统一观赛/回放页座位身份显示用（bot_a/bot_b 各含 name/display_name +
        owner_name/owner_display）。人类对局(match_type=human)时 bot_a_id==bot_b_id
        复用同一 bot 行——人类侧靠 human_seat 区分（api 层标 is_human）。
        """
        with self._tx() as c:
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            sel = (
                "m.*, "
                "ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
                "ua.username AS bot_a_owner_name, ua.display_name AS bot_a_owner_display, "
                "ub.username AS bot_b_owner_name, ub.display_name AS bot_b_owner_display"
            )
            sql = (
                f"SELECT {sel} FROM {tbl} m "
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id "
                "LEFT JOIN users ua ON ba.owner_id=ua.id "
                "LEFT JOIN users ub ON bb.owner_id=ub.id "
                "WHERE m.id=?"
            )
            return _row(c.execute(sql, (match_id,)).fetchone())

    def update_match(self, match_id: str, **fields: Any) -> dict | None:
        allowed = {
            "hands_played",
            "earnings_a",
            "earnings_b",
            "winner",
            "reason",
            "net_bb_a",
            "status",
            "started_at",
            "ended_at",
            "contest_id",
            "n_dots",
            "human_user_id",
            "human_seat",
            "match_seed",
            "technical_loss",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            if sets:
                vals.append(match_id)
                c.execute(f"UPDATE {tbl} SET {','.join(sets)} WHERE id=?", vals)
            return _row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            )

    def delete_match(self, match_id: str) -> bool:
        """删除对局（经 matches_index 定位）：删 per-game 表行 + matches_index + replay。

        统一删除入口，保 matches_index 与 per-game 表不漂移（审计 P0：matches_index
        无清理会导致 like/view 计数静默 drift）。返回是否删到了行。
        """
        with self._tx() as c:
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return False
            cur = c.execute(f"DELETE FROM {tbl} WHERE id=?", (match_id,))
            deleted = cur.rowcount > 0
            if deleted:
                c.execute("DELETE FROM matches_index WHERE id=?", (match_id,))
                c.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
            return deleted

    def list_matches(
        self,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        owner_id: int | None = None,
        bot_id: int | None = None,
        *,
        contest_id: int | None = None,
        game_id: str | None = None,
    ) -> list[dict]:
        """列对局。game_id 指定时只查该游戏表；否则 UNION ALL 三表（跨游戏）。"""
        with self._tx() as c:
            join_bots = (
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id"
            )
            sel = (
                "m.*, ba.name AS bot_a_name, bb.name AS bot_b_name, "
                "ba.display_name AS bot_a_display, "
                "bb.display_name AS bot_b_display"
            )
            where_parts: list[str] = []
            params: list[Any] = []
            if owner_id is not None:
                where_parts.append("m.owner_id=?")
                params.append(owner_id)
            if bot_id is not None:
                where_parts.append("(m.bot_a_id=? OR m.bot_b_id=?)")
                params.extend([bot_id, bot_id])
            if contest_id is not None:
                where_parts.append("m.contest_id=?")
                params.append(contest_id)
            if status:
                where_parts.append("m.status=?")
                params.append(status)
            if game_id:
                where_parts.append("m.game_id=?")
                params.append(game_id)
            where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

            if game_id:
                # 单表查询
                tbl = _matches_table(game_id)
                sql = f"SELECT {sel} FROM {tbl} m {join_bots}{where_sql}"
                sql += " ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
                params.extend([limit, offset])
                return [_row(r) for r in c.execute(sql, params)]

            # 跨游戏：UNION ALL 三表，外层排序+分页
            subselects = []
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                subselects.append(
                    f"SELECT {sel} FROM {tbl} m {join_bots}{where_sql}"
                )
            union = " UNION ALL ".join(subselects)
            # UNION 后参数要按子查询数（=已注册游戏数）复制，每子查询一份 where 参数。
            # 不得硬编码 * 3——新增第 4 游戏会触发 Incorrect number of bindings。
            all_params = params * len(_all_game_ids())
            sql = f"SELECT * FROM ({union}) ORDER BY created_at DESC LIMIT ? OFFSET ?"
            all_params.extend([limit, offset])
            return [_row(r) for r in c.execute(sql, all_params)]

    def list_liked_top_matches(self, limit: int = 10) -> list[dict]:
        """对局点赞排行榜（跨三表 UNION ALL，likes_count>0 的已完成对局）。"""
        lim = max(1, min(limit, 50))
        with self._tx() as c:
            sel = (
                "m.id, m.game_id, m.status, m.winner, m.likes_count, "
                "m.views_count, m.created_at, "
                "ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display"
            )
            join = (
                "JOIN bots ba ON m.bot_a_id=ba.id "
                "JOIN bots bb ON m.bot_b_id=bb.id"
            )
            where = "WHERE m.status='completed' AND m.likes_count > 0"
            subs = []
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                subs.append(f"SELECT {sel} FROM {tbl} m {join} {where}")
            union = " UNION ALL ".join(subs)
            sql = f"SELECT * FROM ({union}) ORDER BY likes_count DESC, views_count DESC LIMIT ?"
            return [_row(r) for r in c.execute(sql, (lim,))]

    # ── match_replays ─────────────────────────────────────────

    def upsert_replay(
        self,
        match_id: str,
        events_json: str = "[]",
        hands_json: str = "[]",
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO match_replays(match_id, events_json, hands_json, "
                "updated_at) VALUES(?,?,?,?) "
                "ON CONFLICT(match_id) DO UPDATE SET "
                "events_json=excluded.events_json, "
                "hands_json=excluded.hands_json, "
                "updated_at=excluded.updated_at",
                (match_id, events_json, hands_json, _now()),
            )

    save_replay = upsert_replay

    def get_replay(self, match_id: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM match_replays WHERE match_id=?", (match_id,)
                ).fetchone()
            )

    # ── ratings（per-game：PK = bot_id + game_id，全面解耦 PR3）─────────

    def _bot_game_id(self, c, bot_id: int) -> str:
        """取 bot 绑定的 game_id（bot 绑定单一游戏）；缺失回退 holdem。"""
        row = c.execute(
            "SELECT game_id FROM bots WHERE id=?", (bot_id,)
        ).fetchone()
        return (row["game_id"] if row and row["game_id"] else "holdem")

    def ensure_rating(self, bot_id: int, *, game_id: str | None = None) -> dict:
        """确保 (bot_id, game_id) 评分行存在。game_id 缺省取 bot 的 game_id。"""
        with self._tx() as c:
            gid = (game_id or self._bot_game_id(c, bot_id)).strip().lower()
            existing = c.execute(
                "SELECT * FROM ratings WHERE bot_id=? AND game_id=?", (bot_id, gid)
            ).fetchone()
            if existing:
                return _row(existing)
            c.execute(
                "INSERT INTO ratings(bot_id, game_id) VALUES(?, ?)",
                (bot_id, gid),
            )
            return _row(
                c.execute(
                    "SELECT * FROM ratings WHERE bot_id=? AND game_id=?",
                    (bot_id, gid),
                ).fetchone()
            )

    def get_rating(self, bot_id: int, *, game_id: str | None = None) -> dict | None:
        """取 (bot_id, game_id) 评分行。game_id 缺省取 bot 的 game_id。"""
        with self._tx() as c:
            gid = (game_id or self._bot_game_id(c, bot_id)).strip().lower()
            return _row(
                c.execute(
                    "SELECT * FROM ratings WHERE bot_id=? AND game_id=?",
                    (bot_id, gid),
                ).fetchone()
            )

    def update_rating_row(
        self, bot_id: int, *, game_id: str | None = None, **fields: Any
    ) -> dict | None:
        """更新 (bot_id, game_id) 评分行；不存在则建。game_id 缺省取 bot 的 game_id。"""
        allowed = {
            "rating",
            "rd",
            "vol",
            "wins",
            "losses",
            "draws",
            "net_chips",
            "matches_played",
            "last_played_at",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            gid = (game_id or self._bot_game_id(c, bot_id)).strip().lower()
            existing = c.execute(
                "SELECT bot_id FROM ratings WHERE bot_id=? AND game_id=?",
                (bot_id, gid),
            ).fetchone()
            if not existing:
                c.execute(
                    "INSERT INTO ratings(bot_id, game_id) VALUES(?, ?)",
                    (bot_id, gid),
                )
            if sets:
                vals.extend([bot_id, gid])
                c.execute(
                    f"UPDATE ratings SET {','.join(sets)} WHERE bot_id=? AND game_id=?",
                    vals,
                )
            return _row(
                c.execute(
                    "SELECT * FROM ratings WHERE bot_id=? AND game_id=?",
                    (bot_id, gid),
                ).fetchone()
            )

    def list_leaderboard(
        self, limit: int = 50, *, game_id: str | None = None
    ) -> list[dict]:
        with self._tx() as c:
            # rating_delta = 当前 rating - 上一条历史评分（升降趋势）；无历史则 NULL
            # ratings/rating_history 现按 (bot_id, game_id) 复合键——join/subquery 都
            # 加 game_id 谓词（bot 绑定单一游戏，r.game_id=b.game_id 恰一行）。
            sql = (
                "SELECT r.bot_id, r.rating, r.rd, r.vol, r.wins, r.losses, "
                "r.draws, r.net_chips, r.matches_played, r.last_played_at, "
                "b.name AS bot_name, b.display_name AS bot_display, "
                "b.format, b.os, b.arch, b.is_builtin, b.game_id, "
                "u.username AS owner_name, u.display_name AS owner_display, "
                "(SELECT rh.rating FROM rating_history rh "
                " WHERE rh.bot_id=r.bot_id AND rh.game_id=r.game_id "
                " ORDER BY rh.id DESC LIMIT 1 OFFSET 1) AS prev_rating "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "WHERE b.is_active=1"
            )
            params: list[Any] = []
            if game_id:
                sql += " AND b.game_id=?"
                params.append(game_id)
            sql += " ORDER BY r.rating DESC LIMIT ?"
            params.append(limit)
            rows = [_row(r) for r in c.execute(sql, params)]
            # 计算并补 tier + delta（应用层，避免 SQL 嵌套过深）
            # 段位 per-game：按该 bot 的 game_id 取对应曲线（经 games 注册表）
            from bzplat.backend.games import registry as _game_registry
            for row in rows:
                prev = row.pop("prev_rating", None)
                if prev is not None:
                    row["rating_delta"] = round(row["rating"] - prev, 2)
                else:
                    row["rating_delta"] = None
                t = _game_registry.tier_for(row.get("game_id") or "holdem", row["rating"])
                row["tier_level"] = t.level
                row["tier_key"] = t.key
                row["tier_name"] = t.name
            return rows

    leaderboard = list_leaderboard

    def least_recently_played(
        self,
        game_id: str | None = None,
        *,
        limit: int = 100,
        stale_since: int | None = None,
        placement_games: int | None = None,
    ) -> list[dict]:
        """按陈旧度返回可对战 bot，供闲时自动对局挑选。

        - stale_since（秒，>0）：只返回 last_played_at 早于 now-stale_since 或从未赛（NULL）的 bot；
          None/0 = 不限。
        - placement_games（>0）：matches_played < 该值的「定级期」bot 排最前（新 bot 优先定级），
          其后按陈旧度（NULL 最前，再按时间升序）。
        仅返回 active+public+非内置且有二进制的 bot。
        """
        with self._tx() as c:
            sql = (
                "SELECT r.bot_id, r.rating, r.rd, r.matches_played, r.last_played_at, "
                "b.name AS bot_name, b.game_id, b.binary_path, b.is_active, b.is_builtin "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE b.is_active=1 AND b.is_builtin=0 "
                "AND b.binary_path!=''"
            )
            params: list[Any] = []
            if game_id:
                sql += " AND b.game_id=?"
                params.append(game_id)
            if stale_since and stale_since > 0:
                # last_played_at 早于 cutoff 或 NULL。
                # 注意：_now() 用本地时间，SQLite datetime('now') 是 UTC，故在 Python 算 cutoff。
                from datetime import datetime, timedelta
                cutoff = (datetime.now() - timedelta(seconds=int(stale_since))).isoformat(timespec="seconds")
                sql += " AND (r.last_played_at IS NULL OR r.last_played_at < ?)"
                params.append(cutoff)
            # 排序：定级期 bot 最前（若有），其后 NULL 最前、再按时间升序
            order = " ORDER BY "
            if placement_games and placement_games > 0:
                order += f"(r.matches_played < {int(placement_games)}) DESC, "
            order += "r.last_played_at IS NULL DESC, r.last_played_at ASC LIMIT ?"
            sql += order
            params.append(limit)
            return [_row(r) for r in c.execute(sql, params)]

    def count_matches(
        self,
        status: str | None = None,
        *,
        game_id: str | None = None,
    ) -> int:
        """按 status/game_id 统计对局数；与 list_matches 对齐——game_id 指定时只查该表，
        否则跨所有已注册游戏表求和。供分页器算 total 用。"""
        with self._tx() as c:
            where_parts: list[str] = []
            params: list[Any] = []
            if status:
                where_parts.append("status=?")
                params.append(status)
            where_sql = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""
            total = 0
            gids = [game_id] if game_id else _all_game_ids()
            for gid in gids:
                tbl = _matches_table(gid)
                row = c.execute(f"SELECT COUNT(*) FROM {tbl}{where_sql}", params).fetchone()
                total += int(row[0]) if row else 0
            return total

    def recover_orphan_matches(self) -> int:
        """启动时清理孤儿对局：把残留的 status=running（无对应内存协程）标 aborted。

        服务非正常退出后，DB 里 running 记录的内存 Task/Future 已丢失（尤其
        人类对局的 _human_turns），不清理会永久卡 running、泄漏并发与活跃用户计数。
        遍历三张 per-game 表清理。返回受影响行数。
        """
        from bzplat.backend.store.schema import STATUS_ABORTED

        with self._tx() as c:
            n = 0
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE status='running'"
                ).fetchone()
                cnt = int(row[0]) if row else 0
                if cnt:
                    c.execute(
                        f"UPDATE {tbl} SET status=?, reason='orphan_after_restart', "
                        "ended_at=datetime('now') WHERE status='running'",
                        (STATUS_ABORTED,),
                    )
                    n += cnt
            return n

    def upsert_rating(
        self,
        bot_id: int,
        rating: float,
        rd: float,
        vol: float,
        *,
        wins: int = 0,
        losses: int = 0,
        draws: int = 0,
        net_chips: int = 0,
        matches_played: int = 0,
        last_played_at: str | None = None,
    ) -> dict | None:
        return self.update_rating_row(
            bot_id,
            rating=rating,
            rd=rd,
            vol=vol,
            wins=wins,
            losses=losses,
            draws=draws,
            net_chips=net_chips,
            matches_played=matches_played,
            last_played_at=last_played_at or _now(),
        )

    # ── pair_stats ────────────────────────────────────────────

    def upsert_pair_stats(
        self,
        bot_a_id: int,
        bot_b_id: int,
        bb_per_100_mean: float,
        ci_low: float | None,
        ci_high: float | None,
        samples: int,
        *,
        a_wins_delta: int = 0,
        a_losses_delta: int = 0,
        draws_delta: int = 0,
    ) -> None:
        """记录双方对战统计。a_wins/a_losses 从 bot_a 视角计；

        bb_per_100_mean/ci 为 holdem 期望盈亏（可选，旧接口保留）；
        胜负计数增量式累加（a_wins_delta 等）。
        """
        with self._tx() as c:
            c.execute(
                "INSERT INTO pair_stats(bot_a_id, bot_b_id, bb_per_100_mean, "
                "ci_low, ci_high, samples, last_played_at, a_wins, a_losses, draws) "
                "VALUES(?,?,?,?,?,?,?, ?,?,?) "
                "ON CONFLICT(bot_a_id, bot_b_id) DO UPDATE SET "
                "bb_per_100_mean=excluded.bb_per_100_mean, "
                "ci_low=excluded.ci_low, ci_high=excluded.ci_high, "
                "samples=excluded.samples, "
                "last_played_at=excluded.last_played_at, "
                "a_wins=pair_stats.a_wins+excluded.a_wins, "
                "a_losses=pair_stats.a_losses+excluded.a_losses, "
                "draws=pair_stats.draws+excluded.draws",
                (
                    bot_a_id,
                    bot_b_id,
                    bb_per_100_mean,
                    ci_low,
                    ci_high,
                    samples,
                    _now(),
                    max(0, a_wins_delta),
                    max(0, a_losses_delta),
                    max(0, draws_delta),
                ),
            )

    def head_to_head(self, bot_a_id: int, bot_b_id: int) -> dict | None:
        """返回 bot_a 视角的对某对手战绩（a_wins/a_losses/draws/samples）。

        pair_stats 以 (min_id, max_id) 规范化存储，读取时按方向还原视角。
        """
        lo, hi = sorted((bot_a_id, bot_b_id))
        with self._tx() as c:
            row = c.execute(
                "SELECT a_wins, a_losses, draws, samples, last_played_at "
                "FROM pair_stats WHERE bot_a_id=? AND bot_b_id=?",
                (lo, hi),
            ).fetchone()
            if not row:
                return None
            d = _row(row)
            # 规范化存储时 bot_a = 小 id；若查询的 bot_a 是大 id，则胜负视角翻转
            if bot_a_id == lo:
                return d
            return {
                "a_wins": d["a_losses"],
                "a_losses": d["a_wins"],
                "draws": d["draws"],
                "samples": d["samples"],
                "last_played_at": d["last_played_at"],
            }

    def add_rating_history(
        self,
        bot_id: int,
        rating: float,
        rd: float,
        vol: float,
        matches_played: int,
        reason: str = "",
        *,
        game_id: str | None = None,
    ) -> None:
        """落一条评分快照（per-game），并截断保留每 (bot,game) 最近 N 条（N=200）。"""
        with self._tx() as c:
            gid = (game_id or self._bot_game_id(c, bot_id)).strip().lower()
            c.execute(
                "INSERT INTO rating_history(bot_id, game_id, rating, rd, vol, "
                "matches_played, reason, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (bot_id, gid, rating, rd, vol, matches_played, reason, _now()),
            )
            # 截断：保留每 (bot, game) 最近 200 条
            c.execute(
                "DELETE FROM rating_history WHERE bot_id=? AND game_id=? AND id NOT IN "
                "(SELECT id FROM rating_history WHERE bot_id=? AND game_id=? "
                "ORDER BY id DESC LIMIT 200)",
                (bot_id, gid, bot_id, gid),
            )

    def list_rating_history(
        self, bot_id: int, *, limit: int = 100, game_id: str | None = None
    ) -> list[dict]:
        """返回评分历史时序（旧→新，per-game），用于画曲线。"""
        with self._tx() as c:
            gid = (game_id or self._bot_game_id(c, bot_id)).strip().lower()
            rows = c.execute(
                "SELECT id, rating, rd, vol, matches_played, reason, created_at "
                "FROM rating_history WHERE bot_id=? AND game_id=? "
                "ORDER BY id DESC LIMIT ?",
                (bot_id, gid, max(1, min(limit, 500))),
            ).fetchall()
            return [_row(r) for r in reversed(rows)]

    # ── comments（评论）───────────────────────────────────────
    def add_comment(
        self, user_id: int, target_type: str, target_id: str, body: str
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO comments(target_type, target_id, user_id, body, "
                "created_at) VALUES(?,?,?,?,?)",
                (target_type, str(target_id), user_id, body, _now()),
            )
            cid = cur.lastrowid
            return _row(
                c.execute(
                    "SELECT c.*, u.username, u.display_name AS user_display "
                    "FROM comments c LEFT JOIN users u ON c.user_id=u.id "
                    "WHERE c.id=?",
                    (cid,),
                ).fetchone()
            )

    def list_comments(
        self, target_type: str, target_id: str, *, limit: int = 100
    ) -> list[dict]:
        with self._tx() as c:
            return [_row(r) for r in c.execute(
                "SELECT c.*, u.username, u.display_name AS user_display "
                "FROM comments c LEFT JOIN users u ON c.user_id=u.id "
                "WHERE c.target_type=? AND c.target_id=? "
                "ORDER BY c.id DESC LIMIT ?",
                (target_type, str(target_id), max(1, min(limit, 500))),
            )]

    def delete_comment(self, comment_id: int, user_id: int) -> bool:
        """仅作者或 admin 可删；返回是否删除成功。"""
        with self._tx() as c:
            row = c.execute(
                "SELECT user_id FROM comments WHERE id=?", (comment_id,)
            ).fetchone()
            if not row:
                return False
            cur = c.execute(
                "DELETE FROM comments WHERE id=? AND user_id=?",
                (comment_id, user_id),
            )
            return cur.rowcount > 0

    def delete_comment_admin(self, comment_id: int) -> bool:
        """admin 强删任意评论（无视作者）；返回是否删除成功（False=评论不存在）。"""
        with self._tx() as c:
            cur = c.execute("DELETE FROM comments WHERE id=?", (comment_id,))
            return cur.rowcount > 0

    def comment_exists(self, comment_id: int) -> bool:
        """只读探测评论是否存在（DELETE handler 区分 404 vs 403 用）。"""
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM comments WHERE id=?", (comment_id,)
            ).fetchone() is not None

    def comment_count(self, target_type: str, target_id: str) -> int:
        with self._tx() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM comments WHERE target_type=? AND target_id=?",
                (target_type, str(target_id)),
            ).fetchone()[0])

    # ── likes（点赞）──────────────────────────────────────────
    def like(
        self, user_id: int, target_type: str, target_id: str
    ) -> bool:
        """点赞；返回 True 表示新建。"""
        tid = str(target_id)
        with self._tx() as c:
            existing = c.execute(
                "SELECT 1 FROM likes WHERE user_id=? AND target_type=? AND target_id=?",
                (user_id, target_type, tid),
            ).fetchone()
            if existing:
                return False
            c.execute(
                "INSERT INTO likes(user_id, target_type, target_id, created_at) "
                "VALUES(?,?,?,?)",
                (user_id, target_type, tid, _now()),
            )
            # 对 match 点赞顺带 +1 计数（经 matches_index 定位到 per-game 表）
            if target_type == "match":
                tbl = self._match_table_of(c, tid)
                if tbl:
                    c.execute(
                        f"UPDATE {tbl} SET likes_count = likes_count + 1 WHERE id=?",
                        (tid,),
                    )
            return True

    def unlike(
        self, user_id: int, target_type: str, target_id: str
    ) -> bool:
        tid = str(target_id)
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM likes WHERE user_id=? AND target_type=? AND target_id=?",
                (user_id, target_type, tid),
            )
            if cur.rowcount > 0 and target_type == "match":
                tbl = self._match_table_of(c, tid)
                if tbl:
                    c.execute(
                        f"UPDATE {tbl} SET likes_count = MAX(0, likes_count - 1) WHERE id=?",
                        (tid,),
                    )
            return cur.rowcount > 0

    def is_liked(
        self, user_id: int, target_type: str, target_id: str
    ) -> bool:
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM likes WHERE user_id=? AND target_type=? AND target_id=?",
                (user_id, target_type, str(target_id)),
            ).fetchone() is not None

    def like_count(self, target_type: str, target_id: str) -> int:
        with self._tx() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM likes WHERE target_type=? AND target_id=?",
                (target_type, str(target_id)),
            ).fetchone()[0])

    def incr_match_view(self, match_id: str) -> None:
        with self._tx() as c:
            tbl = self._match_table_of(c, match_id)
            if tbl:
                c.execute(
                    f"UPDATE {tbl} SET views_count = views_count + 1 WHERE id=?",
                    (match_id,),
                )

    # ── matchpacks（对局数据集下载）──────────────────────────
    def matchpack_months(self, game_id: str | None = None) -> list[dict]:
        """列出有对局数据的游戏×月份分组（count），用于数据下载页。

        跨三表 UNION ALL（每表 game_id 固定，故 game_id 列直接取自表名）。
        """
        with self._tx() as c:
            base = (
                "SELECT game_id, substr(created_at,1,7) AS month, COUNT(*) AS cnt "
                "FROM {tbl} WHERE status='completed' AND created_at IS NOT NULL "
                "AND substr(created_at,1,7) <> '' GROUP BY game_id, substr(created_at,1,7)"
            )
            if game_id:
                # 单表
                tbl = _matches_table(game_id)
                sql = base.format(tbl=tbl) + " ORDER BY month DESC, game_id"
                return [_row(r) for r in c.execute(sql)]
            # 跨游戏 UNION ALL（每子查询 GROUP BY，空表不产生行）
            subs = [base.format(tbl=_matches_table(gid)) for gid in _all_game_ids()]
            union = " UNION ALL ".join(subs)
            sql = f"SELECT game_id, month, SUM(cnt) AS cnt FROM ({union}) GROUP BY game_id, month ORDER BY month DESC, game_id"
            return [_row(r) for r in c.execute(sql)]

    def matchpack_rows(
        self, game_id: str, month: str, *, limit: int = 10000
    ) -> list[dict]:
        """返回某游戏×月份的全部已完成对局（含 replay events），用于打包下载。

        game_id 必填，直接查该游戏表（match_replays 仍是全局单表，按 match_id join）。
        """
        with self._tx() as c:
            tbl = _matches_table(game_id)
            rows = c.execute(
                f"SELECT m.id, m.game_id, m.bot_a_id, m.bot_b_id, m.winner, "
                "m.earnings_a, m.earnings_b, m.hands_played, m.total_hands, "
                "m.match_type, m.created_at, m.ended_at, m.n_dots, "
                "ba.name AS bot_a_name, bb.name AS bot_b_name, "
                "r.events_json, r.hands_json "
                f"FROM {tbl} m LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id "
                "LEFT JOIN match_replays r ON r.match_id=m.id "
                "WHERE m.status='completed' AND m.game_id=? "
                "AND substr(m.created_at,1,7)=? "
                "ORDER BY m.created_at LIMIT ?",
                (game_id, month, max(1, min(limit, 50000))),
            ).fetchall()
            return [_row(r) for r in rows]

    # ── follows（关注关系）────────────────────────────────────
    def follow(self, follower_id: int, followee_id: int) -> bool:
        """关注；返回 True 表示新建关注，False 表示已存在。不能关注自己。"""
        if follower_id == followee_id:
            return False
        with self._tx() as c:
            existing = c.execute(
                "SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?",
                (follower_id, followee_id),
            ).fetchone()
            if existing:
                return False
            c.execute(
                "INSERT INTO follows(follower_id, followee_id, created_at) VALUES(?,?,?)",
                (follower_id, followee_id, _now()),
            )
            return True

    def unfollow(self, follower_id: int, followee_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM follows WHERE follower_id=? AND followee_id=?",
                (follower_id, followee_id),
            )
            return cur.rowcount > 0

    def is_following(self, follower_id: int, followee_id: int) -> bool:
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM follows WHERE follower_id=? AND followee_id=?",
                (follower_id, followee_id),
            ).fetchone() is not None

    def list_followers(self, user_id: int, *, limit: int = 50) -> list[dict]:
        with self._tx() as c:
            return [_row(r) for r in c.execute(
                "SELECT u.id, u.username, u.display_name, f.created_at "
                "FROM follows f JOIN users u ON f.follower_id=u.id "
                "WHERE f.followee_id=? ORDER BY f.created_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 200))),
            )]

    def list_following(self, user_id: int, *, limit: int = 50) -> list[dict]:
        with self._tx() as c:
            return [_row(r) for r in c.execute(
                "SELECT u.id, u.username, u.display_name, f.created_at "
                "FROM follows f JOIN users u ON f.followee_id=u.id "
                "WHERE f.follower_id=? ORDER BY f.created_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 200))),
            )]

    def follower_count(self, user_id: int) -> int:
        with self._tx() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM follows WHERE followee_id=?", (user_id,)
            ).fetchone()[0])

    def following_count(self, user_id: int) -> int:
        with self._tx() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM follows WHERE follower_id=?", (user_id,)
            ).fetchone()[0])

    # ── favorites（收藏 Bot）──────────────────────────────────
    def favorite(self, user_id: int, bot_id: int) -> bool:
        with self._tx() as c:
            existing = c.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND bot_id=?",
                (user_id, bot_id),
            ).fetchone()
            if existing:
                return False
            c.execute(
                "INSERT INTO favorites(user_id, bot_id, created_at) VALUES(?,?,?)",
                (user_id, bot_id, _now()),
            )
            return True

    def unfavorite(self, user_id: int, bot_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM favorites WHERE user_id=? AND bot_id=?", (user_id, bot_id)
            )
            return cur.rowcount > 0

    def is_favorite(self, user_id: int, bot_id: int) -> bool:
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM favorites WHERE user_id=? AND bot_id=?",
                (user_id, bot_id),
            ).fetchone() is not None

    def list_favorites(self, user_id: int, *, limit: int = 50) -> list[dict]:
        with self._tx() as c:
            return [_row(r) for r in c.execute(
                "SELECT b.id, b.name, b.display_name, b.game_id, "
                "u.username AS owner_name, u.display_name AS owner_display, "
                "r.rating, fav.created_at "
                "FROM favorites fav JOIN bots b ON fav.bot_id=b.id "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "LEFT JOIN ratings r ON r.bot_id=b.id AND r.game_id=b.game_id "
                "WHERE fav.user_id=? ORDER BY fav.created_at DESC LIMIT ?",
                (user_id, max(1, min(limit, 200))),
            )]

    def favorite_count(self, bot_id: int) -> int:
        with self._tx() as c:
            return int(c.execute(
                "SELECT COUNT(*) FROM favorites WHERE bot_id=?", (bot_id,)
            ).fetchone()[0])

    # ── notifications ─────────────────────────────────────────
    def add_notification(
        self,
        user_id: int,
        *,
        type: str = "",
        title: str = "",
        body: str = "",
        link: str = "",
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO notifications(user_id, type, title, body, link, "
                "is_read, created_at) VALUES(?,?,?,?,?,?,?)",
                (user_id, type, title, body, link, 0, _now()),
            )
            nid = cur.lastrowid
            return _row(
                c.execute("SELECT * FROM notifications WHERE id=?", (nid,)).fetchone()
            )

    def list_notifications(
        self,
        user_id: int,
        *,
        unread_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM notifications WHERE user_id=?"
            params: list[Any] = [user_id]
            if unread_only:
                sql += " AND is_read=0"
            sql += " ORDER BY id DESC LIMIT ? OFFSET ?"
            params.extend([max(1, min(limit, 200)), max(0, offset)])
            return [_row(r) for r in c.execute(sql, params)]

    def unread_notification_count(self, user_id: int) -> int:
        with self._tx() as c:
            return int(
                c.execute(
                    "SELECT COUNT(*) FROM notifications WHERE user_id=? AND is_read=0",
                    (user_id,),
                ).fetchone()[0]
            )

    def mark_notification_read(self, notif_id: int, user_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?",
                (notif_id, user_id),
            )
            return cur.rowcount > 0

    def mark_all_notifications_read(self, user_id: int) -> int:
        with self._tx() as c:
            cur = c.execute(
                "UPDATE notifications SET is_read=1 WHERE user_id=? AND is_read=0",
                (user_id,),
            )
            return cur.rowcount

    # ── notification_prefs ────────────────────────────────────
    _NOTIF_PREF_DEFAULTS = {
        "email_match_done": 0,
        "email_followed": 0,
        "email_contest": 0,
        "email_comment": 0,
    }

    def get_notification_prefs(self, user_id: int) -> dict:
        with self._tx() as c:
            row = c.execute(
                "SELECT email_match_done, email_followed, email_contest, "
                "email_comment FROM notification_prefs WHERE user_id=?",
                (user_id,),
            ).fetchone()
            if not row:
                # 懒建默认行
                c.execute(
                    "INSERT INTO notification_prefs(user_id) VALUES(?)",
                    (user_id,),
                )
                row = c.execute(
                    "SELECT email_match_done, email_followed, email_contest, "
                    "email_comment FROM notification_prefs WHERE user_id=?",
                    (user_id,),
                ).fetchone()
            return _row(row)

    def update_notification_prefs(self, user_id: int, **fields: Any) -> dict:
        allowed = {
            "email_match_done", "email_followed", "email_contest", "email_comment",
        }
        clean = {k: (1 if v else 0) for k, v in fields.items() if k in allowed}
        with self._tx() as c:
            existing = c.execute(
                "SELECT user_id FROM notification_prefs WHERE user_id=?", (user_id,)
            ).fetchone()
            if not existing:
                c.execute(
                    "INSERT INTO notification_prefs(user_id) VALUES(?)", (user_id,)
                )
            if clean:
                sets = ",".join(f"{k}=?" for k in clean)
                c.execute(
                    f"UPDATE notification_prefs SET {sets} WHERE user_id=?",
                    [*clean.values(), user_id],
                )
            # 内联读取（避免在 _tx 内递归调用 get_notification_prefs 死锁）
            row = c.execute(
                "SELECT email_match_done, email_followed, email_contest, "
                "email_comment FROM notification_prefs WHERE user_id=?",
                (user_id,),
            ).fetchone()
            return _row(row)

    # ── contests ──────────────────────────────────────────────

    def create_contest(
        self,
        title: str,
        organizer_id: int,
        *,
        description: str = "",
        registration_opens_at: str | None = None,
        registration_closes_at: str | None = None,
        starts_at: str | None = None,
        ends_at: str | None = None,
        hands_per_match: int = 70,
        status: str = "draft",
        game_id: str = "holdem",
        stages_json: str = "[]",
        template_id: str = "holdem_swiss_ko",
        current_stage_idx: int = 0,
        match_config_json: str = "{}",
        phase: str = "standalone",
        source_contest_id: int | None = None,
        require_real_name: int = 0,
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO contests(title, description, organizer_id, status, "
                "registration_opens_at, registration_closes_at, starts_at, "
                "ends_at, hands_per_match, created_at, game_id, stages_json, "
                "current_stage_idx, template_id, match_config_json, phase, "
                "source_contest_id, require_real_name) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    title,
                    description,
                    organizer_id,
                    status,
                    registration_opens_at,
                    registration_closes_at,
                    starts_at,
                    ends_at,
                    hands_per_match,
                    _now(),
                    game_id,
                    stages_json,
                    current_stage_idx,
                    template_id,
                    match_config_json,
                    phase,
                    source_contest_id,
                    require_real_name,
                ),
            )
            cid = cur.lastrowid
            return _row(
                c.execute("SELECT * FROM contests WHERE id=?", (cid,)).fetchone()
            )

    def get_contest(self, contest_id: int) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
            )

    def update_contest(self, contest_id: int, **fields: Any) -> dict | None:
        allowed = {
            "title",
            "description",
            "status",
            "registration_opens_at",
            "registration_closes_at",
            "starts_at",
            "ends_at",
            "hands_per_match",
            "game_id",
            "stages_json",
            "current_stage_idx",
            "template_id",
            "rest_ends_at",
            "match_config_json",
            "phase",
            "source_contest_id",
            "official_results_ready",
            "require_real_name",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            if sets:
                vals.append(contest_id)
                c.execute(
                    f"UPDATE contests SET {','.join(sets)} WHERE id=?", vals
                )
            return _row(
                c.execute(
                    "SELECT * FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
            )

    def list_contests(
        self, *, status: str | None = None, organizer_id: int | None = None,
        game_id: str | None = None,
    ) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM contests WHERE 1=1"
            params: list[Any] = []
            if status:
                sql += " AND status=?"
                params.append(status)
            if organizer_id is not None:
                sql += " AND organizer_id=?"
                params.append(organizer_id)
            if game_id:
                sql += " AND game_id=?"
                params.append(game_id)
            sql += " ORDER BY created_at DESC"
            return [_row(r) for r in c.execute(sql, params)]

    # ── contest_entries ───────────────────────────────────────

    def add_entry(self, contest_id: int, user_id: int, bot_id: int) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO contest_entries(contest_id, user_id, bot_id, "
                "registered_at) VALUES(?,?,?,?)",
                (contest_id, user_id, bot_id, _now()),
            )
            eid = cur.lastrowid
            return _row(
                c.execute(
                    "SELECT * FROM contest_entries WHERE id=?", (eid,)
                ).fetchone()
            )

    add_contest_entry = add_entry

    def update_entry(self, contest_id: int, user_id: int, **fields: Any) -> dict | None:
        allowed = {"bot_id", "group_id", "seed", "eliminated", "dispatched_at"}
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            if sets:
                vals.extend([contest_id, user_id])
                c.execute(
                    f"UPDATE contest_entries SET {','.join(sets)} "
                    "WHERE contest_id=? AND user_id=?",
                    vals,
                )
            return _row(
                c.execute(
                    "SELECT * FROM contest_entries WHERE contest_id=? AND user_id=?",
                    (contest_id, user_id),
                ).fetchone()
            )

    def list_entries(self, contest_id: int) -> list[dict]:
        with self._tx() as c:
            return [
                _row(r)
                for r in c.execute(
                    "SELECT * FROM contest_entries WHERE contest_id=? "
                    "ORDER BY registered_at",
                    (contest_id,),
                )
            ]

    list_contest_entries = list_entries

    def get_entry(self, contest_id: int, user_id: int) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM contest_entries WHERE contest_id=? AND user_id=?",
                    (contest_id, user_id),
                ).fetchone()
            )

    get_contest_entry = get_entry

    # ── contest_pairings ──────────────────────────────────────

    def add_pairing(
        self,
        contest_id: int,
        bot_a_id: int,
        bot_b_id: int,
        *,
        round_num: int = 1,
        match_id: str | None = None,
        status: str = "pending",
        stage_idx: int = 0,
        stage_key: str = "",
        group_id: str = "",
        bracket_slot: int | None = None,
        color_first: int = 0,
        entry_a_id: int | None = None,
        entry_b_id: int | None = None,
        bot_a_version_id: int | None = None,
        bot_b_version_id: int | None = None,
        pairing_seed: int | None = None,
        published_at: str | None = None,
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO contest_pairings(contest_id, round_num, entry_a_id, "
                "entry_b_id, bot_a_id, bot_b_id, bot_a_version_id, bot_b_version_id, "
                "pairing_seed, published_at, match_id, status, stage_idx, "
                "stage_key, group_id, bracket_slot, color_first) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    contest_id,
                    round_num,
                    entry_a_id,
                    entry_b_id,
                    bot_a_id,
                    bot_b_id,
                    bot_a_version_id,
                    bot_b_version_id,
                    pairing_seed,
                    published_at,
                    match_id,
                    status,
                    stage_idx,
                    stage_key,
                    group_id,
                    bracket_slot,
                    color_first,
                ),
            )
            pid = cur.lastrowid
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pid,)
                ).fetchone()
            )

    add_contest_pairing = add_pairing

    def list_pairings(
        self, contest_id: int, *, stage_idx: int | None = None
    ) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM contest_pairings WHERE contest_id=?"
            params: list[Any] = [contest_id]
            if stage_idx is not None:
                sql += " AND stage_idx=?"
                params.append(stage_idx)
            sql += " ORDER BY stage_idx, round_num, id"
            return [_row(r) for r in c.execute(sql, params)]

    list_contest_pairings = list_pairings

    def contest_bracket(self, contest_id: int) -> list[dict]:
        """返回对阵（带 bot 名/owner 名 + 对局 winner），便于前端画对阵图。

        每行含 pairing 全字段 + bot_a_name/bot_a_display/bot_b_name/bot_b_display
        + owner_a_name/owner_b_name + winner（从 matches 取）。
        """
        with self._tx() as c:
            # 赛事绑定单一游戏——取其 game_id 定位 per-game 对局表 join winner
            ct = c.execute(
                "SELECT game_id FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            gid = (ct["game_id"] if ct and ct["game_id"] else "holdem").strip().lower()
            tbl = _matches_table(gid)
            rows = c.execute(
                "SELECT p.*, ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
                "ua.username AS owner_a_name, ub.username AS owner_b_name, "
                "m.winner AS match_winner "
                "FROM contest_pairings p "
                "LEFT JOIN bots ba ON p.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON p.bot_b_id=bb.id "
                "LEFT JOIN users ua ON ba.owner_id=ua.id "
                "LEFT JOIN users ub ON bb.owner_id=ub.id "
                f"LEFT JOIN {tbl} m ON p.match_id=m.id "
                "WHERE p.contest_id=? "
                "ORDER BY p.stage_idx, p.round_num, p.id",
                (contest_id,),
            ).fetchall()
            return [_row(r) for r in rows]

    def contest_entries_named(self, contest_id: int) -> list[dict]:
        """返回报名（带 bot 名/owner 名 + seed/group/eliminated）。

        LEFT JOIN bots：bot_id 现可为 NULL（删 bot 后保留 entry，P0 SET NULL）。
        """
        with self._tx() as c:
            rows = c.execute(
                "SELECT e.*, b.name AS bot_name, b.display_name AS bot_display, "
                "b.game_id, u.username AS owner_name, u.display_name AS owner_display "
                "FROM contest_entries e "
                "LEFT JOIN bots b ON e.bot_id=b.id "
                "LEFT JOIN users u ON e.user_id=u.id "
                "WHERE e.contest_id=? ORDER BY e.seed, e.registered_at",
                (contest_id,),
            ).fetchall()
            return [_row(r) for r in rows]

    def update_pairing(self, pairing_id: int, **fields: Any) -> dict | None:
        allowed = {
            "match_id",
            "status",
            "round_num",
            "entry_a_id",
            "entry_b_id",
            "bot_a_id",
            "bot_b_id",
            "bot_a_version_id",
            "bot_b_version_id",
            "pairing_seed",
            "published_at",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            if sets:
                vals.append(pairing_id)
                c.execute(
                    f"UPDATE contest_pairings SET {','.join(sets)} WHERE id=?",
                    vals,
                )
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing_id,)
                ).fetchone()
            )

    update_contest_pairing = update_pairing

    # ── contest_stage_results ─────────────────────────────────

    def upsert_stage_result(
        self,
        contest_id: int,
        stage_idx: int,
        entry_id: int,
        *,
        bot_id: int | None = None,
        stage_key: str = "",
        points: float = 0,
        wins: int = 0,
        draws: int = 0,
        losses: int = 0,
        net_chips: int = 0,
        group_id: str = "",
        rank_in_group: int | None = None,
        payload_json: str = "{}",
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO contest_stage_results"
                "(contest_id, stage_idx, stage_key, entry_id, bot_id, points, wins, "
                "draws, losses, net_chips, group_id, rank_in_group, payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(contest_id, stage_idx, entry_id) DO UPDATE SET "
                "stage_key=excluded.stage_key, bot_id=excluded.bot_id, "
                "points=excluded.points, wins=excluded.wins, draws=excluded.draws, "
                "losses=excluded.losses, net_chips=excluded.net_chips, "
                "group_id=excluded.group_id, rank_in_group=excluded.rank_in_group, "
                "payload_json=excluded.payload_json",
                (
                    contest_id, stage_idx, stage_key, entry_id, bot_id, points,
                    wins, draws, losses, net_chips, group_id, rank_in_group,
                    payload_json,
                ),
            )

    def list_stage_results(
        self, contest_id: int, *, stage_idx: int | None = None
    ) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM contest_stage_results WHERE contest_id=?"
            params: list[Any] = [contest_id]
            if stage_idx is not None:
                sql += " AND stage_idx=?"
                params.append(stage_idx)
            sql += " ORDER BY stage_idx, points DESC, net_chips DESC"
            return [_row(r) for r in c.execute(sql, params)]

    # ── contest_official_results（P2 全员正式名次）─────────────

    def clear_official_results(self, contest_id: int) -> None:
        with self._tx() as c:
            c.execute(
                "DELETE FROM contest_official_results WHERE contest_id=?",
                (contest_id,),
            )

    def upsert_official_result(
        self,
        contest_id: int,
        entry_id: int,
        rank: int,
        *,
        stage_idx: int = 0,
        points: float = 0,
        bot_id: int | None = None,
        user_id: int | None = None,
        tiebreaks_json: str = "{}",
        awarded: str = "",
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO contest_official_results"
                "(contest_id, entry_id, stage_idx, rank, points, bot_id, user_id, "
                "tiebreaks_json, awarded) VALUES(?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(contest_id, entry_id) DO UPDATE SET "
                "stage_idx=excluded.stage_idx, rank=excluded.rank, "
                "points=excluded.points, bot_id=excluded.bot_id, "
                "user_id=excluded.user_id, tiebreaks_json=excluded.tiebreaks_json, "
                "awarded=excluded.awarded",
                (
                    contest_id, entry_id, stage_idx, rank, points, bot_id, user_id,
                    tiebreaks_json, awarded,
                ),
            )

    def list_official_results(self, contest_id: int) -> list[dict]:
        """全员正式名次（按 rank 升序，1..N 唯一连续）。"""
        with self._tx() as c:
            rows = c.execute(
                "SELECT r.*, b.name AS bot_name, b.display_name AS bot_display, "
                "u.username AS owner_name, u.display_name AS owner_display "
                "FROM contest_official_results r "
                "LEFT JOIN bots b ON r.bot_id=b.id "
                "LEFT JOIN users u ON r.user_id=u.id "
                "WHERE r.contest_id=? ORDER BY r.rank",
                (contest_id,),
            ).fetchall()
            return [_row(r) for r in rows]

    # ── contest_templates（赛制模板）──────────────────────────

    def list_contest_templates(self, *, game_id: str | None = None) -> list[dict]:
        with self._tx() as c:
            sql = "SELECT * FROM contest_templates"
            params: list[Any] = []
            if game_id:
                sql += " WHERE game_id=?"
                params.append(game_id)
            sql += " ORDER BY is_builtin DESC, id"
            rows = [_row(r) for r in c.execute(sql, params)]
        for r in rows:
            r["stages"] = _loads_json(r.get("stages_json"), default=[])
            r["match_config"] = _loads_json(r.get("match_config"), default={})
        return rows

    def get_contest_template(self, tid: str) -> dict | None:
        with self._tx() as c:
            r = _row(
                c.execute(
                    "SELECT * FROM contest_templates WHERE id=?", (tid,)
                ).fetchone()
            )
        if not r:
            return None
        r["stages"] = _loads_json(r.get("stages_json"), default=[])
        r["match_config"] = _loads_json(r.get("match_config"), default={})
        return r

    def upsert_contest_template(
        self,
        tid: str,
        *,
        name: str,
        game_id: str,
        match_config: dict | str,
        stages: list | str,
        is_builtin: bool = False,
    ) -> dict:
        mc_json = (
            match_config if isinstance(match_config, str) else json.dumps(match_config)
        )
        st_json = stages if isinstance(stages, str) else json.dumps(stages, ensure_ascii=False)
        with self._tx() as c:
            c.execute(
                "INSERT INTO contest_templates(id, name, game_id, match_config, "
                "stages_json, is_builtin, updated_at) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET name=excluded.name, "
                "game_id=excluded.game_id, match_config=excluded.match_config, "
                "stages_json=excluded.stages_json, updated_at=excluded.updated_at",
                (tid, name, game_id, mc_json, st_json, 1 if is_builtin else 0, _now()),
            )
            r = _row(
                c.execute(
                    "SELECT * FROM contest_templates WHERE id=?", (tid,)
                ).fetchone()
            )
        r["stages"] = _loads_json(r.get("stages_json"), default=[])
        r["match_config"] = _loads_json(r.get("match_config"), default={})
        return r

    def delete_contest_template(self, tid: str) -> bool:
        """删除非内置模板；内置模板返回 False。"""
        with self._tx() as c:
            r = c.execute(
                "SELECT is_builtin FROM contest_templates WHERE id=?", (tid,)
            ).fetchone()
            if not r:
                return False
            if r["is_builtin"]:
                return False
            cur = c.execute("DELETE FROM contest_templates WHERE id=?", (tid,))
            return cur.rowcount > 0

    # ── platform_settings ─────────────────────────────────────

    def get_setting(self, key: str) -> str | None:
        with self._tx() as c:
            row = c.execute(
                "SELECT value FROM platform_settings WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else None

    def set_setting(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO platform_settings(key, value, updated_at) "
                "VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "value=excluded.value, updated_at=excluded.updated_at",
                (key, value, _now()),
            )

    def get_settings(self, keys: list[str] | None = None) -> dict[str, str]:
        with self._tx() as c:
            if keys:
                placeholders = ",".join("?" * len(keys))
                rows = c.execute(
                    f"SELECT key, value FROM platform_settings WHERE key IN ({placeholders})",
                    keys,
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT key, value FROM platform_settings"
                ).fetchall()
            return {r[0]: r[1] for r in rows}

    def seed_setting_if_absent(self, key: str, value: str) -> None:
        with self._tx() as c:
            exists = c.execute(
                "SELECT 1 FROM platform_settings WHERE key=?", (key,)
            ).fetchone()
            if not exists:
                c.execute(
                    "INSERT INTO platform_settings(key, value, updated_at) "
                    "VALUES(?,?,?)",
                    (key, value, _now()),
                )

    # ── email_templates ───────────────────────────────────────

    def get_template(self, key: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM email_templates WHERE key=?", (key,)
                ).fetchone()
            )

    get_email_template = get_template

    def list_templates(self) -> list[dict]:
        with self._tx() as c:
            return [
                _row(r)
                for r in c.execute("SELECT * FROM email_templates ORDER BY key")
            ]

    list_email_templates = list_templates

    def update_template(
        self, key: str, *, subject: str, body_html: str, body_text: str
    ) -> dict:
        with self._tx() as c:
            c.execute(
                "INSERT INTO email_templates"
                "(key, subject, body_html, body_text, updated_at) "
                "VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET "
                "subject=excluded.subject, body_html=excluded.body_html, "
                "body_text=excluded.body_text, updated_at=excluded.updated_at",
                (key, subject, body_html, body_text, _now()),
            )
            return _row(
                c.execute(
                    "SELECT * FROM email_templates WHERE key=?", (key,)
                ).fetchone()
            )

    upsert_email_template = update_template

    # ── email_outbox ──────────────────────────────────────────

    def add_outbox(
        self,
        to_addr: str,
        subject: str,
        *,
        template_key: str = "",
        status: str = "sent",
        error: str = "",
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO email_outbox"
                "(to_addr, subject, template_key, status, error, created_at) "
                "VALUES(?,?,?,?,?,?)",
                (to_addr, subject, template_key, status, error, _now()),
            )

    add_email_outbox = add_outbox

    def list_outbox(
        self,
        *,
        status: str | None = None,
        template_key: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[dict]:
        """发件箱查询（管理员面板用）。"""
        with self._tx() as c:
            sql = "SELECT * FROM email_outbox WHERE 1=1"
            params: list[Any] = []
            if status:
                sql += " AND status=?"
                params.append(status)
            if template_key:
                sql += " AND template_key=?"
                params.append(template_key)
            sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            return [_row(r) for r in c.execute(sql, params)]

    # ── sessions 查询（管理端） ─────────────────────────────

    def list_sessions(
        self, user_id: int | None = None, *, limit: int = 100
    ) -> list[dict]:
        """列会话（可选按用户）。关联用户名便于展示。"""
        with self._tx() as c:
            sql = (
                "SELECT s.*, u.username FROM sessions s "
                "LEFT JOIN users u ON s.user_id=u.id WHERE 1=1"
            )
            params: list[Any] = []
            if user_id is not None:
                sql += " AND s.user_id=?"
                params.append(user_id)
            sql += " ORDER BY s.created_at DESC LIMIT ?"
            params.append(limit)
            return [_row(r) for r in c.execute(sql, params)]

    # ── 删除（管理端，schema 均 ON DELETE CASCADE） ─────────

    def delete_user(self, user_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM users WHERE id=?", (user_id,))
            return cur.rowcount > 0

    def delete_contest(self, contest_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute("DELETE FROM contests WHERE id=?", (contest_id,))
            return cur.rowcount > 0

    def delete_entry(self, contest_id: int, user_id: int) -> bool:
        with self._tx() as c:
            cur = c.execute(
                "DELETE FROM contest_entries WHERE contest_id=? AND user_id=?",
                (contest_id, user_id),
            )
            return cur.rowcount > 0

    # ── 聚合统计（仪表盘） ──────────────────────────────────

    def count_stats(self) -> dict:
        """一次性聚合各表计数 + 对局按状态分组 + 最近趋势。

        对局计数跨三张 per-game 表求和（全面解耦 PR3）。
        """
        with self._tx() as c:
            def one(sql: str, *p: Any) -> int:
                return int(c.execute(sql, p).fetchone()[0])

            def match_count(status: str | None = None) -> int:
                """跨三表统计对局数（可选 status 过滤）。"""
                total = 0
                for gid in _all_game_ids():
                    tbl = _matches_table(gid)
                    if status:
                        total += one(f"SELECT COUNT(*) FROM {tbl} WHERE status=?", status)
                    else:
                        total += one(f"SELECT COUNT(*) FROM {tbl}")
                return total

            stats = {
                "users": one("SELECT COUNT(*) FROM users"),
                "users_active": one("SELECT COUNT(*) FROM users WHERE is_active=1"),
                "users_verified": one("SELECT COUNT(*) FROM users WHERE email_verified=1"),
                "bots": one("SELECT COUNT(*) FROM bots"),
                "bots_active": one("SELECT COUNT(*) FROM bots WHERE is_active=1"),
                "matches": match_count(),
                "matches_completed": match_count("completed"),
                "matches_aborted": match_count("aborted"),
                "matches_running": match_count("running"),
                "matches_pending": match_count("pending"),
                "contests": one("SELECT COUNT(*) FROM contests"),
                "contests_running": one("SELECT COUNT(*) FROM contests WHERE status='running'"),
                "active_sessions": one(
                    "SELECT COUNT(*) FROM sessions WHERE expires_at > ?",
                    _now(),
                ),
            }
            # 按对局状态分组（跨三表 UNION ALL 再聚合）
            subs = [
                f"SELECT status, COUNT(*) AS n FROM {_matches_table(gid)} GROUP BY status"
                for gid in _all_game_ids()
            ]
            rows = c.execute(
                f"SELECT status, SUM(n) AS n FROM ({' UNION ALL '.join(subs)}) "
                "GROUP BY status"
            ).fetchall()
            stats["matches_by_status"] = {r["status"]: int(r["n"]) for r in rows}
            # 最近 7 天每日新对局数（跨三表）
            subs_recent = [
                f"SELECT substr(created_at,1,10) AS d, COUNT(*) AS n "
                f"FROM {_matches_table(gid)} WHERE created_at >= date('now','-7 days') "
                "GROUP BY substr(created_at,1,10)"
                for gid in _all_game_ids()
            ]
            recent = c.execute(
                f"SELECT d, SUM(n) AS n FROM ({' UNION ALL '.join(subs_recent)}) "
                "GROUP BY d ORDER BY d"
            ).fetchall()
            stats["matches_recent_daily"] = [
                {"date": r["d"], "count": int(r["n"])} for r in recent
            ]
            # 最近 5 个用户
            recent_users = c.execute(
                "SELECT id, username, email, role, created_at FROM users "
                "ORDER BY created_at DESC LIMIT 5"
            ).fetchall()
            stats["recent_users"] = [_row(r) for r in recent_users]
            return stats
