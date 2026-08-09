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

from .schema import (
    CODE_RESET,
    CONTEST_CANCELLED,
    CONTEST_DRAFT,
    CONTEST_FINISHED,
    CONTEST_OPEN,
    CONTEST_PUBLISHED,
    CONTEST_REST,
    CONTEST_RUNNING,
    MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,
    SCHEMA,
    STATUS_ABORTED,
    STATUS_COMPLETED,
    STATUS_PENDING,
    STATUS_RUNNING,
    TYPE_CONTEST,
    TYPE_HUMAN,
)

DEFAULT_DB_PATH = "botzone.db"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _row(row: sqlite3.Row | None) -> dict | None:
    return dict(row) if row is not None else None


def _parse_match_json_cols(m: dict | None) -> dict | None:
    """把 match 行的 match_config/result JSON 字符串列解析成 dict（消费方直接用）。

    无效/空 JSON → 空 dict。matches 表的 match_config/result 是双 JSON 通路
    （配置 + 结果详情），物理存 TEXT，逻辑是 dict——统一在此解析，避免各消费方重复 json.loads。
    """
    if m is None:
        return None
    for k in ("match_config", "result"):
        raw = m.get(k)
        if isinstance(raw, str):
            try:
                m[k] = json.loads(raw) if raw else {}
            except (ValueError, TypeError):
                m[k] = {}
        elif m.get(k) is None:
            m[k] = {}
    return m


def match_deltas(m: dict | None) -> tuple[int, int]:
    """从 match dict 的 result JSON 取双方净筹码/胜负分（deltas）。

    matches 表收敛后结果详情存 result JSON（{"deltas":[ea,eb],...}），取代旧的
    earnings_a/earnings_b 物理列。赛事排名（ranking/manager）经此 helper 统一读取，
    避免各处重复解析 JSON + 兜底缺字段。无 result 或 deltas 缺失 → (0, 0)。
    """
    if not m:
        return (0, 0)
    deltas = (m.get("result") or {}).get("deltas")
    if isinstance(deltas, list) and len(deltas) >= 2:
        try:
            return (int(deltas[0]), int(deltas[1]))
        except (TypeError, ValueError):
            return (0, 0)
    return (0, 0)


