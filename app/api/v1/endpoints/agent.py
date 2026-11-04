"""智能体元信息接口。"""

from __future__ import annotations

from fastapi import APIRouter

from app.agent.tools import describe_tools
from app.api.deps import CurrentUserDep, RuntimeDep, SettingsDep
from app.schemas.agent import AgentInfo, ToolInfo

router = APIRouter(prefix="/agent", tags=["智能体"])


@router.get("/info", response_model=AgentInfo, summary="当前智能体配置")
async def agent_info(
    settings: SettingsDep,
    runtime: RuntimeDep,
    current: CurrentUserDep,
) -> AgentInfo:
    """返回模型、提示词、工具等配置（已脱敏，不含 API Key）。

    需要登录：系统提示词与工具清单属于服务端内部配置，不对外公开。
    """
    readiness_error = settings.readiness_error()
    return AgentInfo(
        app_name=settings.app_name,
        version=settings.version,
        env=settings.env,
        provider=settings.provider,
        model=settings.model,
        base_url=settings.base_url_value,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        agent_name=settings.agent_name,
        system_prompt=runtime.system_prompt,
        tools=[ToolInfo(**item) for item in describe_tools(runtime.tools)],
        ready=readiness_error is None,
        readiness_error=readiness_error,
        max_tool_rounds=settings.max_tool_rounds,
    )


@router.get("/tools", response_model=list[ToolInfo], summary="可用工具列表")
async def list_tools(runtime: RuntimeDep, current: CurrentUserDep) -> list[ToolInfo]:
    """列出当前注册的工具及其参数 JSON Schema，便于前端/调试端展示。"""
    return [ToolInfo(**item) for item in describe_tools(runtime.tools)]
