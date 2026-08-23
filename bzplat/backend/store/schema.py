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

# 游戏规则 / Bot 协议 / 评分池是三个独立的持久化契约。通用层
# 只查表，不按 game_id 写分支；一次大版规则切换由 Store 的离线
# cutover API 原子推进 active contract。
GOMOKU_LEGACY_RULESET = "gomoku_freestyle_v1"
GOMOKU_PREVIOUS_RULESET = "gomoku_ccgc_2013_v1"
GOMOKU_CURRENT_RULESET = "gomoku_ccgc_2013_five_move_two_v2"
GOMOKU_LEGACY_PROTOCOL = "gomoku_xy_v1"
GOMOKU_CURRENT_PROTOCOL = "gomoku_action_v2"
GOMOKU_LEGACY_RATING_POOL = "gomoku_freestyle_rating_v1"
GOMOKU_PREVIOUS_RATING_POOL = "gomoku_ccgc_2013_rating_v1"
GOMOKU_CURRENT_RATING_POOL = "gomoku_ccgc_2013_five_move_two_rating_v2"

GAME_RULE_CONTRACTS = {
    "holdem": {
        "ruleset_version": "holdem_hu_nlhe_v1",
        "protocol_version": "holdem_action_v1",
        "rating_pool_id": "holdem_rating_v1",
    },
    "gomoku": {
        "ruleset_version": GOMOKU_CURRENT_RULESET,
        "protocol_version": GOMOKU_CURRENT_PROTOCOL,
        "rating_pool_id": GOMOKU_CURRENT_RATING_POOL,
    },
    "pencil": {
        "ruleset_version": "pencil_ccgc_v1",
        "protocol_version": "pencil_xy_v1",
        "rating_pool_id": "pencil_rating_v1",
    },
}

GAME_LEGACY_RULE_CONTRACTS = {
    **GAME_RULE_CONTRACTS,
    "gomoku": {
        "ruleset_version": GOMOKU_LEGACY_RULESET,
        "protocol_version": GOMOKU_LEGACY_PROTOCOL,
        "rating_pool_id": GOMOKU_LEGACY_RATING_POOL,
    },
}


def game_rule_contract(game_id: str, *, legacy: bool = False) -> dict[str, str]:
    """返回一份可持久化的游戏契约副本；未知游戏 fail closed。"""
    source = GAME_LEGACY_RULE_CONTRACTS if legacy else GAME_RULE_CONTRACTS
    try:
        return dict(source[game_id])
    except KeyError as exc:
        raise ValueError(f"未声明规则契约的游戏: {game_id!r}") from exc

# 私有 Bot debug 持久化硬顶。schema CHECK、Store 防御性校验与上层
# 内存收集器共用，避免三处同义数字漂移。
MATCH_DEBUG_MAX_ENTRY_BYTES = 4 * 1024
MATCH_DEBUG_MAX_ENTRIES_PER_SEAT = 512
MATCH_DEBUG_MAX_BYTES_PER_SEAT = 128 * 1024
MATCH_DEBUG_MAX_ENTRIES_PER_MATCH = 1024
MATCH_DEBUG_MAX_BYTES_PER_MATCH = 256 * 1024

# 全局执行请求状态机。queued 之外的 starting/running/settling 都占用一场
# match slot；直到业务终态已落盘且 sandbox label 清零，才允许从 settling
# 进入终态并释放容量。评分未结算由 per-Bot rated-overlap 门禁隔离，不占用
# 无关任务的全局容量。
EXECUTION_SOURCE_MANUAL = "manual"
EXECUTION_SOURCE_HUMAN = "human"
EXECUTION_SOURCE_CONTEST = "contest"
EXECUTION_SOURCE_AUTO = "auto"
EXECUTION_SOURCES = frozenset(
    {
        EXECUTION_SOURCE_MANUAL,
        EXECUTION_SOURCE_HUMAN,
        EXECUTION_SOURCE_CONTEST,
        EXECUTION_SOURCE_AUTO,
    }
)

# Botzone 传输模式与“代码在哪里执行”是两个正交契约。环境由任务来源和用户
# 选择在入队时冻结，调用方不能提交任意 CPU/内存值。
EXECUTION_ENV_PLATFORM_LOW = "platform_low"
EXECUTION_ENV_PLATFORM_HIGH = "platform_high"
EXECUTION_ENV_REMOTE_LOCAL = "remote_local"
EXECUTION_ENV_HUMAN = "human"
EXECUTION_ENVIRONMENTS = frozenset(
    {
        EXECUTION_ENV_PLATFORM_LOW,
        EXECUTION_ENV_PLATFORM_HIGH,
        EXECUTION_ENV_REMOTE_LOCAL,
        EXECUTION_ENV_HUMAN,
    }
)
EXECUTION_PROFILE_VERSION = 1

# User-hosted Bot identities and sockets are intentionally small, fixed
# product capacities.  A participant needs two simultaneous connections for a
# local-vs-local practice match, while unbounded dormant identities or sockets
# would turn the referee into a general connection host.
LOCAL_AI_MAX_ACTIVE_AGENTS_PER_OWNER = 8
LOCAL_AI_MAX_ONLINE_PER_OWNER = 4
LOCAL_AI_MAX_ONLINE_GLOBAL = 64

EXECUTION_QUEUED = "queued"
EXECUTION_STARTING = "starting"
EXECUTION_RUNNING = "running"
EXECUTION_SETTLING = "settling"
EXECUTION_COMPLETED = "completed"
EXECUTION_CANCELLED = "cancelled"
EXECUTION_INTERRUPTED = "interrupted"
EXECUTION_ACTIVE_STATES = frozenset(
    {EXECUTION_STARTING, EXECUTION_RUNNING, EXECUTION_SETTLING}
)
EXECUTION_TERMINAL_STATES = frozenset(
    {EXECUTION_COMPLETED, EXECUTION_CANCELLED, EXECUTION_INTERRUPTED}
)

