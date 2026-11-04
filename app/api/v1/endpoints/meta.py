"""服务元信息接口（接口总览，方便快速自检）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from app.api.deps import SettingsDep

router = APIRouter(tags=["服务信息"])


@router.get("/info", summary="服务与接口总览")
async def service_info(settings: SettingsDep) -> dict[str, Any]:
    """返回服务名称、版本、前端页面地址与全部接口路径。"""
    prefix = settings.api_prefix
    return {
        "name": settings.app_name,
        "version": settings.version,
        "env": settings.env,
        "api_prefix": prefix,
        "ui": "/ui/",
        "docs": "/docs",
        "openapi": "/openapi.json",
        "auth_required": settings.auth_required,
        "endpoints": {
            "auth_register": f"{prefix}/auth/register",
            "auth_login": f"{prefix}/auth/login",
            "auth_refresh": f"{prefix}/auth/refresh",
            "auth_logout": f"{prefix}/auth/logout",
            "auth_me": f"{prefix}/auth/me",
            "chat": f"{prefix}/chat",
            "chat_stream": f"{prefix}/chat/stream",
            "sessions": f"{prefix}/sessions",
            "session_detail": f"{prefix}/sessions/{{session_id}}",
            "agent_info": f"{prefix}/agent/info",
            "tools": f"{prefix}/agent/tools",
            "workspace": f"{prefix}/workspace",
            "health": f"{prefix}/health",
            "ready": f"{prefix}/health/ready",
        },
    }
