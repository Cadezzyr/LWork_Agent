"""智能体元信息与健康检查模型。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolInfo(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict, description="参数的 JSON Schema")


class AgentInfo(BaseModel):
    app_name: str
    version: str
    env: str
    provider: str
    model: str
    base_url: str | None = None
    temperature: float
    max_tokens: int | None = None
    agent_name: str
    system_prompt: str
    tools: list[ToolInfo] = Field(default_factory=list)
    ready: bool = Field(description="模型配置是否就绪（缺失 API Key 时为 False）")
    readiness_error: str | None = Field(default=None, description="未就绪的原因")
    max_tool_rounds: int
    streaming: bool = True


class ComponentStatus(BaseModel):
    status: Literal["ok", "degraded", "error"]
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded"]
    app_name: str
    version: str
    env: str
    uptime_seconds: float
    components: dict[str, ComponentStatus] = Field(default_factory=dict)
