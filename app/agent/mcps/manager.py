"""MCP 客户端管理：把配置好的 MCP 服务器转换为 LangChain 工具。

设计要点
--------
- **可选依赖**：``langchain-mcp-adapters`` 未安装时只记一条告警并返回空列表，
  服务照常启动（MCP 只是"额外工具来源"，不该成为启动的硬依赖）；
- **失败隔离**：单个服务器连不上/超时只影响它自己，其它服务器的工具照常返回；
- **一次性加载**：MCP 工具在启动时加载一次并缓存在 ``AgentRuntime.tools`` 里，
  之后每次对话复用，避免每个请求都去握手。

用法（由 ``app/main.py`` 的 lifespan 调用）::

    manager = build_mcp_manager(settings)
    tools = await manager.load_tools()
    runtime = build_agent_runtime(settings, extra_tools=tools, mcp_manager=manager)
    ...
    await manager.close()   # 进程退出时
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

from langchain_core.tools import BaseTool

from app.agent.mcps.config import MCPServerSpec, load_server_specs
from app.core.config import Settings
from app.core.logging import get_logger

logger = get_logger("agent.mcps")

_INSTALL_HINT = "未安装 langchain-mcp-adapters，已跳过 MCP 工具（pip install langchain-mcp-adapters）"


class MCPManager:
    """MCP 服务器集合的生命周期管理（构建 -> 加载工具 -> 关闭）。"""

    def __init__(self, specs: list[MCPServerSpec], *, timeout: float = 30.0) -> None:
        self._specs = list(specs)
        self._timeout = timeout
        self._client: Any = None
        self._tools: list[BaseTool] = []
        #: 加载过程中的错误描述（供日志与 /health 之类的诊断查看）
        self.errors: list[str] = []
        #: 是否支持按服务器单独加载；旧版本适配器会退化为一次性加载
        self._per_server = True

    # ------------------------------------------------------------------ 构造
    @classmethod
    def from_settings(cls, settings: Settings) -> MCPManager:
        """按 ``AGENT_MCP_*`` 配置构建；关闭开关时不解析任何内容。"""
        if not settings.mcp_enabled:
            return cls([], timeout=settings.mcp_timeout)
        specs, warnings = load_server_specs(
            settings.mcp_servers,
            config_file=settings.mcp_config_file or None,
        )
        for item in warnings:
            logger.warning("MCP 配置：%s", item)
        return cls(specs, timeout=settings.mcp_timeout)

    # ------------------------------------------------------------------ 状态
    @property
    def enabled(self) -> bool:
        return bool(self._specs)

    @property
    def specs(self) -> list[MCPServerSpec]:
        return list(self._specs)

    @property
    def server_names(self) -> list[str]:
        return [spec.name for spec in self._specs]

    @property
    def tools(self) -> list[BaseTool]:
        return list(self._tools)

    def describe(self) -> list[dict[str, Any]]:
        return [spec.describe() for spec in self._specs]

    # ------------------------------------------------------------------ 加载
    async def load_tools(self) -> list[BaseTool]:
        """连接所有 MCP 服务器并收集工具；任何失败都降级为空/部分结果。"""
        if not self._specs:
            return []

        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError:
            self.errors.append(_INSTALL_HINT)
            logger.warning(_INSTALL_HINT)
            return []

        if self._client is None:
            self._client = MultiServerMCPClient(
                {spec.name: spec.to_client_config() for spec in self._specs}
            )

        collected: list[BaseTool] = []
        seen: set[str] = set()
        for spec in self._specs:
            try:
                loaded = await asyncio.wait_for(
                    self._load_server(spec), timeout=self._timeout
                )
            except asyncio.TimeoutError:
                message = f"MCP 服务器 {spec.name} 连接超时（>{self._timeout:g}s）"
                self.errors.append(message)
                logger.warning(message)
                continue
            except Exception as exc:  # noqa: BLE001 - 单个服务器失败不影响其它服务器
                message = f"MCP 服务器 {spec.name} 加载失败：{type(exc).__name__}: {exc}"
                self.errors.append(message)
                logger.warning(message)
                continue

            for tool in loaded:
                if tool.name in seen:
                    # 同名工具会让模型无法区分，保留先加载的那个
                    logger.warning("MCP 工具 %s 重复出现，已跳过（server=%s）", tool.name, spec.name)
                    continue
                seen.add(tool.name)
                collected.append(tool)
            logger.info("MCP 服务器 %s 提供 %s 个工具", spec.name, len(loaded))

        self._tools = collected
        if collected:
            logger.info(
                "MCP 已就绪：%s 个服务器，%s 个工具（%s）",
                len(self._specs),
                len(collected),
                ", ".join(tool.name for tool in collected),
            )
        return list(collected)

    async def _load_server(self, spec: MCPServerSpec) -> list[BaseTool]:
        """按服务器加载；旧版适配器不支持 ``server_name`` 时退化为一次性加载。"""
        if self._per_server:
            try:
                return await self._client.get_tools(server_name=spec.name)
            except TypeError:
                self._per_server = False
                logger.warning(
                    "当前 langchain-mcp-adapters 不支持按服务器加载，改为一次性加载全部"
                )
        return await self._client.get_tools()

    # ------------------------------------------------------------------ 关闭
    async def close(self) -> None:
        """释放客户端（适配器没有 ``close`` 时是空操作）。"""
        client, self._client = self._client, None
        if client is None:
            return
        closer = getattr(client, "close", None)
        if not callable(closer):
            return
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception:  # noqa: BLE001 - 退出阶段不应影响进程结束
            logger.warning("关闭 MCP 客户端时出现异常", exc_info=True)


def build_mcp_manager(settings: Settings) -> MCPManager:
    """``MCPManager.from_settings`` 的函数式入口，与 ``build_agent_runtime`` 对称。"""
    return MCPManager.from_settings(settings)


__all__ = ["MCPManager", "build_mcp_manager"]
