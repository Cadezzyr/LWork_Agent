"""孤立 tool_calls 的修复逻辑测试。

背景：OpenAI / DeepSeek 等接口要求「带 tool_calls 的 assistant 消息后面必须紧跟对应的
tool 消息」。用户中途停止生成、进程在工具执行前崩溃都会破坏这个约束，之后再请求就会 400。
"""

from __future__ import annotations

import asyncio
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from app.agent.factory import build_agent_runtime
from app.agent.middleware import (
    INTERRUPTED_TOOL_RESULT,
    find_dangling_tool_calls,
    repair_dangling_tool_calls,
)
from app.core.config import Settings
from app.db import Repositories
from app.db.memory import build_in_memory_repositories
from app.db.models import MessageInput
from app.services.agent_service import AgentService
from app.services.conversation_service import ConversationService

TEST_USER_ID = "11111111-1111-1111-1111-111111111111"


def _build_service(settings: Settings, model: Any) -> AgentService:
    """构造一套"内存仓储 + 指定模型"的 AgentService（不依赖数据库）。"""
    runtime = build_agent_runtime(settings, model=model)
    users, sessions, conversations, user_settings = build_in_memory_repositories()
    repositories = Repositories(
        users=users,
        login_sessions=sessions,
        conversations=conversations,
        user_settings=user_settings,
        backend="memory",
    )
    return AgentService(
        runtime,
        ConversationService(repositories, settings),
        settings=settings,
    )


def _ai_with_calls(*call_ids: str) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {"name": "calculator", "args": {"expression": "1+1"}, "id": call_id}
            for call_id in call_ids
        ],
    )


def assert_openai_tool_sequence(messages: list[BaseMessage]) -> None:
    """按 OpenAI 的规则校验消息序列：tool_calls 必须被 tool 消息逐条应答。"""
    index = 0
    while index < len(messages):
        message = messages[index]
        if not (isinstance(message, AIMessage) and message.tool_calls):
            index += 1
            continue
        expected = {call.get("id") for call in message.tool_calls if call.get("id")}
        answered: set[str] = set()
        index += 1
        while index < len(messages) and isinstance(messages[index], ToolMessage):
            answered.add(messages[index].tool_call_id or "")
            index += 1
        missing = expected - answered
        assert not missing, f"缺少工具响应: {missing}"


# --------------------------------------------------------------- 纯函数单测


def test_valid_history_is_returned_unchanged() -> None:
    messages: list[BaseMessage] = [
        HumanMessage(content="算 1+1"),
        _ai_with_calls("call_1"),
        ToolMessage(content="2", tool_call_id="call_1", name="calculator"),
        AIMessage(content="结果是 2"),
    ]
    assert find_dangling_tool_calls(messages) == []
    assert repair_dangling_tool_calls(messages) is messages


def test_single_dangling_call_is_filled() -> None:
    messages: list[BaseMessage] = [HumanMessage(content="算 1+1"), _ai_with_calls("call_1")]
    missing = find_dangling_tool_calls(messages)
    assert missing == ["call_1"]

    repaired = repair_dangling_tool_calls(messages)
    assert len(repaired) == 3
    assert isinstance(repaired[-1], ToolMessage)
    assert repaired[-1].tool_call_id == "call_1"
    assert repaired[-1].status == "error"
    assert repaired[-1].content == INTERRUPTED_TOOL_RESULT
    assert_openai_tool_sequence(repaired)


def test_partially_answered_calls_only_fill_the_missing_one() -> None:
    messages: list[BaseMessage] = [
        HumanMessage(content="并行算两次"),
        _ai_with_calls("call_a", "call_b"),
        ToolMessage(content="1", tool_call_id="call_a", name="calculator"),
    ]
    assert find_dangling_tool_calls(messages) == ["call_b"]
    repaired = repair_dangling_tool_calls(messages)
    assert [m.tool_call_id for m in repaired if isinstance(m, ToolMessage)] == ["call_a", "call_b"]
    assert_openai_tool_sequence(repaired)


def test_multiple_turns_are_each_repaired() -> None:
    messages: list[BaseMessage] = [
        HumanMessage(content="第一轮"),
        _ai_with_calls("call_1"),
        HumanMessage(content="第二轮"),
        _ai_with_calls("call_2"),
        AIMessage(content="好"),
    ]
    assert find_dangling_tool_calls(messages) == ["call_1", "call_2"]
    repaired = repair_dangling_tool_calls(messages)
    assert_openai_tool_sequence(repaired)
    assert len(repaired) == len(messages) + 2


