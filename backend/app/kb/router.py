"""知识库接口。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import AppError, NotFound
from app.core.queue import enqueue
from app.kb import service
from app.models import KbChunk, KbDocument, User

router = APIRouter(tags=["kb"])


class KbDocOut(BaseModel):
    id: uuid.UUID
    title: str
    filename: str | None
    chunk_count: int
    created_at: datetime
    model_config = {"from_attributes": True}


@router.post("/kb/documents", response_model=KbDocOut)
async def upload(
    file: UploadFile = File(...), title: str | None = Form(default=None), user: User = Depends(get_current_user)
) -> Any:
    name = file.filename or "note.md"
    if not name.lower().endswith((".md", ".markdown", ".txt")):
        raise AppError("只支持 Markdown 文件")
    data = await file.read()
    if len(data) > 2 * 1024 * 1024:
        raise AppError("文件不能超过 2 MB")
    text = data.decode("utf-8", errors="replace")
    doc = await service.add_document(user.id, title or name.rsplit(".", 1)[0], name, text)
    await enqueue("kb_ingest", {"document_id": doc.id}, user.id)
    return doc


@router.get("/kb/documents", response_model=list[KbDocOut])
async def list_docs(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    return (
        (
            await db.execute(
                select(KbDocument).where(KbDocument.user_id == user.id).order_by(KbDocument.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


@router.delete("/kb/documents/{doc_id}")
async def delete_doc(
    doc_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    doc = await db.get(KbDocument, doc_id)
    if doc is None or doc.user_id != user.id:
        raise NotFound("文档不存在")
    await db.execute(delete(KbChunk).where(KbChunk.document_id == doc_id))
    await db.delete(doc)
    await db.commit()
    return {"ok": True}


@router.get("/kb/search")
async def search(q: str, top_k: int = 5, user: User = Depends(get_current_user)) -> list[dict[str, Any]]:
    return await service.search(user.id, q, top_k=min(top_k, 20))
