# LangChain Agent API

基于 **LangChain 1.x + LangGraph + FastAPI** 的智能体（Agent）后端脚手架。
自带**登录鉴权 + 对话历史持久化（PostgreSQL）**，前端是零构建的静态页面，可直接部署。

## 技术栈

| 组件 | 版本要求 | 说明 |
| --- | --- | --- |
| Python | >= 3.11 | 使用 `zoneinfo`、`X \| None` 等新语法 |
| langchain | >= 1.0,<2 | `langchain.agents.create_agent` 构建 Agent |
| langgraph | >= 1.0,<2 | 底层状态图 + checkpointer 会话记忆 |
| langchain-openai | >= 1.0,<2 | OpenAI 兼容协议客户端（DeepSeek / Qwen / vLLM / Ollama 通用） |
| langchain-deepseek | >= 1.0,<1.1 | DeepSeek 官方适配（可选，`AGENT_PROVIDER=deepseek` 时需要） |
| fastapi + uvicorn | >= 0.115 / >= 0.30 | HTTP 层与 ASGI 服务器 |
| pydantic-settings | >= 2.3 | 环境变量/`.env` 配置 |
| psycopg2-binary | >= 2.9 | PostgreSQL 驱动（登录态与对话历史落库） |

> 密码哈希与 JWT 全部使用标准库实现（另有 argon2/bcrypt 时自动优先使用），
> **没有引入 passlib / PyJWT 等额外依赖**，详见 `app/core/security.py`。

## 特性

- **登录鉴权开箱可用**：注册 / 登录 / 刷新 / 登出 / 改密 / 当前用户，JWT access token +
  服务端可撤销登录态（登出、改密即刻失效），密码用 argon2id/bcrypt/PBKDF2 哈希
- **对话历史落库**：会话与消息（含工具调用轨迹、token 用量）写入 PostgreSQL，
  **服务重启后历史仍在**；LangGraph 上下文丢失时会自动用数据库历史重建，可继续对话
- **按用户隔离**：会话查询/详情/删除/继续对话都带 `user_id` 条件，
  别人的会话与不存在的会话返回同样的 404
- **开箱即用的 Agent**：模型、工具、系统提示词、记忆一次组装完成（`create_agent` + `InMemorySaver`）
- **自带聊天页面**：`/ui/` 提供类 DeepSeek 的对话界面（登录、流式打字、Markdown、工具调用卡片、历史会话列表），纯静态零构建
- **同步 + 流式双接口**：`POST /chat` 一次返回完整结果；`POST /chat/stream` 以 SSE 推送 token / 工具调用 / 最终结果
- **工具化扩展**：内置 14 个工具 —— 计算器/时间/知识库 + 6 个工作区文件工具 + 3 个联网工具 + 2 个终端工具，新增工具只需一个 `@tool` 函数
- **工作区在页面里配**：Agent 能操作哪个目录、是否允许写，由前端「工作区」面板运行时设置，**每个账号各自独立**
- **模型可插拔**：OpenAI 兼容协议（默认）/ Anthropic / 离线 mock，改 3 个环境变量即可切换
- **离线联调**：`AGENT_PROVIDER=mock` 时无需任何 API Key；未配数据库时自动回退内存存储
- **工程化细节**：统一错误响应、结构化日志、CORS、健康/就绪探针、工具调用轮数上限、会话数量上限、pytest 用例（含 PostgreSQL 集成测试）

## 目录结构

```
.
├── app/
│   ├── main.py                 # FastAPI 入口（create_app / lifespan / run）
│   ├── __main__.py             # python -m app
│   ├── core/
│   │   ├── config.py           # Settings（AGENT_* 环境变量）
│   │   ├── security.py         # 密码哈希（argon2/bcrypt/pbkdf2）+ HS256 JWT
│   │   ├── logging.py          # 统一日志
│   │   └── exceptions.py       # AppError 体系（401/403/404/409/503）+ 统一错误响应
│   ├── db/                     # —— 数据访问层 ——
│   │   ├── __init__.py         # build_repositories：按配置装配 PostgreSQL / 内存仓储
│   │   ├── database.py         # psycopg2 连接池 + 线程池执行封装（Database.run）
│   │   ├── base.py             # 仓储 Protocol 与 Repositories 容器
│   │   ├── models.py           # UserRecord / ConversationRecord / MessageRecord ...
│   │   ├── postgres.py         # 手写 SQL 实现（与 docs/db/schema.postgres.sql 对应）
│   │   └── memory.py           # 内存实现（单元测试 / 未配数据库时的兜底）
│   ├── agent/                  # —— LangChain 相关代码全部集中在这一层 ——
│   │   ├── factory.py          # build_agent_runtime：模型+工具+提示词+checkpointer -> LangGraph
│   │   ├── middleware.py       # 修复"孤立 tool_calls"+ 注入工作区信息的中间件
│   │   ├── workspace.py        # 工作区运行时配置（根目录/写权限）+ 路径守卫
│   │   ├── llm.py              # 模型工厂（openai / anthropic / mock）
│   │   ├── mock_model.py       # 离线假模型（支持 /calc /time /search 指令）
│   │   ├── prompt.py           # 默认系统提示词
│   │   └── tools/              # 工具注册表 + 内置工具
│   ├── api/
│   │   ├── deps.py             # 依赖注入（Settings / Repositories / Auth / Service / 当前用户）
│   │   └── v1/
│   │       ├── router.py       # 路由聚合
│   │       └── endpoints/      # auth / health / agent / chat / sessions / workspace / meta
│   ├── schemas/                # Pydantic 请求/响应模型（含 auth.py）
│   ├── services/
│   │   ├── auth_service.py     # 注册/登录/刷新/登出/改密/鉴权
│   │   ├── conversation_service.py  # 会话与消息的读写、历史回放
│   │   ├── agent_service.py    # 同步/流式调用编排 + 落库 + 上下文重建
│   │   └── messages.py         # LangChain 消息 <-> API 模型 <-> 数据库记录
│   └── utils/sse.py            # SSE 序列化
├── docs/db/                    # 建表脚本（PostgreSQL / MySQL / SQLite）
├── scripts/e2e_smoke.py        # 对真实服务跑一遍登录+对话+历史的冒烟脚本
├── web/                        # 聊天页面（纯静态，无构建步骤）
│   ├── index.html              # 页面结构（登录框 + 侧栏会话列表 + 消息区 + 输入框）
│   ├── styles.css              # 深/浅色主题样式
│   ├── auth.js                 # 登录态：token 存储、自动刷新、统一请求封装
│   ├── markdown.js             # Markdown 渲染器（先转义再渲染，防 XSS）
│   └── app.js                  # 交互逻辑：SSE 流式解析、会话切换、工具卡片
├── tests/                      # pytest 用例（默认全程离线，PostgreSQL 用例可选启用）
├── pyproject.toml              # 可安装包配置（uv / pip install -e .）
├── requirements.txt            # conda / pip 依赖清单（版本已锁定）
├── .env.example
└── README.md
```


