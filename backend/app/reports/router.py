"""报告接口：JSON（报告页）和 Markdown（导出）。"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.deps import get_current_user
from app.core.errors import NotFound
from app.models import Grading, Question, Report, User
from app.reports.service import _asked_questions
from app.sessions.service import load_owned

router = APIRouter(tags=["reports"])


@router.get("/sessions/{session_id}/report")
async def report(
    session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    sess = await load_owned(db, session_id, user)
    rep = await db.get(Report, session_id)
    if rep is None:
        return {"status": sess.status, "ready": False}
    qids = await _asked_questions(session_id)
    gradings = {
        g.question_id: g for g in (await db.execute(select(Grading).where(Grading.session_id == session_id))).scalars()
    }
    questions = {q.id: q for q in (await db.execute(select(Question).where(Question.id.in_(qids)))).scalars()}
    items = []
    for i, qid in enumerate(qids, 1):
        q, g = questions.get(qid), gradings.get(qid)
        if not q or not g:
            continue
        items.append(
            {
                "index": i,
                "question_id": str(q.id),
                "text": q.text,
                "topic_key": q.topic_key,
                "key_points": q.key_points,
                "reference_md": q.reference_md,
                "score": g.score,
                "raw_score": g.raw_score,
                "hint_level": g.hint_level,
                "skipped": g.skipped,
                "point_results": g.point_results,
                "contradictions": g.contradictions,
                "feedback_md": g.feedback_md,
                "review_topics": g.review_topics,
                "judge_runs": g.judge_runs,
                "agreement": g.agreement,
            }
        )
    return {
        "ready": True,
        "status": sess.status,
        "snapshot_id": str(sess.snapshot_id),
        "mode": sess.mode,
        "scores": rep.scores,
        "weak_topics": rep.weak_topics,
        "items": items,
        "summary_md": rep.summary_md,
    }


@router.get("/sessions/{session_id}/report.md", response_class=PlainTextResponse)
async def report_md(
    session_id: uuid.UUID, user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> PlainTextResponse:
    await load_owned(db, session_id, user)
    rep = await db.get(Report, session_id)
    if rep is None:
        raise NotFound("报告还没生成")
    return PlainTextResponse(
        rep.summary_md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="report-{session_id}.md"'},
    )
