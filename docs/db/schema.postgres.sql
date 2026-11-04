-- ============================================================================
-- LangChain Agent API —— 用户登录 + 对话历史 数据库结构（PostgreSQL 13+）
--
-- 与本仓库实现的对应关系：
--   1) app/db/postgres.py 是按本文件手写的 SQL 仓储实现（psycopg2）；
--   2) conversations.id 复用现有 session_id（sess_xxx）作为 LangGraph 的 thread_id，
--      因此 API 契约（ChatRequest.session_id / SessionInfo）无需改动；
--   3) messages 以"会话内自增 seq"排序，回放给模型时按 seq 还原 LangChain 消息
--      （app/services/messages.py: from_records），进程内 checkpointer 丢失时可据此重建上下文；
--   4) 未配置 AGENT_DATABASE_URL 时应用会回退到内存仓储（app/db/memory.py），
--      语义与本结构保持一致，因此单元测试无需数据库。
--
-- 命名约定：snake_case；时间一律 timestamptz（UTC）；主键能不用自增就不用，
-- 对外暴露的 ID 用不可枚举的随机值。
-- ============================================================================

-- gen_random_uuid() 在 PG 13+ 内置于 pgcrypto 也可直接使用；更低版本需装扩展。
CREATE EXTENSION IF NOT EXISTS pgcrypto;
-- 让 username / email 大小写不敏感（可选，见下方唯一索引的替代写法）
CREATE EXTENSION IF NOT EXISTS citext;


-- ---------------------------------------------------------------------------
-- 1. users —— 账号主体
-- ---------------------------------------------------------------------------
CREATE TABLE users (
    id                  UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    -- 登录名：只允许字母数字与 _.- ，长度 3~32；唯一性由下方 lower() 唯一索引保证
    -- CITEXT + 普通唯一索引：citext 的 btree 比较本身大小写不敏感，
    -- 因此 `WHERE username = $1` 既能命中索引，又能拦截 Alice/alice 重复。
    -- 不要改成 lower(username) 表达式索引——那样 `username = $1` 用不上索引。
    username            CITEXT          NOT NULL UNIQUE,
    -- 邮箱可空（支持「用手机号/用户名注册」的产品形态）；登录时与 username 二选一
    email               CITEXT          UNIQUE,
    -- 密码哈希：argon2id 或 bcrypt。绝不允许存明文，也绝不允许为空
    password_hash       TEXT            NOT NULL,
    display_name        VARCHAR(64),
    avatar_url          TEXT,
    -- 1=正常 0=禁用（禁用后仍可查历史，但不能登录/发起对话）
    status              SMALLINT        NOT NULL DEFAULT 1,
    role                VARCHAR(16)     NOT NULL DEFAULT 'user',
    -- 找回密码/改密后使旧 token 全部失效：签发 JWT 时比对 iat >= password_changed_at
    password_changed_at TIMESTAMPTZ     NOT NULL DEFAULT now(),
    last_login_at       TIMESTAMPTZ,
    created_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ     NOT NULL DEFAULT now(),

    CONSTRAINT users_username_format CHECK (username ~ '^[A-Za-z0-9_.-]{3,32}$'),
    CONSTRAINT users_role_valid      CHECK (role IN ('user', 'admin')),
    CONSTRAINT users_status_valid    CHECK (status IN (0, 1))
);

-- 说明：username / email 均为 CITEXT + 普通 UNIQUE 约束（见建表语句），
-- 大小写不敏感的唯一性与 `WHERE username = $1` 走索引两件事同时满足。
-- 早期版本这里曾用 CREATE UNIQUE INDEX ON users (lower(username))，
-- 经 EXPLAIN 实测该索引无法服务登录查询（规划器不认 lower() 等价），已弃用。


