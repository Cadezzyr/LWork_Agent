"""PostgreSQL 存储层的集成测试（默认跳过）。

    $env:AGENT_TEST_DATABASE_URL = "postgresql://postgres:root@127.0.0.1:5432/agent_chat"
    pytest tests/test_postgres_storage.py -v

注意：用例只操作随机生成的数据（随机用户名 / 随机会话 ID），
不会动库里已有的其它记录；但会真实建表数据，请指向测试库。
"""

from __future__ import annotations

import os
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app
from tests.conftest import TEST_JWT_SECRET, parse_sse

DATABASE_URL = os.environ.get("AGENT_TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="未设置 AGENT_TEST_DATABASE_URL，跳过 PostgreSQL 集成测试",
)


def _unique(prefix: str = "u") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


@pytest.fixture()
def pg_settings() -> Settings:
    return Settings(
        env="test",
        provider="mock",
        model="mock-echo",
        enabled_tools="",
        log_level="WARNING",
        database_url=DATABASE_URL,
        jwt_secret=TEST_JWT_SECRET,
        auth_required=True,
        auth_allow_register=True,
    )


@pytest.fixture()
def pg_client(pg_settings: Settings):
    app = create_app(pg_settings)
    with TestClient(app) as test_client:
        assert test_client.app.state.repositories.backend == "postgres", (
            "数据库未连上，检查 AGENT_TEST_DATABASE_URL"
        )
        yield test_client


def _register(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "s3cret-pass"},
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['tokens']['access_token']}"}


def test_health_reports_postgres(pg_client: TestClient) -> None:
    body = pg_client.get("/api/v1/health").json()
    assert body["components"]["storage"]["status"] == "ok"
    assert "PostgreSQL" in body["components"]["storage"]["detail"]


def test_register_and_login_against_postgres(pg_client: TestClient) -> None:
    username = _unique()
    headers = _register(pg_client, username)
    assert pg_client.get("/api/v1/auth/me", headers=headers).json()["username"] == username

    # 大小写不敏感登录（citext）
    login = pg_client.post(
        "/api/v1/auth/login",
        json={"identifier": username.upper(), "password": "s3cret-pass"},
    )
    assert login.status_code == 200

    # 重复注册 -> 409（唯一索引）
    duplicate = pg_client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "s3cret-pass"},
    )
    assert duplicate.status_code == 409


