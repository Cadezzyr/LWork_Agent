"""工作区配置接口测试：根目录由接口在运行时设置，不落配置文件。"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.agent.workspace import bind_workspace_user, get_workspace_manager


def _user_id(client: TestClient) -> str:
    return client.get("/api/v1/auth/me").json()["id"]


def test_get_workspace_defaults_to_project_root(admin_client: TestClient) -> None:
    body = admin_client.get("/api/v1/workspace").json()
    assert body["root"] == body["default_root"]
    assert body["is_default"] is True
    assert body["allow_writes"] is True
    assert body["exists"] is True


def test_put_workspace_switches_root(admin_client: TestClient, workdir: Path) -> None:
    (workdir / "demo.txt").write_text("hello", encoding="utf-8")

    response = admin_client.put("/api/v1/workspace", json={"root": str(workdir)})
    assert response.status_code == 200
    body = response.json()
    assert Path(body["root"]) == workdir.resolve()
    assert body["is_default"] is False
    assert body["file_count"] >= 1
    # 该用户自己的工作区管理器跟着变（Agent 工具用的就是它）
    assert get_workspace_manager(_user_id(admin_client)).root == workdir.resolve()

    # 复原，避免影响其它用例
    restored = admin_client.put("/api/v1/workspace", json={"root": None}).json()
    assert restored["is_default"] is True


def test_put_workspace_rejects_bad_paths(admin_client: TestClient, workdir: Path) -> None:
    missing = admin_client.put("/api/v1/workspace", json={"root": str(workdir / "nope")})
    assert missing.status_code == 400
    assert missing.json()["error"]["code"] == "invalid_workspace"

    relative = admin_client.put("/api/v1/workspace", json={"root": "relative/path"})
    assert relative.status_code == 400
    assert "绝对路径" in relative.json()["error"]["message"]


def test_put_workspace_toggles_writes(admin_client: TestClient) -> None:
    body = admin_client.put("/api/v1/workspace", json={"allow_writes": False}).json()
    assert body["allow_writes"] is False
    assert get_workspace_manager(_user_id(admin_client)).allow_writes is False
    admin_client.put("/api/v1/workspace", json={"allow_writes": True})


def test_validate_endpoint(admin_client: TestClient, workdir: Path) -> None:
    ok = admin_client.post("/api/v1/workspace/validate", json={"root": str(workdir)}).json()
    assert ok["valid"] is True and ok["is_dir"] is True

    bad = admin_client.post("/api/v1/workspace/validate", json={"root": str(workdir / "nope")}).json()
    assert bad["valid"] is False and bad["exists"] is False

    relative = admin_client.post("/api/v1/workspace/validate", json={"root": "foo/bar"}).json()
    assert relative["valid"] is False
    assert "绝对路径" in relative["message"]


def test_tree_endpoint_lists_entries(admin_client: TestClient, workdir: Path) -> None:
    (workdir / "src").mkdir()
    (workdir / "src" / "main.py").write_text("print(1)", encoding="utf-8")
    (workdir / "README.md").write_text("doc", encoding="utf-8")
    admin_client.put("/api/v1/workspace", json={"root": str(workdir)})
    try:
        body = admin_client.get("/api/v1/workspace/tree?depth=2").json()
        paths = [item["path"] for item in body["entries"]]
        assert "src/" in paths
        assert "src/main.py" in paths
        assert "README.md" in paths
    finally:
        admin_client.put("/api/v1/workspace", json={"root": None})


def test_tools_use_workspace_set_via_api(admin_client: TestClient, workdir: Path) -> None:
    """接口改完之后，文件工具立刻按该用户的新根目录工作。"""
    from app.agent.tools.files import read_workspace_file, write_workspace_file

    admin_client.put("/api/v1/workspace", json={"root": str(workdir)})
    try:
        # 工具在 Agent 运行时由 bind_workspace_user 绑定用户，这里手动模拟同样的上下文
        with bind_workspace_user(_user_id(admin_client)):
            write_workspace_file.invoke({"path": "from_api.txt", "content": "hi"})
            assert (workdir / "from_api.txt").read_text(encoding="utf-8") == "hi"
            assert "hi" in read_workspace_file.invoke({"path": "from_api.txt"})
    finally:
        admin_client.put("/api/v1/workspace", json={"root": None})


def test_agent_info_reports_workspace_tools(admin_client: TestClient) -> None:
    tools = {item["name"] for item in admin_client.get("/api/v1/agent/tools").json()}
    assert {"read_workspace_file", "write_workspace_file", "search_workspace"} <= tools
    assert {"web_search", "fetch_web_page"} <= tools


def test_workspace_can_be_changed_by_any_user(user_client: TestClient, workdir: Path) -> None:
    """工作区按用户隔离：普通用户也能改，且只影响自己。"""
    assert user_client.get("/api/v1/workspace").status_code == 200

    response = user_client.put("/api/v1/workspace", json={"root": str(workdir)})
    assert response.status_code == 200
    assert Path(response.json()["root"]) == workdir.resolve()

    user_client.put("/api/v1/workspace", json={"root": None})

    # 未登录时全部拒绝
    user_client.headers.pop("Authorization", None)
    assert user_client.get("/api/v1/workspace").status_code == 401
    assert user_client.put("/api/v1/workspace", json={"allow_writes": True}).status_code == 401


def test_workspace_is_isolated_between_users(
    client: TestClient, make_user, workdir: Path
) -> None:
    """一个用户改工作区，不会影响另一个用户。"""
    alice_headers, _ = make_user("alice")
    bob_headers, _ = make_user("bob")

    changed = client.put(
        "/api/v1/workspace", json={"root": str(workdir)}, headers=alice_headers
    )
    assert changed.status_code == 200
    assert Path(changed.json()["root"]) == workdir.resolve()

    bob = client.get("/api/v1/workspace", headers=bob_headers).json()
    assert bob["is_default"] is True
    assert Path(bob["root"]) != workdir.resolve()

    alice = client.get("/api/v1/workspace", headers=alice_headers).json()
    assert Path(alice["root"]) == workdir.resolve()
