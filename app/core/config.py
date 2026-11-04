"""应用配置。

配置优先级（从高到低）：

1. 显式传给 ``Settings(...)`` 的参数（测试用）
2. 环境变量，例如 ``AGENT_PROVIDER``
3. 当前工作目录下的 ``.env``（可选，用于覆盖单个运行场景）
4. 项目根目录下的 ``.env``（推荐在这里改配置）
5. ``Settings`` 类里的默认值

字段名与 ``AGENT_`` 前缀一一对应：``Settings.model`` -> ``AGENT_MODEL``。
"""

from __future__ import annotations

import os
import re
import secrets
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.agent.prompt import DEFAULT_SYSTEM_PROMPT

Provider = Literal["openai", "anthropic", "deepseek", "mock"]
# 联网检索的搜索源：auto 表示按可用性自动选择（tavily 有 key 时优先）
SearchProvider = Literal["auto", "bing", "duckduckgo", "tavily"]

_DEFAULT_TOOLS = "calculator,get_current_time,search_knowledge_base"
_LOCAL_BASE_URL_HINTS = ("localhost", "127.0.0.1", "0.0.0.0", "::1")

# 未配置 AGENT_JWT_SECRET 时使用的进程级随机密钥（模块加载时生成一次，重启即变）
_EPHEMERAL_JWT_SECRET = secrets.token_urlsafe(48)

# app/core/config.py -> app/core -> app -> 项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = PROJECT_ROOT
# 先读项目根的 .env（保证从任何目录启动都能读到），再读当前目录的 .env（若有则覆盖前者）
_ENV_FILES = (str(_PROJECT_ROOT / ".env"), ".env")


