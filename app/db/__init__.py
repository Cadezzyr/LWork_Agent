"""数据访问层入口：根据配置装配 PostgreSQL 或内存仓储。"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.config import Settings
from app.core.exceptions import DatabaseUnavailableError
from app.core.logging import get_logger
from app.db.base import Repositories
from app.db.database import Database
from app.db.memory import build_in_memory_repositories
from app.db.postgres import build_postgres_repositories

logger = get_logger("db")


@dataclass(slots=True)
class RepositoryBundle:
    """仓储集合 + 底层连接池 + 启动期告警（供 /health 展示）。"""

    repositories: Repositories
    database: Database | None = None
    warning: str | None = None


def build_repositories(settings: Settings) -> RepositoryBundle:
    """按 ``AGENT_DATABASE_URL`` 装配仓储。

    数据库不可用时**不阻塞启动**：退回内存仓储并把原因记到 ``warning``，
    这样 /health 能说明"历史不会持久化"，而不是整个服务起不来。
    """
    dsn = settings.database_dsn
    if dsn is None:
        logger.warning("未配置 AGENT_DATABASE_URL：使用内存仓储（重启后账号与历史丢失）")
        users, sessions, conversations, user_settings = build_in_memory_repositories()
        return RepositoryBundle(
            repositories=Repositories(
                users=users,
                login_sessions=sessions,
                conversations=conversations,
                user_settings=user_settings,
                backend="memory",
            ),
            warning="未配置 AGENT_DATABASE_URL，登录态与对话历史仅保存在进程内存中",
        )

    database = Database(
        dsn,
        minconn=settings.db_pool_min,
        maxconn=settings.db_pool_max,
        connect_timeout=settings.db_connect_timeout,
    )
    try:
        database.open()
    except DatabaseUnavailableError as exc:
        logger.error("数据库不可用，回退到内存仓储：%s", exc.message)
        users, sessions, conversations, user_settings = build_in_memory_repositories()
        return RepositoryBundle(
            repositories=Repositories(
                users=users,
                login_sessions=sessions,
                conversations=conversations,
                user_settings=user_settings,
                backend="memory",
            ),
            warning=f"数据库不可用（{exc.message}），已回退到内存仓储",
        )

    users, sessions, conversations, user_settings = build_postgres_repositories(database)
    logger.info("已连接 PostgreSQL：%s", settings.database_dsn_masked)
    return RepositoryBundle(
        repositories=Repositories(
            users=users,
            login_sessions=sessions,
            conversations=conversations,
            user_settings=user_settings,
            backend="postgres",
        ),
        database=database,
    )


__all__ = ["Database", "Repositories", "RepositoryBundle", "build_repositories"]