## 快速开始

### 方式一：conda 环境（推荐用 requirements.txt）

```powershell
# 1) 创建并激活 conda 环境
conda create -n langchain-agent python=3.12 -y
conda activate langchain-agent

# 2) 安装依赖（版本已锁定为实测通过的组合）
pip install -r requirements.txt

# 3) 准备配置
Copy-Item .env.example .env
#   编辑 .env：填 AGENT_API_KEY、AGENT_DATABASE_URL、AGENT_JWT_SECRET
#   （不填 Key 也能用 mock 模式跑通；不填数据库则回退内存存储）

# 4) 建表（只需一次）
psql -U postgres -d agent_chat -f docs/db/schema.postgres.sql

# 5) 启动
uvicorn app.main:app --reload --port 8000
# 或
python -m app.main
```

> 想跑测试再装开发依赖：`pip install pytest==8.3.5`（或 `pip install -e ".[dev]"`）。
> 想接入 Claude：把 `requirements.txt` 里 `langchain-anthropic` 那行取消注释后重装。

### 方式二：venv / uv（把本目录当可安装包）

```powershell
# uv
uv venv
.\.venv\Scripts\Activate.ps1
uv pip install -e ".[dev]"

# 或 venv + pip
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 准备配置
Copy-Item .env.example .env

# 启动
uvicorn app.main:app --reload --port 8000
# 或
python -m app.main
```

启动后：

- **聊天页面**：<http://127.0.0.1:8000/>（自动跳转到 `/ui/`，界面见下节）
- Swagger 文档：<http://127.0.0.1:8000/docs>
- ReDoc：<http://127.0.0.1:8000/redoc>
- OpenAPI：<http://127.0.0.1:8000/openapi.json>
- 接口总览：<http://127.0.0.1:8000/api/v1/info>

> 没有 API Key 时服务仍可启动，但 `/api/v1/health` 会返回 `degraded`、`/chat` 返回 503
> 并提示缺少 `AGENT_API_KEY`。想先跑通链路就把 `AGENT_PROVIDER` 设为 `mock`。

## 登录与对话历史（PostgreSQL）

### 它是怎么工作的

```
浏览器                       FastAPI                        PostgreSQL
  │  ① POST /auth/register    │                                 │
  ├──────────────────────────►│  写 users / user_sessions ──────►│
  │  ② access + refresh token │                                 │
  │◄──────────────────────────┤                                 │
  │  ③ POST /chat/stream      │                                 │
  │    Authorization: Bearer  │  ④ 先落库 user 消息 ────────────►│
  ├──────────────────────────►│  ⑤ 调 LangGraph（模型+工具）      │
  │  ⑥ SSE token/tool/final   │  ⑦ 落库 assistant+tool 消息 ────►│
  │◄──────────────────────────┤                                 │
  │  ⑧ GET /sessions          │  按 user_id 查询 ───────────────►│
  ├──────────────────────────►│                                 │
  │◄──────────────────────────┤                                 │
```

要点：

1. **数据库是对话历史的唯一事实来源**：`conversations`（会话元数据）+ `messages`
   （每条消息，含 `tool_calls` JSONB、token 用量、耗时、状态）；
2. `conversations.id` 直接复用 `session_id`（`sess_xxx`）作为 LangGraph 的 `thread_id`，
   所以 API 契约没有变化；
3. LangGraph 的 `InMemorySaver` 只是"当前进程的上下文缓存"。进程重启后缓存为空，
   下次请求会用数据库里的历史重新拼出模型输入 —— **重启后可以接着聊**；
4. 消息在会话内按 `seq` 严格递增（事务内分配），历史回放顺序稳定。

### 建表

```bash
psql -U postgres -d agent_chat -f docs/db/schema.postgres.sql
```

脚本要求 PostgreSQL 13+，会创建 `pgcrypto`、`citext` 扩展与 5 张表：
`users` / `user_sessions` / `conversations` / `messages` / `user_settings`。
表设计、索引与查询示例都在脚本注释里（含"为什么要这么建索引"的实测说明）。

### 认证接口

```bash
API=http://127.0.0.1:8000/api/v1

# 注册（成功后直接返回登录态）
curl -X POST $API/auth/register -H "Content-Type: application/json" \
  -d '{"username":"alice","password":"s3cret-pass","email":"alice@example.com"}'

# 登录（用户名或邮箱，大小写不敏感）
curl -X POST $API/auth/login -H "Content-Type: application/json" \
  -d '{"identifier":"alice","password":"s3cret-pass"}'

# 带着 access token 使用业务接口
curl $API/sessions -H "Authorization: Bearer <access_token>"

# access token 过期后用 refresh token 换新的（refresh token 会轮换）
curl -X POST $API/auth/refresh -H "Content-Type: application/json" \
  -d '{"refresh_token":"<refresh_token>"}'

# 登出（立刻吊销该登录态） / 改密（旧 token 全部失效）
curl -X POST $API/auth/logout -H "Authorization: Bearer <access_token>"
curl -X POST $API/auth/password -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"current_password":"s3cret-pass","new_password":"N3w-secret-pass"}'
```

响应示例（注册/登录/刷新都一样）：

```json
{
  "user": { "id": "0f0c...", "username": "alice", "email": "alice@example.com",
            "display_name": "alice", "role": "user", "status": 1,
            "created_at": "2026-01-01T00:00:00Z", "last_login_at": "2026-01-01T00:00:00Z" },
  "tokens": { "access_token": "eyJhbGciOi...", "refresh_token": "ZJ4k...",
              "token_type": "bearer", "expires_in": 3600, "refresh_expires_in": 2592000 }
}
```

### 安全设计（评审重点）

| 关注点 | 做法 |
| --- | --- |
| 密码存储 | argon2id（有 argon2-cffi 时）/ bcrypt / PBKDF2-SHA256(600k)，哈希串带算法前缀，登录成功可自动升级 |
| 登录枚举 | 账号不存在时也跑一次哈希校验；失败统一返回 `invalid_credentials`，不区分"用户不存在/密码错" |
| 登录态撤销 | access token 带 `sid`，校验时查 `user_sessions`；登出/禁用/改密即刻失效（JWT 不再是"撤不掉"的） |
| refresh token | 只存 SHA-256（`token_hash`），每次刷新轮换，旧的立即作废 |
| 改密 | 刷新 `password_changed_at`，签发时间早于它的 token 一律拒绝（留 1 秒时钟余量） |
| 越权 | 会话相关查询全部带 `user_id`；别人的会话与不存在同样返回 404 |
| 弱口令 | 长度下限 + 常见弱口令黑名单 + 不能等于用户名 + 不能全同字符 |
| 令牌算法 | 只认服务端固定的 HS256，忽略 token header 里的 `alg`（防 `alg=none`），签名用常数时间比较 |
| 日志 | 账号在日志里做脱敏；密码、token 明文不落日志 |

