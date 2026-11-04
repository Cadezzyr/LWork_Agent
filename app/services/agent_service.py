"""智能体业务服务：会话编排、同步调用、流式调用、对话落库。

与重构前的关键差异
------------------
1. **一切按用户隔离**：每个入口都要 ``user_id``，会话归属校验在
   ``ConversationService.get`` 里完成（越权一律 404）。
2. **数据库是唯一事实来源**：用户提问先落库、助手回复（含工具轨迹与 token 用量）
   在模型返回后落库；``InMemorySaver`` 只作为"当前进程的上下文缓存"。
3. **上下文可重建**：进程重启导致 checkpointer 为空时，自动用数据库里的历史
   拼出新一次调用的输入，因此重启后仍能接着聊（历史不丢）。
4. **并发安全**：同一会话的多次请求由进程内 ``asyncio.Lock`` 串行化，
   ``seq`` 由仓储层在事务内分配，不会撞号。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Sequence
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    ToolMessage,
)
from langgraph.types import Command

from app.agent.factory import AgentRuntime
from app.agent.workspace import bind_workspace_user
from app.core.config import Settings
from app.core.exceptions import (
    AppError,
    ConflictError,
    DatabaseUnavailableError,
    ModelProviderError,
    ValidationError,
)
from app.core.logging import get_logger
from app.db.models import ConversationRecord, MessageRecord, utcnow
from app.schemas.chat import ChatResponse, DecisionIn, ToolCallRecord
from app.services import messages as msg_utils
from app.services.conversation_service import ConversationService

logger = get_logger("services.agent")

StreamChunk = tuple[str, dict[str, Any]]


class AgentService:
    """封装 Agent 运行时的调用、会话生命周期与消息持久化。"""

    def __init__(
        self,
        runtime: AgentRuntime,
        conversations: ConversationService,
        *,
        settings: Settings | None = None,
        max_sessions: int = 1000,
    ) -> None:
        self.runtime = runtime
        self.settings = settings or runtime.settings
        self.max_sessions = max_sessions
        self.conversations = conversations
        # 会话锁：只保留"当前活跃"的锁对象，避免无限增长
        self._locks: OrderedDict[str, asyncio.Lock] = OrderedDict()
        self._guard = asyncio.Lock()

    # ------------------------------------------------------------------ 会话准备
    async def prepare_conversation(
        self,
        user_id: str,
        *,
        session_id: str | None,
        message: str,
    ) -> ConversationRecord:
        """定位或创建本轮对话（``session_id`` 给了就必须属于当前用户）。"""
        if session_id:
            return await self.conversations.get(user_id, session_id)
        return await self.conversations.create(
            user_id,
            title=ConversationService.title_from_message(message),
            model=self.settings.model,
        )

    async def _lock_for(self, session_id: str) -> asyncio.Lock:
        async with self._guard:
            lock = self._locks.get(session_id)
            if lock is None:
                lock = asyncio.Lock()
                self._locks[session_id] = lock
                self._evict_locks()
            else:
                self._locks.move_to_end(session_id)
            return lock

    def _evict_locks(self) -> None:
        """只清理"没人持有"的锁，正在用的锁一旦丢弃就会破坏串行化。"""
        while len(self._locks) > self.max_sessions:
            for key in list(self._locks.keys()):
                candidate = self._locks[key]
                if not candidate.locked():
                    self._locks.pop(key, None)
                    break
            else:  # 全都在用，放弃本次清理
                return

    async def _snapshot(self, session_id: str) -> Any:
        """读取 checkpointer 里的会话快照（进程重启后为空）。"""
        config = self.runtime.thread_config(session_id)
        try:
            return await self.runtime.graph.aget_state(config)
        except Exception as exc:  # pragma: no cover - 仅防御性分支
            logger.warning("读取会话上下文失败 %s: %s", session_id, exc)
            return None

    @staticmethod
    def _pending_interrupt(snapshot: Any) -> Any | None:
        """取出当前挂起的中断（HumanInTheLoopMiddleware 的确认请求）。"""
        interrupts = getattr(snapshot, "interrupts", None) or ()
        return interrupts[0] if interrupts else None

    @classmethod
    def _snapshot_messages(cls, snapshot: Any) -> list[BaseMessage]:
        values = getattr(snapshot, "values", None) or {}
        history = values.get("messages") if isinstance(values, dict) else None
        return list(history or [])

    async def _load_memory_history(self, session_id: str) -> list[BaseMessage]:
        """从 checkpointer 读取上下文（仅当前进程有效，重启后为空）。"""
        return self._snapshot_messages(await self._snapshot(session_id))

    @staticmethod
    def _new_message_id() -> str:
        return f"lc_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def _turn_messages(
        known: list[BaseMessage],
        history: list[BaseMessage],
        *,
        human_id: str,
    ) -> list[BaseMessage]:
        """本轮新增的消息（不含已单独落库的用户提问）。"""
        candidates = msg_utils.diff_messages(known, history)
        return [item for item in candidates if getattr(item, "id", None) != human_id]

    async def _persist_turn(
        self,
        user_id: str,
        session_id: str,
        new_messages: list[BaseMessage],
        *,
        latency_ms: float,
        turn_increment: int = 1,
    ) -> list[MessageRecord]:
        """把本轮助手消息（含工具调用结果）写入数据库。"""
        payload = msg_utils.to_persist_inputs(
            new_messages,
            model=self.settings.model,
            latency_ms=int(latency_ms),
        )
        if not payload:
            return []
        try:
            return await self.conversations.append_messages(
                user_id,
                session_id,
                payload,
                turn_increment=turn_increment,
            )
        except AppError:
            # 已知的业务异常（例如会话刚被删除）原样抛出，保留正确的 code/状态码
            raise
        except Exception as exc:
            # 落库失败必须让调用方知道：否则用户以为历史保住了。
            # 统一翻译成 503，避免把驱动层的原始异常暴露成 500。
            logger.exception("对话落库失败 session=%s", session_id)
            raise DatabaseUnavailableError(
                f"对话历史保存失败：{type(exc).__name__}: {str(exc).strip()}",
            ) from exc

    async def _persist_turn_safely(
        self,
        user_id: str,
        session_id: str,
        new_messages: list[BaseMessage],
        *,
        latency_ms: float,
        turn_increment: int = 1,
    ) -> str | None:
        """落库但不打断已经生成的回答：返回错误描述（成功时 ``None``）。"""
        try:
            await self._persist_turn(
                user_id,
                session_id,
                new_messages,
                latency_ms=latency_ms,
                turn_increment=turn_increment,
            )
        except Exception as exc:  # noqa: BLE001 - 回答已经生成，先给用户，再报保存失败
            return f"{type(exc).__name__}: {exc}"
        return None

    # -------------------------------------------------------------- 同步调用
    async def chat(
        self,
        *,
        user_id: str,
        message: str,
        session_id: str | None = None,
    ) -> ChatResponse:
        conversation = await self.prepare_conversation(
            user_id,
            session_id=session_id,
            message=message,
        )
        lock = await self._lock_for(conversation.id)

        async with lock:
            config = self.runtime.thread_config(conversation.id)
            human = HumanMessage(content=message, id=self._new_message_id())
            memory = await self._load_memory_history(conversation.id)
            replay = [] if memory else await self.conversations.history(user_id, conversation.id)

            # 1) 先把用户提问落库：即使接下来模型报错，历史里也留得下这句话
            user_record = await self.conversations.append_user_message(
                user_id,
                conversation.id,
                message,
                model=self.settings.model,
            )

            started = time.perf_counter()
            try:
                with bind_workspace_user(user_id):
                    result = await self.runtime.graph.ainvoke(
                        {"messages": [*replay, human]},
                        config,
                    )
            except Exception as exc:
                logger.exception("Agent 调用失败 session=%s", conversation.id)
                raise ModelProviderError(
                    f"模型调用失败：{type(exc).__name__}: {exc}",
                    details={"session_id": conversation.id},
                ) from exc
            elapsed_ms = (time.perf_counter() - started) * 1000

            history = list(result.get("messages") or [])
            new_messages = self._turn_messages(
                memory or replay,
                history,
                human_id=human.id,
            )

            reply = msg_utils.extract_reply(new_messages) or msg_utils.extract_reply(history)
            usage = msg_utils.accumulate_usage(new_messages)
            tool_calls = msg_utils.extract_tool_calls(new_messages)

            # 2) 助手回复 + 工具轨迹落库
            await self._persist_turn(user_id, conversation.id, new_messages, latency_ms=elapsed_ms)

            logger.info(
                "会话 %s 完成一轮：耗时 %.0fms，新增消息 %s 条，工具调用 %s 次",
                conversation.id,
                elapsed_ms,
                len(new_messages),
                len(tool_calls),
            )
            return ChatResponse(
                session_id=conversation.id,
                reply=reply,
                # 与重构前保持一致：本轮新增消息含用户提问（取自刚写入的那条记录）
                messages=[
                    msg_utils.message_to_out(user_record),
                    *msg_utils.to_messages_out(new_messages),
                ],
                tool_calls=tool_calls,
                usage=usage,
                elapsed_ms=round(elapsed_ms, 2),
                # 本轮的响应时间（不是会话创建时间）
                created_at=utcnow(),
            )

    # -------------------------------------------------------------- 流式调用
    async def chat_stream(
        self,
        *,
        user_id: str,
        message: str,
        session_id: str | None = None,
        conversation: ConversationRecord | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """产出 (event, data) 序列，由 HTTP 层转成 SSE。

        ``conversation`` 可以由路由层事先解析并传入：这样"会话不存在/无权访问"
        会在**响应开始之前**以 404 抛出，而不是变成一个已经返回 200 的残缺 SSE 流
        （异步生成器里的异常发生在响应头发出之后，HTTP 状态码已经无法更改）。
        """
        if conversation is None:
            conversation = await self.prepare_conversation(
                user_id,
                session_id=session_id,
                message=message,
            )
        lock = await self._lock_for(conversation.id)

        async with lock:
            config = self.runtime.thread_config(conversation.id)
            human = HumanMessage(content=message, id=self._new_message_id())

            yield "session", {
                "session_id": conversation.id,
                "title": conversation.title,
                "model": conversation.model,
                "created_at": record_created_at(conversation).isoformat(),
            }

            memory = await self._load_memory_history(conversation.id)
            replay = [] if memory else await self.conversations.history(user_id, conversation.id)

            # 用户提问先落库，保证"中断/报错也有记录"
            await self.conversations.append_user_message(
                user_id,
                conversation.id,
                message,
                model=self.settings.model,
            )

            started = time.perf_counter()
            try:
                async for event, data in self._stream_graph(
                    {"messages": [*replay, human]},
                    config,
                    session_id=conversation.id,
                    user_id=user_id,
                ):
                    yield event, data
            except asyncio.CancelledError:
                # 客户端断开：checkpointer 里可能留下"只有 tool_calls 没有结果"的上下文，
                # 下一轮由 RepairDanglingToolCallsMiddleware 自动补齐。
                logger.info(
                    "客户端断开，会话 %s 的流式调用已取消（用户提问已落库）",
                    conversation.id,
                )
                raise
            except Exception as exc:
                logger.exception("流式调用失败 session=%s", conversation.id)
                yield "error", {
                    "code": "model_provider_error",
                    "message": f"模型调用失败：{type(exc).__name__}: {exc}",
                }
                return

            elapsed_ms = (time.perf_counter() - started) * 1000
            snapshot = await self._snapshot(conversation.id)
            history = self._snapshot_messages(snapshot)
            new_messages = self._turn_messages(
                memory or replay,
                history,
                human_id=human.id,
            )

            if self._pending_interrupt(snapshot) is not None:
                # 暂停在人工确认：把"模型提出的工具调用"先落库，刷新页面后仍能看到提案
                persist_error = await self._persist_turn_safely(
                    user_id,
                    conversation.id,
                    new_messages,
                    latency_ms=elapsed_ms,
                )
                if persist_error is not None:
                    yield "error", {
                        "code": "history_not_saved",
                        "message": f"工具调用提案未能保存：{persist_error}",
                    }
                yield "done", {"session_id": conversation.id, "interrupted": True}
                return

            reply = msg_utils.extract_reply(new_messages) or msg_utils.extract_reply(history)
            tool_calls: list[ToolCallRecord] = msg_utils.extract_tool_calls(new_messages)
            usage = msg_utils.accumulate_usage(new_messages)

            persist_error = await self._persist_turn_safely(
                user_id,
                conversation.id,
                new_messages,
                latency_ms=elapsed_ms,
            )

            yield "final", {
                "session_id": conversation.id,
                "reply": reply,
                "tool_calls": [item.model_dump() for item in tool_calls],
                "usage": usage.model_dump(),
                "elapsed_ms": round(elapsed_ms, 2),
                "saved": persist_error is None,
            }
            if persist_error is not None:
                yield "error", {
                    "code": "history_not_saved",
                    "message": f"回答已生成，但对话历史保存失败：{persist_error}",
                }
            yield "done", {"session_id": conversation.id}

    # ---------------------------------------------------- 人工确认（HITL）恢复
    async def prepare_resume(
        self,
        user_id: str,
        *,
        session_id: str,
        decisions: Sequence[DecisionIn],
    ) -> tuple[ConversationRecord, dict[str, Any]]:
        """校验会话归属与待确认的动作，返回可直接喂给图的恢复载荷。

        这些校验必须在 SSE 响应开始**之前**完成，否则错误只能变成流里的 error 事件。
        """
        conversation = await self.conversations.get(user_id, session_id)
        pending = self._pending_interrupt(await self._snapshot(conversation.id))
        if pending is None:
            raise ConflictError(
                "当前会话没有待确认的工具调用：可能已被处理、被新的提问覆盖，或服务重启后状态丢失"
            )
        return conversation, build_resume_payload(getattr(pending, "value", None), decisions)

    async def chat_resume_stream(
        self,
        *,
        user_id: str,
        conversation: ConversationRecord,
        resume: dict[str, Any],
    ) -> AsyncIterator[StreamChunk]:
        """按用户的 approve / edit / reject 决策继续被中断的那一轮对话。"""
        lock = await self._lock_for(conversation.id)

        async with lock:
            config = self.runtime.thread_config(conversation.id)
            memory = await self._load_memory_history(conversation.id)

            started = time.perf_counter()
            try:
                async for event, data in self._stream_graph(
                    Command(resume=resume),
                    config,
                    session_id=conversation.id,
                    user_id=user_id,
                ):
                    yield event, data
            except asyncio.CancelledError:
                logger.info("客户端断开，会话 %s 的恢复调用已取消", conversation.id)
                raise
            except Exception as exc:
                logger.exception("恢复调用失败 session=%s", conversation.id)
                yield "error", {
                    "code": "model_provider_error",
                    "message": f"模型调用失败：{type(exc).__name__}: {exc}",
                }
                return

            elapsed_ms = (time.perf_counter() - started) * 1000
            snapshot = await self._snapshot(conversation.id)
            if self._pending_interrupt(snapshot) is not None:
                # 模型在恢复后又提出了新的待确认调用，继续等用户决策
                yield "done", {"session_id": conversation.id, "interrupted": True}
                return

            history = self._snapshot_messages(snapshot)
            new_messages = self._turn_messages(memory, history, human_id="")

            reply = msg_utils.extract_reply(new_messages) or msg_utils.extract_reply(history)
            # 恢复后新增的往往只有 ToolMessage：把对应的（中断时已落库的）AI 消息一起带上，
            # 才能还原完整的「调用 + 结果」轨迹（工具名与入参原本就在那条 AI 消息里）
            answered = {
                item.tool_call_id
                for item in new_messages
                if isinstance(item, ToolMessage) and item.tool_call_id
            }
            tool_calls: list[ToolCallRecord] = msg_utils.extract_tool_calls(
                [
                    item
                    for item in history
                    if isinstance(item, AIMessage)
                    and any(call.get("id") in answered for call in item.tool_calls)
                ]
                + new_messages
            )
            usage = msg_utils.accumulate_usage(new_messages)

            # 恢复的是同一轮对话，不再增加轮数
            persist_error = await self._persist_turn_safely(
                user_id,
                conversation.id,
                new_messages,
                latency_ms=elapsed_ms,
                turn_increment=0,
            )

            yield "final", {
                "session_id": conversation.id,
                "reply": reply,
                "tool_calls": [item.model_dump() for item in tool_calls],
                "usage": usage.model_dump(),
                "elapsed_ms": round(elapsed_ms, 2),
                "saved": persist_error is None,
            }
            if persist_error is not None:
                yield "error", {
                    "code": "history_not_saved",
                    "message": f"回答已生成，但对话历史保存失败：{persist_error}",
                }
            yield "done", {"session_id": conversation.id}

    async def _stream_graph(
        self,
        graph_input: Any,
        config: dict[str, Any],
        *,
        session_id: str,
        user_id: str,
    ) -> AsyncIterator[StreamChunk]:
        """把图的原始增量翻译成对外事件（token / tool_call / tool_result / interrupt）。

        运行期间把当前用户绑定到协程上下文：工具与提示词中间件据此取到
        **该用户自己**的工作区（而不是全进程共享的那一份）。
        """
        with bind_workspace_user(user_id):
            async for mode, payload in self.runtime.graph.astream(
                graph_input,
                config,
                stream_mode=["messages", "updates"],
            ):
                if mode == "messages":
                    chunk, metadata = payload  # type: ignore[misc]
                    node = (metadata or {}).get("langgraph_node")
                    # 只转发模型输出的增量文本；工具节点返回的是工具结果，
                    # 由下面的 tool_result 事件专门承载。
                    if isinstance(chunk, AIMessageChunk) and node != "tools":
                        text = msg_utils.as_text(getattr(chunk, "content", ""))
                        if text:
                            yield "token", {"content": text, "node": node}
                elif mode == "updates":
                    interrupt = self._interrupt_event(payload, session_id)
                    if interrupt is not None:
                        yield "interrupt", interrupt
                        continue
                    for event, data in self._tool_events(payload):
                        yield event, data

    @staticmethod
    def _interrupt_event(update: Any, session_id: str) -> dict[str, Any] | None:
        """把 ``__interrupt__`` 增量转成前端可直接渲染的确认请求。"""
        if not isinstance(update, dict) or "__interrupt__" not in update:
            return None
        interrupts = update.get("__interrupt__") or ()
        if not interrupts:
            return None
        pending = interrupts[0]
        value = getattr(pending, "value", None)
        value = value if isinstance(value, dict) else {}
        return {
            "session_id": session_id,
            "interrupt_id": getattr(pending, "id", None),
            "action_requests": [
                {
                    "name": item.get("name") or "unknown",
                    "args": dict(item.get("args") or {}),
                    "description": item.get("description"),
                }
                for item in (value.get("action_requests") or [])
                if isinstance(item, dict)
            ],
            "review_configs": [
                {
                    "action_name": item.get("action_name") or "unknown",
                    "allowed_decisions": list(item.get("allowed_decisions") or []),
                }
                for item in (value.get("review_configs") or [])
                if isinstance(item, dict)
            ],
        }

    @staticmethod
    def _tool_events(update: Any) -> list[StreamChunk]:
        """把 LangGraph ``updates`` 增量转成工具相关事件。"""
        events: list[StreamChunk] = []
        if not isinstance(update, dict):
            return events
        for _, delta in update.items():
            if not isinstance(delta, dict):
                continue
            for message in delta.get("messages") or []:
                if isinstance(message, AIMessage) and message.tool_calls:
                    for call in message.tool_calls:
                        events.append(
                            (
                                "tool_call",
                                {
                                    "id": call.get("id"),
                                    "name": call.get("name") or "unknown",
                                    "args": dict(call.get("args") or {}),
                                },
                            )
                        )
                elif isinstance(message, ToolMessage):
                    events.append(
                        (
                            "tool_result",
                            {
                                "id": message.tool_call_id,
                                "name": message.name or "unknown",
                                "status": getattr(message, "status", "success"),
                                "content": msg_utils.as_text(message.content),
                            },
                        )
                    )
        return events


def record_created_at(conversation: ConversationRecord):
    """会话创建时间（内存实现可能缺省，兜底用当前时间）。"""
    return conversation.created_at or utcnow()


def build_resume_payload(value: Any, decisions: Sequence[DecisionIn]) -> dict[str, Any]:
    """把用户的决策转成 ``HumanInTheLoopMiddleware`` 期望的 resume 载荷。

    决策必须与待确认的 ``action_requests`` 一一对应（顺序一致），
    且类型必须在对应工具的 ``allowed_decisions`` 里，否则直接 400。
    """
    request = value if isinstance(value, dict) else {}
    action_requests = [
        item for item in (request.get("action_requests") or []) if isinstance(item, dict)
    ]
    configs = [item for item in (request.get("review_configs") or []) if isinstance(item, dict)]

    if len(decisions) != len(action_requests):
        raise ValidationError(
            f"待确认的工具调用有 {len(action_requests)} 个，但收到 {len(decisions)} 条决策"
        )

    payload: list[dict[str, Any]] = []
    for index, decision in enumerate(decisions):
        config = configs[index] if index < len(configs) else {}
        allowed = list(config.get("allowed_decisions") or [])
        action_name = config.get("action_name") or action_requests[index].get("name") or "unknown"
        if allowed and decision.type not in allowed:
            raise ValidationError(
                f"工具 {action_name} 只允许 {'/'.join(allowed)}，不支持 {decision.type}"
            )

        item: dict[str, Any] = {"type": decision.type}
        if decision.type == "edit":
            edited = decision.edited_action
            assert edited is not None  # DecisionIn 已校验
            item["edited_action"] = {
                "name": edited.name or action_name,
                "args": dict(edited.args or {}),
            }
        elif decision.type == "reject" and decision.message:
            item["message"] = decision.message
        payload.append(item)

    return {"decisions": payload}


__all__ = ["AgentService", "StreamChunk", "build_resume_payload"]
