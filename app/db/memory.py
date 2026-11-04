"""内存仓储实现。

两个用途：
1. **自动化测试**：无需数据库即可覆盖登录、越权隔离、历史回放等逻辑；
2. **未配置 ``AGENT_DATABASE_URL`` 时的本地开发兜底**（启动日志会明确告警：重启即丢）。

语义严格对齐 PostgreSQL 实现：唯一性冲突抛 ``ConflictError``、
越权访问返回 ``None``/``False``、``append_messages`` 原子地分配 seq 并维护计数。
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from app.core.exceptions import ConflictError, SessionNotFoundError
from app.db.models import (
    ConversationRecord,
    LoginSessionRecord,
    MessageInput,
    MessageRecord,
    UserRecord,
    UserSettingsRecord,
    utcnow,
)


class InMemoryUserRepository:
    def __init__(self) -> None:
        self._users: dict[str, UserRecord] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        username: str,
        password_hash: str,
        email: str | None = None,
        display_name: str | None = None,
        role: str = "user",
    ) -> UserRecord:
        async with self._lock:
            lowered = username.lower()
            if any(user.username.lower() == lowered for user in self._users.values()):
                raise ConflictError("该用户名已被占用")
            if email:
                if any(
                    user.email and user.email.lower() == email.lower()
                    for user in self._users.values()
                ):
                    raise ConflictError("该邮箱已被注册")
            now = utcnow()
            record = UserRecord(
                id=str(uuid.uuid4()),
                username=username,
                password_hash=password_hash,
                email=email,
                display_name=display_name,
                status=1,
                role=role,
                password_changed_at=now,
                created_at=now,
                updated_at=now,
            )
            self._users[record.id] = record
            return copy.deepcopy(record)

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        record = self._users.get(str(user_id))
        return copy.deepcopy(record) if record else None

    async def get_by_login(self, identifier: str) -> UserRecord | None:
        target = identifier.strip().lower()
        for record in self._users.values():
            if record.username.lower() == target:
                return copy.deepcopy(record)
            if record.email and record.email.lower() == target:
                return copy.deepcopy(record)
        return None

    async def username_exists(self, username: str) -> bool:
        lowered = username.lower()
        return any(user.username.lower() == lowered for user in self._users.values())

    async def email_exists(self, email: str) -> bool:
        lowered = email.lower()
        return any(user.email and user.email.lower() == lowered for user in self._users.values())

    async def touch_login(self, user_id: str) -> None:
        record = self._users.get(str(user_id))
        if record is not None:
            record.last_login_at = utcnow()
            record.updated_at = utcnow()

    async def update_password_hash(
        self,
        user_id: str,
        password_hash: str,
        *,
        bump_password_changed_at: bool = True,
    ) -> None:
        record = self._users.get(str(user_id))
        if record is not None:
            now = utcnow()
            record.password_hash = password_hash
            if bump_password_changed_at:
                record.password_changed_at = now
            record.updated_at = now

    async def set_status(self, user_id: str, status: int) -> None:
        record = self._users.get(str(user_id))
        if record is not None:
            record.status = int(status)
            record.updated_at = utcnow()


class InMemoryLoginSessionRepository:
    def __init__(self) -> None:
        self._sessions: dict[str, LoginSessionRecord] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> LoginSessionRecord:
        async with self._lock:
            record = LoginSessionRecord(
                id=str(uuid.uuid4()),
                user_id=str(user_id),
                token_hash=token_hash,
                expires_at=expires_at,
                user_agent=user_agent,
                ip=ip,
                last_used_at=utcnow(),
                created_at=utcnow(),
            )
            self._sessions[record.id] = record
            return copy.deepcopy(record)

    async def get_by_id(self, session_id: str) -> LoginSessionRecord | None:
        record = self._sessions.get(str(session_id))
        return copy.deepcopy(record) if record else None

    async def get_by_token_hash(self, token_hash: str) -> LoginSessionRecord | None:
        for record in self._sessions.values():
            if record.token_hash == token_hash:
                return copy.deepcopy(record)
        return None

    async def rotate_token(self, session_id: str, token_hash: str, expires_at: datetime) -> None:
        record = self._sessions.get(str(session_id))
        if record is not None and record.revoked_at is None:
            record.token_hash = token_hash
            record.expires_at = expires_at
            record.last_used_at = utcnow()

    async def touch(self, session_id: str) -> None:
        record = self._sessions.get(str(session_id))
        if record is not None:
            record.last_used_at = utcnow()

    async def revoke(self, session_id: str) -> bool:
        record = self._sessions.get(str(session_id))
        if record is None or record.revoked_at is not None:
            return False
        record.revoked_at = utcnow()
        return True

    async def revoke_all(self, user_id: str) -> int:
        count = 0
        for record in self._sessions.values():
            if record.user_id == str(user_id) and record.revoked_at is None:
                record.revoked_at = utcnow()
                count += 1
        return count


class InMemoryConversationRepository:
    def __init__(self) -> None:
        self._conversations: dict[str, ConversationRecord] = {}
        self._messages: dict[str, list[MessageRecord]] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        *,
        conversation_id: str,
        user_id: str,
        title: str,
        model: str | None = None,
        metadata: dict | None = None,
    ) -> ConversationRecord:
        async with self._lock:
            if conversation_id in self._conversations:
                # 与 PostgreSQL 的主键冲突语义保持一致
                raise ConflictError("该会话 ID 已被占用，请换一个")
            now = utcnow()
            record = ConversationRecord(
                id=conversation_id,
                user_id=str(user_id),
                title=title,
                model=model,
                last_active_at=now,
                created_at=now,
                updated_at=now,
            )
            self._conversations[conversation_id] = record
            self._messages.setdefault(conversation_id, [])
            return copy.deepcopy(record)

    async def get(self, user_id: str, conversation_id: str) -> ConversationRecord | None:
        record = self._conversations.get(conversation_id)
        if record is None or record.user_id != str(user_id) or record.deleted_at is not None:
            return None
        return copy.deepcopy(record)

    async def list(
        self,
        user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        keyword: str | None = None,
    ) -> tuple[list[ConversationRecord], int]:
        pattern = keyword.strip().lower() if keyword and keyword.strip() else None
        owned = [
            record
            for record in self._conversations.values()
            if record.user_id == str(user_id)
            and record.deleted_at is None
            and (pattern is None or pattern in record.title.lower())
        ]
        owned.sort(key=lambda item: item.last_active_at or datetime.min, reverse=True)
        page = owned[offset : offset + limit]
        return [copy.deepcopy(item) for item in page], len(owned)

    async def list_messages(self, user_id: str, conversation_id: str) -> list[MessageRecord]:
        record = await self.get(user_id, conversation_id)
        if record is None:
            return []
        return [copy.deepcopy(item) for item in self._messages.get(conversation_id, [])]

    async def append_messages(
        self,
        user_id: str,
        conversation_id: str,
        messages: Sequence[MessageInput],
        *,
        turn_increment: int = 0,
        title: str | None = None,
    ) -> list[MessageRecord]:
        if not messages:
            return []
        async with self._lock:
            record = self._conversations.get(conversation_id)
            if record is None or record.user_id != str(user_id) or record.deleted_at is not None:
                raise SessionNotFoundError(f"会话不存在或无权访问：{conversation_id}")

            bucket = self._messages.setdefault(conversation_id, [])
            next_seq = (bucket[-1].seq if bucket else 0) + 1
            now = utcnow()
            inserted: list[MessageRecord] = []
            for item in messages:
                stored = MessageRecord(
                    conversation_id=conversation_id,
                    seq=next_seq,
                    role=item.role,
                    content=item.content,
                    name=item.name,
                    tool_call_id=item.tool_call_id,
                    tool_calls=copy.deepcopy(item.tool_calls),
                    status=item.status,
                    error_code=item.error_code,
                    model=item.model,
                    input_tokens=item.input_tokens,
                    output_tokens=item.output_tokens,
                    total_tokens=item.total_tokens,
                    latency_ms=item.latency_ms,
                    langchain_id=item.langchain_id,
                    metadata=copy.deepcopy(item.metadata),
                    created_at=now,
                    id=len(bucket) + 1,
                )
                bucket.append(stored)
                inserted.append(copy.deepcopy(stored))
                next_seq += 1

            record.message_count += len(messages)
            record.turn_count += max(0, turn_increment)
            record.total_tokens += sum(item.total_tokens for item in messages)
            record.last_active_at = now
            record.updated_at = now
            if title:
                record.title = title
            return inserted

    async def rename(self, user_id: str, conversation_id: str, title: str) -> bool:
        record = await self.get(user_id, conversation_id)
        if record is None:
            return False
        async with self._lock:
            stored = self._conversations[conversation_id]
            stored.title = title
            stored.updated_at = utcnow()
        return True

    async def soft_delete(self, user_id: str, conversation_id: str) -> bool:
        record = await self.get(user_id, conversation_id)
        if record is None:
            return False
        async with self._lock:
            stored = self._conversations[conversation_id]
            stored.deleted_at = utcnow()
            stored.updated_at = utcnow()
        return True


class InMemoryUserSettingsRepository:
    def __init__(self) -> None:
        self._settings: dict[str, UserSettingsRecord] = {}

    async def get(self, user_id: str) -> UserSettingsRecord | None:
        record = self._settings.get(str(user_id))
        return copy.deepcopy(record) if record else None

    async def upsert(
        self,
        user_id: str,
        *,
        workspace_root: str | None = None,
        workspace_writable: bool | None = None,
        theme: str | None = None,
        settings: dict | None = None,
    ) -> UserSettingsRecord:
        record = self._settings.get(str(user_id))
        if record is None:
            record = UserSettingsRecord(user_id=str(user_id))
            self._settings[str(user_id)] = record
        if workspace_root is not None:
            record.workspace_root = workspace_root
        if workspace_writable is not None:
            record.workspace_writable = workspace_writable
        if theme is not None:
            record.theme = theme
        if settings is not None:
            record.settings = {**record.settings, **settings}
        record.updated_at = utcnow()
        return copy.deepcopy(record)


def build_in_memory_repositories() -> tuple[Any, Any, Any, Any]:
    """返回与 PostgreSQL 实现同构的四个内存仓储。"""
    return (
        InMemoryUserRepository(),
        InMemoryLoginSessionRepository(),
        InMemoryConversationRepository(),
        InMemoryUserSettingsRepository(),
    )


__all__ = [
    "InMemoryConversationRepository",
    "InMemoryLoginSessionRepository",
    "InMemoryUserRepository",
    "InMemoryUserSettingsRepository",
    "build_in_memory_repositories",
]
