"""botzone-platform SQLite schema.

二进制 Bot 竞赛平台数据模型：用户 / Bot 版本 / 对局 / 评分 / 比赛 / 认证邮件。
"""
from __future__ import annotations

# ── Botzone 运行模式（上传时标明，runner 据此选传输路径）──────────────────
# traditional: 每回合发完整历史信封；longrunning: 首回合完整 + 精确握手后单 request。
# 定义在 SCHEMA 之前，使 Python 默认值、fresh SQL schema 与迁移共同引用同一常量。
RUNTIME_TRADITIONAL = "traditional"
RUNTIME_LONGRUNNING = "longrunning"
VALID_RUNTIME_MODES = frozenset({RUNTIME_TRADITIONAL, RUNTIME_LONGRUNNING})
DEFAULT_RUNTIME_MODE = RUNTIME_TRADITIONAL

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
    os              TEXT    NOT NULL DEFAULT 'linux',
    arch            TEXT    NOT NULL DEFAULT 'amd64',
    format          TEXT    NOT NULL DEFAULT 'elf',
    binary_path     TEXT    NOT NULL DEFAULT '',
    current_version INTEGER NOT NULL DEFAULT 0,
    is_active       INTEGER NOT NULL DEFAULT 1,
    is_builtin      INTEGER NOT NULL DEFAULT 0,
    game_id         TEXT    NOT NULL,
    runtime_mode    TEXT    NOT NULL DEFAULT '__DEFAULT_RUNTIME_MODE__',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(owner_id, name),
    CONSTRAINT chk_bot_os CHECK (os = 'linux'),
    CONSTRAINT chk_bot_arch CHECK (arch = 'amd64'),
    CONSTRAINT chk_format CHECK (format = 'elf'),
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
    os              TEXT    NOT NULL DEFAULT 'linux',
    arch            TEXT    NOT NULL DEFAULT 'amd64',
    format          TEXT    NOT NULL DEFAULT 'elf',
    runtime_mode    TEXT    NOT NULL DEFAULT '__DEFAULT_RUNTIME_MODE__',
    uploaded_at     TEXT    NOT NULL,
    UNIQUE(bot_id, version),
    CONSTRAINT chk_bot_version_os CHECK (os = 'linux'),
    CONSTRAINT chk_bot_version_arch CHECK (arch = 'amd64'),
    CONSTRAINT chk_bot_version_format CHECK (format = 'elf')
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
    created_at              TEXT    NOT NULL,
    game_id                 TEXT    NOT NULL,
    stages_json             TEXT    NOT NULL DEFAULT '[]',
    current_stage_idx       INTEGER NOT NULL DEFAULT 0,
    template_id             TEXT    NOT NULL DEFAULT 'holdem_swiss_ko',
    rest_ends_at            TEXT,
    phase                   TEXT    NOT NULL DEFAULT 'standalone',  -- P2: preliminary/final/standalone
    source_contest_id       INTEGER,  -- P2: 软链（预赛→决赛导航，不复制 entry）
    official_results_ready  INTEGER NOT NULL DEFAULT 0,  -- P2: 全员正式名次是否已落库
    require_real_name       INTEGER NOT NULL DEFAULT 0,  -- 报名是否要求实名
    showcase_key            TEXT,  -- 非空=长期只读的合成演示快照（由专用 seed 管理）
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
    reason          TEXT    NOT NULL DEFAULT '',
    match_type      TEXT    NOT NULL DEFAULT 'challenge',
    status          TEXT    NOT NULL DEFAULT 'pending',
    game_id         TEXT    NOT NULL,
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
    reason          TEXT    NOT NULL DEFAULT '',
    match_type      TEXT    NOT NULL DEFAULT 'challenge',
    status          TEXT    NOT NULL DEFAULT 'pending',
    game_id         TEXT    NOT NULL,
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
    reason          TEXT    NOT NULL DEFAULT '',
    match_type      TEXT    NOT NULL DEFAULT 'challenge',
    status          TEXT    NOT NULL DEFAULT 'pending',
    game_id         TEXT    NOT NULL,
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
    updated_at      TEXT    NOT NULL
);

