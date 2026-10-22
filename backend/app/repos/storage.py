"""仓库存储：按需检出 + 按最近使用时间（LRU）清理（SPEC 7.2）。

清理只删磁盘上的克隆目录，数据库里的索引、画像、题库都保留；
之后有人再用这个快照时，ensure_checkout 会按 SHA 重新浅克隆回来。
"""

from __future__ import annotations

import logging
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update

from app.core.config import get_settings
from app.core.db import session_scope
from app.models import Repository, RepoSnapshot
from app.repos import git
from app.tools.sandbox import snapshot_root

logger = logging.getLogger(__name__)


async def touch(snapshot_id: uuid.UUID) -> None:
    async with session_scope() as db:
        await db.execute(
            update(RepoSnapshot).where(RepoSnapshot.id == snapshot_id).values(last_used_at=datetime.now(UTC))
        )
        await db.commit()


async def ensure_checkout(snapshot_id: uuid.UUID) -> Path:
    async with session_scope() as db:
        snap = await db.get(RepoSnapshot, snapshot_id)
        assert snap is not None
        repo = await db.get(Repository, snap.repository_id)
        assert repo is not None
    root = snapshot_root(repo.id, snap.commit_sha)
    if not root.exists():
        if get_settings().repo_storage_readonly:
            from app.core.errors import AppError
            from app.core.queue import enqueue

            await enqueue("checkout", {"snapshot_id": snapshot_id})
            raise AppError(
                "仓库文件被清理过，正在后台重新拉取，请一分钟后再试", code="checkout_pending", status_code=503
            )
        logger.info("re-checkout snapshot %s", snapshot_id)
        await git.shallow_fetch(repo.url, snap.commit_sha, root, timeout=get_settings().clone_timeout_s, ref=snap.ref)
    await touch(snapshot_id)
    return root


async def cleanup_storage() -> dict[str, int]:
    s = get_settings()
    base = Path(s.repo_storage_dir)
    if not base.exists():
        return {"deleted": 0}
    limit = int(s.storage_limit_gb * 1024**3)
    async with session_scope() as db:
        rows = (
            await db.execute(
                select(RepoSnapshot.id, RepoSnapshot.repository_id, RepoSnapshot.commit_sha, RepoSnapshot.last_used_at)
                .where(RepoSnapshot.status.in_(["ready", "failed"]))
                .order_by(RepoSnapshot.last_used_at.asc())
            )
        ).all()
    sizes = {}
    total = 0
    for r in rows:
        root = snapshot_root(r.repository_id, r.commit_sha)
        if root.exists():
            sz, _ = git.dir_size(root, set())
            sizes[r.id] = (root, sz)
            total += sz
    deleted = 0
    for r in rows:
        if total <= limit:
            break
        if r.id in sizes:
            root, sz = sizes[r.id]
            shutil.rmtree(root, ignore_errors=True)
            total -= sz
            deleted += 1
            logger.info("storage cleanup removed %s (%d bytes)", root, sz)
    return {"deleted": deleted, "total_bytes": total}
