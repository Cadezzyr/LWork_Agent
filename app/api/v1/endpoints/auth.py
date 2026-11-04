"""登录/注册接口。

路径一览（前缀 ``/api/v1``）：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/auth/register` | 注册并直接登录 |
| POST | `/auth/login` | 用户名/邮箱 + 密码登录 |
| POST | `/auth/refresh` | 用 refresh token 换新的 access token |
| POST | `/auth/logout` | 登出（吊销当前登录态） |
| POST | `/auth/logout-all` | 吊销该账号全部登录态 |
| POST | `/auth/password` | 修改密码（改完旧 token 全部失效） |
| GET | `/auth/me` | 当前登录用户信息 |
| GET | `/auth/config` | 前端需要的公开配置（是否开放注册等） |
"""

from __future__ import annotations

from fastapi import APIRouter, Request, status

from app.api.deps import AuthServiceDep, CurrentUserDep, SettingsDep
from app.schemas.auth import (
    AuthResponse,
    ChangePasswordRequest,
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    RegisterRequest,
    UserOut,
)

router = APIRouter(prefix="/auth", tags=["认证"])


def _client_meta(request: Request) -> tuple[str | None, str | None]:
    """取 User-Agent 与客户端 IP（反向代理场景下优先 X-Forwarded-For 首个值）。"""
    user_agent = request.headers.get("user-agent")
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        ip = forwarded.split(",")[0].strip()
    else:
        ip = request.client.host if request.client else None
    return user_agent, ip


@router.get("/config", summary="登录相关的公开配置")
async def auth_config(settings: SettingsDep) -> dict[str, object]:
    """给前端用的公开信息（不含任何密钥）。"""
    return {
        "auth_required": settings.auth_required,
        "allow_register": settings.auth_allow_register,
        "password_min_length": settings.password_min_length,
        "password_max_length": settings.password_max_length,
        "access_token_ttl": settings.access_token_ttl_minutes * 60,
        "refresh_token_ttl": settings.refresh_token_ttl_days * 86400,
    }


@router.post(
    "/register",
    response_model=AuthResponse,
    status_code=status.HTTP_201_CREATED,
    summary="注册（成功后直接返回登录态）",
    responses={409: {"description": "用户名或邮箱已被占用"}},
)
async def register(
    payload: RegisterRequest,
    request: Request,
    service: AuthServiceDep,
) -> AuthResponse:
    user_agent, ip = _client_meta(request)
    return await service.register(
        username=payload.username,
        password=payload.password,
        email=payload.email,
        display_name=payload.display_name,
        user_agent=user_agent,
        ip=ip,
    )


@router.post(
    "/login",
    response_model=AuthResponse,
    summary="登录",
    responses={401: {"description": "用户名或密码错误"}, 403: {"description": "账号被禁用"}},
)
async def login(payload: LoginRequest, request: Request, service: AuthServiceDep) -> AuthResponse:
    user_agent, ip = _client_meta(request)
    return await service.login(
        identifier=payload.identifier,
        password=payload.password,
        user_agent=user_agent,
        ip=ip,
    )


@router.post(
    "/refresh",
    response_model=AuthResponse,
    summary="刷新登录态（refresh token 会轮换）",
    responses={401: {"description": "refresh token 无效或已过期"}},
)
async def refresh(payload: RefreshRequest, service: AuthServiceDep) -> AuthResponse:
    return await service.refresh(payload.refresh_token)


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="登出（吊销当前登录态）",
)
async def logout(
    current: CurrentUserDep,
    service: AuthServiceDep,
    payload: LogoutRequest | None = None,
) -> None:
    await service.logout(
        session_id=current.session_id,
        refresh_token=payload.refresh_token if payload else None,
    )


@router.post("/logout-all", summary="吊销该账号的全部登录态")
async def logout_all(current: CurrentUserDep, service: AuthServiceDep) -> dict[str, int]:
    revoked = await service.logout_all(current.user.id)
    return {"revoked": revoked}


@router.post(
    "/password",
    response_model=AuthResponse,
    summary="修改密码（成功后旧 token 全部失效，返回新的登录态）",
    responses={401: {"description": "当前密码不正确"}},
)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    current: CurrentUserDep,
    service: AuthServiceDep,
) -> AuthResponse:
    user_agent, ip = _client_meta(request)
    return await service.change_password(
        user_id=current.user.id,
        current_password=payload.current_password,
        new_password=payload.new_password,
        user_agent=user_agent,
        ip=ip,
    )


@router.get("/me", response_model=UserOut, summary="当前登录用户")
async def me(current: CurrentUserDep) -> UserOut:
    return current.to_out()
