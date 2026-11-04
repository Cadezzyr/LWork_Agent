"""工作区（Workspace）运行时配置与路径守卫。

设计约定：
- **工作区根目录不在任何配置文件里**，而是由前端页面通过 ``PUT /api/v1/workspace``
  在运行时指定；进程重启后回到默认值（项目根目录）。
- **按用户隔离**：每个登录用户各有一份运行时配置，互不影响。请求入口通过
  :func:`bind_workspace_user` 把当前用户绑到协程上下文，文件工具 / 终端工具 /
  提示词中间件都通过 :func:`get_workspace_manager` 取到"当前用户的那一份"。
- 没绑定用户时（脚本、单测、后台任务）回退到全局默认实例，行为与旧版一致。
- 所有文件操作都要先经过 :meth:`WorkspaceManager.resolve`：把用户/模型给的路径
  解析成绝对路径，并校验它仍落在工作区内、且不是敏感文件。
"""

from __future__ import annotations

import os
import threading
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.core.logging import get_logger

logger = get_logger("agent.workspace")

# 这些目录不参与列举/检索（体积大且与业务无关）
SKIP_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".idea",
        ".pytest_cache",
        ".ruff_cache",
        ".deps",
        ".tmp",
        ".pipcache",
        ".piptmp",
        "dist",
        "build",
    }
)

# 绝对不允许读写的文件（防止把密钥泄露给模型/外部）
DENY_NAMES: frozenset[str] = frozenset({".env", ".env.local", ".env.production", ".env.development"})
DENY_SUFFIXES: frozenset[str] = frozenset({".pem", ".key", ".pfx", ".p12", ".keystore", ".jks"})

# 单文件大小上限（读取/写入/检索共用）
MAX_FILE_BYTES = 400_000


class WorkspaceError(ValueError):
    """工作区配置或路径校验失败。"""


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """工作区当前状态（对外展示用）。"""

    root: str
    exists: bool
    writable: bool
    allow_writes: bool
    is_default: bool
    updated_at: str
    file_count: int | None = None


class WorkspaceManager:
    """运行时工作区：内存态、可被前端修改、线程安全。"""

    def __init__(self, default_root: Path | str, *, allow_writes: bool = True) -> None:
        self._default_root = Path(default_root).expanduser().resolve()
        self._root = self._default_root
        self._allow_writes = allow_writes
        self._updated_at = datetime.now(timezone.utc)
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 状态
    @property
    def root(self) -> Path:
        with self._lock:
            return self._root

    @property
    def allow_writes(self) -> bool:
        with self._lock:
            return self._allow_writes

    @property
    def default_root(self) -> Path:
        return self._default_root

    def info(self, *, with_count: bool = True) -> WorkspaceSnapshot:
        with self._lock:
            root = self._root
            allow = self._allow_writes
            updated = self._updated_at
            is_default = root == self._default_root
        count = None
        if with_count and root.is_dir():
            try:
                count = sum(1 for item in root.iterdir() if not _skipped_name(item.name))
            except OSError:
                count = None
        return WorkspaceSnapshot(
            root=str(root),
            exists=root.is_dir(),
            writable=root.is_dir() and os.access(root, os.W_OK),
            allow_writes=allow,
            is_default=is_default,
            updated_at=updated.isoformat(),
            file_count=count,
        )

    # ------------------------------------------------------------- 运行时设置
    def set_root(self, root: str | None) -> WorkspaceSnapshot:
        """设置工作区根目录；``None``/空串表示恢复默认。"""
        with self._lock:
            if root is None or not str(root).strip():
                self._root = self._default_root
                self._updated_at = datetime.now(timezone.utc)
                logger.info("工作区恢复默认：%s", self._root)
                return self.info()

            raw = str(root).strip().strip("\"'")
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                raise WorkspaceError("工作区必须是绝对路径，例如 D:\\projects\\demo 或 /home/user/demo")
            resolved = candidate.resolve()
            if not resolved.exists():
                raise WorkspaceError(f"目录不存在：{resolved}")
            if not resolved.is_dir():
                raise WorkspaceError(f"不是目录：{resolved}")
            # 拒绝盘符根目录 / 文件系统根目录，避免一个提示注入就把整机暴露出去
            if resolved.parent == resolved or not resolved.parts:
                raise WorkspaceError("不能把磁盘根目录设为工作区，请指定具体目录")

            self._root = resolved
            self._updated_at = datetime.now(timezone.utc)
            logger.warning("工作区已切换为：%s（可写=%s）", resolved, self._allow_writes)
            return self.info()

    def set_allow_writes(self, value: bool) -> WorkspaceSnapshot:
        with self._lock:
            self._allow_writes = bool(value)
            self._updated_at = datetime.now(timezone.utc)
            logger.warning("工作区写权限已设置为：%s", self._allow_writes)
            return self.info()

    # ---------------------------------------------------------------- 路径守卫
    def resolve(self, raw: str | None, *, require_writes: bool = False) -> Path:
        """把相对/绝对路径解析成工作区内的绝对路径。

        Raises:
            WorkspaceError: 路径越界、命中敏感文件，或只读模式下要求写入。
        """
        with self._lock:
            root = self._root
            allow_writes = self._allow_writes

        if require_writes and not allow_writes:
            raise WorkspaceError(
                "当前工作区为只读模式（可在页面「工作区设置」里打开写权限）"
            )

        text = (raw or ".").strip().strip("\"'")
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()

        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise WorkspaceError(f"路径超出工作区范围（{root}）：{raw}") from exc

        if resolved.name.lower() in DENY_NAMES or resolved.suffix.lower() in DENY_SUFFIXES:
            raise WorkspaceError(f"该文件属于敏感配置，禁止访问：{raw}")
        return resolved

    # ------------------------------------------------------------------ 工具
    def relative(self, path: Path) -> str:
        """返回相对工作区的路径（POSIX 风格），用于展示。"""
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def is_skipped(self, path: Path) -> bool:
        try:
            parts = path.relative_to(self.root).parts
        except ValueError:
            parts = path.parts
        return any(part in SKIP_DIRS for part in parts)

    @staticmethod
    def is_sensitive(path: Path) -> bool:
        """是否是敏感文件（.env / 私钥等），列举与检索时也要跳过。"""
        return path.name.lower() in DENY_NAMES or path.suffix.lower() in DENY_SUFFIXES

    def iter_files(self, pattern: str = "**/*", *, limit: int = 2000) -> Iterator[Path]:
        """遍历工作区内匹配的文件（跳过 SKIP_DIRS 与敏感文件）。"""
        root = self.root
        count = 0
        for item in root.glob(pattern):
            if item.is_dir() or self.is_skipped(item) or self.is_sensitive(item):
                continue
            yield item
            count += 1
            if count >= limit:
                return

    def tree(self, subdir: str = ".", *, depth: int = 2, max_entries: int = 300) -> list[dict[str, str]]:
        """返回目录树（供前端确认工作区选得对不对）。"""
        base = self.resolve(subdir)
        if not base.is_dir():
            raise WorkspaceError(f"不是目录：{subdir}")
        depth = max(0, min(int(depth), 6))
        entries: list[dict[str, str]] = []
        base_depth = len(base.parts)

        for current_root, dirs, files in os.walk(base):
            current = Path(current_root)
            level = len(current.parts) - base_depth
            if level >= depth:
                dirs[:] = []
            dirs[:] = sorted(item for item in dirs if item not in SKIP_DIRS)
            for name in sorted(files):
                if name.lower() in DENY_NAMES:
                    continue
                if Path(name).suffix.lower() in DENY_SUFFIXES:
                    continue
                target = current / name
                entries.append({"path": self.relative(target), "type": "file"})
                if len(entries) >= max_entries:
                    return entries
            for name in dirs:
                entries.append({"path": self.relative(current / name) + "/", "type": "dir"})
                if len(entries) >= max_entries:
                    return entries
        return entries