-- 非赛事 Bot 对局的全局评分结算凭据。match completed 与评分写入分属两个
-- 事务；本表的 claim 与 ratings/history/pair_stats 同事务，保证重试恰好一次。
-- matches 已按游戏分表，无法声明单一物理 FK；删除对局时由 Store.delete_match 清理。
CREATE TABLE IF NOT EXISTS match_rating_settlements (
    match_id        TEXT    PRIMARY KEY,
    settled_at      TEXT    NOT NULL,
    settled_order   INTEGER
);

-- 每场对局在创建/首次 v2 迁移时冻结的评分资格。它是后续全量重建排行榜的
-- 稳定输入，不依赖 Bot 以后软删/硬删；matches 按游戏分表，故 match_id 由 Store
-- 维护逻辑引用。迁移只分类，不自动重放或改写既有 ratings/history/settlements。
CREATE TABLE IF NOT EXISTS match_rating_policies (
    match_id        TEXT    PRIMARY KEY,
    game_id         TEXT    NOT NULL,
    bot_a_id        INTEGER,
    bot_b_id        INTEGER,
    settled_order   INTEGER,
    rated           INTEGER NOT NULL CHECK (rated IN (0,1)),
    rating_reason   TEXT    NOT NULL,
    source          TEXT    NOT NULL,
    classified_at   TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_match_rating_policies_reason
    ON match_rating_policies(source,rating_reason,match_id);

-- 排行榜投影是否已经按当前评分资格真值完整重建。升级只负责识别旧污染，
-- 不会擅自重放历史；维护重建必须在同一事务刷新四类投影后再把此哨兵推进到
-- owner-neutral-v2，并记录它覆盖到的 settlement 序号。
CREATE TABLE IF NOT EXISTS rating_projection_state (
    singleton                   INTEGER PRIMARY KEY CHECK (singleton=1),
    policy_version              TEXT    NOT NULL,
    rebuilt_at                  TEXT,
    source_settlement_count     INTEGER NOT NULL DEFAULT 0 CHECK (source_settlement_count>=0),
    source_last_settled_order   INTEGER NOT NULL DEFAULT 0 CHECK (source_last_settled_order>=0),
    source_digest               TEXT    NOT NULL DEFAULT '',
    projection_digest           TEXT    NOT NULL DEFAULT '',
    plan_digest                 TEXT    NOT NULL DEFAULT ''
);
INSERT OR IGNORE INTO rating_projection_state(
    singleton,policy_version,rebuilt_at,source_settlement_count,
    source_last_settled_order,source_digest,projection_digest,plan_digest
) VALUES(1,'legacy-unverified',NULL,0,0,'','','');

-- completed 事务先冻结全局结算序号；实际评分事务随后用同一序号写 settlement。
-- 这样崩溃恢复不必再猜 created_at/ended_at 顺序。
CREATE TABLE IF NOT EXISTS rating_settlement_sequence (
    singleton       INTEGER PRIMARY KEY CHECK (singleton=1),
    next_order      INTEGER NOT NULL CHECK (next_order>=1)
);
INSERT OR IGNORE INTO rating_settlement_sequence(singleton,next_order) VALUES(1,1);

-- 系统自动排位的永久选择审计。活跃队列终态后会删除，但选择时的游标、lane、
-- owner/Bot/配对服务计数、Rating 差、先后手债务和冻结版本必须长期保留，才能
-- 复核公平策略。Bot/用户/版本 ID 故意是审计快照而非 FK，实体硬删后证据仍在。
CREATE TABLE IF NOT EXISTS auto_match_decisions (
    id                          INTEGER PRIMARY KEY AUTOINCREMENT,
    policy_version              TEXT    NOT NULL,
    state_revision              INTEGER NOT NULL,
    cursor_game_idx             INTEGER NOT NULL,
    requested_lane              TEXT    NOT NULL,
    actual_lane                 TEXT    NOT NULL,
    fallback_reason             TEXT    NOT NULL DEFAULT '',
    game_id                     TEXT    NOT NULL,
    bot_a_id                    INTEGER NOT NULL,
    bot_b_id                    INTEGER NOT NULL,
    owner_a_id                  INTEGER NOT NULL,
    owner_b_id                  INTEGER NOT NULL,
    bot_a_version_id            INTEGER NOT NULL,
    bot_b_version_id            INTEGER NOT NULL,
    owner_a_service_before      INTEGER NOT NULL,
    owner_b_service_before      INTEGER NOT NULL,
    bot_a_service_before        INTEGER NOT NULL,
    bot_b_service_before        INTEGER NOT NULL,
    bot_pair_count_before       INTEGER NOT NULL,
    owner_pair_count_before     INTEGER NOT NULL,
    rating_gap                  REAL    NOT NULL,
    bot_a_seat_debt_before      INTEGER NOT NULL,
    bot_b_seat_debt_before      INTEGER NOT NULL,
    selection_reason            TEXT    NOT NULL,
    lifecycle                   TEXT    NOT NULL DEFAULT 'queued',
    match_id                    TEXT,
    attempt_count               INTEGER NOT NULL DEFAULT 0,
    last_attempt_error          TEXT    NOT NULL DEFAULT '',
    created_at                  TEXT    NOT NULL,
    dispatched_at               TEXT,
    terminal_at                 TEXT,
    terminal_reason             TEXT    NOT NULL DEFAULT '',
    settlement_order            INTEGER,
    claim_dispatcher_token      TEXT,
    claim_dispatcher_epoch      INTEGER CHECK (
        claim_dispatcher_epoch IS NULL OR claim_dispatcher_epoch>0
    ),
    CONSTRAINT chk_auto_decision_bots CHECK (bot_a_id <> bot_b_id),
    CONSTRAINT chk_auto_decision_owners CHECK (owner_a_id <> owner_b_id),
    CONSTRAINT chk_auto_decision_lane CHECK (
        requested_lane IN ('placement','formal') AND
        actual_lane IN ('placement','formal')
    ),
    CONSTRAINT chk_auto_decision_lifecycle CHECK (
        lifecycle IN ('queued','dispatched','completed','aborted','cancelled')
    ),
    CONSTRAINT chk_auto_decision_fence_pair CHECK (
        (claim_dispatcher_token IS NULL) = (claim_dispatcher_epoch IS NULL)
    )
);

-- 活跃公平队列只保留 queued/dispatched 两个生命周期；completed 且评分结算
-- 落稳，或 aborted 终态落稳后即删除。match_id 因 matches 按游戏分表而不声明
-- 物理 FK，由 Store 在同一 BEGIN IMMEDIATE 事务维护。
CREATE TABLE IF NOT EXISTS auto_match_queue (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    decision_id         INTEGER NOT NULL UNIQUE
                            REFERENCES auto_match_decisions(id) ON DELETE RESTRICT,
    game_id             TEXT    NOT NULL,
    bot_a_id            INTEGER NOT NULL REFERENCES bots(id) ON DELETE RESTRICT,
    bot_b_id            INTEGER NOT NULL REFERENCES bots(id) ON DELETE RESTRICT,
    bot_a_version_id    INTEGER NOT NULL REFERENCES bot_versions(id) ON DELETE RESTRICT,
    bot_b_version_id    INTEGER NOT NULL REFERENCES bot_versions(id) ON DELETE RESTRICT,
    status              TEXT    NOT NULL DEFAULT 'queued',
    match_id            TEXT,
    dispatcher_token    TEXT,
    dispatcher_epoch    INTEGER CHECK (
        dispatcher_epoch IS NULL OR dispatcher_epoch>0
    ),
    selection_reason    TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    dispatched_at       TEXT,
    CONSTRAINT chk_auto_queue_bots CHECK (bot_a_id <> bot_b_id),
    CONSTRAINT chk_auto_queue_status CHECK (status IN ('queued','dispatched')),
    CONSTRAINT chk_auto_queue_lifecycle CHECK (
        (status='queued' AND match_id IS NULL AND dispatcher_token IS NULL
                         AND dispatcher_epoch IS NULL AND dispatched_at IS NULL) OR
        (status='dispatched' AND match_id IS NOT NULL AND dispatcher_token IS NOT NULL
                             AND dispatcher_epoch IS NOT NULL AND dispatched_at IS NOT NULL)
    )
);

-- v2 独立单例控制面。它与历史 platform_settings 的同名键无关，首次升级始终
-- 默认开启，之后只有管理员严格布尔 API 可改 enabled。
CREATE TABLE IF NOT EXISTS auto_match_control (
    singleton       INTEGER PRIMARY KEY CHECK (singleton=1),
    enabled         INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0,1)),
    updated_at      TEXT    NOT NULL
);
INSERT OR IGNORE INTO auto_match_control(singleton,enabled,updated_at)
VALUES(1,1,CURRENT_TIMESTAMP);

