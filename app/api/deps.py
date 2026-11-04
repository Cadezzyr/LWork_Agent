"""FastAPI 依赖注入。

认证相关的依赖都在这里收口：
- ``get_repositories`` / ``get_auth_service`` / ``get_conversation_service``：来自 ``app.state``；
- ``get_current_user``：解析 ``Authorization: Bearer <access_token>``，
  任何失败都以 401/403 + 统一错误体返回，**不会**串到 500。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.agent.factory import AgentRuntime
from app.core.config import Settings, get_settings
from app.core.exceptions import (
    AgentUnavailableError,
    AuthError,
    DatabaseUnavailableError,
    ForbiddenError,
)
from app.db.base import Repositories
from app.db.models import UserRecord
from app.services.agent_service import AgentService
from app.services.auth_service import AuthenticatedUser, AuthService
from app.services.conversation_service import ConversationService

# auto_error=False：缺失 token 时由我们自己抛统一的 401 错误体
_bearer_scheme = HTTPBearer(auto_error=False, description="Bearer access_token")


def get_app_settings(request: Request) -> Settings:
    settings = getattr(request.app.state, "settings", None)
    return settings if isinstance(settings, Settings) else get_settings()


def get_repositories(request: Request) -> Repositories:
    repositories = getattr(request.app.state, "repositories", None)
    if not isinstance(repositories, Repositories):
        raise DatabaseUnavailableError("数据访问层尚未初始化，请检查服务启动日志")
    return repositories


def get_auth_service(request: Request) -> AuthService:
    service = getattr(request.app.state, "auth_service", None)
    if service is None:
        raise AuthError("认证服务尚未就绪，请检查服务启动日志", code="auth_unavailable")
    return service


def get_conversation_service(request: Request) -> ConversationService:
    service = getattr(request.app.state, "conversation_service", None)
    if service is None:
        raise DatabaseUnavailableError("会话存储尚未就绪，请检查服务启动日志")
    return service


def get_agent_runtime(request: Request) -> AgentRuntime:
    runtime = getattr(request.app.state, "runtime", None)
    if runtime is None:
        reason = getattr(request.app.state, "runtime_error", None)
        raise AgentUnavailableError(
            reason or "智能体运行时尚未初始化，请检查服务启动日志",
        )
    return runtime


def get_agent_service(request: Request) -> AgentService:
    service = getattr(request.app.state, "agent_service", None)
    if service is None:
        reason = getattr(request.app.state, "runtime_error", None)
        raise AgentUnavailableError(
            reason or "智能体服务尚未就绪，请检查 .env 中的模型配置",
        )
    return service


async def get_current_user(
    request: Request,
    auth_service: Annotated[AuthService, Depends(get_auth_service)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> AuthenticatedUser:
    """要求登录：解析并校验 Bearer token。

    ``AGENT_AUTH_REQUIRED=false``（仅本地调试）时，无 token 的请求会被映射到
    一个固定的"匿名用户"，历史仍然按该匿名主体隔离，不会串到真实用户。
    """
    settings = get_app_settings(request)

    if credentials is None or not credentials.credentials:
        if settings.auth_required:
            raise AuthError("缺少访问令牌，请先登录（Authorization: Bearer <token>）")
        return await auth_service.anonymous_user()

    if credentials.scheme.lower() != "bearer":
        raise AuthError("不支持的认证方式，请使用 Bearer token")

    return await auth_service.authenticate(credentials.credentials)


async def require_admin(
    current: Annotated[AuthenticatedUser, Depends(get_current_user)],
) -> UserRecord:
    if current.user.role != "admin":
        raise ForbiddenError("需要管理员权限")
    return current.user


SettingsDep = Annotated[Settings, Depends(get_app_settings)]
RuntimeDep = Annotated[AgentRuntime, Depends(get_agent_runtime)]
AgentServiceDep = Annotated[AgentService, Depends(get_agent_service)]
RepositoriesDep = Annotated[Repositories, Depends(get_repositories)]
AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
ConversationServiceDep = Annotated[ConversationService, Depends(get_conversation_service)]
CurrentUserDep = Annotated[AuthenticatedUser, Depends(get_current_user)]

__all__ = [
    "AgentServiceDep",
    "AuthServiceDep",
    "ConversationServiceDep",
    "CurrentUserDep",
    "RepositoriesDep",
    "RuntimeDep",
    "SettingsDep",
    "get_agent_runtime",
    "get_agent_service",
    "get_app_settings",
    "get_auth_service",
    "get_conversation_service",
    "get_current_user",
    "get_repositories",
    "require_admin",
]
