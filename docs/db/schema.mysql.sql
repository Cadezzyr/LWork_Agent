-- ============================================================================
-- LangChain Agent API —— 用户登录 + 对话历史 数据库结构（MySQL 8.0.16+ / 9.x）
--
-- 与 schema.postgres.sql 等价，差异只来自方言能力，业务字段一一对应。
--
-- MySQL 版本下限说明（低于此版本会静默丢约束，务必确认）：
--   8.0.13+  JSON 列支持表达式默认值  DEFAULT (JSON_OBJECT())
--   8.0.16+  CHECK 约束真正生效（8.0.16 之前只解析不强制）
--   8.0     DESC 索引真正生效（5.7 解析但忽略）
--   8.0     REGEXP / REGEXP_LIKE 可用于 CHECK
--   排序规则 utf8mb4_0900_ai_ci 仅在 8.0+ 存在；5.7 请换成 utf8mb4_unicode_ci
--
-- 执行：
--   mysql -u root -p < docs/db/schema.mysql.sql
-- ============================================================================

CREATE DATABASE IF NOT EXISTS agent_chat
    DEFAULT CHARACTER SET utf8mb4
    COLLATE utf8mb4_0900_ai_ci;

USE agent_chat;


-- ---------------------------------------------------------------------------
-- 1. users —— 账号主体
--
-- 方言对照：PG 用 citext + lower() 唯一索引做「大小写不敏感」，
-- MySQL 靠 utf8mb4_0900_ai_ci 本身就不区分大小写，直接 UNIQUE 即可。
-- 注意 ai_ci 同时「重音不敏感」（é == e），对本表只含 ASCII 的 username 无影响。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id                  CHAR(36)     NOT NULL DEFAULT (UUID()),
    username            VARCHAR(32)  NOT NULL,
    email               VARCHAR(254) NULL,
    -- argon2id / bcrypt 哈希；绝不存明文
    password_hash       VARCHAR(255) NOT NULL,
    display_name        VARCHAR(64)  NULL,
    avatar_url          VARCHAR(512) NULL,
    -- 1=正常 0=禁用（禁用后仍可查历史，不能登录/发消息）
    status              TINYINT      NOT NULL DEFAULT 1,
    role                VARCHAR(16)  NOT NULL DEFAULT 'user',
    -- 改密后使旧 token 失效：签发 JWT 时比对 iat >= password_changed_at
    password_changed_at DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    last_login_at       DATETIME(6)  NULL,
    created_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at          DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                     ON UPDATE CURRENT_TIMESTAMP(6),

    PRIMARY KEY (id),
    UNIQUE KEY uq_users_username (username),
    UNIQUE KEY uq_users_email (email),

    CONSTRAINT ck_users_username_fmt CHECK (username REGEXP '^[A-Za-z0-9_.-]{3,32}$'),
    CONSTRAINT ck_users_role   CHECK (role IN ('user', 'admin')),
    CONSTRAINT ck_users_status CHECK (status IN (0, 1))
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '账号主体';


