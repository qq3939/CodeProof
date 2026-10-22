"""快照状态机与进度推送（SPEC 7.1）。

状态：queued → cloning → indexing → profiling → generating → ready；任一步失败 → failed（可重试）。
进度同时写数据库（刷新页面后能看到）和发布到 Redis 频道 job:{snapshotId}（SSE 实时推送）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import update

from app.core.db import session_scope
from app.core.redis import get_redis
from app.models import RepoSnapshot

STATES = ["queued", "cloning", "indexing", "profiling", "generating", "ready"]
TERMINAL = {"ready", "failed"}


def channel(snapshot_id: uuid.UUID | str) -> str:
    return f"job:{snapshot_id}"


async def publish(snapshot_id: uuid.UUID | str, data: dict[str, Any]) -> None:
    await get_redis().publish(channel(snapshot_id), json.dumps(data, ensure_ascii=False, default=str))


async def set_status(
    snapshot_id: uuid.UUID,
    status: str,
    progress: int,
    message: str | None = None,
    error: str | None = None,
    **fields: Any,
) -> None:
    async with session_scope() as db:
        await db.execute(
            update(RepoSnapshot)
            .where(RepoSnapshot.id == snapshot_id)
            .values(status=status, progress=progress, message=message, error=error, **fields)
        )
        await db.commit()
    await publish(snapshot_id, {"status": status, "progress": progress, "message": message, "error": error})
