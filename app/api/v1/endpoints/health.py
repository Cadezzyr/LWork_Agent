"""健康检查接口。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse

from app.api.deps import SettingsDep
from app.schemas.agent import ComponentStatus, HealthResponse

router = APIRouter(prefix="/health", tags=["健康检查"])


@router.get("", response_model=HealthResponse, summary="存活检查")
async def health(request: Request, settings: SettingsDep) -> HealthResponse:
    """返回服务存活状态与关键组件概况。

    该接口不依赖模型服务，可用于 K8s livenessProbe。
    """
    started_at = getattr(request.app.state, "started_at", None) or time.time()
    runtime = getattr(request.app.state, "runtime", None)
    runtime_error = getattr(request.app.state, "runtime_error", None)

    if runtime is not None:
        agent_status = ComponentStatus(status="ok", detail=f"{settings.provider}:{settings.model}")
        overall = "ok"
    elif runtime_error:
        agent_status = ComponentStatus(status="degraded", detail=runtime_error)
        overall = "degraded"
    else:
        agent_status = ComponentStatus(status="degraded", detail="智能体运行时尚未初始化")
        overall = "degraded"

    storage_status = await _storage_status(request)
    # 未配置数据库属于"开发默认"（内存仓储），不算服务降级；
    # 但一旦配置了数据库却连不上，就必须把整体状态标成 degraded。
    if settings.database_dsn is not None and storage_status.status != "ok":
        overall = "degraded"

    return HealthResponse(
        status=overall,
        app_name=settings.app_name,
        version=settings.version,
        env=settings.env,
        uptime_seconds=round(time.time() - started_at, 3),
        components={
            "agent": agent_status,
            "model_provider": ComponentStatus(
                status="ok" if settings.readiness_error() is None else "degraded",
                detail=settings.readiness_error(),
            ),
            "storage": storage_status,
            "auth": ComponentStatus(
                status="ok" if settings.auth_required else "degraded",
                detail=(
                    "已开启登录校验"
                    if settings.auth_required
                    else "AGENT_AUTH_REQUIRED=false：接口未要求登录，仅适合本地调试"
                ),
            ),
        },
    )


async def _storage_status(request: Request) -> ComponentStatus:
    """数据存储健康状态：数据库可达为 ok，内存兜底为 degraded（历史不持久）。"""
    database = getattr(request.app.state, "database", None)
    warning = getattr(request.app.state, "repository_warning", None)
    if database is None:
        return ComponentStatus(
            status="degraded",
            detail=warning or "未配置 AGENT_DATABASE_URL，使用内存存储（重启后历史丢失）",
        )
    ok, error = await database.ping()
    if not ok:
        return ComponentStatus(status="degraded", detail=f"PostgreSQL 不可用：{error}")
    return ComponentStatus(status="ok", detail="PostgreSQL")


@router.get("/ready", summary="就绪检查")
async def ready(request: Request) -> Any:
    """模型配置与 Agent 运行时都就绪时才返回 200，否则 503。"""
    runtime = getattr(request.app.state, "runtime", None)
    runtime_error = getattr(request.app.state, "runtime_error", None)
    if runtime is None:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "status": "not_ready",
                "reason": runtime_error or "智能体运行时尚未初始化",
            },
        )
    return {
        "status": "ready",
        "agent_name": runtime.settings.agent_name,
        "provider": runtime.settings.provider,
        "model": runtime.settings.model,
        "tools": runtime.tool_names,
    }
