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
            f"SELECT {', '.join(present)} FROM contests"
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

    if "users" in tables:
        _add_col(conn, "users", "bio", "TEXT NOT NULL DEFAULT ''")
        _add_col(conn, "users", "avatar", "TEXT NOT NULL DEFAULT ''")

    if "matches" in tables:
        _add_col(conn, "matches", "game_id", "TEXT NOT NULL DEFAULT 'holdem'")
        _add_col(conn, "matches", "n_dots", "INTEGER")  # pencil 点阵边长（可空）
        _add_col(conn, "matches", "human_user_id", "INTEGER")  # 人类对战：人类用户 id
        _add_col(conn, "matches", "human_seat", "INTEGER")  # 人类坐位 0/1
        # 放宽 match_type CHECK 以纳入 'ladder' / 'human'
        m_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='matches'"
        ).fetchone()
        m_sql_text = (m_sql[0] or "") if m_sql else ""
        if "'human'" not in m_sql_text:
            m_cols = _table_cols(conn, "matches")
            conn.execute(
                """
                CREATE TABLE matches_new (
                    id              TEXT    PRIMARY KEY,
                    bot_a_id        INTEGER NOT NULL REFERENCES bots(id),
                    bot_b_id        INTEGER NOT NULL REFERENCES bots(id),
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
                    game_id         TEXT    NOT NULL DEFAULT 'holdem',
                    n_dots          INTEGER,
                    human_user_id   INTEGER,
                    human_seat      INTEGER,
                    started_at      TEXT,
                    ended_at      TEXT,
                    created_at      TEXT    NOT NULL,
                    CONSTRAINT chk_winner2 CHECK (winner IN (0, 1) OR winner IS NULL),
                    CONSTRAINT chk_status2 CHECK (status IN ('pending','running','completed','aborted')),
                    CONSTRAINT chk_type2 CHECK (match_type IN ('challenge','table','contest','ladder','human'))
                )
                """
            )
            present_m = [c for c in (
                "id", "bot_a_id", "bot_b_id", "owner_id", "contest_id",
                "hands_played", "total_hands", "earnings_a", "earnings_b",
                "winner", "reason", "net_bb_a", "match_type", "status",
                "started_at", "ended_at", "created_at", "game_id", "n_dots",
                "human_user_id", "human_seat",
            ) if c in m_cols]
            conn.execute(
                f"INSERT INTO matches_new ({', '.join(present_m)}) "
                f"SELECT {', '.join(present_m)} FROM matches"
            )
            conn.execute("DROP TABLE matches")
            conn.execute("ALTER TABLE matches_new RENAME TO matches")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_bot_a ON matches(bot_a_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_bot_b ON matches(bot_b_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_owner ON matches(owner_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_contest ON matches(contest_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_matches_time ON matches(created_at)")

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


class Store:
    """SQLite 存储。线程安全；持久连接 check_same_thread=False。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path or DEFAULT_DB_PATH
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._tx() as conn:
            conn.executescript(SCHEMA)
            _migrate(conn)
            seed_email_templates(conn, _now())

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
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO users(username, email, password_hash, role, "
                "display_name, created_at) VALUES(?,?,?,?,?,?)",
                (
                    username,
                    email,
                    password_hash,
                    role,
                    display_name or username,
                    _now(),
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
                "created_at, last_login_at FROM users WHERE username=? AND is_active=1",
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
                "FROM ratings r JOIN bots b ON r.bot_id=b.id "
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
                "FROM ratings r JOIN bots b ON r.bot_id=b.id WHERE b.owner_id=?",
                (owner_id,),
            ).fetchone()
            return _row(agg) if agg else {
                "wins": 0, "losses": 0, "draws": 0,
                "matches_played": 0, "net_chips": 0,
            }

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
                "LEFT JOIN ratings r ON r.bot_id=b.id "
                "WHERE b.is_public=1 AND b.is_active=1 "
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
            sql = (
                "SELECT m.*, ba.name AS bot_a_name, bb.name AS bot_b_name, "
                "ba.display_name AS bot_a_display, bb.display_name AS bot_b_display "
                "FROM matches m "
                "JOIN bots ba ON m.bot_a_id=ba.id "
                "JOIN bots bb ON m.bot_b_id=bb.id "
                "WHERE m.status='completed' "
                "AND (LOWER(ba.name) LIKE ? OR LOWER(bb.name) LIKE ? "
                "OR LOWER(ba.display_name) LIKE ? OR LOWER(bb.display_name) LIKE ?)"
            )
            params: list[Any] = [ql, ql, ql, ql]
            if game_id:
                sql += " AND m.game_id=?"
                params.append(game_id)
            sql += " ORDER BY m.created_at DESC LIMIT ?"
            params.append(max(1, min(limit, 50)))
            return [_row(r) for r in c.execute(sql, params)]

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
        is_public = 1 if fields.get("is_public", True) else 0
        game_id = fields.get("game_id") or "holdem"
        now = _now()
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO bots(owner_id, name, display_name, description, "
                "os, arch, format, binary_path, is_builtin, is_public, game_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
                    is_public,
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
            "is_public",
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
        public_only: bool = False,
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
            if public_only:
                sql += " AND is_public=1"
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
                "LEFT JOIN ratings r ON r.bot_id=b.id "
                "WHERE b.id=?",
                (bot_id,),
            ).fetchone()
            d = _row(row)
            if d is not None:
                from bzplat.backend.engine.tiers import tier_for
                t = tier_for(d.get("rating"))
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

    # ── matches ───────────────────────────────────────────────

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
        with self._tx() as c:
            c.execute(
                "INSERT INTO matches(id, bot_a_id, bot_b_id, owner_id, "
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
                    game_id or "holdem",
                    n_dots,
                    human_user_id,
                    human_seat,
                    _now(),
                ),
            )
            return _row(
                c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
            )

    def get_match(self, match_id: str) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
            )

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
        }
        sets = [f"{k}=?" for k in fields if k in allowed]
        vals = [v for k, v in fields.items() if k in allowed]
        with self._tx() as c:
            if sets:
                vals.append(match_id)
                c.execute(f"UPDATE matches SET {','.join(sets)} WHERE id=?", vals)
            return _row(
                c.execute("SELECT * FROM matches WHERE id=?", (match_id,)).fetchone()
            )

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
        with self._tx() as c:
            sql = (
                "SELECT m.*, ba.name AS bot_a_name, bb.name AS bot_b_name, "
                "ba.display_name AS bot_a_display, "
                "bb.display_name AS bot_b_display "
                "FROM matches m "
                "JOIN bots ba ON m.bot_a_id=ba.id "
                "JOIN bots bb ON m.bot_b_id=bb.id WHERE 1=1"
            )
            params: list[Any] = []
            if owner_id is not None:
                sql += " AND m.owner_id=?"
                params.append(owner_id)
            if bot_id is not None:
                sql += " AND (m.bot_a_id=? OR m.bot_b_id=?)"
                params.extend([bot_id, bot_id])
            if contest_id is not None:
                sql += " AND m.contest_id=?"
                params.append(contest_id)
            if status:
                sql += " AND m.status=?"
                params.append(status)
            if game_id:
                sql += " AND m.game_id=?"
                params.append(game_id)
            sql += " ORDER BY m.created_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])
            return [_row(r) for r in c.execute(sql, params)]

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

    # ── ratings ───────────────────────────────────────────────

    def ensure_rating(self, bot_id: int) -> dict:
        with self._tx() as c:
            existing = c.execute(
                "SELECT * FROM ratings WHERE bot_id=?", (bot_id,)
            ).fetchone()
            if existing:
                return _row(existing)
            c.execute(
                "INSERT INTO ratings(bot_id) VALUES(?)",
                (bot_id,),
            )
            return _row(
                c.execute(
                    "SELECT * FROM ratings WHERE bot_id=?", (bot_id,)
                ).fetchone()
            )

    def get_rating(self, bot_id: int) -> dict | None:
        with self._tx() as c:
            return _row(
                c.execute(
                    "SELECT * FROM ratings WHERE bot_id=?", (bot_id,)
                ).fetchone()
            )

    def update_rating_row(self, bot_id: int, **fields: Any) -> dict | None:
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
            existing = c.execute(
                "SELECT bot_id FROM ratings WHERE bot_id=?", (bot_id,)
            ).fetchone()
            if not existing:
                c.execute("INSERT INTO ratings(bot_id) VALUES(?)", (bot_id,))
            if sets:
                vals.append(bot_id)
                c.execute(
                    f"UPDATE ratings SET {','.join(sets)} WHERE bot_id=?", vals
                )
            return _row(
                c.execute(
                    "SELECT * FROM ratings WHERE bot_id=?", (bot_id,)
                ).fetchone()
            )

    def list_leaderboard(
        self, limit: int = 50, *, game_id: str | None = None
    ) -> list[dict]:
        with self._tx() as c:
            # rating_delta = 当前 rating - 上一条历史评分（升降趋势）；无历史则 NULL
            sql = (
                "SELECT r.bot_id, r.rating, r.rd, r.vol, r.wins, r.losses, "
                "r.draws, r.net_chips, r.matches_played, r.last_played_at, "
                "b.name AS bot_name, b.display_name AS bot_display, "
                "b.format, b.os, b.arch, b.is_builtin, b.game_id, "
                "u.username AS owner_name, u.display_name AS owner_display, "
                "(SELECT rh.rating FROM rating_history rh "
                " WHERE rh.bot_id=r.bot_id ORDER BY rh.id DESC "
                " LIMIT 1 OFFSET 1) AS prev_rating "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id "
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
            from bzplat.backend.engine.tiers import tier_for
            for row in rows:
                prev = row.pop("prev_rating", None)
                if prev is not None:
                    row["rating_delta"] = round(row["rating"] - prev, 2)
                else:
                    row["rating_delta"] = None
                t = tier_for(row["rating"])
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
                "b.name AS bot_name, b.game_id, b.binary_path, b.is_active, b.is_public, b.is_builtin "
                "FROM ratings r JOIN bots b ON r.bot_id=b.id "
                "WHERE b.is_active=1 AND b.is_public=1 AND b.is_builtin=0 "
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

    def count_matches(self, status: str | None = None) -> int:
        """按 status 统计对局数；status=None 时返回全部。"""
        with self._tx() as c:
            if status:
                row = c.execute(
                    "SELECT COUNT(*) FROM matches WHERE status=?", (status,)
                ).fetchone()
            else:
                row = c.execute("SELECT COUNT(*) FROM matches").fetchone()
            return int(row[0]) if row else 0

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
    ) -> None:
        """落一条评分快照，并截断保留最近 N 条（N=200）。"""
        with self._tx() as c:
            c.execute(
                "INSERT INTO rating_history(bot_id, rating, rd, vol, "
                "matches_played, reason, created_at) VALUES(?,?,?,?,?,?,?)",
                (bot_id, rating, rd, vol, matches_played, reason, _now()),
            )
            # 截断：保留每 bot 最近 200 条
            c.execute(
                "DELETE FROM rating_history WHERE bot_id=? AND id NOT IN "
                "(SELECT id FROM rating_history WHERE bot_id=? "
                "ORDER BY id DESC LIMIT 200)",
                (bot_id, bot_id),
            )

    def list_rating_history(
        self, bot_id: int, *, limit: int = 100
    ) -> list[dict]:
        """返回评分历史时序（旧→新），用于画曲线。"""
        with self._tx() as c:
            rows = c.execute(
                "SELECT id, rating, rd, vol, matches_played, reason, created_at "
                "FROM rating_history WHERE bot_id=? "
                "ORDER BY id DESC LIMIT ?",
                (bot_id, max(1, min(limit, 500))),
            ).fetchall()
            return [_row(r) for r in reversed(rows)]

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
                "LEFT JOIN ratings r ON r.bot_id=b.id "
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
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO contests(title, description, organizer_id, status, "
                "registration_opens_at, registration_closes_at, starts_at, "
                "ends_at, hands_per_match, created_at, game_id, stages_json, "
                "current_stage_idx, template_id, match_config_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
        self, *, status: str | None = None, organizer_id: int | None = None
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
    ) -> dict:
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO contest_pairings(contest_id, round_num, bot_a_id, "
                "bot_b_id, match_id, status, stage_idx, stage_key, group_id, "
                "bracket_slot, color_first) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    contest_id,
                    round_num,
                    bot_a_id,
                    bot_b_id,
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
            rows = c.execute(
                "SELECT p.*, ba.name AS bot_a_name, ba.display_name AS bot_a_display, "
                "bb.name AS bot_b_name, bb.display_name AS bot_b_display, "
                "ua.username AS owner_a_name, ub.username AS owner_b_name, "
                "m.winner AS match_winner "
                "FROM contest_pairings p "
                "JOIN bots ba ON p.bot_a_id=ba.id "
                "JOIN bots bb ON p.bot_b_id=bb.id "
                "LEFT JOIN users ua ON ba.owner_id=ua.id "
                "LEFT JOIN users ub ON bb.owner_id=ub.id "
                "LEFT JOIN matches m ON p.match_id=m.id "
                "WHERE p.contest_id=? "
                "ORDER BY p.stage_idx, p.round_num, p.id",
                (contest_id,),
            ).fetchall()
            return [_row(r) for r in rows]

    def contest_entries_named(self, contest_id: int) -> list[dict]:
        """返回报名（带 bot 名/owner 名 + seed/group/eliminated）。"""
        with self._tx() as c:
            rows = c.execute(
                "SELECT e.*, b.name AS bot_name, b.display_name AS bot_display, "
                "b.game_id, u.username AS owner_name, u.display_name AS owner_display "
                "FROM contest_entries e "
                "JOIN bots b ON e.bot_id=b.id "
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
            "bot_a_id",
            "bot_b_id",
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
        bot_id: int,
        *,
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
                "(contest_id, stage_idx, stage_key, bot_id, points, wins, draws, "
                "losses, net_chips, group_id, rank_in_group, payload_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(contest_id, stage_idx, bot_id) DO UPDATE SET "
                "stage_key=excluded.stage_key, points=excluded.points, "
                "wins=excluded.wins, draws=excluded.draws, losses=excluded.losses, "
                "net_chips=excluded.net_chips, group_id=excluded.group_id, "
                "rank_in_group=excluded.rank_in_group, "
                "payload_json=excluded.payload_json",
                (
                    contest_id, stage_idx, stage_key, bot_id, points, wins,
                    draws, losses, net_chips, group_id, rank_in_group, payload_json,
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
        """一次性聚合各表计数 + 对局按状态分组 + 最近趋势。"""
        with self._tx() as c:
            def one(sql: str, *p: Any) -> int:
                return int(c.execute(sql, p).fetchone()[0])

            stats = {
                "users": one("SELECT COUNT(*) FROM users"),
                "users_active": one("SELECT COUNT(*) FROM users WHERE is_active=1"),
                "users_verified": one("SELECT COUNT(*) FROM users WHERE email_verified=1"),
                "bots": one("SELECT COUNT(*) FROM bots"),
                "bots_active": one("SELECT COUNT(*) FROM bots WHERE is_active=1"),
                "matches": one("SELECT COUNT(*) FROM matches"),
                "matches_completed": one("SELECT COUNT(*) FROM matches WHERE status='completed'"),
                "matches_aborted": one("SELECT COUNT(*) FROM matches WHERE status='aborted'"),
                "matches_running": one("SELECT COUNT(*) FROM matches WHERE status='running'"),
                "matches_pending": one("SELECT COUNT(*) FROM matches WHERE status='pending'"),
                "contests": one("SELECT COUNT(*) FROM contests"),
                "contests_running": one("SELECT COUNT(*) FROM contests WHERE status='running'"),
                "active_sessions": one(
                    "SELECT COUNT(*) FROM sessions WHERE expires_at > ?",
                    _now(),
                ),
            }
            # 按对局状态分组
            rows = c.execute(
                "SELECT status, COUNT(*) AS n FROM matches GROUP BY status"
            ).fetchall()
            stats["matches_by_status"] = {r["status"]: int(r["n"]) for r in rows}
            # 最近 7 天每日新对局数
            recent = c.execute(
                "SELECT substr(created_at,1,10) AS d, COUNT(*) AS n "
                "FROM matches WHERE created_at >= date('now','-7 days') "
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