def test_history_without_tool_calls_is_untouched() -> None:
    messages: list[BaseMessage] = [HumanMessage(content="你好"), AIMessage(content="你好呀")]
    assert repair_dangling_tool_calls(messages) is messages


# ------------------------------------------------------- 与 Agent 的集成测试


class RecordingModel(BaseChatModel):
    """记录模型实际收到的消息，用于验证中间件在调用前完成了修复。"""

    requests: list[list[BaseMessage]] = []

    @property
    def _llm_type(self) -> str:
        return "recording"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # noqa: ANN401
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        RecordingModel.requests.append(list(messages))
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content="继续处理完毕"))])


def test_agent_repairs_corrupted_session_before_calling_model(settings: Settings) -> None:
    RecordingModel.requests = []
    service = _build_service(settings, RecordingModel())
    runtime = service.runtime
    session_id = "broken_session"
    config = runtime.thread_config(session_id)

    # 人为制造"模型返回了 tool_calls，但工具从未执行"的历史
    asyncio.run(
        runtime.graph.aupdate_state(
            config,
            {
                "messages": [
                    HumanMessage(content="帮我算 1+1", id="u1"),
                    AIMessage(
                        content="",
                        id="a1",
                        tool_calls=[
                            {"name": "calculator", "args": {"expression": "1+1"}, "id": "call_x"}
                        ],
                    ),
                ]
            },
        )
    )
    asyncio.run(service.conversations.create(TEST_USER_ID, session_id=session_id))

    response = asyncio.run(
        service.chat(user_id=TEST_USER_ID, message="继续", session_id=session_id)
    )
    assert response.reply == "继续处理完毕"

    first_request = RecordingModel.requests[0]
    # 模型收到的是合法序列（否则真实 OpenAI 兼容接口会直接 400）
    assert_openai_tool_sequence(first_request)
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "call_x"
        for message in first_request
    )


def test_streaming_path_also_repairs(settings: Settings) -> None:
    """错误实际发生在流式接口，这条用例专门覆盖 astream 路径。"""
    RecordingModel.requests = []
    service = _build_service(settings, RecordingModel())
    runtime = service.runtime
    session_id = "broken_stream"
    config = runtime.thread_config(session_id)

    asyncio.run(
        runtime.graph.aupdate_state(
            config,
            {
                "messages": [
                    HumanMessage(content="算 3+3", id="u3"),
                    AIMessage(
                        content="",
                        id="a3",
                        tool_calls=[
                            {"name": "calculator", "args": {"expression": "3+3"}, "id": "call_z"}
                        ],
                    ),
                ]
            },
        )
    )
    asyncio.run(service.conversations.create(TEST_USER_ID, session_id=session_id))

    async def collect() -> list[str]:
        events: list[str] = []
        async for event, _data in service.chat_stream(
            user_id=TEST_USER_ID, message="继续", session_id=session_id
        ):
            events.append(event)
        return events

    events = asyncio.run(collect())
    assert "final" in events and events[-1] == "done"
    assert_openai_tool_sequence(RecordingModel.requests[0])
    assert any(
        isinstance(message, ToolMessage) and message.tool_call_id == "call_z"
        for message in RecordingModel.requests[0]
    )


def test_session_detail_exposes_repaired_history(settings: Settings) -> None:
    """对外展示的历史来自数据库：工具调用与结果都原样带出，不需要现场修复。"""
    RecordingModel.requests = []
    service = _build_service(settings, RecordingModel())
    session_id = "broken_detail"

    asyncio.run(service.conversations.create(TEST_USER_ID, session_id=session_id))
    asyncio.run(
        service.conversations.append_messages(
            TEST_USER_ID,
            session_id,
            [
                MessageInput(role="user", content="算 2+2"),
                MessageInput(
                    role="assistant",
                    content="",
                    tool_calls=[
                        {
                            "id": "call_y",
                            "name": "calculator",
                            "args": {"expression": "2+2"},
                            "result": None,
                            "status": "success",
                        }
                    ],
                ),
            ],
            turn_increment=1,
        )
    )

    detail = asyncio.run(service.conversations.detail(TEST_USER_ID, session_id))
    roles = [message.role for message in detail.messages]
    assert roles == ["user", "assistant"]
    # 未完成的工具调用仍然作为"待执行/无结果"呈现，前端会显示占位文案
    assert detail.messages[-1].tool_calls[0].name == "calculator"
    assert detail.messages[-1].tool_calls[0].result is None

