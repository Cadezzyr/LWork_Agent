from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from langchain_core.tools import ToolException, tool
from pydantic import BaseModel, Field

SKILLS_DIR = Path(r"D:\project_test\skills")

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n(.*)$", re.DOTALL)


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    body: str
    metadata: dict[str, Any] = field(default_factory=dict)


def _parse_skill_md(text: str, name: str) -> Skill:
    m = _FRONTMATTER_RE.match(text)
    if not m:  # 没有 frontmatter 时退化为纯 Markdown
        return Skill(name=name, description="", body=text.strip())
    meta = yaml.safe_load(m.group(1)) or {}
    return Skill(
        name=meta.get("name", name),
        description=meta.get("description", ""),
        body=m.group(2).strip(),
        metadata=meta,
    )

@lru_cache(maxsize=1)
def load_skill_registry() -> dict[str, Skill]:
    """启动时扫描技能目录，缓存起来。"""
    registry: dict[str, Skill] = {}
    if SKILLS_DIR.is_dir():
        for d in sorted(SKILLS_DIR.iterdir()):
            f = d / "SKILL.md"
            if d.is_dir() and f.is_file():
                registry[d.name] = _parse_skill_md(
                    f.read_text(encoding="utf-8"), d.name
                )
    return registry


def _format_skill(skill: Skill) -> str:
    return f"# 已加载技能: {skill.name}\n\n{skill.body}"


@tool
def load_skill(skill_name: str) -> str:
    """加载专门的技能提示。
    返回技能的提示和上下文，之后请按该技能的要求完成任务。
    """
    registry = load_skill_registry()

    # 关键：用注册表白名单校验，而不是直接拼路径，防止路径穿越
    if skill_name not in registry:
        available = "、".join(sorted(registry)) or "（无）"
        raise ToolException(f"未知技能 {skill_name!r}。可用技能：{available}")

    return _format_skill(registry[skill_name])