"""Trace 记录：每个 Agent 节点、每次工具调用都写一行 agent_steps（SPEC 8.1、9.8）。

运维页的 Trace 回放按 step 顺序把它们排成时间线。
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Any

from sqlalchemy import func, select

from app.core.db import session_scope
from app.core.text import truncate
from app.llm.types import CallContext
from app.models import AgentStep

logger = logging.getLogger(__name__)


async def record_step(
    ctx: CallContext | None,
    *,
    node: str,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
    result_preview: str | None = None,
    latency_ms: int = 0,
    tokens: int = 0,
) -> None:
    if ctx is None or (ctx.session_id is None and ctx.job_id is None):
        return
    try:
        async with session_scope() as db:
            q = select(func.coalesce(func.max(AgentStep.step), 0))
            q = (
                q.where(AgentStep.session_id == ctx.session_id)
                if ctx.session_id
                else q.where(AgentStep.job_id == ctx.job_id)
            )
            step = int((await db.execute(q)).scalar_one()) + 1
            db.add(
                AgentStep(
                    session_id=ctx.session_id,
                    job_id=ctx.job_id,
                    step=step,
                    node=node,
                    tool_name=tool_name,
                    tool_args=json.loads(json.dumps(tool_args or {}, default=str)) if tool_args is not None else None,
                    result_preview=truncate(result_preview, 600) if result_preview else None,
                    latency_ms=latency_ms,
                    tokens=tokens,
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 —— trace 失败不影响业务
        logger.exception("record_step failed")


def as_uuid(v: Any) -> uuid.UUID | None:
    if v is None or isinstance(v, uuid.UUID):
        return v
    return uuid.UUID(str(v))
