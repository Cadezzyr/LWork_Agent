"""LangChain 消息 <-> API 数据模型的转换工具。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)

from app.db.models import MessageInput, MessageRecord
from app.schemas.chat import MessageOut, ToolCallRecord, Usage


def as_text(content: Any) -> str:
    """把 message.content（str 或多模态块列表）统一成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text", "")))
                elif "text" in block:
                    parts.append(str(block["text"]))
        return "".join(parts)
    if content is None:
        return ""
    return str(content)


def role_of(message: BaseMessage) -> str:
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, AIMessage):
        return "assistant"
    if isinstance(message, ToolMessage):
        return "tool"
    if isinstance(message, SystemMessage):
        return "system"
    return "assistant"


def to_message_out(message: BaseMessage) -> MessageOut:
    tool_calls: list[ToolCallRecord] = []
    if isinstance(message, AIMessage):
        for call in message.tool_calls:
            tool_calls.append(
                ToolCallRecord(
                    id=call.get("id"),
                    name=call.get("name") or "unknown",
                    args=dict(call.get("args") or {}),
                )
            )
    return MessageOut(
        role=role_of(message),  # type: ignore[arg-type]
        content=as_text(message.content),
        name=getattr(message, "name", None),
        tool_calls=tool_calls,
    )


def to_messages_out(messages: list[BaseMessage]) -> list[MessageOut]:
    return [to_message_out(item) for item in messages]


def extract_tool_calls(messages: list[BaseMessage]) -> list[ToolCallRecord]:
    """汇总本轮的工具调用，并把对应的 ``ToolMessage`` 结果回填进去。"""
    records: dict[str, ToolCallRecord] = {}
    ordered: list[ToolCallRecord] = []

    for message in messages:
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                record = ToolCallRecord(
                    id=call.get("id"),
                    name=call.get("name") or "unknown",
                    args=dict(call.get("args") or {}),
                )
                ordered.append(record)
                key = call.get("id") or f"{record.name}-{len(ordered)}"
                records[key] = record
        elif isinstance(message, ToolMessage):
            status = getattr(message, "status", "success")
            text = as_text(message.content)
            target = records.get(message.tool_call_id or "")
            if target is not None:
                target.result = text
                target.status = "error" if status == "error" else "success"
            else:
                ordered.append(
                    ToolCallRecord(
                        id=message.tool_call_id,
                        name=message.name or "unknown",
                        result=text,
                        status="error" if status == "error" else "success",
                    )
                )
    return ordered


def accumulate_usage(messages: list[BaseMessage]) -> Usage:
    """累加 ``usage_metadata``；上游未返回 token 统计时保持 0。"""
    input_tokens = 0
    output_tokens = 0
    total_tokens = 0
    for message in messages:
        metadata = getattr(message, "usage_metadata", None)
        if isinstance(metadata, dict):
            input_tokens += int(metadata.get("input_tokens") or 0)
            output_tokens += int(metadata.get("output_tokens") or 0)
            total_tokens += int(metadata.get("total_tokens") or 0)
    if not total_tokens:
        total_tokens = input_tokens + output_tokens
    return Usage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=total_tokens,
    )


def extract_reply(messages: list[BaseMessage]) -> str:
    """取最后一条 AI 消息的文本作为本轮回复。"""
    for message in reversed(messages):
        if isinstance(message, AIMessage):
            text = as_text(message.content).strip()
            if text:
                return text
    # 兜底：没有任何 AI 文本时，返回最后一条消息内容
    for message in reversed(messages):
        text = as_text(message.content).strip()
        if text:
            return text
    return ""


def diff_messages(
    before: list[BaseMessage],
    after: list[BaseMessage],
) -> list[BaseMessage]:
    """计算本轮新增的消息。

    优先按消息 ID 判重（LangGraph 会为每条消息分配 ID），
    遇到没有 ID 的消息则退化为按长度切片。
    """
    if not before:
        return list(after)
    if all(getattr(item, "id", None) for item in before) and all(
        getattr(item, "id", None) for item in after
    ):
        seen = {item.id for item in before}
        return [item for item in after if item.id not in seen]
    return list(after[len(before) :])


