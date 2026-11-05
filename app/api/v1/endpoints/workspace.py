"""工作区配置接口：根目录与写权限都在运行时设置（不写配置文件）。

前端页面通过这些接口切换 Agent 可操作的范围：

- ``GET  /api/v1/workspace``          查看当前设置
- ``POST /api/v1/workspace/validate`` 校验一个候选目录（不生效，仅预览）
- ``PUT  /api/v1/workspace``          应用新设置
- ``GET  /api/v1/workspace/tree``     预览目录结构（确认路径选得对不对）

⚠️ 工作区是**按用户隔离**的：每个登录用户都有自己的根目录与写权限，
修改只影响自己（进程重启后回到 AGENT_WORKSPACE_ROOT 指定的默认目录）。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter

from app.agent.workspace import WorkspaceError, WorkspaceManager, get_workspace_manager
from app.api.deps import CurrentUserDep
from app.core.exceptions import AppError
from app.core.logging import get_logger
from app.schemas.workspace import (
    WorkspaceInfo,
    WorkspaceTreeEntry,
    WorkspaceTreeResponse,
    WorkspaceUpdateRequest,
    WorkspaceValidateRequest,
    WorkspaceValidateResponse,
)

router = APIRouter(prefix="/workspace", tags=["工作区"])
logger = get_logger("api.workspace")


def _to_info(manager: WorkspaceManager, *, with_count: bool = True) -> WorkspaceInfo:
    snapshot = manager.info(with_count=with_count)
    return WorkspaceInfo(
        root=snapshot.root,
        exists=snapshot.exists,
        writable=snapshot.writable,
        allow_writes=snapshot.allow_writes,
        is_default=snapshot.is_default,
        updated_at=snapshot.updated_at,
        file_count=snapshot.file_count,
        default_root=str(manager.default_root),
    )


@router.get("", response_model=WorkspaceInfo, summary="查看当前工作区设置")
async def read_workspace(current: CurrentUserDep) -> WorkspaceInfo:
    return _to_info(get_workspace_manager(current.user.id))


@router.put(
    "",
    response_model=WorkspaceInfo,
    summary="设置自己的工作区根目录 / 写权限",
)
async def update_workspace(
    payload: WorkspaceUpdateRequest,
    current: CurrentUserDep,
) -> WorkspaceInfo:
    manager = get_workspace_manager(current.user.id)
    provided = payload.model_fields_set
    try:
        # 显式传 root（包括 null）才改根目录：null/空串表示恢复默认
        if "root" in provided:
            manager.set_root(payload.root)
        if "allow_writes" in provided and payload.allow_writes is not None:
            manager.set_allow_writes(payload.allow_writes)
    except WorkspaceError as exc:
        raise AppError(str(exc), status_code=400, code="invalid_workspace") from exc
    logger.info(
        "用户 %s 的工作区更新为 root=%s allow_writes=%s",
        current.user.username,
        manager.root,
        manager.allow_writes,
    )
    return _to_info(manager)


@router.post("/validate", response_model=WorkspaceValidateResponse, summary="校验候选目录")
async def validate_workspace(
    payload: WorkspaceValidateRequest,
    current: CurrentUserDep,
) -> WorkspaceValidateResponse:
    raw = (payload.root or "").strip().strip("\"'")
    candidate = Path(raw).expanduser()
    if not raw:
        return WorkspaceValidateResponse(
            root="", valid=False, exists=False, is_dir=False, writable=False, message="路径不能为空"
        )
    if not candidate.is_absolute():
        return WorkspaceValidateResponse(
            root=str(candidate),
            valid=False,
            exists=False,
            is_dir=False,
            writable=False,
            message="必须是绝对路径",
        )
    resolved = candidate.resolve()
    exists = resolved.exists()
    is_dir = resolved.is_dir()
    writable = is_dir and os.access(resolved, os.W_OK)
    if not exists:
        message = "目录不存在"
    elif not is_dir:
        message = "这是一个文件，不是目录"
    elif resolved.parent == resolved:
        message = "不能使用磁盘根目录"
    else:
        message = "可以设为工作区" if writable else "目录存在但当前进程不可写（只读模式下仍可读取）"
    return WorkspaceValidateResponse(
        root=str(resolved),
        valid=exists and is_dir and resolved.parent != resolved,
        exists=exists,
        is_dir=is_dir,
        writable=writable,
        message=message,
    )


@router.get("/tree", response_model=WorkspaceTreeResponse, summary="预览工作区目录结构")
async def workspace_tree(
    current: CurrentUserDep,
    path: str = ".",
    depth: int = 2,
) -> WorkspaceTreeResponse:
    manager = get_workspace_manager(current.user.id)
    try:
        entries = manager.tree(path, depth=depth)
    except WorkspaceError as exc:
        raise AppError(str(exc), status_code=400, code="invalid_workspace_path") from exc
    return WorkspaceTreeResponse(
        root=str(manager.root),
        entries=[WorkspaceTreeEntry(**item) for item in entries],
    )