> ⚠️ 前端把 token 存在 `localStorage`（实现简单、可跨标签页）。若你的威胁模型包含 XSS，
> 应改为 HttpOnly Cookie + CSRF Token —— 后端已经把"服务端可撤销"这一层做好了，切换成本不高。

### 配置项

```ini
AGENT_DATABASE_URL=postgresql://postgres:root@127.0.0.1:5432/agent_chat
AGENT_JWT_SECRET=<32 字节以上的随机串>        # 留空则每次启动随机生成（重启后登录态失效）
AGENT_AUTH_REQUIRED=true                      # false = 本地调试，不校验登录
AGENT_AUTH_ALLOW_REGISTER=true                # false = 关闭自助注册
AGENT_AUTH_ADMIN_USERNAMES=alice,bob          # 这些用户名注册时直接成为管理员
AGENT_ACCESS_TOKEN_TTL_MINUTES=60
AGENT_REFRESH_TOKEN_TTL_DAYS=30
```

**没有数据库会怎样？** 服务仍能启动：自动回退到内存仓储，登录、对话、历史查看都能用，
但**重启即丢**，`/api/v1/health` 的 `components.storage` 会显示 `degraded` 并说明原因。

## 聊天页面（/ui/）

打开 <http://127.0.0.1:8000/> 就会跳到聊天页面，界面参考 DeepSeek：左侧会话列表、右侧消息流、底部输入框。

**功能**

- **登录 / 注册**：未登录时整页显示登录卡片（登录、注册切换）；token 过期自动刷新，
  刷新失败自动回到登录页
- 流式打字：助手回复逐字出现，带光标；生成中可点「停止」中断（前端 `AbortController` → 后端取消本次调用）
- **Markdown 渲染**：标题、段落软换行、加粗/斜体/删除线、行内代码、链接与自动链接、图片、
  引用块、分割线、有序/无序列表（含子列表、任务清单 ☑/☐）、**GFM 表格（支持左/中/右对齐）**、
  围栏代码块（语言标签 + 一键复制）；流式输出过程中即使代码块还没闭合也能正常渲染
- 工具调用可视化：每次工具调用显示成可折叠卡片（工具名 + 入参 + 返回内容 + 状态）
- **历史对话来自服务端**：打开页面自动加载该账号的会话列表（按最近活跃排序），
  支持标题搜索、点击回看历史消息、删除单个会话、一键清空；刷新/换浏览器都能看到
- 状态提示：侧栏显示登录用户、后端连接状态、当前模型、工具数量、当前工作区；模型未就绪时顶部弹出提示条
- 工作区面板：右上角「工作区」可修改 Agent 能操作的目录、开关写权限、预览目录结构（每个账号各自独立，只影响自己）
- 深色模式：跟随系统 `prefers-color-scheme`；窄屏自动把侧栏变成抽屉

**界面动作与接口的对应关系**

| 页面动作 | 调用的接口 |
| --- | --- |
| 登录 / 注册 | `POST /api/v1/auth/login`、`POST /api/v1/auth/register` |
| 退出登录 | `POST /api/v1/auth/logout` |
| 打开页面（恢复登录态） | `GET /api/v1/auth/me`（失败时 `POST /api/v1/auth/refresh`） |
| 加载历史会话列表 | `GET /api/v1/sessions?limit=100[&keyword=…]` |
| 点击历史会话 | `GET /api/v1/sessions/{session_id}` |
| 删除会话 / 清空全部 | `DELETE /api/v1/sessions/{session_id}`、`DELETE /api/v1/sessions` |
| 发送消息 / 停止 | `POST /api/v1/chat/stream`（SSE：`session`→`token`→`tool_call`→`tool_result`→`final`→`done`） |
| 人工确认（approve / edit / reject） | 流里收到 `interrupt` 事件后点击按钮 → `POST /api/v1/chat/resume`（SSE，事件同上） |
| 加载页面 | `GET /api/v1/health`、`GET /api/v1/agent/info`、`GET /api/v1/workspace` |

**实现说明**

- 纯静态文件（`web/index.html` + `styles.css` + `app.js` + `auth.js` + `markdown.js`），由 FastAPI `StaticFiles` 挂在 `/ui`，**不需要 Node、不需要打包、不依赖 CDN**，内网可用
- `web/auth.js` 统一封装请求：自动带 `Authorization: Bearer`，401 时用 refresh token 自动续期一次
  （并发请求同时 401 只会刷新一次），仍失败则弹出登录框
- SSE 走 `fetch` + `ReadableStream` 手工解析帧（浏览器 `EventSource` 只支持 GET，而后端是 POST）
- **鉴权失败不会进 SSE**：401/404 在开流之前就返回普通 JSON，前端按 `Content-Type` 区分处理
- Markdown 渲染器先做 HTML 转义再拼标签，模型输出里的 `<script>`、`<img onerror>` 只会显示成文本

**想改造界面的位置**

| 想改 | 改哪里 |
| --- | --- |
| 配色 / 圆角 / 字号 | `web/styles.css` 顶部的 `:root` CSS 变量（同时有深色主题变量） |
| 空状态欢迎语与示例问题 | `web/index.html` 里的 `#empty-state` 与 `.suggestion` 的 `data-prompt` |
| 登录卡片文案 | `web/index.html` 的 `#auth-screen` 与 `web/app.js` 的 `setAuthMode` |
| 页面标题、顶栏链接 | `web/index.html` 的 `<title>` 与 `.topbar` |
| 接口地址（前后端分离部署时） | `web/auth.js` 顶部的 `var API = "/api/v1"` |


> 前后端分离部署时，把 `API` 改成 `http://后端地址/api/v1`，并确保后端 `AGENT_CORS_ORIGINS` 包含前端地址。

## 工作区与文件工具

Agent 可以读、写、查、改、删工作区里的文件，但**工作区根目录不写在配置文件里**，
而是在页面右上角「工作区」里设置（或调用 `PUT /api/v1/workspace`）。进程重启后回到默认值
（项目根目录）。

> 工作区**按用户隔离**：每个登录用户都有自己的一份根目录与写权限，可以随时自行修改，
> 改动只影响自己（服务端的 Agent 图是共享的，但运行时通过协程上下文绑定当前用户，
> 工具与提示词中间件取到的是该用户自己的工作区）。

**工具清单**

