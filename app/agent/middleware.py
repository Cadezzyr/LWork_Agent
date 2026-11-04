"""Agent 中间件：修复历史里"孤立的 tool_calls"，避免上游接口报 400。

OpenAI / DeepSeek 等 OpenAI 兼容接口有一条硬约束：
**带 `tool_calls` 的 assistant 消息，后面必须紧跟每个 `tool_call_id` 对应的 tool 消息。**
出现下面这些情况时，会话历史会破坏这个约束：

1. 前端点了「停止」或断开连接 —— 模型已经产出 `tool_calls`，但工具还没执行就被取消；
2. 中间件在模型返回 `tool_calls` 之后直接跳到结束（例如 ModelCallLimitMiddleware 的 end 行为）；
3. 进程在「模型已回复、工具未执行」之间崩溃。

一旦历史被污染，之后每次请求都会 400：

    An assistant message with 'tool_calls' must be followed by tool messages
    responding to each 'tool_call_id'. (insufficient tool messages following tool_calls message)

本模块的中间件在**每次调用模型之前**把缺失的 tool 消息补齐（内容标注为"未执行"），
让历史重新合法。它只修改发给模型的请求，不迁移、不改写已保存的状态，
所以对已经坏掉的会话也能立即自愈。
"""

from __future__ import annotations

from typing import Any

from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from app.core.logging import get_logger

logger = get_logger("agent.middleware")

INTERRUPTED_TOOL_RESULT = "（该工具调用未执行：会话被中断或已取消，因此没有返回内容）"


def find_dangling_tool_calls(messages: list[BaseMessage]) -> list[str]:
    """返回缺少 tool 响应的 `tool_call_id` 列表（按出现顺序）。"""
    missing: list[str] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        index += 1
        if not (isinstance(message, AIMessage) and message.tool_calls):
            continue
        answered: set[str] = set()
        while index < total and isinstance(messages[index], ToolMessage):
            tool_call_id = messages[index].tool_call_id
            if tool_call_id:
                answered.add(tool_call_id)
            index += 1
        for call in message.tool_calls:
            call_id = call.get("id")
            if call_id and call_id not in answered:
                missing.append(call_id)
    return missing


def repair_dangling_tool_calls(
    messages: list[BaseMessage],
    *,
    placeholder: str = INTERRUPTED_TOOL_RESULT,
) -> list[BaseMessage]:
    """补齐缺失的 tool 消息；历史本身合法时原样返回。

    Args:
        messages: 待发送给模型的消息列表。
        placeholder: 补进去的 tool 消息内容。

    Returns:
        新的消息列表；历史本身合法时直接返回入参本身（不做任何拷贝）。
    """
    if not find_dangling_tool_calls(messages):
        return messages

    repaired: list[BaseMessage] = []
    index = 0
    total = len(messages)
    while index < total:
        message = messages[index]
        repaired.append(message)
        index += 1
        if not (isinstance(message, AIMessage) and message.tool_calls):
            continue

        answered: set[str] = set()
        while index < total and isinstance(messages[index], ToolMessage):
            tool_message = messages[index]
            if tool_message.tool_call_id:
                answered.add(tool_message.tool_call_id)
            repaired.append(tool_message)
            index += 1

        for call in message.tool_calls:
            call_id = call.get("id")
            if not call_id or call_id in answered:
                continue
            repaired.append(
                ToolMessage(
                    content=placeholder,
                    tool_call_id=call_id,
                    name=call.get("name"),
                    status="error",
                )
            )
    return repaired


class RepairDanglingToolCallsMiddleware(AgentMiddleware):
    """在调用模型前补齐孤立的 tool_calls（跨 OpenAI 兼容接口的硬约束）。"""

    def _repair(self, request: Any) -> list[BaseMessage]:
        messages = list(request.messages or [])
        missing = find_dangling_tool_calls(messages)
        if not missing:
            return messages
        logger.warning(
            "检测到 %s 个未完成的工具调用（%s），已补齐占位结果后继续",
            len(missing),
            ", ".join(missing[:5]),
        )
        return repair_dangling_tool_calls(messages)

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(request.override(messages=self._repair(request)))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(request.override(messages=self._repair(request)))


WORKSPACE_NOTE_TEMPLATE = (
    "\n\n【工作区信息】当前可操作的目录是 {root}（写权限：{writes}）。"
    "所有文件类工具的路径都相对于该目录，且不能越界到目录之外；"
    "用户在其他目录提到文件时，先确认工作区是否设对。"
)


class WorkspaceContextMiddleware(AgentMiddleware):
    """每次调用模型前，把**当前**工作区根目录写进系统提示词。

    工作区根目录由前端页面在运行时设置，可能随时变化，所以不能固化在提示词里。
    """

    def _with_note(self, request: Any) -> Any:
        from langchain_core.messages import SystemMessage

        from app.agent.workspace import get_workspace_manager

        try:
            snapshot = get_workspace_manager().info(with_count=False)
            note = WORKSPACE_NOTE_TEMPLATE.format(
                root=snapshot.root,
                writes="开启" if snapshot.allow_writes else "关闭（只能读）",
            )
        except Exception as exc:  # pragma: no cover - 防御性分支
            logger.warning("读取工作区信息失败，本轮不注入工作区提示：%s", exc)
            return request

        system_message = getattr(request, "system_message", None)
        if system_message is None:
            return request.override(system_message=SystemMessage(content=note.strip()))
        content = system_message.content
        if isinstance(content, str):
            new_content: Any = content + note
        else:
            new_content = list(content) + [{"type": "text", "text": note}]
        return request.override(system_message=SystemMessage(content=new_content))

    def wrap_model_call(self, request: Any, handler: Any) -> Any:
        return handler(self._with_note(request))

    async def awrap_model_call(self, request: Any, handler: Any) -> Any:
        return await handler(self._with_note(request))


__all__ = [
    "INTERRUPTED_TOOL_RESULT",
    "RepairDanglingToolCallsMiddleware",
    "WorkspaceContextMiddleware",
    "find_dangling_tool_calls",
    "repair_dangling_tool_calls",
]
