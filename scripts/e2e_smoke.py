"""端到端冒烟脚本：对真实运行的服务（默认 http://127.0.0.1:8010）跑一遍登录+对话+历史流程。

用法：
    python scripts/e2e_smoke.py [base_url]

不依赖 pytest，直接 httpx 打真实 HTTP 接口，用于部署后自检。
"""

from __future__ import annotations

import json
import sys
import uuid

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8010"
API = BASE + "/api/v1"


def main() -> int:
    username = f"smoke_{uuid.uuid4().hex[:8]}"
    password = "s3cret-pass"
    client = httpx.Client(timeout=60.0)

    print(f"1) 注册 {username}")
    response = client.post(
        f"{API}/auth/register",
        json={"username": username, "password": password},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    tokens = payload["tokens"]
    headers = {"Authorization": f"Bearer {tokens['access_token']}"}
    print("   user:", payload["user"]["username"], "role:", payload["user"]["role"])

    print("2) 未授权访问应被拒绝")
    assert client.get(f"{API}/sessions").status_code == 401
    print("   OK (401)")

    print("3) /auth/me")
    me = client.get(f"{API}/auth/me", headers=headers)
    assert me.status_code == 200, me.text
    print("   ", me.json()["username"])

    print("4) 同步对话（含工具调用 + 中文）")
    chat = client.post(
        f"{API}/chat",
        json={"message": "/calc (3+4)*2.5 — 中文也要正确保存"},
        headers=headers,
    )
    assert chat.status_code == 200, chat.text
    body = chat.json()
    session_id = body["session_id"]
    print("   session:", session_id, "| reply:", body["reply"][:40])

    print("5) 流式对话（同一会话）")
    with client.stream(
        "POST",
        f"{API}/chat/stream",
        json={"message": "再说一句", "session_id": session_id},
        headers=headers,
    ) as stream:
        assert stream.status_code == 200, stream.read()
        events = []
        buffer = ""
        for chunk in stream.iter_text():
            buffer += chunk
            while "\n\n" in buffer:
                frame, buffer = buffer.split("\n\n", 1)
                name = None
                data = None
                for line in frame.splitlines():
                    if line.startswith("event: "):
                        name = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                if name:
                    events.append(name)
                    if name == "final":
                        print("   final.saved =", data.get("saved"), "| tool_calls:", len(data.get("tool_calls") or []))
    print("   events:", events)

    print("6) 历史列表")
    listed = client.get(f"{API}/sessions", headers=headers).json()
    assert listed["total"] == 1, listed
    assert listed["sessions"][0]["session_id"] == session_id
    print("   total:", listed["total"], "| title:", listed["sessions"][0]["title"])

    print("7) 历史详情（含工具轨迹 + 中文）")
    detail = client.get(f"{API}/sessions/{session_id}", headers=headers).json()
    roles = [item["role"] for item in detail["messages"]]
    print("   roles:", roles, "| turns:", detail["turns"], "| messages:", len(detail["messages"]))
    assert "中文也要正确保存" in detail["messages"][0]["content"], detail["messages"][0]["content"]
    assert detail["messages"][1]["tool_calls"], "工具调用轨迹应被保存"

    print("8) 越权隔离（另一个账号看不到）")
    other = f"smoke_{uuid.uuid4().hex[:8]}"
    other_tokens = client.post(
        f"{API}/auth/register", json={"username": other, "password": password}
    ).json()["tokens"]
    other_headers = {"Authorization": f"Bearer {other_tokens['access_token']}"}
    assert client.get(f"{API}/sessions/{session_id}", headers=other_headers).status_code == 404
    assert client.get(f"{API}/sessions", headers=other_headers).json()["total"] == 0
    print("   OK (404 / 空列表)")

    print("9) 刷新令牌")
    refreshed = client.post(f"{API}/auth/refresh", json={"refresh_token": tokens["refresh_token"]})
    assert refreshed.status_code == 200, refreshed.text
    new_headers = {"Authorization": f"Bearer {refreshed.json()['tokens']['access_token']}"}
    assert client.get(f"{API}/auth/me", headers=new_headers).status_code == 200
    print("   OK")

    print("10) 删除会话 + 登出")
    assert client.delete(f"{API}/sessions/{session_id}", headers=new_headers).status_code == 204
    assert client.get(f"{API}/sessions/{session_id}", headers=new_headers).status_code == 404
    assert client.post(f"{API}/auth/logout", headers=new_headers).status_code == 204
    assert client.get(f"{API}/auth/me", headers=new_headers).status_code == 401
    print("   OK")

    print("\n全部通过 ✅")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
