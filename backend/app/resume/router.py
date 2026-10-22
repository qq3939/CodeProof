"""简历接口。"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, File, Form, UploadFile
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import AppError, NotFound
from app.core.queue import enqueue
from app.models import ClaimVerification, Resume, ResumeClaim, User
from app.resume import service

router = APIRouter(tags=["resume"])


class ResumeOut(BaseModel):
    id: uuid.UUID
    filename: str | None
    status: str
    mapping: dict[str, str]
    created_at: datetime
    model_config = {"from_attributes": True}


class ClaimOut(BaseModel):
    id: uuid.UUID
    project_name: str
    claim_text: str
    claim_type: str
    entities: list[str]
    snapshot_id: uuid.UUID | None
    model_config = {"from_attributes": True}


class MappingIn(BaseModel):
    mapping: dict[str, uuid.UUID | None]


async def _owned(db: AsyncSession, rid: uuid.UUID, user: User) -> Resume:
    r = await db.get(Resume, rid)
    if r is None or r.user_id != user.id:
        raise NotFound("简历不存在")
    return r


@router.post("/resumes", response_model=ResumeOut)
async def upload(
    file: UploadFile | None = File(default=None),
    text: str | None = Form(default=None),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Any:
    if file is not None:
        data = await file.read()
        if len(data) > 5 * 1024 * 1024:
            raise AppError("文件不能超过 5 MB")
        name = file.filename or "resume"
        content = service.pdf_text(data) if name.lower().endswith(".pdf") else data.decode("utf-8", errors="replace")
    elif text and text.strip():
        name, content = None, text
    else:
        raise AppError("请上传 PDF 或粘贴简历文本")
    rid = await service.create_resume(user.id, name, content)
    return await db.get(Resume, rid)


@router.get("/resumes", response_model=list[ResumeOut])
async def list_resumes(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    return (
        (await db.execute(select(Resume).where(Resume.user_id == user.id).order_by(Resume.created_at.desc())))
        .scalars()
        .all()
    )


@router.get("/resumes/{rid}")
async def detail(
    rid: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    r = await _owned(db, rid, user)
    claims = (await db.execute(select(ResumeClaim).where(ResumeClaim.resume_id == rid))).scalars().all()
    projects = sorted({c.project_name for c in claims})
    return {
        "resume": ResumeOut.model_validate(r).model_dump(),
        "projects": projects,
        "text": r.text,
        "claims": [ClaimOut.model_validate(c).model_dump() for c in claims],
    }


@router.put("/resumes/{rid}/mapping")
async def mapping(
    rid: uuid.UUID, body: MappingIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, bool]:
    r = await _owned(db, rid, user)
    await service.set_mapping(r, {k: str(v) if v else None for k, v in body.mapping.items()})
    return {"ok": True}


@router.post("/resumes/{rid}/verify")
async def verify(
    rid: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    r = await _owned(db, rid, user)
    if not r.mapping:
        raise AppError("请先把项目对应到已分析的仓库")
    job = await enqueue("resume_verify", {"resume_id": rid}, user.id)
    return {"job_id": str(job)}


@router.get("/resumes/{rid}/verification")
async def verification(
    rid: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    r = await _owned(db, rid, user)
    rows = (
        await db.execute(
            select(ResumeClaim, ClaimVerification)
            .outerjoin(ClaimVerification, ClaimVerification.claim_id == ResumeClaim.id)
            .where(ResumeClaim.resume_id == rid)
        )
    ).all()
    items = []
    counts: dict[str, int] = {}
    for c, v in rows:
        d = {
            "claim": ClaimOut.model_validate(c).model_dump(),
            "verdict": v.verdict if v else None,
            "evidence": v.evidence if v else [],
            "explanation": v.explanation if v else "",
            "suggestion": v.suggestion if v else "",
        }
        if v:
            counts[v.verdict] = counts.get(v.verdict, 0) + 1
        items.append(d)
    return {"status": r.status, "counts": counts, "items": items}
