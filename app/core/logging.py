"""统一日志配置。"""

from __future__ import annotations

import logging
import sys

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-28s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"
_NOISY_LOGGERS = ("httpx", "httpcore", "openai", "urllib3", "asyncio")


def setup_logging(level: str = "INFO") -> None:
    """初始化根 logger（幂等，可重复调用）。"""
    # Windows 控制台默认不是 UTF-8，这里统一成 UTF-8 以避免中文日志乱码
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except (ValueError, OSError):  # pragma: no cover - 流不可重配置时忽略
                pass

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(_coerce_level(level))

    for name in _NOISY_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)


def _coerce_level(level: str) -> int:
    resolved = logging.getLevelName(str(level).upper())
    return resolved if isinstance(resolved, int) else logging.INFO


def get_logger(name: str) -> logging.Logger:
    """获取带统一前缀的 logger。"""
    return logging.getLogger(name if name.startswith("app") else f"app.{name}")
