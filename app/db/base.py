"""仓储层接口（Protocol）与容器。

服务层只依赖这里的协议，不关心底层是 PostgreSQL 还是内存实现：
- 生产/联调：``PostgresRepositories``（真实落库）
- 单元测试 / 未配置数据库：``InMemoryRepositories``（进程内存，接口语义一致）

每个方法都是 ``async``，PostgreSQL 实现内部会把同步驱动丢进线程池。
所有"按用户查询"的方法都必须显式传入 ``user_id``——把越权风险收敛到一处。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.db.models import (
    ConversationRecord,
    LoginSessionRecord,
    MessageInput,
    MessageRecord,
    UserRecord,
    UserSettingsRecord,
)


class UserRepository(Protocol):
    async def create(
        self,
        *,
        username: str,
        password_hash: str,
        email: str | None = None,
        display_name: str | None = None,
        role: str = "user",
    ) -> UserRecord: ...

    async def get_by_id(self, user_id: str) -> UserRecord | None: ...

    async def get_by_login(self, identifier: str) -> UserRecord | None:
        """按用户名或邮箱查找（大小写不敏感）。"""
        ...

    async def username_exists(self, username: str) -> bool: ...

    async def email_exists(self, email: str) -> bool: ...

    async def touch_login(self, user_id: str) -> None: ...

    async def update_password_hash(
        self,
        user_id: str,
        password_hash: str,
        *,
        bump_password_changed_at: bool = True,
    ) -> None:
        """更新密码哈希。

        ``bump_password_changed_at=True`` 时同时刷新 ``password_changed_at``——
        这正是"改密后旧 token 全部失效"的判定依据；仅在**哈希算法升级重算**时传 False，
        否则会把刚签发的 token 一起作废。
        """
        ...

    async def set_status(self, user_id: str, status: int) -> None:
        """启用/禁用账号（禁用后不能登录，但仍可查历史）。"""
        ...


class LoginSessionRepository(Protocol):
    async def create(
        self,
        *,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> LoginSessionRecord: ...

    async def get_by_id(self, session_id: str) -> LoginSessionRecord | None: ...

    async def get_by_token_hash(self, token_hash: str) -> LoginSessionRecord | None: ...

    async def rotate_token(self, session_id: str, token_hash: str, expires_at: datetime) -> None:
        """刷新时轮换 refresh token（旧的立即失效）。"""
        ...

    async def touch(self, session_id: str) -> None: ...

    async def revoke(self, session_id: str) -> bool: ...

    async def revoke_all(self, user_id: str) -> int: ...


class ConversationRepository(Protocol):
    async def create(
        self,
        *,
        conversation_id: str,
        user_id: str,
        title: str,
        model: str | None = None,
        metadata: dict | None = None,
    ) -> ConversationRecord: ...

    async def get(self, user_id: str, conversation_id: str) -> ConversationRecord | None:
        """按 (user_id, id) 取；不在该用户名下时返回 ``None``（即越权视同不存在）。"""
        ...

    async def list(
        self,
        user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        keyword: str | None = None,
    ) -> tuple[list[ConversationRecord], int]: ...

    async def list_messages(self, user_id: str, conversation_id: str) -> list[MessageRecord]: ...

    async def append_messages(
        self,
        user_id: str,
        conversation_id: str,
        messages: Sequence[MessageInput],
        *,
        turn_increment: int = 0,
        title: str | None = None,
    ) -> list[MessageRecord]:
        """在一个事务里追加消息、分配 seq、维护计数与最近活跃时间。"""
        ...

    async def rename(self, user_id: str, conversation_id: str, title: str) -> bool: ...

    async def soft_delete(self, user_id: str, conversation_id: str) -> bool: ...


class UserSettingsRepository(Protocol):
    async def get(self, user_id: str) -> UserSettingsRecord | None: ...

    async def upsert(
        self,
        user_id: str,
        *,
        workspace_root: str | None = None,
        workspace_writable: bool | None = None,
        theme: str | None = None,
        settings: dict | None = None,
    ) -> UserSettingsRecord: ...


@dataclass(slots=True)
class Repositories:
    """一次进程内的仓储集合（由 main.lifespan 构建后挂在 ``app.state``）。"""

    users: UserRepository
    login_sessions: LoginSessionRepository
    conversations: ConversationRepository
    user_settings: UserSettingsRepository
    backend: str = "memory"

    @property
    def is_persistent(self) -> bool:
        return self.backend == "postgres"


__all__ = [
    "ConversationRepository",
    "LoginSessionRepository",
    "Repositories",
    "UserRepository",
    "UserSettingsRepository",
]
