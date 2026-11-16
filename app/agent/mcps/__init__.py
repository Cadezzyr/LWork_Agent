"""MCP（Model Context Protocol）工具集成。

模块划分
--------
- ``config``  ：解析 ``AGENT_MCP_SERVERS`` / ``AGENT_MCP_CONFIG_FILE`` 里的服务器配置；
- ``manager`` ：用 ``langchain-mcp-adapters`` 连接服务器、把远程工具转成 LangChain 工具。

两种配置方式（任选其一，也可同时使用）：

1. 环境变量里直接写 JSON::

       AGENT_MCP_SERVERS={"weather": {"transport": "http", "url": "http://localhost:8000/mcp"}}

2. 指向一个 JSON 配置文件（支持 Claude Desktop 的 ``mcpServers`` 结构）：:

       AGENT_MCP_CONFIG_FILE=config/mcp.json

``langchain-mcp-adapters`` 是可选依赖，未安装时 MCP 工具会被跳过，服务照常启动。
"""

from app.agent.mcps.config import MCPServerSpec, load_server_specs, parse_servers
from app.agent.mcps.manager import MCPManager, build_mcp_manager

__all__ = [
    "MCPManager",
    "MCPServerSpec",
    "build_mcp_manager",
    "load_server_specs",
    "parse_servers",
]
