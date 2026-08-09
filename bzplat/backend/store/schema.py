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
    bio             TEXT    NOT NULL DEFAULT '',
    avatar          TEXT    NOT NULL DEFAULT '',
    xp              INTEGER NOT NULL DEFAULT 0,
    level           INTEGER NOT NULL DEFAULT 0,
    last_active_at  TEXT,
    real_name       TEXT    NOT NULL DEFAULT '',
    phone           TEXT    NOT NULL DEFAULT '',
    school          TEXT    NOT NULL DEFAULT '',
    student_id      TEXT    NOT NULL DEFAULT '',
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
    is_active       INTEGER NOT NULL DEFAULT 1,
    is_builtin      INTEGER NOT NULL DEFAULT 0,
    game_id         TEXT    NOT NULL DEFAULT 'holdem',
    runtime_mode    TEXT    NOT NULL DEFAULT 'longrunning',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(owner_id, name),
    CONSTRAINT chk_format CHECK (format IN ('elf', 'pe', 'macho', 'unknown')),
    CONSTRAINT chk_runtime CHECK (runtime_mode IN ('traditional', 'longrunning'))
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
    runtime_mode    TEXT    NOT NULL DEFAULT 'longrunning',
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
    game_id                 TEXT    NOT NULL DEFAULT 'holdem',
    stages_json             TEXT    NOT NULL DEFAULT '[]',
    current_stage_idx       INTEGER NOT NULL DEFAULT 0,
    template_id             TEXT    NOT NULL DEFAULT 'holdem_swiss_ko',
    rest_ends_at            TEXT,
    match_config_json       TEXT    NOT NULL DEFAULT '{}',
    phase                   TEXT    NOT NULL DEFAULT 'standalone',  -- P2: preliminary/final/standalone
    source_contest_id       INTEGER,  -- P2: 软链（预赛→决赛导航，不复制 entry）
    official_results_ready  INTEGER NOT NULL DEFAULT 0,  -- P2: 全员正式名次是否已落库
    require_real_name       INTEGER NOT NULL DEFAULT 0,  -- 报名是否要求实名
    CONSTRAINT chk_contest_status CHECK (
        status IN ('draft','open','published','running','rest','finished','cancelled'))
);

