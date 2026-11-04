"""内置工具集合。

新增工具的方式：
1. 在本目录新建模块，用 ``@tool`` 装饰函数并写清楚 docstring（模型靠它决定何时调用）；
2. 在 ``app/agent/tools/__init__.py`` 的 ``ALL_TOOLS`` 中登记。

当前分组：
- 通用：``calculator`` / ``get_current_time`` / ``search_knowledge_base``
- 工作区（读/写/查/改/删）：``files.py``，根目录由前端页面设置（见 ``app/agent/workspace.py``）
- 联网：``web.py``（``web_search`` / ``fetch_web_page``）
- 终端：``shell.py``（``run_command`` / ``run_python``，高风险，见该模块的护栏说明）
"""

from __future__ import annotations

from langchain_core.tools import BaseTool

from app.agent.tools.calculator import calculator
from app.agent.tools.datetime_tool import get_current_time
from app.agent.tools.files import (
    delete_workspace_file,
    edit_workspace_file,
    list_workspace_files,
    read_workspace_file,
    search_workspace,
    write_workspace_file,
)
from app.agent.tools.knowledge import search_knowledge_base
from app.agent.tools.shell import run_command, run_python
from app.agent.tools.web import fetch_web_page, web_search
from app.agent.tools.deepseek_web_search import ds_web_search
from app.agent.tools.load_skill import load_skill





ALL_TOOLS: list[BaseTool] = [
    # 通用能力
    calculator,
    get_current_time,
    search_knowledge_base,
    # 工作区文件操作
    list_workspace_files,
    read_workspace_file,
    search_workspace,
    write_workspace_file,
    edit_workspace_file,
    delete_workspace_file,
    # 联网
    web_search,
    fetch_web_page,
    ds_web_search,
    # 终端（高风险，受写权限开关 + 命令黑名单/白名单约束）
    run_command,
    run_python,
    #读取技能
    load_skill,
]
TOOL_REGISTRY: dict[str, BaseTool] = {item.name: item for item in ALL_TOOLS}


def get_tools(names: list[str] | None = None) -> list[BaseTool]:
    """按名称筛选工具；``names`` 为 None/空 表示注册全部内置工具。

    未注册的名称会被忽略（并在调用方日志中提示），避免因配置写错导致启动失败。
    """
    if not names:
        return list(ALL_TOOLS)
    return [TOOL_REGISTRY[name] for name in names if name in TOOL_REGISTRY]


def resolve_unknown_tools(names: list[str] | None = None) -> list[str]:
    """返回配置中不存在于注册表的工具名，便于启动时告警。"""
    if not names:
        return []
    return [name for name in names if name not in TOOL_REGISTRY]


def describe_tools(tools: list[BaseTool]) -> list[dict[str, object]]:
    """输出工具元信息（供 /agent/tools 与 /agent/info 接口使用）。"""
    described: list[dict[str, object]] = []
    for item in tools:
        schema: dict[str, object] = {}
        args_schema = getattr(item, "args_schema", None)
        if args_schema is not None and hasattr(args_schema, "model_json_schema"):
            schema = args_schema.model_json_schema()
        described.append(
            {
                "name": item.name,
                "description": (item.description or "").strip(),
                "parameters": schema,
            }
        )
    return described


__all__ = [
    "ALL_TOOLS",
    "TOOL_REGISTRY",
    "calculator",
    "delete_workspace_file",
    "describe_tools",
    "edit_workspace_file",
    "fetch_web_page",
    "get_current_time",
    "get_tools",
    "list_workspace_files",
    "read_workspace_file",
    "resolve_unknown_tools",
    "run_command",
    "run_python",
    "search_knowledge_base",
    "search_workspace",
    "web_search",
    "write_workspace_file",
    "ds_web_search",
    "load_skill",
]
