"""知识库检索工具（示例实现）。

这里用内存中的小型文档集合演示"检索类工具"的接入方式。
真实项目可替换为向量库（如 Chroma / pgvector）+ ``langchain`` 的 Retriever：

    retriever = vectorstore.as_retriever(search_kwargs={"k": top_k})
    docs = retriever.invoke(query)
"""

from __future__ import annotations

import re

from langchain_core.tools import tool

_KNOWLEDGE_BASE: list[dict[str, str]] = [
    {
        "id": "lc-001",
        "title": "LangChain 1.x 的 create_agent",
        "content": (
            "langchain.agents.create_agent 是 LangChain 1.x 推荐的 Agent 构建入口，"
            "底层编译为 LangGraph 图（节点为 agent / tools）。它接受 model、tools、"
            "system_prompt、checkpointer 等参数，返回可 invoke/stream 的 CompiledStateGraph。"
        ),
        "tags": "langchain,create_agent,agent,langgraph",
    },
    {
        "id": "lc-002",
        "title": "会话记忆与 checkpointer",
        "content": (
            "给 create_agent 传入 checkpointer（如 langgraph.checkpoint.memory.InMemorySaver）后，"
            "通过 config={'configurable': {'thread_id': session_id}} 即可按会话保存消息历史，"
            "实现多轮对话记忆。生产环境建议换成 SqliteSaver 或 PostgresSaver 做持久化。"
        ),
        "tags": "memory,checkpointer,thread_id,会话,记忆",
    },
    {
        "id": "lc-003",
        "title": "流式输出",
        "content": (
            "CompiledStateGraph 支持 astream(..., stream_mode=['messages', 'updates'])，"
            "'messages' 逐 token 返回模型输出（AIMessageChunk），'updates' 返回每个节点的"
            "增量状态，可用来感知工具调用与工具结果。HTTP 层通常用 SSE 转发给前端。"
        ),
        "tags": "stream,sse,流式,astream",
    },
    {
        "id": "lc-004",
        "title": "工具定义与调用",
        "content": (
            "使用 langchain_core.tools.tool 装饰器把 Python 函数注册为工具，"
            "docstring 会成为工具描述并进入模型上下文；参数类型注解会被转成 JSON Schema。"
            "建议工具内部捕获异常并返回可读错误文本，避免整轮对话失败。"
        ),
        "tags": "tool,工具,pydantic,json schema",
    },
    {
        "id": "lc-005",
        "title": "常见的 OpenAI 兼容服务接入",
        "content": (
            "使用 langchain-openai 的 ChatOpenAI，配置 base_url 与 api_key 就能接入 DeepSeek"
            "（https://api.deepseek.com/v1）、通义千问 DashScope 兼容模式、vLLM、Ollama"
            "（http://localhost:11434/v1）等 OpenAI 兼容服务。"
        ),
        "tags": "openai,deepseek,qwen,ollama,base_url",
    },
]


def _tokenize(text: str) -> set[str]:
    lowered = text.lower()
    tokens = set(re.findall(r"[a-z0-9_]+", lowered))
    tokens.update(re.findall(r"[\u4e00-\u9fff]{2,}", lowered))
    return tokens


def _score(query_tokens: set[str], document: dict[str, str]) -> float:
    haystack = _tokenize(" ".join(document.get(key, "") for key in ("title", "content", "tags")))
    if not haystack:
        return 0.0
    overlap = query_tokens & haystack
    if not overlap:
        # 中文查询按子串兜底匹配，避免分词粒度过粗导致完全无命中
        joined = " ".join(document.get(key, "") for key in ("title", "content", "tags"))
        return 1.0 if any(token in joined for token in query_tokens if len(token) >= 2) else 0.0
    return len(overlap) / len(query_tokens)


@tool
def search_knowledge_base(query: str, top_k: int = 3) -> str:
    """检索内部知识库（LangChain / LangGraph 相关主题），返回最相关的若干条片段。

    当用户询问框架用法、概念、最佳实践等需要查资料的问题时使用本工具。

    Args:
        query: 检索关键词或问题，例如 "LangGraph 如何做多轮记忆"。
        top_k: 返回的片段数量，1~5，默认 3。
    """
    top_k = max(1, min(int(top_k or 3), 5))
    query_tokens = _tokenize(query)
    if not query_tokens:
        return "检索失败：关键词为空"

    ranked = sorted(
        ((_score(query_tokens, doc), doc) for doc in _KNOWLEDGE_BASE),
        key=lambda item: item[0],
        reverse=True,
    )
    hits = [(score, doc) for score, doc in ranked if score > 0][:top_k]
    if not hits:
        return f"知识库中未找到与 '{query}' 相关的内容（共 {len(_KNOWLEDGE_BASE)} 条文档）。"

    lines = [f"命中 {len(hits)} 条（关键词：{query}）："]
    for index, (score, doc) in enumerate(hits, start=1):
        lines.append(
            f"{index}. [{doc['id']}] {doc['title']}（相关度 {score:.2f}）\n   {doc['content']}"
        )
    return "\n".join(lines)
