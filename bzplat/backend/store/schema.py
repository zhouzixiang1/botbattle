"""botzone-platform SQLite schema.

二进制 Bot 竞赛平台数据模型：用户 / Bot 版本 / 对局 / 评分 / 比赛 / 认证邮件。
"""
from __future__ import annotations

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    username        TEXT    NOT NULL UNIQUE,
    email           TEXT    NOT NULL UNIQUE,
    password_hash   TEXT    NOT NULL,
    role            TEXT    NOT NULL DEFAULT 'user',
    display_name    TEXT    NOT NULL DEFAULT '',
    is_active       INTEGER NOT NULL DEFAULT 1,
    email_verified  INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    last_login_at   TEXT,
    CONSTRAINT chk_username CHECK (length(username) >= 3),
    CONSTRAINT chk_role CHECK (role IN ('user', 'organizer', 'admin'))
);

CREATE TABLE IF NOT EXISTS bots (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name            TEXT    NOT NULL,
    display_name    TEXT    NOT NULL DEFAULT '',
    description     TEXT    NOT NULL DEFAULT '',
    os              TEXT    NOT NULL DEFAULT '',
    arch            TEXT    NOT NULL DEFAULT '',
    format          TEXT    NOT NULL DEFAULT 'unknown',
    binary_path     TEXT    NOT NULL DEFAULT '',
    current_version INTEGER NOT NULL DEFAULT 0,
    is_public       INTEGER NOT NULL DEFAULT 1,
    is_active       INTEGER NOT NULL DEFAULT 1,
    is_builtin      INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(owner_id, name),
    CONSTRAINT chk_format CHECK (format IN ('elf', 'pe', 'macho', 'unknown'))
);

CREATE TABLE IF NOT EXISTS bot_versions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    binary_path     TEXT    NOT NULL,
    upload_note     TEXT    NOT NULL DEFAULT '',
    checksum        TEXT    NOT NULL DEFAULT '',
    size_bytes      INTEGER NOT NULL DEFAULT 0,
    os              TEXT    NOT NULL DEFAULT '',
    arch            TEXT    NOT NULL DEFAULT '',
    format          TEXT    NOT NULL DEFAULT 'unknown',
    uploaded_at     TEXT    NOT NULL,
    UNIQUE(bot_id, version)
);

CREATE TABLE IF NOT EXISTS contests (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    title                   TEXT    NOT NULL,
    description             TEXT    NOT NULL DEFAULT '',
    organizer_id            INTEGER NOT NULL REFERENCES users(id),
    status                  TEXT    NOT NULL DEFAULT 'draft',
    registration_opens_at   TEXT,
    registration_closes_at  TEXT,
    starts_at               TEXT,
    ends_at                 TEXT,
    hands_per_match         INTEGER NOT NULL DEFAULT 70,
    created_at              TEXT    NOT NULL,
    CONSTRAINT chk_contest_status CHECK (
        status IN ('draft','open','running','finished','cancelled'))
);

CREATE TABLE IF NOT EXISTS matches (
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
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT    NOT NULL,
    CONSTRAINT chk_winner CHECK (winner IN (0, 1) OR winner IS NULL),
    CONSTRAINT chk_status CHECK (status IN ('pending','running','completed','aborted')),
    CONSTRAINT chk_type CHECK (match_type IN ('challenge','table','contest'))
);

