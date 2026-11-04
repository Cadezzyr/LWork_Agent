"""模型未配置时的降级行为：服务可启动，但对话接口返回 503。"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.conftest import TEST_JWT_SECRET


@pytest.fixture()
def unconfigured_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("AGENT_API_KEY", raising=False)
    # 显式关掉数据库，避免读到开发者本机 .env 里的 AGENT_DATABASE_URL 而污染断言
    settings = Settings(
        env="test",
        provider="openai",
        model="gpt-4o-mini",
        api_key=None,
        log_level="WARNING",
        database_url=None,
        jwt_secret=TEST_JWT_SECRET,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


def test_health_reports_degraded(unconfigured_client: TestClient) -> None:
    body = unconfigured_client.get("/api/v1/health").json()
    assert body["status"] == "degraded"
    assert body["components"]["model_provider"]["status"] == "degraded"


def test_ready_returns_503(unconfigured_client: TestClient) -> None:
    response = unconfigured_client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"


def test_chat_returns_503_with_actionable_message(unconfigured_client: TestClient) -> None:
    # 注意：unconfigured_client 是独立的应用实例（独立的内存仓储），
    # 因此必须在本实例上注册账号，不能复用其它 fixture 的 token。
    registered = unconfigured_client.post(
        "/api/v1/auth/register",
        json={"username": "nomodel", "password": "s3cret-pass"},
    )
    assert registered.status_code == 201
    headers = {"Authorization": f"Bearer {registered.json()['tokens']['access_token']}"}

    response = unconfigured_client.post(
        "/api/v1/chat",
        json={"message": "hi"},
        headers=headers,
    )
    assert response.status_code == 503
    body = response.json()
    assert body["error"]["code"] == "agent_unavailable"
    assert "AGENT_API_KEY" in body["error"]["message"] or "API Key" in body["error"]["message"]


def test_auth_still_works_without_model(unconfigured_client: TestClient) -> None:
    """模型没配好时，登录/历史查询必须照常可用（数据库与模型互不依赖）。"""
    registered = unconfigured_client.post(
        "/api/v1/auth/register",
        json={"username": "nomodel2", "password": "s3cret-pass"},
    )
    assert registered.status_code == 201
    headers = {"Authorization": f"Bearer {registered.json()['tokens']['access_token']}"}
    assert unconfigured_client.get("/api/v1/auth/me", headers=headers).status_code == 200
    assert unconfigured_client.get("/api/v1/sessions", headers=headers).status_code == 200


def test_health_reports_storage_component(unconfigured_client: TestClient) -> None:
    body = unconfigured_client.get("/api/v1/health").json()
    # 未配置数据库 -> 存储为 degraded，且说明历史不持久
    assert body["components"]["storage"]["status"] == "degraded"
    assert "AGENT_DATABASE_URL" in body["components"]["storage"]["detail"]
    assert body["components"]["auth"]["status"] == "ok"
