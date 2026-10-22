"""掌握度接口：考点表格 + 单个考点的分数曲线。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import NotFound
from app.models import Mastery, Repository, User

router = APIRouter(tags=["mastery"])


def _row(m: Mastery, repo_name: str | None, now: datetime) -> dict[str, Any]:
    return {
        "repository_id": str(m.repository_id),
        "repo_name": repo_name,
        "topic_key": m.topic_key,
        "title": m.title,
        "score": round(m.score_ema, 1),
        "attempts": m.attempts,
        "last_seen_at": m.last_seen_at,
        "next_review_at": m.next_review_at,
        "due": bool(m.next_review_at and m.next_review_at <= now),
        "weak": m.score_ema < 60,
    }


@router.get("/mastery")
async def list_mastery(
    repository_id: uuid.UUID | None = None, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    q = (
        select(Mastery, Repository.name)
        .join(Repository, Repository.id == Mastery.repository_id)
        .where(Mastery.user_id == user.id)
    )
    if repository_id:
        q = q.where(Mastery.repository_id == repository_id)
    now = datetime.now(UTC)
    rows = (await db.execute(q.order_by(Mastery.next_review_at.asc().nulls_last()))).all()
    return [_row(m, name, now) for m, name in rows]


@router.get("/mastery/{topic_key:path}/history")
async def history(
    topic_key: str,
    repository_id: uuid.UUID = Query(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    m = await db.get(Mastery, (user.id, repository_id, topic_key))
    if m is None:
        raise NotFound("没有这个考点的记录")
    return {"topic_key": m.topic_key, "title": m.title, "history": m.history, "score": m.score_ema}
