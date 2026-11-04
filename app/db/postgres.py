"""PostgreSQL 仓储实现（手写 SQL，与 ``docs/db/schema.postgres.sql`` 一一对应）。

约定
----
- 所有查询方法都带 ``user_id`` 条件：越权访问返回 ``None``/``False`` 而不是抛错，
  HTTP 层统一翻译成 404，避免"存在性探测"。
- 多语句的写操作放在同一个 ``Database.run`` 回调里 => 同一连接、同一事务。
- jsonb 字段用 ``psycopg2.extras.Json`` 显式包装，dict/list 直接入库。
"""

from __future__ import annotations

import ipaddress
import uuid
from collections.abc import Sequence
from datetime import datetime
from typing import Any

from psycopg2.extras import Json

from app.core.exceptions import ConflictError, DatabaseUnavailableError, SessionNotFoundError
from app.db.database import Database
from app.db.models import (
    ConversationRecord,
    LoginSessionRecord,
    MessageInput,
    MessageRecord,
    UserRecord,
    UserSettingsRecord,
)

_JSON_FIELDS_EMPTY: dict[str, Any] = {}


# ---------------------------------------------------------------- 行 -> 数据类
def _text(value: Any) -> str | None:
    return None if value is None else str(value)


def _row_to_user(row: dict[str, Any]) -> UserRecord:
    return UserRecord(
        id=str(row["id"]),
        username=str(row["username"]),
        password_hash=str(row["password_hash"]),
        email=_text(row.get("email")),
        display_name=_text(row.get("display_name")),
        avatar_url=_text(row.get("avatar_url")),
        status=int(row.get("status") or 0),
        role=str(row.get("role") or "user"),
        password_changed_at=row.get("password_changed_at"),
        last_login_at=row.get("last_login_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )


def _row_to_login_session(row: dict[str, Any]) -> LoginSessionRecord:
    return LoginSessionRecord(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        token_hash=str(row["token_hash"]).strip(),
        expires_at=row["expires_at"],
        user_agent=_text(row.get("user_agent")),
        ip=_text(row.get("ip")),
        revoked_at=row.get("revoked_at"),
        last_used_at=row.get("last_used_at"),
        created_at=row.get("created_at"),
    )


def _row_to_conversation(row: dict[str, Any]) -> ConversationRecord:
    return ConversationRecord(
        id=str(row["id"]),
        user_id=str(row["user_id"]),
        title=str(row.get("title") or "新对话"),
        model=_text(row.get("model")),
        message_count=int(row.get("message_count") or 0),
        turn_count=int(row.get("turn_count") or 0),
        total_tokens=int(row.get("total_tokens") or 0),
        pinned=bool(row.get("pinned")),
        last_active_at=row.get("last_active_at"),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
        deleted_at=row.get("deleted_at"),
    )


def _row_to_message(row: dict[str, Any]) -> MessageRecord:
    tool_calls = row.get("tool_calls")
    metadata = row.get("metadata")
    return MessageRecord(
        conversation_id=str(row["conversation_id"]),
        seq=int(row["seq"]),
        role=str(row["role"]),
        content=str(row.get("content") or ""),
        name=_text(row.get("name")),
        tool_call_id=_text(row.get("tool_call_id")),
        tool_calls=tool_calls if isinstance(tool_calls, list) else [],
        status=str(row.get("status") or "ok"),
        error_code=_text(row.get("error_code")),
        model=_text(row.get("model")),
        input_tokens=int(row.get("input_tokens") or 0),
        output_tokens=int(row.get("output_tokens") or 0),
        total_tokens=int(row.get("total_tokens") or 0),
        latency_ms=row.get("latency_ms"),
        langchain_id=_text(row.get("langchain_id")),
        metadata=metadata if isinstance(metadata, dict) else {},
        created_at=row.get("created_at"),
        id=row.get("id"),
    )


def _row_to_settings(row: dict[str, Any]) -> UserSettingsRecord:
    payload = row.get("settings")
    return UserSettingsRecord(
        user_id=str(row["user_id"]),
        workspace_root=_text(row.get("workspace_root")),
        workspace_writable=bool(row.get("workspace_writable", True)),
        theme=_text(row.get("theme")),
        settings=payload if isinstance(payload, dict) else {},
        updated_at=row.get("updated_at"),
    )


def _jsonb(value: Any) -> Json:
    return Json(value if value is not None else _JSON_FIELDS_EMPTY)


def _as_uuid(value: str) -> str:
    """校验 UUID 字符串；非法值按"不存在"处理（返回一个不可能匹配的哨兵）。"""
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        # 让 SQL 参数合法但不匹配任何行，避免 500
        return "00000000-0000-0000-0000-000000000000"


def _ip_or_none(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _like_pattern(keyword: str | None) -> str | None:
    """把用户输入转成 ILIKE 模式，并转义通配符。

    不转义的话，用户搜 `%` 会匹配到全部会话，`a_b` 会意外匹配 `axb`。
    这里用 ``!`` 作为转义符（而不是反斜杠），避免与 PostgreSQL 的
    ``standard_conforming_strings`` 以及 Python 字符串转义互相干扰。
    """
    if not keyword or not keyword.strip():
        return None
    escaped = keyword.strip().replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


# ------------------------------------------------------------------------ 用户
class PostgresUserRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        username: str,
        password_hash: str,
        email: str | None = None,
        display_name: str | None = None,
        role: str = "user",
    ) -> UserRecord:
        import psycopg2

        def _work(cursor: Any) -> UserRecord:
            cursor.execute(
                """
                INSERT INTO users (username, email, password_hash, display_name, role)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (username, email, password_hash, display_name, role),
            )
            return _row_to_user(cursor.fetchone())

        try:
            return await self._db.run(_work)
        except psycopg2.errors.UniqueViolation as exc:
            constraint = getattr(getattr(exc, "diag", None), "constraint_name", "") or ""
            if "email" in constraint:
                raise ConflictError("该邮箱已被注册") from exc
            raise ConflictError("该用户名已被占用") from exc

    async def get_by_id(self, user_id: str) -> UserRecord | None:
        def _work(cursor: Any) -> UserRecord | None:
            cursor.execute("SELECT * FROM users WHERE id = %s", (_as_uuid(user_id),))
            row = cursor.fetchone()
            return _row_to_user(row) if row else None

        return await self._db.run(_work)

    async def get_by_login(self, identifier: str) -> UserRecord | None:
        def _work(cursor: Any) -> UserRecord | None:
            # username / email 都是 citext，等值比较天然大小写不敏感，且能走唯一索引
            cursor.execute(
                "SELECT * FROM users WHERE username = %s OR email = %s LIMIT 1",
                (identifier, identifier),
            )
            row = cursor.fetchone()
            return _row_to_user(row) if row else None

        return await self._db.run(_work)

    async def username_exists(self, username: str) -> bool:
        return await self._exists("SELECT 1 FROM users WHERE username = %s", username)

    async def email_exists(self, email: str) -> bool:
        return await self._exists("SELECT 1 FROM users WHERE email = %s", email)

    async def _exists(self, sql: str, value: str) -> bool:
        def _work(cursor: Any) -> bool:
            cursor.execute(sql, (value,))
            return cursor.fetchone() is not None

        return await self._db.run(_work)

    async def touch_login(self, user_id: str) -> None:
        def _work(cursor: Any) -> None:
            cursor.execute(
                "UPDATE users SET last_login_at = now(), updated_at = now() WHERE id = %s",
                (_as_uuid(user_id),),
            )

        await self._db.run(_work)

    async def update_password_hash(
        self,
        user_id: str,
        password_hash: str,
        *,
        bump_password_changed_at: bool = True,
    ) -> None:
        sql = (
            """
            UPDATE users
               SET password_hash = %s, password_changed_at = now(), updated_at = now()
             WHERE id = %s
            """
            if bump_password_changed_at
            else """
            UPDATE users SET password_hash = %s, updated_at = now() WHERE id = %s
            """
        )

        def _work(cursor: Any) -> None:
            cursor.execute(sql, (password_hash, _as_uuid(user_id)))

        await self._db.run(_work)

    async def set_status(self, user_id: str, status: int) -> None:
        def _work(cursor: Any) -> None:
            cursor.execute(
                "UPDATE users SET status = %s, updated_at = now() WHERE id = %s",
                (int(status), _as_uuid(user_id)),
            )

        await self._db.run(_work)


# -------------------------------------------------------------------- 登录态会话
class PostgresLoginSessionRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        user_id: str,
        token_hash: str,
        expires_at: datetime,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> LoginSessionRecord:
        def _work(cursor: Any) -> LoginSessionRecord:
            cursor.execute(
                """
                INSERT INTO user_sessions (user_id, token_hash, expires_at, user_agent, ip)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (_as_uuid(user_id), token_hash, expires_at, user_agent, _ip_or_none(ip)),
            )
            return _row_to_login_session(cursor.fetchone())

        return await self._db.run(_work)

    async def get_by_id(self, session_id: str) -> LoginSessionRecord | None:
        def _work(cursor: Any) -> LoginSessionRecord | None:
            cursor.execute("SELECT * FROM user_sessions WHERE id = %s", (_as_uuid(session_id),))
            row = cursor.fetchone()
            return _row_to_login_session(row) if row else None

        return await self._db.run(_work)

    async def get_by_token_hash(self, token_hash: str) -> LoginSessionRecord | None:
        def _work(cursor: Any) -> LoginSessionRecord | None:
            cursor.execute(
                "SELECT * FROM user_sessions WHERE token_hash = %s",
                (token_hash,),
            )
            row = cursor.fetchone()
            return _row_to_login_session(row) if row else None

        return await self._db.run(_work)

    async def rotate_token(self, session_id: str, token_hash: str, expires_at: datetime) -> None:
        def _work(cursor: Any) -> None:
            cursor.execute(
                """
                UPDATE user_sessions
                   SET token_hash = %s, expires_at = %s, last_used_at = now()
                 WHERE id = %s AND revoked_at IS NULL
                """,
                (token_hash, expires_at, _as_uuid(session_id)),
            )

        await self._db.run(_work)

    async def touch(self, session_id: str) -> None:
        def _work(cursor: Any) -> None:
            cursor.execute(
                "UPDATE user_sessions SET last_used_at = now() WHERE id = %s",
                (_as_uuid(session_id),),
            )

        await self._db.run(_work)

    async def revoke(self, session_id: str) -> bool:
        def _work(cursor: Any) -> bool:
            cursor.execute(
                """
                UPDATE user_sessions SET revoked_at = now()
                 WHERE id = %s AND revoked_at IS NULL
                """,
                (_as_uuid(session_id),),
            )
            return cursor.rowcount > 0

        return await self._db.run(_work)

    async def revoke_all(self, user_id: str) -> int:
        def _work(cursor: Any) -> int:
            cursor.execute(
                """
                UPDATE user_sessions SET revoked_at = now()
                 WHERE user_id = %s AND revoked_at IS NULL
                """,
                (_as_uuid(user_id),),
            )
            return int(cursor.rowcount)

        return await self._db.run(_work)


# ------------------------------------------------------------------------ 会话
class PostgresConversationRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def create(
        self,
        *,
        conversation_id: str,
        user_id: str,
        title: str,
        model: str | None = None,
        metadata: dict | None = None,
    ) -> ConversationRecord:
        import psycopg2

        def _work(cursor: Any) -> ConversationRecord:
            cursor.execute(
                """
                INSERT INTO conversations (id, user_id, title, model, metadata)
                VALUES (%s, %s, %s, %s, %s)
                RETURNING *
                """,
                (conversation_id, _as_uuid(user_id), title, model, _jsonb(metadata or {})),
            )
            return _row_to_conversation(cursor.fetchone())

        try:
            return await self._db.run(_work)
        except psycopg2.errors.UniqueViolation as exc:
            raise ConflictError("该会话 ID 已被占用，请换一个") from exc

    async def get(self, user_id: str, conversation_id: str) -> ConversationRecord | None:
        def _work(cursor: Any) -> ConversationRecord | None:
            cursor.execute(
                """
                SELECT * FROM conversations
                 WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """,
                (conversation_id, _as_uuid(user_id)),
            )
            row = cursor.fetchone()
            return _row_to_conversation(row) if row else None

        return await self._db.run(_work)

    async def list(
        self,
        user_id: str,
        *,
        limit: int = 50,
        offset: int = 0,
        keyword: str | None = None,
    ) -> tuple[list[ConversationRecord], int]:
        pattern = _like_pattern(keyword)

        def _work(cursor: Any) -> tuple[list[ConversationRecord], int]:
            # ORDER BY 与部分索引 conversations_user_recent_idx 的定义保持一致，
            # 否则规划器会退化成 Seq Scan + Sort（见 docs/db/schema.postgres.sql 注释）。
            cursor.execute(
                """
                SELECT * FROM conversations
                 WHERE user_id = %s
                   AND deleted_at IS NULL
                   AND (%s::text IS NULL OR title ILIKE %s ESCAPE '!')
                 ORDER BY last_active_at DESC
                 LIMIT %s OFFSET %s
                """,
                (_as_uuid(user_id), pattern, pattern, limit, offset),
            )
            rows = [_row_to_conversation(row) for row in cursor.fetchall()]

            cursor.execute(
                """
                SELECT COUNT(*) AS total FROM conversations
                 WHERE user_id = %s
                   AND deleted_at IS NULL
                   AND (%s::text IS NULL OR title ILIKE %s ESCAPE '!')
                """,
                (_as_uuid(user_id), pattern, pattern),
            )
            total = int(cursor.fetchone()["total"])
            return rows, total

        return await self._db.run(_work)

    async def list_messages(self, user_id: str, conversation_id: str) -> list[MessageRecord]:
        def _work(cursor: Any) -> list[MessageRecord]:
            cursor.execute(
                """
                SELECT m.*
                  FROM messages m
                  JOIN conversations c ON c.id = m.conversation_id
                 WHERE m.conversation_id = %s
                   AND c.user_id = %s
                   AND c.deleted_at IS NULL
                 ORDER BY m.seq
                """,
                (conversation_id, _as_uuid(user_id)),
            )
            return [_row_to_message(row) for row in cursor.fetchall()]

        return await self._db.run(_work)

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

        def _work(cursor: Any) -> list[MessageRecord]:
            # 先锁住会话行：既校验归属，又让同一会话的并发写入串行化（seq 不会撞车）
            cursor.execute(
                """
                SELECT id FROM conversations
                 WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                 FOR UPDATE
                """,
                (conversation_id, _as_uuid(user_id)),
            )
            if cursor.fetchone() is None:
                raise SessionNotFoundError(f"会话不存在或无权访问：{conversation_id}")

            cursor.execute(
                "SELECT COALESCE(MAX(seq), 0) AS max_seq FROM messages WHERE conversation_id = %s",
                (conversation_id,),
            )
            next_seq = int(cursor.fetchone()["max_seq"]) + 1

            inserted: list[MessageRecord] = []
            for item in messages:
                cursor.execute(
                    """
                    INSERT INTO messages (
                        conversation_id, seq, role, content, name, tool_call_id, tool_calls,
                        status, error_code, model, input_tokens, output_tokens, total_tokens,
                        latency_ms, langchain_id, metadata
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        conversation_id,
                        next_seq,
                        item.role,
                        item.content,
                        item.name,
                        item.tool_call_id,
                        _jsonb(item.tool_calls),
                        item.status,
                        item.error_code,
                        item.model,
                        item.input_tokens,
                        item.output_tokens,
                        item.total_tokens,
                        item.latency_ms,
                        item.langchain_id,
                        _jsonb(item.metadata),
                    ),
                )
                inserted.append(_row_to_message(cursor.fetchone()))
                next_seq += 1

            tokens_delta = sum(item.total_tokens for item in messages)
            cursor.execute(
                """
                UPDATE conversations
                   SET message_count = message_count + %s,
                       turn_count = turn_count + %s,
                       total_tokens = total_tokens + %s,
                       last_active_at = now(),
                       updated_at = now(),
                       title = COALESCE(%s, title)
                 WHERE id = %s
                """,
                (len(messages), max(0, turn_increment), tokens_delta, title, conversation_id),
            )
            return inserted

        return await self._db.run(_work)

    async def rename(self, user_id: str, conversation_id: str, title: str) -> bool:
        def _work(cursor: Any) -> bool:
            cursor.execute(
                """
                UPDATE conversations SET title = %s, updated_at = now()
                 WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """,
                (title, conversation_id, _as_uuid(user_id)),
            )
            return cursor.rowcount > 0

        return await self._db.run(_work)

    async def soft_delete(self, user_id: str, conversation_id: str) -> bool:
        def _work(cursor: Any) -> bool:
            cursor.execute(
                """
                UPDATE conversations SET deleted_at = now(), updated_at = now()
                 WHERE id = %s AND user_id = %s AND deleted_at IS NULL
                """,
                (conversation_id, _as_uuid(user_id)),
            )
            return cursor.rowcount > 0

        return await self._db.run(_work)


# -------------------------------------------------------------------- 用户偏好
class PostgresUserSettingsRepository:
    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, user_id: str) -> UserSettingsRecord | None:
        def _work(cursor: Any) -> UserSettingsRecord | None:
            cursor.execute("SELECT * FROM user_settings WHERE user_id = %s", (_as_uuid(user_id),))
            row = cursor.fetchone()
            return _row_to_settings(row) if row else None

        return await self._db.run(_work)

    async def upsert(
        self,
        user_id: str,
        *,
        workspace_root: str | None = None,
        workspace_writable: bool | None = None,
        theme: str | None = None,
        settings: dict | None = None,
    ) -> UserSettingsRecord:
        def _work(cursor: Any) -> UserSettingsRecord:
            cursor.execute("SELECT * FROM user_settings WHERE user_id = %s FOR UPDATE", (_as_uuid(user_id),))
            existing = cursor.fetchone()

            if existing is None:
                cursor.execute(
                    """
                    INSERT INTO user_settings (user_id, workspace_root, workspace_writable, theme, settings)
                    VALUES (%s, %s, COALESCE(%s, true), %s, COALESCE(%s, '{}'::jsonb))
                    RETURNING *
                    """,
                    (
                        _as_uuid(user_id),
                        workspace_root,
                        workspace_writable,
                        theme,
                        _jsonb(settings) if settings is not None else None,
                    ),
                )
                return _row_to_settings(cursor.fetchone())

            assignments: list[str] = []
            params: list[Any] = []
            if workspace_root is not None:
                assignments.append("workspace_root = %s")
                params.append(workspace_root)
            if workspace_writable is not None:
                assignments.append("workspace_writable = %s")
                params.append(workspace_writable)
            if theme is not None:
                assignments.append("theme = %s")
                params.append(theme)
            if settings is not None:
                assignments.append("settings = settings || %s")
                params.append(_jsonb(settings))
            assignments.append("updated_at = now()")
            params.append(_as_uuid(user_id))

            cursor.execute(
                f"UPDATE user_settings SET {', '.join(assignments)} WHERE user_id = %s RETURNING *",
                tuple(params),
            )
            return _row_to_settings(cursor.fetchone())

        return await self._db.run(_work)


def build_postgres_repositories(db: Database) -> tuple[Any, Any, Any, Any]:
    """返回与上面对应的四个仓储实例（供 ``Repositories`` 组装）。"""
    if db is None:  # pragma: no cover - 防御性分支
        raise DatabaseUnavailableError("数据库未初始化")
    return (
        PostgresUserRepository(db),
        PostgresLoginSessionRepository(db),
        PostgresConversationRepository(db),
        PostgresUserSettingsRepository(db),
    )


__all__ = [
    "PostgresConversationRepository",
    "PostgresLoginSessionRepository",
    "PostgresUserRepository",
    "PostgresUserSettingsRepository",
    "build_postgres_repositories",
]
