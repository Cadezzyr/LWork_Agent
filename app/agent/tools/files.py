"""工作区文件工具：读 / 写 / 查 / 改 / 删。

所有路径都经过 :class:`app.agent.workspace.WorkspaceManager` 校验：
必须在当前工作区内（工作区由前端页面设置），敏感文件与写权限开关同样生效。
出错时统一返回「操作失败：…」文本，让模型自己向用户解释，而不是抛异常中断整轮对话。
"""

from __future__ import annotations

import re
from langchain_core.tools import tool

from app.agent.workspace import (
    MAX_FILE_BYTES,
    WorkspaceError,
    get_workspace_manager,
)


def _describe_error(exc: Exception) -> str:
    if isinstance(exc, WorkspaceError):
        return str(exc)
    return f"{type(exc).__name__}: {exc}"


def _is_binary(raw: bytes) -> bool:
    return b"\x00" in raw[:8000]


@tool
def list_workspace_files(
    path: str = ".",
    pattern: str = "*",
    recursive: bool = False,
    max_entries: int = 200,
) -> str:
    """列出工作区中某个目录下的文件与子目录。

    Args:
        path: 相对工作区根目录的路径，默认 "." 表示根目录。
        pattern: 文件名通配符，例如 "*.py"、"*.md"、"*"。
        recursive: 是否递归子目录。
        max_entries: 最多返回多少条，1~500。
    """
    manager = get_workspace_manager()
    try:
        base = manager.resolve(path)
    except WorkspaceError as exc:
        return f"操作失败：{exc}"
    if not base.exists():
        return f"操作失败：路径不存在 -> {path}"
    if not base.is_dir():
        return f"操作失败：{path} 不是目录，读取文件请用 read_workspace_file"

    limit = max(1, min(int(max_entries or 200), 500))
    iterator = base.rglob(pattern) if recursive else base.glob(pattern)
    lines: list[str] = []
    for item in sorted(iterator, key=lambda p: p.as_posix()):
        if manager.is_skipped(item) or manager.is_sensitive(item):
            continue
        try:
            relative = manager.relative(item)
        except ValueError:
            continue
        if item.is_dir():
            lines.append(f"[DIR ] {relative}/")
        else:
            try:
                size = item.stat().st_size
            except OSError:
                size = -1
            lines.append(f"[FILE] {relative}  ({size} B)")
        if len(lines) >= limit:
            lines.append(f"...（已达上限 {limit} 条，可用 pattern 缩小范围）")
            break
    if not lines:
        return f"目录 {path} 为空，或没有匹配 {pattern} 的条目（工作区根：{manager.root}）"
    header = f"工作区根：{manager.root}\n目录 {path}（pattern={pattern}，recursive={recursive}）："
    return header + "\n" + "\n".join(lines)


