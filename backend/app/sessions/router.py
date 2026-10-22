"""考核会话接口（SPEC 8.7、11）。

作答和要提示都是 POST + 流式响应（text/event-stream），前端用 fetch-event-source 读取；
EventSource 只支持 GET，没法带作答内容，所以不用它（见 D-007）。
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import AppError
from app.models import ExamSession, Repository, RepoSnapshot, User
from app.sessions import service

router = APIRouter(tags=["sessions"])

Mode = Literal["deep_dive", "kb_link", "resume", "chain", "change"]


class SessionConfigIn(BaseModel):
    endpoint: str | None = Field(default=None, description="链路学习：接口，如 'POST /api/feed/{id}/like'")
    resume_id: uuid.UUID | None = Field(default=None, description="简历追问：简历 id")
    base: str | None = Field(default=None, description="改动理解：base 提交")
    head: str | None = Field(default=None, description="改动理解：head 提交")
    pr_url: str | None = Field(default=None, description="改动理解：公开 PR 地址")
    token_budget: int | None = Field(default=None, ge=1000)


class CreateSessionIn(BaseModel):
    snapshot_id: uuid.UUID
    mode: Mode = "deep_dive"
    difficulty: Literal["easy", "medium", "hard"] = "medium"
    question_count: int = Field(default=5, ge=1, le=20)
    max_depth: int = Field(default=2, ge=0, le=5)
    allow_hints: bool = True
    style: Literal["plain", "interviewer"] = "plain"
    config: SessionConfigIn = Field(default_factory=SessionConfigIn)


class TurnOut(BaseModel):
    seq: int
    role: str
    content: str
    question_id: uuid.UUID | None
    depth: int
    meta: dict[str, Any] | None
    created_at: datetime
    model_config = {"from_attributes": True}


class SessionOut(BaseModel):
    id: uuid.UUID
    snapshot_id: uuid.UUID
    mode: str
    difficulty: str
    question_count: int
    max_depth: int
    status: str
    total_tokens: int
    cost: float
    finish_reason: str | None
    started_at: datetime
    finished_at: datetime | None
    repo_name: str | None = None
    model_config = {"from_attributes": True}


class SessionDetail(BaseModel):
    session: SessionOut
    turns: list[TurnOut]
    state: dict[str, Any] | None
    events: list[dict[str, Any]] = Field(default_factory=list)


class AnswerIn(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


async def _detail(db: AsyncSession, sess: ExamSession, events: list[dict[str, Any]] | None = None) -> SessionDetail:
    state = await service.state_view(sess.id) if sess.status not in ("preparing", "failed") else None
    await db.refresh(sess)
    return SessionDetail(
        session=SessionOut.model_validate(sess),
        turns=[TurnOut.model_validate(t) for t in await service.turns(db, sess.id)],
        state=state,
        events=events or [],
    )


@router.post("/sessions", response_model=SessionDetail)
async def create(
    body: CreateSessionIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Any:
    payload = body.model_dump()
    payload["config"] = {
        k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in body.config.model_dump().items() if v is not None
    }
    if body.mode == "change" and not (body.config.pr_url or (body.config.base and body.config.head)):
        raise AppError("改动理解模式需要提供 base..head 提交范围或公开 PR 地址")
    if body.mode == "chain" and not body.config.endpoint:
        raise AppError("链路学习模式需要选择一个接口")
    if body.mode == "resume" and not body.config.resume_id:
        raise AppError("简历追问模式需要选择一份简历")
    sess = await service.create_session(db, user, payload)
    events: list[dict[str, Any]] = []
    if sess.status == "active":
        events = await service.start_graph(sess.id)
    return await _detail(db, sess, events)


@router.get("/sessions", response_model=list[SessionOut])
async def list_sessions(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    rows = (
        await db.execute(
            select(ExamSession, Repository.name)
            .join(RepoSnapshot, RepoSnapshot.id == ExamSession.snapshot_id)
            .join(Repository, Repository.id == RepoSnapshot.repository_id)
            .where(ExamSession.user_id == user.id)
            .order_by(ExamSession.started_at.desc())
            .limit(100)
        )
    ).all()
    out = []
    for s, name in rows:
        o = SessionOut.model_validate(s)
        o.repo_name = name
        out.append(o)
    return out


@router.get("/sessions/{session_id}", response_model=SessionDetail)
async def get(session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    sess = await service.load_owned(db, session_id, user)
    return await _detail(db, sess)


def _stream(session_id: uuid.UUID, value: dict[str, Any]) -> StreamingResponse:
    async def gen() -> AsyncIterator[str]:
        try:
            async for ev in service.resume_stream(session_id, value):
                yield service.sse_format(ev)
        except AppError as e:
            yield service.sse_format({"event": "error", "data": {"message": e.message, "code": e.code}})

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )


async def _active(db: AsyncSession, session_id: uuid.UUID, user: User) -> ExamSession:
    sess = await service.load_owned(db, session_id, user)
    if sess.status != "active":
        raise AppError("会话不在进行中", code="session_not_active")
    return sess


@router.post("/sessions/{session_id}/answer")
async def answer(
    session_id: uuid.UUID, body: AnswerIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> StreamingResponse:
    await _active(db, session_id, user)
    return _stream(session_id, {"action": "answer", "text": body.text})


@router.post("/sessions/{session_id}/hint")
async def hint(
    session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> StreamingResponse:
    await _active(db, session_id, user)
    return _stream(session_id, {"action": "hint"})


async def _collect(session_id: uuid.UUID, value: dict[str, Any]) -> list[dict[str, Any]]:
    return [ev async for ev in service.resume_stream(session_id, value)]


@router.post("/sessions/{session_id}/skip", response_model=SessionDetail)
async def skip(
    session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Any:
    sess = await _active(db, session_id, user)
    events = await _collect(session_id, {"action": "skip"})
    return await _detail(db, sess, events)


@router.post("/sessions/{session_id}/finish", response_model=SessionDetail)
async def finish(
    session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Any:
    sess = await _active(db, session_id, user)
    events = await _collect(session_id, {"action": "finish"})
    return await _detail(db, sess, events)
