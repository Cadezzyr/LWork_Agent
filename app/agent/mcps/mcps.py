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