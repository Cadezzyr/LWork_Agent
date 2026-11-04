"""会话管理接口（全部按登录用户隔离）。

任何按 ID 访问的接口都会带上 ``user_id`` 条件：
别人的会话与不存在的会话返回**同样的 404**，不泄露存在性。
"""

from __future__ import annotations

from fastapi import APIRouter, Query, status

from app.api.deps import ConversationServiceDep, CurrentUserDep
from app.core.exceptions import SessionNotFoundError
from app.schemas.chat import (
    SessionCreateRequest,
    SessionDetail,
    SessionInfo,
    SessionListResponse,
)
from app.schemas.common import DeletedResponse, RenameRequest

router = APIRouter(prefix="/sessions", tags=["会话"])


@router.post(
    "",
    response_model=SessionInfo,
    status_code=status.HTTP_201_CREATED,
    summary="创建会话",
)
async def create_session(
    current: CurrentUserDep,
    service: ConversationServiceDep,
    payload: SessionCreateRequest | None = None,
) -> SessionInfo:
    """显式创建会话；也可以直接在 /chat 时不传 session_id 由服务端自动创建。"""
    record = await service.create(
        current.user.id,
        title=payload.title if payload else None,
        session_id=payload.session_id if payload else None,
    )
    return service.to_info(record)


@router.get("", response_model=SessionListResponse, summary="会话列表（当前用户）")
async def list_sessions(
    current: CurrentUserDep,
    service: ConversationServiceDep,
    limit: int = Query(default=50, ge=1, le=200, description="分页大小"),
    offset: int = Query(default=0, ge=0, description="分页偏移"),
    keyword: str | None = Query(default=None, max_length=64, description="按标题模糊搜索"),
) -> SessionListResponse:
    sessions, total = await service.list(
        current.user.id,
        limit=limit,
        offset=offset,
        keyword=keyword,
    )
    return SessionListResponse(total=total, sessions=sessions, limit=limit, offset=offset)


@router.get("/{session_id}", response_model=SessionDetail, summary="会话详情（含历史消息）")
async def get_session(
    session_id: str,
    current: CurrentUserDep,
    service: ConversationServiceDep,
) -> SessionDetail:
    return await service.detail(current.user.id, session_id)


@router.patch("/{session_id}", response_model=SessionInfo, summary="重命名会话")
async def rename_session(
    session_id: str,
    payload: RenameRequest,
    current: CurrentUserDep,
    service: ConversationServiceDep,
) -> SessionInfo:
    return await service.rename(current.user.id, session_id, payload.title)


@router.delete(
    "/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="删除会话（软删除）",
)
async def delete_session(
    session_id: str,
    current: CurrentUserDep,
    service: ConversationServiceDep,
) -> None:
    await service.delete(current.user.id, session_id)


@router.delete("", response_model=DeletedResponse, summary="清空当前用户全部会话（软删除）")
async def clear_sessions(
    current: CurrentUserDep,
    service: ConversationServiceDep,
    limit: int = Query(default=200, ge=1, le=500, description="一次最多清理多少个会话"),
) -> DeletedResponse:
    """批量软删除：取一页列表再逐个删除，避免一次锁太多行。

    只删当前用户可见的会话；并发删除导致"已经不在了"的情况按已删除处理。
    """
    sessions, total = await service.list(current.user.id, limit=limit, offset=0)
    deleted = 0
    for item in sessions:
        try:
            await service.delete(current.user.id, item.session_id)
            deleted += 1
        except SessionNotFoundError:
            continue
    return DeletedResponse(deleted=deleted, remaining=max(0, total - deleted))
