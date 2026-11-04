"""应用级异常与统一错误响应。"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.logging import get_logger

logger = get_logger("core.exceptions")


class AppError(Exception):
    """所有业务异常的基类，会被转换成统一的 JSON 错误响应。"""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if status_code is not None:
            self.status_code = status_code
        if code is not None:
            self.code = code
        self.details = details or {}

    def to_payload(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return {"error": body}


class AgentUnavailableError(AppError):
    """智能体运行时未就绪（模型配置缺失、依赖未安装等）。"""

    status_code = 503
    code = "agent_unavailable"


class ModelProviderError(AppError):
    """调用上游模型服务失败。"""

    status_code = 502
    code = "model_provider_error"


class SessionNotFoundError(AppError):
    status_code = 404
    code = "session_not_found"


class ToolExecutionError(AppError):
    status_code = 400
    code = "tool_execution_error"


# --------------------------------------------------------------------------- 认证
class AuthError(AppError):
    """未通过身份认证（缺 token / token 无效 / 已拉黑）。"""

    status_code = 401
    code = "unauthorized"

    def __init__(
        self,
        message: str = "未登录或登录态已失效，请重新登录",
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, code=code, details=details)


class InvalidCredentialsError(AuthError):
    """用户名/密码错误（对外统一提示，避免暴露"账号是否存在"）。"""

    code = "invalid_credentials"


class ForbiddenError(AppError):
    status_code = 403
    code = "forbidden"


class ConflictError(AppError):
    """资源冲突，例如用户名已被占用。"""

    status_code = 409
    code = "conflict"


class DatabaseUnavailableError(AppError):
    """数据库未配置或连接失败。"""

    status_code = 503
    code = "database_unavailable"


class ValidationError(AppError):
    """入参不满足业务规则（比 FastAPI 的 422 更具体）。"""

    status_code = 400
    code = "validation_error"


def register_exception_handlers(app: FastAPI) -> None:
    """注册异常处理器，保证任何失败都返回统一结构的 JSON。"""

    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        logger.warning("业务异常 [%s] %s", exc.code, exc.message)
        headers = {"WWW-Authenticate": 'Bearer realm="api"'} if exc.status_code == 401 else None
        return JSONResponse(
            status_code=exc.status_code,
            content=exc.to_payload(),
            headers=headers,
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        logger.exception("未捕获异常: %s", exc)
        error = AppError("服务内部错误", details={"type": type(exc).__name__})
        return JSONResponse(status_code=500, content=error.to_payload())
