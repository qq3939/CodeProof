"""后台任务的统一执行入口：按 jobs.kind 分派，并维护 jobs 表的状态。"""

from __future__ import annotations

import logging
import traceback
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import update

from app.core.db import session_scope
from app.core.logging import job_id_var
from app.models import Job

logger = logging.getLogger(__name__)


async def _analyze(args: dict[str, Any], job_id: str) -> None:
    from app.analysis.pipeline import run_analysis

    await run_analysis(uuid.UUID(args["snapshot_id"]), job_id=job_id)


async def _grade(args: dict[str, Any], job_id: str) -> None:
    from app.agents.grader import grade_question

    await grade_question(uuid.UUID(args["session_id"]), uuid.UUID(args["question_id"]), job_id=job_id)


async def _report(args: dict[str, Any], job_id: str) -> None:
    from app.reports.service import build_report

    await build_report(uuid.UUID(args["session_id"]))


async def _resume_verify(args: dict[str, Any], job_id: str) -> None:
    from app.resume.service import verify_resume

    await verify_resume(uuid.UUID(args["resume_id"]), job_id=job_id)


async def _kb_ingest(args: dict[str, Any], job_id: str) -> None:
    from app.kb.service import embed_document

    await embed_document(uuid.UUID(args["document_id"]))


async def _change_prepare(args: dict[str, Any], job_id: str) -> None:
    from app.agents.change import prepare_change

    await prepare_change(uuid.UUID(args["session_id"]), job_id=job_id)


async def _checkout(args: dict[str, Any], job_id: str) -> None:
    from app.repos.storage import ensure_checkout

    await ensure_checkout(uuid.UUID(args["snapshot_id"]))


async def _cleanup(args: dict[str, Any], job_id: str) -> None:
    from app.repos.storage import cleanup_storage

    await cleanup_storage()


HANDLERS: dict[str, Callable[[dict[str, Any], str], Awaitable[None]]] = {
    "analyze": _analyze,
    "grade": _grade,
    "report": _report,
    "resume_verify": _resume_verify,
    "kb_ingest": _kb_ingest,
    "cleanup": _cleanup,
    "change_prepare": _change_prepare,
    "checkout": _checkout,
}


async def run_job(job_id: uuid.UUID) -> None:
    job_id_var.set(str(job_id))
    async with session_scope() as db:
        job = await db.get(Job, job_id)
        if job is None or job.status == "done":
            return
        kind, args = job.kind, dict(job.args)
        await db.execute(
            update(Job)
            .where(Job.id == job_id)
            .values(status="running", attempts=Job.attempts + 1, started_at=datetime.now(UTC), error=None)
        )
        await db.commit()
    try:
        await HANDLERS[kind](args, str(job_id))
    except Exception as e:  # noqa: BLE001
        logger.exception("job failed")
        async with session_scope() as db:
            await db.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    status="failed",
                    error=f"{type(e).__name__}: {e}\n{traceback.format_exc()[-1500:]}",
                    finished_at=datetime.now(UTC),
                )
            )
            await db.commit()
        return
    async with session_scope() as db:
        await db.execute(update(Job).where(Job.id == job_id).values(status="done", finished_at=datetime.now(UTC)))
        await db.commit()