DISPATCHER_STOPPED = "stopped"
DISPATCHER_STARTING = "starting"
DISPATCHER_RUNNING = "running"
DISPATCHER_PAUSED = "paused"
DISPATCHER_STOPPING = "stopping"

# ── 平台通信状态（communications/ 唯一持久化契约）────────────────────
# 站内 conversation/message 是普通平台通信的真相；邮件只是异步 delivery。
# 验证码/重置码属于 transactional delivery，出于安全原因不写普通消息正文。
CONVERSATION_KINDS = frozenset({
    "notification", "support", "bug_report", "broadcast", "auth", "system",
})
CONVERSATION_STATUSES = frozenset({"open", "closed", "archived"})
DELIVERY_CHANNELS = frozenset({"in_app", "email"})
DELIVERY_STATUSES = frozenset({
    "queued", "sending", "sent", "failed", "cancelled",
})
BROADCAST_STATES = frozenset({
    "draft", "scheduled", "running", "completed", "cancelled",
})
BUG_REPORT_STATUSES = frozenset({
    "new", "acknowledged", "needs_info", "in_progress", "resolved",
    "duplicate", "wont_fix",
})

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
    is_ranked       INTEGER NOT NULL DEFAULT 0,
    owner_deleted_at TEXT,
    is_builtin      INTEGER NOT NULL DEFAULT 0,
    game_id         TEXT    NOT NULL,
    runtime_mode    TEXT    NOT NULL DEFAULT '__DEFAULT_RUNTIME_MODE__',
    protocol_version TEXT   NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL,
    UNIQUE(owner_id, name),
    CONSTRAINT chk_bot_os CHECK (os = 'linux'),
    CONSTRAINT chk_bot_arch CHECK (arch = 'amd64'),
    CONSTRAINT chk_format CHECK (format = 'elf'),
    CONSTRAINT chk_bot_ranked CHECK (is_ranked IN (0,1)),
    CONSTRAINT chk_bot_owner_deleted CHECK (
        owner_deleted_at IS NULL OR (is_active=0 AND is_ranked=0)),
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
    protocol_version TEXT   NOT NULL DEFAULT '',
    retired_at      TEXT,
    retirement_reason TEXT  NOT NULL DEFAULT '',
    uploaded_at     TEXT    NOT NULL,
    UNIQUE(bot_id, version),
    CONSTRAINT chk_bot_version_os CHECK (os = 'linux'),
    CONSTRAINT chk_bot_version_arch CHECK (arch = 'amd64'),
    CONSTRAINT chk_bot_version_format CHECK (format = 'elf')
);

-- 用户电脑上的 Bot 连接身份。Bot 行仍是公开名称/所有者/游戏的唯一身份，
-- agent 只声明“这一场由哪台用户电脑回答裁判请求”，绝不伪装成上传版本。
-- 原始连接令牌只在创建/轮换响应中出现一次；数据库永久只保存 SHA-256。
CREATE TABLE IF NOT EXISTS local_ai_agents (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id             TEXT    NOT NULL UNIQUE,
    owner_id              INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bot_id                INTEGER NOT NULL REFERENCES bots(id) ON DELETE CASCADE,
    label                 TEXT    NOT NULL,
    game_id               TEXT    NOT NULL,
    protocol_version      TEXT    NOT NULL DEFAULT '',
    token_hash            TEXT    NOT NULL UNIQUE,
    token_hint            TEXT    NOT NULL,
    status                TEXT    NOT NULL DEFAULT 'active' CHECK (
        status IN ('active','revoked')
    ),
    connection_generation INTEGER NOT NULL DEFAULT 0 CHECK (connection_generation>=0),
    connected_at          TEXT,
    disconnected_at       TEXT,
    last_seen_at          TEXT,
    created_at            TEXT    NOT NULL,
    updated_at            TEXT    NOT NULL,
    UNIQUE(owner_id,label)
);
CREATE INDEX IF NOT EXISTS idx_local_ai_agents_owner
    ON local_ai_agents(owner_id,status,updated_at,id);
CREATE INDEX IF NOT EXISTS idx_local_ai_agents_bot
    ON local_ai_agents(bot_id,status,id);

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
    ruleset_version          TEXT    NOT NULL DEFAULT '',
    protocol_version         TEXT    NOT NULL DEFAULT '',
    rating_pool_id           TEXT    NOT NULL DEFAULT '',
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
    ruleset_version TEXT    NOT NULL DEFAULT '',
    protocol_version TEXT   NOT NULL DEFAULT '',
    rating_pool_id  TEXT    NOT NULL DEFAULT '',
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
    ruleset_version TEXT    NOT NULL DEFAULT '',
    protocol_version TEXT   NOT NULL DEFAULT '',
    rating_pool_id  TEXT    NOT NULL DEFAULT '',
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
    ruleset_version TEXT    NOT NULL DEFAULT '',
    protocol_version TEXT   NOT NULL DEFAULT '',
    rating_pool_id  TEXT    NOT NULL DEFAULT '',
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

