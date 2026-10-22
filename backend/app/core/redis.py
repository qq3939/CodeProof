"""Redis 客户端（单例）。用于 arq 队列、进度 Pub/Sub、限流和配额计数。"""

from __future__ import annotations

from redis.asyncio import Redis

from app.core.config import get_settings

_redis: Redis | None = None


def get_redis() -> Redis:
    global _redis
    if _redis is None:
        _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def reset_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None
