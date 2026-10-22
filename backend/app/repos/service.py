"""仓库接入与去重（SPEC 7.1）。

去重靠数据库唯一约束 UNIQUE(repository_id, commit_sha)：
INSERT ... ON CONFLICT DO NOTHING RETURNING id ——
返回了 id 说明"我"是第一个提交者，负责投递分析任务；没返回说明别人已经建过了，直接复用。
多人同时提交同一仓库也只会有一个 INSERT 成功，不需要分布式锁（见 D-002）。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.errors import NotFound, TooManyRequests
from app.core.queue import enqueue
from app.models import Repository, RepoSnapshot, User, UserRepo
from app.repos import git
from app.repos.progress import TERMINAL
from app.repos.url import normalize_repo_url


async def _running_jobs(db: AsyncSession, user_id: uuid.UUID) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(RepoSnapshot)
                .where(RepoSnapshot.requested_by == user_id, RepoSnapshot.status.not_in(list(TERMINAL)))
            )
        ).scalar_one()
    )


async def upsert_repository(
    db: AsyncSession, url: str, owner: str, name: str, default_branch: str | None
) -> Repository:
    await db.execute(
        pg_insert(Repository)
        .values(id=uuid.uuid4(), url=url, owner=owner, name=name, default_branch=default_branch)
        .on_conflict_do_nothing(index_elements=["url"])
    )
    repo = (await db.execute(select(Repository).where(Repository.url == url))).scalar_one()
    if default_branch and repo.default_branch != default_branch:
        repo.default_branch = default_branch
    return repo


async def get_or_create_snapshot(
    db: AsyncSession, repo: Repository, sha: str, ref: str | None, user: User
) -> tuple[RepoSnapshot, bool]:
    """返回 (快照, 是否新建)。"""
    new_id = uuid.uuid4()
    res = await db.execute(
        pg_insert(RepoSnapshot)
        .values(
            id=new_id, repository_id=repo.id, commit_sha=sha, ref=ref, status="queued", progress=0, requested_by=user.id
        )
        .on_conflict_do_nothing(constraint="uq_snapshot_repo_sha")
        .returning(RepoSnapshot.id)
    )
    created = res.scalar_one_or_none() is not None
    snap = (
        await db.execute(
            select(RepoSnapshot).where(RepoSnapshot.repository_id == repo.id, RepoSnapshot.commit_sha == sha)
        )
    ).scalar_one()
    return snap, created


async def link_user_repo(db: AsyncSession, user: User, repo: Repository, snap: RepoSnapshot, ref: str | None) -> None:
    stmt = pg_insert(UserRepo).values(user_id=user.id, repository_id=repo.id, latest_snapshot_id=snap.id, ref=ref)
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=["user_id", "repository_id"],
            set_={"latest_snapshot_id": snap.id, "ref": ref, "updated_at": datetime.now(UTC)},
        )
    )


async def submit_repo(db: AsyncSession, user: User, raw_url: str, ref: str | None) -> dict[str, Any]:
    s = get_settings()
    ru = normalize_repo_url(raw_url, allow_file=s.allow_file_repos)
    ref = (ref or "").strip() or None
    sha, default_branch = await git.resolve_ref(ru.url, ref)
    repo = await upsert_repository(db, ru.url, ru.owner, ru.name, default_branch)
    snap, created = await get_or_create_snapshot(db, repo, sha, ref or default_branch, user)
    if created and await _running_jobs(db, user.id) > s.max_concurrent_jobs_per_user:
        await db.rollback()
        raise TooManyRequests(f"同时最多运行 {s.max_concurrent_jobs_per_user} 个分析任务，请稍后再试")
    await link_user_repo(db, user, repo, snap, ref)
    await db.commit()
    if created:
        await enqueue("analyze", {"snapshot_id": snap.id}, user.id)
    return {
        "repository_id": repo.id,
        "snapshot_id": snap.id,
        "commit_sha": sha,
        "status": snap.status,
        "reused": not created,
    }


async def refresh_repo(db: AsyncSession, user: User, repository_id: uuid.UUID) -> dict[str, Any]:
    """检测新提交：重新 ls-remote，SHA 变了就建新快照（流水线会自动做增量分析）。
    快照失败过的话，这里也负责重试。"""
    link = await db.get(UserRepo, (user.id, repository_id))
    repo = await db.get(Repository, repository_id)
    if link is None or repo is None:
        raise NotFound("仓库不存在")
    sha, default_branch = await git.resolve_ref(repo.url, link.ref)
    snap, created = await get_or_create_snapshot(db, repo, sha, link.ref or default_branch, user)
    retry = not created and snap.status == "failed"
    if retry:
        snap.status, snap.progress, snap.error = "queued", 0, None
    await link_user_repo(db, user, repo, snap, link.ref)
    await db.commit()
    if created or retry:
        await enqueue("analyze", {"snapshot_id": snap.id}, user.id)
    return {
        "repository_id": repo.id,
        "snapshot_id": snap.id,
        "commit_sha": sha,
        "status": snap.status,
        "reused": not (created or retry),
        "new_commit": created,
    }
