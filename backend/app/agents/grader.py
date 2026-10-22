"""阅卷官：独立的 Agent（SPEC 8.4）。

独立在哪：
- 输入只有题干、答案要点 + 代码片段、用户在这题上的全部回答；
- 不给它看面试官的 quick_assess 结论和出题计划，避免"自己出题自己判"的偏差；
- temperature=0；分数落在及格线 ±10 分内，或"部分命中"较多时，再判两次，
  每个要点取多数结论，并记录三次结果的一致率。
"""

from __future__ import annotations

import logging
import statistics
import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agents.interviewer import hint_penalty
from app.agents.schemas import GradeOut
from app.analysis import evidence as evcheck
from app.core.config import get_settings
from app.core.db import session_scope
from app.llm.client import get_llm, wrap_data
from app.llm.types import CallContext
from app.models import ExamSession, Grading, Question, SessionTurn
from app.repos.storage import ensure_checkout
from app.tools.sandbox import Sandbox

logger = logging.getLogger(__name__)

GRADER_SYSTEM = """你是独立的阅卷官，按"答案要点 + 代码证据"给候选人的回答判分。
规则：
- 逐个要点判定：hit 命中 / partial 部分命中 / miss 未命中，并写理由；
- 找出回答里与代码事实矛盾的说法，附上证据位置；
- 给出 0-100 的总分：主要看要点命中情况，有与代码矛盾的说法要扣分；
- 反馈要具体，指出缺了什么、哪里说错了；review_topics 写建议复习的考点；
- 只依据给出的代码和要点判分，不要因为回答写得长或语气自信就给高分。"""

WEIGHT = {"hit": 1.0, "partial": 0.5, "miss": 0.0}


def needs_rejudge(out: GradeOut, pass_score: int) -> bool:
    partial = sum(1 for p in out.points if p.verdict == "partial")
    return abs(out.score - pass_score) <= 10 or (out.points and partial / len(out.points) >= 0.5)


def merge_runs(runs: list[GradeOut], n_points: int) -> tuple[list[dict[str, Any]], float, int]:
    """多次判分合并：每个要点取多数结论；返回 (要点结果, 一致率, 合并后分数)。"""
    merged: list[dict[str, Any]] = []
    agree = 0
    for i in range(n_points):
        verdicts = [next((p for p in r.points if p.index == i), None) for r in runs]
        vs = [v.verdict if v else "miss" for v in verdicts]
        top, cnt = Counter(vs).most_common(1)[0]
        if cnt == len(vs):
            agree += 1
        reason = next((v.reason for v in verdicts if v and v.verdict == top), "")
        merged.append({"index": i, "verdict": top, "reason": reason, "votes": vs})
    agreement = agree / n_points if n_points else 1.0
    score = int(statistics.median([r.score for r in runs]))
    return merged, agreement, score


async def judge(
    question: dict[str, Any],
    answers: list[str],
    snippets: list[dict[str, Any]],
    ctx: CallContext,
    kb_refs: list[dict[str, Any]] | None = None,
    temperature: float = 0.0,
) -> tuple[GradeOut, int]:
    pts = []
    for i, kp in enumerate(question["key_points"]):
        ev = kp["evidence"][0]
        code = next(
            (s["text"] for s in snippets if s["path"] == ev["path"] and s["start_line"] == ev["start_line"]), ""
        )
        pts.append(
            f"要点 {i}: {kp['point']}\n证据 {ev['path']}:{ev['start_line']}-{ev['end_line']}\n```\n{code[:2500]}\n```"
        )
    kb_text = "\n".join(f"[{k['title']} / {k['heading_path']}] {k['content'][:500]}" for k in (kb_refs or []))
    user = (
        f"题目：{question['text']}\n\n{wrap_data('key_points_and_code', chr(10).join(pts))}\n\n"
        + (f"{wrap_data('knowledge_base', kb_text)}\n\n" if kb_text else "")
        + wrap_data("candidate_answers", "\n---\n".join(answers) or "（未作答）")
    )
    out, usage = await get_llm().structured(
        task="grade.judge",
        tier="strong",
        system=GRADER_SYSTEM,
        user=user,
        schema=GradeOut,
        ctx=ctx,
        temperature=temperature,
        payload={"key_points": question["key_points"], "answers": answers, "topic_key": question.get("topic_key")},
    )
    return out, usage.total


