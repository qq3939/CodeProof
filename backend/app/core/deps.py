"""FastAPI 依赖：当前用户、管理员校验。

两种认证方式：
- 浏览器：HttpOnly Cookie 里的 JWT；
- MCP / 脚本：Authorization: Bearer <个人 API 令牌>（数据库只存哈希）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import Depends, Request
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import Forbidden, Unauthorized
from app.core.security import COOKIE_NAME, TOKEN_PREFIX, decode_jwt, hash_token
from app.models import ApiToken, User


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        raw = auth[7:].strip()
        if raw.startswith(TOKEN_PREFIX):
            tok = (
                await db.execute(
                    select(ApiToken).where(ApiToken.token_hash == hash_token(raw), ApiToken.revoked.is_(False))
                )
            ).scalar_one_or_none()
            if tok is None:
                raise Unauthorized("API 令牌无效或已撤销")
            await db.execute(update(ApiToken).where(ApiToken.id == tok.id).values(last_used_at=datetime.now(UTC)))
            await db.commit()
            user = await db.get(User, tok.user_id)
        else:
            uid = decode_jwt(raw)
            user = await db.get(User, uuid.UUID(uid)) if uid else None
    else:
        token = request.cookies.get(COOKIE_NAME)
        uid = decode_jwt(token) if token else None
        user = await db.get(User, uuid.UUID(uid)) if uid else None
    if user is None:
        raise Unauthorized("请先登录")
    request.state.user_id = str(user.id)
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    if user.role != "admin":
        raise Forbidden("仅管理员可访问")
    return user
