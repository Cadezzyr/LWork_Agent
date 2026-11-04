"""工作区（Workspace）相关的请求/响应模型。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class WorkspaceInfo(BaseModel):
    """当前工作区的运行态信息（只在内存里，不写配置文件）。"""

    root: str = Field(description="当前工作区根目录（绝对路径）")
    exists: bool = Field(description="目录是否存在")
    writable: bool = Field(description="当前进程对该目录是否有写权限")
    allow_writes: bool = Field(description="是否允许 Agent 写/删文件（页面上的开关）")
    is_default: bool = Field(description="是否仍是默认值（项目根目录）")
    updated_at: str = Field(description="最后一次修改时间（ISO8601）")
    file_count: int | None = Field(default=None, description="根目录下的条目数量（用于确认路径对不对）")
    default_root: str = Field(description="默认工作区（项目根目录），可用于「恢复默认」")


class WorkspaceUpdateRequest(BaseModel):
    root: str | None = Field(
        default=None,
        description="新的工作区根目录（绝对路径）；传 null 或空字符串表示恢复默认",
        examples=["D:\\projects\\demo"],
    )
    allow_writes: bool | None = Field(
        default=None,
        description="是否允许写/删文件；不传则保持不变",
    )


class WorkspaceTreeEntry(BaseModel):
    path: str
    type: Literal["file", "dir"]


class WorkspaceTreeResponse(BaseModel):
    root: str
    entries: list[WorkspaceTreeEntry] = Field(default_factory=list)


class WorkspaceValidateRequest(BaseModel):
    root: str = Field(description="待校验的目录（绝对路径）")


class WorkspaceValidateResponse(BaseModel):
    root: str
    valid: bool
    exists: bool
    is_dir: bool
    writable: bool
    message: str