-- 调度 owner lease 防多个服务进程互相派发/恢复。lease 只是内部协调状态，不是
-- 管理员参数；过期后新进程可接管，未过期时其他进程只读队列。
CREATE TABLE IF NOT EXISTS auto_match_dispatcher (
    singleton       INTEGER PRIMARY KEY CHECK (singleton=1),
    owner_token     TEXT,
    lease_epoch     INTEGER NOT NULL DEFAULT 0 CHECK (lease_epoch>=0),
    lease_until     TEXT,
    heartbeat_at    TEXT
);
INSERT OR IGNORE INTO auto_match_dispatcher(singleton,lease_epoch) VALUES(1,0);

-- 持久公平游标与平台故障 circuit breaker。next_lane: 0=placement, 1=formal。
CREATE TABLE IF NOT EXISTS auto_match_fair_state (
    singleton           INTEGER PRIMARY KEY CHECK (singleton=1),
    next_game_idx       INTEGER NOT NULL DEFAULT 0 CHECK (next_game_idx>=0),
    next_lane           INTEGER NOT NULL DEFAULT 0 CHECK (next_lane IN (0,1)),
    revision            INTEGER NOT NULL DEFAULT 0 CHECK (revision>=0),
    bootstrap_version   INTEGER NOT NULL DEFAULT 0 CHECK (bootstrap_version>=0),
    platform_failures   INTEGER NOT NULL DEFAULT 0 CHECK (platform_failures>=0),
    not_before          TEXT,
    updated_at          TEXT    NOT NULL
);
INSERT OR IGNORE INTO auto_match_fair_state(
    singleton,next_game_idx,next_lane,revision,bootstrap_version,
    platform_failures,not_before,updated_at
) VALUES(1,0,0,0,0,0,NULL,CURRENT_TIMESTAMP);

