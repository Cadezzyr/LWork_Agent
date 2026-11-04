"""路由聚合：把所有子路由挂载到 /api/v1 下。"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1.endpoints import agent, auth, chat, health, meta, sessions, workspace

api_router = APIRouter()
api_router.include_router(meta.router)
api_router.include_router(health.router)
api_router.include_router(auth.router)
api_router.include_router(agent.router)
api_router.include_router(chat.router)
api_router.include_router(sessions.router)
api_router.include_router(workspace.router)

__all__ = ["api_router"]
