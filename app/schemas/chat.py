"""对话与会话相关的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

Role = Literal["system", "user", "assistant", "tool"]
DecisionType = Literal["approve", "edit", "reject"]
_SESSION_ID_PATTERN = r"^[A-Za-z0-9_-]{1,128}$"


class ToolCallRecord(BaseModel):
    """一次工具调用及其结果。"""

    id: str | None = Field(default=None, description="工具调用 ID")
    name: str = Field(description="工具名")
    args: dict[str, Any] = Field(default_factory=dict, description="工具入参")
    result: str | None = Field(default=None, description="工具返回内容")
    status: Literal["success", "error"] = Field(default="success", description="调用状态")


class MessageOut(BaseModel):
    """单条会话消息。"""

    role: Role
    content: str
    name: str | None = Field(default=None, description="工具消息对应的工具名")
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)


class Usage(BaseModel):
    """模型 token 用量（上游未返回时为 0）。"""

    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ChatRequest(BaseModel):
    message: str = Field(
        min_length=1,
        max_length=8000,
        description="用户输入内容",
        examples=["帮我算一下 (3+4)*2.5"],
    )
    session_id: str | None = Field(
        default=None,
        pattern=_SESSION_ID_PATTERN,
        description="会话 ID；不传则自动创建新会话（多轮对话需复用同一个 ID）",
        examples=["sess_9f2c1a4b7d8e0f31"],
    )

    @field_validator("message")
    @classmethod
    def _strip_message(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("message 不能只包含空白字符")
        return stripped

    model_config = {
        "json_schema_extra": {
            "example": {"message": "你好，请介绍一下你自己", "session_id": None}
        }
    }


class ChatResponse(BaseModel):
    session_id: str
    reply: str = Field(description="本轮最终回复文本")
    messages: list[MessageOut] = Field(description="本轮新增的消息（含工具调用结果）")
    tool_calls: list[ToolCallRecord] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    elapsed_ms: float = Field(description="本轮耗时（毫秒）")
    created_at: datetime


class SessionCreateRequest(BaseModel):
    session_id: str | None = Field(
        default=None,
        pattern=_SESSION_ID_PATTERN,
        description="自定义会话 ID（可选），不传则自动生成",
    )
    title: str | None = Field(default=None, max_length=60, description="会话标题（可选）")


class SessionInfo(BaseModel):
    session_id: str
    title: str = Field(default="新对话", description="会话标题（默认取首句用户输入）")
    model: str | None = Field(default=None, description="建会话时使用的模型名")
    created_at: datetime
    last_active_at: datetime
    turns: int = Field(description="已完成的问答轮数")
    message_count: int = Field(description="会话中累积的消息条数")
    total_tokens: int = Field(default=0, description="该会话累计消耗的 token 数")


class SessionListResponse(BaseModel):
    total: int
    sessions: list[SessionInfo]
    limit: int = Field(default=50, description="本次请求的分页大小")
    offset: int = Field(default=0, description="本次请求的分页偏移")


class SessionDetail(SessionInfo):
    messages: list[MessageOut] = Field(default_factory=list)


# ------------------------------------------------------------ 人工确认（HITL）
class ActionRequestOut(BaseModel):
    """等待人工确认的一次工具调用。"""

    name: str = Field(description="工具名")
    args: dict[str, Any] = Field(default_factory=dict, description="模型给出的入参")
    description: str | None = Field(default=None, description="中间件生成的确认说明")


class ReviewConfigOut(BaseModel):
    """该工具允许的决策，前端据此决定按钮是否可用。"""

    action_name: str
    allowed_decisions: list[DecisionType] = Field(default_factory=list)


class InterruptInfo(BaseModel):
    """SSE ``interrupt`` 事件载荷：Agent 暂停，等待用户决策。"""

    session_id: str
    interrupt_id: str | None = Field(default=None, description="本次中断的 ID（仅用于排查）")
    action_requests: list[ActionRequestOut] = Field(default_factory=list)
    review_configs: list[ReviewConfigOut] = Field(default_factory=list)


class EditedAction(BaseModel):
    """``edit`` 决策里替换后的工具调用。"""

    name: str = Field(min_length=1, description="工具名（可改成别的工具）")
    args: dict[str, Any] = Field(default_factory=dict, description="修改后的入参")


class DecisionIn(BaseModel):
    """用户对某一次工具调用的决策。"""

    type: DecisionType = Field(description="approve 执行 / edit 改参数后执行 / reject 拒绝")
    edited_action: EditedAction | None = Field(
        default=None, description="仅 type=edit 时必填：修改后的工具名与入参"
    )
    message: str | None = Field(
        default=None, max_length=2000, description="仅 type=reject 时可选：拒绝原因，会回传给模型"
    )

    @model_validator(mode="after")
    def _require_edited_action(self) -> DecisionIn:
        if self.type == "edit" and self.edited_action is None:
            raise ValueError("type=edit 时必须提供 edited_action")
        return self


class ResumeRequest(BaseModel):
    """恢复一次被人工确认中断的对话。"""

    session_id: str = Field(
        pattern=_SESSION_ID_PATTERN, description="发生中断的会话 ID", examples=["sess_9f2c1a4b7d8e0f31"]
    )
    decisions: list[DecisionIn] = Field(
        min_length=1, description="按 ``interrupt`` 事件里 action_requests 的顺序逐条给出决策"
    )

    model_config = {
        "json_schema_extra": {
            "example": {
                "session_id": "sess_9f2c1a4b7d8e0f31",
                "decisions": [{"type": "approve"}],
            }
        }
    }


class StreamEventData(BaseModel):
    """SSE 事件载荷说明（仅用于文档展示）。"""

    event: Literal[
        "session", "token", "tool_call", "tool_result", "interrupt", "final", "error", "done"
    ] = Field(description="SSE 事件名")
    data: dict[str, Any] = Field(default_factory=dict, description="事件载荷")
