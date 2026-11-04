"""工作区文件工具测试（读 / 写 / 查 / 改 / 删 + 边界防护）。"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from app.agent.tools.files import (
    delete_workspace_file,
    edit_workspace_file,
    list_workspace_files,
    read_workspace_file,
    search_workspace,
    write_workspace_file,
)
from app.agent.workspace import init_workspace_manager


@pytest.fixture()
def workspace(workdir: Path) -> Path:
    """把全局工作区指向临时目录（测试结束后由 _restore_workspace 恢复）。"""
    init_workspace_manager(workdir)
    return workdir


@pytest.fixture(autouse=True)
def _restore_workspace() -> Iterator[None]:
    from app.agent.workspace import get_workspace_manager

    default_root = get_workspace_manager().default_root
    yield
    init_workspace_manager(default_root)


def test_write_read_roundtrip(workspace: Path) -> None:
    message = write_workspace_file.invoke({"path": "notes/hello.md", "content": "# 你好\n\n第一行\n"})
    assert "已写入" in message
    assert (workspace / "notes" / "hello.md").read_text(encoding="utf-8") == "# 你好\n\n第一行\n"

    body = read_workspace_file.invoke({"path": "notes/hello.md"})
    assert "共 3 行" in body
    assert "1 | # 你好" in body
    assert "3 | 第一行" in body


def test_append_mode(workspace: Path) -> None:
    write_workspace_file.invoke({"path": "a.txt", "content": "one\n"})
    write_workspace_file.invoke({"path": "a.txt", "content": "two\n", "mode": "append"})
    assert (workspace / "a.txt").read_text(encoding="utf-8") == "one\ntwo\n"


def test_list_and_search(workspace: Path) -> None:
    write_workspace_file.invoke({"path": "pkg/module.py", "content": "def hello():\n    return 1\n"})
    write_workspace_file.invoke({"path": "README.md", "content": "项目说明\n"})

    listing = list_workspace_files.invoke({"path": ".", "recursive": True})
    assert "pkg/module.py" in listing
    assert "README.md" in listing

    # 敏感文件与噪声目录不参与列举
    (workspace / ".env").write_text("SECRET=1", encoding="utf-8")
    assert ".env" not in list_workspace_files.invoke({"path": ".", "pattern": ".*"})

    hits = search_workspace.invoke({"query": "def hello"})
    assert "pkg/module.py:1" in hits


def test_edit_requires_exact_match(workspace: Path) -> None:
    write_workspace_file.invoke({"path": "code.py", "content": "value = 1\nother = 2\n"})

    missing = edit_workspace_file.invoke({"path": "code.py", "old_text": "value = 9", "new_text": "x"})
    assert "未在 code.py 中找到" in missing

    ok = edit_workspace_file.invoke({"path": "code.py", "old_text": "value = 1", "new_text": "value = 42"})
    assert "替换 1 处" in ok
    assert "value = 42" in (workspace / "code.py").read_text(encoding="utf-8")


def test_delete_only_files(workspace: Path) -> None:
    write_workspace_file.invoke({"path": "tmp.txt", "content": "x"})
    assert "已删除" in delete_workspace_file.invoke({"path": "tmp.txt"})
    assert not (workspace / "tmp.txt").exists()
    assert "文件不存在" in delete_workspace_file.invoke({"path": "tmp.txt"})


def test_path_escape_is_rejected(workspace: Path) -> None:
    outside = workspace.parent / "outside.txt"
    outside.write_text("secret", encoding="utf-8")

    for path in ("../outside.txt", str(outside), "../../etc/passwd"):
        result = read_workspace_file.invoke({"path": path})
        assert "操作失败" in result and "超出工作区范围" in result


def test_sensitive_files_are_denied(workspace: Path) -> None:
    (workspace / ".env").write_text("AGENT_API_KEY=sk-xxx", encoding="utf-8")
    (workspace / "server.pem").write_text("-----BEGIN", encoding="utf-8")

    assert "敏感配置" in read_workspace_file.invoke({"path": ".env"})
    assert "敏感配置" in read_workspace_file.invoke({"path": "server.pem"})


def test_readonly_mode_blocks_writes(workspace: Path) -> None:
    from app.agent.workspace import get_workspace_manager

    get_workspace_manager().set_allow_writes(False)
    try:
        result = write_workspace_file.invoke({"path": "blocked.txt", "content": "x"})
        assert "只读模式" in result
        assert not (workspace / "blocked.txt").exists()

        # 只读模式下仍可读
        (workspace / "readable.txt").write_text("ok", encoding="utf-8")
        assert "ok" in read_workspace_file.invoke({"path": "readable.txt"})
    finally:
        get_workspace_manager().set_allow_writes(True)


def test_binary_file_is_refused(workspace: Path) -> None:
    (workspace / "blob.bin").write_bytes(b"\x00\x01\x02binary")
    assert "二进制" in read_workspace_file.invoke({"path": "blob.bin"})


def test_writes_follow_runtime_workspace_change(workdir: Path) -> None:
    from app.agent.workspace import get_workspace_manager

    first = workdir / "ws_one"
    second = workdir / "ws_two"
    first.mkdir()
    second.mkdir()
    manager = init_workspace_manager(first)

    write_workspace_file.invoke({"path": "only_in_first.txt", "content": "a"})
    assert (first / "only_in_first.txt").exists()

    manager.set_root(str(second))
    write_workspace_file.invoke({"path": "only_in_second.txt", "content": "b"})
    assert (second / "only_in_second.txt").exists()
    # 旧目录不再可访问
    assert "超出工作区范围" in read_workspace_file.invoke({"path": str(first / "only_in_first.txt")})
    assert get_workspace_manager().root == second