-- ---------------------------------------------------------------------------
-- 2. user_sessions —— 登录态（服务端可撤销）
--
-- token_hash 用 ascii_bin：哈希是十六进制串，必须逐字节精确比较，
-- 不能受 ai_ci 的大小写/重音模糊匹配影响。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_sessions (
    id           CHAR(36)    NOT NULL DEFAULT (UUID()),
    user_id      CHAR(36)    NOT NULL,
    token_hash   CHAR(64)    CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
    user_agent   VARCHAR(512) NULL,
    ip           VARCHAR(45) NULL,          -- 兼容 IPv6（PG 版是 INET）
    expires_at   DATETIME(6) NOT NULL,
    revoked_at   DATETIME(6) NULL,
    last_used_at DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    created_at   DATETIME(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    PRIMARY KEY (id),
    UNIQUE KEY uq_sessions_token (token_hash),
    KEY ix_sessions_user (user_id, created_at DESC),
    -- MySQL 没有「部分索引」，PG 版那句 WHERE revoked_at IS NULL 只能退化成
    -- 把 revoked_at 放进索引列，靠最左前缀过滤
    KEY ix_sessions_expires (expires_at, revoked_at),

    CONSTRAINT fk_sessions_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '登录态／刷新令牌';


-- ---------------------------------------------------------------------------
-- 3. conversations —— 一次对话（= 原 SessionState + 归属用户）
--
-- id 沿用 sess_xxx 形态，直接作为 LangGraph 的 thread_id 使用。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS conversations (
    id              VARCHAR(64)  NOT NULL,
    user_id         CHAR(36)     NOT NULL,
    title           VARCHAR(120) NOT NULL DEFAULT '新对话',
    model           VARCHAR(64)  NULL,
    message_count   INT UNSIGNED NOT NULL DEFAULT 0,
    turn_count      INT UNSIGNED NOT NULL DEFAULT 0,
    total_tokens    BIGINT UNSIGNED NOT NULL DEFAULT 0,
    -- 最近活跃时间（对应 SessionState.last_active_at）：创建时即写入，之后每条消息刷新。
    -- NOT NULL 是刻意设计：NULL 在 DESC 排序里会带来顺序歧义且影响索引复用。
    last_active_at  DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    pinned          TINYINT(1)   NOT NULL DEFAULT 0,
    archived_at     DATETIME(6)  NULL,
    -- 软删除：用户点「删除」只打标记
    deleted_at      DATETIME(6)  NULL,
    metadata        JSON         NOT NULL DEFAULT (JSON_OBJECT()),
    created_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    updated_at      DATETIME(6)  NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                 ON UPDATE CURRENT_TIMESTAMP(6),

    PRIMARY KEY (id),
    -- 列表页主查询索引：user_id 等值筛选 + last_active_at DESC 提供有序输出。
    -- MySQL 没有部分索引，只能把 deleted_at 放进索引列模拟 PG 的 WHERE deleted_at IS NULL，
    -- 因此它是 (user_id, deleted_at, last_active_at) 三段式。
    KEY ix_conv_user_recent (user_id, deleted_at, last_active_at DESC),

    CONSTRAINT fk_conv_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT ck_conv_id_fmt  CHECK (id REGEXP '^[A-Za-z0-9_-]{1,128}$'),
    CONSTRAINT ck_conv_pinned  CHECK (pinned IN (0, 1))
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '对话会话';


-- ---------------------------------------------------------------------------
-- 4. messages —— 消息本体（含工具调用轨迹与 token 用量）
--
-- content 用 MEDIUMTEXT：TEXT 上限 64KB，长回复/多模态回填容易超。
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS messages (
    id              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    conversation_id VARCHAR(64)     NOT NULL,
    -- 会话内严格递增序号：历史回放、分页、去重全靠它
    seq             INT UNSIGNED    NOT NULL,
    role            VARCHAR(16)     NOT NULL,
    content         MEDIUMTEXT      NOT NULL,
    -- role='tool' 时记录工具名
    name            VARCHAR(64)     NULL,
    -- role='tool' 时指向对应 tool_call
    tool_call_id    VARCHAR(64)     NULL,
    -- assistant 消息的工具调用清单（含回填结果）：
    --   [{"id":"...","name":"calculator","args":{...},"result":"17.5","status":"success"}]
    tool_calls      JSON            NOT NULL DEFAULT (JSON_ARRAY()),
    -- ok / error / cancelled（客户端中途点「停止」）
    status          VARCHAR(16)     NOT NULL DEFAULT 'ok',
    error_code      VARCHAR(64)     NULL,
    model           VARCHAR(64)     NULL,
    input_tokens    INT UNSIGNED    NOT NULL DEFAULT 0,
    output_tokens   INT UNSIGNED    NOT NULL DEFAULT 0,
    total_tokens    INT UNSIGNED    NOT NULL DEFAULT 0,
    latency_ms      INT UNSIGNED    NULL,
    langchain_id    VARCHAR(64)     NULL,
    metadata        JSON            NOT NULL DEFAULT (JSON_OBJECT()),
    created_at      DATETIME(6)     NOT NULL DEFAULT CURRENT_TIMESTAMP(6),

    PRIMARY KEY (id),
    -- 唯一键 (conversation_id, seq) 本身就是一个可按 seq 排序的索引，
    -- 因此不再单独建 (conversation_id, seq) 普通索引（避免重复索引）
    UNIQUE KEY uq_messages_seq (conversation_id, seq),
    KEY ix_messages_tool_call (conversation_id, tool_call_id),

    CONSTRAINT fk_messages_conv FOREIGN KEY (conversation_id)
        REFERENCES conversations (id) ON DELETE CASCADE,
    CONSTRAINT ck_messages_role   CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    CONSTRAINT ck_messages_status CHECK (status IN ('ok', 'error', 'cancelled'))
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '对话消息';

-- 全局全文检索（可选，默认注释掉）：
-- 中文没有空格分词，必须用内置 ngram 解析器，否则搜不到。
-- 需要全文检索时再用 MySQL 的 FULLTEXT，替代 PG 的 to_tsvector + gin。
-- ALTER TABLE messages ADD FULLTEXT KEY ft_messages_content (content) WITH PARSER ngram;


-- ---------------------------------------------------------------------------
-- 5. user_settings —— 用户级运行时偏好（承接现在只存内存里的工作区设置）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS user_settings (
    user_id            CHAR(36)      NOT NULL,
    workspace_root     VARCHAR(1024) NULL,
    workspace_writable TINYINT(1)    NOT NULL DEFAULT 1,
    theme              VARCHAR(16)   NULL,
    settings           JSON          NOT NULL DEFAULT (JSON_OBJECT()),
    updated_at         DATETIME(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                                     ON UPDATE CURRENT_TIMESTAMP(6),

    PRIMARY KEY (user_id),
    CONSTRAINT fk_settings_user FOREIGN KEY (user_id)
        REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT ck_settings_writable CHECK (workspace_writable IN (0, 1))
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '用户偏好／工作区设置';


-- ---------------------------------------------------------------------------
-- 常用查询（验证索引）
-- ---------------------------------------------------------------------------
-- 会话列表（对应 GET /api/v1/sessions），命中 ix_conv_user_recent
-- SELECT id, title, message_count, turn_count, last_active_at
--   FROM conversations
--  WHERE user_id = ? AND deleted_at IS NULL
--  ORDER BY last_active_at DESC
--  LIMIT 50 OFFSET 0;
--
-- 会话详情（必须带 user_id 防越权）
-- SELECT * FROM conversations WHERE id = ? AND user_id = ? AND deleted_at IS NULL;
-- SELECT role, content, name, tool_call_id, tool_calls, status, latency_ms
--   FROM messages WHERE conversation_id = ? ORDER BY seq;
--
-- 追加消息并维护计数（同一事务；seq 靠 uq_messages_seq 兜并发）
-- INSERT INTO messages (conversation_id, seq, role, content)
-- SELECT ?, COALESCE(MAX(seq), 0) + 1, 'user', ?
--   FROM messages WHERE conversation_id = ?;
-- UPDATE conversations
--    SET message_count = message_count + 1,
--        turn_count    = turn_count + 1,
--        last_active_at = NOW(6)
--  WHERE id = ?;