| 工具 | 作用 | 需要写权限 |
| --- | --- | --- |
| `list_workspace_files` | 列目录（支持通配符、递归） | 否 |
| `read_workspace_file` | 读文本文件，带行号、分页 | 否 |
| `search_workspace` | 按内容检索，返回 `文件:行号:内容` | 否 |
| `write_workspace_file` | 新建 / 覆盖 / 追加 | 是 |
| `edit_workspace_file` | 精确字符串替换（改代码首选） | 是 |
| `delete_workspace_file` | 删除单个文件 | 是 |
| `web_search` | 联网搜索（Bing 抓取或 Tavily） | 否 |
| `fetch_web_page` | 抓取网页正文 | 否 |
| `run_command` | 在工作区执行终端命令（黑名单/白名单 + 超时 + 输出上限） | **是** |
| `run_python` | 用当前解释器跑一小段 Python | **是** |

**安全约束（都写在 `app/agent/workspace.py` 里）**

- 所有路径 `resolve()` 后必须落在工作区内，`../` 逃逸、绝对路径越界一律拒绝；
- `.env*`、`*.pem`、`*.key` 等敏感文件禁止读写（也不出现在列表/检索结果里）；
- `.git`、`.venv`、`node_modules`、`__pycache__` 等目录不参与列举与检索；
- 单文件上限 400 KB，读取默认最多 400 行、检索最多 40 条；
- 写/删受「允许写/删文件」开关控制，关掉后工具返回「只读模式」提示而不是报错中断对话。

**怎么用（页面）**

1. 打开 <http://127.0.0.1:8000/>，点右上角「工作区」；
2. 填入绝对路径（例如 `D:\projects\demo`），点「校验路径」看是否合法；
3. 展开「预览目录结构」确认没选错目录；
4. 按需勾选「允许写 / 删文件」，点「保存」——立即生效，当前会话也可以继续用。

**怎么用（接口）**

```bash
curl http://127.0.0.1:8000/api/v1/workspace                     # 查看当前设置
curl -X POST http://127.0.0.1:8000/api/v1/workspace/validate \
     -H "Content-Type: application/json" -d '{"root": "D:/projects/demo"}'
curl -X PUT http://127.0.0.1:8000/api/v1/workspace \
     -H "Content-Type: application/json" \
     -d '{"root": "D:/projects/demo", "allow_writes": true}'
curl http://127.0.0.1:8000/api/v1/workspace/tree?depth=2          # 预览目录
```

**对话里的效果**

```
用户：看一下 hello.py 里有什么，然后加一个 farewell 函数
AI：  ▸ list_workspace_files {path: "."}          已完成
      ▸ read_workspace_file {path: "hello.py"}    已完成
      ▸ edit_workspace_file {path: "hello.py"}    已完成
      ...
```

> 每轮请求都会把「当前工作区根目录 + 写权限」注入系统提示词（`WorkspaceContextMiddleware`），
> 所以改完立刻生效，不需要重启服务，也不用重建 Agent。

## 联网检索

`web_search` 默认抓 Bing 结果页（**不需要任何 Key**），配置 `AGENT_SEARCH_API_KEY` 后自动
改用 Tavily；`AGENT_SEARCH_PROVIDER` 可强制指定 `auto | bing | duckduckgo | tavily`。

```ini
AGENT_SEARCH_PROVIDER=auto
AGENT_SEARCH_API_KEY=            # 可选：填了就优先用 tavily
AGENT_WEB_TIMEOUT=25
AGENT_WEB_MAX_CHARS=4000         # 单次抓网页回传给模型的最大字符数
```

同样有防护：只允许 `http/https`，拒绝 `localhost`、内网与云元数据地址（`169.254.169.254` 等），
脚本/样式会被剥离后再交给模型。

## 终端执行（高风险）

`run_command` 让模型能在工作区里跑终端命令（跑测试、git 操作、装依赖、调用 CLI），
`run_python` 直接执行一小段 Python（省掉引号转义）。这是本项目权限最大的能力，护栏：

| 闸门 | 说明 |
| --- | --- |
| 写权限开关 | 工作区面板关掉「允许写 / 删文件」后，两个终端工具直接拒绝执行 |
| 工作目录 | `cwd` 必须落在工作区内的已有目录，越界拒绝 |
| 危险命令黑名单 | `rm -rf /`、`mkfs`、`dd if=`、`format C:`、`shutdown`/`reboot`、`diskpart`、`bcdedit`、`reg add/delete`、`net user`、`takeown`、`chmod 777 /`、fork bomb、`curl ... \| sh`、`iwr ... \| iex`、`powershell -enc` 等一律拒绝 |
| 白名单模式 | 设置 `AGENT_SHELL_ALLOWLIST=git,python,pytest,dir,ls,cat` 后只允许命令首词在白名单里的命令 |
| 超时 | 默认 30 秒（`AGENT_SHELL_TIMEOUT`），单次调用最多 120 秒，超时直接杀掉 |
| 输出上限 | 默认 8000 字符（`AGENT_SHELL_MAX_OUTPUT`），超出保留头尾并标注截断 |
| 环境变量脱敏 | 子进程不继承 `*API_KEY*` / `*TOKEN*` / `*SECRET*` / `*PASSWORD*` / `*CREDENTIAL*` 变量，防止随口一条命令把密钥打印或外传 |
| 审计 | 每条命令以 WARNING 级别写入日志 |

**Windows 上的 shell 选择**：默认 `auto` → 优先 `pwsh`，其次 `powershell`（5.1），最后 `cmd`。
PowerShell 下 `dir`/`ls`/`cat`/`grep`/`python` 都能直接用；多命令串联用 `;`（Windows PowerShell 5.1
不支持 `&&`），需要 cmd 语法时传 `shell="cmd"`；带空格的路径在 PowerShell 里用 `& "C:\path\app.exe" args`。

> ⚠️ **黑名单只能降低误伤，不等于沙箱隔离**：命令仍以你的用户身份、在整机范围内执行，
> 也能访问网络。跑不可信代码请用容器/虚拟机；不放心时把 `run_command` / `run_python`
> 从 `AGENT_ENABLED_TOOLS` 里删掉，或在页面里关掉写权限（终端会一起被禁用）。

**对话里的用法**

```
用户：帮我把测试跑一下，看看有没有失败
AI：  ▸ run_command {"command": "python -m pytest -q"}   已完成
      ▸ run_command {"command": "dir"}                    已完成
```

## 配置项