async def grade_question(session_id: uuid.UUID, question_id: uuid.UUID, job_id: str | None = None) -> dict[str, Any]:
    s = get_settings()
    async with session_scope() as db:
        sess = await db.get(ExamSession, session_id)
        q = await db.get(Question, question_id)
        assert sess is not None and q is not None
        turns = (
            (
                await db.execute(
                    select(SessionTurn)
                    .where(SessionTurn.session_id == session_id, SessionTurn.question_id == question_id)
                    .order_by(SessionTurn.seq)
                )
            )
            .scalars()
            .all()
        )
    answers = [t.content for t in turns if t.role == "user"]
    hint_level = max([int((t.meta or {}).get("level", 0)) for t in turns if t.role == "hint"], default=0)
    skipped = any(t.role == "system" and (t.meta or {}).get("skipped") for t in turns)
    qd = {"text": q.text, "key_points": q.key_points, "topic_key": q.topic_key}
    ctx = CallContext(node="grader", user_id=sess.user_id, session_id=session_id, job_id=job_id)

    if skipped or not answers:
        point_results = [{"index": i, "verdict": "miss", "reason": "跳过或未作答"} for i in range(len(q.key_points))]
        values: dict[str, Any] = {
            "point_results": point_results,
            "contradictions": [],
            "score": 0,
            "raw_score": 0,
            "hint_level": hint_level,
            "skipped": True,
            "feedback_md": "这道题跳过了，建议复习参考讲解。",
            "review_topics": [q.topic_key] if q.topic_key else [],
            "judge_runs": 0,
            "agreement": None,
        }
    else:
        root = (
            await ensure_checkout(sess.snapshot_id) if not sess.config.get("change_dir") else sess.config["change_dir"]
        )
        sandbox = Sandbox(root, sess.snapshot_id)
        snippets = []
        for kp in q.key_points:
            ev = kp["evidence"][0]
            t = evcheck.snippet(sandbox, ev)
            snippets.append({**ev, "text": t or ""})
        kb_refs = None
        if sess.mode == "kb_link":
            from app.kb.service import search

            kb_refs = await search(sess.user_id, q.text, top_k=3)
        runs: list[GradeOut] = []
        first, _ = await judge(qd, answers, snippets, ctx, kb_refs)
        runs.append(first)
        if needs_rejudge(first, s.pass_score):
            for _ in range(2):
                r, _ = await judge(qd, answers, snippets, ctx, kb_refs)
                runs.append(r)
        merged, agreement, score = merge_runs(runs, len(q.key_points))
        penalty = hint_penalty(hint_level)
        final = max(0, score - penalty)
        fb = runs[0].feedback
        if penalty:
            fb += f"\n\n（使用了 {hint_level} 级提示，扣 {penalty} 分）"
        values = {
            "point_results": merged,
            "contradictions": [c.model_dump() for c in runs[0].contradictions],
            "score": final,
            "raw_score": score,
            "hint_level": hint_level,
            "skipped": False,
            "feedback_md": fb,
            "review_topics": runs[0].review_topics or ([q.topic_key] if final < s.pass_score and q.topic_key else []),
            "judge_runs": len(runs),
            "agreement": agreement if len(runs) > 1 else None,
        }
    async with session_scope() as db:
        stmt = pg_insert(Grading).values(id=uuid.uuid4(), session_id=session_id, question_id=question_id, **values)
        await db.execute(stmt.on_conflict_do_update(constraint="uq_grading_session_question", set_=values))
        await db.commit()
    return values
