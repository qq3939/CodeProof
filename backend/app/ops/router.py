"""运维接口（仅管理员，SPEC 9.8）：任务队列、费用统计、Trace 回放、评测记录。"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Depends
from sqlalchemy import Date, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import require_admin
from app.core.errors import AppError, NotFound
from app.core.queue import dispatch
from app.models import AgentStep, EvalRun, ExamSession, Job, LlmCall, RepoSnapshot, User

router = APIRouter(prefix="/ops", tags=["ops"], dependencies=[Depends(require_admin)])


@router.get("/jobs")
async def jobs(status: str | None = None, limit: int = 100, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    counts = dict((await db.execute(select(Job.status, func.count()).group_by(Job.status))).all())
    q = (
        select(Job, User.login)
        .outerjoin(User, User.id == Job.user_id)
        .order_by(Job.created_at.desc())
        .limit(min(limit, 500))
    )
    if status:
        q = q.where(Job.status == status)
    rows = (await db.execute(q)).all()
    return {
        "counts": counts,
        "items": [
            {
                "id": str(j.id),
                "kind": j.kind,
                "args": j.args,
                "status": j.status,
                "attempts": j.attempts,
                "user": login,
                "error": (j.error or "")[:2000] or None,
                "created_at": j.created_at,
                "started_at": j.started_at,
                "finished_at": j.finished_at,
            }
            for j, login in rows
        ],
    }


@router.post("/jobs/{job_id}/retry")
async def retry(job_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict[str, bool]:
    job = await db.get(Job, job_id)
    if job is None:
        raise NotFound("任务不存在")
    if job.status not in ("failed",):
        raise AppError("只有失败的任务可以重试")
    job.status = "queued"
    if job.kind == "analyze":
        await db.execute(
            update(RepoSnapshot)
            .where(RepoSnapshot.id == uuid.UUID(job.args["snapshot_id"]))
            .values(status="queued", progress=0, error=None)
        )
    await db.commit()
    await dispatch(job_id)
    return {"ok": True}


@router.get("/costs")
async def costs(
    group_by: Literal["day", "user", "session"] = "day", days: int = 30, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    since = func.now() - func.make_interval(0, 0, 0, days)
    cols = [
        func.sum(LlmCall.prompt_tokens).label("prompt_tokens"),
        func.sum(LlmCall.completion_tokens).label("completion_tokens"),
        func.sum(LlmCall.cost).label("cost"),
        func.count().label("calls"),
        func.count().filter(LlmCall.status == "error").label("errors"),
    ]
    if group_by == "day":
        key = cast(LlmCall.created_at, Date).label("key")
        q = select(key, *cols).where(LlmCall.created_at >= since).group_by(key).order_by(key)
    elif group_by == "user":
        key = func.coalesce(User.login, "(系统)").label("key")
        q = (
            select(key, *cols)
            .outerjoin(User, User.id == LlmCall.user_id)
            .where(LlmCall.created_at >= since)
            .group_by(key)
            .order_by(func.sum(LlmCall.cost).desc())
        )
    else:
        key = LlmCall.session_id.label("key")
        q = (
            select(key, *cols)
            .where(LlmCall.created_at >= since, LlmCall.session_id.is_not(None))
            .group_by(key)
            .order_by(func.sum(LlmCall.cost).desc())
            .limit(200)
        )
    rows = (await db.execute(q)).all()
    by_model = (
        await db.execute(
            select(
                LlmCall.model,
                func.sum(LlmCall.prompt_tokens + LlmCall.completion_tokens),
                func.sum(LlmCall.cost),
                func.avg(LlmCall.latency_ms),
            )
            .where(LlmCall.created_at >= since)
            .group_by(LlmCall.model)
        )
    ).all()
    return {
        "group_by": group_by,
        "items": [
            {
                "key": str(r.key),
                "prompt_tokens": int(r.prompt_tokens or 0),
                "completion_tokens": int(r.completion_tokens or 0),
                "cost": round(float(r.cost or 0), 6),
                "calls": int(r.calls),
                "errors": int(r.errors or 0),
            }
            for r in rows
        ],
        "by_model": [
            {"model": m, "tokens": int(t or 0), "cost": round(float(c or 0), 6), "avg_latency_ms": int(lat or 0)}
            for m, t, c, lat in by_model
        ],
    }


@router.get("/sessions")
async def recent_sessions(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (
        await db.execute(
            select(ExamSession, User.login)
            .join(User, User.id == ExamSession.user_id)
            .order_by(ExamSession.started_at.desc())
            .limit(100)
        )
    ).all()
    return [
        {
            "id": str(s.id),
            "user": login,
            "mode": s.mode,
            "status": s.status,
            "total_tokens": s.total_tokens,
            "cost": s.cost,
            "started_at": s.started_at,
        }
        for s, login in rows
    ]


@router.get("/sessions/{session_id}/trace")
async def trace(session_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Trace 回放：把节点/工具调用（agent_steps）和模型调用（llm_calls）按时间合成一条时间线。"""
    steps = (
        (await db.execute(select(AgentStep).where(AgentStep.session_id == session_id).order_by(AgentStep.step)))
        .scalars()
        .all()
    )
    calls = (
        (await db.execute(select(LlmCall).where(LlmCall.session_id == session_id).order_by(LlmCall.created_at)))
        .scalars()
        .all()
    )
    timeline = [
        {
            "type": "tool",
            "at": s.created_at,
            "step": s.step,
            "node": s.node,
            "tool": s.tool_name,
            "args": s.tool_args,
            "result": s.result_preview,
            "latency_ms": s.latency_ms,
        }
        for s in steps
    ]
    timeline += [
        {
            "type": "llm",
            "at": c.created_at,
            "node": c.node,
            "model": c.model,
            "prompt_tokens": c.prompt_tokens,
            "completion_tokens": c.completion_tokens,
            "latency_ms": c.latency_ms,
            "cost": c.cost,
            "status": c.status,
            "error": c.error,
        }
        for c in calls
    ]
    timeline.sort(key=lambda x: x["at"])
    return {
        "session_id": str(session_id),
        "timeline": timeline,
        "totals": {
            "tokens": sum(c.prompt_tokens + c.completion_tokens for c in calls),
            "cost": round(sum(c.cost for c in calls), 6),
            "tool_calls": len(steps),
            "llm_calls": len(calls),
        },
    }


@router.get("/eval-runs")
async def eval_runs(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    rows = (await db.execute(select(EvalRun).order_by(EvalRun.created_at.desc()).limit(100))).scalars().all()
    return [
        {
            "id": str(r.id),
            "suite": r.suite,
            "status": r.status,
            "config": r.config,
            "metrics": r.metrics,
            "report_path": r.report_path,
            "created_at": r.created_at,
            "finished_at": r.finished_at,
        }
        for r in rows
    ]


@router.get("/eval-runs/{run_id}")
async def eval_run(run_id: uuid.UUID, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    r = await db.get(EvalRun, run_id)
    if r is None:
        raise NotFound("评测记录不存在")
    report = None
    if r.report_path and Path(r.report_path).exists():
        report = Path(r.report_path).read_text(encoding="utf-8")
    return {
        "id": str(r.id),
        "suite": r.suite,
        "status": r.status,
        "config": r.config,
        "metrics": r.metrics,
        "report_path": r.report_path,
        "report_md": report,
        "created_at": r.created_at,
    }
