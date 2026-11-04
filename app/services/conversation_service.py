"""会话与消息的持久化服务。

职责边界
--------
- **只做数据**：建会话、写消息、查历史、软删除、标题维护；
- **不碰模型**：LangGraph 调用、流式编排都在 ``AgentService``；
- 让"数据库是对话历史的唯一事实来源"这件事收敛在一个类里，
  将来换成 PostgresSaver 或其它存储时只改这里。

并发：``conversations.append_messages`` 内部对会话行加锁（PostgreSQL 用 ``FOR UPDATE``，
内存实现用 ``asyncio.Lock``），因此同一会话的并发写入不会产生重复 ``seq``。
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from langchain_core.messages import BaseMessage

from app.core.config import Settings
from app.core.exceptions import ConflictError, SessionNotFoundError
from app.core.logging import get_logger
from app.db.base import Repositories
from app.db.models import ConversationRecord, MessageInput, MessageRecord, utcnow
from app.schemas.chat import SessionDetail, SessionInfo
from app.services import messages as msg_utils

logger = get_logger("services.conversations")

#: 会话标题最大长度（与 conversations.title VARCHAR(120) 留足余量）
_TITLE_LIMIT = 60
#: 列表接口分页上限，防止一次拉全表
MAX_PAGE_SIZE = 200


class ConversationService:
    def __init__(self, repositories: Repositories, settings: Settings) -> None:
        self._repos = repositories
        self._settings = settings

    # ------------------------------------------------------------------ 工具方法
    @staticmethod
    def new_session_id() -> str:
        """生成会话 ID（同时用作 LangGraph 的 thread_id）。"""
        return f"sess_{uuid.uuid4().hex[:16]}"

    @staticmethod
    def title_from_message(text: str, *, limit: int = 30) -> str:
        """用首句用户输入生成标题（压缩空白 + 截断）。"""
        cleaned = " ".join(str(text or "").split())
        if not cleaned:
            return "新对话"
        return cleaned if len(cleaned) <= limit else cleaned[:limit] + "…"

    def to_info(self, record: ConversationRecord) -> SessionInfo:
        created = record.created_at or utcnow()
        return SessionInfo(
            session_id=record.id,
            title=record.title or "新对话",
            model=record.model,
            created_at=created,
            last_active_at=record.last_active_at or created,
            turns=record.turn_count,
            message_count=record.message_count,
            total_tokens=record.total_tokens,
        )

    # ------------------------------------------------------------------ 会话 CRUD
    async def create(
        self,
        user_id: str,
        *,
        title: str | None = None,
        session_id: str | None = None,
        model: str | None = None,
    ) -> ConversationRecord:
        """创建会话；``session_id`` 已存在且属于当前用户时直接复用。"""
        resolved_id = session_id or self.new_session_id()
        if session_id:
            existing = await self._repos.conversations.get(user_id, resolved_id)
            if existing is not None:
                return existing

        try:
            record = await self._repos.conversations.create(
                conversation_id=resolved_id,
                user_id=user_id,
                title=(title or "新对话")[:_TITLE_LIMIT],
                model=model or self._settings.model,
            )
        except ConflictError as exc:
            # 该 ID 已被别人（或已删除的会话）占用：不泄露细节，只提示换一个
            raise ConflictError("该会话 ID 已被占用，请换一个") from exc
        logger.info("为用户 %s 创建会话 %s", user_id, record.id)
        return record

    async def get(self, user_id: str, session_id: str) -> ConversationRecord:
        record = await self._repos.conversations.get(user_id, session_id)
        if record is None:
            raise SessionNotFoundError(f"会话不存在或无权访问：{session_id}")
        return record

    async def list(
        self,
        user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        keyword: str | None = None,
    ) -> tuple[list[SessionInfo], int]:
        limit = max(1, min(int(limit), MAX_PAGE_SIZE))
        offset = max(0, int(offset))
        records, total = await self._repos.conversations.list(
            user_id,
            limit=limit,
            offset=offset,
            keyword=keyword,
        )
        return [self.to_info(item) for item in records], total

    async def detail(self, user_id: str, session_id: str) -> SessionDetail:
        record = await self.get(user_id, session_id)
        stored = await self._repos.conversations.list_messages(user_id, session_id)
        info = self.to_info(record)
        return SessionDetail(
            **info.model_dump(),
            messages=msg_utils.records_to_out(stored),
        )

    async def history(self, user_id: str, session_id: str) -> list[BaseMessage]:
        """按 seq 顺序还原 LangChain 消息（用于重建模型上下文）。"""
        stored = await self._repos.conversations.list_messages(user_id, session_id)
        return msg_utils.from_records(stored)

    async def delete(self, user_id: str, session_id: str) -> None:
        removed = await self._repos.conversations.soft_delete(user_id, session_id)
        if not removed:
            raise SessionNotFoundError(f"会话不存在或无权访问：{session_id}")
        logger.info("用户 %s 删除了会话 %s", user_id, session_id)

    async def rename(self, user_id: str, session_id: str, title: str) -> SessionInfo:
        cleaned = (title or "").strip()[:_TITLE_LIMIT]
        if not cleaned:
            raise ConflictError("标题不能为空")
        updated = await self._repos.conversations.rename(user_id, session_id, cleaned)
        if not updated:
            raise SessionNotFoundError(f"会话不存在或无权访问：{session_id}")
        return self.to_info(await self.get(user_id, session_id))

    # -------------------------------------------------------------------- 写消息
    async def append_messages(
        self,
        user_id: str,
        session_id: str,
        messages: Sequence[MessageInput],
        *,
        turn_increment: int = 0,
        title: str | None = None,
    ) -> list[MessageRecord]:
        if not messages:
            return []
        return await self._repos.conversations.append_messages(
            user_id,
            session_id,
            messages,
            turn_increment=turn_increment,
            title=title,
        )

    async def append_user_message(
        self,
        user_id: str,
        session_id: str,
        content: str,
        *,
        model: str | None = None,
    ) -> MessageRecord:
        """先落库用户提问：即使本轮模型调用失败/被中断，历史里也留得下这句话。"""
        records = await self.append_messages(
            user_id,
            session_id,
            [MessageInput(role="user", content=content, model=model)],
        )
        return records[0]


__all__ = ["MAX_PAGE_SIZE", "ConversationService"]
