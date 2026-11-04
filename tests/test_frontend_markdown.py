"""前端 Markdown 渲染器的回归测试（用 Node 跑 JS 用例，没装 Node 就跳过）。

注意：子进程的 stdout 重定向到文件而不是管道，
这样在受限环境（禁止管道）里也能拿到输出。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tests" / "frontend" / "markdown.test.js"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="未安装 Node.js，跳过前端渲染测试")
def test_markdown_renderer_cases(workdir: Path) -> None:
    log = workdir / "markdown_test.log"
    with log.open("w", encoding="utf-8") as handle:
        result = subprocess.run(
            [NODE or "node", str(SCRIPT)],
            stdout=handle,
            stderr=subprocess.STDOUT,
            timeout=120,
            cwd=str(ROOT),
        )
    output = log.read_text(encoding="utf-8", errors="replace")
    assert result.returncode == 0, f"渲染器用例失败：\n{output}"
    assert "通过" in output


def test_web_assets_exist() -> None:
    """页面依赖的静态文件必须存在（否则 /ui/ 打开就是白板）。"""
    for name in ("index.html", "app.js", "styles.css", "markdown.js"):
        assert (ROOT / "web" / name).is_file(), f"缺少 {name}"
