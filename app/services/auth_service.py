"""登录/注册/刷新/登出/鉴权业务逻辑。

安全设计要点（评审时重点看这里）
------------------------------------------------
1. **不泄露账号是否存在**：登录失败一律 ``invalid_credentials``，
   账号不存在时也会跑一次哈希校验（时间侧信道防护）。
2. **登录态可撤销**：access token 带 ``sid`` 指向 ``user_sessions`` 行，
   登出/改密时把该行 ``revoked_at`` 打标记，token 立刻失效——不再依赖"等它过期"。
3. **改密即踢下线**：``password_changed_at`` 参与校验（允许 1 秒时钟余量），
   改密后所有旧 access token 都会被拒绝。
4. **refresh token 轮换**：每次刷新都换新串并更新 ``token_hash``，
   旧串立即作废，降低泄露后的可用窗口。
5. **只存哈希**：库里只有 refresh token 的 SHA-256 与密码哈希（argon2/bcrypt/pbkdf2）。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from app.core.config import Settings
from app.core.exceptions import (
    AuthError,
    DatabaseUnavailableError,
    ForbiddenError,
    InvalidCredentialsError,
    ValidationError,
)
from app.core.logging import get_logger
from app.core.security import (
    TokenError,
    TokenExpiredError,
    create_access_token,
    decode_token,
    hash_password,
    hash_token,
    needs_rehash,
    new_refresh_token,
    verify_password,
)
from app.db.base import Repositories
from app.db.models import LoginSessionRecord, UserRecord, utcnow
from app.schemas.auth import AuthResponse, TokenPair, UserOut

logger = get_logger("services.auth")

# 账号不存在时用来"陪跑"校验的哈希（惰性计算一次，避免每次登录都白算）
_DUMMY_HASH: dict[str, str] = {}
# 「改密后旧 token 失效」判定允许的时钟误差（秒）：iat 是整数秒，签发瞬间可能略早于 now()
_TOKEN_CLOCK_SKEW = 1
# 未开启强制鉴权时使用的匿名账号（用户名不能与正常用户名冲突的概率足够低）
_ANONYMOUS_USERNAME = "anonymous_local"


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    """一次请求的当前用户（依赖注入的返回值）。"""

    user: UserRecord
    session_id: str | None = None

    @property
    def id(self) -> str:
        return self.user.id

    @property
    def is_anonymous(self) -> bool:
        return self.session_id is None

    def to_out(self) -> UserOut:
        return UserOut.from_record(self.user)


class AuthService:
    def __init__(self, repositories: Repositories, settings: Settings) -> None:
        self._repos = repositories
        self._settings = settings
        # 出于写放大考虑，last_used_at 不必每个请求都更新
        self._touch_cache: dict[str, float] = {}

    # ------------------------------------------------------------------ 注册/登录
    async def register(
        self,
        *,
        username: str,
        password: str,
        email: str | None = None,
        display_name: str | None = None,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> AuthResponse:
        if not self._settings.auth_allow_register:
            raise ForbiddenError("当前环境未开放自助注册，请联系管理员开通账号")

        self._validate_password(password, username=username)
        password_hash = hash_password(password)
        role = "admin" if username.lower() in self._settings.admin_usernames else "user"

        user = await self._repos.users.create(
            username=username,
            password_hash=password_hash,
            email=email,
            display_name=display_name or username,
            role=role,
        )
        logger.info("新用户注册成功 user=%s id=%s role=%s", user.username, user.id, role)
        return await self._start_session(user, user_agent=user_agent, ip=ip)

    async def login(
        self,
        *,
        identifier: str,
        password: str,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> AuthResponse:
        user = await self._repos.users.get_by_login(identifier)
        if user is None:
            # 陪跑一次哈希：让"账号不存在"和"密码错误"的耗时接近
            verify_password(password, self._dummy_hash())
            logger.warning("登录失败（账号不存在）identifier=%s ip=%s", _mask(identifier), ip)
            raise InvalidCredentialsError("用户名或密码错误")

        if not verify_password(password, user.password_hash):
            logger.warning("登录失败（密码错误）user=%s ip=%s", user.username, ip)
            raise InvalidCredentialsError("用户名或密码错误")

        if not user.is_active:
            logger.warning("登录失败（账号已禁用）user=%s ip=%s", user.username, ip)
            raise ForbiddenError("账号已被禁用，请联系管理员")

        # 顺带把弱算法哈希升级成当前最优算法（不改 password_changed_at）
        if needs_rehash(user.password_hash):
            try:
                await self._repos.users.update_password_hash(
                    user.id,
                    hash_password(password),
                    bump_password_changed_at=False,
                )
                logger.info("已升级用户 %s 的密码哈希算法", user.username)
            except DatabaseUnavailableError:  # pragma: no cover - 升级失败不影响登录
                logger.warning("升级密码哈希失败（忽略）user=%s", user.username)

        await self._repos.users.touch_login(user.id)
        return await self._start_session(user, user_agent=user_agent, ip=ip)

    async def refresh(self, refresh_token: str) -> AuthResponse:
        token_hash = hash_token(refresh_token)
        session = await self._repos.login_sessions.get_by_token_hash(token_hash)
        if session is None or not session.is_active():
            raise AuthError("登录态已失效，请重新登录", code="invalid_refresh_token")

        user = await self._repos.users.get_by_id(session.user_id)
        if user is None or not user.is_active:
            await self._repos.login_sessions.revoke(session.id)
            raise AuthError("账号不可用，请重新登录", code="invalid_refresh_token")

        # 轮换：旧 refresh token 立即作废
        new_token = new_refresh_token()
        expires_at = utcnow() + timedelta(days=self._settings.refresh_token_ttl_days)
        await self._repos.login_sessions.rotate_token(session.id, hash_token(new_token), expires_at)

        tokens = self._build_token_pair(user=user, session_id=session.id, refresh_token=new_token, expires_at=expires_at)
        return AuthResponse(user=UserOut.from_record(user), tokens=tokens)

    # -------------------------------------------------------------------- 登出
    async def logout(
        self,
        *,
        session_id: str | None = None,
        refresh_token: str | None = None,
    ) -> None:
        """吊销登录态。优先按 refresh token 定位，其次按 access token 的 sid。"""
        target: LoginSessionRecord | None = None
        if refresh_token:
            target = await self._repos.login_sessions.get_by_token_hash(hash_token(refresh_token))
        if target is None and session_id:
            target = await self._repos.login_sessions.get_by_id(session_id)
        if target is None:
            # 幂等：已经失效的登录态再登出也返回成功
            return
        await self._repos.login_sessions.revoke(target.id)
        logger.info("已登出 user_id=%s session=%s", target.user_id, target.id)

    async def logout_all(self, user_id: str) -> int:
        count = await self._repos.login_sessions.revoke_all(user_id)
        logger.info("已吊销用户 %s 的全部登录态（%s 个）", user_id, count)
        return count

    # ------------------------------------------------------------------ 鉴权
    async def authenticate(self, token: str) -> AuthenticatedUser:
        """校验 Bearer token 并加载用户；任何异常都翻译成 401/403。"""
        try:
            payload = decode_token(
                token,
                secret=self._settings.jwt_secret_value,
                expected_type="access",
            )
        except TokenExpiredError as exc:
            raise AuthError("登录态已过期，请刷新或重新登录", code="token_expired") from exc
        except TokenError as exc:
            raise AuthError("登录凭证无效，请重新登录", code="invalid_token") from exc

        session = await self._repos.login_sessions.get_by_id(payload.session_id)
        if session is None or not session.is_active():
            raise AuthError("登录态已失效，请重新登录", code="session_revoked")

        user = await self._repos.users.get_by_id(payload.subject)
        if user is None:
            raise AuthError("账号不存在或已注销", code="user_not_found")
        if not user.is_active:
            raise ForbiddenError("账号已被禁用，请联系管理员")

        # 改密后旧 token 立即失效（允许 1 秒时钟余量）
        if user.password_changed_at is not None:
            changed_at = user.password_changed_at.timestamp()
            if payload.issued_at + _TOKEN_CLOCK_SKEW < changed_at:
                raise AuthError("密码已变更，请重新登录", code="token_stale")

        await self._touch(session.id)
        return AuthenticatedUser(user=user, session_id=session.id)

    async def get_user(self, user_id: str) -> UserRecord:
        user = await self._repos.users.get_by_id(user_id)
        if user is None:
            raise AuthError("账号不存在或已注销", code="user_not_found")
        return user

    async def anonymous_user(self) -> AuthenticatedUser:
        """``AGENT_AUTH_REQUIRED=false`` 时使用的匿名主体。

        历史仍按这个固定账号隔离：既能"不登录直接玩"，又不会和真实用户串数据。
        """
        user = await self._repos.users.get_by_login(_ANONYMOUS_USERNAME)
        if user is None:
            # 不可登录的占位哈希：即使有人猜到账号名也无法用密码登入
            user = await self._repos.users.create(
                username=_ANONYMOUS_USERNAME,
                password_hash="!anonymous-login-disabled!",
                display_name="匿名用户",
            )
        if not user.is_active:
            raise ForbiddenError("匿名账号已被禁用")
        return AuthenticatedUser(user=user, session_id=None)

    # ------------------------------------------------------------------ 改密
    async def change_password(
        self,
        *,
        user_id: str,
        current_password: str,
        new_password: str,
        user_agent: str | None = None,
        ip: str | None = None,
    ) -> AuthResponse:
        user = await self.get_user(user_id)
        if not verify_password(current_password, user.password_hash):
            raise InvalidCredentialsError("当前密码不正确")
        if current_password == new_password:
            raise ValidationError("新密码不能与当前密码相同")

        self._validate_password(new_password, username=user.username)
        await self._repos.users.update_password_hash(
            user.id,
            hash_password(new_password),
            bump_password_changed_at=True,
        )
        # 改密后把所有旧登录态清掉（含当前这条），再发一套新的
        revoked = await self._repos.login_sessions.revoke_all(user.id)
        logger.info("用户 %s 修改密码成功，吊销 %s 个旧登录态", user.username, revoked)

        refreshed = await self.get_user(user.id)
        return await self._start_session(refreshed, user_agent=user_agent, ip=ip)

    # ---------------------------------------------------------------- 内部方法
    async def _start_session(
        self,
        user: UserRecord,
        *,
        user_agent: str | None,
        ip: str | None,
    ) -> AuthResponse:
        refresh_token = new_refresh_token()
        expires_at = utcnow() + timedelta(days=self._settings.refresh_token_ttl_days)
        session = await self._repos.login_sessions.create(
            user_id=user.id,
            token_hash=hash_token(refresh_token),
            expires_at=expires_at,
            user_agent=(user_agent or "")[:500] or None,
            ip=ip,
        )
        tokens = self._build_token_pair(
            user=user,
            session_id=session.id,
            refresh_token=refresh_token,
            expires_at=expires_at,
        )
        return AuthResponse(user=UserOut.from_record(user), tokens=tokens)

    def _build_token_pair(
        self,
        *,
        user: UserRecord,
        session_id: str,
        refresh_token: str,
        expires_at: Any,
    ) -> TokenPair:
        access_ttl = self._settings.access_token_ttl_minutes * 60
        access_token = create_access_token(
            user_id=user.id,
            session_id=session_id,
            secret=self._settings.jwt_secret_value,
            ttl_seconds=access_ttl,
            extra={"username": user.username, "role": user.role},
        )
        refresh_ttl = int((expires_at - utcnow()).total_seconds())
        return TokenPair(
            access_token=access_token,
            refresh_token=refresh_token,
            expires_in=access_ttl,
            refresh_expires_in=max(0, refresh_ttl),
        )

    async def _touch(self, session_id: str) -> None:
        """限频更新 ``last_used_at``（默认 5 分钟一次），避免每个请求都写库。"""
        now = time.monotonic()
        last = self._touch_cache.get(session_id, 0.0)
        if now - last < 300:
            return
        self._touch_cache[session_id] = now
        if len(self._touch_cache) > 10_000:  # 防止长期运行无限增长
            self._touch_cache.clear()
        try:
            await self._repos.login_sessions.touch(session_id)
        except Exception:  # noqa: BLE001 - 记录活跃时间失败不影响请求
            logger.debug("更新登录态活跃时间失败 session=%s", session_id, exc_info=True)

    def _validate_password(self, password: str, *, username: str | None = None) -> None:
        if not isinstance(password, str) or not password:
            raise ValidationError("密码不能为空")
        min_len = self._settings.password_min_length
        max_len = self._settings.password_max_length
        if len(password) < min_len:
            raise ValidationError(f"密码长度至少 {min_len} 位")
        if len(password) > max_len:
            raise ValidationError(f"密码长度最多 {max_len} 位")
        if password.strip() != password:
            raise ValidationError("密码首尾不能包含空白字符")
        if len(set(password)) == 1:
            raise ValidationError("密码过于简单，请更换")
        if username and password.lower() == username.lower():
            raise ValidationError("密码不能与用户名相同")
        if password.lower() in _COMMON_PASSWORDS:
            raise ValidationError("该密码过于常见，请更换")

    def _dummy_hash(self) -> str:
        if not _DUMMY_HASH:
            _DUMMY_HASH["value"] = hash_password("dummy-password-for-timing-only")
        return _DUMMY_HASH["value"]


# 极简弱口令黑名单（真正生产建议接入 HaveIBeenPwned 的 k-anonymity 接口）
_COMMON_PASSWORDS = frozenset(
    {
        "password",
        "password1",
        "password123",
        "12345678",
        "123456789",
        "1234567890",
        "qwerty123",
        "qwertyuiop",
        "admin123",
        "administrator",
        "iloveyou",
        "letmein123",
        "changeme123",
        "welcome123",
    }
)


def _mask(value: str) -> str:
    """日志里对账号做脱敏（保留前 2 位）。"""
    text = value.strip()
    if len(text) <= 2:
        return "**"
    return f"{text[:2]}***"


__all__ = ["AuthService", "AuthenticatedUser"]
