"""GitHub OAuth 登录、会话、个人 API 令牌（SPEC 9.7）。

OAuth 流程（授权码模式）：
1. /auth/github/login 生成随机 state，存进短期 Cookie，重定向到 GitHub 授权页；
2. GitHub 回调 /auth/github/callback?code=&state=，先比对 state（防 CSRF 登录攻击）；
3. 用 code + client_secret 换 access_token（服务端到服务端，secret 不经过浏览器）；
4. 用 token 拿用户资料，按 github_id 新建或更新用户，签发 JWT 放进 HttpOnly Cookie。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx
from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import AppError, NotFound, Unauthorized
from app.core.security import COOKIE_NAME, create_jwt, new_api_token
from app.models import ApiToken, User

router = APIRouter(tags=["auth"])
STATE_COOKIE = "pb_oauth_state"


class MeOut(BaseModel):
    id: uuid.UUID
    login: str
    name: str | None
    avatar_url: str | None
    role: str
    model_config = {"from_attributes": True}


def _set_session_cookie(resp: Response, user_id: uuid.UUID) -> None:
    s = get_settings()
    resp.set_cookie(
        COOKIE_NAME,
        create_jwt(str(user_id)),
        httponly=True,
        samesite="lax",
        secure=s.is_prod,
        max_age=s.jwt_expire_hours * 3600,
        path="/",
    )


async def upsert_user(db: AsyncSession, github_id: int, login: str, name: str | None, avatar: str | None) -> User:
    s = get_settings()
    role = "admin" if login.lower() in {x.lower() for x in s.admin_github_logins} else "user"
    stmt = pg_insert(User).values(
        id=uuid.uuid4(), github_id=github_id, login=login, name=name, avatar_url=avatar, role=role
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["github_id"],
            set_={"login": login, "name": name, "avatar_url": avatar, "role": role},
        )
    )
    await db.commit()
    return (await db.execute(select(User).where(User.github_id == github_id))).scalar_one()


@router.get("/auth/config")
async def auth_config() -> dict[str, bool]:
    """登录页据此决定显示哪些登录方式（没配 OAuth 时不显示一个点了必报错的按钮）。"""
    s = get_settings()
    return {"github": bool(s.github_client_id), "dev_login": s.enable_dev_login and not s.is_prod}


@router.get("/auth/github/login")
async def github_login() -> RedirectResponse:
    s = get_settings()
    if not s.github_client_id:
        raise AppError(
            "还没有配置 GITHUB_CLIENT_ID，请先在 .env 里填写 GitHub OAuth App 信息", code="oauth_not_configured"
        )
    state = secrets.token_urlsafe(24)
    url = "https://github.com/login/oauth/authorize?" + urlencode(
        {
            "client_id": s.github_client_id,
            "redirect_uri": s.oauth_redirect_url,
            "scope": "read:user",
            "state": state,
        }
    )
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(STATE_COOKIE, state, httponly=True, samesite="lax", max_age=600, secure=s.is_prod, path="/")
    return resp


@router.get("/auth/github/callback")
async def github_callback(
    request: Request, code: str, state: str, db: AsyncSession = Depends(get_db)
) -> RedirectResponse:
    s = get_settings()
    if not state or request.cookies.get(STATE_COOKIE) != state:
        raise Unauthorized("OAuth state 校验失败，请重新登录")
    async with httpx.AsyncClient(timeout=15) as client:
        tok = await client.post(
            "https://github.com/login/oauth/access_token",
            data={
                "client_id": s.github_client_id,
                "client_secret": s.github_client_secret,
                "code": code,
                "redirect_uri": s.oauth_redirect_url,
            },
            headers={"Accept": "application/json"},
        )
        access = tok.json().get("access_token")
        if not access:
            raise Unauthorized(f"GitHub 授权失败: {tok.json().get('error_description', 'unknown')}")
        ures = await client.get(
            "https://api.github.com/user",
            headers={"Authorization": f"Bearer {access}", "Accept": "application/vnd.github+json"},
        )
        gh = ures.json()
    user = await upsert_user(db, int(gh["id"]), gh["login"], gh.get("name"), gh.get("avatar_url"))
    resp = RedirectResponse(s.frontend_url + "/", status_code=302)
    resp.delete_cookie(STATE_COOKIE, path="/")
    _set_session_cookie(resp, user.id)
    return resp


class DevLoginIn(BaseModel):
    login: str = Field(min_length=1, max_length=100)


@router.post("/auth/dev-login", response_model=MeOut)
async def dev_login(body: DevLoginIn, response: Response, db: AsyncSession = Depends(get_db)) -> Any:
    """仅在 ENABLE_DEV_LOGIN=true 且非生产环境时可用：没配 OAuth 也能本地调试、跑集成测试。"""
    s = get_settings()
    if not s.enable_dev_login or s.is_prod:
        raise NotFound("Not Found")
    # 用登录名派生一个稳定的负数 github_id，不会和真实 GitHub 用户冲突
    gid = -(int(hashlib.sha256(body.login.encode()).hexdigest()[:12], 16)) - 1
    existing = (await db.execute(select(User).where(User.login == body.login, User.github_id < 0))).scalar_one_or_none()
    user = await upsert_user(db, existing.github_id if existing else gid, body.login, body.login, None)
    _set_session_cookie(response, user.id)
    return user


@router.post("/auth/logout")
async def logout(response: Response) -> dict[str, bool]:
    response.delete_cookie(COOKIE_NAME, path="/")
    return {"ok": True}


@router.get("/me", response_model=MeOut)
async def me(user: User = Depends(get_current_user)) -> Any:
    return user


# ---------------------------------------------------------------- API 令牌
class TokenIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class TokenOut(BaseModel):
    id: uuid.UUID
    name: str
    last_used_at: datetime | None
    revoked: bool
    created_at: datetime
    model_config = {"from_attributes": True}


class TokenCreated(TokenOut):
    token: str = Field(description="明文令牌，只显示这一次")


@router.get("/tokens", response_model=list[TokenOut])
async def list_tokens(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    return (
        (await db.execute(select(ApiToken).where(ApiToken.user_id == user.id).order_by(ApiToken.created_at.desc())))
        .scalars()
        .all()
    )


@router.post("/tokens", response_model=TokenCreated)
async def create_token(
    body: TokenIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Any:
    raw, h = new_api_token()
    tok = ApiToken(user_id=user.id, name=body.name, token_hash=h)
    db.add(tok)
    await db.commit()
    await db.refresh(tok)
    return TokenCreated(
        id=tok.id, name=tok.name, last_used_at=None, revoked=False, created_at=tok.created_at, token=raw
    )


@router.delete("/tokens/{token_id}")
async def revoke_token(
    token_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    tok = await db.get(ApiToken, token_id)
    if tok is None or tok.user_id != user.id:
        raise NotFound("令牌不存在")
    tok.revoked = True
    await db.commit()
    return {"ok": True}
