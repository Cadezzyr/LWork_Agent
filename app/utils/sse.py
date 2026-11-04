"""Server-Sent Events（SSE）序列化工具。"""

from __future__ import annotations

import json
from typing import Any


def format_sse(event: str, data: dict[str, Any]) -> str:
    """把事件序列化成 SSE 帧：``event: xxx\\ndata: {...}\\n\\n``。"""
    payload = json.dumps(data, ensure_ascii=False, default=str)
    return f"event: {event}\ndata: {payload}\n\n"


def format_sse_comment(comment: str) -> str:
    """SSE 注释帧，可用作心跳（前端会忽略）。"""
    return f": {comment}\n\n"