全部配置均可通过 `AGENT_` 前缀的环境变量或 `.env` 覆盖（`app/core/config.py`）。

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `AGENT_APP_NAME` / `AGENT_ENV` / `AGENT_VERSION` | LangChain Agent API / dev / 0.1.0 | 服务元信息 |
| `AGENT_HOST` / `AGENT_PORT` | 127.0.0.1 / 8000 | `python -m app.main` 使用 |
| `AGENT_API_PREFIX` | `/api/v1` | 路由前缀 |
| `AGENT_CORS_ORIGINS` | `*` | 逗号分隔的允许来源 |
| `AGENT_LOG_LEVEL` | `INFO` | 日志级别 |
| `AGENT_PROVIDER` | `openai` | `openai` \| `anthropic` \| `deepseek` \| `mock` |
| `AGENT_MODEL` | `gpt-4o-mini` | 模型名 |
| `AGENT_API_KEY` | 空 | 上游 API Key（`mock` 模式可不填） |
| `AGENT_BASE_URL` | 空 | OpenAI 兼容服务地址 |
| `AGENT_TEMPERATURE` | `0.3` | 采样温度 |
| `AGENT_MAX_TOKENS` | `2048` | 生成上限 |
| `AGENT_REQUEST_TIMEOUT` / `AGENT_MAX_RETRIES` | 60 / 2 | 超时与重试 |
| `AGENT_AGENT_NAME` | `assistant` | Agent 名称（LangGraph 图名） |
| `AGENT_SYSTEM_PROMPT` | 空 | 留空使用 `app/agent/prompt.py` 默认提示词 |
| `AGENT_ENABLED_TOOLS` | `calculator,get_current_time,search_knowledge_base` | 逗号分隔；留空=注册全部 |
| `AGENT_MAX_TOOL_ROUNDS` | `8` | 单轮对话内工具调用上限（防死循环） |
| `AGENT_MAX_SESSIONS` | `1000` | 内存中会话数量上限，超出淘汰最久未活跃 |
| `AGENT_SEARCH_PROVIDER` | `auto` | `auto` \| `bing` \| `duckduckgo` \| `tavily` |
| `AGENT_SEARCH_API_KEY` | 空 | Tavily 等搜索服务 Key；留空则用免 Key 的 Bing 抓取 |
| `AGENT_WEB_TIMEOUT` / `AGENT_WEB_MAX_CHARS` | 25 / 4000 | 抓网页超时（秒）与回传字符上限 |
| `AGENT_SHELL_TIMEOUT` | `30` | 终端命令默认超时（秒），单次可覆盖，上限 120 |
| `AGENT_SHELL_MAX_OUTPUT` | `8000` | 终端输出回传模型的最大字符数 |
| `AGENT_SHELL_ALLOWLIST` | 空 | 命令白名单（逗号分隔）；留空为黑名单模式 |
| `AGENT_DATABASE_URL` | 空 | PostgreSQL 连接串；留空回退内存存储（重启丢历史） |
| `AGENT_DB_POOL_MIN` / `AGENT_DB_POOL_MAX` | 1 / 10 | 连接池最小/最大连接数 |
| `AGENT_DB_CONNECT_TIMEOUT` | 5 | 建连超时（秒） |
| `AGENT_AUTH_REQUIRED` | `true` | 是否强制登录（`false` 仅用于本地调试） |
| `AGENT_AUTH_ALLOW_REGISTER` | `true` | 是否开放自助注册 |
| `AGENT_AUTH_ADMIN_USERNAMES` | 空 | 注册时直接授予 admin 的用户名（逗号分隔） |
| `AGENT_JWT_SECRET` | 空 | JWT 签名密钥；留空则进程级随机（重启后登录态失效） |
| `AGENT_ACCESS_TOKEN_TTL_MINUTES` | `60` | access token 有效期（分钟） |
| `AGENT_REFRESH_TOKEN_TTL_DAYS` | `30` | refresh token 有效期（天） |
| `AGENT_PASSWORD_MIN_LENGTH` / `AGENT_PASSWORD_MAX_LENGTH` | 8 / 128 | 注册密码长度限制 |

### 常见模型接入示例

```ini
# DeepSeek —— 方式 A：官方适配（requirements.txt 已含 langchain-deepseek 1.0.1）
AGENT_PROVIDER=deepseek
AGENT_MODEL=deepseek-chat
AGENT_API_KEY=sk-xxxx

# DeepSeek —— 方式 B：OpenAI 兼容协议（不需要 langchain-deepseek）
AGENT_PROVIDER=openai
AGENT_MODEL=deepseek-chat
AGENT_API_KEY=sk-xxxx
AGENT_BASE_URL=https://api.deepseek.com/v1

# 本地 Ollama
AGENT_PROVIDER=openai
AGENT_MODEL=qwen2.5:7b
AGENT_API_KEY=ollama
AGENT_BASE_URL=http://localhost:11434/v1

# Claude（需 pip install ".[anthropic]"）
AGENT_PROVIDER=anthropic
AGENT_MODEL=claude-sonnet-4-5
AGENT_API_KEY=sk-ant-xxxx

# 完全离线，仅用假模型联调
AGENT_PROVIDER=mock
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/` | 307 跳转到聊天页面 `/ui/` |
| GET | `/ui/` | 聊天页面（静态前端） |
| GET | `/api/v1/info` | 服务元信息与接口清单 |
| GET | `/api/v1/health` | 存活检查（含 agent / model / storage / auth 组件状态） |
| GET | `/api/v1/health/ready` | 就绪检查（未配置模型时 503） |
| GET | `/api/v1/auth/config` | 登录相关公开配置（是否开放注册、密码长度等） |
| POST | `/api/v1/auth/register` | 注册（成功后直接返回登录态） |
| POST | `/api/v1/auth/login` | 登录（用户名或邮箱） |
| POST | `/api/v1/auth/refresh` | 刷新登录态（refresh token 轮换） |
| POST | `/api/v1/auth/logout` | 登出（吊销当前登录态） |
| POST | `/api/v1/auth/logout-all` | 吊销该账号全部登录态 |
| POST | `/api/v1/auth/password` | 修改密码（旧 token 全部失效） |
| GET | `/api/v1/auth/me` | 当前登录用户 |
| GET | `/api/v1/agent/info` | 当前模型/提示词/工具配置（脱敏，需登录） |
| GET | `/api/v1/agent/tools` | 工具清单与参数 JSON Schema（需登录） |
| POST | `/api/v1/chat` | 同步对话（需登录） |
| POST | `/api/v1/chat/stream` | SSE 流式对话（需登录） |
| POST | `/api/v1/chat/resume` | 提交 approve / edit / reject 决策，继续被人工确认中断的那一轮（SSE，需登录） |
| POST | `/api/v1/sessions` | 创建会话 |
| GET | `/api/v1/sessions` | 会话列表（`limit`/`offset`/`keyword`，按最近活跃排序） |
| GET | `/api/v1/sessions/{session_id}` | 会话详情（含历史消息） |
| PATCH | `/api/v1/sessions/{session_id}` | 重命名会话 |
| DELETE | `/api/v1/sessions/{session_id}` | 删除会话（软删除） |
| DELETE | `/api/v1/sessions` | 清空当前用户的会话（软删除） |
| GET | `/api/v1/workspace` | 查看当前工作区（根目录 / 写权限） |
| PUT | `/api/v1/workspace` | 设置自己的工作区根目录与写权限（每个账号独立） |
| POST | `/api/v1/workspace/validate` | 校验候选目录是否可用 |
| GET | `/api/v1/workspace/tree` | 预览工作区目录结构 |

