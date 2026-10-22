"""任务队列（arq）封装。

每个后台任务都先在 Postgres 的 jobs 表里落一行，再投递到 arq：
- arq 的结果只在 Redis 里且会过期，运维页要看历史、要能重试失败任务，所以需要持久化的一份；
- job id 同时作为 arq 的 _job_id，arq 会拒绝重复 id，天然防止同一任务被投递两次。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from typing import Any

from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from app.core.config import get_settings
from app.core.db import session_scope
from app.models import Job

logger = logging.getLogger(__name__)
_pool: ArqRedis | None = None
_inline_tasks: set[asyncio.Task[Any]] = set()


async def get_pool() -> ArqRedis:
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def reset_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
    _pool = None


async def enqueue(kind: str, args: dict[str, Any], user_id: uuid.UUID | None = None) -> uuid.UUID:
    async with session_scope() as db:
        job = Job(
            kind=kind,
            args={k: str(v) if isinstance(v, uuid.UUID) else v for k, v in args.items()},
            user_id=user_id,
            status="queued",
        )
        db.add(job)
        await db.commit()
        job_id = job.id
    await dispatch(job_id)
    return job_id


async def dispatch(job_id: uuid.UUID) -> None:
    if get_settings().queue_mode == "inline":
        from app.jobs import run_job

        task = asyncio.create_task(run_job(job_id))
        _inline_tasks.add(task)
        task.add_done_callback(_inline_tasks.discard)
        return
    pool = await get_pool()
    await pool.enqueue_job("run_job_task", str(job_id), _job_id=f"{job_id}:{uuid.uuid4().hex[:6]}")


async def drain(timeout: float = 120) -> None:
    """测试用：等待 inline 模式下所有后台任务（包括任务里再投递的任务）跑完。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while _inline_tasks:
        if loop.time() > deadline:
            raise TimeoutError("后台任务没有在规定时间内完成")
        await asyncio.wait(list(_inline_tasks), timeout=max(0.1, deadline - loop.time()))