-- 对局表（全面解耦 PR3：拆每游戏一张表 + matches_index 定位）
-- 三表结构完全一致（含所有列，游戏专属列在其他游戏中默认 NULL/0），
-- 便于跨游戏 UNION ALL 聚合。matches_index(id, game_id) 供 get_match(id) 定位。
CREATE TABLE IF NOT EXISTS matches_holdem (
    id              TEXT    PRIMARY KEY,
    bot_a_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    bot_b_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    owner_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    contest_id      INTEGER REFERENCES contests(id) ON DELETE SET NULL,
    winner          INTEGER,
    reason          TEXT    NOT NULL DEFAULT 'completed',
    match_type      TEXT    NOT NULL DEFAULT 'challenge',
    status          TEXT    NOT NULL DEFAULT 'pending',
    game_id         TEXT    NOT NULL DEFAULT 'holdem',
    match_config    TEXT    NOT NULL DEFAULT '{}',
    result          TEXT    NOT NULL DEFAULT '{}',
    human_user_id   INTEGER,
    human_seat      INTEGER,
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT    NOT NULL,
    likes_count     INTEGER NOT NULL DEFAULT 0,
    views_count     INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT chk_winner_h CHECK (winner IN (0, 1) OR winner IS NULL),
    CONSTRAINT chk_status_h CHECK (status IN ('pending','running','completed','aborted')),
    CONSTRAINT chk_type_h CHECK (match_type IN ('challenge','table','contest','ladder','human'))
);
CREATE TABLE IF NOT EXISTS matches_gomoku (
    id              TEXT    PRIMARY KEY,
    bot_a_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    bot_b_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    owner_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    contest_id      INTEGER REFERENCES contests(id) ON DELETE SET NULL,
    winner          INTEGER,
    reason          TEXT    NOT NULL DEFAULT 'completed',
    match_type      TEXT    NOT NULL DEFAULT 'challenge',
    status          TEXT    NOT NULL DEFAULT 'pending',
    game_id         TEXT    NOT NULL DEFAULT 'gomoku',
    match_config    TEXT    NOT NULL DEFAULT '{}',
    result          TEXT    NOT NULL DEFAULT '{}',
    human_user_id   INTEGER,
    human_seat      INTEGER,
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT    NOT NULL,
    likes_count     INTEGER NOT NULL DEFAULT 0,
    views_count     INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT chk_winner_g CHECK (winner IN (0, 1) OR winner IS NULL),
    CONSTRAINT chk_status_g CHECK (status IN ('pending','running','completed','aborted')),
    CONSTRAINT chk_type_g CHECK (match_type IN ('challenge','table','contest','ladder','human'))
);
CREATE TABLE IF NOT EXISTS matches_pencil (
    id              TEXT    PRIMARY KEY,
    bot_a_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    bot_b_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    owner_id        INTEGER REFERENCES users(id) ON DELETE SET NULL,
    contest_id      INTEGER REFERENCES contests(id) ON DELETE SET NULL,
    winner          INTEGER,
    reason          TEXT    NOT NULL DEFAULT 'completed',
    match_type      TEXT    NOT NULL DEFAULT 'challenge',
    status          TEXT    NOT NULL DEFAULT 'pending',
    game_id         TEXT    NOT NULL DEFAULT 'pencil',
    match_config    TEXT    NOT NULL DEFAULT '{}',
    result          TEXT    NOT NULL DEFAULT '{}',
    human_user_id   INTEGER,
    human_seat      INTEGER,
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT    NOT NULL,
    likes_count     INTEGER NOT NULL DEFAULT 0,
    views_count     INTEGER NOT NULL DEFAULT 0,
    CONSTRAINT chk_winner_p CHECK (winner IN (0, 1) OR winner IS NULL),
    CONSTRAINT chk_status_p CHECK (status IN ('pending','running','completed','aborted')),
    CONSTRAINT chk_type_p CHECK (match_type IN ('challenge','table','contest','ladder','human'))
);
-- 跨游戏对局定位：id → game_id（get_match(id) 先查此表定位到哪张 matches_<game>）
CREATE TABLE IF NOT EXISTS matches_index (
    id              TEXT    PRIMARY KEY,
    game_id         TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS match_replays (
    match_id        TEXT    PRIMARY KEY,
    events_json     TEXT    NOT NULL DEFAULT '[]',
    hands_json      TEXT    NOT NULL DEFAULT '[]',
    updated_at      TEXT    NOT NULL
);

-- 非赛事 Bot 对局的全局评分结算凭据。match completed 与评分写入分属两个
-- 事务；本表的 claim 与 ratings/history/pair_stats 同事务，保证重试恰好一次。
-- matches 已按游戏分表，无法声明单一物理 FK；删除对局时由 Store.delete_match 清理。
CREATE TABLE IF NOT EXISTS match_rating_settlements (
    match_id        TEXT    PRIMARY KEY,
    settled_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS ratings (
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
);

CREATE TABLE IF NOT EXISTS pair_stats (
    bot_a_id        INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    bot_b_id        INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    bb_per_100_mean REAL    NOT NULL DEFAULT 0,
    ci_low          REAL,
    ci_high         REAL,
    samples         INTEGER NOT NULL DEFAULT 0,
    last_played_at  TEXT    NOT NULL,
    a_wins          INTEGER NOT NULL DEFAULT 0,
    a_losses        INTEGER NOT NULL DEFAULT 0,
    draws           INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (bot_a_id, bot_b_id)
);

-- 评分历史快照：每次 _apply_ratings 落一条，用于段位趋势/曲线（PR-1 建表 + 落盘，
-- PR-5 段位趋势读取）。每 bot 限保留最近 N 条（见 store 截断）。
CREATE TABLE IF NOT EXISTS rating_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    game_id         TEXT    NOT NULL DEFAULT 'holdem',
    rating          REAL    NOT NULL,
    rd              REAL    NOT NULL,
    vol             REAL    NOT NULL,
    matches_played  INTEGER NOT NULL,
    reason          TEXT    NOT NULL DEFAULT '',   -- match_id 或 'contest:<id>' 等
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_rating_history_bot ON rating_history(bot_id, game_id, id DESC);

-- 评论（target_type = match|bot；target_id = match_id 字符串 或 bot_id 整数）
CREATE TABLE IF NOT EXISTS comments (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    target_type     TEXT    NOT NULL,   -- 'match' | 'bot'
    target_id       TEXT    NOT NULL,   -- match_id 或 bot_id（统一字符串）
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    body            TEXT    NOT NULL,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_comments_target ON comments(target_type, target_id, id DESC);

-- 点赞（target_type = match|bot|comment）
CREATE TABLE IF NOT EXISTS likes (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    target_type     TEXT    NOT NULL,   -- 'match' | 'bot' | 'comment'
    target_id       TEXT    NOT NULL,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (user_id, target_type, target_id)
);
CREATE INDEX IF NOT EXISTS idx_likes_target ON likes(target_type, target_id);

-- 关注关系（follower 关注 followee）
CREATE TABLE IF NOT EXISTS follows (
    follower_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    followee_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (follower_id, followee_id),
    CHECK (follower_id <> followee_id)
);
CREATE INDEX IF NOT EXISTS idx_follows_followee ON follows(followee_id);
CREATE INDEX IF NOT EXISTS idx_follows_follower ON follows(follower_id);

-- 收藏 Bot（user 收藏 bot）
CREATE TABLE IF NOT EXISTS favorites (
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    created_at      TEXT    NOT NULL,
    PRIMARY KEY (user_id, bot_id)
);
CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites(user_id);
CREATE INDEX IF NOT EXISTS idx_favorites_bot ON favorites(bot_id);

-- 站内通知：对局完成 / 被关注 / 赛事阶段变化 / 被评论等
CREATE TABLE IF NOT EXISTS notifications (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    type            TEXT    NOT NULL DEFAULT '',   -- match_done|followed|contest|comment|...
    title           TEXT    NOT NULL DEFAULT '',
    body            TEXT    NOT NULL DEFAULT '',
    link            TEXT    NOT NULL DEFAULT '',   -- 前端路由（如 /match/:id）
    is_read         INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id, id DESC);

-- 通知/邮件偏好（每用户一行）
CREATE TABLE IF NOT EXISTS notification_prefs (
    user_id             INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    email_match_done    INTEGER NOT NULL DEFAULT 0,
    email_followed      INTEGER NOT NULL DEFAULT 0,
    email_contest       INTEGER NOT NULL DEFAULT 0,
    email_comment       INTEGER NOT NULL DEFAULT 0
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
    bot_id          INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    registered_at   TEXT    NOT NULL,
    group_id        TEXT    NOT NULL DEFAULT '',
    seed            INTEGER NOT NULL DEFAULT 0,
    eliminated      INTEGER NOT NULL DEFAULT 0,
    dispatched_at   TEXT,
    UNIQUE(contest_id, user_id)
);

CREATE TABLE IF NOT EXISTS contest_pairings (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id      INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    round_num       INTEGER NOT NULL DEFAULT 1,
    entry_a_id      INTEGER,
    entry_b_id      INTEGER,
    bot_a_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    bot_b_id        INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    bot_a_version_id INTEGER,  -- P1：发布轮冻结的 bot 版本（→ bot_versions.binary_path）
    bot_b_version_id INTEGER,
    pairing_seed    INTEGER,   -- P1：轮次确定性 seed（duplicate/复现用）
    published_at    TEXT,      -- P1：发布时间戳（非 NULL = 已发布轮，dispatch 不改）
    scheduled_at    TEXT,      -- 计划开赛时间（NULL=立即可打；逐场排期用，scheduler 到点 dispatch）
    match_id        TEXT,  -- 逻辑外键，指向 matches_<game>.id（经 matches_index 定位）；无 DB 级 FK
    status          TEXT    NOT NULL DEFAULT 'pending',
    stage_idx       INTEGER NOT NULL DEFAULT 0,
    stage_key       TEXT    NOT NULL DEFAULT '',
    group_id        TEXT    NOT NULL DEFAULT '',
    bracket_slot    INTEGER,
    color_first     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS contest_stage_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id      INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    stage_idx       INTEGER NOT NULL,
    stage_key       TEXT    NOT NULL DEFAULT '',
    entry_id        INTEGER,
    bot_id          INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    points          REAL    NOT NULL DEFAULT 0,
    wins            INTEGER NOT NULL DEFAULT 0,
    draws           INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    net_chips       INTEGER NOT NULL DEFAULT 0,
    group_id        TEXT    NOT NULL DEFAULT '',
    rank_in_group   INTEGER,
    payload_json    TEXT    NOT NULL DEFAULT '{}',
    UNIQUE(contest_id, stage_idx, entry_id)
);

CREATE TABLE IF NOT EXISTS contest_official_results (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id      INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    entry_id        INTEGER NOT NULL,
    stage_idx       INTEGER NOT NULL DEFAULT 0,  -- 末阶段（全员榜来源）
    rank            INTEGER NOT NULL,            -- 1-based 唯一连续正式名次
    points          REAL    NOT NULL DEFAULT 0,
    bot_id          INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    user_id         INTEGER,
    tiebreaks_json  TEXT    NOT NULL DEFAULT '{}',  -- 各破同分项明细（buchholz/sonneborn/...）
    awarded         TEXT    NOT NULL DEFAULT '',    -- 奖项标注（如 suggested_finalist）
    UNIQUE(contest_id, entry_id)
);

CREATE TABLE IF NOT EXISTS platform_settings (
    key             TEXT    PRIMARY KEY,
    value           TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS contest_templates (
    id              TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL,
    game_id         TEXT    NOT NULL DEFAULT 'holdem',
    match_config    TEXT    NOT NULL DEFAULT '{}',
    stages_json     TEXT    NOT NULL DEFAULT '[]',
    is_builtin      INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bots_owner ON bots(owner_id);
CREATE INDEX IF NOT EXISTS idx_bot_versions_bot ON bot_versions(bot_id);
-- 每游戏对局表的索引由 db.py _migrate 的 _PER_GAME_INDEX_COLS 循环建（注册表派生，
-- 覆盖第 4 游戏），不在此字面硬编码（避免重复索引 + 加游戏漏建）。
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_email_codes_user ON email_codes(user_id, purpose);
CREATE INDEX IF NOT EXISTS idx_contests_org ON contests(organizer_id);
CREATE INDEX IF NOT EXISTS idx_contest_entries_c ON contest_entries(contest_id);
CREATE INDEX IF NOT EXISTS idx_contest_pairings_c ON contest_pairings(contest_id);
CREATE INDEX IF NOT EXISTS idx_contest_stage_results_c ON contest_stage_results(contest_id);
CREATE INDEX IF NOT EXISTS idx_contest_templates_game ON contest_templates(game_id);
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
TYPE_LADDER = "ladder"  # 闲时自动对局维护天梯榜（系统发起，无 owner）
TYPE_HUMAN = "human"  # 人类 vs bot 对局（人类侧无 bot/binary，不计 Glicko）

# match_rating_settlements 内部迁移哨兵：旧库首次升级时先把既有 completed
# 非赛事对局视为已结算，防启动恢复把历史评分全部重复计算。对局 ID 为时间戳前缀，
# 不会与此前缀冲突；哨兵与回填在同一 Store 初始化事务提交。
MATCH_RATING_SETTLEMENTS_MIGRATION_SENTINEL = "__migration__:rating_settlements:v1"

# 比赛状态
CONTEST_DRAFT = "draft"
CONTEST_OPEN = "open"
CONTEST_PUBLISHED = "published"  # 排期已发布、等待开赛（报名截止→出排期→到点开打的两阶段中间态）
CONTEST_RUNNING = "running"
CONTEST_REST = "rest"
CONTEST_FINISHED = "finished"
CONTEST_CANCELLED = "cancelled"

# 赛事时间调度器（后台周期扫描 *_at 字段，到点自动推进阶段）
SETTING_CONTEST_SCHEDULER_ENABLED = "contest_scheduler_enabled"
SETTING_CONTEST_SCHEDULER_INTERVAL_SEC = "contest_scheduler_interval_sec"

# 已注册对战引擎（未注册则 contest start / challenge 拒绝）
# schema.py 是无 import 的纯常量模块（为破循环依赖不能从 registry 派生），
# 此字面量是 registry 的镜像——由 games/__init__.py 启动断言 + test_schema_frozensets_match_registry 守护不漂移。
REGISTERED_ENGINES = frozenset({"holdem", "gomoku", "pencil"})  # allow-game-fallback

# 合法 game_id（与 REGISTERED_ENGINES 镜像，守护测试白名单）
VALID_GAME_IDS = frozenset({"holdem", "gomoku", "pencil"})  # allow-game-fallback

# ── Botzone 运行模式（上传时标明，runner 据此选传输路径）──────────────────
# traditional: 每回合发完整历史信封（Bot 自重放）；longrunning: 首回合完整 + 握手后单 request。
RUNTIME_TRADITIONAL = "traditional"
RUNTIME_LONGRUNNING = "longrunning"
VALID_RUNTIME_MODES = frozenset({RUNTIME_TRADITIONAL, RUNTIME_LONGRUNNING})
DEFAULT_RUNTIME_MODE = RUNTIME_TRADITIONAL  # 平台默认传统（每回合完整历史，便于调试）

# ── 经验/等级体系（对标 Botzone 的 level + 活跃度 gating）───────────────
# 经验奖励：各类活动获得的经验
XP_MATCH_PARTICIPATE = 10   # 参与一场对局（任意类型）
XP_MATCH_WIN = 15           # 对局胜利额外加成
XP_CONTEST_PARTICIPATE = 50 # 赛事报名
XP_COMMENT = 2              # 发表评论（活跃度）
XP_FOLLOWED = 3             # 被关注（活跃度）

# 等级阈值：升到 level N 所需累计经验（level 0 = 0xp；level 1 = 100xp；...）
# 采用递增曲线：level_n = 100 * n * (n+1) / 2
def xp_for_level(level: int) -> int:
    """升到指定 level 所需的累计 xp。"""
    if level <= 0:
        return 0
    return 100 * level * (level + 1) // 2


def level_for_xp(xp: int) -> int:
    """根据累计 xp 推导当前 level。"""
    lvl = 0
    while xp_for_level(lvl + 1) <= xp:
        lvl += 1
        if lvl > 1000:  # 安全上限
            break
    return lvl


# 功能 gating 最低等级（对标 Botzone「等级 1 以上可用某功能」）

# 站点配置键（platform_settings）
SETTING_SITE_NAME = "site_name"
SETTING_SITE_LOGO = "site_logo"
SETTING_SITE_ANNOUNCEMENT = "site_announcement"
SETTING_SITE_ABOUT = "site_about"

# platform_settings keys
SETTING_ACTION_TIMEOUT = "action_timeout_sec"
SETTING_MAX_CONCURRENT = "max_concurrent_matches"
SETTING_BOT_CPUS = "bot_cpus"
SETTING_BOT_MEMORY = "bot_memory_mb"
SETTING_CONTEST_REST = "contest_default_rest_minutes"
SETTING_CONTEST_TEMPLATES = "contest_templates"
SETTING_FULL_RR_MAX_N = "full_rr_max_n"

# 裁判规则参数（admin 可调，热生效：下局即用新值；NULL/缺失则用引擎常量兜底）
SETTING_JUDGE_GOMOKU_SIZE = "judge_gomoku_board_size"       # 五子棋棋盘边长，默认 15
SETTING_JUDGE_HOLDEM_STACK = "judge_holdem_starting_stack"  # 德州起始筹码，默认 20000
SETTING_JUDGE_HOLDEM_SB = "judge_holdem_sb"                 # 德州小盲注，默认 50
SETTING_JUDGE_HOLDEM_BB = "judge_holdem_bb"                 # 德州大盲注，默认 100
SETTING_JUDGE_HOLDEM_HANDS = "judge_holdem_default_hands"   # 德州挑战默认手数，默认 70

# 闲时自动对局（维护天梯榜）
SETTING_AUTO_MATCH_ENABLED = "auto_match_enabled"          # "1"|"0"
SETTING_AUTO_MATCH_INTERVAL_SEC = "auto_match_interval_sec"  # 轮询间隔
SETTING_AUTO_MATCH_MIN_IDLE_SEC = "auto_match_min_idle_sec"  # 连续空闲 N 秒才触发
SETTING_AUTO_MATCH_BOT_COOLDOWN = "auto_match_bot_cooldown"  # 同 Bot 两场间隔下限(秒)
SETTING_AUTO_MATCH_STALE_SEC = "auto_match_stale_sec"      # last_played_at 超此视为陈旧（0=不限）
SETTING_AUTO_MATCH_RESERVE_SLOTS = "auto_match_reserve_slots"  # 为用户挑战预留并发槽
SETTING_AUTO_MATCH_PLACEMENT_GAMES = "auto_match_placement_games"  # 新 bot 定级赛场次（前N场优先）
SETTING_AUTO_MATCH_MAX_PER_ROUND = "auto_match_max_per_round"  # 每轮最多补几场
SETTING_AUTO_MATCH_DAILY_CAP = "auto_match_daily_cap"      # 每日后台对局总量上限

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
