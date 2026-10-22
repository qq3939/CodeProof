"""仓库、快照、任务进度接口（SPEC 11）。路由层只做参数校验和调用 service。"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db, session_scope
from app.core.deps import get_current_user
from app.core.errors import NotFound
from app.core.redis import get_redis
from app.models import RepoProfile, Repository, RepoSnapshot, Topic, User, UserRepo
from app.repos import service
from app.repos.progress import TERMINAL, channel
from app.repos.storage import ensure_checkout
from app.tools.sandbox import Sandbox, SandboxError, is_sensitive

router = APIRouter(tags=["repos"])


class SubmitRepoIn(BaseModel):
    url: str = Field(min_length=3, max_length=500)
    ref: str | None = Field(default=None, max_length=200, description="分支、标签或 40 位提交 SHA")


class SubmitRepoOut(BaseModel):
    repository_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str
    status: str
    reused: bool
    new_commit: bool | None = None


class SnapshotOut(BaseModel):
    id: uuid.UUID
    repository_id: uuid.UUID
    commit_sha: str
    ref: str | None
    status: str
    progress: int
    message: str | None
    error: str | None
    size_bytes: int | None
    file_count: int | None
    lang_stats: dict[str, Any] | None
    stats: dict[str, Any] | None
    base_snapshot_id: uuid.UUID | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RepoOut(BaseModel):
    id: uuid.UUID
    url: str
    owner: str
    name: str
    default_branch: str | None
    is_private: bool
    ref: str | None = None
    latest_snapshot: SnapshotOut | None = None


class TopicOutApi(BaseModel):
    id: uuid.UUID
    kind: str
    title: str
    description: str
    evidence: list[dict[str, Any]]
    importance: int
    topic_key: str

    model_config = {"from_attributes": True}


@router.post("/repos", response_model=SubmitRepoOut)
async def submit(body: SubmitRepoIn, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    return await service.submit_repo(db, user, body.url, body.ref)


@router.get("/repos", response_model=list[RepoOut])
async def my_repos(user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)) -> Any:
    rows = (
        await db.execute(
            select(Repository, UserRepo, RepoSnapshot)
            .join(UserRepo, UserRepo.repository_id == Repository.id)
            .outerjoin(RepoSnapshot, RepoSnapshot.id == UserRepo.latest_snapshot_id)
            .where(UserRepo.user_id == user.id)
            .order_by(UserRepo.updated_at.desc())
        )
    ).all()
    return [
        RepoOut(
            id=r.id,
            url=r.url,
            owner=r.owner,
            name=r.name,
            default_branch=r.default_branch,
            is_private=r.is_private,
            ref=ur.ref,
            latest_snapshot=SnapshotOut.model_validate(s) if s else None,
        )
        for r, ur, s in rows
    ]


@router.get("/repos/{repository_id}", response_model=RepoOut)
async def repo_detail(
    repository_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Any:
    link = await db.get(UserRepo, (user.id, repository_id))
    repo = await db.get(Repository, repository_id)
    if repo is None or link is None:
        raise NotFound("仓库不存在")
    snap = await db.get(RepoSnapshot, link.latest_snapshot_id) if link.latest_snapshot_id else None
    return RepoOut(
        id=repo.id,
        url=repo.url,
        owner=repo.owner,
        name=repo.name,
        default_branch=repo.default_branch,
        is_private=repo.is_private,
        ref=link.ref,
        latest_snapshot=SnapshotOut.model_validate(snap) if snap else None,
    )


@router.get("/repos/{repository_id}/snapshots", response_model=list[SnapshotOut])
async def repo_snapshots(
    repository_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Any:
    if await db.get(UserRepo, (user.id, repository_id)) is None:
        raise NotFound("仓库不存在")
    return (
        (
            await db.execute(
                select(RepoSnapshot)
                .where(RepoSnapshot.repository_id == repository_id)
                .order_by(RepoSnapshot.created_at.desc())
            )
        )
        .scalars()
        .all()
    )


@router.post("/repos/{repository_id}/refresh", response_model=SubmitRepoOut)
async def refresh(
    repository_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Any:
    return await service.refresh_repo(db, user, repository_id)


# ---------------------------------------------------------------- 快照
async def load_snapshot(db: AsyncSession, snapshot_id: uuid.UUID, user: User) -> RepoSnapshot:
    """快照是共享的；但私有仓库的快照只有关联了这个仓库的用户能看（M7）。"""
    snap = await db.get(RepoSnapshot, snapshot_id)
    if snap is None:
        raise NotFound("快照不存在")
    repo = await db.get(Repository, snap.repository_id)
    if repo is not None and repo.is_private and await db.get(UserRepo, (user.id, repo.id)) is None:
        raise NotFound("快照不存在")
    return snap


@router.get("/snapshots/{snapshot_id}", response_model=SnapshotOut)
async def snapshot(
    snapshot_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Any:
    return await load_snapshot(db, snapshot_id, user)


@router.get("/snapshots/{snapshot_id}/profile")
async def profile(
    snapshot_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    await load_snapshot(db, snapshot_id, user)
    prof = await db.get(RepoProfile, snapshot_id)
    if prof is None:
        raise NotFound("画像还没生成")
    return {"profile": prof.profile, "summary_md": prof.summary_md}


@router.get("/snapshots/{snapshot_id}/topics", response_model=list[TopicOutApi])
async def topics(
    snapshot_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> Any:
    await load_snapshot(db, snapshot_id, user)
    return (
        (
            await db.execute(
                select(Topic).where(Topic.snapshot_id == snapshot_id).order_by(Topic.importance.desc(), Topic.title)
            )
        )
        .scalars()
        .all()
    )


async def _sandbox(db: AsyncSession, snapshot_id: uuid.UUID, user: User) -> Sandbox:
    snap = await load_snapshot(db, snapshot_id, user)
    if snap.status not in ("indexing", "profiling", "generating", "ready"):
        raise NotFound("快照还没克隆完成")
    root = await ensure_checkout(snapshot_id)
    return Sandbox(root, snapshot_id)


@router.get("/snapshots/{snapshot_id}/tree")
async def tree(
    snapshot_id: uuid.UUID,
    path: str = Query(default="."),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sb = await _sandbox(db, snapshot_id, user)
    try:
        p = sb.resolve(path)
    except SandboxError as e:
        raise NotFound(e.message) from e
    if not p.is_dir():
        raise NotFound("目录不存在")
    from app.tools.sandbox import SKIP_DIRS

    items = []
    for c in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name.lower())):
        if c.name in SKIP_DIRS or c.is_symlink() or (c.is_file() and is_sensitive(c.name)):
            continue
        items.append(
            {
                "name": c.name,
                "path": sb.rel(c),
                "type": "dir" if c.is_dir() else "file",
                "size": c.stat().st_size if c.is_file() else None,
            }
        )
    return {"path": sb.rel(p) if p != sb.root else ".", "items": items}


@router.get("/snapshots/{snapshot_id}/file")
async def file(
    snapshot_id: uuid.UUID,
    path: str,
    start: int | None = None,
    end: int | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    sb = await _sandbox(db, snapshot_id, user)
    try:
        p = sb.resolve(path)
    except SandboxError as e:
        raise NotFound(e.message) from e
    if not p.is_file():
        raise NotFound("文件不存在")
    if p.stat().st_size > 1024 * 1024:
        raise NotFound("文件太大，不支持在线查看")
    text = p.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    return {
        "path": sb.rel(p),
        "content": text,
        "total_lines": len(lines),
        "highlight": [start, end or start] if start else None,
        "language": p.suffix.lstrip("."),
    }


# ---------------------------------------------------------------- 任务进度 SSE
def sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


@router.get("/jobs/{snapshot_id}/events")
async def job_events(
    snapshot_id: uuid.UUID, request: Request, user: User = Depends(get_current_user)
) -> StreamingResponse:
    """分析进度 SSE。先推一次当前状态（刷新页面也能看到），再转发 Redis 频道里的进度。

    EventSource 只能用 GET 且自动带同源 Cookie，所以这里靠 Cookie 鉴权（SPEC 9.7）。
    """
    async with session_scope() as db:
        snap = await load_snapshot(db, snapshot_id, user)
        current = {"status": snap.status, "progress": snap.progress, "message": snap.message, "error": snap.error}

    async def gen() -> AsyncIterator[str]:
        yield sse("progress", current)
        if current["status"] in TERMINAL:
            return
        pubsub = get_redis().pubsub()
        await pubsub.subscribe(channel(snapshot_id))
        try:
            idle = 0.0
            while not await request.is_disconnected():
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=1.0)
                if msg is None:
                    idle += 1
                    if idle >= 15:  # 心跳：防止 Nginx / 浏览器把空闲连接断掉
                        idle = 0
                        yield ": ping\n\n"
                    continue
                idle = 0
                data = json.loads(msg["data"])
                yield sse("progress", data)
                if data.get("status") in TERMINAL:
                    break
        finally:
            await pubsub.unsubscribe(channel(snapshot_id))
            await pubsub.aclose()

    return StreamingResponse(
        gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    )