def _paginate(
    c: sqlite3.Connection,
    base_query: str,
    params: tuple,
    *,
    page: int = 1,
    per_page: int = 50,
) -> tuple[list[dict], int]:
    """通用分页 helper：返回 (rows, total)。

    ``base_query`` 是不含 LIMIT/OFFSET 的 SELECT（不含 ORDER BY 时在 COUNT 里自动裁剪）。
    自动算 total + 加 LIMIT/OFFSET。page 从 1 开始。
    """
    page = max(1, int(page))
    per_page = max(1, min(200, int(per_page)))  # 上限 200 防滥用
    offset = (page - 1) * per_page
    # total：把 SELECT ... 改成 SELECT COUNT(*)。粗略：取 FROM 之前替换。
    count_query = base_query
    # 简单启发：去掉 SELECT ... 到 FROM 之间的列，替换为 COUNT(*)
    from_idx = count_query.upper().find(" FROM ")
    if from_idx > 0:
        count_query = "SELECT COUNT(*)" + count_query[from_idx:]
    else:
        count_query = f"SELECT COUNT(*) FROM ({count_query})"
    # 去掉 ORDER BY（COUNT 不需要，且可能引用别名报错）
    ob_idx = count_query.upper().rfind(" ORDER BY ")
    if ob_idx > 0:
        count_query = count_query[:ob_idx]
    total = int(c.execute(count_query, params).fetchone()[0])
    rows = [
        _row(r) for r in c.execute(
            f"{base_query} LIMIT ? OFFSET ?", params + (per_page, offset)
        ).fetchall()
    ]
    return rows, total


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
    winner          INTEGER,
    reason          TEXT    NOT NULL DEFAULT 'completed',
    match_type      TEXT    NOT NULL DEFAULT 'challenge',
    status          TEXT    NOT NULL DEFAULT 'pending',
    game_id         TEXT    NOT NULL DEFAULT '{gdef}',
    match_config    TEXT    NOT NULL DEFAULT '{{}}',  -- 对局级配置 JSON（hands/n_dots 等），游戏无关；{{}} 经 .format 转义为字面空 JSON
    result          TEXT    NOT NULL DEFAULT '{{}}',  -- 对局结果详情 JSON（hands_played/deltas/net_bb）
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

    # ── contests CHECK 加 'published' 状态（时间编排：排期已发布、等待开赛）──
    # 重建表以放宽 CHECK（旧库 CHECK 不含 'published'，新赛事到点出排期会违反约束）。
    if "contests" in tables and "'published'" not in sql_text:
        cols = _table_cols(conn, "contests")
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
                phase TEXT NOT NULL DEFAULT 'standalone',
                source_contest_id INTEGER,
                official_results_ready INTEGER NOT NULL DEFAULT 0,
                require_real_name INTEGER NOT NULL DEFAULT 0,
                CONSTRAINT chk_contest_status CHECK (
                    status IN ('draft','open','published','running','rest','finished','cancelled'))
            )
            """
        )
        all_cols = [
            "id", "title", "description", "organizer_id", "status",
            "registration_opens_at", "registration_closes_at", "starts_at",
            "ends_at", "hands_per_match", "created_at", "game_id",
            "stages_json", "current_stage_idx", "template_id", "rest_ends_at",
            "match_config_json", "phase", "source_contest_id",
            "official_results_ready", "require_real_name",
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
            ("scheduled_at", "TEXT"),  # 计划开赛时间（逐场排期；NULL=立即可打）
        ):
            _add_col(conn, "contest_pairings", col, decl)

    if "bots" in tables:
        _add_col(conn, "bots", "game_id", "TEXT NOT NULL DEFAULT 'holdem'")
        # Botzone 运行模式（上传时标明，runner 据此选传输路径）
        _add_col(conn, "bots", "runtime_mode", "TEXT NOT NULL DEFAULT 'longrunning'")
        # 下线私有 bot 功能（全局只有「公开」一种状态）：旧库的 is_public 列先转公开
        # 再 DROP COLUMN（保数据不丢）。幂等：列已不存在则跳过。
        if "is_public" in _table_cols(conn, "bots"):
            conn.execute("UPDATE bots SET is_public=1 WHERE is_public=0")
            conn.execute("ALTER TABLE bots DROP COLUMN is_public")

    # bot_versions 加 runtime_mode（每版本独立标明，回滚时恢复该版本的运行模式）
    if "bot_versions" in tables:
        _add_col(conn, "bot_versions", "runtime_mode", "TEXT NOT NULL DEFAULT 'longrunning'")

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

    # Legacy contest_entries tables sometimes had only a plain contest_id index.
    # Concurrent registration uses ON CONFLICT(contest_id, user_id), which SQLite
    # rejects unless a real UNIQUE constraint/index exists.  Keep this after every
    # contest-table rebuild because rebuilding also drops the fresh-schema inline
    # UNIQUE constraint.
    if "contest_entries" in {
        r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }:
        # Historical duplicate registrations are corruption, but must not make an
        # upgrade impossible.  Preserve the earliest entry and repoint every durable
        # entry identity before removing duplicates.
        conn.execute("DROP TABLE IF EXISTS temp._contest_entry_dedup")
        conn.execute(
            "CREATE TEMP TABLE _contest_entry_dedup AS "
            "SELECT e.id AS drop_id, k.keep_id "
            "FROM contest_entries e "
            "JOIN (SELECT contest_id, user_id, MIN(id) AS keep_id "
            "      FROM contest_entries GROUP BY contest_id, user_id HAVING COUNT(*) > 1) k "
            "ON k.contest_id=e.contest_id AND k.user_id=e.user_id "
            "WHERE e.id<>k.keep_id"
        )
        _current_tables = {
            r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        if "contest_pairings" in _current_tables:
            for _entry_col in ("entry_a_id", "entry_b_id"):
                if _entry_col in _table_cols(conn, "contest_pairings"):
                    conn.execute(
                        f"UPDATE contest_pairings SET {_entry_col}=("
                        "SELECT keep_id FROM _contest_entry_dedup d "
                        f"WHERE d.drop_id=contest_pairings.{_entry_col}) "
                        f"WHERE {_entry_col} IN (SELECT drop_id FROM _contest_entry_dedup)"
                    )
        if (
            "contest_stage_results" in _current_tables
            and "entry_id" in _table_cols(conn, "contest_stage_results")
        ):
            # Resolve every row to its final keeper identity before the bulk UPDATE.
            # With 3+ duplicate entries, two drop rows may both have a result while
            # the keeper has none; checking only for an existing keeper row leaves
            # both rows alive and the UPDATE then violates the table UNIQUE key.
            # Prefer an existing keeper row, otherwise preserve the earliest result.
            conn.execute(
                "DELETE FROM contest_stage_results AS duplicate "
                "WHERE duplicate.entry_id IN (SELECT drop_id FROM _contest_entry_dedup) "
                "AND EXISTS (SELECT 1 FROM contest_stage_results preferred "
                "LEFT JOIN _contest_entry_dedup preferred_map "
                "ON preferred_map.drop_id=preferred.entry_id "
                "JOIN _contest_entry_dedup duplicate_map "
                "ON duplicate_map.drop_id=duplicate.entry_id "
                "WHERE preferred.contest_id=duplicate.contest_id "
                "AND preferred.stage_idx=duplicate.stage_idx "
                "AND COALESCE(preferred_map.keep_id, preferred.entry_id)="
                "duplicate_map.keep_id "
                "AND (preferred.entry_id=duplicate_map.keep_id "
                "OR preferred.id<duplicate.id))"
            )
            conn.execute(
                "UPDATE contest_stage_results SET entry_id=("
                "SELECT keep_id FROM _contest_entry_dedup d "
                "WHERE d.drop_id=contest_stage_results.entry_id) "
                "WHERE entry_id IN (SELECT drop_id FROM _contest_entry_dedup)"
            )
        if (
            "contest_official_results" in _current_tables
            and "entry_id" in _table_cols(conn, "contest_official_results")
        ):
            conn.execute(
                "DELETE FROM contest_official_results AS duplicate "
                "WHERE duplicate.entry_id IN (SELECT drop_id FROM _contest_entry_dedup) "
                "AND EXISTS (SELECT 1 FROM contest_official_results preferred "
                "LEFT JOIN _contest_entry_dedup preferred_map "
                "ON preferred_map.drop_id=preferred.entry_id "
                "JOIN _contest_entry_dedup duplicate_map "
                "ON duplicate_map.drop_id=duplicate.entry_id "
                "WHERE preferred.contest_id=duplicate.contest_id "
                "AND COALESCE(preferred_map.keep_id, preferred.entry_id)="
                "duplicate_map.keep_id "
                "AND (preferred.entry_id=duplicate_map.keep_id "
                "OR preferred.id<duplicate.id))"
            )
            conn.execute(
                "UPDATE contest_official_results SET entry_id=("
                "SELECT keep_id FROM _contest_entry_dedup d "
                "WHERE d.drop_id=contest_official_results.entry_id) "
                "WHERE entry_id IN (SELECT drop_id FROM _contest_entry_dedup)"
            )
        conn.execute(
            "DELETE FROM contest_entries "
            "WHERE id IN (SELECT drop_id FROM _contest_entry_dedup)"
        )
        conn.execute("DROP TABLE temp._contest_entry_dedup")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_contest_entries_contest_user "
            "ON contest_entries(contest_id, user_id)"
        )

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
        # match_config + result 双 JSON 通路收敛（删 total_hands/n_dots/hands_played/
        # earnings_a/earnings_b/net_bb_a 6 个游戏专属固定列）。旧数据整理进 JSON 再 DROP
        # （复用 db.py:398-402 bots.is_public 的 DROP COLUMN 模式）。幂等：列已不存在则跳过。
        _add_col(conn, _tbl, "match_config", "TEXT NOT NULL DEFAULT '{}'")
        _add_col(conn, _tbl, "result", "TEXT NOT NULL DEFAULT '{}'")
        _mcols = _table_cols(conn, _tbl)
        if "total_hands" in _mcols:
            # 配置：total_hands → match_config.hands（按行原值）
            conn.execute(
                f"UPDATE {_tbl} SET match_config=json_set(match_config,'$.hands',total_hands) "
                "WHERE total_hands IS NOT NULL"
            )
            conn.execute(f"ALTER TABLE {_tbl} DROP COLUMN total_hands")
        if "n_dots" in _mcols:
            # 配置：n_dots → match_config.n_dots（pencil 专属，按行原值）
            conn.execute(
                f"UPDATE {_tbl} SET match_config=json_set(match_config,'$.n_dots',n_dots) "
                "WHERE n_dots IS NOT NULL"
            )
            conn.execute(f"ALTER TABLE {_tbl} DROP COLUMN n_dots")
        if "hands_played" in _mcols or "earnings_a" in _mcols:
            # 结果：hands_played + earnings_a/b → result.{hands_played,deltas}（按行原值）
            # 注意：WHERE 只判 IS NOT NULL，保留零值行——零手判负（bot 第一手崩）的
            # rounds_played=0/earnings=0 行也必须迁移，否则 result 会丢成 '{}'。
            conn.execute(
                f"UPDATE {_tbl} SET result=json_set(result,'$.hands_played',hands_played) "
                "WHERE hands_played IS NOT NULL"
            )
            conn.execute(
                f"UPDATE {_tbl} SET result=json_set(result,'$.deltas',json_array(earnings_a,earnings_b)) "
                "WHERE earnings_a IS NOT NULL OR earnings_b IS NOT NULL"
            )
            for _dead in ("hands_played", "earnings_a", "earnings_b", "net_bb_a"):
                if _dead in _table_cols(conn, _tbl):
                    conn.execute(f"ALTER TABLE {_tbl} DROP COLUMN {_dead}")

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

    # ── 非赛事 completed 对局评分结算凭据（恰好一次）────────────────────
    # 升级前的 completed 对局大多已经由旧后处理更新过 ratings，但没有 marker。
    # 若直接让启动恢复扫描它们，会把全部历史评分重复计算。首次迁移先把既有
    # completed 非赛事/非 human 对局回填为已结算，再写哨兵；二者随外层事务
    # 一起提交，失败可安全重试。新库首次初始化时没有对局，只写哨兵。
    migrated = conn.execute(
        "SELECT 1 FROM match_rating_settlements WHERE match_id=?",
        (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL,),
    ).fetchone()
    if not migrated:
        migrated_at = _now()
        for _gid in _all_game_ids():
            _tbl = _matches_table(_gid)
            # 新游戏注册与物理表漂移由 Store.__init__ 的既有一致性断言给出
            # 明确诊断；迁移回填不能抢先以 no-such-table 模糊该错误。
            if _tbl not in _tables_after:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO match_rating_settlements(match_id, settled_at) "
                f"SELECT id, COALESCE(ended_at, created_at, ?) FROM {_tbl} "
                "WHERE status=? AND match_type NOT IN (?,?)",
                (migrated_at, STATUS_COMPLETED, TYPE_CONTEST, TYPE_HUMAN),
            )
        conn.execute(
            "INSERT INTO match_rating_settlements(match_id, settled_at) VALUES(?,?)",
            (MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL, migrated_at),
        )


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
        """按对局 ID 或 bot 名模糊搜索已完成对局。"""
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
                "AND (LOWER(m.id) LIKE ? OR LOWER(ba.name) LIKE ? OR LOWER(bb.name) LIKE ? "
                "OR LOWER(ba.display_name) LIKE ? OR LOWER(bb.display_name) LIKE ?)"
            )
            params: list[Any] = [ql, ql, ql, ql, ql]
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
        self, *, role: str | None = None, active_only: bool = False,
        q: str | None = None, real_name: bool | None = None,
        page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        with self._tx() as c:
            sql = "SELECT * FROM users WHERE 1=1"
            params: list[Any] = []
            if role:
                sql += " AND role=?"
                params.append(role)
            if active_only:
                sql += " AND is_active=1"
            if q:
                sql += " AND (LOWER(username) LIKE ? OR LOWER(email) LIKE ?)"
                like = f"%{q.strip().lower()}%"
                params.extend((like, like))
            if real_name is not None:
                complete = (
                    "TRIM(COALESCE(real_name,''))<>'' AND "
                    "TRIM(COALESCE(phone,''))<>'' AND "
                    "TRIM(COALESCE(school,''))<>'' AND "
                    "TRIM(COALESCE(student_id,''))<>''"
                )
                sql += f" AND ({complete})" if real_name else f" AND NOT ({complete})"
            sql += " ORDER BY created_at"
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, tuple(params), page=page, per_page=pp)
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
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

    def reset_password_with_credential(
        self,
        user_id: int,
        password_hash: str,
        *,
        email_code_id: int | None = None,
        email_code: str | None = None,
        reset_token: str | None = None,
    ) -> str:
        """原子消费一次性凭据、更新密码并撤销该用户的全部会话。

        邮箱验证码与管理员重置 token 二选一。返回 ``ok``、``invalid`` 或
        ``expired``；``invalid`` 同时涵盖不存在、已使用和最终 CAS 竞争失败。
        凭据 CAS、密码更新与 session 删除共享同一个 ``BEGIN IMMEDIATE``
        事务，后两步异常时凭据消费也会随事务回滚。
        """
        email_selected = email_code_id is not None or email_code is not None
        token_selected = reset_token is not None
        if email_selected == token_selected:
            raise ValueError("邮箱验证码和重置 token 必须且只能提供一种")
        if email_selected and (email_code_id is None or email_code is None):
            raise ValueError("邮箱验证码 id 与 code 必须同时提供")

        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            used_at = _now()
            checked_at = datetime.now()
            expiry_cutoff = checked_at.isoformat(timespec="microseconds")
            if email_selected:
                credential = c.execute(
                    "SELECT user_id, expires_at, used_at FROM email_codes "
                    "WHERE id=? AND user_id=? AND purpose=? AND code=?",
                    (email_code_id, user_id, CODE_RESET, email_code),
                ).fetchone()
                if not credential or credential["used_at"] is not None:
                    return "invalid"
                try:
                    expired = (
                        datetime.fromisoformat(credential["expires_at"])
                        < checked_at
                    )
                except (TypeError, ValueError):
                    return "invalid"
                if expired:
                    return "expired"
                consume = c.execute(
                    "UPDATE email_codes SET used_at=? "
                    "WHERE id=? AND user_id=? AND purpose=? AND code=? "
                    "AND used_at IS NULL AND expires_at>=?",
                    (
                        used_at,
                        email_code_id,
                        user_id,
                        CODE_RESET,
                        email_code,
                        expiry_cutoff,
                    ),
                )
            else:
                credential = c.execute(
                    "SELECT user_id, expires_at, used_at FROM password_resets "
                    "WHERE token=? AND user_id=?",
                    (reset_token, user_id),
                ).fetchone()
                if not credential or credential["used_at"] is not None:
                    return "invalid"
                try:
                    expired = (
                        datetime.fromisoformat(credential["expires_at"])
                        < checked_at
                    )
                except (TypeError, ValueError):
                    return "invalid"
                if expired:
                    return "expired"
                consume = c.execute(
                    "UPDATE password_resets SET used_at=? "
                    "WHERE token=? AND user_id=? AND used_at IS NULL "
                    "AND expires_at>=?",
                    (used_at, reset_token, user_id, expiry_cutoff),
                )

            if consume.rowcount != 1:
                return "invalid"
            updated = c.execute(
                "UPDATE users SET password_hash=? WHERE id=?",
                (password_hash, user_id),
            )
            if updated.rowcount != 1:
                raise RuntimeError("重置密码时用户记录不存在")
            c.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            return "ok"

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
        is_active = 1 if fields.get("is_active", True) else 0
        game_id = fields.get("game_id") or "holdem"
        from bzplat.backend.store.schema import DEFAULT_RUNTIME_MODE, VALID_RUNTIME_MODES
        runtime_mode = fields.get("runtime_mode") or DEFAULT_RUNTIME_MODE
        if runtime_mode not in VALID_RUNTIME_MODES:
            raise ValueError(f"非法 runtime_mode: {runtime_mode}")
        now = _now()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO bots(owner_id, name, display_name, description, "
                "os, arch, format, binary_path, is_builtin, is_active, game_id, runtime_mode, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    is_active,
                    game_id,
                    runtime_mode,
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
            "runtime_mode",
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
        # 注意：此处不做「活跃引用」业务校验——那是 admin_delete_bot 端点的职责（业务规则）。
        # 本方法保持纯 store 行为：直接删，FK ON DELETE SET NULL（matches，保历史）/ CASCADE
        # （contest_pairings）由 DB 处理。管理端须改调 delete_bot_if_safe() 原子判断。
        with self._tx() as c:
            return c.execute("DELETE FROM bots WHERE id=?", (bot_id,)).rowcount > 0

    def delete_bot_if_safe(self, bot_id: int) -> dict:
        """在一个写事务内检查活跃引用并硬删 Bot，消除 check→delete 竞态。"""
        active_contest_statuses = (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
            CONTEST_REST,
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT id FROM bots WHERE id=?", (bot_id,)).fetchone():
                return {"found": False, "deleted": False, "references": {}}

            match_count = 0
            for gid in _all_game_ids():
                table = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) AS n FROM {table} "
                    "WHERE (bot_a_id=? OR bot_b_id=?) AND status IN (?,?)",
                    (bot_id, bot_id, STATUS_PENDING, STATUS_RUNNING),
                ).fetchone()
                match_count += int(row["n"] if row else 0)

            status_marks = ",".join("?" for _ in active_contest_statuses)
            pairing_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_pairings pairing "
                "JOIN contests contest ON contest.id=pairing.contest_id "
                "WHERE (pairing.bot_a_id=? OR pairing.bot_b_id=?) "
                f"AND contest.status IN ({status_marks})",
                (bot_id, bot_id, *active_contest_statuses),
            ).fetchone()
            entry_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_entries entry "
                "JOIN contests contest ON contest.id=entry.contest_id "
                "WHERE entry.bot_id=? "
                f"AND contest.status IN ({status_marks})",
                (bot_id, *active_contest_statuses),
            ).fetchone()
            refs = {
                "matches": match_count,
                "pairings": int(pairing_row["n"] if pairing_row else 0)
                + int(entry_row["n"] if entry_row else 0),
            }
            if any(refs.values()):
                return {"found": True, "deleted": False, "references": refs}
            deleted = c.execute("DELETE FROM bots WHERE id=?", (bot_id,)).rowcount > 0
            return {"found": True, "deleted": deleted, "references": refs}

    def bot_active_references(self, bot_id: int) -> dict:
        """检查 bot 是否被**活跃**对局/赛事引用（会因此被破坏才拒绝）。

        返回 {matches: n, pairings: n}，全 0 表示可安全硬删。
        注意：已完成的历史对局/赛事（status=completed/finished）不阻拦——FK SET NULL 会
        保留历史（bot_id→NULL），这是预期行为（见 test_delete_bot_preserves_contest_data）。
        仅阻拦 pending/running 对局 + 未完成赛事的报名/对阵（硬删会破坏进行中的赛事）。
        """
        out = {"matches": 0, "pairings": 0}
        with self._tx() as c:
            # 活跃对局：pending/running 状态（跨每游戏表）
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) AS n FROM {tbl} "
                    f"WHERE (bot_a_id=? OR bot_b_id=?) AND status IN ('pending','running')",
                    (bot_id, bot_id),
                ).fetchone()
                if row and row["n"]:
                    out["matches"] += int(row["n"])
            # 进行中赛事（running/published/rest）的报名/对阵：硬删会破坏对阵表（CASCADE）。
            # draft/open 的报名可重建（用户重新报名），不阻拦；finished 的历史 SET NULL 保留。
            row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_pairings cp "
                "JOIN contests c ON c.id=cp.contest_id "
                "WHERE (cp.bot_a_id=? OR cp.bot_b_id=?) "
                "AND c.status IN ('published','running','rest')",
                (bot_id, bot_id),
            ).fetchone()
            if row and row["n"]:
                out["pairings"] += int(row["n"])
            row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_entries ce "
                "JOIN contests c ON c.id=ce.contest_id "
                "WHERE ce.bot_id=? AND c.status IN ('published','running','rest')",
                (bot_id,),
            ).fetchone()
            if row and row["n"]:
                out["pairings"] += int(row["n"])
        return out

    def list_bots(
        self,
        owner_id: int | None = None,
        *,
        active_only: bool = True,
        include_builtin: bool = True,
        game_id: str | None = None,
        page: int | None = None,
        per_page: int = 50,
    ) -> list[dict] | dict:
        """列 bot。``page`` 为 None 时返回 list（旧契约，部分调用方需全量）；
        ``page`` 给定时返回 ``{"items", "page", "per_page", "total"}``。"""
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
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, tuple(params), page=page, per_page=pp)
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
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
        runtime_mode: str | None = None,
        version: int | None = None,
    ) -> dict:
        from bzplat.backend.store.schema import DEFAULT_RUNTIME_MODE, VALID_RUNTIME_MODES
        if runtime_mode is None:
            # 沿用 bot 当前的运行模式（回滚/补传时不强制改模式）
            runtime_mode = self.get_bot(bot_id) or {}
            runtime_mode = runtime_mode.get("runtime_mode") or DEFAULT_RUNTIME_MODE
        if runtime_mode not in VALID_RUNTIME_MODES:
            raise ValueError(f"非法 runtime_mode: {runtime_mode}")
        with self._tx() as c:
            if version is None:
                row = c.execute(
                    "SELECT MAX(version) AS mv FROM bot_versions WHERE bot_id=?",
                    (bot_id,),
                ).fetchone()
                version = (row["mv"] or 0) + 1
            cur = c.execute(
                "INSERT INTO bot_versions(bot_id, version, binary_path, "
                "upload_note, checksum, size_bytes, os, arch, format, runtime_mode, "
                "uploaded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
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
                    runtime_mode,
                    _now(),
                ),
            )
            vid = cur.lastrowid
            c.execute(
                "UPDATE bots SET current_version=?, binary_path=?, os=?, arch=?, "
                "format=?, runtime_mode=?, updated_at=? WHERE id=?",
                (version, binary_path, os, arch, format, runtime_mode, _now(), bot_id),
            )
            return _row(
                c.execute("SELECT * FROM bot_versions WHERE id=?", (vid,)).fetchone()
            )

    def delete_bot_version(self, bot_id: int, version: int) -> bool:
        """删除指定版本；若删的是当前版本，回退到 max(version)（含 runtime_mode）。

        删非当前版本时**不动 bots 镜像**——否则会覆盖用户主动回滚到的旧版本状态。
        """
        with self._tx() as c:
            # 先读当前版本，判定删的是否是当前版本
            cur_bot = c.execute(
                "SELECT current_version FROM bots WHERE id=?", (bot_id,)
            ).fetchone()
            is_current = cur_bot and cur_bot["current_version"] == version

            cur = c.execute(
                "DELETE FROM bot_versions WHERE bot_id=? AND version=?",
                (bot_id, version),
            )
            if cur.rowcount == 0:
                return False
            # 仅当删的是当前版本，才回退镜像到剩余最新版本
            if is_current:
                row = c.execute(
                    "SELECT MAX(version) AS mv, binary_path, os, arch, format, runtime_mode "
                    "FROM bot_versions WHERE bot_id=?",
                    (bot_id,),
                ).fetchone()
                if row and row["mv"]:
                    c.execute(
                        "UPDATE bots SET current_version=?, binary_path=?, os=?, arch=?, "
                        "format=?, runtime_mode=?, updated_at=? WHERE id=?",
                        (row["mv"], row["binary_path"], row["os"], row["arch"],
                         row["format"], row["runtime_mode"], _now(), bot_id),
                    )
            return True

    def set_current_version(self, bot_id: int, version: int) -> dict | None:
        """回滚到指定版本（不删除其他版本）：把 bots 镜像切到该版本的
        binary_path/os/arch/format/runtime_mode，current_version=version。

        用于 MyBots「回滚到此版本」。版本不存在返回 None。
        """
        with self._tx() as c:
            row = c.execute(
                "SELECT version, binary_path, os, arch, format, runtime_mode "
                "FROM bot_versions WHERE bot_id=? AND version=?",
                (bot_id, version),
            ).fetchone()
            if not row:
                return None
            c.execute(
                "UPDATE bots SET current_version=?, binary_path=?, os=?, arch=?, "
                "format=?, runtime_mode=?, updated_at=? WHERE id=?",
                (row["version"], row["binary_path"], row["os"], row["arch"],
                 row["format"], row["runtime_mode"], _now(), bot_id),
            )
            return _row(c.execute("SELECT * FROM bots WHERE id=?", (bot_id,)).fetchone())

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
        """该 bot 历史中版本号最大的版本行（不一定是当前激活版本）。"""
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM bot_versions WHERE bot_id=? "
                    "ORDER BY version DESC LIMIT 1",
                    (bot_id,),
                ).fetchone()
            )

    def get_current_bot_version(self, bot_id: int) -> dict | None:
        """取 ``bots.current_version`` 当前激活版本对应的版本行。

        与 ``get_latest_bot_version``（历史最大版本，用于下一个上传版本号）语义
        刻意分离：用户回滚后，赛事发布必须冻结当前激活版本而非历史最大版本。
        """
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT v.* FROM bots b "
                    "JOIN bot_versions v "
                    "ON v.bot_id=b.id AND v.version=b.current_version "
                    "WHERE b.id=?",
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
        match_type: str = "challenge",
        game_id: str = "holdem",
        match_config: dict | None = None,
        human_user_id: int | None = None,
        human_seat: int | None = None,
    ) -> dict:
        gid = (game_id or "holdem").strip().lower()
        tbl = _matches_table(gid)
        mc_json = json.dumps(match_config or {}, ensure_ascii=False)
        with self._tx() as c:
            c.execute(
                f"INSERT INTO {tbl}(id, bot_a_id, bot_b_id, owner_id, "
                "contest_id, match_type, status, game_id, match_config, "
                "human_user_id, human_seat, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    match_id,
                    bot_a_id,
                    bot_b_id,
                    owner_id,
                    contest_id,
                    match_type,
                    "pending",
                    gid,
                    mc_json,
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
            return _parse_match_json_cols(_row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            ))

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
            return _parse_match_json_cols(_row(c.execute(sql, (match_id,)).fetchone()))

    def update_match(self, match_id: str, **fields: Any) -> dict | None:
        allowed = {
            "winner",
            "reason",
            "result",  # dict → 序列化 JSON 落 result 列
            "status",
            "started_at",
            "ended_at",
            "contest_id",
            "human_user_id",
            "human_seat",
            "match_seed",
            "technical_loss",
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [
            (json.dumps(v, ensure_ascii=False) if k == "result" and not isinstance(v, str) else v)
            for k, v in fields.items()
            if k in allowed
        ]
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

    def abort_match_if_active(self, match_id: str, *, reason: str) -> dict | None:
        """仅把 pending/running 对局原子推进为 aborted；终态绝不倒退。"""
        with self._tx() as c:
            tbl = self._match_table_of(c, match_id)
            if not tbl:
                return None
            c.execute(
                f"UPDATE {tbl} SET status=?, reason=?, ended_at=? "
                "WHERE id=? AND status IN (?,?)",
                (
                    STATUS_ABORTED, reason, _now(), match_id,
                    STATUS_PENDING, STATUS_RUNNING,
                ),
            )
            return _parse_match_json_cols(_row(
                c.execute(f"SELECT * FROM {tbl} WHERE id=?", (match_id,)).fetchone()
            ))

    def delete_match(self, match_id: str) -> bool:
        """删除对局：删 per-game 行、索引、回放和评分结算凭据。

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
                c.execute(
                    "DELETE FROM match_rating_settlements WHERE match_id=?",
                    (match_id,),
                )
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
                return [_parse_match_json_cols(_row(r)) for r in c.execute(sql, params)]

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
            return [_parse_match_json_cols(_row(r)) for r in c.execute(sql, all_params)]

    def contest_has_active_matches(self, contest_id: int) -> bool:
        """赛事是否仍有 pending/running 对局（跨所有已注册游戏表）。"""
        with self._tx() as c:
            for gid in _all_game_ids():
                table = _matches_table(gid)
                row = c.execute(
                    f"SELECT 1 FROM {table} WHERE contest_id=? "
                    "AND status IN (?,?) LIMIT 1",
                    (contest_id, STATUS_PENDING, STATUS_RUNNING),
                ).fetchone()
                if row:
                    return True
            return False

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
                "LEFT JOIN bots ba ON m.bot_a_id=ba.id "
                "LEFT JOIN bots bb ON m.bot_b_id=bb.id"
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
        """更新 (bot_id, game_id) 评分行；不存在则建。game_id 缺省取 bot 的 game_id。

        累加字段（wins/losses/draws/net_chips/matches_played）用 SQL 原子
        ``field = field + ?``（传入增量），防并发 lost-update（审计 P1：同 bot
        并发两局时快照+增量会丢一次）。其余字段（rating/rd/vol/last_played_at）
        是绝对赋值。
        """
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
        # 累加字段：传增量，SQL 原子加（防 lost-update）
        accum = {"wins", "losses", "draws", "net_chips", "matches_played"}
        sets = [
            f"{k} = {k} + ?" if k in accum else f"{k}=?"
            for k in fields
            if k in allowed
        ]
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

    def apply_match_ratings_atomic(
        self,
        bot_a_id: int,
        bot_b_id: int,
        *,
        game_id: str,
        rating_a: tuple[float, float, float],
        rating_b: tuple[float, float, float],
        winner: int | None,
        earnings_a: int,
        earnings_b: int,
        reason: str = "",
        settlement_id: str | None = None,
    ) -> bool:
        """恰好一次地落双边 rating/history、pair_stats 与结算凭据。

        调用方已按 bot 获取评分锁并用同一快照算出 Glicko 新值；本接口把所有
        持久化副作用收进一个 SQLite 事务，任一步失败都会整体回滚（包括最先
        claim 的 settlement marker，因此后续可重试）。同一 ``settlement_id``
        已存在时不产生任何评分副作用并返回 False。

        ``settlement_id=None`` 保留旧调用方的行为（不做幂等 claim）；正常对局
        路径必须传 match_id。同 bot 自博弈只提交 marker、不更新天梯。
        """
        gid = (game_id or "holdem").strip().lower()
        wa = int(winner == 0)
        la = int(winner == 1)
        da = int(winner is None)
        wb, lb, db = la, wa, da
        now = _now()
        with self._tx() as c:
            if settlement_id is not None:
                claimed = c.execute(
                    "INSERT OR IGNORE INTO match_rating_settlements(match_id, settled_at) "
                    "VALUES(?,?)",
                    (settlement_id, now),
                )
                if claimed.rowcount == 0:
                    return False

            # 自博弈没有可用于 Glicko 的对手信息。marker 仍须落盘，否则启动
            # 恢复会在每次重启反复扫描同一 completed 对局。
            if bot_a_id == bot_b_id:
                return True

            for bot_id in (bot_a_id, bot_b_id):
                c.execute(
                    "INSERT OR IGNORE INTO ratings(bot_id, game_id) VALUES(?, ?)",
                    (bot_id, gid),
                )
            for bot_id, values, wins, losses, draws, earnings in (
                (bot_a_id, rating_a, wa, la, da, earnings_a),
                (bot_b_id, rating_b, wb, lb, db, earnings_b),
            ):
                c.execute(
                    "UPDATE ratings SET rating=?, rd=?, vol=?, "
                    "wins=wins+?, losses=losses+?, draws=draws+?, "
                    "net_chips=net_chips+?, matches_played=matches_played+1, "
                    "last_played_at=? WHERE bot_id=? AND game_id=?",
                    (
                        values[0], values[1], values[2], wins, losses, draws,
                        earnings, now, bot_id, gid,
                    ),
                )
                row = c.execute(
                    "SELECT rating, rd, vol, matches_played FROM ratings "
                    "WHERE bot_id=? AND game_id=?",
                    (bot_id, gid),
                ).fetchone()
                c.execute(
                    "INSERT INTO rating_history(bot_id, game_id, rating, rd, vol, "
                    "matches_played, reason, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        bot_id, gid, row["rating"], row["rd"], row["vol"],
                        row["matches_played"], reason, now,
                    ),
                )
                c.execute(
                    "DELETE FROM rating_history "
                    "WHERE bot_id=? AND game_id=? AND id NOT IN "
                    "(SELECT id FROM rating_history WHERE bot_id=? AND game_id=? "
                    "ORDER BY id DESC LIMIT 200)",
                    (bot_id, gid, bot_id, gid),
                )

            lo, hi = sorted((bot_a_id, bot_b_id))
            if bot_a_id == lo:
                aw, al, dd = wa, la, da
            else:
                aw, al, dd = wb, lb, db
            c.execute(
                "INSERT INTO pair_stats(bot_a_id, bot_b_id, bb_per_100_mean, "
                "ci_low, ci_high, samples, last_played_at, a_wins, a_losses, draws) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(bot_a_id, bot_b_id) DO UPDATE SET "
                "bb_per_100_mean=excluded.bb_per_100_mean, "
                "ci_low=excluded.ci_low, ci_high=excluded.ci_high, "
                "samples=excluded.samples, last_played_at=excluded.last_played_at, "
                "a_wins=pair_stats.a_wins+excluded.a_wins, "
                "a_losses=pair_stats.a_losses+excluded.a_losses, "
                "draws=pair_stats.draws+excluded.draws",
                (lo, hi, 0.0, None, None, 0, now, aw, al, dd),
            )
            return True

    def mark_match_rating_settled(self, match_id: str) -> bool:
        """原子写入无评分副作用的结算 marker；已存在返回 False。

        仅用于 completed 行已失去 Bot 外键、无法重算评分的收敛场景。自博弈
        仍走 :meth:`apply_match_ratings_atomic` 的同 bot 分支。
        """
        with self._tx() as c:
            cur = c.execute(
                "INSERT OR IGNORE INTO match_rating_settlements(match_id, settled_at) "
                "VALUES(?,?)",
                (match_id, _now()),
            )
            return cur.rowcount > 0

    def is_match_rating_settled(self, match_id: str) -> bool:
        with self._tx() as c:
            return c.execute(
                "SELECT 1 FROM match_rating_settlements WHERE match_id=?",
                (match_id,),
            ).fetchone() is not None

    def list_unsettled_completed_rating_matches(self) -> list[dict]:
        """列出需启动补算的 completed Bot 对局（跨游戏、排除赛事/人类）。"""
        with self._tx() as c:
            matches: list[dict] = []
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                rows = c.execute(
                    f"SELECT m.* FROM {tbl} m "
                    "LEFT JOIN match_rating_settlements settled ON settled.match_id=m.id "
                    "WHERE m.status=? AND m.match_type NOT IN (?,?) "
                    "AND settled.match_id IS NULL ORDER BY m.created_at, m.id",
                    (STATUS_COMPLETED, TYPE_CONTEST, TYPE_HUMAN),
                ).fetchall()
                matches.extend(
                    _parse_match_json_cols(_row(row)) for row in rows
                )
            matches.sort(key=lambda m: (m.get("created_at") or "", m.get("id") or ""))
            return matches

    def list_leaderboard(
        self, limit: int = 50, *, game_id: str | None = None,
        page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        with self._tx() as c:
            # rating_delta = 当前 rating - 上一条历史评分（升降趋势）；无历史则 NULL
            # ratings/rating_history 现按 (bot_id, game_id) 复合键——join/subquery 都
            # 加 game_id 谓词（bot 绑定单一游戏，r.game_id=b.game_id 恰一行）。
            #
            # 注意：SELECT 中含 prev_rating 子查询（含自己的 FROM），_paginate 的
            # "首个 FROM" 启发会被子查询骗到。故分页时显式写 COUNT（不含子查询），
            # 行查询交由 _paginate 自动加 LIMIT/OFFSET。
            base_from = (
                "FROM ratings r JOIN bots b ON r.bot_id=b.id AND r.game_id=b.game_id "
                "LEFT JOIN users u ON b.owner_id=u.id "
                "WHERE b.is_active=1"
            )
            params: list[Any] = []
            if game_id:
                base_from += " AND b.game_id=?"
                params.append(game_id)
            sel = (
                "SELECT r.bot_id, r.rating, r.rd, r.vol, r.wins, r.losses, "
                "r.draws, r.net_chips, r.matches_played, r.last_played_at, "
                "b.name AS bot_name, b.display_name AS bot_display, "
                "b.format, b.os, b.arch, b.is_builtin, b.game_id, "
                "u.username AS owner_name, u.display_name AS owner_display, "
                "(SELECT rh.rating FROM rating_history rh "
                " WHERE rh.bot_id=r.bot_id AND rh.game_id=r.game_id "
                " ORDER BY rh.id DESC LIMIT 1 OFFSET 1) AS prev_rating "
            )
            order = " ORDER BY r.rating DESC"
            if page is not None:
                # 显式 COUNT（_paginate 启发在此查询上不可靠）
                count_sql = f"SELECT COUNT(*) {base_from}"
                total = int(c.execute(count_sql, tuple(params)).fetchone()[0])
                pp = max(1, min(200, int(per_page)))
                off = (max(1, int(page)) - 1) * pp
                sql = f"{sel}{base_from}{order} LIMIT ? OFFSET ?"
                rows = [_row(r) for r in c.execute(
                    sql, tuple(params) + (pp, off)
                ).fetchall()]
            else:
                sql = f"{sel}{base_from}{order} LIMIT ?"
                rows = [_row(r) for r in c.execute(
                    sql, tuple(params) + (max(1, min(limit, 200)),)
                )]
                total = None  # 旧契约不返回 total
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
            if page is not None:
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
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

    def count_bot_matches(self, bot_id: int) -> int:
        """统计某 bot 参与的对局数（跨所有已注册游戏表，bot_a 或 bot_b 均算）。

        供 /api/bots/{id}/matches 分页算 total——list_matches 用 ``(bot_a_id=? OR
        bot_b_id=?)`` 过滤，count 维度需与之对齐。
        """
        with self._tx() as c:
            total = 0
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE bot_a_id=? OR bot_b_id=?",
                    (bot_id, bot_id),
                ).fetchone()
                total += int(row[0]) if row else 0
            return total

    def recover_orphan_matches(self) -> int:
        """启动时清理孤儿对局：把残留的 status=running（无对应内存协程）标 aborted。

        服务非正常退出后，DB 里 running 记录的内存 Task/Future 已丢失（尤其
        人类对局的 _human_turns），不清理会永久卡 running、泄漏并发与活跃用户计数。
        遍历三张 per-game 表清理。返回受影响行数。

        同时清理孤儿 pending 赛事对局：
        - 所有非 contest pending（challenge/table/ladder/human 等）：进程重启后
          已无对应内存 Task/Future，统一标 ``orphan_pending_after_restart`` aborted；
        - contest_id=NULL AND match_type='contest' AND status='pending'（e2e 残留等无主）；
        - contest 已终态（finished/cancelled）但仍 pending 的赛事对局（排期积压后赛事已结束，
          这些 pending 永不会被打，堵 orchestrator._tasks → auto_matcher 误判不空闲）。

        活跃赛事的 pending contest match 不在本方法粗暴标 aborted：已绑定/
        未绑定的两阶段派发中断由 ``reset_dead_contest_pairings`` 精确删除并重派。
        """
        from bzplat.backend.store.schema import STATUS_ABORTED

        with self._tx() as c:
            n = 0
            for gid in _all_game_ids():
                tbl = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} WHERE status=?",
                    (STATUS_RUNNING,),
                ).fetchone()
                cnt = int(row[0]) if row else 0
                if cnt:
                    c.execute(
                        f"UPDATE {tbl} SET status=?, reason='orphan_after_restart', "
                        "ended_at=datetime('now') WHERE status=?",
                        (STATUS_ABORTED, STATUS_RUNNING),
                    )
                    n += cnt
                # 非赛事 pending 也依赖上一进程的内存 task/future；重启后
                # 不可能继续。contest pending 必须留给后续 pairing 对账精确恢复。
                non_contest_pending = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} "
                    "WHERE status=? AND match_type<>?",
                    (STATUS_PENDING, TYPE_CONTEST),
                ).fetchone()
                pending_count = int(non_contest_pending[0]) if non_contest_pending else 0
                if pending_count:
                    c.execute(
                        f"UPDATE {tbl} SET status=?, "
                        "reason='orphan_pending_after_restart', ended_at=? "
                        "WHERE status=? AND match_type<>?",
                        (
                            STATUS_ABORTED,
                            _now(),
                            STATUS_PENDING,
                            TYPE_CONTEST,
                        ),
                    )
                    n += pending_count
                # 清理孤儿 pending 赛事对局（无 contest 归属的 type=contest pending）
                row2 = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} "
                    f"WHERE status=? AND match_type=? "
                    f"AND contest_id IS NULL",
                    (STATUS_PENDING, TYPE_CONTEST),
                ).fetchone()
                cnt2 = int(row2[0]) if row2 else 0
                if cnt2:
                    c.execute(
                        f"UPDATE {tbl} SET status=?, reason='orphan_pending_no_contest', "
                        "ended_at=datetime('now') "
                        f"WHERE status=? AND match_type=? "
                        f"AND contest_id IS NULL",
                        (STATUS_ABORTED, STATUS_PENDING, TYPE_CONTEST),
                    )
                    n += cnt2
                # 清理已终态赛事的残留 pending 对局（赛事 finished/cancelled 但 match 仍 pending）
                row3 = c.execute(
                    f"SELECT COUNT(*) FROM {tbl} m "
                    f"WHERE m.status=? AND m.contest_id IS NOT NULL "
                    f"AND m.contest_id IN (SELECT id FROM contests "
                    f"WHERE status IN (?,?))",
                    (STATUS_PENDING, CONTEST_FINISHED, CONTEST_CANCELLED),
                ).fetchone()
                cnt3 = int(row3[0]) if row3 else 0
                if cnt3:
                    c.execute(
                        f"UPDATE {tbl} SET status=?, reason='contest_ended_pending_orphan', "
                        "ended_at=datetime('now') "
                        f"WHERE status=? AND contest_id IS NOT NULL "
                        f"AND contest_id IN (SELECT id FROM contests "
                        f"WHERE status IN (?,?))",
                        (
                            STATUS_ABORTED,
                            STATUS_PENDING,
                            CONTEST_FINISHED,
                            CONTEST_CANCELLED,
                        ),
                    )
                    n += cnt3
            return n

    def reset_dead_contest_pairings(self) -> int:
        """启动对账辅助：清理两阶段派发中断留下的死状态。

        1. prepare match 已插入，但进程在 bind pairing 前退出：活跃赛事中会留下
           没有任何 pairing 引用的 pending contest match。这类幽灵对局必须连同
           物理 match 行、matches_index 和 replay 在同一事务内删除。
        2. contest_pairings 里 status='running' 但对应 match 已终态非
           completed（aborted/orphan/pending 或不存在）：复位为 pending +
           match_id=NULL，供 ContestManager.maybe_finish/_dispatch_pending 重派。

        completed 的 pairing 不动（保留真实比赛结果，防误伤）。
        对应 recover_orphan_matches 把 running match 标 aborted 后的赛事善后——
        那些赛事 pairing 仍指 aborted match（_stage_done 不通过 pairing 状态判，而是
        读 match.status，但 _dispatch_pending 只挑 status=pending 且无 match_id 的重派，
        所以 status=running+match_id=aborted 的死 pairing 永远不会被重派 → 赛事卡死）。
        返回重置行数。
        """
        # pairing bind 已提交、runner 尚未 start 时进程可能退出：该 match 仍 pending。
        # 解绑它时必须在同一事务删除物理 match + index + replay，否则随后重派会留下
        # ghost pending match，阻塞 force-finish 并重复占用数据。
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            recovered = 0
            active_statuses = (
                CONTEST_PUBLISHED,
                CONTEST_RUNNING,
                CONTEST_REST,
            )
            status_marks = ",".join("?" for _ in active_statuses)
            # prepare 成功、bind 前硬崩：match 的 contest_id 已写入，但没有
            # pairing.match_id 指向它。这里只在启动对账入口调用，内存中已无
            # 可能继续 bind 的 prepared map，因此删除是唯一可恢复收敛。
            for gid in _all_game_ids():
                table = _matches_table(gid)
                ghosts = c.execute(
                    f"SELECT m.id FROM {table} m "
                    "JOIN contests contest ON contest.id=m.contest_id "
                    "WHERE m.status=? AND m.match_type=? "
                    f"AND contest.status IN ({status_marks}) "
                    "AND NOT EXISTS ("
                    "SELECT 1 FROM contest_pairings pairing WHERE pairing.match_id=m.id"
                    ")",
                    (STATUS_PENDING, TYPE_CONTEST, *active_statuses),
                ).fetchall()
                for ghost in ghosts:
                    match_id = str(ghost["id"])
                    c.execute(f"DELETE FROM {table} WHERE id=?", (match_id,))
                    c.execute("DELETE FROM matches_index WHERE id=?", (match_id,))
                    c.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
                    recovered += 1

            pairings = c.execute(
                "SELECT id, match_id FROM contest_pairings "
                "WHERE status=? AND match_id IS NOT NULL",
                (STATUS_RUNNING,),
            ).fetchall()
            for pairing in pairings:
                match_id = str(pairing["match_id"])
                indexed = c.execute(
                    "SELECT game_id FROM matches_index WHERE id=?", (match_id,)
                ).fetchone()
                table = _matches_table(indexed["game_id"]) if indexed else None
                match = (
                    c.execute(
                        f"SELECT status FROM {table} WHERE id=?", (match_id,)
                    ).fetchone()
                    if table else None
                )
                if match and match["status"] == STATUS_COMPLETED:
                    continue
                cur = c.execute(
                    "UPDATE contest_pairings SET status=?, match_id=NULL "
                    "WHERE id=? AND status=? AND match_id=?",
                    (STATUS_PENDING, pairing["id"], STATUS_RUNNING, match_id),
                )
                if cur.rowcount != 1:
                    continue
                recovered += 1
                if not match or not table:
                    continue
                if match["status"] == STATUS_PENDING:
                    c.execute(f"DELETE FROM {table} WHERE id=?", (match_id,))
                    c.execute("DELETE FROM matches_index WHERE id=?", (match_id,))
                    c.execute("DELETE FROM match_replays WHERE match_id=?", (match_id,))
                elif match["status"] == STATUS_RUNNING:
                    c.execute(
                        f"UPDATE {table} SET status=?, reason='orphan_after_restart', "
                        "ended_at=? WHERE id=? AND status=?",
                        (STATUS_ABORTED, _now(), match_id, STATUS_RUNNING),
                    )
            return recovered

    def list_contests_by_status(self, statuses: list[str]) -> list[dict]:
        """返回 status 在给定集合内的 contest（启动对账 reconcile_running_contests 用）。"""
        if not statuses:
            return []
        with self._tx() as c:
            placeholders = ",".join("?" for _ in statuses)
            rows = c.execute(
                f"SELECT * FROM contests WHERE status IN ({placeholders}) "
                "ORDER BY id",
                tuple(statuses),
            ).fetchall()
            return [_row(r) for r in rows]

    def list_unready_finished_contests(self) -> list[dict]:
        """Return terminal contests whose durable official ranking is incomplete."""
        with self._tx() as c:
            rows = c.execute(
                "SELECT * FROM contests WHERE status=? "
                "AND COALESCE(official_results_ready, 0)=0 ORDER BY id",
                (CONTEST_FINISHED,),
            ).fetchall()
            return [_row(row) for row in rows]

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
        self, target_type: str, target_id: str, *, limit: int = 100,
        page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        with self._tx() as c:
            sql = (
                "SELECT c.*, u.username, u.display_name AS user_display "
                "FROM comments c LEFT JOIN users u ON c.user_id=u.id "
                "WHERE c.target_type=? AND c.target_id=? "
                "ORDER BY c.id DESC"
            )
            params = (target_type, str(target_id))
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, params, page=page, per_page=pp)
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
            sql += " LIMIT ?"
            return [_row(r) for r in c.execute(
                sql, params + (max(1, min(limit, 500)),)
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
        page: int | None = None,
        per_page: int = 50,
    ) -> list[dict] | dict:
        with self._tx() as c:
            sql = "SELECT * FROM notifications WHERE user_id=?"
            params: list[Any] = [user_id]
            if unread_only:
                sql += " AND is_read=0"
            sql += " ORDER BY id DESC"
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, tuple(params), page=page, per_page=pp)
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
            sql += " LIMIT ? OFFSET ?"
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
            # 状态机校验：status 变更须合法（防止 admin PATCH 把 finished/cancelled
            # 错误改写——曾导致 contest3 已完成 96 场却被改成 cancelled 隐藏全部结果）。
            if "status" in fields and fields["status"]:
                from bzplat.backend.store.schema import (
                    CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED,
                    CONTEST_RUNNING, CONTEST_REST, CONTEST_FINISHED, CONTEST_CANCELLED,
                )
                cur = c.execute(
                    "SELECT status FROM contests WHERE id=?", (contest_id,)
                ).fetchone()
                if cur:
                    cur_status = cur["status"]
                    new_status = fields["status"]
                    # 终态不可变（finished/cancelled 是终态，不允许再改）
                    if cur_status in (CONTEST_FINISHED, CONTEST_CANCELLED) and new_status != cur_status:
                        raise ValueError(
                            f"赛事已处于终态 {cur_status}，不能改为 {new_status}"
                        )
                    # cancelled 只能从「未开始」态进入（draft/open/published）
                    if new_status == CONTEST_CANCELLED and cur_status not in (
                        CONTEST_DRAFT, CONTEST_OPEN, CONTEST_PUBLISHED,
                    ):
                        raise ValueError(
                            f"赛事处于 {cur_status} 态，不能取消（仅 draft/open/published 可取消）"
                        )
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
        game_id: str | None = None, page: int | None = None, per_page: int = 20,
        exclude_statuses: list[str] | None = None,
        hidden_owner_id: int | None = None,
    ) -> list[dict] | dict:
        """列赛事，并在分页 SQL 内完成隐藏状态的可见性过滤。

        ``exclude_statuses`` 非空时，匿名/普通用户（``hidden_owner_id=None``）
        始终排除这些状态，即使同时传了显式 ``status`` 也不能绕过。
        组织者传自己的 user id，则可额外看到“自己主办”的隐藏赛事，
        不会因 organizer 角色而看到他人草稿/已取消赛事。admin 调用方
        不传 ``exclude_statuses`` 即保持全见。条件必须在 SQL 分页前应用，
        不得拉取一页后再用 Python 裁剪（会使 total/页数泄漏且错位）。
        """
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
            # 隐藏状态过滤与显式 status 可同时存在：例如访客显式查
            # draft 仍必须得到空集；组织者则只能看自己的 draft。
            if exclude_statuses:
                placeholders = ",".join("?" for _ in exclude_statuses)
                if hidden_owner_id is None:
                    sql += f" AND status NOT IN ({placeholders})"
                else:
                    sql += (
                        f" AND (status NOT IN ({placeholders}) OR organizer_id=?)"
                    )
                params.extend(exclude_statuses)
                if hidden_owner_id is not None:
                    params.append(hidden_owner_id)
            sql += " ORDER BY created_at DESC"
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, tuple(params), page=page, per_page=pp)
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
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

    def add_contest_entry_once(
        self, contest_id: int, user_id: int, bot_id: int
    ) -> dict:
        """原子新增一条用户报名，重复报名统一抛业务 ``ValueError``。

        ``ContestManager.register`` 的资格校验与写入之间可能被另一请求穿插；
        这里在单个 Store 事务内用唯一键冲突策略收口，避免并发重复报名把
        ``sqlite3.IntegrityError`` 泄漏成 500。调用方只有拿到新行后才可执行 XP 等
        后续副作用。
        """
        with self._tx() as c:
            contest = c.execute(
                "SELECT status FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not contest or contest["status"] != CONTEST_OPEN:
                raise ValueError("比赛未开放报名")
            cur = c.execute(
                "INSERT INTO contest_entries(contest_id, user_id, bot_id, registered_at) "
                "VALUES(?,?,?,?) ON CONFLICT(contest_id, user_id) DO NOTHING",
                (contest_id, user_id, bot_id, _now()),
            )
            if cur.rowcount != 1:
                raise ValueError("该用户在此比赛中已报名")
            return _row(
                c.execute(
                    "SELECT * FROM contest_entries WHERE id=?", (cur.lastrowid,)
                ).fetchone()
            )

    def add_contest_roster_entries(
        self, contest_id: int, entries: list[tuple[int, int]]
    ) -> tuple[list[dict], list[int]]:
        """组织者/admin 批量新增名册；状态复核与整批写入同一事务。"""
        with self._tx() as c:
            contest = c.execute(
                "SELECT status FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            added: list[dict] = []
            skipped: list[int] = []
            for user_id, bot_id in entries:
                cur = c.execute(
                    "INSERT INTO contest_entries(contest_id, user_id, bot_id, registered_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(contest_id, user_id) DO NOTHING",
                    (contest_id, user_id, bot_id, _now()),
                )
                if cur.rowcount != 1:
                    skipped.append(user_id)
                    continue
                added.append(_row(c.execute(
                    "SELECT * FROM contest_entries WHERE id=?", (cur.lastrowid,)
                ).fetchone()))
            return added, skipped

    def delete_contest_roster_entry(self, contest_id: int, user_id: int) -> bool:
        """组织者/admin 删除名册；状态复核与 DELETE 同一事务。"""
        with self._tx() as c:
            contest = c.execute(
                "SELECT status FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] not in (CONTEST_DRAFT, CONTEST_OPEN):
                raise ValueError("开赛后不可改名册")
            cur = c.execute(
                "DELETE FROM contest_entries WHERE contest_id=? AND user_id=?",
                (contest_id, user_id),
            )
            return cur.rowcount > 0

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
        scheduled_at: str | None = None,
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO contest_pairings(contest_id, round_num, entry_a_id, "
                "entry_b_id, bot_a_id, bot_b_id, bot_a_version_id, bot_b_version_id, "
                "pairing_seed, published_at, scheduled_at, match_id, status, stage_idx, "
                "stage_key, group_id, bracket_slot, color_first) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    scheduled_at,
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

    def create_contest_stage_pairings(
        self,
        contest_id: int,
        stage_idx: int,
        pairing_rows: list[dict[str, Any]],
        *,
        expected_current_stage_idx: int,
        activate_running: bool = False,
    ) -> list[dict]:
        """Atomically persist one complete stage pairing batch and its state move.

        A stage is a single durability unit: no caller can observe only the first
        few pairings, and advancing ``current_stage_idx``/leaving ``rest`` is
        committed together with the complete batch.  ``BEGIN IMMEDIATE`` plus the
        expected-index check also protects against another process advancing the
        same contest after the manager's read.

        A pre-upgrade crash could have left an unbound partial batch for the *next*
        stage while the contest still points at the previous stage.  That exact
        shape is safe to replace inside this transaction.  Rows with a bound match
        or any other progress are rejected rather than silently overwritten.
        """
        if not pairing_rows:
            raise ValueError("赛事阶段对阵批次不能为空")
        columns = (
            "contest_id",
            "round_num",
            "entry_a_id",
            "entry_b_id",
            "bot_a_id",
            "bot_b_id",
            "bot_a_version_id",
            "bot_b_version_id",
            "pairing_seed",
            "published_at",
            "scheduled_at",
            "match_id",
            "status",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status, current_stage_idx FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] in (CONTEST_FINISHED, CONTEST_CANCELLED):
                raise ValueError("终态赛事不能生成新阶段对阵")
            current_idx = int(contest["current_stage_idx"] or 0)
            if current_idx != int(expected_current_stage_idx):
                raise ValueError("赛事当前阶段已变化，拒绝重复生成对阵")
            if stage_idx not in (current_idx, current_idx + 1):
                raise ValueError("赛事阶段只能生成当前阶段或紧邻的下一阶段")

            existing = c.execute(
                "SELECT id, match_id, status, bot_b_id FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? ORDER BY id",
                (contest_id, stage_idx),
            ).fetchall()
            if existing and stage_idx == current_idx:
                raise ValueError("当前阶段对阵已存在，拒绝重复生成")
            if existing:
                if any(row["match_id"] is not None for row in existing):
                    raise ValueError("下一阶段已有绑定对局，不能覆盖")
                if any(
                    row["status"] not in (STATUS_PENDING, STATUS_COMPLETED)
                    or (
                        row["status"] == STATUS_COMPLETED
                        and row["bot_b_id"] is not None
                    )
                    for row in existing
                ):
                    raise ValueError("下一阶段已有运行进度，不能覆盖")
                c.execute(
                    "DELETE FROM contest_pairings WHERE contest_id=? AND stage_idx=?",
                    (contest_id, stage_idx),
                )

            inserted: list[dict] = []
            placeholders = ",".join("?" for _ in columns)
            for source in pairing_rows:
                row = {
                    "contest_id": contest_id,
                    "round_num": int(source.get("round_num") or 1),
                    "entry_a_id": source.get("entry_a_id"),
                    "entry_b_id": source.get("entry_b_id"),
                    "bot_a_id": source.get("bot_a_id"),
                    "bot_b_id": source.get("bot_b_id"),
                    "bot_a_version_id": source.get("bot_a_version_id"),
                    "bot_b_version_id": source.get("bot_b_version_id"),
                    "pairing_seed": source.get("pairing_seed"),
                    "published_at": source.get("published_at"),
                    "scheduled_at": source.get("scheduled_at"),
                    "match_id": None,
                    "status": source.get("status") or STATUS_PENDING,
                    "stage_idx": stage_idx,
                    "stage_key": source.get("stage_key") or "",
                    "group_id": source.get("group_id") or "",
                    "bracket_slot": source.get("bracket_slot"),
                    "color_first": int(source.get("color_first") or 0),
                }
                cur = c.execute(
                    f"INSERT INTO contest_pairings({','.join(columns)}) "
                    f"VALUES({placeholders})",
                    tuple(row[column] for column in columns),
                )
                inserted.append(
                    _row(
                        c.execute(
                            "SELECT * FROM contest_pairings WHERE id=?",
                            (cur.lastrowid,),
                        ).fetchone()
                    )
                )

            if activate_running:
                c.execute(
                    "UPDATE contests SET status=?, current_stage_idx=?, "
                    "rest_ends_at=NULL WHERE id=?",
                    (CONTEST_RUNNING, stage_idx, contest_id),
                )
            elif stage_idx != current_idx:
                c.execute(
                    "UPDATE contests SET current_stage_idx=? WHERE id=?",
                    (stage_idx, contest_id),
                )
            return inserted

    def append_contest_round_pairings(
        self,
        contest_id: int,
        stage_idx: int,
        pairing_rows: list[dict[str, Any]],
        *,
        expected_current_stage_idx: int,
        expected_previous_max_round: int,
    ) -> list[dict]:
        """Atomically append one complete lazy-generated Swiss/KO round.

        The caller computes every row, including seat order and version snapshots,
        before entering this method.  ``BEGIN IMMEDIATE`` then revalidates the
        durable contest/stage cursor and previous maximum round.  A concurrent or
        retried writer cannot append the same target round twice, and any INSERT
        failure rolls the whole round back.
        """
        if not pairing_rows:
            raise ValueError("赛事轮次对阵批次不能为空")
        previous_round = int(expected_previous_max_round)
        target_round = previous_round + 1
        if any(
            int(source.get("round_num") or 0) != target_round
            for source in pairing_rows
        ):
            raise ValueError("赛事轮次批次必须全部属于紧邻的目标轮")

        columns = (
            "contest_id",
            "round_num",
            "entry_a_id",
            "entry_b_id",
            "bot_a_id",
            "bot_b_id",
            "bot_a_version_id",
            "bot_b_version_id",
            "pairing_seed",
            "published_at",
            "scheduled_at",
            "match_id",
            "status",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status, current_stage_idx FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest:
                raise ValueError("赛事不存在")
            if contest["status"] != CONTEST_RUNNING:
                raise ValueError("仅运行中的赛事可追加后续轮次")
            if int(contest["current_stage_idx"] or 0) != int(
                expected_current_stage_idx
            ):
                raise ValueError("赛事当前阶段已变化，拒绝追加轮次")
            if int(stage_idx) != int(expected_current_stage_idx):
                raise ValueError("只能向赛事当前阶段追加轮次")

            round_state = c.execute(
                "SELECT MAX(round_num) AS max_round FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=?",
                (contest_id, stage_idx),
            ).fetchone()
            actual_max = (
                int(round_state["max_round"])
                if round_state and round_state["max_round"] is not None
                else 0
            )
            if actual_max != previous_round:
                raise ValueError("赛事上一轮已变化，拒绝重复或跨轮追加")
            target_exists = c.execute(
                "SELECT 1 FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? AND round_num=? LIMIT 1",
                (contest_id, stage_idx, target_round),
            ).fetchone()
            if target_exists:
                raise ValueError("赛事目标轮已存在，拒绝重复生成")

            inserted: list[dict] = []
            placeholders = ",".join("?" for _ in columns)
            for source in pairing_rows:
                row = {
                    "contest_id": contest_id,
                    "round_num": target_round,
                    "entry_a_id": source.get("entry_a_id"),
                    "entry_b_id": source.get("entry_b_id"),
                    "bot_a_id": source.get("bot_a_id"),
                    "bot_b_id": source.get("bot_b_id"),
                    "bot_a_version_id": source.get("bot_a_version_id"),
                    "bot_b_version_id": source.get("bot_b_version_id"),
                    "pairing_seed": source.get("pairing_seed"),
                    "published_at": source.get("published_at"),
                    "scheduled_at": source.get("scheduled_at"),
                    "match_id": None,
                    "status": source.get("status") or STATUS_PENDING,
                    "stage_idx": stage_idx,
                    "stage_key": source.get("stage_key") or "",
                    "group_id": source.get("group_id") or "",
                    "bracket_slot": source.get("bracket_slot"),
                    # A/B have already been materialized as actual seat 0/1.
                    "color_first": 0,
                }
                cur = c.execute(
                    f"INSERT INTO contest_pairings({','.join(columns)}) "
                    f"VALUES({placeholders})",
                    tuple(row[column] for column in columns),
                )
                inserted.append(
                    _row(
                        c.execute(
                            "SELECT * FROM contest_pairings WHERE id=?",
                            (cur.lastrowid,),
                        ).fetchone()
                    )
                )
            return inserted

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

    def delete_unstarted_contest_pairings(
        self, contest_id: int, pairing_ids: list[int]
    ) -> int:
        """删除一次失败的阶段生成所留下、且尚未绑定对局的 pairing。

        这是赛事生命周期补偿专用的窄接口：调用方必须传入本次生成前后差集得到的
        精确 ID；SQL 再同时约束 contest_id 与 match_id IS NULL，避免误删并发产生或
        已经派发的合法对阵。返回实际删除行数，供调用方决定是否可安全回滚状态。
        """
        ids = sorted({int(pid) for pid in pairing_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._tx() as c:
            cur = c.execute(
                f"DELETE FROM contest_pairings WHERE contest_id=? "
                f"AND match_id IS NULL AND id IN ({placeholders})",
                [contest_id, *ids],
            )
            return int(cur.rowcount)

    def replace_unstarted_contest_stage_pairings(
        self,
        contest_id: int,
        stage_idx: int,
        pairing_rows: list[dict[str, Any]],
        *,
        expected_existing_ids: list[int],
    ) -> list[dict]:
        """published 首阶段硬崩恢复：原子替换未启动的残缺对阵批次。

        这是一个有意窄化的恢复入口，只允许当前仍为 ``published``、
        ``current_stage_idx`` 未改变，且现有 pairing 全部未绑定 match 时重建。
        已绑定、已进入 running/completed，或赛事存在任何 active match
        都是不可自动推断的不一致，必须显式报错而不能静默续跑。

        ``expected_existing_ids`` 是 manager 在同一 per-contest 锁内看到的快照；
        ``BEGIN IMMEDIATE`` 后再比对一次，阻止多进程/外部写在
        check→replace 窗口中被覆盖。
        """
        expected_ids = sorted({int(pairing_id) for pairing_id in expected_existing_ids})
        columns = (
            "contest_id",
            "round_num",
            "entry_a_id",
            "entry_b_id",
            "bot_a_id",
            "bot_b_id",
            "bot_a_version_id",
            "bot_b_version_id",
            "pairing_seed",
            "published_at",
            "scheduled_at",
            "match_id",
            "status",
            "stage_idx",
            "stage_key",
            "group_id",
            "bracket_slot",
            "color_first",
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            contest = c.execute(
                "SELECT status, current_stage_idx FROM contests WHERE id=?",
                (contest_id,),
            ).fetchone()
            if not contest or contest["status"] != CONTEST_PUBLISHED:
                raise ValueError("published 赛事状态已变化，拒绝重建对阵")
            if int(contest["current_stage_idx"] or 0) != int(stage_idx):
                raise ValueError("published 赛事当前阶段已变化，拒绝重建对阵")

            current = c.execute(
                "SELECT id, match_id, status, bot_b_id FROM contest_pairings "
                "WHERE contest_id=? AND stage_idx=? ORDER BY id",
                (contest_id, stage_idx),
            ).fetchall()
            current_ids = [int(row["id"]) for row in current]
            if current_ids != expected_ids:
                raise ValueError("published 对阵在恢复期间已变化，拒绝覆盖")
            if any(row["match_id"] is not None for row in current):
                raise ValueError("published 对阵已绑定对局，不能自动重建")
            if any(
                row["status"] not in (STATUS_PENDING, STATUS_COMPLETED)
                or (row["status"] == STATUS_COMPLETED and row["bot_b_id"] is not None)
                for row in current
            ):
                raise ValueError("published 对阵已有运行进度，不能自动重建")

            for gid in _all_game_ids():
                table = _matches_table(gid)
                active = c.execute(
                    f"SELECT 1 FROM {table} WHERE contest_id=? "
                    "AND status IN (?,?) LIMIT 1",
                    (contest_id, STATUS_PENDING, STATUS_RUNNING),
                ).fetchone()
                if active:
                    raise ValueError("published 赛事已有 active 对局，不能自动重建")

            c.execute(
                "DELETE FROM contest_pairings WHERE contest_id=? AND stage_idx=?",
                (contest_id, stage_idx),
            )
            inserted: list[dict] = []
            placeholders = ",".join("?" for _ in columns)
            for source in pairing_rows:
                row = {
                    "contest_id": contest_id,
                    "round_num": int(source.get("round_num") or 1),
                    "entry_a_id": source.get("entry_a_id"),
                    "entry_b_id": source.get("entry_b_id"),
                    "bot_a_id": source.get("bot_a_id"),
                    "bot_b_id": source.get("bot_b_id"),
                    "bot_a_version_id": source.get("bot_a_version_id"),
                    "bot_b_version_id": source.get("bot_b_version_id"),
                    "pairing_seed": source.get("pairing_seed"),
                    "published_at": source.get("published_at"),
                    "scheduled_at": source.get("scheduled_at"),
                    "match_id": None,
                    "status": source.get("status") or STATUS_PENDING,
                    "stage_idx": stage_idx,
                    "stage_key": source.get("stage_key") or "",
                    "group_id": source.get("group_id") or "",
                    "bracket_slot": source.get("bracket_slot"),
                    "color_first": int(source.get("color_first") or 0),
                }
                cur = c.execute(
                    f"INSERT INTO contest_pairings({','.join(columns)}) "
                    f"VALUES({placeholders})",
                    tuple(row[column] for column in columns),
                )
                inserted.append(
                    _row(
                        c.execute(
                            "SELECT * FROM contest_pairings WHERE id=?",
                            (cur.lastrowid,),
                        ).fetchone()
                    )
                )
            return inserted

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

    def contest_entries_named(
        self, contest_id: int, *, page: int | None = None, per_page: int = 50,
    ) -> list[dict] | dict:
        """返回报名（带 bot 名/owner 名 + seed/group/eliminated + 实名信息）。

        LEFT JOIN bots：bot_id 现可为 NULL（删 bot 后保留 entry，P0 SET NULL）。
        实名字段（real_name/phone/school/student_id）随行返回——**调用方（api 层）负责
        对非组织者脱敏**（contest_detail 仅组织者可见；export 端点组织者 gated）。
        ``page`` 为 None 时返回 list（旧契约）；给定时返回分页 dict。
        """
        with self._tx() as c:
            sql = (
                "SELECT e.*, b.name AS bot_name, b.display_name AS bot_display, "
                "b.game_id, u.username AS owner_name, u.display_name AS owner_display, "
                "u.real_name, u.phone, u.school, u.student_id "
                "FROM contest_entries e "
                "LEFT JOIN bots b ON e.bot_id=b.id "
                "LEFT JOIN users u ON e.user_id=u.id "
                "WHERE e.contest_id=? ORDER BY e.seed, e.registered_at"
            )
            params = (contest_id,)
            if page is not None:
                pp = max(1, min(200, int(per_page)))
                rows, total = _paginate(c, sql, params, page=page, per_page=pp)
                return {"items": rows, "page": max(1, int(page)), "per_page": pp, "total": total}
            return [_row(r) for r in c.execute(sql, params).fetchall()]

    def list_contest_export(self, contest_id: int) -> list[dict]:
        """合并导出：一行 per 报名者 = 报名信息（实名）+ 结果排名 + 战绩。

        LEFT JOIN official_results：未完赛/未出排名者 rank/points 列为 NULL（仍出现）。
        stage_results 取末阶段（official_results.stage_idx）。供组织者导出 CSV。
        """
        with self._tx() as c:
            rows = c.execute(
                "SELECT e.id AS entry_id, e.seed, e.group_id, e.eliminated, e.registered_at, "
                "u.username AS owner_name, u.display_name AS owner_display, "
                "u.real_name, u.phone, u.school, u.student_id, "
                "b.name AS bot_name, b.display_name AS bot_display, "
                "r.rank, r.points, r.awarded, r.stage_idx, "
                "sr.wins, sr.draws, sr.losses, sr.net_chips "
                "FROM contest_entries e "
                "LEFT JOIN users u ON e.user_id=u.id "
                "LEFT JOIN bots b ON e.bot_id=b.id "
                "LEFT JOIN contest_official_results r "
                "  ON r.entry_id=e.id AND r.contest_id=e.contest_id "
                "LEFT JOIN contest_stage_results sr "
                "  ON sr.entry_id=e.id AND sr.contest_id=e.contest_id "
                "  AND sr.stage_idx=r.stage_idx "
                "WHERE e.contest_id=? "
                "ORDER BY CASE WHEN r.rank IS NULL THEN 999999 ELSE r.rank END, e.seed",
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
            "scheduled_at",
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

    def bind_contest_pairing_match(
        self,
        contest_id: int,
        pairing_id: int,
        match_id: str,
        *,
        activate_running: bool = False,
    ) -> dict:
        """原子绑定 prepared match，并可在同一事务把 published 赛事转 running。

        只接受仍属该赛事、仍为 pending 且 ``match_id IS NULL`` 的 pairing；这样
        challenge 准备成功后若绑定/提交失败，调用方可安全删除尚未启动的 match，
        不会留下 pairing 与 contest 状态的半提交。
        """
        with self._tx() as c:
            contest = c.execute(
                "SELECT status FROM contests WHERE id=?", (contest_id,)
            ).fetchone()
            allowed = ("published", "running")
            if not contest or contest["status"] not in allowed:
                raise ValueError("赛事状态已变化，不能绑定对局")
            cur = c.execute(
                "UPDATE contest_pairings SET match_id=?, status='running' "
                "WHERE id=? AND contest_id=? AND status='pending' AND match_id IS NULL",
                (match_id, pairing_id, contest_id),
            )
            if cur.rowcount != 1:
                raise ValueError("对阵已被派发或状态已变化")
            if activate_running:
                cur = c.execute(
                    "UPDATE contests SET status='running' "
                    "WHERE id=? AND status='published'",
                    (contest_id,),
                )
                if cur.rowcount != 1:
                    raise ValueError("赛事已不处于 published 状态")
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing_id,)
                ).fetchone()
            )

    def unbind_prepared_contest_match(
        self,
        contest_id: int,
        pairing_id: int,
        match_id: str,
        *,
        restore_published: bool = False,
    ) -> bool:
        """prepared match 启动失败时精确撤销刚完成的 pairing 绑定。"""
        with self._tx() as c:
            cur = c.execute(
                "UPDATE contest_pairings SET match_id=NULL, status='pending' "
                "WHERE id=? AND contest_id=? AND match_id=? AND status='running'",
                (pairing_id, contest_id, match_id),
            )
            if cur.rowcount != 1:
                return False
            if restore_published:
                other = c.execute(
                    "SELECT 1 FROM contest_pairings "
                    "WHERE contest_id=? AND match_id IS NOT NULL LIMIT 1",
                    (contest_id,),
                ).fetchone()
                if not other:
                    c.execute(
                        "UPDATE contests SET status='published' "
                        "WHERE id=? AND status='running'",
                        (contest_id,),
                    )
            return True

    # ── contest_stage_results ─────────────────────────────────

    def reset_aborted_contest_pairing(
        self, contest_id: int, match_id: str
    ) -> dict | None:
        """把一场无裁决的 aborted 赛事局从 pairing 上解绑。

        aborted match 行保留为审计/回放历史；只将仍精确绑定该
        match_id 的 pairing 原子复位为 pending，供后续安全重派。
        completed 或已被别的 match 取代的 pairing 绝不会被改写。
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            table = self._match_table_of(c, match_id)
            if not table:
                return None
            match = c.execute(
                f"SELECT status, contest_id FROM {table} WHERE id=?", (match_id,)
            ).fetchone()
            if (
                not match
                or match["status"] != STATUS_ABORTED
                or match["contest_id"] != contest_id
            ):
                return None
            pairing = c.execute(
                "SELECT * FROM contest_pairings "
                "WHERE contest_id=? AND match_id=? LIMIT 1",
                (contest_id, match_id),
            ).fetchone()
            if not pairing:
                return None
            cur = c.execute(
                "UPDATE contest_pairings SET match_id=NULL, status=? "
                "WHERE id=? AND contest_id=? AND match_id=?",
                (STATUS_PENDING, pairing["id"], contest_id, match_id),
            )
            if cur.rowcount != 1:
                return None
            return _row(
                c.execute(
                    "SELECT * FROM contest_pairings WHERE id=?", (pairing["id"],)
                ).fetchone()
            )

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

    def replace_official_results(
        self,
        contest_id: int,
        result_rows: list[dict[str, Any]],
    ) -> None:
        """Atomically replace the complete official ranking and publish readiness.

        ``DELETE`` + every replacement row + ``official_results_ready=1`` are one
        transaction.  A constraint error or process failure therefore preserves
        the previous complete ranking (or leaves a new contest at ready=0) instead
        of exposing a partial table behind a ready flag.
        """
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute(
                "SELECT 1 FROM contests WHERE id=?", (contest_id,)
            ).fetchone():
                raise ValueError("赛事不存在")
            c.execute(
                "DELETE FROM contest_official_results WHERE contest_id=?",
                (contest_id,),
            )
            for row in result_rows:
                c.execute(
                    "INSERT INTO contest_official_results"
                    "(contest_id, entry_id, stage_idx, rank, points, bot_id, user_id, "
                    "tiebreaks_json, awarded) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        contest_id,
                        row["entry_id"],
                        int(row.get("stage_idx") or 0),
                        row["rank"],
                        row.get("points") or 0,
                        row.get("bot_id"),
                        row.get("user_id"),
                        row.get("tiebreaks_json") or "{}",
                        row.get("awarded") or "",
                    ),
                )
            c.execute(
                "UPDATE contests SET official_results_ready=1 WHERE id=?",
                (contest_id,),
            )

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

    def delete_user_if_safe(self, user_id: int) -> dict:
        """原子拒绝会破坏活跃对局/赛事的管理员用户硬删。

        删除用户会经 ``users → bots`` 级联，不能只依赖 Bot 删除端点的保护。
        本方法在 ``BEGIN IMMEDIATE`` 事务内先汇总该用户全部 Bot 的活跃引用及
        其组织的赛事，再决定是否删除；这样另一个连接也不能在检查和 DELETE
        之间插入新的引用。完成态历史仍按 schema 的 SET NULL/CASCADE 契约保留。

        返回 ``found/deleted/bot_ids/blockers``；成功时调用方可用删除前保存的
        ``bot_ids`` 清理对应上传目录。
        """
        active_contest_statuses = (
            CONTEST_PUBLISHED,
            CONTEST_RUNNING,
            CONTEST_REST,
        )
        with self._tx() as c:
            c.execute("BEGIN IMMEDIATE")
            user = c.execute("SELECT id FROM users WHERE id=?", (user_id,)).fetchone()
            if not user:
                return {
                    "found": False,
                    "deleted": False,
                    "bot_ids": [],
                    "blockers": {},
                }

            bot_ids = [
                int(row["id"])
                for row in c.execute(
                    "SELECT id FROM bots WHERE owner_id=? ORDER BY id", (user_id,)
                ).fetchall()
            ]
            match_count = 0
            for gid in _all_game_ids():
                table = _matches_table(gid)
                row = c.execute(
                    f"SELECT COUNT(*) AS n FROM {table} "
                    "WHERE status IN (?,?) AND ("
                    "bot_a_id IN (SELECT id FROM bots WHERE owner_id=?) OR "
                    "bot_b_id IN (SELECT id FROM bots WHERE owner_id=?) OR "
                    "owner_id=? OR human_user_id=?)",
                    (
                        STATUS_PENDING,
                        STATUS_RUNNING,
                        user_id,
                        user_id,
                        user_id,
                        user_id,
                    ),
                ).fetchone()
                match_count += int(row["n"] if row else 0)

            status_marks = ",".join("?" for _ in active_contest_statuses)
            pairing_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_pairings cp "
                "JOIN contests contest ON contest.id=cp.contest_id "
                f"WHERE contest.status IN ({status_marks}) AND ("
                "cp.bot_a_id IN (SELECT id FROM bots WHERE owner_id=?) OR "
                "cp.bot_b_id IN (SELECT id FROM bots WHERE owner_id=?))",
                (*active_contest_statuses, user_id, user_id),
            ).fetchone()
            entry_row = c.execute(
                "SELECT COUNT(*) AS n FROM contest_entries entry "
                "JOIN contests contest ON contest.id=entry.contest_id "
                f"WHERE contest.status IN ({status_marks}) AND (entry.user_id=? OR "
                "entry.bot_id IN (SELECT id FROM bots WHERE owner_id=?))",
                (*active_contest_statuses, user_id, user_id),
            ).fetchone()
            organized_row = c.execute(
                "SELECT COUNT(*) AS n FROM contests WHERE organizer_id=?", (user_id,)
            ).fetchone()
            blockers = {
                "matches": match_count,
                "contest_pairings": int(pairing_row["n"] if pairing_row else 0),
                "contest_entries": int(entry_row["n"] if entry_row else 0),
                "organized_contests": int(organized_row["n"] if organized_row else 0),
            }
            if any(blockers.values()):
                return {
                    "found": True,
                    "deleted": False,
                    "bot_ids": bot_ids,
                    "blockers": blockers,
                }

            deleted = c.execute("DELETE FROM users WHERE id=?", (user_id,)).rowcount > 0
            return {
                "found": True,
                "deleted": deleted,
                "bot_ids": bot_ids,
                "blockers": blockers,
            }

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
