"""数据库连接。

全链路异步：SQLAlchemy 2 的 AsyncEngine + asyncpg 驱动。
为什么每个请求一个 AsyncSession：Session 不是线程/协程安全的，按请求隔离最简单。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import get_settings


class Base(DeclarativeBase):
    pass


_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def get_engine() -> AsyncEngine:
    global _engine, _sessionmaker
    if _engine is None:
        # pool_pre_ping：Postgres 重启后旧连接会失效，取连接前先 ping 一下
        _engine = create_async_engine(get_settings().database_url, pool_pre_ping=True, pool_size=10)
        _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    get_engine()
    assert _sessionmaker is not None
    return _sessionmaker


async def reset_engine() -> None:
    """测试里切换数据库地址后调用。"""
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI 依赖：一个请求一个会话，结束时自动关闭。"""
    async with get_sessionmaker()() as session:
        yield session


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """给 worker、Agent 这类不在请求里的代码用。"""
    async with get_sessionmaker()() as session:
        yield session
