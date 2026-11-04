"""本地 Mock 对话模型：无需 API Key 即可端到端联调 Agent 流程。

行为约定（便于编写确定性测试）：
- 普通文本            -> 直接回显一段说明文字
- ``/calc <表达式>``   -> 发起 ``calculator`` 工具调用
- ``/time [时区]``     -> 发起 ``get_current_time`` 工具调用
- ``/search <关键词>`` -> 发起 ``search_knowledge_base`` 工具调用
- ``/ls [子目录]``     -> 发起 ``list_workspace_files`` 工具调用
- ``/read <路径>``     -> 发起 ``read_workspace_file`` 工具调用
- ``/write <路径> <内容>`` -> 发起 ``write_workspace_file`` 工具调用
- ``/grep <关键词>``   -> 发起 ``search_workspace`` 工具调用
- ``/web <关键词>``    -> 发起 ``web_search`` 工具调用（需外网）
- ``/cmd <命令>``      -> 发起 ``run_command`` 工具调用（需工作区可写）
- ``/py <代码>``       -> 发起 ``run_python`` 工具调用（需工作区可写）
- 已拿到工具结果后     -> 汇总工具输出作为最终答案
"""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.runnables import Runnable

_CALC_RE = re.compile(r"^/calc\s+(.+)$", re.IGNORECASE | re.DOTALL)
_TIME_RE = re.compile(r"^/time(?:\s+(\S+))?$", re.IGNORECASE)
_SEARCH_RE = re.compile(r"^/search\s+(.+)$", re.IGNORECASE | re.DOTALL)
_LS_RE = re.compile(r"^/ls(?:\s+(.+))?$", re.IGNORECASE)
_READ_RE = re.compile(r"^/read\s+(.+)$", re.IGNORECASE | re.DOTALL)
_WRITE_RE = re.compile(r"^/write\s+(\S+)\s+([\s\S]+)$", re.IGNORECASE)
_GREP_RE = re.compile(r"^/grep\s+(.+)$", re.IGNORECASE | re.DOTALL)
_WEB_RE = re.compile(r"^/web\s+(.+)$", re.IGNORECASE | re.DOTALL)
_CMD_RE = re.compile(r"^/cmd\s+([\s\S]+)$", re.IGNORECASE)
_PY_RE = re.compile(r"^/py\s+([\s\S]+)$", re.IGNORECASE)


class MockChatModel(BaseChatModel):
    """可离线运行的假模型，用于本地联调、示例与自动化测试。"""

    model_name: str = "mock-echo"
    chunk_size: int = 16

    @property
    def _llm_type(self) -> str:
        return "mock"

    # Mock 模型对工具协议是"形式绑定"，实际由提示词前缀触发，因此直接返回自身。
    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        return self

    # ---------- 核心逻辑 ----------
    def _build_reply(self, messages: list[BaseMessage]) -> AIMessage:
        tool_outputs = self._collect_tool_outputs(messages)
        if tool_outputs:
            joined = "\n".join(tool_outputs)
            return AIMessage(
                content=f"[mock/{self.model_name}] 已完成工具调用，结果如下：\n{joined}"
            )

        text = self._last_human_text(messages).strip()
        if not text:
            return AIMessage(content=f"[mock/{self.model_name}] 请提供需要处理的内容。")

        calc_match = _CALC_RE.match(text)
        if calc_match:
            return self._tool_call_message("calculator", {"expression": calc_match.group(1).strip()})

        time_match = _TIME_RE.match(text)
        if time_match:
            return self._tool_call_message(
                "get_current_time", {"timezone": (time_match.group(1) or "Asia/Shanghai")}
            )

        search_match = _SEARCH_RE.match(text)
        if search_match:
            return self._tool_call_message(
                "search_knowledge_base", {"query": search_match.group(1).strip()}
            )

        ls_match = _LS_RE.match(text)
        if ls_match:
            return self._tool_call_message(
                "list_workspace_files",
                {"path": (ls_match.group(1) or ".").strip(), "pattern": "*", "recursive": False},
            )

        read_match = _READ_RE.match(text)
        if read_match:
            return self._tool_call_message(
                "read_workspace_file", {"path": read_match.group(1).strip()}
            )

        write_match = _WRITE_RE.match(text)
        if write_match:
            return self._tool_call_message(
                "write_workspace_file",
                {"path": write_match.group(1).strip(), "content": write_match.group(2)},
            )

        grep_match = _GREP_RE.match(text)
        if grep_match:
            return self._tool_call_message(
                "search_workspace", {"query": grep_match.group(1).strip()}
            )

        web_match = _WEB_RE.match(text)
        if web_match:
            return self._tool_call_message(
                "web_search", {"query": web_match.group(1).strip(), "max_results": 5}
            )

        cmd_match = _CMD_RE.match(text)
        if cmd_match:
            return self._tool_call_message("run_command", {"command": cmd_match.group(1).strip()})

        py_match = _PY_RE.match(text)
        if py_match:
            return self._tool_call_message("run_python", {"code": py_match.group(1)})

        return AIMessage(
            content=(
                f"[mock/{self.model_name}] 收到：{text}\n\n"
                "当前为离线 Mock 模式（未调用真实大模型）。可尝试：\n"
                "- `/calc 2*(3+4)`、`/time Asia/Tokyo`、`/search LangGraph 记忆`\n"
                "- `/ls`、`/ls app`、`/read app/main.py`、`/grep FastAPI`、`/write notes.md 你好`\n"
                "- `/web langchain create_agent`（联网检索，需要外网）\n"
                "- `/cmd git status`、`/py print(1+1)`（终端能力，需要工作区可写）"
            )
        )

    @staticmethod
    def _last_human_text(messages: list[BaseMessage]) -> str:
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                return _as_text(message.content)
        return ""

    @staticmethod
    def _collect_tool_outputs(messages: list[BaseMessage]) -> list[str]:
        """取最近一轮用户消息之后的工具结果（说明 Agent 已完成一次工具调用）。"""
        outputs: list[str] = []
        for message in reversed(messages):
            if isinstance(message, HumanMessage):
                break
            if isinstance(message, ToolMessage):
                outputs.append(f"- {message.name or 'tool'}: {_as_text(message.content)}")
        outputs.reverse()
        return outputs

    @staticmethod
    def _tool_call_message(name: str, args: dict[str, Any]) -> AIMessage:
        return AIMessage(
            content="",
            tool_calls=[{"name": name, "args": args, "id": f"call_{uuid.uuid4().hex[:12]}"}],
        )

    # ---------- BaseChatModel 接口 ----------
    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        return ChatResult(generations=[ChatGeneration(message=self._build_reply(messages))])

    def _stream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> Iterator[ChatGenerationChunk]:
        message = self._build_reply(messages)
        if message.tool_calls:
            yield ChatGenerationChunk(
                message=AIMessageChunk(
                    content="",
                    tool_call_chunks=[
                        {
                            "name": call["name"],
                            "args": json.dumps(call.get("args") or {}, ensure_ascii=False),
                            "id": call.get("id") or f"call_{uuid.uuid4().hex[:12]}",
                            "index": index,
                        }
                        for index, call in enumerate(message.tool_calls)
                    ],
                )
            )
            return

        text = _as_text(message.content)
        for start in range(0, len(text), self.chunk_size):
            yield ChatGenerationChunk(
                message=AIMessageChunk(content=text[start : start + self.chunk_size])
            )

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        for chunk in self._stream(messages, stop=stop, **kwargs):
            yield chunk


def _as_text(content: Any) -> str:
    """把 LangChain 的 content（str 或多模态 list）统一成字符串。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return str(content)