-- 公平选择只读这些 auto 专属计数，绝不借用可被前台挑战影响的 ratings/
-- pair_stats。所有计数按游戏隔离；owner 全局活跃唯一由 queue trigger 保证。
CREATE TABLE IF NOT EXISTS auto_match_owner_service (
    owner_id               INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    game_id                TEXT    NOT NULL,
    served_count           INTEGER NOT NULL DEFAULT 0 CHECK (served_count>=0),
    last_served_revision   INTEGER NOT NULL DEFAULT 0 CHECK (last_served_revision>=0),
    last_served_at         TEXT,
    PRIMARY KEY(owner_id,game_id)
);
CREATE TABLE IF NOT EXISTS auto_match_bot_service (
    bot_id                 INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    game_id                TEXT    NOT NULL,
    served_count           INTEGER NOT NULL DEFAULT 0 CHECK (served_count>=0),
    seat_a_count           INTEGER NOT NULL DEFAULT 0 CHECK (seat_a_count>=0),
    seat_b_count           INTEGER NOT NULL DEFAULT 0 CHECK (seat_b_count>=0),
    last_served_revision   INTEGER NOT NULL DEFAULT 0 CHECK (last_served_revision>=0),
    last_served_at         TEXT,
    PRIMARY KEY(bot_id,game_id)
);
CREATE TABLE IF NOT EXISTS auto_match_bot_pair_service (
    game_id                TEXT    NOT NULL,
    bot_lo_id              INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    bot_hi_id              INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    served_count           INTEGER NOT NULL DEFAULT 0 CHECK (served_count>=0),
    last_served_at         TEXT,
    PRIMARY KEY(game_id,bot_lo_id,bot_hi_id),
    CONSTRAINT chk_auto_bot_pair_order CHECK (bot_lo_id < bot_hi_id)
);
CREATE TABLE IF NOT EXISTS auto_match_owner_pair_service (
    game_id                TEXT    NOT NULL,
    owner_lo_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    owner_hi_id            INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    served_count           INTEGER NOT NULL DEFAULT 0 CHECK (served_count>=0),
    last_served_at         TEXT,
    PRIMARY KEY(game_id,owner_lo_id,owner_hi_id),
    CONSTRAINT chk_auto_owner_pair_order CHECK (owner_lo_id < owner_hi_id)
);