> 除 `/health*`、`/info`、`/auth/config|register|login|refresh` 外，
> 所有业务接口都需要 `Authorization: Bearer <access_token>`。


### 1. 同步对话

```bash
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "帮我算一下 (3+4)*2.5"}'
```

```json
{
  "session_id": "sess_2ac7def8fd584068",
  "reply": "计算结果为 17.5。",
  "messages": [
    { "role": "user", "content": "帮我算一下 (3+4)*2.5", "name": null, "tool_calls": [] },
    { "role": "assistant", "content": "", "name": "assistant",
      "tool_calls": [{ "id": "call_1", "name": "calculator",
                       "args": { "expression": "(3+4)*2.5" }, "result": null, "status": "success" }] },
    { "role": "tool", "content": "(3+4)*2.5 = 17.5", "name": "calculator", "tool_calls": [] },
    { "role": "assistant", "content": "计算结果为 17.5。", "name": "assistant", "tool_calls": [] }
  ],
  "tool_calls": [
    { "id": "call_1", "name": "calculator", "args": { "expression": "(3+4)*2.5" },
      "result": "(3+4)*2.5 = 17.5", "status": "success" }
  ],
  "usage": { "input_tokens": 312, "output_tokens": 48, "total_tokens": 360 },
  "elapsed_ms": 1834.2,
  "created_at": "2026-01-01T00:00:00Z"
}
```

多轮对话：把首次响应中的 `session_id` 带回请求体即可。

```bash
curl -X POST http://127.0.0.1:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"session_id": "sess_2ac7def8fd584068", "message": "再乘以 2"}'
```

### 2. 流式对话（SSE）

```bash
curl -N -X POST http://127.0.0.1:8000/api/v1/chat/stream \
  -H "Content-Type: application/json" \
  -d '{"message": "介绍一下 LangGraph 的记忆机制"}'
```

事件类型：

| 事件 | 载荷 | 说明 |
| --- | --- | --- |
| `session` | `{session_id, created_at}` | 首帧，分配/复用会话 |
| `token` | `{content, node}` | 模型增量文本 |
| `tool_call` | `{id, name, args}` | 模型决定调用工具 |
| `tool_result` | `{id, name, status, content}` | 工具返回结果 |
| `final` | `{reply, tool_calls, usage, elapsed_ms, session_id}` | 本轮最终结果（权威） |
| `error` | `{code, message}` | 模型侧异常（HTTP 已是 200） |
| `done` | `{session_id}` | 结束标记 |

浏览器端（`EventSource` 只支持 GET，故用 `fetch`）：

```js
const res = await fetch("http://127.0.0.1:8000/api/v1/chat/stream", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ message: "你好" }),
});
const reader = res.body.getReader();
const decoder = new TextDecoder();
let buffer = "";
while (true) {
  const { done, value } = await reader.read();
  if (done) break;
  buffer += decoder.decode(value, { stream: true });
  const frames = buffer.split("\n\n");
  buffer = frames.pop() ?? "";
  for (const frame of frames) {
    const event = /event: (.*)/.exec(frame)?.[1];
    const data = JSON.parse(/data: (.*)/.exec(frame)?.[1] ?? "{}");
    if (event === "token") process.stdout.write(data.content);
    if (event === "final") console.log("\n---\n", data.reply);
  }
}
```

### 3. 会话管理

```bash
H="Authorization: Bearer <access_token>"
curl "http://127.0.0.1:8000/api/v1/sessions?limit=20&keyword=需求" -H "$H"
curl http://127.0.0.1:8000/api/v1/sessions/sess_2ac7def8fd584068 -H "$H"   # 含完整历史消息
curl -X PATCH http://127.0.0.1:8000/api/v1/sessions/sess_2ac7def8fd584068 \
     -H "$H" -H "Content-Type: application/json" -d '{"title":"新标题"}'
curl -X DELETE http://127.0.0.1:8000/api/v1/sessions/sess_2ac7def8fd584068 -H "$H"
curl -X DELETE "http://127.0.0.1:8000/api/v1/sessions?limit=200" -H "$H"     # 清空全部
```

会话详情响应（`messages` 里的 assistant 行带完整工具轨迹与结果）：

```json
{
  "session_id": "sess_b62d800b7bb94fe6",
  "title": "帮我算一下 (3+4)*2.5",
  "model": "deepseek-chat",
  "created_at": "2026-01-01T00:00:00Z",
  "last_active_at": "2026-01-01T00:05:00Z",
  "turns": 1,
  "message_count": 4,
  "total_tokens": 360,
  "messages": [
    { "role": "user", "content": "帮我算一下 (3+4)*2.5", "name": null, "tool_calls": [] },
    { "role": "assistant", "content": "", "name": "assistant",
      "tool_calls": [{ "id": "call_1", "name": "calculator",
                       "args": { "expression": "(3+4)*2.5" },
                       "result": "(3+4)*2.5 = 17.5", "status": "success" }] },
    { "role": "tool", "content": "(3+4)*2.5 = 17.5", "name": "calculator", "tool_calls": [] },
    { "role": "assistant", "content": "计算结果为 17.5。", "name": "assistant", "tool_calls": [] }
  ]
}
```

### 4. 错误响应

所有错误统一为：

```json
{ "error": { "code": "agent_unavailable", "message": "……", "details": {} } }
```

| HTTP | code | 场景 |
| --- | --- | --- |
| 400 | `validation_error` | 业务校验失败（密码太短/太常见等） |
| 401 | `unauthorized` / `invalid_token` / `token_expired` / `session_revoked` / `token_stale` / `invalid_credentials` | 未登录、令牌无效/过期、登录态被吊销、改密后旧令牌、账号密码错误 |
| 403 | `forbidden` | 账号被禁用、非管理员执行特权操作 |
| 404 | `session_not_found` | 会话不存在**或不属于当前用户** |
| 409 | `conflict` | 用户名/邮箱/会话 ID 已被占用 |
| 422 | `unprocessable_entity`(FastAPI 默认) | 参数校验失败（空消息、非法 session_id） |
| 502 | `model_provider_error` | 上游模型调用失败 |
| 503 | `agent_unavailable` / `database_unavailable` | 模型未就绪 / 数据库连接池不可用 |

## 会话记忆是怎么实现的

