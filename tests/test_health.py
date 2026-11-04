"""健康检查 / 元信息接口测试。"""

from __future__ import annotations

from fastapi.testclient import TestClient


def test_service_info_returns_metadata(client: TestClient) -> None:
    response = client.get("/api/v1/info")
    assert response.status_code == 200
    body = response.json()
    assert body["api_prefix"] == "/api/v1"
    assert body["endpoints"]["chat"] == "/api/v1/chat"
    assert body["ui"] == "/ui/"


def test_root_redirects_to_ui(client: TestClient) -> None:
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/ui/"


def test_ui_serves_chat_page(client: TestClient) -> None:
    response = client.get("/ui/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert "LangChain Agent" in response.text
    # 静态资源也要能取到（前端无构建步骤）
    assert client.get("/ui/app.js").status_code == 200
    assert client.get("/ui/styles.css").status_code == 200
    assert client.get("/ui/markdown.js").status_code == 200


def test_health_reports_ok(client: TestClient) -> None:
    response = client.get("/api/v1/health")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["components"]["agent"]["status"] == "ok"


def test_ready_reports_runtime(client: TestClient) -> None:
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert "calculator" in body["tools"]


def test_agent_info(user_client: TestClient) -> None:
    response = user_client.get("/api/v1/agent/info")
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "mock"
    assert body["agent_name"] == "test-agent"
    assert body["ready"] is True
    names = {tool["name"] for tool in body["tools"]}
    # 通用 + 工作区文件 + 联网
    assert {
        "calculator",
        "get_current_time",
        "search_knowledge_base",
        "read_workspace_file",
        "write_workspace_file",
        "web_search",
    } <= names
    assert len(names) >= 11


def test_tools_endpoint_exposes_schema(user_client: TestClient) -> None:
    response = user_client.get("/api/v1/agent/tools")
    assert response.status_code == 200
    tools = {item["name"]: item for item in response.json()}
    assert "expression" in tools["calculator"]["parameters"]["properties"]
    assert tools["get_current_time"]["description"]
    assert "path" in tools["read_workspace_file"]["parameters"]["properties"]
    assert "query" in tools["web_search"]["parameters"]["properties"]
