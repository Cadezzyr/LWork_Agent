"""对话与会话接口测试（mock provider，全程离线；数据层为内存仓储）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from tests.conftest import parse_sse


def test_chat_creates_session_and_replies(user_client: TestClient) -> None:
    response = user_client.post("/api/v1/chat", json={"message": "你好"})
    assert response.status_code == 200
    body = response.json()
    assert body["session_id"].startswith("sess_")
    assert body["reply"]
    assert [message["role"] for message in body["messages"]] == ["user", "assistant"]
    assert body["tool_calls"] == []
    assert body["elapsed_ms"] >= 0


def test_chat_invokes_tool(user_client: TestClient) -> None:
    body = user_client.post("/api/v1/chat", json={"message": "/calc (3+4)*2.5"}).json()
    assert len(body["tool_calls"]) == 1
    call = body["tool_calls"][0]
    assert call["name"] == "calculator"
    assert call["args"] == {"expression": "(3+4)*2.5"}
    assert call["status"] == "success"
    assert "17.5" in (call["result"] or "")
    assert "17.5" in body["reply"]
    # 本轮消息包含：user / assistant(tool_call) / tool / assistant
    roles = [message["role"] for message in body["messages"]]
    assert roles == ["user", "assistant", "tool", "assistant"]


def test_chat_multi_turn_keeps_memory(user_client: TestClient) -> None:
    first = user_client.post("/api/v1/chat", json={"message": "第一轮"}).json()
    session_id = first["session_id"]

    second = user_client.post(
        "/api/v1/chat",
        json={"message": "第二轮", "session_id": session_id},
    ).json()
    assert second["session_id"] == session_id
    # 第二轮只应返回本轮新增的消息
    assert [message["role"] for message in second["messages"]] == ["user", "assistant"]

    detail = user_client.get(f"/api/v1/sessions/{session_id}").json()
    assert detail["turns"] == 2
    assert len(detail["messages"]) == 4
    assert detail["messages"][0]["content"] == "第一轮"
    # 标题自动取自首句
    assert detail["title"] == "第一轮"


def test_chat_rejects_blank_message(user_client: TestClient) -> None:
    assert user_client.post("/api/v1/chat", json={"message": "   "}).status_code == 422
    assert user_client.post("/api/v1/chat", json={}).status_code == 422
    assert (
        user_client.post("/api/v1/chat", json={"message": "hi", "session_id": "bad id!"}).status_code
        == 422
    )


def test_sessions_crud(user_client: TestClient) -> None:
    created = user_client.post("/api/v1/sessions", json={"session_id": "custom_1"})
    assert created.status_code == 201
    assert created.json()["session_id"] == "custom_1"

    listed = user_client.get("/api/v1/sessions").json()
    assert listed["total"] == 1
    assert listed["sessions"][0]["session_id"] == "custom_1"

    assert user_client.get("/api/v1/sessions/custom_1").json()["messages"] == []
    assert user_client.get("/api/v1/sessions/unknown").status_code == 404

    assert user_client.delete("/api/v1/sessions/custom_1").status_code == 204
    assert user_client.get("/api/v1/sessions/custom_1").status_code == 404
    assert user_client.delete("/api/v1/sessions/custom_1").status_code == 404


def test_stream_emits_expected_events(user_client: TestClient) -> None:
    with user_client.stream("POST", "/api/v1/chat/stream", json={"message": "/calc 12*12"}) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        body = "".join(response.iter_text())

    events = parse_sse(body)
    names = [name for name, _ in events]
    assert names[0] == "session"
    assert "tool_call" in names
    assert "tool_result" in names
    assert "token" in names
    assert names[-2:] == ["final", "done"]

    final = dict(events)["final"]
    assert "144" in final["reply"]
    assert final["tool_calls"][0]["name"] == "calculator"
    assert final["session_id"] == dict(events)["session"]["session_id"]
    assert final["saved"] is True


def test_stream_second_turn_reuses_session(user_client: TestClient) -> None:
    with user_client.stream("POST", "/api/v1/chat/stream", json={"message": "hello"}) as response:
        first = parse_sse("".join(response.iter_text()))
    session_id = dict(first)["session"]["session_id"]

    with user_client.stream(
        "POST", "/api/v1/chat/stream", json={"message": "again", "session_id": session_id}
    ) as response:
        second = parse_sse("".join(response.iter_text()))

    assert dict(second)["session"]["session_id"] == session_id
    detail = user_client.get(f"/api/v1/sessions/{session_id}").json()
    assert detail["turns"] == 2


def test_stream_failure_returns_json_401_not_sse(client: TestClient) -> None:
    """鉴权失败必须在开流之前完成，返回普通 JSON 而不是 SSE。"""
    response = client.post("/api/v1/chat/stream", json={"message": "hi"})
    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "unauthorized"
