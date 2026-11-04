"""PostgreSQL 连接池与异步执行封装。

为什么是 psycopg2（同步驱动）+ 线程池
------------------------------------
- 本项目的部署环境装不了新包，psycopg2 是唯一现成可用的 PostgreSQL 驱动；
- psycopg2 是同步阻塞的，直接在事件循环里调用会卡住整个服务，
  所以统一通过 ``anyio.to_thread.run_sync`` 丢到工作线程执行（anyio 由 starlette 自带）；
- 一个"工作单元"（``run`` 的回调）始终在**同一个线程、同一个连接、同一个事务**里跑完，
  cursor 不会跨线程/跨 await 边界传递，避免 psycopg2 的线程安全陷阱。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypeVar

import anyio

from app.core.exceptions import DatabaseUnavailableError
from app.core.logging import get_logger

logger = get_logger("db.database")

T = TypeVar("T")

_INSTALL_HINT = (
    "缺少 PostgreSQL 驱动 psycopg2。请在服务所用解释器里执行：\n"
    "    pip install psycopg2-binary\n"
    "（或 pip install -r requirements.txt），然后重启服务。"
    "若暂时不想用数据库，把 AGENT_DATABASE_URL 留空即可回退到内存存储。"
)


class Database:
    """极简连接池封装：暴露 ``run`` / ``ping`` / ``close``。

    ``run`` 的语义是"在一个事务里执行一个工作单元"：
    回调正常返回则提交，抛异常则回滚，连接无论如何都会归还到池里。
    """

    def __init__(
        self,
        dsn: str,
        *,
        minconn: int = 1,
        maxconn: int = 10,
        connect_timeout: int = 5,
    ) -> None:
        self._dsn = dsn
        self._minconn = minconn
        self._maxconn = maxconn
        self._connect_timeout = connect_timeout
        self._pool: Any = None

    # ------------------------------------------------------------------ 生命周期
    @property
    def is_open(self) -> bool:
        return self._pool is not None

    def open(self) -> None:
        """建立连接池（幂等）。驱动缺失或连不上都抛 ``DatabaseUnavailableError``。"""
        if self._pool is not None:
            return
        try:
            import psycopg2
            from psycopg2 import pool as pg_pool
        except ImportError as exc:  # pragma: no cover - 取决于运行环境
            raise DatabaseUnavailableError(_INSTALL_HINT) from exc

        try:
            # 最小连接数先建 1 条，避免启动时把数据库打满；不够时池会按需扩到 maxconn
            self._pool = pg_pool.ThreadedConnectionPool(
                max(1, min(self._minconn, self._maxconn)),
                max(1, self._maxconn),
                dsn=self._dsn,
                connect_timeout=self._connect_timeout,
                # 会话级超时，避免一条慢查询把连接永久占住
                options="-c statement_timeout=30000",
                application_name="langchain-agent-api",
            )
            # 统一用 UTF-8 与真实类型（jsonb -> dict / timestamptz -> datetime）
            connection = self._pool.getconn()
            try:
                connection.set_client_encoding("UTF8")
                connection.autocommit = False
            finally:
                self._pool.putconn(connection)
        except DatabaseUnavailableError:
            raise
        except psycopg2.Error as exc:
            self._pool = None
            raise DatabaseUnavailableError(
                f"数据库连接失败：{type(exc).__name__}: {str(exc).strip()}",
            ) from exc

        logger.info("数据库连接池已建立（min=%s, max=%s）", self._minconn, self._maxconn)

    def close(self) -> None:
        if self._pool is None:
            return
        try:
            self._pool.closeall()
        except Exception:  # noqa: BLE001 - 关闭阶段的异常无需影响退出流程
            logger.warning("关闭数据库连接池时出现异常", exc_info=True)
        finally:
            self._pool = None

    # ------------------------------------------------------------------- 执行封装
    async def run(self, work: Callable[[Any], T]) -> T:
        """在专用线程里执行 ``work(cursor)``，并在同一事务中提交/回滚。"""
        return await anyio.to_thread.run_sync(self._run_sync, work)

    def _run_sync(self, work: Callable[[Any], T]) -> T:
        import psycopg2
        from psycopg2.extras import RealDictCursor

        if self._pool is None:
            raise DatabaseUnavailableError("数据库连接池尚未初始化")

        try:
            connection = self._pool.getconn()
        except Exception as exc:  # noqa: BLE001 - 池耗尽（PoolError）等一律视为不可用
            raise DatabaseUnavailableError(
                f"无法从连接池获取连接（AGENT_DB_POOL_MAX={self._maxconn}）：{exc}",
            ) from exc

        try:
            with connection:  # 正常退出提交、异常回滚
                with connection.cursor(cursor_factory=RealDictCursor) as cursor:
                    return work(cursor)
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
            # 连接已损坏（数据库重启、网络闪断）：丢掉整池，下次调用重新建连
            logger.error("数据库连接失效，将重建连接池：%s", str(exc).strip())
            self._discard_pool()
            raise DatabaseUnavailableError(
                f"数据库连接失效：{type(exc).__name__}: {str(exc).strip()}",
            ) from exc
        except psycopg2.Error as exc:
            logger.warning("数据库执行失败：%s: %s", type(exc).__name__, str(exc).strip())
            raise
        finally:
            # 连接可能已被丢弃（池为 None），此时 putconn 无意义
            if self._pool is not None:
                try:
                    self._pool.putconn(connection)
                except Exception:  # noqa: BLE001 - 归还失败不应覆盖业务异常
                    logger.warning("归还数据库连接失败", exc_info=True)

    def _discard_pool(self) -> None:
        pool, self._pool = self._pool, None
        if pool is not None:
            try:
                pool.closeall()
            except Exception:  # noqa: BLE001
                logger.warning("丢弃连接池时出现异常", exc_info=True)

    async def ping(self) -> tuple[bool, str | None]:
        """健康检查用：返回 ``(是否可用, 错误描述)``，不抛异常。"""

        def _work(cursor: Any) -> None:
            cursor.execute("SELECT 1")

        try:
            await self.run(_work)
        except DatabaseUnavailableError as exc:
            return False, exc.message
        except Exception as exc:  # noqa: BLE001 - 探针不应把服务拖垮
            return False, f"{type(exc).__name__}: {str(exc).strip()}"
        return True, None


__all__ = ["Database"]
