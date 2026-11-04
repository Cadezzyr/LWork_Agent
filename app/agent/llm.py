"""模型工厂：把配置转换成 LangChain 的 ChatModel 实例。

默认使用 OpenAI 兼容协议，因此 DeepSeek / 通义千问 / vLLM / Ollama 等服务
只需改 ``AGENT_BASE_URL`` + ``AGENT_MODEL`` 即可接入。
"""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel

from app.core.config import Settings
from app.core.exceptions import AgentUnavailableError
from app.core.logging import get_logger

logger = get_logger("agent.llm")


def build_chat_model(settings: Settings) -> BaseChatModel:
    """按 ``settings.provider`` 构建对话模型。"""
    provider = settings.provider

    if provider == "mock":
        from app.agent.mock_model import MockChatModel

        logger.info("使用离线 Mock 模型（provider=mock），不会发起真实 LLM 请求")
        return MockChatModel()

    if provider == "openai":
        return _build_openai(settings)

    if provider == "anthropic":
        return _build_anthropic(settings)

    if provider == "deepseek":
        return _build_deepseek(settings)

    raise AgentUnavailableError(f"不支持的 provider：{provider}")


def _build_openai(settings: Settings) -> BaseChatModel:
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - 依赖缺失时的友好提示
        raise AgentUnavailableError(
            "未安装 langchain-openai，请执行 `pip install langchain-openai` 后重试"
        ) from exc

    kwargs: dict[str, Any] = {
        "model": settings.model,
        "temperature": settings.temperature,
        "timeout": settings.request_timeout,
        "max_retries": settings.max_retries,
        "stream_usage": True,
    }
    if settings.max_tokens:
        kwargs["max_tokens"] = settings.max_tokens
    if settings.base_url_value:
        kwargs["base_url"] = settings.base_url_value
    if settings.api_key_value:
        kwargs["api_key"] = settings.api_key_value

    try:
        model = ChatOpenAI(**kwargs)
    except Exception as exc:  # 缺少 Key、base_url 非法等
        reason = settings.readiness_error() or str(exc)
        raise AgentUnavailableError(f"构建 OpenAI 兼容模型失败：{reason}") from exc

    logger.info(
        "已构建模型 provider=openai model=%s base_url=%s temperature=%s",
        settings.model,
        settings.base_url_value or "https://api.openai.com/v1",
        settings.temperature,
    )
    return model


def _build_anthropic(settings: Settings) -> BaseChatModel:
    try:
        from langchain_anthropic import ChatAnthropic
    except ImportError as exc:
        raise AgentUnavailableError(
            "未安装 langchain-anthropic，请执行 `pip install langchain-anthropic` 后重试"
        ) from exc

    kwargs: dict[str, Any] = {
        "model": settings.model,
        "temperature": settings.temperature,
        # Anthropic 接口必须显式指定 max_tokens
        "max_tokens": settings.max_tokens or 2048,
        "timeout": settings.request_timeout,
        "max_retries": settings.max_retries,
    }
    if settings.base_url_value:
        kwargs["base_url"] = settings.base_url_value
    if settings.api_key_value:
        kwargs["api_key"] = settings.api_key_value

    try:
        model = ChatAnthropic(**kwargs)
    except Exception as exc:
        reason = settings.readiness_error() or str(exc)
        raise AgentUnavailableError(f"构建 Anthropic 模型失败：{reason}") from exc

    logger.info("已构建模型 provider=anthropic model=%s", settings.model)
    return model


def _build_deepseek(settings: Settings) -> BaseChatModel:
    try:
        from langchain_deepseek import ChatDeepSeek
    except ImportError as exc:
        raise AgentUnavailableError(
            "无法导入 langchain-deepseek："
            f"{type(exc).__name__}: {exc}\n"
            "可能未安装，也可能是版本不兼容"
            "（langchain-deepseek 1.1.x 需要 langchain-core>=1.4.7）。\n"
            "请执行 `pip install -r requirements.txt`，"
            "或改用 OpenAI 兼容协议：AGENT_PROVIDER=openai + "
            "AGENT_BASE_URL=https://api.deepseek.com/v1"
        ) from exc

    kwargs: dict[str, Any] = {
        "model": settings.model,
        "temperature": settings.temperature,
        "timeout": settings.request_timeout,
        "max_retries": settings.max_retries,
    }
    if settings.max_tokens:
        kwargs["max_tokens"] = settings.max_tokens
    if settings.base_url_value:
        kwargs["base_url"] = settings.base_url_value
    if settings.api_key_value:
        kwargs["api_key"] = settings.api_key_value

    try:
        model = ChatDeepSeek(**kwargs)
    except Exception as exc:
        reason = settings.readiness_error() or str(exc)
        raise AgentUnavailableError(f"构建 DeepSeek 模型失败：{reason}") from exc

    logger.info(
        "已构建模型 provider=deepseek model=%s base_url=%s temperature=%s",
        settings.model,
        settings.base_url_value or "https://api.deepseek.com",
        settings.temperature,
    )
    return model