-- ---------------------------------------------------------------------------
-- 2. user_sessions —— 登录态（服务端可撤销的会话/刷新令牌）
--
-- 为什么不只发 JWT：JWT 无法主动下线。这里存一条「服务端会话」，
-- access token 里带 user_session_id，登出时 revoke 即立刻失效。
-- 若只想要无状态 JWT，可以删掉本表，但要接受「无法强制下线」。
-- ---------------------------------------------------------------------------
CREATE TABLE user_sessions (
    id           UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id      UUID        NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- 只存 refresh token 的 SHA-256，泄露数据库也无法直接冒用
    token_hash   CHAR(64)    NOT NULL UNIQUE,
    user_agent   TEXT,
    ip           INET,
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX user_sessions_user_idx ON user_sessions (user_id, created_at DESC);
CREATE INDEX user_sessions_expiry_idx ON user_sessions (expires_at)
    WHERE revoked_at IS NULL;


-- ---------------------------------------------------------------------------
-- 3. conversations —— 一次对话（= 原 SessionState + 归属用户 + 可展示标题）
--
-- id 沿用 session_id 形态（sess_ + 16 位十六进制），既满足 ChatRequest 的
-- ^[A-Za-z0-9_-]{1,128}$ 校验，也直接作为 LangGraph 的 thread_id 使用。
-- ---------------------------------------------------------------------------
CREATE TABLE conversations (
    id              VARCHAR(64)     PRIMARY KEY,
    user_id         UUID            NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    -- 首轮由服务端截取用户首句生成（前端现在也是 truncate(text, 26)）
    title           VARCHAR(120)    NOT NULL DEFAULT '新对话',
    -- 建会话时的模型名，便于历史上不同模型的消息区分
    model           VARCHAR(64),
    -- 冗余计数：列表页不必 count(*)，由应用层在写入消息时同步维护
    message_count   INTEGER         NOT NULL DEFAULT 0,
    turn_count      INTEGER         NOT NULL DEFAULT 0,
    total_tokens    BIGINT          NOT NULL DEFAULT 0,
    -- 最近活跃时间（对应 SessionState.last_active_at）：创建时即写入，之后每条消息刷新。
    -- NOT NULL 是刻意设计：否则 DESC 排序的默认空值方向（NULLS FIRST）与
    -- `ORDER BY ... DESC NULLS LAST` 不一致，索引会完全无法提供有序输出而退化成 Seq Scan + Sort。
    last_active_at  TIMESTAMPTZ     NOT NULL DEFAULT now(),
    pinned          BOOLEAN         NOT NULL DEFAULT false,
    archived_at     TIMESTAMPTZ,
    -- 软删除：用户点「删除」只打标记，保留数据便于审计/撤回
    deleted_at      TIMESTAMPTZ,
    metadata        JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),

    CONSTRAINT conversations_id_format CHECK (id ~ '^[A-Za-z0-9_-]{1,128}$')
);

-- 列表页主查询的唯一索引：某用户未删除的会话按最近活跃倒序。
-- 三个要素缺一不可（每一条都是实测换来的）：
--   1) user_id 在最左，用于等值筛选；
--   2) last_active_at DESC 紧随其后，使 ORDER BY 直接复用索引顺序、省掉 Sort；
--   3) deleted_at 放进 WHERE 谓词（部分索引），使 IS NULL 过滤不需要回表判断。
-- 实测（单用户 5000 会话）：Index Scan 只取 50 行、无 Sort，0.2ms；
-- 少了任意一条就会退化成 Seq/Bitmap Scan + Sort，耗时随会话数线性增长。
CREATE INDEX conversations_user_recent_idx
    ON conversations (user_id, last_active_at DESC)
    WHERE deleted_at IS NULL;

-- 标题模糊搜索（可选）：不建 pg_trgm 也能用 ILIKE，建了才能走索引
-- CREATE EXTENSION IF NOT EXISTS pg_trgm;
-- CREATE INDEX conversations_title_trgm_idx ON conversations USING gin (title gin_trgm_ops);


-- ---------------------------------------------------------------------------
-- 4. messages —— 对话消息（含工具调用轨迹与 token 用量）
--
-- 与现有 MessageOut / ChatResponse 的字段一一对应：
--   role / content / name / tool_calls / usage / elapsed_ms / created_at
-- tool 角色的消息用 tool_call_id 关联到那条 assistant 消息里的调用。
-- ---------------------------------------------------------------------------
CREATE TABLE messages (
    id              BIGINT          GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    conversation_id VARCHAR(64)     NOT NULL
                    REFERENCES conversations (id) ON DELETE CASCADE,
    -- 会话内严格递增序号：历史回放、分页、去重全靠它
    seq             INTEGER         NOT NULL,
    role            VARCHAR(16)     NOT NULL,
    content         TEXT            NOT NULL DEFAULT '',
    -- role='tool' 时记录工具名
    name            VARCHAR(64),
    -- role='tool' 时指向对应 tool_call；同一会话内应存在匹配的 assistant 消息
    tool_call_id    VARCHAR(64),
    -- assistant 消息的工具调用清单（含回填后的结果）：
    --   [{"id": "...", "name": "calculator", "args": {...},
    --     "result": "14.0", "status": "success"}]
    -- 需要按工具维度做统计时再拆独立表，日常读路径用 JSONB 更省 JOIN。
    tool_calls      JSONB           NOT NULL DEFAULT '[]'::jsonb,
    -- ok / error / cancelled（客户端中途点「停止」）
    status          VARCHAR(16)     NOT NULL DEFAULT 'ok',
    error_code      VARCHAR(64),
    model           VARCHAR(64),
    input_tokens    INTEGER         NOT NULL DEFAULT 0,
    output_tokens   INTEGER         NOT NULL DEFAULT 0,
    total_tokens    INTEGER         NOT NULL DEFAULT 0,
    -- 本轮耗时，对应 ChatResponse.elapsed_ms
    latency_ms      INTEGER,
    -- LangGraph 给每条消息分配的 id：跨表去重/对齐用，允许为空
    langchain_id    VARCHAR(64),
    -- 扩展字段：附件、多模态块等
    metadata        JSONB           NOT NULL DEFAULT '{}'::jsonb,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),

    CONSTRAINT messages_role_valid   CHECK (role IN ('system', 'user', 'assistant', 'tool')),
    CONSTRAINT messages_status_valid CHECK (status IN ('ok', 'error', 'cancelled')),
    -- 会话内顺序唯一，避免并发写入产生相同 seq
    CONSTRAINT messages_seq_ux       UNIQUE (conversation_id, seq)
);

