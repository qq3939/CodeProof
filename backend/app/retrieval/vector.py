"""代码向量检索（对照组，SPEC 7.8 / 8.2 vector 模式）。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select

from app.core.db import session_scope
from app.llm.client import get_llm
from app.models import CodeChunk


@dataclass
class VectorHit:
    path: str
    start_line: int
    end_line: int
    score: float


async def vector_search(snapshot_id: uuid.UUID, query: str, top_k: int = 8) -> list[VectorHit]:
    [qv] = await get_llm().embed([query])
    async with session_scope() as db:
        dist = CodeChunk.embedding.cosine_distance(qv)
        rows = (
            await db.execute(
                select(CodeChunk.path, CodeChunk.start_line, CodeChunk.end_line, dist.label("d"))
                .where(CodeChunk.snapshot_id == snapshot_id, CodeChunk.embedding.is_not(None))
                .order_by(dist)
                .limit(top_k)
            )
        ).all()
    return [VectorHit(path=r.path, start_line=r.start_line, end_line=r.end_line, score=1 - float(r.d)) for r in rows]
