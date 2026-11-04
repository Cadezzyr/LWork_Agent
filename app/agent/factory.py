"""Agent 运行时：把模型、工具、提示词、记忆组装成 LangGraph 图。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.core.exceptions import AgentUnavailableError

try:  # pragma: no cover - 依赖版本不匹配时给出可操作的提示
    from langchain.agents import create_agent
    from langchain.agents.middleware import ToolCallLimitMiddleware,HumanInTheLoopMiddleware
    from langchain_core.language_models.chat_models import BaseChatModel
    from langchain_core.tools import BaseTool
    from langgraph.checkpoint.memory import InMemorySaver
except ImportError as exc:  # noqa: BLE001
    raise AgentUnavailableError(
        "LangChain 依赖导入失败："
        f"{type(exc).__name__}: {exc}\n"
        "最常见原因是 langgraph 与 langgraph-prebuilt 版本不匹配"
        "（例如 langgraph 1.0.8 搭配 langgraph-prebuilt 1.0.10 会报 "
        "cannot import name 'ExecutionInfo' from 'langgraph.runtime'）。\n"
        "请在项目所用解释器里执行 `pip install -r requirements.txt`，"
        "或手动对齐：pip install \"langgraph-prebuilt==1.0.7\" \"langgraph-checkpoint==4.0.0\"。"
    ) from exc

from app.agent.llm import build_chat_model
from app.agent.middleware import RepairDanglingToolCallsMiddleware, WorkspaceContextMiddleware
from app.agent.tools import get_tools, resolve_unknown_tools
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger("agent.factory")


@dataclass
class AgentRuntime:
    """一次进程内共享的 Agent 实例集合。

    - ``graph``：编译后的 LangGraph 状态图，支持 invoke / ainvoke / astream
    - ``checkpointer``：会话记忆存储（内存实现，进程重启即丢失）
    """

    settings: Settings
    graph: Any
    model: BaseChatModel
    tools: list[BaseTool]
    checkpointer: Any
    created_at: float = field(default_factory=time.time)

    @property
    def tool_names(self) -> list[str]:
        return [item.name for item in self.tools]

    @property
    def system_prompt(self) -> str:
        return self.settings.resolved_system_prompt

    def thread_config(self, session_id: str) -> dict[str, Any]:
        """LangGraph 按 thread_id 区分会话记忆。"""
        return {"configurable": {"thread_id": session_id}}


def build_agent_runtime(
    settings: Settings,
    *,
    checkpointer: Any | None = None,
    model: BaseChatModel | None = None,
) -> AgentRuntime:
    """构建 Agent 运行时；配置有问题时抛出 ``AgentUnavailableError``。

    ``model`` / ``checkpointer`` 主要给测试用：可以注入记录型模型或共享检查点，
    正常启动时留空即可。
    """
    unknown = resolve_unknown_tools(settings.tool_names)
    if unknown:
        logger.warning("以下工具未注册，已忽略：%s", ", ".join(unknown))

    tools = get_tools(settings.tool_names)
    for tool in tools:
        logger.info("tools:"+tool.name)
        #logger.info(tool.description)
    if not tools:
        logger.warning("当前没有任何可用工具，Agent 只能依靠模型自身知识回答")

    model = model or build_chat_model(settings)
    memory = checkpointer if checkpointer is not None else InMemorySaver()

    middleware = [
        # 必须放在最前面：修复历史里"孤立的 tool_calls"，否则上游 OpenAI 兼容接口会返回
        # 400 (An assistant message with 'tool_calls' must be followed by tool messages ...)。
        # 典型触发场景：用户中途点「停止」、进程在工具执行前崩溃。
        RepairDanglingToolCallsMiddleware(),
        # 单轮对话内允许的工具调用次数，防止工具死循环。
        # 这层在超限时会自动补上 ToolMessage，不会留下孤立调用；
        # 原来还挂过 ModelCallLimitMiddleware(exit_behavior="end")，它会在模型刚返回
        # tool_calls 时直接跳到结束，正好制造孤立调用，因此已移除（改用图自身的 recursion_limit 兜底）。
        ToolCallLimitMiddleware(run_limit=settings.max_tool_rounds, exit_behavior="continue"),
        # 把"当前工作区根目录/写权限"注入系统提示词（工作区由前端页面在运行时设置）
        WorkspaceContextMiddleware(),
        HumanInTheLoopMiddleware(
            interrupt_on={
                # "run_command": {
                #     "allowed_decisions": ["approve", "edit", "reject"],
                # },
                # "run_python":{
                #     "allowed_decisions": ["approve", "edit", "reject"],
                # },
                "calculator": False,
                "get_current_time":False,
                "search_knowledge_base":False,
                "list_workspace_files":False,
                "read_workspace_file":False,
                "search_workspace":False,
                "write_workspace_file":False,
                "edit_workspace_file":False,
                "delete_workspace_file":False,
                "web_search":False,
                "fetch_web_page":False,
                "ds_web_search":False,
                "run_python":False,
                "run_command":False,
                "load_skill":{
                     "allowed_decisions": ["approve", "edit", "reject"],
                 },
            }
        ),
        # FilesystemMiddleware(
        #     backend=None,  # Optional: custom backend (defaults to StateBackend)
        #     system_prompt="Write to the filesystem when...",  # Optional custom addition to the system prompt
        #     custom_tool_descriptions={
        #         "ls": "当使用该工具时,列出文件系统中的文件",
        #         "read_file": "当使用该工具时，读取整个文件或文件的特定行数",
        #         "write_file": "当使用该工具时，写入某个文件",
        #         "edit_file":"当使用该工具时，编辑某个文件"
        #     }  # Optional: Custom descriptions for filesystem tools
        # ),
    ]

    try:
        graph = create_agent(
            model=model,
            tools=tools,
            system_prompt=settings.resolved_system_prompt,
            middleware=middleware,
            checkpointer=memory,
            name=settings.agent_name,
        )
    except Exception as exc:
        raise AgentUnavailableError(f"构建 Agent 失败：{exc}") from exc

    logger.info(
        "Agent 已就绪 name=%s provider=%s model=%s tools=[%s]",
        settings.agent_name,
        settings.provider,
        settings.model,
        ", ".join(item.name for item in tools),
    )
    return AgentRuntime(
        settings=settings,
        graph=graph,
        model=model,
        tools=tools,
        checkpointer=memory,
    )