-- Bot 顶层 debug sidecar 的私有存储。它不属于公开 replay/result/event 契约，
-- 只在对局终态后由编排器一次性写入。通过 matches_index 的 FK 保证删除任意
-- 游戏的对局都会级联清理；Bot/用户删除只清空快照 bot_id，不产生悬空引用。
CREATE TABLE IF NOT EXISTS match_debug_sessions (
    match_id        TEXT    PRIMARY KEY REFERENCES matches_index(id) ON DELETE CASCADE,
    entry_count     INTEGER NOT NULL DEFAULT 0 CHECK (entry_count BETWEEN 0 AND __MATCH_DEBUG_MAX_ENTRIES__),
    total_bytes     INTEGER NOT NULL DEFAULT 0 CHECK (total_bytes BETWEEN 0 AND __MATCH_DEBUG_MAX_BYTES__),
    dropped_count   INTEGER NOT NULL DEFAULT 0 CHECK (dropped_count >= 0),
    created_at      TEXT    NOT NULL,
    updated_at      TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS match_debug_entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    match_id        TEXT    NOT NULL REFERENCES match_debug_sessions(match_id) ON DELETE CASCADE,
    bot_id          INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    seat            INTEGER NOT NULL CHECK (seat IN (0, 1)),
    turn            INTEGER NOT NULL CHECK (turn >= 1),
    leg             INTEGER NOT NULL DEFAULT -1 CHECK (leg >= -1),
    debug_json      TEXT    NOT NULL CHECK (json_valid(debug_json)),
    size_bytes      INTEGER NOT NULL CHECK (size_bytes BETWEEN 1 AND __MATCH_DEBUG_MAX_ENTRY_BYTES__),
    created_at      TEXT    NOT NULL,
    UNIQUE(match_id, seat, turn, leg)
);
CREATE INDEX IF NOT EXISTS idx_match_debug_entries_order
    ON match_debug_entries(match_id, seat, leg, turn);

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
    rating_pool_id  TEXT    NOT NULL DEFAULT '',
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
-- owner-ranked-bot-v4，并记录它覆盖到的 settlement 序号和可信 mutation 链。
-- v2 没有 mutation lineage，升级后必须离线重建，不能沿用其“已验证”标记。
CREATE TABLE IF NOT EXISTS rating_projection_state (
    singleton                   INTEGER PRIMARY KEY CHECK (singleton=1),
    policy_version              TEXT    NOT NULL,
    rebuilt_at                  TEXT,
    source_settlement_count     INTEGER NOT NULL DEFAULT 0 CHECK (source_settlement_count>=0),
    source_last_settled_order   INTEGER NOT NULL DEFAULT 0 CHECK (source_last_settled_order>=0),
    source_digest               TEXT    NOT NULL DEFAULT '',
    projection_digest           TEXT    NOT NULL DEFAULT '',
    plan_digest                 TEXT    NOT NULL DEFAULT '',
    mutation_revision           INTEGER NOT NULL DEFAULT 0 CHECK (mutation_revision>=0),
    trusted_mutation_revision   INTEGER NOT NULL DEFAULT 0 CHECK (trusted_mutation_revision>=0)
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

-- 全来源执行请求。public_id 是唯一对外标识；版本 ID、内部主键、match_config
-- 与故障详情只供调度器使用，所有 API 必须显式白名单投影。
CREATE TABLE IF NOT EXISTS execution_jobs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id           TEXT    NOT NULL UNIQUE,
    source              TEXT    NOT NULL CHECK (
        source IN ('manual','human','contest','auto')
    ),
    status              TEXT    NOT NULL DEFAULT 'queued' CHECK (
        status IN ('queued','starting','running','settling',
                   'completed','cancelled','interrupted')
    ),
    priority            INTEGER NOT NULL CHECK (priority BETWEEN 0 AND 100),
    owner_user_id       INTEGER,
    game_id             TEXT    NOT NULL,
    ruleset_version     TEXT    NOT NULL DEFAULT '',
    protocol_version    TEXT    NOT NULL DEFAULT '',
    rating_pool_id      TEXT    NOT NULL DEFAULT '',
    match_type          TEXT    NOT NULL CHECK (
        match_type IN ('challenge','table','contest','ladder','human')
    ),
    -- These are immutable audit snapshots, not ownership FKs.  Active-request
    -- deletion guards below protect runnable identities while still allowing
    -- ordinary account/Bot retention policy after the request is terminal.
    bot_a_id            INTEGER NOT NULL,
    bot_b_id            INTEGER NOT NULL,
    bot_a_version_id    INTEGER,
    bot_b_version_id    INTEGER,
    bot_a_environment   TEXT    NOT NULL DEFAULT 'platform_low' CHECK (
        bot_a_environment IN ('platform_low','platform_high','remote_local','human')
    ),
    bot_b_environment   TEXT    NOT NULL DEFAULT 'platform_low' CHECK (
        bot_b_environment IN ('platform_low','platform_high','remote_local','human')
    ),
    bot_a_local_agent_id INTEGER,
    bot_b_local_agent_id INTEGER,
    human_user_id       INTEGER,
    human_seat          INTEGER CHECK (human_seat IN (0,1)),
    contest_id          INTEGER,
    contest_pairing_id  INTEGER,
    match_config        TEXT    NOT NULL DEFAULT '{}' CHECK (json_valid(match_config)),
    rated               INTEGER NOT NULL CHECK (rated IN (0,1)),
    rating_reason       TEXT    NOT NULL,
    match_slots         INTEGER NOT NULL DEFAULT 1 CHECK (match_slots=1),
    sandbox_units       INTEGER NOT NULL CHECK (sandbox_units BETWEEN 0 AND 2),
    host_cpu_millis     INTEGER NOT NULL CHECK (host_cpu_millis>=0),
    host_memory_mb      INTEGER NOT NULL CHECK (host_memory_mb>=0),
    profile_version     INTEGER NOT NULL DEFAULT 1 CHECK (profile_version>=0),
    current_match_id    TEXT,
    auto_decision_id    INTEGER,
    cancel_requested    INTEGER NOT NULL DEFAULT 0 CHECK (cancel_requested IN (0,1)),
    attempt_count       INTEGER NOT NULL DEFAULT 0 CHECK (attempt_count>=0),
    cleanup_state       TEXT    NOT NULL DEFAULT 'none' CHECK (
        cleanup_state IN ('none','pending','confirmed')
    ),
    failure_count       INTEGER NOT NULL DEFAULT 0 CHECK (failure_count>=0),
    next_attempt_at     TEXT,
    retryable           INTEGER NOT NULL DEFAULT 0 CHECK (retryable IN (0,1)),
    terminal_reason     TEXT    NOT NULL DEFAULT '',
    last_error          TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL,
    claimed_at          TEXT,
    started_at          TEXT,
    settling_at         TEXT,
    terminal_at         TEXT,
    CONSTRAINT chk_execution_job_human_resources CHECK (
        (source='human' AND match_type='human' AND sandbox_units=1
         AND human_user_id IS NOT NULL AND human_seat IS NOT NULL
         AND ((human_seat=0 AND bot_a_environment='human'
               AND bot_b_environment='platform_low')
              OR (human_seat=1 AND bot_b_environment='human'
                  AND bot_a_environment='platform_low'))) OR
        (source<>'human' AND match_type<>'human'
         AND human_user_id IS NULL AND human_seat IS NULL
         AND bot_a_environment<>'human' AND bot_b_environment<>'human')
    ),
    CONSTRAINT chk_execution_job_environment_source CHECK (
        profile_version=0 OR
        (source='contest' AND bot_a_environment='platform_high'
                          AND bot_b_environment='platform_high') OR
        (source='auto' AND bot_a_environment='platform_low'
                       AND bot_b_environment='platform_low') OR
        (source='manual' AND bot_a_environment IN ('platform_low','remote_local')
                         AND bot_b_environment IN ('platform_low','remote_local')) OR
        source='human'
    ),
    CONSTRAINT chk_execution_job_local_agents CHECK (
        ((bot_a_environment='remote_local') = (bot_a_local_agent_id IS NOT NULL))
        AND ((bot_b_environment='remote_local') = (bot_b_local_agent_id IS NOT NULL))
        AND (bot_a_environment<>'remote_local' OR bot_a_version_id IS NULL)
        AND (bot_b_environment<>'remote_local' OR bot_b_version_id IS NULL)
    ),
    CONSTRAINT chk_execution_job_resource_snapshot CHECK (
        sandbox_units =
            (CASE WHEN bot_a_environment IN ('platform_low','platform_high') THEN 1 ELSE 0 END)
          + (CASE WHEN bot_b_environment IN ('platform_low','platform_high') THEN 1 ELSE 0 END)
        AND (profile_version<>1 OR host_cpu_millis =
            (CASE WHEN bot_a_environment='platform_low' THEN 1000
                  WHEN bot_a_environment='platform_high' THEN 2000 ELSE 0 END)
          + (CASE WHEN bot_b_environment='platform_low' THEN 1000
                  WHEN bot_b_environment='platform_high' THEN 2000 ELSE 0 END))
        AND (profile_version<>1 OR host_memory_mb =
            (CASE WHEN bot_a_environment='platform_low' THEN 512
                  WHEN bot_a_environment='platform_high' THEN 2048 ELSE 0 END)
          + (CASE WHEN bot_b_environment='platform_low' THEN 512
                  WHEN bot_b_environment='platform_high' THEN 2048 ELSE 0 END))
    ),
    CONSTRAINT chk_execution_job_contest_ref CHECK (
        (source='contest' AND contest_id IS NOT NULL
         AND contest_pairing_id IS NOT NULL) OR
        (source<>'contest' AND contest_pairing_id IS NULL)
    ),
    CONSTRAINT chk_execution_job_lifecycle CHECK (
        (status='queued' AND current_match_id IS NULL
         AND claimed_at IS NULL AND started_at IS NULL AND settling_at IS NULL
         AND terminal_at IS NULL AND cleanup_state='none') OR
        (status='starting' AND current_match_id IS NOT NULL
         AND claimed_at IS NOT NULL AND terminal_at IS NULL
         AND cleanup_state IN ('none','pending','confirmed')) OR
        (status='running' AND current_match_id IS NOT NULL
         AND claimed_at IS NOT NULL AND started_at IS NOT NULL
         AND terminal_at IS NULL
         AND cleanup_state IN ('none','pending','confirmed')) OR
        (status='settling' AND current_match_id IS NOT NULL
         AND claimed_at IS NOT NULL AND settling_at IS NOT NULL
         AND terminal_at IS NULL AND cleanup_state IN ('pending','confirmed')) OR
        (status IN ('completed','cancelled','interrupted')
         AND terminal_at IS NOT NULL)
    )
);