class Settings(BaseSettings):
    """运行期配置（不可变快照的构建入口见 ``get_settings``）。"""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILES,
        env_file_encoding="utf-8",
        env_prefix="AGENT_",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 应用基础 ----------
    app_name: str = "LangChain Agent API"
    env: Literal["dev", "test", "prod"] = "dev"
    version: str = "0.1.0"
    log_level: str = "INFO"
    api_prefix: str = "/api/v1"
    host: str = "127.0.0.1"
    port: int = 8000
    # 逗号分隔的来源列表，例如 "http://localhost:5173,https://example.com"；"*" 表示全部放行
    cors_origins: str = "*"

    # ---------- 模型 ----------
    provider: Provider = "openai"
    model: str = "gpt-4o-mini"
    api_key: SecretStr | None = None
    base_url: str | None = None
    temperature: float = Field(default=0.3, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=2048, gt=0)
    request_timeout: float = Field(default=60.0, gt=0)
    max_retries: int = Field(default=2, ge=0, le=10)

    # ---------- 智能体 ----------
    agent_name: str = "assistant"
    system_prompt: str = ""
    # 逗号分隔的工具名；留空表示注册全部内置工具
    enabled_tools: str = _DEFAULT_TOOLS
    max_tool_rounds: int = Field(default=8, ge=1, le=50)
    max_sessions: int = Field(default=1000, ge=1)

    # ---------- 联网检索 ----------
    # auto: 有 search_api_key 就走 tavily，否则用 bing / duckduckgo 抓取
    search_provider: SearchProvider = "auto"
    # Tavily 等服务商的 API Key（Bing / DuckDuckGo 抓取不需要）
    search_api_key: SecretStr | None = None
    web_timeout: float = Field(default=25.0, gt=0)
    # 抓取网页时最多回传给模型的字符数
    web_max_chars: int = Field(default=4000, ge=500, le=20000)

    # ---------- 数据库（PostgreSQL） ----------
    # 例：postgresql://postgres:root@127.0.0.1:5432/agent_chat
    # 留空则回退到进程内存仓储：功能（含登录）可用，但重启后账号与历史全部丢失。
    database_url: SecretStr | None = None
    db_pool_min: int = Field(default=1, ge=1, le=50)
    db_pool_max: int = Field(default=10, ge=1, le=100)
    db_connect_timeout: int = Field(default=5, ge=1, le=60)

    # ---------- 认证（登录态） ----------
    # 关闭后不再要求登录（历史仍按匿名用户隔离），仅用于本地调试，生产必须保持 True
    auth_required: bool = True
    # 是否开放自助注册；关闭后只能由管理员在库里建账号
    auth_allow_register: bool = True
    # 逗号分隔的用户名白名单：这些用户名注册时直接获得 admin 角色（用于开通首个管理员）
    auth_admin_usernames: str = ""
    # JWT 签名密钥；留空则每次启动随机生成（重启后所有 token 失效，仅供本地调试）
    jwt_secret: SecretStr | None = None
    # access token 有效期（分钟）与 refresh token 有效期（天）
    access_token_ttl_minutes: int = Field(default=60, ge=1, le=1440)
    refresh_token_ttl_days: int = Field(default=30, ge=1, le=365)
    # 密码长度上下限（字节数上限同时受哈希算法限制）
    password_min_length: int = Field(default=8, ge=6, le=64)
    password_max_length: int = Field(default=128, ge=16, le=512)

    # ---------- 终端工具 ----------
    # 命令默认超时（秒），单次调用可覆盖，上限 120 秒
    shell_timeout: float = Field(default=30.0, gt=0, le=120)
    # 回传给模型的最大输出字符数
    shell_max_output: int = Field(default=8000, ge=500, le=50000)
    # 逗号分隔的命令白名单；留空表示"黑名单模式"（只拦危险命令）
    # 例：shell_allowlist="git,python,dir,ls,cat" 后其它命令一律拒绝
    shell_allowlist: str = ""

    # ---------- 校验/解析 ----------
    @field_validator(
        "cors_origins", "enabled_tools", "system_prompt", "shell_allowlist", "auth_admin_usernames",
        mode="before",
    )
    @classmethod
    def _none_to_empty_str(cls, value: object) -> object:
        return "" if value is None else value

    # ---------- 派生属性 ----------
    @property
    def cors_origin_list(self) -> list[str]:
        value = self.cors_origins.strip()
        if not value or value == "*":
            return ["*"]
        return [item.strip().strip("'\"") for item in value.split(",") if item.strip()]

    @property
    def tool_names(self) -> list[str] | None:
        """返回启用的工具名列表；``None`` 表示不限制（注册全部）。"""
        value = self.enabled_tools.strip()
        if not value or value == "*":
            return None
        return [item.strip() for item in value.split(",") if item.strip()]

    @property
    def resolved_system_prompt(self) -> str:
        return self.system_prompt.strip() or DEFAULT_SYSTEM_PROMPT

    @property
    def api_key_value(self) -> str | None:
        secret = self.api_key
        if secret is None:
            return None
        value = secret.get_secret_value().strip()
        return value or None

    @property
    def base_url_value(self) -> str | None:
        value = (self.base_url or "").strip()
        return value or None

    # ---------- 数据库 / 认证派生属性 ----------
    @property
    def database_dsn(self) -> str | None:
        """PostgreSQL 连接串；未配置时返回 ``None``（回退内存仓储）。"""
        secret = self.database_url
        if secret is None:
            return None
        return secret.get_secret_value().strip() or None

    @property
    def database_dsn_masked(self) -> str | None:
        """隐去密码的连接串，仅用于日志与 /health 输出。"""
        dsn = self.database_dsn
        if dsn is None:
            return None
        return re.sub(r"://([^:/@]+):[^@]*@", r"://\1:***@", dsn)

    @property
    def jwt_secret_value(self) -> str:
        """JWT 签名密钥。

        未配置 ``AGENT_JWT_SECRET`` 时使用**进程级随机密钥**（模块加载时生成一次）：
        服务重启后旧 token 全部失效，这是有意的安全默认，同时避免用固定弱口令签名。
        """
        secret = self.jwt_secret
        if secret is not None:
            value = secret.get_secret_value().strip()
            if value:
                return value
        return _EPHEMERAL_JWT_SECRET

    @property
    def jwt_secret_is_ephemeral(self) -> bool:
        """是否在用临时密钥（启动时据此打印告警）。"""
        secret = self.jwt_secret
        return not (secret is not None and secret.get_secret_value().strip())

    @property
    def admin_usernames(self) -> set[str]:
        """注册时直接授予 admin 角色的用户名白名单（小写）。"""
        return {
            item.strip().lower()
            for item in self.auth_admin_usernames.split(",")
            if item.strip()
        }

    def auth_warnings(self) -> list[str]:
        """启动期需要提示给运维的认证配置风险。"""
        warnings: list[str] = []
        if self.jwt_secret_is_ephemeral:
            warnings.append(
                "未配置 AGENT_JWT_SECRET：正在使用进程级随机密钥，服务重启后所有登录态失效。"
                "生产环境请设置一个 32 字节以上的随机串。"
            )
        if not self.auth_required:
            warnings.append(
                "AGENT_AUTH_REQUIRED=false：对话与会话接口不再校验登录态，仅适合本地调试。"
            )
        if self.database_dsn is None:
            warnings.append(
                "未配置 AGENT_DATABASE_URL：账号与对话历史保存在进程内存中，重启即丢失。"
            )
        return warnings

    @property
    def search_api_key_value(self) -> str | None:
        secret = self.search_api_key
        if secret is None:
            return None
        value = secret.get_secret_value().strip()
        return value or None

    @property
    def effective_search_provider(self) -> str:
        """解析 ``auto``：有 Tavily key 就用 tavily，否则用 bing 抓取。"""
        if self.search_provider != "auto":
            return self.search_provider
        return "tavily" if self.search_api_key_value else "bing"

    @property
    def uses_local_endpoint(self) -> bool:
        base = self.base_url_value
        if base is None:
            return False
        return any(hint in base for hint in _LOCAL_BASE_URL_HINTS)

    def readiness_error(self) -> str | None:
        """检查模型配置是否可用，返回错误描述；可用则返回 ``None``。"""
        if self.provider == "mock":
            return None
        if self.provider in {"openai", "anthropic", "deepseek"} and not self.api_key_value:
            if not self.uses_local_endpoint:
                return (
                    f"provider={self.provider} 未配置 API Key，"
                    "请设置环境变量 AGENT_API_KEY（或把 AGENT_PROVIDER 设为 mock 以便离线联调）"
                )
        return None

    def safe_summary(self) -> dict[str, object]:
        """用于日志与 /agent/info 输出的脱敏配置摘要。"""
        return {
            "env": self.env,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url_value,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "agent_name": self.agent_name,
            "tools": self.tool_names,
            "api_key_configured": bool(self.api_key_value),
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """进程级单例配置，便于在依赖注入中复用。"""
    return Settings()


def reset_settings_cache() -> None:
    """清理缓存（测试中切换环境变量时使用）。"""
    get_settings.cache_clear()


def detect_env_file() -> str | None:
    """辅助函数：返回当前工作目录下实际生效的 .env 路径（无则 None）。"""
    for candidate in (".env", ".env.local"):
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None