def test_conversation_and_messages_are_persisted(pg_client: TestClient) -> None:
    headers = _register(pg_client, _unique())
    chat = pg_client.post(
        "/api/v1/chat",
        json={"message": "/calc 9*9"},
        headers=headers,
    )
    assert chat.status_code == 200, chat.text
    session_id = chat.json()["session_id"]

    detail = pg_client.get(f"/api/v1/sessions/{session_id}", headers=headers).json()
    assert [item["role"] for item in detail["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert detail["turns"] == 1
    assert detail["message_count"] == 4
    assert detail["messages"][1]["tool_calls"][0]["args"] == {"expression": "9*9"}
    assert "81" in (detail["messages"][1]["tool_calls"][0]["result"] or "")


def test_history_survives_process_restart(pg_client: TestClient, pg_settings: Settings) -> None:
    """换个应用实例（等价于重启进程）后，仍能读到历史并继续对话。"""
    username = _unique("restart")
    headers = _register(pg_client, username)
    session_id = pg_client.post(
        "/api/v1/chat",
        json={"message": "记住：鹦鹉"},
        headers=headers,
    ).json()["session_id"]

    # 重新创建 app：InMemorySaver 与进程内缓存全部重建，只有数据库是共享的
    with TestClient(create_app(pg_settings)) as fresh:
        login = fresh.post(
            "/api/v1/auth/login",
            json={"identifier": username, "password": "s3cret-pass"},
        )
        fresh_headers = {"Authorization": f"Bearer {login.json()['tokens']['access_token']}"}

        listed = fresh.get("/api/v1/sessions", headers=fresh_headers).json()
        assert listed["total"] >= 1
        assert any(item["session_id"] == session_id for item in listed["sessions"])

        detail = fresh.get(f"/api/v1/sessions/{session_id}", headers=fresh_headers).json()
        assert detail["messages"][0]["content"] == "记住：鹦鹉"

        # 上下文由数据库回灌，可以继续这轮对话
        second = fresh.post(
            "/api/v1/chat",
            json={"message": "我刚才让你记住什么？", "session_id": session_id},
            headers=fresh_headers,
        )
        assert second.status_code == 200, second.text
        after = fresh.get(f"/api/v1/sessions/{session_id}", headers=fresh_headers).json()
        assert after["turns"] == 2


def test_cross_user_isolation_in_postgres(pg_client: TestClient) -> None:
    alice = _register(pg_client, _unique("alice"))
    bob = _register(pg_client, _unique("bob"))

    session_id = pg_client.post(
        "/api/v1/chat",
        json={"message": "私密内容"},
        headers=alice,
    ).json()["session_id"]

    assert pg_client.get(f"/api/v1/sessions/{session_id}", headers=bob).status_code == 404
    assert pg_client.delete(f"/api/v1/sessions/{session_id}", headers=bob).status_code == 404
    assert pg_client.get("/api/v1/sessions", headers=bob).json()["total"] == 0
    assert pg_client.get(f"/api/v1/sessions/{session_id}", headers=alice).status_code == 200


def test_soft_delete_hides_but_keeps_rows(pg_client: TestClient) -> None:
    headers = _register(pg_client, _unique())
    session_id = pg_client.post(
        "/api/v1/chat",
        json={"message": "待删除"},
        headers=headers,
    ).json()["session_id"]

    assert pg_client.delete(f"/api/v1/sessions/{session_id}", headers=headers).status_code == 204
    assert pg_client.get(f"/api/v1/sessions/{session_id}", headers=headers).status_code == 404
    assert pg_client.get("/api/v1/sessions", headers=headers).json()["total"] == 0

    def _count() -> int:
        import psycopg2

        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT COUNT(*) FROM conversations WHERE id = %s AND deleted_at IS NOT NULL",
                    (session_id,),
                )
                return int(cursor.fetchone()[0])
        finally:
            connection.close()

    assert _count() == 1


def test_refresh_token_hash_is_stored_not_raw(pg_client: TestClient) -> None:
    username = _unique()
    response = pg_client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "s3cret-pass"},
    )
    refresh_token = response.json()["tokens"]["refresh_token"]

    def _token_hash() -> str:
        import psycopg2

        connection = psycopg2.connect(DATABASE_URL, connect_timeout=5)
        try:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s.token_hash FROM user_sessions s
                      JOIN users u ON u.id = s.user_id
                     WHERE u.username = %s
                     ORDER BY s.created_at DESC LIMIT 1
                    """,
                    (username,),
                )
                return str(cursor.fetchone()[0]).strip()
        finally:
            connection.close()

    stored = _token_hash()
    assert stored != refresh_token
    assert len(stored) == 64  # SHA-256 十六进制

    # 用明文 refresh token 仍能刷新成功（服务端自己算哈希比对）
    refreshed = pg_client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert refreshed.status_code == 200


def test_stream_and_history_in_postgres(pg_client: TestClient) -> None:
    headers = _register(pg_client, _unique())
    with pg_client.stream(
        "POST", "/api/v1/chat/stream", json={"message": "/calc 5+5"}, headers=headers
    ) as response:
        events = parse_sse("".join(response.iter_text()))

    assert events[-1][0] == "done"
    session_id = dict(events)["session"]["session_id"]
    detail = pg_client.get(f"/api/v1/sessions/{session_id}", headers=headers).json()
    assert detail["turns"] == 1
    assert len(detail["messages"]) == 4
