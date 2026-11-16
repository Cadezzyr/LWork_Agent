"""MCP 配置示例（保留作参考）。

真正的实现在同目录的 ``config.py``（配置解析）与 ``manager.py``（客户端与工具加载），
使用方式见 ``app/agent/mcps/__init__.py``；这里只留下最初的手写示例。

运行期通过环境变量启用：

    AGENT_MCP_SERVERS={"math": {"transport": "stdio", "command": "python", "args": ["/path/to/math_server.py"]}}
"""

#from langchain_mcp_adapters.client import MultiServerMCPClient


# client = MultiServerMCPClient(
#     {
#         "math": {
#             "transport": "stdio",  # 本地子进程通信
#             "command": "python",
#             # math_server.py 文件的绝对路径
#             "args": ["/path/to/math_server.py"],
#         },
#         "weather": {
#             "transport": "http",  # 基于 HTTP 的远程服务器
#             # 确保在端口 8000 上启动天气服务器
#             "url": "http://localhost:8000/mcp",
#         }
#     }
# )