1. `build_agent_runtime` 时给 `create_agent` 传入 `checkpointer=InMemorySaver()`；
2. 每次调用把 `{"configurable": {"thread_id": session_id}}` 作为 `config` 传给图；
3. LangGraph 自动按 `thread_id` 保存并回灌历史消息，因此第二轮请求自带上下文；
4. **进程内上下文为空时（服务重启、多副本），`AgentService` 会用数据库里该会话的历史
   （`ConversationService.history`）拼出本轮输入**，所以重启后接着聊不会"失忆"。

> 也就是说：PostgreSQL 是历史的事实来源，`InMemorySaver` 只是热缓存。
> 想彻底去掉回灌逻辑，可以把 checkpointer 换成 `langgraph.checkpoint.postgres.PostgresSaver`，
> 只需改 `app/agent/factory.py` 里 `memory` 的实现。


## 离线 mock 模式

`AGENT_PROVIDER=mock` 时使用 `app/agent/mock_model.py` 中的假模型，不发起任何真实请求：

| 输入 | 行为 |
| --- | --- |
| `/calc 2*(3+4)` | 发起 `calculator` 工具调用 |
| `/time Asia/Tokyo` | 发起 `get_current_time` 工具调用 |
| `/search LangGraph 记忆` | 发起 `search_knowledge_base` 工具调用 |
| `/ls [子目录]` | 发起 `list_workspace_files` 工具调用 |
| `/read <路径>` | 发起 `read_workspace_file` 工具调用 |
| `/write <路径> <内容>` | 发起 `write_workspace_file` 工具调用 |
| `/grep <关键词>` | 发起 `search_workspace` 工具调用 |
| `/web <关键词>` | 发起 `web_search` 工具调用（需要外网） |
| `/cmd <命令>` | 发起 `run_command` 工具调用（需要工作区可写） |
| `/py <代码>` | 发起 `run_python` 工具调用（需要工作区可写） |
| 其它文本 | 回显说明文字 |

该模式用于：本地联调、CI 测试、前端对接（不花 token）。

## 扩展指南

### 新增工具

```python
# app/agent/tools/weather.py
from langchain_core.tools import tool

@tool
def get_weather(city: str) -> str:
    """查询指定城市的实时天气。

    Args:
        city: 城市名，例如 "上海"。
    """
    return f"{city}：晴，26℃"
```

在 `app/agent/tools/__init__.py` 的 `ALL_TOOLS` 中登记，然后把名字加入
`AGENT_ENABLED_TOOLS`。docstring 会成为工具描述（模型据此决定是否调用），
参数类型注解会自动转成 JSON Schema。

### 新增模型服务

`app/agent/llm.py` 的 `build_chat_model` 中按 `settings.provider` 分支即可；
OpenAI 兼容协议的服务（DeepSeek、Qwen、vLLM、Ollama、One-API…）通常只需要改
`AGENT_BASE_URL` + `AGENT_MODEL`，不用写代码。

### 新增接口

在 `app/api/v1/endpoints/` 下加模块，`router.py` 里 `include_router` 即可；
业务逻辑放 `app/services/`，HTTP 层只做参数校验与协议转换。

## 测试

```powershell
pytest            # 默认用例全程 mock + 内存仓储，无需 API Key / 数据库 / 网络
node tests/frontend/markdown.test.js   # 可选：单独跑前端 Markdown 渲染用例（20 条）

# 可选：对真实 PostgreSQL 跑集成测试（会写入随机测试数据）
$env:AGENT_TEST_DATABASE_URL = "postgresql://postgres:root@127.0.0.1:5432/agent_chat"
pytest tests/test_postgres_storage.py -v

# 可选：对已启动的服务做一次端到端冒烟（登录→对话→历史→越权→刷新→登出）
python scripts/e2e_smoke.py http://127.0.0.1:8000
```

覆盖：健康检查（含 storage/auth 组件）、Agent 元信息、工具单测、同步对话、工具调用轨迹、
多轮记忆、会话 CRUD、SSE 事件序列、未配置模型时的 503 降级；
以及登录/注册/刷新/登出/改密、账号禁用、令牌撤销、**跨用户越权隔离**、
历史持久化与"进程重启后靠数据库恢复上下文"、PostgreSQL 仓储实现。

## 后续可扩展方向

- 持久化升级：把 `InMemorySaver` 换成 `PostgresSaver`（当前已是"DB 为事实来源 + 内存缓存"，
  切换只是去掉回灌逻辑）
- 配额与限流：按用户限流、按 token 计费、登录失败次数限制（`docs/db/schema.postgres.sql` 里
  已预留 `login_attempts` 表结构）
- 多租户工作区：已支持按用户隔离（`contextvars` 绑定当前用户），进一步可把配置持久化到
  `user_settings.workspace_root`，避免进程重启后回到默认目录
- RAG：把 `search_knowledge_base` 换成向量库 retriever（Chroma / pgvector）
- 可观测：LangSmith 追踪（`LANGSMITH_TRACING=true`）、Prometheus 指标
- 人机协同：`HumanInTheLoopMiddleware` 做敏感工具审批
- 令牌方式：改成 HttpOnly Cookie + CSRF Token（后端已具备服务端撤销能力）


## 常见问题

**Q：登录后重启服务，历史对话还在吗？**
在。会话与消息都在 PostgreSQL 里；`InMemorySaver` 丢失后会用数据库历史重建上下文。
**但**如果不配 `AGENT_DATABASE_URL`（回退内存仓储），重启就会丢——`/api/v1/health`
的 `components.storage` 会是 `degraded` 并给出原因。

**Q：重启服务后所有人都要重新登录？**
说明 `AGENT_JWT_SECRET` 没设置（用了进程级随机密钥）。在 `.env` 里固定一个随机串即可，
例如 `python -c "import secrets;print(secrets.token_urlsafe(48))"`。
注意：`user_sessions` 里的登录态还在，只要换回同一个密钥，旧 access token 依旧有效。

**Q：怎么创建第一个管理员？**
把用户名写进 `AGENT_AUTH_ADMIN_USERNAMES=alice`（逗号分隔），然后注册该用户名即可获得
`role=admin`。已有账号也可以直接改库：`UPDATE users SET role='admin' WHERE username='alice';`。

**Q：`/api/v1/chat` 返回 401 `unauthorized`？**
忘记带 `Authorization: Bearer <access_token>`，或 access token 已过期。
用 `POST /api/v1/auth/refresh` 换新的（前端 `web/auth.js` 会自动做一次）。

**Q：返回 404 `session_not_found`，但我明明有这个会话？**
会话可能属于别的账号（越权与不存在返回同一个错误，这是有意的）、已被删除，
或数据库被清空过。请用 `GET /api/v1/sessions` 确认当前账号可见的会话。

**Q：提示 `database_unavailable` 或启动日志说"数据库不可用，回退到内存仓储"？**
检查 `AGENT_DATABASE_URL` 是否正确、PostgreSQL 是否在跑、表是否已建
（`psql -f docs/db/schema.postgres.sql`）、驱动是否装了（`pip install psycopg2-binary`）。
注意回退内存仓储后服务仍可用，只是重启丢数据。

