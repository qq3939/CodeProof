"""arq worker 入口：uv run arq app.worker.WorkerSettings

worker 是独立进程（SPEC 4）：克隆、建索引、出题、阅卷这些慢任务不占用 api 的事件循环。
"""

from __future__ import annotations

import uuid
from typing import Any

from arq import cron
from arq.connections import RedisSettings

from app.core.config import get_settings
from app.core.logging import setup_logging


async def run_job_task(ctx: dict[str, Any], job_id: str) -> None:
    from app.jobs import run_job

    await run_job(uuid.UUID(job_id))


async def cleanup_cron(ctx: dict[str, Any]) -> None:
    from app.repos.storage import cleanup_storage

    await cleanup_storage()


async def startup(ctx: dict[str, Any]) -> None:
    setup_logging(get_settings().log_level)


class WorkerSettings:
    functions = [run_job_task]
    cron_jobs = [cron(cleanup_cron, hour={3}, minute={0})]  # 每天凌晨 3 点按 LRU 清理旧快照
    on_startup = startup
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    max_jobs = 4
    job_timeout = 1800  # 大仓库首次分析 + 出题可能要十几分钟
    max_tries = 1  # 失败后由用户或运维页手动重试，避免对外部模型重复计费
