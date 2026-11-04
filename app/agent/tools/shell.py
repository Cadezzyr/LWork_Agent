"""终端工具：在工作区内执行命令 / 运行 Python 片段（高风险能力，带多道闸门）。

这是本项目里权限最大的工具，护栏如下（任何一条不满足都会直接拒绝，而不是报错中断对话）：

1. **写权限闸门**：工作区必须处于「允许写/删文件」状态（页面上的开关），只读模式下一律拒绝；
2. **工作目录**：命令的 cwd 必须是工作区内的目录，越界拒绝；
3. **危险命令黑名单**：`rm -rf /`、`mkfs`、`format C:`、`shutdown`、`reg add`、`diskpart`、
   `curl ... | sh`、`iex` 之类一律拒绝；
4. **可选白名单**：设置 ``AGENT_SHELL_ALLOWLIST=git,python,dir`` 后只允许这些命令；
5. **超时与输出上限**：默认 30 秒、最多 120 秒；输出超过 8000 字符截断；
6. **环境变量脱敏**：子进程不会继承 `*_API_KEY` / `*TOKEN*` / `*SECRET*` / `*PASSWORD*` 等变量，
   避免模型随口一条命令把密钥打印或外传出去；
7. **审计日志**：每条命令都会以 WARNING 级别记录。

注意：这些都是"降低误伤"而不是"完全隔离"。真要跑不可信代码，请放到容器/虚拟机里。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

from langchain_core.tools import tool

from app.agent.workspace import WorkspaceError, get_workspace_manager
from app.core.config import get_settings
from app.core.logging import get_logger

logger = get_logger("agent.tools.shell")

MAX_TIMEOUT = 120.0
DEFAULT_MAX_OUTPUT = 8000

# 明显不可逆/危险的操作：命中即拒绝（大小写不敏感）
DENY_PATTERNS: tuple[str, ...] = (
    r"\brm\s+(-[a-zA-Z]*\s+)*-?[rRfF]{1,2}\s+(/|~|\$HOME|/\*)\b",
    r"\brm\s+-[rRfF]{1,2}\b",
    r"\bmkfs(\.[a-z0-9]+)?\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\bpoweroff\b",
    r"\bformat\s+[a-z]:",
    r"\bdiskpart\b",
    r"\bbcdedit\b",
    r"\bcipher\s+/w",
    r"\breg\s+(add|delete|import)\b",
    r"\bnet\s+user\b",
    r"\bnet\s+localgroup\b",
    r"\btakeown\b",
    r"\bchmod\s+(-r\s+)?777\s+/",
    r"\(\s*\)\s*\{\s*:\|:&\s*\}",  # fork bomb
    r"\b(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(ba)?sh\b",
    r"\b(iwr|invoke-webrequest|invoke-restmethod)\b[^\n|]*\|[^\n]*\b(iex|invoke-expression)\b",
    r"\biex\b",
    r"\binvoke-expression\b",
    r"\bpowershell(\.exe)?\b[^\n]*\s-(e|en|enc|encodedcommand)\b",
    r"\bdel\s+/[fsq]\b",
    r"\brd\s+/s\b",
    r"\brmdir\s+/s\b",
    r"remove-item\b[^\n]*\s-recurse\b[^\n]*\s[a-z]:\\?\s*$",
    r">\s*/dev/(sd|hd|nvme)",
    r">\s*\\\\\.\\physicaldrive",
)

_SECRET_ENV_HINTS = (
    "API_KEY",
    "APIKEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "TOKEN",
    "CREDENTIAL",
    "PRIVATE_KEY",
)


def _child_env() -> dict[str, str]:
    """构造子进程环境：剔除一切看起来像密钥的变量。"""
    env: dict[str, str] = {}
    for key, value in os.environ.items():
        upper = key.upper()
        if any(hint in upper for hint in _SECRET_ENV_HINTS):
            continue
        env[key] = value
    return env


def _resolve_shell(shell: str) -> list[str] | None:
    """返回 shell 前缀（命令本身追加在最后）；返回 None 表示平台不支持。"""
    choice = (shell or "auto").strip().lower()
    if os.name == "nt":
        if choice in {"auto", "powershell", "pwsh"}:
            exe = shutil.which("pwsh") or shutil.which("powershell")
            if exe:
                return [exe, "-NoProfile", "-NonInteractive", "-Command"]
        if choice in {"auto", "cmd"}:
            return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c"]
        return None
    if choice in {"auto", "sh", "bash"}:
        bash = shutil.which("bash")
        if choice == "bash" and bash:
            return [bash, "-lc"]
        return ["/bin/sh", "-c"]
    return None


def _check_policy(command: str, *, apply_allowlist: bool = True) -> str | None:
    """检查危险命令与白名单，返回拒绝原因；通过则返回 None。"""
    text = command.strip()
    lowered = text.lower()
    for pattern in DENY_PATTERNS:
        if re.search(pattern, lowered, re.IGNORECASE):
            return f"命令命中危险模式，已拒绝执行：/{pattern}/"

    if not apply_allowlist:
        return None

    allowlist = [
        item.strip().lower() for item in get_settings().shell_allowlist.split(",") if item.strip()
    ]
    if allowlist:
        first = text.split()[0].strip("\"'") if text.split() else ""
        name = Path(first).name.lower()
        if name not in allowlist:
            return (
                f"白名单模式只允许这些命令：{', '.join(sorted(allowlist))}"
                f"（当前命令首词是 {name or '空'}）"
            )
    return None


def _truncate_output(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    head = limit * 2 // 3
    tail = limit - head
    return text[:head] + "\n…（输出过长，中间已省略）…\n" + text[-tail:], True


def _run(
    argv: list[str],
    *,
    cwd: Path,
    timeout: float,
    label: str,
) -> str:
    """执行子进程并把结果格式化；异常全部转成可读文本。"""
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return (
            f"执行超时（>{timeout:g}s）：{label}\n"
            "建议缩小命令范围、加 --no-pager 之类参数，或提高 timeout 参数（上限 120 秒）"
        )
    except FileNotFoundError as exc:
        return f"执行失败：找不到命令或解释器（{exc}）"
    except OSError as exc:
        return f"执行失败：{type(exc).__name__}: {exc}"

    output = completed.stdout or ""
    limit = get_settings().shell_max_output or DEFAULT_MAX_OUTPUT
    body, truncated = _truncate_output(output, max(500, limit))
    lines = [
        f"命令：{label}",
        f"目录：{cwd}",
        f"退出码：{completed.returncode}",
        "输出：",
        body.strip() if body.strip() else "(无输出)",
    ]
    if truncated:
        lines.append("（输出已截断，可用更精确的命令查看剩余部分）")
    return "\n".join(lines)


@tool
def run_command(command: str, cwd: str = ".", timeout: float = 0, shell: str = "auto") -> str:
    """在工作区内执行终端命令，返回退出码与输出（stdout+stderr 合并）。

    适合：查看目录/文件内容、运行测试、git 操作、安装依赖、调用 CLI 工具。
    注意：命令的工作目录限制在工作区内；只读模式（工作区面板关掉写权限）下会直接拒绝；
    `rm -rf /`、`format`、`shutdown`、`reg add`、`curl | sh` 等危险命令被黑名单拦截。

    Args:
        command: 要执行的命令，例如 "dir"、"git status"、"python -m pytest -q"。
        cwd: 相对工作区的执行目录，默认 "."。
        timeout: 超时秒数，0 表示使用默认（30 秒），上限 120 秒。
        shell: auto | powershell | cmd（Windows）或 auto | sh | bash（Linux/macOS），默认 auto。
    """
    text = (command or "").strip()
    if not text:
        return "执行失败：command 不能为空"

    manager = get_workspace_manager()
    try:
        workdir = manager.resolve(cwd or ".", require_writes=True)
    except WorkspaceError as exc:
        return f"执行失败：{exc}"
    if not workdir.is_dir():
        return f"执行失败：目录不存在 -> {cwd}"

    policy_error = _check_policy(text)
    if policy_error:
        logger.warning("终端命令被策略拒绝：%s", text)
        return f"执行失败：{policy_error}"

    prefix = _resolve_shell(shell)
    if prefix is None:
        return f"执行失败：当前平台不支持 shell={shell}"

    settings = get_settings()
    limit = float(timeout) if timeout and timeout > 0 else float(settings.shell_timeout or 30.0)
    limit = max(1.0, min(limit, MAX_TIMEOUT))

    logger.warning("终端执行（cwd=%s）：%s", workdir, text)
    return _run([*prefix, text], cwd=workdir, timeout=limit, label=text)


@tool
def run_python(code: str, timeout: float = 0) -> str:
    """在项目所用 Python 解释器里运行一小段代码并返回输出。

    适合：数据处理、快速验证一段逻辑、调用标准库；比 `run_command` 少了引号转义麻烦。
    代码会写进工作区的临时文件再执行，执行完即删除；同样受只读模式与超时限制。

    Args:
        code: 要执行的 Python 源码（可多行）。
        timeout: 超时秒数，0 表示使用默认（30 秒），上限 120 秒。
    """
    source = code or ""
    if not source.strip():
        return "执行失败：code 不能为空"

    manager = get_workspace_manager()
    try:
        root = manager.resolve(".", require_writes=True)
    except WorkspaceError as exc:
        return f"执行失败：{exc}"

    policy_error = _check_policy(source, apply_allowlist=False)
    if policy_error:
        logger.warning("Python 片段被策略拒绝（可能包含危险调用）")
        return f"执行失败：{policy_error}"

    settings = get_settings()
    limit = float(timeout) if timeout and timeout > 0 else float(settings.shell_timeout or 30.0)
    limit = max(1.0, min(limit, MAX_TIMEOUT))

    temp_dir = root / ".agent_tmp"
    temp_dir.mkdir(parents=True, exist_ok=True)
    script = temp_dir / f"snippet_{uuid.uuid4().hex[:8]}.py"
    script.write_text(source, encoding="utf-8")
    logger.warning("执行 Python 片段：%s（%s 字符）", script.name, len(source))
    try:
        return _run([sys.executable, str(script)], cwd=root, timeout=limit, label=f"python {script.name}")
    finally:
        script.unlink(missing_ok=True)
        try:
            temp_dir.rmdir()
        except OSError:
            pass


__all__ = ["run_command", "run_python"]
