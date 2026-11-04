"""登录/注册/刷新/登出/改密 与越权隔离的接口测试（内存仓储，全程离线）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import DEFAULT_PASSWORD


def test_register_returns_user_and_tokens(client: TestClient) -> None:
    response = client.post(
        "/api/v1/auth/register",
        json={"username": "alice", "password": DEFAULT_PASSWORD, "email": "Alice@Example.com"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["user"]["username"] == "alice"
    assert body["user"]["email"] == "alice@example.com"  # 统一小写
    assert body["user"]["role"] == "user"
    assert "password" not in body["user"]  # 绝不回传哈希
    tokens = body["tokens"]
    assert tokens["token_type"] == "bearer"
    assert tokens["access_token"] and tokens["refresh_token"]
    assert tokens["expires_in"] > 0


def test_register_rejects_duplicate_username_case_insensitively(client: TestClient) -> None:
    assert client.post(
        "/api/v1/auth/register", json={"username": "Alice", "password": DEFAULT_PASSWORD}
    ).status_code == 201
    conflict = client.post(
        "/api/v1/auth/register", json={"username": "alice", "password": DEFAULT_PASSWORD}
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "conflict"


def test_register_rejects_weak_or_invalid_input(client: TestClient) -> None:
    assert (
        client.post("/api/v1/auth/register", json={"username": "ab", "password": DEFAULT_PASSWORD}).status_code
        == 422
    )
    assert (
        client.post("/api/v1/auth/register", json={"username": "bad name", "password": DEFAULT_PASSWORD}).status_code
        == 422
    )
    # 密码太短 -> 业务校验 400
    assert (
        client.post("/api/v1/auth/register", json={"username": "shortpw", "password": "abc"}).status_code
        == 400
    )
    # 常见弱口令
    weak = client.post("/api/v1/auth/register", json={"username": "weakpw", "password": "password123"})
    assert weak.status_code == 400
    assert weak.json()["error"]["code"] == "validation_error"


def test_login_with_username_and_email(client: TestClient, make_user) -> None:
    make_user("bob", DEFAULT_PASSWORD, email="bob@example.com")

    by_name = client.post("/api/v1/auth/login", json={"identifier": "bob", "password": DEFAULT_PASSWORD})
    assert by_name.status_code == 200
    assert by_name.json()["user"]["username"] == "bob"

    by_email = client.post(
        "/api/v1/auth/login", json={"identifier": "BOB@example.com", "password": DEFAULT_PASSWORD}
    )
    assert by_email.status_code == 200


def test_login_rejects_wrong_password_without_leaking_existence(client: TestClient, make_user) -> None:
    make_user("carol", DEFAULT_PASSWORD)

    wrong_password = client.post(
        "/api/v1/auth/login", json={"identifier": "carol", "password": "totally-wrong"}
    )
    unknown_user = client.post(
        "/api/v1/auth/login", json={"identifier": "nobody", "password": "totally-wrong"}
    )
    assert wrong_password.status_code == unknown_user.status_code == 401
    # 两种失败的 code / message 必须完全一致，否则可被用来枚举账号
    assert wrong_password.json() == unknown_user.json()
    assert wrong_password.json()["error"]["code"] == "invalid_credentials"


def test_me_requires_token_and_returns_profile(client: TestClient, auth_headers) -> None:
    assert client.get("/api/v1/auth/me").status_code == 401

    bad = client.get("/api/v1/auth/me", headers={"Authorization": "Bearer not-a-token"})
    assert bad.status_code == 401
    assert bad.json()["error"]["code"] == "invalid_token"
    assert bad.headers.get("www-authenticate", "").lower().startswith("bearer")

    ok = client.get("/api/v1/auth/me", headers=auth_headers)
    assert ok.status_code == 200
    assert ok.json()["username"] == "alice"


def test_refresh_rotates_token_and_old_one_dies(client: TestClient, make_user) -> None:
    _, payload = make_user("dave")
    refresh_token = payload["tokens"]["refresh_token"]

    first = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert first.status_code == 200
    new_refresh = first.json()["tokens"]["refresh_token"]
    assert new_refresh != refresh_token

    # 旧 refresh token 已被轮换作废
    reused = client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    assert reused.status_code == 401
    assert reused.json()["error"]["code"] == "invalid_refresh_token"

    # 新 access token 可用
    headers = {"Authorization": f"Bearer {first.json()['tokens']['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200


def test_logout_revokes_access_and_refresh(client: TestClient, make_user) -> None:
    headers, payload = make_user("erin")
    refresh_token = payload["tokens"]["refresh_token"]

    assert client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token}, headers=headers).status_code == 204

    # access token 立刻失效（服务端会话被吊销）
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
    # refresh 同样失效
    assert client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}).status_code == 401


def test_logout_is_idempotent(client: TestClient, make_user) -> None:
    headers, _ = make_user("frank")
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 204
    # 二次登出（此时 token 已无效）应返回 401 而不是 500
    assert client.post("/api/v1/auth/logout", headers=headers).status_code == 401


def test_change_password_invalidates_old_tokens(client: TestClient, make_user) -> None:
    headers, payload = make_user("grace")
    other_headers, _ = make_user("grace2")  # 另一个账号，确保互不影响

    changed = client.post(
        "/api/v1/auth/password",
        json={"current_password": DEFAULT_PASSWORD, "new_password": "N3w-secret-pass"},
        headers=headers,
    )
    assert changed.status_code == 200
    new_headers = {"Authorization": f"Bearer {changed.json()['tokens']['access_token']}"}

    # 旧 access token 因 password_changed_at 失效
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 401
    assert client.get("/api/v1/auth/me", headers=new_headers).status_code == 200
    # 其它账号不受影响
    assert client.get("/api/v1/auth/me", headers=other_headers).status_code == 200

    # 旧密码不能再登录，新密码可以
    assert client.post(
        "/api/v1/auth/login", json={"identifier": "grace", "password": DEFAULT_PASSWORD}
    ).status_code == 401
    assert client.post(
        "/api/v1/auth/login", json={"identifier": "grace", "password": "N3w-secret-pass"}
    ).status_code == 200


def test_change_password_rejects_wrong_current(client: TestClient, make_user) -> None:
    headers, _ = make_user("heidi")
    response = client.post(
        "/api/v1/auth/password",
        json={"current_password": "wrong-one", "new_password": "N3w-secret-pass"},
        headers=headers,
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_auth_config_is_public(client: TestClient) -> None:
    body = client.get("/api/v1/auth/config").json()
    assert body["auth_required"] is True
    assert body["allow_register"] is True
    assert body["password_min_length"] >= 6
    assert "jwt_secret" not in str(body)


def test_protected_endpoints_require_token(client: TestClient) -> None:
    for method, path in (
        ("get", "/api/v1/sessions"),
        ("post", "/api/v1/chat"),
        ("post", "/api/v1/chat/stream"),
        ("get", "/api/v1/agent/info"),
    ):
        response = getattr(client, method)(path, **({"json": {"message": "hi"}} if method == "post" else {}))
        assert response.status_code == 401, f"{method} {path} 应要求登录"


def test_auth_required_false_allows_anonymous_chat() -> None:
    """`AGENT_AUTH_REQUIRED=false` 时无需 token 也能对话，但历史仍按匿名账号隔离。"""
    from fastapi.testclient import TestClient as _TestClient

    from app.core.config import Settings
    from app.main import create_app as _create_app
    from tests.conftest import TEST_JWT_SECRET

    settings = Settings(
        env="test",
        provider="mock",
        model="mock-echo",
        enabled_tools="calculator",
        log_level="WARNING",
        database_url=None,
        jwt_secret=TEST_JWT_SECRET,
        auth_required=False,
    )
    app = _create_app(settings)
    with _TestClient(app) as anonymous:
        chat = anonymous.post("/api/v1/chat", json={"message": "/calc 1+1"})
        assert chat.status_code == 200, chat.text
        session_id = chat.json()["session_id"]

        listed = anonymous.get("/api/v1/sessions").json()
        assert listed["total"] == 1
        assert listed["sessions"][0]["session_id"] == session_id

        # 登录接口仍然可用；带 token 的请求会按真实用户隔离，看不到匿名会话
        registered = anonymous.post(
            "/api/v1/auth/register",
            json={"username": "realuser", "password": DEFAULT_PASSWORD},
        )
        assert registered.status_code == 201
        real_headers = {"Authorization": f"Bearer {registered.json()['tokens']['access_token']}"}
        assert anonymous.get("/api/v1/sessions", headers=real_headers).json()["total"] == 0


def test_disabled_user_cannot_use_existing_token(client: TestClient, make_user) -> None:
    _, payload = make_user("ivan")
    headers = {"Authorization": f"Bearer {payload['tokens']['access_token']}"}
    assert client.get("/api/v1/auth/me", headers=headers).status_code == 200

    # 管理员在库里把账号禁用（直接改仓储状态，模拟运维动作）。
    # 用 TestClient 自己的事件循环执行，避免内存仓储的 asyncio.Lock 跨循环绑定问题。
    users = client.app.state.repositories.users
    client.portal.call(users.set_status, payload["user"]["id"], 0)

    blocked = client.get("/api/v1/auth/me", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "forbidden"

    # 被禁用的账号也不能再登录
    assert client.post(
        "/api/v1/auth/login", json={"identifier": "ivan", "password": DEFAULT_PASSWORD}
    ).status_code == 403
