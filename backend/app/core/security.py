"""JWT、API 令牌哈希、Origin 校验。"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import get_settings

COOKIE_NAME = "pb_session"
TOKEN_PREFIX = "pbt_"


def create_jwt(user_id: str) -> str:
    s = get_settings()
    now = datetime.now(UTC)
    payload = {"sub": user_id, "iat": now, "exp": now + timedelta(hours=s.jwt_expire_hours)}
    return jwt.encode(payload, s.jwt_secret, algorithm="HS256")


def decode_jwt(token: str) -> str | None:
    try:
        payload = jwt.decode(token, get_settings().jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None
    sub = payload.get("sub")
    return str(sub) if sub else None


def new_api_token() -> tuple[str, str]:
    """返回 (明文, 哈希)。明文只在创建时展示一次，数据库只存哈希。

    为什么用 SHA-256 而不是 bcrypt：令牌本身是 32 字节随机数，熵足够高，
    不存在字典攻击问题；而每次 API 调用都要算一次哈希，快速哈希更合适。
    """
    raw = TOKEN_PREFIX + secrets.token_urlsafe(32)
    return raw, hash_token(raw)


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def origin_allowed(origin: str | None, host: str | None) -> bool:
    """CSRF 防护：修改数据的请求必须来自允许的 Origin。

    没有 Origin 头的请求（curl、MCP 客户端）不是浏览器发起的，
    浏览器跨站请求一定会带 Origin，所以放行无 Origin 的请求是安全的。
    """
    if not origin:
        return True
    s = get_settings()
    if origin in s.allowed_origins:
        return True
    # 同源：Origin 的 host 和请求 Host 一致
    return bool(host and origin.split("://", 1)[-1] == host)
