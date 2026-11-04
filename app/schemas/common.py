"""通用响应模型。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    code: str = Field(description="机器可读的错误码，例如 agent_unavailable")
    message: str = Field(description="人类可读的错误说明")
    details: dict[str, Any] = Field(default_factory=dict, description="附加诊断信息")


class ErrorResponse(BaseModel):
    error: ErrorDetail

    model_config = {
        "json_schema_extra": {
            "example": {
                "error": {
                    "code": "agent_unavailable",
                    "message": "provider=openai 未配置 API Key",
                    "details": {},
                }
            }
        }
    }


class RenameRequest(BaseModel):
    """重命名会话/资源。"""

    title: str = Field(min_length=1, max_length=60, description="新标题")


class DeletedResponse(BaseModel):
    """批量删除的结果。"""

    deleted: int = Field(description="实际删除的条数")
    remaining: int = Field(default=0, description="仍未删除的条数（超过单次上限时会大于 0）")