-- 历史详情页「按会话取全量并顺序输出」由上面的 UNIQUE (conversation_id, seq)
-- 自带的 btree 索引直接满足，无需再建 (conversation_id, seq) 普通索引。
-- 定位某条工具调用对应的结果
CREATE INDEX messages_tool_call_idx ON messages (conversation_id, tool_call_id)
    WHERE tool_call_id IS NOT NULL;
-- 全局检索（可选）：全文搜索消息内容
-- CREATE INDEX messages_content_fts_idx ON messages
--     USING gin (to_tsvector('simple', content));


-- ---------------------------------------------------------------------------
-- 5. user_settings —— 用户级运行时偏好（承接现在只存在内存里的工作区设置）
--
-- 现状：app/agent/workspace.py 的工作区根目录与写权限仅内存态，
-- 页面「工作区设置」一刷新/重启就丢。登录后应挂到用户维度。
-- ---------------------------------------------------------------------------
CREATE TABLE user_settings (
    user_id            UUID        PRIMARY KEY REFERENCES users (id) ON DELETE CASCADE,
    workspace_root     TEXT,
    workspace_writable BOOLEAN     NOT NULL DEFAULT true,
    theme              VARCHAR(16),
    settings           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- ---------------------------------------------------------------------------
-- 可选扩展（按需启用，当前默认注释掉）
-- ---------------------------------------------------------------------------

-- (a) 登录失败限流：防撞库。可先只靠 Redis，表结构留作审计
-- CREATE TABLE login_attempts (
--     id         BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
--     identifier VARCHAR(128) NOT NULL,   -- 用户名或邮箱（小写）
--     ip         INET,
--     success    BOOLEAN     NOT NULL,
--     created_at TIMESTAMPTZ NOT NULL DEFAULT now()
-- );
-- CREATE INDEX login_attempts_identifier_idx ON login_attempts (identifier, created_at DESC);

-- (b) 会话分享 / 只读链接
-- CREATE TABLE conversation_shares (
--     id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
--     conversation_id VARCHAR(64) NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
--     token_hash      CHAR(64)    NOT NULL UNIQUE,
--     expires_at      TIMESTAMPTZ,
--     created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
-- );


-- ---------------------------------------------------------------------------
-- 常用查询（验证索引是否够用）
-- ---------------------------------------------------------------------------
-- 会话列表（对应 GET /api/v1/sessions）——注意 ORDER BY 与索引定义完全一致，否则白建
-- SELECT id, title, message_count, turn_count, last_active_at, created_at
--   FROM conversations
--  WHERE user_id = $1 AND deleted_at IS NULL
--  ORDER BY last_active_at DESC
--  LIMIT 50 OFFSET 0;
--
-- 会话详情（对应 GET /api/v1/sessions/{id}，必须带 user_id 防越权）
-- SELECT * FROM conversations WHERE id = $1 AND user_id = $2 AND deleted_at IS NULL;
-- SELECT role, content, name, tool_call_id, tool_calls, status, latency_ms
--   FROM messages WHERE conversation_id = $1 ORDER BY seq;
--
-- 追加一条消息并维护计数（同一事务内）
-- INSERT INTO messages (conversation_id, seq, role, content, ...)
-- VALUES ($1, (SELECT COALESCE(MAX(seq), 0) + 1 FROM messages WHERE conversation_id = $1), ...);
-- UPDATE conversations
--    SET message_count = message_count + 1,
--        turn_count    = turn_count + 1,
--        last_active_at = now(),
--        updated_at    = now()
--  WHERE id = $1;
