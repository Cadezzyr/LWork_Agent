"""内置工具的单元测试（不经过 LLM）。"""

from __future__ import annotations

from app.agent.tools import TOOL_REGISTRY, describe_tools, get_tools
from app.agent.tools.calculator import calculator
from app.agent.tools.datetime_tool import get_current_time
from app.agent.tools.knowledge import search_knowledge_base

EXPECTED_TOOL_NAMES = {
    # 通用
    "calculator",
    "get_current_time",
    "search_knowledge_base",
    # 工作区文件
    "list_workspace_files",
    "read_workspace_file",
    "search_workspace",
    "write_workspace_file",
    "edit_workspace_file",
    "delete_workspace_file",
    # 联网
    "web_search",
    "fetch_web_page",
    "ds_web_search",
    # 终端
    "run_command",
    "run_python",
}


def test_registry_contains_all_tools() -> None:
    assert set(TOOL_REGISTRY) == EXPECTED_TOOL_NAMES
    assert len(get_tools(None)) == len(EXPECTED_TOOL_NAMES)
    assert [item.name for item in get_tools(["calculator"])] == ["calculator"]
    assert get_tools(["not-exists"]) == []


def test_describe_tools_has_schema() -> None:
    described = {item["name"]: item for item in describe_tools(get_tools(None))}
    assert described["calculator"]["parameters"]["properties"]["expression"]["type"] == "string"


def test_calculator_basic() -> None:
    assert calculator.invoke({"expression": "(3+4)*2.5"}) == "(3+4)*2.5 = 17.5"
    assert calculator.invoke({"expression": "2^10"}) == "2**10 = 1024"
    assert calculator.invoke({"expression": "sqrt(144)"}) == "sqrt(144) = 12"


def test_calculator_rejects_unsafe_input() -> None:
    assert "计算失败" in calculator.invoke({"expression": "1/0"})
    assert "计算失败" in calculator.invoke({"expression": "__import__('os').system('ls')"})
    assert "计算失败" in calculator.invoke({"expression": "a + 1"})
    assert "计算失败" in calculator.invoke({"expression": "1 +"})


def test_current_time_default_and_explicit_timezone() -> None:
    default = get_current_time.invoke({})
    assert "Asia/Shanghai" in default
    utc = get_current_time.invoke({"timezone": "UTC"})
    assert "UTC" in utc


def test_current_time_falls_back_on_unknown_timezone() -> None:
    result = get_current_time.invoke({"timezone": "Mars/Olympus"})
    assert "未知时区" in result


def test_knowledge_search_hits_and_misses() -> None:
    hit = search_knowledge_base.invoke({"query": "checkpointer 记忆"})
    assert "lc-002" in hit
    miss = search_knowledge_base.invoke({"query": "quantum banana"})
    assert "未找到" in miss
