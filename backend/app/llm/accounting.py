"""计费与配额。

- 每次调用写一行 llm_calls（运维页的费用统计、Trace 回放都从这里查）；
- 会话的 total_tokens / cost 原子累加（UPDATE ... SET x = x + n，不先读后写，避免并发丢失）；
- 每日 Token 配额用 Redis 计数：key 带日期，48 小时过期，天然按天清零。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sqlalchemy import update

from app.core.config import Settings
from app.core.db import session_scope
from app.core.errors import QuotaExceeded
from app.core.redis import get_redis
from app.llm.types import CallContext, Usage
from app.models import ExamSession, LlmCall

logger = logging.getLogger(__name__)


def quota_key(user_id: object) -> str:
    return f"quota:{user_id}:{datetime.now(UTC):%Y%m%d}"


def price(model: str, usage: Usage, s: Settings) -> float:
    p = s.llm_price_table.get(model)
    if not p:
        return 0.0
    return (usage.prompt_tokens * p.get("input", 0) + usage.completion_tokens * p.get("output", 0)) / 1_000_000


async def used_today(user_id: object) -> int:
    v = await get_redis().get(quota_key(user_id))
    return int(v or 0)


async def check_quota(ctx: CallContext, s: Settings) -> None:
    if ctx.user_id is None:
        return
    if await used_today(ctx.user_id) >= s.daily_token_quota:
        raise QuotaExceeded(f"今日 Token 配额（{s.daily_token_quota}）已用完，明天再来")


async def record(
    ctx: CallContext, model: str, usage: Usage, latency_ms: int, status: str, error: str | None, s: Settings
) -> None:
    cost = price(model, usage, s)
    try:
        async with session_scope() as db:
            db.add(
                LlmCall(
                    user_id=ctx.user_id,
                    session_id=ctx.session_id,
                    job_id=ctx.job_id,
                    node=ctx.node,
                    model=model,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    latency_ms=latency_ms,
                    cost=cost,
                    status=status,
                    error=error,
                )
            )
            if ctx.session_id is not None and usage.total:
                await db.execute(
                    update(ExamSession)
                    .where(ExamSession.id == ctx.session_id)
                    .values(total_tokens=ExamSession.total_tokens + usage.total, cost=ExamSession.cost + cost)
                )
            await db.commit()
        if ctx.user_id is not None and usage.total:
            r = get_redis()
            key = quota_key(ctx.user_id)
            await r.incrby(key, usage.total)
            await r.expire(key, 48 * 3600)
    except Exception:  # noqa: BLE001 —— 记账失败不能让业务调用失败，只记日志
        logger.exception("llm accounting failed")
