"""密码哈希与 JWT 签发/校验。

设计取舍
--------
1. **零新增依赖**：本项目的运行环境装不了新包，因此这里只依赖标准库。
   密码哈希按可用性依次尝试 ``argon2-cffi`` -> ``bcrypt`` -> 标准库 PBKDF2-SHA256，
   三者都可用时优先 argon2id（OWASP 推荐），都没有时 PBKDF2-SHA256 也是安全的
   （NIST SP 800-63B 允许，只是抗 GPU 暴力破解能力弱于 argon2/bcrypt）。
2. **哈希串自带算法前缀**（``argon2id$...`` / ``bcrypt$...`` / ``pbkdf2_sha256$...``），
   因此将来换算法或调整迭代次数时，旧哈希仍可校验；登录成功发现算法过时可用
   ``needs_rehash()`` 触发升级。
3. **JWT 只支持 HS256**：算法由服务端常量固定，绝不读取 token header 里的 ``alg``
   去选择校验方式（那是 ``alg=none`` 攻击的根源）；签名比较使用
   ``hmac.compare_digest``（常数时间）。
4. access token 里带 ``sid``（``user_sessions.id``），所以 JWT 虽然是"无状态"的，
   仍然可以靠数据库把登录态**主动撤销**（登出、改密、强制下线）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger

logger = get_logger("core.security")

# --------------------------------------------------------------------------- 密码

#: PBKDF2 迭代次数（OWASP 2023 建议 SHA256 >= 600k；取 600k 需要 ~0.2s/次）
PBKDF2_ITERATIONS = 600_000
#: bcrypt 输入上限：算法本身只取前 72 字节，超长部分会被静默丢弃
_BCRYPT_MAX_BYTES = 72


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _detect_backend() -> str:
    """返回可用的最强哈希后端：``argon2`` / ``bcrypt`` / ``pbkdf2``。"""
    try:  # pragma: no cover - 取决于运行环境
        import argon2  # noqa: F401

        return "argon2"
    except ImportError:
        pass
    try:  # pragma: no cover - 取决于运行环境
        import bcrypt  # noqa: F401

        return "bcrypt"
    except ImportError:
        pass
    return "pbkdf2"


_HASH_BACKEND = _detect_backend()


def hash_password(password: str) -> str:
    """把明文密码转成带算法前缀的哈希串。"""
    if not password:
        raise ValueError("密码不能为空")
    if _HASH_BACKEND == "argon2":
        return _hash_argon2(password)
    if _HASH_BACKEND == "bcrypt":
        return _hash_bcrypt(password)
    return _hash_pbkdf2(password)


def verify_password(password: str, stored_hash: str) -> bool:
    """校验密码；任何异常都按"校验失败"处理，不向调用方泄露细节。"""
    if not password or not stored_hash:
        return False
    try:
        if stored_hash.startswith("argon2"):
            return _verify_argon2(password, stored_hash)
        if stored_hash.startswith("bcrypt$"):
            return _verify_bcrypt(password, stored_hash[len("bcrypt$") :])
        if stored_hash.startswith("$2"):  # 外部写入的裸 bcrypt 哈希
            return _verify_bcrypt(password, stored_hash)
        if stored_hash.startswith("pbkdf2_sha256$"):
            return _verify_pbkdf2(password, stored_hash)
    except Exception:  # noqa: BLE001 - 损坏的哈希串不应导致 500
        logger.warning("密码校验异常（哈希串可能损坏），按校验失败处理")
        return False
    logger.warning("未知的密码哈希格式，按校验失败处理")
    return False


def needs_rehash(stored_hash: str) -> bool:
    """当前哈希是否应该用更强的算法/参数重算（登录成功时顺手升级）。"""
    if _HASH_BACKEND == "argon2":
        return not stored_hash.startswith("argon2")
    if _HASH_BACKEND == "bcrypt":
        return not (stored_hash.startswith("bcrypt$") or stored_hash.startswith("$2"))
    return not stored_hash.startswith("pbkdf2_sha256$")


def _hash_argon2(password: str) -> str:
    from argon2 import PasswordHasher

    hasher = PasswordHasher()
    return "argon2$" + hasher.hash(password)


def _verify_argon2(password: str, stored_hash: str) -> bool:
    from argon2 import PasswordHasher
    from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

    encoded = stored_hash[len("argon2$") :] if stored_hash.startswith("argon2$") else stored_hash
    try:
        return PasswordHasher().verify(encoded, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


def _hash_bcrypt(password: str) -> str:
    import bcrypt

    # bcrypt 超长输入会被截断：先做一次 SHA-256 再 base64，既避免截断也保持固定长度
    payload = _bcrypt_payload(password)
    return "bcrypt$" + bcrypt.hashpw(payload, bcrypt.gensalt()).decode("ascii")


def _verify_bcrypt(password: str, stored: str) -> bool:
    import bcrypt

    return bcrypt.checkpw(_bcrypt_payload(password), stored.encode("ascii"))


def _bcrypt_payload(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) <= _BCRYPT_MAX_BYTES:
        return raw
    return base64.b64encode(hashlib.sha256(raw).digest())


def _hash_pbkdf2(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${_b64encode(salt)}${_b64encode(digest)}"


def _verify_pbkdf2(password: str, stored: str) -> bool:
    try:
        _, iterations, salt_text, digest_text = stored.split("$")
        expected = _b64decode(digest_text)
        actual = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            _b64decode(salt_text),
            int(iterations),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


# ----------------------------------------------------------------------------- JWT

JWT_ALGORITHM = "HS256"
_JWT_HEADER = {"alg": JWT_ALGORITHM, "typ": "JWT"}


class TokenError(Exception):
    """token 无法通过校验时的异常基类（由服务层翻译成 401）。"""


class TokenExpiredError(TokenError):
    pass


class TokenInvalidError(TokenError):
    pass


@dataclass(frozen=True, slots=True)
class TokenPayload:
    """校验通过后的 claims 视图。"""

    subject: str
    session_id: str
    token_type: str
    issued_at: int
    expires_at: int
    raw: dict[str, Any]


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def create_access_token(
    *,
    user_id: str,
    session_id: str,
    secret: str,
    ttl_seconds: int,
    extra: dict[str, Any] | None = None,
) -> str:
    """签发 access token（HS256）。"""
    now = int(time.time())
    payload: dict[str, Any] = {
        "sub": user_id,
        "sid": session_id,
        "typ": "access",
        "iat": now,
        "exp": now + int(ttl_seconds),
        "jti": secrets.token_urlsafe(12),
    }
    if extra:
        payload.update(extra)
    return _encode(payload, secret)


def decode_token(token: str, *, secret: str, expected_type: str = "access") -> TokenPayload:
    """校验并解析 token；失败抛 ``TokenError`` 子类。"""
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenInvalidError("token 格式不正确")
    header_b64, payload_b64, signature_b64 = parts

    try:
        header = json.loads(_b64decode(header_b64))
    except (ValueError, TypeError) as exc:
        raise TokenInvalidError("token 头部不是合法 JSON") from exc

    # 关键：算法只认服务端的 HS256，忽略（并拒绝）header 里声明的其它算法
    if not isinstance(header, dict) or header.get("alg") != JWT_ALGORITHM:
        raise TokenInvalidError("不支持的签名算法")
    if header.get("typ") not in (None, "JWT"):
        raise TokenInvalidError("token 类型不正确")

    signing_input = f"{header_b64}.{payload_b64}".encode("ascii")
    try:
        expected_signature = _sign(signing_input, secret)
        provided_signature = _b64decode(signature_b64)
    except (ValueError, TypeError) as exc:
        raise TokenInvalidError("签名编码不合法") from exc
    if not hmac.compare_digest(expected_signature, provided_signature):
        raise TokenInvalidError("签名校验失败")

    try:
        payload = json.loads(_b64decode(payload_b64))
    except (ValueError, TypeError) as exc:
        raise TokenInvalidError("token 载荷不是合法 JSON") from exc
    if not isinstance(payload, dict):
        raise TokenInvalidError("token 载荷格式不正确")

    token_type = payload.get("typ")
    if expected_type and token_type != expected_type:
        raise TokenInvalidError(f"需要 {expected_type} token")

    subject = payload.get("sub")
    session_id = payload.get("sid")
    if not isinstance(subject, str) or not subject:
        raise TokenInvalidError("token 缺少 sub")
    if not isinstance(session_id, str) or not session_id:
        raise TokenInvalidError("token 缺少 sid")

    now = int(time.time())
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)):
        raise TokenInvalidError("token 缺少过期时间")
    if now >= int(exp):
        raise TokenExpiredError("登录态已过期")
    nbf = payload.get("nbf")
    if isinstance(nbf, (int, float)) and now < int(nbf):
        raise TokenInvalidError("token 尚未生效")
    iat = payload.get("iat")
    issued_at = int(iat) if isinstance(iat, (int, float)) else now

    return TokenPayload(
        subject=subject,
        session_id=session_id,
        token_type=str(token_type),
        issued_at=issued_at,
        expires_at=int(exp),
        raw=payload,
    )


def _encode(payload: dict[str, Any], secret: str) -> str:
    header_b64 = _b64encode(json.dumps(_JWT_HEADER, separators=(",", ":"), sort_keys=True).encode())
    payload_b64 = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    signature = _sign(f"{header_b64}.{payload_b64}".encode("ascii"), secret)
    return f"{header_b64}.{payload_b64}.{_b64encode(signature)}"


# ------------------------------------------------------------------ 其它小工具
def new_refresh_token() -> str:
    """生成不透明 refresh token（32 字节熵，仅其 SHA-256 入库）。"""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """refresh token 的入库形态：SHA-256 十六进制（匹配 ``token_hash CHAR(64)``）。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


__all__ = [
    "TokenError",
    "TokenExpiredError",
    "TokenInvalidError",
    "TokenPayload",
    "create_access_token",
    "decode_token",
    "hash_password",
    "hash_token",
    "needs_rehash",
    "new_refresh_token",
    "verify_password",
]
