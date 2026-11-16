"""MCP（Model Context Protocol）服务器配置解析。

支持两种来源，可同时使用，同名服务器以内联配置为准：

1. 环境变量 ``AGENT_MCP_SERVERS``：一段 JSON 字符串；
2. 配置文件 ``AGENT_MCP_CONFIG_FILE``：指向一个 JSON 文件，内容既可以是扁平的
   ``{"server_name": {...}}``，也可以是 Claude Desktop 风格的
   ``{"mcpServers": {...}}``（相对路径按进程工作目录解析）。

单个服务器的写法与 ``MultiServerMCPClient`` 一致：

- stdio（本地子进程）::

      {"transport": "stdio", "command": "python",
       "args": ["/abs/path/server.py"], "env": {"KEY": "VALUE"}, "cwd": "/abs/dir"}

- 远程 SSE / Streamable HTTP::

      {"transport": "http", "url": "http://localhost:8000/mcp",
       "headers": {"Authorization": "Bearer xxx"}}

  ``http`` 会被归一化成 ``streamable_http``（MCP 规范里的 Streamable HTTP），
  ``ws`` 归一化成 ``websocket``。

解析失败不会抛异常：非法条目被跳过并放进 warnings，由调用方打印日志，
避免一个写错的服务器配置导致整个服务起不来。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

TransportName = Literal["stdio", "sse", "streamable_http", "websocket"]

#: 用户配置里允许写的 transport 别名 -> 规范化名称
_TRANSPORT_ALIASES: dict[str, TransportName] = {
    "stdio": "stdio",
    "sse": "sse",
    "http": "streamable_http",
    "streamable-http": "streamable_http",
    "streamable_http": "streamable_http",
    "ws": "websocket",
    "websocket": "websocket",
}


@dataclass(frozen=True, slots=True)
class MCPServerSpec:
    """一个 MCP 服务器的连接参数（与传输方式无关的统一视图）。"""

    name: str
    transport: TransportName = "stdio"
    # stdio 专用
    command: str | None = None
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    # 远程传输专用
    url: str | None = None
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def is_stdio(self) -> bool:
        return self.transport == "stdio"

    def to_client_config(self) -> dict[str, Any]:
        """转换成 ``MultiServerMCPClient`` 期望的单服务器配置。"""
        if self.is_stdio:
            config: dict[str, Any] = {
                "transport": "stdio",
                "command": self.command,
                "args": list(self.args),
                # 显式给空 dict：部分版本的适配器要求 env 字段存在
                "env": dict(self.env),
            }
            if self.cwd:
                config["cwd"] = self.cwd
            return config
        config = {"transport": self.transport, "url": self.url}
        if self.headers:
            config["headers"] = dict(self.headers)
        return config

    def describe(self) -> dict[str, Any]:
        """日志/接口展示用（不含 headers 里的凭据原文）。"""
        if self.is_stdio:
            return {
                "name": self.name,
                "transport": self.transport,
                "command": self.command,
                "args": list(self.args),
            }
        return {
            "name": self.name,
            "transport": self.transport,
            "url": self.url,
            "headers": sorted(self.headers),
        }


def _as_str_dict(value: Any, *, label: str, warnings: list[str]) -> dict[str, str]:
    """把任意 JSON 值收窄成 ``dict[str, str]``，非法项丢弃并记 warning。"""
    if value is None:
        return {}
    if not isinstance(value, dict):
        warnings.append(f"{label} 必须是对象，已忽略")
        return {}
    result: dict[str, str] = {}
    for key, item in value.items():
        if isinstance(item, (str, int, float, bool)):
            result[str(key)] = str(item)
        else:
            warnings.append(f"{label}.{key} 不是标量，已忽略")
    return result


def _build_spec(name: str, payload: Any, warnings: list[str]) -> MCPServerSpec | None:
    """按单个服务器的 JSON 片段构建 spec；不合法时返回 ``None``。"""
    if not isinstance(payload, dict):
        warnings.append(f"服务器 {name} 的配置必须是对象，已忽略")
        return None

    raw_transport = str(payload.get("transport") or payload.get("type") or "").strip().lower()
    if not raw_transport:
        # 没写 transport 时按有没有 url 猜：有 url 走远程，否则当本地子进程
        raw_transport = "http" if payload.get("url") else "stdio"
    transport = _TRANSPORT_ALIASES.get(raw_transport)
    if transport is None:
        warnings.append(
            f"服务器 {name} 的 transport={raw_transport!r} 不支持"
            f"（可选：{', '.join(sorted(set(_TRANSPORT_ALIASES)))}），已忽略"
        )
        return None

    if transport == "stdio":
        command = payload.get("command")
        if not isinstance(command, str) or not command.strip():
            warnings.append(f"stdio 服务器 {name} 缺少 command，已忽略")
            return None
        raw_args = payload.get("args") or []
        if isinstance(raw_args, str):
            raw_args = [raw_args]
        if not isinstance(raw_args, list):
            warnings.append(f"服务器 {name} 的 args 必须是数组，已按空数组处理")
            raw_args = []
        cwd = payload.get("cwd")
        return MCPServerSpec(
            name=name,
            transport="stdio",
            command=command.strip(),
            args=tuple(str(item) for item in raw_args),
            env=_as_str_dict(payload.get("env"), label=f"{name}.env", warnings=warnings),
            cwd=str(cwd) if cwd else None,
        )

    url = payload.get("url")
    if not isinstance(url, str) or not url.strip():
        warnings.append(f"{transport} 服务器 {name} 缺少 url，已忽略")
        return None
    return MCPServerSpec(
        name=name,
        transport=transport,
        url=url.strip(),
        headers=_as_str_dict(payload.get("headers"), label=f"{name}.headers", warnings=warnings),
    )


def parse_servers(payload: Any) -> tuple[dict[str, Any], list[str]]:
    """把任意形态的 JSON 归一化成 ``{server_name: config}``。"""
    warnings: list[str] = []
    if not isinstance(payload, dict):
        return {}, ["MCP 配置必须是 JSON 对象"]

    # Claude Desktop 风格：{"mcpServers": {...}}
    nested = payload.get("mcpServers")
    if isinstance(nested, dict):
        return dict(nested), warnings
    if "mcpServers" in payload:
        return {}, ["mcpServers 必须是对象"]

    servers: dict[str, Any] = {}
    for name, value in payload.items():
        key = str(name).strip()
        if not key:
            warnings.append("存在空的服务器名称，已忽略")
            continue
        servers[key] = value
    return servers, warnings


def load_server_specs(
    raw: str | None = None,
    *,
    config_file: str | None = None,
) -> tuple[list[MCPServerSpec], list[str]]:
    """解析环境变量与配置文件，返回 ``(specs, warnings)``，永不抛异常。"""
    warnings: list[str] = []
    merged: dict[str, Any] = {}

    path_text = (config_file or "").strip()
    if path_text:
        path = Path(path_text).expanduser()
        if not path.is_absolute():
            path = Path(os.getcwd()) / path
        if not path.is_file():
            warnings.append(f"配置文件不存在：{path}")
        else:
            try:
                file_servers, file_warnings = parse_servers(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except (OSError, json.JSONDecodeError) as exc:
                warnings.append(f"读取 MCP 配置文件失败：{type(exc).__name__}: {exc}")
            else:
                warnings.extend(file_warnings)
                merged.update(file_servers)

    inline = (raw or "").strip()
    if inline:
        try:
            inline_servers, inline_warnings = parse_servers(json.loads(inline))
        except json.JSONDecodeError as exc:
            warnings.append(f"AGENT_MCP_SERVERS 不是合法 JSON：{exc}")
        else:
            warnings.extend(inline_warnings)
            # 同名以内联配置为准，便于临时覆盖文件里的服务器
            merged.update(inline_servers)

    specs: list[MCPServerSpec] = []
    for name in sorted(merged):
        spec = _build_spec(name, merged[name], warnings)
        if spec is not None:
            specs.append(spec)
    return specs, warnings


__all__ = ["MCPServerSpec", "TransportName", "load_server_specs", "parse_servers"]
