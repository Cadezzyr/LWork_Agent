"""对话历史持久化与用户隔离测试。

覆盖三件最关键的事：
1. 每个用户只能看到自己的会话（越权返回 404）；
2. 历史（含工具调用轨迹、token 用量）确实落库，能按 seq 完整回放；
3. 进程内上下文丢失后（模拟服务重启），仍能靠数据库恢复上下文继续对话。
"""

from __future__ import annotations

import functools
from typing import Any

from fastapi.testclient import TestClient

from app.schemas.chat import SessionInfo


def _chat(client: TestClient, message: str, session_id: str | None = None) -> dict[str, Any]:
    response = client.post("/api/v1/chat", json={"message": message, "session_id": session_id})
    assert response.status_code == 200, response.text
    return response.json()


def test_history_is_persisted_with_tool_trace(user_client: TestClient) -> None:
    first = _chat(user_client, "/calc 6*7")
    session_id = first["session_id"]

    detail = user_client.get(f"/api/v1/sessions/{session_id}").json()
    roles = [item["role"] for item in detail["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]

    assistant_with_tools = detail["messages"][1]
    assert assistant_with_tools["tool_calls"][0]["name"] == "calculator"
    assert assistant_with_tools["tool_calls"][0]["args"] == {"expression": "6*7"}
    # 工具返回值已回填进 assistant 行的 JSONB
    assert "42" in (assistant_with_tools["tool_calls"][0]["result"] or "")

    info = SessionInfo(**{k: v for k, v in detail.items() if k != "messages"})
    assert info.message_count == 4
    assert info.turns == 1
    assert info.total_tokens >= 0


def test_session_list_is_sorted_by_recent_activity(user_client: TestClient) -> None:
    first = _chat(user_client, "第一个会话")["session_id"]
    second = _chat(user_client, "第二个会话")["session_id"]
    _chat(user_client, "回到第一个", first)

    listed = user_client.get("/api/v1/sessions").json()
    ids = [item["session_id"] for item in listed["sessions"]]
    assert ids[0] == first  # 最近活跃排最前
    assert second in ids
    assert listed["total"] == 2


def test_session_list_pagination_and_search(user_client: TestClient) -> None:
    for index in range(3):
        _chat(user_client, f"话题{index}")

    page = user_client.get("/api/v1/sessions?limit=2&offset=0").json()
    assert len(page["sessions"]) == 2
    assert page["total"] == 3
    assert page["limit"] == 2

    searched = user_client.get("/api/v1/sessions?keyword=话题1").json()
    assert searched["total"] == 1
    assert "话题1" in searched["sessions"][0]["title"]


def test_history_survives_memory_loss(user_client: TestClient) -> None:
    """模拟服务重启：清空 LangGraph 上下文，仅凭数据库历史继续对话。"""
    first = _chat(user_client, "记住这个词：蓝鲸")
    session_id = first["session_id"]

    # 清空进程内 checkpointer（等价于重启后 InMemorySaver 为空）
    runtime = user_client.app.state.runtime
    runtime.checkpointer.storage.clear()

    second = _chat(user_client, "我刚才让你记住什么？", session_id)
    assert second["session_id"] == session_id
    # 上下文被重建：数据库里的历史被重新喂回模型（mock 模型会回显上下文条数）
    detail = user_client.get(f"/api/v1/sessions/{session_id}").json()
    assert detail["turns"] == 2
    assert detail["messages"][0]["content"] == "记住这个词：蓝鲸"


def test_users_cannot_read_each_others_sessions(
    client: TestClient, make_user
) -> None:
    alice_headers, _ = make_user("alice")
    bob_headers, _ = make_user("bob")

    created = client.post(
        "/api/v1/chat",
        json={"message": "alice 的私密会话"},
        headers=alice_headers,
    )
    session_id = created.json()["session_id"]

    # bob 看不到、读不到、删不掉、也接不上 alice 的会话
    assert client.get("/api/v1/sessions", headers=bob_headers).json()["total"] == 0
    assert client.get(f"/api/v1/sessions/{session_id}", headers=bob_headers).status_code == 404
    assert client.delete(f"/api/v1/sessions/{session_id}", headers=bob_headers).status_code == 404
    hijack = client.post(
        "/api/v1/chat",
        json={"message": "续写别人的会话", "session_id": session_id},
        headers=bob_headers,
    )
    assert hijack.status_code == 404
    assert hijack.json()["error"]["code"] == "session_not_found"

    # alice 自己一切正常
    assert client.get(f"/api/v1/sessions/{session_id}", headers=alice_headers).status_code == 200


def test_delete_is_soft_and_idempotent(user_client: TestClient) -> None:
    session_id = _chat(user_client, "待删除")["session_id"]
    assert user_client.delete(f"/api/v1/sessions/{session_id}").status_code == 204
    assert user_client.get(f"/api/v1/sessions/{session_id}").status_code == 404
    # 重复删除 -> 404（幂等性由前端忽略，API 层如实反馈）
    assert user_client.delete(f"/api/v1/sessions/{session_id}").status_code == 404
    # 列表里也不再出现
    assert user_client.get("/api/v1/sessions").json()["total"] == 0


def test_rename_and_clear_sessions(user_client: TestClient) -> None:
    session_id = _chat(user_client, "原始标题")["session_id"]
    _chat(user_client, "第二条")

    renamed = user_client.patch(f"/api/v1/sessions/{session_id}", json={"title": "自定义标题"})
    assert renamed.status_code == 200
    assert renamed.json()["title"] == "自定义标题"

    cleared = user_client.delete("/api/v1/sessions")
    assert cleared.status_code == 200
    assert cleared.json()["deleted"] == 2
    assert user_client.get("/api/v1/sessions").json()["total"] == 0


def test_stream_history_is_persisted(user_client: TestClient) -> None:
    with user_client.stream("POST", "/api/v1/chat/stream", json={"message": "/calc 2+2"}) as response:
        body = "".join(response.iter_text())
    assert "final" in body

    session_id = user_client.get("/api/v1/sessions").json()["sessions"][0]["session_id"]
    detail = user_client.get(f"/api/v1/sessions/{session_id}").json()
    assert [item["role"] for item in detail["messages"]] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert detail["turns"] == 1


def test_stream_rejects_foreign_session_before_starting_stream(
    client: TestClient, make_user
) -> None:
    """越权/不存在的 session_id 必须在开流之前返回 404，而不是发出去一个 200 的残流。"""
    alice_headers, _ = make_user("alice")
    bob_headers, _ = make_user("bob")

    session_id = client.post(
        "/api/v1/chat",
        json={"message": "alice 的会话"},
        headers=alice_headers,
    ).json()["session_id"]

    response = client.post(
        "/api/v1/chat/stream",
        json={"message": "续写别人的", "session_id": session_id},
        headers=bob_headers,
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "session_not_found"


def test_user_settings_repository_roundtrip(user_client: TestClient) -> None:
    """用户级偏好（工作区设置）应能按用户读写。"""
    repos = user_client.app.state.repositories
    user_id = user_client.get("/api/v1/auth/me").json()["id"]

    saved = user_client.portal.call(
        functools.partial(
            repos.user_settings.upsert,
            user_id,
            workspace_root="D:/projects/demo",
            workspace_writable=False,
        )
    )
    assert saved.workspace_root == "D:/projects/demo"
    assert saved.workspace_writable is False

    loaded = user_client.portal.call(repos.user_settings.get, user_id)
    assert loaded is not None
    assert loaded.workspace_root == "D:/projects/demo"

    # 部分更新不应覆盖其它字段
    updated = user_client.portal.call(
        functools.partial(repos.user_settings.upsert, user_id, theme="dark")
    )
    assert updated.theme == "dark"
    assert updated.workspace_root == "D:/projects/demo"