@tool
def read_workspace_file(path: str, start_line: int = 1, max_lines: int = 400) -> str:
    """读取工作区内的文本文件，返回带行号的内容。

    Args:
        path: 相对工作区根目录的文件路径，例如 "app/main.py"。
        start_line: 起始行号，从 1 开始。
        max_lines: 最多读取多少行，1~2000。
    """
    manager = get_workspace_manager()
    try:
        target = manager.resolve(path)
    except WorkspaceError as exc:
        return f"操作失败：{exc}"
    if not target.is_file():
        return f"操作失败：文件不存在 -> {path}"

    size = target.stat().st_size
    if size > MAX_FILE_BYTES:
        return f"操作失败：文件 {size} B 超过上限 {MAX_FILE_BYTES} B，请改用 search_workspace 定位片段"

    raw = target.read_bytes()
    if _is_binary(raw):
        return f"操作失败：{path} 看起来是二进制文件（含 NUL 字节），无法作为文本读取"

    lines = raw.decode("utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, int(start_line or 1))
    count = max(1, min(int(max_lines or 400), 2000))
    chunk = lines[start - 1 : start - 1 + count]
    body = "\n".join(f"{start + index:>5} | {line}" for index, line in enumerate(chunk))
    tail = "" if start - 1 + count >= total else f"\n...（共 {total} 行，可调大 start_line 继续读）"
    return f"文件 {manager.relative(target)}（共 {total} 行）：\n{body}{tail}"


@tool
def write_workspace_file(path: str, content: str, mode: str = "overwrite") -> str:
    """创建或写入文本文件（需要工作区已打开写权限）。

    Args:
        path: 相对工作区根目录的文件路径，父目录不存在会自动创建。
        content: 要写入的完整文本内容。
        mode: "overwrite" 覆盖写（默认），"append" 追加写。
    """
    manager = get_workspace_manager()
    try:
        target = manager.resolve(path, require_writes=True)
    except WorkspaceError as exc:
        return f"操作失败：{exc}"

    if mode not in {"overwrite", "append"}:
        return "操作失败：mode 只能是 overwrite 或 append"
    payload = content or ""
    data = payload.encode("utf-8")
    if len(data) > MAX_FILE_BYTES:
        return f"操作失败：写入内容 {len(data)} B 超过上限 {MAX_FILE_BYTES} B"

    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a" if mode == "append" else "w", encoding="utf-8", newline="\n") as handle:
        handle.write(payload)
    action = "追加写入" if mode == "append" else "写入"
    return f"已{action} {manager.relative(target)}（{len(data)} B）"


@tool
def edit_workspace_file(path: str, old_text: str, new_text: str, replace_all: bool = False) -> str:
    """对已有文件做精确字符串替换（推荐用它改代码，而不是整篇重写）。

    Args:
        path: 相对工作区根目录的文件路径。
        old_text: 要被替换的原文，必须与文件中完全一致（包含缩进和空行）。
        new_text: 替换后的文本；传空字符串表示删除这段内容。
        replace_all: 是否替换所有匹配项；默认只替换第一处。
    """
    manager = get_workspace_manager()
    try:
        target = manager.resolve(path, require_writes=True)
    except WorkspaceError as exc:
        return f"操作失败：{exc}"
    if not target.is_file():
        return f"操作失败：文件不存在 -> {path}"
    if not old_text:
        return "操作失败：old_text 不能为空"

    text = target.read_text(encoding="utf-8")
    hits = text.count(old_text)
    if hits == 0:
        return (
            f"操作失败：未在 {path} 中找到指定内容。"
            "请先用 read_workspace_file 确认原文（缩进、空行必须完全一致）"
        )
    if hits > 1 and not replace_all:
        return f"操作失败：匹配到 {hits} 处，请提供更精确的 old_text，或设置 replace_all=true"

    updated = text.replace(old_text, new_text) if replace_all else text.replace(old_text, new_text, 1)
    target.write_text(updated, encoding="utf-8", newline="\n")
    replaced = hits if replace_all else 1
    return f"已编辑 {manager.relative(target)}，替换 {replaced} 处"


@tool
def delete_workspace_file(path: str) -> str:
    """删除工作区内的单个文件（需要写权限，不删除目录）。

    Args:
        path: 相对工作区根目录的文件路径。
    """
    manager = get_workspace_manager()
    try:
        target = manager.resolve(path, require_writes=True)
    except WorkspaceError as exc:
        return f"操作失败：{exc}"
    if target.is_dir():
        return f"操作失败：{path} 是目录，本工具只删除单个文件"
    if not target.is_file():
        return f"操作失败：文件不存在 -> {path}"

    target.unlink()
    return f"已删除 {manager.relative(target)}"


@tool
def search_workspace(
    query: str,
    pattern: str = "**/*",
    is_regex: bool = False,
    max_results: int = 40,
) -> str:
    """在工作区内按文件内容检索（只读），返回匹配的文件:行号:内容。

    Args:
        query: 关键词；is_regex=true 时按正则表达式处理。
        pattern: 文件通配符，默认 "**/*"（全部文件），可写 "**/*.py"。
        is_regex: 是否把 query 当作正则表达式。
        max_results: 最多返回多少条命中，1~200。
    """
    manager = get_workspace_manager()
    if not query:
        return "操作失败：query 不能为空"
    regex = None
    if is_regex:
        try:
            regex = re.compile(query)
        except re.error as exc:
            return f"操作失败：正则表达式无效 -> {exc}"
    needle = query.lower()
    limit = max(1, min(int(max_results or 40), 200))

    hits: list[str] = []
    scanned = 0
    for item in manager.iter_files(pattern):
        try:
            if item.stat().st_size > MAX_FILE_BYTES:
                continue
            raw = item.read_bytes()
        except OSError:
            continue
        if _is_binary(raw):
            continue
        scanned += 1
        try:
            text = raw.decode("utf-8", errors="ignore")
        except UnicodeDecodeError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            matched = bool(regex.search(line)) if regex else needle in line.lower()
            if not matched:
                continue
            hits.append(f"{manager.relative(item)}:{lineno}: {line.strip()[:300]}")
            if len(hits) >= limit:
                return "\n".join(hits) + f"\n...（已达上限 {limit} 条）"
    if not hits:
        return f"未在工作区 {manager.root} 中检索到 {query!r}（已扫描 {scanned} 个文件）"
    return f"命中 {len(hits)} 条（扫描 {scanned} 个文件）：\n" + "\n".join(hits)


__all__ = [
    "delete_workspace_file",
    "edit_workspace_file",
    "list_workspace_files",
    "read_workspace_file",
    "search_workspace",
    "write_workspace_file",
]
