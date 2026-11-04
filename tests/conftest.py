"""pytest 共享夹具。

所有用例默认：
- 模型用 ``mock`` provider，不发起真实请求、不需要 API Key；
- 数据层用**内存仓储**，不依赖 PostgreSQL（真实库的验证见 ``test_postgres_storage.py``，
  仅在设置 ``AGENT_TEST_DATABASE_URL`` 时运行）。
"""

from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings, reset_settings_cache
from app.main import create_app

# 测试用 JWT 密钥：固定值，便于断言"重启后 token 仍有效"之类的行为
TEST_JWT_SECRET = "test-secret-key-for-unit-tests-0123456789"
DEFAULT_PASSWORD = "s3cret-pass"


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    reset_settings_cache()
    yield
    reset_settings_cache()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        env="test",
        provider="mock",
        model="mock-echo",
        agent_name="test-agent",
        # 留空 = 注册全部内置工具（含工作区文件工具与联网工具）
        enabled_tools="",
        log_level="WARNING",
        # 数据层：不配数据库 => 内存仓储（每个 TestClient 一套，天然隔离）
        database_url=None,
        jwt_secret=TEST_JWT_SECRET,
        auth_required=True,
        auth_allow_register=True,
    )


@pytest.fixture()
def client(settings: Settings) -> Iterator[TestClient]:
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def make_user(client: TestClient) -> Callable[..., dict[str, Any]]:
    """注册一个用户并返回 ``(auth_headers, payload)``。"""

    def _make(
        username: str = "alice",
        password: str = DEFAULT_PASSWORD,
        *,
        email: str | None = None,
    ) -> tuple[dict[str, str], dict[str, Any]]:
        response = client.post(
            "/api/v1/auth/register",
            json={"username": username, "password": password, "email": email},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        headers = {"Authorization": f"Bearer {payload['tokens']['access_token']}"}
        return headers, payload

    return _make


@pytest.fixture()
def auth_headers(make_user: Callable[..., Any]) -> dict[str, str]:
    headers, _ = make_user()
    return headers


@pytest.fixture()
def user_client(client: TestClient, auth_headers: dict[str, str]) -> TestClient:
    """已登录的客户端（默认用户 alice）。"""
    client.headers.update(auth_headers)
    return client


@pytest.fixture()
def admin_client(settings: Settings) -> Iterator[TestClient]:
    """以管理员身份登录的客户端（用于覆盖需要 admin 的接口，例如改工作区）。"""
    admin_settings = settings.model_copy(update={"auth_admin_usernames": "root"})
    app = create_app(admin_settings)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/v1/auth/register",
            json={"username": "root", "password": DEFAULT_PASSWORD},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        assert payload["user"]["role"] == "admin"
        test_client.headers.update(
            {"Authorization": f"Bearer {payload['tokens']['access_token']}"}
        )
        yield test_client


@pytest.fixture()
def workdir() -> Iterator[Path]:
    """项目内的临时目录。

    不用 ``tmp_path``（系统 TEMP），因为在受限/沙箱环境里系统临时目录可能不可写，
    而项目自身目录一定可写。
    """
    base = Path(__file__).resolve().parent / "_workdir"
    base.mkdir(exist_ok=True)
    target = base / f"case_{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=True)
    try:
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


def parse_sse(body: str) -> list[tuple[str, dict]]:
    """把 SSE 响应体解析成 [(event, data), ...]。"""
    events: list[tuple[str, dict]] = []
    for block in body.strip().split("\n\n"):
        event_name: str | None = None
        data: dict = {}
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: ") :]
            elif line.startswith("data: "):
                data = json.loads(line[len("data: ") :])
        if event_name:
            events.append((event_name, data))
    return events
