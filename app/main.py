"""FastAPI 应用入口。

本地开发（任选其一，都必须在项目根目录执行）：
    uvicorn app.main:app --reload
    python -m app.main
    python app/main.py

启动后：
    聊天页面   http://127.0.0.1:8000/ui/
    API 文档   http://127.0.0.1:8000/docs
"""

# ruff: noqa: E402  # 下面的 import 必须在 sys.path 引导之后，顺序是有意为之

from __future__ import annotations

import sys
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

# 支持 `python app/main.py` 这种脚本方式启动：把项目根目录加入模块搜索路径，
# 否则 `import app.xxx` 会失败（Python 只把脚本所在目录 app/ 放进 sys.path）。
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.agent.factory import build_agent_runtime
from app.agent.workspace import get_workspace_manager, init_workspace_manager
from app.api.v1.router import api_router
from app.core.config import PROJECT_ROOT, Settings, get_settings
from app.core.exceptions import AppError, register_exception_handlers
from app.core.logging import get_logger, setup_logging
from app.db import RepositoryBundle, build_repositories
from app.services.agent_service import AgentService
from app.services.auth_service import AuthService
from app.services.conversation_service import ConversationService

logger = get_logger("main")

# 前端页面目录（纯静态文件，无构建步骤）
_WEB_DIR = Path(__file__).resolve().parent.parent / "web"

_DESCRIPTION = """\
基于 **LangChain 1.x + LangGraph + FastAPI** 的智能体（Agent）后端脚手架。

### 能力
- 聊天页面：`GET /ui/`（纯静态前端，打开即用，含登录、会话历史与工作区设置）
- 登录鉴权：`POST /auth/register|login|refresh|logout`，`GET /auth/me`（JWT + 服务端可撤销登录态）
- `POST /api/v1/chat`：同步对话，返回完整回复、工具调用轨迹与 token 用量
- `POST /api/v1/chat/stream`：SSE 流式对话（token / tool_call / tool_result / final）
- `GET|POST|PATCH|DELETE /api/v1/sessions…`：会话管理与历史查询（按登录用户隔离）
- `GET|PUT /api/v1/workspace`：运行时设置 Agent 可操作的工作区目录与写权限
- `GET /api/v1/agent/info`、`GET /api/v1/agent/tools`：运行时配置与工具清单
- `GET /api/v1/health`、`GET /api/v1/health/ready`：存活/就绪探针
- `GET /api/v1/info`：服务与接口总览

### 鉴权
除 `/health`、`/info`、`/auth/*` 外的业务接口都需要
`Authorization: Bearer <access_token>`；access token 失效时用 `/auth/refresh` 换新的。
会话与消息保存在 PostgreSQL（`AGENT_DATABASE_URL`），因此重启后历史仍在。

### 离线联调
把 `AGENT_PROVIDER` 设为 `mock`，无需任何 API Key 即可跑通完整链路。
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """启动时构建数据仓储与 Agent 运行时。

    两者互相独立：数据库不可用会退回内存仓储（登录与历史仍可用，重启丢失）；
    Agent 运行时构建失败则 /chat 返回 503，但登录与历史查询照常。
    """
    settings: Settings = app.state.settings

    logger.info(
        "正在启动 %s v%s（env=%s, provider=%s, model=%s）",
        settings.app_name,
        settings.version,
        settings.env,
        settings.provider,
        settings.model,
    )

    # ---------- 1) 数据层：PostgreSQL（或内存兜底） ----------
    bundle: RepositoryBundle = build_repositories(settings)
    app.state.repositories = bundle.repositories
    app.state.database = bundle.database
    app.state.repository_warning = bundle.warning
    app.state.auth_service = AuthService(bundle.repositories, settings)
    app.state.conversation_service = ConversationService(bundle.repositories, settings)

    for warning in settings.auth_warnings():
        logger.warning("认证配置提醒：%s", warning)

    # ---------- 2) Agent 运行时 ----------
    try:
        runtime = build_agent_runtime(settings)
    except AppError as exc:
        app.state.runtime_error = exc.message
        logger.error("智能体初始化失败：%s", exc.message)
    except Exception as exc:  # noqa: BLE001 - 启动阶段需要兜底，避免进程直接退出
        app.state.runtime_error = f"{type(exc).__name__}: {exc}"
        logger.exception("智能体初始化异常")
    else:
        app.state.runtime = runtime
        app.state.agent_service = AgentService(
            runtime,
            app.state.conversation_service,
            settings=settings,
            max_sessions=settings.max_sessions,
        )
        app.state.runtime_error = None
        workspace = get_workspace_manager()
        logger.info(
            "启动完成：http://%s:%s/（%s 个工具，工作区 %s，存储 %s）",
            settings.host,
            settings.port,
            len(runtime.tools),
            workspace.root,
            bundle.repositories.backend,
        )

    yield

    app.state.runtime = None
    app.state.agent_service = None
    if app.state.database is not None:
        app.state.database.close()
    app.state.database = None
    logger.info("服务已停止")


def create_app(settings: Settings | None = None) -> FastAPI:
    """应用工厂，便于测试时注入自定义配置。"""
    resolved = settings or get_settings()
    setup_logging(resolved.log_level)

    app = FastAPI(
        title=resolved.app_name,
        description=_DESCRIPTION,
        version=resolved.version,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
    )

    app.state.settings = resolved
    app.state.started_at = time.time()
    app.state.runtime = None
    app.state.agent_service = None
    app.state.runtime_error = None
    # 数据层在 lifespan 里初始化（需要异步/线程池，且失败不能阻塞应用创建）
    app.state.repositories = None
    app.state.database = None
    app.state.auth_service = None
    app.state.conversation_service = None
    app.state.repository_warning = None

    # 工作区默认目录由 AGENT_WORKSPACE_ROOT 指定（默认 D:\LWork_Agent_Project）；
    # 之后每个用户都能在页面「工作区设置」里按需切换（仅内存态，不写配置文件）
    workspace_root = Path(resolved.workspace_root.strip() or PROJECT_ROOT).expanduser()
    try:
        workspace_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "默认工作区目录不可用（%s：%s），回退到项目根目录 %s",
            workspace_root,
            exc,
            PROJECT_ROOT,
        )
        workspace_root = PROJECT_ROOT
    init_workspace_manager(workspace_root)

    origins = resolved.cors_origin_list
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials="*" not in origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    register_exception_handlers(app)
    app.include_router(api_router, prefix=resolved.api_prefix)

    # 挂载聊天页面：http://127.0.0.1:8000/ui/
    if _WEB_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=str(_WEB_DIR), html=True), name="ui")
        logger.info("已挂载前端页面 /ui/（目录 %s）", _WEB_DIR)
    else:  # pragma: no cover - 仅当 web 目录被删除时
        logger.warning("未找到前端目录 %s，/ui/ 将不可用", _WEB_DIR)

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        """根路径直接跳到聊天页面，方便本地打开即用。"""
        target = "/ui/" if _WEB_DIR.is_dir() else f"{resolved.api_prefix}/info"
        return RedirectResponse(url=target, status_code=307)

    return app


app = create_app()


def run() -> None:
    """命令行入口：``python -m app.main`` 或 ``agent-api``。"""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.env == "dev",
        log_config=None,
    )


if __name__ == "__main__":
    run()