**Q：`/auth/register` 返回 409 `conflict`？**
用户名或邮箱已被占用（`citext` 大小写不敏感，`Alice` 与 `alice` 视为同一个）。

**Q：`/auth/register` 返回 400 `validation_error`？**
密码太短、太长、含首尾空格、全同字符、等于用户名，或命中了常见弱口令黑名单。
长度下限由 `AGENT_PASSWORD_MIN_LENGTH` 控制。

**Q：能不能关掉登录，先本地随便跑？**
可以：`AGENT_AUTH_REQUIRED=false`。此时无 token 的请求会映射到一个固定的
`anonymous_local` 账号（历史仍按它隔离），`/api/v1/health` 的 `components.auth`
会标记为 `degraded` 提醒你。**生产环境必须保持 `true`。**

**Q：工作区为什么每个账号不一样？**
工作区**按用户隔离**（根目录与写权限各自一份），改动只影响自己；服务重启后回到默认的项目根目录。
如果你希望整个实例共用一个目录，可以在部署时改 `app/main.py` 里 `init_workspace_manager(PROJECT_ROOT)`
的默认值，或者统一约定大家填同一个路径。

**Q：`/chat` 返回 503「未配置 API Key」？**
在 `.env` 里设置 `AGENT_API_KEY`（并按需设置 `AGENT_BASE_URL`）；或把 `AGENT_PROVIDER` 设为 `mock` 先跑通链路。

**Q：提示 `未安装 langchain-openai`？**
执行 `pip install "langchain-openai>=1.0,<2"`（或 `uv pip install -e ".[dev]"` 重装全部依赖）。

**Q：`ImportError: cannot import name 'ExecutionInfo' from 'langgraph.runtime'`？**
`langgraph` 与 `langgraph-prebuilt` 版本错配（典型：`langgraph 1.0.8` + `langgraph-prebuilt 1.0.10`）。
`langgraph` 只声明了 `langgraph-prebuilt>=1.0.7,<1.1.0`，而 `langgraph-prebuilt` 完全不声明依赖
`langgraph`，所以 pip 不会帮你对齐。按 `requirements.txt` 里的锁定重装即可：

```powershell
pip install "langgraph-prebuilt==1.0.7" "langgraph-checkpoint==4.0.0"
```

**Q：`openai.BadRequestError: 400 ... An assistant message with 'tool_calls' must be followed by tool messages responding to each 'tool_call_id'`？**

会话历史里有一条带 `tool_calls` 的 assistant 消息，但缺少对应的 tool 消息。三种常见成因：

1. 前端点了「停止」或连接断开 —— 模型已经产出 `tool_calls`，工具还没执行就被取消；
2. 进程在「模型已回复、工具未执行」之间崩溃/重启（若换了持久化 checkpointer 会保留下来）；
3. 自定义中间件在模型返回 `tool_calls` 之后直接跳到结束。

修复：`app/agent/middleware.py` 里的 `RepairDanglingToolCallsMiddleware` 会在**每次调用模型前**把缺失的
tool 响应补齐（内容标注为“未执行”），因此已经坏掉的会话在下一轮会自动自愈，无需删会话；
`GET /api/v1/sessions/{id}` 返回的历史也做了同样处理。

同时，`app/agent/factory.py` 里已移除 `ModelCallLimitMiddleware(exit_behavior="end")`——它会在模型刚返回
`tool_calls` 时直接结束，正好制造这种不完整历史；现在只保留 `ToolCallLimitMiddleware`（超限时会自动补
`ToolMessage`），并用图自身的 `recursion_limit` 兜底。

**Q：`pip install -r requirements.txt` 报 `UnicodeDecodeError: 'gbk' codec can't decode byte ...`？**
Windows 中文环境的 locale 是 `cp936/gbk`，pip 读 `-r` 文件时用该编码。本项目的 `requirements.txt`
已保持**纯 ASCII**（注释也是英文）。如果你自己加了中文注释，要么存成 UTF-8 with BOM，要么在第一行加
`# -*- coding: utf-8 -*-`。

**Q：明明在 conda 环境里，却 import 到了版本不对的包？**
可能之前用 `pip install --user` 装过包，落入 `C:\Users\<你>\AppData\Roaming\Python\PythonXY\site-packages`，
任何同版本的解释器（含 conda 环境）都会加载它。建议用项目内干净 venv，或在环境里设 `PYTHONNOUSERSITE=1`。

**Q：Agent 说「路径超出工作区范围」？**
文件工具只能操作「工作区」面板里设置的目录。先把工作区改成目标目录的**上级**目录，
或者改到目标目录本身；路径不能用绝对路径越界（这是防止模型读写你其它文件的关键保护）。

**Q：Agent 说「当前工作区为只读模式」？**
在「工作区」面板里勾选「允许写 / 删文件」再保存；或 `PUT /api/v1/workspace {"allow_writes": true}`。

**Q：重启服务后工作区变回项目根目录了？**
这是设计如此：工作区配置**只存在内存里**（按你的要求不写配置文件）。重启后需要重新设置一次。

**Q：`run_command` 报「只读模式」/「危险模式」？**
只读模式：在「工作区」面板勾选「允许写 / 删文件」——终端与写文件共用这个开关。
危险模式：命令命中了黑名单（删目录树、格式化、改注册表、`curl | sh` 等），换个写法或改用文件工具。
如果是白名单模式报错，把命令首词加进 `AGENT_SHELL_ALLOWLIST`，或把该配置留空。

**Q：`web_search` 报「搜索失败或没有结果」？**
默认抓 Bing 结果页，需要外网直连；被墙/走代理时可在 `.env` 里配 `AGENT_SEARCH_API_KEY`（Tavily）
并设 `AGENT_SEARCH_PROVIDER=tavily`，或换成 `duckduckgo`。

**Q：Windows 控制台中文日志乱码？**
`app/core/logging.py` 已把 stdout 重配为 UTF-8；如仍乱码，建议使用 Windows Terminal 并执行 `chcp 65001`。

**Q：打开 `http://127.0.0.1:8000/` 是空白页 / 404？**
确认项目根目录下的 `web/` 还在（页面由 `StaticFiles` 挂载，启动日志会打印“已挂载前端页面 /ui/”）。
若把它删掉了，`/` 会自动改成跳转 `/api/v1/info`。页面本身不依赖外网，但必须通过后端地址访问
（直接双击 `web/index.html` 打开会因为 `file://` 的跨域限制而无法调用接口）。

**Q：生产环境 CORS 怎么配？**
把 `AGENT_CORS_ORIGINS` 设为实际的逗号分隔域名列表（不要用 `*`）。