CREATE TABLE IF NOT EXISTS match_replays (
    match_id        TEXT    PRIMARY KEY REFERENCES matches(id) ON DELETE CASCADE,
    events_json     TEXT    NOT NULL DEFAULT '[]',
    hands_json      TEXT    NOT NULL DEFAULT '[]',
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS ratings (
    bot_id          INTEGER PRIMARY KEY REFERENCES bots(id) ON DELETE CASCADE,
    rating          REAL    NOT NULL DEFAULT 1500.0,
    rd              REAL    NOT NULL DEFAULT 350.0,
    vol             REAL    NOT NULL DEFAULT 0.06,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    draws           INTEGER NOT NULL DEFAULT 0,
    net_chips       INTEGER NOT NULL DEFAULT 0,
    matches_played  INTEGER NOT NULL DEFAULT 0,
    last_played_at  TEXT
);

CREATE TABLE IF NOT EXISTS pair_stats (
    bot_a_id        INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    bot_b_id        INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    bb_per_100_mean REAL    NOT NULL DEFAULT 0,
    ci_low          REAL,
    ci_high         REAL,
    samples         INTEGER NOT NULL DEFAULT 0,
    last_played_at  TEXT    NOT NULL,
    PRIMARY KEY (bot_a_id, bot_b_id)
);

CREATE TABLE IF NOT EXISTS sessions (
    token           TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at      TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    ip_addr         TEXT    NOT NULL DEFAULT '',
    user_agent      TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS password_resets (
    token           TEXT    PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at      TEXT    NOT NULL,
    used_at         TEXT,
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS email_codes (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    purpose         TEXT    NOT NULL,
    code            TEXT    NOT NULL,
    expires_at      TEXT    NOT NULL,
    used_at         TEXT,
    created_at      TEXT    NOT NULL,
    CONSTRAINT chk_purpose CHECK (purpose IN ('verify', 'reset'))
);

CREATE TABLE IF NOT EXISTS email_templates (
    key             TEXT    PRIMARY KEY,
    subject         TEXT    NOT NULL,
    body_html       TEXT    NOT NULL DEFAULT '',
    body_text       TEXT    NOT NULL DEFAULT '',
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS email_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    to_addr         TEXT    NOT NULL,
    subject         TEXT    NOT NULL,
    template_key    TEXT    NOT NULL DEFAULT '',
    status          TEXT    NOT NULL DEFAULT 'sent',
    error           TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS contest_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id      INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bot_id          INTEGER NOT NULL REFERENCES bots(id),
    registered_at   TEXT    NOT NULL,
    UNIQUE(contest_id, user_id)
);

CREATE TABLE IF NOT EXISTS contest_pairings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id      INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    round_num       INTEGER NOT NULL DEFAULT 1,
    bot_a_id        INTEGER NOT NULL REFERENCES bots(id),
    bot_b_id        INTEGER NOT NULL REFERENCES bots(id),
    match_id        TEXT    REFERENCES matches(id) ON DELETE SET NULL,
    status          TEXT    NOT NULL DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_bots_owner ON bots(owner_id);
CREATE INDEX IF NOT EXISTS idx_bot_versions_bot ON bot_versions(bot_id);
CREATE INDEX IF NOT EXISTS idx_matches_bot_a ON matches(bot_a_id);
CREATE INDEX IF NOT EXISTS idx_matches_bot_b ON matches(bot_b_id);
CREATE INDEX IF NOT EXISTS idx_matches_owner ON matches(owner_id);
CREATE INDEX IF NOT EXISTS idx_matches_contest ON matches(contest_id);
CREATE INDEX IF NOT EXISTS idx_matches_status ON matches(status);
CREATE INDEX IF NOT EXISTS idx_matches_time ON matches(created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_email_codes_user ON email_codes(user_id, purpose);
CREATE INDEX IF NOT EXISTS idx_contests_org ON contests(organizer_id);
CREATE INDEX IF NOT EXISTS idx_contest_entries_c ON contest_entries(contest_id);
CREATE INDEX IF NOT EXISTS idx_contest_pairings_c ON contest_pairings(contest_id);
"""

# 角色
ROLE_USER = "user"
ROLE_ORGANIZER = "organizer"
ROLE_ADMIN = "admin"

# 对局状态
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"

# 对局类型
TYPE_CHALLENGE = "challenge"
TYPE_TABLE = "table"
TYPE_CONTEST = "contest"

# 比赛状态
CONTEST_DRAFT = "draft"
CONTEST_OPEN = "open"
CONTEST_RUNNING = "running"
CONTEST_FINISHED = "finished"
CONTEST_CANCELLED = "cancelled"

# 二进制格式
FMT_ELF = "elf"
FMT_PE = "pe"
FMT_MACHO = "macho"
FMT_UNKNOWN = "unknown"

# 邮件模板
TPL_VERIFY_EMAIL = "verify_email"
TPL_RESET_PASSWORD = "reset_password"
TPL_WELCOME = "welcome"

# 邮箱验证码用途
CODE_VERIFY = "verify"
CODE_RESET = "reset"