def _skipped_name(name: str) -> bool:
    return name in SKIP_DIRS or name.lower() in DENY_NAMES


# ------------------------------------------------------- 按用户隔离的工作区实例
# 上限：只保留最近活跃的 N 个用户实例，避免长期运行后无限增长
_MAX_MANAGERS = 2000

# 未绑定用户时的兜底实例（脚本、单测、后台任务用）
_default_manager: WorkspaceManager | None = None
# user_id -> 该用户自己的工作区管理器
_managers: OrderedDict[str, WorkspaceManager] = OrderedDict()
_manager_lock = threading.Lock()

# 当前请求所属用户；由请求入口（AgentService）set/reset
_current_user: ContextVar[str | None] = ContextVar("workspace_user", default=None)


def init_workspace_manager(default_root: Path | str, *, allow_writes: bool = True) -> WorkspaceManager:
    """初始化默认工作区，并清空所有用户的运行时设置（服务启动时调用一次）。"""
    global _default_manager
    with _manager_lock:
        _managers.clear()
        _default_manager = WorkspaceManager(default_root, allow_writes=allow_writes)
    return _default_manager


@contextmanager
def bind_workspace_user(user_id: str | None) -> Iterator[None]:
    """在当前协程上下文里绑定用户，使工具与中间件拿到该用户的工作区。

    ``user_id`` 为 ``None`` 时表示不绑定（回退到默认实例）。
    """
    token = _current_user.set(user_id)
    try:
        yield
    finally:
        _current_user.reset(token)


def _get_default_manager() -> WorkspaceManager:
    """返回全局默认实例；未初始化时按项目根目录惰性创建。"""
    global _default_manager
    if _default_manager is not None:
        return _default_manager
    from app.core.config import PROJECT_ROOT

    with _manager_lock:
        if _default_manager is None:
            _default_manager = WorkspaceManager(PROJECT_ROOT)
            logger.info("工作区管理器未显式初始化，已回退到项目根目录：%s", PROJECT_ROOT)
        return _default_manager


def get_workspace_manager(user_id: str | None = None) -> WorkspaceManager:
    """获取工作区管理器。

    Args:
        user_id: 显式指定用户；省略时使用 ``bind_workspace_user`` 绑定的用户，
            都没有则回退到全局默认实例。

    Returns:
        该用户自己的工作区管理器（首次访问时按默认根目录创建）。
    """
    uid = user_id if user_id is not None else _current_user.get()
    if not uid:
        return _get_default_manager()

    base = _get_default_manager()
    with _manager_lock:
        manager = _managers.get(uid)
        if manager is None:
            manager = WorkspaceManager(base.default_root)
            _managers[uid] = manager
            while len(_managers) > _MAX_MANAGERS:
                _managers.popitem(last=False)
        else:
            _managers.move_to_end(uid)
        return manager


def reset_workspace_managers() -> None:
    """清空所有用户的运行时设置（测试用）。"""
    with _manager_lock:
        _managers.clear()


__all__ = [
    "DENY_SUFFIXES",
    "MAX_FILE_BYTES",
    "SKIP_DIRS",
    "WorkspaceError",
    "WorkspaceManager",
    "WorkspaceSnapshot",
    "bind_workspace_user",
    "get_workspace_manager",
    "init_workspace_manager",
    "reset_workspace_managers",
]
