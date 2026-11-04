"""数据库记录的数据类。

这一层刻意保持"笨"：只有字段与极少派生属性，不做校验、不做转换，
让 PostgreSQL 实现与内存实现共用同一套数据结构，避免两套行为漂移。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any


def utcnow() -> datetime:
    """统一的当前时间（带时区），与数据库 ``timestamptz`` 对齐。"""
    return datetime.now(UTC)


def as_aware(value: datetime | None) -> datetime | None:
    """把不带时区的时间按 UTC 补上时区，避免 naive/aware 比较报错。"""
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


@dataclass(slots=True)
class UserRecord:
    """``users`` 表的一行。"""

    id: str
    username: str
    password_hash: str
    email: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    status: int = 1
    role: str = "user"
    password_changed_at: datetime | None = None
    last_login_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status == 1

    @property
    def display(self) -> str:
        return self.display_name or self.username


@dataclass(slots=True)
class LoginSessionRecord:
    """``user_sessions`` 表的一行（服务端可撤销的登录态）。"""

    id: str
    user_id: str
    token_hash: str
    expires_at: datetime
    user_agent: str | None = None
    ip: str | None = None
    revoked_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime | None = None

    def is_active(self, *, now: datetime | None = None) -> bool:
        current = now or utcnow()
        if self.revoked_at is not None:
            return False
        expires_at = as_aware(self.expires_at)
        return expires_at is not None and expires_at > current


@dataclass(slots=True)
class ConversationRecord:
    """``conversations`` 表的一行。"""

    id: str
    user_id: str
    title: str = "新对话"
    model: str | None = None
    message_count: int = 0
    turn_count: int = 0
    total_tokens: int = 0
    pinned: bool = False
    last_active_at: datetime | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    deleted_at: datetime | None = None


@dataclass(slots=True)
class MessageInput:
    """待写入 ``messages`` 的一条消息（无 seq/id，由仓储层分配）。"""

    role: str
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    error_code: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int | None = None
    langchain_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class MessageRecord:
    """``messages`` 表的一行。"""

    conversation_id: str
    seq: int
    role: str
    content: str = ""
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    status: str = "ok"
    error_code: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    latency_ms: int | None = None
    langchain_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: datetime | None = None
    id: int | None = None


@dataclass(slots=True)
class UserSettingsRecord:
    """``user_settings`` 表的一行（用户级运行时偏好）。"""

    user_id: str
    workspace_root: str | None = None
    workspace_writable: bool = True
    theme: str | None = None
    settings: dict[str, Any] = field(default_factory=dict)
    updated_at: datetime | None = None


__all__ = [
    "ConversationRecord",
    "LoginSessionRecord",
    "MessageInput",
    "MessageRecord",
    "UserRecord",
    "UserSettingsRecord",
    "as_aware",
    "utcnow",
]
