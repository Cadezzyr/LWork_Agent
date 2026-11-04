-- ============================================================================
-- 同上结构的 SQLite 版本（本地开发 / 单机部署 / pytest 用）
--   sqlite3 agent.db < docs/db/schema.sqlite.sql
--
-- 与 PostgreSQL 版的差异：
--   * UUID -> TEXT（应用层生成 uuid4 字符串），无 pgcrypto 依赖
--   * JSONB -> TEXT（存 JSON 字符串，SQLite 3.38+ 可用 json_ 函数查询）
--   * citext 大小写不敏感 -> 由 COLLATE NOCASE 或 lower() 唯一索引实现
--   * timestamptz -> TEXT（ISO-8601 UTC 字符串，可直接排序比较）
--   * INET -> TEXT
--   * 部分索引 WHERE 子句 SQLite 支持，保留
-- ============================================================================

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 1. users
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                  TEXT        PRIMARY KEY,
    username            TEXT        NOT NULL COLLATE NOCASE,
    email               TEXT        COLLATE NOCASE,
    password_hash       TEXT        NOT NULL,
    display_name        TEXT,
    avatar_url          TEXT,
    status              INTEGER     NOT NULL DEFAULT 1,
    role                TEXT        NOT NULL DEFAULT 'user',
    password_changed_at TEXT        NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    last_login_at       TEXT,
    created_at          TEXT        NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at          TEXT        NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT users_role_valid   CHECK (role IN ('user', 'admin')),
    CONSTRAINT users_status_valid CHECK (status IN (0, 1))
);

-- 大小写不敏感的唯一登录名：靠列上的 COLLATE NOCASE（等价于 PG 的 citext），
-- 而不是 lower(username) 表达式索引——后者无法服务 WHERE username = ? 的等值查询。
CREATE UNIQUE INDEX IF NOT EXISTS users_username_ux ON users (username);
CREATE UNIQUE INDEX IF NOT EXISTS users_email_ux ON users (email)
    WHERE email IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 2. user_sessions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_sessions (
    id           TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash   TEXT NOT NULL UNIQUE,
    user_agent   TEXT,
    ip           TEXT,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT,
    last_used_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    created_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE INDEX IF NOT EXISTS user_sessions_user_idx ON user_sessions (user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS user_sessions_expiry_idx ON user_sessions (expires_at)
    WHERE revoked_at IS NULL;

-- ---------------------------------------------------------------------------
-- 3. conversations
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id              TEXT    PRIMARY KEY,
    user_id         TEXT    NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    title           TEXT    NOT NULL DEFAULT '新对话',
    model           TEXT,
    message_count   INTEGER NOT NULL DEFAULT 0,
    turn_count      INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    -- 最近活跃时间（对应 SessionState.last_active_at）：创建时即写入，避免空值排序歧义
    last_active_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    pinned          INTEGER NOT NULL DEFAULT 0,
    archived_at     TEXT,
    deleted_at      TEXT,
    metadata        TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT conversations_pinned_valid CHECK (pinned IN (0, 1))
);

-- 列表页主查询索引：user_id 等值筛选 + last_active_at DESC 提供有序输出，
-- 部分索引谓词让 IS NULL 过滤不必回表。
CREATE INDEX IF NOT EXISTS conversations_user_recent_idx
    ON conversations (user_id, last_active_at DESC)
    WHERE deleted_at IS NULL;

-- ---------------------------------------------------------------------------
-- 4. messages
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT    NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT    NOT NULL,
    content         TEXT    NOT NULL DEFAULT '',
    name            TEXT,
    tool_call_id    TEXT,
    tool_calls      TEXT    NOT NULL DEFAULT '[]',
    status          TEXT    NOT NULL DEFAULT 'ok',
    error_code      TEXT,
    model           TEXT,
    input_tokens    INTEGER NOT NULL DEFAULT 0,
    output_tokens   INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER,
    langchain_id    TEXT,
    metadata        TEXT    NOT NULL DEFAULT '{}',
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT messages_role_valid   CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    CONSTRAINT messages_status_valid CHECK (status IN ('ok', 'error', 'cancelled')),
    CONSTRAINT messages_seq_ux       UNIQUE (conversation_id, seq)
);

-- 历史回放：UNIQUE (conversation_id, seq) 自带的索引已能按 seq 有序输出，无需重复建索引。
-- 定位某条工具调用对应的结果
CREATE INDEX IF NOT EXISTS messages_tool_call_idx ON messages (conversation_id, tool_call_id)
    WHERE tool_call_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- 5. user_settings
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_settings (
    user_id            TEXT PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    workspace_root     TEXT,
    workspace_writable INTEGER NOT NULL DEFAULT 1,
    theme              TEXT,
    settings           TEXT NOT NULL DEFAULT '{}',
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),

    CONSTRAINT user_settings_writable_valid CHECK (workspace_writable IN (0, 1))
);
