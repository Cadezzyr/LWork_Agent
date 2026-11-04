"""登录/注册相关的请求与响应模型。"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, field_validator

# 与数据库 users_username_format 约束保持一致：字母/数字/下划线/点/短横线，3~32 位
USERNAME_PATTERN = r"^[A-Za-z0-9_.-]{3,32}$"
_USERNAME_RE = re.compile(USERNAME_PATTERN)
# 只做"像邮箱"的轻量校验，避免引入 email-validator 依赖
_EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[A-Za-z]{2,}$")


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class RegisterRequest(BaseModel):
    username: str = Field(description="登录名，3~32 位字母/数字/下划线/点/短横线", examples=["alice"])
    password: str = Field(description="密码，长度下限由 AGENT_PASSWORD_MIN_LENGTH 决定", examples=["s3cret-pass"])
    email: str | None = Field(default=None, description="邮箱（可选，可用于登录）")
    display_name: str | None = Field(default=None, description="显示名（可选）")

    @field_validator("username")
    @classmethod
    def _check_username(cls, value: str) -> str:
        value = value.strip()
        if not _USERNAME_RE.match(value):
            raise ValueError("用户名只能包含字母、数字、下划线、点和短横线，长度 3~32")
        return value

    @field_validator("email")
    @classmethod
    def _check_email(cls, value: str | None) -> str | None:
        value = _clean_optional(value)
        if value is None:
            return None
        if not _EMAIL_RE.match(value):
            raise ValueError("邮箱格式不正确")
        return value.lower()

    @field_validator("display_name")
    @classmethod
    def _check_display_name(cls, value: str | None) -> str | None:
        value = _clean_optional(value)
        if value is not None and len(value) > 64:
            raise ValueError("显示名最长 64 个字符")
        return value


class LoginRequest(BaseModel):
    identifier: str = Field(description="用户名或邮箱（大小写不敏感）", examples=["alice"])
    password: str = Field(description="密码")

    @field_validator("identifier")
    @classmethod
    def _check_identifier(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("用户名/邮箱不能为空")
        if len(value) > 320:
            raise ValueError("用户名/邮箱过长")
        return value


class RefreshRequest(BaseModel):
    refresh_token: str = Field(description="登录时返回的 refresh_token")

    @field_validator("refresh_token")
    @classmethod
    def _check_token(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("refresh_token 不能为空")
        return value


class LogoutRequest(BaseModel):
    refresh_token: str | None = Field(
        default=None,
        description="可选：同时吊销该 refresh token 对应的登录态；不传则按 access token 吊销",
    )


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(description="当前密码")
    new_password: str = Field(description="新密码")


class TokenPair(BaseModel):
    access_token: str = Field(description="访问令牌，放入 Authorization: Bearer <token>")
    refresh_token: str = Field(description="刷新令牌，用于换取新的 access_token")
    token_type: str = Field(default="bearer")
    expires_in: int = Field(description="access_token 有效期（秒）")
    refresh_expires_in: int = Field(description="refresh_token 有效期（秒）")


class UserOut(BaseModel):
    id: str
    username: str
    email: str | None = None
    display_name: str | None = None
    avatar_url: str | None = None
    role: str = "user"
    status: int = 1
    created_at: datetime | None = None
    last_login_at: datetime | None = None

    @classmethod
    def from_record(cls, record: Any) -> UserOut:
        return cls(
            id=record.id,
            username=record.username,
            email=record.email,
            display_name=record.display_name,
            avatar_url=record.avatar_url,
            role=record.role,
            status=record.status,
            created_at=record.created_at,
            last_login_at=record.last_login_at,
        )


class AuthResponse(BaseModel):
    """注册/登录/刷新的统一响应。"""

    user: UserOut
    tokens: TokenPair


__all__ = [
    "AuthResponse",
    "ChangePasswordRequest",
    "LoginRequest",
    "LogoutRequest",
    "RefreshRequest",
    "RegisterRequest",
    "TokenPair",
    "USERNAME_PATTERN",
    "UserOut",
]