# ---------------------------------------------------------------- 落库（写路径）
def to_persist_inputs(
    messages: Sequence[BaseMessage],
    *,
    model: str | None = None,
    latency_ms: int | None = None,
) -> list[MessageInput]:
    """把本轮新增的 LangChain 消息转成待写入 ``messages`` 表的记录。

    工具调用的结果会回填到对应 assistant 行的 ``tool_calls`` JSONB 里，
    这样前端/审计只查一张表就能看到"调了什么、返回了什么"。
    """
    # tool_call_id -> (结果文本, 是否失败)
    tool_results: dict[str, tuple[str, str]] = {}
    for message in messages:
        if isinstance(message, ToolMessage) and message.tool_call_id:
            status = "error" if getattr(message, "status", "success") == "error" else "ok"
            tool_results[message.tool_call_id] = (as_text(message.content), status)

    inputs: list[MessageInput] = []
    last_assistant_index: int | None = None

    for message in messages:
        tool_calls: list[dict[str, Any]] = []
        if isinstance(message, AIMessage):
            for call in message.tool_calls:
                call_id = call.get("id")
                matched = tool_results.get(call_id or "")
                tool_calls.append(
                    {
                        "id": call_id,
                        "name": call.get("name") or "unknown",
                        "args": dict(call.get("args") or {}),
                        "result": matched[0] if matched else None,
                        "status": "error" if matched and matched[1] == "error" else "success",
                    }
                )

        is_tool_message = isinstance(message, ToolMessage)
        inputs.append(
            MessageInput(
                role=role_of(message),
                content=as_text(message.content),
                name=getattr(message, "name", None),
                tool_call_id=getattr(message, "tool_call_id", None) if is_tool_message else None,
                tool_calls=tool_calls,
                status="error" if getattr(message, "status", None) == "error" else "ok",
                model=model,
                langchain_id=getattr(message, "id", None),
            )
        )
        if isinstance(message, AIMessage):
            last_assistant_index = len(inputs) - 1

    # token 用量挂在"本轮最后一条 assistant 消息"上（上游通常只在最后一条返回 usage）
    usage = accumulate_usage(list(messages))
    if last_assistant_index is not None:
        record = inputs[last_assistant_index]
        record.input_tokens = usage.input_tokens
        record.output_tokens = usage.output_tokens
        record.total_tokens = usage.total_tokens
        record.latency_ms = latency_ms

    return inputs


# -------------------------------------------------------------- 回放（读路径）
def from_records(records: Sequence[MessageRecord]) -> list[BaseMessage]:
    """把数据库里的消息还原成 LangChain 消息，用于重建模型上下文。"""
    messages: list[BaseMessage] = []
    for record in records:
        message_id = record.langchain_id or f"db_{record.seq}"
        if record.role == "user":
            messages.append(
                HumanMessage(content=record.content, name=record.name, id=message_id)
            )
        elif record.role == "assistant":
            tool_calls = [
                {
                    "id": (call or {}).get("id"),
                    "name": (call or {}).get("name") or "unknown",
                    "args": dict((call or {}).get("args") or {}),
                }
                for call in (record.tool_calls or [])
                if isinstance(call, dict)
            ]
            messages.append(
                AIMessage(
                    content=record.content,
                    name=record.name,
                    tool_calls=tool_calls,
                    id=message_id,
                )
            )
        elif record.role == "tool":
            messages.append(
                ToolMessage(
                    content=record.content,
                    tool_call_id=record.tool_call_id or "",
                    name=record.name or "tool",
                    id=message_id,
                )
            )
        elif record.role == "system":
            messages.append(SystemMessage(content=record.content, id=message_id))
    return messages


def message_to_out(record: MessageRecord) -> MessageOut:
    """数据库消息 -> 对外模型（工具调用结果已存在 ``tool_calls`` 里）。"""
    calls = [
        ToolCallRecord(
            id=(call or {}).get("id"),
            name=(call or {}).get("name") or "unknown",
            args=dict((call or {}).get("args") or {}),
            result=(call or {}).get("result"),
            status="error" if (call or {}).get("status") == "error" else "success",
        )
        for call in (record.tool_calls or [])
        if isinstance(call, dict)
    ]
    return MessageOut(
        role=record.role,  # type: ignore[arg-type]
        content=record.content,
        name=record.name,
        tool_calls=calls,
    )


def records_to_out(records: Sequence[MessageRecord]) -> list[MessageOut]:
    return [message_to_out(item) for item in records]