-- 每次 claim 对应一个不可复活的 match attempt。running crash 若已有公开事件，
-- 旧 attempt 保留 interrupted 审计；自动/赛事请求可以在同一 public_id 下重排新 attempt。
CREATE TABLE IF NOT EXISTS execution_job_attempts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id          INTEGER NOT NULL REFERENCES execution_jobs(id) ON DELETE RESTRICT,
    attempt_no      INTEGER NOT NULL CHECK (attempt_no>=1),
    match_id        TEXT    NOT NULL UNIQUE,
    status          TEXT    NOT NULL CHECK (
        status IN ('starting','running','settling','completed',
                   'cancelled','interrupted')
    ),
    events_observed INTEGER NOT NULL DEFAULT 0 CHECK (events_observed IN (0,1)),
    created_at      TEXT    NOT NULL,
    started_at      TEXT,
    terminal_at     TEXT,
    terminal_reason TEXT    NOT NULL DEFAULT '',
    UNIQUE(job_id,attempt_no)
);

-- 本机 Bot 的占用凭据与 execution attempt 同生命周期。服务重启时旧 attempt
-- 会由现有恢复管线进入终态，本表让“连接已释放”也成为可审计事实。
CREATE TABLE IF NOT EXISTS local_ai_leases (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id        INTEGER NOT NULL REFERENCES local_ai_agents(id) ON DELETE RESTRICT,
    job_public_id   TEXT    NOT NULL,
    attempt_no      INTEGER NOT NULL CHECK (attempt_no>=1),
    seat            INTEGER NOT NULL CHECK (seat IN (0,1)),
    status          TEXT    NOT NULL DEFAULT 'active' CHECK (
        status IN ('active','released')
    ),
    acquired_at     TEXT    NOT NULL,
    released_at     TEXT,
    terminal_reason TEXT    NOT NULL DEFAULT '',
    UNIQUE(job_public_id,attempt_no,seat)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_local_ai_agent_active_lease
    ON local_ai_leases(agent_id) WHERE status='active';

-- 单 dispatcher 控制面。跨进程唯一性由 DB 邻接 OS flock 提供；本表只保存
-- 可诊断/可恢复的状态，不保存 lease、PID、boot id 或 daemon incarnation。
CREATE TABLE IF NOT EXISTS execution_control (
    singleton           INTEGER PRIMARY KEY CHECK (singleton=1),
    dispatcher_state    TEXT    NOT NULL DEFAULT 'stopped' CHECK (
        dispatcher_state IN ('stopped','starting','running','paused','stopping')
    ),
    accepting           INTEGER NOT NULL DEFAULT 0 CHECK (accepting IN (0,1)),
    auto_enabled        INTEGER NOT NULL DEFAULT 1 CHECK (auto_enabled IN (0,1)),
    deployment_drain_requested INTEGER NOT NULL DEFAULT 0 CHECK (
        deployment_drain_requested IN (0,1)
    ),
    deployment_drain_reason TEXT NOT NULL DEFAULT '',
    pause_reason        TEXT    NOT NULL DEFAULT '',
    retry_count         INTEGER NOT NULL DEFAULT 0 CHECK (retry_count>=0),
    retry_at            TEXT,
    updated_at          TEXT    NOT NULL
);
INSERT OR IGNORE INTO execution_control(
    singleton,dispatcher_state,accepting,auto_enabled,pause_reason,retry_count,updated_at
) VALUES(1,'stopped',0,1,'',0,CURRENT_TIMESTAMP);

-- Docker create 的物理事实先于 execution attempt 容器存在。这个单例 journal
-- 与 DB 邻接的实例级 flock 共同封住跨线程/跨进程的 create/cleanup 窗口；
-- creating 不能仅凭同一 host boot 下的 label 双零自动清除。
CREATE TABLE IF NOT EXISTS docker_launch_journal (
    singleton       INTEGER PRIMARY KEY CHECK (singleton=1),
    state           TEXT    NOT NULL DEFAULT 'idle' CHECK (
        state IN ('idle','creating','created')
    ),
    launch_token    TEXT,
    instance_key    TEXT,
    owner_kind      TEXT CHECK (
        owner_kind IS NULL OR owner_kind IN ('execution','preflight')
    ),
    job_public_id   TEXT,
    attempt_no      INTEGER CHECK (attempt_no IS NULL OR attempt_no>=1),
    slot            INTEGER CHECK (slot IS NULL OR slot>=0),
    container_name  TEXT,
    host_boot_id    TEXT,
    updated_at      TEXT    NOT NULL,
    CONSTRAINT chk_docker_launch_journal_shape CHECK (
        (state='idle' AND launch_token IS NULL AND instance_key IS NULL
         AND owner_kind IS NULL AND job_public_id IS NULL
         AND attempt_no IS NULL AND slot IS NULL AND container_name IS NULL
         AND host_boot_id IS NULL) OR
        (state IN ('creating','created') AND launch_token IS NOT NULL
         AND instance_key IS NOT NULL AND owner_kind IS NOT NULL
         AND job_public_id IS NOT NULL AND attempt_no IS NOT NULL
         AND slot IS NOT NULL AND container_name IS NOT NULL
         AND host_boot_id IS NOT NULL)
    )
);
INSERT OR IGNORE INTO docker_launch_journal(singleton,state,updated_at)
VALUES(1,'idle',CURRENT_TIMESTAMP);

CREATE INDEX IF NOT EXISTS idx_execution_jobs_dispatch
    ON execution_jobs(status,priority,created_at,id);
CREATE INDEX IF NOT EXISTS idx_execution_jobs_owner
    ON execution_jobs(owner_user_id,status,created_at);
CREATE INDEX IF NOT EXISTS idx_execution_jobs_source
    ON execution_jobs(source,status,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_jobs_current_match
    ON execution_jobs(current_match_id) WHERE current_match_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_execution_jobs_active_contest_pairing
    ON execution_jobs(contest_pairing_id)
    WHERE contest_pairing_id IS NOT NULL
      AND status IN ('queued','starting','running','settling');

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
    job_public_id               TEXT,
    CONSTRAINT chk_auto_decision_bots CHECK (bot_a_id <> bot_b_id),
    CONSTRAINT chk_auto_decision_owners CHECK (owner_a_id <> owner_b_id),
    CONSTRAINT chk_auto_decision_lane CHECK (
        requested_lane IN ('bootstrap','established') AND
        actual_lane IN ('bootstrap','established')
    ),
    CONSTRAINT chk_auto_decision_lifecycle CHECK (
        lifecycle IN ('queued','dispatched','completed','aborted','cancelled')
    )
);

-- 持久公平游标。next_lane: 0=bootstrap, 1=established。
CREATE TABLE IF NOT EXISTS auto_match_fair_state (
    singleton           INTEGER PRIMARY KEY CHECK (singleton=1),
    next_game_idx       INTEGER NOT NULL DEFAULT 0 CHECK (next_game_idx>=0),
    next_lane           INTEGER NOT NULL DEFAULT 0 CHECK (next_lane IN (0,1)),
    revision            INTEGER NOT NULL DEFAULT 0 CHECK (revision>=0),
    bootstrap_version   INTEGER NOT NULL DEFAULT 0 CHECK (bootstrap_version>=0),
    updated_at          TEXT    NOT NULL
);
INSERT OR IGNORE INTO auto_match_fair_state(
    singleton,next_game_idx,next_lane,revision,bootstrap_version,updated_at
) VALUES(1,0,0,0,0,CURRENT_TIMESTAMP);

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

-- 评分历史快照：每次 _apply_ratings 落一条，用于数值变化与曲线。
-- 每 bot 限保留最近 N 条（见 store 截断）。
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

-- 每款游戏只有一个实时评分池。大版规则切换时，旧投影完整
-- 归档，实时 ratings/history/pair_stats 从新池零样本开始。
CREATE TABLE IF NOT EXISTS rating_pool_state (
    game_id          TEXT PRIMARY KEY,
    active_pool_id   TEXT NOT NULL,
    ruleset_version  TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    activated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rating_pool_archives (
    game_id          TEXT NOT NULL,
    pool_id          TEXT NOT NULL,
    ruleset_version  TEXT NOT NULL,
    protocol_version TEXT NOT NULL,
    archived_at      TEXT NOT NULL,
    ratings_count    INTEGER NOT NULL,
    history_count    INTEGER NOT NULL,
    pair_count       INTEGER NOT NULL,
    projection_digest TEXT NOT NULL,
    PRIMARY KEY(game_id,pool_id)
);

CREATE TABLE IF NOT EXISTS ratings_archive (
    bot_id          INTEGER NOT NULL,
    game_id         TEXT NOT NULL,
    pool_id         TEXT NOT NULL,
    rating          REAL NOT NULL,
    rd              REAL NOT NULL,
    vol             REAL NOT NULL,
    wins            INTEGER NOT NULL,
    losses          INTEGER NOT NULL,
    draws            INTEGER NOT NULL,
    delta_total     INTEGER NOT NULL,
    matches_played  INTEGER NOT NULL,
    last_played_at  TEXT,
    archived_at     TEXT NOT NULL,
    PRIMARY KEY(bot_id,game_id,pool_id)
);

CREATE TABLE IF NOT EXISTS rating_history_archive (
    original_id     INTEGER NOT NULL,
    bot_id          INTEGER NOT NULL,
    game_id         TEXT NOT NULL,
    pool_id         TEXT NOT NULL,
    rating          REAL NOT NULL,
    rd              REAL NOT NULL,
    vol             REAL NOT NULL,
    matches_played  INTEGER NOT NULL,
    reason          TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    archived_at     TEXT NOT NULL,
    PRIMARY KEY(game_id,pool_id,original_id)
);

CREATE TABLE IF NOT EXISTS pair_stats_archive (
    bot_a_id        INTEGER NOT NULL,
    bot_b_id        INTEGER NOT NULL,
    game_id         TEXT NOT NULL,
    pool_id         TEXT NOT NULL,
    samples         INTEGER NOT NULL,
    last_played_at  TEXT NOT NULL,
    a_wins          INTEGER NOT NULL,
    a_losses        INTEGER NOT NULL,
    draws            INTEGER NOT NULL,
    archived_at     TEXT NOT NULL,
    PRIMARY KEY(bot_a_id,bot_b_id,game_id,pool_id)
);

-- 一次性游戏契约切换的幂等与审计凭据。hard cutover 的 manifest 固定
-- 新建 vN；same-protocol rule-only 使用空 manifest。完整切换边不同或
-- manifest_digest 不同时绝不得复用 cutover_id。
CREATE TABLE IF NOT EXISTS protocol_cutovers (
    cutover_id       TEXT PRIMARY KEY,
    game_id          TEXT NOT NULL,
    from_ruleset     TEXT NOT NULL,
    to_ruleset       TEXT NOT NULL,
    from_protocol    TEXT NOT NULL,
    to_protocol      TEXT NOT NULL,
    from_rating_pool TEXT NOT NULL,
    to_rating_pool   TEXT NOT NULL,
    manifest_digest  TEXT NOT NULL,
    manifest_json    TEXT NOT NULL,
    bot_count        INTEGER NOT NULL,
    retired_count    INTEGER NOT NULL,
    cancelled_jobs   INTEGER NOT NULL,
    archive_digest   TEXT NOT NULL,
    completed_at     TEXT NOT NULL
);

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
    communication_message_public_id TEXT,         -- 新通信真相的兼容投影；旧行保持 NULL
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

-- ── communications：平台/admin ↔ 单个用户；不开放任意用户私信 ──────────
CREATE TABLE IF NOT EXISTS broadcasts (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id               TEXT    NOT NULL UNIQUE,
    state                   TEXT    NOT NULL DEFAULT 'draft',
    created_by_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    audience_kind           TEXT    NOT NULL,
    audience_filter_json    TEXT    NOT NULL DEFAULT '{}',
    audience_snapshot_hash  TEXT    NOT NULL,
    audience_count          INTEGER NOT NULL DEFAULT 0,
    subject                 TEXT    NOT NULL,
    body_text               TEXT    NOT NULL,
    sanitized_html          TEXT    NOT NULL,
    channels_json           TEXT    NOT NULL DEFAULT '["in_app"]',
    approval_token_hash     TEXT    NOT NULL,
    preview_expires_at      TEXT    NOT NULL,
    scheduled_at            TEXT,
    approved_at             TEXT,
    started_at              TEXT,
    completed_at            TEXT,
    cancelled_at            TEXT,
    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL,
    CONSTRAINT chk_broadcast_state CHECK (
        state IN ('draft','scheduled','running','completed','cancelled')),
    CONSTRAINT chk_broadcast_count CHECK (audience_count >= 0)
);
CREATE INDEX IF NOT EXISTS idx_broadcast_state_schedule
    ON broadcasts(state, scheduled_at, id);

CREATE TABLE IF NOT EXISTS conversations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id           TEXT    NOT NULL UNIQUE,
    kind                TEXT    NOT NULL,
    subject             TEXT    NOT NULL DEFAULT '',
    status              TEXT    NOT NULL DEFAULT 'open',
    created_by_user_id  INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_by_kind     TEXT    NOT NULL DEFAULT 'platform',
    broadcast_id        INTEGER REFERENCES broadcasts(id) ON DELETE SET NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    closed_at           TEXT,
    CONSTRAINT chk_conversation_kind CHECK (
        kind IN ('notification','support','bug_report','broadcast','auth','system')),
    CONSTRAINT chk_conversation_status CHECK (
        status IN ('open','closed','archived')),
    CONSTRAINT chk_conversation_creator CHECK (
        created_by_kind IN ('user','admin','platform'))
);
CREATE INDEX IF NOT EXISTS idx_conversations_updated
    ON conversations(updated_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_conversations_broadcast
    ON conversations(broadcast_id);

CREATE TABLE IF NOT EXISTS conversation_participants (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id               TEXT    NOT NULL UNIQUE,
    conversation_id         INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    user_id                 INTEGER REFERENCES users(id) ON DELETE SET NULL,
    participant_kind        TEXT    NOT NULL,
    last_read_message_id    INTEGER,
    joined_at               TEXT    NOT NULL,
    CONSTRAINT chk_participant_kind CHECK (
        participant_kind IN ('user','admin','platform'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_participant_user
    ON conversation_participants(conversation_id, user_id)
    WHERE user_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_conversation_participant_platform
    ON conversation_participants(conversation_id, participant_kind)
    WHERE user_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_conversation_participant_lookup
    ON conversation_participants(user_id, conversation_id);

CREATE TABLE IF NOT EXISTS messages (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id           TEXT    NOT NULL UNIQUE,
    conversation_id     INTEGER NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    reply_to_id         INTEGER REFERENCES messages(id) ON DELETE SET NULL,
    author_user_id      INTEGER REFERENCES users(id) ON DELETE SET NULL,
    author_kind         TEXT    NOT NULL,
    body_text           TEXT    NOT NULL,
    sanitized_html      TEXT    NOT NULL,
    metadata_json       TEXT    NOT NULL DEFAULT '{}',
    created_at          TEXT    NOT NULL,
    CONSTRAINT chk_message_author CHECK (
        author_kind IN ('user','admin','platform'))
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation
    ON messages(conversation_id, id);

CREATE TABLE IF NOT EXISTS deliveries (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id               TEXT    NOT NULL UNIQUE,
    message_id              INTEGER REFERENCES messages(id) ON DELETE CASCADE,
    broadcast_id            INTEGER REFERENCES broadcasts(id) ON DELETE SET NULL,
    channel                 TEXT    NOT NULL,
    recipient_user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    address_snapshot        TEXT    NOT NULL DEFAULT '',
    status                  TEXT    NOT NULL DEFAULT 'queued',
    priority                INTEGER NOT NULL DEFAULT 0,
    attempt_count           INTEGER NOT NULL DEFAULT 0,
    max_attempts            INTEGER NOT NULL DEFAULT 5,
    next_attempt_at         TEXT    NOT NULL,
    last_error              TEXT    NOT NULL DEFAULT '',
    provider                TEXT    NOT NULL DEFAULT '',
    provider_message_id     TEXT    NOT NULL DEFAULT '',
    idempotency_key         TEXT    NOT NULL UNIQUE,
    template_key            TEXT    NOT NULL DEFAULT '',
    template_version        INTEGER NOT NULL DEFAULT 0,
    payload_json            TEXT    NOT NULL DEFAULT '{}',
    claimed_at              TEXT,
    sent_at                 TEXT,
    cancelled_at            TEXT,
    created_at              TEXT    NOT NULL,
    updated_at              TEXT    NOT NULL,
    CONSTRAINT chk_delivery_channel CHECK (channel IN ('in_app','email')),
    CONSTRAINT chk_delivery_status CHECK (
        status IN ('queued','sending','sent','failed','cancelled')),
    CONSTRAINT chk_delivery_attempts CHECK (
        attempt_count >= 0 AND max_attempts > 0)
);
CREATE INDEX IF NOT EXISTS idx_deliveries_due
    ON deliveries(status, next_attempt_at, priority DESC, id);
CREATE INDEX IF NOT EXISTS idx_deliveries_recipient
    ON deliveries(recipient_user_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_deliveries_broadcast
    ON deliveries(broadcast_id, status);

CREATE TABLE IF NOT EXISTS broadcast_recipients (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id           TEXT    NOT NULL UNIQUE,
    broadcast_id        INTEGER NOT NULL REFERENCES broadcasts(id) ON DELETE CASCADE,
    user_id             INTEGER REFERENCES users(id) ON DELETE SET NULL,
    state               TEXT    NOT NULL DEFAULT 'pending',
    attempt_count       INTEGER NOT NULL DEFAULT 0,
    max_attempts        INTEGER NOT NULL DEFAULT 5,
    next_attempt_at     TEXT    NOT NULL DEFAULT '',
    last_error          TEXT    NOT NULL DEFAULT '',
    conversation_id     INTEGER REFERENCES conversations(id) ON DELETE SET NULL,
    created_at          TEXT    NOT NULL,
    processed_at        TEXT,
    CONSTRAINT chk_broadcast_recipient_state CHECK (
        state IN ('pending','processing','delivered','cancelled','failed')),
    CONSTRAINT chk_broadcast_recipient_attempts CHECK (
        attempt_count >= 0 AND max_attempts > 0)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_broadcast_recipient_user
    ON broadcast_recipients(broadcast_id, user_id)
    WHERE user_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_broadcast_recipient_work
    ON broadcast_recipients(broadcast_id, state, id);

-- 小白式 Bug 反馈复用 conversation；状态变化只追加 event，不改历史事件。
CREATE TABLE IF NOT EXISTS bug_reports (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id           TEXT    NOT NULL UNIQUE,
    conversation_id     INTEGER NOT NULL UNIQUE REFERENCES conversations(id) ON DELETE CASCADE,
    reporter_user_id    INTEGER REFERENCES users(id) ON DELETE SET NULL,
    tracking_token_hash TEXT    NOT NULL DEFAULT '',
    category            TEXT    NOT NULL,
    impact              TEXT    NOT NULL,
    title               TEXT    NOT NULL,
    current_route       TEXT    NOT NULL DEFAULT '',
    status              TEXT    NOT NULL DEFAULT 'new',
    duplicate_of_id     INTEGER REFERENCES bug_reports(id) ON DELETE SET NULL,
    created_at          TEXT    NOT NULL,
    updated_at          TEXT    NOT NULL,
    CONSTRAINT chk_bug_status CHECK (
        status IN ('new','acknowledged','needs_info','in_progress','resolved','duplicate','wont_fix'))
);
CREATE INDEX IF NOT EXISTS idx_bug_reports_status
    ON bug_reports(status, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bug_reports_reporter
    ON bug_reports(reporter_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS bug_report_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id           TEXT    NOT NULL UNIQUE,
    bug_report_id       INTEGER NOT NULL REFERENCES bug_reports(id) ON DELETE CASCADE,
    event_type          TEXT    NOT NULL,
    actor_user_id       INTEGER REFERENCES users(id) ON DELETE SET NULL,
    from_status         TEXT    NOT NULL DEFAULT '',
    to_status           TEXT    NOT NULL DEFAULT '',
    note                TEXT    NOT NULL DEFAULT '',
    created_at          TEXT    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bug_report_events
    ON bug_report_events(bug_report_id, id);

CREATE TABLE IF NOT EXISTS diagnostic_bundles (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id           TEXT    NOT NULL UNIQUE,
    bug_report_id       INTEGER NOT NULL UNIQUE REFERENCES bug_reports(id) ON DELETE CASCADE,
    schema_version      INTEGER NOT NULL DEFAULT 1,
    bundle_json         TEXT    NOT NULL,
    created_at          TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS bug_attachments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id           TEXT    NOT NULL UNIQUE,
    bug_report_id       INTEGER NOT NULL REFERENCES bug_reports(id) ON DELETE CASCADE,
    uploaded_by_user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    original_name       TEXT    NOT NULL,
    media_type          TEXT    NOT NULL,
    size_bytes          INTEGER NOT NULL,
    sha256              TEXT    NOT NULL,
    storage_path        TEXT    NOT NULL UNIQUE,
    created_at          TEXT    NOT NULL,
    CONSTRAINT chk_bug_attachment_size CHECK (size_bytes > 0)
);
CREATE INDEX IF NOT EXISTS idx_bug_attachments_report
    ON bug_attachments(bug_report_id, id);

CREATE TABLE IF NOT EXISTS contest_entries (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    contest_id          INTEGER NOT NULL REFERENCES contests(id) ON DELETE CASCADE,
    user_id             INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    bot_id              INTEGER REFERENCES bots(id) ON DELETE SET NULL,
    registered_at       TEXT    NOT NULL,
    group_id            TEXT    NOT NULL DEFAULT '',
    seed                INTEGER NOT NULL DEFAULT 0,
    eliminated          INTEGER NOT NULL DEFAULT 0,
    dispatched_at       TEXT,
    -- 实名赛在报名事务中冻结资料；历史行保持 NULL，由私有读模型明确标为
    -- current_profile_legacy，绝不把迁移时的当前资料伪装成报名快照。
    real_name_snapshot  TEXT,
    phone_snapshot      TEXT,
    school_snapshot     TEXT,
    student_id_snapshot TEXT,
    identity_captured_at TEXT,
    identity_source     TEXT,
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
CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_match_decisions_match
    ON auto_match_decisions(match_id) WHERE match_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_auto_match_decisions_created
    ON auto_match_decisions(id DESC);
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
SCHEMA = SCHEMA.replace(
    "__MATCH_DEBUG_MAX_ENTRIES__", str(MATCH_DEBUG_MAX_ENTRIES_PER_MATCH)
)
SCHEMA = SCHEMA.replace(
    "__MATCH_DEBUG_MAX_BYTES__", str(MATCH_DEBUG_MAX_BYTES_PER_MATCH)
)
SCHEMA = SCHEMA.replace(
    "__MATCH_DEBUG_MAX_ENTRY_BYTES__", str(MATCH_DEBUG_MAX_ENTRY_BYTES)
)

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
TECHNICAL_INCIDENT_MESSAGES = {
    "invalid_json": "Bot 输出不是合法 JSON",
    "invalid_envelope": "Bot 响应信封必须是 JSON 对象",
    "missing_response": "Bot 响应缺少必填 response 字段",
    "invalid_response": "Bot response 字段不符合本游戏协议",
    "missing_keep_running": "LongRunning Bot 未输出 KEEP_RUNNING 握手",
    "invalid_keep_running": "LongRunning Bot 的 KEEP_RUNNING 握手不正确",
    "decision_timeout": "Bot 未在决策时限内输出完整响应行",
    "response_line_too_large": "Bot 响应行超过 64 KiB 上限",
    "local_ai_unavailable": "本地 Bot 连接已中断",
    "local_ai_timeout": "本地 Bot 未在截止时间前响应",
    "local_ai_revoked": "本地 Bot 连接已撤销",
}

# 公开 completed/match_end 唯一允许的稳定裁决码。游戏裁判与平台技术判负
# 只能从这里选择；未知历史文本在读取/迁移边界统一为 completed。
PUBLIC_MATCH_COMPLETED_REASONS = frozenset(
    {
        "bot_deleted",
        "completed",
        "contest_bot_unavailable",
        "crash",
        "double_pass",
        "draw",
        "error",
        "five",
        "illegal",
        "illegal_candidates",
        "illegal_opening",
        "illegal_selection",
        "illegal_swap",
        "majority",
        "protocol_error",
        "score",
        "technical_loss",
        "timeout",
        "board_full",
        "forbidden_double_four",
        "forbidden_double_three",
        "forbidden_overline",
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

# 赛事报名实名来源。持久化只写报名时快照；旧报名在私有读边界派生 legacy
# 来源，不回写本常量，避免把当前资料伪装成历史快照。
CONTEST_IDENTITY_SOURCE_REGISTRATION = "registration_profile"
CONTEST_IDENTITY_SOURCE_LEGACY = "current_profile_legacy"

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