CREATE TABLE IF NOT EXISTS ratings (
    bot_id          INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    game_id         TEXT    NOT NULL,
    rating          REAL    NOT NULL DEFAULT 1500.0,
    rd              REAL    NOT NULL DEFAULT 350.0,
    vol             REAL    NOT NULL DEFAULT 0.06,
    wins            INTEGER NOT NULL DEFAULT 0,
    losses          INTEGER NOT NULL DEFAULT 0,
    draws           INTEGER NOT NULL DEFAULT 0,
    delta_total     INTEGER NOT NULL DEFAULT 0,
    matches_played  INTEGER NOT NULL DEFAULT 0,
    last_played_at  TEXT,
    PRIMARY KEY (bot_id, game_id)
);

CREATE TABLE IF NOT EXISTS pair_stats (
    bot_a_id        INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    bot_b_id        INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
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
    game_id         TEXT    NOT NULL,
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
    delta_total     INTEGER NOT NULL DEFAULT 0,
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
    -- 历史保留表；现行模板来自 games 注册表，运行路径不读取或写入本表。
    id              TEXT    PRIMARY KEY,
    name            TEXT    NOT NULL,
    game_id         TEXT    NOT NULL,
    match_config    TEXT    NOT NULL DEFAULT '{}',
    stages_json     TEXT    NOT NULL DEFAULT '[]',
    is_builtin      INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bots_owner ON bots(owner_id);
CREATE INDEX IF NOT EXISTS idx_bot_versions_bot ON bot_versions(bot_id);
CREATE INDEX IF NOT EXISTS idx_auto_match_queue_order
    ON auto_match_queue(status, id);
CREATE INDEX IF NOT EXISTS idx_auto_match_queue_game
    ON auto_match_queue(game_id, status, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_match_queue_match
    ON auto_match_queue(match_id) WHERE match_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_match_decisions_match
    ON auto_match_decisions(match_id) WHERE match_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_auto_match_decisions_created
    ON auto_match_decisions(id DESC);
-- 整个平台最多一场系统自动排位处于 dispatched；多进程共同受 SQLite 唯一索引约束。
CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_match_queue_one_dispatched
    ON auto_match_queue(status) WHERE status='dispatched';

-- 一个 Bot 在 queued/dispatched 活跃队列中只能出现一次。跨列唯一性无法用普通
-- UNIQUE 表达，写事务内的触发器在 SQLite 串行写锁下提供数据库级硬约束。
CREATE TRIGGER IF NOT EXISTS trg_auto_match_queue_unique_bot_insert
BEFORE INSERT ON auto_match_queue
WHEN EXISTS (
    SELECT 1 FROM auto_match_queue q
    WHERE NEW.bot_a_id IN (q.bot_a_id, q.bot_b_id)
       OR NEW.bot_b_id IN (q.bot_a_id, q.bot_b_id)
)
BEGIN
    SELECT RAISE(ABORT, 'auto-match bot already queued');
END;

CREATE TRIGGER IF NOT EXISTS trg_auto_match_queue_unique_bot_update
BEFORE UPDATE OF bot_a_id,bot_b_id ON auto_match_queue
WHEN EXISTS (
    SELECT 1 FROM auto_match_queue q
    WHERE q.id<>OLD.id AND (
        NEW.bot_a_id IN (q.bot_a_id, q.bot_b_id)
        OR NEW.bot_b_id IN (q.bot_a_id, q.bot_b_id)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'auto-match bot already queued');
END;

CREATE TRIGGER IF NOT EXISTS trg_auto_match_queue_unique_owner_insert
BEFORE INSERT ON auto_match_queue
WHEN EXISTS (
    SELECT 1 FROM auto_match_queue q
    JOIN bots qa ON qa.id=q.bot_a_id
    JOIN bots qb ON qb.id=q.bot_b_id
    JOIN bots na ON na.id=NEW.bot_a_id
    JOIN bots nb ON nb.id=NEW.bot_b_id
    WHERE na.owner_id IN (qa.owner_id,qb.owner_id)
       OR nb.owner_id IN (qa.owner_id,qb.owner_id)
)
BEGIN
    SELECT RAISE(ABORT, 'auto-match owner already queued');
END;

CREATE TRIGGER IF NOT EXISTS trg_auto_match_queue_unique_owner_update
BEFORE UPDATE OF bot_a_id,bot_b_id ON auto_match_queue
WHEN EXISTS (
    SELECT 1 FROM auto_match_queue q
    JOIN bots qa ON qa.id=q.bot_a_id
    JOIN bots qb ON qb.id=q.bot_b_id
    JOIN bots na ON na.id=NEW.bot_a_id
    JOIN bots nb ON nb.id=NEW.bot_b_id
    WHERE q.id<>OLD.id AND (
        na.owner_id IN (qa.owner_id,qb.owner_id)
        OR nb.owner_id IN (qa.owner_id,qb.owner_id)
    )
)
BEGIN
    SELECT RAISE(ABORT, 'auto-match owner already queued');
END;

CREATE TRIGGER IF NOT EXISTS trg_auto_match_queue_identity_insert
BEFORE INSERT ON auto_match_queue
WHEN NOT EXISTS (
    SELECT 1 FROM bots a JOIN users ua ON ua.id=a.owner_id JOIN bots b
      ON a.id=NEW.bot_a_id AND b.id=NEW.bot_b_id
    JOIN users ub ON ub.id=b.owner_id
    JOIN bot_versions va ON va.id=NEW.bot_a_version_id AND va.bot_id=a.id
    JOIN bot_versions vb ON vb.id=NEW.bot_b_version_id AND vb.bot_id=b.id
    JOIN auto_match_decisions d ON d.id=NEW.decision_id
    WHERE a.game_id=NEW.game_id AND b.game_id=NEW.game_id
      AND a.owner_id<>b.owner_id
      AND a.is_active=1 AND b.is_active=1
      AND ua.is_active=1 AND ub.is_active=1
      AND a.is_builtin=0 AND b.is_builtin=0
      AND a.format='elf' AND b.format='elf'
      AND a.os='linux' AND b.os='linux'
      AND a.arch='amd64' AND b.arch='amd64'
      AND va.format='elf' AND vb.format='elf'
      AND va.os='linux' AND vb.os='linux'
      AND va.arch='amd64' AND vb.arch='amd64'
      AND va.binary_path<>'' AND vb.binary_path<>''
      AND d.game_id=NEW.game_id
      AND d.bot_a_id=NEW.bot_a_id AND d.bot_b_id=NEW.bot_b_id
      AND d.bot_a_version_id=NEW.bot_a_version_id
      AND d.bot_b_version_id=NEW.bot_b_version_id
      AND d.lifecycle='queued'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid auto-match queue identity');
END;

CREATE TRIGGER IF NOT EXISTS trg_auto_match_queue_identity_update
BEFORE UPDATE OF decision_id,game_id,bot_a_id,bot_b_id,bot_a_version_id,bot_b_version_id
ON auto_match_queue
WHEN NOT EXISTS (
    SELECT 1 FROM bots a JOIN users ua ON ua.id=a.owner_id JOIN bots b
      ON a.id=NEW.bot_a_id AND b.id=NEW.bot_b_id
    JOIN users ub ON ub.id=b.owner_id
    JOIN bot_versions va ON va.id=NEW.bot_a_version_id AND va.bot_id=a.id
    JOIN bot_versions vb ON vb.id=NEW.bot_b_version_id AND vb.bot_id=b.id
    JOIN auto_match_decisions d ON d.id=NEW.decision_id
    WHERE a.game_id=NEW.game_id AND b.game_id=NEW.game_id
      AND a.owner_id<>b.owner_id
      AND a.is_active=1 AND b.is_active=1
      AND ua.is_active=1 AND ub.is_active=1
      AND a.is_builtin=0 AND b.is_builtin=0
      AND a.format='elf' AND b.format='elf'
      AND a.os='linux' AND b.os='linux'
      AND a.arch='amd64' AND b.arch='amd64'
      AND va.format='elf' AND vb.format='elf'
      AND va.os='linux' AND vb.os='linux'
      AND va.arch='amd64' AND vb.arch='amd64'
      AND va.binary_path<>'' AND vb.binary_path<>''
      AND d.game_id=NEW.game_id
      AND d.bot_a_id=NEW.bot_a_id AND d.bot_b_id=NEW.bot_b_id
      AND d.bot_a_version_id=NEW.bot_a_version_id
      AND d.bot_b_version_id=NEW.bot_b_version_id
      AND d.lifecycle='queued'
)
BEGIN
    SELECT RAISE(ABORT, 'invalid auto-match queue identity');
END;
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

# SQLite identifiers cannot bind a DEFAULT value as a query parameter. Replacing
# this private marker keeps the SQL text readable while preventing a second,
# drifting runtime-mode default literal.
SCHEMA = SCHEMA.replace("__DEFAULT_RUNTIME_MODE__", DEFAULT_RUNTIME_MODE)

# 角色
ROLE_USER = "user"
ROLE_ORGANIZER = "organizer"
ROLE_ADMIN = "admin"

# 对局状态
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_ABORTED = "aborted"

# 对外对局技术故障事件（新写 replay / 实时 SSE / 公开读取唯一命名）
TECHNICAL_INCIDENT_EVENT = "technical_incident"
BOT_CAPACITY_EXHAUSTED_REASON = "bot_capacity_exhausted"
TECHNICAL_INCIDENT_MESSAGES = {
    "invalid_json": "Bot 输出不是合法 JSON",
    "invalid_envelope": "Bot 响应信封必须是 JSON 对象",
    "missing_response": "Bot 响应缺少必填 response 字段",
    "invalid_response": "Bot response 字段不符合本游戏协议",
    "missing_keep_running": "LongRunning Bot 未输出 KEEP_RUNNING 握手",
    "invalid_keep_running": "LongRunning Bot 的 KEEP_RUNNING 握手不正确",
    "decision_timeout": "Bot 未在决策时限内输出完整响应行",
}

# 公开 completed/match_end 唯一允许的稳定裁决码。游戏裁判与平台技术判负
# 只能从这里选择；未知历史文本在读取/迁移边界统一为 completed。
PUBLIC_MATCH_COMPLETED_REASONS = frozenset(
    {
        "bot_deleted",
        "completed",
        "contest_bot_unavailable",
        "crash",
        "draw",
        "error",
        "five",
        "illegal",
        "majority",
        "protocol_error",
        "score",
        "technical_loss",
        "timeout",
    }
)

# 公开 match ``error`` 终局唯一允许的稳定原因码。任意内部异常文本、旧自定义
# 管理员 reason 或未知值在公共边界一律归一为 platform_error；诊断详情只进日志。
PUBLIC_MATCH_ERROR_REASONS = frozenset(
    {
        "admin_aborted",
        "bot_crashed",
        "contest_bot_unavailable",
        "contest_both_bots_unavailable",
        "contest_ended_pending_orphan",
        "human_inactive",
        "invalid_game_id",
        "invalid_match_config",
        "orphan_after_restart",
        "orphan_pending_after_restart",
        "orphan_pending_no_contest",
        "platform_error",
        "version_unavailable",
    }
)
PUBLIC_MATCH_ERROR_FALLBACK = "platform_error"

# 对局类型
TYPE_CHALLENGE = "challenge"
TYPE_TABLE = "table"
TYPE_CONTEST = "contest"
TYPE_LADDER = "ladder"  # 持续自动排位维护天梯榜（系统发起，无 owner）
TYPE_HUMAN = "human"  # 人类 vs bot 对局（人类侧无 bot/binary，不计 Glicko）

# 社交目标类型。comments / likes 是多态引用，SQLite 无法为 target_id 声明
# 跨表外键，因此合法类型集中在这里，并由 Store 在同一写事务内校验目标存在。
COMMENT_TARGET_TYPES = frozenset({"match", "bot"})
LIKE_TARGET_TYPES = frozenset({"match", "bot", "comment"})

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

# 以下 runtime 键只标识旧库历史记录；现行值来自 runtime/config.py。
SETTING_CONTEST_SCHEDULER_ENABLED = "contest_scheduler_enabled"
SETTING_CONTEST_SCHEDULER_INTERVAL_SEC = "contest_scheduler_interval_sec"

# 已注册对战引擎（未注册则 contest start / challenge 拒绝）
# schema.py 是无 import 的纯常量模块（为破循环依赖不能从 registry 派生），
# 此字面量是 registry 的镜像——由 games/__init__.py 启动断言 + test_schema_frozensets_match_registry 守护不漂移。
REGISTERED_ENGINES = frozenset({"holdem", "gomoku", "pencil"})  # allow-game-registry-definition

# 合法 game_id（与 REGISTERED_ENGINES 镜像，守护测试白名单）
VALID_GAME_IDS = frozenset({"holdem", "gomoku", "pencil"})  # allow-game-registry-definition

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

# 历史 platform_settings runtime keys（保留名称供审计/迁移测试，运行路径不消费）
SETTING_ACTION_TIMEOUT = "action_timeout_sec"
SETTING_MAX_CONCURRENT = "max_concurrent_matches"
SETTING_BOT_CPUS = "bot_cpus"
SETTING_BOT_MEMORY = "bot_memory_mb"
SETTING_CONTEST_REST = "contest_default_rest_minutes"
SETTING_CONTEST_TEMPLATES = "contest_templates"
SETTING_FULL_RR_MAX_N = "full_rr_max_n"


# 唯一可执行目标。PE/Mach-O/脚本及其他 ELF 架构仅可作为历史元数据读取，
# 不属于现行 schema 的可写值，也绝不能进入 runner。
FMT_ELF = "elf"
SUPPORTED_BINARY_FORMAT = FMT_ELF
SUPPORTED_BINARY_OS = "linux"
SUPPORTED_BINARY_ARCH = "amd64"
SUPPORTED_BINARY_ERROR = "仅支持 Linux x86_64 ELF64（小端）"


def is_supported_binary_metadata(fmt: str, os_: str, arch: str) -> bool:
    """Whether persisted metadata names the platform's sole runnable target."""
    return (
        fmt == SUPPORTED_BINARY_FORMAT
        and os_ == SUPPORTED_BINARY_OS
        and arch == SUPPORTED_BINARY_ARCH
    )


def require_supported_binary_metadata(fmt: str, os_: str, arch: str) -> None:
    """Reject new/executable references outside the platform's sole target."""
    if not is_supported_binary_metadata(fmt, os_, arch):
        raise ValueError(SUPPORTED_BINARY_ERROR)

# 邮件模板
TPL_VERIFY_EMAIL = "verify_email"
TPL_RESET_PASSWORD = "reset_password"
TPL_WELCOME = "welcome"

# 邮箱验证码用途
CODE_VERIFY = "verify"
CODE_RESET = "reset"
